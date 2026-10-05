#!/usr/bin/env bash
# [v4 req4] 完整 throwaway PG bootstrap · 一键起测试库并跑 NO-GO 回归。
# schema 由 tests/regression/conftest.py 的 session fixture 幂等填齐(users 必填列 + 当前 schema),
# 本脚本只负责起容器 + 设环境 + 跑测试。用法:bash scripts/test_bootstrap_throwaway_pg.sh
set -euo pipefail
NAME=${PG_NAME:-geofix2-test-pg}
PORT=${PG_PORT:-5434}
DBURL="postgresql://geo_admin:test@127.0.0.1:${PORT}/test_geo_agentscope"

echo "=== (re)start throwaway PG :${PORT} ==="
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" -e POSTGRES_PASSWORD=test -e POSTGRES_USER=geo_admin \
  -e POSTGRES_DB=test_geo_agentscope -p "${PORT}:5432" postgres:16 >/dev/null
for i in $(seq 1 30); do
  docker exec "$NAME" pg_isready -U geo_admin -d test_geo_agentscope >/dev/null 2>&1 && break
  sleep 1
done
echo "pg ready"

export TEST_DATABASE_URL="$DBURL"
export DATABASE_URL="$DBURL"
# [v5 req5] 破坏性测试(清表/DROP throwaway 库)显式授权 · 仅本机 test/throwaway 库(见 tests/regression/_dbsafe.py 二次校验)
export ALLOW_DESTRUCTIVE_TEST_DB=1
echo "=== run NO-GO regression + security + 【现役 GEO task/worker 测试】(v8:不再只跑精选) ==="
# [v8 · Deploy-CTO NO-GO] 老板订正:只跑 regression/security 会漏掉现役 test_geo_plan_* 的陈旧断言 →
#   "后端全套全绿"不成立。故一并跑现役 GEO 链测试(资金/状态机随 v5-v8 演进,断言必须同步)。
python -m pytest tests/regression tests/security \
  tests/test_geo_plan_tasks_db.py -q

echo "=== run 双价目表 SSOT 测试(独立 session:其 conftest DROP 重建 recharge_orders 等共享表,不能与上批同跑) ==="
# [v8 reconciliation] geofix 已含双价目表代码(17 文件与 27fd4032 逐字节相同 + 6 文件超集)· 跑其自带测试证实。
python -m pytest tests/pricing_ssot -q
