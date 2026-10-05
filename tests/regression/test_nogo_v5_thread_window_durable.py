"""[v5 req4] 文章生成 · 扣费后线程未启动窗口 · 耐久补偿 4 分支行为测试。

模拟"强制 thread.start 抛异常"后进入收口(precondition: accepted 非空 + thread_started=False + deducted=True),
真实驱动 services.article_write_recovery.handle_write_window_failure,覆盖:
  ① 退款成功 → topics 恢复可重试(release)· 无补偿工单;
  ② 退款返回 false → 标 write_timeout + 落 refund 耐久工单(带 charge_tx_id);
  ③ 退款抛异常 → 标 write_timeout + 落 refund 耐久工单;
  ④ 退款成功但状态恢复(release)失败 → 落 state_fix 耐久工单。
recovery_fn 用【真实 create_recovery_order】写 throwaway 库 → 断言 fund_recovery_orders 真有行(耐久 · 非静默吞)。

判别性:把 handle_write_window_failure 的失败分支改回 except:pass(不落工单)→ ②③④ 的工单行断言失败。
"""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path
import pytest
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _dbsafe import resolve_test_db_url, require_destructive_allowed, _assert_safe_test_db  # noqa: E402

DB = resolve_test_db_url()
pytestmark = pytest.mark.skipif(not DB, reason="需 TEST_DATABASE_URL(本机 test/throwaway 库)")

TXID = 55501
QUOTE = 4242


def _conn():
    c = psycopg2.connect(DB); c.cursor_factory = psycopg2.extras.RealDictCursor
    return c


@pytest.fixture(autouse=True)
def _clean():
    require_destructive_allowed(); _assert_safe_test_db(DB)
    from db.fund_recovery_db import init_fund_recovery_tables
    with _conn() as c:
        init_fund_recovery_tables(c.cursor()); c.commit()
        c.cursor().execute("DELETE FROM fund_recovery_orders"); c.commit()
    yield
    with _conn() as c:
        c.cursor().execute("DELETE FROM fund_recovery_orders"); c.commit()


def _recovery_fn(kind, *, refund_err=None, state_err=None):
    """真实落耐久工单(source 固定 · 带 charge_tx_id)· 供被测函数注入。"""
    from db.fund_recovery_db import create_recovery_order
    return create_recovery_order(
        "article_gen_thread_window", kind, ref_key=str(QUOTE), user_id=777,
        feature_code="article_gen", charge_tx_id=TXID,
        reason="扣费后线程未启动 · 自动收口失败", last_error=(refund_err or state_err),
        payload={"quote_id": QUOTE},
    )


def _rows():
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT kind, status, charge_tx_id, last_error FROM fund_recovery_orders ORDER BY id")
        return cur.fetchall()


def _run(**kw):
    from services.article_write_recovery import handle_write_window_failure
    released = {"ids": None}
    timed_out = {"ids": None}
    _release_raises = kw.pop("_release_raises", False)  # 测试专用开关 · 不传入被测函数

    def _release(ids, qid):
        if _release_raises:
            raise RuntimeError("release DB down")
        released["ids"] = list(ids)

    def _timeout(ids, qid):
        timed_out["ids"] = list(ids)

    base = dict(
        accepted=[1, 2, 3], thread_started=False, deducted=True,
        bill_user_id=777, charge_tx_id=TXID, quote_id=QUOTE,
        release_fn=_release, timeout_fn=_timeout, recovery_fn=_recovery_fn,
    )
    base.update(kw)
    out = asyncio.run(handle_write_window_failure(**base))
    return out, released, timed_out


def test_branch1_refund_success_releases_no_workorder():
    async def _ok_refund(uid, feat, *, reason, charge_tx_id, ledger_type=None):
        assert charge_tx_id == TXID, "必须按本批 charge_tx_id 精确退"
        return {"success": True}
    out, released, timed_out = _run(refund_fn=_ok_refund)
    assert out["outcome"] == "refunded_and_released"
    assert released["ids"] == [1, 2, 3], "退款成功后 topics 必须 release 回可重试"
    assert _rows() == [], "退款成功不应产生补偿工单"


def test_branch2_refund_false_workorder_and_timeout():
    async def _false_refund(uid, feat, *, reason, charge_tx_id, ledger_type=None):
        return {"success": False, "reason": "未找到扣费记录"}
    out, released, timed_out = _run(refund_fn=_false_refund)
    assert out["outcome"] == "refund_failed_workorder"
    assert timed_out["ids"] == [1, 2, 3], "退款 false → topics 标 write_timeout"
    rows = _rows()
    assert len(rows) == 1 and rows[0]["kind"] == "refund", f"退款 false 必须落 refund 耐久工单: {rows}"
    assert rows[0]["charge_tx_id"] == TXID and rows[0]["status"] == "pending"


