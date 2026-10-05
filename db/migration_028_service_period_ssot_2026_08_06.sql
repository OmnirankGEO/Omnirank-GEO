-- migration_028 · 服务期双钟分裂根治(WO_SERVICE_PERIOD_SSOT_2026-08-06 §1.2)
--
-- 这条迁移只干一件事:**把"隐式兜底"赖以存在的那个洞堵上**。
--
-- 病理:`quotes.service_days` 可空 → 全站读点长出五处 `COALESCE(service_days, 365)`
--       / `service_days or 365`。而这一列的库默认值本来就是 365,生产 392 行全部非空
--       (2026-08-06 只读实测:365→380 行 / 30→8 行 / 180→3 行 / 1→1 行,合计 392 = 全表)。
--       也就是说那些兜底**从未真正触发过**,却让"服务期至 = 起始日 + 365 天"这口假钟
--       在 11/13 张 paid 报价上跑了三个月。
--
-- 修法:回填(生产预期 0 行)+ SET NOT NULL。约束立住之后,代码里的兜底就是**可证明的
--       死代码**,才能安全删干净 —— 否则删了兜底会把 NULL 行静默踢出监测池,比留着更糟。
--
-- 🔴 幂等:回填是 WHERE IS NULL(重跑 0 行);SET NOT NULL 对已 NOT NULL 的列重跑无副作用。
-- 🔴 本条**不碰** service_start_date / service_end_date / service_months 的可空性:
--    那三列在 draft 态天然为空(生产 392 行里大量 draft 的 start/end 是 NULL),
--    置 NOT NULL 会当场炸掉所有草稿报价。服务期"必须显式"是**激活时**的规则,
--    由 services/service_period.py 在写入点 fail-closed 保证,不是列约束能表达的。
-- 🔴 漏跑这条的后果是**响亮的**:代码侧已经删掉全部 365 兜底,
--    若真有 NULL 行,读点会拿到 None 并显式跳过 + 告警,不会静默按 365 糊过去。
--    (选响亮方向是刻意的:静默按 365 糊,正是本次事故本身。)

BEGIN;

-- 1) 回填:生产预期影响 0 行。留着是为了让这条迁移在任何环境(含测试库/新库)都能立住约束。
UPDATE quotes
   SET service_days = 365
 WHERE service_days IS NULL;

-- 2) 立约束 —— 兜底代码从此可证明是死代码
ALTER TABLE quotes ALTER COLUMN service_days SET NOT NULL;

-- 3) 把语义写进库里(下一个人读 \d+ quotes 就知道这两个东西不是一回事)
COMMENT ON COLUMN quotes.service_start_date IS
  '合同自然日历服务期 · 起始日 · 与 service_end_date 成对,是服务期唯一 SSOT(services/service_period.py)';
COMMENT ON COLUMN quotes.service_end_date IS
  '合同自然日历服务期 · 结束日 · 服务期唯一 SSOT。倒计时/轮换资格闸/续费提醒/门户token 全读这一列';
COMMENT ON COLUMN quotes.service_months IS
  '仅算 service_end_date 的入参(月数)· 不是第二个真相 · 禁止任何读点拿它反推服务期';
COMMENT ON COLUMN quotes.service_days IS
  '履约达标天数配额(累计达标天数 · Owner A 方案 2026-06-04)· 单位不是日历天 · 严禁加到日期上算"服务期至"';

COMMIT;
