"""
V3.5 工厂模式 · 线下划拨原子操作

[Codex r2 P1-3] 封装代理后台手动划拨给客户的完整操作
单事务原子:扣代理库存 + 客户 wallet 入账 + 双流水

防"代理库存扣了 · 客户没拿到额度"或反向不一致

调用方:api/agent_api.py 等代理后台 endpoint
"""

import logging
from typing import Dict, Any

from services.agent_inventory import allocate_offline as _inventory_allocate_offline
from services.customer_entitlement import grant_to_customer as _customer_grant

logger = logging.getLogger("GEO-V35-OfflineAllocation")


def allocate_offline_to_customer(
    cursor,
    agent_user_id: int,
    customer_user_id: int,
    tool_points: int = 0,
    publish_points: int = 0,
    bonus_points: int = 0,
    description: str = "代理线下划拨",
) -> Dict[str, Any]:
    """
    主事务内 atomic:
      1. 扣代理库存(paid_inventory = tool + publish · bonus_inventory = bonus)
      2. 客户 wallet 入账(tool/publish/bonus 三池分轨)
      3. agent_inventory_transactions 双流水
      4. customer_credit_transactions source='offline_allocation'

    ❌ 不写 agent_revenue_ledger(代理已线下收款 · 平台不代收)
    ❌ 不写 recharge_orders

    上限 check:
      - 代理 paid_inventory ≥ tool_points + publish_points (publish-paid-only 铁律)
      - 代理 bonus_inventory ≥ bonus_points

    使用方式(代理后台 API):
        with get_db() as conn:
            cur = conn.cursor()
            result = allocate_offline_to_customer(
                cur,
                agent_user_id=agent.id,
                customer_user_id=customer.id,
                tool_points=10000,
                publish_points=5000,
                bonus_points=2000,
                description="客户 jane 线下转账 ¥2000 · 代理划拨",
            )
            conn.commit()  # 一次 atomic commit
    """
    if tool_points < 0 or publish_points < 0 or bonus_points < 0:
        raise ValueError("划拨积分必须非负")
    if tool_points == 0 and publish_points == 0 and bonus_points == 0:
        raise ValueError("线下划拨积分至少一个 > 0")

    paid_total = tool_points + publish_points
    bonus_total = bonus_points

    # 1. 扣代理库存(allocate_offline 内部已 check 余额 · 不够抛 ValueError)
    inv_result = _inventory_allocate_offline(
        cursor,
        agent_user_id=agent_user_id,
        customer_user_id=customer_user_id,
        paid_points=paid_total,
        bonus_points=bonus_total,
        description=description,
    )

    # 2. 客户入账(同事务 cursor · atomic)
    # [单账本收敛 2026-07-27] 落点从信用钱包三池改为客户自己的 user_wallets:
    #   tool + publish → paid_points(充值算力) · bonus → bonus_points(赠送算力)
    # 划拨这个业务动作保留(工单 §2),只是不再记进第二本账。
    credit_result = _customer_grant(
        cursor,
        customer_user_id=customer_user_id,
        agent_user_id=agent_user_id,
        paid_points=paid_total,
        bonus_points=bonus_total,
        related_order_id=None,  # 线下无订单号
        source="offline_allocation",
        description=description,
    )

    logger.info(
        f"[OfflineAllocation] agent={agent_user_id} → customer={customer_user_id} · "
        f"tool={tool_points} publish={publish_points} bonus={bonus_points} · atomic 双写完成"
    )

    return {
        "inventory_after": {
            "paid": inv_result["paid_inventory_points"],
            "bonus": inv_result["bonus_inventory_points"],
        },
        "customer_after": {
            # [单账本] 原来返三池 tool/publish/bonus,现返客户 user_wallets 实际余额
            "paid": credit_result["paid_points"],
            "bonus": credit_result["bonus_points"],
        },
        "allocated": {
            "tool": tool_points,
            "publish": publish_points,
            "bonus": bonus_points,
        },
    }


def revoke_offline_from_customer(
    cursor,
    agent_user_id: int,
    customer_user_id: int,
    tool_points: int = 0,
    publish_points: int = 0,
    bonus_points: int = 0,
    related_order_id: str = None,
    description: str = "线下撤回未消费额度",
) -> Dict[str, Any]:
    """
    主事务内 atomic 撤回(allocate_offline_to_customer 的反向):
      1. 算严格上限 (Codex r2:min(请求, 余额, 订单 FIFO 未消费))
      2. 客户 wallet revoke
      3. 代理库存 revoke(回 paid + bonus)

    related_order_id 可选:传则用 FIFO 按订单算未消费上限
    线下场景一般没订单号 · 此时仅按总余额 cap
    """
    from services.customer_entitlement import revoke_from_customer as _customer_revoke
    from services.agent_inventory import revoke_from_customer as _inv_revoke

    # 1. 从客户 user_wallets 回收(内部按当前余额夹紧,绝不扣成负数)
    # [单账本收敛 2026-07-27] 原走 customer_credit.revoke_credit 回收信用钱包三池。
    # 单账本后回收对象就是客户的充值算力/赠送算力。
    # ⚠️ 原实现有一层"按订单 FIFO 算未消费上限"的 cap;单账本下 point_transactions
    #    没有等价的按订单 FIFO 结构,现按【当前余额】夹紧 —— 对客户只会更宽松
    #    (回收得更少),不会多扣,方向是安全的。
    credit_result = _customer_revoke(
        cursor,
        customer_user_id=customer_user_id,
        agent_user_id=agent_user_id,
        paid_points=tool_points + publish_points,
        bonus_points=bonus_points,
        related_order_id=related_order_id,
        source="refund_revoke",
        description=description,
    )
    actually = {
        "tool": credit_result["revoked_paid"],   # 单账本无 tool/publish 之分,全记 tool 位
        "publish": 0,
        "bonus": credit_result["revoked_bonus"],
    }

    # 2. 代理库存回填(按实际撤回量)
    inv_result = _inv_revoke(
        cursor,
        agent_user_id=agent_user_id,
        customer_user_id=customer_user_id,
        paid_points_to_revoke=actually["tool"] + actually["publish"],
        bonus_points_to_revoke=actually["bonus"],
        description=description,
    )

    logger.info(
        f"[OfflineRevoke] agent={agent_user_id} ← customer={customer_user_id} · "
        f"actually tool={actually['tool']} publish={actually['publish']} bonus={actually['bonus']}"
    )

    return {
        "actually_revoked": actually,
        "inventory_after": {
            "paid": inv_result["paid_inventory_points"],
            "bonus": inv_result["bonus_inventory_points"],
        },
        "customer_after": {
            # [单账本] 客户 user_wallets 实际余额
            "paid": credit_result.get("paid_points", 0),
            "bonus": credit_result.get("bonus_points", 0),
        },
    }
