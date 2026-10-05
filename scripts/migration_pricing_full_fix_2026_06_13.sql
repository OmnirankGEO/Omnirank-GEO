-- 报价完整修复 hardening①(2026-06-13 · v2.2 地基评审 confirmed P2)
-- keyword_price_cache_llm 加 assessor_version 列(纯 ADD COLUMN 幂等 · 不触碰报价/扣费表):
--   该表只携带 should_quote 布尔标记(不提供价格数值)· 此前只靠 expires_at(TTL)失效,
--   无法靠版本 bump 灰度/回滚。加版本列后:旧 NULL 行(LLM_FIRST 闲置期)仍可用(graceful),
--   未来 assessor 版本变更 → 旧【非空】版本行读路径软失效(get 只认 NULL 或 current)。
ALTER TABLE keyword_price_cache_llm ADD COLUMN IF NOT EXISTS assessor_version TEXT;
