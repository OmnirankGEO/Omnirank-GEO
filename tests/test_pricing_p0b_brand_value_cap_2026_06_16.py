"""报价 P0-B 前置:品牌词 value 上限单测(2026-06-16)。

防 P0-B(LLM_FIRST_PRICING_VALUE_EVIDENCE_GATE_ENABLED)放松证据闸后,把品牌防守/考虑词
(栖舍 / 栖舍官网 / 栖舍怎么样)当通用决策词抬价。上界 · 只降不升 · 非品牌词不受影响。

覆盖:
  - _is_brand_keyword / _brand_value_cap 纯函数(brand_owned + brand_name 子串双识别 · 四类规则)
  - P0-B 开:栖舍/栖舍官网 → value ≤1.0(vm≤1.25);栖舍怎么样 → value 1.5(vm 1.375·不吃满)
  - P0-B 开:深圳装修哪家好 / 别墅电梯定制(非品牌)→ 不受品牌上限
  - P0-B 开:品牌+业务+真实5118证据 → 不限(rule 3)
  - P0-B 关(当前 P0-A-live):品牌词已被证据闸钳 1.0 → 品牌 cap no-op = 0 变化

跑法:ALLOW_NONTEST_DB=1 TEST_DATABASE_URL=占位 PYTHONIOENCODING=utf-8 \
       python -m pytest tests/test_pricing_p0b_brand_value_cap_2026_06_16.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.pricing_llm_assessor as A   # noqa: E402
import tools.llm_pricing_flag as F       # noqa: E402

LLM_META = {"llm_deviation_pct": 0.0, "llm_used": "dual"}
_F5_EVID = {"search_volume": 800, "sem_price": 12.0, "bidword_company_count": 40}  # 有 5118 证据
_F5_NONE: dict = {}                                                                 # 无 5118 证据


def _ctx(keyword, brand_name="栖舍", five118=None, comp=20, **kw):
    base = dict(keyword=keyword, brand_name=brand_name, measured_comp=comp, saturated=False,
                metaso={"competition_count": comp, "content_count": 30}, cost_multiplier=1.0,
                dynamic_cost=90.0, five118=(five118 or {}), metaso_fallback=False)
    base.update(kw)
    return base


def _judged(keyword_type="brand_owned", value_signal=2.15, comp=20, **kw):
    base = dict(true_competition=comp, cost_per_article_suggested=90.0, keyword_type=keyword_type,
                city=None, city_tier="national", media_tier_required="B", value_signal=value_signal,
                reasoning="x", risk_flags=[], confidence=0.8, national_unclamped=False)
    base.update(kw)
    return base


def _flags(mp, cost=False, value=False, nat=False, trust=False):
    # cost=False:P0-A 不读快照(本批与成本无关·只验 value 侧)·与 P0-A live 无关
    mp.setattr(F, "is_cost_snapshot_enabled", lambda: cost)
    mp.setattr(F, "is_value_evidence_gate_relaxed", lambda: value)   # P0-B
    mp.setattr(F, "is_national_unclamped_enabled", lambda: nat)
    mp.setattr(F, "is_trust_asset_enabled", lambda: trust)


# ============================================================
# 1. 纯函数:品牌识别 + 四类上限规则
# ============================================================

def test_is_brand_keyword():
    assert A._is_brand_keyword("栖舍怎么样", "national_niche", "栖舍") is True   # brand_name 子串
    assert A._is_brand_keyword("xyz产品", "brand_owned", None) is True           # LLM 判 brand_owned
    assert A._is_brand_keyword("深圳装修哪家好", "local_city", "栖舍") is False    # 非品牌
    assert A._is_brand_keyword("安心装修", "local_city", "安") is False           # 品牌名 <2 字不匹配


def test_brand_value_cap_rules():
    # 非品牌 → 不限
    assert A._brand_value_cap("深圳装修哪家好", "local_city", "栖舍") == 3.0
    assert A._brand_value_cap("别墅电梯定制", "national_niche", "栖舍") == 3.0
    # 品牌导航词 → 1.0
    assert A._brand_value_cap("栖舍官网", "brand_owned", "栖舍") == 1.0
    assert A._brand_value_cap("栖舍电话", "brand_owned", "栖舍") == 1.0
    assert A._brand_value_cap("栖舍地址", "brand_owned", "栖舍") == 1.0
    # 品牌考虑/口碑词 → 1.5
    assert A._brand_value_cap("栖舍怎么样", "brand_owned", "栖舍") == 1.5
    assert A._brand_value_cap("栖舍靠谱吗", "national_niche", "栖舍") == 1.5      # 靠 brand_name 子串识别
    assert A._brand_value_cap("栖舍口碑", "brand_owned", "栖舍") == 1.5
    # 纯品牌名(无证据)→ 1.0
    assert A._brand_value_cap("栖舍", "brand_owned", "栖舍") == 1.0
    # 品牌 + 业务词 + 真实证据 → 不限(rule 3)
    assert A._brand_value_cap("栖舍防水补漏", "brand_owned", "栖舍", has_evidence=True) == 3.0
    # 品牌 + 业务词 但无证据 → 1.0(保守)
    assert A._brand_value_cap("栖舍防水补漏", "brand_owned", "栖舍", has_evidence=False) == 1.0
    # 考虑词即使有证据仍 1.5(意图优先于证据 · 口碑词不放飞)
    assert A._brand_value_cap("栖舍怎么样", "brand_owned", "栖舍", has_evidence=True) == 1.5


# ============================================================
# 2. _assemble_result 集成:P0-B 开
# ============================================================

def test_p0b_on_pure_brand_name_not_inflated(monkeypatch):
    # 栖舍(纯品牌·无5118)P0-B 开 → brand cap 1.0 → vm ≤ 1.25
    _flags(monkeypatch, value=True)
    r = A._assemble_result(_ctx("栖舍", five118=_F5_NONE), _judged(value_signal=2.15),
                           LLM_META, {}, 2.0, None)
    assert r["value_signal"] <= 1.0 and r["value_multiplier"] <= 1.25
    assert "brand_value_cap" in r["guards"]


def test_p0b_on_brand_official_site_not_inflated(monkeypatch):
    # 栖舍官网(导航·无5118)P0-B 开 → 1.0
    _flags(monkeypatch, value=True)
    r = A._assemble_result(_ctx("栖舍官网", five118=_F5_NONE), _judged(value_signal=2.5),
                           LLM_META, {}, 2.0, None)
    assert r["value_signal"] <= 1.0 and r["value_multiplier"] <= 1.25


def test_p0b_on_brand_consideration_small_value_not_full(monkeypatch):
    # 栖舍怎么样(考虑·无5118)P0-B 开 → cap 1.5 → vm 1.375(只允许小幅·不吃满 1.75)
    _flags(monkeypatch, value=True)
    r = A._assemble_result(_ctx("栖舍怎么样", five118=_F5_NONE), _judged(value_signal=2.15),
                           LLM_META, {}, 2.0, None)
    assert r["value_signal"] == 1.5
    assert r["value_multiplier"] == pytest.approx(1.375, abs=0.001)
    assert r["value_multiplier"] < 1.75                      # 不吃满
    assert r["guards"]["brand_value_cap"].endswith("->1.5")


def test_p0b_on_generic_city_word_unaffected(monkeypatch):
    # 深圳装修哪家好(非品牌·有5118)P0-B 开 → 不受品牌上限
    _flags(monkeypatch, value=True)
    r = A._assemble_result(_ctx("深圳装修哪家好", brand_name="栖舍", five118=_F5_EVID),
                           _judged(keyword_type="local_city", value_signal=2.2),
                           LLM_META, {}, 2.0, None)
    assert "brand_value_cap" not in r["guards"]
    assert r["value_signal"] == pytest.approx(2.2, abs=0.01)


def test_p0b_on_villa_elevator_unaffected(monkeypatch):
    # 别墅电梯定制(非品牌·有5118)P0-B 开 → 不受品牌上限
    _flags(monkeypatch, value=True)
    r = A._assemble_result(_ctx("别墅电梯定制", brand_name="栖舍", five118=_F5_EVID),
                           _judged(keyword_type="national_niche", value_signal=2.8),
                           LLM_META, {}, 2.0, None)
    assert "brand_value_cap" not in r["guards"]
    assert r["value_signal"] == pytest.approx(2.8, abs=0.01)


def test_p0b_on_brand_business_with_evidence_kept(monkeypatch):
    # 品牌+业务词+真实5118证据 → 不限(rule 3·有竞争/证据支撑)
    _flags(monkeypatch, value=True)
    r = A._assemble_result(_ctx("栖舍防水补漏", five118=_F5_EVID),
                           _judged(value_signal=2.0, keyword_type="brand_owned"),
                           LLM_META, {}, 2.0, None)
    assert "brand_value_cap" not in r["guards"]
    assert r["value_signal"] == pytest.approx(2.0, abs=0.01)


# ============================================================
# 3. P0-B 关(当前 P0-A-live):0 变化铁证
# ============================================================

def test_p0b_off_brand_word_unchanged(monkeypatch):
    # P0-B 关:栖舍怎么样 no-5118 → 已被证据闸钳到 1.0 → 品牌 cap min(1.0,1.5) no-op → vm 1.25(0 变化)
    _flags(monkeypatch, value=False)
    r = A._assemble_result(_ctx("栖舍怎么样", five118=_F5_NONE), _judged(value_signal=2.15),
                           LLM_META, {}, 2.0, None)
    assert r["value_signal"] == 1.0 and r["value_multiplier"] == 1.25
    assert "brand_value_cap" not in r["guards"]              # 证据闸已钳·品牌 cap 不重复触发
    assert "value_evidence_cap" in r["guards"]               # 现状证据闸仍在


def test_p0b_off_generic_word_unchanged(monkeypatch):
    # P0-B 关:非品牌有证据词 → 品牌 cap 不碰 · 与现状一致
    _flags(monkeypatch, value=False)
    r = A._assemble_result(_ctx("深圳装修哪家好", brand_name="栖舍", five118=_F5_EVID),
                           _judged(keyword_type="local_city", value_signal=2.2),
                           LLM_META, {}, 2.0, None)
    assert "brand_value_cap" not in r["guards"]
    assert r["value_signal"] == pytest.approx(2.2, abs=0.01)


# ============================================================
# 4. [Codex 返修] P0-B 关 + 品牌词【有 5118 证据】→ 不触发 brand_value_cap(防 0 变化被破)
#    根因:证据闸 not has_5118_evidence 不钳"有证据"词 → 品牌 cap 若不门控会在 P0-B 关时改价。
# ============================================================

def test_p0b_off_brand_nav_with_evidence_not_capped(monkeypatch):
    # P0-B 关 + 栖舍官网 + 有 5118 证据 → brand_value_cap 不触发 · value_signal 保持 raw(2.5)
    _flags(monkeypatch, value=False)
    r = A._assemble_result(_ctx("栖舍官网", five118=_F5_EVID), _judged(value_signal=2.5),
                           LLM_META, {}, 2.0, None)
    assert "brand_value_cap" not in r["guards"]
    assert r["value_signal"] == pytest.approx(2.5, abs=0.01)


def test_p0b_off_brand_consideration_with_evidence_not_capped(monkeypatch):
    # P0-B 关 + 栖舍怎么样 + 有 5118 证据 → brand_value_cap 不触发 · value_signal 保持 raw(2.15)
    _flags(monkeypatch, value=False)
    r = A._assemble_result(_ctx("栖舍怎么样", five118=_F5_EVID), _judged(value_signal=2.15),
                           LLM_META, {}, 2.0, None)
    assert "brand_value_cap" not in r["guards"]
    assert r["value_signal"] == pytest.approx(2.15, abs=0.01)


def test_p0b_on_brand_nav_with_evidence_still_capped(monkeypatch):
    # 对照:P0-B 开 + 栖舍官网 + 有证据 → 导航词仍 cap 1.0(导航不因证据豁免)
    _flags(monkeypatch, value=True)
    r = A._assemble_result(_ctx("栖舍官网", five118=_F5_EVID), _judged(value_signal=2.5),
                           LLM_META, {}, 2.0, None)
    assert r["value_signal"] == 1.0 and "brand_value_cap" in r["guards"]


def test_p0b_on_brand_consideration_with_evidence_still_capped(monkeypatch):
    # 对照:P0-B 开 + 栖舍怎么样 + 有证据 → 考虑词仍 cap 1.5(意图优先于证据)
    _flags(monkeypatch, value=True)
    r = A._assemble_result(_ctx("栖舍怎么样", five118=_F5_EVID), _judged(value_signal=2.15),
                           LLM_META, {}, 2.0, None)
    assert r["value_signal"] == 1.5 and "brand_value_cap" in r["guards"]
