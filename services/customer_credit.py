"""
V3.5 客户授权额度服务(customer_agent_credit_wallets)· **历史账本 · 停写**

🔴 该表自 2026-07-29 单账本收敛(工单 `WALLET_SINGLE_LEDGER_WORKORDER_2026-07-27.md`)
   起停写:5 户余额已并入各自 `user_wallets` 并清零三池,**行保留**作历史留痕。
   客户算力的唯一落点是 `user_wallets`,新增/回收一律走
   `services/customer_entitlement.py`(grant_to_customer / revoke_from_customer)。

[单账本接线 2026-08-17] 本模块只剩**历史结算安全网**,不再承担任何当前业务写入:
- 已删除(全仓 0 调用方实证见工单 R4):`allocate_credit` / `consume_credit` /
  `revoke_credit` / `freeze_customer_credit` / `estimate_remaining_usage` /
  `get_wallet_summary` / `is_publish_feature` / `InsufficientCreditError` /
  `PublishPaidOnlyError`。
- 保留:`refund_credit` + `commit/release_customer_freeze` —— 工单
  `WALLET_SINGLE_LEDGER_WORKORDER` §4.2 **刻意保留**的在途 v35 冻结结算通道
  (`middleware/billing.py:17` 同款说明);`compute_unspent_from_order` 仍被
  退款链读**历史订单**的 FIFO 未消费量。
- 🔴 **不要**往本模块加新功能。新代码一律用 customer_entitlement。

⚠️ 命名严格:`customer_agent_credit_wallets` 是"代理授权给客户的额度"
  不是平台直营 user_wallets · 不混淆

关联:
- docs/AI-CONTEXT/V35_FACTORY_INVENTORY_MODEL_v6_2026-05-26.md §3/§9/§12
- docs/SYSTEM_TRUTH/08_billing.md(资金唯一 SSOT · 单账本)
"""

import logging
from typing import Optional, Dict, Any

logger = logging.getLogger("GEO-V35-CustomerCredit")


def _log_bonus_grant_reconcile(cursor, customer_user_id: int, context: str) -> Optional[Dict[str, Any]]:
    """Best-effort conservation check for channel-tier customer bonus grants."""
    try:
        from services.bonus_grants import reconcile_owner

        result = reconcile_owner(cursor, "customer", int(customer_user_id))
        pool_balance = int(result.get("pool_balance") or 0)
        active_points = int(result.get("grant_active_points") or 0)
        if pool_balance != active_points:
            logger.warning(
                "[%s] bonus grant reconcile drift · customer=%s pool=%s active=%s frozen=%s",
                context,
                customer_user_id,
                pool_balance,
                active_points,
                result.get("grant_frozen_points"),
            )
        return result
    except Exception as exc:
        logger.warning(
            "[%s] bonus grant reconcile check failed · customer=%s: %s",
            context,
            customer_user_id,
            exc,
        )
        return None


# [单账本接线 2026-08-17 · R4 孤儿清理] PUBLISH_FEATURE_CODES / TOOL_FEATURE_CATEGORIES
# 已删除:它们只服务于「按 feature 决定扣 publish 还是 tool 池」这件事,而三池已随
# 单账本收敛消失(客户算力只有 user_wallets 的 paid/bonus 两轨)。唯一消费方
# is_publish_feature / consume_credit / estimate_remaining_usage 同批删除。
# 发布类功能「只能用充值算力不能用赠送算力」的铁律仍在,但判据 SSOT 是
# `feature_pricing.requires_paid_points`(DB 配置),不是本文件的硬编码白名单。


# ============================================================
# wallet 读写(主事务内 · FOR UPDATE)
# ============================================================

