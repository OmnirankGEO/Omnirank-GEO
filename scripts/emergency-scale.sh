#!/bin/bash
# ============================================
# 应急扩容：一键创建按量付费 ECS 并部署 OmniRank
# 前提：已安装 aliyun-cli 并配置 AccessKey
# 镜像通过 scp 传输（无需 ACR）
# ============================================

set -e

# ---- 配置 ----
REGION="cn-shenzhen"
ZONE="cn-shenzhen-d"
INSTANCE_TYPE="${SCALE_INSTANCE_TYPE:-ecs.c6.xlarge}"  # 4核8GB 计算型
IMAGE_ID="${SCALE_IMAGE_ID:-ubuntu_22_04_x64_20G_alibase_20240101.vhd}"
SECURITY_GROUP="${SCALE_SG_ID:?请设置 SCALE_SG_ID 环境变量}"
VSWITCH_ID="${SCALE_VSWITCH_ID:?请设置 SCALE_VSWITCH_ID 环境变量}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
IMAGE_FILE="${PROJECT_DIR}/data/omnirank-ai.tar.gz"
ENV_FILE="${PROJECT_DIR}/.env"

# 主服务器信息（Redis + DB 地址）
MAIN_SERVER_IP="${MAIN_SERVER_IP:?请设置 MAIN_SERVER_IP（主服务器公网IP）}"
DB_URL="postgresql://geo_admin:${POSTGRES_PASSWORD}@${MAIN_SERVER_IP}:5432/geo_agentscope"
REDIS_URL="redis://${MAIN_SERVER_IP}:6379/0"

# 检查镜像文件是否存在
if [ ! -f "$IMAGE_FILE" ]; then
    echo "错误: 镜像文件不存在: $IMAGE_FILE"
    echo "请先执行: bash scripts/push-image.sh"
    exit 1
fi

IMAGE_SIZE=$(du -h "$IMAGE_FILE" | cut -f1)

echo "========================================="
echo "  应急扩容"
echo "  实例规格: $INSTANCE_TYPE"
echo "  地域: $REGION"
echo "  镜像文件: $IMAGE_SIZE"
echo "========================================="

# 1. 创建按量付费 ECS
echo "[1/6] 创建 ECS 实例..."
RESULT=$(aliyun ecs CreateInstance \
    --RegionId "$REGION" \
    --ZoneId "$ZONE" \
    --InstanceType "$INSTANCE_TYPE" \
    --ImageId "$IMAGE_ID" \
    --SecurityGroupId "$SECURITY_GROUP" \
    --VSwitchId "$VSWITCH_ID" \
    --InternetChargeType PayByTraffic \
    --InternetMaxBandwidthOut 100 \
    --InstanceChargeType PostPaid \
    --InstanceName "omnirank-scale-$(date +%Y%m%d%H%M)" \
    --Password "${SCALE_PASSWORD:?请设置 SCALE_PASSWORD}")

INSTANCE_ID=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['InstanceId'])")
echo "  实例ID: $INSTANCE_ID"

# 2. 分配公网 IP 并启动
echo "[2/6] 启动实例..."
aliyun ecs StartInstance --InstanceId "$INSTANCE_ID"
sleep 15

# 获取公网 IP
IP=$(aliyun ecs DescribeInstances --InstanceIds "['$INSTANCE_ID']" \
    | python3 -c "import sys,json; inst=json.load(sys.stdin)['Instances']['Instance'][0]; print(inst.get('PublicIpAddress',{}).get('IpAddress',[''])[0] or inst.get('EipAddress',{}).get('IpAddress',''))")
echo "  公网 IP: $IP"

# 3. 等待 SSH 可用
echo "[3/6] 等待 SSH 就绪..."
for i in $(seq 1 30); do
    if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=3 "root@$IP" "echo ok" 2>/dev/null; then
        break
    fi
    echo "  等待... ($i/30)"
    sleep 5
done

# 4. 安装 Docker
echo "[4/6] 安装 Docker..."
ssh -o StrictHostKeyChecking=no "root@$IP" bash <<'REMOTE'
set -e
curl -fsSL https://get.docker.com | sh
systemctl start docker
systemctl enable docker
echo "Docker 安装完成"
REMOTE

# 5. 传输镜像并加载
echo "[5/6] 传输镜像到新实例（$IMAGE_SIZE，可能需要几分钟）..."
scp -o StrictHostKeyChecking=no "$IMAGE_FILE" "root@$IP:/tmp/omnirank-ai.tar.gz"

# 同时传输 .env 文件（包含所有 API Key）
if [ -f "$ENV_FILE" ]; then
    scp -o StrictHostKeyChecking=no "$ENV_FILE" "root@$IP:/tmp/.env"
fi

ssh -o StrictHostKeyChecking=no "root@$IP" bash <<REMOTE
set -e
# 加载镜像
echo "加载镜像..."
docker load < /tmp/omnirank-ai.tar.gz
rm -f /tmp/omnirank-ai.tar.gz

# 启动容器（连接主服务器的 DB 和 Redis）
docker run -d \
    --name omnirank-ai \
    --restart unless-stopped \
    -p 80:80 \
    --env-file /tmp/.env \
    -e DATABASE_URL="$DB_URL" \
    -e REDIS_URL="$REDIS_URL" \
    -e WORKERS=4 \
    omnirank-ai:latest

echo "容器已启动"
REMOTE

echo "[6/6] 完成！"
echo "========================================="
echo "  实例ID: $INSTANCE_ID"
echo "  公网IP: $IP"
echo ""
echo "  接下来需要手动操作："
echo "  1. 在主服务器防火墙中放行 $IP 对 5432 和 6379 端口的访问"
echo "     → 宝塔面板 → 安全 → 添加规则"
echo "  2. 将 $IP 添加到 DNS 或 Nginx upstream"
echo "========================================="

# 保存实例信息
mkdir -p "$(dirname "$0")/../logs"
echo "$INSTANCE_ID $IP $(date)" >> "$(dirname "$0")/../logs/scale-instances.log"
