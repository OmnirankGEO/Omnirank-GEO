"""复检返修判别锁 · 成本口径三处错(复检 AI 2026-07-27 抓出,执行方漏)。

三处都是"账本静默算错"—— 和这批工单的主题同源:**测/记错了却没人发现**。

  ① `("deepseek_official","deepseek-v4-flash")` 不在价目表 → 静默落 DEFAULT_PRICING,
     output 单价 0.005 vs 真价 0.002;且 DEFAULT 无 cache_hit 档,
     本包刚补的 `cache_read_input_tokens` 解析被完全抵消;
  ② DeepSeek 的检索次数被写进 **Kimi 专用计费键** → 无条件 × ¥0.036/次,
     单次落库 ¥0.156 vs 真价 ¥0.080(**1.95×**),其中 46% 凭空加的;
  ③ 上一轮"修第 4 处血缘"把 monitoring_db 的映射改成 `hy3-preview`,
     而价目表没有该型号 → **由命中改成未命中**(执行方自己打出来的回归)。
"""
from __future__ import annotations

import pytest

from tools.llm_call_tracker import PRICING_TABLE, estimate_cost


# ---------------------------------------------------------------------------
# ① deepseek_official 必须在价目表里,且与直连同价
# ---------------------------------------------------------------------------
def test_deepseek_official_is_priced_not_defaulted():
    key = ("deepseek_official", "deepseek-v4-flash")
    assert key in PRICING_TABLE, "官方通道未登记 → 静默落 DEFAULT_PRICING,成本算错还没告警"
    assert PRICING_TABLE[key] == PRICING_TABLE[("deepseek", "deepseek-v4-flash")], \
        "同一模型换调用通道单价不该变"


def test_deepseek_official_has_cache_tier():
    """没有 cache_hit 档 = 本包补的 cache_read_input_tokens 解析白做。"""
    assert PRICING_TABLE[("deepseek_official", "deepseek-v4-flash")].get("cache_hit") is not None


def test_official_cache_discount_actually_applies():
    """带缓存命中时必须比不带便宜 —— 否则解析出来了却没用上。"""
    with_cache = estimate_cost("deepseek_official", "deepseek-v4-flash",
                               input_tokens=67461, output_tokens=1306, cached_tokens=512)
    no_cache = estimate_cost("deepseek_official", "deepseek-v4-flash",
                             input_tokens=67461, output_tokens=1306, cached_tokens=0)
    assert with_cache < no_cache


def test_official_is_priced_from_the_table_not_from_the_fallback():
    """🔒 登记过的官方行必须走**表**,不走 DEFAULT_PRICING 兜底。

    🔴 [WO_206 c1p 2026-09-14] 这条原来断言的是 `priced < defaulted`,
       docstring 写「未登记时会落 DEFAULT(output 0.005)—— 真价 0.002,虚高 2.5×」。
       那个判断在 2026-05 成立,**今天反过来了**:官方线高峰输出价已是 ¥8/M
       (= 0.008/K),比 DEFAULT 的 0.005/K **贵**。
       所以「官方比兜底便宜」不再是不变式,而它本来要守的那件事没变 ——
       **登记过的行不许悄悄落兜底**。改成直接钉这件事。
    🔴 顺带把那个反转本身钉下来(见下一条):今天 DEFAULT 对官方线是**低估**,
       而低估是危险的那一侧。
    """
    from tools.llm_call_tracker import PRICING_TABLE

    priced = estimate_cost("deepseek_official", "deepseek-v4-flash",
                           input_tokens=67461, output_tokens=1306)
    row = PRICING_TABLE[("deepseek_official", "deepseek-v4-flash")]
    want = 67461 / 1000 * row["input"] + 1306 / 1000 * row["output"]
    assert abs(priced - want) < 1e-9, "官方行没按表算:%s vs %s" % (priced, want)

    # 🔴 反向对照不能再用「和兜底不相等」:Review 09-14 把兜底抬成了**官方 flash 高峰价**,
    #    于是官方 flash 那几行与兜底**数值恰好相同** —— `!=` 在这里恒假,毫无区分力。
    #    (这正是我自己踩的:先写了「官方 != 兜底」,后落了「兜底 = 官方高峰」的裁定,
    #     两条自相矛盾,注毒之前谁都没看出来。)
    #    改成拿一条**价钱确实不同**的行做对照:百炼侧 v4-pro 是另一套计价。
    other = estimate_cost("dashscope", "deepseek-v4-pro",
                          input_tokens=67461, output_tokens=1306)
    assert abs(priced - other) > 1e-9, (
        "官方行与百炼 v4-pro 行算出来一样 —— 那说明取价这一步根本没按 key 走")


def test_the_fallback_is_no_longer_cheaper_than_the_official_line():
    """🔴 兜底价**不再低于**官方线高峰价(Review 09-14 裁定抬上去了)。

    历史:2026-05 时兜底是 `0.001 / 0.005`,比当时的官方线**贵**,所以老注释写
    「未登记会虚高 2.5×」。到 2026-09-14 官方线涨到 `0.002 / 0.008`,兜底反而
    **便宜** —— 未登记的 (provider, model) 会被**少算**,而少算不报错。
    Review 裁定把兜底抬成官方 flash 高峰价(不取全表 max:那会把 v4-pro 的
    ¥27/M 套到所有未登记模型上,统计失真)。

    这条只钉方向;逐格的比对在 `tests/deepseek_pricing_bands_2026_09_14/`。
    """
    from tools.llm_call_tracker import DEFAULT_PRICING, PRICING_TABLE

    official = PRICING_TABLE[("deepseek", "deepseek-flash")]
    for field in ("input", "output"):
        assert DEFAULT_PRICING[field] >= official[field], (
            "兜底的 %s 又低于官方线高峰了 —— 未登记模型会被少算" % field)


