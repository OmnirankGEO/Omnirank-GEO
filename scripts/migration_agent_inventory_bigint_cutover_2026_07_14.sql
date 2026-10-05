-- 服务商进货大额库存承载
-- 非破坏性类型扩宽 · 幂等 · 仅 prestart 执行；不启用快照 cutover。
-- 只允许 prestart 部署编排使用 psql -X -v ON_ERROR_STOP=1 执行；
-- 可使用 -f，容器部署脚本会通过 stdin 传入同一文件内容。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE system_settings
    ADD COLUMN IF NOT EXISTS updated_by INTEGER,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;

CREATE TABLE IF NOT EXISTS agent_pricing_overrides (
    agent_user_id INTEGER PRIMARY KEY,
    quote_markup_override NUMERIC(4,2),
    sku_markup_override NUMERIC(4,2),
    wholesale_numer INTEGER,
    wholesale_denom INTEGER,
    note TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_by INTEGER
);

-- 修复早期环境可能已存在的不完整表；只做 additive 补列，不重写业务数据。
ALTER TABLE agent_pricing_overrides
    ADD COLUMN IF NOT EXISTS quote_markup_override NUMERIC(4,2),
    ADD COLUMN IF NOT EXISTS sku_markup_override NUMERIC(4,2),
    ADD COLUMN IF NOT EXISTS wholesale_numer INTEGER,
    ADD COLUMN IF NOT EXISTS wholesale_denom INTEGER,
    ADD COLUMN IF NOT EXISTS note TEXT,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ADD COLUMN IF NOT EXISTS updated_by INTEGER;

COMMIT;

-- writer generation fence：marker 启用后，数据库直接拒绝任何空快照代理进货 INSERT。
-- agent_inventory_legacy_eligible 是 activation 时一次性写入的显式 allowlist；
-- DEFAULT FALSE 对旧行立即生效，只有 activation 持锁时已存在的 pending 行会被改为 TRUE。
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE recharge_orders
    ADD COLUMN IF NOT EXISTS agent_inventory_legacy_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS agent_inventory_writer_generation SMALLINT;

CREATE SEQUENCE IF NOT EXISTS agent_inventory_writer_generation_fence_seq
    AS SMALLINT MINVALUE 1 MAXVALUE 2 START WITH 1 NO CYCLE;

CREATE OR REPLACE FUNCTION enforce_agent_inventory_snapshot_writer_generation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    active_generation SMALLINT;
BEGIN
    SELECT COALESCE(
        pg_sequence_last_value('agent_inventory_writer_generation_fence_seq'::regclass),
        1
    )::SMALLINT INTO active_generation;

    IF NEW.order_type = 'agent_inventory_purchase'
       AND active_generation >= 2
       AND (
           NEW.agent_inventory_writer_generation IS DISTINCT FROM 2
           OR
           NEW.pricing_snapshot_jsonb IS NULL
           OR NEW.pricing_catalog_version IS NULL
           OR btrim(NEW.pricing_catalog_version) = ''
       )
    THEN
        RAISE EXCEPTION 'agent inventory snapshot writer generation rejected empty snapshot order'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_agent_inventory_snapshot_writer_generation ON recharge_orders;
CREATE TRIGGER trg_agent_inventory_snapshot_writer_generation
BEFORE INSERT OR UPDATE OF
    order_type, pricing_snapshot_jsonb, pricing_catalog_version,
    agent_inventory_writer_generation, agent_inventory_legacy_eligible
ON recharge_orders
FOR EACH ROW
EXECUTE FUNCTION enforce_agent_inventory_snapshot_writer_generation();

COMMIT;

-- v_bonus_grant_reconcile 同时依赖代理/客户 bonus 余额列。PostgreSQL 不允许在
-- view 仍依赖列时 ALTER TYPE，因此在同一事务中保存定义/owner/显式 grants，
-- 扩宽两张钱包表后原样重建；任一步失败会整体回滚，不留下 view 缺失窗口。
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

