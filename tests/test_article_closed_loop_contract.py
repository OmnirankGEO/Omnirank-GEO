from __future__ import annotations

from datetime import datetime, timezone
import os

import pytest

from services.article_closed_loop_contract import (
    CANARY_THRESHOLD_POLICY_VERSION,
    EVENT_KINDS,
    EVENT_SOURCES,
    FEATURE_FLAG_DEFAULTS,
    REMOVED_EVENT_KINDS,
    SIGNED_RELEASE_BASE_SHA,
    build_event_key,
    build_source_version,
    canonical_json,
    feature_flag_blockers,
)


def _standard_snapshot() -> dict:
    return {
        "session_id": 9,
        "quote_id": 42,
        "owner_user_id": 7,
        "brand_id": 8,
        "session_status": "active",
        "quote_status": "confirmed",
        "service_status": "active",
        "confirmed_keywords": [
            {"confirmed_keyword_id": 11, "keyword": "细胞治疗", "required_articles": 2},
            {"confirmed_keyword_id": 12, "keyword": "隔离器", "required_articles": 1},
        ],
    }


def test_signed_base_and_exact_production_event_set_are_frozen():
    assert SIGNED_RELEASE_BASE_SHA == "95b1f3b2eef4fc2bc37dc1035c6a56b7b0ec33e1"
    assert EVENT_KINDS == {
        "quote_paid_standard",
        "quote_paid_offline",
        "quote_paid_agent_activation",
        "zero_price_writing_project_created",
        "contract_add_on",
        "keyword_reassigned",
        "publication_locked",
    }
    assert REMOVED_EVENT_KINDS == {
        "quote_refund_terminal",
        "quote_cancel_terminal",
        "bonus_article_changed",
    }
    assert not EVENT_KINDS & REMOVED_EVENT_KINDS
    assert EVENT_SOURCES["quote_paid_standard"].authority_predicate.startswith("session.status='active'")
    assert "quote.status='confirmed'" in EVENT_SOURCES["quote_paid_standard"].authority_predicate


def test_source_version_is_stable_and_changes_only_with_whitelisted_authority_snapshot():
    snapshot = _standard_snapshot()
    version_1, hash_1 = build_source_version("quote_paid_standard", snapshot)
    replay = dict(snapshot)
    replay["worker_attempt"] = 99
    replay["observed_at"] = datetime.now(timezone.utc)
    version_2, hash_2 = build_source_version("quote_paid_standard", replay)
    assert (version_1, hash_1) == (version_2, hash_2)

    changed = _standard_snapshot()
    changed["confirmed_keywords"] = [*changed["confirmed_keywords"], {"confirmed_keyword_id": 13, "keyword": "生产", "required_articles": 1}]
    version_3, hash_3 = build_source_version("quote_paid_standard", changed)
    assert (version_3, hash_3) != (version_1, hash_1)


def test_removed_or_incomplete_event_source_fails_closed():
    with pytest.raises(ValueError, match="removed event kind"):
        build_source_version("quote_refund_terminal", {})
    snapshot = _standard_snapshot()
    del snapshot["service_status"]
    with pytest.raises(ValueError, match="service_status"):
        build_source_version("quote_paid_standard", snapshot)


def test_event_key_uses_immutable_source_identity_and_version():
    version, _ = build_source_version("quote_paid_standard", _standard_snapshot())
    key_1 = build_event_key("quote_paid_standard", {"session_id": 9, "quote_id": 42}, version)
    key_2 = build_event_key("quote_paid_standard", {"quote_id": 42, "session_id": 9}, version)
    assert key_1 == key_2
    assert len(key_1) == 64
    with pytest.raises(ValueError, match="session_id"):
        build_event_key("quote_paid_standard", {"quote_id": 42}, version)


def test_canonical_json_normalizes_unicode_time_and_numbers():
    left = {"text": "e\u0301", "value": 1.0, "time": datetime(2026, 7, 20, tzinfo=timezone.utc)}
    right = {"time": datetime(2026, 7, 20, tzinfo=timezone.utc), "value": 1, "text": "é"}
    assert canonical_json(left) == canonical_json(right)


def test_all_new_flags_default_false(monkeypatch):
    for name in FEATURE_FLAG_DEFAULTS:
        monkeypatch.delenv(name, raising=False)
    assert FEATURE_FLAG_DEFAULTS and all(value is False for value in FEATURE_FLAG_DEFAULTS.values())


