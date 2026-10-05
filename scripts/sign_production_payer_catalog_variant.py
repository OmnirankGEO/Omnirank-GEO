"""Sign the production-reanchor catalog variant after the payer-policy migration.

Deployment-window tool for agent-24 P0-1. 外部独立审核裁决（2026-07-23）后，
本工具不再是"把当前生产漂移直接签成合法"的指纹打印器，而是受信三段闸：

  1. 受信 pre-fingerprint：迁移前 shape 必须匹配内置已知 pre 变体之一
     （fresh_pg16_all_accounts_v3 / production_fixture_reanchor_all_accounts_v3 /
     production_reanchor_all_accounts_v3，即 services/organization_schema_contract.py
     ACCEPTED_CATALOG_VARIANTS 中 payer 迁移前已签名的 shape）。``--expect-pre``
     可进一步钉死具体变体名或指纹；不匹配即拒绝并打印实际指纹供人工核对。
  2. 精确 delta：执行幂等 payer 迁移后，对比 pre/post catalog（列/约束/索引/
     触发器/函数/表普查），delta 必须逐对象等于 payer 迁移白名单——2 张新表
     （organization_payer_policies + organization_payer_policy_events）、
     organization_charge_links 的 5 个新列、2 个新 CHECK 约束、1 个新索引、
     1 个新触发器 + 1 个新函数（清单版本随 migration SQL
     organization_payer_policies_2026_07_23_v1 固定）。任何额外/缺失/被改动的
     既有对象都拒绝签名并输出差异。
     外部独立审核裁决（2026-07-23）后，delta 校验从"只核对象名"升级为
     **逐值精确冻结**：新表逐列 name+ordinal+type+null+default、CHECK/FK
     比 pg_get_constraintdef 完整定义（空白规范化）、索引比
     pg_get_indexdef 全定义、触发器比 pg_get_triggerdef 完整定义（含
     WHEN/EXECUTE 目标）、函数比 pg_get_functiondef 完整定义（schema 前缀
     剥离 + 空白规范化，函数体全量参与比对）——错误列类型、CHECK(TRUE)、
     错误索引、新表额外列、篡改触发器定义、篡改函数体全部拒签。
  2.5 migration 文件钉版：内置已评审 migration 的 SHA-256
     （EXPECTED_MIGRATION_SHA256），可用 --expect-migration-sha256 轮换；
     文件实际哈希与钉版不一致即拒签，绝不执行/签署未评审的迁移文本。
  3. 全部通过才输出 post-fingerprint 常量片段，并注明：此签名必须在 Review
     复核后提交进新 clean SHA 才允许部署。

Safety rails:
  - DSN is accepted only from the command line; nothing is read from env.
  - Non-loopback DSNs are refused unless ``--i-understand-prod-clone`` is
    given explicitly (the tool is meant for a schema-only clone, never the
    live production database); loopback DSNs are throwaway test containers
    by convention, so ``--write`` always targets an explicit clone/test
    sentinel, never an implicit production-looking target.
  - Default is dry-run: fingerprint only, zero writes, and never emits a
    signing snippet for an unverified shape.  ``--write`` performs the
    idempotent migration first (executed twice, mirroring prestart semantics)
    in the SAME transaction as the delta verification — any refusal or
    exception rolls the whole transaction back, so a refused signing leaves
    the database with zero changes (2026-07-23 统一 R3 §六).
  - The full DSN (which may embed credentials) is never printed.

Usage:
  python scripts/sign_production_payer_catalog_variant.py --dsn "postgresql://user:pass@clone-host:5432/geo_agentscope_clone" --i-understand-prod-clone --write --expect-pre production_reanchor_all_accounts_v3
  python scripts/sign_production_payer_catalog_variant.py --dsn "postgresql://postgres:pw@localhost:55499/test_org_wallet?options=-csearch_path%3D<schema>" --i-understand-prod-clone --write --expect-pre fresh_pg16_all_accounts_v3
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PAYER_MIGRATION_PATH = ROOT / "scripts" / "migration_organization_payer_policies_2026_07_23.sql"
VARIANT_NAME = "production_reanchor_payer_policies_v1"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# 已评审 migration 文本的钉版 SHA-256（2026-07-23 签发版）。迁移 SQL 任何
# 变更都必须经 Review 复核后同步轮换本常量与下方期望清单；文件实际哈希与
# 钉版不一致时工具拒签，绝不执行未评审文本。
EXPECTED_MIGRATION_SHA256 = "2bb731dd8ad54315ab67cb20d969e2e5619e9def5c4d92adcb30dbf729455ae0"


def _configure_stdout_utf8() -> None:
    """Windows 默认代码页（GBK/cp936 等）下中文输出会按 ANSI 编码落管，
    调用方按 UTF-8 解码即乱码。强制 stdout/stderr UTF-8；reconfigure 在
    重定向流或旧解释器上可能不存在，guard 兼容。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - 输出编码尽力而为，不阻断签名闸
                pass

# 受信 pre 变体：payer 迁移（2026-07-23）前已签名的 shape。
# 仅这些 shape 允许作为 payer 迁移签名基线；其他任何 pre 漂移一律人工核对。
TRUSTED_PRE_VARIANT_NAMES = frozenset({
    "fresh_pg16_all_accounts_v3",
    "production_fixture_reanchor_all_accounts_v3",
    "production_reanchor_all_accounts_v3",
})

# payer 迁移白名单 delta，版本随 scripts/migration_organization_payer_policies_2026_07_23.sql
# 的 organization_payer_policies_2026_07_23_v1 固定。迁移 SQL 变更必须同步轮换本清单
# 与 EXPECTED_MIGRATION_SHA256。
#
# 取值 = PostgreSQL 16 对钉版 migration（SHA-256 见 EXPECTED_MIGRATION_SHA256）在
# 干净 schema 上的真实 pg_catalog 输出（format_type/pg_get_expr/
# pg_get_constraintdef/pg_get_indexdef/pg_get_triggerdef/pg_get_functiondef）。
# 比对前对定义文本做空白规范化（_normalize_sql_text），对 PG16 次要版本的
# pretty 打印换行/缩进差异免疫，表达式/谓词/函数体语义全量参与比对。
PAYER_MIGRATION_DELTA_VERSION = "organization_payer_policies_2026_07_23_v1"
EXPECTED_NEW_TABLES = frozenset({
    "organization_payer_policies",
    "organization_payer_policy_events",
})

