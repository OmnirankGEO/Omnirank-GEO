"""客户算力授予 / 回收 —— 单账本落点（2026-07-27）

Owner 2026-07-27：「不能有两本账，用户只有充值算力和赠送算力」
「服务商补货也是补充值算力，只是会有一定的赠送算力给他」。

服务商「划拨算力给客户」这个**业务动作保留**（工单 §2 已确认），
只是记账落点从 `customer_agent_credit_wallets` 三池改成客户自己的 `user_wallets`：

| 原信用钱包池 | 现落点 |
|---|---|
| `tool_credit_points` + `publish_credit_points` | `user_wallets.paid_points`（充值算力） |
| `bonus_credit_points` | `user_wallets.bonus_points`（赠送算力） |

映射与阶段①迁移脚本 `wallet_credit_merge_migrate_2026_07_27.sql` **完全一致** ——
两处必须同口径，否则迁移与新增会各记一套。

本模块替代 `services.customer_credit` 的 `allocate_credit` / `revoke_credit`
在【客户侧】的作用；服务商库存侧（`agent_inventory`）的逻辑不属本模块，保持原样。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("GEO-CustomerEntitlement")

#: 授予/回收在平台流水里的类型。与充值(recharge)、消费(consume)区分开，
#: 便于财务按"服务商划拨"单独归集。
GRANT_TX_TYPE = "agent_grant"
REVOKE_TX_TYPE = "agent_grant_revoke"

#: 单账本 FIFO 归因的资金轨。point_transactions.point_type 生产实测只有这两个值。
_FIFO_TRACKS = ("paid", "bonus")

#: freeze 流水的 order_id 形如 `FRZ<point_freezes.id>-<task_ref>`。
#: 生产实测 398/398 条 freeze 流水都能按此解析并命中 point_freezes.id。
_FRZ_ID_RE = re.compile(r"^FRZ(\d+)-")


def _still_frozen_freeze_ids(cursor, user_id: int) -> set:
    """该用户【尚未 commit/release】的冻结记录 id 集合。

    工单 §2.4:冻结中的算力「不算已消费、也不算可撤余量」。
    · 不算已消费 → FIFO 重放里**跳过**该 freeze 行(否则它会去吃最老的桶,
      把那张订单的未消费额压低 → 少撤,客户吃亏);
    · 不算可撤余量 → 由三层 min 的**钱包余额层**保证(freeze 当场就把余额扣走了)。
    两条各由一层负责,不重复扣。
    """
    try:
        cursor.execute(
            "SELECT id FROM point_freezes WHERE user_id = %s AND status = 'frozen'",
            (user_id,),
        )
        rows = cursor.fetchall() or []
    except Exception as exc:  # 表缺失/查询失败 → 不跳过(保守:当已消费 → 少撤,不会超收)
        logger.warning("[FIFO] 读 point_freezes 失败,冻结行不豁免(保守): %s", exc)
        return set()
    out = set()
    for r in rows:
        v = r["id"] if isinstance(r, dict) else r[0]
        if v is not None:
            out.add(int(v))
    return out


def compute_unspent_fifo(cursor, user_id: int, related_order_id: str) -> Dict[str, int]:
    """单账本下的「单均未消费」= point_transactions FIFO 归因重放(paid/bonus 双轨)。

    这不是新规则,是**老 FIFO 语义换底座**:07-29 单账本收敛后,客户算力只剩
    `user_wallets` 一处,老的 `customer_credit.compute_unspent_from_order` 读的
    `customer_credit_transactions` 对切换后新建的订单**永远是空的** → 返回 0 →
    撤回恒 0。本函数把同一套 FIFO 语义搬到 `point_transactions` 上。

    ── 算法 ──
    按 (created_at, id) 时间序、**分 paid/bonus 两轨独立**重放:
      · 进(movement > 0)→ 各自成桶,桶键 = 该行 order_id,按序排队;
      · 出(movement < 0)→ 依次吃**最老的未耗尽桶**;
      · 该订单未消费 = 桶键 == related_order_id 的桶的剩余量之和。

    ── 🔴 movement 为什么取 balance_after 差分而不是 amount ──
    生产实测:冻结链会写**两行**——`freeze`(真扣余额)+ 随后 commit 时的
    `consume`(order_id 前缀 CMT · `balance_after` 与上一行**相同** = 只是记账)。
    直接按 `amount` 求和会把每一笔"冻结后落地"的消费**算两遍**(实测 380 条这种记账行)。
    `balance_after` 差分是钱包的真实位移,且**与 type 词表解耦** —— 将来新增 type
    自动进等式,不会像 type 白名单那样天生漏算(inventory_audit 的同款教训)。
    实测 124 条 (user, point_type) 轨里 121 条的末行 balance_after 与
    `user_wallets` 现值逐字相同(3 条历史漂移,与本函数无因果)。

    ── 保守偏置(与老函数同向)──
    `release`(冻结释放)按**新桶**处理,不回补原桶:生产 18 条 release 流水
    与 freeze 流水**按 task_ref 0/18 配得上**,硬造解析不如显式保守。
    后果是"原订单未消费被低估" → **少撤**,绝不会去吃第二张订单的余量。
    老 `compute_unspent_from_order` 的 docstring 自己写的也是这个偏置:
    「算的更少 · 不会撤错给别订单」。

    返回 {"paid_unspent": int, "bonus_unspent": int}(均 >= 0)。
    """
    if not related_order_id:
        return {"paid_unspent": 0, "bonus_unspent": 0}

    frozen_ids = _still_frozen_freeze_ids(cursor, user_id)

    cursor.execute(
        """
        SELECT id, type, point_type, amount, balance_after, order_id
          FROM point_transactions
         WHERE user_id = %s AND point_type = ANY(%s)
         ORDER BY created_at ASC, id ASC
        """,
        (user_id, list(_FIFO_TRACKS)),
    )
    rows = cursor.fetchall() or []

    result = {"paid_unspent": 0, "bonus_unspent": 0}
    # 每轨独立:{track: (prev_balance_after, [[bucket_key, remaining], ...])}
    prev_bal: Dict[str, Optional[int]] = {t: None for t in _FIFO_TRACKS}
    buckets: Dict[str, List[list]] = {t: [] for t in _FIFO_TRACKS}

    for r in rows:
        g = (lambda k, i: r[k] if isinstance(r, dict) else r[i])
        track = g("point_type", 2)
        if track not in buckets:
            continue
        amount = int(g("amount", 3) or 0)
        bal_after = int(g("balance_after", 4) or 0)
        oid = g("order_id", 5)
        row_type = g("type", 1)

        # movement:首行无前值 → 回落 amount;其余取 balance_after 差分
        pb = prev_bal[track]
        mv = amount if pb is None else (bal_after - pb)
        prev_bal[track] = bal_after   # 🔴 无论是否豁免都要推进,否则后续差分全错

        # §2.4 冻结豁免:尚未 commit 的 freeze 不参与归因
        if row_type == "freeze" and oid:
            m = _FRZ_ID_RE.match(str(oid))
            if m and int(m.group(1)) in frozen_ids:
                continue

        if mv > 0:
            buckets[track].append([oid, mv])
        elif mv < 0:
            need = -mv
            # 🔴 撤回(agent_grant_revoke)是**对某一张单的定向冲正**,不是消费:
            #    必须先扣它自己那只桶,扣不完的余量才回落 FIFO。
            #    否则「退 B」会去吃最老的 A 桶 → 两个退款顺序的总撤回不相等
            #    (实测:先 A 后 B = 1200,先 B 后 A = 800),守恒被破坏。
            if row_type == REVOKE_TX_TYPE and oid:
                for b in buckets[track]:
                    if need <= 0:
                        break
                    if b[0] == oid:
                        take = b[1] if b[1] <= need else need
                        b[1] -= take
                        need -= take
            for b in buckets[track]:
                if need <= 0:
                    break
                take = b[1] if b[1] <= need else need
                b[1] -= take
                need -= take
            # need 仍 > 0 = 消费超过已知入账(历史数据不全)· 不倒扣,不报错

    for track, key in (("paid", "paid_unspent"), ("bonus", "bonus_unspent")):
        result[key] = sum(b[1] for b in buckets[track] if b[0] == related_order_id)
    return result


def has_legacy_credit_ledger_history(cursor, user_id: int, related_order_id: str) -> bool:
    """该订单在**老台账**(customer_credit_transactions)里有没有 allocate 历史。

    工单 §2.2 的新旧分流判据 —— **不按日期硬切**:
      有 → 走老 `customer_credit.compute_unspent_from_order`(在册 7 张单不换轨);
      无 → 走本模块的 `compute_unspent_fifo`。
    按日期切会在边界上两头不靠;按"有没有账"切,谁有账谁用那本账,天然无缝。
    """
    if not related_order_id:
        return False
    try:
        cursor.execute(
            """
            SELECT 1 FROM customer_credit_transactions
             WHERE customer_user_id = %s AND related_order_id = %s AND type = 'allocate'
             LIMIT 1
            """,
            (user_id, related_order_id),
        )
        return cursor.fetchone() is not None
    except Exception as exc:
        # 老台账表读不到 → 当作没有历史,走新 FIFO(新账本是唯一还在写的那本)
        logger.warning("[FIFO] 读老台账失败,按无历史处理(走 FIFO): %s", exc)
        return False


def _wallet_balances(cursor, customer_user_id: int) -> Optional[Dict[str, int]]:
    cursor.execute(
        "SELECT paid_points, bonus_points FROM user_wallets WHERE user_id = %s FOR UPDATE",
        (customer_user_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    if isinstance(row, dict):
        return {"paid": int(row["paid_points"] or 0), "bonus": int(row["bonus_points"] or 0)}
    return {"paid": int(row[0] or 0), "bonus": int(row[1] or 0)}


def already_granted(cursor, related_order_id: str, source: str) -> bool:
    """幂等检查：同一 (order_id, source) 是否已授予过。

    🔴 幂等键从 `customer_credit_transactions` 搬到 `point_transactions` ——
    原来 agent_rebate 等路径把幂等标记写在信用流水表里，那张表停写之后
    幂等查询会恒为空 → 支付二次回调重复发钱（判定表 A2）。
    """
    if not related_order_id:
        return False
    cursor.execute(
        "SELECT 1 FROM point_transactions "
        "WHERE type = %s AND order_id = %s AND description LIKE %s LIMIT 1",
        (GRANT_TX_TYPE, str(related_order_id), f"%[{source}]%"),
    )
    return cursor.fetchone() is not None


def grant_to_customer(
    cursor,
    *,
    customer_user_id: int,
    agent_user_id: Optional[int] = None,
    paid_points: int = 0,
    bonus_points: int = 0,
    related_order_id: Optional[str] = None,
    source: str = "offline_allocation",
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """给客户加算力（落 user_wallets + 写 point_transactions）。

    paid_points 对应原 tool+publish（充值算力），bonus_points 对应原 bonus_credit（赠送算力）。
    调用方若原本分 tool/publish 两个数，先相加再传进来 —— 单账本没有这个区分了。
    """
    from db.wallet_db import insert_transaction

    paid_points = int(paid_points or 0)
    bonus_points = int(bonus_points or 0)
    if paid_points < 0 or bonus_points < 0:
        raise ValueError("授予算力不能为负")
    if paid_points == 0 and bonus_points == 0:
        return {"paid_points": 0, "bonus_points": 0, "skipped": True}

    before = _wallet_balances(cursor, customer_user_id)
    if before is None:
        # 显式失败，不静默建行 —— 与 billing 的 NO_WALLET 口径一致
        raise ValueError(f"客户 {customer_user_id} 没有钱包行，拒绝静默处理")

    cursor.execute(
        """
        UPDATE user_wallets
           SET paid_points = paid_points + %s,
               bonus_points = bonus_points + %s,
               updated_at = NOW()
         WHERE user_id = %s
        RETURNING paid_points, bonus_points
        """,
        (paid_points, bonus_points, customer_user_id),
    )
    after_row = cursor.fetchone()
    after_paid = int(after_row["paid_points"] if isinstance(after_row, dict) else after_row[0])
    after_bonus = int(after_row["bonus_points"] if isinstance(after_row, dict) else after_row[1])

    # description 里带 [source] 标记 —— already_granted 的幂等查询依赖它
    tag = f"[{source}]"
    base_desc = description or "服务商划拨算力"
    if paid_points > 0:
        insert_transaction(
            cursor, customer_user_id, GRANT_TX_TYPE, "paid", paid_points, after_paid,
            description=f"{base_desc} {tag}", order_id=related_order_id,
        )
    if bonus_points > 0:
        insert_transaction(
            cursor, customer_user_id, GRANT_TX_TYPE, "bonus", bonus_points, after_bonus,
            description=f"{base_desc}（赠送）{tag}", order_id=related_order_id,
        )

    logger.info(
        "[Entitlement] grant customer=%s agent=%s paid=+%s bonus=+%s source=%s order=%s",
        customer_user_id, agent_user_id, paid_points, bonus_points, source, related_order_id,
    )
    return {"paid_points": after_paid, "bonus_points": after_bonus, "skipped": False}


def revoke_from_customer(
    cursor,
    *,
    customer_user_id: int,
    agent_user_id: Optional[int] = None,
    paid_points: int = 0,
    bonus_points: int = 0,
    related_order_id: Optional[str] = None,
    source: str = "refund_revoke",
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """从客户账上回收算力（服务商撤回划拨 / 退款回收）。

    🔴 按【当前余额】夹紧，绝不把余额扣成负数：客户可能已经消费掉一部分，
    回收只能回收还剩下的。原 revoke_credit 也是这个语义（三层 min）。
    """
    from db.wallet_db import insert_transaction

    paid_points = max(0, int(paid_points or 0))
    bonus_points = max(0, int(bonus_points or 0))
    if paid_points == 0 and bonus_points == 0:
        return {"revoked_paid": 0, "revoked_bonus": 0, "skipped": True}

    before = _wallet_balances(cursor, customer_user_id)
    if before is None:
        raise ValueError(f"客户 {customer_user_id} 没有钱包行，拒绝静默处理")

    take_paid = min(paid_points, before["paid"])
    take_bonus = min(bonus_points, before["bonus"])
    if take_paid == 0 and take_bonus == 0:
        return {"revoked_paid": 0, "revoked_bonus": 0, "skipped": True}

    cursor.execute(
        """
        UPDATE user_wallets
           SET paid_points = paid_points - %s,
               bonus_points = bonus_points - %s,
               updated_at = NOW()
         WHERE user_id = %s
        RETURNING paid_points, bonus_points
        """,
        (take_paid, take_bonus, customer_user_id),
    )
    after_row = cursor.fetchone()
    after_paid = int(after_row["paid_points"] if isinstance(after_row, dict) else after_row[0])
    after_bonus = int(after_row["bonus_points"] if isinstance(after_row, dict) else after_row[1])

    tag = f"[{source}]"
    base_desc = description or "服务商回收算力"
    if take_paid > 0:
        insert_transaction(
            cursor, customer_user_id, REVOKE_TX_TYPE, "paid", -take_paid, after_paid,
            description=f"{base_desc} {tag}", order_id=related_order_id,
        )
    if take_bonus > 0:
        insert_transaction(
            cursor, customer_user_id, REVOKE_TX_TYPE, "bonus", -take_bonus, after_bonus,
            description=f"{base_desc}（赠送）{tag}", order_id=related_order_id,
        )

    logger.info(
        "[Entitlement] revoke customer=%s agent=%s paid=-%s bonus=-%s source=%s order=%s",
        customer_user_id, agent_user_id, take_paid, take_bonus, source, related_order_id,
    )
    return {
        "revoked_paid": take_paid, "revoked_bonus": take_bonus,
        "paid_points": after_paid, "bonus_points": after_bonus, "skipped": False,
    }
