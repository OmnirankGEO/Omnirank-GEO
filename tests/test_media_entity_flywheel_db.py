from datetime import datetime, timezone


def test_upsert_score_snapshot_serializes_datetime_evidence(monkeypatch):
    from db import media_entity_flywheel_db as media_db

    class FakeCursor:
        def execute(self, sql, params):
            assert "media_entity_score_snapshots" in sql
            evidence_json = params[11]
            serialized = evidence_json.dumps(evidence_json.adapted)
            assert "2026-06-15" in serialized

        def fetchone(self):
            return {"id": 1, "entity_id": 7, "industry_key": "tourism_hotel"}

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(media_db, "get_db", lambda: FakeConnection())

    row = media_db.upsert_score_snapshot(
        7,
        {
            "industry_key": "tourism_hotel",
            "score_version": "unit",
            "shadow_score": 12.3,
            "evidence_score": 10,
            "quality_score": 8,
            "inventory_score": 0,
            "outcome_score": 0,
            "reference_status": "reference_only",
            "is_purchasable": False,
            "reasons": ["测试"],
            "evidence": {
                "citation_rollup": {
                    "domain": "example.com",
                    "last_seen_at": datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc),
                }
            },
        },
    )

    assert row["entity_id"] == 7
