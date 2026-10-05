"""GEO v2 Phase 1 contracts: composer versioning, channel/evidence expansion,
asset copy editing, and active EXIF stripping. DB-free mock style."""
import asyncio
import io
import json
from pathlib import Path

import pytest
from PIL import Image
from fastapi import HTTPException
from starlette.requests import Request

import db.connection
from api import marketing_material_api
from services.marketing import content_center, geo_factory, material_storage, prompt_composer
from services.marketing.evidence import freeze_evidence, require_frozen_evidence_live


def _jpeg_with_exif() -> bytes:
    image = Image.new("RGB", (120, 80), "white")
    exif = Image.Exif()
    exif[0x010F] = "TestMake"  # Make
    exif[0x0110] = "TestModel"  # Model
    exif[0x0112] = 6  # Orientation: transpose to portrait on decode
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif.tobytes())
    return buffer.getvalue()


class _FakeCursor:
    def __init__(self, row):
        self._row = row
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row=None):
        self._cursor = _FakeCursor(row)
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def close(self):
        pass


def _request(user_id: int = 9) -> Request:
    request = Request({"type": "http", "method": "PATCH", "path": "/api/marketing/assets/42", "headers": []})
    request.state.user = {"id": user_id}
    return request


def test_prompt_composer_is_versioned_deterministic_with_eight_elements():
    brief = prompt_composer.VisualBrief(
        task_type="channel_marketing_image",
        subject={"audience": "服务商"},
        visual_style="深色、克制、真实业务画面",
        composition_and_lighting="一个强标题，其次证据，最后行动",
        exact_visible_text={"title": "先看真实诊断"},
        platform_ratio="3:4",
        brand_assets={"name": "OmniRank"},
        privacy_and_truth_constraints=("伪造排名或收益", "未经证据支持的数字"),
    )
    composer = prompt_composer.VisualPromptComposer()
    first = composer.compose(brief)
    assert first == composer.compose(brief)  # 确定性模板装配
    assert prompt_composer.PROMPT_COMPOSER_VERSION == "geo-vpc/2.0"
    document = json.loads(first.split("\n", 1)[1])
    assert document["composer_version"] == "geo-vpc/2.0"
    for key in (
        "task_type", "subject", "visual_style", "composition_and_lighting",
        "exact_visible_text", "platform_ratio", "brand_assets", "privacy_and_truth_constraints",
    ):
        assert key in document
    assert "gpt-image-2" in document["model_instruction"]
    assert "禁止程序叠字感" in document["model_instruction"]


def test_visual_prompt_keeps_v1_semantics_and_freezes_composer_version():
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
    document = json.loads(prompt.split("\n", 1)[1])
    assert document["composer_version"] == "geo-vpc/2.0"
    assert document["platform_ratio"] == "9:16"
    # 联系方式开启才进图;none 时 exact_visible_text 不含 contact
    assert "contact" not in document["exact_visible_text"]
    opened = content_center.visual_prompt(
        slot={"slot": "professional_poster", "size": "3:4"}, channel="professional_poster",
        strategy={"audience": "服务商", "single_action": "领取诊断", "single_value": "证据"},
        content={"title": "先看诊断"},
        evidence={"facts": []}, trend={"used": False},
        contact={"mode": "text", "text": "微信 geo-demo", "qr_reference": None}, brand={},
    )
    opened_doc = json.loads(opened.split("\n", 1)[1])
    assert opened_doc["exact_visible_text"]["contact"] == "微信 geo-demo"


def test_private_chat_scene_labels_example_dialogue_in_prompt_and_image_qa():
    prompt = content_center.visual_prompt(
        slot={"slot": "private_chat_scene", "size": "9:16"}, channel="private_chat",
        strategy={"audience": "服务商", "single_action": "领取诊断", "single_value": "证据"},
        content={"opening": "先看诊断"}, evidence={"facts": []}, trend={"used": False},
        contact={"mode": "none", "text": "", "qr_reference": None}, brand={},
    )
    document = json.loads(prompt.split("\n", 1)[1])
    assert document["task_type"] == "simulated_chat_scene"
    assert document["exact_visible_text"]["scene_label"] == "示例对话"
    assert "示例对话" in document["composition_and_lighting"]
    assert any("示例对话" in item for item in document["privacy_and_truth_constraints"])
    exact = geo_factory._exact_image_copy("private_chat", {"opening": "先看诊断"})
    assert exact["scene_label"] == "示例对话"  # visual_qa OCR 强制核验


