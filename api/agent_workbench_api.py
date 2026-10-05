"""
V3.5 W2 · 代理工作台 API(代理库存 + 客户额度 + 白标定价 + 工厂结算 + 推广中心)

15 endpoints · 严格 DTO 隔离(详见 schemas/v35_w2_dto.py)
- 所有响应禁含:platform_cost_cents / raw_cost / tax_rate_bps / tax_mode / *_fee_bps
- 所有 endpoint 必须做 agent ownership 校验(agent_level >= 1)
- 客户归属校验(代理只能操作自己绑定的客户)

memory feedback_v35_factory_inventory_model_v6
"""

import hashlib
import json
import logging
import math
import secrets
import time
import uuid
from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Request, HTTPException, Query, Depends
from pydantic import BaseModel, ConfigDict, Field

from auth.agreement_gate import require_signed_agreement, require_pricing_authority

from db.connection import get_db
from services.agent_inventory import (
    get_or_create_inventory_wallet,
    purchase_inventory_prepay,
    allocate_offline,
    revoke_from_customer,
    check_inventory_alert_level,
)
# [单账本接线 2026-08-17 · C-3] get_or_create_customer_wallet 已不再 import ——
# 它会往停写表 INSERT,本文件唯一调用点(GET .../credit)已改纯读 user_wallets。
from services.customer_credit import compute_unspent_from_order
from services.customer_binding import (
    get_customer_binding,
    list_agent_customers,
)
# 🔴 关系判别**唯一入口**(工单 v3 §P0-1:不得再造第二个关系解析函数)。
#    搜索、划拨签发、渠道申请三条链路共用这一份口径。
from services.channel_partner_requests import (
    resolve_relationship as _resolve_relationship,
    quote_downstream_purchase as _resolve_downstream_quote,
    ChannelPartnerRequestError as _RelationError,
)
from services.relationship_privacy import strip_private_relationship_fields
from services.agent_pricing import (
    strip_admin_fields,
    create_agent_sku_override,
    update_agent_sku_override,
    delete_agent_sku_override,
    calc_prepay_points,
    calc_settlement,
    label_margin,
    MAX_RETAIL_MULTIPLIER,
    RetailSKUIdempotencyConflict,
    RetailSKUNotFound,
    RetailSKUVersionConflict,
)
from services.agent_revenue import (
    get_agent_balance,
    create_settlement_request,
)
from services.channel_tier import get_agent_channel_tier_state
from services.payment_routing import detect_payment_channel, resolve_payment_channel
from services.pricing_publication import PricingPublicationError
from schemas.v35_w2_dto import (
    AgentInventoryBalanceResponse,
    AgentInventoryTransactionItem,
    AgentInventoryTransactionsResponse,
    AgentPurchaseOption,
    AgentPurchaseOptionsResponse,
    AgentPurchaseRequest,
    AgentPurchaseCreateResponse,
    AgentPurchasePreviewRequest,
    AgentPurchasePreviewResponse,
    AllocateOfflineRequest,
    SupplyDownstreamRequest,
    SupplyDownstreamResponse,
    RevokeOfflineRequest,
    AllocateRevokeResultResponse,
    AgentCustomerLookupItem,
    AgentCustomerLookupResponse,
    AgentCustomerCreditResponse,
    AgentCustomerCreditTxItem,
    AgentCustomerCreditTxResponse,
    AgentSKUItem,
    AgentSKUListResponse,
    AgentSKUUpdateRequest,
    AgentSKUCreateRequest,
    AgentSKUPreviewRequest,
    AgentSettlementBalanceResponse,
    AgentLedgerItem,
    AgentAvailableItemsResponse,
    AgentSettlementRequestCreate,
    AgentSettlementRequestResponse,
    AgentPromotionQRResponse,
    AgentPromotionCustomerItem,
    AgentPromotionCustomersResponse,
    AgentClientPurchaseSettingsRequest,
    AgentClientPurchaseSettingsResponse,
    AgentCustomerPurchaseOverrideRequest,
    AgentCustomerPurchaseOverrideResponse,
)
from auth.user_ctx import current_user_id

from api.refusal_log import refuse

logger = logging.getLogger("GEO-V35-W2-AgentAPI")
router = APIRouter(prefix="/api/agent", tags=["V35-W2-代理工作台"])


def _public_procurement_quote_error(exc: Exception, operation: str) -> HTTPException:
    """Keep procurement settlement details in server logs, never public errors."""

    logger.warning(
        "[agent-purchase] %s quote validation failed: %s: %s",
        operation,
        type(exc).__name__,
        exc,
    )
    return HTTPException(
        422,
        detail={
            "code": "PROCUREMENT_QUOTE_INVALID",
            "message": "当前进货报价已失效或不可用，请刷新后重试",
        },
    )


def _reject_platform_direct_procurement(a: dict) -> None:
    """[WO_241 甲] 平台直营账号不进货 —— 进 handler 即拒,**不进算价**。

    事实(台账 09-19 13:35,回滚事务复现):admin 经 `_require_agent` 换成平台直营
    账号 136,而 `build_cash_anchored_quote_terms` 对它抛
    `ResaleError「厂家与最终买方身份非法」`——**平台自己既是厂家又是买方**。
    对照:真服务商 89 三档全通。全站 7 个 admin 都会撞。

    🔴 为什么拦在进 handler 处而不是把那个异常映射得好看些:
       平台直营**根本没有"进货"这件事**(它不从谁那里进)。
       让它一路走到算价再报错,等于用一个结算异常去表达一件业务上不存在的事,
       而那个异常的文案还会把厂家/买方结构说出去。
       **这不是报错措辞问题,是这条路对它就不该存在。**

    🔴 三个入口共用这一处判断:同一个谓词写三遍,迟早有一处跟不上
       (本仓 feedback_one_predicate_one_place_or_half_goes_unverified)。
    """
    # [WO_254 2026-09-20] 码与文案搬去 `services/platform_direct_procurement`,
    # 因为进货中心那一屏打的是 `/api/pricing/procurement/catalog`,不是本端点 ——
    # 原话必须从**同一处**同时供给两个端点,否则改一处就有一边跟不上。
    from services.platform_direct_procurement import platform_direct_no_procurement
    if str(a.get("operating_context") or "") == "platform_direct":
        raise platform_direct_no_procurement()

# Readiness imports this only after all three live inventory endpoints share the
# published procurement quote path.
PROCUREMENT_QUOTE_WIRING_CAPABILITY = "procurement-price-quote-v1"
PROCUREMENT_QUOTE_WIRING_BEHAVIOR = {
    "contract_version": "geo-persisted-price-quote-v2",
    "entry_id": "agent-inventory-purchase",
    "active_route": "/agent/inventory",
    "quote_type": "procurement",
    "quote_endpoints": {
        "fixed": "/api/pricing/procurement/quote",
        "custom_amount": "/api/agent/inventory/purchase-preview",
    },
    "order_endpoint": "/api/agent/inventory/purchase",
    "required_order_field": "price_quote_id",
    "quote_persistence": "price_quotes",
    "callback_pricing_source": "order_snapshot",
    "cash_semantics": "payable_amount_immutable",
    "relationship_effect": "paid_inventory_points_only",
    "quote_required_enforcement": "request_pre_writer",
    "supports_custom_amount": True,
}


# ============================================================
# 鉴权 helper
# ============================================================

def _require_agent(request: Request) -> dict:
    """Resolve the service-provider subject without conflating it with the operator.

    A provider operates its own business identity.  An administrator keeps the
    historical patrol/managed-operation access, but operates the dedicated
    platform-direct service account rather than the administrator's personal
    account.  Ordinary accounts remain forbidden.
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    user_id = user.get("user_id") or current_user_id(user)
    if not user_id:
        raise HTTPException(status_code=401, detail="登录信息不完整")

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT agent_level FROM user_wallets WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        agent_level = (row.get("agent_level") if row and isinstance(row, dict) else (row[0] if row else 0)) or 0
    actor_user_id = int(user_id)
    is_admin = bool(user.get("is_admin", False))
    if is_admin:
        try:
            from services.commercial_service_routing import read_platform_direct_service_identity

            with get_db() as conn:
                service_user = read_platform_direct_service_identity(conn.cursor())
        except Exception as exc:
            logger.error(
                "admin business context unavailable actor=%s: %s",
                actor_user_id,
                exc,
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "ADMIN_OPERATING_CONTEXT_UNAVAILABLE",
                    "message": "平台直营经营账号暂不可用，请联系运维检查配置",
                },
            ) from exc
        return {
            "user_id": int(service_user["id"]),
            "agent_level": int(service_user["agent_level"]),
            "is_admin": True,
            "operator_user_id": actor_user_id,
            "operating_context": "platform_direct",
        }
    # [F-1] 组织员工席位:经营主体是他所属组织的 owner,不是他自己。
    # 沿用本函数既有的"经营主体 ≠ 操作者"结构(与 admin 走平台直营账号同形):
    # 主体 = principal,操作者 = 员工本人,operating_context 标出这是席位代作业。
    # 员工自己的 agent_level 保持真值 0,这里不写回、不改钱包。
    organization_identity = getattr(request.state, "organization_identity", None)
    if organization_identity is not None and getattr(organization_identity, "is_member", False):
        principal_user_id = int(organization_identity.principal_user_id)
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT agent_level FROM user_wallets WHERE user_id = %s", (principal_user_id,))
            prow = cur.fetchone()
            principal_level = (
                prow.get("agent_level") if prow and isinstance(prow, dict) else (prow[0] if prow else 0)
            ) or 0
        if int(principal_level) < 1:
            # 所属组织本身不是服务商 —— 员工同样进不去(继承的是能力,不是凭空获得)
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "AGENT_IDENTITY_REQUIRED",
                    "message": "所属服务商账号尚未开通经营后台",
                },
            )
        return {
            "user_id": principal_user_id,
            "agent_level": int(principal_level),
            "is_admin": False,
            "operator_user_id": actor_user_id,
            "operating_context": "organization_seat",
        }

    if int(agent_level) < 1:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "AGENT_IDENTITY_REQUIRED",
                "message": "当前账号不是服务商，请使用服务商账号进入经营后台",
            },
        )
    return {
        "user_id": actor_user_id,
        "agent_level": int(agent_level),
        "is_admin": False,
        "operator_user_id": actor_user_id,
        "operating_context": "self",
    }


def _lock_agent_identity_for_purchase(cursor, agent_user_id: int) -> None:
    """Recheck the business identity inside the order-writing transaction."""
    cursor.execute(
        "SELECT COALESCE(agent_level,0) AS agent_level FROM user_wallets "
        "WHERE user_id=%s FOR UPDATE",
        (int(agent_user_id),),
    )
    row = cursor.fetchone()
    level = int((row.get("agent_level") if isinstance(row, dict) else row[0]) or 0) if row else 0
    if level < 1:
        raise HTTPException(
            status_code=403,
            detail={"code": "AGENT_IDENTITY_REQUIRED", "message": "当前账号不是服务商，不能创建进货订单"},
        )


def _require_logged_in_user_id(request: Request) -> int:
    """[Option B 2026-06-08] 仅要求登录(不限服务商)· 用于所有登录用户可访问且只读写自己行(WHERE id=self)的端点。"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    user_id = user.get("user_id") or current_user_id(user)
    if not user_id:
        raise HTTPException(status_code=401, detail="登录信息不完整")
    return user_id


def _require_customer_owned_by_agent(cursor, agent_user_id: int, customer_user_id: int) -> dict:
    """锁定商业关系后确认归属；争议态和并发换绑均 fail-closed。"""
    from services.commercial_service_routing import lock_commercial_binding_subject

    lock_commercial_binding_subject(cursor, int(customer_user_id))
    cursor.execute(
        """SELECT * FROM customer_agent_bindings
           WHERE customer_user_id=%s FOR UPDATE""",
        (int(customer_user_id),),
    )
    binding = cursor.fetchone()
    binding = dict(binding) if binding else None
    if not binding:
        # 🔴 [工单 v3 §P0-2] 这里原来先查目标的 `agent_level`,只要是服务商就回
        #    「该账号是服务商,不能作为客户接收划拨」。**那是一个账号枚举面**:
        #    操作者随便填一个手机号/ID 就能问出"此号注册没有、是不是服务商",
        #    而手机号是可枚举的。工单 §P0-2 明令禁止对**陌生账号**做这类身份提示。
        #
        #    改法:先看**有向**关系。
        #      · 有渠道关系(我的下线)→ 可以明说 + 给真实出口(R4:关系存在必须能操作);
        #      · 没有任何关系(含"目标其实是我的上游")→ 一律同一句话,
        #        与"账号不存在"不可区分(R5)。
        try:
            rel = _resolve_relationship(cursor, int(agent_user_id), int(customer_user_id))
        except _RelationError:
            rel = None
        if rel and rel.get("has_channel_relationship"):
            # 🔴 [P0 热修 §4] 这里原来把 `headline`(「这是你的下线服务商」)当错误 message 返回,
            #    前端 `lazyToast.error` 一渲染就是**红底 ⊗ + 一句身份识别结果**,
            #    用户第一眼是"我被拒了"。识别结果不是错误。
            #    现在正确路径是「供货给下线」端点,所以这里只在**用错端点**时触发,
            #    message 换成一句能执行的话,不再复述身份。
            raise HTTPException(
                status_code=409,
                detail=strip_private_relationship_fields({
                    "code": "TARGET_IS_DOWNSTREAM_PARTNER",
                    "message": "对下线服务商请用「供货给下线」,算力会进 TA 的库存算力",
                    "effect_note": rel["effect_note"],
                    "primary_action": rel["primary_action"],
                    "secondary_actions": rel["secondary_actions"],
                }),
            )
        raise HTTPException(
            status_code=409,
            detail={"code": "COMMERCIAL_BINDING_REQUIRED", "message": "该客户尚未由平台绑定给当前服务商"},
        )
    dispute_status = str(binding.get("dispute_status") or "").strip().lower()
    if dispute_status not in {"", "resolved", "reverted"}:
        raise HTTPException(
            status_code=409,
            detail={"code": "COMMERCIAL_BINDING_DISPUTED", "message": "该客户服务关系正在平台处理中"},
        )
    if int(binding["agent_user_id"]) != int(agent_user_id):
        raise HTTPException(status_code=403, detail="该客户不属于当前服务方")
    return binding


@router.get("/channel-tier/me")
async def agent_channel_tier_me(request: Request):
    a = _require_agent(request)
    with get_db() as conn:
        cur = conn.cursor()
        state = get_agent_channel_tier_state(cur, int(a["user_id"]))
    public_state = {key: value for key, value in state.items() if key != "agent_user_id"}
    return {"success": True, "channel_tier": public_state}

_PG_INT4_MAX = 2_147_483_647


def _parse_lookup_user_id(keyword: str) -> Optional[int]:
    """Only parse values that can be safely compared to PostgreSQL int columns."""
    text = (keyword or "").strip()
    if not text.isdigit():
        return None
    if len(text) > 10:
        return None
    value = int(text)
    if value > _PG_INT4_MAX:
        return None
    return value


def _looks_like_phone_label(value: Optional[str]) -> bool:
    if not value:
        return False
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return len(digits) >= 7 and len(digits) >= len(str(value).strip()) - 3


def _safe_lookup_display_name(customer_user_id: int, display_name: Optional[str]) -> str:
    name = (display_name or "").strip()
    if name and not _looks_like_phone_label(name):
        return name
    return f"客户 {customer_user_id}"


# 搜索结果里的关系态 —— 与 `services.channel_partner_requests` 的 relation 一一对应。
# 🔴 注意**没有** upstream 态:上游永远不出现在下级的搜索结果里(工单 §0.1 R5)。
_BINDING_STATUS_OWNED = "owned"                 # 仅客户绑定
_BINDING_STATUS_DOWNSTREAM = "downstream_partner"  # 仅渠道关系(我的下线服务商)
_BINDING_STATUS_BOTH = "both"                   # 两种关系都有

_RELATION_TO_BINDING_STATUS = {
    "customer": _BINDING_STATUS_OWNED,
    "downstream_partner": _BINDING_STATUS_DOWNSTREAM,
    "both": _BINDING_STATUS_BOTH,
}


def _lookup_target_is_provider(row: dict) -> bool:
    """目标是不是**服务商**(而不是普通客户)。

    两个信源取或(工单 v2 §R1):
      · `target_identity == 'service_provider'` —— 由 `resolve_relationship`
        当场重读 `user_wallets.agent_level` 得到,是身份的第一手来源;
      · 有**生效渠道关系**(`downstream_partner` / `both`)—— 渠道关系只在
        服务商之间存在,所以它单独就足以判定;身份读取失败也不会漏判。
    """
    if str(_g(row, "target_identity") or "") == "service_provider":
        return True
    return str(_g(row, "binding_status") or "") in (
        _BINDING_STATUS_DOWNSTREAM, _BINDING_STATUS_BOTH,
    )


