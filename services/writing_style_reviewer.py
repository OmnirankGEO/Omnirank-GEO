"""AI quality review layer for shadow candidates.

- 接线 `writing.geo_triforce_reviewer` 的 100 分 4 维量表(AI可信35/平台审核25/品牌植入25/用户价值15)
  首次投入使用,4 维等比压缩至 70 分。
- Historical adoption-fit is retained as a response field for API compatibility
  but has zero weight until JC5 direct article-question lineage matures.
- 双盲对比:两篇样文随机命名文章甲/乙,不告诉模型哪篇是当前/候选;输出各维分 + 更优篇 + 人话差异。
- 防自嗨:同一对比跑 2 个不同评审模型,双模型一致才给 replace 建议;不一致/单模型 → observe。
- 结果写 simulation.review_summary + 同步版本面 draft 的 judge_summary。

评审是内部后台动作;模型名不外泄客户(看板只显示「评审 A/B · 双评审一致」)。
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import threading
from typing import Any

import httpx

from db.writing_style_simulation_db import get_simulation, update_simulation_review
from services.research_monitor.article_intent_classifier import _extract_json_object
from tools.llm_call_tracker import llm_track, usage_from_response_payload
from writing.flywheel_llm import guard_output  # [V2 review fix] 评语过守卫(禁外部厂商自指/承诺话术)
from writing.geo_triforce_reviewer import DIMENSION_WEIGHTS, SCORE_THRESHOLDS
from writing.llm_utils import API_URLS, ENV_KEY_NAMES
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH

logger = logging.getLogger("GEO-WritingFlywheel.Reviewer")

REVIEW_ARTICLE_MAX_CHARS = 8000
# Quality and outcome are different scores. No historical structure can add a
# citation bonus before the strict outcome/data-readiness gates pass.
_ADOPTION_WEIGHT = 0.0
_BASE_WEIGHT = 1.0

# [出口审核修] per-simulation 在途守卫:防同一对比被并发重复评审(浪费 LLM + last-writer-wins)。
# threading.Lock 跨事件循环安全(评审经端点 create_task 触发,但此守卫不依赖单一 loop)。
_REVIEW_INFLIGHT: set[int] = set()
_REVIEW_INFLIGHT_LOCK = threading.Lock()


def is_review_running(simulation_id: int) -> bool:
    with _REVIEW_INFLIGHT_LOCK:
        return int(simulation_id) in _REVIEW_INFLIGHT


def _weighted_total(scores: dict[str, Any]) -> float:
    """5 维 0-100 分 → 加权 0-100 总分(权重口径 SSOT 在代码,模型只打分)。"""
    def g(k: str) -> float:
        try:
            return max(0.0, min(100.0, float(scores.get(k) or 0)))
        except (TypeError, ValueError):
            return 0.0
    base = (
        DIMENSION_WEIGHTS["ai_trust"] / 100 * g("ai_trust")
        + DIMENSION_WEIGHTS["platform_compliance"] / 100 * g("platform_compliance")
        + DIMENSION_WEIGHTS["brand_value"] / 100 * g("brand_value")
        + DIMENSION_WEIGHTS["user_value"] / 100 * g("user_value")
    )  # 0-100
    return round(_BASE_WEIGHT * base + _ADOPTION_WEIGHT * g("adoption_fit"), 1)


def _norm_scores(raw: dict[str, Any]) -> dict[str, float]:
    keys = ["ai_trust", "platform_compliance", "brand_value", "user_value", "adoption_fit"]
    out = {}
    for k in keys:
        try:
            out[k] = max(0.0, min(100.0, float(raw.get(k) or 0)))
        except (TypeError, ValueError):
            out[k] = 0.0
    return out


def _render_adoption_rubric(holdout_lift: list[dict[str, Any]]) -> str:
    return "结果维度当前不可用；不得根据历史结构预测 AI 引用或采纳。"


def compute_holdout_lift(industry_key: str, style_code: str) -> list[dict[str, Any]]:
    """Return only JC5 cluster-split descriptive lift; never a review bonus."""
    from services.article_structure_analysis import _feature_lift, _feature_share
    from services.writing_answer_distiller import plan_distill_groups

    plan = plan_distill_groups(industry_key, limit=1000)
    for g in plan["groups"]:
        if g["style_code"] == style_code:
            rows = g["_rows"]
            return _feature_lift(
                {"feature_share": _feature_share(rows["holdout_adopted"])},
                {"feature_share": _feature_share(rows["holdout_control"])},
            )
    return []


def _resolve_reviewers() -> list[dict[str, str]]:
    """两个不同评审模型(deepseek + qwen)。缺 key 的跳过;<2 个 → 单模型(永不 replace)。"""
    preferred = [
        # 🔴 [WO_206 c1b② · P1] 原来的默认值是 `deepseek-v3.2` —— 那是**百炼时代**的名字,
        #    官方线从来没有过。Deploy 206-d2 实打:它在官方线上现在**返 400**。
        #    而 `_resolve_reviewers` 只跳过「缺 key」的,名字错要到**调用时**才失败 ⇒
        #    评审A 恒失败 ⇒ 恒单评审 ⇒ 下面那段判定只会返「建议继续观察」,
        #    **replace 这条路从来没通过**(REVIEW_MODEL_A 仓里哪儿都没配)。
        {"label": "评审A", "provider": "deepseek",
         "model": os.getenv("REVIEW_MODEL_A", DEEPSEEK_OFFICIAL_FLASH)},
        {"label": "评审B", "provider": "dashscope", "model": os.getenv("REVIEW_MODEL_B", "qwen-plus")},
    ]
    out: list[dict[str, str]] = []
    for c in preferred:
        key = os.getenv(ENV_KEY_NAMES.get(c["provider"], ""), "").strip()
        if key:
            out.append({**c, "url": API_URLS[c["provider"]], "key": key})
    return out


def _build_review_messages(art_jia: str, art_yi: str, adoption_rubric: str) -> list[dict[str, str]]:
    system = (
        "你是 GEO 内容质量评审员。用同一套发布前质量量表给两篇文章各自打分(每维 0-100),量表:\n"
        "1. ai_trust(可信与可核验性):E-E-A-T、证据边界、结构清晰、意图覆盖。不得预测引用。\n"
        "2. platform_compliance(平台审核):硬广规避、软广风险、合规、标题党规避。\n"
        "3. brand_value(品牌植入有效性):品牌必要性、记忆点、行动引导。\n"
        "4. user_value(用户价值与可读性):实用价值、阅读体验、情感共鸣。\n"
        "5. adoption_fit:固定返回 0；结果数据尚未成熟，不参与总分。\n"
        "这是双盲评审:不告诉你哪篇是旧版/新版。客观打分。只返回 JSON。\n"
        "除打分外,再给「更优的那篇」的结构化评语(优点/风险/一句话建议),只描述其结构与内容特征,"
        "绝不提甲/乙、不猜哪篇是新旧版;一句话建议只总结其价值方向,不替代系统的替换判定。"
        "禁止提及任何 AI 模型或服务商名称(不得说像 GPT/ChatGPT/Claude 等,也不得自我指涉为某模型);"
        "禁止承诺、保证、100% 被引/上榜类话术。"
    )
    user = f"""【发布后结果维度】
{adoption_rubric}

