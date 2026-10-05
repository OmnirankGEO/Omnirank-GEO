"""
services/marketing/executors/reach.py — 触达执行器 + 频控五条(§9.3)

send_touch(user_id, channel, content, ...) 是核心原语:
  频控五条(硬实现):
    ① 同一用户 7 天 ≤1 次(freq_days config)
    ② 每日全局触达总上限(daily_global_cap config,超停)
    ③ 夜间勿扰 21:00-09:00(night_dnd_start/end config · 北京时区)
    ④ 退订/免打扰:DND 名单在查询层强制过滤(is_opted_out)
    ⑤ 服务商与终端分开计数(audience_segment 维度落库,可分设窗口/文案池)
  dry_run:notification flag 关时 → 记 status='dry_run'(不真实发送),供彩排对账。
  渠道:station(站内信 utils.notify)/ wecom(企微 webhook);sms/wecom_service 接口位不实现。

execute_approved_case(case):把审批通过的案件 touch_copy 发给其受众(per-user 直发;
  聚合 cohort 走 resolve_audience,已接线 cohort 直发,未接线返 note)。
"""
import logging
from datetime import datetime
from typing import Optional

import pytz

from db import marketing_db
from services.marketing.executors import wecom

logger = logging.getLogger("GEO-Marketing-Reach")
BEIJING_TZ = pytz.timezone("Asia/Shanghai")


def _in_night_dnd() -> bool:
    start = marketing_db.get_config_int("marketing.touch.night_dnd_start", 21)
    end = marketing_db.get_config_int("marketing.touch.night_dnd_end", 9)
    hour = datetime.now(BEIJING_TZ).hour
    if start <= end:
        return start <= hour < end
    # 跨零点(21..24 或 0..9)
    return hour >= start or hour < end


def _frequency_precheck(user_id: int, segment: str = "end") -> Optional[str]:
    """跑频控五条(不含渠道)。返回抑制原因(str)或 None(放行)。"""
    if marketing_db.is_opted_out(user_id):
        return "opted_out"                      # ④ 退订/DND
    if _in_night_dnd():
        return "night_dnd"                      # ③ 夜间勿扰
    freq_days = marketing_db.get_config_int("marketing.touch.freq_days", 7)
    if marketing_db.touched_within_days(user_id, freq_days):
        return "freq_7d"                        # ① 7 天≤1 次
    # [返工 R6-6] ⑤ 服务商/终端分开计数:各 segment 独立日上限(未单配则同全局默认值)
    global_cap = marketing_db.get_config_int("marketing.touch.daily_global_cap", 500)
    seg_cap = marketing_db.get_config_int(f"marketing.touch.daily_cap_{segment}", global_cap)
    if marketing_db.count_touches_today(segment=segment) >= seg_cap:
        return "daily_cap"                      # ② 每日上限(segment 维度)
    if marketing_db.count_touches_today() >= global_cap:
        return "daily_cap"                      # ② 每日全局上限(总闸仍在)
    return None


def _real_send(user_id: int, channel: str, title: str, content: str) -> tuple[bool, str]:
    """真实发送。station→站内信;wecom→企微;sms/wecom_service→接口位。"""
    if channel == "station":
        try:
            from utils.notify import notify_user, LEVEL_GENTLE
            notify_user(user_id, title or content[:40], level=LEVEL_GENTLE,
                        content=content, type="system")
            return True, "ok"
        except Exception as e:  # noqa: BLE001
            return False, f"station_err: {e}"
    if channel == "wecom":
        return wecom.send_wecom(content)
    if channel == "sms":
        return wecom.send_sms("", content)              # not_implemented
    if channel == "wecom_service":
        return wecom.send_wecom_service("", "", {})     # not_implemented
    return False, "unknown_channel"


