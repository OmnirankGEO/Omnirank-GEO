"""services/ai_ops/ssh_runner.py 测试(DB · 不接真实 SSH)。"""
import types
from datetime import datetime, timedelta, timezone

from db import ai_ops_db as aiops_db
from services.ai_ops import ssh_runner


def _approved(task_id, action_type, risk="L3", expires_at=None):
    ap_id = aiops_db.create_approval(task_id, action_type, risk_level=risk, expires_at=expires_at)
    aiops_db.approve_action(ap_id, approved_by=1)
    return ap_id


def test_plan_only_when_runner_disabled(clean_ai_ops):
    task, _ = aiops_db.create_task(kind="ssh_action", title="t", risk_level="L3")
    ap_id = _approved(task["id"], "prod_status")
    result = ssh_runner.execute_approved_action(ap_id)   # 默认 real_execute=False
    assert result["executed"] is False
    assert result["reason"] == "runner_disabled_or_plan_only"
    events = aiops_db.list_events(task["id"])
    assert any(e["event_type"] == "ssh_plan_recorded" for e in events)


def test_kill_switch_blocks_execution(clean_ai_ops):
    task, _ = aiops_db.create_task(kind="ssh_action", title="t", risk_level="L3")
    ap_id = _approved(task["id"], "prod_status")
    aiops_db.set_policy("ai_ops.kill_switch", {"enabled": True})
    result = ssh_runner.execute_approved_action(ap_id, real_execute=True)
    assert result["executed"] is False
    assert result["reason"] == "kill_switch_on"


def test_expired_approval_not_executed(clean_ai_ops):
    task, _ = aiops_db.create_task(kind="ssh_action", title="t", risk_level="L3")
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    ap_id = _approved(task["id"], "prod_status", expires_at=past)
    result = ssh_runner.execute_approved_action(ap_id, real_execute=True)
    assert result["executed"] is False
    assert result["reason"] == "expired"


def test_non_executable_action_refused(clean_ai_ops):
    task, _ = aiops_db.create_task(kind="ssh_action", title="deploy", risk_level="L4")
    ap_id = _approved(task["id"], "deploy", risk="L4")
    result = ssh_runner.execute_approved_action(ap_id, real_execute=True)
    assert result["executed"] is False
    assert result["reason"] == "action_not_executable_v1"


def test_executed_action_not_rerun(clean_ai_ops):
    # P1-3:已执行的 approved 动作,下一轮不再执行(幂等)
    task, _ = aiops_db.create_task(kind="ssh_action", title="status", risk_level="L3")
    ap_id = _approved(task["id"], "prod_status")
    aiops_db.set_policy("ssh_runner.enabled", {"enabled": True})
    calls = []

    def fake_executor(cmd):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    r1 = ssh_runner.execute_approved_action(ap_id, real_execute=True, executor=fake_executor)
    r2 = ssh_runner.execute_approved_action(ap_id, real_execute=True, executor=fake_executor)
    assert r1["executed"] is True
    assert r2["executed"] is False and r2["reason"] == "already_executed"
    assert len(calls) == 1                       # 只真跑一次
    assert aiops_db.get_approval(ap_id)["approval_status"] == "executed"


def test_real_execute_readonly_with_fake_executor(clean_ai_ops):
    task, _ = aiops_db.create_task(kind="ssh_action", title="status", risk_level="L3")
    ap_id = _approved(task["id"], "prod_status")
    aiops_db.set_policy("ssh_runner.enabled", {"enabled": True})

    def fake_executor(cmd):
        assert cmd[:2] == ["docker", "ps"]
        return types.SimpleNamespace(returncode=0, stdout="omnirank-green Up 3 days", stderr="")

    result = ssh_runner.execute_approved_action(ap_id, real_execute=True, executor=fake_executor)
    assert result["executed"] is True
    assert result["returncode"] == 0
    assert "omnirank-green" in result["stdout"]
    events = aiops_db.list_events(task["id"])
    assert any(e["event_type"] == "ssh_executed" for e in events)
