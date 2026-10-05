-- ============================================================================
-- [v6 req2 · Deploy-CTO 2026-07-13] dispute_hold 订单托管(escrow)表
--   归属冲突订单 hold 期间把 base/bonus 反扣 user_wallets → escrow(不可消费)· 裁决时原子结算/退款。
-- 执行:
--   docker exec -i omnirank-db psql -U geo_admin geo_agentscope < scripts/migration_v6_dispute_escrow_2026_07_13.sql
-- 说明:代码侧 db/dispute_escrow_db.init_dispute_escrow_tables 亦幂等自建(随 init_wallet_tables 启动)。
-- 只覆盖 v6 上线后的新订单(历史已结算 snapshot 不可变 · 老板决策)。
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS dispute_escrow (
    id                  BIGSERIAL PRIMARY KEY,
    order_id            TEXT NOT NULL UNIQUE,
    dispute_id          BIGINT,
    customer_user_id    INTEGER NOT NULL,
    order_agent_user_id INTEGER NOT NULL,
    bound_agent_user_id INTEGER,
    amount_cents        INTEGER NOT NULL DEFAULT 0,
    base_points         INTEGER NOT NULL DEFAULT 0,
    bonus_points        INTEGER NOT NULL DEFAULT 0,
    pricing_snapshot    JSONB,
    status              TEXT NOT NULL DEFAULT 'held'
                        CHECK (status IN ('held','settled','refunded')),
    resolved_agent_user_id INTEGER,
    resolved_by         INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at         TIMESTAMPTZ
);
-- @index-guard idx_dispute_escrow_dispute ON dispute_escrow plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_dispute_escrow_dispute' AND i.indrelid = to_regclass('public.dispute_escrow')) THEN
        NULL;  -- 已在 public.dispute_escrow 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_dispute_escrow_dispute' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_dispute_escrow_dispute 已存在但不在 public.dispute_escrow 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_dispute_escrow_dispute' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_dispute_escrow_dispute ON public.dispute_escrow (dispute_id) WHERE status='held';
    END IF;
END $idxguard$;
-- @index-guard idx_dispute_escrow_customer ON dispute_escrow plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_dispute_escrow_customer' AND i.indrelid = to_regclass('public.dispute_escrow')) THEN
        NULL;  -- 已在 public.dispute_escrow 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_dispute_escrow_customer' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_dispute_escrow_customer 已存在但不在 public.dispute_escrow 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_dispute_escrow_customer' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_dispute_escrow_customer ON public.dispute_escrow (customer_user_id, status);
    END IF;
END $idxguard$;

INSERT INTO _migrations (name, applied_at)
VALUES ('v6_dispute_escrow_2026_07_13', NOW())
ON CONFLICT (name) DO NOTHING;

COMMIT;

-- ROLLBACK(仅在无 held escrow 时):
--   DROP TABLE IF EXISTS dispute_escrow;
--   DELETE FROM _migrations WHERE name='v6_dispute_escrow_2026_07_13';
