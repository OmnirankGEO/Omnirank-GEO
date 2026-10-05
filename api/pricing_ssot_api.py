"""双价目表 SSOT API(§5 / §8 / §9.2 / §16)

对外只暴露平台化人话字段:最终价、算力、用途示例。
禁止返回:base_price_cents、multiplier_bps、cost_floor_cents、channel_beneficiary、成本、实名。

端点:
  GET  /api/pricing/retail/catalog          普通用户售价目录(显式绑定优先，否则平台直营)
  POST /api/pricing/retail/quote            零售报价
  GET  /api/pricing/procurement/catalog     经销商进货目录(渠道链调整)
  POST /api/pricing/procurement/quote       进货报价
  GET  /api/pricing/quote/{quote_id}        查报价(买方本人)
  --- admin(定价中心 · 发布工作流)---
  POST /api/pricing/admin/catalog/draft
  POST /api/pricing/admin/catalog/{id}/validate
  POST /api/pricing/admin/catalog/{id}/publish
  POST /api/pricing/admin/catalog/{id}/rollback
  GET  /api/pricing/admin/catalog/versions
  POST /api/pricing/admin/channel/relationship
  DELETE /api/pricing/admin/channel/relationship/{dealer_id}
  GET  /api/pricing/admin/reconciliation/order/{order_id}      财务人工对账(P0-11)

红线:整数分 · 无税 · 不碰 billing/connection/auth · 前端不算钱。
"""

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel, Field

from db.connection import get_connection, get_db
from services import (
    pricing_catalog,
    price_quote,
    channel_pricing,
    account_codes,
    pricing_readiness,
    dealer_inventory_resale,
)
from services.price_quote import QuoteError
from services.channel_pricing import ChannelError
from services.business_identity import BusinessIdentityUnavailable, is_service_provider
from services.commercial_service_routing import (
    RelationshipError,
    RelationshipResolution,
    public_relationship_http_error,
    resolve_commercial_relationship,
)
from schemas.public_contracts import AgentServiceDTO
from config import pricing_ssot_flags
from auth.agreement_gate import require_signed_agreement

logger = logging.getLogger("GEO-PricingSSOT-API")
router = APIRouter(prefix="/api/pricing", tags=["双价目表SSOT"])


def _pricing_flags_or_503() -> Dict[str, bool]:
    try:
        return pricing_ssot_flags.pricing_flags_snapshot()
    except pricing_ssot_flags.PricingFlagsUnavailable as exc:
        raise HTTPException(
            503,
            detail={
                "code": "PRICING_POLICY_UNAVAILABLE",
                "message": "定价策略暂不可用 · 请稍后重试",
            },
            headers={"Retry-After": "5"},
        ) from exc


# ============================================================ helpers
def _user(request: Request) -> dict:
    return getattr(request.state, "user", None) or {}


def _require_admin(request: Request):
    if not _user(request).get("is_admin"):
        raise HTTPException(403, detail="需要管理员权限")


def _product_names() -> Dict[str, str]:
    """product_code(=template_code)→ 对外展示名。"""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT template_code, default_name FROM sku_templates WHERE is_active = TRUE")
            return {r["template_code"]: r["default_name"] for r in cur.fetchall()}
    except Exception:
        return {}


def _usage_examples(points: int) -> List[str]:
    """§5.2:由当前功能消耗目录动态生成约数示例(不在前端写死)。"""
    ex: List[str] = []
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT feature_code, cost_points FROM feature_pricing WHERE feature_code = ANY(%s) AND is_active = TRUE",
                (["article_gen", "monitor_single", "geo_diagnosis"],),
            )
            costs = {r["feature_code"]: int(r["cost_points"]) for r in cur.fetchall()}
        if costs.get("article_gen"):
            ex.append(f"约可生成 {points // costs['article_gen']:,} 篇基础文章")
        if costs.get("monitor_single"):
            ex.append(f"或监测约 {points // costs['monitor_single']:,} 个词天")
    except Exception:
        pass
    return ex


def _public_entry(entry: Dict[str, Any], names: Dict[str, str], price_valid_until=None) -> Dict[str, Any]:
    """严格公开白名单:只出人话字段(§8.1)。"""
    source = entry.get("source_ref_jsonb") or {}
    if isinstance(source, str):
        try:
            import json
            source = json.loads(source)
        except Exception:
            source = {}
    template_code = source.get("template_code") if isinstance(source, dict) else None
    # Published entries are immutable order inputs, so their human-facing name must
    # come from the same snapshot.  Looking only at today's active templates leaks
    # internal product codes while a newer management draft is waiting to publish.
    snapshot_name = source.get("display_name") if isinstance(source, dict) else None
    display_name = str(
        snapshot_name
        or names.get(template_code or entry["product_code"])
        or entry["product_code"]
    ).strip() or str(entry["product_code"])
    return {
        "product_code": entry["product_code"],
        "display_name": display_name,
        "subtitle": source.get("subtitle") if isinstance(source, dict) else None,
        "sales_pitch": source.get("sales_pitch") if isinstance(source, dict) else None,
        "scene": source.get("scene") if isinstance(source, dict) else None,
        "final_price_cents": int(entry["final_price_cents"]),
        "points_granted": int(entry["paid_points"]),
        "bonus_points": int(entry.get("bonus_points") or 0),
        "usage_examples": _usage_examples(int(entry["paid_points"])),
        **({"price_valid_until": price_valid_until} if price_valid_until else {}),
    }


