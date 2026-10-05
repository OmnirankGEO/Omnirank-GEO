"""任务3 2026-06-08:LLM-first 需单独报价(对齐公式引擎 super_red_ocean)回归 · 纯 mock 不连真 DB。

覆盖:
  A. _classify_unquoted_keyword:商业信号 / 高价值 / 强商业意图 → 需单独报价(True);真信息型 → False
  B. _build_quote_data_from_llm:should_quote=false 高价值/超范畴 → super_red_ocean=true + 需单独报价 review_reason + 三档归0;
     真信息型 → super_red_ocean=false(信息型不报价);should_quote=true 正常词 → super_red_ocean=false
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.batch_pricing_llm import _classify_unquoted_keyword, _build_quote_data_from_llm


# ============================================================
# A. _classify_unquoted_keyword 判定
# ============================================================

def test_classify_commercial_signal_is_single_quote():
    """含商业信号(哪家好/推荐/排名/服务商)→ 需单独报价。"""
    assert _classify_unquoted_keyword("XX律师哪家好", 1.0, "informational") is True
    assert _classify_unquoted_keyword("深圳搬家公司推荐", 1.0, "informational") is True
    assert _classify_unquoted_keyword("留学中介排名", 1.0, "informational") is True
    assert _classify_unquoted_keyword("GEO 服务商", 1.0, "informational") is True
    assert _classify_unquoted_keyword("装修多少钱", 1.0, "informational") is True


def test_classify_high_value_is_single_quote():
    """LLM 判商业价值高(≥3.5)→ 超出标准范畴 → 需单独报价。"""
    assert _classify_unquoted_keyword("某高价值长尾词", 4.0, "informational") is True
    assert _classify_unquoted_keyword("某词", 3.5, "informational") is True


def test_classify_commercial_intent_is_single_quote():
    """强商业意图(transactional/commercial)→ 需单独报价。"""
    assert _classify_unquoted_keyword("某词", 1.0, "transactional") is True
    assert _classify_unquoted_keyword("某词", 1.0, "commercial") is True


def test_classify_true_info_is_not_single_quote():
    """真信息型(科普/流程/是什么 · 无商业信号 · 低价值)→ 信息型不报价。"""
    assert _classify_unquoted_keyword("GEO 是什么", 1.0, "informational") is False
    assert _classify_unquoted_keyword("SEO 优化流程", 2.0, "informational") is False
    assert _classify_unquoted_keyword("什么是搜索引擎", 0.5, "informational") is False


# ============================================================
# B. _build_quote_data_from_llm 标记
# ============================================================

def test_build_marks_high_value_unquoted_as_super_red_ocean():
    """should_quote=false 高价值/超范畴 → super_red_ocean=true + 需单独报价 reason + 三档归0。"""
    kws = [{"keyword": "律师哪家好", "should_quote": False, "value_score_0_5": 4.0, "intent": "commercial"}]
    qd = _build_quote_data_from_llm(kws, "客户", "", "", 0.25, None)
    d = qd["keyword_details"][0]
    assert d["super_red_ocean"] is True
    assert d["needs_single_quote"] is True
    assert "需单独报价" in d["review_reason"]
    assert d["entry_price"] == 0 and d["standard_price"] == 0 and d["flagship_price"] == 0
    assert d["super_red_ocean_level"] == "yellow"


def test_build_marks_true_info_as_not_super_red_ocean():
    """should_quote=false 真信息型 → super_red_ocean=false(信息型不报价·前端显「信息型·不报价」)。"""
    kws = [{"keyword": "GEO 是什么", "should_quote": False, "value_score_0_5": 1.0, "intent": "informational"}]
    qd = _build_quote_data_from_llm(kws, "客户", "", "", 0.25, None)
    d = qd["keyword_details"][0]
    assert d["super_red_ocean"] is False
    assert d["needs_single_quote"] is False
    assert d["should_quote"] is False
    assert d["entry_price"] == 0


def test_build_quoted_keyword_not_super_red_ocean():
    """should_quote=true 正常词 → super_red_ocean=false · 三档保留 LLM 价 · 进套餐。"""
    kws = [{"keyword": "正常词", "should_quote": True,
            "entry_price_yuan": 500, "standard_price_yuan": 800, "flagship_price_yuan": 1200,
            "value_score_0_5": 2.0}]
    qd = _build_quote_data_from_llm(kws, "客户", "", "", 0.25, None)
    d = qd["keyword_details"][0]
    assert d["super_red_ocean"] is False
    assert d["should_quote"] is True
    assert d["standard_price"] == 800
    assert qd["tier_summaries"]["标准版"]["final_price"] == 800


def test_build_mixed_batch_counts():
    """混合:1 正常 + 1 需单独报价 + 1 信息型 → quoted=1 · 套餐总价只含正常词。"""
    kws = [
        {"keyword": "正常词", "should_quote": True, "entry_price_yuan": 400,
         "standard_price_yuan": 700, "flagship_price_yuan": 1000, "value_score_0_5": 2.0},
        {"keyword": "律师哪家好", "should_quote": False, "value_score_0_5": 4.5, "intent": "commercial"},
        {"keyword": "GEO 是什么", "should_quote": False, "value_score_0_5": 0.5, "intent": "informational"},
    ]
    qd = _build_quote_data_from_llm(kws, "客户", "", "", 0.25, None)
    assert qd["quoted_keywords"] == 1
    assert qd["tier_summaries"]["标准版"]["final_price"] == 700  # 只正常词
    sro = [d for d in qd["keyword_details"] if d["super_red_ocean"]]
    info = [d for d in qd["keyword_details"] if not d["super_red_ocean"] and d["should_quote"] is False]
    assert len(sro) == 1 and sro[0]["keyword"] == "律师哪家好"
    assert len(info) == 1 and info[0]["keyword"] == "GEO 是什么"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
