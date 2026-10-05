"""报价 P0-D 信任资产/引用难度因子 · 单元 + 集成测试(2026-06-14)。

覆盖(flag 默认关 = 0 生产报价变化 · 难度侧非利润侧):
  - flag-off & 缺采集 0 报价变化铁证(_assemble_result)
  - 采集判定 A/B/C + 7 类资产 + 评分(trust_asset_collector.build_trust_payload 纯函数)
  - 缺采集口径(build_trust_payload(None) / normalize 哨兵 / 报价只内部标不进 needs_review)
  - 难度杠杆生效(缺背书 → 篇数侧 true_competition 上调;背书足 → 反向降)
  - 三合一报价 needs_review(缺背书+高价值+高竞争);低值低竞争不淹没人审
  - trust_ratio 进 ratios 观测但【不进】lever 计数(compute_blowup_guards)
  - 新鲜度(collected 超龄 → stale + 内部 review)+ 唯一 active DDL
  - 客户端脱敏黑名单含 trust 内部字段 · 人话 label 不脱敏 · scorer 透传

跑法:ALLOW_NONTEST_DB=1 TEST_DATABASE_URL=占位 PYTHONIOENCODING=utf-8 \
       python -m pytest tests/test_pricing_p0d_trust_asset_2026_06_14.py -q
"""
import os
import re
import sys
import textwrap
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import tools.pricing_bands as B            # noqa: E402
import tools.pricing_llm_assessor as A     # noqa: E402
import tools.llm_pricing_flag as F         # noqa: E402
import tools.trust_asset_collector as C    # noqa: E402
import db.trust_asset_db as TDB            # noqa: E402


# ============================================================
# 1. _trust_difficulty_factor 纯函数(线性插值)
# ============================================================

def test_difficulty_factor_low_high_mid():
    # readiness ≤ low → max_uplift;≥ high → max_discount;中间线性
    assert A._trust_difficulty_factor(0.0, 0.3, 0.6, 1.25, 0.9) == 1.25
    assert A._trust_difficulty_factor(0.9, 0.3, 0.6, 1.25, 0.9) == 0.9
    mid = A._trust_difficulty_factor(0.45, 0.3, 0.6, 1.25, 0.9)
    assert 0.9 < mid < 1.25  # 中点 ≈ 1.075


def test_difficulty_factor_degenerate_cfg():
    # high <= low(配置异常)→ 1.0(不调难度·安全)
    assert A._trust_difficulty_factor(0.1, 0.6, 0.6, 1.25, 0.9) == 1.0
    assert A._trust_difficulty_factor("x", 0.3, 0.6, 1.25, 0.9) == 1.0  # 脏值 → 1.0


# ============================================================
# 2. build_trust_payload 采集判定 A/B/C(纯函数 · 无 DB/LLM)
# ============================================================

def test_build_payload_grades_ab():
    sap = {
        "top_sources": [
            {"domain": "people.com.cn", "tier": "tier1_national",
             "evidence_class": "real_ai_citation", "brand_direct": True, "ai_cited_count": 2},
            {"domain": "sohu.com", "tier": "tier2_portal_vertical",
             "evidence_class": "search_brand_direct", "brand_direct": True},
            {"domain": "baike.baidu.com", "tier": "structured_encyclopedia",
             "evidence_class": "industry_reference"},  # 品牌未检出 → 不算品牌背书
        ],
        "tier_counts": {"tier1_national": 1, "tier2_portal_vertical": 1,
                        "structured_encyclopedia": 1, "tier3_small_media_wemedia": 0,
                        "risk_low_quality": 0},
        "endorsement_score": 9,
        "data_quality": {"has_real_ai_citations": True},
    }
    p = C.build_trust_payload(sap)
    assert p["trust_asset_source"] == "collected"
    # 权威媒体(tier1 real_ai=A)已验证 · 百科 industry_reference 不计入
    vmap = {a["type"]: a["grade"] for a in p["verified_trust_assets"]}
    assert vmap.get("authority_media_coverage") == "A"
    assert "encyclopedia" not in vmap  # industry_reference 不算验证
    # readiness 有真实 AI 引用 → 不封顶 · endorsement 9/12=0.75
    assert p["citation_readiness_score"] == pytest.approx(0.75, abs=0.01)
    assert p["trust_asset_score"] > 0
    # missing 含未覆盖类
    miss = {a["type"] for a in p["missing_trust_assets"]}
    assert "official_site" in miss and "business_registry" in miss


