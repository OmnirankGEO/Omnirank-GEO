"""db/ai_ops_db.py 集成测试(真实 PostgreSQL 测试库)。"""
from pathlib import Path

import pytest

from db import ai_ops_db as aiops_db


# ==========================================
# 任务:创建 / 幂等 / 领取 / 状态 / 取消
# ==========================================

def test_create_task_basic(clean_ai_ops):
    task, created = aiops_db.create_task(kind="diagnose", title="t1", instruction="查一下")
    assert created is True
    assert task["id"] > 0
    assert task["status"] == "queued"
    assert task["kind"] == "diagnose"
    assert task["risk_level"] == "L0"
    # 创建即写一条 created 事件
    events = aiops_db.list_events(task["id"])
    assert any(e["event_type"] == "created" for e in events)


def test_create_task_idempotent_by_key(clean_ai_ops):
    t1, c1 = aiops_db.create_task(kind="diagnose", task_key="feedback:1:diagnose", title="a")
    t2, c2 = aiops_db.create_task(kind="diagnose", task_key="feedback:1:diagnose", title="b")
    assert c1 is True
    assert c2 is False           # 第二次幂等命中
    assert t1["id"] == t2["id"]  # 同一任务


def test_create_task_rejects_bad_kind(clean_ai_ops):
    with pytest.raises(ValueError):
        aiops_db.create_task(kind="not_a_kind", title="x")


def test_claim_next_task_respects_priority(clean_ai_ops):
    aiops_db.create_task(kind="diagnose", title="low", priority="P3")
    high, _ = aiops_db.create_task(kind="diagnose", title="high", priority="P0")
    claimed = aiops_db.claim_next_task("worker-1")
    assert claimed is not None
    assert claimed["id"] == high["id"]          # P0 先于 P3
    assert claimed["status"] == "running"
    assert claimed["assigned_worker_id"] == "worker-1"
    assert claimed["started_at"] is not None


def test_claim_next_task_no_double_claim(clean_ai_ops):
    aiops_db.create_task(kind="diagnose", title="only")
    first = aiops_db.claim_next_task("w1")
    second = aiops_db.claim_next_task("w2")
    assert first is not None
    assert second is None        # 只有一条 queued,不会被领两次


def test_claim_respects_allowed_kinds(clean_ai_ops):
    aiops_db.create_task(kind="report", title="r")
    claimed = aiops_db.claim_next_task("w1", allowed_kinds=["diagnose", "fix"])
    assert claimed is None       # report 不在 allowed_kinds
    claimed2 = aiops_db.claim_next_task("w1", allowed_kinds=["report"])
    assert claimed2 is not None


def test_update_task_status_sets_finished_at(clean_ai_ops):
    task, _ = aiops_db.create_task(kind="diagnose", title="t")
    aiops_db.update_task_status(task["id"], "succeeded", summary="done", result={"root_cause": "x"})
    got = aiops_db.get_task(task["id"])
    assert got["status"] == "succeeded"
    assert got["finished_at"] is not None
    assert got["summary"] == "done"
    assert got["result_jsonb"] == {"root_cause": "x"}


def test_cancel_task(clean_ai_ops):
    task, _ = aiops_db.create_task(kind="diagnose", title="t")
    assert aiops_db.cancel_task(task["id"]) is True
    assert aiops_db.get_task(task["id"])["status"] == "cancelled"
    # 已终态再取消返回 False
    assert aiops_db.cancel_task(task["id"]) is False


def test_count_tasks_by_status(clean_ai_ops):
    aiops_db.create_task(kind="diagnose", title="a")
    aiops_db.create_task(kind="diagnose", title="b")
    counts = aiops_db.count_tasks_by_status()
    assert counts["queued"] == 2
    assert counts["failed"] == 0     # 缺失状态补 0


# ==========================================
# 事件 / 产物
# ==========================================

