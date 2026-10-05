# -*- coding: utf-8 -*-
"""§6A Prompt 合同一致性 + §10 六文体规格卡真正进最终 Prompt · 判别锁。

背景(最终接管工单 §6A):同一 prompt 里两条指令互相抵消是已实证的病 ——
证据合同第 2/4 条禁「待核验」进正文,旧第 7 条却要求标出待核验项;
common_rules 曾同时禁止和要求「需进一步核验」。抵消的结果是模型退化成
「公开记录:XX简介」这类空标注(evidence-to-article-gap 2026-08-09 实证)。

本文件把「最终渲染出来的 prompt 文本」当被测对象:所有断言打在渲染产物上,
不打在常量定义处 —— 常量对了、拼装时被另一段合同抵消,这里照样红。
"""
from __future__ import annotations

import re

import pytest

from writing.article_style_contract import STYLE_FAMILIES
from writing.evidence_first_policy import compose_evidence_first_prompt
from writing.evidence_precision_policy import render_evidence_precision_prompt
from writing.source_disclosure_style import SOURCE_DISCLOSURE_PROMPT
from writing.templates.canonical_family_templates import (
    build_article_type_spec_block,
)
from writing.templates.common_rules import COMMON_GUARDRAILS

def _clauses(corpus: str) -> list[str]:
    """把 prompt 语料切成**条款**:续行(行首空白)并回上一行 ——
    一条合同条款可能折行,禁止词与被禁短语常不在同一物理行。"""
    out: list[str] = []
    for line in corpus.splitlines():
        if line[:1].isspace() and out:
            out[-1] += " " + line.strip()
        else:
            out.append(line)
    return out


PACK = {"version": "v1", "items": [
    {"evidence_id": "EV-001", "relationship": "support",
     "verification_status": "search_result", "title": "行业观察",
     "url": "https://e.com/1", "publisher": "中国电梯", "published_at": "2025-03",
     "claim": "交付率", "scope": "深圳", "excerpt": "……"},
]}


def _final_prompt_corpus() -> str:
    """按生成主链的真实拼装顺序,渲染一份「最终 prompt 语料」。"""
    parts = [
        compose_evidence_first_prompt("基础写作指令", "deep_ranking"),
        COMMON_GUARDRAILS,
        SOURCE_DISCLOSURE_PROMPT,
        render_evidence_precision_prompt(PACK, {"brand_name": "测试品牌"}),
    ]
    return "\n\n".join(parts)


# ------------------------------------------------------- §6A 矛盾对逐一钉死
def test_no_instruction_requires_pending_verification_in_body() -> None:
    """「待核验」只许以**禁止语境**出现:任何一处要求写它,当场红。"""
    corpus = _final_prompt_corpus()
    for m in re.finditer(r".{14}待核验", corpus):
        window = m.group(0)
        assert re.search(r"不写|不得|禁止|不出现|不要", window), (
            f"最终 prompt 仍有要求写「待核验」的指令:…{window}…"
        )
    # 旧第 7 条的原话形态必须已消失
    assert "标出待核验项" not in corpus


def test_no_instruction_requires_insufficient_evidence_disclaimer() -> None:
    """「尚无足够公开证据/需进一步核验」不再是要求写的话术。

    判据按**整行**看禁止语境(窗口判据被实测证伪:禁止词离短语超过窗口宽
    就误红)——一行是一条合同条款,条款级语义完整。
    """
    corpus = _final_prompt_corpus()
    assert "明确写“尚无足够公开证据”" not in corpus
    for phrase in ("尚无足够公开证据", "需进一步核验"):
        for line in _clauses(corpus):
            if phrase not in line:
                continue
            assert re.search(r"不写|不得|禁止|不要|严禁", line), (
                f"仍有要求写「{phrase}」的指令:{line!r}"
            )


def test_evidence_id_rules_do_not_contradict() -> None:
    """编号纪律唯一口径:EV/BF 编号不进正文;不存在「可输出 Evidence ID」。"""
    corpus = _final_prompt_corpus()
    assert "可输出 Evidence ID" not in corpus
    assert "不要写 EV-" in corpus or "不得把 Evidence ID" in corpus


