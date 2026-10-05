#!/bin/bash
# ============================================
# 蓝绿部署脚本
# 只能由外部 reviewed-release bootstrap 从独立 release worktree 调用。
# 直接在生产 runtime checkout 中运行会 fail-closed。
#
# 流程: 锁定审核 SHA → 构建待机实例 → 健康检查 → 切流量 → 停旧实例
# 回滚: 从同一 release worktree 运行 scripts/rollback-blue-green.sh
# ============================================
set -euo pipefail

SCRIPT_PATH=$(readlink -f "${BASH_SOURCE[0]}")
PROJECT_DIR=$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd -P)
DEPLOY_SHA="${DEPLOY_SHA:-}"
REVIEWED_DEPLOY_SCRIPT_SHA256="${REVIEWED_DEPLOY_SCRIPT_SHA256:-}"
REVIEWED_ROLLBACK_SCRIPT_SHA256="${REVIEWED_ROLLBACK_SCRIPT_SHA256:-}"
DEPLOY_RELEASE_WORKTREE="${DEPLOY_RELEASE_WORKTREE:-}"
RUNTIME_ROOT="${RUNTIME_ROOT:-}"
OMNIRANK_COMPOSE_PROJECT_NAME="${OMNIRANK_COMPOSE_PROJECT_NAME:-}"
NGINX_CONF="${NGINX_CONF:-/www/server/panel/vhost/nginx/omnirank.top.conf}"
LOCK_FILE="${LOCK_FILE:-/tmp/omnirank-deploy.lock}"
DB_CONTAINER="${DB_CONTAINER:-omnirank-db}"
REDIS_CONTAINER="${REDIS_CONTAINER:-omnirank-redis}"
DB_USER="${DB_USER:-geo_admin}"
DB_NAME="${DB_NAME:-geo_agentscope}"
SNAPSHOT_CAPABILITY="agent-inventory-snapshot-v3"
LEGACY_SNAPSHOT_CAPABILITY="legacy-no-inventory-snapshot-v1"

fatal_release() {
    echo "❌ immutable release gate: $*" >&2
    exit 1
}

assert_release_identity() {
    local actual_head actual_script_hash actual_rollback_hash release_status ignored_release_files
    [[ "$DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]] \
        || fatal_release "DEPLOY_SHA 必须是 40 位小写审核 commit SHA"
    [[ "$REVIEWED_DEPLOY_SCRIPT_SHA256" =~ ^[0-9a-f]{64}$ ]] \
        || fatal_release "REVIEWED_DEPLOY_SCRIPT_SHA256 必须是 64 位小写 SHA-256"
    [[ "$REVIEWED_ROLLBACK_SCRIPT_SHA256" =~ ^[0-9a-f]{64}$ ]] \
        || fatal_release "REVIEWED_ROLLBACK_SCRIPT_SHA256 必须是 64 位小写 SHA-256"
    [ -n "$DEPLOY_RELEASE_WORKTREE" ] \
        || fatal_release "必须由外部 reviewed-release bootstrap 传入 DEPLOY_RELEASE_WORKTREE"
    [ -n "$RUNTIME_ROOT" ] || fatal_release "缺少独立的 RUNTIME_ROOT"
    [ -d "$DEPLOY_RELEASE_WORKTREE" ] || fatal_release "release worktree 不存在"
    [ -d "$RUNTIME_ROOT" ] || fatal_release "runtime root 不存在"

    DEPLOY_RELEASE_WORKTREE=$(cd "$DEPLOY_RELEASE_WORKTREE" && pwd -P)
    RUNTIME_ROOT=$(cd "$RUNTIME_ROOT" && pwd -P)
    [ "$PROJECT_DIR" = "$DEPLOY_RELEASE_WORKTREE" ] \
        || fatal_release "执行脚本不在指定 release worktree"
    [ "$PROJECT_DIR" != "$RUNTIME_ROOT" ] \
        || fatal_release "release worktree 不得复用生产 runtime checkout"
    [ -z "$(git -C "$PROJECT_DIR" rev-parse --show-prefix)" ] \
        || fatal_release "脚本目录不是 release worktree 根"

    actual_head=$(git -C "$PROJECT_DIR" rev-parse HEAD)
    [ "$actual_head" = "$DEPLOY_SHA" ] \
        || fatal_release "HEAD=$actual_head，不等于 DEPLOY_SHA=$DEPLOY_SHA"
    actual_script_hash=$(sha256sum "$SCRIPT_PATH" | awk '{print $1}')
    [ "$actual_script_hash" = "$REVIEWED_DEPLOY_SCRIPT_SHA256" ] \
        || fatal_release "deploy script hash 不匹配审核包"
    [ -f "$PROJECT_DIR/scripts/rollback-blue-green.sh" ] \
        || fatal_release "release 缺少 rollback-blue-green.sh"
    actual_rollback_hash=$(sha256sum "$PROJECT_DIR/scripts/rollback-blue-green.sh" | awk '{print $1}')
    [ "$actual_rollback_hash" = "$REVIEWED_ROLLBACK_SCRIPT_SHA256" ] \
        || fatal_release "rollback script hash 不匹配审核包"
    release_status=$(git -C "$PROJECT_DIR" status --porcelain --untracked-files=all)
    [ -z "$release_status" ] || fatal_release "release worktree 存在意外改动"
    ignored_release_files=$(git -C "$PROJECT_DIR" ls-files --others --ignored --exclude-standard)
    [ -z "$ignored_release_files" ] || fatal_release "release worktree 存在被忽略的额外文件"

    if [ -z "$OMNIRANK_COMPOSE_PROJECT_NAME" ]; then
        OMNIRANK_COMPOSE_PROJECT_NAME=$(basename "$RUNTIME_ROOT")
    fi
    [[ "$OMNIRANK_COMPOSE_PROJECT_NAME" =~ ^[a-z0-9][a-z0-9_-]*$ ]] \
        || fatal_release "Compose project name 非法: $OMNIRANK_COMPOSE_PROJECT_NAME"
}

release_contract_line() {
    local pattern="$1"
    local lines
    lines=$(awk "$pattern { print NR }" "$SCRIPT_PATH")
    [[ "$lines" =~ ^[0-9]+$ ]] || fatal_release "部署契约调用缺失或不唯一: $pattern"
    printf '%s' "$lines"
}

verify_release_contract() {
    local phase_a_line switch_line hot_line activation_line
    phase_a_line=$(release_contract_line '/^[[:space:]]*record_inventory_snapshot_phase_a_rollback[[:space:]]*$/')
    switch_line=$(release_contract_line '/^[[:space:]]*cp "\$NGINX_CONF" "\$NGINX_CONF\.deploy-tmp"[[:space:]]*$/')
    hot_line=$(release_contract_line '/^[[:space:]]*record_inventory_snapshot_hot_rollback_ready[[:space:]]*$/')
    activation_line=$(release_contract_line '/^[[:space:]]*activate_inventory_snapshot_cutover[[:space:]]*$/')
    [ "$phase_a_line" -lt "$switch_line" ] \
        && [ "$switch_line" -lt "$hot_line" ] \
        && [ "$hot_line" -lt "$activation_line" ] \
        || fatal_release "Phase A / switch / hot rollback / activation 顺序不成立"
    echo "RELEASE_IDENTITY_VERIFIED DEPLOY_SHA=$DEPLOY_SHA"
    echo "PHASE_A_GATE_VERIFIED line=$phase_a_line before_switch=$switch_line"
    echo "ACTIVATION_GATE_VERIFIED hot_line=$hot_line before_activation=$activation_line"
}

compose() {
    OMNIRANK_RUNTIME_ROOT="$RUNTIME_ROOT" \
    OMNIRANK_COMPOSE_PROJECT_NAME="$OMNIRANK_COMPOSE_PROJECT_NAME" docker compose \
        --project-name "$OMNIRANK_COMPOSE_PROJECT_NAME" \
        --project-directory "$PROJECT_DIR" \
        --env-file "$RUNTIME_ROOT/.env" \
        -f "$PROJECT_DIR/docker-compose.yml" "$@"
}

container_identity() {
    docker inspect --format='{{.Id}}|{{.State.StartedAt}}' "$1" 2>/dev/null
}

capture_infra_identity() {
    DB_IDENTITY_BEFORE=$(container_identity "$DB_CONTAINER") \
        || fatal_release "无法读取 DB 容器身份: $DB_CONTAINER"
    REDIS_IDENTITY_BEFORE=$(container_identity "$REDIS_CONTAINER") \
        || fatal_release "无法读取 Redis 容器身份: $REDIS_CONTAINER"
    [ -n "$DB_IDENTITY_BEFORE" ] && [ -n "$REDIS_IDENTITY_BEFORE" ] \
        || fatal_release "DB/Redis 容器身份为空"
    echo "INFRA_IDENTITY_CAPTURED db=$DB_IDENTITY_BEFORE redis=$REDIS_IDENTITY_BEFORE"
}

assert_infra_identity_unchanged() {
    local checkpoint="$1" db_now redis_now
    db_now=$(container_identity "$DB_CONTAINER") \
        || fatal_release "$checkpoint 后无法读取 DB 容器身份"
    redis_now=$(container_identity "$REDIS_CONTAINER") \
        || fatal_release "$checkpoint 后无法读取 Redis 容器身份"
    [ "$db_now" = "$DB_IDENTITY_BEFORE" ] \
        || fatal_release "$checkpoint 改变了 DB 容器 ID/StartedAt: $DB_IDENTITY_BEFORE -> $db_now"
    [ "$redis_now" = "$REDIS_IDENTITY_BEFORE" ] \
        || fatal_release "$checkpoint 改变了 Redis 容器 ID/StartedAt: $REDIS_IDENTITY_BEFORE -> $redis_now"
    echo "INFRA_IDENTITY_UNCHANGED checkpoint=$checkpoint"
}

assert_release_identity
if [ "${1:-}" = "--verify-release-contract" ]; then
    verify_release_contract
    exit 0
fi

for required_runtime_file in .env apiclient_key.pem pub_key.pem; do
    [ -f "$RUNTIME_ROOT/$required_runtime_file" ] \
        || fatal_release "runtime 文件缺失: $RUNTIME_ROOT/$required_runtime_file"
done
install -d -m 0700 \
    "$RUNTIME_ROOT/data/marketing-private" \
    "$RUNTIME_ROOT/data/marketing-private/qr-inputs" \
    "$RUNTIME_ROOT/data/marketing-private/geo-materials"
if grep -R -n -E 'marketing-private|/app/data' "$NGINX_CONF" "$PROJECT_DIR/nginx.conf" >/dev/null; then
    fatal_release "Nginx 配置不得映射 /app/data 或 marketing-private"
fi
compose --profile green --profile cron-blue --profile cron-green config --quiet
echo "✅ immutable release gate 通过 · HEAD=$DEPLOY_SHA · runtime checkout 未修改"

db_scalar() {
    docker exec "$DB_CONTAINER" psql -X -U "$DB_USER" -d "$DB_NAME" -tAqc "$1" 2>/dev/null \
        | tr -d '\r\n '
}

snapshot_cutover_state() {
    local state
    state=$(db_scalar "SELECT COUNT(*) FROM system_settings WHERE key='AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT'") || return 1
    case "$state" in
        0|1) printf '%s' "$state" ;;
        *) return 1 ;;
    esac
}

