-- Logical rollback only: retain additive schema and old evidence, force the independent gate closed.
-- No DROP/TRUNCATE/RENAME is permitted for this funds feature.
INSERT INTO system_settings(key, value, value_type, description)
VALUES ('DEALER_INVENTORY_RESALE_ENABLED', 'false', 'boolean', '经销商逐级库存转售总闸')
ON CONFLICT (key) DO UPDATE SET value='false', value_type='boolean', updated_at=NOW();
