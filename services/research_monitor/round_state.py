"""
跑批 round 状态 CRUD + snapshot + heartbeat

A.5.3 round_runner 用本模块管理 round 生命周期:
- create_round_with_snapshot: 起跑前深拷贝 industries+prompts 进 snapshot_json
- update_heartbeat: 每 30 秒更新, server restart 后判断 stale 用
- update_round_progress: stage 推进 + 进度 (自动 pending → running)
- update_round_complete: 完成时设 finished_at + summary
- get_round_snapshot: 拿快照(restart 续跑用)
- get_round_status: 拿 round 当前状态
- truncate_large_jsonb: 大字段截断保护 (50KB 上限)

snapshot_json 上限 50KB, 超出截断为占位符 + original_size_bytes (学 audit_logs 模式
防 pg_dump 巨大字段拖慢备份)。
"""
import json
import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from db.connection import get_connection

logger = logging.getLogger("GEO-ResearchMonitor.RoundState")

MAX_JSONB_SIZE_KB = 50
ROUND_START_LOCK_ID = 2026050901
ACTIVE_ROUND_STATUSES = ('pending', 'running')


class RoundAlreadyRunningError(Exception):
    """已有 pending/running 调研跑批, 禁止并发启动新轮。"""


def truncate_large_jsonb(value: Optional[Dict], max_size_kb: int = MAX_JSONB_SIZE_KB) -> Optional[Dict]:
    """
    若 JSONB 序列化超 max_size_kb, 替换为占位符 + original_size_bytes。
    学 audit_logs 模式防 pg_dump 巨大字段拖慢备份。
    """
    if not value:
        return value
    serialized = json.dumps(value, ensure_ascii=False)
    size_bytes = len(serialized.encode('utf-8'))
    if size_bytes <= max_size_kb * 1024:
        return value
    return {
        'truncated': True,
        'original_size_bytes': size_bytes,
        'note': f'snapshot 超过 {max_size_kb}KB 截断, 防 pg_dump 拖慢',
        'preview': serialized[:200] + '...',
    }


def _generate_round_id() -> str:
    """生成唯一 round_id, 格式 round_YYYYmmdd_HHMMSS_microseconds"""
    return f"round_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"


def create_round_with_snapshot(
    triggered_by: str,
    industries: List[Dict],
    prompts_by_industry: Dict,
    triggered_user_id: Optional[int] = None,
    enforce_single_active: bool = False,
) -> str:
    """
    创建 round + 深拷贝 industries+prompts 进 snapshot_json。

    triggered_by: 'cron' / 'manual' / 'missed_cron_recovery'
    industries: [{'id', 'name', 'slug'}, ...]
    prompts_by_industry: {industry_id: [{'id', 'text', ...}, ...]}

    返回新 round_id。
    """
    round_id = _generate_round_id()
    batch_id = f"batch_{round_id}"

    snapshot = {
        'industries': industries,
        # JSON key 必须是 str
        'prompts_by_industry': {str(k): v for k, v in (prompts_by_industry or {}).items()},
        'snapshotted_at': datetime.now().isoformat(),
    }

    conn = get_connection()
    try:
        cur = conn.cursor()
        if enforce_single_active:
            cur.execute("SELECT pg_try_advisory_xact_lock(%s) AS locked", (ROUND_START_LOCK_ID,))
            lock_row = cur.fetchone() or {}
            locked = lock_row.get('locked') if isinstance(lock_row, dict) else lock_row[0]
            if not locked:
                raise RoundAlreadyRunningError("另一个调研跑批启动事务正在进行, 请稍后刷新")

            cur.execute(
                """
                SELECT round_id, status
                  FROM geo_research_round
                 WHERE status IN ('pending', 'running')
                 ORDER BY started_at DESC NULLS LAST, last_heartbeat_at DESC NULLS LAST
                 LIMIT 1
                """
            )
            active = cur.fetchone()
            if active:
                active_id = active.get('round_id') if isinstance(active, dict) else active[0]
                active_status = active.get('status') if isinstance(active, dict) else active[1]
                raise RoundAlreadyRunningError(
                    f"已有运行中的调研跑批 {active_id} ({active_status}), 请勿并发启动"
                )

        cur.execute(
            """
            INSERT INTO geo_research_round
                (round_id, batch_id, triggered_by, triggered_user_id, status,
                 snapshot_json, last_heartbeat_at)
            VALUES (%s, %s, %s, %s, 'pending', %s, NOW())
            """,
            (
                round_id,
                batch_id,
                triggered_by,
                triggered_user_id,
                json.dumps(snapshot, ensure_ascii=False),
            ),
        )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()

    return round_id