def send_touch(*, user_id: int, channel: str, content: str, title: str = "",
               case_id: Optional[int] = None, campaign_id: Optional[int] = None,
               audience_segment: str = "end", force_dry_run: bool = False) -> dict:
    """发一条触达(过频控五条)。返回 {status, reason?}。

    dry_run 判据:kill_switch / master off / notification flag off / 显式 force_dry_run。
    """
    # 双闸 → 决定 dry_run
    kill = marketing_db.is_kill_switch_enabled()
    master = marketing_db.is_flag_enabled("marketing_agent.enabled", default=False)
    notify_on = marketing_db.is_flag_enabled("marketing_agent.notification.enabled", default=False)
    execute_on = marketing_db.is_flag_enabled("marketing_agent.execute.enabled", default=False)
    dry_run = force_dry_run or kill or (not master) or (not execute_on) or (not notify_on)

    # 频控预检(dry_run 也跑,便于彩排账真实反映会不会被抑制)
    reason = _frequency_precheck(user_id, segment=audience_segment)
    if reason:
        marketing_db.record_touch(user_id=user_id, channel=channel, audience_segment=audience_segment,
                                  case_id=case_id, campaign_id=campaign_id, content_ref=content[:200],
                                  status="suppressed", suppressed_reason=reason, dry_run=dry_run)
        return {"status": "suppressed", "reason": reason, "dry_run": dry_run}

    if dry_run:
        marketing_db.record_touch(user_id=user_id, channel=channel, audience_segment=audience_segment,
                                  case_id=case_id, campaign_id=campaign_id, content_ref=content[:200],
                                  status="dry_run", dry_run=True)
        return {"status": "dry_run"}

    ok, detail = _real_send(user_id, channel, title, content)
    status = "sent" if ok else "failed"
    marketing_db.record_touch(user_id=user_id, channel=channel, audience_segment=audience_segment,
                              case_id=case_id, campaign_id=campaign_id, content_ref=content[:200],
                              status=status, suppressed_reason=("" if ok else detail[:100]), dry_run=False)
    return {"status": status, "detail": detail}


def _extract_user_id(fingerprint: str) -> Optional[int]:
    import re
    fp = str(fingerprint or "")
    if fp.isdigit():
        return int(fp)
    m = re.match(r"^u(?:ser)?[:_]?(\d+)$", fp)
    return int(m.group(1)) if m else None


def resolve_audience(case: dict, limit: int = 500) -> tuple[list[int], str]:
    """把案件受众解析为 user_id 列表。per-user 案 → [uid];聚合 cohort → 查库。

    返回 (user_ids, note)。未接线的 cohort 返回 ([], 'cohort_not_wired')。
    """
    uid = _extract_user_id(case.get("fingerprint", ""))
    if uid is not None:
        return [uid], "single_user"

    # [P0-B] 手动起草案 rule_key=manual_draft,真实人群写在 execution_plan.cohort —— 建案与执行同一口径
    plan = case.get("execution_plan_jsonb") or case.get("execution_plan") or {}
    rule_key = (plan.get("cohort") if isinstance(plan, dict) else None) or case.get("rule_key", "")
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        # [返工 R6-5] 退订 DND 在查询层就滤(Agent/名单不可见被退订用户),发送层 is_opted_out 仍兜底
        if rule_key == "trial_exhausted_active":
            cur.execute("""SELECT w.user_id FROM user_wallets w
                           WHERE w.total_recharged = 0 AND (w.paid_points + w.bonus_points) = 0
                             AND NOT EXISTS (SELECT 1 FROM marketing_optouts o WHERE o.user_id = w.user_id)
                           LIMIT %s""", (limit,))
            return [r["user_id"] for r in cur.fetchall()], "cohort_trial_exhausted"
        if rule_key == "no_recharge_streak":
            # 全站活跃用户(近 7 天有过消费的)—— 限时加赠候选
            cur.execute("""SELECT DISTINCT pt.user_id FROM point_transactions pt
                           WHERE pt.type='consume' AND pt.created_at > NOW() - INTERVAL '7 days'
                             AND NOT EXISTS (SELECT 1 FROM marketing_optouts o WHERE o.user_id = pt.user_id)
                           LIMIT %s""", (limit,))
            return [r["user_id"] for r in cur.fetchall()], "cohort_recent_active"
        if rule_key == "register_no_diagnosis":
            # [P0-B] 新注册(30 天内)还没建过品牌的用户 —— 上手引导/首充候选
            cur.execute("""SELECT u.id AS user_id FROM users u
                           WHERE u.created_at > NOW() - INTERVAL '30 days'
                             AND NOT EXISTS (SELECT 1 FROM brands b
                                             WHERE b.owner_user_id = u.id AND b.deleted_at IS NULL)
                             AND NOT EXISTS (SELECT 1 FROM marketing_optouts o WHERE o.user_id = u.id)
                           LIMIT %s""", (limit,))
            return [r["user_id"] for r in cur.fetchall()], "cohort_register_no_diagnosis"
        if rule_key == "high_value_silent":
            # [P0-B] 充过值、近 14 天没有任何算力流水的沉默用户 —— 唤回候选
            cur.execute("""SELECT w.user_id FROM user_wallets w
                           WHERE w.total_recharged > 0
                             AND NOT EXISTS (SELECT 1 FROM point_transactions pt
                                             WHERE pt.user_id = w.user_id
                                               AND pt.created_at > NOW() - INTERVAL '14 days')
                             AND NOT EXISTS (SELECT 1 FROM marketing_optouts o WHERE o.user_id = w.user_id)
                           LIMIT %s""", (limit,))
            return [r["user_id"] for r in cur.fetchall()], "cohort_high_value_silent"
        return [], "cohort_not_wired"
    except Exception as e:  # noqa: BLE001
        logger.warning("[reach] resolve_audience 失败: %s", e)
        return [], "resolve_error"
    finally:
        conn.close()


