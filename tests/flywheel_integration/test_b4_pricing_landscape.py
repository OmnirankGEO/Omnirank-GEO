"""[B4] 回归:
- B4-1 flag 关(is_trust_asset_enabled=False)时飞轮 landscape 对报价零影响(逐字节一致)。
- landscape 因子数学 + flag 开时确实上调篇数。
- get_industry_citation_landscape 真跑 PG。
"""
import pytest

from tools import pricing_llm_assessor as A


def _ctx(landscape=None):
    trust = None
    if landscape is not None:
        trust = {
            "source": "missing",  # 让品牌信任 difficulty 块不跑,只隔离 landscape
            "trust_asset_score": None,
            "citation_readiness_score": None,
            "trust_asset_needs_review": True,
            "verified_labels": [],
            "missing_labels": [],
            "industry_citation_landscape": landscape,
        }
    return dict(
        keyword="测试词", measured_comp=30, saturated=False,
        metaso={"competition_count": 30, "content_count": 80},
        cost_multiplier=1.0, dynamic_cost=55.0, five118={}, metaso_fallback=False,
        trust_asset=trust,
    )


def _run(ctx):
    return A._assemble_result(ctx, None, {}, {}, 2.0, None)


# ---------- B4-1 flag 关逐字节一致 ----------
def test_landscape_zero_impact_when_flag_off(monkeypatch):
    monkeypatch.setattr("tools.llm_pricing_flag.is_trust_asset_enabled", lambda: False)
    concentrated = {"top_domain_share": 0.85, "total_signals": 200, "distinct_domains": 40}
    r_with = _run(_ctx(landscape=concentrated))
    r_without = _run(_ctx(landscape=None))
    # flag 关:_trust=None,landscape 块不跑 → 篇数/三档价逐字节一致
    assert r_with["true_competition"] == r_without["true_competition"]
    assert r_with["entry_price"] == r_without["entry_price"]
    assert r_with["standard_price"] == r_without["standard_price"]
    assert r_with["flagship_price"] == r_without["flagship_price"]
    assert "industry_landscape" not in (r_with.get("guards") or {})


# ---------- B4-1 flag 开:集中行业上调篇数 ----------
def test_landscape_uplifts_articles_when_flag_on(monkeypatch):
    monkeypatch.setattr("tools.llm_pricing_flag.is_trust_asset_enabled", lambda: True)
    concentrated = {"top_domain_share": 0.85, "total_signals": 200, "distinct_domains": 40}  # ≥ high_share → uplift
    r_land = _run(_ctx(landscape=concentrated))
    r_base = _run(_ctx(landscape=None))  # trust missing 无 landscape
    assert r_land["true_competition"] >= r_base["true_competition"]
    assert r_land["standard_price"] >= r_base["standard_price"]
    assert "industry_landscape" in (r_land.get("guards") or {})


def test_landscape_ignored_when_samples_insufficient(monkeypatch):
    monkeypatch.setattr("tools.llm_pricing_flag.is_trust_asset_enabled", lambda: True)
    thin = {"top_domain_share": 0.85, "total_signals": 3, "distinct_domains": 2}  # total_signals < min_sources(20)
    r_thin = _run(_ctx(landscape=thin))
    r_base = _run(_ctx(landscape=None))
    assert r_thin["true_competition"] == r_base["true_competition"]  # 样本不足 → 不消费
    assert "industry_landscape" not in (r_thin.get("guards") or {})


# ---------- landscape 因子数学 ----------
def test_industry_landscape_factor_math():
    f = A._industry_landscape_factor
    # 集中(share≥high) → max_uplift;分散(share≤low) → max_discount
    assert f(0.9, 0.15, 0.50, 1.10, 0.95) == pytest.approx(1.10)
    assert f(0.05, 0.15, 0.50, 1.10, 0.95) == pytest.approx(0.95)
    # 中点线性
    mid = f(0.325, 0.15, 0.50, 1.10, 0.95)  # t=0.5 → 0.95 + 0.5*(1.10-0.95)=1.025
    assert mid == pytest.approx(1.025, abs=1e-4)
    # 非法输入 → 1.0
    assert f("x", 0.15, 0.50, 1.10, 0.95) == 1.0
    assert f(0.3, 0.5, 0.5, 1.10, 0.95) == 1.0  # high<=low 保护


# ---------- get_industry_citation_landscape 真跑 PG ----------
def test_get_industry_citation_landscape_real_pg(db_with_clean_research):
    from db.geo_source_signals_db import init_geo_source_signal_tables
    init_geo_source_signal_tables()  # 建 geo_research_source_signals
    from services.source_authority_analyzer import get_industry_citation_landscape
    conn = db_with_clean_research
    c = conn.cursor()
    c.execute("DELETE FROM geo_research_source_signals")
    # 头部域名 A 占大头(集中) → top_domain_share 高
    rows = [("https://a.com/1", "a.com", 0.7), ("https://a.com/2", "a.com", 0.6),
            ("https://b.com/1", "b.com", 0.2), ("https://c.com/1", "c.com", 0.1)]
    for i, (url, domain, w) in enumerate(rows):
        c.execute(
            """INSERT INTO geo_research_source_signals
                   (source_url, domain, industry_key, engine, signal_tier, balanced_weight, prompt_id, round_id)
               VALUES (%s,%s,'testind','豆包','cited_source',%s,%s,%s)""",
            (url, domain, w, f"p{i}", f"r{i}"),
        )
    conn.commit()
    landscape = get_industry_citation_landscape("testind", conn=conn)
    assert landscape, "应有数据"
    # a.com 权重 1.3 / 总 1.6 = 0.8125
    assert landscape["top_domain_share"] == pytest.approx(0.8125, abs=0.01)
    assert landscape["distinct_domains"] == 3
    assert landscape["total_signals"] == 4
