"""
AI Ops Center 数据层 · 2026-07-01

6 张表(建表见 scripts/migration_ai_ops_center_2026_07_01.sql):
  - ai_ops_tasks       · 运维任务主表(任务总线)
  - ai_ops_task_events · 任务事件流(时间线 + 审计)
  - ai_ops_artifacts   · 任务产物(context / codex 输出 / patch / 测试日志 / 日报)
  - ai_ops_approvals   · 高危动作审批(L3/L4)
  - ai_ops_reports     · 每日运维/运营日报((report_date, report_type) 幂等)
  - ai_ops_policies    · 权限策略 + Kill Switch(key/value)

约定(与 db/faq_db.py 一致):
  - 连接从 db.connection.get_connection() 取(池化 · RealDictCursor · 非 autocommit)
  - 写操作显式 conn.commit();统一 try/finally: conn.close() 归还池
  - API 层不拼 SQL,只调本模块小函数
  - 任务领取用 FOR UPDATE SKIP LOCKED,多 Worker 不重复领取
"""

import logging
from typing import Any, Optional
import psycopg2  # noqa: F401 · 与 db 家族保持一致的导入约定

logger = logging.getLogger("AiOps-DB")


def _get_conn():
    from db.connection import get_connection
    return get_connection()


# ==========================================
# 常量(与 migration CHECK 保持一致 · 应用层先挡一次)
# ==========================================

SOURCE_TYPES = ('feedback', 'chat', 'schedule', 'alert', 'manual')
TASK_KINDS = ('diagnose', 'fix', 'report', 'ssh_action', 'code_review')
TASK_STATUSES = ('queued', 'running', 'waiting_approval', 'succeeded', 'failed', 'cancelled')
RISK_LEVELS = ('L0', 'L1', 'L2', 'L3', 'L4')
PRIORITIES = ('P0', 'P1', 'P2', 'P3')
EVENT_SEVERITIES = ('info', 'warn', 'error', 'security')
APPROVAL_RISK_LEVELS = ('L3', 'L4')
APPROVAL_STATUSES = ('pending', 'approved', 'rejected', 'expired', 'executed')
REPORT_TYPES = ('daily', 'weekly', 'manual')
REPORT_STATUSES = ('generating', 'ready', 'failed')

# 允许写的策略 key 白名单(PATCH /policies/{key} 只认这些 · 防写任意 key)
POLICY_KEYS = (
    'ai_ops.enabled', 'codex.diagnose.enabled', 'codex.fix.enabled',
    'ssh_runner.enabled', 'ai_ops.kill_switch',
    'ai_ops.chat_llm.enabled',      # 命令台 LLM 意图分类+总管(默认关 · 失败降级规则)
    'ai_ops.glm_triage.enabled',    # GLM 一线分诊(只分诊/回复,不改代码不部署 · 默认关)
    'auto_deploy.enabled',          # L1 自动部署总闸(本轮不实现执行,仅占位 · 默认关)
    'ai_ops.auto_create_from_feedback',  # bug 反馈自动建诊断任务(后台可点 · 默认关 · env 同名开关兼容保留)
    'ai_ops.patrol.enabled',        # 主动巡逻(每5分钟评估告警规则 · 默认关 · 包B)
)

ALERT_SEVERITIES = ('info', 'warn', 'critical')
ALERT_STATUSES = ('firing', 'resolved')

# 领取任务的优先级排序(P0 最先)· CASE 表达式复用
_PRIORITY_ORDER_SQL = (
    "CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END"
)


# ==========================================
# 任务(ai_ops_tasks)
# ==========================================

