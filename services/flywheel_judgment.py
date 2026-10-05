"""飞轮判断层(B 段 · 2026-07-29)· Owner 拍板"重要的地方用 deepseek 接管"。

分工铁律(每个判断点都照这条走):
    **判断归 LLM,编排 / 幂等 / 心跳 / 告警 / 资金对账归确定性代码。**
    LLM 修不了断掉的管道 —— A 段那三个真问题(job 没注册、except 吞异常、账本没生产者)
    换成什么模型都不会自己好。所以这一层只负责"选哪些 / 判哪类 / 挑哪个",
    统计口径、写库、幂等键、告警仍然全部由代码算,防止模型编数。

五条通用约束在本模块统一实现,判断点只写 prompt 和规则兜底:
  1. **多 provider 回落**:主模型 deepseek-v4-flash,失败按链路顺序换下一家,禁单点;
  2. **每日调用与成本上限**:额度从 `flywheel_judgment_log` 当日汇总算(不是内存计数器,
     cron 重启 / 蓝绿换主都不会把额度洗白);超限立刻降级到规则兜底;
     用量查不出来时**按超限处理**(不能"查不到就当零花费"接着刷);
  3. **每次留痕**:输入摘要 / 输出 / provider / model / prompt 版本 / tokens / 花费全部入库,
     规则兜底那次也留痕(带 fallback_reason),事后可复核可回归;
  4. **advisory**:本层永远只返回建议。任何判断点都不得成为阻断闸,
     法律红线与资金守卫保持确定性代码;
  5. **兜底必然存在**:`rule_fallback` 是必填参数 —— provider 全挂时系统行为不变,
     只是少一层智能,绝不因为模型不可用就停摆。
"""
from __future__ import annotations
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from db.flywheel_judgment_db import SOURCE_LLM, SOURCE_RULE, record_judgment, today_usage

logger = logging.getLogger("GEO-FlywheelJudgment")

PROMPT_VERSION = "b1-2026-07-29"

# provider 回落链。首选 = Owner 指定的**官方线 Flash 档**;后面两家只在前一家失败时才会被摸到。
# 🔴 两行的名字**故意不同**:第一行是官方线(2026-09-14 官方改名 deepseek-flash(同一档);名字统一由 config/deepseek_models 出),
#    第二行是百炼上**另一家**的模型 ID,官方改名不改它 —— 一起改会让百炼那条链收到一个它不认识的名字。
# 模型名与 writing/llm_utils.py 的既定口径一致,不在这里另起一套模型别名。
DEFAULT_MODEL_CHAIN: tuple[tuple[str, str], ...] = (
    ("deepseek", DEEPSEEK_OFFICIAL_FLASH),
    ("dashscope", "deepseek-v4-flash"),   # 同模型换通道:DeepSeek 直连挂了走 DashScope 集成
    ("dashscope", "qwen3.6-plus"),        # 再挂就换厂商,宁可换模型也不要停摆
)


@dataclass(frozen=True)
class JudgmentPoint:
    key: str
    name: str
    why: str
    daily_call_cap: int = 200
    daily_cost_cap_cny: float = 5.0
    temperature: float = 0.2
    max_tokens: int = 1500
    timeout_seconds: float = 60.0
    model_chain: tuple[tuple[str, str], ...] = field(default=DEFAULT_MODEL_CHAIN)


