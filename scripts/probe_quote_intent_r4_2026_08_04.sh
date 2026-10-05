#!/usr/bin/env bash
# 只读取证 R4 · 钉死「这批词到底是 07-23 老数据还是 08-04 新产出」
set -u
DB="docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope -v ON_ERROR_STOP=0"

echo "=== A. 194/195 三个时间戳 + pricing_data 内部 generated_at(定价真正发生的时刻)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT id, quote_id, status, created_at, updated_at,
       pricing_data::jsonb->>'generated_at' AS pricing_generated_at,
       (pricing_data::jsonb->'keywords'->0->>'price_locked_until') AS price_lock_until,
       visit_count
FROM keyword_selection_sessions WHERE id IN (194,195);
SQL

echo
echo "=== B. 品牌 679 的 quotes(前端列表那个日期可能来自 quotes 不是 session)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT id, brand_name, status, monthly_price, total_keywords,
       created_at, updated_at, confirmed_at, last_price_adjusted_at
FROM quotes WHERE brand_id = 679 ORDER BY id;
SQL

echo
echo "=== C. 截图里另外几个品牌的日期(证明列表显示的是 created 还是 updated)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT q.id, q.brand_name, q.status, q.total_keywords,
       q.created_at, q.updated_at
FROM quotes q
WHERE q.brand_name LIKE '%西江传说%' OR q.brand_name LIKE '%车同学%'
   OR q.brand_name LIKE '%驰鲸%'   OR q.brand_name LIKE '%AI数字人才%'
ORDER BY q.updated_at DESC LIMIT 20;
SQL

echo
echo "=== D. 品牌 679 的关键词是什么时候生成的(keywords 表 · 出词时刻才是根因现场)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT column_name FROM information_schema.columns
WHERE table_name='keywords' AND column_name IN
 ('id','quote_id','keyword','intent','created_at','category','recommended','source');
SQL
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT id, quote_id, keyword, intent, created_at
FROM keywords WHERE quote_id IN (401,402) ORDER BY id;
SQL

echo
echo "=== E. 品牌 679 的诊断(出词上游·诊断跑于何时)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT id, brand_id, status, created_at, completed_at
FROM diagnoses WHERE brand_id = 679 ORDER BY id;
SQL

echo
echo "=== F. 近 7 天【新生成】的报价里,informational 占比(判「接下来还会不会再出」的现况)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
WITH kw AS (
  SELECT s.id,
         (s.pricing_data::jsonb->>'generated_at') AS gen_at,
         k->>'intent' AS intent
  FROM keyword_selection_sessions s,
       LATERAL jsonb_array_elements(s.pricing_data::jsonb->'keywords') k
  WHERE s.pricing_data IS NOT NULL
)
SELECT left(gen_at,10) AS gen_day,
       COUNT(*) AS kw_rows,
       COUNT(*) FILTER (WHERE intent='informational') AS info_rows,
       COUNT(DISTINCT id) AS sessions
FROM kw
WHERE gen_at >= '2026-07-20'
GROUP BY 1 ORDER BY 1;
SQL

echo
echo "=== G. 近 30 天新建 session 的候选池里 informational 候选占比(源头出词侧)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT left(s.created_at::text,10) AS day,
       COUNT(*) AS cand_rows,
       COUNT(*) FILTER (WHERE k->>'intent' = 'informational') AS info_cand,
       COUNT(*) FILTER (WHERE k->>'intent' IS NULL) AS no_intent_cand
FROM keyword_selection_sessions s,
     LATERAL jsonb_array_elements(s.keywords_snapshot::jsonb) k
WHERE s.created_at > now() - interval '30 days'
GROUP BY 1 ORDER BY 1;
SQL
echo "=== R4 DONE ==="
