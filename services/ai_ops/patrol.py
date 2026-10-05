"""
AI Ops 主动巡逻引擎(保安)· 包B · 2026-07-03

每 5 分钟(scheduler job `ai_ops_patrol` · flag `ai_ops.patrol.enabled` 默认关)
评估一遍库内已有真实信号,异常拉响告警、恢复自动消警:

  规则                 | 信号源                          | 严重度
  ---------------------|--------------------------------|------------------------------
  runner_offline       | ai_ops_worker_heartbeats       | warn;有任务在排队时 critical
  task_failed_recent   | 24h 内 failed 任务数            | ≥1 warn · ≥3 critical
  task_stuck_running   | running 且 started_at > 2h     | 每任务一条 warn
  approval_backlog     | pending 审批 > 24h / 72h       | warn / critical
  queue_pileup         | queued ≥ 10 / ≥ 25             | warn / critical
  report_missing       | 总开关开 · 过点没日报 / 日报 failed | warn
  kill_switch_on       | 急停开着(可见性提醒,防遗忘)      | info

设计红线:
  - 巡逻只读业务信号 + 写 ai_ops_alerts/ai_ops_patrol_runs 自己的表,不碰业务表。
  - Kill Switch 不挡巡逻本身(出事时保安最不能下班),只挡自动立案。
  - 自动立案(仅 task_failed_recent 达 critical):kill 关 + 总开关开 双闸,
    task_key=alert:{id} 幂等,一条 firing 告警只立一案;立的是 L0 只读诊断。
  - 每条规则独立 try/except:单规则失败不拖垮整轮(同日报 report_metrics 教训)。
  - 时间比较全部在 DB 侧(get_patrol_signals · EXTRACT EPOCH),避免时区错位。
"""

import logging
import time
from typing import Optional

from db import ai_ops_db as aiops_db

logger = logging.getLogger("AiOps-Patrol")

# 阈值(常量集中,调整不用翻逻辑)
RUNNER_STALE_SECONDS = 300          # 心跳超 5 分钟算离线(Runner 60s 一跳,给足余量)
FAILED_CRITICAL_AT = 3              # 24h 失败任务数达此值升 critical + 自动立案
QUEUE_WARN_AT = 10
QUEUE_CRITICAL_AT = 25
REPORT_ALERT_AFTER_HOUR = 9         # 本地时间过 9 点还没今天日报才告警(08:10 生成)

# 自动立案白名单:只有这些规则的 critical 告警会自动交 Codex 只读诊断。
# runner_offline 故意不在内——Runner 都离线了,立案也只是排队,且"诊断自己为何离线"无意义。
AUTO_TASK_RULES = frozenset({'task_failed_recent'})


def _finding(fingerprint: str, severity: str, title: str, detail: str = '',
             payload: Optional[dict] = None) -> dict:
    return {"fingerprint": fingerprint, "severity": severity,
            "title": title, "detail": detail, "payload": payload or {}}


# ==========================================
# 规则(每条返回 list[finding];空 list = 该规则一切正常)
# ==========================================

