"""Canonical PostgreSQL 16 catalog contract for organization internal seats.

The fingerprint covers every column of the frozen organization tables, every
organization-owned constraint and index, and the complete isolation column /
constraint / index surface on the 11 legacy actor-artifact tables.  Catalog
rows include exact type modifiers, nullability, defaults, generated/identity
flags, CHECK/UNIQUE/FK definitions and validation state, plus index column
order, expressions, predicates, uniqueness, readiness and opclasses.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any


ORGANIZATION_TABLES = (
    "organizations",
    "organization_schema_migrations",
    "organization_seat_entitlements",
    "organization_roles",
    "organization_memberships",
    "organization_invites",
    "organization_invite_accept_receipts",
    "organization_security_rate_events",
    "organization_role_capabilities",
    "organization_member_capability_overrides",
    "organization_brand_assignments",
    "organization_artifact_shares",
    "organization_spend_limits",
    "organization_automatic_plans",
    "organization_plan_occurrences",
    "organization_charge_links",
    "organization_charge_limit_links",
    "organization_approval_policies",
    "organization_approval_requests",
    "organization_work_outbox",
    "organization_audit_events",
    "organization_product_config_publications",
    "organization_member_legacy_access_snapshots",
    "organization_operator_accounts",
    "organization_invite_verification_challenges",
    "organization_invite_delivery_outbox",
    "organization_payer_policies",
    "organization_payer_policy_events",
)

ACTOR_ARTIFACT_TABLES = (
    "client_profiles",
    "client_materials",
    "diagnosis_records",
    "quotes",
    "keyword_selection_sessions",
    "article_generations",
    "articles",
    "media_publications",
    "monitoring_tasks",
    "monitoring_reports",
    "publish_orders",
)

ACTOR_ARTIFACT_COLUMNS = (
    "brand_id",
    "organization_id",
    "created_by_user_id",
    "created_by_membership_id",
    "created_by_actor_kind",
    "responsible_user_id",
    "artifact_visibility",
    "actor_user_id",
    "actor_membership_id",
    "actor_kind",
    "payer_user_id",
    "approval_request_id",
)

# Generated from a fresh PostgreSQL 16 catalog after the frozen migration. Any
# DDL change requires an explicit contract review and fingerprint rotation.
# Rotated 2026-07-23 for the additive organization payer-policy package
# (organization_payer_policies + organization_payer_policy_events + five frozen
# payer-evidence columns on organization_charge_links). Pre-rotation shapes stay
# accepted below so pre-migration production databases keep an exact variant.
#
# 【2026-07-28 轮换 · 自愈式 DDL 治理收口】团队短代码的三条 DDL 已从业务代码里的
# ensure_schema 补登记为正式迁移(scripts/migration_organization_short_code_2026_07_28.sql
# + db/migration_manifest.py 尾条)。登记之后,任何跑完迁移链的新库都会带上
# organizations.short_code / short_code_locked 两列 + organizations_short_code_unique
# 部分唯一索引,所以"fresh 参考形态"必须随之前移:534/384/104 → 536/384/105。
# 轮换前的 534 形态保留为 fresh_pg16_payer_policies_v1(仍有 payer 专项 PG 夹具
# 按子集迁移链构建它),本常量指向轮换后的当前 fresh 形态。
#
# 指纹取得方式(实算,非手推):PG16 throwaway 容器上按 tests/organization_internal_seats/
# verify_local.py main() 的 fresh 链(base_fixture + internal_seats + all_accounts_onboarding
# + admin_cross_tenant + payer_policies,各自连跑两遍)再叠加本次短代码迁移,
# 用 catalog_fingerprint() 实算。同一次测量先复现了轮换前的 aa2b56ed…(534/384/104)
# 与 fresh+username 的 477c0b81…(534/384/104),两个已签常量逐字命中 → 证明测量口径无损。
EXPECTED_COUNTS = {"columns": 536, "constraints": 384, "indexes": 105}
EXPECTED_FINGERPRINT = "c0cc2f0fc629cbcd0ffc893363781aad5631760c5c9a23313b6091be1b74d3ec"

# 轮换前的 fresh 形态(payer 专项 PG 夹具 tests/organization_internal_seats/
# test_payer_policy_pg.py / test_sign_payer_catalog_tool_pg.py 仍按子集链构建它,
# 那两套夹具刻意只跑到 payer 迁移为止,不代表"新库装完的样子")。
FRESH_PAYER_POLICIES_COUNTS = {"columns": 534, "constraints": 384, "indexes": 104}
FRESH_PAYER_POLICIES_FINGERPRINT = (
    "aa2b56ed6680e3b60a6dd08884bb27602f8a5f7daf47e57ccb194c52d04f0b85"
)

# Pre-payer-policy fresh shape (organization_internal_seats_all_accounts v3).
LEGACY_ALL_ACCOUNTS_COUNTS = {"columns": 505, "constraints": 367, "indexes": 99}
LEGACY_ALL_ACCOUNTS_FINGERPRINT = "99137a7dc54a9e6c21289c20a96a785a701cbbce53ca6ab03e2bb9638d173346"

# The verifier's production-upgrade fixture intentionally exercises a second
# exact PostgreSQL 16 shape. It is not the deployed production signature.
PRODUCTION_FIXTURE_REANCHOR_COUNTS = {
    "columns": 506,
    "constraints": 367,
    "indexes": 99,
}
PRODUCTION_FIXTURE_REANCHOR_FINGERPRINT = (
    "90d8b2c39f0c2cb244226df7586013b3c68bcd84d9636fa78991507179b7f473"
)

# Signed from a schema-only clone of the active da0f202e production backup
# after two idempotent prestart passes. Production has a NOT NULL
# keyword_selection_sessions.brand_id and a nullable publish_orders.brand_id;
# its legacy actor columns also retain their historical ordinals. All accepted
# variants remain byte-exact and fail closed on any column, constraint, index,
# predicate, validation-state, or ordinal drift.
PRODUCTION_REANCHOR_COUNTS = {"columns": 506, "constraints": 367, "indexes": 99}
PRODUCTION_REANCHOR_FINGERPRINT = (
    "38065a9616696c49b58d6fbc6bf0667d63ec7346933cff1e4cf83c3a347a9ed2"
)

# Signed 2026-07-24 from a schema-only clone of the exact production reanchor
# above after the pinned payer-policy migration passed its exact-delta signer.
PRODUCTION_REANCHOR_PAYER_COUNTS = {
    "columns": 535,
    "constraints": 384,
    "indexes": 104,
}
PRODUCTION_REANCHOR_PAYER_FINGERPRINT = (
    "0bb00d5322bb842dbc874bcb8897b46206c97af025a98222e1bea993edbd5ffb"
)

# The verifier's production-upgrade fixture after the additive payer-policy
# migration. Signed from the same NOT NULL publish_orders.brand_id shape.
PRODUCTION_FIXTURE_PAYER_COUNTS = {
    "columns": 535,
    "constraints": 384,
    "indexes": 104,
}
PRODUCTION_FIXTURE_PAYER_FINGERPRINT = (
    "57ae173c410547edb56e163950e07bca23d8f21d842179f90232ab99269a38ba"
)

# [WP6] fresh_pg16_payer_policies_v1 之上叠加"用户名式邀请"迁移
# (migration_organization_username_invite_2026_07_25.sql):organization_invites /
# organization_operator_accounts 的 target_kind CHECK 由 (phone,email) 放宽到
# (phone,email,username)。约束/列/索引计数不变(534/384/104),仅 CHECK 定义变 →
# 目录 fingerprint 变。
# ⚠️ 这是本地 fresh 形态;生产 reanchor 形态(535 列)需 Deploy-CTO 另行签名。
# 🔴 counts 必须写死,不能再写 `= EXPECTED_COUNTS`:2026-07-28 短代码迁移落地后
#    EXPECTED_COUNTS 已轮换为 536/384/105,若继续别名引用,本变体会被静默改成
#    "536 counts + 534 时期的指纹"——一个永远匹配不上的死变体,且没人会发现。
FRESH_PAYER_USERNAME_COUNTS = {"columns": 534, "constraints": 384, "indexes": 104}
FRESH_PAYER_USERNAME_FINGERPRINT = (
    "477c0b81f9aeac26064a64e5038942d924a46e8018501074528ce68318de62fb"
)

# [WP9/WP6] **生产 reanchor 形态**叠加同一「用户名式邀请」迁移后的签名(2026-07-25)。
# 签署方式(受信三闸,全部通过才落此常量):
#   闸1 pre-fingerprint —— 用只读通道(omnirank-ro · SELECT-only)按 catalog_snapshot 的
#        同款查询导出生产 catalog,本地重算指纹 == 已签 production_reanchor_payer_policies_v1
#        (0bb00d53…),证明导出无损且生产当前就在该已签形态上;
#   闸2 精确 delta —— 仅 organization_invites / organization_operator_accounts 两条
#        target_kind CHECK 定义由 (phone,email) 放宽为 (phone,email,username),其余对象
#        逐字节零改动(列/索引/其它约束全等);
#   闸3 counts 不变 —— 放宽是改定义而非增删对象,仍为 535/384/104。
# 放宽后的 pretty 定义取自本地 PG16 对**同型(text 列)**表跑同一迁移 SQL 的实测渲染,
# 且其迁移前渲染与生产逐字相同,故渲染口径一致。
# 生产列类型为 text(非 varchar),渲染为 ARRAY['phone'::text, 'email'::text, 'username'::text]。
PRODUCTION_REANCHOR_PAYER_USERNAME_COUNTS = PRODUCTION_REANCHOR_PAYER_COUNTS
PRODUCTION_REANCHOR_PAYER_USERNAME_FINGERPRINT = (
    "f048e2cbb35f3c08b41289abb220c29c6d783d588a6796900bcd0416fb08cfc2"
)
# [P0 应急签名 2026-07-28 Deploy-CTO] **生产 reanchor + 团队短代码**形态。
#
# 事故经过:`services/organization_short_code.py::ensure_schema` 是**自愈式建列**
# (docstring 原话"老库无需单独迁移脚本"),属懒初始化 —— 代码随 1d5da671 早已上线,
# 但只有真正走到团队短代码那条路径时才执行 DDL。2026-07-28 16:43~17:22 之间首次被
# 触发,当场给 `organizations` 加了 2 列 + 1 部分唯一索引。因为它绕开了迁移清单,
# 也就绕开了本契约的指纹,导致:
#   ① `server.py:80 _organization_schema_readiness_gate` fail-closed →
#      **新容器一律启动失败**(omnirank-blue 崩在启动,热回滚位归零);
#   ② `verify_unified_release_readiness` fail-closed → **一切部署被拦**。
# 当时 green 仍在服务,纯粹因为它在漂移之前就已启动 —— 站点处于"一次进程死亡
# 即不可恢复"的单点状态。故本常量按下述三闸紧急签署。
#
# 为什么是"签新变体"而不是"把列摘掉恢复旧形态":
#   已有 1 个团队分配到了 short_code,而短代码是**席位登录名的一部分**
#   (见 organization_short_code 模块 docstring),摘列会直接弄坏该团队的席位登录 —— 有损。
#
# 签署三闸(全部实测通过,证据见 2026-07-28 处置记录):
#   闸1 pre-fingerprint —— 生产按 catalog_snapshot 同款查询实算,得
#        bacbb607…(counts 537/384/105),与 blue 启动失败日志里 readiness gate
#        自行算出的 actual_fingerprint 逐字相同(两条独立路径互证)。
#   闸2 精确 delta —— 在**事务内**对生产执行「DROP 这 3 个对象」后重算,指纹精确
#        回落到已签 production_reanchor_payer_username_v1(f048e2cb…/535,384,104),
#        随即 ROLLBACK。这证明本形态与上一已签形态之间**有且仅有**这 3 个对象之差,
#        其余列/索引/约束逐对象零改动。
#   闸3 counts —— 535+2=537 列、104+1=105 索引、约束 384 不变,与实测一致。
#
# 新增对象(逐字取自实测渲染):
#   organizations.short_code         text     NULL 可空,无默认
#   organizations.short_code_locked  boolean  NOT NULL DEFAULT false
#   organizations_short_code_unique  CREATE UNIQUE INDEX ... ON organizations
#                                    USING btree (short_code) WHERE (short_code IS NOT NULL)
#
# ⚠️ 本签名只解生产阻塞,**不构成对"自愈式 DDL"这种做法的追认**。
# 遗留待办已于 2026-07-28 由自愈式 DDL 治理包收口:
#   (a) ✅ 三条 DDL 已落成 scripts/migration_organization_short_code_2026_07_28.sql
#       并登记 db/migration_manifest.py 尾条;ensure_schema 保留(幂等自愈仍是对的),
#       但 docstring 里误导性的"老库无需单独迁移脚本"已删除。
#   (b) ✅ fresh 形态已轮换 534/384/104 → 536/384/105(见上方 EXPECTED_* 注释)。
#   (c) ✅ 全仓自愈 DDL 排查表见
#       docs/AI-CONTEXT/SELF_HEALING_DDL_AUDIT_2026-07-28.md。
#
# 🔴 本常量本身**不要动**:它是按三闸签好的当前生产形态,改它等于把已解开的启动门
#    重新锁上。补登记的迁移对生产是 no-op —— 已实测证明:在 ensure_schema 建出的形态上
#    再跑该迁移,catalog 指纹逐字不变(见治理包交付记录的等价性证明)。
PRODUCTION_REANCHOR_SHORT_CODE_COUNTS = {
    "columns": 537,
    "constraints": 384,
    "indexes": 105,
}
PRODUCTION_REANCHOR_SHORT_CODE_FINGERPRINT = (
    "bacbb6077597fc440a71420730d6200a6036413008033fe1dfb257ea4c28ff5f"
)

# [2026-07-28 收口] fresh + 用户名式邀请 + 团队短代码。与 EXPECTED_* 同为 536/384/105,
# 差别只在 organization_invites / organization_operator_accounts 两条 target_kind CHECK
# 是否已放宽到含 'username' → 指纹不同。两者都是"跑完迁移链的新库"的合法形态,
# 取决于该库有没有跑到用户名邀请那条迁移。同批实算,与 EXPECTED_* 一次测量取得。
FRESH_PAYER_USERNAME_SHORT_CODE_COUNTS = {"columns": 536, "constraints": 384, "indexes": 105}
FRESH_PAYER_USERNAME_SHORT_CODE_FINGERPRINT = (
    "1f6570aa656aec036e4f914c04c91e8b17103da4ef4474475b4351b2038a6daa"
)

ACCEPTED_CATALOG_VARIANTS = {
    "fresh_pg16_payer_policies_short_code_v1": (EXPECTED_COUNTS, EXPECTED_FINGERPRINT),
    "fresh_pg16_payer_policies_username_short_code_v1": (
        FRESH_PAYER_USERNAME_SHORT_CODE_COUNTS,
        FRESH_PAYER_USERNAME_SHORT_CODE_FINGERPRINT,
    ),
    "fresh_pg16_payer_policies_v1": (
        FRESH_PAYER_POLICIES_COUNTS,
        FRESH_PAYER_POLICIES_FINGERPRINT,
    ),
    "production_reanchor_short_code_v1": (
        PRODUCTION_REANCHOR_SHORT_CODE_COUNTS,
        PRODUCTION_REANCHOR_SHORT_CODE_FINGERPRINT,
    ),
    "fresh_pg16_payer_policies_username_v1": (
        FRESH_PAYER_USERNAME_COUNTS,
        FRESH_PAYER_USERNAME_FINGERPRINT,
    ),
    "production_reanchor_payer_username_v1": (
        PRODUCTION_REANCHOR_PAYER_USERNAME_COUNTS,
        PRODUCTION_REANCHOR_PAYER_USERNAME_FINGERPRINT,
    ),
    "production_fixture_reanchor_payer_policies_v1": (
        PRODUCTION_FIXTURE_PAYER_COUNTS,
        PRODUCTION_FIXTURE_PAYER_FINGERPRINT,
    ),
    "production_reanchor_payer_policies_v1": (
        PRODUCTION_REANCHOR_PAYER_COUNTS,
        PRODUCTION_REANCHOR_PAYER_FINGERPRINT,
    ),
    "fresh_pg16_all_accounts_v3": (
        LEGACY_ALL_ACCOUNTS_COUNTS,
        LEGACY_ALL_ACCOUNTS_FINGERPRINT,
    ),
    "production_fixture_reanchor_all_accounts_v3": (
        PRODUCTION_FIXTURE_REANCHOR_COUNTS,
        PRODUCTION_FIXTURE_REANCHOR_FINGERPRINT,
    ),
    "production_reanchor_all_accounts_v3": (
        PRODUCTION_REANCHOR_COUNTS,
        PRODUCTION_REANCHOR_FINGERPRINT,
    ),
}


def match_catalog_variant(actual_counts: dict[str, int], fingerprint: str) -> str | None:
    """Return the exact signed catalog variant, or fail closed with ``None``."""
    actual = (actual_counts, fingerprint)
    return next(
        (
            name
            for name, expected in ACCEPTED_CATALOG_VARIANTS.items()
            if actual == expected
        ),
        None,
    )


def _rows(cursor, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    cursor.execute(sql, params)
    return [dict(row) for row in cursor.fetchall()]


def catalog_snapshot(cursor) -> dict[str, list[dict[str, Any]]]:
    """Return the canonical, sorted, schema-name-independent catalog."""
    columns = _rows(
        cursor,
        """
        SELECT cl.relname AS table_name,a.attname AS column_name,a.attnum AS ordinal,
               format_type(a.atttypid,a.atttypmod) AS data_type,a.attnotnull AS not_null,
               a.attidentity AS identity,a.attgenerated AS generated,
               pg_get_expr(ad.adbin,ad.adrelid,TRUE) AS default_expr
        FROM pg_attribute a
        JOIN pg_class cl ON cl.oid=a.attrelid
        JOIN pg_namespace ns ON ns.oid=cl.relnamespace
        LEFT JOIN pg_attrdef ad ON ad.adrelid=a.attrelid AND ad.adnum=a.attnum
        WHERE ns.nspname=current_schema() AND a.attnum>0 AND NOT a.attisdropped
          AND (cl.relname=ANY(%s) OR
               (cl.relname=ANY(%s) AND a.attname=ANY(%s)))
        ORDER BY cl.relname,a.attnum
        """,
        (list(ORGANIZATION_TABLES), list(ACTOR_ARTIFACT_TABLES), list(ACTOR_ARTIFACT_COLUMNS)),
    )
    constraints = _rows(
        cursor,
        """
        SELECT cl.relname AS table_name,co.conname AS name,co.contype AS type,
               co.convalidated AS validated,pg_get_constraintdef(co.oid,TRUE) AS definition
        FROM pg_constraint co
        JOIN pg_class cl ON cl.oid=co.conrelid
        JOIN pg_namespace ns ON ns.oid=cl.relnamespace
        WHERE ns.nspname=current_schema()
          AND (cl.relname=ANY(%s) OR
               (cl.relname=ANY(%s) AND
                (co.conname LIKE 'org_%%' OR co.conname='organization_publish_actor_shape')))
        ORDER BY cl.relname,co.conname
        """,
        (list(ORGANIZATION_TABLES), list(ACTOR_ARTIFACT_TABLES)),
    )
    indexes = _rows(
        cursor,
        """
        SELECT tc.relname AS table_name,ic.relname AS name,ix.indisunique AS is_unique,
               ix.indisvalid AS is_valid,ix.indisready AS is_ready,
               ix.indnkeyatts AS key_count,
               replace(pg_get_indexdef(ix.indexrelid),format('%%I.',current_schema()),'') AS definition,
               pg_get_expr(ix.indpred,ix.indrelid,TRUE) AS predicate,
               ARRAY(
                 SELECT opc.opcname
                 FROM unnest(ix.indclass::oid[]) WITH ORDINALITY v(opcoid,ord)
                 JOIN pg_opclass opc ON opc.oid=v.opcoid ORDER BY v.ord
               ) AS opclasses
        FROM pg_index ix
        JOIN pg_class ic ON ic.oid=ix.indexrelid
        JOIN pg_class tc ON tc.oid=ix.indrelid
        JOIN pg_namespace ns ON ns.oid=tc.relnamespace
        WHERE ns.nspname=current_schema()
          AND (tc.relname=ANY(%s) OR
               (tc.relname=ANY(%s) AND ic.relname LIKE 'idx_org_%%'))
        ORDER BY tc.relname,ic.relname
        """,
        (list(ORGANIZATION_TABLES), list(ACTOR_ARTIFACT_TABLES)),
    )
    return {"columns": columns, "constraints": constraints, "indexes": indexes}


def catalog_fingerprint(cursor) -> dict[str, Any]:
    snapshot = catalog_snapshot(cursor)
    raw = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    actual_counts = {key: len(value) for key, value in snapshot.items()}
    actual = sha256(raw).hexdigest()
    matched_variant = match_catalog_variant(actual_counts, actual)
    return {
        "matches": matched_variant is not None,
        "matched_variant": matched_variant,
        "actual_fingerprint": actual,
        "expected_fingerprint": EXPECTED_FINGERPRINT,
        "actual_counts": actual_counts,
        "expected_counts": dict(EXPECTED_COUNTS),
    }
