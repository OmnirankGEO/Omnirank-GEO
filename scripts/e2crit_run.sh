#!/usr/bin/env bash
# 两包分进程各跑一次(同进程会争 DATABASE_URL)。用法:e2crit_run.sh [p0|pkge|both]
set -u
# 🔴 ROOT 必须从**脚本自己的位置**推,不许写死路径。
#    2026-08-27 实测:上一版写死了 `C:/AI-Test/wt-woa-e2crit-20260827`,
#    在新 worktree 里跑它,报的是**另一棵树**的绿数 —— 数看着完全正常,
#    只是跟你刚改的代码毫无关系。「finding 坐标必须带长在哪棵树上」的同族。
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
P0_DSN="postgresql://geo_admin:p0fixpass@localhost:55491/defgeo_p0fix_test"
P0_REGRESS_DSN="postgresql://geo_admin:p0fixpass@localhost:55491/geo_defgeo_regress_test"
PKGE_DSN="postgresql://geo_admin:testpw@localhost:55492/geo_defgeo_pkge_test"
WHICH="${1:-both}"
cd "$ROOT" || exit 2
rc=0
if [ "$WHICH" = "p0" ] || [ "$WHICH" = "both" ]; then
  echo "── funding_p0 ─────────────────────────────"
  DEFGEO_P0FIX_TEST_DSN="$P0_DSN" TEST_DATABASE_URL="$P0_REGRESS_DSN" PYTHONIOENCODING=utf-8 \
    python -m pytest tests/defgeo_funding_p0_2026_08_25 -q -p no:randomly -p no:warnings -ra
  rc=$(( rc + $? ))
fi
if [ "$WHICH" = "pkge" ] || [ "$WHICH" = "both" ]; then
  echo "── pkgE ───────────────────────────────────"
  TEST_DATABASE_URL="$PKGE_DSN" DATABASE_URL="$PKGE_DSN" PYTHONIOENCODING=utf-8 \
    python -m pytest tests/defensive_geo_pkge_2026_08_24 -q -p no:randomly -p no:warnings -ra
  rc=$(( rc + $? ))
fi
exit $rc
