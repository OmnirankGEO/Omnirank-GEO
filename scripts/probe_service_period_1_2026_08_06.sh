#!/usr/bin/env bash
# 只读取证 · 服务期双钟分裂(WO_SERVICE_PERIOD_SSOT_2026-08-06 §0/§2)
# 通道纪律:heredoc 逐条语句(单条 psql -c 多语句是一个事务,自证会失效)
set -uo pipefail

PSQL="docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope"

echo "===== 0. 只读通道自证(这条 UPDATE 必须失败,否则本次取证作废) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
UPDATE quotes SET service_months = service_months WHERE id = -1;
SQL
echo "---- 上面必须出现 ERROR: cannot execute UPDATE in a read-only transaction ----"

echo
echo "===== 0b. 反向对照:同一只读会话里 SELECT 必须成功 ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT 'select_ok' AS probe, count(*) AS quotes_total FROM quotes;
SQL

echo
echo "===== 1. quotes 服务期相关列的 SQL 4 维(列名/类型/默认值) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT column_name, data_type, column_default, is_nullable
FROM information_schema.columns
WHERE table_name='quotes'
  AND (column_name LIKE 'service%' OR column_name IN ('paid_at','monitoring_enabled','status','tier'))
ORDER BY column_name;
SQL

echo
echo "===== 2. 全部 status='paid' 报价 · 三钟对照表(§2 存量清单原料) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
\pset border 2
SELECT q.id,
       q.brand_name,
       q.service_months            AS months,
       q.service_days              AS days,
       q.service_start_date        AS start_d,
       q.service_end_date          AS end_d,
       q.paid_at::date             AS paid_d,
       q.service_status            AS svc_status,
       q.monitoring_enabled        AS mon_on,
       (q.service_start_date + (COALESCE(q.service_days,365) || ' days')::interval)::date AS ui_countdown_end,
       (q.service_end_date - CURRENT_DATE)                                                AS gate_days_left,
       ((q.service_start_date + (COALESCE(q.service_days,365) || ' days')::interval)::date - CURRENT_DATE) AS ui_days_left
FROM quotes q
WHERE q.status = 'paid'
ORDER BY q.id;
SQL

echo
echo "===== 3. 两钟不一致计数(判据:end_d 与 ui_countdown_end 不同日) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT count(*) FILTER (WHERE service_end_date IS DISTINCT FROM
          (service_start_date + (COALESCE(service_days,365) || ' days')::interval)::date) AS mismatch_cnt,
       count(*) AS paid_total
FROM quotes WHERE status='paid';
SQL

echo
echo "===== 4. 自动监测轮换资格闸 现况(四条件逐条命中数) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT
  count(*) FILTER (WHERE status='paid')                                    AS c1_paid,
  count(*) FILTER (WHERE status='paid' AND monitoring_enabled)             AS c2_mon_on,
  count(*) FILTER (WHERE status='paid' AND monitoring_enabled
                     AND service_end_date IS NOT NULL
                     AND service_end_date >= CURRENT_DATE)                 AS c3_in_period,
  count(*) FILTER (WHERE status='paid' AND monitoring_enabled
                     AND service_end_date IS NOT NULL
                     AND service_end_date >= CURRENT_DATE
                     AND COALESCE(service_status,'active') NOT IN ('expired','paused')) AS c4_status_ok
FROM quotes;
SQL

echo
echo "===== 5. 加上 有发文 + 有核心词 两条件后的最终轮换池 ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT q.id, q.brand_name, q.service_end_date
FROM quotes q
WHERE q.status = 'paid'
  AND q.monitoring_enabled = TRUE
  AND q.service_end_date IS NOT NULL
  AND q.service_end_date >= CURRENT_DATE
  AND COALESCE(q.service_status, 'active') NOT IN ('expired','paused')
  AND EXISTS (SELECT 1 FROM confirmed_keywords ck WHERE ck.quote_id=q.id
                AND (ck.is_core IS NOT FALSE) AND COALESCE(ck.super_red_ocean,FALSE)=FALSE)
  AND EXISTS (SELECT 1 FROM articles a WHERE a.quote_id=q.id AND a.first_published_at IS NOT NULL)
ORDER BY q.id;
SQL

echo
echo "===== 6. 反事实:仅把服务期闸换成 ui_countdown_end 后的池(证明第 4 条闸是瓶颈) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT q.id, q.brand_name,
       q.service_end_date AS gate_end,
       (q.service_start_date + (COALESCE(q.service_days,365) || ' days')::interval)::date AS ui_end
FROM quotes q
WHERE q.status = 'paid'
  AND q.monitoring_enabled = TRUE
  AND (q.service_start_date + (COALESCE(q.service_days,365) || ' days')::interval)::date >= CURRENT_DATE
  AND COALESCE(q.service_status, 'active') NOT IN ('expired','paused')
  AND EXISTS (SELECT 1 FROM confirmed_keywords ck WHERE ck.quote_id=q.id
                AND (ck.is_core IS NOT FALSE) AND COALESCE(ck.super_red_ocean,FALSE)=FALSE)
  AND EXISTS (SELECT 1 FROM articles a WHERE a.quote_id=q.id AND a.first_published_at IS NOT NULL)
ORDER BY q.id;
SQL

echo
echo "===== 7. 晨光富士 #286 / 栖舍 #372 单张详查 ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
\x on
SELECT id, brand_name, status, service_status, service_months, service_days,
       service_start_date, service_end_date, paid_at, monthly_price, paid_amount,
       monitoring_enabled, tier, created_at, updated_at
FROM quotes WHERE id IN (286, 372);
\x off
SQL

echo
echo "===== 8. service_days 的分布(看它到底是谁写的) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT service_days, count(*) FROM quotes GROUP BY service_days ORDER BY 2 DESC;
SQL

echo "===== PROBE DONE ====="
