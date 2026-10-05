"""任务 B-2 判别锁:Kimi 的"已联网"与"引用为 0"都必须是真话。

两个改前的谎:
  ① `web_search_enabled` **硬编码 True** —— 生产实证近 30 天 6935 次调用只有 553 次
     真带 `$web_search`(8%),即 92% 的格子被标成"已联网",其实是模型原生知识;
  ② 模型不按 `---REFERENCES---` 格式输出时,引用**静默**变 0,没有任何告警 ——
     所以这个洞存在很久没人发现。

变异验证(Review-CTO 复检会做):去掉告警分支 → `test_search_without_citations_raises_alarm` 转红。
"""
from __future__ import annotations

import asyncio
import json

import pytest


TOOL_CALL_ROUND = {
    "choices": [{
        "finish_reason": "tool_calls",
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_1",
                "function": {"name": "$web_search",
                             "arguments": json.dumps({"search_result": {"search_id": "x"}})},
            }],
        },
    }],
    "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
}


def _stop_round(content: str):
    return {
        "choices": [{"finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


def _run_kimi(monkeypatch, rounds, capsys=None):
    from tools.ai_visibility import ai_tester

    queue = list(rounds)

    async def _fake_post(client, url, **kwargs):
        return _FakeResponse(queue.pop(0))

    async def _fake_visibility(*_a, **_kw):
        return {"answer_summary": "ok", "mentioned_brands": [], "brand_detected": False}

    monkeypatch.setattr(ai_tester, "_tracked_post", _fake_post)
    monkeypatch.setattr(ai_tester, "_call_analyze_visibility", _fake_visibility)
    monkeypatch.setenv("KIMI_API_KEY", "sk-fake-kimi")
    result = asyncio.run(ai_tester.query_kimi_search("杭州装修公司哪家好", "某品牌"))
    return json.loads(result.content[0]["text"])


WITH_REFS = """杭州靠谱的装修公司有若干家。

---REFERENCES---
1. [2026杭州家装十家发布](https://m.jiemian.com/article/14745524.html)
2. [杭州装修口碑观察](https://news.qq.com/rain/a/20260713A07M3F00)
"""


def test_search_actually_happened_is_reported_truthfully(monkeypatch):
    result = _run_kimi(monkeypatch, [TOOL_CALL_ROUND, _stop_round(WITH_REFS)])
    assert result["web_search_enabled"] is True
    assert result["web_search_call_count"] == 1
    assert len(result["search_citations"]) == 2


def test_no_search_must_not_claim_web_search_enabled(monkeypatch):
    """🔒 改前这里硬编码 True —— 92% 的格子因此谎称"已联网"。"""
    result = _run_kimi(monkeypatch, [_stop_round("我直接回答,没有联网。")])
    assert result["web_search_enabled"] is False, "没搜就不能标已联网"
    assert result["web_search_call_count"] == 0
    assert result["search_citations"] == []
    # 没搜过就不算"解析降级",不许误报
    assert not result.get("citation_parse_degraded")


def test_search_without_citations_raises_alarm(monkeypatch, capsys):
    """🔒 变异锁:去掉告警分支 → 此测转红。

    触发了搜索却解析不到来源 = 解析口径与模型输出脱节,必须留痕,不许静默 0。
    """
    result = _run_kimi(monkeypatch, [TOOL_CALL_ROUND, _stop_round("答案正文,但我没按格式给来源。")])
    assert result["web_search_call_count"] == 1
    assert result["search_citations"] == []
    assert result.get("citation_parse_degraded") is True, "静默吞了:解析失败没有任何标记"
    assert "未解析到任何来源" in capsys.readouterr().out, "解析失败必须打日志"


def test_reference_parser_dedupes_and_strips_body(monkeypatch):
    from tools.ai_visibility.ai_tester import _parse_kimi_references

    body, citations = _parse_kimi_references(
        "正文内容。\n\n---REFERENCES---\n"
        "1. [A](https://a.com/1)\n2. [B](https://b.com/2)\n3. [A 重复](https://a.com/1)\n"
    )
    assert body == "正文内容。"
    assert [c["url"] for c in citations] == ["https://a.com/1", "https://b.com/2"]


def test_parser_degrades_without_separator_but_keeps_body():
    """无分隔符时正文不能被吃掉(降级要保内容)。"""
    from tools.ai_visibility.ai_tester import _parse_kimi_references

    body, citations = _parse_kimi_references("只有正文没有来源段。")
    assert body == "只有正文没有来源段。"
    assert citations == []
