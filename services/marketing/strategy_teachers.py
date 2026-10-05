"""Versioned, replaceable strategy-teacher registry for GEO content packages.

The registry is code-versioned while tenant defaults reuse ``marketing_policies``
and preference audits reuse ``marketing_events``.  A material job always stores
the full returned snapshot; historic work therefore never follows a later
teacher edit or tenant preference change.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

from db import marketing_db


@dataclass(frozen=True)
class StrategyTeacher:
    teacher_id: str
    name: str
    version: str
    system_method: str
    capabilities: tuple[str, ...]
    channel_boundaries: tuple[str, ...]
    enabled: bool = True

    def snapshot(self) -> dict:
        data = asdict(self)
        data["capabilities"] = list(self.capabilities)
        data["channel_boundaries"] = list(self.channel_boundaries)
        return data


_TEACHERS: tuple[StrategyTeacher, ...] = (
    StrategyTeacher(
        teacher_id="shu",
        name="舒老师",
        version="1.0.0",
        system_method=(
            "受众现状→行动阻力→一句人话问题→唯一卖点→真实证据→低门槛唯一行动。"
            "先把术语翻成人话；只处理定位、核心卖点、人话和承诺边界。"
        ),
        capabilities=("positioning", "human_language", "single_value", "claim_boundary"),
        channel_boundaries=(
            "不代替渠道专家写平台套路",
            "不虚构数字、客户证言、排名或收益",
            "一份内容只保留一个人群、一个卖点、一个行动",
        ),
    ),
    StrategyTeacher(
        teacher_id="b2b_sales_coach",
        name="B2B 销售教练",
        version="1.0.0",
        system_method=(
            "购买角色→触发场景→业务风险→可验证价值→异议边界→下一次低风险会谈。"
            "适合高客单服务商获客；渠道表达仍交给对应渠道合同。"
        ),
        capabilities=("b2b_positioning", "objection_mapping", "low_friction_cta"),
        channel_boundaries=(
            "不公开报价或制造虚假紧迫",
            "不把销售推断写成客户事实",
            "不覆盖渠道尺寸、语气和平台合规合同",
        ),
    ),
)

PLATFORM_DEFAULT = ("shu", "1.0.0")


def list_teachers(*, include_disabled: bool = False) -> list[dict]:
    return [t.snapshot() for t in _TEACHERS if include_disabled or t.enabled]


def _lookup(teacher_id: str, version: str) -> StrategyTeacher:
    for teacher in _TEACHERS:
        if teacher.teacher_id == teacher_id and teacher.version == version:
            if not teacher.enabled:
                raise ValueError("strategy_teacher_disabled")
            return teacher
    raise ValueError("strategy_teacher_not_found")


def preference_key(principal_user_id: int) -> str:
    return f"marketing.teacher.default.user.{int(principal_user_id)}"


def get_default_preference(principal_user_id: Optional[int]) -> dict:
    if principal_user_id:
        value = marketing_db.get_policy(preference_key(principal_user_id)) or {}
        teacher_id = str(value.get("teacher_id") or "").strip()
        version = str(value.get("version") or "").strip()
        if teacher_id and version:
            try:
                return {"source": "tenant", **_lookup(teacher_id, version).snapshot()}
            except ValueError:
                # A removed/disabled tenant preference never makes generation
                # silently use that teacher.  It falls back to the documented
                # platform default and reports the source to the caller.
                pass
    teacher = _lookup(*PLATFORM_DEFAULT)
    return {"source": "platform", **teacher.snapshot()}


def resolve_teacher(
    *,
    teacher_id: Optional[str] = None,
    version: Optional[str] = None,
    principal_user_id: Optional[int] = None,
) -> dict:
    if teacher_id or version:
        if not teacher_id or not version:
            raise ValueError("strategy_teacher_version_required")
        return {"source": "request", **_lookup(str(teacher_id), str(version)).snapshot()}
    return get_default_preference(principal_user_id)


def set_default_preference(
    *,
    principal_user_id: int,
    teacher_id: str,
    version: str,
    actor_user_id: int,
    reason: str = "",
) -> dict:
    teacher = _lookup(teacher_id, version)
    key = preference_key(principal_user_id)
    before = marketing_db.get_policy(key) or {}
    value = {
        "teacher_id": teacher.teacher_id,
        "version": teacher.version,
        "enabled": True,
    }
    marketing_db.set_policy(key, value, updated_by=actor_user_id)
    marketing_db.add_event(
        event_type="strategy_teacher_preference_changed",
        actor_id=actor_user_id,
        message=f"策略导师默认值切换为 {teacher.name} {teacher.version}",
        payload={
            "principal_user_id": int(principal_user_id),
            "before": before,
            "after": value,
            "reason": str(reason or "")[:200],
        },
    )
    return {"source": "tenant", **teacher.snapshot()}
