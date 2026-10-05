-- ============================================================
-- 财务中心 · operating_expenses 运营成本表 · GEO CTO-15.23 · 2026-05-30
-- 唯一新表(纯新增 · 幂等 · 不碰任何现有表/扣费逻辑)
-- Deploy 顺序:先跑此 migration(建表+seed)再 rebuild code
-- ============================================================

-- 1. 建表(幂等)
CREATE TABLE IF NOT EXISTS operating_expenses (
    id SERIAL PRIMARY KEY,
    period_month DATE NOT NULL,                 -- 归属月(存 YYYY-MM-01)
    category TEXT NOT NULL,                      -- server/bandwidth/domain/labor/other
    amount_cents INTEGER NOT NULL DEFAULT 0,    -- 分(与全库金额口径一致)
    note TEXT,
    created_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_opex_period ON operating_expenses(period_month);

-- 2. 示例 seed(老板 2026-05-30 授权随意录 · 保证功能可见)
--    6 月=正式起算月 · 5 月=测试月 · note 标"示例" · admin 可在 /admin/finance 改/删
--    仅当表为空时插入(防重复跑)
INSERT INTO operating_expenses (period_month, category, amount_cents, note, created_by)
SELECT * FROM (VALUES
    ('2026-06-01'::date, 'server',    300000, '云服务器(示例·待核实)',   NULL::integer),
    ('2026-06-01'::date, 'bandwidth',  80000, '带宽/CDN(示例·待核实)',   NULL::integer),
    ('2026-06-01'::date, 'domain',     10000, '域名(示例·年费折月)',     NULL::integer),
    ('2026-06-01'::date, 'labor',    4000000, '人力(示例·待核实)',       NULL::integer),
    ('2026-06-01'::date, 'other',     200000, '固定 API 订阅等(示例)',   NULL::integer),
    ('2026-05-01'::date, 'server',    300000, '测试月示例数据',           NULL::integer),
    ('2026-05-01'::date, 'bandwidth',  80000, '测试月示例数据',           NULL::integer),
    ('2026-05-01'::date, 'domain',     10000, '测试月示例数据',           NULL::integer),
    ('2026-05-01'::date, 'labor',    4000000, '测试月示例数据',           NULL::integer),
    ('2026-05-01'::date, 'other',     200000, '测试月示例数据',           NULL::integer)
) AS seed(period_month, category, amount_cents, note, created_by)
WHERE NOT EXISTS (SELECT 1 FROM operating_expenses);

-- 3. 验证
DO $$
DECLARE v_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM information_schema.tables WHERE table_name = 'operating_expenses';
    IF v_count <> 1 THEN
        RAISE EXCEPTION 'finance opex migration FAILED: operating_expenses 表未建';
    END IF;
    RAISE NOTICE 'operating_expenses 就绪 · 当前行数 = %', (SELECT COUNT(*) FROM operating_expenses);
END $$;
