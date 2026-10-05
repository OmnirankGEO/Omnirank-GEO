"""Exact PostgreSQL readiness contract for the article closed-loop sidecar."""
from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any, Final

from services.geo_article_v14_schema_contract import (
    ColumnContract,
    ConstraintContract,
    IndexContract,
)


def _c(sql_type: str, not_null: bool = False, default: str | None = None) -> ColumnContract:
    return ColumnContract(sql_type, not_null, default)


COLUMNS: Final[dict[str, dict[str, ColumnContract]]] = {
    "quotes": {
        "article_plan_writing_mode": _c("character varying(32)"),
        "article_plan_enrolled_at": _c("timestamp with time zone"),
        "article_plan_contract_version": _c("character varying(80)"),
        "article_plan_enrolled_by": _c("integer"),
        "article_plan_enrollment_run_id": _c("bigint"),
    },
    "topics": {
        "delivery_slot_key": _c("uuid"),
        "plan_run_id": _c("bigint"),
        "target_question_snapshot_id": _c("bigint"),
        "article_plan_metadata_version": _c("character varying(80)"),
    },
    "articles": {
        "delivery_slot_key": _c("uuid"),
        "article_revision_key": _c("character(64)"),
        "target_question_snapshot_id": _c("bigint"),
    },
    "geo_article_plan_outbox": {
        "id": _c("bigint", True, "nextval"),
        "event_key": _c("character(64)", True),
        "event_kind": _c("character varying(64)", True),
        "source_kind": _c("character varying(80)", True),
        "source_id": _c("text", True),
        "source_version": _c("character varying(160)", True),
        "source_snapshot_hash": _c("character(64)", True),
        "owner_user_id": _c("integer", True),
        "brand_id": _c("integer", True),
        "quote_id": _c("integer", True),
        "authority_snapshot": _c("jsonb", True),
        "occurred_at": _c("timestamp with time zone", True),
        "observed_at": _c("timestamp with time zone", True, "now()"),
        "status": _c("character varying(32)", True, "pending"),
        "attempt_count": _c("integer", True, "0"),
        "available_at": _c("timestamp with time zone", True, "now()"),
        "claimed_at": _c("timestamp with time zone"),
        "claim_token": _c("character varying(80)"),
        "last_error": _c("text"),
        "completed_at": _c("timestamp with time zone"),
        "manual_resolution_status": _c("character varying(32)"),
        "manual_resolution_reason": _c("text"),
        "manual_resolved_by": _c("integer"),
        "manual_resolved_at": _c("timestamp with time zone"),
        "created_at": _c("timestamp with time zone", True, "now()"),
        "updated_at": _c("timestamp with time zone", True, "now()"),
    },
    "geo_article_contract_revisions": {
        "id": _c("bigint", True, "nextval"),
        "revision_key": _c("character(64)", True),
        "source_event_key": _c("character(64)", True),
        "source_version": _c("character varying(160)", True),
        "owner_user_id": _c("integer", True),
        "brand_id": _c("integer", True),
        "quote_id": _c("integer", True),
        "authority_snapshot": _c("jsonb", True),
        "authority_snapshot_hash": _c("character(64)", True),
        "delivery_count": _c("integer", True),
        "contract_version": _c("character varying(80)", True),
        "created_at": _c("timestamp with time zone", True, "now()"),
    },
    "geo_article_plan_runs": {
        "id": _c("bigint", True, "nextval"),
        "run_key": _c("character(64)", True),
        "contract_revision_id": _c("bigint", True),
        "source_outbox_id": _c("bigint"),
        "owner_user_id": _c("integer", True),
        "brand_id": _c("integer", True),
        "quote_id": _c("integer", True),
        "run_mode": _c("character varying(24)", True),
        "compiler_version": _c("character varying(80)", True),
        "input_snapshot": _c("jsonb", True),
        "input_snapshot_hash": _c("character(64)", True),
        "output_snapshot": _c("jsonb"),
        "output_snapshot_hash": _c("character(64)"),
        "comparison_snapshot": _c("jsonb"),
        "status": _c("character varying(24)", True, "pending"),
        "verdict": _c("character varying(32)", True, "NOT_EVALUATED"),
        "failure_reason": _c("text"),
        "created_at": _c("timestamp with time zone", True, "now()"),
        "finished_at": _c("timestamp with time zone"),
    },
    "geo_article_delivery_slot_events": {
        "id": _c("bigint", True, "nextval"),
        "event_key": _c("character(64)", True),
        "delivery_slot_key": _c("uuid", True),
        "slot_version": _c("integer", True),
        "event_kind": _c("character varying(32)", True),
        "target_state": _c("character varying(24)", True),
        "contract_revision_id": _c("bigint", True),
        "plan_run_id": _c("bigint", True),
        "owner_user_id": _c("integer", True),
        "brand_id": _c("integer", True),
        "quote_id": _c("integer", True),
        "actor_user_id": _c("integer"),
        "source_version": _c("character varying(160)", True),
        "payload": _c("jsonb", True, "'{}'::jsonb"),
        "occurred_at": _c("timestamp with time zone", True),
        "created_at": _c("timestamp with time zone", True, "now()"),
    },
    "geo_article_delivery_slots": {
        "delivery_slot_key": _c("uuid", True),
        "contract_revision_id": _c("bigint", True),
        "contract_ordinal": _c("integer", True),
        "owner_user_id": _c("integer", True),
        "brand_id": _c("integer", True),
        "quote_id": _c("integer", True),
        "current_state": _c("character varying(24)", True),
        "projection_version": _c("integer", True),
        "current_event_id": _c("bigint", True),
        "last_event_key": _c("character(64)", True),
        "keyword_id": _c("integer"),
        "topic_id": _c("integer"),
        "article_id": _c("integer"),
        "superseded_by_slot_key": _c("uuid"),
        "blocked_reason_code": _c("character varying(80)"),
        "blocked_user_message": _c("text"),
        "owner_kind": _c("character varying(40)"),
        "next_action": _c("text"),
        "completion_evidence": _c("text"),
        "blocked_at": _c("timestamp with time zone"),
        "last_reminded_at": _c("timestamp with time zone"),
        "target_resolution_at": _c("timestamp with time zone"),
        "escalation_status": _c("character varying(32)"),
        "resolution_evidence": _c("text"),
        "created_at": _c("timestamp with time zone", True, "now()"),
        "updated_at": _c("timestamp with time zone", True, "now()"),
    },
    "geo_article_target_question_snapshots": {
        "id": _c("bigint", True, "nextval"),
        "question_key": _c("character(64)", True),
        "tenant_owner_user_id": _c("integer", True),
        "brand_id": _c("integer", True),
        "quote_id": _c("integer", True),
        "delivery_slot_key": _c("uuid", True),
        "question_source_type": _c("character varying(40)", True),
        "source_object_type": _c("character varying(64)"),
        "source_id": _c("bigint"),
        "source_text_snapshot": _c("text", True),
        "source_version": _c("character varying(160)", True),
        "source_snapshot_hash": _c("character(64)", True),
        "parent_source_object_type": _c("character varying(64)"),
        "derived_from_id": _c("bigint"),
        "resolver_version": _c("character varying(80)", True),
        "resolution_status": _c("character varying(24)", True),
        "created_by": _c("integer", True),
        "override_reason": _c("text"),
        "approval_evidence_id": _c("text"),
        "created_at": _c("timestamp with time zone", True, "now()"),
    },
    "geo_article_correction_signals": {
        "id": _c("bigint", True, "nextval"),
        "correction_key": _c("character(64)", True),
        "tenant_owner_user_id": _c("integer", True),
        "brand_id": _c("integer", True),
        "quote_id": _c("integer", True),
        "delivery_slot_key": _c("uuid"),
        "topic_id": _c("integer"),
        "article_id": _c("integer"),
        "revision_key": _c("character(64)", True),
        "before_hash": _c("character(64)", True),
        "after_hash": _c("character(64)", True),
        "correction_type": _c("character varying(48)", True),
        "reason": _c("text", True),
        "actor_user_id": _c("integer", True),
        "source_version": _c("character varying(160)", True),
        "candidate_status": _c("character varying(24)", True, "candidate"),
        "occurred_at": _c("timestamp with time zone", True),
        "created_at": _c("timestamp with time zone", True, "now()"),
    },
    "geo_article_review_shadow_events": {
        "id": _c("bigint", True, "nextval"),
        "event_key": _c("character(64)", True),
        "article_id": _c("integer", True),
        "dispatch_source": _c("character varying(80)", True),
        "eligible": _c("boolean", True),
        "reason": _c("character varying(80)", True),
        "canonical_content_hash": _c("character(64)"),
        "outgoing_content_hash": _c("character(64)", True),
        "evidence_manifest_hash": _c("character(64)"),
        "observed_at": _c("timestamp with time zone", True, "now()"),
    },
}


