# R6-H Deploy-CTO Pre-Deployment Runbook · 2026-06-20

## Status

State: pre-deployment design only.

This runbook does not authorize deployment, production flag enablement, customer
output, or production prompt replacement. It is a checklist for Deploy-CTO to
prepare and review a future shadow-only production pilot.

## Candidate Commit

Clean local branch:

- branch: `codex/r6h-shadow-seam-clean`
- commit: `eaa53346 feat(r6h): add shadow-only writing artifacts`

Included files:

- `api/writing_shadow_api.py`
- `frontend/src/components/WritingSettingsDialog.tsx`
- `server.py`
- `tests/api/test_writing_shadow_api.py`
- `tests/test_article_generator_shadow_seam.py`
- `tests/test_writing_settings_shadow_ui_contract.py`
- `tests/test_writing_shadow_only_seam.py`
- `writing/article_generator_service.py`
- `writing/shadow_only_injection.py`

Important exclusion:

- the unrelated `server.py` `api_start_articles` SQL hunk is not in this commit
- unrelated `tests/test_monitoring_optimize_start_articles_2026_06_12.py` is not in this commit
- `qa-artifacts/` is not part of the deployable code commit
- redline files are not touched: billing, auth, DB connection, JWT

## Production Reality

Current production image does not contain this R6-H code or seam hook. Therefore
setting `R6H_SHADOW_INJECTION_ENABLED=true` on the current production image is a
no-op.

A real pilot requires a new image that contains the candidate commit, plus
shared storage and monitoring. This runbook only designs that path.

## Non-Negotiables

- `R6H_CUSTOMER_OUTPUT_ENABLED=false`
- `R6H_SHADOW_ONLY=true`
- `R6H_SHADOW_INJECTION_ENABLED` remains off until Pilot Gate is passed
- default writing prompt and prompt overrides remain unchanged
- no DB migration
- no billing/auth/publish/cache changes
- no live semantic judge calls from the production writing path
- no customer-visible shadow artifact, sidecar, evidence binding, or article body
- recorder failures must never fail article generation

## Pre-Deployment Review Sequence

### 1. Build Candidate From A Production-Equivalent Base

Deploy-CTO should not deploy the current feature branch wholesale if it contains
unrelated finance/admin changes. The intended deployment unit is the clean R6-H
commit above.

Expected design step:

1. identify the production-equivalent base commit or branch
2. cherry-pick `eaa53346` onto that base in a temporary deployment branch
3. confirm the diff still contains only the 9 R6-H files listed above
4. confirm `server.py` contains only the `writing_shadow_api` router hunk
5. confirm no unrelated SQL, finance, billing, auth, cache, publish, or prompt changes

Production base for this review cycle: `83ec082d`. If production HEAD has
changed, stop and re-run the production state review before cherry-picking.

Expected safety check before cherry-pick: `eaa53346` is not an ancestor of the
current production commit. That means the candidate must be cherry-picked as a
single commit; the parent feature branch must not be deployed wholesale.

If cherry-pick reports conflicts, stop and return the conflict list. Do not
hand-resolve conflicts inside the deployment shell.

No image build or cutover is authorized by this step.

### 2. Provision Admin-Only Shared Artifact Root

Required production path:

```text
R6H_SHADOW_ARTIFACT_ROOT=/var/lib/omnirank/admin_shadow_runs
```

The path must be:

- absolute
- outside the repository checkout
- outside nginx/web roots
- outside customer upload/static/media/public roots
- mounted read/write into both blue and green containers at the same path
- readable by the admin-only listing/detail API

Blue/green requirement: active and standby must share the same artifact root.
Otherwise artifacts written before or during cutover may be invisible after
switching traffic.

### 3. Define Retention And Disk Guardrails

Before any production flag-on pilot:

- define retention by age, recommended first pilot: 7 to 14 days
- define retention by total size or max run count
- define cleanup behavior for unreadable or partial runs
- monitor disk usage of the artifact root
- define alert threshold before disk pressure can affect the host

R6-H run ids now include a UTC microsecond suffix plus a short random nonce, so
same quote/topic/article regenerations create separate runs instead of
colliding. Listing may show multiple live runs for the same logical article;
that is expected and should not be deduplicated during the first pilot.

### 4. Measure Recorder Latency Before Broad Flag-On

The recorder writes a small set of local files after article save. It must stay
observational.

Pilot review should capture:

- per-article recorder write latency p50/p95/p99
- recorder exception count
- article generation success rate with the flag on
- whether the async generation path sees visible latency

If p95 latency is visible to users, move recorder writes to a background
executor or queue before broadening the pilot.

### 5. Add Active Alerts

Required alerts before pilot:

- `customer_output_open_count > 0`: page immediately
- `recorder_error_count > 0` or recorder error rate above threshold
- unreadable manifest count above zero
- artifact root disk usage above threshold
- no new artifact observed during an enabled pilot window

The admin UI red badge is not sufficient as production monitoring.

The recorder emits admin-only structured error events into the artifact root so
the listing/observability API can expose a safe `recorder_error_count`. These
events do not include article body text or raw exception text and do not alter
live writing output.

### 6. Rehearse Rollback Before Flag-On

Kill switch:

```text
R6H_SHADOW_INJECTION_ENABLED=false
```

This flag is environment-based. Changing it requires updating the production
environment and restarting/reloading the app container for the process to see
the new value. It is not a DB-backed instant toggle.

Expected rollback behavior:

- article generation continues normally
- no new shadow artifact is written after flag-off
- existing artifacts remain on disk for review or cleanup
- customer output remains blocked
- default prompt remains unchanged

Rollback rehearsal must happen before any production pilot flag-on.

## First Pilot Gate

Production shadow recording may be enabled only after all are true:

- clean R6-H commit is applied to a production-equivalent image branch
- Deploy-CTO confirms the deploy diff is only R6-H
- shared admin-only artifact root is provisioned for blue and green
- `R6H_SHADOW_ARTIFACT_ROOT` is absolute and outside web/customer roots
- retention and cleanup plan is active
- disk, error, unreadable-manifest, and invariant alerts are active
- rollback has been rehearsed
- `R6H_CUSTOMER_OUTPUT_ENABLED=false`
- customer output and default prompt replacement remain explicitly out of scope

## Pilot Shape

First pilot should be time-capped, not full rollout:

- enable `R6H_SHADOW_INJECTION_ENABLED=true` for a short window
- generate or observe a small number of normal article saves
- verify artifacts appear under the shared root
- verify listing/detail/observability can read them
- verify `customer_output_open_count=0`
- verify no prompt, billing, publish, cache, or auth behavior changed
- disable the flag and verify no new artifact is written

## Commands Are Not Included Intentionally

This document intentionally avoids final production build, restart, nginx, or
flag mutation commands. Those commands belong in the final Deploy-CTO execution
runbook after this pre-deployment design is approved.

## Out Of Scope

- deploying the image
- opening production flags
- customer-visible article output
- replacing the default writing prompt
- running semantic judges from production writing
- using shadow observability as content approval
- exposing sidecar, evidence bindings, artifact paths, or full article body to customers
