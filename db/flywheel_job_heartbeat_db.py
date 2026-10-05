"""飞轮 job 统一心跳账本(A2 · 2026-07-29)。

为什么新建一张表而不是复用 `sched_job_runs`:
  `sched_job_runs` 是 **tick 级 claim 账本**(UNIQUE(job_name, scheduled_at) 抢"至多一次触发"),
  只有被 `sched_claim` 包裹的 job 才会留痕 —— 生产实证:飞轮五个 job(采集轮/池蒸馏/
  outcome backfill/有效性报告/媒体实体同步)在 `sched_job_runs` 里**一行都没有**,
  因为它们从来没进过 claim 路径。把它们塞进 claim 会顺带改掉 double-run 语义(fail-closed skip),
  风险远大于收益。本表是**纯观测账本**:一次执行一行,记 started/finished/failed + 处理量,
  不参与任何互斥决策,写失败也绝不影响 job 本身。

SQL 4 维核验(建表):
  1. 列名:job_key/run_key/status/started_at/finished_at/duration_ms/processed/detail/error/host 均在场
  2. data_type:processed INTEGER · duration_ms BIGINT · detail JSONB · 时间列全 TIMESTAMPTZ
     (与 sched_job_runs / ai_ops_alerts 同项目既有口径一致)
  3. 字段归属:全部新列在本新表,不碰 sched_job_runs / ai_ops_alerts / 任何飞轮业务表
  4. dry-run:CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS,全幂等无破坏性 SQL
"""
from __future__ import annotations

import json
import logging
import os
import socket
import uuid
from typing import Any, Iterable, Optional

from db.connection import get_connection, get_db

logger = logging.getLogger("GEO-FlywheelHeartbeat")

STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

_TABLE_READY = False


def _host() -> str:
    try:
        return f"{socket.gethostname()}-{os.getpid()}"[:120]
    except Exception:
        return "unknown"


def init_flywheel_heartbeat_tables(force: bool = False) -> None:
    """幂等建表。进程内缓存一次,避免每次 job 都跑一遍 DDL。"""
    global _TABLE_READY
    if _TABLE_READY and not force:
        return
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS flywheel_job_heartbeats (
                id BIGSERIAL PRIMARY KEY,
                job_key VARCHAR(80) NOT NULL,
                run_key TEXT NOT NULL,
                status VARCHAR(16) NOT NULL,
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                finished_at TIMESTAMPTZ,
                duration_ms BIGINT,
                processed INTEGER NOT NULL DEFAULT 0,
                detail JSONB NOT NULL DEFAULT '{}'::jsonb,
                error TEXT,
                host TEXT,
                CONSTRAINT uq_flywheel_heartbeat_run UNIQUE (job_key, run_key)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_flywheel_heartbeat_job_started "
            "ON flywheel_job_heartbeats(job_key, started_at DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_flywheel_heartbeat_job_success "
            "ON flywheel_job_heartbeats(job_key, finished_at DESC) "
            "WHERE status = 'succeeded'"
        )
    _TABLE_READY = True


