#!/usr/bin/env bash
# 只读取证 #4 · 量化本包三处行为变化的存量影响面
set -uo pipefail
PSQL="docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope"

echo "===== 0. 只读自证(必须 ERROR) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
UPDATE quotes SET service_days = service_days WHERE id = -1;
SQL

echo
echo "===== 1. 迁移 028 回填影响行数(预期 0) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT count(*) AS null_service_days_rows FROM quotes WHERE service_days IS NULL;
SQL

echo
echo "===== 2. _get_effective_window 改 fail-closed 的影响面 ====="
echo "--- 2a. 会进 run_daily_compliance_check 遍历、但没有 service_end_date 的 quote ---"
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT q.id, q.brand_name, q.status, q.service_start_date, q.service_end_date, q.service_days,
       (SELECT count(*) FROM keyword_compliance_log k WHERE k.quote_id=q.id) AS kcl_rows,
       (SELECT max(check_date) FROM keyword_compliance_log k WHERE k.quote_id=q.id) AS last_log
FROM quotes q
WHERE q.service_end_date IS NULL
  AND ( q.status='paid'
        OR (q.status='confirmed' AND COALESCE(q.service_start_date,q.paid_at) IS NOT NULL
            AND EXISTS (SELECT 1 FROM keyword_monitor_subscriptions s
                        WHERE s.quote_id=q.id AND s.status IN ('active','paused_low_balance'))))
ORDER BY q.id;
SQL

echo "--- 2b. 有 active 订阅但 service_end_date 为空的 quote(list_active_subscriptions 侧) ---"
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT q.id, q.brand_name, q.status, q.service_end_date, count(s.id) AS active_subs
FROM keyword_monitor_subscriptions s JOIN quotes q ON q.id=s.quote_id
WHERE s.status='active' AND q.service_end_date IS NULL
GROUP BY q.id, q.brand_name, q.status, q.service_end_date ORDER BY q.id;
SQL

echo
echo "===== 3. 出现率窗口上界由 start+service_days 改成 service_end_date · 会不会截掉已有日志 ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT q.id, q.brand_name,
       (q.service_start_date + (q.service_days || ' days')::interval)::date AS old_end,
       q.service_end_date AS new_end,
       max(k.check_date) AS last_log_date,
       CASE WHEN max(k.check_date) > q.service_end_date THEN 'TRUNCATES' ELSE 'no_truncation' END AS impact
FROM quotes q JOIN keyword_compliance_log k ON k.quote_id=q.id
WHERE q.service_end_date IS NOT NULL
GROUP BY q.id, q.brand_name, q.service_start_date, q.service_days, q.service_end_date
ORDER BY q.id;
SQL

echo
echo "===== 4. 门户 token 续期口径改动影响(现有 active token) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT t.quote_id, t.expires_at::date AS token_exp, q.service_end_date,
       (q.service_start_date + (COALESCE(q.service_days,365) || ' days')::interval)::date AS old_service_end
FROM client_access_tokens t JOIN quotes q ON q.id=t.quote_id
WHERE t.is_active = 1 ORDER BY t.quote_id;
SQL

echo
echo "===== 5. m3 续费桶修复的影响(21 天窗内 · 按新旧判据各数一次) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
WITH latest AS (
  SELECT DISTINCT ON (brand_id) brand_id, id, service_status, service_end_date
  FROM quotes WHERE deleted_at IS NULL ORDER BY brand_id, created_at DESC
)
SELECT
  count(*) FILTER (WHERE service_status='active' AND service_end_date IS NOT NULL
                     AND service_end_date <= CURRENT_DATE + 21) AS old_renewal_bucket,
  count(*) FILTER (WHERE service_status IN ('active','expiring','expired') AND service_end_date IS NOT NULL
                     AND service_end_date <= CURRENT_DATE + 21) AS new_renewal_bucket
FROM latest;
SQL

echo
echo "===== 6. §1.5 到期不静默:现在有几家会被点名 ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT q.id, q.brand_name, q.service_end_date,
       CASE WHEN q.service_end_date IS NULL THEN 'service_period_not_set' ELSE 'service_period_ended' END AS reason,
       (CURRENT_DATE - q.service_end_date) AS overdue_days
FROM quotes q
WHERE q.status='paid' AND q.monitoring_enabled = TRUE
  AND COALESCE(q.service_status,'active') NOT IN ('expired','paused')
  AND (q.service_end_date IS NULL OR q.service_end_date < CURRENT_DATE)
  AND EXISTS (SELECT 1 FROM confirmed_keywords ck WHERE ck.quote_id=q.id
                AND (ck.is_core IS NOT FALSE) AND COALESCE(ck.super_red_ocean,FALSE)=FALSE
                AND NOT EXISTS (SELECT 1 FROM keyword_monitor_subscriptions kms WHERE kms.keyword_id=ck.id))
  AND EXISTS (SELECT 1 FROM articles a WHERE a.quote_id=q.id AND a.first_published_at IS NOT NULL)
ORDER BY q.id;
SQL

echo "===== PROBE4 DONE ====="
