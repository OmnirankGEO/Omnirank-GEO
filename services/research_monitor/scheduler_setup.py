"""
GEO 调研监测 APScheduler 注册 (A.5.6)

复用主系统 api.scheduler 的 BackgroundScheduler 单例,
不新建 scheduler 实例(老板规则:跟主系统其他 job 共享调度池)。

cron: 每月 1 号 + 16 号 凌晨 02:00 北京时间
触发: CronTrigger(day='1,16', hour=2, minute=0, timezone=BEIJING_TZ)

missed cron 补跑:
    默认关闭。只有显式设置 RESEARCH_MONITOR_ENABLE_MISSED_RECOVERY=true 时,
    才会在服务器重启后检查最后一次成功跑批时间并排程 30 秒后补跑。

工程铁律:
- BackgroundScheduler worker 线程是同步的,trigger_research_round_sync 必须同步签名,
  内部用 asyncio.run 包 run_round_with_auto_resume(2026-07-16 起含 4h 超时自动续跑)。
- 注册失败不阻塞 server 启动(调用方 try/except)。
- SQL 全部参数化。
"""

import asyncio
import calendar
import logging
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any

import pytz
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from db.connection import get_connection
from services.flywheel_heartbeat import flywheel_job  # [A2] 采集轮进统一心跳账本

logger = logging.getLogger("GEO-ResearchMonitor.Scheduler")

BEIJING_TZ = pytz.timezone("Asia/Shanghai")

JOB_ID_BIMONTHLY_ROUND = "research_monitor_bimonthly_round"
JOB_ID_MISSED_RECOVERY = "research_monitor_missed_recovery"

# missed cron 判定阈值: 上次完成距最近一次预定 cron 时刻的容忍区间
# 当前简化为 "上次完成 < 最近一次预定 cron 时刻" 即视为漏跑。
# 24 小时阈值留给 should_run_missed_cron 的"宽限窗口"使用。
MISSED_CRON_GRACE_HOURS = 24

# 补跑推迟秒数(避免和其它 startup job 抢资源)
MISSED_RECOVERY_DELAY_SECONDS = 30


def _env_enabled(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


# P14-v10 默认 fallback (DB 没配/读失败时用)
_CRON_DEFAULT_ENABLED = False
_CRON_DEFAULT_DAYS = '1,16'
_CRON_DEFAULT_HOUR = 2

_AUTO_TRIGGER_TYPES = ('cron', 'missed_cron_recovery')
_AUTO_FAILURE_STATUSES = ('failed', 'failed_resumable', 'cancelled')


class ResearchCronGateUnavailable(RuntimeError):
    """The DB-backed automatic-round gate cannot be proven open."""


def _parse_cron_days(days: Optional[str]) -> List[int]:
    """解析 cron_days 配置。非法配置回落到默认 1,16。"""
    raw = str(days or _CRON_DEFAULT_DAYS)
    parsed: List[int] = []
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            day = int(part)
        except (TypeError, ValueError):
            continue
        if 1 <= day <= 31 and day not in parsed:
            parsed.append(day)
    if not parsed:
        return [1, 16]
    return sorted(parsed)


def _parse_cron_hour(hour: Optional[int]) -> int:
    try:
        value = int(hour)
    except (TypeError, ValueError):
        return _CRON_DEFAULT_HOUR
    return value if 0 <= value <= 23 else _CRON_DEFAULT_HOUR


def _load_cron_config_from_db() -> Dict:
    """从 geo_research_config 表读 cron 配置 · 返 {enabled, days, hour}
    schedule 字段可回落默认；自动轮开关必须从 DB 读到合法 bool 才能开启。
    DB/配置异常时 fail-closed，不影响 cron 容器里的其他任务。
    """
    enabled = _CRON_DEFAULT_ENABLED
    available = False
    days = _CRON_DEFAULT_DAYS
    hour = _CRON_DEFAULT_HOUR
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT key, value_json FROM geo_research_config "
                "WHERE key IN ('cron_enabled', 'cron_days', 'cron_hour')"
            )
            for r in cur.fetchall() or []:
                k = r['key'] if isinstance(r, dict) else r[0]
                v = r['value_json'] if isinstance(r, dict) else r[1]
                if k == 'cron_enabled' and isinstance(v, bool):
                    enabled = v
                    available = True
                elif k == 'cron_days' and isinstance(v, str) and v.strip():
                    days = ','.join(str(day) for day in _parse_cron_days(v.strip()))
                elif k == 'cron_hour':
                    hour = _parse_cron_hour(v)
        finally:
            conn.close()
    except Exception as e:
        logger.error(
            f"[ResearchMonitor] 读 cron 配置失败 · 自动轮 fail-closed: "
            f"{type(e).__name__}: {e}"
        )
    if not available:
        enabled = False
        logger.error("[ResearchMonitor] cron_enabled 缺失或类型非法 · 自动轮 fail-closed")
    return {'enabled': enabled, 'days': days, 'hour': hour, 'available': available}