def test_build_payload_declared_only_is_missing():
    # [Codex#2 返修] 仅自报官网/权威源(无真实 SAP 采样)≠ 缺背书 → source=missing + readiness=None
    #   → 不进价格因子(不调篇数/不触发报价 needs_review)· 未知不惩罚(§5.1)。
    p = C.build_trust_payload(
        {"top_sources": [], "tier_counts": {}, "endorsement_score": 0,
         "data_quality": {"has_real_ai_citations": False}},
        official_website="https://mybrand.com",
        declared_authority_sources=["人民网报道(无链接)", "https://gov.cn/notice/123"])
    assert p["trust_asset_source"] == "missing"          # 仅声明无采样 → missing
    assert p["citation_readiness_score"] is None          # 不参与篇数因子
    assert p["trust_asset_score"] is None
    assert p["verified_trust_assets"] == [] and p["missing_trust_assets"] == []
    # 声明仍留痕审计(evidence·标 declared_only_no_sampling)· 但不驱动价格
    assert p["trust_asset_evidence"].get("note") == "declared_only_no_sampling"
    assert p["trust_asset_evidence"]["assets"]["official_site"]["grade"] == "C"


def test_build_payload_real_sampling_no_brand_cited_is_collected():
    # 真实采样发生(top_sources 非空)但品牌未被引用(industry_reference)→ collected + readiness 低
    #   = 真·缺背书(可调篇数)· 与"仅声明"区分
    p = C.build_trust_payload(
        {"top_sources": [{"domain": "x.com", "tier": "tier2_portal_vertical",
                          "evidence_class": "industry_reference"}],
         "tier_counts": {"tier2_portal_vertical": 1}, "endorsement_score": 0,
         "data_quality": {"has_real_ai_citations": False}},
        official_website="https://mybrand.com")
    assert p["trust_asset_source"] == "collected"   # 探测发生过
    assert p["verified_trust_assets"] == []          # 品牌未被引用 → 无 A/B
    assert p["citation_readiness_score"] == 0.0       # 真·缺背书(可驱动篇数上调)


def test_build_payload_none_is_missing():
    # 完全无信号 → source=missing + 内部 needs_review(设计 §5.1)· 不抬价由消费端保证
    p = C.build_trust_payload(None)
    assert p["trust_asset_source"] == "missing"
    assert p["trust_asset_needs_review"] is True
    assert p["verified_trust_assets"] == []


def test_build_payload_risk_dominant_needs_review():
    # 风险来源占优 → 内部 needs_review(供人工核 · 不直接拦报价)
    sap = {"top_sources": [{"domain": "x.tk", "tier": "risk_low_quality",
                            "evidence_class": "search_brand_direct"}],
           "tier_counts": {"risk_low_quality": 3, "tier1_national": 0,
                           "tier2_portal_vertical": 0, "structured_encyclopedia": 0},
           "endorsement_score": 0, "data_quality": {"has_real_ai_citations": False}}
    p = C.build_trust_payload(sap)
    assert p["trust_asset_needs_review"] is True


# ============================================================
# 3. normalize_trust_asset_for_pricing(纯函数 · 新鲜度)
# ============================================================

def test_normalize_none_missing_sentinel():
    n = TDB.normalize_trust_asset_for_pricing(None)
    assert n["source"] == "missing" and n["trust_asset_needs_review"] is True
    assert n["citation_readiness_score"] is None


def test_normalize_collected_passthrough_labels():
    row = {"trust_asset_source": "collected", "trust_asset_score": 0.4,
           "citation_readiness_score": 0.7, "trust_asset_needs_review": False,
           "generated_at": datetime.now(timezone.utc),
           "verified_trust_assets": [{"type": "official_site", "label": "已有官网", "grade": "B"}],
           "missing_trust_assets": [{"type": "encyclopedia", "label": "百科页面"}]}
    n = TDB.normalize_trust_asset_for_pricing(row, stale_days=30)
    assert n["source"] == "collected" and n["citation_readiness_score"] == 0.7
    assert n["verified_labels"] == ["已有官网"] and n["missing_labels"] == ["百科页面"]


