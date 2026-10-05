"""Machine-readable PostgreSQL contract for the GEO article v1.4 package."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any, Final


@dataclass(frozen=True)
class ColumnContract:
    sql_type: str
    not_null: bool = False
    default_contains: str | None = None


@dataclass(frozen=True)
class ConstraintContract:
    table: str
    kind: str
    columns: tuple[str, ...]
    ref_table: str | None = None
    ref_columns: tuple[str, ...] = ()
    definition_contains: str | None = None
    definition_alternatives: tuple[str, ...] = ()
    validated: bool = True
    on_delete: str | None = None
    on_update: str | None = None
    match_type: str | None = None
    deferrable: bool = False
    initially_deferred: bool = False


@dataclass(frozen=True)
class IndexContract:
    table: str
    keys: tuple[str, ...]
    unique: bool = False
    predicate: str | None = None


COLUMNS: Final[dict[str, dict[str, ColumnContract]]] = {
    "topics": {
        "article_id": ColumnContract("integer"),
        "legacy_article_generation_id": ColumnContract("integer"),
    },
    "articles": {
        "style_family": ColumnContract("character varying(64)"),
        "style_contract_version": ColumnContract("character varying(80)"),
        "style_version": ColumnContract("character varying(80)"),
        "generation_request_id": ColumnContract("text"),
        "generation_request_snapshot": ColumnContract("jsonb"),
        "prompt_hash": ColumnContract("character(64)"),
        "evidence_pack": ColumnContract("jsonb"),
        "evidence_manifest_hash": ColumnContract("character(64)"),
        "brand_fact_snapshot": ColumnContract("jsonb"),
        "brand_snapshot_hash": ColumnContract("character(64)"),
        "article_review": ColumnContract("jsonb"),
        "article_review_status": ColumnContract("character varying(40)", default_contains="legacy_unreviewed"),
        "publication_profile": ColumnContract("character varying(64)", True, "standard"),
        "platform_review": ColumnContract("jsonb"),
        "article_human_review_status": ColumnContract("character varying(40)"),
        "article_human_reviewed_by": ColumnContract("integer"),
        "article_human_reviewed_at": ColumnContract("timestamp with time zone"),
        "article_human_review_reason": ColumnContract("text"),
        "current_content_hash": ColumnContract("character(64)"),
        "publication_snapshot": ColumnContract("jsonb"),
        "publication_snapshot_hash": ColumnContract("character(64)"),
        "publication_snapshot_at": ColumnContract("timestamp with time zone"),
        "publication_snapshot_source": ColumnContract("character varying(40)"),
        "publication_snapshot_source_id": ColumnContract("bigint"),
    },
    "mhz_publish_orders": {
        "article_content_snapshot": ColumnContract("text"),
        "article_content_snapshot_hash": ColumnContract("character(64)"),
        "article_content_snapshot_at": ColumnContract("timestamp with time zone"),
        "article_content_snapshot_source": ColumnContract("character varying(40)"),
    },
    "mhz_publish_order_items": {
        "submitted_title_snapshot": ColumnContract("text"),
        "submitted_content_snapshot": ColumnContract("text"),
        "submitted_content_snapshot_hash": ColumnContract("character(64)"),
        "submitted_content_snapshot_at": ColumnContract("timestamp with time zone"),
        "submitted_content_snapshot_source": ColumnContract("character varying(80)"),
    },
    "publish_records": {
        "submitted_title_snapshot": ColumnContract("text"),
        "submitted_content_snapshot": ColumnContract("text"),
        "submitted_content_snapshot_hash": ColumnContract("character(64)"),
        "submitted_content_snapshot_at": ColumnContract("timestamp with time zone"),
        "submitted_content_snapshot_source": ColumnContract("character varying(80)"),
        "public_url": ColumnContract("text"),
        "public_url_reported_explicitly": ColumnContract("boolean", True, "false"),
        "public_url_report_source": ColumnContract("character varying(80)"),
        # [WO 自报收口 2026-08-19] 权威位:服务端核实态(来源位之外的独立一维)
        "public_url_verification_state": ColumnContract(
            "character varying(32)", True, "unverified"),
        # 🔴 R3 §④ 补漏:权威来源列漏在契约外面过一次。它不是可有可无的元数据 ——
        #    `verified ⇒ source ∈ (provider_receipt, human_attestation)` 那条库级
        #    CHECK 整个挂在这一列上,列不在 readiness 契约里 = 那道锁没人盯。
        "public_url_verification_source": ColumnContract("character varying(40)"),
        "public_url_verified_at": ColumnContract("timestamp with time zone"),
        "public_url_verification_method": ColumnContract("character varying(80)"),
        "public_url_verification_detail": ColumnContract("jsonb"),
        # [R3 §①] 可达轴:页面此刻还在不在(与核实轴分开的第二根轴)
        "public_url_availability_state": ColumnContract("character varying(32)"),
        # [R4 §B] 可达轴的权威来源 —— `retracted 只能来自人工` 那条 CHECK 挂在它身上
        "public_url_availability_source": ColumnContract("character varying(40)"),
        "public_url_availability_checked_at": ColumnContract("timestamp with time zone"),
        "public_url_availability_detail": ColumnContract("jsonb"),
    },
    "monitoring_results": {
        "sent_question_snapshot": ColumnContract("text"),
        "keyword_source": ColumnContract("character varying(32)", default_contains="legacy_unknown"),
        "keyword_type": ColumnContract("character varying(32)", default_contains="legacy_unknown"),
        "question_family": ColumnContract("character varying(64)", default_contains="legacy_unknown"),
        "question_family_version": ColumnContract("character varying(80)", default_contains="legacy_unknown"),
        "keyword_source_id": ColumnContract("bigint"),
        "keyword_resolver_status": ColumnContract("character varying(32)", default_contains="legacy_unknown"),
        "provider": ColumnContract("character varying(64)", default_contains="legacy_unknown"),
        "model": ColumnContract("character varying(128)", default_contains="legacy_unknown"),
        "model_revision": ColumnContract("character varying(128)", default_contains="legacy_unknown"),
        "model_revision_unknown_reason": ColumnContract("character varying(64)"),
        "surface": ColumnContract("character varying(64)", default_contains="legacy_unknown"),
        "search_mode": ColumnContract("character varying(64)", default_contains="legacy_unknown"),
        "response_status": ColumnContract("character varying(32)", default_contains="legacy_unknown"),
        "target_brand_snapshot": ColumnContract("text"),
        "target_entity_snapshot": ColumnContract("text"),
        "target_outcome": ColumnContract("character varying(40)", default_contains="legacy_unknown"),
        "outcome_resolver_version": ColumnContract("character varying(80)", default_contains="legacy_unknown"),
        "outcome_resolver_confidence": ColumnContract("numeric(5,4)"),
        "lineage_version": ColumnContract("character varying(80)", default_contains="legacy_unknown"),
        "lineage_status": ColumnContract("character varying(32)", default_contains="legacy_unknown"),
        "lineage_error_reason": ColumnContract("text"),
        "provider_request_id": ColumnContract("text"),
        "sent_at": ColumnContract("timestamp with time zone"),
    },
    "geo_research_raw": {
        "provider": ColumnContract("character varying(64)", default_contains="legacy_unknown"),
        "model": ColumnContract("character varying(128)", default_contains="legacy_unknown"),
        "model_revision": ColumnContract("character varying(128)", default_contains="legacy_unknown"),
        "surface": ColumnContract("character varying(64)", default_contains="legacy_unknown"),
        "search_mode": ColumnContract("character varying(64)", default_contains="legacy_unknown"),
        "prompt_snapshot": ColumnContract("text"),
    },
    "geo_research_prompts": {
        "family_key": ColumnContract("character varying(100)"),
        "parent_prompt_id": ColumnContract("bigint"),
        "question_version": ColumnContract("integer", True, "1"),
        "query_kind": ColumnContract("character varying(40)", True, "research"),
        "source_type": ColumnContract("character varying(40)", True, "manual"),
        "hypothesis": ColumnContract("text"),
        "single_change_dimension": ColumnContract("text"),
        "experiment_group": ColumnContract("character varying(40)", True, "shadow"),
        "evolution_status": ColumnContract("character varying(40)", True, "draft"),
        "policy_version": ColumnContract("character varying(80)", True, "question-evolution-v1"),
        "approved_by": ColumnContract("text"),
        "approved_at": ColumnContract("timestamp with time zone"),
        "activated_at": ColumnContract("timestamp with time zone"),
        "retired_at": ColumnContract("timestamp with time zone"),
    },
    "geo_research_articles": {
        "corpus_grade": ColumnContract("character varying(8)", default_contains="JC0"),
        "canonical_body_hash": ColumnContract("character(64)"),
        "body_hash_algorithm": ColumnContract("character varying(80)"),
        "content_cluster_id": ColumnContract("character varying(80)"),
        "body_boundary_version": ColumnContract("character varying(80)"),
        "label_provenance_version": ColumnContract("character varying(80)"),
    },
    "geo_research_source_signals": {
        "label_provenance_type": ColumnContract("character varying(40)", default_contains="legacy_unknown"),
        "label_provenance_version": ColumnContract("character varying(80)", default_contains="legacy_unknown"),
        "provider": ColumnContract("character varying(64)", default_contains="legacy_unknown"),
        "model": ColumnContract("character varying(128)", default_contains="legacy_unknown"),
        "model_revision": ColumnContract("character varying(128)", default_contains="legacy_unknown"),
        "surface": ColumnContract("character varying(64)", default_contains="legacy_unknown"),
        "search_mode": ColumnContract("character varying(64)", default_contains="legacy_unknown"),
        "prompt_snapshot": ColumnContract("text"),
        "article_snapshot_hash": ColumnContract("character(64)"),
        "article_fetch_id": ColumnContract("bigint"),
        "lineage_status": ColumnContract("character varying(32)", default_contains="legacy_unknown"),
        "lineage_error_reason": ColumnContract("text"),
    },
    "geo_article_review_events": {
        "id": ColumnContract("bigint", True, "nextval"),
        "article_id": ColumnContract("bigint", True),
        "actor_user_id": ColumnContract("integer", True),
        "decision": ColumnContract("character varying(40)", True),
        "reason": ColumnContract("text", True),
        "machine_review_status": ColumnContract("character varying(40)"),
        "machine_review_version": ColumnContract("character varying(100)"),
        "reviewed_content_hash": ColumnContract("character(64)"),
        "evidence_manifest_hash": ColumnContract("character(64)"),
        "created_at": ColumnContract("timestamp with time zone", True, "now()"),
    },
    "geo_article_gold_labels": {
        "id": ColumnContract("bigint", True, "nextval"),
        "judge_kind": ColumnContract("character varying(40)", True),
        "source_id": ColumnContract("bigint", True),
        "machine_label": ColumnContract("character varying(64)", True),
        "human_label": ColumnContract("character varying(64)", True),
        "input_snapshot": ColumnContract("jsonb", True),
        "reviewer_user_id": ColumnContract("integer", True),
        "rationale": ColumnContract("text", True),
        "calibration_version": ColumnContract("character varying(80)", True),
        "created_at": ColumnContract("timestamp with time zone", True, "now()"),
    },
    "geo_article_experiments": {
        "id": ColumnContract("bigint", True, "nextval"),
        "experiment_key": ColumnContract("character varying(40)", True),
        "contract_version": ColumnContract("character varying(80)", True),
        "style_family": ColumnContract("character varying(64)", True),
        "hypothesis": ColumnContract("text", True),
        "single_change_dimension": ColumnContract("character varying(80)", True),
        "primary_metric": ColumnContract("character varying(120)", True),
        "baseline_version_id": ColumnContract("character varying(200)", True),
        "candidate_version_id": ColumnContract("character varying(200)", True),
        "scope": ColumnContract("jsonb", True, "'{}'::jsonb"),
        "min_arm_articles": ColumnContract("integer", True, "30"),
        "minimum_weeks": ColumnContract("integer", True, "4"),
        "frozen_config": ColumnContract("jsonb", True),
        "state": ColumnContract("character varying(40)", True, "preregistered"),
        "created_by": ColumnContract("integer", True),
        "approved_by": ColumnContract("integer"),
        "approval_reason": ColumnContract("text"),
        "approved_at": ColumnContract("timestamp with time zone"),
        "started_at": ColumnContract("timestamp with time zone"),
        "observation_end": ColumnContract("timestamp with time zone"),
        "decision_by": ColumnContract("integer"),
        "decision_reason": ColumnContract("text"),
        "decision_snapshot": ColumnContract("jsonb"),
        "created_at": ColumnContract("timestamp with time zone", True, "now()"),
        "updated_at": ColumnContract("timestamp with time zone", True, "now()"),
    },
    "geo_article_experiment_assignments": {
        "id": ColumnContract("bigint", True, "nextval"),
        "experiment_id": ColumnContract("bigint", True),
        "article_id": ColumnContract("bigint"),
        "topic_id": ColumnContract("bigint", True),
        "generation_request_id": ColumnContract("text"),
        "arm": ColumnContract("character varying(20)", True),
        "article_style_version": ColumnContract("character varying(200)", True),
        "assignment_hash": ColumnContract("character(64)", True),
        "assigned_by": ColumnContract("integer", True),
        "assigned_at": ColumnContract("timestamp with time zone", True, "now()"),
    },
    "geo_article_evolution_runs": {
        "id": ColumnContract("bigint", True, "nextval"),
        "cycle_key": ColumnContract("character varying(16)", True),
        "cycle_version": ColumnContract("character varying(80)", True),
        "trigger_source": ColumnContract("character varying(40)", True),
        "state": ColumnContract("character varying(40)", True),
        "truth_level": ColumnContract("character varying(40)"),
        "data_health": ColumnContract("jsonb", True),
        "corpus_summary": ColumnContract("jsonb", True),
        "review_summary": ColumnContract("jsonb", True),
        "experiment_summary": ColumnContract("jsonb", True),
        "question_summary": ColumnContract("jsonb", True),
        "cost_summary": ColumnContract("jsonb", True, "'{}'::jsonb"),
        "recommendations": ColumnContract("jsonb", True),
        "created_by": ColumnContract("integer", True),
        "started_at": ColumnContract("timestamp with time zone", True),
        "finished_at": ColumnContract("timestamp with time zone"),
        "reviewed_by": ColumnContract("integer"),
        "reviewed_at": ColumnContract("timestamp with time zone"),
        "review_note": ColumnContract("text"),
    },
    "geo_question_evolution_events": {
        "id": ColumnContract("bigint", True, "nextval"),
        "prompt_id": ColumnContract("bigint", True),
        "actor_user_id": ColumnContract("integer", True),
        "action": ColumnContract("character varying(40)", True),
        "from_status": ColumnContract("character varying(40)"),
        "to_status": ColumnContract("character varying(40)"),
        "reason": ColumnContract("text", True),
        "policy_version": ColumnContract("character varying(80)", True),
        "created_at": ColumnContract("timestamp with time zone", True, "now()"),
    },
    "geo_research_corpus_label_events": {
        "id": ColumnContract("bigint", True, "nextval"),
        "article_id": ColumnContract("bigint", True),
        "from_grade": ColumnContract("character varying(8)", True),
        "to_grade": ColumnContract("character varying(8)", True),
        "labeler_version": ColumnContract("character varying(80)", True),
        "direct_signal_count": ColumnContract("integer", True),
        "evidence": ColumnContract("jsonb", True),
        "created_at": ColumnContract("timestamp with time zone", True, "now()"),
    },
    "geo_research_article_fetches": {
        "id": ColumnContract("bigint", True, "nextval"),
        "fetch_event_key": ColumnContract("character varying(64)", True),
        "article_id": ColumnContract("bigint"),
        "source_url": ColumnContract("text", True),
        "normalized_url": ColumnContract("text", True),
        "final_url": ColumnContract("text"),
        "url_hash": ColumnContract("character(40)"),
        "parent_fetch_id": ColumnContract("bigint"),
        "request_profile": ColumnContract("character varying(80)", True),
        "request_profile_version": ColumnContract("character varying(80)", True),
        "preset": ColumnContract("character varying(40)"),
        "engine": ColumnContract("character varying(40)"),
        "cache_policy": ColumnContract("character varying(40)"),
        "timeout_seconds": ColumnContract("integer"),
        "token_budget": ColumnContract("integer"),
        "attempt_number": ColumnContract("integer", True),
        "response_status": ColumnContract("character varying(40)", True),
        "http_status": ColumnContract("integer"),
        "warning": ColumnContract("text"),
        "failure_reason": ColumnContract("character varying(80)"),
        "published_time": ColumnContract("timestamp with time zone"),
        "fetched_at": ColumnContract("timestamp with time zone", True),
        "latency_ms": ColumnContract("integer"),
        "usage_tokens": ColumnContract("integer"),
        "parser_version": ColumnContract("character varying(80)", True),
        "raw_object_key": ColumnContract("text"),
        "raw_response_hash": ColumnContract("character(64)"),
        "raw_body_hash": ColumnContract("character(64)"),
        "body_object_key": ColumnContract("text"),
        "body_hash": ColumnContract("character(64)"),
        "robots_policy": ColumnContract("character varying(40)"),
        "robots_reason": ColumnContract("text"),
        "metadata": ColumnContract("jsonb", True, "'{}'::jsonb"),
        "created_at": ColumnContract("timestamp with time zone", True, "now()"),
    },
}


CONSTRAINTS: Final[dict[str, ConstraintContract]] = {
    "geo_article_review_events_pkey": ConstraintContract("geo_article_review_events", "p", ("id",)),
    "geo_article_gold_labels_pkey": ConstraintContract("geo_article_gold_labels", "p", ("id",)),
    "geo_article_experiments_pkey": ConstraintContract("geo_article_experiments", "p", ("id",)),
    "geo_article_experiment_assignments_pkey": ConstraintContract("geo_article_experiment_assignments", "p", ("id",)),
    "geo_article_evolution_runs_pkey": ConstraintContract("geo_article_evolution_runs", "p", ("id",)),
    "geo_question_evolution_events_pkey": ConstraintContract("geo_question_evolution_events", "p", ("id",)),
    "geo_research_corpus_label_events_pkey": ConstraintContract("geo_research_corpus_label_events", "p", ("id",)),
    "geo_research_article_fetches_pkey": ConstraintContract("geo_research_article_fetches", "p", ("id",)),
    "topics_article_id_articles_fk": ConstraintContract("topics", "f", ("article_id",), "articles", ("id",)),
    "topics_legacy_article_generation_fk": ConstraintContract(
        "topics", "f", ("legacy_article_generation_id",), "article_generations", ("id",)
    ),
    # [WP9-P0-7 ④ · Owner 裁决 D8]决策域由 {approved,rejected} 扩为
    # {approved,rejected,skipped}:skipped = company_facts/brand_softarticle 推荐人审的
    # "明示跳过"(审计留痕)。反漂移意图不变——**只认这三个值**,第四个值或 OR TRUE 绕过仍必须被拒。
    "ck_geo_article_review_decision": ConstraintContract(
        "geo_article_review_events", "c", ("decision",),
        definition_alternatives=(
            "CHECK (decision::text = ANY (ARRAY['approved'::character varying, 'rejected'::character varying, 'skipped'::character varying]::text[]))",
            "CHECK (decision::text = ANY (ARRAY['approved'::character varying::text, 'rejected'::character varying::text, 'skipped'::character varying::text]))",
        ),
    ),
    "fk_geo_article_review_article": ConstraintContract("geo_article_review_events", "f", ("article_id",), "articles", ("id",)),
    "uq_geo_article_gold_label_identity": ConstraintContract(
        "geo_article_gold_labels", "u", ("judge_kind", "source_id", "reviewer_user_id")
    ),
    "uq_geo_article_experiment_key": ConstraintContract("geo_article_experiments", "u", ("experiment_key",)),
    "ck_geo_article_assignment_arm": ConstraintContract(
        "geo_article_experiment_assignments", "c", ("arm",),
        definition_alternatives=(
            "CHECK (arm::text = ANY (ARRAY['control'::character varying, 'candidate'::character varying]::text[]))",
            "CHECK (arm::text = ANY (ARRAY['control'::character varying::text, 'candidate'::character varying::text]))",
        ),
    ),
    "fk_geo_article_assignment_experiment": ConstraintContract(
        "geo_article_experiment_assignments", "f", ("experiment_id",), "geo_article_experiments", ("id",)
    ),
    "fk_geo_article_assignment_article": ConstraintContract("geo_article_experiment_assignments", "f", ("article_id",), "articles", ("id",)),
    "fk_geo_article_assignment_topic": ConstraintContract("geo_article_experiment_assignments", "f", ("topic_id",), "topics", ("id",)),
    "uq_geo_article_assignment_topic": ConstraintContract("geo_article_experiment_assignments", "u", ("experiment_id", "topic_id")),
    "uq_geo_article_assignment_article": ConstraintContract("geo_article_experiment_assignments", "u", ("experiment_id", "article_id")),
    "uq_geo_article_assignment_request": ConstraintContract("geo_article_experiment_assignments", "u", ("generation_request_id",)),
    "uq_geo_article_evolution_cycle": ConstraintContract("geo_article_evolution_runs", "u", ("cycle_key",)),
    "fk_geo_question_event_prompt": ConstraintContract("geo_question_evolution_events", "f", ("prompt_id",), "geo_research_prompts", ("id",)),
    "fk_geo_corpus_event_article": ConstraintContract(
        "geo_research_corpus_label_events", "f", ("article_id",), "geo_research_articles", ("id",)
    ),
    "uq_geo_corpus_event_article_version": ConstraintContract("geo_research_corpus_label_events", "u", ("article_id", "labeler_version")),
    "uq_geo_fetch_event_key": ConstraintContract("geo_research_article_fetches", "u", ("fetch_event_key",)),
    "fk_geo_fetch_article": ConstraintContract(
        "geo_research_article_fetches", "f", ("article_id",),
        "geo_research_articles", ("id",), on_delete="n",
    ),
    "fk_geo_fetch_parent": ConstraintContract(
        "geo_research_article_fetches", "f", ("parent_fetch_id",),
        "geo_research_article_fetches", ("id",), on_delete="n",
    ),
}


INDEXES: Final[dict[str, IndexContract]] = {
    "idx_geo_article_review_events_article": IndexContract(
        "geo_article_review_events", ("article_id", "created_at DESC")
    ),
    "idx_geo_article_gold_labels_kind": IndexContract(
        "geo_article_gold_labels", ("judge_kind", "calibration_version", "source_id")
    ),
    "idx_geo_article_experiments_candidate": IndexContract(
        "geo_article_experiments", ("candidate_version_id", "state")
    ),
    "idx_geo_article_experiment_assignments_topic": IndexContract(
        "geo_article_experiment_assignments", ("topic_id", "experiment_id")
    ),
    "uq_geo_article_experiment_topic": IndexContract(
        "geo_article_experiment_assignments", ("experiment_id", "topic_id"), True,
        "(topic_id IS NOT NULL)",
    ),
    "idx_geo_article_evolution_runs_time": IndexContract(
        "geo_article_evolution_runs", ("started_at DESC",)
    ),
    "idx_geo_question_evolution_events_prompt": IndexContract(
        "geo_question_evolution_events", ("prompt_id", "created_at DESC")
    ),
    "idx_geo_research_corpus_label_events_time": IndexContract(
        "geo_research_corpus_label_events", ("created_at DESC",)
    ),
    "idx_geo_fetches_article_time": IndexContract(
        "geo_research_article_fetches", ("article_id", "fetched_at DESC")
    ),
    "idx_geo_fetches_url_time": IndexContract(
        "geo_research_article_fetches", ("url_hash", "fetched_at DESC")
    ),
    "idx_articles_style_family": IndexContract("articles", ("style_family", "created_at DESC")),
    "idx_articles_publication_snapshot": IndexContract(
        "articles", ("publication_snapshot_at",), predicate="(publication_snapshot_at IS NOT NULL)"
    ),
}


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).replace('"', "")


def _constraint_definition_matches(expected: ConstraintContract, definition: Any) -> bool:
    normalized_definition = _norm(definition)
    if expected.definition_alternatives:
        return normalized_definition in {_norm(candidate) for candidate in expected.definition_alternatives}
    if expected.definition_contains:
        return _norm(expected.definition_contains) in normalized_definition
    return True


def _row_values(row: Any, *keys: str) -> tuple[Any, ...]:
    """Read both psycopg2 tuple rows and RealDictCursor rows."""
    if isinstance(row, Mapping):
        return tuple(row[key] for key in keys)
    return tuple(row[index] for index in range(len(keys)))


def _attnames(cur, table_oid: int, attnums: list[int] | tuple[int, ...] | None) -> tuple[str, ...]:
    if not attnums:
        return ()
    cur.execute(
        "SELECT attnum, attname FROM pg_attribute WHERE attrelid=%s AND attnum=ANY(%s)",
        (table_oid, list(attnums)),
    )
    mapping = {
        int(values[0]): str(values[1])
        for row in cur.fetchall()
        for values in (_row_values(row, "attnum", "attname"),)
    }
    return tuple(mapping[int(num)] for num in attnums)


def schema_blockers(cur) -> list[str]:
    """Return exact pg_catalog mismatches for the current search_path schema."""
    blockers: list[str] = []
    for table, columns in COLUMNS.items():
        cur.execute("SELECT to_regclass(%s)::oid AS table_oid", (table,))
        table_oid = _row_values(cur.fetchone(), "table_oid")[0]
        if table_oid is None:
            blockers.append(f"missing_table:{table}")
            continue
        for name, expected in columns.items():
            cur.execute(
                """
                SELECT format_type(a.atttypid, a.atttypmod) AS actual_type,
                       a.attnotnull AS actual_not_null,
                       pg_get_expr(d.adbin, d.adrelid) AS actual_default
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
            actual_type, actual_not_null, actual_default = _row_values(
                row, "actual_type", "actual_not_null", "actual_default"
            )
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
            SELECT c.contype AS kind, c.convalidated AS validated,
                   c.conrelid AS relid, c.conkey AS conkey,
                   c.confrelid AS confrelid, c.confkey AS confkey,
                   pg_get_constraintdef(c.oid, true) AS definition,
                   c.confdeltype AS on_delete, c.confupdtype AS on_update,
                   c.confmatchtype AS match_type,
                   c.condeferrable AS is_deferrable,
                   c.condeferred AS is_initially_deferred
              FROM pg_constraint c
             WHERE c.conname=%s
               AND c.connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())
            """,
            (name,),
        )
        rows = cur.fetchall()
        if len(rows) != 1:
            blockers.append(f"constraint_count:{name}:{len(rows)}")
            continue
        (
            kind, validated, relid, conkey, confrelid, confkey, definition,
            on_delete, on_update, match_type, is_deferrable, is_initially_deferred,
        ) = _row_values(
            rows[0], "kind", "validated", "relid", "conkey", "confrelid", "confkey",
            "definition", "on_delete", "on_update", "match_type",
            "is_deferrable", "is_initially_deferred",
        )
        cur.execute("SELECT relname AS actual_table FROM pg_class WHERE oid=%s", (relid,))
        actual_table = _row_values(cur.fetchone(), "actual_table")[0]
        columns = _attnames(cur, relid, conkey)
        if (
            actual_table != expected.table or kind != expected.kind
            or columns != expected.columns or bool(validated) != expected.validated
        ):
            blockers.append(
                f"wrong_constraint:{name}:{actual_table}:{kind}:{columns}:validated={validated}"
            )
            continue
        if expected.ref_table:
            cur.execute("SELECT relname AS ref_table FROM pg_class WHERE oid=%s", (confrelid,))
            ref_table = _row_values(cur.fetchone(), "ref_table")[0]
            ref_columns = _attnames(cur, confrelid, confkey)
            if ref_table != expected.ref_table or ref_columns != expected.ref_columns:
                blockers.append(f"wrong_fk:{name}:{ref_table}:{ref_columns}")
            expected_on_delete = expected.on_delete or "a"
            if on_delete != expected_on_delete:
                blockers.append(f"wrong_fk_delete_action:{name}:{on_delete}!={expected_on_delete}")
            expected_on_update = expected.on_update or "a"
            if on_update != expected_on_update:
                blockers.append(f"wrong_fk_update_action:{name}:{on_update}!={expected_on_update}")
            expected_match_type = expected.match_type or "s"
            if match_type != expected_match_type:
                blockers.append(f"wrong_fk_match_type:{name}:{match_type}!={expected_match_type}")
            if bool(is_deferrable) != expected.deferrable:
                blockers.append(
                    f"wrong_fk_deferrable:{name}:{is_deferrable}!={expected.deferrable}"
                )
            if bool(is_initially_deferred) != expected.initially_deferred:
                blockers.append(
                    "wrong_fk_initially_deferred:"
                    f"{name}:{is_initially_deferred}!={expected.initially_deferred}"
                )
        if not _constraint_definition_matches(expected, definition):
            blockers.append(f"weak_constraint:{name}:{definition}")

    for name, expected in INDEXES.items():
        cur.execute(
            """
            SELECT tbl.relname AS table_name, i.indisunique AS is_unique,
                   i.indisvalid AS is_valid, i.indisready AS is_ready,
                   pg_get_expr(i.indpred, i.indrelid) AS predicate,
                   i.indoption::int2[] AS options,
                   ARRAY(
                     SELECT pg_get_indexdef(i.indexrelid, key_no, true)
                       FROM generate_series(1, i.indnkeyatts) AS key_no
                   ) AS keys
              FROM pg_class idx
              JOIN pg_index i ON i.indexrelid=idx.oid
              JOIN pg_class tbl ON tbl.oid=i.indrelid
             WHERE idx.relname=%s
               AND idx.relnamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())
            """,
            (name,),
        )
        rows = cur.fetchall()
        if len(rows) != 1:
            blockers.append(f"index_count:{name}:{len(rows)}")
            continue
        table, unique, valid, ready, predicate, options, keys = _row_values(
            rows[0], "table_name", "is_unique", "is_valid", "is_ready",
            "predicate", "options", "keys",
        )
        option_values = list(options or ())
        actual_keys = tuple(
            _norm(key) + (" DESC" if pos < len(option_values) and (int(option_values[pos]) & 1) else "")
            for pos, key in enumerate(keys or ())
        )
        expected_keys = tuple(_norm(key) for key in expected.keys)
        if (
            table != expected.table or bool(unique) != expected.unique
            or not valid or not ready or actual_keys != expected_keys
            or _norm(predicate) != _norm(expected.predicate)
        ):
            blockers.append(
                f"wrong_index:{name}:{table}:{unique}:{valid}:{ready}:{actual_keys}:{predicate}"
            )
    return blockers


def assert_schema_ready(cur) -> None:
    blockers = schema_blockers(cur)
    if blockers:
        raise RuntimeError("GEO_ARTICLE_V14_SCHEMA_NOT_READY|" + "|".join(blockers))
