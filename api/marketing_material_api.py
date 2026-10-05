"""
营销物料工厂 用户端 API · 2026-07-04 · 前缀 /api/marketing

用户花自己的算力做自己的物料(机器闸门制,无人工审批 · §0)。三处入口共用:
  ① 品牌营销资料页「营销物料」tab · ② C 端对话新工具(confirm 卡 → /generate)· ③ 服务商素材库/批量

端点:
  GET  /templates                     · 场景模板卡(结构化表单预填 client_profiles)
  POST /generate                      · 生成(守卫+freeze 同步 → 后台出图 → 立即返 job_id · R4 异步化)
  GET  /jobs/{id}                     · 任务状态 + 成品资产(前端轮询点)
  GET  /my-materials                  · 我的物料库
  POST /materials/{asset_id}/confirm  · 用户预览自审确认外发(rights_confirmed=1)
  POST /materials/{asset_id}/report   · 举报违规物料(机器闸门的人工兜底 · R5)
  POST /batch-generate                · 服务商为多个绑定客户批量生成(异步 · 逐 brand 归属校验)
  GET  /pricing                       · 物料档价目(算力口径 · 只读)
安全:brand_id 一律过 _verify_brand_ownership(owner_user_id 硬校验 · R5);job/asset 按 user_id 隔离。

计费全走现有 freeze/commit/release(billing 红线 diff=0);对客称"算力"禁"积分"。
"""
import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Response
from pydantic import BaseModel, Field

from db import marketing_db
from services.marketing import material_factory, material_storage
from services.marketing.prompt_composer import PROMPT_COMPOSER_VERSION

logger = logging.getLogger("Marketing-Material-API")
router = APIRouter(prefix="/api/marketing", tags=["营销物料工厂"])
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


def _require_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def _uid(user: dict) -> int:
    return int(user.get("id") or user.get("user_id") or 0)


def _principal_user_id(request: Request, user: dict) -> int:
    # 薄对象判别（2026-07-23 统一 R3 §八.1）：直接在薄对象上读
    # principal_user_id 会 AttributeError 变 500；薄则回退服务端实时解析。
    identity = _request_org_identity(request)
    return int(identity.principal_user_id) if identity is not None else _uid(user)


def _require_frozen_actor_scope(request: Request, geo: dict) -> None:
    """Keep organization-scoped jobs inside their immutable tenant boundary."""
    frozen_org_id = ((geo.get("actor_snapshot") or {}).get("organization_id"))
    if frozen_org_id is None:
        return
    identity = getattr(request.state, "organization_identity", None)
    if identity is None or int(identity.organization_id) != int(frozen_org_id):
        # Do not disclose that a request/job identity exists in another tenant.
        raise HTTPException(status_code=404, detail="任务不存在")


