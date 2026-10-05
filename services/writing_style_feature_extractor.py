"""Extract reusable writing-style features from cited/adopted articles."""

from __future__ import annotations

import json
import re
from typing import Any

from services.article_structure_features import extract_article_structure_features
from services.media_entity_flywheel import normalize_domain, normalize_industry_key
from writing.article_length_contract import (
    ARTICLE_LENGTH_CONTRACT_VERSION,
    count_effective_chars,
    length_bucket,
)


STYLE_KEYWORDS = {
    "ranking": ("榜单", "排名", "TOP", "top", "推荐", "哪家", "十佳"),
    "comparison": ("对比", "测评", "横评", "区别", "哪个好", "优缺点"),
    "guide": ("指南", "攻略", "避坑", "怎么选", "如何", "流程", "教程"),
    "faq": ("FAQ", "常见问题", "问答", "为什么", "怎么办"),
    "case": ("案例", "客户", "项目", "落地", "复盘"),
    "data_report": ("数据", "趋势", "报告", "白皮书", "洞察"),
}


def _text(row: dict[str, Any]) -> str:
    return str(row.get("cleaned_content") or row.get("content") or row.get("inline_cleaned_content") or "")


def _pick_style_family(title: str, content: str, intent_type: str = "") -> str:
    joined = f"{title}\n{content[:1200]}"
    scores = {
        style: sum(1 for kw in kws if kw in joined)
        for style, kws in STYLE_KEYWORDS.items()
    }
    best_score = max(scores.values() or [0])
    if best_score > 0:
        winners = {style for style, score in scores.items() if score == best_score}
        # family 是第二轴,不能被 intent 主轴提前锁死。并列时优先更具体的表达形态,
        # 让 ranking+guide/case 等组合桥接能真正命中文体覆盖。
        for style in ("case", "guide", "comparison", "data_report", "faq", "ranking"):
            if style in winners:
                return style
    if intent_type in STYLE_KEYWORDS:
        return intent_type
    return "guide"


def infer_style_family(title: str, content: str, intent_type: str = "") -> str:
    """Infer the writing style family axis without changing the stored intent label."""
    return _pick_style_family(title, content, intent_type)


def extract_writing_style_features(article: dict[str, Any]) -> dict[str, Any]:
    title = str(article.get("title") or "")
    content = _text(article)
    article_structure = extract_article_structure_features(article)
    style_family = _pick_style_family(title, content, str(article.get("intent_type") or ""))
    paragraphs = [p.strip() for p in re.split(r"\n+", content) if p.strip()]
    avg_para_len = round(sum(len(p) for p in paragraphs) / len(paragraphs), 2) if paragraphs else 0
    numbered_sections = len(re.findall(r"(^|\n)\s*(\d+[\.\)、]|[一二三四五六七八九十]+[、.])", content))
    has_faq = bool(re.search(r"(FAQ|常见问题|问[:：]|答[:：])", content, re.I))
    has_price = bool(re.search(r"(\d+(\.\d+)?\s*(元|万|￥|¥)|价格|报价|费用)", content))
    has_local_signal = bool(re.search(r"(北京|上海|深圳|广州|杭州|成都|重庆|武汉|西安|南京|苏州)", content))
    effective_chars = count_effective_chars(content)
    snapshot = article.get("generation_request_snapshot")
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except Exception:
            snapshot = None
    generation_length_version = "legacy_unknown"
    if isinstance(snapshot, dict):
        plan = snapshot.get("length_plan")
        if isinstance(plan, dict) and plan.get("version"):
            generation_length_version = str(plan["version"])

    return {
        "article_id": article.get("id"),
        "title": title,
        "domain": normalize_domain(article.get("domain") or article.get("url") or ""),
        "industry_key": normalize_industry_key(article.get("primary_industry") or article.get("industry") or ""),
        "style_family": style_family,
        "intent_type": article.get("intent_type") or style_family,
        "content_type": article.get("content_type") or "article",
        "avg_paragraph_length": avg_para_len,
        "paragraph_count": len(paragraphs),
        "numbered_sections": numbered_sections,
        "has_faq": has_faq,
        "has_price": has_price,
        "has_local_signal": has_local_signal,
        "effective_char_count": effective_chars,
        "length_bucket": length_bucket(effective_chars),
        "length_bucket_version": ARTICLE_LENGTH_CONTRACT_VERSION,
        "length_contract_version": generation_length_version,
        "length_is_causal_score": False,
        "article_structure": article_structure,
        "structure_score": article_structure.get("evidence_density_score", 0),
        "feature_version": "writing_style_features_v2_2026-06-17",
    }


def summarize_style_features(features: list[dict[str, Any]]) -> dict[str, Any]:
    if not features:
        return {
            "style_family": "guide",
            "confidence": 0.0,
            "feature_count": 0,
            "common_elements": [],
        }
    counts: dict[str, int] = {}
    for item in features:
        family = item.get("style_family") or "guide"
        counts[family] = counts.get(family, 0) + 1
    style_family, count = max(counts.items(), key=lambda x: x[1])
    common_elements: list[str] = []
    for key, label in [
        ("has_faq", "FAQ 问答段"),
        ("has_price", "价格/费用信息"),
        ("has_local_signal", "本地化表达"),
    ]:
        if sum(1 for f in features if f.get(key)) >= max(1, len(features) // 2):
            common_elements.append(label)
    for key, label in [
        ("lead_answers_question", "开头先回答问题"),
        ("has_checklist", "清单/步骤结构"),
        ("has_risk_or_pitfall", "避坑/风险提示"),
        ("conclusion_has_decision_advice", "结尾给选择建议"),
    ]:
        if sum(1 for f in features if (f.get("article_structure") or {}).get(key)) >= max(1, len(features) // 2):
            common_elements.append(label)
    avg_sections = round(sum(float(f.get("numbered_sections") or 0) for f in features) / len(features), 2)
    if avg_sections >= 2:
        common_elements.append("分点/编号结构")
    return {
        "style_family": style_family,
        "confidence": round(count / len(features), 3),
        "feature_count": len(features),
        "common_elements": common_elements,
        "avg_numbered_sections": avg_sections,
    }
