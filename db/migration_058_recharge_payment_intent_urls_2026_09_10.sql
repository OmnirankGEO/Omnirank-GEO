-- #169 · recharge_orders 保存支付意向的三个 URL(2026-09-10)
--
-- 为什么需要:用户在手机外部浏览器跳出去付款,回来时前端要**恢复同一张单**的
-- 支付出口。今天这三个 URL 建单时返给前端就扔了,全仓从不落库
--   ⇒ 任何"第二次看这张单"(幂等重试 / order-status 恢复)都只能拿到 None。
--   ⇒ 虎皮椒的 url 带它自己生成的 token,**不能由 order_id 重建**。
--
-- 🔴 为什么不塞 pricing_snapshot_jsonb / settlement_snapshot_jsonb:
--    那两列是 08_billing 要求的**不可变资金证据**(报价快照 / 结算快照)。
--    把支付 URL 混进去会让"这张单当时按什么价成交"这个问题多出一堆
--    与定价无关的字段,而资金证据一旦被写脏就无法分辨哪部分是原始的。
--    Review 2026-09-10 §5.2 同此裁定。
--
-- 🔴 零 DML:只加列,不回填历史。历史单的 URL 早已丢失,回填只能靠猜,
--    而猜出来的支付链接会把用户送去一个不属于他那张单的收银台。
--    NULL 在这里是诚实的:它表示"这张单建于本迁移之前,我们没有留下出口"。
--
-- 三列都可空、都无默认值:老代码不写它们也不会报错(additive)。

ALTER TABLE recharge_orders
    ADD COLUMN IF NOT EXISTS payment_url_mobile text;

ALTER TABLE recharge_orders
    ADD COLUMN IF NOT EXISTS payment_url_qrcode text;

ALTER TABLE recharge_orders
    ADD COLUMN IF NOT EXISTS code_url text;

COMMENT ON COLUMN recharge_orders.payment_url_mobile IS
    '#169 · 手机跳转支付页(虎皮椒 url)。建单时写入;NULL = 本迁移之前建的单。';
COMMENT ON COLUMN recharge_orders.payment_url_qrcode IS
    '#169 · PC 扫码支付页(虎皮椒 url_qrcode)。建单时写入。';
COMMENT ON COLUMN recharge_orders.code_url IS
    '#169 · 微信 Native 下单返回的 code_url。建单时写入。';