def _resolve_live_org_identity(request: Request, frozen_org_id: int, *, not_found_detail: str):
    """组织身份 live 解析(共用段):请求态身份缺失/不匹配时以服务端实时解析为准。

    解析不到或组织不匹配 → 404(跨租户不暴露存在性);组织合同异常按原
    http_status 映射(撤权/暂停等 fail-closed)。
    """
    identity = getattr(request.state, "organization_identity", None)
    if identity is not None:
        if not _identity_has_contract(identity, _ORG_IDENTITY_GENERATION_ATTRS):
            # 契约不全的身份对象(如旧测试夹具/薄代理)视同缺失,回退服务端实时解析——
            # 直接在薄对象上读字段会 AttributeError 变 500,语义上必须 fail-closed。
            identity = None
        elif int(identity.organization_id) != int(frozen_org_id):
            # 请求态身份属于其他组织:跨租户实锤,无需服务端解析,直接不暴露存在性。
            raise HTTPException(status_code=404, detail=not_found_detail)
    if identity is None:
        try:
            from db.organization_db import resolve_identity

            identity = resolve_identity(
                _uid(_require_user(request)),
                request_id=str(getattr(request.state, "organization_request_id", "geo-live-recheck")),
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise _organization_http_error(request, exc) from exc
        if identity is None or int(identity.organization_id) != int(frozen_org_id):
            raise HTTPException(status_code=404, detail=not_found_detail)
    return identity


def _require_live_job_org_authority(request: Request, geo: dict, *, revoked_status: int,
                                    not_found_detail: str = "任务不存在",
                                    revoked_detail: str = "组织或成员权限代际已变化") -> None:
    """组织身份 job 的统一 live 复核(2026-07-23 Deploy 阻断 3 收口)。

    冻结 actor_snapshot 与当前 live 身份逐项比对:membership 存在性 + 成员状态
    + 组织/成员/品牌分配代际。代际不一致(撤权后重入会 = 新 membership 行)即拒:
    读取端点传 revoked_status=404(不暴露旧代际任务存在性),重试端点传 403。
    个人身份 job(organization_id 为 NULL)零额外开销短路,行为与历史一致。
    """
    snapshot = (geo or {}).get("actor_snapshot") or {}
    frozen_org_id = snapshot.get("organization_id")
    if frozen_org_id is None:
        return
    identity = _resolve_live_org_identity(request, int(frozen_org_id), not_found_detail=not_found_detail)
    if str(identity.membership_status or "") != "active":
        raise HTTPException(status_code=revoked_status, detail=revoked_detail)
    try:
        identity.require_active_organization()
    except Exception as exc:
        raise _organization_http_error(request, exc) from exc

    def _int_mismatch(field: str) -> bool:
        frozen = snapshot.get(field)
        if frozen is None:
            return False
        current = getattr(identity, field, None)
        if current is None:
            # 冻结有值但 live 身份缺该字段:代际无法对齐,fail-closed 视为不一致。
            return True
        return int(frozen) != int(current)

    if snapshot.get("actor_kind") and str(identity.actor_kind) != str(snapshot["actor_kind"]):
        raise HTTPException(status_code=revoked_status, detail=revoked_detail)
    if snapshot.get("authority_version") is not None and str(snapshot["authority_version"]) != str(identity.authority_version):
        raise HTTPException(status_code=revoked_status, detail=revoked_detail)
    if _int_mismatch("organization_authority_version"):
        raise HTTPException(status_code=revoked_status, detail=revoked_detail)
    if _int_mismatch("membership_id"):
        raise HTTPException(status_code=revoked_status, detail=revoked_detail)
    if _int_mismatch("membership_version"):
        raise HTTPException(status_code=revoked_status, detail=revoked_detail)
    if _int_mismatch("assignment_version"):
        raise HTTPException(status_code=revoked_status, detail=revoked_detail)


def _load_delivery_asset(asset_id: int, user_id: int) -> Optional[dict]:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT a.*,j.user_id,j.brand_id,j.input_fields_jsonb,
                   j.status AS job_status,j.error_summary AS job_error_summary
            FROM marketing_material_assets a
            JOIN marketing_material_jobs j ON j.id=a.job_id
            WHERE a.id=%s AND j.user_id=%s
            """,
            (int(asset_id), int(user_id)),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _require_live_delivery_authority(request: Request, asset: dict) -> None:
    """Recheck tenant generation, brand assignment and evidence before delivery."""
    # 薄对象判别（2026-07-23 统一 R3 §八.1）：下载/交付链先判别回填，薄对象
    # 不得 AttributeError→500（500 禁止，403/404 稳定）。
    _request_org_generation_identity(request)
    geo = (asset.get("input_fields_jsonb") or {}).get("_geo") or {}
    if not geo:
        return
    # Tenant non-disclosure precedes delivery-state diagnostics: the same user
    # may hold jobs in multiple organization scopes during a session.
    _require_frozen_actor_scope(request, geo)
    job_status = str(asset.get("job_status") or "")
    job_error = str(asset.get("job_error_summary") or "")
    if job_status != "succeeded" or job_error not in {"", "partial_free_release"}:
        # A materialized row is only durable progress until the unique billing
        # anchor and package terminal state have converged. Never let a guessed
        # same-user asset id bypass the public-package delivery gate.
        raise HTTPException(status_code=409, detail="素材尚未完成结算")
    if not bool(asset.get("is_final")) or int(asset.get("publish_allowed") or 0) != 1:
        raise HTTPException(status_code=409, detail="素材当前不可交付")
    snapshot = geo.get("actor_snapshot") or {}
    identity = getattr(request.state, "organization_identity", None)
    if snapshot.get("organization_id") is not None:
        if identity is None:
            raise HTTPException(status_code=404, detail="素材不存在")
        try:
            from db.organization_db import resolve_identity

            live_identity = resolve_identity(
                _uid(_require_user(request)),
                request_id=str(getattr(request.state, "organization_request_id", "material-delivery")),
            )
            if live_identity is None or int(live_identity.organization_id) != int(snapshot["organization_id"]):
                raise HTTPException(status_code=403, detail="组织身份已撤销")
            live_identity.require_active_organization()
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise _organization_http_error(request, exc) from exc
        identity = live_identity
        if snapshot.get("actor_kind") and str(identity.actor_kind) != str(snapshot["actor_kind"]):
            raise HTTPException(status_code=403, detail="任务身份已变化")
        if str(identity.membership_status or "") != "active":
            raise HTTPException(status_code=403, detail="成员身份已暂停")
        if snapshot.get("authority_version") is not None and str(snapshot["authority_version"]) != str(identity.authority_version):
            raise HTTPException(status_code=403, detail="组织或成员权限代际已变化")
        if snapshot.get("organization_authority_version") is not None and int(snapshot["organization_authority_version"]) != int(identity.organization_authority_version or 0):
            raise HTTPException(status_code=403, detail="组织权限代际已变化")
        if int(snapshot.get("membership_id") or 0) != int(identity.membership_id or 0):
            raise HTTPException(status_code=403, detail="成员身份已撤销")
        if snapshot.get("membership_version") is not None and int(snapshot["membership_version"]) != int(identity.membership_version or 0):
            raise HTTPException(status_code=403, detail="成员权限代际已变化")
        if snapshot.get("assignment_version") is not None and int(snapshot["assignment_version"]) != int(identity.assignment_version or 0):
            raise HTTPException(status_code=403, detail="客户分配代际已变化")
    from auth.brand_access import require_brand_access
    from services.marketing.evidence import require_frozen_evidence_live

    if asset.get("brand_id"):
        _require_brand_contract_identity(request)
        require_brand_access(request, int(asset["brand_id"]))
    try:
        require_frozen_evidence_live(geo.get("evidence") or {})
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _organization_http_error(request: Request, exc: Exception) -> HTTPException:
    from services.organization_contract import OrganizationError

    if isinstance(exc, OrganizationError):
        request_id = getattr(request.state, "organization_request_id", "unknown")
        return HTTPException(status_code=exc.http_status, detail=exc.as_detail(request_id))
    if str(exc) == "material_request_id_conflict":
        return HTTPException(status_code=409, detail="material_request_id_conflict")
    logger.warning("GEO content package prepare failed: %s", type(exc).__name__)
    return HTTPException(status_code=400, detail="content_package_prepare_failed")


# ---------------------------------------------------------------------------
# 薄身份判别（2026-07-23 统一 R3 §八.1）
#
# 中间件正常路径写入 request.state.organization_identity 的一定是完整的
# IdentityContext；旧测试夹具/薄代理对象可能只带部分字段。直接在薄对象上
# 读属性会 AttributeError 变 500——每个入口在使用身份前必须按自己的消费
# 合同做完整性判别：不完整即视同缺失，回退服务端 resolve_identity 实时
# 解析（唯一权威来源），解析结果回填 request.state，下游 require_brand_access
# / draft CRUD / job / recovery / retry / download 全部只接触完整身份。
# ---------------------------------------------------------------------------

# 代际比对合同：冻结 actor_snapshot 逐项比对所需的全部字段。
_ORG_IDENTITY_GENERATION_ATTRS = (
    "organization_id", "membership_status", "membership_id", "membership_version",
    "actor_kind", "authority_version", "organization_authority_version",
    "assignment_version", "require_active_organization",
)
# 品牌分配复核合同：require_brand_access → assigned_brand_ids 直接解引用的字段。
_ORG_IDENTITY_BRAND_ATTRS = ("is_owner", "principal_user_id", "organization_id", "membership_id")
# 写入口全合同：代际 + 品牌 + 成员态（创建/重试链还会经 _principal_user_id 与
# claim_live_charge 解引用 principal/actor 字段）。
_ORG_IDENTITY_FULL_ATTRS = _ORG_IDENTITY_GENERATION_ATTRS + (
    "principal_user_id", "is_owner", "is_member",
)


def _identity_has_contract(identity, attrs) -> bool:
    return identity is not None and all(hasattr(identity, attr) for attr in attrs)


def _resolve_thin_request_identity(request: Request, *, attrs):
    """薄对象判别共用段：不满足本入口消费合同的身份视同缺失并回退服务端解析。

    解析失败按组织合同原状态码映射（撤权/暂停 fail-closed 403）；解析不到
    成员行返回 None（个人身份路径）；解析成功回填 request.state，后续
    require_brand_access 等直接读 request.state 的调用方只见完整身份。
    """
    identity = getattr(request.state, "organization_identity", None)
    if identity is None or _identity_has_contract(identity, attrs):
        return identity
    try:
        from db.organization_db import resolve_identity

        resolved = resolve_identity(
            _uid(_require_user(request)),
            request_id=str(getattr(request.state, "organization_request_id", "geo-thin-identity-recheck")),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _organization_http_error(request, exc) from exc
    request.state.organization_identity = resolved
    return resolved


def _request_org_identity(request: Request):
    """写入口薄身份判别（全合同）：draft 创建 / content package 创建 / retry。"""
    return _resolve_thin_request_identity(request, attrs=_ORG_IDENTITY_FULL_ATTRS)


def _request_org_generation_identity(request: Request):
    """读/恢复入口薄身份判别（代际合同）：job 轮询 / 下载 / 物料库。"""
    return _resolve_thin_request_identity(request, attrs=_ORG_IDENTITY_GENERATION_ATTRS)


def _require_brand_contract_identity(request: Request) -> None:
    """require_brand_access 前的薄身份兜底：缺品牌复核字段即服务端解析回填。"""
    _resolve_thin_request_identity(request, attrs=_ORG_IDENTITY_BRAND_ATTRS)


def _org_generation_hash_block(identity) -> Optional[dict]:
    """job 幂等锚的组织代际块（2026-07-23 统一 R3 §八.4）。

    request_hash/frozen snapshot 绑 organization generation + membership
    id/generation + assignment/capability generation + brand authority 代际：
    退会重入会（新 membership 行）/撤权换代际后复用旧 request_id → hash
    失配 → 结构化 409，绝不返回旧代际 job 或泄漏旧资产。个人身份无组织
    代际可绑，返回 None，hash 配方与历史完全一致。"""
    if identity is None:
        return None
    return {
        "hash_version": "geo-content-org-generation-v1",
        "organization_id": int(identity.organization_id),
        "membership_id": int(identity.membership_id or 0) or None,
        "membership_version": identity.membership_version,
        "capability_version": getattr(identity, "capability_version", None),
        "assignment_version": identity.assignment_version,
        "organization_authority_version": identity.organization_authority_version,
    }


class GenerateRequest(BaseModel):
    template_id: Optional[int] = None
    material_kind: str = Field(default="poster")
    input_fields: dict = Field(default_factory=dict)
    brand_id: Optional[int] = None
    size: Optional[str] = None
    resolution: Optional[str] = None
    purpose: str = Field(default="", max_length=32)  # 营销目的直通文案 prompt(P0-B 批)


class BatchGenerateRequest(BaseModel):
    template_id: Optional[int] = None
    material_kind: str = Field(default="poster")
    brand_ids: list[int] = Field(default_factory=list)
    base_input_fields: dict = Field(default_factory=dict)
    purpose: str = Field(default="", max_length=32)  # 与单生成同口径直通文案 prompt


@router.get("/templates", summary="场景模板卡")
async def api_templates(request: Request):
    _require_user(request)
    return {"ok": True, "templates": marketing_db.list_templates(active_only=True)}


@router.get("/pricing", summary="物料档价目(算力口径)")
async def api_pricing(request: Request):
    _require_user(request)
    codes = ("mktg_moments_copy", "mktg_poster_basic", "mktg_poster_pro",
             "mktg_bundle_std", "mktg_bundle_pro")
    out = []
    try:
        from db.wallet_db import get_feature_pricing
        for c in codes:
            try:
                p = get_feature_pricing(c)
                out.append({"feature_code": c, "cost_points": p["cost_points"],
                            "feature_name": p["feature_name"]})
            except Exception as _fb_exc:  # noqa: BLE001
                # [WO_240] 同族、不同后果:这里没有"取到一个错的数",
                #   而是**这一档从对客价目里整个消失**。客户看到的是一张
                #   **短了一项**的价目表,而短了几项没有任何痕迹 ——
                #   比一个错的数更难发现,因为屏幕上没有任何异常。
                #   本单只让它出声;要不要改成"占位/整张拒绝"是产品口径,已报 Review。
                from services.fallback_observability import fired as _fb_fired
                _fb_fired(feature=c,
                          where="api/marketing_material_api.py:api_pricing",
                          key="cost_points", used=None,
                          reason="item_dropped",
                          detail=type(_fb_exc).__name__)
    except Exception as e:  # noqa: BLE001
        logger.warning("[material] pricing 读取失败: %s", e)
    return {"ok": True, "pricing": out}


# ============================================================================
# GEO acquisition content center (new primary experience)
# ============================================================================
class InterpretBriefRequest(BaseModel):
    brief: str = Field(min_length=4, max_length=500)
    quick_task: str = Field(default="promote_geo", max_length=40)
    teacher_id: Optional[str] = Field(default=None, max_length=80)
    teacher_version: Optional[str] = Field(default=None, max_length=40)


class TeacherPreferenceRequest(BaseModel):
    teacher_id: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=40)
    reason: str = Field(default="", max_length=200)


class EvidenceSelection(BaseModel):
    source_type: str = Field(default="none", max_length=40)
    diagnosis_id: Optional[int] = None
    facts: list = Field(default_factory=list)


class ContactSelection(BaseModel):
    mode: str = Field(default="none", max_length=20)
    text: str = Field(default="", max_length=120)
    qr_reference: Optional[dict] = None


class ContentPackageRequest(BaseModel):
    request_id: str = Field(min_length=8, max_length=128)
    brief: str = Field(min_length=4, max_length=500)
    quick_task: str = Field(default="promote_geo", max_length=40)
    strategy: dict = Field(default_factory=dict)
    teacher_id: Optional[str] = Field(default=None, max_length=80)
    teacher_version: Optional[str] = Field(default=None, max_length=40)
    channels: list[str] = Field(default_factory=lambda: ["professional_poster", "moments"])
    brand_id: Optional[int] = None
    deal_draft_id: Optional[int] = None
    evidence: EvidenceSelection = Field(default_factory=EvidenceSelection)
    contact: ContactSelection = Field(default_factory=ContactSelection)
    associate_recent_trend: bool = False
    resolution: str = Field(default="1k", max_length=8)
    moments_layout: str = Field(default="single", max_length=16)
    visual_style: Optional[str] = Field(default=None, max_length=40)
    anonymize_brand: bool = False
    only_components: list[str] = Field(default_factory=list)
    parent_job_id: Optional[int] = None
    # 显式确认动作(2026-07-23 外部审查 P1-2):只有用户真的看过提醒并点了
    # 「知道了，继续生成」,前端才置 true;后端仅此时记「用户确认继续」审计。
    warnings_acknowledged: bool = False


def _content_package_rejections() -> dict:
    """结构化 422 合同(SSOT §6 五问):code + message + reason + repair_hint + actions
    + rule_version。rule_version 运行时取法律禁止目录包当前版本(热加载跟随)。"""
    from services.marketing import guards as _guards

    return {
        "published_diagnosis_evidence_not_found": {
            "code": "PUBLISHED_DIAGNOSIS_EVIDENCE_NOT_FOUND",
            "message": "这个客户暂时没有可用于推广的已发布诊断。",
            "reason": "只有当前账号仍有权限、且已经发布的诊断，才能作为对外内容的真实证据。",
            "repair_hint": "AI 可以不引用诊断数字，改用常青口径生成这份内容",
            "actions": [
                {"id": "select_published_diagnosis", "label": "去选择已发布诊断证据"},
                {"id": "use_evergreen", "label": "改为不依赖诊断证据的常青内容"},
            ],
            "rule_version": _guards.legal_pack_version(),
        },
    }


def _content_package_rejection(exc: ValueError):
    """Return a stable, user-actionable contract without weakening evidence QA."""
    raw_code = str(exc)
    detail = _content_package_rejections().get(raw_code)
    if detail is not None:
        return detail
    return raw_code


_STRATEGY_FIELDS = (
    "audience", "audience_status", "action_resistance", "human_problem",
    "core_angle", "single_value", "evidence_statement", "single_action",
)


def _clean_strategy(value: dict, teacher: dict, brief: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError("strategy_required")
    result = {}
    for key in _STRATEGY_FIELDS:
        text = " ".join(str(value.get(key) or "").split()).strip()
        if not text:
            raise ValueError(f"strategy_field_required:{key}")
        result[key] = text[:240]
    result.update({
        "source_brief": " ".join(brief.split())[:500],
        "teacher_id": teacher["teacher_id"],
        "teacher_version": teacher["version"],
        "source": "user_confirmed",
    })
    from services.marketing import guards
    from services.marketing.content_center import scan_geo_claims
    check = scan_geo_claims(" ".join(str(result[key]) for key in _STRATEGY_FIELDS))
    # SSOT 2026-07-23:422 只留法律禁止目录包命中项(广告法极限词/违法内容);
    # 承诺词/灰词等由 _strategy_soft_warnings 落 warning 透传,不阻断。
    if guards.hard_flag_hits(check.get("flags")):
        raise ValueError("strategy_contains_forbidden_claim")
    return result


def _strategy_soft_warnings(strategy: dict) -> list[dict]:
    """策略层承诺词/灰词等软命中 → warning 透传(提醒不阻断)。"""
    from services.marketing import guards
    from services.marketing.content_center import scan_geo_claims, warning_entry

    check = scan_geo_claims(" ".join(str(strategy.get(key) or "") for key in _STRATEGY_FIELDS))
    rest = {
        kind: hits for kind, hits in (check.get("flags") or {}).items()
        if kind not in guards.HARD_FLAG_KINDS
    }
    if not rest:
        return []
    unsigned_hits = rest.pop(guards.UNSIGNED_CATALOG_FLAG, None)
    warnings: list[dict] = []
    if unsigned_hits:
        # 法律目录包未签发:命中词项单列提醒(未签发目录无权硬拦),不与承诺词混同。
        warnings.append(warning_entry(guards.UNSIGNED_CATALOG_FLAG, detail=unsigned_hits))
    if rest:
        warnings.append(warning_entry("strategy_promise_claim", detail=rest))
    return warnings


def _freeze_service_brand(principal_user_id: int) -> dict:
    """营销专用品牌解析(Owner 2026-07-22 品牌轴心裁决)。

    直接读该用户「品牌信息」whitelabel 设置行的 company_name + logo_url:
    - company_name 非空即生效(填了就是用户的品牌,无需 OEM 授权/解锁);
    - 未填则零品牌留白(name/logo 全空,视觉 prompt 与 QA 的空品牌跳过路径接管);
    - 绝不回落 OmniRank/全域上榜 平台兜底。
    刻意不走 resolve_branding_context 的 OEM 门禁/surface 矩阵(营销成品是
    服务商给自己用,不是 customer/agent 公开面)。读取失败按未配置静默降级,
    不阻断生成。logo_url 仍过 is_safe_public_whitelabel_logo_url(防账号路径
    泄漏,与公开面同口径);不安全/缺失的 logo 静默降级为纯文字品牌名。
    """
    from db.connection import get_connection
    from services.public_whitelabel import is_safe_public_whitelabel_logo_url

    blank = {
        "name": "", "product_name": "", "logo_url": "", "brand_color": "",
        "slogan": "", "display_scope": "none", "source": "whitelabel_settings",
    }
    row: dict = {}
    try:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT company_name, product_name, logo_url, slogan, brand_color
                FROM whitelabel_settings WHERE user_id = %s
                """,
                (int(principal_user_id),),
            )
            fetched = cursor.fetchone()
            row = dict(fetched) if fetched else {}
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[marketing-brand] whitelabel 读取失败,按未配置留白: %s", exc)
        return dict(blank)
    company_name = str(row.get("company_name") or "").strip()
    if not company_name:
        return dict(blank)
    logo_url = str(row.get("logo_url") or "").strip()
    return {
        "name": company_name[:120],
        "product_name": str(row.get("product_name") or company_name)[:120],
        "logo_url": logo_url[:500] if is_safe_public_whitelabel_logo_url(logo_url) else "",
        "brand_color": str(row.get("brand_color") or "")[:40],
        "slogan": str(row.get("slogan") or "")[:160],
        "display_scope": "owner_brand",
        "source": "whitelabel_settings",
    }


def _freeze_deal_snapshot(draft: dict, *, operator_user_id: int) -> dict:
    """原始输入/操作人/版本/来源快照冻结进 job input_fields_jsonb(审计可溯)。"""
    from services.marketing import redaction as deal_redaction

    materials = list(draft.get("materials_jsonb") or [])
    redacted = []
    for material in materials:
        ref = deal_redaction.redacted_ref_for_generation(material)
        if ref:
            redacted.append({"material_id": str(material.get("material_id") or ""), **ref})
    form = {
        key: value for key, value in (draft.get("form_jsonb") or {}).items()
        if not str(key).startswith("_")
    }
    return {
        "draft_id": int(draft["id"]),
        "version": str(draft.get("updated_at") or draft.get("created_at") or ""),
        "operator_user_id": int(operator_user_id),
        "form_snapshot": form,
        "sheet_snapshot": draft.get("sheet_jsonb") or {},
        "redacted_materials": redacted,
        "source_inputs": {
            "has_voice_transcript": bool(str(form.get("voice_transcript") or "").strip()),
            "material_count": len(materials),
            "ocr_extracted_count": sum(1 for material in materials if material.get("extracted")),
        },
    }