def get_or_create_customer_wallet(
    cursor,
    customer_user_id: int,
    agent_user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """获取客户授权 wallet · 没有则创建(需 agent_user_id 绑定)"""
    cursor.execute("""
        SELECT customer_user_id, agent_user_id,
               tool_credit_points, publish_credit_points, bonus_credit_points,
               total_purchased_points, total_consumed_points
        FROM customer_agent_credit_wallets
        WHERE customer_user_id = %s FOR UPDATE
    """, (customer_user_id,))
    row = cursor.fetchone()
    if row:
        return dict(row) if isinstance(row, dict) else {
            "customer_user_id": row[0], "agent_user_id": row[1],
            "tool_credit_points": row[2], "publish_credit_points": row[3],
            "bonus_credit_points": row[4],
            "total_purchased_points": row[5], "total_consumed_points": row[6],
        }
    if agent_user_id is None:
        raise ValueError(f"客户 {customer_user_id} 未绑定代理 · 不能创建 wallet")

    cursor.execute("""
        INSERT INTO customer_agent_credit_wallets (customer_user_id, agent_user_id)
        VALUES (%s, %s)
        ON CONFLICT (customer_user_id) DO NOTHING
        RETURNING customer_user_id, agent_user_id,
                  tool_credit_points, publish_credit_points, bonus_credit_points,
                  total_purchased_points, total_consumed_points
    """, (customer_user_id, agent_user_id))
    row = cursor.fetchone()
    if row:
        return dict(row) if isinstance(row, dict) else {
            "customer_user_id": row[0], "agent_user_id": row[1],
            "tool_credit_points": 0, "publish_credit_points": 0, "bonus_credit_points": 0,
            "total_purchased_points": 0, "total_consumed_points": 0,
        }
    # race 兜底
    return get_or_create_customer_wallet(cursor, customer_user_id)


def _insert_credit_transaction(
    cursor,
    customer_user_id: int,
    agent_user_id: int,
    type_: str,
    pool: str,
    points: int,
    balance_tool: int,
    balance_publish: int,
    balance_bonus: int,
    feature_code: Optional[str] = None,
    related_order_id: Optional[str] = None,
    source: Optional[str] = None,
    description: Optional[str] = None,
) -> int:
    """写客户额度流水 · 返回 id"""
    cursor.execute("""
        INSERT INTO customer_credit_transactions (
            customer_user_id, agent_user_id, type, pool, points,
            balance_tool_after, balance_publish_after, balance_bonus_after,
            feature_code, related_order_id, source, description
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (customer_user_id, agent_user_id, type_, pool, points,
          balance_tool, balance_publish, balance_bonus,
          feature_code, related_order_id, source, description))
    row = cursor.fetchone()
    return row["id"] if isinstance(row, dict) else row[0]


# ============================================================
# 划拨入账(线上 / 线下)
# ============================================================


# ============================================================
# 工具消费(扣 wallet · 不动 middleware/billing.py 红线)
# ============================================================


# ============================================================
# 退款 / revoke
# ============================================================

def refund_credit(
    cursor,
    customer_user_id: int,
    tool_points: int = 0,
    publish_points: int = 0,
    bonus_points: int = 0,
    related_order_id: Optional[str] = None,
    description: str = "退款回退额度",
    source: str = "refund_revoke",
) -> Dict[str, Any]:
    """
    退款时回退客户额度(把扣过的还回去)
    与 consume 反向 · 不验证上限(退款场景信任传入)

    source:流水来源标记(默认 refund_revoke)· [批2A] 工具失败退费传 'tool_fail_refund'
    便于幂等查询区分(工具退费 vs 充值退款 vs revoke)。
    """
    wallet = get_or_create_customer_wallet(cursor, customer_user_id)
    agent_user_id = wallet["agent_user_id"]
    new_tool = wallet["tool_credit_points"] + tool_points
    new_publish = wallet["publish_credit_points"] + publish_points
    new_bonus = wallet["bonus_credit_points"] + bonus_points
    total = tool_points + publish_points + bonus_points

    cursor.execute("""
        UPDATE customer_agent_credit_wallets
        SET tool_credit_points = %s,
            publish_credit_points = %s,
            bonus_credit_points = %s,
            total_consumed_points = GREATEST(0, total_consumed_points - %s),
            updated_at = NOW()
        WHERE customer_user_id = %s
    """, (new_tool, new_publish, new_bonus, total, customer_user_id))

    if tool_points > 0:
        _insert_credit_transaction(
            cursor, customer_user_id, agent_user_id, "refund", "tool", tool_points,
            new_tool, new_publish, new_bonus,
            related_order_id=related_order_id, source=source, description=description,
        )
    if publish_points > 0:
        _insert_credit_transaction(
            cursor, customer_user_id, agent_user_id, "refund", "publish", publish_points,
            new_tool, new_publish, new_bonus,
            related_order_id=related_order_id, source=source, description=description,
        )
    if bonus_points > 0:
        _insert_credit_transaction(
            cursor, customer_user_id, agent_user_id, "refund", "bonus", bonus_points,
            new_tool, new_publish, new_bonus,
            related_order_id=related_order_id, source=source, description=description,
        )
        cursor.execute("SAVEPOINT channel_tier_refund_grant_sync")
        try:
            from config.pricing_config import get_bonus_validity_months
            from services.bonus_grants import create_grant
            from services.channel_tier import is_channel_tier_enabled

            if is_channel_tier_enabled(cursor):
                ref = related_order_id or f"{source}:no_order"
                create_grant(
                    cursor,
                    owner_type="customer",
                    owner_id=customer_user_id,
                    granted_points=int(bonus_points),
                    grant_type="admin_adjust",
                    grant_key=f"refund:{ref}:{customer_user_id}:{int(bonus_points)}",
                    expires_months=get_bonus_validity_months(),
                    related_order_id=related_order_id,
                    note=description,
                )
                _log_bonus_grant_reconcile(cursor, customer_user_id, "refund_credit")
            cursor.execute("RELEASE SAVEPOINT channel_tier_refund_grant_sync")
        except Exception as exc:
            cursor.execute("ROLLBACK TO SAVEPOINT channel_tier_refund_grant_sync")
            cursor.execute("RELEASE SAVEPOINT channel_tier_refund_grant_sync")
            logger.warning(
                "[refund_credit] bonus grant compensation failed · customer=%s order=%s points=%s: %s",
                customer_user_id,
                related_order_id,
                bonus_points,
                exc,
            )

    return {
        "tool_credit_points": new_tool,
        "publish_credit_points": new_publish,
        "bonus_credit_points": new_bonus,
    }


def compute_unspent_from_order(
    cursor,
    customer_user_id: int,
    related_order_id: str,
) -> Dict[str, int]:
    """
    [Codex r2 P0-2] 算指定订单划拨的还剩多少未消费(FIFO 简化)
    防退款时撤错其他订单的余额

    算法:
        本订单 allocated_by_pool = SUM(allocate WHERE related_order_id=X) per pool
        本订单 created_at_min = min allocate timestamp
        本订单后所有消费总量(任意来源) = SUM(consume WHERE created_at >= 本订单时间)
        本订单未消费(FIFO) = max(0, allocated - consumed_after_this_order)

        FIFO 假设:客户使用工具时 · 系统先扣最早的 allocate · 后扣的后扣
        这跟 consume_credit 的 bonus-first / tool-second 实际策略不完全一致
        但用作退款 revoke 上限 · 仍然保守安全(算的更少 · 不会撤错给别订单)

    返回:
        {
            "tool_unspent": ...,
            "publish_unspent": ...,
            "bonus_unspent": ...,
        }
    """
    # 1. 本订单各池总划拨
    cursor.execute("""
        SELECT pool, SUM(points) AS alloc_total, MIN(created_at) AS first_at
        FROM customer_credit_transactions
        WHERE customer_user_id = %s AND related_order_id = %s AND type = 'allocate'
        GROUP BY pool
    """, (customer_user_id, related_order_id))
    by_pool = {}
    first_at = None
    for row in cursor.fetchall():
        pool = row["pool"] if isinstance(row, dict) else row[0]
        total = row["alloc_total"] if isinstance(row, dict) else row[1]
        ft = row["first_at"] if isinstance(row, dict) else row[2]
        by_pool[pool] = int(total or 0)
        if first_at is None or (ft and ft < first_at):
            first_at = ft

    if not by_pool or first_at is None:
        return {"tool_unspent": 0, "publish_unspent": 0, "bonus_unspent": 0}

    # 2. [BUG-P2] 真 FIFO 分账(防多订单跨单误算致双拿):
    #    原 "first_at 之后所有 consume 全算本订单" 在多订单下会把【更早订单】的消费也算进本订单 →
    #    退较晚订单时 unspent 低估 → revoke 少撤 → 客户既退现金又留额度(双拿)。单订单无此问题。
    #    FIFO 正解:本订单 allocate【之前】的其他订单先吸收 consume,剩余 consume 才落到本订单(clamp 到本订单 allocate)。
    result = {}
    for pool, alloc in by_pool.items():
        cursor.execute("""
            SELECT
              COALESCE(SUM(CASE WHEN type='allocate' AND created_at < %s THEN points ELSE 0 END), 0) AS prior_alloc,
              COALESCE(SUM(CASE WHEN type='consume' THEN -points ELSE 0 END), 0) AS total_consume
            FROM customer_credit_transactions
            WHERE customer_user_id = %s AND pool = %s
        """, (first_at, customer_user_id, pool))
        r = cursor.fetchone()
        prior_alloc = int((r["prior_alloc"] if isinstance(r, dict) else r[0]) or 0)
        total_consume = int((r["total_consume"] if isinstance(r, dict) else r[1]) or 0)
        # FIFO:先扣本订单之前的 allocate,剩余 consume 才落到本订单(max(0,..) clamp 不超本订单 allocate)
        consumed_from_this = max(0, total_consume - prior_alloc)
        result[pool] = max(0, alloc - consumed_from_this)

    return {
        "tool_unspent": result.get("tool", 0),
        "publish_unspent": result.get("publish", 0),
        "bonus_unspent": result.get("bonus", 0),
    }


# ============================================================
# 长任务冻结链(V3.5 customer_credit_freezes)· **历史账本只读 · 仅结算存量**
#
# [单账本接线 2026-08-17] 造冻结的那一半(`freeze_customer_credit`)已删除 ——
# `middleware/billing.py:1514` 的 freeze_points 硬编码返回 freeze_table='legacy',
# 结构上不可能再产生 v35 冻结(生产实证:customer_credit_freezes 0 行 frozen)。
#
# commit / release 两个**结算**入口刻意保留:工单 WALLET_SINGLE_LEDGER §4.2 点名
# 「切换瞬间若有在途 v35 冻结,必须仍能正确结算」。billing 的 _route_freeze_table
# 按冻结记录【实际所在表】路由(不看客户当前身份),命中 v35 才会走到这里。
#   · commit = 翻 status='committed'(钱在 freeze 时已实扣 · 余额不动)
#   · release = refund_credit 按拆分退回三池(退的是历史账本,与当前业务无关)
# ============================================================


def _lock_customer_freeze(cursor, freeze_id=None, task_ref=None, customer_user_id=None):
    """行锁定位 customer_credit_freezes(freeze_id 优先 · 否则 task_ref 取最近 frozen)。
    [A1] 有 customer_user_id 时带约束:freeze_id 跨 point_freezes/customer_credit_freezes 两表独立自增
    必撞号 → 不带 customer 约束会锁到别的客户同 id 行(跨客户错退)。"""
    cust_cond = " AND customer_user_id = %s" if customer_user_id is not None else ""
    if freeze_id:
        params = (freeze_id, customer_user_id) if customer_user_id is not None else (freeze_id,)
        cursor.execute(
            f"SELECT * FROM customer_credit_freezes WHERE id = %s{cust_cond} FOR UPDATE",
            params,
        )
    elif task_ref:
        params = (task_ref, customer_user_id) if customer_user_id is not None else (task_ref,)
        cursor.execute(
            f"SELECT * FROM customer_credit_freezes WHERE task_ref = %s{cust_cond} AND status = 'frozen' "
            "ORDER BY id DESC LIMIT 1 FOR UPDATE",
            params,
        )
    else:
        return None
    return cursor.fetchone()


def commit_customer_freeze(cursor, freeze_id=None, task_ref=None, reason=None,
                           customer_user_id=None) -> Dict[str, Any]:
    """V3.5 客户长任务【结算】= 翻 status='committed'(钱已在 freeze 实扣 · 坐实=不退 · 余额不动)。
    幂等:status != 'frozen' 返 idempotent no-op(防重复 settle)。
    [A1] customer_user_id 传入则定位带约束(防跨表撞号锁错客户)。
    """
    fz = _lock_customer_freeze(cursor, freeze_id, task_ref, customer_user_id)
    if not fz:
        return {"success": False, "reason": "未找到冻结记录", "v35_customer_credit": True}
    if fz["status"] != "frozen":
        return {"success": True, "idempotent": True, "status": fz["status"], "v35_customer_credit": True}

    cursor.execute("""
        UPDATE customer_credit_freezes
           SET status = 'committed', committed_at = NOW(), reason = COALESCE(%s, reason)
         WHERE id = %s
    """, (reason, fz["id"]))
    logger.info(
        f"[CustomerFreeze] commit id={fz['id']} customer={fz['customer_user_id']} "
        f"amount={fz['amount_total']}"
    )
    return {"success": True, "freeze_id": fz["id"], "amount": fz["amount_total"], "v35_customer_credit": True}


def release_customer_freeze(cursor, freeze_id=None, task_ref=None, reason=None,
                            customer_user_id=None) -> Dict[str, Any]:
    """V3.5 客户长任务【释放】= refund_credit 按 freeze 拆分退回三池 + 翻 status='released'。
    幂等:status != 'frozen' 返 idempotent no-op(防重复退 · 防 sweeper 重退)。
    source='tool_fail_refund'(已在 2A 白名单 · 免改 source CHECK)。
    [A1] customer_user_id 传入则定位带约束(防跨表撞号锁错客户)。
    """
    fz = _lock_customer_freeze(cursor, freeze_id, task_ref, customer_user_id)
    if not fz:
        return {"success": False, "reason": "未找到冻结记录", "v35_customer_credit": True}
    if fz["status"] != "frozen":
        return {"success": True, "idempotent": True, "status": fz["status"], "v35_customer_credit": True}

    customer_user_id = fz["customer_user_id"]
    amount_tool = int(fz.get("amount_tool", 0) or 0)
    amount_publish = int(fz.get("amount_publish", 0) or 0)
    amount_bonus = int(fz.get("amount_bonus", 0) or 0)

    if amount_tool > 0 or amount_publish > 0 or amount_bonus > 0:
        refund_credit(
            cursor, customer_user_id,
            tool_points=amount_tool, publish_points=amount_publish, bonus_points=amount_bonus,
            related_order_id=fz.get("task_ref") or f"CFRZ-{fz['id']}",
            description=reason or "长任务失败释放",
            source="tool_fail_refund",
        )

    cursor.execute("""
        UPDATE customer_credit_freezes
           SET status = 'released', released_at = NOW(), reason = COALESCE(%s, reason)
         WHERE id = %s
    """, (reason, fz["id"]))
    logger.info(
        f"[CustomerFreeze] release id={fz['id']} customer={customer_user_id} amount={fz['amount_total']} "
        f"(tool={amount_tool} publish={amount_publish} bonus={amount_bonus})"
    )
    return {"success": True, "freeze_id": fz["id"], "amount": fz["amount_total"], "v35_customer_credit": True}


# ============================================================
# 用量预估("约可跑 N 次")
# ============================================================