def _shape_agent_customer_lookup_row(row: dict) -> dict:
    """Project lookup rows with stricter privacy for non-owned customers."""
    cid = int(_g(row, "customer_user_id", 0) or 0)
    binding_status = _g(row, "binding_status") or "unbound"
    base = {
        "customer_user_id": cid,
        "phone_masked": _mask_phone(_g(row, "phone")),
        "binding_status": binding_status,
        # [单账本收敛残留 · 工单 v2 §R3] 这三个数现在来自客户自己的 `user_wallets`
        # (`tool_credit_points` ← paid_points · `publish_credit_points` 恒 0 ·
        #  `bonus_credit_points` ← bonus_points)。字段名保留是**前端契约**,
        # 不是还有三个池子 —— 取数 SQL 见 `_lookup_agent_customers`。
        "tool_credit_points": int(_g(row, "tool_credit_points", 0) or 0),
        "publish_credit_points": int(_g(row, "publish_credit_points", 0) or 0),
        "bonus_credit_points": int(_g(row, "bonus_credit_points", 0) or 0),
    }
    # 三种关系态都是**已确认的有向关系**(target 是 actor 的下级/客户),
    # 展示名对上游可见 —— 工单 §0.1 R5 表第 1 行。无关系的账号根本进不到这里。
    if binding_status in (
        _BINDING_STATUS_OWNED, _BINDING_STATUS_DOWNSTREAM, _BINDING_STATUS_BOTH,
    ):
        # 🔴 [工单 v2 §R1] 目标是**服务商**时 `brand_name` 一律置 NULL。
        #    生产实证 2026-08-17:133 搜 163(自己的下线服务商)时卡标题显示
        #    「贵州省禾椒香食品有限公司」—— 那是 163 **自己客户**的品牌名,
        #    由 SQL 里 `ORDER BY b.id DESC LIMIT 1` 的子查询捞出来的。
        #    服务商名下的品牌是 TA 的经营资产,不是 TA 的名字;把它当标题
        #    等于把下线的客户名单第一条透给上游 —— 多一个信息位就是一个泄露位。
        #    🔴 在**响应里**拿掉,不是让前端别渲染:前端少一个入口不是边界。
        base.update({
            "display_name": _safe_lookup_display_name(cid, _g(row, "display_name")),
            "brand_name": None if _lookup_target_is_provider(row) else _g(row, "brand_name"),
        })
    # 关系文案与出口(工单 §P0-2 表 + §P1-3 出口字典)· 由统一解析函数产出,
    # 搜索结果与划拨前解析用**同一套口径**,不在这里另写一份 if/else。
    for key in (
        "relation", "target_identity", "headline", "effect_note",
        "primary_action", "secondary_actions", "allowed_actions", "ledger_note",
    ):
        if key in row:
            base[key] = _g(row, key)
    return base


#: [工单 v2 §R3] 搜索结果里的"对方当前算力" —— 取**客户自己的 `user_wallets`**。
#
#  🔴 为什么改:两条 SQL 原来 `LEFT JOIN customer_agent_credit_wallets`,而那张表
#     自 2026-07-27 单账本收敛(基线 `e4adbf2f6`)起**已停写**。老客户还有残行,
#     于是搜索显示的是**冻结在拆除那天**的旧数;拆除后才有的客户则恒显示 0。
#     两种都是"把陈旧/未知伪装成一个确定数字"。
#  🔴 字段名 `tool_/publish_/bonus_credit_points` **保持不变**(前端契约 + DTO 兼容,
#     响应模型不新增字段 —— `extra="forbid"` 第三次事故的先例)。语义改为:
#       tool_credit_points  ← user_wallets.paid_points (充值算力)
#       publish_credit_points ← 恒 0(单账本后没有独立发布池)
#       bonus_credit_points ← user_wallets.bonus_points (赠送算力)
_LOOKUP_WALLET_COLUMNS = """
               COALESCE(uw.paid_points, 0) AS tool_credit_points,
               0 AS publish_credit_points,
               COALESCE(uw.bonus_points, 0) AS bonus_credit_points,
"""
_LOOKUP_WALLET_JOIN = "LEFT JOIN user_wallets uw ON uw.user_id = u.id"


def _lookup_agent_customers(cursor, agent_user_id: int, query: str, owned_only: bool, limit: int) -> list[dict]:
    """服务商侧客户搜索只返回已明确绑定到当前服务商的客户。"""
    keyword = (query or "").strip()
    if not keyword:
        return []
    safe_limit = max(1, min(int(limit or 8), 20))
    like = f"%{keyword}%"
    lookup_user_id = _parse_lookup_user_id(keyword)
    has_lookup_user_id = lookup_user_id is not None

    cursor.execute(
        f"""
        SELECT DISTINCT ON (u.id)
               u.id AS customer_user_id,
               NULLIF(u.display_name, '') AS display_name,
               u.phone AS phone,
               {_LOOKUP_WALLET_COLUMNS}
               (
                   SELECT b.name
                   FROM brands b
                   WHERE b.owner_user_id = u.id
                   ORDER BY b.id DESC
                   LIMIT 1
               ) AS brand_name,
               'owned' AS binding_status
        FROM customer_agent_bindings cab
        JOIN users u ON u.id = cab.customer_user_id
        {_LOOKUP_WALLET_JOIN}
        WHERE cab.agent_user_id = %s
          AND (
              (%s AND u.id = %s)
              OR u.phone = %s
              OR u.display_name ILIKE %s
              OR EXISTS (
                  SELECT 1 FROM brands b2
                  WHERE b2.owner_user_id = u.id AND b2.name ILIKE %s
              )
          )
        ORDER BY u.id
        LIMIT %s
        """,
        (agent_user_id, has_lookup_user_id, lookup_user_id or 0, keyword, like, like, safe_limit),
    )
    owned_rows = [_dict_row(row, [
        "customer_user_id", "display_name", "phone", "tool_credit_points",
        "publish_credit_points", "bonus_credit_points", "brand_name", "binding_status",
    ]) for row in cursor.fetchall()]

    # 🔴 [工单 v3 §1.3 缺陷本体] 第二张关系表 —— 这就是 Owner 指出的
    #    「像现在划拨给用户是找不到的」。生产实证(2026-08-13 只读取证):
    #      u46 搜 13800138000(u18,自己的下线,渠道关系 id=3 active)→ 命中 0 条,
    #    因为上面那条 SQL 只 `FROM customer_agent_bindings`。
    #    按 R4「关系存在 ⇒ 必须找得到、能操作」,搜不到是缺陷,不是保护。
    #
    # 🔴 有向性(R5):`buyer_dealer_id = 目标` / `upstream_channel_account_id = 我`。
    #    这两个占位符**不能对调** —— 对调就成了"查我的上游是谁"。
    cursor.execute(
        f"""
        SELECT DISTINCT ON (u.id)
               u.id AS customer_user_id,
               NULLIF(u.display_name, '') AS display_name,
               u.phone AS phone,
               {_LOOKUP_WALLET_COLUMNS}
               (
                   SELECT b.name
                   FROM brands b
                   WHERE b.owner_user_id = u.id
                   ORDER BY b.id DESC
                   LIMIT 1
               ) AS brand_name
        FROM channel_pricing_relationships cpr
        JOIN users u ON u.id = cpr.buyer_dealer_id
        {_LOOKUP_WALLET_JOIN}
        WHERE cpr.upstream_channel_account_id = %s
          AND cpr.status = 'active'
          AND cpr.effective_to IS NULL
          AND (
              (%s AND u.id = %s)
              OR u.phone = %s
              OR u.display_name ILIKE %s
              OR EXISTS (
                  SELECT 1 FROM brands b2
                  WHERE b2.owner_user_id = u.id AND b2.name ILIKE %s
              )
          )
        ORDER BY u.id
        LIMIT %s
        """,
        (agent_user_id, has_lookup_user_id, lookup_user_id or 0, keyword, like, like, safe_limit),
    )
    channel_rows = [_dict_row(row, [
        "customer_user_id", "display_name", "phone", "tool_credit_points",
        "publish_credit_points", "bonus_credit_points", "brand_name",
    ]) for row in cursor.fetchall()]

    # ``owned_only`` is retained only for old callers.  It must never reopen the
    # former exact-phone lookup for unbound users: a provider cannot discover or
    # claim a customer before the admin CAS relationship workflow has completed.
    del owned_only
    rows = owned_rows + channel_rows

    result: list[dict] = []
    seen: set[int] = set()
    for row in rows:
        cid = int(_g(row, "customer_user_id", 0) or 0)
        if not cid or cid in seen:
            continue
        seen.add(cid)
        # 🔴 关系态与出口一律由**统一解析函数**给出,搜索这里不自己判 if/else。
        #    工单 §P0-1:「不得再造第二个关系解析函数(会立刻产生第二套判别口径)」。
        #    这里也顺带满足 §P0-1 第 5 条:身份每次重新读取,不吃 binding 的缓存判断。
        try:
            rel = _resolve_relationship(cursor, int(agent_user_id), cid)
        except Exception:  # noqa: BLE001 —— 关系在两条 SQL 之间被 admin 改掉:当作无关系跳过
            continue
        merged = dict(row)
        merged.update(rel)
        merged["binding_status"] = _RELATION_TO_BINDING_STATUS.get(
            rel.get("relation"), _BINDING_STATUS_OWNED
        )
        result.append(_shape_agent_customer_lookup_row(merged))
        if len(result) >= safe_limit:
            break
    return result


# ============================================================
# 1-2. 代理库存 · balance + transactions
# ============================================================

@router.get("/inventory/balance", response_model=AgentInventoryBalanceResponse)
async def agent_inventory_balance(request: Request):
    a = _require_agent(request)
    with get_db() as conn:
        cur = conn.cursor()
        wallet = get_or_create_inventory_wallet(cur, a["user_id"])
        alert_tuple = check_inventory_alert_level(cur, a["user_id"])
        conn.commit()
    # check_inventory_alert_level → Tuple[level_str, extra_dict]
    alert_level = alert_tuple[0] if isinstance(alert_tuple, tuple) else str(alert_tuple)
    return AgentInventoryBalanceResponse(
        paid_inventory_points=wallet["paid_inventory_points"],
        bonus_inventory_points=wallet["bonus_inventory_points"],
        frozen_inventory_points=wallet["frozen_inventory_points"],
        total_purchased_points=wallet["total_purchased_points"],
        total_allocated_points=wallet["total_allocated_points"],
        alert_level=alert_level,
    )


@router.post("/inventory/self-use")
async def agent_inventory_self_use(request: Request):
    """库存转可用算力(自用)· 1:1(工单 §P0-3)。

    这条通道存在的理由是一起真实事故:服务商付了 ¥50,算力进了**库存钱包**,
    而扣费链只认 `user_wallets` —— 钱在账上却一分动不了,名下又没有客户可划,
    最后只能由人手工进数据库。

    资金口径 **① 自用 1:1**(Owner 2026-08-12 拍板,已回写 `docs/SYSTEM_TRUTH/08_billing.md`):
    已知并接受套利面 —— 库存按进货价买入、钱包按零售价消费。

    技术上取工单 §P0-3 的方案 (b):**不碰 `middleware/billing.py`**(受保护文件),
    只新增"把库存划给自己"这一个动作,扣费链一行不改。
    """
    from schemas.admin_agent_inventory import AgentSelfUseRequest
    from services.admin_agent_inventory import (
        InventoryAdminError,
        convert_inventory_for_self_use,
    )

    a = _require_agent(request)
    try:
        payload = AgentSelfUseRequest(**(await request.json()))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — DTO 校验失败按 422 回,不 500
        raise refuse(logger, status=422, code="INVALID_REQUEST_PAYLOAD",
                     detail=f"请求参数不合法:{exc}",
                     context={"agent": a.get("user_id")}) from exc

    request_id = str(
        request.headers.get("X-Request-ID")
        or getattr(request.state, "request_id", None)
        or uuid.uuid4().hex
    )[:128]
    forwarded = request.headers.get("X-Forwarded-For")
    ip_address = (
        forwarded.split(",")[0].strip() if forwarded
        else (request.client.host if request.client else "unknown")
    )
    try:
        return convert_inventory_for_self_use(
            int(a["user_id"]),
            paid_points=payload.paid_points,
            bonus_points=payload.bonus_points,
            reason=payload.reason,
            # 自用是服务商自己的动作,操作人就是本人。
            operator_user_id=int(a["user_id"]),
            operator_username=a.get("username"),
            request_id=request_id,
            ip_address=ip_address,
        )
    except InventoryAdminError as exc:
        status = 409 if exc.code in {
            "DUPLICATE_REQUEST", "INVENTORY_LOT_DRIFT", "LOT_REJECTED",
            "SELF_USE_NOT_CONSERVED", "PLATFORM_SELLER_USE_ISSUANCE",
            "TARGET_NOT_SERVICE_PROVIDER", "BUSINESS_IDENTITY_SSOT_UNAVAILABLE",
        } else 400
        detail = {"code": exc.code, "message": str(exc)}
        detail.update(exc.details or {})
        raise refuse(logger, status=status, code=exc.code, detail=detail,
                     context={"message": str(exc)}) from exc


