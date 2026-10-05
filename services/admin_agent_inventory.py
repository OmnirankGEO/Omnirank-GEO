"""服务商库存的**管理侧**入口 —— admin 调整 / admin 代划拨 / 服务商库存自用。

工单:`WO_INVENTORY_POINTS_DEADLOCK_2026-08-12.md` §P0-1 §P0-3

存在的理由(2026-08-12 P0 真实事故):
    服务商 u133 付了 ¥50、算力进了 `agent_inventory_wallets`,然后**三向锁死**:
      1 自己用不了(扣费链只认 `user_wallets`)
      2 转不出来(库存无自用/转本人钱包动作)
      3 划不出去(划拨要求已有商业绑定,而建绑定只有 admin 治理接口能做)
      4 后台救不了(`server.app.routes` 全扫:admin 前缀下**零个**库存调整/代划拨端点)
    → 只能由 Deploy 手工进数据库解救。本模块就是那个缺掉的后台入口。

🔴 三条实现纪律(直接对着这次事故写):
  1. **不新写扣减逻辑**。钱包侧一律复用
     `services.offline_allocation.allocate_offline_to_customer` /
     `services.agent_inventory.*`;lot 侧一律走 `services.inventory_lot_ledger`。
  2. **不新建绑定端点**。绑定缺失时同事务调
     `services.admin_user_governance.change_commercial_binding_cur()` ——
     审计 / CAS / 版本位全部继承,走完就**不会亮**「旧人工归属缺少直接审计」。
  3. **lot 差额前后守恒**。每个动作前后取 `drift_points_of()` 比对,
     不守恒即整体拒绝提交(这正是应急那次留下 u133 -5416 漂移的形态)。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from db.connection import get_db
from services import inventory_lot_ledger
from services.admin_user_governance import (
    GovernanceNotFound,
    change_commercial_binding_cur,
)

logger = logging.getLogger("GEO-AdminAgentInventory")

ACTION_ADJUST_INCREASE = "adjust_increase"
ACTION_ADJUST_DECREASE = "adjust_decrease"
ACTION_ALLOCATE = "allocate_to_customer"
ACTION_SELF_USE = "self_use_conversion"


class InventoryAdminError(Exception):
    """管理侧库存动作的可预期失败 · code 供 API 层映射状态码与判别锁使用。"""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        self.code = str(code)
        self.details = dict(details)
        super().__init__(message)


# ============================================================
# 共用前置
# ============================================================

def _require_reason(reason: Optional[str]) -> str:
    text = (reason or "").strip()
    if len(text) < 2:
        raise InventoryAdminError("REASON_REQUIRED", "必须填写操作原因(至少 2 个字)")
    return text[:500]


def _lock_provider(cur, agent_user_id: int) -> Dict[str, Any]:
    """锁住服务商身份行并校验其确实是服务商。"""
    cur.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (int(agent_user_id),))
    if not cur.fetchone():
        raise GovernanceNotFound(f"user {agent_user_id}")
    cur.execute(
        "SELECT COALESCE(agent_level,0) AS agent_level FROM user_wallets "
        "WHERE user_id=%s FOR UPDATE",
        (int(agent_user_id),),
    )
    wallet = cur.fetchone()
    if not wallet:
        raise InventoryAdminError(
            "BUSINESS_IDENTITY_SSOT_UNAVAILABLE", "该账号业务身份暂时无法确认,未执行库存操作"
        )
    if int(wallet["agent_level"] or 0) < 1:
        raise InventoryAdminError(
            "TARGET_NOT_SERVICE_PROVIDER",
            "库存只属于服务商;普通用户的算力请走「用户治理 → 钱包调整」",
        )
    return dict(wallet)


def _assert_not_platform_seller(cur, agent_user_id: int) -> None:
    """平台厂家账号有自己的批次发行入口,不走本模块。

    🔴 理由不是洁癖:admin 直接入库建的是 `opening_balance` 批次,而
       `PLATFORM_DORMANT_SOURCE_KINDS` 把 opening_balance 列为平台跳**永不消费**的
       休眠资产 —— 给厂家账号这样加库存,加进去的当场就是死钱。
       厂家账号请走 `POST /api/admin/dealer-resale/manufacturer-lots/issue`。
    """
    # 先探表:直接查不存在的表会把**调用方整个事务**打成 aborted
    # (同形态事故:try/except 包 SQL 而无 SAVEPOINT)。
    cur.execute("SELECT to_regclass('public.dealer_resale_global_settings') AS t")
    probe = cur.fetchone()
    if not (probe and probe["t"]):
        return
    cur.execute(
        "SELECT platform_seller_user_id FROM dealer_resale_global_settings LIMIT 1"
    )
    row = cur.fetchone()
    seller_id = int((row or {}).get("platform_seller_user_id") or 0)
    if seller_id and seller_id == int(agent_user_id):
        raise InventoryAdminError(
            "PLATFORM_SELLER_USE_ISSUANCE",
            "平台厂家账号的库存请走厂家批次发行入口,不走服务商库存调整",
        )


def _wallet_snapshot(cur, agent_user_id: int) -> Dict[str, int]:
    cur.execute(
        """SELECT paid_inventory_points, bonus_inventory_points, frozen_inventory_points
           FROM agent_inventory_wallets WHERE agent_user_id=%s""",
        (int(agent_user_id),),
    )
    row = cur.fetchone()
    if not row:
        return {"paid_inventory_points": 0, "bonus_inventory_points": 0,
                "frozen_inventory_points": 0}
    return {k: int(row[k] or 0) for k in
            ("paid_inventory_points", "bonus_inventory_points", "frozen_inventory_points")}


def _record_admin_action(
    cur, *, action: str, agent_user_id: int, customer_user_id: Optional[int],
    paid_points: int, bonus_points: int, related_order_id: Optional[str],
    operator_user_id: int, operator_username: Optional[str], reason: str,
    request_id: str, ip_address: Optional[str],
    before: Dict[str, Any], after: Dict[str, Any], evidence: Dict[str, Any],
) -> int:
    """留痕:**操作人 + 原因** 是工单验收判据 1 的硬要求。

    `(request_id, action)` 上有唯一索引 —— 同一个 `X-Request-ID` 重复提交
    直接撞唯一键,而不是悄悄扣第二次(资金动作,fail-closed 优于幂等重放)。
    """
    from psycopg2 import errors as pg_errors
    from psycopg2.extras import Json

    try:
        cur.execute("SAVEPOINT admin_inventory_action")
        cur.execute(
            """INSERT INTO agent_inventory_admin_actions(
                   action, agent_user_id, customer_user_id, paid_points, bonus_points,
                   related_order_id, operator_user_id, operator_username, reason,
                   request_id, ip_address, before_snapshot, after_snapshot, evidence_jsonb)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (
                action, int(agent_user_id),
                int(customer_user_id) if customer_user_id is not None else None,
                int(paid_points), int(bonus_points), related_order_id,
                int(operator_user_id), operator_username, reason, request_id, ip_address,
                Json(before), Json(after), Json(evidence),
            ),
        )
        action_id = int(cur.fetchone()["id"])
        cur.execute("RELEASE SAVEPOINT admin_inventory_action")
        return action_id
    except pg_errors.UniqueViolation as exc:
        cur.execute("ROLLBACK TO SAVEPOINT admin_inventory_action")
        raise InventoryAdminError(
            "DUPLICATE_REQUEST",
            "该请求编号已经执行过一次库存操作,已拒绝重复执行",
            request_id=request_id, action=action,
        ) from exc


