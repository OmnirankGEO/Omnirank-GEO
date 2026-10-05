#!/usr/bin/env bash
# ============================================================
# M1d · GEO 商业与发布运维护栏 smoke
#
# 作者: CTO-15.9 session 3 · 2026-04-25 · B6
# PRD: docs/PRD/M1d_GEO商业与发布运维护栏_PRD_v1.md
#
# 6 大 Part × 22+ 场景护栏(M2/M3/M4 改造前必跑 + 部署后回归):
#   §6.1 商业层 (9 smoke)  · 自动 1 (flag) + 手动 checklist 8
#   §6.2 发布运维 (5 smoke) · 自动 0 + 手动 5(原自动 3 条打插件端点,WO_273 随插件后端退役)
#   §6.3 监测运维 (16 smoke) · 自动 8 + 手动 8
#   §6.4 admin (9 smoke) · 自动 0(需登录态)+ 手动 9
#   §6.5 托管用户侧 (7 smoke) · 自动 1(/managed 路由)+ 手动 6
#   §6.6 公域 token (8 smoke) · 自动 4(SPA 路由 + endpoint)+ 手动 4
#
# 用法:
#   BASE_URL=https://omnirank.top TEST_TOKEN=<jwt> bash scripts/m1d_guardrail_smoke.sh
#   不带 TEST_TOKEN 时只跑公开端点 + 路由可达性
# ============================================================

set -euo pipefail

BASE_URL="${BASE_URL:-https://omnirank.top}"
TEST_TOKEN="${TEST_TOKEN:-}"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

PASS=0
FAIL=0
SKIP=0
TOTAL=0

echo ""
echo "============================================================"
echo "M1d 商业 + 发布 + 监测 + admin + 托管 + 公域护栏 smoke"
echo "BASE_URL=$BASE_URL"
echo "TOKEN: $([ -n "$TEST_TOKEN" ] && echo 'YES' || echo 'NO (公开 + 路由可达性 only)')"
echo "============================================================"
echo ""

_pass() { echo "  [PASS] $1"; PASS=$((PASS + 1)); TOTAL=$((TOTAL + 1)); }
_fail() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); TOTAL=$((TOTAL + 1)); }
_skip() { echo "  [SKIP] $1"; SKIP=$((SKIP + 1)); TOTAL=$((TOTAL + 1)); }

_curl_open() {
    local url="$1"
    local method="${2:-GET}"
    local body="${3:-}"
    if [ -n "$body" ]; then
        curl -sS -o /tmp/m1d_body.txt -w "%{http_code}" -X "$method" \
             -H "Content-Type: application/json" \
             -d "$body" "$url"
    else
        curl -sS -o /tmp/m1d_body.txt -w "%{http_code}" -X "$method" "$url"
    fi
}

_curl_auth() {
    local url="$1"
    local method="${2:-GET}"
    local body="${3:-}"
    if [ -z "$TEST_TOKEN" ]; then
        echo "401"
        return
    fi
    if [ -n "$body" ]; then
        curl -sS -o /tmp/m1d_body.txt -w "%{http_code}" -X "$method" \
             -H "Authorization: Bearer $TEST_TOKEN" \
             -H "Content-Type: application/json" \
             -d "$body" "$url"
    else
        curl -sS -o /tmp/m1d_body.txt -w "%{http_code}" -X "$method" \
             -H "Authorization: Bearer $TEST_TOKEN" "$url"
    fi
}

# ============================================================
# Part §6.1 · 商业层
# ============================================================
echo "Part §6.1 · 商业层 (9 smoke · 自动 1 + 手动 8)"
echo "------------------------------------------------------------"

# 1.1 partner_apply flag(公开 endpoint)
HTTP=$(_curl_open "$BASE_URL/api/partner/flag")
if [ "$HTTP" = "200" ] && grep -q '"enabled"' /tmp/m1d_body.txt 2>/dev/null; then
    _pass "§6.1.5 partner/flag 返 200 + enabled 字段"
else
    _fail "§6.1.5 partner/flag 返 $HTTP · 缺 enabled"
fi

