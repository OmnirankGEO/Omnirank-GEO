"""
AI 可见度检测工具 v2.2 - 多引擎联网搜索版

新售默认引擎配置（Owner 2026-07-26 裁决：诊断与监测统一五引擎）：
- DashScope (被测千问 = services.monitoring_lineage.QWEN_ENGINE):
  原生 multimodal-generation 端点 + enable_search/search_options 联网开关
- DeepSeek: 使用现役联网表面
- Doubao: 使用联网内容插件
- Kimi: $web_search + ---REFERENCES--- 解析
- Yuanbao: 复用统一观测表面 yuanbao_hy3_tokenhub（无联网检索表面）

引擎清单**唯一常量源** = ``config/ai_engines.py``（P0-2）。本文件不得再硬编码
引擎列表；改常量即诊断与监测同步变。秘塔保留为显式调用兼容，不进新售默认矩阵。
"""

import asyncio
import contextvars
import httpx
import json
import os
import re
import hashlib
import uuid
from typing import Literal
from agentscope.tool import ToolResponse
from config.deepseek_models import (
    DEEPSEEK_OFFICIAL_FLASH,
    OfficialModelEchoMismatch,
    assert_official_echo,
)

import sys
import io

# 修复Windows GBK终端无法输出emoji的问题
# [WO_228-c1 2026-09-16] 同 server.py:**不再接管 buffer**。
# `TextIOWrapper` 会接管底下的 buffer,回收时连带 close() 它;
# pytest 下那是捕获用的临时文件,关掉之后每条判据 teardown 都炸。
# 这一处比 server.py 安全(有 encoding 守卫,pytest 捕获下通常已是 utf-8 所以不触发),
# 但**形状一样** —— 工单点名的实例不是缺陷类本身,同形的一起改掉。
for _s_name, _s_errors in (("stdout", "replace"), ("stderr", "replace")):
    _s = getattr(sys, _s_name, None)
    _enc = (getattr(_s, "encoding", "") or "").lower()
    if _s is not None and _enc and _enc not in ("utf-8", "utf8"):
        _rc = getattr(_s, "reconfigure", None)
        if callable(_rc):
            _rc(encoding="utf-8", errors=_s_errors)

sys.path.append("../..")
from config.model_config import DEEPSEEK_CONFIG, KIMI_CONFIG, DOUBAO_CONFIG

# [包F ⑥ · 2026-08-24] 被测千问引擎的唯一 SSOT(模型+端点+联网参数)。
# 在这里 import 而不是手写字符串:手写是本仓「按一个模型收钱、用另一个
# 模型干活」那个存量分叉的成因(见该常量的 docstring)。
from services.engine_contract import QWEN_ENGINE as _QWEN
from config.ai_engines import default_diagnosis_engines


def _count_kimi_web_search_calls(payload: dict) -> int:
    """Count charged Kimi $web_search tool calls in an OpenAI-compatible response."""
    if not isinstance(payload, dict):
        return 0
    count = 0
    for choice in payload.get("choices") or []:
        if choice.get("finish_reason") != "tool_calls":
            continue
        message = choice.get("message") or {}
        for tool_call in message.get("tool_calls") or []:
            function = tool_call.get("function") or {}
            if function.get("name") == "$web_search":
                count += 1
    return count


def _count_deepseek_web_search_requests(payload: dict) -> int:
    """官方 DeepSeek 服务端检索次数(Anthropic 兼容响应)。

    实测路径:``usage.server_tool_use.web_search_requests``(2026-07-27 真实响应)。
    """
    if not isinstance(payload, dict):
        return 0
    usage = payload.get("usage") or {}
    server_tool_use = usage.get("server_tool_use") or {}
    try:
        return int(server_tool_use.get("web_search_requests") or 0)
    except (TypeError, ValueError):
        return 0


def _extract_deepseek_official_citations(payload: dict) -> list[dict]:
    """从 ``web_search_tool_result`` 块取真实引用来源。

    🔴 实测订正(2026-07-27):工单推测"正文 text 块带 citations[]
    (web_search_result_location)可取 cited_text" —— **实测不成立**。
    DeepSeek 的 Anthropic 兼容实现只给 ``web_search_tool_result``,text 块里
    ``citations`` 字段根本不存在(两次带搜索的真实响应都是 ``citations: NONE``)。
    所以引用只能从工具结果块取,这是唯一有据的路径,不臆造行内锚点。

    每条结果字段实测为 ``type/title/url/page_age/encrypted_content``;
    ``encrypted_content`` 是 DeepSeek 的不透明大 blob(单条数 KB),不落库、不外传。
    """
    citations: list[dict] = []
    seen: set[str] = set()
    if not isinstance(payload, dict):
        return citations
    for block in payload.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "web_search_tool_result":
            continue
        for item in block.get("content") or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            citations.append({
                "url": url,
                "title": str(item.get("title") or "").strip(),
                "site_name": "",
                "index": len(citations) + 1,
            })
    return citations


def _deepseek_official_text(payload: dict) -> str:
    """拼正文 —— 只取 ``text`` 块,``thinking`` 块是思维链不算回答。"""
    parts: list[str] = []
    for block in payload.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text = str(block.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts).strip()


async def _tracked_post(
    client: httpx.AsyncClient,
    url: str,
    *,
    platform: str,
    model: str,
    metadata: dict | None = None,
    **kwargs,
) -> httpx.Response:
    from tools.llm_call_tracker import llm_track, usage_from_response_payload

    async with llm_track(
        "ai_visibility",
        platform,
        model=model,
        metadata=metadata,
    ) as tracker:
        response = await client.post(url, **kwargs)
        if response.status_code == 200:
            try:
                payload = response.json()
                input_tokens, output_tokens, cached_tokens = usage_from_response_payload(payload)
                if platform == "kimi":
                    web_search_call_count = _count_kimi_web_search_calls(payload)
                elif platform == "deepseek_official":
                    # 官方 DeepSeek 的服务端检索次数在 usage.server_tool_use.web_search_requests
                    # (实测确认有值)。成本口径必须落库,不许估 —— 带搜索的一次调用
                    # input_tokens 实测 6.5 万+(检索结果全量进上下文),不落库就完全看不出来。
                    web_search_call_count = _count_deepseek_web_search_requests(payload)
                else:
                    web_search_call_count = 0
            except Exception:
                input_tokens = output_tokens = cached_tokens = 0
                web_search_call_count = 0
            extra_metadata = {}
            if web_search_call_count:
                # [复检返修 ③· 2026-07-27] `web_search_call_count` 是 **Kimi 专用计费键**
                #   (estimate_cost 对它无条件乘 KIMI_WEB_SEARCH_CALL_CNY ¥0.036/次)。
                #   官方 DeepSeek 的检索费已含在 input_tokens 里,复用这个键 = 重复计费 1.95×。
                #   故换独立键:只留痕、不参与计费。
                if platform == "deepseek_official":
                    extra_metadata["deepseek_web_search_call_count"] = web_search_call_count
                else:
                    extra_metadata["web_search_call_count"] = web_search_call_count
            tracker.record(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                success=True,
                **extra_metadata,
            )
        else:
            tracker.record(success=False, error_msg=f"HTTP {response.status_code}: {response.text[:200]}")
        return response


def _echoed_model(payload) -> str:
    """供应商在**响应里自己报**的模型名。取不到就返回空串。

    [工单 V3-C · C-3 · Codex 三审 P1-5] 这就是 P1-9b 当时"诚实列明未做"的
    那一半:让 adapter 把回显一路带到 ``services.monitoring_lineage``。

    🔴 为什么必须是"响应里的",不能是我们发请求时写的那个:
       ``defgeo_monitoring_attempts.actual_model`` 同时是保真度对账与计价的
       取数口。用请求侧的值去填它,等于让"实际模型"这个词证明不了它自己 ——
       供应商灰度/别名路由、或我们改了常量而请求侧发了旧值时,两者不等。

    🔴 取不到**不猜**:返回空串,下游据此写 ``planned_fallback``。
       元宝走 envelope 抽象、DashScope 原生多模态端点都不回显模型名,
       所以这两条路径本来就长期是 ``planned_fallback`` —— 那是事实,
       不是缺陷,也不许拿计划值去把它填成"已证实"。
    """
    if not isinstance(payload, dict):
        return ""
    # OpenAI 兼容响应(Kimi / 豆包 / DeepSeek 官方)顶层就带 model
    direct = payload.get("model")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    # DashScope 原生协议把元信息放在 output/usage 同级,少数版本回显在这里
    for key in ("output", "response"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            got = nested.get("model")
            if isinstance(got, str) and got.strip():
                return got.strip()
    return ""


# ===============================
# DashScope (阿里云) - 联网搜索
# ===============================
async def query_dashscope_search(
    query: str, check_brand: str = "", max_tokens: int = 2000,
    brand_id: int | None = None,
    brand_display_names: list[str] | None = None,
) -> ToolResponse:
    """
    查询被测千问引擎并检测品牌可见度（联网搜索版）

    模型/端点/联网参数逐值取 ``services.monitoring_lineage.QWEN_ENGINE``
    （包F ⑥ · 2026-08-24 Owner 终裁换代为 qwen3.7-plus + 原生多模态端点）。
    """
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    max_retries = 3

    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                # 🔑 使用 DashScope 原生协议（而非 OpenAI 兼容），以获取搜索来源
                response = await _tracked_post(
                    client,
                    _QWEN["endpoint"],
                    platform="dashscope",
                    model=_QWEN["model"],
                    metadata={"engine": "dashscope_search", "attempt": attempt + 1},
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        # ══════════════════════════════════════════════════
                        # [包F ⑥ · 2026-08-24 Owner 终裁] 千问被测引擎换代
                        # ══════════════════════════════════════════════════
                        # 模型 / 端点 / search_options **逐值**取
                        # services.monitoring_lineage.QWEN_ENGINE ——
                        # 那是被测千问引擎的唯一 SSOT。
                        #
                        # 🔴 为什么不在这里手写字符串:本仓历史上就是因为手写
                        #    才分叉的 —— 2026-08-23 census 实测,
                        #    db/monitoring_db.py 按 qwen3.7-plus **计价**,
                        #    而这一行实际发的是 qwen3-max。我们按一个模型收钱、
                        #    用另一个模型干活,且没有任何判据会红。
                        #    收成一个常量之后,这种分叉在结构上不可能发生。
                        #
                        # 🔴 2026-06-06 那条 hotfix 注释(「3.7-plus 必 400、
                        #    联网仅 Responses API 支持、零角标」)**已作废**:
                        #    它打的是 text-generation 端点。2026-08-24 生产原地
                        #    复探证实,原生 **multimodal-generation** 端点 200 且
                        #    全套 search_options 活(1377 字 / 6 来源 / 30 角标)。
                        #
                        # 🔴 [R2 F-2 · 2026-08-24] `enable_thinking: False`
                        #    **必须传**,由 `_QWEN["base_parameters"]` 铺开。
                        #    R1 这里曾写「这两个键不在探测参数集里,传没探过的
                        #    键=拿生产去试」——**那句前提是错的**:08-24 打通的
                        #    那次探测参数集里明确含 `"enable_thinking": false`。
                        #    Review 生产两臂(同题同参,仅差这一个键):
                        #      省略 → output 2876 tok(reasoning 2031)/ 1343 字 / 17 角标
                        #      false → output  981 tok(零 reasoning)/ 1701 字 / 26 角标
                        #    省略 = thinking 默认开 ⇒ 贵 2.9 倍、答案还更短。
                        #    `result_format` 确实不传(多模态端点无此参数)。
                        #
                        # 🔴 `parameters` 里除 `max_tokens`(调用方给的)之外
                        #    **一个手写键都不许有** —— 全部铺自 SSOT。
                        #    判据 `test_request_parameters_all_come_from_the_ssot`
                        #    钉的是这个形态,所以下一次有人顺手删/加参数会当场红。
                        #
                        # 🔴 `content` 是**数组**形状:多模态端点的入参约定。
                        #
                        # 🔴 同一个「千问」,本仓仍有**两条管线两个模型**
                        #    (census ③#1/#2 · 2026-08-23,包F ⑥ 之后依然如此):
                        #    · 本行(诊断/监测主链)= 被测对象 = QWEN_ENGINE
                        #      的 qwen3.7-plus(原生多模态端点);
                        #    · 调研监测那条链默认 **qwen-plus-latest**
                        #      (services/research_monitor/platforms.py 的
                        #       _load_qwen_model 默认参数 + config/settings_manager.py
                        #       的 research_monitor_tasks.platforms.qwen,包F ⑦b 改的
                        #       就是后者;另有 services/ai_surface_monitoring/lineage.py
                        #       的 qwen_dashscope_search.default_model_key)。
                        #    两边对客户都叫「千问」,答案却可能来自不同模型 ——
                        #    同一品牌在诊断报告与调研面板上看到不一致结论时,
                        #    真因常常在这里。
                        # 🔴 本次**仍然刻意不统一**:这一行是**被观测对象**,
                        #    那一行是**我们自己干活的工具**。统一会让"换干活工具
                        #    降本"顺手动到检索保真基准 —— 两者的判据完全不同
                        #    (被测对象只为保真换,干活工具按成本/质量换)。
                        "model": _QWEN["model"],
                        "input": {"messages": [
                            {"role": "user", "content": [{"text": query}]},
                        ]},
                        "parameters": {
                            **dict(_QWEN["base_parameters"]),
                            "max_tokens": max_tokens,
                            "search_options": dict(_QWEN["search_options"]),
                        },
                    },
                )

                if response.status_code != 200:
                    if attempt < max_retries - 1:
                        print(f"[DashScope] ⚠️ HTTP {response.status_code}，{3}秒后重试 ({attempt + 1}/{max_retries})")
                        await asyncio.sleep(3)
                        continue
                    return ToolResponse(
                        content=[
                            {
                                "type": "text",
                                "text": f"Error: HTTP {response.status_code} - {response.text[:200]}",
                            }
                        ]
                    )

                # 防御空响应
                try:
                    data = response.json()
                except (json.JSONDecodeError, ValueError):
                    if attempt < max_retries - 1:
                        print(f"[DashScope] ⚠️ 响应JSON解析失败，{3}秒后重试 ({attempt + 1}/{max_retries})")
                        await asyncio.sleep(3)
                        continue
                    return ToolResponse(content=[{"type": "text", "text": json.dumps({
                        "answer_summary": f"DashScope API返回空响应（已重试{max_retries}次）",
                        "engine_error": True,  # [#3-C2 P0-2 2026-06-07 资金] API 失败标记·下游 PlatformAdapter 据此标 status=error → fail-closed 退费/不污染
                        "mentioned_brands": [], "brand_detected": False,
                        "web_search_enabled": False, "full_response": ""
                    }, ensure_ascii=False)}])

                # DashScope 原生协议的响应格式：data.output.choices / data.output.search_info
                output = data.get("output", {})
                # 🔴 [包F ⑥] 多模态端点的 ``message.content`` 是**数组**
                #    (``[{"text": ...}, ...]``),文本端点是字符串。
                #    原来这里是 ``.get("content", "")`` 直接当字符串用 ——
                #    换端点后它会拿到一个 list,``.strip()`` 当场 AttributeError,
                #    被外层 except 吞成"异常重试三次"然后整格 engine_error。
                #    也就是说**不改这一行,换代的表现是监测全线静默失败**。
                ai_response = _dashscope_message_text(output)

                # 空响应重试
                if not ai_response.strip() and attempt < max_retries - 1:
                    print(f"[DashScope] ⚠️ 空响应，{3}秒后重试 ({attempt + 1}/{max_retries})")
                    await asyncio.sleep(3)
                    continue

                # [#3-C2 P0-2 2026-06-07 资金] 末次仍空内容 → 引擎失败(API 成功但无内容·非合法未检出:真未检出有内容不提品牌)→ engine_error fail-closed
                if not ai_response.strip():
                    return ToolResponse(content=[{"type": "text", "text": json.dumps({
                        "answer_summary": "DashScope 空内容(已重试耗尽)",
                        "engine_error": True,
                        "mentioned_brands": [], "brand_detected": False,
                        "web_search_enabled": False, "full_response": ""
                    }, ensure_ascii=False)}])

                # 🔑 DDS: 从 search_info 提取搜索来源
                search_citations = _extract_dashscope_native_citations(output)

                visibility_result = await _call_analyze_visibility(
                    ai_response, query, check_brand, "通义千问", brand_id, brand_display_names
                )
                visibility_result["web_search_enabled"] = True
                visibility_result["search_citations"] = search_citations

                # [work order V3-C C-3] provider-echoed model name.
                visibility_result["echoed_model"] = _echoed_model(data)
                return ToolResponse(
                    content=[
                        {
                            "type": "text",
                            "text": json.dumps(visibility_result, ensure_ascii=False),
                        }
                    ]
                )
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"[DashScope] ⚠️ 异常: {e}，{3}秒后重试 ({attempt + 1}/{max_retries})")
                await asyncio.sleep(3)
                continue
            return ToolResponse(content=[{"type": "text", "text": f"Error: {str(e)}"}])


# ===============================
# 元宝 — 复用统一观测表面 yuanbao_hy3_tokenhub
# ===============================
async def query_yuanbao(
    query: str,
    check_brand: str = "",
    max_tokens: int = 2000,
    brand_id: int | None = None,
    brand_display_names: list[str] | None = None,
    *,
    observation_source_ref: str | None = None,
    observation_round_id: str | None = None,
    observation_request_id: str | None = None,
    owner_user_id: int | None = None,
    industry: str = "",
    observation_source_kind: str = "paid_diagnosis",
) -> ToolResponse:
    """通过统一 AI 观测 SSOT 调用元宝 Hy3 表面并复用品牌身份判定。

    这里不复制 TokenHub HTTP 协议，也不把历史 Kimi 回答改名成元宝。策略、预算、
    模型回显与供应商错误均由 ``CollectionService`` fail-closed 处理。

    [P0-2 · 2026-07-26] 统一五引擎后监测侧也要跑元宝，但观测 SSOT 明确
    "研究和监测不得调用 collect_paid_delivery"（``service.py`` PaidDeliveryContextError）。
    因此按 ``observation_source_kind`` 分流：
      - ``paid_diagnosis`` → ``collect_paid_delivery``（已发起的付费诊断不被
        ingest 账本开关误停）
      - 其余（``monitoring``）→ ``collect_observation``（受 ingest_enabled /
        readiness 闸约束；关闸时该格 engine_error，走既有 per_platform_fail /
        release_freeze 退费通道，**不静默跳过、不拖累其余四引擎**）
    """
    del max_tokens  # 采集上限由统一表面配置控制，调用方不得另开一套参数。
    source_kind = (observation_source_kind or "").strip() or "paid_diagnosis"
    source_ref = (observation_source_ref or "").strip() or f"{source_kind}-adhoc:{uuid.uuid4().hex}"
    round_id = (observation_round_id or "").strip() or f"{source_kind}:{source_ref}"
    request_id = (observation_request_id or "").strip()
    if not request_id:
        request_id = f"{source_kind}-yuanbao:" + hashlib.sha256(
            f"{source_ref}|{query}".encode("utf-8")
        ).hexdigest()[:32]

    try:
        from services.ai_surface_monitoring.contracts import CollectionRequest
        from services.geo_observation.integration import collection_runtime

        request = CollectionRequest(
            request_id=request_id,
            source_kind=source_kind,
            source_ref=source_ref,
            question_text=query,
            surface_key="yuanbao_hy3_tokenhub",
            owner_user_id=owner_user_id,
            brand_id=brand_id,
            industry_key=industry or None,
            session_mode="accounted",
            target_brand=check_brand or None,
            brand_display_names=brand_display_names or None,
            metadata={"engine": "yuanbao", "workflow": source_kind},
        )
        # 已发起的客户诊断属于产品履约，不应被“是否登记进 vNext 观测账本”的
        # ingest_enabled 开关误停；专用入口仍完整执行 policy readiness、预算和模型回显门。
        # 监测（source_kind='monitoring'）必须走常规观测入口，付费诊断专用豁免不外借。
        _service = collection_runtime().service
        if source_kind == "paid_diagnosis":
            envelope = await _service.collect_paid_delivery(request, round_id=round_id)
        else:
            envelope = await _service.collect_observation(request, round_id=round_id)
        if envelope.response_status != "answered" or not envelope.answer_text.strip():
            payload = {
                "response": "",
                "answer_summary": "元宝本次未返回有效回答，请稍后重试",
                "brand_detected": False,
                "brand_verdict": "UNKNOWN",
                "engine_error": True,
                "mentioned_brands_inline": [],
                "web_search_enabled": bool(envelope.search_enabled),
                "search_citations": [],
                "platform_key": envelope.platform_key,
                "surface_key": envelope.surface_key,
                "tested_at": envelope.observed_at.isoformat(),
            }
            return ToolResponse(content=[{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}])

        visibility = await _call_analyze_visibility(
            envelope.answer_text,
            query,
            check_brand,
            "元宝",
            brand_id,
            brand_display_names,
        )
        visibility["web_search_enabled"] = bool(envelope.search_enabled)
        visibility["search_citations"] = [
            {
                "url": citation.url,
                "title": citation.title,
                "rank": citation.rank,
                "source_type": citation.source_type,
            }
            for citation in envelope.citations
        ]
        visibility["platform_key"] = envelope.platform_key
        visibility["surface_key"] = envelope.surface_key
        visibility["tested_at"] = envelope.observed_at.isoformat()
        return ToolResponse(content=[{"type": "text", "text": json.dumps(visibility, ensure_ascii=False)}])
    except Exception as exc:
        # 对外不回显策略、密钥、URL 或供应商响应；类型只进入本地诊断日志。
        print(f"[元宝] 统一观测调用失败: {type(exc).__name__}")
        payload = {
            "response": "",
            "answer_summary": "元宝服务暂时不可用，请稍后重试",
            "brand_detected": False,
            "brand_verdict": "UNKNOWN",
            "engine_error": True,
            "mentioned_brands_inline": [],
            "web_search_enabled": False,
            "search_citations": [],
            "platform_key": "yuanbao",
        }
        return ToolResponse(content=[{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}])


