"""
services/marketing/manual_draft.py — 手动起草接真数据(P0-B)+ 五要素质量闸(P0-C)

老板 2026-07-05 拍板背景:此前手动起草产出"约 0 人/预算 0"空壳案被判"太菜"。
本模块让军师从"润色工"变"参谋":
  1. 人群是真的 —— 起草时实时按 cohort SQL 圈人计数(resolve_audience 同源,执行时再圈);
  2. 选择是受限的 —— LLM 只能在 MANUAL_COHORTS 白名单里选人群(禁自由 SQL,数字永远来自 SQL);
  3. 文本是大师驱动的 —— 方案名/站内信文案走 assemble_system_prompt(技能包热加载)+ 出口守卫;
  4. 质量闸 —— 圈选 0 人不建案(宁可无案,不出空壳案),如实返回各人群当前人数。
"""
import hashlib
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from db import marketing_db
from services.marketing import advisor_llm
from services.marketing.skill_packs import assemble_system_prompt

logger = logging.getLogger("GEO-Marketing-ManualDraft")

# 手动起草可选人群白名单(key 必须能被 reach.resolve_audience 解析 —— 建案与执行同一口径)
MANUAL_COHORTS: dict[str, dict] = {
    "trial_exhausted_active": {"label": "体验算力用完、还在活跃的用户", "hook_hint": "首充/权益说明"},
    "no_recharge_streak": {"label": "近 7 天有消费的活跃用户", "hook_hint": "限时加赠/活动通知"},
    "register_no_diagnosis": {"label": "新注册、还没开始用的用户", "hook_hint": "上手引导/首充"},
    "high_value_silent": {"label": "充过值、近 14 天沉默的用户", "hook_hint": "唤回/新功能"},
}

# LLM 挂掉时的关键词兜底(顺序即优先级)
_KEYWORD_MAP: list[tuple[tuple[str, ...], str]] = [
    (("唤回", "沉默", "流失", "老客", "回访"), "high_value_silent"),
    (("注册", "没用", "上手", "激活", "新手"), "register_no_diagnosis"),
    (("首充", "充值", "拉新", "新用户", "体验"), "trial_exhausted_active"),
    (("活跃", "加赠", "复购", "活动", "通知"), "no_recharge_streak"),
]

_COHORT_LIMIT = 500  # 与 resolve_audience 默认一致;达到上限时展示 "500+"


def count_cohorts() -> dict[str, int]:
    """实时圈选各白名单人群人数(SQL 真数,LLM 永不碰这一步)。"""
    from services.marketing.executors.reach import resolve_audience
    counts: dict[str, int] = {}
    for key in MANUAL_COHORTS:
        try:
            uids, _note = resolve_audience({"fingerprint": "", "rule_key": key}, limit=_COHORT_LIMIT)
            counts[key] = len(uids)
        except Exception as e:  # noqa: BLE001
            logger.warning("[manual_draft] 圈选 %s 失败: %s", key, e)
            counts[key] = 0
    return counts


async def pick_cohort(direction: str, counts: dict[str, int], llm=None) -> tuple[str, str]:
    """LLM 受限选择人群(只许选白名单 key)。失败 → 关键词兜底 → 人数最多兜底。

    返回 (cohort_key, pick_source: llm|keyword|fallback_max)。
    """
    llm = llm or advisor_llm._call_llm
    menu = "\n".join(
        f"- {k}: {v['label']}(当前实时圈选 {counts.get(k, 0)} 人;适合:{v['hook_hint']})"
        for k, v in MANUAL_COHORTS.items())
    try:
        content = await llm(
            "你是 OmniRank 营销军师,负责把运营方向落到一个真实可圈选的人群上。只输出 JSON,不解释。",
            f"运营方向:{direction}\n只能从下面的 key 里选一个最贴合方向的人群"
            f"(都不贴合就选人数最多的):\n{menu}\n输出格式:{{\"cohort\": \"<key>\"}}",
        )
        m = re.search(r'"cohort"\s*:\s*"([a-z_]+)"', content or "")
        if m and m.group(1) in MANUAL_COHORTS:
            return m.group(1), "llm"
    except Exception as e:  # noqa: BLE001
        logger.warning("[manual_draft] LLM 选人群失败(走兜底): %s", e)
    for kws, key in _KEYWORD_MAP:
        if any(k in direction for k in kws):
            return key, "keyword"
    best = max(counts, key=lambda k: counts.get(k, 0)) if counts else "trial_exhausted_active"
    return best, "fallback_max"


