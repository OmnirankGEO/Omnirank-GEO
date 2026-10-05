"""
D2-b 代理自定返利 · 2026-05-30 · GEO CTO-15.23

返利从代理 bonus_inventory 出(代理成本 · 丰俭由人)· 取代废除的平台固定返利(15%/28%/5%)。
守恒:复用 allocate_offline(扣代理 bonus) + allocate_credit(给客户 bonus)· 净变化 0 · inventory_audit 不破。
不阻断主充值:disabled/库存不足跳过 + SAVEPOINT 隔离返利异常(只回滚返利·主充值保留)。

设计 + 守恒证明:docs/AI-CONTEXT/AGENT_REBATE_D2B_DESIGN_2026-05-30.md
memory feedback_agent_referral_self_set_from_inventory
"""
import logging
import math
from typing import Dict, Any, Optional

logger = logging.getLogger("GEO-AgentRebate")


def get_rebate_config(cursor, agent_user_id: int) -> Dict[str, Any]:
    """读代理返利规则 · 无配置返默认(关闭)"""
    cursor.execute(
        "SELECT enabled, rebate_rate, max_rebate_points_per_order "
        "FROM agent_rebate_config WHERE agent_user_id = %s",
        (agent_user_id,),
    )
    row = cursor.fetchone()
    if not row:
        return {"enabled": False, "rebate_rate": 0.0, "max_rebate_points_per_order": None}
    if isinstance(row, dict):
        return {
            "enabled": bool(row["enabled"]),
            "rebate_rate": float(row["rebate_rate"] or 0),
            "max_rebate_points_per_order": row["max_rebate_points_per_order"],
        }
    return {"enabled": bool(row[0]), "rebate_rate": float(row[1] or 0),
            "max_rebate_points_per_order": row[2]}


def upsert_rebate_config(cursor, agent_user_id: int, enabled: bool,
                         rebate_rate: float, max_per_order: Optional[int] = None) -> Dict[str, Any]:
    """代理设返利规则 · rate 护栏 0~1.0 · 单笔上限非负"""
    if not math.isfinite(rebate_rate) or rebate_rate < 0 or rebate_rate > 1.0:
        raise ValueError("返利比例须在 0 ~ 100% 之间(且为有效数值)")
    if max_per_order is not None and max_per_order < 0:
        raise ValueError("单笔返利上限不能为负")
    cursor.execute("""
        INSERT INTO agent_rebate_config
            (agent_user_id, enabled, rebate_rate, max_rebate_points_per_order, updated_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (agent_user_id) DO UPDATE SET
            enabled = EXCLUDED.enabled,
            rebate_rate = EXCLUDED.rebate_rate,
            max_rebate_points_per_order = EXCLUDED.max_rebate_points_per_order,
            updated_at = NOW()
    """, (agent_user_id, bool(enabled), round(float(rebate_rate), 4), max_per_order))
    return {"enabled": bool(enabled), "rebate_rate": round(float(rebate_rate), 4),
            "max_rebate_points_per_order": max_per_order}


def apply_rebate_on_settlement(cursor, agent_user_id: int, customer_user_id: int,
                               base_points: int, related_order_id: Optional[str]) -> Dict[str, Any]:
    """
    结算时调(_record_factory_settlement 内 · 主事务 cursor)。
    按代理返利规则从代理 bonus_inventory 额外返 bonus 给客户。

    守恒:agent bonus_inventory −rebate = customer bonus_credit +rebate(净 0 · audit 不破)。
    不阻断主充值:
      - disabled / rate=0 / 库存不足 → 返回不动(主额度已正常入账)
      - 返利执行异常 → SAVEPOINT 回滚返利(主充值保留)+ 日志
    """
    cfg = get_rebate_config(cursor, agent_user_id)
    if not cfg["enabled"] or cfg["rebate_rate"] <= 0:
        return {"rebated_points": 0, "status": "disabled"}

    # 幂等:同 order 已返过则跳过(防支付二次回调重复返利 · P1)
    # 🔴 [单账本收敛 2026-07-27] 幂等键从 customer_credit_transactions 搬到 point_transactions。
    #    原来幂等标记由下方 allocate_credit 写进信用流水表 —— 那张表停写后本查询会【恒为空】,
    #    幂等直接失效 → 支付二次回调会重复返利(给服务商多发钱)。这是拆除时最危险的一条,
    #    必须与停写同批改,不能先停写后搬(判定表 A2)。
    if related_order_id:
        cursor.execute(
            "SELECT 1 FROM point_transactions "
            "WHERE order_id = %s AND type = 'agent_grant' AND description LIKE %s LIMIT 1",
            (related_order_id, "%[agent_rebate]%"),
        )
        if cursor.fetchone():
            logger.info(f"[Rebate] order={related_order_id} 已返过 · 跳过(幂等防二次回调)")
            return {"rebated_points": 0, "status": "already_rebated"}

    rate = cfg["rebate_rate"]
    rebate = int(round(int(base_points or 0) * rate))
    cap = cfg.get("max_rebate_points_per_order")
    if cap is not None and rebate > int(cap):
        rebate = int(cap)
    if rebate <= 0:
        return {"rebated_points": 0, "status": "zero"}

    from services.agent_inventory import allocate_offline, get_or_create_inventory_wallet
    from services.customer_entitlement import grant_to_customer

    # 库存检查(bonus 池)· 不够跳过整笔(不阻断主充值)
    wallet = get_or_create_inventory_wallet(cursor, agent_user_id)
    if wallet["bonus_inventory_points"] < rebate:
        logger.warning(
            f"[Rebate] agent={agent_user_id} bonus 库存不足 需={rebate} 有={wallet['bonus_inventory_points']} "
            f"· 跳过返利(不阻断主充值) order={related_order_id}"
        )
        return {"rebated_points": 0, "status": "insufficient_inventory", "needed": rebate}

    # SAVEPOINT 隔离:返利异常只回滚返利,主充值保留 + 守恒(allocate_offline+credit 要么都成要么都不做)
    cursor.execute("SAVEPOINT agent_rebate_sp")
    try:
        allocate_offline(
            cursor, agent_user_id=agent_user_id, customer_user_id=customer_user_id,
            paid_points=0, bonus_points=rebate,
            description=f"自定返利 {rate * 100:.1f}% · order={related_order_id}",
        )
        # [单账本收敛 2026-07-27] 返利落客户 user_wallets.bonus_points(赠送算力),
        # 幂等标记随之写进 point_transactions(见上方幂等查询,两处必须同源)。
        grant_to_customer(
            cursor, customer_user_id=customer_user_id, agent_user_id=agent_user_id,
            paid_points=0, bonus_points=rebate,
            related_order_id=related_order_id, source="agent_rebate",
            description=f"返利 {rate * 100:.1f}%",
        )
        cursor.execute("RELEASE SAVEPOINT agent_rebate_sp")
    except Exception as e:
        cursor.execute("ROLLBACK TO SAVEPOINT agent_rebate_sp")
        logger.warning(
            f"[Rebate] agent={agent_user_id} customer={customer_user_id} "
            f"返利执行异常(回滚返利·主充值保留): {e}"
        )
        return {"rebated_points": 0, "status": "error", "error": str(e)}

    logger.info(
        f"[Rebate] agent={agent_user_id} customer={customer_user_id} 返利 {rebate} bonus "
        f"(rate={rate}) order={related_order_id}"
    )
    return {"rebated_points": rebate, "status": "applied", "rate": rate}
