"""
CTO-15.23 2026-05-09 · 全量 LLM 调用 tracker
统一 contextmanager 包装 httpx/openai client.post · 写 llm_call_log 表

用法 1(async httpx):
    from tools.llm_call_tracker import llm_track
    async with llm_track('monitoring', 'kimi', model='kimi-k2.6',
                         brand_id=B, quote_id=Q, user_id=U) as t:
        response = await client.post(...)
        usage = response.json().get('usage', {})
        t.record(input_tokens=usage.get('prompt_tokens', 0),
                 output_tokens=usage.get('completion_tokens', 0))

用法 2(失败也记):
    async with llm_track('autofill', 'doubao') as t:
        try:
            r = await client.post(...)
            t.record(input_tokens=N, output_tokens=M, success=True)
        except Exception as e:
            t.record(success=False, error_msg=str(e)[:500])
            raise

异常处理:
- tracker 自身异常 · log warning · 不抛(不阻塞主 LLM 调用)
- LLM 调用异常 · finally 段仍写 log · success=False
"""
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from contextvars import ContextVar
from typing import Any, Dict, Optional

logger = logging.getLogger("LLMTracker")


# ============================================================
# 价格表(元/1K tokens · 后端内部成本核算)
#
# 🔴 开源版里的单价全是示例值:部署方按自己的供应商账单填写。
#   - 只给后端 LLM 成本核算 + admin 对账面板用,不上前端;用户扣费走 feature_pricing 表
#   - DeepSeek 官方线空闲时段价 = 高峰价的一半(官方规则),示例值照此填写
# ============================================================
USD_TO_CNY = float(os.getenv("API_COST_USD_TO_CNY", "7.2"))
KIMI_WEB_SEARCH_CALL_CNY = round(0.004 * USD_TO_CNY, 6)
TIKHUB_CALL_CNY = round(0.001 * USD_TO_CNY, 6)
APIMART_IMAGE_CALL_CNY = round(0.01 * USD_TO_CNY, 6)
DASHSCOPE_ASR_AUDIO_SECOND_CNY = 0.0002


def _usd_mtok_to_cny_1k(usd_per_mtok: float) -> float:
    return round(usd_per_mtok * USD_TO_CNY / 1000, 6)

