"""2026-07-23 Deploy 审核退回 4 个发布阻断的判别性测试(ALLOWED→DENIED 对照)。

每个阻断都给出一对用例:授权/代际有效时放行(ALLOWED),撤权/换代际/跨客户后
硬拒(DENIED)——证明修复真的判别,而不是双向放行或双向硬拒。另覆盖三个非主
阻断:违法词双重否定还原、OCR 异常不透前端、图片像素上限。

DB-free mock 风格(同 test_external_review_2026_07_23.py):不连真实 PG、不打真实 LLM。
"""
import asyncio
import io
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from api import marketing_material_api
from auth import brand_access
from db import marketing_db
from services.marketing import deal_intake, geo_factory, guards
from services.marketing import redaction as deal_redaction


# ---------------------------------------------------------------------------
# 共用桩
# ---------------------------------------------------------------------------
def _stub_user(monkeypatch, user_id: int = 9):
    monkeypatch.setattr(marketing_material_api, "_require_user", lambda _request: {"id": user_id})
    monkeypatch.setattr(marketing_material_api, "_uid", lambda _user: user_id)
    monkeypatch.setattr(marketing_material_api, "_principal_user_id", lambda _request, _user: user_id)


def _fake_request(identity=None, user_id: int = 9):
    return SimpleNamespace(
        state=SimpleNamespace(
            user={"id": user_id},
            organization_identity=identity,
            organization_request_id="discriminative-test",
        )
    )


def _identity(*, organization_id=7, membership_id=55, membership_status="active",
              organization_status="active", actor_kind="member",
              membership_version=1, assignment_version=1,
              organization_authority_version=1):
    from services.organization_contract import IdentityContext

    return IdentityContext(
        request_id="discriminative-test", authenticated_user_id=9, principal_user_id=42,
        payer_user_id=42, actor_kind=actor_kind,
        organization_id=organization_id, organization_status=organization_status,
        membership_status=membership_status, membership_id=membership_id,
        membership_version=membership_version, assignment_version=assignment_version,
        organization_authority_version=organization_authority_version,
    )


