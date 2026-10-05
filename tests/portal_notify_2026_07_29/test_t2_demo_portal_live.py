"""T2 判别锁 —— 演示案例的客户门户直接打开真门户(工单 §T2)。

Owner 拍板:不做脱敏预览版,演示案例的门户就是真门户。
本文件锁的是**行为**,不是源码串:

  锁 A 演示案例点"客户门户" → 拿得到可用入口(即使冻结快照里根本没有 portal_links 行)
  锁 B 入口是内部句柄 —— 客户那串**明文 token 绝不出网**
  锁 C 门户数据走**真 handler 实时读**(子请求真的被发出去、真的带了真实 token)
  锁 D 出站白标脱敏仍然生效:服务商名称/内部角色词/成本/上游身份不出现在响应里
  锁 E 演示身份读门户 → `client_access_tokens` **无新增行**(§验收 3)
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import date, datetime, timedelta

import pytest

from services import demo_access

CASE_ID = "11111111-2222-3333-4444-555555555555"

DEMO_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS admin_demo_cases (
    case_id UUID PRIMARY KEY,
    brand_id INTEGER NOT NULL,
    diagnosis_id INTEGER NOT NULL,
    safe_snapshot JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS admin_demo_case_grants (
    id SERIAL PRIMARY KEY,
    case_id UUID NOT NULL,
    brand_id INTEGER NOT NULL,
    grantee_kind TEXT NOT NULL DEFAULT 'user',
    grantee_user_id INTEGER,
    grantee_organization_id BIGINT,
    capability TEXT NOT NULL DEFAULT 'demo.customer.preview',
    status TEXT NOT NULL DEFAULT 'active',
    valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW() - INTERVAL '1 day',
    expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '7 days'
);
CREATE TABLE IF NOT EXISTS organizations (id BIGSERIAL PRIMARY KEY, status TEXT DEFAULT 'active');
CREATE TABLE IF NOT EXISTS organization_memberships (
    id BIGSERIAL PRIMARY KEY, organization_id BIGINT, user_id INTEGER,
    status TEXT DEFAULT 'active', is_active SMALLINT DEFAULT 1
);
CREATE TABLE IF NOT EXISTS demo_access_events (
    id BIGSERIAL PRIMARY KEY, grant_id INTEGER, case_id UUID, brand_id INTEGER,
    viewer_user_id INTEGER, action TEXT, request_id TEXT, ip_address TEXT,
    blocked_reason TEXT, created_at TIMESTAMPTZ DEFAULT NOW()
);
"""


@pytest.fixture
def demo_case(db, seed):
    db.execute(DEMO_TABLES_SQL)
    db.execute(
        "TRUNCATE admin_demo_cases, admin_demo_case_grants, demo_access_events "
        "RESTART IDENTITY CASCADE"
    )
    ctx = seed(service_days=180, started_days_ago=29, token_expires_in=10)
    # 快照里**故意不写 portal_links** —— 复刻 Owner 报的"入口不可点"场景
    # (token 是快照冻结之后才签发/轮换的)。
    snapshot = {
        "snapshot_contract": "demo-customer-safe-v5",
        "watermark": "演示案例",
        "included": ["customer_overview", "quotes"],
        "customer_overview": {
            "brand_id": ctx["brand_id"], "brand_name": "岱林生物", "industry": "生物医药",
        },
        "quote_snapshots": [{
            "id": ctx["quote_id"], "brand_id": ctx["brand_id"], "brand_name": "岱林生物",
            "status": "paid",
        }],
        "portal_links": [],
    }
    db.execute(
        "INSERT INTO admin_demo_cases (case_id, brand_id, diagnosis_id, safe_snapshot, status) "
        "VALUES (%s, %s, %s, %s, 'active')",
        (CASE_ID, ctx["brand_id"], 1, json.dumps(snapshot, default=str)),
    )
    db.execute("INSERT INTO users (username) VALUES ('demo-viewer') RETURNING id")
    viewer = db.fetchone()["id"]
    db.execute(
        "INSERT INTO admin_demo_case_grants (case_id, brand_id, grantee_kind, grantee_user_id) "
        "VALUES (%s, %s, 'user', %s) RETURNING id",
        (CASE_ID, ctx["brand_id"], viewer),
    )
    grant_id = db.fetchone()["id"]
    # 走真实解析路径拿 context —— 自己 new 一个会因为 expires_at 时区/精度差异
    # 导致 HMAC 素材不一致(句柄派生用到 expires_at),那就变成"夹具跟生产不是一回事"。
    context = demo_access.resolve_demo_case_access(viewer, CASE_ID)
    assert context is not None, "演示授权夹具没建对"
    return {**ctx, "viewer": viewer, "grant_id": grant_id, "context": context}


