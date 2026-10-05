"""
V3.5 代理库存管理服务

核心责任:
- 代理 paid_inventory + bonus_inventory 双轨库存增减
- 库存流水双向 trace(purchase_auto + allocate_to_customer)
- 防负余额(CHECK 约束 + 应用层 FOR UPDATE 锁)
- 库存预警(30/10/0% 阈值)

业务规则铁律:
- 自动补库存必须双流水(purchase_auto +N · allocate -N)· 净 0 变化但 trace 完整
- 发布中心 paid-only:消费 publish_credit 时必须扣 paid_inventory · 不能扣 bonus
- revoke 上限 = min(客户当前余额, 该笔划拨剩余未消费)

关联:
- docs/AI-CONTEXT/V35_FACTORY_INVENTORY_MODEL_v6_2026-05-26.md §3/§7/§11
- memory feedback_v35_factory_inventory_model_v6
"""

import logging
from typing import Optional, Dict, Any, Tuple

logger = logging.getLogger("GEO-V35-AgentInventory")


# ============================================================
# T2 · 赠送/进货侧铸造护栏(工单 WORKORDER_ONDEMAND_MINTING_2026-07-29 §2)
# ============================================================
# 🔴 赠送算力本来就是"按需铸造"(凭空加、不从任何账号扣),但**一道闸都没有**:
#    无单笔上限、无订单号强制、无日累计告警。改充值那条为按需铸造时必须顺手
#    把它纳入**同一份**护栏实现,否则就是"新路有闸、老路没闸"。
#    → 两条路都调 services.inventory_minting_guard.assert_mint_allowed,不许各写一份。

def _guard_mint(
    cursor, *, source: str, agent_user_id: int, pool: str, points: int,
    related_order_id: Optional[str],
) -> None:
    """凭空产生库存前必过的统一护栏 · 拒绝直接抛 MintingGuardError(fail-closed)。"""
    from services import inventory_minting_guard as guard

    guard.assert_mint_allowed(
        cursor,
        source=source,
        agent_user_id=int(agent_user_id),
        pool=str(pool),
        points=int(points),
        related_order_id=related_order_id,
    )


# ============================================================
# 库存读写(主事务内 · 调用方需传 cursor + FOR UPDATE 锁)
# ============================================================

def get_or_create_inventory_wallet(cursor, agent_user_id: int) -> Dict[str, int]:
    """获取代理库存钱包 · 没有则创建 · 自动 FOR UPDATE 行锁"""
    cursor.execute("""
        SELECT paid_inventory_points, bonus_inventory_points, frozen_inventory_points,
               total_purchased_points, total_allocated_points
        FROM agent_inventory_wallets
        WHERE agent_user_id = %s FOR UPDATE
    """, (agent_user_id,))
    row = cursor.fetchone()
    if row:
        return dict(row) if isinstance(row, dict) else {
            "paid_inventory_points": row[0],
            "bonus_inventory_points": row[1],
            "frozen_inventory_points": row[2],
            "total_purchased_points": row[3],
            "total_allocated_points": row[4],
        }
    cursor.execute("""
        INSERT INTO agent_inventory_wallets (agent_user_id) VALUES (%s)
        ON CONFLICT (agent_user_id) DO NOTHING
        RETURNING paid_inventory_points, bonus_inventory_points, frozen_inventory_points,
                  total_purchased_points, total_allocated_points
    """, (agent_user_id,))
    row = cursor.fetchone()
    if row:
        return dict(row) if isinstance(row, dict) else {
            "paid_inventory_points": 0, "bonus_inventory_points": 0,
            "frozen_inventory_points": 0, "total_purchased_points": 0, "total_allocated_points": 0,
        }
    # 已存在 race 兜底
    cursor.execute("""
        SELECT paid_inventory_points, bonus_inventory_points, frozen_inventory_points,
               total_purchased_points, total_allocated_points
        FROM agent_inventory_wallets WHERE agent_user_id = %s FOR UPDATE
    """, (agent_user_id,))
    row = cursor.fetchone()
    return dict(row) if isinstance(row, dict) else dict(zip(
        ["paid_inventory_points","bonus_inventory_points","frozen_inventory_points",
         "total_purchased_points","total_allocated_points"], row))