# ===============================
# Kimi (月之暗面) - 联网搜索
# ===============================
# [Phase 2B 2026-06-07] Kimi $web_search 不返回结构化引用 API(服务端执行 · tool arguments
# 只有 {search_id, usage}),诊断侧 search_citations 此前恒为 []。移植 GEO 调研系统
# (services/research_monitor/platforms.py · 已实证可吐 25 条引用)的做法:system prompt
# 强制模型在正文后附 ---REFERENCES--- 列表 → 正则解析。解析后【正文剥离 references】再跑
# 品牌检出,使 brand_detected/检出率只看正文、不被来源 URL/标题污染。
_KIMI_REFERENCES_SYSTEM_PROMPT = """你是 Kimi。回答用户问题时必须联网搜索。

【极其重要的输出格式要求】
请在你的回答正文之后,必须附上一个完整的"引用来源"列表,格式如下:

---REFERENCES---
1. [文章标题1](完整URL1)
2. [文章标题2](完整URL2)
...

要求:
1. 必须包含你搜索时实际看到的所有网页URL(至少5条,能列多少列多少)
2. URL必须是完整的http或https开头的真实链接
3. 标题写实际的网页标题
4. 用 ---REFERENCES--- 分隔符开头,方便程序识别
5. 严禁编造URL,只能写你搜索时真实出现的网页"""

# 行级匹配 markdown 编号引用(允许 title 含括号/中文/空格)· 同调研系统 KIMI_REF_REGEX
_KIMI_REF_REGEX = re.compile(
    r'^\s*\d+\.\s*\[(.+?)\]\((https?://\S+?)\)\s*$',
    re.MULTILINE,
)


def _parse_kimi_references(content: str):
    """从 Kimi 回答切出 ---REFERENCES--- 列表 → (剥离来源后的正文, [{url,title,rank}])。
    无分隔符 / 空 → (原文, [])(降级 · 不抛 · 与原 search_citations=[] 行为兼容)。"""
    if not content or "---REFERENCES---" not in content:
        return (content or ""), []
    parts = content.split("---REFERENCES---", 1)
    answer = parts[0].strip()
    citations = []
    seen = set()
    for m in _KIMI_REF_REGEX.finditer(parts[1]):
        title = m.group(1).strip()
        url = m.group(2).strip()
        if url in seen:
            continue
        seen.add(url)
        citations.append({"url": url, "title": title, "rank": len(citations) + 1})
    return answer, citations


async def query_kimi_search(
    query: str, check_brand: str = "", max_tokens: int = 2000,
    brand_id: int | None = None,
    brand_display_names: list[str] | None = None,
) -> ToolResponse:
    """
    查询 Kimi AI 并检测品牌可见度（联网搜索版）

    使用 $web_search builtin_function 开启联网
    按照官方文档实现完整的 tool_calls 流程
    """
    api_key = KIMI_CONFIG["api_key"]
    base_url = KIMI_CONFIG["base_url"]
    max_retries = 3

    for attempt in range(max_retries):
        # [Phase 2B] system prompt 强制吐 ---REFERENCES--- 列表(Kimi 无官方 citation API)
        messages = [
            {"role": "system", "content": _KIMI_REFERENCES_SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ]
        tools = [{"type": "builtin_function", "function": {"name": "$web_search"}}]

        try:
            async with httpx.AsyncClient(timeout=180) as client:
                finish_reason = None
                # [CTO-15.23 2026-05-09 Kimi 月烧治理] max_iter 5 → 3 降浪费
                # 实测大部分关键词 2-3 轮 tool_calls 就完成 · 降到 3 防累加 messages 浪费
                # TODO Codex 接续:启用 Moonshot Context Caching · system_prompt 是高频复用 · 加 cache_control 后 input cost -80%
                # 参考 Moonshot 官方文档 https://platform.moonshot.cn/docs/guide/context-caching · 验证字段名后再加
                max_iterations = 3
                iteration = 0
                kimi_search_queries = []  # 🔑 DDS: 记录 Kimi 的搜索查询

                while finish_reason != "stop" and iteration < max_iterations:
                    iteration += 1

                    response = await _tracked_post(
                        client,
                        f"{base_url}/chat/completions",
                        platform="kimi",
                        model="kimi-k2.6",
                        metadata={"engine": "kimi_search", "iteration": iteration},
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            # 🔑 官方文档要求:$web_search 使用 kimi-k2.6(2026-05-09 升级)
                            # https://platform.moonshot.cn/docs/guide/use-web-search
                            "model": "kimi-k2.6",
                            "messages": messages,
                            "max_tokens": max_tokens,
                            "tools": tools,
                            # [2026-05-16 实测] Kimi K2.6 硬约束(thinking=disabled 时 temperature 锁 0.6):
                            # thinking=disabled → temperature ONLY 0.6(其他值 400 "invalid temperature: only 0.6")
                            # thinking 默认 enabled → temperature ONLY 1.0
                            # tool_calls 流程必须 disabled → 锁 temperature=0.6
                            "temperature": 0.6,
                            "thinking": {"type": "disabled"},
                        },
                    )

                    if response.status_code != 200:
                        if attempt < max_retries - 1:
                            print(f"[Kimi] ⚠️ HTTP {response.status_code}，{3}秒后重试 ({attempt + 1}/{max_retries})")
                            await asyncio.sleep(3)
                            break  # break inner loop, retry outer
                        return ToolResponse(
                            content=[
                                {
                                    "type": "text",
                                    "text": f"Error: HTTP {response.status_code} - {response.text[:200]}",
                                }
                            ]
                        )

                    # 防御空响应
                    try:
                        data = response.json()
                    except (json.JSONDecodeError, ValueError):
                        if attempt < max_retries - 1:
                            print(f"[Kimi] ⚠️ 响应JSON解析失败，{3}秒后重试 ({attempt + 1}/{max_retries})")
                            await asyncio.sleep(3)
                            break  # break inner loop, retry outer
                        return ToolResponse(content=[{"type": "text", "text": json.dumps({
                            "answer_summary": f"Kimi API返回空响应（已重试{max_retries}次）",
                            "engine_error": True,  # [#3-C2 P0-2 2026-06-07 资金] API 失败标记·下游标 status=error → fail-closed
                            "mentioned_brands": [], "brand_detected": False,
                            "web_search_enabled": False, "full_response": ""
                        }, ensure_ascii=False)}])

                    choice = data.get("choices", [{}])[0]
                    finish_reason = choice.get("finish_reason")
                    message = choice.get("message", {})

                    if finish_reason == "tool_calls":
                        # 🔑 官方要求：将 assistant message 原封不动添加到 messages
                        messages.append(message)

                        # 处理每个tool_call
                        tool_calls = message.get("tool_calls", [])
                        for tool_call in tool_calls:
                            tool_call_id = tool_call.get("id")
                            tool_call_name = tool_call.get("function", {}).get("name")
                            tool_call_arguments = tool_call.get("function", {}).get(
                                "arguments", "{}"
                            )

                            # 🔑 DDS: 记录 Kimi 的搜索查询参数
                            if tool_call_name == "$web_search":
                                try:
                                    search_args = json.loads(tool_call_arguments)
                                    kimi_search_queries.append(search_args)
                                except:
                                    pass

                            # 🔑 $web_search 的 arguments 必须原封不动返回
                            # 官方文档：search_impl 只需 return arguments
                            try:
                                parsed_args = json.loads(tool_call_arguments)
                                tool_content = json.dumps(parsed_args)
                            except:
                                tool_content = tool_call_arguments

                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_call_id,
                                    "name": tool_call_name,
                                    "content": tool_content,
                                }
                            )

                    elif finish_reason == "stop":
                        # 获取最终回复
                        raw_content = message.get("content", "")
                        # [Phase 2B] 切出 ---REFERENCES--- 引用 + 剥离来源后的正文
                        ai_response, kimi_citations = _parse_kimi_references(raw_content)
                        # 品牌检出只跑【剥离来源后的正文】· 防 references 里的 URL/标题污染检出口径
                        visibility_result = await _call_analyze_visibility(
                            ai_response, query, check_brand, "Kimi", brand_id, brand_display_names
                        )
                        # [B-2 · 2026-07-27] 改前这里**硬编码 True** —— 而生产实证近 30 天
                        # 6935 次调用只有 553 次真的带 $web_search(8%)。也就是说 92% 的格子
                        # 被标成"已联网",其实是模型原生知识。如实按真实调用次数记。
                        kimi_search_calls = len(kimi_search_queries)
                        visibility_result["web_search_enabled"] = bool(kimi_search_calls > 0)
                        visibility_result["web_search_call_count"] = kimi_search_calls

                        # [Phase 2B 2026-06-07] Kimi 经 ---REFERENCES--- prompt 拿到来源(此前恒为 [])
                        # → 进诊断 detail_table.results.kimi.search_citations → 权威背书
                        visibility_result["search_citations"] = kimi_citations

                        # [B-2] 🔴 触发了搜索却一条来源都解析不到 → **必须留痕**。
                        #   改前是静默吞:模型不按 ---REFERENCES--- 格式输出,引用就悄悄变 0,
                        #   没有任何告警,所以这个洞存在很久没人发现。
                        #   注:Kimi 无官方结构化 citation 字段(官方文档只回显 arguments,
                        #   不返回来源列表 —— 2026-07-27 已核官方文档 + 实测),所以正则仍是
                        #   唯一路径;但"解析失败"这件事从此不再无声。
                        if kimi_search_calls > 0 and not kimi_citations:
                            visibility_result["citation_parse_degraded"] = True
                            print(
                                f"[Kimi] ⚠️ 搜索已触发 {kimi_search_calls} 次但未解析到任何来源"
                                f" · 模型未按 ---REFERENCES--- 格式输出 · query={query[:40]}"
                            )

                        # [work order V3-C C-3] provider-echoed model name.
                        visibility_result["echoed_model"] = _echoed_model(data)
                        return ToolResponse(
                            content=[
                                {
                                    "type": "text",
                                    "text": json.dumps(
                                        visibility_result, ensure_ascii=False
                                    ),
                                }
                            ]
                        )
                else:
                    # while loop finished normally (max_iterations reached), no need to retry outer
                    return ToolResponse(
                        content=[
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {
                                        "error": "Max iterations reached",
                                        "engine_error": True,  # [#3-C2 P0-2 2026-06-07 资金] 3 轮 tool_calls 不收敛·无内容产出=引擎失败·下游标 status=error fail-closed(此前裸 error 键漏标→被当合法未检出落库扣费)
                                        "brand_detected": False,
                                    },
                                    ensure_ascii=False,
                                ),
                            }
                        ]
                    )
                # If we broke out of inner loop, continue to next retry attempt
                continue

        except Exception as e:
            if attempt < max_retries - 1:
                print(f"[Kimi] ⚠️ 异常: {e}，{3}秒后重试 ({attempt + 1}/{max_retries})")
                await asyncio.sleep(3)
                continue
            return ToolResponse(content=[{"type": "text", "text": f"Error: {str(e)}"}])


# ===============================
# 豆包 (字节跳动) - 联网搜索
# ===============================
def _doubao_is_refusal_or_empty(text: str) -> bool:
    """检测豆包安全过滤拒答 / 完全空回复(2026-05-09 · CTO-15.23)

    实证案例(广东法制盛邦律所 8 query):
    - Q1/Q3 ai_response 完全空(0 字 · 模型不知道答案 / 内部错误吞掉)
    - Q2 "这个问题,以后再聊"(12 字 · 安全过滤拒答)
    """
    if not text:
        return True
    s = text.strip()
    if not s:
        return True
    # 短回复(<60 字)+ 含拒答关键词
    if len(s) < 60:
        refusal_keywords = [
            "以后再聊", "换个话题", "无法回答", "不便回答", "暂不能",
            "暂时无法", "我不能", "抱歉，我", "无法提供", "请问您",
            "建议您", "我还在学习", "暂未掌握",
        ]
        if any(kw in s for kw in refusal_keywords):
            return True
        # 极短回复一律视为不可用
        if len(s) < 30:
            return True
    return False


async def query_doubao_search(
    query: str, check_brand: str = "", max_tokens: int = 2000,
    search_mode: str = "enhanced", brand_id: int | None = None,
    brand_display_names: list[str] | None = None,
) -> ToolResponse:
    """豆包查询(2026-05-09 · 加 enhanced→standard fallback + 拒答检测)

    实证(广东法制盛邦律所 8 query):enhanced 模式 3 个空回复 + 1 个 12 字拒答 → 25% rate
    修法:enhanced 失败/拒答 → fallback standard mode(web_search) · 都不行 mark API failure
    """
    # 仅 enhanced 入口走 fallback wrapper · standard 内部不递归
    if search_mode == "enhanced":
        primary = await _query_doubao_search_impl(
            query, check_brand, max_tokens, "enhanced", brand_id, brand_display_names
        )
        # 解析 primary 看是否失败 / 拒答 / 空
        try:
            ptext = primary.content[0]["text"]
            pdata = json.loads(ptext) if ptext.startswith("{") else {}
            pans = pdata.get("answer_summary") or pdata.get("response") or ""
            pfull = pdata.get("full_response", "") or pdata.get("response") or pans
            primary_failed = (
                "查询失败" in pans or pans.startswith("Error") or
                "豆包API" in pans or _doubao_is_refusal_or_empty(pfull)
            )
            if primary_failed:
                print(f"[Doubao] enhanced 失败/拒答 · 自动 fallback standard mode")
                fallback = await _query_doubao_search_impl(
                    query, check_brand, max_tokens, "standard", brand_id, brand_display_names
                )
                # 检查 standard 是否也失败
                ftext = fallback.content[0]["text"]
                fdata = json.loads(ftext) if ftext.startswith("{") else {}
                fans = fdata.get("answer_summary") or fdata.get("response") or ""
                ffull = fdata.get("full_response", "") or fdata.get("response") or fans
                fallback_failed = (
                    "查询失败" in fans or fans.startswith("Error") or
                    "豆包API" in fans or _doubao_is_refusal_or_empty(ffull)
                )
                if fallback_failed:
                    # enhanced + standard 都失败 · mark 成 API failure 让上层不计入 mention_rate
                    fdata["answer_summary"] = "查询失败:豆包 enhanced+standard 均不可用(空响应/拒答)"
                    fdata["brand_detected"] = False
                    fdata["doubao_inconclusive"] = True
                    fdata["engine_error"] = True  # [#3-C2 P0-2 2026-06-07 资金] 双失败=引擎失败·下游 PlatformAdapter 标 status=error → fail-closed(此前只设死字段 doubao_inconclusive·零处读取→被当合法未检出落库扣费·豆包主力 enhanced 路径漏标)
                    print(f"[Doubao] ⚠️ enhanced+standard 均失败 · query={query[:40]!r}")
                    return ToolResponse(content=[{"type": "text", "text": json.dumps(fdata, ensure_ascii=False)}])
                return fallback
        except Exception as wrap_err:
            print(f"[Doubao] wrapper 异常: {wrap_err} · 返回 primary 原值")
        return primary
    # 直接调内部实现
    return await _query_doubao_search_impl(
        query, check_brand, max_tokens, search_mode, brand_id, brand_display_names
    )