def _entry_source_ref(entry: Dict[str, Any]) -> Dict[str, Any]:
    source = entry.get("source_ref_jsonb") or {}
    if isinstance(source, str):
        try:
            source = json.loads(source)
        except (TypeError, ValueError):
            return {}
    return dict(source) if isinstance(source, dict) else {}


def _resolve_service_principal(customer_user_id: int):
    try:
        with get_db() as conn:
            return resolve_commercial_relationship(conn.cursor(), customer_user_id)
    except RelationshipError as exc:
        status, detail = public_relationship_http_error(exc)
        logger.warning(
            "retail relationship resolution failed type=%s",
            type(exc).__name__,
        )
        raise HTTPException(status, detail=detail) from exc


def _public_pricing_error(*, operation: str, exc: Exception, status: int = 422) -> HTTPException:
    logger.warning("pricing %s refused type=%s", operation, type(exc).__name__)
    return HTTPException(
        status,
        detail={
            "code": "PRICE_CONFIGURATION_UNAVAILABLE",
            "message": "当前账户价格配置暂不可用，请稍后重试",
        },
    )


def _require_service_provider(user: Dict[str, Any]) -> None:
    if user.get("is_admin"):
        return
    try:
        allowed = is_service_provider(int(user.get("user_id") or 0))
    except BusinessIdentityUnavailable as exc:
        raise HTTPException(
            503,
            detail={"code": "BUSINESS_IDENTITY_UNAVAILABLE", "message": "业务身份暂不可验证，请稍后再试"},
        ) from exc
    if not allowed:
        raise HTTPException(403, detail="仅服务商可使用进货功能")


def _require_retail_customer(user: Dict[str, Any]) -> None:
    """Keep arbitrary-cash retail recharge separate from provider procurement."""
    if user.get("is_admin"):
        return
    try:
        service_provider = is_service_provider(int(user.get("user_id") or 0))
    except BusinessIdentityUnavailable as exc:
        raise HTTPException(
            503,
            detail={"code": "BUSINESS_IDENTITY_UNAVAILABLE", "message": "业务身份暂不可验证，请稍后再试"},
        ) from exc
    if service_provider:
        raise HTTPException(
            403,
            detail={
                "code": "CUSTOMER_RECHARGE_ONLY",
                "message": "自由充值仅供普通客户使用，服务商请前往算力库存进货",
            },
        )


# ============================================================ 零售(普通用户)
# ---------------------------------------------------------------------------
# [#84 §1 · 2026-09-05] 诊断只读价预览 —— 三标签与 legacy **共用同一条规则**
#
# 🔴 路径刻意放在 `/api/pricing/` 下而不是 `/api/diagnosis/price-preview`:
#    实测 `server.app.routes` 里 `/api/diagnosis/{id}`(id: int)注册序号 1507/1508,
#    而本 router 在 857 —— 今天字面路径能赢,但它赢在**注册顺序**这条隐形依赖上。
#    谁调一下 router 注册位置,请求就落到 `{id}` 上,把 "price-preview" 当 int 解析
#    ⇒ 422,**且不会继续往下找路由**。换个 `{id}` 兄弟不存在的前缀 = 消灭这根轴,
#    而不是加一条判据去盯它。
# ---------------------------------------------------------------------------
# 🔴 [订正二十一 · 2026-09-05] 原来这里还有一个 GET 变体,**已删**。
#    它恒 `ai_optimized=False`,而 POST 按开关走 => 3 题、开关开时
#    GET 显 650 / POST 绑 950。前端若用 GET 显价、用 POST 绑提交,
#    就**重造了「看到的 ≠ 扣的」** —— 与本单毒 7 同族,只是换了个入口。
#    §1 尚未上线、无兼容债,所以是删而不是留兼容:
#    **留着一个算法不同的第二入口,迟早有人用它显价。**
#    (它的注释「开关不再决定跑几道题」出自**已撤回**的订正九,一并消掉。)

class DiagnosisPricePreviewIn(BaseModel):
    questions: List[str] = Field(default_factory=list)
    mode: str = ""
    # 🔴 [返修] 这两项是**实际扣费真的吃**的输入,必须进哈希:
    #    实测 3 题、关 650 / 开 950,而上一版 id 同值 => 闸不响、扣 950。
    aiOptimizeCustom: bool = False
    scope: str = "geo"


