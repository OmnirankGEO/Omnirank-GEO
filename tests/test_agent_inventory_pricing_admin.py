"""服务商进货价目表后台化的无数据库资金/契约单测。"""
import subprocess
import sys
from pathlib import Path

import pytest

from services.agent_inventory_pricing import (
    INVENTORY_SNAPSHOT_RUNTIME_CAPABILITY,
    calculate_purchase,
    next_catalog_version,
    normalize_catalog_options,
    points_at_rate,
    stable_option_id,
)
from services.agent_inventory_deploy_gate import (
    LEGACY_INVENTORY_RUNTIME_CAPABILITY,
    evaluate_inventory_rollback,
)


ROOT = Path(__file__).resolve().parents[1]


def test_integer_calculator_is_deterministic_and_conservative():
    result = calculate_purchase(
        amount_cents=100_000,
        discount_numer=200,
        discount_denom=325,
        reward_rate_bps=3333,
        reward_eligible=True,
    )
    assert result == {
        "base_points": 162_500,
        "bonus_points": 54_161,
        "total_points": 216_661,
    }
    assert points_at_rate(5, 1000) == 1  # half-up；不使用 float/banker's rounding


def test_catalog_normalization_keeps_disabled_and_stably_sorts():
    rows = normalize_catalog_options([
        {"amount_cents": 30000, "is_enabled": False, "sort_order": 2},
        {"amount_cents": 20000, "is_enabled": True, "sort_order": 1},
        {"amount_cents": 10000, "is_enabled": True, "sort_order": 1},
    ])
    assert [row["amount_cents"] for row in rows] == [10000, 20000, 30000]
    assert rows[-1]["is_enabled"] is False
    assert rows[-1]["option_id"] == stable_option_id(30000)
    assert normalize_catalog_options([]) == []


def test_catalog_version_namespace_is_monotonic_and_strict():
    assert next_catalog_version("agent-purchase-v1") == "agent-purchase-v2"
    assert next_catalog_version("agent-purchase-v99") == "agent-purchase-v100"
    with pytest.raises(ValueError):
        next_catalog_version("pricing-v2")


def test_no_second_catalog_or_channel_write_bypass_in_source():
    channel_api = (ROOT / "api" / "admin_api.py").read_text(encoding="utf-8")
    channel_service = (ROOT / "services" / "channel_tier_admin.py").read_text(encoding="utf-8")
    global_api = (ROOT / "api" / "admin_factory_api.py").read_text(encoding="utf-8")
    assert "agent_purchase_options: Optional" not in channel_api
    assert '"agent_purchase_options",' not in channel_service
    assert "model_config = ConfigDict(extra=\"forbid\")" in global_api
    assert "pricing_catalog_versions" not in (ROOT / "services" / "agent_inventory_pricing.py").read_text(encoding="utf-8")


def test_wallet_diff_contract_markers_are_inside_authorized_branch():
    source = (ROOT / "db" / "wallet_db.py").read_text(encoding="utf-8")
    branch = source[source.index('if order_type == "agent_inventory_purchase":'):]
    branch = branch[:branch.index("# 入账(user_wallets")]
    assert "pricing_snapshot_jsonb" in branch
    assert "pricing_catalog_version" in branch
    assert "founder_bonus_points_if_eligible" in branch
    assert "bonus_validity_months" in branch
    assert "AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT" in branch
    assert "legacy agent_inventory_purchase 无快照结算" in branch


