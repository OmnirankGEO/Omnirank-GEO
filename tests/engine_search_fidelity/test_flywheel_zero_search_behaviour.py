"""飞轮零检索安全网 · **行为级**判别锁(R2 复核返修 · 2026-07-27)。

为什么另起一个文件:上一版守这层的是
`test_r2_rework.py::test_flywheel_retry_and_fallback_are_implemented_not_just_documented`,
它靠**源码 grep** 三个锚点(`_retry_budget` / `_deepseek_dashscope_fallback_for_research` /
`search_requests == 0`)。复检 AI 用三种方式打残实现,**三次全部 300 全绿**:

  · 整段删除重试        → 锚点仍在 → 没转红
  · 只把 `> 0` 改成 `< 0` → 源码文本一字未少 → 没转红
  · 关掉兜底            → 没转红

那条测试的名字就叫 "implemented_not_just_documented",而它本身正是"名字写了、没兑现"的同一类问题。
飞轮的整个零检索安全网当时**行为级零覆盖** —— 监测侧有 `_run(wired, [0,0,0,0])` 的 harness,飞轮侧没有。

本文件照搬监测侧那套 harness:喂一串"第 N 次调用检索几次"的序列,断言**真实调用次数与真实返回值**。
上面三种破坏方式必须全部转红(见文件末尾的自验说明)。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from services.research_monitor import platforms as fw


def _official_payload(search_calls: int):
    """官方 Anthropic 响应。search_calls=0 → usage 里没有 server_tool_use(实测形状)。"""
    if search_calls <= 0:
        return {
            "id": "fw-zero", "type": "message", "role": "assistant",
            "model": "deepseek-flash", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "我直接回答,没有检索。"}],
            "usage": {"input_tokens": 11, "output_tokens": 300},
        }
    return {
        "id": "fw-hit", "type": "message", "role": "assistant",
        "model": "deepseek-flash", "stop_reason": "end_turn",
        "content": [
            {"type": "server_tool_use", "id": "c1", "name": "web_search", "input": {"query": "q"}},
            {"type": "web_search_tool_result", "tool_use_id": "c1", "content": [
                {"type": "web_search_result", "title": "官方来源",
                 "url": "https://official.example/1", "page_age": None,
                 "encrypted_content": "x" * 30},
            ]},
            {"type": "text", "text": "结论[1]。"},
        ],
        "usage": {"input_tokens": 70000, "output_tokens": 1200,
                  "server_tool_use": {"web_search_requests": search_calls}},
    }


DASHSCOPE_RESULT = {
    "platform": "deepseek", "provider": "dashscope", "model": "deepseek-v4-flash",
    "model_revision": "unknown", "surface": "ai_search", "search_mode": "enable_search",
    "prompt_id": 1, "prompt": "q", "answer": "百炼回答",
    "citations": [
        {"url": "https://ali.example/1", "title": "阿里来源1", "rank": 1, "answer_ranks": [1]},
        {"url": "https://ali.example/2", "title": "阿里来源2", "rank": 2, "answer_ranks": [2]},
    ],
    "ok": True, "error": None, "raw": {"output": {}},
}


@pytest.fixture
def fw_wired(monkeypatch):
    """驱动 query_deepseek:按 seq 决定第 N 次官方调用返回几次检索。"""
    state: dict = {"bodies": [], "fallback_calls": 0, "seq": [],
                   "fallback_result": "ok", "custom_payload": None}

    class _Resp:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            state["bodies"].append(kw.get("json") or {})
            if state.get("custom_payload") is not None:
                return _Resp(state["custom_payload"])
            idx = len(state["bodies"]) - 1
            calls = state["seq"][idx] if idx < len(state["seq"]) else 0
            return _Resp(_official_payload(calls))

    async def _fake_dashscope(prompt_id, prompt):
        state["fallback_calls"] += 1
        if state["fallback_result"] == "raise":
            raise RuntimeError("百炼也挂了")
        if state["fallback_result"] == "not_ok":
            return {"ok": False, "error": "boom"}
        return json.loads(json.dumps(DASHSCOPE_RESULT))

    class _T:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def record(self, **kw):
            return None

    monkeypatch.setattr(fw.httpx, "AsyncClient", lambda **kw: _Client())
    monkeypatch.setattr(fw, "_query_deepseek_via_dashscope", _fake_dashscope)
    monkeypatch.setattr(fw, "llm_track", lambda *a, **k: _T())
    #: 🔴 [WO_221-c1'] 这里**刻意留着旧名**:配置里写旧名、发出去必须是新名,
    #:   正好把「发出前归一」这条防线顺带测到。Anthropic 端点回显原样,
    #:   不归一的话这一路会静默照发旧名。
    monkeypatch.setattr(fw, "_load_model_from_config", lambda *a, **k: "deepseek-v4-flash")
    monkeypatch.setattr(fw, "mark_answer_cited_sources", lambda ans, cits, raw=None: cits)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake-official")
    monkeypatch.setenv("DEEPSEEK_ZERO_SEARCH_RETRIES", "3")
    monkeypatch.setenv("DEEPSEEK_ZERO_SEARCH_FALLBACK", "1")
    return state


def _run(state, seq):
    state["seq"] = seq
    return asyncio.run(fw.query_deepseek(1, "杭州装修公司哪家好"))


# ---------------------------------------------------------------------------
# 🔒 三条对应复检 AI 打残实现的三种方式
# ---------------------------------------------------------------------------
def test_zero_search_retries_exactly_budget_times_then_falls_back(fw_wired):
    """🔒 N=3 且始终零检索 → **恰好 4 次官方调用**(1 首发 + 3 重试)+ 1 次兜底。

    对应破坏方式 ①「整段删除重试」与 ②「> 0 改 < 0」:两者都会让官方调用变成 1 次 → 本测转红。
    """
    result = _run(fw_wired, [0, 0, 0, 0])
    assert len(fw_wired["bodies"]) == 4, (
        f"官方调用 {len(fw_wired['bodies'])} 次,期望 4 次(1 首发 + 3 重试)—— 重试没接通"
    )
    assert fw_wired["fallback_calls"] == 1
    assert result["fallback_used"] is True


def test_retry_stops_as_soon_as_search_hits(fw_wired):
    """命中就停,不空跑剩余预算。"""
    result = _run(fw_wired, [0, 0, 2, 2])
    assert len(fw_wired["bodies"]) == 3, "第 3 次已命中,不该再重试"
    assert fw_wired["fallback_calls"] == 0
    assert result["provider"] == "deepseek_official"
    assert result.get("fallback_used") is not True


def test_fallback_can_be_disabled_and_stays_honest(fw_wired, monkeypatch):
    """🔒 对应破坏方式 ③「关掉兜底」——关掉后必须如实返回零检索,不拿别家数据填。"""
    monkeypatch.setenv("DEEPSEEK_ZERO_SEARCH_FALLBACK", "0")
    result = _run(fw_wired, [0, 0, 0, 0])
    assert len(fw_wired["bodies"]) == 4, "关兜底不影响重试"
    assert fw_wired["fallback_calls"] == 0
    assert result["provider"] == "deepseek_official"
    assert result["web_search_request_count"] == 0
    assert result.get("fallback_used") is not True


def test_fallback_is_invoked_when_enabled(fw_wired):
    """🔒 兜底开着时必须真的被调到(与上一条构成开/关对照)。"""
    _run(fw_wired, [0, 0, 0, 0])
    assert fw_wired["fallback_calls"] == 1, "兜底开着却一次没调 —— 安全网没接通"


# ---------------------------------------------------------------------------
# 兜底的「标记清楚的降级」—— 行为级,不再 grep 源码
# ---------------------------------------------------------------------------
def test_fallback_result_carries_full_degradation_markers(fw_wired):
    result = _run(fw_wired, [0, 0, 0, 0])
    assert result["provider"] == "dashscope", "兜底却写 deepseek_official = 报表说 DeepSeek 实际是阿里"
    assert result["search_mode"] == "dashscope_fallback"
    assert result["fallback_used"] is True
    assert "official_zero_search" in result["fallback_reason"]
    assert result["citations"], "兜底应带回百炼的来源"
    assert all(c["via"] == "dashscope_fallback" for c in result["citations"]), \
        "兜底来源没打 via → 阿里检索器的域分布会混进「AI 真正引用谁」的统计"


@pytest.mark.parametrize("mode", ["raise", "not_ok"])
def test_fallback_failure_keeps_honest_zero(fw_wired, mode):
    """兜底自己挂了(抛异常 / 返回 ok=False)→ 保留官方那次的诚实零检索,不伪造。"""
    fw_wired["fallback_result"] = mode
    result = _run(fw_wired, [0, 0, 0, 0])
    assert fw_wired["fallback_calls"] == 1
    assert result["provider"] == "deepseek_official"
    assert result["web_search_request_count"] == 0
    assert result.get("fallback_used") is not True


def test_retry_budget_is_configurable(fw_wired, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_ZERO_SEARCH_RETRIES", "1")
    _run(fw_wired, [0, 0])
    assert len(fw_wired["bodies"]) == 2, "1 首发 + 1 重试"
    assert fw_wired["fallback_calls"] == 1


def test_zero_retries_falls_back_immediately(fw_wired, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_ZERO_SEARCH_RETRIES", "0")
    _run(fw_wired, [0])
    assert len(fw_wired["bodies"]) == 1
    assert fw_wired["fallback_calls"] == 1


# ---------------------------------------------------------------------------
# 角标指令 & rank 口径 —— 也从 grep 换成行为级
# ---------------------------------------------------------------------------
def test_marker_instruction_actually_reaches_the_model(fw_wired):
    """🔒 角标指令必须真的出现在**发出去的 user content** 里,不是只写在源码里。"""
    _run(fw_wired, [2])
    user_content = fw_wired["bodies"][0]["messages"][0]["content"]
    assert user_content.startswith("杭州装修公司哪家好")
    assert "[n]" in user_content and "内联标注" in user_content, \
        "角标指令没随 prompt 发出 → 模型不输出 [n] → is_answer_cited 全 False"


def test_every_retry_also_carries_the_marker(fw_wired):
    """重试轮同样要带角标指令,否则救回检索却丢了采纳。"""
    _run(fw_wired, [0, 0, 2])
    assert len(fw_wired["bodies"]) == 3
    for body in fw_wired["bodies"]:
        assert "[n]" in body["messages"][0]["content"]


def test_rank_accumulates_across_search_rounds(fw_wired):
    """rank 按原始流序**跨轮累加**(含重复不去重)—— 与角标指令说的"本次搜索结果序号"对齐。"""
    fw_wired["custom_payload"] = {
        "id": "x", "type": "message", "role": "assistant", "model": "deepseek-flash",
        "content": [
            {"type": "web_search_tool_result", "content": [
                {"type": "web_search_result", "title": "A", "url": "https://a/1"},
                {"type": "web_search_result", "title": "B", "url": "https://b/2"},
            ]},
            {"type": "web_search_tool_result", "content": [
                {"type": "web_search_result", "title": "A 重复", "url": "https://a/1"},
                {"type": "web_search_result", "title": "C", "url": "https://c/3"},
            ]},
            {"type": "text", "text": "正文[1][4]"},
        ],
        "usage": {"server_tool_use": {"web_search_requests": 2}},
    }
    r = _run(fw_wired, [2])
    by_url = {c["url"]: c for c in r["citations"]}
    assert [c["url"] for c in r["citations"]] == ["https://a/1", "https://b/2", "https://c/3"]
    assert by_url["https://a/1"]["answer_ranks"] == [1, 3], "跨轮重复应累加原始序号,不是压缩去重"
    assert by_url["https://c/3"]["rank"] == 4, "第二轮第二条的原始序号是 4"


# ---------------------------------------------------------------------------
# 自验说明(给复检 AI)
# ---------------------------------------------------------------------------
# 三种破坏方式与应转红的用例:
#   ① 整段删除重试分支            → test_zero_search_retries_exactly_budget_times_then_falls_back
#                                   test_retry_budget_is_configurable / test_every_retry_also_carries_the_marker
#   ② `_retry_budget > 0` 改 `< 0` → 同上(源码文本不变,但行为变 → 必转红)
#   ③ 关掉兜底(_fallback_enabled 恒 False)
#                                 → test_fallback_is_invoked_when_enabled
#                                   test_fallback_result_carries_full_degradation_markers
#                                   test_fallback_failure_keeps_honest_zero
