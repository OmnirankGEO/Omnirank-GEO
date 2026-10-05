import ast
from pathlib import Path

from services.organization_schema_contract import (
    EXPECTED_COUNTS,
    EXPECTED_FINGERPRINT,
    PRODUCTION_REANCHOR_COUNTS,
    PRODUCTION_REANCHOR_FINGERPRINT,
    PRODUCTION_REANCHOR_PAYER_COUNTS,
    PRODUCTION_REANCHOR_PAYER_FINGERPRINT,
)
from scripts.verify_unified_release_readiness import (
    _organization_catalog_matches,
)


ROOT = Path(__file__).resolve().parents[1]


def _catalog(counts, fingerprint):
    return {
        "actual_counts": counts,
        "actual_fingerprint": fingerprint,
        "expected_counts": {"columns": 416, "constraints": 282, "indexes": 75},
        "expected_fingerprint": (
            "e290c4128022a7cc725f6e092d0aad1f44461d27d7a0f528d162ddafc80d57a6"
        ),
    }


def test_organization_gate_accepts_only_fresh_or_exact_production_reanchor():
    frozen = _catalog(
        EXPECTED_COUNTS,
        EXPECTED_FINGERPRINT,
    )
    production = _catalog(PRODUCTION_REANCHOR_COUNTS, PRODUCTION_REANCHOR_FINGERPRINT)
    production_payer = _catalog(
        PRODUCTION_REANCHOR_PAYER_COUNTS,
        PRODUCTION_REANCHOR_PAYER_FINGERPRINT,
    )
    drifted = _catalog(PRODUCTION_REANCHOR_COUNTS, "0" * 64)
    assert _organization_catalog_matches(frozen)
    assert _organization_catalog_matches(production)
    assert _organization_catalog_matches(production_payer)
    assert not _organization_catalog_matches(drifted)


def test_fresh_gate_restores_only_clean_base_schema_before_migrations():
    source = (ROOT / "scripts" / "deploy-blue-green.sh").read_text(
        encoding="utf-8"
    )
    fresh_gate = source[source.index("fresh_rollback_forward_validation()") :]
    restore = fresh_gate.index("pg_restore --exit-on-error --schema-only")
    prestart = fresh_gate.index('run_candidate_prestart_for_database "$fresh_db"')
    assert restore < prestart
    assert "--data-only" not in fresh_gate[:prestart]


def test_production_schema_hash_ignores_pg16_random_restrict_tokens():
    source = (ROOT / "scripts" / "deploy-blue-green.sh").read_text(
        encoding="utf-8"
    )
    hash_gate = source[
        source.index("production_schema_hash()") : source.index(
            "drop_dry_run_database()"
        )
    ]
    assert "sed -E '/^\\\\(un)?restrict /d'" in hash_gate


def test_rollback_gate_uses_psql_unaligned_boolean_rendering():
    source = (ROOT / "scripts" / "deploy-blue-green.sh").read_text(
        encoding="utf-8"
    )
    rollback_gate = source[
        source.index("assert_unified_rollback_state()") : source.index(
            "fresh_rollback_forward_validation()"
        )
    ]
    assert '[ "$state" = "t|t|t|t|t" ]' in rollback_gate


def test_concurrent_prestart_runs_disable_compose_tty_allocation():
    source = (ROOT / "scripts" / "deploy-blue-green.sh").read_text(
        encoding="utf-8"
    )
    helpers = source[
        source.index("run_candidate_prestart_for_database()") : source.index(
            "run_rollback_sql_for_database()"
        )
    ]
    assert helpers.count("compose run --rm --no-deps -T") == 2


def test_production_prestart_overrides_inert_image_command_without_tty():
    source = (ROOT / "scripts" / "deploy-blue-green.sh").read_text(
        encoding="utf-8"
    )
    start = source.index('echo "[2.5/8] prestart migration + schema verification..."')
    end = source.index('assert_infra_identity_unchanged "prestart"')
    production_gate = source[start:end]
    assert production_gate.count("compose run --rm --no-deps -T") == 2
    assert '--entrypoint python "omnirank-$INACTIVE"' in production_gate
    assert "-m scripts.prestart" in production_gate
    assert "-m scripts.verify_unified_release_readiness" in production_gate
    assert 'ROLE=prestart "omnirank-$INACTIVE";' not in production_gate


def test_immutable_image_must_retain_reviewed_application_command():
    source = (ROOT / "scripts" / "deploy-blue-green.sh").read_text(
        encoding="utf-8"
    )
    image_gate = source[
        source.index('IMMUTABLE_IMAGE_REF="omnirank-release:$DEPLOY_SHA"') :
        source.index('echo "[2.5/8] prestart migration + schema verification..."')
    ]
    assert "--format='{{json .Config.Cmd}}'" in image_gate
    assert '''[ "$BUILT_IMAGE_CMD" = '["/app/start.sh"]' ]''' in image_gate
    assert "--format='{{json .Config.Entrypoint}}'" in image_gate
    assert "null|'[]'" in image_gate


def test_candidate_cron_bootstraps_before_old_leader_is_stopped():
    source = (ROOT / "scripts" / "deploy-blue-green.sh").read_text(
        encoding="utf-8"
    )
    cron_gate = source[source.index('echo "[3.6/8] W4 cron 接管（切流前）..."') :]
    runtime_ready = cron_gate.index(
        '_wait_cron_runtime_ready "omnirank-cron-$INACTIVE" 60'
    )
    old_stop = cron_gate.index(
        'docker stop --time=30 "omnirank-cron-$ACTIVE"'
    )
    assert runtime_ready < old_stop
    assert "curl -fsS --max-time 2 http://localhost:8000/docs" in source


def test_monitoring_api_runtime_annotations_import_any():
    source = (ROOT / "api" / "monitoring_api.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    typing_names = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "typing"
        for alias in node.names
    }
    assert "Any" in typing_names


def test_all_application_roles_keep_release_config_mount_bytecode_free():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert compose.count('PYTHONDONTWRITEBYTECODE: "1"') == 4


def test_graceful_chunk_merge_preserves_the_complete_candidate_build():
    source = (ROOT / "scripts" / "deploy-blue-green.sh").read_text(
        encoding="utf-8"
    )
    start = source.index("# 3.5. Graceful SPA chunks")
    merge = source[start : source.index("# 3.6. W4 cron", start)]

    # Image-layer mtimes describe build time, not release membership. A slow
    # validation/deploy must never age out the candidate release's own assets.
    assert "-mmin +60" not in merge
    assert "-delete" not in merge
    assert "/app/scripts/merge_frontend_assets.py" in merge
    verifier = "bash /app/scripts/verify_frontend_deploy.sh /app/frontend/dist"
    assert verifier in merge
    assert merge.index('docker cp "$TMP_OLD_ASSETS/assets/."') < merge.index(
        "/app/scripts/merge_frontend_assets.py"
    )
    assert merge.index("/app/scripts/merge_frontend_assets.py") < merge.index(verifier)
