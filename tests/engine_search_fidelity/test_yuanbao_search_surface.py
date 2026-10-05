"""任务 C 判别锁:元宝必须真的联网,而且"有没有搜"必须是真话。

开通前的实测(2026-07-27):`hy3` 传了 `web_search_options` 也**静默不搜** ——
HTTP 200、不报错、没有 `search_results`、模型自述无法联网。开通后 `hy3` 仍然如此,
只有 **`hy3-preview`** 真返 `search_results` + `usage.tool_usage.web_search_call`。
**不换模型 = 白开通**,这是本次最容易漏的一步,所以在这里锁死。

变异验证(Review-CTO 复检会做):
  · 去掉 `web_search_options` → `test_request_declares_web_search_options` 转红
  · 模型改回 `hy3`            → `test_surface_model_is_preview` + 血缘一致性 转红
  · `search_source` 改回 lite → `test_search_source_is_standard` 转红
"""
from __future__ import annotations

import asyncio

import pytest


# 逐字段照抄 2026-07-27 开通后 hy3-preview + search_source=standard 的真实响应形状。
REAL_SHAPE = {
    "id": "yb-real-1",
    "model": "hy3-preview",
    "object": "chat.completion",
    "choices": [{
        "finish_reason": "stop",
        "message": {
            "role": "assistant",
            "content": "根据2026年多个装修平台与媒体的测评...[1][2]",
            "search_results": [
                {"index": 1, "url": "https://news.pchouse.com.cn/420/4205763.html",
                 "name": "杭州装修公司推荐2026 值得选的 10 家实力派装企", "site": "太平洋家居网",
                 "snippet": "摘要正文不进 citation"},
                {"index": 2, "url": "https://new.qq.com/rain/a/20260723A0BK5400",
                 "name": "2026年杭州高性价比装修怎么选", "site": "腾讯网", "snippet": "..."},
                # 实测同一轮里会出现重复 URL
                {"index": 3, "url": "https://news.pchouse.com.cn/420/4205763.html",
                 "name": "重复条目", "site": "太平洋家居网", "snippet": "..."},
            ],
        },
    }],
    "usage": {
        "prompt_tokens": 17149, "completion_tokens": 682, "total_tokens": 17831,
        "cache_read_tokens": 6784,
        "prompt_tokens_details": {"cached_tokens": 6784},
        "tool_usage": {"web_search_call": 3},
    },
}


def _no_search_shape():
    """开通前 / 额度耗尽时的形状:200、无 search_results、无 tool_usage。"""
    return {
        "id": "yb-nosearch", "model": "hy3-preview",
        "choices": [{"finish_reason": "stop",
                     "message": {"role": "assistant", "content": "我无法实时联网。"}}],
        "usage": {"prompt_tokens": 32, "completion_tokens": 100, "total_tokens": 132},
    }


def _adapter_with(payload, captured):
    from services.ai_surface_monitoring.adapters.openai_compat import OpenAICompatibleAdapter

    async def _http_post(url, headers, json_body, timeout):
        captured.update({"url": url, "headers": headers, "body": json_body})
        return 200, payload

    return OpenAICompatibleAdapter(
        "yuanbao_hy3_tokenhub", http_post=_http_post, api_key_getter=lambda n: "fake-key"
    )


def _collect(payload):
    from services.ai_surface_monitoring.contracts import CollectionRequest

    captured: dict = {}
    adapter = _adapter_with(payload, captured)
    env = asyncio.run(adapter.collect(CollectionRequest(
        request_id="r1", source_kind="research", source_ref="s",
        question_text="2026年杭州装修公司哪家好?", surface_key="yuanbao_hy3_tokenhub",
    )))
    return env, captured


# ---------------------------------------------------------------------------
# 请求侧
# ---------------------------------------------------------------------------
def test_request_declares_web_search_options():
    """🔒 变异锁:去掉 web_search_options → 转红。TokenHub 不加这个字段就完全不搜。"""
    _env, captured = _collect(REAL_SHAPE)
    options = captured["body"].get("web_search_options")
    assert options, "没声明 web_search_options = TokenHub 根本不会去检索"
    assert options.get("enable") is True


def test_search_source_is_standard():
    """🔒 变异锁:改回 lite → 转红。Owner 2026-07-27 指定用标准版。"""
    _env, captured = _collect(REAL_SHAPE)
    assert captured["body"]["web_search_options"]["search_source"] == "standard"


def test_surface_model_is_preview():
    """🔒 关键锁:模型改回 `hy3` → 转红。

    实测:`hy3` 传了 web_search_options 也静默不搜(开通前后都一样),
    官方支持矩阵也只列 hy3-preview。**不换模型 = 白开通。**
    """
    _env, captured = _collect(REAL_SHAPE)
    assert captured["body"]["model"] == "hy3-preview"


def test_request_hits_tokenhub_same_endpoint_and_key():
    """联网是 TokenHub 自带能力:同端点同 key,不接第二个供应商。"""
    _env, captured = _collect(REAL_SHAPE)
    assert "tokenhub.tencentmaas.com" in captured["url"]
    assert captured["url"].endswith("/chat/completions")


