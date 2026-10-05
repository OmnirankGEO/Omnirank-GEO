"""Agent-facing writing-structure guidance API.

The endpoint is quote-scoped and read-only.  It returns only reviewed active
guidance for the quote industry, and never exposes shadow candidates or audits.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from db.diagnosis_db import get_quote
from services.writing_structure_guidance import build_structure_guidance_for_quote


router = APIRouter(prefix="/api/writing/projects", tags=["写作结构建议"])


@router.get("/{quote_id}/structure-guidance")
async def get_project_structure_guidance(quote_id: int, request: Request):
    from auth.brand_access import require_quote_access

    require_quote_access(request, quote_id)
    quote = get_quote(quote_id)
    if not quote:
        raise HTTPException(status_code=404, detail="报价单不存在")
    payload = build_structure_guidance_for_quote(dict(quote))
    payload["shadow_only"] = True
    payload["production_takeover"] = False
    return payload
