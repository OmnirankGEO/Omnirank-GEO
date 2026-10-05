from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from api import xiaobang_api
from services import customer_operation_plan, gap_assistant
from services.organization_contract import IdentityContext


USER = {
    "id": 17,
    "user_id": 17,
    "username": "assistant-test",
    "is_admin": True,
    "permissions": ["diagnosis:read", "quote:read", "writing:read", "monitoring:read"],
    "client_brand_ids": [],
}


def _app(user: dict | None = None, identity=None) -> FastAPI:
    app = FastAPI()

    class InjectUser(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            request.state.user = dict(user or USER)
            request.state.organization_identity = identity
            return await call_next(request)

    app.add_middleware(InjectUser)
    app.include_router(xiaobang_api.router)
    return app


def _events(response) -> dict[str, list[dict]]:
    events: dict[str, list[dict]] = {}
    current = "message"
    for line in response.text.splitlines():
        if line.startswith("event:"):
            current = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            events.setdefault(current, []).append(json.loads(line.split(":", 1)[1].strip()))
    return events


def _assert_signed_action(events: dict[str, list[dict]]):
    from services import gap_operation_map as omap

    meta = events["meta"][-1]
    assistant = meta["gap_assistant"]
    # [vNext 2026-08-18] 注册表升到 v3。这里不再钉死一个字面量:钉字面量正是
    # 前端 §4.1-7 那条债务的同构形态(一升版就整体失配)。改成「必须等于当前
    # 服务端版本」+「该版本必须在已声明的兼容表里」—— 后者才是真正的合同。
    assert assistant["operation_map_version"] == omap.OPERATION_REGISTRY_VERSION
    assert assistant["operation_map_version"] in omap.OPERATION_REGISTRY_COMPATIBLE_VERSIONS
    assert assistant["actions"]
    for action in assistant["actions"]:
        assert action["operation_id"]
        assert action["registry_version"] == omap.OPERATION_REGISTRY_VERSION
        assert action["target_route"].startswith("/")
        assert "evil.example" not in action["target_route"]


def test_diagnosis_request_is_deterministic_and_never_soft_refuses(monkeypatch):
    monkeypatch.setattr(gap_assistant, "_record", lambda *args, **kwargs: None)
    response = TestClient(_app()).post("/api/xiaobang/chat", json={
        "message": "帮我给我的品牌做一次AI搜索诊断",
        "session_id": "direct",
        "current_page": "/dashboard",
    })
    assert response.status_code == 200
    events = _events(response)
    _assert_signed_action(events)
    action = events["meta"][-1]["gap_assistant"]["actions"][0]
    assert action["operation_id"] == "diagnosis_new"
    assert action["target_route"] == "/diagnosis/new"
    text = "".join(item.get("delta", "") for item in events["text"])
    assert "答不了" not in text


def test_cross_tenant_hint_is_rejected_before_sse_without_identifier_leak(monkeypatch):
    def deny(*args, **kwargs):
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="资源不存在")

    monkeypatch.setattr(customer_operation_plan, "require_brand_access", deny)
    response = TestClient(_app()).post("/api/xiaobang/chat", json={
        "message": "今天先做什么",
        "session_id": "cross-tenant",
        "current_page": "/writing",
        "context_refs": {"brand_id": 999},
    })
    assert response.status_code == 404
    assert "999" not in response.text
    assert "品牌" not in response.text


def test_preset_path_keeps_structured_actions(monkeypatch):
    monkeypatch.setattr(gap_assistant, "_record", lambda *args, **kwargs: None)
    response = TestClient(_app()).post("/api/xiaobang/chat", json={
        "message": "你是谁", "session_id": "preset", "current_page": "",
    })
    events = _events(response)
    assert events["meta"][-1]["sources"][0]["source_type"] == "preset"
    _assert_signed_action(events)


def test_current_page_question_uses_the_current_registered_page(monkeypatch):
    monkeypatch.setattr(gap_assistant, "_record", lambda *args, **kwargs: None)

    async def model_must_not_run(*args, **kwargs):
        raise AssertionError("current-page navigation must remain deterministic")

    monkeypatch.setattr(xiaobang_api, "stream_llm", model_must_not_run)
    response = TestClient(_app()).post("/api/xiaobang/chat", json={
        "message": "这个页面怎么用",
        "session_id": "current-page",
        "current_page": "/writing",
    })
    events = _events(response)
    _assert_signed_action(events)
    action = events["meta"][-1]["gap_assistant"]["actions"][0]
    assert action["operation_id"] == "writing_center"
    assert action["target_route"] == "/writing"


