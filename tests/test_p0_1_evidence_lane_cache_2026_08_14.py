# -*- coding: utf-8 -*-
"""P0-1 · 题证对齐 + 同 quote 检索复用 · 判别锁(2026-08-14)。

仓内纪律:每条「必须命中」配「必须不命中」;元判据先证夹具真含目标形态。
变异点(mutation runner 逐条拆):
  M1 拆 title 注入(_article_terms 恒返 "")→ test_queries_differ_by_title 红;
  M2 拆缓存键 scope 隔离(_lane_cache_get 忽略 scope)→ test_different_scope_no_reuse 红;
  M3 拆缓存本身(_lane_cache_get 恒 None)→ test_same_scope_reuses_provider_once 红。
"""
from __future__ import annotations

import asyncio

import pytest

import writing.evidence_research as er


# ------------------------------------------------------------------ 元判据
def test_article_terms_extracts_title_level_words() -> None:
    terms = er._article_terms(
        "深圳观光电梯定制哪家交付周期快?", keyword="观光电梯定制", brand="晨光富士电梯",
    )
    assert "交付周期" in terms, "标题级意图词没被提出来,后面的注入断言是空的"
    assert "观光电梯定制" not in terms, "quote 级关键词没被剔除(注入的不是文章级变量)"


def test_article_terms_empty_when_title_is_quote_level() -> None:
    # 反向:标题完全由 quote 级变量组成 → 意图词为空 → lane 退回原形态
    assert er._article_terms("观光电梯定制", keyword="观光电梯定制", brand="") == ""


# ------------------------------------------------------------------ 夹具
@pytest.fixture(autouse=True)
def _clean_lane_cache():
    er._lane_cache.clear()
    er._inflight.clear()
    yield
    er._lane_cache.clear()
    er._inflight.clear()


class _ProviderSpy:
    """记录每条 query 被真发了几次的假 provider。"""

    def __init__(self):
        self.calls: list[str] = []

    async def citation_search(self, query, size=5, scenario=""):
        self.calls.append(query)
        return {"citations": [{
            "url": f"https://example.com/{len(self.calls)}",
            "title": f"结果 {query[:10]}", "snippet": "s", "source": "新华网",
        }]}


@pytest.fixture()
def provider_spy(monkeypatch):
    spy = _ProviderSpy()

    from tools.search import provider_router

    monkeypatch.setattr(provider_router, "citation_search", spy.citation_search)
    # 断外部依赖:语料库正文 / reader / 核验层全部短路(纯 lane 行为测试)
    monkeypatch.setattr(er, "_load_jc3_body", lambda url: ("", {}))

    async def _no_reader(url, format="markdown"):
        raise RuntimeError("reader disabled in test")

    from tools.search import metaso_mcp

    monkeypatch.setattr(metaso_mcp, "metaso_web_reader", _no_reader)
    return spy


async def _collect(title: str, scope: str, **kw):
    return await er.collect_evidence_pack(
        title=title, keyword="观光电梯定制", industry="电梯",
        client_brand="甲品牌", competitor_names=[],
        request_id="t", force=True, cache_scope=scope, **kw,
    )


# ------------------------------------------------------------------ ① 题证对齐
async def test_queries_differ_by_title(provider_spy) -> None:
    pack_a = await _collect("观光电梯定制哪家交付周期快", "")
    pack_b = await _collect("观光电梯定制安全规范怎么核验", "")
    qa = [q["query"] for q in pack_a["queries"]]
    qb = [q["query"] for q in pack_b["queries"]]
    assert qa[0] != qb[0], "两篇不同标题的首条 lane query 相同 —— 文章级变量没进 query(根因 A 复发)"
    assert "交付周期" in qa[0] and "安全规范" in qb[0], "query 里找不到标题意图词"
    # 反向对照:其余 quote 级 lane 两篇必须逐字相同(它们是缓存复用的正当对象)
    assert qa[1:] == qb[1:], "quote 级 lane 不应随标题漂移"


async def test_entity_lane_query_carries_title_terms(provider_spy, monkeypatch) -> None:
    monkeypatch.setattr(er, "corpus_lead_terms", lambda industry, limit=6: [])
    lanes_a = er.build_entity_lanes(["乙公司"], keyword="观光电梯", industry="电梯",
                                    article_terms="交付周期快")
    lanes_b = er.build_entity_lanes(["乙公司"], keyword="观光电梯", industry="电梯",
                                    article_terms="安全规范核验")
    assert lanes_a[1][1] != lanes_b[1][1], "同 quote 两 topic 的 entity-lane 第 2 条必须不同"
    # 反向:第 1 条(官方事实面)刻意保持 quote 级,两 topic 必须相同
    assert lanes_a[0][1] == lanes_b[0][1], "官方事实面 lane 不应带文章级变量"


# ------------------------------------------------------------------ ②b R2-3 single-flight(并发)
class _SlowFactory:
    def __init__(self, fail_first: bool = False):
        self.calls = 0
        self.fail_first = fail_first

    async def __call__(self):
        self.calls += 1
        call_no = self.calls
        await asyncio.sleep(0.05)
        if self.fail_first and call_no == 1:
            raise RuntimeError("provider down")
        return [{"url": f"https://r/{call_no}"}]