async def _query_doubao_search_impl(
    query: str, check_brand: str = "", max_tokens: int = 2000,
    search_mode: str = "enhanced", brand_id: int | None = None,
    brand_display_names: list[str] | None = None,
) -> ToolResponse:
    """
    查询 豆包 AI 并检测品牌可见度（联网搜索版）

    search_mode:
      - "enhanced" (默认): doubao_app 豆包助手（¥0.2/次，模型 doubao-seed-1-6，搜索质量高）
      - "standard" (备用): web_search 基础联网搜索（¥0.004/次，搜索质量差，仅限容错场景）
    """
    api_key = DOUBAO_CONFIG.get("seed_api_key") or DOUBAO_CONFIG["api_key"]
    base_url = DOUBAO_CONFIG["base_url"]
    # enhanced 用 doubao_app 专用模型，standard 用 Seed 2.0
    model_name = "doubao-seed-1-6-251015" if search_mode == "enhanced" else "doubao-seed-2-0-pro-260215"

    if not api_key:
        return ToolResponse(
            content=[
                {
                    "type": "text",
                    "text": "Error: DOUBAO_API_KEY or DOUBAO_SEED_API_KEY not configured",
                }
            ]
        )

    # 空响应重试：豆包 Responses API 偶发返回空文本，重试 2 次
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # ── [WO_224-c1 §3.2] 豆包出网并发闸 ───────────────────────────
            # 🔴 429 的根因是**并发打爆账号级 QPS**,不是配额:
            #    Deploy 224-d1 实测同秒并发 6–13 个全部被拒,
            #    同一个 task 的四引擎发起时刻差**全部 = 0.0 秒**(完全并行扇出)。
            # 🔴 闸必须在**这里**,不在监测的 PlatformAdapter:
            #    诊断线(本文件另外 5 处)与情感分类打的是**同一个账号**,
            #    只拦监测的话账号照样被打爆,而监测那边的读数会显示「闸生效了」。
            #    本函数是豆包唯一出网点(query_doubao_search 的三条路径全过它)。
            # 🔴 槽在**每次尝试**里取:重试也是一次真实请求,也要占并发。
            #    上限是后台可调系数,见 `services/doubao_concurrency`。
            from services.doubao_concurrency import doubao_slot
            async with doubao_slot(), httpx.AsyncClient(timeout=180) as client:
                # 根据 search_mode 构建不同的请求
                use_enhanced = (search_mode == "enhanced")

                if use_enhanced:
                    # 增强版：doubao_app 豆包助手（¥0.2/次，深度搜索）
                    headers = {
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "ark-beta-doubao-app": "true",
                    }
                    tools = [{
                        "type": "doubao_app",
                        "feature": {
                            "ai_search": {"type": "enabled"}
                        },
                    }]
                else:
                    # 普通版：web_search 基础联网搜索（¥0.004/次）
                    headers = {
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    }
                    tools = [{
                        "type": "web_search",
                        "max_keyword": 1,
                        "limit": 10,
                        "user_location": {
                            "type": "approximate",
                            "country": "中国",
                            "region": "广东",
                            "city": "深圳",
                        },
                    }]

                response = await _tracked_post(
                    client,
                    f"{base_url}/responses",
                    platform="doubao",
                    model=model_name,
                    metadata={
                        "engine": "doubao_responses",
                        "search_mode": search_mode,
                        "doubao_ai_search_call_count": 1 if use_enhanced else 0,
                        "doubao_web_search_call_count": 0 if use_enhanced else 1,
                    },
                    headers=headers,
                    json={
                        "model": model_name,
                        "input": [
                            {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": query}],
                            }
                        ],
                        "tools": tools,
                    },
                )

                if response.status_code != 200:
                    if attempt < max_retries - 1:
                        # [#3-C1 2026-06-07] 429 限流专属退避:指数 + 抖动 + 读 Retry-After
                        #   高并发整片 429 时线性 2/4/6s 三次易撞同一限流窗口;指数+jitter 把并发请求错开,显著降低 429 复发
                        #   注:这是降发生率,不是根治;429 耗尽仍走下方原返回(根治=C2 改 error+退费+不落有效结果,归资金批)
                        if response.status_code == 429:
                            import random
                            retry_after = 0.0
                            try:
                                ra = response.headers.get("Retry-After")
                                if ra:
                                    retry_after = float(ra)
                            except (TypeError, ValueError):
                                retry_after = 0.0
                            backoff = min(max(retry_after, 4 * (2 ** attempt)) + random.uniform(0, 1.5), 30.0)
                            print(f"[Doubao] ⚠️ HTTP 429 限流，{backoff:.1f}秒后重试 (attempt {attempt + 1}/{max_retries})")
                            await asyncio.sleep(backoff)
                        else:
                            print(f"[Doubao] ⚠️ HTTP {response.status_code}，{2 * (attempt + 1)}秒后重试 (attempt {attempt + 1}/{max_retries})")
                            await asyncio.sleep(2 * (attempt + 1))
                        continue
                    return ToolResponse(content=[{"type": "text", "text": json.dumps({
                        "answer_summary": f"豆包API返回HTTP {response.status_code}，已重试{max_retries}次",
                        "engine_error": True,  # [#3-C2 2026-06-07 资金] API 失败标记·下游 PlatformAdapter 据此标 status=error·不当未检出落库污染达标 + 走既有 release_freeze 退费
                        "mentioned_brands": [], "brand_detected": False,
                        "web_search_enabled": False, "full_response": "",
                        "response": ""
                    }, ensure_ascii=False)}])

                # 豆包偶发返回HTTP 200但空body，需特殊处理
                try:
                    data = response.json()
                except (json.JSONDecodeError, ValueError):
                    if attempt < max_retries - 1:
                        print(f"[Doubao] ⚠️ 响应JSON解析失败(body为空或非JSON)，{2 * (attempt + 1)}秒后重试 (attempt {attempt + 1}/{max_retries})")
                        await asyncio.sleep(2 * (attempt + 1))
                        continue
                    return ToolResponse(content=[{"type": "text", "text": json.dumps({
                        "answer_summary": f"豆包API返回空响应（HTTP {response.status_code}），已重试{max_retries}次",
                        "engine_error": True,  # [#3-C2 2026-06-07 资金] API 失败标记·下游标 status=error·不污染达标 + 走 release_freeze 退费
                        "mentioned_brands": [], "brand_detected": False,
                        "web_search_enabled": False, "full_response": "",
                        "response": ""
                    }, ensure_ascii=False)}])

                # Responses API + web_search 返回格式解析
                # output 包含 web_search_call 项（搜索元数据）和 message 项（文本+引用注释）
                ai_response = ""
                web_search_annotations = []  # web_search 引用注释
                web_search_queries = []  # 搜索关键词
                doubao_citation_blocks = []  # doubao_app 非文本 blocks（含搜索引用）
                if "output" in data:
                    output = data.get("output", [])
                    for item in output:
                        item_type = item.get("type")

                        # web_search_call: 搜索调用元数据
                        if item_type == "web_search_call":
                            action = item.get("action", {})
                            q = action.get("query", "")
                            if q:
                                web_search_queries.append(q)

                        # message: 文本结果 + annotations 引用
                        elif item_type == "message":
                            content = item.get("content", [])
                            for c in content:
                                if c.get("type") == "output_text":
                                    ai_response += c.get("text", "")
                                    # 提取 annotations（web_search 引用）
                                    annotations = c.get("annotations", [])
                                    if annotations:
                                        web_search_annotations.extend(annotations)

                        # 兼容 doubao_app_call 旧格式（过渡期）
                        elif item_type == "doubao_app_call":
                            blocks = item.get("blocks", [])
                            for block in blocks:
                                if block.get("type") == "output_text":
                                    ai_response += block.get("text", "")
                                else:
                                    # 保存非文本 blocks（含 search 引用等）
                                    doubao_citation_blocks.append(block)
                else:
                    # 兜底：尝试旧格式
                    ai_response = (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )

                # 空响应/拒答重试检查(2026-05-09 · CTO-15.23 · 加拒答检测)
                if (not ai_response.strip() or _doubao_is_refusal_or_empty(ai_response)) and attempt < max_retries - 1:
                    reason = "空响应" if not ai_response.strip() else f"拒答短回复({len(ai_response)}字: {ai_response[:30]!r})"
                    print(
                        f"[Doubao] ⚠️ {reason}，{2 * (attempt + 1)}秒后重试 (attempt {attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(2 * (attempt + 1))
                    continue  # 重试

                # [#3-C2 P0-2 2026-06-07 资金] 末次仍空/拒答 → 引擎失败(无内容/拒答)→ engine_error → 下游标 status=error fail-closed。
                #   只命中"空/拒答"(无内容);真未检出是"有内容但不提品牌"·ai_response 有内容不命中 → 走下方正常分析·不误伤。
                #   统一 enhanced wrapper 与 standard 直调(run_client_monitoring 默认 standard)口径。
                if not ai_response.strip() or _doubao_is_refusal_or_empty(ai_response):
                    return ToolResponse(content=[{"type": "text", "text": json.dumps({
                        "answer_summary": "豆包空响应/拒答(已重试耗尽)",
                        "engine_error": True,
                        "mentioned_brands": [], "brand_detected": False,
                        "web_search_enabled": False, "full_response": "", "response": ""
                    }, ensure_ascii=False)}])

                mode_label = "增强版" if use_enhanced else "普通版"
                print(f"[Doubao-{mode_label}] ai_response length: {len(ai_response)}")

                # 提取搜索引用
                if use_enhanced:
                    # 增强版：从 doubao_app_call blocks 提取引用
                    search_citations = _extract_doubao_citations(data, doubao_citation_blocks, ai_response)
                else:
                    # 普通版：从 web_search annotations 提取引用
                    search_citations = _extract_web_search_citations(
                        web_search_annotations, web_search_queries, ai_response
                    )

                visibility_result = await _call_analyze_visibility(
                    ai_response, query, check_brand, "Doubao", brand_id, brand_display_names
                )
                visibility_result["web_search_enabled"] = True
                visibility_result["search_citations"] = search_citations
                visibility_result["search_mode"] = search_mode

                # [work order V3-C C-3] provider-echoed model name.
                visibility_result["echoed_model"] = _echoed_model(data)
                return ToolResponse(
                    content=[
                        {
                            "type": "text",
                            "text": json.dumps(visibility_result, ensure_ascii=False),
                        }
                    ]
                )
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"[Doubao] ⚠️ 异常，{2 * (attempt + 1)}秒后重试: {e}")
                await asyncio.sleep(2 * (attempt + 1))
                continue
            return ToolResponse(content=[{"type": "text", "text": json.dumps({
                "answer_summary": f"豆包API异常: {str(e)[:80]}",
                "engine_error": True,  # [#3-C2 2026-06-07 资金] API 异常标记·下游标 status=error·不污染达标 + 走 release_freeze 退费
                "mentioned_brands": [], "brand_detected": False,
                "web_search_enabled": False, "full_response": "",
                "response": ""
            }, ensure_ascii=False)}])


# ===============================
# DashScope (阿里云) - DeepSeek-V4-Flash 联网搜索 (2026-05-09 升级 v3.2 → v4-flash)
# ===============================
async def query_dashscope_deepseek(
    query: str, check_brand: str = "", max_tokens: int = 2000,
    brand_id: int | None = None,
    brand_display_names: list[str] | None = None,
) -> ToolResponse:
    """
    查询 DashScope DeepSeek-V4-Flash 并检测品牌可见度（联网搜索版）

    使用 enable_search=True 开启 DashScope 内置联网搜索功能
    替代 Metaso/DeepSeek 官方 API · 2026-05-09 老板拍板从 v3.2 升级 v4-flash
    """
    api_key = os.getenv("DASHSCOPE_API_KEY", "")
    max_retries = 3

    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                # 🔑 使用 DashScope 原生协议（而非 OpenAI 兼容），以获取搜索来源
                response = await _tracked_post(
                    client,
                    "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation",
                    platform="dashscope",
                    model="deepseek-v4-flash",
                    metadata={"engine": "dashscope_deepseek", "attempt": attempt + 1},
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "deepseek-v4-flash",
                        "input": {"messages": [{"role": "user", "content": query}]},
                        "parameters": {
                            "max_tokens": max_tokens,
                            # [hotfix 2026-06-06] deepseek-v4-flash 是混合推理模型·DashScope 原生端点须显式关思维链,
                            # 否则 400 InvalidParameter「url error」(对齐 qwen 调用的 enable_thinking:False + 协作日志 v4-flash thinking=OFF 约定)。
                            # ⚠️ Deploy-CTO-NEW 真 key 测:若仍 400,降级 search_options(去 forced_search/search_strategy,保 enable_search+enable_source)。
                            "enable_thinking": False,
                            "enable_search": True,
                            "search_options": {
                                "enable_source": True,  # 🔑 返回搜索来源
                                "enable_citation": True,
                                "search_strategy": "turbo",
                                "forced_search": True,
                            },
                            "result_format": "message",
                        },
                    },
                )

                if response.status_code != 200:
                    if attempt < max_retries - 1:
                        print(f"[DeepSeek-DS] ⚠️ HTTP {response.status_code}，{3}秒后重试 ({attempt + 1}/{max_retries})")
                        await asyncio.sleep(3)
                        continue
                    return ToolResponse(
                        content=[
                            {
                                "type": "text",
                                "text": f"Error: HTTP {response.status_code} - {response.text[:200]}",
                            }
                        ]
                    )

                # 防御空响应
                try:
                    data = response.json()
                except (json.JSONDecodeError, ValueError):
                    if attempt < max_retries - 1:
                        print(f"[DeepSeek-DS] ⚠️ 响应JSON解析失败，{3}秒后重试 ({attempt + 1}/{max_retries})")
                        await asyncio.sleep(3)
                        continue
                    return ToolResponse(content=[{"type": "text", "text": json.dumps({
                        "answer_summary": f"DeepSeek API返回空响应（已重试{max_retries}次）",
                        "engine_error": True,  # [#3-C2 P0-2 2026-06-07 资金] API 失败标记·下游标 status=error → fail-closed
                        "mentioned_brands": [], "brand_detected": False,
                        "web_search_enabled": False, "full_response": ""
                    }, ensure_ascii=False)}])

                output = data.get("output", {})

                if not output.get("choices"):
                    if attempt < max_retries - 1:
                        print(f"[DeepSeek-DS] ⚠️ 响应格式异常，{3}秒后重试 ({attempt + 1}/{max_retries})")
                        await asyncio.sleep(3)
                        continue
                    return ToolResponse(
                        content=[
                            {
                                "type": "text",
                                "text": f"Error: Invalid response format - {json.dumps(data)[:500]}",
                            }
                        ]
                    )

                # [包F ⑥] 与千问那条链同一个折平函数(同一谓词不写两处)。
                # DeepSeek **仍走文本端点** ⇒ 这里拿到的是 str,行为逐字节不变;
                # 哪天它也换端点,不需要再改一次。
                ai_response = _dashscope_message_text(output)

                # 空响应重试
                if not ai_response.strip() and attempt < max_retries - 1:
                    print(f"[DeepSeek-DS] ⚠️ 空响应，{3}秒后重试 ({attempt + 1}/{max_retries})")
                    await asyncio.sleep(3)
                    continue

                # [#3-C2 P0-2 2026-06-07 资金] 末次仍空内容 → 引擎失败(API 成功但无内容·非合法未检出)→ engine_error fail-closed
                if not ai_response.strip():
                    return ToolResponse(content=[{"type": "text", "text": json.dumps({
                        "answer_summary": "DeepSeek 空内容(已重试耗尽)",
                        "engine_error": True,
                        "mentioned_brands": [], "brand_detected": False,
                        "web_search_enabled": False, "full_response": ""
                    }, ensure_ascii=False)}])

                # [Phase 2A 2026-06-07] DeepSeek via DashScope 与 qwen3-max 同一阿里搜索引擎,
                # 来源大量重叠但并非 100% 一致;改为如实采集本引擎 search_info 来源,使权威背书
                # 能体现「deepseek 引擎也引用过该来源」+ 拿到 qwen 未命中的增量来源。
                # 聚合层(source_authority_analyzer)按 domain 合并同源,重叠来源不会重复计分,
                # 故 endorsement_score 不会因此虚高;search_info 缺 → [](与 qwen 同口径)。
                search_citations = _extract_dashscope_native_citations(output)

                visibility_result = await _call_analyze_visibility(
                    ai_response, query, check_brand, "DeepSeek", brand_id, brand_display_names
                )
                visibility_result["web_search_enabled"] = True
                visibility_result["search_citations"] = search_citations

                # [work order V3-C C-3] provider-echoed model name.
                visibility_result["echoed_model"] = _echoed_model(data)
                return ToolResponse(
                    content=[
                        {
                            "type": "text",
                            "text": json.dumps(visibility_result, ensure_ascii=False),
                        }
                    ]
                )
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"[DeepSeek-DS] ⚠️ 异常: {e}，{3}秒后重试 ({attempt + 1}/{max_retries})")
                await asyncio.sleep(3)
                continue
            return ToolResponse(content=[{"type": "text", "text": f"Error: {str(e)}"}])


# ===============================
# DeepSeek 官方 (api.deepseek.com) - 原生服务端联网检索
# ===============================
DEEPSEEK_OFFICIAL_BASE_URL = "https://api.deepseek.com/anthropic"
DEEPSEEK_OFFICIAL_MESSAGES_URL = f"{DEEPSEEK_OFFICIAL_BASE_URL}/v1/messages"
#: 🔴 [WO_221-c1] 官方页 `deepseek-v4-flash` 已退役。**两条端点行为不同**(2026-09-15 各打一发实测):
#:     /v1/chat/completions   请求旧名 -> 200,回显 `deepseek-flash`(厂商归一)
#:     /anthropic/v1/messages 请求旧名 -> 200,回显 `deepseek-v4-flash`(**原样**)
#:   ⇒ 在 Anthropic 那条端点上回显锁是**同义反复**,抓不到「旧名被 flash 承接」;
#:     那条线的防线是**发出前先归一**。
#:   这条走的是 Anthropic 兼容端点(上面的 /anthropic/v1/messages),回显字段同样是 `model`。
DEEPSEEK_OFFICIAL_MODEL = DEEPSEEK_OFFICIAL_FLASH
DEEPSEEK_ANTHROPIC_VERSION = "2023-06-01"
# Anthropic 规范的服务端检索工具。实测(2026-07-27)官方端点接受这个 type 字符串,
# 并真的发起了 server_tool_use。**不要**改成网上流传的 `deepseek-v4-flash-search`
# 之类带后缀的模型 ID —— 官方文档查无实据,实测也不需要。
DEEPSEEK_WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 5,
}

# [零检索兜底 · Owner 2026-07-27 拍板"不阻断流程"]
# 官方是模型自主决定搜不搜,24 题实测有 29% 的题一次都不搜。处理顺序:
#   ① 首次:不强制(保持模型自然行为)
#   ② 零检索 → 带一句"必须先检索"的 system 软重试(实测 3/3 全部救回)
#   ③ 仍为零 → 降级到阿里通道兜底
# 🔴 对**用户**是完全静默的(Owner 2026-07-27 定):客户报告走严格白名单
#    (report_writer_v2 "provider metadata never enter the customer artifact"),
#    provider/surface/search_mode/fallback_used 都不在白名单;citations 渲染只取
#    title+url,不读 via。所以客户看到的与改前一致,不会因为这个设计产生疑问。
# 🔴 对**我们自己的账本**必须留痕:provider 落 dashscope、surface 落 legacy、
#    每条来源带 via=dashscope_fallback。不留的话这一格会变成"71% DeepSeek + 29% 阿里"
#    的混合体 —— 我们自己也分不清哪些数是官方的,同比环比说不清是引擎变了还是
#    当天兜底比例变了,而且阿里检索器的域分布会顺着 citations 污染飞轮与发布推荐。
#    这是内部可审计性,不是给用户看的提示。
#
# **不用 tool_choice 强制**:6 题实测 6/6 全部没有正文 text 块(模型会一直调工具
# 不产出答案),已定论不可用。
# **不用秘塔兜底**:`deepseek_metaso_proxy` 表面在 lineage 里标 unavailable(禁止冒名);
# 且"DeepSeek 搜索逻辑与秘塔相似"的依据是模型自述,模型不知道自己的后端实现。
def _deepseek_search_hint_first() -> bool:
    """[效率优化 · 2026-07-27 实测驱动] **首发就带检索软约束**,默认开。

    24 题实测对照(生产真实题库,同一批题各跑一轮):

    | | 首发不带 system | **首发就带 system** |
    |---|---|---|
    | 命中检索 | 20/24 = 83.3% | **24/24 = 100%** |
    | 延迟中位 | 12.0s | **11.7s** |
    | 平均来源 | 16.9 | **17.8** |
    | input 合计 | 521,078 | **512,569** |
    | 端到端(含重试) | 291s + 4×12s ≈ 339s | **291s** |

    **更快、更省、来源更多、覆盖率 100%** —— 因为它把"零检索→重试"这条链整个消掉了,
    而不是让它跑得更快。重试与兜底保留作保险,但从常规路径变成几乎不触发的兜底。

    口径说明:这条 system 只要求"先检索再作答",**不改变问什么、不诱导任何品牌**。
    而且豆包/通义本来就用 `forced_search` 硬开关 —— 首发带约束反而让四家口径**更一致**,
    不是更偏。设 `DEEPSEEK_SEARCH_HINT_FIRST=0` 可退回"先自然后重试"的老口径做对照。
    """
    return os.getenv("DEEPSEEK_SEARCH_HINT_FIRST", "1").strip().lower() not in {
        "0", "false", "no", "off"
    }


# [复检 R2 返修 · 2026-07-27] **砍到只剩检索指令**。
#   上一版写的是"你正在为一份市场调研提供答案。用户问的是当前市场上有哪些服务商或品牌…
#   并说明你参考了哪些来源。" —— 复检抓出三处非检索内容:
#     · "市场调研" = 给模型加了任务框架;
#     · "有哪些服务商或品牌" = 断言了问题类型(对场景类/认知类问法可能是假的),
#       且**直接诱导枚举品牌**;
#     · "说明你参考了哪些来源" = 诱导陈述来源。
#   而 GEO 的两个核心指标恰好就是**品牌提及数**与**引用数** —— 这就是在诱导被测指标。
#   我原 docstring 写"不诱导任何品牌",与 prompt 原文自相矛盾,复检说得对。
#   且暴露面从"仅重试时发(17–29%)"变成"100% 调用都带",带偏就是全量带偏。
#   现只保留检索行为本身的指令,不描述问题、不要求列来源、不给任务框架。
DEEPSEEK_ZERO_SEARCH_SYSTEM = (
    "回答前必须先用 web_search 工具检索最新公开信息,再基于检索结果作答。"
)
# [Owner 2026-07-27] 定为 **3**:"重试 3 次不行就兜底,不然等太久了" —— 即给足重试机会,
#   但设硬上限,不无限等下去。
#   ⚠️ 如实记一笔:实测里软重试 1 次就 3/3 全部救回,所以 N=3 的收益主要在**极端情况**;
#   代价是零检索那条路径最坏多等 2 轮(每轮官方调用约 5–30s)。
#   便宜的部分是钱:零检索那次 input 仅 11 token,重试几乎不增成本 —— 贵的是**延迟**。
DEEPSEEK_ZERO_SEARCH_RETRIES_DEFAULT = 3
FALLBACK_SEARCH_MODE = "dashscope_fallback"


def _deepseek_zero_search_retries() -> int:
    try:
        return max(0, int(os.getenv("DEEPSEEK_ZERO_SEARCH_RETRIES",
                                    str(DEEPSEEK_ZERO_SEARCH_RETRIES_DEFAULT))))
    except (TypeError, ValueError):
        return DEEPSEEK_ZERO_SEARCH_RETRIES_DEFAULT


def _deepseek_fallback_enabled() -> bool:
    """零检索兜底总开关。默认**开**(Owner:不阻断流程);设 0/false 即回到纯官方口径。"""
    return os.getenv("DEEPSEEK_ZERO_SEARCH_FALLBACK", "1").strip().lower() not in {
        "0", "false", "no", "off"
    }


async def _deepseek_dashscope_fallback(
    query: str, check_brand: str, max_tokens: int,
    brand_id: int | None, brand_display_names: list[str] | None,
    reason: str,
) -> ToolResponse | None:
    """零检索兜底:走阿里通道拿数据,但**把降级如实标出来**。

    返回 None 表示兜底本身也失败(调用方据此保留官方那次的诚实零检索结果)。
    """
    fallback = await query_dashscope_deepseek(
        query, check_brand, max_tokens=max_tokens, brand_id=brand_id,
        brand_display_names=brand_display_names,
    )
    try:
        payload = json.loads(fallback.content[0]["text"])
    except (json.JSONDecodeError, ValueError, KeyError, IndexError):
        return None
    if not isinstance(payload, dict) or payload.get("engine_error"):
        return None

    # 🔴 三个标记缺一不可,否则就成了红线禁止的"静默回落"。
    payload["fallback_used"] = True
    payload["fallback_reason"] = reason
    payload["fallback_provider"] = "dashscope"
    # 驱动监测血缘:batch_monitor 用返回的 search_mode 去解析 provider/surface,
    # 所以这一格会如实落成 dashscope / deepseek_dashscope_search_legacy。
    payload["search_mode"] = FALLBACK_SEARCH_MODE
    # 来源逐条打标:下游(飞轮域分布 / B3 媒体组合决策)可据此排除兜底数据,
    # 免得阿里检索器的域分布污染我们对"AI 真正引用谁"的判断。
    for citation in payload.get("search_citations") or []:
        if isinstance(citation, dict):
            citation["via"] = FALLBACK_SEARCH_MODE

    # 降级本身是**预期内的常规路径**(模型自主决定不搜是它的正常行为),不是故障 ——
    # 所以这里用普通 info 语气,不打 ⚠️、不进告警面,免得运维每天看一堆"警告"。
    # 但**必须留一行**:兜底比例是评估"官方通道到底行不行"的唯一依据,不能没有痕迹。
    print(f"[DeepSeek-Official] 零检索 → 阿里兜底({reason})")
    return ToolResponse(content=[{
        "type": "text", "text": json.dumps(payload, ensure_ascii=False),
    }])


