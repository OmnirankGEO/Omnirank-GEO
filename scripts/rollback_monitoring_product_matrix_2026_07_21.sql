-- Evidence-based rollback for migration_monitoring_product_matrix_2026_07_21.
-- Restore only rows whose original classic-four value was captured by the
-- retired migration and whose value still matches this migration's result.

SET LOCAL search_path = pg_catalog, public;

UPDATE public.client_keywords target
   SET platforms = 'dashscope,deepseek,doubao,yuanbao'
  FROM public.monitoring_platform_matrix_backup_20260720 backup
 WHERE backup.source_table = 'client_keywords'
   AND backup.source_id = target.id
   AND backup.old_value = 'dashscope,deepseek,kimi,doubao'
   AND target.platforms = 'dashscope,deepseek,kimi,doubao';

UPDATE public.extra_keywords target
   SET platforms = 'dashscope,deepseek,doubao,yuanbao'
  FROM public.monitoring_platform_matrix_backup_20260720 backup
 WHERE backup.source_table = 'extra_keywords'
   AND backup.source_id = target.id
   AND backup.old_value = 'dashscope,deepseek,kimi,doubao'
   AND target.platforms = 'dashscope,deepseek,kimi,doubao';

UPDATE public.monitoring_config target
   SET default_platforms = 'dashscope,deepseek,doubao,yuanbao',
       updated_at = NOW()
  FROM public.monitoring_platform_matrix_backup_20260720 backup
 WHERE backup.source_table = 'monitoring_config'
   AND backup.source_id = target.id
   AND backup.old_value = 'dashscope,deepseek,kimi,doubao'
   AND target.default_platforms = 'dashscope,deepseek,kimi,doubao';

ALTER TABLE public.client_keywords
    ALTER COLUMN platforms SET DEFAULT 'dashscope,deepseek,doubao,yuanbao';
ALTER TABLE public.extra_keywords
    ALTER COLUMN platforms SET DEFAULT 'dashscope,deepseek,doubao,yuanbao';
ALTER TABLE public.monitoring_config
    ALTER COLUMN default_platforms SET DEFAULT 'dashscope,deepseek,doubao,yuanbao';
ALTER TABLE public.monitoring_tasks
    ALTER COLUMN platform_count SET DEFAULT 4;

-- The versioned entitlement column and append-only evidence remain in place.
-- Removing them would make already-created confirmed rows ambiguous and is not
-- a safe operational rollback. Code rollback continues to ignore these objects.
