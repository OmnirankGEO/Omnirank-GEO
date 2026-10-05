"""[V3.5 v7 批2A-2 · 2026-06-08 · 对抗审 wmlgiobzc + Codex r2 补丁] V3.5 客户充值退款 admin 工单单测(mock)

覆盖 wallet_api._v35_admin_refund + admin_complete_refund + _get_recharge_settlement_mode:
- settlement_mode 路由(v35 / NULL→direct)· fail-closed(无订单 404 · DB 异常冒泡 不静默回落)
- _v35_admin_refund 直调 _handle_v35_factory_refund(不走 on_recharge_refund 吞异常包装)
- 【Codex r2 P0】_handle 抛 → raise 500(不 fail-open success)
- 【Codex r2 P0】后置断言 final_status != processed / not platform_reversed → raise 500
- 返回 refund_status 真值 · gate 拒 NOT IN(NULL,rejected,failed)(含 processed · 防重入双 clawback)
- 信号聚合 platform_credit_reversed = reversed_at OR refund_clawback(C/D clawback 不设 reversed_at)
- admin_complete_refund:processed → completed(闭环)· 非 processed / 非 admin 拒
"""
import asyncio
import pytest
from datetime import datetime, timedelta


class _FakeCur:
    def __init__(self, rows):
        self._rows = list(rows)
        self.sqls = []

    def execute(self, sql, params=None):
        self.sqls.append(" ".join(sql.split()))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.commits = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def close(self):
        pass


class _FakeReq:
    def __init__(self, user):
        self.state = type("S", (), {"user": user})()
        self.client = None


def _order_row(refund_status=None, days_ago=1, status="paid"):
    return {
        "id": "o1", "user_id": 28, "amount_cents": 3000,
        "payment_status": status, "paid_at": datetime.utcnow() - timedelta(days=days_ago),
        "refund_status": refund_status,
        # [BUG-P3] _v35_admin_refund 窗口判断改用 order 行内 DB now(SELECT now()::timestamp)
        "db_now": datetime.utcnow(),
    }


def _agg(reversed_=False, clawback=False, manual=False):
    return {"any_reversed": reversed_, "has_clawback": clawback, "any_manual": manual}


def _patch_handle(monkeypatch, sink, raises=False):
    import api.referral_api as ref
    def _fake(oid, refund_reason=None, **kwargs):
        sink.append(oid)
        if raises:
            raise RuntimeError("ledger 冲销失败(模拟)")
    monkeypatch.setattr(ref, "_handle_v35_factory_refund", _fake)


# ============================================================
# _get_recharge_settlement_mode(fail-closed)
# ============================================================

def test_settlement_mode_routing(monkeypatch):
    import api.wallet_api as wa
    import db.connection
    cur = _FakeCur([{"settlement_mode": "v35_inventory_settlement"}])
    monkeypatch.setattr(db.connection, "get_connection", lambda: _FakeConn(cur))
    assert wa._get_recharge_settlement_mode("o1") == "v35_inventory_settlement"


def test_settlement_mode_null_returns_direct(monkeypatch):
    import api.wallet_api as wa
    import db.connection
    cur = _FakeCur([{"settlement_mode": None}])  # 订单存在 · mode NULL → direct
    monkeypatch.setattr(db.connection, "get_connection", lambda: _FakeConn(cur))
    assert wa._get_recharge_settlement_mode("o1") == "direct"


def test_settlement_mode_not_found_raises(monkeypatch):
    """[Codex r2 P1] fail-closed:无订单 raise 404(不静默回落 direct)。"""
    import api.wallet_api as wa
    import db.connection
    cur = _FakeCur([])  # 无订单
    monkeypatch.setattr(db.connection, "get_connection", lambda: _FakeConn(cur))
    with pytest.raises(wa.HTTPException) as ei:
        wa._get_recharge_settlement_mode("missing")
    assert ei.value.status_code == 404


# ============================================================
# _v35_admin_refund
# ============================================================