@router.post("/diagnosis-preview")
async def diagnosis_price_preview_post(request: Request, body: DiagnosisPricePreviewIn):
    """[#84 §3] 与 GET 同价,额外返回 `pricePreviewId`(题集 + 计价规则的哈希)。

    🔴 GET 留给 A 做**即时显价**(只要数量就够);提交体**只认本 POST 发的 id** ——
    因为只有这里拿得到题目本身,拿不到题集就算不出能绑住它的哈希。

    🔴 价仍由**同一个** `extra_points_for_questions` 算,本接口不另写一份规则;
    题集归一化走 `normalize_questions`,与 legacy 的 `validate_custom_questions` 同解。
    """
    if not _user(request):
        raise HTTPException(401, detail="未登录")

    from db.wallet_db import get_feature_pricing
    from services.diagnosis_question_pricing import (
        EXTRA_POINTS_PER_QUESTION,
        FREE_CUSTOM_QUESTIONS,
        extra_points_for_questions,
        feature_code_for_scope,
        normalize_questions,
        quote_diagnosis_price,
    )

    # 🔴 [订正二十二] 基价按 scope 取,映射走**唯一那份**:
    #    原先预览/守卫/扣费三处各写一份,任意两处漂了都不会报错。
    _feature = feature_code_for_scope(body.scope)
    row = get_feature_pricing(_feature) or {}
    base = int(row.get("cost_points") or 0)
    if base <= 0:
        raise HTTPException(503, detail="价目暂时读不到,请稍后再试")

    # 🔴 [返修二] points 与 id **一次派生**,不再各算一遍。
    #    上一版两处分开算 => 可以分家:显示 650、id 绑 950、提交放行扣 950,
    #    而**用户看到的是 650**。从同一份 payload 出,结构上不可能不同。
    _q = quote_diagnosis_price(
        body.questions, base_points=base,
        ai_optimized=bool(body.aiOptimizeCustom), feature_code=_feature)
    norm = _q["questions"]
    n = _q["question_count"]
    extra = _q["extra"]
    points = _q["points"]
    return {
        "questionCount": n,
        "mode": body.mode or None,
        "points": points,
        "estimate": points,
        "pricePreviewId": _q["preview_id"],
        "breakdown": {
            "base": base,
            "extra": extra,
            "freeQuestions": int(FREE_CUSTOM_QUESTIONS),
            "perExtraQuestion": int(EXTRA_POINTS_PER_QUESTION),
        },
    }


@router.get("/retail/catalog")
async def retail_catalog(request: Request):
    """普通用户售价目录；商业主体只参与内部结算，响应始终平台化。"""
    user = _user(request)
    uid = user.get("user_id")
    if not uid:
        raise HTTPException(401, detail="未登录")
    flags = _pricing_flags_or_503()
    if not flags.get("PRICING_DUAL_SSOT_ENABLED", False):
        # 总闸未开:本端点不接管(前端仍走旧价路径)· 明示以免误判空目录
        raise HTTPException(503, detail={"code": "SSOT_DISABLED", "message": "双价目表未启用"})

    relationship = _resolve_service_principal(uid)
    service_code = relationship.service_scope_key
    if not service_code:
        raise _public_pricing_error(operation="retail-catalog-code", exc=RuntimeError("missing code"), status=503)
    cat = pricing_catalog.get_published_catalog("retail", service_code)
    if not cat:
        raise _public_pricing_error(operation="retail-catalog", exc=RuntimeError("missing catalog"), status=409)
    names = _product_names()
    resale_enabled = False
    public_items = []
    resale_conn = None
    try:
        from config.dealer_inventory_resale_flags import enabled as dealer_resale_enabled
        resale_enabled = dealer_resale_enabled()
        # A current-cost check is mandatory even when Dealer resale is off.  The
        # same transaction also supplies the optional inventory preview, keeping
        # relationship versions stable for the complete catalog response.
        resale_conn = get_connection()
        resale_cur = resale_conn.cursor()
        for entry in cat["items"]:
            source = _entry_source_ref(entry)
            price_quote.assert_retail_catalog_entry_current(
                resale_cur,
                source_ref=source,
                final_cents=int(entry["final_price_cents"]),
            )
            public = _public_entry(entry, names)
            if resale_enabled:
                root_reference = int(source.get("wholesale_cents") or 0)
                if root_reference <= 0:
                    raise dealer_inventory_resale.ResaleError(
                        "STANDARD_REFERENCE_MISSING",
                        "已发布零售价目缺少厂家标准参考价",
                    )
                terms = dealer_inventory_resale.build_consumer_quote_terms(
                    resale_cur,
                    seller_user_id=int(relationship.service_user_id),
                    consumer_user_id=int(uid),
                    points=int(entry["paid_points"]) + int(entry.get("bonus_points") or 0),
                    catalog_reference_amount_cents=int(entry["final_price_cents"]),
                    pricing_version=str(cat["version"]["version_code"]),
                    digital_goods_acknowledged=True,
                    product_code=str(entry["product_code"]),
                    platform_reference_amount_cents=root_reference,
                    catalog_version_id=str(cat["version"]["id"]),
                    catalog_entry_id=str(entry["id"]),
                )
                public["final_price_cents"] = int(terms["sale_amount_cents"])
                public["resale_mode"] = True
            public_items.append(public)
    except (dealer_inventory_resale.ResaleError, QuoteError) as exc:
        raise _public_pricing_error(operation="retail-catalog-resale", exc=exc, status=409)
    finally:
        if resale_conn is not None:
            resale_conn.close()
    return {
        "success": True,
        "data": {
            "seller_label": "OmniRank 平台",
            "service": relationship.customer_dto().model_dump(),
            "catalog_version": cat["version"]["version_code"],
            "items": public_items,
            **({
                "resale_mode": True,
                "consumer_policy_code": dealer_inventory_resale.DIGITAL_GOODS_POLICY_CODE,
                "digital_goods_notice": (
                    "算力为付款后即时交付的数字商品，不适用无理由退货；"
                    "退款按订单快照及未消费情况审核，由平台统一执行并留痕"
                ),
                "requires_digital_goods_acknowledgement": True,
            } if resale_enabled else {}),
        },
    }


