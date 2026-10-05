-- 迁移 · 手动监测词纳入开关体系,默认关
-- WO_MONITORING_OPTIN_DEFAULT_OFF_2026-08-15 · P0-A.1
-- Owner 2026-08-15:「自动检测一定要手动点开才行,毕竟牵涉到扣费,默认关闭的即可」
--
-- 背景(生产实证 2026-08-15):
--   extra_keywords 15 列,没有 is_monitored;唯一闸是 status,而库默认就是 'active'
--   → 一加词就进该品牌的每一次监测批次,按 monitor_single 130 算力/词/次 扣,界面无开关可关。
--   confirmed_keywords 那边早有 is_monitored(default false),两侧口径分裂 = 本次修的第二现场。
--
-- 🔴 默认只能是 FALSE。这是本单核心要求,不许给 TRUE。
--    存量 6 个真客户在跑的词(brand 289 × 5 / 662 × 1)由**独立幂等脚本**回填:
--      scripts/backfill_monitoring_extra_optin_2026_08_15.py
--    绝不在这里写 UPDATE —— prestart 每次部署会无条件重放全部迁移 SQL(无追踪表),
--    迁移里的数据 UPDATE 会在每次部署把代理手动关掉的词重新打开(工单红线 6)。
--
-- 🔴 顺序无依赖:只 ADD COLUMN IF NOT EXISTS 到 extra_keywords 一张既有表,不动任何既有列。
-- 🔴 漏跑后果**响亮**(刻意):取词 SQL 显式引用 ek.is_monitored → UndefinedColumn 抛出、
--    监测取词当场失败。不选静默 —— 「以为默认关了其实照跑照扣」正是这次事故的形态。
-- 🔴 ADD COLUMN NOT NULL DEFAULT FALSE 的老数据效应是**故意**的:
--    存量行立刻拿到 FALSE、新过滤条件立刻生效 = 默认关立刻成立。
--    因此部署后必须**紧接着**跑上面那个回填脚本,否则 289/662 在窗口期内
--    「跑全部」取不到这 6 个词(显式勾选单跑仍可用 —— 闸不拦显式点名)。

ALTER TABLE extra_keywords
    ADD COLUMN IF NOT EXISTS is_monitored BOOLEAN NOT NULL DEFAULT FALSE;

-- 审计:谁在什么时候把它打开的(工单红线 4「开启动作要能被审计」)。
-- 可空 —— 存量行没有开启动作,不该编造一个时间/人。
ALTER TABLE extra_keywords
    ADD COLUMN IF NOT EXISTS monitoring_enabled_at TIMESTAMP;
ALTER TABLE extra_keywords
    ADD COLUMN IF NOT EXISTS monitoring_enabled_by INTEGER;

-- §验收(部署后手工跑这三条 · 不跑就不算 migrated):
--   1) 列在:
--      SELECT column_name, data_type, is_nullable, column_default
--        FROM information_schema.columns
--       WHERE table_schema='public' AND table_name='extra_keywords'
--         AND column_name IN ('is_monitored','monitoring_enabled_at','monitoring_enabled_by');
--      期望 is_monitored / boolean / NO / false
--   2) 默认真的是关(回填**之前**跑这条,期望 0):
--      SELECT count(*) FROM extra_keywords WHERE is_monitored = TRUE;
--   3) 反向对照(证明上面那个 0 不是因为表空,期望 14):
--      SELECT count(*) FROM extra_keywords;
