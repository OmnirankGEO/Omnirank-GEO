"""
Redis 连接管理
提供全局 Redis 客户端，支持降级到内存缓存

所有模块统一通过此模块获取 Redis 连接
"""

import os
import json
import logging
import time
from typing import Optional, Any

logger = logging.getLogger("GEO-Redis")

_redis_client = None
_redis_available = True
# [FABLE复审 P3根因修复 2026-06-10] 原 _redis_available 一旦初次连接失败就永久 latch False、
#   进程级不自愈 → redis 抖一下(瞬断/重启/启动期未就绪)后 get_redis() 永远返 None,
#   调度器锁续租/抢回重试(server.py 调研&主调度器 cron 的 continue 重试)拿不回连接 = 形同失效。
#   改自愈:连接失败置冷却窗口(默认30s);窗口内直接返 None 不重试(避免每次调用都连超时拖慢),
#   窗口过后下次 get_redis() 自动重连尝试,redis 恢复即自愈。
_redis_retry_after = 0.0
_REDIS_RETRY_COOLDOWN = float(os.getenv("REDIS_RETRY_COOLDOWN_S", "30"))

#: [P1 · 2026-09-02 · 门八前端普查 ①] 降级路上的**单次操作上限**。
#:
#: 跑台实测(容器内直连,50 次 GET):p50 0.12ms / p99 0.21ms / max 0.21ms。
#: 0.5s ≈ p99 的 2400 倍,留足生产抖动余量,又能让"Redis 死了"在半秒内被判出来。
#: 不压到 100ms 以下:那会把偶发抖动误判成死,反而制造无谓降级。
_REDIS_OP_TIMEOUT_S = float(os.getenv("REDIS_OP_TIMEOUT_S", "0.5"))


#: 只有「连不上 / 传不动」才算 Redis 死了。
#:
#: 🔴 [P1-1 · 2026-09-02 · Codex 终审] 上一版对**所有** Exception 都熔断,
#:    而 redis-py 的脚本注册路径把 ``NoScriptError`` 当**正常恢复流程**用:
#:    ``Script.__call__`` 先 ``evalsha`` → 撞 ``NoScriptError`` →
#:    ``script_load`` → 再 ``evalsha`` 成功(redis-py 5.2.1 源码逐字如此)。
#:    冷缓存首调 / Redis 重启 / ``SCRIPT FLUSH`` 之后必然走这一遭。
#:    于是:**脚本明明恢复成功了,client 却已被作废 + 进 30s 冷却** ⇒
#:    ``get_redis()`` 返 None ⇒ ``auth/global_rate_limiter`` fail-open
#:    (登录限流退化成进程内)、``cache/diagnosis_slots`` 槽位与
#:    ``cache/leader_lock`` 整片降级。
#:
#: 🔴 触发方与受害方不是同一个,而且触发条件比"用了 register_script"更窄:
#:    ``register_script`` 经本代理拿到的是 **raw 的绑定方法**,所以返回的
#:    ``Script`` 绑的是 raw(``registered_client is raw``)—— 光注册**不会**
#:    让 NoScript 穿过代理。真正让它穿过来的是调用时**显式传 ``client=r``**
#:    (``Script.__call__``:``if client is None: client = self.registered_client``,
#:    显式传的赢)。本仓 7 处全这么调:
#:      ``auth/rate_limiter.py:166`` · ``cache/diagnosis_slots.py:121/140/169``
#:      · ``cache/leader_lock.py:137/160/177``
#:    受害方是 ``auth/global_rate_limiter:169`` —— 它走 ``r.eval``(每次发脚本
#:    正文,根本不会 NoScript),纯粹被**共享同一个 client** 连累:别人把
#:    client 作废了,它下一次 ``get_redis()`` 拿到 None ⇒ ``return True, limit``
#:    ⇒ 登录限流不生效。
#:
#: 判断口径:``ConnectionError`` / ``TimeoutError``(及其子类 BusyLoading /
#: Authentication)与 ``OSError``(socket 层)⇒ 熔断;
#: ``ResponseError``(含 ``NoScriptError``)/ ``DataError`` / ``WatchError``
#: 以及任何非 Redis 异常 ⇒ **原样抛出但不熔断** —— 服务器答了话,说明连接是活的。
_BREAKER_TYPES = None


