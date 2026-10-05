from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRITING_HALL = ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx"


def _source() -> str:
    return WRITING_HALL.read_text(encoding="utf-8")


def test_writing_hall_hides_placement_recommendation_tab():
    src = _source()

    assert 'value="placement"' not in src
    assert "PlacementRecommendation" not in src
    assert "投放建议" not in src


def test_write_timeout_topics_remain_non_writable():
    src = _source()

    writable_section = src[src.index("const selectedWritableIds"):src.index("const selectedWritableCount")]
    assert "write_timeout" not in writable_section
    assert "topic.status === 'write_timeout'" in src


def test_daily_writing_flow_uses_six_family_per_topic_controls_only():
    src = _source()

    # The old global nine-angle recommendation controls stay removed. Pending
    # rows may use the six-family selector; completed/writing rows show lineage.
    assert "全部:系统推荐" not in src
    assert "系统推荐写法" not in src
    assert "使用系统推荐写法" not in src
    assert "每条待写文章右侧可直接下拉选择文章方向" not in src
    assert src.count("<TopicStyleSelector") >= 3
    assert "topic.status === 'pending' || topic.status === 'failed'" in src
    assert "标题已经决定文章方向" in src
