"""Platform suggested-price repair routes are permanently disabled for retail SSOT."""

import pytest
from fastapi import HTTPException

import api.admin_factory_api as admin_api
import api.agent_workbench_api as agent_api


def test_sync_request_defaults_remain_parseable_for_old_clients():
    req = admin_api.AdminSyncAgentPricesRequest()
    assert req.dry_run is True
    assert req.only_loss is True
    assert req.agent_user_id is None


@pytest.mark.asyncio
async def test_admin_platform_suggested_price_sync_is_410_and_db_free(monkeypatch):
    monkeypatch.setattr(
        admin_api,
        "_require_admin",
        lambda request: {"user_id": 1, "is_admin": True},
    )
    monkeypatch.setattr(
        admin_api,
        "get_db",
        lambda: (_ for _ in ()).throw(AssertionError("disabled route must not open DB")),
    )
    with pytest.raises(HTTPException) as stopped:
        await admin_api.admin_pricing_sync_agent_prices(
            admin_api.AdminSyncAgentPricesRequest(dry_run=False),
            request=None,
        )
    assert stopped.value.status_code == 410
    assert stopped.value.detail["code"] == "PLATFORM_SUGGESTED_PRICE_SYNC_DISABLED"


@pytest.mark.asyncio
async def test_provider_restore_suggested_price_is_410_and_db_free(monkeypatch):
    monkeypatch.setattr(
        agent_api,
        "_require_agent",
        lambda request: {"user_id": 7, "agent_level": 1, "is_admin": False},
    )
    monkeypatch.setattr(
        agent_api,
        "get_db",
        lambda: (_ for _ in ()).throw(AssertionError("disabled route must not open DB")),
    )
    with pytest.raises(HTTPException) as stopped:
        await agent_api.agent_pricing_sku_restore_suggested(
            11,
            request=None,
            version=1,
        )
    assert stopped.value.status_code == 410
    assert stopped.value.detail["code"] == "PLATFORM_SUGGESTED_PRICE_DISABLED"
