"""
跨 worker 诊断进度总线(WORKERS=4 · SPEC §6/§6b)
=================================================

问题(WORKERS>1 病根):诊断任务落 A 进程、WS 连接落 B 进程(3/4 概率)→
`ConnectionManager.active_connections` 是纯进程内存 → 实时进度永远推不到 B 的 WS。

方案(latest-state 语义 · I3):
  - Redis **db=1**(沿用 ConnectionManager 现有命名空间)
  - 快照   `task_status:{sid}`(JSON · **TTL 1800** · 沿用旧键 → 轮询端点/旧读侧零改兼容)
  - 序号   `progress:seq:{sid}`(INCR · 单调 · 允许跳号)
  - 频道   `progress:ch:{sid}`(pubsub · 每 worker 一个订阅循环 psubscribe `progress:ch:*`)
  - 发布   INCR seq → 注入 msg.seq → **守卫 Lua 原子[守卫 SET 快照 EX + 刷 seq TTL + PUBLISH]**
           (SPEC §6.2 · 2026-07-13 boss 审包预警 #3 修:快照 SET 加 seq 守卫 —— 仅当
            新 seq> 现存快照 seq 或 带 terminal 才写,终态间也单调。关闭"MULTI 外 INCR seq"
            与 §3.6a sweeper/reconciler 同 sid 并发时旧 seq 回退覆盖新快照的缝。
            payload 作 opaque ARGV 不进 cjson → 不重蹈中文/浮点强转坑;PUBLISH 始终发,WS 按 seq 去重。)

承诺(I3):最新状态最终可达 · seq 单调 · 客户端可按 seq 去重 · **允许跳号**(断线期中间进度
折叠为最新快照)· 任何读取路径归属校验在调用方(WS/轮询端点已有 RBAC)。

Redis 故障(§6.6/§6b.2):circuit breaker 连续超时 open 60s;期间**中间进度**发布 fail-soft
(返回 None → 调用方本地直投兜底);**终态**由 §3.6a DB 兜底 + reconciler 回补(见 B4),
本模块不承担终态持久化。

只依赖 redis[hiredis]==5.2.1 的 `redis.asyncio`(异步 pub/sub + 发布)与同步 `redis`(轮询快照读)。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("GEO-ProgressBus")

# ---- Redis 命名 ----
_REDIS_HOST = "redis"
_REDIS_PORT = 6379
_REDIS_DB = 1  # 沿用 ConnectionManager 的 task_status 命名空间

_SNAP_PREFIX = "task_status:"        # 旧键 · 轮询端点/旧读侧零改
_SEQ_PREFIX = "progress:seq:"
_CHAN_PREFIX = "progress:ch:"

SNAP_TTL = 1800  # [修 §3.3 TTL bug] 旧 600s < 诊断 1800s 上限 → 慢诊断中途快照过期、跨 worker 轮询 found:False

_SNAPSEQ_PREFIX = "progress:snapseq:"  # [seq 守卫] 记录当前快照所携 seq(终态哨兵编码)· 防旧 seq 回退覆盖
# [seq 守卫] 终态哨兵基:终态写入时 snapseq 记为 TERM_BASE+seq → 任何中间态(seq<<TERM_BASE)永不超过 →
# 终态最终、不被中间态回退覆盖(服务端对称 HC3 前端"终态优先"规则)。单会话 seq 远小于 1e12。
_TERM_BASE = 1_000_000_000_000


def _snap_key(sid: str) -> str:
    return f"{_SNAP_PREFIX}{sid}"


def _seq_key(sid: str) -> str:
    return f"{_SEQ_PREFIX}{sid}"


def _snapseq_key(sid: str) -> str:
    return f"{_SNAPSEQ_PREFIX}{sid}"


def _chan_key(sid: str) -> str:
    return f"{_CHAN_PREFIX}{sid}"


# ============================================================================
# [seq 回退守卫 · 2026-07-13 boss 审包预警 #3] MULTI 外 INCR seq 留了回退缝:
#   两写者(任务写者 publish + §3.6a sweeper/reconciler 也 publish 同 sid 终态)并发时,
#   seq=6 的 SET 可能先于 seq=5 执行 → 旧 seq 覆盖新快照回退。修法(a):快照 SET 加守卫,
#   仅当 新 seq > 现存 或 带 terminal 标记 才写(终态优先且终态间也单调)。
# 用小 Lua 只比较整数 snapseq —— payload 作 opaque ARGV,不进 cjson(不重蹈中文/浮点强转坑)。
# ----------------------------------------------------------------------------
# 异步 · 守卫快照 SET + 刷 seq TTL + PUBLISH(**仅守卫接受时发布** · 拒绝=陈旧/回退,不扇出)· 原子。
# KEYS[1]=snap KEYS[2]=snapseq KEYS[3]=seq KEYS[4]=chan
# ARGV[1]=encoded ARGV[2]=new_seq ARGV[3]=terminal(1/0) ARGV[4]=ttl ARGV[5]=term_base
_PUBLISH_GUARDED_LUA = """
local stored = tonumber(redis.call('GET', KEYS[2]) or '-1')
local newseq = tonumber(ARGV[2])
local term_base = tonumber(ARGV[5])
local is_terminal = ARGV[3] == '1'
local ttl = tonumber(ARGV[4])
local allow = false
if is_terminal then
  if stored < term_base or newseq > (stored - term_base) then allow = true end
