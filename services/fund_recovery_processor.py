"""[v6 req3] fund_recovery_orders 处理器 · scheduler 每分钟认领到期 pending 工单并补偿。

- claim CAS(FOR UPDATE SKIP LOCKED)原子领单 → processing;
- kind='refund'(article_gen 线程窗口退款失败等)→ 重试 refund_points;成功→resolved;失败→退避重排,超限→manual+告警;
- kind='commit'/'release'/'state_fix'(GEO 结算冲突)→ 本就以 status='manual' 登记,scheduler 不领(claim 只取 pending);
  万一有 pending 的此类 → 退避直到转 manual(不自动动钱)。
"""
from __future__ import annotations

import logging

from db.fund_recovery_db import (
    claim_next_recovery_order, resolve_recovery_order_by_worker, requeue_recovery_order,
    refund_evidence_exists,
)

logger = logging.getLogger("GEO-FundRecoveryProc")


async def process_pending(limit: int = 50) -> dict:
    from middleware.billing import refund_points
    stats = {"claimed": 0, "resolved": 0, "requeued": 0, "manual": 0, "idempotent": 0}
    for _ in range(max(1, limit)):
        wo = claim_next_recovery_order("fund_recovery_processor")
        if not wo:
            break
        # [v7 对抗审 P3] claim 在 stale-processing 回收计次触顶时会把工单转 manual 并返回该行 → 跳过(不动钱)继续扫下一单。
        if wo.get("status") == "manual":
            stats["manual"] += 1
            continue
        stats["claimed"] += 1
        oid = wo["id"]
        token = wo.get("claim_token")   # [v7 finding3] worker fencing:resolve/requeue 必须回传本次 lease token
        kind = wo.get("kind")
        try:
            if kind == "refund" and wo.get("user_id") and wo.get("feature_code"):
                # [v9 P1-3] 按工单登记那刻 deduct 落账的账本(ledger_type)精确退款,不再让 refund_points
                #   按【退款时刻】的 _is_v35_customer 猜(客户 legacy→v35 迁移后会查错账本 → 退款失败转 manual)。
                # [v10 第1轮对抗审 P2] 透传 amount_points → 部分退款工单(如文章部分失败退 _err×base)重试时【只退指定额】,
                #   不退全额(否则 over-refund)。amount_points 为 None(全额退工单)→ amount=None → refund_points 全额退(兼容)。
                rr = await refund_points(wo["user_id"], wo["feature_code"],
                                         reason="补偿工单自动退款", charge_tx_id=wo.get("charge_tx_id"),
                                         amount=wo.get("amount_points"),
                                         ledger_type=wo.get("ledger_type"))
                if rr and rr.get("success") is True:
                    resolve_recovery_order_by_worker(oid, token, note=f"auto refund ok: {rr}")
                    stats["resolved"] += 1
                elif refund_evidence_exists(
                    wo.get("user_id"),
                    wo.get("charge_tx_id"),
                    wo.get("ledger_type"),
                    wo.get("feature_code"),
                ):
                    # [v7 finding6] 退款成功后未 resolved 就崩溃 → 重试得 success=False(已退/未找到),但退款流水证据在
                    #   → 幂等已完成 · resolved(不误进 manual)。
                    resolve_recovery_order_by_worker(oid, token, note=f"已退款(退款流水证据 · 幂等收口): {rr}")
                    stats["idempotent"] += 1
                    stats["resolved"] += 1
                    logger.warning(f"[fund_recovery_proc] 工单 {oid} refund 返 {rr} 但已有退款证据 → 幂等 resolved")
                else:
                    _requeue(stats, oid, token, f"refund 未成功且无退款证据: {rr}")
            elif wo.get("source") == "channel_revenue" and kind in ("record", "reverse"):
                # [v9 P1-2 · v10 item3] 渠道收益记账/冲销 exactly-once 耐久补偿(record/reverse 均幂等)。
                #   三态:done=幂等收口 · defer=退款在途正常等待(只延后不累计重试次数) · retry=瞬时失败退避。
                _oc = _retry_channel_revenue(wo)
                if _oc == "done":
                    resolve_recovery_order_by_worker(oid, token, note=f"渠道收益 {kind} 幂等补偿收口")
                    stats["resolved"] += 1
                elif _oc == "defer":
                    # [v10 第1轮对抗审 P3] 正常在途(pending/approved)→ defer(只延后不累计重试)· 防误触 MAX→manual。
                    #   但若退款【长期卡在 in-flight】(工单存活 > 7 天不落终态)→ escalate 人工(防永久静默漏记):
                    #   转 requeue(累计 retry_count → 最终 MAX→manual + CRITICAL),不再无限 defer。
                    if _defer_too_stale(wo):
                        logger.critical(f"[fund_recovery_proc] channel_revenue {kind} 工单 {oid} 退款长期在途(>7d 未落终态)"
                                        f"→ escalate 人工核查 ref={wo.get('ref_key')}")
                        _requeue(stats, oid, token, "退款长期在途(>7d)· escalate 人工核查")
                    else:
                        from db.fund_recovery_db import defer_recovery_order
                        # [v10 第2轮对抗审 nit] 检查返回:lease 失效(迟到 worker · 被 stale reaper 重分派)→ 不虚增 deferred
                        if defer_recovery_order(oid, token, note=f"渠道收益 {kind} 退款在途·延后待终态"):
                            stats["deferred"] = stats.get("deferred", 0) + 1
                else:
                    _requeue(stats, oid, token, f"渠道收益 {kind} 补偿未成功(下轮再试)")
            else:
                # 无自动处理器(commit/release 冲突 / state_fix / 缺 user/feature)→ 退避直到转 manual(不自动动钱)
                _requeue(stats, oid, token, f"kind={kind} 无自动处理器 · 待人工")
        except Exception as e:
            logger.exception(f"[fund_recovery_proc] 工单 {oid} 处理异常: {e}")
            _requeue(stats, oid, token, repr(e))
    return stats


