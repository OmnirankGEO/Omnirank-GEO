"""[B6] 回归:
- B6-1 接管路径 _build_recommendation 改读 shadow_score(回退 match_confidence)+ 批量取分。
- B6-2 引擎加权观测分:flag 关时评分输出逐字节一致(无观测字段·total_score 不变)、flag 开时多观测字段。
"""
import json

import pytest


# ---------- B6-1 接管路径读 shadow_score ----------
def test_build_recommendation_prefers_shadow_score():
    from services.media_flywheel_recommendation import _build_recommendation
    inv = {"inventory_id": 1, "media_name": "M", "price": 50}
    # 有 shadow_score=90 → geo_score/quality 用 shadow,不用 match_confidence(0.5)
    rec = _build_recommendation(
        binding={"match_confidence": 0.5, "entity_key": "e", "shadow_score": 90},
        inventory=inv, policy_id=1, media_type="media")
    assert rec["geo_score"] == pytest.approx(90.0)
    assert rec["quality_score_v2f"] == pytest.approx(0.9)
    assert rec["flywheel_shadow_score"] == 90.0
    assert rec["flywheel_match_confidence"] == pytest.approx(0.5)


def test_build_recommendation_falls_back_to_match_confidence():
    from services.media_flywheel_recommendation import _build_recommendation
    inv = {"inventory_id": 1, "media_name": "M", "price": 50}
    rec = _build_recommendation(
        binding={"match_confidence": 0.5, "entity_key": "e"},  # 无 shadow_score
        inventory=inv, policy_id=1, media_type="media")
    assert rec["geo_score"] == pytest.approx(50.0)
    assert rec["quality_score_v2f"] == pytest.approx(0.5)
    assert rec["flywheel_shadow_score"] is None


def test_get_shadow_scores_by_entity_keys_real_pg(db_with_clean_research):
    from db.media_entity_flywheel_db import init_media_entity_flywheel_tables, get_shadow_scores_by_entity_keys
    init_media_entity_flywheel_tables()
    conn = db_with_clean_research
    c = conn.cursor()
    c.execute("DELETE FROM media_entity_score_snapshots")
    c.execute("DELETE FROM geo_media_entities")
    c.execute("INSERT INTO geo_media_entities (entity_key, canonical_name) VALUES ('ek1','A') RETURNING id")
    eid = c.fetchone()["id"]
    c.execute("""INSERT INTO media_entity_score_snapshots
                   (entity_id, industry_key, score_version, shadow_score, reference_status, is_purchasable, created_at)
                 VALUES (%s,'general','v1',77.5,'reference_only',FALSE, NOW())""", (eid,))
    conn.commit()
    smap = get_shadow_scores_by_entity_keys(["ek1", "missing"], "general")
    assert smap.get("ek1") == pytest.approx(77.5)
    assert "missing" not in smap


# ---------- B6-2 引擎加权观测分 ----------
def test_weighted_brand_ownership_shadow_math(monkeypatch):
    from tools.scoring import geo_scorer
    monkeypatch.setattr(
        "services.placement_service.PlacementService._get_engine_weights",
        lambda self: {"豆包": 0.35, "Kimi": 0.25, "DeepSeek": 0.22, "千问": 0.18},
    )
    # 只有豆包检出 → 加权检出率 = 0.35/(0.35+0.25+0.22) → *10
    data = {"results": [
        {"engine": "doubao", "brand_detected": True},
        {"engine": "kimi", "brand_detected": False},
        {"engine": "deepseek", "brand_detected": False},
    ]}
    w = geo_scorer._weighted_brand_ownership_shadow(data)
    assert w == pytest.approx(0.35 / (0.35 + 0.25 + 0.22) * 10, abs=0.05)
    # 无 results → None
    assert geo_scorer._weighted_brand_ownership_shadow({"results": []}) is None
    assert geo_scorer._weighted_brand_ownership_shadow({}) is None


async def _run_scorer(ai_vis):
    from tools.scoring.geo_scorer import calculate_geo_score
    resp = await calculate_geo_score(ai_visibility_data=ai_vis, brand_name="测试品牌")
    return json.loads(resp.content[0]["text"])


@pytest.mark.asyncio
async def test_scorer_flag_off_byte_identical_and_flag_on_adds_shadow(monkeypatch):
    ai_vis = {
        "brand_detected_count": 1, "total_engines": 3, "detected_count": 1, "total_tests": 3,
        "dimension_stats": {},
        "results": [
            {"engine": "doubao", "brand_detected": True},
            {"engine": "kimi", "brand_detected": False},
            {"engine": "deepseek", "brand_detected": False},
        ],
    }

    # flag 关 → 无观测字段
    monkeypatch.setattr("writing.feature_switches.is_feature_enabled", lambda k: False)
    off = await _run_scorer(ai_vis)
    assert "brand_ownership_score_weighted_shadow" not in off["dimension_scores"]
    total_off = off["total_score"]
    bo_off = off["dimension_scores"]["brand_ownership_score"]

    # flag 开 → 多观测字段;生产分/总分不变
    monkeypatch.setattr("writing.feature_switches.is_feature_enabled",
                        lambda k: k == "diagnosis_engine_weight_shadow")
    monkeypatch.setattr(
        "services.placement_service.PlacementService._get_engine_weights",
        lambda self: {"豆包": 0.35, "Kimi": 0.25, "DeepSeek": 0.22, "千问": 0.18},
    )
    on = await _run_scorer(ai_vis)
    assert "brand_ownership_score_weighted_shadow" in on["dimension_scores"]
    assert on["total_score"] == total_off                      # 总分不变
    assert on["dimension_scores"]["brand_ownership_score"] == bo_off  # 生产分不变
