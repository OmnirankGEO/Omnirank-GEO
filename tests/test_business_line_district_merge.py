import pytest

from tools.keyword_cluster import (
    _extract_product_terms_from_keywords,
    _merge_geo_variant_business_lines,
    _normalize_business_lines_with_id_map,
    _validate_business_lines_coverage,
)


def test_product_terms_strip_city_and_district_prefixes():
    products = _extract_product_terms_from_keywords([
        "深圳福田区全屋定制哪家靠谱",
        "深圳罗湖区榻榻米定制推荐",
    ])

    assert "福田区全屋定制" not in products
    assert "罗湖区榻榻米定制" not in products
    assert "全屋定制" in products
    assert "榻榻米定制" in products


def test_validator_merges_district_longtails_into_declared_parent_business():
    business_lines = [{
        "id": 1,
        "name": "全屋定制设计",
        "description": "为深圳新房、旧房翻新、别墅等提供整体木作定制方案，涵盖衣柜、榻榻米、酒柜、餐边柜等全屋空间设计",
        "example_scenarios": ["深圳新房全屋木作定制推荐"],
        "is_selected": False,
    }]

    result = _validate_business_lines_coverage(
        business_lines,
        [
            "深圳福田区全屋定制哪家靠谱",
            "深圳罗湖区榻榻米定制推荐",
        ],
    )

    assert [line["name"] for line in result] == ["全屋定制设计"]
    assert "深圳福田区全屋定制哪家靠谱" in result[0]["example_scenarios"]
    assert "深圳罗湖区榻榻米定制推荐" in result[0]["example_scenarios"]


def test_merges_llm_geo_variant_business_line_into_parent():
    result = _merge_geo_variant_business_lines([
        {
            "id": 1,
            "name": "全屋定制设计",
            "description": "涵盖衣柜、榻榻米、酒柜、餐边柜等全屋空间设计",
            "example_scenarios": ["深圳新房全屋木作定制推荐"],
            "is_selected": False,
        },
        {
            "id": 2,
            "name": "福田区全屋定制",
            "description": "福田区全屋定制相关业务",
            "example_scenarios": ["深圳福田区全屋定制哪家靠谱"],
            "is_selected": False,
        },
    ])

    assert [line["name"] for line in result] == ["全屋定制设计"]
    assert "深圳福田区全屋定制哪家靠谱" in result[0]["example_scenarios"]


def test_merges_value_modifier_business_line_into_parent():
    result = _merge_geo_variant_business_lines([
        {
            "id": 1,
            "name": "全屋定制设计",
            "description": "提供全屋定制设计方案",
            "example_scenarios": ["深圳全屋定制案例实景"],
            "is_selected": False,
        },
        {
            "id": 2,
            "name": "高性价比全屋定制",
            "description": "高性价比全屋定制相关业务",
            "example_scenarios": ["高性价比全屋定制"],
            "is_selected": False,
        },
    ])

    assert [line["name"] for line in result] == ["全屋定制设计"]
    assert "高性价比全屋定制" in result[0]["example_scenarios"]


def test_merges_included_whole_house_subservice_into_parent():
    result = _merge_geo_variant_business_lines([
        {
            "id": 1,
            "name": "全屋定制设计",
            "description": "提供整体木作定制方案，涵盖衣柜、榻榻米、酒柜、餐边柜等全屋空间设计",
            "example_scenarios": ["深圳全屋定制案例实景"],
            "is_selected": False,
        },
        {
            "id": 2,
            "name": "衣柜定制",
            "description": "提供深圳地区衣柜定制服务",
            "example_scenarios": ["深圳衣柜定制推荐"],
            "is_selected": False,
        },
    ])

    assert [line["name"] for line in result] == ["全屋定制设计"]
    assert "深圳衣柜定制推荐" in result[0]["example_scenarios"]


def test_returns_old_to_new_business_line_id_map_for_legacy_sessions():
    result, id_map = _normalize_business_lines_with_id_map([
        {
            "id": 7,
            "name": "全屋定制设计",
            "description": "涵盖衣柜、榻榻米等全屋空间设计",
            "example_scenarios": [],
            "is_selected": False,
        },
        {
            "id": 9,
            "name": "福田区全屋定制",
            "description": "福田区全屋定制相关业务",
            "example_scenarios": ["深圳福田区全屋定制哪家靠谱"],
            "is_selected": False,
        },
    ])

    assert [line["id"] for line in result] == [1]
    assert id_map == {7: 1, 9: 1}