cat <<'CL61'

  [手动 checklist · 需 SQL/admin 后台]
  [ ] 1.1 代理 v3.2 佣金 T+0 入账(SQL: pending_commissions 表)
  [ ] 1.2 代理 v3.2 佣金 T+3 结算
  [ ] 1.3 代理反作弊 frozen_reason='flag_only_*' 标记
  [ ] 1.4 普通用户 15% bonus 即时入账(referral_bonus_records)
  [ ] 1.6 试用券 trial_pass_records 写入
  [ ] 1.7 白标报价 PDF · 无 OmniRank 水印
  [ ] 1.8 提现审批流转 pending → approved
  [ ] 1.9 钱包充值 user_wallets.paid_points + 13000(¥1=130 积分)
  [ ] 1.10 退款未消耗 × 95% 原路退

CL61

# ============================================================
# Part §6.2 · 发布运维
# ============================================================
echo "Part §6.2 · 发布运维 (5 smoke · 自动 0 + 手动 5)"
echo "------------------------------------------------------------"

# [WO_273 · 2026-09-23] 原 2.4 / 2.5 / 2.6 三条自动项打的是浏览器插件端点(已登录账号列表 /
#   插件发布记录 / 24h 发布计数)。插件后端整体退役,这三个端点已删除 —— 三条同步退役。
#   「退役的端点不许回来」由代码层的路由缺席锁守(tests/extension_retirement_2026_09_23);
#   上线后的实况核验在 Review 的验收步骤里。

cat <<'CL62'

  [手动 checklist · 需测试 quote + 媒介盒子]
  [ ] 2.1 自动发布 placement_service 调度正确媒体
  [ ] 2.2 ManualPublicationForm 写 publication_facts + media_publications
  [ ] 2.3 媒介盒子下 3 单 · publish_order_items 3 行 pending
  [ ] 2.7 admin 撤单 · 钱包 balance 回加 + pending_commissions refunded_cancelled
  [ ] 2.8 客户投诉退款流程

CL62

# ============================================================
# Part §6.3 · 监测运维
# ============================================================
echo "Part §6.3 · 监测运维 (16 smoke · 自动 8 + 手动 8)"
echo "------------------------------------------------------------"

# 3.1 立即触发监测(POST · 需 token)
HTTP=$(_curl_auth "$BASE_URL/api/monitoring/run" "POST" '{"keywords": ["test"]}')
if [ "$HTTP" = "200" ] || [ "$HTTP" = "202" ]; then
    _pass "§6.3.1 /api/monitoring/run POST 返 $HTTP"
elif [ "$HTTP" = "401" ] && [ -z "$TEST_TOKEN" ]; then
    _skip "§6.3.1 /api/monitoring/run · 无 TEST_TOKEN"
elif [ "$HTTP" = "422" ] || [ "$HTTP" = "400" ]; then
    _pass "§6.3.1 /api/monitoring/run 返 $HTTP(参数 schema 校验生效 · endpoint 存在)"
else
    _fail "§6.3.1 /api/monitoring/run 返 $HTTP"
fi

# 3.2 schedule GET
HTTP=$(_curl_auth "$BASE_URL/api/monitoring/schedule")
if [ "$HTTP" = "200" ]; then
    _pass "§6.3.2 /api/monitoring/schedule GET 返 200"
elif [ "$HTTP" = "401" ] && [ -z "$TEST_TOKEN" ]; then
    _skip "§6.3.2 /api/monitoring/schedule · 无 TEST_TOKEN"
else
    _fail "§6.3.2 /api/monitoring/schedule 返 $HTTP"
fi

# 3.6 可回滚列表
HTTP=$(_curl_auth "$BASE_URL/api/monitoring/rollback/tasks")
if [ "$HTTP" = "200" ]; then
    _pass "§6.3.6 /api/monitoring/rollback/tasks 返 200"
elif [ "$HTTP" = "401" ] && [ -z "$TEST_TOKEN" ]; then
    _skip "§6.3.6 · 无 TEST_TOKEN"
else
    _fail "§6.3.6 /api/monitoring/rollback/tasks 返 $HTTP"
fi

# 3.9 归档列表
HTTP=$(_curl_auth "$BASE_URL/api/monitoring/archives")
if [ "$HTTP" = "200" ]; then
    _pass "§6.3.9 /api/monitoring/archives 返 200"
elif [ "$HTTP" = "401" ] && [ -z "$TEST_TOKEN" ]; then
    _skip "§6.3.9 · 无 TEST_TOKEN"
else
    _fail "§6.3.9 /api/monitoring/archives 返 $HTTP"
