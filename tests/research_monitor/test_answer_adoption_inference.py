from services.research_monitor.answer_adoption import (
    extract_answer_citation_indices,
    infer_answer_adoption,
)


def test_extract_indices_allows_chinese_adjacent_markers():
    answer = "据携程介绍[2]，来源[1]显示，见参考[10]。arr[3] 是代码下标。"
    assert extract_answer_citation_indices(answer) == {1, 2, 10}


def test_infer_adopted_when_cite_position_is_in_answer_markers():
    row = {
        "answer_text": "推荐来源[1]，也可以参考携程[3]。",
        "cite_position": 3,
        "engine": "deepseek",
    }
    result = infer_answer_adoption(row)
    assert result["is_answer_cited"] is True
    assert result["adoption_rank"] == 3
    assert result["reason"] == "answer_marker"


def test_infer_not_adopted_when_marker_does_not_match_row_position():
    row = {
        "answer_text": "推荐来源[1]，也可以参考携程[3]。",
        "cite_position": 2,
        "engine": "deepseek",
    }
    result = infer_answer_adoption(row)
    assert result["is_answer_cited"] is False
    assert result["adoption_rank"] is None
    assert result["reason"] == "marker_mismatch"


def test_no_marker_stays_unproven_not_adopted():
    row = {
        "answer_text": "这是一段没有角标的回答。",
        "cite_position": 1,
        "engine": "kimi",
    }
    result = infer_answer_adoption(row)
    assert result["is_answer_cited"] is False
    assert result["adoption_rank"] is None
    assert result["reason"] == "no_answer_marker"