# 新表全列精确冻结：name+ordinal+type+null+default（新表列序由迁移 SQL 固定，
# 不依赖 pre 变体，ordinal 一并钉死）。
EXPECTED_NEW_TABLE_COLUMN_DEFS = (
    {"table_name": "organization_payer_policies", "column_name": "organization_id", "ordinal": 1, "data_type": "bigint", "not_null": True, "identity": "", "generated": "", "default_expr": None},
    {"table_name": "organization_payer_policies", "column_name": "shared_payer_enabled", "ordinal": 2, "data_type": "boolean", "not_null": True, "identity": "", "generated": "", "default_expr": "false"},
    {"table_name": "organization_payer_policies", "column_name": "overage_enabled", "ordinal": 3, "data_type": "boolean", "not_null": True, "identity": "", "generated": "", "default_expr": "false"},
    {"table_name": "organization_payer_policies", "column_name": "per_action_limit_points", "ordinal": 4, "data_type": "bigint", "not_null": False, "identity": "", "generated": "", "default_expr": None},
    {"table_name": "organization_payer_policies", "column_name": "daily_limit_points", "ordinal": 5, "data_type": "bigint", "not_null": False, "identity": "", "generated": "", "default_expr": None},
    {"table_name": "organization_payer_policies", "column_name": "monthly_limit_points", "ordinal": 6, "data_type": "bigint", "not_null": False, "identity": "", "generated": "", "default_expr": None},
    {"table_name": "organization_payer_policies", "column_name": "policy_version", "ordinal": 7, "data_type": "integer", "not_null": True, "identity": "", "generated": "", "default_expr": "1"},
    {"table_name": "organization_payer_policies", "column_name": "updated_by_owner_user_id", "ordinal": 8, "data_type": "integer", "not_null": False, "identity": "", "generated": "", "default_expr": None},
    {"table_name": "organization_payer_policies", "column_name": "enabled_at", "ordinal": 9, "data_type": "timestamp with time zone", "not_null": False, "identity": "", "generated": "", "default_expr": None},
    {"table_name": "organization_payer_policies", "column_name": "disabled_at", "ordinal": 10, "data_type": "timestamp with time zone", "not_null": False, "identity": "", "generated": "", "default_expr": None},
    {"table_name": "organization_payer_policies", "column_name": "reason", "ordinal": 11, "data_type": "text", "not_null": False, "identity": "", "generated": "", "default_expr": None},
    {"table_name": "organization_payer_policies", "column_name": "created_at", "ordinal": 12, "data_type": "timestamp with time zone", "not_null": True, "identity": "", "generated": "", "default_expr": "now()"},
    {"table_name": "organization_payer_policies", "column_name": "updated_at", "ordinal": 13, "data_type": "timestamp with time zone", "not_null": True, "identity": "", "generated": "", "default_expr": "now()"},
    {"table_name": "organization_payer_policy_events", "column_name": "id", "ordinal": 1, "data_type": "bigint", "not_null": True, "identity": "", "generated": "", "default_expr": "nextval('organization_payer_policy_events_id_seq'::regclass)"},
    {"table_name": "organization_payer_policy_events", "column_name": "event_key", "ordinal": 2, "data_type": "text", "not_null": True, "identity": "", "generated": "", "default_expr": None},
    {"table_name": "organization_payer_policy_events", "column_name": "organization_id", "ordinal": 3, "data_type": "bigint", "not_null": True, "identity": "", "generated": "", "default_expr": None},
    {"table_name": "organization_payer_policy_events", "column_name": "request_id", "ordinal": 4, "data_type": "text", "not_null": True, "identity": "", "generated": "", "default_expr": None},
    {"table_name": "organization_payer_policy_events", "column_name": "actor_user_id", "ordinal": 5, "data_type": "integer", "not_null": False, "identity": "", "generated": "", "default_expr": None},
    {"table_name": "organization_payer_policy_events", "column_name": "actor_kind", "ordinal": 6, "data_type": "text", "not_null": True, "identity": "", "generated": "", "default_expr": None},
    {"table_name": "organization_payer_policy_events", "column_name": "source_ip", "ordinal": 7, "data_type": "text", "not_null": False, "identity": "", "generated": "", "default_expr": None},
    {"table_name": "organization_payer_policy_events", "column_name": "old_snapshot", "ordinal": 8, "data_type": "jsonb", "not_null": False, "identity": "", "generated": "", "default_expr": None},
    {"table_name": "organization_payer_policy_events", "column_name": "new_snapshot", "ordinal": 9, "data_type": "jsonb", "not_null": True, "identity": "", "generated": "", "default_expr": None},
    {"table_name": "organization_payer_policy_events", "column_name": "reason", "ordinal": 10, "data_type": "text", "not_null": False, "identity": "", "generated": "", "default_expr": None},
    {"table_name": "organization_payer_policy_events", "column_name": "created_at", "ordinal": 11, "data_type": "timestamp with time zone", "not_null": True, "identity": "", "generated": "", "default_expr": "now()"},
)

