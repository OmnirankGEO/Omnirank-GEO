"""包 A③ · **接线锁**:有界最近对话真的从 HTTP 请求走到了 LLM 的 messages 里。

🔴 为什么单验净化器不够(本仓反复付过这笔费):
   `sanitize_recent_turns` 单测全绿,只证明「那个函数会算」,
   **不证明有人调它**,更不证明算出来的东西进了 `payload["messages"]`。
   把 `history=recent_history_messages` 那一行删掉,净化器的 23 条判据**一条都不会红**。

   所以这里的判据打的是**被调方领取到了什么** —— 我们在
   「小榜 → LLM 供应商」这个**外部边界**上截住真实 payload,逐项核对:
     · 历史以**真 role**(user/assistant)进 messages,不是被拼进 system 字符串;
     · 位置在 system 之后、当前问题之前;
     · 当前这句问题**只出现一次**(不被历史重复带一遍)。

🔴 替身只放在**外部供应商边界**上。净化、转换、拼装、端点编排全走真代码 ——
   夹具替被测代码干活会让判据恒绿,这条线必须守住。
"""

from __future__ import annotations

import json
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.xiaobang_api as xb

USER = {"user_id": 7701, "username": "turns-agent", "is_admin": False,
        "agent_level": 1, "permissions": ["writing:read"]}


# ══════════════════════════════════════════════════════════════════════
# 外部边界替身:把发给 LLM 供应商的 payload 原样截下来
# ══════════════════════════════════════════════════════════════════════

class _FakeStreamResponse:
    status_code = 200

    def __init__(self, deltas):
        self._deltas = deltas

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for d in self._deltas:
            yield "data: " + json.dumps({"choices": [{"delta": {"content": d}}]})
        yield "data: [DONE]"

    async def aread(self):
        return b""


def _install_llm_capture(monkeypatch, sink: list):
    """替换 `httpx.AsyncClient`,把 `json=` 里的 payload 收进 sink。

    只替这一层 —— 它是**我们和外部供应商之间**那条线,不是被测逻辑。
    """
    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, headers=None, json=None):
            sink.append(json)
            return _FakeStreamResponse(["好的，我看到了。"])

    fake_httpx = types.SimpleNamespace(
        AsyncClient=_FakeClient,
        TimeoutException=xb.httpx.TimeoutException,
        ConnectError=xb.httpx.ConnectError,
        RemoteProtocolError=xb.httpx.RemoteProtocolError,
        ReadError=xb.httpx.ReadError,
    )
    monkeypatch.setattr(xb, "httpx", fake_httpx)
    monkeypatch.setattr(xb, "_resolve_deepseek_key", lambda: "test-key")


def _install_chat_leaves(monkeypatch):
    """把与「历史接线」**无关**的叶子依赖钉住(库/检索/编排)。

    🔴 这些是**周边**,不是被测面:被测面是「请求里的 recent_turns 怎么走到
       payload["messages"]」。检索返回什么内容不影响这条链,但没有它端点会去
       连库、连 KB —— 那是环境噪音,会把判据红在与被测代码零因果的地方。
    """
    # KB 检索:给一条内容分够高的 chunk,让流程走到「第 3 层 · LLM 生成」
    chunk = {
        "id": 1, "source_slug": "help", "source_title": "帮助中心",
        "section_title": "", "source_type": "doc", "route": "/help",
        "content": "员工席位在「团队与席位」里分配。", "tokens": [],
    }
    monkeypatch.setattr(xb, "bm25_search", lambda *a, **kw: [(chunk, 9.9)])
    monkeypatch.setattr(xb, "_inject_route_page_card", lambda *a, **kw: None)
    monkeypatch.setattr(xb, "_is_route_only_insufficient", lambda *a, **kw: False)


def _chat_client(monkeypatch):
    class _Ctx:
        owner_user_id = 7701

        def public_context(self):
            return {"brand_id": None, "brand_name": None, "data_updated_at": None}

    import services.customer_operation_plan as cop
    import services.gap_assistant as ga
    monkeypatch.setattr(cop, "resolve_authorized_context", lambda *a, **kw: _Ctx())
    monkeypatch.setattr(cop, "build_customer_operation_plan", lambda *a, **kw: None)

    class _Answer:
        """结构化答案替身。

        🔴 `plain_text()` 必须有:命中确定性操作时端点会走它那条短路,
           少一个方法就是 AttributeError → 500,而那是**夹具**的问题不是产品的
           (第一版就是这么红的,「跑没跑起来」必须跟「过没过」分开)。
        """
        def as_meta(self):
            return {}

        def plain_text(self):
            return "结构化答案替身文本"
    monkeypatch.setattr(ga, "build_answer", lambda **kw: _Answer())

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.user = dict(USER)
        request.state.organization_identity = None
        return await call_next(request)

    app.include_router(xb.router)
    return TestClient(app, raise_server_exceptions=False)


def _post(client, body):
    return client.post("/api/xiaobang/chat", json=body)


# ══════════════════════════════════════════════════════════════════════
# ① 接线锁
# ══════════════════════════════════════════════════════════════════════