async def query_deepseek_official(
    query: str, check_brand: str = "", max_tokens: int = 2000,
    brand_id: int | None = None,
    brand_display_names: list[str] | None = None,
) -> ToolResponse:
    """查询 **DeepSeek 官方** 并检测品牌可见度(服务端原生联网检索)。

    为什么换掉 `query_dashscope_deepseek`
    -------------------------------------
    改前这一格打的是 DashScope,检索由**阿里**执行 —— 我们以为在测 DeepSeek 的引用行为,
    实际测的是阿里的检索行为。生产实证:DeepSeek 引用产出率 0.8%,而同样走阿里检索的
    通义是 29.2%,两家答案还高度相似。GEO 产品承诺的是"让客户被 AI 引用",
    测错对象等于这一格数据整体失效。

    协议差异(踩过一次就够了)
    -------------------------
    官方走 **Anthropic Messages 协议**,不是 OpenAI Chat Completions:
      · 端点 ``/anthropic/v1/messages``;鉴权头是 ``x-api-key``(不是 Bearer);
        必带 ``anthropic-version``;
      · 请求体 ``messages`` + 顶层 ``max_tokens``;
      · 响应 ``content`` 是**块数组**,实测出现过
        ``thinking`` / ``server_tool_use`` / ``web_search_tool_result`` / ``text``。
    所以不能套用本文件里那些 OpenAI 兼容封装。

    实测确认(2026-07-27 真实响应 · 证据 docs/AI-CONTEXT/ENGINE_SEARCH_FIDELITY_EVIDENCE_2026-07-27.md):
      1. 工具声明 ``web_search_20250305`` 被接受,模型真的发起 ``server_tool_use``;
      2. 引用只在 ``web_search_tool_result.content[]``(字段 title/url/page_age/encrypted_content),
         **text 块没有 citations[]** —— 工单原推测的行内锚点不存在;
      3. ``usage.server_tool_use.web_search_requests`` 有真实计数;
      4. 关思考(``thinking: disabled``)不影响检索触发,省 output token;
      5. 不声明工具就完全不搜(响应里没有任何 server_tool_use 块)。

    成本口径:带检索的一次调用实测 ``input_tokens`` 6.5 万+(检索结果全量进上下文),
    不带检索仅 14。这个量级差必须靠 ``_tracked_post`` 真实落库,禁止估算。
    """
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        # 🔴 fail-closed:缺官方 key 时显式报错,**绝不**静默回落 DashScope。
        #    静默回落正是"以为在测 DeepSeek 其实测的是阿里"这个坑的成因。
        print("[DeepSeek-Official] ❌ DEEPSEEK_API_KEY 未配置 · fail-closed 不回落 DashScope")
        return ToolResponse(content=[{"type": "text", "text": json.dumps({
            "answer_summary": "DeepSeek 官方 API key 未配置,本次未采集(不回落其它供应商)",
            "engine_error": True,
            "mentioned_brands": [], "brand_detected": False,
            "web_search_enabled": False, "search_citations": [], "full_response": "",
        }, ensure_ascii=False)}])

    max_retries = 3
    zero_search_rounds = _deepseek_zero_search_retries()
    hint_first = _deepseek_search_hint_first()
    # 默认**第 0 轮就带**检索软约束(实测把命中率从 83.3% 抬到 100%,且更快更省)。
    # 关掉开关则退回老口径:第 0 轮不带(模型自然行为),零检索后再带。
    for search_round in range(zero_search_rounds + 1):
      force_search_hint = hint_first or search_round > 0
      for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                _body = {
                    "model": DEEPSEEK_OFFICIAL_MODEL,
                    "max_tokens": max_tokens,
                    # 结构化采集任务不需要思维链;实测关掉不影响检索触发。
                    "thinking": {"type": "disabled"},
                    "tools": [dict(DEEPSEEK_WEB_SEARCH_TOOL)],
                    "messages": [{"role": "user", "content": query}],
                }
                if force_search_hint:
                    # 软约束重试。**不用 tool_choice 强制** —— 实测强制会让模型
                    # 一直调工具、不产出正文(6/6 空回答)。
                    _body["system"] = DEEPSEEK_ZERO_SEARCH_SYSTEM
                response = await _tracked_post(
                    client,
                    DEEPSEEK_OFFICIAL_MESSAGES_URL,
                    platform="deepseek_official",
                    model=DEEPSEEK_OFFICIAL_MODEL,
                    metadata={"engine": "deepseek_official", "attempt": attempt + 1,
                              "search_round": search_round,
                              "search_provider": "deepseek_native"},
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": api_key,
                        "anthropic-version": DEEPSEEK_ANTHROPIC_VERSION,
                    },
                    json=_body,
                )

                if response.status_code != 200:
                    if attempt < max_retries - 1:
                        print(f"[DeepSeek-Official] HTTP {response.status_code}，3 秒后重试 ({attempt + 1}/{max_retries})")
                        await asyncio.sleep(3)
                        continue
                    return ToolResponse(content=[{"type": "text", "text": json.dumps({
                        "answer_summary": f"DeepSeek 官方 API HTTP {response.status_code}",
                        "engine_error": True,
                        "mentioned_brands": [], "brand_detected": False,
                        "web_search_enabled": False, "search_citations": [], "full_response": "",
                    }, ensure_ascii=False)}])

                try:
                    data = response.json()
                except (json.JSONDecodeError, ValueError):
                    if attempt < max_retries - 1:
                        await asyncio.sleep(3)
                        continue
                    return ToolResponse(content=[{"type": "text", "text": json.dumps({
                        "answer_summary": f"DeepSeek 官方响应 JSON 解析失败(已重试{max_retries}次)",
                        "engine_error": True,
                        "mentioned_brands": [], "brand_detected": False,
                        "web_search_enabled": False, "search_citations": [], "full_response": "",
                    }, ensure_ascii=False)}])

                #: 🔴 [WO_221-c1] 回显锁:响应里的 model 必须等于我们请求的那个。
                #:   对**监测**来说模型身份就是被测量本身 —— 把 B 的回答记成 A 的,
                #:   整列 DeepSeek 数据都不可解读,而 HTTP 200、内容正常、账单不异常,
                #:   没有任何东西会报错(本仓 a-200-can-hide-a-silently-substituted-model)。
                #: 🔴 不采信 != 当成引擎没答:标成 engine_error 是**如实**的 ——
                #:   我们确实不知道是谁答的,把它记成 DeepSeek 才是编。
                try:
                    assert_official_echo(DEEPSEEK_OFFICIAL_MODEL, data.get('model'))
                except OfficialModelEchoMismatch as _echo_err:
                    print(f'[DeepSeek-Official] {_echo_err}')
                    return ToolResponse(content=[{"type": "text", "text": json.dumps({
                        "answer_summary": f"DeepSeek 官方回显模型不符:{_echo_err}",
                        "engine_error": True,
                        "mentioned_brands": [], "brand_detected": False,
                        "web_search_enabled": False, "search_citations": [], "full_response": "",
                    }, ensure_ascii=False)}])

                ai_response = _deepseek_official_text(data)
                if not ai_response and attempt < max_retries - 1:
                    print(f"[DeepSeek-Official] 空内容，3 秒后重试 ({attempt + 1}/{max_retries})")
                    await asyncio.sleep(3)
                    continue
                if not ai_response:
                    return ToolResponse(content=[{"type": "text", "text": json.dumps({
                        "answer_summary": "DeepSeek 官方空内容(已重试耗尽)",
                        "engine_error": True,
                        "mentioned_brands": [], "brand_detected": False,
                        "web_search_enabled": False, "search_citations": [], "full_response": "",
                    }, ensure_ascii=False)}])

                search_citations = _extract_deepseek_official_citations(data)
                search_requests = _count_deepseek_web_search_requests(data)

                visibility_result = await _call_analyze_visibility(
                    ai_response, query, check_brand, "DeepSeek", brand_id, brand_display_names
                )
                # 如实记:检索**真的发生过**才算 enabled(不拿"我们声明了工具"当已检索)。
                visibility_result["web_search_enabled"] = bool(search_requests > 0)
                visibility_result["search_citations"] = search_citations
                visibility_result["web_search_request_count"] = search_requests

                # 触发了检索却一条来源都解析不到 = 解析口径与响应结构脱节,必须留痕。
                # (Kimi 那个洞就是这么长期静默存在的,见工单 §B-2。)
                if search_requests > 0 and not search_citations:
                    block_types = [
                        b.get("type") for b in (data.get("content") or []) if isinstance(b, dict)
                    ]
                    print(
                        f"[DeepSeek-Official] 检索已触发 {search_requests} 次但未解析到任何来源 "
                        f"· 响应块类型={block_types}"
                    )

                visibility_result["search_round"] = search_round

                # 零检索:还有软重试额度就再来一轮(换外层 for),否则进兜底。
                if search_requests == 0:
                    if search_round < zero_search_rounds:
                        print(
                            f"[DeepSeek-Official] 零检索 · 软重试 "
                            f"({search_round + 1}/{zero_search_rounds})"
                        )
                        break  # 跳出 attempt 循环 → 外层进入下一 search_round
                    if _deepseek_fallback_enabled():
                        fb = await _deepseek_dashscope_fallback(
                            query, check_brand, max_tokens, brand_id, brand_display_names,
                            reason=f"official_zero_search_after_{zero_search_rounds}_retries",
                        )
                        if fb is not None:
                            return fb
                        print("[DeepSeek-Official] 兜底不可用 · 保留官方零检索结果(如实)")

                # [work order V3-C C-3] provider-echoed model name.
                visibility_result["echoed_model"] = _echoed_model(data)
                return ToolResponse(content=[{
                    "type": "text",
                    "text": json.dumps(visibility_result, ensure_ascii=False),
                }])
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"[DeepSeek-Official] 异常: {e}，3 秒后重试 ({attempt + 1}/{max_retries})")
                await asyncio.sleep(3)
                continue
            return ToolResponse(content=[{"type": "text", "text": json.dumps({
                "answer_summary": f"DeepSeek 官方调用异常: {str(e)[:200]}",
                "engine_error": True,
                "mentioned_brands": [], "brand_detected": False,
                "web_search_enabled": False, "search_citations": [], "full_response": "",
            }, ensure_ascii=False)}])
    # 兜底的兜底:软重试轮次全部走完仍未 return(理论上不可达,防御性收尾)。
    return ToolResponse(content=[{"type": "text", "text": json.dumps({
        "answer_summary": "DeepSeek 官方未产出可用结果",
        "engine_error": True,
        "mentioned_brands": [], "brand_detected": False,
        "web_search_enabled": False, "search_citations": [], "full_response": "",
    }, ensure_ascii=False)}])


# ===============================
# Metaso (秘塔) - 联网搜索
# ===============================
async def query_metaso_search(
    query: str, check_brand: str = "", max_tokens: int = 2000,
    brand_id: int | None = None,
    brand_display_names: list[str] | None = None,
) -> ToolResponse:
    """
    查询 秘塔 AI 并检测品牌可见度（联网搜索版）

    秘塔使用DeepSeek模型后端，是最接近DeepSeek官方联网体验的方式
    """
    from tools.search.metaso_mcp import metaso_chat

    try:
        result = await metaso_chat(query, model="fast")

        # 解析响应
        try:
            content = result.content[0] if result.content else {}
            if isinstance(content, dict) and content.get("type") == "text":
                result_data = json.loads(content.get("text", "{}"))
            else:
                result_data = content
        except:
            result_data = {}

        # 提取回答
        ai_response = ""
        if isinstance(result_data, dict):
            answer = result_data.get("answer", "")
            if isinstance(answer, str):
                ai_response = answer
            elif isinstance(answer, dict):
                # MCP响应可能嵌套
                content_list = answer.get("content", [])
                if isinstance(content_list, list):
                    ai_response = " ".join(
                        item.get("text", "")
                        for item in content_list
                        if isinstance(item, dict)
                    )

        visibility_result = await _call_analyze_visibility(
            ai_response, query, check_brand, "Metaso", brand_id, brand_display_names
        )
        visibility_result["web_search_enabled"] = True  # 秘塔有联网
        visibility_result["response"] = ai_response  # 保存完整回答

        return ToolResponse(
            content=[
                {
                    "type": "text",
                    "text": json.dumps(visibility_result, ensure_ascii=False),
                }
            ]
        )
    except Exception as e:
        return ToolResponse(content=[{"type": "text", "text": f"Error: {str(e)}"}])


# ===============================
# DeepSeek - 通过DashScope支持联网搜索
# ===============================
async def query_deepseek(
    query: str, check_brand: str = "", max_tokens: int = 2000,
    brand_id: int | None = None,
    brand_display_names: list[str] | None = None,
) -> ToolResponse:
    """
    查询 DeepSeek AI 并检测品牌可见度

    通过DashScope调用DeepSeek-V3，支持enable_search联网搜索
    """
    # 使用DashScope的API Key和URL（支持联网）
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        return ToolResponse(
            content=[
                {"type": "text", "text": "Error: DASHSCOPE_API_KEY not configured"}
            ]
        )
    max_retries = 3

    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await _tracked_post(
                    client,
                    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                    platform="dashscope",
                    model="deepseek-v4-flash",
                    metadata={"engine": "dashscope_deepseek_compatible", "attempt": attempt + 1},
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "deepseek-v4-flash",  # 2026-05-09 升级 v3.2 → v4-flash
                        "messages": [{"role": "user", "content": query}],
                        "max_tokens": max_tokens,
                        "enable_search": True,  # 🔑 开启联网搜索
                    },
                )

                if response.status_code != 200:
                    if attempt < max_retries - 1:
                        print(f"[DeepSeek] ⚠️ HTTP {response.status_code}，{3}秒后重试 ({attempt + 1}/{max_retries})")
                        await asyncio.sleep(3)
                        continue
                    return ToolResponse(
                        content=[
                            {
                                "type": "text",
                                "text": f"Error: HTTP {response.status_code} - {response.text}",
                            }
                        ]
                    )

                # 防御空响应
                try:
                    data = response.json()
                except (json.JSONDecodeError, ValueError):
                    if attempt < max_retries - 1:
                        print(f"[DeepSeek] ⚠️ 响应JSON解析失败，{3}秒后重试 ({attempt + 1}/{max_retries})")
                        await asyncio.sleep(3)
                        continue
                    return ToolResponse(content=[{"type": "text", "text": json.dumps({
                        "answer_summary": f"DeepSeek API返回空响应（已重试{max_retries}次）",
                        "mentioned_brands": [], "brand_detected": False,
                        "web_search_enabled": False, "full_response": ""
                    }, ensure_ascii=False)}])

                ai_response = (
                    data.get("choices", [{}])[0].get("message", {}).get("content", "")
                )

                # 空响应重试
                if not ai_response.strip() and attempt < max_retries - 1:
                    print(f"[DeepSeek] ⚠️ 空响应，{3}秒后重试 ({attempt + 1}/{max_retries})")
                    await asyncio.sleep(3)
                    continue

                visibility_result = await _call_analyze_visibility(
                    ai_response, query, check_brand, "DeepSeek", brand_id, brand_display_names
                )
                visibility_result["web_search_enabled"] = True  # 已联网

                return ToolResponse(
                    content=[
                        {
                            "type": "text",
                            "text": json.dumps(visibility_result, ensure_ascii=False),
                        }
                    ]
                )
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"[DeepSeek] ⚠️ 异常: {e}，{3}秒后重试 ({attempt + 1}/{max_retries})")
                await asyncio.sleep(3)
                continue
            return ToolResponse(content=[{"type": "text", "text": f"Error: {str(e)}"}])


# ===============================
# 旧版兼容接口（逐步废弃）
# ===============================
async def query_kimi(
    query: str, check_brand: str = "", max_tokens: int = 2000
) -> ToolResponse:
    """旧版Kimi查询（不联网），保留兼容性"""
    return await query_kimi_search(query, check_brand, max_tokens)


async def query_doubao(
    query: str, check_brand: str = "", max_tokens: int = 2000
) -> ToolResponse:
    """旧版豆包查询（不联网），保留兼容性"""
    return await query_doubao_search(query, check_brand, max_tokens)


# ===============================
# DDS: 搜索引用提取函数
# ===============================

import re as _re


def _extract_urls_from_text(text: str) -> list[str]:
    """从文本中提取所有 URL"""
    if not text:
        return []
    urls = _re.findall(r"https?://[^\s\)\]\）」》,，。、\n]+", text)
    # 去重并去掉尾部标点
    clean_urls = []
    seen = set()
    for url in urls:
        url = url.rstrip(".,;:!?。，；：！？")
        if url not in seen:
            seen.add(url)
            clean_urls.append(url)
    return clean_urls


def _dashscope_message_text(output: dict) -> str:
    """从 DashScope 原生响应里取正文,**两种 content 形状都吃**。

    · 文本端点(``text-generation``):``message.content`` 是 ``str``;
    · 多模态端点(``multimodal-generation``,包F ⑥ 换代后千问走这条):
      ``message.content`` 是 ``[{"text": "..."}, ...]``。

    🔴 一个函数吃两种形状,而不是在两条调用链各写一份判断:
       本仓记过「同一谓词写两处 ⇒ 必有一处没人验」。DeepSeek 那条链仍走
       文本端点,让它也过这里 —— 于是哪天它也换端点,不需要再改一次。

    🔴 拿不到正文时返回**空串**而不是抛:调用方已经有一套
       "空内容 ⇒ engine_error fail-closed" 的处理(资金相关,#3-C2 P0-2),
       在这里抛会绕过那套处理,把一个可退费的引擎失败变成裸异常。
    """
    try:
        choices = output.get("choices") or []
        if not choices:
            return ""
        content = (choices[0] or {}).get("message", {}).get("content", "")
    except (AttributeError, IndexError, TypeError):
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                # 多模态返回里非文本片段(图/视频)没有 text 键 —— 跳过而不是
                # str() 它,否则正文里会混进 dict 的 repr。
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return ""


def _extract_dashscope_native_citations(output: dict) -> list[dict]:
    """
    从 DashScope 原生协议的 output 中提取搜索来源

    DashScope 原生协议响应格式（2026-02 文档确认）：
    data.output.search_info.search_results = [
        {"index": 1, "title": "...", "url": "..."},
        {"index": 2, "title": "...", "url": "..."},
        ...
    ]

    注意：仅 DashScope 原生协议支持返回搜索来源，OpenAI 兼容接口不支持。
    需要设置 search_options.enable_source: True
    """
    citations = []

    search_info = output.get("search_info", {})
    search_results = search_info.get("search_results", [])

    for item in search_results:
        if isinstance(item, dict):
            url = item.get("url", "")
            if url:
                citations.append(
                    {
                        "title": item.get("title", ""),
                        "url": url,
                        "site_name": item.get("site_name", item.get("hostname", "")),
                        "snippet": str(item.get("snippet", item.get("content", "")))[
                            :200
                        ],
                        "index": item.get("index", 0),
                    }
                )

    # 也提取 extra_tool_info（垂域搜索结果）
    extra_tool_info = search_info.get("extra_tool_info", [])
    if extra_tool_info:
        for info in extra_tool_info:
            if isinstance(info, dict):
                citations.append(
                    {
                        "title": "[垂域搜索]",
                        "url": "",
                        "site_name": "",
                        "snippet": str(info.get("result", ""))[:200],
                        "source": "extra_tool",
                    }
                )

    # 从文本中提取 URL 作为补充
    # 🔴 [R2 · 2026-08-24] 这里**必须**走 ``_dashscope_message_text``。
    #    R1 换代到多模态端点后,这一行还在裸取 ``message.content`` ——
    #    而多模态的 content 是**数组**,直接喂给 ``_extract_urls_from_text``
    #    里的 ``re.findall`` 就是
    #    ``TypeError: expected string or bytes-like object, got 'list'``。
    #    外层 ``except Exception`` 把它吞成"重试三次 → engine_error",
    #    于是**每一次千问监测都 HTTP 200 却判引擎失败**,线上表现是
    #    换代当天监测全线静默失败。
    #
    #    R1 已经把主链那一处折平了、也配了提取器锁,却漏了这第二处 ——
    #    锁打在纯函数上、没打在真正跑完整条 handler 的出口上,
    #    于是 70 条判据 + 22 发变异全绿。真链判据
    #    ``test_assembled_request_body_carries_thinking_off``
    #    (它会断言"一次调用零重试")才是抓到它的那一条。
    ai_response = _dashscope_message_text(output)
    text_urls = _extract_urls_from_text(ai_response)
    url_set = {c["url"] for c in citations}
    for url in text_urls:
        if url not in url_set:
            citations.append(
                {
                    "title": "",
                    "url": url,
                    "site_name": "",
                    "snippet": "",
                    "source": "in_text",
                }
            )

    if citations:
        print(
            f"[DDS] DashScope 原生协议引用: {len(citations)} 条 (API: {len(search_results)}, text: {len(text_urls)})"
        )

    return citations


