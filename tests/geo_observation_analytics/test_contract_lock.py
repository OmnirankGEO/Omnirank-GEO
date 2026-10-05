"""Contract lock: code constants, vendored files and the reference DDL must
never drift from the frozen machine contract."""

from __future__ import annotations

import hashlib
import subprocess

import psycopg2
import pytest

from services.geo_observation_analytics import contract as C

FROZEN_HASHES = {
    "observation_contract_v1.json": "65C10AABC921F3889D2FE70B09D1F4E0C8E8CFD8904AC5AC7F32F79EAC3BD5AA",
    "frontend_copy_and_race_v1.json": "0FF5C64A0DC279AB6B2987EF22C0EE1B36C643F8061D54650B1FF9420E7DA0B6",
    "frontend_race_fixture_v1.json": "B5C477DF0377BCBEB4D36BB8CA78EEAE591893068CFDC5AD7C501FB40F71F1B5",
}


def test_vendored_contract_hashes_match_frozen():
    for name, expected in FROZEN_HASHES.items():
        raw = C.contract_path(name).read_bytes()
        got = hashlib.sha256(raw).hexdigest().upper()
        assert got == expected, f"{name} drifted: {got} != {expected}"


def test_versions_match_contract_and_fixture():
    contract = C.load_frozen_contract()
    fixture = C.load_frozen_fixture()
    copy = C.load_frozen_copy_contract()
    assert C.CONTRACT_VERSION == contract["contract_version"]
    assert C.SCHEMA_VERSION == contract["schema_version"]
    assert C.CONTRACT_VERSION == fixture["contract_version"]
    assert C.METRIC_VERSION == fixture["cases"]["brand_summary"]["response"]["metric_version"]
    assert C.COPY_CONTRACT_VERSION == copy["contract_version"]
    assert C.COPY_CONTRACT_VERSION == fixture["copy_contract_version"]
    assert C.FIXTURE_VERSION == fixture["fixture_version"]


def test_outcome_enum_matches_contract():
    contract = C.load_frozen_contract()
    assert list(C.TARGET_OUTCOMES) == contract["target_outcome"]
    # valid = ten minus the two excluded
    assert set(C.OUTCOMES_EXCLUDED_FROM_VALID) == {"entity_ambiguous", "engine_error"}
    assert set(C.VALID_OUTCOMES) == set(C.TARGET_OUTCOMES) - C.OUTCOMES_EXCLUDED_FROM_VALID
    assert len(C.VALID_OUTCOMES) == 8


def test_other_enums_match_contract():
    contract = C.load_frozen_contract()
    assert list(C.SURFACE_KEYS) == contract["surface_keys"]
    assert list(C.RESPONSE_STATUSES) == contract["response_status"]
    assert list(C.EVENT_PROCESSING_STATES) == contract["event_processing_state"]
    assert list(C.STABILITY_STATES) == contract["stability"]
    assert C.SOURCE_KIND_TO_SOURCE_TYPE == contract["source_kind_to_source_type"]
    assert set(C.SOURCE_TYPES) == set(contract["source_kind_to_source_type"].values())


def test_aggregate_field_names_match_contract_order():
    contract = C.load_frozen_contract()
    contract_fields = [f["name"] for f in contract["aggregate_schema"]["fields"]]
    assert list(C.AGGREGATE_FIELD_NAMES) == contract_fields


def test_public_dto_forbidden_covers_contract():
    contract = C.load_frozen_contract()
    contract_forbidden = set(contract["aggregate_schema"]["public_dto_forbidden_fields"])
    assert contract_forbidden == set(C.PUBLIC_DTO_FORBIDDEN_FIELDS)
    # owner_user_id + aggregate_key are forbidden in ALL non-admin DTOs;
    # brand_id is forbidden only in PUBLIC DTOs (private shows the own brand).
    assert "owner_user_id" in C.PRIVACY_FORBIDDEN_FIELDS
    assert "aggregate_key" in C.PRIVACY_FORBIDDEN_FIELDS
    assert "brand_id" not in C.PRIVACY_FORBIDDEN_FIELDS
    # the full public deny-list is a superset of the contract's public list
    assert contract_forbidden <= C.PUBLIC_DTO_ALL_FORBIDDEN_FIELDS