container_snapshot_capability() {
    docker exec "$1" python -c \
        'from services.agent_inventory_pricing import INVENTORY_SNAPSHOT_RUNTIME_CAPABILITY; print(INVENTORY_SNAPSHOT_RUNTIME_CAPABILITY)' \
        2>/dev/null | tr -d '\r\n '
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

wait_container_ready() {
    local container="$1"
    local port="$2"
    local attempts="${3:-60}"
    local i status
    for i in $(seq 1 "$attempts"); do
        status=$(docker inspect --format='{{.State.Health.Status}}' "$container" 2>/dev/null || echo "not_found")
        if [ "$status" = "healthy" ] || curl -sf "http://localhost:$port" >/dev/null 2>&1; then
            return 0
        fi
        sleep 5
    done
    return 1
}

wait_active_web_drain() {
    local container="omnirank-$ACTIVE" waited=0 max_wait=360 active_tasks recent
    echo "  等待旧 $ACTIVE 长任务排空..."
    while [ "$waited" -lt "$max_wait" ]; do
        active_tasks=$(docker exec "$container" curl -s --max-time 5 \
            http://localhost:8000/api/diagnosis/active-count 2>/dev/null || echo "0")
        if echo "$active_tasks" | grep -q "NOT_AUTHENTICATED\|Unauthorized\|401"; then
            active_tasks=0
        fi
        if ! echo "$active_tasks" | grep -qE '^[0-9]+$'; then
            recent=$(docker logs "$container" --since 30s 2>&1 \
                | grep -v "active-count" \
                | grep -c "diagnosis\|诊断\|run_diagnosis" || true)
            active_tasks=$([ "$recent" -gt 0 ] && echo 1 || echo 0)
        fi
        if [ "$active_tasks" -eq 0 ]; then
            echo "  ✅ 旧 web 无进行中长任务"
            return 0
        fi
        echo "  进行中任务=$active_tasks；等待 (${waited}s/${max_wait}s)"
        sleep 15
        waited=$((waited + 15))
    done
    echo "  ❌ FATAL: 旧 web 长任务 360s 未排空；不强杀"
    return 1
}

inventory_snapshot_schema_state() {
    db_scalar "
        WITH bigint_state AS (
            SELECT COUNT(*) AS n
            FROM information_schema.columns c
            JOIN (VALUES
                ('agent_inventory_wallets','paid_inventory_points'),
                ('agent_inventory_wallets','bonus_inventory_points'),
                ('agent_inventory_wallets','frozen_inventory_points'),
                ('agent_inventory_transactions','points'),
                ('agent_inventory_transactions','balance_paid_after'),
                ('agent_inventory_transactions','balance_bonus_after'),
                ('customer_agent_credit_wallets','tool_credit_points'),
                ('customer_agent_credit_wallets','publish_credit_points'),
                ('customer_agent_credit_wallets','bonus_credit_points'),
                ('customer_credit_transactions','points'),
                ('customer_credit_transactions','balance_tool_after'),
                ('customer_credit_transactions','balance_publish_after'),
                ('customer_credit_transactions','balance_bonus_after')
            ) required(table_name,column_name)
              ON c.table_schema=current_schema()
             AND c.table_name=required.table_name
             AND c.column_name=required.column_name
             AND c.data_type='bigint'
        ), eligibility AS (
            SELECT COUNT(*) AS n FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name='recharge_orders'
              AND column_name='agent_inventory_legacy_eligible' AND data_type='boolean'
        ), writer_generation AS (
            SELECT COUNT(*) AS n FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name='recharge_orders'
              AND column_name='agent_inventory_writer_generation' AND data_type='smallint'
        ), generation_fence AS (
            SELECT COUNT(*) AS n FROM pg_trigger t
            JOIN pg_class c ON c.oid=t.tgrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=current_schema() AND c.relname='recharge_orders'
              AND t.tgname='trg_agent_inventory_snapshot_writer_generation'
              AND NOT t.tgisinternal AND t.tgenabled <> 'D'
        ), generation_sequence AS (
            SELECT COUNT(*) AS n FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=current_schema()
              AND c.relname='agent_inventory_writer_generation_fence_seq'
              AND c.relkind='S'
        )
        SELECT bigint_state.n || '|' || eligibility.n || '|' || writer_generation.n || '|' || generation_fence.n || '|' || generation_sequence.n
        FROM bigint_state,eligibility,writer_generation,generation_fence,generation_sequence"
}

require_inventory_snapshot_schema() {
    local state
    state=$(inventory_snapshot_schema_state)
    if [ "$state" != "13|1|1|1|1" ]; then
        echo "  ❌ FATAL: inventory snapshot schema=$state，要求 13|1|1|1|1"
        return 1
    fi
    echo "  ✅ inventory snapshot schema=13|1|1|1|1"
}

# ===== BEGIN rollback-capability-probe (锁 harness 按此标记整块抽取 · 勿删标记) =====
# [2.5.5/8] 回滚能力探针 —— 关掉唯一一条至今零信号的失效路径。
#
# 窗口:`[2.5/8]` 迁移跑完(库已经是新代码期望的样子)到 `[2.6/8]` 候选容器启动之间,
# 库=新 / 两个槽的代码都=旧。若这批迁移动到"精确相等 / 定义全等"的契约对象,
# 两槽**同时失去可重启性**,而公网照常 200(守卫只在容器启动时跑)、`[7/8]` 照常打
# 「热回滚已就绪」(它验的是**同镜像热备**,不是版本回退)→ 任何指标都不会说"现在不能重启"。
#
# 两条腿(在不同的东西里取证 · 只做腿 A = 只关了一半):
#   腿 A 现役可重启性   = `docker exec omnirank-$ACTIVE`(旧代码 + 新库)
#   腿 B 版本回退可用性 = 上上一版镜像起的一次性容器 `omnirank-release:$prev`
#   —— 「上一版」就是 $ACTIVE 正在跑的那个,腿 A 已覆盖;腿 B 探的是**再往前回退一版**。
#
# 五态(全部**不阻断**):迁移已经跑完了,`exit 1` 撤不回迁移,只会把操作者留在那个坏
# 窗口里而且连候选容器都没起,严格更糟。判断权在人,脚本只负责让这件事不再隐形。
#   ✅ [RollbackCapability:OK]             两腿守卫全过
#   🔴 [RollbackCapability:GuardFail]      任一腿守卫抛异常 → 跑回滚 SQL 或尽快把新代码切上去
#   🔴 [RollbackCapability:NoPrevImage]    再往前一版镜像已不在本机(回退落点没了)→ 重建镜像
#   🔴 [RollbackCapability:ProbeError]     探针自身故障(docker / 网络 / 取 URL 失败)→ 修探针
#   ⚪ [RollbackCapability:NotApplicable]  仅:目标镜像里缺 startup_schema_guards 模块
# 标签用「方括号 + 冒号分段」以便精确 grep(`🔴 \[RollbackCapability:` 抓全部告警、
# `:NoPrevImage` 抓单一态)。措辞里避免否定式 —— 运维 grep 会连否定句一起命中。
# 🔴 探针自身故障归 ProbeError 而**不**归 ⚪:归 ⚪ 会让"探针坏了"和"探针不适用"长得
#    一样 → 探针死掉后永远显示 ⚪ 而没人知道(= 用新的静默失败换掉旧的)。
#
# 🔴 守卫集唯一来源 = `services/startup_schema_guards.py` 的 `run_fleet_schema_guards()`。
#    这里**不另列一份六道** —— 那会成为第三处漂移源,正是单一清单要消灭的东西。
# 🔴 禁 `import server`:ROLE 未设时它会走 `_run_sql_migrations(fail_fast=False)`,
#    即在生产容器里跑迁移(该模块 docstring 自带这条禁令)。
ROLLBACK_PROBE_LOG="${ROLLBACK_PROBE_LOG:-/opt/omnirank/rollback_capability.log}"
ROLLBACK_PROBE_STATE=""
ROLLBACK_PROBE_HEADLINE=""

# 两条腿共用的探测体。**分类只看 stdout 标记,不看退出码**:一个标记都没印到
# = 容器 / 网络 / python 根本没跑起来 = ProbeError,而不是"契约通过"。
rollback_probe_python_body() {
    cat <<'ROLLBACK_PROBE_PY'
import sys
sys.path.insert(0, '/app')
try:
    from services.startup_schema_guards import run_fleet_schema_guards
except ModuleNotFoundError as exc:
    _name = exc.name or ''
    if _name == 'services' or _name.startswith('services.startup_schema_guards'):
        # 回退到引入该模块之前的镜像 = 探针不适用,**不是**契约失败。
        print('RBPROBE_NA', _name)
        sys.exit(0)
    print('RBPROBE_GUARDFAIL ModuleNotFoundError', exc)
    sys.exit(0)
try:
    print('RBPROBE_OK', run_fleet_schema_guards())
except BaseException as exc:
    print('RBPROBE_GUARDFAIL', type(exc).__name__, exc)
    sys.exit(0)
ROLLBACK_PROBE_PY
}

# GuardFail 优先于 OK:两个标记同现只可能是异常场景,按更危险的那侧读。
rollback_probe_classify() {
    case "$1" in
        *RBPROBE_GUARDFAIL*) echo "GuardFail" ;;
        *RBPROBE_NA*)        echo "NotApplicable" ;;
        *RBPROBE_OK*)        echo "OK" ;;
        *)                   echo "ProbeError" ;;
    esac
}

# 🔴 腿 A 的目标必须是 $ACTIVE(正在服务、跑旧代码的那个槽)。
#    改成本次新构建的镜像 = 新代码 × 新库 = 恒绿:探针看起来在跑、六道全过、报告漂亮,
#    而对"现役失去可重启性"零覆盖(变异 ④′ 钉这条)。
rollback_probe_leg_a() {
    docker exec "omnirank-$ACTIVE" python -c "$(rollback_probe_python_body)" 2>&1
}

rollback_probe_resolve_cur() {
    docker inspect "omnirank-$ACTIVE" \
        --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null
}

# prev = 排除现役(cur)与本次候选($DEPLOY_SHA)之后最新的 release 镜像 = **上上一版**。
# 🔴 排除条件里的 $DEPLOY_SHA 不可省:候选镜像在 `[2/6]` 就已经打标而且 Created 最新,
#    省掉它 prev 会选中候选自己 → 新代码 × 新库 → 腿 B 整条恒绿(变异 ⑥ 钉这条)。
rollback_probe_resolve_prev() {
    local cur="$1"
    docker images omnirank-release --format '{{.Tag}}|{{.CreatedAt}}' 2>/dev/null \
        | sort -t'|' -k2 -r | cut -d'|' -f1 \
        | grep -v "^${cur}$" | grep -v "^${DEPLOY_SHA}$" | head -1
}

