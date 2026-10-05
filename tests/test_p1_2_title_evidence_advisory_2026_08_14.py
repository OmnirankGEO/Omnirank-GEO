# -*- coding: utf-8 -*-
"""P1-2 · 题证一致性 advisory + 四档文案 · 判别锁(2026-08-14)。

变异点:
  M1 拆 title 缺省等价(无 title 也出新 findings)→ test_no_title_no_new_codes 红;
  M2 把新 findings 升 hard(违反红线 4)→ test_new_codes_never_hard 红;
  M3 拆绑定 advisory → test_unverified_binding_yields_advisory 红;
  M4 拆四档文案(改回「审核通过」)→ test_four_tier_copy_in_frontend 红。
"""
from __future__ import annotations

from pathlib import Path

from writing.evidence_precision_policy import evaluate_evidence_precision

ROOT = Path(__file__).resolve().parents[1]

_NEW_CODES = {"entity_binding_unverified", "entity_binding_isolated", "title_evidence_alignment_weak"}


def _pack(binding: str | None = None, n: int = 1) -> dict:
    items = []
    for i in range(n):
        item = {"evidence_id": f"EV-{i+1:03d}", "relationship": "support",
                "claim": "无关领域的一条资料", "title": "无关领域资料",
                "url": f"https://x.com/{i}", "verification_status": "search_result_only"}
        if binding:
            item["binding_state"] = binding
        items.append(item)
    return {"items": items}


def _codes(assessment) -> set[str]:
    return {f.code for f in assessment.warnings} | {f.code for f in assessment.hard}


def test_no_title_no_new_codes() -> None:
    """反向对照:title 缺省 = 行为与旧签名逐字一致(不长任何新 code)。"""
    assessment = evaluate_evidence_precision("正文", _pack("entity_unverified", 3), {})
    assert not (_codes(assessment) & _NEW_CODES)


def test_unverified_binding_yields_advisory() -> None:
    assessment = evaluate_evidence_precision(
        "正文", _pack("entity_unverified"), {}, title="观光电梯怎么选",
    )
    hit = [f for f in assessment.warnings if f.code == "entity_binding_unverified"]
    assert hit and hit[0].severity == "advisory"
    assert hit[0].evidence_ids, "A1 卡定位不到具体证据条目(局部化被破)"


def test_isolated_binding_informational() -> None:
    assessment = evaluate_evidence_precision(
        "正文", _pack("different_entity"), {}, title="观光电梯怎么选",
    )
    assert any(f.code == "entity_binding_isolated" for f in assessment.warnings)


def test_alignment_weak_only_when_zero_overlap() -> None:
    weak = evaluate_evidence_precision("正文", _pack(None, 3), {}, title="观光电梯怎么选")
    assert any(f.code == "title_evidence_alignment_weak" for f in weak.warnings)
    # 反向对照:证据与题面相关 → 不报
    pack = {"items": [{
        "evidence_id": "EV-001", "relationship": "support",
        "claim": "观光电梯 选购 指南", "title": "观光电梯选购调研",
        "url": "https://x.com/1", "verification_status": "search_result_only",
    }] * 3}
    ok = evaluate_evidence_precision("正文", pack, {}, title="观光电梯怎么选")
    assert not any(f.code == "title_evidence_alignment_weak" for f in ok.warnings)


def test_small_pack_not_judged() -> None:
    # <3 条不判题证一致(样本太小,判了就是噪音墙)
    tiny = evaluate_evidence_precision("正文", _pack(None, 2), {}, title="观光电梯怎么选")
    assert not any(f.code == "title_evidence_alignment_weak" for f in tiny.warnings)


def test_new_codes_never_hard() -> None:
    """红线 4:证据不足不得硬阻断 —— 新 code 永远不进 hard(eligible 不受影响)。"""
    assessment = evaluate_evidence_precision(
        "正文", _pack("entity_unverified", 3), {}, title="观光电梯怎么选",
    )
    assert not ({f.code for f in assessment.hard} & _NEW_CODES)
    for f in assessment.warnings:
        if f.code in _NEW_CODES:
            assert f.severity == "advisory"


def test_friendly_titles_registered() -> None:
    from services.article_findings_aggregate import _FRIENDLY_TITLES

    for code in _NEW_CODES:
        assert code in _FRIENDLY_TITLES, f"{code} 没有人话标题(工程术语会裸露给用户)"


def test_persistence_paths_pass_title() -> None:
    """接线锁:落库路径都把 title 传给了 evaluate_evidence_precision。"""
    for rel in ("writing/article_writer.py", "tools/article_generator.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "evaluate_evidence_precision(" in src and "title=str(" in src, rel
    ags = (ROOT / "writing/article_generator_service.py").read_text(encoding="utf-8")
    assert ags.count("title=str(article") + ags.count("title=str(article_v2") >= 4


def test_four_tier_copy_in_frontend() -> None:
    """四档文案(frontend_state_copy §1):approved 词不面向用户;operator_hard 不恐吓。"""
    pc = (ROOT / "frontend/src/pages/Publishing/PublishCenter.tsx").read_text(encoding="utf-8")
    assert "已生成 · 可先看稿" in pc
    assert "可进入投放准备" in pc
    assert "需要你确认一处问题" in pc
    # 反向:徽章函数不再产出旧文案
    assert "未审核 · 可发布" not in pc
    wh = (ROOT / "frontend/src/pages/Writing/WritingHall.tsx").read_text(encoding="utf-8")
    assert "已生成 · 可先看稿" in wh and "可进入投放准备" in wh
