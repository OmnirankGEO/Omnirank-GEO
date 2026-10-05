"""[Deploy-CTO NO-GO finding 5] geo_plan mark_status 无条件 CAS(终态=吸收态)· 判别性 DB 行为测试。

🔴 旧实现只在【目标为终态】时加 CAS 守卫 → cancelled→running / done→running 等非终态目标仍成功
   (任务"复活"· 与已释放/已 commit 的 freeze 矛盾)。
✅ 修复:守卫无条件(源态非终态才允许转移)。
判别性:把 db/geo_plan_tasks_db.py 的 where 改回 `if is_terminal: where += ...`(只终态目标守卫)→
        test_cancelled_cannot_resurrect_to_running / test_done_is_absorbing 失败。

需 TEST_DATABASE_URL / DATABASE_URL 指向已 bootstrap 的 throwaway 库(geo_plan_tasks 表)。
"""
from __future__ import annotations
import os
import sys
from pathlib import Path
import pytest
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _dbsafe import resolve_test_db_url  # noqa: E402

DB = resolve_test_db_url()
pytestmark = pytest.mark.skipif(not DB, reason="需 TEST_DATABASE_URL(本机 test/throwaway 库)")


def _status(task_id):
    c = psycopg2.connect(DB)
    c.cursor_factory = psycopg2.extras.RealDictCursor
    try:
        cur = c.cursor()
        cur.execute("SELECT status FROM geo_plan_tasks WHERE id=%s", (task_id,))
        return cur.fetchone()["status"]
    finally:
        c.close()


def _new_task():
    from db.geo_plan_tasks_db import create_task
    return create_task(
        user_id=888001, brand_id=888001,
        params_json={"t": "nogo"}, brand_snapshot={"n": "x"},
        data_mode="full", source="nogo-test",
    )


def test_cancelled_cannot_resurrect_to_running():
    """cancelled 后 mark_status(running) 必须返 False 且状态保持 cancelled(禁止复活)。"""
    from db.geo_plan_tasks_db import mark_status
    tid = _new_task()
    assert mark_status(tid, "running") is True
    assert mark_status(tid, "cancelled", error_code="user_cancelled") is True
    assert _status(tid) == "cancelled"
    # 🔴 关键断言:复活尝试必须被 CAS 拦下
    won = mark_status(tid, "running")
    assert won is False, "cancelled→running 必须被无条件 CAS 拦下(返 False)"
    assert _status(tid) == "cancelled", "状态必须保持 cancelled(未被复活成 running)"


def test_done_is_absorbing():
    """done 是吸收态:done 后任何 mark(running/failed/cancelled) 均返 False 且不改状态。"""
    from db.geo_plan_tasks_db import mark_status
    tid = _new_task()
    mark_status(tid, "running")
    # [v5] running→settling→done(done 只能从 settling)
    assert mark_status(tid, "settling", result_json={"ok": True}) is True
    assert mark_status(tid, "done") is True
    for target in ("running", "failed", "cancelled", "queued"):
        assert mark_status(tid, target) is False, f"done→{target} 必须被 CAS 拦下"
        assert _status(tid) == "done", f"done 后尝试转 {target} 状态不应改变"


def test_first_terminal_wins():
    """两个终态竞争:先到的 cancelled 保留,后到的 done 被拦(first-terminal-wins)。"""
    from db.geo_plan_tasks_db import mark_status
    tid = _new_task()
    mark_status(tid, "running")
    assert mark_status(tid, "cancelled", error_code="user_cancelled") is True
    assert mark_status(tid, "done", result_json={"late": True}) is False, "已 cancelled,后到 done 必须被拦"
    assert _status(tid) == "cancelled"


# [开源 E3 · B2 · 2026-09-28] services/geo_plan_worker.py 随 C 端 GEO 方案任务 API 删除,守 worker 的格退役;
#   db/geo_plan_tasks_db.py 的 CAS 判据(其余格)在役保留。


def test_normal_lifecycle_still_works():
    """[v5] 正常流程 queued→running→settling→done(done 只能从 settling · commit 确认后才 done)。"""
    from db.geo_plan_tasks_db import mark_status
    tid = _new_task()
    assert _status(tid) == "queued"
    assert mark_status(tid, "running") is True
    assert _status(tid) == "running"
    assert mark_status(tid, "settling", result_json={"ok": 1}) is True
    assert _status(tid) == "settling"
    assert mark_status(tid, "done") is True
    assert _status(tid) == "done"