def test_normalize_stale_when_old():
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    old = now - timedelta(days=40)
    row = {"trust_asset_source": "collected", "citation_readiness_score": 0.5,
           "trust_asset_needs_review": False, "generated_at": old,
           "verified_trust_assets": [], "missing_trust_assets": []}
    n = TDB.normalize_trust_asset_for_pricing(row, stale_days=30, now=now)
    assert n["source"] == "stale" and n["trust_asset_needs_review"] is True


def test_ddl_has_unique_active_index():
    joined = "\n".join(TDB.TRUST_ASSET_DDL_STATEMENTS)
    assert "uq_trust_asset_active" in joined
    assert "WHERE is_active" in joined            # 唯一 partial 索引(防同品牌两条 active)
    assert "brand_trust_asset_snapshot" in joined


# ============================================================
# 4. compute_blowup_guards:trust_ratio 进 ratios 观测但不进 lever 计数
# ============================================================

CFG = {"factory_ceiling_niche": 6500.0, "factory_ceiling_local": 3500.0, "no_cache_selling": 10000.0,
       "lever_two": 1.5, "lever_three": 1.3, "national_unclamped_factory_ratio": 2.0}


def _t(factory, selling=0):
    return {"flagship": {"factory_price": factory, "selling_price": selling}}


def _bl(cost, vm, comp, factory):
    return {"cost": cost, "value_mult": vm, "true_competition": comp, "factory_price": factory}


def test_guard_p0d_only_proceeds_and_echoes_trust_ratio():
    # P0-D 单开(A/B/C 全关)→ 不早返 · 算 ratio · trust_ratio 写入 ratios
    r = B.compute_blowup_guards(tiers=_t(2000, 4000), keyword_type="local_city", value_mult=1.0,
                                national_unclamped=False, p0a_on=False, p0b_relaxed=False,
                                p0c_on=False, p0d_on=True, cfg=CFG,
                                cost=55.0, true_competition=25, baseline=_bl(55.0, 1.0, 20, 1800.0),
                                trust_ratio=1.25)
    assert r["ratios"]["trust_ratio"] == 1.25
    assert r["ratios"]["comp_ratio"] == pytest.approx(1.25, abs=0.01)  # 25/20 篇数侧调整


def test_guard_trust_ratio_not_in_lever_count():
    # trust_ratio 高(2.0)但 cost/value/comp 都=1 → 不触发 lever_two(trust 不进计数 · 设计 §5.2)
    r = B.compute_blowup_guards(tiers=_t(2000, 4000), keyword_type="local_city", value_mult=1.0,
                                national_unclamped=False, p0a_on=False, p0b_relaxed=False,
                                p0c_on=False, p0d_on=True, cfg=CFG,
                                cost=55.0, true_competition=20, baseline=_bl(55.0, 1.0, 20, 2000.0),
                                trust_ratio=2.0)
    assert r["ratios"]["trust_ratio"] == 2.0
    assert "lever_two_compound" not in r["guards"] and r["needs_review"] is False


def test_guard_all_flags_off_inert_even_with_trust():
    # 四 flag 全关(含 p0d_on=False)→ 早返 0 变化(即便传 trust_ratio)
    r = B.compute_blowup_guards(tiers=_t(99999, 99999), keyword_type="national_niche", value_mult=1.75,
                                national_unclamped=True, p0a_on=False, p0b_relaxed=False, p0c_on=False,
                                p0d_on=False, cfg=CFG, cost=999.0, true_competition=99,
                                baseline=_bl(55.0, 1.0, 30, 800.0), trust_ratio=1.25)
    assert r == {"needs_review": False, "guarantee_unavailable": False, "no_cache": False, "guards": {}}


# ============================================================
# 5. _assemble_result 集成:flag-off 0 变化 / 缺采集 / 难度杠杆 / 三合一
# ============================================================

LLM_META = {"llm_deviation_pct": 0.0, "llm_used": "dual"}
_F5 = {"search_volume": 800, "sem_price": 12.0, "bidword_company_count": 40}


