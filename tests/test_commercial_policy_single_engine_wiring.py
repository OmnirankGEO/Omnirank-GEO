"""[Review-CTO 2026-07-23 NO-GO P1-1] 唯一引擎接线证明。

证明报价/诊断/监测/写作四入口不再各自发明标准,统一走
services.commercial_query_policy —— 报价扩词硬门委托同一引擎,
诊断/监测/写作在源码层引用同一模块。
"""
from pathlib import Path

from services.commercial_query_policy import buyer_intent_eligible, evaluate
from tools.keyword_expander import KeywordExpander

ROOT = Path(__file__).resolve().parents[1]


def test_expander_gate_delegates_to_policy_engine():
    """报价扩词 _check_geo_feasibility 与 policy 引擎逐例一致(委托关系)。"""
    samples = [
        "深圳电梯厂家哪家靠谱",
        "国产电梯和进口电梯哪个好",
        "A品牌和B品牌哪个更适合医院",
        "深圳装修公司靠谱吗",
        "预算怎么做",
        "电梯是什么",
        "激光设备",
        "GEO和SEO有什么区别",
        "创客教室激光设备采购",
        "学校激光切割机采购方案",
    ]
    expander = KeywordExpander()
    for kw in samples:
        assert expander._check_geo_feasibility(kw) == buyer_intent_eligible(kw), kw
        # evaluate() 与硬门布尔同向(品牌直问除外,这里不含品牌名)
        assert evaluate(kw).commercial_delivery_eligible == buyer_intent_eligible(kw), kw


def test_reviewer_four_counterexamples_now_correct():
    assert evaluate("国产电梯和进口电梯哪个好").commercial_delivery_eligible is True
    assert evaluate("A品牌和B品牌哪个更适合医院").commercial_delivery_eligible is True
    assert evaluate("深圳装修公司靠谱吗").commercial_delivery_eligible is True
    assert evaluate("预算怎么做").commercial_delivery_eligible is False


def _reads(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_all_four_domains_reference_the_single_module():
    # 报价扩词:委托
    assert "from services.commercial_query_policy import buyer_intent_eligible" in _reads(
        "tools/keyword_expander.py"
    )
    # 报价提交:自定义词/快照分区走 evaluate + 统一版本
    assert "from services.commercial_query_policy import" in _reads("api/selection_api.py")
    # 诊断 8 问:LLM 产出等槽修复
    assert "_enforce_commercial_questions" in _reads("tools/keyword_generator.py")
    assert "commercial_query_policy" in _reads("tools/keyword_generator.py")
    # 监测:消费端 advisory 接线
    assert "commercial_query_policy" in _reads("tools/monitoring/batch_monitor.py")
    # 写作:消费端 advisory 接线
    assert "commercial_query_policy" in _reads("writing/keyword_topic_generator.py")


def test_no_second_gate_definition_remains():
    """报价扩词不再自带独立判定规则(硬门体已迁出,只剩委托)。"""
    src = _reads("tools/keyword_expander.py")
    body = src[src.index("def _check_geo_feasibility"):]
    body = body[: body.index("async def expand_keywords_for_client")]
    # 委托体不得再含原硬门的规则常量/正则清单
    assert "knowledge_object_patterns" not in body
    assert "direct_choice_signals" not in body
    assert "buyer_intent_eligible" in body