@router.get("/content-center/bootstrap", summary="GEO 获客内容中心合同")
async def api_content_center_bootstrap(request: Request):
    user = _require_user(request)
    from services.marketing.content_center import CHANNEL_CONTRACTS, QUICK_TASKS, VISUAL_STYLES
    from services.marketing.strategy_teachers import get_default_preference, list_teachers

    principal = _principal_user_id(request, user)
    # 品牌轴心实际口径(Owner 2026-07-22):前端据此提示"已配置=展示公司名/
    # 未配置=成品不含品牌信息",绝不再暗示平台兜底。
    service_brand = _freeze_service_brand(principal)
    return {
        "ok": True,
        "quick_tasks": QUICK_TASKS,
        "channel_contracts": CHANNEL_CONTRACTS,
        "teachers": list_teachers(),
        "default_teacher": get_default_preference(principal),
        "trend_provider_available": False,
        "contact_modes": ["none", "text", "qr"],
        "moments_layouts": ["single", "grid"],
        "visual_styles": VISUAL_STYLES,
        "prompt_composer_version": PROMPT_COMPOSER_VERSION,
        "service_brand": {
            "configured": bool(str(service_brand.get("name") or "").strip()),
            "name": str(service_brand.get("name") or ""),
            "has_logo": bool(str(service_brand.get("logo_url") or "").strip()),
        },
    }


@router.get("/teachers", summary="可用策略导师")
async def api_strategy_teachers(request: Request):
    user = _require_user(request)
    from services.marketing.strategy_teachers import get_default_preference, list_teachers

    principal = _principal_user_id(request, user)
    return {"ok": True, "teachers": list_teachers(), "default": get_default_preference(principal)}


@router.put("/teacher-preference", summary="切换服务商默认策略导师")
async def api_teacher_preference(request: Request, body: TeacherPreferenceRequest):
    user = _require_user(request)
    identity = getattr(request.state, "organization_identity", None)
    if identity is not None and identity.is_member:
        raise HTTPException(status_code=403, detail="员工可使用导师，但只有服务商 Owner 可以修改默认值")
    from services.marketing.strategy_teachers import set_default_preference

    try:
        teacher = set_default_preference(
            principal_user_id=_principal_user_id(request, user), teacher_id=body.teacher_id,
            version=body.version, actor_user_id=_uid(user), reason=body.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "teacher": teacher}


@router.post("/interpret", summary="一句话结构化理解")
async def api_interpret_brief(request: Request, body: InterpretBriefRequest):
    user = _require_user(request)
    from services.marketing.content_center import QUICK_TASKS, interpret_brief
    from services.marketing import product_facts as product_facts_mod
    from services.marketing.strategy_teachers import resolve_teacher

    if body.quick_task not in QUICK_TASKS:
        raise HTTPException(status_code=422, detail="quick_task_invalid")
    try:
        teacher = resolve_teacher(
            teacher_id=body.teacher_id, version=body.teacher_version,
            principal_user_id=_principal_user_id(request, user),
        )
        # 产品事实 SSOT 注入策略理解:包读取失败静默降级为不注入( enrich 而非依赖),
        # 不阻断用户流程。
        facts_pack = None
        try:
            facts_pack = product_facts_mod.resolve_facts_pack()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[product-facts] interpret 注入降级: %s", exc)
        strategy = await interpret_brief(
            body.brief, teacher, facts_pack=facts_pack, quick_task=body.quick_task,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # 提醒预览(2026-07-23 外部审查 P1-2):创建前把策略软提醒透给前端,
    # 有提醒时生成按钮必须先变成显式确认动作(「知道了，继续生成」)。
    warnings: list[dict] = []
    try:
        warnings = _strategy_soft_warnings(strategy)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[interpret] 策略软提醒预览降级: %s", exc)
    return {"ok": True, "strategy": strategy, "teacher": teacher, "warnings": warnings}


@router.get("/product-facts", summary="产品事实库(只读 SSOT)")
async def api_product_facts(request: Request):
    """读当前产品事实包:版本+条数+全部条目。不做在线编辑(后续任务)。"""
    _require_user(request)
    from services.marketing import product_facts as product_facts_mod

    try:
        pack = product_facts_mod.resolve_facts_pack()
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "pack": product_facts_mod.pack_summary(pack)}


@router.post("/qr-reference", summary="上传并验证二维码参考图")
async def api_qr_reference(request: Request, file: UploadFile = File(...)):
    user = _require_user(request)
    raw = await file.read(5 * 1024 * 1024 + 1)
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(status_code=415, detail="二维码参考必须是图片")
    from services.marketing.quality_assurance import (
        sign_qr_reference,
        validate_qr_reference,
    )
    # 组织身份上传记组织上下文进签名(2026-07-23 外部审查 P1-1 同口径):
    # 撤权/换组织后该凭证在内容包创建与生成读取两侧都校验不过,fail-closed。
    identity = getattr(request.state, "organization_identity", None)
    org_id = int(identity.organization_id) if identity is not None else None
    try:
        validation = validate_qr_reference(raw)
        stored = material_storage.save_qr_reference(f"u{_uid(user)}", raw)
        token = sign_qr_reference(
            user_id=_uid(user), reference_id=stored["reference_id"], payload_hash=validation["payload_hash"],
            organization_id=org_id,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "ok": True,
        "qr_reference": {
            "reference_id": stored["reference_id"],
            "payload_hash": validation["payload_hash"],
            "file_sha256": stored["sha256"],
            "payload_preview": validation["payload_preview"],
            "reference_token": token,
            "organization_id": org_id,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        },
    }


# ============================================================================
# 晒成交草稿(deal showcase intake · Phase 2)
# ============================================================================
class DealDraftCreateRequest(BaseModel):
    request_id: str = Field(min_length=8, max_length=128)
    brand_id: Optional[int] = None
    form: dict = Field(default_factory=dict)


class DealSheetPatchRequest(BaseModel):
    sheet: dict = Field(default_factory=dict)
    reason: str = Field(default="", max_length=200)


class DealRedactRequest(BaseModel):
    material_id: str = Field(min_length=1, max_length=64)
    action: str = Field(min_length=1, max_length=20)
    kind: Optional[str] = Field(default=None, max_length=40)
    box: Optional[list] = None
    region_id: Optional[str] = Field(default=None, max_length=64)
    strength: Optional[str] = Field(default=None, max_length=20)
    reason: str = Field(default="", max_length=200)


_DEAL_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_DEAL_IMAGE_MAX = 15 * 1024 * 1024
# 像素上限(2026-07-23 Deploy 非主阻断②):超出即 422 人话,不进解码/落盘链路。
_DEAL_IMAGE_MAX_DIMENSION = 8192
_DEAL_IMAGE_MAX_PIXELS = 40_000_000
_DEAL_MATERIAL_TOTAL_MAX = 20
_DEAL_MATERIAL_BATCH_MAX = 9
_DEAL_AUDIO_TYPES = {
    "audio/webm", "audio/ogg", "audio/wav", "audio/mp3", "audio/mpeg",
    "audio/mp4", "audio/x-m4a", "audio/aac", "video/webm",
}
_DEAL_AUDIO_EXT = {
    "audio/webm": ".webm", "video/webm": ".webm", "audio/ogg": ".ogg",
    "audio/wav": ".wav", "audio/mp3": ".mp3", "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac",
}
_DEAL_AUDIO_MAX = 25 * 1024 * 1024

# OCR 失败对客人话概括(原始异常只进服务端日志,2026-07-23 Deploy 非主阻断①)。
_OCR_ERROR_VISION_UNAVAILABLE = "图片文字识别服务暂时不可用，请稍后重试"
_OCR_ERROR_FAILED = "这张图片的文字识别没有成功，请重试或改用手动填写"


def _validate_deal_image_pixels(raw: bytes) -> None:
    """图片像素上限校验:只读文件头(lazy),宽/高 ≤ 8192 且总像素 ≤ 4000 万。"""
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(raw)) as probe:
            width, height = probe.size
    except Exception as exc:
        raise HTTPException(status_code=422, detail="素材图片无法解析，请更换文件") from exc
    if width > _DEAL_IMAGE_MAX_DIMENSION or height > _DEAL_IMAGE_MAX_DIMENSION:
        raise HTTPException(
            status_code=422,
            detail=f"素材图片尺寸过大：宽和高都不能超过 {_DEAL_IMAGE_MAX_DIMENSION} 像素，请压缩后再上传",
        )
    if int(width) * int(height) > _DEAL_IMAGE_MAX_PIXELS:
        raise HTTPException(
            status_code=422,
            detail="素材图片像素总量过大（超过 4000 万像素），请压缩后再上传",
        )


def _load_owned_deal_draft(draft_id: int, user_id: int, request: Request) -> dict:
    # 薄对象判别（2026-07-23 统一 R3 §八.1）：draft CRUD 全链共用本收口，
    # 先判别/回填完整身份，再走属主/组织/品牌三道复核。
    _request_org_identity(request)
    draft = marketing_db.get_deal_draft(int(draft_id))
    if not draft or int(draft.get("owner_user_id") or 0) != int(user_id):
        raise HTTPException(status_code=404, detail="晒成交草稿不存在")
    _require_live_draft_org_authority(request, draft)
    _require_draft_brand_live(request, draft)
    return draft


def _require_draft_brand_live(request: Request, draft: dict) -> None:
    """草稿绑定品牌的 live 复核(2026-07-23 Deploy 阻断 1)。

    draft.brand_id 非空时,当前 actor 必须对该 brand 持有 live 授权——复用
    auth/brand_access.require_brand_access 同语义(owner 或有效分配;组织
    分配表每请求实时重读,撤权/分配代际变化即时生效),fail-closed 403/404。
    草稿所有读取/写入端点(GET/PATCH/materials/voice/analyze/redact/file/
    content-packages/retry)都经 _load_owned_deal_draft 收口,私有图片读取同链。
    """
    if draft.get("brand_id"):
        from auth.brand_access import require_brand_access

        require_brand_access(request, int(draft["brand_id"]))


def _require_live_draft_org_authority(request: Request, draft: dict) -> None:
    """组织绑定草稿的 live 复核(2026-07-23 外部审查 P1-1,与交付复核同模式)。

    组织身份创建的草稿绑定 organization_id + created_by_membership_id;仅查
    user_id 会让撤权/离组织成员继续读写原组织私有草稿/素材。此处对绑定草稿
    做实时成员/代际复核,fail-closed:
    - 实时解析不到组织身份或组织不匹配 → 404(跨租户不暴露存在性);
    - 成员/组织不可用(撤权、暂停、离组织)→ 403;
    - 创建成员代际不一致(成员被撤后重入 = 新 membership 行)→ 403。
    个人身份草稿(organization_id 为 NULL)不做任何额外校验,行为不变。
    """
    frozen_org_id = draft.get("organization_id")
    if frozen_org_id is None:
        return
    identity = _resolve_live_org_identity(
        request, int(frozen_org_id), not_found_detail="晒成交草稿不存在",
    )
    if str(identity.membership_status or "") != "active":
        raise HTTPException(status_code=403, detail="成员身份已暂停或撤销")
    try:
        identity.require_active_organization()
    except Exception as exc:
        raise _organization_http_error(request, exc) from exc
    created_by = draft.get("created_by_membership_id")
    if created_by is not None and int(identity.membership_id or 0) != int(created_by):
        # 成员被撤销后重新入会会得到新 membership 行;代际不一致即原创建成员已撤。
        raise HTTPException(status_code=403, detail="创建成员身份已撤销")


def _clean_deal_form(form: dict) -> dict:
    """Whitelist the twelve sheet fields; every value is user-typed text."""
    from services.marketing import deal_intake

    cleaned: dict = {}
    for key, value in (form or {}).items():
        key = str(key)
        if key not in deal_intake.SHEET_FIELDS:
            continue
        text = " ".join(str(value or "").split()).strip()
        if text:
            cleaned[key] = text[:500]
    return cleaned


def _public_redaction(state: dict) -> dict:
    from services.marketing.content_center import warning_entry

    return {
        "auto_regions": list(state.get("auto_regions") or []),
        "manual_regions": list(state.get("manual_regions") or []),
        "restored_ids": list(state.get("restored_ids") or []),
        "strength": str(state.get("strength") or "medium"),
        "status": str(state.get("status") or "none"),
        "detection": str(state.get("detection") or ""),
        # 能力边界明示:头像/签名/公章无自动检测,需人工框选复核
        "manual_required_kinds": list(state.get("manual_required_kinds") or []),
        "manual_reviewed": bool(state.get("manual_reviewed")),
        # Owner 2026-07-22:打码复核提醒(放行不阻断),每条 {code, 人话 message}
        "warnings": [warning_entry(str(code)) for code in (state.get("warnings") or [])],
        "has_preview": bool(state.get("preview_key")),
        "has_output": bool(state.get("output_key")),
    }


def _public_material(material: dict) -> dict:
    """storage_key/preview_key 等内部引用永不出服务端;读取走授权端点。"""
    return {
        "material_id": str(material.get("material_id") or ""),
        "width": material.get("width"),
        "height": material.get("height"),
        "size_bytes": material.get("size_bytes"),
        "original_filename": str(material.get("original_filename") or ""),
        "ocr_text": str(material.get("ocr_text") or ""),
        "extracted": bool(material.get("extracted")),
        "vision_ok": bool(material.get("vision_ok")),
        "ocr_error": str(material.get("ocr_error") or ""),
        "redaction": _public_redaction(material.get("redaction") or {}),
    }


def _public_deal_draft(draft: dict) -> dict:
    form = {
        key: value for key, value in (draft.get("form_jsonb") or {}).items()
        if not str(key).startswith("_")
    }
    return {
        "id": int(draft["id"]),
        "brand_id": draft.get("brand_id"),
        "request_id": draft.get("request_id"),
        "form": form,
        "materials": [_public_material(material) for material in (draft.get("materials_jsonb") or [])],
        "sheet": draft.get("sheet_jsonb") or {},
        "status": draft.get("status"),
        "created_at": draft.get("created_at"),
        "updated_at": draft.get("updated_at"),
    }


