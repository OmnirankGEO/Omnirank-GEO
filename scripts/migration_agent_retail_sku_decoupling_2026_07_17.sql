-- Service-provider retail SKU decoupling (additive expansion / prestart-only).
--
-- agent_sku_overrides remains the only writable retail management source.  The
-- published retail catalog remains the runtime price SSOT.  sku_templates is
-- retained only as an optional prefill/provenance source for new rows and as a
-- compatibility reference for historical rows.
--
-- This migration deliberately keeps the legacy sku_template_id column and FK.
-- It only relaxes NOT NULL so a real custom retail SKU can exist without a
-- platform template.  Historical values and orders are never rewritten.

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

ALTER TABLE agent_sku_overrides
    ADD COLUMN IF NOT EXISTS retail_sku_id TEXT,
    ADD COLUMN IF NOT EXISTS points_granted BIGINT,
    ADD COLUMN IF NOT EXISTS source_template_id INTEGER,
    ADD COLUMN IF NOT EXISTS version INTEGER,
    ADD COLUMN IF NOT EXISTS client_request_id TEXT,
    ADD COLUMN IF NOT EXISTS custom_scene TEXT,
    ADD COLUMN IF NOT EXISTS sort_order INTEGER,
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

-- Freeze every historically visible platform fallback as an explicit retail
-- SKU before runtime fallback is removed.  A pre-existing inactive/tombstoned
-- override blocks materialization, preserving the provider's stop-selling fact.
INSERT INTO agent_sku_overrides (
    agent_user_id, sku_template_id, source_template_id, retail_sku_id,
    points_granted, custom_name, custom_subtitle, custom_sales_pitch,
    retail_cents, is_active, margin_warning, sort_order, version,
    created_at, updated_at
)
SELECT
    target.agent_user_id,
    t.id,
    t.id,
    'RSKU-MIG-' || UPPER(MD5(target.agent_user_id::TEXT || ':' || t.id::TEXT)),
    t.points_granted::BIGINT,
    COALESCE(NULLIF(BTRIM(t.default_name), ''), t.template_code),
    t.default_subtitle,
    NULL,
    t.suggested_retail_cents,
    TRUE,
    'allowed',
    0,
    1,
    NOW(),
    NOW()
FROM (
    SELECT DISTINCT u.id AS agent_user_id
      FROM users u
      LEFT JOIN user_wallets w ON w.user_id = u.id
     WHERE COALESCE(u.is_active, 1) = 1
       AND (
            COALESCE(w.agent_level, 0) >= 1
            OR EXISTS (
                SELECT 1 FROM customer_agent_bindings b
                 WHERE b.agent_user_id = u.id
            )
            OR EXISTS (
                SELECT 1 FROM agent_sku_overrides existing
                 WHERE existing.agent_user_id = u.id
            )
       )
) target
CROSS JOIN sku_templates t
WHERE t.is_active = TRUE
  AND NOT EXISTS (
      SELECT 1 FROM _migrations applied
       WHERE applied.name = 'agent_retail_sku_initial_fallback_freeze_2026_07_17'
  )
  AND t.points_granted > 0
  AND t.suggested_retail_cents IS NOT NULL
  AND t.suggested_retail_cents > 0
  AND NOT EXISTS (
      SELECT 1
        FROM agent_sku_overrides existing
       WHERE existing.agent_user_id = target.agent_user_id
         AND COALESCE(existing.source_template_id, existing.sku_template_id) = t.id
  );

-- This marker is deliberately independent from the binary compatibility marker
-- removed by rollback.  Fallback materialization is a one-time data freeze: a
-- later rollback/forward cycle must not turn providers or templates created in
-- the meantime into new retail SKUs.
INSERT INTO _migrations (name, applied_at)
VALUES ('agent_retail_sku_initial_fallback_freeze_2026_07_17', NOW())
ON CONFLICT (name) DO NOTHING;

-- Backfill legacy white-label packages once.  Their current name, points,
-- price and selling state are copied exactly; later template edits can no
-- longer mutate the package's canonical points/name.
UPDATE agent_sku_overrides o
   SET retail_sku_id = COALESCE(
           o.retail_sku_id,
           'RSKU-LEG-' || UPPER(MD5(o.id::TEXT || ':' || o.agent_user_id::TEXT))
       ),
       points_granted = COALESCE(o.points_granted, t.points_granted::BIGINT),
       source_template_id = COALESCE(o.source_template_id, o.sku_template_id),
       custom_name = COALESCE(
           NULLIF(BTRIM(o.custom_name), ''),
           NULLIF(BTRIM(t.default_name), ''),
           t.template_code
       ),
       sort_order = COALESCE(o.sort_order, 0),
       version = COALESCE(o.version, 1)
  FROM sku_templates t
 WHERE o.sku_template_id = t.id
   AND (
       o.retail_sku_id IS NULL OR o.points_granted IS NULL
       OR o.source_template_id IS NULL OR o.version IS NULL
       OR o.sort_order IS NULL OR NULLIF(BTRIM(o.custom_name), '') IS NULL
   );

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM agent_sku_overrides
         WHERE retail_sku_id IS NULL
            OR points_granted IS NULL OR points_granted <= 0
            OR retail_cents IS NULL OR retail_cents <= 0
            OR is_active IS NULL
            OR version IS NULL OR version <= 0
            OR sort_order IS NULL
            OR NULLIF(BTRIM(custom_name), '') IS NULL
    ) THEN
        RAISE EXCEPTION 'agent_sku_overrides contains rows that cannot be canonically backfilled';
    END IF;
