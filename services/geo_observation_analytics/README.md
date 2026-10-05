# GEO Observation Analytics (AI-3) — package + integration guide

> Status: **coded + local verified**. NOT deployed, NOT migrated to prod, NOT flag-flipped, NOT pushed. AI-3 has no authority to declare a release GO.
> Branch `feat/geo-observation-analytics-2026-07-17` @ worktree `C:\AI-Test\omnirank-ai-geo-observation-analytics`.
> Base `DEVELOPMENT_START_SHA=1fae96bd…` (docs-only contract pointer). Ancestors proven: `SIGNED_RELEASE_BASE_SHA=6059b088…` and `CONTRACT_FREEZE_COMMIT=8daa1e53…`.

## What this package is

The unique aggregation backend + read-only product API for the GEO unified observation flywheel. It turns AI-2's anonymized promoted signals into explainable, actionable product data:

```
promoted events × anonymized signals
  → replayable aggregate (geo_observation_aggregates)
  → deterministic metrics / trend / stability / competition / source / opportunity
  → private (service-provider) / public (industry) / admin read-only DTOs
  → Frontend-A/B consume the same OpenAPI + fixture (they build the UI, not AI-3)
```

It does **not** own: provider adapters/sampling (AI-1); observation/policy/aggregate **migration** + readiness + privacy + promotion + audit (AI-2); billing; auth middleware/JWT; `server.py`; `api/scheduler.py`; `api/monitoring_api.py`; any frontend.

## Module map (`services/geo_observation_analytics/`)

