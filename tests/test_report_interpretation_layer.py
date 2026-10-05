"""GEO diagnosis report interpretation layer guards.

The client-facing diagnosis report should explain evidence and business meaning
without turning the report tail into a sales plan or fixed-cycle promise.
"""
import inspect


FORBIDDEN_CLIENT_TERMS = [
    "30 天",
    "30天",
    "30 / 60 / 90",
    "ROI 区间",
    "当前预算",
    "预算建议",
    "月费",
    "投入测算",
    "预约 GEO",
    "规划师",
    "行动优先级",
    "30 天验收",
    "执行计划",
    "保证上榜",
    "确保推荐",
    "首月方案",
]


def _assert_no_client_sales_terms(text: str) -> None:
    for term in FORBIDDEN_CLIENT_TERMS:
        assert term not in text, f"客户诊断报告不应出现销售/周期承诺口径: {term}\n{text[:1200]}"


def _assert_no_actiony_report_framing(text: str) -> None:
    forbidden = [
        "下一步先做什么",
        "先补 AI 可引用素材,再做持续监测",
        "决策第一钩子",
        "越往后你越被动",
        "cdv2-final-cta",
    ]
    for term in forbidden:
        assert term not in text, f"客户诊断报告应保持解读口径,不应出现偏行动/CTA表达: {term}"


def _layers():
    return [
        {
            "key": "brand",
            "label": "品牌认知层",
            "weight": 20,
            "detected": 8,
            "total": 10,
            "rate": 0.8,
            "rate_pct": 80,
            "score": 16,
            "desc": "客户已经知道你",
            "business": "老客户能找到",
            "data_sufficient": True,
        },
        {
            "key": "local",
            "label": "决策获客层",
            "weight": 40,
            "detected": 1,
            "total": 10,
            "rate": 0.1,
            "rate_pct": 10,
            "score": 4,
            "desc": "客户还不知道你",
            "business": "新客候选不足",
            "data_sufficient": True,
        },
        {
            "key": "scenario",
            "label": "场景转化层",
            "weight": 40,
            "detected": 0,
            "total": 10,
            "rate": 0.0,
            "rate_pct": 0,
            "score": 0,
            "desc": "客户比较方案",
            "business": "高意向问题没有证据",
            "data_sufficient": True,
        },
    ]


def _report_data():
    return {
        "brand_name": "测试品牌",
        "industry": "装修",
        "city": "深圳",
        "total_score": 42,
        "funnel_score": {
            "total_score": 42,
            "level": "边缘级",
            "level_meta": {"business_meaning": "有基础但断档"},
            "layers": _layers(),
        },
        "suggestions": {},
    }


def _base_modules():
    return {
        "1": {"module": 1, "rendered_md": "", "conclusion_text": "品牌词有基础,新客入口断档。"},
        "1_interpretation": {
            "module": "1_interpretation",
            "headline": "不是没有基础,而是新客决策入口还没有被 AI 稳定承接。",
            "reading_points": [
                "不要只看总分,先看 AI 在哪一层替品牌说话。",
                "再核对原始回答和证据来源。",
                "样本不足处只做保守判断。",
            ],
            "business_translation": "品牌词有基础,但行业推荐和高意向问题还没有稳定进入候选。",
            "data_caveat": "本次有效证据偏少,结论需要保守理解。",
            "rendered_md": (
                "## 这份报告应该怎么读\n\n"
                "不是没有基础,而是新客决策入口还没有被 AI 稳定承接。\n"
            ),
        },
        "2": {
            "module": 2,
            "summary": {"total_score": 42, "max_score": 100, "level": "边缘级", "level_meta": {}},
            "funnel": {"total_score": 42, "level": "边缘级", "layers": _layers()},
            "layers": _layers(),
            "dimensions": [],
            "keyword_strata": [],
        },
        "3": {"module": 3, "evidences": [], "evidence_count": {"A": 0, "B": 0, "C": 0}, "evidence_total": 3},
        "3_raw": {"module": "3_raw", "tests": [], "rendered_md": ""},
        "3_authority": {"module": "3_authority", "available": False, "rendered_md": ""},
        "4": {"module": 4, "top_competitors": [["竞品A", 2]], "co_citation_total": 8, "declared_competitors": []},
        "5": {
            "module": 5,
            "gap_to_p50": 28,
            "gap_to_p90": 43,
            "est_aiq_lift_pct_low": 12,
            "est_aiq_lift_pct_high": 20,
            "rendered_md": "## 机会解读\n\n差距说明当前还有讨论空间,不代表固定收益。\n",
        },
        "6": {"module": 6, "todos": [], "personalized_actions": [], "rendered_md": ""},
        "7": {"module": 7, "rendered_md": "## 报告解读提纲\n\n围绕证据、趋势和数据边界逐项确认。\n"},
        "8": {"module": 8, "rendered_md": "## 如需进一步沟通,建议围绕这份报告逐项解读\n"},
        "0": {"module": 0, "rendered_md": "", "groups": []},
    }


