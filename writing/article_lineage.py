"""Build one immutable generation-lineage bundle for every article INSERT."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any
from uuid import uuid4

from .article_style_contract import STYLE_CONTRACT_VERSION, family_for_style, resolve_new_generation_style
from .evidence_pack import normalize_evidence_pack
from .geo_article_expert import review_article
from .platform_safety_profiles import normalize_profile, review_for_platform
from .article_length_contract import (
    ARTICLE_LENGTH_CONTRACT_VERSION,
    build_length_plan_for_topic,
    count_effective_chars,
    length_bucket,
)
from .source_disclosure_style import (
    SOURCE_DISCLOSURE_STYLE_VERSION,
    source_disclosure_review,
)
from .evidence_precision_policy import EVIDENCE_PRECISION_CONTRACT_VERSION
from .evidence_first_policy import LEGAL_PROHIBITION_CATALOG_VERSION


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) if not isinstance(value, str) else value
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_article_lineage(
    *,
    topic: dict[str, Any],
    article: dict[str, Any],
    quote_id: int | None,
    industry: str = "",
    client_brand: str = "",
) -> dict[str, Any]:
    style_code = resolve_new_generation_style(article.get("style") or article.get("style_code") or topic.get("style_code")) or "buying_guide"
    request_id = str(article.get("generation_request_id") or topic.get("generation_request_id") or uuid4())
    evidence = normalize_evidence_pack(
        article.get("evidence_pack") or topic.get("evidence_pack") or topic.get("_evidence_pack"),
        request_id=request_id,
    )
    brand_snapshot = article.get("brand_fact_snapshot") or topic.get("brand_fact_snapshot") or {
        "version": "brand-fact-proxy-v1",
        "brand_name": client_brand,
        "quote_id": quote_id,
        "source_status": "current_config_proxy",
        "warning": "Not an independently verified brand-fact snapshot.",
    }
    title = str(article.get("title") or topic.get("title") or "")
    content = str(article.get("content") or "")
    content_hash = _hash(content)
    # [WP12 P0-3 / P1-5] 三铁律与篇幅分档需要竞品集合、文体与篇幅计划才能测量;
    # 缺任何一项都只是少测一条 A1,不影响既有裁决。
    _lineage_length_plan = topic.get("_length_plan")
    if not isinstance(_lineage_length_plan, dict):
        _lineage_length_plan = build_length_plan_for_topic(style_code, topic)
    _competitors = tuple(
        str(c) for c in (
            topic.get("_researched_competitors")
            or topic.get("verified_competitors")
            or article.get("competitors")
            or []
        ) if str(c or "").strip()
    )
    review = review_article(
        title=title,
        content=content,
        evidence_pack=evidence,
        brand_fact_snapshot=brand_snapshot,
        industry=industry,
        target_question=str(topic.get("title") or topic.get("optimized_title") or ""),
        client_brand=client_brand,
        competitor_names=_competitors,
        style_code=style_code,
        family_code=family_for_style(style_code) or "",
        length_plan=_lineage_length_plan,
    ).payload()
    review["hard_failures"] = list(review.get("hard_failures") or [])
    review["warnings"] = list(review.get("warnings") or [])
    publication_profile = normalize_profile(
        article.get("publication_profile") or topic.get("publication_profile")
    )
    platform_review = review_for_platform(
        title=title,
        content=content,
        profile=publication_profile,
    ).payload()
    review["reviewed_content_hash"] = content_hash
    review["reviewed_evidence_manifest_hash"] = evidence["manifest_hash"]
    review["publication_profile"] = publication_profile
    review["platform_review"] = platform_review
    length_plan = _lineage_length_plan
    actual_chars = count_effective_chars(content)
    minimum_chars = int(length_plan.get("minimum_chars") or 0)
    maximum_chars = int(length_plan.get("maximum_chars") or 0)
    if actual_chars < minimum_chars:
        length_status = "below_candidate_range"
    elif maximum_chars and actual_chars > maximum_chars:
        length_status = "above_candidate_range"
    else:
        length_status = "within_candidate_range"
    review["length_diagnostic"] = {
        "version": ARTICLE_LENGTH_CONTRACT_VERSION,
        "actual_effective_chars": actual_chars,
        "length_bucket": length_bucket(actual_chars),
        "candidate_range_status": length_status,
        "quality_or_citation_score": False,
    }
    review["source_disclosure"] = source_disclosure_review(content)
    if review["source_disclosure"]["rewrite_recommended"]:
        review["warnings"].append("mechanical_source_disclosure_repetition")
    if platform_review["decision"] == "rewrite_required":
        review["decision"] = "rewrite_required"
        review["hard_failures"].extend(platform_review["hard_failures"])

    style_version = str(
        article.get("style_version_id")
        or topic.get("_style_version_id")
        or topic.get("style_version_id")
        or ""
    ).strip()
    if not style_version:
        try:
            from .style_control import get_active_version_id

            style_version = str(get_active_version_id(style_code) or "").strip()
        except Exception:
            style_version = ""
    if not style_version:
        style_version = STYLE_CONTRACT_VERSION
    request_snapshot = {
        "version": "generation-request-v1.0",
        "request_id": request_id,
        "topic_id": topic.get("id"),
        "quote_id": quote_id,
        "target_question": topic.get("title") or topic.get("optimized_title"),
        "original_keyword": topic.get("keyword") or topic.get("original_keyword"),
        "user_choice": topic.get("user_choice"),
        "publication_profile": publication_profile,
        "requested_add_images": topic.get("_requested_add_images"),
        "requested_add_contact": topic.get("_requested_add_contact"),
        "effective_add_images": topic.get("_effective_add_images"),
        "effective_add_contact": topic.get("_effective_add_contact"),
        "length_contract_version": ARTICLE_LENGTH_CONTRACT_VERSION,
        "length_plan": length_plan,
        "actual_effective_chars": actual_chars,
        "length_bucket": length_bucket(actual_chars),
        "source_disclosure_style_version": SOURCE_DISCLOSURE_STYLE_VERSION,
        "evidence_precision_contract_version": EVIDENCE_PRECISION_CONTRACT_VERSION,
        # [Review-CTO 2026-07-23 P1] 冻结本篇判定所用的法律禁止清单版本,
        # 与运行时硬门/提示词同源;发布快照/失败记录经此 lineage 一并留痕。
        "legal_prohibition_catalog_version": LEGAL_PROHIBITION_CATALOG_VERSION,
        # 🔴 [D6-A 2026-08-10 · 最终接管工单 §8] 主优势留痕:候选、素材引用、
        # 排序理由与定向增援 query,零 DDL 落快照。target_engine 生成时没有
        # 明确目标就为空,如实记录不硬编;reco_feedback / engine_recognition_state
        # 为 D6-B(客户×问题×引擎×时间窗 六态回喂)预留,本包恒 None。
        "primary_advantage": topic.get("_primary_advantage"),
        "experiment_assignment_id": topic.get("_experiment_assignment_id"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    # [Gate-2 措施标签 2026-08-09] Owner 裁定「不做长期实验,可以安针」——
    # 不建 arm 登记表,改成每篇新稿自带措施标签,事后按标签归因。
    # 🔴 落在 lineage 这一处,是因为**三条 INSERT 路径都经过 build_article_lineage**
    # (`_save_article` / `rewrite_article` / 补发链),各写一份等于给同一规则留三个走样的机会。
    # 🔴 `injected` 必须取自生成侧真实写下的那一位,**不许在这里默认 True** ——
    # 没注入却记 applied 就是给归因喂假数据(而归因是这次唯一的效果判据)。
    try:
        from .gate2_rewrite_measures import build_gate2_measure_tag

        request_snapshot["gate2_measures"] = build_gate2_measure_tag(
            title=title,
            content=content,
            brand_name=client_brand,
            injected=bool(
                topic.get("_gate2_injected")
                or article.get("_gate2_injected")
            ),
            # 🔴 [R3 订正7 2026-08-11] 按篇豁免码(C8/C14)一路传进 tag:
            # 不传 = applied 记全量 = 每篇都记「G8/G9 已应用」而 prompt 里没有。
            exempt_codes=tuple(
                topic.get("_gate2_exempt_codes")
                or article.get("_gate2_exempt_codes")
                or ()
            ),
        )
    except Exception as _g2_tag_err:                      # noqa: BLE001
        # D8:打标失败不阻断保存,但要留下"为什么没打上"。
        request_snapshot["gate2_measures"] = {
            "version": "unavailable", "injected": False, "applied": [], "weak": [],
            "exempted": [], "observed": {},
            "error": f"{type(_g2_tag_err).__name__}: {str(_g2_tag_err)[:120]}",
        }
    return {
        "style_code": style_code,
        "style_family": family_for_style(style_code) or "mapping_unknown",
        "style_contract_version": STYLE_CONTRACT_VERSION,
        "style_version": style_version,
        "generation_request_id": request_id,
        "generation_request_snapshot": request_snapshot,
        "prompt_hash": article.get("prompt_sha256"),
        "evidence_pack": evidence,
        "evidence_manifest_hash": evidence["manifest_hash"],
        "brand_fact_snapshot": brand_snapshot,
        "brand_snapshot_hash": _hash(brand_snapshot),
        "article_review": review,
        "article_review_status": review["decision"],
        "publication_profile": publication_profile,
        "platform_review": platform_review,
        "current_content_hash": content_hash,
    }