def create_task(
    *,
    kind: str,
    source_type: str = 'manual',
    source_id: str = '',
    source_context: Optional[dict] = None,
    feedback_id: Optional[int] = None,
    title: str = '',
    instruction: str = '',
    risk_level: str = 'L0',
    priority: str = 'P3',
    created_by: Optional[int] = None,
    task_key: Optional[str] = None,
) -> tuple[dict, bool]:
    """
    新建任务。task_key 传值时做幂等:已存在直接返回现有任务。
    返回 (task_dict, created)。created=False 表示命中已有幂等任务。
    """
    if kind not in TASK_KINDS:
        raise ValueError(f"invalid kind: {kind}")
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"invalid source_type: {source_type}")
    if risk_level not in RISK_LEVELS:
        raise ValueError(f"invalid risk_level: {risk_level}")
    if priority not in PRIORITIES:
        raise ValueError(f"invalid priority: {priority}")

    import json
    ctx_json = json.dumps(source_context or {}, ensure_ascii=False)

    conn = _get_conn()
    try:
        cur = conn.cursor()
        # task_key 为 NULL 时永不冲突(Postgres UNIQUE 允许多个 NULL);
        # 传值且已存在 → DO NOTHING → 无 RETURNING 行 → 走幂等分支。
        cur.execute(
            """
            INSERT INTO ai_ops_tasks
              (task_key, source_type, source_id, source_context_jsonb, feedback_id,
               kind, title, instruction, risk_level, priority, created_by)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (task_key) DO NOTHING
            RETURNING id
            """,
            (task_key, source_type, source_id, ctx_json, feedback_id,
             kind, title, instruction, risk_level, priority, created_by),
        )
        row = cur.fetchone()
        if row:
            conn.commit()
            task = get_task(row['id'])
            _append_event_conn(conn, row['id'], 'created',
                               f"任务已创建 · kind={kind} · risk={risk_level}", None, 'info')
            conn.commit()
            return task, True
        # 幂等命中已有任务
        if task_key is not None:
            cur.execute("SELECT id FROM ai_ops_tasks WHERE task_key = %s", (task_key,))
            existing = cur.fetchone()
            conn.commit()
            if existing:
                return get_task(existing['id']), False
        # 极端兜底:未命中 RETURNING 也没有幂等 key(理论不该到这)
        conn.rollback()
        raise RuntimeError("create_task 未返回 id 且无幂等 key 可回查")
    finally:
        conn.close()


def get_task(task_id: int) -> Optional[dict]:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM ai_ops_tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_task_by_key(task_key: str) -> Optional[dict]:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM ai_ops_tasks WHERE task_key = %s", (task_key,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_tasks(
    status: Optional[str] = None,
    kind: Optional[str] = None,
    feedback_id: Optional[int] = None,
    limit: int = 100,
) -> list[dict]:
    where = []
    params: list[Any] = []
    if status:
        where.append("status = %s")
        params.append(status)
    if kind:
        where.append("kind = %s")
        params.append(kind)
    if feedback_id is not None:
        where.append("feedback_id = %s")
        params.append(feedback_id)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT * FROM ai_ops_tasks
            {where_sql}
            ORDER BY {_PRIORITY_ORDER_SQL}, created_at DESC
            LIMIT %s
            """,
            [*params, limit],
        )
        return list(cur.fetchall())
    finally:
        conn.close()


def claim_next_task(worker_id: str, allowed_kinds: Optional[list[str]] = None) -> Optional[dict]:
    """
    领取下一条 queued 任务(按优先级 + 时间)。
    FOR UPDATE SKIP LOCKED 保证多 Worker 不重复领取。
    命中即在同事务里标 running + assigned_worker_id + started_at,commit 后返回。
    无可领任务返回 None。
    """
    kind_filter = ""
    params: list[Any] = []
    if allowed_kinds:
        placeholders = ", ".join(["%s"] * len(allowed_kinds))
        kind_filter = f"AND kind IN ({placeholders})"
        params.extend(allowed_kinds)

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT id FROM ai_ops_tasks
            WHERE status = 'queued' {kind_filter}
            ORDER BY {_PRIORITY_ORDER_SQL}, created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """,
            params,
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()  # 结束只读事务,不长期持锁
            return None
        task_id = row['id']
        cur.execute(
            """
            UPDATE ai_ops_tasks
            SET status = 'running',
                assigned_worker_id = %s,
                started_at = COALESCE(started_at, NOW()),
                updated_at = NOW()
            WHERE id = %s
            """,
            (worker_id, task_id),
        )
        _append_event_conn(conn, task_id, 'claimed',
                           f"Worker {worker_id} 领取任务", None, 'info')
        conn.commit()
        return get_task(task_id)
    finally:
        conn.close()


