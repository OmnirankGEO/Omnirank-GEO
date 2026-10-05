"""
诊断并发闸(WORKERS=4 · SPEC §4 · 全局跨 worker 并发上限)
========================================================

病根(WORKERS>1):旧 `_diagnosis_semaphore = asyncio.Semaphore(5)` 是**每进程**内存信号量,
4 worker → 实际全局 20 并发,资源保护形同虚设(LLM key 池 / DB 连接 / RSS 都按单进程算)。

方案(SPEC §4):Redis **ZSET** 做全局并发槽,`member=run_token`、`score=过期时刻`。
- **acquire**:单 Lua 原子[清过期(ZREMRANGEBYSCORE)→ ZCARD → 未满则 ZADD] → 恰一次判定,跨 worker 一致;
- **renew**(心跳顺带续):ZADD 刷 score,槽已被收(reaped/过期)则返 0 → 调用方知租约已失;
- **release**(finally / sweeper 只删自己 token):ZREM member;
- **count**:清过期后 ZCARD。

Redis 故障语义(§6.6):本模块只如实返回 `redis_down`,**不自作主张 fail-open/closed**——
由调用方按 WORKERS 数决定(WORKERS>1 抢槽/freeze 前拒;WORKERS=1 回退进程内 semaphore 兜底,
保 I4 核心链路在 Redis 抖动时行为不变)。

cap = env `DIAGNOSIS_CONCURRENCY_CAP`(默认 8 · SPEC §4);slot TTL = env `DIAGNOSIS_SLOT_TTL_SECONDS`
(默认 180s = 3× 心跳 60s · worker 猝死后槽 ≤180s 自动释放,不永久占位)。

依赖:共享 `cache.redis_client.get_redis()`(db=0 · sync · 失败自愈)。Redis 操作亚毫秒 · 同步调用;
async 入口按需 `asyncio.to_thread` 包裹(admission 路径已大量 to_thread DB 调用)。
"""
from __future__ import annotations

import logging
import os
import time
from typing import NamedTuple, Optional

logger = logging.getLogger("GEO-DiagSlots")

_SLOTS_KEY = "diag:slots"  # ZSET member=run_token score=expiry_epoch


def _cap() -> int:
    try:
        return max(1, int(os.getenv("DIAGNOSIS_CONCURRENCY_CAP", "8")))
    except (TypeError, ValueError):
        return 8


def _ttl() -> int:
    try:
        return max(30, int(os.getenv("DIAGNOSIS_SLOT_TTL_SECONDS", "180")))
    except (TypeError, ValueError):
        return 180


class SlotResult(NamedTuple):
    acquired: bool      # 是否抢到槽(或本 token 已持有 = 幂等重入)
    current: int        # 当前占用数(清过期后)· redis_down 时为 -1
    redis_down: bool    # Redis 不可达(调用方按 WORKERS 决定 fail-open/closed)


# ---- Lua(全部把 now 从 Python 传入 ARGV · 不用 redis.call('TIME') 免复制/非确定性坑)----
# acquire:清过期 → 本 token 已在则续租返成功(幂等)→ 否则 ZCARD < cap 才 ZADD。
# KEYS[1]=slots  ARGV[1]=now ARGV[2]=cap ARGV[3]=member ARGV[4]=expiry
_ACQUIRE_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, ARGV[1])
if redis.call('ZSCORE', KEYS[1], ARGV[3]) then
  redis.call('ZADD', KEYS[1], ARGV[4], ARGV[3])
  return {1, redis.call('ZCARD', KEYS[1])}
end
local c = redis.call('ZCARD', KEYS[1])
if c < tonumber(ARGV[2]) then
  redis.call('ZADD', KEYS[1], ARGV[4], ARGV[3])
  return {1, c + 1}