def _install_rag_path(monkeypatch, *, succeeded: bool, model_text: str):
    monkeypatch.setattr(gap_assistant, "_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(xiaobang_api, "bm25_search", lambda *args, **kwargs: [({
        "source_slug": "doc_writing",
        "source_title": "现役创作说明",
        "section_title": "覆盖长尾问题",
        "source_type": "doc",
        "content": "按当前客户的词包和监测短板决定补写内容。",
        "route": "/writing",
        "token_keywords": ["客户", "长尾词", "覆盖"],
    }, 8.0)])
    monkeypatch.setattr(xiaobang_api, "_maybe_clarify", lambda *args, **kwargs: None)
    monkeypatch.setattr(xiaobang_api, "_inject_route_page_card", lambda *args, **kwargs: None)

    import tools.xiaobang_embed as embed

    async def no_embedding(*args, **kwargs):
        return None

    async def fake_stream(*args, **kwargs):
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put(model_text)
        await queue.put(None)
        return queue, {"succeeded": succeeded}

    monkeypatch.setattr(embed, "embed_one", no_embedding)
    monkeypatch.setattr(xiaobang_api, "stream_llm", fake_stream)


def test_rag_and_model_success_keep_server_actions(monkeypatch):
    _install_rag_path(monkeypatch, succeeded=True, model_text="先看现役词包，再补覆盖短板。")
    response = TestClient(_app()).post("/api/xiaobang/chat", json={
        "message": "如何处理长尾词覆盖短板",
        "session_id": "rag-ok", "current_page": "/writing",
    })
    events = _events(response)
    assert "先看现役词包" in "".join(i.get("delta", "") for i in events["text"])
    _assert_signed_action(events)


def test_member_agent_level_zero_uses_authoritative_identity_for_rag(monkeypatch):
    monkeypatch.setattr(gap_assistant, "_record", lambda *args, **kwargs: None)
    member = IdentityContext(
        request_id="member-rag", authenticated_user_id=73, principal_user_id=17,
        payer_user_id=17, actor_kind="member", organization_id=9,
        actor_user_id=73, membership_id=5,
        capabilities=frozenset({"clients.read_assigned", "quote.read_own"}),
    )
    member_user = {
        **USER,
        "id": 73,
        "user_id": 73,
        "is_admin": False,
        "permissions": [],
    }
    observed = {}

    def member_rag(*args, **kwargs):
        observed.update(kwargs)
        return [({
            "source_slug": "member_quote_help",
            "source_title": "员工报价说明",
            "section_title": "交付利润口径",
            "source_type": "doc",
            "content": "按当前报价和交付范围解释，不读取固定价格。",
            "route": "/pricing",
            "token_keywords": ["报价", "利润"],
        }, 8.0)]

    monkeypatch.setattr(xiaobang_api, "bm25_search", member_rag)
    monkeypatch.setattr(xiaobang_api, "_maybe_clarify", lambda *args, **kwargs: None)
    monkeypatch.setattr(xiaobang_api, "_inject_route_page_card", lambda *args, **kwargs: None)
    import tools.xiaobang_embed as embed

    async def no_embedding(*args, **kwargs):
        return None

    async def fake_stream(*args, **kwargs):
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put("按当前报价与交付范围核对利润口径。")
        await queue.put(None)
        return queue, {"succeeded": True}

    monkeypatch.setattr(embed, "embed_one", no_embedding)
    monkeypatch.setattr(xiaobang_api, "stream_llm", fake_stream)
    response = TestClient(_app(member_user, member)).post("/api/xiaobang/chat", json={
        "message": "服务方利润应该怎么结合交付来理解",
        "session_id": "member-rag",
        "current_page": "/pricing",
    })
    events = _events(response)
    text = "".join(item.get("delta", "") for item in events["text"])
    assert response.status_code == 200
    assert "核对利润口径" in text
    assert "普通用户" not in text
    assert observed["identity"] == "agent"
    assert observed["organization_identity"] is member
    assert "agent_level" not in observed["user"]


