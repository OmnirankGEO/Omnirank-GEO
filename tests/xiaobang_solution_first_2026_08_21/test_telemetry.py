"""包 E · 结构化观测事件的**脱敏**与**指标可算性**。

两件事最容易做成假绿,这里各钉一条:
  ① 「记了一堆字段,但工单要的六个指标算不出来」;
  ② 「按黑名单脱敏」—— 漏的那天没有人会发现,因为多一个字段不会让任何东西变红。
"""

from __future__ import annotations

import pytest

from services.xiaobang_telemetry import (
    ALLOWED_KEYS,
    assert_no_sensitive_payload,
    build_turn_event,
    emit,
)


def _event(**over):
    base = dict(
        session_id="xb_1755000000_abcd",
        request_id="req-1",
        actor_kind="agent",
        current_route="/monitoring?brand_id=101&quote_id=77",
        route_valid=True,
        intent_kind="page_deictic",
        knowledge_manifest_version="content-v1",
        knowledge_status="ok",
        retrieval_state="hit",
        answer_state="answered",
        freshness_state="live",
        exits=[{"exit_id": "handoff"}, {"exit_id": "resolved"}],
        action_kinds=["navigate"],
        # 🔴 默认必须是 unknown:第一版默认写 "resolved",于是「已解决率」那条
        #    把没表态的行也算了进去(0.5 而不是 1/6)—— 红的是夹具不是被测行为。
        resolution="unknown",
    )
    base.update(over)
    return build_turn_event(**base)


# ══════════════════════════════════════════════════════════════════════
# ① 白名单:多一个键都进不来
# ══════════════════════════════════════════════════════════════════════

def test_event_keys_are_exactly_the_allowlist():
    assert set(_event()) == set(ALLOWED_KEYS)


@pytest.mark.parametrize("forbidden", [
    "message", "answer", "attachment_text", "token", "authorization",
    "user_id", "username", "display_name", "contact", "screenshot_ocr",
])
def test_forbidden_fields_have_no_way_in(forbidden):
    """🔴 这些字段**连参数入口都没有** —— 「忘了脱敏」在这里连写都写不出来。

    只断言「事件里没有它们」挡不住「有人加个参数就能塞进来」;
    这里同时断言 `build_turn_event` 的签名里没有它们。
    """
    import inspect
    from services import xiaobang_telemetry as tel
    assert forbidden not in _event()
    assert forbidden not in inspect.signature(tel.build_turn_event).parameters


def test_an_extra_key_is_rejected_by_construction(monkeypatch):
    """🔴 给白名单注毒:把一个键从白名单里拿掉,组装必须当场抛。"""
    from services import xiaobang_telemetry as tel
    monkeypatch.setattr(tel, "ALLOWED_KEYS", frozenset(ALLOWED_KEYS) - {"ticket_id"})
    with pytest.raises(ValueError, match="白名单外"):
        tel.build_turn_event(session_id="s", request_id="r")


# ══════════════════════════════════════════════════════════════════════
# ② 不可反推
# ══════════════════════════════════════════════════════════════════════

def test_correlation_ids_are_not_the_raw_values():
    session = "xb_1755000000_abcd"
    ev = _event(session_id=session)
    assert session not in ev["session_correlation_id"]
    assert ev["session_correlation_id"] != session
    # 同一个 session 稳定,不同 session 不同(否则关联不上 / 或全都撞在一起)
    assert ev["session_correlation_id"] == _event(session_id=session)["session_correlation_id"]
    assert ev["session_correlation_id"] != _event(session_id="other")["session_correlation_id"]


def test_the_same_value_hashes_differently_per_purpose():
    """盐里带用途:同一个原值在 turn / session 两处算出的哈希必须不同。

    不然拿两处哈希一碰就能反推出「这个 turn 属于这个 session」之外的关系。
    """
    ev = _event(session_id="same", request_id="same")
    assert ev["turn_id"] != ev["session_correlation_id"]


def test_query_string_is_stripped_from_the_route():
    """路由留形状,查询串砍掉 —— 里面是 brand_id / quote_id 这类对象 id。"""
    ev = _event()
    assert ev["current_route"] == "/monitoring"
    assert "brand_id" not in ev["current_route"]