async def create_manual_draft(direction: str, creator: Optional[int]) -> dict:
    """一句话方向 → 真人群 + 大师文案 → 完整方案进待审。质量闸:0 人不建案。

    纯服务函数(不依赖 Request),端点只做鉴权+方向守卫后委托到这里;测试直测本函数。
    """
    counts = count_cohorts()
    cohort_key, pick_source = await pick_cohort(direction, counts)
    count = counts.get(cohort_key, 0)

    # —— P0-C 质量闸:圈不到人 = 不建案(宁可无案,不出空壳案)——
    if count <= 0:
        return {
            "ok": True, "created": False, "gate": "no_audience",
            "cohort": cohort_key,
            "cohort_counts": {MANUAL_COHORTS[k]["label"]: v for k, v in counts.items()},
            "note": (f"按这个方向圈到的人群「{MANUAL_COHORTS[cohort_key]['label']}」当前 0 人,"
                     "方案没有创建(质量闸:不出空壳案)。可以换个方向,或等信号积累。"),
        }

    label = MANUAL_COHORTS[cohort_key]["label"]
    count_disp = f"{count}{'+' if count >= _COHORT_LIMIT else ''}"

    # —— LLM 起草文本(技能包热加载 · force 无闸 · 出口守卫在 advisor_llm 内)——
    # 铁律:数字永远来自 SQL,LLM 只写方案名和文案。
    sp = assemble_system_prompt(
        awareness_stage="unknown", rule_key="manual_draft",
        task_hint=f"运营方向:{direction};目标人群:{label}(实时圈选 {count_disp} 人)")
    texts = None
    try:
        texts = await advisor_llm.draft_case_texts(direction, label, count_disp, sp)
    except Exception as e:  # noqa: BLE001
        logger.warning("[manual_draft] LLM 起草失败(用确定性版): %s", e)
    copy_source = "ai" if texts else "fallback"
    title = (texts or {}).get("title") or f"运营发起:{direction}"
    station = (texts or {}).get("station") or (
        f"您好!{direction}。详情可在 OmniRank 查看,做成才扣、失败自动退,欢迎随时找我们。")

    fp = hashlib.md5(direction.encode("utf-8")).hexdigest()[:10]
    case_key = f"manual:{fp}:{datetime.now().date().isoformat()}"

    from services.marketing.case_schema import MarketingCaseDraft
    try:
        d = MarketingCaseDraft(
            rule_key="manual_draft", fingerprint=f"manual:{fp}",
            awareness_stage="unknown", owner_scope="platform",
            trigger_reason=title[:80],
            evidence={"source": "manual_draft", "direction": direction,
                      "cohort": cohort_key, "cohort_counts": counts, "pick_source": pick_source,
                      "note": "人数为建案时实时圈选;执行时按同一 cohort 再次实时圈人"},
            audience={"definition": f"{label}(建案时实时圈选 {count_disp} 人)",
                      "count": min(count, _COHORT_LIMIT), "segment": "end"},
            expected_impact=(f"实时圈选 {count_disp} 人;触达后 7/14 日观察相关转化(充值/回访/再用),"
                             "历史基线积累中,无对照组期间不作因果表述"),
            budget_cost={"points": 0, "cost_estimate_yuan": 0.0}, risk_level="low",
            touch_copy={"station": station},
            execution_plan={"type": "touch", "channels": ["station"], "cohort": cohort_key,
                            "note": "执行时按 cohort 实时圈人;默认站内信,渠道在审批页可改"},
            rollback_plan="触达不可撤回,已显式标注;如含活动/发放,可即时下线并冲销未消耗部分",
            observation_window=7)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "created": False, "gate": "schema",
                "note": f"起草未过强校验:{str(e)[:200]}"}

    row, created = marketing_db.create_case(
        **d.to_db_kwargs(case_key=case_key, created_by=creator,
                         expires_at=datetime.now() + timedelta(hours=72)))
    if created and row:
        marketing_db.backfill_case_no(row["id"])
        marketing_db.add_event(
            event_type="suggestion_generated", case_id=row["id"], actor_id=creator,
            severity="info",
            message=f"运营发起:{direction[:60]} → 人群「{label}」{count_disp} 人(选择来源:{pick_source})")
    return {"ok": True, "case": row, "created": created, "copy_source": copy_source,
            "cohort": cohort_key, "audience_count": count,
            "note": None if created else "同一方向今天已建过,请在队列中查看"}