CONSTRAINTS: Final[dict[str, ConstraintContract]] = {
    "geo_article_plan_outbox_pkey": ConstraintContract("geo_article_plan_outbox", "p", ("id",)),
    "geo_article_contract_revisions_pkey": ConstraintContract("geo_article_contract_revisions", "p", ("id",)),
    "geo_article_plan_runs_pkey": ConstraintContract("geo_article_plan_runs", "p", ("id",)),
    "geo_article_delivery_slot_events_pkey": ConstraintContract("geo_article_delivery_slot_events", "p", ("id",)),
    "geo_article_delivery_slots_pkey": ConstraintContract("geo_article_delivery_slots", "p", ("delivery_slot_key",)),
    "geo_article_target_question_snapshots_pkey": ConstraintContract("geo_article_target_question_snapshots", "p", ("id",)),
    "geo_article_correction_signals_pkey": ConstraintContract("geo_article_correction_signals", "p", ("id",)),
    "geo_article_review_shadow_events_pkey": ConstraintContract("geo_article_review_shadow_events", "p", ("id",)),
    "uq_geo_article_plan_outbox_event_key": ConstraintContract("geo_article_plan_outbox", "u", ("event_key",)),
    "uq_geo_article_contract_revision_key": ConstraintContract("geo_article_contract_revisions", "u", ("revision_key",)),
    "uq_geo_article_plan_run_key": ConstraintContract("geo_article_plan_runs", "u", ("run_key",)),
    "uq_geo_article_plan_run_revision_mode": ConstraintContract("geo_article_plan_runs", "u", ("contract_revision_id", "compiler_version", "run_mode")),
    "uq_geo_article_slot_event_key": ConstraintContract("geo_article_delivery_slot_events", "u", ("event_key",)),
    "uq_geo_article_slot_event_version": ConstraintContract("geo_article_delivery_slot_events", "u", ("delivery_slot_key", "slot_version")),
    "uq_geo_article_slot_revision_ordinal": ConstraintContract("geo_article_delivery_slots", "u", ("contract_revision_id", "contract_ordinal")),
    "uq_geo_article_slot_current_event": ConstraintContract("geo_article_delivery_slots", "u", ("current_event_id",)),
    "uq_geo_article_target_question_key": ConstraintContract("geo_article_target_question_snapshots", "u", ("question_key",)),
    "uq_geo_article_correction_key": ConstraintContract("geo_article_correction_signals", "u", ("correction_key",)),
    "uq_geo_article_review_shadow_event_key": ConstraintContract("geo_article_review_shadow_events", "u", ("event_key",)),
    "ck_geo_article_plan_outbox_kind": ConstraintContract("geo_article_plan_outbox", "c", ("event_kind",), definition_contains="quote_paid_standard"),
    "ck_geo_article_plan_outbox_status": ConstraintContract("geo_article_plan_outbox", "c", ("status",), definition_contains="dead_letter"),
    "ck_geo_article_contract_delivery_count": ConstraintContract("geo_article_contract_revisions", "c", ("delivery_count",), definition_contains="delivery_count >= 0"),
    "ck_geo_article_plan_run_mode": ConstraintContract("geo_article_plan_runs", "c", ("run_mode",), definition_contains="canary"),
    "ck_geo_article_plan_run_status": ConstraintContract("geo_article_plan_runs", "c", ("status",), definition_contains="failed"),
    "ck_geo_article_plan_run_verdict": ConstraintContract("geo_article_plan_runs", "c", ("verdict",), definition_contains="INSUFFICIENT_SAMPLES"),
    "ck_geo_article_slot_event_kind": ConstraintContract("geo_article_delivery_slot_events", "c", ("event_kind",), definition_contains="publication_locked"),
    "ck_geo_article_slot_event_state": ConstraintContract("geo_article_delivery_slot_events", "c", ("target_state",), definition_contains="superseded"),
    "ck_geo_article_slot_state": ConstraintContract("geo_article_delivery_slots", "c", ("current_state",), definition_contains="superseded"),
    "ck_geo_article_slot_blocked_fields": ConstraintContract("geo_article_delivery_slots", "c", ("current_state", "blocked_reason_code", "blocked_user_message", "owner_kind", "next_action", "blocked_at"), definition_contains="blocked_reason_code IS NOT NULL"),
    "ck_geo_article_question_source_type": ConstraintContract("geo_article_target_question_snapshots", "c", ("question_source_type",), definition_contains="purchased_monitoring_exact"),
    "ck_geo_article_question_resolution": ConstraintContract("geo_article_target_question_snapshots", "c", ("resolution_status",), definition_contains="ambiguous"),
    "ck_geo_article_question_override": ConstraintContract("geo_article_target_question_snapshots", "c", ("question_source_type", "override_reason", "approval_evidence_id"), definition_contains="approval_evidence_id IS NOT NULL"),
    "ck_geo_article_correction_candidate": ConstraintContract("geo_article_correction_signals", "c", ("candidate_status",), definition_contains="candidate"),
    "fk_geo_article_plan_outbox_brand": ConstraintContract("geo_article_plan_outbox", "f", ("brand_id",), "brands", ("id",)),
    "fk_geo_article_plan_outbox_quote": ConstraintContract("geo_article_plan_outbox", "f", ("quote_id",), "quotes", ("id",)),
    "fk_geo_article_contract_brand": ConstraintContract("geo_article_contract_revisions", "f", ("brand_id",), "brands", ("id",)),
    "fk_geo_article_contract_quote": ConstraintContract("geo_article_contract_revisions", "f", ("quote_id",), "quotes", ("id",)),
    "fk_geo_article_plan_run_revision": ConstraintContract("geo_article_plan_runs", "f", ("contract_revision_id",), "geo_article_contract_revisions", ("id",)),
    "fk_geo_article_plan_run_outbox": ConstraintContract("geo_article_plan_runs", "f", ("source_outbox_id",), "geo_article_plan_outbox", ("id",)),
    "fk_geo_article_slot_event_revision": ConstraintContract("geo_article_delivery_slot_events", "f", ("contract_revision_id",), "geo_article_contract_revisions", ("id",)),
    "fk_geo_article_slot_event_run": ConstraintContract("geo_article_delivery_slot_events", "f", ("plan_run_id",), "geo_article_plan_runs", ("id",)),
    "fk_geo_article_slot_revision": ConstraintContract("geo_article_delivery_slots", "f", ("contract_revision_id",), "geo_article_contract_revisions", ("id",)),
    "fk_geo_article_slot_current_event": ConstraintContract("geo_article_delivery_slots", "f", ("current_event_id",), "geo_article_delivery_slot_events", ("id",)),
    "fk_geo_article_slot_keyword": ConstraintContract("geo_article_delivery_slots", "f", ("keyword_id",), "confirmed_keywords", ("id",)),
    "fk_geo_article_slot_topic": ConstraintContract("geo_article_delivery_slots", "f", ("topic_id",), "topics", ("id",)),
    "fk_geo_article_slot_article": ConstraintContract("geo_article_delivery_slots", "f", ("article_id",), "articles", ("id",)),
    "fk_geo_article_slot_superseded_by": ConstraintContract("geo_article_delivery_slots", "f", ("superseded_by_slot_key",), "geo_article_delivery_slots", ("delivery_slot_key",)),
    "fk_geo_article_target_question_slot": ConstraintContract("geo_article_target_question_snapshots", "f", ("delivery_slot_key",), "geo_article_delivery_slots", ("delivery_slot_key",)),
    "fk_geo_article_correction_slot": ConstraintContract("geo_article_correction_signals", "f", ("delivery_slot_key",), "geo_article_delivery_slots", ("delivery_slot_key",)),
    "fk_geo_article_review_shadow_article": ConstraintContract("geo_article_review_shadow_events", "f", ("article_id",), "articles", ("id",)),
    "fk_topics_delivery_slot": ConstraintContract("topics", "f", ("delivery_slot_key",), "geo_article_delivery_slots", ("delivery_slot_key",)),
    "fk_topics_plan_run": ConstraintContract("topics", "f", ("plan_run_id",), "geo_article_plan_runs", ("id",)),
    "fk_topics_target_question": ConstraintContract("topics", "f", ("target_question_snapshot_id",), "geo_article_target_question_snapshots", ("id",)),
    "fk_articles_delivery_slot": ConstraintContract("articles", "f", ("delivery_slot_key",), "geo_article_delivery_slots", ("delivery_slot_key",)),
    "fk_articles_target_question": ConstraintContract("articles", "f", ("target_question_snapshot_id",), "geo_article_target_question_snapshots", ("id",)),
}

