# -*- coding: utf-8 -*-
"""迁移重放床:「第一次能过、第二次必挂」这一格,进分母。

## 为什么要两张床

2026-09-01 P0:040–054 用 `pg_get_constraintdef()` 的**渲染文本**跟写死的串比。
同一条逻辑约束「刚 ADD 出来」与「经过 dump→restore」渲染不同 ⇒
首次部署过、还原副本上重放必炸。**18 包 / 门八 / idxguard 都没模拟「在还原副本上重放」**。

- **床 A** 生产 schema 还原 → 重放 ×2 —— 覆盖存量渲染差异
- **床 B** 同上 → **重渲染往返** → 重放 ×2 —— 唯一抓得到本 bug 的那张床

修前实测:床 A 绿 / 床 B 红(点名 `[040] 约束 chk_defgeo_qplan_mode 定义漂移`)。
⇒ 两张床**不是复制品**,B 有独立区分力。

## 为什么用纯 SQL 往返而不是真 pg_dump

跑批镜像里 `pg_dump` 与 `psql` **都没有**(实测),真 dump/restore 形态进不了容器段。
而漂移发生在「DDL 文本被重新解析」那一刻,不在 dump 那一刻:
取 `pg_get_constraintdef()` → DROP → 用那段原文 re-ADD,产出与真
`pg_dump --schema-only | psql` **逐字节相同**的渲染(2026-09-01 并排比对过)。

## 床自己的活性

「重放两次全绿」有两种读法:真幂等,或者**这张床根本没让任何东西漂**
(比如副本两边本来就都是已还原形)。所以 `_rerender()` 快照往返前后并断言
**渲染真的变了 >= MIN_DRIFT 条**;不够就直接失败,后面的绿一律不作数。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import urllib.parse

import pytest

psycopg2 = pytest.importorskip("psycopg2")

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
DUMP = REPO / "tests/article_self_report_2026_08_19/prod_schema_2026-08-19.sql"
BS = chr(92)  # 反斜杠

# 2026-09-01 实测:869 条 CHECK 里 29 条重渲染后定义变了。取保守下限。
MIN_DRIFT = 20

_SNAP_CON = """
    SELECT t.relname || '.' || c.conname, pg_get_constraintdef(c.oid)
      FROM pg_constraint c
      JOIN pg_class t ON t.oid = c.conrelid
      JOIN pg_namespace n ON n.oid = t.relnamespace
     WHERE c.contype = 'c' AND n.nspname = 'public'
"""
_SNAP_OID = """
    SELECT t.relname || '.' || c.conname, c.oid
      FROM pg_constraint c
      JOIN pg_class t ON t.oid = c.conrelid
      JOIN pg_namespace n ON n.oid = t.relnamespace
     WHERE n.nspname = 'public' AND t.relname LIKE 'defgeo%'
"""


def _server_base() -> str:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.fail("TEST_DATABASE_URL 未配置 —— 这张床要一台能建库的 PG")
    p = urllib.parse.urlsplit(dsn)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, "/", "", "")).rstrip("/") + "/"


def _exec(dsn: str, sql: str):
    c = psycopg2.connect(dsn)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute(sql)
    finally:
        c.close()


def _fetch(dsn: str, sql: str) -> dict:
    c = psycopg2.connect(dsn)
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return dict(cur.fetchall())
    finally:
        c.close()


def _fresh(base: str, db: str):
    _exec(base + "postgres", 'DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % db)
    _exec(base + "postgres", 'CREATE DATABASE "%s"' % db)


def _restore_prod(dsn: str):
    """还原生产 schema。psql 元命令按**行**剥掉。

    🔴 不用含反斜杠的正则:那种字面量在本仓的工具链里会被吃掉一层
    (2026-09-01 栽了三次),而剥不掉的表现是 `syntax error at or near "\\"`。
    """
    assert DUMP.exists(), "生产 schema dump 不在:%s" % DUMP
    text = DUMP.read_text(encoding="utf-8", errors="replace")
    kept = [ln for ln in text.splitlines() if not ln.lstrip().startswith(BS)]
    _exec(dsn, chr(10).join(kept))


def _replay(dsn: str) -> tuple[int, str]:
    env = {**os.environ, "REPLAY_ROOT": str(REPO), "REPLAY_QUIET": "1",
           "DATABASE_URL": dsn, "TEST_DATABASE_URL": dsn}
    r = subprocess.run([sys.executable, str(HERE / "replay_driver.py")],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, timeout=2400)
    keep = ("[base]", "[ok]", "[FAIL]", "       ")
    return r.returncode, chr(10).join(
        ln for ln in (r.stdout + r.stderr).splitlines() if ln.startswith(keep))


def _rerender(dsn: str) -> int:
    """纯 SQL 往返;返回**渲染真的变了**的条数。

    只动 `contype='c'`:UNIQUE/PK/FK 渲染成列名清单本来就稳定,而且背后有索引与
    外键依赖,DROP 会连带别的对象 —— 动它们会引入与本 bug 无关的红。
    """
    before = _fetch(dsn, _SNAP_CON)
    c = psycopg2.connect(dsn)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT n.nspname, t.relname, c.conname, pg_get_constraintdef(c.oid)
                  FROM pg_constraint c
                  JOIN pg_class t ON t.oid = c.conrelid
                  JOIN pg_namespace n ON n.oid = t.relnamespace
                 WHERE c.contype = 'c' AND n.nspname = 'public'
            """)
            for ns, tbl, con, cdef in cur.fetchall():
                cur.execute('ALTER TABLE "%s"."%s" DROP CONSTRAINT "%s"' % (ns, tbl, con))
                cur.execute('ALTER TABLE "%s"."%s" ADD CONSTRAINT "%s" %s'
                            % (ns, tbl, con, cdef))
    finally:
        c.close()
    after = _fetch(dsn, _SNAP_CON)
    return sum(1 for k in before.keys() & after.keys() if before[k] != after[k])


