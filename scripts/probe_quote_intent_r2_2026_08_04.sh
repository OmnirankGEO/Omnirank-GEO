#!/usr/bin/env bash
# 只读取证 R2 · 钉死 194 的 pricing_data 真实键名与是否真出价
set -u
DB="docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope -v ON_ERROR_STOP=0"

echo "=== A. 194 pricing_data 顶层键 ==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT jsonb_object_keys(pricing_data::jsonb) AS top_key
FROM keyword_selection_sessions WHERE id=194;
SQL

echo
echo "=== B. 194 keywords[0] 的全部键(键名真相·不猜)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT jsonb_object_keys((pricing_data::jsonb->'keywords')->0) AS kw_key
FROM keyword_selection_sessions WHERE id=194;
SQL

echo
echo "=== C. 194 keywords[0] 原文(截断 2000 字)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT left(jsonb_pretty((pricing_data::jsonb->'keywords')->0), 2000)
FROM keyword_selection_sessions WHERE id=194;
SQL

echo
echo "=== D. 194 tiers 汇总(套餐总价到底是多少)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT left(jsonb_pretty(pricing_data::jsonb->'tiers'), 2000)
FROM keyword_selection_sessions WHERE id=194;
SQL

echo
echo "=== E. 对照组:一个正常出价会话的 keywords[0] 键(证明 B 不是恒空)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
WITH pick AS (
  SELECT s.id FROM keyword_selection_sessions s,
       LATERAL jsonb_array_elements(s.pricing_data::jsonb->'keywords') k
  WHERE s.pricing_data IS NOT NULL AND (k->>'standard_price')::numeric > 0
  ORDER BY s.id DESC LIMIT 1
)
SELECT p.id, left(jsonb_pretty((s.pricing_data::jsonb->'keywords')->0), 1600)
FROM pick p JOIN keyword_selection_sessions s ON s.id = p.id;
SQL

echo
echo "=== F. quote 401 上有没有价(报价单本体·可能与 session 分离)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT id, brand_name, status, monthly_price, total_price, created_at, updated_at
FROM quotes WHERE id = 401;
SQL

echo
echo "=== G. quotes 表列名(先看有哪些价格列,别猜)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT column_name, data_type FROM information_schema.columns
WHERE table_name='quotes' ORDER BY ordinal_position;
SQL

echo
echo "=== H. keyword_price_cache_llm 列名 + 近况 ==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT column_name, data_type FROM information_schema.columns
WHERE table_name='keyword_price_cache_llm' ORDER BY ordinal_position;
SELECT COUNT(*) AS rows_all, COUNT(*) FILTER (WHERE should_quote=false) AS sq_false
FROM keyword_price_cache_llm;
SQL

echo
echo "=== I. 194 所属品牌 679 + 该品牌其它会话 ==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT id, name, industry, city, is_test FROM brands WHERE id=679;
SELECT id, quote_id, status, created_at, updated_at,
       (pricing_data IS NOT NULL) AS has_pricing
FROM keyword_selection_sessions WHERE brand_id=679 ORDER BY id;
SQL
echo "=== R2 DONE ==="
