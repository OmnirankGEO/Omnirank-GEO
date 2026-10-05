-- GEO 抖音图文 · 画幅可自选(杂志级连续组图规范 §8.8)
--
-- 规范原文:「本 3:4 系列是**新增一族,不替代** 9:16 十母版 —— 两族并存,
--   系统默认规划、客户可自选(Owner 08-02 已拍)。」
-- 在此之前生图画幅是代码里写死的 `_IMAGE_SIZE = "3:4"`,"可自选"那一层不存在。
--
-- 🔴 为什么要落成一列而不是塞 generation_meta:
--    重抽单张 / 整条重做都要按**这条内容原来的画幅**出图。塞进 jsonb 的话,
--    每次重写 generation_meta 都可能把它冲掉 —— 然后同一条内容里
--    新抽的那张和旧的几张画幅不一样,组内一致性当场破。
--    它和 style_key 是同一类东西(整组共享的产出属性),style_key 就是一列。
--
-- 🔴 DEFAULT '3:4' + 存量行回填同值:上线后老内容行为**逐字不变**。
-- 🔴 白名单在代码(services/geo_douyin/config.ASPECT_RATIOS),这里**不加 CHECK**:
--    往 CHECK 里加允许值不是 additive 变更(踩过:两槽失去可启动性而公网照常),
--    而画幅将来大概率会加第三种。列上只存字符串,合法性由 normalize_aspect_ratio
--    在写入前收敛 —— 不认识的值落默认,存不进脏值。
--
-- 幂等:IF NOT EXISTS,可反复连跑。

ALTER TABLE geo_douyin_posts
    ADD COLUMN IF NOT EXISTS aspect_ratio TEXT NOT NULL DEFAULT '3:4';
