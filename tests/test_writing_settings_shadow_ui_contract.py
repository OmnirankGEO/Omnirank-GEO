"""
R6-H writing settings UI contract.

This lightweight test guards the admin-only Shadow tab contract without
requiring a browser test runner.
"""
from __future__ import annotations

from pathlib import Path


COMPONENT = Path("frontend/src/components/WritingSettingsDialog.tsx")


def test_writing_settings_exposes_shadow_artifact_listing_tab():
    source = COMPONENT.read_text(encoding="utf-8")

    assert 'value="shadow"' in source
    assert '"/api/writing/shadow-runs"' in source
    assert '`/api/writing/shadow-runs/${encodeURIComponent(runId)}`' in source
    assert "内部观测" in source
    assert "binding_summaries" in source
    assert "manual_review" in source
    assert "quality_summary" in source
    assert "source_summary" in source
    assert "人工复核" in source
    assert "语义评审" in source
    assert "来源" in source
    assert "观测" in source
    assert "阻断原因" in source
    assert "live_run_count" in source
    assert "recorder_error_count" in source
    assert "记录错误" in source
    assert "top_blockers" in source
    assert "body_preview_redacted" in source
    assert "正文预览已收起" in source


def test_writing_settings_shadow_tab_does_not_render_sidecar_internals():
    source = COMPONENT.read_text(encoding="utf-8")

    assert "evidence_bindings" not in source
    assert "sidecar_path" not in source
    assert "artifact_paths" not in source


def test_writing_settings_shadow_tab_has_no_mutating_shadow_actions():
    source = COMPONENT.read_text(encoding="utf-8")

    assert "/api/writing/shadow-runs/${encodeURIComponent(runId)}/approve" not in source
    assert "/api/writing/shadow-runs/${encodeURIComponent(runId)}/publish" not in source
    assert "/api/writing/shadow-runs/${encodeURIComponent(runId)}/customer-output" not in source
    assert "批准上线" not in source
    assert "发布到客户" not in source