end
return {0, c}
"""

# renew:仅当 member 仍在(未被收/未过期)才刷 score;否则返 0(租约已失 → 任务自我中止)。
# KEYS[1]=slots  ARGV[1]=now ARGV[2]=member ARGV[3]=expiry
_RENEW_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, ARGV[1])
if redis.call('ZSCORE', KEYS[1], ARGV[2]) then
  redis.call('ZADD', KEYS[1], ARGV[3], ARGV[2])
  return 1
end
return 0
"""

# count:清过期后 ZCARD。 KEYS[1]=slots ARGV[1]=now
_COUNT_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, ARGV[1])
return redis.call('ZCARD', KEYS[1])
"""

_acquire_script = None
_renew_script = None
_count_script = None


def _get_scripts(r):
    """懒注册 Lua(绑定当前 client)。"""
    global _acquire_script, _renew_script, _count_script
    if _acquire_script is None:
        _acquire_script = r.register_script(_ACQUIRE_LUA)
    if _renew_script is None:
        _renew_script = r.register_script(_RENEW_LUA)
    if _count_script is None:
        _count_script = r.register_script(_COUNT_LUA)


def acquire_slot(run_token: str, now: Optional[float] = None) -> SlotResult:
    """抢并发槽。返回 SlotResult(acquired, current, redis_down)。

    - redis_down=True:Redis 不可达 → 调用方按 WORKERS 决定(>1 拒 / =1 回退 semaphore);
    - acquired=True:抢到(或本 token 已持有幂等重入);
    - acquired=False:已达 cap,拒(文案"当前已有 N 个诊断在跑")。
    """
    from cache.redis_client import get_redis
    r = get_redis()
    if r is None:
        return SlotResult(acquired=False, current=-1, redis_down=True)
    now = time.time() if now is None else now
    try:
        _get_scripts(r)
        res = _acquire_script(keys=[_SLOTS_KEY],
                              args=[now, _cap(), run_token, now + _ttl()], client=r)
        # Lua 返回 {1/0, current}
        ok = bool(res and int(res[0]) == 1)
        cur = int(res[1]) if res and len(res) > 1 else -1
        return SlotResult(acquired=ok, current=cur, redis_down=False)
    except Exception as e:
        logger.warning(f"[DiagSlots] acquire 异常(视作 redis_down 交调用方决策): {e}")
        return SlotResult(acquired=False, current=-1, redis_down=True)


def renew_slot(run_token: str, now: Optional[float] = None) -> Optional[bool]:
    """心跳续租。返回 True=续上 / False=**槽已失**(被收/过期 → 任务应自我中止)/ None=Redis 抖动(不判失)。"""
    from cache.redis_client import get_redis
    r = get_redis()
    if r is None:
        return None
    now = time.time() if now is None else now
    try:
        _get_scripts(r)
        res = _renew_script(keys=[_SLOTS_KEY], args=[now, run_token, now + _ttl()], client=r)
        return bool(res and int(res) == 1)
    except Exception:
        return None


def release_slot(run_token: str) -> bool:
    """释放本 token 的槽(finally / sweeper 只删自己)。Redis 不可达返 False(槽会自然 TTL 过期兜底)。"""
    from cache.redis_client import get_redis
    r = get_redis()
    if r is None:
        return False
    try:
        r.zrem(_SLOTS_KEY, run_token)
        return True
    except Exception as e:
        logger.warning(f"[DiagSlots] release 异常 token={run_token}: {e}")
        return False


def count_slots(now: Optional[float] = None) -> Optional[int]:
    """当前占用数(清过期后)。Redis 不可达返 None。"""
    from cache.redis_client import get_redis
    r = get_redis()
    if r is None:
        return None
    now = time.time() if now is None else now
    try:
        _get_scripts(r)
        return int(_count_script(keys=[_SLOTS_KEY], args=[now], client=r))
    except Exception:
        return None


def cap() -> int:
    """当前并发上限(供文案/告警读)。"""
    return _cap()


def _reset_for_test():
    global _acquire_script, _renew_script, _count_script
    _acquire_script = None
    _renew_script = None
    _count_script = None
