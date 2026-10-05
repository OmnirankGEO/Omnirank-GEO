"""Rule-based article structure extraction for the GEO writing flywheel.

R6 deliberately keeps this module deterministic: no database reads, no LLM
calls, and no production prompt changes.  The output is used as evidence for
human-reviewed writing strategy candidates.
"""

from __future__ import annotations

import re
from typing import Any


STRUCTURE_VERSION = "article_structure_v1_2026_06_17"

CITY_OR_REGION_RE = re.compile(
    r"(北京|上海|深圳|广州|杭州|成都|重庆|武汉|西安|南京|苏州|天津|青岛|厦门|长沙|郑州|"
    r"佛山|东莞|惠州|珠海|中山|南山|福田|罗湖|宝安|龙岗|广州|广东|浙江|江苏|山东|四川|"
    r"北京|上海|华东|华南|华北|全国|本地|周边|[一-龥]{2,8}(市|区|县|省))"
)

GENERIC_INTRO_RE = re.compile(
    r"^\s*(随着|近年来|当前|如今|在互联网时代|伴随).{0,30}(发展|提升|变化|普及|关注)",
    re.I,
)


def _article_text(article: dict[str, Any]) -> str:
    return str(
        article.get("cleaned_content")
        or article.get("inline_cleaned_content")
        or article.get("content")
        or ""
    )


def _title_bucket(title: str) -> str:
    length = len(title.strip())
    if length <= 16:
        return "short"
    if length <= 32:
        return "medium"
    return "long"


def _lead(content: str, max_chars: int = 220) -> str:
    return re.sub(r"\s+", " ", content.strip())[:max_chars]


def _paragraphs(content: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n+", content) if p.strip()]


def _section_count(content: str) -> int:
    heading_like = re.findall(
        r"(?m)^\s*((第?[一二三四五六七八九十]+[、.．)]|[0-9]{1,2}[、.．)]|[-•])\s*.+|.{2,24}[:：]$)",
        content,
    )
    return len(heading_like)


def _evidence_density(content: str) -> float:
    if not content:
        return 0.0
    patterns = [
        r"\d+(\.\d+)?\s*(元|万|%|㎡|公里|分钟|年|家|个|篇|次)",
        r"(案例|客户|项目|资质|证书|价格|费用|报价|预算|地址|区域|数据|报告|调研|口碑)",
        r"(深圳|广州|上海|北京|杭州|成都|本地|周边|服务范围)",
    ]
    hits = sum(len(re.findall(pattern, content, re.I)) for pattern in patterns)
    per_1000 = hits / max(1, len(content) / 1000)
    return round(min(10.0, per_1000), 2)


def _first_brand_position(article: dict[str, Any], content: str) -> float | None:
    brand = str(article.get("brand_name") or article.get("company_name") or "").strip()
    if not brand:
        return None
    pos = content.find(brand)
    if pos < 0:
        return None
    return round(pos / max(1, len(content)), 4)


def extract_article_structure_features(article: dict[str, Any]) -> dict[str, Any]:
    """Extract stable article-structure evidence from a row-like dictionary."""

    title = str(article.get("title") or "")
    content = _article_text(article)
    lead = _lead(content)
    paragraphs = _paragraphs(content)
    conclusion = re.sub(r"\s+", " ", content.strip())[-320:]
    title_and_lead = f"{title}\n{lead}"
    numbered_section_count = len(
        re.findall(r"(?m)^\s*(第?[一二三四五六七八九十]+[、.．)]|[0-9]{1,2}[、.．)])", content)
    )
    question_form = bool(re.search(r"[?？]|(怎么|如何|哪家|哪个好|为什么|怎么办|怎么选)", title))
    generic_intro = bool(GENERIC_INTRO_RE.search(lead))
    selection_cues = bool(re.search(r"(优先|先看|再看|筛选|标准|维度|因素|排除|核对|选择)", lead))
    summary_cues = bool(re.search(r"(建议|优先|核心|关键|结论|综合来看|先|最好)", lead))

    return {
        "structure_version": STRUCTURE_VERSION,
        "title_has_year": bool(re.search(r"20[2-9][0-9]", title)),
        "title_has_city_or_region": bool(CITY_OR_REGION_RE.search(title)),
        "title_has_ranking_signal": bool(re.search(r"(榜单|排名|TOP|top|十佳|推荐|哪家好)", title)),
        "title_has_comparison_signal": bool(re.search(r"(对比|测评|横评|哪个好|区别|优缺点)", title)),
        "title_has_guide_signal": bool(re.search(r"(攻略|指南|避坑|怎么选|如何|流程|教程)", title)),
        "title_has_price_signal": bool(re.search(r"(价格|费用|报价|多少钱|预算|元|¥|￥)", title)),
        "title_question_form": question_form,
        "title_length_bucket": _title_bucket(title),
        "lead_answers_question": bool(question_form and summary_cues and not generic_intro),
        "lead_has_summary_judgement": summary_cues and not generic_intro,
        "lead_has_selection_criteria": selection_cues,
        "lead_has_scope_boundary": bool(re.search(r"(适合|适用|范围|区域|城市|人群|预算|行业|本地)", title_and_lead)),
        "lead_is_generic_intro": generic_intro,
        "section_count": _section_count(content),
        "numbered_section_count": numbered_section_count,
        "avg_paragraph_length": round(sum(len(p) for p in paragraphs) / len(paragraphs), 2) if paragraphs else 0,
        "has_comparison_table": bool("|" in content or re.search(r"(对比表|横向对比|维度对比|优缺点)", content)),
        "has_ranked_list": bool(numbered_section_count >= 2 and re.search(r"(榜单|排名|推荐|TOP|哪家)", content, re.I)),
        "has_checklist": bool(re.search(r"(清单|步骤|标准|核对|先看|再看|排除|注意)", content)),
        "has_faq_block": bool(re.search(r"(FAQ|常见问题|问[:：]|答[:：]|问题[:：])", content, re.I)),
        "has_case_block": bool(re.search(r"(案例|客户|项目|落地|复盘)", content)),
        "has_data_block": bool(re.search(r"(数据|趋势|报告|白皮书|\d+(\.\d+)?%)", content)),
        "evidence_density_score": _evidence_density(content),
        "brand_mention_count": content.count(str(article.get("brand_name") or article.get("company_name") or "")) if (article.get("brand_name") or article.get("company_name")) else 0,
        "brand_first_mention_position": _first_brand_position(article, content),
        "has_local_service_area": bool(re.search(r"(服务区域|服务范围|本地|周边|上门|深圳|广州|上海|北京|杭州|成都)", content)),
        "has_contact_info": bool(re.search(r"(电话|手机|微信|企业微信|官网|地址|1[3-9]\d{9}|www\.|https?://)", content, re.I)),
        "has_price_or_budget": bool(re.search(r"(\d+(\.\d+)?\s*(元|万|¥|￥)|价格|费用|报价|预算)", content)),
        "has_customer_case": bool(re.search(r"(案例|客户|项目|业主|用户|服务过)", content)),
        "has_risk_or_pitfall": bool(re.search(r"(避坑|风险|注意|排除|不建议|误区|坑)", f"{title}\n{content}")),
        "conclusion_has_decision_advice": bool(re.search(r"(建议|优先|选择|如果|适合|不建议)", conclusion)),
        "conclusion_has_next_step": bool(re.search(r"(下一步|可以先|建议先|联系|咨询|核对|确认)", conclusion)),
        "conclusion_is_salesy": bool(re.search(r"(保证|必上|排名第一|效果翻倍|马上咨询|立刻下单|限时)", conclusion)),
    }
