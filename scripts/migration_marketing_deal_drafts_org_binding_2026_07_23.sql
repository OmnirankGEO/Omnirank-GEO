-- RFC: org revocation must reach deal drafts (external review 2026-07-23 P1-1).
-- marketing_deal_drafts rows created under an organization identity bind the
-- organization and the creator membership at write time; reads then recheck the
-- live membership so a revoked/departed member loses access (fail-closed).
-- Legacy personal-identity rows keep organization_id NULL and behave exactly as
-- before (owner_user_id check only).
--
-- Production migrations are pinned to public. Never resolve this state machine
-- through current_schema() or an unqualified regclass: a search_path lure must
-- not make readiness look healthy.
ALTER TABLE public.marketing_deal_drafts
  ADD COLUMN IF NOT EXISTS organization_id BIGINT NULL,
  ADD COLUMN IF NOT EXISTS created_by_membership_id BIGINT NULL;

-- 组织草稿的管理/审计检索(按组织查草稿);部分索引不占个人草稿行。
-- @index-guard idx_marketing_deal_drafts_org ON marketing_deal_drafts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_marketing_deal_drafts_org' AND i.indrelid = to_regclass('public.marketing_deal_drafts')) THEN
        NULL;  -- 已在 public.marketing_deal_drafts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_marketing_deal_drafts_org' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_marketing_deal_drafts_org 已存在但不在 public.marketing_deal_drafts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_marketing_deal_drafts_org' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_marketing_deal_drafts_org ON public.marketing_deal_drafts (organization_id, id DESC) WHERE organization_id IS NOT NULL;
    END IF;
END $idxguard$;

DO $$
DECLARE
  target_oid OID := to_regclass('public.marketing_deal_drafts');
  bad_columns INTEGER;
BEGIN
  IF target_oid IS NULL THEN
    RAISE EXCEPTION 'org binding migration target public.marketing_deal_drafts is missing';
  END IF;

  -- 新列必须存在、类型正确、保持 NULLABLE(个人草稿行不受组织绑定影响)。
  SELECT COUNT(*) INTO bad_columns
  FROM (VALUES
    ('organization_id','bigint'::regtype::oid,FALSE),
    ('created_by_membership_id','bigint'::regtype::oid,FALSE)
  ) wanted(attname,atttypid,attnotnull)
  LEFT JOIN pg_catalog.pg_attribute actual
    ON actual.attrelid=target_oid AND actual.attname=wanted.attname AND NOT actual.attisdropped
  WHERE actual.attnum IS NULL
     OR actual.atttypid<>wanted.atttypid
     OR actual.attnotnull<>wanted.attnotnull;
  IF bad_columns <> 0 THEN
    RAISE EXCEPTION 'org binding migration found missing/wrong columns on marketing_deal_drafts';
  END IF;
END $$;