# PostgreSQL 16.13 canonical ``pg_get_constraintdef(..., true)`` output.  A
# substring check would accept weakened predicates such as
# ``status IS NULL OR status='dead_letter'``.  Pinning the complete definition
# makes readiness reject every semantically broader same-name CHECK.
CHECK_DEFINITIONS: Final[dict[str, str]] = {
    "ck_geo_article_plan_outbox_kind": "CHECK (event_kind::text = ANY (ARRAY['quote_paid_standard'::character varying, 'quote_paid_offline'::character varying, 'quote_paid_agent_activation'::character varying, 'zero_price_writing_project_created'::character varying, 'contract_add_on'::character varying, 'keyword_reassigned'::character varying, 'publication_locked'::character varying]::text[]))",
    "ck_geo_article_plan_outbox_status": "CHECK (status::text = ANY (ARRAY['pending'::character varying, 'processing'::character varying, 'completed'::character varying, 'dead_letter'::character varying]::text[]))",
    "ck_geo_article_contract_delivery_count": "CHECK (delivery_count >= 0)",
    "ck_geo_article_plan_run_mode": "CHECK (run_mode::text = ANY (ARRAY['shadow'::character varying, 'assisted'::character varying, 'canary'::character varying]::text[]))",
    "ck_geo_article_plan_run_status": "CHECK (status::text = ANY (ARRAY['pending'::character varying, 'completed'::character varying, 'failed'::character varying]::text[]))",
    "ck_geo_article_plan_run_verdict": "CHECK (verdict::text = ANY (ARRAY['PASS'::character varying, 'FAIL'::character varying, 'INSUFFICIENT_SAMPLES'::character varying, 'NOT_EVALUATED'::character varying]::text[]))",
    # 🔴 [WP1 · migration 035 · 2026-08-18] 该 CHECK 已被**原子替换**为 14 值版本
    #    (既有 9 种 + 图文渠道 5 种:slot_claimed/slot_released/generation_started/
    #     post_linked/ready)。窄 RFC 见
    #    docs/AI-CONTEXT/RFC_GEO_IMAGE_NOTE_SLOT_SIDECAR_ACTIVATION_2026-08-17.md,
    #    Review-CTO 2026-08-17 批准 R1/R2/R3。
    #    🔴 这里必须**同步**改:本契约把定义逐字钉死(注释里写着"substring 检查会接受
    #    被削弱的谓词"),035 跑完之后不改这里,文章 sidecar 的 readiness 会立刻报
    #    `wrong_constraint_definition` —— 那是一个由我们自己制造的假红。
    #    渲染口径按 PG16 `pg_get_constraintdef(..., true)` 实测:值是
    #    `'x'::character varying`,外层 `::text[]`,与既有 9 值版本同形。
    "ck_geo_article_slot_event_kind": "CHECK (event_kind::text = ANY (ARRAY['created'::character varying, 'blocked'::character varying, 'unblocked'::character varying, 'cancelled'::character varying, 'superseded'::character varying, 'reassigned'::character varying, 'topic_linked'::character varying, 'article_linked'::character varying, 'publication_locked'::character varying, 'slot_claimed'::character varying, 'slot_released'::character varying, 'generation_started'::character varying, 'post_linked'::character varying, 'ready'::character varying]::text[]))",
    "ck_geo_article_slot_event_state": "CHECK (target_state::text = ANY (ARRAY['active'::character varying, 'blocked'::character varying, 'cancelled'::character varying, 'superseded'::character varying]::text[]))",
    "ck_geo_article_slot_state": "CHECK (current_state::text = ANY (ARRAY['active'::character varying, 'blocked'::character varying, 'cancelled'::character varying, 'superseded'::character varying]::text[]))",
    "ck_geo_article_slot_blocked_fields": "CHECK (current_state::text <> 'blocked'::text OR blocked_reason_code IS NOT NULL AND blocked_user_message IS NOT NULL AND owner_kind IS NOT NULL AND next_action IS NOT NULL AND blocked_at IS NOT NULL)",
    "ck_geo_article_question_source_type": "CHECK (question_source_type::text = ANY (ARRAY['purchased_monitoring_exact'::character varying, 'confirmed_current_proxy'::character varying, 'extra_current_proxy'::character varying, 'derived_content_question'::character varying, 'operator_override'::character varying]::text[]))",
    "ck_geo_article_question_resolution": "CHECK (resolution_status::text = ANY (ARRAY['exact'::character varying, 'proxy'::character varying, 'ambiguous'::character varying, 'unresolved'::character varying]::text[]))",
    "ck_geo_article_question_override": "CHECK (question_source_type::text <> 'operator_override'::text OR override_reason IS NOT NULL AND approval_evidence_id IS NOT NULL)",
    "ck_geo_article_correction_candidate": "CHECK (candidate_status::text = ANY (ARRAY['candidate'::character varying, 'reviewed'::character varying, 'rejected'::character varying]::text[]))",
}


