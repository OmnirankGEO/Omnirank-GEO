#!/usr/bin/env bash
# 只读取证 R3 · 用【真实嵌套键名】重算存量 + 核 intent 覆盖率(防 default 误伤)
set -u
DB="docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope -v ON_ERROR_STOP=0"

echo "=== A. 存量:未确认会话中定价明细 100% informational 且【真出价】的(要求4 分母·真键名)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
WITH kw AS (
  SELECT s.id, s.brand_id, s.status, s.updated_at,
         k->>'intent' AS intent,
         COALESCE((k->'standard'->>'price')::numeric, 0) AS std
  FROM keyword_selection_sessions s,
       LATERAL jsonb_array_elements(s.pricing_data::jsonb->'keywords') k
  WHERE s.pricing_data IS NOT NULL
    AND s.status NOT IN ('confirmed','archived','expired')
), agg AS (
  SELECT id, brand_id, status, updated_at, COUNT(*) n,
         COUNT(*) FILTER (WHERE intent='informational') n_info,
         COUNT(*) FILTER (WHERE std > 0) n_priced,
         SUM(std) std_total
  FROM kw GROUP BY 1,2,3,4
)
SELECT id, brand_id, status, updated_at, n, n_info, n_priced, std_total
FROM agg WHERE n>0 AND n_info=n AND n_priced>0 ORDER BY id;
SQL

echo
echo "=== B. 反向对照(证明 A 的判据有判别力·三档必须都非空)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
WITH kw AS (
  SELECT s.id, k->>'intent' AS intent,
         COALESCE((k->'standard'->>'price')::numeric,0) AS std
  FROM keyword_selection_sessions s,
       LATERAL jsonb_array_elements(s.pricing_data::jsonb->'keywords') k
  WHERE s.pricing_data IS NOT NULL
    AND s.status NOT IN ('confirmed','archived','expired')
), agg AS (
  SELECT id, COUNT(*) n, COUNT(*) FILTER (WHERE intent='informational') n_info,
         COUNT(*) FILTER (WHERE std>0) n_priced
  FROM kw GROUP BY 1
)
SELECT COUNT(*) FILTER (WHERE n_info=n AND n_priced>0)          AS all_info_priced,
       COUNT(*) FILTER (WHERE n_info=n AND n_priced=0)          AS all_info_unpriced,
       COUNT(*) FILTER (WHERE n_info>0 AND n_info<n)            AS mixed,
       COUNT(*) FILTER (WHERE n_info=0)                         AS no_info,
       COUNT(*)                                                 AS total
FROM agg;
SQL

echo
echo "=== C. 含已确认在内的全量(看历史上到底成交过几单全信息型)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
WITH kw AS (
  SELECT s.id, s.status, k->>'intent' AS intent,
         COALESCE((k->'standard'->>'price')::numeric,0) AS std
  FROM keyword_selection_sessions s,
       LATERAL jsonb_array_elements(s.pricing_data::jsonb->'keywords') k
  WHERE s.pricing_data IS NOT NULL
), agg AS (
  SELECT id, status, COUNT(*) n, COUNT(*) FILTER (WHERE intent='informational') n_info,
         COUNT(*) FILTER (WHERE std>0) n_priced, SUM(std) std_total
  FROM kw GROUP BY 1,2
)
SELECT status, COUNT(*) AS all_info_priced_sessions, SUM(std_total) AS money
FROM agg WHERE n_info=n AND n_priced>0 GROUP BY status ORDER BY 2 DESC;
SQL

echo
echo "=== D. intent 覆盖率:定价明细里 intent 缺失/为空的比例(防 default 误伤)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT COALESCE(k->>'intent','<MISSING>') AS intent, COUNT(*) AS kw_rows,
       COUNT(DISTINCT s.id) AS sessions
FROM keyword_selection_sessions s,
     LATERAL jsonb_array_elements(s.pricing_data::jsonb->'keywords') k
WHERE s.pricing_data IS NOT NULL
GROUP BY 1 ORDER BY 2 DESC;
SQL

echo
echo "=== E. 混合单里 informational 词的占比分布(判「数量少于商业词」这类口径的现状)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
WITH kw AS (
  SELECT s.id, k->>'intent' AS intent
  FROM keyword_selection_sessions s,
       LATERAL jsonb_array_elements(s.pricing_data::jsonb->'keywords') k
  WHERE s.pricing_data IS NOT NULL
), agg AS (
  SELECT id, COUNT(*) n, COUNT(*) FILTER (WHERE intent='informational') n_info FROM kw GROUP BY 1
)
SELECT n_info, n, COUNT(*) AS sessions FROM agg GROUP BY 1,2 ORDER BY 2,1;
SQL

echo
echo "=== F. 194 全部 6 词的完整定价(证明 6 词各自都真出了价·不是只有第 1 个)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT k->>'keyword' AS kw, k->>'intent' AS intent,
       (k->'entry'->>'price') AS entry, (k->'standard'->>'price') AS std,
       (k->'flagship'->>'price') AS flag, (k->>'value_score') AS val,
       (k->>'search_volume') AS sv
FROM keyword_selection_sessions s,
     LATERAL jsonb_array_elements(s.pricing_data::jsonb->'keywords') k
WHERE s.id=194;
SQL

echo
echo "=== G. keyword_price_cache(公式引擎那张·非 llm)近 30 天 intent 分布 ==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT column_name FROM information_schema.columns
WHERE table_name='keyword_price_cache' AND column_name IN ('intent','cached_at','created_at','expires_at');
SELECT intent, COUNT(*) FROM keyword_price_cache GROUP BY 1 ORDER BY 2 DESC;
SQL

echo
echo "=== H. 194 的 unavailable_keywords / _quote_calculation(顶层另两个键是什么)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT left(jsonb_pretty(pricing_data::jsonb->'unavailable_keywords'),800) AS unavail,
       left(jsonb_pretty(pricing_data::jsonb->'_quote_calculation'),1200) AS calc
FROM keyword_selection_sessions WHERE id=194;
SQL
echo "=== R3 DONE ==="
