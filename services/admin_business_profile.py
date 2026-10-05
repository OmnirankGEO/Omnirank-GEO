"""
Admin 用户经营档案聚合服务 (2026-06-05)

为 GET /api/admin/users/{user_id}/business-profile 聚合 7 段画像:
  overview / service_relation / pricing_config / clients_brands /
  wallet_billing / permissions_security / audit

只读聚合 · 不改任何扣费/价格锁/评分主链 · 全部 try/except 降级(单段失败不拖垮整体)。
归属判定铁律:
  - 服务商 = agent_level >= 1(user_wallets.agent_level)
  - 邀请来源 = referral_links level=1；只用于来源追踪
  - 当前商业服务商(upstream_provider) = customer_agent_bindings；绝不由 inviter 推断
  - downstream 只看 referral_links level=1(老板严禁多级返佣 · 绝不展开 level=2)

价格系数读取(仅 admin 端点返回 · 前台绝不暴露进货系数):
  - quote_markup:  admin override > 服务商自设(users.quote_markup_ratio) > 全局默认 1.0(§4.5/决策5 回成本)
  - purchase(进货): admin override(agent_pricing_overrides.wholesale_numer/denom) > 全局(pricing_config 默认 225/325)
  - sku_markup:    admin override(sku_markup_override) > 服务商自设(users.agent_sku_markup_ratio)

关联:
  - services/agent_pricing_overrides.py(per-agent override 读取)
  - services/agent_pricing.py / config/pricing_config.py(全局 wholesale 默认)
  - db/wallet_db.py get_wallet_balance · db/auth_db.py list_audit_logs
  - services/agent_agreement.py get_agreement_status(经营功能协议 v2.3)
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("GEO-AdminBizProfile")

DEFAULT_QUOTE_MARKUP = 1.0  # [§4.5/决策5 2026-06-06] 默认回成本·operator 自控(v1.3 markup 翻转 + 服务商批同口径)


def _row_get(row, key, idx=None):
    """RealDictCursor 行取值(dict 或 tuple 兼容)。"""
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    if idx is not None:
        try:
            return row[idx]
        except (IndexError, TypeError):
            return None
    return None


def _agent_level_of(cur, user_id: int) -> int:
    """读 user_wallets.agent_level(无钱包记录 → 0)。"""
    try:
        cur.execute("SELECT agent_level FROM user_wallets WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        lvl = _row_get(row, "agent_level", 0)
        return int(lvl) if lvl is not None else 0
    except Exception as exc:
        logger.warning("读 agent_level 失败 user=%s: %s", user_id, exc)
        return 0


# ============================================================
# 各段聚合
# ============================================================

def _build_overview(cur, user_id: int, base_user: Dict[str, Any]) -> Dict[str, Any]:
    agent_level = _agent_level_of(cur, user_id)
    referral_code = ""
    try:
        cur.execute("SELECT code FROM referral_codes WHERE user_id = %s", (user_id,))
        rc = cur.fetchone()
        referral_code = _row_get(rc, "code") or ""
    except Exception:
        referral_code = ""
    is_active_raw = base_user.get("is_active", 1)
    return {
        "id": user_id,
        "username": base_user.get("username"),
        "display_name": base_user.get("display_name"),
        "agent_level": agent_level,
        "referral_code": referral_code,
        "is_service_provider": agent_level >= 1,
        "created_at": base_user.get("created_at"),
        "is_active": bool(is_active_raw) if is_active_raw is not None else True,
        "is_admin": bool(base_user.get("is_admin")),
    }


def _build_service_relation(cur, user_id: int) -> Dict[str, Any]:
    """邀请来源、当前商业服务商、下游关系分别读取，不做跨概念推断。"""
    inviter = None
    # 1) 主源: referral_links level=1 (referred_id = user_id 的上家)
    try:
        cur.execute("""
            SELECT rl.referrer_id, u.display_name, u.username,
                   COALESCE(w.agent_level, 0) AS agent_level
            FROM referral_links rl
            JOIN users u ON u.id = rl.referrer_id
            LEFT JOIN user_wallets w ON w.user_id = rl.referrer_id
            WHERE rl.referred_id = %s AND rl.level = 1
            LIMIT 1
        """, (user_id,))
        row = cur.fetchone()
        if row:
            inviter = {
                "inviter_user_id": _row_get(row, "referrer_id"),
                "display_name": _row_get(row, "display_name") or _row_get(row, "username"),
                "agent_level": int(_row_get(row, "agent_level") or 0),
            }
    except Exception as exc:
        logger.warning("读 inviter(referral_links) 失败 user=%s: %s", user_id, exc)

    # 2) fallback: users.referred_by_agent_id
    if inviter is None:
        try:
            cur.execute("""
                SELECT u.id, u.display_name, u.username,
                       COALESCE(w.agent_level, 0) AS agent_level
                FROM users uu
                JOIN users u ON u.id = uu.referred_by_agent_id
                LEFT JOIN user_wallets w ON w.user_id = u.id
                WHERE uu.id = %s AND uu.referred_by_agent_id IS NOT NULL
                LIMIT 1
            """, (user_id,))
            row = cur.fetchone()
            if row:
                inviter = {
                    "inviter_user_id": _row_get(row, "id"),
                    "display_name": _row_get(row, "display_name") or _row_get(row, "username"),
                    "agent_level": int(_row_get(row, "agent_level") or 0),
                }
        except Exception as exc:
            logger.warning("读 inviter(referred_by_agent_id fallback) 失败 user=%s: %s", user_id, exc)

    # 当前商业服务商只认 customer_agent_bindings；绝不由 inviter/referral 推断。
    upstream_provider = None
    try:
        cur.execute("""
            SELECT cab.id AS binding_id, cab.agent_user_id, cab.binding_source, cab.bound_at,
                   u.display_name, u.username, COALESCE(w.agent_level, 0) AS agent_level
            FROM customer_agent_bindings cab
            JOIN users u ON u.id = cab.agent_user_id
            LEFT JOIN user_wallets w ON w.user_id = cab.agent_user_id
            WHERE cab.customer_user_id = %s
            LIMIT 1
        """, (user_id,))
        row = cur.fetchone()
        if row:
            upstream_provider = {
                "provider_user_id": _row_get(row, "agent_user_id"),
                # 旧 admin DTO 消费者兼容；字段只表示 provider，不再声称来自 inviter。
                "inviter_user_id": _row_get(row, "agent_user_id"),
                "display_name": _row_get(row, "display_name") or _row_get(row, "username"),
                "agent_level": int(_row_get(row, "agent_level") or 0),
                "binding_id": _row_get(row, "binding_id"),
                "binding_source": _row_get(row, "binding_source"),
                "bound_at": _row_get(row, "bound_at"),
            }
    except Exception as exc:
        logger.warning("读 current commercial binding 失败 user=%s: %s", user_id, exc)

    # downstream: referral_links level=1 列表 (referrer_id = user_id)
    downstream_referrals: List[Dict[str, Any]] = []
    try:
        cur.execute("""
            SELECT rl.referred_id, rl.created_at,
                   u.display_name, u.username,
                   COALESCE(w.agent_level, 0) AS agent_level,
                   COALESCE(w.total_recharged, 0) AS total_recharged
            FROM referral_links rl
            JOIN users u ON u.id = rl.referred_id
            LEFT JOIN user_wallets w ON w.user_id = rl.referred_id
            WHERE rl.referrer_id = %s AND rl.level = 1
            ORDER BY rl.created_at DESC
        """, (user_id,))
        for r in (cur.fetchall() or []):
            lvl = int(_row_get(r, "agent_level") or 0)
            downstream_referrals.append({
                "user_id": _row_get(r, "referred_id"),
                "display_name": _row_get(r, "display_name") or _row_get(r, "username"),
                "agent_level": lvl,
                "relation_type": "下级服务商" if lvl >= 1 else "下级普通用户",
                "total_recharged": int(_row_get(r, "total_recharged") or 0),
                "created_at": _row_get(r, "created_at"),
            })
    except Exception as exc:
        logger.warning("读 downstream referral_links 失败 user=%s: %s", user_id, exc)

    # customer_agent_bindings: agent_user_id = user_id 的客户
    bound_customers: List[Dict[str, Any]] = []
    try:
        cur.execute("""
            SELECT cab.customer_user_id, cab.binding_source, cab.bound_at,
                   u.display_name, u.username
            FROM customer_agent_bindings cab
            LEFT JOIN users u ON u.id = cab.customer_user_id
            WHERE cab.agent_user_id = %s
            ORDER BY cab.bound_at DESC
        """, (user_id,))
        for r in (cur.fetchall() or []):
            bound_customers.append({
                "customer_user_id": _row_get(r, "customer_user_id"),
                "display_name": _row_get(r, "display_name") or _row_get(r, "username"),
                "binding_source": _row_get(r, "binding_source"),
                "bound_at": _row_get(r, "bound_at"),
            })
    except Exception as exc:
        logger.warning("读 customer_agent_bindings 失败 user=%s: %s", user_id, exc)

    return {
        "inviter": inviter,
        "upstream_provider": upstream_provider,
        "downstream": {
            "referrals": downstream_referrals,
            "bound_customers": bound_customers,
        },
    }


def _build_pricing_config(cur, user_id: int) -> Dict[str, Any]:
    """仅 admin 端点返回 · 含进货系数(前台绝不暴露)。"""
    # 服务商自设值(users.quote_markup_ratio / agent_sku_markup_ratio)
    self_quote_markup = None
    self_sku_markup = None
    try:
        cur.execute(
            "SELECT quote_markup_ratio, agent_sku_markup_ratio FROM users WHERE id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        if row:
            q = _row_get(row, "quote_markup_ratio")
            s = _row_get(row, "agent_sku_markup_ratio")
            self_quote_markup = float(q) if q is not None else None
            self_sku_markup = float(s) if s is not None else None
    except Exception as exc:
        logger.warning("读 users 自设系数失败 user=%s: %s", user_id, exc)

    # admin override(agent_pricing_overrides)
    override = None
    try:
        from services.agent_pricing_overrides import get_agent_pricing_override
        override = get_agent_pricing_override(user_id)
    except Exception as exc:
        logger.warning("读 agent_pricing_overrides 失败 user=%s: %s", user_id, exc)

    ov_quote = None
    ov_sku = None
    ov_numer = None
    ov_denom = None
    if override:
        ov_quote = override.get("quote_markup_override")
        ov_sku = override.get("sku_markup_override")
        ov_numer = override.get("wholesale_numer")
        ov_denom = override.get("wholesale_denom")

    # ---- quote_markup effective: admin override > 自设 > 默认 2.0 ----
    if ov_quote is not None:
        quote_effective, quote_source = float(ov_quote), "admin_override"
    elif self_quote_markup is not None:
        quote_effective, quote_source = self_quote_markup, "agent_self"
    else:
        quote_effective, quote_source = DEFAULT_QUOTE_MARKUP, "default"

    # ---- purchase 进货系数: admin override(numer/denom 都非空) > 全局默认 ----
    glob_numer, glob_denom = 225, 325
    catalog_version = ""
    try:
        from config.pricing_config import (
            get_agent_purchase_catalog_version,
            get_pricing_config,
            get_wholesale_ratio,
        )
        glob_numer, glob_denom = get_wholesale_ratio()
        catalog_version = get_agent_purchase_catalog_version(get_pricing_config())
    except Exception as exc:
        logger.warning("读全局 wholesale_ratio 失败(回落 225/325): %s", exc)
    if ov_numer and ov_denom:
        p_numer, p_denom, p_source = int(ov_numer), int(ov_denom), "admin_override"
    else:
        p_numer, p_denom, p_source = int(glob_numer), int(glob_denom), "global_default"
    p_effective_ratio = (p_numer / p_denom) if p_denom else None

    # ---- sku_markup: admin override > 自设 ----
    if ov_sku is not None:
        sku_effective, sku_source = float(ov_sku), "admin_override"
    elif self_sku_markup is not None:
        sku_effective, sku_source = self_sku_markup, "agent_self"
    else:
        sku_effective, sku_source = None, "unset"

    return {
        "quote_markup": {
            "effective": quote_effective,
            "source": quote_source,
            "override": float(ov_quote) if ov_quote is not None else None,
            "self": self_quote_markup,
            "default": DEFAULT_QUOTE_MARKUP,
        },
        "purchase_pricing": {
            "catalog_version": catalog_version,
            "wholesale_numer": p_numer,
            "wholesale_denom": p_denom,
            "effective_ratio": p_effective_ratio,
            "source": p_source,
            "global_numer": int(glob_numer),
            "global_denom": int(glob_denom),
        },
        "sku_markup": {
            "effective": sku_effective,
            "source": sku_source,
            "override": float(ov_sku) if ov_sku is not None else None,
            "self": self_sku_markup,
        },
    }


def _build_channel_tier(cur, user_id: int) -> Dict[str, Any]:
    try:
        from services.channel_tier import get_agent_channel_tier_state

        state = get_agent_channel_tier_state(cur, user_id)
        return {
            "enabled": bool(state.get("enabled")),
            "agent_user_id": state.get("agent_user_id") or user_id,
            "natural_tier": state.get("natural_tier") or "none",
            "channel_tier": state.get("channel_tier") or "none",
            "effective_tier": state.get("effective_tier") or "none",
            "override_active": bool(state.get("override_active")),
            "tier_override": state.get("tier_override"),
            "tier_override_until": state.get("tier_override_until"),
            "rolling_12m_yuan": float(state.get("rolling_12m_yuan") or 0),
            "next_tier": state.get("next_tier"),
            "next_threshold_yuan": state.get("next_threshold_yuan"),
            "gap_to_next_yuan": float(state.get("gap_to_next_yuan") or 0),
            "bonus_rate": float(state.get("bonus_rate") or 0),
            "is_founder": bool(state.get("is_founder")),
            "founder_rank": state.get("founder_rank"),
            "first_order_done": bool(state.get("first_order_done")),
            "tier_effective_at": state.get("tier_effective_at"),
            "last_evaluated_at": state.get("last_evaluated_at"),
            "updated_at": state.get("updated_at"),
        }
    except Exception as exc:
        logger.warning("读渠道等级失败 user=%s: %s", user_id, exc)
        return {
            "enabled": False,
            "agent_user_id": user_id,
            "natural_tier": "none",
            "channel_tier": "none",
            "effective_tier": "none",
            "override_active": False,
            "rolling_12m_yuan": 0,
            "next_tier": None,
            "next_threshold_yuan": None,
            "gap_to_next_yuan": 0,
            "bonus_rate": 0,
            "is_founder": False,
            "founder_rank": None,
            "first_order_done": False,
        }


def _build_clients_brands(cur, user_id: int) -> Dict[str, Any]:
    customer_count = 0
    customers: List[Dict[str, Any]] = []
    try:
        cur.execute("""
            SELECT cab.customer_user_id, cab.binding_source, cab.bound_at,
                   u.display_name, u.username
            FROM customer_agent_bindings cab
            LEFT JOIN users u ON u.id = cab.customer_user_id
            WHERE cab.agent_user_id = %s
            ORDER BY cab.bound_at DESC
        """, (user_id,))
        for r in (cur.fetchall() or []):
            customers.append({
                "customer_user_id": _row_get(r, "customer_user_id"),
                "display_name": _row_get(r, "display_name") or _row_get(r, "username"),
                "binding_source": _row_get(r, "binding_source"),
                "bound_at": _row_get(r, "bound_at"),
            })
        customer_count = len(customers)
    except Exception as exc:
        logger.warning("读 clients(customer_agent_bindings) 失败 user=%s: %s", user_id, exc)

    brand_count = 0
    brands: List[Dict[str, Any]] = []
    try:
        cur.execute("""
            SELECT id, name, industry, created_at
            FROM brands
            WHERE owner_user_id = %s AND (is_deleted IS NULL OR is_deleted = FALSE)
            ORDER BY created_at DESC
        """, (user_id,))
        for r in (cur.fetchall() or []):
            brands.append({
                "id": _row_get(r, "id"),
                "name": _row_get(r, "name"),
                "industry": _row_get(r, "industry"),
                "created_at": _row_get(r, "created_at"),
            })
        brand_count = len(brands)
    except Exception as exc:
        logger.warning("读 brands 失败 user=%s: %s", user_id, exc)

    return {
        "customer_count": customer_count,
        "brand_count": brand_count,
        "brands": brands,
        "customers": customers,
    }


def _build_wallet_billing(cur, user_id: int) -> Dict[str, Any]:
    balance = {}
    try:
        from db.wallet_db import get_wallet_balance
        balance = get_wallet_balance(user_id) or {}
    except Exception as exc:
        logger.warning("get_wallet_balance 失败 user=%s: %s", user_id, exc)

    # 消耗汇总: paid 轨真实消费(type='consume' AND point_type='paid')
    paid_consumed = 0
    try:
        cur.execute("""
            SELECT COALESCE(SUM(ABS(amount)), 0) AS consumed
            FROM point_transactions
            WHERE user_id = %s AND type = 'consume' AND point_type = 'paid'
        """, (user_id,))
        paid_consumed = int(_row_get(cur.fetchone(), "consumed") or 0)
    except Exception as exc:
        logger.warning("读 paid 消耗汇总失败 user=%s: %s", user_id, exc)

    # 服务收益: commission_points 余额 + agent_revenue_ledger settled 汇总(若表存在)
    settled_revenue_cents = 0
    try:
        cur.execute("""
            SELECT COALESCE(SUM(agent_settlement_cents), 0) AS settled
            FROM agent_revenue_ledger
            WHERE agent_user_id = %s AND status = 'settled'
        """, (user_id,))
        settled_revenue_cents = int(_row_get(cur.fetchone(), "settled") or 0)
    except Exception as exc:
        logger.warning("读 agent_revenue_ledger settled 失败(可能表未迁移·回落 0) user=%s: %s", user_id, exc)

    # [单账本收敛 2026-07-27] 原注释写"user_wallets.paid_points 会被迁出,真实可用额度在这里"
    # —— 那正是两本账的说法。单账本后【真实可用额度就在 user_wallets】,
    # 本块保留只是为了让后台仍能看到历史信用钱包快照(迁移后各池均为 0)。
    # total_remaining_points 迁移后恒 0 属预期;客户真实余额看 paid_points/bonus_points。
    raw_credit = balance.get("customer_credit") or {}
    customer_credit = {
        "agent_user_id": raw_credit.get("agent_user_id"),
        "tool_credit_points": int(raw_credit.get("tool_credit_points") or 0),
        "publish_credit_points": int(raw_credit.get("publish_credit_points") or 0),
        "bonus_credit_points": int(raw_credit.get("bonus_credit_points") or 0),
        "total_purchased_points": int(raw_credit.get("total_purchased_points") or 0),
        "total_consumed_points": int(raw_credit.get("total_consumed_points") or 0),
    }
    customer_credit["total_remaining_points"] = (
        customer_credit["tool_credit_points"]
        + customer_credit["publish_credit_points"]
        + customer_credit["bonus_credit_points"]
    )

    customer_credit_transactions: List[Dict[str, Any]] = []
    try:
        cur.execute("""
            SELECT c.id, c.agent_user_id, u.display_name AS agent_display_name,
                   c.type, c.pool, c.points,
                   c.balance_tool_after, c.balance_publish_after, c.balance_bonus_after,
                   c.feature_code, c.related_order_id, c.source, c.description, c.created_at
            FROM customer_credit_transactions c
            LEFT JOIN users u ON u.id = c.agent_user_id
            WHERE c.customer_user_id = %s
            ORDER BY c.created_at DESC, c.id DESC
            LIMIT 30
        """, (user_id,))
        for r in (cur.fetchall() or []):
            customer_credit_transactions.append({
                "id": _row_get(r, "id"),
                "agent_user_id": _row_get(r, "agent_user_id"),
                "agent_display_name": _row_get(r, "agent_display_name"),
                "type": _row_get(r, "type"),
                "pool": _row_get(r, "pool"),
                "points": int(_row_get(r, "points") or 0),
                "balance_tool_after": int(_row_get(r, "balance_tool_after") or 0),
                "balance_publish_after": int(_row_get(r, "balance_publish_after") or 0),
                "balance_bonus_after": int(_row_get(r, "balance_bonus_after") or 0),
                "feature_code": _row_get(r, "feature_code"),
                "related_order_id": _row_get(r, "related_order_id"),
                "source": _row_get(r, "source"),
                "description": _row_get(r, "description"),
                "created_at": _row_get(r, "created_at"),
            })
    except Exception as exc:
        logger.warning("读 customer_credit_transactions 失败(可能表未迁移·回落空) user=%s: %s", user_id, exc)

    return {
        "balance": balance,
        "paid_consumed_points": paid_consumed,
        "customer_credit": customer_credit,
        "customer_credit_transactions": customer_credit_transactions,
        "service_revenue": {
            "commission_points": int(balance.get("commission_points") or 0),
            "settled_revenue_cents": settled_revenue_cents,
        },
    }


def _build_permissions_security(cur, user_id: int, base_user: Dict[str, Any]) -> Dict[str, Any]:
    is_active_raw = base_user.get("is_active", 1)

    # 经营功能协议 v2.3(get_agreement_status)
    agreement_factory = {"status": "unknown", "signed": False}
    try:
        from services.agent_agreement import get_agreement_status
        st = get_agreement_status(cur, user_id)
        agreement_factory = {
            "version": st.get("version"),
            "status": st.get("status"),
            "signed": st.get("status") == "signed",
            "signed_at": st.get("signed_at"),
        }
    except Exception as exc:
        logger.warning("读经营功能协议状态失败 user=%s: %s", user_id, exc)

    # 申请审核制协议(partner_agreements / agent_agreements 是否签)
    apply_signed = False
    apply_version = None
    apply_signed_at = None
    for tbl in ("partner_agreements", "agent_agreements"):
        try:
            cur.execute(
                f"SELECT version, signed_at FROM {tbl} WHERE user_id = %s ORDER BY signed_at DESC LIMIT 1",
                (user_id,),
            )
            row = cur.fetchone()
            if row:
                apply_signed = True
                apply_version = _row_get(row, "version")
                apply_signed_at = _row_get(row, "signed_at")
                break
        except Exception:
            # 表不存在 / 列不同 → 跳过尝试下一个
            continue

    return {
        "roles": base_user.get("roles", []),
        "is_active": bool(is_active_raw) if is_active_raw is not None else True,
        "agreement_factory": agreement_factory,
        "agreement_apply": {
            "signed": apply_signed,
            "version": apply_version,
            "signed_at": apply_signed_at,
        },
    }


def _build_audit(user_id: int) -> Dict[str, Any]:
    """该用户相关操作日志:① 该用户作为操作人(user_id 列)② admin 针对该用户(entity_type='user' AND entity_id=user_id)的改动。
    修:原 list_audit_logs(user_id=) 只过滤操作人列,admin 改归属/系数记的是 admin_id → 该用户档案里看不到本屏刚做的变更;改 OR entity 过滤。"""
    try:
        import json as _json
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, user_id, username, action, module, entity_type, entity_id,
                       summary, before_snapshot, after_snapshot, ip_address, created_at
                FROM audit_logs
                WHERE user_id = %s OR (entity_type = 'user' AND entity_id = %s)
                ORDER BY created_at DESC LIMIT 20
                """,
                (user_id, user_id),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        logs: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r) if isinstance(r, dict) else dict(zip(
                ("id", "user_id", "username", "action", "module", "entity_type",
                 "entity_id", "summary", "before_snapshot", "after_snapshot",
                 "ip_address", "created_at"), r))
            for k in ("before_snapshot", "after_snapshot"):
                v = d.get(k)
                if isinstance(v, str) and v:
                    try:
                        d[k] = _json.loads(v)
                    except Exception:
                        pass
            if d.get("created_at") and hasattr(d["created_at"], "isoformat"):
                d["created_at"] = d["created_at"].isoformat()
            logs.append(d)
        return {"total": len(logs), "logs": logs, "page": 1, "page_size": 20}
    except Exception as exc:
        logger.warning("读 audit_logs 失败 user=%s: %s", user_id, exc)
        return {"total": 0, "logs": [], "page": 1, "page_size": 20}