INDEXES: Final[dict[str, IndexContract]] = {
    "idx_geo_article_plan_outbox_due": IndexContract("geo_article_plan_outbox", ("status", "available_at", "id")),
    "idx_geo_article_plan_outbox_quote": IndexContract("geo_article_plan_outbox", ("quote_id", "created_at DESC")),
    "idx_geo_article_contract_quote": IndexContract("geo_article_contract_revisions", ("quote_id", "created_at DESC")),
    "idx_geo_article_plan_runs_quote": IndexContract("geo_article_plan_runs", ("quote_id", "created_at DESC")),
    "idx_geo_article_slot_events_slot": IndexContract("geo_article_delivery_slot_events", ("delivery_slot_key", "slot_version DESC")),
    "idx_geo_article_slots_quote_state": IndexContract("geo_article_delivery_slots", ("quote_id", "current_state", "contract_ordinal")),
    "uq_geo_article_slots_topic": IndexContract("geo_article_delivery_slots", ("topic_id",), True, "(topic_id IS NOT NULL)"),
    "uq_geo_article_slots_article": IndexContract("geo_article_delivery_slots", ("article_id",), True, "(article_id IS NOT NULL)"),
    "idx_geo_article_target_questions_quote": IndexContract("geo_article_target_question_snapshots", ("quote_id", "created_at DESC")),
    "idx_geo_article_corrections_quote": IndexContract("geo_article_correction_signals", ("quote_id", "occurred_at DESC")),
    "idx_geo_article_review_shadow_article_time": IndexContract("geo_article_review_shadow_events", ("article_id", "observed_at DESC")),
    "uq_topics_delivery_slot_key": IndexContract("topics", ("delivery_slot_key",), True, "(delivery_slot_key IS NOT NULL)"),
}


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).replace('"', "")


