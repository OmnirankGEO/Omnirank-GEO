"""治理 §12.3 · 客户确认关键要点不得静默截断出写作 prompt。

Before: ``writing/article_writer`` 用 ``_cn_list[:10]`` 把客户确认的关键要点静默
截断到前 10 条 —— 第 11 条起被悄悄丢出写作 prompt,客户明确确认的商业交付内容
没有进入文章(§12.3「静默损失商业交付的切片需要修」)。

这些判别锁定新行为:全部纳入(不再 [:10] 静默丢);仅当超过安全帽才保留前 cap
条并附一条**可见** overflow 说明(名出剩余条数),绝不静默丢弃。
"""
from writing.article_writer import _format_confirmed_notes


def test_all_notes_included_when_under_cap():
    notes = [f"要点{i}" for i in range(1, 16)]  # 15 条 > 旧的 10 静默上限
    seg = _format_confirmed_notes(notes)
    # 旧行为会静默丢掉第 11-15 条;新行为全部纳入
    for n in notes:
        assert n in seg, f"客户确认要点 {n} 被静默丢弃"
    assert "另有" not in seg  # 未超帽,无 overflow 提示


def test_over_cap_keeps_visible_overflow_marker_not_silent():
    notes = [f"点{i}" for i in range(60)]  # 60 > cap(50)
    seg = _format_confirmed_notes(notes, cap=50)
    # 前 50 条在
    assert "点0" in seg and "点49" in seg
    # 超出的 10 条不是静默消失 —— 必须有可见 overflow 说明并名出条数
    assert "另有 10 条" in seg


def test_empty_or_blank_returns_empty_string():
    assert _format_confirmed_notes([]) == ""
    assert _format_confirmed_notes(None) == ""
    assert _format_confirmed_notes(["", "   ", None]) == ""


def test_blank_items_are_dropped_but_real_ones_kept():
    seg = _format_confirmed_notes(["真要点", "  ", "", "另一要点"])
    assert "真要点" in seg and "另一要点" in seg
    # 头部标题仍在
    assert seg.startswith("**客户确认的关键要点**")
