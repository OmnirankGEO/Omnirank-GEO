"""
调度控制面(WORKERS=4 · SPEC §7)—— 手动触发命令队列 + cron 状态上报。
====================================================================

病根(cron 收敛到 ROLE=cron 后):
  - admin `POST /api/scheduler/trigger/{job_id}` 落在随机 web worker,但 scheduler 只在 cron leader
    running → web 上直接触发要么打不到 job、要么在 web 进程跑副作用(绕过 leader,不恰一次)。
  - `GET /api/scheduler/status` 在 web 读到的是"未 start 的空 scheduler" → 误报 running=false/0 job。

修法:
  - **触发走命令队列**:web 端点 INSERT sched_commands(command_id UNIQUE · ON CONFLICT DO NOTHING);
    cron leader 每 10s FOR UPDATE SKIP LOCKED 原子领取 pending → 用 scheduler.modify(next_run_time=now)
    重排该 job 立即跑 → 标 done/failed。claim 心跳死(90s)可回收重试(kill cron 后另一 leader 续)。
  - **状态走 Redis**:cron leader 每 30s 写 sched:status(两 scheduler 合并 job 列表 + running + epoch),
    TTL 90s;web 端点只读 Redis,心跳消失显 degraded(如实)。

正确性:命令恰一次靠 sched_commands.command_id UNIQUE(建)+ 领取 FOR UPDATE SKIP LOCKED(领);
  job 本体重复由其自身 §2.2 claim/业务幂等兜底。状态是软信息,Redis 不可用即降级。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import socket
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("GEO-SchedControl")

_STATUS_KEY = "sched:status"
_STATUS_TTL = 90
_CMD_HEARTBEAT_INTERVAL = 30   # s · 命令执行中续心跳;stale reclaim 阈值 90s = 3 拍


def _new_token() -> str:
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:12]}"


# ---------------- 命令队列(web 端 enqueue / leader 端 consume)----------------

def enqueue_command(job_name: str, requested_by: Optional[int] = None,
                    args: Optional[dict] = None, command_id: Optional[str] = None) -> Dict[str, Any]:
    """web 端:入队一条手动触发命令(幂等:同 command_id 只建一次)。返回 {command_id, status}。"""
    cid = command_id or uuid.uuid4().hex
    try:
        from db.connection import get_db
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO sched_commands (command_id, job_name, args, status, requested_by, created_at)
                VALUES (%s, %s, %s, 'pending', %s, NOW())
                ON CONFLICT (command_id) DO NOTHING
                """,
                (cid, job_name, json.dumps(args or {}), requested_by),
            )
            conn.commit()
        return {"command_id": cid, "status": "pending", "job_name": job_name}
    except Exception as e:
        logger.error(f"[SchedControl] enqueue {job_name} 失败: {e}")
        return {"command_id": cid, "status": "error", "error": str(e)[:200]}


