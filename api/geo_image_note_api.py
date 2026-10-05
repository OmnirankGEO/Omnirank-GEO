"""GEO 图文合同链 · 六个新端点(规格 02 §5 / §7)。

## 为什么单独一个模块而不是塞进那两个巨型文件

`api/geo_douyin_api.py`(1.6k 行)与 `api/meijiehezi_api.py`(4.4k 行)都已经很大。
本模块用**不带 prefix 的 router + 每条路由写全路径**,挂在同一个 app 上,
对外的路径面与规格逐字一致 —— 这不是"第二套系统",路由前缀、订单引擎、
发布记录页全部沿用既有那一套(规格「禁止另造」清单)。

## 每个 mutation 的固定开头

```
identity = _resolve_identity(request)          # 服务端解析,客户端不参与
_guard(request, action=..., ability=...)       # schema → demo → ability
```

🔴 闸必须在**任何**副作用之前。「未就绪时零副作用」这句话,只有当检查发生在
   claim / 冻结 / 建单 / 外调之前才成立;放在写入处等于事后才发现,
   那时候"零副作用"只是文案。
"""
from __future__ import annotations

import json as _json
import re
import logging
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from auth.brand_access import require_brand_access
from services.geo_douyin.production_draft import parse_etag as parse_draft_etag
from services.geo_douyin.contract_funding import organization_charge_available
from services.geo_douyin.contract_ids import UuidStr, normalize_uuid
from services.geo_douyin.pipeline_gate import pipeline_gate
from services.geo_douyin.contract_states import (
    TASK_STATUS_QUEUED,
)
from services.geo_douyin.contract_route_guard import (
    ABILITY_PRODUCE,
    ABILITY_PUBLISH,
    AbilityNotGranted,
    DemoReadOnly,
    SchemaNotReady,
    error_payload,
    guard_mutation,
)

logger = logging.getLogger("GEO-ImageNote-API")

#: 投放侧 SKU。规格 §7.3 末:publish 复用现役下单/退款链的 `media_proxy_publish`,
#: 不能 route 用 media_publish、reserve 又用另一个 —— 改 SKU 必须导致指纹变化。
PUBLISH_FEATURE_CODE = "media_proxy_publish"

def image_note_daily_limit() -> int:
    """单账号日发上限 —— **读配置 SSOT,不在本模块钉死一个数**。

    🔴 [返工 2026-08-18 · P0-12 · Owner A4] 原来是模块常量 `= 1`。
       两处错:
         ① Owner A4 定的是「不新增固定上限,只服从**真实资格 + today_remaining**」;
         ② 就算要有个数,现役口径也是 `GEO_DOUYIN_ACCOUNT_DAILY_LIMIT`(默认 3),
            而这里钉的是**最保守的那个值** —— 一个账号一天只能发一篇,
            而目录页、preflight、正式提交全用它 ⇒ 第二篇永远显示"额度已满"。

       真实资格判定仍然在 `account_eligibility.evaluate_accounts` 里:
       它先判身份能力(能不能发图文)、再判频控,`today_remaining` 是**它**算的。
       本函数只提供那条频控线的取值,**不是**第二套资格逻辑。
    """
    from services.geo_douyin.config import douyin_per_account_daily_limit

    return int(douyin_per_account_daily_limit())

router = APIRouter(
    tags=["GEO图文合同链"],
    # 🔴 [WO_213] Deploy 2026-09-14 实测:这 9 条路由**一条都不受总闸**,
    #    其中包括花供应商钱的 publish-batch。挂在路由器上一次盖全。
    dependencies=[Depends(pipeline_gate)],
)


# ─────────────────────────────────────────────────────────────
# 公共:身份解析 / 闸
# ─────────────────────────────────────────────────────────────

def _user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def _is_demo(request: Request) -> bool:
    """演示上下文由中间件登记(`resolve_demo_case_access` 已校验 grant 有效期)。"""
    return getattr(request.state, "demo_access_context", None) is not None


def _granted_abilities(request: Request) -> set[str]:
    """当前请求持有的能力。

    🔴 三条分支的理由各不相同,不要合并:
      · **组织成员**:能力来自 membership 快照。`OrganizationGuard` 已按
        `MEMBER_GEO_ROUTE_POLICIES` 在中间件层校验过一次,这里是**对象级之外的
        第二次**能力核对 —— 中间件保证"这条路由需要什么",这里保证"这次请求真有"。
      · **管理员**:平台权限,不是第四种商业身份;给全集。
      · **无组织的服务商 owner**:他就是 owner 本人,天然持有自己账户的能力。
    """
    identity = getattr(request.state, "organization_identity", None)
    if identity is not None:
        # 🔴 [#193 P0 · 2026-09-13] **owner 必须先判**,不能直接回快照。
        #    组织模型里 owner 的能力快照**刻意为空**
        #    (`organization_service.py:2226-2227`:`if row["is_owner"]:
        #      row["effective_capabilities"] = []`)—— owner 的权限不来自快照,
        #    来自"他就是 owner"。全仓其它地方都按这个口径绕
        #    (`organization_approvals.py:315` 是
        #     `identity.is_owner or "..." in identity.capabilities`)。
        #    这里漏了 is_owner ⇒ 有团队的服务商 owner 拿到空集 ⇒ 七个
        #    `_guard(ability=)` 端点全部 403 `ABILITY_NOT_GRANTED`,
        #    而中间件那一层是放行的:**同一个谓词两处实现,一处放行一处拒绝**。
        #    现场:0913a 上线后 QA 服务商(组织 owner)点发布即 403。
        if getattr(identity, "is_owner", False):
            return {ABILITY_PRODUCE, ABILITY_PUBLISH}
        return set(getattr(identity, "capabilities", frozenset()) or frozenset())
    user = _user(request)
    if user.get("is_admin"):
        return {ABILITY_PRODUCE, ABILITY_PUBLISH}
    return {ABILITY_PRODUCE, ABILITY_PUBLISH}


def _schema_blockers() -> list[str]:
    """图文 lane 的 readiness。**只**给本 lane 的 route 用,不挂 FLEET guard
    (规格 02 §13:否则新 lane 没就绪会把整站拒启)。"""
    from db.connection import get_connection
    from services.geo_douyin_schema_contract import schema_blockers

    conn = get_connection()
    try:
        return schema_blockers(conn.cursor())
    except Exception as exc:  # noqa: BLE001
        # 查不出来就当**没就绪**(fail-closed)。反方向是 fail-open —— 那会让
        # 一个连不上库的实例把请求放进去,然后在真正写入时才炸,副作用已经发生。
        logger.warning("[image-note] readiness 查询失败,按未就绪处理: %s", str(exc)[:160])
        return [f"readiness_probe_failed:{type(exc).__name__}"]
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _guard(request: Request, *, action: str, ability: str) -> None:
    """六个 mutation 端点**共用**的开头。写成一个函数是为了让"漏加闸"
    在 code review 里看得见 —— 判据 `test_every_guarded_endpoint_calls_guard`
    会逐个端点断言它被调用过。"""
    try:
        guard_mutation(
            schema_blockers_fn=_schema_blockers,
            is_demo=_is_demo(request),
            action=action,
            granted_abilities=_granted_abilities(request),
            required_ability=ability,
        )
    except SchemaNotReady as exc:
        raise HTTPException(status_code=503, detail=error_payload(exc))
    except DemoReadOnly as exc:
        raise HTTPException(status_code=403, detail=error_payload(exc))
    except AbilityNotGranted as exc:
        raise HTTPException(status_code=403, detail=error_payload(exc))


def _resolve_identity(request: Request) -> dict:
    """服务端解析 tenant/payer/actor。**客户端不参与**(规格 02 §8.2)。

    🔴 payer 与 actor 是两件事:员工是 actor,owner/principal 是 payer。
       写反了会让团队读取、退款、回流全部落到员工租户上
       (规格 §9 末段点名的那个形态)。
    """
    user = _user(request)
    org = getattr(request.state, "organization_identity", None)
    if org is not None:
        return {
            "tenant_owner_user_id": int(org.principal_user_id),
            "payer_user_id": int(org.payer_user_id),
            "actor_user_id": int(org.actor_user_id or org.authenticated_user_id),
            "actor_kind": str(org.actor_kind),
            "organization_id": int(org.organization_id),
            "membership_id": org.membership_id,
            "membership_version": org.membership_version,
            "payer_policy_snapshot": {"resolved": "organization",
                                      "authority_version": org.authority_version},
            # 审批门要的是完整 IdentityContext(策略行按 membership 锁),
            # 不是这里摊平的几个 id。带原对象过去,别在闸里重建一个近似的。
            "_organization_identity": org,
            # 🔴 [返工 2026-08-18 · Codex P0-06] 原来这里**无条件** True。
            #    后果不是"多给了一点权限",而是**每个组织成员必 500**:
            #      readiness=True → resolve_settlement_authority 返回
            #      `organization_charge` → 但本链随后走的是 `freeze_one_item`
            #      (direct 句柄)→ `assert_authority_shape` 判定两套权威互斥 → 抛。
            #    真值只有一个来源:这条 lane **有没有真的接上**组织计费链。
            #    见 `contract_funding.organization_charge_available()`。
            "organization_billing_ready": organization_charge_available(),
        }
    return {
        "tenant_owner_user_id": int(user["user_id"]),
        "payer_user_id": int(user["user_id"]),
        "actor_user_id": int(user["user_id"]),
        "actor_kind": "owner",
        "organization_id": None,
        "membership_id": None,
        "membership_version": None,
        "payer_policy_snapshot": {"resolved": "solo_owner"},
        "organization_billing_ready": False,
    }


def _err(code: str, message: str, *, reason: str, impact: str, repair: str,
         actions: list, retryable: bool = True, **extra) -> dict:
    """统一错误合同(规格 §12)。actions[].type 只用现役 builder 认的枚举。"""
    payload = {
        "code": code, "message": message, "reason": reason, "impact": impact,
        "repair_hint": repair, "actions": actions,
        "next_action": actions[0]["id"] if actions else None,
        "retryable": retryable,
    }
    payload.update(extra)
    return payload


# ─────────────────────────────────────────────────────────────
# 请求模型
# ─────────────────────────────────────────────────────────────

class ProductionItemSettings(BaseModel):
    card_count: int = 4
    aspect_ratio: str = "3:4"
    content_form: str = ""
    style_key: str = ""
    ranking_template: Optional[str] = None
    ranking_entity_count: Optional[int] = None
    style_catalog_version: str = ""


class ProductionPreviewItem(BaseModel):
    # 🔴 [返工 · 链 3] 这两个字段最终会落进 **uuid 列**;在入口就按 uuid 校验,
    #    不让「模型放行、库层 500」再发生一次(见 contract_ids 模块 docstring)。
    item_request_id: UuidStr
    delivery_slot_key: UuidStr
    topic_ref: str
    settings: ProductionItemSettings = Field(default_factory=ProductionItemSettings)


class ProductionPreviewRequest(BaseModel):
    quote_id: int
    # 🔴 类型按**真实列**定:`geo_douyin_posts.contract_revision_id` 与
    #    `geo_article_contract_revisions.id` 都是 BIGINT(034 与生产 schema 实测)。
    #    规格 JSON 示例里的 `"rev_8"` 是**示意**不是类型 —— 照它写成 str 会在
    #    入库那一刻抛 InvalidTextRepresentation(本包自己的协调器判据当场抓到)。
    contract_revision_id: Optional[int] = None
    items: List[ProductionPreviewItem]


