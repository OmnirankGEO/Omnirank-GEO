"""GEO 图文合同链的 schema readiness 契约(规格 02 §13 指定的唯一 owning readiness)。

## 为什么单独一份,而不是挂进 FLEET_SCHEMA_GUARDS

规格 02 §13 明令:**本期不得**把它加进无条件 `FLEET_SCHEMA_GUARDS`。理由是安全方向 ——
fleet guard 是「不就绪就拒绝启动整站」;把一个**新 lane** 挂进去,等于让图文这条新链
没就绪时把诊断/报价/监测/发布全都拖死。正确形态是:

  prestart 跑完 migration → 本模块给出该 lane 的 readiness 结论
  → **只有图文新 route** 在入口复核,未就绪返回 503 `SCHEMA_NOT_READY`
  → 且保证零 claim / 零资金 / 零订单 / 零 provider;legacy route 完全不受影响。

## 判据纪律

本模块只回答「034 迁移跑了没有」,不回答「数据对不对」。列出的每一项都必须是
`db/migration_034_geo_image_note_contract_2026_08_17.sql` 真正创建的东西 ——
配套测试 `tests/geo_image_note_2026_08_17/test_schema_contract_pg16.py` 会:
  ① 在**跑过迁移**的库上断言零 blocker(正向);
  ② 在**没跑迁移**的库上断言 blocker 非空(反向对照 —— 否则"零 blocker"可能是恒真);
  ③ 逐项删除一个列/约束后断言对应 blocker 出现(判别力)。
"""
from __future__ import annotations

from typing import Final


# ---------------------------------------------------------------------------
# 期望的列(表 → 列名集合)。只列 034 新增的,不复述既有列 —— 复述既有列会让本契约
# 在别的窗口合法改表时误红,而**长期红的判据等于没有判据**。
# ---------------------------------------------------------------------------
EXPECTED_COLUMNS: Final[dict[str, frozenset[str]]] = {
    "geo_douyin_posts": frozenset({
        "tenant_owner_user_id", "payer_user_id", "actor_user_id", "payer_policy_snapshot",
        "quote_id", "contract_revision_id", "delivery_slot_key", "counts_toward_contract",
        "source_mode", "production_batch_id", "batch_item_request_id", "batch_item_ordinal",
        "distill_task_id", "topic_ref", "topic_snapshot", "active_revision_id",
        "active_generation_task_id", "generation_epoch",
    }),
    # 🔴 真名是 post_tasks。规格 02 §3.5 写的 `geo_douyin_tasks` 在生产**不存在**
    #    (2026-08-17 只读枚举 pg_tables 实证);按现尖真名接线,不按文档名。
    "geo_douyin_post_tasks": frozenset({
        "request_id", "idempotency_key", "endpoint", "production_batch_id",
        "batch_item_request_id", "batch_item_ordinal", "request_hash", "base_revision_id",
        "generation_epoch", "request_snapshot", "lease_owner", "lease_expires_at",
        # external_started_at 是崩溃恢复的判别列(见 034 里那段注释):
        # 缺它 → reconciler 分不清"可安全重跑"与"已调过供应商",必然二选一地错。
        "heartbeat_at", "external_started_at", "superseded_at", "result_hash", "settlement_status",
        "settlement_authority", "organization_charge_link_id", "execution_id",
        "freeze_table", "payer_user_id", "reserved_amount", "physical_split_snapshot",
    }),
    "geo_douyin_distill_tasks": frozenset({
        "tenant_owner_user_id", "quote_id", "contract_revision_id", "request_id",
        "request_hash", "endpoint", "requested_slot_keys", "result_snapshot_hash",
        "topic_ref_version", "retry_of_task_id", "attempt_no", "lease_owner",
        "lease_expires_at", "heartbeat_at",
    }),
    "publish_idempotency_keys": frozenset({
        "record_kind", "command_id", "root_request_id", "coordination_state", "root_version",
        "request_hash", "tenant_owner_user_id", "principal_user_id", "payer_user_id",
        "actor_user_id", "actor_kind", "organization_id", "membership_id",
        "membership_version", "payer_policy_snapshot", "approval_request_id",
        "approval_policy_version", "approval_payload_hash", "updated_at", "root_kind",
    }),
    "mhz_short_video_drafts": frozenset({
        "command_request_id", "source_kind", "source_id", "source_payload_hash",
        "source_receipt_id", "source_post_revision_id", "prepared_artifact_id",
        "manifest_hash", "provider_payload_snapshot",
    }),
    "mhz_publish_orders": frozenset({
        "command_request_id", "payer_user_id", "actor_user_id", "payer_policy_snapshot",
    }),
    "mhz_publish_order_items": frozenset({
        "command_request_id", "source_geo_post_id", "source_post_revision_id",
        "prepared_artifact_id", "manifest_hash", "item_request_id", "attempt_root_id",
        "attempt_no", "retry_of_item_id", "retry_claim_state", "retry_claim_token",
        "retry_claim_version", "status_version", "terminal_at", "last_authoritative_event_at",
        "price_snapshot", "publish_price_fingerprint", "feature_code", "billing_mode",
        "settlement_authority", "settlement_status", "organization_charge_link_id",
        "execution_id", "freeze_id", "freeze_table", "payer_user_id", "reserved_amount",
        "physical_split_snapshot", "external_started_at", "lease_owner", "lease_expires_at",
        "capacity_date", "capacity_timezone", "capacity_valid_until", "capacity_state",
        "availability", "retracted_at", "retraction_kind", "replaced_by_source",
        "replaced_by_source_id", "published_at_tz",
    }),
    "media_publications": frozenset({
        "tenant_owner_user_id", "source_geo_post_id", "source_post_revision_id",
        "delivery_slot_key", "normalized_url", "normalized_url_hash",
        "url_normalization_version", "published_at_tz", "retracted_at", "retraction_kind",
        "replaced_by_source", "replaced_by_source_id", "evidence_receipt_id",
        "evidence_hash", "body_proof", "time_precision", "request_id", "request_hash",
    }),
}