def get_command_status(command_id: str) -> Optional[Dict[str, Any]]:
    """web 端:轮询命令执行结果。"""
    try:
        from db.connection import get_db
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT command_id, job_name, status, result, last_error, created_at, claimed_at "
                "FROM sched_commands WHERE command_id = %s",
                (command_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        if isinstance(row, dict):
            return dict(row)
        cols = ["command_id", "job_name", "status", "result", "last_error", "created_at", "claimed_at"]
        return dict(zip(cols, row))
    except Exception as e:
        logger.warning(f"[SchedControl] get_command_status 失败: {e}")
        return None


def _find_job(job_id: str):
    """在两个 scheduler 里找 job_id → 返回 (scheduler, job) 或 (None, None)。"""
    try:
        from api.scheduler import get_scheduler
        sch = get_scheduler()
        j = sch.get_job(job_id)
        if j is not None:
            return sch, j
    except Exception:
        pass
    try:
        from scheduler import scheduler as root_sched
        if root_sched is not None:
            j = root_sched.get_job(job_id)
            if j is not None:
                return root_sched, j
    except Exception:
        pass
    return None, None


def consume_commands_once() -> Dict[str, Any]:
    """cron leader job(每 10s):原子领取 pending 命令 → 重排目标 job 立即跑 → 标 done/failed。

    仅在 cron leader 触发(注册进共享 scheduler,随 leader start);非 leader 不跑。
    领取用 FOR UPDATE SKIP LOCKED 保证并发/换主下恰一个消费者领到一条。
    """
    from services.cron_gate import cron_should_fire
    if not cron_should_fire():
        return {"skipped": "not_leader"}

    token = _new_token()
    epoch = None
    try:
        from cache.leader_lock import read_epoch
        epoch = read_epoch()
    except Exception:
        epoch = None

    # 0) [FF10/FF14 P0-1/P1-5] 先回收**死 claimed 命令**(心跳>90s)→ 转 outcome_unknown · **不自动重跑**。
    #    老 leader 心跳死不代表老执行体已停(可能只是丢心跳仍在跑副作用),盲目复活/重触发 = 人工双跑。
    #    故死命令一律**不复跑**,标 outcome_unknown(**业务结果未知**)· 需先核对业务结果,再由受控操作
    #    resolve_unknown_command(记 operator/理由/证据)决定标完成 or 受控重跑 —— **禁盲目换新 request_id 重触发**。
    reaped = _reap_stale_commands()

    # 1) 原子领取一条**仅 status='pending'**(从未启动过 · 重跑安全);死 claimed 不在此复活(见步骤 0)
    try:
        from db.connection import get_db
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE sched_commands
                   SET status='claimed', claim_token=%s, claimed_at=NOW(),
                       claim_heartbeat_at=NOW(), claimed_epoch=%s
                 WHERE id = (
                     SELECT id FROM sched_commands
                      WHERE status='pending'
                      ORDER BY created_at
                      FOR UPDATE SKIP LOCKED
                      LIMIT 1
                 )
                RETURNING command_id, job_name
                """,
                (token, epoch),
            )
            row = cur.fetchone()
            conn.commit()
    except Exception as e:
        logger.warning(f"[SchedControl] 领取命令失败: {e}")
        return {"error": str(e)[:200], "reaped": reaped}

    if not row:
        return {"processed": 0, "reaped": reaped}
    cid = row[0] if not isinstance(row, dict) else row.get("command_id")
    job_name = row[1] if not isinstance(row, dict) else row.get("job_name")

    # 2) [2d 独立 execution identity] 取 job 的 **raw** callable(unwrap sched_claim 时间桶)→ 手动执行
    #    不落 cron 的 (job_name, floor(now/period)) 桶,不会被自然 cron tick 的 claim 顶掉;业务重复由各
    #    job 自身幂等(FOR UPDATE + 状态 CAS)兜底。
    sch, job = _find_job(job_name)
    if job is None:
        _finish_command(cid, token, "failed", None, f"unknown job: {job_name}")
        return {"processed": 1, "status": "failed", "reason": "unknown_job"}
    raw = getattr(job.func, "__wrapped__", job.func)
    job_args = tuple(getattr(job, "args", ()) or ())

    # 3) [2e done 只在业务真正结束后写] 心跳线程续命 → 同步执行 raw → 完成后 CAS(只认 token)标终态
    stop = threading.Event()

    def _hb():
        while not stop.wait(_CMD_HEARTBEAT_INTERVAL):
            _heartbeat_command(cid, token)

    threading.Thread(target=_hb, daemon=True, name=f"sched-cmd-hb-{str(cid)[:8]}").start()

    status, err, result = "done", None, None
    try:
        rv = raw(*job_args)
        # [FF11 P1-3] async job:raw(*) 只是**返回 coroutine 未执行**。必须 await 到真正结束才能标 done,
        #   否则业务零执行、result 存 <coroutine object ...>、抛 "never awaited" = 假完成。consume 跑在
        #   BackgroundScheduler 工作线程(无运行中 event loop)· asyncio.run 建独立 loop 驱动到完成后关闭。
        if inspect.isawaitable(rv):
            rv = asyncio.run(rv)
        result = {"ran": True} if rv is None else {"ran": True, "return": str(rv)[:500]}
    except Exception as e:
        status, err = "failed", str(e)[:400]
        logger.error(f"[SchedControl] 手动命令 {cid} 执行失败 job={job_name}: {e}", exc_info=True)
    finally:
        stop.set()
        _finish_command(cid, token, status, result, err)
    return {"processed": 1, "status": status, "reaped": reaped}


def _reap_stale_commands() -> int:
    """[FF10/FF14 P0-1/P1-5] 回收死 claimed 命令(心跳>90s)→ 转 outcome_unknown · **绝不自动重跑**。
    返回回收条数。防旧执行体复活窗口:老 leader 死后其半执行命令不被第二 leader 复活双跑;且**不建议
    operator 盲目重触发**(旧体可能仍在跑)——需先核对业务结果再走 resolve_unknown_command 受控处置。"""
    try:
        from db.connection import get_db
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE sched_commands
                   SET status='outcome_unknown',
                       last_error = COALESCE(last_error, '')
                                    || 'stale_lease_abandoned:possible_partial_execution;outcome_unknown_verify_before_controlled_resolution'
                 WHERE status='claimed'
                   AND claim_heartbeat_at IS NOT NULL
                   AND claim_heartbeat_at < NOW() - INTERVAL '90 seconds'
                """,
            )
            n = cur.rowcount or 0
            conn.commit()
        if n:
            logger.error(f"[SchedControl] 回收 {n} 条死 claimed 命令 → outcome_unknown(业务结果未知)"
                         f"· 不自动重跑 · 需先核对业务结果再 resolve_unknown_command 受控处置(禁盲目重触发)")
        return n
    except Exception as e:
        logger.warning(f"[SchedControl] 回收死 claimed 命令失败(不影响本轮领取): {e}")
        return 0


