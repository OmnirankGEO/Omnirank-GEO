"""WP3 判据 · registry / SampleSummary 重建 / 五卡 / 受众裁剪 / 文案 SSOT。

(MET-18/22/26/29/30/35/36/40/41/44、UI-23/24、POR-20 的可在无前端条件下闭合的部分)
"""

from __future__ import annotations

import re

import pytest

from services.defensive_geo.presentation import copy_registry as C
from services.defensive_geo.presentation import projection as P
from services.defensive_geo.presentation import registries as R


def cells(**counts: int) -> list[dict]:
    out: list[dict] = []
    for status, n in counts.items():
        out.extend({"status": status} for _ in range(n))
    return out


# ═══════════════════════════════ MET-36 / MET-45:签发闸

def test_all_three_customer_policies_are_signed():
    """[包F ⑧ · 2026-08-24] 三项**已签**,这条判据随之换向。

    一期原文是「事实陈述:三项都还没签。签了要改这条,顺便逼人回来读闸」——
    这次就是那个"回来读闸"的时刻。换向而不是删掉:删掉之后
    谁把签发状态拨回未签、或者新增一项忘了签,都不会有东西变红。
    """
    assert R.unsigned_policies() == (), (
        f"还有未签的:{R.unsigned_policies()}")
    # 三项都在、都签了、都有留痕(ACT-12 要求 H0 门能解析签发元数据)
    for pid in (R.LEVEL_POLICY_VERSION, R.STATE_RULE_VERSION,
                R.SECTION_REGISTRY_VERSION):
        sig = R.signature(pid)
        assert sig.signed and sig.signed_by and sig.signed_at, (pid, sig)


@pytest.mark.parametrize("audience", sorted(R.CUSTOMER_FACING_AUDIENCES))
def test_customer_facing_terminal_projection_is_open_now_that_it_is_signed(audience):
    """🔴 MET-36 的**成对**判据:签发后放行 + 拨回未签立刻重新拦住。

    只判"放行"的话,把整个签发闸删掉同样全绿 —— 而那时未签状态也会
    下发终态呈现,MET-36 就废了。所以同一条判据里把闸拆一次。
    """
    # 正样本:签发后对客裁剪真的产出内容
    assert P.crop_for_audience({"a": 1}, audience)["a"] == 1

    # 反向对照:任一项拨回未签 ⇒ 必须重新抛
    original = dict(R._SIGNATURES)
    try:
        pid = R.LEVEL_POLICY_VERSION
        R._SIGNATURES[pid] = original[pid]._replace(signed=False)
        with pytest.raises(R.PolicyNotSigned):
            P.crop_for_audience({"a": 1}, audience)
    finally:
        R._SIGNATURES.clear()
        R._SIGNATURES.update(original)


@pytest.mark.parametrize("audience", ["service_provider", "service_provider_demo"])
def test_fixture_and_shadow_paths_are_not_blocked(audience):
    """反向对照:闸只挡对客面。全挡住的话它就没有区分力了。"""
    assert P.crop_for_audience({"a": 1}, audience)["a"] == 1


def test_unknown_audience_is_rejected():
    with pytest.raises(ValueError):
        P.crop_for_audience({}, "some_new_audience")


# ═══════════════════════════════ MET-18 / MET-35:SampleSummary 逐值重建

def test_summary_is_rebuilt_from_ledger_not_taken_from_input():
    s = P.build_sample_summary(
        plan_cells=[{}] * 9,
        evidence_cells=cells(answered=6, entity_ambiguous=1,
                             engine_error=1, policy_skipped=1),
        attempt_ledger=[{"error": False}] * 7 + [{"error": True}],
    )
    assert s.planned == 9
    assert s.valid == 6
    assert s.identityAmbiguous == 1
    assert s.response == s.valid + s.identityAmbiguous == 7
    assert s.attempted == s.response + s.engineErrors == 8
    assert s.terminal == s.response + s.engineErrors + s.policySkipped == 9
    P.assert_terminal_conservation(s)


def test_misidentified_counts_as_valid_not_as_error():
    """🔴 MET-26:认错了**不得**降成普通 nonmention / engine_error。

    §15.6 L3479 逐字:valid 含 confirmed/nonmention/misidentified 三个判别分支。
    misidentified 是 entityState,cell status 仍是 answered。
    """
    s = P.build_sample_summary(
        plan_cells=[{}] * 2,
        evidence_cells=[
            {"status": "answered", "entityState": "confirmed"},
            {"status": "answered", "entityState": "misidentified"},
        ],
        attempt_ledger=[{"error": False}] * 2,
    )
    assert s.valid == 2
    assert s.engineErrors == 0


