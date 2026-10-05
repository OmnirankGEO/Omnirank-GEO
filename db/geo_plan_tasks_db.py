"""
Phase 4 · geo_plan_tasks 表 CRUD + 心跳 + 状态转移

作者: CTO-15.5 · 2026-04-20
PRD: .planning/phases/04-c-geo/PRD.md Section 4.1/4.3
CONTEXT: .planning/phases/04-c-geo/04-CONTEXT.md

- 所有时间戳用 NOW() SQL 生成,避免 Python datetime 时区漂移
- RealDictCursor (db.connection 已配)
- 所有函数显式 try/finally 归还连接 (遵循 CLAUDE.md v3.4 连接池规范)
- result_json 存 JSONB,list_tasks_by_user 返精简 result_summary 不返全量

导出函数 (下游 PLAN 02 / PLAN 03 / PLAN 05 消费):
  create_task / get_task_by_id / list_tasks_by_user / find_running_for_brand
  count_running_for_user / update_progress / heartbeat / mark_status
  mark_zombie / sweep_server_restart / update_freeze_id / update_linked_quote
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger("GEO-GeoPlanTasks")


# ============================================================
# 状态 / 阶段常量 (供上游 API 层 + Worker 用)
# ============================================================

# [v5 req1] 新增耐久结算态 settling / settlement_pending / refund_pending:
#   running→settling→done(commit_freeze success=true 确认后才 done);commit/release 失败 → *_pending 补偿队列。
# [v6 req1] settle_conflict:commit/release 命中【相反终态】(commit 时 freeze 已 released / release 时已 committed)
#   → 禁落 done/failed,进人工处置桶(reconcile 不自动重试 · 由 fund_recovery 人工处置 API 收口)。
TASK_STATUSES = ("queued", "running", "settling", "settlement_pending", "refund_pending", "settle_conflict",
                 "done", "failed", "cancelled", "timeout")
TASK_STAGES = ("identify", "expand", "audit", "cluster", "pricing", "done")
RUNNING_STATUSES = ("queued", "running")
# [v5 req1 + v7 finding4] 去重/并发上限口径:非终态一律算"在途"(含 settling / *_pending / settle_conflict),
#   防同品牌重复起 + 并发绕过。settle_conflict(人工待处置)也是【非终态占资金】· 必须算在途,否则用户可再起同品牌任务再冻结资金。
ACTIVE_STATUSES = ("queued", "running", "settling", "settlement_pending", "refund_pending", "settle_conflict")
# [v5 req1] 补偿队列:耐久 pending 态(scheduler reconcile 扫这两个)。
PENDING_SETTLE_STATUSES = ("settlement_pending", "refund_pending")

# result_summary 精简返回的字段长度上限 (避免 list API 返 10K)
_RESULT_SUMMARY_MAX_BYTES = 1500


# ============================================================
# 工具
# ============================================================


def _get_conn():
    """单点 import connection 模块,和 db/notifications.py / db/wallet_db.py 一致."""
    from db.connection import get_connection
    return get_connection()


def _json_dumps(obj: Any) -> str:
    """统一 JSON 序列化参数,避免中文乱码."""
    return json.dumps(obj, ensure_ascii=False, default=str)


def _row_to_dict(row) -> Optional[dict]:
    """RealDictCursor 已返 dict,但某些 cursor factory 可能返 tuple,保底转一手."""
    if row is None:
        return None
    if hasattr(row, "keys"):
        return dict(row)
    return row  # 兜底


def _summarize_result(result_json: Any) -> Optional[dict]:
    """把大结果精简成 summary (给 list API 用,不返全量 result_json).

    保留的字段:keyword_count / cluster_count / entry_price / tier_summaries 首条,
    其他字段截掉. 兜底: 如果 JSON dump 超 1.5K 就直接返 keys 列表 + 总长度.
    """
    if result_json is None:
        return None
    if isinstance(result_json, str):
        try:
            result_json = json.loads(result_json)
        except Exception:
            return {"_type": "string", "_preview": result_json[:200]}
    if not isinstance(result_json, dict):
        return {"_type": type(result_json).__name__}

    summary = {}
    # 常见字段白名单
    for k in ("keyword_count", "cluster_count", "entry_price", "eta_sec",
              "degraded", "has_l1", "has_l2"):
        if k in result_json:
            summary[k] = result_json[k]
    # tier_summaries 只留首条
    if "tier_summaries" in result_json and isinstance(result_json["tier_summaries"], list):
        summary["tier_summaries_first"] = result_json["tier_summaries"][0] if result_json["tier_summaries"] else None
        summary["tier_summaries_count"] = len(result_json["tier_summaries"])
    # clusters 只留 count
    if "clusters" in result_json and isinstance(result_json["clusters"], list):
        summary["clusters_count"] = len(result_json["clusters"])

    # 兜底: 如果 summary 还是超大,只返 keys
    s_str = _json_dumps(summary)
    if len(s_str) > _RESULT_SUMMARY_MAX_BYTES:
        return {"_keys": list(result_json.keys()),
                "_total_bytes": len(_json_dumps(result_json))}
    return summary


# ============================================================
# CRUD
# ============================================================


def create_task(
    user_id: int,
    brand_id: int,
    params_json: dict,
    brand_snapshot: dict,
    data_mode: str,
    source: str,
    ip: Optional[str] = None,
    freeze_id: Optional[int] = None,
) -> int:
    """创建任务 (status='queued').

    调用者责任:
      - 外部已做 RBAC (brand.owner_user_id == user_id)
      - 外部已做 D1 (find_running_for_brand 返 None)
      - 外部已做 D2 (count_running_for_user < 5)
    本函数不做前置校验,避免职责漂移 (API 层一致性校验,DB 层只落地).

    Returns: task_id (BIGSERIAL)
    """
    if data_mode not in ("full", "l1l2_fallback"):
        raise ValueError(f"invalid data_mode: {data_mode}")
    if source is None or not source.strip():
        raise ValueError("source 不能为空")

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO geo_plan_tasks
                (user_id, brand_id, status, params_json, brand_snapshot,
                 progress_percent, data_mode, source, ip_at_start, freeze_id,
                 queued_at)
            VALUES
                (%s, %s, 'queued', %s::jsonb, %s::jsonb,
                 0, %s, %s, %s, %s,
                 NOW())
            RETURNING id
            """,
            (user_id, brand_id,
             _json_dumps(params_json or {}),
             _json_dumps(brand_snapshot or {}),
             data_mode, source, ip, freeze_id),
        )
        task_id = cur.fetchone()["id"]
        conn.commit()
        logger.info(
            f"[GeoPlanTasks] create_task id={task_id} user={user_id} brand={brand_id} "
            f"mode={data_mode} source={source}"
        )
        return int(task_id)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_task_by_id(task_id: int, user_id: Optional[int] = None) -> Optional[dict]:
    """查单任务.

    Args:
        task_id: 任务 id
        user_id: 传了则加 RBAC WHERE user_id=X,不是 owner 返 None (API 层翻 403)

    Returns:
        dict or None
    """
    conn = _get_conn()
    try:
        cur = conn.cursor()
        if user_id is not None:
            cur.execute(
                "SELECT * FROM geo_plan_tasks WHERE id = %s AND user_id = %s",
                (task_id, user_id),
            )
        else:
            cur.execute(
                "SELECT * FROM geo_plan_tasks WHERE id = %s",
                (task_id,),
            )
        row = cur.fetchone()
        return _row_to_dict(row)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_tasks_by_user(
    user_id: int,
    brand_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = 20,
    include_archived: bool = False,
) -> list[dict]:
    """列任务 (不返 result_json 全量,只返 result_summary).

    Args:
        user_id: 必传,RBAC 边界
        brand_id: 可选,过滤 brand
        status: 可选,'running'/'done'/'failed'/'cancelled'/'timeout'/'queued'
                或特殊值 'all' = 全部 · 默认 None 也算全部 (仅过滤 archived)
        limit: 默认 20,max 200
        include_archived: 是否包含已归档任务 (default False)

    Returns:
        [dict]
    """
    if limit is None or limit <= 0:
        limit = 20
    limit = min(limit, 200)

    conn = _get_conn()
    try:
        cur = conn.cursor()
        parts = ["SELECT id, user_id, brand_id, status, progress_stage, progress_percent, "
                 "progress_message, data_mode, source, queued_at, started_at, heartbeat_at, "
                 "done_at, archived_at, linked_quote_id, freeze_id, error_code, error_detail, "
                 "result_json, brand_snapshot "
                 "FROM geo_plan_tasks WHERE user_id = %s"]
        params: list = [user_id]
        if not include_archived:
            parts.append("AND archived_at IS NULL")
        if brand_id is not None:
            parts.append("AND brand_id = %s")
            params.append(brand_id)
        if status and status != "all":
            parts.append("AND status = %s")
            params.append(status)
        parts.append("ORDER BY id DESC LIMIT %s")
        params.append(limit)

        cur.execute(" ".join(parts), params)
        rows = cur.fetchall()

        out: list[dict] = []
        for r in rows:
            d = dict(r) if hasattr(r, "keys") else r
            # 精简 result_json 成 result_summary,节省 list API 流量
            raw = d.pop("result_json", None)
            d["result_summary"] = _summarize_result(raw)
            # 精简 brand_snapshot (只留 name + industry)
            snap = d.pop("brand_snapshot", None) or {}
            if isinstance(snap, str):
                try:
                    snap = json.loads(snap)
                except Exception:
                    snap = {}
            d["brand_name"] = snap.get("name") if isinstance(snap, dict) else None
            d["brand_industry"] = snap.get("industry") if isinstance(snap, dict) else None
            out.append(d)
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass


