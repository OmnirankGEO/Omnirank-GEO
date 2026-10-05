from __future__ import annotations

import re

from services import dealer_inventory_resale
from services import settlement_orchestrator as module
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class Cursor:
    def __init__(self):
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append((statement, params))


def _locked_order(agent_user_id: int = 17):
    return {
        "id": "ORDER-PRIVACY-1",
        "order_type": "customer_recharge",
        "sku_template_id": 4,
        "agent_user_id": agent_user_id,
        "amount_cents": 10000,
        "base_points": 13000,
        "bonus_points": 0,
    }


def _patch_base(monkeypatch, binding):
    monkeypatch.setattr(module, "is_v35_factory_enabled", lambda cursor: True)
    monkeypatch.setattr(module, "legacy_v32_policy", lambda cursor: "disabled")
    monkeypatch.setattr(module, "get_customer_binding", lambda cursor, user_id: binding)
    monkeypatch.setattr(
        dealer_inventory_resale,
        "has_consumer_sale",
        lambda cursor, order_id: False,
    )


def test_bound_snapshot_with_missing_binding_settles_without_recreating_binding(monkeypatch):
    _patch_base(monkeypatch, None)
    orchestrator = module.SettlementOrchestrator()
    calls = []
    monkeypatch.setattr(
        orchestrator,
        "_hold_for_dispute",
        lambda *args, **kwargs: calls.append("hold") or "dispute_hold",
    )
    monkeypatch.setattr(
        orchestrator,
        "_record_factory_settlement",
        lambda *args, **kwargs: calls.append("settled"),
    )

    result = orchestrator.route(
        Cursor(),
        _locked_order(),
        {"user_id": 40},
        {"commercial_resolution": "BOUND", "amount_source": "sku_snapshot"},
    )

    assert result == "v35_inventory_settlement"
    assert calls == ["settled"]


def test_disputed_or_dirty_binding_is_held(monkeypatch):
    binding = {"agent_user_id": 17, "dispute_status": "pending"}
    _patch_base(monkeypatch, binding)
    orchestrator = module.SettlementOrchestrator()
    calls = []
    monkeypatch.setattr(
        orchestrator,
        "_hold_for_dispute",
        lambda *args, **kwargs: calls.append("hold") or "dispute_hold",
    )
    monkeypatch.setattr(
        orchestrator,
        "_record_factory_settlement",
        lambda *args, **kwargs: calls.append("settled"),
    )

    result = orchestrator.route(
        Cursor(),
        _locked_order(),
        {"user_id": 40},
        {"commercial_resolution": "BOUND", "amount_source": "sku_snapshot"},
    )

    assert result == "dispute_hold"
    assert calls == ["hold"]


def test_platform_direct_snapshot_without_immutable_route_markers_is_held(monkeypatch):
    _patch_base(monkeypatch, None)
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", "900")
    orchestrator = module.SettlementOrchestrator()
    calls = []
    monkeypatch.setattr(
        orchestrator,
        "_hold_for_dispute",
        lambda *args, **kwargs: calls.append("hold") or "dispute_hold",
    )
    monkeypatch.setattr(
        orchestrator,
        "_record_factory_settlement",
        lambda *args, **kwargs: calls.append("settled"),
    )

    result = orchestrator.route(
        Cursor(),
        _locked_order(agent_user_id=901),
        {"user_id": 40},
        {"commercial_resolution": "PLATFORM_DIRECT", "amount_source": "sku_snapshot"},
    )

    assert result == "dispute_hold"
    assert calls == ["hold"]


def test_platform_direct_snapshot_settles_to_immutable_order_account(monkeypatch):
    _patch_base(monkeypatch, None)
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", "900")
    orchestrator = module.SettlementOrchestrator()
    settlements = []
    monkeypatch.setattr(
        orchestrator,
        "_record_factory_settlement",
        lambda cursor, order, user, binding, snapshot: settlements.append(binding),
    )
    monkeypatch.setattr(
        orchestrator,
        "_hold_for_dispute",
        lambda *args, **kwargs: "dispute_hold",
    )

    order = _locked_order(agent_user_id=900)
    order["binding_source"] = "platform_direct"
    result = orchestrator.route(
        Cursor(),
        order,
        {"user_id": 40},
        {
            "commercial_resolution": "PLATFORM_DIRECT",
            "commercial_service_source": "platform_direct",
            "amount_source": "sku_snapshot",
        },
    )

    assert result == "v35_platform_direct_settlement"
    assert settlements == [{"agent_user_id": 900, "binding_source": "platform_direct"}]


def test_payment_callback_never_creates_commercial_binding():
    source = (ROOT / "db/wallet_db.py").read_text(encoding="utf-8")
    start = source.index("def complete_recharge")
    block = source[start:]

    assert "upsert_customer_agent_binding(" not in block
    assert "Commercial relationships are administrator-managed facts" in block


def test_platform_direct_mode_uses_v35_refund_and_commission_guards():
    referral = (ROOT / "api/referral_api.py").read_text(encoding="utf-8")
    wallet = (ROOT / "api/wallet_api.py").read_text(encoding="utf-8")

    assert '"v35_inventory_settlement"' in referral
    assert '"v35_platform_direct_settlement"' in referral
    assert re.search(
        r"_sm\s+in\s+\(\s*['\"]v35_inventory_settlement['\"]\s*,\s*"
        r"['\"]v35_platform_direct_settlement['\"]\s*,?\s*\)",
        referral,
    )
    assert re.search(
        r"_v35_mode\s+in\s+\(\s*['\"]v35_inventory_settlement['\"]\s*,\s*"
        r"['\"]v35_platform_direct_settlement['\"]\s*,?\s*\)",
        wallet,
    )
    assert '_v35_mode == "v35_platform_direct_settlement"' in wallet
    assert 'create_refund_work_order(payload, user_id, status="submitted")' in wallet
    assert 'if _v35_mode == "dealer_consumer_resale"' in wallet
