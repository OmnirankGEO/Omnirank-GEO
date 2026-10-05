"""报价 v2.3 P0-A/B/C 收口批 · 补缺测试(2026-06-15)。

覆盖本批补的真实缺口(flag 默认关 = 0 生产变化):
  - P0-A 运行时 source_channel fail-loud:flag 开但快照非 db active(静默回落 bootstrap)→ needs_review + guard
  - P0-A/B/C 缓存不污染:爆价/放飞词(blowup_no_cache/guarantee_unavailable)不进共享缓存 + 两路径接线
  - P0-C 不误伤:p0c_on 但非 national_unclamped 普通词 → 干净放行(P0-C 只咬放飞词)
  - P0-D ⟂ P0-A/B/C 确定性正交:trust_ratio/comp_ratio 在 P0D vs P0D+A+B+C 恒等(把影子 PP1/PP2 升级成 CI)
  - 客户脱敏黑名单收口补项:blowup_no_cache/guards/industry_baseline/v2_assessor_data 对称入黑名单

跑法:ALLOW_NONTEST_DB=1 TEST_DATABASE_URL=占位 PYTHONIOENCODING=utf-8 \
       python -m pytest tests/test_pricing_v23_final_2026_06_15.py -q
"""
import os
import re
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import tools.pricing_bands as B            # noqa: E402
import tools.pricing_llm_assessor as A     # noqa: E402
import tools.llm_pricing_flag as F         # noqa: E402
import tools.media_cost_ssot as M          # noqa: E402
import tools.batch_pricing as BP           # noqa: E402


# ============================================================
# 共用 harness(复刻 P0-D / v23_delta 测试范式)
# ============================================================
LLM_META = {"llm_deviation_pct": 0.0, "llm_used": "dual"}
_F5 = {"search_volume": 800, "sem_price": 12.0, "bidword_company_count": 40}
CFG = {"factory_ceiling_niche": 6500.0, "factory_ceiling_local": 3500.0,
       "entry_selling_ceiling_niche": 15000.0, "entry_selling_ceiling_local": 10000.0,
       "no_cache_selling": 10000.0,
       "lever_two": 1.5, "lever_three": 1.3, "national_unclamped_factory_ratio": 2.0}


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


def _t(factory, selling=0):
    return {"flagship": {"factory_price": factory, "selling_price": selling}}


class _FakeSnapshot:
    """模拟 MediaCostSnapshot:可控 source_channel + media_tier_cost。"""
    def __init__(self, source_channel, cost, needs_review=False):
        self.source_channel = source_channel
        self._cost = cost
        self._nr = needs_review

    def media_tier_cost(self, tier_letter, *a, **k):
        return {"cost": self._cost, "confidence": "exact", "needs_review": self._nr}


# ============================================================
# 1. P0-A 运行时 source_channel fail-loud(本批新护栏)
# ============================================================

def test_p0a_snapshot_source_not_db_marks_needs_review(monkeypatch):
    # P0-A 开 + 快照命中通道 != db(静默回落 bootstrap)→ needs_review + guard.cost_snapshot.snapshot_not_db
    _flags(monkeypatch, cost=True)
    monkeypatch.setattr(M, "load_media_cost_snapshot",
                        lambda *a, **k: _FakeSnapshot("bootstrap", 50.0))
    A._warned_snapshot_not_db = False
    r = A._assemble_result(_ctx(five118=_F5), _judged(), LLM_META, {}, 2.0, None)
    assert r["needs_review"] is True
    cs = r["guards"]["cost_snapshot"]
    assert cs["src"] == "snapshot" and cs["snapshot_not_db"] == "bootstrap"
    # [Codex 返修] bootstrap 占位成本绝不进共享缓存
    assert r["cost_snapshot_uncacheable"] is True


