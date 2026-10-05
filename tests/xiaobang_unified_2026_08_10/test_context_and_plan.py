from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services import customer_operation_plan as plans
from services import gap_assistant
from services import gap_operation_plan
from services.organization_contract import IdentityContext


def _request(user: dict | None = None):
    return SimpleNamespace(
        state=SimpleNamespace(user=user or {
            "id": 7,
            "agent_level": 1,
            "permissions": ["diagnosis:read", "quote:read", "writing:read", "monitoring:read"],
        }, organization_identity=None),
        method="POST",
    )


@pytest.fixture
def brand_only_reads(monkeypatch):
    monkeypatch.setattr(plans, "require_brand_access", lambda *args, **kwargs: None)
    monkeypatch.setattr(plans, "require_quote_access", lambda request, quote_id, **kwargs: {
        "id": quote_id, "brand_id": 41, "status": "confirmed", "deleted_at": None,
        "updated_at": "2026-08-10T10:30:00", "created_at": "2026-08-01T10:00:00",
    })
    monkeypatch.setattr(plans, "_latest_quote_for_brand", lambda brand_id: {
        "id": 88, "brand_id": brand_id, "status": "confirmed", "deleted_at": None,
        "updated_at": "2026-08-10T10:30:00", "created_at": "2026-08-01T10:00:00",
    })

    def fake_fetch(sql, params):
        if "FROM brands" in sql:
            return {"id": params[0], "name": "本租户品牌", "updated_at": "2026-08-10T10:31:00"}
        return None

    monkeypatch.setattr(plans, "_fetch_row", fake_fetch)
    monkeypatch.setattr(plans, "_load_existing_gap_plan", lambda quote: None)
    monkeypatch.setattr(plans, "_load_capacity_contract", lambda quote: {
        "contract_version": "article-capacity-v1", "authorized_articles": 5,
        "consumed_articles": 2, "available_articles": 3,
    })
    # [WP7 2026-08-17] 签名多了 quote_id(品牌级口径会串同品牌两张报价)。
    # 桩必须跟着改成两参 —— 否则调用方一改就 TypeError,而那不是"发现了缺陷",
    # 是桩过期了。不变式没变,只是搬了家。
    monkeypatch.setattr(plans, "_publication_outcome_summary",
                        lambda brand_id, quote_id=None: {
        "available": True, "articles_cited": 1, "articles_observable": 2,
        "metric_version": "publication-outcome-facts-v1",
    })
    monkeypatch.setattr(plans, "_knowledge_summary", lambda brand_id: {
        "available": True, "load_failed": False, "filled": 4, "total": 4, "missing": [],
    })
    monkeypatch.setattr(plans, "_load_operational_metrics", lambda quote_id: {
        "keywords": {"total": 5, "confirmed": 5, "monitored": 2},
        "writing": {"pending": 2, "in_progress": 0, "completed": 1, "failed": 0},
        "publication": {
            "published": 1, "indexed": 1, "url_cited": 0,
            "brand_mentioned": 1, "recommended": 0,
        },
        "monitoring": {"tasks": 1, "completed_tasks": 1, "observations": 3},
        "updated_at": "2026-08-10T10:32:00",
    })


def test_brand_without_quote_hint_still_builds_authorized_plan(brand_only_reads):
    context = plans.resolve_authorized_context(
        _request(), {"brand_id": 41}, "/monitoring"
    )
    plan = plans.build_customer_operation_plan(context)
    assert context.quote_id == 88
    assert plan["context"]["brand_name"] == "本租户品牌"
    assert plan["context"]["page_name"] == "效果监测"
    assert plan["primary_action"]["operation_id"] == "writing_center"
    assert plan["metrics"]["keywords"]["confirmed"] == 5
    assert plan["capacity"]["available_articles"] == 3
    assert plan["metrics"]["publication"]["url_cited"] == 1


def test_context_connection_mutant_loses_the_quote_and_is_detected(brand_only_reads, monkeypatch):
    monkeypatch.setattr(plans, "_latest_quote_for_brand", lambda brand_id: None)
    disconnected = plans.resolve_authorized_context(_request(), {"brand_id": 41}, "/monitoring")
    assert disconnected.quote_id is None


def test_cross_tenant_hint_fails_closed_without_leaking_name(monkeypatch):
    def deny(*args, **kwargs):
        raise HTTPException(status_code=404, detail="资源不存在")

    monkeypatch.setattr(plans, "require_brand_access", deny)
    with pytest.raises(HTTPException) as caught:
        plans.resolve_authorized_context(
            _request(), {"brand_id": 999}, "/writing"
        )
    assert caught.value.status_code == 404
    assert "别人" not in str(caught.value.detail)
    assert "999" not in str(caught.value.detail)


def test_mixed_question_combines_page_entry_and_customer_advice(brand_only_reads, monkeypatch):
    monkeypatch.setattr(gap_assistant, "_record", lambda *args, **kwargs: None)
    context = plans.resolve_authorized_context(_request(), {"brand_id": 41}, "/monitoring")
    operation_plan = plans.build_customer_operation_plan(context)
    answer = gap_assistant.build_answer(
        question="品牌体检在哪里，今天先做什么",
        context_refs={"brand_id": 41},
        request=_request(),
        assistant_request_id="mixed-1",
        actor_user_id=7,
        current_page="/monitoring",
        identity="agent",
        resolved_context=context,
        operation_plan=operation_plan,
    )
    operation_ids = [action["operation_id"] for action in answer.actions]
    assert operation_ids[0] == "diagnosis_new"
    assert "writing_center" in operation_ids
    assert answer.context["brand_name"] == "本租户品牌"
    assert len(answer.reasons) <= 3


