"""[v6 req3] fund_recovery_orders 正式化 · CAS claim / 退避 / 终态 / 唯一键含 kind / 处理器 · 行为测试。

判别性:唯一键去掉 kind → test_same_charge_refund_and_state_fix_not_deduped 失败(state_fix 被 refund 吞掉)。
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


def _conn():
    c = psycopg2.connect(DB); c.cursor_factory = psycopg2.extras.RealDictCursor
    return c


@pytest.fixture(autouse=True)
def _clean():
    require_destructive_allowed(); _assert_safe_test_db(DB)
    from db.fund_recovery_db import init_fund_recovery_tables
    with _conn() as c:
        init_fund_recovery_tables(c.cursor()); c.commit()
        c.cursor().execute("DELETE FROM fund_recovery_orders"); c.commit()
    yield
    with _conn() as c:
        c.cursor().execute("DELETE FROM fund_recovery_orders"); c.commit()


def _status(oid):
    with _conn() as c:
        cur = c.cursor(); cur.execute("SELECT status, retry_count FROM fund_recovery_orders WHERE id=%s", (oid,))
        return cur.fetchone()


def test_same_charge_refund_and_state_fix_not_deduped():
    """🔴 同 charge_tx_id 的 refund 与 state_fix 是不同工单 · 唯一键含 kind → 都入库(不互吞)。"""
    from db.fund_recovery_db import create_recovery_order
    r = create_recovery_order("article_gen_thread_window", "refund", ref_key="Q1", charge_tx_id=8001)
    s = create_recovery_order("article_gen_thread_window", "state_fix", ref_key="Q1", charge_tx_id=8001)
    assert r is not None and s is not None and r != s, "refund 与 state_fix 同 charge 不得互吞"
    with _conn() as c:
        cur = c.cursor(); cur.execute("SELECT COUNT(*) AS n FROM fund_recovery_orders WHERE charge_tx_id=8001")
        assert cur.fetchone()["n"] == 2


def test_same_charge_same_kind_dedups():
    from db.fund_recovery_db import create_recovery_order
    a = create_recovery_order("article_gen_thread_window", "refund", ref_key="Q1", charge_tx_id=8002)
    b = create_recovery_order("article_gen_thread_window", "refund", ref_key="Q1", charge_tx_id=8002)
    assert a == b, "同 kind+charge 重复登记应去重返既有 id"


def test_claim_cas_atomic_and_skip():
    from db.fund_recovery_db import create_recovery_order, claim_next_recovery_order
    oid = create_recovery_order("article_gen_thread_window", "refund", ref_key="Q3", charge_tx_id=8003)
    w1 = claim_next_recovery_order("w1")
    assert w1 and w1["id"] == oid and w1["status"] == "processing", "认领 → processing"
    w2 = claim_next_recovery_order("w2")
    assert w2 is None, "已被认领(processing)不再被领 · 无到期 pending"


def test_requeue_backoff_then_manual():
    """[v7 finding3] requeue 现需持 lease(claim→processing→token)· 每轮 claim 再 requeue · 退避超限转 manual。"""
    from db.fund_recovery_db import (create_recovery_order, requeue_recovery_order,
                                     claim_next_recovery_order, MAX_AUTO_ATTEMPTS)
    oid = create_recovery_order("article_gen_thread_window", "refund", ref_key="Q4", charge_tx_id=8004)
    last = 0
    for _ in range(MAX_AUTO_ATTEMPTS):
        # requeue 把 pending→processing 复位回 pending · 需先 claim 拿新 token(把 next_retry_at 拨到已到期以便 claim)
        with _conn() as c:
            c.cursor().execute("UPDATE fund_recovery_orders SET next_retry_at=NOW()-interval '1 s' WHERE id=%s AND status='pending'", (oid,)); c.commit()
        wo = claim_next_recovery_order("wq")
        if not wo:  # 已 manual(claim 只取 pending/stale-processing)
            break
        last = requeue_recovery_order(oid, error="still failing", claim_token=wo["claim_token"])
    assert last >= MAX_AUTO_ATTEMPTS, f"退避应累计到 MAX · last={last}"
    assert _status(oid)["status"] == "manual", "退避超限 → 转 manual 人工"


def test_admin_cannot_resolve_processing_worker_owned():
    """🔴 [v7 finding3] admin 不得结案【worker 处理中(processing)】的工单(防 admin×worker 竞态)。"""
    from db.fund_recovery_db import create_recovery_order, claim_next_recovery_order, resolve_recovery_order, fail_recovery_order
    oid = create_recovery_order("article_gen_thread_window", "refund", ref_key="Qp", charge_tx_id=8010)
    wo = claim_next_recovery_order("w1")
    assert wo and wo["status"] == "processing"
    assert resolve_recovery_order(oid, note="admin try") is False, "🔴 processing 单 admin 不可 resolve"
    assert fail_recovery_order(oid, note="admin try") is False, "🔴 processing 单 admin 不可 fail"
    assert _status(oid)["status"] == "processing", "仍归 worker 持有"


def test_late_worker_requeue_cannot_flip_resolved():
    """🔴 [v7 finding3] 迟到 worker(lease 已被 stale reaper 换新 token)不得把 resolved 翻回 pending。"""
    from db.fund_recovery_db import (create_recovery_order, claim_next_recovery_order,
                                     requeue_recovery_order, resolve_recovery_order_by_worker)
    oid = create_recovery_order("article_gen_thread_window", "refund", ref_key="Qr", charge_tx_id=8011)
    w1 = claim_next_recovery_order("w1")
    token1 = w1["claim_token"]
    # 模拟 w1 崩 + stale reaper 把该 processing 拨老 → w2 重新认领(新 token2)
    with _conn() as c:
        c.cursor().execute("UPDATE fund_recovery_orders SET claimed_at=NOW()-interval '20 min' WHERE id=%s", (oid,)); c.commit()
    w2 = claim_next_recovery_order("w2")
    token2 = w2["claim_token"]
    assert token2 != token1, "重新认领生成新 token(旧 lease 失效)"
    assert resolve_recovery_order_by_worker(oid, token2) is True  # w2 收口成功
    assert _status(oid)["status"] == "resolved"
    # w1 迟到:requeue(旧 token1)必须 no-op(-1)· 不得把 resolved 翻回 pending
    assert requeue_recovery_order(oid, error="late", claim_token=token1) == -1, "🔴 lease 失效 requeue 必须 no-op"
    assert _status(oid)["status"] == "resolved", "🔴 resolved 不得被迟到 worker 翻回 pending"


def test_duplicate_workorder_blocked_while_processing():
    """🔴 [v7 finding3] 唯一键覆盖未终结态:一单 processing 时再建同键工单被去重(防两 worker 各退一次=双退)。"""
    from db.fund_recovery_db import create_recovery_order, claim_next_recovery_order
    oid = create_recovery_order("article_gen_thread_window", "refund", ref_key="Qd", charge_tx_id=8012)
    claim_next_recovery_order("w1")  # → processing
    # processing 期间再建同 (source,ref_key,kind,charge_tx_id) → 被唯一键挡(返既有 id · 不新建)
    dup = create_recovery_order("article_gen_thread_window", "refund", ref_key="Qd", charge_tx_id=8012)
    assert dup == oid, "🔴 processing 期间同键重复登记应去重(返既有 id)"
    with _conn() as c:
        cur = c.cursor(); cur.execute("SELECT COUNT(*) AS n FROM fund_recovery_orders WHERE charge_tx_id=8012")
        assert cur.fetchone()["n"] == 1, "🔴 未终结态唯一键:不得建重复工单"


def test_null_charge_refund_not_deduped():
    """[v7 二轮对抗审订正] NULL charge_tx_id 工单【不去重】(保 v6 行为)—— ref_key=quote_id 非 per-charge 判别键,
    同 quote 两笔不同扣费(v35 空 tx → charge 均 NULL)若被误并会漏退(客户少退款,比 over-refund 更糟)。
    根治需持久化 order-group key 精确退款(超 v7 范围 · 见出口报告已知遗留)。"""
    from db.fund_recovery_db import create_recovery_order
    a = create_recovery_order("article_gen_thread_window", "refund", ref_key="Qnull", charge_tx_id=None)
    b = create_recovery_order("article_gen_thread_window", "refund", ref_key="Qnull", charge_tx_id=None)
    assert a is not None and b is not None and a != b, "null-charge 两笔各自入库(不误并 · 防漏退)"


def test_stale_reclaim_bumps_retry_and_escalates_manual():
    """🔴 [v7 对抗审 P3] 硬崩循环:stale-processing 回收 bump retry_count · 计到 MAX 转 manual(防无限静默死循环无告警)。"""
    from db.fund_recovery_db import create_recovery_order, claim_next_recovery_order, MAX_AUTO_ATTEMPTS
    oid = create_recovery_order("article_gen_thread_window", "refund", ref_key="Qesc", charge_tx_id=8020)
    with _conn() as c:
        c.cursor().execute("UPDATE fund_recovery_orders SET retry_count=%s WHERE id=%s", (MAX_AUTO_ATTEMPTS - 1, oid)); c.commit()
    w1 = claim_next_recovery_order("w1")   # fresh pending→processing(不 bump)· retry 仍 MAX-1
    assert w1 and w1["id"] == oid and w1["status"] == "processing"
    with _conn() as c:  # 模拟 worker 硬崩(SIGKILL)· claimed_at 拨老 → stale
        c.cursor().execute("UPDATE fund_recovery_orders SET claimed_at=NOW()-interval '20 min' WHERE id=%s", (oid,)); c.commit()
    w2 = claim_next_recovery_order("w2")   # stale 回收 → retry MAX-1+1=MAX → 转 manual
    assert w2 is not None and w2["status"] == "manual", "🔴 回收计次触顶必须转 manual(不再 processing 死循环)"
    assert _status(oid)["status"] == "manual" and _status(oid)["retry_count"] >= MAX_AUTO_ATTEMPTS


def test_processor_already_refunded_idempotent_resolves(monkeypatch):
    """🔴 [v7 finding6 · v8 P1-1] 退款成功后未 resolved 就崩溃 → 重试 refund 返"未找到扣费记录",但【对应账本】退款流水证据在
    → 幂等 resolved(不误进 manual/pending)。v8 修:工单须带 ledger_type='legacy',refund_evidence_exists 只查 legacy 账本。"""
    from db.fund_recovery_db import create_recovery_order
    import services.fund_recovery_processor as P
    import middleware.billing as B

    async def _already_refunded(uid, feat, *, reason=None, charge_tx_id=None, ledger_type=None, amount=None):
        return {"success": False, "reason": "未找到扣费记录"}   # 已全额退过 · 与"从未扣费"不可区分
    monkeypatch.setattr(B, "refund_points", _already_refunded)

    CHARGE = 8013
    with _conn() as c:
        cur = c.cursor()
        cur.execute("INSERT INTO users (id, username) VALUES (7,'u7') ON CONFLICT (id) DO NOTHING")
        # 完整证据身份:同 user + feature + ledger 的 consume 与关联 refund。
        cur.execute("DELETE FROM point_transactions WHERE id=%s", (CHARGE,))
        cur.execute(
            "INSERT INTO point_transactions "
            "(id,user_id,type,point_type,amount,balance_after,feature_code) "
            "VALUES (%s,7,'consume','paid',-130,0,'article_gen')",
            (CHARGE,),
        )
        cur.execute("INSERT INTO point_transactions (user_id, type, point_type, amount, balance_after, feature_code, order_id) "
                    "VALUES (7,'refund','paid',130,130,'article_gen',%s)", (str(CHARGE),))
        c.commit()
    oid = create_recovery_order("article_gen_thread_window", "refund", ref_key="Qi",
                                user_id=7, feature_code="article_gen", charge_tx_id=CHARGE, ledger_type="legacy")
    stats = asyncio.run(P.process_pending(limit=10))
    assert _status(oid)["status"] == "resolved", f"🔴 已退款(有流水证据)必须幂等 resolved · stats={stats}"
    assert stats.get("idempotent", 0) >= 1


def test_resolve_terminal():
    from db.fund_recovery_db import create_recovery_order, resolve_recovery_order
    oid = create_recovery_order("article_gen_thread_window", "refund", ref_key="Q5", charge_tx_id=8005)
    assert resolve_recovery_order(oid, note="done") is True
    assert _status(oid)["status"] == "resolved"
    assert resolve_recovery_order(oid) is False, "已终态不可再处置"


def test_processor_refund_success_resolves(monkeypatch):
    from db.fund_recovery_db import create_recovery_order
    import services.fund_recovery_processor as P
    import middleware.billing as B
    async def _ok_refund(uid, feat, *, reason=None, charge_tx_id=None, ledger_type=None, amount=None):
        return {"success": True, "refunded": 130}
    monkeypatch.setattr(B, "refund_points", _ok_refund)
    oid = create_recovery_order("article_gen_thread_window", "refund", ref_key="Q6", user_id=7, feature_code="article_gen", charge_tx_id=8006)
    stats = asyncio.run(P.process_pending(limit=10))
    assert _status(oid)["status"] == "resolved", f"处理器退款成功应 resolved: {stats}"
    assert stats["resolved"] >= 1


def test_processor_refund_fail_requeues(monkeypatch):
    from db.fund_recovery_db import create_recovery_order
    import services.fund_recovery_processor as P
    import middleware.billing as B
    async def _fail_refund(uid, feat, *, reason=None, charge_tx_id=None, ledger_type=None, amount=None):
        return {"success": False, "reason": "billing down"}
    monkeypatch.setattr(B, "refund_points", _fail_refund)
    oid = create_recovery_order("article_gen_thread_window", "refund", ref_key="Q7", user_id=7, feature_code="article_gen", charge_tx_id=8007)
    asyncio.run(P.process_pending(limit=10))
    row = _status(oid)
    assert row["status"] == "pending" and row["retry_count"] == 1, "退款失败 → 退避重排 pending + retry++"


def test_stale_processing_reclaimed():
    """[v6 对抗审 P2] processing 卡死(worker 崩/蓝绿重启)超时 → 被重新认领(不永久卡死)。"""
    from db.fund_recovery_db import create_recovery_order, claim_next_recovery_order
    oid = create_recovery_order("article_gen_thread_window", "refund", ref_key="Qs", charge_tx_id=8009)
    w1 = claim_next_recovery_order("w1")
    assert w1 and w1["id"] == oid and w1["status"] == "processing"
    # 模拟 worker 崩:claimed_at 拨老到超过 STALE_PROCESSING_SEC
    with _conn() as c:
        c.cursor().execute("UPDATE fund_recovery_orders SET claimed_at = NOW() - interval '20 minutes' WHERE id=%s", (oid,)); c.commit()
    w2 = claim_next_recovery_order("w2")
    assert w2 and w2["id"] == oid, "🔴 卡死 processing 超时必须被重新认领(否则退款静默停补偿)"


def test_geo_conflict_workorder_is_manual():
    """GEO 结算冲突工单以 manual 登记(scheduler 不领 · 待人工)。"""
    from db.fund_recovery_db import create_recovery_order, claim_next_recovery_order
    oid = create_recovery_order("geo_plan_settle", "commit", ref_key="geoplan_1", status="manual",
                                reason="conflict", charge_tx_id=None)
    assert _status(oid)["status"] == "manual"
    assert claim_next_recovery_order("w") is None, "manual 工单不被 claim(只领 pending)"
