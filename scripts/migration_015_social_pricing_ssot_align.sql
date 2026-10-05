-- migration_015_social_pricing_ssot_align.sql
-- v1.7.6.3 P0 紧急 · 社媒定价 SSOT 对齐(2026-05-24 老板拍板)
--
-- 真因:
--   .planning/phases/07-social-studio-subscription/PRICING_DECISIONS.md 锁 capability 维度 9 档加量单价
--   SubscriptionPlansPage.tsx:685 向用户公开承诺 "¥0.04-¥15 单价"
--   但 feature_pricing 表从未按 SSOT UPDATE · 实扣走 GEO 老表 04-11 align 旧值
--   script_gen 一次扣 ¥5 vs 用户预期 ¥0.31 = 差 16 倍 · 消费者保护红线 · MCN beta hold
--
-- 修法:
--   全程 UPSERT 不 UPDATE(老库可能缺 video_asr / industry_brief_rerun 等行 · UPDATE 静默 0 行)
--   配套同步 4 处代码(防 DB 挂时 fallback 又脱节):
--     - db/wallet_db.py:seed_feature_pricing PRICING_DATA
--     - middleware/subscription_billing.py:326-336 hardcoded fallback + BILLING_FALLBACK_CODE_MAP video_asr
--     - frontend/src/pages/Pricing/PricingPage.tsx:46-90 兜底
--     - agents/social_agent.py confirm card 文案
--
-- 验证(Deploy-CTO 部署后必跑):
--   SELECT feature_code, cost_points FROM feature_pricing WHERE feature_code IN (
--     'profile_polish','script_gen','hook_gen','topic_gen','hook_script_combo',
--     'single_video','video_framework','learn_viral','content_review',
--     'video_asr','author_breakdown','team_portrait','rewrite_gen'
--   ) ORDER BY feature_code;
--
-- 真机 smoke 7 路(MCN 门槛 · 任一不过继续 hold):
--   1. script_gen 完整脚本 → 扣 -40(不是 -650)
--   2. hook_gen 开篇钩子 → 扣 -40(不是 -260)
--   3. topic_gen 选题 → 扣 -80(不是 -390)
--   4. hook_script_combo 开篇+脚本 → 扣 -80(不是 -780)
--   5. video_asr 2 分钟 → 扣 -80(不是 -780/-390×2 · video_asr fallback map 修后真扣 40×2)
--   6. author_breakdown / team_portrait → 扣 -1950(不是 -1040)
--   7. profile_polish → 扣 -5(不是 -30)

BEGIN;

-- ============================================================
-- 轻量 5 (8 项)
-- ============================================================
INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
VALUES
  ('profile_polish',  '档案字段润色',         5, 0.04, FALSE),
  ('ai_coach',        'AI教练/顾问对话',      5, 0.04, FALSE),
  ('comment_gen',     '评论生成',             5, 0.04, FALSE),
  ('dm_gen',          '私信话术生成',         5, 0.04, FALSE),
  ('scenario_gen',    '场景话术生成',         5, 0.04, FALSE),
  ('corpus_text',     '语料文本录入+特征提取', 5, 0.04, FALSE),
  ('corpus_upload',   '语料上传+特征提取',     5, 0.04, FALSE),
  ('task_route',      'AI任务路由',           5, 0.04, FALSE)
ON CONFLICT (feature_code) DO UPDATE
SET cost_points = EXCLUDED.cost_points,
    feature_name = EXCLUDED.feature_name,
    cost_compute = EXCLUDED.cost_compute;

-- ============================================================
-- 专业 40 (5 项)
-- ============================================================
INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
VALUES
  ('script_gen',           '完整脚本生成',         40, 0.31, FALSE),
  ('hook_gen',             '开篇钩子生成',         40, 0.31, FALSE),
  ('brand_fill',           '品牌信息AI填充',       40, 0.31, FALSE),
  ('personality_refresh',  '刷新性格画像',         40, 0.31, FALSE),
  ('industry_brief_rerun', '知识库字段重跑（AI）', 40, 0.31, FALSE)
ON CONFLICT (feature_code) DO UPDATE
SET cost_points = EXCLUDED.cost_points,
    feature_name = EXCLUDED.feature_name,
    cost_compute = EXCLUDED.cost_compute;

-- ============================================================
-- 超级/组合 80 (2 项)
-- ============================================================
INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
VALUES
  ('topic_gen',         '选题生成',         80, 0.62, FALSE),
  ('hook_script_combo', '开篇+文案一套',    80, 0.62, FALSE)
ON CONFLICT (feature_code) DO UPDATE
SET cost_points = EXCLUDED.cost_points,
    feature_name = EXCLUDED.feature_name,
    cost_compute = EXCLUDED.cost_compute;

