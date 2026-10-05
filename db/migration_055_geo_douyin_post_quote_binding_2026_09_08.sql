-- 055 · 图文成品记账落到**词**上(#150 §3.2)
-- =============================================================================
-- `/plan` 的 done 今天按 `geo_douyin_posts.keyword` **字符串**归集
-- (`content_plan.py` 自己就标注了那是上界口径)。多张报价买同一个词时,
-- A 报价做了 3 条,B 报价同词的进度**跟着一起涨** —— 两边各自看都正常,
-- 客户按进度以为交付够了、实际没够。
--
-- 本迁移只加**一列**:`confirmed_keyword_id`(算在报价里的哪一个词上)。
--
-- 🔴🔴 `quote_id` **不在本迁移里** —— 它生产上早就有:
--    `db/migration_034_geo_image_note_contract_2026_08_17.sql`(08-17)加的,
--    类型 **bigint**(09-05 生产 dump 实证)。
--    我第一版把它一起加了,而且写成 INTEGER —— 那次 4 维核验跑在**夹具建的库**上,
--    不是生产 schema。「验了」不等于「验对了对象」:同一句「夹具不是真相」
--    我一边写在注释里、一边正在犯。写入侧沿用既有的 bigint 列,本迁移不碰它。
--
-- 🔴🔴 **不建 `idx_geo_douyin_posts_quote`** —— 生产上已存在同名索引,
--    但定义**不同**:`(quote_id, contract_revision_id, batch_item_ordinal)
--    WHERE quote_id IS NOT NULL AND deleted_at IS NULL`。
--    `CREATE INDEX IF NOT EXISTS` 的存在性守卫**只看名字**,定义不同照样跳过 ——
--    于是「我以为建了、其实没建」,而且没有任何东西会说话(本仓老病:
--    按名判存 ≠ 按身份判存)。既有那条比我要建的更严(多两个维度 + 排除软删),
--    本单需要的报价维度归集它已经覆盖。
--
-- 🔴 类型 INTEGER:生产 schema 实证 `confirmed_keywords.id` 是 integer(SERIAL)。
--    (`geo_douyin_posts.quote_id` 是 bigint 而 `quotes.id` 是 integer —— 那是 034
--     留下的既有加宽,不是本单要收拾的东西。)
--
-- 🔴 **零 DML**:不回填历史行。历史成品本来就不知道自己属于哪一个词,
--    回填只能靠「同词即同报价」去猜 —— 而本迁移存在的理由正是那个猜法会串。
--    NULL 在这里是**诚实的"未知"**,不是缺陷。
--
-- 🔴 不加外键:确认词可软删除,硬外键会把「词没了但成品还在」这种合法历史状态
--    变成删不掉的行;加 FK 还要锁被引用表;级联语义没人拍过板,
--    不该由一条 additive 迁移顺手替 Owner 决定。
--
-- 🔴 漏跑后果**响亮**(刻意):写入侧显式写这一列 → 列不在即 UndefinedColumn
--    当场抛,不会退化成"成品建出来了但没记账"。
--
-- 依赖:db/migration_017_geo_douyin_posts_2026_08_01.sql(建表)· 034(quote_id)。
-- 号段:054 归工单 E3 · **055 归本单** · 下一空 = 056。
-- =============================================================================

ALTER TABLE geo_douyin_posts
    ADD COLUMN IF NOT EXISTS confirmed_keyword_id INTEGER;

-- 词维度的归集索引。名字在生产上未被占用(09-05 dump 实证),
-- 所以这条 `IF NOT EXISTS` 是真的会建出来,不是静默跳过。
-- 只索引非空行:历史行全是 NULL,收进索引对"按词归集"没有任何区分力。
CREATE INDEX IF NOT EXISTS idx_geo_douyin_posts_confirmed_keyword
    ON geo_douyin_posts (confirmed_keyword_id)
    WHERE confirmed_keyword_id IS NOT NULL;
