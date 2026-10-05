#!/bin/bash
# ============================================
# 蓝绿热回滚/故障切换
#
# 启动 target 前和切流前各做一次资金安全判定：
#   - Phase A 仅在 generation=1、零未结算 gen2、部署证据精确匹配时允许回 legacy；
#   - marker=1、generation=2 或存在未结算 gen2 时，只允许同 image 快照热备。
# 任何事实无法证明或任一 gate 失败，都在修改 Nginx 前退出。
# ============================================
set -euo pipefail

SCRIPT_PATH=$(readlink -f "${BASH_SOURCE[0]}")
PROJECT_DIR=$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd -P)
DEPLOY_SHA="${DEPLOY_SHA:-}"
DEPLOY_RELEASE_WORKTREE="${DEPLOY_RELEASE_WORKTREE:-}"
REVIEWED_ROLLBACK_SCRIPT_SHA256="${REVIEWED_ROLLBACK_SCRIPT_SHA256:-}"
NGINX_CONF="${NGINX_CONF:-/www/server/panel/vhost/nginx/omnirank.top.conf}"
LOCK_FILE="${LOCK_FILE:-/tmp/omnirank-deploy.lock}"
DB_CONTAINER="${DB_CONTAINER:-omnirank-db}"
DB_USER="${DB_USER:-geo_admin}"
DB_NAME="${DB_NAME:-geo_agentscope}"
SNAPSHOT_CAPABILITY="agent-inventory-snapshot-v3"
LEGACY_SNAPSHOT_CAPABILITY="legacy-no-inventory-snapshot-v1"

[[ "$DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]] || {
    echo "❌ rollback 必须显式传入审核 DEPLOY_SHA"
    exit 1
}
[[ "$REVIEWED_ROLLBACK_SCRIPT_SHA256" =~ ^[0-9a-f]{64}$ ]] || {
    echo "❌ rollback 必须显式传入审核脚本 SHA-256"
    exit 1
}
[ -n "$DEPLOY_RELEASE_WORKTREE" ] || {
    echo "❌ rollback 必须从 reviewed release worktree 调用"
    exit 1
}
DEPLOY_RELEASE_WORKTREE=$(cd "$DEPLOY_RELEASE_WORKTREE" && pwd -P)
[ "$PROJECT_DIR" = "$DEPLOY_RELEASE_WORKTREE" ] || {
    echo "❌ rollback 脚本不属于指定 release worktree"
    exit 1
}
[ "$(git -C "$PROJECT_DIR" rev-parse HEAD)" = "$DEPLOY_SHA" ] || {
    echo "❌ rollback release HEAD 不等于 DEPLOY_SHA"
    exit 1
}
[ "$(sha256sum "$SCRIPT_PATH" | awk '{print $1}')" = "$REVIEWED_ROLLBACK_SCRIPT_SHA256" ] || {
    echo "❌ rollback script hash 不匹配审核包"
    exit 1
}
[ -z "$(git -C "$PROJECT_DIR" status --porcelain --untracked-files=all)" ] || {
    echo "❌ rollback release worktree 存在意外改动"
    exit 1
}
[ -z "$(git -C "$PROJECT_DIR" ls-files --others --ignored --exclude-standard)" ] || {
    echo "❌ rollback release worktree 存在被忽略的额外文件"
    exit 1
}

exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    echo "❌ 错误: 另一个部署/回滚正在进行"
    exit 1
fi

cd "$PROJECT_DIR"

if [ ! -f "$NGINX_CONF" ]; then
    echo "错误: Nginx 配置不存在: $NGINX_CONF"
    exit 1
fi

db_scalar() {
    docker exec "$DB_CONTAINER" psql -X -U "$DB_USER" -d "$DB_NAME" -tAqc "$1" 2>/dev/null \
        | tr -d '\r\n '
}

image_snapshot_capability() {
    local image_id="$1"
    local capability
    capability=$(docker run --rm --network none --entrypoint python "$image_id" -c '
from pathlib import Path
p = Path("/app/services/agent_inventory_pricing.py")
if not p.is_file():
    print("legacy-no-inventory-snapshot-v1")
else:
    line = next((v for v in p.read_text(encoding="utf-8").splitlines() if v.startswith("INVENTORY_SNAPSHOT_RUNTIME_CAPABILITY")), "")
    print(line.split("=", 1)[1].strip().strip(chr(34) + chr(39)) if "=" in line else "unknown")
' 2>/dev/null | tr -d '\r\n ')
    case "$capability" in
        "$SNAPSHOT_CAPABILITY"|"$LEGACY_SNAPSHOT_CAPABILITY") printf '%s' "$capability" ;;
        *) return 1 ;;
    esac
}