fi

# 3.10 可用平台
HTTP=$(_curl_auth "$BASE_URL/api/monitoring/platforms")
if [ "$HTTP" = "200" ]; then
    _pass "§6.3.10 /api/monitoring/platforms 返 200"
elif [ "$HTTP" = "401" ] && [ -z "$TEST_TOKEN" ]; then
    _skip "§6.3.10 · 无 TEST_TOKEN"
else
    _fail "§6.3.10 /api/monitoring/platforms 返 $HTTP"
fi

# 3.11 权重读
HTTP=$(_curl_auth "$BASE_URL/api/monitoring/platform-weights")
if [ "$HTTP" = "200" ]; then
    _pass "§6.3.11 /api/monitoring/platform-weights GET 返 200"
elif [ "$HTTP" = "401" ] && [ -z "$TEST_TOKEN" ]; then
    _skip "§6.3.11 · 无 TEST_TOKEN"
else
    _fail "§6.3.11 /api/monitoring/platform-weights 返 $HTTP"
fi

# 3.13 权重搜索
HTTP=$(_curl_auth "$BASE_URL/api/monitoring/platform-weights/search" "POST" '{"query": "test"}')
if [ "$HTTP" = "200" ] || [ "$HTTP" = "422" ]; then
    _pass "§6.3.13 /api/monitoring/platform-weights/search 返 $HTTP(endpoint 存在)"
elif [ "$HTTP" = "401" ] && [ -z "$TEST_TOKEN" ]; then
    _skip "§6.3.13 · 无 TEST_TOKEN"
else
    _fail "§6.3.13 /api/monitoring/platform-weights/search 返 $HTTP"
fi

# 3.14 客户监测配置(读)· 用 quote_id=1 探活
HTTP=$(_curl_auth "$BASE_URL/api/monitoring/client/1/monitoring-config")
if [ "$HTTP" = "200" ] || [ "$HTTP" = "404" ] || [ "$HTTP" = "403" ]; then
    _pass "§6.3.14 /api/monitoring/client/{id}/monitoring-config 返 $HTTP(endpoint 存在)"
elif [ "$HTTP" = "401" ] && [ -z "$TEST_TOKEN" ]; then
    _skip "§6.3.14 · 无 TEST_TOKEN"
else
    _fail "§6.3.14 /api/monitoring/client/{id}/monitoring-config 返 $HTTP"
fi

cat <<'CL63'

  [手动 checklist · 需触发真任务]
  [ ] 3.3 schedule/run-now POST 立即触发定时任务
  [ ] 3.4 schedule/stop POST 停定时
  [ ] 3.5 SSE run-stream · 4 引擎逐一返回
  [ ] 3.7 单任务回滚 rollback/{task_id}
  [ ] 3.8 批量回滚 rollback-batch
  [ ] 3.12 权重改 PUT · admin 操作
  [ ] 3.15 客户监测配置(改)POST
  [ ] 3.16 服务配置 service-config POST

CL63

# ============================================================
# Part §6.4 · admin
# ============================================================
echo "Part §6.4 · admin (9 smoke · 全手动 · 需登录)"
echo "------------------------------------------------------------"