PRICING_TABLE: Dict[tuple, Dict[str, float]] = {
    # --- 阿里云 DashScope ---
    ("dashscope", "qwen3-max"): {"input": 0.0013, "output": 0.0026},
    ("dashscope", "qwen3.7-max"): {"input": 0.0013, "output": 0.0026, "cache_hit": 0.00026},
    ("dashscope", "qwen3.8-max"): {"input": 0.0013, "output": 0.0026, "cache_hit": 0.00026},
    ("dashscope", "qwen3.7-plus"): {"input": 0.0013, "output": 0.0026},
    ("dashscope", "qwen3.6-max-preview"): {"input": 0.0013, "output": 0.0026},
    ("dashscope", "qwen3.6-plus"): {"input": 0.0013, "output": 0.0026},
    ("dashscope", "qwen-plus-latest"): {"input": 0.0013, "output": 0.0026},
    ("dashscope", "qwen-turbo"): {"input": 0.0013, "output": 0.0026},
    ("dashscope", "qwen-turbo-latest"): {"input": 0.0013, "output": 0.0026},
    ("dashscope", "qwen-flash"): {"input": 0.0013, "output": 0.0026},
    ("dashscope", "qwen-max"): {"input": 0.0013, "output": 0.0026},
    ("dashscope", "deepseek-v4-flash"): {"input": 0.0013, "output": 0.0026},
    ("dashscope", "deepseek-v4-pro"): {"input": 0.0013, "output": 0.0026},
    ("dashscope", "qwen3.6-flash"): {"input": 0.0013, "output": 0.0026},
    ("dashscope", "text-embedding-v4"): {"input": 0.0013, "output": 0},
    ("dashscope", "qwen3-rerank"): {"input": 0.0013, "output": 0},
    ("dashscope", "qwen3-vl-rerank"): {"input": 0.0013, "output": 0},
    ("dashscope", "qwen-audio-asr"): {"input": 0, "output": 0, "flat_rate_per_call": 0.013},
    ("dashscope", "qwen3-asr-flash"): {"input": 0, "output": 0, "audio_second": DASHSCOPE_ASR_AUDIO_SECOND_CNY},
    ("dashscope", "qwen3-asr-flash-filetrans"): {"input": 0, "output": 0, "audio_second": DASHSCOPE_ASR_AUDIO_SECOND_CNY},
    # --- DeepSeek 官方直连(2026-05-22 老板对账更新)---
    ("deepseek", "deepseek-chat"): {"input": 0.007, "output": 0.014, "cache_hit": 0.0004},
    ("deepseek", "deepseek-reasoner"): {"input": 0.007, "output": 0.014, "cache_hit": 0.0004},
    ("deepseek", "deepseek-v4-flash"): {"input": 0.007, "output": 0.014, "cache_hit": 0.0004},
    ("deepseek", "deepseek-flash"): {"input": 0.007, "output": 0.014, "cache_hit": 0.0004},
    ("deepseek", "deepseek-v4-pro"): {"input": 0.007, "output": 0.014, "cache_hit": 0.0004},
    ("deepseek_official", "deepseek-flash"): {"input": 0.007, "output": 0.014, "cache_hit": 0.0004},
    ("deepseek_official", "deepseek-v4-flash"): {"input": 0.007, "output": 0.014, "cache_hit": 0.0004},
    ("deepseek_official", "deepseek-v4-pro"): {"input": 0.007, "output": 0.014, "cache_hit": 0.0004},
    # --- Kimi (Moonshot) ---
    ("kimi", "kimi-k2.5"): {"input": round(0.60 * USD_TO_CNY / 1000, 6), "output": round(3.00 * USD_TO_CNY / 1000, 6)},
    ("kimi", "kimi-k2.6"): {"input": 0.0013, "output": 0.0026, "cache_hit": 0.00026},
    # --- 豆包 (火山方舟 ARK) ---
    ("doubao", "doubao-seed-2-0-pro-260215"): {"input": 0.0013, "output": 0.0026},
    ("doubao", "doubao-seed-1-6-251015"): {"input": 0.0013, "output": 0.0026},
    ("doubao", "ai_search"): {"input": 0, "output": 0.1, "flat_rate": True},
    ("doubao_search", "web_search"): {"input": 0, "output": 0, "flat_rate_per_call": 0.013},
    # --- Tencent TokenHub / Yuanbao Hy3 ---
    ("tencent_tokenhub", "hy3"): {"input": 0.0013, "output": 0.0026, "cache_hit": 0.00026},
    ("tencent_tokenhub", "hy3-preview"): {"input": 0.0013, "output": 0.0026, "cache_hit": 0.00026},
    # --- SiliconFlow ---
    # --- OpenRouter ---
    ("openrouter", "anthropic/claude-sonnet-4.6"): {"input": _usd_mtok_to_cny_1k(3.00), "output": _usd_mtok_to_cny_1k(15.00)},
    ("openrouter", "anthropic/claude-sonnet-4.5"): {"input": _usd_mtok_to_cny_1k(3.00), "output": _usd_mtok_to_cny_1k(15.00)},
    ("openrouter", "anthropic/claude-opus-4.5"): {"input": _usd_mtok_to_cny_1k(5.00), "output": _usd_mtok_to_cny_1k(25.00)},
    ("openrouter", "google/gemini-3.1-pro-preview"): {"input": _usd_mtok_to_cny_1k(2.00), "output": _usd_mtok_to_cny_1k(12.00)},
    ("openrouter", "google/gemini-3.1-pro-preview-customtools"): {"input": _usd_mtok_to_cny_1k(2.00), "output": _usd_mtok_to_cny_1k(12.00)},
    ("openrouter", "google/gemini-3-pro-image-preview"): {"input": _usd_mtok_to_cny_1k(2.00), "output": _usd_mtok_to_cny_1k(12.00)},
    ("openrouter", "google/gemini-3-flash-preview"): {"input": _usd_mtok_to_cny_1k(0.50), "output": _usd_mtok_to_cny_1k(3.00)},
    ("openrouter", "google/gemini-3-pro"): {"input": _usd_mtok_to_cny_1k(2.00), "output": _usd_mtok_to_cny_1k(12.00)},
    ("openrouter", "google/gemini-3-flash"): {"input": _usd_mtok_to_cny_1k(0.50), "output": _usd_mtok_to_cny_1k(3.00)},
    ("openrouter", "openai/gpt-5.1"): {"input": _usd_mtok_to_cny_1k(1.25), "output": _usd_mtok_to_cny_1k(10.00)},
    ("openrouter", "openai/gpt-5.1-high"): {"input": _usd_mtok_to_cny_1k(1.25), "output": _usd_mtok_to_cny_1k(10.00)},
    # --- apimart 图片生成 ---
    ("apimart", "gpt-image-2"): {"input": 0, "output": 0, "flat_rate_per_call": APIMART_IMAGE_CALL_CNY},
    # --- Metaso ---
    ("metaso", "search"): {"input": 0, "output": 0, "flat_rate_per_call": 0.013},
    ("metaso", "reader"): {"input": 0, "output": 0, "flat_rate_per_call": 0.013},
    ("metaso", "chat"): {"input": 0, "output": 0, "flat_rate_per_call": 0.013},
    # --- Data APIs ---
    ("5118", "longtail"): {"input": 0, "output": 0, "flat_rate_per_call": 0.013},
    ("5118", "search_volume"): {"input": 0, "output": 0, "flat_rate_per_call": 0.013},
    ("tikhub", "search"): {"input": 0, "output": 0, "flat_rate_per_call": TIKHUB_CALL_CNY},
    ("tikhub", "endpoint_probe"): {"input": 0, "output": 0, "flat_rate_per_call": TIKHUB_CALL_CNY},
    ("tikhub", "mcp_tool"): {"input": 0, "output": 0, "flat_rate_per_call": TIKHUB_CALL_CNY},
    ("tikhub", "rest"): {"input": 0, "output": 0, "flat_rate_per_call": TIKHUB_CALL_CNY},
    ("tikhub", "hot_search"): {"input": 0, "output": 0, "flat_rate_per_call": TIKHUB_CALL_CNY},
    ("tikhub", "video_detail"): {"input": 0, "output": 0, "flat_rate_per_call": TIKHUB_CALL_CNY},
    ("tikhub", "video_comments"): {"input": 0, "output": 0, "flat_rate_per_call": TIKHUB_CALL_CNY},
    ("tikhub", "user_profile"): {"input": 0, "output": 0, "flat_rate_per_call": TIKHUB_CALL_CNY},
    ("tikhub", "user_videos"): {"input": 0, "output": 0, "flat_rate_per_call": TIKHUB_CALL_CNY},
    ("tikhub", "share_url"): {"input": 0, "output": 0, "flat_rate_per_call": TIKHUB_CALL_CNY},
}

