-- Manual rollback for the ADMIN cross-tenant governance package.
-- Destructive by design: run only on a confirmed rollback target after export.
BEGIN;

DROP TABLE IF EXISTS public.organization_invite_accept_receipts;
DROP TABLE IF EXISTS public.admin_demo_access_events;
DROP TABLE IF EXISTS public.admin_demo_case_grants;
DROP TABLE IF EXISTS public.admin_demo_cases;
DROP TABLE IF EXISTS public.admin_provider_downgrade_plans;
DROP TABLE IF EXISTS public.admin_cross_tenant_audits;
DROP TABLE IF EXISTS public.admin_governance_subject_versions;

COMMIT;
