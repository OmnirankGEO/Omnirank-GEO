def _reset_memory_profile(profile_id: str):
    from db import profile_memory_db as memory_db

    memory_db._ensured = False
    memory_db.init_profile_memory_events_table()
    conn = memory_db._get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM profile_memory_events WHERE profile_id = %s", (profile_id,))
        conn.commit()
    finally:
        conn.close()


def test_step24_profile_memory_lists_by_concept_weight_and_tracks_access():
    from db import profile_memory_db as memory_db

    profile_id = "step24-weighted"
    _reset_memory_profile(profile_id)
    conn = memory_db._get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO profile_memory_events (
                profile_id, source, event_type, canonical_concept,
                title, text, raw_payload, confidence, weight_delta,
                review_status, is_active, importance_score, access_count, created_at
            ) VALUES
                (%s, 'profile_flywheel', 'detail', 'target_customer',
                 'recent-low', '最近但不重要', '{}'::jsonb, 0.7, 1, 'approved', TRUE, 3, 0, NOW()),
                (%s, 'profile_flywheel', 'detail', 'target_customer',
                 'old-high', '旧但更重要的目标客户', '{}'::jsonb, 0.95, 5, 'approved', TRUE, 9, 0, NOW() - INTERVAL '7 days'),
                (%s, 'profile_flywheel', 'detail', 'voice_style',
                 'voice', '不要混入目标客户检索', '{}'::jsonb, 0.9, 2, 'approved', TRUE, 10, 0, NOW())
            RETURNING id
            """,
            (profile_id, profile_id, profile_id),
        )
        conn.commit()
    finally:
        conn.close()

    rows = memory_db.list_profile_memory_events(
        profile_id,
        limit=5,
        prompt_safe=True,
        review_statuses=("approved", "auto"),
        canonical_concept="target_customer",
        track_access=True,
    )

    assert [row["title"] for row in rows] == ["old-high", "recent-low"]
    assert all(row["canonical_concept"] == "target_customer" for row in rows)
    assert rows[0]["importance_score"] == 9

    conn = memory_db._get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT access_count, last_accessed_at FROM profile_memory_events WHERE id = %s",
            (rows[0]["id"],),
        )
        accessed = dict(cur.fetchone())
    finally:
        conn.close()

    assert accessed["access_count"] >= 1
    assert accessed["last_accessed_at"] is not None


def test_step24_record_memory_defaults_importance_by_concept():
    from db import profile_memory_db as memory_db

    profile_id = "step24-default-importance"
    _reset_memory_profile(profile_id)

    event_id = memory_db.record_profile_memory_event(
        profile_id,
        source="profile_flywheel",
        event_type="detail",
        title="不要承诺升学结果",
        text="教育咨询内容不要承诺录取结果",
        canonical_concept="guardrails",
        review_status="approved",
    )

    event = memory_db.get_profile_memory_event(event_id)
    assert event["importance_score"] >= 9


# [开源 E3 · B2 · 2026-09-28] 社媒 soul / 质量校验随包删除,守它们的 4 格退役。


def test_step24_consolidation_finds_similar_memory_candidates():
    from scripts.cron.consolidate_profile_memory import find_memory_consolidation_candidates

    rows = [
        {"id": 1, "canonical_concept": "target_customer", "text": "25-35岁成都宝妈，关注皮肤管理"},
        {"id": 2, "canonical_concept": "target_customer", "text": "成都25到35岁宝妈，最关心皮肤管理"},
        {"id": 3, "canonical_concept": "voice_style", "text": "短句，直接说判断"},
    ]

    candidates = find_memory_consolidation_candidates(rows, threshold=0.55)

    assert candidates
    assert candidates[0]["keep_id"] == 1
    assert candidates[0]["merge_ids"] == [2]


