#!/bin/bash
# ============================================
# OmniRank AI 容器启动脚本
# 启动 Nginx（前端）+ 等待数据库 + uvicorn（后端）
# ============================================

set -e

echo "========================================="
echo "  OmniRank AI 启动中..."
echo "========================================="

# [WORKERS=4 · SPEC §1/D1/D4] 进程角色分派:web(默认·多 worker 服务) / cron(单 worker 跑调度) /
#   prestart(一次性迁移 + 重启恢复 sweep,fleet 服务前单飞,跑完退出)。
ROLE=${ROLE:-web}
echo "  ROLE: $ROLE"

# 初始化数据目录
mkdir -p /app/data /app/output /app/cache /app/logs /app/config

# ---- 等待 PostgreSQL 就绪 ----
echo "[1/3] 等待数据库就绪..."
MAX_RETRIES=30
RETRY=0
until python -c "
import psycopg2, os
psycopg2.connect(os.environ['DATABASE_URL'])
print('  数据库连接成功')
" 2>/dev/null; do
    RETRY=$((RETRY + 1))
    if [ $RETRY -ge $MAX_RETRIES ]; then
        echo "  数据库连接超时（${MAX_RETRIES}次重试），退出"
        exit 1
    fi
    echo "  等待数据库... ($RETRY/$MAX_RETRIES)"
    sleep 2
done

# ---- prestart:一次性迁移 + 重启恢复 sweep,跑完退出(不起 nginx/uvicorn)----
# [WORKERS=4 · D4] 恢复类 sweep(running→failed / writing→pending / zombie round)必须在 fleet
#   服务前单飞一次;若跟随每个 web/cron 容器启动跑,蓝绿重叠期会误标另一色的活跃任务失败。
if [ "$ROLE" = "prestart" ]; then
    echo "[prestart] 运行一次性迁移 + 重启恢复 sweep..."
    python -m scripts.prestart
    echo "[prestart] 完成 · 退出"
    exit 0
fi

# ---- [WO_NO_SILENT_RELOAD_DIRTY_GUARD 2026-08-16 ③] 归档本版 /assets ----
# 目的:部署替换 dist 之后,**还停在老页面的用户**懒加载旧 chunk 时不再 404。
#   vite 是 content-hash 命名 ⇒ 新旧资产天然可并存,归档只增不改。
#   nginx 的 /assets/ 未命中时回落到这里(location @assets_archive)。
#
# 🔴 cp -n(不覆盖):同名文件必然内容相同(hash 即内容),覆盖只会刷新 mtime,
#    而下面的保留期正是按 mtime 算的 —— 覆盖会让老资产永不过期。
# 🔴 保留 14 天后清理:不清会无限增长。14 天 >> 用户单次会话时长,
#    也 >> 一天四班车的节奏;真有人挂了两周以上的页面,他会走 banner 升级那条路。
# 🔴 整段 fail-open:归档失败**不许**拦住服务启动 —— 它是体验优化,不是启动前置。
# 🔴 [R3 · Codex 2026-08-17 抓到] 上一版**不是**真 fail-open:
#   本脚本跑在 `set -e` 下,`mkdir/cp/find` 里任何一个非零退出都会**直接掐掉容器启动**
#   —— 与它自己的注释("整段 fail-open")正相反。
#   引爆条件:宿主机 assets_archive 目录不可写(权限/只读挂载/磁盘满)
#   ⇒ web 与 cron **都起不来**,一个体验优化把整个服务拖下水。
#   ⇒ 整段包进子 shell 并显式吞掉退出码:归档失败只记警告,启动照常。
if [ "$ROLE" != "prestart" ]; then
    ARCHIVE_DIR="${ASSETS_ARCHIVE_DIR:-/app/assets_archive/assets}"
    if [ -d /app/frontend/dist/assets ]; then
        if (
            set +e
            mkdir -p "$ARCHIVE_DIR" 2>/dev/null || exit 1
            # 可写性显式探一下:mkdir 对已存在的只读目录会返 0,光看它不够
            touch "$ARCHIVE_DIR/.write_probe" 2>/dev/null || exit 1
            rm -f "$ARCHIVE_DIR/.write_probe" 2>/dev/null
            cp -rn /app/frontend/dist/assets/. "$ARCHIVE_DIR"/ 2>/dev/null
            find "$ARCHIVE_DIR" -type f -mtime +14 -delete 2>/dev/null
            exit 0
        ); then
            echo "[assets-archive] 已归档 $(find "$ARCHIVE_DIR" -type f 2>/dev/null | wc -l) 个资产文件(保留 14 天)"
        else
            echo "[assets-archive] ⚠️ 归档目录不可写($ARCHIVE_DIR)· 已跳过归档,服务照常启动。" >&2
            echo "[assets-archive] ⚠️ 影响:部署后停在老页面的用户懒加载旧 chunk 会 404 →" >&2
            echo "[assets-archive] ⚠️   走守卫那条路(dirty 出 banner / 非 dirty 静默刷),不是数据风险。" >&2
        fi
    else
        echo "[assets-archive] 跳过:/app/frontend/dist/assets 不存在"
    fi
