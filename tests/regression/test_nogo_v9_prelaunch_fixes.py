"""[v9 · Deploy-CTO NO-GO 返工] 上线前缺陷修复 · 行为级判别测试。

对应老板裁决 6 项(P1×3 + P2×2 + 交付说明):
  P1-1 冻结成功但 update_freeze_id 失败 → 禁先写 cancelled;有 freeze_id 走耐久 finalize_failure(不悬挂资金)。
  P1-2 渠道收益记账/冲销 fail-open → SAVEPOINT 隔离 + 同事务耐久补偿工单(exactly-once);processor 幂等重试;
       fund_recovery_orders.kind CHECK 放宽含 record/reverse(否则 insert 违反 CHECK → 主事务回滚 brick)。
  P1-3 refund_points 增可选 ledger_type · 按【原扣费账本】精确退款(不按退款时刻 _is_v35_customer 猜);processor 透传。
  P2-4 settle_conflict 正向巡检 id 游标分页排空(去 LIMIT 200 硬顶)· 第 201+ 条不再饥饿。
  P2-5 PowerShell 每批 pytest 后立即检查退出码(脚本级 · 见 scripts/test_bootstrap_throwaway_pg.ps1)。

判别性(删对应修复 → 转红)见各测试 docstring。渠道收益【真机器端到端】(真报价→记账/冲销)在 tests/pricing_ssot。
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

U, B = 7901, 8901


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
        cur.execute("INSERT INTO users (id, username) VALUES (%s,'u7901') ON CONFLICT (id) DO NOTHING", (U,))
        cur.execute("DELETE FROM fund_recovery_orders")
        cur.execute("DELETE FROM geo_plan_tasks WHERE user_id=%s", (U,))
        c.commit()
    yield
    with _conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM fund_recovery_orders")
        cur.execute("DELETE FROM geo_plan_tasks WHERE user_id=%s", (U,))
        c.commit()


def _mk_task(brand, data_mode="l1l2_fallback"):
    from db.geo_plan_tasks_db import create_task
    return create_task(user_id=U, brand_id=brand, params_json={}, brand_snapshot={},
                       data_mode=data_mode, source="test")


def _status(tid):
    from db.geo_plan_tasks_db import get_task_by_id
    return get_task_by_id(tid)["status"]


# ============================================================
# P1-1 冻结成功但回填失败 → 耐久收口(禁先写 cancelled)
# ============================================================

# ============================================================
# P1-2 渠道收益 fail-open → 同事务耐久补偿(exactly-once)
# ============================================================

def test_channel_revenue_record_failure_enqueues_durable(monkeypatch):
    """🔴 [P1-2] 渠道收益记账失败 → 【禁 fail-open 只记日志继续完成订单】· 必须 SAVEPOINT 回滚(不污染主事务)
    并【同事务】登记耐久补偿工单(source='channel_revenue' kind='record')。
    删本修复(改回旧 fail-open) → 无工单 → 转红。"""
    import db.wallet_db as W
    import config.pricing_ssot_flags as flg
    monkeypatch.setattr(flg, "channel_pricing_enabled", lambda: True)

    def _boom(cur, order):
        raise RuntimeError("record boom")
    monkeypatch.setattr(W, "_do_record_channel_revenue", _boom)

    with _conn() as c:
        cur = c.cursor()
        W._record_channel_revenue_if_applicable(cur, {"id": "ord-crfail-9", "price_quote_id": "q-x"})
        # 主事务未被毒化(savepoint 已回滚)· 仍可继续(模拟订单其余步骤照常提交)
        cur.execute("SELECT 1 AS ok")
        assert cur.fetchone()["ok"] == 1, "🔴 SAVEPOINT 回滚后主事务必须仍可用(记账失败不阻断主充值)"
        cur.execute("SELECT kind, status FROM fund_recovery_orders "
                    "WHERE source='channel_revenue' AND ref_key='ord-crfail-9'")
        row = cur.fetchone()
        assert row is not None, "🔴 记账失败必须登记耐久补偿工单(exactly-once · 不 fail-open 静默漏记)"
        assert row["kind"] == "record" and row["status"] == "pending"
        c.commit()


def test_insert_recovery_order_cursor_rides_txn_and_allows_channel_kinds():
    """🔴 [P1-2] insert_recovery_order_cursor 复用调用方 cursor【不 commit】(与主业务原子共存亡)·
    且 kind='record'/'reverse' 不被 CHECK 拒绝(v6 老 CHECK 只含 refund/release/commit/state_fix)。
    删 kind CHECK 放宽 → INSERT 违反 CHECK 抛错 → 转红(且生产会 brick)。"""
    from db.fund_recovery_db import insert_recovery_order_cursor
    c1 = _conn()
    cur1 = c1.cursor()
    wid = insert_recovery_order_cursor(cur1, "channel_revenue", "reverse", ref_key="ord-ride-1",
                                       reason="t", payload={"order_id": "ord-ride-1"})
    assert wid is not None, "🔴 kind='reverse' 必须被 CHECK 允许(否则违反约束抛错)"
    # 未 commit → 另一连接看不到(证明复用调用方事务)
    with _conn() as c2:
        cur2 = c2.cursor()
        cur2.execute("SELECT COUNT(*) AS n FROM fund_recovery_orders WHERE ref_key='ord-ride-1'")
        assert cur2.fetchone()["n"] == 0, "🔴 未 commit 前不可见 · 证明 insert 复用调用方事务(非独立提交)"
    c1.commit()
    with _conn() as c3:
        cur3 = c3.cursor()
        cur3.execute("SELECT kind FROM fund_recovery_orders WHERE ref_key='ord-ride-1'")
        assert cur3.fetchone()["kind"] == "reverse", "commit 后可见"
    c1.close()


def test_processor_routes_channel_revenue_branch(monkeypatch):
    """🔴 [P1-2] process_pending 必须把 source='channel_revenue' 工单路由到渠道补偿分支(_retry_channel_revenue)·
    不落入 else(通用无处理器 → 退避待人工)。删 processor 的 channel_revenue 分支 → resolved 不增 → 转红。"""
    import services.fund_recovery_processor as P
    from db.fund_recovery_db import create_recovery_order
    seen = {"n": 0}

    def _fake_retry(wo):
        seen["n"] += 1
        return "done"   # [v10 item3] _retry_channel_revenue 现返三态字符串
    monkeypatch.setattr(P, "_retry_channel_revenue", _fake_retry)
    create_recovery_order("channel_revenue", "record", ref_key="ord-proc-1",
                          reason="t", payload={"order_id": "ord-proc-1"}, status="pending")
    stats = asyncio.run(P.process_pending(limit=5))
    assert seen["n"] == 1, "🔴 channel_revenue 工单必须进渠道补偿分支(非通用 else)"
    assert stats["resolved"] >= 1, "补偿成功 → resolved"


# ============================================================
# P1-3 refund_points 按原扣费账本路由(红线 additive)
# ============================================================

def test_refund_points_routes_by_ledger_type(monkeypatch):
    """🔴 [P1-3] refund_points(ledger_type=) 按【原扣费账本】精确路由,不按【退款时刻】的 _is_v35_customer 猜。
    删本修复(改回无条件 if _is_v35_customer) → ledger_type='v35' 但当前身份 legacy 时不走 v35 → 转红。"""
    import middleware.billing as BILL
    routed = {"v35": 0}

    def _fake_v35_refund(cursor, uid, fc, reason, amount=None, charge_tx_id=None):
        routed["v35"] += 1
        return {"success": True, "v35_customer_credit": True}
    monkeypatch.setattr(BILL, "_refund_v35_customer_credit", _fake_v35_refund)

    # 当前身份 = legacy,但 ledger_type='v35' → 必须强制走 v35 账本
    monkeypatch.setattr(BILL, "_is_v35_customer", lambda cursor, uid: False)
    asyncio.run(BILL.refund_points(U, "article_gen", "t", ledger_type="v35"))
    assert routed["v35"] == 1, "🔴 ledger_type=v35 强制走 v35(即便当前 _is_v35_customer=False)"

    # 当前身份 = v35,但 ledger_type='legacy' → 必须强制走 legacy(不误转 v35)
    monkeypatch.setattr(BILL, "_is_v35_customer", lambda cursor, uid: True)
    routed["v35"] = 0
    asyncio.run(BILL.refund_points(U, "article_gen", "t", ledger_type="legacy"))
    assert routed["v35"] == 0, "🔴 ledger_type=legacy 强制走 legacy(即便当前 _is_v35_customer=True)"

    # ledger_type=None(旧调用)→ 回落 _is_v35_customer(=True)→ 走 v35(向后兼容)
    routed["v35"] = 0
    asyncio.run(BILL.refund_points(U, "article_gen", "t"))
    assert routed["v35"] == 1, "ledger_type=None → 保持原 _is_v35_customer 行为(兼容)"


def test_processor_passes_ledger_type_to_refund(monkeypatch):
    """🔴 [P1-3] fund_recovery_processor 必须把工单登记时钉死的 ledger_type 透传给 refund_points·
    删透传(refund_points 不带 ledger_type)→ captured 为 None → 转红。"""
    import services.fund_recovery_processor as P  # noqa: F401
    captured = {"ledger_type": "SENTINEL"}

    async def _fake_refund(uid, fc, reason="", charge_tx_id=None, ledger_type=None, amount=None):
        captured["ledger_type"] = ledger_type
        return {"success": True}
    monkeypatch.setattr("middleware.billing.refund_points", _fake_refund)
    from db.fund_recovery_db import create_recovery_order
    create_recovery_order("article_gen_thread_window", "refund", ref_key="rk-lt-1", user_id=U,
                          feature_code="article_gen", charge_tx_id=778899, ledger_type="v35")
    asyncio.run(P.process_pending(limit=5))
    assert captured["ledger_type"] == "v35", "🔴 processor 必须透传工单 ledger_type 给 refund_points(否则退错账本转 manual)"


# ============================================================
# P2-4 settle_conflict 正向巡检游标翻页(201 饥饿)
# ============================================================

def test_forward_patrol_drains_beyond_200_settle_conflict():
    """🔴 [P2-4] 正向巡检必须【游标分页排空】所有缺工单的 settle_conflict 任务·
    构造 201 条冲突任务(均无工单),旧固定 LIMIT 200 会漏掉最旧 1 条(永久饥饿无人工入口)。
    删分页(改回 list_settle_conflict_tasks(limit=200) 单页)→ 最旧任务无工单 → 转红。"""
    from db.fund_recovery_db import has_open_workorder
    from services.geo_plan_settlement import ensure_settle_conflict_workorders

    ids = [_mk_task(B + 1000 + i) for i in range(201)]
    with _conn() as c:
        cur = c.cursor()
        cur.execute("UPDATE geo_plan_tasks SET status='settle_conflict' WHERE id = ANY(%s)", (ids,))
        c.commit()

    res = ensure_settle_conflict_workorders()
    assert res["created"] == 201, f"🔴 必须为全部 201 条补建工单(游标排空)· 实际 created={res['created']}"
    # 最旧(最小 id)那条 —— 旧单页 LIMIT 200(id DESC)恰好把它挤出窗口
    assert has_open_workorder("geo_plan_settle", f"geoplan_{min(ids)}") is True, \
        "🔴 第 201 条(最旧)必须也拿到工单 · 不得饥饿"
