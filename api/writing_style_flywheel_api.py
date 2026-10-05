"""Admin API for GEO writing strategy flywheel shadow layer.

[A1 事件循环阻塞治理 · 2026-08-01]
本文件的重聚合 GET(article-structure/analyze、evolution-board、flywheel-insight、trends、
panorama、outcome-summary、article-review-queue)早已 `asyncio.to_thread` 化 —— 不是本次 P1 的源头。
剩下 5 个只读 GET(strategy-versions / strategy-active / strategy-versions/{id}/audit /
style-simulations / style-simulation/{id})仍是 async handler 里裸调同步 psycopg2,
生产 access log 实证 strategy-versions 与 strategy-active 在并发页加载里同样被钉到 rt=60.0 → 504。
按裁定 §A3 改 `def`(FastAPI 自动 run_in_threadpool),行为零变化。归因与全站清单见
`api/media_entity_flywheel_api.py` 顶部 + `scripts/scan_async_endpoint_blocking_2026_08_01.py`。
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from services.flywheel_rebuild_lock import run_rebuild_guarded  # [B1-6] 后台化 + 互斥守卫

from db.media_entity_flywheel_db import get_flywheel_data_health
from db.writing_style_flywheel_db import (
    activate_strategy_version,
    get_active_strategy_version,
    get_strategy_version,
    init_writing_style_flywheel_tables,
    load_strategy_generation_inputs,
    list_strategy_audit_events,
    list_strategy_versions,
    rebuild_style_feature_snapshots_from_articles,
    review_strategy_version,
    rollback_strategy_version,
    upsert_strategy_version,
    upsert_style_feature_snapshot,
)
from services.flywheel_calibration_service import build_calibration_from_db, build_calibration_suggestion
from services.media_entity_flywheel import industry_filter_values, is_all_industry_scope, normalize_industry_key
# 🔴 这两个 import 故意分两行写:tests/test_article_structure_analysis_static.py 的接线锁
# 按字面串 "services.article_structure_analysis import analyze_article_structure_patterns"
# 断言本文件确实接了结构分析。合并成一行会让那条锁失配转红(2026-08-16 实测踩到)。
from services.article_structure_analysis import ReportComputeTimeout
from services.article_structure_analysis import analyze_article_structure_patterns
from services.writing_strategy_service import build_strategy_candidate
from services.writing_style_feature_extractor import extract_writing_style_features
from db.writing_query_intent_db import (  # [W1] 语料标签层
    get_intent_distribution,
    get_query_intent_coverage,
    init_writing_query_intent_tables,
)
from services.research_monitor.query_intent_classifier import (  # [W1] query 意图分类 backfill
    estimate_query_intent_cost,
    is_query_intent_backfill_running,
    run_query_intent_backfill,
)
from services.writing_answer_distiller import (  # [W2] 标准答案蒸馏
    distill_candidates,
    is_distill_running,
)
from db.writing_style_simulation_db import (  # [W3] 模拟对比 shadow
    get_simulation,
    init_writing_style_simulation_tables,
    list_simulations,
)
from services.writing_style_simulation import (  # [W3] 生产同源模拟
    estimate_simulation_cost,
    is_style_simulation_running,
    run_style_simulation,
)
from services.writing_style_reviewer import is_review_running, review_simulation  # [W4] 双盲双模型评审
from services.writing_corpus_export import export_corpus  # [W5] 一键获取被采纳范文集
from services.flywheel_panorama import get_flywheel_panorama, get_flywheel_trends  # [W7] 全景+趋势只读聚合
from services.writing_evolution_board import get_evolution_board  # [W5] 写作进化看板聚合
from services.flywheel_insight import get_flywheel_insight  # [V5] 飞轮总汇总(LLM 解释层)
from writing.flywheel_cache import (  # [V7] 进程 TTL 缓存 + 写动作失效
    SCOPE_BOARD,
    SCOPE_OUTCOME,
    SCOPE_PANORAMA,
    SCOPE_STRUCTURE,
    SCOPE_TRENDS,
    get_or_compute,
    invalidate,
    make_key,
)
from auth.user_ctx import current_user_id


router = APIRouter(prefix="/api/admin/geo-placement-flywheel", tags=["GEO写作飞轮"])


def _require_admin(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


class StyleFeaturePreviewRequest(BaseModel):
    article: dict[str, Any] = Field(..., description="文章行或正文片段")
    persist: bool = False


class StrategyCandidateRequest(BaseModel):
    industry_key: str = Field("general", max_length=100)
    style_features: list[dict[str, Any]] = Field(default_factory=list)
    source_signals: list[dict[str, Any]] = Field(default_factory=list)
    outcome_signals: list[dict[str, Any]] = Field(default_factory=list)
    operator_note: str = Field("", max_length=1000)
    persist: bool = False


class StrategyGenerateFromDataRequest(BaseModel):
    industry_key: str = Field("general", max_length=100)
    limit: int = Field(200, ge=1, le=1000)
    persist: bool = False


class StyleFeatureRebuildRequest(BaseModel):
    industry_key: str = Field("general", max_length=100)
    limit: int = Field(300, ge=1, le=1000)
    min_chars: int = Field(500, ge=0, le=20000)
    dry_run: bool = True


class ArticleStructureAnalyzeRequest(BaseModel):
    industry_key: str = Field("general", max_length=100)
    limit: int = Field(300, ge=1, le=1000)
    min_chars: int = Field(500, ge=0, le=20000)
    dry_run: bool = True
    llm_rules: bool = Field(False, description="[V4] 手动刷新时 True:生成行业化 LLM 建议(按指纹缓存);GET 自动加载恒 False")


class StrategyReviewRequest(BaseModel):
    decision: str = Field(..., pattern="^(approve|reject)$")
    note: str = Field("", max_length=1000)


class StrategyActivateRequest(BaseModel):
    note: str = Field("", max_length=1000)


class ArticleHumanReviewRequest(BaseModel):
    decision: str = Field(..., pattern="^(approved|rejected)$")
    reason: str = Field(..., min_length=5, max_length=2000)


class QuestionCandidateRequest(BaseModel):
    industry_id: int = Field(..., ge=1)
    prompt_text: str = Field(..., min_length=5, max_length=2000)
    hypothesis: str = Field(..., min_length=10, max_length=2000)
    single_change_dimension: str = Field(..., max_length=80)
    parent_prompt_id: int | None = Field(None, ge=1)
    query_kind: str = Field("derived_research", pattern="^(research|derived_research)$")


class QuestionTransitionRequest(BaseModel):
    action: str = Field(..., pattern="^(approve|activate_shadow|retire|reject)$")
    reason: str = Field(..., min_length=5, max_length=2000)


class ArticleExperimentCreateRequest(BaseModel):
    style_family: str = Field(..., max_length=64)
    hypothesis: str = Field(..., min_length=10, max_length=2000)
    single_change_dimension: str = Field(..., max_length=80)
    baseline_version_id: str = Field(..., min_length=1, max_length=200)
    candidate_version_id: str = Field(..., min_length=1, max_length=200)
    scope: dict[str, Any] = Field(default_factory=dict)
    min_arm_articles: int = Field(30, ge=10, le=10000)
    minimum_weeks: int = Field(4, ge=1, le=52)


class ArticleExperimentReasonRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=2000)


class ArticleExperimentAssignRequest(BaseModel):
    article_id: int = Field(..., ge=1)
    arm: str = Field(..., pattern="^(control|candidate)$")


class ArticleExperimentReserveTopicRequest(BaseModel):
    topic_id: int = Field(..., ge=1)
    arm: str = Field(..., pattern="^(control|candidate)$")


class ArticleExperimentDecisionRequest(BaseModel):
    decision: str = Field(..., pattern="^(passed|failed|insufficient_samples|cancelled)$")
    reason: str = Field(..., min_length=5, max_length=2000)


class ArticleEvolutionReviewRequest(BaseModel):
    decision: str = Field(..., pattern="^(approved|no_change|rejected)$")
    note: str = Field(..., min_length=5, max_length=2000)


class ArticleExpertGoldLabelRequest(BaseModel):
    judge_kind: str = Field(..., pattern="^(article_review|target_outcome)$")
    source_id: int = Field(..., ge=1)
    human_label: str = Field(..., min_length=2, max_length=64)
    rationale: str = Field(..., min_length=5, max_length=2000)


class CalibrationPreviewRequest(BaseModel):
    scope_key: str = Field(..., max_length=200)
    predictions: list[dict[str, Any]] = Field(default_factory=list)
    outcomes: list[dict[str, Any]] = Field(default_factory=list)


class QueryIntentBackfillRequest(BaseModel):
    industry_key: str = Field("", max_length=100, description="留空=全部行业")
    limit: int = Field(500, ge=1, le=5000)
    dry_run: bool = True


class DistillCandidatesRequest(BaseModel):
    industry_key: str = Field("", max_length=100, description="留空=全部行业")
    limit: int = Field(1000, ge=1, le=5000)
    min_chars: int = Field(500, ge=0, le=20000)
    cap: int = Field(10, ge=1, le=50, description="单次最多蒸馏组数")
    dry_run: bool = True


class StyleSimulationRequest(BaseModel):
    style_code: str = Field("", max_length=60)
    industry_key: str = Field("general", max_length=100)
    version_id: Optional[str] = Field(None, max_length=200, description="候选草稿版本 id")
    demo_quote_id: Optional[int] = Field(None, description="演示 quote;留空自动挑该行业有档案的 quote")
    title: str = Field("", max_length=500, description="题目;留空按文体自动生成")
    dry_run: bool = True


def _require_operation_note(note: str) -> str:
    cleaned = (note or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="review_note_required")
    return cleaned


def _ensure_strategy_health_allows_activation(industry_key: str) -> dict[str, Any]:
    normalized = normalize_industry_key(industry_key or "general")
    values = [] if is_all_industry_scope(industry_key or normalized) else industry_filter_values(industry_key or normalized)
    health = get_flywheel_data_health(
        industry_key=normalized,
        industry_values=values,
    )
    if not health.get("can_activate"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "health_gate_blocked",
                "message": health.get("summary") or "当前行业数据健康未通过，不能启用或回滚策略",
                "health": health,
            },
        )
    return health


def _report_timeout_payload(exc: ReportComputeTimeout) -> dict[str, Any]:
    """[P0 2026-08-16] 报表计算超时的**明示** degraded 响应。

    🔴 铁律:超时**不许**被 fail-soft 吞成 0。
    「0 篇 / 0.00 倍 / 运行正常」和「算不出来」在前端长得一模一样,而处置完全相反:
    前者等数据,后者要重试/报警。2026-08-16 事故里 panorama 的 fail-soft 就是这么
    把整站堵死伪装成「运行正常」的,排查方按「真没有数据」查了两天。

    所以计数一律给 None(不是 0),并置 degraded 标记让前端渲染「数据加载失败」。
    """
    return {
        "status": "degraded",
        "degraded": True,
        "degraded_reason": "report_compute_timeout",
        "degraded_scope": exc.scope,
        "timeout_ms": exc.timeout_ms,
        # 🔴 None ≠ 0:前端据此渲染「数据加载失败」,绝不渲染 0% / 0 篇
        "loaded": None,
        "groups": None,
        "feature_lift": None,
        "engine_count": None,
        "sample_status": None,
        "signal_measured_rows": None,
        "signal_unmeasured_rows": None,
        "recommended_structure_rules": None,
        "recommended_structure_rules_llm": None,
        "legacy_lineage": None,
        "empty_state_reason": "compute_timeout",
        "empty_state_message": (
            f"报表计算超时（超过 {round(exc.timeout_ms / 1000)} 秒未算完），本次没有取到数据。"
            "这**不是**“没有数据”，请稍后重试；若持续超时请缩小 limit 或联系管理员。"
        ),
        "warnings": ["报表计算超时，本次结果不可用（非零数据缺失）。"],
        "dry_run": True,
        "shadow_only": True,
        "production_takeover": False,
    }


def _article_structure_analysis_for_strategy(result: dict[str, Any]) -> dict[str, Any]:
    groups = result.get("groups") or {}
    adopted = groups.get("adopted_group") or {}
    cited = groups.get("cited_group") or {}
    control = groups.get("search_only_control_group") or {}
    return {
        "adopted_group_count": int(adopted.get("count") or 0),
        "explicit_cited_count": int(cited.get("count") or 0),
        "search_only_control_count": int(control.get("count") or 0),
        "engine_count": int(result.get("engine_count") or adopted.get("engine_count") or cited.get("engine_count") or 0),
        "sample_domain_count": int(adopted.get("domain_count") or 0),
        "feature_lift": result.get("feature_lift") or [],
    }


@router.post("/writing/style-feature-preview")
async def style_feature_preview(req: StyleFeaturePreviewRequest, request: Request):
    _require_admin(request)
    features = extract_writing_style_features(req.article)
    row = None
    if req.persist:
        init_writing_style_flywheel_tables()
        row = upsert_style_feature_snapshot({
            **features,
            "article_id": features.get("article_id"),
            "source_url": req.article.get("url") or req.article.get("source_url") or "",
            "features": features,
        })
    return {"status": "success", "features": features, "snapshot": row, "shadow_only": True}


@router.post("/writing/style-features/rebuild")
async def rebuild_writing_style_features(req: StyleFeatureRebuildRequest, request: Request):
    """Build writing-style snapshots from research article originals.

    This is admin-only and writes only the shadow feature snapshot table.  It
    does not activate a writing strategy or change production prompts.
    """
    _require_admin(request)
    # [B1-6] 后台线程执行 + 互斥(此重建现含 OSS 正文回读,更重,必须离开事件循环)
    def _work() -> dict[str, Any]:
        init_writing_style_flywheel_tables()
        return rebuild_style_feature_snapshots_from_articles(
            industry=req.industry_key,
            limit=req.limit,
            min_chars=req.min_chars,
            dry_run=req.dry_run,
        )
    return await run_rebuild_guarded("writing_style_features_rebuild", _work)


@router.post("/writing/article-structure/analyze")
async def writing_article_structure_analyze(req: ArticleStructureAnalyzeRequest, request: Request):
    """Read-only article-structure research for answer-adopted article patterns."""
    _require_admin(request)
    # [B1-6] 后台线程执行 + 互斥(analyze 现含 OSS 正文回读,量大时会阻塞事件循环)
    # [V4] llm_rules=True(手动[刷新行业化建议])才生成 LLM 建议;GET 自动加载路径恒 False。
    try:
        result = await run_rebuild_guarded(
            "article_structure_analyze",
            analyze_article_structure_patterns,
            industry=req.industry_key, limit=req.limit, min_chars=req.min_chars, llm_rules=req.llm_rules,
        )
    except ReportComputeTimeout as exc:  # [P0] 明示 degraded,不吞成 0
        return _report_timeout_payload(exc)
    if isinstance(result, dict):
        result["dry_run"] = True
        result["shadow_only"] = True
        result["production_takeover"] = False
    if req.llm_rules:
        # [V4] 生成/刷新了 LLM 建议 → 失效 GET analyze 的进程缓存,下次 GET 重算即带上新建议
        invalidate([SCOPE_STRUCTURE])
    return result


@router.get("/writing/article-structure/analyze")
async def writing_article_structure_analyze_get(
    request: Request,
    industry_key: str = "general",
    limit: int = 300,
    min_chars: int = 500,
):
    """Read-only article-structure research for dashboard auto-loading."""
    _require_admin(request)
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit 必须 1-1000")
    if min_chars < 0 or min_chars > 20000:
        raise HTTPException(status_code=400, detail="min_chars 必须 0-20000")
    # [B1-6] dashboard 自动加载:只离开事件循环(to_thread),不加"进行中"返回(否则会破坏自动加载);
    # analyze 是只读 dry-run,并发多次无写竞争。
    # [V7] 进程 TTL 缓存(跨 tab 复用最重聚合);[V4] GET 恒 llm_rules=False → 只取缓存的 LLM 建议不内联调。
    try:
        result = await asyncio.to_thread(
            get_or_compute,
            make_key(SCOPE_STRUCTURE, industry_key, limit, min_chars),
            600.0,
            lambda: analyze_article_structure_patterns(
                industry=industry_key, limit=limit, min_chars=min_chars, llm_rules=False
            ),
        )
    except ReportComputeTimeout as exc:
        # [P0] get_or_compute 对抛异常的 compute **不写缓存**(见 flywheel_cache 文档字符串),
        # 所以 degraded 不会被 600s TTL 钉住,下次请求会重算。
        return _report_timeout_payload(exc)
    return {**result, "dry_run": True, "shadow_only": True, "production_takeover": False}


@router.get("/writing/query-intent/coverage")
async def writing_query_intent_coverage(request: Request, industry_key: str = ""):
    """[W1] 问题类型标签覆盖率 + 分布(看板用)。已分类 distinct query / raw 里 distinct query 总数。"""
    _require_admin(request)
    industry = industry_key.strip() or None

    def _coverage_sync() -> dict:
        init_writing_query_intent_tables()
        out = get_query_intent_coverage(industry)
        out["distribution"] = get_intent_distribution(industry)
        return out

    # [review fix] 全表 GROUP BY 的同步 SQL 挪线程池(生产 WORKERS=1,跑在主循环会卡全站)
    coverage = await asyncio.to_thread(_coverage_sync)
    coverage["backfill_running"] = is_query_intent_backfill_running()
    return {"status": "success", "shadow_only": True, **coverage}


@router.post("/writing/query-intent/backfill")
async def writing_query_intent_backfill(req: QueryIntentBackfillRequest, request: Request):
    """[W1] 给尚未打标签的搜索问题批量分类「问题类型」。

    dry_run 默认:只计数 + 预估成本(零 LLM)。真跑:后台任务 + 分类器内线程锁互斥,
    单条 fail-soft。真跑成本由平台承担(不扣用户费),受 limit 上限;首次全量回填建议先
    dry-run 预估 → 老板授权后再真跑。
    """
    _require_admin(request)
    init_writing_query_intent_tables()
    industry = req.industry_key.strip() or None
    if req.dry_run:
        return {"status": "success", "shadow_only": True, **await run_query_intent_backfill(
            industry=industry, limit=req.limit, dry_run=True
        )}
    if is_query_intent_backfill_running():
        return {"status": "in_progress", "message": "query 分类 backfill 正在进行中,请稍后再试。"}
    # 真跑 LLM:后台任务(create_task 在服务端常驻事件循环,非一次性 loop,安全);分类器内线程锁互斥,
    # 重查询已在服务内挪线程池,不堵主循环。
    asyncio.create_task(run_query_intent_backfill(
        industry=industry, limit=req.limit, dry_run=False,
    ))
    return {
        "status": "started",
        "shadow_only": True,
        "message": "已在后台开始分类;完成后可在覆盖率端点查看进度。",
        "target_limit": req.limit,
        "cost_estimate": estimate_query_intent_cost(req.limit),
    }


@router.post("/writing/distill-candidates")
async def writing_distill_candidates(req: DistillCandidatesRequest, request: Request):
    """[W2] 按 (行业 × 文体) 蒸馏候选包三件套(标准答案模板 + 候选 prompt + 证据)→ 写入版本面 draft。

    dry_run 默认:列出可蒸馏组 + 成本预估(零 LLM)。真跑:后台任务 + 服务内线程锁互斥,每组
    蒸馏→create_distilled_draft 落 draft(经守卫);只落草稿,绝不直接影响客户可见文章(须管理员
    在看板走既有双闸 activate)。首次全量建议先 dry-run 预估 → 老板授权后再真跑。
    """
    user = _require_admin(request)
    industry = req.industry_key.strip() or ""
    if req.dry_run:
        return {"status": "success", "shadow_only": True, **await distill_candidates(
            industry=industry, limit=req.limit, min_chars=req.min_chars, cap=req.cap, dry_run=True
        )}
    if is_distill_running():
        return {"status": "in_progress", "message": "蒸馏正在进行中,请稍后再试。"}
    actor_id = int(current_user_id(user) or user.get("user_id") or 0)
    # 真跑 LLM:后台任务(常驻事件循环,安全)+ 蒸馏器内线程锁互斥;plan 重活已在服务内挪线程池。
    asyncio.create_task(distill_candidates(
        industry=industry, limit=req.limit, min_chars=req.min_chars,
        cap=req.cap, dry_run=False, actor_id=actor_id, persist_draft=True,
    ))
    return {
        "status": "started",
        "shadow_only": True,
        "message": "已在后台开始蒸馏;完成后候选会以 draft 出现在版本面,请在看板审核。",
        "cap": req.cap,
    }


@router.post("/writing/style-simulation")
async def writing_style_simulation(req: StyleSimulationRequest, request: Request):
    """[W3] 生产同源模拟对比:当前版 vs 候选版两篇样文(同题目/同品牌/同结构参考状态,仅 base_prompt 不同)。

    dry_run 默认:解析演示上下文 + 成本预估(零 LLM)。真跑:后台任务 + 服务内 async 互斥,两臂
    生成→存 writing_style_simulations shadow 表。样文**绝不进 articles 主表/客户面**。
    """
    user = _require_admin(request)
    init_writing_style_simulation_tables()
    if req.dry_run:
        return {"status": "success", "shadow_only": True, **await run_style_simulation(
            style_code=req.style_code, industry_key=req.industry_key, version_id=req.version_id,
            demo_quote_id=req.demo_quote_id, title=req.title, dry_run=True,
        )}
    if is_style_simulation_running():
        return {"status": "in_progress", "message": "模拟正在进行中,请稍后再试。"}
    actor_id = int(current_user_id(user) or user.get("user_id") or 0)
    asyncio.create_task(run_style_simulation(
        style_code=req.style_code, industry_key=req.industry_key, version_id=req.version_id,
        demo_quote_id=req.demo_quote_id, title=req.title, dry_run=False, actor_id=actor_id,
    ))
    return {
        "status": "started",
        "shadow_only": True,
        "message": "已在后台生成两篇样文;完成后可在对比列表查看。",
        "cost_estimate": estimate_simulation_cost(),
    }


@router.get("/writing/style-simulations")
def writing_style_simulations_list(
    request: Request, style_code: str = "", industry_key: str = "", limit: int = 20
):
    """[W3] 模拟对比列表(轻量,不含大正文)。"""
    _require_admin(request)
    return {
        "status": "success",
        "shadow_only": True,
        "simulations": list_simulations(
            style_code=style_code.strip() or None,
            industry_key=industry_key.strip() or None,
            limit=limit,
        ),
    }


@router.get("/writing/style-simulation/{simulation_id}")
def writing_style_simulation_detail(simulation_id: int, request: Request):
    """[W3] 单次对比详情(含两篇样文,供看板并排看)。admin-only,绝不客户面。"""
    _require_admin(request)
    row = get_simulation(simulation_id)
    if not row:
        raise HTTPException(status_code=404, detail="simulation_not_found")
    return {"status": "success", "shadow_only": True, "simulation": row}


class ReviewSimulationRequest(BaseModel):
    dry_run: bool = True


@router.post("/writing/style-simulation/{simulation_id}/review")
async def writing_style_simulation_review(simulation_id: int, req: ReviewSimulationRequest, request: Request):
    """[W4] 双盲双模型评审一次对比:5 维评分 + 替换/保持/观察建议 + 人话差异,写回 review_summary + judge_summary。

    dry_run 默认:检查可评审性 + 评审器数量(零 LLM)。真跑:后台任务(2 个评审模型),完成后回填。
    双模型一致才给 replace;<2 个模型或不一致 → observe(防自嗨)。
    """
    _require_admin(request)
    if not get_simulation(simulation_id):
        raise HTTPException(status_code=404, detail="simulation_not_found")
    if req.dry_run:
        return {"status": "success", "shadow_only": True, **await review_simulation(simulation_id, dry_run=True)}
    if is_review_running(simulation_id):
        return {"status": "in_progress", "message": "该对比正在评审中,请稍后再试。"}
    asyncio.create_task(review_simulation(simulation_id, dry_run=False))
    return {"status": "started", "shadow_only": True, "message": "已在后台开始双盲双模型评审,完成后回填对比结果。"}


@router.get("/writing/evolution-board")
async def writing_evolution_board(request: Request, industry_key: str = ""):
    """[W5] 写作进化看板:每文体一张卡(当前版/候选草稿/最新模拟评分/态)。admin-only 聚合。[V7] TTL 缓存。"""
    _require_admin(request)
    ind = industry_key.strip() or None
    result = await asyncio.to_thread(
        get_or_compute, make_key(SCOPE_BOARD, ind or "all"), 60.0, lambda: get_evolution_board(ind)
    )
    return {"status": "success", "shadow_only": True, **result}


@router.get("/writing/article-review-queue")
async def writing_article_review_queue(request: Request, limit: int = 100):
    """Admin-only review queue. Page load is read-only and never triggers an LLM."""
    _require_admin(request)
    from services.article_review_gate import list_review_queue

    rows = await asyncio.to_thread(list_review_queue, limit=max(1, min(limit, 500)))
    return {"status": "success", "items": rows, "count": len(rows)}


@router.post("/writing/article/{article_id}/human-review")
async def writing_article_human_review(
    article_id: int,
    req: ArticleHumanReviewRequest,
    request: Request,
):
    """Explicit admin signature; every decision appends an immutable audit event."""
    user = _require_admin(request)
    from services.article_review_gate import set_human_review

    try:
        row = await asyncio.to_thread(
            set_human_review,
            article_id,
            reviewer_user_id=int(current_user_id(user) or user.get("user_id") or 0),
            decision=req.decision,
            reason=req.reason,
        )
    except ValueError as exc:
        if str(exc) == "article_not_found":
            raise HTTPException(status_code=404, detail="article_not_found") from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "success", "article": row}


@router.post("/writing/question-candidate")
async def writing_question_candidate(req: QuestionCandidateRequest, request: Request):
    """Create an inactive research candidate; never writes paid monitoring questions."""
    user = _require_admin(request)
    from services.question_evolution import create_question_candidate

    try:
        row = await asyncio.to_thread(
            create_question_candidate,
            industry_id=req.industry_id,
            prompt_text=req.prompt_text,
            actor_user_id=int(current_user_id(user)),
            hypothesis=req.hypothesis,
            single_change_dimension=req.single_change_dimension,
            parent_prompt_id=req.parent_prompt_id,
            query_kind=req.query_kind,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "success", "candidate": row, "billing_monitoring_unchanged": True}


@router.post("/writing/question-candidate/{prompt_id}/transition")
async def writing_question_transition(
    prompt_id: int,
    req: QuestionTransitionRequest,
    request: Request,
):
    """Explicit, audited question-candidate transition; activation is shadow-only."""
    user = _require_admin(request)
    from services.question_evolution import transition_question_candidate

    try:
        row = await asyncio.to_thread(
            transition_question_candidate,
            prompt_id,
            actor_user_id=int(current_user_id(user)),
            action=req.action,
            reason=req.reason,
        )
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(status_code=404 if detail == "question_not_found" else 400, detail=detail) from exc
    return {"status": "success", "candidate": row, "shadow_only": True, "billing_monitoring_unchanged": True}


@router.post("/writing/article-experiment")
async def writing_article_experiment_create(req: ArticleExperimentCreateRequest, request: Request):
    """Pre-register exactly one style change; this endpoint cannot activate it."""
    user = _require_admin(request)
    from services.article_experiment_registry import create_experiment

    try:
        row = await asyncio.to_thread(
            create_experiment,
            style_family=req.style_family,
            hypothesis=req.hypothesis,
            single_change_dimension=req.single_change_dimension,
            baseline_version_id=req.baseline_version_id,
            candidate_version_id=req.candidate_version_id,
            scope=req.scope,
            created_by=int(current_user_id(user)),
            min_arm_articles=req.min_arm_articles,
            minimum_weeks=req.minimum_weeks,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "success", "experiment": row, "production_unchanged": True}


@router.post("/writing/article-experiment/{experiment_id}/approve")
async def writing_article_experiment_approve(
    experiment_id: int, req: ArticleExperimentReasonRequest, request: Request,
):
    user = _require_admin(request)
    from services.article_experiment_registry import approve_experiment

    try:
        row = await asyncio.to_thread(
            approve_experiment, experiment_id,
            actor_user_id=int(current_user_id(user)), reason=req.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "success", "experiment": row, "production_unchanged": True}


@router.post("/writing/article-experiment/{experiment_id}/assign")
async def writing_article_experiment_assign(
    experiment_id: int, req: ArticleExperimentAssignRequest, request: Request,
):
    user = _require_admin(request)
    from services.article_experiment_registry import assign_article

    try:
        row = await asyncio.to_thread(
            assign_article, experiment_id, article_id=req.article_id,
            arm=req.arm, actor_user_id=int(current_user_id(user)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "success", "assignment": row, "assigned_before_publication": True}


@router.post("/writing/article-experiment/{experiment_id}/reserve-topic")
async def writing_article_experiment_reserve_topic(
    experiment_id: int,
    req: ArticleExperimentReserveTopicRequest,
    request: Request,
):
    """Reserve the arm before generation; the live style remains unchanged."""
    user = _require_admin(request)
    from services.article_experiment_registry import reserve_topic

    try:
        row = await asyncio.to_thread(
            reserve_topic,
            experiment_id,
            topic_id=req.topic_id,
            arm=req.arm,
            actor_user_id=int(current_user_id(user)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": "success",
        "assignment": row,
        "assigned_before_generation": True,
        "production_default_unchanged": True,
    }


@router.get("/writing/article-experiment/{experiment_id}/evaluation")
async def writing_article_experiment_evaluation(experiment_id: int, request: Request):
    _require_admin(request)
    from services.article_experiment_registry import evaluate_experiment

    try:
        result = await asyncio.to_thread(evaluate_experiment, experiment_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "success", "evaluation": result, "read_only": True}


@router.post("/writing/article-experiment/{experiment_id}/decision")
async def writing_article_experiment_decision(
    experiment_id: int, req: ArticleExperimentDecisionRequest, request: Request,
):
    user = _require_admin(request)
    from services.article_experiment_registry import record_experiment_decision

    try:
        row = await asyncio.to_thread(
            record_experiment_decision, experiment_id, decision=req.decision,
            actor_user_id=int(current_user_id(user)), reason=req.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "success", "experiment": row, "auto_activation": False}


@router.get("/writing/article-evolution-cycles")
async def writing_article_evolution_cycles(request: Request, limit: int = 24):
    _require_admin(request)
    from services.article_evolution_cycle import list_evolution_cycles

    rows = await asyncio.to_thread(list_evolution_cycles, limit=max(1, min(limit, 100)))
    return {"status": "success", "items": rows, "count": len(rows)}


@router.post("/writing/article-evolution-cycle/run")
async def writing_article_evolution_cycle_run(request: Request, dry_run: bool = True):
    user = _require_admin(request)
    from services.article_evolution_cycle import build_evolution_snapshot, persist_evolution_cycle

    if dry_run:
        snapshot = await asyncio.to_thread(build_evolution_snapshot)
        return {"status": "success", "dry_run": True, "snapshot": snapshot}
    from services.research_monitor.corpus_labeler import promote_jc5_from_direct_signals

    label_result = await asyncio.to_thread(
        promote_jc5_from_direct_signals,
        dry_run=False,
        limit=1000,
    )
    result = await asyncio.to_thread(
        persist_evolution_cycle,
        actor_user_id=int(current_user_id(user)),
        trigger="manual",
    )
    return {
        "status": "success", "dry_run": False, **result,
        "jina_label_result": label_result, "auto_activation": False,
    }


@router.post("/writing/jina-corpus/promote-jc5")
async def writing_jina_corpus_promote_jc5(
    request: Request,
    dry_run: bool = True,
    limit: int = 500,
):
    """Promote only body-hash-bound direct citations; URL-only rows stay JC3."""
    _require_admin(request)
    from services.research_monitor.corpus_labeler import promote_jc5_from_direct_signals

    result = await asyncio.to_thread(
        promote_jc5_from_direct_signals,
        dry_run=dry_run,
        limit=max(1, min(limit, 5000)),
    )
    return {"status": "success", **result}


@router.post("/writing/article-evolution-cycle/{run_id}/review")
async def writing_article_evolution_cycle_review(
    run_id: int,
    req: ArticleEvolutionReviewRequest,
    request: Request,
):
    user = _require_admin(request)
    from services.article_evolution_cycle import review_evolution_cycle

    try:
        row = await asyncio.to_thread(
            review_evolution_cycle,
            run_id,
            actor_user_id=int(current_user_id(user)),
            decision=req.decision,
            note=req.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "success", "run": row, "auto_activation": False}


@router.post("/writing/article-expert/gold-label")
async def writing_article_expert_gold_label(
    req: ArticleExpertGoldLabelRequest,
    request: Request,
):
    user = _require_admin(request)
    from services.article_expert_calibration import add_gold_label

    try:
        row = await asyncio.to_thread(
            add_gold_label,
            judge_kind=req.judge_kind,
            source_id=req.source_id,
            human_label=req.human_label,
            reviewer_user_id=int(current_user_id(user)),
            rationale=req.rationale,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "success", "gold_label": row, "requires_two_reviewer_consensus": True}


@router.get("/writing/article-expert/calibration")
async def writing_article_expert_calibration(request: Request, judge_kind: str = "article_review"):
    _require_admin(request)
    from services.article_expert_calibration import evaluate_gold_standard

    try:
        result = await asyncio.to_thread(evaluate_gold_standard, judge_kind)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "success", "calibration": result}


@router.get("/flywheel-panorama")
async def flywheel_panorama(request: Request, industry_key: str = "", industry: str = ""):
    """[W7] 飞轮环五节点计数(数字同源单一来源)。admin-only · 纯 SELECT · 每项 fail-soft。[V7] TTL 缓存。

    [行业口径 2026-08-18] 接行业参数:有行业列的三环(采集/学习/效果回流)真过滤,
    没有行业维度的两环(人工把关/应用到写作)保持全站并在响应里标 industry_scope="site"。
    `industry_key` 与 `industry` 两个名都收 —— 本页其余端点历史上两种写法都在用
    (`?industry=汽车` 与 `?industry_key=auto`),只认一个会让另一半调用方静默退回全站口径。
    🔴 缓存 key 必须带行业,否则第一个行业的结果会被后续所有行业命中(= 换个马甲的同一个 bug)。
    """
    _require_admin(request)
    ind = (industry_key or industry or "").strip()
    result = await asyncio.to_thread(
        get_or_compute, make_key(SCOPE_PANORAMA, ind or "all"), 60.0, lambda: get_flywheel_panorama(ind or None)
    )
    return {"status": "success", "shadow_only": True, **result}


@router.get("/flywheel-trends")
async def flywheel_trends(request: Request, weeks: int = 12):
    """[W7] C3 引用率周走势 + C4 累计增长。admin-only · 纯 SELECT。[V7] TTL 缓存。"""
    _require_admin(request)
    result = await asyncio.to_thread(
        get_or_compute, make_key(SCOPE_TRENDS, weeks), 600.0, lambda: get_flywheel_trends(weeks)
    )
    return {"status": "success", "shadow_only": True, **result}


@router.get("/writing/flywheel-insight")
async def writing_flywheel_insight(request: Request, industry_key: str = "", refresh: bool = False):
    """[V5] 飞轮总汇总:聚合真实数字 → LLM 产 3-5 条 insight + 一句总评。

    数字永远真实(SQL/统计);LLM 只解释。空数据不调 LLM(显示「数据积累中」);24h 缓存 + [刷新]手动重跑。
    """
    _require_admin(request)
    result = await asyncio.to_thread(
        get_flywheel_insight, industry_key.strip() or None, refresh=bool(refresh)
    )
    return {"status": "success", "shadow_only": True, **result}


@router.get("/writing/outcome-summary")
async def writing_outcome_summary(request: Request, since_days: int = 30, min_age_days: int = 30):
    """严格 GEO 实验结果只读聚合；GET 零写、零 LLM、可缓存。"""
    _require_admin(request)
    sd = max(1, min(int(since_days), 180))
    ma = max(0, min(int(min_age_days), 365))
    from services.article_experiment_registry import summarize_strict_experiment_outcomes

    result = await asyncio.to_thread(
        get_or_compute,
        make_key(SCOPE_OUTCOME, sd, ma),
        300.0,
        summarize_strict_experiment_outcomes,
    )
    return {"status": "success", "shadow_only": True, **result}


@router.get("/writing/corpus-export")
async def writing_corpus_export(
    request: Request,
    industry_key: str = "general",
    style_code: str = "",
    intent_type: str = "",
    signal_layer: str = "adopted",
    limit: int = 50,
):
    """[W5] 一键获取被采纳范文集。按 行业/文体/intent/信号层 筛选,批量返回正文(≤50/次)。

    安全边界:admin-only + 单日上限(默认 500 篇)+ 每次写审计 + 只含正文/来源URL/标题(剥内部字段)
    + OSS 失败该篇跳过(fail-soft)。语料为公开抓取正文,无客户数据。
    """
    user = _require_admin(request)
    actor_id = int(current_user_id(user) or user.get("user_id") or 0)
    result = await asyncio.to_thread(
        export_corpus,
        industry_key=industry_key, style_code=style_code.strip() or None,
        intent_type=intent_type.strip() or None, signal_layer=signal_layer,
        limit=limit, actor_id=actor_id,
    )
    return {"status": "success", "shadow_only": True, **result}


@router.post("/writing/strategy-candidate")
async def writing_strategy_candidate(req: StrategyCandidateRequest, request: Request):
    _require_admin(request)
    candidate = build_strategy_candidate(
        industry_key=req.industry_key,
        style_features=req.style_features,
        source_signals=req.source_signals,
        outcome_signals=req.outcome_signals,
        operator_note=req.operator_note,
    )
    row = None
    if req.persist:
        init_writing_style_flywheel_tables()
        row = upsert_strategy_version(candidate)
    return {"status": "success", "candidate": candidate, "strategy_row": row, "shadow_only": True}


@router.post("/writing/strategy-generate-from-data")
async def writing_strategy_generate_from_data(req: StrategyGenerateFromDataRequest, request: Request):
    """Generate a reviewed-needed strategy candidate from collected shadow data."""
    _require_admin(request)
    init_writing_style_flywheel_tables()
    industry_key = normalize_industry_key(req.industry_key)
    inputs = load_strategy_generation_inputs(industry_key, limit=req.limit)
    try:
        article_structure = analyze_article_structure_patterns(
            industry=req.industry_key,
            limit=min(req.limit, 300),
            min_chars=500,
        )
    except ReportComputeTimeout as exc:
        # [P0] 结构分析是策略候选的**输入**。超时若吞成空 dict,build_strategy_candidate
        # 会拿"0 采纳 / 0 lift"当真实规律去生成策略 —— 比报错坏得多。直接 degraded 返回,
        # 不生成、不落库。
        raise HTTPException(status_code=503, detail=_report_timeout_payload(exc)["empty_state_message"])
    inputs["style_features"].append({
        "style_family": "guide",
        "article_structure_analysis": _article_structure_analysis_for_strategy(article_structure),
    })
    candidate = build_strategy_candidate(
        industry_key=industry_key,
        style_features=inputs["style_features"],
        source_signals=inputs["source_signals"],
        outcome_signals=inputs["outcome_signals"],
        operator_note="由后台数据飞轮生成，需管理员审核后才能启用",
    )
    row = upsert_strategy_version(candidate) if req.persist else None
    return {
        "status": "success",
        "candidate": candidate,
        "strategy_row": row,
        "input_counts": {k: len(v) for k, v in inputs.items()},
        "article_structure_analysis": article_structure,
        "shadow_only": True,
        "requires_admin_review": True,
    }


@router.get("/writing/strategy-versions")
def writing_strategy_versions(
    request: Request,
    industry_key: Optional[str] = None,
    status: str = "shadow",
    limit: int = 50,
):
    _require_admin(request)
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit 必须 1-200")
    init_writing_style_flywheel_tables()
    normalized_industry = normalize_industry_key(industry_key) if industry_key else ""
    rows = list_strategy_versions(industry_key=normalized_industry, status=status, limit=limit)
    return {"status": "success", "items": rows, "shadow_only": True}


@router.get("/writing/strategy-active")
def writing_strategy_active(request: Request, industry_key: str = "general"):
    _require_admin(request)
    init_writing_style_flywheel_tables()
    row = get_active_strategy_version(normalize_industry_key(industry_key))
    return {
        "status": "success",
        "item": row,
        "shadow_only": True,
        "production_takeover": False,
    }


@router.post("/writing/strategy-versions/{strategy_id}/review")
async def writing_strategy_review(strategy_id: int, req: StrategyReviewRequest, request: Request):
    user = _require_admin(request)
    init_writing_style_flywheel_tables()
    existing = get_strategy_version(strategy_id)
    if not existing:
        raise HTTPException(status_code=404, detail="策略版本不存在")
    if existing.get("status") == "active":
        raise HTTPException(status_code=400, detail="已启用策略不能直接驳回，请先启用其他版本")
    row = review_strategy_version(
        strategy_id=strategy_id,
        reviewer_id=int(current_user_id(user) or user.get("user_id") or 0),
        decision=req.decision,
        note=req.note,
    )
    if not row:
        raise HTTPException(status_code=404, detail="策略版本不存在")
    return {"status": "success", "item": row, "shadow_only": True}


@router.post("/writing/strategy-versions/{strategy_id}/activate")
async def writing_strategy_activate(strategy_id: int, req: StrategyActivateRequest, request: Request):
    user = _require_admin(request)
    init_writing_style_flywheel_tables()
    note = _require_operation_note(req.note)
    existing = get_strategy_version(strategy_id)
    if not existing:
        raise HTTPException(status_code=404, detail="策略版本不存在")
    health = _ensure_strategy_health_allows_activation(existing.get("industry_key") or "general")
    row = activate_strategy_version(
        strategy_id=strategy_id,
        reviewer_id=int(current_user_id(user) or user.get("user_id") or 0),
        note=note,
    )
    if not row:
        raise HTTPException(status_code=404, detail="策略版本不存在")
    if row.get("error") == "strategy_not_approved":
        raise HTTPException(status_code=409, detail="strategy_not_approved")
    return {
        "status": "success",
        "item": row,
        "strategy": row,
        "health": health,
        "shadow_only": True,
        "production_takeover": False,
    }


@router.post("/writing/strategy-versions/{strategy_id}/rollback")
async def writing_strategy_rollback(strategy_id: int, req: StrategyActivateRequest, request: Request):
    user = _require_admin(request)
    init_writing_style_flywheel_tables()
    note = _require_operation_note(req.note)
    existing = get_strategy_version(strategy_id)
    if not existing:
        raise HTTPException(status_code=404, detail="策略版本不存在")
    health = _ensure_strategy_health_allows_activation(existing.get("industry_key") or "general")
    row = rollback_strategy_version(
        strategy_id=strategy_id,
        reviewer_id=int(current_user_id(user) or user.get("user_id") or 0),
        note=note,
    )
    if not row:
        raise HTTPException(status_code=404, detail="策略版本不存在")
    if row.get("error") == "strategy_not_rollbackable":
        raise HTTPException(status_code=409, detail="strategy_not_rollbackable")
    return {
        "status": "success",
        "item": row,
        "strategy": row,
        "health": health,
        "shadow_only": True,
        "production_takeover": False,
    }


@router.get("/writing/strategy-versions/{strategy_id}/audit")
def writing_strategy_audit(strategy_id: int, request: Request, limit: int = 50):
    _require_admin(request)
    init_writing_style_flywheel_tables()
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit 必须 1-200")
    if not get_strategy_version(strategy_id):
        raise HTTPException(status_code=404, detail="策略版本不存在")
    return {
        "status": "success",
        "items": list_strategy_audit_events(strategy_id=strategy_id, limit=limit),
        "shadow_only": True,
    }


@router.post("/writing/calibration-preview")
async def writing_calibration_preview(req: CalibrationPreviewRequest, request: Request):
    _require_admin(request)
    # [W6.3] body 带 predictions/outcomes → 用 body(向后兼容);都空 → 读库配对(回写数据到位后自动出建议)
    if req.predictions or req.outcomes:
        suggestion = build_calibration_suggestion(
            scope_key=req.scope_key, predictions=req.predictions, outcomes=req.outcomes,
        )
    else:
        suggestion = await asyncio.to_thread(build_calibration_from_db, req.scope_key)
    return {"status": "success", "suggestion": suggestion, "shadow_only": True}


class OutcomeBackfillRequest(BaseModel):
    since_days: int = Field(30, ge=1, le=180)
    min_age_days: int = Field(30, ge=0, le=365)
    dry_run: bool = True


@router.post("/writing/outcome-backfill")
async def writing_outcome_backfill(req: OutcomeBackfillRequest, request: Request):
    """Retired: strict GEO outcomes only come from preregistered experiments.

    The compatibility route remains so older callers fail closed instead of
    writing a second, weakly linked prediction/outcome truth.
    """
    _require_admin(request)
    raise HTTPException(
        status_code=410,
        detail={
            "code": "legacy_geo_outcome_backfill_retired",
            "message": "请使用预注册 GEO 文体实验与 /writing/outcome-summary 严格结果。",
            "dry_run_requested": bool(req.dry_run),
        },
    )


# ===========================================================================
# [WP12 P2-6 · Master SSOT v2.4 ⑥] 写作有效性两周复核报告(只读)
#
# 各文体 / 各长度档 / 各域 / 各引擎 vs 引用率与引用排名,北极星 = 我方已发布
# URL 被引数(签发当时 1/185)。**只出报告,不自动改配比** —— 本路由没有任何
# 写文体配比的路径,配比调整仍走系统设置的人工入口。
# ===========================================================================
@router.get("/writing/effectiveness-report")
async def writing_effectiveness_report(
    request: Request,
    window_days: int = 90,
    industry: str = "",
    refresh: bool = False,
):
    """读最近一期复核报告;refresh=true 时按当前数据实时重算(不落库)。"""
    _require_admin(request)
    if window_days < 7 or window_days > 720:
        raise HTTPException(status_code=400, detail="window_days 必须 7-720")

    from services.writing_effectiveness_report import (
        REVIEW_INTERVAL_DAYS,
        build_writing_effectiveness_report,
        get_latest_writing_effectiveness_report,
    )

    if refresh:
        report = await asyncio.to_thread(
            build_writing_effectiveness_report,
            window_days=window_days,
            industry=industry.strip(),
        )
        return {
            "status": "success",
            "source": "recomputed",
            "review_interval_days": REVIEW_INTERVAL_DAYS,
            "report": report,
            "auto_ratio_adjustment": False,
        }

    stored = await asyncio.to_thread(get_latest_writing_effectiveness_report)
    if not stored:
        return {
            "status": "success",
            "source": "none",
            "review_interval_days": REVIEW_INTERVAL_DAYS,
            "report": None,
            "auto_ratio_adjustment": False,
            "message": "还没有存档报告。cron 每月 1/16 日 05:40 生成一期，也可以用 refresh=true 立即算一份。",
        }
    return {
        "status": "success",
        "source": "stored",
        "review_interval_days": REVIEW_INTERVAL_DAYS,
        "report_key": stored.get("report_key"),
        "generated_at": stored.get("generated_at"),
        "report": stored.get("payload"),
        "auto_ratio_adjustment": False,
    }
