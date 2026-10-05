"""Machine contract for the reversible quote-to-publication article sidecar.

This module deliberately contains no database writes.  It freezes the production
facts signed by Deploy-CTO and supplies deterministic source-version helpers used
by the outbox, reconciler and shadow compiler.  A value absent from this contract
is not a supported event source.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
import unicodedata
from typing import Any, Final, Mapping, Sequence


CONTRACT_VERSION: Final = "geo-article-closed-loop-v1.1"
SOURCE_SERIALIZATION_VERSION: Final = "geo-article-source-json-v1"
SIGNED_RELEASE_BASE_SHA: Final = "95b1f3b2eef4fc2bc37dc1035c6a56b7b0ec33e1"
PRODUCTION_READBACK_VERSION: Final = "deploy-readback-2026-07-20"
COMPILER_VERSION: Final = "geo-article-plan-compiler-v1"
SLOT_EVENT_VERSION: Final = "geo-article-slot-events-v1"


@dataclass(frozen=True)
class EventSourceContract:
    event_kind: str
    source_kind: str
    routes: tuple[str, ...]
    authority_tables: tuple[str, ...]
    authority_predicate: str
    source_identity_fields: tuple[str, ...]
    source_version_fields: tuple[str, ...]
    collection_sort_fields: tuple[str, ...] = ()


EVENT_SOURCES: Final[dict[str, EventSourceContract]] = {
    "quote_paid_standard": EventSourceContract(
        event_kind="quote_paid_standard",
        source_kind="keyword_selection_session",
        routes=("POST /keyword-selection/{token}/mark-paid",),
        authority_tables=("keyword_selection_sessions", "quotes", "confirmed_keywords"),
        authority_predicate=(
            "session.status='active' AND quote.status='confirmed' "
            "AND quote.service_status='active' AND confirmed keyword snapshot is complete"
        ),
        source_identity_fields=("session_id", "quote_id"),
        source_version_fields=(
            "session_id", "quote_id", "owner_user_id", "brand_id", "session_status",
            "quote_status", "service_status", "confirmed_keywords",
        ),
        collection_sort_fields=("confirmed_keyword_id",),
    ),
    "quote_paid_offline": EventSourceContract(
        event_kind="quote_paid_offline",
        source_kind="quote_offline_activation",
        routes=("POST /api/quotes/{quote_id}/offline-mark-paid",),
        authority_tables=("quotes", "confirmed_keywords"),
        authority_predicate=(
            "quote.status IN ('paid','active') AND quote.service_status='active' "
            "AND confirmed keyword snapshot is complete"
        ),
        source_identity_fields=("quote_id",),
        source_version_fields=(
            "quote_id", "owner_user_id", "brand_id", "quote_status", "service_status",
            "source_type", "confirmed_keywords",
        ),
        collection_sort_fields=("confirmed_keyword_id",),
    ),
    "quote_paid_agent_activation": EventSourceContract(
        event_kind="quote_paid_agent_activation",
        source_kind="quote_agent_activation",
        # [开源 E3 · WO_323 G3a · 2026-10-02] 产生本事件的一键激活端点已删;存量 outbox 事件照旧按本契约消费
        routes=("retired producer: quote agent activation endpoint (WO_323 G3a)",),
        authority_tables=("quotes", "confirmed_keywords"),
        authority_predicate=(
            "quote.status IN ('paid','active') AND quote.service_status='active' "
            "AND service period and confirmed keyword snapshot are committed"
        ),
        source_identity_fields=("quote_id",),
        source_version_fields=(
            "quote_id", "owner_user_id", "brand_id", "quote_status", "service_status",
            "service_start_date", "service_end_date", "confirmed_keywords",
        ),
        collection_sort_fields=("confirmed_keyword_id",),
    ),
    "zero_price_writing_project_created": EventSourceContract(
        event_kind="zero_price_writing_project_created",
        source_kind="zero_price_writing_quote",
        routes=(
            # [开源 E3 · B3a] C 端计划建写作项目的端点已随 C 端下线删除;source_type 仍认 c_end_geo_plan(存量行)
            "POST /api/writing/projects/quick-create",
        ),
        authority_tables=("quotes", "confirmed_keywords"),
        authority_predicate=(
            "quote.source_type IN ('c_end_geo_plan','quick_writing') "
            "AND quote.status='confirmed' AND quote.paid_amount=0 "
            "AND quote.writing_status='pending' AND confirmed keyword snapshot is complete"
        ),
        source_identity_fields=("quote_id", "source_type"),
        source_version_fields=(
            "quote_id", "owner_user_id", "brand_id", "source_type", "quote_status",
            "paid_amount", "writing_status", "confirmed_keywords",
        ),
        collection_sort_fields=("confirmed_keyword_id",),
    ),
    "contract_add_on": EventSourceContract(
        event_kind="contract_add_on",
        source_kind="confirmed_keyword_contract_snapshot",
        routes=(
            # [开源 E3 · WO_323 G3a · 2026-10-02] 报价加词端点已删(存量事件照旧消费);写作大厅补词入口仍在役
            "POST /api/writing/projects/{quote_id}/add-keyword",
        ),
        authority_tables=("quotes", "confirmed_keywords"),
        authority_predicate="append/add-keyword transaction committed with a complete confirmed keyword snapshot",
        source_identity_fields=("quote_id",),
        source_version_fields=(
            "quote_id", "owner_user_id", "brand_id", "quote_status", "confirmed_keywords",
        ),
        collection_sort_fields=("confirmed_keyword_id",),
    ),
    "keyword_reassigned": EventSourceContract(
        event_kind="keyword_reassigned",
        source_kind="confirmed_keyword_contract_snapshot",
        # [开源 E3 · WO_323 G3a · 2026-10-02] 产生本事件的报价换词端点已删;存量 outbox 事件照旧按本契约消费
        routes=("retired producer: quote keyword replace endpoint (WO_323 G3a)",),
        authority_tables=("quotes", "confirmed_keywords"),
        authority_predicate="keyword replace transaction committed with a complete confirmed keyword snapshot",
        source_identity_fields=("quote_id",),
        source_version_fields=(
            "quote_id", "owner_user_id", "brand_id", "quote_status", "confirmed_keywords",
        ),
        collection_sort_fields=("confirmed_keyword_id",),
    ),
    "publication_locked": EventSourceContract(
        event_kind="publication_locked",
        source_kind="publication_fact",
        routes=("all canonical article publication fact writers",),
        authority_tables=(
            "mhz_publish_order_items", "media_publications", "publish_order_items", "publish_records",
        ),
        authority_predicate=(
            "explicit successful published status with article identity, platform, provider receipt, "
            "public URL and immutable first submitted-content snapshot; timestamps alone do not qualify"
        ),
        source_identity_fields=("publication_source", "publication_source_id"),
        source_version_fields=(
            "publication_source", "publication_source_id", "owner_user_id", "brand_id", "quote_id",
            "article_id", "status", "platform", "provider_receipt", "public_url",
            "submitted_content_hash", "published_at",
        ),
    ),
}


REMOVED_EVENT_KINDS: Final[frozenset[str]] = frozenset(
    {"quote_refund_terminal", "quote_cancel_terminal", "bonus_article_changed"}
)
EVENT_KINDS: Final[frozenset[str]] = frozenset(EVENT_SOURCES)

SLOT_STATES: Final[frozenset[str]] = frozenset({"active", "blocked", "cancelled", "superseded"})
SLOT_EVENT_KINDS: Final[frozenset[str]] = frozenset(
    {"created", "blocked", "unblocked", "cancelled", "superseded", "reassigned", "topic_linked", "article_linked", "publication_locked"}
)
QUESTION_SOURCE_TYPES: Final[frozenset[str]] = frozenset(
    {
        "purchased_monitoring_exact",
        "confirmed_current_proxy",
        "extra_current_proxy",
        "derived_content_question",
        "operator_override",
    }
)
QUESTION_RESOLUTION_STATUSES: Final[frozenset[str]] = frozenset(
    {"exact", "proxy", "ambiguous", "unresolved"}
)


FEATURE_FLAG_DEFAULTS: Final[dict[str, bool]] = {
    "ARTICLE_PLAN_READ_SUMMARY_ENABLED": False,
    "ARTICLE_PLAN_EVENT_OUTBOX_ENABLED": False,
    "ARTICLE_PLAN_RECONCILER_ENABLED": False,
    "ARTICLE_PLAN_SHADOW_ENABLED": False,
    "ARTICLE_PLAN_ADMIN_DIFF_UI_ENABLED": False,
    "ARTICLE_WRITING_SIMPLE_UI_ENABLED": False,
    "ARTICLE_PLAN_ASSISTED_METADATA_ENABLED": False,
    "ARTICLE_PLAN_CANARY_UPSERT_ENABLED": False,
    "ARTICLE_REVIEW_SHADOW_ENABLED": False,
    "ARTICLE_FLYWHEEL_CANDIDATE_ENABLED": False,
    "ARTICLE_FLYWHEEL_PROMOTION_ENABLED": False,
}
FLAG_DEPENDENCIES: Final[dict[str, frozenset[str]]] = {
    "ARTICLE_PLAN_RECONCILER_ENABLED": frozenset({"ARTICLE_PLAN_EVENT_OUTBOX_ENABLED"}),
    "ARTICLE_PLAN_SHADOW_ENABLED": frozenset(
        {"ARTICLE_PLAN_EVENT_OUTBOX_ENABLED", "ARTICLE_PLAN_RECONCILER_ENABLED"}
    ),
    "ARTICLE_PLAN_ASSISTED_METADATA_ENABLED": frozenset({"ARTICLE_PLAN_SHADOW_ENABLED"}),
    "ARTICLE_PLAN_CANARY_UPSERT_ENABLED": frozenset(
        {
            "ARTICLE_PLAN_SHADOW_ENABLED",
            "ARTICLE_PLAN_RECONCILER_ENABLED",
            "ARTICLE_PLAN_ASSISTED_METADATA_ENABLED",
        }
    ),
    "ARTICLE_PLAN_ADMIN_DIFF_UI_ENABLED": frozenset({"ARTICLE_PLAN_READ_SUMMARY_ENABLED"}),
    "ARTICLE_WRITING_SIMPLE_UI_ENABLED": frozenset({"ARTICLE_PLAN_READ_SUMMARY_ENABLED"}),
    "ARTICLE_FLYWHEEL_PROMOTION_ENABLED": frozenset({"ARTICLE_FLYWHEEL_CANDIDATE_ENABLED"}),
}

# Product/Review/Deploy have not signed numeric non-commercial canary thresholds or
# per-reason blocked SLAs.  Runtime must not invent them.  Commercial invariants are
# always zero-tolerance and are enforced separately.
CANARY_THRESHOLD_POLICY_VERSION: Final = "UNSIGNED"
# A signed policy must replace this with an immutable UTC epoch.  Quotes born
# before it are historical and can never be enrolled by the v1 canary endpoint.
CANARY_NEW_QUOTE_NOT_BEFORE: Final[datetime | None] = None
BLOCKED_SLA_POLICY_VERSION: Final = "UNSIGNED"

INTENTIONAL_ALWAYS_ON_SAFETY_CHANGES: Final[tuple[str, ...]] = (
    "minimum_invalid_outbound_content_gate",
    "leased_startup_writing_recovery_only",
)


def _canonical(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value).strip()
    if isinstance(value, int):
        return value
    if isinstance(value, (float, Decimal)):
        decimal_value = Decimal(str(value))
        if not decimal_value.is_finite():
            raise ValueError("non-finite numbers cannot be source-versioned")
        normalized = decimal_value.normalize()
        if normalized == normalized.to_integral():
            return int(normalized)
        return format(normalized, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=lambda item: str(item))}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical(item) for item in value]
    raise TypeError(f"unsupported canonical source value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def snapshot_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def build_source_version(event_kind: str, snapshot: Mapping[str, Any]) -> tuple[str, str]:
    """Return stable source_version and snapshot hash for an approved event kind."""
    contract = EVENT_SOURCES.get(event_kind)
    if contract is None:
        if event_kind in REMOVED_EVENT_KINDS:
            raise ValueError(f"removed event kind has no runtime source: {event_kind}")
        raise ValueError(f"unknown event kind: {event_kind}")
    missing = [field for field in contract.source_version_fields if field not in snapshot]
    if missing:
        raise ValueError(f"source snapshot missing fields for {event_kind}: {','.join(missing)}")
    selected = {field: snapshot[field] for field in contract.source_version_fields}
    digest = snapshot_hash(
        {"serialization_version": SOURCE_SERIALIZATION_VERSION, "event_kind": event_kind, "snapshot": selected}
    )
    return f"{SOURCE_SERIALIZATION_VERSION}:{digest}", digest


def build_event_key(event_kind: str, source_identity: Mapping[str, Any], source_version: str) -> str:
    contract = EVENT_SOURCES.get(event_kind)
    if contract is None:
        raise ValueError(f"event kind is not active: {event_kind}")
    missing = [field for field in contract.source_identity_fields if field not in source_identity]
    if missing:
        raise ValueError(f"event identity missing fields for {event_kind}: {','.join(missing)}")
    identity = {field: source_identity[field] for field in contract.source_identity_fields}
    return snapshot_hash(
        {
            "contract_version": CONTRACT_VERSION,
            "event_kind": event_kind,
            "source_identity": identity,
            "source_version": source_version,
        }
    )


def _env_bool(name: str) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return FEATURE_FLAG_DEFAULTS[name]
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def feature_flags() -> dict[str, bool]:
    return {name: _env_bool(name) for name in FEATURE_FLAG_DEFAULTS}


def feature_flag_blockers(
    flags: Mapping[str, bool] | None = None,
    *,
    schema_ready: bool,
    compiler_policy_signed: bool = False,
) -> list[str]:
    values = dict(flags or feature_flags())
    blockers: list[str] = []
    for name, dependencies in FLAG_DEPENDENCIES.items():
        if values.get(name):
            missing = sorted(dep for dep in dependencies if not values.get(dep))
            if missing:
                blockers.append(f"missing_flag_dependency:{name}:{','.join(missing)}")
    any_sidecar_usage = any(
        values.get(name, False)
        for name in (
            "ARTICLE_PLAN_READ_SUMMARY_ENABLED",
            "ARTICLE_PLAN_EVENT_OUTBOX_ENABLED",
            "ARTICLE_PLAN_RECONCILER_ENABLED",
            "ARTICLE_PLAN_SHADOW_ENABLED",
            "ARTICLE_PLAN_ADMIN_DIFF_UI_ENABLED",
            "ARTICLE_WRITING_SIMPLE_UI_ENABLED",
            "ARTICLE_PLAN_ASSISTED_METADATA_ENABLED",
            "ARTICLE_PLAN_CANARY_UPSERT_ENABLED",
            "ARTICLE_REVIEW_SHADOW_ENABLED",
            "ARTICLE_FLYWHEEL_CANDIDATE_ENABLED",
            "ARTICLE_FLYWHEEL_PROMOTION_ENABLED",
        )
    )
    if any_sidecar_usage and not schema_ready:
        blockers.append("closed_loop_schema_not_ready")
    if values.get("ARTICLE_PLAN_CANARY_UPSERT_ENABLED"):
        if not os.getenv("ARTICLE_PLAN_CANARY_ALLOWLIST", "").strip():
            blockers.append("canary_allowlist_empty")
        if CANARY_THRESHOLD_POLICY_VERSION == "UNSIGNED" or not compiler_policy_signed:
            blockers.append("canary_threshold_policy_unsigned")
        if CANARY_NEW_QUOTE_NOT_BEFORE is None:
            blockers.append("canary_new_quote_epoch_unsigned")
    if values.get("ARTICLE_FLYWHEEL_PROMOTION_ENABLED"):
        if CANARY_THRESHOLD_POLICY_VERSION == "UNSIGNED" or not compiler_policy_signed:
            blockers.append("flywheel_promotion_policy_unsigned")
    return blockers


def validate_machine_contract() -> None:
    if EVENT_KINDS & REMOVED_EVENT_KINDS:
        raise RuntimeError("active and removed event kinds overlap")
    if set(EVENT_SOURCES) != set(EVENT_KINDS):
        raise RuntimeError("event source map and event kind set differ")
    for event_kind, source in EVENT_SOURCES.items():
        if event_kind != source.event_kind:
            raise RuntimeError(f"event source key mismatch: {event_kind}")
        if not source.routes or not source.authority_tables or not source.authority_predicate:
            raise RuntimeError(f"event source is incomplete: {event_kind}")
        if not source.source_identity_fields or not source.source_version_fields:
            raise RuntimeError(f"event identity/version contract is incomplete: {event_kind}")
    unknown_dependencies = {
        dependency
        for dependencies in FLAG_DEPENDENCIES.values()
        for dependency in dependencies
        if dependency not in FEATURE_FLAG_DEFAULTS
    }
    if unknown_dependencies:
        raise RuntimeError(f"unknown feature flag dependencies: {sorted(unknown_dependencies)}")


validate_machine_contract()
