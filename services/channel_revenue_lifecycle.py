"""[geofix v10] 渠道收益台账生命周期 · refund_status 唯一口径 + 退款后冲销 canonical service。

背景(v9 对抗审两轮暴露):渠道收益记账/冲销此前散在多处、按 truthy 猜 refund_status、退款完成后
无统一冲销入口。本模块建立【单一权威口径】,record(补记)与 reverse(冲销)两条路径共用同一分类,
消除口径漂移。

refund_status 4 分区(依据代码既有 settle 语义 · 见 api/referral_api settle 逻辑 / api/wallet_api 退款流):
  - REVENUE_OWED   : NULL / '' / 'none' / 'rejected' / 'failed'  —— 订单成立·款未退·渠道收益仍应存在 → 允许补记 · 不冲销
  - IN_FLIGHT      : 'pending' / 'approved' / 'channel_refunding' —— 退款在途未终态 → 暂不记账·暂不冲销·等落终态
       ('channel_refunding' = [v11 P1-1] 虎皮椒 RD 退款中 · 渠道现金退款进行中未确认 · 不得提前冲销)
  - REFUND_EFFECTIVE: 'processed' / 'completed' / 'pending_review' —— 已进入不可逆退款/外部现金已退 → 不得新记·应冲销
  - UNKNOWN        : 其它未知值                                    —— fail-closed:不猜成功·告警等人工(不记不冲销)
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger("GEO-ChannelRevenueLifecycle")

# —— 唯一口径:各分区取值集合(小写归一) ——
_REVENUE_OWED = frozenset({"", "none", "rejected", "failed"})   # NULL 亦归此(见 classify)
_IN_FLIGHT = frozenset({"pending", "approved", "channel_refunding"})
_REFUND_EFFECTIVE = frozenset({"processed", "completed", "pending_review"})

# Public read-only contract for consumers that must use the same refund
# effectiveness boundary without re-declaring another status list.
REFUND_EFFECTIVE_STATUSES = _REFUND_EFFECTIVE


def refund_effective_sql(alias: str) -> tuple[str, tuple]:
    """Return the canonical SQL predicate for an actually effective refund.

    ``pending_review`` is effective only with a persisted provider completion
    timestamp or an approved/manual work-order attachment. Callers use this in
    aggregate queries so tier progress cannot diverge from the canonical refund
    sink.
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(alias or "")):
        raise ValueError("invalid SQL alias")
    from db.refund_work_order_db import MANUAL_REFUND_EVIDENCE_TYPES

    normal_statuses = sorted(REFUND_EFFECTIVE_STATUSES.difference({"pending_review"}))
    predicate = f"""(
        LOWER(COALESCE({alias}.refund_status, '')) = ANY(%s)
        OR (
            LOWER(COALESCE({alias}.refund_status, '')) = 'pending_review'
            AND COALESCE({alias}.refunded_amount_cents, 0) > 0
            AND {alias}.refunded_amount_cents <= {alias}.amount_cents
            AND (
                {alias}.refund_completed_at IS NOT NULL
                OR EXISTS (
                    SELECT 1
                      FROM refund_work_orders rwo
                     WHERE rwo.source_order_id = {alias}.id::text
                       AND rwo.status IN ('approved','payout_pending','completed')
                       AND rwo.requested_refund_cents = {alias}.refunded_amount_cents
                       AND (
                            NULLIF(BTRIM(COALESCE(rwo.payout_proof_url, '')), '') IS NOT NULL
                            OR EXISTS (
                                SELECT 1
                                  FROM refund_work_order_attachments rwa
                                 WHERE rwa.work_order_id = rwo.id
                                   AND rwa.evidence_type = ANY(%s)
                            )
                       )
                )
            )
        )
    )"""
    return predicate, (normal_statuses, list(MANUAL_REFUND_EVIDENCE_TYPES))

CLASS_REVENUE_OWED = "REVENUE_OWED"
CLASS_IN_FLIGHT = "IN_FLIGHT"
CLASS_REFUND_EFFECTIVE = "REFUND_EFFECTIVE"
CLASS_UNKNOWN = "UNKNOWN"


def classify_refund_status(refund_status: Optional[str]) -> str:
    """把 recharge_orders.refund_status 归一到 4 分区之一(唯一口径 · record/reverse 共用)。"""
    rs = (refund_status or "").strip().lower()
    if rs in _REVENUE_OWED:
        return CLASS_REVENUE_OWED
    if rs in _IN_FLIGHT:
        return CLASS_IN_FLIGHT
    if rs in _REFUND_EFFECTIVE:
        return CLASS_REFUND_EFFECTIVE
    return CLASS_UNKNOWN


