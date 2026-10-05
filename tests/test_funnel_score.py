"""tests/test_funnel_score — 漏斗 3 层加权评分回归

CTO-G 2026-04-27 · 真实 staging 样本对齐:
  diagnosis_id=135 重庆璧山万家装饰 → 30 分 危急级
  diagnosis_id=110 揭阳雅栖 → 0 分 隐形级
  主导级 sanity check
  数据不足 confidence='low' 行为
"""
from tools.scoring.funnel_score import (
    calculate_funnel_score,
    assert_funnel_score_consistency,
    render_funnel_progress_bar,
    FUNNEL_LAYERS,
)


def test_135_璧山_危急级():
    """重庆璧山万家装饰真实数据 · 4/4 + 5/20 + 0/8 → 30 危急"""
    r = calculate_funnel_score(
        brand_detected=4, brand_total=4,
        local_detected=5, local_total=20,
        scenario_detected=0, scenario_total=8,
    )
    assert r["total_score"] == 30, f"璧山样本期望 30 实得 {r['total_score']}"
    assert r["level"] == "危急级", f"璧山样本期望 危急级 实得 {r['level']}"
    assert r["layers"][0]["score"] == 20.0  # 品牌 100% × 20
    assert r["layers"][1]["score"] == 10.0  # 本地 25% × 40
    assert r["layers"][2]["score"] == 0.0   # 场景 0% × 40


def test_110_揭阳_隐形级():
    """揭阳雅栖 全 0 → 0 隐形"""
    r = calculate_funnel_score(
        brand_detected=0, brand_total=4,
        local_detected=0, local_total=20,
        scenario_detected=0, scenario_total=8,
    )
    assert r["total_score"] == 0
    assert r["level"] == "隐形级"


def test_主导级_85plus():
    """4/4 + 18/20 + 7/8 = 20 + 36 + 35 = 91 → 主导级"""
    r = calculate_funnel_score(
        brand_detected=4, brand_total=4,
        local_detected=18, local_total=20,
        scenario_detected=7, scenario_total=8,
    )
    assert r["total_score"] >= 85, f"主导级期望 >=85 实得 {r['total_score']}"
    assert r["level"] == "主导级"


def test_数据不足_低置信():
    """totals=0 → data_sufficient False · confidence low"""
    r = calculate_funnel_score(
        brand_detected=2, brand_total=2,
        local_detected=0, local_total=0,
        scenario_detected=0, scenario_total=0,
    )
    assert r["layers"][0]["confidence"] == "low"          # total=2 < 5
    assert r["layers"][1]["data_sufficient"] is False     # total=0
    assert r["layers"][2]["data_sufficient"] is False


def test_layer_keys_顺序正确():
    """3 层映射不能搞反: brand/local/scenario"""
    r = calculate_funnel_score(
        brand_detected=1, brand_total=1,
        local_detected=0, local_total=10,
        scenario_detected=0, scenario_total=5,
    )
    keys = [layer["key"] for layer in r["layers"]]
    assert keys == ["brand", "local", "scenario"]


def test_stats_key_映射_brand_awareness_regional_industry_super_tier1():
    """老板红线: ai_visibility_data.dimension_stats key 映射不能搞反"""
    expected = {
        "brand": "brand_awareness",
        "local": "regional_industry",
        "scenario": "super_tier1",
    }
    for layer in FUNNEL_LAYERS:
        assert expected[layer["key"]] == layer["stats_key"], (
            f"{layer['key']} 期望 stats_key={expected[layer['key']]} 实为 {layer['stats_key']}"
        )


def test_权重和等于100():
    """assert_funnel_score_consistency 防权重漂移"""
    assert assert_funnel_score_consistency() is True


def test_progress_bar():
    """ASCII 进度条 · 对齐 markdown 报告"""
    bar = render_funnel_progress_bar(0.0, width=20)
    assert bar == "░" * 20
    bar = render_funnel_progress_bar(1.0, width=20)
    assert bar == "█" * 20
    bar = render_funnel_progress_bar(0.25, width=20)
    assert bar.count("█") == 5 and bar.count("░") == 15


def test_score_inrange_clamp():
    """100% × all + 边界 score=100"""
    r = calculate_funnel_score(
        brand_detected=10, brand_total=10,
        local_detected=20, local_total=20,
        scenario_detected=10, scenario_total=10,
    )
    assert r["total_score"] == 100
    assert r["level"] == "主导级"


def test_level_meta_含business_meaning():
    """报告渲染依赖 business_meaning · 不能为空"""
    r = calculate_funnel_score(
        brand_detected=4, brand_total=4,
        local_detected=5, local_total=20,
        scenario_detected=0, scenario_total=8,
    )
    bm = r["level_meta"].get("business_meaning")
    assert isinstance(bm, str) and len(bm) > 0