# ============================================================
# P0-1 · admin 调整服务商库存
# ============================================================

def admin_adjust_inventory(
    agent_user_id: int, *, direction: str, paid_points: int, bonus_points: int,
    related_order_id: Optional[str], reason: str, operator_user_id: int,
    operator_username: Optional[str], request_id: str, ip_address: Optional[str],
) -> Dict[str, Any]:
    """管理员增/减服务商库存。

    - `increase` 走 `purchase_inventory_admin_adjust`(type `purchase_admin_adjust`),
      因此**必须**带一笔真实存在的 `related_order_id` ——
      这是 `inventory_minting_guard` 护栏 3 的硬要求
      (「把铸造从管理动作变成订单的必然结果」),不是本模块新加的门槛。
    - `decrease` 走 `admin_adjust_decrease`(type `admin_adjust`)。
    两个 type 早在 `agent_inventory_transactions_type_check` 白名单里,**不动那条 CHECK**。
    """
    from services.agent_inventory import (
        admin_adjust_decrease,
        purchase_inventory_admin_adjust,
    )
    from services.dealer_inventory_resale import ResaleError
    from services.inventory_minting_guard import MintingGuardError

    reason = _require_reason(reason)
    paid_points = int(paid_points or 0)
    bonus_points = int(bonus_points or 0)
    if paid_points < 0 or bonus_points < 0:
        raise InventoryAdminError("POINTS_INVALID", "算力必须非负")
    if paid_points == 0 and bonus_points == 0:
        raise InventoryAdminError("POINTS_INVALID", "充值库存与赠送库存至少填一个 > 0")
    if direction not in ("increase", "decrease"):
        raise InventoryAdminError("DIRECTION_INVALID", "调整方向只能是 increase / decrease")

    with get_db() as conn:
        cur = conn.cursor()
        _lock_provider(cur, agent_user_id)
        _assert_not_platform_seller(cur, agent_user_id)
        before = _wallet_snapshot(cur, agent_user_id)
        drift_before = inventory_lot_ledger.drift_points_of(cur, agent_user_id)

        try:
            if direction == "increase":
                purchase_inventory_admin_adjust(
                    cur, agent_user_id,
                    paid_points=paid_points, bonus_points=bonus_points,
                    admin_user_id=operator_user_id,
                    description=f"admin 库存调整 · {reason}",
                    related_order_id=related_order_id,
                )
                lot: Optional[Dict[str, Any]] = None
                if paid_points > 0:
                    # paid 才是"可售资产 + 成本资产"(08_billing.md §5.3),
                    # bonus 本来就不建 lot,也不进 lot 对账维度。
                    lot = inventory_lot_ledger.credit_admin_lot(
                        cur, agent_user_id, paid_points,
                        pricing_version=f"admin_adjust:{request_id}",
                        evidence={
                            "source": "admin_agent_inventory.adjust",
                            "request_id": request_id,
                            "operator_user_id": int(operator_user_id),
                            "related_order_id": related_order_id,
                            "reason": reason,
                        },
                    )
                action = ACTION_ADJUST_INCREASE
                evidence: Dict[str, Any] = {"lot": lot}
            else:
                admin_adjust_decrease(
                    cur, agent_user_id,
                    paid_points=paid_points, bonus_points=bonus_points,
                    admin_user_id=operator_user_id,
                    description=f"admin 库存冲减 · {reason}",
                )
                consumed = None
                if paid_points > 0:
                    consumed = inventory_lot_ledger.consume_lots_fifo(
                        cur, agent_user_id, paid_points, reason="admin 库存冲减",
                    )
                action = ACTION_ADJUST_DECREASE
                evidence = {"consumed_lots": (consumed or {}).get("allocations")}
        except MintingGuardError as exc:
            raise InventoryAdminError(
                "MINT_GUARD_REJECTED", str(exc), guard_code=exc.code,
            ) from exc
        except ResaleError as exc:
            raise InventoryAdminError("LOT_REJECTED", str(exc), lot_code=exc.code) from exc
        except ValueError as exc:
            raise InventoryAdminError("INVENTORY_REJECTED", str(exc)) from exc

        inventory_lot_ledger.assert_drift_unchanged(
            cur, agent_user_id, before=drift_before, action="admin 库存调整",
        )
        after = _wallet_snapshot(cur, agent_user_id)
        evidence.update({"drift_points": drift_before, "direction": direction})
        action_id = _record_admin_action(
            cur, action=action, agent_user_id=agent_user_id, customer_user_id=None,
            paid_points=paid_points, bonus_points=bonus_points,
            related_order_id=related_order_id, operator_user_id=operator_user_id,
            operator_username=operator_username, reason=reason, request_id=request_id,
            ip_address=ip_address, before=before, after=after, evidence=evidence,
        )
    logger.info(
        "[AdminInventory] adjust %s agent=%s paid=%s bonus=%s by=%s action_id=%s",
        direction, agent_user_id, paid_points, bonus_points, operator_user_id, action_id,
    )
    return {
        "success": True, "action": action, "action_id": action_id,
        "agent_user_id": int(agent_user_id), "request_id": request_id,
        "before": before, "after": after,
    }