def record_action(refund_status: Optional[str]) -> str:
    """渠道收益【补记】路径据 refund_status 的动作:'record' | 'defer' | 'skip' | 'unknown'。

    - REVENUE_OWED    → 'record'(款未退·收益仍欠·正常补记)
    - IN_FLIGHT       → 'defer'(在途·只延后不结案·不累计重试次数·防过早误判)
    - REFUND_EFFECTIVE→ 'skip'(已退·补记会成幽灵 recorded 行永不冲销·跳过并收口)
    - UNKNOWN         → 'unknown'(fail-closed:不记不结案·告警等人工)
    """
    cls = classify_refund_status(refund_status)
    return {
        CLASS_REVENUE_OWED: "record",
        CLASS_IN_FLIGHT: "defer",
        CLASS_REFUND_EFFECTIVE: "skip",
        CLASS_UNKNOWN: "unknown",
    }[cls]


def should_reverse_on_refund(refund_status: Optional[str]) -> bool:
    """渠道收益【冲销】路径:仅当退款【已生效】(REFUND_EFFECTIVE)才冲销。
    in_flight(pending/approved)= 仅创建退款申请阶段 → 不提前冲销;revenue_owed/unknown = 不冲销。
    """
    return classify_refund_status(refund_status) == CLASS_REFUND_EFFECTIVE


# —— [v12 item1] 渠道退款回调状态机(RD/CD/UD)· 显式转移表(单一权威口径) ——
UD_FAILED_STATUS = "failed"   # UD 退出 channel_refunding 后的失败态(REVENUE_OWED·保收益·admin 可重试)


def channel_refund_transition(current_rs, event: str) -> dict:
    """[v12 item1] 渠道退款回调(RD/CD/UD)显式状态转移表。返回:
      {"action": 'set'|'noop'|'illegal', "target": str|None, "reverse": bool, "ack": 'success'|'failclosed', "reason": str}

    事件语义:
      - RD=渠道退款处理中 → channel_refunding(IN_FLIGHT · **不冲收益·不退内账**)。
      - CD=渠道现金退款成功 → pending_review(REFUND_EFFECTIVE)+ canonical 收益冲销。
      - UD=渠道退款失败 → 退出 channel_refunding 回 'failed'(REVENUE_OWED · **保原收益+原内账** · 允许人工重试)。
    action 语义:
      - 'set':UPDATE refund_status=target(乐观锁 WHERE=当前态;**touched≠1 → fail-closed**)。
      - 'noop':已处于该事件的幂等终态/更靠后态 → 不改状态(reverse 仍可 True 做幂等冲销)· ack=success。
      - 'illegal':非法迁移/未知值/冲突(UD 撞已生效)→ **ack=failclosed**(不假成功·告警等人工)。
    """
    ev = (event or "").upper()
    rs = (current_rs or "").strip().lower()
    cls = classify_refund_status(current_rs)

    if cls == CLASS_UNKNOWN:
        return {"action": "illegal", "target": None, "reverse": False, "ack": "failclosed",
                "reason": f"未知 refund_status={current_rs}·fail-closed"}

    if ev == "RD":
        if rs == "channel_refunding":
            return {"action": "noop", "target": None, "reverse": False, "ack": "success", "reason": "重复 RD 幂等"}
        if cls == CLASS_REVENUE_OWED:   # NULL/none/failed/rejected → 进入退款中(含失败后重试再入)
            return {"action": "set", "target": "channel_refunding", "reverse": False, "ack": "success", "reason": "RD→channel_refunding"}
        if cls == CLASS_REFUND_EFFECTIVE:
            return {"action": "noop", "target": None, "reverse": False, "ack": "success", "reason": "RD 迟到·已生效(更靠后)·不降级"}
        # IN_FLIGHT 且非 channel_refunding = 平台自发 pending/approved 退款在途 → 不干扰
        return {"action": "noop", "target": None, "reverse": False, "ack": "success", "reason": "平台退款在途·RD 不干扰"}

    if ev == "CD":
        if cls == CLASS_REFUND_EFFECTIVE:
            return {"action": "noop", "target": None, "reverse": True, "ack": "success", "reason": "CD 幂等·已生效·幂等冲销"}
        # REVENUE_OWED(含失败重试后 CD 成功)/ IN_FLIGHT(channel_refunding / 平台 pending·approved)→ 迁移生效 + 冲销
        return {"action": "set", "target": "pending_review", "reverse": True, "ack": "success", "reason": "CD→pending_review+冲销"}

    if ev == "UD":
        if rs == "channel_refunding":
            return {"action": "set", "target": UD_FAILED_STATUS, "reverse": False, "ack": "success",
                    "reason": "UD→退出退款在途态·保收益+内账·允许人工重试"}
        if cls == CLASS_IN_FLIGHT:
            return {"action": "illegal", "target": None, "reverse": False, "ack": "failclosed",
                    "reason": "UD 撞平台 pending/approved 退款态·禁止跨状态机改写·等人工"}
        if cls == CLASS_REFUND_EFFECTIVE:
            return {"action": "illegal", "target": None, "reverse": False, "ack": "failclosed",
                    "reason": "UD 撞已生效退款(现金已退却又报失败)·冲突·fail-closed 等人工"}
        # REVENUE_OWED(本就未在退/已失败)→ 幂等
        return {"action": "noop", "target": None, "reverse": False, "ack": "success", "reason": "UD 幂等·未处于 channel_refunding"}

    return {"action": "illegal", "target": None, "reverse": False, "ack": "failclosed", "reason": f"未知事件 {event}"}