def update_heartbeat(round_id: str) -> bool:
    """推进 last_heartbeat_at, restart sweep 用此判断进程是否活着"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE geo_research_round SET last_heartbeat_at = NOW() WHERE round_id = %s",
            (round_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def update_round_progress(round_id: str, stage: str, progress: Dict) -> bool:
    """
    更新 current_stage + progress_json。
    自动把 status 推进到 'running' (如果还是 pending 的话),
    自动初始化 started_at (如果还没设过),
    顺手推进 last_heartbeat_at。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE geo_research_round
               SET current_stage = %s,
                   progress_json = %s,
                   status = CASE WHEN status = 'pending' THEN 'running' ELSE status END,
                   started_at = COALESCE(started_at, NOW()),
                   last_heartbeat_at = NOW()
             WHERE round_id = %s
            """,
            (stage, json.dumps(progress or {}, ensure_ascii=False), round_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def update_round_complete(round_id: str, status: str, summary: Dict) -> bool:
    """
    设 finished_at + status + summary_json。
    status: 'completed' / 'partial_success' / 'failed' / 'cancelled' / 'failed_resumable'
    summary 大字段走 truncate_large_jsonb 保护。

    管理员取消是更高优先级的终态: 一旦 DB 中已是 cancelled, runner 后续
    completed/failed/timeout 收尾都不能再覆盖它。status='cancelled' 自身仍允许
    写入, 方便月度预算熔断和进程取消路径记录原因。
    """
    summary = truncate_large_jsonb(summary or {})
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE geo_research_round
               SET status = %s,
                   finished_at = NOW(),
                   summary_json = COALESCE(summary_json, '{}'::jsonb) || %s::jsonb
             WHERE round_id = %s
               AND (%s = 'cancelled' OR status <> 'cancelled')
            RETURNING round_id
            """,
            (status, json.dumps(summary, ensure_ascii=False), round_id, status),
        )
        changed = cur.fetchone()
        if changed and status in {
            'completed', 'partial_success', 'failed', 'failed_resumable', 'cancelled'
        }:
            from services.notification_events import NotificationEventType
            from services.notification_outbox import enqueue_admin_notification_events

            event_type = {
                'completed': NotificationEventType.RESEARCH_COMPLETED,
                'partial_success': NotificationEventType.RESEARCH_PARTIAL,
                'cancelled': NotificationEventType.RESEARCH_CANCELLED,
            }.get(status, NotificationEventType.RESEARCH_FAILED)
            enqueue_admin_notification_events(
                cur,
                event_type=event_type,
                business_id=str(round_id),
                terminal_state=status,
                facts={
                    'business_no': str(round_id),
                    'status': {
                        'completed': '已完成',
                        'partial_success': '部分完成',
                        'cancelled': '已取消',
                    }.get(status, '未完成'),
                    'occurred_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                    'summary': '管理员调研轮已进入终态，请在调研页面查看结果。',
                },
            )
        conn.commit()
        return changed is not None
    finally:
        conn.close()


