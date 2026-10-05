"""搜索 Provider 适配层判别锁(工单 SEARCH_PROVIDER_LAYER 2026-07-28 · §5 七组)。

1. 结构性零变化:默认配置(全 metaso)下三个路由入口对同输入行为与改前逐字节一致;
2. 报价隔离底线:场景键注册表不存在报价场景;报价 7 文件不 import 适配层;
3. shim 字段映射:消费字段逐一断言(含真实消费方 _v36_metaso_pages 全链);
4. 回退链:豆包 500/空结果 → 秘塔原参数原样重放;
5. 节流:令牌桶任意 1s 窗口 ≤5 次;客户端发请求前必过桶;429 退避重试 1 次;
6. scholar:深档研究计划含学术层 query;条目带层级标记;解析 scholars 键;
   不可查证条目不放行;B 级(citations/scholar)钉死秘塔直连不进路由;
7. 成本埋点:doubao_search 调用落 llm_call_tracker(按次 flat)。
附:未配置 DOUBAO_SEARCH_API_KEY → 豆包路由整体回退秘塔;预留场景键本单不开。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from agentscope.tool import ToolResponse

import importlib
import tools.search.doubao_search as ds
import tools.search.metaso_mcp as metaso_mcp
import tools.search.provider_router as pr

# 包 __init__ 里 `from .metaso_search import metaso_search` 会用同名函数遮蔽子模块属性,
# `import tools.search.metaso_search as X` 会拿到函数而不是模块 —— 必须走 importlib。
metaso_search_mod = importlib.import_module("tools.search.metaso_search")

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
DOUBAO_RAW = json.loads((FIXTURES / "doubao_web_search_raw_2026_07_28.json").read_text(encoding="utf-8"))
SCHOLAR_INNER = json.loads((FIXTURES / "metaso_scholar_inner_2026_07_28.json").read_text(encoding="utf-8"))

#: 工单 §1-A 报价链 7 文件(零触碰 · 物理隔离)。
PRICING_FILES = [
    ROOT / "tools" / "transparent_pricing.py",
    ROOT / "tools" / "batch_pricing.py",
    ROOT / "tools" / "keyword_value_scorer.py",
    ROOT / "tools" / "pricing_bands.py",
    ROOT / "tools" / "pricing_auditor.py",
    ROOT / "tools" / "c_end_cost_estimate.py",
    ROOT / "tools" / "geo_managed" / "estimate_engine.py",
]


def _clean_env(monkeypatch):
    for key in list(pr.SEARCH_SCENARIO_REGISTRY):
        monkeypatch.delenv(f"SEARCH_PROVIDER_{key.upper()}", raising=False)
    monkeypatch.delenv("SEARCH_PROVIDER_PRICING", raising=False)
    monkeypatch.delenv("DOUBAO_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("DOUBAO_SEARCH_QPS", raising=False)


def _grayscale_env(monkeypatch, scenario: str):
    _clean_env(monkeypatch)
    monkeypatch.setenv(f"SEARCH_PROVIDER_{scenario.upper()}", "doubao")
    monkeypatch.setenv("DOUBAO_SEARCH_API_KEY", "test-key-not-real")


def _sentinel_response(tag: str) -> ToolResponse:
    return ToolResponse(content=[{"type": "text", "text": json.dumps({"sentinel": tag})}])


def _doubao_bomb(monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("豆包客户端不应被调用")

    monkeypatch.setattr(ds, "doubao_web_search_raw", boom)
    monkeypatch.setattr(ds, "doubao_search_webpages", boom)


# ===========================================================================
# §5.1 结构性零变化锁
# ===========================================================================
def test_default_config_web_search_bytes_identical_and_no_doubao(monkeypatch):
    """默认配置:metaso_web_search 原参数原样直达秘塔实现,豆包零参与。

    变异(路由条件 fail-open,如 env 缺省当 doubao)→ 本锁转红。
    """
    _clean_env(monkeypatch)
    _doubao_bomb(monkeypatch)
    recorded = {}
    sentinel = _sentinel_response("direct")

    async def fake_direct(query, scope="webpage", include_summary=True, include_raw_content=False, size=20):
        recorded.update(query=query, scope=scope, include_summary=include_summary,
                        include_raw_content=include_raw_content, size=size)
        return sentinel

    monkeypatch.setattr(metaso_mcp, "_metaso_web_search_direct", fake_direct)
    result = asyncio.run(metaso_mcp.metaso_web_search(
        "q1", scope="webpage", include_summary=False, include_raw_content=True, size=7,
    ))
    assert result is sentinel  # 原样透传,不重新包装
    assert recorded == {
        "query": "q1", "scope": "webpage", "include_summary": False,
        "include_raw_content": True, "size": 7,
    }


def test_default_config_metaso_search_and_citation_search_delegate_verbatim(monkeypatch):
    _clean_env(monkeypatch)
    _doubao_bomb(monkeypatch)
    recorded = {}
    sentinel = _sentinel_response("direct2")

    async def fake_direct(query, scope="webpage", include_summary=True, size=20,
                          include_raw_content=False, concise_snippet=True):
        recorded.update(query=query, scope=scope, include_summary=include_summary, size=size,
                        include_raw_content=include_raw_content, concise_snippet=concise_snippet)
        return sentinel

    monkeypatch.setattr(metaso_search_mod, "_metaso_search_direct", fake_direct)
    result = asyncio.run(metaso_search_mod.metaso_search("q2", size=9, concise_snippet=False))
    assert result is sentinel
    assert recorded["query"] == "q2" and recorded["size"] == 9 and recorded["concise_snippet"] is False

    # citation_search:默认配置只传 (query, size=),与老调用点签名逐字一致
    calls = []
    citation_sentinel = {"citations": [], "sentinel": True}

    async def fake_citations(query, size=30):
        calls.append((query, size))
        return citation_sentinel

    monkeypatch.setattr(metaso_mcp, "metaso_search_with_citations", fake_citations)
    result = asyncio.run(pr.citation_search("q3", size=5, scenario="evidence"))
    assert result is citation_sentinel
    assert calls == [("q3", 5)]


def test_scenario_context_without_env_still_all_metaso(monkeypatch):
    """场景键在场、key 也配了,但没开 SEARCH_PROVIDER_* env → 仍全 metaso。

    key 单独在场不构成放行条件 —— 杀"env 闸被删"的语义变异(fail-open)。
    """
    _clean_env(monkeypatch)
    monkeypatch.setenv("DOUBAO_SEARCH_API_KEY", "test-key-not-real")
    _doubao_bomb(monkeypatch)
    sentinel = _sentinel_response("direct3")

    async def fake_direct(*args, **kwargs):
        return sentinel

    monkeypatch.setattr(metaso_mcp, "_metaso_web_search_direct", fake_direct)
    with pr.search_scenario("research"):
        assert pr.resolve_provider() == "metaso"
        result = asyncio.run(metaso_mcp.metaso_web_search("q"))
    assert result is sentinel


# ===========================================================================
# §5.1/§1-B B 级钉死:引用链与 scholar 永不进路由
# ===========================================================================
def test_b_level_citations_pinned_direct_even_in_grayscale(monkeypatch):
    """evidence 灰度全开 + 场景上下文内,metaso_search_with_citations 仍秘塔直连。

    豆包替身返回**特征 URL**而不是抛错(抛错会被路由入口的回退链吞掉,变异就漏杀):
    引用链一旦被换源,特征 URL 必然出现在 citations 里 → 转红。
    变异(引用链改调路由入口 metaso_web_search)→ 本锁转红。
    """
    _grayscale_env(monkeypatch, "evidence")

    async def doubao_fingerprint(query, *, size=15, **kwargs):
        return [{"title": "mutant", "link": "https://doubao-mutant.example.com/1",
                 "url": "https://doubao-mutant.example.com/1", "content": "x",
                 "rawContent": "x", "summary": "x", "snippet": "x", "date": "", "position": 1}]

    monkeypatch.setattr(ds, "doubao_search_webpages", doubao_fingerprint)
    inner = json.dumps({"webpages": [{"title": "t", "link": "https://a.example.com/1", "snippet": "s"}]},
                       ensure_ascii=False)
    payload = {"status": "success", "query": "q", "scope": "webpage",
               "results": [{"type": "text", "text": inner}], "count": 1}

    async def fake_direct(*args, **kwargs):
        return ToolResponse(content=[{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}])

    monkeypatch.setattr(metaso_mcp, "_metaso_web_search_direct", fake_direct)
    with pr.search_scenario("evidence"):
        assert pr.resolve_provider() == "doubao"  # 场景确实解析为豆包
        result = asyncio.run(metaso_mcp.metaso_search_with_citations("q", size=5))
    urls = [c["url"] for c in result["citations"]]
    assert urls == ["https://a.example.com/1"]  # 只有秘塔直连结果
    assert "https://doubao-mutant.example.com/1" not in urls  # 引用链零豆包参与


def test_scholar_search_pinned_direct_even_in_grayscale(monkeypatch):
    _grayscale_env(monkeypatch, "evidence")
    _doubao_bomb(monkeypatch)
    monkeypatch.setattr(metaso_mcp, "_metaso_web_search_direct", _fake_scholar_direct())
    with pr.search_scenario("evidence"):
        entries = asyncio.run(pr.scholar_evidence_search("人造板 甲醛"))
    assert entries  # 秘塔直连返回,豆包炸弹未响


# ===========================================================================
# §5.2 报价隔离底线锁
# ===========================================================================
def test_registry_contains_no_pricing_scenario_key(monkeypatch):
    """场景键注册表不存在任何报价链场景。变异(注册 pricing 键)→ import 期守卫拒绝。"""
    forbidden = ("pricing", "quote", "estimate", "competition", "报价", "算价")
    for key in pr.SEARCH_SCENARIO_REGISTRY:
        assert not any(token in key for token in forbidden), key
    # 源码里必须存在 import 期守卫(变异删守卫 → 本断言转红)
    src = (ROOT / "tools" / "search" / "provider_router.py").read_text(encoding="utf-8")
    assert "报价链场景键禁止进入搜索 provider 路由注册表" in src
    # 未注册键即使配了 env 也永远 metaso
    _clean_env(monkeypatch)
    monkeypatch.setenv("SEARCH_PROVIDER_PRICING", "doubao")
    monkeypatch.setenv("DOUBAO_SEARCH_API_KEY", "test-key-not-real")
    assert pr.resolve_provider("pricing") == "metaso"


def test_pricing_seven_files_never_touch_provider_layer():
    """报价 7 文件零触碰:不 import 适配层任何符号(物理隔离的静态面)。

    变异(给 transparent_pricing 加 provider_router import)→ 本锁转红。
    """
    for path in PRICING_FILES:
        src = path.read_text(encoding="utf-8")
        for symbol in ("provider_router", "doubao_search", "search_scenario", "resolve_provider"):
            assert symbol not in src, f"{path.name} 引用了适配层符号 {symbol}"
    # 报价链的 search_metaso(competition_analyzer)也不进路由
    analyzer = (ROOT / "tools" / "competition_analyzer.py").read_text(encoding="utf-8")
    for symbol in ("provider_router", "doubao_search", "search_scenario"):
        assert symbol not in analyzer


# ===========================================================================
# §5.3 shim 字段映射锁
# ===========================================================================
def test_shim_maps_every_consumed_field():
    """Url→link · Title→title · Content→content/rawContent · Summary→summary ·
    Snippet→snippet。变异(漏映射 content)→ 本锁转红。"""
    raw = DOUBAO_RAW["Result"]["WebResults"][0]
    row = ds.shim_webpage_row(raw, position=3)
    assert row["link"] == raw["Url"] and row["url"] == raw["Url"]
    assert row["title"] == raw["Title"]
    assert row["content"] == raw["Content"]
    assert row["rawContent"] == raw["Content"]
    assert row["summary"] == raw["Summary"]
    assert row["snippet"] == raw["Snippet"]
    assert row["position"] == 3


def test_doubao_path_wire_shape_through_real_v36_consumer(monkeypatch):
    """全链行为锁:research 灰度开 → 真实消费方 _v36_metaso_pages 拿到带正文的行。

    双层 JSON wire 形状必须与秘塔 MCP 完全一致,消费方零改动。
    """
    _grayscale_env(monkeypatch, "research")

    async def fake_raw(query, **kwargs):
        return list(DOUBAO_RAW["Result"]["WebResults"])

    monkeypatch.setattr(ds, "doubao_web_search_raw", fake_raw)
    from tools.industry_knowledge_collector import _v36_metaso_pages

    pages = asyncio.run(_v36_metaso_pages("南山区全屋定制", size=5))
    assert len(pages) == 2  # 第 3 行无 Content,被消费方原有 content 过滤器滤掉
    assert pages[0]["url"] == DOUBAO_RAW["Result"]["WebResults"][0]["Url"]
    assert pages[0]["title"].startswith("南山区全屋定制")
    assert "ENF" in pages[0]["content"]
    assert pages[0]["snippet"]


def test_doubao_respects_include_raw_content_flag(monkeypatch):
    """include_raw_content=False → 不向豆包要正文,行内也不带正文(只留摘要)。

    🔴 自审抓到的隐性质量回归:消费方(industry_knowledge_collector 的
    `_metaso_search_safe`)把序列化 JSON **截前 2000 字**喂 LLM。无视该参数一律
    内联全文 → 一条正文吃光窗口,8 条召回退化成 1 条(结构没变、内容口径变了)。
    变异(把 need_content 写死 True)→ 本锁转红。
    """
    _grayscale_env(monkeypatch, "research")
    asked: list[bool] = []

    async def fake_raw(query, *, count=50, need_content=True, **kwargs):
        asked.append(need_content)
        rows = []
        for raw in DOUBAO_RAW["Result"]["WebResults"]:
            row = dict(raw)
            if not need_content:
                row["Content"] = ""      # 供应商未返回正文时的真实形状
            rows.append(row)
        return rows

    monkeypatch.setattr(ds, "doubao_web_search_raw", fake_raw)

    # 默认(include_raw_content=False):不要正文
    with pr.search_scenario("research"):
        response = asyncio.run(metaso_mcp.metaso_web_search("q", size=8))
    assert asked == [False]
    inner = json.loads(json.loads(response.content[0]["text"])["results"][0]["text"])
    assert all(not row["content"] and not row["rawContent"] for row in inner["webpages"])
    assert any(row["summary"] or row["snippet"] for row in inner["webpages"])  # 摘要仍在

    # 显式要正文(_v36_metaso_pages 的调用口径):正文必须真的进来
    asked.clear()
    with pr.search_scenario("research"):
        response = asyncio.run(
            metaso_mcp.metaso_web_search("q", size=8, include_raw_content=True)
        )
    assert asked == [True]
    inner = json.loads(json.loads(response.content[0]["text"])["results"][0]["text"])
    assert any("ENF" in row["content"] for row in inner["webpages"])


def test_extract_citations_works_on_shim_rows():
    rows = [ds.shim_webpage_row(r, position=i + 1)
            for i, r in enumerate(DOUBAO_RAW["Result"]["WebResults"])]
    data = metaso_mcp.extract_citations({"webpages": rows})
    assert len(data["citations"]) == 3
    first = data["citations"][0]
    assert first["url"] == rows[0]["link"]
    assert first["title"] == rows[0]["title"]
    assert first["snippet"]


def test_citation_search_doubao_carries_inline_content(monkeypatch):
    """evidence 灰度:citation 带 raw_content(豆包自带正文,evidence 免 reader 一跳)。"""
    _grayscale_env(monkeypatch, "evidence")

    async def fake_webpages(query, size=15, **kwargs):
        return [ds.shim_webpage_row(r, position=i + 1)
                for i, r in enumerate(DOUBAO_RAW["Result"]["WebResults"])]

    monkeypatch.setattr(ds, "doubao_search_webpages", fake_webpages)
    result = asyncio.run(pr.citation_search("q", size=5, scenario="evidence"))
    assert result["source"] == "doubao_search"
    assert result["result_count"] == 3
    by_url = {c["url"]: c for c in result["citations"]}
    content_row = DOUBAO_RAW["Result"]["WebResults"][0]
    assert by_url[content_row["Url"]]["raw_content"] == content_row["Content"]
    assert "raw_content" not in by_url[DOUBAO_RAW["Result"]["WebResults"][2]["Url"]]  # 无正文行不造键


# ===========================================================================
# §5.4 回退链锁
# ===========================================================================
def test_fallback_to_metaso_on_doubao_error_and_empty(monkeypatch):
    """豆包异常/空结果 → 秘塔原参数原样重放。变异(去回退直接抛)→ 本锁转红。"""
    _grayscale_env(monkeypatch, "research")
    sentinel = _sentinel_response("fallback")
    recorded = []

    async def fake_direct(query, scope="webpage", include_summary=True, include_raw_content=False, size=20):
        recorded.append((query, scope, include_summary, include_raw_content, size))
        return sentinel

    monkeypatch.setattr(metaso_mcp, "_metaso_web_search_direct", fake_direct)

    async def raise_error(*args, **kwargs):
        raise ds.DoubaoSearchError("HTTP 500")

    monkeypatch.setattr(ds, "doubao_web_search_raw", raise_error)
    with pr.search_scenario("research"):
        result = asyncio.run(metaso_mcp.metaso_web_search("q9", include_raw_content=True, size=4))
    assert result is sentinel
    assert recorded == [("q9", "webpage", True, True, 4)]  # 原参数原样

    async def empty(*args, **kwargs):
        raise ds.DoubaoSearchEmpty("empty WebResults")

    recorded.clear()
    monkeypatch.setattr(ds, "doubao_web_search_raw", empty)
    with pr.search_scenario("research"):
        result = asyncio.run(metaso_mcp.metaso_web_search("q10"))
    assert result is sentinel and recorded[0][0] == "q10"


def test_citation_search_falls_back_with_original_args(monkeypatch):
    _grayscale_env(monkeypatch, "evidence")
    calls = []
    sentinel = {"citations": [], "fallback": True}

    async def fake_citations(query, size=30):
        calls.append((query, size))
        return sentinel

    monkeypatch.setattr(metaso_mcp, "metaso_search_with_citations", fake_citations)

    async def raise_error(*args, **kwargs):
        raise ds.DoubaoSearchError("boom")

    monkeypatch.setattr(ds, "doubao_search_webpages", raise_error)
    result = asyncio.run(pr.citation_search("qq", size=6, scenario="evidence"))
    assert result is sentinel and calls == [("qq", 6)]


# ===========================================================================
# §5.5 节流锁(令牌桶 5 QPS)+ 429 退避重试
# ===========================================================================
def test_token_bucket_caps_any_one_second_window_at_qps():
    """虚拟时钟:20 个并发 acquire,任意 1s 窗口放行 ≤5。变异(去节流)→ 转红。"""
    clock = {"now": 0.0}

    async def virtual_sleep(seconds):
        clock["now"] += max(0.0, seconds)

    bucket = ds.TokenBucket(5, time_fn=lambda: clock["now"], sleep_fn=virtual_sleep)

    async def run():
        return await asyncio.gather(*[bucket.acquire() for _ in range(20)])

    slots = sorted(asyncio.run(run()))
    assert len(slots) == 20
    for start in slots:
        # 1e-6 容差只吃浮点累积误差(0.2*5 的累加),不放松 5 QPS 语义
        in_window = [s for s in slots if start <= s < start + 1.0 - 1e-6]
        assert len(in_window) <= 5, f"1s 窗口内放行 {len(in_window)} 次 > 5"
    assert slots[-1] - slots[0] >= (20 - 1) * (1.0 / 5) - 1e-6  # 严格间隔调度


def test_client_awaits_throttle_before_post(monkeypatch):
    """客户端每次发请求前必过令牌桶。变异(绕开 acquire)→ 本锁转红。"""
    monkeypatch.setenv("DOUBAO_SEARCH_API_KEY", "test-key-not-real")
    acquired = []

    class RecordingBucket:
        async def acquire(self):
            acquired.append(1)
            return 0.0

    monkeypatch.setattr(ds, "_bucket", lambda: RecordingBucket())
    monkeypatch.setattr(ds.httpx, "AsyncClient", _fake_async_client([(200, DOUBAO_RAW)]))
    rows = asyncio.run(ds.doubao_web_search_raw("q"))
    assert len(rows) == 3
    assert acquired == [1]


def test_retry_once_on_429_then_success_and_5xx_exhaust_raises(monkeypatch):
    monkeypatch.setenv("DOUBAO_SEARCH_API_KEY", "test-key-not-real")
    monkeypatch.setattr(ds, "_bucket", lambda: _NoopBucket())
    monkeypatch.setattr(ds, "_RETRY_BACKOFF_S", 0)
    monkeypatch.setattr(ds.httpx, "AsyncClient", _fake_async_client([(429, {}), (200, DOUBAO_RAW)]))
    rows = asyncio.run(ds.doubao_web_search_raw("q"))
    assert len(rows) == 3  # 429 → 退避重试 1 次 → 成功

    monkeypatch.setattr(ds.httpx, "AsyncClient", _fake_async_client([(500, {}), (500, {})]))
    with pytest.raises(ds.DoubaoSearchError):
        asyncio.run(ds.doubao_web_search_raw("q"))  # 重试 1 次仍失败 → 抛给回退链


# ===========================================================================
# §5.6 scholar 锁
# ===========================================================================
def _fake_scholar_direct():
    """替身**按 scope 返回不同内层键**,与秘塔真实行为一致(2026-07-29 生产实测)。

    🔴 原替身写的是 ``assert scope == "paper"`` —— 它不是忽略 scope,而是**钉死了错的值**:
    生产实测 ``scope='paper'`` 返回的内层键是 **webpages**,只有 ``scope='scholar'``
    才返回 **scholars**。于是 `scholar_evidence_search` 线上恒返 0 条(学术层 100% 无产出、
    每篇深档白烧 ¥0.10),而这条测试**反过来锁死了 bug**:谁把 scope 改对,assert 就炸。

    教训:替身钉入参时,钉的值必须来自**真实 API 实测**,不能来自作者的假设 ——
    否则测试从"防回归"变成"防修复"。
    """
    scholar_inner = json.dumps(
        {"scholars": SCHOLAR_INNER["scholars"],
         "webpages": [{"title": "错键网页行", "link": "https://wrong.example.com/x"}]},
        ensure_ascii=False,
    )
    # paper scope 的真实形状:只有 webpages,没有 scholars。
    paper_inner = json.dumps(
        {"webpages": [{"title": "paper scope 返回的网页行", "link": "https://wrong.example.com/paper"}]},
        ensure_ascii=False,
    )

    async def fake_direct(query, scope="webpage", include_summary=True, include_raw_content=False, size=20):
        inner = scholar_inner if scope == "scholar" else paper_inner
        payload = {"status": "success", "query": "q", "scope": scope,
                   "results": [{"type": "text", "text": inner}], "count": 3}
        return ToolResponse(content=[{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}])

    return fake_direct


def test_scholar_search_issues_scholar_scope_not_paper(monkeypatch):
    """🔴 学术检索必须发 ``scope='scholar'`` —— 发 paper 拿到的是 webpages,解析恒零。

    2026-07-29 生产实测(同一函数、同一 query):
        scope='paper'   → 内层键 ['credits','searchParameters','total','webpages']
        scope='scholar' → 内层键 ['credits','searchParameters','scholars','total']

    变异(scope 改回 'paper')→ 本锁转红。
    """
    _clean_env(monkeypatch)
    seen: dict = {}

    async def recording_direct(query, scope="webpage", include_summary=True,
                               include_raw_content=False, size=20):
        seen["scope"] = scope
        return await _fake_scholar_direct()(
            query, scope=scope, include_summary=include_summary,
            include_raw_content=include_raw_content, size=size,
        )

    monkeypatch.setattr(metaso_mcp, "_metaso_web_search_direct", recording_direct)
    entries = asyncio.run(pr.scholar_evidence_search("人造板 甲醛释放量"))
    assert seen["scope"] == "scholar", (
        f"学术检索发了 scope={seen['scope']!r};秘塔只在 scope='scholar' 时返回 scholars 键,"
        "发 paper 会拿到 webpages → 解析恒零(生产实测)"
    )
    assert entries, "scope 正确时必须有可查证条目"


def test_scholar_parses_scholars_key_and_rejects_unverifiable(monkeypatch):
    """🔴 解析 scholars 键(错读 webpages 得全零);不可查证条目不放行。

    变异(解析 webpages / 放行不可查证)→ 本锁转红。
    """
    _clean_env(monkeypatch)
    monkeypatch.setattr(metaso_mcp, "_metaso_web_search_direct", _fake_scholar_direct())
    entries = asyncio.run(pr.scholar_evidence_search("人造板 甲醛释放量"))
    titles = [e["title"] for e in entries]
    assert "人造板甲醛释放量检测方法的影响因素" in titles      # authors+date 齐 → 放行
    assert any(t.startswith("Low-Odor") for t in titles)        # doi.org 链接 → 放行
    assert "某论坛转载的板材经验帖" not in titles               # 不可查证 → 拒
    assert "错键网页行" not in titles                            # 不吃 webpages 键
    assert entries[0]["authors"] == ["管民"] and entries[0]["date"] == "2012-06-05"


def test_deep_tier_plan_contains_scholar_layer_with_tier_marker(monkeypatch):
    """深档研究计划含学术层 query(≤2);条目带 scholar_citation 层级并可被写作渲染。

    变异(砍学术层)→ 本锁转红。
    """
    import writing.evidence_research as er

    _clean_env(monkeypatch)
    search_calls = []

    async def fake_citation_search(query, *, size=5, scope="webpage", scenario=None):
        search_calls.append(query)
        return {"citations": []}

    async def fake_scholar(query, *, size=8):
        return [{
            "title": "人造板甲醛释放量检测方法的影响因素",
            "authors": ["管民"], "date": "2012-06-05",
            "link": "https://s.wanfangdata.com.cn/paper?q=%E4%BA%BA",
            "score": "high", "snippet": "检测方法对比。",
        }]

    monkeypatch.setattr(pr, "citation_search", fake_citation_search)
    monkeypatch.setattr(pr, "scholar_evidence_search", fake_scholar)

    pack = asyncio.run(er.collect_evidence_pack(
        title="全屋定制板材怎么选", keyword="全屋定制板材", industry="全屋定制",
        client_brand="测试品牌", competitor_names=["竞品甲"], request_id="sp-t",
        force=True, deep_tier=True, whitelist_names=["测试品牌", "竞品甲"],
    ))
    scholar_queries = [q for q in pack["queries"] if q.get("lane_kind") == "scholar"]
    assert 1 <= len(scholar_queries) <= 2
    scholar_items = [i for i in pack["items"] if i.get("source_tier") == "scholar_citation"]
    assert scholar_items, "学术条目必须以学术层级进 pack"
    item = scholar_items[0]
    assert "管民" in item["claim"] and "2012" in item["claim"]  # 作者/年份可被写作消费
    assert item["published_at"] == "2012-06-05"
    assert pack["evidence_supply"]["scholar_queries_issued"] == len(scholar_queries)
    assert pack["evidence_supply"]["scholar_item_count"] == len(scholar_items)
    # 写作渲染面:学术引用真实出现在给 writer 的 Evidence 文本里
    from writing.evidence_pack import render_evidence_pack_for_writer

    rendered = render_evidence_pack_for_writer(pack)
    assert "人造板甲醛释放量检测方法的影响因素" in rendered and "管民" in rendered


def test_compact_tier_has_no_scholar_calls(monkeypatch):
    """紧凑档零新增调用(既有锁口径):学术层只对深档生效。"""
    import writing.evidence_research as er

    _clean_env(monkeypatch)

    async def fake_citation_search(query, *, size=5, scope="webpage", scenario=None):
        return {"citations": []}

    async def fake_scholar(query, *, size=8):
        raise AssertionError("紧凑档不应触发学术检索")

    monkeypatch.setattr(pr, "citation_search", fake_citation_search)
    monkeypatch.setattr(pr, "scholar_evidence_search", fake_scholar)
    pack = asyncio.run(er.collect_evidence_pack(
        title="t", keyword="k", industry="i", client_brand="b",
        request_id="sp-c", force=True, deep_tier=False,
    ))
    assert not [q for q in pack["queries"] if q.get("lane_kind") == "scholar"]


def test_evidence_uses_doubao_inline_content_without_reader(monkeypatch):
    """evidence 灰度:citation 自带正文 → 直接作 body,reader 零调用(成本 ≤¥1.5 的来源)。"""
    import writing.evidence_research as er

    _clean_env(monkeypatch)
    content_row = DOUBAO_RAW["Result"]["WebResults"][0]

    async def fake_citation_search(query, *, size=5, scope="webpage", scenario=None):
        assert scenario == "evidence"  # evidence 场景键在检索调用点登记
        return {"citations": [{
            "title": content_row["Title"], "url": content_row["Url"],
            "snippet": content_row["Snippet"], "source": "m.xnnews.com.cn",
            "date": "", "authors": [], "is_authority": False, "position": 1,
            "raw_content": content_row["Content"],
        }]}

    async def no_reader(url, format="markdown"):
        raise AssertionError("自带正文时不应调用 reader")

    async def fake_scholar(query, *, size=8):
        return []

    monkeypatch.setattr(pr, "citation_search", fake_citation_search)
    monkeypatch.setattr(pr, "scholar_evidence_search", fake_scholar)
    monkeypatch.setattr(metaso_mcp, "metaso_web_reader", no_reader)
    monkeypatch.setattr(er, "_load_jc3_body", lambda url: ("", {}))

    # 核验器替身:本锁只验"内联正文替代 reader",核验产出不在锁面(且不许真调 LLM)
    import writing.evidence_verifier as ev

    async def no_verify(candidates, **kwargs):
        return {}

    monkeypatch.setattr(ev, "select_and_verify_claim_spans", no_verify)
    pack = asyncio.run(er.collect_evidence_pack(
        title="t", keyword="南山区全屋定制", industry="全屋定制", client_brand="b",
        request_id="sp-i", force=True, deep_tier=True, whitelist_names=["b"],
    ))
    items = [i for i in pack["items"] if i["url"] == content_row["Url"]]
    assert items and items[0]["verification_status"] == "body_retrieved_claim_unverified"
    assert items[0]["body_boundary_version"] == "provider-inline-content-v1"
    assert "ENF" in items[0]["excerpt"]


# ===========================================================================
# §5.7 成本埋点锁
# ===========================================================================
def test_cost_tracking_doubao_search_flat_002(monkeypatch):
    from tools.llm_call_tracker import PRICING_TABLE, estimate_cost

    assert PRICING_TABLE[("doubao_search", "web_search")]["flat_rate_per_call"] == 0.013
    assert estimate_cost("doubao_search", "web_search", 0, 0) == 0.013

    monkeypatch.setenv("DOUBAO_SEARCH_API_KEY", "test-key-not-real")
    monkeypatch.setattr(ds, "_bucket", lambda: _NoopBucket())
    tracked = []

    class _FakeTracker:
        def record(self, **kwargs):
            tracked.append(("record", kwargs))

    class _FakeTrackCM:
        def __init__(self, name, platform, **kwargs):
            tracked.append((name, platform))

        async def __aenter__(self):
            return _FakeTracker()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(ds, "llm_track", _FakeTrackCM)
    monkeypatch.setattr(ds.httpx, "AsyncClient", _fake_async_client([(200, DOUBAO_RAW)]))
    asyncio.run(ds.doubao_web_search_raw("q"))
    assert ("doubao_web_search", "doubao_search") in tracked  # 同口径埋点
    assert any(t[0] == "record" and t[1].get("success") for t in tracked)


# ===========================================================================
# §6 附加锁:未配置 key 整体回退 / 预留键不开 / 满配默认 / 多角度聚合
# ===========================================================================
def test_no_api_key_routes_fully_back_to_metaso(monkeypatch):
    """env 开了 doubao 但 DOUBAO_SEARCH_API_KEY 未配置 → 整体回退秘塔。"""
    _clean_env(monkeypatch)
    monkeypatch.setenv("SEARCH_PROVIDER_EVIDENCE", "doubao")
    assert pr.resolve_provider("evidence") == "metaso"
    _doubao_bomb(monkeypatch)
    sentinel = _sentinel_response("nokey")

    async def fake_direct(*args, **kwargs):
        return sentinel

    monkeypatch.setattr(metaso_mcp, "_metaso_web_search_direct", fake_direct)
    with pr.search_scenario("evidence"):
        assert asyncio.run(metaso_mcp.metaso_web_search("q")) is sentinel


def test_reserved_scenarios_not_opened_this_round(monkeypatch):
    """写作素材/诊断链/agent_loop 预留不开:env 设了也不路由(第二批再放)。"""
    _clean_env(monkeypatch)
    monkeypatch.setenv("DOUBAO_SEARCH_API_KEY", "test-key-not-real")
    for key in ("writing_material", "diagnosis", "agent_loop"):
        monkeypatch.setenv(f"SEARCH_PROVIDER_{key.upper()}", "doubao")
        assert key in pr.SEARCH_SCENARIO_REGISTRY
        assert pr.resolve_provider(key) == "metaso"


def test_doubao_client_defaults_are_full_spec():
    """三件套满配:Count=50 + NeedContent=true 是客户端默认值(按次 flat,多要不加钱)。"""
    import inspect

    sig = inspect.signature(ds.doubao_web_search_raw)
    assert sig.parameters["count"].default == 50
    assert sig.parameters["need_content"].default is True
    sig2 = inspect.signature(ds.doubao_search_webpages)
    assert sig2.parameters["size"].default == 50


def test_multi_angle_search_dedupes_and_falls_back(monkeypatch):
    dup_url = "https://dup.example.com/1"

    async def fake_webpages(query, *, size=50, need_content=True):
        if "角度B" in query:
            raise ds.DoubaoSearchError("down")
        return [{"link": dup_url, "title": "dup"},
                {"link": f"https://uniq.example.com/{query[-2:]}", "title": query}]

    async def fallback(query, size):
        return [{"link": "https://fallback.example.com/1", "title": "fb"}]

    monkeypatch.setattr(ds, "doubao_search_webpages", fake_webpages)
    result = asyncio.run(ds.multi_angle_search("主题", ["角度A", "角度B", "角度C"], fallback_search=fallback))
    urls = [r["link"] for r in result["rows"]]
    assert urls.count(dup_url) == 1                       # URL 去重
    assert "https://fallback.example.com/1" in urls        # 单 query 失败走回退
    assert result["query_count"] == 3 and result["fallback_query_count"] == 1


def test_grayscale_wiring_pinned_at_consumers():
    """灰度第一批接线钉在源码:调研抓取(collector 两入口)+ evidence(检索调用点)。"""
    collector = (ROOT / "tools" / "industry_knowledge_collector.py").read_text(encoding="utf-8")
    assert collector.count('with search_scenario("research")') == 2
    evidence = (ROOT / "writing" / "evidence_research.py").read_text(encoding="utf-8")
    assert 'scenario="evidence"' in evidence
    # B 级钉死的源码面(行为面见 test_b_level_citations_pinned_direct_even_in_grayscale)
    mcp_src = (ROOT / "tools" / "search" / "metaso_mcp.py").read_text(encoding="utf-8")
    citations_body = mcp_src.split("async def metaso_search_with_citations")[1].split("async def ")[0]
    assert "_metaso_web_search_direct(" in citations_body
    assert "await metaso_web_search(" not in citations_body


# ===========================================================================
# 测试基建
# ===========================================================================
class _NoopBucket:
    async def acquire(self):
        return 0.0


def _fake_async_client(responses):
    """按序返回 (status_code, json_body) 的 httpx.AsyncClient 替身工厂。"""
    queue = list(responses)

    class FakeResponse:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body
            self.text = json.dumps(body, ensure_ascii=False)

        def json(self):
            return self._body

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            status, body = queue.pop(0)
            return FakeResponse(status, body)

    return FakeClient
