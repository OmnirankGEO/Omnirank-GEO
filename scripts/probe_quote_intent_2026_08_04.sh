#!/usr/bin/env bash
# 只读取证 · 报价意图闸开工基线(WO-QUOTE-INTENT-GATE-2026-08-04)
# 通道:docker exec -i omnirank-db psql · 首行 READ ONLY + 一条必失败 UPDATE 自证
set -u
DB="docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope -v ON_ERROR_STOP=0"

echo "=== 0. 只读通道自证(下面这条 UPDATE 必须报错,否则通道不是只读)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
UPDATE keyword_selection_sessions SET visit_count = visit_count WHERE id = -1;
SQL

echo
echo "=== 1. 会话 194 基本面 ==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT id, brand_id, quote_id, status,
       created_at, updated_at, visit_count,
       jsonb_array_length(COALESCE(pricing_data::jsonb->'keywords','[]'::jsonb)) AS priced_n
FROM keyword_selection_sessions WHERE id = 194;
SQL

echo
echo "=== 2. 会话 194 定价明细:每词 intent / should_quote / 三档价 / 引擎版本 ==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT k->>'keyword'                        AS kw,
       k->>'intent'                         AS intent,
       k->>'should_quote'                   AS should_quote,
       k->>'super_red_ocean'                AS sro,
       k->>'entry_price'                    AS entry,
       k->>'standard_price'                 AS std,
       k->>'flagship_price'                 AS flag,
       k->>'pricing_engine_version'         AS engine
FROM keyword_selection_sessions s,
     LATERAL jsonb_array_elements(s.pricing_data::jsonb->'keywords') k
WHERE s.id = 194;
SQL

echo
echo "=== 3. 会话 194 候选池里没进定价的商业信号词(工单说存在,核之)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT k->>'keyword' AS kw, k->>'category_label' AS cat, k->>'id' AS kid
FROM keyword_selection_sessions s,
     LATERAL jsonb_array_elements(s.keywords_snapshot::jsonb) k
WHERE s.id = 194
ORDER BY (k->>'id')::int;
SQL

echo
echo "=== 4. 全局面:未确认会话中,定价明细 100% informational 的有多少(要求4 存量清理的分母)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
WITH kw AS (
  SELECT s.id, s.brand_id, s.status, s.updated_at,
         k->>'intent' AS intent, (k->>'standard_price')::numeric AS std
  FROM keyword_selection_sessions s,
       LATERAL jsonb_array_elements(s.pricing_data::jsonb->'keywords') k
  WHERE s.pricing_data IS NOT NULL
    AND s.status NOT IN ('confirmed','archived','expired')
), agg AS (
  SELECT id, brand_id, status, updated_at,
         COUNT(*) AS n,
         COUNT(*) FILTER (WHERE intent = 'informational') AS n_info,
         COUNT(*) FILTER (WHERE COALESCE(std,0) > 0)      AS n_priced
  FROM kw GROUP BY id, brand_id, status, updated_at
)
SELECT COUNT(*) AS sessions_all_informational,
       COUNT(*) FILTER (WHERE n_priced > 0) AS of_which_actually_priced
FROM agg WHERE n > 0 AND n_info = n;
SQL

echo
echo "=== 5. 同上,逐条列出(给要求4 存量清理用)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
WITH kw AS (
  SELECT s.id, s.brand_id, s.status, s.updated_at,
         k->>'intent' AS intent, (k->>'standard_price')::numeric AS std
  FROM keyword_selection_sessions s,
       LATERAL jsonb_array_elements(s.pricing_data::jsonb->'keywords') k
  WHERE s.pricing_data IS NOT NULL
    AND s.status NOT IN ('confirmed','archived','expired')
), agg AS (
  SELECT id, brand_id, status, updated_at,
         COUNT(*) AS n,
         COUNT(*) FILTER (WHERE intent = 'informational') AS n_info,
         COUNT(*) FILTER (WHERE COALESCE(std,0) > 0)      AS n_priced,
         SUM(COALESCE(std,0))                             AS std_total
  FROM kw GROUP BY id, brand_id, status, updated_at
)
SELECT id, brand_id, status, updated_at, n, n_info, n_priced, std_total
FROM agg WHERE n > 0 AND n_info = n ORDER BY id;
SQL

echo
echo "=== 6. 反向对照:非全信息型会话也统计一次(证明上面的判据不是恒空)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
WITH kw AS (
  SELECT s.id, k->>'intent' AS intent
  FROM keyword_selection_sessions s,
       LATERAL jsonb_array_elements(s.pricing_data::jsonb->'keywords') k
  WHERE s.pricing_data IS NOT NULL
    AND s.status NOT IN ('confirmed','archived','expired')
), agg AS (
  SELECT id, COUNT(*) n, COUNT(*) FILTER (WHERE intent='informational') n_info
  FROM kw GROUP BY id
)
SELECT COUNT(*) FILTER (WHERE n_info = n)            AS all_info,
       COUNT(*) FILTER (WHERE n_info > 0 AND n_info < n) AS mixed,
       COUNT(*) FILTER (WHERE n_info = 0)            AS no_info,
       COUNT(*)                                      AS total
FROM agg;
SQL

echo
echo "=== 7. LLM-first 报价闸生产实际值(判 llm_on 是否本来就有可能开)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT key, value FROM system_settings
WHERE key ILIKE '%llm_first%' OR key ILIKE '%pricing%' OR key ILIKE '%quote_markup%'
ORDER BY key;
SQL

echo
echo "=== 8. 用户 quote_markup_ratio 分布(判 markup_override 是否恒非 None)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT quote_markup_ratio, COUNT(*) FROM users GROUP BY 1 ORDER BY 2 DESC LIMIT 10;
SQL

echo
echo "=== 9. keyword_price_cache_llm 近 30 天有无数据(判老公式剔词依据是否本来就空)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT COUNT(*) AS rows_all,
       COUNT(*) FILTER (WHERE created_at > now() - interval '30 days') AS rows_30d,
       COUNT(*) FILTER (WHERE should_quote = false) AS should_quote_false
FROM keyword_price_cache_llm;
SQL

echo "=== PROBE DONE ==="
