"""NOGO6 deployment discriminators for the combined pricing + WORKERS=4 release."""
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = (ROOT / "scripts" / "deploy-blue-green.sh").read_text(encoding="utf-8")
ROLLBACK = (ROOT / "scripts" / "rollback-blue-green.sh").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
MIGRATION_MANIFEST = (ROOT / "db" / "migration_manifest.py").read_text(encoding="utf-8")
INVENTORY_MIGRATION = (
    ROOT / "scripts" / "migration_agent_inventory_bigint_cutover_2026_07_14.sql"
).read_text(encoding="utf-8")


def _logical_shell_lines(source: str) -> list[str]:
    return [line.strip() for line in source.replace("\\\n", " ").splitlines()]


def test_rollback_gate_uses_reviewed_image_python_not_host_python36():
    """Old code's naked `python -m` would be a syntax error on the production 3.6 host."""

    assert re.search(
        r'docker run --rm --network none --entrypoint python "\$ACTIVE_IMAGE"\s+'
        r'-m services\.agent_inventory_deploy_gate',
        ROLLBACK.replace("\\\n", " "),
    )
    assert re.search(
        r"(?m)^\s*(?:if\s+[^\n]*\$\()?python\s+-m\s+services\.agent_inventory_deploy_gate",
        ROLLBACK,
    ) is None
    assert "REVIEWED_ROLLBACK_SCRIPT_SHA256" in ROLLBACK
    assert 'sha256sum "$SCRIPT_PATH"' in ROLLBACK
    python_commands = [
        line
        for line in _logical_shell_lines(ROLLBACK)
        if re.search(r"\bpython(?:3)?\b", line) and not line.startswith("#")
    ]
    assert python_commands
    assert all(
        "docker run" in line or "docker exec" in line
        for line in python_commands
    ), python_commands


def test_all_release_compose_run_and_up_commands_are_no_deps():
    commands = [
        line
        for line in _logical_shell_lines(DEPLOY)
        if re.search(r"\bcompose\b.*\b(?:run|up)\b", line)
    ]
    assert len(commands) >= 7
    assert all("--no-deps" in command for command in commands), commands


def test_db_and_redis_identity_are_pinned_across_application_mutations():
    assert "{{.Id}}|{{.State.StartedAt}}" in DEPLOY
    assert 'DB_CONTAINER="${DB_CONTAINER:-omnirank-db}"' in DEPLOY
    assert 'REDIS_CONTAINER="${REDIS_CONTAINER:-omnirank-redis}"' in DEPLOY
    assert DEPLOY.index("capture_infra_identity") < DEPLOY.index("compose run --rm --no-deps")
    checkpoints = re.findall(r'assert_infra_identity_unchanged "([^"]+)"', DEPLOY)
    assert {
        "prestart",
        "candidate-web-up",
        "candidate-cron-up",
        "hot-rollback-rebuild",
        "hot-rollback-fallback",
    }.issubset(set(checkpoints))


def test_cleanup_preserves_named_rollback_images():
    assert "docker image prune -f" in DEPLOY
    assert "docker image prune -af" not in DEPLOY
    assert "cleanup_pattern 'omnirank-rollback-pre-*'" in DEPLOY