wait_ready() {
    local container="$1"
    local port="$2"
    local i status
    for i in $(seq 1 30); do
        status=$(docker inspect --format='{{.State.Health.Status}}' "$container" 2>/dev/null || echo "not_found")
        if [ "$status" = "healthy" ] || curl -sf "http://localhost:$port" >/dev/null 2>&1; then
            return 0
        fi
        sleep 5
    done
    return 1
}

verify_cron_leader() {
    docker exec "$1" python -c \
        "import sys; from services.sched_control import verify_is_current_leader; sys.exit(0 if verify_is_current_leader() else 1)" \
        2>/dev/null
}

wait_cron_leader() {
    local container="$1" i
    for i in $(seq 1 24); do
        if verify_cron_leader "$container"; then
            return 0
        fi
        sleep 5
    done
    return 1
}

wait_legacy_scheduler_lock() {
    local i holder
    for i in $(seq 1 24); do
        holder=$(docker exec omnirank-redis redis-cli --raw GET scheduler:lock 2>/dev/null \
            | tr -d '\r\n' || true)
        if [[ "$holder" =~ ^worker-[0-9]+$ ]]; then
            return 0
        fi
        sleep 5
    done
    return 1
}

restore_active_cron() {
    # Phase A legacy target 的 scheduler 嵌在 web 内；恢复 candidate cron 前必须
    # 先停 legacy web 释放旧 scheduler:lock，避免两个版本的定时任务并跑。
    if [ "${LEGACY_PHASE_A_TARGET:-false}" = "true" ]; then
        docker stop --time=20 "omnirank-$ROLLBACK_TO" >/dev/null 2>&1 || true
    fi
    docker stop --time=20 "omnirank-cron-$ROLLBACK_TO" >/dev/null 2>&1 || true
    docker start "omnirank-cron-$ACTIVE" >/dev/null 2>&1 || true
    if ! wait_cron_leader "omnirank-cron-$ACTIVE"; then
        echo "  🔴 原 active cron 未恢复 leader，需立即人工介入"
    fi
}