def _breaker_error_types() -> tuple:
    """惰性取熔断异常集(本模块对 redis 是惰性 import,这里保持一致)。"""
    global _BREAKER_TYPES
    if _BREAKER_TYPES is None:
        try:
            from redis import exceptions as _rexc
            _BREAKER_TYPES = (_rexc.ConnectionError, _rexc.TimeoutError, OSError)
        except Exception:      # redis 没装/装坏 —— 那就只认 socket 层
            _BREAKER_TYPES = (OSError,)
    return _BREAKER_TYPES


def _mark_broken(exc: Exception) -> None:
    """操作/建连失败的**唯一**处置口:作废 client + 进冷却。

    🔴 [P1] 修的是这个:原来只有**建连**失败会进冷却,而**已连上之后 Redis 死了**
       的那条路,`redis_get` 等只 `logger.warning` 就返回 —— client 永不作废,
       于是 `get_redis()` 每次都把那个坏 client 原样发出去,
       每一次操作都付满 socket_timeout(× retry_on_timeout 重试),
       而 :41 那条冷却分支**永远到不了**(直到进程重启)。
       本模块抬头写着冷却是为了「避免每次调用都连超时拖慢」—— 意图一直是对的,
       只是它从来没覆盖到"连上之后才死"这一半。
       实测代价:Redis 不可达时**每个已鉴权请求** 8~16s(限流器在主链上)。
    """
    global _redis_client, _redis_available, _redis_retry_after
    _redis_client = None
    _redis_available = False
    _redis_retry_after = time.time() + _REDIS_RETRY_COOLDOWN
    logger.warning(
        f"[Redis] 操作失败 · 作废连接并冷却 {_REDIS_RETRY_COOLDOWN:.0f}s(降级内存/DB): {exc}")


class _SelfMarkingRedis:
    """让「失败 ⇒ 作废 + 冷却」挂在**客户端本身**上,而不是挂在本模块的包装函数上。

    🔴 为什么必须这样,而不是在 `redis_get`/`redis_set` 里各写一遍:
       `auth/global_rate_limiter` 拿 `get_redis()` 的 client **自己跑
       `eval` / `pipeline`**,它的异常根本不经过本模块任何包装函数 ——
       而它在**每一个已鉴权请求**上。只包装本模块那几个函数,主腿一次都盖不到
       (判据分母因此打的是 `/api/auth/me` 这种**不碰进度链**的端点)。
       挂在 client 上,所有调用方一个字都不用改,异常照样原样抛给它们。
    """

    __slots__ = ("_r",)

    def __init__(self, raw):
        object.__setattr__(self, "_r", raw)

    def __getattr__(self, name):
        raw = object.__getattribute__(self, "_r")
        attr = getattr(raw, name)
        if not callable(attr):
            return attr

        def _wrapped(*args, **kwargs):
            try:
                out = attr(*args, **kwargs)
            except Exception as exc:
                if isinstance(exc, _breaker_error_types()):
                    _mark_broken(exc)
                raise
            # pipeline() 返回的对象、以及链式调用返回自身的那些,都要继续自证 ——
            # 真正的失败点是 pipe.execute(),不是 pipe 的构造。
            if out is raw:
                return self
            if name == "pipeline":
                return _SelfMarkingRedis(out)
            return out

        return _wrapped

    def __enter__(self):
        return _SelfMarkingRedis(object.__getattribute__(self, "_r").__enter__())

    def __exit__(self, *exc):
        return object.__getattribute__(self, "_r").__exit__(*exc)


