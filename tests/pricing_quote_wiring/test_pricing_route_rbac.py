import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from auth.module_mapping import resolve_permission
from api import pricing_ssot_api


def _request(**user):
    return SimpleNamespace(state=SimpleNamespace(user=user))


def test_pricing_routes_reach_endpoint_level_authorization():
    assert resolve_permission("/api/pricing/retail/catalog") is None
    assert resolve_permission("/api/pricing/procurement/catalog") is None
    assert resolve_permission("/api/pricing/procurement/quote") is None
    assert resolve_permission("/api/pricing/admin/channel/status") is None


def test_regular_user_is_still_rejected_by_procurement_endpoint(monkeypatch):
    monkeypatch.setattr(pricing_ssot_api, "is_service_provider", lambda _uid: False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            pricing_ssot_api.procurement_catalog(
                _request(user_id=501, is_admin=False, agent_level=0)
            )
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == "仅服务商可使用进货功能"


def test_regular_user_is_still_rejected_by_pricing_admin_endpoint():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            pricing_ssot_api.admin_channel_status(
                _request(user_id=501, is_admin=False, agent_level=0)
            )
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == "需要管理员权限"


def test_agent_can_reach_procurement_catalog(monkeypatch):
    monkeypatch.setattr(pricing_ssot_api, "is_service_provider", lambda _uid: True)
    monkeypatch.setattr(
        pricing_ssot_api,
        "_pricing_flags_or_503",
        lambda: {
            "PRICING_DUAL_SSOT_ENABLED": True,
            "CHANNEL_PRICING_ENABLED": False,
        },
    )
    monkeypatch.setattr(
        pricing_ssot_api.pricing_catalog,
        "get_published_catalog",
        lambda catalog_type, scope_key: {
            "version": {
                "id": 1,
                "version_code": "test-v1",
                "calc_meta_jsonb": {
                    "pricing_config_snapshot": {
                        "wholesale_numer": 100, "wholesale_denom": 130,
                        "agent_purchase_bonus_rate": 0, "bonus_validity_months": 12,
                        "founding": {"cap": 10, "min_first_order_yuan": 500,
                                     "first_order_extra_bonus": 0},
                        "agent_tier_config": {},
                    }
                },
            },
            "items": [
                {
                    "id": 1,
                    "product_code": "pack-130",
                    "final_price_cents": 100,
                    "paid_points": 130,
                    "bonus_points": 0,
                    "source_ref_jsonb": {
                        "kind": "agent_purchase_option", "option_id": "pack-130",
                        "amount_cents": 100,
                        "option": {"option_id": "pack-130", "amount_cents": 100,
                                   "reward_eligible": False},
                    },
                }
            ],
        },
    )
    monkeypatch.setattr(pricing_ssot_api, "_product_names", lambda: {"pack-130": "测试包"})

    result = asyncio.run(
        pricing_ssot_api.procurement_catalog(
            _request(user_id=601, is_admin=False, agent_level=1)
        )
    )

    assert result["success"] is True
    assert result["data"]["catalog_version"] == "test-v1"
    item = result["data"]["items"][0]
    assert {
        "product_code": item["product_code"],
        "display_name": item["display_name"],
        "cash_price_cents": item["cash_price_cents"],
        "paid_inventory_points": item["paid_inventory_points"],
        "bonus_inventory_points": item["bonus_inventory_points"],
        "total_inventory_points": item["total_inventory_points"],
    } == {
        "product_code": "pack-130",
        "display_name": "测试包",
        "cash_price_cents": 100,
        "paid_inventory_points": 130,
        "bonus_inventory_points": 0,
        "total_inventory_points": 130,
    }


def test_procurement_uses_wallet_identity_not_request_claim(monkeypatch):
    monkeypatch.setattr(pricing_ssot_api, "is_service_provider", lambda _uid: False)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            pricing_ssot_api.procurement_catalog(
                _request(user_id=602, is_admin=False, agent_level=9)
            )
        )

    assert exc.value.status_code == 403


def test_public_entry_keeps_published_display_name_after_template_is_hidden():
    entry = {
        "product_code": "svsku_internal_code",
        "final_price_cents": 120,
        "paid_points": 130,
        "bonus_points": 0,
        "source_ref_jsonb": {
            "template_code": "retired-template",
            "display_name": "资金链路测试包",
        },
    }

    public = pricing_ssot_api._public_entry(entry, names={})

    assert public["display_name"] == "资金链路测试包"
    assert public["product_code"] == "svsku_internal_code"


def test_agent_level_reads_authoritative_wallet_row():
    assert pricing_ssot_api.is_service_provider(100) is True
    assert pricing_ssot_api.is_service_provider(400) is False


def test_regular_user_reaches_retail_business_error_not_global_rbac(monkeypatch):
    from services.commercial_service_routing import RelationshipConflict

    monkeypatch.setattr(
        pricing_ssot_api,
        "_pricing_flags_or_503",
        lambda: {"PRICING_DUAL_SSOT_ENABLED": True},
    )
    def relationship_conflict(_uid):
        raise RelationshipConflict("test conflict")

    monkeypatch.setattr(
        pricing_ssot_api, "resolve_commercial_relationship",
        lambda _cur, customer_user_id: relationship_conflict(customer_user_id),
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            pricing_ssot_api.retail_catalog(
                _request(user_id=501, is_admin=False, agent_level=0)
            )
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "ACCOUNT_CONFIGURATION_REVIEW_REQUIRED"