| file | responsibility |
|---|---|
| `contract.py` | in-code SSOT for enums/versions/field-names/privacy lists; asserted against the frozen machine contract by `test_contract_lock.py`. |
| `metrics.py` | the ONLY deterministic metric formulas (rates, rank, SoV, volatility, model-shift TVD, trend confidence, stability, confirmed-change). Integer round-half-up bps. |
| `aggregates.py` | fetch promoted-only observations → grouping-set cells → `_compute_cell` → idempotent upsert keyed by `aggregate_key` + versions; per-brand cap; watermark no-regress; `refresh_scope` job entrypoint. |
| `repository.py` | parameterized read queries over aggregates + per-observation questions (`changed` via window fn) + admin overview inputs + public source patterns. |
| `trends.py` | period-over-period comparison (disallowed across model shift), model-shift markers, cross-bucket `confirmed_change`. |
| `opportunities.py` | deterministic rule-based content/media opportunities from aggregate metrics; `auto_action_allowed=False` always. |
| `privacy.py` | k-anonymity gate (raise-only thresholds) + private/public DTO leak guards. |
| `explain.py` | semantic insight engine: anonymized minimal fact pack → official DeepSeek (no fallback) → validated output; DB idempotency (20-concurrent = 1 job/1 model call). |
| `evidence.py` | private evidence resolution seam (owner+brand double-guarded; default reads `monitoring_results`). |
| `errors.py` | `{code,message}` error envelope byte-identical to the fixture `error_cases`. |
| `scripts/migration_geo_observation_aggregate_basis_2026_07_20.sql` | 唯一 analytics schema SSOT，包括 `geo_observation_insight_jobs`；不再维护重复 reference DDL。 |
| `contracts/` | vendored frozen contracts (hash-locked) + **vendored AI-2 real migration** `ai2_migration_geo_observation_v1_2026_07_17.sql` (tests bind to THIS and compare it with a pinned AI-2 committed blob; the unified release must bump the pin after AI-2's next clean commit) + generated `openapi_geo_observation_product_v1.json`. |

> **Schema binding**: AI-3 reads only columns that exist in AI-2's real migration. `search_enabled` is on the *event* (not the signal); `source_visibility` / `evidence_coverage` are DERIVED (`source_count>0` / `quality_score_bps>=5000 OR citation_count>0`); `source_domains` objects use a `type` key. Public rates are `effective_weight`-weighted with a per-brand cap (I8); private rates are raw counts. `refresh_scope` runs under a per-bucket advisory lock in a single transaction.

`schemas/geo_observation_product.py` — audience-separated Pydantic DTOs (`extra='forbid'`).
`api/geo_observation_product_api.py` — `build_routers(deps)` → (product_router, admin_router).

## Metric SSOT (part of `metric_version = geo-observation-metrics-v1`)

- `valid_observations` = the 8 valid outcomes (excludes `entity_ambiguous` + `engine_error`; UNKNOWN never enters a denominator).
- `presence_rate` = (recommended+conditionally_recommended+candidate_only+mentioned_only)/valid.
- Each outcome rate = its count / valid; `refusal_rate` = (refused_no_evidence+refused_risk)/valid (display aggregate; the two are tracked separately).
- `citation_rate`/`source_visibility_rate`/`evidence_coverage_rate` counted separately over valid (fallback sources never enter `citation_rate`).
- rank Top1⊆Top3⊆Top5 cumulative; `avg_position_milli` over positioned observations only (None otherwise).
- `share_of_voice` = presence / (presence + Σcompetitor_count); None when no appearances.
- `volatility` = presence-sequence flip rate; `model_shift_index` = TVD of outcome distributions across the newest model_revision boundary (0 when a side is empty); `trend_confidence` = bounded product of sample/source/span/stability (+brand independence for public).
- `stability` priority: insufficient > shifted > watch > stable. Single-bucket `confirmed_change` is always False (no "improved/declined" on one bucket).
- All `*_bps` are integers 0..10000; `None` is preserved, never coerced to 0.

## Privacy postures

- **private** DTO: may carry `brand_id` (owner's own brand); never `owner_user_id`/upstream/cost/trace/source-PK/HMAC bucket. Guarded by `privacy.assert_no_private_leak`.
- **public** DTO: additionally forbids `brand_id`/`aggregate_key`. Guarded by `privacy.assert_no_public_leak`. Gated by k-anonymity (valid≥10, indep user≥3, indep brand≥3, source types≥2) with an explicit `insufficient_samples` state — never a silent fallback to a wider industry/window.
- **admin** DTO: traceable (event/state counts, policy version, model shifts) but no secrets, no other-customer raw content, no provider cost/trace.

## Semantic engine (official DeepSeek only)

`provider=deepseek`, `base_url=https://api.deepseek.com`, `model=deepseek-v4-flash`, `thinking=disabled`, key via the existing `pick_deepseek_api_key` pool. **No DashScope fallback** (the existing `call_llm_with_fallback` silently degrades to DashScope and is deliberately NOT used — see residual R7). The model receives an anonymized minimal fact pack (zero tenant/upstream/cost/trace/raw-text — enforced by `_assert_pack_is_anonymous` and asserted by the request-body interception test). Idempotent by `input_hash` (browsing/poll never call the model); 20 concurrent generates → one job, one model call (`ON CONFLICT (input_hash) DO NOTHING`). Failure → job `failed` / HTTP 503 `SEMANTIC_INSIGHT_UNAVAILABLE`, no hardcoded fallback, no retry loop. Output is constrained to the provided evidence refs + allowed actions (the model cannot invent actions/refs).

## Integration wiring (final integrator ONLY — AI-3 does not apply any of this)

1. **Migration** (AI-2 + integrator): `scripts/migration_geo_observation_v1_2026_07_17.sql` 与 `scripts/migration_geo_observation_aggregate_basis_2026_07_20.sql` 是唯一可执行 schema SSOT；后者同时管理 additive `geo_observation_insight_jobs`。
2. **Route mapping** (`auth/module_mapping.py`): add `("/api/geo-observation/", None)` to `ROUTE_PREFIX_MAP` (auth required, no module perm, endpoint self-enforces brand scope / k-anon — same posture as `/api/marketing/`, `/api/pricing/`). The admin prefix needs no entry (admin short-circuit; non-admin correctly `403 UNMAPPED_ROUTE`).
3. **Router registration** (`server.py`, existing try/except idiom, fail-fast — never `except: pass`):
   ```python
   from api.geo_observation_product_api import build_routers, default_deps
   _obs_product, _obs_admin = build_routers(default_deps())
   app.include_router(_obs_product)
   app.include_router(_obs_admin)
   ```
4. **Dependency injection** — override `default_deps()` fields at integration:
   - `platform_health_provider` → AI-1 read-only health DTO rows (`platform_key` + `surface_key` + `status`, admin-only surface/provider). Readiness matches these to the policy-**selected** `(platform_key, surface_key)` pair exactly. Default `[]` = degraded.
   - `policy_provider` → AI-2 policy read: `{policy_version, policy:{platforms:[{platform_key, surface_key, enabled, ...}], ...}, effective_flags, ...}` (default `{policy_version: None}` = fail-closed). Each enabled platform's `surface_key` is the selected surface.
   - `collection_readiness_provider` → AI-1 **overall** control-plane readiness. Compose it from `services/ai_surface_monitoring/scheduler_wiring.check_readiness()` (`{ready, problems}` — sampling driver + reservation reaper wired) and `registry.readiness(snapshot)` (`{status: ready|attention_required|blocked, ...}` — surface substitution / adapter registration), taking the **worst**. AI-3 consumes it fail-closed (default `None` = **unavailable**): unwired / exception / `blocked` → `unavailable`; `attention_required` → `attention_required`; a `ready:False` flag never reads `ready`. Folded into admin readiness and surfaced as `AdminOverviewDTO.collection_readiness`.
   - `evidence_source` → confirm AI-2's `source_record_id` semantics; the default `SourceTableEvidenceResolver` assumes `source_record_id == monitoring_results.id` for `recurring_monitoring` and returns no fabricated text for other sources (≤10-line seam to extend to `paid_diagnosis`/`research_round`).
5. **Scheduler** (`api/scheduler.py`, ROLE=cron, single control plane): register a pure job calling `aggregates.refresh_scope(conn, scope_type=…, granularity=…, day=…, config=AggregationConfig(policy_version=<live>))` per scope/granularity, wrapped in the existing `sched_claim` / leader contract. `refresh_scope` is idempotent (blue-green overlap safe). Do NOT double-trigger with root `scheduler.py`.
6. **Go-live flags** (deploy): `GEO_OBSERVATION_AGGREGATION_ENABLED` gates the refresh job; `GEO_OBSERVATION_PRODUCT_ENABLED` gates route exposure; `GEO_OBSERVATION_SEMANTIC_*` set the DeepSeek identity. AI-3 owns none of these switches.

## Residual risks (not closed by this package)

- **R1** Evidence text for `paid_diagnosis`/`research_round` is a documented seam (default: no text → `evidence_available=false`, `question=""`). The `recurring_monitoring` path assumes `source_record_id == monitoring_results.id`; confirm AI-2's registration semantics before go-live.
- **R2** `geo_observation_insight_jobs` is an additive table this package needs; the integrator must ship it in the migration (schema provided). It is not an observation business table and stores no raw tenant text.
- **R3** `admin/aggregate-diff` returns an explicit empty note until the legacy `geo_engine_stats` 口径 mapping is wired at integration (no fabricated shadow comparison).
- **R4** `platform_health` / `policy_version` are injected (AI-1/AI-2); until wired they degrade to empty / null, never fabricated.
- **R5** `aggregates.py` materializes a window's promoted rows into Python (bounded per scope-cell). Fine for shadow v1; push down to SQL if volume grows.
- **R6** Single-bucket `confirmed_change` is always False by design; cross-bucket confirmation is on the trend endpoint.
- **R7** `OfficialDeepSeekTransport` deliberately bypasses the existing `call_llm_with_fallback` (which silently degrades to DashScope). There is no existing official-only helper — flagged interface gap; this package reuses the existing key pool, not a new key system.
- **R8** Metric thresholds/weights are part of `metric_version` and are shadow-only; final calibration by real replay is required before switching default read (per master spec §4 / §8.3).
- **R9** Public source-pattern aggregation assumes AI-2 populates `signals.source_domains` as `[{domain, stype|type}]`.
