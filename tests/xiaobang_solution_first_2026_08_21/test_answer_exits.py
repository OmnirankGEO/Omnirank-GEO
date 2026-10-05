"""包 D①③⑤ · 每一轮失败都有出口 + 重复兜底自动升级。

工单 §5.3 / §8 S05-S06 / §9.1 最后一条(「故意删除 action 后测试必须失败」)。

🔴 重复检测**没有新建任何服务端会话表**(§7.4 明令)——
   用的是包 A③ 已经在带的 `recent_turns`:服务端因此看得见自己上一轮说过什么。
"""

from __future__ import annotations

import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.xiaobang_api as xb
from services.xiaobang_answer_exits import (
    EXIT_CLARIFY,
    EXIT_HANDOFF,
    EXIT_MANUAL,
    EXIT_RESOLVED,
    EXIT_RETRY,
    EXIT_UNRESOLVED,
    assert_has_exit,
    build_exits,
    fallback_signature,
    is_repeat_fallback,
    looks_like_fallback,
)

USER = {"user_id": 7704, "username": "exits-agent", "is_admin": False,
        "agent_level": 1, "permissions": ["writing:read"]}


# ══════════════════════════════════════════════════════════════════════
# ① 兜底指纹 —— 判形态不判措辞
# ══════════════════════════════════════════════════════════════════════

FALLBACKS = [
    "这个我没找到准确答案,你可以去帮助中心翻文档。",
    "我能看到你大概在「效果监测」这个页面，但这块的知识库说明还不够，没法给你准确解答。",
    "不清楚,请查看帮助文档。",
]

NOT_FALLBACKS = [
    "监测每天跑四个引擎,每个关键词各问一次。",
    "员工席位在「团队与席位」里分配。",
    "算力余额在钱包页面看。",
]


@pytest.mark.parametrize("text", FALLBACKS)
def test_fallback_shapes_get_a_signature(text):
    assert looks_like_fallback(text), text
    assert fallback_signature(text), text


@pytest.mark.parametrize("text", NOT_FALLBACKS)
def test_real_answers_get_no_signature(text):
    """反向对照:正常答案不许被当成兜底 —— 否则每一轮都会被判成「又循环了」。"""
    assert not looks_like_fallback(text), text
    assert fallback_signature(text) is None, text


def test_the_same_fallback_on_a_different_page_is_still_the_same_fallback():
    """🔴 指纹**不含页面名**:同一句兜底套在不同页面上,对用户仍是「又没答上来」。

    把页面名算进指纹,重复检测就永远不会命中(每一页都算「新的」)—— 恒绿。
    """
    a = "我能看到你大概在「效果监测」这个页面，但这块的知识库说明还不够，没法给你准确解答。"
    b = "我能看到你大概在「发布投放」这个页面，但这块的知识库说明还不够，没法给你准确解答。"
    assert fallback_signature(a) == fallback_signature(b)


def test_a_different_kind_of_fallback_is_not_a_repeat():
    """反向对照:换了一种兜底 = 有进展,不算循环。"""
    doc_punt = "这个我没找到准确答案,你可以去帮助中心翻文档。"
    manual = "我不清楚这块,点下方把问题反馈给工作人员会有人跟进。"
    assert fallback_signature(doc_punt) != fallback_signature(manual)


def test_is_repeat_fallback_only_fires_on_an_assistant_turn():
    """用户自己说「没找到」不算助手在循环。"""
    text = "这个我没找到准确答案,你可以去帮助中心翻文档。"
    assert is_repeat_fallback([{"role": "assistant", "content": text}], text)
    assert not is_repeat_fallback([{"role": "user", "content": text}], text)
    assert not is_repeat_fallback([], text)


# ══════════════════════════════════════════════════════════════════════
# ② 出口装配
# ══════════════════════════════════════════════════════════════════════

def test_low_confidence_always_has_at_least_one_exit():
    exits = build_exits(confidence="low")
    assert exits
    assert_has_exit(exits, where="t")
    ids = [e["exit_id"] for e in exits]
    assert EXIT_UNRESOLVED in ids and EXIT_RESOLVED in ids, ids


def test_handoff_exit_tells_the_user_what_will_be_submitted():
    """工单 §6 包 D③:必须让用户知道**会提交什么**,不能悄悄外发。"""
    exits = build_exits(confidence="low", handoff=True)
    handoff = [e for e in exits if e["exit_id"] == EXIT_HANDOFF]
    assert handoff, exits
    submits = handoff[0]["submits"]
    assert submits and len(submits) >= 3, submits
    joined = " ".join(submits)
    assert "当前页面" in joined and "最近几轮对话" in joined, submits


def test_repeat_fallback_adds_clarify_and_handoff():
    exits = build_exits(confidence="low", handoff=True, repeat_fallback=True)
    ids = [e["exit_id"] for e in exits]
    assert EXIT_CLARIFY in ids and EXIT_HANDOFF in ids, ids