def update_task_status(
    task_id: int,
    status: Optional[str] = None,
    *,
    summary: Optional[str] = None,
    result: Optional[dict] = None,
    worktree_path: Optional[str] = None,
    codex_session_id: Optional[str] = None,
    assigned_worker_id: Optional[str] = None,
) -> bool:
    """部分更新任务状态/结果。进入终态时自动填 finished_at。返回是否有行被改。"""
    if status is not None and status not in TASK_STATUSES:
        raise ValueError(f"invalid status: {status}")

    import json
    fields = []
    params: list[Any] = []
    if status is not None:
        fields.append("status = %s")
        params.append(status)
        if status in ('succeeded', 'failed', 'cancelled'):
            fields.append("finished_at = COALESCE(finished_at, NOW())")
        if status == 'running':
            fields.append("started_at = COALESCE(started_at, NOW())")
    if summary is not None:
        fields.append("summary = %s")
        params.append(summary)
    if result is not None:
        fields.append("result_jsonb = %s::jsonb")
        params.append(json.dumps(result, ensure_ascii=False))
    if worktree_path is not None:
        fields.append("worktree_path = %s")
        params.append(worktree_path)
    if codex_session_id is not None:
        fields.append("codex_session_id = %s")
        params.append(codex_session_id)
    if assigned_worker_id is not None:
        fields.append("assigned_worker_id = %s")
        params.append(assigned_worker_id)
    if not fields:
        return False
    fields.append("updated_at = NOW()")
    params.append(task_id)

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE ai_ops_tasks SET {', '.join(fields)} WHERE id = %s RETURNING id,kind,status",
            params,
        )
        changed_row = cur.fetchone()
        changed = changed_row is not None
        if changed and status == 'failed':
            from datetime import datetime, timezone
            from services.notification_events import NotificationEventType
            from services.notification_outbox import enqueue_admin_notification_events

            kind = str(changed_row.get('kind') or 'background_job')
            event_type = (
                NotificationEventType.EXTERNAL_CHANNEL_FAILED
                if any(token in kind.lower() for token in ('publish', 'external', 'channel'))
                else NotificationEventType.SYSTEM_JOB_FAILED
            )
            enqueue_admin_notification_events(
                cur,
                event_type=event_type,
                business_id=f"ai_ops:{int(task_id)}",
                terminal_state='failed',
                facts={
                    'business_no': f"AI-OPS-{int(task_id)}",
                    'status': '需要处理',
                    'occurred_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                    'summary': '后台任务已停止，请在运维页面查看安全摘要并处理。',
                },
            )
        conn.commit()
        return changed
    finally:
        conn.close()


def cancel_task(task_id: int) -> bool:
    """把未终态任务标 cancelled。已终态返回 False。"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE ai_ops_tasks
            SET status = 'cancelled',
                finished_at = COALESCE(finished_at, NOW()),
                updated_at = NOW()
            WHERE id = %s
              AND status IN ('queued', 'running', 'waiting_approval')
            """,
            (task_id,),
        )
        changed = cur.rowcount > 0
        if changed:
            _append_event_conn(conn, task_id, 'cancelled', "任务已取消", None, 'warn')
        conn.commit()
        return changed
    finally:
        conn.close()


def count_tasks_by_status() -> dict:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status, COUNT(*) AS c FROM ai_ops_tasks GROUP BY status")
        result = {r['status']: r['c'] for r in cur.fetchall()}
        for s in TASK_STATUSES:
            result.setdefault(s, 0)
        return result
    finally:
        conn.close()


# ==========================================
# 事件(ai_ops_task_events)
# ==========================================

