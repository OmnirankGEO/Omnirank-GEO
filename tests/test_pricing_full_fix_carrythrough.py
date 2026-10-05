"""完整修复返修(老板 2026-06-13)· 集成测试:证明「剥保证价」+「不写共享缓存」真的发生。

覆盖返修 High1(guarantee_unavailable 透传 + 下游剥保证价)+ High2(blowup_no_cache 不写共享缓存)。
针对老板审核结论:"字段产出了但没穿透到下游执行" —— 这里用真实汇总/缓存助手验证下游确实按标执行。

跑法:ALLOW_NONTEST_DB=1 TEST_DATABASE_URL=占位 PYTHONIOENCODING=utf-8 python -m pytest tests/test_pricing_full_fix_carrythrough.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.batch_pricing as BP  # noqa: E402


def _row(keyword, selling, articles=7, cost=55.0, comp=10, **flags):
    """构造一行 scored(含三档出厂价 = selling · 篇数/成本齐全)。flags 注入护栏布尔。"""
    r = {
        "keyword": keyword, "selling_price": selling, "required_articles": articles,
        "total_cost": round(articles * cost, 1), "cost_per_article": cost,
        "effective_competition": comp, "competitor_count": comp, "content_count": comp,
        "entry_price": selling, "standard_price": selling, "flagship_price": selling, "strong_price": selling,
        "entry_articles": articles, "standard_articles": articles,
        "flagship_articles": articles, "strong_articles": articles,
        "super_red_ocean": False, "is_broad": False, "value_score": 1.0,
    }
    r.update(flags)
    return r


# ============================================================
# High2:爆价/放飞词不写全局共享缓存(_cacheable_rows)
# ============================================================
def test_cacheable_rows_drops_blowup_no_cache():
    rows = [_row("正常词", 500), _row("爆价词", 30000, blowup_no_cache=True)]
    out = BP._cacheable_rows(rows)
    assert [r["keyword"] for r in out] == ["正常词"]   # 爆价词被剥离不写缓存


def test_cacheable_rows_drops_guarantee_unavailable():
    rows = [_row("正常词", 500), _row("放飞词", 9000, guarantee_unavailable=True)]
    out = BP._cacheable_rows(rows)
    assert [r["keyword"] for r in out] == ["正常词"]   # 参考价人工核词也不污染共享锁


def test_cacheable_rows_flags_off_keeps_all():
    # 三 flag 全关时 assessor 不产出这两布尔 → 全集写缓存 = 0 行为变化铁证
    rows = [_row("a", 500), _row("b", 800)]
    out = BP._cacheable_rows(rows)
    assert len(out) == 2


# ============================================================
# High1:剥保证价 — flat 汇总层(generate_batch_summary)
# ============================================================
def test_summary_excludes_guarantee_unavailable_from_guaranteed_total():
    normal = _row("正常词", 500, articles=7)
    blown = _row("放飞词", 30000, articles=80, guarantee_unavailable=True)
    s_all = BP.generate_batch_summary([normal, blown], 0.25)
    s_only = BP.generate_batch_summary([normal], 0.25)
    # 放飞词不进套餐保证总价/篇数/成本(剥保证价)
    assert s_all["final_price"] == s_only["final_price"] == 500
    assert s_all["total_articles"] == s_only["total_articles"] == 7
    assert s_all["total_cost"] == s_only["total_cost"]
    # 但仍计入 keyword_count(报价单可见 · 参考价人工核)
    assert s_all["keyword_count"] == 2


def test_summary_superredocean_regression_still_excluded():
    # 回归:super_red_ocean 仍被排除(没被改坏)
    normal = _row("正常词", 500)
    sro = _row("超红海", 9999, super_red_ocean=True)
    assert BP.generate_batch_summary([normal, sro], 0.25)["final_price"] == 500


def test_summary_flags_off_all_counted():
    # 两布尔缺省 False → 全词进保证总价 = 0 变化
    a = _row("a", 500)
    b = _row("b", 800)
    assert BP.generate_batch_summary([a, b], 0.25)["final_price"] == 1300


# ============================================================
# High1:剥保证价 — cluster 汇总层(_compute_cluster_pricing)
# ============================================================
def _cluster(core_kws):
    return {"core_keywords": [{"keyword": k, "is_selected": True} for k in core_kws],
            "covered_keywords": []}


def test_cluster_pricing_skips_guarantee_unavailable():
    scored_map = {
        "正常词": _row("正常词", 500, articles=7),
        "放飞词": _row("放飞词", 30000, articles=80, guarantee_unavailable=True),
    }
    pricing = BP._compute_cluster_pricing(_cluster(["正常词", "放飞词"]), scored_map)
    # 放飞词被跳过 → core_price/full_price 只含正常词(¥500)
    assert pricing["standard"]["core_price"] == 500
    assert pricing["standard"]["full_price"] == 500


def test_cluster_pricing_marks_skipped_word():
    scored_map = {"放飞词": _row("放飞词", 30000, guarantee_unavailable=True)}
    cluster = _cluster(["放飞词"])
    BP._compute_cluster_pricing(cluster, scored_map)
    # 标透传回聚类核心词(供前端「参考价·人工核」渲染)+ needs_review
    kw = cluster["core_keywords"][0]
    assert kw["guarantee_unavailable"] is True and kw["needs_review"] is True


def test_cluster_pricing_flags_off_includes_word():
    scored_map = {"正常词": _row("正常词", 500), "另一词": _row("另一词", 800)}
    pricing = BP._compute_cluster_pricing(_cluster(["正常词", "另一词"]), scored_map)
    assert pricing["standard"]["core_price"] == 1300   # 0 变化


# ============================================================
# High1:markdown 报价单 — 爆价/放飞词显「参考价·人工核」(不裸出保证价)
# ============================================================
def test_markdown_marks_guarantee_unavailable():
    normal = _row("正常词", 500)
    blown = _row("放飞词", 30000, articles=80, guarantee_unavailable=True)
    scored = [normal, blown]
    summary = BP.generate_batch_summary(scored, 0.25)
    tier_summaries = {name: BP.generate_batch_summary(scored, share)
                      for name, share in (("入门版", 0.10), ("标准版", 0.20), ("旗舰版", 0.30))}
    md = BP.generate_batch_markdown("测试品牌", [s["keyword"] for s in scored], scored, summary, tier_summaries)
    assert "参考价·人工核" in md          # 放飞词行不裸出保证价
    assert "放飞词" in md                  # 词仍出现在报价单(只给参考价)


# ============================================================
# 二审隔离(2026-06-13):旧 keyword_price_cache_llm.should_quote=false 不污染 v2.2 公式路径
#   _filter_llm_should_quote_false(keywords, brand, scope, llm_active):
#     llm_active=False(LLM_FIRST 全局关)→ 不读 LLM cache · 原样返回(隔离)
#     llm_active=True(LLM-first 失败 fallback)→ 读 cache · 剔 should_quote=false 信息型词
# ============================================================
def _stub_llm_cache(monkeypatch, fn):
    """用 sys.modules 注入 db.diagnosis_db 桩 · 避免 import 真模块触发 DB 连接(模块加载即连库)。
    helper 内 `from db.diagnosis_db import get_llm_cached_keyword_prices` 会命中此桩。"""
    import sys
    import types
    stub = types.ModuleType("db.diagnosis_db")
    stub.get_llm_cached_keyword_prices = fn
    monkeypatch.setitem(sys.modules, "db.diagnosis_db", stub)


def test_module5_isolated_when_llm_off_does_not_read_cache(monkeypatch):
    """LLM-first 关(llm_active=False)→ 绝不读 keyword_price_cache_llm(隔离铁律)。"""
    called = {"hit": False}

    def _spy(*a, **k):
        called["hit"] = True
        return {"信息词": {"should_quote": False}}   # 即便 cache 有 should_quote=false 也不该被读
    _stub_llm_cache(monkeypatch, _spy)

    out = BP._filter_llm_should_quote_false(["信息词", "正常词"], "测试品牌", None, llm_active=False)
    assert called["hit"] is False          # 没读 LLM cache(v2.2 与旧 LLM cache 完全隔离)
    assert out == ["信息词", "正常词"]      # 一个不剔


def test_module5_filters_when_llm_active(monkeypatch):
    """LLM-first 失败 fallback(llm_active=True)→ 读 cache · 剔 should_quote=false 信息型词。"""
    _stub_llm_cache(monkeypatch, lambda *a, **k: {"信息词": {"should_quote": False}})
    out = BP._filter_llm_should_quote_false(["信息词", "正常词"], "测试品牌", None, llm_active=True)
    assert out == ["正常词"]                # 信息词被剔


def test_module5_active_all_false_keeps_all(monkeypatch):
    """全部 should_quote=false → 不剔(避免空报价崩)。"""
    _stub_llm_cache(monkeypatch, lambda *a, **k: {"a": {"should_quote": False}, "b": {"should_quote": False}})
    out = BP._filter_llm_should_quote_false(["a", "b"], "测试品牌", None, llm_active=True)
    assert out == ["a", "b"]


def test_module5_active_cache_error_degrades(monkeypatch):
    """cache 查询异常 → 降级不剔(best-effort)。"""
    def _boom(*a, **k):
        raise RuntimeError("db down")
    _stub_llm_cache(monkeypatch, _boom)
    out = BP._filter_llm_should_quote_false(["a", "b"], "测试品牌", None, llm_active=True)
    assert out == ["a", "b"]
