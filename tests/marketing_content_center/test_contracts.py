import asyncio
import io
import json

import qrcode
from starlette.requests import Request

from api import marketing_material_api
from services.marketing import content_center, geo_factory, quality_assurance, strategy_teachers
from services.organization_contract import BILLABLE_FEATURE_CAPABILITIES
from services.organization_route_contract import match_member_geo_route


def _qr_bytes(value: str) -> bytes:
    image = qrcode.make(value)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def test_all_channel_contracts_are_independent():
    assert set(content_center.CHANNEL_CONTRACTS) == {
        "professional_poster", "moments", "xiaohongshu", "douyin", "private_chat",
        "infographic", "diagnosis_case",
        "deal_poster", "deal_chat", "deal_data_card", "deal_story", "deal_feedback_card",
    }
    assert content_center.CHANNEL_CONTRACTS["moments"]["ratio"] == "1:1"
    assert content_center.CHANNEL_CONTRACTS["douyin"]["ratio"] == "9:16"
    assert len(content_center.CHANNEL_CONTRACTS["xiaohongshu"]["image_slots"]) >= 3
    assert content_center.CHANNEL_CONTRACTS["private_chat"]["image_slots"] == [
        {"slot": "private_chat_scene", "size": "9:16"}
    ]
    assert content_center.CHANNEL_CONTRACTS["infographic"]["image_slots"] == [
        {"slot": "infographic", "size": "3:4"}
    ]
    assert content_center.CHANNEL_CONTRACTS["deal_chat"]["image_slots"] == [
        {"slot": "deal_chat_scene", "size": "9:16"}
    ]
    for channel in ("deal_poster", "deal_data_card", "deal_feedback_card"):
        assert content_center.CHANNEL_CONTRACTS[channel]["ratio"] == "3:4"
    assert content_center.CHANNEL_CONTRACTS["deal_story"]["ratio"] == "9:16"


def test_teacher_registry_is_versioned_and_replaceable(monkeypatch):
    monkeypatch.setattr(strategy_teachers.marketing_db, "get_policy", lambda _key: None)
    teachers = strategy_teachers.list_teachers()
    assert len(teachers) >= 2
    assert {"teacher_id", "name", "version", "system_method", "capabilities", "channel_boundaries", "enabled"}.issubset(teachers[0])
    default = strategy_teachers.resolve_teacher(principal_user_id=9)
    assert default["teacher_id"] == "shu"
    other = strategy_teachers.resolve_teacher(
        teacher_id="b2b_sales_coach", version="1.0.0", principal_user_id=9,
    )
    assert other["teacher_id"] != default["teacher_id"]


def test_unverified_recent_trend_is_evergreen():
    decision = content_center.decide_trend(requested=True, verified_trend={"verified": True})
    assert decision["requested"] is True
    assert decision["used"] is False
    assert decision["source"] is None
    assert decision["reason"] == "verified_trend_unavailable"


def test_contact_none_scrubs_all_supplied_values():
    result = content_center.normalize_contact(
        "none", text="微信 abc123", qr_reference={"reference_id": "bad.png", "payload_hash": "bad"},
    )
    assert result == {"mode": "none", "text": "", "qr_reference": None}