def test_p0a_snapshot_source_db_no_source_review(monkeypatch):
    # P0-A 开 + 快照命中通道 == db → 不因 source 触发 review(成本回真本身不靠 source 标 review)
    _flags(monkeypatch, cost=True)
    monkeypatch.setattr(M, "load_media_cost_snapshot",
                        lambda *a, **k: _FakeSnapshot("db", 50.0))
    r = A._assemble_result(_ctx(five118=_F5), _judged(), LLM_META, {}, 2.0, None)
    cs = r["guards"]["cost_snapshot"]
    assert cs["src"] == "snapshot" and "snapshot_not_db" not in cs
    # db 真快照成本可正常进共享缓存
    assert r["cost_snapshot_uncacheable"] is False


def test_p0a_snapshot_no_data_fallback_uncacheable(monkeypatch):
    # [Codex 返修] P0-A 开 + 快照该档无数据(cost=0)→ 回落现状公式 + needs_review + 不进共享缓存
    _flags(monkeypatch, cost=True)
    monkeypatch.setattr(M, "load_media_cost_snapshot",
                        lambda *a, **k: _FakeSnapshot("db", 0.0))   # cost=0 → 无该档数据
    r = A._assemble_result(_ctx(five118=_F5), _judged(), LLM_META, {}, 2.0, None)
    assert r["needs_review"] is True
    assert r["guards"]["cost_snapshot"]["src"] == "snapshot_no_data_fallback"
    assert r["cost_snapshot_uncacheable"] is True


def test_p0a_off_snapshot_source_irrelevant(monkeypatch):
    # P0-A 关 → 根本不读快照(走现状公式)→ 无 cost_snapshot guard·source 通道不影响(0 变化铁律)
    _flags(monkeypatch, cost=False)
    monkeypatch.setattr(M, "load_media_cost_snapshot",
                        lambda *a, **k: _FakeSnapshot("bootstrap", 50.0))
    r = A._assemble_result(_ctx(five118=_F5), _judged(), LLM_META, {}, 2.0, None)
    assert "cost_snapshot" not in r["guards"]
    assert r["cost_snapshot_uncacheable"] is False   # flag 关恒 False = 0 变化


# ============================================================
# 2. P0-A/B/C 缓存不污染(场景⑪)
# ============================================================

def test_cacheable_rows_drops_blowup_guarantee_and_snapshot_uncacheable():
    # 爆价/放飞/P0-A 非db成本词不进共享缓存,只干净词可缓存([Codex 返修] 加 cost_snapshot_uncacheable)
    rows = [
        {"keyword": "clean", "blowup_no_cache": False, "guarantee_unavailable": False},
        {"keyword": "blowup", "blowup_no_cache": True, "guarantee_unavailable": False},
        {"keyword": "guard", "blowup_no_cache": False, "guarantee_unavailable": True},
        {"keyword": "p0a_bootstrap", "cost_snapshot_uncacheable": True},
    ]
    out = BP._cacheable_rows(rows)
    assert {r["keyword"] for r in out} == {"clean"}


def test_cacheable_rows_wired_in_flat_and_cluster():
    # _cacheable_rows 必须在 flat + cluster 两条缓存写路径都过滤(def + 2 调用点 = ≥3 次出现)
    src = open(os.path.join(ROOT, "tools", "batch_pricing.py"), encoding="utf-8").read()
    assert src.count("_cacheable_rows(") >= 3, "flat/cluster 至少一条缓存写路径漏过滤爆价词"


def test_should_write_shared_cache_blocks_override_and_multiplier():
    # 自设成本/上级倍率/trust per-brand 价 → 不写共享缓存(防污染跨代理/跨客户全局锁)
    assert BP._should_write_shared_cache(None, 120.0, True, 1.0, trust_price_active=False) is False  # cost_override
    assert BP._should_write_shared_cache(None, None, True, 2.0, trust_price_active=False) is False    # cost_multiplier!=1
    assert BP._should_write_shared_cache(None, None, True, 1.0, trust_price_active=True) is False      # P0-D per-brand
    assert BP._should_write_shared_cache(None, None, True, 1.0, trust_price_active=False) is True       # 干净 → 可写


# ============================================================
# 3. P0-C 不误伤:p0c_on 但非 national 普通词 → 干净放行(场景⑨ 互补正向)
# ============================================================

