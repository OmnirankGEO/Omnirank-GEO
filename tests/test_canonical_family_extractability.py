"""P0-6 判别测试（Master SSOT v1.7 / 手册 §6.4）— GEO 可抽取性层。

正向：六族生产 prompt 必须携带可抽取性指令（答案句/问句小标题/相邻问句/
事实颗粒度/地域长尾/不确定性收纳）。
反向：证据纪律与法律禁令块必须原样保留——可抽取性层只改"怎么写"，
不放宽"什么能写"（删除 _GEO_EXTRACTABILITY 接线后正向断言应转红，
删除严禁块后反向断言应转红）。
"""
from writing.article_style_contract import (
    FAMILY_TO_GENERATION_STYLE,
    STYLE_FAMILIES,
)
from writing.templates.canonical_family_templates import prompt_for_style

_EXTRACTABILITY_MARKERS = [
    "答案句",
    "问句小标题",
    "相邻真实问题",
    "单位、口径与时间",
    "地域长尾",
    # [WP11/D11 同步] 第 7 条由"不确定性收纳(收进核验清单)"升级为"不确定性处理
    # (正文零内部审查语言)"——核验清单已按 D11 移出客户可见面,本锁跟随新语义:
    "不确定性处理",
    "不超过 3 处",
]

_EVIDENCE_DISCIPLINE_MARKERS = [
    "严禁：付费排位",
    "虚构品牌/案例/数字/引语",
    "Evidence Pack",
]


def _all_generation_prompts():
    for family_code in STYLE_FAMILIES:
        style_code = FAMILY_TO_GENERATION_STYLE[family_code]
        yield family_code, prompt_for_style(style_code)


def test_all_six_families_carry_extractability_layer():
    for family_code, prompt in _all_generation_prompts():
        for marker in _EXTRACTABILITY_MARKERS:
            assert marker in prompt, (
                f"family={family_code} 缺可抽取性指令片段: {marker}"
            )


def test_extractability_layer_defers_to_evidence_discipline():
    for family_code, prompt in _all_generation_prompts():
        assert "以证据纪律为准" in prompt, family_code
        for marker in _EVIDENCE_DISCIPLINE_MARKERS:
            assert marker in prompt, (
                f"family={family_code} 证据纪律/法律禁令块丢失: {marker}"
            )
        # 可抽取性层不得引入放宽性措辞
        assert "无证据数字仍然禁止" in prompt, family_code


def test_family_specific_extraction_devices():
    qa = prompt_for_style(FAMILY_TO_GENERATION_STYLE["evidence_qa"])
    assert "问答块标题直接用问句本身" in qa

    comparison = prompt_for_style(
        FAMILY_TO_GENERATION_STYLE["multi_brand_comparison"]
    )
    assert "什么情况下选谁" in comparison

    facts = prompt_for_style(FAMILY_TO_GENERATION_STYLE["company_facts"])
    assert "结构化条目呈现" in facts