def execute_approved_case(case_id: int) -> dict:
    """执行审批通过的案件触达(per-user 直发/聚合 cohort 展开)。返回汇总。"""
    case = marketing_db.get_case(case_id)
    if not case:
        return {"ok": False, "error": "case_not_found"}
    if case["status"] != "approved":
        return {"ok": False, "error": f"状态非 approved: {case['status']}"}
    # [返工 R6-4] 过期案不可执行(approved 后无限期可 execute = 旧方案被误执行;DB 时钟单源判)
    if marketing_db.is_case_expired(case_id):
        marketing_db.update_case_status(case_id, "expired")
        return {"ok": False, "error": "case_expired"}

    plan = case.get("execution_plan_jsonb") or {}
    # campaign_proposal:先落一个 draft 活动(供活动管理页激活;真正发放走 grant 闸/hook),再发公告触达
    if plan.get("type") == "campaign_proposal":
        try:
            budget = int((case.get("budget_cost_jsonb") or {}).get("points", 0) or 0)
            camp, _ = marketing_db.upsert_campaign(
                campaign_code=f"case_{case_id}_{plan.get('campaign','adhoc')}",
                campaign_type="adhoc", name=(case.get("trigger_reason") or "营销活动")[:60],
                budget_cap_points=max(budget, 1), params=plan.get("params") or {},
                status="draft", case_id=case_id)
            marketing_db.add_event(event_type="campaign_drafted", case_id=case_id,
                                   campaign_id=camp["id"], message="按案件生成草稿活动,待激活")
        except Exception as e:  # noqa: BLE001
            logger.warning("[reach] 生成草稿活动失败: %s", e)

    touch_copy = case.get("touch_copy_jsonb") or {}
    channels = [c for c in plan.get("channels", [])
                if c in ("station", "wecom")] or [ch for ch in touch_copy.keys() if ch in ("station", "wecom")]
    if not channels:
        # 诊断类/无渠道案(如 conversion_low):标执行完成,不触达
        marketing_db.update_case_status(case_id, "executed", set_executed=True)
        marketing_db.add_event(event_type="executed", case_id=case_id,
                               message="诊断类案件无触达,标记执行完成")
        return {"ok": True, "channels": [], "sent": 0, "note": "no_touch_channel"}

    user_ids, note = resolve_audience(case)
    segment = (case.get("audience_jsonb") or {}).get("segment", "end")
    counters = {"sent": 0, "dry_run": 0, "suppressed": 0, "failed": 0}
    for uid in user_ids:
        for ch in channels:
            content = touch_copy.get(ch) or touch_copy.get("station") or ""
            if not content:
                continue
            r = send_touch(user_id=uid, channel=ch, content=content,
                           case_id=case_id, campaign_id=None, audience_segment=segment)
            counters[r["status"]] = counters.get(r["status"], 0) + 1

    marketing_db.update_case_status(case_id, "executed", set_executed=True)
    marketing_db.add_event(event_type="executed", case_id=case_id,
                           message=f"触达执行完成 · {counters} · audience={note}",
                           payload={"counters": counters, "audience_note": note,
                                    "audience_size": len(user_ids)})
    return {"ok": True, "channels": channels, "audience": note,
            "audience_size": len(user_ids), **counters}
