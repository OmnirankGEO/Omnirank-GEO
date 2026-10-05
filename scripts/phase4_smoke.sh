#!/usr/bin/env bash
# ============================================================
# Phase 4 · C 端 GEO 方案异步任务重构 · Deploy-CTO 部署后 smoke 测试
#
# 作者: CTO-15.5 · 2026-04-20
# PRD: .planning/phases/04-c-geo/PRD.md Section 7 (10 项验收)
#
# 5 步一键验证:
#   1. scheduler.status total_count >= 19 (原 17 + zombie_killer + archiver)
#   2. /api/geo-plan/full-plan 返 410 Gone
#   3. /api/geo-plan/industry-l1l2-status?industry=餐饮 返 200 + has_l1
#   4. /api/geo-plan/tasks 返 200 + 数组
#   5. DB 层 geo_plan_tasks 表存在 + feature_pricing.geo_plan_l1l2_fallback=130
#
# 任一步失败 exit 1 (让 Deploy-CTO 回滚)
#
# 使用:
#   BASE_URL=https://omnirank.top TEST_TOKEN=xxx bash scripts/phase4_smoke.sh
#   # 或本地:
#   BASE_URL=http://localhost:8000 TEST_TOKEN=xxx bash scripts/phase4_smoke.sh
# ============================================================

set -euo pipefail

BASE_URL="${BASE_URL:-https://omnirank.top}"
TEST_TOKEN="${TEST_TOKEN:-}"   # Deploy-CTO 需先登录 laowang 账号拿 token
DB_CONTAINER="${DB_CONTAINER:-omnirank-db}"
DB_USER="${DB_USER:-geo_admin}"
DB_NAME="${DB_NAME:-geo_agentscope}"

PASS=0
FAIL=0

echo "==================== Phase 4 Smoke Test ===================="
echo "BASE_URL: $BASE_URL"
echo "DB_CONTAINER: $DB_CONTAINER"
echo "================================================================"
echo ""

# ------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------

_pass() {
    echo "  [PASS] $1"
    PASS=$((PASS + 1))
}

_fail() {
    echo "  [FAIL] $1"
    FAIL=$((FAIL + 1))
}

_curl_with_token() {
    local url="$1"
    local method="${2:-GET}"
    if [ -z "$TEST_TOKEN" ]; then
        curl -sS -o /tmp/smoke_body.json -w "%{http_code}" -X "$method" "$url"
    else
        curl -sS -o /tmp/smoke_body.json -w "%{http_code}" -X "$method" \
             -H "Authorization: Bearer $TEST_TOKEN" \
             "$url"
    fi
}

# ------------------------------------------------------------
# Step 1: scheduler.status total_count >= 19
# ------------------------------------------------------------

echo "Step 1/5 · scheduler jobs (total >= 19)..."
HTTP=$(_curl_with_token "$BASE_URL/api/scheduler/status")
if [ "$HTTP" = "200" ]; then
    # 不强依赖 jq, 用 grep + 粗略计数
    COUNT=$(grep -oE '"total_count":\s*[0-9]+' /tmp/smoke_body.json | head -1 | grep -oE '[0-9]+' || echo "0")
    if [ -z "$COUNT" ]; then
        # 用 jobs 数组长度兜底
        COUNT=$(grep -oE '"id":\s*"[^"]*"' /tmp/smoke_body.json | wc -l | tr -d ' ')
    fi
    if [ "$COUNT" -ge 19 ]; then
        _pass "scheduler total=$COUNT (>= 19)"
    else
        _fail "scheduler total=$COUNT (< 19)  · 预期含 geo_plan_task_zombie_killer + geo_plan_task_cleanup_archiver"
    fi
else
    _fail "/api/scheduler/status 返 $HTTP (预期 200)"
fi

# ------------------------------------------------------------
# Step 2: /api/geo-plan/full-plan 返 410 Gone (PLAN 03)
# ------------------------------------------------------------

echo "Step 2/5 · 旧 /api/geo-plan/full-plan 返 410 Gone..."
HTTP=$(_curl_with_token "$BASE_URL/api/geo-plan/full-plan" "POST")
if [ "$HTTP" = "410" ]; then
    # 应含 migrate_to 提示
    if grep -q "migrate_to" /tmp/smoke_body.json 2>/dev/null; then
        _pass "/api/geo-plan/full-plan 返 410 + migrate_to 指路"
    else
        _pass "/api/geo-plan/full-plan 返 410 (无 migrate_to, 但 410 本身符合 Q3 拍板)"
    fi
elif [ "$HTTP" = "422" ]; then
    # 缺 body 校验失败也能证明端点未被彻底删除, 但应该 410
    _fail "/api/geo-plan/full-plan 返 422 (body schema) · 应 410 short-circuit"
else
    _fail "/api/geo-plan/full-plan 返 $HTTP (预期 410)"
