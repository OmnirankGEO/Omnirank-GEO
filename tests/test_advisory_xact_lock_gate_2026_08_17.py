"""§1 判据 · pg_advisory_xact_lock 在 autocommit 连接上等于没锁(WO-LATENT-TRAPS 2026-08-17)

四组判据,每个"必须命中"都配一个"必须不命中":

  A. 真 PG 行为自证 —— 先证明这个坑**是真的**,再证明闸挡得住
     A1 autocommit 连接取 xact 锁后,**另一条连接立刻能拿到同一把锁** = 锁当场没了(必须命中)
     A2 事务连接取同一把锁后,另一条连接**拿不到** = 锁真的在                (必须不命中)
  B. 闸的判别力
     B1 autocommit 连接调 require_xact_scope → 抛 XactLockScopeError        (必须命中)
     B2 事务连接调 require_xact_scope → 静默通过,证明不是恒红               (必须不命中)
     B3 取锁后 assert_lock_scope_effective:autocommit 抛 / 事务通过
  C. 普查回归闸 —— 新代码再引入 autocommit callsite 就红
  D. 接线锁 + 分母对账 —— 锁的是**接线**不是函数(删掉任一处 require_xact_scope 就红)

真 PG 判据需要一个可写的空库,通过 `XACT_GATE_DATABASE_URL`(或 `TEST_DATABASE_URL`)给;
没给就 skip —— 但 C/D 两组是纯静态的,**任何环境都跑**,不会因为没库而整组消失。
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "research"))

from db.xact_lock_guard import (  # noqa: E402
    XactLockScopeError,
    assert_lock_scope_effective,
    require_xact_scope,
)

LOCK_KEY = (920799, 20260817)   # 本判据专用键,不与任何生产 classid 撞


# --------------------------------------------------------------------- 真 PG
def _pg_url():
    return os.environ.get("XACT_GATE_DATABASE_URL") or os.environ.get("TEST_DATABASE_URL")


@pytest.fixture()
def pg():
    url = _pg_url()
    if not url:
        pytest.skip("未给 XACT_GATE_DATABASE_URL / TEST_DATABASE_URL,跳过真 PG 判据")
    psycopg2 = pytest.importorskip("psycopg2")
    conns = []

    def _open(autocommit):
        c = psycopg2.connect(url)
        c.autocommit = autocommit
        conns.append(c)
        return c

    yield _open
    for c in conns:
        try:
            c.rollback()
        except Exception:
            pass
        try:
            c.close()
        except Exception:
            pass


def _try_lock_from_other_conn(opener) -> bool:
    """另开一条连接用 pg_try_advisory_xact_lock 探同一把锁。
    True = 拿到了(说明第一条连接**没在持锁**)。
    """
    other = opener(False)
    cur = other.cursor()
    cur.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", LOCK_KEY)
    got = cur.fetchone()[0]
    other.rollback()
    return bool(got)


def test_A1_autocommit_lock_is_released_immediately(pg):
    """必须命中:autocommit 下取完锁,别人立刻能拿 —— 这就是"锁了等于没锁"的实证。"""
    victim = pg(True)                       # autocommit=True
    cur = victim.cursor()
    cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", LOCK_KEY)
    assert _try_lock_from_other_conn(pg) is True, (
        "反了 —— autocommit 下锁竟然还在?那这条判据没打到被测行为上"
    )


def test_A2_transactional_lock_is_actually_held(pg):
    """必须不命中(反向对照):事务连接持锁期间,别人拿不到 —— 证明 A1 不是环境问题。"""
    holder = pg(False)                      # autocommit=False
    cur = holder.cursor()
    cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", LOCK_KEY)
    assert _try_lock_from_other_conn(pg) is False, (
        "事务连接持锁期间别人也能拿到 —— 说明测试库/键选错了,A1 的'命中'是假的"
    )
    holder.rollback()


def test_B1_guard_raises_on_autocommit(pg):
    """必须命中:闸在 autocommit 连接上抛。"""
    conn = pg(True)
    with pytest.raises(XactLockScopeError) as exc:
        require_xact_scope(conn.cursor(), where="判据 B1")
    assert "autocommit" in str(exc.value)
    assert "判据 B1" in str(exc.value), "报错必须带位置,否则线上排查还得再找一轮"


def test_B2_guard_is_silent_on_transactional_conn(pg):
    """必须不命中(反向对照):事务连接上闸必须静默放行 —— 证明它不是恒红。"""
    conn = pg(False)
    require_xact_scope(conn.cursor(), where="判据 B2")   # 不抛即通过


def test_B3_post_lock_assert_discriminates(pg):
    """取锁**之后**的行为级判据:autocommit 抛 / 事务通过。"""
    bad = pg(True)
    bcur = bad.cursor()
    bcur.execute("SELECT pg_advisory_xact_lock(%s, %s)", LOCK_KEY)
    with pytest.raises(XactLockScopeError):
        assert_lock_scope_effective(bcur, where="判据 B3 正样本")

    good = pg(False)
    gcur = good.cursor()
    gcur.execute("SELECT pg_advisory_xact_lock(%s, %s)", LOCK_KEY)
    assert_lock_scope_effective(gcur, where="判据 B3 反向对照")   # 不抛即通过
    good.rollback()


# ------------------------------------------------------------- 纯静态(必跑)
def test_C_census_reports_zero_autocommit_callsites():
    """普查回归闸:任何新代码把 xact 锁写到 autocommit 连接上,这条当场红。"""
    from advisory_lock_autocommit_census import census

    rows, _grep = census(REPO_ROOT)
    hits = [r for r in rows if r["kind"] == "AUTOCOMMIT"]
    assert rows, "普查一个 callsite 都没扫到 —— 探针没打进去,这条判据零判别力"
    assert not hits, "事务级 advisory lock 取在 autocommit 连接上:\n" + "\n".join(
        "  %s:%d %s  (%s)" % (h["file"], h["line"], h["fn"], h["evidence"]) for h in hits
    )


def test_D1_every_unresolvable_callsite_is_gated():
    """接线锁:静态追不出连接来源的每一处,都必须有 require_xact_scope 兜着。

    锁的是**接线**不是函数 —— 删掉任意一处 require_xact_scope 这条就红。
    """
    import wire_xact_lock_guard as wiring

    rows = wiring.targets(REPO_ROOT)
    assert rows, "需接线集合为空 —— 要么普查坏了,要么分类口径漂了,两种都不许静默通过"
    missing = []
    for r in rows:
        path = os.path.join(REPO_ROOT, r["file"])
        with open(path, "r", encoding="utf-8", newline="") as fh:
            lines = fh.read().replace("\r\n", "\n").split("\n")
        window = lines[max(0, r["line"] - 3): r["line"] - 1]
        if not any("require_xact_scope(" in w for w in window):
            missing.append("%s:%d %s" % (r["file"], r["line"], r["fn"]))
    assert not missing, "以下 xact 锁调用点没有事务硬闸:\n  " + "\n  ".join(missing)


def test_D2_denominator_reconciles_both_directions():
    """分母自证:git grep 每一行都归了类(ORPHAN=0),且每个 AST callsite 都被 grep 覆盖到。"""
    script = os.path.join(REPO_ROOT, "scripts", "research", "advisory_lock_denominator_reconcile.py")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, script], cwd=REPO_ROOT, capture_output=True,
        text=True, encoding="utf-8", errors="replace", env=env,
    )
    assert proc.returncode == 0, "分母对账失败:\n" + (proc.stdout or "") + (proc.stderr or "")
    assert "ORPHAN    0" in proc.stdout or "ORPHAN=0" in proc.stdout or "对账结果: PASS" in proc.stdout
