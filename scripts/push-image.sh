#!/bin/bash
# ============================================
# 构建 Docker 镜像并导出为 tar 文件
# 用于应急扩容时传输到新 ECS
# ============================================

set -e

IMAGE_NAME="omnirank-ai"
TAG="${1:-latest}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
OUTPUT_DIR="${PROJECT_DIR}/data"
OUTPUT_FILE="${OUTPUT_DIR}/omnirank-ai.tar.gz"

echo "========================================="
echo "  构建并导出 Docker 镜像"
echo "  镜像: $IMAGE_NAME:$TAG"
echo "  输出: $OUTPUT_FILE"
echo "========================================="

# 1. 构建镜像
echo "[1/2] 构建镜像..."
cd "$PROJECT_DIR"
docker build -t "$IMAGE_NAME:$TAG" --build-arg CACHEBUST="$(date +%s)" .

# 2. 导出为 tar.gz
echo "[2/2] 导出镜像..."
docker save "$IMAGE_NAME:$TAG" | gzip > "$OUTPUT_FILE"

SIZE=$(du -h "$OUTPUT_FILE" | cut -f1)
echo "========================================="
echo "  导出完成！"
echo "  文件: $OUTPUT_FILE"
echo "  大小: $SIZE"
echo ""
echo "  每次代码更新部署后重新执行此脚本保持镜像最新"
echo "========================================="
