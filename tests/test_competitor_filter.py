"""
D4a · 竞品共现过滤纯逻辑单测(_filter_competitor_brands)。
跑法:python -m pytest tests/test_competitor_filter.py -q --noconftest -o addopts="" -p no:cacheprovider
"""
from tools.monitoring.batch_monitor import _filter_competitor_brands


def test_filter_target_brand_and_dedup():
    mentioned = ["远大电梯", "通力电梯", "远大电梯", "  ", "通力电梯有限公司"]
    out = _filter_competitor_brands(mentioned, target_brand="远大电梯")
    # 目标品牌别名剔除(远大电梯 + 含子串的)· 去重 · 去空
    assert "远大电梯" not in out
    assert "通力电梯" in out
    # 去重:通力电梯只一次
    assert out.count("通力电梯") == 1


def test_empty_and_none():
    assert _filter_competitor_brands([], "X") == []
    assert _filter_competitor_brands(None, "X") == []
    assert _filter_competitor_brands(["A", "B"], "") == ["A", "B"]  # 无目标品牌不过滤


def test_non_str_skipped():
    out = _filter_competitor_brands(["腾讯", 123, None, "阿里"], "百度")
    assert out == ["腾讯", "阿里"]


def test_target_substring_both_directions():
    # 目标品牌是某项子串 / 某项是目标品牌子串 都算别名剔除
    out = _filter_competitor_brands(["远大", "海康威视", "远大集团股份"], target_brand="远大集团")
    assert "海康威视" in out          # 与目标互不为子串 → 保留
    assert "远大" not in out          # "远大" 是 "远大集团" 子串 → 剔除
    assert "远大集团股份" not in out  # "远大集团" 是 "远大集团股份" 子串 → 剔除


def test_dedup_case_insensitive():
    out = _filter_competitor_brands(["Apple", "apple", "APPLE"], target_brand="谷歌")
    assert len(out) == 1
