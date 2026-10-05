-- Compatibility rollback for the retail-SKU expansion.
-- No columns/data are dropped.  An old binary requires sku_template_id on every
-- row, therefore rollback fails closed when true template-free SKUs exist.

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM agent_sku_overrides WHERE sku_template_id IS NULL
    ) THEN
        RAISE EXCEPTION
            'rollback blocked: template-free retail SKUs require the decoupled binary';
    END IF;
END $$;

ALTER TABLE agent_sku_overrides ALTER COLUMN sku_template_id SET NOT NULL;

DELETE FROM _migrations
 WHERE name = 'agent_retail_sku_decoupling_2026_07_17';

COMMIT;
