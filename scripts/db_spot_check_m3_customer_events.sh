#!/usr/bin/env bash
# CTO-C DB spot check · m3_customer_events 数据完整性
#
# 老板补充 (2026-04-27) · 8003 镜像部署后复跑必报 5 项:
#   raw_token_in_event_key       = 0
#   raw_or_share_in_metadata     = 0
#   public_report inserted=true  (PASS/FAIL)
#   selection     inserted=true  (PASS/FAIL)
#   portal        inserted=true  (PASS/FAIL)
#   mismatch / fake token / fake report 全 reject
#
# 用法:
#   # 把 staging 真实 token / share_code 用逗号串接传 RAW_TOKENS
#   # (这些值是要在 m3_customer_events 中扫不到的)
#   RAW_TOKENS="jQ4Lzwwr9pYjxXdZ,IHZ7EKMHQIJF,SOMESHARECODE" \
#     bash scripts/db_spot_check_m3_customer_events.sh
#
#   # 也支持自定义 PG_HOST_CONTAINER / PG_USER / PG_DB
#   PG_HOST_CONTAINER=omnirank-db PG_USER=geo_admin PG_DB=geo_agentscope \
#     RAW_TOKENS="..." bash scripts/db_spot_check_m3_customer_events.sh

set -e

PG_HOST_CONTAINER="${PG_HOST_CONTAINER:-omnirank-db}"
PG_USER="${PG_USER:-geo_admin}"
PG_DB="${PG_DB:-geo_agentscope}"
RAW_TOKENS="${RAW_TOKENS:-}"

pq() { docker exec "$PG_HOST_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -t -A -c "$1" 2>&1; }

echo "=== CTO-C DB spot check · m3_customer_events ==="
echo "PG_HOST_CONTAINER=$PG_HOST_CONTAINER"
echo "RAW_TOKENS=${RAW_TOKENS:-<unset · 跳过 9.2.1/9.2.2 raw 扫描>}"
echo

# ================================================================
# 9.2.1 raw_token_in_event_key
# ================================================================
RAW_IN_EK=0
if [ -n "$RAW_TOKENS" ]; then
  IFS=',' read -ra TOKS <<<"$RAW_TOKENS"
  WHERE_CLAUSE=""
  for t in "${TOKS[@]}"; do
    t_clean=$(echo "$t" | xargs)  # trim
    if [ -n "$t_clean" ]; then
      [ -n "$WHERE_CLAUSE" ] && WHERE_CLAUSE+=" OR "
      WHERE_CLAUSE+="event_key LIKE '%${t_clean}%'"
    fi
  done
  RAW_IN_EK=$(pq "SELECT COUNT(*) FROM m3_customer_events WHERE $WHERE_CLAUSE")
fi
echo "raw_token_in_event_key       = $RAW_IN_EK     [期望 0]"
[ "$RAW_IN_EK" = "0" ] && echo "  [PASS] 9.2.1" || echo "  [FAIL] 9.2.1 · raw token 漏入 event_key"

# ================================================================
# 9.2.2 raw_or_share_in_metadata
# ================================================================
RAW_IN_META=0
SHARE_IN_META=$(pq "SELECT COUNT(*) FROM m3_customer_events WHERE metadata ? 'share_code' OR metadata ? 'raw_token'")
RAW_TOKEN_IN_META=0
if [ -n "$RAW_TOKENS" ]; then
  IFS=',' read -ra TOKS <<<"$RAW_TOKENS"
  WHERE_META=""
  for t in "${TOKS[@]}"; do
    t_clean=$(echo "$t" | xargs)
    if [ -n "$t_clean" ]; then
      [ -n "$WHERE_META" ] && WHERE_META+=" OR "
      WHERE_META+="metadata::text LIKE '%${t_clean}%'"
    fi
  done
  RAW_TOKEN_IN_META=$(pq "SELECT COUNT(*) FROM m3_customer_events WHERE $WHERE_META")
fi
RAW_IN_META=$((SHARE_IN_META + RAW_TOKEN_IN_META))
echo "raw_or_share_in_metadata     = $RAW_IN_META     [期望 0 · share_code/raw_token field=$SHARE_IN_META · raw value=$RAW_TOKEN_IN_META]"
[ "$RAW_IN_META" = "0" ] && echo "  [PASS] 9.2.2" || echo "  [FAIL] 9.2.2 · metadata 漏 raw token / share_code"

# ================================================================
# 9.2.3-5 三路 inserted 计数 (按 source 分桶 · 任意非 0 视为 inserted)
# ================================================================
echo
echo "---- 三路写入 (近 1 小时) ----"
for src in public_report selection portal; do
  CNT=$(pq "SELECT COUNT(*) FROM m3_customer_events WHERE source='$src' AND occurred_at >= NOW() - INTERVAL '1 hour'")
  if [ -n "$CNT" ] && [ "$CNT" -gt 0 ]; then
    echo "  $src : $CNT 条 [PASS · inserted=true]"
  else
    echo "  $src : 0 条 [FAIL · 复跑期间无任何 $src 事件]"
  fi
done

# ================================================================
# 9.2.6-9 reject 检查 · DB 不该写入 mismatch / fake 事件
# 我们靠 smoke log + DB count 双验:
#   smoke log 应有 D1-D4 的 [PASS] 行
#   DB 不应有 'smoke_d1'/'smoke_d2'/'smoke_d3'/'smoke_d4' 开头的 event_key
# ================================================================
echo
echo "---- reject 路径 DB 校验 (smoke_d* 不应入库) ----"
REJECT_LEAK=$(pq "SELECT COUNT(*) FROM m3_customer_events WHERE event_key LIKE 'smoke_d1\_%' ESCAPE '\\' OR event_key LIKE 'smoke_d2\_%' ESCAPE '\\' OR event_key LIKE 'smoke_d3\_%' ESCAPE '\\' OR event_key LIKE 'smoke_d4\_%' ESCAPE '\\'")
if [ "$REJECT_LEAK" = "0" ]; then
  echo "  smoke_d* 行数 = 0 [PASS · 9.2.6-9 reject 路径未污染 DB]"
else
  echo "  smoke_d* 行数 = $REJECT_LEAK [FAIL · 安全 reject 失效 · 检查 D 反查代码]"
fi

# ================================================================
# 总结 · token_hash / ip_hash / user_agent_hash 长度
# ================================================================
echo
echo "---- 哈希长度抽查 (期望 32 char) ----"
HASH_LEN_OK=$(pq "SELECT COUNT(*) FROM m3_customer_events WHERE token_hash IS NOT NULL AND LENGTH(token_hash) <> 32")
echo "  token_hash 长度 != 32 的行数: $HASH_LEN_OK"
HASH_IP_OK=$(pq "SELECT COUNT(*) FROM m3_customer_events WHERE ip_hash IS NOT NULL AND LENGTH(ip_hash) <> 32")
echo "  ip_hash 长度 != 32 的行数:    $HASH_IP_OK"
HASH_UA_OK=$(pq "SELECT COUNT(*) FROM m3_customer_events WHERE user_agent_hash IS NOT NULL AND LENGTH(user_agent_hash) <> 32")
echo "  user_agent_hash 长度 != 32:   $HASH_UA_OK"

echo
echo "=== 复跑回填 §9.3 用以下数字 ==="
echo "raw_token_in_event_key   = $RAW_IN_EK"
echo "raw_or_share_in_metadata = $RAW_IN_META"
echo
echo "=== spot check 完成 ==="
