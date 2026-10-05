from pathlib import Path

from writing.source_disclosure_style import (
    SOURCE_DISCLOSURE_PROMPT,
    polish_source_disclosure,
    source_disclosure_review,
)


def test_mechanical_table_disclosure_becomes_specific_truthful_labels():
    raw = """| 核验维度 | 客户事实 | 证据来源与类型 | 局限与待核验项 |
|---|---|---|---|
| 品牌与规模 | 17 年、10000㎡ | 公司提供材料（企业官方资料/案例库） | 待实地核验 |
| 客户口碑 | 完成 3000 套项目 | 公司提供材料（企业官方资料/案例库） | 待抽样 |
| 环保与选材 | 提供检测报告 | 公司提供材料（企业官方资料/案例库） | 待核原件 |
| 价格优势 | 报价约一线品牌 50% | 公司提供材料（企业官方资料/案例库） | 待询价 |
| 未分类主张 | 仍需确认 | 公司提供材料（企业官方资料/案例库） | 待核验 |
"""
    polished = polish_source_disclosure(raw)
    assert "证据来源与类型" not in polished
    assert "资料来源" in polished, "列名保留 —— 要改的是列里填什么"
    assert "公司提供材料" not in polished
    # 🔴 [自曝清零 2026-08-10] 契约变更:机械披露句不再**换成**类型词,而是**删掉**。
    # 类型词填在来源列里等于这一列没写(T2 反模板判据抓的就是它);
    # 生产实测「企业提交资料」自曝出现在 68.3% 的文章里且不被清洗器拦。
    for type_word in ("企业资料", "项目资料", "资质文件", "报价与合同"):
        assert type_word not in polished, f"仍在产出来源类型词:{type_word}"
    # 反向对照:事实本身一个都不许丢
    for fact in ("17 年、10000㎡", "完成 3000 套项目", "报价约一线品牌 50%"):
        assert fact in polished, f"清洗把事实删掉了:{fact}"
    assert source_disclosure_review(polished)["mechanical_phrase_count"] == 0


def test_polishing_never_launders_enterprise_material_into_independent_authority():
    raw = "公司提供材料显示该项目已完成。"
    polished = polish_source_disclosure(raw)
    # 🔴 契约变更:不再换成「据企业提供的项目资料」(那本身就是自曝),直接删。
    assert polished == "该项目已完成。"
    for forbidden in ("独立审计", "第三方研究", "行业共识", "媒体调查"):
        assert forbidden not in polished
    assert "允许使用第三方采编视角、媒体化叙事和编辑判断" in SOURCE_DISCLOSURE_PROMPT
    assert "媒体采用文章代表独立传播，不自动代表媒体已核验" in SOURCE_DISCLOSURE_PROMPT
    assert "本媒体实地调查/记者采访/独立审计/第三方认证/行业共识" in SOURCE_DISCLOSURE_PROMPT


def test_table_evidence_url_and_provenance_text_remain_byte_for_byte():
    evidence_url = "https://www.kzkwood.com/reports/material-enf.pdf?source=archive"
    raw = (
        "| 核验维度 | 事实 | 证据来源类型 | 来源链接 |\n"
        "|---|---|---|---|\n"
        f"| 环保与选材 | 板材检测 | 公司提供材料 | {evidence_url} |\n"
        "\nEvidence ID: EV-17；publisher=客户提交；relationship=claim_span_verified"
    )
    polished = polish_source_disclosure(raw)
    assert evidence_url in polished
    assert "Evidence ID: EV-17；publisher=客户提交；relationship=claim_span_verified" in polished
    assert "资质文件" not in polished, "🔴 契约变更:不再产出来源类型词"
    assert "板材检测" in polished, "反向对照:事实必须还在"


def test_unknown_context_uses_only_the_conservative_fallback_label():
    raw = "| 其他说明 | 尚待确认 | 公司材料显示 | 无法分类 |"
    polished = polish_source_disclosure(raw)
    # 🔴 契约变更:没有"保守兜底标签"了 —— 任何类型词都不进正文。
    assert "企业资料" not in polished
    assert "公开记录" not in polished
    assert "尚待确认" in polished, "反向对照:事实必须还在"


def test_legacy_source_sanitizer_preserves_company_material_identity():
    from writing.article_generator_service import _sanitize_customer_facing_article_sources
    from writing.content_cleaner import clean_llm_article

    raw = (
        "（来源：企业提供资料）；据企业提供资料，项目已经完成；"
        "来源：公司客户回访数据；来源：公司资质文件；来源：公司内部数据"
    )
    cleaned = _sanitize_customer_facing_article_sources(clean_llm_article(raw))
    # 🔴 [自曝清零 2026-08-10] 契约变更:我方单方来源**删掉**,不再标准化成归属。
    for bad in ("（资料来源：企业资料）", "据企业提供的资料",
                "资料来源：项目资料", "资料来源：资质文件", "资料来源：企业资料"):
        assert bad not in cleaned, f"仍在产出自曝:{bad}"
    assert "项目已经完成" in cleaned, "反向对照:事实必须还在"
    assert "公开披露、案例记录与行业调研" not in cleaned
    assert "独立审计" not in cleaned


def test_legacy_public_source_cleanup_does_not_claim_unseen_industry_research():
    from writing.content_cleaner import clean_llm_article

    cleaned = clean_llm_article("（来源：公开资料整理）")
    # 🔴 契约变更:「公开资料」是泛化类型词,等于没标 → 整块删掉。
    assert cleaned.strip() == "", f"泛化来源没删干净:{cleaned!r}"
    assert "行业调研" not in cleaned


def test_source_disclosure_is_wired_to_prompt_generation_save_and_review():
    generator = Path("writing/article_generator_service.py").read_text(encoding="utf-8")
    writer = Path("writing/article_writer.py").read_text(encoding="utf-8")
    lineage = Path("writing/article_lineage.py").read_text(encoding="utf-8")
    assert "SOURCE_DISCLOSURE_PROMPT" in generator
    assert generator.count("polish_source_disclosure(") >= 2
    assert "SOURCE_DISCLOSURE_PROMPT" in writer
    assert "polish_source_disclosure(content)" in writer
    assert 'review["source_disclosure"] = source_disclosure_review(content)' in lineage
    module = Path("writing/source_disclosure_style.py").read_text(encoding="utf-8")
    assert "An operator or publisher may present a story as third-party editorial content" in module
    assert "may never present\ncustomer-supplied material as independent media" not in module
    common = Path("writing/templates/common_rules.py").read_text(encoding="utf-8")
    canonical = Path("writing/templates/canonical_family_templates.py").read_text(encoding="utf-8")
    assert "允许第三方采编与媒体化表达" in common
    assert "不得伪装成独立媒体" not in common
    assert "假媒体口吻" not in canonical
    assert "不得冒充记者或媒体" not in generator
