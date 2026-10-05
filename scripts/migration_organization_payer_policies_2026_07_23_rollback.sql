-- Rollback for migration_organization_payer_policies_2026_07_23.sql (dev/test only).
-- Removes the payer-policy surface (audit trigger/function, both tables, the
-- split CHECK constraints and the overage index). The five additive
-- organization_charge_links evidence columns intentionally stay: dropping them
-- would leave attisdropped catalog ghosts that permanently diverge the signed
-- catalog fingerprint after a forward re-run. They are inert NULL/0 columns.

DROP TRIGGER IF EXISTS trg_org_payer_policy_events_append_only ON organization_payer_policy_events;
DROP FUNCTION IF EXISTS organization_payer_policy_events_append_only();
DROP TABLE IF EXISTS organization_payer_policy_events;
DROP TABLE IF EXISTS organization_payer_policies;

ALTER TABLE organization_charge_links DROP CONSTRAINT IF EXISTS organization_charge_overage_consent_required;
ALTER TABLE organization_charge_links DROP CONSTRAINT IF EXISTS organization_charge_payer_split_valid;
DROP INDEX IF EXISTS idx_org_charge_overage_period;

UPDATE organization_schema_migrations
SET status='rolled_back',rolled_back_at=NOW()
WHERE version='organization_payer_policies_2026_07_23_v1';