DEFAULT_PRICING = {"input": 0.007, "output": 0.014, "cache_hit": 0.0004}

_DS_FLASH_OFF_PEAK = {"input": 0.0035, "output": 0.007, "cache_hit": 0.0002}
_DS_PRO_OFF_PEAK = {"input": 0.0035, "output": 0.007, "cache_hit": 0.0002}

OFF_PEAK_PRICING: Dict[tuple, Dict[str, float]] = {
    ("deepseek", "deepseek-chat"): _DS_FLASH_OFF_PEAK,
    ("deepseek", "deepseek-reasoner"): _DS_FLASH_OFF_PEAK,
    ("deepseek", "deepseek-v4-flash"): _DS_FLASH_OFF_PEAK,
    ("deepseek", "deepseek-flash"): _DS_FLASH_OFF_PEAK,
    ("deepseek", "deepseek-v4-pro"): _DS_PRO_OFF_PEAK,
    ("deepseek_official", "deepseek-flash"): _DS_FLASH_OFF_PEAK,
    ("deepseek_official", "deepseek-v4-flash"): _DS_FLASH_OFF_PEAK,
    ("deepseek_official", "deepseek-v4-pro"): _DS_PRO_OFF_PEAK,
}

#: 高峰窗口,**UTC**,周一至周五。`(起, 止)` 半开区间 `[起, 止)`。
#: 🔴 用 UTC 判不用北京时间判:官方英文页的口径就是 UTC,而北京时间是它加八小时的
#:    **派生表述**。在派生表述上判,等于平白多一次 +8 的错位机会 ——
#:    而错位八小时的后果是整段时间按错档计价,且不会有任何东西报错。
_DEEPSEEK_PEAK_WINDOWS_UTC = ((1, 4), (6, 10))


