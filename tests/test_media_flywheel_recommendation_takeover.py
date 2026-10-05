from __future__ import annotations


def _binding(**overrides):
    row = {
        "id": 7,
        "entity_key": "me_test",
        "industry_key": "real_estate",
        "media_source": "mhz_media",
        "inventory_id": 101,
        "media_name": "房天下",
        "match_confidence": 0.91,
        "evidence": {"reason": "答案采纳来源"},
    }
    row.update(overrides)
    return row


def _inventory(**overrides):
    row = {
        "media_source": "mhz_media",
        "inventory_id": 101,
        "media_name": "房天下",
        "price": 38,
        "our_price_yuan": 30,
        "our_price_points": 300,
        "area": "综合全国",
        "portal_media": "垂直媒体",
        "resource_type_name": "房产家居",
        "geo_rank_platform": "DeepSeek,Kimi",
        "authority_media": 1,
        "is_active": True,
        "entrance_link": "https://example.com",
    }
    row.update(overrides)
    return row


def test_media_flywheel_takeover_requires_feature_switch(monkeypatch):
    from services import media_flywheel_recommendation as rec

    monkeypatch.setattr(rec, "is_feature_enabled", lambda key: False)
    monkeypatch.setattr(rec, "_load_ready_policy", lambda industry_key: {"id": 1, "status": "ready_shadow"})
    monkeypatch.setattr(rec, "_load_flywheel_rows", lambda **kwargs: [_binding()])

    result = rec.recommend_from_media_flywheel(industry="房地产", media_type="media", limit=3)

    assert result["used"] is False
    assert result["reason"] == "media_takeover_disabled"
    assert result["vertical"] == []
    assert result["generic"] == []


def test_media_flywheel_takeover_requires_ready_policy(monkeypatch):
    from services import media_flywheel_recommendation as rec

    monkeypatch.setattr(rec, "is_feature_enabled", lambda key: True)
    monkeypatch.setattr(rec, "_load_ready_policy", lambda industry_key: None)
    monkeypatch.setattr(rec, "_load_flywheel_rows", lambda **kwargs: [_binding()])

    result = rec.recommend_from_media_flywheel(industry="房地产", media_type="media", limit=3)

    assert result["used"] is False
    assert result["reason"] == "media_takeover_policy_not_ready"


def test_media_flywheel_takeover_returns_approved_binding_recommendations(monkeypatch):
    from services import media_flywheel_recommendation as rec

    monkeypatch.setattr(rec, "is_feature_enabled", lambda key: True)
    monkeypatch.setattr(rec, "_load_ready_policy", lambda industry_key: {"id": 3, "status": "ready_shadow"})
    monkeypatch.setattr(rec, "_load_flywheel_rows", lambda **kwargs: [(_binding(), _inventory())])

    result = rec.recommend_from_media_flywheel(industry="房地产", media_type="media", limit=3)

    assert result["used"] is True
    assert result["matched_industry"] == "real_estate"
    assert result["recommendation_source"] == "media_flywheel"
    assert result["generic"] == []
    assert result["vertical"][0]["media_id"] == 101
    assert result["vertical"][0]["media_name"] == "房天下"
    assert result["vertical"][0]["source"] == "media_flywheel"
    assert result["vertical"][0]["flywheel_policy_id"] == 3
    assert "飞轮审核绑定" in result["vertical"][0]["reason"]


def test_media_flywheel_takeover_respects_excluded_media_ids(monkeypatch):
    from services import media_flywheel_recommendation as rec

    monkeypatch.setattr(rec, "is_feature_enabled", lambda key: True)
    monkeypatch.setattr(rec, "_load_ready_policy", lambda industry_key: {"id": 3, "status": "ready_shadow"})
    monkeypatch.setattr(rec, "_load_flywheel_rows", lambda **kwargs: [(_binding(), _inventory())])

    result = rec.recommend_from_media_flywheel(
        industry="房地产",
        media_type="media",
        limit=3,
        exclude_media_ids={101},
    )

    assert result["used"] is False
    assert result["reason"] == "media_flywheel_no_usable_bindings"


def test_publish_v2_calls_media_flywheel_before_legacy_fallback():
    from pathlib import Path

    source = Path("services/placement_service.py").read_text(encoding="utf-8")

    assert "recommend_from_media_flywheel" in source
    assert "media_flywheel" in source
    assert "recommendation_source" in source