CREATE TEMP TABLE _agent_inventory_bigint_view_state ON COMMIT DROP AS
SELECT pg_get_viewdef(c.oid, TRUE) AS view_definition,
       pg_get_userbyid(c.relowner) AS owner_name
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public'
   AND c.relname = 'v_bonus_grant_reconcile'
   AND c.relkind = 'v';

CREATE TEMP TABLE _agent_inventory_bigint_view_grants ON COMMIT DROP AS
SELECT CASE WHEN acl.grantee = 0 THEN NULL ELSE pg_get_userbyid(acl.grantee) END AS grantee_name,
       acl.privilege_type,
       acl.is_grantable
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl, acldefault('r', c.relowner))) acl
 WHERE n.nspname = 'public'
   AND c.relname = 'v_bonus_grant_reconcile'
   AND c.relkind = 'v'
   AND acl.grantee <> c.relowner;

DROP VIEW IF EXISTS public.v_bonus_grant_reconcile;

ALTER TABLE agent_inventory_wallets
    ALTER COLUMN paid_inventory_points TYPE BIGINT USING paid_inventory_points::BIGINT,
    ALTER COLUMN bonus_inventory_points TYPE BIGINT USING bonus_inventory_points::BIGINT,
    ALTER COLUMN frozen_inventory_points TYPE BIGINT USING frozen_inventory_points::BIGINT;

ALTER TABLE customer_agent_credit_wallets
    ALTER COLUMN tool_credit_points TYPE BIGINT USING tool_credit_points::BIGINT,
    ALTER COLUMN publish_credit_points TYPE BIGINT USING publish_credit_points::BIGINT,
    ALTER COLUMN bonus_credit_points TYPE BIGINT USING bonus_credit_points::BIGINT;

DO $$
DECLARE
    saved_view RECORD;
    saved_grant RECORD;
    grantee_sql TEXT;
BEGIN
    SELECT * INTO saved_view FROM _agent_inventory_bigint_view_state LIMIT 1;
    IF FOUND THEN
        EXECUTE format(
            'CREATE VIEW public.v_bonus_grant_reconcile AS %s',
            saved_view.view_definition
        );
        FOR saved_grant IN SELECT * FROM _agent_inventory_bigint_view_grants LOOP
            grantee_sql := CASE
                WHEN saved_grant.grantee_name IS NULL THEN 'PUBLIC'
                ELSE quote_ident(saved_grant.grantee_name)
            END;
            EXECUTE format(
                'GRANT %s ON TABLE public.v_bonus_grant_reconcile TO %s%s',
                saved_grant.privilege_type,
                grantee_sql,
                CASE WHEN saved_grant.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END
            );
        END LOOP;
        EXECUTE format(
            'ALTER VIEW public.v_bonus_grant_reconcile OWNER TO %I',
            saved_view.owner_name
        );
    END IF;
END $$;

COMMIT;

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE agent_inventory_transactions
    ALTER COLUMN points TYPE BIGINT USING points::BIGINT,
    ALTER COLUMN balance_paid_after TYPE BIGINT USING balance_paid_after::BIGINT,
    ALTER COLUMN balance_bonus_after TYPE BIGINT USING balance_bonus_after::BIGINT;

COMMIT;

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE customer_credit_transactions
    ALTER COLUMN points TYPE BIGINT USING points::BIGINT,
    ALTER COLUMN balance_tool_after TYPE BIGINT USING balance_tool_after::BIGINT,
    ALTER COLUMN balance_publish_after TYPE BIGINT USING balance_publish_after::BIGINT,
    ALTER COLUMN balance_bonus_after TYPE BIGINT USING balance_bonus_after::BIGINT;

COMMIT;

-- 验证：information_schema.columns 中上述 13 列 data_type 均为 bigint；
--       recharge_orders legacy eligibility / writer generation 列存在且 trigger 已启用；
--       本脚本绝不写 AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT。
-- 回滚：代码可停止使用新快照字段，但不得把 BIGINT 窄化回 INTEGER，
--       避免已写入的大额库存/流水丢失。
