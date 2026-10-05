"""GEO Observation Analytics (AI-3).

Owns: the aggregate repository/formula/refresh job, the deterministic metric
SSOT, private/public/admin product DTOs and the read-only product API for the
GEO unified observation flywheel.

Does NOT own: provider adapters/sampling (AI-1), observation/policy/aggregate
migration + readiness + privacy + promotion + audit (AI-2), billing, auth
middleware, server/scheduler wiring, or any frontend (Frontend-A/B candidates).

Reads only ``processing_state='promoted'`` events joined to anonymized signals;
writes only its own ``geo_observation_aggregates`` rows (keyed idempotently by
``aggregate_key`` + versions). No LLM computes a metric; the semantic engine
only explains already-verified deterministic facts via official DeepSeek.
"""

from __future__ import annotations

from . import contract, metrics  # noqa: F401

__all__ = ["contract", "metrics"]
