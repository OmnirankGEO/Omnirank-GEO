"""决策台账与 H0 闸矩阵(规格 §2/§15.1 · §19.1 #20)。

判据的核心是一句话:**candidate 不能被测试当成 signed**。
它对应一次真实 P0(invrel 2026-08-13):长工单里「未拍板前默认 X」被读成公理,
变成了运行时硬阻断。
"""

from __future__ import annotations

import pytest

from services import xiaobang_decision_ledger as ledger


def test_fresh_worktree_can_read_the_ledger():
    manifest = ledger.decision_manifest()
    assert manifest["ledger_version"] == ledger.LEDGER_VERSION
    assert manifest["decisions"] and manifest["h0_gates"]


def test_every_owner_conversation_input_is_still_candidate():
    """§2 的 XO-01..XO-11 全部 candidate,一条都不许被悄悄提成 signed。"""
    xo = [d for d in ledger.DECISIONS if d.decision_id.startswith("XO-")]
    assert len(xo) == 11, [d.decision_id for d in xo]
    for record in xo:
        assert record.status == ledger.STATUS_CANDIDATE, record.decision_id
        assert record.signed_source is None, record.decision_id
        assert not ledger.is_signed(record.decision_id)


def test_the_three_conflicting_inputs_carry_an_explicit_conflict_note():
    """XO-06/07/08 与已签发口径冲突,必须写明调和解,不能装作没冲突。

    判据取「调和轴 + 指回某条 signed 决策」,不钉死某个具体 rule_id ——
    XO-08 的调和解落在 DP-A5(不许伪造成功)而不是 DP-A6.1,钉死 A6.1
    会把一条**写对了的**记录判红。
    """
    signed_ids = {d.decision_id for d in ledger.DECISIONS
                  if d.status == ledger.STATUS_SIGNED}
    for decision_id in ("XO-06", "XO-07", "XO-08"):
        record = ledger.resolve_decision(decision_id)
        assert record is not None
        assert "compute_only" in record.conflict_note, decision_id
        assert any(sid in record.conflict_note for sid in signed_ids), decision_id


def test_every_h0_gate_resolves_to_signed_rule_and_source_hash():
    """每个硬门的 rule_id / version / source / hash 都要能解析(§19.1 #20)。"""
    verified = {row["decision_id"]: row for row in ledger.verify_signed_sources()}
    assert verified, "签发件核验结果为空 —— 判据本身失效了"
    for gate in ledger.H0_GATES:
        assert gate.decision_ids, gate.gate_id
        for decision_id in gate.decision_ids:
            record = ledger.resolve_decision(decision_id)
            assert record is not None, (gate.gate_id, decision_id)
            assert record.status == ledger.STATUS_SIGNED, (gate.gate_id, decision_id)
            assert record.rule_version
            assert record.signed_source is not None
            assert verified[decision_id]["state"] == "match", verified[decision_id]
        assert gate.user_next_action, gate.gate_id


def test_assert_gate_is_signed_rejects_a_gate_backed_by_a_candidate():
    """反向变异:造一个挂在 candidate 上的闸,必须抛。

    🔴 这条是全模块最重要的一条。它证明的不是"我写对了",而是"写错了会响"。
    """
    forged = ledger.H0Gate(
        "XB-H0-FORGED", "money", ("XO-06",), "打开核对并确认",
    )
    ledger._GATE_BY_ID["XB-H0-FORGED"] = forged
    try:
        with pytest.raises(ledger.UnsignedRuleError) as excinfo:
            ledger.assert_gate_is_signed("XB-H0-FORGED")
        assert "XO-06" in str(excinfo.value)
    finally:
        ledger._GATE_BY_ID.pop("XB-H0-FORGED", None)


def test_unregistered_gate_id_is_rejected():
    with pytest.raises(ledger.UnsignedRuleError):
        ledger.assert_gate_is_signed("XB-H0-DOES-NOT-EXIST")


def test_real_gates_pass_the_same_assertion(subtests=None):
    """正向对照:真闸必须全过 —— 否则上一条的"抛"只证明它恒抛。"""
    for gate in ledger.H0_GATES:
        assert ledger.assert_gate_is_signed(gate.gate_id).gate_id == gate.gate_id


def test_two_tier_confirmation_gates_are_split_by_side_effect():
    """两档确认制各有自己的闸,不共用一条 —— 共用就没法只对一档变异。"""
    external = ledger.resolve_gate("XB-H0-EXTERNAL-CONFIRM")
    silent = ledger.resolve_gate("XB-H0-SILENT-NOTICE")
    assert external.applies_to_side_effect == ("external",)
    assert silent.applies_to_side_effect == ("compute_only",)
    # 静默扣档必须挂在 A6.1 上,而不是挂在任何一条 XO candidate 上。
    assert "DP-A6.1" in silent.decision_ids
    assert not any(d.startswith("XO-") for d in silent.decision_ids)


def test_signed_record_cannot_be_constructed_without_a_source():
    with pytest.raises(ValueError):
        ledger.DecisionRecord(
            "FAKE", "无出处的签发", "凭空", ledger.STATUS_SIGNED, "v1",
        )


def test_candidate_record_cannot_carry_a_signed_source():
    with pytest.raises(ValueError):
        ledger.DecisionRecord(
            "FAKE2", "候选却挂签发件", "凭空", ledger.STATUS_CANDIDATE, "v1",
            signed_source=ledger.SignedSource("x.md", "0" * 64, "§1"),
        )


def test_source_verification_distinguishes_unreachable_from_mismatch():
    """三态不能压成两态:「读不到」和「不匹配」是两件事。"""
    missing = ledger.SignedSource("docs/does-not-exist-xbvnext.md", "0" * 64, "§1")
    assert missing.verify()["state"] == "unreachable"
    wrong = ledger.SignedSource(
        "docs/SYSTEM_TRUTH/08_billing.md", "0" * 64, "全文",
    )
    assert wrong.verify()["state"] == "mismatch"
