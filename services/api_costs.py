"""Unified API/LLM cost aggregation.

The admin home dashboard, TV dashboard, and cost monitor should not each invent
their own cost query.  This module keeps the compatibility bridge while older
call sites are being migrated to llm_call_log.

[2026-05-29 老板 P0] caller 归因审计:
  - 所有 LLM 调用入 llm_call_log 时必须设 caller 字符串(non-empty)
  - llm_call_log.caller IS NULL / '' → 在 union SQL fallback 为 'llm_call_log'(generic)
  - 'llm_call_log' / 'legacy_token_usage' 都是 UNKNOWN_CALLERS · 反映未归因调用
  - 复盘 / 报告类未来加 LLM 总结时必须用 KNOWN_CALLERS 中已注册的字符串 ·
    见 KNOWN_CALLERS_FAMILIES / KNOWN_CALLERS_EXACT 注释 +
    tests/test_admin_dashboard_cost_truth_v1_3.py(is_known_caller 真执行覆盖)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Sequence


# [2026-05-29 老板 P0] caller 归因白名单 · 防"unknown"占比扩散
# 新增 LLM 调用点时必须从此列表选一个(或新加一个有意义的 caller 字符串)
#
# 命名约定:
#   <module>_<action>  · 例如:monitoring_insights / article_writing / keyword_seed
#   monitoring_*       · 监测相关(insights / platform_mau / report_review 等)
#   article_*          · 文章生成
#   keyword_*          · 关键词生成 / 聚类 / 扩展
#   research_*         · 研究类(资料抓取 / 平台分析)
#   social_*           · 社媒模块(dynamic_review 等)
#   rewrite_*          · 改写类(douyin / xhs / bilibili)
#   knowledge_*        · 知识库
#   intake_*           · 引导填写 AI
#   advisor_*          · advisor RAG
KNOWN_CALLERS_FAMILIES = (
    "monitoring_",         # monitoring_insights / monitoring_platform_mau / monitoring_report_review(future)
    "article_",            # article_writing
    "keyword_",            # keyword_seed / keyword_cluster_*
    "research_",           # research_resolve_* / research_xhs_* / research_center_*
    "rewrite_",            # rewrite_douyin_* / rewrite_xhs_* / rewrite_bilibili_*
    "social_",             # social_dynamic_review
    "tikhub_",             # tikhub_* 系列
    "knowledge_",          # knowledge_cleaning
    "intake_",             # intake_ai_draft
    "advisor_",            # advisor_*
    "asr_",                # asr_transcription / asr_video_comments
    "geo_managed_",        # geo_managed_monitoring
    "industry_",           # industry_case_collector
    "aliyun_",             # aliyun_ocr
    "placement_",          # placement_competitor_audit(v1.3 补 · 原漏)
    "competitor_",         # competitor_research(v1.3 补)
)

# [v1.3 2026-05-29 老板复审 P1-4 实证补全]
# 裸 caller(无族前缀)精确白名单 · grep 全库 caller= 实证后补
# 根因:严格前缀匹配漏判裸名 · 尤其 'monitoring'(最高频自动监测 caller · 不带下划线)
#   → 被误判 unknown → unknown_caller_ratio 被合法成本占满(接 UI 当天误导老板)
KNOWN_CALLERS_EXACT = frozenset({
    "monitoring",          # 自动监测主 caller(scheduler / batch_monitor · 最高频)
    "autofill",            # server.py 品牌自动填充
    "ai_visibility",       # 监测可见度(union SQL 特殊归并 'ai_visibility')
    "competitor_research", # server.py 竞品研究
})

# UNKNOWN_CALLERS · 反映归因缺失 · API cost summary 应单独 surface 这些占比
UNKNOWN_CALLERS = (
    "llm_call_log",        # llm_call_log.caller IS NULL / '' fallback
    "legacy_token_usage",  # 旧 token_usage 表 fallback
)


def is_known_caller(caller: str) -> bool:
    """v1.1/v1.3 caller 归因审计 · 用于 cost summary 区分 known vs unknown

    [v1.1 P1-4 收紧] 移除 `or "_" in caller` · 严格按族前缀 · 拒绝 UNKNOWN_CALLERS / 空
    [v1.3 P1-4 实证补全] 加 KNOWN_CALLERS_EXACT 裸名白名单 · 补 placement_/competitor_ 族
      根因:严格前缀漏判裸 caller(monitoring/autofill/...)· monitoring 是最高频 ·
            漏判会让 unknown_caller_ratio 被合法成本占满 · 误导
    新加 caller 必须在 KNOWN_CALLERS_FAMILIES 注册一个族 · 或裸名进 KNOWN_CALLERS_EXACT
    """
    if not caller:
        return False
    if caller in UNKNOWN_CALLERS:
        return False
    if caller in KNOWN_CALLERS_EXACT:
        return True
    return any(caller.startswith(fam) for fam in KNOWN_CALLERS_FAMILIES)


def build_api_cost_union_sql() -> str:
    """Return the source union used for API cost reports.

    Parameters expected by the returned SQL:
      1. llm_call_log since
      2. token_usage since
      3. token_usage duplicate-check since
      4. monitoring_token_usage since
    """
    return """
        SELECT
            created_at,
            COALESCE(NULLIF(caller, ''), 'llm_call_log') AS caller,
            COALESCE(NULLIF(platform, ''), 'unknown') AS platform,
            COALESCE(NULLIF(model, ''), COALESCE(NULLIF(platform, ''), 'unknown')) AS model_label,
            COALESCE(input_tokens, 0) AS input_tokens,
            COALESCE(output_tokens, 0) AS output_tokens,
            COALESCE(duration_ms, 0) AS duration_ms,
            COALESCE(estimated_cost, 0) AS estimated_cost,
            COALESCE(success, TRUE) AS success,
            'llm_call_log' AS source_table
        FROM llm_call_log
        WHERE created_at >= %s

        UNION ALL

        SELECT
            tu.created_at,
            COALESCE(NULLIF(tu.operation_type, ''), 'legacy_token_usage') AS caller,
            CASE
                WHEN tu.model_name ILIKE 'kimi%%' THEN 'kimi'
                WHEN tu.model_name ILIKE 'doubao%%' THEN 'doubao'
                WHEN tu.model_name ILIKE 'qwen%%' THEN 'dashscope'
                WHEN tu.model_name ILIKE 'deepseek-v4%%' THEN 'dashscope'
                WHEN tu.model_name ILIKE 'deepseek%%' THEN 'deepseek'
                WHEN tu.model_name ILIKE 'anthropic/%%' THEN 'openrouter'
                WHEN tu.model_name ILIKE 'google/%%' THEN 'openrouter'
                WHEN tu.model_name ILIKE 'openai/%%' THEN 'openrouter'
                ELSE 'legacy_token_usage'
            END AS platform,
            COALESCE(NULLIF(tu.model_name, ''), 'unknown') AS model_label,
            COALESCE(tu.input_tokens, 0) AS input_tokens,
            COALESCE(tu.output_tokens, 0) AS output_tokens,
            0 AS duration_ms,
            COALESCE(tu.estimated_cost, 0) AS estimated_cost,
            TRUE AS success,
            'token_usage' AS source_table
        FROM token_usage tu
        WHERE tu.created_at >= %s
          AND NOT EXISTS (
              SELECT 1
              FROM llm_call_log llm
              WHERE llm.created_at >= %s
                AND llm.created_at BETWEEN tu.created_at - INTERVAL '2 seconds'
                                       AND tu.created_at + INTERVAL '2 seconds'
                AND COALESCE(NULLIF(llm.model, ''), '') = COALESCE(NULLIF(tu.model_name, ''), '')
                AND COALESCE(llm.input_tokens, 0) = COALESCE(tu.input_tokens, 0)
                AND COALESCE(llm.output_tokens, 0) = COALESCE(tu.output_tokens, 0)
          )

        UNION ALL

        SELECT
            mtu.created_at,
            'monitoring' AS caller,
            COALESCE(NULLIF(mtu.platform, ''), 'monitoring') AS platform,
            COALESCE(NULLIF(mtu.platform, ''), 'monitoring') AS model_label,
            COALESCE(mtu.input_tokens, 0) AS input_tokens,
            COALESCE(mtu.output_tokens, 0) AS output_tokens,
            0 AS duration_ms,
            COALESCE(mtu.estimated_cost, 0) AS estimated_cost,
            TRUE AS success,
            'monitoring_token_usage' AS source_table
        FROM monitoring_token_usage mtu
        WHERE mtu.created_at >= %s
          AND NOT EXISTS (
              SELECT 1
              FROM llm_call_log llm
              WHERE llm.created_at BETWEEN mtu.created_at - INTERVAL '30 seconds'
                                       AND mtu.created_at + INTERVAL '30 seconds'
                AND (
                    llm.platform = mtu.platform
                    OR COALESCE(llm.metadata->>'monitoring_platform', '') = mtu.platform
                    OR (
                        mtu.platform = 'deepseek'
                        AND llm.platform = 'dashscope'
                        AND COALESCE(llm.model, '') ILIKE 'deepseek%%'
                    )
                )
                AND (
                    (
                        llm.caller = 'monitoring'
                        AND COALESCE(llm.metadata->>'task_id', '') = mtu.task_id::text
                    )
                    OR llm.caller IN ('monitoring', 'ai_visibility')
                )
          )
    """


def _union_params(since: datetime) -> Sequence[Any]:
    return (since, since, since, since)


def _until_clause(until: datetime | None) -> tuple[str, tuple[Any, ...]]:
    if until is None:
        return "", ()
    return "WHERE created_at < %s", (until,)


def get_api_cost_summary(
    cur,
    since: datetime,
    *,
    until: datetime | None = None,
    limit: int = 15,
) -> Dict[str, Any]:
    union_sql = build_api_cost_union_sql()
    params = _union_params(since)
    until_sql, until_params = _until_clause(until)

    cur.execute(
        f"""
        SELECT
            COALESCE(SUM(estimated_cost), 0) AS total_cost,
            COUNT(*) AS total_calls,
            COALESCE(AVG(estimated_cost), 0) AS avg_cost,
            COUNT(*) FILTER (WHERE success = FALSE) AS failed_calls,
            COALESCE(AVG(NULLIF(duration_ms, 0)), 0) AS avg_duration_ms
        FROM ({union_sql}) api_cost_rows
        {until_sql}
        """,
        (*params, *until_params),
    )
    total_row = cur.fetchone() or {}
    total_cost = float(total_row.get("total_cost") or 0)
    total_calls = int(total_row.get("total_calls") or 0)

    cur.execute(
        f"""
        SELECT
            platform,
            model_label,
            COALESCE(SUM(estimated_cost), 0) AS cost,
            COUNT(*) AS calls,
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens
        FROM ({union_sql}) api_cost_rows
        {until_sql}
        GROUP BY platform, model_label
        ORDER BY cost DESC, calls DESC
        LIMIT %s
        """,
        (*params, *until_params, limit),
    )
    items: List[Dict[str, Any]] = [
        {
            "model": r["model_label"],
            "platform": r["platform"],
            "cost_yuan": round(float(r.get("cost") or 0), 4),
            "calls": int(r.get("calls") or 0),
            "input_tokens": int(r.get("input_tokens") or 0),
            "output_tokens": int(r.get("output_tokens") or 0),
        }
        for r in cur.fetchall()
    ]

    # [v1.1 2026-05-29 老板复审 P1-4] 归因审计 · 真按 Python is_known_caller 分类
    # 旧版 SQL `WHERE caller IN ('llm_call_log','legacy_token_usage')` 只算 fallback name ·
    #   没用 is_known_caller helper · 凡是 caller 非空都被当 known → 'random_new_feature' 漏网
    # 新版:按 caller GROUP BY 后 Python 端用 is_known_caller 分类
    cur.execute(
        f"""
        SELECT
            caller,
            COALESCE(SUM(estimated_cost), 0) AS cost,
            COUNT(*) AS calls
        FROM ({union_sql}) api_cost_rows
        {until_sql}
        GROUP BY caller
        """,
        (*params, *until_params),
    )
    unknown_cost = 0.0
    unknown_calls = 0
    for r in cur.fetchall():
        if not is_known_caller(r.get("caller") or ""):
            unknown_cost += float(r.get("cost") or 0)
            unknown_calls += int(r.get("calls") or 0)

    return {
        "total_cost": round(total_cost, 4),
        "total_calls": total_calls,
        "avg_cost": round(float(total_row.get("avg_cost") or 0), 6),
        "failed_calls": int(total_row.get("failed_calls") or 0),
        "avg_duration_ms": int(float(total_row.get("avg_duration_ms") or 0)),
        "items": items,
        # v1.1 归因审计 · 让 dashboard 可见还有多少 cost / calls 没归因
        # 现在 'foo_bar' / 'random_new_feature' 类乱命名 caller 也会进 unknown
        "unknown_caller_cost": round(unknown_cost, 4),
        "unknown_caller_calls": unknown_calls,
        "unknown_caller_ratio": round(unknown_cost / total_cost, 4) if total_cost > 0 else 0,
    }


def get_api_cost_by_caller(cur, since: datetime, *, until: datetime | None = None) -> List[Dict[str, Any]]:
    union_sql = build_api_cost_union_sql()
    until_sql, until_params = _until_clause(until)
    cur.execute(
        f"""
        SELECT
            caller,
            COUNT(*) AS calls,
            COALESCE(SUM(estimated_cost), 0) AS cost,
            COALESCE(AVG(estimated_cost), 0) AS avg_cost,
            COUNT(*) FILTER (WHERE success = FALSE) AS failed
        FROM ({union_sql}) api_cost_rows
        {until_sql}
        GROUP BY caller
        ORDER BY cost DESC, calls DESC
        """,
        (*_union_params(since), *until_params),
    )
    return [dict(r) for r in cur.fetchall()]


def get_api_cost_by_platform(cur, since: datetime, *, until: datetime | None = None) -> List[Dict[str, Any]]:
    union_sql = build_api_cost_union_sql()
    until_sql, until_params = _until_clause(until)
    cur.execute(
        f"""
        SELECT
            platform,
            COUNT(*) AS calls,
            COALESCE(SUM(estimated_cost), 0) AS cost,
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens,
            COALESCE(AVG(NULLIF(duration_ms, 0)), 0) AS avg_duration_ms,
            COUNT(*) FILTER (WHERE success = FALSE) AS failed
        FROM ({union_sql}) api_cost_rows
        {until_sql}
        GROUP BY platform
        ORDER BY cost DESC, calls DESC
        """,
        (*_union_params(since), *until_params),
    )
    return [dict(r) for r in cur.fetchall()]


def get_api_cost_timeline(cur, since: datetime, *, granularity: str = "day") -> List[Dict[str, Any]]:
    if granularity not in ("hour", "day"):
        raise ValueError("granularity must be hour or day")

    union_sql = build_api_cost_union_sql()
    cur.execute(
        f"""
        SELECT
            DATE_TRUNC(%s, created_at) AS bucket,
            platform,
            COUNT(*) AS calls,
            COALESCE(SUM(estimated_cost), 0) AS cost
        FROM ({union_sql}) api_cost_rows
        GROUP BY bucket, platform
        ORDER BY bucket
        """,
        (granularity, *_union_params(since)),
    )
    return [dict(r) for r in cur.fetchall()]


def get_api_cost_daily(cur, since: datetime) -> Dict[str, float]:
    union_sql = build_api_cost_union_sql()
    cur.execute(
        f"""
        SELECT DATE(created_at) AS date, COALESCE(SUM(estimated_cost), 0) AS cost
        FROM ({union_sql}) api_cost_rows
        GROUP BY DATE(created_at)
        """,
        _union_params(since),
    )
    return {str(r["date"]): round(float(r.get("cost") or 0), 2) for r in cur.fetchall()}


def get_api_call_count(cur, since: datetime) -> int:
    union_sql = build_api_cost_union_sql()
    cur.execute(
        f"SELECT COUNT(*) AS calls FROM ({union_sql}) api_cost_rows",
        _union_params(since),
    )
    row = cur.fetchone() or {}
    return int(row.get("calls") or 0)
