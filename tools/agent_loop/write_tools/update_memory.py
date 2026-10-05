"""Write canonical profile memory through the reviewed memory ledger."""

from __future__ import annotations

from typing import Any

from db.profile_memory_db import record_profile_memory_event, review_profile_memory_event


async def update_profile_memory(
    *,
    profile_id: str,
    concept: str,
    text: str,
    source: str,
    supersedes_event_id: str | None = None,
    confidence: float = 0.7,
    ctx_user_id: int | None = None,
) -> dict[str, Any]:
    review_status = "pending" if source == "ai_inferred" else "auto"
    event_id = record_profile_memory_event(
        profile_id=profile_id,
        source="agent_loop",
        event_type="memory_update",
        title=_title_for_concept(concept),
        text=text,
        dimension=concept,
        canonical_concept=concept,
        raw_payload={"tool": "update_profile_memory", "source": source, "supersedes_event_id": supersedes_event_id},
        confidence=confidence,
        user_id=ctx_user_id,
        review_status=review_status,
    )
    archived = False
    if supersedes_event_id and source == "user_explicit":
        archived = review_profile_memory_event(
            int(supersedes_event_id),
            is_active=False,
            reviewed_by_user_id=ctx_user_id,
            review_status="dismissed",
            notes=f"superseded_by:{event_id}",
        )
    return {
        "status": "pending_confirm" if review_status == "pending" else "auto",
        "event_id": event_id,
        "review_status": review_status,
        "archived_superseded": archived,
    }


def _title_for_concept(concept: str) -> str:
    return {
        "business_identity": "业务身份",
        "target_customer": "目标客户",
        "offer_and_proof": "卖点与证明",
        "voice_style": "表达风格",
        "guardrails": "内容边界",
    }.get(concept, "客户记忆")
