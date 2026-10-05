"""零检索兜底判别锁(Owner 2026-07-27 拍板"不阻断流程")。

处理顺序(2026-07-27 起):**首发就带检索软约束** → 零检索则软重试 N 次(N=3)
→ 仍为零则降级阿里通道。首发带约束是 24 题实测的结果(命中 100% vs 83.3%,且更快更省),
它把"零检索→重试"从常规路径变成几乎不触发的保险。

本文件的重点**不是**"兜底能跑通",而是 **兜底必须是「标记清楚的降级」**:
  · `provider` 落 **dashscope**(不是 deepseek_official)—— 否则报表写着 DeepSeek、数据来自阿里;
  · 每条来源带 `via=dashscope_fallback` —— 下游飞轮域分布 / B3 媒体组合可据此排除,
    免得阿里检索器的域分布污染"AI 真正引用谁"的判断;
  · `fallback_used` / `fallback_reason` 落库,且打日志 —— 不静默。

少任何一条,这一格就会变成"71% DeepSeek + 29% 阿里"的混合体 —— 那比统一用阿里更糟,
同比环比分不清是引擎变了还是当天兜底比例变了。工单 §5 红线禁止的正是这个。
"""
from __future__ import annotations

import asyncio
import json

import pytest


def _official_payload(*, search_calls: int):
    """官方响应。search_calls=0 → 没检索(实测形状:usage 里没有 server_tool_use)。"""
    content = [{"type": "text", "text": "答案正文"}]
    usage = {"input_tokens": 11, "output_tokens": 300}
    if search_calls:
        content = [
            {"type": "server_tool_use", "id": "c1", "name": "web_search", "input": {"query": "q"}},
            {"type": "web_search_tool_result", "tool_use_id": "c1", "content": [
                {"type": "web_search_result", "title": "官方来源",
                 "url": "https://official.example/a", "page_age": None,
                 "encrypted_content": "x" * 50},
            ]},
            {"type": "text", "text": "答案正文"},
        ]
        usage = {"input_tokens": 65000, "output_tokens": 1200,
                 "server_tool_use": {"web_search_requests": search_calls}}
    #: 🔴 [WO_221-c1] 官方响应的回显名改成现在真的那个;
    #:   本文件的百炼兜底臂走 `query_dashscope_deepseek` 桩,与这里无关。
    return {"id": "ds", "model": "deepseek-flash", "content": content, "usage": usage}


class _Resp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._p


ALI_RESULT = {
    "answer_summary": "阿里通道答案",
    "mentioned_brands": [], "brand_detected": False,
    "web_search_enabled": True,
    "search_citations": [
        {"url": "https://ali.example/1", "title": "阿里来源1"},
        {"url": "https://ali.example/2", "title": "阿里来源2"},
    ],
}


@pytest.fixture
def wired(monkeypatch):
    """官方按 search_calls 序列返回;阿里兜底返回固定结果。记录调用轨迹。"""
    from tools.ai_visibility import ai_tester

    state = {"official_bodies": [], "ali_calls": 0, "seq": []}

    async def _fake_post(client, url, *, platform=None, model=None, metadata=None, **kwargs):
        body = kwargs.get("json") or {}
        state["official_bodies"].append(body)
        calls = state["seq"][len(state["official_bodies"]) - 1] \
            if len(state["official_bodies"]) <= len(state["seq"]) else 0
        return _Resp(_official_payload(search_calls=calls))

    async def _fake_ali(query, check_brand="", max_tokens=2000, brand_id=None,
                       brand_display_names=None):
        state["ali_calls"] += 1
        from tools.ai_visibility.ai_tester import ToolResponse
        return ToolResponse(content=[{
            "type": "text",
            "text": json.dumps(json.loads(json.dumps(ALI_RESULT)), ensure_ascii=False),
        }])

    async def _fake_vis(*_a, **_kw):
        return {"answer_summary": "ok", "mentioned_brands": [], "brand_detected": False}

    monkeypatch.setattr(ai_tester, "_tracked_post", _fake_post)
    monkeypatch.setattr(ai_tester, "query_dashscope_deepseek", _fake_ali)
    monkeypatch.setattr(ai_tester, "_call_analyze_visibility", _fake_vis)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake")
    monkeypatch.setenv("DEEPSEEK_ZERO_SEARCH_RETRIES", "1")
    monkeypatch.setenv("DEEPSEEK_ZERO_SEARCH_FALLBACK", "1")
    return state