elseif stored < term_base and newseq > stored then
  allow = true
end
redis.call('EXPIRE', KEYS[3], ttl)
if allow then
  local store_seq = newseq
  if is_terminal then store_seq = term_base + newseq end
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ttl)
  redis.call('SET', KEYS[2], tostring(store_seq), 'EX', ttl)
  redis.call('PUBLISH', KEYS[4], ARGV[1])
end
return allow and 1 or 0
"""

# 同步 · 无 seq 的直接置态(queued / bus-down 兜底 / 终态 error)守卫:
#   仅当尚无 seq'd 快照(snapseq 缺失)或本次终态,才允许覆盖 → 防 queued/陈旧 sync 写回退进行中快照。
# KEYS[1]=snap KEYS[2]=snapseq  ARGV[1]=encoded ARGV[2]=terminal(1/0) ARGV[3]=ttl ARGV[4]=term_base
_SYNC_SET_GUARDED_LUA = """
local stored = redis.call('GET', KEYS[2])
local is_terminal = ARGV[2] == '1'
if stored ~= false and not is_terminal then
  return 0
end
local ttl = tonumber(ARGV[3])
redis.call('SET', KEYS[1], ARGV[1], 'EX', ttl)
if is_terminal then
  redis.call('SET', KEYS[2], ARGV[4], 'EX', ttl)
