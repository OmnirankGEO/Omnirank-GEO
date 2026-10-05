-- ============================================================================
-- pricing v2.1 成本驱动 LLM 评估师 · migration 2026-06-11
-- ============================================================================
-- 老板拍商业逻辑:客户价 = 篇数 × 单篇成本 × 服务商系数 · LLM 只判物理量(竞争量级 + 媒体档次)
-- cache 增 JSONB 字段 v2_assessor_data 装算价底盘(true_competition/cost/media_tier/llm 双验/risk_flags)
--
-- 🔴 部署序(铁律 DB before code · feedback_deploy_order_db_before_code):
--   1. 【先】跑此 migration(幂等 · 老代码不写此列零影响 · 可提前单独上)
--   2. 【后】蓝绿启动新代码(save_keyword_prices_cache INSERT 含 v2_assessor_data 列 · 列必须已存在,
--      否则每次缓存写入 column does not exist 崩 → 缓存永远写不进 → 每单全量重调 LLM 成本爆炸)
--
-- 本文件已登记进:
--   · server.py _run_sql_migrations MIGRATIONS 清单(启动自动跑 · autocommit · 整文件 execute)
--   · db/diagnosis_db.py init_db 的 _safe_add_column 自迁移(dev/CI/全新部署兜底)
--   → 三通道幂等覆盖(prod 手跑 / 启动迁移 / init_db)
--
-- ⚠️ 故意【不】失效老 v1.x cache 行:
--   新代码读路径按 pricing_bands.CURRENT_PRICING_FORMULA_VERSION 精确过滤(当前 v2.2_2026-06-11)
--   → 旧版本行天然 miss · lazy 重算
--   旧行 7 天 TTL 自然过期。若在蓝绿切换前 UPDATE 失效旧行,会打穿旧代码的 7 天价格锁
--   (切换窗口内旧实例全量 cache miss 重算 → 价格漂移 + 成本尖峰)— Workflow 对抗验证 2026-06-11 抓的坑。
--
-- ⚠️ keyword_price_cache_llm(LLM-first flat 模式独立缓存表)不在本 migration 范围:
--   该表无版本列 · 生产默认 cluster 模式(flat 休眠)· 若将来开 flat 灰度,先单独清该表
--   (UPDATE keyword_price_cache_llm SET expires_at = CURRENT_TIMESTAMP - INTERVAL '1 day')。
--
-- 回滚:ALTER TABLE keyword_price_cache DROP COLUMN IF EXISTS v2_assessor_data;(旧代码不读此列)
-- ============================================================================

ALTER TABLE keyword_price_cache
    ADD COLUMN IF NOT EXISTS v2_assessor_data JSONB;

COMMENT ON COLUMN keyword_price_cache.v2_assessor_data IS
    'pricing v2.1 LLM 评估师算价底盘 · true_competition / cost_per_article / media_tier / llm 双验元数据 / risk_flags / guards · recalculate_for_tier 用它现算各档(SSOT) · 2026-06-11';
