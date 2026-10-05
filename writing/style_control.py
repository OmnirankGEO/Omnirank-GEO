"""Admin-only writing style flywheel control plane.

This module stores prompt/style versions in a JSON file, keeps the code-default
prompt as a stable rollback root, and activates versions by syncing the selected
prompt into the existing ``prompt_overrides`` path used by ``style_registry``.

It intentionally does not publish customer output, does not call an LLM, and
does not migrate the database.  Flywheel alignment creates draft candidates
only; an explicit admin activation is required before the prompt override is
changed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CONTROL_FILE_ENV = "WRITING_STYLE_CONTROL_FILE"
AUDIT_LOG_ENV = "WRITING_STYLE_AUDIT_LOG_FILE"
FLYWHEEL_SOURCE_ROOT_ENV = "WRITING_STYLE_FLYWHEEL_SOURCE_ROOT"

DEFAULT_CONTROL_FILE = "data/writing_style_versions.json"
DEFAULT_AUDIT_LOG_FILE = "data/writing_style_audit_log.jsonl"
VALID_STATUSES = {"draft", "shadow_ready", "admin_review", "active", "stable", "retired", "blocked"}

BLOCK = "BLOCK"
RETRY = "RETRY"
WARN = "WARN"

INTERNAL_FIELD_RE = re.compile(
    r"\b(cost|margin|ratio|markup|factory|guard|owner_user_id)\b|毛利|进货价|经营空间",
    re.I,
)
PRICE_RE = re.compile(r"(?:¥|￥)?\s*\d+(?:\.\d+)?\s*(?:元|块|千|万|万元|亿元|亿)")
PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s*%|百分之[一二三四五六七八九十百\d]+")
CUSTOMER_CASE_RE = re.compile(r"客户案例|真实案例|成功案例|案例显示")
SOURCE_RE = re.compile(r"据统计|数据显示|白皮书|报告显示|研究表明")
HIGH_RISK_PROMISE_RE = re.compile(r"治愈|疗效|收益保证|保证上榜|保证排名|100%|百分百")


class StyleControlError(Exception):
    """Base class for writing style control errors."""


class StyleControlConflict(StyleControlError):
    """Raised when an optimistic-lock config version does not match."""


class StyleControlNotFound(StyleControlError):
    """Raised when a requested version is missing."""


class StyleControlBlocked(StyleControlError):
    """Raised when an operation is blocked by guard or status rules."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _control_path() -> Path:
    return Path(os.getenv(CONTROL_FILE_ENV) or DEFAULT_CONTROL_FILE)


def _audit_path() -> Path:
    return Path(os.getenv(AUDIT_LOG_ENV) or DEFAULT_AUDIT_LOG_FILE)


def _source_root() -> Path:
    return Path(os.getenv(FLYWHEEL_SOURCE_ROOT_ENV) or "qa-artifacts/r6c_20260619")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _append_audit(event: dict[str, Any]) -> None:
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def _style_name(style_code: str) -> str:
    from writing.style_registry import WRITING_STYLES

    return str((WRITING_STYLES.get(style_code) or {}).get("name") or style_code)


def _default_prompt(style_code: str) -> str:
    from writing.style_registry import _get_default_prompt

    return _get_default_prompt(style_code)


def _known_style_codes() -> list[str]:
    from writing.style_registry import WRITING_STYLES

    return list(WRITING_STYLES.keys())


def _baseline_version(style_code: str) -> dict[str, Any]:
    prompt = _default_prompt(style_code)
    created_at = "2026-06-21T00:00:00Z"
    return {
        "version_id": f"baseline_{style_code}_code_default",
        "style_code": style_code,
        "style_name": _style_name(style_code),
        "source": "code_default",
        "status": "stable",
        "prompt_text": prompt,
        "prompt_sha256": _sha256(prompt),
        "strategy_summary": "GEO v1.4 六类文体：证据、E-E-A-T、事实边界与可复核结构是稳定基线。",
        "evidence_chain": {
            "summary": "GEO v1.4 已完成六类合同、Evidence Pack、发布快照、严格结果实验与人工启用闸门的本地验证；生产仍需独立签发。",
            "items": [
                "三类历史高风险模板停止新生成",
                "所有文体共享证据优先与 E-E-A-T 硬门",
                "只有预注册 GEO 实验 PASS 才可人工启用候选",
            ],
        },
        "guard_summary": {"decision": "pass", "worst_action": "PASS", "findings": []},
        "judge_summary": {"semantic_decision": "pass_with_warning", "provider_count": 3},
        "created_at": created_at,
        "created_by": 0,
        "activated_at": None,
        "stable_baseline": True,
        "rollback_parent": None,
        "sample_count": 0,
        "control_sample_count": 0,
        "risk_level": "normal",
        "allowed_industries": [],
        "blocked_industries": [],
        "rollout_recommendation": {
            "eligibility": "production_default",
            "reason": "当前安全基线；候选必须通过预注册 GEO 实验和人工审核，历史高风险模板不得恢复新生成。",
        },
    }


