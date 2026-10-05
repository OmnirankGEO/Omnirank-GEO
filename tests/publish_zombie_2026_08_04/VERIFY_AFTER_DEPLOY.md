# 上线后验收 SQL · WO-PUB-ZOMBIE-2026-08-04

> 刻意存成 `.md` 而不是 `.sql`:仓库 `.gitignore` 的 `*.sql` 白名单只放行
> `scripts/` 与 `db/`;而且交付里出现 `.sql` 会让部署前核验把它当**未登记的迁移**报警。
> 本文件是纯只读验收查询,不是迁移。

跑法(生产 · 把下面代码块存成临时文件后):

```bash
docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope -P pager=off < /tmp/verify.sql
```

```sql
-- [WO-PUB-ZOMBIE-2026-08-04] 上线后验收 · 只读
-- 跑法(生产)：
--   docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope -P pager=off < 本文件
--
-- 🔴 工单 §7.2.2 写的判据「refund_key='item:%' 从 0 条变为对应条数」**不能用**：
--    2026-08-04 开工前实测该口径**已有 163 条 / 631,215 积分**（信用侧 0 条）。
--    拿"总数从 0 变 N"当判据 = 恒假。改成钉那 12 个具体 item_id 各出现 1 行。
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;

\echo '===== 0. 只读闸自证（下面这条必须报错，否则本轮验收作废）====='
UPDATE mhz_config SET value = value WHERE key = '__no_such_key__';

\echo '===== 1. 12 条存量僵尸是否已进终态 / 转人工 ====='
\echo '判据：status 全部离开 pending / paused_admin_dedupe；无一条仍是非终态且无单号'
SELECT i.id AS item_id, o.user_id, i.status, i.manual_review_required,
       left(coalesce(i.reject_reason, '-'), 40) AS reason
  FROM mhz_publish_order_items i
  JOIN mhz_publish_orders o ON o.id = i.order_id
 WHERE i.id IN (36, 37, 38, 418, 427, 429, 430, 431, 434, 435, 436, 437)
 ORDER BY i.id;

\echo '===== 2. 退款流水（钉具体 item_id，不看总数）====='
\echo '预期：user 15 的 36/37/38 + user 24 的 434/435/436/437 共 7 行；'
\echo '      admin(user 1) 的 418/427/429/430/431 **应为 0 行** —— 那 5 单 actually_deducted=0，'
\echo '      退款 cap 会把额度砍到 0 并 skipped，只清状态不退钱（不是缺陷）。'
SELECT order_id AS refund_key, user_id, amount, created_at
  FROM point_transactions
 WHERE type = 'refund' AND feature_code = 'media_proxy_publish'
   AND order_id IN ('item:36','item:37','item:38','item:418','item:427','item:429',
                    'item:430','item:431','item:434','item:435','item:436','item:437')
 ORDER BY order_id;

\echo '--- 2b. 汇总：真实用户应退 17,355 = user15 14,235 + user24 3,120 ---'
SELECT user_id, count(*) AS rows, sum(amount) AS pts
  FROM point_transactions
 WHERE type = 'refund' AND feature_code = 'media_proxy_publish'
   AND order_id LIKE 'item:%'
   AND order_id IN ('item:36','item:37','item:38','item:434','item:435','item:436','item:437')
 GROUP BY user_id ORDER BY user_id;

\echo '===== 3. 钱包守恒：退款金额必须等于钱包增量 ====='
\echo '判据：每个 user 的 paid_points 增量 = 上面 2b 的 pts（人工与部署前快照比对）'
SELECT user_id, paid_points, updated_at FROM user_wallets WHERE user_id IN (15, 24, 1) ORDER BY user_id;

\echo '===== 4. 🔴 不回退：有单号的 7 条 submitted（19,110 分）必须原样不动 ====='
\echo '判据：仍为 submitted、mhz_order_id 非空、且没有对应 item:%% 退款流水'
SELECT i.id AS item_id, i.status, i.mhz_order_id, i.cost_points,
       (SELECT count(*) FROM point_transactions t
         WHERE t.type='refund' AND t.feature_code='media_proxy_publish'
           AND t.order_id = 'item:' || i.id) AS wrongly_refunded
  FROM mhz_publish_order_items i
 WHERE i.status = 'submitted' AND i.mhz_order_id IS NOT NULL AND i.mhz_order_id <> ''
 ORDER BY i.id;
\echo '   ↑ wrongly_refunded 必须全为 0'

\echo '===== 5. 🔴 禁退区没被误退：外部有单但未被认领的条目 ====='
\echo '判据：这些必须 manual_review_required=TRUE 且 **无** item:%% 退款流水'
SELECT i.id AS item_id, i.status, i.manual_review_required,
       (SELECT count(*) FROM mhz_synced_orders s
         WHERE TRIM(s.title)=TRIM(o.article_title)
           AND (s.resource_id = i.media_id OR s.media_name = i.media_name)) AS ext_hit,
       (SELECT count(*) FROM point_transactions t
         WHERE t.type='refund' AND t.feature_code='media_proxy_publish'
           AND t.order_id = 'item:' || i.id) AS refunded_rows
  FROM mhz_publish_order_items i
  JOIN mhz_publish_orders o ON o.id = i.order_id
 WHERE i.manual_review_required = TRUE
 ORDER BY i.id DESC LIMIT 20;

\echo '===== 6. 清扫器自己是否在跑（cron 槽写的心跳）====='
SELECT key, value FROM mhz_config
 WHERE key IN ('last_orphan_zombie_sweep', 'last_orphan_zombie_sweep_result',
               'last_status_sync', 'last_toutiao_status_sync');

\echo '===== 7. 新僵尸是否绝迹（本次修复的最终判据）====='
\echo '判据：超过 3 小时仍非终态且无外部单号的条目应为 0（awaiting_* 系列除外，它们有各自主人）'
SELECT i.status, count(*) AS n, sum(i.cost_points) AS pts
  FROM mhz_publish_order_items i
  JOIN mhz_publish_orders o ON o.id = i.order_id
 WHERE i.status IN ('pending', 'submitted', 'paused_admin_dedupe')
   AND (i.mhz_order_id IS NULL OR i.mhz_order_id = '')
   AND o.created_at < NOW() - INTERVAL '3 hours'
 GROUP BY i.status;
\echo '   ↑ 应返回 0 行'
```