-- ============================================================
-- 视频分钟 40/分钟 (老板审核重点 · video_asr 暗雷)
-- 配套必改:
--   middleware/subscription_billing.py BILLING_FALLBACK_CODE_MAP["video_asr"]="video_asr"
--   不再映射到 single_video(否则 single_video=390 后 video_asr 也变 390/分钟)
-- ============================================================
INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
VALUES ('video_asr', '视频ASR转录(按分钟)', 40, 0.31, FALSE)
ON CONFLICT (feature_code) DO UPDATE
SET cost_points = EXCLUDED.cost_points,
    feature_name = EXCLUDED.feature_name,
    cost_compute = EXCLUDED.cost_compute;

-- ============================================================
-- 联网 10 (v1.7.6.3.1 P0 · Codex 审核 2026-05-24 抓到 · 之前漏)
-- 真因:
--   web_search 旧 BILLING_FALLBACK_CODE_MAP 映射到 find_trending=0(免费)
--   导致 web_search 超量 fallback 仍 0 扣 · 跟 SSOT "联网 10" 冲突
-- 配套必改:
--   middleware/subscription_billing.py BILLING_FALLBACK_CODE_MAP["web_search"]="web_search"
--   find_trending=0 保留(产品要的免费"找热点")· 不再承接 web_search 计费
-- ============================================================
INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
VALUES ('web_search', '联网搜索', 10, 0.08, FALSE)
ON CONFLICT (feature_code) DO UPDATE
SET cost_points = EXCLUDED.cost_points,
    feature_name = EXCLUDED.feature_name,
    cost_compute = EXCLUDED.cost_compute;

-- ============================================================
-- 拆视频短 390 (4 项)
-- ============================================================
INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
VALUES
  ('single_video',    '单视频采集+分析', 390, 3.0, FALSE),
  ('video_framework', '视频框架提取',    390, 3.0, FALSE),
  ('learn_viral',     '学爆款',          390, 3.0, FALSE),
  ('content_review',  '链接内容复盘',    390, 3.0, FALSE)
ON CONFLICT (feature_code) DO UPDATE
SET cost_points = EXCLUDED.cost_points,
    feature_name = EXCLUDED.feature_name,
    cost_compute = EXCLUDED.cost_compute;

-- ============================================================
-- 拆博主短 1950 (2 项)
-- ============================================================
INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
VALUES
  ('author_breakdown', '博主拆解', 1950, 15.0, FALSE),
  ('team_portrait',    '团队画像', 1950, 10.0, FALSE)
ON CONFLICT (feature_code) DO UPDATE
SET cost_points = EXCLUDED.cost_points,
    feature_name = EXCLUDED.feature_name,
    cost_compute = EXCLUDED.cost_compute;

-- ============================================================
-- 保持(已对 SSOT · 强 UPSERT 防 prod 旧漂)
-- v1.7.6.3.3 (Codex 三审 2026-05-24): 之前只注释"保持"不 UPSERT
-- 若 prod DB 这 3 项被人改飘过 · migration 不会修 · 跟"用 migration 消灭 DB 旧价"目标冲突
-- 修法:即使值不变 · 也强 UPSERT · 保证 prod 真值 = SSOT
-- ============================================================
INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
VALUES
  ('rewrite_gen',      '爆款仿写',           650, 5.0, FALSE),
  ('social_diagnosis', '社媒专项诊断',       650, 5.0, FALSE),
  ('deep_analyze',     '品牌深度行业解析',   500, 4.5, FALSE)
ON CONFLICT (feature_code) DO UPDATE
SET cost_points = EXCLUDED.cost_points,
    feature_name = EXCLUDED.feature_name,
    cost_compute = EXCLUDED.cost_compute;

COMMIT;

-- 验证 SQL(Deploy-CTO 必跑)· v1.7.6.3.3 加 social_diagnosis / deep_analyze
-- SELECT feature_code, cost_points FROM feature_pricing
-- WHERE feature_code IN (
--   'profile_polish','script_gen','hook_gen','topic_gen','hook_script_combo',
--   'single_video','video_framework','learn_viral','content_review',
--   'video_asr','web_search','author_breakdown','team_portrait',
--   'rewrite_gen','social_diagnosis','deep_analyze'
-- )
-- ORDER BY feature_code;
-- 期望:
--   author_breakdown=1950, content_review=390, deep_analyze=500, hook_gen=40,
--   hook_script_combo=80, learn_viral=390, profile_polish=5, rewrite_gen=650,
--   script_gen=40, single_video=390, social_diagnosis=650, team_portrait=1950,
--   topic_gen=80, video_asr=40, video_framework=390, web_search=10
