"""[v5 req4 · Deploy-CTO 2026-07-13] 文章生成 · 扣费后线程未启动窗口的资金/状态【耐久收口】。

从 server.py 抽出为模块级可测函数(依赖注入 refund/release/timeout/recovery 四个 callable),
使"强制 thread.start 抛异常 → 4 分支(退款成功/退款 false/退款异常/状态写失败)"可被真实驱动测试。

铁律:禁 except: pass。任何退款/状态恢复失败都必须【落耐久补偿工单】(fund_recovery_orders)或至少 CRITICAL,
      绝不静默吞掉资金/状态失败。
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("GEO-ArticleWriteRecovery")


async def handle_write_window_failure(
    *,
    accepted: list,
    thread_started: bool,
    deducted: bool,
    bill_user_id: Optional[int],
    charge_tx_id: Optional[int],
    quote_id: Any,
    ledger_type: Optional[str] = None,               # [v10 item2] 扣费实际落的账本('legacy'|'v35')· 透传给 refund_fn 精确退原账本
    refund_fn: Callable[..., Awaitable[dict]],        # async (user_id, feature, *, reason, charge_tx_id, ledger_type) -> {"success": bool}
    release_fn: Callable[[list, Any], Any],           # (topic_ids, quote_id) -> None · 恢复 topics 回 draft(可重试)
    timeout_fn: Callable[[list, Any], Any],           # (topic_ids, quote_id) -> None · 标 write_timeout(不可重选)
    recovery_fn: Callable[..., Any],                  # (kind, *, refund_err=, state_err=) -> None · 落耐久补偿工单
) -> dict:
    """返回 {"outcome": <state>} 便于断言。

    outcome:
      - "noop"                       :不在"已 accept 且线程未启动"窗口
      - "released_no_charge"         :未扣费 → 直接 release 回 draft
      - "refunded_and_released"      :已扣费 → 退款成功 + release 成功(可重试)
      - "refunded_state_fix_workorder":退款成功但 release 失败 → 落 state_fix 工单
      - "refund_failed_workorder"    :退款 false/异常 → 标 write_timeout + 落 refund 工单
      - "outer_workorder"            :兜底异常 → 落 refund 工单(不 raise)
    """
    if not (accepted and not thread_started):
        return {"outcome": "noop"}
    try:
        if not deducted:
            release_fn(accepted, quote_id)
            return {"outcome": "released_no_charge"}

        refunded_ok = False
        refund_err: Optional[str] = None
        if bill_user_id:
            try:
                rr = await refund_fn(bill_user_id, "article_gen",
                                     reason="扣费后线程未启动 自动退款", charge_tx_id=charge_tx_id,
                                     ledger_type=ledger_type)  # [v10 item2] 精确退原扣费账本
                refunded_ok = bool(rr and rr.get("success"))
                if not refunded_ok:
                    refund_err = f"refund_points 返回未成功: {rr}"
            except Exception as we:
                refund_err = repr(we)
                logger.error(f"[写作窗口退款异常] quote={quote_id} err={we}")
        else:
            refund_err = "无计费用户(bill_user_id 为空)但标记了已扣费"

        if refunded_ok:
            try:
                release_fn(accepted, quote_id)
                logger.info(f"[写作窗口] quote={quote_id} 扣费已精确退款一次 · topics 恢复可重试")
                return {"outcome": "refunded_and_released"}
            except Exception as se:
                # 退款成功但状态恢复失败 → 状态/资金已不一致 · 落耐久工单待补偿(不吞)
                logger.error(f"[写作窗口·状态恢复失败] quote={quote_id} 退款成功但 release 失败: {se}")
                recovery_fn("state_fix", state_err=repr(se))
                return {"outcome": "refunded_state_fix_workorder"}
        else:
            try:
                timeout_fn(accepted, quote_id)
            except Exception as me:
                logger.error(f"[写作窗口·标 write_timeout 失败] quote={quote_id}: {me}")
            logger.error(f"[写作窗口 · 需人工告警] quote={quote_id} 扣费已发生但退款未成功 · "
                         f"topics={accepted} 已标 write_timeout · 落耐久补偿工单(勿重选双扣)")
            recovery_fn("refund", refund_err=refund_err)
            return {"outcome": "refund_failed_workorder"}
    except Exception as outer:
        # 🔴 禁 except:pass · 兜底异常 CRITICAL + 尽力落耐久工单(不 raise 掩盖原始异常)
        logger.critical(f"[写作窗口·补偿兜底异常] quote={quote_id} 收口失败,落耐久工单: {outer!r}")
        recovery_fn("refund", refund_err=repr(outer))
        return {"outcome": "outer_workorder"}
