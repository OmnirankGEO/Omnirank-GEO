"""[BUG-P3] V3.5 结算反扣 user_wallets 用 GREATEST(0,..) 静默截断而流水记全额 · 静态守护

根因:_record_factory_settlement 反扣 user_wallets 用 GREATEST(0, paid-revoke),pricing_snapshot
与实际入账不一致时(SKU 模板改 / fallback 取 order 字段)反扣量 > 刚入账量 → GREATEST 截断吞掉不足
部分(不报错),但 insert_transaction 仍记全额 -revoke + balance_after 记截断后真实余额 →
amount 累加与 balance_after 落差对不上、无法对账;且客户拿全额额度而 user_wallets 只扣部分=跨账本多点。
修:反扣前 SELECT 余额校验,越界 → logger.error(RECON 需人工核对)+ revoke 截到实际可扣量,流水记真实扣减。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_revoke_clamped_to_balance():
    src = (ROOT / "services" / "settlement_orchestrator.py").read_text(encoding="utf-8")
    assert "[Orchestrator.v35][RECON]" in src, "反扣越界须告警(需人工对账)"
    assert "_clamp_paid = min(_want_paid, _avail_paid)" in src, (
        "越界须把待迁移付费额度截到实际可扣量"
    )
    assert "revoke_paid = _clamp_paid" in src, (
        "流水反扣额必须复用同一钳制结果，保持 amount 与真实扣减一致"
    )
    assert "_clamp_bonus = min(bonus_points, _avail_bonus)" in src
    assert "revoke_bonus = _clamp_bonus" in src
