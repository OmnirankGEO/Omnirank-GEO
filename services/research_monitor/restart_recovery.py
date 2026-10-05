"""
Server restart 后的僵尸 round sweep 工具

启动时被 server.py 的 startup event 调用,检查 geo_research_round 表里
所有 status='running' 的行,如果已经心跳超时或开始超过 6 小时,标记为 failed_resumable。

老板拍板:默认 mark_failed_resumable,不 auto_resume(避免重启风暴 / 月度预算 / cron 重叠)。
admin 想恢复就在 GUI 点"续跑"按钮触发 API。

判定僵尸 round 的标准 (任一即标记):
- status IN ('running', 'pending') 且 last_heartbeat_at 距今超过 10 分钟
- status IN ('running', 'pending') 且 created_at 距今超过 6 小时

P14-v15 (review HIGH#6): 扩到 pending 状态 · 之前只扫 running 漏 BackgroundTask 未启动就死的 round
  (mark_round_resume_requested 后 worker 没起 / 续跑 endpoint 200 后 worker 崩 · round 永远卡 pending)
"""

import logging
from datetime import datetime
from typing import Dict, List

from db.connection import get_connection

logger = logging.getLogger("GEO-ResearchMonitor.Recovery")

HEARTBEAT_STALE_MINUTES = 10  # 心跳超 10 分钟视为死
ROUND_HARD_TIMEOUT_HOURS = 6  # 开始超 6 小时视为死(单轮硬超时 4h + 2h 安全余量)


