"""Shadow-only writing injection gates for R6-H.

The live article generation path may call the recorder only behind feature
flags to write admin-local shadow artifacts. It must not alter generated
content, default prompts, database writes, billing, publishing, or customer
output. Customer output remains disabled by default.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


VISIBLE_EVIDENCE_MARKER_RE = re.compile(
    r"(?:"
    r"\[\s*[Ee]\s*\d+(?:\s*[,;，；、/\s]\s*(?:[Ee]\s*)?\d+)*\s*\]"
    r"|［\s*[Ee]\s*\d+(?:\s*[,;，；、/\s]\s*(?:[Ee]\s*)?\d+)*\s*］"
    r"|【\s*[Ee]\s*\d+(?:\s*[,;，；、/\s]\s*(?:[Ee]\s*)?\d+)*\s*】"
    r")"
)
SENTENCE_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")
SENTENCE_TRAILING_PUNCTUATION = "。！？!?；;"
HIGH_RISK_UNBOUND_CLAIM_RE = re.compile(
    r"(?:\d|[％%]|元|万|千|百|倍|分|治愈|疗效|癌症|癌|保证|承诺|提升|降低|增长|排名|第一|"
    r"唯一|指定|认证|国家|卫健委|官方|权威|战略合作|合作伙伴|国际认证)"
)
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
SHADOW_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
ALLOWED_RISK_LEVELS = {"", "low", "normal", "medium", "high"}
ALLOWED_EVIDENCE_MODES = {"no_evidence", "with_evidence"}
TRUE_VALUES = {"1", "true", "yes", "y", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "off"}
UNSAFE_STORAGE_SEGMENTS = {
    "public",
    "static",
    "assets",
    "frontend",
    "customer",
    "customers",
    "token",
    "tokens",
    "share",
    "shared",
    "publish",
    "published",
    "www",
    "wwwroot",
    "inetpub",
    "htdocs",
    "html",
}
UNSAFE_STORAGE_KEYWORDS = {
    "customerfiles",
    "customer-files",
    "customer_files",
    "public-data",
    "public_data",
}
TRUSTED_EVIDENCE_PACKET_SOURCES = {
    "trusted_research_storage",
    "trusted_server_research",
    "trusted_server_store",
    "server_research_storage",
    "server_side_research_storage",
    "server_trusted_store",
    "research_storage",
}
DEFAULT_SHADOW_ARTIFACT_ROOT = "qa-artifacts/r6c_20260619/r6e_shadow_harness/admin_shadow_runs"
RECORDER_ERROR_LOG_NAME = "recorder_errors.jsonl"


@dataclass(frozen=True)
class ShadowInjectionConfig:
    enabled: bool = False
    shadow_only: bool = True
    customer_output_enabled: bool = False


def _parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    return default


def _parse_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        text = str(value).strip()
        numeric = float(text)
        if not numeric.is_integer():
            return default
        return int(numeric)
    except (TypeError, ValueError):
        return default


def _normalize_token(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = ZERO_WIDTH_RE.sub("", text)
    return re.sub(r"[\s\-]+", "_", text.strip().lower())


def _compact_token(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", _normalize_token(value))


def _sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def load_shadow_injection_config(env: Mapping[str, str] | None = None) -> ShadowInjectionConfig:
    """Load R6-H feature flags.

    Defaults are deliberately fail-closed:
    - feature disabled;
    - shadow-only enabled;
    - customer output disabled.
    """

    if env is None:
        try:
            from writing.feature_switches import is_feature_enabled

            env_enabled = _parse_bool(os.environ.get("R6H_SHADOW_INJECTION_ENABLED"), False)
            env_customer_output = _parse_bool(os.environ.get("R6H_CUSTOMER_OUTPUT_ENABLED"), False)
            return ShadowInjectionConfig(
                enabled=is_feature_enabled("r6h_shadow_injection") or env_enabled,
                shadow_only=_parse_bool(os.environ.get("R6H_SHADOW_ONLY"), True),
                customer_output_enabled=is_feature_enabled("r6h_customer_output") or env_customer_output,
            )
        except Exception:
            source = os.environ
    else:
        source = env
    return ShadowInjectionConfig(
        enabled=_parse_bool(source.get("R6H_SHADOW_INJECTION_ENABLED"), False),
        shadow_only=_parse_bool(source.get("R6H_SHADOW_ONLY"), True),
        customer_output_enabled=_parse_bool(source.get("R6H_CUSTOMER_OUTPUT_ENABLED"), False),
    )


def _risk_level(case: Mapping[str, Any], route: Mapping[str, Any] | None = None) -> str:
    route = route or {}
    if case.get("high_liability_vertical"):
        return "high"
    case_level = _normalize_token(case.get("risk_level"))
    route_level = _normalize_token(route.get("risk_level"))
    compact_levels = {_compact_token(level) for level in (case_level, route_level)}
    if (
        "high" in {case_level, route_level}
        or any(level.startswith("high_") for level in (case_level, route_level) if level)
        or any(level.startswith("highrisk") or level.startswith("highliability") for level in compact_levels if level)
    ):
        return "high"
    return case_level or route_level


def _risk_level_unrecognized(case: Mapping[str, Any], route: Mapping[str, Any] | None = None) -> bool:
    route = route or {}
    for raw_level in (case.get("risk_level"), route.get("risk_level")):
        level = _normalize_token(raw_level)
        if not level:
            continue
        if _risk_level({"risk_level": level}, None) == "high":
            continue
        if level not in ALLOWED_RISK_LEVELS:
            return True
    return False


def _evidence_mode(case: Mapping[str, Any]) -> str:
    return _normalize_token(case.get("evidence_mode") or "no_evidence")


def _evidence_mode_unrecognized(case: Mapping[str, Any]) -> bool:
    return _evidence_mode(case) not in ALLOWED_EVIDENCE_MODES


def _semantic_provider_complete(semantic: Mapping[str, Any]) -> bool:
    required = _parse_int(semantic.get("required_provider_count"), 0)
    actual = _parse_int(semantic.get("provider_count"), 0)
    return required <= 0 or actual >= required


def plan_shadow_route(
    case: Mapping[str, Any],
    guard: Mapping[str, Any],
    semantic: Mapping[str, Any],
    route: Mapping[str, Any] | None = None,
    metric: Mapping[str, Any] | None = None,
    conflict: Mapping[str, Any] | None = None,
    config: ShadowInjectionConfig | None = None,
) -> dict[str, Any]:
    """Plan internal shadow routing without enabling customer output."""

    config = config or load_shadow_injection_config()
    metric = metric or {}
    conflict = conflict or {}
    blockers: list[str] = []
    manual_reasons: list[str] = []

    if not config.enabled:
        blockers.append("feature_flag_off")
    if not config.shadow_only:
        blockers.append("shadow_only_disabled")

    guard_decision = _normalize_token(guard.get("shadow_decision") or "missing")
    if guard_decision in {"missing", "blocked", "retry_required"}:
        blockers.append(f"guard_{guard_decision}")
    elif guard_decision not in {"pass", "pass_with_warning"}:
        blockers.append("guard_unrecognized_decision")

    semantic_decision = _normalize_token(semantic.get("semantic_decision") or "missing")
    if semantic_decision in {"missing", "incomplete", "blocked"}:
        blockers.append(f"semantic_{semantic_decision}")
    elif semantic_decision not in {"pass", "pass_with_warning"}:
        blockers.append("semantic_unrecognized_decision")
    if not _semantic_provider_complete(semantic):
        blockers.append("semantic_required_provider_missing")

    if conflict.get("needs_human_review") or _parse_int(conflict.get("conflict_count"), 0) > 0:
        manual_reasons.append("source_conflict_manual_review")
    if _normalize_token(metric.get("decision")) == "manual_review":
        manual_reasons.extend(metric.get("reasons") or ["metric_retention_manual_review"])

    if _risk_level_unrecognized(case, route):
        manual_reasons.append("risk_level_unrecognized_manual_review")
    if _risk_level(case, route) in {"high", "high_liability"}:
        manual_reasons.append("high_liability_manual_review")

    if _parse_bool(semantic.get("needs_human_review"), False):
        manual_reasons.extend(semantic.get("manual_review_reasons") or ["semantic_manual_review"])

    route = "disabled"
    if config.enabled and config.shadow_only:
        route = "manual_review" if manual_reasons else "shadow_candidate"
    if blockers:
        route = "disabled"

    return {
        "case_id": case.get("case_id", ""),
        "shadow_injection_route": route,
        "manual_review_required": bool(manual_reasons),
        "manual_review_reasons": sorted(set(str(r) for r in manual_reasons if r)),
        "customer_output_allowed": False,
        "blockers": sorted(set(blockers)),
    }


def _article_has_visible_markers(article_text: str) -> bool:
    return bool(VISIBLE_EVIDENCE_MARKER_RE.search(article_text or ""))


def _validate_evidence_bindings(case: Mapping[str, Any], article_text: str) -> list[str]:
    reasons: list[str] = []
    if _article_has_visible_markers(article_text):
        reasons.append("visible_evidence_markers_in_customer_text")

    mode = _evidence_mode(case)
    if mode not in ALLOWED_EVIDENCE_MODES:
        reasons.append("evidence_mode_unrecognized")
        return reasons
    if mode != "with_evidence":
        return reasons

    if _normalize_token(case.get("customer_article_mode")) != "stripped_safe_binding":
        reasons.append("missing_stripped_safe_binding_mode")

    bindings = case.get("evidence_bindings") or []
    if not isinstance(bindings, list) or not bindings:
        reasons.append("missing_evidence_bindings")

    expected_sha = str(case.get("customer_article_sha256") or "").strip().lower()
    if not expected_sha:
        reasons.append("missing_customer_article_sha256")
    elif expected_sha != _sha256_text(article_text).lower():
        reasons.append("customer_article_sha_mismatch")

    packet_ids = _evidence_packet_ids(case)
    if not packet_ids:
        reasons.append("missing_evidence_packet")
    valid_spans: list[tuple[int, int]] = []
    for binding in bindings if isinstance(bindings, list) else []:
        if not isinstance(binding, Mapping):
            reasons.append("binding_invalid")
            break
        evidence_id = str(binding.get("evidence_id") or "").strip()
        if not evidence_id:
            reasons.append("binding_missing_evidence_id")
            break
        if evidence_id not in packet_ids:
            reasons.append("binding_evidence_id_unknown")
            break
        span = binding.get("claim_span") if isinstance(binding.get("claim_span"), Mapping) else {}
        start = _parse_int(span.get("start"), -1)
        end = _parse_int(span.get("end"), -1)
        if start < 0 or end <= start or end > len(article_text):
            reasons.append("binding_span_out_of_range")
            break
        if start == 0 and end >= len(article_text) and len(list(SENTENCE_RE.finditer(article_text))) > 1:
            reasons.append("binding_span_too_broad")
        if end - start > 500:
            reasons.append("binding_span_too_broad")
        claim_text = str(binding.get("claim_text") or "").strip()
        if not claim_text:
            reasons.append("binding_missing_claim_text")
            break
        if article_text[start:end].strip() != claim_text:
            reasons.append("binding_claim_text_mismatch")
            break
        valid_spans.append((start, end))

    for sentence in SENTENCE_RE.finditer(article_text or ""):
        sentence_text = sentence.group(0).strip()
        if not sentence_text:
            continue
        sentence_start, _sentence_end = sentence.span()
        content_end = _sentence_content_end(sentence_start, sentence.group(0))
        if not any(start <= sentence_start and end >= content_end for start, end in valid_spans):
            if HIGH_RISK_UNBOUND_CLAIM_RE.search(sentence_text):
                reasons.append("unbound_high_risk_claim")
            else:
                reasons.append("unbound_claim_sentence")
            break

    return sorted(set(reasons))


def _evidence_packet_ids(case: Mapping[str, Any]) -> set[str]:
    packet = case.get("evidence_packet") or case.get("evidence") or case.get("evidences") or []
    ids: set[str] = set()
    if not isinstance(packet, list):
        return ids
    for item in packet:
        if not isinstance(item, Mapping):
            continue
        evidence_id = item.get("evidence_id") or item.get("id")
        if evidence_id:
            ids.add(str(evidence_id).strip())
    return ids


def _sentence_content_end(sentence_start: int, sentence_text: str) -> int:
    stripped = sentence_text.rstrip(SENTENCE_TRAILING_PUNCTUATION)
    return sentence_start + len(stripped)


def evaluate_customer_output_gate(
    case: Mapping[str, Any],
    article_text: str,
    guard: Mapping[str, Any],
    semantic: Mapping[str, Any],
    route: Mapping[str, Any] | None = None,
    conflict: Mapping[str, Any] | None = None,
    metric: Mapping[str, Any] | None = None,
    config: ShadowInjectionConfig | None = None,
) -> dict[str, Any]:
    """Fail-closed customer-output gate for future live seam callers.

    Passing this function is not enough to publish unless the explicit customer
    output flag is also enabled. In the current R6-H phase that flag defaults to
    false and should remain false.
    """

    config = config or load_shadow_injection_config()
    route = route or {}
    conflict = conflict or {}
    metric = metric or {}
    blockers: list[str] = []
    warnings: list[str] = []

    if _risk_level_unrecognized(case, route):
        blockers.append("risk_level_unrecognized")
    if _risk_level(case, route) in {"high", "high_liability"}:
        blockers.append("high_liability_manual_review_required")
    manual_review_raw = route.get("manual_review_required")
    if _parse_bool(manual_review_raw, False):
        blockers.append("manual_review_required")
    elif manual_review_raw not in (None, "", False) and _normalize_token(manual_review_raw) not in FALSE_VALUES:
        blockers.append("manual_review_required_unrecognized")
    if not route:
        blockers.append("route_missing")
    elif "customer_output_allowed" not in route:
        blockers.append("route_customer_output_not_explicitly_allowed")
    elif not _parse_bool(route.get("customer_output_allowed"), False):
        blockers.append("route_customer_output_not_allowed")

    guard_decision = _normalize_token(guard.get("shadow_decision") or "missing")
    if guard_decision in {"missing", "blocked", "retry_required"}:
        blockers.append(f"guard_{guard_decision}")
    elif guard_decision == "pass_with_warning":
        warnings.append("guard_pass_with_warning")
    elif guard_decision != "pass":
        blockers.append("guard_unrecognized_decision")

    semantic_decision = _normalize_token(semantic.get("semantic_decision") or "missing")
    if semantic_decision in {"missing", "incomplete", "blocked"}:
        blockers.append(f"semantic_{semantic_decision}")
    elif semantic_decision not in {"pass", "pass_with_warning"}:
        blockers.append("semantic_unrecognized_decision")
    if _parse_bool(semantic.get("needs_human_review"), False):
        blockers.append("semantic_manual_review_required")
    if not _semantic_provider_complete(semantic):
        blockers.append("semantic_required_provider_missing")

    if conflict.get("needs_human_review") or _parse_int(conflict.get("conflict_count"), 0) > 0:
        blockers.append("source_conflict_unresolved")
    if _normalize_token(metric.get("decision")) == "manual_review":
        blockers.append("metric_retention_manual_review")

    blockers.extend(_validate_evidence_bindings(case, article_text))
    blockers.extend(_evidence_packet_source_blockers(case))

    shadow_gate_allowed = not blockers
    if not config.customer_output_enabled:
        blockers.append("customer_output_feature_flag_off")

    customer_output_allowed = not blockers
    return {
        "case_id": case.get("case_id", ""),
        "shadow_gate_allowed": shadow_gate_allowed,
        "customer_output_allowed": customer_output_allowed,
        "gate_decision": "allow_customer_output" if customer_output_allowed else "blocked",
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
    }


def _safety_flags(external_llm_called: bool = False) -> dict[str, bool]:
    return {
        "production_touched": False,
        "database_touched": False,
        "deployment_touched": False,
        "published": False,
        "billing_touched": False,
        "auth_touched": False,
        "cache_touched": False,
        "flag_touched": False,
        "external_llm_called": external_llm_called,
    }


def _assert_admin_only_storage_path(output_root: Path) -> Path:
    resolved = Path(output_root).resolve()
    unsafe_parts = [
        part
        for part in (path_part.lower() for path_part in resolved.parts)
        if part in UNSAFE_STORAGE_SEGMENTS or part in UNSAFE_STORAGE_KEYWORDS
    ]
    if unsafe_parts:
        raise ValueError("shadow artifacts must be stored in an admin-only local path")
    return resolved


def _json_safe_paths(data: Mapping[str, Path]) -> dict[str, str]:
    return {key: str(value) for key, value in data.items()}


def _safe_text(value: Any, max_length: int = 160) -> str:
    return str(value or "")[:max_length]


def _safe_string_list(values: Any, limit: int = 40, max_length: int = 160) -> list[str]:
    if not isinstance(values, list):
        return []
    return [_safe_text(item, max_length) for item in values[:limit] if str(item or "").strip()]


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _manual_review_projection(
    route: Mapping[str, Any],
    gate_result: Mapping[str, Any],
    semantic: Mapping[str, Any],
) -> dict[str, Any]:
    manual_reasons = set(_safe_string_list(route.get("manual_review_reasons")))
    manual_reasons.update(_safe_string_list(semantic.get("manual_review_reasons")))
    blockers = _safe_string_list(gate_result.get("blockers"))
    warnings = _safe_string_list(gate_result.get("warnings"))
    manual_required = (
        _parse_bool(route.get("manual_review_required"), False)
        or _parse_bool(semantic.get("needs_human_review"), False)
        or bool(blockers)
    )

    return {
        "route": _safe_text(route.get("shadow_injection_route") or "unknown", 80),
        "manual_review_required": manual_required,
        "manual_review_reasons": sorted(manual_reasons),
        "blockers": blockers,
        "warnings": warnings,
        "customer_output_allowed": False,
    }


def _quality_summary_projection(
    guard: Mapping[str, Any],
    semantic: Mapping[str, Any],
    conflict: Mapping[str, Any] | None,
    metric: Mapping[str, Any] | None,
) -> dict[str, Any]:
    conflict = conflict or {}
    metric = metric or {}
    return {
        "guard_decision": _normalize_token(guard.get("shadow_decision") or "missing"),
        "semantic_decision": _normalize_token(semantic.get("semantic_decision") or "missing"),
        "provider_count": _parse_int(semantic.get("provider_count"), 0),
        "required_provider_count": _parse_int(semantic.get("required_provider_count"), 0),
        "warn_count": _parse_int(semantic.get("warn_count"), 0),
        "fail_count": _parse_int(semantic.get("fail_count"), 0),
        "issue_count": _parse_int(semantic.get("issue_count"), 0),
        "semantic_needs_human_review": _parse_bool(semantic.get("needs_human_review"), False),
        "metric_decision": _normalize_token(metric.get("decision") or ""),
        "metric_retention_rate": _safe_float(metric.get("metric_retention_rate") or metric.get("retention_rate")),
        "source_conflict_count": _parse_int(conflict.get("conflict_count"), 0),
        "source_conflict_needs_review": _parse_bool(conflict.get("needs_human_review"), False),
    }


def _source_summary_projection(case: Mapping[str, Any]) -> dict[str, Any]:
    source = _normalize_token(case.get("evidence_packet_source") or case.get("evidence_source"))
    bindings = case.get("evidence_bindings") or []
    return {
        "evidence_mode": _evidence_mode(case),
        "evidence_packet_source": source,
        "trusted_source": bool(source and source in TRUSTED_EVIDENCE_PACKET_SOURCES),
        "evidence_count": len(_evidence_packet_ids(case)),
        "binding_count": len(bindings) if isinstance(bindings, list) else 0,
    }


def _evidence_packet_source_blockers(case: Mapping[str, Any]) -> list[str]:
    if _evidence_mode(case) != "with_evidence":
        return []
    source = _normalize_token(case.get("evidence_packet_source") or case.get("evidence_source"))
    if not source:
        return ["missing_evidence_packet_source"]
    if source not in TRUSTED_EVIDENCE_PACKET_SOURCES:
        return ["untrusted_evidence_packet_source"]
    return []


def _validate_shadow_run_id(run_id: str) -> str:
    value = str(run_id or "").strip()
    if not value or ".." in value or not SHADOW_RUN_ID_RE.match(value):
        raise ValueError("invalid shadow run id")
    return value


def _shadow_seam_preflight_blockers(case: Mapping[str, Any], article_text: str) -> list[str]:
    blockers: list[str] = []
    if not str(article_text or "").strip():
        blockers.append("missing_customer_article_text")
    blockers.extend(_evidence_packet_source_blockers(case))
    return blockers


def create_shadow_run_artifacts(
    case: Mapping[str, Any],
    article_text: str,
    guard: Mapping[str, Any],
    semantic: Mapping[str, Any],
    route: Mapping[str, Any],
    output_root: str | Path,
    config: ShadowInjectionConfig | None = None,
    conflict: Mapping[str, Any] | None = None,
    metric: Mapping[str, Any] | None = None,
    run_id: str | None = None,
    preflight_blockers: list[str] | None = None,
) -> dict[str, Any]:
    """Record a local/admin-only shadow run artifact bundle.

    The writer is fail-closed:
    - feature flag off -> no files are written;
    - shadow-only false -> no files are written;
    - public/customer-like paths are rejected.
    """

    config = config or load_shadow_injection_config()
    if not config.enabled or not config.shadow_only:
        return {
            "status": "disabled",
            "artifact_written": False,
            "customer_output_allowed": False,
            "blockers": ["feature_flag_off" if not config.enabled else "shadow_only_disabled"],
            "safety": _safety_flags(),
        }

    output_root = _assert_admin_only_storage_path(Path(output_root))
    case_id = str(case.get("case_id") or "unknown_case")
    safe_case_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", case_id).strip("_") or "unknown_case"
    if run_id is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{safe_case_id}_{timestamp}"
    else:
        run_id = _validate_shadow_run_id(run_id)

    run_dir = output_root / run_id
    preview_dir = run_dir / "preview"
    internal_dir = run_dir / "_internal"
    preview_dir.mkdir(parents=True, exist_ok=False)
    internal_dir.mkdir(parents=True, exist_ok=False)

    gate_result = evaluate_customer_output_gate(
        case=case,
        article_text=article_text,
        guard=guard,
        semantic=semantic,
        route=route,
        conflict=conflict,
        metric=metric,
        config=config,
    )
    if preflight_blockers:
        gate_result = {
            **gate_result,
            "shadow_gate_allowed": False,
            "customer_output_allowed": False,
            "gate_decision": "blocked",
            "blockers": sorted(set((gate_result.get("blockers") or []) + preflight_blockers)),
        }

    article_path = preview_dir / "article.md"
    sidecar_path = internal_dir / "evidence_bindings.json"
    gate_path = internal_dir / "customer_output_gate_result.json"
    manifest_path = run_dir / "shadow_run_manifest.json"
    report_path = run_dir / "SHADOW_RUN_REPORT.md"

    article_path.write_text(article_text, encoding="utf-8")
    sidecar = {
        "case_id": case_id,
        "visibility": "admin_only_internal",
        "customer_article_sha256": _sha256_text(article_text),
        "evidence_bindings": case.get("evidence_bindings") or [],
        "marker_derived": True,
        "guard_pass_is_not_evidence_truth": True,
    }
    sidecar_path.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
    gate_path.write_text(json.dumps(gate_result, ensure_ascii=False, indent=2), encoding="utf-8")

    artifact_paths = {
        "article": article_path,
        "sidecar": sidecar_path,
        "gate_result": gate_path,
        "manifest": manifest_path,
        "report": report_path,
    }
    manifest = {
        "status": "shadow_only_run_recorded",
        "run_id": run_id,
        "case_id": case_id,
        "shadow_only": True,
        "customer_output_allowed": False,
        "shadow_gate_allowed": bool(gate_result.get("shadow_gate_allowed")),
        "sidecar_visibility": "admin_only_internal",
        "customer_article_sha256": _sha256_text(article_text),
        "sidecar_sha256": _sha256_text(sidecar_path.read_text(encoding="utf-8")),
        "blockers": gate_result.get("blockers") or [],
        "manual_review": _manual_review_projection(route, gate_result, semantic),
        "quality_summary": _quality_summary_projection(guard, semantic, conflict, metric),
        "source_summary": _source_summary_projection(case),
        "artifact_paths": _json_safe_paths(artifact_paths),
        "safety": _safety_flags(),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    report = "\n".join(
        [
            f"# R6-H Shadow Run {run_id}",
            "",
            f"- case_id: `{case_id}`",
            "- status: `shadow_only_run_recorded`",
            f"- customer_output_allowed: `{gate_result.get('customer_output_allowed')}`",
            f"- blockers: `{', '.join(gate_result.get('blockers') or []) or 'none'}`",
            "- sidecar_visibility: `admin_only_internal`",
            "- production/live/customer output: `blocked unless future hard gate explicitly allows`",
            "",
        ]
    )
    report_path.write_text(report, encoding="utf-8")

    return {
        "status": "recorded",
        "artifact_written": True,
        "run_id": run_id,
        "case_id": case_id,
        "customer_output_allowed": False,
        "shadow_gate_allowed": bool(gate_result.get("shadow_gate_allowed")),
        "blockers": gate_result.get("blockers") or [],
        "artifact_paths": artifact_paths,
        "safety": _safety_flags(),
    }


def _bump_counter(counter: dict[str, int], key: Any) -> None:
    label = str(key or "unknown").strip() or "unknown"
    counter[label] = counter.get(label, 0) + 1


def _top_counter(counter: Mapping[str, int], limit: int = 8) -> list[dict[str, Any]]:
    return [
        {"label": label, "count": count}
        for label, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def _shadow_observability_summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    blocker_counts: dict[str, int] = {}
    guard_counts: dict[str, int] = {}
    semantic_counts: dict[str, int] = {}
    evidence_mode_counts: dict[str, int] = {}
    source_trust_counts = {"trusted": 0, "untrusted_or_missing": 0}

    live_run_count = 0
    manual_review_required_count = 0
    sidecar_present_count = 0
    customer_output_open_count = 0
    customer_output_closed_count = 0
    unreadable_manifest_count = 0

    for run in runs:
        run_id = str(run.get("run_id") or "")
        case_id = str(run.get("case_id") or "")
        if run_id.startswith("live_") or case_id.startswith("LIVE-"):
            live_run_count += 1
        if run.get("sidecar_present"):
            sidecar_present_count += 1
        if run.get("customer_output_allowed"):
            customer_output_open_count += 1
        else:
            customer_output_closed_count += 1
        if run.get("manual_review_required"):
            manual_review_required_count += 1
        if run.get("status") == "manifest_unreadable":
            unreadable_manifest_count += 1

        _bump_counter(status_counts, run.get("status") or "listed")
        quality = run.get("quality_summary") if isinstance(run.get("quality_summary"), Mapping) else {}
        source = run.get("source_summary") if isinstance(run.get("source_summary"), Mapping) else {}
        _bump_counter(guard_counts, quality.get("guard_decision") or "missing")
        _bump_counter(semantic_counts, quality.get("semantic_decision") or "missing")
        _bump_counter(evidence_mode_counts, source.get("evidence_mode") or "unknown")
        if source.get("trusted_source"):
            source_trust_counts["trusted"] += 1
        else:
            source_trust_counts["untrusted_or_missing"] += 1
        for blocker in run.get("blockers") or []:
            _bump_counter(blocker_counts, blocker)

    total_runs = len(runs)
    return {
        "total_runs": total_runs,
        "live_run_count": live_run_count,
        "manual_review_required_count": manual_review_required_count,
        "sidecar_present_count": sidecar_present_count,
        "customer_output_open_count": customer_output_open_count,
        "customer_output_closed_count": customer_output_closed_count,
        "unreadable_manifest_count": unreadable_manifest_count,
        "shadow_gate_allowed_count": sum(1 for run in runs if run.get("shadow_gate_allowed")),
        "shadow_gate_blocked_count": sum(1 for run in runs if not run.get("shadow_gate_allowed")),
        "by_status": status_counts,
        "by_guard_decision": guard_counts,
        "by_semantic_decision": semantic_counts,
        "by_evidence_mode": evidence_mode_counts,
        "by_source_trust": source_trust_counts,
        "top_blockers": _top_counter(blocker_counts),
    }


def _recorder_error_summary(output_root: Path) -> dict[str, Any]:
    error_path = output_root / RECORDER_ERROR_LOG_NAME
    error_type_counts: dict[str, int] = {}
    count = 0
    unreadable = False
    if error_path.exists():
        try:
            for line in error_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                count += 1
                try:
                    event = json.loads(line)
                    _bump_counter(error_type_counts, event.get("error_type") or "unknown")
                except json.JSONDecodeError:
                    _bump_counter(error_type_counts, "unreadable_line")
        except OSError:
            unreadable = True
    return {
        "recorder_error_count": count,
        "recorder_error_log_unreadable": unreadable,
        "top_recorder_error_types": _top_counter(error_type_counts),
    }


def list_shadow_run_artifacts(output_root: str | Path) -> dict[str, Any]:
    """List local/admin-only shadow runs without exposing sidecar content."""

    output_root = _assert_admin_only_storage_path(Path(output_root))
    if not output_root.exists():
        return {
            "status": "listed",
            "artifact_count": 0,
            "runs": [],
            "observability": {
                **_shadow_observability_summary([]),
                **_recorder_error_summary(output_root),
            },
            "safety": _safety_flags(),
        }

    runs: list[dict[str, Any]] = []
    for manifest_path in sorted(output_root.glob("*/shadow_run_manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            runs.append(
                {
                    "run_id": manifest_path.parent.name,
                    "status": "manifest_unreadable",
                    "case_id": "",
                    "customer_output_allowed": False,
                    "shadow_gate_allowed": False,
                    "sidecar_present": False,
                    "blockers": ["manifest_unreadable"],
                    "blocker_count": 1,
                    "manual_review_required": True,
                    "quality_summary": {
                        "guard_decision": "missing",
                        "semantic_decision": "missing",
                    },
                    "source_summary": {
                        "evidence_mode": "unknown",
                        "trusted_source": False,
                    },
                }
            )
            continue

        internal_dir = manifest_path.parent / "_internal"
        sidecar_present = (internal_dir / "evidence_bindings.json").exists()
        manual_review = manifest.get("manual_review") if isinstance(manifest.get("manual_review"), Mapping) else {}
        quality_summary = manifest.get("quality_summary") if isinstance(manifest.get("quality_summary"), Mapping) else {}
        source_summary = manifest.get("source_summary") if isinstance(manifest.get("source_summary"), Mapping) else {}
        blockers = [str(item) for item in (manifest.get("blockers") or [])]
        runs.append(
            {
                "run_id": str(manifest.get("run_id") or manifest_path.parent.name),
                "status": str(manifest.get("status") or ""),
                "case_id": str(manifest.get("case_id") or ""),
                "shadow_only": bool(manifest.get("shadow_only")),
                "shadow_gate_allowed": bool(manifest.get("shadow_gate_allowed")),
                "customer_output_allowed": bool(manifest.get("customer_output_allowed")),
                "sidecar_visibility": str(manifest.get("sidecar_visibility") or ""),
                "sidecar_present": sidecar_present,
                "customer_article_sha256": str(manifest.get("customer_article_sha256") or ""),
                "blocker_count": len(blockers),
                "blockers": blockers,
                "manual_review_required": bool(manual_review.get("manual_review_required")),
                "quality_summary": {
                    "guard_decision": _safe_text(quality_summary.get("guard_decision"), 80),
                    "semantic_decision": _safe_text(quality_summary.get("semantic_decision"), 80),
                },
                "source_summary": {
                    "evidence_mode": _safe_text(source_summary.get("evidence_mode"), 80),
                    "trusted_source": bool(source_summary.get("trusted_source")),
                },
            }
        )

    return {
        "status": "listed",
        "artifact_count": len(runs),
        "runs": runs,
        "observability": {
            **_shadow_observability_summary(runs),
            **_recorder_error_summary(output_root),
        },
        "safety": _safety_flags(),
    }


def get_shadow_run_artifact_detail(output_root: str | Path, run_id: str) -> dict[str, Any]:
    """Read a single admin-only shadow run as a safe review projection."""

    output_root = _assert_admin_only_storage_path(Path(output_root))
    safe_run_id = _validate_shadow_run_id(run_id)
    run_dir = (output_root / safe_run_id).resolve()
    if output_root not in run_dir.parents:
        raise ValueError("invalid shadow run path")

    manifest_path = run_dir / "shadow_run_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(safe_run_id)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    gate_path = run_dir / "_internal" / "customer_output_gate_result.json"
    sidecar_path = run_dir / "_internal" / "evidence_bindings.json"
    article_path = run_dir / "preview" / "article.md"

    gate = {}
    if gate_path.exists():
        try:
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            gate = {"blockers": ["gate_result_unreadable"]}

    sidecar = {}
    if sidecar_path.exists():
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            sidecar = {}

    article_text = article_path.read_text(encoding="utf-8") if article_path.exists() else ""
    manual_review = manifest.get("manual_review")
    if not isinstance(manual_review, Mapping):
        manual_review = _manual_review_projection({}, gate, {})
    quality_summary = manifest.get("quality_summary")
    if not isinstance(quality_summary, Mapping):
        quality_summary = _quality_summary_projection(gate, {}, None, None)
    source_summary = manifest.get("source_summary")
    if not isinstance(source_summary, Mapping):
        source_summary = {}
    evidence_mode = _normalize_token(source_summary.get("evidence_mode"))
    body_preview_redacted = evidence_mode != "with_evidence"
    body_preview_policy = (
        "with_evidence_review_excerpt"
        if not body_preview_redacted
        else "no_evidence_body_redacted"
        if evidence_mode == "no_evidence"
        else "body_redacted_without_evidence_mode"
    )

    raw_bindings = sidecar.get("evidence_bindings") if isinstance(sidecar, Mapping) else []
    binding_summaries: list[dict[str, Any]] = []
    if isinstance(raw_bindings, list):
        for index, binding in enumerate(raw_bindings[:80], start=1):
            if not isinstance(binding, Mapping):
                continue
            span = binding.get("claim_span") if isinstance(binding.get("claim_span"), Mapping) else {}
            binding_summaries.append(
                {
                    "index": index,
                    "evidence_id": str(binding.get("evidence_id") or ""),
                    "claim_excerpt": str(binding.get("claim_text") or "")[:240],
                    "start": _parse_int(span.get("start"), 0),
                    "end": _parse_int(span.get("end"), 0),
                }
            )

    sentence_summaries: list[dict[str, Any]] = []
    if not body_preview_redacted:
        for index, sentence in enumerate(SENTENCE_RE.finditer(article_text or ""), start=1):
            if index > 40:
                break
            sentence_start, sentence_end = sentence.span()
            linked_ids = []
            for binding in binding_summaries:
                if binding["start"] <= sentence_start and binding["end"] >= _sentence_content_end(sentence_start, sentence.group(0)):
                    linked_ids.append(binding["evidence_id"])
            sentence_summaries.append(
                {
                    "index": index,
                    "text_excerpt": sentence.group(0).strip()[:280],
                    "start": sentence_start,
                    "end": sentence_end,
                    "linked_evidence_ids": sorted(set(linked_ids)),
                }
            )

    blockers = gate.get("blockers") or manifest.get("blockers") or []
    warnings = gate.get("warnings") or []

    return {
        "status": "detail",
        "run_id": str(manifest.get("run_id") or safe_run_id),
        "case_id": str(manifest.get("case_id") or ""),
        "shadow_only": bool(manifest.get("shadow_only")),
        "shadow_gate_allowed": bool(manifest.get("shadow_gate_allowed")),
        "customer_output_allowed": False,
        "blockers": [str(item) for item in blockers],
        "warnings": [str(item) for item in warnings],
        "sidecar": {
            "present": sidecar_path.exists(),
            "visibility": str(manifest.get("sidecar_visibility") or sidecar.get("visibility") or ""),
        },
        "article": {
            "sha256": _sha256_text(article_text),
            "char_count": len(article_text),
            "body_preview_redacted": body_preview_redacted,
            "body_preview_policy": body_preview_policy,
            "sentence_summaries": sentence_summaries,
        },
        "binding_summaries": binding_summaries,
        "manual_review": {
            "route": _safe_text(manual_review.get("route"), 80),
            "manual_review_required": bool(manual_review.get("manual_review_required")),
            "manual_review_reasons": _safe_string_list(manual_review.get("manual_review_reasons")),
            "blockers": _safe_string_list(manual_review.get("blockers")),
            "warnings": _safe_string_list(manual_review.get("warnings")),
            "customer_output_allowed": False,
        },
        "quality_summary": {
            "guard_decision": _safe_text(quality_summary.get("guard_decision"), 80),
            "semantic_decision": _safe_text(quality_summary.get("semantic_decision"), 80),
            "provider_count": _parse_int(quality_summary.get("provider_count"), 0),
            "required_provider_count": _parse_int(quality_summary.get("required_provider_count"), 0),
            "warn_count": _parse_int(quality_summary.get("warn_count"), 0),
            "fail_count": _parse_int(quality_summary.get("fail_count"), 0),
            "issue_count": _parse_int(quality_summary.get("issue_count"), 0),
            "semantic_needs_human_review": bool(quality_summary.get("semantic_needs_human_review")),
            "metric_decision": _safe_text(quality_summary.get("metric_decision"), 80),
            "metric_retention_rate": _safe_float(quality_summary.get("metric_retention_rate")),
            "source_conflict_count": _parse_int(quality_summary.get("source_conflict_count"), 0),
            "source_conflict_needs_review": bool(quality_summary.get("source_conflict_needs_review")),
        },
        "source_summary": {
            "evidence_mode": _safe_text(source_summary.get("evidence_mode"), 80),
            "evidence_packet_source": _safe_text(source_summary.get("evidence_packet_source"), 120),
            "trusted_source": bool(source_summary.get("trusted_source")),
            "evidence_count": _parse_int(source_summary.get("evidence_count"), 0),
            "binding_count": _parse_int(source_summary.get("binding_count"), 0),
        },
        "safety": _safety_flags(),
    }


def run_shadow_only_seam_adapter(
    case: Mapping[str, Any],
    article_text: str,
    guard: Mapping[str, Any],
    semantic: Mapping[str, Any],
    output_root: str | Path,
    config: ShadowInjectionConfig | None = None,
    conflict: Mapping[str, Any] | None = None,
    metric: Mapping[str, Any] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Unwired adapter for future shadow seam development.

    It plans a shadow route and records local artifacts only when explicitly
    enabled. It never grants customer output in this phase.
    """

    config = config or load_shadow_injection_config()
    if not config.enabled or not config.shadow_only:
        return {
            "status": "disabled",
            "artifact_written": False,
            "route": {
                "shadow_injection_route": "disabled",
                "customer_output_allowed": False,
            },
            "customer_output_allowed": False,
            "blockers": ["feature_flag_off" if not config.enabled else "shadow_only_disabled"],
            "safety": _safety_flags(),
        }

    route = plan_shadow_route(
        case=case,
        guard=guard,
        semantic=semantic,
        metric=metric,
        conflict=conflict,
        config=config,
    )
    preflight_blockers = _shadow_seam_preflight_blockers(case, article_text)
    artifact = create_shadow_run_artifacts(
        case=case,
        article_text=article_text,
        guard=guard,
        semantic=semantic,
        route=route,
        conflict=conflict,
        metric=metric,
        output_root=output_root,
        config=config,
        run_id=run_id,
        preflight_blockers=preflight_blockers,
    )
    return {
        **artifact,
        "route": route,
        "customer_output_allowed": False,
        "safety": _safety_flags(),
    }


