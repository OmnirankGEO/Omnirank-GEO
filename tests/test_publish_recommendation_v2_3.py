from pathlib import Path

from services.article_type import ArticleType, normalize_article_type
from services.publish_recommendation import (
    MONITORING_OUTCOME_SQL,
    build_recommendation_packages,
    classify_media_tier,
    history_feedback_weight,
    is_publish_snapshot_required,
    score_media_candidate,
)


ROOT = Path(__file__).resolve().parents[1]


def _media(mid, name, price=50, industry="房地产", portal="门户网站", geo_rank=1, authority=0):
    return {
        "id": mid,
        "media_id": mid,
        "media_source": "media",
        "media_name": name,
        "platform_name": name,
        "industry": industry,
        "price": price,
        "our_price_yuan": price,
        "portal_media": portal,
        "geo_rank": geo_rank,
        "pc_weight": 3,
        "m_weight": 2,
        "authority_media": authority,
        "inclusion_rate": 70,
        "citation_rate": 0.45,
    }


def _names(package):
    return [item["media_name"] for item in package["items"]]


def _has_any_name(names, anchors):
    normalized_names = [name.replace(" ", "") for name in names]
    return any(anchor.replace(" ", "") in name for anchor in anchors for name in normalized_names)


def _bad_candidates(start_id=900, industry="房地产"):
    return [
        _media(start_id, "随机任选包收录", price=20, industry=industry, portal="门户网站", geo_rank=1),
        _media(start_id + 1, "十元专区套餐系列", price=10, industry=industry, portal="门户网站", geo_rank=1),
        _media(start_id + 2, "低价小站", price=4, industry=industry, portal="门户网站", geo_rank=1),
        _media(start_id + 3, "异常天价媒体", price=100001, industry=industry, portal="门户网站", geo_rank=1),
        _media(start_id + 4, "双信号缺失媒体", price=50, industry=industry, portal="", geo_rank=None),
    ]


def _assert_clean_package(package):
    assert package["items"], package
    for item in package["items"]:
        name = item["media_name"]
        assert item["evidence"], item
        assert item["risk_note"] == ""
        for forbidden in ["套餐系列", "随机", "任选", "秒杀", "包收录", "十元专区", "小站", "低价", "包月套餐"]:
            assert forbidden not in name
        assert 5 <= item["price_yuan"] <= 100000


def test_article_type_normalization_is_ssot_and_defaults_safely():
    assert normalize_article_type("案例故事") == ArticleType.CASE_STORY
    assert normalize_article_type("policy_trend") == ArticleType.POLICY_TREND
    assert normalize_article_type("unknown-from-llm") == ArticleType.PRODUCT_PROMOTE


def test_media_scoring_excludes_noise_prices_and_dual_signal_missing():
    noisy = score_media_candidate(_media(1, "十元专区套餐系列", price=10))
    too_cheap = score_media_candidate(_media(2, "正常媒体", price=4))
    too_expensive = score_media_candidate(_media(3, "正常媒体", price=100001))
    missing_signals = score_media_candidate(_media(4, "正常媒体", portal="", geo_rank=None))
    good = score_media_candidate(_media(5, "房天下", price=80, portal="垂直媒体", geo_rank=3))

    assert noisy["is_recommendable"] is False
    assert too_cheap["is_recommendable"] is False
    assert too_expensive["is_recommendable"] is False
    assert missing_signals["is_recommendable"] is False
    assert good["is_recommendable"] is True
    assert classify_media_tier(good) in {"L1", "L2"}


def test_history_weight_uses_sigmoid_ramp_instead_of_hard_switch():
    assert history_feedback_weight(500) < 0.002
    assert 0.045 <= history_feedback_weight(1000) <= 0.055
    assert history_feedback_weight(1500) > 0.095


