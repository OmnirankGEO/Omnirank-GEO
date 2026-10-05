"""A4 前半段:结构建议 payload 必须带回策略身份,否则指派账本无从归因。

修前 `build_guidance_payload` 只吐"写法内容",不吐"这是哪个版本"。于是
`record_article_fingerprint` 从来拿不到 strategy_id —— 生产 182 行文章指纹里 strategy_id 全是 NULL。
"""
from __future__ import annotations

from services.writing_structure_guidance import build_guidance_payload


def test_active_strategy_row_carries_identity():
    payload = build_guidance_payload(
        {
            "id": 4242,
            "industry_key": "geo_test",
            "strategy_version": "v-2026-07",
            "style_family": "guide",
            "guidance": "先给选择标准",
            "common_elements": ["先给标准"],
            "guardrails": ["不承诺固定效果"],
            "source_summary": {},
            "confidence": 0.6,
        },
        quote_id=123,
        industry="geo_test",
    )
    assert payload["strategy_id"] == 4242
    assert payload["strategy_version"] == "v-2026-07"
    assert payload["resolution"] == "active_strategy"


def test_industry_baseline_row_is_labelled_baseline():
    """行业基线是现造的 row(没有 id)—— 必须落成 baseline,不能冒充 active 版本。"""
    payload = build_guidance_payload(
        {
            "industry_key": "geo_test",
            "style_family": "guide",
            "guidance": "按行业范文基线",
            "common_elements": [],
            "guardrails": [],
            "source_summary": {},
            "confidence": 0.5,
        },
        quote_id=124,
        industry="geo_test",
    )
    assert payload["strategy_id"] is None
    assert payload["resolution"] == "industry_baseline"


def test_no_strategy_falls_back_to_system_default():
    payload = build_guidance_payload(None, quote_id=125, industry="geo_test")
    assert payload["strategy_id"] is None
    assert payload["strategy_version"] is None
    assert payload["resolution"] == "system_default"


def test_generator_passes_strategy_identity_into_fingerprint():
    """生成端必须把 topic 里的策略身份透传给指纹写入,否则这条链还是断的。"""
    import inspect

    from writing import article_generator_service

    source = inspect.getsource(article_generator_service)
    start = source.index("            record_article_fingerprint(")
    call_block = source[start:source.index("\n            )", start)]
    assert 'strategy_id=topic.get("_strategy_id")' in call_block
    assert 'strategy_version=topic.get("_strategy_version")' in call_block