def _extract_dashscope_citations(data: dict) -> list[dict]:
    """
    从 DashScope API 返回中提取联网搜索引用数据

    DashScope enable_search=True 时，返回格式可能包含：
    - data["web_search_info"]["search_results"] (顶层)
    - data["choices"][0]["message"]["web_search_info"]["search_results"] (消息级)
    - data["choices"][0]["web_search_info"] (choice级)

    每条搜索结果通常有: title, url, site_name, snippet/content
    """
    citations = []

    # 尝试多个可能的位置提取 web_search_info
    search_info = None

    # 位置1: 顶层
    if "web_search_info" in data:
        search_info = data["web_search_info"]

    # 位置2: choice 级别
    if not search_info:
        choices = data.get("choices", [])
        if choices:
            choice = choices[0]
            if "web_search_info" in choice:
                search_info = choice["web_search_info"]
            elif "message" in choice and "web_search_info" in choice.get("message", {}):
                search_info = choice["message"]["web_search_info"]

    # 位置3: 检查 metadata / search_info 等其他可能字段名
    if not search_info:
        for key in ("search_info", "search_results", "references", "citations"):
            if key in data:
                search_info = data[key]
                break

    if search_info:
        # 如果是列表，直接使用
        results = (
            search_info
            if isinstance(search_info, list)
            else search_info.get("search_results", [])
        )

        for item in results:
            if isinstance(item, dict):
                citation = {
                    "title": item.get("title", ""),
                    "url": item.get("url", item.get("link", "")),
                    "site_name": item.get("site_name", item.get("hostname", "")),
                    "snippet": item.get(
                        "snippet", item.get("content", item.get("text", ""))
                    )[:200],
                }
                if citation["url"]:
                    citations.append(citation)

    # 补充：从回复文本中提取 URL（作为备用）
    ai_response = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    text_urls = _extract_urls_from_text(ai_response)
    url_set = {c["url"] for c in citations}
    for url in text_urls:
        if url not in url_set:
            citations.append(
                {
                    "title": "",
                    "url": url,
                    "site_name": "",
                    "snippet": "",
                    "source": "in_text",  # 标记为文本内提取
                }
            )

    if citations:
        print(
            f"[DDS] DashScope 引用: {len(citations)} 条 (API: {len(citations) - len(text_urls)}, 文本: {len(text_urls)})"
        )

    return citations


def _extract_kimi_citations(
    data: dict, search_queries: list, ai_response: str
) -> list[dict]:
    """
    从 Kimi 的响应中提取搜索引用

    Kimi 的 $web_search 是内置函数，搜索结果由服务端处理。
    我们可以从以下来源提取引用：
    1. 最终响应的 message 中可能有 search_results / references 字段
    2. 记录的搜索查询参数
    3. 回复文本中内嵌的 URL
    """
    citations = []

    # 从 API 响应中查找引用字段
    choice = data.get("choices", [{}])[0]
    message = choice.get("message", {})

    # 检查 Kimi 是否在 message 中返回引用
    for key in (
        "search_results",
        "references",
        "citations",
        "web_search_results",
        "context",
    ):
        ref_data = message.get(key) or data.get(key)
        if ref_data and isinstance(ref_data, list):
            for item in ref_data:
                if isinstance(item, dict) and item.get("url"):
                    citations.append(
                        {
                            "title": item.get("title", ""),
                            "url": item.get("url", ""),
                            "site_name": item.get("site_name", ""),
                            "snippet": str(
                                item.get("snippet", item.get("content", ""))
                            )[:200],
                        }
                    )

    # 记录搜索查询信息（即使没有 URL，也是有价值的元数据）
    search_meta = []
    for sq in search_queries:
        if isinstance(sq, dict):
            search_meta.append(sq.get("query", sq.get("keyword", str(sq))))
        else:
            search_meta.append(str(sq))

    # 从文本中提取 URL
    text_urls = _extract_urls_from_text(ai_response)
    url_set = {c["url"] for c in citations}
    for url in text_urls:
        if url not in url_set:
            citations.append(
                {
                    "title": "",
                    "url": url,
                    "site_name": "",
                    "snippet": "",
                    "source": "in_text",
                }
            )

    # 如果有搜索查询元数据，添加到第一条引用中或创建元数据记录
    if search_meta and not citations:
        citations.append(
            {
                "title": "[Kimi search queries]",
                "url": "",
                "site_name": "",
                "snippet": ", ".join(search_meta),
                "source": "search_meta",
            }
        )
    elif search_meta and citations:
        citations[0]["search_queries"] = search_meta

    if citations:
        print(f"[DDS] Kimi 引用: {len(citations)} 条")

    return citations


def _extract_web_search_citations(
    annotations: list, search_queries: list, ai_response: str
) -> list[dict]:
    """
    从 web_search 基础联网搜索的 annotations 中提取引用

    web_search 返回格式：
    output[].type = "message"
    output[].content[].annotations = [
        {type: "url_citation", url: "...", title: "...", start_index: N, end_index: N}, ...
    ]
    """
    citations = []
    url_set = set()

    # 从 annotations 提取引用
    for ann in annotations:
        if not isinstance(ann, dict):
            continue
        url = ann.get("url", "")
        if url and url not in url_set:
            url_set.add(url)
            citations.append({
                "title": ann.get("title", ""),
                "url": url,
                "site_name": "",
                "snippet": "",
            })

    # 从文本中提取额外的 URL
    text_urls = _extract_urls_from_text(ai_response)
    for url in text_urls:
        if url not in url_set:
            url_set.add(url)
            citations.append({
                "title": "",
                "url": url,
                "site_name": "",
                "snippet": "",
                "source": "in_text",
            })

    if citations:
        # 添加搜索元数据
        if search_queries:
            citations[0]["search_queries"] = search_queries
        print(f"[DDS] 豆包 web_search 引用: {len(citations)} 条 (queries: {search_queries})")

    return citations


def _extract_doubao_citations(
    data: dict, citation_blocks: list, ai_response: str
) -> list[dict]:
    """
    [旧版] 从豆包 doubao_app 的返回中提取搜索引用（保留向后兼容）

    实际数据结构（2026-02 确认）：
    output[0].type = "doubao_app_call"
    output[0].blocks = [
        {type: "search", summary: "搜索 2 个关键词，参考 12 篇资料",
         queries: ["关键词1", "关键词2"],
         results: [{text_card: {title, sitename, url}}, ...]},
        {type: "output_text", text: "..."}
    ]
    """
    citations = []
    search_queries = []
    search_summary = ""

    # 从收集的非文本 blocks 中提取引用
    for block in citation_blocks:
        if not isinstance(block, dict):
            continue

        block_type = block.get("type", "")

        # 🔑 核心：type="search" 的 block 包含搜索结果
        if block_type == "search":
            search_summary = block.get("summary", "")
            search_queries = block.get("queries", [])
            results = block.get("results", [])

            for result_item in results:
                if not isinstance(result_item, dict):
                    continue

                # 实际结构：results[].text_card.{title, sitename, url}
                text_card = result_item.get("text_card", {})
                if text_card and isinstance(text_card, dict):
                    url = text_card.get("url", "")
                    if url:
                        citations.append(
                            {
                                "title": text_card.get("title", ""),
                                "url": url,
                                "site_name": text_card.get(
                                    "sitename", text_card.get("site_name", "")
                                ),
                                "snippet": "",
                            }
                        )
                        continue

                # 兜底：直接在 result_item 层查找
                url = result_item.get("url", result_item.get("link", ""))
                if url:
                    citations.append(
                        {
                            "title": result_item.get("title", ""),
                            "url": url,
                            "site_name": result_item.get(
                                "sitename", result_item.get("site_name", "")
                            ),
                            "snippet": str(
                                result_item.get("text", result_item.get("snippet", ""))
                            )[:200],
                        }
                    )
            continue

        # 直接的引用/来源块
        if block_type in (
            "reference",
            "citation",
            "source",
            "search_result",
            "web_search",
        ):
            url = block.get("url", block.get("link", ""))
            if url:
                citations.append(
                    {
                        "title": block.get("title", ""),
                        "url": url,
                        "site_name": block.get("site_name", block.get("hostname", "")),
                        "snippet": str(
                            block.get(
                                "text", block.get("content", block.get("snippet", ""))
                            )
                        )[:200],
                    }
                )
                continue

        # 嵌套结构：blocks 内部可能有 items/results
        for key in ("items", "results", "references", "sources", "data"):
            nested = block.get(key, [])
            if isinstance(nested, list):
                for item in nested:
                    if isinstance(item, dict):
                        # 先检查 text_card 包装
                        text_card = item.get("text_card", {})
                        if text_card and isinstance(text_card, dict):
                            url = text_card.get("url", "")
                            if url:
                                citations.append(
                                    {
                                        "title": text_card.get("title", ""),
                                        "url": url,
                                        "site_name": text_card.get(
                                            "sitename", text_card.get("site_name", "")
                                        ),
                                        "snippet": "",
                                    }
                                )
                                continue

                        # 直接在 item 层查找
                        url = item.get("url", item.get("link", ""))
                        if url:
                            citations.append(
                                {
                                    "title": item.get("title", ""),
                                    "url": url,
                                    "site_name": item.get("site_name", ""),
                                    "snippet": str(
                                        item.get("text", item.get("snippet", ""))
                                    )[:200],
                                }
                            )

    # 添加搜索元数据
    if search_queries and citations:
        citations[0]["search_queries"] = search_queries
        citations[0]["search_summary"] = search_summary

    # 检查 data 顶层的引用字段
    for key in ("references", "citations", "sources", "search_results"):
        ref_data = data.get(key)
        if ref_data and isinstance(ref_data, list):
            url_set = {c["url"] for c in citations}
            for item in ref_data:
                if isinstance(item, dict):
                    url = item.get("url", item.get("link", ""))
                    if url and url not in url_set:
                        citations.append(
                            {
                                "title": item.get("title", ""),
                                "url": url,
                                "site_name": item.get("site_name", ""),
                                "snippet": str(
                                    item.get("text", item.get("snippet", ""))
                                )[:200],
                            }
                        )

    # 如果有未解析的 citation_blocks，保存原始结构作为调试信息
    if citation_blocks and not citations:
        block_types = [
            b.get("type", "unknown") for b in citation_blocks[:5] if isinstance(b, dict)
        ]
        block_keys = []
        for b in citation_blocks[:3]:
            if isinstance(b, dict):
                block_keys.append(list(b.keys())[:5])
        citations.append(
            {
                "title": "[Doubao unresolved blocks]",
                "url": "",
                "site_name": "",
                "snippet": f"types={block_types}, keys={block_keys}",
                "source": "debug_raw_blocks",
                "raw_block_count": len(citation_blocks),
            }
        )

    # 从文本中提取 URL
    text_urls = _extract_urls_from_text(ai_response)
    url_set = {c.get("url", "") for c in citations}
    for url in text_urls:
        if url not in url_set:
            citations.append(
                {
                    "title": "",
                    "url": url,
                    "site_name": "",
                    "snippet": "",
                    "source": "in_text",
                }
            )

    if citations:
        print(
            f"[DDS] 豆包 引用: {len(citations)} 条 (blocks: {len(citation_blocks)}, queries: {search_queries})"
        )

    return citations


# 品牌别名覆盖（使用 contextvars 实现异步任务隔离，防止监测与诊断并发时互相干扰）
# ⚠️ 旧的全局变量保留为 None，不再使用；新代码统一用 _custom_brand_ctx
_custom_brand_variants_override = None  # DEPRECATED: 仅保留供向后兼容，不再生效
_custom_brand_ctx: contextvars.ContextVar = contextvars.ContextVar(
    'custom_brand_variants', default=None
)


# [CTO-15.23 2026-05-05] 行业泛词词典 · 用于 _analyze_visibility 弱匹配判断
# 多 token variants 全部 ∈ 此集合 → 视为行业泛词组合(如"瑜伽普拉提")· 强迫走 LLM verify
# 老板原 case:"深圳怡然瑜伽普拉提" → variant "瑜伽普拉提" 撞 response "悦享瑜伽普拉提" 假阳
# 维护原则:仅纳入"独立成行业类目"的词 · 不纳入泛形容词(高端/专业/优质)
_INDUSTRY_GENERIC_TOKENS = {
    # 健身/运动
    "瑜伽", "普拉提", "健身", "舞蹈", "拳击", "搏击", "武术", "太极",
    # 美容/护肤
    "美容", "美发", "美甲", "纹绣", "护肤", "spa", "SPA", "理发",
    # 培训/教育
    "培训", "教育", "辅导", "考研", "留学", "早教", "幼教",
    # 装修/家装
    "装修", "装饰", "家装", "全屋", "整装", "硬装", "软装", "工装",
    # 餐饮
    "餐饮", "火锅", "烧烤", "外卖", "茶饮", "咖啡", "烘焙", "料理",
    # 不动产
    "地产", "房产", "置业", "公寓", "楼盘",
    # 建材
    "建材", "板材", "瓷砖", "木门", "地板", "墙板", "涂料",
    # 酒店/住宿
    "酒店", "宾馆", "民宿", "客栈",
    # 设备/工业
    "电梯", "空调", "锅炉", "净水", "暖通", "消防", "安防",
    # 服务
    "咨询", "设计", "维修", "保养", "搬家", "保洁", "婚庆", "摄影",
    # 商业泛词
    "推荐", "服务",
}


# ===============================
# 工具函数
# ===============================
def _generate_brand_variants(brand_name: str, custom_variants: list[str] = None) -> list[str]:
    """
    生成品牌名称的常见变体（jieba 智能分词版）

    策略：
    1. 客户自定义别名优先（参数 > 全局变量）
    2. 分隔符拆分（·、括号等）
    3. jieba 分词提取有意义的词组组合
    4. 不依赖硬编码的地区/行业列表，用分词自动识别边界

    Args:
        brand_name: 品牌全称
        custom_variants: 客户指定的品牌别名列表（优先级最高，线程安全）
    """
    # 客户自定义别名优先：参数传递 > contextvars（两者都是异步安全的）
    effective_custom = custom_variants or _custom_brand_ctx.get()
    if effective_custom:
        result = list(effective_custom)
        print(
            f"[Brand Variants] 使用客户指定品牌别名: {result} (原始公司名: {brand_name})"
        )
        return result

    import re
    import jieba
    import jieba.posseg as pseg

    variants = set()
    variants.add(brand_name)

    # ===== Step 1: 分隔符拆分 =====
    # "揭阳大昀地产·万汇广场" → "揭阳大昀地产", "万汇广场"
    separators = r"[·•・｜|—–\-/&]"
    parts = [p.strip() for p in re.split(separators, brand_name) if p.strip() and len(p.strip()) >= 2]
    if len(parts) > 1:
        for part in parts:
            variants.add(part)

    # ===== Step 2: 括号变体处理 =====
    if re.search(r"[（()）)]", brand_name):
        # 去括号保留内容
        no_parens = re.sub(r"[（()）)]", "", brand_name).strip()
        if no_parens and len(no_parens) >= 2:
            variants.add(no_parens)
        # 去括号及内容
        no_paren_content = re.sub(r"[（(][^）)]*[）)]", "", brand_name).strip()
        if no_paren_content and len(no_paren_content) >= 2:
            variants.add(no_paren_content)
        # 仅括号内的内容（可能是核心品牌名，如"XX（品牌名）有限公司"）
        inner = re.findall(r"[（(]([^）)]+)[）)]", brand_name)
        for i in inner:
            i = i.strip()
            if len(i) >= 2:
                variants.add(i)

    # ===== Step 3: jieba 分词 — 对所有当前变体做智能拆解 =====
    STOPWORD_FLAGS = {"ns", "f", "r", "p", "c", "u", "x", "m", "q", "d"}
    # 通用/行业词，不应作为独立检测变体（会大量误匹配）
    GENERIC_WORDS = {
        # 公司组织形式
        "有限", "股份", "责任", "公司", "集团", "中国", "中华", "国际",
        "有限公司", "股份有限公司", "有限责任公司",
        # 行政区划
        "省", "市", "区", "县", "镇", "街道",
        # IT/科技
        "科技", "技术", "网络", "信息", "数据", "智能", "电子", "软件", "互联网",
        # 房产/建筑
        "地产", "房产", "置业", "实业", "控股", "投资", "建筑", "建材", "开发",
        "广场", "中心", "大厦", "花园", "公寓",
        # 制造/商业
        "制造", "生产", "加工", "工厂", "工程", "设备", "材料", "贸易", "商贸",
        # 服务/文化
        "医疗", "教育", "文化", "传媒", "广告", "物流", "服务", "咨询",
        "食品", "能源", "化工", "设计", "品牌",
        # 家居行业高频通用词（极易误匹配）
        "定制", "全屋", "家具", "家居", "装修", "装饰", "橱柜", "衣柜", "木作",
        "板材", "实木", "原木", "红木", "木门", "地板", "瓷砖",
        "美学", "环保", "高端", "豪华", "奢华", "简约", "现代",
        "推荐", "优质", "专业", "品质",
    }
    ORG_SUFFIXES = [
        "有限责任公司", "股份有限公司", "有限公司", "集团公司", "集团",
        "公司", "门店", "分店", "旗舰店",
    ]

    def _residue_after_generic_and_location(text: str) -> str:
        """去掉地名/行业通用词/组织后缀后，检查是否还剩真正的品牌识别词。

        监测里最怕把"璧山"、"装饰设计有限公司"这类泛词当品牌别名。
        这类词一旦进入 variants，"璧山装修哪家好"的竞品回答会被误判为命中客户品牌。
        """
        residue = text.strip()
        for suffix in sorted(ORG_SUFFIXES, key=len, reverse=True):
            if residue.endswith(suffix):
                residue = residue[: -len(suffix)]
                break

        for word, flag in pseg.cut(residue):
            w = word.strip()
            if not w:
                continue
            if flag == "ns" or w in GENERIC_WORDS:
                residue = residue.replace(w, "")

        for generic in sorted(GENERIC_WORDS, key=len, reverse=True):
            residue = residue.replace(generic, "")

        return residue.strip()

    def _is_location_only(text: str) -> bool:
        tokens = [(w.strip(), f) for w, f in pseg.cut(text.strip()) if w.strip()]
        return bool(tokens) and all(flag == "ns" for _, flag in tokens)

    all_to_segment = list(variants)
    for text in all_to_segment:
        words = list(pseg.cut(text))

        # 收集每个词及其词性
        meaningful_words = []
        for word, flag in words:
            word = word.strip()
            if len(word) < 2:
                continue
            if flag in STOPWORD_FLAGS:
                continue
            if word in GENERIC_WORDS:
                continue
            meaningful_words.append(word)

        # 单个有意义的词作为变体
        for w in meaningful_words:
            variants.add(w)

        # 相邻有意义词的组合（保留词序），捕获如 "大昀地产"、"万汇广场" 等
        if len(meaningful_words) >= 2:
            for i in range(len(meaningful_words)):
                for j in range(i + 2, min(i + 4, len(meaningful_words) + 1)):
                    combo = "".join(meaningful_words[i:j])
                    if len(combo) >= 3 and combo != brand_name:
                        variants.add(combo)

    # ===== Step 4: 用原文连续子串补充（修复 jieba 分词错误）=====
    # jieba 可能把"奥特莱斯"分成"奥特"+"莱斯"，但原文连续子串能兜住
    # 对每个变体，用 jieba.cut 后取相邻 2-3 个 token 的原文子串
    substr_variants = set()
    for text in all_to_segment:
        tokens = list(jieba.cut(text))
        for i in range(len(tokens)):
            for span in range(2, min(5, len(tokens) - i + 1)):
                substr = "".join(tokens[i:i + span])
                if 2 <= len(substr) <= len(text) and substr != text and substr not in GENERIC_WORDS:
                    # 只保留不全是通用词的子串
                    substr_variants.add(substr)
    variants.update(substr_variants)

    # ===== Step 5: 后缀/前缀剥离（基于词性，不硬编码列表）=====
    new_variants = set()
    for v in list(variants):
        if v in GENERIC_WORDS or len(v) < 4:
            continue
        words = list(pseg.cut(v))
        if len(words) >= 2:
            last_word, last_flag = words[-1]
            if last_flag in ("n", "vn", "an") and len(last_word) <= 3:
                core = v[: -len(last_word)].strip()
                if len(core) >= 2:
                    new_variants.add(core)
            first_word, first_flag = words[0]
            if first_flag == "ns" and len(v) > len(first_word) + 1:
                core = v[len(first_word):].strip()
                if len(core) >= 2:
                    new_variants.add(core)
    variants.update(new_variants)

    # ===== Step 6: 过滤太短、太通用、含残余标点的变体 =====
    punct_chars = set("·•・｜|—–-/&（()）)\"'""''")

    def _is_ascii_brand(s: str) -> bool:
        """判断是否为纯英文/数字品牌名（如 QZQZ, BYD, 7天）"""
        return all(c.isascii() for c in s if not c.isspace())

    filtered = []
    for v in variants:
        if len(v) < 2:
            continue
        if v in GENERIC_WORDS:
            continue
        # 去掉首尾标点残余
        clean = v.strip()
        while clean and clean[0] in punct_chars:
            clean = clean[1:]
        while clean and clean[-1] in punct_chars:
            clean = clean[:-1]
        if len(clean) < 2:
            continue
        if clean in GENERIC_WORDS:
            continue
        # 关键规则：2字纯中文变体需要用词性判断是否为品牌名
        # 英文/混合品牌名（如 QZQZ, BYD）不受此限制
        if clean != brand_name and _is_location_only(clean):
            continue
        if clean != brand_name and len(_residue_after_generic_and_location(clean)) < 2:
            continue
        # [CTO-15.23 2026-05-05] P0 修单 token 行业词混入变体 bug
        # 老板 case:"深圳怡然瑜伽普拉提" → 原算法生成"普拉提"(3字单 token n)作变体
        # → response 含"器械普拉提/垫上普拉提/孕产普拉提" 5 次 → 高置信度放行 → 假阳
        # 修法:把 len==2 单 token 词性过滤延伸到 ≤5 字单 token(覆盖"普拉提"/"瑜伽馆"等行业词)
        # 多 token 变体("怡然瑜伽")保留 · 因为它含品牌识别词组合
        if 2 <= len(clean) <= 5 and not _is_ascii_brand(clean):
            tokens = list(pseg.cut(clean))
            if len(tokens) == 1:
                # jieba 识别为单一词汇,看词性
                _, flag = tokens[0]
                # ns 地名 / n 普通名词 / v 动词 / a 形容词 → 行业泛词,不作品牌别名
                # nz 专名 / nr 人名 / nt 机构 / eng 英文 → 保留(品牌特征词)
                if flag not in ("nz", "nr", "nt", "eng"):
                    continue
            # len(tokens) >= 2:多 token(如"纯甄"被拆成 2 单字)或组合词 → 保留
        filtered.append(clean)

    # 去重，按长度降序排列（长变体先匹配，避免短变体误匹配）
    result = list(dict.fromkeys(filtered))
    # 确保原始品牌名在最前面
    if brand_name in result:
        result.remove(brand_name)
    result.insert(0, brand_name)

    print(f"[Brand Variants] {brand_name} -> {result}")
    return result