def mark_round_resume_requested(
    round_id: str,
    requested_by: Optional[int] = None,
    extra_summary: Optional[Dict] = None,
    allowed_statuses: tuple = ('failed_resumable', 'cancelled'),
) -> bool:
    """
    抢占一个 failed_resumable round 进入续跑队列。

    和 create_round_with_snapshot(enforce_single_active=True) 共用 advisory lock:
    - 若已有 pending/running 跑批, 抛 RoundAlreadyRunningError
    - 若目标 round 已被别人抢先改走, 返回 False
    - 抢到后标为 pending, 清 finished_at, 让 BackgroundTask 后续推进为 running

    extra_summary: [2026-07-16 超时自动续跑] 额外并入 summary_json 的字段
    (如 auto_resume_count / auto_resume_history)。必须与抢占同一条 UPDATE 原子落库:
    若拆成"抢占成功后再补写计数", 中间崩溃会丢计数 → 自动续跑失去上限约束。

    allowed_statuses: 允许被抢占的 status 集合(UPDATE WHERE 层原子生效, 不是
    先读后判的 TOCTOU 检查)。默认 ('failed_resumable','cancelled') = P14-v9
    admin 人工续跑按钮语义(cancelled 也可人工续)。**自动续跑路径必须传
    ('failed_resumable',)**: 出口审核抓到的复活缺陷 — 4h 超时收尾若输给
    admin cancel(update_round_complete 的 cancelled 守卫使收尾 no-op),
    run_round 仍返回 failed_resumable, 外壳若沿用默认窗口会把管理员刚
    cancel 的轮原子抢回 pending, 自动推翻人工终态。
    """
    allowed = tuple(allowed_statuses or ('failed_resumable',))
    resume_summary = {
        'resume_requested_by': requested_by,
        'resume_requested_at': datetime.now().isoformat(),
    }
    if extra_summary:
        resume_summary.update(extra_summary)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT pg_try_advisory_xact_lock(%s) AS locked", (ROUND_START_LOCK_ID,))
        lock_row = cur.fetchone() or {}
        locked = lock_row.get('locked') if isinstance(lock_row, dict) else lock_row[0]
        if not locked:
            raise RoundAlreadyRunningError("另一个调研跑批启动/续跑事务正在进行, 请稍后刷新")

        cur.execute(
            """
            SELECT round_id, status
              FROM geo_research_round
             WHERE status IN ('pending', 'running')
               AND round_id <> %s
             ORDER BY started_at DESC NULLS LAST, last_heartbeat_at DESC NULLS LAST
             LIMIT 1
            """,
            (round_id,),
        )
        active = cur.fetchone()
        if active:
            active_id = active.get('round_id') if isinstance(active, dict) else active[0]
            active_status = active.get('status') if isinstance(active, dict) else active[1]
            raise RoundAlreadyRunningError(
                f"已有运行中的调研跑批 {active_id} ({active_status}), 请勿并发续跑"
            )

        # P14-v9 (2026-05-27 老板反馈): cancelled 也允许续跑 · 跟 API RESUMABLE_STATUSES 对齐
        #   (人工按钮默认窗口; 自动续跑经 allowed_statuses 收窄, 见 docstring)
        # 注: 仍清空 current_stage/progress_json · _infer_resume_from_stage 已在 API 层
        #     在调本函数之前拿到 base.current_stage 推断完起点 · 这里清不影响断点续
        # [2026-07-16 出口审核修] started_at 由清 NULL 改置 NOW(): NULL 窗口内
        #   僵尸 sweep 的 6h 判据 COALESCE(started_at, created_at) 会落到老 created_at
        #   (续跑第 2/3 段时必然 >6h), 心跳再新鲜也会被 hard_timeout 分支判死 →
        #   活轮被标 failed_resumable + summary 被整体替换。置 NOW() 让每段 run_age
        #   从本段抢占起算(每段 ≤4h 硬超时 < 6h), 6h hard_timeout 分支不再触达
        #   活续跑轮; worker 没接手的真死 pending 仍被 10 分钟心跳分支正常回收。
        #   [二轮覆审订正] 心跳分支对活轮仍可能触达(段内 >10min 单点心跳空窗,
        #   base 既有竞态), 本修复只封 6h 分支, 口径详见 run_round_with_auto_resume
        #   docstring 已知口径 (b)。
        cur.execute(
            """
            UPDATE geo_research_round
               SET status = 'pending',
                   current_stage = NULL,
                   progress_json = NULL,
                   started_at = NOW(),
                   finished_at = NULL,
                   error_message = NULL,
                   last_heartbeat_at = NOW(),
                   summary_json = COALESCE(summary_json, '{}'::jsonb) || %s::jsonb
             WHERE round_id = %s
               AND status IN %s
            """,
            (json.dumps(resume_summary, ensure_ascii=False), round_id, allowed),
        )
        claimed = cur.rowcount > 0
        conn.commit()
        return claimed
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def get_round_snapshot(round_id: str) -> Optional[Dict]:
    """拿 snapshot_json, restart 续跑用"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT snapshot_json FROM geo_research_round WHERE round_id = %s",
            (round_id,),
        )
        row = cur.fetchone()
        return row['snapshot_json'] if row else None
    finally:
        conn.close()


def get_round_status(round_id: str) -> Optional[Dict]:
    """拿 round 当前状态(round_id / status / current_stage / progress / 时间戳 / summary)"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT round_id, batch_id, status, current_stage, progress_json,
                   started_at, last_heartbeat_at, finished_at, summary_json
              FROM geo_research_round
             WHERE round_id = %s
            """,
            (round_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def infer_resume_from_stage(current_stage: Optional[str]) -> str:
    """P14-v9 · 根据 round 上次停止的 current_stage 推算续跑起点

    [2026-07-16 超时自动续跑] 原 api/research_monitor_round_api._infer_resume_from_stage
    逐字下沉至此(services 层不 import api;api 侧保留同名别名)。
    调用约定: 必须在 mark_round_resume_requested 之前读 current_stage
    (抢占会清空 current_stage/progress_json)。

    后端 round.current_stage 写法示例:
      - stage_1_ai_fetch        (stage_1 进行中 · 中断)
      - stage_1_ai_fetch_done   (stage_1 完成 · 该跑 stage_2)
      - stage_2_prefilter_done  (stage_2 完成 · 该跑 stage_3)
      - stage_7_aggregate_done  (stage_7 完成 · 该跑 stage_8)
      - legacy_imported / None  (不识别 · 全跑)

    规则:
      - 字段以 stage_N_xxx_done 结尾 → 该 stage 已完成 · 从 stage_(N+1) 续
      - 字段是 stage_N_xxx (无 _done) → 该 stage 中断 · 重跑 stage_N
      - None / 不识别 / legacy_ → fallback 'stage_1' (全跑)
      - n>8 → 全跑完了 · 兜底 'stage_8'
    """
    if not current_stage:
        return "stage_1"
    m = re.match(r'stage_(\d+)', current_stage)
    if not m:
        return "stage_1"
    n = int(m.group(1))
    if current_stage.endswith('_done'):
        n += 1
    if n < 1:
        n = 1
    if n > 8:
        n = 8
    return f"stage_{n}"