def _canonical_check_definition(value: Any) -> str:
    """Normalize only PG16's equivalent varchar-array cast renderings."""
    normalized = _norm(value).lower()
    normalized = normalized.replace("::character varying::text", "::character varying")
    return normalized.replace("]::text[]", "]")


def _check_definition_matches(expected: str, actual: Any) -> bool:
    return _canonical_check_definition(expected) == _canonical_check_definition(actual)


def _values(row: Any, *keys: str) -> tuple[Any, ...]:
    if isinstance(row, Mapping):
        return tuple(row[key] for key in keys)
    return tuple(row[index] for index in range(len(keys)))


def _attnames(cur, table_oid: int, attnums: list[int] | tuple[int, ...] | None) -> tuple[str, ...]:
    if not attnums:
        return ()
    cur.execute("SELECT attnum, attname FROM pg_attribute WHERE attrelid=%s AND attnum=ANY(%s)", (table_oid, list(attnums)))
    names = {int(_values(row, "attnum", "attname")[0]): str(_values(row, "attnum", "attname")[1]) for row in cur.fetchall()}
    return tuple(names[int(number)] for number in attnums)


def schema_blockers(cur) -> list[str]:
    blockers: list[str] = []
    for table, expected_columns in COLUMNS.items():
        cur.execute("SELECT to_regclass(%s)::oid AS table_oid", (table,))
        table_oid = _values(cur.fetchone(), "table_oid")[0]
        if table_oid is None:
            blockers.append(f"missing_table:{table}")
            continue
        for name, expected in expected_columns.items():
            cur.execute(
                """
                SELECT format_type(a.atttypid,a.atttypmod) AS actual_type,
                       a.attnotnull AS actual_not_null,
                       pg_get_expr(d.adbin,d.adrelid) AS actual_default
                  FROM pg_attribute a
                  LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
                 WHERE a.attrelid=%s AND a.attname=%s AND a.attnum>0 AND NOT a.attisdropped
                """,
                (table_oid, name),
            )
            row = cur.fetchone()
            if not row:
                blockers.append(f"missing_column:{table}.{name}")
                continue
            actual_type, actual_not_null, actual_default = _values(row, "actual_type", "actual_not_null", "actual_default")
            if str(actual_type) != expected.sql_type:
                blockers.append(f"wrong_type:{table}.{name}:{actual_type}!={expected.sql_type}")
            if bool(actual_not_null) != expected.not_null:
                blockers.append(f"wrong_nullability:{table}.{name}:{actual_not_null}!={expected.not_null}")
            if expected.default_contains is None:
                if actual_default is not None:
                    blockers.append(f"unexpected_default:{table}.{name}:{actual_default}")
            elif expected.default_contains not in _norm(actual_default):
                blockers.append(f"wrong_default:{table}.{name}:{actual_default}")

    for name, expected in CONSTRAINTS.items():
        cur.execute(
            """
            SELECT c.contype AS kind,c.convalidated AS validated,c.conrelid AS relid,c.conkey AS conkey,
                   c.confrelid AS confrelid,c.confkey AS confkey,pg_get_constraintdef(c.oid,true) AS definition,
                   c.confdeltype AS on_delete,c.confupdtype AS on_update,c.confmatchtype AS match_type,
                   c.condeferrable AS is_deferrable,c.condeferred AS is_initially_deferred
              FROM pg_constraint c
             WHERE c.conname=%s AND c.connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())
            """,
            (name,),
        )
        rows = cur.fetchall()
        if len(rows) != 1:
            blockers.append(f"constraint_count:{name}:{len(rows)}")
            continue
        kind, validated, relid, conkey, confrelid, confkey, definition, on_delete, on_update, match_type, is_deferrable, is_initially_deferred = _values(
            rows[0], "kind", "validated", "relid", "conkey", "confrelid", "confkey", "definition", "on_delete", "on_update", "match_type", "is_deferrable", "is_initially_deferred"
        )
        cur.execute("SELECT %s::regclass::oid", (expected.table,))
        expected_relid = _values(cur.fetchone(), "oid")[0]
        actual_columns = _attnames(cur, int(relid), conkey)
        if str(kind) != expected.kind or int(relid) != int(expected_relid) or actual_columns != expected.columns:
            blockers.append(f"wrong_constraint_identity:{name}:{kind}:{actual_columns}")
        if bool(validated) != expected.validated:
            blockers.append(f"wrong_constraint_validated:{name}:{validated}")
        if expected.ref_table:
            cur.execute("SELECT %s::regclass::oid", (expected.ref_table,))
            expected_confrelid = _values(cur.fetchone(), "oid")[0]
            actual_ref_columns = _attnames(cur, int(confrelid), confkey)
            if int(confrelid) != int(expected_confrelid) or actual_ref_columns != expected.ref_columns:
                blockers.append(f"wrong_constraint_reference:{name}:{actual_ref_columns}")
            if str(on_delete) != (expected.on_delete or "a") or str(on_update) != (expected.on_update or "a"):
                blockers.append(f"wrong_constraint_actions:{name}:{on_delete}:{on_update}")
            if str(match_type) != (expected.match_type or "s"):
                blockers.append(f"wrong_constraint_match:{name}:{match_type}")
        if bool(is_deferrable) != expected.deferrable or bool(is_initially_deferred) != expected.initially_deferred:
            blockers.append(f"wrong_constraint_deferral:{name}")
        exact_definition = CHECK_DEFINITIONS.get(name)
        if exact_definition is not None:
            if not _check_definition_matches(exact_definition, definition):
                blockers.append(f"wrong_constraint_definition:{name}:{definition}")
        elif expected.definition_contains and _norm(expected.definition_contains).lower() not in _norm(definition).lower():
            blockers.append(f"wrong_constraint_definition:{name}:{definition}")

    for name, expected in INDEXES.items():
        cur.execute(
            """
            SELECT i.indrelid AS relid,i.indisunique AS is_unique,i.indisvalid AS is_valid,
                   i.indisready AS is_ready,i.indoption::int2[] AS options,
                   ARRAY(
                       SELECT pg_get_indexdef(i.indexrelid,key_no,true)
                         FROM generate_series(1,i.indnkeyatts) AS key_no
                        ORDER BY key_no
                   ) AS actual_keys,
                   pg_get_expr(i.indpred,i.indrelid) AS predicate
              FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
             WHERE c.relname=%s AND c.relnamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())
            """,
            (name,),
        )
        rows = cur.fetchall()
        if len(rows) != 1:
            blockers.append(f"index_count:{name}:{len(rows)}")
            continue
        relid, is_unique, is_valid, is_ready, options, actual_keys, predicate = _values(
            rows[0], "relid", "is_unique", "is_valid", "is_ready", "options", "actual_keys", "predicate"
        )
        cur.execute("SELECT %s::regclass::oid", (expected.table,))
        expected_relid = _values(cur.fetchone(), "oid")[0]
        if int(relid) != int(expected_relid) or bool(is_unique) != expected.unique or not bool(is_valid) or not bool(is_ready):
            blockers.append(f"wrong_index_identity:{name}")
        option_values = list(options or ())
        normalized_actual_keys = tuple(
            _norm(key).lower() + (" desc" if pos < len(option_values) and (int(option_values[pos]) & 1) else "")
            for pos, key in enumerate(actual_keys or ())
        )
        if normalized_actual_keys != tuple(_norm(key).lower() for key in expected.keys):
            blockers.append(f"wrong_index_keys:{name}:{normalized_actual_keys}")
        if _norm(predicate).lower() != _norm(expected.predicate).lower():
            blockers.append(f"wrong_index_predicate:{name}:{predicate}")
    return blockers


def assert_schema_ready(cur) -> None:
    blockers = schema_blockers(cur)
    if blockers:
        raise RuntimeError("GEO_ARTICLE_CLOSED_LOOP_SCHEMA_NOT_READY:" + "|".join(blockers))