def _refresh_code_default_baseline(version: dict[str, Any], style_code: str) -> bool:
    """Keep persisted stable baseline aligned with the current code default."""
    if not isinstance(version, dict):
        return False
    if not version.get("stable_baseline") or version.get("source") != "code_default":
        return False
    fresh = _baseline_version(style_code)
    changed = False
    for key in (
        "prompt_text",
        "prompt_sha256",
        "strategy_summary",
        "evidence_chain",
        "guard_summary",
        "judge_summary",
        "sample_count",
        "control_sample_count",
        "rollout_recommendation",
    ):
        if version.get(key) != fresh.get(key):
            version[key] = fresh.get(key)
            changed = True
    return changed


def _bootstrap_state() -> dict[str, Any]:
    versions = [_baseline_version(code) for code in _known_style_codes()]
    stable_by_style = {v["style_code"]: v["version_id"] for v in versions}
    return {
        "schema_version": 1,
        "config_version": 1,
        "updated_at": _now_iso(),
        "active_by_style": dict(stable_by_style),
        "stable_by_style": stable_by_style,
        "versions": versions,
    }


def _normalize_state(raw: dict[str, Any] | None) -> dict[str, Any]:
    state = raw if isinstance(raw, dict) else _bootstrap_state()
    state.setdefault("schema_version", 1)
    state.setdefault("config_version", 1)
    state.setdefault("active_by_style", {})
    state.setdefault("stable_by_style", {})
    state.setdefault("versions", [])
    existing_ids = {v.get("version_id") for v in state["versions"] if isinstance(v, dict)}
    changed = False
    for style_code in _known_style_codes():
        baseline_id = f"baseline_{style_code}_code_default"
        if baseline_id not in existing_ids:
            state["versions"].append(_baseline_version(style_code))
            changed = True
        else:
            for version in state["versions"]:
                if version.get("version_id") == baseline_id:
                    if _refresh_code_default_baseline(version, style_code):
                        changed = True
                    break
        state["stable_by_style"].setdefault(style_code, baseline_id)
        state["active_by_style"].setdefault(style_code, state["stable_by_style"][style_code])
    if changed:
        state["config_version"] = int(state.get("config_version") or 1) + 1
        state["updated_at"] = _now_iso()
    return state


def load_control_state() -> dict[str, Any]:
    raw = _read_json(_control_path())
    before = json.dumps(raw, ensure_ascii=False, sort_keys=True) if isinstance(raw, dict) else None
    state = _normalize_state(raw)
    after = json.dumps(state, ensure_ascii=False, sort_keys=True)
    if not _control_path().exists() or before != after:
        _write_json(_control_path(), state)
    return state


def _save_state(state: dict[str, Any]) -> dict[str, Any]:
    state["config_version"] = int(state.get("config_version") or 0) + 1
    state["updated_at"] = _now_iso()
    _write_json(_control_path(), state)
    # [V7] 单一写入 choke → 覆盖全部 6 个 style-control 写动作(采纳/驳回/回滚/启用/退役/蒸馏 draft/对齐/评审同步):
    # 失效看板/全景/outcome/总汇总 的进程缓存,否则运营点完[采纳]后看板 60s 不刷新以为没生效。
    # flywheel_cache 是纯 stdlib 叶子(不 import 本模块),lazy import + fail-soft,绝不破坏写路径。
    try:
        from writing.flywheel_cache import invalidate_control_derived

        invalidate_control_derived()
    except Exception:
        pass
    return state


def _assert_expected_version(state: dict[str, Any], expected_config_version: int | None) -> None:
    if expected_config_version is None:
        raise StyleControlConflict("config_version_required")
    if int(expected_config_version) != int(state.get("config_version") or 0):
        raise StyleControlConflict(
            f"config_version_conflict: expected {expected_config_version}, got {state.get('config_version')}"
        )


def _find_version(state: dict[str, Any], version_id: str) -> dict[str, Any]:
    for version in state.get("versions") or []:
        if version.get("version_id") == version_id:
            return version
    raise StyleControlNotFound(f"version_not_found: {version_id}")