end
return 1
"""

_async_publish_script = None   # AsyncScript(绑定 async client)
_sync_set_script = None        # Script(绑定 sync client)


# ---- 发布/同步客户端(懒加载 · 连不上冷却后自动重连 · **禁永久 broken latch**)----
# [foundation-fix 2026-07-13 boss #1] 旧 `_*_broken=True` 是永久闩:Redis 抖一下即永久降级本地,
#   直到进程重启才恢复。改为**冷却时间戳退避**:失败置 retry_at=now+cooldown,冷却过后自动重连;
#   任何操作异常 _mark_*_broken() 重置 client=None + 置冷却 → 下次 get 重连。与 publish 的 circuit
#   breaker(60s)叠加:cooldown(30s)< breaker,breaker 半开放行时连接冷却已过 → 真正重连。
# 长期 subscriber 不使用这些全局单例/冷却；每次连接尝试独占 client，并在自己的指数退避中关闭重建。
_REDIS_RECONNECT_COOLDOWN = 30.0

#: [P1 · 2026-09-02] 降级路上的单次操作上限。与 cache.redis_client 同一个环境变量,
#: 两处一个值 —— 两个模块各写一个数,调参时必然漏掉一处。
_SYNC_OP_TIMEOUT_S = float(os.getenv("REDIS_OP_TIMEOUT_S", "0.5"))
_SUBSCRIBER_RECONNECT_INITIAL = 1.0
_SUBSCRIBER_RECONNECT_MAX = 30.0
_SUBSCRIBER_HEALTH_CHECK_INTERVAL = 30.0
_SUBSCRIBER_STABLE_SECONDS = _SUBSCRIBER_HEALTH_CHECK_INTERVAL
_SUBSCRIBER_HANDSHAKE_TIMEOUT = 2.0
_SUBSCRIBER_HEARTBEAT_INTERVAL = 10.0
_SUBSCRIBER_HEARTBEAT_TIMEOUT = 3.0

_async_redis = None            # redis.asyncio.Redis(短操作:publish · 有限 socket timeout)
_async_redis_retry_at = 0.0    # 下次允许重连时刻(time.monotonic 秒);0=可立即试
_sync_redis = None             # redis.Redis(同步 · 轮询快照读)
_sync_redis_retry_at = 0.0


def _mark_async_broken():
    """连接/操作失败 → 重置 async client 并置冷却(冷却后 _get_async_redis 自动重连)。"""
    global _async_redis, _async_redis_retry_at
    _async_redis = None
    _async_redis_retry_at = time.monotonic() + _REDIS_RECONNECT_COOLDOWN


def _mark_sync_broken():
    global _sync_redis, _sync_redis_retry_at
    _sync_redis = None
    _sync_redis_retry_at = time.monotonic() + _REDIS_RECONNECT_COOLDOWN


async def _get_async_redis():
    """发布专用 client：有限读超时 + 全局冷却，禁止长期 Pub/Sub 借用。"""
    global _async_redis, _async_redis_retry_at
    if _async_redis is not None:
        return _async_redis
    if time.monotonic() < _async_redis_retry_at:
        return None  # 冷却期内 · 暂不重连(避免每消息狂连);冷却过后自动尝试
    try:
        import redis.asyncio as aioredis
        client = aioredis.Redis(
            host=_REDIS_HOST, port=_REDIS_PORT, db=_REDIS_DB,
            decode_responses=True,
            socket_connect_timeout=2, socket_timeout=5,
            health_check_interval=30,
        )
        await client.ping()
        _async_redis = client
        _async_redis_retry_at = 0.0
        logger.info("[ProgressBus] async Redis 连接成功(db=1 · publish 就绪)")
        return _async_redis
    except Exception as e:
        _async_redis_retry_at = time.monotonic() + _REDIS_RECONNECT_COOLDOWN
        logger.warning(f"[ProgressBus] async Redis 连不上 · {_REDIS_RECONNECT_COOLDOWN:.0f}s 后自动重试(非永久 latch): {e}")
        return None


async def _close_async_resource(resource) -> None:
    """兼容 redis-py 现役/旧版本关闭 API，优先 aclose 避免 close 弃用告警。"""
    if resource is None:
        return
    closer = getattr(resource, "aclose", None)
    if closer is None:
        closer = getattr(resource, "close", None)
    if closer is None:
        return
    result = closer()
    if inspect.isawaitable(result):
        await result


async def _create_subscriber_redis():
    """创建一次订阅尝试独占的 Redis client；永不读超时，且不写发布 client 全局状态。"""
    import redis.asyncio as aioredis

    client = aioredis.Redis(
        host=_REDIS_HOST,
        port=_REDIS_PORT,
        db=_REDIS_DB,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=None,
        socket_keepalive=True,
        health_check_interval=_SUBSCRIBER_HEALTH_CHECK_INTERVAL,
    )
    try:
        # socket_timeout=None 只服务长期空闲读；建连握手仍须有应用层上限，防 TCP 已接收但不响应。
        await asyncio.wait_for(client.ping(), timeout=_SUBSCRIBER_HANDSHAKE_TIMEOUT)
    except BaseException:
        try:
            await _close_async_resource(client)
        except Exception:
            pass
        raise
    return client


async def _wait_for_psubscribe_ack(pubsub, pattern: str) -> None:
    """消费并核对订阅确认；仅发出 PSUBSCRIBE 不等于服务端已经接纳。"""
    while True:
        message = await pubsub.get_message(timeout=0.2)
        if not message:
            await asyncio.sleep(0)
            continue
        if message.get("type") != "psubscribe":
            continue
        channel = message.get("channel")
        if isinstance(channel, bytes):
            channel = channel.decode("utf-8", errors="replace")
        if channel == pattern:
            return


async def _wait_for_retry_or_stop(stop_event: Optional[asyncio.Event], delay: float) -> bool:
    """退避等待；返回 True 表示收到 stop，避免 shutdown 最多卡 30 秒。"""
    if stop_event is None:
        await asyncio.sleep(delay)
        return False
    if stop_event.is_set():
        return True
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
        return True
    except asyncio.TimeoutError:
        return False


def _get_sync_redis():
    global _sync_redis, _sync_redis_retry_at
    if _sync_redis is not None:
        return _sync_redis
    if time.monotonic() < _sync_redis_retry_at:
        return None
    try:
        import redis as redis_lib
        client = redis_lib.Redis(
            host=_REDIS_HOST, port=_REDIS_PORT, db=_REDIS_DB,
            decode_responses=True,
            socket_connect_timeout=_SYNC_OP_TIMEOUT_S,
            socket_timeout=_SYNC_OP_TIMEOUT_S,
            retry_on_timeout=False,
        )
        client.ping()
        _sync_redis = client
        _sync_redis_retry_at = 0.0
        return _sync_redis
    except Exception:
        _sync_redis_retry_at = time.monotonic() + _REDIS_RECONNECT_COOLDOWN
        return None


# ---- circuit breaker(§6b.2)----
class _Breaker:
    def __init__(self, fail_threshold: int = 5, open_seconds: float = 60.0):
        self.fail_threshold = fail_threshold
        self.open_seconds = open_seconds
        self._consecutive = 0
        self._opened_at = 0.0

    def is_open(self, now: float) -> bool:
        if self._opened_at and (now - self._opened_at) < self.open_seconds:
            return True
        if self._opened_at:  # 冷却结束 → half-open 探测
            self._opened_at = 0.0
        return False

    def record_ok(self):
        self._consecutive = 0

    def record_fail(self, now: float):
        self._consecutive += 1
        if self._consecutive >= self.fail_threshold and not self._opened_at:
            self._opened_at = now
            logger.error(f"[ProgressBus] circuit breaker OPEN {self.open_seconds}s(连续 {self._consecutive} 次失败)")


_breaker = _Breaker()


# 守卫拒绝哨兵(publish 返回):seq 陈旧/乱序被守卫 Lua 拒 → 调用方**不得本地直投**(快照已有更新态)。
PUBLISH_REJECTED = 0


async def publish(session_id: str, message: dict, terminal: bool = False,
                  monotonic: Callable[[], float] = time.monotonic) -> Optional[int]:
    """
    发布一条进度(中间态或终态)。返回:
      - seq(int > 0):守卫接受并已 PUBLISH(调用方可本地直投低延迟 · seq 去重);
      - `PUBLISH_REJECTED`(0):守卫**拒绝**(此 seq 陈旧/乱序 · 快照已有更新态)→ 调用方**不得本地直投**
        (否则把被 Redis 拒的陈旧事件本地推给 WS → 终态页倒退回"处理中");
      - None:Redis 故障/断路器打开 → 中间进度 fail-soft 本地兜底(终态另由 §3.6a DB 兜底)。
    """
    now = monotonic()
    if _breaker.is_open(now):
        return None
    ar = await _get_async_redis()
    if ar is None:
        return None
    try:
        seq = await ar.incr(_seq_key(session_id))
        msg = dict(message)
        msg["seq"] = seq
        if terminal:
            msg["terminal"] = True  # HC3:前端终态优先于 seq 去重
        encoded = json.dumps(msg, ensure_ascii=False)
        # [seq 守卫] 守卫快照 SET(仅新 seq> 现存 或 terminal 才写)+ 刷 seq TTL + PUBLISH,单 Lua 原子。
        global _async_publish_script
        if _async_publish_script is None:
            _async_publish_script = ar.register_script(_PUBLISH_GUARDED_LUA)
        _res = await _async_publish_script(
            keys=[_snap_key(session_id), _snapseq_key(session_id),
                  _seq_key(session_id), _chan_key(session_id)],
            args=[encoded, seq, 1 if terminal else 0, SNAP_TTL, _TERM_BASE],
            client=ar,
        )
        _breaker.record_ok()
        # [返工 实时] 守卫拒绝(allow=0)→ 此事件陈旧未 PUBLISH → 返 REJECTED · 调用方不得本地直投
        try:
            _accepted = int(_res) == 1
        except (TypeError, ValueError):
            _accepted = True  # 返回不可解析时保守当接受(不误吞)
        if not _accepted:
            return PUBLISH_REJECTED
        return seq
    except Exception as e:
        _breaker.record_fail(monotonic())
        _mark_async_broken()  # 重置连接 + 置冷却 → 冷却过后自动重连(禁永久 latch · 与 breaker 叠加)
        logger.warning(f"[ProgressBus] publish 失败 session={session_id}: {e}")
        return None


#: `set_snapshot_sync` 的三态。**刻意与 `publish` 同一套语义**(见 PUBLISH_REJECTED):
#: 一个模块里两条写路径,守卫结果的表达方式不该有两种。
SYNC_SET_ACCEPTED = 1
SYNC_SET_REJECTED = 0


def set_snapshot_sync(session_id: str, message: dict) -> Optional[int]:
    """
    同步写快照(不发 pubsub · 不分配 seq)。用于"排队 queued"/bus-down 兜底/终态 error 直接置态
    (ConnectionManager._set_status / send_message 兜底),让轮询端点/新连接能读到。

    [seq 守卫] 走守卫 Lua:仅当尚无 seq'd 快照(publish 未接管)或本次终态才覆盖 → 防 queued/陈旧
    sync 写回退掉 publish 已推进的快照。

    返回(与 ``publish`` 同构):
      - ``SYNC_SET_ACCEPTED``(1):守卫接受,Redis 快照已写;
      - ``SYNC_SET_REJECTED``(0):守卫**拒绝**(Redis 里已有 seq'd 快照且本次非终态)
        → 调用方**不得**把这条消息写进进程内兜底,否则本地会倒退回 queued,
        而 Redis 一旦失联,那份倒退的本地态就成了用户看到的唯一答案(P2-RUNTIME-1);
      - ``None``:Redis 故障/不可达 → 调用方按 fail-soft 自行决定要不要本地兜底。

    🔴 [P2-RUNTIME-1 2026-08-31] 原来这里返 ``bool``,把「拒绝」和「接受」压成同一个 ``True``,
       于是调用方**不可能**知道 Redis 拒了它。改成三态是这条修复的地基:
       没有这个区分,下游 ``_set_status`` 的"拒绝就别写本地"根本无从判断。
    """
    r = _get_sync_redis()
    if r is None:
        return None
    try:
        terminal = bool(
            message.get("terminal") or message.get("done")
            or message.get("type") == "complete" or message.get("error")
        )
        encoded = json.dumps(message, ensure_ascii=False)
        global _sync_set_script
        if _sync_set_script is None:
            _sync_set_script = r.register_script(_SYNC_SET_GUARDED_LUA)
        allowed = _sync_set_script(
            keys=[_snap_key(session_id), _snapseq_key(session_id)],
            args=[encoded, 1 if terminal else 0, SNAP_TTL, _TERM_BASE],
            client=r,
        )
        # Lua 返 1=写了 / 0=守卫拒。原来这个返回值被整个丢掉。
        return SYNC_SET_ACCEPTED if int(allowed or 0) == 1 else SYNC_SET_REJECTED
    except Exception:
        _mark_sync_broken()  # 重置 sync 连接 + 冷却重连
        return None


def get_snapshot_sync(session_id: str) -> Optional[dict]:
    """同步读最新快照(轮询端点用 · Redis 跨 worker 安全)。"""
    r = _get_sync_redis()
    if r is None:
        return None
    try:
        raw = r.get(_snap_key(session_id))
        if raw:
            return json.loads(raw)
    except Exception:
        _mark_sync_broken()
    return None


def delete_snapshot_sync(session_id: str) -> None:
    r = _get_sync_redis()
    if r is not None:
        try:
            r.delete(_snap_key(session_id))
            r.delete(_seq_key(session_id))
            r.delete(_snapseq_key(session_id))  # [seq 守卫] 一并清 snapseq,防复用 sid 时旧哨兵拦住新快照
        except Exception:
            _mark_sync_broken()


async def run_subscriber(dispatch: Callable[[str, dict], Awaitable[None]],
                         stop_event: Optional[asyncio.Event] = None) -> None:
    """
    每 worker 一个常驻订阅循环(SPEC §6.3):psubscribe `progress:ch:*`;
    收到消息 → 解析 session_id + msg → 交 dispatch(交本 worker 持有该 sid 的 WS 投递)。
    断线指数退避重连 + 重订阅(latest-state:重连期中间进度可丢,新连接靠快照对齐)。
    此循环必须在**每个 web worker** 常驻；cron/prestart 运行域禁止启动。
    """
    backoff = _SUBSCRIBER_RECONNECT_INITIAL
    while stop_event is None or not stop_event.is_set():
        subscriber = None
        pubsub = None
        retry_delay = None
        subscribed_at = None
        try:
            # 长期 Pub/Sub 必须独占 client/socket_timeout=None；不得借 publish 的有限超时单例。
            subscriber = await _create_subscriber_redis()
            pubsub = subscriber.pubsub()
            pattern = f"{_CHAN_PREFIX}*"
            await asyncio.wait_for(
                pubsub.psubscribe(pattern),
                timeout=_SUBSCRIBER_HANDSHAKE_TIMEOUT,
            )
            await asyncio.wait_for(
                _wait_for_psubscribe_ack(pubsub, pattern),
                timeout=_SUBSCRIBER_HANDSHAKE_TIMEOUT,
            )
            subscribed_at = time.monotonic()
            logger.info("[ProgressBus] 订阅循环就绪 psubscribe progress:ch:*")
            heartbeat_counter = 0
            pending_heartbeat = None
            heartbeat_deadline = 0.0
            next_heartbeat_at = subscribed_at + _SUBSCRIBER_HEARTBEAT_INTERVAL
            while stop_event is None or not stop_event.is_set():
                now = time.monotonic()
                if pending_heartbeat is not None and now >= heartbeat_deadline:
                    raise TimeoutError("subscriber heartbeat PONG deadline exceeded")
                if pending_heartbeat is None and now >= next_heartbeat_at:
                    heartbeat_counter += 1
                    pending_heartbeat = f"progress-bus:{heartbeat_counter}:{int(now * 1000)}"
                    await asyncio.wait_for(
                        pubsub.ping(pending_heartbeat),
                        timeout=_SUBSCRIBER_HANDSHAKE_TIMEOUT,
                    )
                    heartbeat_deadline = time.monotonic() + _SUBSCRIBER_HEARTBEAT_TIMEOUT

                # timeout 是本次 poll 的本地等待上限，不是 socket_timeout；可及时响应 stop_event。
                poll_timeout = 1.0
                if pending_heartbeat is not None:
                    poll_timeout = max(
                        0.05,
                        min(1.0, heartbeat_deadline - time.monotonic()),
                    )
                m = await pubsub.get_message(timeout=poll_timeout)
                if not m:
                    continue
                if m.get("type") == "pong":
                    pong_data = m.get("data")
                    if isinstance(pong_data, bytes):
                        pong_data = pong_data.decode("utf-8", errors="replace")
                    if pending_heartbeat is not None and pong_data == pending_heartbeat:
                        pending_heartbeat = None
                        next_heartbeat_at = (
                            time.monotonic() + _SUBSCRIBER_HEARTBEAT_INTERVAL
                        )
                    continue
                if m.get("type") != "pmessage":
                    continue
                chan = m.get("channel") or ""
                if not chan.startswith(_CHAN_PREFIX):
                    continue
                sid = chan[len(_CHAN_PREFIX):]
                data = m.get("data")
                try:
                    msg = json.loads(data)
                except Exception:
                    continue
                try:
                    await dispatch(sid, msg)
                except Exception as e:
                    logger.warning(f"[ProgressBus] dispatch 异常 session={sid}: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # accept 后立刻掉线也属于连续失败，不能每次 psubscribe 就把退避洗回 1s。
            # 只有连接至少稳定跨过一个 health-check 周期，才把下一次重连恢复为初始延迟。
            if subscribed_at is not None and (
                time.monotonic() - subscribed_at >= _SUBSCRIBER_STABLE_SECONDS
            ):
                backoff = _SUBSCRIBER_RECONNECT_INITIAL
            retry_delay = min(backoff, _SUBSCRIBER_RECONNECT_MAX)
            logger.warning(
                f"[ProgressBus] 订阅循环断开 · {retry_delay:.0f}s 后重连(发布 client 不受影响): {e}"
            )
            backoff = min(backoff * 2, _SUBSCRIBER_RECONNECT_MAX)
        finally:
            for resource in (pubsub, subscriber):
                try:
                    await _close_async_resource(resource)
                except Exception:
                    pass
        if retry_delay is not None:
            if await _wait_for_retry_or_stop(stop_event, retry_delay):
                break


def _reset_for_test():
    """仅测试用:清全局单例。"""
    global _async_redis, _async_redis_retry_at, _sync_redis, _sync_redis_retry_at, _breaker
    global _async_publish_script, _sync_set_script
    _async_redis = None
    _async_redis_retry_at = 0.0
    _sync_redis = None
    _sync_redis_retry_at = 0.0
    _async_publish_script = None
    _sync_set_script = None
    _breaker = _Breaker()
