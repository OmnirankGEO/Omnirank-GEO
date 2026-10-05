"""[工单 2026-08-03 ①] 完整回答定位锚点 · 后端锁。

🔴 这组锁存在的理由是**生产实测把工单前提推翻了**:
   2026-08-03 全表 613 行 `identity_evidence_snippet` **恒为 NULL**(±420 证据窗口
   从来没落过库),而 `response_snippet` 恒为 `full_response` 的前 500 字逐字前缀。
   所以"高亮证据窗口"在真实数据上没有锚点 —— 锚点必须退到"已看过的部分到此为止"。
   这些锁把两级锚点都钉住,并且钉住**优先级**:窗口有值时必须用窗口,不能被兜底吃掉。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.monitoring_db import build_identity_answer_anchor  # noqa: E402


# 测试数据两条前提,都是被实测打回来才补上的:
#   1. 全文必须真的长过窗口偏移 —— 初版只有 ~515 字,FULL[600:700] 切出空串,
#      "窗口"退化成空值被当作缺失,锁变恒真;
#   2. 每个位置的切片必须**唯一** —— 第二版用重复串,同一段 100 字在 510 处就出现过,
#      `find` 如实返回首次命中 510,断言 600 当场红。重复文本会让"定位对不对"无从判起。
# 所以这里用递增编号段落,任意切片全局唯一。
FULL = "".join(f"第{i:04d}段回答内容。" for i in range(100))
SEEN = FULL[:500]
assert len(FULL) > 700, "测试数据前提:全文必须长过窗口偏移"
assert FULL.count(FULL[600:700]) == 1, "测试数据前提:切片必须全局唯一,否则定位断言没有判别力"


def test_evidence_window_wins_when_present():
    """窗口有值且能逐字定位 → 必须用窗口的真实偏移(未来窗口落库后自动生效)。"""
    window = FULL[600:700]
    assert len(window) == 100, "窗口切片不能为空,否则本条锁恒真"
    got = build_identity_answer_anchor(FULL, SEEN, window)
    assert got["anchor"] == "evidence_window"
    assert got["start"] == 600
    assert got["end"] == 700
    # 反向对照:若被兜底吃掉,start 会等于 len(SEEN)=500 —— 明确断言它不是
    assert got["start"] != len(SEEN)


def test_falls_back_to_seen_prefix_boundary_when_window_missing():
    """生产现状(窗口 NULL)→ 定位到"已看过的 500 字之后"。"""
    got = build_identity_answer_anchor(FULL, SEEN, None)
    assert got["anchor"] == "seen_prefix"
    assert got["start"] == 500
    assert got["end"] == len(FULL)


def test_empty_string_window_is_treated_as_missing():
    """写入侧是 `... or ""`,所以空串和 NULL 都会出现,必须同等对待。"""
    assert build_identity_answer_anchor(FULL, SEEN, "")["anchor"] == "seen_prefix"


def test_window_not_found_in_full_text_does_not_fake_an_offset():
    """窗口存在但不在全文里(数据不一致)→ 不能硬造偏移,退兜底。"""
    got = build_identity_answer_anchor(FULL, SEEN, "这段文字全文里根本没有")
    assert got["anchor"] == "seen_prefix"


def test_snippet_not_a_prefix_yields_no_anchor():
    """片段不是前缀时,"已看过到此为止"不成立 —— 宁可不定位,也不给一个无意义的位置。"""
    got = build_identity_answer_anchor(FULL, "完全无关的片段", None)
    assert got["anchor"] == "none"
    assert got["start"] is None


def test_snippet_equal_to_full_text_yields_no_anchor():
    """全文没有比片段更多的内容 → 没有"后面还有"可定位。"""
    got = build_identity_answer_anchor(SEEN, SEEN, None)
    assert got["anchor"] == "none"


def test_empty_full_response_is_safe():
    assert build_identity_answer_anchor(None, SEEN, None)["anchor"] == "none"
    assert build_identity_answer_anchor("", SEEN, None)["anchor"] == "none"


@pytest.mark.parametrize("bad", ["evidence_window", "seen_prefix"])
def test_anchor_span_is_always_inside_full_text(bad):
    """任何锚点都必须落在全文范围内 —— 越界会让前端 slice 出空串,静默失去高亮。"""
    window = FULL[600:700] if bad == "evidence_window" else None
    got = build_identity_answer_anchor(FULL, SEEN, window)
    assert got["anchor"] == bad
    assert 0 <= got["start"] <= len(FULL)
    assert got["start"] < got["end"] <= len(FULL)
