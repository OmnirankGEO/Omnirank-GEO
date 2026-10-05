-- ============================================================================
-- 回滚 · 双供应商同媒体择优路由(对应 migration_media_provider_routing_2026_08_02.sql)
--
-- 🔴 先读这段再执行 ————————————————————————————————————————————————
--
-- **绝大多数情况下你不需要这个文件。** 正常回滚顺序是:
--   1. `media_provider_routing_enabled` 置 '0'  → 路由立刻停,不再有新的改道单
--   2. `clear_catalog_dedupe()`                 → 目录复原(hidden_by_dedupe 全 FALSE)
--   3. 镜像回退到上一版
-- 迁移是**纯 additive** 的,代码回退后这些表/列只是没人读,不会让老代码报错。
-- 留着它们的代价是零,删掉的代价见下。
--
-- 🔴 本文件分三段,风险**递增**,请按需只跑前面的段:
--
--   段 1(安全)  停用 + 清目录隐藏 —— 可逆,不丢数据,任何时候都能跑
--   段 2(有损)  删 media_provider_equivalence 表 —— 丢掉映射表与人工核过的结论
--   段 3(危险)  删订单表三列 —— **只有在确认没有任何在途改道单时才可以**
--
-- 🔴🔴 段 3 的硬前提(不满足就是数据事故):
--   `routed_media_id` 是「这条 item 实际发去了哪家」的**唯一记录**。删掉它,
--   在途的改道单就永远认不出渠道 —— 快易播回流不再扫到它,状态停在 submitted,
--   24 小时后被判「未发布成功」退款,**可稿其实已经发出去、钱也已经付给对方了**。
--   所以段 3 自带 fail-closed 预检:有在途改道单就直接抛异常中止,不给你删。
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 段 1 · 停用 + 复原目录(安全 · 可逆 · 不丢数据)
-- ---------------------------------------------------------------------------
UPDATE mhz_config SET value = '0', updated_at = NOW()
 WHERE key = 'media_provider_routing_enabled';

UPDATE mhz_media   SET hidden_by_dedupe = FALSE WHERE hidden_by_dedupe;
UPDATE mhz_wemedia SET hidden_by_dedupe = FALSE WHERE hidden_by_dedupe;

-- 到这里为止:路由已停、目录已复原,用户看到的东西与上线前逐位相同。
-- 表和列还在,但没人读。**多数回滚到此为止即可。**


-- ---------------------------------------------------------------------------
-- 段 2 · 删映射表(有损:丢掉人工核过的 human_approved 结论)
--
-- ⚠️ 跑之前建议先导出留档:
--   \copy (SELECT * FROM media_provider_equivalence WHERE confidence='human_approved')
--     TO '/tmp/equivalence_human_approved_backup.csv' CSV HEADER
--
-- 🔴 启用方式:把行首的 `--!` 整体删掉(**只有 `--!` 开头的行是可启用语句**)。
--    普通 `--` 开头的是散文和示例,不要动 —— 上面那条 \copy 就是示例,
--    2026-08-02 dry-run 时我的脚本把它一起解注释,当场 syntax error。
-- ---------------------------------------------------------------------------
--! DROP INDEX IF EXISTS ix_media_provider_equivalence_kyb;
--! DROP INDEX IF EXISTS uq_media_provider_equivalence_mhz;
--! DROP TABLE IF EXISTS media_provider_equivalence;


-- ---------------------------------------------------------------------------
-- 段 3 · 删列(危险 · 自带 fail-closed 预检)
--
-- 🔴 启用方式同上:删掉行首 `--!`。预检不过会 RAISE EXCEPTION 中止整个事务,
-- 前面两段的效果也会一并回滚 —— 这是有意的:宁可什么都不做,也不要删一半。
-- ---------------------------------------------------------------------------
--! DO $$
--! DECLARE
--!     in_flight INT;
--!     dedupe_left INT;
--! BEGIN
--!     -- 预检 1:还有在途(未到终态)的改道单吗?
--!     SELECT count(*) INTO in_flight
--!       FROM mhz_publish_order_items
--!      WHERE routed_media_id IS NOT NULL
--!        AND status NOT IN ('published','failed','rejected','withdrawn','cancelled');
--!     IF in_flight > 0 THEN
--!         RAISE EXCEPTION
--!           '中止:还有 % 条在途改道单。删掉 routed_media_id 会让它们永远认不出渠道 → '
--!           '24 小时后被误判未发布并退款,而稿件其实已发出、钱已付给对方。'
--!           '请等它们全部到终态(或先把 media_provider_routing_enabled 置 0 后再等一个'
--!           '完整的 24 小时兜底周期)再来。', in_flight;
--!     END IF;
--
--!     -- 预检 2:段 1 真的跑过了吗?(反向对照:没跑过就说明执行顺序错了)
--!     SELECT count(*) INTO dedupe_left FROM (
--!         SELECT 1 FROM mhz_media   WHERE hidden_by_dedupe
--!         UNION ALL
--!         SELECT 1 FROM mhz_wemedia WHERE hidden_by_dedupe
--!     ) t;
--!     IF dedupe_left > 0 THEN
--!         RAISE EXCEPTION '中止:还有 % 条目录仍被 hidden_by_dedupe 隐藏,段 1 没跑。', dedupe_left;
--!     END IF;
--
--!     ALTER TABLE mhz_publish_order_items DROP COLUMN IF EXISTS routed_cost_yuan;
--!     ALTER TABLE mhz_publish_order_items DROP COLUMN IF EXISTS routed_media_id;
--!     ALTER TABLE mhz_publish_order_items DROP COLUMN IF EXISTS routed_provider;
--
--!     ALTER TABLE mhz_media   DROP COLUMN IF EXISTS hidden_by_dedupe;
--!     ALTER TABLE mhz_wemedia DROP COLUMN IF EXISTS hidden_by_dedupe;
--
--!     DELETE FROM mhz_config
--!      WHERE key IN ('routing_saving_share_to_user','media_provider_routing_enabled');
--
--!     RAISE NOTICE '段 3 完成:三列 + 两列 + 两个配置位已删除。';
--! END $$;
