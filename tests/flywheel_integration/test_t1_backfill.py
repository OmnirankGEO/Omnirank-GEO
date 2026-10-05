"""[T1] 全局 catch-up backfill:完成轮 raw → source_signals(dry_run 不写 / 真跑写 / 幂等)。"""
import asyncio

import pytest

import db.diagnosis_db  # noqa: F401  建 geo_research_raw / geo_research_round
from db.connection import get_connection
from db.geo_source_signals_db import init_geo_source_signal_tables


_ROUND = "T1BACKFILL"
_BATCH = "batch_T1BACKFILL"


def _seed_completed_round(conn):
    cur = conn.cursor()
    cur.execute("DELETE FROM geo_research_round WHERE round_id = %s", (_ROUND,))
    cur.execute(
        """
        INSERT INTO geo_research_round (round_id, batch_id, triggered_by, status, created_at)
        VALUES (%s, %s, 'manual', 'completed', NOW())
        """,
        (_ROUND, _BATCH),
    )
    for i in range(3):
        cur.execute(
            """
            INSERT INTO geo_research_raw
                (industry, query, engine, cited_platform, cite_position, cite_url,
                 cite_title, cite_excerpt, answer_text, is_answer_cited, adoption_rank, batch_id, researcher)
            VALUES ('教育培训', 'q', 'doubao', '', %s, %s, '', '', '答案', FALSE, NULL, %s, '')
            """,
            (i + 1, f"https://t1b.com/{i}", _BATCH),
        )
    conn.commit()


def _count(conn, sql, params=()):
    """读计数后必须结束事务 —— 否则夹具连接持 ACCESS SHARE 锁,把自己锁死。

    [2026-08-01 自锁] 与 test_flywheel_bridge._count 同一缺陷:夹具连接非 autocommit,
    一次 SELECT 就开事务;紧接着 run_source_signals_backfill → _stage_source_signals →
    init_geo_source_signal_tables() 要 `ALTER TABLE geo_research_source_signals`
    (ACCESS EXCLUSIVE)→ 与自己的读锁互斥 → 永久 hang(不是报错,是挂死)。
    37c49950 引入 SQL 保留字别名后用例更早就炸,所以这个自锁一直没暴露。
    """
    cur = conn.cursor()
    cur.execute(sql, params)
    row = cur.fetchone()
    value = int(list(row.values())[0]) if row else 0
    conn.rollback()
    return value


def _clean(conn):
    cur = conn.cursor()
    cur.execute("DELETE FROM geo_research_source_signals WHERE round_id = %s", (_BATCH,))
    cur.execute("DELETE FROM geo_research_round WHERE round_id = %s", (_ROUND,))
    conn.commit()


def test_backfill_targets_completed_round_and_is_idempotent(db_with_clean_research):
    from services.research_monitor.flywheel_bridge import (
        _backfill_target_batches,
        run_source_signals_backfill,
    )
    conn = db_with_clean_research
    init_geo_source_signal_tables()
    _clean(conn)
    _seed_completed_round(conn)

    # 目标批次含本完成轮
    batches = _backfill_target_batches(0, 100)
    assert _BATCH in batches

    # dry_run 不写库
    before = _count(conn, "SELECT COUNT(*) AS c FROM geo_research_source_signals WHERE round_id=%s", (_BATCH,))
    dry = asyncio.run(run_source_signals_backfill(since_days=0, dry_run=True, max_rounds=100))
    after_dry = _count(conn, "SELECT COUNT(*) AS c FROM geo_research_source_signals WHERE round_id=%s", (_BATCH,))
    assert dry["dry_run"] is True
    assert dry["batches_processed"] >= 1
    assert after_dry == before, "dry_run 不得写库"

    # 真跑写入
    real = asyncio.run(run_source_signals_backfill(since_days=0, dry_run=False, max_rounds=100))
    total1 = _count(conn, "SELECT COUNT(*) AS c FROM geo_research_source_signals WHERE round_id=%s", (_BATCH,))
    assert real["dry_run"] is False
    assert total1 >= 1

    # 幂等:再跑一遍不新增
    asyncio.run(run_source_signals_backfill(since_days=0, dry_run=False, max_rounds=100))
    total2 = _count(conn, "SELECT COUNT(*) AS c FROM geo_research_source_signals WHERE round_id=%s", (_BATCH,))
    assert total2 == total1, "backfill 必须幂等"

    _clean(conn)