evaluate_rollback_gate() {
    local phase_value
    ROLLBACK_GATE_OUTPUT="UNPROVEN|rollback facts 尚未完整采集"
    CUTOVER_ACTIVE=$(db_scalar "SELECT COUNT(*) FROM system_settings WHERE key='AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT'")
    WRITER_GENERATION=$(db_scalar "SELECT COALESCE(pg_sequence_last_value(to_regclass('agent_inventory_writer_generation_fence_seq')),0)")
    UNSETTLED_GEN2=$(db_scalar "SELECT COUNT(*) FROM recharge_orders WHERE order_type='agent_inventory_purchase' AND agent_inventory_writer_generation=2 AND payment_status IS DISTINCT FROM 'paid'")
    phase_value=$(db_scalar "SELECT COALESCE(value,'') FROM system_settings WHERE key='AGENT_INVENTORY_SNAPSHOT_PHASE_A_ROLLBACK'")
    PHASE_EVIDENCE_FRESH=$(db_scalar "SELECT COALESCE(MAX(CASE WHEN updated_at >= clock_timestamp()-INTERVAL '30 minutes' AND updated_at <= clock_timestamp() THEN 1 ELSE 0 END),0) FROM system_settings WHERE key='AGENT_INVENTORY_SNAPSHOT_PHASE_A_ROLLBACK'")
    IFS='|' read -r PHASE_LEGACY_IMAGE PHASE_CANDIDATE_IMAGE PHASE_LEGACY_CAPABILITY PHASE_CANDIDATE_CAPABILITY <<< "$phase_value"

    ACTIVE_IMAGE=$(docker inspect --format='{{.Image}}' "omnirank-$ACTIVE")
    TARGET_IMAGE_PRE=$(docker inspect --format='{{.Image}}' "omnirank-$ROLLBACK_TO" 2>/dev/null || true)
    if [ -z "$ACTIVE_IMAGE" ] || [ -z "$TARGET_IMAGE_PRE" ]; then
        ROLLBACK_GATE_OUTPUT="IMAGE_UNPROVEN|active/target image identity 缺失"
        return 1
    fi
    ACTIVE_CAPABILITY=$(image_snapshot_capability "$ACTIVE_IMAGE") || {
        ROLLBACK_GATE_OUTPUT="ACTIVE_CAPABILITY_UNPROVEN|无法从 active image 证明快照能力"
        return 1
    }
    TARGET_CAPABILITY=$(image_snapshot_capability "$TARGET_IMAGE_PRE") || {
        ROLLBACK_GATE_OUTPUT="TARGET_CAPABILITY_UNPROVEN|无法从 target image 证明快照能力"
        return 1
    }

    # 宿主机生产 Python=3.6，不能解析审核模块的 3.7+ 语法。安全判定必须使用
    # 当前 active 审核镜像内的 Python，且禁网、无 volume、无外部副作用。
    if ROLLBACK_GATE_OUTPUT=$(docker run --rm --network none --entrypoint python "$ACTIVE_IMAGE" \
        -m services.agent_inventory_deploy_gate \
        --marker-count "$CUTOVER_ACTIVE" \
        --writer-generation "$WRITER_GENERATION" \
        --unsettled-generation2-orders "$UNSETTLED_GEN2" \
        --active-image "$ACTIVE_IMAGE" \
        --target-image "$TARGET_IMAGE_PRE" \
        --active-capability "$ACTIVE_CAPABILITY" \
        --target-capability "$TARGET_CAPABILITY" \
        --phase-evidence-fresh "$PHASE_EVIDENCE_FRESH" \
        --phase-legacy-image "${PHASE_LEGACY_IMAGE:-}" \
        --phase-candidate-image "${PHASE_CANDIDATE_IMAGE:-}" \
        --phase-legacy-capability "${PHASE_LEGACY_CAPABILITY:-}" \
        --phase-candidate-capability "${PHASE_CANDIDATE_CAPABILITY:-}" 2>&1); then
        return 0
    else
        local gate_status=$?
        return "$gate_status"
    fi
}

# 判断当前活跃实例（通过 proxy_pass 端口判断）
if grep -q "proxy_pass http://127.0.0.1:8001" "$NGINX_CONF"; then
    ACTIVE="blue"
    ROLLBACK_TO="green"
    ACTIVE_PORT=8001
    ROLLBACK_PORT=8002
elif grep -q "proxy_pass http://127.0.0.1:8002" "$NGINX_CONF"; then
    ACTIVE="green"
    ROLLBACK_TO="blue"
    ACTIVE_PORT=8002
    ROLLBACK_PORT=8001
else
    echo "错误: Nginx 配置中未找到 proxy_pass 8001 或 8002"
    exit 1
fi

echo "========================================="
echo "  热切换: $ACTIVE ($ACTIVE_PORT) → $ROLLBACK_TO ($ROLLBACK_PORT)"
echo "========================================="

echo "[1/5] marker / generation / gen2 订单 / capability / image 启动前预检..."
if ! evaluate_rollback_gate; then
    echo "  ❌ FATAL: rollback 安全门拒绝: $ROLLBACK_GATE_OUTPUT"
    echo "  目标容器未启动，Nginx 未修改"
    exit 1
fi
echo "  ✅ 启动前预检通过: $ROLLBACK_GATE_OUTPUT"
echo "  state: marker=$CUTOVER_ACTIVE generation=$WRITER_GENERATION unsettled_gen2=$UNSETTLED_GEN2"