def _append_event_conn(conn, task_id: int, event_type: str, message: str,
                       payload: Optional[dict], severity: str) -> None:
    """在已有连接/事务里写一条事件(不自己 commit,由调用方统一提交)。"""
    import json
    if severity not in EVENT_SEVERITIES:
        severity = 'info'
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO ai_ops_task_events (task_id, event_type, severity, message, payload_jsonb)
        VALUES (%s, %s, %s, %s, %s::jsonb)
        """,
        (task_id, event_type, severity, message or '',
         json.dumps(payload or {}, ensure_ascii=False)),
    )


def append_event(task_id: int, event_type: str, message: str = '',
                 payload: Optional[dict] = None, severity: str = 'info') -> None:
    """独立写一条任务事件并提交。"""
    conn = _get_conn()
    try:
        _append_event_conn(conn, task_id, event_type, message, payload, severity)
        conn.commit()
    finally:
        conn.close()


def list_events(task_id: int, after_id: Optional[int] = None, limit: int = 500) -> list[dict]:
    """按时间正序拿事件流。after_id 用于前端增量轮询。"""
    where = ["task_id = %s"]
    params: list[Any] = [task_id]
    if after_id is not None:
        where.append("id > %s")
        params.append(after_id)
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT * FROM ai_ops_task_events
            WHERE {' AND '.join(where)}
            ORDER BY id ASC
            LIMIT %s
            """,
            [*params, limit],
        )
        return list(cur.fetchall())
    finally:
        conn.close()


# ==========================================
# 产物(ai_ops_artifacts)
# ==========================================

def add_artifact(
    task_id: int,
    artifact_type: str,
    *,
    title: str = '',
    content_text: str = '',
    storage_type: str = 'db',
    storage_url: str = '',
    metadata: Optional[dict] = None,
) -> int:
    import json
    if storage_type not in ('db', 'file', 'oss'):
        raise ValueError(f"invalid storage_type: {storage_type}")
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ai_ops_artifacts
              (task_id, artifact_type, title, storage_type, content_text, storage_url, metadata_jsonb)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            RETURNING id
            """,
            (task_id, artifact_type, title, storage_type, content_text, storage_url,
             json.dumps(metadata or {}, ensure_ascii=False)),
        )
        new_id = cur.fetchone()['id']
        conn.commit()
        return new_id
    finally:
        conn.close()


def list_artifacts(task_id: int, artifact_type: Optional[str] = None) -> list[dict]:
    where = ["task_id = %s"]
    params: list[Any] = [task_id]
    if artifact_type:
        where.append("artifact_type = %s")
        params.append(artifact_type)
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT * FROM ai_ops_artifacts
            WHERE {' AND '.join(where)}
            ORDER BY created_at DESC, id DESC
            """,
            params,
        )
        return list(cur.fetchall())
    finally:
        conn.close()


# ==========================================
# 审批(ai_ops_approvals)
# ==========================================

def create_approval(
    task_id: int,
    action_type: str,
    *,
    risk_level: str = 'L4',
    requested_by: Optional[int] = None,
    requested_reason: str = '',
    command_plan: Optional[dict] = None,
    expires_at=None,
) -> int:
    import json
    if risk_level not in APPROVAL_RISK_LEVELS:
        raise ValueError(f"invalid approval risk_level: {risk_level}")
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ai_ops_approvals
              (task_id, action_type, risk_level, requested_by, requested_reason,
               command_plan_jsonb, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
            RETURNING id
            """,
            (task_id, action_type, risk_level, requested_by, requested_reason,
             json.dumps(command_plan or {}, ensure_ascii=False), expires_at),
        )
        new_id = cur.fetchone()['id']
        _append_event_conn(conn, task_id, 'approval_requested',
                           f"申请审批 · {action_type} · {risk_level}",
                           {"approval_id": new_id}, 'security')
        # 任务进入待审批
        cur.execute(
            """UPDATE ai_ops_tasks SET status = 'waiting_approval', updated_at = NOW()
               WHERE id = %s AND status IN ('queued', 'running')""",
            (task_id,),
        )
        conn.commit()
        return new_id
    finally:
        conn.close()


def get_approval(approval_id: int) -> Optional[dict]:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM ai_ops_approvals WHERE id = %s", (approval_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_approvals(status: Optional[str] = None, limit: int = 100) -> list[dict]:
    where = []
    params: list[Any] = []
    if status:
        where.append("approval_status = %s")
        params.append(status)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT * FROM ai_ops_approvals
            {where_sql}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            [*params, limit],
        )
        return list(cur.fetchall())
    finally:
        conn.close()


def approve_action(approval_id: int, approved_by: int) -> bool:
    """审批通过。只把 pending 改 approved(不代表已执行,SSH Runner 另行领取)。"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE ai_ops_approvals
            SET approval_status = 'approved', approved_by = %s, approved_at = NOW()
            WHERE id = %s AND approval_status = 'pending'
            RETURNING task_id
            """,
            (approved_by, approval_id),
        )
        row = cur.fetchone()
        if row:
            _append_event_conn(conn, row['task_id'], 'approval_approved',
                               f"审批通过 · approval={approval_id} · by={approved_by}",
                               {"approval_id": approval_id}, 'security')
        conn.commit()
        return bool(row)
    finally:
        conn.close()


def reject_action(approval_id: int, approved_by: int, reason: str = '') -> bool:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE ai_ops_approvals
            SET approval_status = 'rejected', approved_by = %s, approved_at = NOW()
            WHERE id = %s AND approval_status = 'pending'
            RETURNING task_id
            """,
            (approved_by, approval_id),
        )
        row = cur.fetchone()
        if row:
            _append_event_conn(conn, row['task_id'], 'approval_rejected',
                               f"审批驳回 · approval={approval_id} · {reason}",
                               {"approval_id": approval_id, "reason": reason}, 'security')
        conn.commit()
        return bool(row)
    finally:
        conn.close()