def _ctx(trust=None, five118=None, comp=20, **kw):
    base = dict(keyword="别墅电梯定制", measured_comp=comp, saturated=False,
                metaso={"competition_count": comp, "content_count": 30}, cost_multiplier=1.0,
                dynamic_cost=90.0, five118=(five118 or {}), metaso_fallback=False)
    if trust is not None:
        base["trust_asset"] = trust
    base.update(kw)
    return base


def _judged(comp=20, value_signal=3.0, **kw):
    base = dict(true_competition=comp, cost_per_article_suggested=90.0, keyword_type="national_niche",
                city=None, city_tier="national", media_tier_required="A", value_signal=value_signal,
                reasoning="x", risk_flags=[], confidence=0.8, national_unclamped=False)
    base.update(kw)
    return base


def _trust(source="collected", readiness=0.1, score=0.1, needs_review=False, verified=None, missing=None):
    return {"source": source, "trust_asset_score": score, "citation_readiness_score": readiness,
            "trust_asset_needs_review": needs_review,
            "verified_labels": verified or [], "missing_labels": missing or []}


def _flags(mp, cost=False, value=False, nat=False, trust=False):
    mp.setattr(F, "is_cost_snapshot_enabled", lambda: cost)
    mp.setattr(F, "is_value_evidence_gate_relaxed", lambda: value)
    mp.setattr(F, "is_national_unclamped_enabled", lambda: nat)
    mp.setattr(F, "is_trust_asset_enabled", lambda: trust)


def _prices(r):
    return (r["entry_price"], r["standard_price"], r["flagship_price"],
            r["entry_articles"], r["standard_articles"], r["flagship_articles"], r["true_competition"])


def test_assemble_flag_off_trust_no_change(monkeypatch):
    # flag 关 → 即便 ctx 注入 trust_asset(缺背书)· 价/篇/竞争完全不变 + trust 字段恒 None/默认
    _flags(monkeypatch, trust=False)
    base = A._assemble_result(_ctx(five118=_F5), _judged(), LLM_META, {}, 2.0, None)
    withtrust = A._assemble_result(_ctx(trust=_trust(readiness=0.05), five118=_F5),
                                   _judged(), LLM_META, {}, 2.0, None)
    assert _prices(base) == _prices(withtrust)               # 0 变化铁证
    assert withtrust["trust_ratio"] is None and withtrust["trust_asset_source"] is None
    assert withtrust["comp_ratio"] is None                   # flag 全关不算基线


def test_assemble_p0d_missing_no_price_change(monkeypatch):
    # flag 开 + 缺采集(missing)→ 价/篇/竞争不变 · 只内部标 guard · 不进报价 needs_review
    _flags(monkeypatch, trust=True)
    off = A._assemble_result(_ctx(five118=_F5), _judged(), LLM_META, {}, 2.0, None)
    miss = A._assemble_result(_ctx(trust=_trust(source="missing", readiness=None), five118=_F5),
                              _judged(), LLM_META, {}, 2.0, None)
    assert _prices(off) == _prices(miss)                      # 缺采集不抬价
    assert miss["guards"].get("trust_asset_source") == "missing"
    assert miss["needs_review"] is False                      # 缺采集不进报价人审队列


def test_assemble_p0d_low_readiness_raises_articles(monkeypatch):
    # flag 开 + 缺背书(readiness 低)→ true_competition 上调(篇数侧) · trust_ratio>1
    _flags(monkeypatch, trust=True)
    off = A._assemble_result(_ctx(trust=_trust(source="collected", readiness=0.05), five118=_F5),
                             _judged(), LLM_META, {}, 2.0, None)
    # 对照:flag 关基线
    _flags(monkeypatch, trust=False)
    base = A._assemble_result(_ctx(five118=_F5), _judged(), LLM_META, {}, 2.0, None)
    assert off["true_competition"] > base["true_competition"]
    assert off["standard_articles"] >= base["standard_articles"]
    assert off["trust_ratio"] and off["trust_ratio"] > 1.0
    assert "trust_difficulty" in off["guards"]


def test_assemble_p0d_high_readiness_lowers(monkeypatch):
    # flag 开 + 背书足(readiness 高)→ true_competition 反向降 · trust_ratio<1
    _flags(monkeypatch, trust=True)
    hi = A._assemble_result(_ctx(trust=_trust(source="collected", readiness=0.95), five118=_F5),
                            _judged(), LLM_META, {}, 2.0, None)
    _flags(monkeypatch, trust=False)
    base = A._assemble_result(_ctx(five118=_F5), _judged(), LLM_META, {}, 2.0, None)
    assert hi["true_competition"] <= base["true_competition"]
    assert hi["trust_ratio"] and hi["trust_ratio"] < 1.0


