"""[v5 req1] GEO 任务耐久结算态 + 补偿队列 · PG 判别性行为测试。

覆盖:
- running→settling→done(commit success=true 才 done);
- commit=false → settlement_pending(不 done · 结果已落库);
- release=false → refund_pending(记 pending_terminal);
- reconcile:settlement_pending--commit-->done · refund_pending--release-->终态 · 仍失败 bump retry 留队列;
- cancel/settle 竞态:running→settling(worker) vs running→refund_pending(cancel)只一个赢,不双动帐;
- 双 worker 并发只一个结算;
- zombie / server_restart → 补偿队列(资金守恒:freeze 由 reconcile 释放【恰一次】)。

判别性:去掉 finalize_success 的 settling/commit 门控(直接 mark done)→ commit_false 测试失败;
        去掉 mark_zombie 的 pending 路由(改回直接 failed)→ zombie 测试失败。
"""
from __future__ import annotations
import sys
import threading
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

FREEZE = 987654


def _conn():
    c = psycopg2.connect(DB); c.cursor_factory = psycopg2.extras.RealDictCursor
    return c


@pytest.fixture(autouse=True)
def _clean():
    require_destructive_allowed(); _assert_safe_test_db(DB)
    with _conn() as c:
        c.cursor().execute("DELETE FROM geo_plan_tasks"); c.commit()
    yield
    with _conn() as c:
        c.cursor().execute("DELETE FROM geo_plan_tasks"); c.commit()


def _insert(status="running", freeze=True, hb_ago_sec=None, result=None, pending_terminal=None,
            data_mode="l1l2_fallback"):
    with _conn() as c:
        cur = c.cursor()
        cur.execute(
            """INSERT INTO geo_plan_tasks
               (user_id, brand_id, status, data_mode, source, freeze_id, result_json, pending_terminal,
                heartbeat_at)
               VALUES (1, 1, %s, %s, 'test', %s, %s::jsonb, %s,
                       CASE WHEN %s IS NULL THEN NOW() ELSE NOW() - (%s || ' seconds')::interval END)
               RETURNING id""",
            (status, data_mode, (FREEZE if freeze else None),
             psycopg2.extras.Json(result) if result is not None else None,
             pending_terminal, hb_ago_sec, str(hb_ago_sec) if hb_ago_sec else '0'),
        )
        tid = cur.fetchone()["id"]; c.commit()
        return tid


def _status(tid):
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT status, pending_terminal, settle_retry_count, result_json FROM geo_plan_tasks WHERE id=%s", (tid,))
        return cur.fetchone()


async def _ok(*a, **k):
    return {"success": True}


async def _false(*a, **k):
    return {"success": False, "reason": "test-forced-false"}


async def _raise(*a, **k):
    raise RuntimeError("test-forced-exception")


def _outbox_events(task_id):
    """读取与任务终态同事务写入的用户通知事件。"""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(
            "SELECT event_type, terminal_state FROM notification_outbox "
            "WHERE business_id=%s ORDER BY id",
            (str(task_id),),
        )
        return list(cur.fetchall())


@pytest.fixture
def patch_billing(monkeypatch):
    """返回一个可设 commit/release 行为的句柄。"""
    import services.geo_plan_settlement as S
    calls = {"commit": 0, "release": 0}

    def set_commit(fn):
        async def _w(*a, **k):
            calls["commit"] += 1
            return await fn(*a, **k)
        monkeypatch.setattr(S, "commit_freeze", _w)

    def set_release(fn):
        async def _w(*a, **k):
            calls["release"] += 1
            return await fn(*a, **k)
        monkeypatch.setattr(S, "release_freeze", _w)

    set_commit(_ok); set_release(_ok)  # 默认成功 · 用例按需覆盖
    return {"set_commit": set_commit, "set_release": set_release, "calls": calls}


# ---------------- 成功路径 ----------------

def test_success_commit_ok_settling_then_done(patch_billing):
    from services.geo_plan_settlement import finalize_success
    tid = _insert(status="running", freeze=True)
    st = asyncio.run(finalize_success(tid, freeze_id=FREEZE, freeze_table="legacy", user_id=1,
                                      brand_id=1, result={"clusters": [1, 2]}, data_mode="l1l2_fallback"))
    assert st == "done"
    assert _status(tid)["status"] == "done"
    assert patch_billing["calls"]["commit"] == 1, "done 前必须 commit_freeze 恰一次"