EXPECTED_TABLES: Final[frozenset[str]] = frozenset({
    "geo_douyin_post_revisions",
    "geo_douyin_production_batches",
    "geo_douyin_publish_artifacts",
})

# 资金与对象身份的**形态**约束。这些不是装饰:删掉任何一条,
# 「一篇一账号」「资金终态只走一种」「权威句柄互斥」就退回到靠调用方自觉。
EXPECTED_CONSTRAINTS: Final[frozenset[str]] = frozenset({
    "ck_geo_douyin_posts_source_mode",
    "ck_geo_douyin_task_settlement_status",
    "ck_geo_douyin_task_settlement_authority",
    "ck_geo_douyin_task_authority_shape",
    "ck_publish_idem_record_kind",
    "ck_publish_idem_coordination_state",
    "ck_publish_idem_kind_shape",
    "fk_publish_idem_retry_root",
    "ck_mhz_draft_source_kind",
    "ck_mhz_item_billing_mode",
    "ck_mhz_item_settlement_authority",
    "ck_mhz_item_settlement_status",
    "ck_mhz_item_capacity_state",
    "ck_mhz_item_availability",
    "ck_mhz_item_authority_shape",
    "ck_mhz_item_freeze_mode_pairing",
    "ck_media_pub_time_precision",
    "ck_geo_douyin_post_rev_status",
    "ck_geo_douyin_post_rev_operation",
    "ck_geo_douyin_batch_status",
    "ck_geo_douyin_artifact_state",
})

EXPECTED_INDEXES: Final[frozenset[str]] = frozenset({
    "uq_geo_douyin_posts_active_slot",
    "uq_geo_douyin_posts_batch_item",
    "uq_geo_douyin_post_rev_no",
    "uq_geo_douyin_post_rev_active",
    "uq_geo_douyin_batch_request",
    "uq_geo_douyin_artifact_request",
    "uq_geo_douyin_task_idem",
    "uq_geo_douyin_task_active_generation",
    "uq_geo_douyin_distill_request",
    "uq_geo_douyin_distill_contract_inflight",
    "uq_publish_idem_command_id",
    "uq_publish_idem_request_kind",
    "uq_mhz_item_attempt",
    "uq_mhz_item_live_revision_root",
    "uq_mhz_item_command_item",
    "uq_media_pub_active_url",
    "uq_media_pub_request",
})

