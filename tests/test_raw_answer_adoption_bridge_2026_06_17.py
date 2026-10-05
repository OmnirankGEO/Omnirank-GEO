from services.research_monitor.raw_adoption_bridge import (
    build_raw_adoption_source_signal_payload,
    should_bridge_raw_adoption,
)


def _raw_row(**overrides):
    row = {
        "id": 123,
        "industry": "旅游酒店",
        "query": "旅游酒店推荐问题",
        "engine": "DeepSeek",
        "cite_position": 2,
        "cite_url": "https://example.com/adopted-source",
        "cite_title": "被答案采纳的旅游酒店文章",
        "answer_text": "据资料[2]显示，这篇文章被答案引用。",
        "is_answer_cited": True,
        "adoption_rank": 2,
        "batch_id": "batch_round_20260616_020000_014290",
        "researcher": "",
        "total_sources_in_answer": 35,
    }
    row.update(overrides)
    return row


def test_raw_answer_cited_row_becomes_answer_adopted_source_signal():
    payload = build_raw_adoption_source_signal_payload(_raw_row())

    assert payload["source_url"] == "https://example.com/adopted-source"
    assert payload["industry_key"] == "tourism_hotel"
    assert payload["engine"] == "deepseek"
    assert payload["prompt_id"] == "旅游酒店推荐问题"
    assert payload["signal_tier"] == "answer_adopted"
    assert payload["source_position"] == 2
    assert payload["total_sources_in_answer"] == 35
    assert payload["round_id"] == "batch_round_20260616_020000_014290"
    assert 0 < payload["balanced_weight"] < 1
    assert payload["metadata"]["raw_id"] == 123
    assert payload["metadata"]["adoption_rank"] == 2
    assert payload["metadata"]["source"] == "geo_research_raw_answer_bridge"


def test_adoption_rank_without_boolean_still_bridges_conservatively():
    payload = build_raw_adoption_source_signal_payload(
        _raw_row(is_answer_cited=False, adoption_rank=1)
    )

    assert should_bridge_raw_adoption(_raw_row(is_answer_cited=False, adoption_rank=1))
    assert payload["signal_tier"] == "answer_adopted"


def test_search_only_raw_row_is_not_bridged():
    row = _raw_row(is_answer_cited=False, adoption_rank=None)

    assert not should_bridge_raw_adoption(row)
