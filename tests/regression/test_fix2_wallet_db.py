"""Regression locks for Fix-2 batch · target file: db/wallet_db.py + settlement_orchestrator.py

Finding in this batch:
  - GEO-R6-CAN-005 (P2, concurrent-binding-race) -> [v5 FIXED, skip 已删]
    修点:services/settlement_orchestrator.py::SettlementOrchestrator.route() 归属守卫 —
    order.agent_user_id 与【最终绑定服务商】binding.agent_user_id 不一致时,返 'dispute_hold' 拒绝直接结算
    (不划库存 / 不写 agent_revenue_ledger / 不划客户额度);支付回调不再创建商业关系，
    报价订单事务使用 canonical resolver 锁定 BOUND / PLATFORM_DIRECT。
    真 DB 行为 + X/Y 并发 + 三账守恒判别测试见 tests/regression/test_nogo_v5_r6can005_settlement_dispute.py。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


WALLET_DB = Path(__file__).resolve().parents[2] / "db" / "wallet_db.py"
ORCH = Path(__file__).resolve().parents[2] / "services" / "settlement_orchestrator.py"


def _src(p) -> str:
    return p.read_text(encoding="utf-8")


def test_wallet_db_source_present():
    """Sanity: callback delegates to settlement without creating relationships."""
    src = _src(WALLET_DB)
    assert "upsert_customer_agent_binding" not in src
    assert "SettlementOrchestrator" in src


def test_geo_r6_can_005_dispute_hold_guard_present():
    """[v5 FIXED] 归属守卫在结算 SSOT 中存在 · fix marker 未回退。"""
    src = _src(ORCH)
    assert "_attribution_conflict" in src, "缺归属冲突守卫 _attribution_conflict(R6-CAN-005 回退)"
    assert "_hold_for_dispute" in src, "缺 dispute_hold 分支(R6-CAN-005 回退)"
    assert "dispute_hold" in src, "缺 dispute_hold settlement_mode(R6-CAN-005 回退)"
    assert "GEO-R6-CAN-005" in src


def test_geo_r6_can_005_complete_recharge_never_auto_binds():
    """支付回调不得恢复旧的自动换绑旁路；争议由结算 SSOT 判定。"""
    src = _src(WALLET_DB)
    assert "_bind_res = upsert_customer_agent_binding" not in src
    assert "upsert_customer_agent_binding" not in src
    orchestrator = _src(ORCH)
    assert "_attribution_conflict" in orchestrator
    assert "_hold_for_dispute" in orchestrator