def test_numeric_string_business_line_ids_are_mapped_for_legacy_sessions():
    result, id_map = _normalize_business_lines_with_id_map([
        {"id": "10", "name": "企业AI定向人才培养", "description": ""},
        {"id": "20", "name": "大数据开发实训", "description": ""},
    ])

    assert [line["id"] for line in result] == [1, 2]
    assert id_map == {10: 1, 20: 2}


def test_duplicate_business_line_ids_are_rejected_as_ambiguous():
    with pytest.raises(ValueError, match="duplicate business line id"):
        _normalize_business_lines_with_id_map([
            {"id": 7, "name": "企业AI定向人才培养", "description": ""},
            {"id": "7", "name": "大数据开发实训", "description": ""},
        ])


def test_auto_supplemented_quote_noise_maps_back_to_existing_businesses():
    result, id_map = _normalize_business_lines_with_id_map([
        {
            "id": 1,
            "name": "全屋定制设计安装",
            "description": "为深圳家庭提供从测量、设计到安装的一站式全屋定制服务，涵盖各空间柜体定制。",
            "example_scenarios": ["深圳全屋定制推荐"],
            "is_selected": False,
        },
        {
            "id": 2,
            "name": "榻榻米定制",
            "description": "针对小户型、多功能房等空间定制榻榻米，含升降台、储物及被褥收纳设计。",
            "example_scenarios": ["深圳榻榻米定制推荐"],
            "is_selected": False,
        },
        {
            "id": 3,
            "name": "餐边柜定制",
            "description": "定制餐边柜，可集成冰箱、电器等功能，提升餐厅收纳与美观。",
            "example_scenarios": ["深圳定制餐边柜推荐"],
            "is_selected": False,
        },
        {
            "id": 4,
            "name": "定制家具性价比方案",
            "description": "提供高性价比的全屋定制家具方案，对比成品家具与木工打柜，优化预算。",
            "example_scenarios": ["深圳全屋定制高性价比推荐"],
            "is_selected": False,
        },
        {
            "id": 13,
            "name": "全屋定制公司口碑",
            "description": "全屋定制公司口碑相关业务",
            "example_scenarios": ["全屋定制公司口碑"],
            "is_selected": False,
            "_auto_supplemented": True,
        },
        {
            "id": 32,
            "name": "榻榻米被褥定制",
            "description": "榻榻米被褥定制相关业务",
            "example_scenarios": ["榻榻米被褥定制"],
            "is_selected": False,
            "_auto_supplemented": True,
        },
        {
            "id": 33,
            "name": "餐边柜全屋定制安装",
            "description": "餐边柜全屋定制安装相关业务",
            "example_scenarios": ["餐边柜全屋定制安装"],
            "is_selected": False,
            "_auto_supplemented": True,
        },
        {
            "id": 27,
            "name": "定制家具和木工打柜子哪个划算",
            "description": "定制家具和木工打柜子哪个划算相关业务",
            "example_scenarios": ["定制家具和木工打柜子哪个划算"],
            "is_selected": False,
            "_auto_supplemented": True,
        },
    ])

    assert [line["name"] for line in result] == [
        "全屋定制设计安装",
        "榻榻米定制",
        "餐边柜定制",
        "定制家具性价比方案",
    ]
    assert id_map[13] == 1
    assert id_map[32] == 2
    assert id_map[33] == 3
    assert id_map[27] == 4


def test_renames_standalone_geo_business_line_to_service_name():
    result = _merge_geo_variant_business_lines([
        {
            "id": 1,
            "name": "福田区全屋定制",
            "description": "福田区全屋定制相关业务",
            "example_scenarios": ["深圳福田区全屋定制哪家靠谱"],
            "is_selected": False,
        },
    ])

    assert result[0]["name"] == "全屋定制"


def test_product_coverage_does_not_match_only_generic_prefix():
    from tools.keyword_cluster import _is_product_covered

    assert _is_product_covered("全屋定制", "全屋定制设计服务")
    assert _is_product_covered("榻榻米定制", "涵盖衣柜、榻榻米、酒柜")
    assert not _is_product_covered("全屋定制", "全屋保洁服务")


def test_validator_still_supplements_distinct_elevator_products():
    result = _validate_business_lines_coverage(
        [
            {
                "id": 1,
                "name": "观光电梯销售安装",
                "description": "观光电梯相关业务",
                "example_scenarios": [],
                "is_selected": False,
            },
            {
                "id": 2,
                "name": "载货电梯销售安装",
                "description": "载货电梯相关业务",
                "example_scenarios": [],
                "is_selected": False,
            },
        ],
        ["深圳观光电梯", "深圳别墅电梯", "深圳载货电梯"],
    )

    assert [line["name"] for line in result] == [
        "观光电梯销售安装",
        "载货电梯销售安装",
        "别墅电梯",
    ]
