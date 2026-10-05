"""V3/V4/V5 共享 · 写作飞轮「解释层」LLM 助手(叶子模块)。

本 SPEC 的核心:数字永远真实(SQL/统计),LLM 只把真实数字/规律「说成人话」。
V3 历史评语 / V4 结构规则行业化 / V5 飞轮总汇总 三处都需要:
  ① 同步调 LLM(它们都跑在 sync 聚合 / to_thread 线程池里,用 sync httpx 最干净,不折腾事件循环);
  ② 输出守卫(承诺话术 / 外部厂商自指 不外泄);
  ③ 数据指纹(数据不变不重跑,省钱)。

叶子纪律:只 import writing.llm_utils / tools.llm_call_tracker / 标准库 —— 不 import services.* 或
style_control,避免与 flywheel 服务成环。

🔴 铁律:
- **数字零 LLM 产**:调用方只把「已算好的真实数字」塞进 prompt,prompt 明令「只引用给定数字,禁止推算新数字」。
- **空数据不调**:调用方在数据不足时根本不进来(不传空 prompt 让模型编)。
- **fail-soft**:LLM 失败 / 守卫拦截 → 返回 None,调用方回退静态文案,绝不阻断看板。
- **监测引擎名可露**:豆包/DeepSeek/Kimi/千问 是 GEO 被推荐的目标平台(用户面向对象),不属「内部实现」,不禁;
  真正要挡的是「我们用什么模型写内容」这类外部厂商自指 + 承诺/保证话术。
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

import httpx

from writing.llm_utils import get_llm_config, get_thinking_disabled_params

logger = logging.getLogger("GEO-WritingFlywheel.LLM")

# 输出守卫黑名单(小写子串命中即拦截 → 调用方回退静态)。
# 注意:豆包/deepseek/kimi/qwen/千问 是监测引擎名,面向用户合法,**不列入**。
# 🔴 不放裸 "100%"/"百分之百":真实统计(如某特征采纳组 100% 具备)是本 SPEC 要放行的合法数字,
#    裸子串会把「被采纳文章 100% 具备列表」这类真实解释整段误拦 → 静默 defeat V4/V5。
#    承诺语义靠下方 _GUARD_PROMISE_RE(百分比 + 上榜/被引 等结果词的组合)精准命中。
_GUARD_BANNED = (
    # 承诺/保证话术(明确承诺短语,非裸百分比)
    "保证上", "保证被引", "保证收录", "必定上", "一定能上", "稳上榜", "包上榜", "承诺上榜", "承诺被引",
    # 非监测的外部厂商 / 模型自指(内部实现不外泄;"gpt" 裸串覆盖 GPT/ChatGPT/GPT-4/"GPT 模板腔")
    "openai", "gpt", "claude", "anthropic", "gemini",
    "作为一个ai", "作为ai语言模型", "作为一个语言模型", "我是一个大语言模型", "as an ai",
)

# 承诺语义组合:承诺/保证词 ↔ 结果词(上榜/被引/收录/引用率/排名第一/百分之百)近距离共现才拦,放行真实统计百分比。
# [review fix] 承诺词必须是双字以上且承诺语义明确的组合。早版含单字「稳/包」与通用副词「一定」,
# 对本域高频合法话术误拦率极高:「样本包含被引文章」「表现稳定,被引率略升」「有一定的被引提升」
# 全被静默丢弃(间隔字符集还放行逗号);V5 整段守卫下一条误拦拖死全部 insight。
# 同时补漏(原 FN):确保/肯定能 承诺动词、引用率 结果词、「引用率可达100%」比率式绝对承诺、全角%。
_GUARD_PROMISE_RE = re.compile(
    r"(?:(?:保证|承诺|必定|确保|一定能|肯定能|稳上|包上)[^。;;\n]{0,8}"
    r"(?:上榜|被引|收录|引用率|排名第一|100\s*[%％]|百分之百|百分百))"
    r"|(?:(?:100\s*[%％]|百分之百|百分百)[^。;;\n]{0,8}(?:上榜|被引|收录|引用率|有效|保证|承诺))"
    r"|(?:(?:被引率|引用率|收录率|上榜率)[^。;;\n]{0,4}(?:100\s*[%％]|百分之百|百分百))"
)


def fingerprint(*parts: Any) -> str:
    """对「稳定标识字段」做 sha256,数据真变才变(用整数计数,不用已 round 的浮点,防抖动)。"""
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def guard_output(text: str) -> tuple[str, bool]:
    """返回 (原文, blocked)。命中外部厂商自指 / 承诺话术 → blocked=True,调用方回退静态。

    真实统计百分比(含 100%)放行 —— 只有「承诺/保证 + 上榜/被引」近距离组合才判承诺话术。
    """
    raw = text or ""
    low = raw.lower()
    for bad in _GUARD_BANNED:
        if bad in low:
            return text, True
    if _GUARD_PROMISE_RE.search(raw):
        return text, True
    return text, False


def call_flywheel_llm_sync(
    task_name: str,
    system: str,
    user: str,
    *,
    max_tokens: int = 400,
    temperature: float = 0.3,
    json_mode: bool = False,
) -> str:
    """同步调写作飞轮解释层 LLM(默认走写作全局模型族 = deepseek v4-flash,成本分币级)。

    task_name 走 get_llm_config(task_name,'writing') 开放注册(未注册回落写作默认模型,可跑)。
    失败直接抛(调用方 fail-soft 回退静态)。禁在有运行中事件循环的线程直接调(sync httpx 会阻塞)——
    本仓 V3/V4/V5 均在 sync 聚合 / asyncio.to_thread 线程池内调用,安全。
    """
    from tools.llm_call_tracker import llm_track_sync, usage_from_response_payload

    api_url, api_key, model, provider = get_llm_config(task_name, "writing")
    if not api_key:
        raise RuntimeError(f"写作 LLM 未配置 api_key(task={task_name})")
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    payload.update(get_thinking_disabled_params(api_url, model))  # 解释类任务关思考,省时省钱
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    with llm_track_sync(
        caller="writing_flywheel", platform=provider, model=model,
        metadata={"task": task_name},
    ) as tracker:
        resp = httpx.post(api_url, headers=headers, json=payload, timeout=60.0)
        resp.raise_for_status()
        data = resp.json()
        it, ot, ct = usage_from_response_payload(data)
        tracker.record(input_tokens=it, output_tokens=ot, cached_tokens=ct, success=True)
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