fi

# ---- 等待 Redis 就绪 ----
# 镜像已安装 Python redis 客户端，但不包含 redis-cli。统一按 REDIS_URL 探活，
# 避免 Redis 正常时仍把 WORKERS>1 误降级为 1。
redis_ready() {
  python - <<'PY'
import os
import redis

client = redis.Redis.from_url(
    os.getenv("REDIS_URL", "redis://redis:6379/0"),
    socket_connect_timeout=2,
    socket_timeout=2,
)
raise SystemExit(0 if client.ping() else 1)
PY
}

echo "[1.5/3] 等待 Redis 就绪..."
RETRY=0
until redis_ready 2>/dev/null; do
    RETRY=$((RETRY + 1))
    if [ $RETRY -ge 10 ]; then
        echo "  Redis 连接超时，将使用内存缓存降级模式"
        break
    fi
    echo "  等待 Redis... ($RETRY/10)"
    sleep 1
done

# ---- 启动 Nginx ----
echo "[2/3] 启动 Nginx..."
nginx

# 捕获信号，确保 Nginx 和 uvicorn 都能优雅退出
trap "nginx -s quit; kill $UVICORN_PID 2>/dev/null; wait $UVICORN_PID; exit 0" SIGTERM SIGINT

# ---- 启动后端 API ----
echo "[3/3] 启动后端服务..."
# [WORKERS=4 · SPEC §5/D4] WORKERS 规则(取代旧"强制 1"):
#   - ROLE=cron:强制 1(单进程跑 scheduler · leader 选主处理蓝绿重叠)。
#   - ROLE=web :允许 >1,但 D4 解锁守卫 —— WORKERS>1 需 PROGRESS_BUS_READY=1 且 Redis 可达
#     (跨 worker 进度/状态全靠 Redis 总线;任一不满足回退 1,防退化成"多 worker 不共享 state")。
if [ "$ROLE" = "cron" ]; then
  WORKERS=1
  echo "  ROLE=cron · WORKERS 强制 1"
else
  WORKERS=${WORKERS:-1}
  if [ "$WORKERS" != "1" ]; then
    if [ "$PROGRESS_BUS_READY" != "1" ]; then
      echo "  ⚠️ WORKERS=$WORKERS 但 PROGRESS_BUS_READY!=1 · 未确认跨 worker 进度总线就绪 · 回退 WORKERS=1"
      WORKERS=1
    elif ! redis_ready 2>/dev/null; then
      echo "  ⚠️ WORKERS=$WORKERS 但 Redis 不可达 · 跨 worker 总线依赖 Redis · 回退 WORKERS=1"
      WORKERS=1
    fi
  fi
fi
echo "  后端API:  http://localhost:8000"
echo "  前端界面: http://localhost:80"
echo "  API文档:  http://localhost:8000/docs"
echo "  Workers:  $WORKERS"
echo "========================================="

python -m uvicorn server:app --host 0.0.0.0 --port 8000 --workers $WORKERS --timeout-keep-alive 5 &
UVICORN_PID=$!
wait $UVICORN_PID
