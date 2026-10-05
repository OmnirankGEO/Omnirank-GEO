"""Admin runtime feature switches for writing/flywheel controls.

The settings are JSON-backed to avoid a schema migration for this control
plane. Defaults preserve current production behavior: prompt overrides remain
available, structure guidance remains opt-in usable, and all customer-output
paths stay closed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FEATURE_SWITCH_FILE_ENV = "WRITING_FEATURE_SWITCH_FILE"
DEFAULT_FEATURE_SWITCH_FILE = "data/writing_feature_switches.json"


class FeatureSwitchError(Exception):
    """Base class for writing feature switch errors."""


class FeatureSwitchNotFound(FeatureSwitchError):
    """Raised when a switch key is unknown."""


class FeatureSwitchConflict(FeatureSwitchError):
    """Raised when optimistic locking fails."""


class FeatureSwitchLocked(FeatureSwitchError):
    """Raised when a permanently locked switch is changed."""


class FeatureSwitchConfirmationRequired(FeatureSwitchError):
    """Raised when a dangerous change lacks confirmation."""


@dataclass(frozen=True)
class FeatureSwitchSpec:
    key: str
    label: str
    description: str
    consequence: str
    default_enabled: bool
    source: str
    locked: bool = False
    dangerous: bool = False
    enable_confirm: str = ""
    disable_confirm: str = ""


SWITCH_SPECS: dict[str, FeatureSwitchSpec] = {
    "writing_style_overrides": FeatureSwitchSpec(
        key="writing_style_overrides",
        label="新模板用于后续文章",
        description="决定管理员审核通过的新文章模板是否用于后续生成。",
        consequence="只有存在已启用的新模板时才会实际生效；如果当前启用模板为 0，后续文章仍使用系统默认模板。",
        default_enabled=True,
        source="data/writing_style_versions.json active_by_style + writing_config prompt_overrides",
        dangerous=True,
        enable_confirm="ENABLE_WRITING_STYLE_OVERRIDES",
        disable_confirm="DISABLE_WRITING_STYLE_OVERRIDES",
    ),
    "r6h_shadow_injection": FeatureSwitchSpec(
        key="r6h_shadow_injection",
        label="内部观察记录",
        description="保存文章后，额外留一份仅管理员可看的检查记录。",
        consequence="只用于内部排查，不改变文章正文，也不会给客户看。",
        default_enabled=False,
        source="writing.shadow_only_injection.load_shadow_injection_config",
    ),
    "r6h_customer_output": FeatureSwitchSpec(
        key="r6h_customer_output",
        label="实验内容给客户看",
        description="是否允许实验链路直接影响客户能看到的内容。",
        consequence="永久关闭；界面和后端都锁死，当前不提供开启入口。",
        default_enabled=False,
        source="R6H customer output hard gate",
        locked=True,
        dangerous=True,
        enable_confirm="ENABLE_R6H_CUSTOMER_OUTPUT",
    ),
    "structure_guidance": FeatureSwitchSpec(
        key="structure_guidance",
        label="文章结构建议",
        description="生成文章时，可参考已沉淀的文章结构经验。",
        consequence="开启后，选择使用结构建议的文章会参考它；关闭后，文章按原流程生成。",
        default_enabled=True,
        source="server.py api_start_articles use_structure_guidance gate",
    ),
    "media_takeover": FeatureSwitchSpec(
        key="media_takeover",
        label="媒体推荐接管",
        description="当媒体资料、白名单和绑定都达标后，允许媒体推荐优先使用飞轮审核结果。",
        consequence="开启后，已审核通过的行业会优先使用飞轮推荐；未达标或无策略时自动回到旧推荐，不直接发布给客户。",
        default_enabled=False,
        source="services.media_flywheel_recommendation + placement_service.recommend_for_publish_v2",
        dangerous=True,
        enable_confirm="ENABLE_MEDIA_TAKEOVER",
    ),
    "diagnosis_engine_weight_shadow": FeatureSwitchSpec(
        key="diagnosis_engine_weight_shadow",
        label="诊断品牌词占有·引擎加权观测分",
        description="诊断报告在原「品牌词占有」分之外，额外并行算一份按引擎权重加权的观测分（只观测、不改生产分）。",
        consequence="开启后仅在报告结果里多出一个内部观测字段，供对比新旧口径；不改变客户看到的总分/等级，也不重算历史报告。关闭时评分输出与改前完全一致。",
        default_enabled=False,
        source="tools.scoring.geo_scorer._weighted_brand_ownership_shadow",
    ),
    "monitoring_scheduled_distillation": FeatureSwitchSpec(
        key="monitoring_scheduled_distillation",
        label="定时监测后自动蒸馏洞察",
        description="定时监测任务完成后,自动生成关键词洞察(与手动监测一致)。",
        consequence="开启后每次定时监测完成会额外触发一次 LLM 蒸馏(平台承担成本,不向用户扣费);关闭时定时监测行为与改前完全一致。",
        default_enabled=False,
        source="api/scheduler.py::_async_run_brand + tools.distillation.trigger.trigger_distillation",
    ),
    "flywheel_round_bridge": FeatureSwitchSpec(
        key="flywheel_round_bridge",
        label="调研跑批后自动刷新飞轮影子层",
        description="调研 round 完成后,自动按序刷新飞轮下游影子层(来源信号→答案采纳→媒体主体→绑定候选→写作策略候选),不再依赖手动 rebuild。",
        consequence="开启后每轮调研完成会补跑下游影子/候选层(仅 SQL/规则聚合,不调 LLM,不接管客户可见输出);只跑到「候选/影子」为止,媒体绑定生效、策略启用、接管仍须人工审核。关闭时定时/手动调研行为与改前完全一致。",
        default_enabled=False,
        source="api/scheduler round-complete hook + services.research_monitor.flywheel_bridge.run_round_bridge",
    ),
    "flywheel_answer_entity_auto": FeatureSwitchSpec(
        key="flywheel_answer_entity_auto",
        label="飞轮桥接内自动抽取答案实体(耗 LLM)",
        description="在「调研跑批后自动刷新飞轮影子层」链里,额外对本轮答案做实体抽取(需调 LLM,受 per-round / 每日成本上限约束)。",
        consequence="开启且飞轮桥接也开启时,每轮完成会对待抽答案做一次 LLM 实体抽取(平台承担成本,受 FLYWHEEL_ANSWER_ENTITY_PER_ROUND_CAP / FLYWHEEL_ANSWER_ENTITY_DAILY_CAP 上限约束);关闭时飞轮桥接跳过答案实体抽取,不产生任何 LLM 成本。",
        default_enabled=False,
        dangerous=True,
        enable_confirm="ENABLE_FLYWHEEL_ANSWER_ENTITY_AUTO",
        source="services.research_monitor.flywheel_bridge answer-entity stage + answer_entity_extractor.rebuild_answer_entities",
    ),
    "answer_entity_auto_extract": FeatureSwitchSpec(
        key="answer_entity_auto_extract",
        label="定时每日增量抽取答案实体(耗 LLM)",
        description="每日定时对新答案组做增量实体抽取(only_pending),独立于调研 round 触发,受单日组数上限约束。",
        consequence="开启后 scheduler 每日增量抽取待抽答案组(平台承担 LLM 成本,受 ANSWER_ENTITY_DAILY_EXTRACT_CAP 上限,默认 500 组);关闭时 scheduler 行为与改前完全一致,不产生任何 LLM 成本。",
        default_enabled=False,
        dangerous=True,
        enable_confirm="ENABLE_ANSWER_ENTITY_AUTO_EXTRACT",
        source="api/scheduler daily job + services.research_monitor.answer_entity_extractor.rebuild_answer_entities",
    ),
    "binding_auto_approve": FeatureSwitchSpec(
        key="binding_auto_approve",
        label="高置信媒体绑定候选自动通过",
        description="候选 rebuild 产出的 domain_exact 精确域名匹配(置信≥0.95、无风险、可采购)自动通过,免人工点。",
        consequence="开启后仅对 rebuild 新产出的高置信 domain_exact 候选自动 approve(走既有实时库存校验,不可采购照样拦),reviewed_by 记 system:auto、独立审计;name_alias(0.86 名字匹配)不自动。只写后台数据,不改线上推荐(推荐融合已退役、接管开关关闭),不越 media_takeover 授权闸门。关闭时零自动,与改前字节一致。",
        default_enabled=False,
        dangerous=True,
        enable_confirm="ENABLE_BINDING_AUTO_APPROVE",
        source="api/media_entity_flywheel_api rebuild _work + services.media_binding_candidates.is_auto_approvable",
    ),
    "query_intent_auto_classify": FeatureSwitchSpec(
        key="query_intent_auto_classify",
        label="定时自动给搜索问题打「问题类型」标签(耗 LLM)",
        description="每日定时对尚未打标签的搜索问题做「问题类型」分类(榜单/教程/对比/数据/政策/定义/FAQ 等 8 类),供写作蒸馏按问题类型分组。",
        consequence="开启后 scheduler 每日对新问题做 LLM 分类(平台承担成本,受 QUERY_INTENT_DAILY_CLASSIFY_CAP 上限,默认 500 条);关闭时 scheduler 行为与改前字节一致,不产生任何 LLM 成本(仍可管理员手动 dry-run 预估后再真跑)。",
        default_enabled=False,
        dangerous=True,
        enable_confirm="ENABLE_QUERY_INTENT_AUTO_CLASSIFY",
        source="api/scheduler daily job + services.research_monitor.query_intent_classifier.classify_query_intent",
    ),
    "writing_distill_auto": FeatureSwitchSpec(
        key="writing_distill_auto",
        label="新语料到位后自动蒸馏写作候选(耗 LLM)",
        description="当某(行业×问题类型×文体)组的采纳语料够量时,自动用 LLM 蒸馏一份「候选模板 + 候选 prompt」写入版本面草稿,供管理员在看板审核。",
        consequence="开启后 scheduler 定时对达标组做 LLM 蒸馏(平台承担成本,受 WRITING_DISTILL_AUTO_CAP 上限,默认每次 10 组);产物只落「草稿」态,绝不直接影响客户可见文章(仍需管理员在看板点采纳走既有双闸)。关闭时无任何自动蒸馏,与改前字节一致(仍可管理员手动 dry-run 预估后真跑)。",
        default_enabled=False,
        dangerous=True,
        enable_confirm="ENABLE_WRITING_DISTILL_AUTO",
        source="api/scheduler daily job + services.writing_answer_distiller.distill_candidates",
    ),
    "writing_outcome_backfill": FeatureSwitchSpec(
        key="writing_outcome_backfill",
        label="定时回写模板上线后的真实被引效果",
        description="每周定时对上线 ≥30 天的写作模板版本,查监测/引用数据算出「新版 vs 旧版被引率」,写入效果事件表供看板真实回环面板。",
        consequence="开启后 scheduler 每周回写效果事件(只读监测/引用数据 + 写内部效果表,不调 LLM、不改客户可见输出、不自动回滚);看板据此亮绿灯/黄灯给「建议回滚」提示。关闭时不回写,看板回环面板显示「效果数据积累中」,与改前字节一致。",
        default_enabled=False,
        source="api/scheduler weekly job + services.writing_outcome_backfill.backfill_writing_outcomes",
    ),
    "geo_article_evolution_auto": FeatureSwitchSpec(
        key="geo_article_evolution_auto",
        label="每月两次生成 GEO 文章进化审计",
        description="每月 1 日和 16 日汇总发布快照、AI 监测血缘、Jina 正文等级、文章审核、问题候选和预注册实验，生成待人工签发的进化记录。",
        consequence="只写内部审计快照，不自动启用模板、不调整文章配比、不修改客户购买问题，也不承诺引用提升。",
        default_enabled=False,
        source="api/scheduler + services.article_evolution_cycle",
        dangerous=True,
        enable_confirm="ENABLE_GEO_ARTICLE_EVOLUTION_AUTO",
    ),
    "entity_rank_boost": FeatureSwitchSpec(
        key="entity_rank_boost",
        label="飞轮有效分参与发布媒体排序",
        description="发布推荐组合包(试投/稳妥/权威)排序时,对已审核通过的媒体绑定按飞轮有效分(答案实体点名 + 媒体 shadow 分)做同分微重排。",
        consequence="开启后,组合包排序在原有效分基础上,对有 approved 绑定 + 飞轮有效分的媒体做小幅加权(同分优先),影响客户可见的媒体推荐顺序;无绑定/无飞轮分的媒体不受影响,也不改各媒体的可投放判定。关闭时排序与改前字节一致,不取任何飞轮数据(零成本)。",
        default_enabled=False,
        source="services.publish_recommendation.build_recommendation_packages entity_rank_boost + db.media_entity_flywheel_db approved绑定/shadow",
        dangerous=True,
        enable_confirm="ENABLE_ENTITY_RANK_BOOST",
    ),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _config_path() -> Path:
    return Path(os.getenv(FEATURE_SWITCH_FILE_ENV) or DEFAULT_FEATURE_SWITCH_FILE)


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


def _bootstrap_state() -> dict[str, Any]:
    now = _now_iso()
    return {
        "schema_version": 1,
        "config_version": 1,
        "updated_at": now,
        "switches": {
            key: {
                "enabled": spec.default_enabled,
                "updated_at": now,
                "updated_by": 0,
                "note": "default",
            }
            for key, spec in SWITCH_SPECS.items()
        },
    }


def _normalize_state(raw: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
    state = raw if isinstance(raw, dict) else _bootstrap_state()
    changed = not isinstance(raw, dict)
    state.setdefault("schema_version", 1)
    state.setdefault("config_version", 1)
    state.setdefault("updated_at", _now_iso())
    switches = state.setdefault("switches", {})
    now = _now_iso()
    for key, spec in SWITCH_SPECS.items():
        if not isinstance(switches.get(key), dict):
            switches[key] = {
                "enabled": spec.default_enabled,
                "updated_at": now,
                "updated_by": 0,
                "note": "default",
            }
            changed = True
        if spec.locked and switches[key].get("enabled") is not False:
            switches[key]["enabled"] = False
            switches[key]["note"] = "locked_false"
            switches[key]["updated_at"] = now
            changed = True
    return state, changed


def _safe_switch(key: str, item: dict[str, Any]) -> dict[str, Any]:
    spec = SWITCH_SPECS[key]
    enabled = bool(item.get("enabled")) and not spec.locked
    return {
        "key": spec.key,
        "label": spec.label,
        "description": spec.description,
        "consequence": spec.consequence,
        "enabled": enabled,
        "locked": spec.locked,
        "dangerous": spec.dangerous,
        "requires_confirmation": bool(spec.enable_confirm or spec.disable_confirm),
        "enable_confirm": spec.enable_confirm,
        "disable_confirm": spec.disable_confirm,
        "source": spec.source,
        "updated_at": item.get("updated_at"),
        "updated_by": int(item.get("updated_by") or 0),
        "note": str(item.get("note") or ""),
    }


def get_feature_switch_state() -> dict[str, Any]:
    path = _config_path()
    state, changed = _normalize_state(_read_json(path))
    if changed or not path.exists():
        _write_json(path, state)
    return {
        "schema_version": state.get("schema_version"),
        "config_version": int(state.get("config_version") or 1),
        "updated_at": state.get("updated_at"),
        "switches": [_safe_switch(key, state["switches"][key]) for key in SWITCH_SPECS],
    }


def _load_state_for_write() -> dict[str, Any]:
    state, changed = _normalize_state(_read_json(_config_path()))
    if changed:
        _write_json(_config_path(), state)
    return state


def get_switch(key: str) -> dict[str, Any]:
    if key not in SWITCH_SPECS:
        raise FeatureSwitchNotFound(f"unknown_feature_switch: {key}")
    state = _load_state_for_write()
    return _safe_switch(key, state["switches"][key])


def is_feature_enabled(key: str) -> bool:
    try:
        return bool(get_switch(key).get("enabled"))
    except FeatureSwitchError:
        return False


def _assert_expected_version(state: dict[str, Any], expected_config_version: int | None) -> None:
    if expected_config_version is None:
        raise FeatureSwitchConflict("config_version_required")
    if int(expected_config_version) != int(state.get("config_version") or 0):
        raise FeatureSwitchConflict(
            f"config_version_conflict: expected {expected_config_version}, got {state.get('config_version')}"
        )


def update_feature_switch(
    key: str,
    enabled: bool,
    *,
    actor_id: int = 0,
    expected_config_version: int | None,
    note: str = "",
    confirm: str = "",
) -> dict[str, Any]:
    if key not in SWITCH_SPECS:
        raise FeatureSwitchNotFound(f"unknown_feature_switch: {key}")
    spec = SWITCH_SPECS[key]
    if spec.locked and enabled:
        raise FeatureSwitchLocked(f"feature_switch_locked: {key}")

    state = _load_state_for_write()
    _assert_expected_version(state, expected_config_version)

    required = spec.enable_confirm if enabled else spec.disable_confirm
    if required and confirm != required:
        raise FeatureSwitchConfirmationRequired(f"confirmation_required: {required}")

    item = state["switches"][key]
    item["enabled"] = bool(enabled) and not spec.locked
    item["updated_at"] = _now_iso()
    item["updated_by"] = int(actor_id or 0)
    item["note"] = str(note or "")[:500]
    state["config_version"] = int(state.get("config_version") or 1) + 1
    state["updated_at"] = _now_iso()
    _write_json(_config_path(), state)
    return {
        "schema_version": state.get("schema_version"),
        "config_version": state["config_version"],
        "updated_at": state["updated_at"],
        "switch": _safe_switch(key, item),
    }