END $$;

-- Blue/green compatibility: the old active binary does not know the canonical
-- columns yet.  Fill them from its historical sku_template_id before NOT NULL
-- checks run.  New template-independent writers supply their fields explicitly;
-- source_template_id remains provenance and is never used as legacy identity.
CREATE OR REPLACE FUNCTION agent_retail_sku_legacy_fill()
RETURNS TRIGGER AS $$
DECLARE
    template_points BIGINT;
    template_name TEXT;
    template_code_value TEXT;
BEGIN
    IF NEW.sku_template_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT points_granted::BIGINT, default_name, template_code
      INTO template_points, template_name, template_code_value
      FROM sku_templates
     WHERE id = NEW.sku_template_id;

    IF NEW.retail_sku_id IS NULL THEN
        NEW.retail_sku_id := 'RSKU-LEG-' || UPPER(MD5(CONCAT_WS(
            ':', NEW.agent_user_id::TEXT, NEW.sku_template_id::TEXT,
            COALESCE(NEW.id::TEXT, ''), CLOCK_TIMESTAMP()::TEXT,
            TXID_CURRENT()::TEXT
        )));
    END IF;
    NEW.points_granted := COALESCE(NEW.points_granted, template_points);
    NEW.source_template_id := COALESCE(NEW.source_template_id, NEW.sku_template_id);
    IF NULLIF(BTRIM(NEW.custom_name), '') IS NULL THEN
        NEW.custom_name := COALESCE(NULLIF(BTRIM(template_name), ''), template_code_value);
    END IF;
    NEW.version := COALESCE(NEW.version, 1);
    NEW.sort_order := COALESCE(NEW.sort_order, 0);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgname = 'trg_agent_retail_sku_legacy_fill'
           AND tgrelid = 'agent_sku_overrides'::regclass
           AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_agent_retail_sku_legacy_fill
        BEFORE INSERT ON agent_sku_overrides
        FOR EACH ROW EXECUTE FUNCTION agent_retail_sku_legacy_fill();
    END IF;
END $$;

ALTER TABLE agent_sku_overrides
    ALTER COLUMN sku_template_id DROP NOT NULL,
    ALTER COLUMN retail_sku_id SET NOT NULL,
    ALTER COLUMN points_granted SET NOT NULL,
    ALTER COLUMN custom_name SET NOT NULL,
    ALTER COLUMN version SET DEFAULT 1,
    ALTER COLUMN version SET NOT NULL,
    ALTER COLUMN sort_order SET DEFAULT 0,
    ALTER COLUMN sort_order SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_agent_retail_sku_source_template'
           AND conrelid = 'agent_sku_overrides'::regclass
    ) THEN
        ALTER TABLE agent_sku_overrides
            ADD CONSTRAINT fk_agent_retail_sku_source_template
            FOREIGN KEY (source_template_id) REFERENCES sku_templates(id) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_agent_retail_sku_identity'
           AND conrelid = 'agent_sku_overrides'::regclass
    ) THEN
        ALTER TABLE agent_sku_overrides
            ADD CONSTRAINT chk_agent_retail_sku_identity
            CHECK (retail_sku_id ~ '^RSKU-[A-Z0-9-]{8,59}$') NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_agent_retail_sku_points'
           AND conrelid = 'agent_sku_overrides'::regclass
    ) THEN
        ALTER TABLE agent_sku_overrides
            ADD CONSTRAINT chk_agent_retail_sku_points
            CHECK (points_granted > 0 AND points_granted <= 9000000000000000) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_agent_retail_sku_money'
           AND conrelid = 'agent_sku_overrides'::regclass
    ) THEN
        ALTER TABLE agent_sku_overrides
            ADD CONSTRAINT chk_agent_retail_sku_money
            CHECK (retail_cents > 0 AND retail_cents <= 2000000000) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_agent_retail_sku_version'
           AND conrelid = 'agent_sku_overrides'::regclass
    ) THEN
        ALTER TABLE agent_sku_overrides
            ADD CONSTRAINT chk_agent_retail_sku_version
            CHECK (version >= 1) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_agent_retail_sku_name'
           AND conrelid = 'agent_sku_overrides'::regclass
    ) THEN
        ALTER TABLE agent_sku_overrides
            ADD CONSTRAINT chk_agent_retail_sku_name
            CHECK (CHAR_LENGTH(BTRIM(custom_name)) BETWEEN 1 AND 120) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_agent_retail_sku_client_request'
           AND conrelid = 'agent_sku_overrides'::regclass
    ) THEN
        ALTER TABLE agent_sku_overrides
            ADD CONSTRAINT chk_agent_retail_sku_client_request
            CHECK (
                client_request_id IS NULL
                OR CHAR_LENGTH(BTRIM(client_request_id)) BETWEEN 8 AND 80
            ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_agent_retail_sku_copy_lengths'
           AND conrelid = 'agent_sku_overrides'::regclass
    ) THEN
        ALTER TABLE agent_sku_overrides
            ADD CONSTRAINT chk_agent_retail_sku_copy_lengths
            CHECK (
                (custom_subtitle IS NULL OR CHAR_LENGTH(custom_subtitle) <= 240)
                AND (custom_sales_pitch IS NULL OR CHAR_LENGTH(custom_sales_pitch) <= 1000)
                AND (custom_scene IS NULL OR CHAR_LENGTH(custom_scene) <= 500)
            ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_agent_retail_sku_tombstone'
           AND conrelid = 'agent_sku_overrides'::regclass
    ) THEN
        ALTER TABLE agent_sku_overrides
            ADD CONSTRAINT chk_agent_retail_sku_tombstone
            CHECK (deleted_at IS NULL OR is_active = FALSE) NOT VALID;
    END IF;