class RetailQuoteRequest(BaseModel):
    product_code: str
    # 现役结算把一个目录 entry 视为一个完整算力包；开放多件会让 SKU 成本/积分
    # 快照出现两套数量语义。先固定 1，未来若支持购物车须另做聚合快照契约。
    quantity: int = Field(default=1, ge=1, le=1)
    idempotency_key: Optional[str] = None
    digital_goods_acknowledged: bool = False
    terms_acceptance_id: str = Field(..., min_length=10, max_length=80)


class RetailCustomAmountQuoteRequest(BaseModel):
    amount_cents: int = Field(ge=100, le=1_000_000)
    idempotency_key: Optional[str] = Field(default=None, max_length=128)
    digital_goods_acknowledged: bool = False
    terms_acceptance_id: str = Field(..., min_length=10, max_length=80)


@router.post("/retail/quote")
async def retail_quote(req: RetailQuoteRequest, request: Request):
    user = _user(request)
    uid = user.get("user_id")
    if not uid:
        raise HTTPException(401, detail="未登录")
    flags = _pricing_flags_or_503()
    if not flags.get("PRICING_DUAL_SSOT_ENABLED", False):
        raise HTTPException(503, detail={"code": "SSOT_DISABLED", "message": "双价目表未启用"})
    relationship = _resolve_service_principal(uid)
    service_code = relationship.service_scope_key
    if not service_code:
        raise _public_pricing_error(operation="retail-quote-code", exc=RuntimeError("missing code"), status=503)
    commercial_service_source = (
        "explicit_binding"
        if relationship.resolution is RelationshipResolution.BOUND
        else "platform_direct"
    )
    try:
        q = price_quote.issue_quote(
            quote_type="retail", scope_key=service_code, product_code=req.product_code,
            buyer_user_id=uid, quantity=req.quantity, seller_policy_version=service_code,
            idempotency_key=req.idempotency_key,
            commercial_service_source=commercial_service_source,
            retail_seller_user_id=int(relationship.service_user_id),
            consumer_policy_acknowledged=req.digital_goods_acknowledged,
            purchase_terms_acceptance_id=req.terms_acceptance_id,
        )
    except QuoteError as e:
        raise _public_pricing_error(operation="retail-quote", exc=e)
    resale_mode = price_quote.quote_order_pricing_snapshot(q).get("consumer_resale_mode") is True
    return {"success": True, "data": {
        "quote_id": q["quote_id"], "final_price_cents": q["final_price_cents"],
        "points_granted": q["points_granted"], "bonus_points": q["bonus_points"],
        "currency": q["currency"], "price_valid_until": q["expires_at"].isoformat(),
        "service": relationship.customer_dto().model_dump(), "seller_label": "OmniRank 平台",
        **({
            "resale_mode": True,
            "consumer_policy_code": dealer_inventory_resale.DIGITAL_GOODS_POLICY_CODE,
            "digital_goods_acknowledged": True,
        } if resale_mode else {}),
    }}


@router.post("/retail/custom-amount-quote")
async def retail_custom_amount_quote(req: RetailCustomAmountQuoteRequest, request: Request):
    user = _user(request)
    uid = user.get("user_id")
    if not uid:
        raise HTTPException(401, detail="未登录")
    _require_retail_customer(user)
    flags = _pricing_flags_or_503()
    if not flags.get("PRICING_DUAL_SSOT_ENABLED", False):
        raise HTTPException(503, detail={"code": "SSOT_DISABLED", "message": "双价目表未启用"})
    relationship = _resolve_service_principal(uid)
    service_code = relationship.service_scope_key
    if not service_code:
        raise _public_pricing_error(
            operation="retail-custom-quote-code", exc=RuntimeError("missing code"), status=503,
        )
    commercial_service_source = (
        "explicit_binding"
        if relationship.resolution is RelationshipResolution.BOUND
        else "platform_direct"
    )
    try:
        q = price_quote.issue_retail_cash_quote(
            scope_key=service_code,
            buyer_user_id=int(uid),
            retail_seller_user_id=int(relationship.service_user_id),
            amount_cents=int(req.amount_cents),
            commercial_service_source=commercial_service_source,
            consumer_policy_acknowledged=req.digital_goods_acknowledged,
            purchase_terms_acceptance_id=req.terms_acceptance_id,
            idempotency_key=req.idempotency_key,
        )
    except QuoteError as exc:
        raise _public_pricing_error(operation="retail-custom-quote", exc=exc)
    resale_mode = price_quote.quote_order_pricing_snapshot(q).get("consumer_resale_mode") is True
    return {"success": True, "data": {
        "quote_id": q["quote_id"],
        "final_price_cents": q["final_price_cents"],
        "points_granted": q["points_granted"],
        "bonus_points": 0,
        "currency": q["currency"],
        "price_valid_until": q["expires_at"].isoformat(),
        "amount_source": "customer_entered_cash",
        "service": relationship.customer_dto().model_dump(),
        "seller_label": "OmniRank 平台",
        **({
            "resale_mode": True,
            "consumer_policy_code": dealer_inventory_resale.DIGITAL_GOODS_POLICY_CODE,
            "digital_goods_acknowledged": True,
        } if resale_mode else {}),
    }}