# ══════════════════════════════════════════════════════════════════════
# ③ 形态复核(值里的泄漏)
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("poison", [
    "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoxfQ",
    "Bearer abc123",
    "authorization: x",
    "13800138000",
    "someone@example.com",
    "很长的用户正文" * 30,
])
def test_sensitive_shapes_are_caught_even_inside_an_allowed_field(poison):
    """🔴 白名单挡结构,这一层挡值 —— 有人把正文塞进 `error_kind` 也要红。"""
    with pytest.raises(ValueError):
        assert_no_sensitive_payload({"error_kind": poison}, where="t")


def test_ordinary_enum_values_pass_the_shape_check():
    """反向对照:正常枚举值不许被判红,否则复核会逼人把事件掏空。"""
    assert_no_sensitive_payload(_event(), where="t")


def test_emit_never_raises_even_on_a_poisoned_event(caplog):
    """🔴 观测永远不许打断问答:事件脏了就丢,不往上抛。"""
    emit({"event": "x", "error_kind": "Bearer leak"})   # 不抛
    emit({"event": "x", "error_kind": "ok"})


# ══════════════════════════════════════════════════════════════════════
# ④ 六个管理端指标必须**算得出来**
# ══════════════════════════════════════════════════════════════════════

def test_every_required_metric_is_derivable():
    """🔴 工单 §6 包 E 点名的六个率 + 版本分布,逐个从事件字段算一遍。

    「记了一堆字段但算不出要的指标」是观测面最常见的假绿:
    字段齐全、看起来很专业,真要出报表时发现分母不在。
    """
    rows = [
        _event(help_center_only=True, exits=[], resolution="unknown"),
        _event(repeat_fallback=True, exits=[{"exit_id": "clarify"}]),
        _event(answer_state="low_confidence", exits=[], resolution="unknown"),
        _event(route_valid=False),
        _event(handoff_submitted=True, ticket_id=4321, resolution="handoff"),
        _event(resolution="resolved"),
    ]
    n = len(rows)

    def rate(pred):
        return sum(1 for r in rows if pred(r)) / n

    # 1 帮助中心唯一答复率
    assert rate(lambda r: r["help_center_only"]) == 1 / n
    # 2 连续相同 fallback 率
    assert rate(lambda r: r["repeat_fallback"]) == 1 / n
    # 3 低置信且无动作率
    assert rate(lambda r: r["answer_state"] == "low_confidence" and not r["exit_ids"]) == 1 / n
    # 4 无效 route 率
    assert rate(lambda r: r["route_valid"] is False) == 1 / n
    # 5 人工接管率(且工单 id 拿得到)
    assert rate(lambda r: r["handoff_submitted"]) == 1 / n
    assert [r["ticket_id"] for r in rows if r["handoff_submitted"]] == [4321]
    # 6 已解决率
    assert rate(lambda r: r["resolution"] == "resolved") == 1 / n
    # 7 知识版本分布
    versions = {}
    for r in rows:
        versions[r["knowledge_manifest_version"]] = \
            versions.get(r["knowledge_manifest_version"], 0) + 1
    assert versions == {"content-v1": n}


def test_the_zero_tolerance_metrics_have_a_denominator_at_all():
    """🔴 分母自证:上面六个率里的每一个,字段都必须**真的存在**于事件里。

    少一个字段,对应那条率就永远算不出来,而报表会显示 0% —— 「没有分母」
    和「指标为零」在数字上一模一样,本仓反复付过这笔费。
    """
    ev = _event()
    for field in ("help_center_only", "repeat_fallback", "answer_state", "exit_ids",
                  "route_valid", "handoff_submitted", "ticket_id", "resolution",
                  "knowledge_manifest_version"):
        assert field in ev, field


# ══════════════════════════════════════════════════════════════════════
# ⑤ 接线锁:一轮问答**恰好**发一条事件,且从哪一支返回都发
# ══════════════════════════════════════════════════════════════════════

