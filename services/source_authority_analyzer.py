"""
Source Authority Pack（SAP）聚合服务

把诊断报告里的"引用证据/链接"升级成客户能看懂的「权威背书证据」:
- 区分信源等级(一级国家/结构化百科/二级门户垂类/三级小媒体自媒体/风险低可信)
- 区分证据类别(真实 AI 引用 / 搜索品牌直引 / 行业参考)
- 给出独立的权威背书分 endorsement_score(0-20)

🔴 铁律:
- endorsement_score 是【独立解释层】,绝不进 5 维评分 SSOT 主分(避免与 geo_scope_scorer 的
  authority_score 维度撞名/双计分)。本模块只产出展示数据,不参与任何总分计算。
- 复用(不改)现有分级函数:_classify_domain_authority(keyword_value_scorer)+ get_domain_tier(domain_tiering)。
- 不新建完整 Evidence Store;聚合逻辑抽取自 api/distillation_api.py 的 /source-analysis(单一来源,防漂移)。
- 本批只做"已有证据"三类(真实 AI 引用 / 搜索品牌直引 / 行业参考),不做 mhz_media 媒体候选(二期)。
- 话术禁夸大:不说"AI 已信任/保证 AI 会引用/一定提升排名";如实标注 Kimi/DeepSeek 平台不返回引用。
- DB 取数失败 → 降级"数据不足"(不编造来源),不抛断报告生成。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger("GEO-SourceAuthority")

# ========== 信源四级(+百科)标签 ==========
TIER_LABELS = {
    "tier1_national": "一级·国家/央媒/政府",
    "structured_encyclopedia": "结构化百科源",
    "tier2_portal_vertical": "二级·头部门户/垂类知名站",
    "tier3_small_media_wemedia": "三级·小门户/自媒体/普通内容站",
    "risk_low_quality": "风险/低可信来源",
}

TIER_REASONS = {
    "tier1_national": "国家级/央媒/政府信源,高可信背书",
    "structured_encyclopedia": "结构化百科源,高可信、适合 AI 摘录",
    "tier2_portal_vertical": "头部门户/垂类知名站,有可引用素材",
    "tier3_small_media_wemedia": "小门户/自媒体/普通内容站,有曝光但说服力弱",
    "risk_low_quality": "短链/低质站,不建议作品牌背书",
}

# 常见域名 → 中文展示名(命不中则回退域名本身)
DOMAIN_DISPLAY_NAMES = {
    "people.com.cn": "人民网",
    "xinhuanet.com": "新华网",
    "cctv.com": "央视网",
    "gov.cn": "政府网站",
    "china.com.cn": "中国网",
    "36kr.com": "36氪",
    "iyiou.com": "亿欧",
    "jiqizhixin.com": "机器之心",
    "huxiu.com": "虎嗅",
    "tmtpost.com": "钛媒体",
    "zhihu.com": "知乎",
    "baijiahao.baidu.com": "百家号",
    "sohu.com": "搜狐",
    "163.com": "网易",
    "qq.com": "腾讯网",
    "sina.com.cn": "新浪",
    "csdn.net": "CSDN",
    "cnblogs.com": "博客园",
    "jianshu.com": "简书",
    "toutiao.com": "今日头条",
    "bilibili.com": "哔哩哔哩",
    "douyin.com": "抖音",
    "xiaohongshu.com": "小红书",
    "weibo.com": "微博",
    "baike.baidu.com": "百度百科",
    "baike.so.com": "360百科",
    "wikipedia.org": "维基百科",
}

# 结构化百科源(单列,不进 tier1_national)
_ENCYCLOPEDIA_DOMAINS = ("baike.baidu.com", "baike.so.com", "wikipedia.org")

# 证据类别优先级(数值越大权重越高;合并同域名时保留高优先级)
_EVIDENCE_PRIORITY = {
    "industry_reference": 1,
    "search_brand_direct": 2,
    "real_ai_citation": 3,
}

# 计入背书分的证据类别(行业参考仅展示不计分)
_SCORED_CLASSES = {"real_ai_citation", "search_brand_direct"}

# 计入背书分的信源等级(风险来源不计分)
_SCORED_TIERS = {
    "tier1_national",
    "structured_encyclopedia",
    "tier2_portal_vertical",
    "tier3_small_media_wemedia",
}

_CITATION_ENGINE_NOTE = (
    "部分 AI 引擎(如 Kimi、DeepSeek)在 API 层不返回引用来源,"
    "本统计基于可获取引用的引擎 + 搜索核验,非全引擎覆盖。"
)

# 复用现有分级函数(惰性导入,避免 import 期重依赖 + 便于单测纯逻辑)
_classify_domain_authority = None
_get_domain_tier = None


def _load_classifiers():
    global _classify_domain_authority, _get_domain_tier
    if _classify_domain_authority is None:
        from tools.keyword_value_scorer import _classify_domain_authority as cda
        _classify_domain_authority = cda
    if _get_domain_tier is None:
        from services.research_monitor.domain_tiering import get_domain_tier as gdt
        _get_domain_tier = gdt
    return _classify_domain_authority, _get_domain_tier


def extract_domain(url: str) -> str:
    """从 URL 提取归一化域名(与 /source-analysis 一致:netloc 小写去 www.)"""
    if not url or not isinstance(url, str):
        return ""
    try:
        parsed = urlparse(url if "://" in url else "http://" + url)
        domain = (parsed.netloc or "").lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return ""


def classify_sap_tier(domain: str) -> str:
    """
    四级(+结构化百科)映射,复用 _classify_domain_authority + get_domain_tier:
    - 百科类 → structured_encyclopedia
    - S → tier1_national
    - A / B → tier2_portal_vertical
    - C / social → tier3_small_media_wemedia
    - D 且 blacklist(短链/低质未知站)→ risk_low_quality;否则 tier3
    """
    if not domain:
        return "tier3_small_media_wemedia"
    nd = domain.lower()
    for enc in _ENCYCLOPEDIA_DOMAINS:
        if enc in nd:
            return "structured_encyclopedia"

    classify, domain_tier = _load_classifiers()
    cat = classify(domain)          # S/A/B/C/social/D
    block = domain_tier(domain)     # whitelist/gray/blacklist

    if cat == "S":
        return "tier1_national"
    if cat in ("A", "B"):
        return "tier2_portal_vertical"
    if cat in ("C", "social"):
        return "tier3_small_media_wemedia"
    # cat == 'D'(未识别)
    if block == "blacklist":
        return "risk_low_quality"
    return "tier3_small_media_wemedia"


def _display_name(domain: str) -> str:
    if not domain:
        return "未知来源"
    for known, name in DOMAIN_DISPLAY_NAMES.items():
        if known in domain:
            return name
    return domain


def _tier_to_evidence_class(tier_field: Any) -> Optional[str]:
    """brand_direct_citations._tier → evidence_class(缺/未知保守归 industry_reference)"""
    t = (tier_field or "").strip().lower() if isinstance(tier_field, str) else ""
    if t == "brand_direct":
        return "search_brand_direct"
    if t == "irrelevant":
        return None  # 丢弃
    # industry_reference / 缺失 / 未知 → 行业参考(仅展示不计分,保守不过度授信)
    return "industry_reference"


def _compute_endorsement_score(scored_tier_counts: dict[str, int]) -> int:
    """
    权威背书分(0-20,绝不进总分):
    一级 + 结构化百科:每个 +5,上限 12
    二级:每个 +2,上限 8
    三级:每个 +0.5,上限 3
    风险:0
    """
    t1 = scored_tier_counts.get("tier1_national", 0) + scored_tier_counts.get("structured_encyclopedia", 0)
    t2 = scored_tier_counts.get("tier2_portal_vertical", 0)
    t3 = scored_tier_counts.get("tier3_small_media_wemedia", 0)
    raw = min(12, t1 * 5) + min(8, t2 * 2) + min(3.0, t3 * 0.5)
    return int(round(min(20.0, raw)))


def _build_gaps(tier_counts: dict[str, int], class_counts: dict[str, int]) -> list[str]:
    gaps: list[str] = []
    t1 = tier_counts.get("tier1_national", 0) + tier_counts.get("structured_encyclopedia", 0)
    t2 = tier_counts.get("tier2_portal_vertical", 0)
    t3 = tier_counts.get("tier3_small_media_wemedia", 0)
    risk = tier_counts.get("risk_low_quality", 0)
    if t1 == 0:
        gaps.append("缺一级权威/结构化百科信源")
    if t3 > (t1 + t2):
        gaps.append("AI 当前可引用来源集中在三级普通内容源,推荐置信度有限")
    if class_counts.get("industry_reference", 0) > class_counts.get("search_brand_direct", 0) + class_counts.get("real_ai_citation", 0):
        gaps.append("品牌直引偏少,多为行业参考来源")
    if risk > 0 and risk >= (t1 + t2):
        gaps.append("存在较多低可信/短链来源,不建议作为品牌背书")
    return gaps


def build_source_authority_pack(
    monitoring_citations: list[dict],
    brand_direct_citations: list[dict],
    *,
    has_monitoring_query: bool = True,
    tier_classifier=None,
    has_monitoring_citations: bool = None,  # [返修 2026-06-07] None=按旧逻辑算;显式传则用传入值(拆开监测/诊断语义)
    has_diagnosis_citations: bool = False,
) -> dict:
    """
    纯逻辑聚合(可单测,不碰 DB):
    [SAP-AI] tier_classifier 可注入(默认 classify_sap_tier · 纯逻辑/单测行为不变);
             生产经 aggregate_source_authority 注入 AI 判定器(清单外未知域名交 AI 判等级)。
    - monitoring_citations: [{url, title, platform, keyword}]  → real_ai_citation
    - brand_direct_citations: [{url|link, title|site_name, snippet, _tier}]  → search_brand_direct / industry_reference
    返回 SAP dict(见模块顶部约定;不含 mhz 媒体候选)。
    """
    monitoring_citations = monitoring_citations or []
    brand_direct_citations = brand_direct_citations or []

    # domain -> 聚合记录
    agg: dict[str, dict] = {}

    def _touch(domain: str, url: str, title: str) -> dict:
        if domain not in agg:
            agg[domain] = {
                "domain": domain,
                "sample_url": url or "",
                "sample_title": title or "",
                "evidence_class": "industry_reference",
                "brand_direct": False,
                "ai_cited_count": 0,
                "search_count": 0,
                "platforms": set(),
            }
        rec = agg[domain]
        if title and len(title) > len(rec["sample_title"]):
            rec["sample_title"] = title
        if url and not rec["sample_url"]:
            rec["sample_url"] = url
        return rec

    def _upgrade_class(rec: dict, new_class: str) -> None:
        if _EVIDENCE_PRIORITY.get(new_class, 0) > _EVIDENCE_PRIORITY.get(rec["evidence_class"], 0):
            rec["evidence_class"] = new_class

    # ① 监测 search_citations:仅【品牌被检出(is_detected=true 且 mention_type != none)】的引用才算
    #    真实 AI 引用(real_ai_citation · 计入背书分);品牌【未检出】的回答里的引用只作行业参考
    #    (industry_reference · 不计分 · 不显示"AI 实际引用过")—— 否则会把"AI 没提品牌的回答用过的来源"
    #    误算成品牌背书,虚高 endorsement_score。
    has_real_ai = False
    for cit in monitoring_citations:
        if not isinstance(cit, dict):
            continue
        url = cit.get("url") or cit.get("link") or ""
        domain = extract_domain(url)
        if not domain or len(domain) < 3:
            continue
        title = cit.get("title") or cit.get("site_name") or ""
        rec = _touch(domain, url, title)
        platform = cit.get("platform") or ""
        if platform:
            rec["platforms"].add(platform)
        is_brand_cited = bool(cit.get("is_detected")) and (cit.get("mention_type") or "none") != "none"
        if is_brand_cited:
            has_real_ai = True
            _upgrade_class(rec, "real_ai_citation")
            rec["brand_direct"] = True  # 品牌被检出 + AI 实际引用 → 计入背书
            rec["ai_cited_count"] += 1
        else:
            # 品牌未在该回答中被检出 → 仅行业参考(不计分 · 不算品牌背书)
            _upgrade_class(rec, "industry_reference")
            rec["search_count"] += 1

    # ② 搜索品牌直引 / 行业参考(诊断 brand_direct_citations)
    has_brand_direct = False
    for cit in brand_direct_citations:
        if not isinstance(cit, dict):
            continue
        ev_class = _tier_to_evidence_class(cit.get("_tier"))
        if ev_class is None:
            continue  # irrelevant 丢弃
        url = cit.get("url") or cit.get("link") or ""
        domain = extract_domain(url)
        if not domain or len(domain) < 3:
            continue
        title = cit.get("title") or cit.get("site_name") or ""
        rec = _touch(domain, url, title)
        _upgrade_class(rec, ev_class)
        rec["search_count"] += 1
        if ev_class == "search_brand_direct":
            has_brand_direct = True
            rec["brand_direct"] = True

    # 分级 + 计数
    _tier_of = tier_classifier or classify_sap_tier  # [SAP-AI] 注入则用 AI 判定器,否则现行硬编码
    tier_counts = {k: 0 for k in TIER_LABELS}
    scored_tier_counts = {k: 0 for k in _SCORED_TIERS}
    class_counts = {"real_ai_citation": 0, "search_brand_direct": 0, "industry_reference": 0}

    sources: list[dict] = []
    for rec in agg.values():
        tier = _tier_of(rec["domain"])
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        ev = rec["evidence_class"]
        class_counts[ev] = class_counts.get(ev, 0) + 1
        # 计分:仅 real_ai/brand_direct 且非风险等级
        if ev in _SCORED_CLASSES and tier in _SCORED_TIERS:
            scored_tier_counts[tier] = scored_tier_counts.get(tier, 0) + 1
        sources.append({
            "domain": rec["domain"],
            "display_name": _display_name(rec["domain"]),
            "tier": tier,
            "tier_label": TIER_LABELS.get(tier, tier),
            "reason": TIER_REASONS.get(tier, ""),
            "evidence_class": ev,
            "brand_direct": bool(rec["brand_direct"]),
            "ai_cited_count": rec["ai_cited_count"],
            "platforms": sorted(rec["platforms"]),
            "sample_title": rec["sample_title"],
            "sample_url": rec["sample_url"],
        })

    # 排序:证据类别优先级 → AI 引用次数 + 搜索次数
    def _sort_key(s: dict):
        rec = agg[s["domain"]]
        return (
            _EVIDENCE_PRIORITY.get(s["evidence_class"], 0),
            rec["ai_cited_count"] + rec["search_count"],
        )
    sources.sort(key=_sort_key, reverse=True)
    top_sources = sources[:20]

    total_sources = len(agg)
    endorsement_score = _compute_endorsement_score(scored_tier_counts) if total_sources else 0
    gaps = _build_gaps(tier_counts, class_counts) if total_sources else ["暂无足够的 AI 引用数据生成权威背书分析"]

    # 解释文案(安全话术)
    if total_sources:
        t1 = tier_counts["tier1_national"] + tier_counts["structured_encyclopedia"]
        score_explanation = (
            f"AI 当前可引用/搜索核验到的来源共 {total_sources} 个:"
            f"一级权威 {tier_counts['tier1_national']}、结构化百科 {tier_counts['structured_encyclopedia']}、"
            f"二级门户/垂类 {tier_counts['tier2_portal_vertical']}、三级内容源 {tier_counts['tier3_small_media_wemedia']}、"
            f"风险来源 {tier_counts['risk_low_quality']};其中真实 AI 引用 {class_counts['real_ai_citation']} 个、"
            f"搜索品牌直引 {class_counts['search_brand_direct']} 个、行业参考 {class_counts['industry_reference']} 个(行业参考仅展示不计分)。"
        )
        if t1 == 0:
            score_explanation += "当前缺国家级/结构化百科级权威信源。"
    else:
        score_explanation = "暂无足够的 AI 引用数据,无法生成权威背书分布。"

    limitations: list[str] = []
    if not has_real_ai:
        limitations.append("当前统计以搜索核验的品牌提及为主,暂无可获取引擎的真实 AI 引用样本。")

    return {
        "module": "3_authority",
        "endorsement_score": endorsement_score,
        "score_max": 20,
        "score_explanation": score_explanation,
        "tier_counts": tier_counts,
        "top_sources": top_sources,
        "source_classes": {
            "real_ai_citation": {
                "count": class_counts["real_ai_citation"],
                "note": "AI 回答中实际返回的引用来源(基于可获取引用的引擎)",
            },
            "search_brand_direct": {
                "count": class_counts["search_brand_direct"],
                "note": "搜索核验到品牌被直接提及的来源",
            },
            "industry_reference": {
                "count": class_counts["industry_reference"],
                "note": "行业参考来源(仅展示,不计入背书分)",
            },
        },
        "gaps": gaps,
        "data_quality": {
            "has_real_ai_citations": has_real_ai,
            "has_search_brand_direct": has_brand_direct,
            "has_monitoring_citations": (
                bool(has_monitoring_query and monitoring_citations)
                if has_monitoring_citations is None else bool(has_monitoring_citations)
            ),
            "has_diagnosis_citations": bool(has_diagnosis_citations),
            "citation_engine_note": _CITATION_ENGINE_NOTE,
            "limitations": limitations,
        },
    }


def _fetch_monitoring_citations(brand_id: int, days: int = 30, conn=None) -> tuple[list[dict], bool]:
    """
    取 monitoring_results.search_citations(复用 /source-analysis 的查询思路)。
    返回 (flat_citations, query_ok)。任何异常 → ([], False),报告生成降级而非崩溃。
    """
    flat: list[dict] = []
    own_conn = False
    try:
        if conn is None:
            from db.distillation_db import get_connection
            conn = get_connection()
            own_conn = True
        cursor = conn.cursor()
        from services.monitoring_identity_review import aggregate_eligible_sql
        cursor.execute(
            f"""
            SELECT mr.platform, mr.keyword, mr.search_citations, mr.is_detected, mr.mention_type
            FROM monitoring_results mr
            JOIN monitoring_tasks mt ON mr.task_id = mt.id
            WHERE mt.brand_id = %s
              AND {aggregate_eligible_sql('mr')}
              AND mr.tested_at >= NOW() - INTERVAL '%s days'
              AND mr.search_citations IS NOT NULL
              AND mr.search_citations <> ''
              AND mr.search_citations <> '[]'
            """,
            (brand_id, days),
        )
        rows = cursor.fetchall()
        for row in rows:
            platform = row["platform"] or ""
            keyword = row["keyword"] or ""
            is_detected = bool(row.get("is_detected") if hasattr(row, "get") else row["is_detected"])
            mention_type = (row.get("mention_type") if hasattr(row, "get") else row["mention_type"]) or "none"
            raw = row["search_citations"]
            try:
                citations = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(citations, list):
                continue
            for cit in citations:
                if not isinstance(cit, dict):
                    continue
                flat.append({
                    "url": cit.get("url") or cit.get("link") or "",
                    "title": cit.get("title") or cit.get("site_name") or "",
                    "platform": platform,
                    "keyword": keyword,
                    "is_detected": is_detected,
                    "mention_type": mention_type,
                })
        return flat, True
    except Exception as e:  # noqa: BLE001 — 报告生成必须降级不崩
        logger.warning("SAP: 取监测引用失败,降级为无监测数据: %s", e)
        return [], False
    finally:
        if own_conn and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _extract_brand_direct(web_search_data: Any) -> list[dict]:
    """从 report_data 的 web_search 数据里取 brand_direct_citations(容错)"""
    if not isinstance(web_search_data, dict):
        return []
    cits = web_search_data.get("brand_direct_citations")
    if isinstance(cits, list):
        return cits
    return []


def _extract_diagnosis_citations(detail_table: Any) -> list[dict]:
    """[Phase1-B 2026-06-07] 从诊断 ai_visibility detail_table 抽各引擎 search_citations → flat
    (同 _fetch_monitoring_citations 形状 {url,title,platform,keyword,is_detected,mention_type})。
    诊断侧无 mention_type → brand_detected=True 记 'direct'(与监测同口径计入背书),否则 'none'(仅行业参考)。
    detail_table 缺/非法 → [](绝不抛 · 报告生成降级)。"""
    flat: list[dict] = []
    if not isinstance(detail_table, list):
        return flat
    for item in detail_table:
        if not isinstance(item, dict):
            continue
        question = item.get("question") or ""
        results = item.get("results") or {}
        if not isinstance(results, dict):
            continue
        for engine, res in results.items():
            if not isinstance(res, dict):
                continue
            cits = res.get("search_citations") or []
            if not isinstance(cits, list):
                continue
            is_detected = bool(res.get("brand_detected"))
            from services.mention_vocabulary import mention_type_from_outcome

            mention_type = mention_type_from_outcome(
                res.get("target_outcome"), is_detected=is_detected
            )
            for cit in cits:
                if not isinstance(cit, dict):
                    continue
                url = cit.get("url") or cit.get("link") or ""
                if not url:
                    continue
                flat.append({
                    "url": url,
                    "title": cit.get("title") or cit.get("site_name") or "",
                    "platform": engine,
                    "keyword": question,
                    "is_detected": is_detected,
                    "mention_type": mention_type,
                })
    return flat


def _dedupe_ai_citations(*citation_lists) -> list[dict]:
    """[Phase1-B] 合并多路 AI 引用(监测 + 诊断)按 (url, platform, keyword) 去重。
    重复时若任一来源 is_detected=True 则升级为已检出(保留品牌背书计分)。"""
    by_key: dict = {}
    order: list = []
    for lst in citation_lists:
        for cit in (lst or []):
            if not isinstance(cit, dict):
                continue
            url = (cit.get("url") or cit.get("link") or "").strip()
            if not url:
                continue
            key = (url.lower(), cit.get("platform") or "", cit.get("keyword") or "")
            if key not in by_key:
                by_key[key] = dict(cit)
                order.append(key)
            elif bool(cit.get("is_detected")) and not bool(by_key[key].get("is_detected")):
                by_key[key]["is_detected"] = True
                from services.mention_vocabulary import (
                    MENTION_MENTIONED,
                    normalize_mention_type,
                )

                by_key[key]["mention_type"] = (
                    normalize_mention_type(cit.get("mention_type"))
                    if cit.get("mention_type")
                    else MENTION_MENTIONED
                )
    return [by_key[k] for k in order]


def _build_ai_tier_classifier(monitoring_citations, brand_direct_citations):
    """[SAP-AI] 预解析所有域名 → AI 批量判等级 → 返回 resolver(domain)->tier。
    resolver: AI 命中用 AI 结果,否则回落 classify_sap_tier(已知域名/AI 未判到的)。
    任意失败 → 直接返回 classify_sap_tier(完全回落现行行为 · 绝不抛断报告生成)。"""
    try:
        from services.domain_authority_ai import classify_domains_ai
        domains = []
        for cit in (monitoring_citations or []):
            if isinstance(cit, dict):
                domains.append(extract_domain(cit.get("url") or cit.get("link") or ""))
        for cit in (brand_direct_citations or []):
            if isinstance(cit, dict):
                domains.append(extract_domain(cit.get("url") or cit.get("link") or ""))
        domains = [d for d in domains if d and len(d) >= 3]
        if not domains:
            return classify_sap_tier
        ai_map = classify_domains_ai(domains)
        if not ai_map:
            return classify_sap_tier

        def _resolver(domain: str) -> str:
            return ai_map.get((domain or "").lower()) or classify_sap_tier(domain)

        return _resolver
    except Exception as e:
        logger.warning("[SAP] AI 域名判定整体失败,回落 classify_sap_tier: %s", e)
        return classify_sap_tier


def get_industry_citation_landscape(industry: str, conn=None) -> dict:
    """[B4-1] 读飞轮 geo_research_source_signals 该行业引用源集中度(头部域名份额)。

    只读、fail-soft。返回 {} 表示无数据/不可用/样本不足。
    头部域名份额(top_domain_share)越高 = 行业引用越集中 = 越难挤进 → 定价篇数侧应上调难度。
    与飞轮页/行业信号同源(geo_research_source_signals),按行业名归一取数。
    """
    industry = (industry or "").strip()
    if not industry:
        return {}
    own_conn = False
    try:
        from services.media_entity_flywheel import industry_filter_values, is_all_industry_scope
        if is_all_industry_scope(industry):
            return {}  # 全行业口径对"该行业集中度"无意义
        if conn is None:
            from db.connection import get_connection
            conn = get_connection()
            own_conn = True
        values = [v.lower() for v in (industry_filter_values(industry) or [industry])]
        cur = conn.cursor()
        cur.execute("""
            SELECT domain, SUM(balanced_weight)::float AS w, COUNT(*) AS n
            FROM geo_research_source_signals
            WHERE LOWER(industry_key) = ANY(%s)
              AND domain IS NOT NULL AND domain <> ''
            GROUP BY domain
            ORDER BY w DESC
        """, (values,))
        rows = cur.fetchall()
        if not rows:
            return {}
        weights = [float(r["w"] or 0) for r in rows]
        total_w = sum(weights)
        total_signals = sum(int(r["n"] or 0) for r in rows)
        if total_w <= 0:
            return {}
        top_share = weights[0] / total_w
        return {
            "top_domain_share": round(top_share, 4),
            "distinct_domains": len(rows),
            "total_signals": total_signals,
            "difficulty": round(top_share, 4),  # 语义:越集中越难
        }
    except Exception as e:
        logger.warning("[B4-1] get_industry_citation_landscape 失败(降级不消费): %s", e)
        return {}
    finally:
        if own_conn and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def aggregate_source_authority(
    brand_id: int,
    web_search_data: Any = None,
    days: int = 30,
    conn=None,
    diagnosis_detail_table: Any = None,
    industry: str = "",
) -> dict:
    """
    SAP 主入口:取监测真实引用 + 诊断 4 引擎引用 + 诊断品牌直引 → 聚合分级 → SAP dict。
    [Phase1-B 2026-06-07] 新增读诊断自己的 ai_visibility detail_table 引用(此前只读 monitoring_results
    + web_search brand_direct,导致"只诊断没监测"的品牌权威背书必空)。诊断 + 监测引用按
    (url+平台+问题) 去重后合并,brand_direct 单独走原口径。
    报告生成调用此函数;DB 不可用时降级"数据不足",不抛异常。
    """
    monitoring_citations, query_ok = _fetch_monitoring_citations(brand_id, days=days, conn=conn)
    diagnosis_citations = _extract_diagnosis_citations(diagnosis_detail_table)
    # 诊断 + 监测引用同属"AI 实际引用"语义 → 合并去重(按 url+平台+问题)· 不与 brand_direct 混
    ai_citations = _dedupe_ai_citations(monitoring_citations, diagnosis_citations)
    brand_direct = _extract_brand_direct(web_search_data)
    # [SAP-AI] 清单外未知域名交 AI 判等级(已知域名仍走硬编码快通)· 整体失败回落 classify_sap_tier
    tier_classifier = _build_ai_tier_classifier(ai_citations, brand_direct)
    pack = build_source_authority_pack(
        ai_citations,
        brand_direct,
        has_monitoring_query=query_ok,
        tier_classifier=tier_classifier,
        # [返修 2026-06-07] 拆开来源语义:监测/诊断各自标记 · 不再用 has_monitoring_query 承载诊断来源
        has_monitoring_citations=bool(query_ok and monitoring_citations),
        has_diagnosis_citations=bool(diagnosis_citations),
    )
    # [B4-1] 仅当调用方显式传 industry 时,附带飞轮行业引用格局(独立字段·只读)。
    # 既有 SAP 调用方不传 industry → 不触发这次额外查询,行为零变化。
    if industry:
        landscape = get_industry_citation_landscape(industry, conn=conn)
        if landscape:
            pack["industry_citation_landscape"] = landscape
    return pack