def test_handle_rechecks_channel_refunding_evidence_inside_ledger_transaction(monkeypatch):
    """RD can arrive after the API pre-check; the ledger transaction must block before mutation."""
    import api.referral_api as ref

    ledger = {
        "id": 91, "agent_user_id": 46, "customer_user_id": 18,
        "agent_settlement_cents": 100, "status": "frozen",
        "settle_at": datetime.now(), "settled_at": None, "reversed_at": None,
        "customer_paid_cents": 114, "factory_cents": 100,
        "gateway_fee_bps": 0, "gateway_fee_cents": 0,
        "settlement_service_fee_bps": 0, "settlement_service_fee_cents": 0,
        "agent_margin_before_tax_cents": 14,
        "tax_rate_bps": 0, "tax_mode": "none", "tax_withholding_cents": 0,
    }
    cur = _FakeCur([
        ledger,
        {"refund_status": "channel_refunding", "refund_completed_at": None},
        None,  # no reviewed payout/channel-refund proof
    ])
    conn = _FakeConn(cur)
    monkeypatch.setattr(ref, "get_connection", lambda: conn)

    with pytest.raises(ValueError, match="渠道退款仍在途中"):
        ref._handle_v35_factory_refund(
            "o-rd-race",
            refund_reason="admin_v35_refund",
        )

    assert conn.commits == 0, "证据复核失败必须零提交"
    assert any("FROM recharge_orders" in sql and "FOR UPDATE" in sql for sql in cur.sqls), \
        "证据复核必须在真实冲销事务内锁订单行"
    assert not any("customer_credit_transactions" in sql for sql in cur.sqls), \
        "channel_refunding 无证据时不得触达额度账本"

def test_returns_real_status_processed(monkeypatch):
    """直调 _handle · 后置断言通过(processed + reversed)· 返回真值 processed。"""
    import api.wallet_api as wa
    import db.connection, db.auth_db
    cur1 = _FakeCur([_order_row()])
    cur2 = _FakeCur([_agg(reversed_=True), {"refund_status": "processed"}])
    conns = [_FakeConn(cur1), _FakeConn(cur2)]
    monkeypatch.setattr(db.connection, "get_connection", lambda: conns.pop(0))
    sink = []
    _patch_handle(monkeypatch, sink)
    monkeypatch.setattr(db.auth_db, "create_audit_log", lambda **k: None)
    req = wa.RefundRequest(order_id="o1", reason="客服核验", service_provider_signoff=True)
    res = asyncio.run(wa._v35_admin_refund(req, {"user_id": 1, "username": "admin"}, None))
    assert res["status"] == "processed"
    assert res["platform_credit_reversed"] is True
    assert sink == ["o1"]  # 调了 _handle
    assert not any("refund_status = 'pending'" in s for s in cur1.sqls)  # 不预占位(P0-2)


def test_signal_clawback_cd(monkeypatch):
    """C/D settled clawback 不设 reversed_at → platform_credit_reversed 仍 True(聚合 reversed OR clawback)。"""
    import api.wallet_api as wa
    import db.connection, db.auth_db
    cur1 = _FakeCur([_order_row()])
    cur2 = _FakeCur([_agg(reversed_=False, clawback=True, manual=True), {"refund_status": "processed"}])
    conns = [_FakeConn(cur1), _FakeConn(cur2)]
    monkeypatch.setattr(db.connection, "get_connection", lambda: conns.pop(0))
    _patch_handle(monkeypatch, [])
    monkeypatch.setattr(db.auth_db, "create_audit_log", lambda **k: None)
    req = wa.RefundRequest(order_id="o1", reason="x", service_provider_signoff=True)
    res = asyncio.run(wa._v35_admin_refund(req, {"user_id": 1, "username": "a"}, None))
    assert res["platform_credit_reversed"] is True
    assert res["manual_review"] is True


def test_raises_when_handle_fails(monkeypatch):
    """[Codex r2 P0] _handle_v35_factory_refund 抛 → raise 500 · 不 fail-open success · 不写 success audit。"""
    import api.wallet_api as wa
    import db.connection, db.auth_db
    cur1 = _FakeCur([_order_row()])
    monkeypatch.setattr(db.connection, "get_connection", lambda: _FakeConn(cur1))
    sink = []
    _patch_handle(monkeypatch, sink, raises=True)
    audited = []
    monkeypatch.setattr(db.auth_db, "create_audit_log", lambda **k: audited.append(k))
    req = wa.RefundRequest(order_id="o1", reason="x", service_provider_signoff=True)
    with pytest.raises(wa.HTTPException) as ei:
        asyncio.run(wa._v35_admin_refund(req, {"user_id": 1, "username": "a"}, None))
    assert ei.value.status_code == 500
    assert sink == ["o1"]       # _handle 被调(然后抛)
    assert len(audited) == 0    # 不写 success audit


def test_raises_when_status_not_processed(monkeypatch):
    """[Codex r2 P0] _handle 正常返但 ledger 没真改(status 非 processed / 未 reversed)→ raise 500。"""
    import api.wallet_api as wa
    import db.connection, db.auth_db
    cur1 = _FakeCur([_order_row()])
    # 聚合全 False + refund_status 仍 NULL(unknown)→ 后置断言失败
    cur2 = _FakeCur([_agg(reversed_=False, clawback=False), {"refund_status": None}])
    conns = [_FakeConn(cur1), _FakeConn(cur2)]
    monkeypatch.setattr(db.connection, "get_connection", lambda: conns.pop(0))
    _patch_handle(monkeypatch, [])
    audited = []
    monkeypatch.setattr(db.auth_db, "create_audit_log", lambda **k: audited.append(k))
    req = wa.RefundRequest(order_id="o1", reason="x", service_provider_signoff=True)
    with pytest.raises(wa.HTTPException) as ei:
        asyncio.run(wa._v35_admin_refund(req, {"user_id": 1, "username": "a"}, None))
    assert ei.value.status_code == 500
    assert len(audited) == 0    # 断言失败不写 success audit


