"""聚合钱包 ↔ `dealer_inventory_lots` 的直接对侧写入(**非转售链路**专用)。

存在的理由(2026-08-12 生产实测):
    `docs/SYSTEM_TRUTH/08_billing.md` §5.3 写明
        「聚合钱包只是缓存式总量,有限 lot 才是成本与所有权 SSOT。
          两者不一致时停止交易,不允许自动猜平。」
    但线下划拨链路(`services.agent_inventory.allocate_offline`)**只扣聚合钱包、
    不消 lot**,而 `services.inventory_audit` 的对账等式只比
    「钱包 vs `agent_inventory_transactions` 流水」—— lot 这一维**没有任何判据**。
    结果:生产已经漂移且没人看得见
        u125  钱包 833 / lot 10833   (drift -10000)
        u133  钱包   0 / lot  5416   (drift  -5416)   ← 2026-08-12 应急划拨造成

本模块只做一件事:给**非转售**的库存进出补上 lot 侧的对侧写入,
让新链路(admin 调整 / admin 代划拨 / 库存自用)**不再制造新的漂移**。

🔴 边界(不要越界):
  - 转售 / JIT / 消费者订单仍走 `dealer_inventory_resale` 的 reserve→settle 两段式,
    本模块**不介入**,也不复制那套预占语义。
  - 本模块**不修既有漂移**。存量处置是单独的资金动作(需 Owner 授权 + 备份 + dry-run)。
  - 消费不足时 **fail-closed**(抛 `ResaleError`),绝不"自动猜平"。

判据设计(为什么这些函数值得被锁):
  `lot_drift_rows()` 是**真判据**,不是恒真断言 —— 它比对两张独立表的聚合值,
  数据层造出漂移它必须返回非空行;两边一致时必须返回空。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("GEO-InventoryLotLedger")

# `chk_dealer_lot_source_kind` 只允许这四个值。admin 直接入库的批次没有转售血缘,
# 语义最接近「开账批次」。🔴 不要为了好看去改这条 CHECK ——
# 本仓有过 CHECK 加值导致现役代码失去可重启性的事故先例。
ADMIN_CREDIT_SOURCE_KIND = "opening_balance"


def _resale():
    from services import dealer_inventory_resale

    return dealer_inventory_resale


def consume_lots_fifo(
    cur,
    owner_agent_user_id: int,
    points: int,
    *,
    reason: str,
) -> Dict[str, Any]:
    """按 FIFO 消耗 owner 的有限 lot · 复用既有 `fifo_allocations` 规划器。

    🔴 fail-closed:lot 不足直接抛 `ResaleError(SELLER_INVENTORY_INSUFFICIENT)`,
       调用方必须整体回滚。绝不允许"钱包扣了 lot 没消"这种半边账。

    返回 {"allocations": [...], "points": N, "cost_basis_cents": M}
    """
    resale = _resale()
    points = int(points)
    if points <= 0:
        return {"allocations": [], "points": 0, "cost_basis_cents": 0}

    # 与转售链同一把行锁顺序:先按 acquired_at,lot_id 排序取,再逐条 CAS 扣减。
    allocations = resale.fifo_allocations(cur, int(owner_agent_user_id), points)
    total_cost = 0
    for allocation in allocations:
        take = int(allocation["points"])
        cost = int(allocation["cost_basis_cents"])
        cur.execute(
            """UPDATE dealer_inventory_lots
               SET remaining_points=remaining_points-%s,
                   remaining_cost_cents=remaining_cost_cents-%s,
                   status=CASE WHEN remaining_points=%s AND reserved_points=0
                               THEN 'consumed' ELSE 'active' END,
                   updated_at=NOW()
               WHERE lot_id=%s AND owner_agent_user_id=%s AND status='active'
                 AND remaining_points >= %s AND remaining_cost_cents >= %s""",
            (
                take, cost, take, str(allocation["lot_id"]), int(owner_agent_user_id),
                take, cost,
            ),
        )
        if cur.rowcount != 1:
            # 并发下另一笔已经吃掉了这批 —— 不重试、不降级,整笔回滚由调用方负责。
            raise resale.ResaleError(
                "SELLER_INVENTORY_INSUFFICIENT",
                f"库存批次 {allocation['lot_id']} 在扣减时余量不足({reason})",
            )
        total_cost += cost
    logger.info(
        "[LotLedger] consume owner=%s points=%s lots=%s reason=%s",
        owner_agent_user_id, points, [a["lot_id"] for a in allocations], reason,
    )
    return {"allocations": allocations, "points": points, "cost_basis_cents": total_cost}


def credit_admin_lot(
    cur,
    owner_agent_user_id: int,
    points: int,
    *,
    pricing_version: str,
    evidence: Dict[str, Any],
    cost_basis_cents: Optional[int] = None,
) -> Dict[str, Any]:
    """admin 直接入库时建一条对侧 lot,使聚合钱包与 lot 仍然相等。

    🔴 `source_order_id` 一律留 NULL:`ux_dealer_lot_source_order` 是
       (source_order_id) 上的唯一索引,同一张充值订单若已被转售链建过 lot,
       这里再写就会撞唯一键。订单号进 `evidence_jsonb`,可查可审计。
    """
    resale = _resale()
    points = int(points)
    if points <= 0:
        raise ValueError("入库算力必须为正")
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError("admin 入库批次必须带审计证据")
    version = str(pricing_version or "").strip()
    if not version:
        raise ValueError("admin 入库批次必须带 pricing_version")

    if cost_basis_cents is None:
        from services.inventory_minting_guard import mint_cost_basis_cents

        cost_basis_cents = mint_cost_basis_cents(points)
    cost = int(cost_basis_cents)
    if cost <= 0:
        # `chk_dealer_lot_acquisition_cost_positive`
        raise ValueError("admin 入库批次成本必须为正")

    lot_id = resale._stable_id("DAL", f"{owner_agent_user_id}:{version}:{points}")
    cur.execute(
        """INSERT INTO dealer_inventory_lots
           (lot_id, owner_agent_user_id, source_order_id, source_transfer_id,
            original_points, remaining_points, reserved_points,
            acquisition_cost_cents, remaining_cost_cents, reserved_cost_cents,
            status, source_kind, pricing_version, quote_id, evidence_jsonb)
           VALUES (%s,%s,NULL,NULL,%s,%s,0,%s,%s,0,'active',%s,%s,NULL,%s::jsonb)
           ON CONFLICT (lot_id) DO NOTHING
           RETURNING lot_id""",
        (
            lot_id, int(owner_agent_user_id), points, points, cost, cost,
            ADMIN_CREDIT_SOURCE_KIND, version,
            json.dumps(evidence, ensure_ascii=False, sort_keys=True),
        ),
    )
    row = cur.fetchone()
    if not row:
        # 同一 (owner, version, points) 已建过 —— version 里带 request_id,
        # 因此这只可能是同一次请求的重放,直接当幂等命中。
        logger.info("[LotLedger] credit lot 幂等命中 lot=%s", lot_id)
        return {"lot_id": lot_id, "points": points, "cost_basis_cents": cost, "idempotent": True}
    logger.info(
        "[LotLedger] credit owner=%s points=%s lot=%s cost=%s",
        owner_agent_user_id, points, lot_id, cost,
    )
    return {"lot_id": lot_id, "points": points, "cost_basis_cents": cost, "idempotent": False}


def credit_channel_supply_lot(
    cur,
    owner_agent_user_id: int,
    points: int,
    *,
    pricing_version: str,
    acquisition_cost_cents: int,
    evidence: Dict[str, Any],
) -> Dict[str, Any]:
    """上游线下供货给下线时,在**下线**侧建对侧 lot(P0 热修 §2)。

    🔴 为什么必须建:上游那边 `consume_lots_fifo` 消掉了 lot,下线的聚合钱包却增加了。
       只写钱包不建 lot,下线立刻出现 `wallet_side > lot_side` 的漂移 ——
       正是 invchain 那笔 −15,416 的同型(而且当时对账天天报绿)。
       两侧同事务:上游 lot↓钱包↓、下线 lot↑钱包↑,系统总量守恒。

    `source_kind='direct_resale'`:下线的这批算力**确实**是从上游经销商处取得的。
    该值早已在 `chk_dealer_lot_source_kind` 白名单里(2026-08-13 生产现取实证),
    因此本次**零迁移** —— 不碰 CHECK(往 CHECK 加值不是 additive,本仓有事故先例)。

    与 `credit_admin_lot` 同规矩:
      · `source_order_id` 留 NULL(`ux_dealer_lot_source_order` 唯一索引,线下供货无订单);
      · `root_order_id` / `source_hop_seq` 一起留 NULL(`chk_dealer_lot_jit_lineage`
        要求两者同时为空或同时有值);线下供货不走 JIT 血缘;
      · `source_transfer_id` 留 NULL(其外键指向 `dealer_inventory_transfers`,
        线下供货不建 transfer 行);上下游对应关系写进 `evidence_jsonb`,可查可审计。
    """
    resale = _resale()
    points = int(points)
    if points <= 0:
        raise ValueError("供货算力必须为正")
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError("供货批次必须带审计证据")
    version = str(pricing_version or "").strip()
    if not version:
        raise ValueError("供货批次必须带 pricing_version")
    cost = int(acquisition_cost_cents)
    if cost <= 0:
        # `chk_dealer_lot_acquisition_cost_positive`
        raise ValueError("供货批次成本必须为正")

    lot_id = resale._stable_id("CSL", f"{owner_agent_user_id}:{version}:{points}")
    cur.execute(
        """INSERT INTO dealer_inventory_lots
           (lot_id, owner_agent_user_id, source_order_id, source_transfer_id,
            original_points, remaining_points, reserved_points,
            acquisition_cost_cents, remaining_cost_cents, reserved_cost_cents,
            status, source_kind, pricing_version, quote_id, evidence_jsonb)
           VALUES (%s,%s,NULL,NULL,%s,%s,0,%s,%s,0,'active','direct_resale',%s,NULL,%s::jsonb)
           ON CONFLICT (lot_id) DO NOTHING
           RETURNING lot_id""",
        (
            lot_id, int(owner_agent_user_id), points, points, cost, cost, version,
            json.dumps(evidence, ensure_ascii=False, sort_keys=True),
        ),
    )
    row = cur.fetchone()
    if not row:
        # version 里带幂等 ref,因此重复只可能是同一次请求的重放。
        logger.info("[LotLedger] supply lot 幂等命中 lot=%s", lot_id)
        return {"lot_id": lot_id, "points": points, "cost_basis_cents": cost, "idempotent": True}
    logger.info(
        "[LotLedger] supply owner=%s points=%s lot=%s cost=%s",
        owner_agent_user_id, points, lot_id, cost,
    )
    return {"lot_id": lot_id, "points": points, "cost_basis_cents": cost, "idempotent": False}


# ============================================================
# 对账维度:钱包 vs lot
# ============================================================

def lot_drift_rows(cur, agent_user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """返回聚合钱包与 lot 对不上的服务商明细(对上的不返回)。

    口径:`paid_inventory_points + frozen_inventory_points`
          vs 未消耗 lot 的 `remaining_points + reserved_points`。
    🔴 `bonus_inventory_points` **不进等式** —— 赠送库存按 08_billing.md §5.3
       「不能变成 paid、不能作为有价库存转售」,本来就不建 lot。
    """
    params: List[Any] = []
    where = ""
    if agent_user_id is not None:
        where = "WHERE w.agent_user_id = %s"
        params.append(int(agent_user_id))
    cur.execute(
        f"""
        SELECT w.agent_user_id,
               w.paid_inventory_points + w.frozen_inventory_points AS wallet_side,
               COALESCE(l.lot_side, 0) AS lot_side,
               (w.paid_inventory_points + w.frozen_inventory_points)
                 - COALESCE(l.lot_side, 0) AS drift_points
        FROM agent_inventory_wallets w
        LEFT JOIN (
            SELECT owner_agent_user_id,
                   SUM(remaining_points + reserved_points) AS lot_side
            FROM dealer_inventory_lots
            WHERE status <> 'consumed'
            GROUP BY owner_agent_user_id
        ) l ON l.owner_agent_user_id = w.agent_user_id
        {where}
        """,
        tuple(params),
    )
    rows = []
    for raw in cur.fetchall() or []:
        row = dict(raw) if isinstance(raw, dict) else {
            "agent_user_id": raw[0], "wallet_side": raw[1],
            "lot_side": raw[2], "drift_points": raw[3],
        }
        if int(row["drift_points"] or 0) != 0:
            rows.append({
                "agent_user_id": int(row["agent_user_id"]),
                "wallet_side": int(row["wallet_side"] or 0),
                "lot_side": int(row["lot_side"] or 0),
                "drift_points": int(row["drift_points"] or 0),
            })
    return rows


def lot_drift_summary(cur) -> Dict[str, Any]:
    """给对账等式用的 lot 维度汇总(明细 + 总差额)。"""
    rows = lot_drift_rows(cur)
    return {
        "drift_agent_count": len(rows),
        "drift_total_points": sum(row["drift_points"] for row in rows),
        "drift_abs_points": sum(abs(row["drift_points"]) for row in rows),
        "agents": rows[:50],
    }


def drift_points_of(cur, agent_user_id: int) -> int:
    """单个服务商当前的钱包-lot 差额(对得上时为 0)。"""
    rows = lot_drift_rows(cur, agent_user_id=int(agent_user_id))
    return int(rows[0]["drift_points"]) if rows else 0


def assert_drift_unchanged(cur, agent_user_id: int, *, before: int, action: str) -> int:
    """本次操作**不得改变**该服务商的钱包-lot 差额。

    🔴 判据写成【前后差分】而不是焊死"必须为 0":
       u125 / u133 身上已有存量漂移(-10000 / -5416),存量处置是单独的资金动作
       (Owner 2026-08-12:本轮不动)。焊死为 0 会让新链路对这两个账号**恒红** ——
       而恒红的判据和恒真的判据一样没有判别力,顺带把本来要解救的人继续锁死。
       差分口径既放行存量、又能抓住"钱包扣了 lot 没消"的新漏账:
       lot-blind 的实现会把差额从 -5416 变成 -4416,当场转红。
    """
    after = drift_points_of(cur, int(agent_user_id))
    if after != int(before):
        raise _resale().ResaleError(
            "INVENTORY_LOT_DRIFT",
            f"{action} 改变了聚合钱包与库存批次的差额({before} → {after}),已整体拒绝",
        )
    return after