SCHEMA_CONTRACT_VERSION: Final = "geo-image-note-schema-v1"

#: 少数**定义本身就是不变式**的对象:名字对了不算数,谓词/取值域也要对。
#:
#: 🔴 [返工 2026-08-18 · Codex P1-8] 原来 readiness 只比名字。于是
#:    「索引还在但谓词被改窄了」「CHECK 还在但允许值被加了一个」这两类
#:    **最危险的漂移**在 readiness 眼里完全就绪 —— 它们恰恰是不改名字的。
#:    本仓已有同族教训:「往 CHECK 加允许值 ≠ additive」。
#:
#: 只列真正承重的三个,不是全表铺开:铺开会让 readiness 变成 schema 快照比对,
#: 任何无关的格式化差异都能把整站拒启(fail-closed 的代价必须与收益相称)。
DEFINITION_FRAGMENTS: Final[dict[str, tuple[str, ...]]] = {
    # 一篇一账号:必须是「非终态 **或** 仍有效」(P0-11 修的那个 OR)
    "uq_mhz_item_live_revision_root": ("terminal_at IS NULL", "OR", "availability"),
    # 幂等记录形态:三种 record_kind 的配对不能被放松
    "ck_publish_idem_kind_shape": ("legacy", "root", "retry"),
    # 资金句柄形态:direct/admin_exempt 必须整组齐备
    "ck_geo_douyin_task_authority_shape": ("organization_charge", "direct_freeze",
                                           "admin_exempt", "reserved_amount"),
}


def schema_blockers(cur) -> list[str]:
    """返回阻塞项列表;空列表 = 该 lane 就绪。不抛异常,便于 readiness 报告聚合。"""
    blockers: list[str] = []

    for table in sorted(EXPECTED_TABLES):
        cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (f"public.{table}",))
        row = cur.fetchone()
        if not _first(row):
            blockers.append(f"missing_table:{table}")

    for table in sorted(EXPECTED_COLUMNS):
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name=%s",
            (table,),
        )
        actual = {_first(r) for r in cur.fetchall()}
        if not actual:
            blockers.append(f"missing_table:{table}")
            continue
        for column in sorted(EXPECTED_COLUMNS[table] - actual):
            blockers.append(f"missing_column:{table}.{column}")

    cur.execute(
        "SELECT conname FROM pg_constraint "
        "WHERE connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())"
    )
    actual_constraints = {_first(r) for r in cur.fetchall()}
    for name in sorted(EXPECTED_CONSTRAINTS - actual_constraints):
        blockers.append(f"missing_constraint:{name}")

    cur.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname=current_schema()"
    )
    actual_indexes = {_first(r) for r in cur.fetchall()}
    for name in sorted(EXPECTED_INDEXES - actual_indexes):
        blockers.append(f"missing_index:{name}")

    # ── 定义级核对(P1-8)。名字在 ≠ 语义对。 ──────────────────────────
    for name in sorted(DEFINITION_FRAGMENTS):
        cur.execute(
            "SELECT COALESCE("
            "  (SELECT pg_get_indexdef(c.oid) FROM pg_class c"
            "    WHERE c.relname = %(name)s AND c.relkind = 'i'),"
            "  (SELECT pg_get_constraintdef(k.oid) FROM pg_constraint k"
            "    WHERE k.conname = %(name)s)"
            ") AS definition", {"name": name})
        definition = _first(cur.fetchone()) or ""
        if not definition:
            # 名字那一层已经报过 missing_*;这里不重复报,避免同一件事两条阻塞。
            continue
        missing = [f for f in DEFINITION_FRAGMENTS[name] if f not in definition]
        if missing:
            blockers.append(f"definition_drift:{name}:{'+'.join(missing)}")

    return blockers


def _first(row):
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]


def assert_schema_ready(cur) -> None:
    """未就绪时抛出。图文新 route 入口捕获它并返回 503 `SCHEMA_NOT_READY`。

    🔴 调用方**必须**在任何 claim / 资金 / 订单 / provider 之前调用它,
       否则「未就绪时零副作用」这句承诺就只是文档。
    """
    blockers = schema_blockers(cur)
    if blockers:
        raise RuntimeError("GEO_IMAGE_NOTE_SCHEMA_NOT_READY:" + "|".join(blockers))