def test_real_jwt_provider_uses_one_wallet_identity_for_kb_and_signed_actions(monkeypatch):
    """删掉 request 级 operation_identity 传递后，四个动作断言会立即转红。"""
    monkeypatch.setattr(gap_assistant, "_record", lambda *args, **kwargs: None)
    wallet_reads: list[int] = []

    def provider_level(user_id):
        wallet_reads.append(int(user_id))
        return 1

    monkeypatch.setattr(xiaobang_api, "_fetch_agent_level", provider_level)
    provider_user = {
        "id": 74,
        "user_id": 74,
        "username": "provider-real-jwt",
        "is_admin": False,
        # 真实 JWT 没有 agent_level；只能由本次请求的钱包解析结果判服务商。
        "permissions": [
            "diagnosis:read", "quote:read", "writing:read", "publish:read",
            "monitoring:read",
        ],
        "client_brand_ids": [],
    }
    client = TestClient(_app(provider_user, identity=None))

    allowed = {
        "经营总览在哪里": ("agent_overview", "/agent/profit"),
        "算力库存在哪里": ("agent_inventory", "/agent/inventory"),
    }
    for question, expected in allowed.items():
        response = client.post("/api/xiaobang/chat", json={
            "message": question,
            "session_id": f"provider-{expected[0]}",
            "current_page": "/dashboard",
        })
        assert response.status_code == 200
        assistant = _events(response)["meta"][-1]["gap_assistant"]
        action = assistant["actions"][0]
        assert (action["operation_id"], action["target_route"]) == expected
        assert "没有" not in assistant["headline"]

    for question, forbidden_operation in (
        ("购买算力在哪里", "customer_recharge"),
        ("推荐有礼在哪里", "referral"),
    ):
        response = client.post("/api/xiaobang/chat", json={
            "message": question,
            "session_id": f"provider-deny-{forbidden_operation}",
            "current_page": "/dashboard",
        })
        assert response.status_code == 200
        assistant = _events(response)["meta"][-1]["gap_assistant"]
        assert "没有" in assistant["headline"]
        assert forbidden_operation not in {
            action["operation_id"] for action in assistant["actions"]
        }

    observed_rag = {}

    def provider_rag(*args, **kwargs):
        observed_rag.update(kwargs)
        return [({
            "source_slug": "provider_profit_help",
            "source_title": "服务商经营说明",
            "section_title": "利润口径",
            "source_type": "doc",
            "content": "利润需结合当前报价和已交付范围核对。",
            "route": "/agent/profit",
            "token_keywords": ["服务方", "利润", "交付"],
        }, 8.0)]

    async def no_embedding(*args, **kwargs):
        return None

    async def provider_stream(*args, **kwargs):
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put("请按当前报价与已交付范围核对利润。")
        await queue.put(None)
        return queue, {"succeeded": True}

    import tools.xiaobang_embed as embed
    monkeypatch.setattr(embed, "embed_one", no_embedding)
    monkeypatch.setattr(xiaobang_api, "bm25_search", provider_rag)
    monkeypatch.setattr(xiaobang_api, "_maybe_clarify", lambda *args, **kwargs: None)
    monkeypatch.setattr(xiaobang_api, "_inject_route_page_card", lambda *args, **kwargs: None)
    monkeypatch.setattr(xiaobang_api, "stream_llm", provider_stream)
    response = client.post("/api/xiaobang/chat", json={
        "message": "服务方利润怎么结合交付范围核对",
        "session_id": "provider-rag",
        "current_page": "/agent/profit",
    })
    assert response.status_code == 200
    events = _events(response)
    assert "已交付范围核对利润" in "".join(
        item.get("delta", "") for item in events["text"]
    )
    assert observed_rag["identity"] == "agent"
    assert observed_rag["organization_identity"] == "agent"

    # 五个独立 ASGI 请求各解析一次；KB/动作链不能再自行从 JWT 猜第二遍。
    assert wallet_reads == [74, 74, 74, 74, 74]


def test_llm_failure_keeps_deterministic_fallback_action(monkeypatch):
    _install_rag_path(monkeypatch, succeeded=False, model_text="模型暂时不可用。")
    response = TestClient(_app()).post("/api/xiaobang/chat", json={
        "message": "如何处理长尾词覆盖短板",
        "session_id": "rag-fail", "current_page": "/writing",
    })
    events = _events(response)
    assert events["meta"][-1]["confidence"] == "low"
    _assert_signed_action(events)


def test_completely_removed_model_still_keeps_navigation(monkeypatch):
    called = False

    async def exploded(*args, **kwargs):
        nonlocal called
        called = True
        raise RuntimeError("model removed")

    monkeypatch.setattr(gap_assistant, "_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(xiaobang_api, "stream_llm", exploded)
    response = TestClient(_app()).post("/api/xiaobang/chat", json={
        "message": "品牌体检在哪里", "session_id": "no-model", "current_page": "/dashboard",
    })
    events = _events(response)
    _assert_signed_action(events)
    assert not called
    assert events["meta"][-1]["gap_assistant"]["actions"][0]["target_route"] == "/diagnosis/new"


def test_model_generated_unknown_url_never_becomes_an_action(monkeypatch):
    _install_rag_path(monkeypatch, succeeded=True, model_text="请打开 https://evil.example/secret")
    response = TestClient(_app()).post("/api/xiaobang/chat", json={
        "message": "如何处理长尾词覆盖短板",
        "session_id": "evil-url", "current_page": "/writing",
    })
    events = _events(response)
    _assert_signed_action(events)
    assert all(
        "evil.example" not in action["target_route"]
        for action in events["meta"][-1]["gap_assistant"]["actions"]
    )


def test_operation_plan_read_failure_is_unavailable_never_fake_zero(monkeypatch):
    monkeypatch.setattr(gap_assistant, "_record", lambda *args, **kwargs: None)

    def unavailable(*args, **kwargs):
        raise RuntimeError("read replica unavailable")

    monkeypatch.setattr(customer_operation_plan, "build_customer_operation_plan", unavailable)
    response = TestClient(_app()).post("/api/xiaobang/chat", json={
        "message": "今天先做什么",
        "session_id": "plan-read-failed",
        "current_page": "/dashboard",
    })
    assert response.status_code == 200
    events = _events(response)
    _assert_signed_action(events)
    assistant = events["meta"][-1]["gap_assistant"]
    text = "".join(item.get("delta", "") for item in events["text"])
    assert assistant["degraded"] is True
    assert "暂时无法读取" in text
    assert "重试" in text
    for fake_zero in ("待写 0", "已完成 0", "已发布 0", "被推荐 0"):
        assert fake_zero not in text
    assert {action["operation_id"] for action in assistant["actions"]} >= {
        "client_list", "help_center",
    }