rollback_probe_leg_b() {
    local prev="$1" net dburl
    net=$(docker inspect "$DB_CONTAINER" \
        --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null \
        | awk '{print $1}')
    if [ -z "$net" ]; then
        echo "RBPROBE_NET_UNRESOLVED"
        return 0
    fi
    # 🔴 直接取现役容器正在用的 URL:不解析口令、不回显、不碰 .env
    #    (`.env` 里 DATABASE_URL 指向 localhost:5432,而 compose 服务级 environment
    #     覆盖成 @db:5432,且 db 的 5432 未映射宿主 → 用 .env 从任何容器都连不通)。
    dburl=$(docker exec "omnirank-$ACTIVE" printenv DATABASE_URL 2>/dev/null)
    if [ -z "$dburl" ]; then
        echo "RBPROBE_DBURL_UNRESOLVED"
        return 0
    fi
    # 🔴 `export` + `-e DATABASE_URL`(**只给变量名**,值由 docker 从环境继承)。
    #    带值写法会让口令出现在 docker run 的命令行参数里 → 宿主 ps aux /
    #    /proc/<pid>/cmdline 在那几秒可见。子 shell 限定作用域,不污染父环境。
    (
        export DATABASE_URL="$dburl"
        docker run --rm --network "$net" \
            -e DATABASE_URL \
            --entrypoint python "omnirank-release:$prev" \
            -c "$(rollback_probe_python_body)" 2>&1
    ) || true
}

# 落痕格式 `ts|deploy_sha|state|active|prev` 必须机器可解析 —— 下一批要 parse 它,
# 判断「连续两批都是红态」。单批红可能是操作者已知并打算前滚;连续多批说明没人处置。
rollback_probe_red_streak() {
    if [ ! -f "$ROLLBACK_PROBE_LOG" ]; then
        echo 0
        return 0
    fi
    awk -F'|' '
        $3=="GuardFail" || $3=="NoPrevImage" || $3=="ProbeError" { n=n+1; next }
        { n=0 }
        END { print n+0 }
    ' "$ROLLBACK_PROBE_LOG" 2>/dev/null || echo 0
}

probe_rollback_capability() {
    local out_a out_b state_a state_b state cur prev prev_label prev_short ts streak total
    echo "[2.5.5/8] 回滚能力探针(两腿 · 告警不阻断)..."

    cur=$(rollback_probe_resolve_cur || true)
    out_a=$(rollback_probe_leg_a || true)
    state_a=$(rollback_probe_classify "$out_a")

    if [ -z "$cur" ]; then
        # 认不出现役版本 = 探针自身故障(prev 也就无从排除)。
        prev=""
        prev_label="unresolved"
        out_b="RBPROBE_CUR_UNRESOLVED"
        state_b="ProbeError"
    else
        prev=$(rollback_probe_resolve_prev "$cur" || true)
        if [ -n "$prev" ]; then
            out_b=$(rollback_probe_leg_b "$prev" || true)
            state_b=$(rollback_probe_classify "$out_b")
            prev_label="$prev"
        else
            out_b="RBPROBE_PREV_ABSENT"
            state_b="NoPrevImage"
            prev_label="absent"
        fi
    fi

    # 严重度合并:契约已破 > 回退落点没了 > 探针坏了 > 探针不适用 > 通过。
    # NoPrevImage 与 GuardFail 分开标签:处置动作不同(重建镜像 vs 跑回滚 SQL)。
    if [ "$state_a" = "GuardFail" ] || [ "$state_b" = "GuardFail" ]; then
        state="GuardFail"
    elif [ "$state_b" = "NoPrevImage" ]; then
        state="NoPrevImage"
    elif [ "$state_a" = "ProbeError" ] || [ "$state_b" = "ProbeError" ]; then
        state="ProbeError"
    elif [ "$state_a" = "NotApplicable" ] || [ "$state_b" = "NotApplicable" ]; then
        state="NotApplicable"
    else
        state="OK"
    fi

    prev_short="$prev_label"
    if [ ${#prev_label} -eq 40 ]; then
        prev_short="${prev_label:0:8}"
    fi
    local ident="active=$ACTIVE(${cur:0:8}) prev=$prev_short legA=$state_a legB=$state_b"

    streak=$(rollback_probe_red_streak)
    case "$state" in
        OK)
            ROLLBACK_PROBE_HEADLINE="✅ [RollbackCapability:OK] $ident 两腿 fleet 守卫全过"
            ;;
        NotApplicable)
            ROLLBACK_PROBE_HEADLINE="⚪ [RollbackCapability:NotApplicable] $ident 目标镜像里缺 startup_schema_guards 模块 · 属探针适用范围以外"
            ;;
        GuardFail)
            ROLLBACK_PROBE_HEADLINE="🔴 [RollbackCapability:GuardFail] $ident 处置:跑回滚 SQL 或尽快把新代码切上去"
            ;;
        NoPrevImage)
            ROLLBACK_PROBE_HEADLINE="🔴 [RollbackCapability:NoPrevImage] $ident 处置:重建镜像以恢复版本回退落点"
            ;;
        *)
            ROLLBACK_PROBE_HEADLINE="🔴 [RollbackCapability:ProbeError] $ident 处置:修探针"
            ;;
    esac
    case "$state" in
        OK|NotApplicable) ;;
        *)
            if [ "$streak" -ge 1 ]; then
                total=$((streak + 1))
                ROLLBACK_PROBE_HEADLINE="$ROLLBACK_PROBE_HEADLINE · 🔴 连续 $total 批不可回退 · 前面 $streak 批已是红态且至今无人处置"
            fi
            ;;
    esac

    echo "  $ROLLBACK_PROBE_HEADLINE"
    case "$state" in
        OK|NotApplicable) ;;
        *)
            echo "$out_a" | tail -3 | sed 's/^/    legA| /'
            echo "$out_b" | tail -3 | sed 's/^/    legB| /'
            ;;
    esac
    # §6 探不到什么 —— 防止下一任把"探针绿"读成"所有契约都安全"。
    echo "  探针范围: FLEET_SCHEMA_GUARDS 六道(diagnosis / geo_article_v14 / article_closed_loop / dealer_resale / geo_observation / monitoring_product)"
    echo "  探不到 1: server.py 的 organization schema readiness 目录指纹是另一个对象"
    echo "  探不到 2: whitelabel_backoffice 要用 prestart 那条正在跑迁移的游标,故意留在 prestart"
    echo "  探不到 3: 只在部署时刻取一次快照;此后自愈式 DDL 仍可能在数天后被真实用户走到而改表"
    echo "  探不到 4: 从旧 release 目录发起的部署没有探针(旧目录里躺的是旧脚本,而 sha256 分版表恰恰允许这么部署)"
    echo "  探不到 5: 腿 B 只验再往前一版;跳多版回退仍靠回滚依赖台账 + 人工判断"
    echo "  探针只报告,处置靠人 + 回滚依赖台账"

    # 部署日志会被下一次切流冲掉 → 必须另落宿主持久文件。
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    mkdir -p "$(dirname "$ROLLBACK_PROBE_LOG")" 2>/dev/null || true
    if ! printf '%s|%s|%s|%s|%s\n' "$ts" "$DEPLOY_SHA" "$state" "$ACTIVE" "$prev_label" \
        >> "$ROLLBACK_PROBE_LOG" 2>/dev/null; then
        echo "  ⚠️ 落痕写入失败: $ROLLBACK_PROBE_LOG"
    fi
    ROLLBACK_PROBE_STATE="$state"
    return 0
}
# ===== END rollback-capability-probe =====

backup_before_prestart_migrations() {
    local backup_dir
    backup_dir="${BACKUP_DIR:-/opt/omnirank/backups}"
    mkdir -p "$backup_dir"
    BACKUP_FILE="$backup_dir/pre-reviewed-release-$(date +%Y%m%d-%H%M%S).dump"
    echo "  prestart DDL 前备份数据库: $BACKUP_FILE"
    docker exec "$DB_CONTAINER" pg_dump -Fc -Z 6 \
        -U "$DB_USER" "$DB_NAME" > "$BACKUP_FILE"
    test -s "$BACKUP_FILE"
    docker exec -i "$DB_CONTAINER" pg_restore -l < "$BACKUP_FILE" >/dev/null
}

production_schema_hash() {
    docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" "$DB_NAME" \
        --schema-only --no-owner --no-privileges 2>/dev/null \
        | sed -E '/^\\(un)?restrict /d' \
        | sha256sum | awk '{print $1}'
}

drop_dry_run_database() {
    local dry_db="$1"
    docker exec "$DB_CONTAINER" dropdb -U "$DB_USER" --if-exists --force "$dry_db" \
        >/dev/null 2>&1 || true
}

run_candidate_prestart_for_database() {
    local dry_db="$1"
    compose run --rm --no-deps -T \
        -e ROLE=prestart -e DRY_RUN_DB="$dry_db" \
        --entrypoint sh "omnirank-$INACTIVE" -c \
        'export DATABASE_URL="${DATABASE_URL%/*}/$DRY_RUN_DB"; exec python -m scripts.prestart'
}

run_candidate_readiness_for_database() {
    local dry_db="$1"
    compose run --rm --no-deps -T \
        -e ROLE=prestart -e DRY_RUN_DB="$dry_db" \
        --entrypoint sh "omnirank-$INACTIVE" -c \
        'export DATABASE_URL="${DATABASE_URL%/*}/$DRY_RUN_DB"; exec python -m scripts.verify_unified_release_readiness'
}

run_rollback_sql_for_database() {
    local dry_db="$1"
    local relative_sql="$2"
    [ -f "$PROJECT_DIR/$relative_sql" ] \
        || fatal_release "rollback-forward SQL 缺失: $relative_sql"
    docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 \
        -U "$DB_USER" -d "$dry_db" < "$PROJECT_DIR/$relative_sql"
}

