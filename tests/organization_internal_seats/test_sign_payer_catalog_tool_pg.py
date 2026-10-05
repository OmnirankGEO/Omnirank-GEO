"""True PostgreSQL 16 gates for the payer catalog signing tool.

外部独立审核裁决（2026-07-23）：签名工具必须是"受信 pre-fingerprint →
migration → 精确 delta → post-fingerprint"三段闸，不得把任意生产漂移直接
签成合法。本套件在一次性 schema 上实测三场景：正常路径通过并产出可粘贴
片段；人为注入多余表（fingerprint 不可见、census 可见）→ 拒绝；pre 指纹
不符（错误 --expect-pre / 已迁移 shape）→ 拒绝。仅跑 loopback 丢弃 PG16。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from verify_local import (  # noqa: E402
    BASE_SQL,
    CROSS_TENANT_MIGRATION_SQL,
    MIGRATION_SQL,
    ONBOARDING_MIGRATION_SQL,
    PAYER_MIGRATION_SQL,
    connect,
    create_schema,
    execute_sql,
    isolate_public_qualified_sql,
)

TOOL = ROOT / "scripts" / "sign_production_payer_catalog_variant.py"

DSN = os.environ.get("TEST_ORG_WALLET_DSN") or os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres:test_org_wallet_pw@localhost:55499/test_org_wallet",
)
if (urlsplit(DSN).hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
    raise RuntimeError("signing tool pytest only accepts a loopback throwaway PostgreSQL DSN")


def _build_pre_payer_schema(prefix: str, *, payer_migrated: bool = False) -> tuple[str, str]:
    schema, dsn = create_schema(DSN, prefix)
    execute_sql(dsn, BASE_SQL)
    execute_sql(dsn, MIGRATION_SQL)
    execute_sql(dsn, ONBOARDING_MIGRATION_SQL)
    execute_sql(dsn, MIGRATION_SQL)
    execute_sql(dsn, ONBOARDING_MIGRATION_SQL)
    cross_tenant_sql = isolate_public_qualified_sql(CROSS_TENANT_MIGRATION_SQL, schema)
    execute_sql(dsn, cross_tenant_sql)
    execute_sql(dsn, cross_tenant_sql)
    if payer_migrated:
        execute_sql(dsn, PAYER_MIGRATION_SQL)
        execute_sql(dsn, PAYER_MIGRATION_SQL)
    return schema, dsn


def _run_tool(*args: str) -> tuple[int, str]:
    # 显式钉住子进程 UTF-8：Windows 默认代码页（GBK/cp936）下工具内
    # sys.stdout.reconfigure(encoding='utf-8') 是主防线，env 是第二道——
    # 两道都要在，默认 cmd/PowerShell 与 PYTHONUTF8=1 两种环境才都全绿。
    completed = subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def _write_args(dsn: str, *extra: str) -> tuple[str, ...]:
    return (
        "--dsn", dsn,
        "--i-understand-prod-clone",
        "--write",
        *extra,
    )


def test_signing_tool_refuses_loopback_write_without_explicit_clone_ack():
    """localhost can be an SSH tunnel to production; loopback is never consent."""
    _, dsn = _build_pre_payer_schema("org_sign_loopback_guard")
    code, output = _run_tool(
        "--dsn", dsn, "--write", "--expect-pre", "fresh_pg16_all_accounts_v3",
    )
    assert code == 2, output
    payload = json.loads(output)
    assert "loopback may be an SSH tunnel" in payload["error"]


def test_signing_tool_happy_path_emits_reviewed_snippet():
    """正常路径：受信 pre → 迁移 → 精确 delta → post 片段。

    判别性：若工具退回"指纹直接打印"（删 pre/delta 闸），多余对象/错误 pre
    场景不再拒绝——本测试与下面两个拒绝测试一起转红。
    """
    _, dsn = _build_pre_payer_schema("org_sign_happy")
    code, output = _run_tool(*_write_args(
        dsn, "--expect-pre", "fresh_pg16_all_accounts_v3",
    ))
    assert code == 0, output
    payload = json.loads(output[: output.index("\n# =================")])
    assert payload["pre_variant"] == "fresh_pg16_all_accounts_v3"
    assert payload["delta_verified"] is True
    assert payload["delta_version"] == "organization_payer_policies_2026_07_23_v1"
    assert payload["migration_applied"] is True
    assert "此签名必须在 Review 复核后提交进新 clean SHA 才允许部署" in output
    assert "PRODUCTION_REANCHOR_PAYER_FINGERPRINT" in output
    # post shape 正是 fresh_pg16_payer_policies_v1（片段指纹可复核）
    from services.organization_schema_contract import catalog_fingerprint

    with connect(dsn) as conn:
        result = catalog_fingerprint(conn.cursor())
    assert result["matched_variant"] == "fresh_pg16_payer_policies_v1"
    assert payload["actual_fingerprint"] == result["actual_fingerprint"]


def test_signing_tool_rejects_extra_injected_table():
    """人为注入多余表（fingerprint 不可见、census 可见）→ delta 闸拒绝签名。"""
    _, dsn = _build_pre_payer_schema("org_sign_extra")
    execute_sql(dsn, "CREATE TABLE organization_payer_policies_shadow (id BIGINT PRIMARY KEY)")
    code, output = _run_tool(*_write_args(
        dsn, "--expect-pre", "fresh_pg16_all_accounts_v3",
    ))
    assert code == 2, output
    payload = json.loads(output)
    assert "does not equal the payer-migration whitelist" in payload["error"]
    assert any("organization_payer_policies_shadow" in problem for problem in payload["delta_problems"])
    assert "PRODUCTION_REANCHOR_PAYER_FINGERPRINT" not in output


def test_signing_tool_rejects_untrusted_pre():
    """pre 指纹不符：错误 --expect-pre 钉错变体、已迁移 shape 重签 → 拒绝并打印实际指纹。"""
    _, dsn = _build_pre_payer_schema("org_sign_wrong_pre")
    code, output = _run_tool(*_write_args(
        dsn, "--expect-pre", "production_reanchor_all_accounts_v3",
    ))
    assert code == 2, output
    payload = json.loads(output)
    assert "does not match --expect-pre" in payload["error"]
    assert payload["actual_pre_variant"] == "fresh_pg16_all_accounts_v3"
    assert len(payload["actual_pre_fingerprint"]) == 64
    assert "PRODUCTION_REANCHOR_PAYER_FINGERPRINT" not in output

    _, migrated_dsn = _build_pre_payer_schema("org_sign_migrated", payer_migrated=True)
    code, output = _run_tool(*_write_args(
        migrated_dsn, "--expect-pre", "fresh_pg16_all_accounts_v3",
    ))
    assert code == 2, output
    payload = json.loads(output)
    assert "does not match any trusted baseline" in payload["error"]
    assert payload["matched_existing_variant"] == "fresh_pg16_payer_policies_v1"
    assert len(payload["actual_pre_fingerprint"]) == 64


def test_signing_tool_dry_run_never_emits_snippet_for_untrusted_shape():
    """dry-run 零写入：受信 pre 提示 --write；不受信 shape 拒签且无片段。"""
    _, dsn = _build_pre_payer_schema("org_sign_dry_pre")
    code, output = _run_tool("--dsn", dsn)
    assert code == 0, output
    payload = json.loads(output)
    assert payload["trusted_pre_baseline"] == "fresh_pg16_all_accounts_v3"
    assert payload["migration_applied"] is False
    assert "PRODUCTION_REANCHOR_PAYER_FINGERPRINT" not in output

    _, drift_dsn = _build_pre_payer_schema("org_sign_dry_drift")
    execute_sql(drift_dsn, "ALTER TABLE organizations ADD COLUMN drift_marker TEXT")
    code, output = _run_tool("--dsn", drift_dsn)
    assert code == 2, output
    assert "PRODUCTION_REANCHOR_PAYER_FINGERPRINT" not in output


def test_signing_tool_rejects_wrong_migration_sha256():
    """migration 文件钉版：--expect-migration-sha256 与实际文件不符 → 拒签。

    判别性：删掉 SHA 闸 → 篡改/错位 migration 文本被照常执行并签名，本
    断言转红。"""
    _, dsn = _build_pre_payer_schema("org_sign_sha")
    code, output = _run_tool(
        *_write_args(dsn, "--expect-pre", "fresh_pg16_all_accounts_v3"),
        "--expect-migration-sha256", "0" * 64,
    )
    assert code == 2, output
    payload = json.loads(output)
    assert "sha256 does not match" in payload["error"]
    assert payload["expected_migration_sha256"] == "0" * 64
    assert len(payload["actual_migration_sha256"]) == 64
    assert "PRODUCTION_REANCHOR_PAYER_FINGERPRINT" not in output
    # 默认内置哈希 = 当前签发文件真实哈希（happy path 已隐含覆盖，这里显式钉死）
    import hashlib

    actual = hashlib.sha256((ROOT / "scripts" / "migration_organization_payer_policies_2026_07_23.sql").read_bytes()).hexdigest()
    assert actual == payload["actual_migration_sha256"]


# ===========================================================================
# 逐值精确冻结 delta 闸：审核员攻击样本全复现（真 PG16 容器实测）
# ===========================================================================


def _load_tool_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("sign_production_payer_catalog_variant", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tamper(sql: str, old: str, new: str) -> str:
    assert old in sql, f"tamper anchor missing: {old[:80]}"
    return sql.replace(old, new, 1)


def _delta_problems(prefix: str, tampered_sql: str | None) -> list:
    """pre-payer schema →（可选）篡改 migration 落库 → 工具 delta 闸实测。"""
    tool = _load_tool_module()
    _, dsn = _build_pre_payer_schema(prefix)
    pre = tool._snapshot(dsn)
    execute_sql(dsn, tampered_sql if tampered_sql is not None else PAYER_MIGRATION_SQL)
    post = tool._snapshot(dsn)
    return tool._verify_delta(pre, post)


def test_delta_gate_accepts_exact_pinned_migration():
    """对照组：未篡改的钉版 migration → delta 零问题。

    判别性：内置期望清单若与 PG16 真实输出漂移（如 pg_get_constraintdef
    文本写错），本测试立即转红——期望清单的正确性由真容器钉死。"""
    problems = _delta_problems("org_sign_exact", None)
    assert problems == [], problems


def test_delta_gate_rejects_wrong_column_type_on_new_table():
    """错误列类型（新表 policy_version INTEGER→BIGINT）→ 拒签。"""
    tampered = _tamper(
        PAYER_MIGRATION_SQL,
        "policy_version INTEGER NOT NULL DEFAULT 1",
        "policy_version BIGINT NOT NULL DEFAULT 1",
    )
    problems = _delta_problems("org_sign_coltype", tampered)
    assert any(
        "mismatched columns definition: organization_payer_policies.policy_version" in problem
        for problem in problems
    ), problems


def test_delta_gate_rejects_wrong_column_type_on_existing_table():
    """错误列类型（既有表新列 within_limit_points BIGINT→INTEGER）→ 拒签。"""
    tampered = _tamper(
        PAYER_MIGRATION_SQL,
        "ADD COLUMN IF NOT EXISTS within_limit_points BIGINT NOT NULL DEFAULT 0",
        "ADD COLUMN IF NOT EXISTS within_limit_points INTEGER NOT NULL DEFAULT 0",
    )
    problems = _delta_problems("org_sign_existype", tampered)
    assert any(
        "mismatched columns definition: organization_charge_links.within_limit_points" in problem
        for problem in problems
    ), problems


def test_delta_gate_rejects_check_true_on_new_constraint():
    """CHECK(TRUE)（organization_charge_payer_split_valid 被掏空）→ 拒签。"""
    tampered = _tamper(
        PAYER_MIGRATION_SQL,
        "within_limit_points >= 0 AND overage_points >= 0\n                AND within_limit_points + overage_points <= reserved_ceiling_points",
        "TRUE",
    )
    problems = _delta_problems("org_sign_checktrue", tampered)
    assert any(
        "mismatched constraints definition: organization_charge_links.organization_charge_payer_split_valid" in problem
        for problem in problems
    ), problems


def test_delta_gate_rejects_wrong_index_definition():
    """错误索引（idx_org_payer_policy_events_org 只剩首列）→ 拒签。"""
    tampered = _tamper(
        PAYER_MIGRATION_SQL,
        "ON organization_payer_policy_events(organization_id,created_at DESC,id DESC)",
        "ON organization_payer_policy_events(organization_id)",
    )
    problems = _delta_problems("org_sign_badindex", tampered)
    assert any(
        "mismatched indexes definition: organization_payer_policy_events.idx_org_payer_policy_events_org" in problem
        for problem in problems
    ), problems


def test_delta_gate_rejects_extra_column_on_new_table():
    """新表额外列（organization_payer_policies 多一个 backdoor 列）→ 拒签。"""
    tampered = _tamper(
        PAYER_MIGRATION_SQL,
        "    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),\n    CONSTRAINT pk_organization_payer_policies",
        "    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),\n    backdoor TEXT,\n    CONSTRAINT pk_organization_payer_policies",
    )
    problems = _delta_problems("org_sign_extracol", tampered)
    assert any(
        "unexpected columns on organization_payer_policies" in problem and "backdoor" in problem
        for problem in problems
    ), problems


def test_delta_gate_rejects_tampered_trigger_definition():
    """篡改触发器定义（同名但 BEFORE→AFTER）→ 拒签。"""
    tampered = _tamper(
        PAYER_MIGRATION_SQL,
        "BEFORE UPDATE OR DELETE ON organization_payer_policy_events",
        "AFTER UPDATE OR DELETE ON organization_payer_policy_events",
    )
    problems = _delta_problems("org_sign_badtrg", tampered)
    assert any(
        "mismatched trigger definition: organization_payer_policy_events.trg_org_payer_policy_events_append_only" in problem
        for problem in problems
    ), problems


def test_delta_gate_rejects_tampered_function_body():
    """篡改函数体（append-only RAISE 被换成 RETURN NEW 静默放行）→ 拒签。"""
    tampered = _tamper(
        PAYER_MIGRATION_SQL,
        "RAISE EXCEPTION 'organization payer policy events are append-only';",
        "RETURN NEW;",
    )
    problems = _delta_problems("org_sign_badfunc", tampered)
    assert any(
        "mismatched function definition: organization_payer_policy_events_append_only" in problem
        for problem in problems
    ), problems
