"""包 B② · `current_page` 不再关掉正确答案 —— 判据打**真实抽屉的请求形态**。

🔴 工单 §9.1 第一条原话:「真实 drawer 请求带 `current_page` 时,
   『你能做什么』仍命中 meta/capability 答案」。

   本仓此前的判据全是**API 直调形态**(不带 current_page),
   所以「同一句话在抽屉里得到另一种结果」这件事**一条判据都没打到**。
   这里每一条都带 `current_page`,和真实抽屉逐字同形。
"""

from __future__ import annotations

import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.xiaobang_api as xb
from services.xiaobang_question_intent import (
    deterministic_answer_allowed,
    is_self_contained_question,
    mentions_current_page,
)

USER = {"user_id": 7702, "username": "gate-agent", "is_admin": False,
        "agent_level": 1, "permissions": ["writing:read"]}


# ══════════════════════════════════════════════════════════════════════
# ① 纯函数层:自足问句 vs 指页面问句(成对样本)
# ══════════════════════════════════════════════════════════════════════

SELF_CONTAINED = [
    "你除了引导我去帮助中心还能做什么",
    "你能做什么",
    "你还能干嘛",
    "小榜能力有哪些",
    "怎么收费",
    "监测怎么计费",
    "你是谁",
    "你用的什么模型",
]

PAGE_DEICTIC = [
    "这个页面怎么用",
    "当前页面这块是什么意思",
    "这页的按钮点不了",
    "这个按钮点不了",
    "这个输入框填什么",
    "页面上的那个数字是什么",
    "截图里这个报错怎么办",
    "这里的开关是干嘛的",
]

NEITHER = [
    "员工席位怎么用",
    "GEO 图文无法使用",
    "怎么发起品牌体检",
    "监测要跑几个引擎",
]


@pytest.mark.parametrize("q", SELF_CONTAINED)
def test_self_contained_questions_win_even_with_a_current_page(q):
    """必须命中:自足问句带着页面也照样短路。"""
    assert is_self_contained_question(q), q
    assert deterministic_answer_allowed(q, current_page="/monitoring"), q
    # 带截图也一样
    assert deterministic_answer_allowed(q, current_page="/monitoring",
                                        attachment_text="按钮 报错 状态"), q


@pytest.mark.parametrize("q", PAGE_DEICTIC)
def test_page_deictic_questions_hand_the_wheel_to_the_page(q):
    """必须不命中:真的在指页面时,页面卡 + RAG 说了算。"""
    assert mentions_current_page(q), q
    assert not deterministic_answer_allowed(q, current_page="/monitoring"), q


@pytest.mark.parametrize("q", NEITHER)
def test_ordinary_questions_are_allowed_and_are_not_mistaken_for_page_deixis(q):
    """反向对照:普通自足问题既不该被当成指页面,也该被放行。

    只验上面两组,挡不住「把所有问题都判成指页面」这种一刀切 ——
    那会让确定性答案继续全局失效,而 SELF_CONTAINED 那组照样绿。
    """
    assert not mentions_current_page(q), q
    assert deterministic_answer_allowed(q, current_page="/monitoring"), q


def test_bare_pronoun_plus_action_verb_goes_to_the_rag_path_not_a_canned_answer():
    """指示代词 + 操作动词(「这个怎么用」「这个怎么填」)→ 交给 RAG/LLM。

    🔴 **我改过这条判据的方向,理由记在这里**:
      · 旧断言(本包早先版本):「这个怎么用」是裸指代,不算指页面 ⇒ 放行确定性答案;
      · 新断言:算指页面 ⇒ 走 RAG/LLM;
      · 依据:`recent_turns` 只在 **RAG/LLM 那条路**上进 messages
        (`stream_llm(history=...)`),确定性短路那条**根本不带历史**。
        所以把「这个怎么用」交给确定性答案,恰恰是让它**永远解析不了上文** ——
        与工单 §4 P1-2「追问『这个怎么用』要能被解析」直接冲突。
        交给 RAG 才是能真正用上历史的那条路。
      · 顺带:这样 `tests/system_kb/test_preset_gating.py` 的两条既有断言
        **不需要翻转**(实测 6 passed),少欠两条翻转账。
    """
    assert mentions_current_page("这个怎么用")
    assert not deterministic_answer_allowed("这个怎么用", current_page="/monitoring")
    # 反向对照:带明确主语的同类问法**不算**指页面,照旧放行确定性答案
    assert not mentions_current_page("员工席位怎么用")
    assert deterministic_answer_allowed("员工席位怎么用", current_page="/monitoring")
    # 反向对照:「应该怎么用」里的「该」不是指示代词,不许误判
    assert not mentions_current_page("应该怎么用")


# ══════════════════════════════════════════════════════════════════════
# ② 真 HTTP · 抽屉形态(必带 current_page)
# ══════════════════════════════════════════════════════════════════════