@router.get("/inventory/transactions", response_model=AgentInventoryTransactionsResponse)
async def agent_inventory_transactions(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    a = _require_agent(request)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) AS c FROM agent_inventory_transactions
            WHERE agent_user_id = %s
        """, (a["user_id"],))
        total = _row_count(cur.fetchone())
        cur.execute("""
            SELECT id, type, pool, points, balance_paid_after, balance_bonus_after,
                   related_customer_user_id, related_order_id, description, created_at
            FROM agent_inventory_transactions
            WHERE agent_user_id = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """, (a["user_id"], limit, offset))
        items = [
            AgentInventoryTransactionItem(**_dict_row(row, [
                "id","type","pool","points","balance_paid_after","balance_bonus_after",
                "related_customer_user_id","related_order_id","description","created_at",
            ])) for row in cur.fetchall()
        ]
    return AgentInventoryTransactionsResponse(items=items, total=total)


# ============================================================
# 3-4. 代理进货(入口 A · 预付充值)
# ============================================================

# 代理预付进货:走出厂折扣换算(示例 9 折:1 元出厂 ≈ 144.4 出厂积分)
# 公式:base_points = amount_cents * 325 // 225(SKU 量纲铁律 points × 225 == cents × 325)
# bonus_points = base_points × 进货奖励比例(示例 5% · 营销首次/月度可送)
PURCHASE_OPTIONS = [
    {"amount_cents": 100000, "base_points": 144444, "bonus_points": 7222,
     "label": "¥1000 进货 · 充值 144,444 + 赠送 7,222(首次/月度 5% 赠送)",
     "is_first_month_bonus": True},
    {"amount_cents": 500000, "base_points": 722222, "bonus_points": 36111,
     "label": "¥5000 进货 · 充值 722,222 + 赠送 36,111(5% 赠送)",
     "is_first_month_bonus": True},
    {"amount_cents": 1000000, "base_points": 1444444, "bonus_points": 72222,
     "label": "¥10000 进货 · 充值 1,444,444 + 赠送 72,222(5% 赠送)",
     "is_first_month_bonus": True},
]


class AgentPurchasePreviewWiringRequest(BaseModel):
    """Compatibility envelope for fixed-option or free-amount purchase previews."""
    model_config = ConfigDict(extra="forbid")
    amount_cents: Optional[int] = Field(default=None, gt=0, le=2_000_000_000)
    option_id: Optional[str] = None
    idempotency_key: Optional[str] = Field(default=None, max_length=128)


class AgentPurchasePreviewWiringResponse(AgentPurchasePreviewResponse):
    price_quote_id: Optional[str] = None
    product_code: Optional[str] = None
    expires_at: Optional[datetime] = None


class AgentPurchaseWiringRequest(BaseModel):
    """Quoted branch accepts only price_quote_id; legacy fields remain flag-compatible."""
    model_config = ConfigDict(extra="forbid")
    price_quote_id: Optional[str] = None
    idempotency_key: Optional[str] = Field(default=None, max_length=128)
    amount_cents: Optional[int] = Field(default=None, gt=0, le=2_000_000_000)
    option_id: Optional[str] = None
    expected_catalog_version: Optional[str] = None
    expected_quote_fingerprint: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    channel: Optional[str] = "auto"


def _pricing_quote_flags() -> tuple[bool, bool]:
    from config.pricing_ssot_flags import PricingFlagsUnavailable, pricing_flags_snapshot

    try:
        flags = pricing_flags_snapshot()
    except PricingFlagsUnavailable as exc:
        raise HTTPException(
            503,
            detail={
                "code": "PRICING_POLICY_UNAVAILABLE",
                "message": "定价策略暂不可用 · 已暂停创建订单，请稍后重试",
            },
            headers={"Retry-After": "5"},
        ) from exc
    return (
        bool(flags.get("PRICING_DUAL_SSOT_ENABLED", False)),
        bool(flags.get("PRICING_QUOTE_REQUIRED", False)),
    )


def _json_object(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _published_procurement_view(agent_user_id: int) -> tuple[dict, list[dict]]:
    """Return provider-safe fixed options after the canonical quote preview."""
    from decimal import Decimal
    from services import price_quote, pricing_catalog

    catalog = pricing_catalog.get_published_catalog("procurement", "PLATFORM_BASE")
    if not catalog or not catalog.get("items"):
        raise HTTPException(
            503,
            detail={"code": "PROCUREMENT_CATALOG_UNAVAILABLE", "message": "进货目录尚未发布"},
        )
    output: list[dict] = []
    with get_db() as conn:
        cur = conn.cursor()
        for entry in catalog["items"]:
            source = _json_object(entry.get("source_ref_jsonb"))
            option = _json_object(source.get("option"))
            option_id = source.get("option_id") or option.get("option_id")
            if source.get("kind") != "agent_purchase_option" or not option_id:
                raise HTTPException(
                    503,
                    detail={
                        "code": "PROCUREMENT_CATALOG_SOURCE_INVALID",
                        "message": f"已发布进货目录 {entry.get('product_code')} 缺少来源快照",
                    },
                )
            preview = price_quote.build_procurement_quote_preview(
                cur,
                dealer_id=int(agent_user_id),
                entry=dict(entry),
                version=dict(catalog["version"]),
                quantity=1,
            )
            snapshot = dict(preview["order_pricing_snapshot"])
            amount = int(preview["final_price_cents"])
            base = int(preview["points_granted"])
            bonus = int(preview["bonus_points"])
            amount_yuan = Decimal(amount) / Decimal(100)
            label = f"¥{amount_yuan:g} 进货 · 到账 {base:,} 算力"
            if bonus:
                label += f" + 奖励 {bonus:,}"
            output.append({
                "option_id": str(option_id),
                "product_code": str(entry["product_code"]),
                "amount_cents": amount,
                "base_points": base,
                "bonus_points": bonus,
                "total_points": base + bonus,
                "label": label,
                "is_first_month_bonus": bool(snapshot.get("reward_eligible", False)),
                "reward_description": price_quote.public_procurement_reward_description(snapshot),
                "quote_fingerprint": str(snapshot["quote_fingerprint"]),
                "sort_order": int(option.get("sort_order", 0)),
            })
    return dict(catalog["version"]), output


def _calc_prepay_points(amount_cents: int, agent_user_id=None) -> int:
    """代理预付积分按实时出厂价计算: per-agent override > 全局 pricing_config。"""
    return calc_prepay_points(amount_cents, agent_user_id)


def _compute_purchase_options(agent_user_id=None, *, include_version: bool = False):
    """服务商卡片使用同一快照计算器；停用档不会输出。"""
    from config.pricing_config import get_agent_purchase_catalog_version, merge_pricing_config
    from services.agent_inventory_pricing import build_purchase_snapshot, normalize_catalog_options, public_option
    from services.config_epoch import read_config_epoch_strict
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_xact_lock_shared(920713, 1)")
        epoch_before = read_config_epoch_strict(cur)
        cur.execute("SELECT value FROM system_settings WHERE key='pricing_config'")
        row = cur.fetchone()
        raw = row.get("value") if isinstance(row, dict) else (row[0] if row else {})
        if isinstance(raw, str):
            raw = json.loads(raw or "{}")
        config = merge_pricing_config(raw if isinstance(raw, dict) else {})
        version = get_agent_purchase_catalog_version(config)
        output = []
        for option in normalize_catalog_options(config.get("agent_purchase_options", [])):
            if not option["is_enabled"]:
                continue
            snapshot = build_purchase_snapshot(
                cur, config=config, catalog_version=version, agent_user_id=agent_user_id,
                amount_cents=option["amount_cents"], option=option,
            )
            output.append(public_option(snapshot, option))
        if read_config_epoch_strict(cur) != epoch_before:
            raise RuntimeError("配置纪元回读不一致")
        return (version, output) if include_version else output


def _channel_tier_purchase_context(agent_user_id=None):
    """Return (enabled, rolling_before_yuan) for purchase UI/order creation.

    Any read failure falls back to legacy behavior so the new channel-tier
    feature cannot break existing purchase flows while the flag is off.
    """
    if not agent_user_id:
        return False, 0.0
    try:
        from services.channel_tier import (
            compute_rolling_12m_yuan,
            is_channel_tier_enabled,
        )
        with get_db() as conn:
            cur = conn.cursor()
            if not is_channel_tier_enabled(cur):
                return False, 0.0
            rolling_before_yuan = compute_rolling_12m_yuan(cur, int(agent_user_id))
            return True, float(rolling_before_yuan)
    except Exception as exc:
        logger.warning("[agent_purchase] 渠道等级上下文读取失败 · 回落旧赠送 agent=%s: %s", agent_user_id, exc)
        return False, 0.0


@router.get(
    "/inventory/purchase-options",
    response_model=AgentPurchaseOptionsResponse,
    dependencies=[Depends(require_signed_agreement)],
)
async def agent_inventory_purchase_options(request: Request):
    a = _require_agent(request)
    _reject_platform_direct_procurement(a)
    dual_enabled, _ = _pricing_quote_flags()
    if dual_enabled:
        # [WO_241 甲] 这一处原来**没有** try —— preview 与下单都接了 `QuoteError`,
        #   只有它裸着,于是同一个异常在两个入口是 422、在这里是 **500**。
        #   「同一种异常两处处置不同」本身就是缺陷,而 500 还会把栈里的结算文案
        #   带进日志与告警面。
        from services import price_quote
        try:
            version, published = _published_procurement_view(int(a["user_id"]))
        except price_quote.QuoteError as exc:
            raise _public_procurement_quote_error(exc, "options") from exc
        return AgentPurchaseOptionsResponse(
            catalog_version=str(version["version_code"]),
            options=[AgentPurchaseOption(**{k: v for k, v in row.items() if k != "product_code"}) for row in published],
        )
    version, options = _compute_purchase_options(a["user_id"], include_version=True)
    return AgentPurchaseOptionsResponse(
        catalog_version=version,
        options=[AgentPurchaseOption(**o) for o in options]
    )


@router.post(
    "/inventory/purchase-preview",
    response_model=AgentPurchasePreviewWiringResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_signed_agreement)],
)
async def agent_inventory_purchase_preview(req: AgentPurchasePreviewWiringRequest, request: Request):
    """自由金额/固定档下单前用同一事务 adapter 生成内容寻址报价。"""
    a = _require_agent(request)
    _reject_platform_direct_procurement(a)          # [WO_241 甲]
    dual_enabled, _ = _pricing_quote_flags()
    from config.dealer_inventory_resale_flags import enabled as dealer_resale_enabled
    resale_enabled = dealer_resale_enabled()
    if resale_enabled and not dual_enabled:
        raise HTTPException(
            503,
            detail={
                "code": "PROCUREMENT_PRICING_PREREQUISITE_MISSING",
                "message": "当前进货价格配置尚未就绪，请稍后重试",
            },
        )
    if dual_enabled:
        from services import price_quote
        try:
            if req.option_id:
                version, published = _published_procurement_view(int(a["user_id"]))
                selected = next(
                    (
                        row for row in published
                        if row["option_id"] == req.option_id
                        or row["product_code"] == req.option_id
                    ),
                    None,
                )
                if selected is None:
                    raise HTTPException(
                        409,
                        detail={
                            "code": "PURCHASE_OPTION_UNAVAILABLE",
                            "current_catalog_version": version["version_code"],
                        },
                    )
                if (
                    req.amount_cents is not None
                    and int(req.amount_cents) != int(selected["amount_cents"])
                ):
                    raise HTTPException(
                        409,
                        detail={
                            "code": "PURCHASE_OPTION_AMOUNT_CHANGED",
                            "current_catalog_version": version["version_code"],
                        },
                    )
                quote = price_quote.issue_procurement_quote(
                    dealer_id=int(a["user_id"]),
                    product_code=str(selected["product_code"]),
                    idempotency_key=req.idempotency_key,
                )
            else:
                if req.amount_cents is None:
                    raise HTTPException(
                        422,
                        detail={"code": "AMOUNT_REQUIRED", "message": "请输入进货金额"},
                    )
                quote = price_quote.issue_procurement_custom_amount_quote(
                    dealer_id=int(a["user_id"]),
                    requested_amount_cents=int(req.amount_cents),
                    idempotency_key=req.idempotency_key,
                )
            snapshot = price_quote.quote_order_pricing_snapshot(quote, required=True)
        except price_quote.QuoteError as exc:
            raise _public_procurement_quote_error(exc, "preview") from exc
        return AgentPurchasePreviewWiringResponse(
            catalog_version=str(quote["catalog_version"]),
            option_id=str(snapshot["option_id"]),
            amount_cents=int(quote["final_price_cents"]),
            base_points=int(quote["points_granted"]),
            bonus_points=int(quote["bonus_points"]),
            total_points=int(quote["points_granted"]) + int(quote["bonus_points"]),
            reward_description=price_quote.public_procurement_reward_description(snapshot),
            quote_fingerprint=str(snapshot["quote_fingerprint"]),
            tier_at_order=str(snapshot.get("benefit_tier_at_order") or snapshot.get("tier_at_order") or "none"),
            tier_bonus_rate_bps=int(snapshot.get("benefit_tier_bonus_rate_bps") or snapshot.get("tier_bonus_rate_bps") or 0),
            crosses_tier_threshold=bool(snapshot.get("benefit_crosses_tier_threshold", snapshot.get("crosses_tier_threshold"))),
            projected_rolling_12m_yuan=str(snapshot.get("projected_rolling_12m_yuan_snapshot") or "0"),
            price_quote_id=str(quote["quote_id"]),
            product_code=str(quote["product_code"]),
            expires_at=quote["expires_at"],
        )
    if req.amount_cents is None:
        raise HTTPException(422, detail={"code": "AMOUNT_REQUIRED", "message": "请输入进货金额"})
    from config.pricing_config import get_agent_purchase_catalog_version, merge_pricing_config
    from services.agent_inventory_pricing import build_purchase_snapshot, normalize_catalog_options
    from services.config_epoch import read_config_epoch_strict

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_xact_lock_shared(920713, 1)")
        epoch_before = read_config_epoch_strict(cur)
        cur.execute("SELECT value FROM system_settings WHERE key='pricing_config' FOR SHARE")
        row = cur.fetchone()
        raw = row.get("value") if isinstance(row, dict) else (row[0] if row else {})
        if isinstance(raw, str):
            raw = json.loads(raw or "{}")
        config = merge_pricing_config(raw if isinstance(raw, dict) else {})
        version = get_agent_purchase_catalog_version(config)
        enabled = [
            option for option in normalize_catalog_options(config.get("agent_purchase_options", []))
            if option["is_enabled"]
        ]
        option = None
        if req.option_id:
            option = next((item for item in enabled if item["option_id"] == req.option_id), None)
            if not option:
                raise HTTPException(
                    409,
                    detail={"code": "PURCHASE_OPTION_UNAVAILABLE", "current_catalog_version": version},
                )
            if int(option["amount_cents"]) != int(req.amount_cents):
                raise HTTPException(
                    409,
                    detail={"code": "PURCHASE_OPTION_AMOUNT_CHANGED", "current_catalog_version": version},
                )
        else:
            option = next(
                (item for item in enabled if int(item["amount_cents"]) == int(req.amount_cents)),
                None,
            )
        snapshot = build_purchase_snapshot(
            cur,
            config=config,
            catalog_version=version,
            agent_user_id=int(a["user_id"]),
            amount_cents=int(req.amount_cents),
            option=option,
        )
        if read_config_epoch_strict(cur) != epoch_before:
            raise HTTPException(503, detail={"code": "PRICING_FRESHNESS_UNCONFIRMED"})
        return AgentPurchasePreviewResponse(
            catalog_version=version,
            option_id=snapshot["option_id"],
            amount_cents=snapshot["amount_cents"],
            base_points=snapshot["base_points"],
            bonus_points=snapshot["bonus_points"],
            total_points=snapshot["total_points"],
            reward_description=(
                "本档含赠送库存"
                if int(snapshot.get("bonus_points") or 0) > 0
                else "本档暂无固定奖励"
            ),
            quote_fingerprint=snapshot["quote_fingerprint"],
        )


class RedeemFromCommissionRequest(BaseModel):
    redeem_yuan: float  # 想用多少 settled 利润换算力(元)


@router.post("/inventory/redeem-from-commission")
async def agent_redeem_from_commission(req: RedeemFromCommissionRequest, request: Request):
    """[V3.5 v7 批1C · 老板 A 2026-06-08] 服务商利润换算力。

    settled 利润(agent_revenue_ledger · T+3 后)按当前出厂折扣【等价换】paid_inventory 算力,
    等于"用税前利润主动进货 · 不过微信支付 · 不扣 fees/税"。即时完成 · 无审核。
    与提现共用 settled ledger · available SSOT(get_agent_balance)防双花。
    """
    a = _require_agent(request)
    redeem_cents = int(round((req.redeem_yuan or 0) * 100))
    if redeem_cents <= 0:
        raise HTTPException(400, "换算力金额必须 > 0")
    from services.agent_commission_redeem import redeem_commission_to_inventory
    try:
        with get_db() as conn:
            cur = conn.cursor()
            result = redeem_commission_to_inventory(cur, a["user_id"], redeem_cents)
            conn.commit()
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"success": True, **result}


def _detect_channel(user_agent: str) -> str:
    """
    UA 检测统一走 services.payment_routing:
    微信内 → wechat_jsapi · 手机外部 → xunhupay · PC → wechat_native
    """
    return detect_payment_channel(user_agent)


def _create_quoted_inventory_order(
    *, agent_user_id: int, quote_id: str, order_id: str, idempotency_key: Optional[str]
) -> tuple[str, dict, bool]:
    """Atomically pin quote→order and consume it without reading live pricing config."""
    from services import price_quote
    from services.agent_inventory_pricing import (
        compute_quote_fingerprint,
        inventory_snapshot_activation_state,
    )

    with get_db() as conn:
        cur = conn.cursor()
        try:
            def _existing_idempotent_order():
                if not idempotency_key:
                    return None
                cur.execute(
                    """SELECT id, price_quote_id, pricing_snapshot_jsonb
                       FROM recharge_orders
                       WHERE user_id=%s AND idempotency_key=%s
                       FOR UPDATE""",
                    (int(agent_user_id), idempotency_key),
                )
                row = cur.fetchone()
                if not row:
                    return None
                row = dict(row)
                if row.get("price_quote_id") != quote_id:
                    raise price_quote.QuoteError("幂等键已用于另一笔进货报价")
                existing_snapshot = _json_object(row.get("pricing_snapshot_jsonb"))
                if not existing_snapshot or existing_snapshot.get("price_quote_id") != quote_id:
                    raise price_quote.QuoteError("幂等订单缺少匹配的不可变报价快照")
                if existing_snapshot.get("resale_mode"):
                    from services import dealer_inventory_resale

                    if not dealer_inventory_resale.has_resale_order(cur, str(row["id"])):
                        raise price_quote.QuoteError("逐级转售幂等订单缺少卖方库存预占")
                return str(row["id"]), existing_snapshot, True

            existing = _existing_idempotent_order()
            if existing:
                return existing
            cur.execute("SELECT pg_advisory_xact_lock(920714, %s)", (int(agent_user_id),))
            _lock_agent_identity_for_purchase(cur, int(agent_user_id))
            existing = _existing_idempotent_order()
            if existing:
                return existing
            activation = inventory_snapshot_activation_state(cur)
            if not activation["ready"]:
                raise HTTPException(
                    503,
                    detail={
                        "code": "INVENTORY_PURCHASE_ACTIVATION_PENDING",
                        "message": "进货系统正在升级，暂停创建新订单，请稍后重试",
                    },
                    headers={"Retry-After": "30"},
                )
            try:
                quote = price_quote.lock_and_validate(
                    cur,
                    quote_id,
                    buyer_user_id=int(agent_user_id),
                    quote_type="procurement",
                )
            except price_quote.QuoteError:
                # A concurrent request using the same quote waits on its row lock.
                # Once the winner commits, map the loser to the existing order rather
                # than leaking a 500/duplicate payment attempt.
                existing = _existing_idempotent_order()
                if existing:
                    return existing
                raise
            snapshot = price_quote.quote_order_pricing_snapshot(quote, required=True)
            required = {
                "catalog_version", "option_id", "amount_cents", "base_points",
                "discount_source", "discount_numer", "discount_denom",
                "channel_tier_enabled", "tier_at_order", "tier_source",
                "tier_bonus_rate_bps", "tier_bonus_points",
                "founder_eligibility_source", "founder_seat_policy",
                "founder_cap_snapshot", "founder_bonus_rate_bps_snapshot",
                "founder_min_first_order_yuan_snapshot", "option_source", "reward_eligible",
                "founder_bonus_points_if_eligible", "bonus_rate_bps", "bonus_points", "total_points",
                "bonus_validity_months", "rolling_before_yuan_snapshot",
                "projected_rolling_12m_yuan_snapshot", "tier_override_until_snapshot",
                "quote_schema_version", "calculator_version", "quote_fingerprint",
                "price_quote_id", "quote_type", "catalog_version_id", "catalog_entry_id",
                "product_code", "buyer_paid_cents", "buyer_user_id",
                "cash_anchor_semantic_version", "cash_anchor_fingerprint",
                "payable_amount_cents", "paid_inventory_points", "cash_anchor",
                "purchase_discount_version", "purchase_discount_snapshot",
            }
            missing = sorted(required.difference(snapshot))
            if missing:
                raise price_quote.QuoteError(f"进货报价订单快照缺字段: {','.join(missing)}")
            if str(snapshot["price_quote_id"]) != quote_id:
                raise price_quote.QuoteError("进货报价快照 quote_id 不一致")
            if int(snapshot["buyer_user_id"]) != int(agent_user_id):
                raise price_quote.QuoteError("进货报价快照买方不一致")
            if str(snapshot["catalog_version"]) != str(quote["catalog_version"]):
                raise price_quote.QuoteError("进货报价快照目录版本不一致")
            if int(snapshot["amount_cents"]) != int(quote["final_price_cents"]):
                raise price_quote.QuoteError("进货报价快照金额不一致")
            if int(snapshot["base_points"]) != int(quote["points_granted"]):
                raise price_quote.QuoteError("进货报价快照基础算力不一致")
            if int(snapshot["bonus_points"]) != int(quote["bonus_points"]):
                raise price_quote.QuoteError("进货报价快照奖励不一致")
            if int(snapshot["payable_amount_cents"]) != int(quote["final_price_cents"]):
                raise price_quote.QuoteError("进货报价现金锚定金额不一致")
            if int(snapshot["paid_inventory_points"]) != int(quote["points_granted"]):
                raise price_quote.QuoteError("进货报价现金锚定算力不一致")
            from config.dealer_inventory_resale_flags import enabled as dealer_resale_enabled
            resale_now = dealer_resale_enabled(cur)
            if resale_now and snapshot.get("resale_mode") is not True:
                raise price_quote.QuoteError(
                    "逐级库存转售已开启，旧进货报价不能创建新订单 · 请重新报价"
                )
            if not resale_now and snapshot.get("resale_mode") is True:
                raise price_quote.QuoteError(
                    "逐级库存转售已关闭，未下单的旧转售报价已作废 · 请重新报价"
                )
            if str(snapshot["quote_fingerprint"]) != compute_quote_fingerprint(
                snapshot, agent_user_id=int(agent_user_id)
            ):
                raise price_quote.QuoteError("进货报价指纹不一致")
            if snapshot.get("channel_beneficiary_user_id") is not None and snapshot.get(
                "upstream_cost_basis_cents"
            ) is None:
                raise price_quote.QuoteError("进货报价内部结算快照不完整")
            if snapshot.get("resale_mode"):
                resale_required = {
                    "resale_snapshot_version", "seller_user_id", "buyer_user_id", "points",
                    "seller_lot_allocations", "seller_cost_basis_cents", "sale_amount_cents",
                    "margin_cents", "downstream_markup_bps", "pricing_version",
                    "seller_policy_version", "payment_collector",
                    "refund_responsible_user_id", "source_kind", "refund_window_hours",
                }
                resale_missing = sorted(resale_required.difference(snapshot))
                if resale_missing:
                    raise price_quote.QuoteError(
                        f"逐级转售报价快照缺字段: {','.join(resale_missing)}"
                    )
                if int(snapshot["points"]) != int(snapshot["base_points"]):
                    raise price_quote.QuoteError("逐级转售报价充值库存与固定权益不一致")
                if int(snapshot["sale_amount_cents"]) != int(snapshot["amount_cents"]):
                    raise price_quote.QuoteError("逐级转售报价售价与支付金额不一致")
                if snapshot.get("channel_beneficiary_user_id") is not None:
                    raise price_quote.QuoteError("逐级转售报价禁止同时产生旧渠道佣金")
                if int(snapshot.get("resale_snapshot_version") or 0) == 2:
                    jit_required = {
                        "fulfillment_plan", "jit_fulfillment_mode",
                        "standard_reference_cents", "edge_multiplier_bps",
                    }
                    jit_missing = sorted(jit_required.difference(snapshot))
                    if jit_missing:
                        raise price_quote.QuoteError(
                            f"JIT 逐级转售报价快照缺字段:{','.join(jit_missing)}"
                        )
                    plan = snapshot.get("fulfillment_plan")
                    if not isinstance(plan, dict) or int(plan.get("plan_version") or 0) != 2:
                        raise price_quote.QuoteError("JIT 逐级转售履约计划非法")
                    if snapshot.get("jit_fulfillment_mode") != "RECURSIVE_FIFO":
                        raise price_quote.QuoteError("JIT 逐级转售履约模式非法")

            cur.execute(
                """
                INSERT INTO recharge_orders
                    (id, user_id, amount_cents, base_points, bonus_points,
                     payment_method, payment_status, order_type,
                     pricing_snapshot_jsonb, pricing_catalog_version,
                     price_quote_id, idempotency_key, agent_inventory_writer_generation)
                VALUES (%s, %s, %s, %s, %s, 'wechat', 'pending',
                        'agent_inventory_purchase', %s::jsonb, %s, %s, %s, 2)
                """,
                (
                    order_id,
                    int(agent_user_id),
                    int(quote["final_price_cents"]),
                    int(quote["points_granted"]),
                    int(quote["bonus_points"]),
                    json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
                    str(quote["catalog_version"]),
                    quote_id,
                    idempotency_key,
                ),
            )
            if snapshot.get("resale_mode"):
                from services import dealer_inventory_resale

                try:
                    dealer_inventory_resale.reserve_order(
                        cur, order_id=str(order_id), snapshot=snapshot
                    )
                except dealer_inventory_resale.ResaleError as exc:
                    raise price_quote.QuoteError(str(exc)) from exc
            if not price_quote.consume_quote(cur, quote_id, order_id):
                raise price_quote.QuoteError("进货报价已被消费 · 请重新报价")
            conn.commit()
            return order_id, snapshot, False
        except Exception:
            conn.rollback()
            raise


async def _start_agent_inventory_payment(
    *, agent_user_id: int, order_id: str, chosen: dict, chosen_channel: str
) -> AgentPurchaseCreateResponse:
    """Preserve the existing JSAPI/Native/Xunhu payment routing for both writers."""
    displayed_bonus_points = int(chosen["bonus_points"])
    code_url = None
    payment_url_mobile = None
    payment_url_qrcode = None
    needs_openid = False
    actual_channel = chosen_channel
    from decimal import Decimal
    amount_yuan = Decimal(int(chosen["amount_cents"])) / Decimal(100)
    desc = f"OmniRank 代理预付进货 ¥{amount_yuan:g}"

    async def _xunhupay_fallback(reason: str = "") -> None:
        nonlocal payment_url_qrcode, payment_url_mobile, actual_channel
        from services.xunhupay import create_xunhupay_order
        xr = await create_xunhupay_order(
            order_id=order_id,
            amount_yuan=amount_yuan,
            title=desc,
            attach=str(agent_user_id),
        )
        payment_url_qrcode = xr.get("url_qrcode")
        payment_url_mobile = xr.get("url")
        actual_channel = "xunhupay"
        if reason:
            logger.info("[agent_purchase] 落 xunhupay · %s · order=%s", reason, order_id)

    # The provider route is refund evidence (08_billing §4.1). Persist the intended
    # route before any provider can create a payable intent, so a process crash cannot
    # leave an externally payable order without a durable original-route record.
    # 🔴 同一口径、同一谓词:复用 wallet 侧的 `_persist_actual_payment_channel`,
    #    不在本文件另抄一份(两份会各自漂移,而只有一份会有人验)。
    if chosen_channel not in ("wechat_jsapi", "wechat_native", "xunhupay"):
        # 先于持久化判掉未知渠道 —— 否则会撞 chk_recharge_actual_payment_channel,
        # 把一个本该 400 的输入变成 500。
        raise HTTPException(400, f"未知支付渠道: {chosen_channel}")
    from api.wallet_api import _persist_actual_payment_channel
    try:
        _persist_actual_payment_channel(str(order_id), actual_channel)
    except Exception as exc:
        from services.dealer_inventory_resale import cancel_any_reservation
        try:
            cancel_any_reservation(str(order_id), "payment_route_persist_failed")
        except Exception as release_exc:
            logger.critical(
                "[agent_purchase] 路由持久化失败后的库存预占释放也失败 · order=%s: %s",
                order_id, release_exc,
            )
        logger.error("[agent_purchase] 支付路由持久化失败 · order=%s: %s", order_id, exc)
        raise HTTPException(500, "支付下单失败(路由未能留痕)· 请稍后重试") from exc

    try:
        if chosen_channel == "wechat_jsapi":
            needs_openid = True
        elif chosen_channel == "wechat_native":
            from services.wechat_pay import create_native_order
            result = await create_native_order(
                out_trade_no=order_id,
                total_yuan=amount_yuan,
                description=desc,
            )
            code_url = result.get("code_url")
            if not code_url:
                raise RuntimeError("Native 下单未返 code_url")
        elif chosen_channel == "xunhupay":
            await _xunhupay_fallback()
        else:
            raise HTTPException(400, f"未知支付渠道: {chosen_channel}")
    except HTTPException:
        raise
    except Exception as exc:
        if chosen_channel == "wechat_jsapi":
            logger.error("[agent_purchase] JSAPI 下单入口异常 · 不降级虎皮椒: %s", exc)
            raise HTTPException(500, "微信内支付下单失败 · 请稍后重试") from exc
        logger.error("[agent_purchase] %s 下单失败 · 降级 xunhupay: %s", chosen_channel, exc)
        try:
            await _xunhupay_fallback(reason=f"直连失败({chosen_channel}): {exc}")
        except Exception as fallback_exc:
            logger.exception("[agent_purchase] xunhupay 兜底也失败 · order=%s: %s", order_id, fallback_exc)
            from services.dealer_inventory_resale import cancel_any_reservation
            try:
                cancel_any_reservation(str(order_id), "payment_intent_failed")
            except Exception as release_exc:
                logger.critical(
                    "[agent_purchase] 支付失败后的库存预占释放失败 · order=%s: %s",
                    order_id, release_exc,
                )
            raise HTTPException(500, "支付下单失败(直连 + 兜底都失败)· 请稍后重试") from fallback_exc

    # 显式 xunhupay 或直连失败降级 —— 实际路由与建单时的意向不同,补写一次。
    # 🔴 wallet 口径的第二次写:少了它,降级过的单在退款时会按错误的通道去查。
    if actual_channel == "xunhupay" and chosen_channel != "xunhupay":
        try:
            _persist_actual_payment_channel(str(order_id), "xunhupay")
        except Exception as exc:
            from services.dealer_inventory_resale import cancel_any_reservation
            try:
                cancel_any_reservation(str(order_id), "payment_route_fallback_persist_failed")
            except Exception as release_exc:
                logger.critical(
                    "[agent_purchase] 降级路由持久化失败后的库存预占释放也失败 · order=%s: %s",
                    order_id, release_exc,
                )
            logger.error("[agent_purchase] 降级路由持久化失败 · order=%s: %s", order_id, exc)
            raise HTTPException(500, "支付下单失败(降级路由未能留痕)· 请稍后重试") from exc

    # #169 · 把这次拿到的支付出口存下来,好让用户跳出去付款后回来还找得到同一张单。
    # 🔴 不阻断:URL 是恢复用的便利,不是资金证据 —— 存不下也不该让一张
    #    已经可以支付的订单下单失败(与上面路由持久化的取舍相反,理由见函数 docstring)。
    from api.wallet_api import _persist_payment_intent_urls
    _persist_payment_intent_urls(
        str(order_id),
        payment_url_mobile=payment_url_mobile,
        payment_url_qrcode=payment_url_qrcode,
        code_url=code_url,
    )

    logger.info(
        "[agent_purchase] agent=%s order=%s amount=%s base=%s bonus=%s "
        "channel=%s code_url_len=%s h5=%s",
        agent_user_id,
        order_id,
        chosen["amount_cents"],
        chosen["base_points"],
        displayed_bonus_points,
        actual_channel,
        len(code_url) if code_url else 0,
        bool(payment_url_mobile),
    )
    return AgentPurchaseCreateResponse(
        order_id=order_id,
        amount_cents=int(chosen["amount_cents"]),
        base_points=int(chosen["base_points"]),
        bonus_points=displayed_bonus_points,
        quote_fingerprint=str(chosen["quote_fingerprint"]),
        actual_channel=actual_channel,
        code_url=code_url,
        payment_url_qrcode=payment_url_qrcode,
        payment_url_mobile=payment_url_mobile,
        needs_openid=needs_openid,
    )


@router.post(
    "/inventory/purchase",
    response_model=AgentPurchaseCreateResponse,
    dependencies=[Depends(require_signed_agreement)],
)
async def agent_inventory_purchase(req: AgentPurchaseWiringRequest, request: Request):
    """
    创建代理预付进货订单 · 写 recharge_orders(order_type='agent_inventory_purchase')
    + 调 WeChat Pay 服务拿 code_url / h5_url(对齐 /wallet/recharge 闭环)
    支付回调走 complete_recharge **早分支** · 不进 Orchestrator
    """
    a = _require_agent(request)
    _reject_platform_direct_procurement(a)          # [WO_241 甲]
    dual_enabled, quote_is_required = _pricing_quote_flags()
    from config.dealer_inventory_resale_flags import enabled as dealer_resale_enabled
    resale_enabled = dealer_resale_enabled()
    quote_id = getattr(req, "price_quote_id", None)
    if resale_enabled and not dual_enabled:
        raise HTTPException(
            503,
            detail={
                "code": "PROCUREMENT_PRICING_PREREQUISITE_MISSING",
                "message": "当前进货价格配置尚未就绪，请稍后重试",
            },
        )
    if resale_enabled and not quote_id:
        raise HTTPException(
            422,
            detail={"code": "PROCUREMENT_QUOTE_REQUIRED", "message": "当前进货必须先获取有效报价"},
        )
    use_persisted_quote = bool((dual_enabled or resale_enabled) and quote_id)
    if quote_is_required and not dual_enabled:
        raise HTTPException(
            503,
            detail={"code": "PRICING_FLAG_STATE_INVALID", "message": "报价强制闸已开启但双价目表未开启"},
        )
    if quote_is_required and not quote_id:
        raise HTTPException(422, detail={"code": "QUOTE_REQUIRED", "message": "请先获取进货报价"})
    if use_persisted_quote:
        mixed = [
            name for name in (
                "amount_cents", "option_id", "expected_catalog_version", "expected_quote_fingerprint"
            )
            if getattr(req, name, None) is not None
        ]
        if mixed:
            raise HTTPException(
                422,
                detail={
                    "code": "QUOTED_REQUEST_MIXED",
                    "message": f"报价下单只允许 price_quote_id/channel/idempotency_key，禁止混传: {','.join(mixed)}",
                },
            )
        if not getattr(req, "idempotency_key", None):
            raise HTTPException(
                422,
                detail={"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "报价下单必须提交幂等键"},
            )
    elif req.amount_cents is None:
        raise HTTPException(422, detail={"code": "AMOUNT_REQUIRED", "message": "请输入进货金额"})

    # ===== 走统一支付路由 · 先校验再写 pending 订单 =====
    requested = (req.channel or "auto").lower()
    ua = request.headers.get("User-Agent", "")
    from services.xunhupay import is_force_xunhupay
    try:
        chosen_channel = resolve_payment_channel(
            requested,
            ua,
            force_xunhupay=is_force_xunhupay(),
        )
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={"code": "PAYMENT_CHANNEL_UNAVAILABLE", "message": str(e)},
        )

    order_id = f"AIP{uuid.uuid4().hex[:16].upper()}"
    if use_persisted_quote:
        from services import price_quote
        try:
            order_id, chosen, reused = _create_quoted_inventory_order(
                agent_user_id=int(a["user_id"]),
                quote_id=str(quote_id),
                order_id=order_id,
                idempotency_key=str(req.idempotency_key),
            )
        except price_quote.QuoteError as exc:
            raise _public_procurement_quote_error(exc, "create") from exc
        if reused:
            logger.info(
                "[agent_purchase] 幂等重试复用订单 agent=%s quote=%s order=%s",
                a["user_id"], quote_id, order_id,
            )
            # Never create a second external payment intent for the same persisted
            # order. The client can continue the existing JSAPI flow or poll this order.
            # #169 · 但要把第一次下单存下的出口**回填**回去:原来这里返三个 None,
            #        用户同 key 重试就拿到一张没有任何支付出口的单 —— 点了没反应。
            #        这是从迁移 058 的新列**读**,不向虎皮椒/微信发任何新请求。
            #        建于 058 之前的老单读到 None,表现与改动前一致。
            from api.wallet_api import _load_payment_intent_urls
            _urls = _load_payment_intent_urls(str(order_id))
            return AgentPurchaseCreateResponse(
                order_id=order_id,
                amount_cents=int(chosen["amount_cents"]),
                base_points=int(chosen["base_points"]),
                bonus_points=int(chosen["bonus_points"]),
                quote_fingerprint=str(chosen["quote_fingerprint"]),
                actual_channel=chosen_channel,
                code_url=_urls["code_url"],
                payment_url_qrcode=_urls["payment_url_qrcode"],
                payment_url_mobile=_urls["payment_url_mobile"],
                needs_openid=chosen_channel == "wechat_jsapi",
            )
        return await _start_agent_inventory_payment(
            agent_user_id=int(a["user_id"]),
            order_id=order_id,
            chosen=chosen,
            chosen_channel=chosen_channel,
        )

    with get_db() as conn:
        cur = conn.cursor()
        try:
            from config.pricing_config import get_agent_purchase_catalog_version, merge_pricing_config
            from services.agent_inventory_pricing import (
                build_purchase_snapshot,
                inventory_snapshot_activation_state,
                normalize_catalog_options,
            )
            from services.config_epoch import read_config_epoch_strict
            cur.execute("SELECT pg_advisory_xact_lock_shared(920713, 1)")
            cur.execute("SELECT pg_advisory_xact_lock(920714, %s)", (int(a["user_id"]),))
            _lock_agent_identity_for_purchase(cur, int(a["user_id"]))
            epoch_before = read_config_epoch_strict(cur)
            cur.execute("SELECT value FROM system_settings WHERE key='pricing_config' FOR SHARE")
            row = cur.fetchone()
            raw = row.get("value") if isinstance(row, dict) else (row[0] if row else {})
            if isinstance(raw, str):
                raw = json.loads(raw or "{}")
            config = merge_pricing_config(raw if isinstance(raw, dict) else {})
            version = get_agent_purchase_catalog_version(config)
            if req.expected_catalog_version and req.expected_catalog_version != version:
                raise HTTPException(409, detail={"code": "CATALOG_VERSION_CONFLICT", "current_catalog_version": version})
            enabled = [o for o in normalize_catalog_options(config.get("agent_purchase_options", [])) if o["is_enabled"]]
            option = None
            if req.option_id:
                option = next((o for o in enabled if o["option_id"] == req.option_id), None)
                if not option:
                    raise HTTPException(409, detail={"code": "PURCHASE_OPTION_UNAVAILABLE", "current_catalog_version": version})
                if int(option["amount_cents"]) != int(req.amount_cents):
                    raise HTTPException(409, detail={"code": "PURCHASE_OPTION_AMOUNT_CHANGED", "current_catalog_version": version})
            else:
                option = next((o for o in enabled if int(o["amount_cents"]) == int(req.amount_cents)), None)
            snapshot = build_purchase_snapshot(
                cur, config=config, catalog_version=version, agent_user_id=int(a["user_id"]),
                amount_cents=int(req.amount_cents), option=option,
            )
            if (
                req.expected_quote_fingerprint
                and req.expected_quote_fingerprint != snapshot["quote_fingerprint"]
            ):
                raise HTTPException(
                    409,
                    detail={
                        "code": "QUOTE_FINGERPRINT_CONFLICT",
                        "current_catalog_version": version,
                        "current_quote_fingerprint": snapshot["quote_fingerprint"],
                    },
                )
            activation = inventory_snapshot_activation_state(cur)
            if not activation["ready"]:
                raise HTTPException(
                    503,
                    detail={
                        "code": "INVENTORY_PURCHASE_ACTIVATION_PENDING",
                        "message": "进货系统正在升级，暂停创建新订单，请稍后重试",
                    },
                    headers={"Retry-After": "30"},
                )
            cur.execute("""
                INSERT INTO recharge_orders
                    (id, user_id, amount_cents, base_points, bonus_points,
                     payment_method, payment_status, order_type,
                     pricing_snapshot_jsonb, pricing_catalog_version,
                     agent_inventory_writer_generation)
                VALUES (%s, %s, %s, %s, %s, 'wechat', 'pending', 'agent_inventory_purchase', %s, %s, 2)
            """, (
                order_id, a["user_id"], snapshot["amount_cents"], snapshot["base_points"],
                snapshot["bonus_points"], json.dumps(snapshot, ensure_ascii=False), version,
            ))
            if read_config_epoch_strict(cur) != epoch_before:
                raise HTTPException(503, detail={"code": "PRICING_FRESHNESS_UNCONFIRMED"})
            conn.commit()
            chosen = snapshot
        except Exception:
            conn.rollback()
            raise

    return await _start_agent_inventory_payment(
        agent_user_id=int(a["user_id"]),
        order_id=order_id,
        chosen=chosen,
        chosen_channel=chosen_channel,
    )


# ============================================================
# 5-6. 线下划拨 + revoke
# ============================================================

@router.get("/customers/lookup", response_model=AgentCustomerLookupResponse)
async def agent_lookup_customers(
    request: Request,
    q: str = Query(..., min_length=1, max_length=80),
    owned_only: bool = Query(False),
    limit: int = Query(8, ge=1, le=20),
):
    """线下划拨/撤回前的客户解析 · 只返回当前服务商已绑定客户。"""
    a = _require_agent(request)
    with get_db() as conn:
        cur = conn.cursor()
        items = _lookup_agent_customers(cur, a["user_id"], q, owned_only=owned_only, limit=limit)
    return AgentCustomerLookupResponse(
        items=[AgentCustomerLookupItem(**item) for item in items],
        total=len(items),
    )


@router.get("/customers/{target_user_id:int}/purchase-notice")
async def agent_downstream_purchase_notice(
    request: Request,
    target_user_id: int,
    amount_cents: int = Query(..., ge=1, le=2_000_000_000),
):
    """「复制进货提醒」的数据源(工单 §P0-1 次动作 / §3.2 未拍板前的默认出口)。

    🔴 计价读的是这对上下游**已绑定**的系数(R3),不回落默认、不按身份现算 ——
       §4.1 第 4 条计价锁钉的就是这条路径。
    🔴 无有向下级关系 → 与"账号不存在"同一份 404(R5),不透露对方是否存在。
    """
    a = _require_agent(request)
    with get_db() as conn:
        cur = conn.cursor()
        try:
            rel = _resolve_relationship(cur, int(a["user_id"]), int(target_user_id))
            if not rel.get("has_channel_relationship"):
                raise _RelationError("TARGET_NOT_FOUND", "未找到该账号")
            quote = _resolve_downstream_quote(cur, int(a["user_id"]), int(target_user_id), amount_cents)
        except _RelationError as exc:
            if exc.code == "TARGET_NOT_FOUND":
                raise HTTPException(
                    status_code=404,
                    detail={"code": "TARGET_NOT_FOUND", "message": "未找到该账号"},
                )
            raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)})
    yuan = quote["price_cents"] / 100
    return strip_private_relationship_fields({
        "success": True,
        "target_user_id": int(target_user_id),
        "price_cents": quote["price_cents"],
        # 人话文案:禁内部字段名/表名/等级编号(工单 §0.5)
        "message": (
            f"你好,请到「库存中心 → 进货」下单本批算力,"
            f"按我们已约定的进货价合计 ¥{yuan:,.2f}。下单后算力会直接进你的库存算力。"
        ),
    })


def _compute_offline_idem_ref(request: Request, agent_user_id: int, req, *, kind: str) -> str:
    """
    [GEO-R2-CAN-017] 线下划拨/撤回幂等键 · 防止重复/双击提交造成双侧账目翻倍。
    - 调用方 `Idempotency-Key` / `X-Idempotency-Key` 头优先 → 跨重试稳定去重(不受时间限制)。
    - 缺失时按 (kind + 代理 + 客户 + 三池 + 备注 + 10s 时间桶) 派生服务端去重窗口,
      同一 10 秒内的相同请求判为重复,不同批次(不同桶)互不影响。
    返回 20 位 hex ref · 由调用方拼进 related_order_id / description 与账目原子绑定。
    """
    idem_key = (
        request.headers.get("Idempotency-Key")
        or request.headers.get("X-Idempotency-Key")
        or ""
    ).strip()
    note = getattr(req, "description", None) or getattr(req, "reason", None) or ""
    if idem_key:
        seed = f"{kind}:k:{idem_key}"
    else:
        bucket = int(time.time()) // 10  # 10s 服务端去重窗口
        # 🔴 [P0 热修] 用 getattr 而不是直接取属性:供货 DTO(`SupplyDownstreamRequest`)
        #    的字段是 downstream_user_id / paid_points / bonus_points,没有
        #    customer_user_id / tool_points / publish_points。写死字段名会让
        #    供货端点在**第一次调用**就 AttributeError 500。
        target = getattr(req, "customer_user_id", None) or getattr(req, "downstream_user_id", 0)
        amounts = ":".join(str(getattr(req, f, 0) or 0) for f in (
            "tool_points", "publish_points", "bonus_points", "paid_points",
        ))
        seed = f"{kind}:s:{agent_user_id}:{target}:{amounts}:{note}:{bucket}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20]


@router.post("/inventory/supply-downstream", response_model=SupplyDownstreamResponse)
async def agent_supply_downstream(req: SupplyDownstreamRequest, request: Request):
    """🔴 [P0 热修 §1+§2] 上游线下供货给下线服务商 —— 这就是被阻断掉的那个动作。

    与 `allocate-offline` 的区别只在**账本落点**:进对方的库存算力(可继续向下分销),
    而不是可用算力。守卫走同一套**有向**关系解析:没有生效渠道关系的目标,
    响应与"账号不存在"同构,不透露对方是否存在(R5)。
    """
    a = _require_agent(request)
    if req.paid_points == 0 and req.bonus_points == 0:
        raise HTTPException(400, "供货算力至少一项 > 0")

    with get_db() as conn:
        cur = conn.cursor()
        try:
            rel = _resolve_relationship(cur, int(a["user_id"]), int(req.downstream_user_id))
        except _RelationError:
            rel = None
        if not rel or not rel.get("has_channel_relationship"):
            # 与"账号不存在"同一份响应 —— 不因为多了个写动作就多一个探测位。
            raise HTTPException(
                status_code=404, detail={"code": "TARGET_NOT_FOUND", "message": "未找到该账号"},
            )

        # 幂等:与线下划拨同一套派生规则,避免双击造成双侧账目翻倍。
        supply_ref = f"supply_{_compute_offline_idem_ref(request, a['user_id'], req, kind='supply')}"
        cur.execute(
            """SELECT 1 FROM agent_inventory_transactions
               WHERE agent_user_id=%s AND type='resale_transfer_in'
                 AND related_customer_user_id=%s AND description LIKE %s
               LIMIT 1""",
            (int(req.downstream_user_id), int(a["user_id"]), f"%{supply_ref}%"),
        )
        if cur.fetchone():
            up_w = get_or_create_inventory_wallet(cur, int(a["user_id"]))
            down_w = get_or_create_inventory_wallet(cur, int(req.downstream_user_id))
            conn.commit()
            return SupplyDownstreamResponse(
                success=True, downstream_user_id=int(req.downstream_user_id),
                supplied_paid=0, supplied_bonus=0,
                agent_paid_inventory_after=up_w["paid_inventory_points"],
                agent_bonus_inventory_after=up_w["bonus_inventory_points"],
                downstream_paid_inventory_after=down_w["paid_inventory_points"],
                downstream_bonus_inventory_after=down_w["bonus_inventory_points"],
            )

        from services.agent_inventory import supply_downstream_inventory
        from services.dealer_inventory_resale import ResaleError

        note = (req.description or "线下供货给下线服务商").strip()
        try:
            result = supply_downstream_inventory(
                cur,
                upstream_user_id=int(a["user_id"]),
                downstream_user_id=int(req.downstream_user_id),
                paid_points=int(req.paid_points),
                bonus_points=int(req.bonus_points),
                description=f"{note} [{supply_ref}]",
                idempotency_ref=supply_ref,
            )
        except ValueError as exc:
            # 库存不足等业务校验 → 400,透传文案(别笼统"请重试")
            raise HTTPException(status_code=400, detail=str(exc))
        except ResaleError as exc:
            # lot 不足 fail-closed:整笔回滚,钱包一分未动
            raise HTTPException(
                status_code=409,
                detail={"code": "INVENTORY_LOT_INSUFFICIENT", "message": "可供货的库存批次不足,请先进货"},
            )
        conn.commit()

    return SupplyDownstreamResponse(
        success=True,
        downstream_user_id=int(req.downstream_user_id),
        supplied_paid=result["supplied_paid"],
        supplied_bonus=result["supplied_bonus"],
        agent_paid_inventory_after=result["agent_paid_after"],
        agent_bonus_inventory_after=result["agent_bonus_after"],
        downstream_paid_inventory_after=result["downstream_paid_after"],
        downstream_bonus_inventory_after=result["downstream_bonus_after"],
    )


@router.post("/inventory/allocate-offline", response_model=AllocateRevokeResultResponse)
async def agent_allocate_offline(req: AllocateOfflineRequest, request: Request):
    """
    线下划拨 · 双侧写入(落点口径 = 单账本收敛后的真实行为,2026-07-27 基线 `e4adbf2f6`):

    - 服务商侧 `agent_inventory_wallets`:
        paid_inventory  -= (tool_points + publish_points)
        bonus_inventory -= bonus_points
    - 客户侧 **`user_wallets`**(不再是 `customer_agent_credit_wallets` 三池 ——
      那张表已停写;老 docstring 写的"三池 += 各自"与代码不符,2026-08-17 订正):
        paid_points  += (tool_points + publish_points)   ← 充值算力
        bonus_points += bonus_points                     ← 赠送算力
      实际写入由 `services.customer_entitlement.grant_to_customer` 执行,
      流水落 `point_transactions`(type=`agent_grant`)。
    - 不写 agent_revenue_ledger(服务商已线下收款 · 平台不代收)

    🔴 请求模型仍保留 `publish_points` 字段(向后兼容,禁从 API 拔掉),
       但它与 `tool_points` 落到**同一个** `paid_points`;前端自 2026-08-17 起
       固定传 `publish_points=0`(弹窗已收敛成 充值算力 / 赠送算力 两格)。
    """
    a = _require_agent(request)
    if req.tool_points == 0 and req.publish_points == 0 and req.bonus_points == 0:
        raise HTTPException(400, "划拨积分至少一项 > 0")
    if min(req.tool_points, req.publish_points, req.bonus_points) < 0:
        raise HTTPException(400, "划拨积分必须非负")

    with get_db() as conn:
        cur = conn.cursor()
        # 新建商业绑定只能走管理员 expected_version/CAS 治理接口。这里与
        # 管理员换绑共享 subject advisory lock，并在锁内复核当前关系。
        _require_customer_owned_by_agent(cur, a["user_id"], req.customer_user_id)

        # [GEO-R2-CAN-017] 幂等防重:offline_ref 由幂等键派生并写入客户流水 related_order_id
        # (与双侧账目原子绑定)。若同 ref 的 allocate 流水已存在 → 判为重复提交 · 重放当前余额,
        # 不再二次扣代理库存/给客户入账。放在归属校验之后,保证越权仍返 403。
        offline_ref = f"offline_{_compute_offline_idem_ref(request, a['user_id'], req, kind='alloc')}"
        # [单账本收敛 2026-07-27] 幂等标记从 customer_credit_transactions 搬到
        # point_transactions —— 信用流水表停写后原查询会恒为空 → 重复提交会二次划拨。
        cur.execute(
            """
            SELECT 1 FROM point_transactions
            WHERE user_id = %s AND order_id = %s AND type = 'agent_grant'
            LIMIT 1
            """,
            (req.customer_user_id, offline_ref),
        )
        if cur.fetchone():
            # [单账本收敛 2026-07-27] 重放当前余额:读客户 user_wallets 而非信用钱包
            from db.wallet_db import get_or_create_wallet as _get_user_wallet
            cust_w = _get_user_wallet(req.customer_user_id)
            agent_w = get_or_create_inventory_wallet(cur, a["user_id"])
            from services.notification_events import NotificationEventType, RecipientKind
            from services.notification_outbox import enqueue_notification_event
            cur.execute("SELECT clock_timestamp() AS event_at")
            event_at = cur.fetchone()["event_at"]
            allocation_facts = {
                "business_no": offline_ref,
                "points": f"{req.tool_points + req.publish_points + req.bonus_points:,}",
                "status": "库存划拨已完成",
                "occurred_at": event_at.isoformat(timespec="seconds"),
            }
            for recipient_user_id, recipient_kind in (
                (int(a["user_id"]), RecipientKind.AGENT),
                (int(req.customer_user_id), RecipientKind.CUSTOMER),
            ):
                enqueue_notification_event(
                    cur,
                    event_type=NotificationEventType.INVENTORY_ALLOCATION_COMPLETED,
                    business_id=offline_ref,
                    terminal_state="completed",
                    recipient_user_id=recipient_user_id,
                    recipient_kind=recipient_kind,
                    facts=allocation_facts,
                )
            conn.commit()
            return AllocateRevokeResultResponse(
                success=True,
                customer_user_id=req.customer_user_id,
                new_tool_credit=int(cust_w.get("paid_points") or 0),
                new_publish_credit=0,
                new_bonus_credit=int(cust_w.get("bonus_points") or 0),
                agent_paid_inventory_after=agent_w["paid_inventory_points"],
                agent_bonus_inventory_after=agent_w["bonus_inventory_points"],
            )

        # 代理侧:tool+publish 都来自 paid_inventory · bonus 来自 bonus_inventory
        paid_decrement = req.tool_points + req.publish_points
        try:
            agent_after = allocate_offline(
                cur,
                agent_user_id=a["user_id"],
                customer_user_id=req.customer_user_id,
                paid_points=paid_decrement,
                bonus_points=req.bonus_points,
                description=req.description or "线下划拨",
            )
        except ValueError as e:
            # 库存不足等业务校验 → 400(非 500)· 透传文案让前端明确提示而非笼统"请重试"
            raise HTTPException(status_code=400, detail=str(e))

        # 客户侧:三池入账 · related_order_id 用上方幂等 offline_ref 标记线下来源(见 [GEO-R2-CAN-017])
        # [单账本收敛 2026-07-27] 落点改为客户 user_wallets:
        #   tool + publish → paid_points(充值算力) · bonus → bonus_points(赠送算力)
        from services.customer_entitlement import grant_to_customer
        customer_after = grant_to_customer(
            cur,
            customer_user_id=req.customer_user_id,
            agent_user_id=a["user_id"],
            paid_points=req.tool_points + req.publish_points,
            bonus_points=req.bonus_points,
            related_order_id=offline_ref,
            source="offline_allocation",
            description=req.description or "线下划拨",
        )
        from services.notification_events import NotificationEventType, RecipientKind
        from services.notification_outbox import enqueue_notification_event
        cur.execute("SELECT clock_timestamp() AS event_at")
        event_at = cur.fetchone()["event_at"]
        allocation_facts = {
            "business_no": offline_ref,
            "points": f"{req.tool_points + req.publish_points + req.bonus_points:,}",
            "status": "库存划拨已完成",
            "occurred_at": event_at.isoformat(timespec="seconds"),
        }
        for recipient_user_id, recipient_kind in (
            (int(a["user_id"]), RecipientKind.AGENT),
            (int(req.customer_user_id), RecipientKind.CUSTOMER),
        ):
            enqueue_notification_event(
                cur,
                event_type=NotificationEventType.INVENTORY_ALLOCATION_COMPLETED,
                business_id=offline_ref,
                terminal_state="completed",
                recipient_user_id=recipient_user_id,
                recipient_kind=recipient_kind,
                facts=allocation_facts,
            )
        conn.commit()
    return AllocateRevokeResultResponse(
        success=True,
        customer_user_id=req.customer_user_id,
        # [单账本收敛 2026-07-27] 响应字段名保持不变(前端契约),值改为客户 user_wallets 实际余额:
        #   new_tool_credit ← paid_points(充值算力) · new_publish_credit 恒 0(已无独立发布池)
        new_tool_credit=customer_after["paid_points"],
        new_publish_credit=0,
        new_bonus_credit=customer_after["bonus_points"],
        agent_paid_inventory_after=agent_after["paid_inventory_points"],
        agent_bonus_inventory_after=agent_after["bonus_inventory_points"],
    )


@router.post("/inventory/revoke-offline", response_model=AllocateRevokeResultResponse)
async def agent_revoke_offline(req: RevokeOfflineRequest, request: Request):
    """
    线下 revoke · W2 走总余额 cap(不按 allocation 分批 · 那个留 W4 加 allocation_id)
    customer.revoke_credit 内部做三层 min(请求 · 当前余额 · 不传 order_id 时仅总余额 cap)
    """
    a = _require_agent(request)
    if min(req.tool_points, req.publish_points, req.bonus_points) < 0:
        raise HTTPException(400, "撤回积分必须非负")
    with get_db() as conn:
        cur = conn.cursor()
        _require_customer_owned_by_agent(cur, a["user_id"], req.customer_user_id)

        # [GEO-R2-CAN-017] 幂等防重:revoke 走总余额 cap 路径(related_order_id 必须保持 None,
        # 传值会切到 order cap 语义)。故把幂等 ref 作为 [idem:<ref>] 标记写进 description,
        # 命中同 ref 的历史 revoke 流水 → 判为重复提交 · 重放当前余额,不再二次撤回/回库存。
        idem_marker = f"[idem:{_compute_offline_idem_ref(request, a['user_id'], req, kind='revoke')}]"
        # [单账本收敛 2026-07-27] 幂等标记搬到 point_transactions(信用流水表已停写)
        cur.execute(
            """
            SELECT 1 FROM point_transactions
            WHERE user_id = %s AND type = 'agent_grant_revoke'
              AND description LIKE %s
            LIMIT 1
            """,
            (req.customer_user_id, f"%{idem_marker}%"),
        )
        if cur.fetchone():
            # [单账本收敛 2026-07-27] 重放当前余额:读客户 user_wallets 而非信用钱包
            from db.wallet_db import get_or_create_wallet as _get_user_wallet
            cust_w = _get_user_wallet(req.customer_user_id)
            agent_w = get_or_create_inventory_wallet(cur, a["user_id"])
            return AllocateRevokeResultResponse(
                success=True,
                customer_user_id=req.customer_user_id,
                new_tool_credit=int(cust_w.get("paid_points") or 0),
                new_publish_credit=0,
                new_bonus_credit=int(cust_w.get("bonus_points") or 0),
                agent_paid_inventory_after=agent_w["paid_inventory_points"],
                agent_bonus_inventory_after=agent_w["bonus_inventory_points"],
            )

        # 客户侧:总余额 cap revoke(W2 不传 related_order_id · 走 cap 路径)
        # [单账本收敛 2026-07-27] 从客户 user_wallets 回收(按当前余额夹紧,不扣成负)
        from services.customer_entitlement import revoke_from_customer as _revoke_customer_wallet
        customer_after = _revoke_customer_wallet(
            cur,
            customer_user_id=req.customer_user_id,
            agent_user_id=a["user_id"],
            paid_points=req.tool_points + req.publish_points,
            bonus_points=req.bonus_points,
            related_order_id=None,  # W2 不分批
            source="refund_revoke",
            description=f"{req.reason or '代理 revoke 线下'} {idem_marker}",
        )
        actually = {
            "tool": customer_after.get("revoked_paid", 0),
            "publish": 0,
            "bonus": customer_after.get("revoked_bonus", 0),
        }

        # 代理侧:按实际 revoke 量回库存(三层 min 后的真实值)
        actual_paid = int(actually.get("tool", 0) or 0) + int(actually.get("publish", 0) or 0)
        actual_bonus = int(actually.get("bonus", 0) or 0)
        agent_after = revoke_from_customer(
            cur,
            agent_user_id=a["user_id"],
            customer_user_id=req.customer_user_id,
            paid_points_to_revoke=actual_paid,
            bonus_points_to_revoke=actual_bonus,
            description=req.reason or "代理 revoke 线下",
        )
        conn.commit()
    return AllocateRevokeResultResponse(
        success=True,
        customer_user_id=req.customer_user_id,
        # [单账本收敛 2026-07-27] 响应字段名保持不变(前端契约),值改为客户 user_wallets 实际余额:
        #   new_tool_credit ← paid_points(充值算力) · new_publish_credit 恒 0(已无独立发布池)
        new_tool_credit=customer_after["paid_points"],
        new_publish_credit=0,
        new_bonus_credit=customer_after["bonus_points"],
        agent_paid_inventory_after=agent_after["paid_inventory_points"],
        agent_bonus_inventory_after=agent_after["bonus_inventory_points"],
    )


# ============================================================
# 7-8. 客户额度查询(代理视角)
# ============================================================

@router.get("/customers/{customer_user_id}/credit", response_model=AgentCustomerCreditResponse)
async def agent_customer_credit(customer_user_id: int, request: Request):
    """客户额度查询(代理视角)· **纯读 · 单账本**

    [单账本接线 2026-08-17 · C-3] 原来这里调 get_or_create 语义的建行函数,
    它在客户无行时会往停写表 `customer_agent_credit_wallets` **INSERT 一行** ——
    一个 GET 端点带写副作用,且写的是 2026-07-29 已停写的表,会打破
    「信用钱包户数不再增长」这条收敛不变式。

    改为纯读 `user_wallets`(客户算力单账本唯一落点),响应契约**一字不变**:
    · `tool_credit_points`    ← user_wallets.paid_points(充值算力,可用于全部功能)
    · `publish_credit_points` ← 恒 0(单账本无「发布专用池」这个概念)
    · `bonus_credit_points`   ← user_wallets.bonus_points(赠送算力)
    映射与 `services/customer_entitlement.py` 及迁移脚本同源。
    不新增字段 —— response_model 是 extra="forbid" 先例,多一个键即 500。

    ⚠️ DEPRECATED:前端封装 `agentApi.customerCredit`(frontend/src/lib/v35w2Api.ts)
    全仓 0 调用点。端点保留仅为兼容,勿在新代码引用;请改用钱包余额接口。
    """
    a = _require_agent(request)
    with get_db() as conn:
        cur = conn.cursor()
        binding = _require_customer_owned_by_agent(cur, a["user_id"], customer_user_id)
        # 纯读:没有行就是 0,绝不建行
        cur.execute(
            "SELECT paid_points, bonus_points FROM user_wallets WHERE user_id = %s",
            (customer_user_id,),
        )
        _w = cur.fetchone() or {}
        wallet = {
            "tool_credit_points": int(_g(_w, "paid_points", 0) or 0),
            "publish_credit_points": 0,
            "bonus_credit_points": int(_g(_w, "bonus_points", 0) or 0),
        }

        cur.execute("""
            SELECT display_name, phone FROM users WHERE id = %s
        """, (customer_user_id,))
        u = cur.fetchone() or {}

        cur.execute("""
            SELECT COALESCE(SUM(CASE WHEN points > 0 THEN points ELSE 0 END), 0) AS allocated,
                   COALESCE(SUM(CASE WHEN type = 'consume' THEN ABS(points) ELSE 0 END), 0) AS consumed
            FROM customer_credit_transactions
            WHERE customer_user_id = %s
        """, (customer_user_id,))
        sums = cur.fetchone() or {}
        conn.commit()

    phone_masked = _mask_phone(_g(u, "phone"))
    return AgentCustomerCreditResponse(
        customer_user_id=customer_user_id,
        customer_display_name=_g(u, "display_name"),
        phone_masked=phone_masked,
        tool_credit_points=wallet.get("tool_credit_points", 0),
        publish_credit_points=wallet.get("publish_credit_points", 0),
        bonus_credit_points=wallet.get("bonus_credit_points", 0),
        total_allocated=int(_g(sums, "allocated", 0) or 0),
        total_consumed=int(_g(sums, "consumed", 0) or 0),
        binding_source=binding.get("binding_source"),
        bound_at=binding.get("bound_at"),
        dispute_status=binding.get("dispute_status"),
    )


@router.get(
    "/customers/{customer_user_id}/credit/transactions",
    response_model=AgentCustomerCreditTxResponse,
)
async def agent_customer_credit_tx(
    customer_user_id: int,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    a = _require_agent(request)
    with get_db() as conn:
        cur = conn.cursor()
        _require_customer_owned_by_agent(cur, a["user_id"], customer_user_id)

        cur.execute("""
            SELECT COUNT(*) AS c FROM customer_credit_transactions
            WHERE customer_user_id = %s
        """, (customer_user_id,))
        total = _row_count(cur.fetchone())

        cur.execute("""
            SELECT id, type, pool, points, related_order_id, description, created_at
            FROM customer_credit_transactions
            WHERE customer_user_id = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """, (customer_user_id, limit, offset))
        items = [
            AgentCustomerCreditTxItem(**_dict_row(row, [
                "id","type","pool","points","related_order_id","description","created_at",
            ])) for row in cur.fetchall()
        ]

        # 线上 FIFO 总和(可分批 revoke 上限)
        cur.execute("""
            SELECT DISTINCT related_order_id FROM customer_credit_transactions
            WHERE customer_user_id = %s
              AND type = 'allocate'
              AND related_order_id IS NOT NULL
              AND related_order_id NOT LIKE 'offline_%%'
        """, (customer_user_id,))
        online_fifo = 0
        for r in cur.fetchall():
            oid = _g(r, "related_order_id", 0)
            if oid:
                unspent = compute_unspent_from_order(cur, customer_user_id, oid)
                online_fifo += sum(unspent.values()) if isinstance(unspent, dict) else int(unspent or 0)

        # 线下总余额(W2 仅 cap revoke · 分批留 W4)
        cur.execute("""
            SELECT COALESCE(SUM(points), 0) AS s
            FROM customer_credit_transactions
            WHERE customer_user_id = %s
              AND (related_order_id LIKE 'offline_%%' OR related_order_id IS NULL)
        """, (customer_user_id,))
        offline_total = int(_g(cur.fetchone(), "s", 0) or 0)
        conn.commit()

    return AgentCustomerCreditTxResponse(
        items=items, total=total,
        online_fifo_available=online_fifo,
        offline_total_remaining=max(0, offline_total),
    )


# ============================================================
# 9-10. 白标定价 SKU(代理视角 · raw cost 永不外露)
# ============================================================

def _publish_retail_after_agent_mutation(
    cur,
    *,
    agent_user_id: int,
    actor_id: int,
    reason: str,
):
    """Publish source mutation atomically when the runtime retail SSOT is active."""
    from config.pricing_ssot_flags import pricing_flags_snapshot

    if not pricing_flags_snapshot(cur).get("PRICING_DUAL_SSOT_ENABLED", False):
        return None
    from services.pricing_publication import materialize_retail_catalog

    return materialize_retail_catalog(
        cur,
        agent_user_id=int(agent_user_id),
        actor_id=int(actor_id),
        reason=str(reason),
    )


def _lock_retail_before_agent_mutation(cur) -> bool:
    """Use the publication writer lock order before the first management write."""
    from config.pricing_ssot_flags import pricing_flags_snapshot

    if not pricing_flags_snapshot(cur).get("PRICING_DUAL_SSOT_ENABLED", False):
        return False
    from services.pricing_publication import lock_retail_management_sources

    lock_retail_management_sources(cur)
    return True

@router.get("/pricing/skus", response_model=AgentSKUListResponse)
async def agent_pricing_skus(request: Request):
    """
    Canonical 服务商零售目录。平台模板不再自动出现在零售列表中。
    """
    a = _require_agent(request)
    with get_db() as conn:
        cur = conn.cursor()
        from services.agent_retail_sku_schema import assert_schema_ready
        from services.agent_pricing import preview_agent_retail_sku

        assert_schema_ready(cur)
        cur.execute("""
            SELECT o.id AS override_id, o.retail_sku_id, o.version,
                   o.sku_template_id, o.source_template_id, o.points_granted,
                   o.custom_name, o.custom_subtitle, o.custom_sales_pitch, o.custom_scene,
                   o.retail_cents, o.is_active, o.sort_order,
                   t.template_code, t.sku_type
            FROM agent_sku_overrides o
            LEFT JOIN sku_templates t ON t.id = o.source_template_id
            WHERE o.agent_user_id = %s AND o.deleted_at IS NULL
            ORDER BY o.sort_order, o.id
        """, (a["user_id"],))
        override_rows = [dict(r) for r in cur.fetchall()]
        items = []
        for d in override_rows:
            retail = int(d.get("retail_cents") or 0)
            economics = preview_agent_retail_sku(
                cur,
                agent_user_id=int(a["user_id"]),
                points_granted=int(d["points_granted"]),
                retail_cents=retail,
                _schema_checked=True,
            )
            wholesale = int(economics["estimated_cost_cents"])
            items.append(AgentSKUItem(
                retail_sku_id=str(d["retail_sku_id"]),
                version=int(d["version"]),
                sku_template_id=(int(d["sku_template_id"]) if d.get("sku_template_id") is not None else None),
                source_template_id=(int(d["source_template_id"]) if d.get("source_template_id") is not None else None),
                override_id=int(d["override_id"]),
                sku_key=d.get("template_code") or str(d["retail_sku_id"]),
                category=d.get("sku_type") or "credit_pack",
                display_name=str(d.get("custom_name") or ""),
                subtitle=d.get("custom_subtitle"),
                extra_promo_text=d.get("custom_sales_pitch"),
                scene=d.get("custom_scene"),
                sort_order=int(d.get("sort_order") or 0),
                points_granted=int(d["points_granted"]),
                wholesale_cents=wholesale,
                retail_cents=retail,
                is_active=bool(d.get("is_active") if d.get("is_active") is not None else True),
                margin_label=economics["margin_label"],
                margin_warning=economics["margin_action"],
            ))
    # 排序:category · provider sort_order · stable internal id
    items.sort(key=lambda it: (
        it.category or "",
        it.sort_order or 0,
        it.override_id if it.override_id is not None else 1 << 31,
    ))
    return AgentSKUListResponse(items=items)


@router.get("/pricing/templates")
async def agent_pricing_templates(request: Request):
    """
    已停用的服务商推荐模板兼容端点。

    服务商零售包改为直接自定义。保留空响应是为了兼容仍缓存旧 bundle 的浏览器，
    避免旧页面因 404 反复报错；平台模板、底价和建议价均不得进入服务商响应。
    """
    _require_agent(request)
    return {"templates": []}


@router.post("/pricing/skus", dependencies=[Depends(require_signed_agreement)])
async def agent_pricing_sku_create(req: AgentSKUCreateRequest, request: Request):
    """
    新增独立服务商零售包。source_template_id 仅记录可选预填来源。
    """
    a = _require_agent(request)
    if req.source_template_id and req.sku_template_id and req.source_template_id != req.sku_template_id:
        raise HTTPException(400, "source_template_id 与兼容 sku_template_id 冲突")
    source_template_id = req.source_template_id or req.sku_template_id
    with get_db() as conn:
        cur = conn.cursor()
        try:
            _lock_retail_before_agent_mutation(cur)
            result = create_agent_sku_override(
                cur,
                agent_user_id=a["user_id"],
                sku_template_id=None,
                source_template_id=source_template_id,
                points_granted=req.points_granted,
                client_request_id=req.client_request_id,
                custom_name=req.display_name,
                custom_subtitle=req.subtitle,
                custom_sales_pitch=req.extra_promo_text,
                custom_scene=req.scene,
                retail_cents=req.retail_cents,
                is_active=req.is_active,
                sort_order=req.sort_order or 0,
            )
            publication = _publish_retail_after_agent_mutation(
                cur,
                agent_user_id=int(a["user_id"]),
                actor_id=int(a["operator_user_id"]),
                reason=f"agent retail SKU create {result['retail_sku_id']}",
            )
        except (RetailSKUIdempotencyConflict, PricingPublicationError) as e:
            raise HTTPException(409, str(e))
        except RuntimeError as e:
            raise HTTPException(503, str(e))
        except ValueError as e:
            raise HTTPException(409 if "有效成本" in str(e) else 400, str(e))
        conn.commit()
    return {
        "success": True,
        "id": result["id"],
        "retail_sku_id": result["retail_sku_id"],
        "version": result["version"],
        "points_granted": result["points_granted"],
        "margin_label": result.get("margin_label"),
        "idempotent_replay": bool(result.get("idempotent_replay")),
        "published_catalog_version": publication.get("version_code") if publication else None,
    }


@router.post("/pricing/skus/preview")
async def agent_pricing_sku_preview(req: AgentSKUPreviewRequest, request: Request):
    """Canonical provider-visible cost/profit preview; no relationship details."""
    a = _require_agent(request)
    from services.agent_pricing import preview_agent_retail_sku

    with get_db() as conn:
        try:
            data = preview_agent_retail_sku(
                conn.cursor(),
                agent_user_id=int(a["user_id"]),
                points_granted=req.points_granted,
                retail_cents=req.retail_cents,
            )
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    return {"success": True, "data": data}


@router.put("/pricing/skus/{override_id}", dependencies=[Depends(require_signed_agreement)])
async def agent_pricing_sku_save(
    override_id: int, req: AgentSKUUpdateRequest, request: Request
):
    """按 owner + optimistic version 编辑 canonical retail SKU。"""
    a = _require_agent(request)
    with get_db() as conn:
        cur = conn.cursor()
        try:
            _lock_retail_before_agent_mutation(cur)
            result = update_agent_sku_override(
                cur,
                agent_user_id=a["user_id"],
                override_id=override_id,
                custom_name=req.display_name,
                custom_subtitle=req.subtitle,
                custom_sales_pitch=req.extra_promo_text,
                custom_scene=req.scene,
                points_granted=req.points_granted,
                retail_cents=req.retail_cents,
                is_active=req.is_active,
                sort_order=req.sort_order,
                expected_version=req.version,
            )
            publication = _publish_retail_after_agent_mutation(
                cur,
                agent_user_id=int(a["user_id"]),
                actor_id=int(a["operator_user_id"]),
                reason=f"agent retail SKU update {result['retail_sku_id']} v{result['version']}",
            )
        except RetailSKUNotFound as e:
            raise HTTPException(404, str(e))
        except (RetailSKUVersionConflict, PricingPublicationError) as e:
            raise HTTPException(409, str(e))
        except RuntimeError as e:
            raise HTTPException(503, str(e))
        except ValueError as e:
            raise HTTPException(409 if "有效成本" in str(e) else 400, str(e))
        conn.commit()
    return {
        "success": True,
        "version": result["version"],
        "published_catalog_version": publication.get("version_code") if publication else None,
    }


@router.delete("/pricing/skus/{override_id}", dependencies=[Depends(require_signed_agreement)])
async def agent_pricing_sku_delete(
    override_id: int, request: Request, version: int = Query(..., ge=1),
):
    """
    Tombstone 零售包(by override_id · 归属校验在 service 层)。
    一律保留历史身份和订单引用；任何默认模板/批量改价都不得使它复活。
    """
    a = _require_agent(request)
    with get_db() as conn:
        cur = conn.cursor()
        try:
            _lock_retail_before_agent_mutation(cur)
            result = delete_agent_sku_override(
                cur, agent_user_id=a["user_id"], override_id=override_id,
                expected_version=version,
            )
            publication = _publish_retail_after_agent_mutation(
                cur,
                agent_user_id=int(a["user_id"]),
                actor_id=int(a["operator_user_id"]),
                reason=f"agent retail SKU tombstone override={override_id}",
            )
        except RetailSKUNotFound as e:
            raise HTTPException(404, str(e))
        except (RetailSKUVersionConflict, PricingPublicationError) as e:
            raise HTTPException(409, str(e))
        except RuntimeError as e:
            raise HTTPException(503, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        conn.commit()
    return {
        "success": True,
        "action": result.get("action"),
        "version": result.get("version"),
        "published_catalog_version": publication.get("version_code") if publication else None,
    }


@router.post("/pricing/skus/{override_id}/restore-suggested-price", dependencies=[Depends(require_signed_agreement)])
async def agent_pricing_sku_restore_suggested(
    override_id: int, request: Request, version: int = Query(..., ge=1),
):
    """Disabled compatibility route: platform suggested prices are not retail SSOT."""
    _require_agent(request)
    raise HTTPException(
        410,
        detail={
            "code": "PLATFORM_SUGGESTED_PRICE_DISABLED",
            "message": "平台建议售价不再用于服务商零售包，请按当前有效成本自行定价",
        },
    )


# ============================================================
# 10b. 全局加价系数(D3 · 2026-05-30 · 代理一键定价)
# ============================================================

class MarkupRatioRequest(BaseModel):
    # [2026-06-06 返修] 移除"客户毛利率"margin 暗门:仅保留 ratio(售价倍数)
    ratio: float


@router.get("/pricing/markup-ratio")
async def get_markup_ratio(request: Request):
    """读代理当前 SKU 全局加价系数(未设返 null)· 区别于 GEO quote_markup_ratio"""
    a = _require_agent(request)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT agent_sku_markup_ratio FROM users WHERE id = %s", (a["user_id"],))
        row = cur.fetchone()
    r = None
    if row:
        r = row["agent_sku_markup_ratio"] if isinstance(row, dict) else row[0]
    # [D3] admin 强制 override 优先展示(平台限定该服务商零售系数时·UI 显示实际生效值)
    admin_enforced = False
    try:
        from services.agent_pricing_overrides import get_agent_sku_markup_override
        _ov = get_agent_sku_markup_override(a["user_id"])
        if _ov is not None and _ov > 0:
            r = _ov
            admin_enforced = True
    except Exception:
        pass
    return {"ratio": float(r) if r is not None else None,
            "min": 1.0, "max": float(MAX_RETAIL_MULTIPLIER),
            "admin_enforced": admin_enforced}


@router.put("/pricing/markup-ratio", dependencies=[Depends(require_signed_agreement)])
async def set_markup_ratio(req: MarkupRatioRequest, request: Request):
    """仅保存系数(不批量改价 · 配合 apply-markup 一键应用)"""
    a = _require_agent(request)
    # [2026-05-30 bug4] 老板:销售定价由代理自定·不设上下限·丰俭由人 · 仅保留系数为正(防 0/负售价)
    if not math.isfinite(req.ratio) or req.ratio <= 0:
        raise HTTPException(400, "售价倍数须为正数")
    # [v12 item6] 系数统一走 canonical(与 apply-markup 同一列 agent_sku_markup_ratio · 同源量化):
    #   禁 round(ratio,2)(banker/float 误差)· 用 resolve_canonical_markup(Decimal ROUND_HALF_UP)· 否则同系数两存值。
    from services.agent_pricing import resolve_canonical_markup
    canonical_ratio = resolve_canonical_markup(req.ratio)["ratio"]
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_xact_lock(920512, %s)", (int(a["user_id"]),))
        cur.execute("UPDATE users SET agent_sku_markup_ratio = %s WHERE id = %s",
                    (canonical_ratio, a["user_id"]))
        conn.commit()
    return {"success": True, "ratio": float(canonical_ratio)}


# ===== [M2 2026-06-07] 服务商自设单篇内容成本 cost_per_article(A 完全覆盖系统动态成本)=====
class CostPerArticleRequest(BaseModel):
    cost: float | None = None  # 单篇内容成本(元)· None/留空 = 清空·回系统估算


@router.get("/pricing/cost-per-article")
async def get_agent_cost_per_article(request: Request):
    """[M2/Option B 2026-06-08] 读自设单篇内容成本(未设/留空返 null · 算价走系统动态估算)。
    所有登录用户可读自己的(只读自己行·WHERE id=self·读不需签免责协议)。"""
    uid = _require_logged_in_user_id(request)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT cost_per_article FROM users WHERE id = %s", (uid,))
        row = cur.fetchone()
    c = None
    if row:
        c = row["cost_per_article"] if isinstance(row, dict) else row[0]
    return {"cost_per_article": float(c) if c is not None else None, "system_default": 60.0}


@router.put("/pricing/cost-per-article", dependencies=[Depends(require_signed_agreement)])
async def set_agent_cost_per_article(req: CostPerArticleRequest, request: Request):
    """[M2/Option B 2026-06-08] 自设单篇内容成本 · 所有登录用户可设自己的:
    admin / 服务商(走 v2.3)放行;普通用户须已签《报价定价免责协议》(require_pricing_authority)。
    RBAC 仅本人(WHERE id=self 防 IDOR)· None/留空=清空回系统估算 · 设值=A完全覆盖系统动态。"""
    require_pricing_authority(request)  # 普通用户须已签免责协议(admin/服务商豁免)
    uid = _require_logged_in_user_id(request)
    val = None
    if req.cost is not None:
        if not math.isfinite(req.cost) or req.cost <= 0:
            raise HTTPException(400, "单篇成本须为正数")
        if req.cost > 2000:
            raise HTTPException(400, "单篇成本上限 ¥2000(防误填)")
        val = round(req.cost, 2)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET cost_per_article = %s WHERE id = %s", (val, uid))
        conn.commit()
    return {"success": True, "cost_per_article": val}


@router.post("/pricing/apply-markup", dependencies=[Depends(require_signed_agreement)])
async def apply_markup(req: MarkupRatioRequest, request: Request):
    """
    一键按倍数把代理【所有白标包(override)】售价定为 进货价 × 倍数(1:N · 2026-06-06)。
    · 过 update_agent_sku_override 护栏(label_margin)· 超协议上限的包自动跳过 + 提示
    · 保留代理已设的包装(custom_name/subtitle/sales_pitch/scene/上架/排序)· 只改 retail_cents

    [2026-06-06 返修] 移除"客户毛利率"margin 暗门:售价 = 进货价 × ratio(仅倍数一种模式)。
    """
    a = _require_agent(request)
    ratio = req.ratio
    # [2026-05-30 bug4] 不设上下限 · 仅系数为正(销售定价由代理自定)
    if not math.isfinite(ratio) or ratio <= 0:
        raise HTTPException(400, "加价系数须为正数")
    # [D3] admin 强制 sku markup override 优先(平台给单个服务商限定零售系数·丰俭由人·有降有涨)
    try:
        from services.agent_pricing_overrides import get_agent_sku_markup_override
        _admin_sku_ov = get_agent_sku_markup_override(a["user_id"])
        if _admin_sku_ov is not None and _admin_sku_ov > 0:
            ratio = _admin_sku_ov
    except Exception:
        pass
    # [v11 F5] 系数先量化成【唯一 canonical 值】(单点 resolve_canonical_markup),再【同时】用于
    #   bps / DB 存储 / 响应 —— 消除"1.234 按 1.234 定价、其它链路按存储的 1.23 读取"= 同系数两套价。
    from services.agent_pricing import apply_markup_for_agent, resolve_canonical_markup
    _cm = resolve_canonical_markup(ratio)
    canonical_ratio = _cm["ratio"]   # Decimal(2 位)· 与 bps 同源 · 直接入库(NUMERIC(4,2))
    bps = _cm["bps"]
    ratio_out = float(canonical_ratio)
    try:
        with get_db() as conn:
            cur = conn.cursor()
            _lock_retail_before_agent_mutation(cur)
            # 存 canonical 系数(与 bps 同源)· advisory 锁 + 定价/materialize 在 apply_markup_for_agent 内单点完成 · 可测
            cur.execute("UPDATE users SET agent_sku_markup_ratio = %s WHERE id = %s",
                        (canonical_ratio, a["user_id"]))
            _res = apply_markup_for_agent(cur, a["user_id"], bps)
            publication = _publish_retail_after_agent_mutation(
                cur,
                agent_user_id=int(a["user_id"]),
                actor_id=int(a["operator_user_id"]),
                reason=f"agent retail SKU apply markup {bps}bps",
            )
            conn.commit()
    except PricingPublicationError as exc:
        raise HTTPException(409, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    created, updated, skipped = _res["created"], _res["updated"], _res["skipped"]

    applied_count = len(created) + len(updated)
    return {
        "success": True, "ratio": ratio_out, "markup_bps": bps,
        "created_count": len(created), "updated_count": len(updated),
        "skipped_count": len(skipped), "applied_count": applied_count,
        "created": created, "updated": updated, "skipped": skipped,
        "published_catalog_version": publication.get("version_code") if publication else None,
        # 兼容旧前端(applied = created + updated)
        "applied": created + updated,
    }


# ============================================================
# 10c. 代理自定返利配置(D2-b · 2026-05-30 · 返利从代理 bonus_inventory 出)
# ============================================================

class RebateConfigRequest(BaseModel):
    enabled: bool
    rebate_rate: float
    max_rebate_points_per_order: Optional[int] = None


@router.get("/pricing/rebate-config")
async def get_rebate_config_ep(request: Request):
    """读代理返利规则(无配置返默认关闭)"""
    a = _require_agent(request)
    from services.agent_rebate import get_rebate_config
    with get_db() as conn:
        cur = conn.cursor()
        cfg = get_rebate_config(cur, a["user_id"])
    return cfg


@router.put("/pricing/rebate-config", dependencies=[Depends(require_signed_agreement)])
async def set_rebate_config_ep(req: RebateConfigRequest, request: Request):
    """代理设返利规则 · rate 0~100% · 返利积分从代理 bonus_inventory 出(丰俭由人)"""
    a = _require_agent(request)
    from services.agent_rebate import upsert_rebate_config
    if not math.isfinite(req.rebate_rate) or req.rebate_rate < 0 or req.rebate_rate > 1.0:
        raise HTTPException(400, "返利比例须在 0 ~ 100% 之间(且为有效数值)")
    with get_db() as conn:
        cur = conn.cursor()
        try:
            res = upsert_rebate_config(
                cur, a["user_id"], req.enabled, req.rebate_rate, req.max_rebate_points_per_order
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        conn.commit()
    return {"success": True, **res}


# ============================================================
# 11-13. 工厂结算
# ============================================================

@router.get("/settlement/balance", response_model=AgentSettlementBalanceResponse)
async def agent_settlement_balance(request: Request):
    a = _require_agent(request)
    with get_db() as conn:
        cur = conn.cursor()
        bal = get_agent_balance(cur, a["user_id"])
        # 累计预扣税 · ledger.status 只有 frozen/settled/cancelled(原 IN 含 pending_payout/paid 是死值)· 只认已结算
        cur.execute("""
            SELECT COALESCE(SUM(tax_withholding_cents), 0) AS s
            FROM agent_revenue_ledger
            WHERE agent_user_id = %s AND status = 'settled'
        """, (a["user_id"],))
        tax_total = int(_g(cur.fetchone(), "s", 0) or 0)
        # [2026-05-30 修恒0卡] 退款追回(待抵扣)· source='refund_clawback' 负数行(insert_revenue_clawback 写)· 取绝对值展示
        cur.execute("""
            SELECT COALESCE(SUM(ABS(agent_settlement_cents)), 0) AS s
            FROM agent_revenue_ledger
            WHERE agent_user_id = %s AND source = 'refund_clawback' AND status <> 'cancelled'
        """, (a["user_id"],))
        clawback_total = int(_g(cur.fetchone(), "s", 0) or 0)
    return AgentSettlementBalanceResponse(
        frozen_cents=bal.get("frozen_cents", 0),
        available_cents=bal.get("available_cents", 0),
        # [2026-05-30 修恒0卡] get_agent_balance 实际返 pending_payout_cents(原读 pending_cents 恒缺→¥0)
        pending_cents=bal.get("pending_payout_cents", 0),
        paid_cents=bal.get("paid_cents", 0),
        # [2026-05-30 修恒0卡] get_agent_balance 不返此 key · 单独查 refund_clawback(原读不存在的 key→¥0)
        clawback_pending_cents=clawback_total,
        tax_withholding_total_cents=tax_total,
    )


@router.get("/settlement/available-items", response_model=AgentAvailableItemsResponse)
async def agent_settlement_available_items(request: Request):
    """
    可提现 ledger items · [boss r1 修正]:
    - W1 schema 真字段:agent_settlement_cents(不是 amount_cents)
    - 排除已被任何 pending/approved/paid request 锁定的 ledger(items SSOT · 不用 deprecated settlement_request_id)
    - 部分锁定:NOT EXISTS 排除已完全锁定的;部分锁住可用剩余(W2 仍按完整 ledger 列展示 · 内部 service create 时 FIFO 选剩余 cap)
    """
    a = _require_agent(request)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT l.id, l.source, l.recharge_order_id, l.customer_user_id,
                   l.customer_paid_cents,
                   l.agent_settlement_cents AS amount_cents,
                   l.tax_withholding_cents, l.status,
                   l.frozen_at, l.settle_at, l.settled_at,
                   l.manual_review_required, l.note,
                   COALESCE((
                       SELECT SUM(i.locked_amount_cents)
                       FROM agent_settlement_request_items i
                       JOIN agent_settlement_requests r ON r.id = i.settlement_request_id
                       WHERE i.ledger_id = l.id AND r.status IN ('pending','approved','paid')
                   ), 0)
                   + COALESCE((
                       SELECT SUM(ri.locked_amount_cents)
                       FROM agent_commission_redemption_items ri
                       JOIN agent_commission_redemption_requests rr ON rr.id = ri.redemption_request_id
                       WHERE ri.ledger_id = l.id AND rr.status = 'redeemed'
                   ), 0) AS already_locked
            FROM agent_revenue_ledger l
            WHERE l.agent_user_id = %s
              AND l.status = 'settled'
              AND l.agent_settlement_cents > 0
              AND l.manual_review_required = FALSE
              AND COALESCE(l.reversed_at, NULL) IS NULL
            ORDER BY l.settled_at NULLS LAST, l.id
        """, (a["user_id"],))
        items = []
        total = 0
        for row in cur.fetchall():
            d = _dict_row(row, [
                "id","source","recharge_order_id","customer_user_id","customer_paid_cents",
                "amount_cents","tax_withholding_cents","status",
                "frozen_at","settle_at","settled_at","manual_review_required","note",
                "already_locked",
            ])
            full_amount = int(d.get("amount_cents", 0) or 0)
            locked = int(d.pop("already_locked", 0) or 0)
            remaining = full_amount - locked
            if remaining <= 0:
                continue  # 已完全锁定 · 不展示
            # 展示剩余可提部分(前端体感"这条 ledger 还能提 X")
            d["amount_cents"] = remaining
            items.append(AgentLedgerItem(**d))
            total += remaining
    return AgentAvailableItemsResponse(items=items, total_amount_cents=total)


