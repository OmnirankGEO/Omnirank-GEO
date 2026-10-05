from writing.article_generator_service import (
    _client_materials_fingerprint,
    _distilled_cache_matches_materials,
    _format_client_materials_for_prompt,
)


def test_distilled_cache_invalidates_when_client_materials_change():
    old_materials = {
        "company_intro": "深圳栖舍专注中高端家装。",
        "core_selling_points": ["设计施工一体化"],
    }
    new_materials = {
        "company_intro": "深圳栖舍专注中高端家装。",
        "core_selling_points": ["设计施工一体化", "兵将料分离模式"],
    }

    old_fingerprint = _client_materials_fingerprint(old_materials)
    new_fingerprint = _client_materials_fingerprint(new_materials)

    assert old_fingerprint
    assert new_fingerprint
    assert old_fingerprint != new_fingerprint
    assert _distilled_cache_matches_materials(
        {"_source_meta": {"client_materials_fingerprint": old_fingerprint}},
        old_fingerprint,
    )
    assert not _distilled_cache_matches_materials(
        {"_source_meta": {"client_materials_fingerprint": old_fingerprint}},
        new_fingerprint,
    )


def test_old_distilled_cache_without_material_lineage_is_invalid_when_materials_exist():
    current_materials = {
        "company_intro": "深圳栖舍设计装修有限公司。",
        "case_studies": [{"client": "刘女士", "result": "老房翻新后满意交付"}],
    }

    current_fingerprint = _client_materials_fingerprint(current_materials)

    assert current_fingerprint
    assert not _distilled_cache_matches_materials(
        {"client_profile": "{}", "selling_points": "{}"},
        current_fingerprint,
    )
    assert _distilled_cache_matches_materials(
        {"client_profile": "{}", "selling_points": "{}"},
        None,
    )


def test_client_materials_prompt_block_exposes_customer_facts_to_article_generation():
    text = _format_client_materials_for_prompt(
        {
            "company_intro": "深圳栖舍专注中高端家装。",
            "unique_value": "以自有施工团队和设计施工一体化交付。",
            "methodology": "施工采用兵将料分离模式。",
            "core_selling_points": ["透明报价", "闭口零增项"],
            "case_studies": [{"client": "刘女士", "result": "160平方半包合同45万"}],
            "testimonials": [{"quote": "验收彻底放心"}],
        }
    )

    assert "深圳栖舍专注中高端家装" in text
    assert "设计施工一体化" in text
    assert "兵将料分离" in text
    assert "透明报价" in text
    assert "刘女士" in text
    assert "验收彻底放心" in text