def _client_with_identity(identity):
    app = FastAPI()

    @app.middleware("http")
    async def _inject_identity(request, call_next):
        request.state.organization_identity = identity
        return await call_next(request)

    app.include_router(marketing_material_api.router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# 阻断 1:草稿读取的品牌分配 live 复核(撤分配 → 403/404 fail-closed)
# ---------------------------------------------------------------------------
def _org_draft(**patch):
    draft = {
        "id": 31, "owner_user_id": 9, "brand_id": 501,
        "organization_id": 7, "created_by_membership_id": 55,
        "request_id": "deal-draft-001", "form_jsonb": {}, "materials_jsonb": [],
        "sheet_jsonb": {}, "status": "draft", "created_at": None, "updated_at": None,
    }
    draft.update(patch)
    return draft


def test_blocker1_draft_read_allowed_while_brand_assignment_live(monkeypatch):
    """ALLOWED:组织绑定草稿 + 当前 actor 对绑定品牌仍有 live 授权 → 放行。"""
    draft = _org_draft()
    monkeypatch.setattr(marketing_db, "get_deal_draft", lambda _id: draft)
    monkeypatch.setattr(brand_access, "require_brand_access", lambda _request, _brand_id: None)
    loaded = marketing_material_api._load_owned_deal_draft(31, 9, _fake_request(identity=_identity()))
    assert loaded["id"] == 31


def test_blocker1_draft_read_denied_after_brand_assignment_revoked(monkeypatch):
    """DENIED:同一草稿、组织成员身份仍有效,但客户(品牌)分配已撤销 → 403/404。"""
    draft = _org_draft()
    monkeypatch.setattr(marketing_db, "get_deal_draft", lambda _id: draft)

    def revoked(_request, _brand_id):
        raise HTTPException(status_code=404, detail="资源不存在")

    monkeypatch.setattr(brand_access, "require_brand_access", revoked)
    with pytest.raises(HTTPException) as excinfo:
        marketing_material_api._load_owned_deal_draft(31, 9, _fake_request(identity=_identity()))
    assert excinfo.value.status_code in {403, 404}


def test_blocker1_personal_draft_brand_revocation_also_fail_closed(monkeypatch):
    """DENIED(个人身份链):个人草稿绑了品牌,授权撤销后同样 fail-closed。"""
    draft = _org_draft(organization_id=None, created_by_membership_id=None)
    monkeypatch.setattr(marketing_db, "get_deal_draft", lambda _id: draft)

    def revoked(_request, _brand_id):
        raise HTTPException(status_code=404, detail="资源不存在")

    monkeypatch.setattr(brand_access, "require_brand_access", revoked)
    with pytest.raises(HTTPException) as excinfo:
        marketing_material_api._load_owned_deal_draft(31, 9, _fake_request(identity=None))
    assert excinfo.value.status_code in {403, 404}


# ---------------------------------------------------------------------------
# 阻断 2:幂等锚纳入创建成员代际 + replay 返回前 live 复核
# ---------------------------------------------------------------------------
def _create_draft_client(monkeypatch, *, draft, created, captured=None, identity=None):
    _stub_user(monkeypatch)

    def fake_create_or_get(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        return draft, created

    monkeypatch.setattr(marketing_db, "create_or_get_deal_draft", fake_create_or_get)
    return _client_with_identity(identity)


def test_blocker2_request_hash_binds_creator_membership(monkeypatch):
    """组织身份创建:request_hash 纳入 created_by_membership_id;个人身份不纳入。"""
    captured: dict = {}
    client = _create_draft_client(
        monkeypatch, draft=_org_draft(brand_id=None), created=True,
        captured=captured, identity=_identity(membership_id=55),
    )
    response = client.post("/api/marketing/deal-drafts", json={"request_id": "discrim-b2-0001", "form": {}})
    assert response.status_code == 200, response.text
    assert captured["request_hash"] == geo_factory.canonical_hash({
        "owner_user_id": 9, "brand_id": None, "form": {},
        "organization_id": 7, "created_by_membership_id": 55,
        "hash_version": "deal-draft-org-member-v1",
    })
    # 个人身份同样使用版本化配方，但不含成员键；旧配方仅作为服务端
    # 精确兼容 replay，不能继续成为新草稿的权威哈希。
    captured.clear()
    client = _create_draft_client(
        monkeypatch, draft=_org_draft(brand_id=None, organization_id=None, created_by_membership_id=None),
        created=True, captured=captured, identity=None,
    )
    response = client.post("/api/marketing/deal-drafts", json={"request_id": "discrim-b2-0002", "form": {}})
    assert response.status_code == 200, response.text
    assert captured["request_hash"] == geo_factory.canonical_hash({
        "owner_user_id": 9, "brand_id": None, "form": {},
        "organization_id": None, "hash_version": "deal-draft-org-member-v1",
    })


def test_blocker2_replay_allowed_for_same_live_member(monkeypatch):
    """ALLOWED:同一在职成员用原 request_id replay → 原样返回旧草稿(幂等语义不变)。"""
    client = _create_draft_client(
        monkeypatch, draft=_org_draft(brand_id=None), created=False,
        identity=_identity(membership_id=55),
    )
    response = client.post("/api/marketing/deal-drafts", json={"request_id": "deal-draft-001", "form": {}})
    assert response.status_code == 200, response.text
    assert response.json()["created"] is False
    assert response.json()["draft"]["id"] == 31


def test_blocker2_replay_denied_for_rejoined_member(monkeypatch):
    """DENIED:退会后重入会(新 membership 行)用原 request_id replay → 403,不返回旧草稿。"""
    client = _create_draft_client(
        monkeypatch, draft=_org_draft(brand_id=None), created=False,
        identity=_identity(membership_id=99),  # 旧草稿 created_by_membership_id=55
    )
    response = client.post("/api/marketing/deal-drafts", json={"request_id": "deal-draft-001", "form": {}})
    assert response.status_code == 403
    assert "draft" not in response.json()


def test_blocker2_replay_denied_after_brand_assignment_revoked(monkeypatch):
    """DENIED:同一成员但草稿绑定客户分配已撤销 → replay 403/404,不返回旧草稿。"""
    draft = _org_draft()
    _stub_user(monkeypatch)
    monkeypatch.setattr(marketing_db, "create_or_get_deal_draft", lambda **_kwargs: (draft, False))

    def revoked(_request, _brand_id):
        raise HTTPException(status_code=404, detail="资源不存在")

    monkeypatch.setattr(brand_access, "require_brand_access", revoked)
    client = _client_with_identity(_identity(membership_id=55))
    response = client.post("/api/marketing/deal-drafts", json={"request_id": "deal-draft-001", "form": {}})
    assert response.status_code in {403, 404}
    assert "draft" not in response.json()


# ---------------------------------------------------------------------------
# 阻断 3:job 读取/retry 统一 live 复核(代际不一致:读 404、retry 403)
# ---------------------------------------------------------------------------
def _org_geo_snapshot(**patch):
    identity = _identity()
    snapshot = {
        "request_id": "discrim-b3-0001", "request_hash": "h" * 64,
        "channels": ["moments"], "moments_layout": "single",
        "strategy": {"audience": "服务商", "single_action": "领取诊断", "single_value": "真实证据"},
        "teacher": {"teacher_id": "shu", "version": "1.0.0", "name": "舒老师"},
        "evidence": {"source_type": "none", "facts": []},
        "contact": {"mode": "none", "text": "", "qr_reference": None},
        "brand": {"name": ""}, "angles": {"moments": None},
        "actor_snapshot": {
            "authenticated_user_id": 9, "principal_user_id": 42,
            "organization_id": 7, "actor_kind": "member",
            "membership_id": 55, "membership_version": 1,
            "organization_authority_version": 1, "assignment_version": 1,
            "authority_version": identity.authority_version,
        },
    }
    snapshot.update(patch)
    return snapshot


def _org_job(geo, **patch):
    job = {
        "id": 41, "user_id": 9, "brand_id": None, "status": "succeeded",
        "error_summary": "", "resolution": "1k",
        "input_fields_jsonb": {"_geo": geo},
    }
    job.update(patch)
    return job


def test_blocker3_job_read_allowed_for_same_live_member(monkeypatch):
    """ALLOWED:冻结 actor 快照与当前 live 身份同代际 → 读取放行。"""
    geo = _org_geo_snapshot()
    monkeypatch.setattr(marketing_db, "get_job", lambda _id: _org_job(geo))
    monkeypatch.setattr(marketing_db, "list_assets", lambda _id: [])
    result = asyncio.run(marketing_material_api.api_job(41, _fake_request(identity=_identity())))
    assert result["ok"] is True and result["job"]["job_id"] == 41


def test_blocker3_job_read_404_for_rejoined_member(monkeypatch):
    """DENIED:重入会员工(新 membership 行)读旧代际 job → 404(不暴露存在性)。"""
    geo = _org_geo_snapshot()
    monkeypatch.setattr(marketing_db, "get_job", lambda _id: _org_job(geo))
    monkeypatch.setattr(marketing_db, "list_assets", lambda _id: [])
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(marketing_material_api.api_job(41, _fake_request(identity=_identity(membership_id=99))))
    assert excinfo.value.status_code == 404


def test_blocker3_job_read_404_after_assignment_generation_bump(monkeypatch):
    """DENIED:客户分配代际变化(assignment_version 已推进)→ 读取 404。"""
    geo = _org_geo_snapshot()
    monkeypatch.setattr(marketing_db, "get_job", lambda _id: _org_job(geo))
    monkeypatch.setattr(marketing_db, "list_assets", lambda _id: [])
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(marketing_material_api.api_job(
            41, _fake_request(identity=_identity(assignment_version=2)),
        ))
    assert excinfo.value.status_code == 404


def _retry_body():
    return marketing_material_api.RetryComponentsRequest(
        request_id="retry-disc-00001", component_ids=["moments:copy"],
    )


def test_blocker3_retry_allowed_for_same_live_member(monkeypatch):
    """ALLOWED:同代际成员局部重试放行(到 prepare 为止,后续链路 mock)。"""
    geo = _org_geo_snapshot()
    monkeypatch.setattr(marketing_db, "get_job", lambda _id: _org_job(geo))

    async def prepare(**_kwargs):
        return {"status": "generating", "job_id": 42}

    monkeypatch.setattr(geo_factory, "prepare_geo_package_job", prepare)
    result = asyncio.run(marketing_material_api.api_retry_content_components(
        41, _fake_request(identity=_identity()), _retry_body(),
    ))
    assert result["ok"] is True and result["job_id"] == 42


def test_blocker3_retry_403_for_rejoined_member(monkeypatch):
    """DENIED:重入会员工对旧代际 job retry → 403。"""
    geo = _org_geo_snapshot()
    monkeypatch.setattr(marketing_db, "get_job", lambda _id: _org_job(geo))
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(marketing_material_api.api_retry_content_components(
            41, _fake_request(identity=_identity(membership_id=99)), _retry_body(),
        ))
    assert excinfo.value.status_code == 403


def test_blocker3_personal_job_zero_overhead(monkeypatch):
    """个人身份 job(actor_snapshot.organization_id IS NULL)短路,不做任何组织复核。"""
    marketing_material_api._require_live_job_org_authority(
        _fake_request(identity=None), {"actor_snapshot": {"organization_id": None}},
        revoked_status=404,
    )
    marketing_material_api._require_live_job_org_authority(
        _fake_request(identity=None), {}, revoked_status=403,
    )


def test_blocker3_prepare_actor_snapshot_compared_same_standard(monkeypatch):
    """geo_factory 执行/恢复入口同口径:重入会成员带旧代际快照进 prepare → 拒绝。"""
    geo = _org_geo_snapshot()
    with pytest.raises(ValueError, match="organization_actor_generation_mismatch"):
        asyncio.run(geo_factory.prepare_geo_package_job(
            user_id=9, brand_id=None, geo_snapshot=geo, request_hash="h" * 64,
            organization_identity=_identity(membership_id=99),
        ))
    # ALLOWED 对照:同代际快照顺利穿过 actor 复核,进入既有幂等/replay 分支。
    monkeypatch.setattr(
        marketing_db, "create_or_get_material_job",
        lambda **_kwargs: ({"id": 1, "status": "succeeded", "error_summary": ""}, False),
    )
    monkeypatch.setattr(marketing_db, "list_assets", lambda _id: [])
    prepared = asyncio.run(geo_factory.prepare_geo_package_job(
        user_id=9, brand_id=None, geo_snapshot=geo, request_hash="h" * 64,
        organization_identity=_identity(),
    ))
    assert prepared["job_id"] == 1


# ---------------------------------------------------------------------------
# 阻断 4:showcase_deal 强制同客户(跨客户 → 422 人话)
# ---------------------------------------------------------------------------
def _showcase_client(monkeypatch, *, draft, captured_access=None):
    _stub_user(monkeypatch)
    monkeypatch.setattr(marketing_db, "get_deal_draft", lambda _id: draft)
    monkeypatch.setattr(marketing_db, "add_event", lambda **_kwargs: None)

    def access(_request, brand_id):
        if captured_access is not None:
            captured_access.append(int(brand_id))

    monkeypatch.setattr(brand_access, "require_brand_access", access)
    from services.marketing import strategy_teachers

    monkeypatch.setattr(
        strategy_teachers, "resolve_teacher",
        lambda **_kwargs: {"teacher_id": "shu", "version": "1.0.0", "name": "舒老师"},
    )
    monkeypatch.setattr(marketing_material_api, "_freeze_service_brand", lambda _p: {"name": ""})

    async def prepare(**_kwargs):
        return {"status": "generating", "job_id": 77}

    monkeypatch.setattr(geo_factory, "prepare_geo_package_job", prepare)
    return _client_with_identity(None)


def _showcase_draft(**patch):
    draft = _org_draft(organization_id=None, created_by_membership_id=None)
    draft["sheet_jsonb"] = deal_intake.empty_sheet()
    draft.update(patch)
    return draft


def _showcase_payload(**overrides):
    payload = {
        "request_id": "discrim-b4-0001",
        "brief": "晒一笔真实成交记录",
        "quick_task": "showcase_deal",
        "channels": ["deal_poster"],
        "deal_draft_id": 31,
        "contact": {"mode": "none"},
    }
    payload.update(overrides)
    return payload


def test_blocker4_cross_customer_deal_generation_422(monkeypatch):
    """DENIED:A 客户成交草稿 + body 指定 B 客户 → 422 人话,不生成。"""
    client = _showcase_client(monkeypatch, draft=_showcase_draft(brand_id=501))
    response = client.post("/api/marketing/content-packages", json=_showcase_payload(brand_id=502))
    assert response.status_code == 422
    assert "另一个客户" in str(response.json()["detail"])


def test_blocker4_same_customer_allowed(monkeypatch):
    """ALLOWED:body.brand_id 与草稿同客户 → 照常进入生成链路。"""
    client = _showcase_client(monkeypatch, draft=_showcase_draft(brand_id=501))
    response = client.post("/api/marketing/content-packages", json=_showcase_payload(
        request_id="discrim-b4-0002", brand_id=501,
    ))
    assert response.status_code == 200, response.text
    assert response.json()["job_id"] == 77


def test_blocker4_blank_body_brand_falls_back_to_draft_brand(monkeypatch):
    """ALLOWED:body 未带品牌时以草稿绑定客户为准,并真的过了品牌分配 live 复核。"""
    accessed: list = []
    client = _showcase_client(monkeypatch, draft=_showcase_draft(brand_id=501), captured_access=accessed)
    response = client.post("/api/marketing/content-packages", json=_showcase_payload(request_id="discrim-b4-0003"))
    assert response.status_code == 200, response.text
    assert 501 in accessed


# ---------------------------------------------------------------------------
# 非主阻断③:违法词双重否定还原(否定套否定仍硬拦)
# ---------------------------------------------------------------------------
def test_double_negation_illegal_words_restored_to_hard_block():
    """「不反对/不打击/不是不/杜绝/严禁+违法词」仍命中硬拦;正面执法表述放行。"""
    assert guards.legal_pack()["source"] == "config"  # 签发包在场,硬拦口径生效
    for text in ("不反对假冒", "不打击假冒", "不是不打击假冒", "杜绝假冒", "严禁假冒"):
        result = guards.scan_forbidden(text)
        assert result["passed"] is False, text
        assert "假冒" in (result["flags"].get("illegal_content") or []), text
    for text in ("打击假冒", "反对假冒伪劣", "识别假货", "远离假货", "抵制假币流通"):
        result = guards.scan_forbidden(text)
        assert "illegal_content" not in result["flags"], text


# ---------------------------------------------------------------------------
# 非主阻断①:OCR 原始异常不透前端(只透人话概括,细节进服务端日志)
# ---------------------------------------------------------------------------
def test_ocr_error_is_human_summary_raw_exception_stays_server_side(monkeypatch, tmp_path):
    _stub_user(monkeypatch)
    monkeypatch.setenv("MARKETING_PRIVATE_UPLOAD_ROOT", str(tmp_path / "marketing-private"))
    draft = _org_draft(
        brand_id=None, organization_id=None, created_by_membership_id=None,
        materials_jsonb=[{
            "material_id": "m1", "storage_key": "private/u9/m1.png",
            "width": 100, "height": 100, "size_bytes": 10, "original_filename": "m1.png",
            "ocr_text": "", "extracted": False,
        }],
    )
    monkeypatch.setattr(marketing_db, "get_deal_draft", lambda _id: dict(draft))
    saved: dict = {}

    def update(_draft_id, **kwargs):
        saved.update(kwargs)
        return {**draft, "materials_jsonb": kwargs.get("materials", draft["materials_jsonb"])}

    monkeypatch.setattr(marketing_db, "update_deal_draft", update)

    def boom(_ref):
        raise RuntimeError("psycopg2.OperationalError: connection to db.internal:5432 refused")

    monkeypatch.setattr(deal_redaction, "read_private_image", boom)

    async def fake_sheet(**_kwargs):
        return deal_intake.empty_sheet()

    monkeypatch.setattr(deal_intake, "extract_sheet", fake_sheet)
    app = FastAPI()
    app.include_router(marketing_material_api.router)
    client = TestClient(app)
    response = client.post("/api/marketing/deal-drafts/31/analyze")
    assert response.status_code == 200, response.text  # OCR 失败不阻断确认单
    material = next(m for m in response.json()["draft"]["materials"] if m["material_id"] == "m1")
    assert material["ocr_error"] == marketing_material_api._OCR_ERROR_FAILED
    # 原始异常(驱动名/内部地址)永不出服务端响应,只进日志
    assert "psycopg2" not in response.text and "db.internal" not in response.text
    assert "psycopg2" not in str(saved.get("materials"))


# ---------------------------------------------------------------------------
# 非主阻断②:图片上传像素上限(宽/高 ≤ 8192、总像素 ≤ 4000 万,超限 422 人话)
# ---------------------------------------------------------------------------
def _png_bytes(size=(24, 24)):
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _upload_client(monkeypatch, tmp_path):
    _stub_user(monkeypatch)
    monkeypatch.setenv("MARKETING_PRIVATE_UPLOAD_ROOT", str(tmp_path / "marketing-private"))
    draft = _org_draft(brand_id=None, organization_id=None, created_by_membership_id=None)
    monkeypatch.setattr(marketing_db, "get_deal_draft", lambda _id: dict(draft))
    monkeypatch.setattr(
        marketing_db, "update_deal_draft",
        lambda _id, **kwargs: {**draft, "materials_jsonb": kwargs.get("materials", [])},
    )
    app = FastAPI()
    app.include_router(marketing_material_api.router)
    return TestClient(app)


class _FakeProbe:
    def __init__(self, size):
        self.size = size

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_deal_material_upload_within_pixel_limits_allowed(monkeypatch, tmp_path):
    client = _upload_client(monkeypatch, tmp_path)
    response = client.post(
        "/api/marketing/deal-drafts/31/materials",
        files=[("files", ("chat.png", _png_bytes(), "image/png"))],
    )
    assert response.status_code == 200, response.text
    assert response.json()["added"][0]["material_id"]


def test_deal_material_upload_over_pixel_limits_422(monkeypatch, tmp_path):
    client = _upload_client(monkeypatch, tmp_path)
    for size, marker in (((9000, 100), "8192"), ((7000, 7000), "4000 万")):
        monkeypatch.setattr("PIL.Image.open", lambda *_a, **_k: _FakeProbe(size))
        response = client.post(
            "/api/marketing/deal-drafts/31/materials",
            files=[("files", ("big.png", _png_bytes(), "image/png"))],
        )
        assert response.status_code == 422, (size, response.text)
        assert marker in str(response.json()["detail"]), (size, response.text)
