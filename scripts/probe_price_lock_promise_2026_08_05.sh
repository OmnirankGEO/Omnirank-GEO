#!/bin/bash
# 只读取证 · 价格锁承诺脱钩(WO_PRICE_LOCK_PROMISE_2026-08-05)
# 零写入:整段跑在一个 READ ONLY 事务里,并且第一件事就是用一条必然失败的 UPDATE 自证只读。
# 🔴 必须用 heredoc 喂 psql —— 单条 `psql -c "..."` 里的多语句是**一个事务**,
#    `SET SESSION CHARACTERISTICS` 只对【后续】事务生效,UPDATE 会被放行(2026-08-05 实测踩过)。
set -u
echo "================ 价格锁承诺 · 只读取证 · $(date '+%F %T') ================"

docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope <<'SQL'
BEGIN READ ONLY;

\echo '── 0. 只读自证(下面这条 UPDATE 必须报 read-only transaction,否则本次取证作废)──'
SAVEPOINT ro_probe;
UPDATE keyword_price_cache SET city = city WHERE false;
ROLLBACK TO SAVEPOINT ro_probe;

\echo ''
\echo '── 1. 共享价格锁缓存:近 30 天写入 / 当前未过期行数 ──'
SELECT
  COUNT(*)                                                        AS total_rows,
  COUNT(*) FILTER (WHERE cached_at > now() - interval '30 days')   AS written_30d,
  COUNT(*) FILTER (WHERE cached_at > now() - interval '7 days')    AS written_7d,
  COUNT(*) FILTER (WHERE expires_at > now())                       AS unexpired_now,
  MAX(cached_at)                                                   AS last_write
FROM keyword_price_cache;

\echo ''
\echo '── 2. LLM 独立表(工单称历史总行数为 0 · 现场核)──'
SELECT COUNT(*) AS total_rows, MAX(cached_at) AS last_write FROM keyword_price_cache_llm;

\echo ''
\echo '── 3-pre. SQL 4 维核验:列名 + data_type 现取(不照抄仓库 DDL)──'
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE (table_name = 'keyword_selection_sessions' AND column_name IN ('id','pricing_data','updated_at','status'))
   OR (table_name = 'keyword_price_cache'        AND column_name IN ('keyword','expires_at','cached_at'))
ORDER BY table_name, column_name;

\echo ''
\echo '── 3. 最近 15 个报价会话:承诺了锁期的词数 vs 真在缓存里的词数 ──'
\echo '     (promised_locked 远大于 really_locked = 本工单要修的假承诺)'
WITH s AS (
  SELECT id,
         jsonb_array_elements(
           CASE jsonb_typeof(pricing_data::jsonb -> 'keywords')
                WHEN 'array' THEN pricing_data::jsonb -> 'keywords'
                ELSE '[]'::jsonb END
         ) AS kw
  FROM (
    SELECT id, pricing_data FROM keyword_selection_sessions
    WHERE pricing_data IS NOT NULL AND pricing_data <> ''
    ORDER BY id DESC LIMIT 15
  ) t
)
SELECT s.id                                                       AS session_id,
       COUNT(*)                                                   AS priced_words,
       COUNT(*) FILTER (WHERE s.kw ->> 'price_locked_until' IS NOT NULL) AS promised_locked,
       COUNT(*) FILTER (WHERE EXISTS (
         SELECT 1 FROM keyword_price_cache c
         WHERE c.keyword = s.kw ->> 'keyword' AND c.expires_at > now()
       ))                                                          AS really_locked
FROM s
GROUP BY s.id
ORDER BY s.id DESC;

\echo ''
\echo '── 4. 反向对照:同一条 join 换成【不带未过期条件】必须查得到(证明第 3 步不是 join 写错)──'
WITH s AS (
  SELECT id,
         jsonb_array_elements(
           CASE jsonb_typeof(pricing_data::jsonb -> 'keywords')
                WHEN 'array' THEN pricing_data::jsonb -> 'keywords'
                ELSE '[]'::jsonb END
         ) AS kw
  FROM (
    SELECT id, pricing_data FROM keyword_selection_sessions
    WHERE pricing_data IS NOT NULL AND pricing_data <> ''
    ORDER BY id DESC LIMIT 15
  ) t
)
SELECT COUNT(*) FILTER (WHERE EXISTS (
         SELECT 1 FROM keyword_price_cache c WHERE c.keyword = s.kw ->> 'keyword'
       )) AS in_cache_ignoring_expiry,
       COUNT(*) FILTER (WHERE EXISTS (
         SELECT 1 FROM keyword_price_cache c
         WHERE c.keyword = s.kw ->> 'keyword' AND c.expires_at > now()
       )) AS in_cache_unexpired
FROM s;

\echo ''
\echo '── 5. 缓存表最近 5 行(看最后一次写入是什么时候的事)──'
SELECT c.keyword, c.industry, c.city, c.cached_at, c.expires_at,
       (c.expires_at > now()) AS still_unexpired
FROM keyword_price_cache c
ORDER BY c.cached_at DESC
LIMIT 5;

COMMIT;
SQL

echo ""
echo "================ 取证结束(全程只读)================"
