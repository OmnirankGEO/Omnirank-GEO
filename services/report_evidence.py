"""
P0.2 周报证据锚点抽取(CTO-15.7 2026-04-24)

只读 adapter · 复用现有 monitoring_results · 不建新事实源(Codex 最小变更)

老板批:
 · [2026-06-06 反转·证据区不隐藏] 4 引擎(豆包/通义千问/DeepSeek/Kimi)· DeepSeek/Kimi 无搜索角标但有 AI 回答片段 → 客户看全 4 引擎
 · Evidence A/B/C 3 级
 · 报告 prompt 强制每结论引 1 条 evidence
 · 数据不足时写"本期数据不足 · 不做趋势结论"(不粉饰)

数据源:
 · monitoring_results.platform IN ('doubao', 'dashscope', 'deepseek', 'kimi')
 · monitoring_tasks.brand_id 关联品牌
 · monitoring_results.tested_at 时间窗过滤
"""
from __future__ import annotations
import json
import logging
from typing import Literal, TypedDict, Optional

from services.monitoring_identity_review import aggregate_eligible_sql

logger = logging.getLogger("GEO-ReportEvidence")

EvidenceLevel = Literal["A", "B", "C"]


class EvidenceItem(TypedDict):
    source_type: str          # 'doubao' | 'dashscope' | 'deepseek' | 'kimi'
    source_label: str         # 中文展示(豆包 / 通义千问 / DeepSeek / Kimi)
    tested_at: str            # ISO 时间
    keyword: str              # 查询词
    mention_type: str         # monitoring_results.mention_type
    response_snippet: str     # 截断 200 字
    search_citations: list    # parsed JSON(可能为空数组)
    evidence_level: EvidenceLevel
    weight: float             # 展示排序权重(不进评分)· 豆包0.55/通义0.45/DeepSeek0.40/Kimi0.35


# 平台展示名归一化 · [2026-06-06 老板:证据区不隐藏·客户看全 4 引擎] 补 DeepSeek/Kimi
# [P0-2 · 2026-07-26] 平台名映射改为消费引擎清单单源。
#   旧版少了元宝 → 下面 ``engine not in _PLATFORM_LABEL: continue`` 会把元宝的
#   全部实测**整段丢掉**，客户报告的证据里永远看不到这个平台，还看不出为什么少了。
def _build_platform_labels() -> dict:
    from config.ai_engines import UNIFIED_ENGINES, engine_label

    return {engine: engine_label(engine) for engine in UNIFIED_ENGINES}


_PLATFORM_LABEL = _build_platform_labels()

# 展示排序权重 · 仅决定证据 top max_items 的展示顺序 · 【不进 GEO 评分】(评分 SSOT 在 scoring_levels.py)
# 无原生搜索的表面(DeepSeek/Kimi/元宝)有 AI 回答片段但 citations 更少 → 展示权重略低
_PLATFORM_WEIGHT = {
    "doubao": 0.55,
    "dashscope": 0.45,
    "deepseek": 0.40,
    "kimi": 0.35,
    "yuanbao": 0.30,
}


def _classify_evidence_level(
    mention_type: str,
    response_snippet: str,
    full_response: str,
    brand_name: str,
) -> EvidenceLevel:
    """
    Evidence 3 级(Codex 0424 方案 · 老板批):
    - A: mention_type 精确品牌提及 · 或 snippet/full_response 含品牌名
    - B: 品类/场景提及 · 未点名品牌
    - C: 无明确命中(保留做"数据覆盖"证据 · 不下结论)
    """
    # [P0-3 · 2026-07-26] 词表 SSOT:新旧值都要认。旧库只有 direct，新库写
    #   recommended/mentioned；漏认新值会让新报告的 A 级证据莫名降级成 B。
    from services.mention_vocabulary import BRAND_PRESENT_MENTION_TYPES

    mt = (mention_type or "").strip().lower()
    if mt in BRAND_PRESENT_MENTION_TYPES:
        return "A"

    # 应用层校验:snippet / full_response 是否含品牌名
    if brand_name and brand_name.strip():
        for field in (response_snippet, full_response):
            if field and brand_name in field:
                return "A"

    if mt in ("category_mention", "partial"):
        return "B"

    # 默认 B(有监测响应但未点品牌)· C 留给真的无 evidence 的场景
    if response_snippet or full_response:
        return "B"
    return "C"


