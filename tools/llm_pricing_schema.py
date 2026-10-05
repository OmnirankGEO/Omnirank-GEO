"""LLM-first 报价 · Pydantic V2 强 Schema 校验

P0 门禁(老板 2026-05-12 §4.6 拍板):
- 字段名必须带单位后缀:`*_yuan`(人民币整数元)/ `*_0_5`(0-5 浮点)
- 禁止无单位字段(`standard_price` 等)
- `value_score_0_5` 必须在 [0, 5] · 否则整批 retry
- 任一价格字段不是整数元 · 整批 retry
- `should_quote=false` 时 entry/standard/flagship 三个价格必须全 -1
- `intent=informational` 且 `funnel=awareness` 默认 `should_quote=false`
- fallback provider 也必须跑此 schema · 不能只看 HTTP 200

实测发现(2026-05-12 deepseek-v4-flash):
- 弱 prompt: standard_price 可能输出 5.0(单位被混淆)→ Pydantic 拒
- 强 schema prompt: standard_price_yuan=2500 整数元 · 信息型 -1 · 稳定
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


IntentType = Literal["transactional", "commercial", "informational"]
FunnelType = Literal["decision", "consideration", "awareness"]


class LLMPricedKeyword(BaseModel):
    """LLM 输出的单关键词报价结构 · 必须通过此 schema 校验才落库

    [CTO-15.23 2026-05-12 P1 修] 老板审计 · extra='forbid' 真正禁止无单位字段
      原 default(允许 extra)· standard_price=5.0 这种脏字段被静默忽略 · 只要同时有
      standard_price_yuan 就能通过
      修法:extra='forbid' · LLM 输出含任何 schema 未声明字段直接 ValidationError
    """

    model_config = ConfigDict(extra="forbid")

    keyword: str = Field(min_length=1, max_length=120)
    intent: IntentType
    funnel: FunnelType
    value_score_0_5: float = Field(ge=0.0, le=5.0)

    entry_price_yuan: int = Field(ge=-1, le=20000)
    standard_price_yuan: int = Field(ge=-1, le=20000)
    flagship_price_yuan: int = Field(ge=-1, le=20000)

    should_quote: bool
    reason_zh: str = Field(min_length=1, max_length=120)

    business_line: str = Field(default="", max_length=40)

    needs_review: bool = Field(default=False)
    review_reason: str = Field(default="", max_length=80)

    @model_validator(mode="after")
    def _validate_consistency(self) -> "LLMPricedKeyword":
        if not self.should_quote:
            if (self.entry_price_yuan, self.standard_price_yuan, self.flagship_price_yuan) != (-1, -1, -1):
                raise ValueError(
                    f"should_quote=false 时三档价格必须全 -1 · "
                    f"实际 ({self.entry_price_yuan},{self.standard_price_yuan},{self.flagship_price_yuan})"
                )
        else:
            for label, v in (("entry", self.entry_price_yuan), ("standard", self.standard_price_yuan), ("flagship", self.flagship_price_yuan)):
                if v < 100:
                    raise ValueError(f"should_quote=true 时 {label}_price_yuan 必须 ≥ 100 · 实际 {v}")
            if not (self.entry_price_yuan < self.standard_price_yuan < self.flagship_price_yuan):
                raise ValueError(
                    f"should_quote=true 时三档必须严格递增 · "
                    f"实际 entry={self.entry_price_yuan} std={self.standard_price_yuan} flag={self.flagship_price_yuan}"
                )

        if self.intent == "informational" and self.funnel == "awareness" and self.should_quote:
            if not self.review_reason:
                raise ValueError("informational + awareness 但 should_quote=true · 必须填 review_reason 给出强商业理由")

        return self


class LLMPricingBatch(BaseModel):
    """LLM 一次输出的整批关键词报价"""

    model_config = ConfigDict(extra="forbid")

    keywords: list[LLMPricedKeyword] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_duplicate_keywords(self) -> "LLMPricingBatch":
        seen: set[str] = set()
        for kw in self.keywords:
            if kw.keyword in seen:
                raise ValueError(f"批内重复关键词: {kw.keyword}")
            seen.add(kw.keyword)
        return self


SOFT_GUARD_STANDARD_MIN_YUAN = 250
SOFT_GUARD_STANDARD_MAX_YUAN = 6000
SOFT_GUARD_BUNDLE_DEVIATION_RATIO = 3.0


def apply_price_soft_guard(kw: LLMPricedKeyword) -> LLMPricedKeyword:
    """单关键词软护栏 · 不覆盖 LLM 判断 · 只 flag needs_review"""
    if not kw.should_quote:
        return kw

    flags: list[str] = []
    if kw.standard_price_yuan < SOFT_GUARD_STANDARD_MIN_YUAN:
        flags.append(f"标准版 ¥{kw.standard_price_yuan} < ¥{SOFT_GUARD_STANDARD_MIN_YUAN} 软下限")
    if kw.standard_price_yuan > SOFT_GUARD_STANDARD_MAX_YUAN:
        flags.append(f"标准版 ¥{kw.standard_price_yuan} > ¥{SOFT_GUARD_STANDARD_MAX_YUAN} 软上限")

    if flags:
        reason = " · ".join(flags)
        return kw.model_copy(update={"needs_review": True, "review_reason": (kw.review_reason + " · " + reason).strip(" · ")})
    return kw


def parse_llm_response(raw_text: str) -> LLMPricingBatch:
    """从 LLM 原始文本解析 + 强 schema 校验

    支持 markdown 代码块包裹 · 失败时 json_repair 兜底

    Raises:
        ValueError: schema 校验失败 · 调用方触发 retry
    """
    import json

    text = (raw_text or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            from json_repair import repair_json
            repaired = repair_json(text, return_objects=False)
            data = json.loads(repaired)
        except Exception as exc:
            raise ValueError(f"JSON parse + json_repair 都失败: {exc} · raw[:200]={text[:200]}") from exc

    return LLMPricingBatch.model_validate(data)
