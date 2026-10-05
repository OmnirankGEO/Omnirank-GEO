"""[BUG-P1] 预付进货(agent_inventory_prepay)退款退错池 · 回归单测(monkeypatch · 不依赖真 DB)

根因:退款分流只认 settlement_mode='v35_inventory_settlement',agent_inventory_prepay 落 legacy →
对个人 user_wallets 扣减(退错池:钱退了/库存还在 双重得利,或余额不足整体卡死)。
修:① agent_inventory.refund_inventory_prepay 专用库存反向链(扣回未划拨,不碰 user_wallets,
   库存不足标 manual_review);② wallet_api 退款分流加 agent_inventory_prepay 分支。
"""
from pathlib import Path

import pytest


class _C:
    def execute(self, *a, **k):
        pass


def test_refund_inventory_prepay_sufficient(monkeypatch):
    import services.agent_inventory as ai
    monkeypatch.setattr(ai, "get_or_create_inventory_wallet",
                        lambda c, a: {"paid_inventory_points": 144444, "bonus_inventory_points": 7222})
    inserts = []
    monkeypatch.setattr(ai, "_insert_inventory_transaction", lambda *a, **k: inserts.append(a))
    r = ai.refund_inventory_prepay(_C(), agent_user_id=1, paid_points=144444,
                                   bonus_points=7222, related_order_id="AIP1")
    assert r["success"] is True
    assert r["paid_inventory_points"] == 0 and r["bonus_inventory_points"] == 0
    assert r["refunded_paid"] == 144444 and r["refunded_bonus"] == 7222
    # paid + bonus 各一笔 refund_clawback · points 为负(扣回)
    assert len(inserts) == 2
    assert all(a[2] == "refund_clawback" for a in inserts)
    assert all(a[4] < 0 for a in inserts)


def test_refund_inventory_prepay_insufficient_manual_review(monkeypatch):
    # 库存余额 < 进货额(部分已划拨给客户)→ manual_review,不强扣、不碰 user_wallets
    import services.agent_inventory as ai
    monkeypatch.setattr(ai, "get_or_create_inventory_wallet",
                        lambda c, a: {"paid_inventory_points": 50000, "bonus_inventory_points": 0})
    inserts = []
    monkeypatch.setattr(ai, "_insert_inventory_transaction", lambda *a, **k: inserts.append(a))
    r = ai.refund_inventory_prepay(_C(), agent_user_id=1, paid_points=144444, bonus_points=7222)
    assert r["success"] is False and r["manual_review"] is True
    assert r["avail_paid"] == 50000
    assert len(inserts) == 0  # 不写任何扣回流水


def test_refund_inventory_prepay_negative_raises():
    import services.agent_inventory as ai
    with pytest.raises(ValueError):
        ai.refund_inventory_prepay(_C(), agent_user_id=1, paid_points=-1)


def test_wallet_api_routes_prepay_refund_static():
    # 静态守护:退款分流必须有 agent_inventory_prepay 专用分支 + 调库存反向链
    src = Path(__file__).resolve().parents[1].joinpath("api", "wallet_api.py").read_text(encoding="utf-8")
    assert '_v35_mode == "agent_inventory_prepay"' in src, "退款分流缺 agent_inventory_prepay 分支 → 退错池复发"
    assert "refund_inventory_prepay" in src, "须调库存反向链(不碰 user_wallets)"