def test_infographic_private_chat_and_moments_grid_component_contracts():
    assert geo_factory.valid_component_ids(["infographic"]) == {
        "infographic:copy", "infographic:image:infographic",
    }
    assert "private_chat:image:private_chat_scene" in geo_factory.valid_component_ids(["private_chat"])
    assert geo_factory.valid_component_ids(["moments"]) == {"moments:copy", "moments:image:moments"}
    grid = geo_factory.valid_component_ids(["moments"], moments_layout="grid")
    assert grid == {"moments:copy"} | {f"moments:image:moments_grid_{index}" for index in range(1, 10)}
    # only_components 差集自然覆盖九宫格组件
    plan = geo_factory._component_plan({
        "channels": ["moments"], "moments_layout": "grid",
        "only_components": ["moments:image:moments_grid_3"],
    })
    assert [item["component_id"] for item in plan] == ["moments:image:moments_grid_3"]
    assert content_center.normalize_moments_layout("GRID") == "grid"
    with pytest.raises(ValueError, match="moments_layout_invalid"):
        content_center.normalize_moments_layout("collage")


def test_evidence_four_way_sources_and_provenance_stamps():
    evergreen = freeze_evidence(brand_id=None, source_type="none")
    assert evergreen["facts"] == [] and evergreen["snapshot_hash"]
    require_frozen_evidence_live(evergreen)  # 默认 none 不阻断,不触 DB

    brand = freeze_evidence(
        brand_id=101, source_type="brand_facts",
        facts=[
            {"key": "founding_year", "label": "成立年份", "value": "2015"},
            {"key": "deal_amount", "label": "成交金额", "value": "5万元", "provenance": "customer_asserted"},
        ],
    )
    assert brand["source_type"] == "brand_facts"
    assert brand["facts"][0]["provenance"] == "brand_asserted"
    assert brand["facts"][1]["provenance"] == "customer_asserted"  # 晒成交出处标记透传
    require_frozen_evidence_live(brand)  # 品牌自述事实不核验系统 SSOT

    with pytest.raises(ValueError, match="unsupported_evidence_source"):
        freeze_evidence(brand_id=101, source_type="monitoring")
    with pytest.raises(ValueError, match="evidence_brand_required"):
        freeze_evidence(brand_id=None, source_type="system_facts", facts=[{"key": "geo_score", "value": 71}])


def test_system_facts_verify_against_ssot_and_keep_three_way_rejection(monkeypatch):
    row = {"id": 55, "brand_id": 101, "total_score": 71, "level": "B", "created_at": None}
    monkeypatch.setattr(db.connection, "get_connection", lambda: _FakeConn(row))
    frozen = freeze_evidence(
        brand_id=101, source_type="system_facts",
        facts=[{"key": "geo_score", "label": "GEO 诊断评分", "value": 71}],
    )
    assert frozen["source_type"] == "system_facts"
    assert frozen["facts"] == [{
        "key": "geo_score", "label": "GEO 诊断评分", "value": 71, "provenance": "system_verified",
    }]
    with pytest.raises(ValueError, match="system_fact_not_verifiable"):
        freeze_evidence(brand_id=101, source_type="system_facts", facts=[{"key": "geo_score", "value": 99}])
    with pytest.raises(ValueError, match="system_fact_not_verifiable"):
        freeze_evidence(brand_id=101, source_type="system_facts", facts=[{"key": "推荐率", "value": "80"}])

    require_frozen_evidence_live(frozen)  # SSOT 未变 → 放行
    monkeypatch.setattr(db.connection, "get_connection", lambda: _FakeConn(None))
    with pytest.raises(ValueError, match="evidence_revoked_or_deleted"):
        require_frozen_evidence_live(frozen)  # 一重拒:来源删除/撤回
    monkeypatch.setattr(
        db.connection, "get_connection",
        lambda: _FakeConn({**row, "total_score": 72}),
    )
    with pytest.raises(ValueError, match="evidence_changed_since_freeze"):
        require_frozen_evidence_live(frozen)  # 三重拒:冻结后被篡改


