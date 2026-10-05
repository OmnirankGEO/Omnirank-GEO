"""CommercialQueryPolicy 合同测试 · SSOT §7 验收反例 + 指令 §六.1/六.2 矩阵。"""
import pytest

from services.commercial_query_policy import (
    INTENT_BRAND_DIRECT,
    INTENT_COMMERCIAL,
    INTENT_KNOWLEDGE,
    INTENT_SEO_FRAGMENT,
    INTENT_UNCERTAIN,
    POLICY_VERSION,
    evaluate,
    is_knowledge_or_fragment,
)

# ---- 指令 §六.1 商业通过矩阵(全部必须 eligible=True) ----
MUST_PASS = [
    "深圳电梯厂家哪家靠谱",
    "预算 3000 元以内 GEO 服务商怎么选",
    "细胞治疗隔离器品牌对比与采购建议",
    "揭阳商务酒店推荐，哪家性价比高",
    "创客教室激光设备采购",
    "学校激光切割机采购方案",
]

# ---- 指令 §六.2 必须排除付费交付 ----
MUST_EXCLUDE = [
    "电梯是什么",
    "AI 行业未来趋势",
    "就业率真实吗",
    "激光设备",
    "GEO 和 SEO 有什么区别",
]


@pytest.mark.parametrize("q", MUST_PASS)
def test_commercial_matrix_passes(q):
    d = evaluate(q)
    assert d.commercial_delivery_eligible is True, (q, d.reason_codes)
    assert d.intent_type == INTENT_COMMERCIAL
    assert d.policy_version == POLICY_VERSION
    assert not d.needs_clarification


@pytest.mark.parametrize("q", MUST_EXCLUDE)
def test_knowledge_and_fragments_excluded(q):
    d = evaluate(q)
    assert d.commercial_delivery_eligible is False, (q, d.reason_codes)
    assert d.intent_type in (INTENT_KNOWLEDGE, INTENT_SEO_FRAGMENT)
    assert is_knowledge_or_fragment(d)  # 不可人工改选回付费交付
    assert d.reason_codes  # 必须给出可解释原因


def test_seo_fragment_vs_natural_procurement():
    # 同为「激光设备」:裸词排除,自然采购问题通过 —— 双向反例(§8.1)
    assert evaluate("激光设备").intent_type == INTENT_SEO_FRAGMENT
    assert evaluate("创客教室激光设备采购").commercial_delivery_eligible is True


def test_reviewer_counter_examples_2026_07_23():
    """[Review-CTO NO-GO 返工] 实测误判 4 例全部纠正(唯一引擎裁决):
    比较/尽调类是合法商业方向(SSOT §3.1 对比/优缺点/口碑),
    方法类知识题不得因带商业名词误放。"""
    # 类别/品牌比较 → 商业(答案会给出具体选择)
    assert evaluate("国产电梯和进口电梯哪个好").commercial_delivery_eligible is True
    assert evaluate("A品牌和B品牌哪个更适合医院").commercial_delivery_eligible is True
    # 供给方信任问法 = 供应商尽调 → 商业
    d = evaluate("深圳装修公司靠谱吗")
    assert d.commercial_delivery_eligible is True
    assert d.intent_type == INTENT_COMMERCIAL
    # 方法类知识题:含"预算"也不得放行
    d = evaluate("预算怎么做")
    assert d.commercial_delivery_eligible is False
    assert d.intent_type == INTENT_KNOWLEDGE


def test_which_family_and_provider_queries_commercial():
    assert evaluate("乌鲁木齐以租代购公司哪家好").commercial_delivery_eligible is True
    # 方法论型对比仍是知识(选择标准/对比方法)
    d = evaluate("工业机器人供应商对比方法")
    assert d.commercial_delivery_eligible is False
    assert d.intent_type == INTENT_KNOWLEDGE


def test_brand_direct_is_entity_control_not_knowledge():
    # 品牌信任直问是实体识别对照,可交付,不被"靠谱吗"知识信号误杀(§4.2)
    d = evaluate("深圳绿源门窗靠谱吗", brand_name="绿源门窗")
    assert d.commercial_delivery_eligible is True
    assert d.intent_type == INTENT_BRAND_DIRECT
    # 无品牌上下文的泛求证仍是知识题
    assert evaluate("以租代购靠谱吗").commercial_delivery_eligible is False


def test_natural_entity_phrase_default_admitted():
    # 自然商业结构(城市+行业+公司)默认宽进(§2.2)
    d = evaluate("深圳办公室装修公司")
    assert d.commercial_delivery_eligible is True


def test_uncertain_goes_to_clarification_not_silent_drop():
    # 歧义项 → needs_clarification=True(§3.3:不得静默删除)
    d = evaluate("行业白皮书发布月历")
    assert d.commercial_delivery_eligible is False
    assert d.intent_type == INTENT_UNCERTAIN
    assert d.needs_clarification is True


def test_informational_hint_respected_without_commercial_text():
    d = evaluate("电梯维保周期说明", intent_hint="informational")
    assert d.commercial_delivery_eligible is False
    assert d.intent_type == INTENT_KNOWLEDGE


def test_commercial_text_overrides_informational_hint():
    # 模型误标 informational 不得静默丢商业词(§3.3)
    d = evaluate("福田电梯维保哪家靠谱", intent_hint="informational")
    assert d.commercial_delivery_eligible is True


def test_price_and_budget_questions_are_commercial():
    for q in ("杭州小程序开发多少钱", "GEO 优化报价", "3万预算内怎么选设备"):
        assert evaluate(q).commercial_delivery_eligible is True, q


def test_empty_and_whitespace():
    d = evaluate("   ")
    assert d.commercial_delivery_eligible is False
    assert "EMPTY_QUERY" in d.reason_codes


def test_as_dict_contract_fields():
    d = evaluate("深圳电梯厂家哪家靠谱").as_dict()
    for k in (
        "policy_version", "commercial_delivery_eligible", "intent_type",
        "reason_codes", "normalized_question", "needs_clarification",
    ):
        assert k in d


def test_uncertain_custom_word_not_priced_but_clarifiable():
    """[Review-CTO P1-2] needs_clarification 词不可计价(excluded),但与知识
    词不同:归入需澄清区,理由单列,澄清后可重新提交(非永久排除)。"""
    from services.commercial_query_policy import excluded_from_paid_delivery
    d = evaluate("行业白皮书发布月历")
    assert d.intent_type == INTENT_UNCERTAIN
    assert d.needs_clarification is True
    assert d.commercial_delivery_eligible is False
    # 统一"不得进入付费交付"判据:知识词 + 裸词 + 待澄清词都为真
    assert excluded_from_paid_delivery(d) is True
    assert excluded_from_paid_delivery(evaluate("电梯是什么")) is True
    assert excluded_from_paid_delivery(evaluate("深圳电梯厂家哪家靠谱")) is False