def _meta():
    return {
        "brand_name": "测试品牌",
        "total_score": 42,
        "level": "边缘级",
        "industry": "装修",
        "city": "深圳",
        "created_at": "2026-06-09",
        "diagnosis_id": 1001,
    }


def _completeness():
    return {"score": 55, "level": "中", "groups": [], "impact_notes": [], "missing_summary": "资料基本可判断"}


def test_build_interpretation_explains_new_customer_gap_without_sales_terms():
    from services.report_writer_v2 import build_module_1_interpretation

    module = build_module_1_interpretation(_report_data(), modules={"3": {"evidence_total": 3}})
    md = module["rendered_md"]

    assert "这份报告应该怎么读" in md
    assert "新客决策入口" in md
    assert "不是没有基础" in md
    assert "证据" in md
    assert "保守" in md
    _assert_no_client_sales_terms(md)


def test_diagnosis_client_tail_is_interpretation_not_sales_plan():
    from services.report_writer_v2 import (
        build_module_5_opportunity,
        build_module_7_plan_30d,
        build_module_8_offer,
    )

    text = "\n".join(
        [
            build_module_5_opportunity(_report_data())["rendered_md"],
            build_module_7_plan_30d(_report_data(), report_type="diagnosis")["rendered_md"],
            build_module_8_offer(_report_data(), report_type="diagnosis", audience="client")["rendered_md"],
        ]
    )

    assert "报告解读" in text or "逐项解读" in text
    _assert_no_client_sales_terms(text)


def test_assemble_order_places_interpretation_after_cover():
    import services.report_writer_v2 as rw

    src = inspect.getsource(rw.assemble_report_v2)
    assert '"1_interpretation"' in src
    assert '"1", "1_interpretation", "2"' in src


def test_customer_decision_page_renders_interpretation_after_four_things():
    from services.report_html_renderer import render_customer_decision_page_html

    html = render_customer_decision_page_html(
        meta=_meta(),
        modules_jsonb={"client": {"modules": _base_modules()}, "funnel": {"total_score": 42, "level": "边缘级", "layers": _layers()}},
        completeness=_completeness(),
        audience="client",
    )

    assert "客户最该先看 4 件事" in html
    assert "这份报告应该怎么读" in html
    assert "不要只看总分" in html
    assert html.index("客户最该先看 4 件事") < html.index("这份报告应该怎么读")
    assert html.index("这份报告应该怎么读") < html.index("3 层漏斗 · 你的真实状态")
    _assert_no_client_sales_terms(html)
    _assert_no_actiony_report_framing(html)


def test_customer_decision_page_uses_neutral_final_note_and_evidence_legend():
    from services.report_html_renderer import render_customer_decision_page_html

    html = render_customer_decision_page_html(
        meta=_meta(),
        modules_jsonb={"client": {"modules": _base_modules()}, "funnel": {"total_score": 42, "level": "边缘级", "layers": _layers()}},
        completeness=_completeness(),
        audience="client",
    )

    assert "cdv2-final-note" in html
    assert "直接证据:AI 直接提到品牌、竞品或结论" in html
    assert "参考证据:与品牌或行业相关,但需要结合上下文" in html
    assert "待验证线索:目前只能作为后续核验方向" in html
    assert "Evidence A/B/C" not in html
    assert "报告到此结束" in html
    _assert_no_client_sales_terms(html)
    _assert_no_actiony_report_framing(html)