assert_unified_rollback_state() {
    local dry_db="$1"
    local state
    state=$(docker exec "$DB_CONTAINER" psql -X -U "$DB_USER" -d "$dry_db" -tAqc "
        SELECT concat_ws('|',
            pg_catalog.to_regclass('public.article_generation_revision_events') IS NULL,
            pg_catalog.to_regclass('public.monitoring_run_cells') IS NULL,
            pg_catalog.to_regclass('public.admin_demo_cases') IS NULL,
            (SELECT COUNT(*)=0 FROM information_schema.columns
              WHERE table_schema='public'
                AND table_name='marketing_material_generation_attempts'
                AND column_name IN (
                  'component_id','submit_state','poll_state','submit_guard_token',
                  'last_heartbeat_at','provider_result_jsonb','materialization_state',
                  'resolution_state','resolved_at'
                )),
            pg_catalog.to_regclass('public.quote_pricing_snapshots') IS NOT NULL
        )" | tr -d '\r\n ')
    [ "$state" = "t|t|t|t|t" ] \
        || fatal_release "unified rollback state 不符合预期: $state"
}

fresh_rollback_forward_validation() {
    local fresh_db pid_a pid_b
    fresh_db="omnirank_fresh_${DEPLOY_SHA:0:8}_$$"
    drop_dry_run_database "$fresh_db"
    docker exec "$DB_CONTAINER" createdb -U "$DB_USER" "$fresh_db"

    # The legacy application has no authoritative empty-database bootstrap for
    # every historical billing table. Restore only the clean active schema into
    # a new PG16 database: no production rows are copied, while all migrations
    # are exercised against the exact production-base catalog they will face.
    if ! docker exec -i \
        -e PGOPTIONS='-c max_parallel_maintenance_workers=0 -c maintenance_work_mem=32MB' \
        "$DB_CONTAINER" pg_restore --exit-on-error --schema-only \
        --no-owner --no-privileges -U "$DB_USER" -d "$fresh_db" < "$BACKUP_FILE"; then
        drop_dry_run_database "$fresh_db"
        fatal_release "PG16 fresh schema-only clean-base restore 失败"
    fi

    if ! run_candidate_prestart_for_database "$fresh_db" \
       || ! run_candidate_readiness_for_database "$fresh_db"; then
        drop_dry_run_database "$fresh_db"
        fatal_release "PG16 fresh migration/readiness 失败"
    fi

    if ! run_rollback_sql_for_database "$fresh_db" \
            scripts/rollback_article_generation_task_state_2026_07_21.sql \
       || ! run_rollback_sql_for_database "$fresh_db" \
            scripts/rollback_monitoring_cell_retry_2026_07_21.sql \
       || ! run_rollback_sql_for_database "$fresh_db" \
            scripts/rollback_quote_snapshots_and_archives_2026_07_21.sql \
       || ! run_rollback_sql_for_database "$fresh_db" \
            scripts/rollback_admin_cross_tenant_governance_2026_07_21.sql \
       || ! run_rollback_sql_for_database "$fresh_db" \
            scripts/rollback_geo_provider_attempts_2026_07_21.sql; then
        drop_dry_run_database "$fresh_db"
        fatal_release "PG16 unified rollback 失败"
    fi
    assert_unified_rollback_state "$fresh_db"

    if ! run_candidate_prestart_for_database "$fresh_db" \
       || ! run_candidate_prestart_for_database "$fresh_db" \
       || ! run_candidate_readiness_for_database "$fresh_db"; then
        drop_dry_run_database "$fresh_db"
        fatal_release "PG16 rollback-forward / 2x 失败"
    fi

    run_candidate_prestart_for_database "$fresh_db" & pid_a=$!
    run_candidate_prestart_for_database "$fresh_db" & pid_b=$!
    if ! wait "$pid_a" || ! wait "$pid_b" \
       || ! run_candidate_readiness_for_database "$fresh_db"; then
        drop_dry_run_database "$fresh_db"
        fatal_release "PG16 并发 prestart 单飞验证失败"
    fi
    drop_dry_run_database "$fresh_db"
    echo "  ✅ PG16 fresh schema-only clean base + rollback-forward + 2x + concurrent prestart 通过"
}

migration_dry_run_and_readiness() {
    local dry_db db_size free_bytes required_bytes schema_before schema_after
    dry_db="omnirank_dryrun_${DEPLOY_SHA:0:8}_$$"
    db_size=$(db_scalar "SELECT pg_database_size(current_database())")
    free_bytes=$(df -PB1 "$RUNTIME_ROOT" | awk 'NR==2 {print $4}')
    [[ "$db_size" =~ ^[0-9]+$ ]] && [[ "$free_bytes" =~ ^[0-9]+$ ]] \
        || fatal_release "无法核算 migration dry-run 磁盘预算"
    required_bytes=$((db_size * 2 + 1073741824))
    [ "$free_bytes" -gt "$required_bytes" ] \
        || fatal_release "migration dry-run 空间不足: free=$free_bytes required>$required_bytes"

    schema_before=$(production_schema_hash)
    [[ "$schema_before" =~ ^[0-9a-f]{64}$ ]] \
        || fatal_release "无法锁定 production schema hash"
    drop_dry_run_database "$dry_db"
    docker exec "$DB_CONTAINER" createdb -U "$DB_USER" "$dry_db"
    if ! docker exec -i \
        -e PGOPTIONS='-c max_parallel_maintenance_workers=0 -c maintenance_work_mem=32MB' \
        "$DB_CONTAINER" pg_restore --exit-on-error \
        --no-owner --no-privileges -U "$DB_USER" -d "$dry_db" < "$BACKUP_FILE"; then
        drop_dry_run_database "$dry_db"
        fatal_release "production dump 恢复到隔离 dry-run DB 失败"
    fi
    if ! run_candidate_prestart_for_database "$dry_db" \
       || ! run_candidate_prestart_for_database "$dry_db" \
       || ! run_candidate_readiness_for_database "$dry_db"; then
        drop_dry_run_database "$dry_db"
        fatal_release "migration dry-run / 2x / readiness 失败，production 未 forward"
    fi
    drop_dry_run_database "$dry_db"
    schema_after=$(production_schema_hash)
    [ "$schema_after" = "$schema_before" ] \
        || fatal_release "dry-run 污染 production schema: $schema_before -> $schema_after"
    echo "  ✅ production clone dry-run + 2x + unified readiness 通过；production schema 未变"
}

activate_inventory_snapshot_cutover() {
    docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" \
        < scripts/activate_agent_inventory_snapshot_cutover_2026_07_14.sql
}

record_inventory_snapshot_hot_rollback_ready() {
    docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" <<SQL
INSERT INTO system_settings(key,value,value_type,description,updated_at)
VALUES (
    'AGENT_INVENTORY_SNAPSHOT_HOT_ROLLBACK_READY',
    '$SNAPSHOT_CAPABILITY|$CANDIDATE_IMAGE_ID|$HOT_IMAGE_ID',
    'deploy_capability',
    'active 与 hot rollback 已验证为同 image 快照 binary；仅供首次 activation 十分钟窗口',
    clock_timestamp()
)
ON CONFLICT(key) DO UPDATE SET
    value=EXCLUDED.value,
    value_type=EXCLUDED.value_type,
    description=EXCLUDED.description,
    updated_at=EXCLUDED.updated_at;
SQL
}

record_inventory_snapshot_phase_a_rollback() {
    docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" <<SQL
INSERT INTO system_settings(key,value,value_type,description,updated_at)
VALUES (
    'AGENT_INVENTORY_SNAPSHOT_PHASE_A_ROLLBACK',
    '$PREVIOUS_IMAGE_ID|$CANDIDATE_IMAGE_ID|$PREVIOUS_CAPABILITY|$CANDIDATE_IMAGE_CAPABILITY',
    'deploy_capability',
    'Phase A 回滚证据：legacy/candidate image 及 capability；候选切流前写入',
    clock_timestamp()
)
ON CONFLICT(key) DO UPDATE SET
    value=EXCLUDED.value,
    value_type=EXCLUDED.value_type,
    description=EXCLUDED.description,
    updated_at=EXCLUDED.updated_at;
SQL
}

# 排他锁：防止两个 AI 同时跑部署脚本（曾因此导致 502）
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    EXISTING_PID=$(cat "$LOCK_FILE.pid" 2>/dev/null || echo "unknown")
    echo "❌ 错误: 另一个部署正在进行 (PID=$EXISTING_PID)"
    echo "    如果确认无并发，删除 $LOCK_FILE 后重试"
    exit 1
fi
echo "$$" > "$LOCK_FILE.pid"
trap 'rm -f "$LOCK_FILE.pid"' EXIT

cd "$PROJECT_DIR"

# 检查 Nginx 配置文件是否存在
if [ ! -f "$NGINX_CONF" ]; then
    echo "错误: Nginx 配置不存在: $NGINX_CONF"
    echo "请先按照 docs/蓝绿部署方案.md Step 3 创建宿主机 Nginx 配置"
    exit 1
fi

# 判断当前活跃实例（通过 proxy_pass 端口判断）
if grep -q "proxy_pass http://127.0.0.1:8001" "$NGINX_CONF"; then
    ACTIVE="blue"
    INACTIVE="green"
    ACTIVE_PORT=8001
    INACTIVE_PORT=8002
elif grep -q "proxy_pass http://127.0.0.1:8002" "$NGINX_CONF"; then
    ACTIVE="green"
    INACTIVE="blue"
    ACTIVE_PORT=8002
    INACTIVE_PORT=8001
else
    echo "错误: Nginx 配置中未找到 proxy_pass 8001 或 8002"
    exit 1
fi

echo "========================================="
echo "  蓝绿部署"
echo "  当前活跃: $ACTIVE ($ACTIVE_PORT)"
echo "  即将更新: $INACTIVE ($INACTIVE_PORT)"
echo "========================================="

# ===== BEGIN silent-revert-gate ============================================
# [1.9/8] 静默回退门:生产尖必须是 DEPLOY_SHA 的祖先。
#
# 🔴 由来(2026-08-01 真实事故未遂):待部署包 24c46669 与生产尖 4d454e70
#   **互不包含**(共同分叉于 f0f886dc)。原样部署会让当天上线的短视频服务端中转
#   悄悄消失 —— 而最阴的是:**它的验收判据照样全绿**,因为判据查的是 .env 里的闸
#   (仍 True/False),**查配置不查代码 → 回退不可见**。
#   分叉是 Review 复审时发现的;此门把"靠人发现"变成"机器必拦"。
#
# 🔴 PROD_SHA 必须【现取】:双信源(容器内 /app/RELEASE_SHA + OCI label)互校。
#   不用 prod-current tag(那是快照,且带注释 tag 裸 rev-parse 会返回 tag 对象而非
#   commit —— 同一个坑 2026-08-01 一天内被踩两次),不用任何记忆值。
silent_revert_gate() {
    local f l prod
    f=$(docker exec "omnirank-$ACTIVE" cat /app/RELEASE_SHA 2>/dev/null | tr -d '\r\n ')
    l=$(docker inspect -f '{{index .Config.Labels "io.omnirank.release.sha"}}' \
          "omnirank-$ACTIVE" 2>/dev/null | tr -d '\r\n ')

    # 首次装机 / 活跃槽还没跑起来 → 线上没有可丢的代码,放行但必须响亮说明
    if [ -z "$f" ] && [ -z "$l" ]; then
        echo "  ⚠️ [1.9/8] 取不到活跃槽版本(容器未运行?)—— 视为首次部署,无可回退代码,放行"
        return 0
    fi
    # 有一个取到、另一个取不到,或两者不等 → 状态不明,fail-closed
    if [ "$f" != "$l" ]; then
        echo "  ❌ FATAL: [1.9/8] 活跃槽双信源不一致 —— /app/RELEASE_SHA=[$f] label=[$l]"
        echo "     状态不明时不放行(fail-closed)。人工确认生产真实版本后再部署。"
        return 1
    fi
    prod="$f"
    if [ "$prod" = "$DEPLOY_SHA" ]; then
        echo "  ✅ [1.9/8] 静默回退门:与生产尖同版(重放同一 SHA)"
        return 0
    fi
    if git -C "$PROJECT_DIR" merge-base --is-ancestor "$prod" "$DEPLOY_SHA" 2>/dev/null; then
        echo "  ✅ [1.9/8] 静默回退门:生产尖 ${prod:0:12}… 是本次 HEAD 的祖先 → 不会丢线上代码"
        return 0
    fi
    echo "  ❌ FATAL: [1.9/8] 静默回退门 —— 生产尖 ${prod:0:12}… **不是** $DEPLOY_SHA 的祖先!"
    echo "     部署会让线上已有代码消失。会丢掉的 commit:"
    git -C "$PROJECT_DIR" log --oneline "$DEPLOY_SHA..$prod" 2>/dev/null | head -10 | sed 's/^/       /'
    echo "     处置:把本包 rebase/cherry-pick 到 ${prod:0:12}… 之上,交 Review 重出 clean SHA。"
    echo "     🔴 不要绕过本门 —— 它挡的正是"判据全绿但代码被回退"那一类。"
    return 1
}
if ! silent_revert_gate; then
    exit 1
fi
# ===== END silent-revert-gate ==============================================

# release Compose 只允许管理应用/cron。锚定基础容器 ID + StartedAt，之后每个
# run/up/rebuild 都复核，确保 db/redis 没有被隐式重建或重启。
capture_infra_identity

# 1. 审核 SHA 已由外部 bootstrap 锁定；本进程不更新或覆盖任何工作树。
echo "[1/6] 使用 immutable reviewed release: $DEPLOY_SHA"

BUILT_IMAGE_REF="${OMNIRANK_COMPOSE_PROJECT_NAME}-omnirank-${INACTIVE}"
IMMUTABLE_IMAGE_REF="omnirank-release:$DEPLOY_SHA"
EXISTING_IMMUTABLE_ID=$(docker image inspect "$IMMUTABLE_IMAGE_REF" \
    --format='{{.Id}}' 2>/dev/null || true)

# A retry after a pre-traffic migration/readiness failure must reuse the exact
# immutable artifact. Rebuilding with a new CACHEBUST could produce a second
# image for the same commit and either waste disk or make provenance ambiguous.
if [ -n "$EXISTING_IMMUTABLE_ID" ]; then
    BUILT_IMAGE_ID="$EXISTING_IMMUTABLE_ID"
    BUILT_RELEASE_SHA=$(docker image inspect "$BUILT_IMAGE_ID" \
        --format='{{index .Config.Labels "org.opencontainers.image.revision"}}')
    [ "$BUILT_RELEASE_SHA" = "$DEPLOY_SHA" ] \
        || fatal_release "$IMMUTABLE_IMAGE_REF revision=$BUILT_RELEASE_SHA，不等于 DEPLOY_SHA"
    docker tag "$BUILT_IMAGE_ID" "$BUILT_IMAGE_REF"
    echo "[2/6] 复用已验证 immutable inactive image: $BUILT_IMAGE_ID"
else
    CACHEBUST=$(date +%s)
    echo "[2/6] 构建 $INACTIVE 实例 (CACHEBUST=$CACHEBUST)..."
    if [ "$INACTIVE" = "green" ]; then
        compose --profile green build \
            --build-arg CACHEBUST="$CACHEBUST" \
            --build-arg RELEASE_SHA="$DEPLOY_SHA" \
            omnirank-green
    else
        compose build \
            --build-arg CACHEBUST="$CACHEBUST" \
            --build-arg RELEASE_SHA="$DEPLOY_SHA" \
            omnirank-blue
    fi
    BUILT_IMAGE_ID=$(docker image inspect "$BUILT_IMAGE_REF" --format='{{.Id}}' 2>/dev/null || true)
    [ -n "$BUILT_IMAGE_ID" ] || fatal_release "构建后无法解析 inactive image ID"
    BUILT_RELEASE_SHA=$(docker image inspect "$BUILT_IMAGE_ID" \
        --format='{{index .Config.Labels "org.opencontainers.image.revision"}}')
    [ "$BUILT_RELEASE_SHA" = "$DEPLOY_SHA" ] \
        || fatal_release "inactive image revision=$BUILT_RELEASE_SHA，不等于 DEPLOY_SHA"
    docker tag "$BUILT_IMAGE_ID" "$IMMUTABLE_IMAGE_REF"
fi
BUILT_IMAGE_CMD=$(docker image inspect "$BUILT_IMAGE_ID" --format='{{json .Config.Cmd}}')
[ "$BUILT_IMAGE_CMD" = '["/app/start.sh"]' ] \
    || fatal_release "$IMMUTABLE_IMAGE_REF command=$BUILT_IMAGE_CMD，不是审核启动命令 [/app/start.sh]"
BUILT_IMAGE_ENTRYPOINT=$(docker image inspect "$BUILT_IMAGE_ID" --format='{{json .Config.Entrypoint}}')
case "$BUILT_IMAGE_ENTRYPOINT" in
    null|'[]') ;;
    *) fatal_release "$IMMUTABLE_IMAGE_REF entrypoint=$BUILT_IMAGE_ENTRYPOINT，必须为空" ;;