# 新表全约束精确冻结：CHECK 比 pg_get_constraintdef 完整定义（含表达式文本
# 规范化），PK/UNIQUE/FK 同样比完整定义。
EXPECTED_NEW_TABLE_CONSTRAINT_DEFS = (
    {"table_name": "organization_payer_policies", "name": "organization_payer_policies_daily_limit_points_check", "type": "c", "validated": True, "definition": "CHECK (daily_limit_points IS NULL OR daily_limit_points >= 0)"},
    {"table_name": "organization_payer_policies", "name": "organization_payer_policies_monthly_limit_points_check", "type": "c", "validated": True, "definition": "CHECK (monthly_limit_points IS NULL OR monthly_limit_points >= 0)"},
    {"table_name": "organization_payer_policies", "name": "organization_payer_policies_organization_id_fkey", "type": "f", "validated": True, "definition": "FOREIGN KEY (organization_id) REFERENCES organizations(id)"},
    {"table_name": "organization_payer_policies", "name": "organization_payer_policies_per_action_limit_points_check", "type": "c", "validated": True, "definition": "CHECK (per_action_limit_points IS NULL OR per_action_limit_points >= 0)"},
    {"table_name": "organization_payer_policies", "name": "organization_payer_policies_policy_version_check", "type": "c", "validated": True, "definition": "CHECK (policy_version >= 1)"},
    {"table_name": "organization_payer_policies", "name": "organization_payer_policies_updated_by_owner_user_id_fkey", "type": "f", "validated": True, "definition": "FOREIGN KEY (updated_by_owner_user_id) REFERENCES users(id)"},
    {"table_name": "organization_payer_policies", "name": "organization_payer_policy_enabled_limits_required", "type": "c", "validated": True, "definition": "CHECK (NOT shared_payer_enabled AND NOT overage_enabled OR per_action_limit_points IS NOT NULL AND daily_limit_points IS NOT NULL AND monthly_limit_points IS NOT NULL)"},
    {"table_name": "organization_payer_policies", "name": "pk_organization_payer_policies", "type": "p", "validated": True, "definition": "PRIMARY KEY (organization_id)"},
    {"table_name": "organization_payer_policy_events", "name": "organization_payer_policy_events_actor_kind_check", "type": "c", "validated": True, "definition": "CHECK (actor_kind = ANY (ARRAY['owner'::text, 'platform_admin'::text, 'system'::text]))"},
    {"table_name": "organization_payer_policy_events", "name": "organization_payer_policy_events_actor_user_id_fkey", "type": "f", "validated": True, "definition": "FOREIGN KEY (actor_user_id) REFERENCES users(id)"},
    {"table_name": "organization_payer_policy_events", "name": "organization_payer_policy_events_event_key_check", "type": "c", "validated": True, "definition": "CHECK (btrim(event_key) <> ''::text)"},
    {"table_name": "organization_payer_policy_events", "name": "organization_payer_policy_events_event_key_key", "type": "u", "validated": True, "definition": "UNIQUE (event_key)"},
    {"table_name": "organization_payer_policy_events", "name": "organization_payer_policy_events_organization_id_fkey", "type": "f", "validated": True, "definition": "FOREIGN KEY (organization_id) REFERENCES organizations(id)"},
    {"table_name": "organization_payer_policy_events", "name": "organization_payer_policy_events_pkey", "type": "p", "validated": True, "definition": "PRIMARY KEY (id)"},
    {"table_name": "organization_payer_policy_events", "name": "organization_payer_policy_events_request_id_check", "type": "c", "validated": True, "definition": "CHECK (btrim(request_id) <> ''::text)"},
)

# 新表全索引精确冻结：比 pg_get_indexdef 完整定义 + 唯一性/就绪态/键列数/
# 谓词/opclass 序列。
EXPECTED_NEW_TABLE_INDEX_DEFS = (
    {"table_name": "organization_payer_policies", "name": "pk_organization_payer_policies", "is_unique": True, "is_valid": True, "is_ready": True, "key_count": 1, "definition": "CREATE UNIQUE INDEX pk_organization_payer_policies ON organization_payer_policies USING btree (organization_id)", "predicate": None, "opclasses": ["int8_ops"]},
    {"table_name": "organization_payer_policy_events", "name": "idx_org_payer_policy_events_org", "is_unique": False, "is_valid": True, "is_ready": True, "key_count": 3, "definition": "CREATE INDEX idx_org_payer_policy_events_org ON organization_payer_policy_events USING btree (organization_id, created_at DESC, id DESC)", "predicate": None, "opclasses": ["int8_ops", "timestamptz_ops", "int8_ops"]},
    {"table_name": "organization_payer_policy_events", "name": "organization_payer_policy_events_event_key_key", "is_unique": True, "is_valid": True, "is_ready": True, "key_count": 1, "definition": "CREATE UNIQUE INDEX organization_payer_policy_events_event_key_key ON organization_payer_policy_events USING btree (event_key)", "predicate": None, "opclasses": ["text_ops"]},
    {"table_name": "organization_payer_policy_events", "name": "organization_payer_policy_events_pkey", "is_unique": True, "is_valid": True, "is_ready": True, "key_count": 1, "definition": "CREATE UNIQUE INDEX organization_payer_policy_events_pkey ON organization_payer_policy_events USING btree (id)", "predicate": None, "opclasses": ["int8_ops"]},
)

# 既有表 organization_charge_links 的 5 个新列精确冻结：name+type+null+
# default 逐值比对。不钉 ordinal——legacy 列数随受信 pre 变体不同
# （fresh=505 / production_reanchor=506），5 列的绝对序位随基线平移；最终
# 签发指纹仍逐字节固化实际序位，delta 闸只需钉住定义。
EXPECTED_NEW_COLUMN_DEFS_ON_EXISTING = (
    {"table_name": "organization_charge_links", "column_name": "payer_policy_version", "data_type": "integer", "not_null": False, "identity": "", "generated": "", "default_expr": None},
    {"table_name": "organization_charge_links", "column_name": "owner_consent_snapshot", "data_type": "jsonb", "not_null": False, "identity": "", "generated": "", "default_expr": None},
    {"table_name": "organization_charge_links", "column_name": "employee_limit_snapshot", "data_type": "jsonb", "not_null": False, "identity": "", "generated": "", "default_expr": None},
    {"table_name": "organization_charge_links", "column_name": "within_limit_points", "data_type": "bigint", "not_null": True, "identity": "", "generated": "", "default_expr": "0"},
    {"table_name": "organization_charge_links", "column_name": "overage_points", "data_type": "bigint", "not_null": True, "identity": "", "generated": "", "default_expr": "0"},
)
EXPECTED_NEW_COLUMNS_ON_EXISTING = frozenset(
    (row["table_name"], row["column_name"]) for row in EXPECTED_NEW_COLUMN_DEFS_ON_EXISTING
)

