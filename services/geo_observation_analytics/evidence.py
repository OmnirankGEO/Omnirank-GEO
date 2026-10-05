"""Private evidence resolution seam.

The per-observation question text and answer excerpt are the customer's OWN
private content; they live in the source business tables (never in the
anonymized public signals). This module is the INTEGRATION SEAM: the API depends
on an ``EvidenceSource`` protocol, and ships a default resolver for the primary
``recurring_monitoring`` source. It always double-guards on
``owner_user_id + brand_id`` so it can never surface another tenant's content.

The default resolver only reads a fixed whitelist of source tables/columns; the
table name is never interpolated from data. ``paid_diagnosis`` and
``research_round`` resolution are documented integration TODOs (they degrade to
``evidence_available`` from the signal rather than fabricating text).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from psycopg2.extras import RealDictCursor


@dataclass(frozen=True)
class SourceRef:
    observation_id: str
    source_type: str
    source_table: str
    source_record_id: str
    source_subkey: str
    platform_key: Optional[str]
    surface_key: Optional[str]


@dataclass(frozen=True)
class ResolvedEvidence:
    question: str
    answer_excerpt: Optional[str]
    matched_text: Optional[str]


class EvidenceSource(Protocol):
    def resolve_batch(
        self, conn, owner_user_id: int, brand_id: int, refs: list[SourceRef]
    ) -> dict[str, ResolvedEvidence]:  # pragma: no cover - protocol
        ...

    def resolve_one(
        self, conn, owner_user_id: int, brand_id: int, ref: SourceRef
    ) -> Optional[ResolvedEvidence]:  # pragma: no cover - protocol
        ...


class SourceTableEvidenceResolver:
    """Default resolver. Reads the owner's OWN monitoring records for
    recurring_monitoring; degrades safely for other sources (no fabrication)."""

    _MAX_EXCERPT = 400

    def resolve_one(
        self, conn, owner_user_id: int, brand_id: int, ref: SourceRef
    ) -> Optional[ResolvedEvidence]:
        if ref.source_type == "recurring_monitoring":
            return self._resolve_monitoring(conn, owner_user_id, brand_id, ref)
        # paid_diagnosis / research_round: integration TODO — no fake text.
        return None

    def resolve_batch(
        self, conn, owner_user_id: int, brand_id: int, refs: list[SourceRef]
    ) -> dict[str, ResolvedEvidence]:
        out: dict[str, ResolvedEvidence] = {}
        for ref in refs:
            resolved = self.resolve_one(conn, owner_user_id, brand_id, ref)
            if resolved is not None:
                out[ref.observation_id] = resolved
        return out

    def _resolve_monitoring(
        self, conn, owner_user_id: int, brand_id: int, ref: SourceRef
    ) -> Optional[ResolvedEvidence]:
        # source_record_id is expected to be a monitoring_results.id (string).
        try:
            record_id = int(ref.source_record_id)
        except (TypeError, ValueError):
            return None
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # brand_id guard via monitoring_tasks join so we never read another
            # tenant's monitoring row even if a stale ref is passed in.
            try:
                from services.monitoring_identity_review import aggregate_eligible_sql
                cur.execute(
                    f"""
                    SELECT r.keyword AS keyword, r.full_response AS full_response,
                           r.response_snippet AS response_snippet, r.mention_type AS mention_type
                    FROM monitoring_results r
                    JOIN monitoring_tasks t ON t.id = r.task_id
                    WHERE r.id = %s AND t.brand_id = %s
                      AND {aggregate_eligible_sql('r')}
                    """,
                    (record_id, brand_id),
                )
                row = cur.fetchone()
            except Exception:
                return None
        if not row:
            return None
        answer = row.get("full_response") or row.get("response_snippet")
        excerpt = None
        if answer:
            excerpt = answer[: self._MAX_EXCERPT]
        return ResolvedEvidence(
            question=row.get("keyword") or "",
            answer_excerpt=excerpt,
            matched_text=row.get("mention_type") or None,
        )