def test_events_incremental_after_id(clean_ai_ops):
    task, _ = aiops_db.create_task(kind="diagnose", title="t")
    aiops_db.append_event(task["id"], "codex_started", "开跑")
    all_events = aiops_db.list_events(task["id"])
    assert len(all_events) >= 2
    last_id = all_events[0]["id"]
    newer = aiops_db.list_events(task["id"], after_id=last_id)
    assert all(e["id"] > last_id for e in newer)


def test_artifacts(clean_ai_ops):
    task, _ = aiops_db.create_task(kind="fix", title="t")
    aiops_db.add_artifact(task["id"], "patch", title="diff", content_text="--- a\n+++ b")
    aiops_db.add_artifact(task["id"], "test_log", content_text="1 passed")
    patches = aiops_db.list_artifacts(task["id"], artifact_type="patch")
    assert len(patches) == 1
    assert patches[0]["content_text"].startswith("--- a")
    assert len(aiops_db.list_artifacts(task["id"])) == 2


# ==========================================
# 审批
# ==========================================

def test_approval_flow_approve(clean_ai_ops):
    task, _ = aiops_db.create_task(kind="ssh_action", title="restart", risk_level="L3")
    ap_id = aiops_db.create_approval(task["id"], "ssh_command", risk_level="L3",
                                     requested_reason="重启 worker")
    # 任务进入待审批
    assert aiops_db.get_task(task["id"])["status"] == "waiting_approval"
    assert aiops_db.approve_action(ap_id, approved_by=1) is True
    ap = aiops_db.get_approval(ap_id)
    assert ap["approval_status"] == "approved"
    assert ap["approved_by"] == 1
    # 已 approved 再 approve 返回 False
    assert aiops_db.approve_action(ap_id, approved_by=1) is False


def test_approval_flow_reject(clean_ai_ops):
    task, _ = aiops_db.create_task(kind="ssh_action", title="deploy", risk_level="L4")
    ap_id = aiops_db.create_approval(task["id"], "deploy", risk_level="L4")
    assert aiops_db.reject_action(ap_id, approved_by=2, reason="太危险") is True
    assert aiops_db.get_approval(ap_id)["approval_status"] == "rejected"


def test_count_approvals_by_status(clean_ai_ops):
    task, _ = aiops_db.create_task(kind="ssh_action", title="t", risk_level="L3")
    aiops_db.create_approval(task["id"], "ssh_command", risk_level="L3")
    counts = aiops_db.count_approvals_by_status()
    assert counts["pending"] == 1
    assert counts["approved"] == 0


# ==========================================
# 日报
# ==========================================

def test_report_upsert_idempotent(clean_ai_ops):
    from datetime import date
    d = date(2026, 7, 1)
    r1 = aiops_db.save_report(d, markdown="v1", summary="s1")
    r2 = aiops_db.save_report(d, markdown="v2", summary="s2")
    assert r1 == r2   # (report_date, report_type) 唯一 → 同一行覆盖
    got = aiops_db.get_report(d)
    assert got["markdown"] == "v2"
    assert got["summary"] == "s2"
    assert len(aiops_db.list_reports()) == 1


# ==========================================
# 策略 / Kill Switch
# ==========================================

def test_policies_seeded(clean_ai_ops):
    policies = aiops_db.get_policies()
    assert policies["ai_ops.enabled"] == {"enabled": False}
    assert policies["codex.diagnose.enabled"] == {"enabled": True}
    assert policies["ai_ops.kill_switch"] == {"enabled": False}


def test_kill_switch_toggle(clean_ai_ops):
    assert aiops_db.is_kill_switch_enabled() is False
    aiops_db.set_policy("ai_ops.kill_switch", {"enabled": True}, updated_by=1)
    assert aiops_db.is_kill_switch_enabled() is True


# ==========================================
# 迁移已注册进 server 启动迁移清单
# ==========================================

def test_migration_registered_in_server():
    server_src = (Path(__file__).resolve().parents[2] / "server.py").read_text(encoding="utf-8")
    assert "migration_ai_ops_center_2026_07_01.sql" in server_src
