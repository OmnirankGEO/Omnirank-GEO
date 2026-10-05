"""
R0 freeze 结果五分支判定判别测试(WORKERS=4 · SPEC §3.0 · 纯函数无 DB)
====================================================================
付费成功 / 免单exempt / 确定失败cancelled / 结构异常anomaly / 未知unknown。
未知禁 cancelled(核心不变量)· 不耦合 fastapi(getattr status_code 判 400/402)。
"""
from services.diagnosis_runs import classify_freeze_result


class _HTTPErr(Exception):
    """模拟 fastapi.HTTPException:带 status_code。"""
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"http {status_code}")


def test_branch1_paid_success_legacy():
    c = classify_freeze_result({"freeze_id": 123, "freeze_table": "legacy", "amount": 650}, None)
    assert c.branch == "paid" and c.freeze_id == 123 and c.freeze_backend == "legacy" and c.billing_mode == "paid"


def test_branch1_paid_success_v35():
    c = classify_freeze_result({"freeze_id": 5, "freeze_table": "v35"}, None)
    assert c.branch == "paid" and c.freeze_id == 5 and c.freeze_backend == "v35"


def test_branch2_exempt_free():
    c = classify_freeze_result({"freeze_id": None, "amount": 0, "free": True}, None)
    assert c.branch == "exempt" and c.billing_mode == "exempt" and c.freeze_id is None


def test_branch2_exempt_admin():
    c = classify_freeze_result({"freeze_id": None, "free": True, "admin_exempt": True}, None)
    assert c.branch == "exempt" and c.billing_mode == "exempt"


def test_branch3_cancelled_402():
    c = classify_freeze_result(None, _HTTPErr(402))
    assert c.branch == "cancelled"


def test_branch3_cancelled_400():
    c = classify_freeze_result(None, _HTTPErr(400))
    assert c.branch == "cancelled"


def test_branch5_unknown_500_not_cancelled():
    # 500 非确定性 → 未知(禁 cancelled)
    c = classify_freeze_result(None, _HTTPErr(500))
    assert c.branch == "unknown"


def test_branch5_unknown_generic_exception_not_cancelled():
    # 无 status_code 的裸异常(超时/断连)→ 未知(禁 cancelled)
    c = classify_freeze_result(None, ValueError("connection reset"))
    assert c.branch == "unknown"


def test_branch4_anomaly_freeze_id_no_backend():
    # 有 freeze_id 但 freeze_table 缺 → 结构异常
    c = classify_freeze_result({"freeze_id": 99}, None)
    assert c.branch == "anomaly" and c.freeze_id == 99 and c.freeze_backend is None


def test_branch4_anomaly_bad_backend():
    c = classify_freeze_result({"freeze_id": 99, "freeze_table": "weird"}, None)
    assert c.branch == "anomaly" and c.freeze_id == 99


def test_branch5_unknown_no_freeze_no_free():
    # freeze_id 空且非 free(不该出现)→ 未知(保守 · 禁 cancelled)
    c = classify_freeze_result({"freeze_id": None}, None)
    assert c.branch == "unknown"


def test_cancelled_only_from_deterministic_never_from_none_result():
    # 关键不变量:cancelled 只来自 400/402 确定性失败,任何"结果不明"都不得 cancelled
    for res, exc in [(None, _HTTPErr(503)), (None, TimeoutError()), ({"freeze_id": 7}, None)]:
        assert classify_freeze_result(res, exc).branch != "cancelled"


# ============ 返工2 纯逻辑判别(状态集/映射/快照 · 无 DB)============
from services import diagnosis_runs as _dr


def test_delivery_repair_is_terminal_not_active():
    # [返工2 P0-2] delivery_repair_pending 是非成功终态:在 TERMINAL(reconciler 回补)· 不在 ACTIVE(不占品牌活跃锁)
    assert "delivery_repair_pending" in _dr.TERMINAL_STATUSES
    assert "delivery_repair_pending" not in _dr.ACTIVE_STATUSES


