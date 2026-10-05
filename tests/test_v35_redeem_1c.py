"""[V3.5 v7 批1C · 2026-06-08] 利润换算力 + 防双花 SSOT 单测(mock cursor·不依赖真 DB)

覆盖:
- calc_redeem_points floor(平台不亏·与 prepay 同口径·非 directive 的 ceil)
- redeem available 不足 raise
- redeem 正常 FIFO 锁 + 加 paid_inventory + type=purchase_from_commission
- redeem race 锁不足 raise(rollback)
- get_agent_balance available 减 redeem_locked(防双花核心)

真 DB 集成(模拟服务商真兑换 + paid_inventory 真加)由 Deploy 在 staging 验证。
"""
import pytest


# ---------- FakeCursor for redeem ----------

class _RedeemCursor:
    def __init__(self, ledger_rows):
        self._ledger_rows = ledger_rows
        self._mode = None
        self.inserted_items = []
        self.inventory_updated = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "INSERT INTO agent_commission_redemption_requests" in s:
            self._mode = "new_req"
        elif "agent_commission_redemption_items" in s and "INSERT" in s:
            self.inserted_items.append(params)
            self._mode = None
        elif "FROM agent_revenue_ledger" in s and "FOR UPDATE" in s:
            self._mode = "ledger"
        elif "UPDATE agent_inventory_wallets" in s:
            self.inventory_updated = params
            self._mode = None
        else:
            self._mode = None

    def fetchone(self):
        if self._mode == "new_req":
            return {"id": 777}
        return None

    def fetchall(self):
        if self._mode == "ledger":
            return self._ledger_rows
        return []


def _patch_redeem_deps(monkeypatch, available_cents, numer=9500, denom=13000,
                       wallet_paid=500, tx_capture=None):
    import services.agent_revenue as ar
    import services.agent_pricing as ap
    import services.agent_inventory as ai
    monkeypatch.setattr(ar, "get_agent_balance", lambda c, a: {"available_cents": available_cents})
    monkeypatch.setattr(ap, "get_agent_wholesale_ratio", lambda a: (numer, denom))
    monkeypatch.setattr(ai, "get_or_create_inventory_wallet",
                        lambda c, a: {"paid_inventory_points": wallet_paid, "bonus_inventory_points": 0})

    def _fake_tx(c, a, t, pool, pts, np, nb, description=None):
        if tx_capture is not None:
            tx_capture.update(type=t, pool=pool, points=pts, new_paid=np, desc=description)
    monkeypatch.setattr(ai, "_insert_inventory_transaction", _fake_tx)


# ---------- calc_redeem_points (纯函数) ----------

def test_redeem_points_floor_not_ceil():
    from services.agent_commission_redeem import calc_redeem_points
    # 95 折 9500/13000:¥1.05=105 → 105*13000//9500 = 143(floor · directive 写 144 是 ceil 会让平台微亏)
    assert calc_redeem_points(105, 9500, 13000) == 143
    # 9 折 225/325:¥1=100 → 100*325//225 = 144
    assert calc_redeem_points(100, 225, 325) == 144
    assert calc_redeem_points(0, 9500, 13000) == 0


def test_redeem_points_matches_prepay_floor():
    """redeem 换算力 == prepay 进货同口径(cents × denom // numer)。"""
    from services.agent_commission_redeem import calc_redeem_points
    cents, numer, denom = 12345, 9500, 13000
    assert calc_redeem_points(cents, numer, denom) == cents * denom // numer


# ---------- redeem_commission_to_inventory ----------

def test_redeem_insufficient_available_raises(monkeypatch):
    from services.agent_commission_redeem import redeem_commission_to_inventory
    _patch_redeem_deps(monkeypatch, available_cents=50)
    with pytest.raises(ValueError, match="不足"):
        redeem_commission_to_inventory(_RedeemCursor([]), 28, 100)  # 要 100 > 可用 50


