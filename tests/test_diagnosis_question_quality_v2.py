# -*- coding: utf-8 -*-
"""[Review-CTO 2026-07-27] 诊断选词守卫二修判别锁(Owner 生产实测 diagnosis 489)。

生产原样复现的两个病:
  A. 题面鬼话:"广东省深圳市龙岗科技推广和应用服务业、跨境数字营销服务业 /
     TikTok跨境B2B外贸获客、海外社媒全案运营哪家好?" —— geo=整串地址剥不动、
     trade=industry 登记名录原文整串,拼进模板没人会这么问。
  B. 场景转化层"本层未实测":守卫补层只 append 尾部,下游 ai_test 只测 [:8],
     补的场景题全被切掉 —— 守卫没进真实执行入口。
"""
from services.diagnosis_question_quality import (
    FUNNEL_LAYERS,
    LAYER_BRAND,
    LAYER_LOCAL,
    LAYER_SCENARIO,
    build_question_templates,
    distill_trade,
    enforce_question_quality,
    normalize_city,
)

# 驰鲸 brand 712 生产原始输入
CHIJING_CITY = "广东省深圳市龙岗区"
CHIJING_INDUSTRY = "科技推广和应用服务业 / TikTok海外B2B精准获客、外贸社媒全案营销"
CHIJING_KEYWORDS = [
    "TikTok外贸B2B获客", "TikTok工厂询盘服务商", "低成本外贸获客渠道",
    "TikTok工厂代运营", "外贸TikTok广告投放公司", "五金工厂TikTok外贸获客",
]


def test_normalize_city_extracts_city_from_full_address():
    assert normalize_city(CHIJING_CITY) == "深圳", (
        "整串注册地址必须提取出城市;剥尾缀剩'广东省深圳市龙岗'就是鬼话题面的来源"
    )
    # 旧行为兼容:干净城市名不受影响
    assert normalize_city("深圳") == "深圳"
    assert normalize_city("深圳市") == "深圳"
    assert normalize_city("全国") == ""


def test_distill_trade_prefers_short_service_keyword():
    trade = distill_trade(CHIJING_INDUSTRY, CHIJING_KEYWORDS)
    assert trade in CHIJING_KEYWORDS, trade
    assert len(trade) <= 14
    # 不传核心词 → 从 industry 提炼,且绝不能是登记名录整串
    fallback = distill_trade(CHIJING_INDUSTRY)
    assert "科技推广和应用服务业" not in fallback
    assert len(fallback) <= 14


def test_templates_read_like_human_questions_not_registry_strings():
    pools = build_question_templates(
        brand_name="深圳市驰鲸科技有限公司",
        industry=CHIJING_INDUSTRY,
        city=CHIJING_CITY,
        scope="regional",
        keywords=CHIJING_KEYWORDS,
    )
    all_questions = [q for pool in pools.values() for q in pool]
    assert all_questions
    for q in all_questions:
        assert "广东省" not in q, q
        assert "科技推广和应用服务业" not in q, q
        assert "龙岗" not in q, q
    # 决策获客层模板必须长成"深圳{品类}哪家好?"这种真问法
    assert any(q.startswith("深圳") for q in pools[LAYER_LOCAL])


def test_reorder_keeps_every_layer_inside_tested_slice():
    # 复现 B:LLM 给的 8 题全是决策层(缺场景/品牌),守卫补层后下游只测前 8
    questions = [f"深圳TikTok代运营哪家好{i}？" for i in range(8)]
    types = {q: LAYER_LOCAL for q in questions}
    result = enforce_question_quality(
        questions, types,
        brand_name="驰鲸科技", industry=CHIJING_INDUSTRY, city=CHIJING_CITY,
        business_scope="regional", engine_count=5,
        keywords=CHIJING_KEYWORDS, max_tested_questions=8,
    )
    tested = result["questions"][:8]
    tested_layers = {result["question_types"][q] for q in tested}
    for layer in FUNNEL_LAYERS:
        assert layer in tested_layers, (
            f"层 {layer} 不在前 8 题里 —— 报告会显示'本层未实测',守卫等于没做"
        )
    # 不丢词:原 8 题仍全部在输出里(只是排序变了)
    assert set(questions) <= set(result["questions"])


def test_no_reorder_when_max_not_given():
    # 兼容锁:不传 max_tested_questions → 行为与旧版一致(只追加不重排)
    questions = [f"深圳TikTok代运营哪家好{i}？" for i in range(8)]
    types = {q: LAYER_LOCAL for q in questions}
    result = enforce_question_quality(
        questions, types,
        brand_name="驰鲸科技", industry=CHIJING_INDUSTRY, city=CHIJING_CITY,
        business_scope="regional", engine_count=5,
    )
    assert result["questions"][:8] == questions, "不传 max 时原顺序必须原样保留"