@router.get("/settlement/withdrawal-quote")
async def agent_withdrawal_quote(amount_yuan: float, request: Request):
    """[V3.5 v7 批1D · 老板 B 2026-06-08] 提现报价 · 提交前清晰看「提 ¥X · 扣 ¥Y · 到账 ¥Z」三段。

    新 ledger 提现(settled margin · 与利润换算力共用 available SSOT 防双花)。
    ⚠️ 老 commission_points 提现(V3.2)废除 = 独立后续 batch · 不在本批。
    W2 铁律(v35_w2_dto docstring):只返【金额】(平台服务费合并 + 代扣税单列 + 总扣 + 到账)·
    绝不返费率 *_bps(平台费率结构是商业机密)。
    """
    a = _require_agent(request)
    if not math.isfinite(amount_yuan) or amount_yuan <= 0:
        raise HTTPException(400, "提现金额必须 > 0")
    gross_cents = int(round(amount_yuan * 100))
    if gross_cents <= 0:
        raise HTTPException(400, "提现金额过小")
    from services.agent_pricing import calc_withdrawal_fees
    with get_db() as conn:
        cur = conn.cursor()
        bal = get_agent_balance(cur, a["user_id"])
        fees = calc_withdrawal_fees(cur, a["user_id"], gross_cents)
    if fees["net_cents"] <= 0:
        raise HTTPException(400, "提现金额过小 · 扣费后到账为 0 · 请提高提现金额")
    available_cents = int(bal.get("available_cents", 0) or 0)
    return {
        "success": True,
        "gross_cents": gross_cents,
        "available_cents": available_cents,
        "sufficient": available_cents >= gross_cents,
        # W2 铁律:平台服务费合并(不分项露通道/服务费率)· 代扣税单列(金额 · 允许)
        "platform_fee_cents": fees["platform_fee_cents"],
        "tax_cents": fees["tax_cents"],
        "total_fee_cents": fees["total_fee_cents"],
        "net_cents": fees["net_cents"],
        "label": f"提现 ¥{gross_cents/100:.2f} · 扣 ¥{fees['total_fee_cents']/100:.2f} · 到账 ¥{fees['net_cents']/100:.2f}",
    }