# 既有表 2 个新 CHECK 约束：完整定义比对（含 NOT VALID→VALIDATE 后的
# validated 终态）。
EXPECTED_NEW_CONSTRAINT_DEFS_ON_EXISTING = (
    {"table_name": "organization_charge_links", "name": "organization_charge_overage_consent_required", "type": "c", "validated": True, "definition": "CHECK (overage_points = 0 OR payer_policy_version IS NOT NULL AND owner_consent_snapshot IS NOT NULL)"},
    {"table_name": "organization_charge_links", "name": "organization_charge_payer_split_valid", "type": "c", "validated": True, "definition": "CHECK (within_limit_points >= 0 AND overage_points >= 0 AND (within_limit_points + overage_points) <= reserved_ceiling_points)"},
)
EXPECTED_NEW_CONSTRAINTS_ON_EXISTING = frozenset(
    (row["table_name"], row["name"]) for row in EXPECTED_NEW_CONSTRAINT_DEFS_ON_EXISTING
)

# 既有表 1 个新索引：完整定义比对（含 WHERE 谓词与 opclass）。
EXPECTED_NEW_INDEX_DEFS_ON_EXISTING = (
    {"table_name": "organization_charge_links", "name": "idx_org_charge_overage_period", "is_unique": False, "is_valid": True, "is_ready": True, "key_count": 2, "definition": "CREATE INDEX idx_org_charge_overage_period ON organization_charge_links USING btree (organization_id, created_at) WHERE (overage_points > 0)", "predicate": "overage_points > 0", "opclasses": ["int8_ops", "timestamptz_ops"]},
)
EXPECTED_NEW_INDEXES_ON_EXISTING = frozenset(
    (row["table_name"], row["name"]) for row in EXPECTED_NEW_INDEX_DEFS_ON_EXISTING
)

# 新触发器：完整定义比对（pg_get_triggerdef，含事件集/时机/WHEN 条件与
# EXECUTE FUNCTION 目标）。同名不同定义的触发器拒签。
EXPECTED_NEW_TRIGGER_DEFS = {
    ("organization_payer_policy_events", "trg_org_payer_policy_events_append_only"): "CREATE TRIGGER trg_org_payer_policy_events_append_only BEFORE DELETE OR UPDATE ON organization_payer_policy_events FOR EACH ROW EXECUTE FUNCTION organization_payer_policy_events_append_only()",
}
EXPECTED_NEW_TRIGGERS = frozenset(EXPECTED_NEW_TRIGGER_DEFS)

# 新函数：pg_get_functiondef 完整定义比对（schema 前缀剥离 + 空白规范化）。
# 选择"全量定义比对"而非关键语句白名单：该函数仅一条 RAISE 语句，全量
# 比对更严格——函数体被改成 RETURN NEW（静默放行 append-only 篡改）即
# 定义不符拒签。
EXPECTED_NEW_FUNCTION_DEFS = {
    "organization_payer_policy_events_append_only": {
        "args": "",
        "definition": "CREATE OR REPLACE FUNCTION organization_payer_policy_events_append_only()\n RETURNS trigger\n LANGUAGE plpgsql\nAS $function$\nBEGIN\n    RAISE EXCEPTION 'organization payer policy events are append-only';\nEND;\n$function$\n",
    },
}
EXPECTED_NEW_FUNCTIONS = frozenset(EXPECTED_NEW_FUNCTION_DEFS)


class _Refusal(Exception):
    """Signing gate refusal carrying a JSON payload; main() prints and exits 2."""

    def __init__(self, payload: str) -> None:
        super().__init__(payload)
        self.payload = payload


def _connect(dsn: str):
    conn = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    return conn


def _migration_sql() -> str:
    return PAYER_MIGRATION_PATH.read_text(encoding="utf-8")


def _apply_payer_migration_tx(cursor) -> None:
    """Idempotent payer migration, executed twice (prestart rerun semantics).

    2026-07-23 统一 R3 §六：migration 与后续 delta 验证必须跑在同一事务里，
    由调用方在验证失败后整体 rollback——拒签时数据库零变化。"""
    sql = _migration_sql()
    cursor.execute("SET lock_timeout='15s'")
    cursor.execute(sql)
    cursor.execute(sql)