def test_redeem_success_fifo_and_inventory(monkeypatch):
    from services.agent_commission_redeem import redeem_commission_to_inventory
    tx = {}
    _patch_redeem_deps(monkeypatch, available_cents=1000, wallet_paid=500, tx_capture=tx)
    # 一笔 settled ledger 200 cents 无锁
    cur = _RedeemCursor([{"id": 10, "agent_settlement_cents": 200, "already_locked": 0}])
    result = redeem_commission_to_inventory(cur, 28, 105)  # ¥1.05
    assert result["inventory_points_granted"] == 143          # floor
    assert result["redeem_cents"] == 105
    assert result["new_paid_inventory_points"] == 500 + 143
    assert result["locked_items"] == [{"ledger_id": 10, "locked": 105}]
    assert result["redemption_id"] == 777
    # 库存流水 type 正确 + 加对算力
    assert tx["type"] == "purchase_from_commission"
    assert tx["pool"] == "paid"
    assert tx["points"] == 143
    assert tx["new_paid"] == 500 + 143
    assert cur.inventory_updated[0] == 500 + 143               # UPDATE 新 paid_inventory


def test_redeem_fifo_spans_multiple_ledgers(monkeypatch):
    """跨多笔 settled ledger FIFO 锁定(部分锁)。"""
    from services.agent_commission_redeem import redeem_commission_to_inventory
    _patch_redeem_deps(monkeypatch, available_cents=1000)
    cur = _RedeemCursor([
        {"id": 1, "agent_settlement_cents": 60, "already_locked": 0},
        {"id": 2, "agent_settlement_cents": 100, "already_locked": 0},
    ])
    result = redeem_commission_to_inventory(cur, 28, 105)  # 需 105 = 60(ledger1) + 45(ledger2)
    assert result["locked_items"] == [{"ledger_id": 1, "locked": 60}, {"ledger_id": 2, "locked": 45}]


def test_redeem_race_lock_insufficient_raises(monkeypatch):
    """available 够(get_agent_balance)但 ledger 实际可锁不足(并发 race)→ raise rollback。"""
    from services.agent_commission_redeem import redeem_commission_to_inventory
    _patch_redeem_deps(monkeypatch, available_cents=1000)
    # ledger 全被锁(already_locked == 总额)→ 无可锁
    cur = _RedeemCursor([{"id": 10, "agent_settlement_cents": 200, "already_locked": 200}])
    with pytest.raises(ValueError, match="race"):
        redeem_commission_to_inventory(cur, 28, 105)


def test_redeem_zero_raises(monkeypatch):
    from services.agent_commission_redeem import redeem_commission_to_inventory
    _patch_redeem_deps(monkeypatch, available_cents=1000)
    with pytest.raises(ValueError, match="> 0"):
        redeem_commission_to_inventory(_RedeemCursor([]), 28, 0)


# ---------- get_agent_balance 防双花(available 减 redeem_locked) ----------

def test_get_agent_balance_subtracts_redeem_locked():
    from services.agent_revenue import get_agent_balance

    class _BalCursor:
        def __init__(self):
            self._mode = None

        def execute(self, sql, params=None):
            s = " ".join(sql.split())
            if "agent_commission_redemption_items" in s:
                self._mode = "redeem"
            elif "agent_settlement_request_items" in s:
                self._mode = "withdraw"
            elif "agent_revenue_ledger" in s:
                self._mode = "ledger"
            else:
                self._mode = None

        def fetchone(self):
            if self._mode == "ledger":
                return {"frozen": 0, "settled_total": 1000}
            if self._mode == "withdraw":
                return {"pending_locked": 100, "paid_locked": 200}
            if self._mode == "redeem":
                return {"redeem_locked": 300}
            return None

    bal = get_agent_balance(_BalCursor(), 28)
    assert bal["settled_total_cents"] == 1000
    assert bal["pending_payout_cents"] == 100
    assert bal["paid_cents"] == 200
    assert bal["redeemed_cents"] == 300
    # 防双花核心:available = settled − 提现锁 − 已打款 − 换算力锁
    assert bal["available_cents"] == 1000 - 100 - 200 - 300  # 400
