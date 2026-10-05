-- dedup_legacy_research_csv_rerun_2026_06_15.sql
-- Purpose:
--   Clean the accidental second run of scripts/migrate_legacy_csv_to_research_monitor.py.
--   Scope is intentionally narrow:
--     - prompts: only source='seed_history'
--     - round/citation rows: only round_id LIKE 'round_legacy_%'
--   Run first inside BEGIN ... ROLLBACK for row-count review, then COMMIT.

\set ON_ERROR_STOP on

BEGIN;

SELECT pg_advisory_xact_lock(hashtext('dedup_legacy_research_csv_rerun_2026_06_15'));

CREATE TEMP TABLE _legacy_prompt_keep AS
SELECT
    industry_id,
    prompt_text,
    MIN(id) AS keep_id,
    COUNT(*) AS duplicate_count
FROM geo_research_prompts
WHERE source = 'seed_history'
GROUP BY industry_id, prompt_text
HAVING COUNT(*) > 1;

CREATE TEMP TABLE _legacy_prompt_map AS
SELECT
    p.id AS duplicate_id,
    k.keep_id,
    p.industry_id,
    p.prompt_text
FROM geo_research_prompts p
JOIN _legacy_prompt_keep k
  ON k.industry_id = p.industry_id
 AND k.prompt_text = p.prompt_text
WHERE p.source = 'seed_history'
  AND p.id <> k.keep_id;

SELECT 'prompt_duplicate_groups' AS metric, COUNT(*) AS value
FROM _legacy_prompt_keep;

SELECT 'duplicate_prompts_to_remove' AS metric, COUNT(*) AS value
FROM _legacy_prompt_map;

-- 1) Remove duplicate citation rows created solely because the duplicate
--    prompt_id made the table UNIQUE key look different.
WITH ranked AS (
    SELECT
        c.id,
        ROW_NUMBER() OVER (
            PARTITION BY c.article_id, c.round_id, c.platform, p.industry_id, p.prompt_text
            ORDER BY c.id
        ) AS rn
    FROM geo_research_article_citations c
    JOIN geo_research_prompts p ON p.id = c.prompt_id
    WHERE c.round_id LIKE 'round_legacy_%'
      AND p.source = 'seed_history'
),
deleted AS (
    DELETE FROM geo_research_article_citations c
    USING ranked r
    WHERE c.id = r.id
      AND r.rn > 1
    RETURNING c.id
)
SELECT 'deleted_duplicate_citations' AS metric, COUNT(*) AS value
FROM deleted;

-- 2) Any surviving citation that still points at a duplicate prompt becomes
--    a canonical citation. This should normally be 0 after step 1, but keeps
--    the script safe for partial reruns.
WITH updated AS (
    UPDATE geo_research_article_citations c
       SET prompt_id = m.keep_id
      FROM _legacy_prompt_map m
     WHERE c.prompt_id = m.duplicate_id
     RETURNING c.id
)
SELECT 'updated_citation_prompt_ids' AS metric, COUNT(*) AS value
FROM updated;

-- 3) Deduplicate round_call rows by the business key actually used by the
--    legacy import: one prompt text × one platform × one round.
WITH ranked AS (
    SELECT
        rc.id,
        ROW_NUMBER() OVER (
            PARTITION BY rc.round_id, rc.industry_id, rc.prompt_text, rc.platform
            ORDER BY rc.id
        ) AS rn
    FROM geo_research_round_call rc
    WHERE rc.round_id LIKE 'round_legacy_%'
),
deleted AS (
    DELETE FROM geo_research_round_call rc
    USING ranked r
    WHERE rc.id = r.id
      AND r.rn > 1
    RETURNING rc.id
)
SELECT 'deleted_duplicate_round_calls' AS metric, COUNT(*) AS value
FROM deleted;

-- 4) Canonicalize any remaining round_call prompt_id references.
WITH updated AS (
    UPDATE geo_research_round_call rc
       SET prompt_id = m.keep_id
      FROM _legacy_prompt_map m
     WHERE rc.prompt_id = m.duplicate_id
     RETURNING rc.id
)
SELECT 'updated_round_call_prompt_ids' AS metric, COUNT(*) AS value
FROM updated;

-- 5) Remove duplicate prompt rows after child references no longer need them.
WITH deleted AS (
    DELETE FROM geo_research_prompts p
    USING _legacy_prompt_map m
    WHERE p.id = m.duplicate_id
      AND NOT EXISTS (
          SELECT 1
          FROM geo_research_article_citations c
          WHERE c.prompt_id = p.id
      )
    RETURNING p.id
)
SELECT 'deleted_duplicate_prompts' AS metric, COUNT(*) AS value
FROM deleted;

-- 6) Prevent this exact importer from creating duplicate seed prompts again.
CREATE UNIQUE INDEX IF NOT EXISTS idx_geo_research_prompts_seed_history_unique_text
    ON geo_research_prompts(industry_id, prompt_text)
    WHERE source = 'seed_history';

-- Legacy round calls have no table-level unique constraint in the current
-- schema. Keep this index scoped to historical import rounds so future reruns
-- can safely use ON CONFLICT DO NOTHING without changing live collection.
CREATE UNIQUE INDEX IF NOT EXISTS idx_geo_research_round_call_legacy_unique
    ON geo_research_round_call(round_id, industry_id, prompt_text, platform)
    WHERE round_id LIKE 'round_legacy_%';

-- Final review queries. Deploy should compare these with the expected
-- post-clean counts from the incident report.
SELECT 'after_prompts_seed_history' AS metric, COUNT(*) AS value
FROM geo_research_prompts
WHERE source = 'seed_history';

SELECT 'after_round_calls_legacy' AS metric, COUNT(*) AS value
FROM geo_research_round_call
WHERE round_id LIKE 'round_legacy_%';

SELECT 'after_citations_legacy' AS metric, COUNT(*) AS value
FROM geo_research_article_citations
WHERE round_id LIKE 'round_legacy_%';

SELECT 'remaining_seed_prompt_duplicates' AS metric, COUNT(*) AS value
FROM (
    SELECT industry_id, prompt_text
    FROM geo_research_prompts
    WHERE source = 'seed_history'
    GROUP BY industry_id, prompt_text
    HAVING COUNT(*) > 1
) d;

-- Dry-run default: leave this ROLLBACK for the first execution.
-- After Deploy confirms row counts, change only this line to COMMIT.
ROLLBACK;