@router.post("/settlement/requests", response_model=AgentSettlementRequestResponse, dependencies=[Depends(require_signed_agreement)])
async def agent_settlement_request_create(
    req: AgentSettlementRequestCreate, request: Request
):
    """
    W1 service 内部走 FIFO 选 settled ledger items + 部分锁定(Codex r2 P0-1 修正版)
    传金额 · service 自动选 items · 同一 ledger 不重复锁定
    """
    a = _require_agent(request)
    with get_db() as conn:
        cur = conn.cursor()
        try:
            result = create_settlement_request(
                cur,
                agent_user_id=a["user_id"],
                request_amount_cents=req.amount_cents,
                bank_name=req.bank_name,
                bank_account=req.bank_account,
                account_holder=req.account_holder,
                invoice_required=req.invoice_required,
            )
        except ValueError as ve:
            raise HTTPException(400, str(ve))
        request_id = int(result.get("request_id") or result.get("id"))
        cur.execute(
            "SELECT created_at FROM agent_settlement_requests WHERE id=%s",
            (request_id,),
        )
        created = cur.fetchone()
        created_at = created["created_at"] if isinstance(created, dict) else created[0]
        from services.notification_events import NotificationEventType, RecipientKind
        from services.notification_outbox import (
            enqueue_admin_notification_events,
            enqueue_notification_event,
        )
        facts = {
            "business_no": f"SETTLEMENT-{request_id}",
            "amount": f"{int(req.amount_cents) / 100:.2f} 元",
            "status": "已提交，等待审核",
            "occurred_at": created_at.isoformat(timespec="seconds"),
        }
        enqueue_notification_event(
            cur,
            event_type=NotificationEventType.AGENT_SETTLEMENT_SUBMITTED,
            business_id=str(request_id),
            terminal_state="submitted",
            recipient_user_id=int(a["user_id"]),
            recipient_kind=RecipientKind.AGENT,
            facts=facts,
        )
        enqueue_admin_notification_events(
            cur,
            event_type=NotificationEventType.AGENT_SETTLEMENT_SUBMITTED,
            business_id=str(request_id),
            terminal_state="submitted",
            facts=facts,
        )
        conn.commit()
    return AgentSettlementRequestResponse(
        id=request_id,
        status=result.get("status", "pending"),
        amount_cents=result.get("request_amount_cents") or result.get("amount_cents", req.amount_cents),
        ledger_count=len(result.get("locked_items", [])) or result.get("ledger_count", 0),
        # [V3.5 v7 批1D] 提现透明三段(金额 · 不露 *_bps · W2 铁律)
        net_cents=result.get("net_cents", 0),
        total_fee_cents=result.get("total_fee_cents", 0),
        platform_fee_cents=result.get("platform_fee_cents", 0),
        tax_cents=result.get("tax_cents", 0),
    )