def find_running_for_brand(brand_id: int) -> Optional[dict]:
    """查某品牌是否有 running/queued 任务 (D1 同品牌去重).

    命中返任务 dict,没有返 None.
    若有多个 (极端并发),返最新那条.
    """
    conn = _get_conn()
    try:
        cur = conn.cursor()
        # [v7 finding4] 用 ACTIVE_STATUSES 单一口径(含 settle_conflict)· 避免与并发/索引口径漂移
        cur.execute(
            """
            SELECT id, user_id, brand_id, status, progress_stage, progress_percent,
                   progress_message, queued_at, started_at, heartbeat_at, freeze_id
            FROM geo_plan_tasks
            WHERE brand_id = %s AND status = ANY(%s)
            ORDER BY id DESC
            LIMIT 1
            """,
            (brand_id, list(ACTIVE_STATUSES)),
        )
        row = cur.fetchone()
        return _row_to_dict(row)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def count_running_for_user(user_id: int) -> int:
    """统计用户当前 running/queued 任务数 (D2 并发上限)."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        # [v7 finding4] 用 ACTIVE_STATUSES 单一口径(含 settle_conflict)
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM geo_plan_tasks WHERE user_id = %s AND status = ANY(%s)",
            (user_id, list(ACTIVE_STATUSES)),
        )
        row = cur.fetchone()
        return int(row["cnt"]) if row else 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# 状态转移
# ============================================================


def update_progress(
    task_id: int,
    stage: str,
    percent: int,
    message: Optional[str] = None,
) -> None:
    """更新进度 + 同时刷 heartbeat_at.

    stage 不做枚举校验 (容忍 worker 传自定义阶段描述,TASK_STAGES 只是参考).
    percent 钳位到 [0, 100].
    """
    try:
        p = int(percent)
    except Exception:
        p = 0
    p = max(0, min(100, p))

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE geo_plan_tasks
               SET progress_stage = %s,
                   progress_percent = %s,
                   progress_message = %s,
                   heartbeat_at = NOW()
             WHERE id = %s
            """,
            (stage, p, message, task_id),
        )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def heartbeat(task_id: int) -> None:
    """仅更新 heartbeat_at (worker 循环中间无明显进展时调)."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE geo_plan_tasks SET heartbeat_at = NOW() WHERE id = %s",
            (task_id,),
        )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


_TERMINAL_STATUSES = ("done", "failed", "cancelled", "timeout")

# [v5 req1 · 耐久结算态 2026-07-13] 严格转移表(CAS):目标态 → 允许的源态白名单。
#   happy:  queued→running→settling→done(commit_freeze success=true 确认后才 done)
#   结算失败:settling→settlement_pending(commit 失败/异常 · 补偿队列 · 重试 commit → done)
#   退款队列:running→refund_pending(失败/取消/超时且需退 freeze · 或 zombie/restart)· 退成功 → 终态
#   done:   settling→done(inline commit ok)/ settlement_pending→done(补偿 commit ok)
#   failed/timeout/cancelled: running→(无 freeze 直接终态)/ refund_pending→(退成功后落终态)
#   cancelled 另允许 queued→(未起即取消 · dispatch 失败)
# mark_status 用 `WHERE id=%s AND status IN (<允许源态>)` 实现;命中 0 行 → 返 False(调用方据此跳过副作用)。
_ALLOWED_SOURCE_STATES = {
    "running":            ("queued",),
    "settling":           ("running",),
    "settlement_pending": ("settling",),
    "refund_pending":     ("queued", "running"),  # queued:未起即取消但已冻结 → 也走退款队列
    # [v6 req1] settle_conflict:commit/release 命中相反终态 → 人工桶(禁 done/failed)
    "settle_conflict":    ("settling", "settlement_pending", "refund_pending"),
    # done/failed/cancelled 另允许从 settle_conflict 转入 = admin 人工处置后收口(fund_recovery 处置 API)
    "done":               ("settling", "settlement_pending", "settle_conflict"),
    "failed":             ("running", "refund_pending", "settle_conflict"),
    "timeout":            ("running", "refund_pending", "settle_conflict"),
    "cancelled":          ("queued", "running", "refund_pending", "settle_conflict"),
}


def mark_status(
    task_id: int,
    status: str,
    result_json: Optional[dict] = None,
    error_code: Optional[str] = None,
    error_detail: Optional[str] = None,
    pending_terminal: Optional[str] = None,
) -> bool:
    """标任务状态.

    状态转移规则:
      - queued → running: 同时更新 started_at = NOW(),heartbeat_at = NOW()
      - * → done/failed/cancelled/timeout: 同时更新 done_at = NOW()
      - done 时 result_json 写入 (JSONB)
      - failed/timeout 时 error_code / error_detail 写入

    [GEO-R8-CAN-001] 终态转移改为 compare-and-set(first-terminal-wins):
      一旦任务已处于任一终态,禁止被后续终态覆盖。修复 cancel 与 worker
      完成之间的竞态 —— 用户 cancel 后 status='cancelled' 且 freeze 已释放,
      若 worker 完成路径随后再 mark_status(done),旧实现会无条件把
      'cancelled' 覆盖成 'done',产生矛盾状态(且触发对已释放 freeze 的
      commit)。加 CAS 守卫后此 mark 变为 no-op,状态保持 cancelled。
      合法的取消(queued/running → cancelled)不受影响,因为源态非终态。

    Returns:
        bool — 本次 UPDATE 是否真正命中并改动了一行。终态被 CAS 拦下时返 False
               (调用方可据此跳过后续 commit_freeze 等副作用)。
    """
    if status not in TASK_STATUSES:
        raise ValueError(f"invalid status: {status}")

    is_terminal = status in _TERMINAL_STATUSES

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status FROM geo_plan_tasks WHERE id = %s FOR UPDATE", (task_id,))
        _before = cur.fetchone()
        previous_status = (
            (_before.get("status") if isinstance(_before, dict) else _before[0])
            if _before else None
        )
        sets = ["status = %s"]
        params: list = [status]

        if status == "running":
            sets.extend(["started_at = COALESCE(started_at, NOW())", "heartbeat_at = NOW()"])
        if is_terminal:
            sets.append("done_at = COALESCE(done_at, NOW())")
        if result_json is not None:
            sets.append("result_json = %s::jsonb")
            params.append(_json_dumps(result_json))
        if error_code is not None:
            sets.append("error_code = %s")
            params.append(error_code)
        if error_detail is not None:
            sets.append("error_detail = %s")
            params.append(error_detail)
        # [v5 req1] refund_pending 记住"退款成功后应落的终态"(failed/timeout/cancelled)· 补偿据此收口
        if pending_terminal is not None:
            sets.append("pending_terminal = %s")
            params.append(pending_terminal)

        params.append(task_id)
        # [v4 req1 · Deploy-CTO 状态机完整化 2026-07-13] 严格转移表 CAS:目标态只允许从白名单源态转入。
        # 取代 v3 的"源态非终态"粗守卫 —— 那还允许 running→running(幂等复标)、queued→done(跳过 running)
        # 等不合法转移。现按 _ALLOWED_SOURCE_STATES 精确约束;命中 0 行 → changed=False,调用方跳过副作用。
        _allowed_src = _ALLOWED_SOURCE_STATES.get(status)
        if _allowed_src is not None:
            _ph = ", ".join(["%s"] * len(_allowed_src))
            where = f"WHERE id = %s AND status IN ({_ph})"
            params.extend(_allowed_src)
        else:
            # 未列出的目标态(如 queued · 正常流程不经 mark_status 置 queued)→ 保守:仅允许从非终态转入
            where = "WHERE id = %s AND status NOT IN ('done', 'failed', 'cancelled', 'timeout')"
        cur.execute(
            f"UPDATE geo_plan_tasks SET {', '.join(sets)} {where}",
            params,
        )
        changed = cur.rowcount > 0
        if changed and (is_terminal or status == "settle_conflict"):
            cur.execute(
                "SELECT user_id, COALESCE(done_at, NOW()) AS terminal_at "
                "FROM geo_plan_tasks WHERE id = %s",
                (task_id,),
            )
            terminal_row = cur.fetchone()
            if terminal_row and terminal_row.get("user_id") is not None:
                from services.notification_events import NotificationEventType, RecipientKind
                from services.notification_outbox import (
                    enqueue_admin_notification_events,
                    enqueue_notification_event,
                )

                if status == "done":
                    event_type = NotificationEventType.GEO_PLAN_COMPLETED
                    terminal_state = "completed"
                    status_text = "方案已生成"
                elif status == "settle_conflict":
                    event_type = NotificationEventType.GEO_PLAN_MANUAL_REQUIRED
                    terminal_state = "manual_required"
                    status_text = "需要平台人工核验"
                elif previous_status == "refund_pending":
                    event_type = NotificationEventType.GEO_PLAN_REFUNDED
                    terminal_state = "refunded"
                    status_text = "任务未完成，费用已退回"
                elif status == "cancelled":
                    event_type = NotificationEventType.GEO_PLAN_CANCELLED
                    terminal_state = "cancelled"
                    status_text = "任务已取消"
                else:
                    event_type = NotificationEventType.GEO_PLAN_FAILED
                    terminal_state = "failed"
                    status_text = "方案生成未完成"

                terminal_at = terminal_row.get("terminal_at")
                facts = {
                    "business_no": f"PLAN-{task_id}",
                    "status": status_text,
                    "occurred_at": terminal_at.isoformat(timespec="seconds"),
                    "summary": "请在报价方案页面查看结果和下一步。",
                }
                enqueue_notification_event(
                    cur,
                    event_type=event_type,
                    business_id=str(task_id),
                    terminal_state=terminal_state,
                    recipient_user_id=int(terminal_row["user_id"]),
                    recipient_kind=RecipientKind.USER,
                    facts=facts,
                )
                if status == "settle_conflict":
                    enqueue_admin_notification_events(
                        cur,
                        event_type=event_type,
                        business_id=str(task_id),
                        terminal_state=terminal_state,
                        facts=facts,
                    )
        conn.commit()
        if not changed:
            logger.warning(
                f"[GeoPlanTasks] mark_status id={task_id} -> {status} SKIPPED "
                f"(源态已是终态,CAS 拦截,禁止覆盖/复活既有终态) [GEO-R8-CAN-001]"
            )
        else:
            logger.info(
                f"[GeoPlanTasks] mark_status id={task_id} -> {status} "
                f"error={error_code or '-'} terminal={is_terminal} changed={changed}"
            )
        return changed
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# Zombie 扫表 / 启动恢复
# ============================================================


def mark_zombie(threshold_seconds: int = 120) -> int:
    """扫 heartbeat 超时任务 →【进补偿队列】(不直接 failed · 防丢 freeze)。

    [v5 req1] 两类都进【同一补偿队列】,由 scheduler reconcile 释放/提交 freeze 后落终态:
      - running(stale) → refund_pending(pending_terminal='failed')· freeze 需 release;
      - settling(stale) → settlement_pending(pending_terminal='done')· 结果已在 settling 落库 · freeze 需 commit。
    默认 2 分钟 (PRD GEO-REQ-SCHED-1). Returns: 被处理的行数
    """
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE geo_plan_tasks
               SET status = 'refund_pending', pending_terminal = 'failed',
                   error_code = 'zombie',
                   error_detail = 'heartbeat 超时 · 进退款补偿队列(freeze 待 release)'
             WHERE status = 'running'
               AND heartbeat_at IS NOT NULL
               AND heartbeat_at < NOW() - (%s || ' seconds')::interval
            """,
            (str(threshold_seconds),),
        )
        n1 = cur.rowcount
        cur.execute(
            """
            UPDATE geo_plan_tasks
               SET status = 'settlement_pending', pending_terminal = 'done',
                   error_code = 'zombie_settling',
                   error_detail = '结算中 heartbeat 超时 · 进结算补偿队列(freeze 待 commit)'
             WHERE status = 'settling'
               AND heartbeat_at IS NOT NULL
               AND heartbeat_at < NOW() - (%s || ' seconds')::interval
            """,
            (str(threshold_seconds),),
        )
        n2 = cur.rowcount
        # [v8 对抗审 P3 修] queued 无心跳,mark_zombie 旧版(heartbeat_at IS NOT NULL)永远漏它 → 仅靠 startup sweep 收口
        #   (worker 在 pickup 前挂/DB 瞬时故障留 queued+冻结,要等下次重启才清)。周期 reaper 也收【陈旧】queued(年龄闸
        #   防误杀对端新任务);口径与 sweep 一致:funded→refund_pending / 无资金 full→cancelled。阈值用 max(threshold, 180s)
        #   确保远超正常 pickup 秒级窗口。
        _q_age = max(int(threshold_seconds or 0), 180)
        cur.execute(
            """
            UPDATE geo_plan_tasks
               SET status = 'refund_pending', pending_terminal = 'cancelled',
                   error_code = 'zombie_queued',
                   error_detail = '排队超时无 worker 接管 · 进退款补偿队列(freeze 待 release)'
             WHERE status = 'queued' AND queued_at < NOW() - (%s || ' seconds')::interval
                   AND (freeze_id IS NOT NULL OR data_mode = 'l1l2_fallback')
            """,
            (str(_q_age),),
        )
        n3 = cur.rowcount
        cur.execute(
            """
            UPDATE geo_plan_tasks
               SET status = 'cancelled',
                   error_code = 'zombie_queued_unfunded',
                   error_detail = '排队超时无 worker 接管(无冻结·免费)· 安全取消'
             WHERE status = 'queued' AND queued_at < NOW() - (%s || ' seconds')::interval
                   AND freeze_id IS NULL AND data_mode = 'full'
            """,
            (str(_q_age),),
        )
        n4 = cur.rowcount
        n = n1 + n2 + n3 + n4
        conn.commit()
        if n:
            logger.warning(f"[GeoPlanTasks] mark_zombie → 补偿队列 {n1} running→refund_pending + {n2} settling→settlement_pending "
                           f"+ {n3} queued→refund_pending + {n4} queued→cancelled (threshold={threshold_seconds}s)")
        return int(n)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def sweep_server_restart(grace_seconds: int = 180) -> int:
    """FastAPI startup hook 调:把【陈旧】孤儿任务进补偿队列 / 安全终结(不直接 failed · 防丢 freeze)。

    [v5 req1] running → refund_pending(pending_terminal='failed')· settling → settlement_pending(pending_terminal='done')。
    [v8 P1-3] worker 是进程内 asyncio.create_task · 重启后 queued 任务永远无 worker 接管 → 也必须收口。
    [v8 对抗审 P2 修 · 蓝绿安全] 🔴 蓝绿共享一个 DB(blue+green+db)· 无 grace 的一刀切 sweep 会在 green 启动时
      【误杀 blue 正在跑的 queued/running 活任务】(冻结被退、任务被 cancel)。故加【年龄闸】:只清【确实陈旧】的孤儿——
        running/settling:COALESCE(heartbeat_at, started_at, queued_at) 早于 NOW()-grace(活任务心跳新鲜 → 不动);
        queued:queued_at 早于 NOW()-grace(对端刚建的新任务 queued_at 新鲜 → 不动 · queued 无心跳只能看年龄)。
      grace 默认 180s(> 心跳 30s · 且 worker 正常秒级 pickup queued)· 测试传 grace_seconds=0 立即清。
    Returns: 被处理的行数
    """
    _age = f"(NOW() - INTERVAL '{int(max(0, grace_seconds))} seconds')"
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            UPDATE geo_plan_tasks
               SET status = 'refund_pending', pending_terminal = 'failed',
                   error_code = 'server_restart',
                   error_detail = '服务重启时任务在运行中(心跳陈旧)· 进退款补偿队列(freeze 待 release)'
             WHERE status = 'running' AND COALESCE(heartbeat_at, started_at, queued_at) < {_age}
            """
        )
        n1 = cur.rowcount
        cur.execute(
            f"""
            UPDATE geo_plan_tasks
               SET status = 'settlement_pending', pending_terminal = 'done',
                   error_code = 'restart_settling',
                   error_detail = '服务重启时任务在结算中(心跳陈旧)· 进结算补偿队列(freeze 待 commit)'
             WHERE status = 'settling' AND COALESCE(heartbeat_at, started_at, queued_at) < {_age}
            """
        )
        n2 = cur.rowcount
        # [v8 P1-3] funded / 可能 funded 的【陈旧】queued → refund_pending(freeze 待 release · 覆盖 freeze_id 回填前崩溃 orphan)
        cur.execute(
            f"""
            UPDATE geo_plan_tasks
               SET status = 'refund_pending', pending_terminal = 'cancelled',
                   error_code = 'server_restart_queued',
                   error_detail = '服务重启时任务陈旧滞留队列(worker 进程内已丢)· 进退款补偿队列(freeze 待 release)'
             WHERE status = 'queued' AND queued_at < {_age}
                   AND (freeze_id IS NOT NULL OR data_mode = 'l1l2_fallback')
            """
        )
        n3 = cur.rowcount
        # [v8 P1-3] 确定无资金的【陈旧】queued(free full · 无 freeze)→ 安全 cancelled
        cur.execute(
            f"""
            UPDATE geo_plan_tasks
               SET status = 'cancelled',
                   error_code = 'server_restart_unfunded',
                   error_detail = '服务重启时任务陈旧滞留队列(无冻结·免费)· 安全取消'
             WHERE status = 'queued' AND queued_at < {_age}
                   AND freeze_id IS NULL AND data_mode = 'full'
            """
        )
        n4 = cur.rowcount
        n = n1 + n2 + n3 + n4
        conn.commit()
        if n:
            logger.warning(f"[GeoPlanTasks] sweep_server_restart(grace={grace_seconds}s) → 补偿队列 {n1} running→refund_pending + "
                           f"{n2} settling→settlement_pending + {n3} queued→refund_pending + {n4} queued→cancelled")
        return int(n)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_task_statuses(task_ids: list) -> dict:
    """[v8 二轮对抗审 P3] 批量取任务状态 {id: status}· 供巡检反向收口一次查代替 N 次 get_task_by_id。"""
    ids = [int(t) for t in (task_ids or []) if t is not None]
    if not ids:
        return {}
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, status FROM geo_plan_tasks WHERE id = ANY(%s)", (ids,))
        out = {}
        for r in cur.fetchall():
            d = _row_to_dict(r)
            out[d["id"]] = d["status"]
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_settle_conflict_tasks(limit: int = 200, before_id: int = None) -> list[dict]:
    """[v8 P2 · v9 P2-4 加 id 游标分页] 列 settle_conflict 任务(非归档 · id DESC)· 供巡检确保每条都有人工工单入口。

    before_id: 仅取 id < before_id(游标翻页排空)· 修 v8 固定 `LIMIT 200` 致第 201+ 条冲突任务
      永远排不进窗口 → 缺工单永久饥饿。分页由巡检侧循环排空(见 ensure_settle_conflict_workorders 正向段)。
    """
    conn = _get_conn()
    try:
        cur = conn.cursor()
        _lim = min(int(limit or 200), 500)
        if before_id is not None:
            cur.execute(
                "SELECT id, user_id, brand_id, freeze_id, freeze_table, error_code FROM geo_plan_tasks "
                "WHERE status='settle_conflict' AND archived_at IS NULL AND id < %s ORDER BY id DESC LIMIT %s",
                (int(before_id), _lim),
            )
        else:
            cur.execute(
                "SELECT id, user_id, brand_id, freeze_id, freeze_table, error_code FROM geo_plan_tasks "
                "WHERE status='settle_conflict' AND archived_at IS NULL ORDER BY id DESC LIMIT %s",
                (_lim,),
            )
        return [_row_to_dict(r) for r in cur.fetchall()]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_pending_settlements(limit: int = 50) -> list[dict]:
    """[v5 req1] 扫【补偿队列】(settlement_pending / refund_pending)· 供 scheduler reconcile 补偿。"""
    if limit is None or limit <= 0:
        limit = 50
    limit = min(limit, 500)
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, user_id, brand_id, status, freeze_id, freeze_table, pending_terminal,
                   error_code, error_detail, result_json, settle_retry_count, data_mode
            FROM geo_plan_tasks
            WHERE status IN ('settlement_pending','refund_pending')
              AND archived_at IS NULL
            ORDER BY id ASC
            LIMIT %s
            """,
            (limit,),
        )
        return [dict(r) if hasattr(r, "keys") else r for r in cur.fetchall()]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def bump_settle_retry(task_id: int, error: Optional[str] = None) -> int:
    """[v5 req1] 补偿重试仍失败 → 记 retry 次数 + 最近错误(状态不变 · 留队列下轮再试)。返回新 retry_count。"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE geo_plan_tasks SET settle_retry_count = settle_retry_count + 1, "
            "settle_last_error = %s WHERE id = %s RETURNING settle_retry_count",
            (str(error)[:400] if error else None, task_id),
        )
        row = cur.fetchone()
        conn.commit()
        return int(row["cnt"]) if row and "cnt" in row else (int(row["settle_retry_count"]) if row else 0)
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# 辅助:API 层可能用到
# ============================================================


def update_freeze_id(task_id: int, freeze_id: int, freeze_table: Optional[str] = None) -> None:
    """回填 freeze_id (先 create_task 后 freeze_points 的两步流程用).

    API 层流程:
      1. task_id = create_task(freeze_id=None)
      2. fr = await freeze_points(user_id, 'geo_plan_l1l2_fallback', task_ref=f'geoplan_{task_id}')
      3. update_freeze_id(task_id, fr['freeze_id'], fr['freeze_table'])

    [A0] freeze_table('legacy'|'v35')回填,worker commit/release 时回传给 billing 免猜撞号歧义。
    """
    conn = _get_conn()
    try:
        cur = conn.cursor()
        if freeze_table is not None:
            try:
                cur.execute(
                    "UPDATE geo_plan_tasks SET freeze_id = %s, freeze_table = %s WHERE id = %s",
                    (freeze_id, freeze_table, task_id),
                )
            except Exception:
                # [A0] 列未建(部署序:migration 未先行)→ 降级只写 freeze_id · 不阻断主冻结回填
                conn.rollback()
                cur.execute(
                    "UPDATE geo_plan_tasks SET freeze_id = %s WHERE id = %s",
                    (freeze_id, task_id),
                )
        else:
            cur.execute(
                "UPDATE geo_plan_tasks SET freeze_id = %s WHERE id = %s",
                (freeze_id, task_id),
            )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def update_linked_quote(task_id: int, quote_id: int) -> None:
    """任务完成后,用户点"确认方案"生成 quote,回填 quote_id."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE geo_plan_tasks SET linked_quote_id = %s WHERE id = %s",
            (quote_id, task_id),
        )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def archive_old_done(days_threshold: int = 90) -> int:
    """把 done_at < now - N 天的任务标 archived_at = now (PLAN 03 scheduler 每天调).

    不删数据,只打标记. archived_at IS NOT NULL 的在 list_tasks_by_user 默认过滤.

    Returns: 被归档的行数
    """
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE geo_plan_tasks
               SET archived_at = NOW()
             WHERE archived_at IS NULL
               AND done_at IS NOT NULL
               AND done_at < NOW() - (%s || ' days')::interval
            """,
            (str(days_threshold),),
        )
        n = cur.rowcount
        conn.commit()
        if n:
            logger.info(f"[GeoPlanTasks] archive_old_done archived {n} tasks (older than {days_threshold}d)")
        return int(n)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def archive_done_older_than(days: int = 90) -> int:
    """把 done_at < now - N 天且 status 处于终态的任务标 archived_at = now.

    和 archive_old_done 的差异: 本函数额外限定 status IN ('done','failed','cancelled','timeout')
    防御性: 即使 done_at 被其他路径意外写入,非终态任务也不会被错误归档.
    PLAN 03 scheduler 每日凌晨 3 点调, 对应 D15 维度 (离线归档).

    Args:
        days: 阈值天数 (默认 90)

    Returns: 被归档的行数
    """
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE geo_plan_tasks
               SET archived_at = NOW()
             WHERE archived_at IS NULL
               AND done_at IS NOT NULL
               AND done_at < NOW() - (%s || ' days')::interval
               AND status IN ('done','failed','cancelled','timeout')
            """,
            (str(days),),
        )
        n = cur.rowcount
        conn.commit()
        if n:
            logger.info(
                f"[GeoPlanTasks] archive_done_older_than archived {n} tasks (older than {days}d, terminal status)"
            )
        return int(n)
    finally:
        try:
            conn.close()
        except Exception:
            pass
