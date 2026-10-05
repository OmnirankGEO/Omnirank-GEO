-- 秘塔断血受害面扫描(只读 · 2026-08-04 止血包 ④)
--
-- 背景:秘塔余额不足走 HTTP 200 通道返回 {"errCode":3000,"errMsg":"余额不足"},
--   判据只看 status → 竞争池为空 → effective_competition = max(1,0) = 1(地板值)
--   → 报价按「零竞争」出,篇数落 MIN_ARTICLES_DEFAULT=5 打底。
--   `余额不足` 最早出现于 2026-07-29 09:47(llm_call_log),故扫描窗口自 07-29 起。
--
-- 篇数公式(与 tools/pricing_bands.py 逐字一致,不是另造的口径):
--   _article_competition(c) = max(1, round(min(c,100)/5)*5)
--   required_articles       = max(5, ceil(target_share * C / (1 - target_share)))
--   eff_comp=1 时 C=1 → ceil(0.25*1/0.75)=1 → 落 5 打底,与受害报价单上的 5 完全吻合。
--
-- 用法(生产只读):
--   docker cp <此文件> omnirank-db:/tmp/victim.sql
--   docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope -f /tmp/victim.sql
--
-- 🔴 本脚本全程只有 SELECT / SET,无任何 DDL/DML。
--
-- 🔴 **故意不登记 db/migration_manifest.py —— 不要"修"这条告警。**
--   check_my_base.sh 会把任何未登记的新 .sql 报成「上线后永远不会跑」。
--   对迁移脚本那是对的;对本文件恰恰相反:它是**只读取证**脚本,
--   登记进 manifest = 每次部署 prestart 都会执行它(而且它带 \echo / \pset
--   这类 psql 元命令,不是 manifest runner 的输入形状)。
--   「永远不自动跑」就是本文件想要的状态。人工按上面的用法跑即可。

SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
\pset pager off

\echo '════ ① 受害报价清单(07-29 起 · 快照里存在 effective_competition <= 1 的词)════'

WITH kw AS (
    SELECT s.id            AS snapshot_id,
           s.quote_id,
           s.created_at,
           (e ->> 'keyword')                       AS keyword,
           NULLIF(e ->> 'effective_competition','')::numeric AS eff_comp
    FROM quote_pricing_snapshots s,
         LATERAL jsonb_array_elements(s.pricing_snapshot -> 'keywords') e
    WHERE s.created_at >= DATE '2026-07-29'
),
agg AS (
    SELECT snapshot_id, quote_id, created_at,
           count(*)                                        AS kw_in_snapshot,
           count(*) FILTER (WHERE eff_comp <= 1)           AS kw_floor,
           min(eff_comp)                                   AS min_eff,
           max(eff_comp)                                   AS max_eff
    FROM kw GROUP BY 1,2,3
)
SELECT a.snapshot_id,
       a.quote_id,
       q.brand_name,
       q.industry,
       q.city,
       q.status,
       q.target_share,
       q.monthly_price,
       q.paid_amount,
       a.kw_in_snapshot,
       a.kw_floor                                          AS kw_竞争为1,
       a.min_eff, a.max_eff,
       -- 报价单实际卖出的篇数(confirmed_keywords 是交付口径的权威)
       (SELECT COALESCE(sum(ck.required_articles),0)
          FROM confirmed_keywords ck WHERE ck.quote_id = a.quote_id) AS 已售篇数,
       a.created_at
FROM agg a
JOIN quotes q ON q.id = a.quote_id
WHERE a.kw_floor > 0
ORDER BY a.created_at DESC;

\echo ''
\echo '════ ② 受害面汇总 ════'

WITH kw AS (
    SELECT s.id AS snapshot_id, s.quote_id,
           NULLIF(e ->> 'effective_competition','')::numeric AS eff_comp
    FROM quote_pricing_snapshots s,
         LATERAL jsonb_array_elements(s.pricing_snapshot -> 'keywords') e
    WHERE s.created_at >= DATE '2026-07-29'
),
bad AS (
    SELECT DISTINCT quote_id FROM kw WHERE eff_comp <= 1
)
SELECT count(DISTINCT b.quote_id)                          AS 受害报价数,
       count(DISTINCT q.brand_id)                          AS 受害品牌数,
       COALESCE(sum(q.paid_amount),0)                      AS 已收款合计,
       COALESCE(sum(q.monthly_price),0)                    AS 报价金额合计,
       count(*) FILTER (WHERE q.status = 'confirmed')      AS 已确认数
FROM bad b JOIN quotes q ON q.id = b.quote_id;

\echo ''
\echo '════ ③ 逐词明细(供重算「应需篇数」时逐词喂真实竞争)════'

-- 注:LATERAL 必须排在引用它的 JOIN 之前,否则 `column "e" does not exist`。
SELECT s.quote_id,
       q.brand_name,
       q.target_share,
       (e ->> 'keyword')                                   AS keyword,
       (e ->> 'effective_competition')                     AS 报价时竞争,
       ck.required_articles                                AS 已售篇数,
       ck.final_price,
       -- 用 eff_comp=1 复算篇数:若与「已售篇数」逐词相等,即证明因果链闭合
       GREATEST(5, CEIL(q.target_share
                        * GREATEST(1, ROUND(LEAST(
                              COALESCE(NULLIF(e ->> 'effective_competition','')::numeric, 1), 100) / 5) * 5)
                        / (1 - q.target_share)))           AS 用报价竞争复算篇数
FROM quote_pricing_snapshots s
JOIN quotes q ON q.id = s.quote_id
CROSS JOIN LATERAL jsonb_array_elements(s.pricing_snapshot -> 'keywords') e
LEFT JOIN confirmed_keywords ck
       ON ck.quote_id = s.quote_id AND ck.keyword = (e ->> 'keyword')
WHERE s.created_at >= DATE '2026-07-29'
  AND COALESCE(NULLIF(e ->> 'effective_competition','')::numeric, 1) <= 1
ORDER BY s.quote_id, ck.id;

\echo ''
\echo '════ ④ 对照组:同窗口内竞争强度正常的报价(证明扫描判据不是恒真)════'

WITH kw AS (
    SELECT s.id AS snapshot_id, s.quote_id, s.created_at,
           NULLIF(e ->> 'effective_competition','')::numeric AS eff_comp
    FROM quote_pricing_snapshots s,
         LATERAL jsonb_array_elements(s.pricing_snapshot -> 'keywords') e
    WHERE s.created_at >= DATE '2026-07-29'
)
SELECT snapshot_id, quote_id, created_at::date AS d,
       count(*) AS kw_n, min(eff_comp) AS min_eff, max(eff_comp) AS max_eff
FROM kw
GROUP BY 1,2,3
HAVING min(eff_comp) > 1
ORDER BY d DESC;   -- 注:created_at 未进 GROUP BY,只能按别名 d 排

\echo ''
\echo '════ ⑤ 秘塔业务失败时间线(llm_call_log · 圈定断血窗口)════'

SELECT created_at::date                                     AS d,
       caller,
       count(*)                                             AS n,
       count(*) FILTER (WHERE NOT success)                   AS 记为失败,
       count(*) FILTER (WHERE success)                       AS 记为成功,
       min(duration_ms)                                      AS min_ms,
       max(duration_ms)                                      AS max_ms
FROM llm_call_log
WHERE platform = 'metaso' AND created_at >= DATE '2026-07-29'
GROUP BY 1,2
ORDER BY 1,2;