def test_diagnosis_freeze_stamps_provenance_and_live_recheck(monkeypatch):
    row = {"id": 55, "brand_id": 101, "total_score": 71, "level": "B", "created_at": None}
    monkeypatch.setattr(db.connection, "get_connection", lambda: _FakeConn(row))
    frozen = freeze_evidence(brand_id=101, source_type="latest_diagnosis")
    assert frozen["source_type"] == "diagnosis"
    assert {fact["provenance"] for fact in frozen["facts"]} == {"diagnosis"}
    require_frozen_evidence_live(frozen)
    assert content_center.diagnosis_case_evidence_ok(frozen) is True
    brand_only = freeze_evidence(brand_id=101, source_type="brand_facts", facts=[{"key": "k", "value": "v"}])
    assert content_center.diagnosis_case_evidence_ok(brand_only) is False


def test_geo_snapshot_freezes_prompt_composer_version_and_moments_layout(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from auth import brand_access
    from services.marketing import strategy_teachers

    monkeypatch.setattr(marketing_material_api, "_require_user", lambda _request: {"id": 1})
    monkeypatch.setattr(marketing_material_api, "_uid", lambda _user: 1)
    monkeypatch.setattr(marketing_material_api, "_principal_user_id", lambda _request, _user: 1)
    monkeypatch.setattr(brand_access, "require_brand_access", lambda _request, _brand_id: None)
    monkeypatch.setattr(
        strategy_teachers, "resolve_teacher",
        lambda **_kwargs: {"teacher_id": "shu", "version": "1.0.0", "name": "舒老师"},
    )
    monkeypatch.setattr(marketing_material_api, "_clean_strategy", lambda value, _teacher, _brief: value)
    monkeypatch.setattr(
        marketing_material_api, "_freeze_service_brand",
        lambda _principal: {"name": "OmniRank"},
    )
    captured = {}

    async def prepare(**kwargs):
        captured["geo_snapshot"] = kwargs["geo_snapshot"]
        return {"status": "generating", "job_id": 1}

    monkeypatch.setattr(geo_factory, "prepare_geo_package_job", prepare)
    app = FastAPI()
    app.include_router(marketing_material_api.router)
    response = TestClient(app).post("/api/marketing/content-packages", json={
        "request_id": "composer-freeze-001",
        "brief": "向服务商老板推广真实 GEO 诊断",
        "quick_task": "promote_geo",
        "strategy": {"audience": "服务商老板"},
        "channels": ["moments"],
        "evidence": {"source_type": "none"},
        "contact": {"mode": "none"},
        "moments_layout": "grid",
    })
    assert response.status_code == 200, response.text
    snapshot = captured["geo_snapshot"]
    assert snapshot["prompt_composer_version"] == "geo-vpc/2.0"
    assert snapshot["contract_version"] == "geo-content-center/1.0"
    assert snapshot["moments_layout"] == "grid"

    invalid = TestClient(app).post("/api/marketing/content-packages", json={
        "request_id": "composer-freeze-002",
        "brief": "向服务商老板推广真实 GEO 诊断",
        "quick_task": "promote_geo",
        "strategy": {"audience": "服务商老板"},
        "channels": ["moments"],
        "moments_layout": "collage",
    })
    assert invalid.status_code == 422
    assert invalid.json()["detail"] == "moments_layout_invalid"


def _copy_asset() -> dict:
    return {
        "id": 42, "job_id": 7, "asset_kind": "copy", "bundle_slot": "professional_poster:copy",
        "content_text": json.dumps(
            {"title": "先看真实诊断", "body": "只讲可核验事实", "cta": "领取诊断", "tags": ["#GEO"]},
            ensure_ascii=False,
        ),
        "job_status": "succeeded", "job_error_summary": "",
        "input_fields_jsonb": {"_geo": {
            "evidence": {"source_type": "none", "facts": []},
            "contact": {"mode": "none", "text": ""},
        }},
    }


def _stub_edit_endpoint(monkeypatch, asset: dict):
    events = []
    conn = _FakeConn()
    monkeypatch.setattr(marketing_material_api, "_load_delivery_asset", lambda _asset_id, _uid: asset)
    monkeypatch.setattr(marketing_material_api, "_require_live_delivery_authority", lambda _request, _asset: None)
    monkeypatch.setattr(db.connection, "get_connection", lambda: conn)
    monkeypatch.setattr(
        marketing_material_api.marketing_db, "add_event",
        lambda **kwargs: events.append(kwargs),
    )
    return conn, events


def test_asset_copy_edit_saves_after_qa_and_writes_audit_event(monkeypatch):
    conn, events = _stub_edit_endpoint(monkeypatch, _copy_asset())
    body = marketing_material_api.AssetCopyEditRequest(updates={"title": "只看可核验事实"})
    response = asyncio.run(marketing_material_api.api_edit_asset_copy(42, _request(), body))
    assert response["ok"] is True
    assert response["content"]["title"] == "只看可核验事实"
    assert response["content"]["cta"] == "领取诊断"  # 未提交字段保持不变
    assert any("UPDATE marketing_material_assets SET content_text" in sql for sql, _ in conn._cursor.executed)
    assert conn.commits == 1
    assert len(events) == 1 and events[0]["event_type"] == "geo_asset_copy_edited"
    assert events[0]["payload"]["fields"] == ["title"]
    # 审计五要素(SSOT 2026-07-23 §2.4):操作人/时间/原因/规则版本/原内容/修改内容
    payload = events[0]["payload"]
    for key in ("operator_user_id", "at", "reason", "rule_version", "content_before", "content_after"):
        assert key in payload
    assert payload["content_before"]["title"] == "先看真实诊断"
    assert payload["content_after"]["title"] == "只看可核验事实"


def test_asset_copy_edit_ad_law_rejection_writes_nothing(monkeypatch):
    """Owner 2026-07-22 分层:编辑保存只有广告法极限词 422;其余 warning 照常保存。"""
    conn, events = _stub_edit_endpoint(monkeypatch, _copy_asset())
    body = marketing_material_api.AssetCopyEditRequest(updates={"title": "全网第一品牌，先看真实诊断"})
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(marketing_material_api.api_edit_asset_copy(42, _request(), body))
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["code"] == "asset_copy_edit_rejected"
    assert excinfo.value.detail["errors"][0]["code"] == "forbidden_claim"
    assert "第一品牌" in str(excinfo.value.detail["errors"][0]["detail"])
    # 五问合同(SSOT 2026-07-23 §6):422 detail 五字段齐
    for key in ("message", "reason", "repair_hint", "actions", "rule_version"):
        assert excinfo.value.detail.get(key) not in (None, "", [])
    assert conn._cursor.executed == [] and events == []  # 失败不假成功:不落库、不审计成功

    # 联系方式提醒(原硬拦)→ 保存成功,warnings 透传
    conn, events = _stub_edit_endpoint(monkeypatch, _copy_asset())
    body = marketing_material_api.AssetCopyEditRequest(updates={"title": "加我微信 13800138000 详聊"})
    response = asyncio.run(marketing_material_api.api_edit_asset_copy(42, _request(), body))
    assert response["ok"] is True
    assert response["qa"]["passed"] is True
    codes = {warning["code"] for warning in response["qa"]["warnings"]}
    assert "contact_forbidden_when_none" in codes
    assert any("UPDATE marketing_material_assets SET content_text" in sql for sql, _ in conn._cursor.executed)
    assert len(events) == 1 and events[0]["event_type"] == "geo_asset_copy_edited"


def test_asset_copy_edit_rejects_images_and_unknown_fields(monkeypatch):
    image_asset = {**_copy_asset(), "asset_kind": "bundle_item", "bundle_slot": "professional_poster:image:professional_poster"}
    conn, events = _stub_edit_endpoint(monkeypatch, image_asset)
    body = marketing_material_api.AssetCopyEditRequest(updates={"title": "换一句"})
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(marketing_material_api.api_edit_asset_copy(42, _request(), body))
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail == "asset_copy_only_editable"

    _stub_edit_endpoint(monkeypatch, _copy_asset())
    bad = marketing_material_api.AssetCopyEditRequest(updates={"script": "不属于本渠道"})
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(marketing_material_api.api_edit_asset_copy(42, _request(), bad))
    assert excinfo.value.status_code == 422
    assert str(excinfo.value.detail).startswith("asset_copy_field_not_editable")
    assert conn._cursor.executed == [] and events == []


def test_asset_edit_route_is_classified_for_members():
    from services.organization_route_contract import match_member_geo_route

    route = match_member_geo_route("PATCH", "/api/marketing/assets/42")
    assert route is not None
    assert route.capability == "materials.write"
    assert route.resource_kind == "marketing_material" and route.operation == "update"


def test_material_image_save_strips_exif_and_transposes(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    raw = _jpeg_with_exif()
    stored = material_storage.save_material_image("u-exif", raw, "photo.jpg", private=False)
    data = Path(stored["storage_key"]).read_bytes()
    with Image.open(io.BytesIO(data)) as image:
        assert not image.getexif()  # EXIF/元数据已剥离
        assert image.size == (80, 120)  # 方向已摆正
    assert (stored["width"], stored["height"]) == (80, 120)
    assert data != raw  # 原图字节不再原样保留

    with pytest.raises(ValueError, match="material_image_invalid"):
        material_storage.save_material_image("u-exif", b"not-an-image", "broken.png", private=False)


def test_qr_reference_save_strips_metadata_and_preserves_payload(monkeypatch, tmp_path):
    qrcode = pytest.importorskip("qrcode")
    monkeypatch.setenv("MARKETING_PRIVATE_UPLOAD_ROOT", str(tmp_path / "marketing-private"))
    from PIL.PngImagePlugin import PngInfo

    from services.marketing.quality_assurance import validate_qr_reference

    metadata = PngInfo()
    metadata.add_text("Make", "TestMake")
    metadata.add_text("internal_path", "/app/data/secret")
    buffer = io.BytesIO()
    qrcode.make("https://example.test/exif-qr").convert("RGB").save(buffer, format="PNG", pnginfo=metadata)
    raw = buffer.getvalue()
    original = validate_qr_reference(raw)
    stored = material_storage.save_qr_reference("u-exif", raw)
    assert stored["reference_id"].endswith(".png")
    _, data = material_storage.qr_reference_data_uri("u-exif", stored["reference_id"], stored["sha256"])
    with Image.open(io.BytesIO(data)) as image:
        assert not image.getexif()
        assert not getattr(image, "text", {})  # 文本元数据块也被剥离
    assert validate_qr_reference(data)["payload_hash"] == original["payload_hash"]


# ---------------------------------------------------------------------------
# 对抗审查 round-1 修复回归
# ---------------------------------------------------------------------------
def test_v1_three_key_evidence_snapshot_still_passes_live_check(monkeypatch):
    """#3:v1 冻结的三键 facts 快照(无 provenance)不得因形状升级被永久拒。

    部署前冻结的存量任务,facts 是 {key,label,value};live 重建是四键。
    投影比对让两代快照都过;来源真被篡改仍三重拒。
    """
    row = {"id": 55, "brand_id": 101, "total_score": 71, "level": "B", "created_at": None}
    monkeypatch.setattr(db.connection, "get_connection", lambda: _FakeConn(row))
    # 手工构造 v1 形状快照:三键 facts + 旧口径 snapshot_hash(整快照 digest)
    v1_snapshot = {
        "source_type": "diagnosis",
        "source_id": 55,
        "brand_id": 101,
        "source_created_at": None,
        "facts": [
            {"key": "geo_score", "label": "GEO 诊断评分", "value": 71},
            {"key": "diagnosis_level", "label": "诊断等级", "value": "B"},
        ],
        "source_note": "依据本次已发布诊断结果，生成时已冻结",
        "chat_authorized": False,
        "organization_id": None,
        "created_by_membership_id": None,
    }
    from services.marketing.evidence import _digest

    v1_snapshot["snapshot_hash"] = _digest(v1_snapshot)
    require_frozen_evidence_live(v1_snapshot)  # 存量 v1 任务交付权恢复:放行

    monkeypatch.setattr(
        db.connection, "get_connection",
        lambda: _FakeConn({**row, "total_score": 72}),
    )
    with pytest.raises(ValueError, match="evidence_changed_since_freeze"):
        require_frozen_evidence_live(v1_snapshot)  # 篡改仍拒,不弱化三重拒
    monkeypatch.setattr(db.connection, "get_connection", lambda: _FakeConn(None))
    with pytest.raises(ValueError, match="evidence_revoked_or_deleted"):
        require_frozen_evidence_live(v1_snapshot)  # 删除/撤回仍拒


def test_visual_style_is_validated_frozen_and_reaches_prompt(monkeypatch):
    """#1:visual_style 白名单校验 → 冻结进 geo_snapshot → build_visual_brief 取值。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from auth import brand_access
    from services.marketing import strategy_teachers

    monkeypatch.setattr(marketing_material_api, "_require_user", lambda _request: {"id": 1})
    monkeypatch.setattr(marketing_material_api, "_uid", lambda _user: 1)
    monkeypatch.setattr(marketing_material_api, "_principal_user_id", lambda _request, _user: 1)
    monkeypatch.setattr(brand_access, "require_brand_access", lambda _request, _brand_id: None)
    monkeypatch.setattr(
        strategy_teachers, "resolve_teacher",
        lambda **_kwargs: {"teacher_id": "shu", "version": "1.0.0", "name": "舒老师"},
    )
    monkeypatch.setattr(marketing_material_api, "_clean_strategy", lambda value, _teacher, _brief: value)
    monkeypatch.setattr(marketing_material_api, "_freeze_service_brand", lambda _p: {"name": "OmniRank"})
    captured = {}

    async def prepare(**kwargs):
        captured["geo_snapshot"] = kwargs["geo_snapshot"]
        return {"status": "generating", "job_id": 1}

    monkeypatch.setattr(geo_factory, "prepare_geo_package_job", prepare)
    app = FastAPI()
    app.include_router(marketing_material_api.router)
    client = TestClient(app)
    response = client.post("/api/marketing/content-packages", json={
        "request_id": "visual-style-001",
        "brief": "向服务商老板推广真实 GEO 诊断",
        "quick_task": "promote_geo",
        "strategy": {"audience": "服务商老板"},
        "channels": ["moments"],
        "evidence": {"source_type": "none"},
        "contact": {"mode": "none"},
        "visual_style": "bright",
    })
    assert response.status_code == 200, response.text
    assert captured["geo_snapshot"]["visual_style"] == "bright"  # 不再静默丢弃

    # 非法风格:422 明确拒,不静默回落
    invalid = client.post("/api/marketing/content-packages", json={
        "request_id": "visual-style-002",
        "brief": "向服务商老板推广真实 GEO 诊断",
        "quick_task": "promote_geo",
        "strategy": {"audience": "服务商老板"},
        "channels": ["moments"],
        "visual_style": "neon_cyberpunk",
    })
    assert invalid.status_code == 422
    assert invalid.json()["detail"] == "visual_style_invalid"

    # 冻结值真的进 prompt;默认值保持原硬编码口径(v1 行为兼容)
    styled = content_center.visual_prompt(
        slot={"slot": "moments", "size": "1:1"}, channel="moments",
        strategy={"audience": "服务商", "single_action": "领取诊断", "single_value": "证据"},
        content={"title": "先看诊断"}, evidence={"facts": []}, trend={"used": False},
        contact={"mode": "none", "text": "", "qr_reference": None}, brand={},
        visual_style="bright",
    )
    document = json.loads(styled.split("\n", 1)[1])
    assert document["visual_style"] == content_center.VISUAL_STYLES["bright"]
    assert "明亮活泼" in document["visual_style"]
    default = content_center.visual_prompt(
        slot={"slot": "moments", "size": "1:1"}, channel="moments",
        strategy={"audience": "服务商", "single_action": "领取诊断", "single_value": "证据"},
        content={"title": "先看诊断"}, evidence={"facts": []}, trend={"used": False},
        contact={"mode": "none", "text": "", "qr_reference": None}, brand={},
    )
    default_doc = json.loads(default.split("\n", 1)[1])
    assert "Linear 风格" in default_doc["visual_style"]  # 旧硬编码口径 = tech_dark 默认
    with pytest.raises(ValueError, match="visual_style_invalid"):
        content_center.normalize_visual_style("neon_cyberpunk")


def test_saved_images_strip_icc_profile(monkeypatch, tmp_path):
    """#8:PNG 重编码不得从 im.info 回写 ICC(设备型号/软件名/版权字段)。"""
    monkeypatch.chdir(tmp_path)
    image = Image.new("RGB", (60, 40), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", icc_profile=b"FAKE-ICC-DEVICE-PROFILE")
    raw = buffer.getvalue()
    with Image.open(io.BytesIO(raw)) as probe:
        assert probe.info.get("icc_profile") == b"FAKE-ICC-DEVICE-PROFILE"  # 输入确实带 ICC

    stored = material_storage.save_material_image("u-icc", raw, "photo.png", private=False)
    data = Path(stored["storage_key"]).read_bytes()
    assert b"iCCP" not in data  # iCCP chunk 不再回写
    with Image.open(io.BytesIO(data)) as saved:
        assert not saved.info.get("icc_profile")

    # 打码渲染同口径
    from services.marketing import redaction

    rendered = redaction.render_redacted(raw, [], strength="medium")
    assert b"iCCP" not in rendered
    with Image.open(io.BytesIO(rendered)) as saved:
        assert not saved.info.get("icc_profile")
