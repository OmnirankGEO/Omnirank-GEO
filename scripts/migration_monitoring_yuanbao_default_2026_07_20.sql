-- Move the active monitoring configuration from Kimi to Yuanbao without
-- rewriting immutable monitoring results or reports. Exact legacy defaults
-- are backed up before replacement so rollback remains evidence-based.

CREATE TABLE IF NOT EXISTS monitoring_platform_matrix_backup_20260720 (
    source_table TEXT NOT NULL CHECK (
        source_table IN ('client_keywords', 'extra_keywords', 'monitoring_config')
    ),
    source_id BIGINT NOT NULL,
    old_value TEXT NOT NULL,
    migrated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source_table, source_id)
);

INSERT INTO monitoring_platform_matrix_backup_20260720 (source_table, source_id, old_value)
SELECT 'client_keywords', id, platforms
  FROM client_keywords
 WHERE platforms = 'dashscope,deepseek,kimi,doubao'
ON CONFLICT (source_table, source_id) DO NOTHING;

INSERT INTO monitoring_platform_matrix_backup_20260720 (source_table, source_id, old_value)
SELECT 'extra_keywords', id, platforms
  FROM extra_keywords
 WHERE platforms = 'dashscope,deepseek,kimi,doubao'
ON CONFLICT (source_table, source_id) DO NOTHING;

INSERT INTO monitoring_platform_matrix_backup_20260720 (source_table, source_id, old_value)
SELECT 'monitoring_config', id, default_platforms
  FROM monitoring_config
 WHERE default_platforms = 'dashscope,deepseek,kimi,doubao'
ON CONFLICT (source_table, source_id) DO NOTHING;

UPDATE client_keywords
   SET platforms = 'dashscope,deepseek,doubao,yuanbao'
 WHERE platforms = 'dashscope,deepseek,kimi,doubao';

UPDATE extra_keywords
   SET platforms = 'dashscope,deepseek,doubao,yuanbao'
 WHERE platforms = 'dashscope,deepseek,kimi,doubao';

UPDATE monitoring_config
   SET default_platforms = 'dashscope,deepseek,doubao,yuanbao',
       updated_at = NOW()
 WHERE default_platforms = 'dashscope,deepseek,kimi,doubao';

ALTER TABLE client_keywords
    ALTER COLUMN platforms SET DEFAULT 'dashscope,deepseek,doubao,yuanbao';
ALTER TABLE extra_keywords
    ALTER COLUMN platforms SET DEFAULT 'dashscope,deepseek,doubao,yuanbao';
ALTER TABLE monitoring_config
    ALTER COLUMN default_platforms SET DEFAULT 'dashscope,deepseek,doubao,yuanbao';

DO $$
DECLARE
    item RECORD;
    actual_default TEXT;
BEGIN
    FOR item IN
        SELECT * FROM (VALUES
            ('client_keywords', 'platforms'),
            ('extra_keywords', 'platforms'),
            ('monitoring_config', 'default_platforms')
        ) AS expected(table_name, column_name)
    LOOP
        SELECT pg_get_expr(d.adbin, d.adrelid)
          INTO actual_default
          FROM pg_attrdef d
          JOIN pg_attribute a
            ON a.attrelid = d.adrelid
           AND a.attnum = d.adnum
         WHERE d.adrelid = item.table_name::regclass
           AND a.attname = item.column_name;
        IF actual_default IS NULL
           OR position('dashscope,deepseek,doubao,yuanbao' IN actual_default) = 0 THEN
            RAISE EXCEPTION 'active monitoring default mismatch: %.% = %',
                item.table_name, item.column_name, actual_default;
        END IF;
    END LOOP;
END $$;