def test_entity_state_and_target_outcome_are_two_different_axes():
    """两轴不得合并 —— 合并会让 MET-26/30 静默变绿。"""
    assert "misidentified" in C.ENTITY_STATE_LABELS
    assert "misidentified" not in C.TARGET_OUTCOME_LABELS
    assert "entity_ambiguous" in C.TARGET_OUTCOME_LABELS
    assert "entity_ambiguous" not in C.ENTITY_STATE_LABELS


def test_forged_summary_cannot_pass_conservation():
    """MET-18「伪造任一汇总字段转红」—— 投影失败,不下发半真半假的数字。"""
    with pytest.raises(P.SummaryInconsistent):
        P.build_sample_summary(
            plan_cells=[{}] * 2,
            evidence_cells=cells(engine_error=2),
            attempt_ledger=[],                    # engineErrors=2 > attemptRecords=0
        )


def test_more_evidence_than_plan_is_rejected():
    with pytest.raises(P.SummaryInconsistent):
        P.build_sample_summary(
            plan_cells=[{}],
            evidence_cells=cells(answered=3),
            attempt_ledger=[{"error": False}] * 3,
        )


def test_unknown_cell_status_is_rejected():
    with pytest.raises(P.SummaryInconsistent):
        P.build_sample_summary(
            plan_cells=[{}], evidence_cells=[{"status": "made_up"}],
            attempt_ledger=[{"error": False}],
        )


def test_running_report_may_have_terminal_below_planned():
    """反向对照:运行中 terminal<planned 合法,只有终态才要求相等。

    合并这两条会把"还在跑"误判成"数据坏了"。
    """
    s = P.build_sample_summary(
        plan_cells=[{}] * 5, evidence_cells=cells(answered=2),
        attempt_ledger=[{"error": False}] * 2,
    )
    assert s.terminal == 2 < s.planned
    with pytest.raises(P.SummaryInconsistent):
        P.assert_terminal_conservation(s)


# ═══════════════════════════════ MET-29 / MET-41:五卡

def test_five_cards_exactly_once_in_signed_order():
    s = P.build_sample_summary(
        plan_cells=[{}] * 2, evidence_cells=cells(answered=2),
        attempt_ledger=[{"error": False}] * 2,
    )
    views = P.project_cards(
        summary_by_card={k: s for k in R.CARD_KEYS},
        level_by_card={k: "guarded" for k in R.CARD_KEYS},
    )
    assert [v.key for v in views] == [
        "identity", "recommendation", "scenario", "competition", "evidence"
    ]
    assert len(views) == len({v.key for v in views}) == 5


def test_missing_card_is_rejected_not_silently_dropped():
    s = P.build_sample_summary(
        plan_cells=[{}], evidence_cells=cells(answered=1),
        attempt_ledger=[{"error": False}],
    )
    partial = {k: s for k in R.CARD_KEYS if k != "evidence"}
    with pytest.raises(ValueError):
        P.project_cards(summary_by_card=partial,
                        level_by_card={k: "guarded" for k in R.CARD_KEYS})


def test_zero_valid_forces_no_conclusion_and_unknown_level():
    """§9.7「所有平台失败/有效回答为 0 ⇒ 不生成百分比、等级」。

    没有有效样本却给等级 = 编数字。这条钉死那一格。
    """
    s = P.build_sample_summary(
        plan_cells=[{}] * 3, evidence_cells=cells(engine_error=3),
        attempt_ledger=[{"error": True}] * 3,
    )
    views = P.project_cards(
        summary_by_card={k: s for k in R.CARD_KEYS},
        # 就算调用方硬塞一个好等级,也必须被压回 unknown
        level_by_card={k: "guarded" for k in R.CARD_KEYS},
    )
    for v in views:
        assert v.state == "no_conclusion"
        assert v.level_key == "unknown"
        assert v.level_label == "暂无结论"


def test_every_section_state_has_an_executable_next_step():
    """§0.5.6:任何状态都不许是死路(含 Z-2.3 的 no-baseline 分支)。"""
    for state in ("ready", "partial", "no_conclusion"):
        assert R.state_actions(state)


# ═══════════════════════════════ MET-22 / UI-23:受众裁剪

