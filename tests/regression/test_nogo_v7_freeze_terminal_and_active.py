"""[v7 finding2 + finding4] settle_conflict 人工收口据【真实 freeze 状态】派生终态 + settle_conflict 算在途。

finding2 判别:资金终态必须与 freeze 真实资金方向一致 —— committed(已扣款)只能 done · released(已退款)只能失败态。
  删 read_freeze_status 派生、改回信任 admin 传入终态 → test_released_freeze_forbids_done 转红(允许"已退款却交付")。
finding4 判别:settle_conflict 是非终态占资金 · 必须算在途 —— 删 ACTIVE_STATUSES 的 settle_conflict →
  test_settle_conflict_counts_as_active_brand 转红(返 None → 用户可再起同品牌任务再冻结资金)。
"""
from __future__ import annotations
import sys
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

UID, BRAND = 7301, 8301


def _conn():
    c = psycopg2.connect(DB); c.cursor_factory = psycopg2.extras.RealDictCursor
    return c


@pytest.fixture(autouse=True)
def _clean():
    require_destructive_allowed(); _assert_safe_test_db(DB)
    with _conn() as c:
        cur = c.cursor()
        cur.execute("INSERT INTO users (id, username) VALUES (%s,'u7301') ON CONFLICT (id) DO NOTHING", (UID,))
        cur.execute("DELETE FROM geo_plan_tasks WHERE user_id=%s", (UID,))
        cur.execute("DELETE FROM point_freezes WHERE user_id=%s", (UID,))
        c.commit()
    yield
    with _conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM geo_plan_tasks WHERE user_id=%s", (UID,))
        cur.execute("DELETE FROM point_freezes WHERE user_id=%s", (UID,))
        c.commit()


def _setup_conflict_task(freeze_status: str, pending_terminal: str = "failed"):
    """建一个 settle_conflict 任务 + 对应 legacy point_freezes(status=freeze_status)· 返回 task_id。"""
    with _conn() as c:
        cur = c.cursor()
        cur.execute("INSERT INTO point_freezes (user_id, feature_code, amount_total, status) "
                    "VALUES (%s,'geo_plan_l1l2_fallback',130,%s) RETURNING id", (UID, freeze_status))
        fz = cur.fetchone()["id"]
        cur.execute("INSERT INTO geo_plan_tasks (user_id,brand_id,status,data_mode,source,freeze_id,freeze_table,"
                    "pending_terminal,error_code) VALUES (%s,%s,'settle_conflict','l1l2_fallback','test',%s,'legacy',"
                    "%s,'commit_conflict') RETURNING id", (UID, BRAND, fz, pending_terminal))
        tid = cur.fetchone()["id"]
        c.commit()
        return tid


def _task_status(tid):
    with _conn() as c:
        cur = c.cursor(); cur.execute("SELECT status FROM geo_plan_tasks WHERE id=%s", (tid,))
        return cur.fetchone()["status"]


# ============================================================
# finding2 · 据真实 freeze 状态派生终态
# ============================================================

def test_committed_freeze_only_allows_done():
    """freeze committed(已扣款)→ 只能 done;请求 failed 被拒(防已扣款却标失败)。"""
    from services.geo_plan_settlement import resolve_settle_conflict
    tid = _setup_conflict_task("committed")
    # 请求 failed → 拒(与 committed 矛盾)
    r = resolve_settle_conflict(tid, admin_id=1, requested_terminal="failed")
    assert r["ok"] is False and "committed" in r["reason"], f"🔴 committed 应拒 failed · {r}"
    assert _task_status(tid) == "settle_conflict", "被拒 → 任务仍 settle_conflict"
    # 请求 done → 通过(与 committed 一致)
    r2 = resolve_settle_conflict(tid, admin_id=1, requested_terminal="done")
    assert r2["ok"] is True and r2["terminal"] == "done", f"committed → done · {r2}"
    assert _task_status(tid) == "done"