_DEFER_STALE_SEC = 7 * 86400   # [v10 P3] 在途工单存活超此(7d)→ escalate 人工(防永久静默 defer 漏记)


def _defer_too_stale(wo) -> bool:
    """[v10 第1轮对抗审 P3] 工单存活是否已过久(退款长期卡 in-flight)· created_at 缺失/异常 → 保守 False(继续 defer)。"""
    created = wo.get("created_at")
    if created is None:
        return False
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        if getattr(created, "tzinfo", None) is None:
            # naive datetime → 视为 UTC(与 DB NOW() 一致)
            created = created.replace(tzinfo=timezone.utc)
        return (now - created).total_seconds() > _DEFER_STALE_SEC
    except Exception:
        return False


def _retry_channel_revenue(wo) -> str:
    """[v9 P1-2 · v10 item3] 幂等补偿渠道收益 record / reverse · 返回三态字符串:
      'done'  = 已确认终态(收口)· 'defer' = 退款在途正常等待(只延后不累计重试)· 'retry' = 瞬时失败/未确认(退避重排)。

    - reverse:调 reverse_channel_revenue(recorded→reversed 幂等)· 校验该 order 台账无 recorded 残留;
    - record:按【refund_status 唯一口径】(services.channel_revenue_lifecycle.record_action)决策:
        record→补记 · skip→已退款跳过收口 · defer→在途延后 · unknown→fail-closed 退避(不猜成功·escalate 人工)。
    自开事务(get_db 出块提交)· 抛错由上层 process_pending 捕获退避。
    """
    import json as _json
    from db.connection import get_db
    payload = wo.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = _json.loads(payload)
        except Exception:
            payload = {}
    order_id = payload.get("order_id") or wo.get("ref_key")
    if not order_id:
        return "retry"
    order_id = str(order_id)
    kind = wo.get("kind")
    with get_db() as conn:
        cur = conn.cursor()
        if kind == "reverse":
            from services.channel_pricing import reverse_channel_revenue
            reverse_channel_revenue(cur, order_id)
            cur.execute("SELECT status FROM channel_revenue_ledger WHERE recharge_order_id=%s", (order_id,))
            rows = cur.fetchall()
            if not rows:
                return "done"  # 从未记账 → 无需冲销,收口
            return "done" if all((r["status"] if isinstance(r, dict) else r[0]) == "reversed" for r in rows) else "retry"
        # record
        # 🔴 [v10 第1轮对抗审 P2] FOR UPDATE 锁订单行:与退款冲销路径(canonical reverse_channel_revenue_on_refund
        #   同样 FOR UPDATE recharge_orders)【跨事务串行化】。否则蓝绿双实例下 record worker(无锁读旧 refund_status)
        #   与并发退款 reverse 写偏序 → record worker 插入 recorded 行、reverse 看不到未提交行命中 0 → 已退款订单残留
        #   幽灵 recorded 幽灵行永不冲销(渠道收益多计)。加锁后:退款在途则 record 阻塞到退款提交→读到生效态→skip。
        cur.execute("SELECT * FROM recharge_orders WHERE id=%s FOR UPDATE", (order_id,))
        order = cur.fetchone()
        if not order:
            return "done"  # 订单已不存在(回滚/删除)→ 无需记账,收口
        # [v10 item3] 唯一口径决策(record/skip/defer/unknown)· 消除 v9 散在 processor 的 refund_status 猜测。
        from services.channel_revenue_lifecycle import record_action
        _rs = order.get("refund_status") if isinstance(order, dict) else None
        _act = record_action(_rs)
        if _act == "skip":
            # 款已真退回 → 补记会成"已退款却计收益"的幽灵 recorded 行 → 跳过并收口。
            logger.info("[fund_recovery_proc] channel_revenue record 跳过:order=%s 已真退款(refund_status=%s)", order_id, _rs)
            return "done"
        if _act == "defer":
            # 退款在途(pending/approved)= 正常等待 → 延后不累计重试次数(不因等待转 manual)。
            logger.info("[fund_recovery_proc] channel_revenue record 延后:order=%s 退款在途(refund_status=%s)", order_id, _rs)
            return "defer"
        if _act == "unknown":
            # fail-closed:未知 refund_status 不猜成功 → 退避(累计后 escalate manual 供人工),不记账不结案。
            logger.critical("[fund_recovery_proc] channel_revenue record fail-closed:order=%s 未知 refund_status=%s · 等人工", order_id, _rs)
            return "retry"
        # 'record'(failed/rejected/none/NULL = 款未退·订单成立)→ 渠道收益仍欠 → 正常补记。
        from db.wallet_db import _do_record_channel_revenue
        _do_record_channel_revenue(cur, dict(order))
        cur.execute("SELECT status FROM channel_revenue_ledger WHERE recharge_order_id=%s", (order_id,))
        row = cur.fetchone()
        if row is None:
            return "done"  # 不适用(无直属渠道 procurement 报价)→ 收口
        return "done" if (row["status"] if isinstance(row, dict) else row[0]) == "recorded" else "retry"


def _requeue(stats, oid, token, err):
    from db.fund_recovery_db import MAX_AUTO_ATTEMPTS
    n = requeue_recovery_order(oid, error=err, claim_token=token)
    if n == -1:
        # [v7 finding3] lease 已失效(被 stale reaper 重新分派)→ 本 worker 不再拥有该单 · 不计数(新 worker 会处理)
        return
    if n >= MAX_AUTO_ATTEMPTS:
        stats["manual"] += 1
    else:
        stats["requeued"] += 1
