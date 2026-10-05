from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _switch_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "writing_feature_switches.json"
    monkeypatch.setenv("WRITING_FEATURE_SWITCH_FILE", str(path))
    return path


def test_feature_switches_bootstrap_runtime_defaults(monkeypatch, tmp_path):
    _switch_file(monkeypatch, tmp_path)

    from writing.feature_switches import get_feature_switch_state

    state = get_feature_switch_state()
    switches = {item["key"]: item for item in state["switches"]}

    assert state["config_version"] >= 1
    assert switches["writing_style_overrides"]["enabled"] is True
    assert switches["r6h_shadow_injection"]["enabled"] is False
    assert switches["r6h_customer_output"]["enabled"] is False
    assert switches["r6h_customer_output"]["locked"] is True
    assert switches["structure_guidance"]["enabled"] is True
    assert switches["media_takeover"]["enabled"] is False


def test_feature_switch_updates_persist_with_optimistic_lock(monkeypatch, tmp_path):
    _switch_file(monkeypatch, tmp_path)

    from writing.feature_switches import get_feature_switch_state, is_feature_enabled, update_feature_switch

    before = get_feature_switch_state()
    result = update_feature_switch(
        "r6h_shadow_injection",
        True,
        actor_id=9,
        expected_config_version=before["config_version"],
        note="local smoke",
    )

    assert result["switch"]["enabled"] is True
    assert result["config_version"] == before["config_version"] + 1
    assert is_feature_enabled("r6h_shadow_injection") is True

    with pytest.raises(Exception) as exc:
        update_feature_switch(
            "media_takeover",
            True,
            actor_id=9,
            expected_config_version=before["config_version"],
        )
    assert "config_version_conflict" in str(exc.value)


def test_customer_output_switch_is_locked_even_with_confirmation(monkeypatch, tmp_path):
    _switch_file(monkeypatch, tmp_path)

    from writing.feature_switches import FeatureSwitchLocked, get_feature_switch_state, update_feature_switch

    state = get_feature_switch_state()
    with pytest.raises(FeatureSwitchLocked):
        update_feature_switch(
            "r6h_customer_output",
            True,
            actor_id=1,
            expected_config_version=state["config_version"],
            confirm="ENABLE_R6H_CUSTOMER_OUTPUT",
        )


def test_shadow_config_reads_feature_switch_file_but_explicit_env_still_overrides(monkeypatch, tmp_path):
    _switch_file(monkeypatch, tmp_path)

    from writing.feature_switches import get_feature_switch_state, update_feature_switch
    from writing.shadow_only_injection import load_shadow_injection_config

    state = get_feature_switch_state()
    update_feature_switch(
        "r6h_shadow_injection",
        True,
        actor_id=1,
        expected_config_version=state["config_version"],
    )

    config = load_shadow_injection_config()
    assert config.enabled is True
    assert config.customer_output_enabled is False

    explicit = load_shadow_injection_config(env={"R6H_SHADOW_INJECTION_ENABLED": "false", "R6H_CUSTOMER_OUTPUT_ENABLED": "true"})
    assert explicit.enabled is False
    assert explicit.customer_output_enabled is True


def test_style_override_switch_controls_prompt_overrides(monkeypatch, tmp_path):
    _switch_file(monkeypatch, tmp_path)
    writing_config = tmp_path / "writing_config.json"

    import writing.style_registry as style_registry
    from writing.article_style_contract import STYLE_CONTRACT_VERSION
    from writing.feature_switches import get_feature_switch_state, update_feature_switch

    monkeypatch.setattr(style_registry, "WRITING_CONFIG_FILE", str(writing_config))
    style_registry.save_writing_config(
        prompt_overrides={"buying_guide": "OVERRIDE_PROMPT"},
        prompt_override_contract_versions={"buying_guide": STYLE_CONTRACT_VERSION},
    )

    guarded = style_registry.get_prompt_for_style("buying_guide")
    assert guarded.startswith("[GEO_EVIDENCE_FIRST_V1]")
    assert "OVERRIDE_PROMPT" in guarded

    state = get_feature_switch_state()
    update_feature_switch(
        "writing_style_overrides",
        False,
        actor_id=2,
        expected_config_version=state["config_version"],
        confirm="DISABLE_WRITING_STYLE_OVERRIDES",
    )

    assert "OVERRIDE_PROMPT" not in style_registry.get_prompt_for_style("buying_guide")


def test_media_takeover_switch_is_off_by_default_and_can_enable_ready_gate(monkeypatch, tmp_path):
    _switch_file(monkeypatch, tmp_path)

    from services.media_takeover_gate import evaluate_takeover_gate
    from writing.feature_switches import get_feature_switch_state, update_feature_switch

    kwargs = {
        "industry_key": "real-estate",
        "health": {"level": "green", "can_activate": True},
        "approved_bindings": [{"id": 1}],
        "adoption_summary": {"answer_adopted_rows": 50, "explicit_cited_rows": 50},
        "whitelist": {"industry_keys": ["real-estate"]},
    }

    assert evaluate_takeover_gate(**kwargs)["production_takeover"] is False

    state = get_feature_switch_state()
    update_feature_switch(
        "media_takeover",
        True,
        actor_id=3,
        expected_config_version=state["config_version"],
        confirm="ENABLE_MEDIA_TAKEOVER",
    )

    result = evaluate_takeover_gate(**kwargs)
    assert result["ready"] is True
    assert result["production_takeover"] is True