def test_success_commit_false_goes_settlement_pending(patch_billing):
    from services.geo_plan_settlement import finalize_success
    patch_billing["set_commit"](_false)
    tid = _insert(status="running", freeze=True)
    st = asyncio.run(finalize_success(tid, freeze_id=FREEZE, freeze_table="legacy", user_id=1,
                                      brand_id=1, result={"clusters": [1]}, data_mode="l1l2_fallback"))
    assert st == "settlement_pending"
    row = _status(tid)
    assert row["status"] == "settlement_pending", "🔴 commit=false 必须 → settlement_pending(禁 done)"
    assert row["pending_terminal"] == "done"
    assert row["result_json"] is not None, "结果必须已在 settling 落库(补偿可 done)"


def test_success_commit_exception_goes_settlement_pending(patch_billing):
    from services.geo_plan_settlement import finalize_success
    patch_billing["set_commit"](_raise)
    tid = _insert(status="running", freeze=True)
    st = asyncio.run(finalize_success(tid, freeze_id=FREEZE, freeze_table="legacy", user_id=1,
                                      brand_id=1, result={"clusters": [1]}, data_mode="l1l2_fallback"))
    assert st == "settlement_pending"
    assert _status(tid)["status"] == "settlement_pending"


def test_success_already_terminal_skips(patch_billing):
    from services.geo_plan_settlement import finalize_success
    tid = _insert(status="cancelled", freeze=True)  # 已终态 · settling CAS 必失败
    st = asyncio.run(finalize_success(tid, freeze_id=FREEZE, freeze_table="legacy", user_id=1,
                                      brand_id=1, result={"clusters": [1]}, data_mode="l1l2_fallback"))
    assert st == "skipped"
    assert _status(tid)["status"] == "cancelled", "已终态不得被 settling/done 覆盖"
    assert patch_billing["calls"]["commit"] == 0, "已终态不得 commit(防对已处理 freeze 二次动帐)"


# ---------------- 失败/退款路径 ----------------

def test_failure_release_ok_running_to_terminal(patch_billing):
    from services.geo_plan_settlement import finalize_failure
    tid = _insert(status="running", freeze=True)
    ok = asyncio.run(finalize_failure(tid, freeze_id=FREEZE, freeze_table="legacy", user_id=1, brand_id=1,
                                      terminal="failed", error_code="internal", error_detail="x"))
    assert ok is True
    assert _status(tid)["status"] == "failed"
    assert patch_billing["calls"]["release"] == 1, "退款 release 恰一次"


def test_failure_release_false_goes_refund_pending(patch_billing):
    from services.geo_plan_settlement import finalize_failure
    patch_billing["set_release"](_false)
    tid = _insert(status="running", freeze=True)
    ok = asyncio.run(finalize_failure(tid, freeze_id=FREEZE, freeze_table="legacy", user_id=1, brand_id=1,
                                      terminal="cancelled", error_code="user_cancelled"))
    assert ok is False
    row = _status(tid)
    assert row["status"] == "refund_pending", "🔴 release=false 必须 → refund_pending(freeze 不静默丢)"
    assert row["pending_terminal"] == "cancelled", "记住退成功后应落 cancelled"


def test_failure_no_freeze_direct_terminal(patch_billing):
    from services.geo_plan_settlement import finalize_failure
    tid = _insert(status="running", freeze=False, data_mode="full")
    ok = asyncio.run(finalize_failure(tid, freeze_id=None, freeze_table=None, user_id=1, brand_id=1,
                                      terminal="failed", error_code="internal"))
    assert ok is True and _status(tid)["status"] == "failed"
    assert patch_billing["calls"]["release"] == 0, "无 freeze 不 release"


# ---------------- reconcile 补偿队列 ----------------

