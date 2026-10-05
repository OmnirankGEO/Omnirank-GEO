from __future__ import annotations

import pytest


def test_no_binding_resolves_to_configured_platform_direct_without_referral_lookup():
    from services.commercial_relationship import (
        RelationshipResolution,
        classify_commercial_relationship,
    )

    resolution = classify_commercial_relationship(
        customer_user_id=40,
        binding_rows=[],
        platform_direct_user_id=9001,
    )

    assert resolution.resolution is RelationshipResolution.PLATFORM_DIRECT
    assert resolution.service_user_id == 9001
    assert resolution.public_configuration_status == "ready"


def test_unique_valid_binding_is_used_internally():
    from services.commercial_relationship import (
        RelationshipResolution,
        classify_commercial_relationship,
    )

    resolution = classify_commercial_relationship(
        customer_user_id=40,
        binding_rows=[
            {
                "id": 5,
                "agent_user_id": 17,
                "dispute_status": None,
                "agent_is_active": True,
                "agent_level": 1,
            }
        ],
        platform_direct_user_id=9001,
    )

    assert resolution.resolution is RelationshipResolution.BOUND
    assert resolution.service_user_id == 17


@pytest.mark.parametrize(
    "rows",
    [
        [
            {"id": 5, "agent_user_id": 17, "dispute_status": None, "agent_is_active": True, "agent_level": 1},
            {"id": 6, "agent_user_id": 18, "dispute_status": None, "agent_is_active": True, "agent_level": 1},
        ],
        [
            {"id": 5, "agent_user_id": 17, "dispute_status": "pending", "agent_is_active": True, "agent_level": 1},
        ],
        [
            {"id": 5, "agent_user_id": 17, "dispute_status": None, "agent_is_active": False, "agent_level": 1},
        ],
    ],
)
def test_dirty_or_conflicting_relationship_fails_closed(rows):
    from services.commercial_relationship import (
        RelationshipConflict,
        classify_commercial_relationship,
    )

    with pytest.raises(RelationshipConflict):
        classify_commercial_relationship(
            customer_user_id=40,
            binding_rows=rows,
            platform_direct_user_id=9001,
        )


def test_relationship_resolver_contains_no_referral_inference():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    source = (root / "services/commercial_relationship.py").read_text(encoding="utf-8")
    assert "referral_links" not in source
    assert "find_direct_inviter" not in source


def test_bound_relationship_does_not_require_platform_direct_configuration(monkeypatch):
    from services.commercial_relationship import (
        RelationshipResolution,
        resolve_commercial_relationship,
    )

    monkeypatch.delenv("PLATFORM_DIRECT_SERVICE_USER_ID", raising=False)

    class Cursor:
        def __init__(self):
            self.execute_count = 0

        def execute(self, query, params):
            self.execute_count += 1

        def fetchall(self):
            return [{
                "id": 5,
                "agent_user_id": 17,
                "dispute_status": None,
                "agent_is_active": True,
                "agent_level": 1,
            }]

    cursor = Cursor()
    result = resolve_commercial_relationship(cursor, 40)

    assert result.resolution is RelationshipResolution.BOUND
    assert result.service_user_id == 17
    assert cursor.execute_count == 1


def test_commercial_relationship_lock_serializes_empty_binding_gap():
    from services.commercial_relationship import lock_commercial_relationship

    calls = []

    class Cursor:
        def execute(self, query, params):
            calls.append((" ".join(query.split()), params))

    lock_commercial_relationship(Cursor(), 40)

    assert calls == [
        ("SELECT pg_advisory_xact_lock(%s,%s)", (920716, 40)),
    ]