esac
echo "  ✅ immutable image=$IMMUTABLE_IMAGE_REF id=$BUILT_IMAGE_ID revision=$BUILT_RELEASE_SHA"

# W4 schema 只由一次性 prestart 执行；--no-deps 禁止 Compose 管理 DB/Redis。
echo "[2.5/8] prestart migration + schema verification..."
backup_before_prestart_migrations
migration_dry_run_and_readiness
fresh_rollback_forward_validation
if ! compose run --rm --no-deps -T -e ROLE=prestart \
    --entrypoint python "omnirank-$INACTIVE" \
    -m scripts.prestart; then
    echo "  ❌ FATAL: prestart 迁移/反查失败；候选未启动"
    exit 1
fi
if ! compose run --rm --no-deps -T -e ROLE=prestart \
    --entrypoint python "omnirank-$INACTIVE" \
    -m scripts.verify_unified_release_readiness; then
    echo "  ❌ FATAL: production forward 后 unified readiness 失败；候选未启动"
    exit 1
fi
assert_infra_identity_unchanged "prestart"
require_inventory_snapshot_schema || exit 1

# 🔴 位置铁律:必须在 `[2.5/8]` 迁移**之后**(守卫验的是迁移建完的对象,排在迁移前必然
#    误判)、且在 `[2.6/8]` 启动候选容器**之前**(那之后 $INACTIVE 变成新候选、
#    `[7/8]` 之后连 $ACTIVE 槽都被换成候选镜像 → 探什么都恒绿)。
#    `|| true` 是语义的一部分:告警不阻断(迁移已跑完,abort 只会把人留在坏窗口里)。
probe_rollback_capability || true

echo "[2.6/8] 启动 $INACTIVE web (--no-deps)..."
if [ "$INACTIVE" = "green" ]; then
    compose --profile green up -d --no-deps omnirank-green
else
    compose up -d --no-deps omnirank-blue
fi
assert_infra_identity_unchanged "candidate-web-up"

# 3. 等待健康检查通过
echo "[3/6] 等待 $INACTIVE 健康检查..."
HEALTHY=false
for i in $(seq 1 60); do
    # 检查容器健康状态
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' "omnirank-$INACTIVE" 2>/dev/null || echo "not_found")
    if [ "$STATUS" = "healthy" ]; then
        HEALTHY=true
        echo "  健康检查通过 (${i}x30s)"
        break
    fi
    # 也尝试 HTTP 探测
    if curl -sf "http://localhost:$INACTIVE_PORT" > /dev/null 2>&1; then
        HEALTHY=true
        echo "  HTTP 探测通过"
        break
    fi
    echo "  等待中... ($i/60) status=$STATUS"
    sleep 5
done

if [ "$HEALTHY" = false ]; then
    echo "  错误: $INACTIVE 启动超时（5分钟），中止部署"
    echo "  查看日志: docker logs omnirank-$INACTIVE --tail 50"
    compose stop "omnirank-$INACTIVE"
    exit 1
fi

# 3.4. W4 WORKERS/ROLE 实证门。web 多 worker 必须显式声明 Redis progress bus 就绪；
# cron 恒单 worker。真实 worker 数必须与声明完全一致。
echo "[3.4/8] WORKERS/ROLE verify gate..."
NEW_ROLE=$(docker exec "omnirank-$INACTIVE" printenv ROLE 2>/dev/null | tr -d '\r\n')
NEW_ROLE=${NEW_ROLE:-web}
NEW_WORKERS=$(docker exec "omnirank-$INACTIVE" printenv WORKERS 2>/dev/null | tr -d '\r\n' || echo "1")
NEW_PBR=$(docker exec "omnirank-$INACTIVE" printenv PROGRESS_BUS_READY 2>/dev/null | tr -d '\r\n')
REAL_WORKERS=$(docker exec -i "omnirank-$INACTIVE" python - <<'PYEOF' 2>/dev/null || true
import os
import sys

procs = {}
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        with open("/proc/%s/cmdline" % pid, "rb") as handle:
            cmd = handle.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        with open("/proc/%s/stat" % pid) as handle:
            ppid = int(handle.read().split(")")[-1].split()[1])
        procs[int(pid)] = (ppid, cmd)
    except Exception:
        pass

masters = sorted(
    pid
    for pid, (ppid, cmd) in procs.items()
    if "uvicorn server:app" in cmd
    and "uvicorn server:app" not in procs.get(ppid, (0, ""))[1]
)
if not masters:
    print(0)
    sys.exit(0)
master = masters[0]

def is_worker(cmd):
    return "resource_tracker" not in cmd and ("spawn_main" in cmd or "uvicorn" in cmd)

workers = [pid for pid, (ppid, cmd) in procs.items() if ppid == master and is_worker(cmd)]
print(len(workers) if workers else 1)
PYEOF
)
REAL_WORKERS=$(echo "$REAL_WORKERS" | tr -d '\r\n ')
REAL_WORKERS=${REAL_WORKERS:-0}
echo "  ROLE=$NEW_ROLE env_WORKERS=$NEW_WORKERS real_workers=$REAL_WORKERS PBR=$NEW_PBR"
if [ "$NEW_ROLE" != "web" ]; then
    echo "  ❌ FATAL: 候选流量容器 ROLE=$NEW_ROLE，要求 web"
    compose stop "omnirank-$INACTIVE"
    exit 1
fi
if [ "$NEW_WORKERS" != "1" ] && [ "$NEW_PBR" != "1" ]; then
    echo "  ❌ FATAL: WORKERS=$NEW_WORKERS>1 但 PROGRESS_BUS_READY!=1"
    compose stop "omnirank-$INACTIVE"
    exit 1
fi
if [ "$REAL_WORKERS" != "$NEW_WORKERS" ]; then
    echo "  ❌ FATAL: 声明 WORKERS=$NEW_WORKERS，真实 worker=$REAL_WORKERS"
    compose stop "omnirank-$INACTIVE"
    exit 1
