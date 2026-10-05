"""[v6 req1] GEO commit/release 精确终态校验 · 反向终态测试。

commit 仅接受 fresh success 或 idempotent+status=committed;release 仅接受 fresh 或 idempotent+status=released。
相反终态(commit 时已 released / release 时已 committed)或歧义 → settle_conflict(禁 done/failed)+ 落 fund_recovery 工单。

判别性:把 _commit_outcome 改回"idempotent 一律 ok"(不看 status)→ 本文件 conflict 断言失败(错落 done)。
"""
from __future__ import annotations
import sys
import asyncio
from pathlib import Path
import pytest
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _dbsafe import resolve_test_db_url, require_destructive_allowed, _assert_safe_test_db  # noqa: E402

DB = resolve_test_db_url()
pytestmark = pytest.mark.skipif(not DB, reason="需 TEST_DATABASE_URL(本机 test/throwaway 库)")

FREEZE = 771001


def _conn():
    c = psycopg2.connect(DB); c.cursor_factory = psycopg2.extras.RealDictCursor
    return c


@pytest.fixture(autouse=True)
def _clean():
    require_destructive_allowed(); _assert_safe_test_db(DB)
    from db.fund_recovery_db import init_fund_recovery_tables
    with _conn() as c:
        init_fund_recovery_tables(c.cursor()); c.commit()
        cur = c.cursor()
        cur.execute("DELETE FROM geo_plan_tasks"); cur.execute("DELETE FROM fund_recovery_orders"); c.commit()
    yield
    with _conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM geo_plan_tasks"); cur.execute("DELETE FROM fund_recovery_orders"); c.commit()


def _insert(status="running", pending_terminal=None, result=None):
    with _conn() as c:
        cur = c.cursor()
        cur.execute(
            """INSERT INTO geo_plan_tasks (user_id, brand_id, status, data_mode, source, freeze_id, result_json, pending_terminal)
               VALUES (1,1,%s,'l1l2_fallback','test',%s,%s::jsonb,%s) RETURNING id""",
            (status, FREEZE, psycopg2.extras.Json(result) if result else None, pending_terminal))
        tid = cur.fetchone()["id"]; c.commit(); return tid


def _status(tid):
    with _conn() as c:
        cur = c.cursor(); cur.execute("SELECT status, error_code FROM geo_plan_tasks WHERE id=%s", (tid,))
        return cur.fetchone()


def _recovery_count(op=None):
    with _conn() as c:
        cur = c.cursor()
        if op:
            cur.execute("SELECT COUNT(*) AS n FROM fund_recovery_orders WHERE source='geo_plan_settle' AND kind=%s", (op,))
        else:
            cur.execute("SELECT COUNT(*) AS n FROM fund_recovery_orders WHERE source='geo_plan_settle'")
        return cur.fetchone()["n"]


async def _fresh_ok(*a, **k):
    return {"success": True, "freeze_id": FREEZE, "amount": 130}


async def _idem_committed(*a, **k):
    return {"success": True, "idempotent": True, "status": "committed"}


async def _idem_released(*a, **k):
    return {"success": True, "idempotent": True, "status": "released"}


async def _ambiguous(*a, **k):
    return {"success": False, "ambiguous": True, "reason": "跨表撞号"}


async def _notfound(*a, **k):
    return {"success": False, "reason": "未找到冻结记录"}


@pytest.fixture
def patch(monkeypatch):
    import services.geo_plan_settlement as S
    def set_commit(fn): monkeypatch.setattr(S, "commit_freeze", fn)
    def set_release(fn): monkeypatch.setattr(S, "release_freeze", fn)
    return {"commit": set_commit, "release": set_release}


# ---------------- outcome 单元 ----------------

def test_commit_outcome_matrix():
    from services.geo_plan_settlement import _commit_outcome
    assert _commit_outcome({"success": True}) == "ok"                                   # fresh
    assert _commit_outcome({"success": True, "idempotent": True, "status": "committed"}) == "ok"
    assert _commit_outcome({"success": True, "idempotent": True, "status": "released"}) == "conflict"  # 🔴 相反终态
    assert _commit_outcome({"success": False, "ambiguous": True}) == "conflict"
    assert _commit_outcome({"success": False, "reason": "未找到"}) == "retry"
    assert _commit_outcome(None) == "retry"


def test_release_outcome_matrix():
    from services.geo_plan_settlement import _release_outcome
    assert _release_outcome({"success": True}) == "ok"
    assert _release_outcome({"success": True, "idempotent": True, "status": "released"}) == "ok"
    assert _release_outcome({"success": True, "idempotent": True, "status": "committed"}) == "conflict"  # 🔴 相反终态
    assert _release_outcome({"success": False, "ambiguous": True}) == "conflict"
    assert _release_outcome({"success": False, "reason": "未找到"}) == "retry"


# ---------------- inline 反向终态 ----------------

