-- [P1-3 媒体形态分档 · 2026-08-14] media_domain_directory.media_form
--
-- 研究定稿缺口:publication_profile(生成时 standard/strict 两档)与
-- mhz_media.portal_media(下单选媒)「从不相遇」,渠道渲染只做联系方式软化,
-- 没有按媒体形态的署名/口吻适配 —— 「据搜狐网报道」类假第三方形态的分档判据
-- 一直悬空。本迁移给域名目录加**形态档**:
--   portal_site      门户主站(真编辑媒体主站)
--   platform_account 平台号(搜狐号/百家号/企鹅号等自发布频道)
--   vertical_media   垂直媒体(行业站/垂直编辑媒体)
--   self_site        自站(企业自有站点)
--   NULL             未分档(fail-closed:渲染层不产出形态化署名)
--
-- 幂等:ADD COLUMN IF NOT EXISTS + DO $$ 守卫 CHECK;无数据 UPDATE
-- (prestart 每次部署重放安全)。分档值由 admin 治理入口逐条订正
-- (source='admin' 优先级既有语义不变),不由迁移代拍。

ALTER TABLE media_domain_directory
    ADD COLUMN IF NOT EXISTS media_form VARCHAR(24) DEFAULT NULL;

DO $media_form_check$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_media_domain_directory_media_form'
    ) THEN
        ALTER TABLE media_domain_directory
            ADD CONSTRAINT chk_media_domain_directory_media_form
            CHECK (media_form IS NULL OR media_form IN
                   ('portal_site', 'platform_account', 'vertical_media', 'self_site'));
    END IF;
END
$media_form_check$;

COMMENT ON COLUMN media_domain_directory.media_form IS
    'P1-3 媒体形态档:portal_site/platform_account/vertical_media/self_site;NULL=未分档(渲染层 fail-closed 不产出形态化署名)。admin 治理入口可改。';