# ============================================================ 进货(经销商)
@router.get("/procurement/catalog", dependencies=[Depends(require_signed_agreement)])
async def procurement_catalog(request: Request):
    user = _user(request)
    uid = user.get("user_id")
    if not uid:
        raise HTTPException(401, detail="未登录")
    # [WO_254 2026-09-20] 🔴 **这一屏读的是这个端点**,不是 purchase-options。
    #   平台直营(管理员)在这里原来一路走到算价,撞 `ResaleError` 后被映射成
    #   `PRICE_CONFIGURATION_UNAVAILABLE`「价格配置暂不可用」—— 一句**运维故障**的话,
    #   而真相是"平台不向自己进货"这件业务上本来就不存在的事。
    #   于是屏幕显示前端兜底句「尚未取到平台口径说明」,明示原话一次都没到过屏幕。
    #   所以在**取价之前**就出声,且与 purchase-options 用同一份码与文案。
    from services.platform_direct_procurement import (
        is_platform_direct_actor, platform_direct_no_procurement,
    )
    if is_platform_direct_actor(user):
        raise platform_direct_no_procurement()
    _require_service_provider(user)
    flags = _pricing_flags_or_503()
    if not flags.get("PRICING_DUAL_SSOT_ENABLED", False):
        raise HTTPException(503, detail={"code": "SSOT_DISABLED", "message": "双价目表未启用"})

    base_cat = pricing_catalog.get_published_catalog("procurement", "PLATFORM_BASE")
    if not base_cat:
        raise HTTPException(409, detail={"code": "NO_PUBLISHED_PROCUREMENT", "message": "平台进货目录未发布"})

    names = _product_names()
    items = []
    preview_conn = None
    try:
        preview_conn = get_connection()
        preview_cur = preview_conn.cursor()
        for e in base_cat["items"]:
            preview = price_quote.build_procurement_quote_preview(
                preview_cur,
                dealer_id=int(uid),
                entry=dict(e),
                version=dict(base_cat["version"]),
                quantity=1,
            )
            snapshot = preview["order_pricing_snapshot"]
            item = {
                "product_code": e["product_code"],
                "display_name": names.get(e["product_code"], e["product_code"]),
                "cash_price_cents": int(preview["final_price_cents"]),
                "paid_inventory_points": int(preview["points_granted"]),
                "bonus_inventory_points": int(preview["bonus_points"]),
                "total_inventory_points": int(preview["points_granted"]) + int(preview["bonus_points"]),
                "tier_at_order": snapshot.get("benefit_tier_at_order") or snapshot.get("tier_at_order"),
                "tier_source": snapshot.get("tier_source"),
                "tier_bonus_rate_bps": int(snapshot.get("benefit_tier_bonus_rate_bps") or snapshot.get("tier_bonus_rate_bps") or 0),
                "crosses_tier_threshold": bool(snapshot.get("benefit_crosses_tier_threshold", snapshot.get("crosses_tier_threshold"))),
                "projected_rolling_12m_yuan": snapshot.get("projected_rolling_12m_yuan_snapshot"),
                "reward_description": price_quote.public_procurement_reward_description(snapshot),
            }
            items.append(item)
    except (ChannelError, dealer_inventory_resale.ResaleError, QuoteError, ValueError) as ce:
        raise _public_pricing_error(operation="procurement-catalog", exc=ce, status=409)
    finally:
        if preview_conn is not None:
            preview_conn.close()
    response_data = {
        "seller_label": "OmniRank 平台",
        "service": AgentServiceDTO(configuration_status="ready").model_dump(),
        "catalog_version": base_cat["version"]["version_code"],
        "items": items,
    }
    try:
        with get_db() as conn:
            from services.channel_tier import get_agent_channel_tier_state
            response_data["tier_progress"] = get_agent_channel_tier_state(conn.cursor(), int(uid))
    except Exception as exc:
        logger.warning("procurement tier progress unavailable user=%s error=%s", uid, exc)
    return {"success": True, "data": response_data}


class ProcurementQuoteRequest(BaseModel):
    product_code: str
    quantity: int = Field(default=1, ge=1, le=1)
    idempotency_key: Optional[str] = None


