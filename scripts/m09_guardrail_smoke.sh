#!/usr/bin/env bash
# ============================================================
# M0.9 · C 端 GEO 入口与异步任务护栏 smoke
#
# 作者: CTO-15.9 session 3 · 2026-04-25 · B.3
# PRD: docs/PRD/M0.9_C端GEO入口与异步任务兼容计划_PRD_v1.md
#
# 8 场景护栏(M3 改造前必跑):
#   API 5 (来自 PRD §6.1) + C 端入口 3 + 前端清单(打印不执行)
#
# 复用:phase4_smoke.sh 已覆盖 API 5 步 · 本脚本先调它再补 3 项
#
# 任一 API 步失败 exit 1 · 让 Deploy-CTO/CTO-16 回滚
#
# 用法:
#   BASE_URL=https://omnirank.top TEST_TOKEN=<token> bash scripts/m09_guardrail_smoke.sh
# ============================================================

set -euo pipefail

BASE_URL="${BASE_URL:-https://omnirank.top}"
TEST_TOKEN="${TEST_TOKEN:-}"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

PASS=0
FAIL=0

echo ""
echo "============================================================"
echo "M0.9 GEO C 端护栏 smoke (8 场景)"
echo "BASE_URL=$BASE_URL"
echo "============================================================"
echo ""

_pass() { echo "  [PASS] $1"; PASS=$((PASS + 1)); }
_fail() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }
_skip() { echo "  [SKIP] $1"; }

_curl() {
    local url="$1"
    local method="${2:-GET}"
    if [ -z "$TEST_TOKEN" ]; then
        curl -sS -o /tmp/m09_body.txt -w "%{http_code}" -X "$method" "$url"
    else
        curl -sS -o /tmp/m09_body.txt -w "%{http_code}" -X "$method" \
             -H "Authorization: Bearer $TEST_TOKEN" "$url"
    fi
}

# ============================================================
# Part A · API smoke 5 步(委托 phase4_smoke.sh)
# ============================================================

echo "Part A · API smoke 5 步(/api/geo-plan/*)"
echo "------------------------------------------------------------"
if [ -f "$SCRIPT_DIR/phase4_smoke.sh" ]; then
    set +e
    BASE_URL="$BASE_URL" TEST_TOKEN="$TEST_TOKEN" bash "$SCRIPT_DIR/phase4_smoke.sh"
    P4_STATUS=$?
    set -e
    if [ "$P4_STATUS" = "0" ]; then
        _pass "Part A · phase4_smoke.sh 5 步全绿"
    else
        _fail "Part A · phase4_smoke.sh 失败(exit=$P4_STATUS)"
    fi
else
    _skip "Part A · phase4_smoke.sh 不存在 · 跳过"
fi

echo ""
echo "Part B · M0.9 C 端入口 3 项"
echo "------------------------------------------------------------"

# ============================================================
# Part B · M0.9 特有 3 项 C 端入口
# ============================================================

# B.1 · /c/chat 服务可达(SPA index 200)
echo "B.1/3 · /c/chat 路由前端 SPA 可达..."
HTTP=$(_curl "$BASE_URL/c/chat")
if [ "$HTTP" = "200" ]; then
    if grep -qi "<!DOCTYPE html>\|<html" /tmp/m09_body.txt 2>/dev/null; then
        _pass "/c/chat 返 200 + SPA HTML(纯路由 200 即可 · 实际渲染需浏览器)"
    else
        _fail "/c/chat 返 200 但 body 非 HTML · 静态服务异常?"
    fi
else
    _fail "/c/chat 返 $HTTP (预期 200 由 SPA fallback 接管)"
fi

# B.2 · /c/geo-plan 路由 SPA 可达
echo "B.2/3 · /c/geo-plan 路由 SPA 可达..."
HTTP=$(_curl "$BASE_URL/c/geo-plan")
if [ "$HTTP" = "200" ]; then
    _pass "/c/geo-plan 返 200(SPA fallback)"
else
    _fail "/c/geo-plan 返 $HTTP (预期 200)"
fi

# B.3 · /c/history 路由 SPA 可达
echo "B.3/3 · /c/history 路由 SPA 可达..."
HTTP=$(_curl "$BASE_URL/c/history")
if [ "$HTTP" = "200" ]; then
    _pass "/c/history 返 200(SPA fallback)"
else
    _fail "/c/history 返 $HTTP (预期 200)"
fi

# ============================================================
# Part C · 前端 6 场景人工 checklist(M3 开工前必跑)
# ============================================================

cat <<'CHECKLIST'

============================================================
Part C · 前端 6 场景手动 smoke(M3 开工前必跑 · 截图存档)
============================================================

请在浏览器(无痕窗口)按顺序执行 · 任一项失败立即停 · 走 PRD §7 回滚:

  [ ] 1. 浏览器开 /c/chat
        预期:200 + 对话页渲染 + 输入框可用 + 无 console error

  [ ] 2. 输入"帮我做品牌体检" → 发送
        预期:Agent 返 confirm_card 或直接进 GEO 诊断流程
        (不应被 MISSING_INDUSTRY 阻断 · 元指令 17 永远不中断对话)

  [ ] 3. 打开 /c/geo-plan → 点"开始生成"
        预期:立即 toast "任务已创建" + Panel 自动弹出

  [ ] 4. 关闭 Panel(× 按钮 · 不点取消)
        预期:右下出现常驻 Bubble · Bubble 可点重开 Panel

  [ ] 5. 任务完成
        预期:NotificationBell 抖动 1 次 · 通知数 +1 · 点 Bell 列表显示新通知

  [ ] 6. 进入 /c/history
        预期:列表第 1 行是刚跑完的 task · status='completed'

完成后 · 把 6 张截图打包附在 M3 开工 ticket 上。
若任一项 FAIL · 不要继续 M3 改造 · 走回滚或 hotfix。

CHECKLIST

# ============================================================
# 汇总
# ============================================================

echo ""
echo "============================================================"
echo "M0.9 smoke 自动化部分汇总"
echo "PASS: $PASS"
echo "FAIL: $FAIL"
echo "============================================================"

if [ "$FAIL" -gt 0 ]; then
    echo "❌ M0.9 自动化部分失败 $FAIL 项 · 不要进行 M3 改造"
    exit 1
fi

echo "✅ M0.9 API + C 端入口 smoke 全绿"
echo "下一步 · 人工跑 Part C 6 场景 · 通过后再启动 M3 前端改造"
exit 0
