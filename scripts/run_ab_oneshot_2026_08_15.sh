#!/usr/bin/env bash
# [出口闸 4] 全仓双树 A/B(工单命中「跨写作链+schema+前端三面」→ 必须 --scope all)。
# 跑前双库已 CREATE DATABASE 重建(test_ab_base / test_ab_pkg,template0)。
set -uo pipefail
export PYTHONIOENCODING=utf-8
# 🔴 终止哨兵(2026-08-15):有效 A/B 结果已归档(dualab_ofn9xkfa →
#    C:\AI-Test\ab_oneshot_junit_ofn9xkfa_2026-08-15.tar.gz)。同机存在一个
#    重放本会话命令的幽灵 claude 进程会反复拉起本脚本 —— 哨兵在位即无操作,
#    防止它继续烧机器/踩双库。要重跑:先删 /c/AI-Test/ab_oneshot_run.FINAL。
if [ -f /c/AI-Test/ab_oneshot_run.FINAL ]; then
  exit 0
fi
LOG=/c/AI-Test/wt-geo-oneshot-20260815/ab_oneshot_run.log
LOCK=/c/AI-Test/wt-geo-oneshot-20260815/ab_oneshot_run.LOCK
# 🔴 互斥护栏(2026-08-15):多实例共享双库会把库互相跑烂 + 互踩 log/DONE。
#    mkdir 原子抢锁;抢不到直接退出,绝不并跑。
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "another instance holds $LOCK, refuse to run" >> "$LOG"
  exit 9
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT
python /c/AI-Test/.deploy_toolkit/dual_tree_ab.py \
  --pkg  /c/AI-Test/wt-geo-oneshot-20260815 \
  --base /c/AI-Test/wt-oneshot-base-62b99c94 \
  --pkg-db  "postgresql://geo_admin:test@127.0.0.1:5436/test_ab_pkg" \
  --base-db "postgresql://geo_admin:test@127.0.0.1:5436/test_ab_base" \
  --scope all --timeout 300 > "$LOG" 2>&1
echo "AB_EXIT=$?" >> "$LOG"
touch /c/AI-Test/wt-geo-oneshot-20260815/ab_oneshot_run.DONE