def _sign_all(monkeypatch):
    for pid in list(R._SIGNATURES):
        monkeypatch.setitem(
            R._SIGNATURES, pid,
            R.SignatureState(pid, True, "owner", "2026-08-21", "test-signed"),
        )


def test_customer_payload_drops_internal_keys_at_any_depth(monkeypatch):
    _sign_all(monkeypatch)
    payload = {
        "score": 7,
        "cards": [{"key": "identity", "planTrace": "T1",
                   "nested": {"providerName": "kimi", "label": "已守住"}}],
        "scoringModel": "qwen3-max",
    }
    out = P.crop_for_audience(payload, "customer")
    flat = repr(out)
    for forbidden in ("planTrace", "providerName", "scoringModel", "kimi", "qwen3-max"):
        assert forbidden not in flat, forbidden
    assert out["cards"][0]["nested"]["label"] == "已守住"      # 业务内容留着


def test_provider_keeps_trace_reverse_control(monkeypatch):
    """反向对照:服务商面必须**保留** trace。全剥的话上一条就没有区分力。"""
    _sign_all(monkeypatch)
    out = P.crop_for_audience({"planTrace": "T1"}, "service_provider")
    assert out["planTrace"] == "T1"


def test_provider_demo_is_not_degraded_but_has_zero_side_effects(monkeypatch):
    _sign_all(monkeypatch)
    out = P.crop_for_audience({"planTrace": "T1"}, "service_provider_demo")
    assert out["planTrace"] == "T1"               # 「demo 不缩水」
    assert out["sideEffectsDisabled"] is True


# ═══════════════════════════════ U-1 / MET-40:文案 SSOT

def test_untranslated_enum_raises_instead_of_echoing():
    with pytest.raises(C.UntranslatedEnum):
        C.translate("target_outcome", "some_new_outcome")


def test_every_translated_label_is_human_not_an_internal_enum():
    """全量遍历所有译文表:没有一条译文本身长得像内部枚举。"""
    for table_name, table in C.ALL_TABLES.items():
        for key, label in table.items():
            assert not C.looks_like_internal_enum(label), (table_name, key, label)


def test_retired_wording_never_appears_in_any_live_label():
    """U-1 废弃词表:废了就是废了,不许在任何现役译文里复活。"""
    live = [v for table in C.ALL_TABLES.values() for v in table.values()]
    for retired in C.RETIRED_LABELS:
        for label in live:
            assert retired not in label, (retired, label)


def test_internal_enum_detector_actually_detects():
    """上面两条的**尺子自证** —— 尺子坏了,'全都不像内部枚举'会假绿。"""
    assert C.looks_like_internal_enum("needs_strengthening") is True
    assert C.looks_like_internal_enum("commercial_basis_established") is True
    assert C.looks_like_internal_enum("已守住") is False
    assert C.looks_like_internal_enum("已收款,系统准备开工中") is False


def test_leak_scanner_finds_internal_enum_in_values_only():
    leaks = P.find_internal_leaks(
        {"levelKey": "needs_strengthening", "levelLabel": "待加强"}
    )
    assert len(leaks) == 1
    assert "needs_strengthening" in leaks[0]


def test_four_state_words_are_the_signed_set():
    assert [C.LEVEL_LABELS[k] for k in
            ("guarded", "needs_strengthening", "priority_fix", "unknown")] == [
        "已守住", "待加强", "优先修复", "暂无结论"]


def test_signed_sentences_are_verbatim():
    assert C.SENTENCES["engine_error_notice"] == (
        "豆包这次没答上来,不计入本次统计;记录已保留,其他平台的结果不受影响")
    assert C.SENTENCES["preview_expired"] == (
        "刚才那一步已过期,请重新发起;没有扣除任何算力")
    assert C.SENTENCES["not_comparable"] == (
        "这次和上次测的问题、平台不一样,结果对照看,不算涨跌")


def test_offensive_side_uses_the_signed_name_not_the_retired_alias():
    """U-1 废除「主动获客」「主动推荐」,进攻侧主名 = 主动抢推荐。"""
    assert C.MODE_LABELS["offensive"] == "主动抢推荐"
    assert C.MODE_SHORT_LABELS["offensive"] == "抢推荐"
    assert "主动获客" not in C.MODE_EXPLAINERS["hybrid"]


def test_registry_census_is_mechanical():
    """POR-20:分母机械导出。加一档忘了改判据,这里会先炸。"""
    assert C.registry_census()["level"] == len(R.level_keys()) == 4
    assert R.registry_census()["cards"] == 5
