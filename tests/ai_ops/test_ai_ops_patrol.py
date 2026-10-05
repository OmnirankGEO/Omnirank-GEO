"""主动巡逻(services/ai_ops/patrol · 包B)测试。

覆盖:flag 门控 / 各规则拉响与自动消警 / 自动立案双闸(kill + 总开关)/
去重幂等 / 手动恢复端点 / admin 门禁 / fail-soft。
时间敏感场景(心跳老化 / 任务卡死)用 SQL 直接改时间戳,比较全在 DB 侧。
"""
import types

import pytest

import api.ai_ops_api as ai_ops_api
from db import ai_ops_db as aiops_db
from services.ai_ops import patrol

ADMIN = {"id": 1, "is_admin": True}


def _req(user):
    return types.SimpleNamespace(state=types.SimpleNamespace(user=user))


def _firing(rule_key=None):
    rows = aiops_db.list_alerts(status='firing', limit=100)
    return [a for a in rows if rule_key is None or a['rule_key'] == rule_key]


def _mk_failed_tasks(n):
    ids = []
    for i in range(n):
        t, _ = aiops_db.create_task(kind='diagnose', title=f"fail-{i}", instruction="i")
        aiops_db.update_task_status(t['id'], 'failed', summary="boom")
        ids.append(t['id'])
    return ids


# ==========================================
# flag 门控 + 打卡
# ==========================================

def test_patrol_flag_off_skips(clean_ai_ops):
    assert patrol.run_patrol(force=False) is None          # 默认关 → 静默跳过
    assert aiops_db.get_last_patrol_run() is None


def test_patrol_force_runs_and_records(clean_ai_ops):
    r = patrol.run_patrol(force=True)
    assert r is not None and r['firing'] == 0 and r['opened'] == 0
    last = aiops_db.get_last_patrol_run()
    assert last is not None and last['firing_count'] == 0


def test_patrol_flag_on_runs(clean_ai_ops):
    aiops_db.set_policy('ai_ops.patrol.enabled', {'enabled': True})
    assert patrol.run_patrol(force=False) is not None


# ==========================================
# 规则:任务失败(warn → critical)+ 自动立案双闸
# ==========================================

def test_failed_tasks_warn_alert(clean_ai_ops):
    _mk_failed_tasks(1)
    patrol.run_patrol(force=True)
    alerts = _firing('task_failed_recent')
    assert len(alerts) == 1
    assert alerts[0]['severity'] == 'warn'
    assert alerts[0]['task_id'] is None                    # warn 不自动立案


def test_failed_critical_auto_task_with_gates(clean_ai_ops):
    _mk_failed_tasks(3)
    # 总开关关 → 告警 critical 但不立案
    patrol.run_patrol(force=True)
    a = _firing('task_failed_recent')[0]
    assert a['severity'] == 'critical' and a['task_id'] is None
    # 总开关开 → 自动立案(L0 只读诊断 · source=alert)
    aiops_db.set_policy('ai_ops.enabled', {'enabled': True})
    patrol.run_patrol(force=True)
    a = _firing('task_failed_recent')[0]
    assert a['task_id'] is not None
    task = aiops_db.get_task(a['task_id'])
    assert task['kind'] == 'diagnose' and task['source_type'] == 'alert'
    assert task['risk_level'] == 'L0'
    # 幂等键 = 规则+指纹+日期(不绑告警行 id · 复审 P2-1)
    assert task['task_key'].startswith("alert:task_failed_recent:")
    # 再巡逻:同一告警不重复立案(task_key 幂等 + task_id 已挂)
    before = len(aiops_db.list_tasks(kind='diagnose', limit=100))
    patrol.run_patrol(force=True)
    assert len(aiops_db.list_tasks(kind='diagnose', limit=100)) == before


def test_manual_resolve_then_refire_reuses_same_task(clean_ai_ops):
    """复审 P2-1 回归:手动恢复后条件仍在 → 下一轮新告警行复用同一诊断任务,
    绝不重复立案烧 Codex;新行 task_id 重新挂链(「看诊断任务」按钮不消失)。"""
    _mk_failed_tasks(3)
    aiops_db.set_policy('ai_ops.enabled', {'enabled': True})
    patrol.run_patrol(force=True)
    a1 = _firing('task_failed_recent')[0]
    task_id = a1['task_id']
    assert task_id is not None
    # admin 手动恢复 → 条件仍在 → 下一轮 refire 开新行
    aiops_db.resolve_alert_by_id(a1['id'], resolved_by=1)
    patrol.run_patrol(force=True)
    a2 = _firing('task_failed_recent')[0]
    assert a2['id'] != a1['id']                            # 是新告警行
    assert a2['task_id'] == task_id                        # 但复用同一任务
    diagnose_alert_tasks = [t for t in aiops_db.list_tasks(kind='diagnose', limit=100)
                            if t['source_type'] == 'alert']
    assert len(diagnose_alert_tasks) == 1                  # 全程只立过一案


