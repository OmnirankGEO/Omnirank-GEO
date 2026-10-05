"""分层采样策略 + 重复规则 + 异常复测触发(§6 / §9)。

原则:
  - 重复次数是**配置**,不是常量(env > 传入 default)。
  - 重复结果是独立 observation,不在采集层平均成一条。
  - 异常触发追加复测,用确定性信号(response_status 变化 / 引用全消失 / 模型版本变化 /
    平台表面版本变化),不交给 LLM 判断。
  - 客户购买的 monitoring_query 是规范问题 SSOT;有值时逐字一致(由 adapter 保证)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

from services.ai_surface_monitoring.contracts import ObservationEnvelopeV1

logger = logging.getLogger("GEO-AISurface-Sampling")


class RepeatCondition(str, Enum):
    NORMAL = "normal"                          # 正常稳定问题
    YOY_CHANGE_OVER_THRESHOLD = "yoy_change"   # 同比变化超阈值
    MODEL_VERSION_CHANGE = "model_version"     # 平台模型版本变化
    REFUSED = "refused"                        # 拒绝推荐/无法推荐
    ENTITY_AMBIGUOUS = "entity_ambiguous"      # 品牌歧义/相似品牌
    NEW_PLATFORM_RESEARCH = "new_platform"     # 未知新平台研究(代表性金标准集)
    NO_SEARCH_COMPARISON = "no_search"         # 核心题族关闭联网对照


# §6.2 默认重复规则(可被 policy/env 覆盖)。new_platform 用金标准集另行处理,不给固定 int。
DEFAULT_REPEAT_COUNTS: Dict[RepeatCondition, int] = {
    RepeatCondition.NORMAL: 1,
    RepeatCondition.YOY_CHANGE_OVER_THRESHOLD: 3,
    RepeatCondition.MODEL_VERSION_CHANGE: 3,
    RepeatCondition.REFUSED: 2,
    RepeatCondition.ENTITY_AMBIGUOUS: 3,
    RepeatCondition.NO_SEARCH_COMPARISON: 1,
}

# 老板已裁定元宝 Hy3 为默认通道 → 不适用"未知新平台代表性金标准集"准入
_NEW_PLATFORM_EXEMPT_SURFACES = frozenset({"yuanbao_hy3_tokenhub"})


def repeat_count(condition: RepeatCondition, overrides: Optional[Dict[RepeatCondition, int]] = None) -> int:
    """返回某条件下的重复采样次数。overrides(来自 policy)优先于默认。"""
    table = dict(DEFAULT_REPEAT_COUNTS)
    if overrides:
        table.update(overrides)
    return int(table.get(condition, 1))


@dataclass(frozen=True)
class AnomalyDecision:
    should_repeat: bool
    extra_runs: int
    reason: Optional[str] = None


def detect_anomaly(
    prev: Optional[ObservationEnvelopeV1],
    curr: ObservationEnvelopeV1,
    *,
    overrides: Optional[Dict[RepeatCondition, int]] = None,
) -> AnomalyDecision:
    """比对上一条与当前 observation,决定是否追加复测。确定性信号,不用 LLM。

    触发(§9):
      - 平台/模型/搜索表面版本变化(model_revision / surface_key;搜索开关见下)。
      - 引用来源全部消失(prev 有 citation,curr 变 0)。
      - 明确回答 ↔ 拒绝/未知 的状态翻转。

    关于 search_enabled(§9 列它为搜索表面信号):当前所有 adapter 的 envelope.search_enabled
    恒等于 surface 的 spec.default_search_enabled(base/openai_compat/wrapped 均如此),即
    **由 surface_key 唯一决定**、同一 surface 内为常量。故"搜索开/关变化"必然表现为 surface_key
    变化,已被下方 surface_key 比对覆盖 —— 无需(也不应)对同 surface_key 再单独比 search_enabled
    (那样只会命中生产不可达的输入,徒增 dead branch 与假绿测试)。base.py:246 的
    `result.search_enabled ?? default` 是留给"未来某 adapter 逐条上报 search_enabled"的休眠钩子;
    真接入那天再让本函数增补比对并配可达的判别测试。(复审#2 R6-3-R13/R14 已就此定谳)
    """
    if prev is None:
        return AnomalyDecision(False, 0)

    # 表面/模型版本变化(search_enabled 由 surface_key 决定 → 搜索开关变化已在此覆盖,见 docstring)
    if prev.surface_key != curr.surface_key or (prev.model_revision or "") != (curr.model_revision or ""):
        return AnomalyDecision(True, repeat_count(RepeatCondition.MODEL_VERSION_CHANGE, overrides) - 1,
                               "model/surface version change")

    # 明确回答 ↔ 拒绝/未知(§9:出现变未出现或反向、明确推荐变拒绝均触发)
    answered_states = {"answered"}
    refused_or_unknown = {"refused", "unknown"}
    if (prev.response_status in answered_states and curr.response_status in refused_or_unknown) or (
        prev.response_status in refused_or_unknown and curr.response_status in answered_states
    ):
        return AnomalyDecision(True, repeat_count(RepeatCondition.REFUSED, overrides) - 1,
                               "answered<->refused/unknown flip")

    # 引用全消失
    prev_cited = sum(1 for c in prev.citations if c.source_type == "citation")
    curr_cited = sum(1 for c in curr.citations if c.source_type == "citation")
    if prev_cited > 0 and curr_cited == 0:
        return AnomalyDecision(True, repeat_count(RepeatCondition.YOY_CHANGE_OVER_THRESHOLD, overrides) - 1,
                               "all citations disappeared")

    return AnomalyDecision(False, 0)


@dataclass(frozen=True)
class SampleSpec:
    """一次待执行采样(surface × run_index)。"""

    surface_key: str
    run_index: int


def build_sample_specs(
    surface_keys: List[str],
    *,
    condition: RepeatCondition = RepeatCondition.NORMAL,
    overrides: Optional[Dict[RepeatCondition, int]] = None,
) -> List[SampleSpec]:
    """给定要跑的表面集合 + 条件,展开成 (surface, run_index) 列表。

    new_platform 条件对元宝 Hy3 豁免(老板已裁定默认通道,不再要求金标准集准入)。
    """
    specs: List[SampleSpec] = []
    for sk in surface_keys:
        if condition == RepeatCondition.NEW_PLATFORM_RESEARCH:
            if sk in _NEW_PLATFORM_EXEMPT_SURFACES:
                runs = 1
            else:
                # 代表性金标准集:用 3 次作为可运行下界(真实金标准集大小由校准 harness 决定)
                runs = 3
        else:
            runs = repeat_count(condition, overrides)
        for i in range(max(runs, 1)):
            specs.append(SampleSpec(surface_key=sk, run_index=i + 1))
    return specs


def cap_round_calls(specs: List[SampleSpec], max_calls_per_round: Optional[int]) -> List[SampleSpec]:
    """按 policy.sampling_budget.max_calls_per_round 截断单轮采样计划(复审 P1-3:该上限此前无消费方)。

    **不静默**:被丢弃的 spec 数量会 log 出来,避免"以为全跑了实际截断"。None/<=0 表示不限。
    """
    if not max_calls_per_round or max_calls_per_round <= 0:
        return specs
    if len(specs) <= max_calls_per_round:
        return specs
    dropped = len(specs) - max_calls_per_round
    logger.warning("[sampling] 单轮采样计划 %d 条超 max_calls_per_round=%d,截断丢弃 %d 条(非静默)",
                   len(specs), max_calls_per_round, dropped)
    return specs[:max_calls_per_round]
