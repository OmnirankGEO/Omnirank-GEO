-- Stage3 URL budget industry_name canonicalisation (2026-07-18)
--
-- The 2026-07-16 migration may already be recorded as applied, so changing
-- that file alone cannot upgrade a live database. This additive manifest entry
-- makes the upgrade explicit and idempotent. It never deletes or merges rows.

DO $$
BEGIN
    IF to_regclass('public.geo_research_url_budget') IS NULL THEN
        RAISE EXCEPTION
            'ABORT[stage3_url_budget_industry_name]: dependency table is missing';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM geo_research_url_budget
         WHERE industry_name IS NULL
    ) THEN
        IF EXISTS (
            SELECT 1
              FROM geo_research_url_budget
             GROUP BY round_id, url_hash, COALESCE(industry_name, '')
            HAVING COUNT(*) > 1
        ) THEN
            RAISE EXCEPTION
                'ABORT[stage3_url_budget_industry_name]: NULL/empty collision requires manual review';
        END IF;

        UPDATE geo_research_url_budget
           SET industry_name = ''
         WHERE industry_name IS NULL;
    END IF;

    ALTER TABLE geo_research_url_budget
        ALTER COLUMN industry_name SET DEFAULT '',
        ALTER COLUMN industry_name SET NOT NULL;
END $$;
