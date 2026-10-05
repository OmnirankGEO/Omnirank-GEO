"""
V3.5 充值结算编排器(SettlementOrchestrator)

核心责任(Codex r1 P0 + r2 强调):
- 主事务唯一充值结算入口
- 三路径互斥(v35_inventory_settlement / v32_legacy / direct)
- 同一订单不能双写老分润 + 新工厂(防 race / 防双收)
- pricing_snapshot 锁价 · 写回 recharge_orders.settlement_mode 防双写

调用方:db.wallet_db.complete_recharge() 主事务内 · 唯一入口

关联:
- docs/AI-CONTEXT/V35_FACTORY_INVENTORY_MODEL_v6_2026-05-26.md §6/§14
- memory feedback_v35_factory_inventory_model_v6
- memory feedback_codex_trust_but_verify (Codex 9 轮 review)
"""

import json
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger("GEO-V35-SettlementOrchestrator")


# ============================================================
# 配置开关读取
# ============================================================

def is_v35_factory_enabled(cursor) -> bool:
    """V3.5 工厂模式总开关"""
    cursor.execute("""
        SELECT value FROM system_settings WHERE key='V35_FACTORY_INVENTORY_ENABLED' LIMIT 1
    """)
    row = cursor.fetchone()
    if not row: return False
    val = (row["value"] if isinstance(row, dict) else row[0]) or ""
    return val.strip().lower() == "true"


def legacy_v32_policy(cursor) -> str:
    """V3.2 老分润开关 · 'passthrough_30day' / 'disabled' / 'rollback_full'"""
    cursor.execute("""
        SELECT value FROM system_settings WHERE key='LEGACY_REFERRAL_V32_ENABLED' LIMIT 1
    """)
    row = cursor.fetchone()
    if not row: return "passthrough_30day"
    val = (row["value"] if isinstance(row, dict) else row[0]) or "passthrough_30day"
    return val.strip()


# ============================================================
# 客户绑定 lookup
# ============================================================

def get_customer_binding(cursor, customer_user_id: int) -> Optional[Dict[str, Any]]:
    """Compatibility call into the canonical relationship SSOT."""
    from services.commercial_service_routing import read_locked_commercial_binding

    return read_locked_commercial_binding(cursor, customer_user_id)


