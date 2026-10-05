"""MET-38 判据 · 三种 branded commitment + metric registry + 互斥 exclusion。

判据的核心形态是**否定性**的:「改任一参与字段而 hash 不变」「不同 payload 同 hash」
「B 复用 A 的 hash」「三类互换」都必须被拒。
所以每条否定判据都配一条正样本(同输入必须同 hash),否则"全都不相等"
可能只是因为 hash 函数每次都在乱返回 —— 那种"红"没有区分力。
"""

from __future__ import annotations

import json
import random

import pytest

from services.defensive_geo.presentation import commitments as CM
from services.defensive_geo.presentation import metric_definitions as MD

KEY = b"test-commitment-key-not-a-real-secret"
CTX = CM.CommitmentContext(
    issuer="omnirank", tenant="t1", brand="b1",
    audience="customer", projection_policy_version="defgeo-presentation-v2",
)


def scope(**over):
    kw = dict(surface_keys=["web", "app"], search_mode_keys=["ai_search"],
              run_indexes=[1, 2], public_scope_labels=["近 30 天"])
    kw.update(over)
    return CM.scope_commitment(ctx=over.pop("ctx", CTX), key=KEY, **kw)


def slice_of(key="brand_mention_rate", **over):
    d = MD.definition(key)
    kw = dict(metric_definition_key=key, registry_row=d.as_row(),
              denominator_registry_version=MD.DENOMINATOR_REGISTRY_VERSION,
              definition_version=d.definition_version,
              calculation_version=d.calculation_version)
    kw.update(over)
    return CM.slice_commitment(ctx=CTX, key=KEY, **kw)


def cohort(**over):
    kw = dict(baseline_scope=scope(), current_scope=scope(),
              baseline_slice=slice_of(), current_slice=slice_of(),
              mode_side="defensive", brand_exposure="named",
              family_key="trust_reliability", platform_key="doubao",
              matched_cell_ids=["c1", "c2"], matched_cell_count=2)
    kw.update(over)
    return CM.cohort_commitment(ctx=CTX, key=KEY, **kw)


# ═══════════════════════════ 正样本:确定性(否则否定判据没有区分力)

def test_same_input_yields_same_commitment():
    assert scope() == scope()
    assert slice_of() == slice_of()
    assert cohort() == cohort()


def test_commitment_is_hex_sha256():
    for value in (scope(), slice_of(), cohort()):
        assert len(value) == 64 and all(c in "0123456789abcdef" for c in value)


# ═══════════════════════════ 三类互换必须拒(domain separation)

def test_the_three_kinds_cannot_impersonate_each_other():
    """同一份 payload 换 kind 必须得到不同 hash —— 靠 domain separation。"""
    payload = {"same": "payload"}
    values = {
        kind: CM._compute(kind, CTX, payload, key=KEY)
        for kind in CM.COMMITMENT_KINDS
    }
    assert len(set(values.values())) == len(CM.COMMITMENT_KINDS), values


def test_policy_versions_are_distinct_names():
    assert len(set(CM.POLICY_VERSION.values())) == 3


# ═══════════════════════════ 改任一参与字段 ⇒ hash 必变

@pytest.mark.parametrize("field,value", [
    ("issuer", "other"), ("tenant", "t2"), ("brand", "b2"),
    ("audience", "service_provider"), ("projection_policy_version", "v9"),
])
def test_changing_any_context_field_changes_the_commitment(field, value):
    base = scope()
    other_ctx = CTX._replace(**{field: value})
    changed = CM.scope_commitment(
        ctx=other_ctx, key=KEY, surface_keys=["web", "app"],
        search_mode_keys=["ai_search"], run_indexes=[1, 2],
        public_scope_labels=["近 30 天"])
    assert changed != base, field


def test_changing_scope_payload_changes_the_commitment():
    assert scope(surface_keys=["web"]) != scope()
    assert scope(run_indexes=[1, 2, 3]) != scope()
    assert scope(public_scope_labels=["近 7 天"]) != scope()