@router.post("/procurement/quote", dependencies=[Depends(require_signed_agreement)])
async def procurement_quote(req: ProcurementQuoteRequest, request: Request):
    user = _user(request)
    uid = user.get("user_id")
    if not uid:
        raise HTTPException(401, detail="未登录")
    _require_service_provider(user)
    flags = _pricing_flags_or_503()
    if not flags.get("PRICING_DUAL_SSOT_ENABLED", False):
        raise HTTPException(503, detail={"code": "SSOT_DISABLED", "message": "双价目表未启用"})
    try:
        q = price_quote.issue_procurement_quote(
            dealer_id=uid, product_code=req.product_code, quantity=req.quantity,
            idempotency_key=req.idempotency_key,
        )
    except (QuoteError, ChannelError, dealer_inventory_resale.ResaleError) as e:
        raise _public_pricing_error(operation="procurement-quote", exc=e)
    order_snapshot = price_quote.quote_order_pricing_snapshot(q, required=True)
    response_data = {
        "quote_id": q["quote_id"], "cash_price_cents": q["final_price_cents"],
        "paid_inventory_points": q["points_granted"], "bonus_inventory_points": q["bonus_points"],
        "currency": q["currency"], "price_valid_until": q["expires_at"].isoformat(),
        "service": AgentServiceDTO(configuration_status="ready").model_dump(),
        "seller_label": "OmniRank 平台",
        "total_inventory_points": int(q["points_granted"]) + int(q["bonus_points"]),
        "tier_at_order": order_snapshot.get("benefit_tier_at_order") or order_snapshot.get("tier_at_order"),
        "tier_source": order_snapshot.get("tier_source"),
        "tier_bonus_rate_bps": int(order_snapshot.get("benefit_tier_bonus_rate_bps") or order_snapshot.get("tier_bonus_rate_bps") or 0),
        "crosses_tier_threshold": bool(order_snapshot.get("benefit_crosses_tier_threshold", order_snapshot.get("crosses_tier_threshold"))),
        "projected_rolling_12m_yuan": order_snapshot.get("projected_rolling_12m_yuan_snapshot"),
        "reward_description": price_quote.public_procurement_reward_description(order_snapshot),
    }
    return {"success": True, "data": response_data}


@router.get("/quote/{quote_id}")
async def get_quote(quote_id: str, request: Request):
    user = _user(request)
    q = price_quote.get_quote(quote_id)
    if not q:
        raise HTTPException(404, detail="报价不存在")
    if q["buyer_user_id"] != user.get("user_id") and not user.get("is_admin"):
        raise HTTPException(403, detail="无权查看")
    pub = {
        "quote_id": q["quote_id"], "quote_type": q["quote_type"], "status": q["status"],
        "final_price_cents": q["final_price_cents"], "points_granted": q["points_granted"],
        "bonus_points": q["bonus_points"], "currency": q["currency"],
        "price_valid_until": q["expires_at"].isoformat(),
    }
    if user.get("is_admin"):  # 财务/审计可见完整证据链
        pub["_admin"] = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in q.items()}
    return {"success": True, "data": pub}


# ============================================================ admin 发布工作流
class DraftEntry(BaseModel):
    product_code: str
    base_price_cents: int = Field(ge=0)
    multiplier_bps: int = Field(default=10000, ge=0)
    final_price_cents: Optional[int] = Field(default=None, ge=0)
    paid_points: int = Field(ge=0)
    bonus_points: int = Field(default=0, ge=0)
    cost_floor_cents: Optional[int] = None
    usage_example_version: Optional[str] = None


class DraftRequest(BaseModel):
    catalog_type: str
    scope_key: str
    version_code: str
    reason: str = ""
    entries: List[DraftEntry]


@router.post("/admin/catalog/draft")
async def admin_create_draft(req: DraftRequest, request: Request):
    _require_admin(request)
    if req.catalog_type in ("procurement", "retail"):
        raise HTTPException(409, detail={
            "code": "CASH_CATALOG_MANAGED_SOURCE_ONLY",
            "message": "现金目录只能从 /admin/pricing-center 管理配置单向发布，禁止独立编辑",
        })
    try:
        vid = pricing_catalog.create_draft_version(
            catalog_type=req.catalog_type, scope_key=req.scope_key, version_code=req.version_code,
            entries=[e.model_dump() for e in req.entries], reason=req.reason,
            created_by=_user(request).get("user_id"),
        )
    except ValueError as e:
        raise HTTPException(422, detail=str(e))
    problems = pricing_catalog.validate_version(vid)
    return {"success": True, "data": {"version_id": vid, "validation": problems}}


@router.post("/admin/catalog/{version_id}/validate")
async def admin_validate(version_id: int, request: Request):
    _require_admin(request)
    return {"success": True, "data": {"problems": pricing_catalog.validate_version(version_id)}}


class PublishRequest(BaseModel):
    force_approve: bool = False  # 极端价审批(admin 明确批准)


@router.post("/admin/catalog/{version_id}/publish")
async def admin_publish(version_id: int, req: PublishRequest, request: Request):
    _require_admin(request)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT catalog_type FROM pricing_catalog_versions WHERE id=%s", (version_id,))
        _version = cur.fetchone()
    if _version and _version["catalog_type"] in ("procurement", "retail"):
        raise HTTPException(409, detail={
            "code": "CASH_CATALOG_MANAGED_SOURCE_ONLY",
            "message": "现金目录只能经事务化管理源发布适配器发布",
        })
    approver = _user(request).get("user_id") if req.force_approve else None
    try:
        ver = pricing_catalog.publish_version(version_id, approved_by=approver)
    except ValueError as e:
        raise HTTPException(422, detail=str(e))
    return {"success": True, "data": {"version_id": ver["id"], "status": ver["status"],
                                      "effective_from": ver["effective_from"].isoformat() if ver.get("effective_from") else None}}


class RollbackRequest(BaseModel):
    new_version_code: str
    force_approve: bool = False


