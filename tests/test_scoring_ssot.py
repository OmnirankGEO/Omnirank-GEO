"""
test_scoring_ssot.py · B3 (CTO-15.9 session 3 · 2026-04-25)

元指令 14 · 评分 SSOT 一致性测试:
  - LEVEL_META 6 等级完整 · min/max 无 gap 无 overlap
  - get_level / get_meta 任意分数返一致 level + meta
  - build_cover_conclusion 包含 level summary
  - report_writer_v2 module 1 cover 引用 SSOT(scoring_levels.build_cover_conclusion)
  - tools/scoring/geo_scorer.py summary 引用 SSOT(get_summary)

防止三处写 3 版互相打架(顶部等级 / 正文等级表 / 前端徽章)的复发。
"""
from __future__ import annotations

import pytest


def test_level_meta_completeness():
    """LEVEL_META 6 个等级 · 字段齐全"""
    from tools.scoring.scoring_levels import LEVEL_META

    assert len(LEVEL_META) == 6, f"应有 6 个等级 · 实有 {len(LEVEL_META)}"
    expected_levels = {"领先", "成熟", "成长", "起步", "待提升", "空白"}
    assert set(LEVEL_META.keys()) == expected_levels

    required_fields = {"label", "min", "max", "color", "badge_class", "summary"}
    for name, meta in LEVEL_META.items():
        assert required_fields.issubset(meta.keys()), f"{name} 缺字段:{required_fields - set(meta.keys())}"
        assert isinstance(meta["min"], int) and isinstance(meta["max"], int)
        assert meta["min"] <= meta["max"], f"{name} min({meta['min']}) > max({meta['max']})"


def test_level_meta_no_gap_no_overlap():
    """min/max 边界:0-100 全覆盖 · 无 gap 无 overlap"""
    from tools.scoring.scoring_levels import LEVEL_META

    ranges = sorted([(int(m["min"]), int(m["max"]), name) for name, m in LEVEL_META.items()])
    assert ranges[0][0] == 0, f"最低区间应从 0 开始 · 实 {ranges[0][0]}"
    assert ranges[-1][1] == 100, f"最高区间应到 100 · 实 {ranges[-1][1]}"

    for i in range(len(ranges) - 1):
        cur_max = ranges[i][1]
        next_min = ranges[i + 1][0]
        assert next_min == cur_max + 1, (
            f"区间不连续 · {ranges[i][2]}({cur_max}) → {ranges[i+1][2]}({next_min}) · 缺 gap 或 overlap"
        )


@pytest.mark.parametrize("score,expected_level", [
    (0, "空白"),
    (10, "空白"),
    (19, "空白"),
    (20, "待提升"),
    (39, "待提升"),
    (40, "起步"),
    (54, "起步"),
    (55, "成长"),
    (69, "成长"),
    (70, "成熟"),
    (84, "成熟"),
    (85, "领先"),
    (100, "领先"),
    (None, "空白"),
])
def test_get_level_boundary(score, expected_level):
    """边界分数 → 等级映射唯一"""
    from tools.scoring.scoring_levels import get_level
    assert get_level(score) == expected_level


def test_get_meta_returns_full_meta():
    """get_meta 返完整 meta(level 名 + 所有字段)"""
    from tools.scoring.scoring_levels import get_meta
    meta = get_meta(72)
    assert meta["level"] == "成熟"
    assert meta["color"] == "green"
    assert "summary" in meta
    assert "badge_class" in meta


def test_build_cover_conclusion_includes_level():
    """封面结论包含 level summary"""
    from tools.scoring.scoring_levels import build_cover_conclusion
    text = build_cover_conclusion(72, industry="装修")
    # 必须包含等级名(顶部结论一致性)
    assert "成熟" in text or "成熟级" in text


def test_report_v2_module1_uses_ssot():
    """report_writer_v2 module 1 cover 必须调 SSOT(get_meta + build_cover_conclusion)"""
    import services.report_writer_v2 as rw
    src = open(rw.__file__, "r", encoding="utf-8").read()
    assert "from tools.scoring.scoring_levels import" in src, "module 1 应 import SSOT"
    assert "get_meta" in src, "module 1 应调 get_meta"
    assert "build_cover_conclusion" in src, "module 1 应调 build_cover_conclusion"


def test_geo_scorer_uses_ssot_get_summary():
    """tools/scoring/geo_scorer.py summary 应走 get_summary SSOT(A.2 已完成)"""
    import importlib.util
    import os
    scorer_path = os.path.join(os.path.dirname(__file__), "..", "tools", "scoring", "geo_scorer.py")
    if not os.path.exists(scorer_path):
        pytest.skip("geo_scorer.py 不存在 · 跳过(可能模块路径变化)")
    src = open(scorer_path, "r", encoding="utf-8").read()
    assert "get_summary" in src, "geo_scorer 应调 scoring_levels.get_summary(SSOT 元指令 14)"


def test_no_hardcoded_level_text_in_report_writer():
    """防退化:report_writer_v2 不应再硬编码 '85 分以上' / '70-84' 类文本"""
    import services.report_writer_v2 as rw
    src = open(rw.__file__, "r", encoding="utf-8").read()
    forbidden_phrases = [
        "85 分以上",
        "70-84",
        "55-69",
    ]
    for p in forbidden_phrases:
        assert p not in src, f"report_writer_v2 含硬编码区间文本 '{p}' · 违反 SSOT"
