"""Flywheel bridge-run audit + freshness/observability (P0-1 + item7).

Two responsibilities, both shadow/audit-only (no customer data, no LLM):

1. `geo_flywheel_bridge_runs` — one row per round-completion bridge run, with the
   per-stage audit numbers (loaded / inserted / updated / skipped / failed /
   by_engine / by_tier / last_processed_raw_id) so a stuck pipeline is visible.

2. `get_flywheel_bridge_health()` — a freshness snapshot the admin UI uses to warn
   "调研数据已更新，但飞轮影子层未同步" when the shadow layer lags fresh research
   data by > 24h.  Prevents the pipeline from silently drifting again.

Freshness reads intentionally use one short-lived connection PER query so a
missing/empty table can never abort a shared transaction and cascade the rest to
NULL (the fail-soft-subquery-on-shared-cursor trap).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from psycopg2.extras import Json

from db.connection import get_connection, get_db

STALE_THRESHOLD_HOURS = 24.0

# 一次桥接跑超过这个小时数还停在 status='running' = 进程死在半路,不会再有人收尾。
# 生产实测(2026-08-01)有 5 条这样的孤儿行,最早 2026-07-06,从没被任何面板显示过。
STUCK_RUN_THRESHOLD_HOURS = 2.0

#: [飞轮收尾包④ §3H-3 · 2026-08-01] 孤儿桥跑的终态标记。
#:
#: 生产实测(2026-08-01)`status='running'` 共 6 行,其中 5 行是 2026-07-06~07-18 的
#: 孤儿(进程死在半路,没人收尾),第 6 行是当天 20:01 起的、可能还在真跑。
#: 🔴 所以**绝不能按 status 一刀切**,必须带时间阈值 —— 否则会把正在跑的那条标死。
#: 🔴 也**绝不删审计行**(已裁定):跑过就是跑过,标终态不等于抹掉历史。
#: `status` 列是 VARCHAR(20) 且**无 CHECK 约束**(已核 DDL),写入新取值是纯 additive:
#: 本仓 07-30 踩过"往 CHECK 加允许值被当成 additive → 两槽都起不来",这里刻意先核过。
STALE_ORPHAN_STATUS = "stale_orphan"


def _jsonb(value: Any) -> Json:
    return Json(value, dumps=lambda obj: json.dumps(obj, ensure_ascii=False, default=str))


def init_flywheel_bridge_tables() -> None:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS geo_flywheel_bridge_runs (
                id BIGSERIAL PRIMARY KEY,
                round_id VARCHAR(120) NOT NULL DEFAULT '',
                batch_id VARCHAR(120) NOT NULL DEFAULT '',
                trigger_source VARCHAR(40) NOT NULL DEFAULT 'manual',
                dry_run BOOLEAN NOT NULL DEFAULT FALSE,
                status VARCHAR(20) NOT NULL DEFAULT 'running',
                stages JSONB NOT NULL DEFAULT '{}'::jsonb,
                loaded INTEGER NOT NULL DEFAULT 0,
                inserted INTEGER NOT NULL DEFAULT 0,
                updated INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                last_processed_raw_id BIGINT,
                error TEXT,
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                finished_at TIMESTAMPTZ
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_flywheel_bridge_runs_round "
            "ON geo_flywheel_bridge_runs(round_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_flywheel_bridge_runs_started "
            "ON geo_flywheel_bridge_runs(started_at DESC)"
        )


def start_bridge_run(
    round_id: str,
    batch_id: str,
    *,
    trigger_source: str = "manual",
    dry_run: bool = False,
) -> int:
    init_flywheel_bridge_tables()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO geo_flywheel_bridge_runs
                (round_id, batch_id, trigger_source, dry_run, status, started_at)
            VALUES (%s, %s, %s, %s, 'running', NOW())
            RETURNING id
            """,
            (round_id or "", batch_id or "", trigger_source, bool(dry_run)),
        )
        return int(cur.fetchone()["id"])