# ---------------------------------------------------------------------- 锁 A
def test_lockA_portal_entry_resolves_even_without_frozen_portal_links(demo_case):
    """锁 A:冻结快照里没有 portal_links,演示案例的门户入口仍然拿得到。

    这正是 Owner 报"客户门户卡片点不开"的场景:旧口径要求快照里那条指纹逐字匹配,
    token 一轮换/一新签发就永久失效。
    """
    context = demo_case["context"]
    assert context.snapshot.get("portal_links") == []

    link = demo_access.resolve_portal_link(context, quote_id=demo_case["quote_id"])

    assert link is not None, "演示案例的门户入口解析不出来"
    assert link["quote_id"] == demo_case["quote_id"]


def test_lockA2_entry_handle_round_trips(demo_case):
    """句柄能被 resolve 回同一个 case / quote(端到端可用)。"""
    context = demo_case["context"]
    entry = demo_access.demo_portal_entry(context, quote_id=demo_case["quote_id"])
    assert entry and demo_access.is_demo_portal_entry(entry)

    resolved = demo_access.resolve_demo_portal_entry(entry)

    assert resolved is not None, "句柄解析失败 —— 演示门户打不开"
    assert resolved.quote_id == demo_case["quote_id"]
    assert resolved.context.case_id == CASE_ID


# ---------------------------------------------------------------------- 锁 B
def test_lockB_plaintext_customer_token_never_leaves_the_server(demo_case):
    """锁 B:门户凭证面返回的是内部句柄,客户那串明文 token 绝不出现在响应里。"""
    context = demo_case["context"]
    status, payload = demo_access.snapshot_transport_payload(
        context, method="GET", path=f"/api/portal/tokens/{demo_case['quote_id']}",
    )
    body = json.dumps(payload, ensure_ascii=False, default=str)

    assert status == 200
    assert demo_case["token"] not in body, "明文客户 token 泄漏到了演示响应里"
    assert payload["token"]["token"].startswith("D")
    assert demo_access.is_demo_portal_entry(payload["token"]["token"])


# ---------------------------------------------------------------------- 锁 C
def test_lockC_transport_issues_a_real_subrequest_with_the_real_token(demo_case):
    """锁 C:数据源是**真 handler**——子请求真被发出去,且真带了那串真实 token。

    用一个 stub ASGI app 冒充真 app:它把收到的 scope 记下来并返回真实 handler
    形态的 JSON。断言子请求的 path/query/Authorization 都对。
    """
    captured = {}

    async def stub_app(scope, receive, send):
        captured["scope"] = scope
        body = json.dumps({"status": "success", "keywords": [{"keyword": "洁净室"}]}).encode()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})

    status, payload = asyncio.run(demo_access.demo_portal_live_read(
        stub_app,
        path=f"/api/monitoring/clients/{demo_case['quote_id']}/keywords",
        query={"client_id": str(demo_case["quote_id"])},
        portal_token=demo_case["token"],
    ))

    assert status == 200
    assert payload["keywords"][0]["keyword"] == "洁净室", "没有拿到真 handler 的实时数据"
    headers = dict(captured["scope"]["headers"])
    assert headers[b"authorization"] == f"Bearer {demo_case['token']}".encode(), \
        "子请求没带真实门户 token —— 那就不是真门户链路"
    assert captured["scope"]["path"].endswith("/keywords")
    assert b"client_id" in captured["scope"]["query_string"]
    assert captured["scope"]["method"] == "GET", "演示门户子请求必须只读"