def test_bigint_migration_and_cutover_activation_are_split_and_idempotent():
    migration = (ROOT / "scripts" / "migration_agent_inventory_bigint_cutover_2026_07_14.sql").read_text(encoding="utf-8")
    activation = (ROOT / "scripts" / "activate_agent_inventory_snapshot_cutover_2026_07_14.sql").read_text(encoding="utf-8")
    assert "ALTER COLUMN paid_inventory_points TYPE BIGINT" in migration
    assert "ALTER COLUMN points TYPE BIGINT" in migration
    assert "ALTER COLUMN tool_credit_points TYPE BIGINT" in migration
    assert "ALTER COLUMN balance_tool_after TYPE BIGINT" in migration
    assert "ADD COLUMN IF NOT EXISTS updated_by INTEGER" in migration
    assert "agent_inventory_legacy_eligible BOOLEAN NOT NULL DEFAULT FALSE" in migration
    assert "agent_inventory_writer_generation SMALLINT" in migration
    assert "agent_inventory_writer_generation_fence_seq" in migration
    assert "pg_sequence_last_value" in migration
    assert "trg_agent_inventory_snapshot_writer_generation" in migration
    assert "BEFORE INSERT OR UPDATE OF" in migration
    assert "writer generation rejected empty snapshot order" in migration
    assert "INSERT INTO system_settings" not in migration
    assert "psql -X -v ON_ERROR_STOP=1" in migration
    assert "AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT" in activation
    assert "LOCK TABLE recharge_orders IN SHARE MODE" in activation
    assert "bigint_columns <> 13" in activation
    assert "agent_inventory_legacy_eligible = CASE" in activation
    assert "NOT agent_inventory_legacy_eligible" in activation
    assert "check_agent_inventory_writer_generation_required" in activation
    assert "setval('agent_inventory_writer_generation_fence_seq', 2, TRUE)" in activation
    assert "AGENT_INVENTORY_SNAPSHOT_HOT_ROLLBACK_READY" in activation
    assert "fresh same-image hot rollback evidence is missing" in activation
    assert "created_at >= cutover_at" not in activation
    assert "ALTER COLUMN" not in activation
    assert "clock_timestamp()::timestamp::text" in activation
    assert "marker is null or in the future" in activation
    assert "psql -X -v ON_ERROR_STOP=1" in activation
    for runtime in ("server.py", "api/scheduler.py", "start.sh", "docker-compose.yml"):
        assert "migration_agent_inventory_bigint_cutover_2026_07_14" not in (ROOT / runtime).read_text(encoding="utf-8")
        assert "activate_agent_inventory_snapshot_cutover_2026_07_14" not in (ROOT / runtime).read_text(encoding="utf-8")
    assert "ALTER TABLE system_settings" not in (ROOT / "api" / "admin_factory_api.py").read_text(encoding="utf-8")


def test_blue_green_scripts_enforce_two_phase_same_image_hot_rollback():
    deploy = (ROOT / "scripts" / "deploy-blue-green.sh").read_text(encoding="utf-8")
    rollback = (ROOT / "scripts" / "rollback-blue-green.sh").read_text(encoding="utf-8")
    capability = INVENTORY_SNAPSHOT_RUNTIME_CAPABILITY

    assert capability in deploy and capability in rollback
    assert "container_snapshot_capability" in deploy
    assert "CANDIDATE_IMAGE_ID" in deploy and "HOT_IMAGE_ID" in deploy
    assert "ACTIVE_SNAPSHOT_IMAGE_ID" in deploy
    assert "record_inventory_snapshot_hot_rollback_ready" in deploy
    assert "--force-recreate --no-build" in deploy
    assert deploy.index("HOT_IMAGE_ID") < deploy.rindex("activate_inventory_snapshot_cutover")
    assert deploy.index("ACTIVE_SNAPSHOT_IMAGE_ID") < deploy.rindex("activate_inventory_snapshot_cutover")
    assert "NGINX_CONF.deploy-tmp" in deploy
    assert "snapshot_cutover_state" in deploy
    assert "无法严格读取 inventory snapshot cutover 状态" in deploy
    assert "同 image 热回滚保持运行" in deploy

    assert "未能在 150s 内就绪；Nginx 未修改" in rollback
    assert "agent_inventory_writer_generation_fence_seq" in rollback
    assert "payment_status IS DISTINCT FROM 'paid'" in rollback
    assert "services.agent_inventory_deploy_gate" in rollback
    first_gate = rollback.index("if ! evaluate_rollback_gate; then")
    second_gate = rollback.rindex("if ! evaluate_rollback_gate; then")
    start_target = rollback.index('docker start "omnirank-$ROLLBACK_TO"')
    switch_nginx = rollback.index("sed -i")
    assert first_gate < start_target < second_gate < switch_nginx
    assert 'docker start "omnirank-$ROLLBACK_TO"' in rollback
    assert "docker compose up" not in rollback
    assert "Phase A/Phase B 均明确禁用" in rollback
    assert "保持运行作为反向热备" in rollback

    assert "AGENT_INVENTORY_SNAPSHOT_PHASE_A_ROLLBACK" in deploy
    assert deploy.rindex("record_inventory_snapshot_phase_a_rollback") < deploy.index("NGINX_CONF.deploy-tmp")
    assert "agent-inventory-snapshot-v2" not in deploy
    assert "agent-inventory-snapshot-v2" not in rollback
    activation = (ROOT / "scripts" / "activate_agent_inventory_snapshot_cutover_2026_07_14.sql").read_text(encoding="utf-8")
    assert INVENTORY_SNAPSHOT_RUNTIME_CAPABILITY in activation
    assert "agent-inventory-snapshot-v2" not in activation
    purchase = (ROOT / "api" / "agent_workbench_api.py").read_text(encoding="utf-8")
    assert "INVENTORY_PURCHASE_ACTIVATION_PENDING" in purchase
    assert purchase.index("INVENTORY_PURCHASE_ACTIVATION_PENDING") < purchase.index("INSERT INTO recharge_orders")


