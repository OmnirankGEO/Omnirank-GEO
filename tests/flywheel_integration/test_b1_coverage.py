"""[B1-5] 回归:get_media_flywheel_coverage 按实体去重的同分母口径。

验证 DISTINCT ON (entity_id) 取最新一条快照:同实体多行快照只计一次,
且以"最新"那条的 reference_status/is_purchasable 为准。
"""
import db.media_entity_flywheel_db as mdb


def _clean_media(conn):
    c = conn.cursor()
    c.execute("DELETE FROM media_entity_score_snapshots")
    c.execute("DELETE FROM geo_media_entities")
    conn.commit()


def test_coverage_distinct_denominator(db_with_clean_research):
    mdb.init_media_entity_flywheel_tables()
    conn = db_with_clean_research
    _clean_media(conn)
    c = conn.cursor()

    c.execute("INSERT INTO geo_media_entities (entity_key, canonical_name) VALUES ('e-a','A') RETURNING id")
    aid = c.fetchone()["id"]
    c.execute("INSERT INTO geo_media_entities (entity_key, canonical_name) VALUES ('e-b','B') RETURNING id")
    bid = c.fetchone()["id"]

    # 实体 A 两条快照(跨行业):较早 general=purchasable,较新 GEO=reference_only。
    # 去重取最新 → A 记为 reference_only(不是 purchasable)。
    c.execute(
        """INSERT INTO media_entity_score_snapshots
               (entity_id, industry_key, score_version, shadow_score, reference_status, is_purchasable, created_at)
           VALUES (%s,'general','v1',80,'purchasable',TRUE, NOW() - INTERVAL '1 day')""",
        (aid,),
    )
    c.execute(
        """INSERT INTO media_entity_score_snapshots
               (entity_id, industry_key, score_version, shadow_score, reference_status, is_purchasable, created_at)
           VALUES (%s,'GEO','v1',70,'reference_only',FALSE, NOW())""",
        (aid,),
    )
    # 实体 B 一条快照 reference_only
    c.execute(
        """INSERT INTO media_entity_score_snapshots
               (entity_id, industry_key, score_version, shadow_score, reference_status, is_purchasable, created_at)
           VALUES (%s,'general','v1',60,'reference_only',FALSE, NOW())""",
        (bid,),
    )
    conn.commit()

    cov = mdb.get_media_flywheel_coverage()

    # 旧口径:快照行数 = 3(保留,向后兼容)
    assert cov["score_snapshots"] == 3
    # 新去重口径:已评分去重实体 = 2
    assert cov["scored_entities"] == 2
    # A 取最新(reference_only)、B reference_only → 去重后两个都 reference_only
    assert cov["by_reference_status_distinct"].get("reference_only") == 2
    # A 最新非 purchasable → 可投放去重数 = 0(证明"取最新"而非把旧 purchasable 也算上)
    assert cov["purchasable_entities"] == 0
    # 同分母:各状态去重数之和 == scored_entities
    assert sum(cov["by_reference_status_distinct"].values()) == cov["scored_entities"]
