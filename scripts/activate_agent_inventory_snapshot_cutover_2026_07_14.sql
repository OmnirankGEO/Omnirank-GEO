-- 代理进货不可变快照 cutover 激活（部署编排 Phase B，不属于 prestart migration）。
-- 硬前提：active 与 hot rollback 均已由部署脚本验证为同一 image，且都具备
-- agent-inventory-snapshot-v3 能力（含 Phase A 新订单停写门）；旧 binary 已停止且所有请求/DB 事务已经排空。
-- 表锁与 writer-generation trigger 共同封闭“事务先开始、marker 后 INSERT”的窗口。
-- 只允许由受控部署编排使用 psql -X -v ON_ERROR_STOP=1 执行；
-- 可使用 -f，容器部署脚本会通过 stdin 传入同一文件内容。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

DO $$
DECLARE
    bigint_columns INTEGER;
    eligibility_columns INTEGER;
    generation_columns INTEGER;
    generation_triggers INTEGER;
    generation_sequences INTEGER;
BEGIN
    SELECT COUNT(*) INTO bigint_columns
    FROM information_schema.columns c
    JOIN (VALUES
        ('agent_inventory_wallets', 'paid_inventory_points'),
        ('agent_inventory_wallets', 'bonus_inventory_points'),
        ('agent_inventory_wallets', 'frozen_inventory_points'),
        ('agent_inventory_transactions', 'points'),
        ('agent_inventory_transactions', 'balance_paid_after'),
        ('agent_inventory_transactions', 'balance_bonus_after'),
        ('customer_agent_credit_wallets', 'tool_credit_points'),
        ('customer_agent_credit_wallets', 'publish_credit_points'),
        ('customer_agent_credit_wallets', 'bonus_credit_points'),
        ('customer_credit_transactions', 'points'),
        ('customer_credit_transactions', 'balance_tool_after'),
        ('customer_credit_transactions', 'balance_publish_after'),
        ('customer_credit_transactions', 'balance_bonus_after')
    ) AS required(table_name, column_name)
      ON c.table_name = required.table_name
     AND c.column_name = required.column_name
    WHERE c.table_schema = current_schema()
      AND c.data_type = 'bigint';

    IF bigint_columns <> 13 THEN
        RAISE EXCEPTION 'snapshot cutover refused: expected 13 BIGINT inventory columns, found %', bigint_columns;
    END IF;

    SELECT COUNT(*) INTO eligibility_columns
    FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'recharge_orders'
      AND column_name = 'agent_inventory_legacy_eligible'
      AND data_type = 'boolean';

    SELECT COUNT(*) INTO generation_triggers
    FROM pg_trigger t
    JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = current_schema()
      AND c.relname = 'recharge_orders'
      AND t.tgname = 'trg_agent_inventory_snapshot_writer_generation'
      AND NOT t.tgisinternal
      AND t.tgenabled <> 'D';

    SELECT COUNT(*) INTO generation_columns
    FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'recharge_orders'
      AND column_name = 'agent_inventory_writer_generation'
      AND data_type = 'smallint';

    SELECT COUNT(*) INTO generation_sequences
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = current_schema()
      AND c.relname = 'agent_inventory_writer_generation_fence_seq'
      AND c.relkind = 'S';

    IF eligibility_columns <> 1 OR generation_columns <> 1
       OR generation_triggers <> 1 OR generation_sequences <> 1 THEN
        RAISE EXCEPTION 'snapshot cutover refused: writer generation fence is incomplete';
    END IF;
END $$;

-- 与旧 writer 排空协议配合：等待当前 recharge_orders 写事务结束，并阻止新 INSERT
-- 直到 cutover 事务提交。只有完成外部停写前提后才允许执行。
LOCK TABLE recharge_orders IN SHARE MODE;

-- 首次 activation 必须消费部署脚本刚写入的双槽位能力证据。直接手工运行 SQL、
-- 只启动一个新容器或用不同 image 伪造热回滚都会在 marker 写入前失败。
DO $$
DECLARE
    marker_exists BOOLEAN;
    readiness_value TEXT;
    readiness_updated_at TIMESTAMP;
    capability TEXT;
    active_image TEXT;
    hot_image TEXT;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM system_settings
        WHERE key = 'AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT'
    ) INTO marker_exists;

    IF NOT marker_exists THEN
        SELECT value, updated_at INTO readiness_value, readiness_updated_at
        FROM system_settings
        WHERE key = 'AGENT_INVENTORY_SNAPSHOT_HOT_ROLLBACK_READY';

        capability := split_part(COALESCE(readiness_value, ''), '|', 1);
        active_image := split_part(COALESCE(readiness_value, ''), '|', 2);
        hot_image := split_part(COALESCE(readiness_value, ''), '|', 3);
        IF capability <> 'agent-inventory-snapshot-v3'
           OR active_image = ''
           OR hot_image = ''
           OR active_image <> hot_image
           OR readiness_updated_at IS NULL
           OR readiness_updated_at < clock_timestamp() - INTERVAL '10 minutes'
           OR readiness_updated_at > clock_timestamp()
        THEN
            RAISE EXCEPTION 'snapshot cutover refused: fresh same-image hot rollback evidence is missing';
        END IF;
    END IF;