def _rollback_decision(**overrides):
    facts = {
        "marker_count": 0,
        "writer_generation": 1,
        "unsettled_generation2_orders": 0,
        "active_image": "sha256:candidate",
        "target_image": "sha256:legacy",
        "active_capability": INVENTORY_SNAPSHOT_RUNTIME_CAPABILITY,
        "target_capability": LEGACY_INVENTORY_RUNTIME_CAPABILITY,
        "phase_evidence_fresh": True,
        "phase_legacy_image": "sha256:legacy",
        "phase_candidate_image": "sha256:candidate",
        "phase_legacy_capability": LEGACY_INVENTORY_RUNTIME_CAPABILITY,
        "phase_candidate_capability": INVENTORY_SNAPSHOT_RUNTIME_CAPABILITY,
    }
    facts.update(overrides)
    return evaluate_inventory_rollback(**facts)


def test_rollback_decision_denies_legacy_when_gen2_may_need_settlement():
    safe_phase_a = _rollback_decision()
    assert safe_phase_a.allowed is True
    assert safe_phase_a.code == "LEGACY_PHASE_A_ALLOWED"

    pending = _rollback_decision(unsettled_generation2_orders=1)
    assert pending.allowed is False
    assert pending.code == "SNAPSHOT_TARGET_REQUIRED"

    sequence_only_cutover = _rollback_decision(writer_generation=2)
    assert sequence_only_cutover.allowed is False
    assert sequence_only_cutover.code == "SNAPSHOT_TARGET_REQUIRED"

    reviewed_broken_v2 = _rollback_decision(active_capability="agent-inventory-snapshot-v2")
    assert reviewed_broken_v2.allowed is False
    assert reviewed_broken_v2.code == "ACTIVE_CAPABILITY_INVALID"


def test_rollback_decision_allows_only_exact_same_snapshot_image_when_required():
    activated_to_legacy = _rollback_decision(marker_count=1, writer_generation=2)
    assert activated_to_legacy.allowed is False
    assert activated_to_legacy.code == "SNAPSHOT_TARGET_REQUIRED"

    mismatched = _rollback_decision(
        marker_count=1,
        writer_generation=2,
        target_image="sha256:other-snapshot",
        target_capability=INVENTORY_SNAPSHOT_RUNTIME_CAPABILITY,
    )
    assert mismatched.allowed is False
    assert mismatched.code == "SNAPSHOT_TARGET_REQUIRED"

    same_image = _rollback_decision(
        writer_generation=2,
        target_image="sha256:candidate",
        target_capability=INVENTORY_SNAPSHOT_RUNTIME_CAPABILITY,
    )
    assert same_image.allowed is True
    assert same_image.code == "SAME_IMAGE_SNAPSHOT_ALLOWED"


@pytest.mark.parametrize(
    ("extra_args", "expected_status", "expected_code"),
    [
        (["--unsettled-generation2-orders", "1"], 1, "SNAPSHOT_TARGET_REQUIRED"),
        (["--writer-generation", "2"], 1, "SNAPSHOT_TARGET_REQUIRED"),
        ([], 0, "LEGACY_PHASE_A_ALLOWED"),
    ],
)
def test_rollback_gate_cli_exit_code_matches_decision(extra_args, expected_status, expected_code):
    args = [
        sys.executable,
        "-m",
        "services.agent_inventory_deploy_gate",
        "--marker-count",
        "0",
        "--writer-generation",
        "1",
        "--unsettled-generation2-orders",
        "0",
        "--active-image",
        "sha256:candidate",
        "--target-image",
        "sha256:legacy",
        "--active-capability",
        INVENTORY_SNAPSHOT_RUNTIME_CAPABILITY,
        "--target-capability",
        LEGACY_INVENTORY_RUNTIME_CAPABILITY,
        "--phase-evidence-fresh",
        "1",
        "--phase-legacy-image",
        "sha256:legacy",
        "--phase-candidate-image",
        "sha256:candidate",
        "--phase-legacy-capability",
        LEGACY_INVENTORY_RUNTIME_CAPABILITY,
        "--phase-candidate-capability",
        INVENTORY_SNAPSHOT_RUNTIME_CAPABILITY,
    ]
    for option in ("--writer-generation", "--unsettled-generation2-orders"):
        if option in extra_args:
            index = args.index(option)
            del args[index:index + 2]
    completed = subprocess.run(args + extra_args, cwd=ROOT, text=True, capture_output=True, check=False)
    assert completed.returncode == expected_status
    assert completed.stdout.startswith(f"{expected_code}|")
