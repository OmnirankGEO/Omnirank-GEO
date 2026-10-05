from __future__ import annotations

from pathlib import Path

from services import settlement_orchestrator as module


ROOT = Path(__file__).resolve().parents[2]


class Cursor:
    def __init__(self):
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append((statement, params))


def _locked_order(agent_user_id: int = 17, *, binding_source: str = "explicit_binding") -> dict:
    return {
        "id": "ORDER-PRIVACY-1",
        "order_type": "customer_recharge",
        "sku_template_id": 4,
        "agent_user_id": agent_user_id,
        "binding_source": binding_source,
        "amount_cents": 10000,
        "base_points": 13000,
        "bonus_points": 0,
    }


def _patch_base(monkeypatch, binding) -> None:
    from services import dealer_inventory_resale

    monkeypatch.setattr(dealer_inventory_resale, "has_consumer_sale", lambda *_args: False)
    monkeypatch.setattr(module, "is_v35_factory_enabled", lambda cursor: True)
    monkeypatch.setattr(module, "legacy_v32_policy", lambda cursor: "disabled")
    monkeypatch.setattr(module, "get_customer_binding", lambda cursor, user_id: binding)


def _route_with_spies(monkeypatch, binding, *, agent_user_id=17, resolution="BOUND"):
    _patch_base(monkeypatch, binding)
    orchestrator = module.SettlementOrchestrator()
    calls = []
    monkeypatch.setattr(
        orchestrator,
        "_hold_for_dispute",
        lambda *args, **kwargs: calls.append(("hold", None)) or "dispute_hold",
    )
    monkeypatch.setattr(
        orchestrator,
        "_record_factory_settlement",
        lambda cursor, order, user, relationship, snapshot: calls.append(
            ("settled", relationship)
        ),
    )
    is_platform_direct = resolution == "PLATFORM_DIRECT"
    result = orchestrator.route(
        Cursor(),
        _locked_order(
            agent_user_id,
            binding_source="platform_direct" if is_platform_direct else "explicit_binding",
        ),
        {"user_id": 40},
        {
            "commercial_resolution": resolution,
            "commercial_service_source": (
                "platform_direct" if is_platform_direct else "explicit_binding"
            ),
            "amount_source": "sku_snapshot",
        },
    )
    return result, calls


def test_payment_callback_never_creates_or_rebuilds_commercial_relationship():
    source = (ROOT / "db/wallet_db.py").read_text(encoding="utf-8")
    callback = source[source.index("def complete_recharge"):]

    assert "upsert_customer_agent_binding(" not in callback
    assert "INSERT INTO customer_agent_bindings" not in callback
    assert "UPDATE customer_agent_bindings" not in callback


def test_bound_snapshot_with_missing_current_relationship_uses_immutable_principal(monkeypatch):
    result, calls = _route_with_spies(monkeypatch, None)
    assert result == "v35_inventory_settlement"
    assert calls == [(
        "settled",
        {
            "agent_user_id": 17,
            "binding_source": "explicit_binding",
            "immutable_order_snapshot": True,
        },
    )]


def test_valid_bound_snapshot_settles_to_the_existing_principal(monkeypatch):
    relationship = {"agent_user_id": 17, "dispute_status": None}
    result, calls = _route_with_spies(monkeypatch, relationship)
    assert result == "v35_inventory_settlement"
    assert calls == [(
        "settled",
        {
            "agent_user_id": 17,
            "binding_source": "explicit_binding",
            "immutable_order_snapshot": True,
        },
    )]


def test_conflicted_relationship_is_held(monkeypatch):
    result, calls = _route_with_spies(
        monkeypatch, {"agent_user_id": 17, "dispute_status": "pending"},
    )
    assert result == "dispute_hold"
    assert calls == [("hold", None)]


def test_platform_direct_snapshot_keeps_immutable_principal_after_config_change(monkeypatch):
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", "900")
    result, calls = _route_with_spies(
        monkeypatch, None, agent_user_id=901, resolution="PLATFORM_DIRECT",
    )
    assert result == "v35_platform_direct_settlement"
    assert calls == [(
        "settled", {"agent_user_id": 901, "binding_source": "platform_direct"},
    )]


def test_platform_direct_snapshot_settles_without_creating_a_relationship(monkeypatch):
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", "900")
    result, calls = _route_with_spies(
        monkeypatch, None, agent_user_id=900, resolution="PLATFORM_DIRECT",
    )
    assert result == "v35_platform_direct_settlement"
    assert calls == [(
        "settled", {"agent_user_id": 900, "binding_source": "platform_direct"},
    )]


def test_platform_direct_mode_uses_v35_refund_and_legacy_commission_guards():
    wallet = (ROOT / "api/wallet_api.py").read_text(encoding="utf-8")
    referral = (ROOT / "api/referral_api.py").read_text(encoding="utf-8")

    mode_tuple = '("v35_inventory_settlement", "v35_platform_direct_settlement")'
    assert f"if _v35_mode in {mode_tuple}:" in wallet
    assert '"v35_platform_direct_settlement",' in referral
    assert "if _sm in (" in referral
    assert "_handle_v35_factory_refund(" in referral
