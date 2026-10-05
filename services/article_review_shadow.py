"""Best-effort review eligibility observations while the hard gate is off."""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from services.article_closed_loop_contract import feature_flags, snapshot_hash


logger = logging.getLogger("GEO-Article-Review-Shadow")
SHADOW_VERSION = "geo-article-review-shadow-v1"


def record_dispatch_review_shadow(
    *,
    article_id: int,
    dispatch_source: str,
    outgoing_content: str,
) -> dict[str, Any]:
    """Persist a deduplicated observation without changing dispatch outcome."""
    if not feature_flags()["ARTICLE_REVIEW_SHADOW_ENABLED"]:
        return {"recorded": False, "reason": "flag_disabled"}
    try:
        normalized_article_id = int(article_id or 0)
    except (TypeError, ValueError):
        normalized_article_id = 0
    if normalized_article_id <= 0:
        return {"recorded": False, "reason": "article_identity_missing"}

    from db.connection import get_connection
    from services.article_review_gate import evaluate_publication_eligibility

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT a.content,a.evidence_manifest_hash,
                   q.owner_user_id AS quote_owner_user_id,
                   b.owner_user_id AS brand_owner_user_id
              FROM articles a
              JOIN quotes q ON q.id=a.quote_id
              JOIN brands b ON b.id=q.brand_id
             WHERE a.id=%s AND COALESCE(b.is_deleted,FALSE)=FALSE
            """,
            (normalized_article_id,),
        )
        article = cur.fetchone()
        if not article:
            return {"recorded": False, "reason": "article_or_active_brand_not_found"}
        quote_owner = article.get("quote_owner_user_id")
        brand_owner = article.get("brand_owner_user_id")
        if quote_owner is not None and brand_owner is not None and int(quote_owner) != int(brand_owner):
            return {"recorded": False, "reason": "tenant_owner_mismatch"}
        eligibility = evaluate_publication_eligibility(
            normalized_article_id,
            cursor=cur,
            _evaluate_when_rollout_disabled=True,
        )
        canonical_hash = hashlib.sha256(str(article.get("content") or "").encode("utf-8")).hexdigest()
        outgoing_hash = hashlib.sha256(str(outgoing_content or "").encode("utf-8")).hexdigest()
        reason = str(eligibility.get("reason") or "unknown")[:80]
        payload = {
            "shadow_version": SHADOW_VERSION,
            "article_id": normalized_article_id,
            "dispatch_source": str(dispatch_source or "unknown")[:80],
            "eligible": bool(eligibility.get("eligible")),
            "reason": reason,
            "canonical_content_hash": canonical_hash,
            "outgoing_content_hash": outgoing_hash,
            "evidence_manifest_hash": article.get("evidence_manifest_hash"),
        }
        event_key = snapshot_hash(payload)
        cur.execute(
            """
            INSERT INTO geo_article_review_shadow_events (
                event_key,article_id,dispatch_source,eligible,reason,
                canonical_content_hash,outgoing_content_hash,evidence_manifest_hash
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (event_key) DO NOTHING
            RETURNING id
            """,
            (
                event_key,
                normalized_article_id,
                payload["dispatch_source"],
                payload["eligible"],
                reason,
                canonical_hash,
                outgoing_hash,
                article.get("evidence_manifest_hash"),
            ),
        )
        row = cur.fetchone()
        conn.commit()
        return {
            "recorded": bool(row),
            "eligible": payload["eligible"],
            "reason": reason,
            "event_key": event_key,
        }
    except Exception as exc:
        conn.rollback()
        logger.exception("review shadow write failed article=%s", normalized_article_id)
        return {"recorded": False, "reason": type(exc).__name__}
    finally:
        conn.close()