def deepseek_price_band(at: Optional[datetime] = None) -> str:
    """这一刻属于官方线的哪一档:`"peak"` 或 `"off_peak"`。

    🔴 `at` 为 None ⇒ 一律返回 `"peak"`。**不拿"现在"当默认**:
       调用方之所以没传时刻,是因为它不知道那次调用发生在什么时候
       (存量 token_usage 回填、离线估算都是这种);拿"现在"去判,
       等于用一个和那次调用无关的时刻决定它的价钱,而且**看起来还很合理**。
       拿不到时刻就按贵的算 —— 低估比高估危险。
    """
    if at is None:
        return "peak"
    moment = at.astimezone(timezone.utc) if at.tzinfo else at.replace(tzinfo=timezone.utc)
    if moment.weekday() >= 5:          # 周六 / 周日 全天空闲
        return "off_peak"
    hour = moment.hour
    for start, end in _DEEPSEEK_PEAK_WINDOWS_UTC:
        if start <= hour < end:
            return "peak"
    return "off_peak"

# 价目表未命中的告警去重(同一 (platform, model) 只警告一次)。
_PRICING_MISS_WARNED: set = set()
_TRACKING_CONTEXT: ContextVar[Optional[Dict[str, Any]]] = ContextVar("llm_tracking_context", default=None)