class ProductionDraftRequest(BaseModel):
    quote_id: int
    # 🔴 类型按**真实列**定:`geo_douyin_posts.contract_revision_id` 与
    #    `geo_article_contract_revisions.id` 都是 BIGINT(034 与生产 schema 实测)。
    #    规格 JSON 示例里的 `"rev_8"` 是**示意**不是类型 —— 照它写成 str 会在
    #    入库那一刻抛 InvalidTextRepresentation(本包自己的协调器判据当场抓到)。
    contract_revision_id: Optional[int] = None
    selected_slot_keys: List[str] = Field(default_factory=list)
    topic_refs: dict = Field(default_factory=dict)
    settings: ProductionItemSettings = Field(default_factory=ProductionItemSettings)
    scroll_anchor: Optional[str] = None


class BatchItem(BaseModel):
    item_request_id: UuidStr
    delivery_slot_key: UuidStr
    topic_ref: str
    expected_price_fingerprint: str
    # 🔴 [返工 2026-08-18 · P0-08] 槽位乐观锁的期望版本,由 delivery-plan 下发、
    #    页面原样回传。原来模型里没有它,handler 只能填 0,而新建槽位的
    #    `projection_version` 是 1 ⇒ CAS 恒零行 ⇒ 每一次 claim 都 409。
    #    默认 0 保留给"没读过计划就提交"的调用方 —— 那种请求本来就该被 CAS 拒。
    expected_slot_version: int = 0
    settings: ProductionItemSettings = Field(default_factory=ProductionItemSettings)


class CreateBatchRequest(BaseModel):
    # 🔴 [返工 2026-08-18 · P0-02] 原来**没有**这个字段,而 Pydantic v2 默认
    #    `extra='ignore'` —— 前端发的幂等键一声不响地消失,handler 每次自造
    #    一个新 uuid,于是双击创建两个批次。模块层的 20 并发幂等判据是真的,
    #    但从 UI 根本走不到。**字段被静默丢弃比 422 更危险**:422 至少会喊。
    request_id: UuidStr
    quote_id: int
    # 🔴 类型按**真实列**定:`geo_douyin_posts.contract_revision_id` 与
    #    `geo_article_contract_revisions.id` 都是 BIGINT(034 与生产 schema 实测)。
    #    规格 JSON 示例里的 `"rev_8"` 是**示意**不是类型 —— 照它写成 str 会在
    #    入库那一刻抛 InvalidTextRepresentation(本包自己的协调器判据当场抓到)。
    contract_revision_id: Optional[int] = None
    draft_id: Optional[str] = None
    expected_draft_etag: Optional[str] = None
    expected_total_price_points: int
    items: List[BatchItem]


class PublishPreviewItem(BaseModel):
    # `mhz_publish_order_items.item_request_id` 同样是 uuid 列。
    item_request_id: UuidStr
    geo_post_id: int
    # 🔴 [返工 2026-08-18 · P0-01] 原声明为 str,而前端发的是 number。
    #    Pydantic v2 默认不做 int→str 强转,于是真 HTTP 422。
    #    本包在协调器轮刚因**反方向**的类型错配栽过(str 进 BIGINT 列)——
    #    同一个病:契约两端各写各的,没有一条判据在中间对表。
    post_revision_id: int
    prepared_artifact_id: str
    manifest_hash: str
    media_id: int


class PublishPreviewRequest(BaseModel):
    # 🔴 发布必须 quote-scoped(WP7 整轮修的就是"猜作用域"这类缺陷)。
    #    模型收不下 brand_id/quote_id,作用域信息就在合同缝合面上丢失,
    #    服务端只能靠猜 —— 前端明明发了。
    brand_id: Optional[int] = None
    quote_id: Optional[int] = None
    items: List[PublishPreviewItem]


#: [#184 d5] **内容来源铁律**的守门人:发出去的内容只能由服务端按
#: `post_id` 从冻结版本/产物取,请求体只收 post_id + 账号清单 + 幂等键。
#:
#: 🔴 今天这些键是**静默忽略**的(Pydantic 默认丢弃未声明字段),所以调用方
#:    传了标题,它既不生效也不报错 —— 而调用方会以为生效了。
#: 🔴 用**显式拒绝清单**而不是 `extra="forbid"`:后者在本仓炸过三次生产
#:    (:1601 自记),它会把任何一个无害的新增键也变成 422。
#:    这里只拒**携带内容**的那几个,其余未知键照旧忽略。
_CONTENT_KEYS_NOT_ACCEPTED = (
    "title", "body", "body_text", "content", "images", "image_urls",
    "cover_image", "hashtags", "cards",
)


def _reject_client_supplied_content(values):
    if not isinstance(values, dict):
        return values
    hit = sorted(k for k in _CONTENT_KEYS_NOT_ACCEPTED if k in values)
    if hit:
        raise ValueError(
            "CLIENT_CONTENT_NOT_ACCEPTED: 请求体不接受内容字段 %s ——"
            "发布内容一律由服务端按 post_id 从冻结版本取,"
            "请只传 post_id / 账号 / 幂等键" % "、".join(hit))
    return values


class PublishBatchItem(PublishPreviewItem):
    # [#184 d5] 逐项也挡:内容字段可能挂在项上而不是请求顶层。
    _reject_content = model_validator(mode="before")(
        staticmethod(_reject_client_supplied_content))

    expected_price_fingerprint: str
    # [#184 d6] `expected_price_points` 已删。原注释说「服务端据此逐项比对」——
    #   **那是假的**:全仓零读(只有声明、注释和一个夹具)。逐项价身份由
    #   `expected_price_fingerprint` 真正承担:`publish_coordinator.py:164`
    #   逐项调 `assert_price_unchanged`,不符抛 PriceChanged,而价格本身就编在
    #   指纹里。留着一个没人读的字段 + 一句说它在把关的注释,比没有更糟:
    #   下一个人会以为那道闸存在。



class PublishBatchRequest(BaseModel):
    # 🔴 这一个**刻意**不收成 UuidStr:它落进的两列
    #    (`publish_idempotency_keys.request_id` / `mhz_publish_order_items.command_request_id`)
    #    实测都是 **text**。按"列是什么就校验什么"办 —— 统一收紧看着整齐,
    #    但会把老链已经发过的非 uuid command id 挡在门外。
    # [#184 d5] 顶层也挡一道:两处都要,因为调用方两个位置都可能塞。
    _reject_content = model_validator(mode="before")(
        staticmethod(_reject_client_supplied_content))

    request_id: str
    expected_total_price_points: int
    items: List[PublishBatchItem]


def _recompute_items(item_dicts: list) -> list[dict]:
    """服务端**重新**算每一项的价与指纹(P0-02 的核心)。

    🔴 与 `/production-preview` 走**同一个**定价源和同一套指纹构造 ——
       不是"再写一份差不多的算法"。两份算法必然漂移,而漂移的那天
       表现是"预览和扣费对不上",查起来要翻两处实现。
       这里通过复用 `read_unit_points` / `extra_card_points` /
       `production_fingerprint` 保证只有一份。

    🔴 同步上下文里跑 async 定价函数:用一个**独立事件循环**,
       不碰调用方的连接与事务(冻结那处同理,见 §8.2 的注释)。
    """
    import asyncio as _a

    from services.geo_douyin.config import FEATURE_CODE_IMAGE_POST
    from services.geo_douyin.contract_pricing import production_fingerprint
    from services.geo_douyin.pricing import extra_card_points, read_unit_points

    loop = _a.new_event_loop()
    try:
        unit = int(loop.run_until_complete(read_unit_points(FEATURE_CODE_IMAGE_POST)))
        out = []
        for item in item_dicts:
            settings = item.get("settings") or {}
            card_count = int(settings.get("card_count") or 0)
            extra = int(loop.run_until_complete(extra_card_points(card_count)))
            final_points = unit + extra
            out.append({
                "final_price_points": final_points,
                "extra_points": extra,
                "fingerprint": production_fingerprint(
                    feature_code=FEATURE_CODE_IMAGE_POST, unit_points=unit,
                    card_count=card_count, extra_card_points=extra,
                    final_price_points=final_points,
                    resolver_version="geo_douyin.pricing.v1",
                    catalog_version="feature_pricing",
                    style_catalog_version=settings.get("style_catalog_version") or None,
                    policy_version=None),
            })
        return out
    finally:
        loop.close()


# ─────────────────────────────────────────────────────────────
# 1. 制作算力预览(不冻结、不创建)
# ─────────────────────────────────────────────────────────────

@router.post("/api/geo-douyin/production-preview")
async def api_production_preview(req: ProductionPreviewRequest, request: Request):
    """服务端权威制作算力。前端**不计算**任何价格(01 §10 不可签收条件)。

    只复用制作定价源(`feature_pricing` / `services.geo_douyin.pricing`),
    **不**调用媒体账号价格 resolver —— 两条链的指纹不可互换(规格 §6.2)。
    """
    import asyncio

    _user(request)
    _guard(request, action="production.preview", ability=ABILITY_PRODUCE)

    from db.diagnosis_db import get_quote

    quote = await asyncio.to_thread(get_quote, int(req.quote_id))
    if not quote:
        raise HTTPException(status_code=404, detail="报价不存在")
    if not quote.get("brand_id"):
        raise HTTPException(status_code=409, detail="这张报价还没有关联客户")
    require_brand_access(request, int(quote["brand_id"]))

    from services.geo_douyin.contract_pricing import (
        OPERATION_PRODUCTION, canonical_price_snapshot, production_fingerprint,
    )
    from services.geo_douyin.pricing import extra_card_points, read_unit_points
    from services.geo_douyin.config import FEATURE_CODE_IMAGE_POST

    unit = await read_unit_points(FEATURE_CODE_IMAGE_POST)
    items_out: list[dict[str, Any]] = []
    total = 0
    for item in req.items:
        extra = await extra_card_points(item.settings.card_count)
        final_points = int(unit) + int(extra)
        total += final_points
        fingerprint = production_fingerprint(
            feature_code=FEATURE_CODE_IMAGE_POST, unit_points=int(unit),
            card_count=int(item.settings.card_count), extra_card_points=int(extra),
            final_price_points=final_points,
            resolver_version="geo_douyin.pricing.v1",
            catalog_version="feature_pricing",
            style_catalog_version=item.settings.style_catalog_version or None,
            policy_version=None)
        items_out.append({
            "item_request_id": item.item_request_id,
            "delivery_slot_key": item.delivery_slot_key,
            "final_price_points": final_points,
            "production_price_fingerprint": fingerprint,
            "price_snapshot": canonical_price_snapshot(
                operation=OPERATION_PRODUCTION, final_price_points=final_points,
                feature_code=FEATURE_CODE_IMAGE_POST,
                resolver_version="geo_douyin.pricing.v1", catalog_version="feature_pricing",
                markup_version=None, policy_version=None,
                preview_expires_at="", confirmed_at=None),
        })
    return {"status": "success", "items": items_out, "total_price_points": total}


# ─────────────────────────────────────────────────────────────
# 2. 提交前制作草稿(ETag CAS)
# ─────────────────────────────────────────────────────────────