def mark_approval_executed(approval_id: int) -> bool:
    """SSH Runner 执行成功后标记 executed(幂等 · 防每轮重复执行)。只对 approved 生效。"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE ai_ops_approvals
            SET approval_status = 'executed', executed_at = NOW()
            WHERE id = %s AND approval_status = 'approved'
            RETURNING task_id
            """,
            (approval_id,),
        )
        row = cur.fetchone()
        if row:
            _append_event_conn(conn, row['task_id'], 'ssh_action_executed',
                               f"动作已执行并标记 executed · approval={approval_id}",
                               {"approval_id": approval_id}, 'security')
        conn.commit()
        return bool(row)
    finally:
        conn.close()


def count_approvals_by_status() -> dict:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT approval_status, COUNT(*) AS c FROM ai_ops_approvals GROUP BY approval_status"
        )
        result = {r['approval_status']: r['c'] for r in cur.fetchall()}
        for s in APPROVAL_STATUSES:
            result.setdefault(s, 0)
        return result
    finally:
        conn.close()


# ==========================================
# 日报(ai_ops_reports)
# ==========================================

def save_report(
    report_date,
    *,
    report_type: str = 'daily',
    status: str = 'ready',
    markdown: str = '',
    summary: str = '',
    metrics: Optional[dict] = None,
    action_items: Optional[list] = None,
    generated_by_task_id: Optional[int] = None,
) -> int:
    """
    upsert 日报((report_date, report_type) 唯一)。同日重复生成覆盖同一行,幂等。
    返回 report id。
    """
    import json
    if report_type not in REPORT_TYPES:
        raise ValueError(f"invalid report_type: {report_type}")
    if status not in REPORT_STATUSES:
        raise ValueError(f"invalid report status: {status}")
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ai_ops_reports
              (report_date, report_type, status, markdown, summary,
               metrics_jsonb, action_items_jsonb, generated_by_task_id)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
            ON CONFLICT (report_date, report_type) DO UPDATE SET
              status = EXCLUDED.status,
              markdown = EXCLUDED.markdown,
              summary = EXCLUDED.summary,
              metrics_jsonb = EXCLUDED.metrics_jsonb,
              action_items_jsonb = EXCLUDED.action_items_jsonb,
              generated_by_task_id = EXCLUDED.generated_by_task_id,
              updated_at = NOW()
            RETURNING id
            """,
            (report_date, report_type, status, markdown, summary,
             json.dumps(metrics or {}, ensure_ascii=False),
             json.dumps(action_items or [], ensure_ascii=False),
             generated_by_task_id),
        )
        report_id = cur.fetchone()['id']
        conn.commit()
        return report_id
    finally:
        conn.close()


def get_report(report_date, report_type: str = 'daily') -> Optional[dict]:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM ai_ops_reports WHERE report_date = %s AND report_type = %s",
            (report_date, report_type),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_reports(limit: int = 60) -> list[dict]:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, report_date, report_type, status, summary,
                   action_items_jsonb, generated_by_task_id, created_at, updated_at
            FROM ai_ops_reports
            ORDER BY report_date DESC, report_type
            LIMIT %s
            """,
            (limit,),
        )
        return list(cur.fetchall())
    finally:
        conn.close()


