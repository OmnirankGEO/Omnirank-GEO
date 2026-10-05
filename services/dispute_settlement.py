"""[v7 finding1 · P0 · 老板 NO-GO 硬要求] 唯一 V3.5 争议裁决结算服务。

禁止手写第二套资金算法:裁决结算(keep_old→X / reassign→Y)【复用】充值主路径同一 canonical 链
    settlement_orchestrator.record_v35_core_settlement:
        purchase_auto_and_allocate → allocate_credit → calc_settlement → insert_revenue_ledger → settlement_snapshot

全量继承订单快照(pricing_snapshot):tool/publish/bonus 分轨 + wholesale_cents 锁价 + payment_method;税走 agent 维度。
绝不硬编码出厂系数(旧 v6 硬编码全局出厂系数是 P0 亏损根因)· 绝不把 base+bonus 全塞 tool 池(丢 publish 分轨)。

守恒(三账 · 逐字段):
- hold 时:user_wallets −(base+bonus) · dispute_escrow +(base+bonus)
- settle 时:dispute_escrow −(base+bonus) · **本函数写回 user_wallets +(base+bonus)**
  (2026-09-05 #74 补:core 自 2aef3b59b 起不写任何钱包,而争议链在 hold 时已把钱反扣走,
   所以「客户的算力留在 user_wallets」这句对争议路径不成立 —— 必须由本函数写回。)
             + 代理库存双流水 net0 + agent_revenue_ledger 记 factory=快照锁价 / 结算=客户付款−factory
  → 客户拿到的 credit 总额 == escrow 释放额(守恒)· factory 用快照锁价(非重算)· 平台不亏。
"""
from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger("GEO-DisputeSettle")