def _parse_citations(raw: Optional[str]) -> list:
    """search_citations TEXT → list · 非 JSON 降级 []"""
    if not raw or not raw.strip() or raw.strip() in ("[]", "null"):
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def _truncate(text: Optional[str], limit: int = 200) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def extract_report_evidence(
    brand_id: Optional[int],
    period_start: Optional[str],
    period_end: Optional[str],
    *,
    brand_name: str = "",
    max_items: int = 5,
) -> list[EvidenceItem]:
    """
    抽取周报 evidence 锚点

    Args:
        brand_id: 品牌 ID(必)· 用于 JOIN monitoring_tasks.brand_id
        period_start / period_end: ISO 时间字符串(YYYY-MM-DD 或 timestamp)· 任一空则用 NOW() 回溯
        brand_name: 品牌名(供 A 级 evidence 字面匹配判定)
        max_items: 返回最大条数(老板默认 5)

    Returns:
        list[EvidenceItem]:按 weight × evidence_level 加权排序 · 取 top max_items
        · 无数据返 [] · 调用方应处理"数据不足"降级
    """
    if not brand_id:
        logger.debug("[report_evidence] 未指定 brand_id · 返空")
        return []

    # CTO-15.16 M2 修 P0.2 import bug:`get_db_conn` 不存在于 db.connection
    # 只有 `get_connection` 和 `get_db` · 老代码硬写 get_db_conn 导致 evidence 永远返 [] ·
    # 报告"P0.2 evidence 注入"段落每次都走"本期数据不足"降级 · 这是真 bug
    try:
        from db.connection import get_connection as get_db_conn
    except Exception as e:
        logger.warning(f"[report_evidence] 无法 import get_connection: {e}")
        return []

    # SQL · 4 引擎(豆包/通义千问/DeepSeek/Kimi · 2026-06-06 老板:证据区不隐藏·客户看全 4 引擎)
    sql = f"""
        SELECT
            mr.platform,
            mr.keyword,
            mr.mention_type,
            mr.response_snippet,
            mr.full_response,
            mr.search_citations,
            mr.tested_at
        FROM monitoring_results mr
        JOIN monitoring_tasks mt ON mt.id = mr.task_id
        WHERE mt.brand_id = %s
          AND {aggregate_eligible_sql('mr')}
          AND mr.platform IN ('doubao', 'dashscope', 'deepseek', 'kimi')
          AND (mr.response_snippet IS NOT NULL AND mr.response_snippet != '')
    """
    params: list = [brand_id]

    if period_start:
        sql += " AND mr.tested_at >= %s"
        params.append(period_start)
    if period_end:
        sql += " AND mr.tested_at <= %s"
        params.append(period_end)

    sql += " ORDER BY mr.tested_at DESC LIMIT 200"  # 先取候选 200 · 应用层加权挑 max_items

    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        logger.warning(f"[report_evidence] SQL 异常: {e}")
        return []

    if not rows:
        return []

    # 构造 EvidenceItem 列表
    items: list[EvidenceItem] = []
    for row in rows:
        platform = row["platform"] if not isinstance(row, tuple) else row[0]
        keyword = row["keyword"] if not isinstance(row, tuple) else row[1]
        mention_type = row["mention_type"] if not isinstance(row, tuple) else row[2]
        snippet = row["response_snippet"] if not isinstance(row, tuple) else row[3]
        full_resp = row["full_response"] if not isinstance(row, tuple) else row[4]
        citations = row["search_citations"] if not isinstance(row, tuple) else row[5]
        tested_at = row["tested_at"] if not isinstance(row, tuple) else row[6]

        if platform not in _PLATFORM_WEIGHT:
            continue

        level = _classify_evidence_level(
            mention_type or "",
            snippet or "",
            full_resp or "",
            brand_name,
        )

        items.append({
            "source_type": platform,
            "source_label": _PLATFORM_LABEL[platform],
            "tested_at": str(tested_at) if tested_at else "",
            "keyword": keyword or "",
            "mention_type": mention_type or "none",
            "response_snippet": _truncate(snippet, 200),
            "search_citations": _parse_citations(citations),
            "evidence_level": level,
            "weight": _PLATFORM_WEIGHT[platform],
        })

    # 加权排序:A > B > C · 同级按 weight 降序 · 同 weight 按 tested_at 降序
    level_rank = {"A": 2, "B": 1, "C": 0}
    items.sort(
        key=lambda x: (
            -level_rank[x["evidence_level"]],
            -x["weight"],
            x["tested_at"] or "",
        ),
        reverse=False,  # level_rank 负号 · 升序=最高优先
    )

    # 去重:同 (keyword, platform) 只保 level 最高的一条
    seen: set[tuple[str, str]] = set()
    unique: list[EvidenceItem] = []
    for item in items:
        key = (item["keyword"], item["source_type"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    return unique[:max_items]


def extract_diagnosis_evidence(
    detail_table,
    *,
    brand_name: str = "",
    max_items: int = 12,
) -> list[EvidenceItem]:
    """[Phase 4 2026-06-07] 监测证据为空时的【展示兜底】:从本次诊断 detail_table 构造 evidence。

    与 extract_report_evidence 同形 EvidenceItem · 复用 _classify_evidence_level / _PLATFORM_*
    / _truncate · 同口径加权排序 + (keyword,platform) 去重。
    仅做报告展示兜底 → 不进 GEO 主分 / 不改 brand_detected / 不改检出率 / 不碰扣费·评分·监测执行链路。
    detail_table 缺/非法 → [](绝不抛 · 报告生成降级)。
    """
    items: list[EvidenceItem] = []
    if not isinstance(detail_table, list):
        return items
    for it in detail_table:
        if not isinstance(it, dict):
            continue
        keyword = it.get("question") or ""
        results = it.get("results") or {}
        if not isinstance(results, dict):
            continue
        for engine, res in results.items():
            if engine not in _PLATFORM_LABEL or not isinstance(res, dict):
                continue
            answer = (
                res.get("full_response")
                or res.get("response")
                or res.get("answer_summary")
                or ""
            )
            if not (answer and str(answer).strip()):
                continue  # 无 AI 回答文本 → 不构造(同监测 SQL response_snippet != '' 口径)
            brand_detected = bool(res.get("brand_detected"))
            from services.mention_vocabulary import mention_type_from_outcome

            mention_type = mention_type_from_outcome(
                res.get("target_outcome"), is_detected=brand_detected
            )
            citations = res.get("search_citations") or []
            if not isinstance(citations, list):
                citations = []
            level = _classify_evidence_level(mention_type, str(answer), str(answer), brand_name)
            items.append({
                "source_type": engine,
                "source_label": _PLATFORM_LABEL[engine],
                "tested_at": "",
                "keyword": keyword or "",
                "mention_type": mention_type,
                "response_snippet": _truncate(str(answer), 200),
                "search_citations": citations,
                "evidence_level": level,
                "weight": _PLATFORM_WEIGHT[engine],
            })

    # 加权排序 + 去重 · 与 extract_report_evidence 同口径
    level_rank = {"A": 2, "B": 1, "C": 0}
    items.sort(
        key=lambda x: (-level_rank[x["evidence_level"]], -x["weight"], x["tested_at"] or ""),
        reverse=False,
    )
    seen: set[tuple[str, str]] = set()
    unique: list[EvidenceItem] = []
    for item in items:
        k = (item["keyword"], item["source_type"])
        if k in seen:
            continue
        seen.add(k)
        unique.append(item)
    return unique[:max_items]


def format_evidence_for_prompt(evidences: list[EvidenceItem]) -> str:
    """
    渲染 evidence 列表为 prompt 文本段

    格式:
        ## 证据锚点 · 本期 AI 实测样本(必须每个结论引用至少 1 条)
        1. 【豆包 / A级 / 2026-04-20】Q:"关键词"
           AI 回答摘录:"...片段..."
        2. 【通义千问 / B级 / 2026-04-19】Q:"关键词"
           AI 回答摘录:"..."

    无 evidence 时返"## 证据锚点 · 本期数据不足 · 不得下趋势结论"
    """
    if not evidences:
        return "## 证据锚点 · 本期数据不足\n\n本期未采集到 4 引擎(豆包/通义千问/DeepSeek/Kimi)的有效 AI 回答样本。按写作原则,不得下趋势结论,只能描述当前状态和下一步可验证的具体动作。"

    lines = [
        "## 证据锚点 · 本期 AI 实测样本",
        "",
        "**引用规则**:所有趋势/原因/改善判断必须来自下列 evidence。",
        "- A 级(含品牌名的直接证据)可以下具体结论",
        "- B 级(品类层面的旁证)只能倾向性描述",
        "- 无 A/B 级时不得写趋势判断,只描述当前状态",
        "",
    ]
    for i, ev in enumerate(evidences, 1):
        date_short = (ev.get("tested_at") or "")[:10]
        lines.append(
            f'{i}. 【{ev["source_label"]} / {ev["evidence_level"]}级 / {date_short}】Q:"{ev["keyword"]}"'
        )
        snippet = ev.get("response_snippet") or "(无响应片段)"
        lines.append(f'   AI 回答摘录:"{snippet}"')
        cits = ev.get("search_citations") or []
        if cits:
            lines.append(f"   引用来源数:{len(cits)}")
        lines.append("")
    return "\n".join(lines)