def test_industry_packages_keep_positive_anchors_and_filter_negative_examples():
    candidates = [
        _media(1, "房天下", price=80, industry="房地产", portal="垂直媒体", geo_rank=2),
        _media(2, "吉屋", price=60, industry="房地产", portal="垂直媒体", geo_rank=2),
        _media(3, "和讯网", price=120, industry="房地产", portal="门户网站", geo_rank=1),
        _media(4, "随机任选包收录", price=20, industry="房地产", portal="门户网站", geo_rank=1),
        _media(5, "低价小站", price=4, industry="房地产", portal="门户网站", geo_rank=1),
    ]

    payload = build_recommendation_packages(
        article_summary={
            "industry": "房地产",
            "article_type": "product_promote",
            "semantic_keywords": ["楼盘", "住宅"],
        },
        candidates=candidates,
        recommendation_level=3,
    )

    all_names = [item["media_name"] for pkg in payload["packages"] for item in pkg["items"]]
    assert any(name in all_names for name in ["房天下", "吉屋", "和讯网"])
    assert "随机任选包收录" not in all_names
    assert "低价小站" not in all_names


def test_five_industry_three_packages_meet_positive_and_negative_acceptance():
    cases = [
        {
            "industry": "房地产",
            "anchors": ["房天下", "吉屋", "和讯网"],
            "candidates": [
                _media(101, "房天下", price=30, industry="房地产", portal="垂直媒体", geo_rank=3),
                _media(102, "吉屋", price=40, industry="房地产", portal="垂直媒体", geo_rank=3),
                _media(103, "和讯网", price=50, industry="房地产", portal="门户网站", geo_rank=2),
                _media(104, "地方房产门户", price=60, industry="房地产", portal="门户网站", geo_rank=2),
            ],
        },
        {
            "industry": "汽车",
            "anchors": ["懂车帝", "汽车之家", "太平洋汽车"],
            "candidates": [
                _media(201, "懂车帝", price=30, industry="汽车", portal="垂直媒体", geo_rank=3),
                _media(202, "汽车之家", price=40, industry="汽车", portal="垂直媒体", geo_rank=3),
                _media(203, "太平洋汽车", price=50, industry="汽车", portal="垂直媒体", geo_rank=2),
                _media(204, "易车", price=60, industry="汽车", portal="垂直媒体", geo_rank=2),
            ],
        },
        {
            "industry": "GEO/企业服务",
            "anchors": ["CSDN", "IT之家", "IT 之家", "搜狐", "知乎"],
            "candidates": [
                _media(301, "CSDN", price=30, industry="GEO/企业服务", portal="技术社区", geo_rank=3),
                _media(302, "IT 之家", price=40, industry="GEO/企业服务", portal="科技媒体", geo_rank=3),
                _media(303, "知乎", price=50, industry="GEO/企业服务", portal="UGC社区", geo_rank=2),
                _media(304, "搜狐", price=60, industry="GEO/企业服务", portal="门户网站", geo_rank=2),
            ],
        },
        {
            "industry": "医疗健康",
            "anchors": ["健康", "医疗", "政府", "人民网", "新华网"],
            "requires": {
                "vertical": ["健康界", "医疗"],
                "authority": ["政府", "人民网", "新华网"],
            },
            "candidates": [
                _media(401, "健康界", price=30, industry="医疗健康", portal="医疗垂直", geo_rank=3),
                _media(402, "医疗垂直网", price=40, industry="医疗健康", portal="医疗垂直", geo_rank=3),
                _media(403, "人民网健康", price=50, industry="医疗健康", portal="政府/权威", geo_rank=2, authority=1),
                _media(404, "新华网健康", price=60, industry="医疗健康", portal="政府/权威", geo_rank=2, authority=1),
            ],
        },
        {
            "industry": "教育培训",
            "anchors": ["中国教育在线", "环球网校", "新东方"],
            "candidates": [
                _media(501, "中国教育在线", price=30, industry="教育培训", portal="教育垂直", geo_rank=3),
                _media(502, "环球网校", price=40, industry="教育培训", portal="教育垂直", geo_rank=3),
                _media(503, "新东方", price=50, industry="教育培训", portal="教育垂直", geo_rank=2),
                _media(504, "搜狐教育", price=60, industry="教育培训", portal="门户网站", geo_rank=2),
            ],
        },
    ]

    for case in cases:
        candidates = case["candidates"] + _bad_candidates(industry=case["industry"])
        payload = build_recommendation_packages(
            article_summary={
                "industry": case["industry"],
                "article_type": "product_promote",
                "semantic_keywords": [case["industry"], "品牌可信度"],
            },
            candidates=candidates,
            recommendation_level=3,
        )

        assert {pkg["package_type"] for pkg in payload["packages"]} == {"trial", "balanced", "authority"}
        for package in payload["packages"]:
            _assert_clean_package(package)
            names = _names(package)
            assert _has_any_name(names, case["anchors"])
            if case.get("requires"):
                assert _has_any_name(names, case["requires"]["vertical"])
                assert _has_any_name(names, case["requires"]["authority"])


