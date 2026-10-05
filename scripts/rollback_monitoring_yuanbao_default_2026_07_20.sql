-- Restore only rows changed by migration_monitoring_yuanbao_default_2026_07_20.
-- Immutable monitoring results and reports are never touched.

UPDATE client_keywords target
   SET platforms = backup.old_value
  FROM monitoring_platform_matrix_backup_20260720 backup
 WHERE backup.source_table = 'client_keywords'
   AND backup.source_id = target.id
   AND target.platforms = 'dashscope,deepseek,doubao,yuanbao';

UPDATE extra_keywords target
   SET platforms = backup.old_value
  FROM monitoring_platform_matrix_backup_20260720 backup
 WHERE backup.source_table = 'extra_keywords'
   AND backup.source_id = target.id
   AND target.platforms = 'dashscope,deepseek,doubao,yuanbao';

UPDATE monitoring_config target
   SET default_platforms = backup.old_value,
       updated_at = NOW()
  FROM monitoring_platform_matrix_backup_20260720 backup
 WHERE backup.source_table = 'monitoring_config'
   AND backup.source_id = target.id
   AND target.default_platforms = 'dashscope,deepseek,doubao,yuanbao';

ALTER TABLE client_keywords
    ALTER COLUMN platforms SET DEFAULT 'dashscope,deepseek,kimi,doubao';
ALTER TABLE extra_keywords
    ALTER COLUMN platforms SET DEFAULT 'dashscope,deepseek,kimi,doubao';
ALTER TABLE monitoring_config
    ALTER COLUMN default_platforms SET DEFAULT 'dashscope,deepseek,kimi,doubao';