def _client(monkeypatch, llm_sink: list):
    """只替外部 LLM 边界与库叶子;意图分层、preset 匹配、端点编排全走真代码。"""
    class _FakeResp:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"LLM 生成的答案"}}]}'
            yield "data: [DONE]"

        async def aread(self):
            return b""

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        def stream(self, method, url, headers=None, json=None):
            llm_sink.append(json)
            return _FakeResp()

    monkeypatch.setattr(xb, "httpx", types.SimpleNamespace(
        AsyncClient=_FakeClient,
        TimeoutException=xb.httpx.TimeoutException,
        ConnectError=xb.httpx.ConnectError,
        RemoteProtocolError=xb.httpx.RemoteProtocolError,
        ReadError=xb.httpx.ReadError,
    ))
    monkeypatch.setattr(xb, "_resolve_deepseek_key", lambda: "test-key")
    chunk = {"id": 1, "source_slug": "monitoring", "source_title": "效果监测",
             "section_title": "", "source_type": "doc", "route": "/monitoring",
             "content": "监测每天跑四个引擎。", "tokens": []}
    monkeypatch.setattr(xb, "bm25_search", lambda *a, **kw: [(chunk, 9.9)])
    monkeypatch.setattr(xb, "_inject_route_page_card", lambda *a, **kw: None)
    monkeypatch.setattr(xb, "_is_route_only_insufficient", lambda *a, **kw: False)

    class _Ctx:
        owner_user_id = 7702

        def public_context(self):
            return {"brand_id": None, "brand_name": None, "data_updated_at": None}

    import services.customer_operation_plan as cop
    import services.gap_assistant as ga
    monkeypatch.setattr(cop, "resolve_authorized_context", lambda *a, **kw: _Ctx())
    monkeypatch.setattr(cop, "build_customer_operation_plan", lambda *a, **kw: None)

    class _Answer:
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


def _drawer_post(client, message, **kw):
    """抽屉形态:`current_page` **恒带**(这正是真实 `useXiaobangChat` 的行为)。"""
    body = {"message": message, "session_id": "xb_gate", "current_page": "/monitoring"}
    body.update(kw)
    resp = client.post("/api/xiaobang/chat", json=body)
    text = resp.text
    return resp, text


def test_what_can_you_do_hits_the_preset_even_from_the_real_drawer(monkeypatch):
    """🔴 主锁 · 工单 S03:抽屉里问「你还能做什么」必须命中 preset,不能落到 LLM/帮助中心。

    把闸改回 `not current_page and not attachment_text`,本条必红。
    """
    sink: list = []
    client = _client(monkeypatch, sink)
    resp, text = _drawer_post(client, "你除了引导我去帮助中心还能做什么")
    assert resp.status_code == 200

    # 必须命中:走的是 preset 那条(meta 里有 preset 来源)
    assert '"source_type": "preset"' in text or '"source_type":"preset"' in text, text[:600]
    # 必须不命中:没有落到 LLM
    assert not sink, "命中 preset 就不该再调 LLM;实得 %d 次调用" % len(sink)


def test_page_deictic_question_from_the_drawer_still_goes_to_rag(monkeypatch):
    """反向对照:真的在指页面时,**不许**被 preset 抢走。

    只验「preset 能命中了」挡不住「preset 现在什么都抢」——
    那会把页面问题也答成套话,比原来更糟。
    """
    sink: list = []
    client = _client(monkeypatch, sink)
    resp, text = _drawer_post(client, "这个页面的这个按钮点不了")
    assert resp.status_code == 200
    assert '"source_type": "preset"' not in text and '"source_type":"preset"' not in text
    assert sink, "指页面的问题应当走到 RAG/LLM"


def test_pricing_question_from_the_drawer_is_not_blocked_by_the_page(monkeypatch):
    """「怎么收费」是自足问句 —— 站在监测页问也该拿到确定性答案。"""
    sink: list = []
    client = _client(monkeypatch, sink)
    resp, text = _drawer_post(client, "怎么收费")
    assert resp.status_code == 200
    assert '"source_type": "preset"' in text or '"source_type":"preset"' in text, text[:600]


def test_attachment_alone_no_longer_closes_the_deterministic_answer(monkeypatch):
    """带截图 + 自足问句 ⇒ 仍然命中确定性答案(截图本身不再是一道闸)。"""
    sink: list = []
    client = _client(monkeypatch, sink)
    resp, text = _drawer_post(client, "你能做什么", attachment_text="按钮 报错 状态 步骤")
    assert resp.status_code == 200
    assert '"source_type": "preset"' in text or '"source_type":"preset"' in text, text[:600]


def test_attachment_plus_page_deictic_still_goes_to_rag(monkeypatch):
    """反向对照:截图 + 指页面 ⇒ 照旧走 RAG。"""
    sink: list = []
    client = _client(monkeypatch, sink)
    resp, text = _drawer_post(client, "截图里这个报错怎么办", attachment_text="报错 500")
    assert resp.status_code == 200
    assert '"source_type": "preset"' not in text and '"source_type":"preset"' not in text
    assert sink