# ============================================================
# 主入口
# ============================================================

def build_business_profile(user_id: int, base_user: Dict[str, Any]) -> Dict[str, Any]:
    """聚合 7 段经营档案。base_user 为 db.auth_db.get_user(user_id) 的返回(含 roles/is_admin)。"""
    from db.connection import get_connection
    conn = get_connection()
    _prev_ac = getattr(conn, "autocommit", False)
    try:
        # [复检 med] 纯只读聚合 · autocommit=True 让每段 SELECT 独立 · 单段 DB 异常(如 agent_revenue_ledger 表未迁移)不污染后续段(防 aborted txn 后健康段静默返空)
        try:
            conn.autocommit = True
        except Exception:
            pass
        cur = conn.cursor()
        overview = _build_overview(cur, user_id, base_user)
        service_relation = _build_service_relation(cur, user_id)
        pricing_config = _build_pricing_config(cur, user_id)
        channel_tier = _build_channel_tier(cur, user_id)
        clients_brands = _build_clients_brands(cur, user_id)
        wallet_billing = _build_wallet_billing(cur, user_id)
        permissions_security = _build_permissions_security(cur, user_id, base_user)
    finally:
        try:
            conn.autocommit = _prev_ac
        except Exception:
            pass
        conn.close()

    audit = _build_audit(user_id)

    return {
        "overview": overview,
        "service_relation": service_relation,
        "pricing_config": pricing_config,
        "channel_tier": channel_tier,
        "clients_brands": clients_brands,
        "wallet_billing": wallet_billing,
        "permissions_security": permissions_security,
        "audit": audit,
    }