def test_failed_critical_kill_switch_blocks_auto_task(clean_ai_ops):
    _mk_failed_tasks(3)
    aiops_db.set_policy('ai_ops.enabled', {'enabled': True})
    aiops_db.set_policy('ai_ops.kill_switch', {'enabled': True})
    patrol.run_patrol(force=True)
    a = _firing('task_failed_recent')[0]
    assert a['severity'] == 'critical'
    assert a['task_id'] is None                            # 急停冻结一切自主行为


# ==========================================
# 规则:kill switch 提醒 + 自动消警
# ==========================================

def test_kill_switch_alert_fires_and_resolves(clean_ai_ops):
    aiops_db.set_policy('ai_ops.kill_switch', {'enabled': True})
    patrol.run_patrol(force=True)
    assert len(_firing('kill_switch_on')) == 1
    assert _firing('kill_switch_on')[0]['severity'] == 'info'
    # 解除急停 → 下一轮自动消警
    aiops_db.set_policy('ai_ops.kill_switch', {'enabled': False})
    r = patrol.run_patrol(force=True)
    assert r['resolved'] >= 1
    assert len(_firing('kill_switch_on')) == 0


# ==========================================
# 规则:Runner 离线(时间戳 SQL 老化)
# ==========================================

def test_runner_offline_warn_then_recover(clean_ai_ops, pg_conn):
    aiops_db.upsert_heartbeat("w1", host="h", version="v", env_enabled=False,
                              codex_available=True, ssh_runner_enabled=False, note="")
    patrol.run_patrol(force=True)
    assert len(_firing('runner_offline')) == 0             # 心跳新鲜不告警
    cur = pg_conn.cursor()
    cur.execute("UPDATE ai_ops_worker_heartbeats SET last_seen_at = NOW() - INTERVAL '10 minutes'")
    pg_conn.commit()
    patrol.run_patrol(force=True)
    alerts = _firing('runner_offline')
    assert len(alerts) == 1 and alerts[0]['severity'] == 'warn'   # 队列空 → warn
    # 有任务排队 → 升 critical(同一 fingerprint 原地升级不重复开)
    aiops_db.create_task(kind='diagnose', title="q", instruction="i")
    patrol.run_patrol(force=True)
    alerts = _firing('runner_offline')
    assert len(alerts) == 1 and alerts[0]['severity'] == 'critical'
    # 心跳恢复 → 消警
    aiops_db.upsert_heartbeat("w1", host="h", version="v", env_enabled=False,
                              codex_available=True, ssh_runner_enabled=False, note="")
    patrol.run_patrol(force=True)
    assert len(_firing('runner_offline')) == 0


def test_runner_never_seen_no_alert(clean_ai_ops):
    patrol.run_patrol(force=True)
    assert len(_firing('runner_offline')) == 0             # 从未接入 ≠ 异常


# ==========================================
# 规则:任务卡死(每任务一条 · 恢复消警)
# ==========================================

def test_stuck_running_per_task_and_resolve(clean_ai_ops, pg_conn):
    t, _ = aiops_db.create_task(kind='diagnose', title="stuck", instruction="i")
    aiops_db.update_task_status(t['id'], 'running')
    cur = pg_conn.cursor()
    cur.execute("UPDATE ai_ops_tasks SET started_at = NOW() - INTERVAL '3 hours' WHERE id = %s", (t['id'],))
    pg_conn.commit()
    patrol.run_patrol(force=True)
    alerts = _firing('task_stuck_running')
    assert len(alerts) == 1 and alerts[0]['fingerprint'] == str(t['id'])
    aiops_db.update_task_status(t['id'], 'failed', summary="killed")
    patrol.run_patrol(force=True)
    assert len(_firing('task_stuck_running')) == 0
    # 注意:标 failed 后 task_failed_recent 会接棒拉响(合理:失败要被看见)
    assert len(_firing('task_failed_recent')) == 1


# ==========================================
# 规则:队列堆积 + 审批积压
# ==========================================

def test_queue_pileup_thresholds(clean_ai_ops):
    for i in range(patrol.QUEUE_WARN_AT):
        aiops_db.create_task(kind='diagnose', title=f"q{i}", instruction="i")
    patrol.run_patrol(force=True)
    alerts = _firing('queue_pileup')
    assert len(alerts) == 1 and alerts[0]['severity'] == 'warn'


