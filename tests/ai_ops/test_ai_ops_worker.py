"""services/ai_ops/worker.py run_task 测试(fake Codex + 真实测试库)。"""
import types

import pytest

from db import ai_ops_db as aiops_db
from services.ai_ops import worker as worker_mod
from services.ai_ops import codex_runner


def _config(tmp_path):
    cfg = worker_mod.WorkerConfig()
    cfg.repo_path = str(tmp_path / "clean_repo")
    cfg.worktree_root = str(tmp_path / "worktrees")
    cfg.artifact_root = str(tmp_path / "artifacts")
    cfg.worker_id = "test-worker"
    return cfg


def test_run_task_dry_run(clean_ai_ops, tmp_path):
    task, _ = aiops_db.create_task(kind="diagnose", title="t", instruction="查错")
    worker_mod.run_task(task, _config(tmp_path), dry_run=True)
    got = aiops_db.get_task(task["id"])
    assert got["status"] == "succeeded"
    arts = aiops_db.list_artifacts(task["id"], artifact_type="ops_context")
    assert len(arts) == 1
    assert "Project Rules" in arts[0]["content_text"]


def test_run_task_diagnose_with_fake_codex(clean_ai_ops, tmp_path, monkeypatch):
    calls = {"worktree": 0, "codex": 0, "remove": 0}

    def fake_create(repo, wt, ref="HEAD", runner=None):
        calls["worktree"] += 1

    captured = {}

    def fake_run(worktree, prompt, **kwargs):
        calls["codex"] += 1
        captured["prompt"] = prompt
        # 断言绝不对活 repo 跑
        assert kwargs.get("forbid_paths")
        return {"returncode": 0, "stdout": "", "stderr": "",
                "last_message": "根因: 前端未处理 500", "cmd": []}

    def fake_remove(repo, wt, runner=None):
        calls["remove"] += 1

    monkeypatch.setattr(codex_runner, "create_worktree", fake_create)
    monkeypatch.setattr(codex_runner, "run_codex", fake_run)
    monkeypatch.setattr(codex_runner, "remove_worktree", fake_remove)

    task, _ = aiops_db.create_task(kind="diagnose", title="t", instruction="查 500")
    worker_mod.run_task(task, _config(tmp_path), dry_run=False)

    got = aiops_db.get_task(task["id"])
    assert got["status"] == "succeeded"
    assert "根因" in got["summary"]
    assert calls == {"worktree": 1, "codex": 1, "remove": 1}
    outs = aiops_db.list_artifacts(task["id"], artifact_type="codex_output")
    assert len(outs) == 1
    assert "根因" in outs[0]["content_text"]
    # P0-1:Codex 拿到的是完整 ops_context(不是只有 task.instruction)
    assert "## Task" in captured["prompt"]
    assert "Project Rules" in captured["prompt"]
    assert "现在请执行" in captured["prompt"]


def test_run_task_fix_creates_merge_approval(clean_ai_ops, tmp_path, monkeypatch):
    monkeypatch.setattr(codex_runner, "create_worktree", lambda *a, **k: None)
    monkeypatch.setattr(codex_runner, "remove_worktree", lambda *a, **k: None)
    monkeypatch.setattr(codex_runner, "run_codex",
                        lambda *a, **k: {"returncode": 0, "stdout": "", "stderr": "",
                                         "last_message": "已修", "cmd": []})
    monkeypatch.setattr(codex_runner, "collect_git_diff",
                        lambda wt, **k: ("foo.py | 2 +-",
                                         "diff --git a/foo.py b/foo.py\n+++ b/foo.py\n@@\n+x = 1\n"))
    task, _ = aiops_db.create_task(kind="fix", title="修", instruction="修 bug", risk_level="L1")
    worker_mod.run_task(task, _config(tmp_path), dry_run=False)
    got = aiops_db.get_task(task["id"])
    assert got["status"] == "waiting_approval"     # 干净 diff 也不自动合并,等人工审批
    aps = aiops_db.list_approvals()
    assert len(aps) == 1
    assert aps[0]["action_type"] == "merge_fix"
    assert aps[0]["risk_level"] == "L3"


def test_run_task_fix_redline_diff_is_l4(clean_ai_ops, tmp_path, monkeypatch):
    monkeypatch.setattr(codex_runner, "create_worktree", lambda *a, **k: None)
    monkeypatch.setattr(codex_runner, "remove_worktree", lambda *a, **k: None)
    monkeypatch.setattr(codex_runner, "run_codex",
                        lambda *a, **k: {"returncode": 0, "stdout": "", "stderr": "",
                                         "last_message": "改了计费", "cmd": []})
    monkeypatch.setattr(codex_runner, "collect_git_diff",
                        lambda wt, **k: ("middleware/billing.py | 2 +-",
                                         "diff --git a/middleware/billing.py b/middleware/billing.py\n"
                                         "+++ b/middleware/billing.py\n@@\n+x = 1\n"))
    task, _ = aiops_db.create_task(kind="fix", title="改计费", risk_level="L1")
    worker_mod.run_task(task, _config(tmp_path), dry_run=False)
    aps = aiops_db.list_approvals()
    assert aps[0]["risk_level"] == "L4"            # 红线文件 → L4
    events = aiops_db.list_events(task["id"])
    assert any(e["event_type"] == "review_flagged" for e in events)


def test_run_task_marks_failed_on_nonzero(clean_ai_ops, tmp_path, monkeypatch):
    monkeypatch.setattr(codex_runner, "create_worktree", lambda *a, **k: None)
    monkeypatch.setattr(codex_runner, "remove_worktree", lambda *a, **k: None)
    monkeypatch.setattr(codex_runner, "run_codex",
                        lambda *a, **k: {"returncode": 1, "stdout": "", "stderr": "boom",
                                         "last_message": "", "cmd": []})
    task, _ = aiops_db.create_task(kind="diagnose", title="t", instruction="x")
    worker_mod.run_task(task, _config(tmp_path), dry_run=False)
    assert aiops_db.get_task(task["id"])["status"] == "failed"


def test_process_one_blocked_by_kill_switch(clean_ai_ops, tmp_path):
    aiops_db.create_task(kind="diagnose", title="t")
    aiops_db.set_policy("ai_ops.kill_switch", {"enabled": True})
    did = worker_mod.process_one(_config(tmp_path))
    assert did is False   # Kill Switch 开启不领取


def test_process_one_blocked_when_db_disabled_no_claim(clean_ai_ops, tmp_path):
    # P1-1:DB ai_ops.enabled=false(默认)时,claim 前就返回,任务不被短暂改 running
    aiops_db.create_task(kind="diagnose", title="t")
    cfg = _config(tmp_path)
    cfg.enabled = True   # 进程级开,但 DB 总开关默认关
    did = worker_mod.process_one(cfg)
    assert did is False
    queued = aiops_db.list_tasks(status="queued")
    assert len(queued) == 1                       # 仍 queued,没被 claim
    events = aiops_db.list_events(queued[0]["id"])
    assert not any(e["event_type"] == "claimed" for e in events)
