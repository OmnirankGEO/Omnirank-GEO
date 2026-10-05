"""W3 · 模拟对比层(老板点名:原文 vs 修改后)。

对指定 style_code + 行业 + 代表性题目,生成两篇样文:
  - 当前版 = 生产现行 prompt(get_prompt_for_style)
  - 候选版 = 草稿候选 prompt(同题目、同品牌上下文、同温度、同 max_tokens)

🔴 生产同源(Codex 审核补强):模拟走与生产 `ArticleGeneratorService._generate_single` **同一套**
prompt/input 组装(含白标/知识库/竞品/结构注入),不是裸 `_call_llm` 干净实验。做法:调真生产方法
并传 `sim_overrides`,仅覆盖 base_prompt(候选)+ 钉住两个随机/有状态 helper(dynamic_scores /
case_industry),使**两臂只差 base_prompt**;两臂 user_message 逐字节一致(运行时记录 + 单测断言)。

🔴 控制面固定:结构参考(user prompt 附加)两臂状态一致(同为当前 active 或同为关闭),写进记录。

🔴 shadow 纪律:样文存 `writing_style_simulations`,**绝不进 articles 主表、绝不进客户面**。
成本三闸:dry_run 默认(计数 + 预估)/ 真跑后台 + 互斥。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from db.writing_style_simulation_db import get_demo_quote, insert_simulation

logger = logging.getLogger("GEO-WritingFlywheel.Simulation")

_SIM_LOCK = asyncio.Lock()
_RUNNING_STYLE: dict[str, str | None] = {"style_code": None}  # 当前正在模拟的文体(供看板 generating 态)


def get_running_simulation_style() -> str | None:
    return _RUNNING_STYLE.get("style_code")


def _record_failed_simulation(
    *,
    style_code: str,
    industry_key: str,
    version_id: str | None,
    topic_title: str,
    error: str,
    actor_id: int = 0,
) -> None:
    """[review fix] 真跑失败落一条 failed 行(绝不抛错)。

    真跑走 API 后台 fire-and-forget,异常否则无痕:看板 failed 态(「生成失败,可重试」)
    代码上不可达,运营看到「已在后台生成」后永远等不到结果也看不到错误。"""
    try:
        insert_simulation(
            style_code=style_code or "unknown",
            industry_key=industry_key or "general",
            version_id=version_id,
            topic_title=topic_title or "",
            demo_quote_id=None,
            demo_brand_name="",
            current_article="",
            candidate_article="",
            structure_guidance_state="",
            user_message_identical=None,
            cost_note="",
            status="failed",
            error=str(error or "")[:300],
            created_by=actor_id,
        )
        _invalidate_sim_caches()  # [V7] failed 行也改看板 → 失效缓存(预备失败路径不经 run 的 finally)
    except Exception as rec_exc:
        logger.warning("[simulation] failed 行落库失败(放弃记录): %s", str(rec_exc)[:200])

# 两篇文章生成:每篇约 input 4000 + output 8000 tokens(写作模型),保守估
_SIM_INPUT_TOKENS = 4000
_SIM_OUTPUT_TOKENS = 8000

_STYLE_DEMO_TITLE = {
    "ranking_v2": "{industry}十大品牌推荐榜单",
    "authority_ranking": "{industry}权威机构榜单",
    "recommendation_review": "{industry}优质品牌盘点推荐",
    "comparison_review": "{industry}主流方案对比测评",
    "buying_guide": "{industry}选购指南与避坑要点",
    "qa_recommendation": "{industry}常见问题与选择建议",
    "data_report": "{industry}行业数据与趋势报告",
    "risk_compliance": "{industry}合规风控要点解读",
    "brand_softarticle": "{industry}品牌案例深度报道",
    "price_roi": "{industry}价格与投入产出分析",
    "trojan_horse": "{industry}趋势洞察",
    "company_profile": "{industry}企业深度报道",
}


def _load_candidate(version_id: str) -> tuple[str, str]:
    """从版本面取候选 prompt_text + style_code(草稿态)。"""
    from writing.style_control import load_control_state

    state = load_control_state()
    for v in state.get("versions") or []:
        if v.get("version_id") == version_id:
            return str(v.get("prompt_text") or ""), str(v.get("style_code") or "")
    raise ValueError(f"version_not_found: {version_id}")


def _pick_demo_title(quote: dict[str, Any], style_code: str) -> str:
    industry = str(quote.get("industry") or "行业").strip() or "行业"
    tmpl = _STYLE_DEMO_TITLE.get(style_code, "{industry}推荐榜单")
    return tmpl.format(industry=industry)


def _resolve_structure_guidance(quote: dict[str, Any], title: str) -> tuple[str, str]:
    """结构参考状态(两臂共用同一份)。返回 (instruction, state)。"""
    from writing.feature_switches import is_feature_enabled

    if not is_feature_enabled("structure_guidance"):
        return "", "off_flag"
    try:
        from services.writing_structure_guidance import (
            build_bounded_structure_instruction,
            build_structure_guidance_for_quote,
        )
        payload = build_structure_guidance_for_quote(
            {"id": quote.get("quote_id"), "industry": quote.get("industry") or ""}
        )
        instr = build_bounded_structure_instruction(payload, title=title, user_choice="auto")
        return instr, ("on" if instr else "off_no_guidance")
    except Exception as exc:  # 结构参考失败不阻断模拟,如实记录状态
        logger.warning("[simulation] 结构参考解析失败: %s", str(exc)[:200])
        return "", "off_error"


def _compute_structure_diff(
    title: str, art_current: dict[str, Any], art_candidate: dict[str, Any]
) -> dict[str, Any]:
    """[V1] 两篇样文各跑 21 布尔特征(BOOLEAN_FEATURES),给出候选版相对当前版的结构 diff。

    纯函数(正则/字符串,零 LLM 零成本);只遍历 BOOLEAN_FEATURES 的 21 布尔键
    (extract 返回 ~34 混合键,遍历全部会把整数/字符串当布尔 diff 出乱码徽章)。
    失败 fail-soft 返回 {}(绝不阻断模拟落库)。"""
    try:
        from services.article_structure_analysis import BOOLEAN_FEATURES
        from services.article_structure_features import extract_article_structure_features

        feat_cur = extract_article_structure_features(
            {"title": title, "content": str(art_current.get("content") or "")}
        )
        feat_cand = extract_article_structure_features(
            {"title": title, "content": str(art_candidate.get("content") or "")}
        )
        added: list[str] = []
        missing: list[str] = []
        for key, label in BOOLEAN_FEATURES.items():
            cur_on = bool(feat_cur.get(key))
            cand_on = bool(feat_cand.get(key))
            if cand_on and not cur_on:
                added.append(label)
            elif cur_on and not cand_on:
                missing.append(label)
        return {"added": added, "missing": missing}
    except Exception as exc:  # 结构 diff 是锦上添花,失败绝不阻断模拟
        logger.warning("[simulation] 结构 diff 计算失败(忽略): %s", str(exc)[:200])
        return {}


def _invalidate_sim_caches() -> None:
    """[V7] 模拟真跑(成功/失败/预备失败)改动 sim 表 → 失效看板/总汇总缓存。fail-soft,绝不破坏写路径。"""
    try:
        from writing.flywheel_cache import SCOPE_BOARD, SCOPE_INSIGHT, invalidate

        invalidate([SCOPE_BOARD, SCOPE_INSIGHT])
    except Exception:
        pass


def estimate_simulation_cost() -> dict[str, Any]:
    """一次模拟 = 2 篇文章生成。"""
    return {
        "generations": 2,
        "est_input_tokens": 2 * _SIM_INPUT_TOKENS,
        "est_output_tokens": 2 * _SIM_OUTPUT_TOKENS,
        "est_cost_cny": round(2 * (_SIM_INPUT_TOKENS / 1000 * 0.002 + _SIM_OUTPUT_TOKENS / 1000 * 0.008), 4),
    }


def is_style_simulation_running() -> bool:
    return _SIM_LOCK.locked()


async def run_style_simulation(
    *,
    style_code: str = "",
    industry_key: str = "general",
    version_id: str | None = None,
    candidate_prompt: str | None = None,
    demo_quote_id: int | None = None,
    title: str = "",
    dry_run: bool = True,
    actor_id: int = 0,
) -> dict[str, Any]:
    """生产同源模拟对比。dry_run:解析演示上下文 + 成本预估(零 LLM)。真跑:后台 + 互斥,两臂生成→存 shadow。"""
    resolved_style = style_code
    try:
        if candidate_prompt is None and version_id:
            candidate_prompt, resolved_style = _load_candidate(version_id)
            resolved_style = style_code or resolved_style
        candidate_prompt = str(candidate_prompt or "").strip()
        if not candidate_prompt:
            raise ValueError("需要 version_id(草稿) 或 candidate_prompt")
        if not resolved_style:
            raise ValueError("需要 style_code")

        quote = get_demo_quote(industry_key, demo_quote_id)
        if not quote:
            raise ValueError(f"未找到行业「{industry_key}」的演示 quote,请指定 demo_quote_id")
        demo_industry = str(quote.get("industry") or industry_key)
        demo_title = (title or "").strip() or _pick_demo_title(quote, resolved_style)
        structure_instruction, structure_state = _resolve_structure_guidance(quote, demo_title)
    except Exception as exc:
        # [review fix] 真跑在后台 fire-and-forget:准备阶段失败也要落 failed 行,否则无痕(dry_run 直接抛给调用方)
        if not dry_run:
            _record_failed_simulation(
                style_code=resolved_style or style_code, industry_key=industry_key,
                version_id=version_id, topic_title=title, error=str(exc), actor_id=actor_id,
            )
        raise

    if dry_run:
        return {
            "mode": "dry_run",
            "style_code": resolved_style,
            "industry_key": industry_key,
            "version_id": version_id,
            "demo_quote_id": quote.get("quote_id"),
            "demo_brand_name": quote.get("brand_name"),
            "topic_title": demo_title,
            "structure_guidance_state": structure_state,
            **estimate_simulation_cost(),
            "note": "真跑将生成 2 篇样文(当前版/候选版),仅存内部对比表,不进客户面。",
        }

    if _SIM_LOCK.locked():
        return {"status": "in_progress", "message": "模拟正在进行中,请稍后再试。"}

    async with _SIM_LOCK:
        _RUNNING_STYLE["style_code"] = resolved_style  # 看板据此显示该文体「生成中」
        try:
            from writing.article_generator_service import ArticleGeneratorService
            from writing.llm_utils import get_llm_config

            api_url, api_key, model, _provider = get_llm_config("article_writing", "writing")
            if not api_key:
                raise RuntimeError("写作 LLM 未配置 api_key")

            svc = ArticleGeneratorService(
                quote.get("quote_id"), quote.get("brand_name") or "演示品牌", demo_industry
            )
            # 钉住两个随机/有状态 helper —— 两臂只差 base_prompt
            try:
                from writing.ranking_prompt_v9 import generate_dynamic_scores
                pinned_scores = generate_dynamic_scores()
            except Exception:
                pinned_scores = {}
            pinned_case = svc._get_next_case_industry()

            topic = {
                "id": None,
                "title": demo_title,
                "style_code": resolved_style,
                "user_choice": "auto",
                "_trust_legacy_style": True,
                "structure_guidance_instruction": structure_instruction,
            }
            base_sim = {"dynamic_scores": pinned_scores, "case_industry": pinned_case}

            # 臂 A · 当前版(base_prompt 缺省 → 生产现行 prompt)
            sim_a = dict(base_sim)
            art_a = await svc._generate_single(topic, api_url, api_key, model, sim_overrides=sim_a)
            # 臂 B · 候选版(base_prompt 覆盖)
            sim_b = dict(base_sim)
            sim_b["base_prompt"] = candidate_prompt
            art_b = await svc._generate_single(topic, api_url, api_key, model, sim_overrides=sim_b)

            um_identical = sim_a.get("_captured_user_message") == sim_b.get("_captured_user_message")
            if not um_identical:
                logger.warning(
                    "[simulation] 两臂 user_message 不一致(style=%s industry=%s)—— 需排查非 base_prompt 差异源",
                    resolved_style, industry_key,
                )
            cost = estimate_simulation_cost()
            structure_diff = _compute_structure_diff(demo_title, art_a, art_b)  # [V1] 纯函数,零成本
            sid = insert_simulation(
                style_code=resolved_style,
                industry_key=industry_key,
                version_id=version_id,
                topic_title=demo_title,
                demo_quote_id=quote.get("quote_id"),
                demo_brand_name=quote.get("brand_name") or "",
                current_article=str(art_a.get("content") or ""),
                candidate_article=str(art_b.get("content") or ""),
                structure_guidance_state=structure_state,
                user_message_identical=um_identical,
                cost_note=f"2 篇文章生成 · 预估 ¥{cost['est_cost_cny']}(平台承担)",
                status="generated",
                created_by=actor_id,
                structure_diff=structure_diff,
            )
            logger.info(
                "[simulation] 完成 sid=%s style=%s um_identical=%s", sid, resolved_style, um_identical
            )
            return {
                "status": "completed",
                "simulation_id": sid,
                "style_code": resolved_style,
                "user_message_identical": um_identical,
                "current_word_count": len(str(art_a.get("content") or "")),
                "candidate_word_count": len(str(art_b.get("content") or "")),
                "structure_guidance_state": structure_state,
            }
        except Exception as exc:
            logger.warning("[simulation] 模拟失败: %s", str(exc)[:300])
            # [review fix] 落 failed 行:看板 failed 态可达 + 运营可见错误(臂A成功臂B失败时钱已花,更要可见)
            _record_failed_simulation(
                style_code=resolved_style, industry_key=industry_key,
                version_id=version_id, topic_title=demo_title, error=str(exc), actor_id=actor_id,
            )
            return {"status": "failed", "error": str(exc)[:300]}
        finally:
            _RUNNING_STYLE["style_code"] = None
            _invalidate_sim_caches()  # [V7] 真跑改动 sim 表 → 失效看板缓存(在后台协程收尾,非 handler return)
