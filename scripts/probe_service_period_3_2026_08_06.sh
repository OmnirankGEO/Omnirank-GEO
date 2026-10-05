#!/usr/bin/env bash
set -uo pipefail
PSQL="docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope"

echo "===== 0. 只读自证(必须 ERROR) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
UPDATE quotes SET service_days = service_days WHERE id = -1;
SQL

echo
echo "===== 1. audit_logs 真列名 ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT column_name, data_type FROM information_schema.columns
WHERE table_name='audit_logs' ORDER BY ordinal_position;
SQL

echo
echo "===== 2. #286/#372/#94 审计轨迹 ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
\x on
SELECT * FROM audit_logs
WHERE entity_type='quote' AND entity_id IN (94,286,372)
ORDER BY id;
\x off
SQL

echo
echo "===== 3. 晨光富士 全部报价单(同品牌多单 · §2 需要) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT id, brand_id, status, service_status, monthly_price, paid_amount,
       service_months, service_days, service_start_date, service_end_date, paid_at, created_at
FROM quotes WHERE brand_name LIKE '%晨光富士%' ORDER BY id;
SQL

echo
echo "===== 4. 11 张不一致报价的金额/期限原始凭据(paid_amount vs monthly_price 推月数) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT id, brand_name, tier, monthly_price, paid_amount,
       CASE WHEN COALESCE(monthly_price,0) > 0
            THEN ROUND(paid_amount::numeric / monthly_price::numeric, 2) END AS implied_months,
       service_months, service_days, service_start_date, service_end_date, paid_at::date AS paid_d
FROM quotes
WHERE status='paid'
  AND service_end_date IS DISTINCT FROM
      (service_start_date + (COALESCE(service_days,365) || ' days')::interval)::date
ORDER BY id;
SQL

echo
echo "===== 5. monitoring_tasks 的 quote 关联列到底叫什么 ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT column_name, data_type FROM information_schema.columns
WHERE table_name='monitoring_tasks' ORDER BY ordinal_position;
SQL

echo "===== PROBE3 DONE ====="