def test_changing_one_registry_row_leaf_changes_the_slice_commitment():
    """MET-38「改 registry row……而 hash 不变」必须不可能。"""
    d = MD.definition("brand_mention_rate")
    tampered = dict(d.as_row())
    tampered["rounding"] = "floor"
    assert slice_of(registry_row=tampered) != slice_of()


def test_cross_brand_commitments_never_collide():
    """跨 brand 不得相等或复用(§15.5 L2062)。"""
    other = CM.scope_commitment(
        ctx=CTX._replace(brand="b2"), key=KEY, surface_keys=["web", "app"],
        search_mode_keys=["ai_search"], run_indexes=[1, 2],
        public_scope_labels=["近 30 天"])
    assert other != scope()


def test_different_key_yields_different_commitment():
    other = CM.scope_commitment(
        ctx=CTX, key=b"another-key", surface_keys=["web", "app"],
        search_mode_keys=["ai_search"], run_indexes=[1, 2],
        public_scope_labels=["近 30 天"])
    assert other != scope()


# ═══════════════════════════ metric B 不得复用 metric A 的 cohort hash

def test_metric_b_cannot_reuse_metric_a_cohort_hash():
    """🔴 §15.5 L2062 逐字。靠 slice commitment 进 cohort payload 自然保证。"""
    a = cohort(baseline_slice=slice_of("brand_mention_rate"),
               current_slice=slice_of("brand_mention_rate"))
    b = cohort(baseline_slice=slice_of("scenario_coverage"),
               current_slice=slice_of("scenario_coverage"))
    assert a != b


def test_slice_commitments_differ_per_metric_key():
    values = {k: slice_of(k) for k in MD.METRIC_KEYS}
    assert len(set(values.values())) == len(MD.METRIC_KEYS)


# ═══════════════════════════ framing:不同 payload 不得同 hash

def test_framing_prevents_boundary_shifting_collisions():
    """裸拼接下 ('ab','c') 与 ('a','bc') 同串;length-prefix framing 必须区分。"""
    assert CM._frame(b"ab", b"c") != CM._frame(b"a", b"bc")


def test_context_boundary_cannot_be_shifted():
    """把 tenant 的尾字符挪进 brand,不得得到同一 commitment。"""
    a = CM.scope_commitment(ctx=CTX._replace(tenant="ab", brand="c"), key=KEY,
                            surface_keys=[], search_mode_keys=[],
                            run_indexes=[], public_scope_labels=[])
    b = CM.scope_commitment(ctx=CTX._replace(tenant="a", brand="bc"), key=KEY,
                            surface_keys=[], search_mode_keys=[],
                            run_indexes=[], public_scope_labels=[])
    assert a != b


# ═══════════════════════════ JCS 正确性

def test_jcs_sorts_object_keys_by_utf16_not_code_point():
    """🔴 这条证明本实现**不是** json.dumps(sort_keys=True) 的换皮。

    U+FFFD 单个 UTF-16 单元 0xFFFD(65533);U+10000 是代理对,首单元 0xD800(55296)。
    code point 序:U+FFFD 在前;UTF-16 序:U+10000 在前。两者**相反**。
    """
    payload = {"�": 1, "\U00010000": 2}
    mine = CM._jcs_dumps(payload).decode("utf-8")
    naive = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"))
    assert mine.index("\U00010000") < mine.index("�")      # UTF-16 序
    assert naive.index("\U00010000") > naive.index("�")    # code point 序
    assert mine != naive


def test_jcs_applies_nfc_so_equivalent_strings_agree():
    """NFC:组合字符与预组合字符必须归一到同一 commitment。"""
    composed = CM._jcs_dumps({"k": "é"})          # U+00E9
    decomposed = CM._jcs_dumps({"k": "é"})  # e + U+0301
    assert composed == decomposed


def test_jcs_preserves_array_order():
    """数组**不排序** —— 排序会把"顺序变了"悄悄抹平。"""
    assert CM._jcs_dumps({"a": [2, 1]}) != CM._jcs_dumps({"a": [1, 2]})