def get_latest_report(report_type: str = 'daily') -> Optional[dict]:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT * FROM ai_ops_reports WHERE report_type = %s
               ORDER BY report_date DESC LIMIT 1""",
            (report_type,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ==========================================
# 策略 / Kill Switch(ai_ops_policies)
# ==========================================

def get_policies() -> dict:
    """返回 {key: value_dict}。"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT key, value_jsonb FROM ai_ops_policies")
        return {r['key']: r['value_jsonb'] for r in cur.fetchall()}
    finally:
        conn.close()


def get_policy(key: str) -> Optional[dict]:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT value_jsonb FROM ai_ops_policies WHERE key = %s", (key,))
        row = cur.fetchone()
        return row['value_jsonb'] if row else None
    finally:
        conn.close()


def set_policy(key: str, value: dict, updated_by: Optional[int] = None) -> None:
    import json
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ai_ops_policies (key, value_jsonb, updated_by, updated_at)
            VALUES (%s, %s::jsonb, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET
              value_jsonb = EXCLUDED.value_jsonb,
              updated_by = EXCLUDED.updated_by,
              updated_at = NOW()
            """,
            (key, json.dumps(value or {}, ensure_ascii=False), updated_by),
        )
        conn.commit()
    finally:
        conn.close()


def is_flag_enabled(key: str, default: bool = False) -> bool:
    """读某策略 key 的 {"enabled": bool}。缺失/异常返回 default。"""
    val = get_policy(key)
    if not isinstance(val, dict):
        return default
    return bool(val.get('enabled', default))


def is_kill_switch_enabled() -> bool:
    return is_flag_enabled('ai_ops.kill_switch', default=False)


# ==========================================
# Runner 心跳(ai_ops_worker_heartbeats · P1-B)
# ==========================================

def upsert_heartbeat(
    worker_id: str,
    *,
    host: str = '',
    version: str = '',
    env_enabled: bool = False,
    codex_available: bool = False,
    ssh_runner_enabled: bool = False,
    note: str = '',
) -> None:
    """Worker 上报心跳(worker_id 主键 upsert · last_seen_at 每次刷新)。只存运行元数据,不存密钥。"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ai_ops_worker_heartbeats
              (worker_id, host, version, last_seen_at, env_enabled,
               codex_available, ssh_runner_enabled, note)
            VALUES (%s, %s, %s, NOW(), %s, %s, %s, %s)
            ON CONFLICT (worker_id) DO UPDATE SET
              host = EXCLUDED.host,
              version = EXCLUDED.version,
              last_seen_at = NOW(),
              env_enabled = EXCLUDED.env_enabled,
              codex_available = EXCLUDED.codex_available,
              ssh_runner_enabled = EXCLUDED.ssh_runner_enabled,
              note = EXCLUDED.note
            """,
            (worker_id, host[:200], version[:100], bool(env_enabled),
             bool(codex_available), bool(ssh_runner_enabled), note[:500]),
        )
        conn.commit()
    finally:
        conn.close()


def get_runner_status(stale_seconds: int = 120, on_error: str = 'none') -> Optional[dict]:
    """
    返回最近一次 Worker 心跳 + 在线判定(last_seen_at 在 stale 窗口内 → online）。
    无任何心跳行 → None(控制塔显示"未接入")。
    查询异常:on_error='none'(默认 · overview 用)返回 None 保 200;
    on_error='raise'(巡逻用)重新抛出——巡逻必须区分"查询失败"和"从未接入",
    否则会把真实离线告警误消(包B 复审 P3-1)。两路都先 rollback 防连接池污染。
    """
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT worker_id, host, version, last_seen_at, env_enabled,
                   codex_available, ssh_runner_enabled, note,
                   EXTRACT(EPOCH FROM (NOW() - last_seen_at)) AS age_seconds
            FROM ai_ops_worker_heartbeats
            ORDER BY last_seen_at DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
        conn.commit()
        if not row:
            return None
        d = dict(row)
        age = int(float(d.pop('age_seconds') or 0))
        d['age_seconds'] = age
        d['online'] = age <= stale_seconds
        return d
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("[ai_ops] 读取 Runner 心跳失败(可能迁移未落库): %s", e)
        if on_error == 'raise':
            raise
        return None
    finally:
        conn.close()


# ==========================================
# 告警 / 巡逻(ai_ops_alerts + ai_ops_patrol_runs · 包B)
# ==========================================

def upsert_alert(
    rule_key: str,
    *,
    severity: str = 'warn',
    title: str,
    detail: str = '',
    fingerprint: str = '',
    payload: Optional[dict] = None,
) -> dict:
    """
    开启/续报一条告警。同 (rule_key, fingerprint) 已有 firing 行 → 只刷新
    last_seen_at/detail/severity(部分唯一索引兜底并发);否则插入新 firing 行。
    返回 {"alert": row, "opened": bool}。opened=True 表示这是新拉响的告警。
    """
    import json
    if severity not in ALERT_SEVERITIES:
        severity = 'warn'
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE ai_ops_alerts
            SET last_seen_at = NOW(), detail = %s, severity = %s,
                payload = %s::jsonb, title = %s
            WHERE rule_key = %s AND fingerprint = %s AND status = 'firing'
            RETURNING *
            """,
            (detail, severity, json.dumps(payload or {}, ensure_ascii=False, default=str),
             title, rule_key, fingerprint),
        )
        row = cur.fetchone()
        if row:
            conn.commit()
            return {"alert": dict(row), "opened": False}
        cur.execute(
            """
            INSERT INTO ai_ops_alerts (rule_key, fingerprint, severity, title, detail, payload)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (rule_key, fingerprint) WHERE status = 'firing' DO UPDATE
              SET last_seen_at = NOW(), detail = EXCLUDED.detail,
                  severity = EXCLUDED.severity, title = EXCLUDED.title,
                  payload = EXCLUDED.payload
            RETURNING *
            """,
            (rule_key, fingerprint, severity, title, detail,
             json.dumps(payload or {}, ensure_ascii=False, default=str)),
        )
        row = cur.fetchone()
        conn.commit()
        return {"alert": dict(row), "opened": True}
    finally:
        conn.close()


def resolve_alerts(rule_key: str, fingerprint: Optional[str] = None,
                   resolved_by: Optional[int] = None) -> int:
    """恢复某规则(可选限定 fingerprint)的全部 firing 告警,返回恢复条数。"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        if fingerprint is None:
            cur.execute(
                """
                UPDATE ai_ops_alerts
                SET status = 'resolved', resolved_at = NOW(), resolved_by = %s
                WHERE rule_key = %s AND status = 'firing'
                """,
                (resolved_by, rule_key),
            )
        else:
            cur.execute(
                """
                UPDATE ai_ops_alerts
                SET status = 'resolved', resolved_at = NOW(), resolved_by = %s
                WHERE rule_key = %s AND fingerprint = %s AND status = 'firing'
                """,
                (resolved_by, rule_key, fingerprint),
            )
        n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def resolve_alert_by_id(alert_id: int, resolved_by: Optional[int] = None) -> Optional[dict]:
    """手动恢复单条告警(admin 按钮)。已 resolved 的返回 None(幂等语义交给调用方)。"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE ai_ops_alerts
            SET status = 'resolved', resolved_at = NOW(), resolved_by = %s
            WHERE id = %s AND status = 'firing'
            RETURNING *
            """,
            (resolved_by, alert_id),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        conn.close()


def set_alert_task(alert_id: int, task_id: int) -> None:
    """把自动立案的诊断任务挂到告警上(一条 firing 告警只立一案)。"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE ai_ops_alerts SET task_id = %s WHERE id = %s", (task_id, alert_id))
        conn.commit()
    finally:
        conn.close()