@router.put("/api/geo-douyin/production-drafts/{draft_id}")
async def api_save_production_draft(draft_id: str, req: ProductionDraftRequest,
                                    request: Request):
    """草稿**不** claim slot、不建 post/task、不调 provider、不冻结算力(规格 §5.2)。

    冲突走 `If-Match`:两标签页同改同一草稿时,旧 ETag 提交返回可理解的冲突界面,
    不静默覆盖(01 §3.3)。
    """
    _user(request)
    _guard(request, action="production.draft.save", ability=ABILITY_PRODUCE)
    if_match = request.headers.get("if-match")
    if not if_match:
        raise HTTPException(status_code=428, detail={
            "code": "REVISION_CONFLICT",
            "message": "缺少版本标识",
            "reason": "草稿保存必须带 If-Match,避免两处同时修改互相覆盖",
            "impact": "本次未保存",
            "repair_hint": "重新加载草稿后再保存",
            "actions": [{"id": "reload_latest", "label": "加载最新版", "type": "retry"}],
            "next_action": "reload_latest", "retryable": True,
        })
    expected_etag = parse_draft_etag(if_match) if if_match != "*" else None

    import asyncio

    from db.connection import get_connection
    from db.diagnosis_db import get_quote
    from services.geo_douyin.production_draft import DraftConflict, save_draft

    quote = await asyncio.to_thread(get_quote, int(req.quote_id))
    if not quote or not quote.get("brand_id"):
        raise HTTPException(status_code=404, detail="报价不存在或未关联客户")
    require_brand_access(request, int(quote["brand_id"]))

    user = _user(request)
    identity = {
        # 🔴 payer 由服务端解析,客户端不参与(规格 §8.2)。无组织上下文时
        #    owner 就是本人;有组织时 payer 是 owner,actor 才是员工。
        "tenant_owner_user_id": int(user["user_id"]),
        "payer_user_id": int(user["user_id"]),
        "actor_user_id": int(user["user_id"]),
        "payer_policy_snapshot": {"resolved": "solo_owner"},
    }
    org = getattr(request.state, "organization_identity", None)
    if org is not None:
        identity = {
            "tenant_owner_user_id": int(org.principal_user_id),
            "payer_user_id": int(org.payer_user_id),
            "actor_user_id": int(org.actor_user_id or org.authenticated_user_id),
            "payer_policy_snapshot": {"resolved": "organization",
                                      "authority_version": org.authority_version},
        }

    def _save() -> dict:
        conn = get_connection()
        try:
            cur = conn.cursor()
            result = save_draft(
                cur, batch_id=str(draft_id), identity=identity,
                brand_id=int(quote["brand_id"]), quote_id=int(req.quote_id),
                contract_revision_id=req.contract_revision_id,
                payload=req.model_dump(), expected_total_price_points=0,
                expected_etag=expected_etag)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    try:
        saved = await asyncio.to_thread(_save)
    except DraftConflict as exc:
        raise HTTPException(status_code=409, detail={
            "code": "REVISION_CONFLICT",
            "message": "这份草稿在别处被改过了",
            "reason": "你手上的版本不是最新版",
            "impact": "本次未保存,已保存的内容没有被覆盖",
            "repair_hint": "加载最新版后再改,或对比两边差异",
            "actions": [
                {"id": "reload_latest", "label": "加载最新版", "type": "retry"},
                {"id": "compare_changes", "label": "对比差异", "type": "nav"},
            ],
            "next_action": "reload_latest", "retryable": True,
            "current_etag": (f'"draft-{exc.current_etag}"' if exc.current_etag else None),
        })
    return {"status": "success", "draft_id": str(saved["batch_id"]),
            "etag": saved["etag"], "etag_revision": int(saved["etag_revision"])}


# ─────────────────────────────────────────────────────────────
# 3. 批量创建
# ─────────────────────────────────────────────────────────────