def test_invalid_flag_graph_and_unsigned_canary_fail_closed(monkeypatch):
    flags = dict(FEATURE_FLAG_DEFAULTS)
    flags["ARTICLE_PLAN_SHADOW_ENABLED"] = True
    blockers = feature_flag_blockers(flags, schema_ready=True)
    assert any(item.startswith("missing_flag_dependency:ARTICLE_PLAN_SHADOW_ENABLED") for item in blockers)

    flags = {name: True for name in FEATURE_FLAG_DEFAULTS}
    monkeypatch.setenv("ARTICLE_PLAN_CANARY_ALLOWLIST", "quote:42")
    blockers = feature_flag_blockers(flags, schema_ready=True, compiler_policy_signed=False)
    assert CANARY_THRESHOLD_POLICY_VERSION == "UNSIGNED"
    assert "canary_threshold_policy_unsigned" in blockers
    assert "canary_new_quote_epoch_unsigned" in blockers
    assert "flywheel_promotion_policy_unsigned" in blockers


def test_sidecar_write_flags_require_schema_readiness():
    flags = dict(FEATURE_FLAG_DEFAULTS)
    flags["ARTICLE_PLAN_EVENT_OUTBOX_ENABLED"] = True
    assert "closed_loop_schema_not_ready" in feature_flag_blockers(flags, schema_ready=False)

    read_flags = dict(FEATURE_FLAG_DEFAULTS)
    read_flags["ARTICLE_PLAN_READ_SUMMARY_ENABLED"] = True
    assert "closed_loop_schema_not_ready" in feature_flag_blockers(read_flags, schema_ready=False)


def test_server_startup_enforces_the_flag_dependency_contract():
    # [守卫单一来源 2026-07-30] 守卫实现自 server.py 迁到 services/startup_schema_guards.py;
    # server.py(web/cron)与 scripts/prestart.py 现在共用 run_fleet_schema_guards() 一份清单。
    # 本锁守的性质不变(启动期强制 flag 依赖契约),外加两条:该守卫必须在共用清单里、
    # 且启动路径必须真调那份清单 —— 否则实现在但没人跑 = 换皮假绿。
    source = open("services/startup_schema_guards.py", encoding="utf-8").read()
    start = source.index("def verify_article_closed_loop_schema_fail_closed")
    end = source.index("# ========== 单一来源清单", start)
    block = source[start:end]
    assert "feature_flag_blockers(" in block
    assert 'CANARY_THRESHOLD_POLICY_VERSION != "UNSIGNED"' in block
    assert "article_closed_loop_flag_contract_blocked" in block

    from services.startup_schema_guards import FLEET_SCHEMA_GUARDS

    assert ("article_closed_loop", "verify_article_closed_loop_schema_fail_closed") in FLEET_SCHEMA_GUARDS
    server_source = open("server.py", encoding="utf-8").read()
    assert "run_fleet_schema_guards(log=logger)" in server_source
    assert "run_fleet_schema_guards(log=logger, prefix=" in open(
        "scripts/prestart.py", encoding="utf-8"
    ).read()


def test_dispatch_calls_review_shadow_without_turning_it_into_a_hard_gate(monkeypatch):
    monkeypatch.setenv("GEO_ARTICLE_PUBLICATION_REVIEW_GATE_ENABLED", "false")
    import services.article_publish_dispatch as dispatch
    import services.article_review_shadow as shadow
    from services.article_publish_dispatch import prepare_article_dispatch_snapshot

    calls = []
    monkeypatch.setattr(dispatch, "_load_contact_consent_context", lambda article_id: (None, None))
    monkeypatch.setattr(
        shadow,
        "record_dispatch_review_shadow",
        lambda **kwargs: calls.append(kwargs) or {"recorded": False, "reason": "simulated_failure"},
    )
    snapshot = prepare_article_dispatch_snapshot(
        article_id=42,
        source_title="隔离器选型方法指南",
        source_content="本文介绍隔离器选型时需要核对的材料和步骤。",
        outgoing_title="隔离器选型方法指南",
        outgoing_content="本文介绍隔离器选型时需要核对的材料和步骤。",
        source="scheduler_retry",
    )
    assert snapshot.review_reason == "publication_review_gate_disabled"
    assert calls == [
        {
            "article_id": 42,
            "dispatch_source": "scheduler_retry",
            "outgoing_content": "本文介绍隔离器选型时需要核对的材料和步骤。",
        }
    ]