def _new_version_id(style_code: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{style_code}_draft_{stamp}_{secrets.token_hex(3)}"


def _eligibility(sample_count: int, control_sample_count: int) -> tuple[str, str]:
    if sample_count < 20:
        return "observation", "样本少于 20，只能观察，不能生成上线候选。"
    if sample_count < 100:
        return "shadow", "样本 20-99，只能 shadow 验证，不能直接默认启用。"
    if control_sample_count <= 0:
        return "eligible_without_control", "采纳样本达标，但缺少对照组，不能声称因果提升。"
    return "eligible", "样本和对照组均达标，可生成候选 draft，仍需管理员审核。"


def evaluate_style_guard(prompt_text: str, *, evidence_mode: str = "with_evidence", risk_level: str = "normal") -> dict[str, Any]:
    text = str(prompt_text or "")
    mode = str(evidence_mode or "with_evidence").strip().lower()
    risk = str(risk_level or "normal").strip().lower()
    findings: list[dict[str, str]] = []

    def add(label: str, action: str, reason: str) -> None:
        findings.append({"label": label, "action": action, "reason": reason})

    if INTERNAL_FIELD_RE.search(text):
        add("internal_field_leak", BLOCK, "prompt 含成本/毛利/ratio/markup 等内部字段")
    if mode == "no_evidence" and PRICE_RE.search(text):
        add("no_evidence_price", BLOCK, "no_evidence 模式禁止价格/金额")
    if mode == "no_evidence" and PERCENT_RE.search(text):
        add("no_evidence_percent", BLOCK, "no_evidence 模式禁止百分比")
    if mode == "no_evidence" and CUSTOMER_CASE_RE.search(text):
        add("no_evidence_customer_case", BLOCK, "no_evidence 模式禁止客户案例断言")
    if mode == "no_evidence" and SOURCE_RE.search(text):
        add("no_evidence_source_claim", BLOCK, "no_evidence 模式禁止伪来源")
    if risk in {"high", "high_liability"} and HIGH_RISK_PROMISE_RE.search(text):
        add("high_liability_promise", BLOCK, "高责任行业禁止疗效/收益/排名承诺")
    if not findings and len(text) < 200:
        add("prompt_too_short", RETRY, "候选 prompt 过短，需补足结构约束")

    if any(f["action"] == BLOCK for f in findings):
        decision, worst = "blocked", BLOCK
    elif any(f["action"] == RETRY for f in findings):
        decision, worst = "retry_required", RETRY
    elif findings:
        decision, worst = "pass_with_warning", WARN
    else:
        decision, worst = "pass", "PASS"
    return {"decision": decision, "worst_action": worst, "findings": findings}


def _normalize_summary_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")


def _iter_named_values(payload: Any, parent_key: str = ""):
    if isinstance(payload, dict):
        for raw_key, value in payload.items():
            key = _normalize_summary_key(raw_key)
            path_key = f"{parent_key}.{key}" if parent_key else key
            yield path_key, key, value
            yield from _iter_named_values(value, path_key)
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            yield from _iter_named_values(value, f"{parent_key}[{index}]")


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip().rstrip("%")
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def _pick_named_number(payload: Any, keys: set[str]) -> tuple[float | None, str]:
    matches: list[tuple[float, str]] = []
    for path_key, key, value in _iter_named_values(payload):
        if key not in keys:
            continue
        number = _coerce_number(value)
        if number is not None:
            matches.append((number, path_key))
    if not matches:
        return None, ""
    number, source_key = max(matches, key=lambda item: item[0])
    return number, source_key


def _normalize_rate(value: float | None) -> float:
    if value is None:
        return 0.0
    if value > 1:
        value = value / 100.0
    return max(0.0, min(1.0, float(value)))


SAMPLE_COUNT_KEYS = {
    "sample_count",
    "total_sample_count",
    "article_sample_count",
    "case_sample_count",
    "adopted_sample_count",
    "reviewed_sample_count",
    "accepted_sample_count",
}
CONTROL_COUNT_KEYS = {
    "control_sample_count",
    "control_count",
    "total_control_count",
    "control_article_count",
    "baseline_sample_count",
    "comparison_sample_count",
}
SHADOW_RATE_KEYS = {"shadow_pass_rate", "guard_pass_rate", "pass_rate", "strict_pass_rate"}
SEMANTIC_RATE_KEYS = {"semantic_pass_rate", "judge_pass_rate", "semantic_judge_pass_rate"}
LATEST_CANDIDATE_KEYS = {"latest_candidate", "candidate_version", "prompt_version", "style_version"}


def _pick_named_text(payload: Any, keys: set[str]) -> str:
    for _path_key, key, value in _iter_named_values(payload):
        if key in keys and isinstance(value, str) and value.strip():
            return value.strip()[:160]
    return ""


def _flywheel_summary_from_files(industry_key: str, style_code: str) -> dict[str, Any]:
    root = _source_root()
    candidates = [
        root / "analysis" / "r6c_structure_summary.json",
        root / "r6h_shadow_injection_plan" / "r6h_shadow_injection_summary.json",
        root / "r6e_shadow_harness" / "r6e_shadow_guard_summary.json",
    ]
    sample_count: int | None = None
    control_sample_count: int | None = None
    shadow_pass_rate = 0.0
    semantic_pass_rate = 0.0
    latest_candidate = ""
    source_files: list[str] = []
    evidence_notes: list[str] = []
    for path in candidates:
        payload = _read_json(path)
        if not payload:
            continue
        source_files.append(str(path))
        named_sample, sample_source = _pick_named_number(payload, SAMPLE_COUNT_KEYS)
        if named_sample is not None:
            sample_count = max(sample_count or 0, int(named_sample))
            evidence_notes.append(f"sample_count:{sample_source}")
        named_control, control_source = _pick_named_number(payload, CONTROL_COUNT_KEYS)
        if named_control is not None:
            control_sample_count = max(control_sample_count or 0, int(named_control))
            evidence_notes.append(f"control_sample_count:{control_source}")
        named_shadow_rate, shadow_source = _pick_named_number(payload, SHADOW_RATE_KEYS)
        if named_shadow_rate is not None:
            shadow_pass_rate = max(shadow_pass_rate, _normalize_rate(named_shadow_rate))
            evidence_notes.append(f"shadow_pass_rate:{shadow_source}")
        named_semantic_rate, semantic_source = _pick_named_number(payload, SEMANTIC_RATE_KEYS)
        if named_semantic_rate is not None:
            semantic_pass_rate = max(semantic_pass_rate, _normalize_rate(named_semantic_rate))
            evidence_notes.append(f"semantic_pass_rate:{semantic_source}")
        named_candidate = _pick_named_text(payload, LATEST_CANDIDATE_KEYS)
        if named_candidate:
            latest_candidate = named_candidate
            evidence_notes.append("latest_candidate:named_field")
    sample_known = sample_count is not None
    control_known = control_sample_count is not None
    return {
        "industry_key": industry_key,
        "style_code": style_code,
        "sample_count": int(sample_count or 0),
        "control_sample_count": int(control_sample_count or 0),
        "sample_count_status": "known" if sample_known else "unknown",
        "control_sample_count_status": "known" if control_known else "unknown",
        "shadow_pass_rate": shadow_pass_rate,
        "semantic_pass_rate": semantic_pass_rate,
        "latest_candidate": latest_candidate or "local_flywheel_summary",
        "source_files": source_files,
        "evidence_notes": evidence_notes,
    }


def _flywheel_summary_from_article_structure(industry_key: str, style_code: str) -> dict[str, Any]:
    try:
        from services.article_structure_analysis import analyze_article_structure_patterns

        payload = analyze_article_structure_patterns(industry_key or "general", limit=500, min_chars=500)
    except Exception:
        return {}
    if not payload or payload.get("status") != "success":
        return {}

    groups = payload.get("groups") or {}
    adopted_count = int((groups.get("adopted_group") or {}).get("count") or 0)
    cited_count = int((groups.get("cited_group") or {}).get("count") or 0)
    search_control_count = int((groups.get("search_only_control_group") or {}).get("count") or 0)
    reference_count = int((groups.get("reference_group") or {}).get("count") or 0)
    sample_count = adopted_count + cited_count
    control_sample_count = search_control_count + reference_count
    if sample_count <= 0 and control_sample_count <= 0 and int(payload.get("loaded") or 0) <= 0:
        return {}

    feature_lift = payload.get("feature_lift") or []
    recommended_rules = payload.get("recommended_structure_rules") or []
    evidence_notes = [
        f"source:{payload.get('source') or 'geo_research_articles + articles'}",
        f"adopted_group:{adopted_count}",
        f"cited_group:{cited_count}",
        f"search_control_group:{search_control_count}",
        f"reference_group:{reference_count}",
    ]
    return {
        "industry_key": industry_key or "general",
        "style_code": style_code,
        "sample_count": sample_count,
        "control_sample_count": control_sample_count,
        "sample_count_status": "known",
        "control_sample_count_status": "known",
        "shadow_pass_rate": 0.0,
        "semantic_pass_rate": 0.0,
        "latest_candidate": "article_structure_service",
        "source_files": ["live:geo_research_articles", "live:articles"],
        "evidence_notes": evidence_notes,
        "article_structure": {
            "loaded": int(payload.get("loaded") or 0),
            "research_article_count": int(payload.get("research_article_count") or 0),
            "generated_article_count": int(payload.get("generated_article_count") or 0),
            "sample_status": payload.get("sample_status") or "",
            "source": payload.get("source") or "",
            "recommended_rules": recommended_rules[:6],
            "feature_lift": feature_lift[:12],
        },
    }


def build_flywheel_summary(industry_key: str = "general", style_code: str = "buying_guide") -> dict[str, Any]:
    summary = _flywheel_summary_from_files(industry_key, style_code)
    if summary.get("sample_count_status") == "unknown":
        live_summary = _flywheel_summary_from_article_structure(industry_key, style_code)
        if live_summary:
            summary = live_summary
    if summary.get("sample_count_status") == "unknown":
        eligibility = "unknown"
        reason = "未读到具名 sample_count 字段；证据链数字未知，不能据此判断上线资格。"
    else:
        eligibility, reason = _eligibility(
            int(summary.get("sample_count") or 0),
            int(summary.get("control_sample_count") or 0),
        )
    summary["eligibility"] = eligibility
    summary["eligibility_reason"] = reason
    return summary


def _build_prompt_draft(style_code: str, industry_key: str, evidence_mode: str, summary: dict[str, Any]) -> str:
    base = _default_prompt(style_code)
    sample_label = (
        str(int(summary.get("sample_count") or 0))
        if summary.get("sample_count_status") != "unknown"
        else "unknown"
    )
    control_label = (
        str(int(summary.get("control_sample_count") or 0))
        if summary.get("control_sample_count_status") != "unknown"
        else "unknown"
    )
    addendum = f"""

# R6/R7 文体飞轮结构指导（管理员 draft, 未自动上线）
- 来源行业: {industry_key}
- 目标文体: {_style_name(style_code)}({style_code})
- 证据模式: {evidence_mode}
- 样本数: {sample_label}
- 对照组样本: {control_label}
- 写法要求: 先回答用户真实问题, 再给选择标准、证据边界、风险提示和行动清单。
- 证据要求: 有证据时必须绑定到客户资料或服务端研究资料; no_evidence 时不得写价格、百分比、评分、客户案例或外部机构背书。
- 安全要求: 禁止输出任何内部经营核算、内部校验或账号归属字段。
- 高责任行业: 医疗、法律、金融、合规、安全相关内容必须转为保守建议, 不得作效果承诺。
"""
    return base.rstrip() + "\n" + addendum.strip() + "\n"


def _evidence_chain(style_code: str, industry_key: str, evidence_mode: str, summary: dict[str, Any]) -> dict[str, Any]:
    sample_count = int(summary.get("sample_count") or 0)
    control_count = int(summary.get("control_sample_count") or 0)
    eligibility, reason = _eligibility(sample_count, control_count)
    return {
        "summary": f"一键对齐生成候选: {industry_key}/{style_code}，样本 {sample_count}，对照 {control_count}。",
        "why_new_is_better": [
            "使用 R6/R7 飞轮结构信号作为写作顺序约束。",
            "保留旧 prompt 作为 stable baseline，可随时回退。",
            "候选先进入 draft，不直接替换线上默认。",
            "guard 会阻断内部字段、no_evidence 数字和高责任承诺。",
        ],
        "sample_threshold": {
            "sample_count": sample_count,
            "control_sample_count": control_count,
            "eligibility": eligibility,
            "reason": reason,
        },
        "evidence_mode": evidence_mode,
        "source_files": summary.get("source_files") or [],
    }


def align_draft_from_flywheel(
    *,
    style_code: str,
    industry_key: str,
    evidence_mode: str,
    actor_id: int,
    expected_config_version: int | None,
    source_summary: dict[str, Any] | None = None,
    risk_level: str = "normal",
    allowed_industries: list[str] | None = None,
    blocked_industries: list[str] | None = None,
) -> dict[str, Any]:
    state = load_control_state()
    _assert_expected_version(state, expected_config_version)
    if style_code not in _known_style_codes():
        raise StyleControlBlocked(f"unknown_style_code: {style_code}")

    summary = source_summary or build_flywheel_summary(industry_key, style_code)
    sample_count = int(summary.get("sample_count") or 0)
    control_count = int(summary.get("control_sample_count") or 0)
    eligibility, reason = _eligibility(sample_count, control_count)
    prompt = _build_prompt_draft(style_code, industry_key, evidence_mode, summary)
    guard = evaluate_style_guard(prompt, evidence_mode=evidence_mode, risk_level=risk_level)
    version = {
        "version_id": _new_version_id(style_code),
        "style_code": style_code,
        "style_name": _style_name(style_code),
        "source": "flywheel_align",
        "status": "draft" if guard["decision"] != "blocked" else "blocked",
        "prompt_text": prompt,
        "prompt_sha256": _sha256(prompt),
        "strategy_summary": f"{_style_name(style_code)} · {industry_key} · {eligibility}",
        "evidence_chain": _evidence_chain(style_code, industry_key, evidence_mode, summary),
        "guard_summary": guard,
        "judge_summary": {
            "semantic_decision": "not_run",
            "provider_count": 0,
            "note": "一键对齐只生成 draft; 语义 judge 在发布前或 shadow 流程中运行。",
        },
        "created_at": _now_iso(),
        "created_by": int(actor_id or 0),
        "activated_at": None,
        "stable_baseline": False,
        "rollback_parent": state["stable_by_style"].get(style_code),
        "sample_count": sample_count,
        "control_sample_count": control_count,
        "risk_level": risk_level,
        "allowed_industries": allowed_industries or [],
        "blocked_industries": blocked_industries or [],
        "rollout_recommendation": {
            "eligibility": eligibility,
            "reason": reason,
            "may_activate": guard["decision"] != "blocked" and eligibility in {"eligible", "eligible_without_control", "shadow"},
            "customer_output_allowed": False,
        },
    }
    state["versions"].append(version)
    state = _save_state(state)
    _append_audit({
        "created_at": _now_iso(),
        "action": "align_draft",
        "actor_id": int(actor_id or 0),
        "style_code": style_code,
        "version_id": version["version_id"],
        "from_status": None,
        "to_status": version["status"],
        "note": "一键对齐生成 draft",
    })
    return {"version": version, "state": state}


def create_distilled_draft(
    *,
    style_code: str,
    industry_key: str,
    prompt_text: str,
    template_doc: str = "",
    evidence: dict[str, Any] | None = None,
    judge_summary: dict[str, Any] | None = None,
    strategy_summary: str = "",
    actor_id: int = 0,
    expected_config_version: int | None = None,
    evidence_mode: str = "with_evidence",
    risk_level: str = "normal",
    sample_count: int = 0,
    control_sample_count: int = 0,
    source: str = "answer_distiller",
) -> dict[str, Any]:
    """[W2] 落一个由服务层(蒸馏器)产出的候选 prompt 为 draft 版本。

    与 `align_draft_from_flywheel` 的唯一区别:prompt_text 由外部传入(蒸馏器已调 LLM 产出),
    本函数**不调 LLM** —— 只跑既有 `evaluate_style_guard` 守卫 + 持久化 + 审计,不违反控制面
    「不调 LLM / 不发布客户输出」铁律。守卫 block(内部字段泄漏/无证据违规/高危承诺)→ status=blocked,
    永不可 activate。落点是 draft,须管理员在看板走既有双闸(activate)才影响后续新文章。
    """
    state = load_control_state()
    if expected_config_version is not None:
        _assert_expected_version(state, expected_config_version)
    if style_code not in _known_style_codes():
        raise StyleControlBlocked(f"unknown_style_code: {style_code}")

    prompt = str(prompt_text or "")
    guard = evaluate_style_guard(prompt, evidence_mode=evidence_mode, risk_level=risk_level)
    eligibility, reason = _eligibility(int(sample_count or 0), int(control_sample_count or 0))
    version = {
        "version_id": _new_version_id(style_code),
        "style_code": style_code,
        "style_name": _style_name(style_code),
        "source": source,
        "status": "draft" if guard["decision"] != "blocked" else "blocked",
        "prompt_text": prompt,
        "prompt_sha256": _sha256(prompt),
        "answer_template_doc": str(template_doc or ""),  # [W2] 运营可读的「标准答案模板」人话文档
        "strategy_summary": strategy_summary or f"{_style_name(style_code)} · {industry_key} · {eligibility}",
        "evidence_chain": evidence or {},
        "guard_summary": guard,
        "judge_summary": judge_summary or {
            "semantic_decision": "not_run",
            "provider_count": 0,
            "note": "蒸馏只生成 draft; 语义评审在 W4 模拟对比流程中运行。",
        },
        "created_at": _now_iso(),
        "created_by": int(actor_id or 0),
        "activated_at": None,
        "stable_baseline": False,
        "rollback_parent": state["stable_by_style"].get(style_code),
        "sample_count": int(sample_count or 0),
        "control_sample_count": int(control_sample_count or 0),
        "risk_level": risk_level,
        "allowed_industries": [],
        "blocked_industries": [],
        "rollout_recommendation": {
            "eligibility": eligibility,
            "reason": reason,
            "may_activate": guard["decision"] != "blocked" and eligibility in {"eligible", "eligible_without_control", "shadow"},
            "customer_output_allowed": False,
        },
    }
    state["versions"].append(version)
    state = _save_state(state)
    _append_audit({
        "created_at": _now_iso(),
        "action": "distill_draft",
        "actor_id": int(actor_id or 0),
        "style_code": style_code,
        "version_id": version["version_id"],
        "from_status": None,
        "to_status": version["status"],
        "note": f"蒸馏候选生成 draft · {industry_key}",
    })
    return {"version": version, "state": state}


def get_active_version_id(style_code: str) -> str | None:
    """[W6] 取某文体当前 active 版本 id(供文章生成时打稳定归因指纹)。无 active/异常 → None。"""
    try:
        state = load_control_state()
        return (state.get("active_by_style") or {}).get(style_code) or None
    except Exception:
        return None


def get_style_version(version_id: str) -> dict[str, Any] | None:
    """Read one immutable style-version record without changing activation state."""
    try:
        state = load_control_state()
        for version in state.get("versions") or []:
            if version.get("version_id") == version_id:
                return dict(version)
    except Exception:
        return None
    return None


def set_version_judge_summary(version_id: str, judge_summary: dict[str, Any]) -> dict[str, Any]:
    """[W4] 把 AI 评审结论回写到某版本的 judge_summary(不改 status/prompt,只更新评审摘要)。"""
    state = load_control_state()
    version = _find_version(state, version_id)
    version["judge_summary"] = judge_summary or {}
    state = _save_state(state)
    _append_audit({
        "created_at": _now_iso(),
        "action": "set_judge_summary",
        "actor_id": 0,
        "style_code": version.get("style_code"),
        "version_id": version_id,
        "from_status": version.get("status"),
        "to_status": version.get("status"),
        "note": f"评审回写:{judge_summary.get('semantic_decision', '')}",
    })
    return {"version": version, "state": state}


def activate_style_version(
    *,
    version_id: str,
    actor_id: int,
    expected_config_version: int | None,
    note: str = "",
) -> dict[str, Any]:
    state = load_control_state()
    _assert_expected_version(state, expected_config_version)
    version = _find_version(state, version_id)
    if version.get("status") == "blocked":
        raise StyleControlBlocked("blocked_version_cannot_activate")
    guard = version.get("guard_summary") or {}
    if guard.get("worst_action") == BLOCK or guard.get("decision") == "blocked":
        raise StyleControlBlocked("guard_blocked")
    # Gate 7: manual click cannot promote a quality-only or historical-correlation
    # candidate. It must match an explicitly passed, pre-registered experiment;
    # the live evaluation is re-run so a stale state flag is not sufficient.
    from services.article_data_health import get_article_data_health
    from services.article_experiment_registry import can_activate_candidate
    from writing.article_style_contract import family_for_style

    data_health = get_article_data_health()
    style_code = version["style_code"]
    family = family_for_style(style_code)
    experiment_gate = can_activate_candidate(
        candidate_version_id=version_id,
        style_family=family or "mapping_unknown",
    )
    if not experiment_gate.get("allowed"):
        raise StyleControlBlocked(
            "experiment_gate_blocked:" + str(experiment_gate.get("reason") or "unknown")
        )
    version["activation_evidence"] = {
        "data_health_version": data_health.get("version"),
        "data_health_state": data_health.get("state"),
        "experiment_id": (experiment_gate.get("experiment") or {}).get("experiment_id"),
        "experiment_decision": (experiment_gate.get("experiment") or {}).get("decision"),
    }

    previous_active_id = state.get("active_by_style", {}).get(style_code)
    previous_status = version.get("status")
    for item in state.get("versions") or []:
        if item.get("style_code") == style_code and item.get("status") == "active":
            item["status"] = "stable" if item.get("stable_baseline") else "retired"
    version["status"] = "active"
    version["activated_at"] = _now_iso()
    version["activated_by"] = int(actor_id or 0)
    state["active_by_style"][style_code] = version_id

    from writing.style_registry import save_writing_config

    from writing.article_style_contract import STYLE_CONTRACT_VERSION
    save_writing_config(
        prompt_overrides={style_code: version["prompt_text"]},
        prompt_override_contract_versions={style_code: STYLE_CONTRACT_VERSION},
    )
    state = _save_state(state)
    _append_audit({
        "created_at": _now_iso(),
        "action": "activate",
        "actor_id": int(actor_id or 0),
        "style_code": style_code,
        "version_id": version_id,
        "from_status": previous_status,
        "to_status": "active",
        "previous_active_id": previous_active_id,
        "note": note,
    })
    return {"version": version, "state": state}


def rollback_style(
    *,
    style_code: str,
    actor_id: int,
    expected_config_version: int | None,
    note: str = "",
) -> dict[str, Any]:
    state = load_control_state()
    _assert_expected_version(state, expected_config_version)
    stable_id = state.get("stable_by_style", {}).get(style_code)
    if not stable_id:
        raise StyleControlNotFound(f"stable_baseline_not_found: {style_code}")
    stable = _find_version(state, stable_id)
    previous_active_id = state.get("active_by_style", {}).get(style_code)
    for item in state.get("versions") or []:
        if item.get("style_code") == style_code and item.get("status") == "active":
            item["status"] = "retired" if not item.get("stable_baseline") else "stable"
    stable["status"] = "active"
    stable["activated_at"] = _now_iso()
    stable["activated_by"] = int(actor_id or 0)
    state["active_by_style"][style_code] = stable_id

    from writing.style_registry import save_writing_config

    from writing.article_style_contract import STYLE_CONTRACT_VERSION
    save_writing_config(
        prompt_overrides={style_code: stable["prompt_text"]},
        prompt_override_contract_versions={style_code: STYLE_CONTRACT_VERSION},
    )
    state = _save_state(state)
    _append_audit({
        "created_at": _now_iso(),
        "action": "rollback",
        "actor_id": int(actor_id or 0),
        "style_code": style_code,
        "version_id": stable_id,
        "from_status": previous_active_id,
        "to_status": "active",
        "note": note,
    })
    return {"active_version": stable, "state": state}


def retire_style_version(
    *,
    version_id: str,
    actor_id: int,
    expected_config_version: int | None,
    note: str = "",
) -> dict[str, Any]:
    state = load_control_state()
    _assert_expected_version(state, expected_config_version)
    version = _find_version(state, version_id)
    if version.get("stable_baseline"):
        raise StyleControlBlocked("stable_baseline_cannot_retire")
    if state.get("active_by_style", {}).get(version.get("style_code")) == version_id:
        raise StyleControlBlocked("active_version_cannot_retire")
    previous = version.get("status")
    version["status"] = "retired"
    state = _save_state(state)
    _append_audit({
        "created_at": _now_iso(),
        "action": "retire",
        "actor_id": int(actor_id or 0),
        "style_code": version.get("style_code"),
        "version_id": version_id,
        "from_status": previous,
        "to_status": "retired",
        "note": note,
    })
    return {"version": version, "state": state}


def list_style_versions(style_code: str | None = None, status: str | None = None) -> dict[str, Any]:
    state = load_control_state()
    versions = list(state.get("versions") or [])
    if style_code:
        versions = [v for v in versions if v.get("style_code") == style_code]
    if status:
        versions = [v for v in versions if v.get("status") == status]
    return {
        "schema_version": state["schema_version"],
        "config_version": state["config_version"],
        "active_by_style": state.get("active_by_style") or {},
        "stable_by_style": state.get("stable_by_style") or {},
        "versions": versions,
    }


def get_style_version(version_id: str) -> dict[str, Any]:
    state = load_control_state()
    version = _find_version(state, version_id)
    return {"config_version": state["config_version"], "version": version}


def get_evidence_chain(version_id: str) -> dict[str, Any]:
    payload = get_style_version(version_id)
    version = payload["version"]
    return {
        "config_version": payload["config_version"],
        "version_id": version_id,
        "style_code": version.get("style_code"),
        "status": version.get("status"),
        "evidence_chain": version.get("evidence_chain") or {},
        "guard_summary": version.get("guard_summary") or {},
        "judge_summary": version.get("judge_summary") or {},
        "rollout_recommendation": version.get("rollout_recommendation") or {},
    }


def list_audit_events(limit: int = 100) -> list[dict[str, Any]]:
    path = _audit_path()
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except Exception:
            continue
    return events[-max(1, min(int(limit or 100), 500)) :]