def _select_core_brand(brand_variants: list[str], full_name: str) -> str:
    """
    选择最具辨识度的品牌核心名用于 LLM 验证。

    策略：
    1. 过滤掉纯通用词组成的变体（如"科技有限公司"、"智联科技"）
    2. 过滤掉以城市/地区开头、以通用后缀结尾的变体
    3. 在剩余变体中优先选取 3-6 字的中等长度变体
    """
    # 通用词：不应作为品牌核心名的组成部分
    GENERIC_PARTS = {
        "有限", "股份", "责任", "公司", "集团", "中国", "中华", "国际",
        "有限公司", "股份有限公司", "有限责任公司", "科技有限公司",
        "技术有限公司", "网络科技", "信息科技", "信息技术",
        "科技", "技术", "网络", "信息", "数据", "智能", "电子", "软件",
        "互联网", "地产", "实业", "控股", "投资", "建筑", "工程",
        "贸易", "商贸", "服务", "咨询", "设计", "传媒", "广告",
        "省", "市", "区", "县",
    }

    def _is_generic(variant: str) -> bool:
        """判断变体是否由纯通用词组成"""
        if variant in GENERIC_PARTS:
            return True
        # 检查变体是否以通用后缀结尾且去掉后缀后过短
        for suffix in ["有限公司", "科技", "技术", "公司", "集团"]:
            if variant.endswith(suffix):
                core = variant[:-len(suffix)]
                if len(core) <= 1 or core in GENERIC_PARTS:
                    return True
        return False

    # 过滤：排除全名、过短变体、纯通用变体
    candidates = [
        v for v in brand_variants
        if v != full_name and len(v) >= 3 and not _is_generic(v)
    ]

    if not candidates:
        # 如果全部被过滤，退回到非通用的较长变体
        fallback = [v for v in brand_variants if v != full_name and len(v) >= 3]
        if fallback:
            return max(fallback, key=len)
        return full_name

    # 理想范围：3-6 字（足够具体，不过长）
    ideal = [v for v in candidates if 3 <= len(v) <= 6]
    if ideal:
        # 在理想范围内选最长的，更有辨识度
        return max(ideal, key=len)

    # 没有理想范围的，选最接近 4 字长度的
    return min(candidates, key=lambda v: abs(len(v) - 4))


async def _extract_mentioned_brands_llm(
    text: str, max_brands: int = 8, *, status_sink: dict | None = None
) -> list[str]:
    """模块级 mentioned_brands 抽取(给 _analyze_visibility 复用)

    对应原 query_single 内 nested extract_companies_with_llm 函数 · 提到模块级
    用 deepseek-chat 一次性抽 AI 回复里被推荐/提及的所有公司名

    [P1-7 · 2026-07-26] 旧版所有失败路径都是静默 ``return []``：没配 key、抽取
    接口非 200、回答太短、JSON 解析失败，全都长得像"这个行业没有竞品"。结果
    诊断报告的竞争格局只剩本品牌一条，客户以为自己没有对手。
    现在把失败原因写进 ``status_sink['mentioned_brands_status']``，展示层据此说明
    "本次没能采集到同行名单 + 原因 + 下一步"，而不是伪造一个空名单当结论。
    """

    def _mark(status: str) -> None:
        if status_sink is not None:
            status_sink["mentioned_brands_status"] = status

    if not text or len(text.strip()) < 50:
        _mark("answer_too_short")
        return []
    prompt = f"""请从以下AI回答中提取被推荐或提及的服务商/公司名称。

要求:
1. 只提取具体的公司/品牌名称(尽量给完整全称 · 如"广东法制盛邦(深圳)律师事务所")
2. 不要提取平台名称(TikTok/抖音/小红书/百度)
3. 不要提取行业描述("代运营公司"/"出海服务商")
4. 包括但不限于推荐列表、对比、举例中提到的公司名
5. 至少给出 {max_brands} 个候选(若有这么多)

AI回答:
{text[:1200]}

请用 JSON 数组返回公司全称列表,例如:["广东法制盛邦(深圳)律师事务所", "北京市盈科(深圳)律师事务所"]
没有具体公司则返回:[]"""
    api_key = DEEPSEEK_CONFIG.get("api_key")
    base_url = DEEPSEEK_CONFIG.get("base_url", "https://api.deepseek.com/v1")
    if not api_key:
        _mark("extractor_not_configured")
        return []
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await _tracked_post(
                client,
                f"{base_url}/chat/completions",
                platform="deepseek",
                model="deepseek-chat",
                metadata={"engine": "mentioned_brands_extract"},
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "deepseek-chat",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 400,
                    "temperature": 0.1,
                },
            )
        if resp.status_code != 200:
            _mark("extractor_error")
            return []
        content = resp.json()["choices"][0]["message"]["content"]
        import re as _re_extract
        m = _re_extract.search(r"\[.*?\]", content, _re_extract.DOTALL)
        if not m:
            _mark("extractor_unparsable")
            return []
        arr = json.loads(m.group())
        brands = [c.strip() for c in arr if isinstance(c, str) and len(c.strip()) >= 2][:max_brands]
        # 空数组是**合法结论**(这条回答里真的没点名任何公司),和"抽取失败"分开记。
        _mark("ok" if brands else "no_brands_in_answer")
        return brands
    except Exception as e:
        print(f"[mentioned_brands extract] 失败: {e}")
        _mark("extractor_error")
        return []


async def _deepseek_v4_flash_verify_brand(
    ai_response: str,
    brand_full_name: str,
    mentioned_brands: list[str] | None = None,
    *,
    brand_id: int | None = None,
) -> str:
    """Compatibility wrapper around the single structured identity resolver.

    ``mentioned_brands`` is accepted for older callers but never used to broaden
    trusted identity.  The return value is the explicit YES/NO/UNKNOWN string.
    """
    from services.brand_identity_resolver import BrandIdentityResolver

    resolver = BrandIdentityResolver.for_brand(
        brand_id,
        fallback_name=brand_full_name,
    )
    return (await resolver.resolve(ai_response)).verdict.value


def _normalize_brand_name(s: str) -> str:
    """Compatibility export; bracket content is preserved, never discarded."""
    from services.brand_identity_resolver import normalize_brand_name

    return normalize_brand_name(s)


def _exact_or_strict_match(brand_full_name: str, candidates: list[str]) -> tuple[bool, str]:
    """严苛精确匹配:normalize 后全称相等 / 一方包另一方且短的一方>=6 字
    防止 2-4 字短简称误中(如"盛邦"/"广东法制"/"法制")· 但允许去括号变体匹配

    返回 (matched, matched_text)
    """
    full_raw = (brand_full_name or "").strip()
    full_norm = _normalize_brand_name(full_raw)
    if not full_norm:
        return False, ""
    for c in candidates or []:
        if not c:
            continue
        c_clean = c.strip()
        if not c_clean:
            continue
        c_norm = _normalize_brand_name(c_clean)
        if not c_norm:
            continue
        # 完全相等(normalize 后)
        if c_norm == full_norm:
            return True, c_clean
        # 一方完全包另一方(normalize 后)· 短的一方至少 6 字(原始长度 · 不算 normalize)
        if c_norm in full_norm or full_norm in c_norm:
            shorter = min(len(c_norm), len(full_norm))
            if shorter >= 6:
                return True, c_clean
    return False, ""


async def _call_analyze_visibility(
    ai_response: str,
    query: str,
    check_brand: str,
    engine: str,
    brand_id: int | None,
    brand_display_names: list[str] | None,
) -> dict:
    """Keep the legacy internal call shape when no persisted identity was supplied."""
    if brand_id is None and not brand_display_names:
        return await _analyze_visibility(ai_response, query, check_brand, engine)
    return await _analyze_visibility(
        ai_response,
        query,
        check_brand,
        engine,
        brand_id=brand_id,
        brand_display_names=brand_display_names,
    )


async def _analyze_visibility(
    ai_response: str, query: str, check_brand: str, engine: str,
    mentioned_brands: list[str] | None = None,
    *,
    brand_id: int | None = None,
    brand_display_names: list[str] | None = None,
) -> dict:
    """Resolve one answer through the shared BrandIdentityResolver.

    ``mentioned_brands`` is retained only for competitor co-occurrence display;
    it can no longer broaden or decide the target-brand verdict.
    """
    from services.brand_identity_resolver import (
        BrandIdentityResolver,
        BrandVerdict,
        load_brand_identity,
    )

    visibility_result = {
        "engine": engine,
        "query": query,
        "response": ai_response,
        "brand_detected": False,
        "brand_mentioned_count": 0,
        "brand_position": None,
        "is_recommended": False,
        "brand_variants_found": [],  # 保留字段(向后兼容)· 新版填 mentioned_brands
        "mentioned_brands_inline": [],  # 新增 · 给 query_single 复用避免重复 extract
        "brand_verdict": BrandVerdict.NO.value,
    }

    if not check_brand or not ai_response or not ai_response.strip():
        return visibility_result

    retryable_reasons = {
        "identity_load_failed",
        "provider_not_sent",
    }
    decision = None
    identity_attempts = 0
    for attempt in range(2):
        identity_attempts = attempt + 1
        identity = await asyncio.to_thread(
            load_brand_identity,
            brand_id,
            fallback_name=check_brand,
            fallback_display_names=brand_display_names,
        )
        decision = await BrandIdentityResolver(identity).resolve(ai_response)
        if (
            decision.verdict is not BrandVerdict.UNKNOWN
            or decision.reason not in retryable_reasons
            or attempt == 1
        ):
            break
        # Re-adjudicate the same answer once. Never repeat the platform query.
        await asyncio.sleep(0)

    assert decision is not None
    visibility_result["brand_detection_attempts"] = identity_attempts
    visibility_result["identity_retry_count"] = max(0, identity_attempts - 1)
    visibility_result["brand_verdict"] = decision.verdict.value
    visibility_result["detection_method"] = decision.method
    visibility_result["detection_reason"] = decision.reason
    if decision.matched_alias:
        visibility_result["matched_text"] = decision.matched_alias
    if decision.matched_start is not None:
        visibility_result["matched_start"] = decision.matched_start
    if decision.matched_end is not None:
        visibility_result["matched_end"] = decision.matched_end
    if decision.matched_alias:
        visibility_result["identity_candidates"] = [decision.matched_alias]
    if decision.evidence_snippet:
        visibility_result["identity_evidence_snippet"] = decision.evidence_snippet

    if decision.verdict is BrandVerdict.UNKNOWN:
        # Existing diagnosis/monitoring aggregation already excludes engine_error
        # rows and routes the enclosing run through retry/refund semantics.
        visibility_result.update({
            "engine_error": True,
            "brand_detection_unknown": True,
            "error_code": (
                "brand_identity_data_unavailable"
                if decision.reason == "identity_load_failed"
                else (
                    "brand_identity_retry_exhausted"
                    if decision.reason == "provider_not_sent" and identity_attempts >= 2
                    else "brand_identity_unresolved"
                )
            ),
            "answer_summary": (
                "品牌资料暂时无法读取，请稍后重试"
                if decision.reason in retryable_reasons
                else "品牌名称需要确认"
            ),
        })
        if decision.reason in retryable_reasons and identity_attempts >= 2:
            visibility_result["brand_detection_retry_exhausted"] = True
    elif decision.verdict is BrandVerdict.YES:
        visibility_result["brand_detected"] = True
        visibility_result["brand_mentioned_count"] = 1

    # Extract competitor names once in ai_tester. PlatformAdapter must only reuse
    # this field and must never launch another extraction/verifier request.
    if mentioned_brands is None and decision.verdict is not BrandVerdict.UNKNOWN:
        mentioned_brands = await _extract_mentioned_brands_llm(
            ai_response, status_sink=visibility_result
        )
    elif mentioned_brands is not None:
        visibility_result["mentioned_brands_status"] = "reused_upstream"
    else:
        # UNKNOWN 身份不跑抽取(省一次调用),但要说明为什么没名单
        visibility_result["mentioned_brands_status"] = "identity_unresolved"
    visibility_result["mentioned_brands_inline"] = mentioned_brands or []
    visibility_result["brand_variants_found"] = mentioned_brands or []  # 兼容旧字段命名

    # ─── [WO 2026-08-06 §1] 近失(疑似同品牌变体)候选落库 ───
    #
    # 🔴 只在**没判命中**时算。命中格已经有 matched_text,再挂候选只会制造歧义。
    # 🔴 近失**不改 verdict、不改 brand_detected、不改分母** —— 它只是给待确认
    #    卡片一个可点的候选名(WO §1.3:未确认前不许直接算提到)。
    #
    # 两个来源缺一不可(诊断 561 实证):
    #   · UNKNOWN/invalid_matched_text 的 16 格 **没有** mentioned_brands
    #     (UNKNOWN 跳过抽取省一次调用),唯一线索是复核层被打回的 near_miss_alias;
    #   · NO 的 7 格 **没有** near_miss_alias(复核层自己判的 NO),
    #     线索在 mentioned_brands 名单里(实测每格 5 个,「阿强龙虾」在列)。
    if not visibility_result["brand_detected"]:
        try:
            from services.brand_name_near_miss import near_miss_candidates_for_identity

            near_misses = near_miss_candidates_for_identity(
                identity,
                [
                    *( [decision.near_miss_alias] if decision.near_miss_alias else [] ),
                    *(mentioned_brands or []),
                ],
            )
            if near_misses:
                visibility_result["near_miss_candidates"] = [
                    item.as_dict() for item in near_misses
                ]
                # 待确认卡片读的是 identity_candidates(cell_candidates 的第一顺位),
                # 这里补进去,代理才有按钮可点。matched_text **不动**(语义分离)。
                existing = list(visibility_result.get("identity_candidates") or [])
                for item in near_misses:
                    if item.display not in existing:
                        existing.append(item.display)
                visibility_result["identity_candidates"] = existing
        except Exception as exc:  # 近失是增益,失败绝不拖垮采集
            print(f"[near_miss] 计算失败({type(exc).__name__}) → 跳过")

    # ─── 后置处理:被检出时计算 position / 推荐位 / sentiment ───
    if visibility_result["brand_detected"]:
        # Position is resolved by the SSOT against the original answer.  Do not
        # search the canonical name again after a verified one-character typo.
        first_position = decision.matched_start
        visibility_result["brand_position"] = first_position
        if first_position is not None and first_position >= 0:
            line_index = ai_response.count("\n", 0, first_position)
            visibility_result["is_recommended"] = line_index < 5
        # 情感分析(旧 _analyze_sentiment 用 brand_variants 列表 · 这里传 [matched_text or check_brand])
        try:
            sentiment = _analyze_sentiment(ai_response, [decision.matched_alias or check_brand])
            visibility_result["sentiment_score"] = sentiment["score"]
            visibility_result["sentiment_label"] = sentiment["label"]
            visibility_result["negative_mentions"] = sentiment["negative_count"]
            visibility_result["positive_mentions"] = sentiment["positive_count"]
        except Exception as e:
            print(f"[sentiment] 失败: {e}")

    # [P0-3 · 2026-07-26] 落盘 target_outcome（推荐档位）。
    #
    # 生产实证：诊断结果只带 brand_detected + is_recommended(=命中位置在前 5 行)，
    # 没有任何"推荐/仅提到"的真实判定，导致报告推荐率恒 0% 且"仅提到 / 推荐"
    # 两个标签里后者永远取不到值。这里复用统一观测 SSOT 的确定性分类器
    # (services/geo_observation/entity_review.classify_outcome)，采集时就把档位
    # 算出来写进结果，报告层不再靠启发式反推。零额外 provider 调用。
    visibility_result["target_outcome"] = _resolve_target_outcome(
        visibility_result, ai_response
    )

    return visibility_result


def _resolve_target_outcome(visibility_result: dict, ai_response: str) -> str:
    """把 resolver 五态 + 回答正文映射成统一观测 target_outcome（零 provider 调用）。

    硬轴(engine_error / entity_ambiguous / not_mentioned)由 classify_outcome 保证；
    推荐档位用其确定性基线。判定失败时回落 ``entity_ambiguous`` —— 绝不当"未提到"，
    否则识别失败会被算成品牌真的没被提（SSOT §10.2：PENDING/UNKNOWN 不等于 0）。
    """
    try:
        from services.geo_observation.contracts import EntityState, ResponseStatus
        from services.geo_observation.entity_review import classify_outcome

        if visibility_result.get("engine_error") or visibility_result.get("brand_detection_unknown"):
            return "entity_ambiguous"
        if not (ai_response or "").strip():
            return "engine_error"
        entity_state = (
            EntityState.confirmed_mention
            if visibility_result.get("brand_detected")
            else EntityState.confirmed_non_mention
        )
        return classify_outcome(
            ResponseStatus.answered,
            entity_state,
            bool(visibility_result.get("brand_detected")),
            ai_response,
        ).value
    except Exception as exc:  # 分类器不可用不得拖垮采集
        print(f"[target_outcome] 判定失败({type(exc).__name__}) → entity_ambiguous")
        return "entity_ambiguous"