# ============================================================
# P0-1 · admin 代服务商划拨给客户
# ============================================================

def _resolve_allocation_binding(
    cur, *, agent_user_id: int, customer_user_id: int, binding_expected_version: Optional[int],
    reason: str, operator_user_id: int, operator_username: Optional[str],
    request_id: str, ip_address: Optional[str],
) -> Dict[str, Any]:
    """确认/建立「这个客户归这个服务商」· 三种局面各有明确出口,**不静默成功**。"""
    cur.execute(
        "SELECT COALESCE(agent_level,0) AS agent_level FROM user_wallets WHERE user_id=%s",
        (int(customer_user_id),),
    )
    target = cur.fetchone()
    if not target:
        raise InventoryAdminError(
            "BUSINESS_IDENTITY_SSOT_UNAVAILABLE", "目标账号业务身份暂时无法确认,未执行划拨"
        )
    if int(target["agent_level"] or 0) >= 1:
        # 工单 P0-2 第 2 条:身份分流要**给出口**,不能只报错。
        raise InventoryAdminError(
            "TARGET_IS_SERVICE_PROVIDER",
            "该账号是服务商,不能作为「客户」接收划拨;服务商上下游请走渠道关系",
            next_step="channel_relationship",
            next_step_endpoint=(
                f"PUT /api/admin/user-governance/users/{int(customer_user_id)}"
                "/channel-relationship"
            ),
        )

    cur.execute(
        "SELECT id, agent_user_id FROM customer_agent_bindings WHERE customer_user_id=%s",
        (int(customer_user_id),),
    )
    binding = cur.fetchone()
    if binding and int(binding["agent_user_id"]) == int(agent_user_id):
        return {"binding_action": "existing", "binding_id": int(binding["id"])}
    if binding:
        # 工单 §P0-1 三选一 → 选 (1) 拒绝并提示走换绑流程。
        # 理由:`customer_agent_bindings.customer_user_id` 有 UNIQUE,跨绑划拨会破坏
        # 归属唯一性;而 CAS 换绑是**归属变更**,不该被"划个算力"顺手带过去 ——
        # 归属改了会连带撤销原服务商的 user_clients 投影与待支付订单校验。
        raise InventoryAdminError(
            "CUSTOMER_BOUND_TO_OTHER_PROVIDER",
            "该客户当前归属其他服务商;请先走「商业服务归属变更」完成换绑,再划拨",
            current_provider_user_id=int(binding["agent_user_id"]),
            next_step="commercial_service_binding",
            next_step_endpoint=(
                f"PUT /api/admin/user-governance/users/{int(customer_user_id)}"
                "/commercial-service-binding"
            ),
        )

    if binding_expected_version is None:
        raise InventoryAdminError(
            "BINDING_VERSION_REQUIRED",
            "该客户还没有商业服务归属;本次划拨会同时建立归属,请带上 binding_expected_version 确认",
        )
    # 🔴 同事务内调服务函数(不是 HTTP 自调用、不是裸 INSERT):
    #    审计 / CAS / 版本位 / user_clients 投影全部继承,失败整体回滚。
    result = change_commercial_binding_cur(
        cur, int(customer_user_id), int(agent_user_id),
        expected_version=int(binding_expected_version),
        reason=f"库存代划拨同时建立商业服务归属 · {reason}",
        operator_user_id=int(operator_user_id), operator_username=operator_username,
        request_id=request_id, ip_address=ip_address,
    )
    return {
        "binding_action": "created",
        "binding_version": result.get("version"),
        "permission_invalidations": result.get("_permission_invalidations") or [],
    }