def test_factual_boundary_qualifiers_are_preserved() -> None:
    """§6A 保留侧(必须不误删):「可能/推测/在该样本内」等真实限定词
    的保留条款必须还在 —— 反自曝不等于删掉事实边界。"""
    corpus = _final_prompt_corpus()
    assert "限定词" in corpus and "保留" in corpus


def test_self_disclosure_ban_and_downgrade_ladder_coexist() -> None:
    """禁自曝与降级阶梯必须同时在场:只禁不给出路 = 逼模型编造。"""
    corpus = _final_prompt_corpus()
    assert "企业提交资料" in corpus and "换信源" in corpus
    assert "整条不写" in corpus or "不写这条" in corpus


def test_no_stop_writing_instruction_remains() -> None:
    """出稿硬门已废:最终 prompt 不得再有「停止出稿/返回待补材料」类指令。
    (「不得停止出稿、不得输出"待补材料"」这类**禁止**表述是正确形态,放行。)"""
    corpus = _final_prompt_corpus()
    for phrase in ("停止出稿", "待补材料"):
        for line in _clauses(corpus):
            if phrase not in line:
                continue
            assert re.search(r"不得|不许|禁止", line), (
                f"仍有「{phrase}」类停写指令:{line!r}"
            )
    # 元判据:降级阶梯必须在场(禁令的替代出路),且旧「出稿硬门」标题已不存在
    # (变异实测:只查阶梯正文不查标题,标题被换回「出稿硬门」照样绿 —— 补上)
    assert "降级阶梯" in corpus
    assert "出稿硬门" not in corpus, "「出稿硬门」标题复活 —— 停写制度回潮"


def test_conclusion_entities_need_support_fact_clause_present() -> None:
    """结论型实体必须挂支持事实的条款在场(变异 M47 实测:此前无锁)。
    同时三选一出路必须跟着(收短/降背景/移出),不许整块删。"""
    corpus = _final_prompt_corpus()
    assert "必须至少有一项可核验的支持事实" in corpus
    assert "收短" in corpus and "背景" in corpus


# ------------------------------------------------------- §10 规格卡进最终 prompt
@pytest.mark.parametrize("family", sorted(STYLE_FAMILIES.keys()))
def test_all_six_families_render_spec_cards(family: str) -> None:
    block = build_article_type_spec_block(
        family, engines=("deepseek", "kimi"), verified_entity_count=5,
    )
    assert block and "文体规格卡" in block, f"{family} 规格卡渲染为空"


def test_six_families_cover_workorder_taxonomy() -> None:
    """工单 §10 的六类逐一有承载族(名称按仓内 family 码)。"""
    fams = set(STYLE_FAMILIES.keys())
    assert fams == {
        "multi_brand_comparison",   # 1 多品牌比较/榜单
        "implementation_guide",     # 2 方法与实施指南
        "case_data_roi",            # 3 案例与结果
        "evidence_qa",              # 4 证据型问答
        "trend_policy_risk",        # 5 趋势、政策与风险
        "company_facts",            # 6 企业事实与品牌说明
    }


def test_spec_card_block_is_wired_into_generator_prompt() -> None:
    """接线锁:规格卡块真的拼进 system_prompt(打在调用点,不打在函数存在)。"""
    import inspect

    import writing.article_generator_service as m

    src = inspect.getsource(m)
    anchor = src.find("_spec_card_block = build_article_type_spec_block(")
    assert anchor > 0, "生成器没调用规格卡渲染"
    wiring = src[anchor:anchor + 600]
    assert 'system_prompt = system_prompt + "\\n\\n" + _spec_card_block' in wiring, (
        "规格卡渲染了但没拼进 system_prompt —— 接线断"
    )


def test_spec_cards_are_defaults_not_quotas() -> None:
    """§10 反向:规格卡是写作默认,不是配额/长度硬门 —— 不得出现
    「必须达到 N 字」「不足即废稿」类硬门话术。"""
    for family in STYLE_FAMILIES:
        block = build_article_type_spec_block(
            family, engines=("deepseek",), verified_entity_count=5,
        ) or ""
        assert "废稿" not in block
        assert not re.search(r"必须(?:达到|凑足|写满)\s*\d+\s*字", block), (
            f"{family} 规格卡出现字数硬门"
        )
