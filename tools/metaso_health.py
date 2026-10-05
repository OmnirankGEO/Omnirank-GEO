"""秘塔返回可用性判定 + 连续业务失败告警(2026-08-04 止血包)

背景(2026-08-04 生产实证):
  秘塔的**业务级失败走 HTTP 200 通道** —— 余额不足时返回
  `{"errCode": 3000, "errMsg": "余额不足"}`,HTTP 状态仍是 200。
  原实现一律 `if status == 200 → success`,导致四连塌:
    1. llm_call_log 记 success=True(假绿,¥0.05 照记)
    2. 多账号 failover 不触发(断血账号不会切备用 key)
    3. 上层 `if "error" in result` 守卫判不出 → 兜底
       estimate_competition_from_keyword 被绕过
    4. webpages 缺失 → A/B/C 全 0 → effective_competition 落地板值 1
       → 报价按「零竞争」出,篇数落 MIN_ARTICLES_DEFAULT 打底
  当天两个真客户报价(quote 421/422)共 39 个词竞争强度全部 = 1,
  quote 422 按此卖 35 篇,按真实竞争(23–63)应需 83 篇,交付覆盖率 42.2%。

本模块提供**唯一**的可用性判据,供所有 metaso 出口共用,避免各处各判一套。
"""

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger("GEO-MetasoHealth")

# 正常搜索响应必带的信封字段(用于区分「真·零结果」与「响应体根本不对」)
_ENVELOPE_KEYS = ("searchParameters", "credits", "total")

# 连续多少次业务级失败才拉告警(避免偶发抖动刷屏)
METASO_CONSECUTIVE_ALERT_THRESHOLD = 20

METASO_ALERT_RULE_KEY = "metaso_business_failure"
METASO_ALERT_FINGERPRINT = "metaso:business_failure"


def metaso_result_error(result: Any) -> Optional[str]:
    """判秘塔返回是否可用。

    返回 None = 可用;返回字符串 = 不可用的原因(可直接进日志/告警/留痕)。

    判为不可用的四条(任一命中):
      1. 已被上游包成 {"error": ...}(HTTP 非 200 / 异常 / 全账号 failover 耗尽)
      2. body 里 errCode 非 0 —— 秘塔业务级失败走 200 通道的那一类(如 3000 余额不足)
      3. 响应体缺 webpages 键(根本不是搜索结果的形状)
      4. webpages 为空**且**信封字段全无(searchParameters/credits/total)

    **刻意不判为失败的一条**:webpages == [] 但信封完整。
      那是冷门词的真·零结果,是合法业务事实,不该兜底也不该告警。
      这条就是验收要求里的「正常空结果不告警」反向对照。
    """
    if result is None:
        return "空响应(None)"
    if not isinstance(result, dict):
        return f"响应不是 dict(实际 {type(result).__name__})"

    if "error" in result:
        return (str(result.get("error")) or "error")[:200]

    code = result.get("errCode", result.get("errcode"))
    if code is not None:
        try:
            code_is_bad = int(code) != 0
        except (TypeError, ValueError):
            # 非数字 errCode 一律视为失败:宁可多兜底,不可再假绿
            code_is_bad = True
        if code_is_bad:
            msg = result.get("errMsg") or result.get("errmsg") or ""
            return f"errCode={code} {msg}".strip()[:200]

    pages = result.get("webpages")
    if pages is None:
        return "响应体缺 webpages 键(不是搜索结果形状)"
    if isinstance(pages, list) and not pages:
        if not any(k in result for k in _ENVELOPE_KEYS):
            return "webpages 为空且响应信封缺失(searchParameters/credits/total 均无)"
        # 信封完整的零结果 → 合法,放行

    return None


# ============================================================
# 连续业务失败 → 告警一次(去重)· 恢复 → 再告一次
# ============================================================

_state = {"consecutive": 0, "alerting": False}
_state_lock = threading.Lock()


def reset_metaso_health_state() -> None:
    """重置计数与告警态。当前调用点只有测试;不新增「只读快照」之类没人调的函数
    (死函数 = 建了没接线,复审会当缺陷)。真要看状态请查 ai_ops_alerts 表。"""
    with _state_lock:
        _state["consecutive"] = 0
        _state["alerting"] = False


def record_metaso_result(ok: bool, reason: str = "", *, source: str = "") -> Optional[str]:
    """记一次秘塔调用的**业务级**结果,按需拉/收告警。

    ok=True  → 计数清零;若此前在告警中,收一次恢复告警。
    ok=False → 连续计数 +1;达到阈值且尚未告警 → 拉一次告警(之后保持静默,不刷屏)。

    返回 'raise' / 'resolve' / None,表示本次是否真的产生了告警动作(便于测试断言)。

    注:计数是**进程内**的。生产 WORKERS=1,web 进程内计数即全量;
    cron 容器是独立进程、独立计数,但告警层按 (rule_key, fingerprint) 去重,
    多进程同时拉也只会有一条 firing 行。
    """
    action = None
    n = 0
    with _state_lock:
        if ok:
            _state["consecutive"] = 0
            if _state["alerting"]:
                _state["alerting"] = False
                action = "resolve"
        else:
            _state["consecutive"] += 1
            n = _state["consecutive"]
            if not _state["alerting"] and n >= METASO_CONSECUTIVE_ALERT_THRESHOLD:
                _state["alerting"] = True
                action = "raise"

    if action == "raise":
        _fire_alert(n, reason, source)
    elif action == "resolve":
        _resolve_alert()
    return action


def _fire_alert(consecutive: int, reason: str, source: str) -> None:
    detail = (
        f"秘塔搜索连续 {consecutive} 次业务级失败(最近原因:{reason or '未知'};"
        f"调用点:{source or '未标注'})。"
        f"竞争强度已改走经验兜底 estimate_competition_from_keyword,"
        f"期间新出的报价竞争维不是实测值。请检查秘塔账号余额/配额。"
    )
    logger.error("[MetasoHealth] %s", detail)
    try:
        from db import ai_ops_db
        ai_ops_db.upsert_alert(
            METASO_ALERT_RULE_KEY,
            severity="critical",
            title="秘塔搜索不可用 · 报价竞争强度已降级为经验兜底",
            detail=detail,
            fingerprint=METASO_ALERT_FINGERPRINT,
            payload={"consecutive": consecutive, "reason": reason, "source": source},
        )
    except Exception as exc:
        # 告警通道自身故障不能再吞 —— 至少留 error 日志(日志是最后一道人眼面)
        logger.error("[MetasoHealth] 告警落库失败: %s", exc)


def _resolve_alert() -> None:
    logger.warning("[MetasoHealth] 秘塔搜索已恢复,收起告警")
    try:
        from db import ai_ops_db
        ai_ops_db.resolve_alerts(METASO_ALERT_RULE_KEY, METASO_ALERT_FINGERPRINT)
    except Exception as exc:
        logger.warning("[MetasoHealth] 告警恢复失败: %s", exc)