# ---------------------------------------------------------------------------
# ② DeepSeek 检索次数不得套用 Kimi 单价
# ---------------------------------------------------------------------------
def test_deepseek_search_calls_are_not_billed_at_kimi_rate():
    """🔒 复检抓到的 1.95× 虚高。

    官方 DeepSeek 的检索费**已含在 input_tokens 里**(实测:带检索 6.5-7.8 万,
    不带检索仅 14),再按次加一遍就是重复计费。
    """
    base = estimate_cost("deepseek_official", "deepseek-v4-flash",
                         input_tokens=78024, output_tokens=1300)
    with_search = estimate_cost("deepseek_official", "deepseek-v4-flash",
                                input_tokens=78024, output_tokens=1300,
                                metadata={"deepseek_web_search_call_count": 2})
    assert with_search == base, "DeepSeek 的检索次数不该额外计费(成本已在 token 里)"


def test_kimi_search_calls_still_billed():
    """Kimi 的 $web_search 由 Moonshot 按次单收,这条计费必须保留。"""
    base = estimate_cost("kimi", "kimi-k2.6", input_tokens=1000, output_tokens=500)
    billed = estimate_cost("kimi", "kimi-k2.6", input_tokens=1000, output_tokens=500,
                           metadata={"web_search_call_count": 2})
    assert billed > base


def test_deepseek_official_does_not_write_kimi_billing_key():
    """🔒 源码级锁:官方 DeepSeek 必须写独立 metadata 键,不许复用 Kimi 的计费键。"""
    import inspect

    from tools.ai_visibility import ai_tester

    src = inspect.getsource(ai_tester._tracked_post)
    assert 'extra_metadata["deepseek_web_search_call_count"]' in src
    assert 'if platform == "deepseek_official":' in src


# ---------------------------------------------------------------------------
# ③ hy3-preview 回归:血缘改了型号,价目表必须跟上
# ---------------------------------------------------------------------------
def test_hy3_preview_is_priced():
    """🔒 上一轮改血缘打出来的回归 —— 映射换了型号,价目表没跟,由命中变未命中。"""
    key = ("tencent_tokenhub", "hy3-preview")
    assert key in PRICING_TABLE, "元宝换 hy3-preview 后价目表未命中 → 落 DEFAULT,带缓存时虚高 38.6%"
    assert PRICING_TABLE[key] == PRICING_TABLE[("tencent_tokenhub", "hy3")]


def test_hy3_preview_cache_discount_applies():
    with_cache = estimate_cost("tencent_tokenhub", "hy3-preview",
                               input_tokens=17149, output_tokens=682, cached_tokens=6784)
    no_cache = estimate_cost("tencent_tokenhub", "hy3-preview",
                             input_tokens=17149, output_tokens=682, cached_tokens=0)
    assert with_cache < no_cache


def test_lineage_model_and_pricing_table_stay_in_sync():
    """🔒 血缘里出现的每个 (provider, model) 都必须在价目表里有价。

    这条锁的是**同一类错的下一次**:以后谁再改血缘型号却忘了改价目表,这里立刻红。
    """
    from services.ai_surface_monitoring.lineage import SURFACE_SPECS

    missing = []
    for spec in SURFACE_SPECS.values():
        if spec.availability != "active":
            continue
        key = (spec.provider_key, spec.default_model_key)
        # 火山/百炼等走各自原生 SDK 的表面 provider_key 与计费 platform 命名不同源,
        # 只校验本批涉及的两条官方/腾讯通道。
        if key[0] in {"deepseek_official", "tencent_tokenhub"} and key not in PRICING_TABLE:
            missing.append(key)
    assert not missing, f"血缘里的型号在价目表里没有价:{missing}"


# ---------------------------------------------------------------------------
# 未命中不再静默
# ---------------------------------------------------------------------------
def test_pricing_miss_is_warned_not_silent(caplog):
    """🔒 价目表未命中原本**静默**落默认值 —— 本包的两处错就是这么溜过去的。"""
    import logging

    from tools import llm_call_tracker

    llm_call_tracker._PRICING_MISS_WARNED.discard(("brand_new_vendor", "brand_new_model"))
    with caplog.at_level(logging.WARNING, logger="LLMTracker"):
        estimate_cost("brand_new_vendor", "brand_new_model", input_tokens=100, output_tokens=100)
    assert any("价目表未命中" in r.message for r in caplog.records), "未命中还是静默的"


def test_pricing_miss_warning_is_deduped(caplog):
    """同一 key 只警告一次,不刷屏。"""
    import logging

    from tools import llm_call_tracker

    llm_call_tracker._PRICING_MISS_WARNED.discard(("dedupe_vendor", "dedupe_model"))
    with caplog.at_level(logging.WARNING, logger="LLMTracker"):
        for _ in range(3):
            estimate_cost("dedupe_vendor", "dedupe_model", input_tokens=10, output_tokens=10)
    hits = [r for r in caplog.records if "价目表未命中" in r.message]
    assert len(hits) == 1
