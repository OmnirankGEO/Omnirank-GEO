"""
per-job "至多一次 tick" DB claim(WORKERS=4 · SPEC §2.2 · FF3 硬化)
====================================================================

cron 已收敛到 ROLE=cron + LeaderLock 选主(server.py),正常态只有一个 leader 触发 job。
本 claim 是**第二层**防护,盖住"蓝绿换主/续租竞争"的 handover 窄窗:老 leader 丢租那一刻、
新 leader 刚接管,可能都想触发同一 scheduled tick。sched_job_runs UNIQUE(job_name, scheduled_at)
保证恰一个 claim 成功。

FF3 硬化(boss 收口#2a/2b):
- **claim_token 身份**:每次 claim 生成唯一 token;heartbeat / finish 都 **CAS 只认自己的 token**
  (被 reclaim 后旧持有者 finish 打不动别人的行,不会误标 done)。
- **fail-CLOSED 默认**:claim 查询异常 → **不执行**(money/高风险 job 绝不冒双跑风险)。
  可靠性由 prestart(pg_advisory_lock 单飞 + 迁移失败非零退出,见 scripts/prestart)+ server 导入期
  幂等迁移共同保证 sched_job_runs 表必存在。纯 housekeeping job 可显式 fail_open=True 放行。
- 重复执行的**最终**兜底仍是各 job 业务幂等键 + §3 终态 CAS(见 job 幂等审计清单)。

FF10 fencing 硬化(boss 复审 P0-1 · 死 claim 复活不得产生第二次副作用):
- **heartbeat CAS 返回是否仍持有**:True=仍持有 / False=**已确定丢失**(行被他人接管/状态变更/删除)/
  None=DB 抖动无法判定(不判丢租,下拍再试)。丢租(False)→ 心跳线程置 `lease_lost` Event → 停止续心跳。
  合作式 job 体内可调 `lease_lost()` 在不可逆副作用前中止(本 tick claim 已失)。
- **reclaimable 默认 False(fail-closed 反 double-execution)**:money/副作用 job **禁止**自动 stale
  reclaim —— 老 leader 心跳死不代表老执行体已停(可能只是慢/GC/网络),盲目换 token 重跑 = 老体
  可能仍在退款/结算 + 新体又跑一遍 = **双副作用**。故死 claim **不自动复活**,转 `retry_pending`
  人工复核(下一 tick 的新 scheduled_at 会正常新起,不永久卡)。只有**协作式检查 lease 或天然幂等**
  的 job 才可显式 `reclaimable=True` 换回自动接管。同 tick 双 leader 的正确性仍由 INSERT UNIQUE 保证
  (与 reclaim 无关):第二 leader INSERT 冲突立即返 None,不双跑。
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import logging
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional


def scheduler_sync_callable(fn):
    """Adapt an async-capable callable for APScheduler's thread executor.

    BackgroundScheduler does not await coroutine return values. Keep
    ``sched_claim`` async-aware for direct callers, but drive the returned
    awaitable to completion at the scheduler boundary so a job cannot be
    logged as successful while doing no work.
    """
    @functools.wraps(fn)
    def runner(*args, **kwargs):
        result = fn(*args, **kwargs)
        if inspect.isawaitable(result):
            return asyncio.run(result)
        return result

    return runner

logger = logging.getLogger("GEO-SchedClaim")

_HEARTBEAT_INTERVAL = 30       # s · 回收阈值 90s = 3 拍
_STALE_RECLAIM_SECONDS = 90

# 当前 tick 的 lease_lost Event(仅在 sched_claim 包裹的 job 体内有值)· 供合作式 job 在副作用前自查
_CURRENT_LEASE: contextvars.ContextVar = contextvars.ContextVar("sched_claim_lease", default=None)


def lease_lost() -> bool:
    """job 体内可调:True=本 tick 的 claim 已**确定丢失**(应在不可逆副作用前中止)。
    无活动 lease(未经 sched_claim 包裹 / 未丢租)返回 False(默认放行 · 不误伤)。"""
    ev = _CURRENT_LEASE.get()
    return bool(ev is not None and ev.is_set())


def _bucket(period_seconds: int) -> datetime:
    """把 now floor 到 period 网格 → 同 tick 的两 leader 落同 scheduled_at。"""
    p = max(1, int(period_seconds))
    b = int(time.time() // p) * p
    return datetime.fromtimestamp(b, tz=timezone.utc)


def _new_token() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:12]}"


def claim_job_run(job_name: str, scheduled_at: datetime, epoch: Optional[int] = None,
                  fail_open: bool = False, reclaimable: bool = False) -> Optional[str]:
    """短事务抢 claim。返回 claim_token(赢,应执行)或 None(被抢/死 claim/失败-fail-closed → skip)。

    ① 首次 INSERT 成功 → 赢(第一 claimer,恒放行)。
    ② 冲突(行已存在):
       - **reclaimable=True**(天然幂等/协作式 job):既有 claim 心跳死(>90s 且 status='claimed')→
         原子 reclaim 换 token 返回(自动接管);活跃/终态 → None。
       - **reclaimable=False(默认 · money/副作用 · FF10 fail-closed)**:**绝不自动复活死 claim**
         (老执行体可能仍在跑 → 双副作用)。死 claim(>90s)→ 转 `outcome_unknown`(**业务结果未知**态 · 非
         "可随手重跑" · 先核对业务结果再受控处置)并返 None;活跃/终态 → None。下一 tick 新 scheduled_at 正常新起。
    claim 查询异常:fail_open=True 返回本地 token 放行(退回选主主防线,非 money);
    否则 **fail-CLOSED 返 None**(money/高风险 skip,不双跑扣钱)。
    """
    token = _new_token()
    try:
        from db.connection import get_db
        with get_db() as conn:
            cur = conn.cursor()
            # ① 尝试首次 claim
            cur.execute(
                """
                INSERT INTO sched_job_runs (job_name, scheduled_at, epoch, status, claim_token, claim_heartbeat_at)
                VALUES (%s, %s, %s, 'claimed', %s, NOW())
                ON CONFLICT (job_name, scheduled_at) DO NOTHING
                RETURNING claim_token
                """,
                (job_name, scheduled_at, epoch, token),
            )
            if cur.fetchone():
                conn.commit()
                return token
            # ② 冲突
            if reclaimable:
                # 天然幂等/协作式:死 claim(>90s)原子接管换 token
                cur.execute(
                    """
                    UPDATE sched_job_runs
                       SET claim_token = %s, claim_heartbeat_at = NOW(), epoch = %s, status = 'claimed'
                     WHERE job_name = %s AND scheduled_at = %s
                       AND status = 'claimed'
                       AND claim_heartbeat_at < NOW() - INTERVAL '90 seconds'
                    RETURNING claim_token
                    """,
                    (token, epoch, job_name, scheduled_at),
                )
                reclaimed = cur.fetchone()
                conn.commit()
                if reclaimed:
                    logger.warning(f"[SchedClaim] reclaim 死 claim {job_name}@{scheduled_at.isoformat()}"
                                   f"(reclaimable=True · 幂等/协作式 · 心跳>90s)")
                    return token
                return None  # 活跃 claim 被他人持有 / 已终态 → skip
            # reclaimable=False(默认 money/副作用):**不复活** · 死 claim 转 outcome_unknown(业务结果未知)
            cur.execute(
                """
                UPDATE sched_job_runs
                   SET status = 'outcome_unknown',
                       last_error = COALESCE(last_error, '')
                                    || 'stale_lease_abandoned:possible_partial_execution;outcome_unknown_verify_before_controlled_resolution'
                 WHERE job_name = %s AND scheduled_at = %s
                   AND status = 'claimed'
                   AND claim_heartbeat_at < NOW() - INTERVAL '90 seconds'
                RETURNING id
                """,
                (job_name, scheduled_at),
            )
            flagged = cur.fetchone()
            conn.commit()
            if flagged:
                logger.error(f"[SchedClaim] {job_name}@{scheduled_at.isoformat()} 检测到死 claim(心跳>90s)· "
                             f"**非 reclaimable(money/副作用)→ 转 outcome_unknown · 不自动重跑**(防旧执行体双副作用)· "
                             f"业务结果未知 · 需先核对业务结果再受控处置(禁盲目重跑)")
            return None  # 活跃 claim 被他人持有 / 已终态 / 死 claim 已转 manual → 一律 skip,不执行
    except Exception as e:
        if fail_open:
            logger.warning(f"[SchedClaim] claim {job_name} 异常 · fail-OPEN 放行(非 money · 退回选主兜底): {e}")
            return token
        logger.error(f"[SchedClaim] claim {job_name} 异常 · **fail-CLOSED 跳过**(money/高风险不冒双跑): {e}")
        return None


def heartbeat_job_run(job_name: str, scheduled_at: datetime, token: str) -> Optional[bool]:
    """续心跳 · CAS 只认自己的 token。返回:
    True=仍持有(rowcount>0);False=**已确定丢失**(rowcount==0:行被他人接管/状态变更/删除);
    None=DB 抖动无法判定(不判丢租,下拍再试;回收阈值 90s = 3 拍容一时)。"""
    try:
        from db.connection import get_db
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE sched_job_runs SET claim_heartbeat_at = NOW() "
                "WHERE job_name = %s AND scheduled_at = %s AND claim_token = %s AND status = 'claimed'",
                (job_name, scheduled_at, token),
            )
            affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception:
        return None  # 不确定 · 保留 claim,下拍再试(单次抖动不判丢租,避免误伤长任务)


def finish_job_run(job_name: str, scheduled_at: datetime, token: str,
                   status: str = "done", last_error: Optional[str] = None) -> None:
    """标终态 · **CAS 只认自己的 token**:被 reclaim 后旧持有者 finish 不会误标别人的行。"""
    try:
        from db.connection import get_db
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE sched_job_runs
                   SET status = %s, last_error = %s, finished_at = NOW()
                 WHERE job_name = %s AND scheduled_at = %s AND claim_token = %s AND status = 'claimed'
                """,
                (status, (last_error or None), job_name, scheduled_at, token),
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"[SchedClaim] finish {job_name} 标 {status} 失败(不影响业务): {e}")