# ============================================================
# [v7 finding1 · P0] 唯一 V3.5 结算 core —— 库存双流水 + 额度分轨 + 锁价结算 + ledger + 订单快照
# ============================================================
#
# 铁律(老板 v7 NO-GO 硬要求):禁止任何路径【手写第二套资金算法】。
#   充值主路径 _record_factory_settlement 与【争议裁决结算】(services/dispute_settlement.py)
#   都必须经此 core,复用 canonical 链:
#       purchase_auto_and_allocate → allocate_credit → calc_settlement → insert_revenue_ledger → settlement_snapshot
#   —— tool/publish/bonus/wholesale/payment_method/tax 全部由【调用方从订单快照透传】,core 不再私算 factory 系数。
#
#   fund-source 差异(user_wallets 反扣 vs escrow 已冻结)留在各前端,不进 core;
#   core 只负责【已确定归属 agent + 已确定分轨额度 + 已锁定 factory】后的 5 步 canonical 落账。
def record_v35_core_settlement(
    cursor,
    *,
    order_id,
    customer_user_id: int,
    agent_user_id: int,
    inv_paid_points: int,
    inv_bonus_points: int,
    credit_tool_points: int,
    credit_publish_points: int,
    credit_bonus_points: int,
    settle_points_granted: int,
    customer_paid_cents: int,
    locked_factory_cents: Optional[int],
    payment_method: str,
    tax_rate_bps: int,
    tax_mode: str,
    credit_source: str,
    credit_description: str,
    apply_rebate: bool = False,
    rebate_base_points: int = 0,
    write_order_snapshot: bool = True,
    ledger_source: str = "recharge",
    ledger_note: Optional[str] = None,
) -> Dict[str, Any]:
    """V3.5 结算 canonical 5 步(充值主路径 + 争议裁决共用 · 唯一算法)。

    - inv_*         :库存双流水(purchase_auto_and_allocate)· paid 池承载 tool+publish · bonus 池承载 bonus。
    - credit_*      :客户授权额度分轨(allocate_credit)· 与 fund-source 反扣额守恒(调用方保证 credit==扣减源)。
    - settle_*/customer_paid_cents/locked_factory_cents:结算公式入参(calc_settlement)·
                     factory 用【订单快照锁价 wholesale_cents】(locked_factory_cents),None 才回落全局配置实时算。
    - apply_rebate  :仅充值主路径 True(代理自定返利 · SAVEPOINT 隔离不阻断);裁决结算 False(不在老板 5 步链内)。
    返回 {"breakdown": <calc_settlement dict>, "ledger_id": int}。
    """
    from services.agent_inventory import purchase_auto_and_allocate
    from services.agent_revenue import insert_revenue_ledger
    from services.agent_pricing import calc_settlement

    # 1. 库存双流水(net 0 · 留出厂成本流入 / 客户授权流出 trace)
    purchase_auto_and_allocate(
        cursor,
        agent_user_id=agent_user_id,
        customer_user_id=customer_user_id,
        points_granted=int(inv_paid_points),
        bonus_points=int(inv_bonus_points),
        related_order_id=order_id,
    )

    # 2. [单账本收敛 2026-07-27] 原来这里 allocate_credit 把额度分轨记进客户信用钱包
    #    (tool/publish/bonus 三池)。Owner 定「不能有两本账」→ 删除。
    #    客户的充值算力/赠送算力就留在 user_wallets(complete_recharge 已入账),
    #    不再复制一份到第二个账本。
    #    🔴 credit_tool_points / credit_publish_points / credit_bonus_points 三个形参
    #       【保留不删】:调用方(充值主路径与争议裁决)都在传,删形参会改签名断两条链;
    #       它们现在只用于下面的结算/ledger 口径,不再驱动任何写入。

    # 3. 代理自定返利(仅充值主路径 · SAVEPOINT 隔离 · 绝不阻断)
    if apply_rebate and rebate_base_points > 0:
        try:
            from services.agent_rebate import apply_rebate_on_settlement
            rr = apply_rebate_on_settlement(
                cursor, agent_user_id=agent_user_id, customer_user_id=customer_user_id,
                base_points=int(rebate_base_points), related_order_id=order_id,
            )
            if rr.get("rebated_points"):
                logger.info(f"[v35core] order={order_id} 代理返利 {rr['rebated_points']} bonus (rate={rr.get('rate')})")
        except Exception as _re:
            logger.warning(f"[v35core] order={order_id} 代理返利异常(跳过·不阻断): {_re}")

    # 4. 结算公式(锁价 factory)+ 写代理收益 ledger(frozen · T+3)
    # [v7 对抗审 P3 修 · SSOT] 无下单锁价(locked_factory_cents=None · 边界老单/无快照)时,回落实时算必须用
    #   【per-agent 出厂折扣 override】(有则用其比例 · 无则 calc_settlement 内回落全局)· 充值主路径与争议裁决共用
    #   此单一规则,不再各自 fallback(防两路径 factory 口径漂移)。仅有 override 的服务商介入,prod 全部无 override 时零变化。
    if locked_factory_cents is None and int(settle_points_granted) > 0:
        try:
            from services.agent_pricing import calc_factory_cents, get_agent_wholesale_ratio
            from services.agent_pricing_overrides import get_agent_wholesale_override
            if get_agent_wholesale_override(agent_user_id):
                locked_factory_cents = calc_factory_cents(int(settle_points_granted), *get_agent_wholesale_ratio(agent_user_id))
        except Exception as _fexc:
            logger.warning("[v35core] agent=%s per-agent 出厂折扣实时算失败(回落全局): %s", agent_user_id, _fexc)
    breakdown = calc_settlement(
        customer_paid_cents=int(customer_paid_cents),
        points_granted=int(settle_points_granted),
        payment_method=payment_method or "wechat_pay",
        tax_rate_bps=tax_rate_bps,
        factory_cents_override=locked_factory_cents,
    )
    ledger_id = insert_revenue_ledger(
        cursor,
        agent_user_id=agent_user_id,
        recharge_order_id=order_id,
        customer_user_id=customer_user_id,
        customer_paid_cents=breakdown["customer_paid_cents"],
        factory_cents=breakdown["factory_cents"],
        gateway_fee_bps=breakdown["gateway_fee_bps"],
        gateway_fee_cents=breakdown["gateway_fee_cents"],
        settlement_service_fee_bps=breakdown["settlement_service_fee_bps"],
        settlement_service_fee_cents=breakdown["settlement_service_fee_cents"],
        agent_margin_before_tax_cents=breakdown["agent_margin_before_tax_cents"],
        tax_rate_bps=breakdown["tax_rate_bps"],
        tax_mode=tax_mode,
        tax_withholding_cents=breakdown["tax_withholding_cents"],
        agent_settlement_cents=breakdown["agent_settlement_cents"],
        source=ledger_source,
        note=ledger_note,
    )

    # 5. 回写 recharge_orders 拆账快照(settlement_snapshot_jsonb · 不覆盖下单 pricing_snapshot_jsonb)
    if write_order_snapshot:
        cursor.execute("""
            UPDATE recharge_orders SET
                agent_user_id = %s,
                factory_cents = %s,
                gateway_fee_bps = %s,
                gateway_fee_cents = %s,
                settlement_service_fee_bps = %s,
                settlement_service_fee_cents = %s,
                agent_margin_before_tax_cents = %s,
                tax_rate_bps = %s,
                tax_withholding_cents = %s,
                agent_revenue_cents = %s,
                settlement_snapshot_jsonb = %s::jsonb
            WHERE id = %s
        """, (
            agent_user_id,
            breakdown["factory_cents"],
            breakdown["gateway_fee_bps"], breakdown["gateway_fee_cents"],
            breakdown["settlement_service_fee_bps"], breakdown["settlement_service_fee_cents"],
            breakdown["agent_margin_before_tax_cents"],
            breakdown["tax_rate_bps"], breakdown["tax_withholding_cents"],
            breakdown["agent_settlement_cents"],
            json.dumps({**breakdown, "ledger_id": ledger_id, "tax_mode": tax_mode}),
            order_id,
        ))

    return {"breakdown": breakdown, "ledger_id": ledger_id}