def _verify_migration_sha256(expected_sha256: str) -> str:
    """Pin the migration text: actual file SHA-256 must equal the reviewed hash.

    任何与钉版不一致的 migration 文件都视为未评审文本——不执行、不签署，
    输出实际哈希供人工核对。返回实际哈希（已校验一致）。"""
    actual = sha256(PAYER_MIGRATION_PATH.read_bytes()).hexdigest()
    expected = str(expected_sha256 or "").strip().lower()
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise _Refusal(
            json.dumps(
                {
                    "error": "--expect-migration-sha256 must be a 64-char hex sha256",
                    "expect_migration_sha256": expected_sha256,
                },
                ensure_ascii=False,
            )
        )
    if actual != expected:
        raise _Refusal(
            json.dumps(
                {
                    "error": "migration file sha256 does not match the pinned expectation; "
                    "refusing to run or sign an unreviewed migration",
                    "migration_file": PAYER_MIGRATION_PATH.name,
                    "delta_version": PAYER_MIGRATION_DELTA_VERSION,
                    "expected_migration_sha256": expected,
                    "actual_migration_sha256": actual,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return actual


def _row_key(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)


def _trigger_snapshot(cursor) -> set[str]:
    """Trigger identity surface on every organization-covered table."""
    from services.organization_schema_contract import ACTOR_ARTIFACT_TABLES, ORGANIZATION_TABLES

    cursor.execute(
        """
        SELECT cl.relname AS table_name,tg.tgname AS name,
               pg_get_triggerdef(tg.oid,TRUE) AS definition
        FROM pg_trigger tg
        JOIN pg_class cl ON cl.oid=tg.tgrelid
        JOIN pg_namespace ns ON ns.oid=cl.relnamespace
        WHERE ns.nspname=current_schema() AND NOT tg.tgisinternal
          AND cl.relname=ANY(%s)
        ORDER BY cl.relname,tg.tgname
        """,
        (list(ORGANIZATION_TABLES) + list(ACTOR_ARTIFACT_TABLES),),
    )
    return {_row_key(dict(row)) for row in cursor.fetchall()}


def _function_snapshot(cursor) -> set[str]:
    """User-defined function identity surface in the current schema.

    含 pg_get_functiondef 完整定义（schema 前缀剥离，便于跨 schema 比对）——
    函数体被篡改（例如 append-only 函数改成 RETURN NEW 静默放行）会改变
    定义文本，delta 闸按定义不符拒签。"""
    # 注意：本查询无参数绑定，psycopg2 原样发送 SQL，format('%I.', ...) 用
    # 单百分号（带参数绑定的查询才需要 %%I 转义）。
    cursor.execute(
        """
        SELECT p.proname AS name,pg_get_function_identity_arguments(p.oid) AS args,
               replace(pg_get_functiondef(p.oid),format('%I.',current_schema()),'') AS definition
        FROM pg_proc p
        JOIN pg_namespace ns ON ns.oid=p.pronamespace
        WHERE ns.nspname=current_schema() AND p.prokind='f'
        ORDER BY p.proname
        """
    )
    return {_row_key(dict(row)) for row in cursor.fetchall()}


def _table_census(cursor) -> set[str]:
    """Every organization/actor-artifact table physically present in the schema."""
    from services.organization_schema_contract import ACTOR_ARTIFACT_TABLES

    cursor.execute(
        """
        SELECT tablename FROM pg_tables
        WHERE schemaname=current_schema()
          AND (tablename='organizations' OR tablename LIKE 'organization\\_%%' ESCAPE '\\' OR tablename=ANY(%s))
        ORDER BY tablename
        """,
        (list(ACTOR_ARTIFACT_TABLES),),
    )
    return {str(row["tablename"]) for row in cursor.fetchall()}


def _snapshot_cursor(cursor) -> dict:
    """Full signing surface on an existing cursor (single-transaction mode)."""
    from services.organization_schema_contract import catalog_fingerprint, catalog_snapshot

    catalog = catalog_snapshot(cursor)
    return {
        "fingerprint": catalog_fingerprint(cursor),
        "catalog": {key: {_row_key(row) for row in value} for key, value in catalog.items()},
        "triggers": _trigger_snapshot(cursor),
        "functions": _function_snapshot(cursor),
        "tables": _table_census(cursor),
    }


def _snapshot(dsn: str) -> dict:
    """Full signing surface: catalog fingerprint + trigger/function/table probes."""
    with _connect(dsn) as conn:
        with conn.cursor() as cursor:
            return _snapshot_cursor(cursor)


def _trusted_pre_variants() -> dict:
    from services.organization_schema_contract import ACCEPTED_CATALOG_VARIANTS

    return {
        name: value
        for name, value in ACCEPTED_CATALOG_VARIANTS.items()
        if name in TRUSTED_PRE_VARIANT_NAMES
    }


def _match_trusted_pre(fingerprint_result: dict) -> str | None:
    matched = fingerprint_result.get("matched_variant")
    return matched if matched in TRUSTED_PRE_VARIANT_NAMES else None


def _resolve_expect_pre(expect_pre: str) -> tuple[str, dict, str]:
    """Resolve --expect-pre (variant name or fingerprint) to a trusted pre variant."""
    trusted = _trusted_pre_variants()
    if expect_pre in trusted:
        return expect_pre, trusted[expect_pre][0], trusted[expect_pre][1]
    for name, (counts, fingerprint) in trusted.items():
        if expect_pre == fingerprint:
            return name, counts, fingerprint
    raise _Refusal(
        json.dumps(
            {
                "error": "--expect-pre is not a trusted pre-payer variant",
                "expect_pre": expect_pre,
                "trusted_pre_variants": sorted(TRUSTED_PRE_VARIANT_NAMES),
            },
            ensure_ascii=False,
        )
    )


def _verify_pre(dsn: str, expect_pre: str | None) -> tuple[dict, str]:
    """Gate 1: pre-migration shape must match a trusted baseline (optionally pinned)."""
    pre = _snapshot(dsn)
    matched = _match_trusted_pre(pre["fingerprint"])
    if matched is None:
        raise _Refusal(
            json.dumps(
                {
                    "error": "pre-migration shape does not match any trusted baseline; "
                    "manual reconciliation required before signing",
                    "actual_pre_fingerprint": pre["fingerprint"]["actual_fingerprint"],
                    "actual_pre_counts": pre["fingerprint"]["actual_counts"],
                    "matched_existing_variant": pre["fingerprint"]["matched_variant"],
                    "trusted_pre_variants": sorted(TRUSTED_PRE_VARIANT_NAMES),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    if expect_pre:
        expected_name, expected_counts, expected_fingerprint = _resolve_expect_pre(expect_pre)
        actual = (pre["fingerprint"]["actual_counts"], pre["fingerprint"]["actual_fingerprint"])
        if actual != (expected_counts, expected_fingerprint):
            raise _Refusal(
                json.dumps(
                    {
                        "error": f"pre-migration shape does not match --expect-pre '{expected_name}'",
                        "actual_pre_fingerprint": pre["fingerprint"]["actual_fingerprint"],
                        "actual_pre_counts": pre["fingerprint"]["actual_counts"],
                        "actual_pre_variant": matched,
                        "expected_pre_variant": expected_name,
                        "expected_pre_fingerprint": expected_fingerprint,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
    return pre, matched


def _catalog_row_ref(category: str, row_key: str) -> tuple[str | None, str | None]:
    row = json.loads(row_key)
    if category == "columns":
        return row.get("table_name"), row.get("column_name")
    return row.get("table_name"), row.get("name")


def _normalize_sql_text(value) -> str:
    """SQL 定义文本规范化：折叠连续空白为单空格。

    pg_get_constraintdef / pg_get_indexdef / pg_get_triggerdef /
    pg_get_functiondef 的 pretty 打印在 PG16 次要版本间可能存在换行/缩进
    差异；折叠空白后做逐字比对，表达式文本、谓词、函数体语义全量参与，
    只对排版差异免疫。"""
    return " ".join(str(value or "").split())


def _canonical_column(row: dict, *, include_ordinal: bool) -> dict:
    canonical = {
        "table_name": row.get("table_name"),
        "column_name": row.get("column_name"),
        "data_type": row.get("data_type"),
        "not_null": bool(row.get("not_null")),
        "identity": str(row.get("identity") or ""),
        "generated": str(row.get("generated") or ""),
        "default_expr": (
            None
            if row.get("default_expr") is None
            else _normalize_sql_text(row.get("default_expr"))
        ),
    }
    if include_ordinal:
        canonical["ordinal"] = int(row.get("ordinal"))
    return canonical


def _canonical_constraint(row: dict) -> dict:
    return {
        "table_name": row.get("table_name"),
        "name": row.get("name"),
        "type": row.get("type"),
        "validated": bool(row.get("validated")),
        "definition": _normalize_sql_text(row.get("definition")),
    }


def _canonical_index(row: dict) -> dict:
    return {
        "table_name": row.get("table_name"),
        "name": row.get("name"),
        "is_unique": bool(row.get("is_unique")),
        "is_valid": bool(row.get("is_valid")),
        "is_ready": bool(row.get("is_ready")),
        "key_count": int(row.get("key_count")),
        "definition": _normalize_sql_text(row.get("definition")),
        "predicate": (
            None
            if row.get("predicate") is None
            else _normalize_sql_text(row.get("predicate"))
        ),
        "opclasses": [str(opclass) for opclass in (row.get("opclasses") or [])],
    }


def _diff_expected_objects(
    *,
    category: str,
    scope: str,
    key_field: str,
    expected_defs,
    actual_rows,
    canonicalize,
) -> list[str]:
    """逐值比对内置期望清单与实际 pg_catalog 行：缺失/多余/定义不符全列出。"""
    problems: list[str] = []
    expected_by_key = {str(row[key_field]): row for row in expected_defs}
    actual_by_key = {str(row.get(key_field)): row for row in actual_rows}
    for key in sorted(expected_by_key):
        actual = actual_by_key.get(key)
        if actual is None:
            problems.append(f"missing expected {category}: {scope}.{key}")
            continue
        expected_canonical = canonicalize(expected_by_key[key])
        actual_canonical = canonicalize(actual)
        if expected_canonical != actual_canonical:
            field_diffs = {
                field: {
                    "expected": expected_canonical.get(field),
                    "actual": actual_canonical.get(field),
                }
                for field in expected_canonical
                if expected_canonical.get(field) != actual_canonical.get(field)
            }
            problems.append(
                f"mismatched {category} definition: {scope}.{key}: "
                + json.dumps(field_diffs, ensure_ascii=False, sort_keys=True, default=str)
            )
    for key in sorted(actual_by_key):
        if key not in expected_by_key:
            problems.append(
                f"unexpected {category} on {scope}: "
                + json.dumps(actual_by_key[key], ensure_ascii=False, sort_keys=True, default=str)
            )
    return problems


def _verify_delta(pre: dict, post: dict) -> list[str]:
    """Gate 2: exact pre→post delta must equal the payer-migration whitelist.

    外部独立审核裁决（2026-07-23）后，本闸从"只核对象名"升级为逐值精确
    冻结：错误列类型、CHECK(TRUE)、错误索引、新表额外列、篡改触发器定义、
    篡改函数体、既有对象的任何其他改动，全部拒签。"""
    problems: list[str] = []

    post_columns = [json.loads(row_key) for row_key in post["catalog"]["columns"]]
    post_constraints = [json.loads(row_key) for row_key in post["catalog"]["constraints"]]
    post_indexes = [json.loads(row_key) for row_key in post["catalog"]["indexes"]]

    # 2a. 既有对象零改动纪律：pre→post 的删除/原位修改（体现为 removed）
    #     与非白名单新增一律拒签。新表对象与既有表白名单对象在此不判——
    #     它们的精确定义由 2b/2c 逐值冻结。
    for category, expected_on_existing in (
        ("columns", EXPECTED_NEW_COLUMNS_ON_EXISTING),
        ("constraints", EXPECTED_NEW_CONSTRAINTS_ON_EXISTING),
        ("indexes", EXPECTED_NEW_INDEXES_ON_EXISTING),
    ):
        added = post["catalog"][category] - pre["catalog"][category]
        removed = pre["catalog"][category] - post["catalog"][category]
        for row_key in sorted(removed):
            problems.append(f"removed {category}: {row_key}")
        for row_key in sorted(added):
            table, name = _catalog_row_ref(category, row_key)
            if table in EXPECTED_NEW_TABLES:
                continue
            if (table, name) in expected_on_existing:
                continue
            problems.append(f"unexpected added {category}: {row_key}")

    # 2b. 新表（organization_payer_policies / organization_payer_policy_events）
    #     全对象精确冻结：逐列 name+ordinal+type+null+default、CHECK/FK/PK 比
    #     pg_get_constraintdef 完整定义（表达式文本规范化）、索引比
    #     pg_get_indexdef 全定义。新表额外列 / 错误列类型 / CHECK(TRUE) /
    #     错误索引 / 缺失对象全部在此拒签。
    for table in sorted(EXPECTED_NEW_TABLES):
        problems += _diff_expected_objects(
            category="columns",
            scope=table,
            key_field="column_name",
            expected_defs=[row for row in EXPECTED_NEW_TABLE_COLUMN_DEFS if row["table_name"] == table],
            actual_rows=[row for row in post_columns if row.get("table_name") == table],
            canonicalize=lambda row: _canonical_column(row, include_ordinal=True),
        )
        problems += _diff_expected_objects(
            category="constraints",
            scope=table,
            key_field="name",
            expected_defs=[row for row in EXPECTED_NEW_TABLE_CONSTRAINT_DEFS if row["table_name"] == table],
            actual_rows=[row for row in post_constraints if row.get("table_name") == table],
            canonicalize=_canonical_constraint,
        )
        problems += _diff_expected_objects(
            category="indexes",
            scope=table,
            key_field="name",
            expected_defs=[row for row in EXPECTED_NEW_TABLE_INDEX_DEFS if row["table_name"] == table],
            actual_rows=[row for row in post_indexes if row.get("table_name") == table],
            canonicalize=_canonical_index,
        )

    # 2c. 既有表 organization_charge_links 的 5 新列 / 2 新 CHECK / 1 新索引
    #     同样逐值精确冻结（仅名字落入白名单不够，定义必须与钉版一致）。
    problems += _diff_expected_objects(
        category="columns",
        scope="organization_charge_links",
        key_field="column_name",
        expected_defs=EXPECTED_NEW_COLUMN_DEFS_ON_EXISTING,
        actual_rows=[
            row
            for row in post_columns
            if (row.get("table_name"), row.get("column_name")) in EXPECTED_NEW_COLUMNS_ON_EXISTING
        ],
        canonicalize=lambda row: _canonical_column(row, include_ordinal=False),
    )
    problems += _diff_expected_objects(
        category="constraints",
        scope="organization_charge_links",
        key_field="name",
        expected_defs=EXPECTED_NEW_CONSTRAINT_DEFS_ON_EXISTING,
        actual_rows=[
            row
            for row in post_constraints
            if (row.get("table_name"), row.get("name")) in EXPECTED_NEW_CONSTRAINTS_ON_EXISTING
        ],
        canonicalize=_canonical_constraint,
    )
    problems += _diff_expected_objects(
        category="indexes",
        scope="organization_charge_links",
        key_field="name",
        expected_defs=EXPECTED_NEW_INDEX_DEFS_ON_EXISTING,
        actual_rows=[
            row
            for row in post_indexes
            if (row.get("table_name"), row.get("name")) in EXPECTED_NEW_INDEXES_ON_EXISTING
        ],
        canonicalize=_canonical_index,
    )

    added_tables = post["tables"] - pre["tables"]
    removed_tables = pre["tables"] - post["tables"]
    if added_tables != EXPECTED_NEW_TABLES:
        problems.append(
            "table census delta mismatch: "
            f"added={sorted(added_tables)} removed={sorted(removed_tables)} "
            f"expected_added={sorted(EXPECTED_NEW_TABLES)}"
        )
    # 终态普查必须精确等于已知表集：任何 pre 既有或迁移新增的多余组织表都是
    # 生产漂移（fingerprint 只覆盖已知表，census 兜底未知表），拒绝签名。
    from services.organization_schema_contract import ACTOR_ARTIFACT_TABLES, ORGANIZATION_TABLES

    expected_final_tables = set(ORGANIZATION_TABLES) | set(ACTOR_ARTIFACT_TABLES)
    if post["tables"] != expected_final_tables:
        problems.append(
            "post-migration table census is not exactly the contracted set: "
            f"extra={sorted(post['tables'] - expected_final_tables)} "
            f"missing={sorted(expected_final_tables - post['tables'])}"
        )
    for table in sorted(EXPECTED_NEW_TABLES):
        if table not in post["tables"]:
            problems.append(f"missing expected table: {table}")

    # 触发器：先按 pre→post added/removed 守住既有对象零改动，再对期望
    # 触发器做完整定义比对（pg_get_triggerdef：事件集/时机/WHEN 条件与
    # EXECUTE FUNCTION 目标全量参与；同名不同定义的篡改触发器拒签）。
    added_triggers = post["triggers"] - pre["triggers"]
    removed_triggers = pre["triggers"] - post["triggers"]
    for row_key in sorted(removed_triggers):
        problems.append(f"removed triggers: {row_key}")
    for row_key in sorted(added_triggers):
        row = json.loads(row_key)
        if (row.get("table_name"), row.get("name")) not in EXPECTED_NEW_TRIGGERS:
            problems.append(f"unexpected added triggers: {row_key}")
    post_trigger_rows = [json.loads(row_key) for row_key in post["triggers"]]
    for (table, name), expected_definition in sorted(EXPECTED_NEW_TRIGGER_DEFS.items()):
        actual = next(
            (
                row
                for row in post_trigger_rows
                if row.get("table_name") == table and row.get("name") == name
            ),
            None,
        )
        if actual is None:
            problems.append(f"missing expected trigger: {table}.{name}")
        elif _normalize_sql_text(actual.get("definition")) != _normalize_sql_text(expected_definition):
            problems.append(
                f"mismatched trigger definition: {table}.{name}: "
                + json.dumps(
                    {
                        "expected": _normalize_sql_text(expected_definition),
                        "actual": _normalize_sql_text(actual.get("definition")),
                    },
                    ensure_ascii=False,
                )
            )

    # 函数：added/removed 零改动纪律 + 期望函数完整定义比对
    # （pg_get_functiondef，schema 前缀剥离 + 空白规范化，函数体全量参与）。
    added_functions = post["functions"] - pre["functions"]
    removed_functions = pre["functions"] - post["functions"]
    for row_key in sorted(removed_functions):
        problems.append(f"removed functions: {row_key}")
    for row_key in sorted(added_functions):
        row = json.loads(row_key)
        if row.get("name") not in EXPECTED_NEW_FUNCTIONS:
            problems.append(f"unexpected added functions: {row_key}")
    post_function_rows = [json.loads(row_key) for row_key in post["functions"]]
    for name, expected in sorted(EXPECTED_NEW_FUNCTION_DEFS.items()):
        actual = next((row for row in post_function_rows if row.get("name") == name), None)
        if actual is None:
            problems.append(f"missing expected function: {name}")
            continue
        if str(actual.get("args") or "") != str(expected["args"]):
            problems.append(
                f"mismatched function arguments: {name}: "
                + json.dumps(
                    {"expected": expected["args"], "actual": actual.get("args")},
                    ensure_ascii=False,
                )
            )
        elif _normalize_sql_text(actual.get("definition")) != _normalize_sql_text(expected["definition"]):
            problems.append(
                f"mismatched function definition: {name}: "
                + json.dumps(
                    {
                        "expected": _normalize_sql_text(expected["definition"]),
                        "actual": _normalize_sql_text(actual.get("definition")),
                    },
                    ensure_ascii=False,
                )
            )

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sign the production_reanchor_payer_policies_v1 catalog variant "
        "from a schema-only production clone (dry-run by default; snippet only "
        "after trusted-pre + exact-delta verification)."
    )
    parser.add_argument(
        "--dsn",
        required=True,
        help="target DSN (schema-level clone or loopback test container; "
        "query options such as options=-csearch_path=<schema> are honored)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="apply the additive payer migration (idempotent, run twice) before "
        "fingerprinting; default is dry-run (fingerprint only, zero writes)",
    )
    parser.add_argument(
        "--expect-pre",
        default=None,
        help="pin the expected pre-migration baseline by variant name or fingerprint "
        f"(one of: {', '.join(sorted(TRUSTED_PRE_VARIANT_NAMES))})",
    )
    parser.add_argument(
        "--expect-migration-sha256",
        default=EXPECTED_MIGRATION_SHA256,
        help="pinned sha256 of the payer migration SQL file (default: built-in hash "
        "of the reviewed 2026-07-23 migration); any mismatch refuses to run or sign",
    )
    parser.add_argument(
        "--i-understand-prod-clone",
        action="store_true",
        help="required for every --write target, including loopback/SSH tunnels: "
        "confirms the target is an expendable test database or schema-only "
        "production clone, never the live database",
    )
    args = parser.parse_args()

    _configure_stdout_utf8()

    host = (urlsplit(args.dsn).hostname or "").lower()
    if args.write and not args.i_understand_prod_clone:
        print(
            json.dumps(
                {
                    "error": "--write refused without --i-understand-prod-clone; "
                    "loopback may be an SSH tunnel to production",
                    "host": host,
                },
                ensure_ascii=False,
            )
        )
        return 2
    if host not in LOOPBACK_HOSTS and not args.i_understand_prod_clone:
        print(
            json.dumps(
                {
                    "error": "non-loopback DSN refused without --i-understand-prod-clone",
                    "host": host,
                },
                ensure_ascii=False,
            )
        )
        return 2

    # Gate 0: migration 文件钉版——实际 SHA-256 与已评审哈希不一致时，不执行、
    # 不签署、甚至不做 dry-run 指纹解读（任何输出都可能误导人工核对）。
    try:
        _verify_migration_sha256(args.expect_migration_sha256)
    except _Refusal as refusal:
        print(refusal.payload)
        return 2

    if not args.write:
        # Dry-run: fingerprint only, never a signing snippet for an unverified shape.
        result = _snapshot(args.dsn)["fingerprint"]
        matched = result["matched_variant"]
        trusted_pre = _match_trusted_pre(result)
        print(
            json.dumps(
                {
                    "dsn_host": host,
                    "migration_applied": False,
                    "matched_existing_variant": matched,
                    "trusted_pre_baseline": trusted_pre,
                    "actual_counts": result["actual_counts"],
                    "actual_fingerprint": result["actual_fingerprint"],
                    "note": (
                        f"shape already matches signed variant '{matched}'; nothing to sign"
                        if matched
                        else (
                            f"trusted pre-migration shape '{trusted_pre}'; rerun with "
                            "--write to migrate and sign"
                            if trusted_pre
                            else "untrusted shape — manual reconciliation required; "
                            "no signing snippet emitted"
                        )
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if (matched or trusted_pre) else 2

    # Gate 1: trusted pre-fingerprint (optionally pinned by --expect-pre).
    try:
        pre, pre_variant = _verify_pre(args.dsn, args.expect_pre)
    except _Refusal as refusal:
        print(refusal.payload)
        return 2

    # 2026-07-23 统一 R3 §六：migration 与 delta 验证在同一事务内完成。
    # 验证失败（delta 不符 / 迁移执行异常）整体 rollback——拒签时数据库
    # 零变化，绝不出现"签了半截迁移"的现场。
    conn = psycopg2.connect(args.dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    try:
        with conn.cursor() as cursor:
            _apply_payer_migration_tx(cursor)
            post = _snapshot_cursor(cursor)
            # Gate 2: exact delta equals the payer-migration whitelist.
            problems = _verify_delta(pre, post)
            if problems:
                conn.rollback()
                print(
                    json.dumps(
                        {
                            "error": "post-migration delta does not equal the payer-migration "
                            f"whitelist ({PAYER_MIGRATION_DELTA_VERSION}); refusing to sign; "
                            "migration rolled back, database unchanged",
                            "pre_variant": pre_variant,
                            "delta_problems": problems,
                            "actual_post_fingerprint": post["fingerprint"]["actual_fingerprint"],
                            "actual_post_counts": post["fingerprint"]["actual_counts"],
                            "rolled_back": True,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 2
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - 迁移/快照任何异常都必须整体回滚并拒签
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        print(
            json.dumps(
                {
                    "error": "migration or verification raised; rolled back, database unchanged",
                    "exception_type": type(exc).__name__,
                    "rolled_back": True,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    finally:
        conn.close()

    # Gate 3: all checks passed — emit the post-fingerprint snippet.
    counts = post["fingerprint"]["actual_counts"]
    fingerprint = post["fingerprint"]["actual_fingerprint"]
    matched = post["fingerprint"]["matched_variant"]
    print(
        json.dumps(
            {
                "dsn_host": host,
                "migration_applied": True,
                "pre_variant": pre_variant,
                "delta_version": PAYER_MIGRATION_DELTA_VERSION,
                "delta_verified": True,
                "matched_existing_variant": matched,
                "actual_counts": counts,
                "actual_fingerprint": fingerprint,
                "note": (
                    f"shape already matches signed variant '{matched}'"
                    if matched
                    else "trusted pre + exact whitelist delta verified — after contract "
                    "review, paste the snippet below into "
                    "services/organization_schema_contract.py ACCEPTED_CATALOG_VARIANTS"
                ),
                "deployment_rule": "此签名必须在 Review 复核后提交进新 clean SHA 才允许部署",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print("\n# ================= paste-able snippet =================")
    print(
        f"PRODUCTION_REANCHOR_PAYER_COUNTS = {{\"columns\": {counts['columns']}, "
        f"\"constraints\": {counts['constraints']}, \"indexes\": {counts['indexes']}}}"
    )
    print("PRODUCTION_REANCHOR_PAYER_FINGERPRINT = (")
    print(f"    \"{fingerprint}\"")
    print(")\n")
    print("# into ACCEPTED_CATALOG_VARIANTS:")
    print(f"    \"{VARIANT_NAME}\": (")
    print("        PRODUCTION_REANCHOR_PAYER_COUNTS,")
    print("        PRODUCTION_REANCHOR_PAYER_FINGERPRINT,")
    print("    ),")
    print("\n# 此签名必须在 Review 复核后提交进新 clean SHA 才允许部署")
    return 0


if __name__ == "__main__":
    sys.exit(main())
