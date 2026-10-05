from services.article_structure_features import extract_article_structure_features


def test_article_structure_detects_title_lead_body_evidence_and_conclusion_patterns():
    article = {
        "title": "2026年深圳酒店怎么选？亲子度假酒店避坑指南",
        "inline_cleaned_content": (
            "如果带孩子去深圳度假，优先看位置、亲子设施和交通便利性。"
            "建议先排除交通不便、早餐和儿童设施不明确的酒店。"
            "\n\n一、先看区域\n深圳湾适合亲子休闲，靠近地铁和商圈。"
            "\n\n二、再看设施\n儿童泳池、早餐、停车和取消政策都要核对。"
            "\n\n三、预算和案例\n亲子家庭常见预算约800元到1500元，案例显示周末价格波动更大。"
            "\n\n常见问题：预算不高怎么选？建议先排除交通不便的酒店。"
            "\n\n结论：如果第一次带孩子来深圳，建议优先选交通便利、亲子设施明确的酒店。"
        ),
    }

    features = extract_article_structure_features(article)

    assert features["structure_version"] == "article_structure_v1_2026_06_17"
    assert features["title_has_year"] is True
    assert features["title_has_city_or_region"] is True
    assert features["title_has_guide_signal"] is True
    assert features["title_question_form"] is True
    assert features["lead_answers_question"] is True
    assert features["lead_has_selection_criteria"] is True
    assert features["lead_is_generic_intro"] is False
    assert features["numbered_section_count"] >= 3
    assert features["has_faq_block"] is True
    assert features["has_checklist"] is True
    assert features["has_price_or_budget"] is True
    assert features["has_customer_case"] is True
    assert features["has_risk_or_pitfall"] is True
    assert features["conclusion_has_decision_advice"] is True
    assert features["evidence_density_score"] > 0


def test_article_structure_marks_generic_intro_without_overclaiming_answer():
    article = {
        "title": "酒店行业发展分析",
        "inline_cleaned_content": "随着互联网的发展，越来越多用户开始关注酒店服务质量。本文将进行介绍。",
    }

    features = extract_article_structure_features(article)

    assert features["lead_is_generic_intro"] is True
    assert features["lead_answers_question"] is False
    assert features["title_question_form"] is False
    assert features["conclusion_is_salesy"] is False


def test_article_structure_marks_salesy_conclusion_as_risk_not_success():
    article = {
        "title": "广州装修公司哪家好？装修避坑指南",
        "inline_cleaned_content": (
            "广州装修建议先看资质、案例和报价透明度。"
            "\n\n一、看报价\n二、看案例\n三、看合同"
            "\n\n结尾：选择我们保证30天排名第一，效果翻倍，马上咨询即可。"
        ),
    }

    features = extract_article_structure_features(article)

    assert features["lead_answers_question"] is True
    assert features["has_risk_or_pitfall"] is True
    assert features["conclusion_is_salesy"] is True
