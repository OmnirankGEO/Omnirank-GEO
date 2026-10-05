"""services/ai_ops/chat_intent.py 测试 · 全部 monkeypatch _call_llm seam,不真调 API。"""
import pytest

from services.ai_ops import chat_intent


@pytest.mark.asyncio
async def test_classify_parses_clean_json(monkeypatch):
    async def fake(prompt):
        return '{"intent": "fix"}'
    monkeypatch.setattr(chat_intent, '_call_llm', fake)
    assert await chat_intent.classify_intent("帮我修一下支付") == 'fix'


@pytest.mark.asyncio
async def test_classify_parses_json_wrapped_in_text(monkeypatch):
    async def fake(prompt):
        return '好的,分类结果:{"intent": "question_status"} 以上'
    monkeypatch.setattr(chat_intent, '_call_llm', fake)
    assert await chat_intent.classify_intent("系统怎么样") == 'question_status'


@pytest.mark.asyncio
async def test_classify_unknown_intent_returns_none(monkeypatch):
    # LLM 幻觉出不在集合里的意图 → None(降级规则),不放行任意字符串进路由
    async def fake(prompt):
        return '{"intent": "rm_rf_prod"}'
    monkeypatch.setattr(chat_intent, '_call_llm', fake)
    assert await chat_intent.classify_intent("x") is None


@pytest.mark.asyncio
async def test_classify_garbage_returns_none(monkeypatch):
    async def fake(prompt):
        return '我觉得这是一个修复请求'
    monkeypatch.setattr(chat_intent, '_call_llm', fake)
    assert await chat_intent.classify_intent("x") is None


@pytest.mark.asyncio
async def test_classify_llm_failure_returns_none(monkeypatch):
    async def fake(prompt):
        return None   # _call_llm 内部已把超时/HTTP错误吞成 None
    monkeypatch.setattr(chat_intent, '_call_llm', fake)
    assert await chat_intent.classify_intent("x") is None


@pytest.mark.asyncio
async def test_call_llm_without_key_returns_none(monkeypatch, real_llm_seams):
    # 无 key(本地/测试环境常态)→ None,不发任何网络请求。
    # 必须用 conftest 捕获的真实函数体——autouse 已把 chat_intent._call_llm
    # 换成假函数,直接调会测了个寂寞(包B.2 复审 P2-2)
    monkeypatch.setattr(chat_intent, '_get_api_key', lambda: "")
    assert await real_llm_seams["chat_intent._call_llm"]("prompt") is None


def test_prompt_prefix_covers_all_intents():
    # 提示词里意图集合与 INTENTS 常量对齐(防加意图漏改提示词)
    for intent in chat_intent.INTENTS:
        assert intent in chat_intent._PROMPT_PREFIX
