"""2026-07-23 外部独立审查修复回归(5 P1 + 4 P2 + 路由合同补充)。

- P1-1 组织撤权后 draft/QR 仍可访问 → 草稿组织绑定 + live 复核 + QR 组织上下文
- P1-2 「用户确认继续」无真实确认动作 → 显式确认标记 + 双事件口径
- P1-3 丢响应恢复重跑文案 LLM → 恢复/重放重用已持久化 copy 资产
- P1-4 匿名打码漏 ASCII 人名/英文头衔 → redaction/mask 同口径扩模式
- P1-5 未签发法律目录硬拦 + 子串误判 → fail-open advisory + 否定前缀放行
- P2-1 AI 候选被法律红线拦截时静默回落 → 丢弃必须落 warning 透出
- P2-2 douyin 已是晒成交可选渠道(回归断言)
- P2-3 局部重试沿用旧事实快照 → sheet 更新后重取 facts/strategy
- 补充 /api/marketing/product-facts 组织只读路由合同

DB-free mock 风格(同 test_deal_showcase.py):不连真实 PG、不打真实 LLM。
"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import db.connection
from api import marketing_material_api
from db import marketing_db
from services import organization_route_contract
from services.marketing import content_center, deal_intake, geo_factory, guards, redaction
import tools.multi_llm_caller


# ---------------------------------------------------------------------------
# 共用桩(与 test_deal_showcase.py 同风格)
# ---------------------------------------------------------------------------
def _stub_user(monkeypatch, user_id: int = 9):
    monkeypatch.setattr(marketing_material_api, "_require_user", lambda _request: {"id": user_id})
    monkeypatch.setattr(marketing_material_api, "_uid", lambda _user: user_id)
    monkeypatch.setattr(marketing_material_api, "_principal_user_id", lambda _request, _user: user_id)


class _SeqCursor:
    def __init__(self, rows):
        self._rows = list(rows)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


class _SeqConn:
    def __init__(self, rows):
        self._cursor = _SeqCursor(rows)
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


def _fake_request(identity=None, user_id: int = 9):
    return SimpleNamespace(
        state=SimpleNamespace(
            user={"id": user_id},
            organization_identity=identity,
            organization_request_id="test-request",
        )
    )


def _identity(*, organization_id=7, membership_id=55, membership_status="active",
              organization_status="active", actor_kind="member"):
    from services.organization_contract import IdentityContext

    return IdentityContext(
        request_id="test-request", authenticated_user_id=9, principal_user_id=42,
        payer_user_id=42, actor_kind=actor_kind,
        organization_id=organization_id, organization_status=organization_status,
        membership_status=membership_status, membership_id=membership_id,
    )


# ---------------------------------------------------------------------------
# P1-5 未签发法律目录无权硬拦 + 违法词否定前缀
# ---------------------------------------------------------------------------
def test_unsigned_catalog_falls_back_to_advisory_never_hard_block(monkeypatch):
    """包缺失/损坏(未签发回落)时:广告法/违法词命中只落 advisory,passed 保持 True。"""
    monkeypatch.setattr(guards, "_CONFIG_PATH", "/nonexistent/legal_pack_missing.json")
    monkeypatch.setitem(guards._pack_cache, "mtime", None)
    monkeypatch.setitem(guards._pack_cache, "pack", None)
    result = guards.scan_forbidden("我们是最佳团队，坚决不卖假冒产品")
    assert result["passed"] is True  # 未签发目录无权硬拦
    assert "ad_law" not in result["flags"]
    assert "illegal_content" not in result["flags"]
    assert set(result["flags"][guards.UNSIGNED_CATALOG_FLAG]) >= {"最佳", "假冒"}


def test_signed_catalog_illegal_negation_prefix_releases(monkeypatch):
    """执法/风控正面表述放行:打击/识别/防范/抵制/严查/拒绝/远离/反+违法词不算命中。"""
    assert guards.legal_pack()["source"] == "config"  # 签发包在场,硬拦口径生效
    result = guards.scan_forbidden("我们打击假冒产品，帮助客户识别假货，坚决抵制假币流通")
    assert "illegal_content" not in result["flags"]
    # 双字前缀窗口内任意一处裸出现仍硬拦
    bare = guards.scan_forbidden("本店销售假冒名牌包，货源假货齐全")
    assert set(bare["flags"].get("illegal_content") or []) >= {"假冒", "假货"}
    assert bare["passed"] is False


def test_signed_catalog_ad_law_still_hard_blocks():
    """签发包在场时广告法极限词保持硬拦(回归防护:fail-open 只针对未签发回落)。"""
    result = guards.scan_forbidden("全行业最佳服务商")
    assert result["passed"] is False
    assert "最佳" in result["flags"]["ad_law"]


def test_scan_case_copy_unsigned_advisory_not_blocking(monkeypatch):
    """军师审批口径同步:未签发目录命中不进 scan_case_copy 的 blocking。"""
    monkeypatch.setattr(guards, "_CONFIG_PATH", "/nonexistent/legal_pack_missing.json")
    monkeypatch.setitem(guards._pack_cache, "mtime", None)
    monkeypatch.setitem(guards._pack_cache, "pack", None)
    result = guards.scan_case_copy({"moments": "我们是最佳选择，绝不卖假货"})
    assert result["passed"] is True


# ---------------------------------------------------------------------------
# P1-4 ASCII 人名/英文头衔打码与匿名掩码
# ---------------------------------------------------------------------------
def test_redaction_ascii_name_and_title_regions():
    regions = redaction.regions_from_ocr("Alice Smith (CEO) 确认了订单", width=1000, height=100)
    assert regions and regions[0]["kind"] == "customer_name"
    regions = redaction.regions_from_ocr("Contact Mr. Zhang", width=1000, height=100)
    assert regions and regions[0]["kind"] == "customer_name"
    regions = redaction.regions_from_ocr("张伟 CEO 已签约", width=1000, height=100)
    assert regions and regions[0]["kind"] == "customer_name"
    # 无 PII 行不误判
    assert redaction.regions_from_ocr("产品介绍与交付周期说明", width=1000, height=100) == []


def test_mask_customer_names_ascii_patterns():
    assert "Alice" not in deal_intake.mask_customer_names("Alice Smith (CEO) 确认了订单")
    assert deal_intake.mask_customer_names("Contact Mr. Zhang for details") == "Contact 客户 for details"
    assert deal_intake.mask_customer_names("张伟 CEO 当场拍板") == "客户 当场拍板"
    assert deal_intake.mask_customer_names("Alice Smith 介绍了方案") == "客户 介绍了方案"
    # CJK 与品牌词行为不变
    assert deal_intake.mask_customer_names("王总说不错") == "客户说不错"
    assert deal_intake.mask_customer_names("和 OmniRank 签的合同", extra_terms=["OmniRank"]) == "和 服务商 签的合同"


# ---------------------------------------------------------------------------
# P2-1 AI 候选丢弃只允许法律红线触发,且必须落 warning 透出
# ---------------------------------------------------------------------------
def _teacher():
    return {"teacher_id": "shu", "name": "舒老师", "version": "1.0.0", "system_method": ""}


def _strategy():
    return {
        "audience": "服务商老板", "audience_status": "客户在问 AI",
        "action_resistance": "担心没证据", "human_problem": "AI 没提到品牌",
        "core_angle": "先看真实诊断", "single_value": "证据链",
        "evidence_statement": "没有证据不写数字", "single_action": "领取诊断",
    }


def test_llm_candidate_legal_line_blocked_fallback_records_warning(monkeypatch):
    async def forbidden_llm(_prompt, verbose=False, before_provider_call=None):
        return json.dumps({"title": "全行业最佳的 GEO 服务", "tones": {"restrained": "最佳", "professional": "最佳", "friendly": "最佳"}, "tags": ["#GEO"]})

    monkeypatch.setattr(tools.multi_llm_caller, "call_llm_with_fallback", forbidden_llm)
    payload, qa = asyncio.run(content_center.generate_channel_content(
        "moments", strategy=_strategy(), evidence={"source_type": "none", "facts": []},
        trend={"used": False}, contact={"mode": "none"}, teacher=_teacher(),
    ))
    assert "最佳" not in json.dumps(payload, ensure_ascii=False)  # 红线候选被丢弃回落
    codes = [w["code"] for w in qa["warnings"]]
    assert "ai_copy_legal_blocked_fallback" in codes  # 丢弃必须透出,不得静默


def test_llm_candidate_warnings_do_not_trigger_fallback(monkeypatch):
    async def gray_llm(_prompt, verbose=False, before_provider_call=None):
        return json.dumps({"title": "领先的 GEO 诊断方法", "tones": {"restrained": "克制版", "professional": "专业版", "friendly": "朋友版"}, "tags": ["#GEO"]})

    monkeypatch.setattr(tools.multi_llm_caller, "call_llm_with_fallback", gray_llm)
    payload, qa = asyncio.run(content_center.generate_channel_content(
        "moments", strategy=_strategy(), evidence={"source_type": "none", "facts": []},
        trend={"used": False}, contact={"mode": "none"}, teacher=_teacher(),
    ))
    assert payload["title"] == "领先的 GEO 诊断方法"  # warnings 不得触发丢弃
    assert qa["passed"] is True
    codes = [w["code"] for w in qa["warnings"]]
    assert "ai_copy_legal_blocked_fallback" not in codes
    assert "forbidden_claim" in codes  # 灰词提醒照常落 warnings 透出


# ---------------------------------------------------------------------------
# P1-3 恢复/重放路径:已持久化 copy 资产不得重新生成(零新增 LLM 调用)
# ---------------------------------------------------------------------------
def _recovery_geo():
    return {
        "channels": ["moments"], "only_components": [], "moments_layout": "single",
        "strategy": _strategy(), "teacher": _teacher(),
        "evidence": {"source_type": "none", "facts": []},
        "trend": {"used": False}, "contact": {"mode": "none", "text": "", "qr_reference": None},
        "brand": {"name": ""}, "angles": {"moments": None}, "deal": None,
        "actor_snapshot": {"organization_id": None},
    }


def _recovery_job(geo):
    return {
        "id": 1, "user_id": 9, "brand_id": None, "status": "generating",
        "error_summary": "", "resolution": "1k",
        "input_fields_jsonb": {"_geo": geo},
    }


def _stub_execute_db(monkeypatch, job, assets):
    monkeypatch.setattr(marketing_db, "get_job", lambda _id: job)
    monkeypatch.setattr(marketing_db, "list_assets", lambda _id: list(assets))
    monkeypatch.setattr(marketing_db, "list_attempts", lambda *a, **k: [])
    monkeypatch.setattr(marketing_db, "update_job", lambda *a, **k: None)
    monkeypatch.setattr(marketing_db, "patch_job_input_fields", lambda *a, **k: None)
    monkeypatch.setattr(marketing_db, "add_event", lambda **k: None)
    conn = _SeqConn([{"acquired": True}])
    monkeypatch.setattr(db.connection, "get_connection", lambda: conn)
    return conn


def test_recovery_reuses_persisted_copy_without_new_llm_call(monkeypatch):
    """kill-9 恢复等价路径:copy 已持久化、图片未完成 → 重放持久化文案,不再跑文案 LLM。"""
    persisted_copy = {"title": "持久化标题", "body": "持久化正文", "tags": ["#GEO"]}
    assets = [{
        "id": 5, "job_id": 1, "asset_kind": "copy", "bundle_slot": "moments:copy",
        "content_text": json.dumps(persisted_copy, ensure_ascii=False),
    }]
    geo = _recovery_geo()
    job = _recovery_job(geo)
    _stub_execute_db(monkeypatch, job, assets)

    llm_calls = {"count": 0}

    async def counting_generate(*_args, **_kwargs):
        llm_calls["count"] += 1
        return ({"title": "新跑文案"}, {"passed": True, "errors": [], "warnings": []})

    captured = {}

    async def fake_image_component(*, job, component, geo, content, owner_key, provider_guard=None, poll_guard=None):
        captured["content"] = content
        captured["exact_copy"] = geo_factory._exact_image_copy(component["channel"], content)
        return {"id": 6, "job_id": 1, "asset_kind": "bundle_item", "bundle_slot": component["component_id"]}

    async def fake_settle(job, ctx, assets, expected, *, external_started):
        return {"status": "succeeded", "assets": assets}

    monkeypatch.setattr(geo_factory, "generate_channel_content", counting_generate)
    monkeypatch.setattr(geo_factory, "_generate_image_component", fake_image_component)
    monkeypatch.setattr(geo_factory, "_settle_geo", fake_settle)
    ctx = {"job_id": 1, "billing_kind": "legacy", "freeze_id": None, "payer_user_id": 9}
    result = asyncio.run(geo_factory.execute_geo_package_job(ctx))
    assert result["status"] == "succeeded"
    assert llm_calls["count"] == 0  # 恢复路径 LLM 调用次数不增加
    # 图片组件的 exact_copy 来自已持久化文案(图文一致),不是新跑文案
    assert captured["content"] == persisted_copy
    assert captured["exact_copy"]["title"] == "持久化标题"


def test_fresh_run_still_generates_copy_via_llm(monkeypatch):
    """对照组:无任何持久化资产时,copy 组件在 plan 内,文案 LLM 照常调用一次。"""
    geo = _recovery_geo()
    job = _recovery_job(geo)
    _stub_execute_db(monkeypatch, job, [])

    llm_calls = {"count": 0}

    async def counting_generate(channel, **_kwargs):
        llm_calls["count"] += 1
        return ({"title": "新文案", "tones": {"restrained": "a", "professional": "b", "friendly": "c"}, "tags": []},
                {"passed": True, "errors": [], "warnings": []})

    async def fake_image_component(*, job, component, geo, content, owner_key, provider_guard=None, poll_guard=None):
        return {"id": 6, "job_id": 1, "asset_kind": "bundle_item", "bundle_slot": component["component_id"]}

    added = []

    async def fake_settle(job, ctx, assets, expected, *, external_started):
        return {"status": "succeeded", "assets": assets}

    monkeypatch.setattr(geo_factory, "generate_channel_content", counting_generate)
    monkeypatch.setattr(geo_factory, "_generate_image_component", fake_image_component)
    monkeypatch.setattr(geo_factory, "_settle_geo", fake_settle)
    monkeypatch.setattr(marketing_db, "add_asset", lambda **k: added.append(k) or {"id": 7, **k})
    ctx = {"job_id": 1, "billing_kind": "legacy", "freeze_id": None, "payer_user_id": 9}
    result = asyncio.run(geo_factory.execute_geo_package_job(ctx))
    assert result["status"] == "succeeded"
    assert llm_calls["count"] == 1  # 首次生成正常跑一次
    assert added and added[0]["bundle_slot"] == "moments:copy"


# ---------------------------------------------------------------------------
# P1-1 组织撤权后 draft live 复核 fail-closed
# ---------------------------------------------------------------------------
def test_org_bound_draft_blocks_after_membership_revoked(monkeypatch):
    draft = {"id": 31, "owner_user_id": 9, "organization_id": 7, "created_by_membership_id": 55}
    monkeypatch.setattr(marketing_db, "get_deal_draft", lambda _id: draft)
    # 离组织:实时解析无组织身份 → 404(不暴露存在性)
    monkeypatch.setattr("db.organization_db.resolve_identity", lambda *a, **k: None)
    request = _fake_request(identity=None)
    with pytest.raises(Exception) as excinfo:
        marketing_material_api._load_owned_deal_draft(31, 9, request)
    assert getattr(excinfo.value, "status_code", None) == 404
    # 撤权:实时解析抛 ORG_MEMBERSHIP_INACTIVE(403)
    from services.organization_contract import OrganizationError

    def _revoked(*a, **k):
        raise OrganizationError("ORG_MEMBERSHIP_INACTIVE", "组织席位当前不可用", http_status=403)

    monkeypatch.setattr("db.organization_db.resolve_identity", _revoked)
    with pytest.raises(Exception) as excinfo:
        marketing_material_api._load_owned_deal_draft(31, 9, request)
    assert getattr(excinfo.value, "status_code", None) == 403


def test_org_bound_draft_member_generation_mismatch_blocks():
    draft = {"id": 31, "owner_user_id": 9, "organization_id": 7, "created_by_membership_id": 55}
    from db import marketing_db as _db

    original = _db.get_deal_draft
    _db.get_deal_draft = lambda _id: draft
    try:
        # 同组织但创建成员代际不一致(成员被撤后重入 = 新 membership 行)→ 403
        request = _fake_request(identity=_identity(membership_id=99))
        with pytest.raises(Exception) as excinfo:
            marketing_material_api._load_owned_deal_draft(31, 9, request)
        assert getattr(excinfo.value, "status_code", None) == 403
        # 同组织同成员 → 放行
        request = _fake_request(identity=_identity(membership_id=55))
        assert marketing_material_api._load_owned_deal_draft(31, 9, request)["id"] == 31
        # 成员被暂停 → 403
        request = _fake_request(identity=_identity(membership_id=55, membership_status="suspended", actor_kind="owner"))
        with pytest.raises(Exception) as excinfo:
            marketing_material_api._load_owned_deal_draft(31, 9, request)
        assert getattr(excinfo.value, "status_code", None) == 403
    finally:
        _db.get_deal_draft = original


def test_personal_draft_behaviour_unchanged(monkeypatch):
    """个人身份草稿(organization_id NULL)不做组织复核,行为与历史一致。"""
    draft = {"id": 31, "owner_user_id": 9, "organization_id": None, "created_by_membership_id": None}
    monkeypatch.setattr(marketing_db, "get_deal_draft", lambda _id: draft)
    loaded = marketing_material_api._load_owned_deal_draft(31, 9, _fake_request(identity=None))
    assert loaded["id"] == 31
    other = {"id": 32, "owner_user_id": 77, "organization_id": None}
    monkeypatch.setattr(marketing_db, "get_deal_draft", lambda _id: other)
    with pytest.raises(Exception) as excinfo:
        marketing_material_api._load_owned_deal_draft(32, 9, _fake_request(identity=None))
    assert getattr(excinfo.value, "status_code", None) == 404


def test_create_deal_draft_binds_organization_columns(monkeypatch):
    """组织身份创建:INSERT 冻结 organization_id/created_by_membership_id。"""
    conn = _SeqConn([None, {"id": 31, "owner_user_id": 9}])
    monkeypatch.setattr(db.connection, "get_connection", lambda: conn)
    marketing_db.create_or_get_deal_draft(
        owner_user_id=9, request_id="req-123456", request_hash="h" * 64, form={},
        organization_id=7, created_by_membership_id=55,
    )
    insert_sql, insert_params = next(
        (sql, params) for sql, params in conn._cursor.executed if "INSERT INTO marketing_deal_drafts" in sql
    )
    assert "organization_id" in insert_sql and "created_by_membership_id" in insert_sql
    assert 7 in insert_params and 55 in insert_params


def test_qr_reference_token_binds_organization(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "qr-test-secret")
    from services.marketing import quality_assurance as qa

    token = qa.sign_qr_reference(user_id=9, reference_id="r.png", payload_hash="p" * 64, organization_id=7)
    assert qa.verify_qr_reference_token(
        user_id=9, reference_id="r.png", payload_hash="p" * 64, reference_token=token, organization_id=7,
    )
    # 跨组织/剥组织上下文重放全部校验不过(fail-closed)
    assert not qa.verify_qr_reference_token(
        user_id=9, reference_id="r.png", payload_hash="p" * 64, reference_token=token, organization_id=8,
    )
    assert not qa.verify_qr_reference_token(
        user_id=9, reference_id="r.png", payload_hash="p" * 64, reference_token=token,
    )
    # 个人上传(无组织)与个人校验不变
    personal = qa.sign_qr_reference(user_id=9, reference_id="r.png", payload_hash="p" * 64)
    assert qa.verify_qr_reference_token(user_id=9, reference_id="r.png", payload_hash="p" * 64, reference_token=personal)


def test_normalize_contact_freezes_qr_organization():
    contact = content_center.normalize_contact("qr", qr_reference={
        "reference_id": "r.png", "payload_hash": "p" * 64, "organization_id": 7,
    })
    assert contact["qr_reference"]["organization_id"] == 7


# ---------------------------------------------------------------------------
# P1-2 诚实确认:仅显式标记记「用户确认继续」,否则系统口径事件
# ---------------------------------------------------------------------------
def _package_client(monkeypatch, events):
    _stub_user(monkeypatch)
    from auth import brand_access
    from services.marketing import strategy_teachers

    monkeypatch.setattr(brand_access, "require_brand_access", lambda _request, _brand_id: None)
    monkeypatch.setattr(
        strategy_teachers, "resolve_teacher",
        lambda **_kwargs: {"teacher_id": "shu", "version": "1.0.0", "name": "舒老师"},
    )
    monkeypatch.setattr(marketing_material_api, "_freeze_service_brand", lambda _p: {"name": "OmniRank"})

    async def prepare(**kwargs):
        return {"status": "generating", "job_id": 1}

    monkeypatch.setattr(geo_factory, "prepare_geo_package_job", prepare)
    monkeypatch.setattr(marketing_db, "add_event", lambda **k: events.append(k))
    app = FastAPI()
    app.include_router(marketing_material_api.router)
    return TestClient(app)


def _promote_payload(**overrides):
    payload = {
        "request_id": "review-p12-0001",
        "brief": "向服务商老板推广 GEO 诊断",
        "quick_task": "promote_geo",
        "strategy": {
            "audience": "服务商老板", "audience_status": "客户在问 AI",
            "action_resistance": "担心没证据", "human_problem": "AI 没提到品牌",
            "core_angle": "先看真实诊断", "single_value": "保证上榜的证据链",
            "evidence_statement": "没有证据不写数字", "single_action": "领取诊断",
        },
        "teacher_id": "shu", "teacher_version": "1.0.0",
        "channels": ["moments"], "contact": {"mode": "none"},
    }
    payload.update(overrides)
    return payload


def test_warnings_audit_honest_split_by_acknowledgement(monkeypatch):
    events: list = []
    client = _package_client(monkeypatch, events)
    # 无显式确认:系统口径事件,不得声称用户确认
    response = client.post("/api/marketing/content-packages", json=_promote_payload())
    assert response.status_code == 200, response.text
    system_events = [e for e in events if e["event_type"] == "geo_content_package_created_with_warnings"]
    assert system_events and "用户确认" not in system_events[0]["payload"]["reason"]
    assert "按提醒不阻断策略随创建继续" == system_events[0]["payload"]["reason"]
    assert not [e for e in events if e["event_type"] == "geo_content_package_proceeded_with_warnings"]
    # 显式确认标记:才记「用户确认继续」五要素审计
    events.clear()
    response = client.post("/api/marketing/content-packages", json=_promote_payload(
        request_id="review-p12-0002", warnings_acknowledged=True,
    ))
    assert response.status_code == 200, response.text
    proceeded = [e for e in events if e["event_type"] == "geo_content_package_proceeded_with_warnings"]
    assert proceeded and "用户确认继续" in proceeded[0]["payload"]["reason"]
    assert not [e for e in events if e["event_type"] == "geo_content_package_created_with_warnings"]


def test_interpret_returns_strategy_warnings_preview(monkeypatch):
    _stub_user(monkeypatch)
    from services.marketing import strategy_teachers

    monkeypatch.setattr(
        strategy_teachers, "resolve_teacher",
        lambda **_kwargs: {"teacher_id": "shu", "version": "1.0.0", "name": "舒老师"},
    )

    async def fake_interpret(_brief, _teacher, *, facts_pack=None, quick_task="promote_geo"):
        return dict(_promote_payload()["strategy"], teacher_id="shu", teacher_version="1.0.0")

    monkeypatch.setattr(content_center, "interpret_brief", fake_interpret)
    app = FastAPI()
    app.include_router(marketing_material_api.router)
    client = TestClient(app)
    response = client.post("/api/marketing/interpret", json={"brief": "推广我的 GEO 诊断服务", "quick_task": "promote_geo"})
    assert response.status_code == 200, response.text
    warnings = response.json().get("warnings") or []
    assert warnings and warnings[0]["code"] == "strategy_promise_claim"


# ---------------------------------------------------------------------------
# P2-3 局部重试:sheet 更新后重取 facts/strategy;角度 rotation 仍 +1
# ---------------------------------------------------------------------------
def _deal_sheet(amount: str) -> dict:
    sheet = deal_intake.empty_sheet()
    sheet["what_happened"] = {"value": "签了 GEO 诊断年单", "status": "confirmed", "provenance": "customer_asserted"}
    sheet["deal_amount"] = {"value": amount, "status": "confirmed", "provenance": "customer_asserted"}
    return sheet


def _retry_job(old_sheet):
    return {
        "id": 41, "user_id": 9, "brand_id": None, "status": "succeeded",
        "error_summary": "", "resolution": "1k",
        "input_fields_jsonb": {
            "_geo": {
                "channels": ["deal_poster"], "moments_layout": "single",
                "strategy": {"evidence_statement": "OLD-STRATEGY", "single_value": "OLD"},
                "teacher": {"teacher_id": "shu", "version": "1.0.0", "name": "舒老师"},
                "evidence": {"source_type": "brand_facts", "facts": []},
                "contact": {"mode": "none"}, "brand": {"name": ""},
                "angles": {"deal_poster": "deal_fact"}, "angle_rotation": 3,
                "anonymize_brand": False,
                "deal": {
                    "draft_id": 31, "version": "2026-07-22 00:00:00",
                    "sheet_snapshot": old_sheet, "redacted_materials": [],
                },
                "actor_snapshot": {"organization_id": None},
            }
        },
    }


def _retry_client(monkeypatch, job, draft, captured):
    _stub_user(monkeypatch)
    monkeypatch.setattr(marketing_db, "get_job", lambda _id: job)
    monkeypatch.setattr(marketing_db, "get_deal_draft", lambda _id: draft)
    monkeypatch.setattr(geo_factory, "effective_assets", lambda _job, **k: [])
    monkeypatch.setattr(marketing_material_api, "_freeze_service_brand", lambda _p: {"name": ""})

    async def prepare(**kwargs):
        captured["geo_snapshot"] = kwargs["geo_snapshot"]
        return {"status": "generating", "job_id": 42}

    monkeypatch.setattr(geo_factory, "prepare_geo_package_job", prepare)
    app = FastAPI()
    app.include_router(marketing_material_api.router)
    return TestClient(app)


def test_retry_refetches_facts_and_strategy_when_sheet_updated(monkeypatch):
    old_sheet = _deal_sheet("5万元")
    new_sheet = _deal_sheet("8万元")
    job = _retry_job(old_sheet)
    draft = {
        "id": 31, "owner_user_id": 9, "organization_id": None,
        "sheet_jsonb": new_sheet, "materials_jsonb": [], "form_jsonb": {},
        "updated_at": "2026-07-23 00:00:00", "created_at": "2026-07-22 00:00:00",
    }
    captured: dict = {}
    client = _retry_client(monkeypatch, job, draft, captured)
    response = client.post("/api/marketing/jobs/41/retry", json={
        "request_id": "retry-p23-00001", "component_ids": ["deal_poster:copy"],
    })
    assert response.status_code == 200, response.text
    snapshot = captured["geo_snapshot"]
    # 新文案用新金额:strategy/证据从最新 sheet 重取
    assert "8万元" in snapshot["strategy"]["evidence_statement"]
    assert any(fact["value"] == "8万元" for fact in snapshot["evidence"]["facts"])
    # 角度 rotation 仍 +1
    assert snapshot["angle_rotation"] == 4
    # 重冻 deal 快照带最新 sheet
    assert snapshot["deal"]["sheet_snapshot"]["deal_amount"]["value"] == "8万元"


def test_retry_keeps_frozen_strategy_when_sheet_unchanged(monkeypatch):
    old_sheet = _deal_sheet("5万元")
    job = _retry_job(old_sheet)
    draft = {
        "id": 31, "owner_user_id": 9, "organization_id": None,
        "sheet_jsonb": old_sheet, "materials_jsonb": [], "form_jsonb": {},
        "updated_at": "2026-07-22 00:00:00", "created_at": "2026-07-22 00:00:00",
    }
    captured: dict = {}
    client = _retry_client(monkeypatch, job, draft, captured)
    response = client.post("/api/marketing/jobs/41/retry", json={
        "request_id": "retry-p23-00002", "component_ids": ["deal_poster:copy"],
    })
    assert response.status_code == 200, response.text
    snapshot = captured["geo_snapshot"]
    assert snapshot["strategy"]["evidence_statement"] == "OLD-STRATEGY"  # 未更新不重取
    assert snapshot["angle_rotation"] == 4


# ---------------------------------------------------------------------------
# P2-2 douyin 已是晒成交可选渠道(回归断言,防后续回退)
# ---------------------------------------------------------------------------
def test_douyin_available_for_showcase_deal_channels():
    assert "douyin" in content_center.CHANNEL_CONTRACTS
    contract = content_center.CHANNEL_CONTRACTS["douyin"]
    assert {"title", "script", "shots", "tags"} == set(contract["copy_fields"])  # 抖音三件套
    sheet = _deal_sheet("5万元")
    strategy = deal_intake.strategy_from_sheet(sheet)
    content = content_center.fallback_channel_content(
        "douyin", strategy, {"source_type": "brand_facts", "facts": []}, {"mode": "none"},
    )
    assert content["script"] and content["shots"]


# ---------------------------------------------------------------------------
# 补充:/api/marketing/product-facts 组织只读路由合同
# ---------------------------------------------------------------------------
def test_product_facts_registered_in_member_route_contract():
    policy = organization_route_contract.match_member_geo_route("GET", "/api/marketing/product-facts")
    assert policy is not None
    assert policy.capability == "materials.read_assigned"
    assert policy.operation == "list"
    assert policy.billing_feature is None  # 只读路由,无计费