def admin_allocate_to_customer(
    agent_user_id: int, customer_user_id: int, *, paid_points: int, bonus_points: int,
    binding_expected_version: Optional[int], reason: str, operator_user_id: int,
    operator_username: Optional[str], request_id: str, ip_address: Optional[str],
) -> Dict[str, Any]:
    """管理员代服务商把库存划拨给客户(服务商自己划不动时的兜底入口)。"""
    from services.dealer_inventory_resale import ResaleError
    from services.offline_allocation import allocate_offline_to_customer

    reason = _require_reason(reason)
    paid_points = int(paid_points or 0)
    bonus_points = int(bonus_points or 0)
    if paid_points < 0 or bonus_points < 0:
        raise InventoryAdminError("POINTS_INVALID", "算力必须非负")
    if paid_points == 0 and bonus_points == 0:
        raise InventoryAdminError("POINTS_INVALID", "划拨算力至少填一个 > 0")
    if int(agent_user_id) == int(customer_user_id):
        raise InventoryAdminError(
            "SELF_ALLOCATION",
            "服务商给自己划拨请走「库存转可用算力」,不走代客户划拨",
            next_step="self_use", next_step_endpoint="POST /api/agent/inventory/self-use",
        )

    with get_db() as conn:
        cur = conn.cursor()
        _lock_provider(cur, agent_user_id)
        binding_info = _resolve_allocation_binding(
            cur, agent_user_id=agent_user_id, customer_user_id=customer_user_id,
            binding_expected_version=binding_expected_version, reason=reason,
            operator_user_id=operator_user_id, operator_username=operator_username,
            request_id=request_id, ip_address=ip_address,
        )
        before = _wallet_snapshot(cur, agent_user_id)
        drift_before = inventory_lot_ledger.drift_points_of(cur, agent_user_id)

        try:
            allocation = allocate_offline_to_customer(
                cur, agent_user_id=int(agent_user_id), customer_user_id=int(customer_user_id),
                tool_points=paid_points, publish_points=0, bonus_points=bonus_points,
                description=f"管理员代划拨 · {reason}",
            )
            # 🔴 [R1 返修] 这里**不再**外挂 consume_lots_fifo:消耗已下沉到
            #    `agent_inventory.allocate_offline`(本调用链:
            #     allocate_offline_to_customer → allocate_offline → 消 lot)。
            #    留着外挂 = **同一笔消两遍**,lot 少扣一倍、差额转正 ——
            #    `assert_drift_unchanged` 会当场抛(不会静默烂账),但这条路直接不可用。
            #    判别锁:test_admin_allocate_consumes_lot_exactly_once。
        except ResaleError as exc:
            raise InventoryAdminError("LOT_REJECTED", str(exc), lot_code=exc.code) from exc
        except ValueError as exc:
            raise InventoryAdminError("INVENTORY_REJECTED", str(exc)) from exc

        inventory_lot_ledger.assert_drift_unchanged(
            cur, agent_user_id, before=drift_before, action="管理员代划拨",
        )
        after = _wallet_snapshot(cur, agent_user_id)
        action_id = _record_admin_action(
            cur, action=ACTION_ALLOCATE, agent_user_id=agent_user_id,
            customer_user_id=customer_user_id, paid_points=paid_points,
            bonus_points=bonus_points, related_order_id=None,
            operator_user_id=operator_user_id, operator_username=operator_username,
            reason=reason, request_id=request_id, ip_address=ip_address,
            before=before, after=after,
            evidence={
                "binding": binding_info,
                # lot 消耗明细已由 allocate_offline 内部完成,这里只记"消了多少"
                "lot_consumed_points": paid_points,
                "drift_points": drift_before,
                "customer_after": allocation.get("customer_after"),
            },
        )
    logger.info(
        "[AdminInventory] allocate agent=%s → customer=%s paid=%s bonus=%s binding=%s",
        agent_user_id, customer_user_id, paid_points, bonus_points,
        binding_info.get("binding_action"),
    )
    return {
        "success": True, "action": ACTION_ALLOCATE, "action_id": action_id,
        "agent_user_id": int(agent_user_id), "customer_user_id": int(customer_user_id),
        "request_id": request_id, "before": before, "after": after,
        "binding": binding_info,
        "customer_after": allocation.get("customer_after"),
        "_permission_invalidations": binding_info.get("permission_invalidations") or [],
    }


