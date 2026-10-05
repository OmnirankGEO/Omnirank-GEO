-- 只读诊断 · 12 个孤儿 regenerating topics(6122-6133)
-- [WO 2026-08-08 · W3 第 3 件 · 小件]
--
-- 🔴 本文件**只读**。没有任何 UPDATE / DELETE / DDL。
--    处置 SQL 在交付说明 §4,**待 Review 过目后随班执行**,不在本文件里。
--
-- 跑法(生产只读取证的既有口径):
--   docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope \
--     -c "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;" \
--     -f scripts/diagnose_orphan_regenerating_topics_2026_08_08.sql
--
-- SQL 4 维核验(2026-08-08 现场查过 information_schema,不是照抄记忆):
--   列名   topics: id/quote_id/status/is_optimize/optimized_title/regenerate_count/
--                  original_keyword/fail_reason/created_at/article_id/writing_started_at
--          🔴 topics **没有** updated_at 列(第一版我写了,当场报错)
--   类型   status/optimized_title/original_keyword/fail_reason = text ·
--          is_optimize = boolean · regenerate_count = integer ·
--          created_at = timestamp without time zone
--   归属   regenerate_count / is_optimize 属 topics,不属 quotes
--   dry-run 本文件无写操作,不适用

\echo '=== 1. 孤儿清单(全部 regenerating 行) ==='
SELECT t.id, t.quote_id, t.status, t.is_optimize, t.regenerate_count,
       (t.article_id IS NOT NULL) AS has_article,
       t.fail_reason,
       left(coalesce(t.optimized_title, ''), 30) AS optimized_title,
       left(coalesce(t.original_keyword, ''), 24) AS original_keyword,
       t.created_at, t.writing_started_at
  FROM topics t
 WHERE t.status = 'regenerating'
 ORDER BY t.id;

\echo ''
\echo '=== 2. 为什么没有任何任务捡走它们(判据比对) ==='
-- 恢复任务 services/optimize_title_jobs.recover_optimize_title_jobs 的 WHERE 是
--   is_optimize = TRUE AND status='regenerating' AND optimized_title='标题生成中...'
-- 下面这张表把两个条件逐行比出来:命中 0 条 = 恢复任务永远看不见它们。
SELECT t.id,
       t.is_optimize                                   AS matches_is_optimize,
       (t.optimized_title = '标题生成中...')            AS matches_placeholder,
       (t.is_optimize AND t.optimized_title = '标题生成中...') AS would_be_recovered
  FROM topics t
 WHERE t.status = 'regenerating'
 ORDER BY t.id;

\echo ''
\echo '=== 3. 它们卡住的连带后果(客户可见面) ==='
-- ① writing/direction_distribution.LOCKED_STATUSES 含 regenerating → 方向/标题不可改
-- ② services/article_generation_reset.py:81 对 regenerating 抛 ARTICLE_RESET_IN_PROGRESS
--    → 客户/服务商**连"全量重置"都点不动**,这是真正的用户面卡死
-- 下面看这批 topic 所属报价单还活着没有、有没有已交付文章
SELECT q.id AS quote_id, q.brand_name, q.status AS quote_status,
       (q.deleted_at IS NOT NULL) AS quote_deleted, q.created_at::date AS quote_created,
       count(*) FILTER (WHERE t.status = 'regenerating')            AS stuck_topics,
       count(*)                                                     AS total_topics,
       count(*) FILTER (WHERE t.article_id IS NOT NULL)             AS topics_with_article,
       count(*) FILTER (WHERE t.status = 'completed')               AS completed_topics
  FROM topics t
  JOIN quotes q ON q.id = t.quote_id
 WHERE t.quote_id IN (SELECT DISTINCT quote_id FROM topics WHERE status = 'regenerating')
 GROUP BY q.id, q.brand_name, q.status, q.deleted_at, q.created_at
 ORDER BY q.id;

\echo ''
\echo '=== 4. 这批 topic 到底烧了多少钱(平台侧 LLM 成本) ==='
SELECT l.metadata->>'topic_id' AS topic_id,
       count(*)                                                        AS llm_shots,
       count(DISTINCT l.metadata->>'generation_request_id')            AS request_ids,
       count(DISTINCT l.model)                                         AS models,
       round(sum(l.estimated_cost), 4)                                 AS cost,
       count(*) FILTER (WHERE l.metadata->>'article_id' IS NULL)       AS shots_without_article,
       min(l.created_at)::date AS first_day, max(l.created_at)::date AS last_day
  FROM llm_call_log l
 WHERE l.caller = 'article_writing'
   AND l.metadata->>'topic_id' IN (
         SELECT id::text FROM topics WHERE status = 'regenerating')
 GROUP BY 1
 ORDER BY llm_shots DESC;

\echo ''
\echo '=== 5. 🔴 客户侧有没有被重复扣费(topic_gen) ==='
-- server.py 的选词重生成路径:先 check_balance_only(不真扣),再 regenerate_topic()
-- 标 regenerating,再调 LLM。regenerate_count 6-15 说明重生成被点了很多次 ——
-- 必须确认客户钱包是不是也被扣了 6-15 次。
-- [复审订正 2026-08-08] 上一版这里的列名是猜的(`reason` / `points`),
--   两个都不存在。现场查 information_schema 得到真列:
--     point_transactions(id, user_id, type, point_type, amount, balance_after,
--                        feature_code, description, order_id, created_at,
--                        brand_id, source, consumption_version)
--   → 功能码走 `feature_code`,金额走 `amount`。
SELECT to_char(t.created_at, 'YYYY-MM-DD') AS day,
       t.feature_code, t.type, t.point_type,
       count(*) AS n, sum(t.amount) AS total_amount
  FROM point_transactions t
 WHERE t.feature_code = 'topic_gen'
   AND t.created_at >= '2026-07-15' AND t.created_at < '2026-07-30'
 GROUP BY 1, 2, 3, 4 ORDER BY 1;

\echo '--- 5b. 精确到这批孤儿所属品牌/用户(quote 386 → brand 662 → owner 24) ---'
SELECT t.user_id, t.brand_id, count(*) AS topic_gen_calls, sum(t.amount) AS total_amount,
       min(t.created_at)::date AS first_day, max(t.created_at)::date AS last_day
  FROM point_transactions t
 WHERE t.feature_code = 'topic_gen'
   AND (t.brand_id = 662 OR t.user_id = 24)
 GROUP BY 1, 2 ORDER BY 3 DESC;

\echo '--- 5c. 反向对照:feature_code 到底有哪些取值(证明上面不是恒空) ---'
SELECT feature_code, count(*) FROM point_transactions
 WHERE created_at >= '2026-07-01' GROUP BY 1 ORDER BY 2 DESC LIMIT 12;

\echo ''
\echo '=== 6. 反向对照:非孤儿的 regenerating 应该是 0 条(证明第 1 节不是恒真) ==='
-- 若这里有行,说明还存在**正在正常跑**的 regenerating(那种不能动),
-- 处置名单必须把它们排除掉。判据:创建于 24 小时内 = 可能还在跑。
SELECT count(*) AS recent_regenerating_within_24h
  FROM topics
 WHERE status = 'regenerating'
   AND created_at > NOW() - INTERVAL '24 hours';
