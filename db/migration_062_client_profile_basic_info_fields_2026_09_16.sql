-- 062 · client_profiles.basic_info_fields(WO_220-c2)
-- 写作大厅「补全知识库」基础资料表六字段的**后端落点**。一列可空,**零 DML**。
--
-- ═══════════════════════════════════════════════════════════════════
-- 🔴 为什么只有两个字段住这里,另外四个复用既有列
--
-- 六字段先逐个**真写真读**验过(探针 scratchpad/wo220_roundtrip_probe.py,
-- 写一段带顿号和逗号的自由文本再 SELECT 回来比对):
--
--   target_customers   -> target_users        ✅ 逐字相同
--   key_selling_points -> selling_points      ✅ 逐字相同
--   forbidden_notes    -> brand_constraints   ✅ 逐字相同
--   business_summary   -> business_summary    ✅ 逐字相同(列一直在,只是不在写白名单里)
--   products_services  -> products            🔴 被按顿号/逗号**切碎成数组**
--   proof_cases        -> success_cases       🔴 同上
--
-- 「我们做租车、代驾,还有商务接送,不承诺最低价」写进 products 会变成
-- ["我们做租车","代驾","还有商务接送","不承诺最低价"] —— 「不承诺最低价」
-- 凭空成了一个独立条目。切碎发生在 `db/profile_db.py` 的 `array_fields` 分支
-- (`update_profile` 这条路)。
--
-- 🔴 措辞要准:**不要写成「那两列是数组」**。它们的形态**取决于写入方** ——
--    `_json_text_or_none` 只在调用方传 list/dict 时才 `json.dumps`,传 str 原样存;
--    所以生产上一部分档案里是字符串、一部分是 JSON。
--    不复用它们的理由是「形态不定 + 这条写入路会切碎自由文本」,不是「它是个数组」。
--    把形态说死,会让下一个人按错误的前提去改它。
--
-- 🔴 不去改 products 的数组语义:那是**有意的** ——
--    客户档案页按数组用它(`MarketingTab.tsx:193` `match.products || []`),
--    后端 44 份、前端 26 份在读。把一张自由文本表单的需求倒灌进去,
--    等于替那 70 份代码改了它们看到的形状。
--
-- 🔴 也不把六个字段**全部**塞进本列:那会让 target_users / selling_points /
--    brand_constraints 有**两个住处**。客户档案页(MarketingTab)今天就在用
--    自由文本框编辑这三列 —— 两个面本来就该共享同一份事实,这正是本单要的。
--    同一谓词两个住处,迟早有一处没人验。
--
-- ⇒ 有同语义真列的复用真列,没有的才住这里。本列**只放没有去处的那些**。
--
-- 🔴 不回填:老档案里没有「基础资料表」这个概念,猜不出用户当初会往
--    products_services / proof_cases 里写什么。缺省 '{}' 表示「建于本迁移前,
--    没人填过」,比编一个值诚实。
--
-- 🔴 JSONB 不是 TEXT:字段会增(六个是今天的口径,不是终局),
--    而每加一个就加一列的话,`client_profiles` 已经 80 列了。
-- ═══════════════════════════════════════════════════════════════════

ALTER TABLE client_profiles
    ADD COLUMN IF NOT EXISTS basic_info_fields JSONB DEFAULT '{}'::jsonb;

COMMENT ON COLUMN client_profiles.basic_info_fields IS
    '写作大厅基础资料表里**没有同语义真列**的字段(products_services / proof_cases)。'
    '其余四个字段各自复用真列:business_summary / target_users / selling_points / brand_constraints。'
    '键名 = 表单字段名。详见 migration_062 抬头。';