@router.post("/api/geo-douyin/batches")
async def api_create_batch(req: CreateBatchRequest, request: Request):
    """处理顺序见规格 §5.3:原子 claim → 解析 payer/actor → 校验 → 逐项价格指纹 →
    审批 → slot CAS → 建 batch/post/task → 逐项 freeze → commit → durable worker 领取。

    🔴 provider 只能在 commit 之后开始;异常整体 rollback。
    """
    _user(request)
    _guard(request, action="production.batch.create", ability=ABILITY_PRODUCE)
    if not req.items:
        raise HTTPException(status_code=400, detail={
            "code": "EMPTY_BATCH", "message": "还没有选择要制作的内容",
            "reason": "本次提交没有任何一项", "impact": "本次未创建任何任务",
            "repair_hint": "返回选择要做的内容",
            "actions": [{"id": "back_to_select", "label": "返回选择", "type": "nav"}],
            "next_action": "back_to_select", "retryable": True,
        })
    # ── §5.3 处理顺序:claim → 解析 payer/actor → 校验 → 逐项价格指纹 →
    #    slot CAS → 建 batch/post/task → 逐项 freeze → commit → worker 领取 ──
    import asyncio
    import uuid as _uuid

    from db.connection import get_connection
    from db.diagnosis_db import get_quote
    from services.geo_douyin.contract_freeze import FreezeProducedNoHandle, freeze_one_item
    from services.geo_douyin.contract_funding import (
        FundingHandleInvalid, PlatformAccountUnavailable, resolve_settlement_authority,
    )
    from services.geo_douyin.contract_idempotency import (
        IdempotencyConflict, claim_request, production_request_payload,
        record_response, request_hash,
    )
    from services.geo_douyin.contract_pricing import (
        OPERATION_PRODUCTION, PriceChanged, assert_price_unchanged,
    )
    from services.geo_douyin.contract_approval import (
        ACTION_SPEND, ApprovalPending, require_approval,
    )
    from services.geo_douyin.delivery_plan import contract_lane_enabled
    from services.geo_douyin.delivery_slots import (
        SlotAuthorityMissing, SlotClaimConflict, SlotIdentityMismatch,
        activation_blockers, claim as claim_slot, load_slot_authority,
    )
    from services.geo_douyin.post_revisions import begin_generation, supersede_active_tasks
    from services.geo_douyin.config import FEATURE_CODE_IMAGE_POST

    user = _user(request)
    identity = _resolve_identity(request)

    quote = await asyncio.to_thread(get_quote, int(req.quote_id))
    if not quote or not quote.get("brand_id"):
        raise HTTPException(status_code=404, detail=_err(
            "SOURCE_NOT_FOUND", "报价不存在或未关联客户", reason="找不到这张报价",
            impact="本次未创建任何制作任务", repair="返回选择客户与报价",
            actions=[{"id": "back", "label": "返回选择", "type": "nav"}]))
    brand_id = int(quote["brand_id"])
    require_brand_access(request, brand_id)

    try:
        settlement = resolve_settlement_authority(
            is_admin=bool(user.get("is_admin")),
            organization_id=identity.get("organization_id"),
            organization_billing_ready=bool(identity.get("organization_billing_ready")),
            owner_user_id=int(identity["payer_user_id"]))
    except PlatformAccountUnavailable as exc:
        raise HTTPException(status_code=503, detail=_err(
            "PUBLISH_CHANNEL_UNAVAILABLE", "平台承担账户暂不可用", reason=str(exc),
            impact="本次未创建任何制作任务,未冻结算力",
            repair="联系管理员核对平台账户配置",
            actions=[{"id": "contact_admin", "label": "联系管理员", "type": "contact"}],
            retryable=False))
    if settlement.get("authority") is None:
        raise HTTPException(status_code=403, detail=_err(
            "ABILITY_NOT_GRANTED", "这次操作需要由团队负责人发起",
            reason=str(settlement.get("handoff_reason") or "组织计费未就绪"),
            impact="本次未创建任何制作任务", repair="交给团队负责人",
            actions=[{"id": "handoff", "label": "交给团队负责人", "type": "nav"}],
            retryable=False))

    payload = production_request_payload(
        draft_id=req.draft_id, draft_etag=req.expected_draft_etag,
        quote_id=req.quote_id, contract_revision_id=req.contract_revision_id,
        items=[i.model_dump() for i in req.items],
        expected_total_price_points=req.expected_total_price_points)
    rhash = request_hash(owner_user_id=int(identity["tenant_owner_user_id"]),
                         endpoint="/api/geo-douyin/batches", payload=payload)
    item_dicts = [i.model_dump() for i in req.items]

    def _create() -> dict:
        conn = get_connection()
        try:
            cur = conn.cursor()

            # ① RFC R1 四前置。任一不满足 → activation_pending,**零 claim 零资金**。
            blockers = activation_blockers(
                cur, quote_id=int(req.quote_id),
                contract_lane_enabled=contract_lane_enabled(),
                sidecar_schema_blockers=[], image_note_schema_blockers=[])
            if blockers:
                conn.rollback()
                return {"activation_pending": True, "blockers": blockers}

            # ② 幂等根。🔴 [返工 · P0-02] 原来**根本没调** claim_request:
            #    handler 每次自造 uuid,双击就是两个批次。WP2 建的原子幂等
            #    (ON CONFLICT DO NOTHING RETURNING + 三者比对)是真的,
            #    只是这条链从没把它接上 —— 「模块真实但执行链没接通」的标本。
            #
            # 🔴 [返工 2026-08-18 · 链 3] batch_id 必须**先于** claim 生成:
            #    `ck_publish_idem_kind_shape` 要求 `record_kind='root'` 的行
            #    `command_id IS NOT NULL`。原来这里不传 command_id ⇒ 每一次
            #    create-batch 在 claim 那一步就 CheckViolation **500**,
            #    比 uuid 那几处更早,任何真实请求都到不了后面。
            #    这条只有「真 HTTP 打真库」发现得了:模型层看不见 CHECK 约束。
            batch_id = str(_uuid.uuid4())
            is_new, claimed = claim_request(
                cur, request_id=req.request_id,
                endpoint="/api/geo-douyin/batches",
                owner_user_id=int(identity["tenant_owner_user_id"]),
                expected_hash=rhash, identity=identity,
                command_id="prodbatch:" + batch_id)
            if not is_new:
                # 回放:同一 request_id 同一 payload → 返回上次结果,零新增副作用。
                conn.commit()
                replay = claimed.get("response_json") or {}
                return {"activation_pending": False, "replayed": True,
                        "batch_id": replay.get("batch_id"),
                        "items": replay.get("items") or []}

            # ③ 组织审批门(规格 §7.3 · P0-07)。
            #    🔴 位置:claim 之后、**逐项重算/槽位 CAS/冻结/建单之前**。
            #       晚一格就不叫闸 —— 冻结之后被拒还要再退一次,
            #       建单之后被拒等于事后追认。
            approval = require_approval(
                cur, identity=identity, action_type=ACTION_SPEND,
                estimated_points=int(req.expected_total_price_points),
                feature_code=FEATURE_CODE_IMAGE_POST, payload_hash=rhash)

            # ④ 逐项**服务端重算** + 确认锁。🔴 原来是
            #    `expected_total // len(items)` 把总价摊平 ——
            #    卡数不同的两项价格本就不同,摊平之后每一笔冻结都是错的;
            #    而且它信的是**客户端报的总价**,服务端从未自己算过。
            recomputed = _recompute_items(item_dicts)
            for item, calc in zip(item_dicts, recomputed):
                assert_price_unchanged(
                    expected_fingerprint=str(item.get("expected_price_fingerprint") or ""),
                    actual_fingerprint=calc["fingerprint"],
                    operation=OPERATION_PRODUCTION)
            server_total = sum(c["final_price_points"] for c in recomputed)
            if server_total != int(req.expected_total_price_points):
                # 总价确认锁:零冻结零创建,让用户重新确认(唯一保留的二次确认)。
                raise PriceChanged(
                    f"服务端总价 {server_total} 与确认时的 "
                    f"{int(req.expected_total_price_points)} 不一致")

            # ④ 合同修订号取自**槽位行**,不取自请求(P0-08 同族)。
            #    批次/post/task 三张表都要落它;取错等于整批挂在错的合同版本下。
            #    这里顺带把全部槽位锁住 —— 同一事务内后面 claim 用的就是这几行。
            slot_authority = {
                item["delivery_slot_key"]: load_slot_authority(
                    cur, slot_key=item["delivery_slot_key"])
                for item in item_dicts
            }
            revisions = {int(s["contract_revision_id"]) for s in slot_authority.values()}
            if len(revisions) != 1:
                raise SlotIdentityMismatch(
                    f"本批槽位跨了多个合同修订 {sorted(revisions)},请分批提交")
            contract_revision_id = revisions.pop()
            if (req.contract_revision_id is not None
                    and int(req.contract_revision_id) != contract_revision_id):
                raise SlotIdentityMismatch(
                    f"页面声明的合同修订 {req.contract_revision_id} 与槽位所属的 "
                    f"{contract_revision_id} 不一致,请刷新交付计划")

            cur.execute(
                "INSERT INTO geo_douyin_production_batches ("
                " batch_id, tenant_owner_user_id, payer_user_id, actor_user_id,"
                " brand_id, quote_id, contract_revision_id, etag_revision,"
                " request_id, request_hash, expected_total_price_points, status,"
                # 🔴 审批证据随批次落库:「这批是按哪条策略、哪次审批放行的」
                #    是事后追责唯一答得出的问题。闸判了却不留痕 = 判据只能验行为
                #    不能验事实。三列都是 034 已有的,零迁移。
                " approval_request_id, approval_policy_version, approval_payload_hash,"
                " created_by, created_at, updated_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s,'accepted',"
                "         %s,%s,%s,%s,now(),now())"
                " ON CONFLICT DO NOTHING RETURNING batch_id",
                (batch_id, int(identity["tenant_owner_user_id"]),
                 int(identity["payer_user_id"]), int(identity["actor_user_id"]),
                 brand_id, int(req.quote_id), contract_revision_id,
                 # 🔴 原为 `req.request_id if hasattr(req, "request_id") else batch_id`。
                 #    模型上没有该字段时 hasattr **恒假**,永远走 else ——
                 #    一个看起来处理了两种情况、实际只有一条活路的分支。
                 req.request_id,
                 rhash, int(req.expected_total_price_points),
                 (str(approval["approval_request_id"])
                  if approval.get("approval_request_id") else None),
                 (str(approval["policy_version"])
                  if approval.get("policy_version") else None),
                 str(approval.get("payload_hash") or rhash),
                 int(identity["actor_user_id"])))
            if cur.fetchone() is None:
                raise IdempotencyConflict("这批制作已经提交过了")

            out_items = []
            for ordinal, item in enumerate(item_dicts, start=1):
                # ② slot CAS —— 零行即冲突,整项零创建零冻结。
                # 🔴 [返工 · P0-08] `contract_revision_id / plan_run_id` 不再由这里
                #    编造(原来分别是 `or 0` 和字面 0,而两列都是 NOT NULL + FK ⇒
                #    每次真实 claim 必 ForeignKeyViolation)。现在由 delivery_slots
                #    从**槽位行**取;这里传的 quote/brand 降级为核对项。
                claim_slot(cur, slot_key=item["delivery_slot_key"],
                           expected_version=int(item.get("expected_slot_version") or 0),
                           identity=identity,
                           contract_revision_id=req.contract_revision_id,
                           quote_id=int(req.quote_id), brand_id=brand_id,
                           topic_ref=item.get("topic_ref"))

                # ③ 建 post(带完整商业交付身份;counts_toward_contract 显式 true)
                cur.execute(
                    "INSERT INTO geo_douyin_posts ("
                    " brand_id, created_by, keyword, content_type, status,"
                    " tenant_owner_user_id, payer_user_id, actor_user_id, quote_id,"
                    " contract_revision_id, delivery_slot_key, counts_toward_contract,"
                    " source_mode, production_batch_id, batch_item_request_id,"
                    " batch_item_ordinal, topic_ref)"
                    " VALUES (%s,%s,%s,'image_post','draft',%s,%s,%s,%s,%s,%s,TRUE,"
                    " 'contract',%s,%s,%s,%s) RETURNING id",
                    (brand_id, int(identity["actor_user_id"]), "",
                     int(identity["tenant_owner_user_id"]), int(identity["payer_user_id"]),
                     int(identity["actor_user_id"]), int(req.quote_id),
                     contract_revision_id, item["delivery_slot_key"],
                     batch_id, item["item_request_id"], ordinal, item.get("topic_ref")))
                post_id = int(dict(cur.fetchone())["id"])

                # ④ 逐项 freeze —— 必须在建 task **之前**。
                #
                # 🔴 [返工 2026-08-18 · 链 3] 原先是「先 INSERT task(带
                #    settlement_authority、不带句柄)→ freeze → UPDATE 补句柄」。
                #    但 034 的 `ck_geo_douyin_task_authority_shape` 要求
                #    `direct_freeze` 的行在**写入那一刻**就必须 freeze_id /
                #    freeze_table / payer_user_id / reserved_amount 四格齐备 ⇒
                #    INSERT 当场 CheckViolation **500**。把句柄补在后一句 UPDATE，
                #    在有 CHECK 的表上根本走不到那一句。
                #    顺序换了，**事务边界没换**：仍然同一个 cursor、
                #    同一个事务，任一步异常整体 rollback(规格 §8.2)。
                supersede_active_tasks(cur, geo_post_id=post_id)
                # 🔴 `geo_douyin_post_tasks.task_ref` 是 **varchar(64)**。
                #    原式 `imgnote:<uuid36>:<uuid36>` = 81 字符 ⇒
                #    StringDataRightTruncation **500**。去连字符取 32 位 + 序号：
                #    `imgnote:<32>:<n>` ≯ 45 字符，且在批次内唯一(表上有 UNIQUE)。
                task_ref = "imgnote:" + batch_id.replace("-", "") + ":" + str(ordinal)

                import asyncio as _a
                loop = _a.new_event_loop()
                try:
                    _calc = recomputed[ordinal - 1] if ordinal - 1 < len(recomputed) else None
                    handle = loop.run_until_complete(freeze_one_item(
                        cur, payer_user_id=int(settlement["payer_user_id"]),
                        feature_code=FEATURE_CODE_IMAGE_POST, task_ref=task_ref,
                        brand_id=brand_id,
                        # 🔴 逐项**重算价**，不是总价摊平；额外卡加价随之进冻结。
                        expected_points=int(_calc["final_price_points"]),
                        extra_cost=int(_calc["extra_points"]),
                        authority=str(settlement["authority"])))
                finally:
                    loop.close()

                # ⑤ 建 task：句柄**整组**随行写入，不留一个“声称已记账但没句柄”的窗口。
                #    status 按规格 §5.7 的**首态**：`queued`(不是老链的 `pending`)。
                cur.execute(
                    "INSERT INTO geo_douyin_post_tasks ("
                    " post_id, user_id, task_ref, status, request_id, idempotency_key,"
                    " endpoint, production_batch_id, batch_item_request_id,"
                    " batch_item_ordinal, request_hash, settlement_status,"
                    " settlement_authority, payer_user_id, freeze_id, freeze_table,"
                    " reserved_amount, physical_split_snapshot, billing_mode,"
                    " progress_total)"
                    " VALUES (%s,%s,%s,%s,%s,%s,'/api/geo-douyin/batches',"
                    " %s,%s,%s,%s,'frozen',%s,%s,%s,%s,%s,%s::jsonb,%s,%s)"
                    " RETURNING id",
                    (post_id, int(identity["payer_user_id"]), task_ref,
                     TASK_STATUS_QUEUED,
                     item["item_request_id"], item["item_request_id"],
                     batch_id, item["item_request_id"], ordinal, rhash,
                     settlement["authority"], int(handle["payer_user_id"]),
                     handle["freeze_id"], handle["freeze_table"],
                     handle["reserved_amount"],
                     _json.dumps(handle["physical_split_snapshot"], ensure_ascii=False),
                     # P1-6:这一行的钱按哪套口径结算,显式写进列里,不靠反推。
                     str(settlement["billing_mode"]),
                     int((item.get("settings") or {}).get("card_count") or 0)))
                task_id = int(dict(cur.fetchone())["id"])
                begin_generation(cur, geo_post_id=post_id, task_id=task_id)
                out_items.append({"item_request_id": item["item_request_id"],
                                  "geo_post_id": post_id, "task_id": task_id,
                                  "status": TASK_STATUS_QUEUED})

            # ⑥ 结果回写幂等行 —— 回放才答得出同一个 batch_id（见 record_response）
            record_response(cur, request_id=req.request_id,
                            owner_user_id=int(identity["tenant_owner_user_id"]),
                            response={"batch_id": batch_id, "items": out_items})
            conn.commit()          # ← provider(生成)只能在这之后开始
            return {"activation_pending": False, "batch_id": batch_id,
                    "items": out_items}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    try:
        out = await asyncio.to_thread(_create)
    except PriceChanged as exc:
        raise HTTPException(status_code=409, detail=_err(
            "PRICE_CHANGED", "算力已更新,请重新确认", reason=str(exc),
            impact="本次未创建任何制作任务,未冻结算力",
            repair="返回核对区查看新的算力后重新提交",
            actions=[{"id": "recheck", "label": "重新确认", "type": "retry"}],
            retryable=True))
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=_err(
            "IDEMPOTENCY_CONFLICT", "这批制作已经提交过了", reason=str(exc),
            impact="本次未新增任务、未冻结算力", repair="加载已提交的批次",
            actions=[{"id": "load_batch", "label": "查看本次制作", "type": "nav"}]))
    except SlotClaimConflict as exc:
        raise HTTPException(status_code=409, detail=_err(
            "SLOT_CLAIM_CONFLICT", "有一篇已经由同事开始制作了", reason=str(exc),
            impact="整批未创建、未冻结算力", repair="刷新本报价的待制作列表",
            actions=[{"id": "refresh_plan", "label": "刷新列表", "type": "retry"}]))
    except SlotIdentityMismatch as exc:
        # 🔴 归 409 而不是 403:它既可能是"页面拿的是旧计划"(常见、可自愈),
        #    也可能是"有人在拿别人的 slot_key 试"(罕见)。对前者给刷新动作,
        #    对后者什么也没泄露 —— 两种成因共用一句不区分的回答是刻意的。
        raise HTTPException(status_code=409, detail=_err(
            "SLOT_SCOPE_MISMATCH", "交付计划已更新,请刷新后重新提交", reason=str(exc),
            impact="整批未创建、未冻结算力", repair="刷新本报价的待制作列表",
            actions=[{"id": "refresh_plan", "label": "刷新列表", "type": "retry"}]))
    except ApprovalPending as exc:
        # 零副作用的等待态:批次都没建,更没有冻结。前端按"等审批"渲染。
        raise HTTPException(status_code=409, detail=_err(
            "APPROVAL_REQUIRED", "这次制作需要团队负责人审批", reason=str(exc),
            impact="本次未创建任何制作任务,未冻结算力",
            repair="等待团队负责人在审批中心处理,或改由负责人发起",
            actions=[{"id": "open_approvals", "label": "查看审批", "type": "nav"},
                     {"id": "handoff", "label": "交给团队负责人", "type": "nav"}],
            approval_request_id=exc.approval_request_id,
            policy_version=exc.policy_version))
    except SlotAuthorityMissing as exc:
        # 前置没到位 ⇒ 与四前置同一种降级形态:零 claim 零资金,不是 500。
        raise HTTPException(status_code=409, detail=_err(
            "PLAN_NOT_COMPILED", "这张报价的交付计划还没编译完成", reason=str(exc),
            impact="本次未创建任何制作任务,未冻结算力",
            repair="稍后刷新;若长时间未就绪请联系管理员核对开通状态",
            actions=[{"id": "refresh_plan", "label": "刷新", "type": "retry"},
                     {"id": "contact_admin", "label": "联系管理员核对",
                      "type": "contact"}]))
    except (FundingHandleInvalid, FreezeProducedNoHandle) as exc:
        raise HTTPException(status_code=500, detail=_err(
            "COMMAND_CREATE_FAILED", "提交失败,算力未被扣除", reason=str(exc),
            impact="已整体回滚:未创建任务、未冻结算力", repair="稍后重试",
            actions=[{"id": "retry", "label": "重试", "type": "retry"}]))

    if out.get("activation_pending"):
        # RFC §3 降级形态:零 claim、零资金、零制作
        return {"status": "activation_pending", "user_message": "制作计划准备中",
                "repair_hint": "稍后刷新;若长时间未就绪请联系管理员核对开通状态",
                "actions": [{"id": "refresh_plan", "label": "刷新", "type": "retry"},
                            {"id": "contact_admin", "label": "联系管理员核对",
                             "type": "contact"}],
                "next_action": "refresh_plan", "retryable": True,
                "blockers": out["blockers"]}
    return {"status": "accepted", "batch_id": out["batch_id"], "items": out["items"]}


