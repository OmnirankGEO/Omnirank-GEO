"""SSOT business-governance-master §3.3 / §9.5 — reverse discriminator.

The unified CommercialQueryPolicy is the single paid-delivery-eligibility
authority. Any upstream intent label (e.g. the LLM/rule intent produced by
tools.keyword_value_scorer._override_intent_by_pattern) is SUBORDINATE:

  * an "informational" hint can NEVER drop an engine-commercial keyword out of
    paid delivery (guards against silently losing a real buyer-intent word);
  * a "commercial" hint can NEVER launder an engine-knowledge keyword into paid
    delivery (guards against a second classifier overruling the engine).

This proves the residual intent heuristics are hints, not a second delivery gate.
"""
from services.commercial_query_policy import (
    INTENT_COMMERCIAL,
    INTENT_KNOWLEDGE,
    evaluate,
)


# Buyer-intent proven by the text engine (must stay deliverable).
_ENGINE_COMMERCIAL = [
    "深圳电梯厂家哪家靠谱",
    "律师事务所排名",
    "细胞治疗隔离器品牌对比与采购建议",
    "预算3000元以内的GEO服务商怎么选",
    "国产和进口哪个好",
]

# Pure knowledge (no selection/purchase value; must stay excluded).
_ENGINE_KNOWLEDGE = [
    "电梯是什么",
    "GEO和SEO的区别",
]


def test_informational_hint_cannot_drop_engine_commercial_keyword():
    for kw in _ENGINE_COMMERCIAL:
        decision = evaluate(kw, intent_hint="informational")
        assert decision.commercial_delivery_eligible is True, kw
        assert decision.intent_type == INTENT_COMMERCIAL, kw


def test_commercial_hint_cannot_force_engine_knowledge_keyword_into_delivery():
    for kw in _ENGINE_KNOWLEDGE:
        decision = evaluate(kw, intent_hint="commercial")
        assert decision.commercial_delivery_eligible is False, kw
        assert decision.intent_type == INTENT_KNOWLEDGE, kw


def test_engine_is_authoritative_without_any_hint():
    assert evaluate("深圳电梯厂家哪家靠谱").commercial_delivery_eligible is True
    assert evaluate("电梯是什么").commercial_delivery_eligible is False
