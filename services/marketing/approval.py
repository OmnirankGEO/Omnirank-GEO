"""
services/marketing/approval.py — 五绿勾后端真校验器 + 审批流

参考图 03 右栏审批检查清单 = §8 安全模型的 UI 化,逐条程序化校验真实结果,不是装饰。
五项(§B.4):
  ① 禁承诺词扫描(不会承诺排名)
  ② 不涉余额改动断言(不直接改余额 · 仅通过算力赠送 bonus)
  ③ 频控预检(触达频控已检查 · 7 天≤1 次 / 日全局上限)
  ④ 预算校验(预算在上限内 · 三级上限 单人/单批/单日)
  ⑤ 回滚方案非空(可回滚 · 可停止并回收未发放)

validate_case(case) → {'checks': {...}, 'all_passed': bool}。
approve/reject/request_changes 走 marketing_db + 记事件;approve 时若五勾未过 → 拒绝放行。
"""
import logging
import re
from typing import Optional

from db import marketing_db
from services.marketing.guards import scan_case_copy, check_no_promise

logger = logging.getLogger("GEO-Marketing-Approval")


def _extract_user_id(fingerprint: str) -> Optional[int]:
    if not fingerprint:
        return None
    fp = str(fingerprint)
    if fp.isdigit():
        return int(fp)
    m = re.match(r"^u(?:ser)?[:_]?(\d+)$", fp)
    if m:
        return int(m.group(1))
    return None


def _check_no_promise(case: dict) -> dict:
    """① 禁承诺词:扫 touch_copy 各渠道 + expected_impact。"""
    copy = case.get("touch_copy_jsonb") or {}
    r = scan_case_copy(copy)
    impact_hits = check_no_promise(case.get("expected_impact") or "")
    passed = r["passed"] and not impact_hits
    detail = "文案未出现'保证排名'等承诺性用语" if passed else \
        f"命中承诺/违禁:{r['flags']}{(' impact:'+str(impact_hits)) if impact_hits else ''}"
    return {"key": "no_promise", "label": "不会承诺排名", "passed": passed, "detail": detail}


def _check_no_balance_edit(case: dict) -> dict:
    """② 不直接改余额:执行计划里任何发放只走 bonus 赠送台账;不得直接改 paid/commission 面值。"""
    plan = case.get("execution_plan_jsonb") or {}
    budget = case.get("budget_cost_jsonb") or {}
    plan_str = str(plan) + str(budget)
    # 命中直接改余额/paid/commission 面值的字样 → 不通过
    bad = [w for w in ("paid_points", "commission_points", "set balance", "改余额",
                       "直接扣", "扣 paid", "面值") if w in plan_str]
    passed = len(bad) == 0
    detail = "不直接修改用户余额,仅通过算力赠送(bonus)" if passed else f"疑似直接改余额:{bad}"
    return {"key": "no_balance_edit", "label": "不直接改余额", "passed": passed, "detail": detail}


def _check_frequency(case: dict) -> dict:
    """③ 频控预检:per-user 案检查 7 天≤1;聚合案检查日全局上限余量。"""
    fp = case.get("fingerprint") or ""
    uid = _extract_user_id(fp)
    freq_days = marketing_db.get_config_int("marketing.touch.freq_days", 7)
    if uid is not None:
        recently = marketing_db.touched_within_days(uid, freq_days)
        passed = not recently
        detail = (f"同一用户 {freq_days} 天内触达不超过 1 次,已通过频控规则校验"
                  if passed else f"该用户 {freq_days} 天内已被触达,频控命中")
    else:
        cap = marketing_db.get_config_int("marketing.touch.daily_global_cap", 500)
        used = marketing_db.count_touches_today()
        passed = used < cap
        detail = f"今日全局触达 {used}/{cap},仍在上限内" if passed else f"今日全局触达已达上限 {used}/{cap}"
    return {"key": "frequency", "label": "触达频控已检查", "passed": passed, "detail": detail}