END $$;

ALTER TABLE agent_sku_overrides VALIDATE CONSTRAINT fk_agent_retail_sku_source_template;
ALTER TABLE agent_sku_overrides VALIDATE CONSTRAINT chk_agent_retail_sku_identity;
ALTER TABLE agent_sku_overrides VALIDATE CONSTRAINT chk_agent_retail_sku_points;
ALTER TABLE agent_sku_overrides VALIDATE CONSTRAINT chk_agent_retail_sku_money;
ALTER TABLE agent_sku_overrides VALIDATE CONSTRAINT chk_agent_retail_sku_version;
ALTER TABLE agent_sku_overrides VALIDATE CONSTRAINT chk_agent_retail_sku_name;
ALTER TABLE agent_sku_overrides VALIDATE CONSTRAINT chk_agent_retail_sku_client_request;
ALTER TABLE agent_sku_overrides VALIDATE CONSTRAINT chk_agent_retail_sku_copy_lengths;
ALTER TABLE agent_sku_overrides VALIDATE CONSTRAINT chk_agent_retail_sku_tombstone;

-- @index-guard ux_agent_retail_sku_id ON agent_sku_overrides unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_agent_retail_sku_id' AND i.indrelid = to_regclass('agent_sku_overrides')) THEN
        NULL;  -- 已在 agent_sku_overrides 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_agent_retail_sku_id' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('agent_sku_overrides'))) THEN
        RAISE EXCEPTION '[index-guard] ux_agent_retail_sku_id 已存在但不在 agent_sku_overrides 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_agent_retail_sku_id' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('agent_sku_overrides')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_agent_retail_sku_id ON agent_sku_overrides (retail_sku_id);
    END IF;
END $idxguard$;
-- @index-guard ux_agent_retail_sku_client_request ON agent_sku_overrides unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_agent_retail_sku_client_request' AND i.indrelid = to_regclass('agent_sku_overrides')) THEN
        NULL;  -- 已在 agent_sku_overrides 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_agent_retail_sku_client_request' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('agent_sku_overrides'))) THEN
        RAISE EXCEPTION '[index-guard] ux_agent_retail_sku_client_request 已存在但不在 agent_sku_overrides 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_agent_retail_sku_client_request' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('agent_sku_overrides')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_agent_retail_sku_client_request ON agent_sku_overrides (agent_user_id, client_request_id) WHERE client_request_id IS NOT NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_agent_retail_sku_catalog ON agent_sku_overrides plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_agent_retail_sku_catalog' AND i.indrelid = to_regclass('agent_sku_overrides')) THEN
        NULL;  -- 已在 agent_sku_overrides 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_agent_retail_sku_catalog' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('agent_sku_overrides'))) THEN
        RAISE EXCEPTION '[index-guard] idx_agent_retail_sku_catalog 已存在但不在 agent_sku_overrides 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_agent_retail_sku_catalog' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('agent_sku_overrides')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_agent_retail_sku_catalog ON agent_sku_overrides (agent_user_id, is_active, sort_order, id) WHERE deleted_at IS NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_agent_retail_sku_source_template ON agent_sku_overrides plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_agent_retail_sku_source_template' AND i.indrelid = to_regclass('agent_sku_overrides')) THEN
        NULL;  -- 已在 agent_sku_overrides 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_agent_retail_sku_source_template' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('agent_sku_overrides'))) THEN
        RAISE EXCEPTION '[index-guard] idx_agent_retail_sku_source_template 已存在但不在 agent_sku_overrides 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_agent_retail_sku_source_template' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('agent_sku_overrides')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_agent_retail_sku_source_template ON agent_sku_overrides (source_template_id) WHERE source_template_id IS NOT NULL;
    END IF;
END $idxguard$;

INSERT INTO _migrations (name, applied_at)
VALUES ('agent_retail_sku_decoupling_2026_07_17', NOW())
ON CONFLICT (name) DO NOTHING;

COMMIT;
