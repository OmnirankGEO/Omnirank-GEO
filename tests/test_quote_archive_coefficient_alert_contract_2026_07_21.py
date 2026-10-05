from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest
import services.quote_pricing_snapshot as quote_snapshot_module

from services.organization_contract import (
    GOVERNANCE_AUDIT_ACCESS_CONTRACT,
    IdentityContext,
    OrganizationError,
    require_governance_audit_access,
)
from services.organization_route_contract import match_member_geo_route
from services.quote_pricing_snapshot import (
    PRIVATE_CONTEXT_KEY,
    QuoteSnapshotError,
    build_coefficient_preview,
    quote_payload_hash,
    publish_frozen_snapshot,
    stamp_calculation_context,
)


ROOT = Path(__file__).resolve().parents[1]


def _base_pricing(coefficient: float = 2.0) -> dict:
    pricing = {
        "tiers": {
            "entry": {"total_price": 200, "total_articles": 2},
            "standard": {"total_price": 400, "total_articles": 4},
            "flagship": {"total_price": 600, "total_articles": 6},
        },
        "keywords": [
            {
                "id": 11,
                "keyword": "同名客户关键词",
                "cost_per_article": 73,
                "entry": {"price": 200, "articles": 2},
                "standard": {"price": 400, "articles": 4},
                "flagship": {"price": 600, "articles": 6},
            }
        ],
    }
    return stamp_calculation_context(pricing, None, coefficient=coefficient)[0]


def test_quote_coefficient_scales_existing_prices_without_touching_cost_or_input():
    source = _base_pricing(2.0)
    result = build_coefficient_preview(source, None, new_coefficient=3.0)

    assert result["summaries"]["standard"]["total_price"] == 600
    assert result["keywords"][0]["standard"] == 600
    assert result["pricing_data"]["keywords"][0]["cost_per_article"] == 73
    assert source["keywords"][0]["standard"]["price"] == 400
    assert source[PRIVATE_CONTEXT_KEY]["coefficient"] == 2.0
    assert result["calculation_version"] == "quote-coefficient-application-v1"


def test_quote_coefficient_preview_is_deterministic_and_legacy_missing_baseline_is_p1():
    source = _base_pricing(1.5)
    first = build_coefficient_preview(source, None, new_coefficient=2.2)
    second = build_coefficient_preview(source, None, new_coefficient=2.2)
    assert first["snapshot_hash"] == second["snapshot_hash"]

    with pytest.raises(QuoteSnapshotError) as caught:
        build_coefficient_preview({"tiers": {}, "keywords": []}, None, new_coefficient=2)
    assert caught.value.code == "QUOTE_COEFFICIENT_BASELINE_MISSING"
    assert caught.value.as_detail()["priority"] == "P1"


def test_quote_payload_hash_binds_exact_persisted_source_and_clusters():
    pricing = _base_pricing(2.0)
    clusters = {"clusters": [{"cluster_name": "A"}]}
    original = quote_payload_hash(pricing, clusters)
    assert original == quote_payload_hash(json.dumps(pricing), json.dumps(clusters))
    changed = json.loads(json.dumps(pricing, ensure_ascii=False))
    changed["keywords"][0]["standard"]["price"] += 1
    assert quote_payload_hash(changed, clusters) != original


def test_quote_mutations_require_preview_and_source_hash_preconditions():
    selection_source = (ROOT / "api" / "selection_api.py").read_text(encoding="utf-8")
    service_source = (ROOT / "services" / "quote_pricing_snapshot.py").read_text(encoding="utf-8")
    assert "expected_snapshot_hash: str = Field(...," in selection_source
    assert "expected_source_hash=expected_source_hash" in selection_source
    assert "QUOTE_PUBLISH_SOURCE_STALE" in service_source
    locked_select = service_source[service_source.index("def publish_frozen_snapshot"):]
    assert "s.pricing_data" in locked_select and "s.clusters_data" in locked_select
    assert "current_source_hash != expected_source_hash" in locked_select