def find_zombie_rounds() -> List[Dict]:
    """
    查所有 status='running' 但实际已死的 round。

    返回 [{'round_id', 'status', 'current_stage', 'started_at',
           'last_heartbeat_at', 'reason'}, ...]

    每行 reason 字段说明判死理由:
    - 'heartbeat_stale: 心跳超过 10 分钟未更新'
    - 'hard_timeout: round 总时长超过 6 小时'
    - 两个都触发优先标 hard_timeout(更严重)
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        # P14-v15 (review HIGH#6): 扩到 pending · 且 pending 没 started_at 用 created_at 算超时
        cur.execute(
            """
            SELECT round_id,
                   status,
                   current_stage,
                   started_at,
                   last_heartbeat_at,
                   created_at,
                   (NOW() - last_heartbeat_at) AS heartbeat_age,
                   (NOW() - COALESCE(started_at, created_at)) AS run_age
              FROM geo_research_round
             WHERE status IN ('running', 'pending')
               AND (
                    COALESCE(last_heartbeat_at, started_at, created_at) < NOW() - (%s || ' minutes')::INTERVAL
                    OR
                    COALESCE(started_at, created_at) < NOW() - (%s || ' hours')::INTERVAL
                   )
            """,
            (str(HEARTBEAT_STALE_MINUTES), str(ROUND_HARD_TIMEOUT_HOURS)),
        )
        rows = cur.fetchall()

        zombies: List[Dict] = []
        for row in rows:
            row_dict = dict(row) if not isinstance(row, dict) else dict(row)

            started_at = row_dict.get('started_at')
            last_hb = row_dict.get('last_heartbeat_at')
            now = datetime.now(started_at.tzinfo) if started_at else datetime.now()

            hard_timeout = False
            heartbeat_stale = False

            if started_at is not None:
                run_secs = (now - started_at).total_seconds()
                if run_secs > ROUND_HARD_TIMEOUT_HOURS * 3600:
                    hard_timeout = True

            if last_hb is not None:
                hb_secs = (now - last_hb).total_seconds()
                if hb_secs > HEARTBEAT_STALE_MINUTES * 60:
                    heartbeat_stale = True

            # hard_timeout 优先(更严重 · 即便心跳还在也得标死)
            if hard_timeout:
                reason = (
                    f'hard_timeout: round 总时长超过 {ROUND_HARD_TIMEOUT_HOURS} 小时'
                )
            elif heartbeat_stale:
                reason = (
                    f'heartbeat_stale: 心跳超过 {HEARTBEAT_STALE_MINUTES} 分钟未更新'
                )
            else:
                # SQL 已过滤 · 理论不会落到这里 · 兜底防御
                reason = 'unknown_zombie'

            zombies.append({
                'round_id': row_dict.get('round_id'),
                'status': row_dict.get('status'),
                'current_stage': row_dict.get('current_stage'),
                'started_at': started_at,
                'last_heartbeat_at': last_hb,
                'reason': reason,
            })

        return zombies
    finally:
        conn.close()


def mark_zombie_failed_resumable(round_id: str, reason: str, last_stage: str) -> bool:
    """
    把单个僵尸 round 标记为 failed_resumable。

    UPDATE 带 status IN ('running', 'pending') 防并发(其他人已改过则不动)。

    P14 post-review fix (老板复核): 之前 WHERE 只匹配 status='running' ·
    跟 find_zombie_rounds 已扩到 pending 不一致 · 导致 pending 僵尸被检测出来
    但 UPDATE 0 行 · 续跑 endpoint 200 后 worker 没接手卡 pending 的坑没真修上.

    summary_json 用 jsonb_build_object 直接 PG 端构建,避免 Python 序列化时区差异。

    返回 True/False(是否真改了一行)
    """
    detected_at = datetime.now().isoformat()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE geo_research_round
               SET status = 'failed_resumable',
                   finished_at = NOW(),
                   summary_json = jsonb_build_object(
                       'reason', %s,
                       'last_seen_stage', %s,
                       'detected_at', %s,
                       'note', 'server restart sweep 自动检测的僵尸 round'
                   )
             WHERE round_id = %s
               AND status IN ('running', 'pending')
            """,
            (reason, last_stage, detected_at, round_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def sweep_zombie_rounds() -> Dict:
    """
    主入口,server startup 调用。
    1. 调 find_zombie_rounds 拿列表
    2. 对每行调 mark_zombie_failed_resumable
    3. 返 {'detected': N, 'marked': M, 'rounds': [{round_id, reason, marked_ok}, ...]}

    异常处理:连不上 DB / 表不存在 → log warning + 返
                 {'detected': 0, 'marked': 0, 'error': str}。
    不要让 sweep 失败把整个 server 启动挂掉。
    """
    try:
        zombies = find_zombie_rounds()
    except Exception as e:
        logger.warning(
            f"[Recovery] sweep 查询僵尸 round 失败(跳过 · 不阻塞启动): "
            f"{type(e).__name__}: {e}"
        )
        return {'detected': 0, 'marked': 0, 'error': f'{type(e).__name__}: {e}'}

    detected = len(zombies)
    if detected == 0:
        logger.info("[Recovery] sweep 检测到 0 个僵尸 round")
        return {'detected': 0, 'marked': 0, 'rounds': []}

    logger.info(f"[Recovery] sweep 检测到 {detected} 个僵尸 round")

    marked = 0
    results: List[Dict] = []
    for z in zombies:
        round_id = z.get('round_id')
        reason = z.get('reason', 'unknown')
        last_stage = z.get('current_stage') or 'unknown'

        try:
            ok = mark_zombie_failed_resumable(round_id, reason, last_stage)
        except Exception as e:
            logger.warning(
                f"[Recovery] {round_id} 标记 failed_resumable 异常(跳过该行): "
                f"{type(e).__name__}: {e}"
            )
            ok = False

        if ok:
            marked += 1
            logger.warning(
                f"[Recovery] {round_id} 标记 failed_resumable: {reason} "
                f"(last_stage={last_stage})"
            )
        else:
            logger.info(
                f"[Recovery] {round_id} 未标记(已被并发改过 status 或 UPDATE 0 行)"
            )

        results.append({
            'round_id': round_id,
            'reason': reason,
            'last_stage': last_stage,
            'marked_ok': ok,
        })

    return {
        'detected': detected,
        'marked': marked,
        'rounds': results,
    }
