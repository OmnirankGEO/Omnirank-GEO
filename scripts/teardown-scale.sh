#!/bin/bash
# ============================================
# 释放临时扩容 ECS 实例
# ============================================

set -e

INSTANCE_ID="${1:?用法: $0 <实例ID>}"

echo "========================================="
echo "  释放扩容实例: $INSTANCE_ID"
echo "========================================="

# 1. 停止实例
echo "[1/2] 停止实例..."
aliyun ecs StopInstance --InstanceId "$INSTANCE_ID" --ForceStop true
sleep 10

# 2. 释放实例
echo "[2/2] 释放实例..."
aliyun ecs DeleteInstance --InstanceId "$INSTANCE_ID" --Force true

echo "========================================="
echo "  实例 $INSTANCE_ID 已释放"
echo "  别忘了："
echo "  1. 从 DNS 或 Nginx upstream 中移除该实例 IP"
echo "  2. 从主服务器防火墙中移除该 IP 的白名单"
echo "========================================="