def test_eligibility_is_promoted_only():
    contract = C.load_frozen_contract()
    assert C.ELIGIBLE_PROCESSING_STATE == "promoted"
    assert "promoted" in contract["aggregate_schema"]["eligibility"]
    assert "withdrawn" in contract["aggregation_eligibility"]


# SHA256 of the vendored AI-2 migration the tests bind to. Always-run
# reproducible pin: the AI-3 delivery is proven against exactly this migration
# byte-for-byte. Vendored from AI-2's latest governance snapshot (which now
# carries the compliance + gold-standard columns). AI-2's governance file is
# still UNCOMMITTED; final unified release MUST re-vendor from AI-2's committed
# clean SHA and bump this pin — a documented gate dependency.
VENDORED_AI2_MIGRATION_SHA256 = (
    "5e7bfb881ec7b6a3d1146802da2ce68372aea8dfd2c00f9f42907b0aca84fe5d"
)
AI2_GOVERNANCE_SOURCE_COMMIT = "d604c7984b0e6c553395514fc0058668fd2c8942"
AI2_GOVERNANCE_MIGRATION_PATH = "scripts/migration_geo_observation_v1_2026_07_17.sql"


def test_vendored_ai2_migration_hash_locked():
    """Always-run pin: the vendored AI-2 migration must match its frozen SHA256,
    so the delivery's 'runs against AI-2's real migration' proof is reproducible
    regardless of whether the sibling governance worktree is present."""
    raw = C.contract_path("ai2_migration_geo_observation_v1_2026_07_17.sql").read_bytes()
    got = hashlib.sha256(raw).hexdigest()
    assert got == VENDORED_AI2_MIGRATION_SHA256, f"vendored AI-2 migration changed: {got}"


def test_hash_lock_detects_migration_drift():
    """The pin is a REAL detector, not a tautology: the current vendored bytes
    match, but any single-byte change (drift) produces a different digest that
    would FAIL the always-run hash-lock above."""
    raw = C.contract_path("ai2_migration_geo_observation_v1_2026_07_17.sql").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == VENDORED_AI2_MIGRATION_SHA256
    drifted = raw + b"\n-- AI-2 added a compliance column\n"
    assert hashlib.sha256(drifted).hexdigest() != VENDORED_AI2_MIGRATION_SHA256


def test_vendored_ai2_migration_matches_governance():
    """HARD-FAIL cross-package drift gate against AI-2's committed source blob.

    A sibling-worktree path is not a release proof: it silently skipped in CI or
    any clean integration worktree. The committed source SHA is stable and must
    be present in the unified repository; a missing object or any byte drift is a
    real integration failure, never a skip.
    """
    vendored = C.contract_path("ai2_migration_geo_observation_v1_2026_07_17.sql")
    proc = subprocess.run(
        ["git", "show", f"{AI2_GOVERNANCE_SOURCE_COMMIT}:{AI2_GOVERNANCE_MIGRATION_PATH}"],
        cwd=C.contract_path("observation_contract_v1.json").parents[3],
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, (
        "AI-2 committed migration blob is unavailable; the unified release must "
        f"include/fetch {AI2_GOVERNANCE_SOURCE_COMMIT}: {proc.stderr.decode(errors='replace')}"
    )
    gov_norm = proc.stdout.replace(b"\r\n", b"\n")
    ven_norm = vendored.read_bytes().replace(b"\r\n", b"\n")
    got = hashlib.sha256(gov_norm).hexdigest()
    assert ven_norm == gov_norm, (
        "AI-2 committed governance migration drifted from the vendored pin "
        f"(governance sha256={got}). Re-vendor + bump VENDORED_AI2_MIGRATION_SHA256; "
        "this HARD-FAILS the unified merge."
    )


@pytest.mark.integration
def test_reference_ddl_columns_match_contract(isolated_postgres_schema):
    """The reference/test aggregate table columns == machine-contract fields."""
    from db import connection

    conn = psycopg2.connect(connection.DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='geo_observation_aggregates' ORDER BY ordinal_position"
            )
            cols = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    # Integrator-owned additive identity column: frozen v1 metric fields remain
    # byte-for-byte unchanged; policy_basis_hash is appended by the activation
    # safety migration and is intentionally outside the historical fixture.
    assert cols == [
        *C.AGGREGATE_FIELD_NAMES,
        "policy_basis_hash",
        "eligibility_epoch",
        "promotion_sequence_watermark",
    ]