@router.get("/api/geo-douyin/batches/{batch_id}")
async def api_get_batch(batch_id: str, request: Request):
    """批次动态汇总与逐项状态。只读,不过 mutation 闸,但仍走 schema readiness。

    🔴 [返工 2026-08-18 · P0-03/04] 原来这里是 `raise HTTPException(404)` **无条件**。
       第一页提交后就靠它轮询进度 —— 恒 404 意味着**任何批次都查不到状态**,
       界面永远停在"已提交"。而"恒 404"在行为上与"这个批次不存在"长得一模一样,
       所以既有的 route 判据(只证明装饰器存在)全绿。
       教训写在 `test_real_http_contract.py` 那条源码形态判据里。

    🔴 租户隔离写在 SQL 里(`tenant_owner_user_id = %(owner)s`),不是先查再比 ——
       先查再比会在"查得到但不属于你"时泄露批次存在性。
    """
    import asyncio

    from db.connection import get_connection
    from services.geo_douyin.contract_states import (
        describe_task_status, project_batch_status,
    )

    _user(request)
    identity = _resolve_identity(request)
    blockers = _schema_blockers()
    if blockers:
        raise HTTPException(status_code=503, detail=error_payload(SchemaNotReady(blockers)))

    try:
        batch_key = normalize_uuid(batch_id)
    except ValueError:
        # 不是 uuid ⇒ 不可能是本链的批次。给 404 而不是 500,
        # 也不给 422 —— 路径参数格式错在用户眼里就是"没有这个批次"。
        raise HTTPException(status_code=404, detail=_err(
            "BATCH_NOT_FOUND", "找不到这次制作", reason="批次标识格式不对",
            impact="没有任何变化", repair="返回制作页重新提交",
            actions=[{"id": "back", "label": "返回制作页", "type": "nav"}],
            retryable=False))

    def _load() -> Optional[dict]:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT batch_id, status, brand_id, quote_id, contract_revision_id,"
                "       expected_total_price_points, created_at, updated_at"
                "  FROM geo_douyin_production_batches"
                " WHERE batch_id = %(batch_id)s"
                "   AND tenant_owner_user_id = %(owner)s",
                {"batch_id": batch_key,
                 "owner": int(identity["tenant_owner_user_id"])})
            head = cur.fetchone()
            if head is None:
                return None
            cur.execute(
                "SELECT t.id AS task_id, t.status, t.stage, t.progress_done,"
                "       t.progress_total, t.error_msg, t.settlement_status,"
                "       t.batch_item_request_id, t.batch_item_ordinal, t.reserved_amount,"
                "       p.id AS geo_post_id, p.delivery_slot_key, p.status AS post_status"
                "  FROM geo_douyin_post_tasks t"
                "  JOIN geo_douyin_posts p ON p.id = t.post_id"
                " WHERE t.production_batch_id = %(batch_id)s"
                "   AND t.superseded_at IS NULL"
                " ORDER BY t.batch_item_ordinal",
                {"batch_id": batch_key})
            rows = [dict(r) for r in (cur.fetchall() or [])]
            return {"head": dict(head), "items": rows}
        finally:
            conn.close()

    out = await asyncio.to_thread(_load)
    if out is None:
        raise HTTPException(status_code=404, detail=_err(
            "BATCH_NOT_FOUND", "找不到这次制作", reason="批次不存在或不属于当前账户",
            impact="没有任何变化", repair="返回制作页重新提交",
            actions=[{"id": "back", "label": "返回制作页", "type": "nav"}],
            retryable=False))

    head, rows = out["head"], out["items"]
    items = []
    for row in rows:
        detail = describe_task_status(row.get("status"))
        state = detail["state"]
        items.append({
            "item_request_id": str(row.get("batch_item_request_id") or ""),
            "delivery_slot_key": str(row.get("delivery_slot_key") or ""),
            "geo_post_id": row.get("geo_post_id"),
            "state": state,
            "settlement_status": row.get("settlement_status"),
            "progress": {"done": int(row.get("progress_done") or 0),
                         "total": int(row.get("progress_total") or 0)},
            # 🔴 失败原因**原样**下发机器可读的那一份;界面文案由前端
            #    `BATCH_ITEM_COPY` 翻译。后端不在这里编用户文案 ——
            #    编了就会与前端那张表打架,而打架时没人知道哪边是对的。
            "failure_reason": (row.get("error_msg") or None) if state == "failed" else None,
            # 🔴 归口是封闭的,但**原值不丢**:认不出来的那个词原样带出来,
            #    排查时才知道库里到底写的是什么。认得出来的一律不带这个键 ——
            #    正常情况下前端不该看到工程词。
            **({} if detail["recognized"] else {"state_raw": detail["raw"]}),
        })

    # 汇总是**派生值**:按逐项状态数出来,不读第二处真值。
    summary = {"total": len(items)}
    for state in ("queued", "running", "settlement_pending", "ready",
                  "failed", "cancelled", "needs_action"):
        summary[state] = sum(1 for i in items if i["state"] == state)
    return {
        "status": "success",
        "batch_id": str(head["batch_id"]),
        # 🔴 [第 3 棒 · Codex R2 P1] 对外状态是**派生投影**,不是表头那一列。
        #    `geo_douyin_production_batches.status` 建行写 'accepted' 之后
        #    全仓无人更新 ⇒ 直接下发它,批次做完了界面上也永远是"已接受"。
        #    表头那一列降级为审计留痕(`record_state`),不再承担对外语义。
        "state": project_batch_status([i["state"] for i in items]),
        "record_state": str(head.get("status") or ""),
        "quote_id": head.get("quote_id"),
        "brand_id": head.get("brand_id"),
        "expected_total_price_points": head.get("expected_total_price_points"),
        "items": items,
        "summary": summary,
    }


# ─────────────────────────────────────────────────────────────
# 4. 发布预览(只读验证,不上传、不占频控、不冻结、不调渠道)
# ─────────────────────────────────────────────────────────────

@router.post("/api/meijiehezi/image-notes/publish-preview")
async def api_image_note_publish_preview(req: PublishPreviewRequest, request: Request):
    """规格 §7.2:只带**已持久化**的 artifact 身份;服务端只读验证 + 重跑资格/频控/定价。

    该响应只是确认快照 —— 不上传 artifact、不占频控容量、不冻结资金、不调用发布渠道。
    """
    _user(request)
    _guard(request, action="publish.preview", ability=ABILITY_PUBLISH)

    from services.geo_douyin.publish_command import assert_one_to_one, OneToOneViolation

    payload = [i.model_dump() for i in req.items]
    for row in payload:
        row.setdefault("expected_price_fingerprint", "preview")
    try:
        assert_one_to_one(payload)
    except OneToOneViolation as exc:
        raise HTTPException(status_code=409, detail={
            "code": "ONE_TO_ONE_REQUIRED", "message": "一篇作品只能对应一个账号",
            "reason": str(exc), "impact": "本次未下单、未冻结算力、未联系发布渠道",
            "repair_hint": "拆成一篇一个账号后重新提交",
            "actions": [{"id": "fix_mapping", "label": "返回修改分配", "type": "nav"}],
            "next_action": "fix_mapping", "retryable": True,
        })
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "code": "EMPTY_BATCH", "message": "还没有选择要发布的作品",
            "reason": str(exc), "impact": "本次未下单",
            "repair_hint": "先选择作品", "actions": [
                {"id": "back_to_select", "label": "返回选择", "type": "nav"}],
            "next_action": "back_to_select", "retryable": True,
        })
    # ── 真定价 + 真资格 ──────────────────────────────────────
    import asyncio

    from db.connection import get_connection
    from services.geo_douyin.account_eligibility import evaluate_accounts
    from services.geo_douyin.contract_pricing import (
        OPERATION_PUBLISH, canonical_price_snapshot, publish_fingerprint,
    )
    from services.media_price_projection import get_markup, resolve_price_points

    identity = _resolve_identity(request)
    media_ids = sorted({int(i.media_id) for i in req.items})

    def _resolve() -> dict:
        conn = get_connection()
        try:
            cur = conn.cursor()
            markup = get_markup()
            elig = evaluate_accounts(
                cur, media_ids, daily_limit=image_note_daily_limit(),
                price_resolver=lambda row: {
                    # 🔴 投放价**只**复用 media_price_projection.resolve_price_points ——
                    #    它自己的 docstring 写着「本函数是那处的统一口径」
                    #    (publish_recommendation 曾漏乘 markup 少收钱)。
                    #    同一 resolver 同时服务目录显示/筛选/排序/preview/下单/结算
                    #    (规格 §6.2)。这里绝不调制作定价源,也不自己拼价公式。
                    "final_price_points": resolve_price_points(row, markup),
                    "price_version": f"markup:{markup}",
                })
            return {"markup": markup, "eligibility": elig}
        finally:
            conn.close()

    resolved = await asyncio.to_thread(_resolve)
    elig = resolved["eligibility"]

    items_out: list[dict[str, Any]] = []
    total = 0
    blocked: list[dict[str, Any]] = []
    for item in req.items:
        account = elig.get(int(item.media_id))
        if account is None or not account.available:
            blocked.append({
                "item_request_id": item.item_request_id,
                "media_id": int(item.media_id),
                "reason_code": (account.reason_code if account else "ACCOUNT_NOT_FOUND"),
            })
            continue
        points = int(account.final_price_points or 0)
        total += points
        items_out.append({
            "item_request_id": item.item_request_id,
            "geo_post_id": int(item.geo_post_id),
            "post_revision_id": item.post_revision_id,
            "prepared_artifact_id": item.prepared_artifact_id,
            "manifest_hash": item.manifest_hash,
            "media_id": int(item.media_id),
            # 🔴 [返工 · Codex P1-4] 逐项 eligibility。前端 `PublishPreviewItem`
            #    声明里一直有这个字段,后端从来没发过 ⇒ 页面拿到 undefined,
            #    只能回落到目录里的**乐观值**,于是"预览说能发、提交说不能发"。
            #    真资格判定就在下面这个 account 对象上,发出去而不是丢掉。
            "eligibility": {
                "eligible": bool(account.available),
                "reason": account.reason_code,
                "today_remaining": int(account.today_remaining),
            },
            "final_price_points": points,
            "publish_price_fingerprint": publish_fingerprint(
                feature_code=PUBLISH_FEATURE_CODE, media_id=int(item.media_id),
                final_price_points=points,
                markup_version=str(account.price_version or ""),
                resolver_version="media_price_projection.v1",
                catalog_version="mhz_short_video"),
            "today_remaining": int(account.today_remaining),
            "price_snapshot": canonical_price_snapshot(
                operation=OPERATION_PUBLISH, final_price_points=points,
                feature_code=PUBLISH_FEATURE_CODE,
                resolver_version="media_price_projection.v1",
                catalog_version="mhz_short_video",
                markup_version=str(account.price_version or ""),
                policy_version=None, preview_expires_at="", confirmed_at=None),
        })

    if blocked and not items_out:
        raise HTTPException(status_code=409, detail=_err(
            "ACCOUNT_NOT_ELIGIBLE", "选中的账号暂时都不能发图文",
            reason="账号资格或今日余量不满足", impact="本次未下单、未冻结算力",
            repair="换账号或调整筛选",
            actions=[{"id": "change_account", "label": "换账号", "type": "nav"}],
            blocked=blocked))

    # 🔴 该响应只是**确认快照**:不上传 artifact、不占频控容量、不冻结资金、
    #    不调用发布渠道(规格 §7.2)。上面全程只有读。
    return {"status": "success", "items": items_out,
            "total_price_points": total, "blocked": blocked}