def test_gate_rejects_processed(monkeypatch):
    """[must_fix#2 P0-1] 已 processed gate 拒 → 不重入 _handle(防 C/D 二次 clawback)。"""
    import api.wallet_api as wa
    import db.connection
    cur1 = _FakeCur([_order_row(refund_status="processed")])
    monkeypatch.setattr(db.connection, "get_connection", lambda: _FakeConn(cur1))
    sink = []
    _patch_handle(monkeypatch, sink)
    req = wa.RefundRequest(order_id="o1", reason="x", service_provider_signoff=True)
    with pytest.raises(wa.HTTPException) as ei:
        asyncio.run(wa._v35_admin_refund(req, {"user_id": 1, "username": "a"}, None))
    assert ei.value.status_code == 400
    assert len(sink) == 0   # 不重入


def test_gate_rejects_pending(monkeypatch):
    import api.wallet_api as wa
    import db.connection
    cur1 = _FakeCur([_order_row(refund_status="pending")])
    monkeypatch.setattr(db.connection, "get_connection", lambda: _FakeConn(cur1))
    sink = []
    _patch_handle(monkeypatch, sink)
    req = wa.RefundRequest(order_id="o1", reason="x", service_provider_signoff=True)
    with pytest.raises(wa.HTTPException) as ei:
        asyncio.run(wa._v35_admin_refund(req, {"user_id": 1, "username": "a"}, None))
    assert ei.value.status_code == 400
    assert len(sink) == 0


def test_no_fixed_three_day_window_in_admin_internal_reversal():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "api" / "wallet_api.py").read_text(encoding="utf-8")
    start = source.index("async def _v35_admin_refund")
    end = source.index("\n@router", start)
    block = source[start:end]
    assert "timedelta(days=3)" not in block
    assert "超过 3 天退款窗口" not in block


def test_rejects_not_found(monkeypatch):
    import api.wallet_api as wa
    import db.connection
    cur1 = _FakeCur([])
    monkeypatch.setattr(db.connection, "get_connection", lambda: _FakeConn(cur1))
    sink = []
    _patch_handle(monkeypatch, sink)
    req = wa.RefundRequest(order_id="missing", reason="x", service_provider_signoff=True)
    with pytest.raises(wa.HTTPException) as ei:
        asyncio.run(wa._v35_admin_refund(req, {"user_id": 1, "username": "a"}, None))
    assert ei.value.status_code == 404
    assert len(sink) == 0


# ============================================================
# admin_complete_refund(闭环)
# ============================================================

def test_admin_complete_refund(monkeypatch):
    import api.wallet_api as wa
    import db.connection, db.auth_db
    cur = _FakeCur([{"id": "o1", "refund_status": "processed"}])
    monkeypatch.setattr(db.connection, "get_connection", lambda: _FakeConn(cur))
    monkeypatch.setattr(db.auth_db, "create_audit_log", lambda **k: None)
    req = _FakeReq({"user_id": 1, "username": "admin", "is_admin": True})
    body = wa.RefundCompleteRequest(note="真款已退付")
    res = asyncio.run(wa.admin_complete_refund("o1", body, req))
    assert res["status"] == "completed"
    assert any("refund_status = 'completed'" in s for s in cur.sqls)


def test_admin_complete_rejects_non_processed(monkeypatch):
    import api.wallet_api as wa
    import db.connection
    cur = _FakeCur([{"id": "o1", "refund_status": "pending"}])
    monkeypatch.setattr(db.connection, "get_connection", lambda: _FakeConn(cur))
    req = _FakeReq({"user_id": 1, "username": "admin", "is_admin": True})
    body = wa.RefundCompleteRequest(note="")
    with pytest.raises(wa.HTTPException) as ei:
        asyncio.run(wa.admin_complete_refund("o1", body, req))
    assert ei.value.status_code == 400


def test_admin_complete_rejects_non_admin(monkeypatch):
    import api.wallet_api as wa
    req = _FakeReq({"user_id": 9, "username": "u", "is_admin": False})
    body = wa.RefundCompleteRequest(note="")
    with pytest.raises(wa.HTTPException) as ei:
        asyncio.run(wa.admin_complete_refund("o1", body, req))
    assert ei.value.status_code == 403
