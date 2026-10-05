-- ============================================================================
-- 048 · diagnosis_runs 落「冻结时的物理三池拆分快照」(P0-3b · WO 挂账 A)
-- ============================================================================
--
-- 为什么要这一列
-- ----------------------------------------------------------------------------
-- `middleware/billing.py:1611` 的部分扣费分支第一行就是:
--     if not _reserved_split: raise RuntimeError("organization settle requires immutable reserved split")
-- 而 `_actual_and_release_split` 还要一个权威 `order` —— 它决定**先扣哪个池、
-- 未履约的余额退回哪个池**。order 是冻结当时的钱包扣费偏好,事后从冻结行推不出来。
--
-- P0-3 因此只在「只有一个池出钱」时自动按比例扣(那时 order 排法不影响结果,可证),
-- 多池一律转人工(`reserved_split_order_unknown`)—— 猜 = 拿客户的赠送池当现金池扣。
--
-- 本列把 `freeze_points` **已经返回**的 `physical_split_snapshot`
-- (billing.py:1503,含权威 order)在冻结当时原样存下来,多池也就能自动按比例扣了。
-- 这与 organization 链把 split 存进 charge link、结算时原样回传
-- (services/organization_billing.py:1465)是**同一条「不可变预留快照」纪律**,不是新发明。
--
-- 顺序 / 依赖
-- ----------------------------------------------------------------------------
-- 🔴 无依赖:只碰 diagnosis_runs 一张既有表,不加 FK、不建索引(不按此列查询)。
--
-- 重放安全
-- ----------------------------------------------------------------------------
-- 🔴 prestart 每次部署**无条件重放全部迁移**(无追踪表)。本文件:
--    · 只有一条 `ADD COLUMN IF NOT EXISTS` → 天然幂等;
--    · **零 DML** —— 不回填、不 UPDATE 任何一行。存量 run 该列保持 NULL 是**有意**的:
--      NULL 的语义是「这一单冻结时没留下拆分快照」,`_reserved_split_for_run` 据此
--      回落到 P0-3 的既有逻辑(单池可证 → 自动;多池 → 转人工)。
--      绝不能拿「现在的冻结行」去补造历史 order —— 那正是本列要消灭的那种猜。
--
-- 漏跑后果(**刻意做成不响亮**)
-- ----------------------------------------------------------------------------
-- 🔴 与别的迁移相反,这一列漏跑**不会**让端点 500,而是静默回落到 P0-3 的行为:
--    多池降级单继续转人工。这是**有意**的 fail-safe —— 这条路径动的是钱,
--    "少自动化一点"永远好过"因为列不在就猜一个 order 出来"。
--    读取侧 `_reserved_split_for_run` 用 `run.get("reserved_split_snapshot_jsonb")`
--    (get_run 是 SELECT *),列不存在时取到 None,与"列在但为 NULL"同一条分支。
--
-- 回滚
-- ----------------------------------------------------------------------------
-- 🔴 回滚 = `ALTER TABLE diagnosis_runs DROP COLUMN reserved_split_snapshot_jsonb;`
--    零业务残留(列里只有冻结当时的池金额与顺序,不是审计事实)。
--    但**不放进部署清单自动做** —— 回滚后在途的多池降级单会从"自动按比例"退回
--    "转人工",属于行为回退,该由人确认。
-- ============================================================================

ALTER TABLE diagnosis_runs
    ADD COLUMN IF NOT EXISTS reserved_split_snapshot_jsonb jsonb;

COMMENT ON COLUMN diagnosis_runs.reserved_split_snapshot_jsonb IS
    'freeze_points 返回的 physical_split_snapshot 原样快照(bonus/commission/paid + 权威 order)。'
    'NULL = 该 run 冻结时未留快照(存量),结算侧回落单池可证/多池转人工。P0-3b WO 挂账 A(编号 048:040-047 已被冻结的防御 GEO 班列占用)。';