def test_customer_decision_page_renders_safe_tail_modules_without_legacy_terms():
    from services.report_html_renderer import render_customer_decision_page_html

    modules = _base_modules()
    modules["6"] = {
        "module": 6,
        "rendered_md": "## 报告观察清单\n\n- 优先核对 AI 没有稳定提及品牌的高意向问题。\n",
    }
    modules["8"] = {
        "module": 8,
        "rendered_md": "## 报告局限说明\n\n本报告代表本次采样窗口,适合逐项解读,不代表执行承诺。\n",
    }

    html = render_customer_decision_page_html(
        meta=_meta(),
        modules_jsonb={"client": {"modules": modules}, "funnel": {"total_score": 42, "level": "边缘级", "layers": _layers()}},
        completeness=_completeness(),
        audience="client",
    )

    assert "05 · 机会解读" in html
    assert "差距说明当前还有讨论空间" in html
    assert "06 · 报告观察清单" in html
    assert "高意向问题" in html
    assert "07 · 报告解读提纲" in html
    assert "围绕证据、趋势和数据边界" in html
    assert "08 · 报告局限说明" in html
    assert "本报告代表本次采样窗口" in html
    _assert_no_client_sales_terms(html)
    _assert_no_actiony_report_framing(html)


def test_customer_decision_page_filters_legacy_tail_modules():
    from services.report_html_renderer import render_customer_decision_page_html

    modules = _base_modules()
    modules["5"] = {"module": 5, "rendered_md": "## 机会估算\n\nROI 区间预估(30 天): 20%。\n"}
    modules["6"] = {"module": 6, "rendered_md": "## 行动优先级(Top 10)\n\n30 天验收标准。\n"}
    modules["7"] = {"module": 7, "rendered_md": "## 30 天行动计划\n\n预约 GEO 规划师。\n"}
    modules["8"] = {"module": 8, "rendered_md": "## 方案承接\n\n约 400 积分 · 客户版(精简)。\n"}

    html = render_customer_decision_page_html(
        meta=_meta(),
        modules_jsonb={"client": {"modules": modules}, "funnel": {"total_score": 42, "level": "边缘级", "layers": _layers()}},
        completeness=_completeness(),
        audience="client",
    )

    assert "05 · 机会解读" in html
    assert "06 · 报告观察清单" in html
    assert "07 · 报告解读提纲" in html
    assert "08 · 报告局限说明" in html
    for term in ["ROI 区间", "30 天行动计划", "30 天验收", "预约 GEO", "积分", "客户版(精简)", "方案承接"]:
        assert term not in html
    _assert_no_client_sales_terms(html)
    _assert_no_actiony_report_framing(html)


def test_render_report_html_client_path_has_interpretation_not_30_day_plan():
    from services.report_html_renderer import render_report_html

    html = render_report_html(
        meta=_meta(),
        modules_jsonb={"client": {"modules": _base_modules()}, "funnel": {"total_score": 42, "level": "边缘级", "layers": _layers()}},
        completeness=_completeness(),
        audience="client",
    )

    assert "报告解读" in html
    assert "这份报告应该怎么读" in html
    assert "围绕证据、趋势和数据边界" in html
    _assert_no_client_sales_terms(html)


def test_legacy_client_modules_do_not_leak_old_30_day_plan():
    from services.report_html_renderer import render_customer_decision_page_html, render_report_html

    modules = _base_modules()
    modules.pop("1_interpretation", None)
    modules["7"] = {
        "module": 7,
        "rendered_md": "## 30 天行动计划\n\n### 30 天验收标准\n\n预约 GEO 规划师看 ROI 区间。\n",
    }

    payload = {
        "client": {"modules": modules},
        "funnel": {"total_score": 42, "level": "边缘级", "layers": _layers()},
    }

    decision_html = render_customer_decision_page_html(
        meta=_meta(),
        modules_jsonb=payload,
        completeness=_completeness(),
        audience="client",
    )
    full_html = render_report_html(
        meta=_meta(),
        modules_jsonb=payload,
        completeness=_completeness(),
        audience="client",
    )

    assert "这份报告应该怎么读" in decision_html
    assert "这份报告应该怎么读" in full_html
    _assert_no_client_sales_terms(decision_html)
    _assert_no_client_sales_terms(full_html)


def test_assembled_client_markdown_has_no_hidden_sales_plan_modules():
    from services.report_writer_v2 import assemble_report_v2

    data = _report_data()
    data["diagnosis_data"] = {"ai_visibility_data": {"detail_table": []}}
    data["suggestions"] = {
        "high_priority": [{"issue": "缺少官网 FAQ", "action": "补充 FAQ 和案例页"}]
    }

    result = assemble_report_v2(data, brand_id=999999, report_type="diagnosis", audience="client")
    md = result["full_markdown"]

    assert "这份报告应该怎么读" in md
    assert "报告观察清单" in md
    assert "报告解读提纲" in md
    assert "优先核对什么" in md
    _assert_no_client_sales_terms(md)
    _assert_no_actiony_report_framing(md)