def _run(state, seq):
    from tools.ai_visibility.ai_tester import query_deepseek_official

    state["seq"] = seq
    r = asyncio.run(query_deepseek_official("杭州装修公司哪家好", "某品牌"))
    return json.loads(r.content[0]["text"])


# ---------------------------------------------------------------------------
def test_first_round_carries_search_hint_by_default(wired):
    """[2026-07-27 契约变更] 首发**就带**检索软约束。

    原断言是"首次保持模型自然行为、不带 system"。24 题实测推翻了这个取舍:
    首发带约束的命中率 100% vs 不带 83.3%,而且**更快更省**(延迟中位 12.0→11.7s,
    input 521k→513k)—— 因为它把"零检索→重试"整条链消掉了。
    "模型自然行为"这个纯净口径可用 DEEPSEEK_SEARCH_HINT_FIRST=0 找回(见下一条)。
    """
    _run(wired, [2])
    assert len(wired["official_bodies"]) == 1
    assert "web_search" in wired["official_bodies"][0]["system"]
    assert wired["ali_calls"] == 0


def test_zero_search_triggers_soft_retry_with_system_hint(wired):
    """🔒 零检索 → 第二轮带"必须先 web_search"的 system 软约束(实测 3/3 救回)。"""
    result = _run(wired, [0, 2])
    assert len(wired["official_bodies"]) == 2
    # [2026-07-27] 两轮都带软约束(首发已默认带);重试的意义从"加约束"变成"再试一次"。
    assert all("web_search" in b["system"] for b in wired["official_bodies"])
    # 软重试成功 → 用官方结果,**不**兜底
    assert wired["ali_calls"] == 0
    assert result.get("fallback_used") is not True
    assert result["search_citations"][0]["url"] == "https://official.example/a"


def test_soft_retry_never_uses_tool_choice(wired):
    """🔒 软重试用 system 软约束,**绝不**用 tool_choice 强制。

    6 题实测强制会让模型一直调工具、6/6 没有正文 text 块,已定论不可用。
    """
    _run(wired, [0, 2])
    for body in wired["official_bodies"]:
        assert "tool_choice" not in body


def test_fallback_after_retries_exhausted(wired):
    """软重试用尽仍零检索 → 降级阿里,且**标记齐全**。"""
    result = _run(wired, [0, 0])
    assert len(wired["official_bodies"]) == 2, "应先把软重试额度用完"
    assert wired["ali_calls"] == 1

    assert result["fallback_used"] is True
    assert result["fallback_provider"] == "dashscope"
    assert "official_zero_search" in result["fallback_reason"]
    # 🔴 驱动血缘:batch_monitor 靠这个把 provider 落成 dashscope
    assert result["search_mode"] == "dashscope_fallback"


def test_fallback_citations_are_tagged_for_downstream_isolation(wired):
    """🔒 每条兜底来源必须带 via 标记 —— 下游飞轮/媒体组合据此排除,防域分布污染。"""
    result = _run(wired, [0, 0])
    citations = result["search_citations"]
    assert citations, "兜底应带回阿里的来源"
    assert all(c["via"] == "dashscope_fallback" for c in citations), \
        "兜底来源没打标 → 阿里检索器的域分布会混进「AI 真正引用谁」的统计"


def test_fallback_can_be_switched_off(wired, monkeypatch):
    """关掉兜底 → 如实返回零检索,不拿别家数据填。"""
    monkeypatch.setenv("DEEPSEEK_ZERO_SEARCH_FALLBACK", "0")
    result = _run(wired, [0, 0])
    assert wired["ali_calls"] == 0
    assert result.get("fallback_used") is not True
    assert result["web_search_enabled"] is False
    assert result["search_citations"] == []


