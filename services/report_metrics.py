"""
report_metrics — M2 报告 2.0 · 评分 + 完整度 + 关键词分层 SSOT

CTO-B 2026-04-26 W1 · feat/m2-diagnosis-report-v2-full

定位(老板决策点 + 元指令 14):
  · 报告所有指标的唯一计算入口 · 禁止 12/30 vs 15/32 满分制混用
  · 关键词分层(品牌词 / 本地获客词 / 高转化场景词)单一数据源
  · 输入完整度作为一等公民 · 不再事后估算

数据源(reconcile 基线已存在):
  - tools/scoring/geo_scope_scorer.GEO_SCOPE_DIMENSIONS  · 5 维度 100 制
  - diagnosis 主流程产出 · ai_visibility_data.dimension_stats
    (super_tier1=场景词 / regional_industry=本地词 / brand_awareness=品牌词)
  - tools/keyword_intent_classifier  · 用于 info/noise 排除(不主导分层)
  - tools/scoring/scoring_levels  · 等级 SSOT(领先/成熟/成长/起步/待提升/空白)

使用:
  from services.report_metrics import (
      get_dimension_breakdown,
      get_keyword_strata_rates,
      compute_data_completeness,
      DIMENSIONS_100,
  )
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from tools.scoring.geo_scope_scorer import GEO_SCOPE_DIMENSIONS, GEO_SCOPE_MAX_SCORE
from tools.scoring.scoring_levels import get_meta as get_level_meta

logger = logging.getLogger("GEO-ReportMetrics")


# ============================================================================
# 100 制评分 SSOT(禁止任何报告路径再用 80/120 等其他满分基)
# ============================================================================

# 5 维度 · 来自 GEO_SCOPE_DIMENSIONS · 总和 100
DIMENSIONS_100: list[dict[str, Any]] = [
    {
        "key": "ai_recommendation_score",
        "label": GEO_SCOPE_DIMENSIONS["ai_recommendation_score"]["name"],
        "max": GEO_SCOPE_DIMENSIONS["ai_recommendation_score"]["max"],   # 30
        "icon": GEO_SCOPE_DIMENSIONS["ai_recommendation_score"]["icon"],
        "explanation": "AI 引擎(豆包/通义/DeepSeek/Kimi)实测推荐次数",
    },
    {
        "key": "web_content_score",
        "label": GEO_SCOPE_DIMENSIONS["web_content_score"]["name"],
        "max": GEO_SCOPE_DIMENSIONS["web_content_score"]["max"],          # 25
        "icon": GEO_SCOPE_DIMENSIONS["web_content_score"]["icon"],
        "explanation": "网页搜索中品牌直接引用数量(W1 修:不再用泛搜索 result_count 兜底)",
    },
    {
        "key": "authority_score",
        "label": GEO_SCOPE_DIMENSIONS["authority_score"]["name"],
        "max": GEO_SCOPE_DIMENSIONS["authority_score"]["max"],            # 20
        "icon": GEO_SCOPE_DIMENSIONS["authority_score"]["icon"],
        "explanation": "权威媒体/百科/政企域名引用次数",
    },
    {
        "key": "structured_content_score",
        "label": GEO_SCOPE_DIMENSIONS["structured_content_score"]["name"],
        "max": GEO_SCOPE_DIMENSIONS["structured_content_score"]["max"],   # 15
        "icon": GEO_SCOPE_DIMENSIONS["structured_content_score"]["icon"],
        "explanation": "FAQ / 长文 / 知识图谱完整度",
    },
    {
        "key": "brand_foundation_score",
        "label": GEO_SCOPE_DIMENSIONS["brand_foundation_score"]["name"],
        "max": GEO_SCOPE_DIMENSIONS["brand_foundation_score"]["max"],     # 10
        "icon": GEO_SCOPE_DIMENSIONS["brand_foundation_score"]["icon"],
        "explanation": "品牌词占位 + 信息准确度",
    },
]
assert sum(d["max"] for d in DIMENSIONS_100) == GEO_SCOPE_MAX_SCORE == 100, \
    "DIMENSIONS_100 总分必须 100 · SSOT 校验失败"


def get_dimension_breakdown(scores: dict[str, Any] | None) -> list[dict[str, Any]]:
    """返回 5 维度的 score / max / 百分比 / 评级标记 · 用于报告 Module 2 雷达 + 详情表

    Args:
        scores: diagnosis_records 表行 dict · 含各维度分数字段

    Returns:
        [{key, label, score, max, percent, status, explanation, icon}, ...]
        score=None 表示该维度缺数据 · 报告不显示百分比 · 必须明确标注"数据不足"
    """
    out: list[dict[str, Any]] = []
    if not scores:
        scores = {}
    for d in DIMENSIONS_100:
        raw = scores.get(d["key"])
        try:
            score = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            score = None
        # status: 缺数据 / 良好 / 一般 / 待提升 / 空白
        if score is None:
            status = "缺数据"
            percent = None
        else:
            percent = round(100 * score / d["max"], 1) if d["max"] > 0 else 0
            if percent >= 80:
                status = "良好"
            elif percent >= 60:
                status = "一般"
            elif percent >= 30:
                status = "待提升"
            else:
                status = "空白"
        out.append({
            "key": d["key"],
            "label": d["label"],
            "icon": d["icon"],
            "score": score,
            "max": d["max"],
            "percent": percent,
            "status": status,
            "explanation": d["explanation"],
        })
    return out


def get_total_score_and_level(scores: dict[str, Any] | None) -> dict[str, Any]:
    """汇总 5 维度 → 100 制总分 + 等级 SSOT

    缺维度时该维度按 0 算(老板红线:不假装数据完整 · 报告会显示"X 维缺数据")
    """
    breakdown = get_dimension_breakdown(scores)
    total = sum((b["score"] or 0) for b in breakdown)
    missing = [b["label"] for b in breakdown if b["score"] is None]
    meta = get_level_meta(total)
    return {
        "total_score": total,
        "max_score": 100,
        "level": meta["level"],
        "level_meta": meta,
        "missing_dimensions": missing,
        "has_missing": bool(missing),
    }


# ============================================================================
# 关键词分层(决策点 1:用现有 dimension_stats + intent_classifier)
# ============================================================================

# 老板决策 1 的层级映射(rule-based · 不依赖 LLM 分类器主导)
KEYWORD_STRATA_LAYERS = [
    {
        "key": "brand",
        "label": "品牌认知词",
        "explanation": "包含品牌名 / 公司全称 / 导航类查询",
        "stats_key": "brand_awareness",     # AI 测试已分桶字段
    },
    {
        "key": "local",
        "label": "本地获客词",
        "explanation": "包含城市 / 区县 / 地域 / near-me 类查询",
        "stats_key": "regional_industry",
    },
    {
        "key": "scenario",
        "label": "高转化场景词",
        "explanation": "场景化 / 服务类 / 痛点 / 转化决策类查询",
        "stats_key": "super_tier1",
    },
]


def get_keyword_strata_rates(diagnosis_data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """从 ai_visibility_data.dimension_stats 拆 3 层关键词推荐率(决策点 1)

    数据源真实性:dimension_stats 是 diagnosis 已经按 super_tier1 / regional_industry /
    brand_awareness 三类跑过 4 引擎实测后的真实分组,直接拆 3 层不再二次推断。

    每层返回:
      {key, label, total, detected, rate(0-1), rate_text(描述化),
       confidence(high/medium/low), data_sufficient(bool)}

    data_sufficient=False 的层 · 报告必须显式写"数据不足 N 个测试 · 不下结论"
    (老板红线 · 决策点 5)
    """
    if not diagnosis_data:
        diagnosis_data = {}
    ai_data = diagnosis_data.get("ai_visibility_data") or {}
    dim_stats = ai_data.get("dimension_stats") or {}

    out: list[dict[str, Any]] = []
    for layer in KEYWORD_STRATA_LAYERS:
        st = dim_stats.get(layer["stats_key"]) or {}
        total = int(st.get("total", 0) or 0)
        detected = int(st.get("detected", 0) or 0)
        rate = (detected / total) if total > 0 else 0.0
        # 置信度:测试样本数决定 · 老板红线:< 5 必须标"数据不足"
        if total >= 15:
            conf = "high"
            sufficient = True
        elif total >= 5:
            conf = "medium"
            sufficient = True
        else:
            conf = "low"
            sufficient = False

        # rate_text · 复用 transparent_pricing 翻译话术(走 describe_probability 等价)
        if not sufficient:
            rate_text = f"数据不足({total} 次测试)"
        elif rate >= 0.8:
            rate_text = "几乎每次都出现"
        elif rate >= 0.5:
            rate_text = f"超过半数(每 2 次约 {round(rate * 2)} 次出现)"
        elif rate >= 0.3:
            rate_text = f"约 1/3(每 3 次约 {max(1, round(rate * 3))} 次出现)"
        elif rate >= 0.1:
            rate_text = f"零星出现(每 10 次约 {max(1, round(rate * 10))} 次)"
        else:
            rate_text = "几乎不出现"

        out.append({
            "key": layer["key"],
            "label": layer["label"],
            "explanation": layer["explanation"],
            "total": total,
            "detected": detected,
            "rate": round(rate, 4),
            "rate_pct": round(rate * 100, 1),
            "rate_text": rate_text,
            "confidence": conf,
            "data_sufficient": sufficient,
        })
    return out


def classify_keyword_to_stratum(
    keyword: str,
    brand_name: str = "",
    city: str = "",
    cities: list[str] | str | None = None,
    industry: str = "",
) -> str:
    """单关键词 → 层级映射(W3 升级 · 用于报告内列表展示 · 不主导评分)

    CTO-B 2026-04-26 W3 增强:
      旧:仅匹配 brand_name + 单 city · "璧山装修"在 city='重庆' 时漏判为 scenario
      新:支持 brands.cities 数组(多城市) + 区县/地域子串库
        老板验收硬要求:重庆璧山样本要正确归到本地获客词

    决策点 1 映射:
      - 含品牌名 / brand / company / 官网 → 品牌词
      - 含城市 / 区县 / 地域 / near-me / 附近 → 本地获客词
      - 默认 → 高转化场景词
    info 类(怎么/什么是)由 keyword_intent_classifier 异步打标 · 不在此函数

    Args:
        keyword: 关键词
        brand_name: 品牌名(命中 = 品牌层)
        city: brand.city 单值
        cities: brand.cities 字符串"重庆,璧山"或 list ["重庆","璧山"]
        industry: 行业 · 用于场景层增强(暂未用)

    Returns: "brand" / "local" / "scenario"
    """
    kw = (keyword or "").strip().lower()
    if not kw:
        return "scenario"
    bn = (brand_name or "").strip().lower()

    # 1) 品牌层(命中即返)
    if bn and bn in kw:
        return "brand"
    for marker in ("官网", "官方", "company", "brand", "navigation"):
        if marker in kw:
            return "brand"

    # 2) 本地层 · 扩展城市/区县词库
    location_terms: set[str] = set()
    if city and city.strip():
        location_terms.add(city.strip().lower())
    # cities 字段(多城市/区县列表)
    if cities:
        if isinstance(cities, str):
            for part in cities.replace("、", ",").replace(";", ",").split(","):
                p = part.strip().lower()
                if p:
                    location_terms.add(p)
        elif isinstance(cities, list):
            for c in cities:
                if isinstance(c, str) and c.strip():
                    location_terms.add(c.strip().lower())

    # 命中任一具体地名(如 重庆 / 璧山 / 渝北)
    for term in location_terms:
        if term and term in kw:
            return "local"

    # 通用地域词(辅助)· 长度 ≥ 3 防"市/县"单字误伤
    if len(kw) >= 3:
        for marker in ("附近", "near", "本地", "周边", "市", "县", "区", "省", "镇", "街道"):
            if marker in kw:
                return "local"

    # 3) 默认场景层
    return "scenario"


# ============================================================================
# 输入完整度(决策点 5 · 老板红线:不假装数据完整)
# ============================================================================

# 完整度评估的 4 大组(每组 25 分 · 总 100)
COMPLETENESS_GROUPS = [
    {
        "key": "brand_basic",
        "label": "品牌基础信息",
        "weight": 25,
        "fields": [
            ("brand_name", "品牌名称", 6),
            ("industry", "行业/品类", 6),
            ("city", "主营城市", 4),
            ("business_type", "B2B/B2C 类型", 3),
            ("city_scope", "服务区域(本地/全国)", 3),
            ("description", "品牌描述", 3),
        ],
    },
    {
        "key": "industry_brief",
        "label": "行业深度资料",
        "weight": 25,
        "fields": [
            ("industry_brief.service_scope", "服务范围画像", 5),
            ("industry_brief.target_users", "目标客户画像", 5),
            ("industry_brief.my_differentiation", "差异化卖点", 5),
            ("industry_brief.local_competitors", "本地竞品", 4),
            ("industry_brief.case_evidence", "成功案例", 3),
            ("industry_brief.user_voices", "用户原声", 3),
        ],
    },
    {
        "key": "keywords",
        "label": "关键词覆盖",
        "weight": 25,
        "fields": [
            ("keywords_count_brand", "品牌词 ≥ 1 个", 5),
            ("keywords_count_local", "本地词 ≥ 3 个", 8),
            ("keywords_count_scenario", "场景词 ≥ 5 个", 12),
        ],
    },
    {
        "key": "evidence_inputs",
        "label": "证据/竞品输入",
        "weight": 25,
        "fields": [
            ("competitors_count", "建档竞品 ≥ 3 个", 8),
            ("ai_test_total", "AI 测试样本 ≥ 15 次", 10),
            ("publications_count", "已发布证据 ≥ 1 条", 4),
            ("web_brand_direct_count", "网页品牌直引 ≥ 1 条", 3),
        ],
    },
]


def _get_nested(d: dict, dotted_key: str, default=None):
    """读 'industry_brief.service_scope' 这种点路径 · 缺路径返 default"""
    cur: Any = d or {}
    for part in dotted_key.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
        if cur is None:
            return default
    return cur


def _is_filled(value: Any, threshold: int = 1) -> bool:
    """判断字段是否已填(空字符串/0/空列表/None 都视为未填)"""
    if value is None:
        return False
    if isinstance(value, (list, dict)):
        return len(value) >= threshold
    if isinstance(value, (int, float)):
        return value >= threshold
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


#: 🔴 [WO_REPORT_METRICS_QUOTE_KEYS 2026-09-03] **读 `quotes` 的唯一出口。**
#:
#: 本文件曾经在三个地方直接 `quote.get(...)`,其中两个键在 `quotes` 表上**根本不存在**
#: (`keywords` / `publications_count`;真实列是 `total_keywords` / `total_articles`),
#: 第三个 `competitor_list` 存在但**是 JSON 文本**,却被 `len()` 当序列数。
#:
#: 三处是**同一个形状**:每一处都有一个"看起来合理"的兜底,把错误吸收掉了 ——
#: `or []` / `int(... or 0)` / `len(str)` 都不报错、不告警、结果都落在合理量级里,
#: 所以它们能活这么久。**验收判据因此不能只是"改完不报错"** —— 那正是它们改之前的状态,
#: 判据必须打在**值本身**上(见 tests/report_metrics_quote_keys_2026_09_03/)。
#:
#: 集中成函数是为了单一来源:下次再有人要读 quote,只会看到这里的列名。


#: 退出分母时给她看的那句话。**不是错误提示** —— 这一格还没到该有的时候。
#: 🔴 与「缺失」分开写:说成缺失,她会去找自己漏填了什么,而根本没有可填的东西。
DEFERRED_FIELD_NOTE: dict[str, str] = {
    # 「还轮不到统计」—— 这次诊断名下还没有报价单。
    ("publications_count", "no_quote"): "报价后开始统计",
}

#: 🔴 「我们没查到」与「还没到该有的时候」是**两件事**。
#:    压成同一句会让她以为系统知道答案(知道是 0),而其实我们不知道。
DEFERRED_FIELD_NOTE_UNAVAILABLE = "这项暂时统计不到,不影响本次评分"


#: 🔴 [#54/#55 2026-09-04] 「已发布证据」**不再从 quote 取任何东西**。
#:
#: 曾经读 `quotes.total_articles` —— 那是**报价里承诺的篇数**,不是「已发布」;
#: 标签写的却是「已发布证据 ≥ 1 条」。语义错配,而且该列由主力选词链路建单时
#: 硬编码为 0 且全流程从不回写 ⇒ 这一格实际恒缺失。
#:
#: 现在的口径(Review 2026-09-04 裁定):
#:   事实源 = `media_publications` 行存在(该表没有 status 列,存在即已发布);
#:   `articles.first_published_at` 只是 denormalized 加速列(schema 注释原文:
#:   「事实源 media_publications · 仅查询加速用」),**不作真值**;
#:   归属 = 该品牌**全部**报价单汇总(quotes.brand_id → quote_id),
#:   不是「品牌最新一张 LIMIT 1」。
#:
#: 值由**调用方查好传入**(本函数是纯函数,不查库),见
#: `services/diagnosis_identity_decision.py` 的 `published_count`。


def _competitors_count(brand: dict, quote: dict) -> int:
    """竞品条数:优先品牌档案的 `competitors_jsonb`,回落到报价的 `competitor_list`。

    🔴 `quotes.competitor_list` 是 **text**,内容是 JSON 数组的字符串
       (与 `services/client_knowledge.py:296` 同口径,那边一直是 `json.loads` 之后再用)。
       本文件曾经直接 `len()` 它 —— 数的是**字符数**:
       Deploy 实测非空样本平均 3219.8 字符 ⇒ 竞品数会变成三四千,**虚高约两个数量级**。
       今天不发作只是因为 `competitors_jsonb` 优先且几乎总是非空;
       **它是埋着的雷,不是正在冒烟的火** —— 哪天有品牌档案空而报价有竞品就会炸。
    """
    items: Any = brand.get("competitors_jsonb")
    if not (isinstance(items, list) and items):
        raw = quote.get("competitor_list")
        if isinstance(raw, str):
            try:
                items = json.loads(raw) if raw.strip() else []
            except (TypeError, ValueError):
                # 解析不了就当没有 —— 但**不要**退回 len(str),那正是原来的 bug。
                items = []
        else:
            items = raw
    return len(items) if isinstance(items, list) else 0


def compute_data_completeness(
    *,
    brand: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
    diagnosis: dict[str, Any] | None = None,
    quote: dict[str, Any] | None = None,
    # 🔴 [#54/#55] **required keyword-only,故意不给默认值。**
    #    `assemble_diagnosis_report_v2` 有 5 个调用点;给了默认值,漏改的那个会
    #    **安静地**让这一项退出分母 ⇒ 分数被拉高、零报错、零日志。
    #    没有默认值 ⇒ 漏改当场 TypeError。**用一条红换掉一个不会提问的绿。**
    #    形态直接复用既有契约 `StageValue.as_dict()`:{count, available, reason}
    #    (services/publication_stage_projection.py),所以
    #    `load_quote_projection(...)["stages"]["published_active"]` 可以零转换传进来。
    published: dict[str, Any],
) -> dict[str, Any]:
    """计算"本次诊断输入完整度" 0-100 · 报告头部展示

    决策点 5(老板红线):缺数据明确写出 · 不假装。

    输入(任一允许 None):
      brand: brands 表行 · {industry, city, business_type, city_scope, description, ...}
      profile: client_profiles 表行 · {industry_brief: {...}}
      diagnosis: diagnosis_records 表行 · {ai_total_tests, web_brand_direct_count, ...}
      quote: 最近一次报价 · {competitors_count, keywords...}

    Returns:
      {
        "score": 0-100,
        "level": "充足" / "可用" / "勉强" / "严重不足",
        "groups": [{key, label, weight, score, status, missing_fields, ...}],
        "missing_summary": "缺 X 项关键输入 · 影响 Y 个判断",
        "impact_notes": ["缺 industry_brief 会导致行业机会估算降级", ...],
      }
    """
    brand = brand or {}
    profile = profile or {}
    diagnosis = diagnosis or {}
    quote = quote or {}

    # 拼大 dict 供 _get_nested 查
    merged: dict[str, Any] = {}
    merged.update({k: v for k, v in brand.items() if k != "industry_brief"})
    merged["industry_brief"] = profile.get("industry_brief") or {}

    # F6 P2-2 修复(Phase B · CTO-15.10 · 2026-04-27):字段名 alias
    # brands 表实际列是 name/cities/notes · 但 COMPLETENESS_GROUPS 评估器查
    # brand_name/city/description · 老 v3 算法字段名错位 → 报告永远说"缺品牌名称/
    # 主营城市/品牌描述"。加 alias 让两套字段名都生效。
    merged["brand_name"] = (
        brand.get("name") or brand.get("brand_name") or ""
    )
    merged["city"] = (
        brand.get("city") or brand.get("cities") or ""
    )
    merged["description"] = (
        brand.get("description")
        or brand.get("notes")
        or profile.get("business")
        or ""
    )

    # 关键词数(从 diagnosis.keywords 拆)
    #
    # 🔴 [WO_REPORT_METRICS_QUOTE_KEYS 2026-09-03] 这里**曾经**先读 `quote.get("keywords")`,
    #    而 `quotes` 表**没有 keywords 这一列**(Deploy 生产库 `\d quotes` 实核)
    #    ⇒ 那一截从来没生效过,`or` 把它悄悄吞掉,恒走 diagnosis 兜底。
    #
    # 🔴 **不要把它改名成 `total_keywords`。** 那是 `integer`(计数),不是列表;
    #    而下面的分层循环被 `isinstance(raw_keywords, list)` 包着 ——
    #    整数进来会被**静默跳过**,三个计数仍然全 0。
    #    改名只会让键名看起来合理,把"明显不工作"变成"看起来在工作",更难被发现。
    #    (关键词列表的真身在 `confirmed_keywords` 表;接入 = 卡 #47,会改动对客完整度分数,
    #     属产品决定,本单不做。)
    kw_brand_n = 0
    kw_local_n = 0
    kw_scenario_n = 0
    raw_keywords = diagnosis.get("keywords") or []
    if isinstance(raw_keywords, str):
        raw_keywords = [k.strip() for k in raw_keywords.split(",") if k.strip()]
    if isinstance(raw_keywords, list):
        bn = brand.get("name") or brand.get("brand_name") or ""
        cs = brand.get("city") or ""
        cities_field = brand.get("cities") or None
        ind = brand.get("industry") or ""
        for kw in raw_keywords:
            kw_str = kw if isinstance(kw, str) else (kw.get("keyword") if isinstance(kw, dict) else "")
            if not kw_str:
                continue
            stratum = classify_keyword_to_stratum(kw_str, bn, cs, cities=cities_field, industry=ind)
            if stratum == "brand":
                kw_brand_n += 1
            elif stratum == "local":
                kw_local_n += 1
            else:
                kw_scenario_n += 1
    merged["keywords_count_brand"] = kw_brand_n
    merged["keywords_count_local"] = kw_local_n
    merged["keywords_count_scenario"] = kw_scenario_n

    # 数值字段
    merged["competitors_count"] = _competitors_count(brand, quote)
    merged["ai_test_total"] = int(diagnosis.get("ai_total_tests") or 0)
    merged["web_brand_direct_count"] = int(diagnosis.get("web_brand_direct_count") or 0)
    # 🔴 [#54/#55] **四态**,判据靠 `available` 这一格,不靠对 count 做真值判断。
    #    `if published.get("count"):` 会把 None(不可用)与 0(有报价 0 篇)压成一档,
    #    而这两档处置**正好相反**:前者退出分母,后者记缺失。这是本单头号毒。
    #
    #      reason=no_quote      该次诊断名下没有报价单        → 退出分母
    #      reason=其它(不可用) 投影拿不到/查询失败           → 退出分母,但**理由与上一档分开**
    #      available 且 count=0 有报价、真发布 0 篇           → 记缺失
    #      available 且 count>0 有报价、真发布 ≥1 篇          → 满足
    #
    #    🔴 两种"退出分母"的 reason 必须分得开:「还没到该有的时候」与「我们没查到」
    #    是两件事,压成一句会让人以为系统知道答案。
    published = published or {}
    _publications_available = bool(published.get("available"))
    _publications_deferred = not _publications_available
    _publications_reason = published.get("reason") or ("no_quote" if not quote else "unavailable")
    merged["publications_count"] = int(published.get("count") or 0) if _publications_available else 0

    groups_out: list[dict[str, Any]] = []
    total_score = 0.0
    impact_notes: list[str] = []

    # 🔴 [#55 2026-09-04] **退出分母**的字段:既不算分、也不记缺失,
    #    并且**从 field_total 里减掉** —— 归一化(scale = max_pts / field_total)本来就在,
    #    所以其余项会按比例把这一组的权重吸收满,**一个权重常量都不用改**。
    #    今天只有一项会退出:该品牌一张报价单都没有时的「已发布证据」。
    #    理由:报价晚于诊断产生,首诊时这一格**结构性**永远缺失,
    #    把它算进分母等于对每一份首诊报告都扣一次注定扣的分。
    deferred_keys: set[str] = {"publications_count"} if _publications_deferred else set()

    for grp in COMPLETENESS_GROUPS:
        max_pts = float(grp["weight"])
        live_fields = [f for f in grp["fields"] if f[0] not in deferred_keys]
        # 字段权重总和(应该 == max_pts · 否则归一化)
        field_total = sum(f[2] for f in live_fields)
        scale = max_pts / field_total if field_total > 0 else 1.0

        grp_score = 0.0
        missing: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = [
            {"key": f[0], "label": f[1], "weight": f[2],
             "reason": _publications_reason,
             "note": DEFERRED_FIELD_NOTE.get((f[0], _publications_reason),
                                             DEFERRED_FIELD_NOTE_UNAVAILABLE)}
            for f in grp["fields"] if f[0] in deferred_keys
        ]
        for field_key, field_label, field_weight in live_fields:
            # 数字阈值字段:>= 1/3/5/15 等
            threshold_map = {
                "keywords_count_brand": 1,
                "keywords_count_local": 3,
                "keywords_count_scenario": 5,
                "competitors_count": 3,
                "ai_test_total": 15,
                "publications_count": 1,
                "web_brand_direct_count": 1,
            }
            threshold = threshold_map.get(field_key, 1)
            value = _get_nested(merged, field_key)
            filled = _is_filled(value, threshold=threshold)
            if filled:
                grp_score += field_weight * scale
            else:
                missing.append({"key": field_key, "label": field_label, "weight": field_weight})

        grp_score_int = round(grp_score)
        total_score += grp_score_int

        if grp["key"] == "industry_brief" and grp_score_int < max_pts * 0.5:
            impact_notes.append(
                "行业深度资料未达 50% · 机会估算 / 30 天计划个性化会降级 · "
                "建议在客户工作台先跑一次行业深度分析"
            )
        if grp["key"] == "keywords" and kw_brand_n + kw_local_n + kw_scenario_n < 5:
            impact_notes.append(
                "关键词总数 < 5 · 推荐率分层置信度低 · 建议先在报价系统选 ≥ 10 个关键词"
            )
        if grp["key"] == "evidence_inputs" and grp_score_int < max_pts * 0.4:
            impact_notes.append(
                "证据/竞品输入不足 · 报告 Module 3-4(AI 实测证据/竞品分析)将明显空段"
            )

        groups_out.append({
            "key": grp["key"],
            "label": grp["label"],
            "weight": int(max_pts),
            "score": grp_score_int,
            "percent": round(100 * grp_score_int / max_pts, 1) if max_pts > 0 else 0,
            "missing_fields": missing,
            # 🔴 与 missing_fields **分开**:「还轮不到统计」不是「缺」。
            #    压成一档她会以为自己漏填了东西,而这一格根本还没到该有的时候。
            "deferred_fields": deferred,
        })

    total_score = int(round(total_score))
    if total_score >= 80:
        level = "充足"
    elif total_score >= 60:
        level = "可用"
    elif total_score >= 35:
        level = "勉强"
    else:
        level = "严重不足"

    missing_summary = ""
    total_missing = sum(len(g["missing_fields"]) for g in groups_out)
    if total_missing == 0:
        missing_summary = "所有关键输入均已提供 · 报告 8 模块满置信度产出"
    else:
        missing_summary = (
            f"缺 {total_missing} 项关键输入 · 影响 {len(impact_notes)} 个判断 · "
            f"报告内会显式标注"
        )

    return {
        "score": total_score,
        "max_score": 100,
        "level": level,
        "groups": groups_out,
        "missing_summary": missing_summary,
        "impact_notes": impact_notes,
    }


# ============================================================================
# 满分校验(开发期断言 · 防回归)
# ============================================================================

def assert_no_dimension_max_drift():
    """运行期校验:DIMENSIONS_100 / GEO_SCOPE_DIMENSIONS / scoring_levels 三处一致"""
    geo_total = sum(d["max"] for d in GEO_SCOPE_DIMENSIONS.values())
    if geo_total != 100:
        raise AssertionError(f"GEO_SCOPE_DIMENSIONS 总分 {geo_total} != 100")
    own_total = sum(d["max"] for d in DIMENSIONS_100)
    if own_total != 100:
        raise AssertionError(f"DIMENSIONS_100 总分 {own_total} != 100")
    return True
