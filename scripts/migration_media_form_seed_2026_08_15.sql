-- [R2-7 · 2026-08-15] media_form 种子分档(高频域名)。
-- 来源 = config/verified_domain_carriers.json(R7 人工核过的载体映射,27 条,
-- 覆盖生产被引 Top 域:搜狐/网易/腾讯/知乎/博客园/人民网/澎湃 等)。
-- 🔴 轴区分(裁定 §4-3):本表 media_form 是**载体形态轴**(发布渲染用);
--    飞轮 STEM_DEMOTED_NON_PLATFORM 是**名称证据轴**(实体名可信度用)——
--    同一域名(如 jia.com)在两轴各有归类,互不解释,勿混用。
-- 档位派生规则(确定性):official_media / portal_media(真编辑媒体)→ portal_site;
-- platform_ugc(自发布频道)→ platform_account。self_site / vertical_media 不种
-- (需逐域人工判断,走 admin 治理入口)。
--
-- 幂等 + 只加注不删:
--   · 新域名 INSERT(source='builtin',zh_name 取载体名);
--   · 已有域名 ON CONFLICT 只在 media_form IS NULL 时补档 —— admin 已订正的
--     (source='admin' 或已有档)绝不覆盖,zh_name/one_liner 一概不动;
--   · prestart 每次部署重放安全(重放第二遍 0 行变化)。

INSERT INTO media_domain_directory (domain, zh_name, one_liner, source, media_form)
VALUES
    ('163.com', '网易新闻', '', 'builtin', 'platform_account'),
    ('36kr.com', '36氪', '', 'builtin', 'portal_site'),
    ('baijiahao.baidu.com', '百家号', '', 'builtin', 'platform_account'),
    ('baike.baidu.com', '百度百科', '', 'builtin', 'platform_account'),
    ('bilibili.com', '哔哩哔哩', '', 'builtin', 'platform_account'),
    ('cctv.com', '央视网', '', 'builtin', 'portal_site'),
    ('cnblogs.com', '博客园', '', 'builtin', 'platform_account'),
    ('csdn.net', 'CSDN', '', 'builtin', 'platform_account'),
    ('douban.com', '豆瓣', '', 'builtin', 'platform_account'),
    ('ifeng.com', '凤凰网', '', 'builtin', 'platform_account'),
    ('jia.com', '齐家网', '', 'builtin', 'platform_account'),
    ('jianshu.com', '简书', '', 'builtin', 'platform_account'),
    ('jiemian.com', '界面新闻', '', 'builtin', 'portal_site'),
    ('mp.weixin.qq.com', '微信公众号', '', 'builtin', 'platform_account'),
    ('people.com.cn', '人民网', '', 'builtin', 'portal_site'),
    ('qcc.com', '企查查', '', 'builtin', 'platform_account'),
    ('qq.com', '腾讯网', '', 'builtin', 'platform_account'),
    ('sina.cn', '新浪网', '', 'builtin', 'platform_account'),
    ('sina.com.cn', '新浪网', '', 'builtin', 'platform_account'),
    ('sohu.com', '搜狐网', '', 'builtin', 'platform_account'),
    ('thepaper.cn', '澎湃新闻', '', 'builtin', 'portal_site'),
    ('toutiao.com', '今日头条', '', 'builtin', 'platform_account'),
    ('xiaohongshu.com', '小红书', '', 'builtin', 'platform_account'),
    ('xinhuanet.com', '新华网', '', 'builtin', 'portal_site'),
    ('zhidao.baidu.com', '百度知道', '', 'builtin', 'platform_account'),
    ('zhihu.com', '知乎', '', 'builtin', 'platform_account'),
    ('zhuangyi.com', '装一网', '', 'builtin', 'platform_account')
ON CONFLICT (domain) DO UPDATE
   SET media_form = EXCLUDED.media_form,
       updated_at = NOW()
 WHERE media_domain_directory.media_form IS NULL;