# ─── 旧 _analyze_visibility 已废弃 · 下面是历史保留函数 ───
async def _analyze_visibility_DEPRECATED(
    ai_response: str, query: str, check_brand: str, engine: str
) -> dict:
    """[DEPRECATED 2026-05-09 · CTO-15.23] 旧硬编码版本 · 不再使用 · 保留代码做实证对照

    旧版假阳性率 78%(广东法制盛邦律师事务所 case · 23 detected / 5 真命中 / 18 假阳性)
    新版用 mentioned_brands + deepseek-v4-flash 两步替代
    """
    visibility_result = {
        "engine": engine,
        "query": query,
        "response": ai_response,
        "brand_detected": False,
        "brand_mentioned_count": 0,
        "brand_position": None,
        "is_recommended": False,
        "brand_variants_found": [],  # 记录匹配到的变体
    }

    if check_brand:
        response_lower = ai_response.lower()
        brand_lower = check_brand.lower()

        # 生成品牌变体列表（支持模糊匹配）
        brand_variants = _generate_brand_variants(check_brand)

        # 检测所有变体
        total_mentions = 0
        strong_mentions = 0
        first_position = None
        variants_found = []

        for variant in brand_variants:
            variant_lower = variant.lower()
            count = response_lower.count(variant_lower)
            if count > 0:
                # ⚠️ 对 ≤2 字的中文变体做额外验证：
                # 检查是否只是其他公司名的子串（如"智联"出现在"智联招聘"中）
                # [CTO-15.23 2026-05-05] P0 \u4fee\u5355 token \u884c\u4e1a\u8bcd\u5f3a\u5339\u914d\u5047\u9633\u6027
                # \u8001\u677f case "\u6df1\u5733\u5fc3\u60a6\u745c\u4f3d\u666e\u62c9\u63d0" \u2192 "\u666e\u62c9\u63d0"(3\u5b57 jieba \u8bef\u8bc6 nr)\u88ab\u5f53\u72ec\u7acb\u53d8\u4f53
                # \u2192 response \u542b 5 \u6b21"\u666e\u62c9\u63d0" \u5f3a\u5339\u914d \u22653 \u2192 L1701 \u9ad8\u7f6e\u4fe1\u5ea6\u76f4\u63a5\u653e\u884c(\u865a\u9ad8)
                # \u4fee\u6cd5:\u5f31\u5339\u914d\u5ef6\u4f38\u5230 3-4 \u5b57\u5355 jieba token(\u884c\u4e1a\u6cdb\u8bcd\u5acc\u7591)\u00b7 \u5f3a\u8feb\u8d70 LLM verify
                # [CTO-15.23 2026-05-05 v2] P0 \u5355+\u591a token \u5168\u884c\u4e1a\u6cdb\u8bcd\u5047\u9633\u4fee
                # f55e8bc3 \u4fee\u4e86\u5355 token "\u666e\u62c9\u63d0" \u4f46\u6f0f\u591a token:
                #   "\u745c\u4f3d\u666e\u62c9\u63d0" 4 \u5b57 2 token \u5168\u884c\u4e1a\u6cdb\u8bcd \u00b7 response \u542b"\u60a6\u4eab\u745c\u4f3d\u666e\u62c9\u63d0" \u2192 \u5047\u547d\u4e2d
                # \u4fee\u6cd5:\u6269\u5c55\u5f31\u5339\u914d\u5230"\u591a token \u5168\u884c\u4e1a\u6cdb\u8bcd" \u00b7 \u5f3a\u8feb\u8d70 LLM verify
                try:
                    import jieba.posseg as _pseg_v
                    _tokens_pairs = list(_pseg_v.cut(variant))
                    _token_words = [t.word for t in _tokens_pairs]
                    _is_industry_polluted = (
                        any('\u4e00' <= c <= '\u9fff' for c in variant)
                        and (
                            # case 1:\u5355 token 3-4 \u5b57(\u884c\u4e1a\u6cdb\u8bcd\u5acc\u7591 \u00b7 \u542b jieba \u8bef\u8bc6 nr)
                            (len(_token_words) == 1 and 3 <= len(variant) <= 4)
                            # case 2:\u591a token + \u6240\u6709 token \u90fd\u662f\u884c\u4e1a\u6cdb\u8bcd(\u8986\u76d6"\u745c\u4f3d\u666e\u62c9\u63d0")
                            or (len(_token_words) >= 2 and all(t in _INDUSTRY_GENERIC_TOKENS for t in _token_words))
                        )
                    )
                except Exception:
                    _is_industry_polluted = False
                if (len(variant) <= 2 and any('\u4e00' <= c <= '\u9fff' for c in variant)) or _is_industry_polluted:
                    # 检查每次出现是否被更长的已知上下文包裹
                    # 简单策略：如果有 ≥3 字的变体也匹配到了，就跳过短变体（避免重复计数）
                    longer_matched = any(
                        variant_lower in lv.lower() and len(lv) > len(variant)
                        for lv in brand_variants
                        if response_lower.count(lv.lower()) > 0
                    )
                    if longer_matched:
                        continue  # 已被更长变体覆盖，跳过
                    # 否则记录但标记为弱匹配
                    variants_found.append(f"{variant}({count},弱)")
                    # 弱匹配只算 0.5 权重（不足以单独触发高置信度检出）
                    total_mentions += max(1, count // 2)
                else:
                    total_mentions += count
                    strong_mentions += count
                    variants_found.append(f"{variant}({count})")
                if first_position is None:
                    first_position = response_lower.find(variant_lower)

        # 初步判断：字符串匹配
        string_match_detected = total_mentions > 0
        visibility_result["string_match_detected"] = string_match_detected
        visibility_result["brand_mentioned_count"] = total_mentions
        visibility_result["brand_variants_found"] = variants_found

        # ===== LLM验证层 =====
        if string_match_detected:
            # 排除问题文本本身中的品牌提及（防止AI只是复读问题）
            query_lower = query.lower()
            query_brand_mentions = sum(
                query_lower.count(v.lower()) for v in brand_variants
            )
            net_mentions = total_mentions - query_brand_mentions
            strong_query_mentions = sum(
                query_lower.count(v.lower())
                for v in brand_variants
                if v and len(v) >= 3
            )
            strong_net_mentions = max(0, strong_mentions - strong_query_mentions)

            # 高置信度匹配：回复正文中强品牌变体被提及≥3次，才直接判定为检出。
            # 弱匹配(尤其2字地名/泛词)不得绕过 LLM，否则会把"璧山唐卡"误判成"璧山万家"。
            if strong_net_mentions >= 3:
                visibility_result["brand_detected"] = True
                visibility_result["llm_verified"] = True
                visibility_result["detection_method"] = "high_confidence_string_match"
                visibility_result["brand_mentioned_count"] = strong_net_mentions
                print(
                    f"[高置信度] ✅ {check_brand} 在回复正文中强匹配出现{strong_net_mentions}次，直接判定检出"
                )
            else:
                # 低频匹配：用LLM验证，传入有辨识度的品牌核心名
                # 避免使用过短的变体（如"深工"、"智联"）导致误判
                core_brand = _select_core_brand(brand_variants, check_brand)
                llm_verified = await _llm_verify_brand_mention(
                    ai_response=ai_response[:1500],
                    brand_name=core_brand,
                    full_company_name=check_brand,
                    matched_variants=variants_found,
                )
                visibility_result["llm_verified"] = llm_verified
                visibility_result["brand_detected"] = llm_verified
                visibility_result["detection_method"] = "llm_verified"

                if not llm_verified:
                    print(
                        f"[LLM验证] ❌ 字符串匹配到 {variants_found}(净{net_mentions}次)，但LLM判定非品牌推荐"
                    )
                else:
                    print(f"[LLM验证] ✅ 确认 {core_brand} 被真正推荐")
        else:
            # 🔑 关键修复：字符串没匹配到时，仍用LLM做兜底检测
            # 解决品牌全称、子公司名等变体无法被字符串匹配覆盖的问题
            if len(ai_response.strip()) > 50:  # 回复内容足够长才值得检测
                # 兜底检测用较长的核心名，避免太短导致误判
                core_brand = _select_core_brand(brand_variants, check_brand)
                llm_verified = await _llm_verify_brand_mention(
                    ai_response=ai_response[:1500],
                    brand_name=core_brand,
                    full_company_name=check_brand,
                    matched_variants=[],
                )
                visibility_result["llm_verified"] = llm_verified
                visibility_result["brand_detected"] = llm_verified
                visibility_result["detection_method"] = "llm_fallback"

                if llm_verified:
                    print(
                        f"[LLM兜底] ✅ 字符串未命中，但LLM确认 {check_brand} 被提及（可能是全称/子公司）"
                    )
                    visibility_result["brand_mentioned_count"] = 1  # 至少被提到1次
                else:
                    print(f"[LLM兜底] ❌ 确认 {check_brand} 未被提及")
            else:
                visibility_result["brand_detected"] = False
                visibility_result["llm_verified"] = None

        # ===== Flash LLM 安全网 =====
        # 当常规检测判定"未检出"时，用独立的 Flash LLM 做最终复核
        # 防止因变体生成缺陷、竞态条件等导致的漏检
        if not visibility_result["brand_detected"] and len(ai_response.strip()) > 100:
            flash_result = await _flash_llm_safety_net(
                ai_response=ai_response[:2000],
                brand_name=check_brand,
                brand_variants=brand_variants[:5],  # 传入前5个变体供参考
            )
            if flash_result:
                visibility_result["brand_detected"] = True
                visibility_result["detection_method"] = "flash_safety_net"
                visibility_result["brand_mentioned_count"] = max(
                    visibility_result["brand_mentioned_count"], 1
                )
                print(
                    f"[Flash安全网] ✅ 常规检测未命中，但Flash LLM确认 {check_brand} 被推荐/提及"
                )

        if visibility_result["brand_detected"]:
            visibility_result["brand_position"] = first_position
            lines = ai_response.split("\n")
            for i, line in enumerate(lines[:10]):
                if brand_lower in line.lower():
                    visibility_result["is_recommended"] = i < 5
                    break

            # 舆情分析：检测正面/负面情感
            sentiment = _analyze_sentiment(ai_response, brand_variants)
            visibility_result["sentiment_score"] = sentiment["score"]  # -1.0 到 1.0
            visibility_result["sentiment_label"] = sentiment[
                "label"
            ]  # positive/neutral/negative
            visibility_result["negative_mentions"] = sentiment["negative_count"]
            visibility_result["positive_mentions"] = sentiment["positive_count"]

    return visibility_result


# ===============================
# LLM验证层 - 低成本模型验证品牌推荐
# ===============================
async def _llm_verify_brand_mention(
    ai_response: str, brand_name: str, matched_variants: list[str],
    full_company_name: str = "",
) -> bool:
    """
    使用LLM验证品牌是否被真正提及/推荐（异步版本，避免阻塞事件循环）

    作用：过滤误检测（如只是在问题中重复了品牌名）

    Args:
        ai_response: AI的回复内容
        brand_name: 品牌核心名（用于检测）
        matched_variants: 字符串匹配到的变体列表
        full_company_name: 完整公司名（提供上下文，帮助LLM区分同名公司）

    Returns:
        bool: True=确认品牌被提及, False=误检测
    """
    import os

    # 构建品牌标识：核心名 + 全称（如果不同）
    if full_company_name and full_company_name != brand_name:
        brand_label = f"{brand_name}（全称：{full_company_name}）"
    else:
        brand_label = brand_name

    # [CTO-15.23 2026-05-11 P0-4] 把 step 1 已识别变体清单注入 prompt 作为客观事实
    # 老板报"QZQZ 美学定制" step 1 命中 + step 2 v4-flash 否定 → 假阴性 → 评分偏低
    # 真因:原 prompt 让 LLM "严格全称匹配" · LLM 只看 AI 文本 · 忽略 step 1 字符串匹配结果
    # 修法:告诉 LLM step 1 客观匹配到的变体 · LLM 只在 "推荐 vs 仅引用问题"二选一
    variants_hint = ""
    if matched_variants:
        # 去重 + 最多 8 个变体作上下文
        unique_variants = list(dict.fromkeys(matched_variants))[:8]
        variants_hint = f"""
【步骤1 客观字符串匹配结果】
以下品牌变体已在 AI 回复中字符串匹配到(客观事实 · 不可否认):
  {', '.join(unique_variants)}
"""

    # 优化的验证 Prompt - step 1 客观事实 + step 2 LLM 只负责区分"推荐 vs 仅引用"
    verify_prompt = f"""请判断下面的 AI 回复中,是否**推荐或介绍**了 {brand_label} 这个公司/品牌。
{variants_hint}
【AI 回复内容】
{ai_response}

【判断规则】
回答 YES 的情况(只要满足其中一条即可):
1. 回复中出现了 {brand_name} 的**全称、公认简称、或步骤1 已匹配到的任一变体**,并有介绍、描述或推荐
2. {brand_name} 出现在 AI 推荐列表中(列表项 / 编号 / 项目符号)
3. 对 {brand_name} 有业务、产品、服务等方面的实质性描述
4. 步骤 1 字符串匹配到的变体出现在 AI 推荐段落里(非用户问题段落)

回答 NO 的情况:
1. 步骤 1 匹配到的变体**仅在用户问题中重复出现**,后文没有任何相关介绍/推荐
2. 提到了名称完全不同的另一家公司(非任何已匹配变体)
3. 仅泛泛讨论行业/品类,完全没有引用任何匹配变体
4. 🔴 [2026-05-25 P0-A] 同后缀但品牌专有前缀不同的公司一律 NO
   - 即使 step 1 字符串匹配到的变体跟目标公司同行业后缀(电梯/地产/酒店/科技)
   - 只要前缀品牌专有名不一致(晨光富士 vs 江苏富士)· 一律 NO

⚠️ 重要原则:
- **步骤 1 已字符串匹配到变体 = 客观事实** · 不要试图否定该匹配
- 你只需要判断:**这次出现是"被推荐"还是"仅在问题中重复"**
- [2026-05-25 P0-A] 平衡判断 · 同后缀不同前缀必 NO · 防假阳客户付费看错数据
- 简称/英文/异写算 YES(保 QZQZ)· 同后缀不同前缀一律 NO(修富士电梯)

只回答 YES 或 NO 一个词。"""

    # 主LLM：qwen-flash（异步调用）
    dashscope_key = os.getenv("DASHSCOPE_API_KEY")
    if dashscope_key:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await _tracked_post(
                    client,
                    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                    platform="dashscope",
                    model="qwen-flash",
                    metadata={"engine": "recommendation_verify"},
                    headers={
                        "Authorization": f"Bearer {dashscope_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "qwen-flash",
                        "messages": [{"role": "user", "content": verify_prompt}],
                        "max_tokens": 10,
                        "temperature": 0,
                    },
                )
            if response.status_code == 200:
                data = response.json()
                answer = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                    .upper()
                )
                print(f"[LLM验证] qwen-flash回答: {answer}")
                return answer.startswith("YES")
        except Exception as e:
            print(f"[LLM验证] qwen-flash调用失败: {e}")

    # 备用：DeepSeek（异步调用·[failover 2026-06-11] 多 key 失败自动换下一个·单 key=直调向后兼容）
    from services.llm.deepseek_key_pool import has_deepseek_key, adeepseek_call_with_failover
    if has_deepseek_key():
        try:
            async def _verify_do(deepseek_key):
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await _tracked_post(
                        client,
                        "https://api.deepseek.com/chat/completions",
                        platform="deepseek",
                        model="deepseek-chat",
                        metadata={"engine": "recommendation_verify_fallback"},
                        headers={
                            "Authorization": f"Bearer {deepseek_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": "deepseek-chat",
                            "messages": [{"role": "user", "content": verify_prompt}],
                            "max_tokens": 10,
                            "temperature": 0,
                        },
                    )
                if resp.status_code != 200:
                    raise RuntimeError(f"deepseek HTTP {resp.status_code}")  # 触发 failover 换 key
                return resp
            response = await adeepseek_call_with_failover(_verify_do)
            data = response.json()
            answer = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
                .upper()
            )
            print(f"[LLM验证] deepseek回答: {answer}")
            return answer.startswith("YES")
        except Exception as e:
            print(f"[LLM验证] deepseek调用失败: {e}")

    # 如果 LLM 都失败，保守判定为未检出。
    # 监测是给客户看的事实层，误判"检出/达标"比漏判更伤信任。
    # 高频强匹配已在 _analyze_visibility 中提前放行，这里只处理低频/模糊匹配。
    print(f"[LLM验证] LLM调用都失败，保守判定未检出")
    return False


# ===============================
# Flash LLM 安全网 - 最终复核层
# ===============================
async def _flash_llm_safety_net(
    ai_response: str, brand_name: str, brand_variants: list[str]
) -> bool:
    """
    Flash LLM 安全网：当常规检测（字符串匹配+LLM验证）判定"未检出"时，
    用独立的 Flash LLM 做最终复核，防止漏检。

    与 _llm_verify_brand_mention 的区别：
    - 使用更宽松的判断标准（防止漏检，而非防止误检）
    - 同时传入品牌全称和主要变体，增加覆盖面
    - 专注于"是否提到了这个品牌"，而非"是否完全一致"
    """
    import os

    # 构建变体提示
    variants_hint = "、".join(brand_variants[:5]) if brand_variants else brand_name

    prompt = f"""请仔细阅读下面的AI回复，判断是否提到或推荐了以下品牌/公司：
品牌名称：{brand_name}
可能的简称/别名：{variants_hint}

【AI回复内容】
{ai_response}

【判断标准】
回答YES：回复中提到了上述品牌的全称、简称或任何一个别名，并有正面描述、推荐、或列入推荐列表
回答NO：完全没有提到上述品牌的任何名称或别名

只回答YES或NO一个词。"""

    # 使用 qwen-flash（极低延迟、低成本）
    dashscope_key = os.getenv("DASHSCOPE_API_KEY")
    if dashscope_key:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await _tracked_post(
                    client,
                    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                    platform="dashscope",
                    model="qwen-turbo-latest",
                    metadata={"engine": "brand_mention_verify"},
                    headers={
                        "Authorization": f"Bearer {dashscope_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "qwen-turbo-latest",  # 比 qwen-flash 更快
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 10,
                        "temperature": 0,
                    },
                )
            if response.status_code == 200:
                data = response.json()
                answer = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                    .upper()
                )
                print(f"[Flash安全网] qwen-turbo回答: {answer}")
                return answer.startswith("YES")
        except Exception as e:
            print(f"[Flash安全网] qwen-turbo调用失败: {e}")

    return False  # Flash LLM 失败时不做检出（保守策略）


def _analyze_sentiment(text: str, brand_variants: list[str]) -> dict:
    """
    分析文本中品牌提及的情感倾向（舆情分析）

    Returns:
        dict: {score: -1.0~1.0, label: str, positive_count: int, negative_count: int}
    """
    text_lower = text.lower()

    # 负面关键词（与品牌相关的负面评价）
    negative_keywords = [
        # 产品问题
        "质量差",
        "质量问题",
        "容易坏",
        "故障",
        "缺陷",
        "bug",
        "闪退",
        "卡顿",
        "发热",
        "电池不耐用",
        "续航差",
        "充电慢",
        "信号差",
        "掉线",
        "死机",
        # 服务问题
        "售后差",
        "维修贵",
        "客服态度差",
        "不推荐",
        "不建议",
        "踩坑",
        "坑",
        "差评",
        "失望",
        "后悔",
        "被骗",
        "虚假宣传",
        "货不对板",
        # 安全/隐私
        "隐私泄露",
        "安全问题",
        "病毒",
        "恶意软件",
        "骚扰",
        "垃圾",
        # 负面比较
        "不如",
        "比不上",
        "落后",
        "过时",
        "淘汰",
        "低端",
        # 通用负面
        "差",
        "烂",
        "垃圾",
        "坑爹",
        "糟糕",
        "恶心",
        "垃圾",
        "最差",
        "worst",
    ]

    # 正面关键词
    positive_keywords = [
        # 产品优点
        "推荐",
        "首选",
        "最佳",
        "最好",
        "优秀",
        "出色",
        "领先",
        "顶级",
        "旗舰",
        "性价比高",
        "性价比",
        "值得购买",
        "值得入手",
        "真香",
        "良心",
        "好评",
        "满意",
        "惊艳",
        "超值",
        "划算",
        # 性能
        "流畅",
        "稳定",
        "快",
        "强大",
        "高效",
        "省电",
        "续航强",
        "信号好",
        # 服务
        "服务好",
        "售后好",
        "贴心",
        "专业",
        # 设计
        "漂亮",
        "好看",
        "时尚",
        "高级",
        "质感好",
        # 比较
        "比...好",
        "领先",
        "第一",
        "冠军",
        "top",
        "best",
    ]

    # 统计品牌附近的情感词
    positive_count = 0
    negative_count = 0

    # 检查每个品牌变体附近的情感
    for variant in brand_variants:
        variant_lower = variant.lower()
        pos = 0
        while True:
            pos = text_lower.find(variant_lower, pos)
            if pos == -1:
                break

            # 提取品牌前后各100个字符的上下文
            context_start = max(0, pos - 100)
            context_end = min(len(text_lower), pos + len(variant_lower) + 100)
            context = text_lower[context_start:context_end]

            # 检测情感词
            for neg_word in negative_keywords:
                if neg_word in context:
                    negative_count += 1
                    break  # 每个上下文只计算一次负面

            for pos_word in positive_keywords:
                if pos_word in context:
                    positive_count += 1
                    break  # 每个上下文只计算一次正面

            pos += 1

    # 计算情感分数 (-1.0 到 1.0)
    total = positive_count + negative_count
    if total == 0:
        score = 0.0  # 中性
        label = "neutral"
    else:
        score = (positive_count - negative_count) / total
        if score > 0.2:
            label = "positive"
        elif score < -0.2:
            label = "negative"
        else:
            label = "neutral"

    return {
        "score": round(score, 2),
        "label": label,
        "positive_count": positive_count,
        "negative_count": negative_count,
    }