【文章甲】
{art_jia[:REVIEW_ARTICLE_MAX_CHARS]}

【文章乙】
{art_yi[:REVIEW_ARTICLE_MAX_CHARS]}

请返回 JSON,严格如下:
{{
  "jia": {{"ai_trust":0-100,"platform_compliance":0-100,"brand_value":0-100,"user_value":0-100,"adoption_fit":0}},
  "yi": {{"ai_trust":0-100,"platform_compliance":0-100,"brand_value":0-100,"user_value":0-100,"adoption_fit":0}},
  "better": "jia|yi|tie",
  "diff_summary": ["更优的那篇相对另一篇的优势点,人话,不超过 3 条,不要提甲/乙,只描述结构/内容特征"],
  "strengths": ["更优的那篇的核心优点,≤3 条,人话,不提甲/乙"],
  "risks": ["更优的那篇仍存在的风险或待改进点,≤3 条,不提甲/乙"],
  "one_line_advice": "给运营的一句话结论:总结更优篇的价值方向,不下最终替换决定"
}}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def _call_review_llm(messages: list[dict[str, str]], reviewer: dict[str, str]) -> str:
    payload = {
        "model": reviewer["model"],
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {reviewer['key']}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=120.0) as client:
        async with llm_track(
            "writing_flywheel", reviewer["provider"], model=reviewer["model"],
            metadata={"task": "style_review"},
        ) as tracker:
            try:
                resp = await client.post(reviewer["url"], headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                it, ot, ct = usage_from_response_payload(data)
                tracker.record(input_tokens=it, output_tokens=ot, cached_tokens=ct, success=True)
            except Exception as exc:
                tracker.record(success=False, error_msg=str(exc)[:500])
                raise
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "")


