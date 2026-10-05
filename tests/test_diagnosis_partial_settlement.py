"""[C组①·V10/§12.1]诊断降级交付 **部分结算** 判别(纯逻辑层,不动资金原语)。

锁定:
- 无判定 / sufficient → 走原全额 commit(**向后兼容,行为不变**);
- degraded + 可读冻结额 → 只收已履约部分(未履约不计费),且 1 ≤ 应收 ≤ 冻结额(守恒上界);
- 判定版本不符 / 比例越界 / 冻结额读不到 → **不多收也不少收**,一律报错交人工(禁猜);
- 判定可从 run 的**耐久快照**取(不只依赖调用方本次传入,防丢参导致误全额)。
"""
import pytest

import services.diagnosis_runs as dr
from services.diagnosis_sample_contract import SAMPLE_CONTRACT_VERSION


def _verdict(outcome="degraded", ratio=0.25, version=SAMPLE_CONTRACT_VERSION):
    return {"version": version, "outcome": outcome, "billable_ratio": ratio}


def _run(total=None, backend="legacy"):
    return {"freeze_id": 1, "owner_user_id": 7, "freeze_task_ref": "diag_x",
            "freeze_backend": backend, "final_snapshot_jsonb": None, "_total": total}


@pytest.fixture()
def frozen(monkeypatch):
    """把冻结额读取替换为可控值(不碰真库/资金原语)。"""
    box = {"total": 1000}
    monkeypatch.setattr(dr, "_frozen_total_for_run", lambda run: box["total"])
    return box


def test_no_verdict_keeps_full_commit_backward_compatible(frozen):
    actual, err = dr._partial_commit_points(_run(), {})
    assert actual is None and err is None          # 老流程零影响


def test_sufficient_outcome_keeps_full_commit(frozen):
    actual, err = dr._partial_commit_points(_run(), {"delivery_verdict": _verdict("sufficient", 1.0)})
    assert actual is None and err is None


def test_degraded_charges_only_the_delivered_share(frozen):
    actual, err = dr._partial_commit_points(_run(), {"delivery_verdict": _verdict(ratio=0.25)})
    assert err is None
    assert actual == 250                            # 1000 × 25%:未履约的 75% 不收


def test_degraded_never_exceeds_frozen_and_never_zero(frozen):
    frozen["total"] = 10
    actual, _ = dr._partial_commit_points(_run(), {"delivery_verdict": _verdict(ratio=0.01)})
    assert 1 <= actual <= 10                        # 守恒上界 + 确有交付不白送
    frozen["total"] = 1000
    actual2, _ = dr._partial_commit_points(_run(), {"delivery_verdict": _verdict(ratio=0.999)})
    assert actual2 <= 1000


def test_version_mismatch_goes_manual_not_guessed(frozen):
    actual, err = dr._partial_commit_points(
        _run(), {"delivery_verdict": _verdict(version="diagnosis-min-sample-v0")})
    assert actual is None and err == "delivery_verdict_version_mismatch"


@pytest.mark.parametrize("bad_ratio", [-0.1, 0.0, 1.0, 1.5, "abc", None])
def test_out_of_range_or_unparsable_ratio_goes_manual(frozen, bad_ratio):
    actual, err = dr._partial_commit_points(_run(), {"delivery_verdict": _verdict(ratio=bad_ratio)})
    assert actual is None and err is not None       # 绝不按坏比例动钱


def test_unreadable_frozen_amount_goes_manual(monkeypatch):
    monkeypatch.setattr(dr, "_frozen_total_for_run", lambda run: None)
    actual, err = dr._partial_commit_points(_run(), {"delivery_verdict": _verdict()})
    assert actual is None and err == "frozen_amount_unreadable"   # 读不到金额就不猜


def test_unexpected_outcome_goes_manual(frozen):
    actual, err = dr._partial_commit_points(_run(), {"delivery_verdict": _verdict("weird_state")})
    assert actual is None and err.startswith("unexpected_delivery_outcome")


def test_verdict_is_taken_from_durable_snapshot_when_call_arg_missing(frozen, monkeypatch):
    # 调用方没带 snapshot(丢参/重放)→ 从 run 的耐久快照取,避免误按全额收
    monkeypatch.setattr(dr, "_snapshot_dict", lambda v: {"delivery_verdict": _verdict(ratio=0.5)})
    run = _run()
    run["final_snapshot_jsonb"] = '{"delivery_verdict": {}}'
    actual, err = dr._partial_commit_points(run, None)
    assert err is None and actual == 500
