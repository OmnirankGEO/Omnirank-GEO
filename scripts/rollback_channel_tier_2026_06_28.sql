DROP VIEW IF EXISTS v_bonus_grant_reconcile;
DROP TABLE IF EXISTS bonus_grants;
DROP TABLE IF EXISTS founder_seats;
DROP TABLE IF EXISTS agent_tier_change_log;
DROP TABLE IF EXISTS agent_channel_tier_state;
DELETE FROM system_settings WHERE key = 'CHANNEL_TIER_ENABLED';