def list_alerts(status: Optional[str] = None, limit: int = 100) -> list[dict]:
    # 年龄在 DB 侧算(EXTRACT EPOCH):前端拿相对秒数展示,
    # 避免 naive TIMESTAMP 被浏览器 new Date() 按本地时区错位(心跳包同款教训)。
    cols = ("*, EXTRACT(EPOCH FROM (NOW() - first_seen_at))::bigint AS first_seen_age_seconds, "
            "EXTRACT(EPOCH FROM (NOW() - last_seen_at))::bigint AS last_seen_age_seconds")
    conn = _get_conn()
    try:
        cur = conn.cursor()
        if status:
            cur.execute(
                f"SELECT {cols} FROM ai_ops_alerts WHERE status = %s ORDER BY last_seen_at DESC LIMIT %s",
                (status, int(limit)),
            )
        else:
            cur.execute(
                f"SELECT {cols} FROM ai_ops_alerts ORDER BY (status = 'firing') DESC, last_seen_at DESC LIMIT %s",
                (int(limit),),
            )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def count_firing_alerts() -> int:
    """fail-soft:表未落库/异常返回 0(overview 不因巡逻表缺失而 500)。"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM ai_ops_alerts WHERE status = 'firing'")
        return int(cur.fetchone()['n'])
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("[ai_ops] 统计 firing 告警失败(可能迁移未落库): %s", e)
        return 0
    finally:
        conn.close()


def record_patrol_run(*, firing_count: int, opened_count: int,
                      resolved_count: int, duration_ms: int, note: str = '') -> None:
    """记录一次巡逻(打卡),并裁剪只留最近 500 条。"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ai_ops_patrol_runs (firing_count, opened_count, resolved_count, duration_ms, note)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (firing_count, opened_count, resolved_count, duration_ms, note),
        )
        cur.execute(
            """
            DELETE FROM ai_ops_patrol_runs
            WHERE id NOT IN (SELECT id FROM ai_ops_patrol_runs ORDER BY ran_at DESC, id DESC LIMIT 500)
            """
        )
        conn.commit()
    finally:
        conn.close()