def test_success_terminal_set_only_committed_and_exempt():
    # [返工2 P0-1c] reconciler 判成功性:仅 committed / completed_exempt 是成功终态;退款/失败终态不在其中
    assert set(_dr._SUCCESS_TERMINAL_STATUSES) == {"committed", "completed_exempt"}
    for s in ("released", "cancelled", "cancelled_no_freeze", "failed_exempt", "delivery_repair_pending"):
        assert s not in _dr._SUCCESS_TERMINAL_STATUSES


def test_freeze_table_mapping():
    # [返工2 P0-3] 双表映射:legacy=point_freezes(user_id) / v35=customer_credit_freezes(customer_user_id)
    assert _dr._FREEZE_TABLE["legacy"] == ("point_freezes", "user_id")
    assert _dr._FREEZE_TABLE["v35"] == ("customer_credit_freezes", "customer_user_id")


def test_delivery_repair_snapshot_is_error_terminal():
    # [返工2 P0-2] delivery_repair 快照必须是异常终态(type=error · done · 非 complete)→ 前端不显"完成"
    snap = _dr._delivery_repair_snapshot()
    assert snap["type"] == "error" and snap["done"] is True and snap.get("terminal") is True


def test_cancel_snapshot_is_error_terminal():
    # [返工 P1-3] cancelled/cancelled_no_freeze 快照是异常终态(Redis 过期后不回退成功态)
    snap = _dr._cancel_snapshot()
    assert snap["type"] == "error" and snap["done"] is True


# ============ 返工3 纯逻辑判别(manual_resolving 态集 + 处置意图 · 无 DB)============
def test_manual_resolving_is_transient_not_terminal_not_active():
    # [返工3 P0] manual_resolving(双表 claim 已持久意图)是**非终态非活跃**中间态:
    #   不在 TERMINAL(reconciler 不当终态回补)· 不在 ACTIVE(不占品牌活跃锁)· 不在 SUCCESS(绝非成功)
    assert "manual_resolving" not in _dr.TERMINAL_STATUSES
    assert "manual_resolving" not in _dr.ACTIVE_STATUSES
    assert "manual_resolving" not in _dr._SUCCESS_TERMINAL_STATUSES


def test_manual_lease_window_positive():
    # [返工4 P0] 双表处置租约时长存在且 < 超时告警窗口(租约过期即 sweeper 接管续跑 · 长期卡才告警)
    assert _dr._MANUAL_LEASE_SECONDS > 0
    assert _dr._MANUAL_LEASE_SECONDS < _dr._MANUAL_STUCK_ALERT_SECONDS


def test_intent_json_stable_and_parse_roundtrip():
    # [返工3 P0] 意图 JSON 规范化(sort_keys 稳定)+ 解析回环
    j = _dr._intent_json("adminA", "legacy", 11, "commit", "v35", 22)
    d = _dr._parse_intent(j)
    assert d["keeper_backend"] == "legacy" and d["keeper_freeze_id"] == 11 and d["keeper_decision"] == "commit"
    assert d["other_backend"] == "v35" and d["other_freeze_id"] == 22
    # 同资金意图不同 operator/顺序 → JSON 稳定(键排序)· 资金键一致
    j2 = _dr._intent_json("adminA", "legacy", 11, "commit", "v35", 22)
    assert j == j2


def test_same_intent_only_when_money_keys_match():
    # [返工3 P0] _same_intent:仅比资金键(operator 变化不算不同意图 → 幂等 resume);资金键任一不同 → False
    base = _dr._intent_json("adminA", "legacy", 11, "commit", "v35", 22)
    same_diff_operator = _dr._intent_json("adminB", "legacy", 11, "commit", "v35", 22)
    diff_keeper_decision = _dr._intent_json("adminA", "legacy", 11, "release", "v35", 22)
    diff_other_fid = _dr._intent_json("adminA", "legacy", 11, "commit", "v35", 999)
    assert _dr._same_intent(base, same_diff_operator) is True     # operator 不同仍同意图(幂等续跑)
    assert _dr._same_intent(base, diff_keeper_decision) is False  # keeper_decision 不同 → 拒并发相反处置
    assert _dr._same_intent(base, diff_other_fid) is False        # other 冻结不同 → 拒
    assert _dr._same_intent(None, base) is False                  # 无持久意图 → 非一致
    assert _dr._same_intent("not-json", base) is False            # 坏 JSON → 非一致(fail-safe)
