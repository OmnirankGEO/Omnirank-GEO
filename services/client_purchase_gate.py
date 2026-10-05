"""客户线上购买门控 · 判定 SSOT(2026-07-29)

工单:docs/AI-CONTEXT/WORKORDER_CLIENT_PURCHASE_GATE_2026-07-27.md

商业口径(玩法 B 产品化):
  服务商线下收客户的钱、用自己账号帮客户操作。客户线上直充会把关系变成玩法 A
  (平台收款 + 佣金)。这个开关让服务商自选玩法。不改任何资金流、不动计费/退款,
  纯购买入口门控。

白标铁律(对客户可见的一切文案):
  客户面前**不得出现**服务商名称/公司名/账号名,也不得出现"服务商/代理/主账号/
  销售"等内部角色词。统一称呼定死为「推荐人」(见 BLOCKED_MESSAGE)。
  本模块对外返回的 payload **只有布尔值和固定文案**,绝不回传 provider_user_id
  之类的归属主体标识 —— 那等于把服务商身份泄给客户。

归属链(全部在树里核实过真实 schema · 2026-07-29,未臆造):
  - 服务商身份 = ``user_wallets.agent_level >= 1``
  - 客户 → 服务商的商业归属 SSOT = ``customer_agent_bindings``
    (services/commercial_service_routing.py 模块级铁律:"Only customer_agent_bindings
     is the ordinary-customer commercial SSOT",referral 只是来源证据、绝不用于推断
     商业归属 —— 所以本模块**不读** referral_links / users.referred_by_agent_id)
  - 销售(子账号)= ``organization_memberships`` 里 ``is_owner = FALSE`` 的 active 成员;
    主账号 = ``organizations.owner_user_id``。绑定落在销售身上时上溯到主账号读开关。

判定优先级(Owner 2026-07-27 补拍板):
  客户级 ``customer_agent_bindings.online_purchase_override`` 三态
    (NULL 跟随 / TRUE 允许 / FALSE 仅线下)
  > 主账号 ``users.allow_client_online_purchase``(默认 TRUE)

fail-open 铁律:
  找不到唯一主账号(无绑定 / 多条绑定 / 自注册无推荐人)、或读库出任何异常 →
  一律**视为开(不拦)**。这是资金入口,判定层自己出问题绝不能把用户的付款路堵死。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger("GEO-ClientPurchaseGate")

# 被拦时对**客户**可见的口径:错误码 + 文案。全站单点,前后端同一份。
BLOCKED_CODE = "ONLINE_PURCHASE_BY_REFERRER_ONLY"
BLOCKED_MESSAGE = "如需充值或购买，请联系您的推荐人办理"

# 三态覆盖的对外取值(服务商侧 API 用;客户侧永远看不到)。
OVERRIDE_INHERIT = "inherit"
OVERRIDE_ALLOW = "allow"
OVERRIDE_OFFLINE_ONLY = "offline_only"
OVERRIDE_CHOICES = (OVERRIDE_INHERIT, OVERRIDE_ALLOW, OVERRIDE_OFFLINE_ONLY)


def override_to_db(choice: str) -> Optional[bool]:
    """三态字符串 → DB 列值(NULL / TRUE / FALSE)。"""
    if choice == OVERRIDE_INHERIT:
        return None
    if choice == OVERRIDE_ALLOW:
        return True
    if choice == OVERRIDE_OFFLINE_ONLY:
        return False
    raise ValueError(f"online_purchase_override 必须在 {OVERRIDE_CHOICES} · 实际 {choice!r}")


def override_from_db(value: Optional[bool]) -> str:
    """DB 列值 → 三态字符串。"""
    if value is None:
        return OVERRIDE_INHERIT
    return OVERRIDE_ALLOW if value else OVERRIDE_OFFLINE_ONLY


def _row_get(row: Any, key: str, default=None):
    """RealDictCursor 行取值(dict / tuple 兼容)。"""
    if row is None:
        return default
    if isinstance(row, Mapping):
        return row.get(key, default)
    return default


def _allow(reason: str, **extra) -> Dict[str, Any]:
    result: Dict[str, Any] = {"can_purchase_online": True, "reason": reason}
    result.update(extra)
    return result


def _resolve_primary_account(cursor, agent_user_id: int) -> int:
    """把绑定上的服务方上溯到**主账号**。

    销售(子账号)= organization_memberships 里 is_owner=FALSE 的 active 成员;
    此时开关读组织 owner。绑定本身就是 owner、或该账号不属于任何 active 组织 →
    它自己就是主账号。多个 active 组织(不应发生)取 organization_id 最小的那个,
    保证判定确定性。
    """
    cursor.execute(
        """
        SELECT o.owner_user_id
        FROM organization_memberships m
        JOIN organizations o ON o.id = m.organization_id
        WHERE m.user_id = %s
          AND m.status = 'active'
          AND m.is_owner IS FALSE
          AND o.status = 'active'
        ORDER BY o.id
        LIMIT 1
        """,
        (int(agent_user_id),),
    )
    row = cursor.fetchone()
    owner_user_id = _row_get(row, "owner_user_id")
    if owner_user_id is None:
        return int(agent_user_id)
    return int(owner_user_id)


def resolve_online_purchase_permission(cursor, user_id: int) -> Dict[str, Any]:
    """判定该用户能否线上直接购买。

    返回 ``{"can_purchase_online": bool, "reason": str}``。
    ``reason`` 只进服务端日志和服务商侧接口,**绝不**下发给客户。
    """
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return _allow("invalid_user_id")
    if uid <= 0:
        return _allow("invalid_user_id")

    try:
        # 1) admin 与服务商本人永不受门控(工单 §1-4:不影响服务商及销售自己的购买)。
        cursor.execute(
            """
            SELECT
                COALESCE(
                    (SELECT uw.agent_level FROM user_wallets uw WHERE uw.user_id = %(uid)s), 0
                ) AS agent_level,
                EXISTS(
                    SELECT 1 FROM user_roles ur JOIN roles r ON r.id = ur.role_id
                    WHERE ur.user_id = %(uid)s AND r.name = 'admin'
                ) AS is_admin,
                EXISTS(
                    SELECT 1 FROM organization_memberships m
                    WHERE m.user_id = %(uid)s AND m.status = 'active'
                ) AS is_org_member