def _rule_runner_offline(signals: dict) -> list[dict]:
    # on_error='raise':查询失败要抛给 per-rule try(跳过且不消警),
    # 不能和"从未接入"混为 None → 否则会把真实离线告警误消(复审 P3-1)
    hb = aiops_db.get_runner_status(stale_seconds=RUNNER_STALE_SECONDS, on_error='raise')
    if hb is None:
        return []          # 从未接入 = 初始化状态,面板已有"未接入"展示,不算异常
    if hb.get('online'):
        return []
    age_min = int(hb.get('age_seconds', 0) // 60)
    queued = int(signals.get('queued_count') or 0)
    sev = 'critical' if queued > 0 else 'warn'
    return [_finding(
        str(hb.get('worker_id') or ''), sev,
        f"本机 Codex Runner 离线 {age_min} 分钟",
        (f"最后心跳 {age_min} 分钟前(worker={hb.get('worker_id')})。"
         + (f"当前有 {queued} 条任务在排队等它。" if queued else "当前队列为空,影响有限。")
         + "任务不会丢失,Runner 恢复后继续处理;若长期不用可忽略本告警。"),
        {"age_seconds": hb.get('age_seconds'), "queued_count": queued},
    )]


def _rule_task_failed_recent(signals: dict) -> list[dict]:
    n = int(signals.get('failed_24h') or 0)
    if n <= 0:
        return []
    recent = signals.get('failed_recent') or []
    ids = [str(r.get('id')) for r in recent]
    sev = 'critical' if n >= FAILED_CRITICAL_AT else 'warn'
    return [_finding(
        '', sev,
        f"24 小时内 {n} 个任务失败",
        f"最近失败任务:#{' #'.join(ids)}。点进任务看事件流与产物定位原因。",
        {"failed_24h": n, "recent_ids": ids},
    )]


def _rule_task_stuck_running(signals: dict) -> list[dict]:
    stuck = signals.get('stuck_running') or []
    # 超过 10 条聚合成一条,防告警爆炸(复审 P3-4 附带)
    if len(stuck) > 10:
        ids = [str(s.get('id')) for s in stuck[:10]]
        return [_finding(
            'aggregate', 'critical',
            f"{len(stuck)} 个任务卡在 running 超 2 小时",
            f"批量卡死(前 10 个:#{' #'.join(ids)}),多半是 Worker 整体出事,优先查 Runner/容器。",
            {"count": len(stuck), "sample_ids": ids},
        )]
    out = []
    for s in stuck:
        tid = s.get('id')
        hours = int(s.get('age_seconds', 0) // 3600)
        out.append(_finding(
            str(tid), 'warn',
            f"任务 #{tid} 已 running 超 {hours} 小时",
            "远超 Codex 单任务上限(30 分钟),多半是 Worker 中途被杀没收尾。"
            "确认无进程在跑后,可在任务详情把它标失败重排。",
            {"task_id": tid, "age_seconds": s.get('age_seconds')},
        ))
    return out


def _rule_approval_backlog(signals: dict) -> list[dict]:
    over24 = int(signals.get('approvals_over_24h') or 0)
    if over24 <= 0:
        return []
    over72 = int(signals.get('approvals_over_72h') or 0)
    sev = 'critical' if over72 > 0 else 'warn'
    return [_finding(
        '', sev,
        f"{over24} 条审批挂起超 24 小时" + (f"(其中 {over72} 条超 72 小时)" if over72 else ""),
        "审批中心有等你拍板的动作;长期挂起的修复补丁会和主干越走越远,越晚合并冲突越多。",
        {"over_24h": over24, "over_72h": over72},
    )]


def _rule_queue_pileup(signals: dict) -> list[dict]:
    queued = int(signals.get('queued_count') or 0)
    if queued < QUEUE_WARN_AT:
        return []
    oldest_h = int((signals.get('oldest_queued_seconds') or 0) // 3600)
    sev = 'critical' if queued >= QUEUE_CRITICAL_AT else 'warn'
    return [_finding(
        '', sev,
        f"队列堆积 {queued} 条任务",
        f"最老的一条已排队 {oldest_h} 小时。检查:总开关是否开着、本机 Runner 是否在线并已授权领取。",
        {"queued_count": queued, "oldest_queued_hours": oldest_h},
    )]


def _rule_report_missing(_signals: dict) -> list[dict]:
    # 只在总开关开着时有意义(定时日报被总开关 gate,关着没日报是设计不是故障)
    if not aiops_db.is_flag_enabled('ai_ops.enabled', default=False):
        return []
    from datetime import date, datetime
    today = date.today()                      # 与 report_builder 同一约定(服务器本地日期)
    report = aiops_db.get_report(today, report_type='daily')
    if report and report.get('status') == 'failed':
        return [_finding(
            f"failed:{today.isoformat()}", 'warn',
            f"今日日报({today})生成失败",
            "日报任务跑了但失败。可在报告中心手动重新生成,并看对应任务事件流。",
            {"report_date": today.isoformat()},
        )]
    if report:
        return []
    if datetime.now().hour < REPORT_ALERT_AFTER_HOUR:
        return []                             # 08:10 才生成,没到点不算缺
    return [_finding(
        f"missing:{today.isoformat()}", 'warn',
        f"今日日报({today})缺失",
        "总开关开着但过点没有生成日报。检查 scheduler 是否在跑,或在报告中心手动生成。",
        {"report_date": today.isoformat()},
    )]


def _rule_kill_switch_on(_signals: dict) -> list[dict]:
    if not aiops_db.is_kill_switch_enabled():
        return []
    return [_finding(
        '', 'info',
        "Kill Switch 急停开着",
        "执行通道全部冻结(任务只排队不执行)。这是提醒不是故障——处理完事故记得解除,别忘在这。",
        {},
    )]


RULES: list[tuple[str, object]] = [
    ('runner_offline', _rule_runner_offline),
    ('task_failed_recent', _rule_task_failed_recent),
    ('task_stuck_running', _rule_task_stuck_running),
    ('approval_backlog', _rule_approval_backlog),
    ('queue_pileup', _rule_queue_pileup),
    ('report_missing', _rule_report_missing),
    ('kill_switch_on', _rule_kill_switch_on),
]


# ==========================================
# 引擎
# ==========================================

def _maybe_auto_task(alert: dict) -> None:
    """
    critical 白名单告警自动立 L0 只读诊断案。kill 关 + 总开关开 双闸。
    幂等键 = 规则+指纹+当天日期(不是告警行 id):手动"标记恢复"后条件仍在
    → 下一轮开出新告警行时命中同一 key → 复用同一任务重新挂链,
    绝不因反复 resolve/refire 重复烧 Codex(复审 P2-1);跨天新事件才立新案。
    """
    if alert.get('severity') != 'critical' or alert.get('rule_key') not in AUTO_TASK_RULES:
        return
    if alert.get('task_id'):
        return
    if aiops_db.is_kill_switch_enabled():
        return                                # 急停冻结一切自主行为
    if not aiops_db.is_flag_enabled('ai_ops.enabled', default=False):
        return
    try:
        from datetime import date
        payload = alert.get('payload') or {}
        ids = payload.get('recent_ids') or []
        task, created = aiops_db.create_task(
            kind='diagnose',
            source_type='alert',
            source_id=str(alert['id']),
            source_context={"alert": {k: alert.get(k) for k in
                                      ('id', 'rule_key', 'severity', 'title', 'detail', 'payload')}},
            title=f"巡逻告警自动立案 · {alert.get('title', '')}"[:120],
            instruction=(
                f"巡逻发现异常:{alert.get('title')}。{alert.get('detail')} "
                f"相关失败任务 id:{', '.join(map(str, ids)) or '见告警 payload'}。"
                "请只读诊断共同根因并给出修复建议,不要改任何文件。"
            ),
            risk_level='L0',
            priority='P2',
            task_key=f"alert:{alert['rule_key']}:{alert.get('fingerprint', '')}:{date.today().isoformat()}",
        )
        # created 与否都补挂链(复审 P3-3:set_alert_task 一次失败/refire 新行都能重新链上)
        aiops_db.set_alert_task(alert['id'], task['id'])
        if created:
            logger.info("[patrol] 告警 #%s 自动立案任务 #%s", alert['id'], task['id'])
    except Exception as e:  # noqa: BLE001 · 立案失败不影响告警本身
        logger.warning("[patrol] 告警 #%s 自动立案失败: %s", alert.get('id'), e)


def run_patrol(force: bool = False) -> Optional[dict]:
    """
    跑一轮巡逻。flag 关且非 force(admin 手动"立即巡逻")→ None。
    返回 {"firing": n, "opened": n, "resolved": n, "duration_ms": n}。
    """
    if not force and not aiops_db.is_flag_enabled('ai_ops.patrol.enabled', default=False):
        return None
    t0 = time.monotonic()
    signals = aiops_db.get_patrol_signals()
    if signals is None:
        logger.warning("[patrol] 信号查询失败,本轮跳过")
        return None

    existing = [a for a in aiops_db.list_alerts(status='firing', limit=500)]
    opened = resolved = 0

    for rule_key, rule_fn in RULES:
        # try 覆盖整段(评估+写告警+消警):任何一步 DB 闪断都只废这一条规则,
        # 且不消警(避免把查询失败误判成恢复)——复审 P3-1/P3-4。
        try:
            findings = rule_fn(signals)
            current_fps = set()
            for f in findings:
                current_fps.add(f['fingerprint'])
                res = aiops_db.upsert_alert(
                    rule_key, severity=f['severity'], title=f['title'],
                    detail=f['detail'], fingerprint=f['fingerprint'], payload=f['payload'],
                )
                if res['opened']:
                    opened += 1
                    logger.warning("[patrol] 拉响告警 [%s/%s] %s", rule_key, f['severity'], f['title'])
                _maybe_auto_task(res['alert'])
            # 恢复:库里 firing 但本轮没再命中的 fingerprint
            for a in existing:
                if a['rule_key'] == rule_key and a['fingerprint'] not in current_fps:
                    resolved += aiops_db.resolve_alerts(rule_key, a['fingerprint'])
        except Exception as e:  # noqa: BLE001 · 单规则失败不拖垮整轮
            logger.warning("[patrol] 规则 %s 本轮失败,跳过(不消警): %s", rule_key, e)
            continue

    firing = aiops_db.count_firing_alerts()
    duration_ms = int((time.monotonic() - t0) * 1000)
    try:
        aiops_db.record_patrol_run(firing_count=firing, opened_count=opened,
                                   resolved_count=resolved, duration_ms=duration_ms)
    except Exception as e:  # noqa: BLE001 · 打卡失败不影响巡逻结果
        logger.warning("[patrol] 巡逻打卡失败: %s", e)
    return {"firing": firing, "opened": opened, "resolved": resolved, "duration_ms": duration_ms}


def run_patrol_job() -> None:
    """Scheduler 入口(每 5 分钟)。flag 关 = 静默跳过,永不抛异常。"""
    try:
        run_patrol(force=False)
    except Exception as e:  # noqa: BLE001
        logger.warning("[patrol] 巡逻轮异常: %s", e)