def test_missing_published_evidence_is_a_stable_actionable_422(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from auth import brand_access
    from services.marketing import evidence

    monkeypatch.setattr(marketing_material_api, "_require_user", lambda _request: {"id": 1})
    monkeypatch.setattr(marketing_material_api, "_uid", lambda _user: 1)
    monkeypatch.setattr(marketing_material_api, "_principal_user_id", lambda _request, _user: 1)
    monkeypatch.setattr(brand_access, "require_brand_access", lambda _request, _brand_id: None)
    monkeypatch.setattr(
        strategy_teachers,
        "resolve_teacher",
        lambda **_kwargs: {"teacher_id": "shu", "version": "1.0.0", "name": "舒老师"},
    )
    monkeypatch.setattr(marketing_material_api, "_clean_strategy", lambda value, _teacher, _brief: value)

    def reject_missing(**_kwargs):
        raise ValueError("published_diagnosis_evidence_not_found")

    monkeypatch.setattr(evidence, "freeze_evidence", reject_missing)
    app = FastAPI()
    app.include_router(marketing_material_api.router)
    response = TestClient(app).post("/api/marketing/content-packages", json={
        "request_id": "evidence-contract-001",
        "brief": "向服务商老板推广真实 GEO 诊断",
        "quick_task": "promote_geo",
        "strategy": {"audience": "服务商老板"},
        "teacher_id": "shu",
        "teacher_version": "1.0.0",
        "channels": ["professional_poster"],
        "brand_id": 101,
        "evidence": {"source_type": "latest_diagnosis", "diagnosis_id": None},
        "contact": {"mode": "none", "text": "", "qr_reference": None},
        "associate_recent_trend": False,
        "resolution": "1k",
    })
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "PUBLISHED_DIAGNOSIS_EVIDENCE_NOT_FOUND"
    assert "已发布" in detail["message"]
    assert {item["id"] for item in detail["actions"]} == {
        "select_published_diagnosis", "use_evergreen",
    }
    assert "published_diagnosis_evidence_not_found" not in json.dumps(detail, ensure_ascii=False)


def test_public_job_assets_hide_provider_and_storage_internals():
    state = geo_factory.job_public_state(
        {"id": 11, "status": "succeeded", "input_fields_jsonb": {"_geo": {"channels": []}}},
        [{
            "id": 7, "asset_kind": "poster", "bundle_slot": "professional_poster:image:professional_poster",
            "url_stored": "/uploads/material.png", "url_provider": "https://provider.example/private",
            "provider_request_id": "secret-request-id", "content_text": None,
        }],
    )
    assert state["assets"][0]["download_url"] == "/api/marketing/materials/7/download"
    assert "url_stored" not in state["assets"][0]
    assert "url_provider" not in state["assets"][0]
    assert "provider_request_id" not in state["assets"][0]


def test_in_progress_job_with_early_assets_remains_generating():
    state = geo_factory.job_public_state(
        {
            "id": 12,
            "status": "generating",
            "input_fields_jsonb": {"_geo": {"channels": ["professional_poster"]}},
        },
        [{
            "id": 8,
            "bundle_slot": "professional_poster:image:professional_poster",
            "url_stored": "private://marketing-materials/u12/early.png",
        }],
    )
    assert state["status"] == "generating"
    assert state["assets"] == []
    assert "download_url" not in json.dumps(state, ensure_ascii=False)
    assert any(component["status"] == "succeeded" for component in state["components"])
    assert any(component["status"] == "pending" for component in state["components"])


def test_public_job_contact_hides_qr_storage_reference_and_hash():
    state = geo_factory.job_public_state(
        {
            "id": 13,
            "status": "succeeded",
            "input_fields_jsonb": {"_geo": {
                "channels": [],
                "contact": {
                    "mode": "qr",
                    "text": "",
                    "qr_reference": {"reference_id": "opaque-qr.png", "payload_hash": "a" * 64},
                },
            }},
        },
        [],
    )
    assert state["contact"] == {"mode": "qr", "text": "", "qr_validated": True}


def test_polling_a_committed_generating_job_schedules_crash_recovery(monkeypatch):
    job = {
        "id": 501,
        "user_id": 9,
        "status": "generating",
        "freeze_id": 77,
        "resolution": "1k",
        "input_fields_jsonb": {"_geo": {
            "request_id": "poll-recovery-501",
            "request_hash": "a" * 64,
            "channels": [],
            "evidence": {"source_type": "none"},
        }},
    }
    monkeypatch.setattr(marketing_material_api.marketing_db, "get_job", lambda _job_id: job)
    monkeypatch.setattr(geo_factory.marketing_db, "list_assets", lambda _job_id: [])

    async def prepare(**_kwargs):
        return {"status": "generating", "job_id": 501, "_ctx": {"job_id": 501}}

    async def execute(_ctx):
        return {"status": "generating"}

    scheduled = []

    def capture_task(coroutine):
        scheduled.append(coroutine)
        coroutine.close()

    monkeypatch.setattr(geo_factory, "prepare_geo_package_job", prepare)
    monkeypatch.setattr(geo_factory, "execute_geo_package_job", execute)
    monkeypatch.setattr(marketing_material_api.asyncio, "create_task", capture_task)
    request = Request({"type": "http", "method": "GET", "path": "/api/marketing/jobs/501", "headers": []})
    request.state.user = {"id": 9}
    response = asyncio.run(marketing_material_api.api_job(501, request))
    assert response["job"]["status"] == "generating"
    assert len(scheduled) == 1


def test_claim_qa_contact_and_numbers_without_evidence_are_warnings_not_blocks():
    """Owner 2026-07-22 分层:无证据数字/联系方式提醒 → warnings,不阻断。"""
    qa = content_center.claim_evidence_qa(
        {"title": "排名第1，联系我 13800138000"},
        evidence={"facts": []}, contact={"mode": "none", "text": ""}, channel="moments",
    )
    assert qa["passed"] is True  # warnings 不阻断
    assert qa["errors"] == []
    codes = {warning["code"] for warning in qa["warnings"]}
    assert "contact_forbidden_when_none" in codes
    assert "number_without_evidence" in codes
    assert all(warning["message"] for warning in qa["warnings"])  # 每条带人话 message


def test_claim_qa_ad_law_words_are_the_only_hard_error():
    """法律红线:广告法违禁极限词 → errors 硬拦;承诺词/保证词 → warnings 放行。"""
    hard = content_center.claim_evidence_qa(
        {"title": "全网第一品牌，最强 GEO 服务"},
        evidence={"facts": []}, contact={"mode": "none", "text": ""}, channel="moments",
    )
    assert hard["passed"] is False
    assert [error["code"] for error in hard["errors"]] == ["forbidden_claim"]
    assert "第一品牌" in str(hard["errors"][0]["detail"])
    assert hard["errors"][0]["message"]  # 人话原因

    soft = content_center.claim_evidence_qa(
        {"title": "我们保证帮你把内容做好"},
        evidence={"facts": []}, contact={"mode": "none", "text": ""}, channel="moments",
    )
    assert soft["passed"] is True  # 承诺词提醒不阻断
    assert soft["errors"] == []
    assert [warning["code"] for warning in soft["warnings"]] == ["forbidden_claim"]
    assert "保证" in str(soft["warnings"][0]["detail"])


def test_claim_qa_allows_only_the_explicit_text_contact_number():
    qa = content_center.claim_evidence_qa(
        {"title": "先看真实诊断", "cta": "预约体验：13800138000"},
        evidence={"facts": []},
        contact={"mode": "text", "text": "13800138000"},
        channel="professional_poster",
    )
    assert qa["passed"] is True


def test_no_promise_boundary_language_is_allowed_but_positive_claims_fail():
    assert content_center.scan_geo_claims("不愿意承诺虚假结果，只展示真实证据")["passed"] is True
    assert content_center.scan_geo_claims("承诺排名第一")["passed"] is False


def test_diagnosis_case_without_evidence_generates_evergreen_with_warning():
    """Owner 2026-07-22:案例卡无冻结证据不再 ValueError;按常青口径生成 + warning。"""
    teacher = strategy_teachers.list_teachers()[0]
    strategy = {
        "audience": "服务商", "audience_status": "客户在问 AI", "action_resistance": "担心无证据",
        "human_problem": "答案里有没有品牌", "core_angle": "先看诊断", "single_value": "证据链",
        "evidence_statement": "无数据就明确写无数据", "single_action": "领取诊断",
    }
    content, qa = asyncio.run(content_center.generate_channel_content(
        "diagnosis_case", strategy=strategy, evidence={"facts": []},
        trend={"used": False}, contact={"mode": "none", "text": ""}, teacher=teacher,
    ))
    assert content["title"]  # 照常产出,不阻断
    assert qa["passed"] is True
    warnings = {warning["code"]: warning for warning in qa["warnings"]}
    assert "diagnosis_case_requires_frozen_evidence" in warnings
    assert "建议先发布诊断报告" in warnings["diagnosis_case_requires_frozen_evidence"]["message"]


def test_diagnosis_case_rejects_internal_source_id_but_allows_fact_numbers():
    strategy = {
        "audience": "服务商", "audience_status": "客户在问 AI", "action_resistance": "担心无证据",
        "human_problem": "答案里有没有品牌", "core_angle": "先看诊断", "single_value": "证据链",
        "evidence_statement": "引用已冻结诊断", "single_action": "领取诊断",
    }
    frozen_evidence = {
        "source_id": 123,
        "source_note": "依据本次已发布诊断结果，生成时已冻结",
        "facts": [{"key": "geo_score", "label": "GEO 诊断评分", "value": 71}],
    }
    content = content_center.fallback_channel_content(
        "diagnosis_case", strategy, frozen_evidence, {"mode": "none", "text": ""},
    )
    qa = content_center.claim_evidence_qa(
        content, evidence=frozen_evidence, contact={"mode": "none", "text": ""}, channel="diagnosis_case",
    )
    assert content["source_note"] == "依据本次已发布诊断结果，生成时已冻结"
    assert qa["passed"] is True
    assert "123" not in str(content)


def test_qr_reference_and_generated_qr_must_match_exact_payload(monkeypatch):
    source = _qr_bytes("https://omnirank.example/lead/abc")
    other = _qr_bytes("https://omnirank.example/lead/other")
    validated = quality_assurance.validate_qr_reference(source)
    assert quality_assurance.verify_generated_qr(source, validated["payload_hash"])["passed"] is True
    assert quality_assurance.verify_generated_qr(other, validated["payload_hash"])["passed"] is False


def test_qr_reference_token_is_tenant_bound(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-only-secret")
    token = quality_assurance.sign_qr_reference(
        user_id=1, reference_id="opaque-qr.png", payload_hash="a" * 64,
    )
    assert quality_assurance.verify_qr_reference_token(
        user_id=1, reference_id="opaque-qr.png", payload_hash="a" * 64,
        reference_token=token,
    )
    assert not quality_assurance.verify_qr_reference_token(
        user_id=2, reference_id="opaque-qr.png", payload_hash="a" * 64,
        reference_token=token,
    )


def test_employee_routes_and_billing_features_are_classified():
    create = match_member_geo_route("POST", "/api/marketing/content-packages")
    retry = match_member_geo_route("POST", "/api/marketing/jobs/42/retry")
    read = match_member_geo_route("GET", "/api/marketing/jobs/42")
    recover = match_member_geo_route("GET", "/api/marketing/content-packages/by-request/client-request-1")
    download = match_member_geo_route("GET", "/api/marketing/materials/42/download")
    reconcile = match_member_geo_route("POST", "/api/marketing/jobs/42/attempts/7/reconcile-provider")
    assert create and create.capability == "materials.write"
    assert retry and retry.capability == "materials.write"
    assert read and read.capability == "materials.read_assigned"
    assert recover and recover.capability == "materials.read_assigned"
    assert download and download.capability == "materials.read_assigned"
    assert download.resource_kind == "marketing_material" and download.operation == "detail"
    assert reconcile is None
    for code in ("mktg_moments_copy", "mktg_poster_basic", "mktg_poster_pro", "mktg_bundle_std", "mktg_bundle_pro"):
        assert BILLABLE_FEATURE_CAPABILITIES[code] == "materials.write"


def test_visual_prompt_freezes_all_required_director_inputs():
    prompt = content_center.visual_prompt(
        slot={"slot": "douyin_cover", "size": "9:16"}, channel="douyin",
        strategy={"audience": "装修服务商", "single_action": "领取诊断", "single_value": "看见证据"},
        content={"title": "客户问 AI 时，有没有你？", "script": "先看真实回答"},
        evidence={"facts": [], "source_note": "未引用诊断数字"},
        trend={"used": False}, contact={"mode": "none", "text": "", "qr_reference": None},
        brand={"name": "OmniRank", "brand_color": "#7ddc45"},
    )
    for value in ("gpt-image-2", "9:16", "装修服务商", "领取诊断", "OmniRank", "联系方式", "二维码"):
        assert value in prompt
    assert "近期热点" in prompt and "evergreen" in prompt


# ---------------------------------------------------------------------------
# Owner 2026-07-22 合规口径:API 层分层契约
# ---------------------------------------------------------------------------
def _stub_package_api(monkeypatch):
    from auth import brand_access
    from services.marketing import strategy_teachers as _teachers

    monkeypatch.setattr(marketing_material_api, "_require_user", lambda _request: {"id": 1})
    monkeypatch.setattr(marketing_material_api, "_uid", lambda _user: 1)
    monkeypatch.setattr(marketing_material_api, "_principal_user_id", lambda _request, _user: 1)
    monkeypatch.setattr(brand_access, "require_brand_access", lambda _request, _brand_id: None)
    monkeypatch.setattr(
        _teachers,
        "resolve_teacher",
        lambda **_kwargs: {"teacher_id": "shu", "version": "1.0.0", "name": "舒老师"},
    )
    monkeypatch.setattr(marketing_material_api, "_freeze_service_brand", lambda _p: {"name": "OmniRank"})
    captured = {}

    async def prepare(**kwargs):
        captured["geo_snapshot"] = kwargs["geo_snapshot"]
        return {"status": "generating", "job_id": 1}

    monkeypatch.setattr(geo_factory, "prepare_geo_package_job", prepare)
    return captured


def _strategy_payload(**overrides) -> dict:
    payload = {
        "request_id": "posture-contract-001",
        "brief": "向服务商老板推广真实 GEO 诊断",
        "quick_task": "promote_geo",
        "strategy": {
            "audience": "服务商老板", "audience_status": "客户在问 AI", "action_resistance": "担心没证据",
            "human_problem": "答案里有没有品牌", "core_angle": "先看诊断", "single_value": "证据链",
            "evidence_statement": "只用冻结证据", "single_action": "领取诊断",
        },
        "channels": ["moments"],
        "evidence": {"source_type": "none"},
        "contact": {"mode": "none"},
    }
    payload.update(overrides)
    return payload


def test_strategy_ad_law_words_still_422_but_promise_words_pass_with_warning(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _stub_package_api(monkeypatch)
    app = FastAPI()
    app.include_router(marketing_material_api.router)
    client = TestClient(app)

    # 广告法违禁极限词:唯一 422 的法律红线
    ad_law = dict(_strategy_payload()["strategy"], single_value="全网第一品牌的首选方案")
    blocked = client.post("/api/marketing/content-packages", json=_strategy_payload(
        request_id="posture-adlaw-001", strategy=ad_law,
    ))
    assert blocked.status_code == 422
    assert blocked.json()["detail"] == "strategy_contains_forbidden_claim"

    # 承诺词/保证词:放行 + warning 透传(提醒不阻断)
    promise = dict(_strategy_payload()["strategy"], single_value="我们保证把内容做好")
    captured = {}
    monkeypatch.setattr(geo_factory, "prepare_geo_package_job", _async_capture(captured))
    passed = client.post("/api/marketing/content-packages", json=_strategy_payload(
        request_id="posture-promise-001", strategy=promise,
    ))
    assert passed.status_code == 200, passed.text
    warnings = captured["geo_snapshot"]["warnings"]
    assert [warning["code"] for warning in warnings] == ["strategy_promise_claim"]
    assert warnings[0]["message"]  # 人话 message


def _async_capture(captured: dict):
    async def prepare(**kwargs):
        captured["geo_snapshot"] = kwargs["geo_snapshot"]
        return {"status": "generating", "job_id": 1}

    return prepare


def test_diagnosis_case_channel_without_evidence_passes_with_warning(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    captured = _stub_package_api(monkeypatch)
    app = FastAPI()
    app.include_router(marketing_material_api.router)
    client = TestClient(app)
    response = client.post("/api/marketing/content-packages", json=_strategy_payload(
        request_id="posture-diagcase-001", channels=["diagnosis_case"],
    ))
    assert response.status_code == 200, response.text  # 不再 422 硬拦
    warnings = captured["geo_snapshot"]["warnings"]
    assert [warning["code"] for warning in warnings] == ["diagnosis_case_requires_frozen_evidence"]
    assert "建议先发布诊断报告" in warnings[0]["message"]


def test_job_public_state_exposes_warnings_with_human_messages():
    state = geo_factory.job_public_state(
        {
            "id": 21, "status": "succeeded",
            "input_fields_jsonb": {"_geo": {
                "channels": ["moments"],
                "warnings": [{
                    "code": "number_without_evidence",
                    "message": "文案里的数字没有对应证据来源，发布前请核对",
                    "channel": "moments", "component_id": "moments:copy",
                }],
            }},
        },
        [],
    )
    assert state["warnings"] == [{
        "code": "number_without_evidence",
        "message": "文案里的数字没有对应证据来源，发布前请核对",
        # SSOT 2026-07-23 五问合同:透出五字段(存量 warning 缺省为空)
        "reason": "", "repair_hint": "", "actions": [], "rule_version": "",
        "channel": "moments", "component_id": "moments:copy",
    }]