# ─────────────────────────────────────────────────────────────
# 5. 一篇一账号批量提交
# ─────────────────────────────────────────────────────────────

@router.post("/api/meijiehezi/image-notes/publish-batch")
async def api_image_note_publish_batch(req: PublishBatchRequest, request: Request):
    """规格 §7.3。一个 item 恰好一个作品版本 + 一个账号;
    同一 active post revision 映射两账号时**整批**拒绝,零订单零预占零冻结零外调。
    """
    _user(request)
    _guard(request, action="publish.submit", ability=ABILITY_PUBLISH)

    from services.geo_douyin.publish_command import assert_one_to_one, OneToOneViolation

    try:
        assert_one_to_one([i.model_dump() for i in req.items])
    except OneToOneViolation as exc:
        raise HTTPException(status_code=409, detail={
            "code": "ONE_TO_ONE_REQUIRED", "message": "一篇作品只能对应一个账号",
            "reason": str(exc),
            "impact": "整批未提交:未下单、未占用账号余量、未冻结算力、未联系发布渠道",
            "repair_hint": "拆成一篇一个账号后重新提交",
            "actions": [{"id": "fix_mapping", "label": "返回修改分配", "type": "nav"}],
            "next_action": "fix_mapping", "retryable": True,
        })
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "code": "EMPTY_BATCH", "message": "还没有选择要发布的作品",
            "reason": str(exc), "impact": "本次未下单",
            "repair_hint": "先选择作品",
            "actions": [{"id": "back_to_select", "label": "返回选择", "type": "nav"}],
            "next_action": "back_to_select", "retryable": True,
        })
    # ── §8.2 全序:claim → 解析 payer → 校验 → 同一事务 reservation/建单/逐项 freeze ──
    #
    # 🔴 [WO-B ② 2026-08-20] 这一整条**搬进** `services/geo_douyin/publish_batch_core.py`。
    #    搬家的理由不是"handler 太长":小榜五阶段的 execute adapter 要接的就是这一条,
    #    而在 execute 那边重写一遍等于**两份资金链** —— 同一谓词写两处,
    #    必有一处没人验,漂移那天的表现是"扣了钱没建单"或"建了单没冻钱"。
    #    搬家是**纯移动**:顺序、异常类型、每一句 SQL 一个字没改;
    #    HTTP 错误映射(下面那一串 except)留在 handler,那一层本来就该在这里。
    import asyncio

    from db.connection import get_connection
    from services.geo_douyin.artifact_prepare import ArtifactRequestConflict
    from services.geo_douyin.contract_freeze import FreezeProducedNoHandle
    from services.geo_douyin.contract_funding import (
        FundingHandleInvalid, PlatformAccountUnavailable, resolve_settlement_authority,
    )
    from services.geo_douyin.contract_idempotency import IdempotencyConflict
    from services.geo_douyin.contract_pricing import PriceChanged, PriceFingerprintMismatch
    from services.geo_douyin.contract_approval import ApprovalPending
    from services.geo_douyin.legal_gate import (
        LegalGateBlocked, repair_hint as legal_repair_hint,
    )
    from services.geo_douyin.publish_command import CapacityExceeded, PublishAttemptConflict
    from services.geo_douyin.publish_batch_core import materialize_publish_batch

    user = _user(request)
    identity = _resolve_identity(request)
    endpoint = "/api/meijiehezi/image-notes/publish-batch"

    try:
        settlement = resolve_settlement_authority(
            is_admin=bool(user.get("is_admin")),
            organization_id=identity.get("organization_id"),
            organization_billing_ready=bool(identity.get("organization_billing_ready")),
            owner_user_id=int(identity["payer_user_id"]))
    except PlatformAccountUnavailable as exc:
        # fail-closed:绝不回落成「扣服务商」(2026-08-16 生产事故形态)
        raise HTTPException(status_code=503, detail=_err(
            "PUBLISH_CHANNEL_UNAVAILABLE", "平台承担账户暂不可用", reason=str(exc),
            impact="本次未下单、未冻结算力、未联系发布渠道",
            repair="联系管理员核对平台账户配置",
            actions=[{"id": "contact_admin", "label": "联系管理员", "type": "contact"}],
            retryable=False))

    if settlement.get("authority") is None:
        raise HTTPException(status_code=403, detail=_err(
            "ABILITY_NOT_GRANTED", "这次操作需要由团队负责人发起",
            reason=str(settlement.get("handoff_reason") or "组织计费未就绪"),
            impact="本次未下单、未冻结算力",
            repair="交给团队负责人,或联系管理员开通团队计费",
            actions=[{"id": "handoff", "label": "交给团队负责人", "type": "nav"},
                     {"id": "contact_admin", "label": "联系管理员", "type": "contact"}],
            retryable=False))

    item_dicts = [i.model_dump() for i in req.items]

    def _materialize() -> dict:
        conn = get_connection()
        try:
            cur = conn.cursor()
            out = materialize_publish_batch(
                cur,
                request_id=req.request_id,
                endpoint=endpoint,
                identity=identity,
                settlement=settlement,
                items=item_dicts,
                expected_total_price_points=int(req.expected_total_price_points),
                daily_limit=image_note_daily_limit(),
                feature_code=PUBLISH_FEATURE_CODE,
                command_id="pubcmd_" + str(req.request_id))
            if out.get("replayed"):
                conn.rollback()
                return out
            conn.commit()          # ← provider 只能在这之后开始
            return out
        except Exception:
            conn.rollback()        # 任一步异常整体 rollback
            raise
        finally:
            conn.close()

    try:
        out = await asyncio.to_thread(_materialize)
    except PublishAttemptConflict as exc:
        raise HTTPException(status_code=409, detail=_err(
            "PUBLISH_ATTEMPT_EXISTS", "请先查看这条作品的发布结果", reason=str(exc),
            impact="本次未新增订单、未冻结算力、未联系发布渠道",
            repair="查看已有提交；尚在处理的等待渠道回执，记录异常时联系管理员",
            actions=[{"id": "load_command", "label": "查看已提交结果", "type": "nav"},
                     {"id": "contact_admin", "label": "联系管理员", "type": "contact"}],
            retryable=False, command_id=exc.command_id))
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=_err(
            "IDEMPOTENCY_CONFLICT", "这个请求已经提交过了", reason=str(exc),
            impact="本次未新增订单、未冻结算力、未联系发布渠道",
            repair="加载已提交的结果,或用新的请求重试",
            actions=[{"id": "load_command", "label": "查看已提交结果", "type": "nav"},
                     {"id": "retry", "label": "重新提交", "type": "retry"}]))
    except (PriceFingerprintMismatch, PriceChanged) as exc:
        raise HTTPException(status_code=409, detail=_err(
            "PRICE_CHANGED", "账号算力有变化,请重新确认", reason=str(exc),
            impact="本次尚未下单或冻结算力", repair="刷新逐项算力并再次确认",
            actions=[{"id": "refresh_and_confirm", "label": "刷新并重新确认",
                      "type": "retry"}]))
    except ArtifactRequestConflict as exc:
        raise HTTPException(status_code=409, detail=_err(
            "ARTIFACT_STALE", "发布素材需要重新准备", reason=str(exc),
            impact="本次未下单、未冻结算力", repair="重新准备并确认当前版本",
            actions=[{"id": "reprepare", "label": "重新准备", "type": "retry"}]))
    except ApprovalPending as exc:
        raise HTTPException(status_code=409, detail=_err(
            "APPROVAL_REQUIRED", "这次投放需要团队负责人审批", reason=str(exc),
            impact="本次未下单、未冻结算力、未联系发布渠道",
            repair="等待团队负责人在审批中心处理,或改由负责人发起",
            actions=[{"id": "open_approvals", "label": "查看审批", "type": "nav"},
                     {"id": "handoff", "label": "交给团队负责人", "type": "nav"}],
            approval_request_id=exc.approval_request_id,
            policy_version=exc.policy_version))
    except LegalGateBlocked as exc:
        # 🔴 广告法命中是**局部可修**的问题,所以逐条下发位置与词,
        #    而不是甩一句"文案不合规"让用户自己找。
        raise HTTPException(status_code=409, detail=_err(
            "LEGAL_TERM_BLOCKED", "文案里有广告法明令禁止的说法",
            reason=f"命中 {len(exc.hits)} 处(目录版本 {exc.pack_version})",
            impact="本次未下单、未冻结算力、未联系发布渠道",
            repair=legal_repair_hint(exc.hits),
            actions=[{"id": "edit_copy", "label": "去修改文案", "type": "nav"}],
            hits=exc.hits, pack_version=exc.pack_version))
    except CapacityExceeded as exc:
        raise HTTPException(status_code=409, detail=_err(
            "FREQUENCY_LIMIT_REACHED", "账号今天的额度不够", reason=str(exc),
            impact="本次未下单、未冻结算力、未联系发布渠道",
            repair="换账号或明天再发",
            actions=[{"id": "change_account", "label": "换账号", "type": "nav"}]))
    except (FundingHandleInvalid, FreezeProducedNoHandle) as exc:
        raise HTTPException(status_code=500, detail=_err(
            "COMMAND_CREATE_FAILED", "提交失败,算力未被扣除", reason=str(exc),
            impact="已整体回滚:未下单、未冻结算力、未联系发布渠道",
            repair="稍后重试;若反复出现请联系管理员",
            actions=[{"id": "retry", "label": "重试", "type": "retry"},
                     {"id": "contact_admin", "label": "联系管理员", "type": "contact"}]))

    if out.get("replayed"):
        return {"status": "accepted", "replayed": True,
                "command_id": "pubcmd_" + str(req.request_id)}
    return {"status": "accepted", "replayed": False,
            "command_id": out["command_id"], "command_status": "accepted",
            "summary": out["summary"], "items": out["items"]}


# ───────────────────────────────────────────────────────────
# 5b. 发布 command 读回与失败项重试（规格 §7.5 · Codex P0-04 后半）
# ───────────────────────────────────────────────────────────

