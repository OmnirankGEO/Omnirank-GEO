# -*- coding: utf-8 -*-
"""Renewal monitoring lifecycle regressions.

The monitoring UI lists keywords at brand scope, but run/enable/compliance used
separate quote-scope rules.  Renewal customers whose old quote was calendar
expired but not yet fulfilled could see keywords while monitoring refused to run.
"""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MON_DB = (ROOT / "db" / "monitoring_db.py").read_text(encoding="utf-8")
MON_API = (ROOT / "api" / "monitoring_api.py").read_text(encoding="utf-8")
SCHEDULER = (ROOT / "api" / "scheduler.py").read_text(encoding="utf-8")
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")


def _block(src: str, marker: str, span: int = 6500) -> str:
    start = src.find(marker)
    assert start >= 0, f"missing marker: {marker}"
    return src[start : start + span]


def test_service_anchor_helper_allows_expired_paid_until_fulfilled():
    """expired is not a hard stop when compliant_days is still below service_days."""
    assert "def quote_service_anchor_condition_sql(" in MON_DB
    helper = _block(MON_DB, "def quote_service_anchor_condition_sql(", 4500)

    assert "cancelled" in helper and "inactive" in helper
    assert "NOT IN ('cancelled', 'inactive')" in helper
    assert "service_status" in helper and "<> 'expired'" in helper
    assert "compliant_days" in helper
    # [服务期 SSOT 2026-08-06] migration_028 把 service_days 置 NOT NULL 后,
    #   `COALESCE(..., 365)` 是可证明的死代码,已拔除。这条要守的语义
    #   (达标天数未满就不硬停)不变,只是不再钉那个兜底字面量。
    #   断言改钉到**真正生成这段 SQL 的** quote_unfulfilled_compliance_condition_sql 上
    #   —— 旧写法靠 `_block(..., 4500)` 一路读到后面别的函数才碰巧命中 COALESCE,
    #      那是"判据打偏了还恰好绿"。
    unfulfilled = _block(MON_DB, "def quote_unfulfilled_compliance_condition_sql(", 2000)
    assert "), 0) < {a}.service_days" in unfulfilled, "达标天数未满就不硬停 · 这条比较不见了"
    assert "COALESCE({a}.service_days" not in unfulfilled, "365 兜底不该回来"
    assert "keyword_compliance_log" in helper


def test_list_run_enable_and_api_scope_reuse_same_service_anchor_helper():
    """List, run, API quote scope, and enable guard must not drift again."""
    for marker in (
        "def resolve_service_anchored_quote_ids_for_brand(",
        "def is_quote_service_anchored(",
        "def get_client_keywords(",
        "def get_keywords_for_monitoring(",
        "def list_active_subscriptions(",
    ):
        assert "quote_service_anchor_condition_sql" in _block(MON_DB, marker, 9000), marker

    assert "quote_service_anchor_condition_sql" in _block(MON_API, "def _quote_rows_for_brand(", 3500)
    assert "is_quote_service_anchored" in _block(SERVER, "def _assert_keyword_quote_service_anchored_blocking", 4500)


def test_compliance_summary_is_brand_scope_not_selected_quote_only():
    """Portal merge may request the renewed quote while displayed keywords live on the previous quote."""
    block = _block(MON_DB, "def get_keyword_compliance_summary(", 9000)

    assert "seed_quote" in block
    assert "scoped_quotes" in block
    assert "quote_service_anchor_condition_sql(\"q\")" in block
    assert "JOIN scoped_quotes q ON q.id = kcl.quote_id" in block
    assert "service_days" in block and "tier" in block
    assert "target_rate = _TIER_TARGET_MAP.get" in block


def test_calendar_expiry_job_no_longer_sets_service_status_expired_or_disables_tokens():
    """Calendar due date is a renewal signal, not automatic service completion."""
    block = _block(SCHEDULER, "def _check_service_periods()", 3600)

    assert "SET service_status = 'expired'" not in block
    assert "UPDATE client_access_tokens SET is_active = 0" not in block
    assert "compliant_days" in block or "达标天数" in block
    assert "日历" in block
