#!/usr/bin/env bash
# 只读取证 R5 · 194 候选池里 commercial_delivery_eligible 等裁决字段的实际落库值
set -u
DB="docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope -v ON_ERROR_STOP=0"

echo "=== A. 194 keywords_snapshot 每词的裁决字段 + 是否被勾选 ==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
WITH s AS (SELECT keywords_snapshot::jsonb sn, selected_keyword_ids::jsonb sel
           FROM keyword_selection_sessions WHERE id=194)
SELECT k->>'id'                            AS id,
       k->>'keyword'                       AS keyword,
       k->>'category_label'                AS cat,
       k->>'intent'                        AS intent,
       k->>'intent_type'                   AS intent_type,
       k->>'commercial_delivery_eligible'  AS cde,
       k->>'recommended'                   AS recommended,
       k->>'default_selected'              AS default_sel,
       k->>'hard_block'                    AS hard_block,
       k->>'policy_version'                AS pol_ver,
       (s.sel @> to_jsonb((k->>'id')::int)) AS is_selected
FROM s, LATERAL jsonb_array_elements(s.sn) k
ORDER BY (k->>'id')::int;
SQL

echo
echo "=== B. 194 keywords_snapshot[0] 全键(看是走了哪条建快照路径)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT jsonb_object_keys((keywords_snapshot::jsonb)->0) FROM keyword_selection_sessions WHERE id=194;
SQL

echo
echo "=== C. 194 的 selected_keyword_ids / custom / pending 原文 ==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT selected_keyword_ids, custom_keywords, pending_keywords,
       delivery_excluded_custom_keywords
FROM keyword_selection_sessions WHERE id=194;
SQL

echo
echo "=== D. 全局:落库候选里 commercial_delivery_eligible=false 却被勾选的词有多少(闸失效面)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
WITH x AS (
  SELECT s.id AS sid, s.status,
         k->>'keyword' AS kw,
         k->>'commercial_delivery_eligible' AS cde,
         (s.selected_keyword_ids::jsonb @> to_jsonb((k->>'id')::int)) AS sel
  FROM keyword_selection_sessions s,
       LATERAL jsonb_array_elements(s.keywords_snapshot::jsonb) k
  WHERE s.keywords_snapshot IS NOT NULL
    AND s.selected_keyword_ids IS NOT NULL
    AND (k->>'id') ~ '^[0-9]+$'
)
SELECT COUNT(*) FILTER (WHERE cde='false' AND sel)            AS ineligible_but_selected,
       COUNT(*) FILTER (WHERE cde='false' AND NOT sel)        AS ineligible_not_selected,
       COUNT(*) FILTER (WHERE cde='true'  AND sel)            AS eligible_selected,
       COUNT(*) FILTER (WHERE cde IS NULL)                    AS cde_missing,
       COUNT(*)                                               AS total
FROM x;
SQL

echo
echo "=== E. 上面那批「不合格却被勾选」的词样例(证明 D 不是恒空)==="
$DB <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
WITH x AS (
  SELECT s.id AS sid, s.status, k->>'keyword' AS kw,
         k->>'commercial_delivery_eligible' AS cde,
         (s.selected_keyword_ids::jsonb @> to_jsonb((k->>'id')::int)) AS sel
  FROM keyword_selection_sessions s,
       LATERAL jsonb_array_elements(s.keywords_snapshot::jsonb) k
  WHERE s.keywords_snapshot IS NOT NULL AND s.selected_keyword_ids IS NOT NULL
    AND (k->>'id') ~ '^[0-9]+$'
)
SELECT sid, status, kw, cde FROM x WHERE cde='false' AND sel ORDER BY sid DESC LIMIT 25;
SQL
echo "=== R5 DONE ==="