def test_approval_backlog(clean_ai_ops, pg_conn):
    t, _ = aiops_db.create_task(kind='fix', title="f", instruction="i", risk_level='L1')
    aiops_db.create_approval(t['id'], action_type='merge_fix', risk_level='L3',
                             requested_reason="r", command_plan={})
    cur = pg_conn.cursor()
    cur.execute("UPDATE ai_ops_approvals SET created_at = NOW() - INTERVAL '30 hours'")
    pg_conn.commit()
    patrol.run_patrol(force=True)
    alerts = _firing('approval_backlog')
    assert len(alerts) == 1 and alerts[0]['severity'] == 'warn'
    cur.execute("UPDATE ai_ops_approvals SET created_at = NOW() - INTERVAL '80 hours'")
    pg_conn.commit()
    patrol.run_patrol(force=True)
    assert _firing('approval_backlog')[0]['severity'] == 'critical'


# ==========================================
# 规则:日报缺失/失败(REPORT_ALERT_AFTER_HOUR 可控 · 不依赖真实时刻)
# ==========================================

def test_report_missing_only_when_enabled(clean_ai_ops, monkeypatch):
    monkeypatch.setattr(patrol, 'REPORT_ALERT_AFTER_HOUR', 0)   # 永远"过点"
    patrol.run_patrol(force=True)
    assert len(_firing('report_missing')) == 0             # 总开关关 → 没日报是设计
    aiops_db.set_policy('ai_ops.enabled', {'enabled': True})
    patrol.run_patrol(force=True)
    assert len(_firing('report_missing')) == 1


def test_report_failed_alert_and_ready_resolves(clean_ai_ops, monkeypatch):
    from datetime import date
    monkeypatch.setattr(patrol, 'REPORT_ALERT_AFTER_HOUR', 0)
    aiops_db.set_policy('ai_ops.enabled', {'enabled': True})
    aiops_db.save_report(date.today(), status='failed', summary="x")
    patrol.run_patrol(force=True)
    alerts = _firing('report_missing')
    assert len(alerts) == 1 and '失败' in alerts[0]['title']
    aiops_db.save_report(date.today(), status='ready', summary="ok")
    patrol.run_patrol(force=True)
    assert len(_firing('report_missing')) == 0


def test_report_not_yet_due_no_alert(clean_ai_ops, monkeypatch):
    monkeypatch.setattr(patrol, 'REPORT_ALERT_AFTER_HOUR', 24)  # 永远"没到点"
    aiops_db.set_policy('ai_ops.enabled', {'enabled': True})
    patrol.run_patrol(force=True)
    assert len(_firing('report_missing')) == 0


# ==========================================
# API:列表 / 手动恢复 / 手动巡逻 / admin 门禁
# ==========================================

@pytest.mark.asyncio
async def test_alerts_api_list_and_resolve(clean_ai_ops):
    _mk_failed_tasks(1)
    patrol.run_patrol(force=True)
    res = await ai_ops_api.api_list_alerts(_req(ADMIN))
    assert res['firing_count'] == 1
    alert_id = res['alerts'][0]['id']
    ok = await ai_ops_api.api_resolve_alert(alert_id, _req(ADMIN))
    assert ok['ok'] is True
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:              # 重复恢复 → 404
        await ai_ops_api.api_resolve_alert(alert_id, _req(ADMIN))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_patrol_api_run_and_admin_gate(clean_ai_ops):
    res = await ai_ops_api.api_run_patrol(_req(ADMIN))
    assert res['ok'] is True
    from fastapi import HTTPException
    for call in (
        ai_ops_api.api_run_patrol(_req({"id": 2, "is_admin": False})),
        ai_ops_api.api_list_alerts(_req({"id": 2, "is_admin": False})),
    ):
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == 403


# ==========================================
# 单规则失败不拖垮整轮(fail-soft)
# ==========================================

def test_single_rule_failure_does_not_break_patrol(clean_ai_ops, monkeypatch):
    def boom(_signals):
        raise RuntimeError("rule exploded")
    monkeypatch.setattr(patrol, 'RULES',
                        [('boom_rule', boom)] + [r for r in patrol.RULES if r[0] == 'kill_switch_on'])
    aiops_db.set_policy('ai_ops.kill_switch', {'enabled': True})
    r = patrol.run_patrol(force=True)
    assert r is not None
    assert len(_firing('kill_switch_on')) == 1             # 后续规则照常评估
