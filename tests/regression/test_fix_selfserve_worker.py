"""[GEO-R8-CAN-009] 判别性回归锁 —— services/research_monitor/selfserve_worker.py 结算对账自愈。

主形态 = source-inspection:断言修复标志(自动对账器 + 幂等 commit 重试 + 不 release + marker 升级)
存在于源码。回退修复(删对账器 / 改成 release / 静默清 marker)则相应断言失败。
不依赖 DB / 不 import server.py。

附:纯逻辑单测(monkeypatch commit_freeze + set_selfserve_task_status,不碰 DB)验对账三分支行为。
"""
import os
import re
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

WORKER_PATH = ROOT / "services" / "research_monitor" / "selfserve_worker.py"
SRC = WORKER_PATH.read_text(encoding="utf-8")


# ============================================================
# source-inspection 判别锁
# ============================================================


def test_reconciler_function_present():
    """[GEO-R8-CAN-009] 自动对账器 + 其 sync 包装必须存在(回退删除则失败)。"""
    assert "async def reconcile_pending_settlement_selfserve(" in SRC
    assert "def reconcile_pending_settlement_selfserve_sync(" in SRC


def test_reconciler_retries_idempotent_commit():
    """[GEO-R8-CAN-009] 对账器必须重试 commit_freeze(自愈 needs_manual),而非放任冻结。"""
    m = re.search(
        r"async def reconcile_pending_settlement_selfserve\(.*?\n(?=\ndef reconcile_pending_settlement_selfserve_sync)",
        SRC, re.DOTALL,
    )
    assert m, "找不到 reconcile_pending_settlement_selfserve 函数体"
    body = m.group(0)
    assert "commit_freeze(" in body, "对账器必须调 commit_freeze 幂等补扣"
    # 铁律:对账器绝不 release(release = 免费送)。
    assert "release_freeze(" not in body, "对账器绝不能 release 冻结(免费送)"


def test_reconciler_targets_needs_manual_and_escalates_recharge():
    """[GEO-R8-CAN-009] 只捞 needs_manual 单;冻结已 released 时升级 needs_recharge 不静默当成功。"""
    assert "_SETTLEMENT_NEEDS_MANUAL_MARKER" in SRC
    assert "_SETTLEMENT_NEEDS_RECHARGE_MARKER" in SRC
    assert "billing_commit_failed_needs_manual" in SRC
    # released 幂等 → 升级 needs_recharge 分支存在
    assert 'commit_result.get("status") == "released"' in SRC
    assert "_SETTLEMENT_NEEDS_RECHARGE_MARKER" in SRC


def test_reconciler_only_marks_reconciled_on_success():
    """[GEO-R8-CAN-009] 只有 commit success=True 才清 marker(reconciled),success!=True 保持 needs_manual。"""
    assert "_SETTLEMENT_RECONCILED_MARKER" in SRC
    assert 'commit_result.get("success") is not True' in SRC


# ============================================================
# 纯逻辑单测(monkeypatch·不碰 DB)
# ============================================================


def _load_worker_with_stubs(commit_return=None, commit_raises=None):
    """import selfserve_worker 并 monkeypatch 其模块级 commit_freeze / set_selfserve_task_status /
    列表函数,收集 set_selfserve_task_status 调用。返回 (module, calls, release_calls)。"""
    import importlib
    import services.research_monitor.selfserve_worker as w
    importlib.reload(w)

    status_calls = []
    release_calls = []

    async def fake_commit(**kwargs):
        if commit_raises is not None:
            raise commit_raises
        return commit_return

    async def fake_release(**kwargs):
        release_calls.append(kwargs)
        return {"success": True}

    def fake_set_status(task_id, status, failed_reason=None, mark_started=False, mark_finished=False):
        status_calls.append({"task_id": task_id, "status": status, "failed_reason": failed_reason})

    def fake_list(limit=200):
        return [{"id": 42, "freeze_id": 7, "freeze_table": "v35", "user_id": 99}]

    w.commit_freeze = fake_commit
    w.release_freeze = fake_release
    w.set_selfserve_task_status = fake_set_status
    w._list_needs_manual_settlement_tasks = fake_list
    return w, status_calls, release_calls


def _run(coro):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_behavior_success_clears_marker():
    """commit success=True → 清 marker 为 reconciled,计 1 条,绝不 release。"""
    w, calls, releases = _load_worker_with_stubs(commit_return={"success": True})
    n = _run(w.reconcile_pending_settlement_selfserve())
    assert n == 1
    assert releases == []
    assert any(c["failed_reason"] == w._SETTLEMENT_RECONCILED_MARKER for c in calls)


def test_behavior_released_escalates_recharge():
    """commit 命中已 released 幂等 → 升级 needs_recharge,不计成功,绝不 release。"""
    w, calls, releases = _load_worker_with_stubs(
        commit_return={"success": True, "idempotent": True, "status": "released"},
    )
    n = _run(w.reconcile_pending_settlement_selfserve())
    assert n == 0
    assert releases == []
    assert any(c["failed_reason"] == w._SETTLEMENT_NEEDS_RECHARGE_MARKER for c in calls)


def test_behavior_ambiguous_keeps_needs_manual():
    """commit success!=True(ambiguous)→ 保持 needs_manual(不清 marker · 不 release)。"""
    w, calls, releases = _load_worker_with_stubs(
        commit_return={"success": False, "ambiguous": True},
    )
    n = _run(w.reconcile_pending_settlement_selfserve())
    assert n == 0
    assert releases == []
    # 不应写 reconciled / recharge marker
    assert not any(
        c["failed_reason"] in (w._SETTLEMENT_RECONCILED_MARKER, w._SETTLEMENT_NEEDS_RECHARGE_MARKER)
        for c in calls
    )


def test_behavior_commit_exception_keeps_needs_manual():
    """commit 抛异常 → 保持 needs_manual(不动钱 · 不 release · 不清 marker)。"""
    w, calls, releases = _load_worker_with_stubs(commit_raises=RuntimeError("billing down"))
    n = _run(w.reconcile_pending_settlement_selfserve())
    assert n == 0
    assert releases == []
    assert calls == []