def _claim_and_start_hb(job_name: str, period_seconds: int, fail_open: bool, reclaimable: bool):
    """抢 claim + 起心跳线程。返回 (scheduled_at, token, stop, lease_lost);token=None 表示未抢到/fail-closed。
    心跳确定丢租(heartbeat→False)时置 lease_lost Event 并停止续心跳(合作式 job 体可自查)。"""
    scheduled_at = _bucket(period_seconds)
    epoch = None
    try:
        from cache.leader_lock import read_epoch
        epoch = read_epoch()
    except Exception:
        epoch = None
    token = claim_job_run(job_name, scheduled_at, epoch, fail_open=fail_open, reclaimable=reclaimable)
    if token is None:
        return scheduled_at, None, None, None
    stop = threading.Event()
    lease_lost_ev = threading.Event()

    def _hb():
        while not stop.wait(_HEARTBEAT_INTERVAL):
            held = heartbeat_job_run(job_name, scheduled_at, token)
            if held is False:                 # 确定丢租(rowcount==0)· None(抖动)不判丢
                lease_lost_ev.set()
                logger.error(f"[SchedClaim] {job_name} **lease 已丢**(心跳 CAS 命中 0 行 · 行被接管/变更)· "
                             f"合作式 job 体应在不可逆副作用前中止")
                return

    threading.Thread(target=_hb, daemon=True, name=f"sched-claim-hb-{job_name}").start()
    return scheduled_at, token, stop, lease_lost_ev