@router.post("/deal-drafts", summary="创建晒成交草稿(表单 + request_id 幂等)")
async def api_create_deal_draft(request: Request, body: DealDraftCreateRequest):
    user = _require_user(request)
    if not _REQUEST_ID_RE.fullmatch(str(body.request_id or "")):
        raise HTTPException(status_code=422, detail="request_id_invalid")
    uid = _uid(user)
    # 薄对象判别（2026-07-23 统一 R3 §八.1）：先于 require_brand_access 回填
    # 完整身份,薄对象不得 AttributeError→500。
    identity = _request_org_identity(request)
    if body.brand_id:
        from auth.brand_access import require_brand_access

        require_brand_access(request, body.brand_id)
    form = _clean_deal_form(body.form)
    from services.marketing import geo_factory as _geo_factory

    # 组织身份创建的草稿绑定组织与创建成员(2026-07-23 外部审查 P1-1):
    # 撤权/离组织后 live 复核 fail-closed。组织上下文进 request_hash,
    # 同一 request_id 跨身份复用按 409 冲突而非跨身份 replay。
    org_id = int(identity.organization_id) if identity is not None else None
    membership_id = int(identity.membership_id) if identity is not None and identity.membership_id is not None else None
    legacy_hash_payload = {
        "owner_user_id": uid, "brand_id": body.brand_id, "form": form,
        "organization_id": org_id,
    }
    if membership_id is not None:
        # 2026-07-23 Deploy 阻断 2①:创建成员代际进幂等锚——退会后重入会
        # (新 membership 行)用原 request_id 重放会得到 409,而非取回旧代际草稿。
        legacy_hash_payload["created_by_membership_id"] = membership_id
    # Preserve lost-response replay for drafts created by the immediately
    # preceding deployed recipe. The DB layer accepts this hash only after
    # independently matching owner/brand/org/membership/form.
    legacy_request_hash = _geo_factory.canonical_hash(legacy_hash_payload)
    hash_payload = {
        **legacy_hash_payload,
        "hash_version": "deal-draft-org-member-v1",
    }
    request_hash = _geo_factory.canonical_hash(hash_payload)
    try:
        draft, created = marketing_db.create_or_get_deal_draft(
            owner_user_id=uid, request_id=body.request_id, request_hash=request_hash,
            brand_id=body.brand_id, form=form,
            organization_id=org_id, created_by_membership_id=membership_id,
            compatible_request_hashes=(legacy_request_hash,),
        )
    except ValueError as exc:
        code = str(exc)
        status = 409 if code == "deal_draft_request_id_conflict" else 422
        raise HTTPException(status_code=status, detail=code) from exc
    if not created:
        # 2026-07-23 Deploy 阻断 2②:replay 返回前与读取同口径 live 复核——
        # 撤权/离组织/客户分配撤销 → 403/404,绝不把旧代际草稿交回当前身份。
        _require_live_draft_org_authority(request, draft)
        _require_draft_brand_live(request, draft)
    return {"ok": True, "created": created, "draft": _public_deal_draft(draft)}


@router.get("/deal-drafts/{draft_id}", summary="读取晒成交草稿(属主隔离)")
async def api_get_deal_draft(draft_id: int, request: Request):
    user = _require_user(request)
    draft = _load_owned_deal_draft(int(draft_id), _uid(user), request)
    return {"ok": True, "draft": _public_deal_draft(draft)}


def _discard_added_materials(added: list) -> None:
    """写库失败路径的孤儿清理:已落盘但不会有 DB 引用的素材文件尽力删除。"""
    for material in added:
        try:
            material_storage.delete_material_reference(str(material.get("storage_key") or ""))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[deal] 孤儿素材清理失败 key=%s: %s", material.get("storage_key"), exc)


@router.post("/deal-drafts/{draft_id}/materials", summary="上传成交图片素材(私有存储 · EXIF 剥离)")
async def api_upload_deal_materials(draft_id: int, request: Request, files: list[UploadFile] = File(...)):
    user = _require_user(request)
    uid = _uid(user)
    _load_owned_deal_draft(int(draft_id), uid, request)
    if not files or len(files) > _DEAL_MATERIAL_BATCH_MAX:
        raise HTTPException(status_code=422, detail="materials_count_invalid")
    from services.marketing import redaction as deal_redaction

    payloads: list[tuple[UploadFile, bytes]] = []
    for file in files:
        ext = Path(file.filename or "").suffix.lower()
        if ext not in _DEAL_IMAGE_EXTS or not (file.content_type or "").startswith("image/"):
            raise HTTPException(status_code=415, detail="素材必须是 png/jpg/webp 图片")
        raw = await file.read(_DEAL_IMAGE_MAX + 1)
        if not raw or len(raw) > _DEAL_IMAGE_MAX:
            raise HTTPException(status_code=413, detail="单张素材不能超过 15MB")
        _validate_deal_image_pixels(raw)
        payloads.append((file, raw))
    added = []
    for file, raw in payloads:
        try:
            # 统一重编码(Phase 1 _reencode_stripped):EXIF/元数据剥离,原图字节不落盘。
            stored = material_storage.save_material_image(
                f"u{uid}", raw, file.filename or "material.png", private=True,
            )
        except ValueError as exc:
            _discard_added_materials(added)  # 批次中途失败:已落盘的前序文件一并清理
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        added.append({
            "material_id": uuid.uuid4().hex[:16],
            "storage_key": stored["public_url"],
            "width": stored["width"],
            "height": stored["height"],
            "size_bytes": stored["size_bytes"],
            "sha256": stored["sha256"],
            "original_filename": (file.filename or "")[:120],
            "ocr_text": "",
            "extracted": False,
            "redaction": deal_redaction.default_redaction_state(),
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        })
    updated: dict = {}
    for write_attempt in (1, 2):
        # 乐观锁(updated_at):与 analyze/redact 并发时后到者 409 重取,不静默覆盖。
        draft = _load_owned_deal_draft(int(draft_id), uid, request)
        materials = list(draft.get("materials_jsonb") or [])
        if len(materials) + len(added) > _DEAL_MATERIAL_TOTAL_MAX:
            _discard_added_materials(added)  # 422 前清理:本批文件已落盘但不会入库
            raise HTTPException(status_code=422, detail="materials_total_exceeded")
        materials.extend(added)
        try:
            updated = marketing_db.update_deal_draft(
                int(draft["id"]), materials=materials,
                expected_updated_at=draft.get("updated_at"),
            )
            break
        except ValueError as exc:
            if str(exc) == "deal_draft_update_conflict" and write_attempt == 1:
                continue  # 并发写冲突:重取草稿重放一次(条目幂等重建)
            if str(exc) == "deal_draft_update_conflict":
                _discard_added_materials(added)  # 双冲突 409:已落盘文件无 DB 引用,清理防孤儿
                raise HTTPException(status_code=409, detail="deal_draft_update_conflict") from exc
            raise
    return {
        "ok": True,
        "added": [_public_material(material) for material in added],
        "draft": _public_deal_draft(updated),
    }


@router.post("/deal-drafts/{draft_id}/voice", summary="上传成交语音 → ASR 转写入表单")
async def api_upload_deal_voice(draft_id: int, request: Request, file: UploadFile = File(...)):
    user = _require_user(request)
    uid = _uid(user)
    _load_owned_deal_draft(int(draft_id), uid, request)  # 属主校验;写库前循环内重取
    base_type = (file.content_type or "").split(";")[0].strip()
    if base_type not in _DEAL_AUDIO_TYPES:
        raise HTTPException(status_code=415, detail=f"不支持的音频格式: {file.content_type}")
    raw = await file.read(_DEAL_AUDIO_MAX + 1)
    if not raw or len(raw) > _DEAL_AUDIO_MAX:
        raise HTTPException(status_code=413, detail="音频不能超过 25MB")
    from services.marketing import deal_intake

    result = await deal_intake.transcribe_voice_bytes(raw, suffix=_DEAL_AUDIO_EXT.get(base_type, ".webm"))
    text = str(result.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="语音识别未返回有效文字，请重试或改用表单填写")
    updated: dict = {}
    for write_attempt in (1, 2):
        # 乐观锁(updated_at):与 PATCH/analyze 并发时后到者 409 重取,不静默覆盖。
        draft = _load_owned_deal_draft(int(draft_id), uid, request)
        form = dict(draft.get("form_jsonb") or {})
        form["voice_transcript"] = text[:4000]
        try:
            updated = marketing_db.update_deal_draft(
                int(draft["id"]), form=form,
                expected_updated_at=draft.get("updated_at"),
            )
            break
        except ValueError as exc:
            if str(exc) == "deal_draft_update_conflict" and write_attempt == 1:
                continue  # 并发写冲突:重取草稿重放一次(转写结果幂等)
            if str(exc) == "deal_draft_update_conflict":
                raise HTTPException(status_code=409, detail="deal_draft_update_conflict") from exc
            raise
    return {
        "ok": True,
        "transcript": text,
        "provider": str(result.get("provider") or ""),
        "draft": _public_deal_draft(updated),
    }


@router.post("/deal-drafts/{draft_id}/analyze", summary="OCR 全部素材 + 三路合并 → 成交确认单")
async def api_analyze_deal_draft(draft_id: int, request: Request):
    user = _require_user(request)
    uid = _uid(user)
    _load_owned_deal_draft(int(draft_id), uid, request)
    from services.marketing import deal_intake
    from services.marketing import redaction as deal_redaction
    from tools.vision.image_describe import describe_image_structured

    updated: dict = {}
    sheet: dict = {}
    for write_attempt in (1, 2):
        # 乐观锁(updated_at):与 redact/上传并发时后到者 409 重取,不静默覆盖。
        draft = _load_owned_deal_draft(int(draft_id), uid, request)
        materials = []
        for raw_material in (draft.get("materials_jsonb") or []):
            material = dict(raw_material)
            if not material.get("extracted"):
                try:
                    data, _mime = deal_redaction.read_private_image(str(material.get("storage_key") or ""))
                    vision = await describe_image_structured(data, "deal-material.png")
                    material["ocr_text"] = str((vision or {}).get("ocr_text") or "")[:4000]
                    material["vision_ok"] = bool((vision or {}).get("vision_ok"))
                    # 只有 vision 真成功才标记 extracted:失败可重试,不再永久跳过。
                    material["extracted"] = bool(material["vision_ok"])
                    if not material["vision_ok"]:
                        # ocr_error 对客只透人话概括(2026-07-23 Deploy 非主阻断①),
                        # 内部状态/异常细节只进服务端日志。
                        material["ocr_error"] = _OCR_ERROR_VISION_UNAVAILABLE
                    else:
                        material.pop("ocr_error", None)
                except Exception as exc:  # noqa: BLE001 · OCR 失败不阻断确认单(表单/语音仍在)
                    logger.warning("[deal] 素材 OCR 失败 material=%s: %s", material.get("material_id"), exc)
                    material["vision_ok"] = False
                    material["extracted"] = False
                    # 原始异常文本永不出服务端:对客人话概括,细节留在上方日志。
                    material["ocr_error"] = _OCR_ERROR_FAILED
            materials.append(material)
        form = dict(draft.get("form_jsonb") or {})
        voice_transcript = str(form.pop("voice_transcript", "") or "")
        clean_form = {key: value for key, value in form.items() if not str(key).startswith("_")}
        sheet = await deal_intake.extract_sheet(
            form=clean_form,
            voice_transcript=voice_transcript,
            ocr_texts=[str(material.get("ocr_text") or "") for material in materials],
        )
        # 用户已 confirmed 的字段不被重提取覆盖(顺序操作 lost-update:
        # 先确认后重跑 analyze 时,确认值优先于新提取的 tentative)。
        sheet = deal_intake.merge_sheet_preserving_confirmed(draft.get("sheet_jsonb") or {}, sheet)
        try:
            updated = marketing_db.update_deal_draft(
                int(draft["id"]), materials=materials, sheet=sheet, status="analyzed",
                expected_updated_at=draft.get("updated_at"),
            )
            break
        except ValueError as exc:
            if str(exc) == "deal_draft_update_conflict" and write_attempt == 1:
                continue  # 并发写冲突:重取草稿重放一次(OCR 幂等)
            if str(exc) == "deal_draft_update_conflict":
                raise HTTPException(status_code=409, detail="deal_draft_update_conflict") from exc
            raise
    return {"ok": True, "sheet": sheet, "draft": _public_deal_draft(updated)}