def test_floats_are_rejected_not_silently_formatted():
    """MET-38「负/小数 count……拒绝」。浮点还会带来跨语言编码歧义。"""
    with pytest.raises(CM.CommitmentError):
        CM._jcs_dumps({"n": 1.5})


def test_bool_is_not_coerced_to_int():
    assert CM._jcs_dumps({"b": True}) != CM._jcs_dumps({"b": 1})


# ═══════════════════════════ cohort 分子分母可复算

def test_cohort_rejects_count_identity_mismatch():
    with pytest.raises(CM.CommitmentError):
        cohort(matched_cell_ids=["c1"], matched_cell_count=2)


def test_cohort_rejects_duplicate_cell_ids():
    with pytest.raises(CM.CommitmentError):
        cohort(matched_cell_ids=["c1", "c1"], matched_cell_count=2)


def test_cohort_rejects_negative_count():
    with pytest.raises(CM.CommitmentError):
        cohort(matched_cell_ids=[], matched_cell_count=-1)


# ═══════════════════════════ resolver 侧重验(commitment 不含授权)

def test_same_authority_guard_rejects_cross_brand():
    with pytest.raises(CM.CommitmentError):
        CM.assert_same_authority(CTX, CTX._replace(brand="b2"))


def test_same_authority_guard_passes_for_identical_context():
    CM.assert_same_authority(CTX, CTX)          # 反向对照:不得恒抛


# ═══════════════════════════ metric registry(MET-38 前半)

def test_every_registry_row_has_all_required_fields_nonempty():
    for key, d in MD.registry_rows().items():
        for field in MD.REQUIRED_DEFINITION_FIELDS:
            value = getattr(d, field)
            assert isinstance(value, str) and value, (key, field)


def test_unknown_metric_key_is_rejected():
    with pytest.raises(ValueError):
        MD.definition("no_such_metric")


def test_exclusion_precedence_is_a_total_order():
    """互斥的前提:precedence 互不相同。相同就无法确定 primary。"""
    precedences = [r.precedence for r in MD._EXCLUSIONS]
    assert len(set(precedences)) == len(precedences)


def test_candidate_falls_into_exactly_one_bucket_and_conserves():
    cands = (
        [{"id": f"ok{i}", "exclusion_codes": []} for i in range(6)]
        + [{"id": "e1", "exclusion_codes": ["engine_error"]}]
        + [{"id": "e2", "exclusion_codes": ["policy_skipped"]}]
    )
    r = MD.classify_candidates(cands)
    assert r.denominator == 6
    assert r.exclusions == {"engine_error": 1, "policy_skipped": 1}
    assert r.exclusion_candidate_count == 8
    assert r.conserves()


def test_multi_hit_candidate_is_counted_once_under_highest_precedence():
    """🔴 不双算:同时命中两条规则只记 precedence 最高那条。"""
    r = MD.classify_candidates([
        {"id": "x", "exclusion_codes": ["policy_skipped", "engine_error"]},
    ])
    assert r.exclusions == {"engine_error": 1}       # precedence 10 < 30
    assert sum(r.exclusions.values()) == 1
    assert r.conserves()


def test_classification_is_order_independent():
    """MET-38「输入换序漂移拒绝」。"""
    cands = [
        {"id": "a", "exclusion_codes": []},
        {"id": "b", "exclusion_codes": ["engine_error"]},
        {"id": "c", "exclusion_codes": ["not_attempted", "entity_ambiguous"]},
        {"id": "d", "exclusion_codes": []},
    ]
    first = MD.classify_candidates(cands)
    for seed in range(5):
        shuffled = list(cands)
        random.Random(seed).shuffle(shuffled)
        assert MD.classify_candidates(shuffled) == first, seed


def test_duplicate_candidate_id_is_rejected():
    with pytest.raises(MD.ClassificationError):
        MD.classify_candidates([{"id": "x", "exclusion_codes": []}] * 2)


def test_unknown_exclusion_code_is_rejected():
    with pytest.raises(MD.ClassificationError):
        MD.classify_candidates([{"id": "x", "exclusion_codes": ["made_up"]}])
