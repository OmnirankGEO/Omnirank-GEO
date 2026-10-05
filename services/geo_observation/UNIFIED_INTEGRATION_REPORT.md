# GEO Observation vNext Unified Integration Report

Status: `coded + local verified`. The scheduler/billing convergence blocker is
closed in code. This package still requires final unified release verification and
is not production GO by itself.

## Source lineage

Common development base: `1fae96bdb6f4125c43fec892ffb8332c6ec8f897`

The unified branch contains all four selected inputs as Git ancestors:

1. AI-1 collection: `da431ceaf9986ea9529e8caff6021d3d7eb91a2e`
2. AI-2 governance: `ec5e60c44c1bbe36a7cad72dd07d781e3146e108`
3. AI-3 analytics/API: `c83b7473f5630b2520e8c9833b668e848991ed1c`
4. Frontend-A: `22564b3e76051a394d883df407a6cd0697545b7a`

Frontend-B is deliberately excluded.

## Integration decisions

- `services/geo_observation/integration.py` is the only cross-package adapter.
- AI-2's PostgreSQL policy remains the policy SSOT. The adapter validates its full
  shape before AI-1 consumes it.
- AI-1's database-backed budget ledger and reservation reaper are configured at
  startup. AI-3 receives real AI-1 health/readiness and the AI-2 policy provider.
- AI-2 promotion/reconciliation and AI-3 aggregation are registered through the
  existing ROLE=cron leader. The observation jobs themselves do not start a scheduler.
- AI-1 collection jobs are not registered yet. There is no audited production
  `ObservationSamplingDriver`, and registering a second collection path would risk
  duplicate provider calls and charges. Existing paid diagnosis, monitoring, and
  research producers remain authoritative; AI-2's reconciler imports their terminal
  records into the observation ledger.
- AI-2 governance routes and AI-3 product/admin routes are registered fail-fast.
  Route collision and RBAC precedence have explicit integration tests.

## Database ownership

The release has exactly one observation migration in the prestart manifest:

`scripts/migration_geo_observation_v1_2026_07_17.sql`

It owns the AI-2 observation tables plus the three integration tables required by
AI-1/AI-3:

- `geo_ai_surface_cost_ledger`
- `geo_ai_surface_cost_reservations`
- `geo_observation_insight_jobs`

The migration is additive and idempotent. It repairs compatible partial schemas and
fails closed when existing data cannot be converted without inventing business
values. Startup checks columns, types, lengths, nullability, defaults, constraints,
constraint validation, indexes, and index predicates.

## Frontend placement

- The selected Frontend-A workbench enhances the existing Monitoring data-insight
  tab; it does not add a second observation center.
- Diagnosis embeds the recommendation-behavior section.
- Admin receives `/admin/geo-observation-center`.
- Methodology is available at `/monitoring/methodology`.
- With `product_enabled=false`, Monitoring falls back to the existing Insights
  Center and Diagnosis hides the unavailable module. Dark deployment therefore
  does not replace working production screens with a 503 panel.

## Verification

- AI-1 collection suite with PostgreSQL: `207 passed`
- AI-2 governance suite: `148 passed`
- AI-3 analytics/API suite: `137 passed`
- Unified integration suite: `13 passed`
- Frontend-A Playwright: `59 passed / 0 failed / 0 skipped`
- Frontend-A contract check: 13 calls and 30 DTOs matched AI-3
- Frontend TypeScript, production builds, no-lookbehind and privacy checks: passed
- Disposable PostgreSQL: migration executed twice; default drift was rejected;
  rerunning the migration repaired it; startup verification then passed
- Python compile and `git diff --check`: passed
- Billing, connection, auth/JWT, wallet redlines and social territory: no diff
- Merge-overlap review found and fixed two cross-package contract gaps:
  1. AI-2 intentionally promotes public research without customer contributor
     buckets, while AI-3 had required a bucket for every public source. Public
     research is now included without weakening the one-vote bucket gate for paid
     diagnosis and monitoring; a real-PostgreSQL regression test locks the behavior.
  2. AI-2's reconciler previously imported terminal source rows even while
     `ingest_enabled=false`. The ingest gate now stops only new registration while
     withdrawals and retention anonymization continue to run.

## Monitoring scheduler P1 closure

Automatic monitoring now has one registration control plane in `api/scheduler.py`.
The root scheduler retains `job_daily_monitoring` only as an implementation/manual
compatibility entry and does not register it automatically.

Entitlement ownership is disjoint and fail-closed:

- Ownership is decided per purchased phrase, not per quote. Once a phrase has a
  keyword-subscription record, its current subscription state remains authoritative:
  active phrases use `monitoring_keyword_daily`; paused or cancelled phrases do not
  run and cannot fall through to another billing feature.
- Other phrases in the same paid quote can remain on the legacy quote schedule,
  preserving existing 6/12/24-hour delivery through `scheduled_monitoring` without
  running the subscribed phrase twice.
- Only phrases that never entered the keyword-subscription model can use the legacy
  quote schedule.

The PostgreSQL ownership regression creates legacy-only, subscription-only, paused,
cancelled, and mixed-entitlement quotes. It proves every runnable phrase has exactly
one owner and no phrase can feed both billing features.

Verification on a disposable PostgreSQL 16 instance:

- ownership and single-registration proof: `4 passed`;
- monitoring billing, subscription, service-anchor, renewal, scheduler, and regression
  matrix is rerun by Deploy on the final merged SHA with no related deselection;
- the stale tests that treated `confirmed` as an invalid service state were corrected
  to the current service-anchor SSOT. Their status-hook tests now execute without a
  database import and prove both `confirmed` and `paid` preserve existing subscriptions;
- paid monitoring registration failure is fatal for ROLE=cron, preventing a cron
  leader from starting while silently missing the delivery job;
- scheduler/observation integration contract checks: `26 passed`.

## Release and activation gates

The scheduler/billing P1 is closed in code and must be reverified by Deploy against
the final unified SHA. All observation feature flags remain false.

Full activation is still blocked until Deploy verifies all of the following:

1. A real audited sampling driver is connected, or an explicit architecture decision
   keeps existing collectors as the permanent source of truth.
2. Yuanbao and DeepSeek staging provider smoke tests pass with production-like keys.
3. `GEO_OBSERVATION_HMAC_KEY_V1` is configured consistently across workers.
4. Legal basis, consent version, retention policy, and withdrawal handling are signed.
5. The gold evaluation gate meets its evidence-derived thresholds.
6. Flags are opened in order: ingest, then promotion after legal/gold gates, then
   aggregation, then product after aggregates and readiness are observable.

Deploy must use the final unified SHA only. The four source packages must not be
deployed separately.