LEGACY_PHASE_A_TARGET=false
if [ "$TARGET_CAPABILITY" = "$LEGACY_SNAPSHOT_CAPABILITY" ]; then
    LEGACY_PHASE_A_TARGET=true
    echo "[1.5/5] Phase A legacy target：旧 web 内嵌 cron，零 gen2 资金 gate 已通过"
else
    echo "[1.5/5] 证明 target cron 与 target web 是同一审核镜像..."
    TARGET_CRON_IMAGE_PRE=$(docker inspect --format='{{.Image}}' "omnirank-cron-$ROLLBACK_TO" 2>/dev/null || true)
    TARGET_CRON_ROLE=$(docker exec "omnirank-cron-$ROLLBACK_TO" printenv ROLE 2>/dev/null | tr -d '\r\n' || true)
    TARGET_CRON_WORKERS=$(docker exec "omnirank-cron-$ROLLBACK_TO" printenv WORKERS 2>/dev/null | tr -d '\r\n' || true)
    if [ "$TARGET_CRON_IMAGE_PRE" != "$TARGET_IMAGE_PRE" ] \
       || [ "$TARGET_CRON_ROLE" != "cron" ] \
       || [ "$TARGET_CRON_WORKERS" != "1" ]; then
        echo "  ❌ FATAL: target cron image/ROLE/WORKERS 无法证明；未停止 active cron，Nginx 未修改"
        exit 1
    fi
fi

echo "[2/5] 先停 active cron，再启动同镜像 target web+cron..."
# W4 D3：旧 web 启动前必须摘掉当前 cron，禁止两个版本的资金定时任务混跑。
ACTIVE_CRON_WAS_RUNNING=$(docker inspect --format='{{.State.Running}}' "omnirank-cron-$ACTIVE" 2>/dev/null || true)
if [ "$ACTIVE_CRON_WAS_RUNNING" = "true" ]; then
    docker stop --time=30 "omnirank-cron-$ACTIVE"
fi
TARGET_CRON_WAS_RUNNING=false
if [ "$LEGACY_PHASE_A_TARGET" != "true" ]; then
    TARGET_CRON_WAS_RUNNING=$(docker inspect --format='{{.State.Running}}' "omnirank-cron-$ROLLBACK_TO" 2>/dev/null || true)
    if [ "$TARGET_CRON_WAS_RUNNING" != "true" ]; then
        docker start "omnirank-cron-$ROLLBACK_TO" >/dev/null
    fi
fi
# 只启动预检过的现有容器；compose up 可因配置/标签变化
# 重建成另一 image，所以 Phase A/Phase B 均明确禁用。
TARGET_WAS_RUNNING=$(docker inspect --format='{{.State.Running}}' "omnirank-$ROLLBACK_TO")
if [ "$TARGET_WAS_RUNNING" != "true" ]; then
    docker start "omnirank-$ROLLBACK_TO" >/dev/null
fi

echo "[3/5] 等待目标就绪（超时绝不切流）..."
if ! wait_ready "omnirank-$ROLLBACK_TO" "$ROLLBACK_PORT"; then
    echo "  ❌ FATAL: $ROLLBACK_TO 未能在 150s 内就绪；Nginx 未修改"
    if [ "$TARGET_WAS_RUNNING" != "true" ]; then
        docker stop --time=10 "omnirank-$ROLLBACK_TO" >/dev/null 2>&1 || true
    fi
    restore_active_cron
    exit 1
fi
echo "  ✅ 目标实例已就绪"

if [ "$LEGACY_PHASE_A_TARGET" = "true" ]; then
    if ! wait_legacy_scheduler_lock; then
        echo "  ❌ FATAL: legacy target 120s 未取得旧 scheduler:lock；Nginx 未修改"
        if [ "$TARGET_WAS_RUNNING" != "true" ]; then
            docker stop --time=10 "omnirank-$ROLLBACK_TO" >/dev/null 2>&1 || true
        fi
        restore_active_cron
        exit 1
    fi
    echo "  ✅ legacy target 内嵌 scheduler 已取得旧锁"