def test_reconcile_settlement_pending_commit_ok_to_done(patch_billing):
    from services.geo_plan_settlement import reconcile_pending
    tid = _insert(status="settlement_pending", freeze=True, result={"clusters": [1]}, pending_terminal="done")
    stats = asyncio.run(reconcile_pending(limit=10))
    assert _status(tid)["status"] == "done", "补偿 commit 成功 → done"
    assert stats["settled_done"] >= 1


def test_reconcile_refund_pending_release_ok_to_terminal(patch_billing):
    from services.geo_plan_settlement import reconcile_pending
    tid = _insert(status="refund_pending", freeze=True, pending_terminal="failed")
    stats = asyncio.run(reconcile_pending(limit=10))
    assert _status(tid)["status"] == "failed", "补偿 release 成功 → pending_terminal(failed)"
    assert stats["refunded_terminal"] >= 1


def test_reconcile_commit_still_fails_bumps_retry_stays_pending(patch_billing):
    from services.geo_plan_settlement import reconcile_pending
    patch_billing["set_commit"](_false)
    tid = _insert(status="settlement_pending", freeze=True, result={"clusters": [1]}, pending_terminal="done")
    stats = asyncio.run(reconcile_pending(limit=10))
    row = _status(tid)
    assert row["status"] == "settlement_pending", "补偿仍失败 → 留队列"
    assert row["settle_retry_count"] == 1, "补偿失败必须 bump retry"
    assert stats["still_pending"] >= 1


# ---------------- zombie / restart → 补偿队列 + 资金守恒 ----------------

def test_zombie_routes_running_and_settling_to_pending():
    from db.geo_plan_tasks_db import mark_zombie
    t_run = _insert(status="running", freeze=True, hb_ago_sec=300)
    t_set = _insert(status="settling", freeze=True, hb_ago_sec=300, result={"clusters": [1]})
    n = mark_zombie(threshold_seconds=120)
    assert n == 2
    assert _status(t_run)["status"] == "refund_pending" and _status(t_run)["pending_terminal"] == "failed"
    assert _status(t_set)["status"] == "settlement_pending" and _status(t_set)["pending_terminal"] == "done"


def test_restart_routes_to_pending():
    from db.geo_plan_tasks_db import sweep_server_restart
    t_run = _insert(status="running", freeze=True)
    t_set = _insert(status="settling", freeze=True, result={"clusters": [1]})
    sweep_server_restart(grace_seconds=0)   # [v8] 测试立即清;生产默认 180s 年龄闸(蓝绿共享 DB 防误杀活任务)
    assert _status(t_run)["status"] == "refund_pending"
    assert _status(t_set)["status"] == "settlement_pending"


def test_zombie_then_reconcile_releases_freeze_once(patch_billing):
    """资金守恒:zombie→refund_pending→reconcile release【恰一次】→ failed。"""
    from db.geo_plan_tasks_db import mark_zombie
    from services.geo_plan_settlement import reconcile_pending
    tid = _insert(status="running", freeze=True, hb_ago_sec=300)
    mark_zombie(threshold_seconds=120)
    assert _status(tid)["status"] == "refund_pending"
    asyncio.run(reconcile_pending(limit=10))
    assert _status(tid)["status"] == "failed"
    assert patch_billing["calls"]["release"] == 1, "🔴 freeze 恰释放一次(资金守恒 · 不漏不重)"


# ---------------- 竞态:cancel vs settle ----------------