END $$;

DO $$
DECLARE
    marker_exists BOOLEAN;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM system_settings
        WHERE key = 'AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT'
    ) INTO marker_exists;

    IF NOT marker_exists THEN
        -- 只在首次 activation、持有表锁时，把当下真实存在的 legacy pending 订单
        -- 固化为显式 allowlist。重复 activation 绝不扩大该集合。
        UPDATE recharge_orders
        SET agent_inventory_writer_generation = CASE
                WHEN pricing_snapshot_jsonb IS NOT NULL
                     AND pricing_catalog_version IS NOT NULL
                     AND btrim(pricing_catalog_version) <> '' THEN 2
                ELSE 1
            END,
            agent_inventory_legacy_eligible = CASE
                WHEN payment_status = 'pending'
                     AND pricing_snapshot_jsonb IS NULL
                     AND pricing_catalog_version IS NULL THEN TRUE
                ELSE FALSE
            END
        WHERE order_type = 'agent_inventory_purchase'
          AND agent_inventory_writer_generation IS NULL;
    END IF;
END $$;

-- generation constraint 不读取 system_settings。因此旧事务即使使用 REPEATABLE READ、
-- 看不见 marker，也无法在 activation 后插入未声明代际的代理进货订单。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'recharge_orders'::regclass
          AND conname = 'check_agent_inventory_writer_generation_required'
    ) THEN
        ALTER TABLE recharge_orders
            ADD CONSTRAINT check_agent_inventory_writer_generation_required
            CHECK (
                order_type IS DISTINCT FROM 'agent_inventory_purchase'
                OR agent_inventory_writer_generation IN (1, 2)
            ) NOT VALID;
    END IF;
END $$;

ALTER TABLE recharge_orders
    VALIDATE CONSTRAINT check_agent_inventory_writer_generation_required;

INSERT INTO system_settings (key, value, description)
VALUES (
    'AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT',
    clock_timestamp()::timestamp::text,
    '双槽位快照能力验证后启用；generation=1 allowlist / generation=2 snapshot writer'
)
ON CONFLICT (key) DO NOTHING;

DO $$
DECLARE
    cutover_text TEXT;
    cutover_at TIMESTAMP;
    invalid_orders BIGINT;
BEGIN
    SELECT value INTO cutover_text
    FROM system_settings
    WHERE key = 'AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT';

    IF cutover_text IS NULL OR btrim(cutover_text) = '' THEN
        RAISE EXCEPTION 'snapshot cutover refused: marker is missing or invalid';
    END IF;

    BEGIN
        cutover_at := cutover_text::timestamp;
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION 'snapshot cutover refused: marker is missing or invalid';
    END;

    IF cutover_at IS NULL OR cutover_at > clock_timestamp()::timestamp THEN
        RAISE EXCEPTION 'snapshot cutover refused: marker is null or in the future';
    END IF;

    SELECT COUNT(*) INTO invalid_orders
    FROM recharge_orders
    WHERE order_type = 'agent_inventory_purchase'
      AND (
          (payment_status = 'pending' AND pricing_snapshot_jsonb IS NULL AND (
              NOT agent_inventory_legacy_eligible
              OR agent_inventory_writer_generation IS DISTINCT FROM 1
          ))
          OR
          (pricing_snapshot_jsonb IS NOT NULL AND agent_inventory_writer_generation IS DISTINCT FROM 2)
      );

    IF invalid_orders <> 0 THEN
        RAISE EXCEPTION 'snapshot cutover refused: % empty-snapshot orders are outside the legacy allowlist', invalid_orders;
    END IF;
END $$;

-- sequence 是非 MVCC generation fence：它在 REPEATABLE READ 旧事务中也可见。
-- 放在所有可回滚验证之后、COMMIT 之前；若随后发生极罕见提交失败，结果是安全侧
-- fail-closed（旧 writer 被拒绝），部署脚本必须重试 activation，不得回滚旧 binary。
SELECT setval('agent_inventory_writer_generation_fence_seq', 2, TRUE);

COMMIT;

-- 幂等：重复执行保留首次 marker，且绝不把后来出现的空快照订单加入 allowlist。
-- 回滚：代码可继续兼容 marker 前 legacy 订单；不得删除 marker 或用新时间覆盖边界证据。