def test_p0c_on_non_national_word_clean_passthrough():
    # P0-C 开 + 非 national_unclamped 常规词 + factory<ceiling → 不剥保证价/不 review(P0-C 只咬放飞词)
    r = B.compute_blowup_guards(tiers=_t(2000, 4000), keyword_type="local_city", value_mult=1.25,
                                national_unclamped=False, p0a_on=False, p0b_relaxed=False, p0c_on=True,
                                cfg=CFG)
    assert r["guarantee_unavailable"] is False and r["needs_review"] is False
    assert "national_unclamped_review" not in r["guards"]
    assert "national_unclamped_no_guarantee" not in r["guards"]
    assert "national_unclamped_factory_ratio" not in r["guards"]


def test_p0c_national_word_intercepted():
    # 对照:P0-C 开 + national_unclamped 放飞词 → needs_review,但入门价未超阈值时仍保留报价
    r = B.compute_blowup_guards(tiers=_t(2000, 4000), keyword_type="national_niche", value_mult=1.0,
                                national_unclamped=True, p0a_on=False, p0b_relaxed=False, p0c_on=True,
                                cfg=CFG)
    assert r["guarantee_unavailable"] is False and r["needs_review"] is True
    assert "national_unclamped_review" in r["guards"]
    assert "national_unclamped_no_guarantee" not in r["guards"]


# ============================================================
# 4. P0-D ⟂ P0-A/B/C 确定性正交(把影子 PP1/PP2 升级成 CI · 场景④⑤)
# ============================================================

def test_p0d_trust_ratio_orthogonal_under_abc(monkeypatch):
    # 用 db 快照避免 source_channel review 干扰;trust readiness=0.1 ≤ low → 难度因子 max_uplift。
    monkeypatch.setattr(M, "load_media_cost_snapshot",
                        lambda *a, **k: _FakeSnapshot("db", 50.0))

    def _run(cost, value, nat):
        _flags(monkeypatch, cost=cost, value=value, nat=nat, trust=True)
        return A._assemble_result(_ctx(trust=_trust(source="collected", readiness=0.1), five118=_F5),
                                  _judged(), LLM_META, {}, 2.0, None)

    r_p0d = _run(False, False, False)       # P0D(当前生产基线)
    r_full = _run(True, True, True)          # P0D+A+B+C
    # 难度因子与篇数侧调整完全不被 A/B/C 影响(正交铁证)
    assert r_p0d["trust_ratio"] == r_full["trust_ratio"]
    assert r_p0d["trust_ratio"] is not None and r_p0d["trust_ratio"] > 1.0
    assert r_p0d["comp_ratio"] == r_full["comp_ratio"]
    assert r_p0d["true_competition"] == r_full["true_competition"]


# ============================================================
# 5. 客户脱敏黑名单收口补项(纵深对齐)
# ============================================================

def _load_blacklist():
    sel = open(os.path.join(ROOT, "api", "selection_api.py"), encoding="utf-8").read()
    mt = re.search(r"(_CUSTOMER_INTERNAL_KW_FIELDS\s*=\s*\([\s\S]*?\n\))", sel)
    assert mt, "未定位 _CUSTOMER_INTERNAL_KW_FIELDS"
    ns: dict = {}
    exec(textwrap.dedent(mt.group(1)), ns)
    return ns["_CUSTOMER_INTERNAL_KW_FIELDS"]


def test_p0abc_strip_blacklist_additions():
    bl = _load_blacklist()
    for fld in ("blowup_no_cache", "guards", "industry_baseline", "v2_assessor_data",
                "cost_snapshot_uncacheable"):
        assert fld in bl, f"{fld} 未脱敏(与 guarantee_unavailable 同类内部护栏/审计字段·纵深应对齐)"
    # 对称/回归:guarantee_unavailable + ratio + trust 内部字段仍在
    assert "guarantee_unavailable" in bl and "factory_ratio" in bl and "trust_ratio" in bl
    # 有意保留给客户(市场事实/不报价标 · 转人话可见)
    assert "super_red_ocean" not in bl and "should_quote" not in bl
    assert "trust_verified_labels" not in bl and "trust_missing_labels" not in bl
