"""[B5] 回归:
- B5-1 SSOT 公式 + 效果回流(citation_lift → 回写 outcome_rollup + 重算 shadow · 幂等 · 无信号零副作用)。
- B5-2 v1 flag 关走原路径(零变化)· flag 开走含融合的 v2 细排。
"""
import pytest

import db.diagnosis_db  # noqa: F401  建 geo_research_raw / geo_engine_stats


# ---------- B5-1 SSOT 公式 ----------
def test_outcome_and_blend_ssot():
    from services.media_entity_flywheel import outcome_score_from_rollup, blend_shadow_score
    # outcome_score = min(100, published*8 + max(0,lift)*12)
    assert outcome_score_from_rollup({"published_count": 2, "citation_lift_30d": 3}) == pytest.approx(2 * 8 + 3 * 12)
    assert outcome_score_from_rollup({"published_count": 0, "citation_lift_30d": -5}) == 0.0  # 负 lift 不减
    assert outcome_score_from_rollup({"published_count": 20, "citation_lift_30d": 0}) == 100.0  # 封顶
    assert outcome_score_from_rollup(None) == 0.0
    # blend: evidence .52 + quality .18 + inventory .20 + outcome .10
    assert blend_shadow_score(100, 100, 100, 100) == pytest.approx(100.0)
    assert blend_shadow_score(50, 40, 30, 0) == pytest.approx(50 * 0.52 + 40 * 0.18 + 30 * 0.20)


# ---------- B5-1 效果回流(真跑 PG) ----------
def _insert_entity_snapshot(conn, domain, ev, q, inv):
    c = conn.cursor()
    c.execute("INSERT INTO geo_media_entities (entity_key, canonical_name, domain) VALUES (%s,%s,%s) RETURNING id",
              (f"k-{domain}", domain, domain))
    eid = c.fetchone()["id"]
    from services.media_entity_flywheel import blend_shadow_score
    shadow = blend_shadow_score(ev, q, inv, 0)
    c.execute("""INSERT INTO media_entity_score_snapshots
                   (entity_id, industry_key, score_version, shadow_score, evidence_score, quality_score,
                    inventory_score, outcome_score, reference_status, is_purchasable, reasons, evidence, created_at)
                 VALUES (%s,'general','v1',%s,%s,%s,%s,0,'reference_only',FALSE,'[]'::jsonb,'{}'::jsonb, NOW())
                 RETURNING id""", (eid, shadow, ev, q, inv))
    sid = c.fetchone()["id"]
    conn.commit()
    return eid, sid


def _seed_citations(conn, platform, recent_n):
    c = conn.cursor()
    for i in range(recent_n):
        c.execute("""INSERT INTO geo_research_raw (industry, query, engine, cited_platform, cite_position, created_at)
                     VALUES ('测试','q','doubao',%s,1, NOW())""", (platform,))
    conn.commit()


def test_outcome_sync_writes_rollup_and_is_idempotent(db_with_clean_research):
    from db.media_entity_flywheel_db import init_media_entity_flywheel_tables
    init_media_entity_flywheel_tables()
    from services.media_outcome_sync import sync_media_entity_outcome_rollups
    conn = db_with_clean_research
    c = conn.cursor()
    c.execute("DELETE FROM media_entity_score_snapshots")
    c.execute("DELETE FROM geo_media_entities")
    conn.commit()

    eid, sid = _insert_entity_snapshot(conn, "testmedia.com", ev=50, q=40, inv=30)
    _seed_citations(conn, "testmedia.com", recent_n=4)  # 近窗 4 次,前窗 0 → lift=4

    res = sync_media_entity_outcome_rollups(window_days=30)
    assert res["updated"] >= 1

    c.execute("SELECT outcome_score, shadow_score, evidence FROM media_entity_score_snapshots WHERE id=%s", (sid,))
    row = c.fetchone()
    # published_count=0(mhz 表在测试库缺→fail-soft 0), lift=4 → outcome = 4*12 = 48
    assert float(row["outcome_score"]) == pytest.approx(48.0, abs=0.01)
    from services.media_entity_flywheel import blend_shadow_score
    assert float(row["shadow_score"]) == pytest.approx(blend_shadow_score(50, 40, 30, 48.0), abs=0.01)
    assert row["evidence"]["outcome_rollup"]["citation_lift_30d"] == 4

    # 幂等:重跑同样数据 → outcome 不变(不双计)
    sync_media_entity_outcome_rollups(window_days=30)
    c.execute("SELECT outcome_score FROM media_entity_score_snapshots WHERE id=%s", (sid,))
    assert float(c.fetchone()["outcome_score"]) == pytest.approx(48.0, abs=0.01)


def test_outcome_sync_no_signal_no_change(db_with_clean_research):
    from db.media_entity_flywheel_db import init_media_entity_flywheel_tables
    init_media_entity_flywheel_tables()
    from services.media_outcome_sync import sync_media_entity_outcome_rollups
    conn = db_with_clean_research
    c = conn.cursor()
    c.execute("DELETE FROM media_entity_score_snapshots")
    c.execute("DELETE FROM geo_media_entities")
    conn.commit()
    eid, sid = _insert_entity_snapshot(conn, "nosignal.com", ev=50, q=40, inv=30)
    # 无引用、无发布 → 不更新
    sync_media_entity_outcome_rollups(window_days=30)
    c.execute("SELECT outcome_score, evidence FROM media_entity_score_snapshots WHERE id=%s", (sid,))
    row = c.fetchone()
    assert float(row["outcome_score"]) == 0.0
    assert "outcome_rollup" not in (row["evidence"] or {})


# ---------- v1 路径(blend 已退役 2026-07-03 收口)----------
def test_v1_returns_list_no_crash(monkeypatch):
    """[彻底修 2026-07-03] v1 走原路径。历史 NameError(recommendations/mhz_media 未初始化)已修:
    返回 list 不再抛,publish_api /media/recommend(无 try 兜底)不再 500。"""
    import db.publish_db as pub
    from services import placement_service as ps
    # 测试库无 mhz_media 表 → 媒体池置空,专测"不再 NameError + 返回 list"(不测交叉匹配内容)。
    monkeypatch.setattr(pub, "get_media_list", lambda **kw: {"media": []})
    out = ps.recommend_for_publish(industry="教育培训")
    assert isinstance(out, list)


def test_v1_blend_routing_retired(monkeypatch):
    """[blend 退役] flywheel_score_blend 删除后:即便 is_feature_enabled 恒 True + 白名单,
    recommend_for_publish 也不再路由到 v2 融合壳(_recommend_for_publish_blended 已删),
    始终走原 v1 路径,输出无 flywheel_blend。"""
    import db.publish_db as pub
    from services import placement_service as ps
    monkeypatch.setattr("writing.feature_switches.is_feature_enabled", lambda k: True)
    monkeypatch.setenv("FLYWHEEL_BLEND_INDUSTRIES", "教育培训")
    called = {"v2": False}

    def flag_v2(**kw):
        called["v2"] = True
        return {"vertical": [], "generic": []}

    monkeypatch.setattr(ps, "recommend_for_publish_v2", flag_v2)
    monkeypatch.setattr(pub, "get_media_list", lambda **kw: {"media": []})
    out = ps.recommend_for_publish(industry="教育培训", limit=8)
    assert isinstance(out, list)
    assert called["v2"] is False, "blend 退役后 v1 不应再路由到 v2 融合壳"
    assert not any(r.get("flywheel_blend") for r in out), "输出不应有 flywheel_blend"
    assert not hasattr(ps, "_recommend_for_publish_blended")
    assert not hasattr(ps, "_resolve_flywheel_blend")