def _safe_identifier_part(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_")
    return safe or fallback


def _utc_run_suffix() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}_{secrets.token_hex(3)}"


def record_live_generation_shadow_artifact(
    *,
    quote_id: Any,
    brand_name: str,
    industry: str,
    topic: Mapping[str, Any],
    article: Mapping[str, Any],
    output_root: str | Path | None = None,
    config: ShadowInjectionConfig | None = None,
) -> dict[str, Any]:
    """Record a live-generation candidate into the admin-only shadow harness.

    This helper is deliberately conservative: live article generation currently
    does not produce a verified stripped-safe evidence sidecar, so the shadow
    case is recorded as no_evidence with missing guard/semantic decisions. That
    gives admins an auditable artifact without implying customer readiness.
    """

    article_text = str(article.get("content") or "")
    topic_id = topic.get("id")
    article_id = article.get("id")
    case = {
        "case_id": (
            f"LIVE-Q{_safe_identifier_part(quote_id, 'unknown')}"
            f"-T{_safe_identifier_part(topic_id, 'unknown')}"
            f"-A{_safe_identifier_part(article_id, 'unknown')}"
        ),
        "evidence_mode": "no_evidence",
        "risk_level": "normal",
        "brand_name": str(brand_name or ""),
        "industry": str(industry or ""),
        "topic_id": topic_id,
        "quote_id": quote_id,
        "article_id": article_id,
        "style": article.get("style") or topic.get("style_code") or topic.get("style") or "",
    }
    run_id = (
        f"live_quote{_safe_identifier_part(quote_id, 'unknown')}"
        f"_topic{_safe_identifier_part(topic_id, 'unknown')}"
        f"_article{_safe_identifier_part(article_id, 'unknown')}"
        f"_{_utc_run_suffix()}"
    )
    root = output_root or os.getenv("R6H_SHADOW_ARTIFACT_ROOT") or DEFAULT_SHADOW_ARTIFACT_ROOT
    return run_shadow_only_seam_adapter(
        case=case,
        article_text=article_text,
        guard={"shadow_decision": "missing"},
        semantic={
            "semantic_decision": "missing",
            "needs_human_review": True,
            "provider_count": 0,
            "required_provider_count": 3,
            "manual_review_reasons": ["semantic_not_run"],
        },
        output_root=root,
        config=config,
        run_id=run_id,
    )