# ============================================================
# 接口2 辅助: 防循环检测(沿 referral_links level=1 向上追)
# ============================================================

def detect_referral_cycle(cur, ancestor_start_id: int, target_user_id: int, max_depth: int = 50) -> bool:
    """从 ancestor_start_id 沿 referral_links level=1 向上追其 referrer 链,
    若链上遇到 target_user_id 则返回 True(会形成循环 · 拒绝)。
    自身相等也算(ancestor == target)。max_depth 防脏数据死循环。
    """
    if ancestor_start_id == target_user_id:
        return True
    current = ancestor_start_id
    seen = {current}
    depth = 0
    while depth < max_depth:
        try:
            cur.execute(
                "SELECT referrer_id FROM referral_links WHERE referred_id = %s AND level = 1 LIMIT 1",
                (current,),
            )
        except Exception as exc:
            # [P2 fail-closed] 关系变更是关键后台操作,防循环校验查询失败必须阻断(不放行),由调用方转 400
            logger.warning("防循环追溯查询失败 current=%s: %s · fail-closed 阻断", current, exc)
            raise
        row = cur.fetchone()
        parent = _row_get(row, "referrer_id")
        if parent is None:
            return False
        if parent == target_user_id:
            return True
        if parent in seen:
            # 既有数据已成环 · 不再追(避免死循环)· 但与 target 无关则放行
            return False
        seen.add(parent)
        current = parent
        depth += 1
    return False