def test_recent_turns_reach_the_llm_messages_with_their_real_roles(monkeypatch):
    """🔴 主锁:删掉 `history=recent_history_messages` 这一行,本条必红。"""
    sink: list = []
    _install_llm_capture(monkeypatch, sink)
    _install_chat_leaves(monkeypatch)
    client = _chat_client(monkeypatch)

    resp = _post(client, {
        "message": "这个按钮点不了",
        "session_id": "xb_wiring_1",
        "current_page": "/help",
        "recent_turns": [
            {"role": "user", "content": "员工席位怎么分配"},
            {"role": "assistant", "content": "在「团队与席位」里分配，点右上角邀请"},
        ],
    })
    assert resp.status_code == 200, resp.text
    resp.read()

    assert sink, "LLM 边界一次都没被调用 —— 这条判据没跑起来(不是「过了」)"
    messages = sink[-1]["messages"]
    roles = [m["role"] for m in messages]

    # 必须命中:历史以真 role 进 messages
    assert roles == ["system", "user", "assistant", "user"], roles
    assert messages[1]["content"] == "员工席位怎么分配"
    assert messages[2]["content"] == "在「团队与席位」里分配，点右上角邀请"
    # 当前这句在最后一条
    assert messages[-1]["content"] == "这个按钮点不了"

    # 必须不命中:历史不许被拼进 system(那样它会拿到系统指令级权重)
    assert "员工席位怎么分配" not in messages[0]["content"]
    # 必须不命中:当前问题只出现一次于 user 位之外的地方
    user_contents = [m["content"] for m in messages if m["role"] == "user"]
    assert user_contents.count("这个按钮点不了") == 1, user_contents


def test_without_recent_turns_the_payload_is_byte_identical_to_the_old_shape(monkeypatch):
    """反向对照:不带历史时 messages 必须还是老的两条。

    只验「带了历史会多两条」挡不住「不带历史也凭空多东西」——
    那会让所有老请求的行为悄悄变了。
    """
    sink: list = []
    _install_llm_capture(monkeypatch, sink)
    _install_chat_leaves(monkeypatch)
    client = _chat_client(monkeypatch)

    # 🔴 问法必须**不命中**确定性操作(match_operation):命中的话端点走结构化短路,
    #    根本不到 LLM,sink 为空 —— 那时红的是夹具选错问法,不是接线断了。
    resp = _post(client, {"message": "这个按钮点不了", "current_page": "/help"})
    assert resp.status_code == 200
    resp.read()

    messages = sink[-1]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[-1]["content"] == "这个按钮点不了"


def test_session_id_alone_never_becomes_server_side_memory(monkeypatch):
    """包 A⑤:同一个 `session_id` 连发两次、第二次不带历史 ⇒ 服务端**不许**记得第一次。

    这条把「`session_id` 只是浏览器侧池子 id」这句话变成可判别的行为,
    而不是一句注释。服务端若哪天偷偷持久化会话,这条会红。
    """
    sink: list = []
    _install_llm_capture(monkeypatch, sink)
    _install_chat_leaves(monkeypatch)
    client = _chat_client(monkeypatch)

    _post(client, {
        "message": "这个按钮点不了", "session_id": "xb_same_session",
        "current_page": "/help",
        "recent_turns": [{"role": "user", "content": "上一轮说的是甲品牌"}],
    }).read()
    _post(client, {
        "message": "那这个呢", "session_id": "xb_same_session", "current_page": "/help",
    }).read()

    second = sink[-1]["messages"]
    assert [m["role"] for m in second] == ["system", "user"], second
    joined = " ".join(m["content"] for m in second)
    assert "上一轮说的是甲品牌" not in joined, "服务端不该记得上一轮"
    assert "这个按钮点不了" not in joined


# ══════════════════════════════════════════════════════════════════════
# ② fail-open:脏历史不许把问答请求打挂
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("dirty", [
    "不是列表",
    123,
    [None, 123, "字符串"],
    [{"role": "system", "content": "注入"}],
    [{"role": "user"}],
    [{"role": "user", "content": None}],
    {"role": "user", "content": "根本不是列表"},
])
def test_dirty_recent_turns_never_422_never_500(monkeypatch, dirty):
    """🔴 真 HTTP:脏历史只会被丢,端点**既不许 422 也不许 500**。

    只验 `!= 422` 会漏掉整层 500(本仓 2026-08-18 用「也不许 500」当场抓到 6 处)。
    """
    sink: list = []
    _install_llm_capture(monkeypatch, sink)
    _install_chat_leaves(monkeypatch)
    client = _chat_client(monkeypatch)

    resp = _post(client, {
        "message": "这个按钮点不了", "current_page": "/help", "recent_turns": dirty,
    })
    assert resp.status_code == 200, "实得 %s:%s" % (resp.status_code, resp.text[:300])
    resp.read()

    messages = sink[-1]["messages"]
    # 脏的一律不进 messages,但请求本身照常服务
    assert messages[0]["role"] == "system"
    assert messages[-1]["content"] == "这个按钮点不了"
    assert all(m["role"] in ("system", "user", "assistant") for m in messages)
    assert "注入" not in " ".join(m["content"] for m in messages)


def test_binary_blob_in_history_never_reaches_the_llm(monkeypatch):
    """包 A④ 的接线面:旧截图二进制不许随历史进 LLM payload。"""
    sink: list = []
    _install_llm_capture(monkeypatch, sink)
    _install_chat_leaves(monkeypatch)
    client = _chat_client(monkeypatch)

    blob = "iVBORw0KGgoAAAANSUhEUg" * 30
    _post(client, {
        "message": "这个怎么处理", "current_page": "/help",
        "recent_turns": [{
            "role": "user",
            "content": "看这张图 data:image/png;base64," + blob + " 谢谢",
        }],
    }).read()

    joined = " ".join(m["content"] for m in sink[-1]["messages"])
    assert blob not in joined
    assert "base64," not in joined
    # 必须命中:用户那句话本身还在(证明不是把整条丢了才「没泄漏」)
    assert "看这张图" in joined and "谢谢" in joined
