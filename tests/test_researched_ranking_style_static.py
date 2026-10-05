from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ranking_generation_instruction_uses_evidence_gate_not_score_table():
    source = (ROOT / "writing" / "article_generator_service.py").read_text(encoding="utf-8")

    assert "评分表+3痛点+2案例" not in source
    assert "五档能力评级" not in source
    assert "五星/四星半" not in source
    assert "evaluate_content_trust" in source
    assert "EvidenceFirstViolation" in source
    assert "persistence boundary" in source


def test_ranking_registry_no_longer_points_to_legacy_score_prompt():
    """[命名 SSOT 2026-07-28] 原断言要求 registry 的 prompt_source 指向
    evidence_ranking_template。核实后发现 ``prompt_source`` **全仓零消费方**，
    是纯历史元数据 —— 断言它的取值等于给死字段背书，会让人误以为改它能改 prompt。

    改锁真正重要的两件事：① 绝不指回含造分段的 V9；② 现役 prompt 的唯一解析
    路径确实是 canonical family 合同。
    """
    source = (ROOT / "writing" / "style_registry.py").read_text(encoding="utf-8")

    assert '"ranking_v2": RANKING_PROMPT_V9' not in source
    assert "RANKING_PROMPT_V9" not in source
    # 现役解析链：_get_default_prompt → canonical_family_templates.prompt_for_style
    assert "canonical_family_templates import prompt_for_style" in source


def test_legacy_article_writer_ranking_prompt_uses_same_evidence_template():
    source = (ROOT / "writing" / "article_writer.py").read_text(encoding="utf-8")

    assert "RANKING_MAIN_PROMPT = RANKING_PROMPT_V9" not in source
    assert "EVIDENCE_RANKING_PROMPT" in source
    assert "get_scoring_template" not in source
    assert "RANKING_MAIN_PROMPT = EVIDENCE_RANKING_PROMPT" in source
    assert "RANKING_LIST_PROMPT = EVIDENCE_RANKING_PROMPT" in source
