-- =====================================================================
-- migration_016: 报价系数第二步 · 无 owner 的 L0 自设报价覆盖系数 flag(默认 OFF)
-- =====================================================================
-- 背景:
--   报价系数第二步 = 无 owner 的孤立 L0 普通用户可自设报价覆盖系数 + 防历史残留脏数据。
--   代码侧 config/v3_3_1_flags.py 的 _FLAG_DEFAULTS 已兜底 False;本 migration 仅向
--   system_settings 注入该 flag,让 admin 后台可灰度开启。
--   default = false → 上线 0 影响(无 owner 的 L0 报价回落系统默认 · 防残留 quote_markup_ratio 生效)。
--
-- flag 名【故意无 V3_3_1_ 前缀】→ 代码走 get_flag 读取(env>DB>default)· 与 V3.3.1 总开关解耦。
-- 范围:仅 1 行 INSERT · 不动任何表结构 · 不动 users / user_wallets / 业务表 · 幂等重跑 0 ERROR。
-- 红线:不动 schema · 0 影响现有 17+ flag。
-- =====================================================================

BEGIN;

-- system_settings 表(若 007/008 migration 未上 · 兼容建表)
CREATE TABLE IF NOT EXISTS system_settings (
    key          VARCHAR(100) PRIMARY KEY,
    value        TEXT,
    value_type   VARCHAR(20) DEFAULT 'string',  -- string / int / float / bool / json
    description  TEXT,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_by   INTEGER
);

-- 注入 flag(默认 false · 不影响生产)
INSERT INTO system_settings (key, value, value_type, description) VALUES
    ('L0_QUOTE_MARKUP_SELF_EDIT_ENABLED', 'false', 'bool',
     '报价系数第二步 · 无 owner 的 L0 普通用户是否可自设报价覆盖系数 · '
     'false=无 owner L0 报价回落系统默认(防残留脏数据)· true=自设生效(灰度)')
ON CONFLICT (key) DO UPDATE SET
    description = EXCLUDED.description,
    updated_at  = CURRENT_TIMESTAMP;
    -- 注:只更 description/updated_at · 不覆盖 value → admin 已手动开过(value='true')重跑不会被重置

COMMIT;

-- =====================================================================
-- 验收 SQL(部署后跑)
-- =====================================================================
-- 1. flag 已注入(默认安全 false)
--    SELECT key, value, value_type FROM system_settings WHERE key='L0_QUOTE_MARKUP_SELF_EDIT_ENABLED';
--    必须见 1 行 · value='false'
--
-- 2. admin 灰度开启(⚠️ 开之前先确认无 owner 的 L0 残留 quote_markup_ratio 已清回 NULL):
--    UPDATE system_settings SET value='true', updated_at=CURRENT_TIMESTAMP
--      WHERE key='L0_QUOTE_MARKUP_SELF_EDIT_ENABLED';
--    (get_flag 30s 缓存 · 或调 config.v3_3_1_flags.clear_cache() 立即生效)
--
-- 3. 紧急回滚(两种任一):
--    a. UPDATE system_settings SET value='false' WHERE key='L0_QUOTE_MARKUP_SELF_EDIT_ENABLED';
--    b. 设环境变量 L0_QUOTE_MARKUP_SELF_EDIT_ENABLED=false(env 优先级最高 · 立即生效)
