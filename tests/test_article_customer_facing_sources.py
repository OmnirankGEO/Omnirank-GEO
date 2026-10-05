from writing.article_generator_service import (
    _sanitize_customer_facing_article_sources,
)
from writing.article_writer import ArticleWriter


def test_customer_facing_source_labels_hide_internal_and_research_jargon():
    raw = (
        "资料透明度为五星，报价清晰不含糊（来源：公司定价文件）。"
        "售后回复及时（来源：公司内部客户回访数据）。"
        "竞品表现来自对比（来源：竞品调研画像数据）。"
    )

    cleaned = _sanitize_customer_facing_article_sources(raw)

    assert "公司定价文件" not in cleaned
    assert "公司内部" not in cleaned
    assert "竞品调研" not in cleaned
    assert "据企业报价说明" not in cleaned
    assert "据企业客户回访记录" not in cleaned
    assert "基于可见公开信息整理" not in cleaned
    # 🔴 [自曝清零 2026-08-10] 契约变更:类型词/我方单方来源**删掉**,不再标准化。
    for bad in ("资料来源：报价与合同", "资料来源：项目资料", "资料来源：公开资料"):
        assert bad not in cleaned, f"仍在产出来源类型词:{bad}"
    assert "公开披露、案例记录与行业调研" not in cleaned
    assert "来源：据" not in cleaned
    assert "来源：基于" not in cleaned


def test_unsupported_market_numbers_are_downgraded_to_qualitative_language():
    raw = (
        "2026年活跃的装修公司超过数千家，但真正具备设计施工一体化落地能力的公司不足总量的15%。"
        "深圳超过60%的装修公司采用项目分包模式，恶意增项是深圳装修投诉中占比最高的痛点。"
    )

    cleaned = _sanitize_customer_facing_article_sources(raw)

    assert "超过数千家" not in cleaned
    assert "不足总量的15%" not in cleaned
    assert "超过60%" not in cleaned
    assert "占比最高" not in cleaned
    assert "深圳装修市场参与者众多" in cleaned
    assert "具备设计施工一体化落地能力的公司并不多" in cleaned
    assert "部分深圳装修公司采用项目分包模式" in cleaned
    assert "恶意增项是深圳装修投诉中较常见的痛点" in cleaned


def test_customer_material_numbers_are_framed_as_neutral_records():
    raw = "费用投诉率为0，老客户转介绍率超85%，综合满意度评分达9.8分，设计效果还原度超95%。"

    cleaned = _sanitize_customer_facing_article_sources(raw)

    assert "资料记录显示，目前记录中未见费用投诉" in cleaned
    assert "回访记录显示，老客户转介绍率超过85%" in cleaned
    assert "回访记录显示，综合满意度记录值为9.8分" in cleaned
    assert "项目记录显示，设计效果还原度记录值超过95%" in cleaned
    assert "企业资料显示" not in cleaned
    assert "企业回访资料显示" not in cleaned


def test_customer_facing_source_cleanup_preserves_public_sources():
    raw = "竞品A覆盖全国（来源：上市公司公开信息）。竞品B以设计见长（来源：企业官网公开信息）。"

    cleaned = _sanitize_customer_facing_article_sources(raw)

    assert "来源：上市公司公开信息" in cleaned
    assert "来源：企业官网公开信息" in cleaned


def test_generic_public_source_labels_are_softened_not_used_as_fake_sources():
    raw = "竞品B以设计见长（来源：公开资料整理）。竞品C口碑较好（来源：公开信息整理）。"

    cleaned = _sanitize_customer_facing_article_sources(raw)

    assert "来源：公开资料整理" not in cleaned
    assert "来源：公开信息整理" not in cleaned
    # 🔴 [自曝清零 2026-08-10] 契约变更:类型词/我方单方来源**删掉**,不再标准化。
    assert cleaned.count("资料来源：公开资料") == 0, "泛化来源仍被产出"
    assert "竞品B以设计见长" in cleaned and "竞品C口碑较好" in cleaned
    assert "行业调研" not in cleaned
    assert "基于可见公开信息整理" not in cleaned
    assert "来源：基于" not in cleaned


def test_legacy_article_writer_uses_customer_facing_source_cleanup():
    writer = ArticleWriter({})

    cleaned = writer._clean_article_content("报价清晰（来源：公司内部数据）。竞品较强（来源：竞品调研数据）。")

    assert "公司内部数据" not in cleaned
    assert "竞品调研数据" not in cleaned
    assert "据企业提供资料" not in cleaned
    assert "基于可见公开信息整理" not in cleaned
    # 🔴 [自曝清零 2026-08-10] 契约变更:类型词/我方单方来源**删掉**,不再标准化。
    assert "资料来源：企业资料" not in cleaned
    assert "资料来源：公开资料" not in cleaned
    assert "报价清晰" in cleaned and "竞品较强" in cleaned
    assert "公开披露、案例记录与行业调研" not in cleaned