def test_retryable_failure_offers_retry_and_a_manual_path():
    ids = [e["exit_id"] for e in build_exits(confidence="low", handoff=True, retryable=True)]
    assert EXIT_RETRY in ids and EXIT_MANUAL in ids, ids


def test_assert_has_exit_actually_raises_on_an_empty_list():
    """🔴 给守卫注毒:空出口必须抛,否则它是摆设。"""
    with pytest.raises(ValueError):
        assert_has_exit([], where="poison")


# ══════════════════════════════════════════════════════════════════════
# ③ 真 HTTP:三支失败面各自带出口
# ══════════════════════════════════════════════════════════════════════

def _client(monkeypatch, *, llm_text=None, llm_ok=True, route_only=False):
    class _FakeResp:
        status_code = 200 if llm_ok else 500

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        async def aiter_lines(self):
            import json as _j
            yield "data: " + _j.dumps({"choices": [{"delta": {"content": llm_text or "好"}}]})
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
            return _FakeResp()

    monkeypatch.setattr(xb, "httpx", types.SimpleNamespace(
        AsyncClient=_FakeClient,
        TimeoutException=xb.httpx.TimeoutException,
        ConnectError=xb.httpx.ConnectError,
        RemoteProtocolError=xb.httpx.RemoteProtocolError,
        ReadError=xb.httpx.ReadError,
    ))
    monkeypatch.setattr(xb, "_resolve_deepseek_key", lambda: "k")
    chunk = {"id": 1, "source_slug": "/monitoring", "source_title": "效果监测",
             "section_title": "", "source_type": "doc", "route": "/monitoring",
             "content": "监测每天跑四个引擎。", "tokens": []}
    monkeypatch.setattr(xb, "bm25_search", lambda *a, **kw: [(chunk, 9.9)])
    monkeypatch.setattr(
        xb, "_inject_route_page_card",
        lambda *a, **kw: ({"source_title": "效果监测", "content": "页面卡"} if route_only else None))
    monkeypatch.setattr(xb, "_is_route_only_insufficient", lambda *a, **kw: route_only)

    class _Ctx:
        owner_user_id = 7704

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


def _post(client, message, turns=None):
    body = {"message": message, "session_id": "xb_exits", "current_page": "/monitoring"}
    if turns is not None:
        body["recent_turns"] = turns
    r = client.post("/api/xiaobang/chat", json=body)
    return r, r.text


ROUTE_ONLY_FALLBACK = (
    "我能看到你大概在「效果监测」这个页面，但这块的知识库说明还不够，"
    "没法给你准确解答。建议点下方把问题反馈给工作人员，会有人帮你跟进。"
)


def test_route_only_fallback_carries_exits(monkeypatch):
    """🔴 主锁:低置信兜底那支必须带出口。删掉那段 `exits`,本条必红。"""
    client = _client(monkeypatch, route_only=True)
    resp, text = _post(client, "这个页面的这个按钮点不了")
    assert resp.status_code == 200
    assert '"exits"' in text, text[:400]
    assert EXIT_HANDOFF in text and EXIT_RESOLVED in text, text[:400]


def test_the_second_identical_fallback_escalates(monkeypatch):
    """🔴 工单 S06:同一条兜底第二次必须**改口**并升级 `should_escalate`。"""
    client = _client(monkeypatch, route_only=True)
    _, first = _post(client, "这个按钮点不了")
    assert '"should_escalate": false' in first.lower(), first[:300]

    _, second = _post(client, "那这个呢", turns=[
        {"role": "user", "content": "这个按钮点不了"},
        {"role": "assistant", "content": ROUTE_ONLY_FALLBACK},
    ])
    assert '"should_escalate": true' in second.lower(), second[:500]
    assert "同一句我不想再说第二遍" in second, second[:500]
    assert EXIT_CLARIFY in second, second[:500]


def test_a_different_previous_fallback_does_not_escalate(monkeypatch):
    """反向对照:上一轮是**另一种**兜底 ⇒ 不算循环,不许升级。

    只验「重复会升级」挡不住「一见到兜底就升级」—— 那会把正常的第一次也当循环。
    """
    client = _client(monkeypatch, route_only=True)
    _, text = _post(client, "这个按钮点不了", turns=[
        {"role": "assistant", "content": "我不清楚这块,点下方把问题反馈给工作人员会有人跟进。"},
    ])
    assert '"should_escalate": false' in text.lower(), text[:400]


def test_llm_no_answer_branch_carries_exits(monkeypatch):
    """截图那句「去帮助中心翻文档」的出处 —— 现在必须带出口。"""
    client = _client(monkeypatch, llm_text="这个我没找到准确答案,你可以去帮助中心翻文档。")
    resp, text = _post(client, "这个按钮点不了")
    assert resp.status_code == 200
    assert '"exits"' in text, text[:400]
    assert EXIT_HANDOFF in text, text[:400]
    assert '"handoff": true' in text.lower(), text[:400]