def _aggregate(results: list[dict[str, Any]], reviewer_count: int) -> tuple[str, str]:
    """双模型一致才 replace/keep;不一致或单模型 → observe(永不单模型 replace,防自嗨)。"""
    ok = [r for r in results if "error" not in r]
    if not ok:
        return "observe", "评审全部失败,无法给出建议"
    sides = [r["better_side"] for r in ok]
    dual = len(ok) >= 2
    if dual and all(s == "candidate" for s in sides):
        return "replace", "双评审一致:候选版更优"
    if dual and all(s == "current" for s in sides):
        return "keep", "双评审一致:当前版更优,候选未超越"
    if not dual:
        s = sides[0]
        if s == "current":
            return "keep", "单评审:当前版不差(缺第二评审,不建议替换)"
        return "observe", "单评审(缺第二模型确认),建议继续观察,不贸然替换"
    return "observe", "双评审意见不一致,建议继续观察"


def _de_blind_result(parsed: dict[str, Any], jia_is: str) -> dict[str, Any]:
    jia = _norm_scores(parsed.get("jia") or {})
    yi = _norm_scores(parsed.get("yi") or {})
    yi_is = "candidate" if jia_is == "current" else "current"
    cur = jia if jia_is == "current" else yi
    cand = jia if jia_is == "candidate" else yi
    better_blind = str(parsed.get("better") or "tie").strip().lower()
    if better_blind == "jia":
        better_side = jia_is
    elif better_blind == "yi":
        better_side = yi_is
    else:
        better_side = "tie"
    return {
        "current_score": _weighted_total(cur),
        "candidate_score": _weighted_total(cand),
        "current_dims": cur,
        "candidate_dims": cand,
        "better_side": better_side,
        # [V2] LLM 产的结构化评语 —— 与 diff_summary 同类(描述更优篇特征,非甲/乙标签),去盲后原样透传。
        # [review fix] 逐条过守卫:命中外部厂商自指/承诺话术的条目丢弃(评审是唯一未受守卫的解释层,补齐)。
        "diff_summary": _clean_commentary_list(parsed.get("diff_summary")),
        "strengths": _clean_commentary_list(parsed.get("strengths")),
        "risks": _clean_commentary_list(parsed.get("risks")),
        "one_line_advice": _clean_commentary_text(parsed.get("one_line_advice")),
    }


def _clean_commentary_list(items: Any) -> list[str]:
    """去空 + 过守卫(命中厂商/承诺话术的条目丢弃)+ 截前 3 条。"""
    out: list[str] = []
    for x in (items or []):
        s = str(x or "").strip()
        if s and not guard_output(s)[1]:
            out.append(s)
    return out[:3]


def _clean_commentary_text(value: Any) -> str:
    """一句话建议:命中守卫 → 置空(由聚合层 fail-soft 回退 reason)。"""
    s = str(value or "").strip()
    return "" if (not s or guard_output(s)[1]) else s


def _score_band(score: float) -> str:
    if score >= SCORE_THRESHOLDS["excellent"]:
        return "excellent"
    if score >= SCORE_THRESHOLDS["good"]:
        return "good"
    if score >= SCORE_THRESHOLDS["needs_work"]:
        return "needs_work"
    return "fail"


