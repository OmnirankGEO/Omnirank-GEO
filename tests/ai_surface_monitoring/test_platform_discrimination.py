"""平台判别(§10.2 / §3):Kimi 退出默认、订单履约、Hy3 标注、deepseek 不冒名、search 三态。"""

from __future__ import annotations

import pytest

from services.ai_surface_monitoring.adapters.base import BaseSurfaceAdapter, EngineResult
from services.ai_surface_monitoring.adapters.wrapped import WrappedResearchAdapter
from services.ai_surface_monitoring.adapters.openai_compat import OpenAICompatibleAdapter
from services.ai_surface_monitoring.contracts import CollectionRequest
from services.ai_surface_monitoring.lineage import get_surface_spec
from services.ai_surface_monitoring.policy import FakeObservationPolicy
from services.ai_surface_monitoring.registry import (
    AdapterRegistry,
    OrderPlatformEntitlement,
    _build_default_adapters,
)
from services.ai_surface_monitoring.service import CollectionService, SurfaceNotAllowedError
from .conftest import ingest_policy, make_engine_fetch, make_http_post


def _build_registry_with_captures():
    """每个 wrapped 表面各自 capture,便于统计"谁被调用了"。"""
    captures = {sk: {} for sk in
                ("doubao_ark_api_search", "qwen_dashscope_search",
                 "deepseek_dashscope_search_legacy", "other_explicit")}
    adapters = _build_default_adapters()
    for sk, cap in captures.items():
        adapters[sk] = WrappedResearchAdapter(sk, fetch=make_engine_fetch(capture=cap))
    # yuanbao / deepseek native 也换成 fake http
    adapters["yuanbao_hy3_tokenhub"] = OpenAICompatibleAdapter(
        "yuanbao_hy3_tokenhub",
        http_post=make_http_post({"id": "x", "model": "hy3",
                                  "choices": [{"message": {"content": "答"}}], "usage": {}}),
        api_key_getter=lambda n: "K")
    adapters["deepseek_native_no_search"] = OpenAICompatibleAdapter(
        "deepseek_native_no_search",
        http_post=make_http_post({"id": "x", "model": "deepseek-v4-flash",
                                  "choices": [{"message": {"content": "答"}}], "usage": {}}),
        api_key_getter=lambda n: "K", verify_model_echo=False)
    reg = AdapterRegistry(ingest_policy(), adapters=adapters)
    return reg, captures


def _req(surface: str, rid: str = "r") -> CollectionRequest:
    return CollectionRequest(request_id=rid, source_kind="research", source_ref="s",
                             question_text="深圳装修公司推荐", query_kind="non_branded", surface_key=surface)


async def test_new_sales_default_round_kimi_zero():
    reg, captures = _build_registry_with_captures()
    svc = CollectionService(reg)
    # 新售默认全量轮:只跑默认表面(不含 Kimi)
    default_surfaces = reg.default_sampling_surfaces()
    assert "other_explicit" not in default_surfaces  # Kimi 表面不在默认
    assert not any(get_surface_spec(sk).platform_key == "kimi" for sk in default_surfaces)
    reqs = [_req(sk, f"r{i}") for i, sk in enumerate(default_surfaces)]
    envs = [e async for e in svc.collect_batch(reqs, round_id="rt")]
    assert len(envs) == len(default_surfaces)
    # Kimi adapter 从未被调用(capture 空)
    assert captures["other_explicit"] == {}


async def test_order_with_kimi_entitlement_honored():
    reg, captures = _build_registry_with_captures()
    svc = CollectionService(reg)
    ent = OrderPlatformEntitlement({"other_explicit"})  # surface 级权益(历史 Kimi 表面)
    # 订单含 Kimi:sampling_surfaces_for_order 应含 other_explicit
    surfaces = reg.sampling_surfaces_for_order(ent)
    assert "other_explicit" in surfaces
    env = await svc.collect_observation(_req("other_explicit", "rk"), order_entitlement=ent, round_id="rt")
    assert env.surface_key == "other_explicit"
    assert captures["other_explicit"].get("prompt") == "深圳装修公司推荐"  # 确实调用了 Kimi


async def test_kimi_blocked_without_entitlement():
    reg, captures = _build_registry_with_captures()
    svc = CollectionService(reg)
    with pytest.raises(SurfaceNotAllowedError):
        await svc.collect_observation(_req("other_explicit", "rk2"), round_id="rt")
    assert captures["other_explicit"] == {}  # 未授权时零调用