def test_assemble_p0d_compound_needs_review(monkeypatch):
    # 三合一:缺背书(readiness 低) + 高价值(vm≥1.5) + 高竞争(comp≥30) → 报价 needs_review
    _flags(monkeypatch, trust=True)
    r = A._assemble_result(_ctx(trust=_trust(source="collected", readiness=0.05), five118=_F5, comp=35),
                           _judged(comp=35, value_signal=3.0), LLM_META, {}, 2.0, None)
    assert r["value_multiplier"] >= 1.5 and r["needs_review"] is True
    assert "trust_asset_compound" in r["guards"]


def test_assemble_p0d_low_value_no_compound(monkeypatch):
    # 缺背书 + 低价值(无 5118 → vm 钳 1.0)+ 高竞争 → 不触发三合一(不淹没人审队列)
    _flags(monkeypatch, trust=True)
    r = A._assemble_result(_ctx(trust=_trust(source="collected", readiness=0.05), comp=35),
                           _judged(comp=35, value_signal=1.0), LLM_META, {}, 2.0, None)
    assert "trust_asset_compound" not in r["guards"]


def test_assemble_p0d_labels_passthrough(monkeypatch):
    # 人话 label 透传到结果(供前端转人话 · 不脱敏)
    _flags(monkeypatch, trust=True)
    r = A._assemble_result(
        _ctx(trust=_trust(source="collected", readiness=0.7,
                          verified=["已有官网"], missing=["权威媒体报道"]), five118=_F5),
        _judged(), LLM_META, {}, 2.0, None)
    assert r["trust_verified_labels"] == ["已有官网"]
    assert r["trust_missing_labels"] == ["权威媒体报道"]


# ============================================================
# 6. 客户端脱敏 + scorer 透传(regex/源文件 · 不连库)
# ============================================================

def _load_blacklist():
    sel = open(os.path.join(ROOT, "api", "selection_api.py"), encoding="utf-8").read()
    mt = re.search(r"(_CUSTOMER_INTERNAL_KW_FIELDS\s*=\s*\([\s\S]*?\n\))", sel)
    assert mt, "未定位 _CUSTOMER_INTERNAL_KW_FIELDS"
    ns: dict = {}
    exec(textwrap.dedent(mt.group(1)), ns)
    return ns["_CUSTOMER_INTERNAL_KW_FIELDS"]


def test_trust_internal_fields_in_strip_blacklist():
    bl = _load_blacklist()
    for fld in ("trust_asset_source", "trust_asset_score", "citation_readiness_score",
                "trust_asset_needs_review", "trust_ratio", "trust_asset_evidence"):
        assert fld in bl, f"{fld} 未脱敏(§5.3 客户绝不见裸 trust 分数/难度因子/采集状态)"
    # 人话 label 有意保留给客户(转人话)· 不应被脱敏
    assert "trust_verified_labels" not in bl and "trust_missing_labels" not in bl
    # 回归:v2.3 ratio 脱敏不回退
    assert "factory_ratio" in bl and "guarantee_unavailable" in bl


def test_scorer_wires_trust_fields():
    src = open(os.path.join(ROOT, "tools", "keyword_value_scorer.py"), encoding="utf-8").read()
    assert 'trust_asset=trust_asset' in src, "score_keywords 未把 trust_asset 传给 assessor"
    for fld in ("trust_asset_source", "citation_readiness_score", "trust_ratio",
                "trust_verified_labels", "trust_missing_labels"):
        assert f'a.get("{fld}")' in src, f"scored 行未透传 {fld}"


# ============================================================
# 7. [Codex#1 返修] brand_id 取快照(不按 brand_name 猜)+ 共享缓存 per-brand 跳过
# ============================================================

import tools.batch_pricing as BP  # noqa: E402