JUDGMENT_POINTS: dict[str, JudgmentPoint] = {
    p.key: p for p in (
        JudgmentPoint(
            key="research_topic_selection",
            name="B1 采集选题",
            why="下一轮采哪些行业/问题。现为静态题库,不看历史被引产出率也不看覆盖缺口。",
            daily_call_cap=40, daily_cost_cap_cny=3.0, max_tokens=2000,
        ),
        JudgmentPoint(
            key="corpus_value_labeling",
            name="B2 语料价值判定",
            why="两万多条引用当前无人消费,先判哪些可复用并打结构化标签。",
            daily_call_cap=300, daily_cost_cap_cny=10.0, max_tokens=1200,
        ),
        JudgmentPoint(
            key="media_mix_decision",
            name="B3 媒体组合决策",
            why="问题族×行业的真实 mix + 缺货同族降位。统计口径仍由代码算,模型只给取舍。",
            daily_call_cap=100, daily_cost_cap_cny=5.0, max_tokens=1500,
        ),
        JudgmentPoint(
            key="diagnosis_reuse_matching",
            name="B4 诊断沉淀复用",
            why="新客户进来先判哪些既有行业语料/竞品图谱可直接用(给素材,不硬编码)。",
            daily_call_cap=200, daily_cost_cap_cny=6.0, max_tokens=1500,
        ),
        JudgmentPoint(
            key="writing_strategy_selection",
            name="B5 写作策略选择",
            why="在 A4 指派管道之上选文体/角度/取材,结果进 assignments,被引回流进 outcome_events。",
            daily_call_cap=300, daily_cost_cap_cny=10.0, max_tokens=1200,
        ),
    )
}


@dataclass
class JudgmentResult:
    """判断结果。`source` 说明这次到底是模型给的还是规则兜底给的 —— 消费方可据此调整信任度。"""
    point_key: str
    source: str
    payload: Any
    provider: Optional[str] = None
    model: Optional[str] = None
    fallback_reason: Optional[str] = None
    advisory: bool = True

    @property
    def from_llm(self) -> bool:
        return self.source == SOURCE_LLM


def judgment_enabled(point_key: str) -> bool:
    """判断点总开关。默认**关**:B 段随 A 段一起部署但不自动开始花钱,
    由 Owner 逐点打开(FLYWHEEL_JUDGE_<POINT>=1,或 FLYWHEEL_JUDGE_ALL=1 全开)。"""
    if os.getenv("FLYWHEEL_JUDGE_ALL", "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    flag = f"FLYWHEEL_JUDGE_{point_key.upper()}"
    return os.getenv(flag, "").strip().lower() in {"1", "true", "yes", "on"}


def _budget_verdict(point: JudgmentPoint) -> Optional[str]:
    """返回 None = 有额度;返回字符串 = 拒绝理由(直接走规则兜底)。"""
    usage = today_usage(point.key)
    if not usage.get("available"):
        return "budget_unknown"  # 查不到用量 → 按超限处理(fail-closed 防失控刷量)
    if usage["calls"] >= point.daily_call_cap:
        return f"daily_call_cap_reached({usage['calls']}/{point.daily_call_cap})"
    if usage["cost_cny"] >= point.daily_cost_cap_cny:
        return f"daily_cost_cap_reached({usage['cost_cny']:.4f}/{point.daily_cost_cap_cny})"
    return None


def _parse_json(text: str) -> Optional[Any]:
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1] if "```" in cleaned[3:] else cleaned[3:]
        if cleaned.lstrip().lower().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
    try:
        return json.loads(cleaned)
    except Exception:
        try:
            from json_repair import repair_json
            return json.loads(repair_json(cleaned))
        except Exception:
            return None


def _call_one(provider: str, model: str, prompt: str, point: JudgmentPoint) -> dict[str, Any]:
    """单次调用。返回 {ok, text, input_tokens, output_tokens, cost_cny, error}。绝不抛。"""
    import httpx

    from writing.llm_utils import API_URLS, get_api_key_for_provider, get_thinking_disabled_params

    api_url = API_URLS.get(provider)
    api_key = get_api_key_for_provider(provider)
    if not api_url or not api_key:
        return {"ok": False, "error": f"{provider} 未配置 API key"}

    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": point.temperature,
        "max_tokens": point.max_tokens,
    }
    body.update(get_thinking_disabled_params(api_url, model))

    try:
        from tools.llm_call_tracker import estimate_cost_for_model, usage_from_response_payload

        with httpx.Client(timeout=point.timeout_seconds) as client:
            resp = client.post(
                api_url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body,
            )
        if resp.status_code != 200:
            return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        payload = resp.json()
        text = payload["choices"][0]["message"]["content"]
        in_tok, out_tok, _cached = usage_from_response_payload(payload)
        try:
            cost = float(estimate_cost_for_model(model, in_tok, out_tok))
        except Exception:
            cost = 0.0
        return {"ok": True, "text": text, "input_tokens": in_tok,
                "output_tokens": out_tok, "cost_cny": cost}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


