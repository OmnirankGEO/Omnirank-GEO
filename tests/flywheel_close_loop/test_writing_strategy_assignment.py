"""A4:写作策略指派账本必须真的有水,并且能回流成 outcome_events。

生产实证(2026-07-28):`writing_strategy_assignments` 0 行 + 全仓零写入代码,
`writing_strategy_versions` 31 行全是 shadow(active=0)。所以这里同时守两件事:
  ① 有 active 策略时按版本落账;
  ② **没有** active 策略(生产现状)时也按基线落账 —— 否则管道在启用之前永远是干的。
"""
from __future__ import annotations

import pytest

from db.connection import get_db
from services.writing_strategy_assignment import (
    RESOLUTION_ACTIVE,
    RESOLUTION_BASELINE,
    backfill_assignment_outcomes,
    record_strategy_assignment,
)

pytestmark = pytest.mark.integration


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall() or []]


def _make_strategy_version(industry_key: str = "geo_test") -> int:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO writing_strategy_versions (industry_key, strategy_version, status, style_family) "
            "VALUES (%s, %s, 'active', 'guide') RETURNING id",
            (industry_key, "v-test-1"),
        )
        return int(cur.fetchone()["id"])


def test_baseline_assignment_is_recorded_without_active_strategy(writing_flywheel_db):
    """🔒 关键:active 版本为 0 时也要落账,否则"管道有水"这条验收永远达不到。"""
    assert record_strategy_assignment(
        quote_id=9001, industry_key="geo_test", brand_id=77,
        strategy_id=None, resolution=RESOLUTION_BASELINE, style_family="guide",
    )

    rows = _rows("SELECT * FROM writing_strategy_assignments WHERE quote_id=%s", (9001,))
    assert len(rows) == 1
    assert rows[0]["strategy_id"] is None
    assert rows[0]["resolution"] == RESOLUTION_BASELINE
    assert rows[0]["assignment_status"] == "baseline"
    assert rows[0]["brand_id"] == 77


def test_active_strategy_assignment_records_strategy_id(writing_flywheel_db):
    strategy_id = _make_strategy_version()
    record_strategy_assignment(
        quote_id=9002, industry_key="geo_test", brand_id=88,
        strategy_id=strategy_id, strategy_version="v-test-1",
        resolution=RESOLUTION_ACTIVE, style_family="guide",
    )
    rows = _rows("SELECT * FROM writing_strategy_assignments WHERE quote_id=%s", (9002,))
    assert rows[0]["strategy_id"] == strategy_id
    assert rows[0]["assignment_status"] == "active"


def test_assignment_is_idempotent_per_quote_and_strategy(writing_flywheel_db):
    """同一 quote 反复开批次不许堆行 —— 否则账本会被重生成刷成噪音。"""
    for _ in range(3):
        record_strategy_assignment(
            quote_id=9003, industry_key="geo_test", strategy_id=None,
            resolution=RESOLUTION_BASELINE,
        )
    rows = _rows("SELECT * FROM writing_strategy_assignments WHERE quote_id=%s", (9003,))
    assert len(rows) == 1


def test_assignment_write_never_raises(writing_flywheel_db, monkeypatch):
    """落账失败绝不能阻断写作 —— 生成链路上任何异常都只准换成 False。"""
    import services.writing_strategy_assignment as mod

    def _boom(*_a, **_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(mod, "ensure_assignment_schema", _boom)
    assert record_strategy_assignment(quote_id=9004, industry_key="geo_test") is False


def test_backfill_writes_outcome_events_from_assignments(writing_flywheel_db, monkeypatch):
    """指派 → 文章 → 被引 → outcome_events。被引链路注入替身,不依赖 research 域表。"""
    import services.writing_strategy_assignment as mod

    record_strategy_assignment(
        quote_id=9005, industry_key="geo_test", brand_id=55,
        strategy_id=None, resolution=RESOLUTION_BASELINE,
    )
    # 指派要"够老"才进回流窗口。
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE writing_strategy_assignments SET assigned_at = NOW() - INTERVAL '45 days' "
            "WHERE quote_id = %s", (9005,)
        )

    monkeypatch.setattr(mod, "_articles_for_quote", lambda quote_id, assigned_at: [101, 102])
    monkeypatch.setattr(
        "services.writing_outcome_backfill._citation_count_for_articles",
        lambda ids, since_days=30: {
            "articles": len(ids), "matched_articles": 2, "citations": 6,
            "insufficient_data": False,
        },
    )

    result = backfill_assignment_outcomes(dry_run=False, min_age_days=30)
    assert result["written"] == 1
    assert result["measured"] == 1

    events = _rows("SELECT * FROM writing_strategy_outcome_events WHERE quote_id=%s", (9005,))
    assert len(events) == 1
    assert events[0]["ai_citations_delta_30d"] == 6
    assert events[0]["publish_status"] == "measured"
    assert events[0]["brand_id"] == 55
    assert events[0]["metadata"]["source"] == "assignment_backfill"

    # 同周重跑 → 刷新同一行,不堆积。
    backfill_assignment_outcomes(dry_run=False, min_age_days=30)
    assert len(_rows("SELECT * FROM writing_strategy_outcome_events WHERE quote_id=%s", (9005,))) == 1


