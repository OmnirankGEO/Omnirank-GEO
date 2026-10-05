#!/usr/bin/env bash
# 只读取证 #5 · 晨光富士 #286 到底是被哪一条闸停的(工单假设待证)
set -uo pipefail
PSQL="docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope"

echo "===== 0. 只读自证(必须 ERROR) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
UPDATE quotes SET service_days = service_days WHERE id = -1;
SQL

echo
echo "===== 1. #286 的核心词 / KMS 订阅 / is_monitored 状态 ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT ck.id, left(ck.keyword,20) AS kw, ck.is_core, ck.super_red_ocean,
       ck.is_monitored, ck.monitoring_status,
       s.id AS kms_id, s.status AS kms_status, s.enabled_at::date AS kms_from,
       s.last_charged_at::date AS last_charged
FROM confirmed_keywords ck
LEFT JOIN keyword_monitor_subscriptions s ON s.keyword_id = ck.id
WHERE ck.quote_id = 286 ORDER BY ck.id;
SQL

echo
echo "===== 2. #94 同样一份(对照:它是唯一还在轮换里的) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT ck.id, left(ck.keyword,20) AS kw, ck.is_monitored, ck.monitoring_status,
       s.status AS kms_status, s.last_charged_at::date AS last_charged
FROM confirmed_keywords ck
LEFT JOIN keyword_monitor_subscriptions s ON s.keyword_id = ck.id
WHERE ck.quote_id = 94 ORDER BY ck.id;
SQL

echo
echo "===== 3. #286 最近的 monitoring_tasks(真跑过没有) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT id, status, trigger_type, keyword_count, created_at, completed_at
FROM monitoring_tasks WHERE quote_id = 286 ORDER BY id DESC LIMIT 10;
SQL

echo
echo "===== 4. 全站 KMS 订阅现状(list_active_subscriptions 的原料) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT s.status, count(*) FROM keyword_monitor_subscriptions s GROUP BY s.status ORDER BY 2 DESC;
SQL

echo
echo "===== 5. get_monitoring_enabled_clients 四条件之外那两条(无KMS核心词/有发文)分别卡掉谁 ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT q.id, q.brand_name, q.monitoring_enabled, q.service_end_date,
       (SELECT count(*) FROM confirmed_keywords ck WHERE ck.quote_id=q.id
          AND (ck.is_core IS NOT FALSE) AND COALESCE(ck.super_red_ocean,FALSE)=FALSE) AS core_kw,
       (SELECT count(*) FROM confirmed_keywords ck WHERE ck.quote_id=q.id
          AND (ck.is_core IS NOT FALSE) AND COALESCE(ck.super_red_ocean,FALSE)=FALSE
          AND NOT EXISTS (SELECT 1 FROM keyword_monitor_subscriptions kms WHERE kms.keyword_id=ck.id)
        ) AS core_kw_without_sub,
       (SELECT count(*) FROM articles a WHERE a.quote_id=q.id AND a.first_published_at IS NOT NULL) AS published
FROM quotes q WHERE q.status='paid' AND q.monitoring_enabled = TRUE ORDER BY q.id;
SQL

echo
echo "===== 6. 最近 30 天真正写进 keyword_compliance_log 的 quote(谁还在被服务) ====="
$PSQL <<'SQL'
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT quote_id, count(*) AS rows_30d, max(check_date) AS last_day
FROM keyword_compliance_log WHERE check_date > CURRENT_DATE - 30
GROUP BY quote_id ORDER BY quote_id;
SQL

echo "===== PROBE5 DONE ====="
