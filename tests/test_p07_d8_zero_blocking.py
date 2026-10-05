"""[WP9-P0-7 · D8] 零阻断 / 唯一发布点法律门 / 人审推荐制 判别(轻量·不依赖全生成栈)。

锁定:
- ③ 发布审核门代码默认开(env 缺省即在岗:文章层零阻断后,它是唯一残留法律硬点);
- ④ HUMAN_DECISIONS 含 skipped(推荐人审可明示跳过);
- ④ evaluate_publication_eligibility 对 company_facts/brand_softarticle 由硬拦改为 overridable
  的 recommended_review(源锁);skip 只放行 recommended_review,不越 legal/platform(set_human_review 守卫);
- ① 文章层不再对法律 hard raise EvidenceFirstViolation(源锁:save 路径改存草稿带 findings)。
"""
import inspect
import os

import services.article_review_gate as gate


def test_publication_gate_default_enabled(monkeypatch):
    # ③ env 缺省 → 默认开(唯一防线在岗);仍可显式关。
    monkeypatch.delenv("GEO_ARTICLE_PUBLICATION_REVIEW_GATE_ENABLED", raising=False)
    assert gate.is_publication_review_gate_enabled() is True
    monkeypatch.setenv("GEO_ARTICLE_PUBLICATION_REVIEW_GATE_ENABLED", "false")
    assert gate.is_publication_review_gate_enabled() is False


def test_skipped_is_a_valid_human_decision():
    # ④ 推荐人审可明示跳过
    assert "skipped" in gate.HUMAN_DECISIONS
    assert {"approved", "rejected"} <= gate.HUMAN_DECISIONS


def test_brand_story_is_recommended_not_forced_block():
    # ④ company_facts/brand_softarticle 由 brand_story_requires_human_review(硬拦)改为
    # brand_story_review_recommended(overridable);human=='skipped' 时 bypass。
    src = inspect.getsource(gate.evaluate_publication_eligibility)
    assert "brand_story_review_recommended" in src
    assert '"overridable": True' in src or "'overridable': True" in src
    assert 'human != "skipped"' in src
    # 旧的强制人审死拦措辞不再是拦截返回
    assert '"reason": "brand_story_requires_human_review"' not in src


def test_skip_guard_only_for_recommended_review():
    # ④ set_human_review:skip 只放行 recommended_review,不能越 legal/platform/机器未过。
    src = inspect.getsource(gate.set_human_review)
    assert 'decision == "skipped"' in src
    assert 'brand_story_review_recommended' in src
    assert "skip_only_for_recommended_review" in src


def test_no_production_code_anywhere_still_raises_evidence_violation():
    """[返修 P1-1] 源锁由单文件字符串改为**全仓 grep**。

    上一版只扫 article_generator_service.py,漏了 tools/article_generator.py 的三处活链
    (补发/批量/替换路径),导致"全仓 raise 归零"的说法失实。现在扫遍全部生产 .py:
    只要还有任何一处 `raise EvidenceFirstViolation`,本判别就红。
    """
    from pathlib import Path
    root = Path(gate.__file__).parent.parent
    skip_dirs = {"tests", "qa", ".deploy_runtime", "node_modules", "_archived", ".git"}
    offenders = []
    for path in root.rglob("*.py"):
        if any(part in skip_dirs for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if "raise EvidenceFirstViolation" in line and not line.lstrip().startswith("#"):
                offenders.append(f"{path.relative_to(root)}:{i}")
    assert offenders == [], f"文章层仍有拒存路径(D8 零阻断未落实): {offenders}"


def test_zero_blocking_paths_land_needs_legal_fix_instead():
    """拆掉 raise 后必须**给出口**:各拒存点都要落 needs_legal_fix 草稿态,不能一删了之。"""
    from pathlib import Path
    root = Path(gate.__file__).parent.parent
    for rel, minimum in (("writing/article_generator_service.py", 2),
                         ("writing/article_writer.py", 1),
                         ("tools/article_generator.py", 3),
                         ("writing/authority_ranking_writer.py", 1)):
        src = (root / rel).read_text(encoding="utf-8")
        assert src.count("needs_legal_fix") >= minimum, f"{rel} 拆了阻断却没给 findings 出口"
