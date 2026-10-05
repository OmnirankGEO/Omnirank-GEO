#!/bin/bash
# ============================================
# 前端构建 + 上传到阿里云 OSS
# 前提：已安装 ossutil 并配置 AccessKey
# ============================================

set -e

# 配置（从 .env 读取或使用默认值）
OSS_BUCKET="${OSS_BUCKET:-omnirank}"
OSS_PREFIX="${OSS_PREFIX:-frontend}"
OSS_ENDPOINT="${OSS_ENDPOINT:-oss-cn-shenzhen.aliyuncs.com}"
CDN_URL="${VITE_CDN_URL:-}"

if [ -z "$CDN_URL" ]; then
    echo "错误: 请设置 VITE_CDN_URL 环境变量（如 https://cdn.yourdomain.com/）"
    exit 1
fi

echo "========================================="
echo "  前端构建 + CDN 部署"
echo "  CDN: $CDN_URL"
echo "  OSS: oss://$OSS_BUCKET/$OSS_PREFIX/"
echo "========================================="

# 1. 构建前端
echo "[1/3] 构建前端..."
cd "$(dirname "$0")/../frontend"
VITE_CDN_URL="$CDN_URL" npm run build

# 2. 上传到 OSS
echo "[2/3] 上传到 OSS..."
ossutil cp -r dist/ "oss://$OSS_BUCKET/$OSS_PREFIX/" \
    --endpoint "$OSS_ENDPOINT" \
    --update \
    --jobs 10

# 3. 设置缓存头
echo "[3/3] 设置缓存策略..."
# index.html: no-cache
ossutil set-meta "oss://$OSS_BUCKET/$OSS_PREFIX/index.html" \
    Cache-Control:no-cache,no-store,must-revalidate \
    --endpoint "$OSS_ENDPOINT" \
    --update

# assets/: 长缓存
ossutil set-meta "oss://$OSS_BUCKET/$OSS_PREFIX/assets/" \
    Cache-Control:public,max-age=2592000,immutable \
    --endpoint "$OSS_ENDPOINT" \
    --recursive \
    --update

echo "========================================="
echo "  部署完成！"
echo "  访问: $CDN_URL"
echo "========================================="