# ===============================
# 批量查询 - 联网搜索版
# ===============================
async def batch_query_ai_engines(
    query: str, check_brand: str = "", engines: list[str] = None
) -> ToolResponse:
    """
    批量查询多个 AI 引擎并检测品牌可见度（联网搜索版）

    Args:
        query (str): 搜索查询语句
        check_brand (str): 要检测的品牌名称
        engines (list[str]): 要查询的引擎列表
            默认值来自 config.ai_engines(统一五引擎)；metaso 仅供显式历史兼容

    Returns:
        ToolResponse: 包含所有引擎结果的综合报告
    """
    if engines is None:
        engines = default_diagnosis_engines()

    results = []

    # 使用并行查询提高效率
    async def query_single_engine(engine: str):
        try:
            if engine == "dashscope":
                result = await query_dashscope_search(query, check_brand)
            elif engine == "kimi":
                result = await query_kimi_search(query, check_brand)
            elif engine == "doubao":
                result = await query_doubao_search(query, check_brand)
            elif engine == "yuanbao":
                result = await query_yuanbao(query, check_brand)
            elif engine == "metaso":
                result = await query_metaso_search(query, check_brand)
            elif engine == "deepseek":
                # 保留DeepSeek作为备用对照组
                result = await query_deepseek(query, check_brand)
            else:
                return {"engine": engine, "error": "Unknown engine"}

            # 解析响应
            response_text = result.content[0]["text"]

            # 检查是否为错误响应
            if response_text.startswith("Error:"):
                return {"engine": engine, "error": response_text}

            result_data = json.loads(response_text)
            return result_data
        except json.JSONDecodeError as e:
            return {
                "engine": engine,
                "error": f"JSON parse error: {e}",
                "raw_response": response_text[:200]
                if "response_text" in locals()
                else "No response",
            }
        except Exception as e:
            return {"engine": engine, "error": str(e)}

    # 并行执行所有引擎查询
    tasks = [query_single_engine(engine) for engine in engines]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 处理异常结果
    processed_results = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            processed_results.append({"engine": engines[i], "error": str(result)})
        else:
            processed_results.append(result)

    # 综合统计
    successful_results = [r for r in processed_results if "error" not in r]
    web_search_results = [
        r for r in successful_results if r.get("web_search_enabled", False)
    ]

    summary = {
        "total_engines": len(processed_results),
        "successful_engines": len(successful_results),
        "web_search_engines": len(web_search_results),
        "brand_detected_count": sum(
            1 for r in successful_results if r.get("brand_detected", False)
        ),
        "total_mentions": sum(
            r.get("brand_mentioned_count", 0) for r in successful_results
        ),
        "recommendation_count": sum(
            1 for r in successful_results if r.get("is_recommended", False)
        ),
        "results": processed_results,
    }

    return ToolResponse(
        content=[{"type": "text", "text": json.dumps(summary, ensure_ascii=False)}]
    )


async def check_longtail_keywords(
    keywords: list[str], check_brand: str, engines: list[str] = None
) -> ToolResponse:
    """
    批量检测长尾关键词在多个AI引擎的品牌可见度（联网搜索版）

    Args:
        keywords (list[str]): 长尾关键词列表
        check_brand (str): 要检测的品牌名称
        engines (list[str]): 要查询的引擎列表，默认使用联网引擎

    Returns:
        ToolResponse: 包含每个关键词检测结果的报告
    """
    if engines is None:
        # 引擎清单单源 config.ai_engines；历史引擎仍可由调用方显式传入。
        engines = default_diagnosis_engines()

    results = []

    for keyword in keywords:
        keyword_result = {"keyword": keyword, "engines": []}

        for engine in engines:
            try:
                if engine == "dashscope":
                    result = await query_dashscope_search(
                        keyword, check_brand, max_tokens=1500
                    )
                elif engine == "kimi":
                    result = await query_kimi_search(
                        keyword, check_brand, max_tokens=1500
                    )
                elif engine == "doubao":
                    result = await query_doubao_search(
                        keyword, check_brand, max_tokens=1500
                    )
                elif engine == "yuanbao":
                    result = await query_yuanbao(
                        keyword, check_brand, max_tokens=1500
                    )
                elif engine == "metaso":
                    result = await query_metaso_search(
                        keyword, check_brand, max_tokens=1500
                    )
                elif engine == "deepseek":
                    result = await query_deepseek(keyword, check_brand, max_tokens=1500)
                else:
                    continue

                result_data = json.loads(result.content[0]["text"])
                keyword_result["engines"].append(result_data)
            except Exception as e:
                keyword_result["engines"].append({"engine": engine, "error": str(e)})

        # 计算该关键词的综合检测结果
        keyword_result["detected_in_any"] = any(
            e.get("brand_detected", False) for e in keyword_result["engines"]
        )
        keyword_result["recommended_in_any"] = any(
            e.get("is_recommended", False) for e in keyword_result["engines"]
        )

        results.append(keyword_result)

    # 统计
    detected_count = sum(1 for r in results if r.get("detected_in_any", False))
    coverage_rate = detected_count / len(keywords) if keywords else 0

    summary = {
        "total_keywords": len(keywords),
        "brand": check_brand,
        "engines_tested": engines,
        "detected_count": detected_count,
        "coverage_rate": round(coverage_rate * 100, 2),
        "recommended_count": sum(
            1 for r in results if r.get("recommended_in_any", False)
        ),
        "keyword_results": results,
    }

    return ToolResponse(
        content=[{"type": "text", "text": json.dumps(summary, ensure_ascii=False)}]
    )


async def detailed_ai_visibility_test(
    questions: list[str],
    check_brand: str,
    industry: str = "",
    engines: list[str] = None,
    custom_brand_variants: list[str] = None,  # [NEW] 客户指定品牌别名
    brand_id: int | None = None,
    observation_source_ref: str | None = None,
    observation_scope: str = "primary",
    owner_user_id: int | None = None,
) -> dict:
    """
    [Phase 12.8] 详细AI可见度测试 - 联网搜索增强版

    Args:
        questions: 测试问题列表
        check_brand: 要检测的品牌名称
        industry: 行业
        engines: AI引擎列表
        custom_brand_variants: 客户指定的对外品牌名，如["奥莱超级会员店", "奥莱换鞋吧"]
    """
    # [NEW] 设置品牌别名覆盖（使用 contextvars，异步任务隔离，不影响并发监测）
    if custom_brand_variants:
        _custom_brand_ctx.set(custom_brand_variants)
        print(f"\n🎯 [AI测试] 使用客户指定品牌别名: {custom_brand_variants}")
    else:
        _custom_brand_ctx.set(None)

    if engines is None:
        # 引擎清单单源 config.ai_engines(统一五引擎)。
        engines = default_diagnosis_engines()

    observation_source_ref = (
        (observation_source_ref or "").strip()
        or f"diagnosis-adhoc:{uuid.uuid4().hex}"
    )
    observation_round_id = f"paid-diagnosis:{observation_source_ref}"

    detail_table = []
    brand_stats = {engine: {"detected": 0, "total": 0} for engine in engines}

    # [性能优化] 并行执行所有AI查询
    async def query_single(question: str, engine: str):
        """单个查询任务"""
        try:
            if engine == "dashscope":
                result = await query_dashscope_search(
                    question, check_brand, max_tokens=1500, brand_id=brand_id,
                    brand_display_names=custom_brand_variants,
                )
            elif engine == "kimi":
                result = await query_kimi_search(
                    question, check_brand, max_tokens=1500, brand_id=brand_id,
                    brand_display_names=custom_brand_variants,
                )
            elif engine == "doubao":
                result = await query_doubao_search(
                    question, check_brand, max_tokens=1500, brand_id=brand_id,
                    brand_display_names=custom_brand_variants,
                )
            elif engine == "yuanbao":
                request_id = "diagnosis-yuanbao:" + hashlib.sha256(
                    f"{observation_source_ref}|{observation_scope}|{question}".encode("utf-8")
                ).hexdigest()[:32]
                result = await query_yuanbao(
                    question,
                    check_brand,
                    max_tokens=1500,
                    brand_id=brand_id,
                    brand_display_names=custom_brand_variants,
                    observation_source_ref=observation_source_ref,
                    observation_round_id=observation_round_id,
                    observation_request_id=request_id,
                    owner_user_id=owner_user_id,
                    industry=industry,
                )
            elif engine == "deepseek":
                # [2026-07-27] DeepSeek 官方原生检索(改前打的是 DashScope,测的是阿里的检索行为)。
                # 诊断与监测共用同一批 query 函数,两侧分派必须一致(工单 §1)。
                result = await query_deepseek_official(
                    question, check_brand, max_tokens=1500, brand_id=brand_id,
                    brand_display_names=custom_brand_variants,
                )
            else:
                return None

            result_text = result.content[0]["text"]
            try:
                result_data = json.loads(result_text)
            except (json.JSONDecodeError, ValueError):
                # 兜底：引擎返回了非JSON文本（如 "Error: ..."）
                return {
                    "question": question,
                    "engine": engine,
                    "answer_summary": f"查询失败: {result_text[:80]}",
                    "mentioned_brands": [],
                    "brand_detected": False,
                    "brand_verdict": "UNKNOWN",
                    "engine_error": True,
                    "web_search_enabled": False,
                    "full_response": result_text,
                    "search_citations": [],  # [Phase1-A] 兜底空数组
                }
            full_response = result_data.get("response", "")

            # 提取回答摘要
            answer_summary = full_response[:150].replace("\n", " ").strip()
            if len(full_response) > 150:
                answer_summary += "..."

            # mentioned_brands SSOT:_analyze_visibility extracts at most once.
            # Missing inline data is an empty list; never launch a second request.
            inline = result_data.get("mentioned_brands_inline")
            mentioned_brands = inline if isinstance(inline, list) else []
            brand_detected = result_data.get("brand_detected", False)
            brand_verdict = result_data.get("brand_verdict") or ("YES" if brand_detected else "NO")
            engine_error = bool(result_data.get("engine_error") or brand_verdict == "UNKNOWN")
            web_search_enabled = result_data.get("web_search_enabled", False)
            # [Phase1-A 2026-06-07] 透传单引擎已采的搜索来源(qwen/doubao 在各自 query_*_search 里 set 过)
            #   此前聚合层丢弃 → 诊断 detail_table 无来源 → 权威背书空。补带不影响 brand_detected/mentioned_brands/full_response。
            search_citations = result_data.get("search_citations") or []

            return {
                "question": question,
                "engine": engine,
                "answer_summary": answer_summary,
                "mentioned_brands": mentioned_brands[:5],
                "brand_detected": brand_detected,
                "brand_verdict": brand_verdict,
                "engine_error": engine_error,
                "web_search_enabled": web_search_enabled,
                "full_response": full_response,
                "search_citations": search_citations,
                "platform_key": result_data.get("platform_key"),
                "surface_key": result_data.get("surface_key"),
                "tested_at": result_data.get("tested_at"),
                # [2026-07-22 板块A] 五态显式化:透传 resolver 判定理由/候选/证据,
                # 让 PENDING_IDENTITY vs PROVIDER_UNKNOWN 不再需要重判即可区分(纯 additive)
                "detection_reason": result_data.get("detection_reason"),
                "detection_method": result_data.get("detection_method"),
                "matched_text": result_data.get("matched_text"),
                "identity_candidates": result_data.get("identity_candidates") or [],
                "identity_evidence_snippet": result_data.get("identity_evidence_snippet"),
                # [P0-3] 推荐档位(采集时判定 · 报告层直接读,不再靠启发式反推)
                "target_outcome": result_data.get("target_outcome"),
                "is_recommended": result_data.get("is_recommended"),
                # [P1-7] 同行名单抽取状态:区分"真的没点名公司"与"抽取失败"
                "mentioned_brands_status": result_data.get("mentioned_brands_status"),
            }
        except Exception as e:
            return {
                "question": question,
                "engine": engine,
                "answer_summary": f"查询失败: {str(e)[:50]}",
                "mentioned_brands": [],
                "brand_detected": False,
                "brand_verdict": "UNKNOWN",
                "engine_error": True,
                "web_search_enabled": False,
                "error": str(e),
                "search_citations": [],  # [Phase1-A] 兜底空数组
            }

    # 创建所有任务并并行执行
    # 🔴 [工单 C-2(c) · Codex 终审 P1-9] 必须是 **Task**,不是裸协程。
    #    裸协程在 `asyncio.wait_for` 超时被取消后**拿不回任何已完成结果** ——
    #    `gather` 整体被 cancel,已经跑完的那几格连同它们的返回值一起消失。
    #    包成 Task 之后,超时那一刻每个 Task 自己知道自己 done 没 done。
    tasks = [
        asyncio.ensure_future(query_single(question, engine))
        for question in questions
        for engine in engines
    ]

    # 并行执行所有查询（8问题×4引擎=32个），带超时保护
    try:
        results_list = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=240,  # 4分钟总超时
        )
        # 将异常转为空结果
        results_list = [
            r if not isinstance(r, Exception) else None
            for r in results_list
        ]
    except asyncio.TimeoutError:
        # ══════════════════════════════════════════════════════════════════
        # 🔴 [工单 C-2(c)] 总超时 = **降级交付**,不是整批丢弃
        # ══════════════════════════════════════════════════════════════════
        # 改动前这里是 `results_list = []`,而紧邻那一行 print 写的是
        # 「使用已完成的结果」—— 文案与行为正好相反:已经成功返回的平台
        # 结果被全部扔掉,`total_tests` 归零。
        #
        # 后果落在**钱**上,不只是报告少几格:
        #   · `total_tests=0` + `total_planned>0` ⇒
        #     `diagnosis_sample_contract.evaluate_sample` 第 ② 档
        #     `zero_usable_result` ⇒ INSUFFICIENT ⇒ 整单退款、报告不交付;
        #   · 而真相可能是 32 格里 30 格已经拿到了答案,只有 2 格卡在超时。
        # 「已交付的部分不整批抹掉」是 §12.1 的原话,也是诊断链降级口径的本体。
        #
        # 所以这里收割**已完成**的那些 Task,让降级交付 + 按履约结算
        # (`billable_ratio = succeeded/planned`)接手;仍在飞的那几格
        # 显式 cancel 止损(否则它们会在后台继续烧钱,而结果没人消费)。
        results_list = []
        for t in tasks:
            if not t.done():
                t.cancel()
                results_list.append(None)
                continue
            try:
                r = t.result()
            except BaseException:                    # noqa: BLE001 —— 取消/异常都算这一格没成
                r = None
            results_list.append(r if not isinstance(r, Exception) else None)
        # [工单 E3-5 · Codex 二审 P2] 这一行数的是**真正留下来的结果**,
        # 不是 `sum(1 for t in tasks if t.done())`。
        #
        # 🔴 旧写法不是"有时会多数几格",是**永远**报 N/N:
        #    `asyncio.wait_for` 超时时会先 cancel 掉被等的 gather 并
        #    **等这次取消落定**再抛 TimeoutError,所以走到这里时每个 task
        #    都已经是终态 —— 而 asyncio 里**被取消的 task 也是 done()**。
        #    于是这条日志结构上永远说不出"降级了几格":显示 6/6、实留 4。
        #    (本仓 test_c2_total_timeout_degrade.py 的 docstring 里已经
        #     用独立小程序实测过这三行:done=[True,True] /
        #     cancelled()=[True,False] / t.cancel()=[False,False]。)
        #
        # 🔴 只改**计数口径**,结算一个字不动:结算走的是
        #    `brand_stats` → total_tests/total_planned/total_failed →
        #    `diagnosis_sample_contract.evaluate_sample`,那条链只统计
        #    `results_list` 里非 None 的格,本来就是对的(二审确认)。
        #    `done_count` 全仓只有这里定义、只有下面这句消费,不出本函数。
        kept_count = sum(1 for r in results_list if r is not None)
        print(
            f"  ⚠️ AI可见度测试总超时(4分钟)，保留已完成的 {kept_count}/{len(tasks)} 格结果，"
            f"其余按未履约计(降级交付)"
        )

    # 整理结果
    question_results = {}
    for r in results_list:
        if r is None:
            continue
        q = r["question"]
        e = r["engine"]

        if q not in question_results:
            question_results[q] = {"question": q, "results": {}}

        question_results[q]["results"][e] = {
            "answer_summary": r["answer_summary"],
            "mentioned_brands": r["mentioned_brands"],
            "brand_detected": r["brand_detected"],
            "brand_verdict": r.get("brand_verdict", "NO"),
            "status": "error" if r.get("engine_error") else "success",
            "web_search_enabled": r.get("web_search_enabled", False),
            "full_response": r.get("full_response", ""),
            "search_citations": r.get("search_citations") or [],  # [Phase1-A] 透传搜索来源(供权威背书聚合读)
            "platform_key": r.get("platform_key"),
            "surface_key": r.get("surface_key"),
            "tested_at": r.get("tested_at"),
            # [2026-07-22 板块A] 五态显式化透传(additive · 旧读取方忽略)
            "detection_reason": r.get("detection_reason"),
            "detection_method": r.get("detection_method"),
            "matched_text": r.get("matched_text"),
            "identity_candidates": r.get("identity_candidates") or [],
            "identity_evidence_snippet": r.get("identity_evidence_snippet"),
            # [WO 2026-08-06 §1] 疑似同品牌变体(待确认线索 · **不是**命中)。
            #   没有这一行,近失就只活在内存里 —— 落库快照里没有,待确认卡片
            #   和报告层都读不到,等于白算。
            "near_miss_candidates": r.get("near_miss_candidates") or [],
        }

        # 统计（跳过API失败的测试）
        answer_text = r.get("answer_summary", "")
        is_api_failure = (
            bool(r.get("engine_error"))
            or r.get("brand_verdict") == "UNKNOWN"
            or "查询失败" in answer_text
            or answer_text.startswith("Error")
        )
        if not is_api_failure:
            brand_stats[e]["total"] += 1
            if r["brand_detected"]:
                brand_stats[e]["detected"] += 1

    # 按原始问题顺序排列
    detail_table = [question_results[q] for q in questions if q in question_results]

    # 生成汇总（仅计算有效测试）
    total_tests = sum(s["total"] for s in brand_stats.values())
    total_detected = sum(s["detected"] for s in brand_stats.values())
    total_planned = len(questions) * len(engines)
    total_failed = total_planned - total_tests

    # [WO 2026-08-06 §1.2-3] 近失汇总 —— 「进件别名补齐」的**实证版**。
    #
    # 工单原意是"品牌只有单一形态时,启动诊断前先造候选别名"。这里实现成
    # **跑完之后从真实答案里收候选**,是一处有意偏离,理由两条(都实测过):
    #   ① 启动前无据可造 —— 只能让 LLM 猜,猜出来的别名是编的,而
    #      `brand_aliases` 被 `db/distillation_db.get_all_brand_aliases()`
    #      **不分 source 全量读取**(蒸馏归一用),写进去等于让一个未经确认的
    #      身份合并悄悄泄漏到另一个子系统 —— 正是"候选先进 pending"要防的事;
    #   ② 答案里的写法是**用户真实会用的**那个(561:23/32 格写「阿强龙虾」),
    #      比任何猜测都准,而且零额外 LLM 调用、零扣费。
    # 单一形态品牌的标记一并带出,报告层要解释"为什么这家更容易被判未提到"。
    near_miss_rollup: dict[str, dict] = {}
    for r in results_list:
        if r is None:
            continue
        for item in (r.get("near_miss_candidates") or []):
            if not isinstance(item, dict):
                continue
            key = str(item.get("display") or "").strip()
            if not key:
                continue
            entry = near_miss_rollup.setdefault(
                key,
                {"display": key, "trusted_form": item.get("trusted_form"), "cells": 0},
            )
            entry["cells"] += 1

    # [NEW] 清除品牌别名覆盖
    _custom_brand_ctx.set(None)

    return {
        "questions": questions,
        "engines": engines,
        "detail_table": detail_table,
        "near_miss_summary": sorted(
            near_miss_rollup.values(), key=lambda item: (-item["cells"], item["display"])
        ),
        "brand_detection_summary": {
            "total_tests": total_tests,  # 有效测试数（排除API失败）
            "total_planned": total_planned,  # 计划测试数
            "total_failed": total_failed,  # API失败数
            "detected_count": total_detected,
            "detection_rate": round(total_detected / total_tests * 100, 1)
            if total_tests > 0
            else 0,
            "by_engine": {
                engine: {
                    "detected": stats["detected"],
                    "total": stats["total"],
                    "rate": round(stats["detected"] / stats["total"] * 100, 1)
                    if stats["total"] > 0
                    else 0,
                }
                for engine, stats in brand_stats.items()
            },
        },
    }
