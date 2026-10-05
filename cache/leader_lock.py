"""
分布式选主锁(WORKERS=4 · SPEC §2.1 / D3)—— 全仓第一处 Redis Lua。
====================================================================

为什么新建(现有 `cache.redis_client.acquire_lock` 不能用于选主):
  - acquire_lock **两分支都 fail-OPEN**(Redis 挂 return True + except return True)→ WORKERS=4
    Redis 抖一下就 4 个"leader"齐发 cron(重复扣费/重复结算)。选主必须 **fail-CLOSED**。
  - release_lock **裸 DEL 不验 owner** → 任意 worker 可删他人锁(过期后被慢持有者误删)。
  - renew 是 server.py 内联 **非原子 GET-then-EXPIRE**(TOCTOU)。
  本模块用 Lua CAS 一次性解决三者:原子获取 / owner-CAS 续租 / owner-CAS 释放。

D3 回滚互斥(整包回滚序:先停新 cron 再起旧 web):
  一个 Lua **原子获取三把键** = `sched:leader` + 旧 `scheduler:lock` + 旧
  `research-monitor-scheduler:lock`(要么全拿要么全不拿)。旧 web-scheduler 代码仍用
  `redis_client.acquire_lock("scheduler:lock", ...)`(SET NX)抢旧键 → 新旧不可能同时持有
  → 天然互斥。**每次任期生成全新 token**(不复用进程 token)。

sched:epoch(job fencing · SPEC §2.2):
  任期开始 `INCR sched:epoch` → 副作用 job 开始时捕获 epoch,关键写库/结算前复查;不符即中止。
  **独立 Redis 计数器**,**不复用 services/config_epoch.py 的 EpochCache**——后者是 Postgres 撑的
  配置失效计数器(2s 节流探针 + fail-soft-to-stale),做 leader fence 不安全(需强读不陈旧)。
  注意(§2.2):epoch 只是**优化/快速失败**,恰一次的**正确性在 DB**(sched_job_runs UNIQUE +
  业务幂等键 + 终态 CAS),不靠 Redis 检查。

只用 cache.redis_client 的同步单例(db=0 · 与旧 scheduler:lock 同命名空间 → 互斥成立)。
同步实现:契合 server.py 调度启动(sync @app.on_event)与续租后台线程(sync)。
"""
from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from typing import List, Optional

from cache.redis_client import get_redis

logger = logging.getLogger("GEO-LeaderLock")

# D3 三把键(顺序固定 · Lua 内逐一处理)
DEFAULT_LEADER_KEYS: List[str] = [
    "sched:leader",
    "scheduler:lock",
    "research-monitor-scheduler:lock",
]
EPOCH_KEY = "sched:epoch"

# ---- Lua 脚本 ----
# 原子获取:三键全空(或已被本 token 持有)才拿;拿则 SET PX 三键 + INCR epoch;返回新 epoch(>0),否则 0
_ACQUIRE_LUA = """
for i=1,#KEYS do
  local v = redis.call('GET', KEYS[i])
  if v and v ~= ARGV[1] then
    return 0
  end
end
for i=1,#KEYS do
  redis.call('SET', KEYS[i], ARGV[1], 'PX', tonumber(ARGV[2]))
end
return redis.call('INCR', ARGV[3])
"""

# owner-CAS 续租:三键都仍归本 token 才 PEXPIRE 续期;任一不符返 0(=丢租,调用方必须停止派发)
_RENEW_LUA = """
for i=1,#KEYS do
  if redis.call('GET', KEYS[i]) ~= ARGV[1] then
    return 0
  end
end
for i=1,#KEYS do
  redis.call('PEXPIRE', KEYS[i], tonumber(ARGV[2]))
end
return 1
"""

# owner-CAS 释放:只删仍归本 token 的键(防误删他人)
_RELEASE_LUA = """
local n = 0
for i=1,#KEYS do
  if redis.call('GET', KEYS[i]) == ARGV[1] then
    redis.call('DEL', KEYS[i])
    n = n + 1
  end
end
return n
"""


