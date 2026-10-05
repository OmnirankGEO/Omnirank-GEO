-- ============================================================
-- 补 customer_credit_transactions source CHECK 加 'agent_rebate'(D2-b 返利 · P0)
-- ============================================================
-- 2026-05-30 Deploy-CTO v27 资金 GATE 真机对账(green BEGIN/ROLLBACK)抓到:
--   GEO CTO 建表(migration_v35_factory_inventory:175)+ 代码写 source='agent_rebate'
--   (services/agent_rebate.py:117 allocate_credit)· 但漏更新此 CHECK 约束白名单。
--   Codex 静态审看代码逻辑没发现(没真跑 DB)· 直接 cutover 则 D2-b 返利上线即坏(每笔被 DB 拒回滚)。
--   prod 已由 Deploy-CTO 手动 ALTER 修复(v27 · green:8002)· 本文件为正式 migration,防 fresh DB 重建漏掉。
--
-- 原白名单(v35_factory:175):online_payment / offline_allocation / admin_adjust / tool_consume / refund_revoke
-- 新增:agent_rebate(D2-b 代理自定返利 · 从代理 bonus 库存出)
--
-- 幂等:DROP IF EXISTS 默认约束名 + ADD 含全部 6 个 source 值。
-- ⚠️ prod 若已存在非默认名的手动约束(Deploy v27 手动加),先 \d customer_credit_transactions 查名后 DROP,再跑本文件统一为标准名。

ALTER TABLE customer_credit_transactions
    DROP CONSTRAINT IF EXISTS customer_credit_transactions_source_check;

ALTER TABLE customer_credit_transactions
    ADD CONSTRAINT customer_credit_transactions_source_check
    CHECK (source IN (
        'online_payment',
        'offline_allocation',
        'admin_adjust',
        'tool_consume',
        'refund_revoke',
        'agent_rebate'
    ));

-- 验证:
-- SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='customer_credit_transactions_source_check';
-- 应包含 'agent_rebate'