def _chat_client(monkeypatch, *, route_only=False, llm_text="好的", boom=False):
    import types
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import api.xiaobang_api as xb

    class _FakeResp:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        async def aiter_lines(self):
            import json as _j
            yield "data: " + _j.dumps({"choices": [{"delta": {"content": llm_text}}]})
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

        def stream(self, *a, **kw):
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
    if boom:
        def _explode(*a, **kw):
            raise RuntimeError("retrieval blew up")
        monkeypatch.setattr(xb, "bm25_search", _explode)
    else:
        monkeypatch.setattr(xb, "bm25_search", lambda *a, **kw: [(chunk, 9.9)])
    monkeypatch.setattr(xb, "_inject_route_page_card",
                        lambda *a, **kw: ({"source_title": "效果监测",
                                           "content": "卡"} if route_only else None))
    monkeypatch.setattr(xb, "_is_route_only_insufficient", lambda *a, **kw: route_only)

    class _Ctx:
        owner_user_id = 7705

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
            return "替身"
    monkeypatch.setattr(ga, "build_answer", lambda **kw: _Answer())

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.user = {"user_id": 7705, "is_admin": False, "agent_level": 1,
                              "permissions": []}
        request.state.organization_identity = None
        return await call_next(request)

    app.include_router(xb.router)
    return TestClient(app, raise_server_exceptions=False)


def _capture_events(monkeypatch):
    sink = []
    import services.xiaobang_telemetry as tel
    monkeypatch.setattr(tel, "emit", lambda ev: sink.append(dict(ev)))
    return sink


@pytest.mark.parametrize("kind,kw", [
    ("正常答复", {}),
    ("低置信兜底", {"route_only": True}),
    ("LLM 自陈无答案", {"llm_text": "这个我没找到准确答案,你可以去帮助中心翻文档。"}),
    ("链路抛异常", {"boom": True}),
])
def test_exactly_one_event_per_turn_from_every_branch(monkeypatch, kind, kw):
    """🔴 主锁:这条流有 12 个 `done` 出口,**每一支**都必须恰好发一条事件。

    逐支埋 emit 必然漏一支,而漏掉的那支不会让任何东西变红 ——
    所以收口点只有一个(generator 的 finally)。把那个 finally 删掉,本条必红。
    """
    sink = _capture_events(monkeypatch)
    client = _chat_client(monkeypatch, **kw)
    resp = client.post("/api/xiaobang/chat", json={
        "message": "这个按钮点不了", "session_id": "xb_tel", "current_page": "/monitoring",
    })
    assert resp.status_code == 200
    resp.read()
    assert len(sink) == 1, "%s:实得 %d 条事件" % (kind, len(sink))
    ev = sink[0]
    assert set(ev) == set(ALLOWED_KEYS)
    assert ev["current_route"] == "/monitoring"
    assert ev["actor_kind"] == "agent"


def test_the_emitted_event_carries_no_user_text(monkeypatch):
    """真 HTTP 下复核一次:用户原文与答案正文都不许出现在事件里。"""
    sink = _capture_events(monkeypatch)
    client = _chat_client(monkeypatch, route_only=True)
    secret = "我的手机号是13800138000请回电"
    client.post("/api/xiaobang/chat", json={
        "message": secret, "session_id": "xb_tel2", "current_page": "/monitoring",
        "recent_turns": [{"role": "user", "content": secret}],
    }).read()
    assert len(sink) == 1
    blob = str(sink[0])
    assert "13800138000" not in blob, blob
    assert "手机号" not in blob, blob
    assert secret not in blob


def test_the_failing_branch_reports_its_error_kind(monkeypatch):
    """异常支必须把**错误类别**(不是堆栈、不是正文)记进事件。"""
    sink = _capture_events(monkeypatch)
    client = _chat_client(monkeypatch, boom=True)
    client.post("/api/xiaobang/chat", json={
        "message": "这个按钮点不了", "current_page": "/monitoring",
    }).read()
    assert sink[0]["error_kind"] == "RuntimeError", sink[0]
    assert sink[0]["answer_state"] == "error"
    assert sink[0]["exit_ids"], "异常支也必须有出口"