def test_fetch_trust_uses_brand_id_not_name(monkeypatch):
    # flag 开 + 有 brand_id → 用 get_active_trust_snapshot(brand_id=...)· 记录调用确认按 id 不按 name
    monkeypatch.setattr(F, "is_trust_asset_enabled", lambda: True)
    seen = {}

    def _fake_get(brand_id=None, brand_name=None):
        seen["brand_id"] = brand_id
        seen["brand_name"] = brand_name
        return {"trust_asset_source": "collected", "citation_readiness_score": 0.5,
                "trust_asset_needs_review": False, "generated_at": datetime.now(timezone.utc),
                "verified_trust_assets": [], "missing_trust_assets": []}
    monkeypatch.setattr(TDB, "get_active_trust_snapshot", _fake_get)
    ta = BP._fetch_trust_asset_for_pricing(4567)
    assert seen["brand_id"] == 4567 and seen.get("brand_name") is None   # 按 id 取·绝不传 name
    assert ta["source"] == "collected"


def test_fetch_trust_no_brand_id_degrades_missing(monkeypatch):
    # [Codex#1] flag 开但拿不到 brand_id → 降级 missing(绝不按 brand_name 猜 → 防同名串客户)
    monkeypatch.setattr(F, "is_trust_asset_enabled", lambda: True)
    called = {"n": 0}
    monkeypatch.setattr(TDB, "get_active_trust_snapshot",
                        lambda **k: called.__setitem__("n", called["n"] + 1))
    ta = BP._fetch_trust_asset_for_pricing(None)
    assert ta["source"] == "missing" and called["n"] == 0   # 无 brand_id → 根本不查(更不按名查)


def test_fetch_trust_flag_off_none(monkeypatch):
    monkeypatch.setattr(F, "is_trust_asset_enabled", lambda: False)
    assert BP._fetch_trust_asset_for_pricing(123) is None


def test_shared_cache_skipped_when_trust_active():
    # P0-D 有 collected/stale 信任快照 → per-brand 价 → 不写 (industry,city,keyword) 共享缓存(防串客户)
    assert BP._should_write_shared_cache(None, None, True, 1.0, trust_price_active=True) is False
    # missing/None 不动价 → 缓存照常(向后兼容)
    assert BP._should_write_shared_cache(None, None, True, 1.0, trust_price_active=False) is True


def test_trust_price_active_helper():
    assert BP._trust_price_active({"source": "collected"}) is True
    assert BP._trust_price_active({"source": "stale"}) is True
    assert BP._trust_price_active({"source": "missing"}) is False
    assert BP._trust_price_active(None) is False


def test_declared_only_end_to_end_no_adjust(monkeypatch):
    # 端到端:仅声明 → collector missing → 喂 assessor(flag 开)→ 不调篇数 / 不触发报价 needs_review
    p = C.build_trust_payload(
        {"top_sources": [], "tier_counts": {}, "endorsement_score": 0,
         "data_quality": {"has_real_ai_citations": False}},
        official_website="https://only-declared.com")
    # collector payload(missing)→ DB row 形状 → normalize → assessor 消费形状
    row = {"trust_asset_source": p["trust_asset_source"],
           "trust_asset_score": p["trust_asset_score"],
           "citation_readiness_score": p["citation_readiness_score"],
           "trust_asset_needs_review": p["trust_asset_needs_review"],
           "verified_trust_assets": p["verified_trust_assets"],
           "missing_trust_assets": p["missing_trust_assets"],
           "generated_at": datetime.now(timezone.utc)}
    norm = TDB.normalize_trust_asset_for_pricing(row)
    assert norm["source"] == "missing"
    _flags(monkeypatch, trust=True)
    off = A._assemble_result(_ctx(five118=_F5, comp=35), _judged(comp=35, value_signal=3.0),
                             LLM_META, {}, 2.0, None)
    declared = A._assemble_result(_ctx(trust=norm, five118=_F5, comp=35),
                                  _judged(comp=35, value_signal=3.0), LLM_META, {}, 2.0, None)
    assert _prices(off) == _prices(declared)              # 仅声明不涨篇数
    assert "trust_asset_compound" not in declared["guards"]  # 不触发三合一报价 needs_review


# ============================================================
# 8. flag 默认关(无 DB → _get_setting 回落 default false)
# ============================================================

def test_flags_default_off():
    F.clear_cache()
    assert F.is_trust_asset_enabled() is False
    assert F.is_trust_collect_enabled() is False
