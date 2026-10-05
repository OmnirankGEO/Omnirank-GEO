"""Versioned immutable brand-fact snapshot used by one generation request."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Final


BRAND_FACT_SNAPSHOT_VERSION: Final = "brand-fact-snapshot-v1.0"
_FACT_FIELDS: Final = (
    "company_intro", "unique_value", "service_area", "methodology",
    "core_selling_points", "case_studies", "pricing_tiers", "testimonials",
    "credentials",
)


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _json_safe(value: Any) -> Any:
    """Freeze DB-backed values into a JSON-native snapshot."""
    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            default=lambda item: item.isoformat()
            if isinstance(item, datetime)
            else str(item),
        )
    )


def build_brand_fact_snapshot(
    *,
    brand_id: int | None,
    brand_name: str,
    industry: str,
    client_materials: dict[str, Any] | None,
    diagnosis_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze current inputs without upgrading customer statements to truth."""
    materials = client_materials if isinstance(client_materials, dict) else {}
    source = str(materials.get("_material_source") or "current_profile")
    claims: list[dict[str, Any]] = []
    for field in _FACT_FIELDS:
        value = materials.get(field)
        if value in (None, "", [], {}):
            continue
        claims.append({
            "claim_id": f"BF-{len(claims) + 1:03d}",
            "field": field,
            "value": value,
            "provenance": "customer_provided",
            "verification_status": "customer_asserted",
            "source": source,
        })
    base = {
        "version": BRAND_FACT_SNAPSHOT_VERSION,
        "brand_id": brand_id,
        "brand_name": brand_name,
        "industry": industry,
        "source": source,
        "source_updated_at": materials.get("_confirmed_at") or materials.get("updated_at"),
        "claims": claims,
        "diagnosis_input_hash": _sha(diagnosis_data) if diagnosis_data else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "limitations": [
            "customer_asserted 只代表客户或代理当前提供的信息，不等于独立验证。",
            "外部事实必须另外绑定 Evidence ID；冲突时不得由写作模型自行覆盖。",
        ],
    }
    safe_base = _json_safe(base)
    safe_base["snapshot_hash"] = _sha(safe_base)
    return safe_base
