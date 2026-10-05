"""搜索 Provider 场景键路由 —— "豆包管量、秘塔管质、报价链谁都不碰"。

工单 SEARCH_PROVIDER_LAYER 2026-07-28 · 五条纪律:
1. 适配层只做在 tools/search/ 入口内部,31 个消费方零改动;
2. 默认配置(不设任何 ``SEARCH_PROVIDER_*`` env)= 全 metaso = 与今日逐字节一致;
3. 🔴 报价链物理隔离:本注册表**不存在**任何报价场景键(下方 import 期守卫),
   报价 7 文件(transparent_pricing/batch_pricing/keyword_value_scorer/pricing_bands/
   pricing_auditor/c_end_cost_estimate/geo_managed.estimate_engine)零触碰 ——
   报价换搜索源 = 报价数值漂移 = 同时打破 7 天价格锁 / C 端代理端同函数 / 价格稳定三铁律;
4. 🔴 B 级不可替:metaso_chat / metaso_scholar_search / metaso_document_search /
   metaso_search_with_citations 保持秘塔直连,不进路由;
5. 本单只开第一批灰度(D 级新场景 + evidence / research 两个场景键);
   写作素材 / 诊断链 / agent_loop 场景键预留不开(env 设了也不路由)。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
import re
from typing import Any, Final
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# 场景键注册表
# ---------------------------------------------------------------------------
SCENARIO_EVIDENCE: Final = "evidence"          # Evidence Pack 证据采集(写作前研究)
SCENARIO_RESEARCH: Final = "research"          # 调研抓取(行业语料/知识采集)
SCENARIO_DEEP_RESEARCH: Final = "deep_research"  # D 级新场景:深档逐家检索/调研召回扩容

#: 预留场景键(第二批放量,本单不开)。
_RESERVED_SCENARIOS: Final = ("writing_material", "diagnosis", "agent_loop")

SEARCH_SCENARIO_REGISTRY: Final = frozenset({
    SCENARIO_EVIDENCE,
    SCENARIO_RESEARCH,
    SCENARIO_DEEP_RESEARCH,
    *_RESERVED_SCENARIOS,
})

#: 本单第一批灰度 —— 只有这些键允许被 env 路由到豆包。
GRAYSCALE_FIRST_BATCH: Final = frozenset({
    SCENARIO_EVIDENCE,
    SCENARIO_RESEARCH,
    SCENARIO_DEEP_RESEARCH,
})

# 🔴 底线锁(工单 §5.2):注册表出现报价类场景键 → import 期直接拒绝,全站转红。
_FORBIDDEN_SCENARIO_TOKENS: Final = ("pricing", "quote", "estimate", "competition", "报价", "算价")
for _key in SEARCH_SCENARIO_REGISTRY:
    if any(token in _key for token in _FORBIDDEN_SCENARIO_TOKENS):
        raise RuntimeError(f"报价链场景键禁止进入搜索 provider 路由注册表: {_key!r}")

_ACTIVE_SCENARIO: ContextVar[str | None] = ContextVar("search_provider_scenario", default=None)


@contextmanager
def search_scenario(key: str):
    """在上下文内注册场景键(零改动接线方式:入口包一层,深层调用自动继承)。"""
    if key not in SEARCH_SCENARIO_REGISTRY:
        raise ValueError(f"未注册的搜索场景键: {key!r}(注册表: {sorted(SEARCH_SCENARIO_REGISTRY)})")
    token = _ACTIVE_SCENARIO.set(key)
    try:
        yield
    finally:
        _ACTIVE_SCENARIO.reset(token)


def active_scenario() -> str | None:
    return _ACTIVE_SCENARIO.get()


def resolve_provider(scenario: str | None = None) -> str:
    """场景 → provider。任何一道闸没过都回 metaso(部署即零变化)。

    闸序:场景在注册表 → 场景在本单灰度批 → env ``SEARCH_PROVIDER_<KEY>=doubao``
    → 豆包 key 已配置(未配置整体回退秘塔 · 工单 §6 前置依赖锁)。
    """
    key = scenario if scenario is not None else _ACTIVE_SCENARIO.get()
    if not key or key not in SEARCH_SCENARIO_REGISTRY:
        return "metaso"
    if key not in GRAYSCALE_FIRST_BATCH:
        return "metaso"  # 预留键本单不开,env 设了也不路由
    if os.environ.get(f"SEARCH_PROVIDER_{key.upper()}", "").strip().lower() != "doubao":
        return "metaso"
    from tools.search.doubao_search import is_doubao_search_configured

    if not is_doubao_search_configured():
        return "metaso"  # 未配置 DOUBAO_SEARCH_API_KEY → 豆包路由整体回退秘塔
    return "doubao"


# ---------------------------------------------------------------------------
# 引用形检索(Evidence 场景的 provider 无关入口)
# ---------------------------------------------------------------------------
async def citation_search(
    query: str,
    *,
    size: int = 15,
    scope: str = "webpage",
    scenario: str | None = None,
) -> dict:
    """与 ``metaso_search_with_citations`` 同返回结构的 provider 路由入口。

    - 默认配置 / 非 webpage scope → 原样委托 metaso_search_with_citations
      (该函数本体是 B 级,保持秘塔直连,本入口不改它一个字);
    - 场景解析为 doubao → 豆包检索 → shim → 复用同一 extract_citations 出引用结构,
      并把豆包自带正文以 ``raw_content`` 附在 citation 上(秘塔路径没有这个键,
      消费方按"有就用、没有走 reader"处理,默认行为零变化);
    - 豆包异常/空结果 → 原参数原样重放秘塔(回退链)。
    """
    from tools.search import metaso_mcp

    # 与历史调用点逐字节一致:webpage scope 不显式传 scope(老代码就是只传 query+size,
    # 既有替身/monkeypatch 依赖该签名),非 webpage 才带 scope。
    if scope != "webpage":
        return await metaso_mcp.metaso_search_with_citations(query, scope=scope, size=size)
    if resolve_provider(scenario) != "doubao":
        return await metaso_mcp.metaso_search_with_citations(query, size=size)

    from tools.search.doubao_search import DoubaoSearchError, doubao_search_webpages

    try:
        rows = await doubao_search_webpages(query, size=size)
    except DoubaoSearchError:
        return await metaso_mcp.metaso_search_with_citations(query, size=size)

    citation_data = metaso_mcp.extract_citations({"webpages": rows})
    content_by_url = {
        str(row.get("link") or ""): str(row.get("content") or "")
        for row in rows
    }
    for citation in citation_data["citations"]:
        inline = content_by_url.get(str(citation.get("url") or ""), "")
        if inline:
            citation["raw_content"] = inline
    return {
        "query": query,
        "scope": scope,
        "result_count": len(citation_data["citations"]),
        "citations": citation_data["citations"],
        "authority_sources": citation_data["authority_sources"],
        "authority_count": citation_data["authority_count"],
        "source": "doubao_search",
    }


# ---------------------------------------------------------------------------
# Scholar 学术层(B 级秘塔直连 · 不进 provider 路由)
# ---------------------------------------------------------------------------
_SCHOLAR_VERIFIABLE_HOST_TOKENS: Final = ("wanfangdata", "doi.org")
_YEAR_RE: Final = re.compile(r"(19|20)\d{2}")


def scholar_entry_verifiable(entry: dict) -> bool:
    """可查证性判定(工单 §3.3):``authors+date 齐`` 或 ``link 为万方/doi.org``。"""
    authors_ok = bool(entry.get("authors")) and bool(str(entry.get("date") or "").strip())
    host = (urlparse(str(entry.get("link") or "")).hostname or "").casefold()
    link_ok = any(token in host for token in _SCHOLAR_VERIFIABLE_HOST_TOKENS)
    return authors_ok or link_ok


def scholar_entry_year(entry: dict) -> str:
    match = _YEAR_RE.search(str(entry.get("date") or ""))
    return match.group(0) if match else ""


async def scholar_evidence_search(query: str, *, size: int = 8) -> list[dict]:
    """秘塔学术检索 → 只放行可查证条目(供 Evidence 管道消费)。

    🔴 响应条目字段是 **``scholars``**(不是 webpages —— Review 实测踩过解析
    错键得全零,样例夹具 scholar_probe2.json)。条目结构
    title/authors/date/link/score/snippet。

    走 ``_metaso_web_search_direct``(B 级秘塔直连),永不进 provider 路由 ——
    豆包搜索完全没有 scholar 能力。
    """
    from tools.search.metaso_mcp import _metaso_web_search_direct

    response = await _metaso_web_search_direct(
        query, scope="scholar", include_summary=True, size=size,
    )
    entries: list[dict] = []
    for raw in _parse_mcp_inner_items(response, item_key="scholars"):
        if not isinstance(raw, dict):
            continue
        entry = {
            "title": str(raw.get("title") or "").strip(),
            "authors": [str(a).strip() for a in (raw.get("authors") or []) if str(a or "").strip()],
            "date": str(raw.get("date") or "").strip(),
            "link": str(raw.get("link") or raw.get("url") or "").strip(),
            "score": str(raw.get("score") or ""),
            "snippet": str(raw.get("snippet") or "").strip(),
        }
        # 不可查证条目不放行(判别锁:放行不可查证 → 转红)。
        if entry["title"] and entry["link"] and scholar_entry_verifiable(entry):
            entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# 标准类证据 lane(秘塔 document 文库 · B 级秘塔直连 · 不进 provider 路由)
# 工单 WORKORDER_STANDARD_EVIDENCE_LANE_2026-07-29
# ---------------------------------------------------------------------------
#: 本 lane 的来源标识。**放行判定的唯一入口约束**(工单 §2):证据条目必须带这个
#: source 才可能走"标题级免正文"通道 —— 没有它,任意 .gov.cn 网页会从 evidence /
#: research 别的 lane 混进来白拿免正文特权。判定实现在
#: ``writing.evidence_research.standard_lane_admits``。
STANDARD_LANE_SOURCE: Final = "metaso.document"


async def document_evidence_search(query: str, *, size: int = 10) -> list[dict]:
    """秘塔文库检索 → 原样条目(每条打上 ``source=metaso.document``)。

    🔴 内层条目键是 **``documents``**(不是 webpages / docs)。实测 2026-07-29 裸
    MCP 内层为 ``{credits, documents, searchParameters, total}``,夹具
    ``tests/fixtures/metaso_document_probe_2026_07_29.json`` 是那次调用的原样落盘。
    错键 → 恒返空 → lane 看着在跑实际空转(本仓 scholar 刚踩过同一坑)。

    条目字段(实测):``title / link / authorityDomain / authorityType / authors /
    date / summary / snippet / docId / score / position``。

    走 ``_metaso_web_search_direct``(B 级秘塔直连),永不进 provider 路由 ——
    豆包检索没有文库 scope。单次 3 credits。

    **本函数不做放行判定**:它只负责把真实条目取回来并标 source;权威域名闸在
    ``writing.evidence_research.standard_lane_admits``(工单 §2 两条必要条件)。
    """
    from tools.search.metaso_mcp import _metaso_web_search_direct

    response = await _metaso_web_search_direct(
        query, scope="document", include_summary=True, size=size,
    )
    entries: list[dict] = []
    for raw in _parse_mcp_inner_items(response, item_key="documents"):
        if not isinstance(raw, dict):
            continue
        entries.append({
            "title": str(raw.get("title") or "").strip(),
            "link": str(raw.get("link") or "").strip(),
            "url": str(raw.get("url") or "").strip(),
            # 实测:非权威条目这两个键直接缺失(不是空串)。
            "authorityDomain": str(raw.get("authorityDomain") or "").strip(),
            "authorityType": str(raw.get("authorityType") or "").strip(),
            "authors": [str(a).strip() for a in (raw.get("authors") or []) if str(a or "").strip()],
            "date": str(raw.get("date") or "").strip(),
            "snippet": str(raw.get("summary") or raw.get("snippet") or "").strip(),
            "docId": str(raw.get("docId") or "").strip(),
            "source": STANDARD_LANE_SOURCE,
        })
    return entries


def _parse_mcp_inner_items(response: Any, *, item_key: str) -> list:
    """解析 MCP 双层 JSON:外层 status/results → results[0].text → 内层 item_key 列表。"""
    try:
        blocks = getattr(response, "content", None) or []
        block = blocks[0] if blocks else {}
        raw = block.get("text") if isinstance(block, dict) else ""
        outer = json.loads(raw) if isinstance(raw, str) else {}
        results = outer.get("results") or []
        if not (results and isinstance(results[0], dict)):
            return []
        inner_text = results[0].get("text", "{}")
        inner = json.loads(inner_text) if isinstance(inner_text, str) else inner_text
        items = (inner or {}).get(item_key)
        return list(items) if isinstance(items, list) else []
    except Exception:
        return []


__all__ = [
    "SCENARIO_EVIDENCE",
    "SCENARIO_RESEARCH",
    "SCENARIO_DEEP_RESEARCH",
    "SEARCH_SCENARIO_REGISTRY",
    "GRAYSCALE_FIRST_BATCH",
    "search_scenario",
    "active_scenario",
    "resolve_provider",
    "citation_search",
    "scholar_evidence_search",
    "scholar_entry_verifiable",
    "scholar_entry_year",
    "STANDARD_LANE_SOURCE",
    "document_evidence_search",
]