def test_lockC2_non_json_upstream_falls_back_instead_of_leaking(demo_case):
    """真 handler 返非 JSON(比如 502 HTML)→ 返 None 让调用方回落快照,不透原文。"""
    async def html_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 502,
                    "headers": [(b"content-type", b"text/html")]})
        await send({"type": "http.response.body", "body": b"<html>502</html>"})

    status, payload = asyncio.run(demo_access.demo_portal_live_read(
        html_app, path="/api/monitoring/trend", query={}, portal_token=demo_case["token"],
    ))
    assert status == 502
    assert payload is None


# ---------------------------------------------------------------------- 锁 D
def test_lockD_whitelabel_and_privacy_scrub_still_applies_to_live_payload():
    """锁 D:真 handler 的 owner 视角 DTO 出网前,服务商/成本/上游身份被剥掉。

    工单 T2 §3:白标脱敏是"仍要守住的唯一一条",演示不是放宽它的理由。
    """
    owner_view = {
        "status": "success",
        "client": {
            "brand_name": "岱林生物",
            "parent_agent_name": "某某网络科技有限公司",   # 上游服务商身份
            "upstream_agent_id": 42,
            "agent_code": "SV-0007",
            "base_cost": 12.5,
            "markup_multiplier": 2.0,
            "portal_token": "A7K2M9XQ4B1C",
            "contact_phone": "13800000000",
            "keywords": [{"keyword": "洁净室", "cost_level": "中", "unit_cost": 3.1}],
        },
    }

    cleaned = demo_access.scrub_demo_payload(owner_view)

    client = cleaned["client"]
    assert client["brand_name"] == "岱林生物"           # 客户自己的信息保留
    assert "parent_agent_name" not in client
    assert "upstream_agent_id" not in client
    assert "agent_code" not in client
    assert "base_cost" not in client
    assert "markup_multiplier" not in client
    assert "portal_token" not in client
    assert "contact_phone" not in client
    assert "unit_cost" not in client["keywords"][0]
    assert client["keywords"][0]["cost_level"] == "中"   # 定性档位是允许的
    assert "A7K2M9XQ4B1C" not in json.dumps(cleaned, ensure_ascii=False)


# ---------------------------------------------------------------------- 锁 E
def test_lockE_demo_read_mints_no_new_portal_token_row(db, demo_case):
    """锁 E(§验收 3):演示身份把门户读一圈,client_access_tokens 一行都不许新增。"""
    db.execute("SELECT id, token, expires_at, is_active FROM client_access_tokens ORDER BY id")
    before = [dict(r) for r in db.fetchall()]

    context = demo_case["context"]
    entry = demo_access.demo_portal_entry(context, quote_id=demo_case["quote_id"])
    demo_access.resolve_demo_portal_entry(entry)
    demo_access.resolve_portal_link(context, quote_id=demo_case["quote_id"])
    demo_access.snapshot_transport_payload(
        context, method="GET", path=f"/api/portal/tokens/{demo_case['quote_id']}",
    )

    db.execute("SELECT id, token, expires_at, is_active FROM client_access_tokens ORDER BY id")
    after = [dict(r) for r in db.fetchall()]
    assert after == before, "演示读门户签发/改动了客户门户凭证"


def test_lockE2_cross_brand_quote_is_refused(db, demo_case):
    """授权边界没放宽:不属于本演示品牌的 quote 一律解析不出入口。"""
    db.execute("INSERT INTO brands (name, owner_user_id) VALUES ('别家品牌', 999) RETURNING id")
    other_brand = db.fetchone()["id"]
    db.execute(
        "INSERT INTO quotes (brand_id, status, paid_at, service_start_date, service_days) "
        "VALUES (%s, 'paid', NOW(), CURRENT_DATE, 180) RETURNING id",
        (other_brand,),
    )
    other_quote = db.fetchone()["id"]
    db.execute(
        "INSERT INTO client_access_tokens (quote_id, brand_name, token, is_active, expires_at) "
        "VALUES (%s, '别家品牌', 'OTHERTOKEN1', 1, %s)",
        (other_quote, date.today() + timedelta(days=30)),
    )

    assert demo_access.resolve_portal_link(demo_case["context"], quote_id=other_quote) is None