async def test_kimi_history_readable_lineage_preserved():
    """Kimi 历史仍可读:adapter 注册在册,身份由 provider/model/product_label 携带。"""
    reg, _ = _build_registry_with_captures()
    adapter = reg.get_adapter("other_explicit")
    lin = adapter.describe_lineage()
    assert lin.platform_key == "kimi" and lin.product_label == "Kimi"
    spec = get_surface_spec("other_explicit")
    assert spec.legacy_read_only is True and spec.default_enabled is False


async def test_hy3_surface_and_product_label():
    reg, _ = _build_registry_with_captures()
    svc = CollectionService(reg)
    env = await svc.collect_observation(_req("yuanbao_hy3_tokenhub", "ry"), round_id="rt")
    assert env.surface_key == "yuanbao_hy3_tokenhub"
    assert env.platform_key == "yuanbao"
    assert env.provider_key == "tencent_tokenhub"
    assert env.model_key == "hy3"
    # [2026-07-27] 元宝已转联网(TokenHub 控制台开通)。[2026-08-03] 模型由 preview 改回 hy3。
    # search_enabled 现在反映**真实是否检索过**:桩响应没有 usage.tool_usage → False。
    # 这条断言的意义因此从"该表面不支持搜索"变成"没搜到就不许谎称搜了"。
    assert env.search_enabled is False
    lin = reg.get_adapter("yuanbao_hy3_tokenhub").describe_lineage()
    assert lin.product_label == "元宝"  # 用户端显示"元宝"


def test_wsa_is_separate_surface_not_overriding_hy3():
    wsa = get_surface_spec("tencent_wsa_search")
    hy3 = get_surface_spec("yuanbao_hy3_tokenhub")
    assert wsa.surface_key != hy3.surface_key
    assert wsa.provider_key == "tencent_wsa" and hy3.provider_key == "tencent_tokenhub"
    assert wsa.availability == "unavailable"  # 本批不默认启用;单独启用才 active
    assert hy3.availability == "active"


def test_deepseek_native_vs_dashscope_legacy_not_conflated():
    native = get_surface_spec("deepseek_native_no_search")
    legacy = get_surface_spec("deepseek_dashscope_search_legacy")
    with_search = get_surface_spec("deepseek_native_with_search")
    # 同平台不同 surface / provider / 搜索状态
    assert native.platform_key == legacy.platform_key == "deepseek"
    assert native.provider_key == "deepseek" and legacy.provider_key == "dashscope"  # 官方 vs 百炼
    assert native.default_search_enabled is False and legacy.default_search_enabled is True
    # [2026-07-27 实测转正] 官方 /anthropic 端点接受 web_search_20250305 并真发起检索,
    # 原判"无可验证原生搜索"已被真实响应否定 → 转 active(证据见
    # docs/AI-CONTEXT/ENGINE_SEARCH_FIDELITY_EVIDENCE_2026-07-27.md)。
    assert with_search.availability == "active"
    assert with_search.provider_key == "deepseek_official"  # 与 legacy 的 dashscope 严格区分


def test_doubao_surface_is_honest_ark_api():
    spec = get_surface_spec("doubao_ark_api_search")
    assert spec.surface_key == "doubao_ark_api_search"  # 不是 doubao_app_api 冒名
    assert spec.provider_key == "volcengine"


async def test_search_queries_three_states():
    """search_queries 的 null / empty / real 三态可区分(base 映射逻辑)。"""
    class _StubAdapter(BaseSurfaceAdapter):
        def __init__(self, result):
            super().__init__("doubao_ark_api_search")
            self._result = result

        async def _fetch(self, request, prompt_text):
            return self._result

    req = _req("doubao_ark_api_search", "rq")
    # null:无 search_queries
    env_null = await _StubAdapter(EngineResult(answer="a", search_queries=None)).collect(req)
    assert env_null.search_queries is None
    # empty:空列表 → None(不返回空壳)
    env_empty = await _StubAdapter(EngineResult(answer="a", search_queries=[])).collect(req)
    assert env_empty.search_queries is None
    # real:provider 返回真实子查询
    env_real = await _StubAdapter(EngineResult(
        answer="a", search_queries=[{"text": "深圳 装修 口碑", "rank": 1}])).collect(req)
    assert env_real.search_queries is not None and len(env_real.search_queries) == 1
    assert env_real.search_queries[0].text == "深圳 装修 口碑"
    assert env_real.search_queries[0].provider_returned is True


async def test_search_enabled_reflects_surface():
    reg, _ = _build_registry_with_captures()
    svc = CollectionService(reg)
    env_hy3 = await svc.collect_observation(_req("yuanbao_hy3_tokenhub", "s1"), round_id="rt")
    env_doubao = await svc.collect_observation(_req("doubao_ark_api_search", "s2"), round_id="rt")
    assert env_hy3.search_enabled is False   # hy3 无搜索
    assert env_doubao.search_enabled is True  # 豆包 ai_search 联网
