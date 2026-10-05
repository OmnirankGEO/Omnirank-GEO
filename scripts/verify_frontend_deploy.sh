#!/bin/bash
# [CTO-15.23 2026-05-20 P0 救火] 前端部署完整性验证
#
# 同事报:用户 Chrome 一直循环加载 index-XXX.js · 部署不完整时新 HTML 引用 chunk 在 prod 缺失
# 本脚本对比 index.html 引用的 chunk 文件 vs prod /app/frontend/dist/assets/ 实际文件
# 缺失任一 chunk → exit 1 + 列出缺失 · Deploy-CTO 立即回滚或重 sync
#
# 用法(Deploy-CTO SSH 上 prod 跑):
#   docker exec omnirank-blue bash /app/scripts/verify_frontend_deploy.sh
#   docker exec omnirank-green bash /app/scripts/verify_frontend_deploy.sh
#
# 退出码:
#   0 = 所有 chunk 完整
#   1 = 有缺失(列出文件名)
#   2 = index.html 找不到或读取失败

set -u
DIST_DIR="${1:-/app/frontend/dist}"
INDEX_HTML="$DIST_DIR/index.html"

if [[ ! -f "$INDEX_HTML" ]]; then
  echo "❌ index.html not found at $INDEX_HTML"
  exit 2
fi

echo "📋 verify_frontend_deploy · DIST=$DIST_DIR"
echo "  index.html: $(stat -c '%Y' "$INDEX_HTML" 2>/dev/null || stat -f '%m' "$INDEX_HTML") ts"

# 提取 index.html 引用的所有 /assets/ 文件
referenced_chunks=$(grep -oE '/assets/[a-zA-Z0-9_.-]+\.(js|css|woff2?|png|svg|webp|jpg|jpeg)' "$INDEX_HTML" | sort -u)

if [[ -z "$referenced_chunks" ]]; then
  echo "⚠️  index.html 内未发现 /assets/ 引用 · 可能 HTML 损坏 / 异常空"
  exit 2
fi

total=0
missing=0
missing_files=()

while IFS= read -r path; do
  total=$((total + 1))
  full_path="$DIST_DIR$path"
  if [[ ! -f "$full_path" ]]; then
    missing=$((missing + 1))
    missing_files+=("$path")
  fi
done <<< "$referenced_chunks"

echo "  总引用 chunk: $total"
echo "  缺失: $missing"

if [[ $missing -gt 0 ]]; then
  echo ""
  echo "🚨 部署不完整 · 缺失 $missing 个 chunk:"
  for f in "${missing_files[@]}"; do
    echo "  - $f"
  done
  echo ""
  echo "Deploy-CTO 行动:"
  echo "  1. 立刻 rollback 蓝绿(切回上一稳定版)"
  echo "  2. 检查 docker build 是否 frontend dist 完整复制 (Dockerfile multi-stage COPY)"
  echo "  3. 重 build + rsync 完整 dist 后再切流量"
  exit 1
fi

echo "✅ 所有 chunk 完整 · 部署 OK"
exit 0