def test_retry_count_is_configurable(wired, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_ZERO_SEARCH_RETRIES", "2")
    _run(wired, [0, 0, 0])
    assert len(wired["official_bodies"]) == 3, "应做 1 次首发 + 2 次软重试"
    assert wired["ali_calls"] == 1


def test_zero_retries_falls_back_immediately(wired, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_ZERO_SEARCH_RETRIES", "0")
    _run(wired, [0])
    assert len(wired["official_bodies"]) == 1
    assert wired["ali_calls"] == 1


def test_fallback_failure_keeps_honest_zero(wired, monkeypatch):
    """兜底自己也挂 → 保留官方那次的诚实零检索,不伪造。"""
    from tools.ai_visibility import ai_tester
    from tools.ai_visibility.ai_tester import ToolResponse

    async def _broken_ali(*_a, **_kw):
        return ToolResponse(content=[{"type": "text", "text": json.dumps({
            "engine_error": True, "answer_summary": "阿里也挂了"}, ensure_ascii=False)}])

    monkeypatch.setattr(ai_tester, "query_dashscope_deepseek", _broken_ali)
    result = _run(wired, [0, 0])
    assert result.get("fallback_used") is not True
    assert result["web_search_enabled"] is False


# ---------------------------------------------------------------------------
# 血缘:兜底那一格必须落 dashscope
# ---------------------------------------------------------------------------
def test_lineage_reports_dashscope_when_fallback_used():
    """🔒 红线锁:兜底数据的 provider 必须是 dashscope,不能继续写 deepseek_official。"""
    from tools.monitoring.batch_monitor import _resolve_runtime_lineage

    provider, model, surface, mode = _resolve_runtime_lineage("deepseek", "dashscope_fallback")
    assert provider == "dashscope", "兜底却写 deepseek_official = 报表说 DeepSeek 实际是阿里"
    assert surface == "deepseek_dashscope_search_legacy"
    assert mode == "dashscope_fallback"


def test_lineage_still_official_on_normal_path():
    from tools.monitoring.batch_monitor import _resolve_runtime_lineage

    provider, _m, _s, mode = _resolve_runtime_lineage("deepseek", "deepseek_native")
    assert provider == "deepseek_official"
    assert mode == "deepseek_native"


# ---------------------------------------------------------------------------
# 效率优化:首发就带检索软约束(2026-07-27 实测驱动)
# ---------------------------------------------------------------------------
def test_search_hint_is_sent_on_first_round_by_default(wired):
    """🔒 24 题实测:首发带 system 把命中率 83.3% → 100%,延迟中位 12.0→11.7s,input 略降。

    它把"零检索 → 重试"这条链**整个消掉**,而不是让它跑得更快 ——
    重试与兜底因此从常规路径退化成几乎不触发的保险。
    """
    result = _run(wired, [2])
    assert len(wired["official_bodies"]) == 1, "首发就该命中,不需要重试"
    assert "web_search" in wired["official_bodies"][0]["system"], "首发没带检索软约束"
    assert wired["ali_calls"] == 0
    assert result["search_citations"]


def test_hint_first_can_be_switched_off_for_comparison(wired, monkeypatch):
    """留对照口径:关掉开关退回"先自然、零检索再带"的老行为。"""
    monkeypatch.setenv("DEEPSEEK_SEARCH_HINT_FIRST", "0")
    _run(wired, [0, 2])
    assert "system" not in wired["official_bodies"][0], "关掉后首发不该带 system"
    assert "web_search" in wired["official_bodies"][1]["system"]


def test_retry_and_fallback_still_work_with_hint_first(wired, monkeypatch):
    """首发带约束后仍要保留保险:极端情况下重试与兜底照常生效。"""
    monkeypatch.setenv("DEEPSEEK_ZERO_SEARCH_RETRIES", "3")  # Owner 定的生产默认
    result = _run(wired, [0, 0, 0, 0])
    assert len(wired["official_bodies"]) == 4, "N=3 → 1 次首发 + 3 次重试"
    assert wired["ali_calls"] == 1
    assert result["fallback_used"] is True


def test_default_retry_rounds_is_three():
    """Owner 2026-07-27 定:重试 3 次不行就兜底。"""
    from tools.ai_visibility.ai_tester import DEEPSEEK_ZERO_SEARCH_RETRIES_DEFAULT

    assert DEEPSEEK_ZERO_SEARCH_RETRIES_DEFAULT == 3


def test_flywheel_also_sends_hint_first():
    """飞轮与监测/诊断同口径,不能一边带一边不带。"""
    import inspect

    from services.research_monitor import platforms as fw

    src = inspect.getsource(fw.query_deepseek)
    assert '"system": DEEPSEEK_OFFICIAL_FORCE_SEARCH_SYSTEM' in src