def record_shadow_recorder_error(
    *,
    quote_id: Any,
    topic: Mapping[str, Any],
    article: Mapping[str, Any],
    error: BaseException,
    output_root: str | Path | None = None,
    config: ShadowInjectionConfig | None = None,
) -> dict[str, Any]:
    """Write a structured admin-only recorder error event.

    This is best-effort observability. It never proves content quality and must
    never block live article generation.
    """

    config = config or load_shadow_injection_config()
    if not config.enabled or not config.shadow_only:
        return {
            "status": "disabled",
            "artifact_written": False,
            "customer_output_allowed": False,
            "safety": _safety_flags(),
        }

    root = _assert_admin_only_storage_path(
        Path(output_root or os.getenv("R6H_SHADOW_ARTIFACT_ROOT") or DEFAULT_SHADOW_ARTIFACT_ROOT)
    )
    root.mkdir(parents=True, exist_ok=True)
    event = {
        "event": "r6h_shadow_recorder_error",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "quote_id": str(quote_id or ""),
        "topic_id": str(topic.get("id") or ""),
        "article_id": str(article.get("id") or ""),
        "error_type": type(error).__name__,
        "error_message_sha256": _sha256_text(str(error)),
        "error_message_length": len(str(error)),
        "customer_output_allowed": False,
        "safety": _safety_flags(),
    }
    error_path = root / RECORDER_ERROR_LOG_NAME
    with error_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return {
        "status": "recorded",
        "artifact_written": True,
        "customer_output_allowed": False,
        "safety": _safety_flags(),
    }