def sched_claim(job_name: str, period_seconds: int, fail_open: bool = False,
                reclaimable: bool = False):
    """装饰器:给 job callable 套 tick 级 claim(token/CAS/lease fencing)。**sync 或 async job 均可**
    (async job 返回 async wrapper,await 真正业务后才 finish)。

    fail_open 默认 False(fail-CLOSED · money/高风险);纯 housekeeping 可传 True。
    reclaimable 默认 False(FF10 · money/副作用**禁自动复活死 claim**,防旧执行体双副作用);
      仅天然幂等或体内自查 `lease_lost()` 的 job 才可传 True 换回自动接管。
    """
    def deco(fn):
        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*args, **kwargs):
                scheduled_at, token, stop, lease_ev = _claim_and_start_hb(
                    job_name, period_seconds, fail_open, reclaimable)
                if token is None:
                    return None  # 已被抢 / 死 claim 未复活 / fail-closed skip
                _lease_reset = _CURRENT_LEASE.set(lease_ev)
                status, err = "done", None
                try:
                    return await fn(*args, **kwargs)
                except Exception as e:
                    status, err = "failed", str(e)[:480]
                    raise
                finally:
                    _CURRENT_LEASE.reset(_lease_reset)
                    stop.set()
                    finish_job_run(job_name, scheduled_at, token, status, err)
            return awrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            scheduled_at, token, stop, lease_ev = _claim_and_start_hb(
                job_name, period_seconds, fail_open, reclaimable)
            if token is None:
                return None  # 已被抢 / 死 claim 未复活 / fail-closed skip
            _lease_reset = _CURRENT_LEASE.set(lease_ev)
            status, err = "done", None
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                status, err = "failed", str(e)[:480]
                raise
            finally:
                _CURRENT_LEASE.reset(_lease_reset)
                stop.set()
                finish_job_run(job_name, scheduled_at, token, status, err)
        return wrapper
    return deco