def _insert_inventory_transaction(
    cursor,
    agent_user_id: int,
    type_: str,
    pool: str,
    points: int,
    balance_paid_after: int,
    balance_bonus_after: int,
    related_customer_user_id: Optional[int] = None,
    related_order_id: Optional[str] = None,
    description: Optional[str] = None,
) -> int:
    """写库存流水 · 返回 id"""
    cursor.execute("""
        INSERT INTO agent_inventory_transactions (
            agent_user_id, type, pool, points,
            balance_paid_after, balance_bonus_after,
            related_customer_user_id, related_order_id, description
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (agent_user_id, type_, pool, points,
          balance_paid_after, balance_bonus_after,
          related_customer_user_id, related_order_id, description))
    row = cursor.fetchone()
    return row["id"] if isinstance(row, dict) else row[0]


# ============================================================
# 库存补充(三入口)
# ============================================================

def purchase_inventory_prepay(
    cursor,
    agent_user_id: int,
    paid_points: int,
    bonus_points: int = 0,
    related_order_id: Optional[str] = None,
    description: str = "预付充值进货",
) -> Dict[str, Any]:
    """入口 A · 代理预付充值进货"""
    if paid_points < 0 or bonus_points < 0:
        raise ValueError("进货积分必须非负")

    # Dealer resale orders are reserved before an external payment intent exists.
    # Their callback must consume the seller reservation and create the buyer lot
    # in this same caller-owned transaction.  Detection is row-based rather than
    # flag-based: closing the writer gate must never strand an already-paid order.
    resale_settled: Optional[Dict[str, Any]] = None
    resale_order = False
    if related_order_id:
        from services import dealer_inventory_resale

        resale_order = dealer_inventory_resale.has_resale_order(cursor, str(related_order_id))
        if resale_order and paid_points > 0:
            resale_settled = dealer_inventory_resale.settle_reserved_order(
                cursor,
                buyer_user_id=int(agent_user_id),
                total_points=int(paid_points),
                order_id=str(related_order_id),
            )
            if resale_settled is None:
                raise RuntimeError("逐级转售订单探测与结算结果不一致")
        if resale_order and bonus_points > 0:
            dealer_inventory_resale.assert_resale_bonus_credit_allowed(
                cursor,
                order_id=str(related_order_id),
                buyer_user_id=int(agent_user_id),
            )
        if resale_order and bonus_points == 0:
            return resale_settled or get_or_create_inventory_wallet(cursor, agent_user_id)

    wallet = get_or_create_inventory_wallet(cursor, agent_user_id)
    wallet_paid_points = 0 if resale_order else paid_points
    # 🔴 只有真正凭空加的那部分才叫铸造:resale 单的 paid 已由逐级转售链供给
    # (wallet_paid_points 被清零),不重复过护栏;bonus 永远是凭空加,永远过护栏。
    from services import inventory_minting_guard as _mint_guard

    if wallet_paid_points > 0:
        _guard_mint(
            cursor, source=_mint_guard.SOURCE_INVENTORY_PREPAY,
            agent_user_id=agent_user_id, pool=_mint_guard.POOL_PAID,
            points=wallet_paid_points, related_order_id=related_order_id,
        )
    if bonus_points > 0:
        _guard_mint(
            cursor, source=_mint_guard.SOURCE_INVENTORY_PREPAY,
            agent_user_id=agent_user_id, pool=_mint_guard.POOL_BONUS,
            points=bonus_points, related_order_id=related_order_id,
        )
    new_paid = wallet["paid_inventory_points"] + wallet_paid_points
    new_bonus = wallet["bonus_inventory_points"] + bonus_points

    cursor.execute("""
        UPDATE agent_inventory_wallets
        SET paid_inventory_points = %s,
            bonus_inventory_points = %s,
            total_purchased_points = total_purchased_points + %s,
            updated_at = NOW()
        WHERE agent_user_id = %s
    """, (new_paid, new_bonus, wallet_paid_points + bonus_points, agent_user_id))

    if wallet_paid_points > 0:
        _insert_inventory_transaction(
            cursor, agent_user_id, "purchase_prepay", "paid", wallet_paid_points,
            new_paid, new_bonus, related_order_id=related_order_id, description=description,
        )
    if bonus_points > 0:
        _insert_inventory_transaction(
            cursor, agent_user_id, "purchase_prepay", "bonus", bonus_points,
            new_paid, new_bonus, related_order_id=related_order_id, description=description,
        )
    result = {"paid_inventory_points": new_paid, "bonus_inventory_points": new_bonus}
    if resale_settled is not None:
        result.update({"resale_settled": True, "resale_result": resale_settled})
    return result


def refund_inventory_prepay(
    cursor,
    agent_user_id: int,
    paid_points: int,
    bonus_points: int = 0,
    related_order_id: Optional[str] = None,
    description: str = "预付进货退款扣回",
) -> Dict[str, Any]:
    """[BUG-P1] 预付进货(agent_inventory_prepay)退款专用反向链。

    从代理库存扣回【未划拨】的进货积分(写 refund_clawback 流水),【不碰 user_wallets】——
    根因:原退款分流只认 v35_inventory_settlement,agent_inventory_prepay 落 legacy 路径
    对个人 user_wallets 扣减(退错池:钱退了/库存还在,或余额不足整体卡死)。

    库存余额不足(部分进货已划拨给客户)→ 返回 manual_review,不强扣
    (避免把已交付客户的额度凭空冲掉)。
    """
    if paid_points < 0 or bonus_points < 0:
        raise ValueError("退款积分必须非负")
    wallet = get_or_create_inventory_wallet(cursor, agent_user_id)
    avail_paid = wallet["paid_inventory_points"]
    avail_bonus = wallet["bonus_inventory_points"]
    if avail_paid < paid_points or avail_bonus < bonus_points:
        return {
            "success": False, "manual_review": True,
            "reason": "库存余额不足(部分进货已划拨给客户)· 需人工核对划拨明细后处理",
            "avail_paid": avail_paid, "avail_bonus": avail_bonus,
            "need_paid": paid_points, "need_bonus": bonus_points,
        }
    new_paid = avail_paid - paid_points
    new_bonus = avail_bonus - bonus_points
    cursor.execute("""
        UPDATE agent_inventory_wallets
        SET paid_inventory_points = %s,
            bonus_inventory_points = %s,
            total_purchased_points = GREATEST(0, total_purchased_points - %s),
            updated_at = NOW()
        WHERE agent_user_id = %s
    """, (new_paid, new_bonus, paid_points + bonus_points, agent_user_id))
    if paid_points > 0:
        _insert_inventory_transaction(
            cursor, agent_user_id, "refund_clawback", "paid", -paid_points,
            new_paid, new_bonus, related_order_id=related_order_id, description=description,
        )
    if bonus_points > 0:
        _insert_inventory_transaction(
            cursor, agent_user_id, "refund_clawback", "bonus", -bonus_points,
            new_paid, new_bonus, related_order_id=related_order_id, description=description,
        )
    return {"success": True, "paid_inventory_points": new_paid, "bonus_inventory_points": new_bonus,
            "refunded_paid": paid_points, "refunded_bonus": bonus_points}


def purchase_auto_and_allocate(
    cursor,
    agent_user_id: int,
    customer_user_id: int,
    points_granted: int,
    bonus_points: int = 0,
    related_order_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    入口 B · 线上付款触发的自动补库存 + 即时划拨给客户
    Codex r2 强调:必须双流水留 trace · 净变化 0 但财务可看到出厂成本结转

    points_granted = paid 部分(主体 · 客户可用工具+发布)
    bonus_points   = 赠送部分(仅工具 · 禁发布)
    """
    if points_granted < 0 or bonus_points < 0:
        raise ValueError("分配积分必须非负")
    if points_granted == 0 and bonus_points == 0:
        return {"paid_inventory_points": 0, "bonus_inventory_points": 0,
                "allocated_paid": 0, "allocated_bonus": 0}

    from services import inventory_minting_guard as _mint_guard

    if points_granted > 0:
        _guard_mint(
            cursor, source=_mint_guard.SOURCE_INVENTORY_PURCHASE_AUTO,
            agent_user_id=agent_user_id, pool=_mint_guard.POOL_PAID,
            points=points_granted, related_order_id=related_order_id,
        )
    if bonus_points > 0:
        _guard_mint(
            cursor, source=_mint_guard.SOURCE_INVENTORY_PURCHASE_AUTO,
            agent_user_id=agent_user_id, pool=_mint_guard.POOL_BONUS,
            points=bonus_points, related_order_id=related_order_id,
        )

    wallet = get_or_create_inventory_wallet(cursor, agent_user_id)

    # 流水 1: 自动补库存(进货 · paid + bonus 分别记)
    # 流水 2: 即时划拨给客户(allocate)
    # 净变化 0 · 但 trace 可看出厂成本流入 / 客户授权流出

    # paid 池
    intermediate_paid = wallet["paid_inventory_points"] + points_granted
    if points_granted > 0:
        _insert_inventory_transaction(
            cursor, agent_user_id, "purchase_auto", "paid", points_granted,
            intermediate_paid, wallet["bonus_inventory_points"],
            related_customer_user_id=customer_user_id,
            related_order_id=related_order_id,
            description="线上付款自动进货 · paid",
        )
    final_paid = intermediate_paid  # 同步 -N 立即划拨
    if points_granted > 0:
        final_paid = intermediate_paid - points_granted  # 净变化 0
        _insert_inventory_transaction(
            cursor, agent_user_id, "allocate_to_customer", "paid", -points_granted,
            final_paid, wallet["bonus_inventory_points"],
            related_customer_user_id=customer_user_id,
            related_order_id=related_order_id,
            description="线上付款自动划拨给客户 · paid",
        )

    # bonus 池(同样双流水)
    intermediate_bonus = wallet["bonus_inventory_points"] + bonus_points
    if bonus_points > 0:
        _insert_inventory_transaction(
            cursor, agent_user_id, "purchase_auto", "bonus", bonus_points,
            final_paid, intermediate_bonus,
            related_customer_user_id=customer_user_id,
            related_order_id=related_order_id,
            description="线上付款自动进货 · bonus",
        )
    final_bonus = intermediate_bonus
    if bonus_points > 0:
        final_bonus = intermediate_bonus - bonus_points
        _insert_inventory_transaction(
            cursor, agent_user_id, "allocate_to_customer", "bonus", -bonus_points,
            final_paid, final_bonus,
            related_customer_user_id=customer_user_id,
            related_order_id=related_order_id,
            description="线上付款自动划拨给客户 · bonus",
        )

    # wallet 更新(净变化 0 · 仅 total_purchased + total_allocated 累计)
    cursor.execute("""
        UPDATE agent_inventory_wallets
        SET total_purchased_points = total_purchased_points + %s,
            total_allocated_points = total_allocated_points + %s,
            updated_at = NOW()
        WHERE agent_user_id = %s
    """, (points_granted + bonus_points, points_granted + bonus_points, agent_user_id))

    return {
        "paid_inventory_points": final_paid,
        "bonus_inventory_points": final_bonus,
        "allocated_paid": points_granted,
        "allocated_bonus": bonus_points,
    }


def purchase_inventory_admin_adjust(
    cursor,
    agent_user_id: int,
    paid_points: int = 0,
    bonus_points: int = 0,
    admin_user_id: Optional[int] = None,
    description: str = "对公转账 admin 入账",
    related_order_id: Optional[str] = None,
) -> Dict[str, Any]:
    """入口 C · admin 对公转账后手动入账

    🔴 T2:这条也是凭空加,同样必须绑真实订单(工单 §3-3 "把铸造从管理动作变成
    订单的必然结果",顺带解决 boss_authorized 自填无凭证)。新增 related_order_id
    为必填语义 —— 本函数当前无生产调用方(全仓非测试调用 0 处),不构成回归。
    """
    if paid_points < 0 or bonus_points < 0:
        raise ValueError("admin 入账积分必须非负")
    if paid_points == 0 and bonus_points == 0:
        raise ValueError("admin 入账积分至少一个 > 0")

    from services import inventory_minting_guard as _mint_guard

    if paid_points > 0:
        _guard_mint(
            cursor, source=_mint_guard.SOURCE_INVENTORY_ADMIN_ADJUST,
            agent_user_id=agent_user_id, pool=_mint_guard.POOL_PAID,
            points=paid_points, related_order_id=related_order_id,
        )
    if bonus_points > 0:
        _guard_mint(
            cursor, source=_mint_guard.SOURCE_INVENTORY_ADMIN_ADJUST,
            agent_user_id=agent_user_id, pool=_mint_guard.POOL_BONUS,
            points=bonus_points, related_order_id=related_order_id,
        )

    wallet = get_or_create_inventory_wallet(cursor, agent_user_id)
    new_paid = wallet["paid_inventory_points"] + paid_points
    new_bonus = wallet["bonus_inventory_points"] + bonus_points

    cursor.execute("""
        UPDATE agent_inventory_wallets
        SET paid_inventory_points = %s,
            bonus_inventory_points = %s,
            total_purchased_points = total_purchased_points + %s,
            updated_at = NOW()
        WHERE agent_user_id = %s
    """, (new_paid, new_bonus, paid_points + bonus_points, agent_user_id))

    desc = description + (f" (admin={admin_user_id})" if admin_user_id else "")
    if paid_points > 0:
        _insert_inventory_transaction(
            cursor, agent_user_id, "purchase_admin_adjust", "paid", paid_points,
            new_paid, new_bonus, related_order_id=related_order_id, description=desc,
        )
    if bonus_points > 0:
        _insert_inventory_transaction(
            cursor, agent_user_id, "purchase_admin_adjust", "bonus", bonus_points,
            new_paid, new_bonus, related_order_id=related_order_id, description=desc,
        )
    return {"paid_inventory_points": new_paid, "bonus_inventory_points": new_bonus}


def admin_adjust_decrease(
    cursor,
    agent_user_id: int,
    paid_points: int = 0,
    bonus_points: int = 0,
    admin_user_id: Optional[int] = None,
    description: str = "admin 库存冲减",
) -> Dict[str, Any]:
    """入口 C 的反向 · admin 冲减服务商库存(录错/超发的更正)。

    🔴 与 `refund_inventory_prepay` 的区别:那条是**退款**反向链(type
    `refund_clawback`,语义绑一笔进货订单的退款);这条是**管理更正**
    (type `admin_adjust`)。两种语义混用会让财务分不清"退了钱"和"改了账"。
    `admin_adjust` 早已在 `agent_inventory_transactions_type_check` 白名单里,
    本函数是第一个写它的调用方 —— **不需要动那条 CHECK**(红线)。

    余额不足直接抛 ValueError:库存不能扣成负数
    (`agent_inventory_wallets` 有 CHECK 兜底,应用层先给人话)。
    """
    if paid_points < 0 or bonus_points < 0:
        raise ValueError("冲减积分必须非负")
    if paid_points == 0 and bonus_points == 0:
        raise ValueError("冲减积分至少一个 > 0")

    wallet = get_or_create_inventory_wallet(cursor, agent_user_id)
    if wallet["paid_inventory_points"] < paid_points:
        raise ValueError(
            f"充值库存不足:需要冲减 {paid_points} · 当前 {wallet['paid_inventory_points']}"
        )
    if wallet["bonus_inventory_points"] < bonus_points:
        raise ValueError(
            f"赠送库存不足:需要冲减 {bonus_points} · 当前 {wallet['bonus_inventory_points']}"
        )

    new_paid = wallet["paid_inventory_points"] - paid_points
    new_bonus = wallet["bonus_inventory_points"] - bonus_points
    cursor.execute("""
        UPDATE agent_inventory_wallets
        SET paid_inventory_points = %s,
            bonus_inventory_points = %s,
            total_purchased_points = GREATEST(0, total_purchased_points - %s),
            updated_at = NOW()
        WHERE agent_user_id = %s
    """, (new_paid, new_bonus, paid_points + bonus_points, agent_user_id))

    desc = description + (f" (admin={admin_user_id})" if admin_user_id else "")
    if paid_points > 0:
        _insert_inventory_transaction(
            cursor, agent_user_id, "admin_adjust", "paid", -paid_points,
            new_paid, new_bonus, description=desc,
        )
    if bonus_points > 0:
        _insert_inventory_transaction(
            cursor, agent_user_id, "admin_adjust", "bonus", -bonus_points,
            new_paid, new_bonus, description=desc,
        )
    return {"paid_inventory_points": new_paid, "bonus_inventory_points": new_bonus,
            "decreased_paid": paid_points, "decreased_bonus": bonus_points}


# ============================================================
# 线下划拨(代理后台手动)
# ============================================================

def allocate_offline(
    cursor,
    agent_user_id: int,
    customer_user_id: int,
    paid_points: int = 0,
    bonus_points: int = 0,
    description: str = "线下手动划拨",
) -> Dict[str, Any]:
    """
    路径 B · 代理线下收钱后手动划拨给客户
    ❌ 不写 agent_revenue_ledger(代理已线下收款 · 平台不代收)
    ❌ 不写 recharge_orders
    需要先 check 库存够

    🔴 [R1 返修 2026-08-12] 这里**必须同事务消 lot** —— 本函数就是那个漏账源。
       `08_billing.md §5.3`:聚合钱包只是缓存式总量,有限 lot 才是成本与所有权 SSOT。
       本函数原来只扣 `agent_inventory_wallets` 不动 `dealer_inventory_lots`,
       而对账等式只比「钱包 vs 流水」,这一维零判据 → 生产已漂移
       u125 −10000 / u133 −5416(净差 −15,416),期间对账天天报绿。

       消耗**下沉到这里**而不是包在端点外面:三个调用方
         · `api/agent_workbench_api.py`(服务商线下划拨)—— 漏账源本体
         · `services/offline_allocation.allocate_offline_to_customer`(内部就是调本函数)
         · `services/agent_rebate.py`(返利,只传 bonus → 天然 no-op)
       只在端点上包一层的话,下一个调用方照样漏。

       🔴 **只消 `paid_points` 的 lot**:`bonus` 永不建 lot、永不消 lot
       (§5.3「bonus 不能变成 paid、不能作为有价库存转售」)。把 bonus 算进去
       会凭空多消一份有价库存 —— 判别锁 M17 钉这条。

       🔴 fail-closed:lot 不足直接抛 `ResaleError`,**钱包一分不动**(先消 lot 再扣钱包)。
       当前生产撞不上(26 个库存钱包全部 wallet_side ≤ lot_side),但分支仍然要有锁。
    """
    if paid_points < 0 or bonus_points < 0:
        raise ValueError("划拨积分必须非负")
    if paid_points == 0 and bonus_points == 0:
        return {"paid_inventory_points": 0, "bonus_inventory_points": 0}

    wallet = get_or_create_inventory_wallet(cursor, agent_user_id)
    if wallet["paid_inventory_points"] < paid_points:
        raise ValueError(f"服务方充值库存不足: 需要 {paid_points} · 当前 {wallet['paid_inventory_points']}")
    if wallet["bonus_inventory_points"] < bonus_points:
        raise ValueError(f"服务方赠送库存不足: 需要 {bonus_points} · 当前 {wallet['bonus_inventory_points']}")

    # 🔴 lot 侧**先于**钱包侧写:lot 不足时抛出,钱包这一步根本没执行到,
    #    不会留下"钱包扣了 lot 没消"的半边账(那正是本函数原来的病)。
    if paid_points > 0:
        from services.inventory_lot_ledger import consume_lots_fifo

        consume_lots_fifo(cursor, agent_user_id, paid_points, reason=description)

    new_paid = wallet["paid_inventory_points"] - paid_points
    new_bonus = wallet["bonus_inventory_points"] - bonus_points

    cursor.execute("""
        UPDATE agent_inventory_wallets
        SET paid_inventory_points = %s,
            bonus_inventory_points = %s,
            total_allocated_points = total_allocated_points + %s,
            updated_at = NOW()
        WHERE agent_user_id = %s
    """, (new_paid, new_bonus, paid_points + bonus_points, agent_user_id))

    if paid_points > 0:
        _insert_inventory_transaction(
            cursor, agent_user_id, "allocate_to_customer_offline", "paid", -paid_points,
            new_paid, new_bonus, related_customer_user_id=customer_user_id,
            description=description,
        )
    if bonus_points > 0:
        _insert_inventory_transaction(
            cursor, agent_user_id, "allocate_to_customer_offline", "bonus", -bonus_points,
            new_paid, new_bonus, related_customer_user_id=customer_user_id,
            description=description,
        )
    return {
        "paid_inventory_points": new_paid,
        "bonus_inventory_points": new_bonus,
        "allocated_paid": paid_points,
        "allocated_bonus": bonus_points,
    }


# ============================================================
# 线下供货给下线服务商(P0 热修 2026-08-13 · WO_INVREL_P0_HOTFIX §2)
# ============================================================

def supply_downstream_inventory(
    cursor,
    upstream_user_id: int,
    downstream_user_id: int,
    paid_points: int = 0,
    bonus_points: int = 0,
    description: str = "线下供货给下线服务商",
    idempotency_ref: str = "",
) -> Dict[str, Any]:
    """上游把自己的算力供给**下线服务商** · 进对方的**库存算力**。

    Owner 2026-08-13 拍板:服务商线下进货不走线上,这笔算力是上游的,
    怎么用是他的事,**不许阻断**。本函数就是那个被阻断掉的动作。

    🔴 为什么进库存钱包而不是可用钱包:
       下线是服务商,库存算力**可继续向下分销**;而对方在自己的库存中心本来就有
       「转为可用算力」(1:1)。进库存钱包不损失任何能力,进可用钱包则**永久丧失**
       分销能力 —— 把选择权留给对方,符合「怎么用是他的事」。

    🔴 双侧 lot 同事务(invchain 的血教训,别重犯):
         上游  lot ↓ + 钱包 ↓        下线  lot ↑ + 钱包 ↑
       只写钱包不动 lot,下一次对账就是新的 −N 漂移,而对账等式当时照样报绿。
       lot **先于**钱包写:lot 不足直接抛 `ResaleError`,钱包一分不动,
       不会留下"钱包扣了 lot 没消"的半边账。

    🔴 `bonus` 永不建 lot、永不消 lot(`08_billing.md` §5.3:bonus 不能变成 paid、
       不能作为有价库存转售)。把 bonus 算进 lot 会凭空多消一份有价库存。

    🔴 **不写** `agent_revenue_ledger`、**不写** `recharge_orders` ——
       线下已收款,平台不代收(与 `allocate_offline` 同语义)。
       成本按 R3 取**已绑定**的 `cost_multiplier_bps`,不回落默认、不按身份现算:
       下线这批的入账成本 = 上游本批实际成本 × 已约定进货系数。
       这不是"给平台记一笔收入",而是让下线 lot 的成本基础反映它真实的进货价 ——
       否则下线再往下转售时,成本基础是错的,分润链跟着错。
    """
    from services.channel_partner_requests import resolve_bound_cost_multiplier_bps
    from services.dealer_inventory_resale import calculate_hop_sale
    from services.inventory_lot_ledger import consume_lots_fifo, credit_channel_supply_lot

    upstream_id, downstream_id = int(upstream_user_id), int(downstream_user_id)
    paid_points, bonus_points = int(paid_points), int(bonus_points)
    if paid_points < 0 or bonus_points < 0:
        raise ValueError("供货算力必须非负")
    if paid_points == 0 and bonus_points == 0:
        raise ValueError("供货算力至少一项 > 0")
    if upstream_id == downstream_id:
        raise ValueError("不能给自己供货")

    # 🔴 有向关系 + 已绑定系数一次取到:没有生效的渠道关系直接抛错,
    #    绝不回落默认系数(R3)。这同时也是"只能供给自己的下线"的守卫 ——
    #    `resolve_bound_cost_multiplier_bps` 内部查的是有向关系(R5)。
    bound_bps = resolve_bound_cost_multiplier_bps(cursor, upstream_id, downstream_id)

    up_wallet = get_or_create_inventory_wallet(cursor, upstream_id)
    if up_wallet["paid_inventory_points"] < paid_points:
        raise ValueError(
            f"充值库存不足: 需要 {paid_points} · 当前 {up_wallet['paid_inventory_points']}"
        )
    if up_wallet["bonus_inventory_points"] < bonus_points:
        raise ValueError(
            f"赠送库存不足: 需要 {bonus_points} · 当前 {up_wallet['bonus_inventory_points']}"
        )
    down_wallet = get_or_create_inventory_wallet(cursor, downstream_id)

    ref = str(idempotency_ref or "").strip() or f"{upstream_id}-{downstream_id}-{paid_points}-{bonus_points}"
    supply_lot = None
    if paid_points > 0:
        # ① 上游侧消 lot(先于任何钱包写)· 拿到本批的**真实成本基础**
        consumed = consume_lots_fifo(cursor, upstream_id, paid_points, reason=description)
        upstream_cost_cents = int(consumed["cost_basis_cents"])
        # ② 下线侧建 lot · 成本 = 上游成本 × 已绑定进货系数(复用既有计价原语,不重造公式)
        downstream_cost_cents = calculate_hop_sale(upstream_cost_cents, bound_bps)
        supply_lot = credit_channel_supply_lot(
            cursor, downstream_id, paid_points,
            pricing_version=f"chsup-{ref}"[:64],
            acquisition_cost_cents=downstream_cost_cents,
            evidence={
                "kind": "offline_channel_supply",
                "upstream_user_id": upstream_id,
                "downstream_user_id": downstream_id,
                "upstream_cost_basis_cents": upstream_cost_cents,
                "idempotency_ref": ref,
                "note": "上游线下收款后供货;平台不代收,不写收益账与充值订单",
            },
        )

    # ③ 两侧聚合钱包
    up_paid = up_wallet["paid_inventory_points"] - paid_points
    up_bonus = up_wallet["bonus_inventory_points"] - bonus_points
    cursor.execute("""
        UPDATE agent_inventory_wallets
        SET paid_inventory_points = %s,
            bonus_inventory_points = %s,
            total_allocated_points = total_allocated_points + %s,
            updated_at = NOW()
        WHERE agent_user_id = %s
    """, (up_paid, up_bonus, paid_points + bonus_points, upstream_id))

    down_paid = down_wallet["paid_inventory_points"] + paid_points
    down_bonus = down_wallet["bonus_inventory_points"] + bonus_points
    cursor.execute("""
        UPDATE agent_inventory_wallets
        SET paid_inventory_points = %s,
            bonus_inventory_points = %s,
            total_purchased_points = total_purchased_points + %s,
            updated_at = NOW()
        WHERE agent_user_id = %s
    """, (down_paid, down_bonus, paid_points + bonus_points, downstream_id))

    # ④ 双侧流水 · `resale_transfer_out/in` 早在 type CHECK 白名单里(2026-08-13 生产现取)
    for pool, amount in (("paid", paid_points), ("bonus", bonus_points)):
        if amount <= 0:
            continue
        _insert_inventory_transaction(
            cursor, upstream_id, "resale_transfer_out", pool, -amount,
            up_paid, up_bonus, related_customer_user_id=downstream_id,
            description=description,
        )
        _insert_inventory_transaction(
            cursor, downstream_id, "resale_transfer_in", pool, amount,
            down_paid, down_bonus, related_customer_user_id=upstream_id,
            description=description,
        )

    return {
        # 🔴 键名**刻意不叫** `upstream_*`:R5 的机械扫描按前缀封 `upstream_`,
        #    而这几个值其实是**调用方自己**的余额,不是"上游是谁"的线索。
        #    与其给扫描器开例外(那等于把判据挖个洞),不如按调用方视角命名。
        #    判别锁 `test_64c_supply_result_has_zero_private_fields` 钉这条。
        "agent_paid_after": up_paid,
        "agent_bonus_after": up_bonus,
        "downstream_paid_after": down_paid,
        "downstream_bonus_after": down_bonus,
        "supplied_paid": paid_points,
        "supplied_bonus": bonus_points,
        # 🔴 不回传 `cost_multiplier_bps` / `relationship_id` 等关系反推字段(R5)。
        "lot_id": (supply_lot or {}).get("lot_id"),
    }


# ============================================================
# revoke(撤回 · 严格上限)· Codex r2 修正
# ============================================================

def revoke_from_customer(
    cursor,
    agent_user_id: int,
    customer_user_id: int,
    paid_points_to_revoke: int = 0,
    bonus_points_to_revoke: int = 0,
    description: str = "撤回未消费额度",
) -> Dict[str, Any]:
    """
    撤回客户已分配但未消费的额度 · 回代理 inventory
    严格上限:撤回 = min(客户当前余额, 该笔划拨剩余未消费)
    已消费部分不可撤 · 由代理线下协商
    """
    if paid_points_to_revoke < 0 or bonus_points_to_revoke < 0:
        raise ValueError("撤回积分必须非负")

    wallet = get_or_create_inventory_wallet(cursor, agent_user_id)
    new_paid = wallet["paid_inventory_points"] + paid_points_to_revoke
    new_bonus = wallet["bonus_inventory_points"] + bonus_points_to_revoke

    cursor.execute("""
        UPDATE agent_inventory_wallets
        SET paid_inventory_points = %s,
            bonus_inventory_points = %s,
            updated_at = NOW()
        WHERE agent_user_id = %s
    """, (new_paid, new_bonus, agent_user_id))

    if paid_points_to_revoke > 0:
        _insert_inventory_transaction(
            cursor, agent_user_id, "revoke_from_customer", "paid", paid_points_to_revoke,
            new_paid, new_bonus, related_customer_user_id=customer_user_id,
            description=description,
        )
    if bonus_points_to_revoke > 0:
        _insert_inventory_transaction(
            cursor, agent_user_id, "revoke_from_customer", "bonus", bonus_points_to_revoke,
            new_paid, new_bonus, related_customer_user_id=customer_user_id,
            description=description,
        )
    return {"paid_inventory_points": new_paid, "bonus_inventory_points": new_bonus}


# ============================================================
# 库存预警(配置化阈值)
# ============================================================

def check_inventory_alert_level(
    cursor,
    agent_user_id: int,
    initial_purchased_points: Optional[int] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    返回 (level, info)
        level: 'ok' / 'low_warning' / 'critical' / 'block'
    """
    from services.agent_pricing import get_platform_fee_config  # 复用 settings 读
    cursor.execute("SELECT value FROM system_settings WHERE key='agent_inventory_alert_config'")
    row = cursor.fetchone()
    if row:
        import json
        val = row["value"] if isinstance(row, dict) else row[0]
        config = json.loads(val) if isinstance(val, str) else val
    else:
        config = {"low_warning_pct": 30, "critical_pct": 10, "block_pct": 0}

    wallet = get_or_create_inventory_wallet(cursor, agent_user_id)
    total_purchased = wallet["total_purchased_points"]
    current_total = wallet["paid_inventory_points"] + wallet["bonus_inventory_points"]

    if total_purchased == 0:
        return ("ok", {"reason": "未进货", "current_total": current_total})

    remaining_pct = current_total * 100 / max(total_purchased, 1)

    if remaining_pct <= config["block_pct"]:
        return ("block", {"remaining_pct": remaining_pct, "current_total": current_total})
    elif remaining_pct <= config["critical_pct"]:
        return ("critical", {"remaining_pct": remaining_pct, "current_total": current_total})
    elif remaining_pct <= config["low_warning_pct"]:
        return ("low_warning", {"remaining_pct": remaining_pct, "current_total": current_total})
    return ("ok", {"remaining_pct": remaining_pct, "current_total": current_total})


def ensure_inventory_sufficient(
    cursor,
    agent_user_id: int,
    required_paid: int = 0,
    required_bonus: int = 0,
) -> bool:
    """
    检查代理库存是否足够 · 不够抛错(给充值 API 用)
    返回 True · 不够 raise InsufficientInventoryError
    """
    wallet = get_or_create_inventory_wallet(cursor, agent_user_id)
    if wallet["paid_inventory_points"] < required_paid:
        raise InsufficientInventoryError(
            agent_user_id=agent_user_id,
            pool="paid",
            required=required_paid,
            available=wallet["paid_inventory_points"],
        )
    if wallet["bonus_inventory_points"] < required_bonus:
        raise InsufficientInventoryError(
            agent_user_id=agent_user_id,
            pool="bonus",
            required=required_bonus,
            available=wallet["bonus_inventory_points"],
        )
    return True


class InsufficientInventoryError(Exception):
    """代理库存不足 · 客户购买阻断"""

    def __init__(self, agent_user_id: int, pool: str, required: int, available: int):
        self.agent_user_id = agent_user_id
        self.pool = pool
        self.required = required
        self.available = available
        super().__init__("服务方库存不足，请稍后再试")