def test_publish_rejects_a_concurrent_quote_change_before_any_snapshot_write(monkeypatch):
    original = _base_pricing(2.0)
    changed = json.loads(json.dumps(original, ensure_ascii=False))
    changed["keywords"][0]["standard"]["price"] += 1

    class Cursor:
        writes: list[str] = []

        def execute(self, statement, _params=None):
            if statement.lstrip().startswith("SELECT"):
                return
            self.writes.append(statement)

        def fetchone(self):
            return {
                "session_id": 9,
                "brand_id": 3,
                "status": "pricing_pending_review",
                "pricing_data": changed,
                "clusters_data": None,
                "active_pricing_snapshot_id": None,
            }

    class Connection:
        def __init__(self):
            self.cursor_instance = Cursor()
            self.committed = False
            self.rolled_back = False

        def cursor(self):
            return self.cursor_instance

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

        def close(self):
            pass

    connection = Connection()
    monkeypatch.setattr(quote_snapshot_module, "get_connection", lambda: connection)
    with pytest.raises(QuoteSnapshotError) as caught:
        publish_frozen_snapshot(
            7,
            actor_user_id=1,
            actor_membership_id=None,
            pricing_data=original,
            clusters_data=None,
            reason="审核发送",
            expected_source_hash=quote_payload_hash(original, None),
        )
    assert caught.value.code == "QUOTE_PUBLISH_SOURCE_STALE"
    assert connection.cursor_instance.writes == []
    assert connection.committed is False
    assert connection.rolled_back is True


def test_id_only_archive_routes_cover_same_name_same_and_cross_tenant_counterexamples():
    selection_source = (ROOT / "api" / "selection_api.py").read_text(encoding="utf-8")
    archive_source = (ROOT / "services" / "artifact_archive.py").read_text(encoding="utf-8")
    brand_source = (ROOT / "api" / "brand_api.py").read_text(encoding="utf-8")
    server_source = (ROOT / "server.py").read_text(encoding="utf-8")

    assert '@router.delete("/quotes/{quote_id}")' in selection_source
    assert "archive_quote," in selection_source
    assert "quote_id," in selection_source
    assert "delete_session(token)" not in selection_source
    assert "WHERE q.id=%s" in archive_source
    assert "WHERE id=%s" in archive_source
    assert "WHERE brand_id = %s" in brand_source or "WHERE brand_id=%s" in brand_source
    assert '@app.delete("/api/writing/projects/{quote_id}")' in server_source
    writing_delete = server_source[server_source.index('@app.delete("/api/writing/projects/{quote_id}")'):]
    assert "ALTER TABLE" not in writing_delete.split('@app.post("/api/writing/projects/{quote_id}/restore")')[0]
    # Names remain display-only evidence; none of the new mutations bind by them.
    assert "UPDATE quotes SET" not in archive_source.split("WHERE q.id=%s")[0]


def test_session_creation_locks_the_live_exact_quote_not_an_unrelated_read_path():
    source = (ROOT / "db" / "diagnosis_db.py").read_text(encoding="utf-8")
    create_block = source[source.index("def create_selection_session("):source.index("def list_selection_sessions(")]
    article_read = source[source.index("def get_articles_by_task("):source.index("def get_articles_by_diagnosis(")]
    assert "WHERE id=%s AND brand_id=%s AND deleted_at IS NULL FOR UPDATE" in create_block
    assert "QUOTE_ARCHIVED_OR_SCOPE_CHANGED" in create_block
    assert "QUOTE_ARCHIVED_OR_SCOPE_CHANGED" not in article_read


def test_client_restore_only_reverses_rows_archived_by_the_client_cascade():
    brand_source = (ROOT / "api" / "brand_api.py").read_text(encoding="utf-8")
    migration = (ROOT / "scripts" / "migration_quote_snapshots_and_archives_2026_07_21.sql").read_text(
        encoding="utf-8"
    )

    # A same-name or same-brand object that was independently archived before
    # the client cascade must stay archived when the client is restored.
    assert "archived_with_brand_at=CURRENT_TIMESTAMP" in brand_source
    assert "AND archived_with_brand_at IS NOT NULL" in brand_source
    assert migration.count("archived_with_brand_at TIMESTAMPTZ") == 4


def test_name_based_cache_delete_is_fail_closed_p1():
    source = (ROOT / "api" / "selection_api.py").read_text(encoding="utf-8")
    endpoint = source[source.index('@router.delete("/keyword-selection/{token}/price-cache")'):]
    assert "QUOTE_CACHE_BRAND_ID_NAMESPACE_REQUIRED" in endpoint
    assert "clear_keyword_prices_cache(" not in endpoint
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "audit_name_based_writes.py"), "--strict-destructive"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report["destructive_name_writes"] == []
    assert all(item["priority"] == "P1" for item in report["remaining_name_keyed_upserts"])


