"""
test_cluster_delivery_mapping.py · C4.1 (CTO-15.9 session 3 · 2026-04-25)

P1.3b 主题包 + 交付映射 helper 测试
"""
from __future__ import annotations

import pytest


def test_default_platforms_for_business_tag():
    from services.cluster_delivery_mapping import _default_platforms_for, _BUSINESS_TAG_TO_PLATFORMS
    assert _default_platforms_for("品牌词") == _BUSINESS_TAG_TO_PLATFORMS["品牌词"]
    assert _default_platforms_for("地域词") == _BUSINESS_TAG_TO_PLATFORMS["地域词"]
    assert _default_platforms_for("") == _BUSINESS_TAG_TO_PLATFORMS["通用"]
    assert _default_platforms_for("未知") == _BUSINESS_TAG_TO_PLATFORMS["通用"]


@pytest.mark.parametrize("articles,expected_weeks", [
    (1, 1), (3, 1),
    (4, 2), (7, 2),
    (8, 4), (15, 4),
    (16, 6), (30, 6),
    (31, 8), (100, 8),
])
def test_default_weeks_for_articles(articles, expected_weeks):
    from services.cluster_delivery_mapping import _default_weeks_for
    assert _default_weeks_for(articles) == expected_weeks


def test_apply_delivery_defaults_fills_empty():
    from services.cluster_delivery_mapping import apply_delivery_defaults
    cluster = {"business_tag": "类目词", "articles_standard": 10}
    apply_delivery_defaults(cluster)
    assert cluster["platforms_jsonb"]
    assert cluster["weeks_to_complete"] == 4


def test_apply_delivery_defaults_preserves_existing():
    from services.cluster_delivery_mapping import apply_delivery_defaults
    cluster = {
        "business_tag": "品牌词",
        "platforms_jsonb": ["自定义平台"],
        "weeks_to_complete": 2,
        "articles_standard": 50,
    }
    apply_delivery_defaults(cluster)
    assert cluster["platforms_jsonb"] == ["自定义平台"]  # 不覆盖
    assert cluster["weeks_to_complete"] == 2


def test_format_delivery_table_empty():
    from services.cluster_delivery_mapping import format_delivery_table
    assert format_delivery_table([]) == ""


def test_format_delivery_table_with_data():
    from services.cluster_delivery_mapping import format_delivery_table, apply_delivery_defaults
    clusters = [
        apply_delivery_defaults({
            "cluster_name": "本地装修主题包",
            "business_tag": "地域词",
            "core_keyword_count": 5,
            "articles_standard": 12,
            "price_standard": 15000,
        })
    ]
    md = format_delivery_table(clusters)
    assert "本地装修主题包" in md
    assert "12 篇" in md
    assert "¥15,000" in md
    assert "4 周" in md  # 12 篇 → 4 周
    assert "大众点评" in md  # 地域词推 大众点评


def test_calculate_total_weeks_max():
    from services.cluster_delivery_mapping import calculate_total_weeks
    clusters = [
        {"weeks_to_complete": 2},
        {"weeks_to_complete": 6},
        {"weeks_to_complete": 4},
    ]
    assert calculate_total_weeks(clusters) == 6  # 取最长瓶颈


def test_calculate_total_weeks_empty():
    from services.cluster_delivery_mapping import calculate_total_weeks
    assert calculate_total_weeks([]) == 0
    assert calculate_total_weeks([{"weeks_to_complete": 0}]) == 0


def test_get_cluster_delivery_summary():
    from services.cluster_delivery_mapping import get_cluster_delivery_summary, apply_delivery_defaults
    clusters = [
        apply_delivery_defaults({"business_tag": "品牌词", "articles_standard": 5, "price_standard": 8000}),
        apply_delivery_defaults({"business_tag": "类目词", "articles_standard": 12, "price_standard": 15000}),
    ]
    s = get_cluster_delivery_summary(clusters)
    assert s["cluster_count"] == 2
    assert s["total_articles"] == 17
    assert s["total_price"] == 23000
    assert s["total_weeks"] == 4  # 5篇=2周 vs 12篇=4周 · max=4
    assert "百度百科" in s["all_platforms"] or "知乎" in s["all_platforms"]


def test_summary_empty():
    from services.cluster_delivery_mapping import get_cluster_delivery_summary
    s = get_cluster_delivery_summary([])
    assert s["cluster_count"] == 0
    assert s["total_articles"] == 0
    assert s["all_platforms"] == []


def test_format_delivery_handles_string_platforms_jsonb():
    """DB 返 JSONB 时可能是 str · 解析后再 join"""
    from services.cluster_delivery_mapping import format_delivery_table
    import json as _json
    clusters = [{
        "cluster_name": "x",
        "business_tag": "类目词",
        "core_keyword_count": 3,
        "articles_standard": 5,
        "price_standard": 5000,
        "weeks_to_complete": 2,
        "platforms_jsonb": _json.dumps(["知乎", "百家号"], ensure_ascii=False),
    }]
    md = format_delivery_table(clusters)
    assert "知乎" in md
    assert "百家号" in md