@router.patch("/deal-drafts/{draft_id}", summary="编辑成交确认单字段(编辑即确认 · 审计事件)")
async def api_patch_deal_draft(draft_id: int, request: Request, body: DealSheetPatchRequest):
    user = _require_user(request)
    uid = _uid(user)
    _load_owned_deal_draft(int(draft_id), uid, request)
    from services.marketing import deal_intake

    updates = body.sheet if isinstance(body.sheet, dict) else {}
    unknown = sorted(set(updates) - set(deal_intake.SHEET_FIELDS))
    if unknown:
        raise HTTPException(status_code=422, detail=f"deal_sheet_field_unknown:{','.join(unknown)}")
    if not updates:
        raise HTTPException(status_code=422, detail="deal_sheet_edit_empty")
    updated: dict = {}
    sheet: dict = {}
    sheet_before: dict = {}
    for write_attempt in (1, 2):
        # 乐观锁(updated_at):analyze 同写 sheet 列,后到者 409 重取重放,
        # 不静默覆盖(重放基于最新 sheet 重套本次编辑,编辑语义幂等)。
        draft = _load_owned_deal_draft(int(draft_id), uid, request)
        sheet = dict(draft.get("sheet_jsonb") or {})
        for field in deal_intake.SHEET_FIELDS:
            sheet.setdefault(field, {"value": "", "status": "tentative", "provenance": "customer_asserted"})
        sheet_before = {field: dict(sheet.get(field) or {}) for field in updates}
        for field, raw in updates.items():
            if isinstance(raw, dict):
                value = str(raw.get("value") or "").strip()
                status = str(raw.get("status") or "confirmed")
            else:
                value, status = str(raw or "").strip(), "confirmed"
            sheet[field] = {
                "value": value[: deal_intake._FIELD_LIMITS[field]],
                "status": "tentative" if status == "tentative" else "confirmed",
                "provenance": "customer_asserted",
            }
        try:
            updated = marketing_db.update_deal_draft(
                int(draft["id"]), sheet=sheet,
                expected_updated_at=draft.get("updated_at"),
            )
            break
        except ValueError as exc:
            if str(exc) == "deal_draft_update_conflict" and write_attempt == 1:
                continue  # 并发写冲突:重取草稿重放一次
            if str(exc) == "deal_draft_update_conflict":
                raise HTTPException(status_code=409, detail="deal_draft_update_conflict") from exc
            raise
    from services.marketing.content_center import QA_RULES_VERSION, audit_quintuple

    marketing_db.add_event(
        event_type="geo_deal_sheet_edited", actor_id=uid,
        message=f"编辑成交确认单 draft={int(draft_id)} 字段={','.join(sorted(updates))}",
        payload={
            "draft_id": int(draft_id), "fields": sorted(updates),
            # 审计五要素(SSOT §2.4):操作人/时间/原因/规则版本/原内容/修改内容。
            **audit_quintuple(
                operator_user_id=uid,
                reason=body.reason or "人工编辑成交确认单字段(编辑即确认)",
                rule_version=QA_RULES_VERSION,
                content_before=sheet_before,
                content_after={field: sheet.get(field) for field in updates},
            ),
        },
    )
    return {"ok": True, "sheet": updated.get("sheet_jsonb") or sheet, "draft": _public_deal_draft(updated)}


def _clean_region_box(box) -> list:
    values = [int(v) for v in list(box or [])[:4]]
    if len(values) != 4 or any(v < 0 or v > 20000 for v in values) or values[2] <= 0 or values[3] <= 0:
        raise ValueError("redaction_box_invalid")
    return values


def _render_deal_redaction(uid: int, material: dict, state: dict) -> tuple[str, str]:
    """原图 + 当前生效区域 → 像素化预览/成品;返回 (私有引用, sha256)。"""
    from services.marketing import redaction as deal_redaction

    data, _mime = deal_redaction.read_private_image(str(material.get("storage_key") or ""))
    rendered = deal_redaction.render_redacted(
        data, deal_redaction.active_regions(state),
        strength=str(state.get("strength") or "medium"),
    )
    stored = deal_redaction.save_redacted_image(
        f"u{uid}", rendered, filename=f"deal-redact-{material.get('material_id')}.png",
    )
    return str(stored["public_url"]), str(stored["sha256"])


@router.post("/deal-drafts/{draft_id}/redact", summary="素材打码(auto/add/restore/strength/acknowledge/confirm → 授权预览)")
async def api_redact_deal_material(draft_id: int, request: Request, body: DealRedactRequest):
    user = _require_user(request)
    uid = _uid(user)
    from services.marketing import redaction as deal_redaction

    action = str(body.action or "").strip().lower()
    if action not in deal_redaction.REDACTION_ACTIONS:
        raise HTTPException(status_code=422, detail="redaction_action_invalid")
    material_id = ""
    state: dict = {}
    state_before: dict = {}
    updated: dict = {}
    for write_attempt in (1, 2):
        # 乐观锁(updated_at):与 analyze/上传并发时后到者 409 重取,不静默覆盖。
        draft = _load_owned_deal_draft(int(draft_id), uid, request)
        materials = [dict(material) for material in (draft.get("materials_jsonb") or [])]
        target = next(
            ((index, material) for index, material in enumerate(materials)
             if str(material.get("material_id")) == str(body.material_id)),
            None,
        )
        if target is None:
            raise HTTPException(status_code=404, detail="素材不存在")
        index, material = target
        material_id = str(material.get("material_id"))
        state = {**deal_redaction.default_redaction_state(), **(material.get("redaction") or {})}
        state_before = dict(state)
        try:
            if action == "auto":
                data, _mime = deal_redaction.read_private_image(str(material.get("storage_key") or ""))
                detected = await deal_redaction.auto_detect(
                    data, width=int(material.get("width") or 0), height=int(material.get("height") or 0),
                )
                state["auto_regions"] = detected["regions"]
                state["detection"] = str(detected.get("detection") or "")
                # 能力边界诚实化:avatar/seal/signature 无自动检测,随结果明示。
                state["manual_required_kinds"] = list(detected.get("manual_required_kinds") or [])
                state["manual_reviewed"] = bool(state.get("manual_regions"))
                if detected.get("ocr_text") and not material.get("ocr_text"):
                    material["ocr_text"] = str(detected["ocr_text"])[:4000]
            elif action == "add":
                deal_redaction.add_manual_region(
                    state, kind=str(body.kind or "other"), box=_clean_region_box(body.box),
                )
                state["manual_reviewed"] = True  # 手动框选本身就是人工复核行为
            elif action == "restore":
                deal_redaction.restore_region(state, str(body.region_id or ""))
            elif action == "strength":
                state["strength"] = deal_redaction.normalize_strength(str(body.strength or ""))
            elif action == "acknowledge":
                # 人工已逐图复核(含三类无自动检测区域),不重渲染预览。
                state["manual_reviewed"] = True
            if action in {"auto", "add", "restore", "strength"}:
                key, sha = _render_deal_redaction(uid, material, state)
                state["preview_key"], state["preview_sha256"] = key, sha
                state["status"] = "previewed"
            elif action == "confirm":
                if deal_redaction.confirm_requires_manual_review(state):
                    # Owner 2026-07-22:vision 报隐私风险但自动区域为空不再 422;
                    # confirm 照过,redaction 状态记 warning 提醒人工复核。
                    state_warnings = state.setdefault("warnings", [])
                    if "redaction_manual_review_suggested" not in state_warnings:
                        state_warnings.append("redaction_manual_review_suggested")
                if not state.get("preview_key"):
                    raise ValueError("redaction_preview_required")
                key, sha = _render_deal_redaction(uid, material, state)
                state["output_key"], state["output_sha256"] = key, sha
                state["status"] = "confirmed"
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        material["redaction"] = state
        materials[index] = material
        status = str(draft.get("status") or "draft")
        if action == "confirm":
            status = "confirmed"
        elif status in {"draft", "analyzed"} and action != "acknowledge":
            status = "redacted"
        try:
            updated = marketing_db.update_deal_draft(
                int(draft["id"]), materials=materials, status=status,
                expected_updated_at=draft.get("updated_at"),
            )
            break
        except ValueError as exc:
            if str(exc) == "deal_draft_update_conflict" and write_attempt == 1:
                continue  # 并发写冲突:重取草稿重放一次
            if str(exc) == "deal_draft_update_conflict":
                raise HTTPException(status_code=409, detail="deal_draft_update_conflict") from exc
            raise
    if action == "acknowledge":
        # 「人工确认继续」审计五要素(SSOT §2.4/§6):人工复核确认继续必须留
        # operator/at/reason/rule_version/content_before/content_after。
        from services.marketing.content_center import QA_RULES_VERSION, audit_quintuple

        marketing_db.add_event(
            event_type="geo_deal_redaction_acknowledged", actor_id=uid,
            message=f"人工确认打码复核 draft={int(draft_id)} material={material_id}",
            payload={
                "draft_id": int(draft_id), "material_id": material_id,
                **audit_quintuple(
                    operator_user_id=uid,
                    reason=body.reason or "人工确认已完成打码复核(含三类无自动检测区域),继续",
                    rule_version=QA_RULES_VERSION,
                    content_before={
                        "manual_reviewed": bool(state_before.get("manual_reviewed")),
                        "status": str(state_before.get("status") or "none"),
                    },
                    content_after={
                        "manual_reviewed": bool(state.get("manual_reviewed")),
                        "status": str(state.get("status") or "none"),
                    },
                ),
            },
        )
    return {
        "ok": True,
        "material_id": material_id,
        "redaction": _public_redaction(state),
        "preview_url": (
            f"/api/marketing/deal-drafts/{int(draft_id)}"
            f"/materials/{material_id}/file?variant=preview"
        ),
        "draft": _public_deal_draft(updated),
    }