class CommandRetryRequest(BaseModel):
    """重试**明确失败且已 release** 的项（规格 02 §330）。

    🔴 `item_request_ids` 是必填而不是"不传就全重试"：全重试会把
       `awaiting_sync`（结果未知）那些也投出去 —— 供应商可能已经发了。
       让调用方逐项点名，是把"选哪些"这个决定留在**看得见状态的那一层**。
    """
    request_id: str
    item_request_ids: List[UuidStr]


def _load_command(cur, *, command_request_id: str, owner_user_id: int) -> list:
    """从原 command 的发布根读当前末端，旧链接也能看到换账号后的结果。

    原命令和末端订单都校验归属；没有 attempt_root_id 的历史项仍按自身读取。
    """
    cur.execute(
        "SELECT i.id, i.item_request_id, i.status AS state, i.settlement_status,"
        "       i.media_id, COALESCE(NULLIF(i.media_name, ''), m.media_name, '') AS media_name, i.source_geo_post_id,"
        "       i.source_post_revision_id, i.cost_points, i.reject_reason,"
        "       i.retracted_at, i.replaced_by_source_id, i.terminal_at,"
        "       i.availability, i.attempt_no, i.last_submit_at AS external_started_at"
        "  FROM mhz_publish_order_items seed"
        "  JOIN mhz_publish_orders origin ON origin.id = seed.order_id"
        "  JOIN LATERAL ("
        "       SELECT candidate.* FROM mhz_publish_order_items candidate"
        "       JOIN mhz_publish_orders current_order ON current_order.id=candidate.order_id"
        "       WHERE current_order.user_id=%(owner)s"
        "         AND (candidate.id=seed.id OR (seed.attempt_root_id IS NOT NULL"
        "              AND candidate.attempt_root_id=seed.attempt_root_id"
        "              AND candidate.source_geo_post_id=seed.source_geo_post_id"
        "              AND candidate.source_post_revision_id=seed.source_post_revision_id))"
        "       ORDER BY candidate.attempt_no DESC NULLS LAST,candidate.id DESC LIMIT 1"
        "  ) i ON TRUE"
        "  LEFT JOIN mhz_short_video m ON m.id = i.media_id"
        " WHERE seed.command_request_id = %(cmd)s"
        "   AND origin.user_id = %(owner)s"
        " ORDER BY i.id",
        {"cmd": str(command_request_id), "owner": int(owner_user_id)})
    return [dict(r) for r in (cur.fetchall() or [])]


@router.get("/api/meijiehezi/image-notes/commands/{command_id}")
async def api_get_publish_command(command_id: str, request: Request):
    """command 的**动态**投影（规格 §7.5 八态优先级）。

    🔴 [返工 2026-08-18 · P0-04] 原来这个端点**不存在**：提交之后前端只能
       永远显示第一次返回的 `accepted`。`project_command_status` /
       `is_retryable` / `next_action_for` 三个 projector 早就写好了，
       但**零生产调用者** —— 与 durable worker 同一种死法。

    🔴 状态是**投影**不是存储列：它由当前全部有效 attempt 现算。
       存一列"command 状态"必然与逐项状态漂移，而漂移时没人知道哪边是真的。
    """
    import asyncio

    from db.connection import get_connection
    from services.geo_douyin.publish_command import (
        is_retryable, next_action_for, normalized_availability, project_command_status,
    )

    _user(request)
    identity = _resolve_identity(request)
    blockers = _schema_blockers()
    if blockers:
        raise HTTPException(status_code=503, detail=error_payload(SchemaNotReady(blockers)))

    # command_id 对外形如 `pubcmd_<request_id>`；去前缀拿回 request_id。
    request_id = command_id[len("pubcmd_"):] if command_id.startswith("pubcmd_") else command_id

    def _load() -> list:
        conn = get_connection()
        try:
            return _load_command(conn.cursor(), command_request_id=request_id,
                                 owner_user_id=int(identity["tenant_owner_user_id"]))
        finally:
            conn.close()

    attempts = await asyncio.to_thread(_load)
    if not attempts:
        raise HTTPException(status_code=404, detail=_err(
            "COMMAND_NOT_FOUND", "找不到这次投放", reason="投放不存在或不属于当前账户",
            impact="没有任何变化", repair="返回投放页重新提交",
            actions=[{"id": "back", "label": "返回投放页", "type": "nav"}],
            retryable=False))

    items = [{
        "item_request_id": str(a.get("item_request_id") or ""),
        "geo_post_id": a.get("source_geo_post_id"),
        "post_revision_id": a.get("source_post_revision_id"),
        "media_id": a.get("media_id"),
        "media_name": a.get("media_name") or "",
        "state": str(a.get("state") or ""),
        "settlement_status": a.get("settlement_status"),
        "availability": normalized_availability(a),
        # 🔴 `retryable` 是 projector 的**派生值**，不是 raw state：
        #    只有「明确失败 **且** 资金已 released」才可重试。
        #    结果未知一律不可重投 —— 供应商可能已经发了。
        "retryable": is_retryable(a),
        "next_action": next_action_for(a),
        "failure_reason": a.get("reject_reason") or None,
    } for a in attempts]
    return {
        "status": "success",
        "command_id": "pubcmd_" + request_id,
        "command_status": project_command_status(
            root_pending_approval=False, coordination_failed=False, attempts=attempts),
        "items": items,
        "summary": {"total": len(items),
                    "retryable": sum(1 for i in items if i["retryable"])},
    }


@router.post("/api/meijiehezi/image-notes/commands/{command_id}/retry")
async def api_retry_publish_command(command_id: str, req: CommandRetryRequest,
                                    request: Request):
    """把点名的失败项重新排队（规格 §330）。

    🔴 只接受 `is_retryable` 为真的项。判定复用**同一个** projector ——
       在这里另写一遍"什么算可重试"必然与读端漂移，而漂移的方向是
       "读端说不能重试、写端放行" ⇒ 重复外调。

    🔴 **本轮只做到"标记待重投"**：真正的 child attempt 建单与重新冻结
       由下一轮补 —— 因为老 item 的钱已经 release 了，直接把它改回 queued
       等于没有冻结却在跑。交付单 §5 已列为残项，**不许读成已完成**。
    """
    import asyncio

    from db.connection import get_connection
    from services.geo_douyin.publish_command import is_retryable

    _user(request)
    _guard(request, action="publish.retry", ability=ABILITY_PUBLISH)
    identity = _resolve_identity(request)
    request_id = command_id[len("pubcmd_"):] if command_id.startswith("pubcmd_") else command_id
    wanted = set(req.item_request_ids)

    def _mark() -> dict:
        conn = get_connection()
        try:
            cur = conn.cursor()
            attempts = _load_command(cur, command_request_id=request_id,
                                     owner_user_id=int(identity["tenant_owner_user_id"]))
            by_key = {str(a.get("item_request_id") or ""): a for a in attempts}
            missing = sorted(wanted - set(by_key))
            if missing:
                raise KeyError("这些项不属于本次投放：" + ", ".join(missing))
            blocked = sorted(k for k in wanted if not is_retryable(by_key[k]))
            if blocked:
                # 🔴 一条不合格就整批拒，不做"能重的重、不能重的跳过" ——
                #    部分执行会让用户以为全部重投了。
                raise ValueError(
                    "这些项当前不可重试（只有明确失败且算力已退回的才可以）："
                    + ", ".join(blocked))
            cur.execute(
                # 🔴 [#114] 生产 mhz_publish_order_items 82 列**没有 `updated_at`** ——
                #    这张表**有意**不用通用 updated_at,变更靠 `status_version` /
                #    `last_authoritative_event_at` / `retry_claim_version` 这类**语义化**列记。
                #    带着 updated_at 的 UPDATE 每次 UndefinedColumn ⇒ 重试认领**整条失败**。
                #    这里删掉该赋值,不找「近似列」硬凑:created_at / submitted_at 是事件时刻,
                #    与「最后一次被改」不是一回事,拿它顶会写出错的语义。
                "UPDATE mhz_publish_order_items"
                "   SET retry_claim_state = 'requested',"
                "       retry_claim_token = %(token)s,"
                "       retry_claim_version = COALESCE(retry_claim_version, 0) + 1"
                " WHERE id = ANY(%(ids)s::integer[])"
                "   AND status IN ('failed','rejected') AND settlement_status='released'"
                "   AND replaced_by_source_id IS NULL"
                "   AND (availability IS NULL OR availability <> 'replaced')"
                " RETURNING id",
                {"token": str(req.request_id),
                 "ids": [int(by_key[key]["id"]) for key in sorted(wanted)]})
            marked = len(cur.fetchall())
            if marked != len(wanted):
                raise ValueError("发布记录已有变化，请刷新投放结果后重试。")
            conn.commit()
            return {"marked": marked}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    try:
        out = await asyncio.to_thread(_mark)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=_err(
            "ITEM_NOT_IN_COMMAND", "有项不属于这次投放", reason=str(exc.args[0]),
            impact="本次未重投任何一项", repair="刷新投放结果后重试",
            actions=[{"id": "refresh", "label": "刷新", "type": "retry"}]))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=_err(
            "ITEM_NOT_RETRYABLE", "有项现在不能重投", reason=str(exc),
            impact="本次未重投任何一项",
            repair="结果未知的项需要人工核对，不能自动重投",
            actions=[{"id": "contact_admin", "label": "联系管理员", "type": "contact"}],
            retryable=False))
    return {"status": "accepted", "command_id": "pubcmd_" + request_id,
            "marked": out["marked"]}



# ─────────────────────────────────────────────────────────────
# 6. 发布素材准备(耗时 mutation,不是纯读)
# ─────────────────────────────────────────────────────────────

def _load_active_revision_id(post_id: int):
    """直读 `geo_douyin_posts.active_revision_id`。

    存在的理由见调用点:`db.geo_douyin_db.get_post` 的字段清单是 034 之前写的,
    不含这一列。改那份清单会改变**所有**用 `get_post` 的端点的响应形状,
    所以这里窄读一列。
    """
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT active_revision_id FROM geo_douyin_posts WHERE id = %s",
                    (int(post_id),))
        row = cur.fetchone()
        return (dict(row).get("active_revision_id") if row else None)
    finally:
        conn.close()


#: [#196 c1b/c1c] 代码串 -> 人话。worker 写进 `card_statuses.reason` 的是**给机器看的**,
#:   一共三种产出(`contract_worker.py:366-395` 逐条核过):
#:   1. `NO_CARDS` 这样的代码串;
#:   2. `f"{type(exc).__name__}: {str(exc)[:160]}"` 裸异常串,**没过任何脱敏**;
#:   3. 适配器的 `PreparedMedia.error` —— 其中 `_neutral(f"第 {i+1} 张图上传失败:{e}")`
#:      (`publish_adapter.py:253-258`)过了词表,但**词表外的主机名原样留在里面**。
_ARTIFACT_REASON_HUMAN = {
    "NO_CARDS": "作品还没有出图,先把卡片做出来再准备发布",
}

#: state -> 说不出具体原因时的通用句。**两句不可互换**,它们说的是不同的下一步:
#:   failed  = 渠道侧零残留(一张都没传上去),重来一次是干净的;
#:   unknown = 渠道侧**可能已有半份素材**,自动重传会造重复,必须转人工。
#:   写反的后果不是"话术难看",是让人去重传一个已经有残留的作品 ——
#:   `contract_worker.py:380-386` 那段注释解释的正是这件事。
_GENERIC_BY_STATE = {
    "failed": "素材准备失败,收起再展开面板会重新准备",
    "unknown": ("上传时连接中断,渠道可能已收到部分素材,"
                "系统不会自动重传,请联系人工核对"),
}