def test_inline_commit_conflict_goes_settle_conflict_not_done(patch):
    from services.geo_plan_settlement import finalize_success
    patch["commit"](_idem_released)  # commit 时 freeze 已被 released → 冲突
    tid = _insert("running")
    st = asyncio.run(finalize_success(tid, freeze_id=FREEZE, freeze_table="legacy", user_id=1, brand_id=1,
                                      result={"clusters": [1]}, data_mode="l1l2_fallback"))
    assert st == "settle_conflict"
    assert _status(tid)["status"] == "settle_conflict", "🔴 commit 命中 released 禁落 done → settle_conflict"
    assert _recovery_count("commit") == 1, "冲突必落 fund_recovery 人工工单"


def test_inline_commit_committed_ok_done(patch):
    from services.geo_plan_settlement import finalize_success
    patch["commit"](_idem_committed)
    tid = _insert("running")
    st = asyncio.run(finalize_success(tid, freeze_id=FREEZE, freeze_table="legacy", user_id=1, brand_id=1,
                                      result={"clusters": [1]}, data_mode="l1l2_fallback"))
    assert st == "done" and _status(tid)["status"] == "done", "idempotent+committed 应 ok→done"
    assert _recovery_count() == 0


def test_inline_release_conflict_goes_settle_conflict_not_failed(patch):
    from services.geo_plan_settlement import finalize_failure
    patch["release"](_idem_committed)  # release 时 freeze 已被 committed → 冲突
    tid = _insert("running")
    ok = asyncio.run(finalize_failure(tid, freeze_id=FREEZE, freeze_table="legacy", user_id=1, brand_id=1,
                                      terminal="failed", error_code="internal"))
    assert ok is False
    assert _status(tid)["status"] == "settle_conflict", "🔴 release 命中 committed 禁落 failed → settle_conflict"
    assert _recovery_count("release") == 1


# ---------------- reconcile 反向终态 ----------------

def test_reconcile_settlement_pending_commit_conflict(patch):
    from services.geo_plan_settlement import reconcile_pending
    patch["commit"](_idem_released)
    tid = _insert("settlement_pending", pending_terminal="done", result={"clusters": [1]})
    stats = asyncio.run(reconcile_pending(limit=10))
    assert _status(tid)["status"] == "settle_conflict", "🔴 reconcile commit 命中 released → settle_conflict(非 done)"
    assert stats.get("conflict", 0) >= 1 and _recovery_count("commit") == 1


def test_reconcile_refund_pending_release_conflict(patch):
    from services.geo_plan_settlement import reconcile_pending
    patch["release"](_idem_committed)
    tid = _insert("refund_pending", pending_terminal="failed")
    stats = asyncio.run(reconcile_pending(limit=10))
    assert _status(tid)["status"] == "settle_conflict", "🔴 reconcile release 命中 committed → settle_conflict(非 failed)"
    assert stats.get("conflict", 0) >= 1 and _recovery_count("release") == 1


def test_ambiguous_commit_is_conflict_not_retry(patch):
    from services.geo_plan_settlement import finalize_success
    patch["commit"](_ambiguous)
    tid = _insert("running")
    st = asyncio.run(finalize_success(tid, freeze_id=FREEZE, freeze_table="legacy", user_id=1, brand_id=1,
                                      result={"clusters": [1]}, data_mode="l1l2_fallback"))
    assert st == "settle_conflict", "跨表撞号歧义应进 settle_conflict(人工)非无限 retry"


def test_settle_conflict_workorder_exactly_once(patch):
    """[v6 对抗审 P3] 并发/重复触达同一 conflict 任务:仅赢 settle_conflict CAS 的落工单(exactly-once)。"""
    from services.geo_plan_settlement import _raise_settle_conflict
    tid = _insert("settling", result={"clusters": [1]})
    res = {"idempotent": True, "status": "released"}
    _raise_settle_conflict(tid, freeze_id=FREEZE, user_id=1, brand_id=1, op="commit", res=res)
    _raise_settle_conflict(tid, freeze_id=FREEZE, user_id=1, brand_id=1, op="commit", res=res)  # CAS 失败方
    assert _status(tid)["status"] == "settle_conflict"
    assert _recovery_count("commit") == 1, "🔴 重复触达同一 conflict 任务只落一条工单"


def test_settle_conflict_not_dead_end_can_exit_to_terminal(patch):
    """[v6 对抗审 P2] settle_conflict 不是永久死态:可被人工收口到终态(admin settle-geo-task 走此转移)。"""
    from services.geo_plan_settlement import _raise_settle_conflict
    from db.geo_plan_tasks_db import mark_status
    tid = _insert("settling", result={"clusters": [1]})
    _raise_settle_conflict(tid, freeze_id=FREEZE, user_id=1, brand_id=1, op="commit", res={"idempotent": True, "status": "released"})
    assert _status(tid)["status"] == "settle_conflict"
    assert mark_status(tid, "done") is True, "🔴 settle_conflict 必须能人工收口到 done(非永久死态)"
    # [v7 finding2] API 收口出口存在且走 resolve_settle_conflict(据真实 freeze 派生终态 · 内部调 mark_status)
    src = (ROOT / "api" / "admin_fund_recovery_api.py").read_text(encoding="utf-8")
    assert "settle-geo-task" in src and "resolve_settle_conflict" in src, \
        "admin 必须有 settle_conflict 收口出口(经 resolve_settle_conflict 据 freeze 派生终态)"