def test_branch3_refund_raises_workorder():
    async def _raise_refund(uid, feat, *, reason, charge_tx_id, ledger_type=None):
        raise RuntimeError("billing timeout")
    out, released, timed_out = _run(refund_fn=_raise_refund)
    assert out["outcome"] == "refund_failed_workorder"
    rows = _rows()
    assert len(rows) == 1 and rows[0]["kind"] == "refund", f"退款异常必须落 refund 耐久工单: {rows}"
    assert "billing timeout" in (rows[0]["last_error"] or ""), "工单必须记录退款异常原文"


def test_branch4_refund_ok_but_release_fails_state_fix_workorder():
    async def _ok_refund(uid, feat, *, reason, charge_tx_id, ledger_type=None):
        return {"success": True}
    out, released, timed_out = _run(refund_fn=_ok_refund, _release_raises=True)
    assert out["outcome"] == "refunded_state_fix_workorder"
    rows = _rows()
    assert len(rows) == 1 and rows[0]["kind"] == "state_fix", f"退款成功但状态恢复失败 → state_fix 工单: {rows}"


def test_noop_when_thread_started():
    async def _ok_refund(uid, feat, *, reason, charge_tx_id, ledger_type=None):
        return {"success": True}
    out, _, _ = _run(refund_fn=_ok_refund, thread_started=True)
    assert out["outcome"] == "noop"
    assert _rows() == [], "线程已启动 → 不收口不落工单"


def test_recovery_dedup_only_on_nonnull_charge_tx():
    """[v5 对抗审 P3] 同 charge_tx_id(非空)重复登记 → 去重返既有 id(不谎报 None);
    charge_tx_id 为 None 的两笔退款 → 各自入库(不误并 · 防漏退)。

    [v7 二轮对抗审] 一度尝试对 null-charge 同 ref_key 去重,但 ref_key=quote_id 非 per-charge 判别键,
    同 quote 两笔不同扣费会被误并致【漏退】(客户少退款 · 比 over-refund 更糟)→ 已撤销,保 v6 不去重行为。
    根治需持久化 charge order-group key 精确退款(超 v7 范围 · 见出口报告已知遗留)。"""
    from db.fund_recovery_db import create_recovery_order
    with _conn() as c:
        c.cursor().execute("DELETE FROM fund_recovery_orders"); c.commit()
    # 非空 charge_tx_id 重复 → 去重
    id1 = create_recovery_order("article_gen_thread_window", "refund", ref_key="Q9", charge_tx_id=70001)
    id2 = create_recovery_order("article_gen_thread_window", "refund", ref_key="Q9", charge_tx_id=70001)
    assert id1 is not None and id2 == id1, f"同笔重复登记须去重返既有 id(非 None): {id1} vs {id2}"
    with _conn() as c:
        cur = c.cursor(); cur.execute("SELECT COUNT(*) AS n FROM fund_recovery_orders WHERE charge_tx_id=70001")
        assert cur.fetchone()["n"] == 1, "同 charge_tx_id 只一条 pending"
    # None charge_tx_id 两笔退款 → 都入库(不误并 · 防漏退)
    n1 = create_recovery_order("article_gen_thread_window", "refund", ref_key="Q9", charge_tx_id=None)
    n2 = create_recovery_order("article_gen_thread_window", "refund", ref_key="Q9", charge_tx_id=None)
    assert n1 is not None and n2 is not None and n1 != n2, "🔴 两笔 null-charge 退款各自入库(不误并致漏退)"
    with _conn() as c:
        cur = c.cursor(); cur.execute("SELECT COUNT(*) AS n FROM fund_recovery_orders WHERE charge_tx_id IS NULL")
        assert cur.fetchone()["n"] == 2, "两笔 null-charge 各自入库(耐久不丢)"


def test_no_except_pass_in_recovery_source():
    """禁 except: pass 回退锁:被测收口函数源码不得含裸 except: pass。"""
    import re
    src = (ROOT / "services" / "article_write_recovery.py").read_text(encoding="utf-8")
    # 行首锚定的【代码】裸吞检测(不误伤 docstring/注释里提到 "except: pass" 的散文)
    assert not re.search(r"(?m)^\s*except\b[^\n]*:\s*pass\s*$", src), "禁裸 except ...: pass 静默吞"
    # server.py 的收口闭包也不得回退成裸吞
    ssrc = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "handle_write_window_failure" in ssrc, "server.py 必须委托到可测收口函数"