def _row_value(row: Any, key: str, index: int = 0) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return row[index] if row else None


def _remove_auto_round_jobs_best_effort() -> None:
    """Remove only research auto-round jobs. Other cron jobs are untouched."""
    try:
        from api.scheduler import get_scheduler
        scheduler = get_scheduler()
    except Exception as e:
        logger.warning(f"[ResearchMonitor] 自动轮熔断后获取 scheduler 失败: {e}")
        return
    for job_id in (JOB_ID_BIMONTHLY_ROUND, JOB_ID_MISSED_RECOVERY):
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass


def _trip_auto_round_circuit(
    *,
    round_id: str,
    status: str,
    reason: str,
    expected_gate_updated_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Atomically pause future auto rounds and enqueue an admin alert.

    The business round terminal is written by round_state. This transaction owns
    only the research cron gate and its durable notification; paid diagnosis,
    customer monitoring and other scheduler jobs are outside this function.
    """
    from services.notification_events import NotificationEventType
    from services.notification_outbox import enqueue_admin_notification_events

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT value_json, updated_at
              FROM geo_research_config
             WHERE key = 'cron_enabled'
             FOR UPDATE
            """
        )
        row = cur.fetchone()
        current = _row_value(row, 'value_json')
        gate_updated_at = _row_value(row, 'updated_at', 1)
        if not isinstance(current, bool):
            raise ResearchCronGateUnavailable(
                "cron_enabled missing or not boolean while opening circuit"
            )
        if (
            expected_gate_updated_at is not None
            and gate_updated_at != expected_gate_updated_at
        ):
            conn.rollback()
            logger.warning(
                "[ResearchMonitor] 自动轮熔断 CAS 失效：管理员已更新开关，"
                f"保留新决定 round_id={round_id}"
            )
            return {
                'paused': False,
                'changed': False,
                'stale_gate': True,
                'round_id': round_id,
                'status': status,
            }

        changed = False
        if current:
            cur.execute(
                """
                UPDATE geo_research_config
                   SET value_json = 'false'::jsonb,
                       updated_by = 'auto_round_circuit',
                       updated_at = NOW()
                 WHERE key = 'cron_enabled'
                   AND value_json = 'true'::jsonb
                RETURNING key
                """
            )
            changed = cur.fetchone() is not None

        enqueue_admin_notification_events(
            cur,
            event_type=NotificationEventType.RESEARCH_MANUAL_REQUIRED,
            business_id=f"auto-circuit:{round_id}",
            terminal_state=f"paused:{status}",
            facts={
                'business_no': str(round_id),
                'status': '自动调研已暂停',
                'occurred_at': datetime.now(BEIJING_TZ).isoformat(timespec='seconds'),
                'summary': (
                    f"自动轮终态为 {status}，系统已暂停下一次自动触发。"
                    f"请核验后由管理员重新开启。原因：{reason[:500]}"
                ),
            },
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

    _remove_auto_round_jobs_best_effort()
    logger.error(
        f"[ResearchMonitor] AUTO_ROUND_CIRCUIT_OPEN round_id={round_id} "
        f"status={status} changed={changed} reason={reason}"
    )
    return {'paused': True, 'changed': changed, 'round_id': round_id, 'status': status}


def _preflight_auto_round_allowed() -> Optional[datetime]:
    """DB-authoritative gate checked immediately before an automatic run.

    ``updated_at`` is the explicit re-arm boundary: a historical failed round
    before the latest admin enable action cannot trip a newly re-opened schedule.
    """
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT value_json, updated_at
                  FROM geo_research_config
                 WHERE key = 'cron_enabled'
                """
            )
            row = cur.fetchone()
            enabled = _row_value(row, 'value_json')
            rearmed_at = _row_value(row, 'updated_at', 1)
            if not isinstance(enabled, bool) or rearmed_at is None:
                raise ResearchCronGateUnavailable(
                    "cron_enabled gate is missing or invalid"
                )
            if not enabled:
                return None

            cur.execute(
                """
                SELECT round_id, status,
                       COALESCE(error_message, summary_json->>'reason', '') AS reason
                  FROM geo_research_round
                 WHERE triggered_by = ANY(%s)
                   AND created_at >= %s
                 ORDER BY created_at DESC, id DESC
                 LIMIT 1
                """,
                (list(_AUTO_TRIGGER_TYPES), rearmed_at),
            )
            latest = cur.fetchone()
        finally:
            conn.close()
    except ResearchCronGateUnavailable:
        raise
    except Exception as e:
        raise ResearchCronGateUnavailable(
            f"automatic-round preflight DB read failed: {type(e).__name__}: {e}"
        ) from e

    if latest:
        status = str(_row_value(latest, 'status', 1) or '')
        if status in _AUTO_FAILURE_STATUSES:
            _trip_auto_round_circuit(
                round_id=str(_row_value(latest, 'round_id')),
                status=status,
                reason=str(_row_value(latest, 'reason', 2) or 'previous automatic round failed'),
                expected_gate_updated_at=rearmed_at,
            )
            return None
    return rearmed_at


def reschedule_cron_from_db() -> Dict:
    """配置改完后调本函数 · 重新读 DB + 重注册 cron job (admin PUT /config 后用)
    返回 {registered, days, hour, next_run_at}
    """
    cfg = _load_cron_config_from_db()
    try:
        from api.scheduler import get_scheduler
        scheduler = get_scheduler()
    except Exception as e:
        logger.warning(f"[reschedule_cron] scheduler 不可用: {type(e).__name__}: {e}")
        return {'registered': False, 'reason': 'scheduler_unavailable', **cfg}

    if not cfg['enabled']:
        try:
            scheduler.remove_job(JOB_ID_BIMONTHLY_ROUND)
            logger.info("[reschedule_cron] cron 已禁用 · 移除 job")
        except Exception:
            pass
        return {'registered': False, 'next_run_at': None, **cfg}

    scheduler.add_job(
        trigger_research_round_sync,
        trigger=CronTrigger(day=cfg['days'], hour=cfg['hour'], minute=0, timezone=BEIJING_TZ),
        id=JOB_ID_BIMONTHLY_ROUND,
        name=f"GEO 调研监测自动跑批 ({cfg['days']} 号 {cfg['hour']:02d}:00 CST)",
        replace_existing=True,
    )
    logger.info(
        f"[reschedule_cron] cron 已更新: days={cfg['days']} hour={cfg['hour']}"
    )
    # 读 next run time
    next_run = None
    try:
        job = scheduler.get_job(JOB_ID_BIMONTHLY_ROUND)
        if job and job.next_run_time:
            next_run = job.next_run_time.isoformat()
    except Exception:
        pass
    return {'registered': True, 'next_run_at': next_run, **cfg}


def get_next_cron_run_time() -> Optional[str]:
    """读 scheduler 下次触发时间 (ISO 字符串 · 含时区) · 用于 UI 显示"""
    try:
        from api.scheduler import get_scheduler
        scheduler = get_scheduler()
        job = scheduler.get_job(JOB_ID_BIMONTHLY_ROUND)
        if job and job.next_run_time:
            return job.next_run_time.isoformat()
    except Exception as e:
        logger.warning(f"[get_next_cron_run_time] {type(e).__name__}: {e}")
    return None


# ==================== 跑批入口 ====================

@flywheel_job("research_auto_round")
def trigger_research_round_sync(triggered_by: str = 'cron'):
    """
    BackgroundScheduler worker 线程入口(必须同步签名)。
    内部用 asyncio.run 包 run_round_with_auto_resume。

    流程:
    1. 拿 active industries + prompts (get_active_plan)
    2. create_round_with_snapshot('cron', industries, prompts_by_industry)
    3. asyncio.run(run_round_with_auto_resume(round_id, snapshot))
       (4h 硬超时自动续跑的重入都在这一个 asyncio.run 内完成 · 2026-07-16)
    4. 异常不再静默吞:@flywheel_job 统一落心跳表 + 拉 AIOps 告警(A2 · 2026-07-29),
       熔断落库仍走原路径,scheduler 依然不会被单个 job 拖挂。

    返回值:给心跳层判成败用的结构化结果(APScheduler 本身不消费返回)。
    """
    round_id: Optional[str] = None
    gate_token: Optional[datetime] = None
    try:
        try:
            gate_token = _preflight_auto_round_allowed()
            if gate_token is None:
                logger.warning("[ResearchMonitor] 自动轮 DB gate 已关闭，本次不触发")
                # 闸门关着不是"任务失败"(是治理状态),不拉失败告警;
                # 由飞轮看门狗的 gate 规则单独报"自动调研已暂停"。
                return {"processed": 0, "skipped": "gate_closed"}
        except ResearchCronGateUnavailable as e:
            logger.error(f"[ResearchMonitor] 自动轮 gate 无法确认，fail-closed: {e}")
            return {"status": "error", "error": f"gate_unavailable: {e}"}

        plan = get_active_plan()
        industries = plan.get('industries') or []
        prompts_by_industry = plan.get('prompts_by_industry') or {}

        if not industries:
            logger.warning(
                "[ResearchMonitor] 触发跑批时未发现 active 行业, 暂停后续自动轮"
            )
            _trip_auto_round_circuit(
                round_id=f"preflight-{datetime.now(BEIJING_TZ):%Y%m%d%H%M%S}",
                status='failed',
                reason='no_active_industries',
                expected_gate_updated_at=gate_token,
            )
            return {"status": "error", "error": "no_active_industries"}
        # [B1 · 2026-07-29] 采集选题:让 v4-flash 在"被引产出率 / 客户行业分布 / 覆盖缺口"
        #   三类**代码算好的**事实上做取舍,替掉"全部 active 行业照单全收"的静态题库。
        #   advisory:判断点默认关 / 超预算 / 模型全挂 / 输出越界 → 原样返回全量 plan,
        #   行为与改前完全一致。放在 active 校验**之后**、prompt 校验之前,
        #   这样"没有 active 行业"仍然照旧熔断,不会被选题层遮住。
        try:
            from services.flywheel_topic_selection import select_round_topics
            selected_plan = select_round_topics(
                {"industries": industries, "prompts_by_industry": prompts_by_industry}
            )
            industries = selected_plan.get("industries") or industries
            prompts_by_industry = selected_plan.get("prompts_by_industry") or prompts_by_industry
        except Exception as topic_error:
            logger.warning(
                f"[ResearchMonitor] 采集选题异常,按全量 plan 继续: "
                f"{type(topic_error).__name__}: {topic_error}"
            )

        prompt_total = sum(len(v or []) for v in prompts_by_industry.values())
        if prompt_total <= 0:
            logger.warning(
                "[ResearchMonitor] 触发跑批时未发现 active prompts, 暂停后续自动轮"
            )
            _trip_auto_round_circuit(
                round_id=f"preflight-{datetime.now(BEIJING_TZ):%Y%m%d%H%M%S}",
                status='failed',
                reason='no_active_prompts',
                expected_gate_updated_at=gate_token,
            )
            return {"status": "error", "error": "no_active_prompts"}

        # 延迟导入避免循环依赖 + 减小 import 开销
        from services.research_monitor.round_state import (
            create_round_with_snapshot,
            RoundAlreadyRunningError,
        )
        from services.research_monitor.round_runner import run_round_with_auto_resume

        try:
            round_id = create_round_with_snapshot(
                triggered_by=triggered_by,
                industries=industries,
                prompts_by_industry=prompts_by_industry,
                enforce_single_active=True,
            )
        except RoundAlreadyRunningError as e:
            logger.warning(f"[ResearchMonitor] 已有跑批运行, cron 本轮跳过: {e}")
            return {"processed": 0, "skipped": "already_running"}
        logger.info(f"[ResearchMonitor] cron 触发新一轮跑批 round_id={round_id}")

        # 使用内存中的完整 snapshot, 避免 DB 展示字段截断影响执行计划。
        snapshot = {
            'industries': industries,
            'prompts_by_industry': {str(k): v for k, v in prompts_by_industry.items()},
        }

        # BackgroundScheduler worker 线程没有 event loop, 必须用 asyncio.run 包。
        # [2026-07-16 改动二] 换 run_round_with_auto_resume: 4h 硬超时自动续跑的重入
        # 全部发生在这一个 asyncio.run 存活期内 = cron leader 既有执行路径之内
        # ("跑批结束"日志之前), 不新造触发面, 蓝绿双 cron 的 leader gate 零感知。
        status = asyncio.run(run_round_with_auto_resume(round_id, snapshot))
        logger.info(f"[ResearchMonitor] round_id={round_id} 跑批结束 status={status}")
        if status in _AUTO_FAILURE_STATUSES:
            from services.research_monitor.round_state import get_round_status
            current = get_round_status(round_id) or {}
            summary = current.get('summary_json') if isinstance(current, dict) else {}
            if not isinstance(summary, dict):
                summary = {}
            _trip_auto_round_circuit(
                round_id=round_id,
                status=str(current.get('status') or status),
                reason=str(
                    current.get('error_message')
                    or summary.get('reason')
                    or 'automatic round ended unsuccessfully'
                ),
                expected_gate_updated_at=gate_token,
            )
            return {"status": "error", "error": f"round_{status}", "round_id": round_id}
        return {"processed": 1, "round_id": round_id, "round_status": status}
    except Exception as e:
        # 不让 scheduler 整体挂，但必须暂停下一次调研自动轮并写耐久告警。
        logger.exception(f"[ResearchMonitor] cron 跑批异常: {type(e).__name__}: {e}")
        try:
            _trip_auto_round_circuit(
                round_id=round_id or f"exception-{datetime.now(BEIJING_TZ):%Y%m%d%H%M%S}",
                status='failed',
                reason=f"{type(e).__name__}: {e}",
                expected_gate_updated_at=gate_token,
            )
        except Exception as trip_error:
            logger.exception(
                f"[ResearchMonitor] 自动轮异常后熔断落库失败: "
                f"{type(trip_error).__name__}: {trip_error}"
            )
        return {"status": "error", "error": f"{type(e).__name__}: {e}", "round_id": round_id}


# ==================== DB 查询 ====================

def get_active_plan() -> Dict:
    """
    查 active 行业 + 每个行业的 active prompts。

    返:
      {
        'industries': [{'id','name','slug'}, ...],
        'prompts_by_industry': {industry_id: [{'id','prompt_text', ...}, ...]}
      }

    industries 为空时返 {'industries':[], 'prompts_by_industry':{}}
    (不抛异常, 让上游决定怎么处理)
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, name, slug
              FROM geo_research_industries
             WHERE active = TRUE
             ORDER BY sort_order, id
            """
        )
        rows = cur.fetchall() or []
        industries = [
            {'id': r['id'], 'name': r['name'], 'slug': r['slug']}
            for r in rows
        ]

        if not industries:
            return {'industries': [], 'prompts_by_industry': {}}

        cur.execute(
            """
            SELECT id, industry_id, prompt_text, sort_order, is_sensitive, source
              FROM geo_research_prompts
             WHERE active = TRUE
             ORDER BY industry_id, sort_order, id
            """
        )
        prompt_rows = cur.fetchall() or []

        prompts_by_industry: Dict[int, List[Dict]] = {ind['id']: [] for ind in industries}
        for pr in prompt_rows:
            ind_id = pr['industry_id']
            if ind_id not in prompts_by_industry:
                # 行业可能被软删, prompt 还存在 → 跳过
                continue
            prompts_by_industry[ind_id].append({
                'id': pr['id'],
                'prompt_text': pr['prompt_text'],
                'sort_order': pr.get('sort_order') if isinstance(pr, dict) else pr['sort_order'],
                'is_sensitive': pr.get('is_sensitive') if isinstance(pr, dict) else pr['is_sensitive'],
                'source': pr.get('source') if isinstance(pr, dict) else pr['source'],
            })

        return {
            'industries': industries,
            'prompts_by_industry': prompts_by_industry,
        }
    finally:
        conn.close()


def get_last_completed_round_at() -> Optional[datetime]:
    """
    最后一次 status IN ('completed','partial_success') 的 finished_at(TIMESTAMPTZ)。

    返 datetime(带 tz) 或 None。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT MAX(finished_at) AS max
              FROM geo_research_round
             WHERE status IN ('completed', 'partial_success')
            """
        )
        row = cur.fetchone()
        if not row:
            return None
        # RealDictCursor → dict; 容错普通 cursor
        if isinstance(row, dict):
            return row.get('max')
        return row[0]
    finally:
        conn.close()


# ==================== missed cron 判定 ====================

def _last_scheduled_cron_time(
    now: Optional[datetime] = None,
    days: Optional[str] = None,
    hour: Optional[int] = None,
) -> datetime:
    """
    算"过去最近一次本应触发的 cron 时间"。

    cron days/hour 跟随 geo_research_config, DB 读失败时回落 1,16 / 02:00。

    返一个 BEIJING_TZ 的 aware datetime, 永远是过去时刻(<= now)。
    """
    if now is None:
        now = datetime.now(BEIJING_TZ)
    elif now.tzinfo is None:
        now = BEIJING_TZ.localize(now)
    else:
        now = now.astimezone(BEIJING_TZ)

    cron_days = _parse_cron_days(days)
    cron_hour = _parse_cron_hour(hour)
    candidates: List[datetime] = []

    def add_month_candidates(year: int, month: int) -> None:
        max_day = calendar.monthrange(year, month)[1]
        for day in cron_days:
            if day <= max_day:
                candidates.append(
                    BEIJING_TZ.localize(datetime(year, month, day, cron_hour, 0, 0))
                )

    year, month = now.year, now.month
    for _ in range(14):
        add_month_candidates(year, month)
        if month == 1:
            year, month = year - 1, 12
        else:
            month -= 1

    past = [c for c in candidates if c <= now]
    if not past:
        # 理论上不会发生(上个月候选永远是过去), 兜底返 1970
        return BEIJING_TZ.localize(datetime(1970, 1, 1, cron_hour, 0, 0))
    return max(past)


def should_run_missed_cron() -> bool:
    """
    判定是否需要补跑 missed cron。

    规则:
    - RESEARCH_MONITOR_ENABLE_MISSED_RECOVERY 未显式开启 → 不补跑
    - 已开启时,从未跑过 (get_last_completed_round_at 返 None) → 补跑
    - 上次完成时间早于"最近一次预定 cron 时刻" → 补跑
      (说明那次 cron 没有跑, 服务器在那个时刻没活着)

    Note: 阈值 MISSED_CRON_GRACE_HOURS 仅作为日志/语义标识,
    实际判定看 last_completed 是否覆盖最近一次预定 cron。
    """
    if not _env_enabled('RESEARCH_MONITOR_ENABLE_MISSED_RECOVERY', default=False):
        logger.info("[ResearchMonitor] missed cron 补跑未显式启用, 跳过启动补跑")
        return False

    cfg = _load_cron_config_from_db()
    if not cfg.get('enabled', True):
        logger.info("[ResearchMonitor] cron 已禁用, 跳过 missed cron 补跑")
        return False

    last_completed = get_last_completed_round_at()
    if last_completed is None:
        logger.info("[ResearchMonitor] 历史无成功跑批记录 → 触发补跑")
        return True

    # 把 last_completed 统一到 BEIJING_TZ aware
    if last_completed.tzinfo is None:
        last_completed = BEIJING_TZ.localize(last_completed)
    else:
        last_completed = last_completed.astimezone(BEIJING_TZ)

    last_scheduled = _last_scheduled_cron_time(
        days=cfg.get('days'),
        hour=cfg.get('hour'),
    )

    if last_completed < last_scheduled:
        gap = (datetime.now(BEIJING_TZ) - last_scheduled).total_seconds() / 3600
        logger.info(
            f"[ResearchMonitor] 上次成功={last_completed.isoformat()} "
            f"早于最近预定 cron={last_scheduled.isoformat()} "
            f"(距今 {gap:.1f}h) → 触发补跑"
        )
        return True

    return False


JOB_ID_MISSED_ROUND_CATCHUP = "research_monitor_missed_round_catchup"

# 补跑宽限:预定时刻过去多久之后才认为"这次真的漏了"。
# 留足够时间给正常 cron 自己跑完(一轮采集本身可能跑数小时),避免抢跑。
MISSED_ROUND_CATCHUP_GRACE_HOURS = 12


@flywheel_job("research_missed_round_catchup")
def run_missed_round_catchup() -> Dict:
    """[A1 · 2026-07-29] 漏跑的采集轮周期性自愈(每 6 小时查一次)。

    修前的处境:补跑逻辑(`should_run_missed_cron`)只在 **server 启动那一刻** 跑一次,
    而且要显式设 `RESEARCH_MONITOR_ENABLE_MISSED_RECOVERY=true` 才生效(生产没设)。
    于是只要 cron leader 在每月 1 号 02:00 那一刻不在(重启/换主/熔断刚被人工恢复),
    这一轮就整整跳过半个月,而且没有任何痕迹 —— 采集是飞轮上游,断一轮全链路跟着空转。

    本 job 把它变成周期性自愈,判定全部基于 DB(不依赖任何内存状态,因此天然支持重启续跑):
      ① 闸门必须开(cron_enabled=true)——熔断/人工关停时绝不擅自补跑;
      ② 当前没有活跃 round(create_round_with_snapshot 的 enforce_single_active 是最终防线);
      ③ 最近一次成功跑批早于"最近一次预定 cron 时刻",且该时刻已过去 ≥ 宽限期。
    满足三条才调**既有**的 trigger_research_round_sync(不新造第二条起轮路径)。
    """
    cfg = _load_cron_config_from_db()
    if not cfg.get('enabled'):
        return {"processed": 0, "skipped": "gate_closed"}

    last_scheduled = _last_scheduled_cron_time(days=cfg.get('days'), hour=cfg.get('hour'))
    age_hours = (datetime.now(BEIJING_TZ) - last_scheduled).total_seconds() / 3600
    if age_hours < MISSED_ROUND_CATCHUP_GRACE_HOURS:
        return {"processed": 0, "skipped": "within_grace", "age_hours": round(age_hours, 1)}

    last_completed = get_last_completed_round_at()
    if last_completed is not None:
        if last_completed.tzinfo is None:
            last_completed = BEIJING_TZ.localize(last_completed)
        else:
            last_completed = last_completed.astimezone(BEIJING_TZ)
        if last_completed >= last_scheduled:
            return {"processed": 0, "skipped": "already_ran"}

    if _has_active_round():
        return {"processed": 0, "skipped": "round_running"}

    logger.warning(
        "[ResearchMonitor] 检测到漏跑:最近预定 %s(距今 %.1fh)之后没有成功跑批 → 自动补跑",
        last_scheduled.isoformat(), age_hours,
    )
    result = trigger_research_round_sync(triggered_by='missed_cron_recovery')
    return {"processed": 1, "catchup_triggered": True, "inner": result}


def _has_active_round() -> bool:
    """DB 权威:是否有在跑的 round。查询失败 → 保守当作"有"(不补跑,fail-closed)。"""
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                # 状态词表与 round_state / restart_recovery 一致:活跃态只有 pending / running。
                "SELECT COUNT(*) AS c FROM geo_research_round "
                "WHERE status IN ('pending', 'running')"
            )
            row = cur.fetchone()
            return int(_row_value(row, 'c') or 0) > 0
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"[ResearchMonitor] 活跃 round 查询失败 · 补跑 fail-closed: {e}")
        return True


# ==================== 主入口 ====================

def schedule_research_monitor_jobs() -> Dict:
    """
    Server startup 在 setup_schedule(...) 之后调用。

    1. 拿主 scheduler: from api.scheduler import get_scheduler
    2. RESEARCH_MONITOR_CRON_ENABLED=true 时注册半月 cron
    3. RESEARCH_MONITOR_ENABLE_MISSED_RECOVERY=true 且 should_run_missed_cron() 为 True 时,
       加一个 date trigger 推迟 30s 跑

    返 {
      'registered': True,
      'missed_cron_recovery_scheduled': bool,
      'main_job_id': str,
    }
    """
    if os.getenv("ROLE", "").lower() == "backup":
        logger.warning("[ResearchMonitor] ROLE=backup · 跳过调研监测 cron 注册")
        return {
            'registered': False,
            'missed_cron_recovery_scheduled': False,
            'main_job_id': JOB_ID_BIMONTHLY_ROUND,
            'skipped_reason': 'backup_role',
        }

    from api.scheduler import get_scheduler
    scheduler = get_scheduler()

    # [A1 · 2026-07-29] 漏跑自愈巡查(每 6h)**先于闸门判定注册**:
    #   job 体第一件事就是查 cron_enabled,闸关时立刻 no-op。放在闸门之前注册,
    #   是为了让"管理员把闸重新打开"这件事不再需要顺带重注册一个 job ——
    #   熔断→人工恢复这条路径上少一个必须有人记得做的动作,就少一次半个月的静默。
    catchup_registered = False
    try:
        scheduler.add_job(
            run_missed_round_catchup,
            trigger=IntervalTrigger(hours=6),
            id=JOB_ID_MISSED_ROUND_CATCHUP,
            name='GEO 调研采集轮漏跑自愈巡查(每 6h)',
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        catchup_registered = True
        logger.info("[ResearchMonitor] 采集轮漏跑自愈巡查已注册(每 6h)")
    except Exception as e:
        logger.warning(f"[ResearchMonitor] 漏跑自愈巡查注册失败: {type(e).__name__}: {e}")

    # P14-v10 (2026-05-28): cron 配置从 geo_research_config 表读 · admin 可在系统配置页改
    #   旧 env 开关 RESEARCH_MONITOR_CRON_ENABLED 仅作 emergency override (env=false 强制关掉)
    cfg = _load_cron_config_from_db()
    if not cfg['enabled']:
        logger.info("[ResearchMonitor] cron 已禁用 (DB cron_enabled=false), 不注册跑批 job")
        return {
            'registered': False,
            'missed_cron_recovery_scheduled': False,
            'missed_round_catchup_registered': catchup_registered,
            'main_job_id': JOB_ID_BIMONTHLY_ROUND,
            'config': cfg,
        }
    # env 兜底: 若 env 显式设 false · 即使 DB true 也不注册 (运维急停)
    if os.getenv('RESEARCH_MONITOR_CRON_ENABLED', '').strip().lower() in {'false', '0', 'no', 'off'}:
        logger.warning("[ResearchMonitor] env RESEARCH_MONITOR_CRON_ENABLED=false 强制关掉 cron")
        return {
            'registered': False,
            'missed_cron_recovery_scheduled': False,
            'main_job_id': JOB_ID_BIMONTHLY_ROUND,
            'config': cfg,
            'reason': 'env_override_false',
        }

    scheduler.add_job(
        trigger_research_round_sync,
        trigger=CronTrigger(day=cfg['days'], hour=cfg['hour'], minute=0, timezone=BEIJING_TZ),
        id=JOB_ID_BIMONTHLY_ROUND,
        name=f"GEO 调研监测自动跑批 ({cfg['days']} 号 {cfg['hour']:02d}:00 CST)",
        replace_existing=True,
    )
    logger.info(
        f"[ResearchMonitor] cron 已注册: id={JOB_ID_BIMONTHLY_ROUND}, "
        f"trigger=每月 {cfg['days']} 号 {cfg['hour']:02d}:00 CST"
    )

    missed_scheduled = False
    try:
        if should_run_missed_cron():
            run_at = datetime.now(BEIJING_TZ) + timedelta(seconds=MISSED_RECOVERY_DELAY_SECONDS)
            scheduler.add_job(
                trigger_research_round_sync,
                trigger='date',
                run_date=run_at,
                id=JOB_ID_MISSED_RECOVERY,
                name='GEO 调研监测 missed cron 补跑',
                kwargs={'triggered_by': 'missed_cron_recovery'},
                replace_existing=True,
            )
            missed_scheduled = True
            logger.info(
                f"[ResearchMonitor] missed cron 补跑已排程, run_at={run_at.isoformat()}"
            )
    except Exception as e:
        # 补跑判定异常不影响主 cron 注册成功
        logger.warning(f"[ResearchMonitor] missed cron 判定异常被吞: {type(e).__name__}: {e}")

    return {
        'registered': True,
        'missed_cron_recovery_scheduled': missed_scheduled,
        'missed_round_catchup_registered': catchup_registered,
        'main_job_id': JOB_ID_BIMONTHLY_ROUND,
    }
