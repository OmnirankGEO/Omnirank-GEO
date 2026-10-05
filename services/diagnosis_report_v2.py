"""
diagnosis_report_v2 — M2 报告 2.0 · 诊断报告主链入口

CTO-B 2026-04-26 W2 · feat/m2-diagnosis-report-v2-full

定位:
  · 老板硬要求 A:诊断主链必须真正走 v2(W1 SSOT + report_writer_v2 8 模块)
  · v1 generate_enhanced_report 仅作为 v2 组装异常时的紧急 fallback
  · 决策点 5:v2 缺数据时仍输出 v2 + 模块明确标注 "数据不足"
  · 决策点 2:同时产出 internal + client 两版 markdown · 落库 diagnosis_records 新列

数据契约(diagnosis_workflow.py 调用):
  diagnosis_results: dict 含
    - data: { ai_visibility, web_search, asr_transcripts, business_context, ... }
    - scores: { ... 5 dim }
    - score_data: { dimension_scores, total_score, level, ... }
    - keywords: list[str] | str
    - report_text(已生成的) · 不需要 · 本模块从零装配
  brand: brands 表行 dict
  profile: client_profiles 表行 dict(可空)
  quote: 最近一次报价 dict(可空)
  brand_id: int(必传 · evidence + competitor 抽取依赖)

返回:
  {
    "version": "v2",
    "internal": { "full_markdown", "modules", "evidence_count", "lint_warnings" },
    "client":   { "full_markdown", "modules", "evidence_count", "lint_warnings" },
    "completeness": { score, level, groups, impact_notes },
    "error": None | "<错误描述>",  # 决策点 5:异常时记录,前端 banner "报告生成异常"
    "generated_at": ISO 时间
  }
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from services.brand_latest_ssot import sync_brand_latest

logger = logging.getLogger("GEO-DiagnosisReportV2")


def _shape_report_data(
    diagnosis_results: dict,
    score_data: dict,
    brand: dict,
    profile: dict,
    completeness: dict,
) -> dict[str, Any]:
    """把 diagnosis_workflow 产出和 brand/profile 拼成 report_writer_v2 期望的 report_data

    CTO-B 2026-04-26 quality fix · P0 评分口径冲突根治:
      老诊断 raw_data_json.scores 是 8 维老制(ai_engine_score/social_media_score/...)
        各维度 max 25/20/18/15/12/5/5 = 100 · DB total_score 是这 7 个 sum
      v2 是 5 维 SSOT 100 制(ai_recommendation/web_content/authority/structured/brand_foundation)
        各维度 max 30/25/20/15/10 = 100
      旧 7 维 keys 不能直接 map 到新 5 维(ai_engine_score 不等于 ai_recommendation_score 等)·
      互相 map 必出现"封面 8/100 vs 评分总览 1/100"双重计分 bug。

    解决:
      · 优先使用调用方显式传入的 score_data.dimension_scores(新诊断管线写入)
      · 如果没有 · 用 raw_data 的 ai_visibility + web_search 重新调用 calculate_geo_scope_score()
        重算 5 维 SSOT(W1 已修 result_count fallback bug)
      · 用重算后的 total_score 作为报告所有"总分"展示的 SSOT
        DB old total_score 仅作为"诊断历史快照"参考 · 不再混用
    """
    # 5 维度 SSOT 100 制 dimension_scores 来源:
    # 1) 优先用 score_data.dimension_scores(新诊断主流程已算)
    dim_scores = score_data.get("dimension_scores") or {}

    # 2) 兼容旧 score_data 平铺字段(W2 注入约定)
    if not dim_scores:
        dim_scores = {
            k: score_data.get(k)
            for k in [
                "ai_recommendation_score", "web_content_score", "authority_score",
                "structured_content_score", "brand_foundation_score",
            ]
            if score_data.get(k) is not None
        }

    raw_data = diagnosis_results.get("data") or {}
    ai_visibility = raw_data.get("ai_visibility") or {}
    web_search = raw_data.get("web_search") or {}

    # 3) 全部为空 / 全 0 时(老记录或 regen 合成的 0 占位) · 用 ai_visibility + web_search 重新跑 5 维 SSOT
    #    这里调 W1 已修复 result_count fallback bug 的 calculate_geo_scope_score
    #    quality fix(2026-04-26):全 0 也判"无 SSOT" · 避免规避重算
    ssot_keys = ("ai_recommendation_score", "web_content_score", "authority_score",
                 "structured_content_score", "brand_foundation_score")
    has_5dim_ssot = (
        all(dim_scores.get(k) is not None for k in ssot_keys)
        and sum(int(dim_scores.get(k) or 0) for k in ssot_keys) > 0
    )
    recomputed = False
    if not has_5dim_ssot and (ai_visibility or web_search):
        try:
            from tools.scoring.geo_scope_scorer import calculate_geo_scope_score
            recomp = calculate_geo_scope_score(
                ai_visibility_data=ai_visibility,
                web_search_data=web_search,
            )
            new_dims = recomp.get("dimension_scores") or {}
            if new_dims:
                dim_scores = new_dims
                recomputed = True
                logger.info(
                    "[diagnosis_report_v2] 老记录无 5 维 SSOT · 已用 calculate_geo_scope_score 重算"
                )
        except Exception as e:
            logger.warning(f"[diagnosis_report_v2] 5 维 SSOT 重算异常: {e}")

    # 4) 总分 SSOT 决策:重算成功 → 用重算 sum;否则用 score_data.total_score 或 DB total
    if recomputed:
        # 重算成功 · 报告内总分必须等于 5 维 sum(避免封面 vs 5 维表打架)
        total_score = sum(int(v or 0) for v in dim_scores.values())
    else:
        total_score = score_data.get("total_score") or sum(int(v or 0) for v in dim_scores.values())

    business_context = raw_data.get("business_context") or {}
    suggestions = raw_data.get("action_plan") or {}
    if not suggestions:
        # action_plan 可能落在不同字段名
        suggestions = diagnosis_results.get("suggestions") or {}

    # report_writer_v2 Module 6 需要的 high/medium/ongoing 结构
    if isinstance(suggestions, dict):
        formatted_suggestions = {
            "high_priority": suggestions.get("high_priority") or suggestions.get("p0") or [],
            "medium_priority": suggestions.get("medium_priority") or suggestions.get("p1") or [],
            "ongoing": suggestions.get("ongoing") or suggestions.get("p2") or [],
        }
    else:
        formatted_suggestions = {"high_priority": [], "medium_priority": [], "ongoing": []}

    # CTO-G 2026-04-27 · 注入漏斗 3 层评分(老板拍板 · 替代旧 5 维度)
    # 数据源:ai_visibility_data.dimension_stats(已经按 brand_awareness/regional_industry/super_tier1 分桶)
    dim_stats = (ai_visibility or {}).get("dimension_stats") or {}
    brand_st = dim_stats.get("brand_awareness") or {}
    local_st = dim_stats.get("regional_industry") or {}
    scenario_st = dim_stats.get("super_tier1") or {}
    funnel_input = {
        "brand_detected": int(brand_st.get("detected", 0) or 0),
        "brand_total": int(brand_st.get("total", 0) or 0),
        "local_detected": int(local_st.get("detected", 0) or 0),
        "local_total": int(local_st.get("total", 0) or 0),
        "scenario_detected": int(scenario_st.get("detected", 0) or 0),
        "scenario_total": int(scenario_st.get("total", 0) or 0),
    }
    try:
        from tools.scoring.funnel_score import calculate_funnel_score
        funnel_result = calculate_funnel_score(**funnel_input)
    except Exception as fe:
        logger.warning(f"[diagnosis_report_v2] 漏斗评分计算失败 · 降级 0 分隐形级: {fe}")
        funnel_result = {
            "total_score": 0,
            "max_score": 100,
            "level": "隐形级",
            "level_meta": {"label": "隐形级", "color": "rose", "color_hex": "#9f1239",
                           "summary": "评分异常", "business_meaning": "评分计算异常 · 请联系运维"},
            "layers": [],
            # [GEO-R10-CAN-009] 打典型「算分失败」标记 · 让 update_diagnosis_v2_in_db 的
            # write_funnel_columns gate 判 False → 不把 0 分/隐形级回写 diagnosis_records.total_score
            # /level 与 brands.latest_score(否则覆盖上一次有效总分)。fallback 仅用于当次渲染兜底。
            "calc_failed": True,
        }

    if not any(formatted_suggestions.values()):
        from services.report_action_recommendations import (
            build_layer_context,
            derive_funnel_suggestions,
        )

        # [P2-12] 带上本次实测的可量化上下文（0 命中词 + AI 实际引用的信源域名数），
        #   让行动建议是"针对哪几个词、做几篇、参照量多少"，不是一句抽象话。
        formatted_suggestions = derive_funnel_suggestions(
            funnel_result, layer_context=build_layer_context(ai_visibility)
        )

    return {
        "brand_name": brand.get("name") or brand.get("brand_name") or diagnosis_results.get("brand_name", ""),
        "industry": brand.get("industry") or diagnosis_results.get("industry", ""),
        "city": brand.get("city") or "",
        # CTO-G 2026-04-27 · total_score 改用漏斗 3 层评分(覆盖老 5 维 SSOT 旧总分)
        "total_score": funnel_result["total_score"],
        "scores": dim_scores,            # 5 维度 SSOT 100 制 · 给 Module 2 旧表(向后兼容)
        "funnel_score": funnel_result,   # 新漏斗评分 · Module 2 主用
        "suggestions": formatted_suggestions,  # 给 Module 6
        # CTO-B W2 注入的扩展字段
        "completeness": completeness,    # 给 Module 0
        "diagnosis_data": {
            "ai_visibility_data": ai_visibility,   # 给 Module 2 关键词分层
            "web_search_data": web_search,
            "business_context": business_context,
            "asr_transcripts": raw_data.get("asr_transcripts", []),
        },
        "monthly_search_volume": diagnosis_results.get("monthly_search_volume"),
    }


def assemble_diagnosis_report_v2(
    diagnosis_results: dict,
    brand: dict,
    profile: Optional[dict] = None,
    quote: Optional[dict] = None,
    *,
    brand_id: int,
    score_data: Optional[dict] = None,
    # 🔴 [#54/#55] 「已发布证据」四态,由调用方**查好传入**(本层不查库,
    #    在装配深处自开连接会重放 08-10 那次自锁死)。
    #    形态 = `StageValue.as_dict()`:{count, available, reason}。
    #    **required keyword-only,故意不给默认值** —— 5 个调用点里漏改一个,
    #    默认值会让这一项安静地退出分母、把分数拉高、零报错;没有默认值则当场 TypeError。
    published: dict[str, Any],
) -> dict[str, Any]:
    """诊断报告 v2 装配 · 同时产出 internal + client 两版

    决策点 2:落库 + 失效后重算 · 调用方负责把返回值落到 diagnosis_records 新列
    决策点 5:不静默降级 v1 · v2 异常时返 error 字段 · 调用方决定是否再 fallback v1

    Args:
        diagnosis_results: diagnosis_workflow 主流程产出 · 含 data/scores
        brand: brands 表行
        profile: client_profiles 表行(可空)
        quote: 最近一次报价(可空 · 用于完整度计算)
        brand_id: 必传 · evidence + competitor 抽取依赖
        score_data: 可显式传入(诊断主流程已算的)· 不传则从 diagnosis_results 取

    Returns:
        见模块 docstring
    """
    from services.report_metrics import compute_data_completeness
    from services.report_writer_v2 import assemble_report_v2

    profile = profile or {}
    quote = quote or {}
    score_data = score_data or diagnosis_results.get("score_data") or diagnosis_results.get("scores") or {}

    # 1) 完整度(W1 SSOT)· 必须先算 · 注入 report_data 给 Module 0
    diag_db_row = {
        "ai_total_tests": (diagnosis_results.get("data", {}).get("ai_visibility", {}) or {}).get("total_tests", 0),
        "web_brand_direct_count": (
            diagnosis_results.get("data", {}).get("web_search", {}) or {}
        ).get("brand_direct_count", 0),
        "keywords": diagnosis_results.get("keywords") or "",
    }
    try:
        completeness = compute_data_completeness(
            brand=brand,
            profile=profile,
            diagnosis=diag_db_row,
            quote=quote,
            published=published,
        )
    except Exception as ce:
        logger.warning(f"[diagnosis_report_v2] 完整度计算失败 · 降级为空 banner: {ce}")
        completeness = {
            "score": None,
            "level": "未知",
            "groups": [],
            "impact_notes": [f"完整度计算异常: {ce}"],
            "missing_summary": "完整度无法计算 · 请联系运维查 services.report_metrics 日志",
        }

    # 2) 拼 report_data
    report_data = _shape_report_data(diagnosis_results, score_data, brand, profile, completeness)

    # 3) 装配 internal + client 两版(决策点 2)
    # v3.6 白标:按 surface 解析 branding(client→customer surface 出代理品牌 / internal→agent surface 仅 oem)·
    # 透传给 assemble_report_v2 → Module 8 CTA(P1 修:Module 8「和 XX GEO 规划师聊」需 branding)。
    _wl_owner = (brand or {}).get("owner_user_id")
    internal_branding = None
    client_branding = None
    try:
        from services.public_whitelabel import resolve_branding_context
        internal_branding = resolve_branding_context(
            surface="agent", owner_user_id=_wl_owner, brand_id=brand_id
        ).get("brand")
        _client_ctx = resolve_branding_context(
            surface="customer", owner_user_id=_wl_owner, brand_id=brand_id
        )
        # [P1 Codex finding2] 客户面无白标(platform_default)→ None,绝不把平台默认 dict 喂 Module 8 CTA(否则露 OmniRank)
        client_branding = _client_ctx.get("brand") if _client_ctx.get("source") != "platform_default" else None
    except Exception as be:
        logger.warning(f"[diagnosis_report_v2] branding 解析失败 · 退平台默认: {be}")

    error_msg: Optional[str] = None
    internal_result: dict[str, Any] = {}
    client_result: dict[str, Any] = {}

    try:
        internal_result = assemble_report_v2(
            report_data,
            brand_id=brand_id,
            report_type="diagnosis",
            audience="internal",
            branding=internal_branding,
        )
    except Exception as ie:
        logger.error(f"[diagnosis_report_v2] internal 装配异常: {ie}", exc_info=True)
        error_msg = f"internal 装配异常: {ie}"

    try:
        client_result = assemble_report_v2(
            report_data,
            brand_id=brand_id,
            report_type="diagnosis",
            audience="client",
            branding=client_branding,
        )
    except Exception as ce:
        logger.error(f"[diagnosis_report_v2] client 装配异常: {ce}", exc_info=True)
        error_msg = (error_msg or "") + f" · client 装配异常: {ce}"

    # 4) 拼最终返回
    # P1-4 SSOT(2026-04-27): 顶层暴露 funnel_score · update_diagnosis_v2_in_db 回写 total_score/level 列
    # 让 M3 内部视角(/api/diagnosis/{id}) 与公开视角(/api/public/report/{id}) 取同一总分等级
    # [CTO-15.23 2026-05-09] 顶层暴露 sentiment · update_diagnosis_v2_in_db 写进 modules_jsonb.sentiment
    # 仅代理端 internal 视角 renderer 渲染 · 客户决策页(render_customer_decision_page_html)不显示
    sentiment_data = (diagnosis_results.get("data") or {}).get("sentiment") or {}
    out: dict[str, Any] = {
        "version": "v2",
        "internal": internal_result,
        "client": client_result,
        "completeness": completeness,
        "funnel_score": report_data.get("funnel_score"),  # 漏斗 3 层 SSOT · 给 update_diagnosis_v2_in_db 回写列用
        "sentiment": sentiment_data if sentiment_data else None,  # 4 引擎舆情诊断
        "error": error_msg,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "brand_id": brand_id,
    }

    if error_msg:
        logger.warning(
            f"[diagnosis_report_v2] brand={brand_id} 装配存在异常 · "
            f"调用方应记录到 report_v2_error 列 · 前端展示「报告生成异常」: {error_msg}"
        )

    return out


def update_diagnosis_v2_in_db(diagnosis_id: int, v2_result: dict, *, conn=None) -> bool:
    """把 v2 装配结果写入 diagnosis_records 的 W1 新列(决策点 2)

    Args:
        diagnosis_id: 诊断记录 id
        v2_result: assemble_diagnosis_report_v2 返回值
        conn: [2026-07-22 板块A A7] 可选外部事务连接。传入时复用该连接并跳过
            commit/rollback/close(由调用方保证单事务原子性 · 诊断人工确认重算链用);
            不传时行为与之前完全一致(自开连接 + 自提交)。

    Returns:
        True = 写入成功
    """
    if not diagnosis_id or not v2_result:
        return False

    import json as _json
    from db.connection import get_connection

    internal = v2_result.get("internal") or {}
    client = v2_result.get("client") or {}
    completeness = v2_result.get("completeness") or {}

    internal_md = internal.get("full_markdown", "")
    client_md = client.get("full_markdown", "")

    # P1-4 SSOT(2026-04-27): 顶层 funnel · 让 services.report_v2_score.resolve_canonical_score
    # 老 v2 报告(没回写 total_score/level 列)读取时优先从这里取
    funnel_score = v2_result.get("funnel_score") or {}

    # [CTO-15.23 2026-05-09] 4 引擎舆情诊断 · 仅代理端 internal 视角 renderer 渲染
    sentiment = v2_result.get("sentiment") or None

    modules_jsonb = {
        "internal": {
            "modules": internal.get("modules"),
            "evidence_count": internal.get("evidence_count"),
            "lint_warnings": internal.get("lint_warnings", []),
        },
        "client": {
            "modules": client.get("modules"),
            "evidence_count": client.get("evidence_count"),
            "lint_warnings": client.get("lint_warnings", []),
        },
        "funnel": {
            "total_score": funnel_score.get("total_score"),
            "level": funnel_score.get("level"),
            "level_meta": funnel_score.get("level_meta"),
            "layers": funnel_score.get("layers"),
        } if funnel_score else None,
        "sentiment": sentiment,
    }

    completeness_breakdown = {
        "groups": completeness.get("groups", []),
        "impact_notes": completeness.get("impact_notes", []),
        "missing_summary": completeness.get("missing_summary", ""),
        "level": completeness.get("level", ""),
    }

    # P1-4 SSOT(2026-04-27): v2 漏斗有效时同步回写 total_score / level 列
    # 让 M3 内部视角(/api/diagnosis/{id}) + 公开视角(/api/public/report/{id})
    # + 任何旧 SQL 列读取(管理后台 / 列表)都拿到漏斗 3 层 SSOT 同一值。
    # 漏斗失败兜底时 (funnel_score=None / total_score=None) 不动旧列, 保持向后兼容。
    funnel_total = (
        funnel_score.get("total_score")
        if isinstance(funnel_score, dict) else None
    )
    funnel_level = (
        funnel_score.get("level")
        if isinstance(funnel_score, dict) else None
    )
    # [GEO-R10-CAN-009] 漏斗算分异常时 fallback 会给出 total_score=0 / level=隐形级(非 None),
    # 若不识别就会把 0 分回写覆盖上一次有效总分。这里显式认「calc_failed」典型标记 → 跳过回写,
    # 保留 diagnosis_records.total_score/level 与 brands.latest_score 的最后一次有效规范分。
    funnel_calc_failed = bool(
        isinstance(funnel_score, dict) and funnel_score.get("calc_failed")
    )
    write_funnel_columns = (
        funnel_total is not None
        and funnel_level is not None
        and not v2_result.get("error")
        and not funnel_calc_failed
    )

    # [2026-07-22 板块A A7] 外部连接 = 调用方拥有事务边界(单事务原子重算链)
    owns_connection = conn is None
    if owns_connection:
        conn = get_connection()
    try:
        cur = conn.cursor()
        if write_funnel_columns:
            cur.execute("""
                UPDATE diagnosis_records
                SET report_v2_version = %s,
                    report_v2_internal_md = %s,
                    report_v2_client_md = %s,
                    report_v2_modules_jsonb = %s,
                    report_v2_generated_at = NOW(),
                    report_v2_error = %s,
                    data_completeness_score = %s,
                    data_completeness_breakdown = %s,
                    total_score = %s,
                    level = %s
                WHERE id = %s
            """, (
                "v2",
                internal_md,
                client_md,
                _json.dumps(modules_jsonb, ensure_ascii=False, default=str),
                v2_result.get("error"),
                completeness.get("score"),
                _json.dumps(completeness_breakdown, ensure_ascii=False, default=str),
                int(funnel_total),
                funnel_level,
                diagnosis_id,
            ))
        else:
            cur.execute("""
                UPDATE diagnosis_records
                SET report_v2_version = %s,
                    report_v2_internal_md = %s,
                    report_v2_client_md = %s,
                    report_v2_modules_jsonb = %s,
                    report_v2_generated_at = NOW(),
                    report_v2_error = %s,
                    data_completeness_score = %s,
                    data_completeness_breakdown = %s
                WHERE id = %s
            """, (
                "v2",
                internal_md,
                client_md,
                _json.dumps(modules_jsonb, ensure_ascii=False, default=str),
                v2_result.get("error"),
                completeness.get("score"),
                _json.dumps(completeness_breakdown, ensure_ascii=False, default=str),
                diagnosis_id,
            ))

        # 🔴 [#63 · Review §19③] 报告冻结的**同一个事务**里写一行报告快照。
        #    在此之前 `defgeo_report_snapshots` **全仓零 INSERT** ——
        #    读点(五卡)取不到行就走「绑不了就隐藏」,于是五卡**永远留白**,
        #    而 08-21 的验收把那个留白读成了「诚实留白」。
        #    写在这里而不是 `diagnosis_workflow.py:353`:那处只在**失败**路径
        #    写 `report_v2_generated_at`,不是冻结时刻。
        try:
            from services.defensive_geo.monitoring import snapshot_store as _snapstore
            _snapstore.persist_report_snapshot(
                cur, diagnosis_id=diagnosis_id,
                state=("failed" if v2_result.get("error") else "ready"))
        except Exception as _snap_err:
            # 🔴 报告本身比快照重要,所以不抛;但**不能静默** ——
            #    被正确捕获的失败不留痕迹,最该诊断的就最查不到。
            logger.warning(
                "[defgeo-snapshot] 诊断 %s 的报告快照没写成(五卡会留白):%s",
                diagnosis_id, _snap_err, exc_info=True)
        # [CTO-15.23 2026-05-06 P0-2 SSOT 修复] 同步漏斗分到 brands.latest_score
        # [CTO-15.23 2026-05-07 P0-1 v2 补丁] 同步 latest_diagnosis_id 防黏滞
        # 老板 E2E 测试发现:报告页 13/100 vs 客户横幅 33/100 撕裂
        # 根因 v1:v2 漏斗写 diagnosis_records.total_score=13 但忘写 brands.latest_score
        # 根因 v2(05-07 v2 体检师 13 行 mismatch):
        #   v1 update_brand_stats 失败(被 except 吞)→ latest_diagnosis_id 黏滞老 diag
        #   v2 同步只补 latest_score → JOIN 老 latest_diagnosis_id 拿到老 total_score → mismatch
        # → 必须同时同步 latest_diagnosis_id · 单事务覆盖防黏滞
        # 修法:JOIN diagnosis_records 取 brand_id · 不依赖外部参数 · 不重复加 diagnosis_count
        # ⚠️ 不能调 update_brand_stats(L1949 diagnosis_count + 1 · v1 已经 +1 过)
        # [WO_V2_REGEN_POLLUTES_BRAND_LATEST 2026-08-08] 上面这条归属校验是对的,但缺
        #   「这份得是**最新**那份」—— 重生一份**旧**诊断的 v2 报告会把 latest_* 拽回旧诊断。
        #   实测:浙江岱林生物客户列表分 69 → 50。触发路径含服务商对旧诊断做人工身份确认
        #   (decide_brand_cell → 重生 v2),**那是日常操作**。
        #   → 改走 SSOT 单点,归属 + 最新 + published-only 三重约束都在 SQL 里。
        #   brand_id 传 None = 保持本处既有语义(归属以诊断记录为准,不依赖外部参数)。
        if write_funnel_columns:
            try:
                _synced = sync_brand_latest(
                    cur, diagnosis_id=diagnosis_id, score=int(funnel_total), brand_id=None
                )
                if _synced == 0:
                    # 0 行 = 被守卫挡下(不是最新 / 非 published),是**正常防御结果**不是错误。
                    logger.info(
                        "[diagnosis_report_v2] SSOT sync skipped by guard "
                        "diagnosis_id=%s(非该品牌 published-only 最新那份 · 冗余列保持不动)",
                        diagnosis_id,
                    )
                else:
                    logger.info(f"[diagnosis_report_v2] SSOT sync brands.latest_score+latest_diagnosis_id "
                                f"diagnosis_id={diagnosis_id} score={int(funnel_total)}")
            except Exception as _sync_err:
                logger.warning(f"[diagnosis_report_v2] brands.latest_score 同步失败(非阻塞): {_sync_err}")

        if owns_connection:
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"[diagnosis_report_v2] 写入 diagnosis_records 失败 id={diagnosis_id}: {e}", exc_info=True)
        if owns_connection:
            try:
                conn.rollback()
            except Exception:
                pass
        return False
    finally:
        if owns_connection:
            try:
                conn.close()
            except Exception:
                pass


def is_diagnosis_v2_enabled(brand_id: Optional[int] = None) -> bool:
    """诊断报告 v2 开关(决策 + 老板硬要求 A:诊断主链必须走 v2)

    与 report_writer_v2.is_report_v2_enabled 区别:
      - 周报/月报路径(ai_write_report)用旧的 is_report_v2_enabled · 灰度白名单
      - 诊断主链路(本函数)默认 ON · 老板拍 W1-W5 全量直接走 v2
      - settings.report_v2_diagnosis_disabled = True 时全局关(紧急回滚开关)
    """
    try:
        from config.settings_manager import load_settings
        settings = load_settings()
    except Exception:
        return True  # settings 失败也走 v2(默认 ON · 决策硬要求 A)

    if getattr(settings, "report_v2_diagnosis_disabled", False) is True:
        return False
    return True