@router.post("/admin/catalog/{version_id}/rollback")
async def admin_rollback(version_id: int, req: RollbackRequest, request: Request):
    _require_admin(request)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT catalog_type FROM pricing_catalog_versions WHERE id=%s", (version_id,))
        _version = cur.fetchone()
    if _version and _version["catalog_type"] in ("procurement", "retail"):
        raise HTTPException(409, detail={
            "code": "CASH_CATALOG_MANAGED_SOURCE_ONLY",
            "message": "现金目录回滚必须重新从管理源物化为新版本",
        })
    uid = _user(request).get("user_id")
    try:
        ver = pricing_catalog.rollback_to(version_id, new_version_code=req.new_version_code,
                                          created_by=uid, approved_by=uid if req.force_approve else None)
    except ValueError as e:
        raise HTTPException(422, detail=str(e))
    return {"success": True, "data": {"version_id": ver["id"], "status": ver["status"]}}


@router.get("/admin/catalog/versions")
async def admin_list_versions(catalog_type: str, scope_key: str, request: Request):
    _require_admin(request)
    return {"success": True, "data": pricing_catalog.list_versions(catalog_type, scope_key)}


# ============================================================ admin 单向物化发布
class PublicationDryRunRequest(BaseModel):
    service_user_ids: Optional[List[int]] = None


class PublicationReviewScope(BaseModel):
    catalog_type: str = Field(pattern=r"^(procurement|retail)$")
    scope_key: str = Field(min_length=1, max_length=80)
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class PublicationPublishRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    expected_epoch: int = Field(ge=0)
    reviewed_target_mode: str = Field(pattern=r"^(all_active|explicit)$")
    reviewed_service_user_ids: List[int]
    reviewed_scopes: List[PublicationReviewScope]
    # 二次确认清单:必须逐个 scope 点名,不给"全局强制"开关 ——
    # 一个布尔量能把所有服务方一起清空,那正是 07-27 那类事故的放大器。
    confirmed_empty_retail_scopes: List[str] = Field(default_factory=list, max_length=200)


@router.post("/admin/publication/dry-run")
async def admin_publication_dry_run(req: PublicationDryRunRequest, request: Request):
    _require_admin(request)
    from services import pricing_publication
    try:
        data = pricing_publication.dry_run_publication(
            scope_service_user_ids=req.service_user_ids,
        )
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "PUBLICATION_INVALID", "message": str(exc)})
    return {"success": True, "data": data}


@router.post("/admin/publication/publish")
async def admin_publication_publish(req: PublicationPublishRequest, request: Request):
    _require_admin(request)
    from services import pricing_publication
    try:
        data = pricing_publication.publish_all(
            actor_id=int(_user(request).get("user_id")),
            reason=req.reason,
            expected_epoch=req.expected_epoch,
            reviewed_target_mode=req.reviewed_target_mode,
            reviewed_service_user_ids=req.reviewed_service_user_ids,
            reviewed_scopes=[scope.model_dump() for scope in req.reviewed_scopes],
            confirmed_empty_retail_scopes=req.confirmed_empty_retail_scopes,
        )
    except pricing_publication.EmptyRetailCatalogRejected as exc:
        raise HTTPException(
            409,
            detail={
                "code": "EMPTY_RETAIL_CATALOG_REJECTED",
                "message": str(exc),
                "scope_key": exc.scope_key,
                "confirm_field": "confirmed_empty_retail_scopes",
            },
        ) from exc
    except (
        pricing_publication.PublicationEpochConflict,
        pricing_publication.PublicationReviewConflict,
    ) as exc:
        raise HTTPException(
            409,
            detail={"code": "PUBLICATION_REVIEW_CONFLICT", "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            422,
            detail={"code": "PUBLICATION_INVALID", "message": str(exc)},
        ) from exc
    return {"success": True, "data": data}


@router.get("/admin/publication/status")
async def admin_publication_status(request: Request):
    _require_admin(request)
    from services import pricing_publication
    return {"success": True, "data": pricing_publication.publication_status()}


# ============================================================ admin 编号治理(唯一写入口)
class AccountCodeTargets(BaseModel):
    service_user_ids: Optional[List[int]] = None
    channel_user_ids: Optional[List[int]] = None


@router.post("/admin/account-codes/dry-run")
async def admin_account_codes_dry_run(req: AccountCodeTargets, request: Request):
    _require_admin(request)
    try:
        data = account_codes.account_code_dry_run(
            service_user_ids=req.service_user_ids,
            channel_user_ids=req.channel_user_ids,
        )
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc))
    return {"success": True, "data": data}


@router.post("/admin/account-codes/prepare")
async def admin_account_codes_prepare(req: AccountCodeTargets, request: Request):
    _require_admin(request)
    try:
        data = account_codes.prepare_account_codes(
            service_user_ids=req.service_user_ids,
            channel_user_ids=req.channel_user_ids,
        )
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc))
    return {"success": True, "data": data}


@router.get("/admin/account-codes/status")
async def admin_account_codes_status(request: Request):
    _require_admin(request)
    return {"success": True, "data": account_codes.account_code_status()}


# ============================================================ admin 渠道关系
class ChannelRelChange(BaseModel):
    buyer_dealer_id: int
    upstream_channel_account_id: int
    expected_relationship_version: Optional[str] = None
    new_relationship_version: str = Field(min_length=1, max_length=128)
    cost_multiplier_bps: int = Field(default=10000, ge=10000)
    reason: str = ""


