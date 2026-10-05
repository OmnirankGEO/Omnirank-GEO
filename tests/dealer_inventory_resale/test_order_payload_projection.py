"""订单类端点字段投影 · 非 admin 路径白名单(2026-07-29 新增要求)

要求原文:
  凡返回 recharge_orders 行或 pricing_snapshot_jsonb 的端点,非 admin 路径必须走
  白名单字段投影,禁止原样返回整个 jsonb。
  判别锁:以服务商身份调用任一订单类端点 → 响应体中不得出现
          fulfillment_plan / hop / acquisition_cost / margin 等键。
  变异:任一端点改成原样返回 jsonb → 转红。

## 这组锁怎么保证有判别力(不是"响应本来就空"的假绿)

三层:
  1. **正向**:同一批端点用 **admin** 身份调,禁键**必须出现** —— 证明数据真的在,
     只是被非 admin 投影挡掉了。admin 那边一片空白的话下面的"没有禁键"就毫无意义。
  2. **载荷非空**:非 admin 响应必须真的带回订单数据(断言业务键在),
     不接受 404/空列表当绿。
  3. **变异**:把 `_serialize_order` 改成非 admin 也返回整行 → 本组必红
     (由 scripts/verify_order_payload_projection_2026_07_29.py 跑)。

全部行为级:真起 FastAPI TestClient、真连 PG16、真调 handler,零源码串断言。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api.dealer_inventory_resale_api import router as resale_router

from .test_resale_funds import _sell_water_to_consumer

# 工单点名的四个键 + 它们在本仓的真实列名/别名。
# 只写 "margin" 一个词是抓不住 margin_cents 的 —— 按**子串**匹配,宁可宽。
FORBIDDEN_KEY_TOKENS = (
    "fulfillment_plan",
    "hop",                  # hops / hop_seq / transfer_entries 里的 hop_*
    "acquisition_cost",     # acquisition_cost_cents
    "margin",               # margin_cents / agent_margin_before_tax_cents
    "pricing_snapshot",     # pricing_snapshot_jsonb 原样回吐
    "settlement_snapshot",
    "seller_cost_basis",
    "cost_basis_cents",
    "downstream_markup_bps",
)


def _walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _walk_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_keys(child)


def _offending_keys(payload):
    return sorted({
        key for key in _walk_keys(payload)
        for token in FORBIDDEN_KEY_TOKENS
        if token in key.lower()
    })


def _client() -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def inject_test_user(request: Request, call_next):
        raw = request.headers.get("x-test-user")
        if raw:
            request.state.user = {
                "user_id": int(raw),
                "is_admin": request.headers.get("x-test-admin") == "1",
            }
        return await call_next(request)

    app.include_router(resale_router)
    return TestClient(app)


#: 服务商(非 admin)能打的订单类端点 → (path, 至少要出现的业务键之一)。
#: 第二项是**载荷非空证明**:响应必须真的带回该端点的业务数据,
#: 否则"没有禁键"只是因为什么都没返回。
PROVIDER_ORDER_ENDPOINTS = (
    ("/api/dealer-resale/orders", {"items", "total"}),
    ("/api/dealer-resale/orders/O-L2-SVC", {"order_id", "sale_amount_cents", "state"}),
    ("/api/dealer-resale/orders/O-L2-SVC/export", {"item", "format", "exported_at"}),
    ("/api/dealer-resale/profits/summary", {"b2b", "consumer", "pending_cents"}),
)


@pytest.fixture
def seeded(db_conn):
    _sell_water_to_consumer(db_conn)
    db_conn.commit()
    return db_conn


@pytest.mark.parametrize("path, expected_keys", PROVIDER_ORDER_ENDPOINTS,
                         ids=[p for p, _ in PROVIDER_ORDER_ENDPOINTS])
def test_provider_order_endpoints_never_expose_cost_or_fulfillment_internals(
    seeded, path, expected_keys
):
    """🔴 以**服务商**身份调订单类端点 → 响应体不得出现成本/毛利/履约链的键。"""
    client = _client()
    response = client.get(path, headers={"x-test-user": "30"})
    assert response.status_code == 200, response.text
    body = response.json()

    # 载荷非空 —— 空响应里"没有禁键"是假绿
    assert body, f"{path} 返回空载荷,本锁失去意义"
    keys = set(_walk_keys(body))
    assert keys & expected_keys, (
        f"{path} 没带回业务数据 · 期望其一 {sorted(expected_keys)} · 实得 {sorted(keys)[:20]}"
    )

    assert _offending_keys(body) == [], (
        f"{path} 以服务商身份泄露了内部字段:{_offending_keys(body)}"
    )


def test_admin_chain_does_expose_them_so_the_lock_is_proven_discriminating(seeded):
    """正向对照:同一批数据在 **admin** 链路上禁键必须出现。

    没有这一条,上面那四个"没有禁键"可能只是因为数据根本没生成。
    """
    client = _client()
    response = client.get(
        "/api/admin/dealer-resale/orders/O-L2-SVC/chain",
        headers={"x-test-user": "1", "x-test-admin": "1"},
    )
    assert response.status_code == 200, response.text
    leaked = _offending_keys(response.json())
    assert leaked, "admin 链路也没有这些键 —— 说明数据没生成,上面的锁是空绿"
    # 至少要覆盖工单点名的其中两类,证明我们盯的就是那批字段
    joined = " ".join(leaked)
    assert "margin" in joined or "cost_basis" in joined
