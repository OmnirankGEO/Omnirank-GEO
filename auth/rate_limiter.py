"""
登录失败限流(WORKERS=4 · SPEC §8 · 两键模型 + 单 Lua 原子)
====================================================================
保护暴力破解:同一用户名 5 分钟内连续失败 5 次 → 锁定 15 分钟。

为什么改(WORKERS=4 病根):
  原实现用进程内 `_attempts` dict —— 单 worker 有效,但 4 worker 各持一份 →
  阈值被稀释 4 倍(攻击者拿 5×4=20 次),锁定也不跨 worker 生效(I4④)。
  改为 Redis 全局:所有 worker 共享同一计数与锁 → 真·15 分钟全局锁定。

正常态(Redis 可用)· 两键模型:
  - attempts ZSET `ratelimit:login:att:{u}` —— 失败时间戳滑动窗口(300s)。
  - lock  key   `ratelimit:login:lock:{u}` —— 触发即 SET PX 900_000(真 15 分钟锁)。
  record 走**单 Lua 原子**(prune→zadd→zcard→满则置 lock),避免 check-then-act 竞态;
  成功登录 DEL 两键。check 只读 lock 的 PTTL(读天然原子)。

Redis 故障降级(SPEC §8 修订 · 非等价语义,已如实标注):
  回落进程内本地计数,但阈值**收紧为 2 次/300s**(4 worker 全局上界 8 次,同数量级),
  本地锁定 900s + ERROR 告警。**不做 DB 兜底**(§12:登录路径不加 DB 写)。

对外签名与语义 100% 保持(check_rate_limit / record_failed_attempt / clear_attempts /
get_attempt_count),api/auth_api.py 无需改动。
"""

import logging
import time
import uuid
from collections import defaultdict
from typing import Dict, List, Tuple

from cache.redis_client import get_redis

logger = logging.getLogger("GEO-LoginRateLimit")

# 配置(语义基准 · I4④ 明示"真 15 分钟全局锁")
MAX_ATTEMPTS = 5          # 最大失败次数(正常态)
ATTEMPT_WINDOW = 300      # 统计窗口(秒)= 5 分钟
LOCKOUT_DURATION = 900    # 锁定时长(秒)= 15 分钟

# Redis 故障降级(SPEC §8):本地每 worker 阈值收紧,使 4 worker 全局上界≈8(同数量级)
LOCAL_DEGRADE_MAX = 2

# ---- Redis 键 ----
def _att_key(username: str) -> str:
    return f"ratelimit:login:att:{username}"


def _lock_key(username: str) -> str:
    return f"ratelimit:login:lock:{username}"


# ---- 单 Lua 原子:记录一次失败 → 达阈值即置 900s 锁 ----
# KEYS[1]=attempts zset  KEYS[2]=lock key
# ARGV[1]=now  ARGV[2]=window_start(now-300)  ARGV[3]=unique_member
# ARGV[4]=max_attempts  ARGV[5]=lockout_ms  ARGV[6]=attempts_ttl_ms
# 返回:1=本次触发/仍在锁定(locked)  0=未触发
_RECORD_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, ARGV[2])
redis.call('ZADD', KEYS[1], ARGV[1], ARGV[3])
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[6]))
local cnt = redis.call('ZCARD', KEYS[1])
if cnt >= tonumber(ARGV[4]) then
  redis.call('SET', KEYS[2], '1', 'PX', tonumber(ARGV[5]))
  return 1