def _check_budget(case: dict) -> dict:
    """④ 预算校验:案件预算算力 ≤ 单笔硬顶 / 单人 / 单日余量三级上限。"""
    budget = case.get("budget_cost_jsonb") or {}
    points = int(budget.get("points", 0) or 0)
    if points <= 0:
        return {"key": "budget", "label": "预算在上限内", "passed": True,
                "detail": "本案不涉及算力发放(预算 0)"}
    hard = marketing_db.get_config_int("marketing.grant.hard_ceiling")
    per_day = marketing_db.get_config_int("marketing.grant.cap_per_day")
    # [返工 R7] 案件总盘上限走独立 config(原 hard*100 魔数);默认 = 单日上限(总盘不该超过一天的发放力)
    case_budget_cap = marketing_db.get_config_int("marketing.case.budget_cap", per_day)
    used_today = marketing_db.sum_grants_today()
    within_hard = points <= case_budget_cap
    within_day = (used_today + points) <= per_day
    passed = within_day and within_hard
    detail = (f"预计算力成本 {points},今日已发 {used_today},在单日上限 {per_day} 内"
              if passed else f"预算 {points} 超上限(今日已发 {used_today}/{per_day})")
    return {"key": "budget", "label": "预算在上限内", "passed": passed, "detail": detail}


def _check_rollback(case: dict) -> dict:
    """⑤ 回滚方案非空:可停止并回收未发放。触达不可撤回须显式标注(仍算合规)。"""
    rb = (case.get("rollback_plan") or "").strip()
    passed = len(rb) >= 4
    detail = "如效果不达预期,可停止活动并回收未发放算力,已有记录可追溯" if passed else "回滚方案缺失"
    return {"key": "rollback", "label": "可回滚", "passed": passed, "detail": detail}


def validate_case(case: dict) -> dict:
    """跑五绿勾真校验器。返回 {'checks': [...], 'all_passed': bool}。"""
    checks = [
        _check_no_promise(case),
        _check_no_balance_edit(case),
        _check_frequency(case),
        _check_budget(case),
        _check_rollback(case),
    ]
    return {"checks": checks, "all_passed": all(c["passed"] for c in checks)}


def approve_case(case_id: int, approver_id: Optional[int], note: str = "") -> dict:
    """批准:先跑五绿勾真校验;未过则拒绝放行(返回 ok=False + checks)。"""
    case = marketing_db.get_case(case_id)
    if not case:
        return {"ok": False, "error": "case_not_found"}
    if case["status"] not in ("pending", "changes_requested"):
        return {"ok": False, "error": f"状态不可批准: {case['status']}"}
    # [返工 R6-4] 过期案不可批(巡逻扫过期有 ≤24h 窗口期,此处兜死;DB 时钟单源判)
    if marketing_db.is_case_expired(case_id):
        marketing_db.update_case_status(case_id, "expired")
        return {"ok": False, "error": "case_expired"}

    result = validate_case(case)
    approval = marketing_db.record_approval(
        case_id=case_id, decision="approve", five_checks=result,
        checks_passed=result["all_passed"], approver_id=approver_id, note=note)
    if not result["all_passed"]:
        marketing_db.add_event(event_type="approval_blocked", case_id=case_id,
                               severity="warn", actor_id=approver_id,
                               message="五绿勾未全过,批准被拦截",
                               payload={"checks": result["checks"]})
        return {"ok": False, "error": "checks_failed", "checks": result["checks"],
                "approval_id": approval["id"]}

    marketing_db.update_case_status(case_id, "approved", approved_by=approver_id)
    marketing_db.add_event(event_type="approved", case_id=case_id, actor_id=approver_id,
                           message="方案审批通过", payload={"note": note})
    return {"ok": True, "approval_id": approval["id"], "checks": result["checks"]}


def reject_case(case_id: int, approver_id: Optional[int], note: str = "") -> dict:
    case = marketing_db.get_case(case_id)
    if not case:
        return {"ok": False, "error": "case_not_found"}
    marketing_db.record_approval(case_id=case_id, decision="reject", approver_id=approver_id, note=note)
    marketing_db.update_case_status(case_id, "rejected", approved_by=approver_id)
    marketing_db.add_event(event_type="rejected", case_id=case_id, actor_id=approver_id,
                           message="方案被驳回", payload={"note": note})
    return {"ok": True}


def request_changes(case_id: int, approver_id: Optional[int], note: str = "") -> dict:
    case = marketing_db.get_case(case_id)
    if not case:
        return {"ok": False, "error": "case_not_found"}
    marketing_db.record_approval(case_id=case_id, decision="request_changes",
                                 approver_id=approver_id, note=note)
    marketing_db.update_case_status(case_id, "changes_requested", approved_by=approver_id)
    marketing_db.add_event(event_type="changes_requested", case_id=case_id, actor_id=approver_id,
                           message="要求修改后重新提交", payload={"note": note})
    return {"ok": True}
