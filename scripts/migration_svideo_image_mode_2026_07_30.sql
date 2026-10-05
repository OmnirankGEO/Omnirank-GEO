-- ============================================================================
-- [T5 · 短视频开闸包 · 2026-07-30] mhz_short_video_drafts 补 图文笔记 两列
-- ============================================================================
-- 背景(真环境采样 SAMPLE_RESULT_SVIDEO_UPLOAD_AND_IMAGE_MODE_2026-07-30.md §3)：
--   媒介盒子 save.html **同一个 endpoint、无新字段**即可发图文笔记，差异只有三处：
--     article_type 1→3 · video_url 留空 · image_urls 多图英文逗号分隔。
--   我们的 draft 占位表(订单以 -draft.id 作 article_id)此前只存 video_url，
--   图文模式的素材没有落点 → 重投/回流重建 payload 拿不到图片，
--   且 find_recent_svideo_duplicates 只按 video_url 匹配 → 图文模式**完全没有重复提交拦截**
--   (同一组图连点几次各自成单各自扣费)。
--
-- 🔴 为什么必须有这个迁移文件、而不是只改 db/meijiehezi_db.py 里的建表语句：
--   那里是 `CREATE TABLE IF NOT EXISTS`，对**存量表是 no-op** —— 只改那里等于
--   "新库有这两列、生产库永远没有"，然后 INSERT 才在运行时炸。
--   (同型事故已发生过：自愈式建表不跑不建且静默。)
--
-- 性质：纯 additive · 幂等 · 可反复连跑 · 不改任何既有列/约束/数据。
--   漏跑的表现是短视频下单 INSERT 报 column does not exist(响亮失败)，不是静默降级；
--   下方自验会让漏跑在 prestart 阶段就非零退出，容器不会带残缺 schema 起来。
-- 依赖：只依赖 mhz_short_video_drafts 基表已存在(2026-07-04 短视频 lane 建)。
--   置 manifest 文件尾：不被任何前置迁移依赖。
-- ============================================================================

-- 基表不存在 → 说明短视频 lane 的建表还没跑过，本迁移无对象可加。
-- 此时不建表(建表口径的 SSOT 在 db/meijiehezi_db.py，这里建会造出第二份形状定义)，
-- 直接跳过；下方自验同样按"表存在才验列"的口径，不误伤全新库的首次部署顺序。
DO $$
BEGIN
    IF to_regclass('public.mhz_short_video_drafts') IS NULL THEN
        RAISE NOTICE '[svideo-image-mode] mhz_short_video_drafts 尚不存在，跳过补列';
        RETURN;
    END IF;

    ALTER TABLE mhz_short_video_drafts
        ADD COLUMN IF NOT EXISTS article_type INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE mhz_short_video_drafts
        ADD COLUMN IF NOT EXISTS image_urls TEXT NOT NULL DEFAULT '';
END $$;

-- ── 后置自验(fail-closed)：表在但列没加上 → RAISE EXCEPTION → prestart 非零退出 ──
-- 只验"列在不在 + 类型对不对"，不验默认值文本(不同 PG 版本 pg_get_expr 渲染有差)。
DO $$
DECLARE
    v_article_type_type TEXT;
    v_image_urls_type   TEXT;
BEGIN
    IF to_regclass('public.mhz_short_video_drafts') IS NULL THEN
        RETURN;
    END IF;

    SELECT data_type INTO v_article_type_type
      FROM information_schema.columns
     WHERE table_schema = 'public'
       AND table_name = 'mhz_short_video_drafts'
       AND column_name = 'article_type';

    SELECT data_type INTO v_image_urls_type
      FROM information_schema.columns
     WHERE table_schema = 'public'
       AND table_name = 'mhz_short_video_drafts'
       AND column_name = 'image_urls';

    IF v_article_type_type IS NULL THEN
        RAISE EXCEPTION '[svideo-image-mode] 自验失败：mhz_short_video_drafts.article_type 未建立';
    END IF;
    IF v_article_type_type <> 'integer' THEN
        RAISE EXCEPTION '[svideo-image-mode] 自验失败：article_type 类型是 % 不是 integer', v_article_type_type;
    END IF;
    IF v_image_urls_type IS NULL THEN
        RAISE EXCEPTION '[svideo-image-mode] 自验失败：mhz_short_video_drafts.image_urls 未建立';
    END IF;
    IF v_image_urls_type <> 'text' THEN
        RAISE EXCEPTION '[svideo-image-mode] 自验失败：image_urls 类型是 % 不是 text', v_image_urls_type;
    END IF;

    RAISE NOTICE '[svideo-image-mode] ✅ 自验通过：article_type(integer) + image_urls(text) 就位';
END $$;
