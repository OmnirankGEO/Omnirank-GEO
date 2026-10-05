"""[P0-1] round-scoped flywheel bridge worker:
- source_signals stage paginates without dropping rows (>page_size).
- idempotent rerun (same round twice) does not create duplicate rows.
- dry_run writes nothing.
- answer_entities stage is skipped (no LLM) while flag flywheel_answer_entity_auto is OFF.
"""
import asyncio

import pytest

import db.diagnosis_db  # noqa: F401  建 geo_research_raw / geo_research_round / articles / citations
from db.connection import get_connection
from db.geo_source_signals_db import init_geo_source_signal_tables


def _seed_round_raw(conn, batch_id, rows):
    """rows: list of (engine, query, cite_url, cite_position, answer_text, is_answer_cited)."""
    c = conn.cursor()
    for (engine, query, url, pos, ans, cited) in rows:
        c.execute(
            """
            INSERT INTO geo_research_raw
                (industry, query, engine, cited_platform, cite_position, cite_url,
                 cite_title, cite_excerpt, answer_text, is_answer_cited, adoption_rank,
                 batch_id, researcher)
            VALUES ('教育培训', %s, %s, '', %s, %s, '', '', %s, %s, NULL, %s, '')
            """,
            (query, engine, pos, url, ans, cited, batch_id),
        )
    conn.commit()


def _count(conn, sql, params=()):
    """读计数后必须结束事务,否则夹具连接会一直持该表的 ACCESS SHARE 锁。

    [2026-08-01 自锁] 夹具连接非 autocommit:一次 SELECT 就开了事务,不收尾就停在
    `idle in transaction`。紧接着 _stage_source_signals 里的
    init_geo_source_signal_tables() 要对同一张表做 `ALTER TABLE ... ADD COLUMN`
    (ACCESS EXCLUSIVE)→ 与自己的读锁互斥 → 测试挂死,不是报错是永久 hang。
    这是测试脚手架自身的缺陷:在 37c49950 引入 SQL 保留字别名后,用例早在上一行
    就抛 syntax error,从没执行到这里,所以该自锁一直没暴露。
    rollback() 而非 commit():这是只读计数,不该产生任何写副作用。
    """
    c = conn.cursor()
    c.execute(sql, params)
    row = c.fetchone()
    value = int(list(row.values())[0]) if row else 0
    conn.rollback()
    return value


def _clean_signals(conn, batch_id):
    c = conn.cursor()
    c.execute("DELETE FROM geo_research_source_signals WHERE round_id = %s", (batch_id,))
    conn.commit()


def test_source_signals_stage_paginates_and_is_idempotent(db_with_clean_research):
    from services.research_monitor.flywheel_bridge import _stage_source_signals
    conn = db_with_clean_research
    init_geo_source_signal_tables()
    batch_id = "batch_RTEST_PAGINATE"
    _clean_signals(conn, batch_id)
    _seed_round_raw(conn, batch_id, [
        ("doubao", "q1", "https://a.com/1", 1, "答案[1]", False),
        ("doubao", "q1", "https://b.com/2", 2, "答案[1]", False),
        ("kimi", "q2", "https://c.com/3", 1, "回答无标记", False),
        ("kimi", "q2", "https://d.com/4", 2, "回答无标记", False),
        ("deepseek", "q3", "https://e.com/5", 1, "文本", False),
    ])

    # page_size=2 forces 3 pages over 5 rows — must not drop any row.
    res = _stage_source_signals(batch_id, page_size=2, dry_run=False)
    assert res["loaded"] == 5, "分页不能漏行:5 行必须全部 loaded"
    assert res["pages"] >= 3
    assert res["written"] == 5
    total1 = _count(conn, "SELECT COUNT(*) AS c FROM geo_research_source_signals WHERE round_id=%s", (batch_id,))
    assert total1 == 5
    assert res["inserted"] == 5
    assert res["last_processed_raw_id"] is not None
    assert sum(res["by_engine"].values()) == 5

    # idempotent rerun → same total, inserted 0 (upsert updates in place)
    res2 = _stage_source_signals(batch_id, page_size=100, dry_run=False)
    total2 = _count(conn, "SELECT COUNT(*) AS c FROM geo_research_source_signals WHERE round_id=%s", (batch_id,))
    assert total2 == total1, "重跑同一 round 写入必须幂等"
    assert res2["inserted"] == 0

    _clean_signals(conn, batch_id)


def test_source_signals_dry_run_writes_nothing(db_with_clean_research):
    from services.research_monitor.flywheel_bridge import _stage_source_signals
    conn = db_with_clean_research
    init_geo_source_signal_tables()
    batch_id = "batch_RTEST_DRY"
    _clean_signals(conn, batch_id)
    _seed_round_raw(conn, batch_id, [("doubao", "q", "https://x.com/1", 1, "答案[1]", False)])
    before = _count(conn, "SELECT COUNT(*) AS c FROM geo_research_source_signals WHERE round_id=%s", (batch_id,))
    res = _stage_source_signals(batch_id, page_size=100, dry_run=True)
    after = _count(conn, "SELECT COUNT(*) AS c FROM geo_research_source_signals WHERE round_id=%s", (batch_id,))
    assert after == before == 0
    assert res["written"] == 0
    assert res["loaded"] == 1
    _clean_signals(conn, batch_id)


def test_run_round_bridge_dry_run_no_write_and_answer_entity_flag_off(db_with_clean_research):
    from services.research_monitor.flywheel_bridge import run_round_bridge
    conn = db_with_clean_research
    init_geo_source_signal_tables()
    round_id = "RTEST_BRIDGE"
    batch_id = "batch_RTEST_BRIDGE"
    _clean_signals(conn, batch_id)
    _seed_round_raw(conn, batch_id, [("doubao", "q", "https://y.com/1", 1, "答案[1]", False)])
    before = _count(conn, "SELECT COUNT(*) AS c FROM geo_research_source_signals WHERE round_id=%s", (batch_id,))

    res = asyncio.run(run_round_bridge(round_id, dry_run=True))

    after = _count(conn, "SELECT COUNT(*) AS c FROM geo_research_source_signals WHERE round_id=%s", (batch_id,))
    assert after == before, "dry_run 不得写库"
    assert res["dry_run"] is True
    assert res["status"] in ("success", "partial")
    assert "source_signals" in res["stages"]
    # 答案实体 flag 默认关 → 跳过,零 LLM 成本
    ae = res["stages"]["answer_entities"]
    assert ae.get("status") == "skipped"
    assert ae.get("reason") == "flag_off"
    assert ae.get("llm_called") is False
    _clean_signals(conn, batch_id)
