"""Monitoring subscription enable must use the service-anchor SSOT.

2026-06-23 regression:
The monitoring page already lists service-anchored customers, and the daily
subscription runner already accepts the same service-anchor policy. The enable
endpoints must not keep the older paid-only gate, otherwise admins can see
eligible keywords but cannot turn on monitoring.
"""
from __future__ import annotations

import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _read(rel_path: str) -> str:
    with open(os.path.join(ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _block(src: str, marker: str, width: int = 5000) -> str:
    start = src.find(marker)
    assert start >= 0, f"missing marker: {marker}"
    return src[start : start + width]


def test_enable_endpoints_call_service_anchor_guard_not_paid_only_guard():
    src = _read("server.py")
    single = _block(src, '@app.post("/api/monitoring/keyword/{keyword_id}/enable")')
    batch = _block(src, '@app.post("/api/monitoring/keyword/batch-enable")', 7000)

    assert "def _assert_keyword_quote_service_anchored_blocking" in src
    assert "_assert_keyword_quote_service_anchored_blocking" in single
    assert "_assert_keyword_quote_service_anchored_blocking" in batch
    assert "_assert_keyword_quote_paid_blocking" not in single
    assert "_assert_keyword_quote_paid_blocking" not in batch
    assert "service_anchor_blocked" in batch


def test_service_anchor_guard_accepts_paid_or_confirmed_with_anchor():
    src = _read("server.py")
    guard = _block(src, "def _assert_keyword_quote_service_anchored_blocking", 4200)

    assert "SELECT status, paid_at, service_start_date, service_status" in guard
    assert "is_quote_service_anchored" in guard
    assert "status == \"paid\"" in guard or "status == 'paid'" in guard
    assert "service_start_date" in guard
    assert "paid_at" in guard
    assert "cancelled" in guard and "inactive" in guard
    assert "累计达标完成" in guard
    assert "服务尚未开始" in guard or "未进入服务期" in guard


def test_daily_subscription_runner_already_uses_same_service_anchor_policy():
    src = _read("db/monitoring_db.py")
    runner = _block(src, "def list_active_subscriptions()", 6500)

    assert "quote_service_anchor_condition_sql" in runner
    assert "cancelled', 'expired', 'inactive'" not in runner
    assert "compliant_days < service_days" in runner