@pytest.fixture(scope="module")
def base() -> str:
    return _server_base()


def test_mrb_01_bed_a_fresh_prod_schema_replays_twice(base):
    """床 A:生产 schema 还原之后,连续重放两次都必须全绿。"""
    db = "migreplay_bed_a_test"
    _fresh(base, db)
    dsn = base + db
    _restore_prod(dsn)
    rc1, out1 = _replay(dsn)
    assert rc1 == 0, "床 A 第 1 次重放失败:" + chr(10) + out1
    rc2, out2 = _replay(dsn)
    assert rc2 == 0, "床 A 第 2 次重放失败:" + chr(10) + out2


def test_mrb_02_bed_b_restored_copy_replays_twice(base):
    """🔴🔴 床 B:还原副本(= 渲染被重写过)上连续重放两次都必须全绿。

    这是「首次部署过、第二次必炸」那一格。修前它红,并点名
    `[040] 约束 chk_defgeo_qplan_mode 定义漂移`。
    """
    db = "migreplay_bed_b_test"
    _fresh(base, db)
    dsn = base + db
    _restore_prod(dsn)
    rc0, out0 = _replay(dsn)
    assert rc0 == 0, "床 B 预置失败,后面的读数没有基准:" + chr(10) + out0

    drifted = _rerender(dsn)
    assert drifted >= MIN_DRIFT, (
        "床的活性守卫:往返后只有 %d 条约束的渲染变了(应 >= %d)。"
        "这张床没在测它该测的东西 —— 后面的绿一律不作数。" % (drifted, MIN_DRIFT))

    rc1, out1 = _replay(dsn)
    assert rc1 == 0, "还原副本上第 1 次重放失败:" + chr(10) + out1
    rc2, out2 = _replay(dsn)
    assert rc2 == 0, "还原副本上第 2 次重放失败:" + chr(10) + out2


def test_mrb_03_second_replay_does_not_rebuild_objects(base):
    """🔴 终态信号:第二遍重放**没有把对象 DROP 重建**(oid 不变)。

    「重放两次全绿」还可能是「每次 DROP 重建都成功」——那样每次部署都有一段
    「约束不存在」的窗口,并发写在那一瞬间不受约束。oid 不变才证 no-op。
    形态抄自 `tests/geo_image_note_2026_08_17` 的
    `test_034_replay_does_not_rebuild_the_live_root_index`。
    """
    db = "migreplay_bed_oid_test"
    _fresh(base, db)
    dsn = base + db
    _restore_prod(dsn)
    rc, out = _replay(dsn)
    assert rc == 0, "预置失败:" + chr(10) + out

    before = _fetch(dsn, _SNAP_OID)
    assert len(before) >= 50, (
        "只抓到 %d 个 defgeo_* 对象 —— 分母不对,这条判据等于没跑" % len(before))
    rc, out = _replay(dsn)
    assert rc == 0, "第二遍重放失败:" + chr(10) + out
    after = _fetch(dsn, _SNAP_OID)

    rebuilt = sorted(k for k in before.keys() & after.keys() if before[k] != after[k])
    assert not rebuilt, (
        "第二遍重放把这些对象 DROP 重建了(存在无约束窗口):" + ", ".join(rebuilt[:12]))
    gone = sorted(before.keys() - after.keys())
    assert not gone, "第二遍重放之后这些对象没了:" + ", ".join(gone[:12])