def test_combined_release_uses_shared_w4_web_cron_images_and_runtime_state():
    from db.migration_manifest import MIGRATIONS

    assert all((ROOT / migration).is_file() for migration in MIGRATIONS)
    assert COMPOSE.count("ROLE: web") == 2
    assert COMPOSE.count("ROLE: cron") == 2
    for colour in ("blue", "green"):
        image = f"${{OMNIRANK_COMPOSE_PROJECT_NAME:-geo_agentscope}}-omnirank-{colour}"
        assert COMPOSE.count(f"image: {image}") == 2
        assert f"${{OMNIRANK_RUNTIME_ROOT:-.}}/logs/cron-{colour}:/app/logs" in COMPOSE
    # [WO_273 · 改指向] 原来是写死的 6 个模块名。插件后端退役删了 cache/extension_state.py,
    #   写死的名单要么跟着删一个名字(下次再删一个模块还得记得改),要么留着就红。
    #   改成与仓内真实文件对账,两个方向都锁:
    #   · 每个在库的 cache/*.py 都必须在 4 个服务里逐文件只读覆盖 —— 漏一个,容器里就会
    #     露出运行时根里那份陈旧副本(/app/cache 整个挂的是运行时根);
    #   · compose 里不许覆盖一个已经不在库的模块 —— 短语法 bind 会在宿主机上建一个同名空目录。
    tracked = {Path(p).name for p in subprocess.check_output(
        ["git", "ls-files", "cache/*.py"], cwd=ROOT, text=True).split()}
    assert {"__init__.py", "redis_client.py", "progress_bus.py"} <= tracked, "分母自证:取到了真实文件"
    mounted = set(re.findall(r"\./cache/([\w.]+\.py):/app/cache/\1:ro", COMPOSE))
    assert mounted == tracked, (sorted(mounted - tracked), sorted(tracked - mounted))
    for module in sorted(tracked):
        assert COMPOSE.count(f"./cache/{module}:/app/cache/{module}:ro") == 4
    assert "compose --profile green --profile cron-blue --profile cron-green config --quiet" in DEPLOY
    assert DEPLOY.index("prestart migration + schema verification") < DEPLOY.index(
        '启动 $INACTIVE web (--no-deps)'
    )
    assert "migration_agent_inventory_bigint_cutover_2026_07_14.sql" in MIGRATION_MANIFEST
    assert MIGRATION_MANIFEST.index("migration_pricing_dual_ssot_2026_07_12.sql") < MIGRATION_MANIFEST.index(
        "migration_agent_inventory_bigint_cutover_2026_07_14.sql"
    )
    assert MIGRATION_MANIFEST.index("migration_agent_inventory_bigint_cutover_2026_07_14.sql") < MIGRATION_MANIFEST.index(
        "migration_008_pricing_llm_first.sql"
    )
    assert "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP" in INVENTORY_MIGRATION
    assert MIGRATION_MANIFEST.index("migration_008_pricing_llm_first.sql") < MIGRATION_MANIFEST.index(
        "migration_pricing_full_fix_2026_06_13.sql"
    )
    assert DEPLOY.index("backup_before_prestart_migrations") < DEPLOY.index(
        'compose run --rm --no-deps -T -e ROLE=prestart'
    )
    assert DEPLOY.index("W4 cron 接管（切流前）") < DEPLOY.index(
        'cp "$NGINX_CONF" "$NGINX_CONF.deploy-tmp"'
    )


def test_first_w4_topology_bootstrap_quiesces_legacy_combined_writer():
    maintenance = DEPLOY.index('return 503;')
    drain = DEPLOY.index("wait_active_web_drain", maintenance)
    stop_legacy = DEPLOY.index('docker stop --time=30 "omnirank-$ACTIVE"', drain)
    prove_new_cron = DEPLOY.index('_wait_cron_leader "omnirank-cron-$INACTIVE"', stop_legacy)
    switch_candidate = DEPLOY.index(
        'proxy_pass http://127.0.0.1:$INACTIVE_PORT;', prove_new_cron
    )
    assert maintenance < drain < stop_legacy < prove_new_cron < switch_candidate


def test_running_same_color_cron_prevents_false_legacy_topology_bootstrap():
    topology = DEPLOY[DEPLOY.index("LEGACY_TOPOLOGY_BOOTSTRAP=false") :]
    topology = topology[: topology.index('if ! _wait_cron_leader "omnirank-cron-$INACTIVE"')]
    assert "ACTIVE_CRON_RUNNING=$(docker inspect" in topology
    assert '[ "$ACTIVE_WEB_ROLE" = "web" ] || [ "$ACTIVE_CRON_RUNNING" = "true" ]' in topology
    assert topology.index('if [ "$ACTIVE_CRON_RUNNING" = "true" ]') < topology.index(
        'LEGACY_TOPOLOGY_BOOTSTRAP=true'
    )
    assert 'docker stop --time=30 "omnirank-cron-$ACTIVE"' in topology