# ============================================================
# P0-3 · 库存自用(Owner 2026-08-12 拍板:① 1:1 转换)
# ============================================================

def convert_inventory_for_self_use(
    agent_user_id: int, *, paid_points: int, bonus_points: int, reason: str,
    operator_user_id: int, operator_username: Optional[str], request_id: str,
    ip_address: Optional[str],
) -> Dict[str, Any]:
    """服务商把自己的库存算力转成本人可用算力 · **1:1**。

    资金口径(Owner 2026-08-12 拍板 · 已回写 `docs/SYSTEM_TRUTH/08_billing.md`):
        三选一取 **① 自用 1:1**。已知并接受套利面 ——
        库存按进货价买入、钱包按零售价消费,1:1 自用等于服务商用进货价享受零售服务。

    技术方案取工单 §P0-3 的 (b):**不碰 `middleware/billing.py`**(受保护文件)。
    落点是「把库存划给自己」这一个动作,扣费链一行不改:
        `allocate_offline_to_customer(agent=X, customer=X)`
    —— 复用既有事务函数,库存侧写 `allocate_to_customer_offline` 流水,
    钱包侧写 `point_transactions(agent_grant)`,对账等式两边同步。

    守恒(验收判据 6):库存等额减少 · 钱包等额增加 · 不凭空产生算力。
    """
    from services.dealer_inventory_resale import ResaleError
    from services.offline_allocation import allocate_offline_to_customer

    reason = _require_reason(reason)
    paid_points = int(paid_points or 0)
    bonus_points = int(bonus_points or 0)
    if paid_points < 0 or bonus_points < 0:
        raise InventoryAdminError("POINTS_INVALID", "算力必须非负")
    if paid_points == 0 and bonus_points == 0:
        raise InventoryAdminError("POINTS_INVALID", "转换算力至少填一个 > 0")

    with get_db() as conn:
        cur = conn.cursor()
        _lock_provider(cur, agent_user_id)
        _assert_not_platform_seller(cur, agent_user_id)
        before = _wallet_snapshot(cur, agent_user_id)
        cur.execute(
            "SELECT paid_points, bonus_points FROM user_wallets WHERE user_id=%s",
            (int(agent_user_id),),
        )
        own_before = cur.fetchone()
        drift_before = inventory_lot_ledger.drift_points_of(cur, agent_user_id)

        try:
            allocation = allocate_offline_to_customer(
                cur, agent_user_id=int(agent_user_id), customer_user_id=int(agent_user_id),
                tool_points=paid_points, publish_points=0, bonus_points=bonus_points,
                description=f"库存转可用算力(自用)· {reason}",
            )
            # 🔴 [R1 返修] 同上:消耗已下沉到 allocate_offline,外挂删除防双重消耗。
            #    判别锁:test_self_use_consumes_lot_exactly_once。
        except ResaleError as exc:
            raise InventoryAdminError("LOT_REJECTED", str(exc), lot_code=exc.code) from exc
        except ValueError as exc:
            raise InventoryAdminError("INVENTORY_REJECTED", str(exc)) from exc

        inventory_lot_ledger.assert_drift_unchanged(
            cur, agent_user_id, before=drift_before, action="库存自用转换",
        )
        after = _wallet_snapshot(cur, agent_user_id)
        # 🔴 守恒断言在**提交之前**跑:库存少多少、钱包必须多多少(1:1)。
        #    不守恒说明某一侧没落,宁可整笔回滚也不留半边账。
        inventory_out = (before["paid_inventory_points"] - after["paid_inventory_points"]) + (
            before["bonus_inventory_points"] - after["bonus_inventory_points"]
        )
        wallet_in = (
            int(allocation["customer_after"]["paid"]) - int(own_before["paid_points"] or 0)
        ) + (
            int(allocation["customer_after"]["bonus"]) - int(own_before["bonus_points"] or 0)
        )
        if inventory_out != wallet_in or inventory_out != paid_points + bonus_points:
            raise InventoryAdminError(
                "SELF_USE_NOT_CONSERVED",
                f"库存自用转换不守恒(库存出 {inventory_out} · 钱包入 {wallet_in}),已整体拒绝",
            )
        action_id = _record_admin_action(
            cur, action=ACTION_SELF_USE, agent_user_id=agent_user_id,
            customer_user_id=agent_user_id, paid_points=paid_points,
            bonus_points=bonus_points, related_order_id=None,
            operator_user_id=operator_user_id, operator_username=operator_username,
            reason=reason, request_id=request_id, ip_address=ip_address,
            before=before, after=after,
            evidence={
                "conversion_ratio": "1:1",
                "funding_basis": "08_billing.md · Owner 2026-08-12 定 ① 自用 1:1",
                "lot_consumed_points": paid_points,
                "drift_points": drift_before,
                "inventory_out": inventory_out,
                "wallet_in": wallet_in,
            },
        )
    logger.info(
        "[AdminInventory] self-use agent=%s paid=%s bonus=%s action_id=%s",
        agent_user_id, paid_points, bonus_points, action_id,
    )
    return {
        "success": True, "action": ACTION_SELF_USE, "action_id": action_id,
        "agent_user_id": int(agent_user_id), "request_id": request_id,
        "inventory_before": before, "inventory_after": after,
        "wallet_after": allocation["customer_after"],
        "converted_paid": paid_points, "converted_bonus": bonus_points,
    }