fi
echo "  ✅ W4 web gate 通过"

# 3.45. 资金 binary capability + expansion gate。
# activation 绝不能在这里执行：必须等旧 binary 停止、同 image 热回滚槽位就绪后。
echo "[3.45/8] 代理进货快照能力与 expansion gate..."
NEW_CAPABILITY=$(container_snapshot_capability "omnirank-$INACTIVE")
if [ "$NEW_CAPABILITY" != "$SNAPSHOT_CAPABILITY" ]; then
    echo "  ❌ FATAL: 候选 binary capability=$NEW_CAPABILITY，要求 $SNAPSHOT_CAPABILITY"
    compose stop "omnirank-$INACTIVE"
    exit 1
fi
echo "  ✅ 候选 binary capability=$NEW_CAPABILITY"

CANDIDATE_IMAGE_ID=$(docker inspect --format='{{.Image}}' "omnirank-$INACTIVE")
CONTAINER_RELEASE_SHA=$(docker exec "omnirank-$INACTIVE" cat /app/RELEASE_SHA 2>/dev/null | tr -d '\r\n')
IMAGE_RELEASE_SHA=$(docker image inspect "$CANDIDATE_IMAGE_ID" \
    --format='{{index .Config.Labels "org.opencontainers.image.revision"}}')
PREVIOUS_IMAGE_ID=$(docker inspect --format='{{.Image}}' "omnirank-$ACTIVE")
ROLLBACK_IMAGE_REF=$(docker inspect --format='{{.Config.Image}}' "omnirank-$ACTIVE")
if [ -z "$CANDIDATE_IMAGE_ID" ] || [ -z "$PREVIOUS_IMAGE_ID" ] || [ -z "$ROLLBACK_IMAGE_REF" ]; then
    echo "  ❌ FATAL: 无法锁定 candidate/rollback image identity"
    exit 1
fi
if [ "$CANDIDATE_IMAGE_ID" != "$BUILT_IMAGE_ID" ] \
   || [ "$CONTAINER_RELEASE_SHA" != "$DEPLOY_SHA" ] \
   || [ "$IMAGE_RELEASE_SHA" != "$DEPLOY_SHA" ]; then
    echo "  ❌ FATAL: candidate image/container release identity 不一致"
    echo "  built=$BUILT_IMAGE_ID candidate=$CANDIDATE_IMAGE_ID file=$CONTAINER_RELEASE_SHA label=$IMAGE_RELEASE_SHA"
    exit 1
fi
docker tag "$PREVIOUS_IMAGE_ID" "omnirank-rollback-pre-$(date +%Y%m%d-%H%M%S)"

PREVIOUS_CAPABILITY=$(image_snapshot_capability "$PREVIOUS_IMAGE_ID") || {
    echo "  ❌ FATAL: 无法从旧 active image 严格读取进货快照 capability"
    exit 1
}
CANDIDATE_IMAGE_CAPABILITY=$(image_snapshot_capability "$CANDIDATE_IMAGE_ID") || {
    echo "  ❌ FATAL: 无法从 candidate image 严格读取进货快照 capability"
    exit 1
}
if [ "$CANDIDATE_IMAGE_CAPABILITY" != "$SNAPSHOT_CAPABILITY" ]; then
    echo "  ❌ FATAL: candidate image capability=$CANDIDATE_IMAGE_CAPABILITY"
    exit 1
fi

CUTOVER_STATE=$(snapshot_cutover_state) || {
    echo "  ❌ FATAL: 无法严格读取 inventory snapshot cutover 状态"
    exit 1
}
if [ "$CUTOVER_STATE" = "1" ]; then
    echo "  cutover 已激活，只做 schema/fence 严格验证"
    require_inventory_snapshot_schema || exit 1
else
    echo "  cutover 未激活；prestart expansion 已完成（仍不写 marker）"
    require_inventory_snapshot_schema || exit 1
    CUTOVER_STATE=$(snapshot_cutover_state) || {
        echo "  ❌ FATAL: expansion 后无法严格读取 cutover 状态"
        exit 1
    }
    if [ "$CUTOVER_STATE" = "1" ]; then
        echo "  ❌ FATAL: expansion 不得写 cutover marker"
        exit 1
    fi
fi

# 候选切流前固化 Phase A 双槽位证据。rollback 只有在证据新鲜、
# image/capability 完全匹配且零未结算 gen2 订单时才能切回 legacy binary。
record_inventory_snapshot_phase_a_rollback
echo "  ✅ Phase A rollback 证据已固化 · old=$PREVIOUS_CAPABILITY candidate=$CANDIDATE_IMAGE_CAPABILITY"

# 3.5. Graceful SPA chunks 合并(2026-05-11 加 · 真 0 用户感知)
# CTO-2026-05-11 用户感知 0 关键 ·
#   旧问题:user 浏览器加载的老 bundle 引用老 chunk hash(如 index-D0_Tqyth.js)
#          deploy 后新 build 没这些 hash · 用户切 tab/lazy load 时 fetch 404
#          → frontend 显示"Failed to fetch dynamically imported module"+ "系统已更新"提示
#          → 用户被打断
#   修法:nginx swap 前 · 从老 active 容器把 assets/ 合并进新 active(只补缺失 · 不覆盖)
#          → 老 bundle 仍能 fetch 老 chunk · 新 bundle 用新 chunk · 两全
#   候选容器每次都从 immutable image 重建，只会合并上一代 assets，
#   因此不需要在容器内按 mtime 清理。镜像层保留的构建时间可能早于部署
#   60min 以上；按 mtime 删除会把当前 index.html 引用的整套当前 chunks 误删。
#   合并前固化候选 dist 哈希清单，合并后必须逐文件完整通过；老 chunk
#   只补缺失且不覆盖候选文件。
echo "[3.5/6] graceful SPA chunks 合并 (老 bundle 用户无感过渡)..."
TMP_OLD_ASSETS="/tmp/graceful-chunks-$$"
mkdir -p "$TMP_OLD_ASSETS"
docker cp "omnirank-$ACTIVE:/app/frontend/dist/assets" "$TMP_OLD_ASSETS/" 2>/dev/null || true
if [ -d "$TMP_OLD_ASSETS/assets" ]; then
    OLD_COUNT=$(find "$TMP_OLD_ASSETS/assets" -mindepth 1 -maxdepth 1 -type f | wc -l)
else
    OLD_COUNT=0
fi
if [ "$OLD_COUNT" -gt 0 ]; then
    docker exec "omnirank-$INACTIVE" mkdir -p /tmp/old-merge-in 2>/dev/null || true
    docker cp "$TMP_OLD_ASSETS/assets/." "omnirank-$INACTIVE:/tmp/old-merge-in/" 2>/dev/null || true
    if ! docker exec "omnirank-$INACTIVE" bash -c '
        set -e
        python /app/scripts/merge_frontend_assets.py \
          /app/frontend/dist /tmp/old-merge-in
        bash /app/scripts/verify_frontend_deploy.sh /app/frontend/dist >/dev/null
        rm -rf /tmp/old-merge-in
    ' 2>&1 | tail -3; then
        echo "  ❌ FATAL: graceful SPA chunks 合并破坏 candidate build；Nginx 未切流"
        rm -rf "$TMP_OLD_ASSETS"
        exit 1
    fi
else
    echo "  老容器无 assets 可合并(首次 deploy 或老容器异常)"
fi
rm -rf "$TMP_OLD_ASSETS"

# 3.6. W4 cron 蓝绿接管必须在 web 切流前完成。cron 与同色 web 共用同一审核
# image；任何 ROLE/image/leader 事实不成立都保持 Nginx 指向旧栈。
_verify_cron_leader_redis() {
    docker exec "$1" python -c \
        "import sys; from services.sched_control import verify_is_current_leader; sys.exit(0 if verify_is_current_leader() else 1)" \
        2>/dev/null
}

_wait_cron_leader() {
    local container="$1" attempts="$2" i
    for i in $(seq 1 "$attempts"); do
        if _verify_cron_leader_redis "$container"; then
            return 0
        fi
        sleep 5
    done
    return 1
}

_wait_cron_runtime_ready() {
    local container="$1" attempts="$2" i
    for i in $(seq 1 "$attempts"); do
        if docker exec "$container" \
            curl -fsS --max-time 2 http://localhost:8000/docs >/dev/null 2>&1; then
            return 0
        fi
        sleep 5
    done
    return 1
}

restore_legacy_web_after_failed_cron_bootstrap() {
    echo "  恢复旧一体化 web/cron 与原 Nginx 配置..."
    docker stop --time=20 "omnirank-cron-$INACTIVE" >/dev/null 2>&1 || true
    docker start "omnirank-$ACTIVE" >/dev/null 2>&1 || true
    wait_container_ready "omnirank-$ACTIVE" "$ACTIVE_PORT" 30 || true
    if [ -f "$NGINX_CONF.maintenance-tmp" ]; then
        mv "$NGINX_CONF.maintenance-tmp" "$NGINX_CONF"
        if ! nginx -t 2>/dev/null || ! nginx -s reload; then
            echo "  🔴 原 Nginx 配置未能 reload，旧 web 已恢复；需立即人工介入"
        fi
    fi
}

restore_w4_stack_after_failed_switch() {
    echo "  恢复原 W4 cron 与原 Nginx 配置..."
    docker stop --time=20 "omnirank-cron-$INACTIVE" >/dev/null 2>&1 || true
    if [ "$OLD_CRON_WAS_RUNNING" = "true" ]; then
        docker start "omnirank-cron-$ACTIVE" >/dev/null 2>&1 || true
        _wait_cron_leader "omnirank-cron-$ACTIVE" 24 || \
            echo "  🔴 旧 cron 未恢复 leader，需立即人工介入"
    fi
    if [ -f "$NGINX_CONF.deploy-tmp" ]; then
        mv "$NGINX_CONF.deploy-tmp" "$NGINX_CONF"
        if ! nginx -t 2>/dev/null || ! nginx -s reload; then
            echo "  🔴 原 Nginx 配置未能 reload；需立即人工介入"
        fi
    fi
}

echo "[3.6/8] W4 cron 接管（切流前）..."
if ! compose --profile "cron-$INACTIVE" up -d --no-deps "omnirank-cron-$INACTIVE"; then
    echo "  ❌ FATAL: candidate cron 启动失败；Nginx 未修改"
    exit 1
fi
assert_infra_identity_unchanged "candidate-cron-up"

