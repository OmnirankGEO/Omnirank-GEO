-- [资金 cron 幂等批] GEO 托管 tick + 待审自动发布加 claim 列(防蓝绿双跑双扣/媒体双投)
-- 锁层非唯一防线:redis scheduler 锁兜单实例,数据层 CAS claim 兜锁失效/蓝绿切换窗口双跑。
-- 列可空·additive·零回归。⚠️ 部署序:migration 先于代码(claim CAS 写这些列)。
BEGIN;

-- 主 tick 占用标记(_process_one_campaign 起始 CAS · 近 N 分钟已处理则跳过)
ALTER TABLE managed_campaigns
    ADD COLUMN IF NOT EXISTS last_tick_claimed_at TIMESTAMP;

-- 待审自动发布占用标记(process_due_pending_reviews · 仅 claim 赢家发布)
ALTER TABLE pending_review_articles
    ADD COLUMN IF NOT EXISTS autopublish_claimed_at TIMESTAMP;

COMMIT;

-- 核验:
-- SELECT column_name FROM information_schema.columns
--  WHERE (table_name='managed_campaigns' AND column_name='last_tick_claimed_at')
--     OR (table_name='pending_review_articles' AND column_name='autopublish_claimed_at');
