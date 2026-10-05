"""WP8 · §13 rollout · reusable domain alert builders.

Every builder in ``services.governance_alerts`` must produce a §13-compliant
machine contract (7 fields incl. impact + >=1 legal next-step action). These are
the exits that convert bare ``str(e)`` / partial ``{code,message}`` dead-ends
(accident #8) into governed alerts. The discriminant here is uniform: for every
builder, ``is_alert_contract(payload)`` is True AND it carries a stable code plus
at least one action — a dead-end can never survive construction.
"""
import inspect

import pytest

from services.governance_contract import is_alert_contract
from services import governance_alerts as ga


# Every public builder we expect this module to expose, with the args needed to
# invoke it and the stable code it must emit. Adding a rollout builder without a
# row here (or vice-versa) fails the coverage guard below.
BUILDERS = {
    "diagnosis_failed_alert": (("some traceback text",), {}, "DIAGNOSIS_RUN_FAILED"),
    "autofill_all_providers_failed_alert": (("TimeoutError: x",), {}, "AUTOFILL_ALL_PROVIDERS_FAILED"),
    "monitoring_run_failed_alert": (("boom",), {}, "MONITORING_RUN_FAILED"),
    "monitoring_stream_failed_alert": (("boom",), {}, "MONITORING_STREAM_FAILED"),
    "monitoring_no_monitorable_keywords_alert": ((), {}, "no_monitorable_keywords"),
    "monitoring_platform_not_entitled_alert": ((), {}, "requested_platform_not_entitled"),
    "monitoring_no_eligible_platforms_alert": ((), {}, "no_eligible_monitoring_platforms"),
    "monitoring_insufficient_points_alert": ((), {"required": 260, "available": 10}, "INSUFFICIENT_POINTS"),
    "monitoring_billing_error_alert": (("db down",), {}, "MONITORING_BILLING_ERROR"),
    "publish_insufficient_paid_points_alert": ((), {"required": 500, "available": 100}, "INSUFFICIENT_PAID_POINTS"),
    "publish_needs_manual_review_alert": ((), {"submitted_count": 3}, "NEED_MANUAL_REVIEW"),
    "content_stream_failed_alert": (("write",), {}, "CONTENT_GENERATION_FAILED"),
    "advisor_chat_failed_alert": ((), {}, "ADVISOR_CHAT_FAILED"),
    "advisor_not_found_alert": ((), {}, "ADVISOR_NOT_AVAILABLE"),
    "team_operation_failed_alert": (("diagnose",), {}, "TEAM_OPERATION_FAILED"),
    # [WP9-P0-7 ② · D8] 文章 finding 定位标注 + "AI 修复此处/忽略";段级修复失败的诚实出口
    "article_finding_alert": (
        ({"code": "absolute_first_claim", "severity": "hard",
          "message": "含绝对化用语", "matched_text": "行业第一"},),
        {}, "ABSOLUTE_FIRST_CLAIM",
    ),
    "span_repair_failed_alert": (("still_violating",), {}, "SPAN_REPAIR_FAILED"),
}


@pytest.mark.parametrize("name", list(BUILDERS))
def test_every_builder_is_a_contract_with_action(name):
    args, kwargs, code = BUILDERS[name]
    payload = getattr(ga, name)(*args, **kwargs)
    assert is_alert_contract(payload), f"{name} is not a §13 contract"
    assert payload["code"] == code, f"{name} code drifted"
    assert len(payload["actions"]) >= 1, f"{name} has no next-step action"
    # every action must be usable UI: id + label
    for a in payload["actions"]:
        assert a.get("id") and a.get("label")
    # why + what-it-affected must be present and non-empty (accident #8 guard)
    assert payload["reason"].strip()
    assert payload["impact"].strip()


def test_no_supplier_names_leak_in_autofill_alert():
    # feedback_no_supplier_names_to_users: raw provider names / exception class
    # names must never reach the user-facing reason/message/impact/repair_hint.
    payload = ga.autofill_all_providers_failed_alert(
        "TimeoutError: doubao dashscope kimi all failed"
    )
    surface = " ".join(
        str(payload.get(k) or "") for k in ("message", "reason", "impact", "repair_hint")
    ).lower()
    for banned in ("doubao", "dashscope", "kimi", "timeouterror", "traceback"):
        assert banned not in surface, f"leaked '{banned}' to user-facing alert"


def test_resource_alerts_preserve_numeric_fields():
    p = ga.publish_insufficient_paid_points_alert(required=500, available=100)
    assert p["required"] == 500 and p["available_paid"] == 100
    m = ga.monitoring_insufficient_points_alert(required=260, available=10)
    assert m["required"] == 260 and m["available"] == 10


def test_coverage_guard_every_public_builder_is_locked():
    # Any new *_alert builder added to the module must be pinned in BUILDERS,
    # so the contract discriminant above runs against it (no silent additions).
    public = {
        n for n, obj in inspect.getmembers(ga, inspect.isfunction)
        if n.endswith("_alert") and obj.__module__ == ga.__name__
    }
    assert public == set(BUILDERS), (
        f"builders not pinned: {public ^ set(BUILDERS)}"
    )
