"""诊断 8 问商业资格测试 · SSOT geo-commercial-intent-governance-v1.0 §4.2。

- 8 问兜底模板必须 8/8 通过 CommercialQueryPolicy(不得被知识题补槽);
- 品牌直问仅 1 个,作为实体识别对照(brand_direct),不挤占推荐位;
- brand_api 探测词生成提示词不得再把知识题当正面示例。
"""
from pathlib import Path

from services.commercial_query_policy import (
    INTENT_BRAND_DIRECT,
    evaluate,
)
from tools.keyword_generator import _fallback_business_context

ROOT = Path(__file__).resolve().parents[1]


def _assert_eight_questions_commercial(brand_name: str, industry: str, keywords: list[str]):
    ctx = _fallback_business_context(brand_name, industry, keywords)
    questions = list(ctx.get("real_user_questions") or [])
    assert len(questions) == 8, f"应为 8 问,实得 {len(questions)}: {questions}"
    brand_direct_count = 0
    for question in questions:
        decision = evaluate(question, brand_name=brand_name)
        assert decision.commercial_delivery_eligible is True, (
            f"诊断问被判无商业资格: {question} · {decision.reason_codes}"
        )
        if decision.intent_type == INTENT_BRAND_DIRECT:
            brand_direct_count += 1
    # 品牌直问 = 实体识别对照,只允许 1 个,不挤占推荐/比较问题(§4.2)
    assert brand_direct_count <= 1, f"品牌直问超额: {brand_direct_count}"


def test_local_fallback_eight_questions_all_commercial():
    _assert_eight_questions_commercial("绿源门窗", "门窗定制", ["深圳门窗定制"])


def test_national_fallback_eight_questions_all_commercial():
    _assert_eight_questions_commercial("云途科技", "SaaS 营销系统", ["SaaS 营销系统"])


def test_brand_api_prompt_no_longer_teaches_knowledge_examples():
    src = (ROOT / "api" / "brand_api.py").read_text(encoding="utf-8")
    # 旧正面示例(知识题占名额)必须移除
    assert '业务对比/服务问询 2-4 个:"GEO 和 SEO 区别"' not in src
    assert '"AI 搜索优化怎么做" / "社媒代运营哪家靠谱"' not in src
    # 新正面示例 = 供应商比较/价格问询
    assert "供应商比较/价格问询" in src
    assert "GEO 优化服务商哪家靠谱" in src
    # 知识题必须出现在禁止区,并给出理由
    assert "纯知识/百科题" in src
    # 自然排名/推荐问句明确合法(不再被当 SEO 词一刀切)
    assert "自然的排名/推荐/对比/价格问句完全合法且核心" in src


def test_llm_knowledge_questions_are_slot_repaired_not_dropped():
    """[Review-CTO P1-1b] LLM 返回的知识题被等槽换成商业题,数量不缩减。"""
    from tools.keyword_generator import _enforce_commercial_questions

    parsed = {
        "real_user_questions": [
            "GEO和SEO有什么区别",          # 知识题 → 应被换
            "深圳装修公司哪家好",           # 商业题 → 保留
            "AI行业未来趋势",              # 知识题 → 应被换
            "预算怎么做",                  # 知识题 → 应被换
        ],
        "question_types": {
            "GEO和SEO有什么区别": "super_tier1",
            "深圳装修公司哪家好": "regional_industry",
            "AI行业未来趋势": "super_tier1",
            "预算怎么做": "super_tier1",
        },
    }
    out = _enforce_commercial_questions(parsed, "示例品牌", "装修", ["深圳装修"])
    from services.commercial_query_policy import evaluate
    questions = out["real_user_questions"]
    # 数量不缩减(§3.3)
    assert len(questions) == 4
    # 保留的商业题仍在
    assert "深圳装修公司哪家好" in questions
    # 全部满足商业资格(知识题已被等槽换掉)
    assert all(evaluate(q, brand_name="示例品牌").commercial_delivery_eligible for q in questions)
    # question_types 与题目对齐
    assert set(out["question_types"].keys()) == set(questions)