CANDIDATE_CRON_IMAGE_ID=$(docker inspect --format='{{.Image}}' "omnirank-cron-$INACTIVE" 2>/dev/null || true)
CANDIDATE_CRON_ROLE=$(docker exec "omnirank-cron-$INACTIVE" printenv ROLE 2>/dev/null | tr -d '\r\n' || true)
CANDIDATE_CRON_WORKERS=$(docker exec "omnirank-cron-$INACTIVE" printenv WORKERS 2>/dev/null | tr -d '\r\n' || true)
if [ "$CANDIDATE_CRON_IMAGE_ID" != "$CANDIDATE_IMAGE_ID" ] \
   || [ "$CANDIDATE_CRON_ROLE" != "cron" ] \
   || [ "$CANDIDATE_CRON_WORKERS" != "1" ]; then
    echo "  ❌ FATAL: cron image/ROLE/WORKERS 不匹配审核候选"
    echo "  web_image=$CANDIDATE_IMAGE_ID cron_image=$CANDIDATE_CRON_IMAGE_ID role=$CANDIDATE_CRON_ROLE workers=$CANDIDATE_CRON_WORKERS"
    docker stop --time=20 "omnirank-cron-$INACTIVE" >/dev/null 2>&1 || true
    exit 1
fi

# 冷镜像首次 import server.py 可能超过 leader 接管窗口。先让候选在旧 leader
# 仍持锁时完成 runtime bootstrap（此时只能 follower，不触发 cron），再停旧 cron
# 并开始 120s 换主计时，避免把启动耗时误判为 Redis leader 故障。
if ! _wait_cron_runtime_ready "omnirank-cron-$INACTIVE" 60; then
    echo "  ❌ FATAL: candidate cron 300s 未完成 runtime bootstrap；Nginx 不切候选"
    docker stop --time=20 "omnirank-cron-$INACTIVE" >/dev/null 2>&1 || true
    exit 1
fi
echo "  ✅ candidate cron runtime ready（旧 leader 尚未停止）"

LEGACY_TOPOLOGY_BOOTSTRAP=false
OLD_CRON_WAS_RUNNING=false
ACTIVE_WEB_ROLE=$(docker exec "omnirank-$ACTIVE" printenv ROLE 2>/dev/null | tr -d '\r\n' || true)
ACTIVE_CRON_RUNNING=$(docker inspect --format='{{.State.Running}}' "omnirank-cron-$ACTIVE" 2>/dev/null || true)
# A prior hotfix image may have lost ROLE=web while the same-color independent cron
# remains online. The running cron is authoritative evidence that W4 topology exists.
if [ "$ACTIVE_WEB_ROLE" = "web" ] || [ "$ACTIVE_CRON_RUNNING" = "true" ]; then
    if [ "$ACTIVE_CRON_RUNNING" = "true" ]; then
        OLD_CRON_WAS_RUNNING=true
        docker stop --time=30 "omnirank-cron-$ACTIVE"
        echo "  已停旧 W4 cron($ACTIVE)，等待 candidate cron 接管"
    else
        echo "  旧 W4 cron 未运行；candidate cron 必须直接证明 leader"
    fi
else
    # 首次从旧一体化 web/cron 迁入 W4。新 LeaderLock 与旧 scheduler:lock 互斥，
    # 所以不能在旧 web 仍服务时证明新 cron leader。先把入口置 503，再排空、
    # 停旧 writer/scheduler；失败则恢复旧容器和原 Nginx。
    LEGACY_TOPOLOGY_BOOTSTRAP=true
    cp "$NGINX_CONF" "$NGINX_CONF.maintenance-tmp"
    sed -i "s|proxy_pass http://127.0.0.1:$ACTIVE_PORT;|return 503;|" "$NGINX_CONF"
    if ! grep -q "return 503;" "$NGINX_CONF" || ! nginx -t 2>/dev/null; then
        mv "$NGINX_CONF.maintenance-tmp" "$NGINX_CONF"
        docker stop --time=20 "omnirank-cron-$INACTIVE" >/dev/null 2>&1 || true
        echo "  ❌ FATAL: 首次 W4 维护闸 Nginx 校验失败"
        exit 1
    fi
    if ! nginx -s reload; then
        restore_legacy_web_after_failed_cron_bootstrap
        echo "  ❌ FATAL: 首次 W4 维护闸 reload 失败，已尝试恢复原栈"
        exit 1
    fi
    echo "  首次 W4 拓扑迁移：入口已 fail-closed 503，开始排空旧一体化 web/cron"
    if ! wait_active_web_drain; then
        restore_legacy_web_after_failed_cron_bootstrap
        exit 1
    fi
    docker stop --time=30 "omnirank-$ACTIVE"
fi

if ! _wait_cron_leader "omnirank-cron-$INACTIVE" 24; then
    echo "  ❌ FATAL: candidate cron 120s 未成为 Redis leader；Nginx 不切候选"
    docker stop --time=20 "omnirank-cron-$INACTIVE" >/dev/null 2>&1 || true
    if [ "$LEGACY_TOPOLOGY_BOOTSTRAP" = "true" ]; then
        restore_legacy_web_after_failed_cron_bootstrap
    elif [ "$OLD_CRON_WAS_RUNNING" = "true" ]; then
        docker start "omnirank-cron-$ACTIVE" >/dev/null 2>&1 || true
        _wait_cron_leader "omnirank-cron-$ACTIVE" 24 || \
            echo "  🔴 旧 cron 未恢复 leader，需立即人工介入"
    fi
    exit 1
fi
echo "  ✅ candidate cron 是当前 Redis leader；同 image=$CANDIDATE_CRON_IMAGE_ID"

# [WO_264 §3-②] 切槽前扫「对外服务的产物」(存档 ∪ 本槽 dist)。理由与实现见该门抬头。
_SAG="$PROJECT_DIR/scripts/gate_served_assets_scan.sh"
if [ -f "$_SAG" ]; then
    echo "[3.9/6] 切槽前扫对外服务的产物(WO_264 §3-②)..."
    bash "$_SAG" --repo "$PROJECT_DIR" --container "omnirank-$INACTIVE"; _SAGRC=$?
    case "$_SAGRC" in
        0) : ;;
        1) echo "  🔴 产物含白名单外的真实客户数据形状 —— **停在切槽前**"; exit 1 ;;
        3) echo "  ⚠️  【没跑成·灰】不拦车,但这一班**没有扫过** —— 记进签字条" ;;
        *) echo "  🔴 未知退出码 $_SAGRC —— 当红,停在切槽前"; exit 1 ;;
    esac
else
    echo "  ⚠️  [3.9/6] 跳过:$_SAG 不在(WO_264 尚未随包上线)"
fi

# 4. 切换 Nginx 流量（先备份，语法失败必须恢复）
echo "[4/6] 切换流量到 $INACTIVE ($INACTIVE_PORT)..."
if [ "$LEGACY_TOPOLOGY_BOOTSTRAP" = "true" ]; then
    sed -i "s|return 503;|proxy_pass http://127.0.0.1:$INACTIVE_PORT;|" "$NGINX_CONF"
else
    cp "$NGINX_CONF" "$NGINX_CONF.deploy-tmp"
    sed -i "s|proxy_pass http://127.0.0.1:$ACTIVE_PORT|proxy_pass http://127.0.0.1:$INACTIVE_PORT|" "$NGINX_CONF"
fi

# 验证 Nginx 配置语法
if ! grep -q "proxy_pass http://127.0.0.1:$INACTIVE_PORT" "$NGINX_CONF" \
   || grep -q "return 503;" "$NGINX_CONF" \
   || ! nginx -t 2>/dev/null; then
    if [ "$LEGACY_TOPOLOGY_BOOTSTRAP" = "true" ]; then
        restore_legacy_web_after_failed_cron_bootstrap
    else
        restore_w4_stack_after_failed_switch
    fi
    echo "  错误: Nginx 配置语法错误，已恢复原配置，中止切换"
    exit 1
fi
if ! nginx -s reload; then
    if [ "$LEGACY_TOPOLOGY_BOOTSTRAP" = "true" ]; then
        restore_legacy_web_after_failed_cron_bootstrap
    else
        restore_w4_stack_after_failed_switch
    fi
    echo "  ❌ FATAL: Nginx reload 失败，已尝试恢复原栈"
    exit 1
fi
rm -f "$NGINX_CONF.deploy-tmp"
rm -f "$NGINX_CONF.maintenance-tmp"
echo "  流量已切换到 $INACTIVE ($INACTIVE_PORT)"

# 5/6. 常规 W4 蓝绿在切流后排空旧 web；首次拓扑迁移已在维护闸内排空并停止。
if [ "$LEGACY_TOPOLOGY_BOOTSTRAP" != "true" ]; then
    echo "[5/8] 等待旧 $ACTIVE web 长任务完成..."
    wait_active_web_drain || exit 1
    sleep 10
    echo "[6/8] 停止旧 $ACTIVE web (graceful)..."
    docker stop --time=30 "omnirank-$ACTIVE"
else
    echo "[5-6/8] 首次 W4 拓扑迁移的旧一体化 web/cron 已在切流前排空并停止"
fi

# 7. 同 image 热回滚槽位。
# 先用候选 image 重建旧槽位并通过 health/capability/image identity 三重 gate，
# 再允许 activation。这样 marker 生效后的任一时刻都不存在旧 binary 回滚位。
echo "[7/8] 重建同 image 热回滚槽位 omnirank-$ACTIVE..."
docker tag "$CANDIDATE_IMAGE_ID" "$ROLLBACK_IMAGE_REF"
if [ "$ACTIVE" = "green" ]; then
    compose --profile green --profile cron-green up -d --no-deps --force-recreate --no-build \
        omnirank-green omnirank-cron-green
else
    compose --profile cron-blue up -d --no-deps --force-recreate --no-build \
        omnirank-blue omnirank-cron-blue
fi
assert_infra_identity_unchanged "hot-rollback-rebuild"

if ! wait_container_ready "omnirank-$ACTIVE" "$ACTIVE_PORT" 60; then
    echo "  ❌ FATAL: 热回滚槽位未在 300s 内就绪；activation 未执行"
    echo "  尝试恢复 pre-cutover 旧 image 作为热备（marker 仍不存在）"
    docker tag "$PREVIOUS_IMAGE_ID" "$ROLLBACK_IMAGE_REF" || true
    if [ "$ACTIVE" = "green" ]; then
        compose --profile green up -d --no-deps --force-recreate --no-build omnirank-green || true
    else
        compose up -d --no-deps --force-recreate --no-build omnirank-blue || true
    fi
    assert_infra_identity_unchanged "hot-rollback-fallback"
    exit 1
fi

HOT_CAPABILITY=$(container_snapshot_capability "omnirank-$ACTIVE")
HOT_IMAGE_ID=$(docker inspect --format='{{.Image}}' "omnirank-$ACTIVE")
HOT_CRON_IMAGE_ID=$(docker inspect --format='{{.Image}}' "omnirank-cron-$ACTIVE" 2>/dev/null || true)
HOT_CRON_ROLE=$(docker exec "omnirank-cron-$ACTIVE" printenv ROLE 2>/dev/null | tr -d '\r\n' || true)
HOT_CRON_WORKERS=$(docker exec "omnirank-cron-$ACTIVE" printenv WORKERS 2>/dev/null | tr -d '\r\n' || true)
if [ "$HOT_CAPABILITY" != "$SNAPSHOT_CAPABILITY" ] \
   || [ "$HOT_IMAGE_ID" != "$CANDIDATE_IMAGE_ID" ] \
   || [ "$HOT_CRON_IMAGE_ID" != "$CANDIDATE_IMAGE_ID" ] \
   || [ "$HOT_CRON_ROLE" != "cron" ] \
   || [ "$HOT_CRON_WORKERS" != "1" ]; then
    echo "  ❌ FATAL: 热回滚能力/image 不匹配；activation 未执行"
    echo "  hot_capability=$HOT_CAPABILITY hot_image=$HOT_IMAGE_ID hot_cron_image=$HOT_CRON_IMAGE_ID candidate_image=$CANDIDATE_IMAGE_ID"
    exit 1