@router.get("/deal-drafts/{draft_id}/materials/{material_id}/file", summary="授权读取素材原图/打码预览/成品")
async def api_deal_material_file(draft_id: int, material_id: str, request: Request, variant: str = "preview"):
    user = _require_user(request)
    uid = _uid(user)
    draft = _load_owned_deal_draft(int(draft_id), uid, request)
    material = next(
        (item for item in (draft.get("materials_jsonb") or [])
         if str(item.get("material_id")) == str(material_id)),
        None,
    )
    if not material:
        raise HTTPException(status_code=404, detail="素材不存在")
    state = material.get("redaction") or {}
    refs = {
        "original": str(material.get("storage_key") or ""),
        "preview": str(state.get("preview_key") or ""),
        "output": str(state.get("output_key") or state.get("preview_key") or ""),
    }
    if variant not in refs:
        raise HTTPException(status_code=422, detail="variant_invalid")
    ref = refs[variant]
    if not ref:
        raise HTTPException(status_code=404, detail="文件不存在")
    from services.marketing import redaction as deal_redaction

    try:
        data, mime = deal_redaction.read_private_image(ref)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="文件不存在") from exc
    return Response(
        content=data,
        media_type=mime,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'inline; filename="deal-material-{material_id[:12]}.{mime.split("/")[-1]}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/content-packages", summary="生成完整 GEO 获客内容包")
async def api_create_content_package(request: Request, body: ContentPackageRequest):
    user = _require_user(request)
    if not _REQUEST_ID_RE.fullmatch(str(body.request_id or "")):
        raise HTTPException(status_code=422, detail="request_id_invalid")
    uid = _uid(user)
    # 薄对象判别（2026-07-23 统一 R3 §八.1）：先于 require_brand_access /
    # actor_snapshot 冻结回填完整身份,薄对象不得 AttributeError→500。
    identity = _request_org_identity(request)
    from auth.brand_access import require_brand_access
    from services.marketing import geo_factory
    from services.marketing.content_center import (
        CHANNEL_CONTRACTS, QUICK_TASKS, decide_trend, diagnosis_case_evidence_ok,
        normalize_contact, normalize_moments_layout, normalize_visual_style, scan_geo_claims,
    )
    from services.marketing.evidence import freeze_evidence
    from services.marketing.quality_assurance import verify_qr_reference_token
    from services.marketing.strategy_teachers import resolve_teacher

    if body.quick_task not in QUICK_TASKS:
        raise HTTPException(status_code=422, detail="quick_task_invalid")
    channels = list(dict.fromkeys(body.channels))
    if not channels or len(channels) > len(CHANNEL_CONTRACTS) or any(ch not in CHANNEL_CONTRACTS for ch in channels):
        raise HTTPException(status_code=422, detail="channels_invalid")
    if body.resolution not in {"1k", "2k", "4k"}:
        raise HTTPException(status_code=422, detail="resolution_invalid")
    try:
        moments_layout = normalize_moments_layout(body.moments_layout)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        # 白名单校验:非法风格 422,绝不静默丢弃用户选择
        visual_style = normalize_visual_style(body.visual_style)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    only_components = list(dict.fromkeys(body.only_components))
    if body.parent_job_id is not None or only_components:
        raise HTTPException(status_code=422, detail="组件修订只能通过任务重试接口创建")
    if only_components and not set(only_components).issubset(geo_factory.valid_component_ids(channels)):
        raise HTTPException(status_code=422, detail="only_components_invalid")
    if body.brand_id:
        require_brand_access(request, body.brand_id)

    deal_draft = None
    deal_brand_id = body.brand_id
    if body.quick_task == "showcase_deal":
        if not body.deal_draft_id:
            raise HTTPException(status_code=422, detail="deal_draft_required")
        # 属主 + 组织绑定 live 复核(撤权/离组织 fail-closed),404 不暴露存在性。
        deal_draft = _load_owned_deal_draft(int(body.deal_draft_id), uid, request)
        draft_brand_id = deal_draft.get("brand_id")
        if draft_brand_id and body.brand_id and int(draft_brand_id) != int(body.brand_id):
            # 2026-07-23 Deploy 阻断 4:成交草稿与请求品牌必须同客户——跨客户
            # 生成会把 A 客户的成交事实贴到 B 客户内容上(数据血缘红线)。
            raise HTTPException(
                status_code=422,
                detail="这份成交草稿属于另一个客户，不能跨客户生成晒成交内容",
            )
        if deal_brand_id is None and draft_brand_id:
            # body 未带品牌时以草稿绑定品牌为准,同样过品牌分配 live 复核。
            deal_brand_id = int(draft_brand_id)
            require_brand_access(request, deal_brand_id)

    service_brand: dict = {}
    creation_warnings: list[dict] = []
    try:
        teacher = resolve_teacher(
            teacher_id=body.teacher_id, version=body.teacher_version,
            principal_user_id=_principal_user_id(request, user),
        )
        qr = body.contact.qr_reference or {}
        if body.contact.mode == "qr":
            qr_org = qr.get("organization_id")
            qr_org = int(qr_org) if qr_org is not None else None
            current_org = int(identity.organization_id) if identity is not None else None
            if not verify_qr_reference_token(
                user_id=uid, reference_id=str(qr.get("reference_id") or ""), payload_hash=str(qr.get("payload_hash") or ""),
                reference_token=str(qr.get("reference_token") or ""), organization_id=qr_org,
            ):
                raise ValueError("qr_reference_token_invalid")
            if qr_org != current_org:
                # 上传时的组织上下文必须与当前请求身份一致(撤权/换组织 fail-closed)。
                raise ValueError("qr_reference_token_invalid")
        contact = normalize_contact(body.contact.mode, text=body.contact.text, qr_reference=qr)
        if deal_draft is not None:
            # 晒成交:确认单组装 strategy(缺口通用非承诺表述)+ evidence=brand_facts。
            # 品牌自述事实不读诊断、不核验系统 SSOT,绝不被
            # published_diagnosis_evidence_not_found 阻断。
            # anonymize_brand(匿名晒单):客户名类提法服务端掩码后再冻结;
            # 冻结品牌名(含 ASCII)作为字面词一并掩掉,防经确认单原文回流。
            from services.marketing import deal_intake

            sheet = deal_draft.get("sheet_jsonb") or {}
            brand_mask_terms = None
            if body.anonymize_brand:
                # 匿名掩码需要冻结品牌名(含 ASCII)作为字面词;快照复用同一份。
                # 未配置品牌时品牌名空串,掩码词表置 None(不往掩码器塞空词)。
                service_brand = _freeze_service_brand(_principal_user_id(request, user))
                _brand_name = str(service_brand.get("name") or "").strip()
                brand_mask_terms = [_brand_name] if _brand_name else None
            strategy = deal_intake.strategy_from_sheet(
                sheet, anonymize=body.anonymize_brand, mask_terms=brand_mask_terms,
            )
            strategy["teacher_id"] = teacher["teacher_id"]
            strategy["teacher_version"] = teacher["version"]
            from services.marketing import guards as _guards

            check = scan_geo_claims(" ".join(str(strategy[key]) for key in _STRATEGY_FIELDS))
            # SSOT 2026-07-23:只有法律禁止目录包命中项 422;承诺词/灰词落 warning 透传。
            if _guards.hard_flag_hits(check.get("flags")):
                raise ValueError("strategy_contains_forbidden_claim")
            creation_warnings.extend(_strategy_soft_warnings(strategy))
            evidence = freeze_evidence(
                brand_id=deal_brand_id, source_type="brand_facts",
                facts=deal_intake.facts_from_sheet(
                    sheet, anonymize=body.anonymize_brand, mask_terms=brand_mask_terms,
                ),
            )
        else:
            strategy = _clean_strategy(body.strategy, teacher, body.brief)
            creation_warnings.extend(_strategy_soft_warnings(strategy))
            evidence = freeze_evidence(
                brand_id=body.brand_id, source_type=body.evidence.source_type,
                diagnosis_id=body.evidence.diagnosis_id, facts=list(body.evidence.facts or []),
                organization_id=(
                    int(identity.organization_id)
                    if identity is not None else None
                ),
                membership_id=(
                    int(identity.membership_id)
                    if identity is not None and identity.membership_id is not None else None
                ),
            )
        if "diagnosis_case" in channels and not diagnosis_case_evidence_ok(evidence):
            # Owner 2026-07-22:不再 422;放行并按常青口径生成,落 warning 提醒。
            from services.marketing.content_center import warning_entry as _warning_entry

            creation_warnings.append(_warning_entry("diagnosis_case_requires_frozen_evidence"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_content_package_rejection(exc)) from exc

    request_payload = body.model_dump(mode="json") if hasattr(body, "model_dump") else body.dict()
    request_hash_payload = {
        "principal_user_id": _principal_user_id(request, user),
        "actor_user_id": uid,
        "payload": request_payload,
        "teacher": {"teacher_id": teacher["teacher_id"], "version": teacher["version"]},
    }
    org_generation = _org_generation_hash_block(identity)
    if org_generation is not None:
        # job 幂等锚绑组织/成员/分配/能力代际（2026-07-23 统一 R3 §八.4）：
        # 退会重入会（新 membership 行）/代际推进后复用旧 request_id → hash
        # 失配 → 结构化 409，绝不返回旧代际 job 或泄漏旧资产。
        request_hash_payload["authority_generation"] = org_generation
    request_hash = geo_factory.canonical_hash(request_hash_payload)
    # 角度池(Owner:同主题天天发不重样):新包按 request hash 定轮换位,
    # 同一 job 内各渠道角度不重复;角度分配冻结进 geo_snapshot 可溯。
    from services.marketing import content_angles
    from services.marketing import product_facts as product_facts_mod

    angle_rotation = content_angles.rotation_from_request_hash(request_hash)
    assigned_angles = content_angles.assign_angles(channels, rotation=angle_rotation)
    # 产品事实 SSOT:创建时按 quick_task/渠道/角度选相关条目,pack_id+version+
    # 事实子集深拷贝冻结进 geo_snapshot;旧 job 永远按冻结版本与条目生成,
    # 包文件热更新不回流。包读取失败降级为 None( enrich 而非依赖),不阻断生成。
    frozen_facts_pack = None
    try:
        frozen_facts_pack = product_facts_mod.freeze_facts_for_snapshot(
            product_facts_mod.resolve_facts_pack(),
            quick_task=body.quick_task, channels=channels, angles=assigned_angles,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[product-facts] 快照冻结降级(本包不含产品事实): %s", exc)
    geo_snapshot = {
        "contract_version": "geo-content-center/1.0",
        "prompt_composer_version": PROMPT_COMPOSER_VERSION,
        "request_id": body.request_id,
        "request_hash": request_hash,
        "brief": " ".join(body.brief.split())[:500],
        "quick_task": body.quick_task,
        "strategy": strategy,
        "teacher": teacher,
        "channels": channels,
        "angles": assigned_angles,
        "angle_rotation": angle_rotation,
        "product_facts": frozen_facts_pack,
        "evidence": evidence,
        "trend": decide_trend(requested=body.associate_recent_trend),
        "contact": contact,
        "brand": service_brand or _freeze_service_brand(_principal_user_id(request, user)),
        "moments_layout": moments_layout,
        "visual_style": visual_style,
        "anonymize_brand": bool(body.anonymize_brand),
        "deal": (
            _freeze_deal_snapshot(deal_draft, operator_user_id=uid)
            if deal_draft is not None else None
        ),
        "warnings": creation_warnings,
        "only_components": only_components,
        "parent_job_id": body.parent_job_id,
        "actor_snapshot": {
            "authenticated_user_id": uid,
            "principal_user_id": _principal_user_id(request, user),
            "organization_id": int(identity.organization_id) if identity is not None else None,
            "authority_version": identity.authority_version if identity is not None else None,
            "actor_kind": identity.actor_kind if identity is not None else None,
            "membership_id": identity.membership_id if identity is not None else None,
            "membership_version": identity.membership_version if identity is not None else None,
            "organization_authority_version": identity.organization_authority_version if identity is not None else None,
            "assignment_version": identity.assignment_version if identity is not None else None,
        },
    }
    try:
        prepared = await geo_factory.prepare_geo_package_job(
            user_id=uid, brand_id=deal_brand_id if deal_draft is not None else body.brand_id,
            geo_snapshot=geo_snapshot,
            request_hash=request_hash, resolution=body.resolution,
            organization_identity=identity,
        )
    except Exception as exc:
        raise _organization_http_error(request, exc) from exc
    if prepared.get("_ctx"):
        asyncio.create_task(geo_factory.execute_geo_package_job(prepared.pop("_ctx")))
    if creation_warnings and prepared["status"] not in {"failed", "blocked"}:
        # 「warnings 下继续生成」审计五要素(SSOT §2.4/§6)。诚实化口径
        # (2026-07-23 外部审查 P1-2):只有用户真的做过显式确认动作
        # (warnings_acknowledged=true,前端「知道了，继续生成」按钮)才记
        # 「用户确认继续」;否则记系统口径事件,不得声称用户确认过。
        # 观测性写入 fail-soft:审计失败不得拖垮已成功创建的生成请求。
        from services.marketing.content_center import QA_RULES_VERSION, audit_quintuple

        try:
            if body.warnings_acknowledged:
                event_type = "geo_content_package_proceeded_with_warnings"
                message = f"用户看过 {len(creation_warnings)} 条提醒后确认继续生成内容包 job={prepared.get('job_id')}"
                reason = "存在审核提醒，用户确认继续生成"
            else:
                event_type = "geo_content_package_created_with_warnings"
                message = f"带 {len(creation_warnings)} 条提醒创建内容包 job={prepared.get('job_id')}（提醒不阻断，随创建继续）"
                reason = "按提醒不阻断策略随创建继续"
            marketing_db.add_event(
                event_type=event_type, actor_id=uid,
                message=message,
                payload={
                    "job_id": prepared.get("job_id"),
                    **audit_quintuple(
                        operator_user_id=uid,
                        reason=reason,
                        rule_version=QA_RULES_VERSION,
                        content_before=None,
                        content_after={"warnings": creation_warnings},
                    ),
                },
            )
        except Exception:
            logger.warning("geo_content_package warnings audit write failed", exc_info=True)
    return {"ok": prepared["status"] not in {"failed", "blocked"}, **prepared}


def _verify_brand_ownership(brand_id: int, user_id: int) -> bool:
    """[返工 R5] brand 归属硬校验(RBAC 元指令:强制 owner_user_id == current_user_id)。
    服务商为绑定客户生成:客户 brand 本就挂服务商名下(owner_user_id=服务商)——同一条件覆盖。"""
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM brands WHERE id=%s AND owner_user_id=%s", (brand_id, user_id))
        return cur.fetchone() is not None
    except Exception as e:  # noqa: BLE001
        logger.warning("[material] brand 归属校验失败(保守拒): %s", e)
        return False
    finally:
        conn.close()


def _pull_brand_marketing_fields(brand_id: Optional[int], base: dict) -> tuple[dict, list]:
    """反哺:从 client_profiles 拉卖点/联系方式补全结构化字段(用户少填)。
    [返工 R5] 换真实存在的函数(原 get_business_profile_by_brand 不存在=死功能)+键名对齐。
    ⚠️ 调用方必须先过 _verify_brand_ownership(profile_db 红字契约:服务端可信 brand_id)。
    返回 (fields, competitors) —— 竞品名取自结构化画像,进守卫清洗。"""
    fields = dict(base or {})
    competitors: list = []
    if not brand_id:
        return fields, competitors
    try:
        from db.profile_db import get_structured_knowledge_by_brand, get_contact_info_by_brand
        sk = get_structured_knowledge_by_brand(brand_id) or {}
        contact = get_contact_info_by_brand(brand_id) or {}
        if not fields.get("selling_point") and isinstance(sk, dict):
            fields["selling_point"] = str(sk.get("differentiation") or sk.get("products") or "")[:200]
        if not fields.get("contact"):
            fields["contact"] = (contact.get("phone") or contact.get("wechat")
                                 or contact.get("website") or "")
        if not fields.get("title") and contact.get("brand_name"):
            fields.setdefault("title", contact["brand_name"])
        comp = sk.get("competitors") if isinstance(sk, dict) else None
        if isinstance(comp, list):
            competitors = [str(c) for c in comp if c][:10]
    except Exception as e:  # noqa: BLE001
        logger.warning("[material] 拉品牌字段失败: %s", e)
    return fields, competitors


@router.get("/brand-prefill/{brand_id}", summary="一键预填:拉品牌资料的卖点/联系方式(前端填表用)")
async def api_brand_prefill(brand_id: int, request: Request):
    """[前端返工 2026-07-05] 向导"一键填写"数据源:选品牌 → 卖点/联系方式/品牌名灌进表单,用户可改。
    归属硬校验同 generate(防跨租户读他人客户联系方式)。"""
    user = _require_user(request)
    uid = _uid(user)
    if not _verify_brand_ownership(brand_id, uid):
        raise HTTPException(status_code=403, detail="品牌不存在或不属于当前账号")
    fields, _competitors = _pull_brand_marketing_fields(brand_id, {})
    return {"ok": True, "brand_id": brand_id, "fields": fields}


class AiFillRequest(BaseModel):
    brand_id: Optional[int] = None
    purpose: str = Field(default="recharge", max_length=32)
    material_kind: str = Field(default="poster", max_length=16)


# 「AI 帮我写」回落示例(LLM 不可用时永不空手;与前端 EXAMPLES 同源口径 · 无承诺词)
_AI_FILL_FALLBACK: dict = {
    "recharge": {"title": "新用户专享礼", "subtitle": "首充双倍 · 算力加量不加价",
                 "selling_point": "AI 搜索推荐看得见,做成才扣,失败自动退",
                 "contact": "详询您的专属顾问", "date": "即日起至 7 月 31 日"},
    "repurchase": {"title": "老朋友,回来看看", "subtitle": "您的品牌数据又有新变化",
                   "selling_point": "诊断报告已更新,继续优化被 AI 推荐的机会",
                   "contact": "联系您的专属顾问查看详情", "date": "本周内有效"},
    "restock": {"title": "算力库存提醒", "subtitle": "库存偏低,补货享配货加赠",
                "selling_point": "提前备货不断档,客户交付更从容",
                "contact": "登录经营后台即可进货", "date": "本月配货政策见后台"},
    "notice": {"title": "本月活动上新", "subtitle": "充值加赠 · 新功能上线",
               "selling_point": "海报物料 / 朋友圈文案一键生成,获客更省事",
               "contact": "打开 OmniRank 查看活动详情", "date": "活动时间以页面公示为准"},
    # —— [P0-B 批] 替客户的生意做物料(选中客户品牌时的目的集,内容全是客户行业通用口径)——
    "opening": {"title": "开业大吉 · 恭迎莅临", "subtitle": "新店开业 · 好礼相迎",
                "selling_point": "开业当周进店有礼,欢迎老朋友带新朋友",
                "contact": "到店详询", "date": "开业当周有效"},
    "promo": {"title": "本周特惠进行中", "subtitle": "限时优惠 · 数量有限",
              "selling_point": "热销单品直降,先到先得",
              "contact": "详询店内 / 微信", "date": "本周内有效"},
    "invite": {"title": "诚邀您的莅临", "subtitle": "专场活动 · 座位有限",
               "selling_point": "现场交流答疑,更有伴手礼相赠",
               "contact": "回复本条即可报名", "date": "活动时间见下方"},
    "daily": {"title": "今日分享", "subtitle": "一条实用小知识",
              "selling_point": "干货分享,持续为您更新行业实用内容",
              "contact": "关注我,每天一条干货", "date": "每日更新"},
    # —— [P0-B 批] 推广自己的服务(不关联品牌时的目的集)——
    "acquire": {"title": "让 AI 搜索推荐你的品牌", "subtitle": "客户在问 AI,答案里要有你",
                "selling_point": "诊断+优化+监测一站式,做成才扣、失败自动退",
                "contact": "加微信免费聊聊", "date": "本月可约"},
    "callback": {"title": "好久不见,近况如何?", "subtitle": "您的品牌数据有新变化",
                 "selling_point": "上次的优化还有后续空间,来看看最新进展",
                 "contact": "随时找我,一杯茶的时间", "date": "本周有空随时约"},
}
_FILL_LIMITS = {"title": 20, "subtitle": 30, "selling_point": 50, "contact": 40, "date": 20}
_PURPOSE_HUMAN = {"recharge": "吸引新用户完成首次充值", "repurchase": "唤回老客户再次使用或充值",
                  "restock": "提醒服务商补充算力库存", "notice": "告知客户新活动或权益",
                  "opening": "客户门店开业/上新造势", "promo": "客户促销活动宣传",
                  "invite": "邀请顾客参加线下活动", "daily": "日常专业形象内容(轻分享不硬卖)",
                  "acquire": "推广自己的 AI 搜索优化服务获客", "callback": "唤回自己的沉默老客户"}


@router.post("/ai-fill", summary="AI 帮我写:按品牌资料+营销目的现写一套表单文案(免费·fail-soft)")
async def api_ai_fill(request: Request, body: AiFillRequest):
    """[AI 原生返工 2026-07-05 老板拍板] 任何要填的地方都有 AI 代填:
    LLM 按该品牌卖点/联系方式 + 所选目的生成 {title,subtitle,selling_point,contact,date};
    逐字段过守卫(违规弃用),LLM 挂/解析失败回落示例+品牌资料合并——永不空手。"""
    user = _require_user(request)
    uid = _uid(user)
    if body.brand_id and not _verify_brand_ownership(body.brand_id, uid):
        raise HTTPException(status_code=403, detail="品牌不存在或不属于当前账号")
    brand_fields, _comp = _pull_brand_marketing_fields(body.brand_id, {})
    purpose = body.purpose if body.purpose in _AI_FILL_FALLBACK else "recharge"
    fallback = {**_AI_FILL_FALLBACK[purpose]}
    # 品牌资料优先合入回落(联系方式/品牌名是真实资产,示例只是兜底)
    for k in ("title", "selling_point", "contact"):
        if (brand_fields.get(k) or "").strip():
            fallback[k] = brand_fields[k]

    out = dict(fallback)
    source = "fallback"
    try:
        from tools.multi_llm_caller import call_llm_with_fallback
        brand_ctx = "\n".join(f"{k}: {v}" for k, v in brand_fields.items() if v) or "(无品牌资料,按通用口径写)"
        prompt = (
            "你是营销文案师。为一张中文营销海报写 5 个字段,严格返回 JSON(不要多余文字):\n"
            '{"title": "...", "subtitle": "...", "selling_point": "...", "contact": "...", "date": "..."}\n'
            f"营销目的:{_PURPOSE_HUMAN[purpose]}。\n品牌资料:\n{brand_ctx}\n"
            "要求:自然口语不打官腔;对客只说'算力'不说'积分';绝不使用保证/承诺/必上/100%/第一名等承诺或极限词;"
            f"字数上限 标题20/副标题30/卖点50/联系方式40/日期20;联系方式若品牌资料有就原样保留。"
        )
        raw = await call_llm_with_fallback(prompt, verbose=False)
        if raw:
            import json_repair
            parsed = json_repair.loads(raw)
            if isinstance(parsed, dict):
                from services.marketing.guards import scan_forbidden
                for k, limit in _FILL_LIMITS.items():
                    v = str(parsed.get(k) or "").strip()
                    if not v:
                        continue
                    if scan_forbidden(v)["flags"]:  # 任一守卫命中(含 advisory)弃用,保留回落值
                        continue  # 违规字段弃用,保留回落值
                    out[k] = v[:limit]
                source = "ai"
    except Exception as e:  # noqa: BLE001 · AI 挂了绝不挡人,回落即答案
        logger.warning("[material] ai-fill LLM 失败(回落示例): %s", e)
    return {"ok": True, "fields": out, "source": source}


@router.post("/generate", summary="生成物料(机器闸门 + freeze 后异步生成,立即返 job_id)")
async def api_generate(request: Request, body: GenerateRequest):
    user = _require_user(request)
    uid = _uid(user)
    # [返工 R5] brand 归属硬校验:防跨租户读他人客户联系方式/卖点
    if body.brand_id and not _verify_brand_ownership(body.brand_id, uid):
        raise HTTPException(status_code=403, detail="品牌不存在或不属于当前账号")
    fields, competitors = _pull_brand_marketing_fields(body.brand_id, body.input_fields)
    # [返工 R4] 异步化:守卫+日上限+freeze 同步做完(扣费确定),生成转后台;
    # 出图 ~63s > nginx 60s,同步等待=必 504 且照扣费。前端拿 job_id 走 GET /jobs/{id} 轮询。
    prep = await material_factory.prepare_material_job(
        user_id=uid, template_id=body.template_id, input_fields=fields,
        material_kind=body.material_kind, brand_id=body.brand_id, owner_scope="user",
        size=body.size, resolution=body.resolution, competitors=competitors,
        purpose=body.purpose)
    if prep["status"] == "blocked":
        # 生成前守卫拦截 = 零扣费 · 返回 200 带 blocked(前端提示修改文案)
        return {"ok": False, **prep}
    if prep["status"] in ("failed", "error"):
        raise HTTPException(status_code=400, detail={k: v for k, v in prep.items() if k != "_ctx"})
    ctx = prep.pop("_ctx")
    import asyncio
    asyncio.create_task(material_factory.execute_material_job(ctx))
    return {"ok": True, "status": "generating", "job_id": prep["job_id"]}


@router.get("/jobs/{job_id}", summary="任务状态 + 成品资产")
async def api_job(job_id: int, request: Request):
    user = _require_user(request)
    job = marketing_db.get_job(job_id)
    if not job or job["user_id"] != _uid(user):
        raise HTTPException(status_code=404, detail="任务不存在")
    geo = (job.get("input_fields_jsonb") or {}).get("_geo") or {}
    if geo:
        from auth.brand_access import require_brand_access
        from services.marketing import geo_factory
        from services.marketing.evidence import require_frozen_evidence_live

        _require_live_job_org_authority(request, geo, revoked_status=404)
        if job.get("brand_id"):
            require_brand_access(request, int(job["brand_id"]))
        try:
            require_frozen_evidence_live(geo.get("evidence") or {})
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if str(job.get("status") or "") in {"pending", "generating"}:
            # Polling is also the crash-recovery signal.  Re-preparing the same
            # immutable request only rehydrates its billing context; advisory
            # locks prevent a second executor or a second user charge.
            try:
                prepared = await geo_factory.prepare_geo_package_job(
                    user_id=_uid(user), brand_id=job.get("brand_id"), geo_snapshot=geo,
                    request_hash=str(geo.get("request_hash") or ""),
                    resolution=str(job.get("resolution") or "1k"),
                    organization_identity=getattr(request.state, "organization_identity", None),
                )
                if prepared.get("_ctx"):
                    asyncio.create_task(geo_factory.execute_geo_package_job(prepared.pop("_ctx")))
            except Exception as exc:  # existing job remains visible; next poll/recovery can retry
                logger.warning("GEO job poll recovery deferred job=%s: %s", job_id, exc)
            job = marketing_db.get_job(job_id) or job
        state = geo_factory.job_public_state(job)
        return {"ok": True, "job": state, "assets": state["assets"]}
    return {"ok": True, "job": job, "assets": marketing_db.list_assets(job_id)}


@router.get("/content-packages/by-request/{request_id}", summary="按持久幂等键恢复未确认任务")
async def api_content_package_by_request(request_id: str, request: Request):
    user = _require_user(request)
    if not _REQUEST_ID_RE.fullmatch(str(request_id or "")):
        raise HTTPException(status_code=422, detail="request_id_invalid")
    job = marketing_db.get_job_by_request_id(_uid(user), request_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return await api_job(int(job["id"]), request)


@router.get("/my-materials", summary="我的物料库")
async def api_my_materials(request: Request, limit: int = 50):
    user = _require_user(request)
    rows = marketing_db.list_user_materials(_uid(user), limit=max(1, min(int(limit), 100)))
    from auth.brand_access import require_brand_access
    from services.marketing.evidence import require_frozen_evidence_live
    from services.marketing.geo_factory import job_public_state

    materials = []
    for row in rows:
        geo = (row.get("input_fields_jsonb") or {}).get("_geo") or {}
        if not geo:
            materials.append(row)
            continue
        job = marketing_db.get_job(int(row["job_id"]))
        if not job:
            continue
        try:
            _require_live_job_org_authority(request, geo, revoked_status=403)
            if job.get("brand_id"):
                require_brand_access(request, int(job["brand_id"]))
            require_frozen_evidence_live(geo.get("evidence") or {})
        except (HTTPException, ValueError):
            continue
        state = job_public_state(job)
        materials.append(state)
    return {"ok": True, "materials": materials}


class RetryComponentsRequest(BaseModel):
    request_id: str = Field(min_length=8, max_length=128)
    component_ids: list[str] = Field(default_factory=list)


class ProviderOutcomeResolutionRequest(BaseModel):
    resolution: str = Field(max_length=20)
    provider_task_id: str = Field(default="", max_length=200)
    image_url: str = Field(default="", max_length=2000)
    note: str = Field(default="", max_length=500)


@router.post("/jobs/{job_id}/retry", summary="仅重做失败或指定组件")
async def api_retry_content_components(job_id: int, request: Request, body: RetryComponentsRequest):
    user = _require_user(request)
    uid = _uid(user)
    if not _REQUEST_ID_RE.fullmatch(str(body.request_id or "")):
        raise HTTPException(status_code=422, detail="request_id_invalid")
    job = marketing_db.get_job(job_id)
    if not job or int(job["user_id"]) != uid:
        raise HTTPException(status_code=404, detail="任务不存在")
    geo = (job.get("input_fields_jsonb") or {}).get("_geo") or {}
    if not geo:
        raise HTTPException(status_code=422, detail="旧版物料不支持组件级重做")
    if str(job.get("status") or "") not in {"succeeded", "failed", "blocked"}:
        raise HTTPException(status_code=409, detail="当前版本仍在生成，不能创建局部重试")
    if job.get("error_summary") in {"billing_commit_pending", "billing_release_pending"}:
        raise HTTPException(status_code=409, detail="结算状态待核对，暂不允许重做")
    from auth.brand_access import require_brand_access
    from services.marketing import geo_factory
    from services.marketing.evidence import require_frozen_evidence_live

    _require_live_job_org_authority(request, geo, revoked_status=403)
    if job.get("brand_id"):
        require_brand_access(request, int(job["brand_id"]))
    try:
        require_frozen_evidence_live(geo.get("evidence") or {})
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    valid = geo_factory.valid_component_ids(
        list(geo.get("channels") or []),
        moments_layout=str(geo.get("moments_layout") or "single"),
    )
    requested = list(dict.fromkeys(body.component_ids))
    if not requested:
        completed = {
            str(asset.get("bundle_slot") or "") for asset in geo_factory.effective_assets(job)
        }
        requested = sorted(valid - completed)
    if not requested or not set(requested).issubset(valid):
        raise HTTPException(status_code=422, detail="component_ids_invalid")
    retry_geo = dict(geo)
    retry_geo.update({
        "request_id": body.request_id,
        "parent_job_id": int(job_id),
        "only_components": requested,
        "revision_no": int(geo.get("revision_no") or 1) + 1,
    })
    # 角度轮换(Owner:同主题多次生成角度不同):局部重试按 job 血缘父 rotation+1,
    # 重新分配并冻结,保证与上一版角度不同;存量无角度 job 从 rotation=1 起步。
    from services.marketing import content_angles

    retry_rotation = content_angles.next_rotation(geo.get("angle_rotation"))
    retry_geo["angle_rotation"] = retry_rotation
    retry_geo["angles"] = content_angles.assign_angles(
        list(geo.get("channels") or []), rotation=retry_rotation,
    )
    frozen_deal = geo.get("deal") or {}
    if frozen_deal.get("draft_id"):
        # deal 打码门禁看冻结快照里的 redacted_materials;用户补打完码后重试必须
        # 重新冻结草稿最新状态(否则 deal_chat 永远再跳过,局部重试死循环)。
        # 属主 + 组织绑定 live 复核(撤权/离组织 fail-closed)。
        deal_draft = _load_owned_deal_draft(int(frozen_deal["draft_id"]), uid, request)
        refreshed = _freeze_deal_snapshot(deal_draft, operator_user_id=uid)
        refreshed["skipped_components"] = []  # 新一次执行重新评估跳过,不继承旧记录
        retry_geo["deal"] = refreshed
        # sheet 在冻结后有更新(2026-07-23 外部审查 P2-3):重取 facts/strategy,
        # 局部重试不再沿用旧事实快照(改了金额重试,新文案必须用新金额);
        # 打码状态本身已由上方 refreshed 覆盖;contact/渠道/角度轮换(+1)不变。
        frozen_version = str(frozen_deal.get("version") or "")
        current_version = str(deal_draft.get("updated_at") or deal_draft.get("created_at") or "")
        current_sheet = deal_draft.get("sheet_jsonb") or {}
        if current_version != frozen_version and current_sheet != (frozen_deal.get("sheet_snapshot") or {}):
            from services.marketing import deal_intake as _deal_intake
            from services.marketing.evidence import freeze_evidence as _freeze_evidence

            anonymize = bool(geo.get("anonymize_brand"))
            brand_mask_terms = None
            if anonymize:
                # 与创建时同口径:冻结品牌名(含 ASCII)作为字面词一并掩掉。
                frozen_brand_name = str((geo.get("brand") or {}).get("name") or "").strip()
                brand_mask_terms = [frozen_brand_name] if frozen_brand_name else None
            teacher = geo.get("teacher") or {}
            new_strategy = _deal_intake.strategy_from_sheet(
                current_sheet, anonymize=anonymize, mask_terms=brand_mask_terms,
            )
            new_strategy["teacher_id"] = teacher.get("teacher_id")
            new_strategy["teacher_version"] = teacher.get("version")
            retry_geo["strategy"] = new_strategy
            retry_geo["evidence"] = _freeze_evidence(
                brand_id=job.get("brand_id"), source_type="brand_facts",
                facts=_deal_intake.facts_from_sheet(
                    current_sheet, anonymize=anonymize, mask_terms=brand_mask_terms,
                ),
            )
    request_hash = geo_factory.canonical_hash({
        "actor_user_id": uid,
        "parent_job_id": int(job_id),
        "request_id": body.request_id,
        "component_ids": requested,
        "source_request_hash": geo.get("request_hash"),
    })
    retry_geo["request_hash"] = request_hash
    identity = getattr(request.state, "organization_identity", None)
    try:
        prepared = await geo_factory.prepare_geo_package_job(
            user_id=uid, brand_id=job.get("brand_id"), geo_snapshot=retry_geo,
            request_hash=request_hash, resolution=str(job.get("resolution") or "1k"),
            organization_identity=identity,
        )
    except Exception as exc:
        raise _organization_http_error(request, exc) from exc
    if prepared.get("_ctx"):
        asyncio.create_task(geo_factory.execute_geo_package_job(prepared.pop("_ctx")))
    return {"ok": prepared["status"] not in {"failed", "blocked"}, **prepared}


@router.post(
    "/jobs/{job_id}/attempts/{attempt_id}/reconcile-provider",
    summary="管理员核销 provider 响应未知 attempt",
)
async def api_reconcile_provider_outcome(
    job_id: int,
    attempt_id: int,
    request: Request,
    body: ProviderOutcomeResolutionRequest,
):
    user = _require_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="仅平台管理员可核销 provider 响应未知状态")
    job = marketing_db.get_job(int(job_id))
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    try:
        resolved = marketing_db.resolve_unknown_attempt(
            attempt_id=int(attempt_id), job_id=int(job_id), resolution=body.resolution,
            provider_task_id=body.provider_task_id, image_url=body.image_url,
            operator_user_id=_uid(user), note=body.note,
        )
    except ValueError as exc:
        code = str(exc)
        status = 409 if code in {"provider_attempt_not_unknown", "organization_unknown_resolution_conflict"} else 422
        raise HTTPException(status_code=status, detail=code) from exc
    from services.marketing.geo_factory import recover_reconciled_geo_job

    asyncio.create_task(recover_reconciled_geo_job(int(job_id)))
    return {
        "ok": True,
        "job_id": int(job_id),
        "attempt_id": int(resolved["id"]),
        "resolution": body.resolution,
        "status": "reconciliation_queued",
    }


@router.post("/materials/{asset_id}/confirm", summary="用户预览自审确认外发")
async def api_confirm_rights(asset_id: int, request: Request):
    user = _require_user(request)
    asset = _load_delivery_asset(int(asset_id), _uid(user))
    if not asset:
        raise HTTPException(status_code=404, detail="素材不存在")
    _require_live_delivery_authority(request, asset)
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE marketing_material_assets SET rights_confirmed=1 WHERE id=%s AND rights_confirmed=0",
            (asset_id,),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "asset_id": asset_id, "rights_confirmed": 1}


class AssetCopyEditRequest(BaseModel):
    updates: dict = Field(default_factory=dict)
    reason: str = Field(default="", max_length=200)


def _clean_copy_field_value(value) -> object:
    """Bound one edited copy field; lists/dicts stay in their original shape."""
    if isinstance(value, str):
        return " ".join(value.split()).strip()[:500]
    if isinstance(value, list):
        return [" ".join(str(item).split()).strip()[:500] for item in value[:20]]
    if isinstance(value, dict):
        return {
            str(key)[:40]: " ".join(str(item).split()).strip()[:500]
            for key, item in list(value.items())[:20]
        }
    raise ValueError("asset_copy_field_type_invalid")


@router.patch("/assets/{asset_id}", summary="编辑成品文案(重跑 claim QA+守卫 → 审计事件;不可改图)")
async def api_edit_asset_copy(asset_id: int, request: Request, body: AssetCopyEditRequest):
    """成品级文案编辑:仅 copy 资产的渠道 copy_fields 可改;图片字节不 touch。

    保存前按该 job 冻结的 evidence/contact/channel 重跑
    scan_geo_claims(claim_evidence_qa 内含)+guards。分层口径(Owner 2026-07-22;
    SSOT 2026-07-23):只有法律禁止目录包命中项(errors)→ 422 不落库;承诺词/灰词/
    无证据数字等进 warnings 照常保存并透传。成功后写 marketing_events 审计五要素事件。
    """
    user = _require_user(request)
    uid = _uid(user)
    asset = _load_delivery_asset(int(asset_id), uid)
    if not asset:
        raise HTTPException(status_code=404, detail="素材不存在")
    _require_live_delivery_authority(request, asset)
    if str(asset.get("asset_kind") or "") != "copy":
        raise HTTPException(status_code=422, detail="asset_copy_only_editable")
    from services.marketing.content_center import channel_contract, claim_evidence_qa

    channel = str(asset.get("bundle_slot") or "").split(":", 1)[0]
    try:
        contract = channel_contract(channel)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    editable = set(contract["copy_fields"])
    updates = body.updates if isinstance(body.updates, dict) else {}
    unknown = sorted(set(updates) - editable)
    if unknown:
        raise HTTPException(status_code=422, detail=f"asset_copy_field_not_editable:{','.join(unknown)}")
    if not updates:
        raise HTTPException(status_code=422, detail="asset_copy_edit_empty")
    try:
        content = json.loads(asset.get("content_text") or "{}")
        if not isinstance(content, dict):
            raise ValueError
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="素材文案内容损坏，不能编辑") from exc
    content_before = dict(content)
    try:
        for key, value in updates.items():
            content[key] = _clean_copy_field_value(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    geo = (asset.get("input_fields_jsonb") or {}).get("_geo") or {}
    evidence = geo.get("evidence") or {"source_type": "none", "facts": []}
    contact = geo.get("contact") or {"mode": "none", "text": ""}
    qa = claim_evidence_qa(content, evidence=evidence, contact=contact, channel=channel)
    if not qa["passed"]:
        # 只有法律红线(法律禁止目录包命中项)拒绝保存;warnings 不阻断。
        # 422 detail 走五问合同(error_entry):code/message/reason/repair_hint/actions/rule_version。
        from services.marketing.content_center import error_entry as _error_entry

        raise HTTPException(
            status_code=422,
            detail=_error_entry("asset_copy_edit_rejected", errors=qa["errors"]),
        )

    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE marketing_material_assets SET content_text=%s WHERE id=%s",
            (json.dumps(content, ensure_ascii=False, sort_keys=True), int(asset_id)),
        )
        conn.commit()
    finally:
        conn.close()
    from services.marketing.content_center import QA_RULES_VERSION as _QA_RULES_VERSION
    from services.marketing.content_center import audit_quintuple as _audit_quintuple

    marketing_db.add_event(
        event_type="geo_asset_copy_edited", actor_id=uid,
        message=f"编辑成品文案 asset={int(asset_id)} channel={channel} 字段={','.join(sorted(updates))}",
        payload={
            "asset_id": int(asset_id), "job_id": int(asset["job_id"]),
            "channel": channel, "fields": sorted(updates), "qa_passed": True,
            "warnings_acknowledged": len(qa.get("warnings") or []),
            # 审计五要素(SSOT §2.4):操作人/时间/原因/规则版本/原内容/修改内容。
            **_audit_quintuple(
                operator_user_id=uid,
                reason=body.reason or "人工编辑成品文案并确认继续(含提醒照常保存)",
                rule_version=_QA_RULES_VERSION,
                content_before=content_before,
                content_after=dict(content),
            ),
        },
    )
    return {"ok": True, "asset_id": int(asset_id), "channel": channel, "content": content, "qa": qa}


@router.get("/materials/{asset_id}/download", summary="授权读取或下载最终素材")
async def api_download_material(asset_id: int, request: Request):
    user = _require_user(request)
    asset = _load_delivery_asset(int(asset_id), _uid(user))
    if not asset:
        raise HTTPException(status_code=404, detail="素材不存在")
    _require_live_delivery_authority(request, asset)
    try:
        data, mime = material_storage.read_material_public_reference(str(asset.get("url_stored") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="素材文件不存在") from exc
    return Response(
        content=data,
        media_type=mime,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'inline; filename="geo-material-{int(asset_id)}.{mime.split("/")[-1]}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/batch-generate", summary="服务商为多个绑定客户批量生成(异步,返 job_id 列表)")
async def api_batch_generate(request: Request, body: BatchGenerateRequest):
    user = _require_user(request)
    uid = _uid(user)
    import asyncio
    results = []
    for bid in body.brand_ids[:50]:
        # [返工 R5] 逐 brand 归属校验:非本人客户跳过标 forbidden(批量不整单炸)
        if not _verify_brand_ownership(bid, uid):
            results.append({"brand_id": bid, "status": "forbidden", "job_id": None})
            continue
        fields, competitors = _pull_brand_marketing_fields(bid, body.base_input_fields)
        # [返工 R4] 守卫+freeze 同步(扣费确定),生成后台;批量数十分钟同步等=必超时
        prep = await material_factory.prepare_material_job(
            user_id=uid, template_id=body.template_id, input_fields=fields,
            material_kind=body.material_kind, brand_id=bid, owner_scope="user",
            competitors=competitors, purpose=body.purpose)
        if prep.get("status") == "generating":
            asyncio.create_task(material_factory.execute_material_job(prep.pop("_ctx")))
        results.append({"brand_id": bid, "status": prep["status"], "job_id": prep.get("job_id")})
    return {"ok": True, "results": results}


class ReportRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


@router.post("/materials/{asset_id}/report", summary="举报违规物料(机器闸门的人工兜底通道)")
async def api_report_material(asset_id: int, request: Request, body: ReportRequest):
    """[返工 R5 + Codex 复审加固] 任何登录用户可举报,但:
    events-only(不直接写资产 risk_flags,处置权在 admin——防猜 asset_id 污染他人素材风控字段)
    + 同人同资产幂等 + 每人每日限 10 次。admin 在事件流看到举报后人工处置。"""
    user = _require_user(request)
    uid = _uid(user)
    r = marketing_db.record_material_report(asset_id, uid, body.reason)
    if r["status"] == "not_found":
        raise HTTPException(status_code=404, detail="素材不存在")
    if r["status"] == "daily_limited":
        raise HTTPException(status_code=429, detail=f"今日举报已达上限({r.get('limit', 10)} 次)")
    if r["status"] == "error":
        raise HTTPException(status_code=500, detail="举报暂不可用,请稍后再试")
    # reported / already_reported 都算成功(幂等语义)
    return {"ok": True, "asset_id": asset_id, "status": r["status"]}