class ChannelRelBatchRequest(BaseModel):
    relationships: List[ChannelRelChange] = Field(min_length=1, max_length=500)


@router.post("/admin/channel/relationships/dry-run")
async def admin_channel_dry_run(req: ChannelRelBatchRequest, request: Request):
    _require_admin(request)
    try:
        data = channel_pricing.dry_run_relationships(
            [item.model_dump() for item in req.relationships]
        )
    except ChannelError as exc:
        raise HTTPException(422, detail={"code": "CHANNEL_RELATIONSHIP_INVALID", "message": str(exc)})
    return {"success": True, "data": data}


@router.put("/admin/channel/relationships/{buyer_dealer_id}")
async def admin_save_channel(
    buyer_dealer_id: int, req: ChannelRelChange, request: Request,
):
    _require_admin(request)
    if int(req.buyer_dealer_id) != int(buyer_dealer_id):
        raise HTTPException(422, detail="路径 buyer_dealer_id 与请求体不一致")
    uid = _user(request).get("user_id")
    try:
        data = channel_pricing.save_relationships(
            [req.model_dump()], approved_by=uid, created_by=uid,
        )
    except ChannelError as e:
        raise HTTPException(409, detail={"code": "CHANNEL_RELATIONSHIP_CONFLICT", "message": str(e)})
    return {"success": True, "data": data}


@router.put("/admin/channel/relationships:batch")
async def admin_save_channel_batch(req: ChannelRelBatchRequest, request: Request):
    _require_admin(request)
    uid = _user(request).get("user_id")
    try:
        data = channel_pricing.save_relationships(
            [item.model_dump() for item in req.relationships], approved_by=uid, created_by=uid,
        )
    except ChannelError as exc:
        raise HTTPException(409, detail={"code": "CHANNEL_RELATIONSHIP_CONFLICT", "message": str(exc)})
    return {"success": True, "data": data}


# backward route:同样要求新 OCC 合同，不再接受无 expected 的旧 replace 语义
@router.post("/admin/channel/relationship")
async def admin_create_channel(req: ChannelRelChange, request: Request):
    return await admin_save_channel(req.buyer_dealer_id, req, request)


@router.delete("/admin/channel/relationship/{dealer_id}")
async def admin_archive_channel(
    dealer_id: int, request: Request, expected_relationship_version: str,
):
    _require_admin(request)
    try:
        n = channel_pricing.archive_relationship(
            dealer_id, expected_relationship_version=expected_relationship_version,
        )
    except ChannelError as exc:
        raise HTTPException(409, detail={"code": "CHANNEL_RELATIONSHIP_CONFLICT", "message": str(exc)})
    return {"success": True, "data": {"archived": n}}


@router.get("/admin/channel/status")
async def admin_channel_status(request: Request):
    _require_admin(request)
    return {"success": True, "data": channel_pricing.relationship_status()}


@router.get("/admin/readiness")
async def admin_pricing_readiness(request: Request):
    """只读开闸证据；永不翻 flag，失败也返回 200 + ready=false。"""
    _require_admin(request)
    return {"success": True, "data": pricing_readiness.get_readiness()}


# ============================================================ 财务人工对账(P0-11)
@router.get("/admin/reconciliation/order/{order_id}")
async def reconciliation_order(order_id: str, request: Request):
    """按订单号导出完整证据链:付款人/实付/渠道/退款/渠道收益/结算(§15.2#20)。
    仅 admin/财务;整数分;无税计算(税由人工对账)。"""
    _require_admin(request)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM recharge_orders WHERE id = %s", (order_id,))
        order = cur.fetchone()
        if not order:
            raise HTTPException(404, detail="订单不存在")
        order = dict(order)
        cur.execute("SELECT * FROM channel_revenue_ledger WHERE recharge_order_id = %s", (order_id,))
        channel_rev = cur.fetchone()
        quote = None
        if order.get("price_quote_id"):
            cur.execute("SELECT * FROM price_quotes WHERE quote_id = %s", (order["price_quote_id"],))
            quote = cur.fetchone()

    def _iso(v):
        return v.isoformat() if hasattr(v, "isoformat") else v

    return {"success": True, "data": {
        "order_id": order["id"],
        "payer_user_id": order["user_id"],
        "paid_amount_cents": order["amount_cents"],
        "payment_method": order.get("payment_method"),
        "payment_status": order.get("payment_status"),
        "payment_id": order.get("payment_id"),
        "refund_status": order.get("refund_status"),
        "price_quote_id": order.get("price_quote_id"),
        "pricing_catalog_version": order.get("pricing_catalog_version"),
        "pricing_snapshot": order.get("pricing_snapshot_jsonb"),        # 原始定价证据(不可覆盖)
        "settlement_snapshot": order.get("settlement_snapshot_jsonb"),  # 结算证据(分列)
        "channel_revenue": {k: _iso(v) for k, v in dict(channel_rev).items()} if channel_rev else None,
        "quote": {k: _iso(v) for k, v in dict(quote).items()} if quote else None,
        "seller_legal_entity": "PLATFORM",
        "note": "税务由财务线下人工处理 · 系统不计算税率/税点",
    }}