def judge(
    point_key: str,
    *,
    prompt: str,
    rule_fallback: Callable[[], Any],
    input_summary: Optional[dict[str, Any]] = None,
    validate: Optional[Callable[[Any], bool]] = None,
) -> JudgmentResult:
    """跑一个判断点。**任何情况下都会返回结果** —— provider 全挂就是规则兜底那一份。

    `validate` 可选:模型输出结构不符预期时同样降级到规则兜底(防止把畸形结构喂给下游)。
    """
    point = JUDGMENT_POINTS.get(point_key)
    if point is None:
        raise KeyError(f"未登记的判断点:{point_key}(新判断点必须先进 JUDGMENT_POINTS)")

    summary = dict(input_summary or {})

    def _fallback(reason: str, error: Optional[str] = None) -> JudgmentResult:
        payload = rule_fallback()
        # "判断点没开"不是一次判断,没有可复核的内容;而 B3 挂在发布推荐这种热路径上,
        # 每次都记一行会把留痕表刷成噪音。其余降级原因(超预算/模型全挂/结构不合规)全部留痕。
        if reason != "point_disabled":
            record_judgment(
                point_key=point.key, source=SOURCE_RULE, prompt_version=PROMPT_VERSION,
                input_summary=summary, output=payload, fallback_reason=reason, error=error,
            )
            logger.info("[FlywheelJudgment] %s 走规则兜底(%s)", point.key, reason)
        return JudgmentResult(point.key, SOURCE_RULE, payload, fallback_reason=reason)

    if not judgment_enabled(point.key):
        return _fallback("point_disabled")

    verdict = _budget_verdict(point)
    if verdict:
        return _fallback(verdict)

    errors: list[str] = []
    for provider, model in point.model_chain:
        started = time.time()
        outcome = _call_one(provider, model, prompt, point)
        latency_ms = int((time.time() - started) * 1000)
        if not outcome.get("ok"):
            errors.append(f"{provider}/{model}: {outcome.get('error')}")
            continue

        parsed = _parse_json(outcome.get("text") or "")
        if parsed is None or (validate and not validate(parsed)):
            errors.append(f"{provider}/{model}: 输出结构不合预期")
            # 结构不合预期也要留痕(带 error),否则 prompt 回归时无据可查。
            record_judgment(
                point_key=point.key, source=SOURCE_LLM, provider=provider, model=model,
                prompt_version=PROMPT_VERSION, input_summary=summary,
                output={"raw": (outcome.get("text") or "")[:2000]},
                input_tokens=outcome.get("input_tokens", 0),
                output_tokens=outcome.get("output_tokens", 0),
                cost_cny=outcome.get("cost_cny", 0.0), latency_ms=latency_ms,
                error="unparseable_or_invalid_structure",
            )
            continue

        record_judgment(
            point_key=point.key, source=SOURCE_LLM, provider=provider, model=model,
            prompt_version=PROMPT_VERSION, input_summary=summary, output=parsed,
            input_tokens=outcome.get("input_tokens", 0),
            output_tokens=outcome.get("output_tokens", 0),
            cost_cny=outcome.get("cost_cny", 0.0), latency_ms=latency_ms,
        )
        return JudgmentResult(point.key, SOURCE_LLM, parsed, provider=provider, model=model)

    return _fallback("all_providers_failed", error=" | ".join(errors)[:2000])