def resolve_unknown_command(command_id: str, operator: str, decision: str,
                            note: str) -> Dict[str, Any]:
    """[FF14 P1-5] 受控处置 outcome_unknown 命令(operator 已**先核对业务结果**后调用)。

    decision:
      - 'confirmed_done'    业务结果已确认**发生**(旧执行体跑完了)→ 标 done · 不重跑。
      - 'controlled_rerun'  业务结果确认**未发生**(旧执行体没跑成/被证实无副作用)→ 受控入队一条**新命令**
                            (新 command_id · 幂等)重跑,原命令标 done 归档。
    强制记 operator + note(核对的业务结果/理由/证据)+ resolved_at 到 sched_commands(审计留痕)。
    仅对 status='outcome_unknown' 生效(CAS);其他状态拒绝(防误操作)。
    """
    if not operator or not note:
        return {"status": "error", "error": "operator 与 note(业务结果核对证据)必填"}
    if decision not in ("confirmed_done", "controlled_rerun"):
        return {"status": "error", "error": "decision 必须是 confirmed_done | controlled_rerun"}
    # [FF15 P0] **单事务原子**:CAS 原命令 done + (受控重跑时)入队新命令 + 记新旧关联,一起提交。
    #   任一步失败 → 全回滚 → 原命令保持 outcome_unknown(绝不"原命令已 done 但新命令不存在"的静默丢任务)。
    #   故不用 enqueue_command(它自带独立事务),受控重跑的 INSERT 内联进同一连接同一事务。
    import json as _json
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE sched_commands
               SET status='done', resolved_by=%s, resolution_note=%s, resolved_at=NOW(),
                   last_error = COALESCE(last_error,'') || '|resolved:' || %s
             WHERE command_id=%s AND status='outcome_unknown'
            RETURNING job_name, args
            """,
            (operator, note, decision, command_id),
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return {"status": "error", "error": "命令不存在或非 outcome_unknown 态(拒绝处置)"}
        job_name = row[0] if not isinstance(row, dict) else row.get("job_name")
        args = row[1] if not isinstance(row, dict) else row.get("args")
        rerun_id = None
        if decision == "controlled_rerun":
            new_args = args if isinstance(args, dict) else (_json.loads(args) if args else {})
            rerun_id = f"rerun-{command_id}-{uuid.uuid4().hex[:8]}"
            # 同一事务入队新命令(pending · consume 正常执行)。
            # [FF16 P1] **不用 ON CONFLICT DO NOTHING**:静默插 0 行会让"原命令标 done + 返回不存在的
            #   rerun_command_id"却没真建重跑命令 = 丢任务。改让 command_id 唯一冲突直接抛(触发整笔回滚);
            #   并**强制 rowcount==1**兜底(任何 0 行插入即抛 → rollback → 原命令保持 outcome_unknown)。
            cur.execute(
                """
                INSERT INTO sched_commands (command_id, job_name, args, status, requested_by, created_at, resolution_note)
                VALUES (%s, %s, %s, 'pending', %s, NOW(), %s)
                """,
                (rerun_id, job_name, _json.dumps(new_args), None, f"controlled_rerun_of:{command_id}"),
            )
            if cur.rowcount != 1:
                raise RuntimeError(
                    f"受控重跑入队 INSERT 影响 {cur.rowcount} 行(应 1)· 冲突/异常 · 整笔回滚保原命令 outcome_unknown")
            # 记新旧关联到原命令(审计可追溯)
            cur.execute(
                "UPDATE sched_commands SET last_error = COALESCE(last_error,'') || '|rerun:' || %s "
                "WHERE command_id=%s",
                (rerun_id, command_id),
            )
        conn.commit()   # ← 唯一提交点:UPDATE + INSERT + 关联一起落 · 原子
        result: Dict[str, Any] = {"status": "success", "command_id": command_id,
                                  "decision": decision, "resolved_by": operator}
        if rerun_id:
            result["rerun_command_id"] = rerun_id
        return result
    except Exception as e:
        try:
            conn.rollback()   # 任一步失败 → 全回滚 → 原命令保持 outcome_unknown(不丢任务)
        except Exception:
            pass
        logger.error(f"[SchedControl] resolve_unknown_command {command_id} 失败(已回滚 · 原命令保持 outcome_unknown): {e}")
        return {"status": "error", "error": str(e)[:200]}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _heartbeat_command(command_id: str, token: str) -> None:
    """执行中续心跳 · CAS 只认自己的 token(被 reclaim 后打不动)。"""
    try:
        from db.connection import get_db
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE sched_commands SET claim_heartbeat_at=NOW() "
                "WHERE command_id=%s AND claim_token=%s AND status='claimed'",
                (command_id, token),
            )
            conn.commit()
    except Exception:
        pass


def _finish_command(command_id: str, token: str, status: str,
                    result: Optional[dict], last_error: Optional[str]):
    """业务结束后标终态 · **CAS 只认自己的 token**:被 reclaim 后旧执行者不会误标别人的命令。"""
    try:
        from db.connection import get_db
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE sched_commands SET status=%s, result=%s, last_error=%s "
                "WHERE command_id=%s AND claim_token=%s AND status='claimed'",
                (status, json.dumps(result) if result is not None else None, last_error, command_id, token),
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"[SchedControl] 标记命令 {command_id}={status} 失败: {e}")


# ---------------- 状态上报(leader 写 Redis / web 读)----------------

def report_status_once() -> None:
    """cron leader job(每 30s):把两 scheduler 的 job 列表 + running + epoch 写 sched:status(TTL 90s)。"""
    from services.cron_gate import cron_should_fire
    if not cron_should_fire():
        return
    try:
        jobs: List[dict] = []
        running = False
        try:
            from api.scheduler import get_scheduler
            sch = get_scheduler()
            running = running or bool(sch.running)
            for j in sch.get_jobs():
                jobs.append({"id": j.id, "name": j.name,
                             "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
                             "source": "main"})
        except Exception:
            pass
        try:
            from scheduler import scheduler as root_sched
            if root_sched is not None:
                running = running or bool(root_sched.running)
                for j in root_sched.get_jobs():
                    jobs.append({"id": j.id, "name": j.name,
                                 "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
                                 "source": "root"})
        except Exception:
            pass
        epoch = None
        try:
            from cache.leader_lock import read_epoch
            epoch = read_epoch()
        except Exception:
            pass
        payload = {
            "running": running, "jobs": jobs, "job_count": len(jobs),
            "epoch": epoch, "host": socket.gethostname(),
            "reported_at": datetime.now(timezone.utc).isoformat(),
        }
        from cache.redis_client import get_redis
        r = get_redis()
        if r is not None:
            r.setex(_STATUS_KEY, _STATUS_TTL, json.dumps(payload, ensure_ascii=False))
    except Exception as e:
        logger.warning(f"[SchedControl] 状态上报失败: {e}")


def verify_is_current_leader(hostname: Optional[str] = None) -> bool:
    """[FF12 P0-2] 部署/回滚 cron 硬门用:本主机是否为**当前 Redis leader + fresh 心跳**。
    判据(不看历史日志 · 只看 Redis 当前态,避免复用旧容器命中过期"成为 leader"记录):
      - sched:leader 值(=hostname-pid-uuid)以本 hostname 开头(当前 owner 是本容器);
      - sched:status(setex TTL 90s · 存在即 <90s 新鲜)host==本 hostname 且 running=true。
    任一不符 / Redis 不可用 → False(fail-closed:未证实是 leader 就不算,让部署 abort + 自动恢复旧 cron)。"""
    me = hostname or socket.gethostname()
    try:
        from cache.redis_client import get_redis
        r = get_redis()
        if r is None:
            return False
        leader = r.get("sched:leader")
        st = r.get(_STATUS_KEY)
        if isinstance(leader, (bytes, bytearray)):
            leader = leader.decode("utf-8", "replace")
        if isinstance(st, (bytes, bytearray)):
            st = st.decode("utf-8", "replace")
        if not leader or not str(leader).startswith(me + "-"):
            return False
        if not st:
            return False
        d = json.loads(st)
        return d.get("host") == me and bool(d.get("running"))
    except Exception:
        return False


def read_status() -> Dict[str, Any]:
    """web 端点:读 Redis sched:status;心跳消失/Redis 不可用 → degraded。"""
    try:
        from cache.redis_client import get_redis
        r = get_redis()
        if r is None:
            return {"degraded": True, "reason": "redis_unavailable", "jobs": [], "running": False}
        raw = r.get(_STATUS_KEY)
        if not raw:
            return {"degraded": True, "reason": "cron_heartbeat_missing", "jobs": [], "running": False}
        data = json.loads(raw)
        data["degraded"] = False
        return data
    except Exception as e:
        return {"degraded": True, "reason": f"read_error:{str(e)[:80]}", "jobs": [], "running": False}
