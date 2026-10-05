"""[v8 · Deploy-CTO NO-GO 返工] GEO 任务资金/状态耐久性 · 4 类行为测试。

对应老板要求补的四类行为测试:
  1. 跨账本同 ID:refund_evidence_exists 只查 ledger_type 声明的账本,绝不因两表同数字 order_id 误判(P1-1)。
  2. dispatch+release 双失败:dispatch 失败进耐久 finalize_failure;release 也失败 → 留 refund_pending 补偿队列(P1-2)。
  3. 冻结后进程退出:sweep_server_restart 覆盖 queued(funded→refund_pending / 未冻 full→cancelled / l1l2 orphan→refund_pending)(P1-3)。
  4. 通用 resolve 不得关 GEO 冲突工单:geo_plan_settle 工单禁走通用终结接口 + settle_conflict 缺工单巡检补建(P2)。

判别性(删对应修复 → 转红)见各测试 docstring。
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

U, B = 7401, 8401


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
        cur.execute("INSERT INTO users (id, username) VALUES (%s,'u7401') ON CONFLICT (id) DO NOTHING", (U,))
        cur.execute("DELETE FROM fund_recovery_orders")
        cur.execute("DELETE FROM geo_plan_tasks WHERE user_id=%s", (U,))
        cur.execute("DELETE FROM point_transactions WHERE user_id=%s", (U,))
        c.commit()
    yield
    with _conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM fund_recovery_orders")
        cur.execute("DELETE FROM geo_plan_tasks WHERE user_id=%s", (U,))
        cur.execute("DELETE FROM point_transactions WHERE user_id=%s", (U,))
        c.commit()


def _mk_task(brand, data_mode="l1l2_fallback"):
    from db.geo_plan_tasks_db import create_task
    return create_task(user_id=U, brand_id=brand, params_json={}, brand_snapshot={},
                       data_mode=data_mode, source="test")


def _status(tid):
    from db.geo_plan_tasks_db import get_task_by_id
    return get_task_by_id(tid)["status"]


# ============================================================
# 1. 跨账本同 ID(P1-1)
# ============================================================

def test_refund_evidence_ledger_isolation():
    """🔴 [P1-1] point_transactions 与 customer_credit_transactions 的 id 空间独立 · 同数字可能都存在。
    refund_evidence_exists 必须【只查 ledger_type 声明的账本】· 绝不跨表误判(否则客户实际未退款却被误 resolved 漏退)。
    删 ledger_type 隔离(两表都查)→ v35 查会命中 legacy 的同号退款 → 本测试转红。"""
    from db.fund_recovery_db import refund_evidence_exists
    N = 55501
    with _conn() as c:
        cur = c.cursor()
        # legacy 有完整 user+feature+charge 证据;v35 无对应 consume/refund。
        cur.execute("DELETE FROM point_transactions WHERE id=%s", (N,))
        cur.execute(
            "INSERT INTO point_transactions "
            "(id,user_id,type,point_type,amount,balance_after,feature_code) "
            "VALUES (%s,%s,'consume','paid',-130,0,'article_gen')",
            (N, U),
        )
        cur.execute("INSERT INTO point_transactions (user_id,type,point_type,amount,balance_after,feature_code,order_id) "
                    "VALUES (%s,'refund','paid',130,130,'article_gen',%s)", (U, str(N)))
        c.commit()
    assert refund_evidence_exists(U, N, "v35", "article_gen") is False, "🔴 v35 账本无此退款 · 绝不串 legacy 同号 order_id 误判"
    assert refund_evidence_exists(U, N, "legacy", "article_gen") is True, "legacy 账本确有此退款"
    assert refund_evidence_exists(U, N, None, "article_gen") is False, "ledger_type 缺失 → 保守 False(不谎报已退)"
    assert refund_evidence_exists(U, None, "legacy", "article_gen") is False, "charge_tx_id 缺失 → 保守 False"
    assert refund_evidence_exists(U, N, "legacy", None) is False, "feature_code 缺失 → 保守 False"


# ============================================================
# 2. dispatch+release 双失败(P1-2)
# ============================================================

def test_dispatch_release_double_failure_stays_refund_pending(monkeypatch):
    """🔴 [P1-2] dispatch 失败走耐久 finalize_failure;若 release 也失败 → 任务【留 refund_pending】补偿队列,
    绝不丢、绝不静默吞、绝不假成功。删 finalize_failure 的 refund_pending 耐久(改回吞异常)→ 转红。"""
    import services.geo_plan_settlement as S
    from db.geo_plan_tasks_db import update_freeze_id
    tid = _mk_task(B + 7)
    update_freeze_id(tid, 99777, "legacy")

    async def _fail_release(**kw):
        return {"success": False, "reason": "billing down"}   # 瞬时失败(非终态)
    monkeypatch.setattr(S, "release_freeze", _fail_release)

    ok = asyncio.run(S.finalize_failure(tid, freeze_id=99777, freeze_table="legacy", user_id=U, brand_id=B + 7,
                                        terminal="cancelled", error_code="dispatch_failed"))
    assert ok is False, "release 失败 → 未落终态(返 False)"
    assert _status(tid) == "refund_pending", "🔴 dispatch+release 双失败必须留 refund_pending 补偿队列(不丢/不假成功)"


# ============================================================
# 3. 冻结后进程退出(P1-3)
# ============================================================

def test_sweep_covers_queued_funded_and_unfunded():
    """🔴 [P1-3] worker 是进程内 asyncio.create_task · 重启后 queued 永久无 worker。sweep 必须覆盖 queued:
    funded→refund_pending · l1l2 orphan(freeze_id 空)→refund_pending · 确定无资金 full→cancelled。
    删 sweep 的 queued 分支 → 前两条转红(冻结额度永久卡死)。"""
    from db.geo_plan_tasks_db import update_freeze_id, sweep_server_restart
    tid_funded = _mk_task(B + 10, data_mode="l1l2_fallback")
    update_freeze_id(tid_funded, 99001, "legacy")
    tid_full = _mk_task(B + 11, data_mode="full")               # 无冻结 · 确定无资金
    tid_orphan = _mk_task(B + 12, data_mode="l1l2_fallback")    # l1l2 但 freeze_id 空(回填前崩溃 orphan)
    sweep_server_restart(grace_seconds=0)   # [v8] 测试立即清;生产默认 180s 年龄闸(蓝绿共享 DB 防误杀对端活任务)
    assert _status(tid_funded) == "refund_pending", "🔴 funded queued 必须进退款补偿队列"
    assert _status(tid_orphan) == "refund_pending", "🔴 l1l2 orphan queued 必须进退款补偿(覆盖 freeze_id 回填前崩溃)"
    assert _status(tid_full) == "cancelled", "确定无资金 full queued → 安全取消"


def test_sweep_grace_spares_fresh_peer_tasks():
    """🔴 [P1-3 对抗审 P2] 蓝绿共享一个 DB · green 启动跑 sweep 时 blue 仍活 · 年龄闸必须放过【对端刚建的新任务】。
    删年龄闸(一刀切 sweep)→ 刚建的 funded queued 被误杀退款 → 转红。"""
    from db.geo_plan_tasks_db import update_freeze_id, sweep_server_restart
    tid = _mk_task(B + 20, data_mode="l1l2_fallback")
    update_freeze_id(tid, 99020, "legacy")
    swept = sweep_server_restart()   # 默认 grace=180s · 刚建(queued_at=NOW)不该被清
    assert _status(tid) == "queued", "🔴 grace 内的新任务不得被 sweep 误杀(蓝绿共享 DB 安全)"


def test_reconcile_releases_orphan_freeze_by_task_ref(monkeypatch):
    """[P1-3] refund_pending 且 freeze_id 空(orphan)→ reconcile 按 task_ref 尝试 release · 释放/确认无冻结后落终态。"""
    import services.geo_plan_settlement as S
    from db.geo_plan_tasks_db import mark_status
    tid = _mk_task(B + 13, data_mode="l1l2_fallback")
    # 进 refund_pending(freeze_id 空 · pending_terminal=cancelled)
    mark_status(tid, "refund_pending", pending_terminal="cancelled", error_code="server_restart_queued")
    released = {"by_task_ref": None}

    async def _rel(**kw):
        released["by_task_ref"] = kw.get("task_ref")
        return {"success": True, "freeze_id": 1}   # 模拟 orphan 冻结被释放
    monkeypatch.setattr(S, "release_freeze", _rel)
    monkeypatch.setattr(S, "list_pending_settlements", lambda limit=50: [
        {"id": tid, "status": "refund_pending", "freeze_id": None, "freeze_table": "legacy",
         "user_id": U, "brand_id": B + 13, "pending_terminal": "cancelled", "error_code": "server_restart_queued"}])
    asyncio.run(S.reconcile_pending(limit=10))
    assert released["by_task_ref"] == f"geoplan_task_{tid}", "🔴 orphan 必须按 task_ref 尝试释放冻结"
    assert _status(tid) == "cancelled", "释放后落终态 cancelled"


def test_reconcile_no_orphan_freeze_still_terminals(monkeypatch):
    """[v8 对抗审 P2] refund_pending 且 freeze_id 空 · release 返"未找到冻结记录"(确无冻结)→ 仍落终态(不永久留队列)。"""
    import services.geo_plan_settlement as S
    from db.geo_plan_tasks_db import mark_status
    tid = _mk_task(B + 14, data_mode="l1l2_fallback")
    mark_status(tid, "refund_pending", pending_terminal="cancelled", error_code="server_restart_queued")

    async def _no_freeze(**kw):
        return {"success": False, "reason": "未找到冻结记录"}   # 确实无冻结(回填前从未冻结)
    monkeypatch.setattr(S, "release_freeze", _no_freeze)
    monkeypatch.setattr(S, "list_pending_settlements", lambda limit=50: [
        {"id": tid, "status": "refund_pending", "freeze_id": None, "freeze_table": "legacy",
         "user_id": U, "brand_id": B + 14, "pending_terminal": "cancelled", "error_code": "server_restart_queued"}])
    asyncio.run(S.reconcile_pending(limit=10))
    assert _status(tid) == "cancelled", "🔴 确无冻结('未找到')也必须落终态 · 不无限留队列"


# ============================================================
# 4. 通用 resolve 不得关 GEO 冲突工单(P2)
# ============================================================

def test_generic_terminal_rejects_geo_settle():
    """🔴 [P2] geo_plan_settle(settle_conflict 冲突)工单禁走通用 resolve/fail(会致工单与任务脱钩)· 必须走 settle-geo-task。"""
    from db.fund_recovery_db import create_recovery_order
    from api.admin_fund_recovery_api import _reject_geo_settle_via_generic
    from fastapi import HTTPException
    oid = create_recovery_order("geo_plan_settle", "commit", ref_key="geoplan_1", status="manual", charge_tx_id=None)
    with pytest.raises(HTTPException):
        _reject_geo_settle_via_generic(oid)
    # 非 geo_plan_settle 工单不拦
    oid2 = create_recovery_order("article_gen_thread_window", "refund", ref_key="x", charge_tx_id=999)
    _reject_geo_settle_via_generic(oid2)   # 不抛


def test_patrol_creates_missing_settle_conflict_workorder():
    """🔴 [P2] _raise_settle_conflict 先标任务后建单 · 建单失败则任务无人工入口。巡检必须补建缺失工单(幂等)。"""
    from db.geo_plan_tasks_db import mark_status
    from db.fund_recovery_db import has_open_workorder
    from services.geo_plan_settlement import ensure_settle_conflict_workorders
    tid = _mk_task(B + 15, data_mode="l1l2_fallback")
    mark_status(tid, "running"); mark_status(tid, "settling", result_json={})
    mark_status(tid, "settle_conflict", error_code="commit_conflict")   # 模拟"标了任务但没建成工单"
    assert has_open_workorder("geo_plan_settle", f"geoplan_{tid}") is False
    res = ensure_settle_conflict_workorders()
    assert res["created"] >= 1, "🔴 巡检必须为缺工单的 settle_conflict 任务补建"
    assert has_open_workorder("geo_plan_settle", f"geoplan_{tid}") is True
    # 幂等:再跑不重复建
    ensure_settle_conflict_workorders()
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM fund_recovery_orders WHERE source='geo_plan_settle' AND ref_key=%s",
                    (f"geoplan_{tid}",))
        assert cur.fetchone()["n"] == 1, "巡检幂等 · 不重复建工单"


def test_patrol_closes_workorder_when_task_terminal():
    """🔴 [v8 对抗审 P2] 反向:工单未终结但关联任务已终态(settle_geo_task 先标任务后关工单崩溃 / 重复工单)→
    巡检必须收口工单,否则永久 un-closeable(通用接口被拦 + settle-geo-task 因任务终态 409)。"""
    from db.geo_plan_tasks_db import mark_status
    from db.fund_recovery_db import create_recovery_order, get_recovery_order
    from services.geo_plan_settlement import ensure_settle_conflict_workorders
    tid = _mk_task(B + 16, data_mode="l1l2_fallback")
    # 任务已到终态,但残留一条未终结 geo_plan_settle 工单
    mark_status(tid, "running"); mark_status(tid, "settling", result_json={}); mark_status(tid, "settle_conflict")
    mark_status(tid, "done")   # settle_conflict→done(人工已收口任务,但工单未关)
    oid = create_recovery_order("geo_plan_settle", "commit", ref_key=f"geoplan_{tid}", status="manual",
                                charge_tx_id=None, payload={"task_id": tid})
    res = ensure_settle_conflict_workorders()
    assert res["closed"] >= 1, "🔴 任务已终态的开口工单必须被巡检反向收口"
    assert get_recovery_order(oid)["status"] == "resolved"


def test_geoplan_index_prededup_survives_existing_duplicates():
    """🔴🔴 [v8 二轮对抗审 P1] prod 已有重复 open geo_plan_settle 工单(TOCTOU/蓝绿双建历史)时,init/迁移建唯一键
    【前必须 pre-dedup】· 否则 CREATE UNIQUE INDEX 报 duplicate key → 污染 init_wallet_tables 共享事务 →
    ledger_type/escrow schema 全回滚 + 索引永不建 + 每次启动重复失败。删 pre-dedup → init 抛异常 → 转红。"""
    from db.fund_recovery_db import init_fund_recovery_tables
    ref = "geoplan_888001"
    with _conn() as c:
        cur = c.cursor()
        cur.execute("DROP INDEX IF EXISTS uniq_fund_recovery_geoplan_open")   # 先去键才能塞重复
        for k in ("commit", "state_fix"):   # 同 (source,ref_key) 两条 open · 模拟 TOCTOU/蓝绿双建
            cur.execute("INSERT INTO fund_recovery_orders (source, ref_key, kind, status, charge_tx_id) "
                        "VALUES ('geo_plan_settle', %s, %s, 'manual', NULL)", (ref, k))
        c.commit()
    # 重建唯一键:pre-dedup 折叠重复后 CREATE UNIQUE INDEX 才成功(不报 duplicate · 不污染事务)
    with _conn() as c:
        init_fund_recovery_tables(c.cursor()); c.commit()
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM fund_recovery_orders WHERE source='geo_plan_settle' AND ref_key=%s "
                    "AND status IN ('pending','processing','manual')", (ref,))
        assert cur.fetchone()["n"] == 1, "🔴 pre-dedup 必须折叠到只剩 1 条未终结"
        cur.execute("SELECT 1 FROM pg_indexes WHERE indexname='uniq_fund_recovery_geoplan_open'")
        assert cur.fetchone() is not None, "🔴 唯一键必须建成(pre-dedup 后 CREATE 不报 duplicate)"


def test_geoplan_settle_workorder_dedup_unique_index():
    """🔴 [v8 对抗审 P2] DB 唯一键兜底 exactly-once:同任务(source,ref_key)重复登记 geo_plan_settle 工单被挡(防 TOCTOU 双建)。"""
    from db.fund_recovery_db import create_recovery_order
    ref = "geoplan_777001"
    a = create_recovery_order("geo_plan_settle", "commit", ref_key=ref, status="manual", charge_tx_id=None,
                              payload={"task_id": 777001})
    # 不同 kind(state_fix)· 同 (source,ref_key)→ 唯一键挡(ON CONFLICT DO NOTHING)
    create_recovery_order("geo_plan_settle", "state_fix", ref_key=ref, status="manual", charge_tx_id=None,
                          payload={"task_id": 777001})
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM fund_recovery_orders WHERE source='geo_plan_settle' AND ref_key=%s "
                    "AND status IN ('pending','processing','manual')", (ref,))
        assert cur.fetchone()["n"] == 1, "🔴 同任务 geo_plan_settle 工单唯一键去重(exactly-once)"