def test_nginx_failure_stops_candidate_cron_before_restoring_old_stack():
    restore = DEPLOY[DEPLOY.index("restore_legacy_web_after_failed_cron_bootstrap()") :]
    restore = restore[: restore.index("}\n")]
    assert restore.index('docker stop --time=20 "omnirank-cron-$INACTIVE"') < restore.index(
        'docker start "omnirank-$ACTIVE"'
    )
    nginx_failure = DEPLOY[DEPLOY.index("# 验证 Nginx 配置语法", DEPLOY.index("W4 cron 接管")) :]
    nginx_failure = nginx_failure[: nginx_failure.index("流量已切换到")]
    restore_w4 = DEPLOY[DEPLOY.index("restore_w4_stack_after_failed_switch()") :]
    restore_w4 = restore_w4[: restore_w4.index("}\n")]
    assert 'docker stop --time=20 "omnirank-cron-$INACTIVE"' in restore_w4
    assert 'grep -q "return 503;"' in DEPLOY
    assert 'grep -q "proxy_pass http://127.0.0.1:$INACTIVE_PORT"' in DEPLOY
    assert 'grep -q "proxy_pass http://127.0.0.1:$ROLLBACK_PORT"' in ROLLBACK
    final_switch = DEPLOY[DEPLOY.index("# 验证 Nginx 配置语法") : DEPLOY.index("流量已切换到")]
    assert "if ! nginx -s reload; then" in final_switch
    assert "restore_legacy_web_after_failed_cron_bootstrap" in final_switch
    assert "restore_w4_stack_after_failed_switch" in final_switch
    assert final_switch.index("if ! nginx -s reload; then") < final_switch.index(
        'rm -f "$NGINX_CONF.deploy-tmp"'
    )
    rollback_switch = ROLLBACK[ROLLBACK.index('cp "$NGINX_CONF" "$NGINX_CONF.rollback-tmp"') :]
    assert "if ! nginx -s reload; then" in rollback_switch
    assert rollback_switch.index("if ! nginx -s reload; then") < rollback_switch.index(
        'rm -f "$NGINX_CONF.rollback-tmp"'
    )
    assert "restore_active_cron" in rollback_switch


def test_rollback_switches_cron_only_after_pricing_gate_and_same_image_proof():
    first_gate = ROLLBACK.index("if ! evaluate_rollback_gate; then")
    cron_image = ROLLBACK.index("TARGET_CRON_IMAGE_PRE", first_gate)
    stop_active_cron = ROLLBACK.index('docker stop --time=30 "omnirank-cron-$ACTIVE"', cron_image)
    start_target_web = ROLLBACK.index('docker start "omnirank-$ROLLBACK_TO"', stop_active_cron)
    leader = ROLLBACK.index('wait_cron_leader "omnirank-cron-$ROLLBACK_TO"', start_target_web)
    second_gate = ROLLBACK.rindex("if ! evaluate_rollback_gate; then")
    nginx = ROLLBACK.index('cp "$NGINX_CONF" "$NGINX_CONF.rollback-tmp"')
    assert first_gate < cron_image < stop_active_cron < start_target_web < leader < second_gate < nginx
    assert 'if [ "$TARGET_CAPABILITY" = "$LEGACY_SNAPSHOT_CAPABILITY" ]' in ROLLBACK
    assert ROLLBACK.index('docker stop --time=30 "omnirank-cron-$ACTIVE"') < ROLLBACK.rindex(
        "wait_legacy_scheduler_lock"
    )
    restore = ROLLBACK[ROLLBACK.index("restore_active_cron()") :]
    restore = restore[: restore.index("}\n")]
    assert restore.index('docker stop --time=20 "omnirank-$ROLLBACK_TO"') < restore.index(
        'docker start "omnirank-cron-$ACTIVE"'
    )
