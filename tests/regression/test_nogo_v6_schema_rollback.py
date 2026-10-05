"""[v6 req5] GEO 结算态 schema 独立可执行 rollback · fail-closed 判别性测试。

pending 非零 → rollback 必须 raise(fail-closed · 不伪造终态);排空后 → rollback 成功收窄回 6 态。
PG DDL 事务性:测试内 rollback()【不 commit】→ schema 收窄被撤销 → 不污染共享 throwaway 库。

判别性:去掉 rollback 的 precheck fail-closed 守卫 → test_rollback_fail_closed_on_pending 失败(带 pending 也回滚)。
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


def _insert(status):
    with _conn() as c:
        cur = c.cursor()
        cur.execute("INSERT INTO geo_plan_tasks (user_id,brand_id,status,data_mode,source,pending_terminal) "
                    "VALUES (1,1,%s,'full','test',%s) RETURNING id",
                    (status, "failed" if status == "refund_pending" else None))
        tid = cur.fetchone()["id"]; c.commit(); return tid


def test_rollback_fail_closed_on_pending():
    """🔴 有 settlement_pending/refund_pending/settle_conflict → rollback 必须 fail-closed(raise)。"""
    from scripts.rollback_geo_plan_settlement_v5v6 import rollback, precheck
    _insert("refund_pending")
    with _conn() as c:
        cur = c.cursor()
        pc = precheck(cur)
        assert pc["pending"] >= 1 and pc["safe"] is False
        with pytest.raises(RuntimeError, match="fail-closed"):
            rollback(cur)
        c.rollback()


def test_rollback_fail_closed_on_inflight_running():
    """[v6 对抗审 P2] 在途 running/queued 也阻止 rollback(其后继 settling/refund_pending 会被 shrink 移除致崩)。"""
    from scripts.rollback_geo_plan_settlement_v5v6 import rollback, precheck
    _insert("running")
    with _conn() as c:
        cur = c.cursor()
        assert precheck(cur)["safe"] is False, "🔴 在途 running 任务必须算 blocking"
        with pytest.raises(RuntimeError, match="fail-closed"):
            rollback(cur)
        c.rollback()


def test_rollback_succeeds_when_no_pending():
    """无残留 pending(全终态)→ rollback 收窄回 6 态成功(事务内验证 · 不 commit 不污染库)。"""
    from scripts.rollback_geo_plan_settlement_v5v6 import rollback, precheck
    _insert("done"); _insert("failed")
    with _conn() as c:
        cur = c.cursor()
        assert precheck(cur)["safe"] is True
        r = rollback(cur)   # DDL 在事务内(未 commit)
        assert r["rolled_back"] is True
        # 收窄后:写 settlement_pending(18 字符)应被拒(CHECK 或列宽 · 用 savepoint 隔离)
        cur.execute("SAVEPOINT sp1")
        with pytest.raises(psycopg2.Error):
            cur.execute("INSERT INTO geo_plan_tasks (user_id,brand_id,status,data_mode,source) "
                        "VALUES (1,1,'settlement_pending','full','t')")
        cur.execute("ROLLBACK TO SAVEPOINT sp1")
        c.rollback()        # 🔴 撤销 DDL → 共享库 schema 保持 v6(PG 事务性 DDL)


def test_rollback_fail_closed_on_archived_conflict():
    """🔴 [v7 finding5] 归档的 settle_conflict(CHECK-illegal 态)也必须阻止 rollback。

    旧 precheck 对全部 blocking 态加 archived_at IS NULL → 归档的 settle_conflict 被漏算 → 误报 safe=True →
    随后 shrink CHECK 时归档行 CheckViolation → rollback 中途崩(finding5)。现 CHECK-illegal 态全表计数。
    """
    from scripts.rollback_geo_plan_settlement_v5v6 import precheck, rollback
    with _conn() as c:
        cur = c.cursor()
        cur.execute("INSERT INTO geo_plan_tasks (user_id,brand_id,status,data_mode,source,archived_at) "
                    "VALUES (1,1,'settle_conflict','full','test',NOW())")
        c.commit()
    with _conn() as c:
        cur = c.cursor()
        pc = precheck(cur)
        assert pc["safe"] is False, "🔴 归档 settle_conflict 必须算 blocking(否则 shrink CHECK 崩)"
        assert pc["blocking_by_state"].get("settle_conflict", 0) >= 1
        with pytest.raises(RuntimeError, match="fail-closed"):
            rollback(cur)
        c.rollback()


def test_rollback_allows_archived_inflight():
    """[v7 finding5] 归档的 running/queued(CHECK-legal 且惰性 · 不会转移)不阻止 rollback —— 仅非归档在途才 quiesce。"""
    from scripts.rollback_geo_plan_settlement_v5v6 import precheck
    with _conn() as c:
        cur = c.cursor()
        cur.execute("INSERT INTO geo_plan_tasks (user_id,brand_id,status,data_mode,source,archived_at) "
                    "VALUES (1,1,'running','full','test',NOW())")   # 归档 running(惰性)
        cur.execute("INSERT INTO geo_plan_tasks (user_id,brand_id,status,data_mode,source) "
                    "VALUES (1,1,'done','full','test')")
        c.commit()
    with _conn() as c:
        cur = c.cursor()
        assert precheck(cur)["safe"] is True, "归档 running(CHECK-legal 惰性)不该 block · 只非归档在途才 quiesce"


def test_rollback_does_not_fake_terminal():
    """rollback 源码不得用直接 UPDATE 伪造 done/failed(只 reconcile 排空 + shrink schema)。"""
    src = (ROOT / "scripts" / "rollback_geo_plan_settlement_v5v6.py").read_text(encoding="utf-8")
    import re
    assert not re.search(r"UPDATE\s+geo_plan_tasks\s+SET\s+status\s*=\s*'(done|failed|cancelled|timeout)'", src, re.I), \
        "🔴 rollback 禁止直接 UPDATE 伪造终态"
    assert "fail-closed" in src and "reconcile" in src
