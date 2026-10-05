import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException, Request


ROOT = Path(__file__).resolve().parents[1]


def test_owned_lookup_does_not_fallback_to_phone_like_username():
    from api.agent_workbench_api import _shape_agent_customer_lookup_row

    projected = _shape_agent_customer_lookup_row({
        "customer_user_id": 42,
        "display_name": "13800138000",
        "brand_name": "栖舍设计",
        "phone": "13800138000",
        "binding_status": "owned",
        "tool_credit_points": 1,
        "publish_credit_points": 2,
        "bonus_credit_points": 3,
    })

    assert projected["display_name"] == "客户 42"
    assert projected["brand_name"] == "栖舍设计"
    assert projected["phone_masked"] == "138****8000"


def test_lookup_user_id_parser_rejects_pg_int_overflow():
    from api.agent_workbench_api import _parse_lookup_user_id

    assert _parse_lookup_user_id("123") == 123
    assert _parse_lookup_user_id("2147483647") == 2147483647
    assert _parse_lookup_user_id("2147483648") is None
    assert _parse_lookup_user_id("13800138000123456789") is None
    assert _parse_lookup_user_id("abc") is None


def test_inventory_dialog_resolves_customer_before_allocate_or_revoke():
    src = (ROOT / "frontend/src/pages/Agent/InventoryCenter.tsx").read_text(encoding="utf-8")

    assert "lookupCustomers" in src
    assert "输入手机号 / 客户名 / 客户ID 搜索" in src
    assert "请先搜索并选择客户" in src
    assert "parseInt(customer)" not in src
    assert "客户编号" not in src


def test_agent_customer_lookup_api_is_owned_only_and_masked():
    api = (ROOT / "api/agent_workbench_api.py").read_text(encoding="utf-8")
    schema = (ROOT / "schemas/v35_w2_dto.py").read_text(encoding="utf-8")
    client = (ROOT / "frontend/src/lib/v35w2Api.ts").read_text(encoding="utf-8")

    assert '@router.get("/customers/lookup"' in api
    assert "AgentCustomerLookupResponse" in schema
    assert "_require_agent(request)" in api
    assert "_mask_phone" in api
    assert "phone_masked" in api
    assert "cab.agent_user_id = %s" in api
    assert "cab.customer_user_id IS NULL" not in api
    assert "'unbound' AS binding_status" not in api
    allocation = api.split("async def agent_allocate_offline", 1)[1].split(
        "async def agent_revoke_offline", 1
    )[0]
    assert "upsert_customer_agent_binding" not in allocation
    assert 'binding_source="admin_manual"' not in allocation
    assert "_require_customer_owned_by_agent" in allocation
    assert "COMMERCIAL_BINDING_REQUIRED" in api
    assert "NULLIF(u.username" not in api
    assert "owned_only" in api
    assert "lookupCustomers" in client


def test_unbound_offline_allocation_returns_409_before_any_inventory_write(monkeypatch):
    import api.agent_workbench_api as workbench
    from schemas.v35_w2_dto import AllocateOfflineRequest

    writes = []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return object()

    monkeypatch.setattr(workbench, "_require_agent", lambda _request: {"user_id": 28})
    monkeypatch.setattr(workbench, "get_db", lambda: FakeConnection())
    def reject_unbound(*_args):
        raise HTTPException(
            status_code=409,
            detail={"code": "COMMERCIAL_BINDING_REQUIRED", "message": "not bound"},
        )

    monkeypatch.setattr(workbench, "_require_customer_owned_by_agent", reject_unbound)
    monkeypatch.setattr(
        "services.agent_inventory.allocate_offline",
        lambda *_args, **_kwargs: writes.append("inventory"),
    )

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    payload = AllocateOfflineRequest(customer_user_id=615, tool_points=100)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(workbench.agent_allocate_offline(payload, request))

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "COMMERCIAL_BINDING_REQUIRED"
    assert writes == []
