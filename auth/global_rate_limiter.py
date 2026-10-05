"""
全局 API 限流中间件
基于 Redis 滑动窗口，按登录用户限流

限流规则:
- 普通 API: 30 次/分钟/用户
- LLM 流式接口 (SSE): 5 次/分钟/用户
- 未认证请求: 20 次/分钟/IP
"""

import os
import time
import logging
from fastapi import Request
from fastapi.responses import JSONResponse

from cache.redis_client import get_redis

logger = logging.getLogger("GEO-RateLimit")

_REDIS_FAILURE_LOG_INTERVAL_SECONDS = 30.0
_last_redis_failure_log_at = float("-inf")

# 限流配置（[并发-5 2026-06-10 打广告高并发] 全部 env 可调·压测/广告期经 env 提额免改码·force-recreate 生效）
NORMAL_LIMIT = int(os.getenv("RL_NORMAL_LIMIT", "100"))      # 普通 API: 100 次/分钟
# [并发-5] SSE 拆 bucket:诊断/监测重 GEO 流 vs 对话轻流,此前共享单一 5/分钟 bucket →
# 压测/共享账号下"诊断吃掉对话额度、反之亦然"互相挤爆 429。拆开 + 各自提额。
SSE_LIMIT = int(os.getenv("RL_SSE_LIMIT", "5"))             # 兜底:未分类 SSE(理论不该命中)
DIAG_SSE_LIMIT = int(os.getenv("RL_DIAG_SSE_LIMIT", "10"))  # 诊断/监测/调研重 GEO 流: 10/分钟
CHAT_SSE_LIMIT = int(os.getenv("RL_CHAT_SSE_LIMIT", "20"))  # 对话/选题/脚本轻流: 20/分钟
LIGHT_LIMIT = int(os.getenv("RL_LIGHT_LIMIT", "300"))      # 轻量 housekeeping: 300 次/分钟(v1.7.6 P1-3 RATE-002)
                        # 用于 list/delete 等不消耗 LLM 的轻 op · 防被 normal bucket 同篇消耗挤爆
PUBLISH_HISTORY_READ_LIMIT = int(os.getenv("RL_PUBLISH_HISTORY_READ_LIMIT", "60"))
                        # 发布记录纯读:独立窄桶 · 不让发布页流量挤占身份/普通 API
ANON_LIMIT = int(os.getenv("RL_ANON_LIMIT", "60"))         # 未认证: 60 次/分钟
WINDOW = 60             # 窗口大小（秒）

# [并发-5] 诊断/监测重 GEO SSE 流(广告 burst 主战场·重 LLM+数据 API)→ 独立 bucket
DIAG_SSE_PATTERNS = (
    "/api/diagnosis/run",
    "/api/monitoring/run-stream",
)

# [并发-5] 对话/选题/脚本轻 SSE 流(社媒/C 端对话·聊天天然高频)→ 独立 bucket
# [开源 E3 · B2 · 2026-09-28] 原 7 条(content 快写 / 对话 / 选题 / 采访脚本流、社媒改写流、agent 对话、C 端对话)
#   所在端点已随 E3 全部删除,在役路由 0 条以它们开头 ⇒ 条目删;桶保留,新的轻对话流登记到这里即生效。
CHAT_SSE_PATTERNS = ()

# 全部 SSE 端点(兜底分类用)
SSE_PATTERNS = DIAG_SSE_PATTERNS + CHAT_SSE_PATTERNS

# v1.7.6 P1-3 RATE-002 · 轻量 housekeeping 端点(无 LLM · DB 单读单写)
# 单独走 LIGHT bucket (300/min) · 防被 normal 100/min 跟其他 API 抢额度
# [开源 E3 · B2 · 2026-09-28] 原 3 条(聊天附件 list / delete、DELETE 会话)所在端点已随 E3 删除,
#   在役路由 0 条以它们开头 ⇒ 条目与 DELETE 会话的特判一起删;桶保留。
LIGHT_PATTERNS = ()

# 只允许这个经过认证的纯 GET 进入发布记录读桶。不得使用 prefix，避免未来
# /publish-history/* 写操作被误纳入轻量桶。
PUBLISH_HISTORY_READ_PATH = "/api/meijiehezi/publish-history"

_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local window_start = tonumber(ARGV[1])
local member = ARGV[2]
local score = tonumber(ARGV[3])
local limit = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])
redis.call('ZREMRANGEBYSCORE', key, 0, window_start)
local count = redis.call('ZCARD', key)
if count >= limit then
    return {0, 0}