@contextmanager
def llm_tracking_context(
    *,
    caller: Optional[str] = None,
    platform: Optional[str] = None,
    model: Optional[str] = None,
    brand_id: Optional[int] = None,
    quote_id: Optional[int] = None,
    user_id: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """Temporarily attach business context to nested llm_track calls."""
    parent = _TRACKING_CONTEXT.get() or {}
    merged_metadata = dict(parent.get("metadata") or {})
    if metadata:
        merged_metadata.update({k: v for k, v in metadata.items() if v is not None})

    next_context = dict(parent)
    for key, value in {
        "caller": caller,
        "platform": platform,
        "model": model,
        "brand_id": brand_id,
        "quote_id": quote_id,
        "user_id": user_id,
    }.items():
        if value is not None:
            next_context[key] = value
    next_context["metadata"] = merged_metadata

    token = _TRACKING_CONTEXT.set(next_context)
    try:
        yield
    finally:
        _TRACKING_CONTEXT.reset(token)


def _apply_tracking_context(
    *,
    caller: str,
    platform: str,
    model: Optional[str] = None,
    brand_id: Optional[int] = None,
    quote_id: Optional[int] = None,
    user_id: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    current = _TRACKING_CONTEXT.get() or {}
    merged_metadata = dict(current.get("metadata") or {})
    if metadata:
        merged_metadata.update(metadata)

    final_caller = current.get("caller") or caller
    final_platform = current.get("platform") or platform
    final_model = model or current.get("model")
    if final_caller != caller:
        merged_metadata.setdefault("original_caller", caller)
    if final_platform != platform:
        merged_metadata.setdefault("original_platform", platform)

    return {
        "caller": final_caller,
        "platform": final_platform,
        "model": final_model,
        "brand_id": brand_id if brand_id is not None else current.get("brand_id"),
        "quote_id": quote_id if quote_id is not None else current.get("quote_id"),
        "user_id": user_id if user_id is not None else current.get("user_id"),
        "metadata": merged_metadata,
    }


def estimate_cost(
    platform: str,
    model: Optional[str],
    input_tokens: int,
    output_tokens: int,
    metadata: Optional[Dict[str, Any]] = None,
    cached_tokens: int = 0,
    at: Optional[datetime] = None,
) -> float:
    """估算成本(元) · 用 PRICING_TABLE SSOT · 未命中 fallback 保守值。

    `at` = 那次调用**实际发生的时刻**(带时区最好,裸的按 UTC 解)。
    只有官方线有峰谷两档:`PRICING_TABLE` 存的是**高峰价**,
    只有在**确知** `at` 且它落在空闲段时,才用 `OFF_PEAK_PRICING` 覆盖。
    🔴 不传 `at` ⇒ 按高峰算。低估比高估危险:高估只是账面难看,
       低估会让「这条链花不了多少钱」这个判断建立在一个假数字上。
    """
    key = (platform, model) if model else None
    pricing = PRICING_TABLE.get(key) if key else None
    if pricing and key in OFF_PEAK_PRICING and deepseek_price_band(at) == "off_peak":
        pricing = OFF_PEAK_PRICING[key]
    if not pricing:
        if platform == "tikhub":
            pricing = {"input": 0, "output": 0, "flat_rate_per_call": TIKHUB_CALL_CNY}
        else:
            # [复检返修 ①· 2026-07-27] 价目表未命中原本是**静默**落保守默认值 ——
            #   本包就是这么把 deepseek_official / hy3-preview 悄悄算错的,谁都没发现。
            #   同一个 (platform, model) 只警告一次,不刷屏;但绝不再无声。
            if key and key not in _PRICING_MISS_WARNED:
                _PRICING_MISS_WARNED.add(key)
                logger.warning(
                    "[LLMCost] 价目表未命中 %s → 落 DEFAULT_PRICING(output 0.005/K,且无缓存档)。"
                    "新增 provider/model 请补 PRICING_TABLE,否则成本会算错。", key,
                )
            pricing = DEFAULT_PRICING

    metadata = metadata or {}

    if pricing.get("audio_second") is not None:
        try:
            audio_seconds = float(metadata.get("audio_seconds") or metadata.get("duration_seconds") or 0)
        except (TypeError, ValueError):
            audio_seconds = 0
        cost = max(audio_seconds, 0) * float(pricing["audio_second"])
    elif pricing.get("flat_rate_per_call") is not None:
        raw_units = metadata.get("billable_units")
        try:
            billable_units = float(raw_units) if raw_units is not None else 1.0
        except (TypeError, ValueError):
            billable_units = 1.0
        cost = float(pricing["flat_rate_per_call"]) * max(billable_units, 0)
    elif pricing.get("flat_rate"):
        cost = float(pricing.get("output", 0))
    else:
        # 标准 token-based
        if pricing.get("cache_hit") is not None and cached_tokens > 0:
            cached_input_tokens = min(max(int(cached_tokens), 0), max(int(input_tokens), 0))
            uncached_input_tokens = max(int(input_tokens) - cached_input_tokens, 0)
            input_cost = (
                (uncached_input_tokens / 1000.0) * pricing["input"]
                + (cached_input_tokens / 1000.0) * pricing["cache_hit"]
            )
        else:
            input_cost = (input_tokens / 1000.0) * pricing["input"]
        cost = input_cost + (output_tokens / 1000.0) * pricing["output"]

    cost += float(metadata.get("flat_cost_yuan") or metadata.get("extra_cost_yuan") or 0)
    # ⚠️ 这个键是 **Kimi 专用**:Moonshot 的 $web_search 由供应商按次单收(¥0.036/次)。
    #    别的引擎不要复用它,否则等于凭空按 Kimi 单价加钱(复检返修 ③ 抓到的就是这个)。
    cost += int(metadata.get("web_search_call_count") or 0) * KIMI_WEB_SEARCH_CALL_CNY
    # [复检返修 ③· 2026-07-27] 官方 DeepSeek 的服务端检索**不单独按次计费**:
    #   官方文档说明 web search 产生的是"额外 token 成本",实测也印证 ——
    #   带检索一次调用 input_tokens 6.5-7.8 万,不带检索仅 14(检索结果全量进上下文)。
    #   也就是说检索费用**已经含在 input_tokens 里**,再按次加一遍就是重复计费。
    #   改前它被写进 Kimi 的计费键 → 单次落库 ¥0.156 vs 真价 ¥0.080(1.95×),
    #   其中 46% 是按 Kimi 单价凭空加的。故:**独立键、只记次数、不计费**。
    #   若将来 DeepSeek 官方公布独立检索单价,在此处补一行即可(现在不许拍脑袋填)。
    _ = int(metadata.get("deepseek_web_search_call_count") or 0)
    cost += int(metadata.get("doubao_ai_search_call_count") or 0) * 0.2
    cost += int(metadata.get("doubao_web_search_call_count") or 0) * 0.004

    return round(cost, 6)


def infer_platform_from_url(url: str, *, default: str = "unknown") -> str:
    """Infer cost platform from a provider URL."""
    u = (url or "").lower()
    if "dashscope" in u or "aliyuncs" in u:
        return "dashscope"
    if "deepseek" in u:
        return "deepseek"
    if "moonshot" in u or "kimi" in u:
        return "kimi"
    if "volces" in u or "ark.cn" in u or "doubao" in u:
        return "doubao"
    if "openrouter" in u:
        return "openrouter"
    if "siliconflow" in u:
        return "siliconflow"
    if "metaso" in u:
        return "metaso"
    if "5118" in u:
        return "5118"
    if "tikhub" in u:
        return "tikhub"
    return default


def estimate_cost_for_model(model_name: str, input_tokens: int, output_tokens: int,
                            platform: Optional[str] = None,
                            at: Optional[datetime] = None) -> float:
    """Compatibility helper for legacy token_usage call sites that only know model.

    🔴 `at` 默认 None ⇒ 走高峰价。本函数现有四个调用方(db/diagnosis_db.py:3077、
       db/monitoring_db.py:4775 经 estimate_cost、services/flywheel_judgment.py:186、
       writing/article_generator_service.py:3034 经 estimate_cost)**都不知道调用时刻**,
       所以它们全部按高峰计 —— 这是故意的,不是漏传。
    """
    platform = platform or infer_platform_for_model(model_name)
    return estimate_cost(platform, model_name, input_tokens, output_tokens, at=at)


def infer_platform_for_model(model_name: Optional[str], *, default: str = "unknown") -> str:
    model = (model_name or "").lower()
    if model.startswith("kimi"):
        return "kimi"
    if model.startswith("doubao"):
        return "doubao"
    if model.startswith("qwen") or model.startswith("deepseek-v4"):
        return "dashscope"
    if model.startswith("deepseek"):
        return "deepseek"
    if model.startswith(("anthropic/", "google/", "openai/")):
        return "openrouter"
    if model.startswith("pro/deepseek"):
        return "siliconflow"
    return default


def usage_from_response_payload(payload: Any) -> tuple[int, int, int]:
    """Extract prompt/completion/cache tokens from OpenAI-compatible JSON."""
    usage = payload.get("usage", {}) if isinstance(payload, dict) else getattr(payload, "usage", {}) or {}

    def _usage_get(name: str, default: int = 0) -> Any:
        if isinstance(usage, dict):
            return usage.get(name, default)
        return getattr(usage, name, default)

    input_tokens = int(
        _usage_get("prompt_tokens")
        or _usage_get("input_tokens")
        or _usage_get("total_input_tokens")
        or 0
    )
    output_tokens = int(
        _usage_get("completion_tokens")
        or _usage_get("output_tokens")
        or _usage_get("total_output_tokens")
        or 0
    )
    total_tokens = int(_usage_get("total_tokens") or 0)
    if input_tokens == 0 and output_tokens == 0 and total_tokens:
        input_tokens = total_tokens
    cached_tokens = int(
        _usage_get("prompt_cache_hit_tokens")
        or _usage_get("cached_tokens")
        # Anthropic Messages 协议(DeepSeek 官方端点走这一套)的命中缓存字段名。
        # 不补这一项,官方 DeepSeek 的缓存命中会被记成 0,成本口径偏高。
        or _usage_get("cache_read_input_tokens")
        or 0
    )
    return input_tokens, output_tokens, cached_tokens


class LLMCallContext:
    """单次调用上下文 · callsite 调 .record() 写指标"""

    def __init__(
        self,
        caller: str,
        platform: str,
        model: Optional[str] = None,
        brand_id: Optional[int] = None,
        quote_id: Optional[int] = None,
        user_id: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.caller = caller
        self.platform = platform
        self.model = model
        self.brand_id = brand_id
        self.quote_id = quote_id
        self.user_id = user_id
        self.metadata: Dict[str, Any] = metadata or {}
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self.success = True
        self.error_msg: Optional[str] = None
        #: [WO 未送达不计费 2026-08-08] 这次调用**确定没送到对方服务器**
        #: (ConnectError 一族)。置位后 `estimate_cost()` 返 0 —— 供应商不会
        #: 为一个没送出去的请求收钱,我们的成本账也不该记。
        self.undelivered = False
        self.start_time = time.monotonic()
        #: [WO_206 c1p] 这次调用**真实发生的时刻**(UTC)。`start_time` 是 monotonic,
        #: 只能量时长、读不出"是几点" —— 而峰谷计价要的正是"几点"。
        self.started_at_utc = datetime.now(timezone.utc)

    def record(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        success: bool = True,
        error_msg: Optional[str] = None,
        model: Optional[str] = None,
        **extra_metadata: Any,
    ) -> None:
        """callsite 在 LLM 返回后调用 · 记录真实 tokens + 状态"""
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cached_tokens = cached_tokens
        self.success = success
        if error_msg is not None:
            self.error_msg = error_msg[:500] if len(error_msg) > 500 else error_msg
        if model:
            self.model = model
        if extra_metadata:
            self.metadata.update(extra_metadata)

    def estimate_cost(self) -> float:
        # [WO 未送达不计费 2026-08-08 · 资金] 归零点放在**这里**而不是 `_write_log_row`
        # 的入参处:`estimate_cost()` 是"这次调用花了多少"的唯一回答口,
        # 归零放它里面,任何调用方都拿到同一个答案(禁第二套)。
        if self.undelivered:
            return 0.0
        return estimate_cost(
            self.platform,
            self.model,
            self.input_tokens,
            self.output_tokens,
            metadata=self.metadata,
            cached_tokens=self.cached_tokens,
            # 🔴 用**开始**时刻不用结束时刻:跨档的那一次按它开始时的档算,
            #    与供应商"按请求落在哪个窗口"的口径一致,也避免长请求被算贵。
            at=self.started_at_utc,
        )


def _is_undelivered_exception(exc: BaseException) -> bool:
    """这个异常是不是「确定没送到对方服务器」。

    🔴 **不在这里另写一份异常清单。** 判据复用
    `tools.search.provider_circuit_breaker.undelivered_error_types()`,
    它自己又转发 `services.marketing.image_client._connect_error_types()` ——
    那份 docstring 明写「**唯一**的"没送出去"异常集合。改这里 = 改计费安全边界」。
    三处同一个来源,改一处三处一起变。

    🔴 取不到判据时**返回 False = 照旧计费**。这是刻意的方向选择:
    `llm_call_log` 是成本核算 SSOT,判据坏掉时宁可**多记**(可发现、可对账),
    也不要**少记**(静默少算成本 = 账对不上还查不出为什么)。
    """
    try:
        from tools.search.provider_circuit_breaker import undelivered_error_types

        types = undelivered_error_types()
    except Exception:  # pragma: no cover - 依赖缺失时保守计费
        return False
    return bool(types) and isinstance(exc, types)


@asynccontextmanager
async def llm_track(
    caller: str,
    platform: str,
    *,
    model: Optional[str] = None,
    brand_id: Optional[int] = None,
    quote_id: Optional[int] = None,
    user_id: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """async context manager · with 体内 callsite 调 t.record() 提交指标
    无论 with 体抛异常都会写 log(success=False)
    """
    ctx_kwargs = _apply_tracking_context(
        caller=caller,
        platform=platform,
        model=model,
        brand_id=brand_id,
        quote_id=quote_id,
        user_id=user_id,
        metadata=metadata,
    )
    ctx = LLMCallContext(
        **ctx_kwargs,
    )
    try:
        yield ctx
    except Exception as e:
        # caller 没自己 try/except 时兜底 · 但仍 raise(让上游处理)
        if ctx.success:  # 没人调 record(success=False)
            ctx.success = False
            ctx.error_msg = f"{type(e).__name__}: {str(e)[:400]}"
        # [WO 未送达不计费 2026-08-08 · 资金] 只认「确定没送出去」那一族。
        #   收到**任何** HTTP 响应(含 4xx/5xx)= 已送达 = 该记的成本照记;
        #   ReadTimeout 一族也**不算**(可能已受理,供应商可能已计费)。
        #   判据复用熔断包那份唯一定义,见 `_is_undelivered_exception`。
        if _is_undelivered_exception(e):
            ctx.undelivered = True
            ctx.metadata = {**(ctx.metadata or {}), "undelivered": True}
        raise
    finally:
        duration_ms = int((time.monotonic() - ctx.start_time) * 1000)
        try:
            await asyncio.to_thread(
                _write_log_row,
                caller=ctx.caller,
                platform=ctx.platform,
                model=ctx.model,
                input_tokens=ctx.input_tokens,
                output_tokens=ctx.output_tokens,
                cached_tokens=ctx.cached_tokens,
                estimated_cost=ctx.estimate_cost(),
                duration_ms=duration_ms,
                brand_id=ctx.brand_id,
                quote_id=ctx.quote_id,
                user_id=ctx.user_id,
                success=ctx.success,
                error_msg=ctx.error_msg,
                metadata=ctx.metadata,
            )
        except Exception as track_err:
            # tracker 自身异常不阻塞主流程 · log warning 即可
            logger.warning(
                f"[LLMTracker] 写 llm_call_log 失败 · 不阻塞 caller={caller} platform={platform}: {track_err}"
            )


def _write_log_row(
    *,
    caller: str,
    platform: str,
    model: Optional[str],
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    estimated_cost: float,
    duration_ms: int,
    brand_id: Optional[int],
    quote_id: Optional[int],
    user_id: Optional[int],
    success: bool,
    error_msg: Optional[str],
    metadata: Dict[str, Any],
) -> None:
    """sync 写 DB · async caller 走 asyncio.to_thread 包"""
    from db.connection import get_connection
    from psycopg2.extras import Json

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO llm_call_log
                (caller, platform, model, input_tokens, output_tokens, cached_tokens,
                 estimated_cost, duration_ms, brand_id, quote_id, user_id,
                 success, error_msg, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                caller,
                platform,
                model,
                input_tokens,
                output_tokens,
                cached_tokens,
                estimated_cost,
                duration_ms,
                brand_id,
                quote_id,
                user_id,
                success,
                error_msg,
                Json(metadata or {}),
            ),
        )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# sync 版本(给少数仍是 sync 的 callsite 用 · 例如 scheduler 内某些 helper)
# 95% 场景用 async llm_track · sync 版本仅兜底
# ============================================================

class _SyncLLMTrack:
    """同步 contextmanager · 用于 sync callsite"""
    def __init__(self, **kwargs):
        self.ctx = LLMCallContext(**kwargs)

    def __enter__(self):
        return self.ctx

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None and self.ctx.success:
            self.ctx.success = False
            self.ctx.error_msg = f"{exc_type.__name__}: {str(exc_val)[:400]}"
        # [WO R2 2026-08-09] 🔴 同步路径补上与 `llm_track` **同一条**判据。
        #   上一版只改了异步版,sync 版 8 个调用点(OCR / 观测解释 / 进件 AI /
        #   知识管线 / 投放 / ASR …)一个都没覆盖。当时的理由是"它们全是 token
        #   计费,未送达 tokens=0 成本≈0" —— 那是**当前恰好没事**,不是设计保证:
        #   任何一处接上 flat-rate 供应商,缺口当场生效。这里直接对齐,不留时间差。
        if exc_val is not None and _is_undelivered_exception(exc_val):
            self.ctx.undelivered = True
            self.ctx.metadata = {**(self.ctx.metadata or {}), "undelivered": True}
        duration_ms = int((time.monotonic() - self.ctx.start_time) * 1000)
        try:
            _write_log_row(
                caller=self.ctx.caller,
                platform=self.ctx.platform,
                model=self.ctx.model,
                input_tokens=self.ctx.input_tokens,
                output_tokens=self.ctx.output_tokens,
                cached_tokens=self.ctx.cached_tokens,
                estimated_cost=self.ctx.estimate_cost(),
                duration_ms=duration_ms,
                brand_id=self.ctx.brand_id,
                quote_id=self.ctx.quote_id,
                user_id=self.ctx.user_id,
                success=self.ctx.success,
                error_msg=self.ctx.error_msg,
                metadata=self.ctx.metadata,
            )
        except Exception as e:
            logger.warning(f"[LLMTracker:sync] 写 log 失败 caller={self.ctx.caller}: {e}")
        # 不吞异常
        return False


def llm_track_sync(**kwargs) -> _SyncLLMTrack:
    """sync 版本 contextmanager · with 体内调 ctx.record(...)"""
    return _SyncLLMTrack(**_apply_tracking_context(**kwargs))