def heartbeat_start(job_key: str, detail: Optional[dict[str, Any]] = None) -> Optional[str]:
    """开跑打点。返回 run_key(后续 finish/fail 要带回);写失败返 None(观测层绝不阻断业务)。"""
    run_key = uuid.uuid4().hex
    try:
        init_flywheel_heartbeat_tables()
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO flywheel_job_heartbeats (job_key, run_key, status, detail, host)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                """,
                (
                    job_key[:80], run_key, STATUS_RUNNING,
                    json.dumps(detail or {}, ensure_ascii=False, default=str), _host(),
                ),
            )
        return run_key
    except Exception as exc:
        logger.warning("[FlywheelHeartbeat] start 落库失败 job_key=%s: %s", job_key, exc)
        return None


def _close_run(
    job_key: str,
    run_key: Optional[str],
    *,
    status: str,
    processed: int = 0,
    detail: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
) -> bool:
    payload = json.dumps(detail or {}, ensure_ascii=False, default=str)
    try:
        init_flywheel_heartbeat_tables()
        with get_db() as conn:
            cur = conn.cursor()
            if run_key:
                cur.execute(
                    """
                    UPDATE flywheel_job_heartbeats
                       SET status = %s,
                           finished_at = NOW(),
                           duration_ms = GREATEST(0, (EXTRACT(EPOCH FROM (NOW() - started_at)) * 1000)::bigint),
                           processed = %s,
                           detail = %s::jsonb,
                           error = %s
                     WHERE job_key = %s AND run_key = %s
                    """,
                    (status, int(processed or 0), payload, (error or None)[:4000] if error else None,
                     job_key[:80], run_key),
                )
                if cur.rowcount:
                    return True
            # start 打点失败(run_key=None)或行已丢 → 补一行终态,不让失败无声消失。
            cur.execute(
                """
                INSERT INTO flywheel_job_heartbeats
                    (job_key, run_key, status, finished_at, duration_ms, processed, detail, error, host)
                VALUES (%s, %s, %s, NOW(), 0, %s, %s::jsonb, %s, %s)
                ON CONFLICT (job_key, run_key) DO NOTHING
                """,
                (
                    job_key[:80], run_key or uuid.uuid4().hex, status, int(processed or 0),
                    payload, (error or None)[:4000] if error else None, _host(),
                ),
            )
        return True
    except Exception as exc:
        logger.warning("[FlywheelHeartbeat] %s 落库失败 job_key=%s: %s", status, job_key, exc)
        return False


def heartbeat_success(
    job_key: str,
    run_key: Optional[str],
    *,
    processed: int = 0,
    detail: Optional[dict[str, Any]] = None,
) -> bool:
    return _close_run(job_key, run_key, status=STATUS_SUCCEEDED, processed=processed, detail=detail)


def heartbeat_failure(
    job_key: str,
    run_key: Optional[str],
    *,
    error: str,
    processed: int = 0,
    detail: Optional[dict[str, Any]] = None,
) -> bool:
    return _close_run(
        job_key, run_key, status=STATUS_FAILED, processed=processed, detail=detail, error=error
    )


def get_job_summary(job_key: str) -> dict[str, Any]:
    """单个 job 的观测摘要。表不存在 / DB 抖动 → available=False(调用方按"无法证明健康"处理)。"""
    return get_job_summaries([job_key]).get(job_key, {"job_key": job_key, "available": False})


def get_job_summaries(job_keys: Iterable[str]) -> dict[str, dict[str, Any]]:
    keys = [str(k)[:80] for k in job_keys if str(k or "").strip()]
    if not keys:
        return {}
    out: dict[str, dict[str, Any]] = {
        k: {
            "job_key": k,
            "available": False,
            "last_run_at": None,
            "last_status": None,
            "last_success_at": None,
            "last_error": None,
            "last_processed": None,
            "consecutive_failures": 0,
            "runs_7d": 0,
        }
        for k in keys
    }
    try:
        init_flywheel_heartbeat_tables()
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT DISTINCT ON (job_key)
                       job_key, status, started_at, finished_at, processed, error
                  FROM flywheel_job_heartbeats
                 WHERE job_key = ANY(%s)
                 ORDER BY job_key, started_at DESC, id DESC
                """,
                (keys,),
            )
            for row in cur.fetchall() or []:
                key = row["job_key"]
                out[key].update({
                    "available": True,
                    "last_run_at": row["started_at"],
                    "last_status": row["status"],
                    "last_error": row["error"],
                    "last_processed": row["processed"],
                })

            cur.execute(
                """
                SELECT DISTINCT ON (job_key)
                       job_key, finished_at, processed
                  FROM flywheel_job_heartbeats
                 WHERE job_key = ANY(%s) AND status = 'succeeded'
                 ORDER BY job_key, finished_at DESC, id DESC
                """,
                (keys,),
            )
            for row in cur.fetchall() or []:
                out[row["job_key"]].update({
                    "available": True,
                    "last_success_at": row["finished_at"],
                    "last_success_processed": row["processed"],
                })

            # 连续失败数 = 最后一次成功之后的 failed 行数(无成功记录则统计全部 failed)。
            cur.execute(
                """
                SELECT h.job_key, COUNT(*) AS failures
                  FROM flywheel_job_heartbeats h
                  LEFT JOIN LATERAL (
                        SELECT MAX(s.started_at) AS last_success_started_at
                          FROM flywheel_job_heartbeats s
                         WHERE s.job_key = h.job_key AND s.status = 'succeeded'
                  ) ls ON TRUE
                 WHERE h.job_key = ANY(%s)
                   AND h.status = 'failed'
                   AND (ls.last_success_started_at IS NULL OR h.started_at > ls.last_success_started_at)
                 GROUP BY h.job_key
                """,
                (keys,),
            )
            for row in cur.fetchall() or []:
                out[row["job_key"]]["consecutive_failures"] = int(row["failures"] or 0)

            cur.execute(
                """
                SELECT job_key, COUNT(*) AS runs
                  FROM flywheel_job_heartbeats
                 WHERE job_key = ANY(%s) AND started_at > NOW() - INTERVAL '7 days'
                 GROUP BY job_key
                """,
                (keys,),
            )
            for row in cur.fetchall() or []:
                out[row["job_key"]]["runs_7d"] = int(row["runs"] or 0)
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[FlywheelHeartbeat] 摘要查询失败: %s", exc)
    return out


def list_recent_runs(job_key: str = "", limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 50), 500))
    try:
        init_flywheel_heartbeat_tables()
        conn = get_connection()
        try:
            cur = conn.cursor()
            if job_key:
                cur.execute(
                    """
                    SELECT job_key, run_key, status, started_at, finished_at,
                           duration_ms, processed, detail, error, host
                      FROM flywheel_job_heartbeats
                     WHERE job_key = %s
                     ORDER BY started_at DESC, id DESC
                     LIMIT %s
                    """,
                    (job_key[:80], limit),
                )
            else:
                cur.execute(
                    """
                    SELECT job_key, run_key, status, started_at, finished_at,
                           duration_ms, processed, detail, error, host
                      FROM flywheel_job_heartbeats
                     ORDER BY started_at DESC, id DESC
                     LIMIT %s
                    """,
                    (limit,),
                )
            return [dict(r) for r in cur.fetchall() or []]
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[FlywheelHeartbeat] 明细查询失败: %s", exc)
        return []
