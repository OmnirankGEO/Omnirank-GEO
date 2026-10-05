# -*- coding: utf-8 -*-
"""P1-5a · 飞轮止血护栏 · 判别锁(2026-08-14)。

背景(研究定稿 §12,Review 裁定纠正后的口径):can_apply(数据置信位)恒 false
期间,B5 的 LLM 文体选择仍实改了 4/10 批次 37 个 topic,且 outcome_events 恒 0
→ 活跃且不可自纠的偏差源。护栏 = can_apply=False 时不应用 LLM 选择,退回
ratio 抽签;can_apply 转真值后闸自然打开(止血与 D6-B 回流是同一开关两侧)。

变异点:
  M1 拆闸(删 `if not can_apply` 分支)→ test_guard_blocks_llm_choice_without_data 红;
  M2 拆接线(server 不传 can_apply)→ test_server_wiring_passes_can_apply 红。
"""
from __future__ import annotations

import inspect

import pytest

from services.flywheel_writing_strategy_choice import apply_writing_style_choice

_CHOICE_LLM = {"style_family": "buying_guide", "source": "llm", "angle": "", "reason": "意图匹配"}


@pytest.fixture()
def _resolvable(monkeypatch):
    import writing.style_registry as style_registry

    monkeypatch.setattr(style_registry, "resolve_user_choice", lambda family, industry: family)


def _topics():
    return [{"id": 1, "user_choice": "auto"}, {"id": 2, "user_choice": "guide"}]


def test_guard_blocks_llm_choice_without_data(_resolvable) -> None:
    topics = _topics()
    result = apply_writing_style_choice(topics, dict(_CHOICE_LLM), "家居", can_apply=False)
    assert result["applied_topics"] == 0
    assert result["skip_reason"] == "no_citation_data_guard"
    assert topics[0]["user_choice"] == "auto", "闸上了 topic 还是被改 —— 没退回 ratio 抽签"


def test_guard_opens_with_data(_resolvable) -> None:
    # 反向对照:can_apply=True + source=llm → 正常应用(闸不是恒关)
    topics = _topics()
    result = apply_writing_style_choice(topics, dict(_CHOICE_LLM), "家居", can_apply=True)
    assert result["applied_topics"] == 1
    assert topics[0]["user_choice"] == "buying_guide"
    assert topics[1]["user_choice"] == "guide", "非 auto 档被接管(既有边界 1 被破)"


def test_rule_source_reason_not_masked(_resolvable) -> None:
    # rule 兜底本来就不应用;skip_reason 必须仍报 source,不被数据闸抢走归因
    result = apply_writing_style_choice(
        _topics(), {"style_family": "buying_guide", "source": "rule"}, "家居", can_apply=False,
    )
    assert result["skip_reason"] == "source=rule"


def test_default_keeps_legacy_signature(_resolvable) -> None:
    # 不传 can_apply = 旧签名行为(既有调用方零破坏);生产接线必须显式传真实值
    topics = _topics()
    result = apply_writing_style_choice(topics, dict(_CHOICE_LLM), "家居")
    assert result["applied_topics"] == 1


def test_server_wiring_passes_can_apply() -> None:
    """接线锁:server.py 调用点必须把 guidance 的 can_apply 真传进来。

    没有这条,「服务层 ✅ 参数 ✅ 调用方零传参」= 死护栏(本仓 08-13 invrel
    补录端点没接前端的同型事故)。"""
    from pathlib import Path

    server_path = Path(__file__).resolve().parents[1] / "server.py"
    src = server_path.read_text(encoding="utf-8")
    assert 'can_apply=bool(_guidance_payload.get("can_apply"))' in src
