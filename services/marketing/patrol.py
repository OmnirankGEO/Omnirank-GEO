"""
services/marketing/patrol.py — 军师每日巡逻(照抄 services/ai_ops/patrol.py 房规)

run_patrol(force):
  - 双闸首行:非 force 且 master flag(marketing_agent.enabled)关 → 直接返回(只读观测本身也随总闸)。
  - kill switch 生效则不产案(只观测)。
  - expire_due_cases:先把过期未批的 pending 案作废(防旧方案误执行)。
  - get_marketing_signals 一次 round-trip → RULES(10 条纯函数)逐条评估 → DraftSpec。
  - 每条 firing:7 天频控(has_recent_case)+ 日期域幂等键(case_key)双重去重 → 建议案件落库。
  - 确定性草稿过 pydantic 10 字段强校验;LLM 润色 flag 关时不跑(默认)。
  - 每案 add_event('suggestion_generated');最后 record_patrol_run 打卡。
run_patrol_job():scheduler 入口,吞异常永不抛(照 ai_ops run_patrol_job)。
"""
import asyncio
import logging
import time
from datetime import date, datetime, timedelta
from typing import Optional

from db import marketing_db
from services.marketing import signal_rules
from services.marketing.case_schema import MarketingCaseDraft
from services.marketing.skill_packs import assemble_system_prompt

logger = logging.getLogger("GEO-Marketing-Patrol")

# 案件 pending 有效期(过期未批自动作废)· 天
CASE_TTL_DAYS = 14


def _build_case_draft(spec: dict, touch_copy: Optional[dict] = None) -> Optional[MarketingCaseDraft]:
    """从 rule DraftSpec 构确定性 MarketingCaseDraft(过 10 字段强校验)。失败 → None(跳过)。"""
    try:
        b = spec.get("budget") or {}
        aud = spec.get("audience") or {}
        return MarketingCaseDraft(
            rule_key=spec["rule_key"],
            fingerprint=spec.get("fingerprint", ""),
            awareness_stage=spec.get("awareness_stage", "unknown"),
            owner_scope=spec.get("owner_scope", "platform"),
            skill_packs=spec.get("skill_packs", []),
            trigger_reason=spec.get("trigger_reason", ""),
            evidence=spec.get("evidence") or {},
            audience={
                "definition": aud.get("definition", ""),
                "count": int(aud.get("count", 0) or 0),
                "segment": aud.get("segment", "end"),
            },
            expected_impact=spec.get("expected_impact", ""),
            budget_cost={
                "points": int(b.get("points", 0) or 0),
                "cost_estimate_yuan": float(b.get("cost_estimate_yuan", 0) or 0),
            },
            risk_level=spec.get("risk_level", "low"),
            touch_copy=touch_copy if touch_copy is not None else (spec.get("touch_copy") or {}),
            execution_plan=spec.get("execution_plan") or {},
            rollback_plan=spec.get("rollback_plan", ""),
            observation_window=int(spec.get("observation_window", 7) or 7),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[patrol] 案件草稿校验失败 rule=%s fp=%s: %s",
                       spec.get("rule_key"), spec.get("fingerprint"), e)
        return None


def _maybe_enrich(spec: dict) -> Optional[dict]:
    """若 advisor_llm flag 开,润色 touch_copy(新事件循环里跑 async)。flag 关/失败 → None(用确定性版)。"""
    if not marketing_db.is_flag_enabled("marketing_agent.advisor_llm.enabled", default=False):
        return None
    try:
        from services.marketing.advisor_llm import enrich_touch_copy
        system_prompt = assemble_system_prompt(
            awareness_stage=spec.get("awareness_stage", "unknown"),
            rule_key=spec.get("rule_key", ""),
            task_hint="润色触达文案")
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(enrich_touch_copy(spec, system_prompt))
        finally:
            loop.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("[patrol] LLM 润色失败(用确定性草稿): %s", e)
        return None


def run_patrol(force: bool = False) -> Optional[dict]:
    """跑一轮巡逻。返回 {signals_matched, cases_opened, cases_suppressed, duration_ms} 或 None(信号查询失败)。"""
    if not force and not marketing_db.is_flag_enabled("marketing_agent.enabled", default=False):
        return None
    if marketing_db.is_kill_switch_enabled():
        # 急停:只观测不产案(保安不下岗,但不自主行动)
        return {"signals_matched": 0, "cases_opened": 0, "cases_suppressed": 0,
                "duration_ms": 0, "note": "kill_switch"}

    t0 = time.time()
    # 过期未批作废
    expired = marketing_db.expire_due_cases()

    signals = marketing_db.get_marketing_signals()
    if signals is None:
        return None

    freq_days = marketing_db.get_config_int("marketing.touch.freq_days", 7)
    today = date.today().isoformat()
    matched = opened = suppressed = 0

    for rule_key, rule_fn in signal_rules.RULES:
        try:
            specs = rule_fn(signals) or []
        except Exception as e:  # noqa: BLE001
            logger.warning("[patrol] 规则 %s 本轮失败,跳过(不消警): %s", rule_key, e)
            continue

        for spec in specs:
            matched += 1
            fp = spec.get("fingerprint", "")
            # 7 天频控(同 规则×目标 一周内不重复刷屏军情队列)
            if marketing_db.has_recent_case(rule_key, fp, within_days=freq_days):
                suppressed += 1
                continue

            enriched_copy = _maybe_enrich(spec)
            draft = _build_case_draft(spec, touch_copy=enriched_copy)
            if draft is None:
                suppressed += 1
                continue

            case_key = f"mkt:{rule_key}:{fp}:{today}"
            expires_at = datetime.now() + timedelta(days=CASE_TTL_DAYS)
            kwargs = draft.to_db_kwargs(
                case_key=case_key, status="pending",
                llm_model=("deepseek-v4-flash" if enriched_copy else ""),
                created_by=None, expires_at=expires_at)
            try:
                case, created = marketing_db.create_case(**kwargs)
            except Exception as e:  # noqa: BLE001
                logger.warning("[patrol] 建案失败 rule=%s fp=%s: %s", rule_key, fp, e)
                suppressed += 1
                continue

            if created and case:
                opened += 1
                marketing_db.backfill_case_no(case["id"])
                marketing_db.add_event(
                    event_type="suggestion_generated", case_id=case["id"],
                    message=f"巡逻命中 {rule_key} · {spec.get('trigger_reason','')[:80]}",
                    payload={"rule_key": rule_key, "fingerprint": fp,
                             "awareness_stage": spec.get("awareness_stage")})
            else:
                suppressed += 1

    duration_ms = int((time.time() - t0) * 1000)
    marketing_db.record_patrol_run(
        signals_matched=matched, cases_opened=opened, cases_suppressed=suppressed,
        duration_ms=duration_ms,
        note=f"expired={expired}" + (" force" if force else ""))
    return {"signals_matched": matched, "cases_opened": opened,
            "cases_suppressed": suppressed, "expired": expired, "duration_ms": duration_ms}


def run_patrol_job() -> None:
    """Scheduler 入口(每日)。flag 关 = 静默跳过,永不抛异常。"""
    try:
        result = run_patrol(force=False)
        if result:
            logger.info("[patrol] 巡逻完成: %s", result)
    except Exception as e:  # noqa: BLE001
        logger.warning("[patrol] 巡逻轮异常: %s", e)
