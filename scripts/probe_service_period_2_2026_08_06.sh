#!/usr/bin/env bash
# 只读取证 #2 · #286 三钟来源考古 + 轮换池其余闸的实际瓶颈
set -uo pipefail
PSQL="docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope"

echo "===== 0. 只读自证(必须 ERROR) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
UPDATE quotes SET service_days = service_days WHERE id = -1;
SQL

echo
echo "===== 1. #286 / #372 的 audit_logs 全轨迹 ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT id, created_at, action, username, summary, after
FROM audit_logs
WHERE entity_type='quote' AND entity_id IN (286,372)
ORDER BY id;
SQL

echo
echo "===== 2. 13 张 paid 报价 · 其余三条闸逐张(有核心词 / 有发文 / monitoring_enabled) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT q.id, q.brand_name, q.monitoring_enabled AS mon_on,
       (SELECT count(*) FROM confirmed_keywords ck WHERE ck.quote_id=q.id
          AND (ck.is_core IS NOT FALSE) AND COALESCE(ck.super_red_ocean,FALSE)=FALSE) AS core_kw,
       (SELECT count(*) FROM articles a WHERE a.quote_id=q.id AND a.first_published_at IS NOT NULL) AS published,
       q.service_end_date, q.service_days
FROM quotes q WHERE q.status='paid' ORDER BY q.id;
SQL

echo
echo "===== 3. 反事实 B:只放宽服务期闸(其余三条不动)· 池会变成几张 ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT count(*) AS pool_if_no_period_gate
FROM quotes q
WHERE q.status='paid' AND q.monitoring_enabled = TRUE
  AND EXISTS (SELECT 1 FROM confirmed_keywords ck WHERE ck.quote_id=q.id
                AND (ck.is_core IS NOT FALSE) AND COALESCE(ck.super_red_ocean,FALSE)=FALSE)
  AND EXISTS (SELECT 1 FROM articles a WHERE a.quote_id=q.id AND a.first_published_at IS NOT NULL);
SQL

echo
echo "===== 4. keyword_compliance_log 里 #286 的达标累计(履约钟第三读点) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT quote_id, count(*) AS log_rows,
       count(*) FILTER (WHERE is_compliant) AS compliant_days,
       min(check_date) AS first_day, max(check_date) AS last_day
FROM keyword_compliance_log WHERE quote_id IN (94,286) GROUP BY quote_id;
SQL

echo
echo "===== 5. 谁调过 service-config?(audit 无记录 → 查 quotes.service_days<>365 的那 12 张) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT id, brand_name, status, service_days, service_months, service_start_date, service_end_date, updated_at
FROM quotes WHERE service_days IS DISTINCT FROM 365 ORDER BY id;
SQL

echo
echo "===== 6. client_access_tokens 与 service_end_date 是否同步(第四个受害读点) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT t.quote_id, t.expires_at::date AS token_exp, q.service_end_date,
       (t.expires_at::date - q.service_end_date) AS drift_days
FROM client_access_tokens t JOIN quotes q ON q.id = t.quote_id
WHERE q.status='paid' AND t.is_active = 1
ORDER BY t.quote_id;
SQL

echo
echo "===== 7. monitoring_tasks 最近一次真正跑过的 quote(轮换实证) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT quote_id, count(*) AS runs, max(created_at) AS last_run
FROM monitoring_tasks
WHERE created_at > CURRENT_DATE - INTERVAL '60 days'
GROUP BY quote_id ORDER BY last_run DESC LIMIT 15;
SQL

echo "===== PROBE2 DONE ====="
