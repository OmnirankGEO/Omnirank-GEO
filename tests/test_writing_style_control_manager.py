from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _temp_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_path = tmp_path / "writing_style_versions.json"
    audit_path = tmp_path / "writing_style_audit_log.jsonl"
    writing_config = tmp_path / "writing_config.json"
    monkeypatch.setenv("WRITING_STYLE_CONTROL_FILE", str(state_path))
    monkeypatch.setenv("WRITING_STYLE_AUDIT_LOG_FILE", str(audit_path))
    monkeypatch.setenv("WRITING_STYLE_FLYWHEEL_SOURCE_ROOT", str(tmp_path / "flywheel"))

    import writing.style_registry as style_registry

    monkeypatch.setattr(style_registry, "WRITING_CONFIG_FILE", str(writing_config))


def _allow_strict_experiment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Activation unit tests must pass the same gates as production."""
    import services.article_data_health as data_health
    import services.article_experiment_registry as experiments

    monkeypatch.setattr(
        data_health,
        "get_article_data_health",
        lambda: {"version": "test-health-v1", "state": "ready"},
    )
    monkeypatch.setattr(
        experiments,
        "can_activate_candidate",
        lambda **_kwargs: {
            "allowed": True,
            "reason": "passed",
            "experiment": {"experiment_id": 9001, "decision": "PASS"},
        },
    )


def test_control_state_bootstraps_stable_baselines(monkeypatch, tmp_path):
    _temp_paths(monkeypatch, tmp_path)

    from writing.style_control import load_control_state

    state = load_control_state()

    assert state["schema_version"] == 1
    assert state["config_version"] >= 1
    assert state["active_by_style"]["buying_guide"].startswith("baseline_buying_guide")
    assert state["active_by_style"]["ranking_v2"].startswith("baseline_ranking_v2")
    assert state["active_by_style"] == state["stable_by_style"]
    assert state["stable_by_style"]["buying_guide"].startswith("baseline_buying_guide")
    baseline = next(v for v in state["versions"] if v["style_code"] == "buying_guide")
    assert baseline["status"] == "stable"
    assert baseline["stable_baseline"] is True
    assert baseline["prompt_text"]
    assert "# GEO Article Contract implementation_guide" in baseline["prompt_text"]
    assert "证据优先总契约" in baseline["prompt_text"]
    assert baseline["source"] == "code_default"
    assert baseline["rollout_recommendation"]["eligibility"] == "production_default"


def test_existing_stable_baseline_refreshes_to_r6_v09(monkeypatch, tmp_path):
    _temp_paths(monkeypatch, tmp_path)

    state_path = tmp_path / "writing_style_versions.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "config_version": 3,
                "updated_at": "2026-06-21T00:00:00Z",
                "active_by_style": {},
                "stable_by_style": {"buying_guide": "baseline_buying_guide_code_default"},
                "versions": [
                    {
                        "version_id": "baseline_buying_guide_code_default",
                        "style_code": "buying_guide",
                        "style_name": "选购指南",
                        "source": "code_default",
                        "status": "stable",
                        "prompt_text": "旧版默认 prompt",
                        "prompt_sha256": "legacy",
                        "stable_baseline": True,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    from writing.style_control import load_control_state

    state = load_control_state()
    baseline = next(v for v in state["versions"] if v["version_id"] == "baseline_buying_guide_code_default")

    assert state["config_version"] == 4
    assert baseline["prompt_text"] != "旧版默认 prompt"
    assert "# GEO Article Contract implementation_guide" in baseline["prompt_text"]
    assert baseline["strategy_summary"].startswith("GEO v1.4")
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    persisted_baseline = next(v for v in persisted["versions"] if v["version_id"] == "baseline_buying_guide_code_default")
    assert "# GEO Article Contract implementation_guide" in persisted_baseline["prompt_text"]


def test_default_prompt_uses_v14_contract_and_versioned_override_is_guarded(monkeypatch, tmp_path):
    _temp_paths(monkeypatch, tmp_path)

    from writing.style_registry import WRITING_CONFIG_FILE, get_prompt_for_style

    prompt = get_prompt_for_style("buying_guide")
    assert "# GEO Article Contract implementation_guide" in prompt
    assert "证据优先总契约" in prompt
    assert "文体：方法与实施指南" in prompt

    from writing.article_style_contract import STYLE_CONTRACT_VERSION

    Path(WRITING_CONFIG_FILE).write_text(
        json.dumps(
            {
                "llm_config": {"provider": "dashscope", "model": "qwen3.7-max"},
                "prompt_overrides": {"buying_guide": "管理员显式覆盖模板"},
                "prompt_override_contract_versions": {"buying_guide": STYLE_CONTRACT_VERSION},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    override_prompt = get_prompt_for_style("buying_guide")
    assert override_prompt.startswith("[GEO_EVIDENCE_FIRST_V1]")
    assert "管理员显式覆盖模板" in override_prompt
    assert "经审核的候选差异指令" in override_prompt


def test_align_draft_creates_candidate_without_activation(monkeypatch, tmp_path):
    _temp_paths(monkeypatch, tmp_path)

    from writing.style_control import align_draft_from_flywheel, load_control_state

    before = load_control_state()
    result = align_draft_from_flywheel(
        style_code="buying_guide",
        industry_key="education",
        evidence_mode="with_evidence",
        actor_id=7,
        expected_config_version=before["config_version"],
        source_summary={
            "sample_count": 128,
            "control_sample_count": 44,
            "shadow_pass_rate": 0.97,
            "semantic_pass_rate": 0.92,
            "latest_candidate": "r6d_new_style_candidate_v0_9",
        },
    )

    draft = result["version"]
    after = result["state"]
    assert draft["status"] == "draft"
    assert draft["style_code"] == "buying_guide"
    assert draft["source"] == "flywheel_align"
    assert draft["sample_count"] == 128
    assert draft["control_sample_count"] == 44
    assert draft["rollout_recommendation"]["eligibility"] == "eligible"
    assert "active" not in draft["status"]
    assert after["active_by_style"]["buying_guide"].startswith("baseline_buying_guide")
    assert draft["version_id"] not in after["active_by_style"].values()
    assert "一键对齐生成候选" in draft["evidence_chain"]["summary"]


def test_retired_ranking_code_routes_to_unordered_evidence_comparison(monkeypatch, tmp_path):
    _temp_paths(monkeypatch, tmp_path)

    from writing.style_registry import get_prompt_for_style

    prompt = get_prompt_for_style("ranking_v2")

    assert "独立选型研究员" not in prompt
    assert "文体：选购与多品牌比较" in prompt
    assert "无序的场景矩阵" in prompt
    assert "不得自称权威研究院" in prompt
    for old_marker in [
        "XX.X/100",
        "score_top1",
        "95-100分",
        "综合评分**：XX.X/100",
        "评分区间（客户必须高于竞品）",
    ]:
        assert old_marker not in prompt


def test_guard_blocks_internal_fields_and_no_evidence_numbers(monkeypatch, tmp_path):
    _temp_paths(monkeypatch, tmp_path)

    from writing.style_control import evaluate_style_guard

    guard = evaluate_style_guard(
        "这里写 cost margin ratio。无证据也写 3000元、8亿元 和 45%。",
        evidence_mode="no_evidence",
        risk_level="normal",
    )

    labels = {finding["label"] for finding in guard["findings"]}
    assert guard["decision"] == "blocked"
    assert guard["worst_action"] == "BLOCK"
    assert "internal_field_leak" in labels
    assert "no_evidence_price" in labels
    assert "no_evidence_percent" in labels


def test_flywheel_summary_uses_named_fields_not_arbitrary_numbers(monkeypatch, tmp_path):
    _temp_paths(monkeypatch, tmp_path)
    summary_path = tmp_path / "flywheel" / "analysis" / "r6c_structure_summary.json"
    summary_path.parent.mkdir(parents=True)
    summary_path.write_text(
        json.dumps(
            {
                "report_id": "artifact-1234",
                "notes": "control appears here but no named sample fields",
                "nested": {"random_score": 9876},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    from writing.style_control import build_flywheel_summary

    summary = build_flywheel_summary("education", "buying_guide")

    assert summary["sample_count"] == 0
    assert summary["control_sample_count"] == 0
    assert summary["sample_count_status"] == "unknown"
    assert summary["control_sample_count_status"] == "unknown"
    assert summary["eligibility"] == "unknown"


def test_flywheel_summary_reads_explicit_named_fields(monkeypatch, tmp_path):
    _temp_paths(monkeypatch, tmp_path)
    summary_path = tmp_path / "flywheel" / "analysis" / "r6c_structure_summary.json"
    summary_path.parent.mkdir(parents=True)
    summary_path.write_text(
        json.dumps(
            {
                "sample_count": 128,
                "control_sample_count": 44,
                "shadow_pass_rate": 0.91,
                "semantic_pass_rate": 87,
                "latest_candidate": "r6d_new_style_candidate_v0_9",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    from writing.style_control import build_flywheel_summary

    summary = build_flywheel_summary("education", "buying_guide")

    assert summary["sample_count"] == 128
    assert summary["control_sample_count"] == 44
    assert summary["sample_count_status"] == "known"
    assert summary["control_sample_count_status"] == "known"
    assert summary["shadow_pass_rate"] == 0.91
    assert summary["semantic_pass_rate"] == 0.87
    assert summary["latest_candidate"] == "r6d_new_style_candidate_v0_9"
    assert summary["eligibility"] == "eligible"


def test_flywheel_summary_falls_back_to_live_article_structure(monkeypatch, tmp_path):
    _temp_paths(monkeypatch, tmp_path)

    def fake_analyze_article_structure_patterns(industry, limit=300, min_chars=500):
        assert industry == "general"
        return {
            "status": "success",
            "loaded": 256,
            "sample_status": "ready",
            "groups": {
                "adopted_group": {"count": 88},
                "cited_group": {"count": 31},
                "search_only_control_group": {"count": 76},
                "reference_group": {"count": 61},
            },
            "feature_lift": [
                {
                    "label": "开头先回答问题",
                    "adopted_share": 0.72,
                    "control_share": 0.35,
                    "lift": 2.057,
                    "recommended": True,
                }
            ],
            "recommended_structure_rules": ["开头 200 字内先直接回答问题，再解释选择标准。"],
            "source": "geo_research_articles + geo_research_source_signals",
        }

    import services.article_structure_analysis as article_structure_analysis

    monkeypatch.setattr(
        article_structure_analysis,
        "analyze_article_structure_patterns",
        fake_analyze_article_structure_patterns,
    )

    from writing.style_control import build_flywheel_summary

    summary = build_flywheel_summary("general", "buying_guide")

    assert summary["sample_count"] == 119
    assert summary["control_sample_count"] == 137
    assert summary["sample_count_status"] == "known"
    assert summary["control_sample_count_status"] == "known"
    assert summary["latest_candidate"] == "article_structure_service"
    assert summary["source_files"] == ["live:geo_research_articles", "live:articles"]
    assert summary["article_structure"]["loaded"] == 256
    assert summary["eligibility"] == "eligible"


def test_activate_syncs_prompt_override_and_rollback_restores_baseline(monkeypatch, tmp_path):
    _temp_paths(monkeypatch, tmp_path)
    _allow_strict_experiment(monkeypatch)

    from writing.style_control import activate_style_version, align_draft_from_flywheel, load_control_state, rollback_style
    from writing.style_registry import _get_writing_config, get_prompt_for_style

    state = load_control_state()
    draft = align_draft_from_flywheel(
        style_code="buying_guide",
        industry_key="manufacturing",
        evidence_mode="with_evidence",
        actor_id=7,
        expected_config_version=state["config_version"],
        source_summary={"sample_count": 140, "control_sample_count": 35},
    )
    version = draft["version"]

    activated = activate_style_version(
        version_id=version["version_id"],
        actor_id=7,
        expected_config_version=draft["state"]["config_version"],
        note="启用经过验证的新文体",
    )

    assert activated["version"]["status"] == "active"
    config = _get_writing_config()
    assert config["prompt_overrides"]["buying_guide"] == version["prompt_text"]
    active_prompt = get_prompt_for_style("buying_guide")
    assert version["prompt_text"] in active_prompt
    assert "经审核的候选差异指令" in active_prompt

    rolled = rollback_style(
        style_code="buying_guide",
        actor_id=7,
        expected_config_version=activated["state"]["config_version"],
        note="回退 stable baseline",
    )

    config_after = _get_writing_config()
    stable_version_id = rolled["state"]["stable_by_style"]["buying_guide"]
    stable = next(v for v in rolled["state"]["versions"] if v["version_id"] == stable_version_id)
    assert rolled["active_version"]["version_id"] == stable_version_id
    assert config_after["prompt_overrides"]["buying_guide"] == stable["prompt_text"]
    assert stable["prompt_text"] in get_prompt_for_style("buying_guide")


def test_expected_config_version_conflict_is_rejected(monkeypatch, tmp_path):
    _temp_paths(monkeypatch, tmp_path)

    from writing.style_control import StyleControlConflict, align_draft_from_flywheel

    with pytest.raises(StyleControlConflict):
        align_draft_from_flywheel(
            style_code="buying_guide",
            industry_key="education",
            evidence_mode="with_evidence",
            actor_id=7,
            expected_config_version=999,
            source_summary={"sample_count": 100, "control_sample_count": 20},
        )

    with pytest.raises(StyleControlConflict, match="config_version_required"):
        align_draft_from_flywheel(
            style_code="buying_guide",
            industry_key="education",
            evidence_mode="with_evidence",
            actor_id=7,
            expected_config_version=None,
            source_summary={"sample_count": 100, "control_sample_count": 20},
        )


def test_audit_log_records_align_activate_and_rollback(monkeypatch, tmp_path):
    _temp_paths(monkeypatch, tmp_path)
    _allow_strict_experiment(monkeypatch)

    from writing.style_control import activate_style_version, align_draft_from_flywheel, load_control_state, rollback_style

    state = load_control_state()
    draft = align_draft_from_flywheel(
        style_code="risk_compliance",
        industry_key="legal",
        evidence_mode="with_evidence",
        actor_id=3,
        expected_config_version=state["config_version"],
        source_summary={"sample_count": 101, "control_sample_count": 30},
    )
    activated = activate_style_version(
        version_id=draft["version"]["version_id"],
        actor_id=3,
        expected_config_version=draft["state"]["config_version"],
        note="activate",
    )
    rollback_style(
        style_code="risk_compliance",
        actor_id=3,
        expected_config_version=activated["state"]["config_version"],
        note="rollback",
    )

    events = [
        json.loads(line)
        for line in (tmp_path / "writing_style_audit_log.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    actions = [event["action"] for event in events]
    assert actions == ["align_draft", "activate", "rollback"]
    assert all(event["actor_id"] == 3 for event in events)
