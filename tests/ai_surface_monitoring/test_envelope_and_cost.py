"""信封 + 成本(§10.4 / Codex #5,#6):整数 micros 无 float、版本化 Hy3、未知 null、保守回落。"""

from __future__ import annotations

import pytest

from services.ai_surface_monitoring.contracts import (
    ObservationEnvelopeV1,
    UsageEvidence,
    canonical_question_hash,
    normalize_question,
)
from services.ai_surface_monitoring.cost_policy import (
    MICROS_PER_YUAN,
    PRICING_VERSION,
    budget_charge_micros,
    estimate_usage_cost,
)


def test_micros_are_integers_no_float():
    u = estimate_usage_cost("yuanbao_hy3_tokenhub", input_tokens=1234, output_tokens=567, cached_tokens=89)
    assert isinstance(u.estimated_cost_micros, int)
    # 手算:uncached=1234-89=1145 → 1145*1 + 89*0.25 + 567*4 (每token micros) = 1145 + 22(floor 22.25) ... 用整表
    # (1145*1_000_000 + 89*250_000 + 567*4_000_000)//1_000_000
    expected = (1145 * 1_000_000 + 89 * 250_000 + 567 * 4_000_000) // 1_000_000
    assert u.estimated_cost_micros == expected


def test_hy3_pricing_is_versioned_not_flat():
    # 两个不同 token 量 → 不同成本(证明按 token 计,非固定每次)
    small = estimate_usage_cost("yuanbao_hy3_tokenhub", 100, 100, 0).estimated_cost_micros
    big = estimate_usage_cost("yuanbao_hy3_tokenhub", 10000, 10000, 0).estimated_cost_micros
    assert big > small > 0
    assert PRICING_VERSION == "2026-07-16"
    # 官方费率:输入 1 / 输出 4 元每百万 → 100 in +100 out = 100*1 + 100*4 = 500 micros
    assert small == 500


def test_hy3_cache_discount_applied():
    no_cache = estimate_usage_cost("yuanbao_hy3_tokenhub", 1000, 0, 0).estimated_cost_micros
    all_cache = estimate_usage_cost("yuanbao_hy3_tokenhub", 1000, 0, 1000).estimated_cost_micros
    assert no_cache == 1000       # 1000 * 1 micro
    assert all_cache == 250       # 1000 * 0.25 micro(缓存打折)
    assert all_cache < no_cache


def test_unknown_cost_is_null_not_zero():
    # token 计费但无 usage → None(不写假 0)
    u = estimate_usage_cost("yuanbao_hy3_tokenhub", None, None, None)
    assert u.estimated_cost_micros is None
    assert u.usage_is_estimated is True


def test_doubao_flat_is_exact():
    u = estimate_usage_cost("doubao_ark_api_search", None, None, None)
    assert u.estimated_cost_micros == 200_000  # ¥0.2 flat
    assert u.usage_is_estimated is False        # flat 精确


def test_budget_charge_never_zero_on_unknown():
    unknown = UsageEvidence(estimated_cost_micros=None, usage_is_estimated=True)
    micros, is_fallback = budget_charge_micros(unknown)
    assert micros > 0 and is_fallback is True   # 保守回落,绝不 0(防低估→超支)


def test_budget_charge_uses_known_cost():
    known = UsageEvidence(estimated_cost_micros=57_000, usage_is_estimated=True)
    micros, is_fallback = budget_charge_micros(known)
    assert micros == 57_000 and is_fallback is False


def test_micros_per_yuan_constant():
    assert MICROS_PER_YUAN == 1_000_000


def test_extra_field_rejected():
    with pytest.raises(Exception):
        ObservationEnvelopeV1(request_id="r", source_kind="monitoring", source_ref="s",
                              question_text="q", question_hash="h", query_kind="non_branded",
                              platform_key="p", provider_key="pr", model_key="m",
                              surface_key="doubao_ark_api_search", session_mode="clean",
                              prompt_text="p", answer_text="a", answer_hash="h",
                              response_status="answered",
                              observed_at=__import__("datetime").datetime.now(),
                              UNKNOWN_FIELD=1)


def test_question_hash_normalizes_but_preserves_original():
    padded = "  上海  律师  推荐 "
    h1 = canonical_question_hash(padded)
    h2 = canonical_question_hash("上海 律师 推荐")
    assert h1 == h2                       # 规范化后哈希一致
    assert normalize_question(padded) == "上海 律师 推荐"


def test_source_kind_maps_to_source_type():
    for sk, st in [("research", "research_round"), ("paid_diagnosis", "paid_diagnosis"),
                   ("monitoring", "recurring_monitoring")]:
        env = ObservationEnvelopeV1(
            request_id="r", source_kind=sk, source_ref="s", question_text="q",
            question_hash="h", query_kind="non_branded", platform_key="p", provider_key="pr",
            model_key="m", surface_key="doubao_ark_api_search", session_mode="clean",
            prompt_text="p", answer_text="a", answer_hash="h", response_status="answered",
            observed_at=__import__("datetime").datetime.now(),
        )
        assert env.source_type() == st
