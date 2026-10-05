-- ============================================================================
-- migration_066 · 发布渠道 API 的媒体编号映射(开源版 · E5-4b · 2026-10-03)
-- ============================================================================
-- 为什么:发布渠道 API(PUBLISH_CHANNEL=api)对外给的媒体条目编号是字符串(如 off_xxx),
--   应用的媒体目录 mhz_media.id 是整数。这张表给每个渠道编号分配一个本地整数主键,
--   目录同步与下单都按它来回换(services/publish_channels/api_client.py)。
-- 只建新表,不碰任何存量表;IF NOT EXISTS,可重放。
-- 没接入发布渠道 API 时这张表一直是空的。
-- ============================================================================
CREATE TABLE IF NOT EXISTS public.publish_channel_offers (
    local_id    BIGSERIAL PRIMARY KEY,
    offer_id    TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 每个条目一次下单的本地记录(第二版 · 每个媒体单独报价、单独下单)。
--   client_reference 在下单前写入、随订单发给渠道;下单结果不明时,状态回流按它在渠道的订单列表里认单。
--   outcome:pending(已发出,结果未知)/ ordered(已受理,order_sn 有值)/ rejected(渠道明确没受理)。
--   claimed_item_id:被哪个本地条目认领(防止两个条目认同一单)。
CREATE TABLE IF NOT EXISTS public.publish_channel_submissions (
    client_reference  TEXT PRIMARY KEY,
    local_media_id    BIGINT NOT NULL,
    title_sha         TEXT NOT NULL,
    outcome           TEXT NOT NULL DEFAULT 'pending',
    order_sn          TEXT,
    claimed_item_id   BIGINT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_publish_channel_submissions_media
    ON public.publish_channel_submissions (local_media_id, title_sha, created_at);