cat <<'CL64'

  [手动 checklist · 全部需 admin 身份登录]
  [ ] 4.1 /admin/orders 加载 20 单列表
  [ ] 4.2 /admin/users 改非 admin 角色成功
  [ ] 4.3 非 admin 越权 /admin/* → 403
  [ ] 4.4 /admin/audit 看 24h 动作日志
  [ ] 4.5 /admin/partner-applications 审核状态流转
  [ ] 4.6 /admin/industry-corrections 触发矫正写入
  [ ] 4.7 /admin/withdrawals 批一笔提现 · approved
  [ ] 4.8 /admin/managed 批托管审核 · approved
  [ ] 4.9 /tv 大屏(非 admin 返 403)

CL64

# ============================================================
# Part §6.5 · 全自动托管用户侧
# ============================================================
echo "Part §6.5 · 全自动托管用户侧 (7 smoke · 自动 1 + 手动 6)"
echo "------------------------------------------------------------"

# 5.1 /managed 路由 SPA 可达
HTTP=$(_curl_open "$BASE_URL/managed")
if [ "$HTTP" = "200" ]; then
    _pass "§6.5.1 /managed SPA 路由 200"
else
    _fail "§6.5.1 /managed 返 $HTTP"
fi

cat <<'CL65'

  [手动 checklist · 需代理身份 + 已签约托管]
  [ ] 5.2 /managed/:id 详情 · "待审 (N)" 按钮可点+滚锚点
  [ ] 5.3 /managed/brand/:brandId · 5 引擎状态显示
  [ ] 5.4 /material-center/:profileId · 12 项物料 + 呼吸灯
  [ ] 5.5 /managed/:id 调方案 · adjust + confirm-adjust 状态/扣费正确
  [ ] 5.6 托管退款 · 提示充值池模式不退款
  [ ] 5.7 暂停/恢复 pause/resume/confirm-resume 状态正确

CL65

# ============================================================
# Part §6.6 · 公域 token 链路
# ============================================================
echo "Part §6.6 · 公域 token 链路 (8 smoke · 自动 4 + 手动 4)"
echo "------------------------------------------------------------"

# 6.1 /q/:code SPA 路由
HTTP=$(_curl_open "$BASE_URL/q/test_code")
if [ "$HTTP" = "200" ]; then
    _pass "§6.6.1 /q/:code SPA 路由 200"
else
    _fail "§6.6.1 /q/:code 返 $HTTP"
fi

# 6.2 /s/:token SPA 路由
HTTP=$(_curl_open "$BASE_URL/s/test_token")
if [ "$HTTP" = "200" ]; then
    _pass "§6.6.2 /s/:token SPA 路由 200"
else
    _fail "§6.6.2 /s/:token 返 $HTTP"
fi

# 6.5 /portal SPA 路由
HTTP=$(_curl_open "$BASE_URL/portal")
if [ "$HTTP" = "200" ]; then
    _pass "§6.6.5 /portal SPA 路由 200"
else
    _fail "§6.6.5 /portal 返 $HTTP"
fi

# 6.7 /api/portal/verify (POST · 公开端点 · 用空 token 测探活)
HTTP=$(_curl_open "$BASE_URL/api/portal/verify" "POST" '{"token": "INVALID"}')
if [ "$HTTP" = "200" ]; then
    if grep -q '"valid":\s*false' /tmp/m1d_body.txt 2>/dev/null; then
        _pass "§6.6.7 /api/portal/verify 返 200 + valid:false(endpoint 存在 · 校验生效)"
    else
        _pass "§6.6.7 /api/portal/verify 返 200(endpoint 存在)"
    fi
else
    _fail "§6.6.7 /api/portal/verify 返 $HTTP"
fi

cat <<'CL66'

  [手动 checklist · 需真实 token]
  [ ] 6.3 /m/:token 素材确认 · 12 物料显示
  [ ] 6.4 /public/report/:id 公开报告 · read-only 无操作按钮
  [ ] 6.6 token 过期 · 友好页 / 不存在 404
  [ ] 6.8 移动端布局响应式 · 不出横向滚动

CL66

# ============================================================
# 汇总
# ============================================================

echo ""
echo "============================================================"
echo "M1d smoke 自动化部分汇总"
echo "PASS: $PASS"
echo "FAIL: $FAIL"
echo "SKIP: $SKIP (无 TEST_TOKEN 跳过)"
echo "TOTAL automated: $TOTAL"
echo "============================================================"
echo "覆盖 PRD §6 全 6 Part(57 场景):"
echo "  §6.1 商业层 9 · 自动 1 + 手动 8"
echo "  §6.2 发布运维 5 · 自动 0 + 手动 5"
echo "  §6.3 监测运维 16 · 自动 8 + 手动 8"
echo "  §6.4 admin 9 · 全手动"
echo "  §6.5 托管用户侧 7 · 自动 1 + 手动 6"
echo "  §6.6 公域 token 8 · 自动 4 + 手动 4"
echo "============================================================"

if [ "$FAIL" -gt 0 ]; then
    echo "❌ M1d 自动化部分失败 $FAIL 项"
    echo "   可能 endpoint 已变 · git blame api/monitoring_api.py 找最近改动"
    exit 1
fi

echo "✅ M1d 自动化部分全绿(PASS=$PASS · SKIP=$SKIP)"
echo "下一步 · 按 6 part 手动 checklist 跑一轮 · 通过后才能启动 M2/M3/M4 改造"
exit 0