async def review_simulation(
    simulation_id: int,
    *,
    dry_run: bool = True,
    seed: int | None = None,
    sync_judge: bool = True,
) -> dict[str, Any]:
    """对一次模拟对比做双盲双模型评审。dry_run:检查可评审性 + 评审器数量(零 LLM)。"""
    row = get_simulation(simulation_id)
    if not row:
        raise ValueError(f"simulation_not_found: {simulation_id}")
    reviewers = _resolve_reviewers()

    if dry_run:
        return {
            "mode": "dry_run",
            "simulation_id": simulation_id,
            "reviewer_count": len(reviewers),
            "dual_model_available": len(reviewers) >= 2,
            "note": (
                "≥2 个评审模型可用 → 一致才给替换建议;<2 个 → 只能观察不替换(防自嗨)。"
                if len(reviewers) < 2 else "双模型可用,可给出替换/保持/观察建议。"
            ),
        }

    if not reviewers:
        raise RuntimeError("无可用评审模型(缺 DEEPSEEK_API_KEY / DASHSCOPE_API_KEY)")

    sid = int(simulation_id)
    with _REVIEW_INFLIGHT_LOCK:
        if sid in _REVIEW_INFLIGHT:
            return {"status": "in_progress", "simulation_id": sid, "message": "该对比正在评审中,请稍后再试。"}
        _REVIEW_INFLIGHT.add(sid)
    try:
        # [review fix] holdout lift = plan_distill_groups(limit=1000,全量 OSS 正文回读)是同步重活:
        # 挪线程池,不堵 uvicorn 唯一事件循环(生产 WORKERS=1,堵住 = 全站冻结)。
        holdout_lift = await asyncio.to_thread(
            compute_holdout_lift, row.get("industry_key") or "general", row.get("style_code") or ""
        )
        adoption_rubric = _render_adoption_rubric(holdout_lift)

        # 双盲:随机决定 文章甲 是 current 还是 candidate
        rnd = random.Random(seed)
        swap = rnd.random() < 0.5
        art_current = str(row.get("current_article") or "")
        art_candidate = str(row.get("candidate_article") or "")
        # [GEO-R10-CAN-019] 长文尾部截断守卫:_build_review_messages 只喂两篇各前
        # REVIEW_ARTICLE_MAX_CHARS 字,超长文章的尾部对两个模型都不可见。若候选版的
        # 劣化/违规/降质内容恰在未见尾部,双模型仍可能给 replace → 整版替换上线错误内容。
        # 记录覆盖率证据(随判定落库),并在下方对超长样本禁用「替换」判定(降级 observe)。
        current_chars = len(art_current)
        candidate_chars = len(art_candidate)
        tail_truncated = (
            current_chars > REVIEW_ARTICLE_MAX_CHARS
            or candidate_chars > REVIEW_ARTICLE_MAX_CHARS
        )
        if swap:
            art_jia, art_yi, jia_is = art_candidate, art_current, "candidate"
        else:
            art_jia, art_yi, jia_is = art_current, art_candidate, "current"
        messages = _build_review_messages(art_jia, art_yi, adoption_rubric)

        results: list[dict[str, Any]] = []
        for rv in reviewers:
            try:
                raw = await _call_review_llm(messages, rv)
                parsed = _extract_json_object(raw)
                deb = _de_blind_result(parsed, jia_is)
                results.append({"label": rv["label"], **deb})
            except Exception as exc:
                logger.warning("[review] %s 评审失败: %s", rv.get("label"), str(exc)[:200])
                results.append({"label": rv.get("label"), "error": str(exc)[:200]})

        verdict, reason = _aggregate(results, len(reviewers))
        ok = [r for r in results if "error" not in r]
        if not ok:
            # [review fix] 双评审全失败:不写 review 结果(卡片保持「待评审」可重试),
            # 否则 0.0 分被标 status='reviewed',看板把失败伪装成已评审。
            logger.warning("[review] 全部评审模型调用失败 sid=%s,不落 review 结果", sid)
            return {"status": "review_failed", "simulation_id": simulation_id,
                    "message": "全部评审模型调用失败,未写入评审结果,可稍后重试",
                    "errors": [str(r.get("error") or "")[:120] for r in results]}
        # [review fix] 受污染样本对(两臂 user_message 不一致)不给替换建议:强制 observe,防脏对比误导上线
        polluted_pair = row.get("user_message_identical") is False
        if polluted_pair and verdict == "replace":
            verdict, reason = "observe", f"样本受污染(两臂输入不一致),不作替换建议;原判定:{reason}"
        # [GEO-R10-CAN-019] 尾部截断的整版判定不可信:评审只看了前 8000 字,
        # 尾部差异未被看见 → 不给「替换」建议(降级 observe),防未评审内容随整版上线。
        if tail_truncated and verdict == "replace":
            verdict, reason = "observe", (
                f"长文尾部超 {REVIEW_ARTICLE_MAX_CHARS} 字未纳入评审"
                f"(当前 {current_chars} 字 / 候选 {candidate_chars} 字),"
                f"不作整版替换建议(尾部差异未被看见);原判定:{reason}"
            )
        avg_current = round(sum(r["current_score"] for r in ok) / len(ok), 1) if ok else 0.0
        avg_candidate = round(sum(r["candidate_score"] for r in ok) / len(ok), 1) if ok else 0.0
        # 差异摘要取偏向候选的那份(或第一份);[V2] 结构化评语取同一份评审,保持口径一致
        picked = next((r for r in ok if r["better_side"] == "candidate"), None) or ok[0]
        diff = picked["diff_summary"]
        # [review fix · C1] 评语描述的是「更优的那篇」:keep 判定下 picked.better_side=='current',
        # 评语实为当前(旧)版优点 → 前端据此渲染「当前版优势」而非硬编码「候选版优势」,防误导决策。
        commentary_side = picked.get("better_side") or "tie"
        # [V2] fail-soft:LLM 漏返结构化字段时回退——strengths 空回退 diff_summary(看板永不空白),
        # one_line_advice 空回退代码推导的 reason。绝不在全失败路径落假评语(已被上方 not ok 守卫拦截)。
        strengths = list(picked.get("strengths") or [])
        risks = list(picked.get("risks") or [])
        one_line_advice = str(picked.get("one_line_advice") or "").strip()
        if not strengths:
            strengths = list(diff or [])
        if not one_line_advice:
            one_line_advice = reason

        review_summary = {
            "verdict": verdict,            # replace / keep / observe
            "reason": reason,
            "avg_current_score": avg_current,
            "avg_candidate_score": avg_candidate,
            "score_delta": round(avg_candidate - avg_current, 1),
            "candidate_band": _score_band(avg_candidate),
            "dual_model": len(ok) >= 2,
            "reviewer_agreement": len({r["better_side"] for r in ok}) == 1 if ok else False,
            "diff_summary": diff or [],
            "strengths": strengths,          # [V2] 更优篇优点(LLM,fail-soft 回退 diff)
            "risks": risks,                  # [V2] 更优篇风险/待改进(LLM,缺则空)
            "one_line_advice": one_line_advice,  # [V2] 一句话建议(LLM,fail-soft 回退 reason)
            "commentary_side": commentary_side,  # [C1] 评语归属:current/candidate/tie(前端据此定标题)
            "reviewers": [{"label": r.get("label"), "ok": "error" not in r} for r in results],  # 不外泄模型名
            "holdout_lift_used": bool(holdout_lift),
            "blind_swap": swap,
            "polluted_pair": polluted_pair,
            # [GEO-R10-CAN-019] 覆盖率/证据溯源:随判定持久化,让看板明示评审是否只看了截断前缀。
            "tail_truncated": tail_truncated,
            "reviewed_max_chars": REVIEW_ARTICLE_MAX_CHARS,
            "current_chars": current_chars,
            "candidate_chars": candidate_chars,
        }
        update_simulation_review(simulation_id, review_summary)
        # [V7] 评审回填改动 sim 的 review_summary → 看板卡态变 → 失效看板/总汇总缓存(后台协程收尾处,fail-soft)
        try:
            from writing.flywheel_cache import SCOPE_BOARD, SCOPE_INSIGHT, invalidate
            invalidate([SCOPE_BOARD, SCOPE_INSIGHT])
        except Exception:
            pass

        if sync_judge and row.get("version_id"):
            try:
                from writing.style_control import set_version_judge_summary
                set_version_judge_summary(
                    row["version_id"],
                    {
                        "semantic_decision": verdict,
                        "provider_count": len(ok),
                        "avg_current_score": avg_current,
                        "avg_candidate_score": avg_candidate,
                        "note": reason,
                    },
                )
            except Exception as exc:
                logger.warning("[review] 同步 judge_summary 失败: %s", str(exc)[:200])

        return {"status": "completed", "simulation_id": simulation_id, **review_summary}
    finally:
        with _REVIEW_INFLIGHT_LOCK:
            _REVIEW_INFLIGHT.discard(sid)