else
    if ! wait_cron_leader "omnirank-cron-$ROLLBACK_TO"; then
        echo "  ❌ FATAL: target cron 120s 未成为 Redis leader；Nginx 未修改"
        if [ "$TARGET_WAS_RUNNING" != "true" ]; then
            docker stop --time=10 "omnirank-$ROLLBACK_TO" >/dev/null 2>&1 || true
        fi
        restore_active_cron
        exit 1
    fi
    echo "  ✅ target cron 已成为当前 Redis leader"
fi

echo "[4/5] 启动后身份复核 + 切流前动态再判定..."
TARGET_IMAGE=$(docker inspect --format='{{.Image}}' "omnirank-$ROLLBACK_TO")
TARGET_CAPABILITY_AFTER=$(image_snapshot_capability "$TARGET_IMAGE") || true
if [ "$TARGET_IMAGE" != "$TARGET_IMAGE_PRE" ] || [ "$TARGET_CAPABILITY_AFTER" != "$TARGET_CAPABILITY" ]; then
    echo "  ❌ FATAL: target 启动后 image/capability 漂移；Nginx 未修改"
    restore_active_cron
    exit 1
fi
if [ "$LEGACY_PHASE_A_TARGET" != "true" ]; then
    TARGET_CRON_IMAGE_AFTER=$(docker inspect --format='{{.Image}}' "omnirank-cron-$ROLLBACK_TO" 2>/dev/null || true)
    if [ "$TARGET_CRON_IMAGE_AFTER" != "$TARGET_CRON_IMAGE_PRE" ]; then
        echo "  ❌ FATAL: target cron 启动后 image 漂移；Nginx 未修改"
        restore_active_cron
        exit 1
    fi
fi
if ! evaluate_rollback_gate; then
    echo "  ❌ FATAL: 切流前状态已不安全: $ROLLBACK_GATE_OUTPUT"
    if [ "$TARGET_WAS_RUNNING" != "true" ]; then
        docker stop --time=10 "omnirank-$ROLLBACK_TO" >/dev/null 2>&1 || true
    fi
    restore_active_cron
    echo "  Nginx 未修改"
    exit 1
fi
echo "  ✅ 动态 rollback gate 复核通过: $ROLLBACK_GATE_OUTPUT"

echo "[5/5] 切换 Nginx；原 active 保持运行作为反向热备..."
cp "$NGINX_CONF" "$NGINX_CONF.rollback-tmp"
sed -i "s|proxy_pass http://127.0.0.1:$ACTIVE_PORT|proxy_pass http://127.0.0.1:$ROLLBACK_PORT|" "$NGINX_CONF"
if ! grep -q "proxy_pass http://127.0.0.1:$ROLLBACK_PORT" "$NGINX_CONF" \
   || ! nginx -t 2>/dev/null; then
    mv "$NGINX_CONF.rollback-tmp" "$NGINX_CONF"
    restore_active_cron
    echo "  ❌ FATAL: Nginx 配置校验失败，已恢复原配置"
    exit 1
fi
if ! nginx -s reload; then
    mv "$NGINX_CONF.rollback-tmp" "$NGINX_CONF"
    if ! nginx -t 2>/dev/null || ! nginx -s reload; then
        echo "  🔴 原 Nginx 配置未能 reload；需立即人工介入"
    fi
    restore_active_cron
    echo "  ❌ FATAL: Nginx reload 失败，已恢复原配置并尝试恢复 active cron"
    exit 1
fi
rm -f "$NGINX_CONF.rollback-tmp"

echo ""
echo "========================================="
echo "  热切换完成"
echo "  活跃: $ROLLBACK_TO ($ROLLBACK_PORT)"
echo "  热备: $ACTIVE ($ACTIVE_PORT) · 保持运行"
if [ "$LEGACY_PHASE_A_TARGET" = "true" ]; then
    echo "  cron leader: legacy web 内嵌 scheduler · candidate cron 已停止"
else
    echo "  cron leader: omnirank-cron-$ROLLBACK_TO · 原 cron 已停止"
fi
echo "========================================="
