#!/usr/bin/env bash
# [P0 血缘三报 2026-08-16] 复现事故锁链 + 证明「statement_timeout 单独不够」。
#
# 事故链(工单 §0):血缘三报 SELECT 长时间不结束 → 第 30 班 prestart 的 ALTER
# 排在它们后面(granted=f)→ PG 锁队列 FIFO → 后面所有 SELECT 全排死。
#
# 三种形态,**成对判据**:
#   A. 事务跨应用侧长耗时(= _load_rows 在事务里做 OSS 回读)+ statement_timeout=5s
#      → 必须【仍然堵】。证明工单要求的止血层单独**拦不住**本次事故形态。
#   B. 同上但加 idle_in_transaction_session_timeout=5s → 必须【不堵】。
#   C. 事务在应用侧长耗时**之前**就结束(本包根治改法)   → 必须【不堵】。
# 三条同向全绿 = 判据零判别力。
#
# 🔴 第一版这个脚本自己是坏的:holder 用了 `psql -c "SET ..." <<SQL`,
#    而 psql 带 -c 时**根本不读 stdin** → holder 事务从没跑起来,
#    三态全 alter_blocked=0,"A 必须堵"假红。教训:先证明 holder 真的在持锁,
#    再去读被阻塞计数(反向对照物本身为零 = 什么都没证明)。
set -u
C=lineage-prodshape-pg
export MSYS_NO_PATHCONV=1

dq() { docker exec -i "$C" psql -U geo_admin -d geo_agentscope -tAX -v ON_ERROR_STOP=1 "$@"; }

cleanup() {
  docker exec -i "$C" psql -U geo_admin -d geo_agentscope -tAX -c \
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity
      WHERE datname='geo_agentscope' AND pid <> pg_backend_pid()
        AND application_name IN ('p0_holder','p0_alter','p0_reader')" >/dev/null 2>&1
  docker exec -i "$C" psql -U geo_admin -d geo_agentscope -tAX -c \
    "ALTER TABLE geo_research_articles DROP COLUMN IF EXISTS p0_lock_probe_col" >/dev/null 2>&1
  sleep 1
}

run_case() {
  local name="$1" holder_guc="$2" hold_inside="$3"
  cleanup

  local tail_in tail_out
  if [ "$hold_inside" = "yes" ]; then
    # 事务内跑完 SELECT 后应用侧继续干活(\! sleep = OSS 回读),事务**不关**
    tail_in=$'\\! sleep 22\nCOMMIT;'
  else
    # 根治形态:取完数即结束事务,应用侧耗时发生在事务**之外**
    tail_in=$'COMMIT;\n\\! sleep 22'
  fi

  docker exec -i "$C" psql -U geo_admin -d geo_agentscope -tAX >/dev/null 2>&1 <<SQL &
SET application_name='p0_holder';
BEGIN;
${holder_guc}
SELECT count(*) FROM geo_research_articles;
${tail_in}
SQL
  sleep 5

  # ── 前置自证:holder 必须真的开着事务并持有该表的锁,否则本轮判据无效 ──
  local holder_locks
  holder_locks=$(dq -c "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING (pid)
                         WHERE a.application_name='p0_holder'
                           AND l.relation='geo_research_articles'::regclass" | tr -d '[:space:]')

  docker exec -i "$C" psql -U geo_admin -d geo_agentscope -tAX >/dev/null 2>&1 <<'SQL' &
SET application_name='p0_alter';
SET lock_timeout='9s';
ALTER TABLE geo_research_articles ADD COLUMN p0_lock_probe_col int;
SQL
  sleep 3

  docker exec -i "$C" psql -U geo_admin -d geo_agentscope -tAX >/dev/null 2>&1 <<'SQL' &
SET application_name='p0_reader';
SET lock_timeout='7s';
SELECT count(*) FROM geo_research_articles;
SQL
  sleep 3

  local ba br
  ba=$(dq -c "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING (pid)
               WHERE a.application_name='p0_alter' AND NOT l.granted" | tr -d '[:space:]')
  br=$(dq -c "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING (pid)
               WHERE a.application_name='p0_reader' AND NOT l.granted" | tr -d '[:space:]')
  wait 2>/dev/null
  cleanup
  echo "${name}|holder_locks=${holder_locks:-0}|alter_blocked=${ba:-0}|reader_blocked=${br:-0}"
}

echo "=== P0 锁链探针 · $(date '+%F %T') ==="
A=$(run_case "A 事务内跨应用耗时 + statement_timeout=5s" "SET LOCAL statement_timeout='5s';" yes)
B=$(run_case "B 同上 + idle_in_txn_timeout=5s" "SET LOCAL statement_timeout='5s'; SET LOCAL idle_in_transaction_session_timeout='5s';" yes)
Cc=$(run_case "C 事务先结束再做应用侧耗时(本包改法)" "SET LOCAL statement_timeout='5s';" no)
echo "$A"; echo "$B"; echo "$Cc"; echo "---"

# 判据可用性第 0 关:A 的 holder 必须真的持锁,否则"A 必须堵"是假红
a_holds=$(echo "$A" | grep -c 'holder_locks=[1-9]')
if [ "$a_holds" != 1 ]; then
  echo "RESULT=INVALID A 的 holder 没持锁 → 判据不可用(不是结论不成立)"; exit 3
fi
a_ok=$(echo "$A"  | grep -c 'alter_blocked=[1-9]')
b_ok=$(echo "$B"  | grep -c 'alter_blocked=0')
c_ok=$(echo "$Cc" | grep -c 'alter_blocked=0')
echo "判据 A(必须堵)=$a_ok  B(必须不堵)=$b_ok  C(必须不堵)=$c_ok"
if [ "$a_ok" = 1 ] && [ "$b_ok" = 1 ] && [ "$c_ok" = 1 ]; then
  echo "RESULT=PASS 三态可区分:statement_timeout 单独拦不住(A 堵);关事务/掐 idle 才拦得住"
  exit 0
fi
echo "RESULT=FAIL 判据无判别力或结论不成立"; exit 1