def test_publish_batch_requires_snapshot_before_real_submission():
    assert is_publish_snapshot_required({"items": [{"article_id": 1}], "snapshot_id": 42}) is False
    assert is_publish_snapshot_required({"items": [{"article_id": 1}]}) is True


def test_monitoring_outcome_sql_uses_real_table_and_engine_names():
    assert "monitoring_results" in MONITORING_OUTCOME_SQL
    assert "monitoring_records" not in MONITORING_OUTCOME_SQL
    assert "qwen3-max" not in MONITORING_OUTCOME_SQL
    for engine in ("dashscope", "deepseek", "kimi", "doubao"):
        assert engine in MONITORING_OUTCOME_SQL


def test_placement_ui_removes_risky_promise_copy_and_links_publish_ledger():
    recommendation_text = (ROOT / "frontend/src/pages/Writing/PlacementRecommendation.tsx").read_text(encoding="utf-8")
    center_text = (ROOT / "frontend/src/pages/Placement/PlacementCenter.tsx").read_text(encoding="utf-8")
    combined = recommendation_text + center_text

    for forbidden in [
        "匹配最优",
        "全面覆盖",
        "不限价格",
        "最大化AI引擎覆盖",
        "AI 智能匹配投放平台",
        "全部推荐平台，请操作员按序执行投放",
    ]:
        assert forbidden not in combined

    assert "/api/publish/outcomes" in center_text
    assert "/api/publish/decision-snapshots" in center_text
    assert "ai_citations_delta_30d" in center_text
    assert "failed_reason" in center_text


def test_placement_service_exposes_publish_ledger_lookup_helpers():
    service_text = (ROOT / "services/placement_service.py").read_text(encoding="utf-8")

    assert "def get_publish_outcome_records" in service_text
    assert "def get_publish_decision_snapshots" in service_text
    assert "推荐最优投放平台" not in service_text
    assert "请务必以此为投放依据" not in service_text
    assert "全面覆盖" not in service_text
    assert "最大化引擎覆盖" not in service_text


def test_publish_recommendation_cache_copy_does_not_claim_ai_unavailable():
    publish_api_text = (ROOT / "api/publish_api.py").read_text(encoding="utf-8")

    assert "AI 暂不可用·已用 {age_days} 天前数据" not in publish_api_text
    assert "已使用 {age_days} 天前 AI 分析缓存" in publish_api_text
    assert "实时 AI 暂不可用，已使用历史推荐池" in publish_api_text


def test_publish_center_keeps_ai_recommendation_as_helper_layer():
    center_text = (ROOT / "frontend/src/pages/Publishing/PublishCenter.tsx").read_text(encoding="utf-8")

    assert "L{deepAnalysis.recommendation_level}" not in center_text
    assert "recommendationSourceLabel" in center_text
    assert "重新分析" in center_text
    assert "隐藏分析" in center_text
    assert "const showAdvancedMediaSearch = true" in center_text
