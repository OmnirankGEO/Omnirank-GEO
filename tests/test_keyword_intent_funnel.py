"""
D4b · 关键词 intent/funnel 写作方向 formatter 单测。
跑法:python -m pytest tests/test_keyword_intent_funnel.py -q --noconftest -o addopts="" -p no:cacheprovider
"""
from writing.ranking_prompt_v9 import format_keyword_intent_funnel


def test_known_intent_funnel():
    s = format_keyword_intent_funnel("transactional", "decision")
    assert "交易型" in s
    assert "决策阶段" in s
    assert "不跑题" in s


def test_null_fallback():
    s = format_keyword_intent_funnel(None, None)
    assert "通用" in s
    assert "认知阶段" in s


def test_unknown_fallback():
    s = format_keyword_intent_funnel("weird_intent", "weird_funnel")
    assert "通用" in s
    assert "认知阶段" in s


def test_case_insensitive():
    s = format_keyword_intent_funnel("INFORMATIONAL", "AWARENESS")
    assert "信息型" in s
    assert "认知阶段" in s


def test_commercial_consideration():
    s = format_keyword_intent_funnel("commercial", "consideration")
    assert "商业调研型" in s
    assert "考虑阶段" in s