#: 「这不是人话」的形状。**白名单**:命中任一就不给原话。
#:   · 连续 4 个以上 ASCII 字母 —— 类名 / 主机名 / 协议名 / 库名全中招,
#:     而中文原话里不会出现(适配器那三句纯中文一个字母都没有);
#:   · `://` 与 `= ( ) < >` —— 代码与 URL 的形状。
#:   c1b 用的是「开头长得像异常就换」的黑名单,对**没见过的形状**结构性失明:
#:   适配器那句"中文前缀 + 技术尾巴"整条漏过去,而那正是上线后最常见的一句。
#:   白名单反过来 —— 没见过的形状默认不放行。
#:   只认半角:全角括号是正常中文标点,认了会把好句子一起堵掉。
_TECHNICAL_SHAPE = re.compile(r"[A-Za-z]{4,}|://|[=()<>]")

#: 出口长度上限:面板一行放得下的量。
_REASON_MAX_CHARS = 120


def _humanize_artifact_reason(raw: str, *, state: str) -> str:
    """把落库的机器原话翻成**对服务商可见**的一句;翻不出就按 `state` 换通用句。

    顺序:查表 -> 词表脱敏 -> 形状脱敏 -> 白名单判人话 -> 截断。

    **挡得住什么**(逐条都有判据钉着):
      · 已知代码串(`NO_CARDS`)-> 查表换人话;
      · 词表内的供应商标识 -> `scrub_text`;
      · 词表**外**的主机名 / URL -> `scrub_hosts_and_urls`(纵深:词表永远落后现实);
      · 洗完仍带技术形状的 -> 不给原话,按 `state` 换通用句。

    **挡不住什么**(写在这里,免得下一个人以为它是万能的):
      · 纯中文写成的技术细节 —— 上游回「数据库连接池耗尽」会原样透出;
      · 纯数字的内部单号 —— 「订单 20260913001 失败」会原样透出。
      两者都不携带供应商身份,是可接受的残留。要连这些也堵,只能改成
      「只许表内句子」的强白名单 —— 那会把适配器那三句有用的中文一起堵掉,
      用户就再也看不到「第 2 张图读取为空」这种真正能指导下一步的话。
    """
    from services.publish_channel_privacy import scrub_hosts_and_urls, scrub_text

    text = str(raw or "").strip()
    if not text:
        return ""
    if text in _ARTIFACT_REASON_HUMAN:
        return _ARTIFACT_REASON_HUMAN[text]

    cleaned = scrub_hosts_and_urls(scrub_text(text)).strip()
    if not cleaned or _TECHNICAL_SHAPE.search(cleaned):
        # 认不出的一律按 unknown 兜底:宁可多说一句「别自动重传」,
        # 也不能对一个可能有残留的作品说「重来一次就行」。
        return _GENERIC_BY_STATE.get(state) or _GENERIC_BY_STATE["unknown"]
    if len(cleaned) > _REASON_MAX_CHARS:
        cleaned = cleaned[:_REASON_MAX_CHARS].rstrip() + "…"
    return cleaned


def _artifact_failure_reason(artifact: dict) -> str:
    """[#196 c1] 素材准备失败的**人话**原因。非 failed/unknown 一律空串。

    🔴 **没有专门的 failure_reason 列**(`geo_douyin_publish_artifacts` 建表逐列核过:
       只有 state 与 card_statuses)。原话在 `card_statuses` 里那条非 ready 项的
       `reason` 上(worker 写的形状 `contract_worker.py:366-390`)。
       工单 §4 写的是「mark_failed 落库的原话」——那两只函数其实只写 card_statuses。

    🔴 取**第一条**非 ready 且 reason 非空的:多张卡可能各有原因,面板一行只放得下
       一句;全取会挤成一堆,取空串等于没说。

    🔴 [c1c] 最后那道 `contains_vendor_trace` 护的是**绕过 scrub 的两条路** ——
       查表命中的那句、按 state 替换的那两句:它们是我们自己写的常量,不过
       `scrub_text`,哪天有人往表里写了带供应商名的句子,只有这道拦得住。
       它**护不了**上游原话:原话已经过 scrub,而这道与 scrub 共用同一张词表,
       洗完必然为假。c1b 在这里写「词表没跟上也不泄露」是**假的**
       (Review 毒 G:删掉它 10/10 全绿)。词表没跟上是靠 `scrub_hosts_and_urls`
       按形状兜的,不是靠这一行。
    """
    from services.publish_channel_privacy import contains_vendor_trace

    state = str(artifact.get("state") or "")
    if state not in ("failed", "unknown"):
        return ""
    rows = artifact.get("card_statuses") or []
    if isinstance(rows, str):
        try:
            rows = _json.loads(rows)
        except Exception:
            return ""
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        if str(row.get("state") or "") == "ready":
            continue
        raw = str(row.get("reason") or "").strip()
        if not raw:
            continue
        human = _humanize_artifact_reason(raw, state=state)
        if not human or contains_vendor_trace(human):
            return ""          # 洗不干净就不给 —— 不泄露优先于有话说
        return human
    return ""


@router.post("/api/geo-douyin/posts/{post_id}/prepare-publish-media-v2")
async def api_prepare_publish_media_v2(post_id: int, request: Request):
    """规格 §7.1:冻结 `post_revision_id + local_manifest_hash`,独立持久
    `request_id/request_hash/state/lease/heartbeat`;同 key/hash 返回同一 artifact,异 hash 409。

    🔴 路径带 `-v2`:现役 `/prepare-publish-media` 仍在被前端调用,
       直接改它会同时改变活的线上行为。新链走 v2,cutover 由 WP7 的 caller census 统一做。
    """
    import asyncio

    _user(request)
    _guard(request, action="publish.prepare_media", ability=ABILITY_PUBLISH)
    identity = _resolve_identity(request)

    request_id = request.headers.get("idempotency-key") or ""
    if not request_id:
        raise HTTPException(status_code=400, detail=_err(
            "IDEMPOTENCY_KEY_REQUIRED", "这次操作缺少请求标识",
            reason="素材准备会真的调用发布渠道上传图片,必须能识别重复提交",
            impact="本次未准备任何素材,未联系发布渠道",
            repair="刷新页面后重试",
            actions=[{"id": "retry", "label": "重试", "type": "retry"}]))
    # 🔴 [返工 · 链 3] `geo_douyin_publish_artifacts.request_id` 是 **uuid** 列。
    #    header 走不了 Pydantic,所以这一道必须手写 —— 少了它,
    #    `prep-<id>-<rev>` 这种老形态会在 INSERT 那一刻 500。
    try:
        request_id = normalize_uuid(request_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(
            "IDEMPOTENCY_KEY_INVALID", "这次操作的请求标识格式不对", reason=str(exc),
            impact="本次未准备任何素材,未联系发布渠道",
            repair="刷新页面后重试;若反复出现请联系管理员",
            actions=[{"id": "retry", "label": "重试", "type": "retry"}]))

    from db.connection import get_connection
    from db.geo_douyin_db import get_post
    from services.geo_douyin.artifact_prepare import (
        ArtifactRequestConflict, claim_artifact,
    )
    from services.geo_douyin.contract_idempotency import request_hash
    from services.geo_douyin.tenant_scope import ScopeUnavailable, assert_object_in_scope

    post = await asyncio.to_thread(get_post, int(post_id))
    if not post:
        raise HTTPException(status_code=404, detail=_err(
            "SOURCE_NOT_FOUND", "找不到这篇作品", reason="作品不存在或已删除",
            impact="本次未准备任何素材", repair="刷新作品库",
            actions=[{"id": "refresh", "label": "刷新作品库", "type": "retry"}]))
    if post.get("brand_id"):
        require_brand_access(request, int(post["brand_id"]))

    # [第 3 棒 · Codex R2 P0-3 · 第二层 · **已修,2026-09-13 #184 d1**]
    #    原注:`_POST_FIELDS` 不含 `active_revision_id` ⇒ `post.get(...)` 恒 None
    #    ⇒ 本入口对每一篇作品都 409。**那一半已经修掉**:
    #    `db/geo_douyin_db.py:26` 的 `_POST_FIELDS` 现在含该列。
    #    下面的 `_load_active_revision_id` 兜底**保留** —— 存量作品那一列仍可能为空
    #    (要等 J4 回填脚本执行),兜底读的是同一列、不是第二个真相源。
    #    另一半("生产从不建 revision")由 #184 d1 修在生成链上。
    #    这里直读那一列而不是改 `_POST_FIELDS`:后者的返回体被多处直接展开进
    #    响应,加键是**跨端点**的形状变更(本仓 `extra="forbid"` 已三次炸生产)。
    revision_id = post.get("active_revision_id")
    if not revision_id:
        revision_id = await asyncio.to_thread(_load_active_revision_id, int(post_id))
    if not revision_id:
        raise HTTPException(status_code=409, detail=_err(
            "SOURCE_NOT_READY", "这篇作品还没有可发布的版本",
            reason="作品尚未生成完成", impact="本次未准备任何素材",
            repair="回到校对页确认作品已完成",
            actions=[{"id": "open_post", "label": "查看作品", "type": "nav"}]))

    payload = {"post_id": int(post_id), "post_revision_id": int(revision_id)}
    rhash = request_hash(owner_user_id=int(identity["tenant_owner_user_id"]),
                         endpoint="prepare-publish-media-v2", payload=payload)

    def _claim() -> tuple[bool, dict]:
        conn = get_connection()
        try:
            cur = conn.cursor()
            # 🔴 对象级二次校验用**后面真正要写的那一行**,不是请求里的 id
            assert_object_in_scope(
                post, tenant_owner_user_id=identity["tenant_owner_user_id"],
                authorized_brand_ids=[int(post["brand_id"])] if post.get("brand_id") else [],
                actor_user_id=identity["actor_user_id"])
            out = claim_artifact(
                cur, geo_post_id=int(post_id), post_revision_id=int(revision_id),
                tenant_owner_user_id=int(identity["tenant_owner_user_id"]),
                request_id=request_id, request_hash=rhash)
            conn.commit()
            return out
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    try:
        is_new, artifact = await asyncio.to_thread(_claim)
    except ArtifactRequestConflict as exc:
        raise HTTPException(status_code=409, detail=_err(
            "IDEMPOTENCY_CONFLICT", "这个请求标识已经用过了", reason=str(exc),
            impact="本次未准备任何素材,未联系发布渠道", repair="用新的请求重试",
            actions=[{"id": "retry", "label": "重新准备", "type": "retry"}]))
    except ScopeUnavailable as exc:
        raise HTTPException(status_code=403, detail=_err(
            "SOURCE_ACCESS_DENIED", "你已无法操作这个客户", reason=str(exc),
            impact="本次未准备任何素材", repair="返回客户选择",
            actions=[{"id": "back", "label": "返回客户选择", "type": "nav"}],
            retryable=False))

    # 🔴 claim 只落身份,**不在请求线程里跑逐图上传** —— 那是耗时外调,
    #    由 durable worker 领。规格 §7.1:task/outbox 先持久化,API 才返回。
    return {
        "status": "accepted", "replayed": not is_new,
        "prepared_artifact_id": int(artifact["prepared_artifact_id"]),
        "post_revision_id": int(artifact["post_revision_id"]),
        "manifest_hash": artifact.get("manifest_hash"),
        "state": str(artifact["state"]),
        # [#196 c1] 失败/未知时把**落库的原话**带出去。面板现在只有前端自己那句
        #   「准备失败」,用户既不知道为什么、也不知道该不该重试。
        "failure_reason": _artifact_failure_reason(artifact),
    }