class LeaderLock:
    """
    选主锁实例。用法:
        lock = LeaderLock(ttl_seconds=60)
        epoch = lock.acquire()          # None=没抢到(含 Redis 故障 fail-closed)
        if epoch is not None:
            ... 我是 leader(任期 epoch)· 后台每 RENEW_INTERVAL 调 lock.renew()
            if not lock.renew(): 停止派发/pause scheduler
            ... 退出时 lock.release()
    """

    def __init__(self, keys: Optional[List[str]] = None, epoch_key: str = EPOCH_KEY,
                 ttl_seconds: int = 60):
        self.keys = list(keys) if keys else list(DEFAULT_LEADER_KEYS)
        self.epoch_key = epoch_key
        self.ttl_ms = int(ttl_seconds * 1000)
        self.token: Optional[str] = None
        self.epoch: Optional[int] = None
        self._acquire = None
        self._renew = None
        self._release = None

    def _ensure_scripts(self, r) -> bool:
        if self._acquire is None:
            try:
                self._acquire = r.register_script(_ACQUIRE_LUA)
                self._renew = r.register_script(_RENEW_LUA)
                self._release = r.register_script(_RELEASE_LUA)
            except Exception as e:
                logger.warning(f"[LeaderLock] register_script 失败: {e}")
                self._acquire = self._renew = self._release = None
                return False
        return True

    @staticmethod
    def _new_token() -> str:
        return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:12]}"

    def acquire(self) -> Optional[int]:
        """尝试成为 leader。成功返回任期 epoch(>0);失败/Redis 故障返回 None(fail-CLOSED)。"""
        r = get_redis()
        if r is None:
            return None  # fail-CLOSED(现 acquire_lock 是 fail-OPEN — 这里刻意相反)
        if not self._ensure_scripts(r):
            return None
        token = self._new_token()
        try:
            res = self._acquire(keys=self.keys, args=[token, self.ttl_ms, self.epoch_key], client=r)
            epoch = int(res) if res is not None else 0
        except Exception as e:
            logger.warning(f"[LeaderLock] acquire 异常 · fail-closed 不成为 leader: {e}")
            return None
        if epoch > 0:
            self.token = token
            self.epoch = epoch
            logger.info(f"[LeaderLock] 成为 leader · epoch={epoch} · token={token}")
            return epoch
        return None

    def renew(self) -> bool:
        """续租三键。仍是 owner 返 True;丢租/Redis 故障返 False → 调用方必须停止派发(fail-CLOSED)。"""
        if not self.token:
            return False
        r = get_redis()
        if r is None:
            logger.warning("[LeaderLock] renew 时 Redis 不可用 · 视为丢租(fail-closed)")
            return False
        if not self._ensure_scripts(r):
            return False
        try:
            ok = int(self._renew(keys=self.keys, args=[self.token, self.ttl_ms], client=r)) == 1
        except Exception as e:
            logger.warning(f"[LeaderLock] renew 异常 · 视为丢租: {e}")
            return False
        if not ok:
            logger.warning(f"[LeaderLock] 丢租(token={self.token} 已非三键持有者)· 放弃 leadership")
            self.token = None
            self.epoch = None
        return ok

    def release(self) -> None:
        """让位:owner-CAS 删除仍归本 token 的键。"""
        if not self.token:
            return
        r = get_redis()
        if r is not None and self._ensure_scripts(r):
            try:
                self._release(keys=self.keys, args=[self.token], client=r)
            except Exception as e:
                logger.warning(f"[LeaderLock] release 异常(忽略 · 键会自然过期): {e}")
        logger.info(f"[LeaderLock] 释放 leadership · token={self.token}")
        self.token = None
        self.epoch = None

    def is_leader(self) -> bool:
        return self.token is not None


def read_epoch(r=None) -> Optional[int]:
    """读当前 sched:epoch(job fencing 用)。Redis 故障返 None → job 只能靠 DB 幂等键兜底(§2.2)。"""
    r = r or get_redis()
    if r is None:
        return None
    try:
        v = r.get(EPOCH_KEY)
        return int(v) if v is not None else 0
    except Exception:
        return None