def test_backfill_is_honest_when_no_publication_match(writing_flywheel_db, monkeypatch):
    """没发布/没匹配 → 诚实记 insufficient + 0 被引,绝不编造功效。"""
    import services.writing_strategy_assignment as mod

    record_strategy_assignment(quote_id=9006, industry_key="geo_test", strategy_id=None)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE writing_strategy_assignments SET assigned_at = NOW() - INTERVAL '45 days' "
            "WHERE quote_id = %s", (9006,)
        )

    monkeypatch.setattr(mod, "_articles_for_quote", lambda quote_id, assigned_at: [201])
    monkeypatch.setattr(
        "services.writing_outcome_backfill._citation_count_for_articles",
        lambda ids, since_days=30: {
            "articles": 1, "matched_articles": 0, "citations": 0, "insufficient_data": True,
        },
    )

    backfill_assignment_outcomes(dry_run=False, min_age_days=30)
    events = _rows("SELECT * FROM writing_strategy_outcome_events WHERE quote_id=%s", (9006,))
    assert events[0]["publish_status"] == "insufficient"
    assert events[0]["ai_citations_delta_30d"] == 0


def test_backfill_skips_assignments_younger_than_window(writing_flywheel_db, monkeypatch):
    import services.writing_strategy_assignment as mod

    record_strategy_assignment(quote_id=9007, industry_key="geo_test", strategy_id=None)
    monkeypatch.setattr(mod, "_articles_for_quote", lambda quote_id, assigned_at: [301])

    result = backfill_assignment_outcomes(dry_run=False, min_age_days=30)
    assert result["scanned"] == 0
    assert not _rows("SELECT * FROM writing_strategy_outcome_events WHERE quote_id=%s", (9007,))


def test_version_level_and_assignment_level_rows_coexist(writing_flywheel_db, monkeypatch):
    """两条回流各写各的行:版本级(new_version_id 非空)与指派级(quote_id 非空)不得互相覆盖。"""
    import services.writing_strategy_assignment as mod

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO writing_strategy_outcome_events
                (strategy_id, article_id, industry_key, publish_status,
                 ai_citations_delta_30d, new_version_id, measurement_window, observed_at)
            VALUES (NULL, NULL, 'geo_test', 'measured', 3, 'style-v9',
                    date_trunc('week', NOW())::date, NOW())
            """
        )

    record_strategy_assignment(quote_id=9008, industry_key="geo_test", strategy_id=None)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE writing_strategy_assignments SET assigned_at = NOW() - INTERVAL '45 days' "
            "WHERE quote_id = %s", (9008,)
        )
    monkeypatch.setattr(mod, "_articles_for_quote", lambda quote_id, assigned_at: [401])
    monkeypatch.setattr(
        "services.writing_outcome_backfill._citation_count_for_articles",
        lambda ids, since_days=30: {
            "articles": 1, "matched_articles": 1, "citations": 9, "insufficient_data": False,
        },
    )
    backfill_assignment_outcomes(dry_run=False, min_age_days=30)

    all_rows = _rows("SELECT * FROM writing_strategy_outcome_events ORDER BY id")
    assert len(all_rows) == 2
    assert {r["new_version_id"] for r in all_rows} == {"style-v9", None}