def test_cancel_settle_race_only_one_terminal(patch_billing):
    """running 上 worker结算(→settling→done) 与 cancel(→refund_pending→cancelled)竞争,只一个赢。"""
    from services.geo_plan_settlement import finalize_success, finalize_failure
    tid = _insert(status="running", freeze=True)
    barrier = threading.Barrier(2)
    results = {}

    def _worker():
        barrier.wait()
        results["settle"] = asyncio.run(finalize_success(
            tid, freeze_id=FREEZE, freeze_table="legacy", user_id=1, brand_id=1,
            result={"clusters": [1]}, data_mode="l1l2_fallback"))

    def _cancel():
        barrier.wait()
        results["cancel"] = asyncio.run(finalize_failure(
            tid, freeze_id=FREEZE, freeze_table="legacy", user_id=1, brand_id=1,
            terminal="cancelled", error_code="user_cancelled"))

    t1 = threading.Thread(target=_worker); t2 = threading.Thread(target=_cancel)
    t1.start(); t2.start(); t1.join(); t2.join()

    final = _status(tid)["status"]
    # 只允许两种收敛:worker 赢(done)或 cancel 赢(cancelled/refund_pending)· 绝不半途矛盾
    assert final in ("done", "cancelled", "refund_pending"), f"竞态收敛异常: {final}"
    # 恰一个通道产生了终态动作(另一个 CAS 未赢)
    settle_won = results.get("settle") == "done"
    cancel_won = results.get("cancel") is True or final in ("cancelled", "refund_pending")
    assert settle_won ^ cancel_won or (final == "done" and not cancel_won), \
        f"竞态必须恰一个赢: settle={results.get('settle')} cancel={results.get('cancel')} final={final}"


def test_reconcile_refund_pending_sends_refunded_notification(patch_billing):
    """补偿确认退费后只宣称“未完成且已退回”，不能降格成普通失败通知。"""
    from services.geo_plan_settlement import reconcile_pending
    tid = _insert(status="refund_pending", freeze=True, pending_terminal="failed")
    # error_code 需为可通知类:直接置 internal
    with _conn() as c:
        c.cursor().execute("UPDATE geo_plan_tasks SET error_code='internal' WHERE id=%s", (tid,)); c.commit()
    asyncio.run(reconcile_pending(limit=10))
    assert _status(tid)["status"] == "failed"
    events = _outbox_events(tid)
    assert len([e for e in events if e["event_type"] == "geo_plan.refunded"]) == 1
    assert not [e for e in events if e["event_type"] == "geo_plan.failed"]


def test_reconcile_refund_pending_cancelled_notifies_refunded_once(patch_billing):
    """取消且已确认退费时，以更强的 refunded 终态通知一次。"""
    from services.geo_plan_settlement import reconcile_pending
    tid = _insert(status="refund_pending", freeze=True, pending_terminal="cancelled")
    with _conn() as c:
        c.cursor().execute("UPDATE geo_plan_tasks SET error_code='user_cancelled' WHERE id=%s", (tid,)); c.commit()
    asyncio.run(reconcile_pending(limit=10))
    assert _status(tid)["status"] == "cancelled"
    events = _outbox_events(tid)
    assert len([e for e in events if e["event_type"] == "geo_plan.refunded"]) == 1
    assert not [e for e in events if e["event_type"] in {"geo_plan.failed", "geo_plan.cancelled"}]


def test_inline_failure_after_release_sends_refunded_notification(patch_billing):
    """inline 释放冻结确认后也必须宣称已退回，不能只写失败。"""
    from services.geo_plan_settlement import finalize_failure
    tid = _insert(status="running", freeze=True)
    asyncio.run(finalize_failure(tid, freeze_id=FREEZE, freeze_table="legacy", user_id=1, brand_id=1,
                                 terminal="failed", error_code="external_api", error_detail="deepseek down"))
    assert _status(tid)["status"] == "failed"
    events = _outbox_events(tid)
    assert len([e for e in events if e["event_type"] == "geo_plan.refunded"]) == 1
    assert not [e for e in events if e["event_type"] == "geo_plan.failed"]


def test_double_worker_only_one_settles(patch_billing):
    """两 worker 并发 finalize_success 同一 running 任务 → 只一个赢 settling→done,另一个 skipped。"""
    from services.geo_plan_settlement import finalize_success
    tid = _insert(status="running", freeze=True)
    barrier = threading.Barrier(2)
    outs = []

    def _w():
        barrier.wait()
        outs.append(asyncio.run(finalize_success(
            tid, freeze_id=FREEZE, freeze_table="legacy", user_id=1, brand_id=1,
            result={"clusters": [1]}, data_mode="l1l2_fallback")))

    ts = [threading.Thread(target=_w) for _ in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert _status(tid)["status"] == "done"
    assert outs.count("done") == 1 and outs.count("skipped") == 1, f"只一个赢结算: {outs}"
    assert patch_billing["calls"]["commit"] == 1, "commit 恰一次(赢家)"