def finish_bridge_run(
    run_id: int,
    *,
    status: str,
    stages: dict[str, Any],
    totals: dict[str, Any],
    last_processed_raw_id: int | None = None,
    error: str | None = None,
) -> None:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE geo_flywheel_bridge_runs
               SET status = %s,
                   stages = %s,
                   loaded = %s,
                   inserted = %s,
                   updated = %s,
                   skipped = %s,
                   failed = %s,
                   last_processed_raw_id = %s,
                   error = %s,
                   finished_at = NOW()
             WHERE id = %s
            """,
            (
                status,
                _jsonb(stages or {}),
                int((totals or {}).get("loaded", 0)),
                int((totals or {}).get("inserted", 0)),
                int((totals or {}).get("updated", 0)),
                int((totals or {}).get("skipped", 0)),
                int((totals or {}).get("failed", 0)),
                last_processed_raw_id,
                ((error or "")[:2000] or None),
                run_id,
            ),
        )


def _isolated_scalar(sql: str, params: tuple | None = None) -> Any:
    """Run one aggregate on its OWN connection so a missing table can't cascade.

    Returns the first column of the single result row, or None on any error /
    empty result.
    """
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(sql, params or ())
        row = cur.fetchone()
        if not row:
            return None
        return list(row.values())[0]
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _isolated_row(sql: str, params: tuple | None = None) -> dict[str, Any] | None:
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(sql, params or ())
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _lag_hours(newer: Any, older: Any) -> float | None:
    if not isinstance(newer, datetime) or not isinstance(older, datetime):
        return None
    def _utc(dt: datetime) -> datetime:
        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    delta = _utc(newer) - _utc(older)
    return round(delta.total_seconds() / 3600.0, 1)


def extract_failed_stages(stages: Any) -> list[dict[str, str]]:
    """把 stages jsonb 里 status='failed' 的 stage 提到顶层。

    [2026-08-01 断供事故] 桥的 stage 级失败原来只存在 `stages` jsonb 深处:
    run 级 `error` 是 NULL、`status` 只写 'partial',面板看上去"基本正常"。
    结果 source_signals 这一 stage **每轮 100% 失败**(SQL 保留字别名解析错误)
    连续 14 天没人发现。放松守卫那半边不能静默 —— 失败必须冒到顶层。
    """
    if isinstance(stages, str):
        try:
            stages = json.loads(stages)
        except (ValueError, TypeError):
            return []
    if not isinstance(stages, dict):
        return []
    failed: list[dict[str, str]] = []
    for name, payload in stages.items():
        if not isinstance(payload, dict):
            continue
        if str(payload.get("status") or "") == "failed":
            failed.append({
                "stage": str(name),
                "error": str(payload.get("error") or "")[:500],
            })
    return failed


def get_flywheel_bridge_health() -> dict[str, Any]:
    """Freshness + last-bridge-run snapshot for the anti-drift admin warning."""
    last_raw_at = _isolated_scalar("SELECT MAX(created_at) AS v FROM geo_research_raw")
    last_source_signal_at = _isolated_scalar(
        "SELECT MAX(observed_at) AS v FROM geo_research_source_signals"
    )
    last_answer_metric_at = _isolated_scalar(
        "SELECT MAX(observed_at) AS v FROM geo_answer_adoption_metrics"
    )
    last_media_entity_at = _isolated_scalar(
        "SELECT MAX(updated_at) AS v FROM geo_media_entities"
    )

    last_run = _isolated_row(
        """
        SELECT round_id, batch_id, status, dry_run, error, started_at, finished_at,
               loaded, inserted, updated, skipped, failed, last_processed_raw_id,
               stages
          FROM geo_flywheel_bridge_runs
         ORDER BY started_at DESC
         LIMIT 1
        """
    )
    failed_stages = extract_failed_stages((last_run or {}).get("stages"))
    stuck_runs = _isolated_scalar(
        """
        SELECT COUNT(*) AS v
          FROM geo_flywheel_bridge_runs
         WHERE status = 'running'
           AND started_at < CURRENT_TIMESTAMP - (%s * INTERVAL '1 hour')
        """,
        (STUCK_RUN_THRESHOLD_HOURS,),
    )
    # [包B §B3 · 2026-08-01] 被清扫收掉的孤儿跑计数。
    #   注意 `stuck_run_count` 只数 status='running' —— 清扫一旦生效它就归 0,
    #   若不单独带出 stale_orphan,面板上"清扫到底跑没跑过"完全不可见(死函数的另一种形态)。
    stale_orphan_runs = _isolated_scalar(
        """
        SELECT COUNT(*) AS v
          FROM geo_flywheel_bridge_runs
         WHERE status = %s
        """,
        (STALE_ORPHAN_STATUS,),
    )
    last_stale_orphan_at = _isolated_scalar(
        """
        SELECT MAX(finished_at) AS v
          FROM geo_flywheel_bridge_runs
         WHERE status = %s
        """,
        (STALE_ORPHAN_STATUS,),
    )
    last_success_round_id = _isolated_scalar(
        """
        SELECT round_id AS v
          FROM geo_flywheel_bridge_runs
         WHERE status = 'success' AND NOT dry_run
         ORDER BY finished_at DESC NULLS LAST
         LIMIT 1
        """
    )

    # lag of the FIRST downstream shadow layer (source_signals) behind raw.
    lag_hours = _lag_hours(last_raw_at, last_source_signal_at)
    stale = bool(lag_hours is not None and lag_hours > STALE_THRESHOLD_HOURS)

    return {
        "last_raw_at": last_raw_at,
        "last_source_signal_at": last_source_signal_at,
        "last_answer_metric_at": last_answer_metric_at,
        "last_media_entity_at": last_media_entity_at,
        "lag_hours": lag_hours,
        "stale": stale,
        "stale_threshold_hours": STALE_THRESHOLD_HOURS,
        "stale_message": (
            "调研数据已更新，但飞轮影子层未同步" if stale else ""
        ),
        # [2026-08-01 断供事故] stage 级失败提到顶层,不许再埋在 stages jsonb 里。
        "failed_stages": failed_stages,
        "has_failed_stage": bool(failed_stages),
        "failed_stage_message": (
            "上一次桥接有 {} 个环节失败:{} —— 轮次会显示 completed,但这些下游层没更新".format(
                len(failed_stages), "、".join(s["stage"] for s in failed_stages)
            ) if failed_stages else ""
        ),
        # 停在 running 再没收尾的孤儿跑(进程死在半路),原来任何面板都不显示。
        "stuck_run_count": int(stuck_runs or 0),
        "stuck_run_threshold_hours": STUCK_RUN_THRESHOLD_HOURS,
        "stuck_run_message": (
            f"有 {int(stuck_runs or 0)} 次桥接卡在运行中超过 "
            f"{STUCK_RUN_THRESHOLD_HOURS} 小时未收尾(进程中断)"
            if stuck_runs else ""
        ),
        # [包B §B3] 已被自愈清扫收成终态的孤儿跑(下一次桥跑入口自动收,不加 cron)。
        "stale_orphan_status": STALE_ORPHAN_STATUS,
        "stale_orphan_run_count": int(stale_orphan_runs or 0),
        "last_stale_orphan_at": last_stale_orphan_at,
        "stale_orphan_message": (
            f"历史有 {int(stale_orphan_runs or 0)} 次桥接因进程中断未收尾,"
            f"已由自愈清扫标记为终态(审计行保留,不删)"
            if stale_orphan_runs else ""
        ),
        "last_bridge_status": (last_run or {}).get("status"),
        "last_bridge_at": (last_run or {}).get("started_at"),
        "last_bridge_finished_at": (last_run or {}).get("finished_at"),
        "last_bridge_dry_run": bool((last_run or {}).get("dry_run")) if last_run else None,
        "last_success_round_id": last_success_round_id,
        "last_error": (last_run or {}).get("error"),
        "last_bridge_run": last_run,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def list_recent_bridge_runs(limit: int = 20) -> list[dict[str, Any]]:
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, round_id, batch_id, trigger_source, dry_run, status,
                   loaded, inserted, updated, skipped, failed,
                   last_processed_raw_id, error, started_at, finished_at
              FROM geo_flywheel_bridge_runs
             ORDER BY started_at DESC
             LIMIT %s
            """,
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def sweep_stale_orphan_runs(
    *, threshold_hours: float | None = None, dry_run: bool = True
) -> dict[str, Any]:
    """[包④ §3H-3] 把超时仍 `running` 的桥跑收成 `stale_orphan` 终态。

    返回 {threshold_hours, dry_run, matched, swept, run_ids}。

    🔴 三条硬性质:
      1. **带时间阈值**,不按 status 一刀切 —— 正在跑的那条不能被标死;
      2. **只改 status + finished_at,不删行**(已裁定:审计行保留);
      3. **幂等**:只匹配 `status='running'`,已收过的行不会被再收一次。
    """
    hours = float(threshold_hours if threshold_hours is not None else STUCK_RUN_THRESHOLD_HOURS)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, round_id, started_at
              FROM geo_flywheel_bridge_runs
             WHERE status = 'running'
               AND started_at < CURRENT_TIMESTAMP - (%s * INTERVAL '1 hour')
             ORDER BY started_at
            """,
            (hours,),
        )
        matched = [dict(r) for r in cur.fetchall()]
        if dry_run:
            conn.rollback()
            return {
                "threshold_hours": hours,
                "dry_run": True,
                "matched": len(matched),
                "swept": 0,
                "run_ids": [int(r["id"]) for r in matched],
            }

        cur.execute(
            """
            UPDATE geo_flywheel_bridge_runs
               SET status = %s,
                   finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP),
                   error = COALESCE(
                       error,
                       '进程未收尾,由 stale_orphan 清扫标记为终态(审计行保留)'
                   )
             WHERE status = 'running'
               AND started_at < CURRENT_TIMESTAMP - (%s * INTERVAL '1 hour')
            RETURNING id
            """,
            (STALE_ORPHAN_STATUS, hours),
        )
        swept = [int(r["id"]) for r in cur.fetchall()]
        conn.commit()
        return {
            "threshold_hours": hours,
            "dry_run": False,
            "matched": len(matched),
            "swept": len(swept),
            "run_ids": swept,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