# ============================================================
# SettlementOrchestrator 类
# ============================================================

class SettlementOrchestrator:
    """
    充值结算路由 · 三互斥
    必须在 complete_recharge 主事务内调 · 传 cursor + 已锁单的 order
    """

    def route(
        self,
        cursor,
        order: Dict[str, Any],
        user: Dict[str, Any],
        pricing_snapshot: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        返回 settlement_mode · 在主事务内写回 recharge_orders 防双写
        - 'v35_inventory_settlement' : 客户绑代理 + V3.5 工厂开关 ON
        - 'v35_platform_direct_settlement': 无商业绑定、受控平台直营主体
        - 'v32_legacy' : 仅兼容满足既有策略的历史订单
        - 'direct'     : 旧直营 · 无分润
        """
        customer_user_id = user.get("user_id") or user.get("id")
        order_id = order.get("id")
        order_type = order.get("order_type")
        order_sku_id = order.get("sku_template_id")
        order_agent_id = order.get("agent_user_id")
        is_v35_locked_order = (
            order_type == "customer_recharge"
            and order_sku_id is not None
            and order_agent_id is not None
        )
        if not is_v35_locked_order and pricing_snapshot:
            is_v35_locked_order = pricing_snapshot.get("amount_source") == "sku_snapshot"

        # New service-provider → consumer sales are identified by the durable
        # reservation row, never by the callback-time flag.  This branch consumes
        # real seller FIFO inventory and must run before the legacy auto-purchase
        # V3.5 path, which intentionally manufactures a +N/-N trace with net zero.
        from services import dealer_inventory_resale
        if dealer_inventory_resale.has_consumer_sale(cursor, str(order_id)):
            result = dealer_inventory_resale.settle_consumer_sale(
                cursor,
                order_id=str(order_id),
                consumer_user_id=int(customer_user_id),
                pricing_snapshot=dict(pricing_snapshot or {}),
            )
            cursor.execute(
                """UPDATE recharge_orders
                   SET settlement_mode='dealer_consumer_resale',
                       settlement_snapshot_jsonb=COALESCE(settlement_snapshot_jsonb,'{}'::jsonb)
                         || %s::jsonb
                   WHERE id=%s""",
                (json.dumps(result or {}, ensure_ascii=False), str(order_id)),
            )
            logger.info(
                "[Orchestrator] order=%s → dealer_consumer_resale · seller=%s consumer=%s",
                order_id, order_agent_id, customer_user_id,
            )
            return "dealer_consumer_resale"

        # 1. 检查 V3.5 工厂开关
        v35_on = is_v35_factory_enabled(cursor)
        v32_policy = legacy_v32_policy(cursor)

        # 2. 检查客户绑定
        binding = get_customer_binding(cursor, customer_user_id)
        commercial_resolution = (
            (pricing_snapshot or {}).get("commercial_resolution")
            if isinstance(pricing_snapshot, dict)
            else None
        )
        has_current_direct_markers = (
            order.get("binding_source") == "platform_direct"
            and isinstance(pricing_snapshot, dict)
            and pricing_snapshot.get("commercial_service_source") == "platform_direct"
            and commercial_resolution == "PLATFORM_DIRECT"
        )
        is_platform_direct_order = has_current_direct_markers
        current_dispute_status = str((binding or {}).get("dispute_status") or "").strip().lower()
        if (
            is_v35_locked_order
            and commercial_resolution == "BOUND"
            and order_agent_id is not None
            and binding
            and int(binding.get("agent_user_id") or 0) == int(order_agent_id)
            and (
                bool(binding.get("configuration_conflict"))
                or current_dispute_status not in ("", "resolved", "reverted")
            )
        ):
            # A genuine W4 dispute on the same commercial relationship remains
            # a hold.  Only absence/later rebind is insulated by the immutable
            # order snapshot below.
            return self._hold_for_dispute(
                cursor, order, customer_user_id, order_agent_id, binding, pricing_snapshot
            )
        if order.get("binding_source") == "platform_direct" and not has_current_direct_markers:
            raise ValueError("平台直营订单缺少不可变商业服务路由快照")
        if is_platform_direct_order:
            if not order_agent_id:
                raise ValueError("平台直营订单缺少不可变服务账号")
            # This order remains pinned even if the customer establishes an explicit
            # binding after checkout.  That newer relation applies to future quotes;
            # it must neither redirect nor block an already-paid immutable order.
            # The synthetic fact is never persisted as a customer binding.
            binding = {
                "agent_user_id": int(order_agent_id),
                "binding_source": "platform_direct",
            }
        elif (
            is_v35_locked_order
            and commercial_resolution == "BOUND"
            and order_agent_id is not None
        ):
            # A later admin relationship change applies only to new quotes.  The
            # paid callback must consume the immutable principal captured at
            # checkout; consulting the current projection here creates an
            # unresolvable NULL-dispute escrow.
            binding = {
                "agent_user_id": int(order_agent_id),
                "binding_source": order.get("binding_source") or "order_snapshot",
                "immutable_order_snapshot": True,
            }
        else:
            dispute_status = str((binding or {}).get("dispute_status") or "").strip().lower()
            if (binding or {}).get("configuration_conflict") or dispute_status not in (
                "", "resolved", "reverted"
            ):
                return self._hold_for_dispute(
                    cursor, order, customer_user_id, order_agent_id, binding, pricing_snapshot
                )

        # 2.5 [GEO-R6-CAN-005 v5] 归属冲突 → dispute/hold · 禁直接结算
        #   场景:订单快照锁定服务主体 Y,回调时有效商业绑定却是 X。
        #   回调绝不改绑；若继续按当前 binding(=X)
        #   走 _record_factory_settlement,会把【Y 订单的收益/库存/客户额度错划给 X】(跨账错配 · 服务商 X/Y 并发绑定竞态)。
        #   铁律:order.agent_user_id 与【最终绑定服务商】不一致 → 一律 dispute_hold:
        #        不划库存、不写 agent_revenue_ledger、不划客户额度;等 admin/W4 裁决后再结算。
        if self._attribution_conflict(order_agent_id, binding):
            return self._hold_for_dispute(cursor, order, customer_user_id, order_agent_id, binding, pricing_snapshot)

        # 3. 路由判断(三互斥)
        # [boss r7 P0-1 修正] 订单创建后 flag 切换不能改变结算路径
        # 资金链铁律:订单 snapshot 决定路径 · 不让 admin 中途改 flag 把已下单客户钱搞丢
        # V3.5 强制路径条件(任一满足):
        #   a. order_type='customer_recharge' + sku_template_id + agent_user_id(SKU 订单 · W3 标记)
        #   b. order_type='customer_recharge' + pricing_snapshot.amount_source='sku_snapshot'(双保险)
        # 不再让 flag=false 把已创建 SKU 订单降级到 direct/v32
        if is_v35_locked_order and commercial_resolution == "PLATFORM_DIRECT":
            if not is_platform_direct_order or order_agent_id is None:
                return self._hold_for_dispute(
                    cursor, order, customer_user_id, order_agent_id, binding, pricing_snapshot
                )
            self._record_factory_settlement(
                cursor,
                order,
                user,
                binding,
                pricing_snapshot,
            )
            mode = "v35_platform_direct_settlement"
            logger.info("[Orchestrator] order=%s → platform-direct snapshot", order_id)
        elif is_v35_locked_order and commercial_resolution == "BOUND":
            # V3.5 锁定订单 · 必须走 V3.5 路径(忽略当前 flag · 防中途改 flag 错路由)
            # BOUND snapshot 必须仍有同一条有效商业绑定；回调阶段禁止猜测或重建关系。
            if not binding:
                return self._hold_for_dispute(
                    cursor, order, customer_user_id, order_agent_id, binding, pricing_snapshot
                )
            self._record_factory_settlement(cursor, order, user, binding, pricing_snapshot)
            mode = "v35_inventory_settlement"
            logger.info(
                f"[Orchestrator] order={order_id} → v35_factory(locked by order snapshot) · "
                f"agent={order_agent_id} flag={v35_on}"
            )
        elif is_v35_locked_order:
            # Legacy locked orders without a signed resolution marker are not
            # safe to infer after the privacy/direct-account migration.
            return self._hold_for_dispute(
                cursor, order, customer_user_id, order_agent_id, binding, pricing_snapshot
            )
        elif binding and v35_on:
            # V3.5 工厂路径(legacy 路径 · 老充值订单 + binding + flag=true 才走)
            self._record_factory_settlement(cursor, order, user, binding, pricing_snapshot)
            mode = "v35_inventory_settlement"
            logger.info(f"[Orchestrator] order={order_id} → v35_factory · agent={binding['agent_user_id']}")
        else:
            # No commercial binding is always platform direct. Promotion links
            # never select a settlement principal.
            mode = "direct"
            logger.info(f"[Orchestrator] order={order_id} → direct (v35_on={v35_on}, v32_policy={v32_policy}, has_binding={bool(binding)})")

        # 写回 settlement_mode · 防双写
        cursor.execute("""
            UPDATE recharge_orders SET settlement_mode = %s WHERE id = %s
        """, (mode, order_id))
        return mode

    # ========================================================
    # [GEO-R6-CAN-005 v5] 归属冲突守卫
    # ========================================================

    @staticmethod
    def _attribution_conflict(order_agent_id, binding) -> bool:
        """order.agent_user_id 与【最终绑定服务商】binding.agent_user_id 是否不一致。

        None(无订单归属 / 无绑定)一律不算冲突(交常规三互斥路由)。
        """
        if order_agent_id is None or not binding:
            return False
        eff = binding.get("agent_user_id")
        if eff is None:
            return False
        try:
            return int(eff) != int(order_agent_id)
        except (TypeError, ValueError):
            return str(eff) != str(order_agent_id)

    def _hold_for_dispute(self, cursor, order, customer_user_id, order_agent_id, binding, pricing_snapshot=None) -> str:
        """[v6 req2 · 老板决策] 归属冲突订单进 dispute_hold:标 settlement_mode='dispute_hold' +
        【下单即冻结进 escrow】(反扣 user_wallets · 客户争议期消费不到)+ 关联 dispute_id · 待 admin 裁决原子结算/退款。

        绝不划库存 / 不写 agent 收益(A/B 未定)/ 不划服务商额度 —— 全部延到裁决时按 keep_old→X/reassign→Y/reject→退。
        已存在的关系争议状态会被保留；回调不创建、覆盖或删除商业绑定。
        """
        order_id = order.get("id")
        eff = binding.get("agent_user_id") if binding else None
        logger.error(
            "[Orchestrator][GEO-R6-CAN-005] order=%s 归属冲突 · order.agent=%s ≠ 绑定 agent=%s(dispute=%s)"
            " → dispute_hold + escrow(反扣不可消费 · 待裁决)",
            order_id, order_agent_id, eff, (binding or {}).get("dispute_status"),
        )
        cursor.execute("UPDATE recharge_orders SET settlement_mode='dispute_hold' WHERE id=%s", (order_id,))
        # 关联既有 pending dispute_id(W4 disputes 表存在时)；没有证据时不补造关系记录。
        #   🔴 必须先 to_regclass 探表:表不存在时直接 SELECT 会【abort 整个主事务】(PG 语义)→ 连累整笔充值回滚。
        dispute_id = None
        cursor.execute("SELECT to_regclass('customer_agent_binding_disputes') AS reg")
        _reg = cursor.fetchone()
        _reg = (_reg["reg"] if isinstance(_reg, dict) else _reg[0]) if _reg else None
        if _reg is not None:
            cursor.execute("SELECT id FROM customer_agent_binding_disputes WHERE customer_user_id=%s AND status='pending' "
                           "ORDER BY id DESC LIMIT 1", (customer_user_id,))
            r = cursor.fetchone()
            dispute_id = (r["id"] if isinstance(r, dict) else r[0]) if r else None
        # 下单即冻结进 escrow(反扣 user_wallets · 不可消费)· 失败必 raise → 整笔充值回滚(客户钱不丢 · 不留可消费漏洞)
        from db.dispute_escrow_db import create_escrow
        create_escrow(
            cursor, order_id=order_id, dispute_id=dispute_id, customer_user_id=customer_user_id,
            order_agent_user_id=order_agent_id, bound_agent_user_id=eff,
            amount_cents=int(order.get("amount_cents", 0) or 0),
            base_points=int(order.get("base_points", 0) or 0),
            bonus_points=int(order.get("bonus_points", 0) or 0),
            pricing_snapshot=pricing_snapshot,
        )
        return "dispute_hold"

    # ========================================================
    # V3.5 工厂路径
    # ========================================================

    def _record_factory_settlement(
        self,
        cursor,
        order: Dict[str, Any],
        user: Dict[str, Any],
        binding: Dict[str, Any],
        pricing_snapshot: Optional[Dict[str, Any]],
    ):
        """
        V3.5 路径:
        1. 自动补库存双流水(代理 paid_inventory + bonus_inventory)
        2. 客户授权 wallet 入账(tool/publish/bonus 分轨)
        3. 写代理收益 ledger(frozen · T+3 settle)
        """
        # [v7 finding1 · P0] canonical 5 步落账收敛到 record_v35_core_settlement(唯一算法)·
        #   本方法只做 fund-source 特有部分(锁价 factory 计算 / user_wallets 守恒钳制 + 反扣审计流水)。
        from services.agent_pricing import get_agent_tax_rate_bps, get_agent_tax_mode

        customer_user_id = user.get("user_id") or user.get("id")
        agent_user_id = binding["agent_user_id"]
        order_id = order["id"]

        # 1. 从 pricing_snapshot 或 order 解出 N (客户获得积分)
        if pricing_snapshot:
            points_granted = pricing_snapshot.get("points_granted", 0)
            tool_points = pricing_snapshot.get("tool_points", points_granted)
            publish_points = pricing_snapshot.get("publish_points", 0)
            bonus_points = pricing_snapshot.get("bonus_points", 0)
            customer_paid_cents = pricing_snapshot.get("customer_paid_cents", order.get("amount_cents", 0))
        else:
            # fallback · 老 recharge_orders 字段
            points_granted = order.get("base_points", 0) + order.get("bonus_points", 0)
            tool_points = order.get("base_points", 0)
            publish_points = 0  # 默认 0 · 由 SKU 模板决定
            bonus_points = order.get("bonus_points", 0)
            customer_paid_cents = order.get("amount_cents", 0)

        # 2. [v7 P0] 结算 factory 锁价(fund-source 无关 · 透传 core):优先用下单 pricing_snapshot 锁定的 wholesale_cents。
        #    [v7 对抗审 P3] 无锁价值(None)时的回落实时算(per-agent override / 全局)统一由 record_v35_core_settlement
        #    单点处理(充值主路径与争议裁决共用同一 factory 规则 · 不再各自 fallback 致口径漂移)。
        _locked_factory = None
        if pricing_snapshot:
            _w = pricing_snapshot.get("wholesale_cents")
            if _w and int(_w) > 0:
                _locked_factory = int(_w)
            else:
                # [FIX·P2-C] snapshot 存在但缺 wholesale_cents → 透传 None · core 回落实时算(边界·记录便于系数一致性对账)
                logger.warning("[Orchestrator] order=%s pricing_snapshot 存在但缺 wholesale_cents · "
                               "core 回落实时算 factory_cents(注意与下单时出厂系数一致性)", order.get("id"))

        # [单账本收敛 2026-07-27] 原步骤 3+4 已删除,它们是"两账同额移动"的配套:
        #   步骤 3 读 user_wallets 余额把客户 credit 入账额钳到可扣量(守恒);
        #   步骤 4 反向扣 user_wallets 并写 v35_migrated_to_customer_credit 审计流水。
        # 🔴 这是两本账的第二个入账源(生产实证 7 笔 -28,925,source=v35_orchestrator_migrate),
        #    且是【充值主路径】。单账本后钱不再搬家 —— 充值留在 user_wallets,
        #    既不需要反扣,也不需要守恒钳制。
        # 下面 core 的 credit_* 参数沿用未钳制的原值(它们已不驱动写入,仅参与结算口径)。
        _clamp_tool = tool_points
        _clamp_publish = publish_points
        _clamp_bonus = bonus_points

        # 5. [v7 P0] canonical 结算 core(唯一算法 · 与争议裁决共用):库存双流水 + 额度分轨 + 锁价结算 + ledger + 订单快照。
        #    inv 用未钳制 snapshot 额(与原语义一致 · net0 库存 trace);credit 用钳制额(守恒);
        #    factory 用锁价 _locked_factory;apply_rebate=True(充值主路径特有 · SAVEPOINT 隔离不阻断)。
        core = record_v35_core_settlement(
            cursor,
            order_id=order_id,
            customer_user_id=customer_user_id,
            agent_user_id=agent_user_id,
            inv_paid_points=(tool_points + publish_points),
            inv_bonus_points=bonus_points,
            credit_tool_points=_clamp_tool,
            credit_publish_points=_clamp_publish,
            credit_bonus_points=_clamp_bonus,
            settle_points_granted=points_granted,
            customer_paid_cents=customer_paid_cents,
            locked_factory_cents=_locked_factory,
            payment_method=order.get("payment_method") or "wechat_pay",
            tax_rate_bps=get_agent_tax_rate_bps(cursor, agent_user_id),
            tax_mode=get_agent_tax_mode(cursor, agent_user_id),
            credit_source="online_payment",
            credit_description=f"线上充值 · 订单 {order_id}",
            apply_rebate=True,
            rebate_base_points=(tool_points + publish_points + bonus_points),
            write_order_snapshot=True,
        )
        breakdown = core["breakdown"]; ledger_id = core["ledger_id"]
        logger.info(
            f"[Orchestrator.v35] order={order_id} customer={customer_user_id} agent={agent_user_id} "
            f"R={breakdown['customer_paid_cents']} factory={breakdown['factory_cents']} "
            f"settlement={breakdown['agent_settlement_cents']} ledger={ledger_id}"
        )

    # ========================================================
    # V3.2 兼容路径(过渡期 30 天)
    # ========================================================

    def _record_legacy_settlement(self, cursor, order: Dict[str, Any], user: Dict[str, Any]):
        """
        v32_legacy 路径 · Orchestrator 仅 mark + log,**不写**老分润

        [Codex r6 双写保险丝]
        实际 V3.2 老分润由 db.wallet_db.complete_recharge **事务外** fallback
        调 process_recharge_commission_v3(自开 conn + 自 commit · 保 race 兼容)统一负责。

        ⚠️ 严禁在此处 import / 调用任何老分润写入函数(例如 record_legacy_commission_tx)。
            否则同一 v32_legacy 订单会被双写(Orchestrator 内 + wallet_db fallback 外)。
            如需将老分润收口到主事务内,必须同时移除 wallet_db.py:763 的 fallback,
            两端不能并存。
        """
        order_id = order.get("id")
        customer_user_id = user.get("user_id") or user.get("id")
        logger.info(
            f"[Orchestrator.v32_legacy] order={order_id} customer={customer_user_id} · "
            f"仅 mark settlement_mode='v32_legacy' · 老分润由 complete_recharge 事务外 fallback 写入"
        )