def test_released_freeze_forbids_done():
    """🔴 freeze released(已退款)→ 只能失败态;请求 done 被拒(防已退款却交付)。"""
    from services.geo_plan_settlement import resolve_settle_conflict
    tid = _setup_conflict_task("released")
    r = resolve_settle_conflict(tid, admin_id=1, requested_terminal="done")
    assert r["ok"] is False and "released" in r["reason"], f"🔴 released 必须拒 done · {r}"
    assert _task_status(tid) == "settle_conflict"
    r2 = resolve_settle_conflict(tid, admin_id=1, requested_terminal="failed")
    assert r2["ok"] is True and r2["terminal"] == "failed", f"released → 失败态 · {r2}"
    assert _task_status(tid) == "failed"


def test_released_freeze_no_terminal_defaults_failed_not_done():
    """🔴🔴 [v7 对抗审 P1] released + admin 省略 terminal(None)+ 任务 pending_terminal='done'
    (settling→settlement_pending 派生冲突的典型口径)→ 绝不能回落 done · 必须落失败态。

    这是 v7 finding2 修复的【残留洞】:read_freeze_status 已判 released,但省略 terminal 时旧代码回落
    task.pending_terminal='done' → 已退款却交付。删本修复 → 转红。
    """
    from services.geo_plan_settlement import resolve_settle_conflict
    tid = _setup_conflict_task("released", pending_terminal="done")   # freeze 已退款 · 但任务残留 pending_terminal='done'
    r = resolve_settle_conflict(tid, admin_id=1, requested_terminal=None)  # admin 省略终态
    assert r["ok"] is True, f"released + 省略 terminal 应成功落失败态 · {r}"
    assert r["terminal"] != "done", f"🔴 released 绝不能落 done(即使 pending_terminal='done')· 实得 {r['terminal']}"
    assert r["terminal"] == "failed"
    assert _task_status(tid) == "failed", "🔴 已退款任务必须落失败态(不交付)"


def test_frozen_freeze_refused_to_reconcile():
    """freeze 仍 frozen(未终结)→ 拒绝人工强制终态(应交补偿队列 reconcile)。"""
    from services.geo_plan_settlement import resolve_settle_conflict
    tid = _setup_conflict_task("frozen")
    r = resolve_settle_conflict(tid, admin_id=1, requested_terminal="failed")
    assert r["ok"] is False and "frozen" in r["reason"], f"🔴 frozen 未终结应拒 · {r}"
    assert _task_status(tid) == "settle_conflict"


def test_read_freeze_status_legacy():
    """read_freeze_status 精确读 legacy point_freezes 的真实 status。"""
    from services.geo_plan_settlement import read_freeze_status
    with _conn() as c:
        cur = c.cursor()
        cur.execute("INSERT INTO point_freezes (user_id, feature_code, amount_total, status) "
                    "VALUES (%s,'f',1,'committed') RETURNING id", (UID,))
        fz = cur.fetchone()["id"]; c.commit()
    assert read_freeze_status(fz, "legacy", UID) == "committed"
    assert read_freeze_status(999999999, "legacy", UID) is None, "不存在 → None"


# ============================================================
# finding4 · settle_conflict 算在途(去重 + 并发)
# ============================================================

def test_settle_conflict_counts_as_active_brand():
    """🔴 settle_conflict 任务在途 → find_running_for_brand 返回它(非 None)· 防再起同品牌任务再冻结资金。"""
    from db.geo_plan_tasks_db import find_running_for_brand
    tid = _setup_conflict_task("committed")
    hit = find_running_for_brand(BRAND)
    assert hit is not None and hit["id"] == tid, "🔴 settle_conflict 必须算在途(否则可再起同品牌任务再冻结)"


def test_settle_conflict_counts_user_concurrency():
    """🔴 settle_conflict 计入用户并发 count_running_for_user(防并发绕过冻结资金)。"""
    from db.geo_plan_tasks_db import count_running_for_user
    _setup_conflict_task("committed")
    assert count_running_for_user(UID) >= 1, "🔴 settle_conflict 必须计入用户在途并发"