def test_customer_public_quote_uses_only_frozen_snapshot_after_publish():
    source = (ROOT / "api" / "selection_api.py").read_text(encoding="utf-8")
    public_handler = source[source.index('@router.get("/s/{token}")'):source.index('@router.post("/s/{token}/submit-keywords")')]
    assert "get_frozen_snapshot" in public_handler
    assert "QUOTE_SNAPSHOT_REQUIRED" in public_handler
    assert "pricing_pending_review" in public_handler


def test_organization_route_contract_reuses_existing_capabilities():
    assert match_member_geo_route("POST", "/api/quotes/42/coefficient").capability == "quote.create"
    assert match_member_geo_route("DELETE", "/api/quotes/42").capability == "team.output_handoff"
    assert match_member_geo_route("DELETE", "/api/writing/projects/42").capability == "team.output_handoff"
    assert match_member_geo_route("GET", "/api/keyword-selection/list").capability == "quote.read_own"
    assert match_member_geo_route("POST", "/api/keyword-selection/Abc_123-X/generate-quote").capability == "quote.create"
    assert match_member_geo_route("POST", "/api/keyword-selection/Abc_123-X/approve-quote") is None
    assert match_member_geo_route("POST", "/api/keyword-selection/Abc_123-X/mark-paid") is None


def test_snapshot_migration_runs_after_organization_contract():
    from db.migration_manifest import MIGRATIONS

    organization = "scripts/migration_organization_internal_seats_2026_07_20.sql"
    snapshot = "scripts/migration_quote_snapshots_and_archives_2026_07_21.sql"
    assert snapshot in MIGRATIONS
    assert MIGRATIONS.index(organization) < MIGRATIONS.index(snapshot)


def test_snapshot_migration_pins_exact_public_schema_and_immutable_audit():
    migration = (ROOT / "scripts" / "migration_quote_snapshots_and_archives_2026_07_21.sql").read_text(
        encoding="utf-8"
    )
    assert "SET LOCAL search_path = public, pg_catalog" in migration
    assert "VALIDATE CONSTRAINT quote_pricing_snapshots_quote_brand_fk" in migration
    assert "convalidated IS DISTINCT FROM true" in migration
    assert "actual_desc IS DISTINCT FROM item.descending" in migration
    assert "t.tgenabled" in migration and "trigger_row.tgtype IS DISTINCT FROM 27" in migration
    assert "pg_get_serial_sequence('public.quote_pricing_snapshots','id')" in migration
    assert "WHERE conname='" not in migration

    rollback = (ROOT / "scripts" / "rollback_quote_snapshots_and_archives_2026_07_21.sql").read_text(
        encoding="utf-8"
    )
    assert "UPDATE quotes" not in rollback
    assert "UPDATE keyword_selection_sessions" not in rollback
    assert "DELETE " not in rollback and "DROP " not in rollback


def test_admin_and_organization_audit_share_one_access_contract():
    assert GOVERNANCE_AUDIT_ACCESS_CONTRACT["organization_owner"] == "organization.manage"
    assert GOVERNANCE_AUDIT_ACCESS_CONTRACT["platform_admin"] == "platform_admin.use"
    assert require_governance_audit_access(is_platform_admin=True) == "platform"
    owner = IdentityContext(
        request_id="r",
        authenticated_user_id=1,
        principal_user_id=1,
        payer_user_id=1,
        actor_kind="owner",
        organization_id=7,
    )
    assert require_governance_audit_access(identity=owner) == "organization:7"
    member = IdentityContext(
        request_id="r2",
        authenticated_user_id=2,
        principal_user_id=1,
        payer_user_id=1,
        actor_kind="member",
        actor_user_id=2,
        organization_id=7,
        capabilities=frozenset({"quote.create"}),
    )
    with pytest.raises(OrganizationError) as caught:
        require_governance_audit_access(identity=member)
    assert caught.value.code == "GOVERNANCE_AUDIT_ACCESS_DENIED"


def test_alert_action_schema_and_representative_census_are_machine_auditable():
    schema = json.loads((ROOT / "schemas" / "alert_action_contract.json").read_text(encoding="utf-8"))
    assert schema["title"] == "AlertAction v1"
    assert {"action", "target", "permission", "recovery"} in [set(item["required"]) for item in schema["anyOf"]]

    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "audit_alert_actions.py"), "--strict-representative"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report["files_scanned"] > 100
    assert report["representative_missing_contract"] == []