@router.get("/settlement/payout-account")
async def agent_settlement_payout_account(request: Request):
    """
    [GAPS#4 2026-06-04] 返回该代理最近一次提现使用的收款账户(供前端预填 · 免每次手填)。
    复用现有提现历史 agent_settlement_requests · 不新建表 · 账号脱敏只回尾 4 位。
    无历史返 null。
    """
    a = _require_agent(request)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT bank_name, account_holder, bank_account
            FROM agent_settlement_requests
            WHERE agent_user_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (a["user_id"],),
        )
        row = cur.fetchone()
    if not row:
        return {"account": None}
    bank_name = row["bank_name"] if isinstance(row, dict) else row[0]
    account_holder = row["account_holder"] if isinstance(row, dict) else row[1]
    account_no = (row["bank_account"] if isinstance(row, dict) else row[2]) or ""
    tail = account_no[-4:] if len(account_no) >= 4 else account_no
    account_no_masked = ("****" + tail) if tail else None
    return {
        "account": {
            "bank_name": bank_name,
            "account_holder": account_holder,
            "account_no_masked": account_no_masked,
        }
    }


# ============================================================
# 14-15. 推广中心
# ============================================================

@router.get("/promotion/qrcode", response_model=AgentPromotionQRResponse, dependencies=[Depends(require_signed_agreement)])
async def agent_promotion_qrcode(request: Request):
    """
    [boss r8 P0-1 修正] 推广码统一走 referral_codes 表(不是 user_wallets · 该列不存在)
    [boss r8 P0-2 修正] 推广链直指 /customer/recharge(带 query)· QR content URL-encoded
    """
    import shortuuid as _suuid
    from urllib.parse import quote as _urlquote
    a = _require_agent(request)
    # get-or-create referral_codes.code(W3 复用老推荐码表 · 不再用 user_wallets.referral_code)
    # [boss r9 P1] ON CONFLICT DO NOTHING RETURNING 并发时返空 · 必须 re-SELECT 库里真实 code
    # 防返一个未入库的本地 code · 导致客户端拿不到合法 invite_code
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT code FROM referral_codes WHERE user_id = %s", (a["user_id"],))
        row = cur.fetchone()
        code = _g(row, "code")
        if not code:
            new_code = f"OR-{_suuid.uuid()[:8].upper()}"
            cur.execute(
                "INSERT INTO referral_codes (user_id, code) VALUES (%s, %s) "
                "ON CONFLICT (user_id) DO NOTHING RETURNING code",
                (a["user_id"], new_code),
            )
            r2 = cur.fetchone()
            code = _g(r2, "code")
            if not code:
                # 并发场景:其他请求已 INSERT · ON CONFLICT 跳过 · RETURNING 为空
                # re-SELECT 拿库里真实 code · 防返未入库码
                cur.execute("SELECT code FROM referral_codes WHERE user_id = %s", (a["user_id"],))
                rrow = cur.fetchone()
                code = _g(rrow, "code")
                if not code:
                    # 极端 race(理论不发生)· 再 INSERT 一次
                    logger.error(f"[promotion/qrcode] referral_codes race · agent={a['user_id']} 兜底失败 · 用本地码")
                    code = new_code
            conn.commit()
    # 对外只给不透明短链；推广归因放在服务端元数据和 HttpOnly cookie 中，
    # 禁止把推荐码、账号编号或商业关系写进 URL。
    from api.share_api import _create_short_link

    from services.owned_image_policy import public_base_origin  # [WO_331 · 2026-10-03] 唯一出处
    public_origin = public_base_origin()
    short_code = _create_short_link(
        f"{public_origin}/login?mode=register",
        "register",
        int(a["user_id"]),
        {"referral_code": code},
    )
    ref_link = f"{public_origin}/api/sl/{short_code}"
    # [2026-05-30 bug5 修] 原 qr_url 指向不存在的 /api/qrcode(真实端点是 /api/share/qrcode 且参数是 url)→ 404 图片损坏
    # 改用 base64 data URI 自包含:img src 直接渲染 · 不依赖端点路径/认证(<img> 不带 JWT header)
    try:
        from api.share_api import _generate_qr_base64
        qr_url = _generate_qr_base64(ref_link)
    except Exception as _qe:
        logger.warning(f"[promotion/qrcode] base64 二维码生成失败 · 降级外部服务: {_qe}")
        qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=240x240&data={_urlquote(ref_link, safe='')}"
    return AgentPromotionQRResponse(qr_code_url=qr_url, ref_link=ref_link, invite_code=code)