# ---------------------------------------------------------------------------
# 响应侧
# ---------------------------------------------------------------------------
def test_search_results_become_citations_deduped():
    env, _ = _collect(REAL_SHAPE)
    urls = [c.url for c in env.citations]
    assert urls == [
        "https://news.pchouse.com.cn/420/4205763.html",
        "https://new.qq.com/rain/a/20260723A0BK5400",
    ], "同一轮里的重复 URL 必须去重"
    assert env.citations[0].title.startswith("杭州装修公司推荐2026")


def test_snippet_not_leaked_into_citation():
    """snippet 是正文摘要,不进引用证据。"""
    env, _ = _collect(REAL_SHAPE)
    for c in env.citations:
        assert "摘要正文不进" not in (c.title or "")


def test_inline_marker_distinguishes_cited_from_merely_retrieved():
    """正文写了 [1][2] → 这两条是"采纳引用";没角标的只是"检索到但没用上"。

    契约里 citation 与 source 的区别靠这个,不能一律标成采纳。
    """
    env, _ = _collect(REAL_SHAPE)
    by_url = {c.url: c for c in env.citations}
    assert by_url["https://news.pchouse.com.cn/420/4205763.html"].source_type == "citation"
    assert by_url["https://new.qq.com/rain/a/20260723A0BK5400"].source_type == "citation"

    quiet = {**REAL_SHAPE}
    quiet["choices"] = [{"finish_reason": "stop", "message": {
        **REAL_SHAPE["choices"][0]["message"], "content": "正文里没有任何角标。"}}]
    env2, _ = _collect(quiet)
    assert {c.source_type for c in env2.citations} == {"source"}


def test_search_enabled_reflects_actual_calls():
    env, _ = _collect(REAL_SHAPE)
    assert env.search_enabled is True


def test_no_search_must_not_claim_enabled():
    """🔒 开通前/额度耗尽时:200 但没搜 —— 绝不能因为"我们请求里开了开关"就标已联网。

    这正是开通前的真实形状:静默失效。标成"已联网 + 引用 0"比标"不适用"更糟。
    """
    env, _ = _collect(_no_search_shape())
    assert env.search_enabled is False
    assert env.citations == []


def test_yuanbao_removed_from_no_search_engines():
    """报告层:元宝的引用数不再是"不适用"。"""
    from config.ai_engines import NO_SEARCH_ENGINES

    assert "yuanbao" not in NO_SEARCH_ENGINES


# ---------------------------------------------------------------------------
# 四处血缘一致(工单点了 3 处,实际有第 4 处 monitoring_db)
# ---------------------------------------------------------------------------
def test_four_lineage_sites_agree_on_yuanbao():
    import inspect

    from db import monitoring_db
    from services.ai_surface_monitoring.lineage import SURFACE_SPECS
    from tools.monitoring.batch_monitor import _resolve_runtime_lineage

    # ① 观测表面定义
    spec = SURFACE_SPECS["yuanbao_hy3_tokenhub"]
    assert spec.default_model_key == "hy3-preview"
    assert spec.default_search_enabled is True
    assert spec.search_provider == "tencent_tokenhub"

    # ② 监测血缘表
    provider, model, surface, search_mode = _resolve_runtime_lineage("yuanbao", "standard")
    assert (provider, model, surface, search_mode) == (
        "tencent_tokenhub", "hy3-preview", "ai_search", "tencent_tokenhub")

    # ③ 执行层 = adapter 用 spec.default_model_key(上面 test_surface_model_is_preview 已锁真实请求体)

    # ④ 成本估算映射(工单没点到的第 4 处 —— 漏改会按旧模型算钱)
    src = inspect.getsource(monitoring_db)
    assert '"yuanbao": ("tencent_tokenhub", "hy3-preview")' in src, \
        "monitoring_db 的成本映射仍停在 hy3(第 4 处血缘漏改)"


def test_no_silent_fallback_to_other_provider():
    """🔴 红线:表面替换表必须为空 —— 官方通道挂了绝不静默回落别家供应商。"""
    from services.ai_surface_monitoring.lineage import SURFACE_SUBSTITUTIONS

    assert SURFACE_SUBSTITUTIONS == {}


# ---------------------------------------------------------------------------
# QPS 护栏(官方限制 5 QPS)
# ---------------------------------------------------------------------------
def test_tokenhub_rpm_guard_is_below_official_qps():
    """🔒 官方限 5 QPS = 300 RPM。默认必须留余量,且**不能是"不限"**。

    QPS 限的是每秒发起数,对它生效的是 RpmLimiter(平滑节流),不是并发 Semaphore ——
    改前 RpmLimiter 默认不限,而监测侧并发 10 / 观测层每平台 8 都会直接压过 5 QPS。
    """
    from services.ai_surface_monitoring.cost_policy import default_provider_rpm

    rpm = default_provider_rpm().get("tencent_tokenhub")
    assert rpm is not None, "TokenHub 没有 RPM 上限 = 一定撞 5 QPS 限流"
    assert 0 < rpm <= 300, f"RPM {rpm} 超过官方 5 QPS(=300 RPM)"


def test_service_injects_provider_rpm_by_default():
    """默认构造的 service 必须带上 RPM 限,不能靠调用方记得传。"""
    from services.ai_surface_monitoring.cost_policy import RpmLimiter, default_provider_rpm

    limiter = RpmLimiter(rpm_by_domain=default_provider_rpm())
    assert limiter._rpm_for("tencent_tokenhub") is not None
    assert limiter._rpm_for("dashscope") is None, "未登记的供应商保持不限(既有行为零变化)"
