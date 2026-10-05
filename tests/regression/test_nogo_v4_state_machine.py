"""[v4→v5 req1] GEO task 状态机 + 并发 · 判别性 DB 行为测试(throwaway PG · schema 由 conftest 自动 provisioning)。

v5 严格转移表:running 仅 queued→;settling 仅 running→;done 仅 settling/settlement_pending→;
  settlement_pending 仅 settling→;refund_pending 仅 queued/running→;failed/timeout 仅 running/refund_pending→;
  cancelled 仅 queued/running/refund_pending→。
  🔴 v5 关键:running→done 【不再合法】(必须 running→settling→done · commit 确认后才 done)。

判别性:把 mark_status 改回粗守卫 → test_running_running_rejected / test_queued_to_done_rejected /
        test_running_to_done_rejected 失败。
"""
from __future__ import annotations
import sys
import threading
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _dbsafe import resolve_test_db_url  # noqa: E402

DB = resolve_test_db_url()
pytestmark = pytest.mark.skipif(not DB, reason="需 TEST_DATABASE_URL(本机 test/throwaway 库)")


def _new_task():
    from db.geo_plan_tasks_db import create_task
    return create_task(user_id=880501, brand_id=880501, params_json={"t": "v4"},
                       brand_snapshot={"n": "x"}, data_mode="full", source="v4-test")


def _status(tid):
    import psycopg2, psycopg2.extras
    c = psycopg2.connect(DB); c.cursor_factory = psycopg2.extras.RealDictCursor
    try:
        cur = c.cursor(); cur.execute("SELECT status FROM geo_plan_tasks WHERE id=%s", (tid,))
        return cur.fetchone()["status"]
    finally:
        c.close()


def test_transition_table_valid_and_invalid():
    from db.geo_plan_tasks_db import mark_status
    tid = _new_task()  # queued
    # 非法:done/failed/timeout/settling 不能从 queued
    assert mark_status(tid, "done") is False, "queued→done 必须拒"
    assert mark_status(tid, "failed") is False, "queued→failed 必须拒"
    assert mark_status(tid, "timeout") is False, "queued→timeout 必须拒"
    assert mark_status(tid, "settling") is False, "queued→settling 必须拒(settling 只能从 running)"
    assert _status(tid) == "queued"
    # 合法:queued→running
    assert mark_status(tid, "running") is True
    # 非法:running→running(幂等复标必须拒)
    assert mark_status(tid, "running") is False, "running→running 必须拒"
    # 🔴 v5:running→done 不再合法(必须先 settling)
    assert mark_status(tid, "done", result_json={"ok": 1}) is False, "running→done 必须拒(v5 须先 settling)"
    # 合法:running→settling→done
    assert mark_status(tid, "settling", result_json={"ok": 1}) is True
    assert mark_status(tid, "done") is True
    # 非法:done→任何(终态吸收)
    assert mark_status(tid, "cancelled") is False
    assert mark_status(tid, "running") is False


def test_running_running_rejected():
    from db.geo_plan_tasks_db import mark_status
    tid = _new_task()
    assert mark_status(tid, "running") is True
    assert mark_status(tid, "running") is False, "🔴 running→running 必须 False(v3 粗守卫会 True)"


def test_queued_to_done_rejected():
    from db.geo_plan_tasks_db import mark_status
    tid = _new_task()
    assert mark_status(tid, "done") is False, "🔴 queued→done 必须 False(done 只能从 settling/settlement_pending)"


def test_running_to_done_rejected():
    from db.geo_plan_tasks_db import mark_status
    tid = _new_task()
    assert mark_status(tid, "running") is True
    assert mark_status(tid, "done") is False, "🔴 v5:running→done 必须 False(须先 settling · commit 确认后才 done)"


def test_settling_to_settlement_pending_and_done():
    from db.geo_plan_tasks_db import mark_status
    tid = _new_task()
    mark_status(tid, "running")
    assert mark_status(tid, "settling") is True
    # settling→settlement_pending(commit 失败)· settlement_pending→done(补偿 commit ok)
    assert mark_status(tid, "settlement_pending", pending_terminal="done") is True
    assert mark_status(tid, "done") is True, "settlement_pending→done 合法"


def test_refund_pending_to_terminal():
    from db.geo_plan_tasks_db import mark_status
    tid = _new_task()
    mark_status(tid, "running")
    assert mark_status(tid, "refund_pending", pending_terminal="failed") is True
    # refund_pending→failed(补偿 release ok)
    assert mark_status(tid, "failed") is True
    assert mark_status(tid, "done") is False, "终态吸收"


def test_cancelled_from_queued_or_running_only():
    from db.geo_plan_tasks_db import mark_status
    t1 = _new_task()
    assert mark_status(t1, "cancelled") is True, "queued→cancelled 合法"
    t2 = _new_task()
    mark_status(t2, "running")
    assert mark_status(t2, "cancelled") is True, "running→cancelled 合法"
    # cancelled 后不能再 cancel
    assert mark_status(t2, "cancelled") is False


def test_double_worker_only_one_enters_running():
    """两 worker 同抢 running:只有一个 CAS=True(进入业务),另一个 False(立即退出)。"""
    from db.geo_plan_tasks_db import mark_status
    tid = _new_task()
    results = []
    barrier = threading.Barrier(2)

    def w():
        barrier.wait()
        results.append(mark_status(tid, "running"))

    ts = [threading.Thread(target=w) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count(True) == 1, f"🔴 只能一个 worker 进入 running,实际 {results}"
    assert results.count(False) == 1
    assert _status(tid) == "running"


def test_settle_vs_refund_race_one_winner_no_double():
    """running 后 settling(结算) 与 refund_pending(取消/失败)竞态:只有一个 CAS=True,状态=赢家。

    这是 cancel/settle 竞态的状态机原语:两条 claim 都从 running 出发,CAS 保证只一个赢,
    另一个 False → 其 freeze 副作用(commit/release)不会执行,不白送/双扣/错通知。
    """
    from db.geo_plan_tasks_db import mark_status
    tid = _new_task()
    assert mark_status(tid, "running") is True
    results = {}
    barrier = threading.Barrier(2)

    def w(target, kw):
        barrier.wait()
        results[target] = mark_status(tid, target, **kw)

    ts = [
        threading.Thread(target=w, args=("settling", {"result_json": {"ok": 1}})),
        threading.Thread(target=w, args=("refund_pending", {"pending_terminal": "cancelled"})),
    ]
    [t.start() for t in ts]
    [t.join() for t in ts]
    won = [k for k, v in results.items() if v]
    assert len(won) == 1, f"🔴 settling/refund_pending 竞态必须只一个赢(否则白送/双扣/错通知),实际 {results}"
    assert _status(tid) == won[0], "状态必须 = 赢家"