end
return 0
"""

_record_script = None


def _ensure_record_script(r):
    global _record_script
    if _record_script is None:
        _record_script = r.register_script(_RECORD_LUA)
    return _record_script


# ---- 降级:进程内本地计数(仅 Redis 故障期 · 非等价语义)----
_local_attempts: Dict[str, List[float]] = defaultdict(list)
_local_lock_until: Dict[str, float] = {}
_last_degrade_alert_at: float = 0.0
_DEGRADE_ALERT_INTERVAL = 60  # ERROR 告警限频(秒)· 防 Redis 抖动刷屏


def _alert_degraded():
    """Redis 故障期 ERROR 告警(限频)· 交付文档标注:故障期为降级语义,非全局等价。"""
    global _last_degrade_alert_at
    now = time.time()
    if now - _last_degrade_alert_at >= _DEGRADE_ALERT_INTERVAL:
        _last_degrade_alert_at = now
        logger.error(
            "[LoginRateLimit] Redis 不可用 · 登录限流降级为本地每 worker 2 次/300s"
            "(非全局等价 · WORKERS=4 下全局上界≈8)· 请尽快恢复 Redis"
        )


def _local_check(username: str) -> Tuple[bool, str]:
    now = time.time()
    until = _local_lock_until.get(username, 0.0)
    if now < until:
        minutes = int(until - now) // 60 + 1
        return False, f"登录失败次数过多，请 {minutes} 分钟后再试"
    return True, ""


def _local_record(username: str):
    now = time.time()
    _local_attempts[username] = [t for t in _local_attempts[username] if now - t < ATTEMPT_WINDOW]
    _local_attempts[username].append(now)
    if len(_local_attempts[username]) >= LOCAL_DEGRADE_MAX:
        _local_lock_until[username] = now + LOCKOUT_DURATION


def _local_clear(username: str):
    _local_attempts.pop(username, None)
    _local_lock_until.pop(username, None)


# ================= 对外 API(签名/语义保持)=================

def check_rate_limit(username: str) -> Tuple[bool, str]:
    """
    检查用户名是否被限流(登录前调用)。

    Returns:
        (allowed, message) —— (True, "") 允许;(False, "错误信息") 被限流。
    """
    r = get_redis()
    if r is None:
        _alert_degraded()
        return _local_check(username)
    try:
        pttl = r.pttl(_lock_key(username))  # >0=锁定剩余 ms;-1 无过期(不应出现);-2 无键
        if isinstance(pttl, int) and pttl > 0:
            minutes = pttl // 60000 + 1
            return False, f"登录失败次数过多，请 {minutes} 分钟后再试"
        return True, ""
    except Exception as e:
        logger.warning(f"[LoginRateLimit] check Redis 异常 · 降级本地: {e}")
        _alert_degraded()
        return _local_check(username)


def record_failed_attempt(username: str):
    """记录一次失败的登录尝试(达阈值即置 15 分钟全局锁)。"""
    r = get_redis()
    if r is None:
        _alert_degraded()
        _local_record(username)
        return
    try:
        now = time.time()
        member = f"{now:.6f}:{uuid.uuid4().hex[:8]}"
        script = _ensure_record_script(r)
        script(
            keys=[_att_key(username), _lock_key(username)],
            args=[
                repr(now),                       # ARGV[1] now(score)
                repr(now - ATTEMPT_WINDOW),      # ARGV[2] window_start
                member,                          # ARGV[3] unique member
                MAX_ATTEMPTS,                    # ARGV[4]
                LOCKOUT_DURATION * 1000,         # ARGV[5] lockout ms
                LOCKOUT_DURATION * 1000,         # ARGV[6] attempts ttl ms(覆盖锁定期)
            ],
            client=r,
        )
    except Exception as e:
        logger.warning(f"[LoginRateLimit] record Redis 异常 · 降级本地: {e}")
        _alert_degraded()
        _local_record(username)


def clear_attempts(username: str):
    """登录成功后清除失败记录 + 解锁(DEL 两键)。"""
    r = get_redis()
    if r is None:
        _local_clear(username)
        return
    try:
        r.delete(_att_key(username), _lock_key(username))
    except Exception as e:
        logger.warning(f"[LoginRateLimit] clear Redis 异常: {e}")
    _local_clear(username)  # 兜底清本地(降级期残留)


def get_attempt_count(username: str) -> int:
    """获取当前窗口内失败次数(调试用)。"""
    r = get_redis()
    if r is None:
        now = time.time()
        return len([t for t in _local_attempts.get(username, []) if now - t < ATTEMPT_WINDOW])
    try:
        now = time.time()
        return int(r.zcount(_att_key(username), now - ATTEMPT_WINDOW, now))
    except Exception:
        return 0
