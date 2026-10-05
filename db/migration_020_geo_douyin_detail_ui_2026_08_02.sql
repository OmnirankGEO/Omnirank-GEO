-- GEO 抖音图文 · 详情页交互补全所需的四个字段
-- 工单【前端工单 · AI 创作中心"制作 GEO 图文"详情页重做 + 交互补全】
-- Safe to run repeatedly（全部 ADD COLUMN IF NOT EXISTS，纯 additive，无破坏性 DDL）。
--
-- 为什么这四个字段必须落库，而不是放前端 state：
--
--   redraw_count      「重抽免费但每条累计限 10 次」是**后端强制**的额度。
--                     放前端 = 刷新页面即清零 = 无上限白嫖生图 API。
--                     工单原文:"计数落库,后端强制,不是前端数"。
--
--   style_key         四款风格影响的是【生图 prompt 模板】,再次创作时要按上次的风格
--                     续做;不落库则每次重做都退回默认风格。
--
--   contact_enabled   「插入联系方式」开关。它不是展示态 —— 收尾卡的**画面内容**
--                     取决于它,必须与作品同生命周期。
--
--   closing_stale     🔴 诚实位。开关一拨并不能改变一张【已经渲染好的 PNG】。
--                     置 TRUE 表示"当前收尾卡的画面与开关不一致,要重抽一次才生效",
--                     前端据此显式提示。不设这个位就只能假装联动(开关亮着但图没变)。
--
-- 🔴 PG 11+ 带 DEFAULT 的 ADD COLUMN 不重写全表;本表生产 23 列量级、行数很小,无锁风险。

ALTER TABLE geo_douyin_posts
    ADD COLUMN IF NOT EXISTS redraw_count    INTEGER     NOT NULL DEFAULT 0;

ALTER TABLE geo_douyin_posts
    ADD COLUMN IF NOT EXISTS style_key       VARCHAR(32);

ALTER TABLE geo_douyin_posts
    ADD COLUMN IF NOT EXISTS contact_enabled BOOLEAN     NOT NULL DEFAULT FALSE;

ALTER TABLE geo_douyin_posts
    ADD COLUMN IF NOT EXISTS closing_stale   BOOLEAN     NOT NULL DEFAULT FALSE;

-- 额度是"每条内容累计"，不是"每张卡"。加个下界约束防止被写成负数绕过上限。
-- （上限 10 走应用层配置，不写死在 CHECK 里 —— 改额度不该要迁移。
--   🔴 也不能往 CHECK 里塞允许值:往 CHECK 加允许值不是 additive,历史上栽过。）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_geo_douyin_posts_redraw_nonneg'
    ) THEN
        ALTER TABLE geo_douyin_posts
            ADD CONSTRAINT ck_geo_douyin_posts_redraw_nonneg CHECK (redraw_count >= 0);
    END IF;
END $$;
