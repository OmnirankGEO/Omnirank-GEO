-- Extend admin governance scopes for channel hierarchy, password security and
-- audited wallet correction. The wallet correction itself is a runtime ledger
-- write; this migration only widens the governance evidence enum.
-- Prestart-only, idempotent, and data-preserving. No relationship backfill.

ALTER TABLE admin_user_governance_versions
    DROP CONSTRAINT IF EXISTS admin_user_governance_versions_scope_check;
ALTER TABLE admin_user_governance_versions
    ADD CONSTRAINT admin_user_governance_versions_scope_check CHECK (
        scope IN (
            'business_identity', 'commercial_binding', 'channel_relationship',
            'platform_access', 'password_security', 'wallet_adjustment'
        )
    ) NOT VALID;
ALTER TABLE admin_user_governance_versions
    VALIDATE CONSTRAINT admin_user_governance_versions_scope_check;

ALTER TABLE admin_user_governance_audits
    DROP CONSTRAINT IF EXISTS admin_user_governance_audits_scope_check;
ALTER TABLE admin_user_governance_audits
    ADD CONSTRAINT admin_user_governance_audits_scope_check CHECK (
        scope IN (
            'business_identity', 'commercial_binding', 'channel_relationship',
            'platform_access', 'password_security', 'wallet_adjustment'
        )
    ) NOT VALID;
ALTER TABLE admin_user_governance_audits
    VALIDATE CONSTRAINT admin_user_governance_audits_scope_check;

DO $$
DECLARE
    table_name TEXT;
    definition TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'admin_user_governance_versions',
        'admin_user_governance_audits'
    ]
    LOOP
        SELECT pg_get_constraintdef(c.oid)
          INTO definition
          FROM pg_constraint c
         WHERE c.conrelid = table_name::regclass
           AND c.conname = table_name || '_scope_check'
           AND c.contype = 'c'
           AND c.convalidated;
        IF definition IS NULL
           OR position('channel_relationship' IN definition) = 0
           OR position('password_security' IN definition) = 0
           OR position('wallet_adjustment' IN definition) = 0 THEN
            RAISE EXCEPTION '% scope constraint extension failed', table_name;
        END IF;
    END LOOP;
END $$;