def reverse_channel_revenue_on_refund(cursor, order_id) -> dict:
    """[v10 item6] 退款完成后【渠道收益冲销】canonical service —— 所有退款完成入口共用。

    在【调用方事务(cursor)】内执行(退款状态与本冲销原子提交):
      1. FOR UPDATE 锁订单;
      2. 据 refund_status 判定退款是否【已生效】;pending_review 还须核验 CD/人工实际退款凭证,
         仅创建申请的 pending/approved 不提前冲销;
      3. 幂等执行 reverse_channel_revenue(recorded→reversed · UPDATE WHERE status='recorded');
      4. 冲销异常 → SAVEPOINT 回滚局部 + 同事务登记 kind='reverse' 耐久补偿工单(exactly-once)· 登记再失败则不吞传播;
      5. 不改余额/库存/佣金/现金退款算法 · 只收口 channel_revenue_ledger。

    返回 {"reversed": bool, "class": str, "enqueued": bool, "skipped": str|None}。
    """
    order_id = str(order_id)
    cursor.execute(
        "SELECT id, refund_status, refund_completed_at "
        "FROM recharge_orders WHERE id=%s FOR UPDATE",
        (order_id,),
    )
    row = cursor.fetchone()
    if not row:
        return {"reversed": False, "class": None, "enqueued": False, "skipped": "order_not_found"}
    rs = row.get("refund_status") if isinstance(row, dict) else (row[1] if len(row) > 1 else None)
    cls = classify_refund_status(rs)
    from services import dealer_inventory_resale
    dealer_resale_synced = dealer_inventory_resale.sync_refund_from_recharge(
        cursor, order_id
    )
    if cls != CLASS_REFUND_EFFECTIVE:
        # 未生效(在途/款未退/未知)→ 不提前冲销(等真正落生效态再由对应入口调用)
        return {
            "reversed": False,
            "class": cls,
            "enqueued": False,
            "skipped": f"not_effective:{cls}",
            "dealer_resale_synced": dealer_resale_synced,
        }

    # pending_review is a workflow state, not proof by itself. Enforce the same
    # CD/manual-evidence rule in this canonical sink so recovery jobs and future
    # callers cannot bypass API-level guards with a stale or hand-edited status.
    if (rs or "").strip().lower() == "pending_review":
        from db.refund_work_order_db import assert_external_refund_evidence_cur
        refund_completed_at = (
            row.get("refund_completed_at") if isinstance(row, dict)
            else (row[2] if len(row) > 2 else None)
        )
        assert_external_refund_evidence_cur(
            cursor,
            order_id,
            rs,
            refund_completed_at,
        )

    cursor.execute("SAVEPOINT sp_chanrev_reverse")
    try:
        from services.channel_pricing import reverse_channel_revenue
        reverse_channel_revenue(cursor, order_id)   # 幂等:UPDATE ... WHERE status='recorded'
        cursor.execute("RELEASE SAVEPOINT sp_chanrev_reverse")
        return {
            "reversed": True,
            "class": cls,
            "enqueued": False,
            "skipped": None,
            "dealer_resale_synced": dealer_resale_synced,
        }
    except Exception as _e:  # noqa: BLE001
        try:
            cursor.execute("ROLLBACK TO SAVEPOINT sp_chanrev_reverse")
        except Exception:
            pass
        # 同事务耐久补偿(exactly-once · 与退款状态原子);登记再失败不吞 → 传播 → 调用方事务回滚重试(fail-closed)
        from db.fund_recovery_db import insert_recovery_order_cursor
        insert_recovery_order_cursor(
            cursor, "channel_revenue", "reverse", ref_key=order_id,
            reason="退款完成渠道收益冲销失败·耐久 exactly-once 补偿", last_error=repr(_e),
            payload={"order_id": order_id},
        )
        logger.error("[channel_revenue] 退款冲销失败→已登记耐久补偿工单 order=%s: %s", order_id, _e)
        return {"reversed": False, "class": cls, "enqueued": True, "skipped": None}
