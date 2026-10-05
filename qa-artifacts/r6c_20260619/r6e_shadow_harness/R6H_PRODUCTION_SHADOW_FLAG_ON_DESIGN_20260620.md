# R6-H Production Shadow Flag-On Design · 2026-06-20

## Status

State: design only. Not deployed. Not production-verified.

This design authorizes neither customer output nor production prompt
replacement. It only describes the conditions for enabling
`R6H_SHADOW_INJECTION_ENABLED=true` in production as a shadow-only recorder.

## Production Reality Check

Deploy-CTO's read-only production review found that the current production
image does not contain the R6-H shadow module or the article-generation seam
hook. No `R6H_*` environment variables are configured either.

Therefore, setting `R6H_SHADOW_INJECTION_ENABLED=true` on the current
production image would be a no-op. A real production pilot requires this
sequence:

1. Deploy an image that contains the shadow recorder module and seam hook.
2. Configure a shared admin-only artifact volume for blue and green.
3. Set `R6H_SHADOW_ARTIFACT_ROOT` to that shared absolute path.
4. Install retention, disk, error, and invariant alerts.
5. Rehearse rollback.
6. Only then enable `R6H_SHADOW_INJECTION_ENABLED=true` for a short pilot.

`R6H_CUSTOMER_OUTPUT_ENABLED` must remain false throughout this sequence.

## Non-Negotiables

- `R6H_CUSTOMER_OUTPUT_ENABLED=false`
- `R6H_SHADOW_ONLY=true`
- Default prompt and writing style stay unchanged.
- Shadow recorder output is admin-only and never customer-visible.
- No DB migration, no billing/auth/publish/cache changes, no live semantic judge.
- The recorder must not block article generation; recorder failures remain
  best-effort observability failures.

## Detail Body Visibility Decision

Production flag-on must use the stricter detail policy:

- `no_evidence` and unknown evidence modes do not expose sentence summaries.
- Detail keeps only article hash, character count, blockers, review summaries,
  source summary, and observability counters.
- `with_evidence` may keep bounded sentence excerpts only for admin evidence
  review because sidecar bindings need a sentence-level review surface.

Rationale: local smoke proved the detail path can read live shadow artifacts,
but no-evidence live artifacts have no evidence binding value. Exposing their
sentence summaries mainly exposes article body content, so it is redacted.

## Required Production Configuration

Set an absolute, admin-only, non-web-served root:

```text
R6H_SHADOW_ARTIFACT_ROOT=/var/lib/omnirank/admin_shadow_runs
```

The path must be outside:

- the repository checkout
- nginx/web roots
- customer file roots
- public/static/media directories

Blue/green requirement: both active and standby app containers must read and
write the same admin-only volume, or the active instance's API will not see
artifacts written by the other instance during cutover.

The expected deployment shape is a shared host bind mount or equivalent volume
mounted into both blue and green at the same path:

```text
/var/lib/omnirank/admin_shadow_runs:/var/lib/omnirank/admin_shadow_runs:rw
```

If the variable is omitted, the recorder falls back to a relative path under
the app working directory, which is not acceptable for production.

## Retention And Disk

Before enabling production shadow recording:

- set retention by age, for example 7-14 days for first pilot
- set retention by count or total size
- monitor free disk
- define behavior for duplicated run ids: skip existing run and log, do not
  overwrite by default

Live recorder run ids include quote/topic/article identifiers plus a UTC
timestamp and short nonce. Regeneration of the same article therefore creates a
new run instead of colliding with the previous directory.

Suggested first pilot:

- enable for a short window
- cap by time rather than customer count
- inspect disk growth after the pilot window

## Latency

The current recorder writes a small set of local files synchronously after the
article has been saved. Before any broad production flag-on:

- measure per-article recorder latency under production-like concurrency
- record p50/p95/p99 write time
- if p95 becomes visible to users, move file writing to a background executor
  or queue

The current local smoke only proves correctness, not production latency.

## Rollback

Kill switch:

```text
R6H_SHADOW_INJECTION_ENABLED=false
```

This flag is environment-based. Changing it requires updating the production
environment and restarting/reloading the app container for the process to see
the new value. It is not a DB-backed instant toggle.

Expected rollback behavior:

- new article generation stops recording shadow artifacts
- live article generation output remains unchanged
- existing artifacts remain on disk for admin review or cleanup
- customer output remains blocked

Rollback verification:

- generate one article after disabling the flag
- confirm no new shadow run appears
- confirm article generation still succeeds

## Alerts

Add active alerting before production flag-on:

- `customer_output_open_count > 0`: page immediately, this violates the shadow
  invariant
- `recorder_error_count > 0` or recorder exception/error rate above threshold
- artifact root disk usage above threshold
- unreadable manifest count above zero

The admin UI red badge is not enough for production monitoring.

The recorder emits admin-only structured error events into the artifact root so
the listing/observability API can expose a safe `recorder_error_count` without
returning article body text or raw exception text. Recorder errors remain
best-effort observability failures and must not block writing.

## First Production Pilot Gate

Production shadow flag-on is allowed only after all of these are true:

- Claude/Codex review passes this design
- absolute artifact root is configured
- root is confirmed admin-only and shared across blue/green
- retention/cleanup is defined
- disk and invariant alerts are active
- rollback command and verification are rehearsed
- production `R6H_CUSTOMER_OUTPUT_ENABLED` remains false

## Explicitly Out Of Scope

- customer-visible article output
- replacing the default writing prompt
- enabling customer output hard gate
- running live semantic judges from production writing path
- using shadow observability as quality approval