""",
            {"uid": uid},
        )
        identity = cursor.fetchone()
        if bool(_row_get(identity, "is_admin")):
            return _allow("admin")
        if int(_row_get(identity, "agent_level", 0) or 0) >= 1:
            return _allow("service_provider_self")
        # 销售(组织内部席位)自己买不受名下客户门控约束。
        if bool(_row_get(identity, "is_org_member")):
            return _allow("organization_member_self")

        # 2) 归属链:只认 customer_agent_bindings(商业归属 SSOT)。
        #    无绑定 / 多条绑定 → 找不到唯一主账号 → 不拦。
        cursor.execute(
            """
            SELECT agent_user_id, online_purchase_override
            FROM customer_agent_bindings
            WHERE customer_user_id = %s
            ORDER BY id
            """,
            (uid,),
        )
        rows = cursor.fetchall() or []
        if len(rows) != 1:
            return _allow("no_unique_provider")

        binding = rows[0]
        agent_user_id = _row_get(binding, "agent_user_id")
        if agent_user_id is None:
            return _allow("no_unique_provider")
        override = _row_get(binding, "online_purchase_override")

        # 3) 客户级三态优先于主账号默认。
        if override is True:
            return _allow("customer_override_allow")
        if override is False:
            return {
                "can_purchase_online": False,
                "reason": "customer_override_offline_only",
            }

        # 4) 跟随默认 → 上溯主账号后读主账号开关(绝不读销售层)。
        primary_user_id = _resolve_primary_account(cursor, int(agent_user_id))
        cursor.execute(
            "SELECT allow_client_online_purchase FROM users WHERE id = %s",
            (primary_user_id,),
        )
        row = cursor.fetchone()
        allowed = _row_get(row, "allow_client_online_purchase")
        if allowed is None:
            # 主账号不存在或列读不到 → 不拦。
            return _allow("provider_default_unavailable")
        if bool(allowed):
            return _allow("provider_default_allow")
        return {
            "can_purchase_online": False,
            "reason": "provider_default_offline_only",
        }
    except Exception as exc:  # noqa: BLE001 — 资金入口,判定失败一律放行
        logger.warning("[purchase-gate] 判定失败 user=%s · fail-open 不拦: %s", uid, exc)
        return _allow("gate_unavailable")


def can_purchase_online(user_id: int, *, cursor=None) -> bool:
    """便捷布尔读法;不传 cursor 时自开连接。"""
    if cursor is not None:
        return bool(resolve_online_purchase_permission(cursor, user_id)["can_purchase_online"])
    try:
        from db.connection import get_db

        with get_db() as conn:
            return bool(
                resolve_online_purchase_permission(conn.cursor(), user_id)["can_purchase_online"]
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[purchase-gate] 连接失败 user=%s · fail-open 不拦: %s", user_id, exc)
        return True


def blocked_http_detail() -> Dict[str, str]:
    """被拦时的 HTTP detail(客户可见)· 零内部术语零服务商信息。"""
    return {"code": BLOCKED_CODE, "message": BLOCKED_MESSAGE}


def require_online_purchase_allowed(user_id: int, *, cursor=None) -> None:
    """支付发起端点统一守卫:被拦则 403 + 同口径 code/文案。"""
    if can_purchase_online(user_id, cursor=cursor):
        return
    from fastapi import HTTPException

    raise HTTPException(status_code=403, detail=blocked_http_detail())
