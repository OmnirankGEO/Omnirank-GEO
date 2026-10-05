"""Tenant-bound compatibility metadata for the GEO article delivery sidecar.

This module never creates titles or articles and never changes billing.  It only
binds an explicitly created legacy object to an already-existing delivery slot
for a quote that has been persistently enrolled in ``slot_aware_v1``.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from typing import Any, Mapping

from psycopg2.extras import Json

from services.article_closed_loop_contract import CONTRACT_VERSION, feature_flags, snapshot_hash


METADATA_VERSION = "geo-article-slot-metadata-v1"
QUESTION_RESOLVER_VERSION = "geo-article-question-resolver-v1"
WRITING_MODE = "slot_aware_v1"
logger = logging.getLogger("GEO-Article-Closed-Loop-Metadata")


class SlotMetadataUnavailable(RuntimeError):
    pass


def _row(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, Mapping):
        return dict(row)
    raise TypeError("mapping cursor required")


def quote_uses_slot_adapter(cur, quote_id: int) -> bool:
    cur.execute(
        "SELECT article_plan_writing_mode FROM quotes WHERE id=%s",
        (int(quote_id),),
    )
    row = _row(cur.fetchone())
    return row.get("article_plan_writing_mode") == WRITING_MODE


def enroll_new_quote_canary(cur, *, quote_id: int, actor_user_id: int) -> dict[str, Any]:
    """Persist sticky canary mode only after every signed admission gate passes."""
    from services import article_closed_loop_contract as contract

    flags = contract.feature_flags()
    blockers = contract.feature_flag_blockers(
        flags,
        schema_ready=True,
        compiler_policy_signed=contract.CANARY_THRESHOLD_POLICY_VERSION != "UNSIGNED",
    )
    if blockers:
        raise SlotMetadataUnavailable(f"canary_admission_blocked:{','.join(sorted(blockers))}")
    allowlist = {
        token.strip()
        for token in str(os.getenv("ARTICLE_PLAN_CANARY_ALLOWLIST", "")).replace(";", ",").split(",")
        if token.strip()
    }
    if f"quote:{int(quote_id)}" not in allowlist:
        raise SlotMetadataUnavailable("canary_quote_not_allowlisted")
    not_before = contract.CANARY_NEW_QUOTE_NOT_BEFORE
    if not_before is None:
        raise SlotMetadataUnavailable("canary_new_quote_epoch_unsigned")
    cur.execute(
        """
        SELECT q.created_at,q.article_plan_writing_mode,r.id AS run_id,r.verdict,
               r.comparison_snapshot,cr.id AS contract_revision_id
          FROM quotes q
          JOIN brands b ON b.id=q.brand_id AND COALESCE(b.is_deleted,FALSE)=FALSE
          JOIN LATERAL (
              SELECT * FROM geo_article_plan_runs
               WHERE quote_id=q.id AND status='completed'
               ORDER BY id DESC LIMIT 1
          ) r ON TRUE
          JOIN geo_article_contract_revisions cr ON cr.id=r.contract_revision_id
         WHERE q.id=%s
         FOR UPDATE OF q
        """,
        (int(quote_id),),
    )
    row = _row(cur.fetchone())
    if not row:
        raise SlotMetadataUnavailable("canary_quote_or_completed_shadow_missing")
    if row.get("article_plan_writing_mode") == WRITING_MODE:
        return {"enrolled": True, "already_enrolled": True, "run_id": int(row["run_id"])}
    created_at = row.get("created_at")
    if not isinstance(created_at, datetime):
        raise SlotMetadataUnavailable("canary_quote_created_at_unavailable")
    created_utc = created_at.replace(tzinfo=created_at.tzinfo or timezone.utc).astimezone(timezone.utc)
    policy_utc = not_before.replace(tzinfo=not_before.tzinfo or timezone.utc).astimezone(timezone.utc)
    if created_utc < policy_utc:
        raise SlotMetadataUnavailable("historical_quote_cannot_enter_canary")
    comparison = row.get("comparison_snapshot") or {}
    if isinstance(comparison, str):
        try:
            comparison = json.loads(comparison)
        except (TypeError, ValueError):
            comparison = {}
    if (
        row.get("verdict") != "PASS"
        or not isinstance(comparison, Mapping)
        or comparison.get("commercial_invariant_verdict") != "PASS"
        or comparison.get("canary_verdict") != "PASS"
    ):
        raise SlotMetadataUnavailable("canary_shadow_verdict_not_pass")
    cur.execute(
        """
        UPDATE quotes
           SET article_plan_writing_mode=%s,article_plan_enrolled_at=NOW(),
               article_plan_contract_version=%s,article_plan_enrolled_by=%s,
               article_plan_enrollment_run_id=%s
         WHERE id=%s AND article_plan_writing_mode IS NULL
        RETURNING id
        """,
        (WRITING_MODE, contract.CONTRACT_VERSION, int(actor_user_id), int(row["run_id"]), int(quote_id)),
    )
    if not cur.fetchone():
        raise SlotMetadataUnavailable("canary_quote_mode_conflict")
    return {
        "enrolled": True,
        "already_enrolled": False,
        "run_id": int(row["run_id"]),
        "contract_revision_id": int(row["contract_revision_id"]),
        "actor_user_id": int(actor_user_id),
    }


def _load_slot(cur, *, quote_id: int, keyword_id: int | None, require_unlinked: bool) -> dict[str, Any]:
    params: list[Any] = [int(quote_id)]
    where = ["s.quote_id=%s", "s.current_state='active'"]
    if keyword_id is not None:
        where.append("s.keyword_id=%s")
        params.append(int(keyword_id))
    if require_unlinked:
        where.append("s.topic_id IS NULL")
    cur.execute(
        f"""
        SELECT s.*,r.id AS latest_plan_run_id
          FROM geo_article_delivery_slots s
          LEFT JOIN LATERAL (
              SELECT id FROM geo_article_plan_runs
               WHERE contract_revision_id=s.contract_revision_id AND status='completed'
               ORDER BY id DESC LIMIT 1
          ) r ON TRUE
         WHERE {' AND '.join(where)}
         ORDER BY s.contract_ordinal,s.delivery_slot_key
         LIMIT 1
         FOR UPDATE OF s
        """,
        params,
    )
    slot = _row(cur.fetchone())
    if not slot:
        raise SlotMetadataUnavailable("no_available_delivery_slot")
    return slot


def _question_snapshot_for_slot(
    cur,
    *,
    slot: Mapping[str, Any],
    actor_user_id: int,
) -> int:
    """Resolve a typed question without ever overwriting the purchased phrase."""
    cur.execute(
        """
        SELECT q.owner_user_id AS quote_owner_user_id,b.owner_user_id AS brand_owner_user_id,
               q.brand_id,ck.id AS keyword_id,r.id AS contract_revision_id,
               r.source_version AS contract_source_version,r.authority_snapshot
          FROM quotes q
          JOIN confirmed_keywords ck ON ck.id=%s AND ck.quote_id=q.id
          JOIN brands b ON b.id=q.brand_id
          JOIN geo_article_contract_revisions r
            ON r.id=%s AND r.quote_id=q.id AND r.brand_id=q.brand_id
         WHERE q.id=%s AND COALESCE(b.is_deleted,FALSE)=FALSE
        """,
        (slot.get("keyword_id"), slot.get("contract_revision_id"), slot.get("quote_id")),
    )
    source = _row(cur.fetchone())
    if not source:
        raise SlotMetadataUnavailable("question_source_scope_mismatch")
    quote_owner = source.get("quote_owner_user_id")
    brand_owner = source.get("brand_owner_user_id")
    if quote_owner is not None and brand_owner is not None and int(quote_owner) != int(brand_owner):
        raise SlotMetadataUnavailable("question_source_tenant_mismatch")
    owner_user_id = quote_owner or brand_owner
    if owner_user_id is None:
        raise SlotMetadataUnavailable("question_source_owner_missing")
    if int(owner_user_id) != int(slot["owner_user_id"]) or int(source["brand_id"]) != int(slot["brand_id"]):
        raise SlotMetadataUnavailable("question_source_tenant_mismatch")

    authority_snapshot = source.get("authority_snapshot") or {}
    if isinstance(authority_snapshot, str):
        try:
            authority_snapshot = json.loads(authority_snapshot)
        except (TypeError, ValueError):
            authority_snapshot = {}
    authority_keywords = authority_snapshot.get("confirmed_keywords") if isinstance(authority_snapshot, Mapping) else None
    authority_matches = [
        item for item in (authority_keywords or [])
        if int(item.get("confirmed_keyword_id") or 0) == int(source["keyword_id"])
    ]
    if len(authority_matches) != 1:
        raise SlotMetadataUnavailable("question_contract_keyword_snapshot_missing_or_ambiguous")
    authority_keyword = authority_matches[0]
    purchased = str(authority_keyword.get("monitoring_query") or "").strip()
    if purchased:
        source_type = "purchased_monitoring_exact"
        resolution = "exact"
        text = purchased
    else:
        source_type = "confirmed_current_proxy"
        resolution = "proxy"
        text = str(authority_keyword.get("keyword") or "").strip()
    if not text:
        raise SlotMetadataUnavailable("question_text_unresolved")

    payload = {
        "tenant_owner_user_id": int(owner_user_id),
        "brand_id": int(slot["brand_id"]),
        "quote_id": int(slot["quote_id"]),
        "delivery_slot_key": str(slot["delivery_slot_key"]),
        "question_source_type": source_type,
        "source_object_type": "confirmed_keywords",
        "source_id": int(source["keyword_id"]),
        "contract_revision_id": int(source["contract_revision_id"]),
        "contract_source_version": str(source["contract_source_version"]),
        "source_text_snapshot": text,
        "resolution_status": resolution,
        "resolver_version": QUESTION_RESOLVER_VERSION,
    }
    digest = snapshot_hash(payload)
    question_key = snapshot_hash({"contract": CONTRACT_VERSION, "question": payload})
    cur.execute(
        """
        INSERT INTO geo_article_target_question_snapshots (
            question_key,tenant_owner_user_id,brand_id,quote_id,delivery_slot_key,
            question_source_type,source_object_type,source_id,source_text_snapshot,
            source_version,source_snapshot_hash,parent_source_object_type,derived_from_id,
            resolver_version,resolution_status,created_by
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (question_key) DO UPDATE SET question_key=EXCLUDED.question_key
        RETURNING id
        """,
        (
            question_key,
            payload["tenant_owner_user_id"],
            payload["brand_id"],
            payload["quote_id"],
            payload["delivery_slot_key"],
            source_type,
            "confirmed_keywords",
            payload["source_id"],
            text,
            f"{QUESTION_RESOLVER_VERSION}:{digest}",
            digest,
            "geo_article_contract_revisions",
            payload["contract_revision_id"],
            QUESTION_RESOLVER_VERSION,
            resolution,
            int(actor_user_id),
        ),
    )
    return int(_row(cur.fetchone())["id"])


def _append_slot_event(
    cur,
    *,
    slot: Mapping[str, Any],
    event_kind: str,
    actor_user_id: int,
    payload: dict[str, Any],
    topic_id: int | None = None,
    article_id: int | None = None,
    target_state: str | None = None,
) -> int:
    effective_state = target_state or str(slot["current_state"])
    next_version = int(slot.get("projection_version") or 0) + 1
    event_key = snapshot_hash(
        {
            "metadata_version": METADATA_VERSION,
            "delivery_slot_key": str(slot["delivery_slot_key"]),
            "slot_version": next_version,
            "event_kind": event_kind,
            "payload": payload,
        }
    )
    cur.execute(
        """
        INSERT INTO geo_article_delivery_slot_events (
            event_key,delivery_slot_key,slot_version,event_kind,target_state,
            contract_revision_id,plan_run_id,owner_user_id,brand_id,quote_id,
            actor_user_id,source_version,payload,occurred_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        ON CONFLICT (event_key) DO UPDATE SET event_key=EXCLUDED.event_key
        RETURNING id
        """,
        (
            event_key,
            slot["delivery_slot_key"],
            next_version,
            event_kind,
            effective_state,
            slot["contract_revision_id"],
            slot.get("latest_plan_run_id"),
            slot["owner_user_id"],
            slot["brand_id"],
            slot["quote_id"],
            int(actor_user_id),
            METADATA_VERSION,
            Json(payload),
        ),
    )
    event_id = int(_row(cur.fetchone())["id"])
    cur.execute(
        """
        UPDATE geo_article_delivery_slots
           SET projection_version=%s,current_event_id=%s,last_event_key=%s,current_state=%s,
               topic_id=COALESCE(%s,topic_id),article_id=COALESCE(%s,article_id),updated_at=NOW()
         WHERE delivery_slot_key=%s AND projection_version=%s
        """,
        (
            next_version,
            event_id,
            event_key,
            effective_state,
            topic_id,
            article_id,
            slot["delivery_slot_key"],
            slot["projection_version"],
        ),
    )
    if cur.rowcount != 1:
        raise SlotMetadataUnavailable("slot_projection_cas_conflict")
    return event_id


def set_slot_blocked(
    cur,
    *,
    delivery_slot_key: str,
    reason_code: str,
    user_message: str,
    owner_kind: str,
    next_action: str,
    target_resolution_at: Any,
    actor_user_id: int,
) -> dict[str, Any]:
    if not all(str(value or "").strip() for value in (reason_code, user_message, owner_kind, next_action)):
        raise SlotMetadataUnavailable("blocked_fields_incomplete")
    if target_resolution_at is None:
        raise SlotMetadataUnavailable("blocked_target_time_required")
    cur.execute(
        """
        SELECT s.*,e.plan_run_id AS latest_plan_run_id
          FROM geo_article_delivery_slots s
          JOIN geo_article_delivery_slot_events e ON e.id=s.current_event_id
         WHERE s.delivery_slot_key=%s
         FOR UPDATE OF s
        """,
        (delivery_slot_key,),
    )
    slot = _row(cur.fetchone())
    if not slot:
        raise SlotMetadataUnavailable("delivery_slot_not_found")
    if slot.get("completion_evidence"):
        raise SlotMetadataUnavailable("published_slot_cannot_be_blocked")
    payload = {
        "blocked_reason_code": reason_code,
        "blocked_user_message": user_message,
        "owner_kind": owner_kind,
        "next_action": next_action,
        "target_resolution_at": str(target_resolution_at),
    }
    next_version = int(slot.get("projection_version") or 0) + 1
    event_key = snapshot_hash(
        {
            "metadata_version": METADATA_VERSION,
            "delivery_slot_key": str(slot["delivery_slot_key"]),
            "slot_version": next_version,
            "event_kind": "blocked",
            "payload": payload,
        }
    )
    cur.execute(
        """
        INSERT INTO geo_article_delivery_slot_events (
            event_key,delivery_slot_key,slot_version,event_kind,target_state,
            contract_revision_id,plan_run_id,owner_user_id,brand_id,quote_id,
            actor_user_id,source_version,payload,occurred_at
        ) VALUES (%s,%s,%s,'blocked','blocked',%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        RETURNING id
        """,
        (
            event_key,slot["delivery_slot_key"],next_version,slot["contract_revision_id"],slot["latest_plan_run_id"],
            slot["owner_user_id"],slot["brand_id"],slot["quote_id"],int(actor_user_id),
            METADATA_VERSION,Json(payload),
        ),
    )
    event_id = int(_row(cur.fetchone())["id"])
    cur.execute(
        """
        UPDATE geo_article_delivery_slots
           SET current_state='blocked',projection_version=%s,current_event_id=%s,last_event_key=%s,
               blocked_reason_code=%s,blocked_user_message=%s,owner_kind=%s,next_action=%s,
               blocked_at=NOW(),target_resolution_at=%s,escalation_status='open',updated_at=NOW()
         WHERE delivery_slot_key=%s AND projection_version=%s
        """,
        (
            next_version,event_id,event_key,reason_code,user_message,owner_kind,next_action,
            target_resolution_at,delivery_slot_key,slot["projection_version"],
        ),
    )
    if cur.rowcount != 1:
        raise SlotMetadataUnavailable("slot_projection_cas_conflict")
    return {"blocked": True}


def unblock_slot(
    cur,
    *,
    delivery_slot_key: str,
    resolution_evidence: str,
    actor_user_id: int,
) -> dict[str, Any]:
    evidence = str(resolution_evidence or "").strip()
    if not evidence:
        raise SlotMetadataUnavailable("resolution_evidence_required")
    cur.execute(
        """
        SELECT s.*,e.plan_run_id AS latest_plan_run_id
          FROM geo_article_delivery_slots s
          JOIN geo_article_delivery_slot_events e ON e.id=s.current_event_id
         WHERE s.delivery_slot_key=%s
         FOR UPDATE OF s
        """,
        (delivery_slot_key,),
    )
    slot = _row(cur.fetchone())
    if not slot or slot.get("current_state") != "blocked":
        raise SlotMetadataUnavailable("blocked_slot_not_found")
    _append_slot_event(
        cur,
        slot=slot,
        event_kind="unblocked",
        actor_user_id=actor_user_id,
        target_state="active",
        payload={"resolution_evidence": evidence},
    )
    cur.execute(
        """
        UPDATE geo_article_delivery_slots
           SET blocked_reason_code=NULL,blocked_user_message=NULL,owner_kind=NULL,next_action=NULL,
               completion_evidence=completion_evidence,blocked_at=NULL,last_reminded_at=NULL,
               target_resolution_at=NULL,escalation_status='resolved',resolution_evidence=%s,updated_at=NOW()
         WHERE delivery_slot_key=%s AND current_state='active'
        """,
        (evidence, delivery_slot_key),
    )
    return {"unblocked": True}


def bind_topic_to_slot(
    cur,
    *,
    quote_id: int,
    topic_id: int,
    keyword_id: int | None,
    actor_user_id: int,
) -> dict[str, Any]:
    """Bind only sticky canary quotes; legacy and shadow-only quotes are untouched."""
    sticky = quote_uses_slot_adapter(cur, quote_id)
    if not sticky and not feature_flags()["ARTICLE_PLAN_ASSISTED_METADATA_ENABLED"]:
        return {"bound": False, "reason": "flag_disabled"}
    if not sticky:
        return {"bound": False, "reason": "legacy_quote"}
    slot = _load_slot(cur, quote_id=quote_id, keyword_id=keyword_id, require_unlinked=True)
    question_id = _question_snapshot_for_slot(cur, slot=slot, actor_user_id=actor_user_id)
    _append_slot_event(
        cur,
        slot=slot,
        event_kind="topic_linked",
        actor_user_id=actor_user_id,
        payload={"topic_id": int(topic_id), "question_snapshot_id": question_id},
        topic_id=int(topic_id),
    )
    cur.execute(
        """
        UPDATE topics
           SET delivery_slot_key=%s,plan_run_id=%s,target_question_snapshot_id=%s,
               article_plan_metadata_version=%s
         WHERE id=%s AND quote_id=%s AND delivery_slot_key IS NULL
           AND keyword_id IS NOT DISTINCT FROM %s AND article_id IS NULL
        """,
        (
            slot["delivery_slot_key"],
            slot.get("latest_plan_run_id"),
            question_id,
            METADATA_VERSION,
            int(topic_id),
            int(quote_id),
            keyword_id,
        ),
    )
    if cur.rowcount != 1:
        raise SlotMetadataUnavailable("topic_binding_scope_or_state_conflict")
    return {"bound": True, "delivery_slot_key": str(slot["delivery_slot_key"]), "question_snapshot_id": question_id}


def bind_article_to_topic(cur, *, topic_id: int, article_id: int, actor_user_id: int) -> dict[str, Any]:
    cur.execute(
        """
        SELECT s.*,t.target_question_snapshot_id,r.id AS latest_plan_run_id
          FROM topics t
          JOIN geo_article_delivery_slots s ON s.delivery_slot_key=t.delivery_slot_key
          JOIN articles a ON a.id=t.article_id AND a.topic_id=t.id AND a.quote_id=s.quote_id
          LEFT JOIN LATERAL (
              SELECT id FROM geo_article_plan_runs
               WHERE contract_revision_id=s.contract_revision_id AND status='completed'
               ORDER BY id DESC LIMIT 1
          ) r ON TRUE
         WHERE t.id=%s AND t.article_id=%s AND a.id=%s AND s.topic_id=t.id
           AND s.current_state='active'
         FOR UPDATE OF s
        """,
        (int(topic_id), int(article_id), int(article_id)),
    )
    slot = _row(cur.fetchone())
    if not slot:
        cur.execute(
            "SELECT q.article_plan_writing_mode,t.delivery_slot_key "
            "FROM topics t JOIN quotes q ON q.id=t.quote_id WHERE t.id=%s",
            (int(topic_id),),
        )
        topic_scope = _row(cur.fetchone())
        if topic_scope.get("article_plan_writing_mode") == WRITING_MODE:
            raise SlotMetadataUnavailable("slot_aware_article_binding_not_active")
        return {"bound": False, "reason": "legacy_or_unlinked_topic"}
    revision_key = snapshot_hash(
        {"metadata_version": METADATA_VERSION, "slot": str(slot["delivery_slot_key"]), "article_id": int(article_id)}
    )
    _append_slot_event(
        cur,
        slot=slot,
        event_kind="article_linked",
        actor_user_id=actor_user_id,
        payload={"article_id": int(article_id), "revision_key": revision_key},
        article_id=int(article_id),
    )
    cur.execute(
        """
        UPDATE articles
           SET delivery_slot_key=%s,article_revision_key=%s,target_question_snapshot_id=%s
         WHERE id=%s AND topic_id=%s AND delivery_slot_key IS NULL
        """,
        (
            slot["delivery_slot_key"],
            revision_key,
            slot.get("target_question_snapshot_id"),
            int(article_id),
            int(topic_id),
        ),
    )
    if cur.rowcount != 1:
        raise SlotMetadataUnavailable("article_binding_scope_or_state_conflict")
    return {"bound": True, "revision_key": revision_key}


def record_correction_signal(
    cur,
    *,
    quote_id: int,
    topic_id: int | None,
    article_id: int | None,
    before_text: str,
    after_text: str,
    correction_type: str,
    reason: str,
    actor_user_id: int,
) -> dict[str, Any]:
    """Write a tenant-scoped candidate only; no production strategy is changed."""
    if not feature_flags()["ARTICLE_FLYWHEEL_CANDIDATE_ENABLED"]:
        return {"recorded": False, "reason": "flag_disabled"}
    before_hash = hashlib.sha256(str(before_text).encode("utf-8")).hexdigest()
    after_hash = hashlib.sha256(str(after_text).encode("utf-8")).hexdigest()
    if before_hash == after_hash:
        return {"recorded": False, "reason": "unchanged"}
    cur.execute(
        """
        SELECT q.owner_user_id,q.brand_id,b.owner_user_id AS brand_owner,b.is_deleted,
               t.id AS resolved_topic_id,COALESCE(t.delivery_slot_key,a.delivery_slot_key) AS delivery_slot_key,
               a.id AS resolved_article_id,a.topic_id AS article_topic_id,a.article_revision_key
          FROM quotes q JOIN brands b ON b.id=q.brand_id
          LEFT JOIN topics t ON t.id=%s AND t.quote_id=q.id
          LEFT JOIN articles a ON a.id=%s AND a.quote_id=q.id
         WHERE q.id=%s
        """,
        (topic_id, article_id, int(quote_id)),
    )
    scope = _row(cur.fetchone())
    if not scope or bool(scope.get("is_deleted")):
        raise SlotMetadataUnavailable("correction_scope_unavailable")
    if topic_id is not None and int(scope.get("resolved_topic_id") or 0) != int(topic_id):
        raise SlotMetadataUnavailable("correction_topic_scope_mismatch")
    if article_id is not None and int(scope.get("resolved_article_id") or 0) != int(article_id):
        raise SlotMetadataUnavailable("correction_article_scope_mismatch")
    if topic_id is not None and article_id is not None and int(scope.get("article_topic_id") or 0) != int(topic_id):
        raise SlotMetadataUnavailable("correction_topic_article_mismatch")
    owner = scope.get("owner_user_id") or scope.get("brand_owner")
    if owner is None or (scope.get("owner_user_id") and scope.get("brand_owner") and int(scope["owner_user_id"]) != int(scope["brand_owner"])):
        raise SlotMetadataUnavailable("correction_tenant_mismatch")
    if not scope.get("delivery_slot_key"):
        return {"recorded": False, "reason": "closed_loop_lineage_missing"}
    revision_key = scope.get("article_revision_key") or snapshot_hash(
        {"quote_id": int(quote_id), "topic_id": topic_id, "article_id": article_id, "before_hash": before_hash}
    )
    payload = {
        "tenant_owner_user_id": int(owner),
        "brand_id": int(scope["brand_id"]),
        "quote_id": int(quote_id),
        "delivery_slot_key": str(scope["delivery_slot_key"]) if scope.get("delivery_slot_key") else None,
        "topic_id": topic_id,
        "article_id": article_id,
        "revision_key": revision_key,
        "before_hash": before_hash,
        "after_hash": after_hash,
        "correction_type": correction_type,
        "reason": str(reason or "agent_explicit_edit").strip(),
        "actor_user_id": int(actor_user_id),
    }
    source_version = f"correction-v1:{snapshot_hash(payload)}"
    correction_key = snapshot_hash({"candidate": payload, "source_version": source_version})
    cur.execute(
        """
        INSERT INTO geo_article_correction_signals (
            correction_key,tenant_owner_user_id,brand_id,quote_id,delivery_slot_key,
            topic_id,article_id,revision_key,before_hash,after_hash,correction_type,
            reason,actor_user_id,source_version,candidate_status,occurred_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'candidate',NOW())
        ON CONFLICT (correction_key) DO NOTHING RETURNING id
        """,
        (
            correction_key,
            payload["tenant_owner_user_id"],
            payload["brand_id"],
            payload["quote_id"],
            payload["delivery_slot_key"],
            topic_id,
            article_id,
            revision_key,
            before_hash,
            after_hash,
            correction_type,
            payload["reason"],
            actor_user_id,
            source_version,
        ),
    )
    row = cur.fetchone()
    return {"recorded": bool(row), "candidate_status": "candidate"}


def record_correction_signal_in_transaction_if_enabled(cur, **kwargs: Any) -> dict[str, Any]:
    """Best-effort correction seam; never poisons the legacy edit transaction."""
    if not feature_flags()["ARTICLE_FLYWHEEL_CANDIDATE_ENABLED"]:
        return {"recorded": False, "reason": "flag_disabled"}
    cur.execute("SAVEPOINT geo_article_correction_signal_sp")
    try:
        result = record_correction_signal(cur, **kwargs)
        cur.execute("RELEASE SAVEPOINT geo_article_correction_signal_sp")
        return result
    except Exception as exc:
        cur.execute("ROLLBACK TO SAVEPOINT geo_article_correction_signal_sp")
        cur.execute("RELEASE SAVEPOINT geo_article_correction_signal_sp")
        logger.exception("article correction candidate write failed without blocking the legacy edit")
        return {"recorded": False, "reason": type(exc).__name__}