end
redis.call('ZADD', key, score, member)
redis.call('EXPIRE', key, ttl)
return {1, limit - count - 1}
"""

# 不限流的路径
EXEMPT_PATHS = {
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/captcha",
    "/api/auth/send-sms",
    "/api/auth/refresh",
    "/api/m3/customer-events/public",  # endpoint 内部有 per-IP 公开埋点限流
    "/docs",
    "/openapi.json",
    "/redoc",
    "/health",
}

EXEMPT_PREFIXES = (
    "/api/portal/",
    "/api/s/",
    "/api/sl/",
    "/api/m/",
    "/api/public/",
    "/static/",
    "/assets/",
    # [开源 E3 · B2 · 2026-09-28] 原 C 端 GEO 方案任务轮询豁免(Phase 4 · 2026-04-20 老板批 S2)随任务 API
    #   在 B3c 删除,在役路由 0 条以它开头 ⇒ 条目删。
)


def _is_sse_endpoint(path: str) -> bool:
    """判断是否为 SSE/长耗时端点"""
    return any(path.startswith(p) for p in SSE_PATTERNS)


def _sse_bucket(path: str) -> tuple[str, int]:
    """[并发-5] SSE 分桶:诊断/监测重流 vs 对话轻流,各自独立 bucket + 限额。

    Returns: (bucket_kind, limit) — bucket_kind 拼进 redis key 实现隔离。
    """
    if any(path.startswith(p) for p in DIAG_SSE_PATTERNS):
        return "sse_diag", DIAG_SSE_LIMIT
    if any(path.startswith(p) for p in CHAT_SSE_PATTERNS):
        return "sse_chat", CHAT_SSE_LIMIT
    return "sse", SSE_LIMIT  # 兜底(理论不命中)


def _is_light_endpoint(path: str, method: str) -> bool:
    """v1.7.6 P1-3 · 判断是否为轻量 housekeeping(list/delete/无 LLM)。

    原登记的聊天附件 / 会话端点已随开源 E3 删除,名单现为空(见 LIGHT_PATTERNS)。
    """
    return any(path.startswith(p) for p in LIGHT_PATTERNS)


def _is_publish_history_read(path: str, method: str) -> bool:
    return method == "GET" and path == PUBLISH_HISTORY_READ_PATH


def _check_rate_limit(key: str, limit: int, window: int) -> tuple[bool, int]:
    """
    滑动窗口限流检查（Redis sorted set）

    Returns:
        (allowed: bool, remaining: int)
    """
    r = get_redis()
    if r is None:
        return True, limit  # Redis 不可用，不限流

    try:
        now = time.time()
        window_start = now - window

        # Production Redis uses one atomic script.  The previous four-command
        # pipeline allowed simultaneous workers to all observe the same pre-add
        # count and briefly exceed the configured limit.  Keep the pipeline path
        # only for legacy test doubles that intentionally do not implement EVAL.
        if hasattr(r, "eval"):
            member = f"{now:.9f}:{os.getpid()}:{time.monotonic_ns()}"
            results = r.eval(
                _SLIDING_WINDOW_LUA,
                1,
                key,
                window_start,
                member,
                now,
                limit,
                window + 1,
            )
            return bool(int(results[0])), int(results[1])

        pipe = r.pipeline()
        pipe.zremrangebyscore(key, 0, window_start)   # 清除过期记录
        pipe.zcard(key)                                # 当前窗口请求数
        pipe.zadd(key, {str(now): now})                # 记录本次请求
        pipe.expire(key, window + 1)                   # 设置过期
        results = pipe.execute()

        current_count = results[1]

        if current_count >= limit:
            # 超限，回滚本次记录
            try:
                r.zrem(key, str(now))
            except Exception:
                pass
            return False, 0

        return True, limit - current_count - 1
    except Exception as exc:
        global _last_redis_failure_log_at
        # Keep the established fail-open contract when a cached Redis client
        # becomes unavailable after startup. Rate limiting must not turn every
        # otherwise healthy API request into HTTP 500 during a Redis restart.
        failed_at = time.monotonic()
        if failed_at - _last_redis_failure_log_at >= _REDIS_FAILURE_LOG_INTERVAL_SECONDS:
            _last_redis_failure_log_at = failed_at
            logger.warning("Redis rate-limit check failed; allowing request: %s", exc)
        return True, limit


def setup_rate_limit_middleware(app):
    """注册全局限流中间件"""

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        path = request.url.path
        method = request.method

        # 跳过不需要限流的路径
        if path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES):
            return await call_next(request)

        # OPTIONS 预检不限流
        if method == "OPTIONS":
            return await call_next(request)

        # 静态资源不限流
        if "." in path.split("/")[-1]:
            return await call_next(request)

        # 非 API 路径不限流
        if not path.startswith("/api/"):
            return await call_next(request)

        # 确定限流 key 和限制
        user = getattr(request.state, "user", None)
        if user and isinstance(user, dict) and user.get("user_id"):
            # 管理员不限流
            if user.get("is_admin"):
                return await call_next(request)
            uid = user["user_id"]
            if _is_sse_endpoint(path):
                # [并发-5] 诊断/监测重流 与 对话轻流 分桶隔离 · 互不挤占
                bucket_kind, limit = _sse_bucket(path)
                key = f"ratelimit:{bucket_kind}:{uid}"
            elif _is_publish_history_read(path, method):
                key = f"ratelimit:publish_history_read:{uid}"
                limit = PUBLISH_HISTORY_READ_LIMIT
            elif _is_light_endpoint(path, method):
                # v1.7.6 P1-3 RATE-002 · 轻量 housekeeping 单独 bucket · 防 list/delete 跟 normal 抢
                key = f"ratelimit:light:{uid}"
                limit = LIGHT_LIMIT
            else:
                key = f"ratelimit:api:{uid}"
                limit = NORMAL_LIMIT
        else:
            # 未认证请求按 IP 限流
            ip = request.headers.get("X-Real-IP", request.client.host if request.client else "unknown")
            key = f"ratelimit:anon:{ip}"
            limit = ANON_LIMIT

        allowed, remaining = _check_rate_limit(key, limit, WINDOW)

        if not allowed:
            logger.warning(f"[RateLimit] 限流触发: {key} ({path})")
            return JSONResponse(
                status_code=429,
                content={"detail": "请求过于频繁，请稍后再试", "code": "RATE_LIMITED"},
                headers={
                    "Retry-After": str(WINDOW),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response

    logger.info("[RateLimit] 全局限流中间件已注册")
