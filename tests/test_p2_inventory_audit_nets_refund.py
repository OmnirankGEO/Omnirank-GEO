"""[BUG-P2] inventory_audit 对账等式无视 type='refund' 行 → 每笔退费制造永久幻影漂移 · 静态守护

根因:freeze_customer_credit 用 consume_credit 真扣(type='consume')→ platform_consumed 永久计入;
工具失败退费/长任务 release 用 refund_credit 退回客户额度(type='refund'),但 run_audit 的
platform_consumed 只数 consume、refunded_or_revoked 只数 revoke,type='refund' 在等式两侧都不出现 →
每发生一笔退费 diff_total 永久 += 退费额。资金链唯一自动安全网(diff>1 告警)被退费持续打假漂移,
运维脱敏且掩盖真实漂移(如线上退款库存灌水)。
修:platform_consumed 改净额(consume − refund),refund 抵消对应 consume。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_audit_platform_consumed_nets_refund():
    src = (ROOT / "services" / "inventory_audit.py").read_text(encoding="utf-8")
    assert "WHEN type='refund'" in src, "platform_consumed 须净额抵消 refund(防退费假漂移)"
    assert "type IN ('consume', 'refund')" in src, "对账须同时纳入 consume 与 refund 净额"
