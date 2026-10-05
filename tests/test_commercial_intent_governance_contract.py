from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SSOT = (
    ROOT
    / "docs"
    / "DECISION"
    / "GEO_COMMERCIAL_INTENT_GOVERNANCE_SSOT_2026-07-23.md"
)
ARCHIVE = (
    ROOT
    / "docs"
    / "AI-CONTEXT"
    / "ARCHIVE"
    / "COMMERCIAL_INTENT_POLICY_SUPERSEDED_INDEX_2026-07-23.md"
)


def _read(path: Path) -> str:
    assert path.is_file(), f"missing governance file: {path}"
    return path.read_text(encoding="utf-8")


def test_commercial_intent_ssot_is_declared_and_machine_referenceable() -> None:
    manual = _read(ROOT / "CLAUDE.md")
    ssot = _read(SSOT)

    assert SSOT.relative_to(ROOT).as_posix() in manual
    for required in (
        "CommercialQueryPolicy",
        "法无禁止皆可为",
        "commercial_delivery_eligible=false",
        "不得进入付费交付",
        "AI 仅修正未通过部分",
        "人工确认继续",
        "技术层不可人工绕过",
    ):
        assert required in ssot


def test_superseded_manual_reentry_rule_is_not_active() -> None:
    manual = _read(ROOT / "CLAUDE.md")
    assert "但必须可见、可人工改选" not in manual
    assert "不得被选择、计价、确认、监测、生成标题或进入文章交付血缘" in manual


def test_archive_names_known_runtime_drift_without_claiming_it_is_fixed() -> None:
    archive = _read(ARCHIVE)
    for path in (
        "tools/keyword_expander.py",
        "api/brand_api.py",
        "tools/monitoring/batch_monitor.py",
        "writing/keyword_topic_generator.py",
        "writing/article_generation_failure.py",
    ):
        assert path in archive
    assert "implementation drift" in archive
    assert "不能仅凭本归档宣称线上行为已经改变" in archive


def test_known_conflicting_strategy_is_explicitly_archived() -> None:
    legacy = _read(
        ROOT
        / "docs"
        / "AI-CONTEXT"
        / "NEXT_STAGE_TRUSTED_RECOMMENDATION_OS_IMPLEMENTATION_SPEC_2026-07-17.md"
    )
    assert "ARCHIVED AS COMMERCIAL-CONTENT AUTHORITY" in legacy
    assert SSOT.relative_to(ROOT).as_posix() in legacy


def test_hard_block_boundary_preserves_system_safety() -> None:
    ssot = _read(SSOT)
    for boundary in (
        "跨租户访问",
        "重复扣费",
        "账本不守恒",
        "密钥",
        "不可逆数据破坏",
        "空正文",
    ):
        assert boundary in ssot
