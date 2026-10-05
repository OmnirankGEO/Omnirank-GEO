"""
[P0-D 信任资产/引用难度因子 2026-06-14] 品牌级信任资产采集器(独立批)

职责:复用诊断已有的 search_citations / domain 权威分级(经 source_authority_analyzer.
aggregate_source_authority 聚合)+ 品牌声明(官网 / authority_sources)→ 归并 7 类信任资产
× 证据等级 A/B/C → 算 trust_asset_score / citation_readiness_score → 写 brand_trust_asset_snapshot。

设计要点:
  ① build_trust_payload 是【纯函数】(吃 SAP dict + 声明 → 出 payload)· 0 DB/LLM · 完整可单测。
  ② collect_and_store 是【异步薄壳】· 取诊断 detail_table + 声明 → 调聚合 → build → 持久化 · 全 fail-soft。
  ③ 绝不阻塞诊断/报价(异步 · try/except 兜底 · 失败只 log)。
  ④ 证据等级:被 AI 引用(real_ai_citation)=A · 搜索品牌直引(search_brand_direct)=B · 仅声明=C。
  ⑤ 难度侧用 citation_readiness(被引用基础)· 高=容易(背书足)· 低=难(缺背书)。绝不裸乘利润系数。

🔴 红线:不碰 billing/auth/db.connection · 不进 5 维评分 SSOT · 不改诊断/报价公式。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("GEO-TrustAssetCollector")

# ============================================================
# 7 类信任资产(设计 §4.1)· label = 客户面人话 · weight 用于 trust_asset_score
# ============================================================
ASSET_ORDER: List[str] = [
    "official_site",
    "encyclopedia",
    "authority_media_coverage",
    "institutional_pages",
    "business_registry",
    "owned_content",
    "verifiable_cases",
]
# label = 客户面人话 · 必须是【类目中性名词】(同一 label 同时进 verified「已检测到X」与 missing「可补充X」
#   两个方向 · 不能用「已有官网」这类预设占有的措辞 → 否则 missing 侧会出现「可补充已有官网」自相矛盾)。
ASSET_LABELS: Dict[str, str] = {
    "official_site": "官方网站",
    "encyclopedia": "百科页面",
    "authority_media_coverage": "权威媒体报道",
    "institutional_pages": "政府/协会/学术信源",
    "business_registry": "主体公开信息",
    "owned_content": "自有内容阵地",
    "verifiable_cases": "可验证案例",
}
_CLASS_WEIGHT: Dict[str, float] = {
    "official_site": 0.10,
    "encyclopedia": 0.15,
    "authority_media_coverage": 0.30,
    "institutional_pages": 0.20,
    "business_registry": 0.10,
    "owned_content": 0.05,
    "verifiable_cases": 0.10,
}
_GRADE_FACTOR = {"A": 1.0, "B": 0.6, "C": 0.2}
_GRADE_RANK = {"A": 3, "B": 2, "C": 1}

_INSTITUTIONAL_SUFFIXES = ("gov.cn", "edu.cn", "org.cn", ".gov", ".edu")
_BUSINESS_REGISTRY_DOMAINS = ("qcc.com", "qichacha.com", "tianyancha.com", "qixin.com", "11315.com")

# citation_readiness 归一目标:endorsement t1 上限 12(source_authority_analyzer._compute_endorsement_score)
_READINESS_TARGET = 12.0


def _extract_domain(url: str) -> str:
    """简易 domain 抽取(优先复用 source_authority_analyzer.extract_domain)。"""
    if not url:
        return ""
    try:
        from services.source_authority_analyzer import extract_domain as _ed
        return (_ed(url) or "").lower()
    except Exception:
        pass
    s = str(url).strip().lower()
    for pre in ("https://", "http://", "//"):
        if s.startswith(pre):
            s = s[len(pre):]
    s = s.split("/")[0].split("?")[0]
    if s.startswith("www."):
        s = s[4:]
    return s


def _classify_asset_from_source(domain: str, tier: str) -> Optional[str]:
    """已验证来源(domain + SAP tier)→ 7 类中的哪一类(tier3/风险 → None 不计入命名资产)。"""
    d = (domain or "").lower()
    for suf in _INSTITUTIONAL_SUFFIXES:
        if d.endswith(suf):
            return "institutional_pages"
    for reg in _BUSINESS_REGISTRY_DOMAINS:
        if reg in d:
            return "business_registry"
    if tier == "structured_encyclopedia":
        return "encyclopedia"
    if tier in ("tier1_national", "tier2_portal_vertical"):
        return "authority_media_coverage"
    return None


def build_trust_payload(
    sap: Optional[Dict[str, Any]],
    *,
    official_website: Optional[str] = None,
    declared_authority_sources: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """纯函数:SAP 聚合结果 + 品牌声明 → trust 快照 payload(无 DB/LLM · 可单测)。

    sap = services.source_authority_analyzer.aggregate_source_authority(...) 的返回(可 None)。
    返回 payload(供 db.trust_asset_db.save_trust_snapshot)。
    """
    sap = sap or {}
    top_sources = sap.get("top_sources") or []
    tier_counts = sap.get("tier_counts") or {}
    endorsement_score = float(sap.get("endorsement_score", 0) or 0)
    data_quality = sap.get("data_quality") or {}
    has_real_ai = bool(data_quality.get("has_real_ai_citations", False))

    # assets: class_key -> {"grade": best, "evidence": [...]}
    assets: Dict[str, Dict[str, Any]] = {}

    def _record(class_key: str, grade: str, evidence: Dict[str, Any]) -> None:
        if class_key not in assets:
            assets[class_key] = {"grade": grade, "evidence": []}
        elif _GRADE_RANK.get(grade, 0) > _GRADE_RANK.get(assets[class_key]["grade"], 0):
            assets[class_key]["grade"] = grade
        assets[class_key]["evidence"].append(evidence)

    own_domain = _extract_domain(official_website) if official_website else ""
    verified_domains: set = set()

    # ① 已验证来源(诊断/搜索核验)→ 命名资产 · A=被 AI 引用 / B=搜索品牌直引
    for s in top_sources:
        if not isinstance(s, dict):
            continue
        ev_class = s.get("evidence_class")
        if ev_class not in ("real_ai_citation", "search_brand_direct"):
            continue  # industry_reference(品牌未检出)不算品牌背书
        grade = "A" if ev_class == "real_ai_citation" else "B"
        domain = (s.get("domain") or "").lower()
        tier = s.get("tier") or ""
        verified_domains.add(domain)
        # 自有域名(命中官网)→ official_site
        if own_domain and (domain == own_domain or domain.endswith("." + own_domain)):
            _record("official_site", grade, {"domain": domain, "tier": tier, "via": ev_class})
            continue
        cls = _classify_asset_from_source(domain, tier)
        if cls:
            _record(cls, grade, {"domain": domain, "tier": tier, "via": ev_class})

    # ② 声明类(证据等级最低 C)· 官网 / authority_sources(未在已验证集命中才记 C)
    if own_domain:
        if "official_site" not in assets:
            _record("official_site", "C", {"domain": own_domain, "via": "declared"})
        # 有官网 = 有自有内容阵地(声明级)
        _record("owned_content", "C", {"domain": own_domain, "via": "declared"})

    for src in (declared_authority_sources or []):
        if not src:
            continue
        raw = str(src).strip()
        dom = _extract_domain(raw) if ("." in raw and "/" in raw or raw.startswith("http")) else ""
        if dom and dom in verified_domains:
            continue  # 已在 ① 验证过 · 不重复记 C
        if dom:
            cls = _classify_asset_from_source(dom, "") or "verifiable_cases"
            if cls not in assets:
                _record(cls, "C", {"domain": dom, "via": "declared"})
        else:
            # 纯名称声明(无 URL)→ 可验证案例/证言(声明级)
            if "verifiable_cases" not in assets:
                _record("verifiable_cases", "C", {"name": raw[:60], "via": "declared"})

    # ③ 评分
    trust_asset_score = round(min(1.0, sum(
        _CLASS_WEIGHT.get(c, 0.0) * _GRADE_FACTOR.get(info["grade"], 0.0)
        for c, info in assets.items()
    )), 3)

    base_readiness = min(1.0, endorsement_score / _READINESS_TARGET) if _READINESS_TARGET else 0.0
    if not has_real_ai:
        base_readiness = min(base_readiness, 0.5)  # 无真实 AI 引用样本 → readiness 封顶 0.5
    citation_readiness_score = round(base_readiness, 3)

    # ④ verified / missing(grade A/B = 已验证;缺失 or 仅 C = missing/需补证据)
    verified_classes = {c for c, info in assets.items() if info["grade"] in ("A", "B")}
    verified_trust_assets = [
        {"type": c, "label": ASSET_LABELS[c], "grade": assets[c]["grade"]}
        for c in ASSET_ORDER if c in verified_classes
    ]
    missing_trust_assets = [
        {"type": c, "label": ASSET_LABELS[c]}
        for c in ASSET_ORDER if c not in verified_classes
    ]

    _declared = {
        "official_website": (official_website or "")[:200],
        "authority_sources_count": len(declared_authority_sources or []),
    }
    _assets_evidence = {c: {"grade": info["grade"], "evidence": info["evidence"]}
                        for c, info in assets.items()}

    # ⑤ source 口径(Codex#2 返修):必须有【真实 SAP 采样】(top_sources 非空 = 探测返回了来源)才算 collected。
    #    仅自报官网/自报权威源(grade C 声明)≠ 缺背书 → 不进价格因子(不调篇数 / 不触发报价 needs_review)。
    #    无采样 → source='missing' + readiness=None(消费端 source/readiness 双闸都拦)· 未知不惩罚(§5.1)。
    has_sampling = bool(top_sources)
    if not has_sampling:
        return {
            "trust_asset_source": "missing",
            "trust_asset_score": None,
            "citation_readiness_score": None,
            "trust_asset_needs_review": True,   # 内部:建议补真实采样(诊断/监测)· ≠ 报价 needs_review
            "verified_trust_assets": [],
            "missing_trust_assets": [],
            "trust_asset_evidence": {
                "note": "declared_only_no_sampling",
                "assets": _assets_evidence, "declared": _declared,
            },
        }

    risk = int(tier_counts.get("risk_low_quality", 0) or 0)
    authoritative = (int(tier_counts.get("tier1_national", 0) or 0)
                     + int(tier_counts.get("structured_encyclopedia", 0) or 0)
                     + int(tier_counts.get("tier2_portal_vertical", 0) or 0))
    needs_review = risk > 0 and risk >= max(1, authoritative)   # 风险源占优 → 内部人工核

    return {
        "trust_asset_source": "collected",
        "trust_asset_score": trust_asset_score,
        "citation_readiness_score": citation_readiness_score,
        "trust_asset_needs_review": bool(needs_review),
        "verified_trust_assets": verified_trust_assets,
        "missing_trust_assets": missing_trust_assets,
        "trust_asset_evidence": {
            "assets": _assets_evidence,
            "endorsement_score": endorsement_score,
            "tier_counts": tier_counts,
            "has_real_ai_citations": has_real_ai,
            "declared": _declared,
        },
    }


def collect_trust_assets(
    brand_id: int,
    *,
    diagnosis_detail_table: Any = None,
    official_website: Optional[str] = None,
    declared_authority_sources: Optional[List[Any]] = None,
    conn=None,
) -> Optional[Dict[str, Any]]:
    """聚合 SAP + 声明 → payload(不写库)· 任何异常返回 None(fail-soft)。"""
    try:
        from services.source_authority_analyzer import aggregate_source_authority
        sap = aggregate_source_authority(
            brand_id, diagnosis_detail_table=diagnosis_detail_table, conn=conn)
    except Exception as exc:
        logger.warning("collect_trust_assets: aggregate_source_authority 失败 brand=%s: %s", brand_id, exc)
        sap = None

    # 声明(官网 + authority_sources)· best-effort
    if official_website is None or declared_authority_sources is None:
        try:
            from db.profile_db import get_contact_info_by_brand, get_effective_brief_by_brand
            if official_website is None:
                official_website = (get_contact_info_by_brand(brand_id) or {}).get("website") or ""
            if declared_authority_sources is None:
                brief = get_effective_brief_by_brand(brand_id) or {}
                src = brief.get("authority_sources")
                declared_authority_sources = src if isinstance(src, list) else []
        except Exception as exc:
            logger.debug("collect_trust_assets: 声明读取失败 brand=%s: %s", brand_id, exc)

    if sap is None and not official_website and not declared_authority_sources:
        # 完全无信号 → 仍产 missing payload(记录"已尝试·无数据")
        return build_trust_payload(None, official_website=official_website,
                                   declared_authority_sources=declared_authority_sources)
    return build_trust_payload(sap, official_website=official_website,
                               declared_authority_sources=declared_authority_sources)


async def collect_and_store(
    brand_id: int,
    *,
    diagnosis_detail_table: Any = None,
    official_website: Optional[str] = None,
    declared_authority_sources: Optional[List[Any]] = None,
) -> Optional[int]:
    """采集 + 持久化(latest active)· 返回新快照 id · 全程 fail-soft(绝不抛/绝不阻塞调用方)。

    异步壳:聚合/DB 是阻塞调用 → 丢线程池跑,不卡事件循环(诊断链路调用安全)。
    """
    import asyncio

    def _run() -> Optional[int]:
        try:
            payload = collect_trust_assets(
                brand_id,
                diagnosis_detail_table=diagnosis_detail_table,
                official_website=official_website,
                declared_authority_sources=declared_authority_sources,
            )
            if payload is None:
                return None
            from db.trust_asset_db import save_trust_snapshot
            return save_trust_snapshot(brand_id, payload)
        except Exception as exc:
            logger.warning("collect_and_store 失败 brand=%s: %s", brand_id, exc)
            return None

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:
        logger.warning("collect_and_store to_thread 失败 brand=%s: %s", brand_id, exc)
        return None