@router.get("/promotion/customers", response_model=AgentPromotionCustomersResponse, dependencies=[Depends(require_signed_agreement)])
async def agent_promotion_customers(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    a = _require_agent(request)
    from services.client_purchase_gate import (
        override_from_db,
        resolve_online_purchase_permission,
    )

    with get_db() as conn:
        cur = conn.cursor()
        data = list_agent_customers(cur, a["user_id"], limit=limit, offset=offset)
        # [客户线上购买门控 2026-07-29] 列表行带上三态与实际生效结果。
        # 生效结果走同一个判定 SSOT,不在这里重算两级逻辑(防与客户侧漂移)。
        customer_ids = [int(d["customer_user_id"]) for d in data["items"]]
        overrides: dict[int, str] = {}
        if customer_ids:
            cur.execute(
                """SELECT customer_user_id, online_purchase_override
                   FROM customer_agent_bindings
                   WHERE customer_user_id = ANY(%s)""",
                (customer_ids,),
            )
            for row in cur.fetchall() or []:
                overrides[int(_g(row, "customer_user_id"))] = override_from_db(
                    _g(row, "online_purchase_override")
                )
        effective = {
            cid: bool(resolve_online_purchase_permission(cur, cid)["can_purchase_online"])
            for cid in customer_ids
        }
        cur.execute(
            "SELECT allow_client_online_purchase FROM users WHERE id = %s",
            (a["user_id"],),
        )
        _row = cur.fetchone()
        default_allow = True if _row is None else bool(_g(_row, "allow_client_online_purchase", True))

    items = []
    for d in data["items"]:
        cid = int(d["customer_user_id"])
        items.append(AgentPromotionCustomerItem(
            customer_user_id=cid,
            display_name=d.get("display_name"),
            phone_masked=d.get("phone_masked"),
            binding_source=d.get("binding_source", "unknown"),
            bound_at=d["bound_at"],
            dispute_status=d.get("dispute_status"),
            tool_credit=int(d.get("tool_credit", 0) or 0),
            publish_credit=int(d.get("publish_credit", 0) or 0),
            bonus_credit=int(d.get("bonus_credit", 0) or 0),
            online_purchase_override=overrides.get(cid, "inherit"),
            can_purchase_online=effective.get(cid, True),
        ))
    return AgentPromotionCustomersResponse(
        items=items,
        total=data["total"],
        default_allow_client_online_purchase=default_allow,
    )


# ============================================================
# [客户线上购买开关 2026-07-29] 服务商侧设置
#
# 两级开关:主账号默认 + 每客户三态覆盖。判定优先级"客户级 > 主账号默认"
# 单点实现在 services/client_purchase_gate.py,本层只负责读写和 RBAC。
#
# 文案边界:这里是**服务商侧**界面,可以直说"你的客户/线上购买/联系你办理";
# 客户侧只会看到 client_purchase_gate.BLOCKED_MESSAGE 那一句「联系您的推荐人」。
#
# RBAC:_require_agent 已把组织员工席位解析成经营主体(principal = 主账号),
# 所以这里的 a["user_id"] 天然就是主账号 —— 销售改不到别人的开关,
# 也不会有"销售层开关"这种东西存在。
# ============================================================

@router.get("/client-purchase/settings", response_model=AgentClientPurchaseSettingsResponse)
async def get_client_purchase_settings(request: Request):
    """读主账号默认:名下客户能否线上直接购买(默认开)。"""
    a = _require_agent(request)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT allow_client_online_purchase FROM users WHERE id = %s",
            (a["user_id"],),
        )
        row = cur.fetchone()
    allowed = True if row is None else bool(row["allow_client_online_purchase"])
    return AgentClientPurchaseSettingsResponse(allow_client_online_purchase=allowed)