fi
echo "  ✅ web+cron 热回滚已就绪 · capability=$HOT_CAPABILITY · image=$HOT_IMAGE_ID"

# 长任务排空和热备重建可能持续数分钟；证据入库前必须重新证明
# 当前承接流量的 active 仍就绪、具备能力且与 hot rollback 同 image。
if ! wait_container_ready "omnirank-$INACTIVE" "$INACTIVE_PORT" 1; then
    echo "  ❌ FATAL: 当前 active 在 activation 前已不就绪；marker 未写入"
    exit 1
fi
ACTIVE_SNAPSHOT_CAPABILITY=$(container_snapshot_capability "omnirank-$INACTIVE")
ACTIVE_SNAPSHOT_IMAGE_ID=$(docker inspect --format='{{.Image}}' "omnirank-$INACTIVE")
if [ "$ACTIVE_SNAPSHOT_CAPABILITY" != "$SNAPSHOT_CAPABILITY" ] \
   || [ "$ACTIVE_SNAPSHOT_IMAGE_ID" != "$CANDIDATE_IMAGE_ID" ] \
   || [ "$ACTIVE_SNAPSHOT_IMAGE_ID" != "$HOT_IMAGE_ID" ]; then
    echo "  ❌ FATAL: active/hot capability 或 image 身份不一致；marker 未写入"
    exit 1
fi
echo "  ✅ active + hot rollback 双槽位即时复核通过"
record_inventory_snapshot_hot_rollback_ready

# 8. Phase B activation。此时 active + hot rollback 是同一快照 binary，旧 writer 已停止。
echo "[8/8] inventory snapshot cutover activation..."
CUTOVER_STATE=$(snapshot_cutover_state) || {
    echo "  ❌ FATAL: activation 前无法严格读取 cutover 状态"
    exit 1
}
if [ "$CUTOVER_STATE" = "1" ]; then
    echo "  marker 已存在，跳过写入并复核 fence"
else
    activate_inventory_snapshot_cutover
fi
require_inventory_snapshot_schema || exit 1
CUTOVER_STATE=$(snapshot_cutover_state) || {
    echo "  ❌ FATAL: activation 后无法严格读取 cutover 状态"
    exit 1
}
if [ "$CUTOVER_STATE" != "1" ]; then
    echo "  ❌ FATAL: activation 完成后 marker 仍不存在"
    exit 1
fi
GENERATION_CONSTRAINT=$(db_scalar "SELECT COUNT(*) FROM pg_constraint WHERE conrelid='recharge_orders'::regclass AND conname='check_agent_inventory_writer_generation_required' AND convalidated")
if [ "$GENERATION_CONSTRAINT" != "1" ]; then
    echo "  ❌ FATAL: writer generation constraint 未验证"
    exit 1
fi
ACTIVE_WRITER_GENERATION=$(db_scalar "SELECT COALESCE(pg_sequence_last_value('agent_inventory_writer_generation_fence_seq'::regclass),0)")
if [ "$ACTIVE_WRITER_GENERATION" != "2" ]; then
    echo "  ❌ FATAL: writer generation fence=$ACTIVE_WRITER_GENERATION，要求 2"
    exit 1
fi
INVALID_LEGACY=$(db_scalar "SELECT COUNT(*) FROM recharge_orders WHERE order_type='agent_inventory_purchase' AND payment_status='pending' AND pricing_snapshot_jsonb IS NULL AND NOT agent_inventory_legacy_eligible")
if [ "$INVALID_LEGACY" != "0" ]; then
    echo "  ❌ FATAL: activation 后存在 $INVALID_LEGACY 笔 allowlist 外空快照订单"
    exit 1
fi
echo "  ✅ activation 完成；allowlist 外空快照订单=0；同 image 热回滚保持运行"

# ============================================
# [Deploy 2026-05-25 · WO-A ① 重写 2026-08-20] 小榜知识 release
#   一步做两件、顺序不能反:**先 seed 同步 → 后重建索引**
#   (build_faq_chunks 从 faq_items 表取数;seed 没同步就重建 = 拿旧答案建新 release)
#
# 🔴 上一版的坏法(留作病历 · 别再写回去):
#     KB_LOG=$(docker exec ... python -m tools.xiaobang_kb_indexer ... 2>&1 | tail -8 || true)
#     if echo "$KB_LOG" | grep -q "索引完成"; then
#   · `|| true` 把**退出码**整个吞掉 —— 成功与失败在这里没有区别;
#   · 判成功改看 tail -8 里有没有「索引完成」。而 indexer 收尾按 count_chunks()
#     逐类型打印,生产上有 doc/faq/preset + 5 类 sys_* ⇒「索引完成」那一行
#     **排在末 8 行之外**被 tail -8 切掉 ⇒ 重建成功也报「⚠️ 失败」。
#     于是没人再信这一步,每次改成手工重建 —— 这才是「每次部署要人工重建」的真因,
#     不是「接线不存在」(接线 2026-05-25 就在这儿)。
#   · 失败只落在这份部署日志里,下一次切流就冲掉了 —— 零告警面。
#
# 现在:**退出码即判据** + 两种失败形态分别处理 + 落 ai_ops_alerts。
# 失败仍**不阻断** deploy:重建是单事务 clear-then-insert,失败时旧 release
# 原样在服务(fail-closed);拿一次内容重建把整次发布拦下来是本末倒置。
# ============================================
echo ""
echo "[Post-Deploy] 小榜知识 release(先 seed 同步 → 后重建索引)..."
KB_RC=0
KB_OUT=$(docker exec "omnirank-$INACTIVE" python -m services.kb_release_ops \
    --release-sha "$DEPLOY_SHA" 2>&1) || KB_RC=$?
if [ "$KB_RC" = "0" ]; then
    echo "  ✅ 小榜知识 release 完成"
    echo "$KB_OUT" | tail -3 | sed 's/^/    /'
elif [ "$KB_RC" = "20" ] || [ "$KB_RC" = "21" ]; then
    # 形态一:步骤**跑起来了**,自己 fail-closed(被调方抛异常 / 返回非零)。
    #   容器内已写 ai_ops_alerts(rule_key=kb_release_step_failed),这里只复述。
    echo "  ⚠️ 小榜知识 release 失败 rc=$KB_RC(旧索引继续服务 · 容器内已落告警)"
    echo "$KB_OUT" | tail -6 | sed 's/^/    /'
else
    # 形态二:步骤**压根没跑起来**(docker exec 125/126/127 · 镜像缺模块 · OOM…)。
    #   容器内没有任何人来得及告警 ⇒ 这里补一条,否则这次失败只剩这份会被冲掉的日志。
    echo "  ⚠️ 小榜知识 release **未能启动** rc=$KB_RC —— 补一条告警"
    echo "$KB_OUT" | tail -6 | sed 's/^/    /'
    docker exec "omnirank-$INACTIVE" python -m services.kb_release_ops \
        --report-failure --shape launch_failed --release-sha "$DEPLOY_SHA" \
        --detail "deploy-blue-green.sh: kb_release_ops 未能启动 rc=$KB_RC" \
        >/dev/null 2>&1 || echo "  🔴 补告警也失败 —— 人工查 omnirank-$INACTIVE 的 python 环境"
fi

echo ""
# ===== BEGIN rollback-capability-summary (锁 harness 按此标记整块抽取 · 勿删标记) =====
echo "========================================="
# 🔴 任一红态必须排在摘要**第一行**:部署日志会被下一次切流冲掉,
#    操作者往往只扫一眼尾部 —— 排在后面等于没报。
case "$ROLLBACK_PROBE_STATE" in
    ""|OK|NotApplicable) ;;
    *) echo "  $ROLLBACK_PROBE_HEADLINE" ;;
esac
echo "  部署完成"
echo "  活跃: $INACTIVE ($INACTIVE_PORT)"
echo "  热回滚: $ACTIVE ($ACTIVE_PORT) · 同 image / 同快照能力 / 保持运行"
# 绿态照常按顺序打,不抢摘要头部。
case "$ROLLBACK_PROBE_STATE" in
    OK|NotApplicable) echo "  回滚能力探针: $ROLLBACK_PROBE_HEADLINE" ;;
esac
echo ""
echo "  热切换: DEPLOY_SHA=$DEPLOY_SHA DEPLOY_RELEASE_WORKTREE=$PROJECT_DIR REVIEWED_ROLLBACK_SCRIPT_SHA256=$REVIEWED_ROLLBACK_SCRIPT_SHA256 bash $PROJECT_DIR/scripts/rollback-blue-green.sh"
echo "========================================="
# ===== END rollback-capability-summary =====

# ============================================
# Post-Deploy 自动清理(2026-05-08 加 · 防磁盘满)
# 保留最新 3 staging-* + 3 rollback-pre-*(刚 deploy 的 + 上 2 版备份)
# 失败不阻断脚本(deploy 已成功 · 清理是 bonus)· || true 兜底
# ============================================
echo ""
echo "[Post-Deploy] 自动清理旧镜像(保留最新 3 staging + 3 rollback-pre)..."
DISK_BEFORE=$(df -h / | tail -1 | awk '{print $4" free"}')

cleanup_pattern() {
    local pattern="$1"
    docker images --filter "reference=$pattern" --format '{{.CreatedAt}}|{{.Repository}}:{{.Tag}}'         | sort -r         | tail -n +4         | awk -F'|' '{print $2}'         | while read -r tag; do
            [ -n "$tag" ] && docker rmi "$tag" 2>&1 | grep -E '^(Untagged|Deleted):' | head -2 || true
        done || true
}

cleanup_pattern 'omnirank-staging-*' || true
cleanup_pattern 'omnirank-rollback-pre-*' || true
docker builder prune -f >/dev/null 2>&1 || true

# 只清 dangling image。`-a` 会删掉未被容器引用但刻意保留的 rollback-pre-* tag，
# 与上方“保留最新 3 个”契约冲突。
docker image prune -f >/dev/null 2>&1 || true

DISK_AFTER=$(df -h / | tail -1 | awk '{print $4" free"}')
STAGING_COUNT=$(docker images --filter 'reference=omnirank-staging-*' --format '{{.Repository}}' | wc -l)
ROLLBACK_COUNT=$(docker images --filter 'reference=omnirank-rollback-pre-*' --format '{{.Repository}}' | wc -l)
echo "  磁盘: $DISK_BEFORE → $DISK_AFTER · staging-*: $STAGING_COUNT · rollback-pre-*: $ROLLBACK_COUNT"