def _load_snapshot(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return {}


def settle_escrow_to_agent(cursor, escrow: dict, agent_user_id: int, admin_id: Optional[int]) -> bool:
    """裁决结算给 agent_user_id(keep_old→原服务商 X / reassign→新服务商 Y)· 原子 · 幂等(仅 held→settled CAS)。

    走【唯一】canonical 结算 core · 全量继承 escrow.pricing_snapshot 分轨 + 锁价 · 守恒钳到 escrow held(base/bonus)。
    返回 True=本次真结算;False=非 held(重复裁决 no-op · 不二次结算)。
    """
    eid = escrow["id"]
    # 1. 幂等占位:held→settled CAS(防重复裁决二次结算)· 一并取回结算所需字段(含快照)
    cursor.execute(
        "UPDATE dispute_escrow SET status='settled', resolved_agent_user_id=%s, resolved_by=%s, resolved_at=NOW() "
        "WHERE id=%s AND status='held' "
        "RETURNING base_points, bonus_points, customer_user_id, amount_cents, order_id, pricing_snapshot",
        (agent_user_id, admin_id, eid),
    )
    row = cursor.fetchone()
    if not row:
        logger.warning(f"[DisputeSettle] escrow={eid} 非 held(重复裁决?)· 跳过 settle")
        return False

    base = int(row["base_points"] or 0)          # escrow 冻结的 paid 部分(承载 tool+publish)
    bonus = int(row["bonus_points"] or 0)         # escrow 冻结的 bonus 部分
    customer = row["customer_user_id"]
    amount_cents = int(row["amount_cents"] or 0)
    order_id = row["order_id"]
    snap = _load_snapshot(row["pricing_snapshot"])

    # 🔴🔴 [#74 P0 · Review §21 代批 · 2026-09-05] 把 escrow 释放的额度**写回客户钱包**。
    #
    #    在此之前这一步**根本不存在**:`create_escrow` 在 hold 时
    #    `UPDATE user_wallets … paid_points - base`(反扣走),`refund_escrow` 会放回,
    #    而 settle 侧 escrow 出账后**没有任何语句写回** —— keep_old / reassign 两种裁决下
    #    客户的 base+bonus 直接蒸发。
    #
    #    代码当时在打自己:下面 :5 的日志逐字写「客户额度 +N」,本模块 docstring 写
    #    「客户拿到的 credit 总额 == escrow 释放额(守恒)」—— 都描述了一件没发生的事。
    #
    #    回归点 `2aef3b59b`(2026-07-27 单账本收敛)从 core 删掉 `allocate_credit`,
    #    理由是「客户的算力留在 user_wallets(complete_recharge 已入账)」。
    #    这句话对**充值主路径**成立,对**争议路径不成立** —— 那条路把钱反扣走了。
    #    写下一个全局前提,没有逐个调用方回答「你用没用到它」。
    #
    #    金额取 **escrow 行本身**(上面 CAS 的 RETURNING),不重算、不读当前价:
    #    退款/结算一律按原订单不可变快照单跳反转。
    cursor.execute("SELECT paid_points, bonus_points FROM user_wallets WHERE user_id = %s",
                   (customer,))
    _before = cursor.fetchone() or {}
    cursor.execute(
        "UPDATE user_wallets SET paid_points = paid_points + %s, "
        "bonus_points = bonus_points + %s, updated_at = NOW() WHERE user_id = %s "
        "RETURNING paid_points, bonus_points",
        (base, bonus, customer))
    # 🔴 与 `refund_escrow:139` **同解**的防蒸发自证:钱包行缺失 ⇒ UPDATE 0 行 ⇒
    #    钱既不在 escrow 也没回客户。拒绝坐实 settled,整笔裁决回滚,强制人工。
    #    这一侧原先没有自证 —— 方向相反、后果相同,而只有一侧配了尺子。
    if cursor.rowcount != 1:
        raise RuntimeError(
            "[DisputeSettle][CRITICAL] escrow=%s 入账目标 user_wallets(customer=%s)命中 %s 行"
            "(期望 1)· 拒绝坐实 settled(防资金蒸发)· 裁决将回滚"
            % (eid, customer, cursor.rowcount))
    # 🔴 [#74 ① · Review §21] rowcount==1 只证「有一行被 UPDATE 了」,
    #    证不了「加进去的正好是 base+bonus」。逐值核对余额差 —— 这一步才是入账额自证。
    _after = cursor.fetchone() or {}
    _got = (int(_after.get("paid_points", 0)) - int(_before.get("paid_points", 0)),
            int(_after.get("bonus_points", 0)) - int(_before.get("bonus_points", 0)))
    if _got != (base, bonus):
        raise RuntimeError(
            "[DisputeSettle][CRITICAL] escrow=%s 入账额 %r != escrow 释放额 (%s, %s)"
            "· 拒绝坐实 settled · 裁决将回滚" % (eid, _got, base, bonus))
    # 2. 从快照继承分轨(tool/publish)· 守恒钳到 escrow held(tool 优先填 base,余量给 publish → tool+publish==base)
    snap_tool = int(snap.get("tool_points", base) or 0)
    credit_tool = min(max(snap_tool, 0), base)
    credit_publish = base - credit_tool
    credit_bonus = bonus

    # 3. 结算入参全量继承快照:锁价 wholesale_cents / 客户付款 / 支付方式;税走 agent 维度
    customer_paid_cents = int(snap.get("customer_paid_cents", amount_cents) or amount_cents)
    _w = snap.get("wholesale_cents")
    locked_factory = int(_w) if (_w and int(_w) > 0) else None
    payment_method = snap.get("payment_method") or "wechat_pay"

    # 🔴 [#74 P3] 税制优先用**下单时冻进快照**的那一份,别读今天的配置:
    #    裁决常在数月后发生,现读等于拿今天的税制结算几个月前的订单。
    #    ⚠️ 冻的是 **order_agent** 的税。`reassign` 结给**另一个**服务商时,
    #       把 A 的税率套到 B 身上是错的 —— 那种情况只能现读 B 的,并记明用了哪一种。
    _frozen_agent = snap.get("order_agent_user_id")
    _use_frozen = (_frozen_agent is not None
                   and int(_frozen_agent) == int(agent_user_id)
                   and snap.get("order_agent_tax_rate_bps") is not None)
    if _use_frozen:
        tax_rate_bps = int(snap["order_agent_tax_rate_bps"])
        tax_mode = snap.get("order_agent_tax_mode")
        tax_provenance = "frozen_snapshot"
    else:
        from services.agent_pricing import get_agent_tax_mode, get_agent_tax_rate_bps
        tax_rate_bps = get_agent_tax_rate_bps(cursor, agent_user_id)
        tax_mode = get_agent_tax_mode(cursor, agent_user_id)
        tax_provenance = "live_lookup"

    # 4. 唯一 canonical 结算 core(与充值主路径同一算法)· 不再手写第二套
    from services.settlement_orchestrator import record_v35_core_settlement
    core = record_v35_core_settlement(
        cursor,
        order_id=order_id,
        customer_user_id=customer,
        agent_user_id=agent_user_id,
        inv_paid_points=base,
        inv_bonus_points=bonus,
        credit_tool_points=credit_tool,
        credit_publish_points=credit_publish,
        credit_bonus_points=credit_bonus,
        settle_points_granted=(base + bonus),
        customer_paid_cents=customer_paid_cents,
        locked_factory_cents=locked_factory,
        payment_method=payment_method,
        tax_rate_bps=tax_rate_bps,
        tax_mode=tax_mode,
        # [单账本收敛 2026-07-27] credit_source/credit_description 保留形参(core 签名未改),
        #   但已不再驱动信用钱包写入 —— core 里的 allocate_credit 已删除。
        credit_source="online_payment",
        credit_description=f"争议裁决结算 · escrow={eid} · 订单 {order_id}",
        apply_rebate=False,                 # 返利不在老板 5 步裁决链内
        rebate_base_points=0,
        write_order_snapshot=True,
        ledger_source="recharge",
        ledger_note=f"dispute 裁决结算 · escrow={eid} · order={order_id} · agent={agent_user_id}"
                    f" · tax={tax_provenance}",
    )
    bd = core["breakdown"]

    # 5. 订单结算态收口(dispute_hold → dispute_settled)· 审计
    cursor.execute("UPDATE recharge_orders SET settlement_mode='dispute_settled' WHERE id=%s", (order_id,))
    logger.warning(
        "[DisputeSettle] escrow=%s → settled to agent=%s · 客户额度 +%s(tool=%s publish=%s bonus=%s)· "
        "factory=%s(快照锁价)settlement=%s ledger=%s",
        eid, agent_user_id, base + bonus, credit_tool, credit_publish, credit_bonus,
        bd["factory_cents"], bd["agent_settlement_cents"], core["ledger_id"],
    )
    return True
