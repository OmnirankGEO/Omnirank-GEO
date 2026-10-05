-- GEO 抖音图文 · AI 一键蒸馏选题定价(Owner 2026-08-03 拍板「AI蒸馏收130算力」)
--
-- 交付单 §3.4 原本写的是"蒸馏不收费,待 Owner 拍" —— Owner 已拍:**收 130**。
-- 本条只新增一个 feature_code,不动 018/019/021 建的任何一行。
--
-- 🔴 为什么单独一条迁移而不是改 021:
--    021 的头部注释逐字写着"两条都是新增 feature_code",内容与注释是对上的。
--    往里塞第三行会让那份注释当场变成谎话,而下一个人读迁移多半只读注释。
--    新增一条,让"哪次拍板加了哪一行"在文件名上就能读出来。
--
-- 🔴 计费形态是 **A 类同步短任务**(charge_on_success:成功才扣),
--    不是 B 类冻结 —— 蒸馏是一次同步 LLM 调用,产出立刻返回,
--    没有"任务在后台跑、要占位"这回事。用冻结反而多一次可能失败的资金动作。
--    失败(LLM 挂 / JSON 坏 / 选题全被串味丢光)一律抛异常 → **一分不扣**。
--
-- ⚠️ cost_compute 取值:沿用 021 里核过的口径 —— 同量级样本中 130 → 1.00
--    (即 cost_points/130 的人民币金额)。该列在 middleware/billing.py 里**零引用**
--    (021 已 grep 实核),不参与任何扣费判定。
--
-- 幂等:INSERT ... ON CONFLICT DO UPDATE,可反复连跑。

INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
VALUES ('geo_douyin_topic_distill', 'GEO 图文 AI 一键蒸馏选题', 130, 1.00, FALSE)
ON CONFLICT (feature_code) DO UPDATE
SET feature_name = EXCLUDED.feature_name,
    cost_points = EXCLUDED.cost_points,
    cost_compute = EXCLUDED.cost_compute,
    requires_paid_points = EXCLUDED.requires_paid_points,
    is_active = TRUE;
