"""[P3 2026-06-08] 报价中心调用点契约(源码扫描型·防回归)。

配合 test_quote_markup_phase2_l0(证明 get_quote_markup_for_quote_viewer 返本人实际值含 1.0),
本文件守住【调用点】契约:报价生成必须传本人实际系数/成本,不传 override-None(致引擎回退后台默认),
cluster 兜底不得直接吃缓存价。重型端点 mock(auth/session/db)不实际,故用源码扫描守契约。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_server_diagnosis_quote_passes_actual_markup():
    # [P1-1] server.py 诊断报价:markup_override 用 get_quote_markup_for_quote_viewer(实际值含1.0)
    src = _read("server.py")
    assert "quote_markup_override = get_quote_markup_for_quote_viewer(user_id)" in src


def test_selection_quote_passes_actual_markup():
    # [P1-1] selection_api 选词报价:markup_override = 本人实际系数(= quote_markup_ratio)
    src = _read("api/selection_api.py")
    assert "quote_markup_ratio = get_quote_markup_for_quote_viewer(user_id)" in src
    assert "quote_markup_override = quote_markup_ratio" in src
    # 不应再用 override-None 函数喂报价生成
    assert "get_quote_markup_override_for_quote_viewer" not in src


def test_cluster_fallback_recomputes_not_cache_price():
    # [P2-2] cluster 兜底不得直接用缓存价(entry/standard/flagship_price)· 必须按本人系数重算
    src = _read("api/selection_api.py")
    assert 'c.get("entry_price")' not in src
    assert 'c.get("standard_price")' not in src
    assert 'c.get("flagship_price")' not in src
    # 仍应按 _fallback_customer_unit(= 本人成本 × 本人系数)重算
    assert "_fallback_customer_unit" in src


def test_legacy_keywords_quote_defended():
    # [P2-1] /api/keywords/quote 旧接口防御:有登录用户传本人系数/成本
    src = _read("server.py")
    assert "get_quote_markup_for_quote_viewer(_uid)" in src
    assert "markup_override=_qm_override" in src
    assert "cost_per_article_override=_cost_override" in src


# ===== [P1 成本地板·值级回归] 兜底单篇成本:本人自设 > 缓存真实成本 > 60 =====

def test_p1_fallback_unit_cost_override_wins():
    from services.quote_pricing_preferences import _resolve_fallback_unit_cost
    assert _resolve_fallback_unit_cost(350.0, None) == 350.0      # 本人自设 350
    assert _resolve_fallback_unit_cost(500.0, 350.0) == 500.0     # override 优先于缓存(Codex 点3·cluster)


def test_p1_fallback_unit_cost_cached_when_no_override():
    from services.quote_pricing_preferences import _resolve_fallback_unit_cost
    # 无 override 但缓存有真实成本 350(如央媒)→ 350(不是回落 60)
    assert _resolve_fallback_unit_cost(None, 350.0) == 350.0


def test_p1_fallback_unit_cost_default_60():
    from services.quote_pricing_preferences import _resolve_fallback_unit_cost
    assert _resolve_fallback_unit_cost(None, None) == 60.0
    assert _resolve_fallback_unit_cost(None, 0) == 60.0   # 缓存成本 0/无效 → 60
    assert _resolve_fallback_unit_cost(None, None, default=80.0) == 80.0


def test_p1_fallback_price_by_350_not_60(monkeypatch=None):
    # [Codex 点3] cost_override=350·markup=1.0 → standard 2 篇兜底价 = 2×350 = 700(不是旧硬编码 2×60=120)
    from services.quote_pricing_preferences import _resolve_fallback_unit_cost
    unit = _resolve_fallback_unit_cost(350.0, None)
    fallback_customer_unit = int(unit * 1.0)         # × 本人系数 1.0
    std_price = int(2 * fallback_customer_unit)      # standard 2 篇
    assert std_price == 700
    assert std_price != 120                          # 旧硬编码 60 的错值


def test_c_end_keeps_skip_markup_transparent():
    # [Codex 点3] C 端成本估算保持 skip_markup=True(透明成本·不套本人系数·不该改)
    src = (ROOT / "tools" / "c_end_cost_estimate.py").read_text(encoding="utf-8")
    assert "skip_markup=True" in src