async def test_single_flight_concurrent_one_call() -> None:
    """🔴 R2-3 并发正例(返修单反例的反命题):同 key 并发 N 次 → provider 恰 1 次。
    旧 get/await/put 形态下这里是 8 次(重复付费)。"""
    import asyncio as aio

    factory = _SlowFactory()
    results = await aio.gather(*[
        er._single_flight_fetch("q-sf", "citation", "同一条query", factory)
        for _ in range(8)
    ])
    assert factory.calls == 1, f"并发 8 次真调了 provider {factory.calls} 次(重复付费)"
    assert all(r[0] == results[0][0] for r in results), "等待者拿到的结果与 leader 不一致"
    assert sum(1 for r in results if not r[1]) == 1, "cache_hit 记账错(应恰 1 个 leader)"
    assert not er._inflight, "进行中态没清理"


async def test_single_flight_cross_scope_separate_calls() -> None:
    """反例:跨 scope 并发必须各自调用(隔离不许被合并省钱省穿)。"""
    import asyncio as aio

    factory = _SlowFactory()
    await aio.gather(
        er._single_flight_fetch("q-a", "citation", "同一条query", factory),
        er._single_flight_fetch("q-b", "citation", "同一条query", factory),
    )
    assert factory.calls == 2, "跨 scope 被合并成一次调用(隔离破)"


async def test_single_flight_error_path_not_reused() -> None:
    """R2-3 异常路径:leader 失败 → 当轮等待者如实同失败;**后续新请求不复用坏
    Future**,重新发起并成功;进行中态清理干净。"""
    import asyncio as aio

    factory = _SlowFactory(fail_first=True)
    results = await aio.gather(
        er._single_flight_fetch("q-err", "citation", "q", factory),
        er._single_flight_fetch("q-err", "citation", "q", factory),
        return_exceptions=True,
    )
    assert all(isinstance(r, RuntimeError) for r in results), f"失败没有如实传递: {results}"
    assert not er._inflight, "失败后进行中态残留(后续会复用坏 Future)"
    ok, hit = await er._single_flight_fetch("q-err", "citation", "q", factory)
    assert ok and not hit and factory.calls == 2, "失败后的新请求没有重新发起"


async def test_single_flight_cancel_cleans_state() -> None:
    """R2-3 取消路径:leader 被取消 → 进行中态清理;下一个请求全新发起。"""
    import asyncio as aio

    factory = _SlowFactory()
    task = aio.ensure_future(er._single_flight_fetch("q-cancel", "citation", "q", factory))
    await aio.sleep(0.01)
    task.cancel()
    try:
        await task
    except aio.CancelledError:
        pass
    assert not er._inflight, "取消后进行中态残留"
    ok, hit = await er._single_flight_fetch("q-cancel", "citation", "q", factory)
    assert ok and factory.calls == 2


async def test_concurrent_collect_real_entry(provider_spy) -> None:
    """R2-3 真入口并发:两篇同 quote 同标题**并发** collect_evidence_pack ——
    provider 总调用数 = 唯一 query 数(5),不是 10(返修单反例的入口级复现)。"""
    import asyncio as aio

    await aio.gather(_collect("同一标题", "q-conc"), _collect("同一标题", "q-conc"))
    # 紧凑档默认 query_cap=4(GEO_ARTICLE_EVIDENCE_QUERY_CAP)→ 4 条唯一 lane;
    # 两篇并发仍只许各调一次 = 总 4 次(旧形态是 8 次重复付费)。
    assert len(provider_spy.calls) == len(set(provider_spy.calls)) == 4, (
        f"并发两篇真调 provider {len(provider_spy.calls)} 次(期望 4 条唯一 query 各一次)"
    )


# ------------------------------------------------------------------ ② 同 quote 复用
async def test_same_scope_reuses_provider_once(provider_spy) -> None:
    await _collect("同一标题", "quote-77")
    calls_after_first = len(provider_spy.calls)
    pack2 = await _collect("同一标题", "quote-77")
    assert len(provider_spy.calls) == calls_after_first, (
        "同 scope 逐字相同的 query 第二篇又真发了 provider —— 只计费一次不成立"
    )
    hits = [q for q in pack2["queries"] if q.get("cache_hit")]
    assert hits, "第二篇的 queries 里没有 cache_hit=True 落账(复用不可复算)"


async def test_different_scope_no_reuse(provider_spy) -> None:
    # 反向对照(工单判别测试原文):不同 quote 不得互用缓存
    await _collect("同一标题", "quote-77")
    calls_after_first = len(provider_spy.calls)
    pack2 = await _collect("同一标题", "quote-88")
    assert len(provider_spy.calls) > calls_after_first, "跨 quote 互用了缓存(隔离破)"
    assert not any(q.get("cache_hit") for q in pack2["queries"])


async def test_empty_scope_never_caches(provider_spy) -> None:
    # 反向对照:scope 空 = 不缓存,与旧签名行为一致
    await _collect("同一标题", "")
    calls_after_first = len(provider_spy.calls)
    await _collect("同一标题", "")
    assert len(provider_spy.calls) == calls_after_first * 2


# ------------------------------------------------------------------ 接线锁
def test_ags_passes_quote_scope() -> None:
    """接线不是函数存在性:调用点必须真传 cache_scope=quote_id。"""
    import inspect

    import writing.article_generator_service as ags

    src = inspect.getsource(ags)
    assert 'cache_scope=str(getattr(self, "quote_id"' in src, (
        "article_generator_service 没把 quote_id 传进 cache_scope —— 缓存层是死件"
    )