fi

# ------------------------------------------------------------
# Step 3: /api/geo-plan/industry-l1l2-status 返 200 + has_l1
# ------------------------------------------------------------

echo "Step 3/5 · /api/geo-plan/industry-l1l2-status?industry=餐饮..."
INDUSTRY_ENC="%E9%A4%90%E9%A5%AE"  # URL encoded 餐饮
HTTP=$(_curl_with_token "$BASE_URL/api/geo-plan/industry-l1l2-status?industry=$INDUSTRY_ENC")
if [ "$HTTP" = "200" ]; then
    if grep -q '"has_l1"' /tmp/smoke_body.json 2>/dev/null; then
        _pass "industry-l1l2-status 返 200 + has_l1 字段"
    else
        _fail "industry-l1l2-status 返 200 但缺 has_l1 字段"
    fi
elif [ "$HTTP" = "401" ] || [ "$HTTP" = "403" ]; then
    _fail "industry-l1l2-status 返 $HTTP · TEST_TOKEN 未设或失效"
else
    _fail "industry-l1l2-status 返 $HTTP (预期 200)"
fi

# ------------------------------------------------------------
# Step 4: /api/geo-plan/tasks 返 200 + tasks 数组
# ------------------------------------------------------------

echo "Step 4/5 · /api/geo-plan/tasks..."
HTTP=$(_curl_with_token "$BASE_URL/api/geo-plan/tasks?limit=5")
if [ "$HTTP" = "200" ]; then
    if grep -q '"tasks"' /tmp/smoke_body.json 2>/dev/null; then
        _pass "/api/geo-plan/tasks 返 200 + tasks 字段"
    else
        _fail "/api/geo-plan/tasks 返 200 但 payload 格式异常"
    fi
elif [ "$HTTP" = "401" ] || [ "$HTTP" = "403" ]; then
    _fail "/api/geo-plan/tasks 返 $HTTP · TEST_TOKEN 未设或失效"
else
    _fail "/api/geo-plan/tasks 返 $HTTP (预期 200)"
fi

# ------------------------------------------------------------
# Step 5: DB 层 geo_plan_tasks 表 + feature_pricing 价目表
# ------------------------------------------------------------

echo "Step 5/5 · DB 层 geo_plan_tasks 表 + feature_pricing.geo_plan_l1l2_fallback..."

if command -v docker >/dev/null 2>&1; then
    # 5a · 表存在
    set +e
    TABLE_EXISTS=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAc \
        "SELECT 1 FROM information_schema.tables WHERE table_name='geo_plan_tasks'" 2>/dev/null | tr -d ' \n' || echo "")
    set -e

    if [ "$TABLE_EXISTS" = "1" ]; then
        _pass "geo_plan_tasks 表存在"
    else
        _fail "geo_plan_tasks 表不存在 · PLAN 01 migration SQL 未跑?"
    fi

    # 5b · feature_pricing 条目
    set +e
    PRICING=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAc \
        "SELECT cost_points FROM feature_pricing WHERE feature_code='geo_plan_l1l2_fallback' LIMIT 1" 2>/dev/null | tr -d ' \n' || echo "")
    set -e

    if [ "$PRICING" = "130" ]; then
        _pass "feature_pricing.geo_plan_l1l2_fallback = 130"
    elif [ -z "$PRICING" ]; then
        _fail "feature_pricing 缺 geo_plan_l1l2_fallback · PLAN 01 seed SQL 未跑?"
    else
        _fail "feature_pricing.geo_plan_l1l2_fallback = $PRICING (预期 130)"
    fi

    # 5c · 4 关键索引 (软检查, fail 不 fatal)
    set +e
    IDX_COUNT=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAc \
        "SELECT count(*) FROM pg_indexes WHERE tablename='geo_plan_tasks'" 2>/dev/null | tr -d ' \n' || echo "0")
    set -e
    if [ "$IDX_COUNT" -ge 4 ]; then
        _pass "geo_plan_tasks 索引数=$IDX_COUNT (>= 4)"
    else
        echo "  [WARN] geo_plan_tasks 索引数=$IDX_COUNT (< 4) · 查询性能可能有问题"
    fi
else
    echo "  [SKIP] docker 不可用 · Step 5 跳过 · Deploy-CTO 人工验证"
fi

# ------------------------------------------------------------
# 汇总
# ------------------------------------------------------------

echo ""
echo "==================== Smoke Result ===================="
echo "PASS: $PASS"
echo "FAIL: $FAIL"
echo "======================================================="

if [ "$FAIL" -gt 0 ]; then
    echo "❌ smoke 失败 $FAIL 项 · 建议 Deploy-CTO 触发回滚"
    exit 1
fi

echo "✅ smoke 全部通过 · 可进入 PRD Section 7 老板 10 项人工验收"
exit 0