@router.put("/client-purchase/settings", response_model=AgentClientPurchaseSettingsResponse)
async def set_client_purchase_settings(
    req: AgentClientPurchaseSettingsRequest, request: Request
):
    """切主账号默认。关闭后名下客户(未单独覆盖的)线上购买入口被拦。"""
    a = _require_agent(request)
    with get_db() as conn:
        cur = conn.cursor()
        # WHERE id=self · 防 IDOR(与 markup-ratio / cost-per-article 同形)
        cur.execute(
            "UPDATE users SET allow_client_online_purchase = %s WHERE id = %s",
            (bool(req.allow_client_online_purchase), a["user_id"]),
        )
        if cur.rowcount != 1:
            conn.rollback()
            raise HTTPException(404, "账号不存在")
        conn.commit()
    return AgentClientPurchaseSettingsResponse(
        allow_client_online_purchase=bool(req.allow_client_online_purchase)
    )


@router.put(
    "/client-purchase/customers/{customer_user_id}",
    response_model=AgentCustomerPurchaseOverrideResponse,
)
async def set_customer_purchase_override(
    customer_user_id: int,
    req: AgentCustomerPurchaseOverrideRequest,
    request: Request,
):
    """设单客户三态覆盖:inherit(跟随默认)/ allow(允许线上)/ offline_only(仅线下)。"""
    a = _require_agent(request)
    from services.client_purchase_gate import (
        OVERRIDE_CHOICES,
        override_from_db,
        override_to_db,
        resolve_online_purchase_permission,
    )

    try:
        db_value = override_to_db(req.override)
    except ValueError:
        raise HTTPException(400, f"override 必须是 {'/'.join(OVERRIDE_CHOICES)} 之一")

    with get_db() as conn:
        cur = conn.cursor()
        # 只能改**自己名下**客户的行:WHERE 同时锁客户与服务方,越权改别人客户
        # 直接 rowcount=0 → 404,不做二次查询绕过。
        # 服务方一侧同时认"主账号本人"与"主账号组织下的 active 销售席位",
        # 与 client_purchase_gate 的上溯口径一致 —— 绑定落在销售身上时,
        # 开关仍归主账号管,否则会出现"读得到却改不了"的死角。
        cur.execute(
            """UPDATE customer_agent_bindings
               SET online_purchase_override = %(override)s
               WHERE customer_user_id = %(customer_user_id)s
                 AND (
                     agent_user_id = %(principal)s
                     OR EXISTS (
                         SELECT 1
                         FROM organization_memberships m
                         JOIN organizations o ON o.id = m.organization_id
                         WHERE m.user_id = customer_agent_bindings.agent_user_id
                           AND m.status = 'active'
                           AND m.is_owner IS FALSE
                           AND o.status = 'active'
                           AND o.owner_user_id = %(principal)s
                     )
                 )""",
            {
                "override": db_value,
                "customer_user_id": int(customer_user_id),
                "principal": a["user_id"],
            },
        )
        if cur.rowcount != 1:
            conn.rollback()
            raise HTTPException(404, "该客户不在你名下")
        conn.commit()
        effective = resolve_online_purchase_permission(cur, int(customer_user_id))

    return AgentCustomerPurchaseOverrideResponse(
        customer_user_id=int(customer_user_id),
        online_purchase_override=override_from_db(db_value),
        can_purchase_online=bool(effective["can_purchase_online"]),
    )


# ============================================================
# helpers
# ============================================================

def _row_count(row) -> int:
    if row is None:
        return 0
    if isinstance(row, dict):
        return int(row.get("c", 0) or 0)
    return int(row[0] or 0)


def _g(row, key: str, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    return default


def _dict_row(row, keys: list) -> dict:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    return {k: row[i] for i, k in enumerate(keys) if i < len(row)}


def _mask_phone(phone: Optional[str]) -> Optional[str]:
    if not phone:
        return None
    if len(phone) >= 11:
        return phone[:3] + "****" + phone[-4:]
    return "***"


def _gen_referral_code(user_id: int) -> str:
    """user_id 确定性短码 fallback · base36 + secrets 后缀"""
    base = f"U{user_id:06d}"
    return base[:8].upper()