def get_redis():
    """
    获取 Redis 客户端（单例·失败自愈）
    Redis 不可用时返回 None，调用方自行降级。
    [自愈] 连接失败后进入冷却窗口(_REDIS_RETRY_COOLDOWN 秒);窗口内返 None 不重试,
    窗口过后自动再尝试重连,redis 恢复即自愈(修 FABLE 复审指出的永久 latch P3 根因)。
    """
    global _redis_client, _redis_available, _redis_retry_after

    # 已有可用客户端 → 直接返回(在途瞬断由 redis-py health_check/retry_on_timeout 自处理)
    if _redis_client is not None:
        return _redis_client

    # 不可用状态:冷却窗口内不重试 · 窗口过后落到下方重连(自愈)
    if not _redis_available and time.time() < _redis_retry_after:
        return None

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    try:
        import redis as redis_lib
        _raw = redis_lib.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=_REDIS_OP_TIMEOUT_S,
            socket_timeout=_REDIS_OP_TIMEOUT_S,
            # 🔴 降级路上关重试:重试把"判死"的代价乘上倍数,而这条路的价值
            #    恰恰是**尽快判死**再走 DB/放行。瞬断由冷却到期后的单次探测接住。
            retry_on_timeout=False,
            health_check_interval=30,
        )
        _raw.ping()
        logger.info(f"[Redis] {'自愈重连成功' if not _redis_available else '连接成功'}: {redis_url}")
        _redis_client = _SelfMarkingRedis(_raw)
        _redis_available = True
        _redis_retry_after = 0.0
        return _redis_client
    except Exception as e:
        _redis_client = None
        _redis_available = False
        _redis_retry_after = time.time() + _REDIS_RETRY_COOLDOWN
        logger.warning(f"[Redis] 连接失败(降级内存·{_REDIS_RETRY_COOLDOWN:.0f}s 后自动重试): {e}")
        return None


def redis_get(key: str) -> Optional[str]:
    """安全读取，Redis 不可用时返回 None"""
    r = get_redis()
    if r is None:
        return None
    try:
        return r.get(key)
    except Exception as e:
        logger.warning(f"[Redis] GET {key} 失败: {e}")
        return None


def redis_set(key: str, value: str, ex: int = None) -> bool:
    """安全写入，Redis 不可用时返回 False"""
    r = get_redis()
    if r is None:
        return False
    try:
        r.set(key, value, ex=ex)
        return True
    except Exception as e:
        logger.warning(f"[Redis] SET {key} 失败: {e}")
        return False


def redis_delete(key: str) -> bool:
    """安全删除"""
    r = get_redis()
    if r is None:
        return False
    try:
        r.delete(key)
        return True
    except Exception as e:
        logger.warning(f"[Redis] DEL {key} 失败: {e}")
        return False


def redis_get_json(key: str) -> Optional[Any]:
    """读取并解析 JSON"""
    val = redis_get(key)
    if val is None:
        return None
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return None


def redis_set_json(key: str, value: Any, ex: int = None) -> bool:
    """序列化为 JSON 并写入"""
    try:
        return redis_set(key, json.dumps(value, ensure_ascii=False), ex=ex)
    except (TypeError, ValueError):
        return False


def acquire_lock(lock_name: str, ttl: int = 30, holder: str = "") -> bool:
    """
    Redis 分布式锁（SETNX）
    ttl: 锁过期时间（秒），防止死锁
    holder: 锁持有者标识（用于调试）
    """
    r = get_redis()
    if r is None:
        return True  # Redis 不可用时不阻塞，降级为无锁
    try:
        value = holder or f"lock-{time.time()}"
        return bool(r.set(lock_name, value, nx=True, ex=ttl))
    except Exception as e:
        logger.warning(f"[Redis] 获取锁 {lock_name} 失败: {e}")
        return True  # 降级


def release_lock(lock_name: str) -> bool:
    """释放分布式锁"""
    return redis_delete(lock_name)