def get_last_patrol_run() -> Optional[dict]:
    """fail-soft:表未落库/异常返回 None。"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT *, EXTRACT(EPOCH FROM (NOW() - ran_at)) AS age_seconds
            FROM ai_ops_patrol_runs ORDER BY ran_at DESC, id DESC LIMIT 1
            """
        )
        row = cur.fetchone()
        conn.commit()
        if not row:
            return None
        d = dict(row)
        d['age_seconds'] = int(float(d.pop('age_seconds') or 0))
        return d
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("[ai_ops] 读取巡逻记录失败(可能迁移未落库): %s", e)
        return None
    finally:
        conn.close()


def get_patrol_signals() -> Optional[dict]:
    """
    巡逻规则的原始信号,一次往返全取。所有时间差都在 DB 侧算
    (EXTRACT EPOCH · 同 get_runner_status),避免应用时区 vs DB 时区错位。
    fail-soft:异常返回 None(巡逻这轮跳过,不炸 scheduler)。
    """
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM ai_ops_tasks
                WHERE status = 'failed' AND finished_at > NOW() - INTERVAL '24 hours') AS failed_24h,
              (SELECT COALESCE(json_agg(json_build_object('id', id, 'title', LEFT(title, 60))), '[]'::json)
                 FROM (SELECT id, title FROM ai_ops_tasks
                        WHERE status = 'failed' AND finished_at > NOW() - INTERVAL '24 hours'
                        ORDER BY finished_at DESC LIMIT 5) t) AS failed_recent,
              (SELECT COUNT(*) FROM ai_ops_tasks WHERE status = 'queued') AS queued_count,
              (SELECT COALESCE(MAX(EXTRACT(EPOCH FROM (NOW() - created_at)))::bigint, 0)
                 FROM ai_ops_tasks WHERE status = 'queued') AS oldest_queued_seconds,
              (SELECT COALESCE(json_agg(json_build_object('id', id, 'age_seconds', age)), '[]'::json)
                 FROM (SELECT id, EXTRACT(EPOCH FROM (NOW() - started_at))::bigint AS age
                        FROM ai_ops_tasks
                        WHERE status = 'running' AND started_at < NOW() - INTERVAL '2 hours') s) AS stuck_running,
              (SELECT COUNT(*) FROM ai_ops_approvals
                WHERE approval_status = 'pending' AND created_at < NOW() - INTERVAL '24 hours') AS approvals_over_24h,
              (SELECT COUNT(*) FROM ai_ops_approvals
                WHERE approval_status = 'pending' AND created_at < NOW() - INTERVAL '72 hours') AS approvals_over_72h
            """
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("[ai_ops] 巡逻信号查询失败: %s", e)
        return None
    finally:
        conn.close()