def test_missing_customer_has_executable_selection_exit(monkeypatch):
    monkeypatch.setattr(gap_assistant, "_record", lambda *args, **kwargs: None)
    context = plans.resolve_authorized_context(_request(), None, "/dashboard")
    plan = plans.build_customer_operation_plan(context)
    answer = gap_assistant.build_answer(
        question="今天先做什么",
        context_refs=None,
        request=_request(),
        assistant_request_id="none-1",
        actor_user_id=7,
        current_page="/dashboard",
        identity="agent",
        resolved_context=context,
        operation_plan=plan,
    )
    assert answer.actions[0]["operation_id"] == "client_list"
    assert answer.actions[0]["target_route"] == "/my-clients"


def test_unauthorized_operation_is_replaced_by_an_executable_help_exit():
    answer = gap_assistant.answer_navigation(
        "审计日志在哪里",
        user={"id": 8, "agent_level": 0, "permissions": ["writing:read"]},
        identity="normal_user",
        route_context={},
    )
    assert answer is not None
    assert "没有" in answer.headline
    assert [action["operation_id"] for action in answer.actions] == ["help_center"]
    assert all(action["target_route"] != "/admin/audit" for action in answer.actions)


def test_customer_plan_reads_existing_p4_without_materializing(monkeypatch):
    bundle = {
        "snapshot": {"snapshot_id": "pure-read", "rule_version": gap_operation_plan.RULE_VERSION},
        "items": [],
    }
    monkeypatch.setattr(gap_operation_plan, "read_current_snapshot", lambda quote: bundle)

    def forbidden_materialization(*args, **kwargs):
        raise AssertionError("read-only assistant context must not materialize P4")

    monkeypatch.setattr(gap_operation_plan, "build_snapshot", forbidden_materialization)
    monkeypatch.setattr(gap_operation_plan, "compute_capacity", lambda *args: object())
    monkeypatch.setattr(gap_operation_plan, "present_snapshot", lambda *args, **kwargs: {
        "snapshot_id": "pure-read", "items": [], "capacity": {},
    })
    from db import gap_plan_db
    monkeypatch.setattr(gap_plan_db, "get_publications", lambda quote_id: {})

    loaded = plans._load_existing_gap_plan({"id": 88})
    assert loaded is not None
    assert loaded["snapshot_id"] == "pure-read"


def test_read_current_snapshot_is_pure_and_returns_only_matching_generation(monkeypatch):
    from db import gap_plan_db

    monkeypatch.setattr(gap_operation_plan, "_gather_facts", lambda quote: {"quote": quote["id"]})
    monkeypatch.setattr(gap_operation_plan, "_data_version", lambda facts: "facts-v2")
    monkeypatch.setattr(gap_plan_db, "get_snapshot_by_data_version", lambda quote_id, version: {
        "snapshot_id": "existing-v2",
        "rule_version": gap_operation_plan.RULE_VERSION,
    })
    monkeypatch.setattr(gap_plan_db, "list_items", lambda snapshot_id: [{"plan_item_id": "P-1"}])
    monkeypatch.setattr(
        gap_plan_db, "persist_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("pure read wrote snapshot")),
    )

    bundle = gap_operation_plan.read_current_snapshot({"id": 88})
    assert bundle == {
        "snapshot": {
            "snapshot_id": "existing-v2",
            "rule_version": gap_operation_plan.RULE_VERSION,
        },
        "items": [{"plan_item_id": "P-1"}],
    }


def test_member_p4_actions_are_filtered_by_operation_registry_capability():
    member = IdentityContext(
        request_id="member-p4", authenticated_user_id=81, principal_user_id=7,
        payer_user_id=7, actor_kind="member", organization_id=3,
        capabilities=frozenset({"clients.read_assigned"}),
    )
    snapshot = {
        "summary": {"next_step": "先找发布渠道"},
        "capacity": {},
        "items": [{
            "status": {"code": "hold_until_domain_access_confirmed"},
            "actions": [{
                "action_id": "open_media_library", "label": "去媒体库查渠道",
                "enabled": True,
            }],
            "rationale": {},
        }],
    }
    answer = gap_assistant.answer_operations(
        snapshot,
        user={"id": 81, "agent_level": 0, "permissions": []},
        identity=member,
    )
    assert [action["operation_id"] for action in answer.actions] == ["client_list"]
    assert all(action.get("target_route") != "/publish" for action in answer.actions)

    publish_member = IdentityContext(
        request_id="member-publish", authenticated_user_id=82, principal_user_id=7,
        payer_user_id=7, actor_kind="member", organization_id=3,
        capabilities=frozenset({"clients.read_assigned", "quote.read_own", "publish.plan"}),
    )
    allowed = gap_assistant.answer_operations(
        snapshot,
        user={"id": 82, "agent_level": 0, "permissions": []},
        identity=publish_member,
    )
    assert "media_library" in {action["operation_id"] for action in allowed.actions}
