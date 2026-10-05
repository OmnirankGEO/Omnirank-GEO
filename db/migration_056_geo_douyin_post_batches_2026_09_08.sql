-- 056 · 图文批量下单的幂等台账(#150 §3.3)
-- =============================================================================
-- 现页顺序发 N 次 `POST /posts`,双击就重复下单、一条失败即停。
-- 改走 `POST /posts/batch`:一个 `request_id` 幂等。
--
-- 🔴 **不复用 `billing_deduction_idempotency`**:那张表是**扣费**的幂等台账
--    (`charge_tx_id` / `deducted` / `refunded_at` …)。把"一次批量建单请求"塞进去,
--    资金对账会读到一批既不扣费也不退费的行 —— 幂等语义借了,资金语义污染了。
--    两件事只是"都叫幂等",不是同一件事。
--
-- 🔴 主键是 **(created_by, request_id)** 不是 request_id 单键:
--    request_id 由前端生成,不同用户可能撞同一个串;单键会让 A 的重放
--    读到 B 的结果 —— 那是跨租户泄漏,不是幂等。
--
-- 🔴 `status` 有 in_progress:并发双击时,抢到插入的那一次干活,
--    另一次读到 in_progress 并原样返回 —— **不重复建单**。
--    没有这一态的话,第二次要么等、要么重做,而重做正是要防的事。
--
-- 🔴 **零 DML**:新表,不回填。
--
-- 号段:055 归本单 §3.2 · **056 归本单 §3.3** · 下一空 = 057。
-- =============================================================================

CREATE TABLE IF NOT EXISTS geo_douyin_post_batches (
    created_by      INTEGER     NOT NULL,
    request_id      TEXT        NOT NULL,
    brand_id        INTEGER,
    status          TEXT        NOT NULL DEFAULT 'in_progress',
    -- 逐条结果:[{"index":0,"post_id":123,"code":null,"message":""}, …]
    -- 重放时原样返回它 —— 幂等的含义是"同一个请求得到同一个答案",
    -- 不是"第二次什么都不做然后返空"。
    result          JSONB       NOT NULL DEFAULT '[]'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    PRIMARY KEY (created_by, request_id)
);

-- 按人看最近批次(排障用)。只索引真有归属的行。
CREATE INDEX IF NOT EXISTS idx_geo_douyin_post_batches_recent
    ON geo_douyin_post_batches (created_by, created_at DESC);
