"""[单账本收敛 · 阶段③ · 2026-07-27] 停写信用钱包 + 下游同步改 —— 判别测试

工单：WALLET_SINGLE_LEDGER_WORKORDER_2026-07-27.md (a80b4a4e) §3 阶段③
影响判定表：WALLET_SINGLE_LEDGER_TEARDOWN_IMPACT_MAP_2026-07-27.md 的 A/B/E 档

阶段②拆的是"扣费按身份分流"，阶段③拆的是"还有谁在往信用钱包写"以及
"谁在读它、读到 0 会算错/显示错"。

锁三件事：
  B 档 —— 七个入账源不得再写 customer_agent_credit_wallets
  A 档 —— 幂等键 / 守恒式 / 对账口径必须同批改（不改会重复发钱、天天告警）
  E 档 —— 客户与代理可见的余额不得静默变 0
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _code(rel: str) -> str:
    """剥注释与 docstring —— 我们特意留了大量"为什么这样改"的说明，不能让它们把锁弄红。"""
    s = _src(rel)

    def _blank(m: re.Match) -> str:
        body = m.group(0)
        # 🔴 SQL 也写在三引号里 —— 不能一起剥掉，否则 "FROM point_transactions"
        #    这类断言会永远找不到目标（我第一版就是这么写错的，5 条锁假红）。
        if re.search(r"\b(SELECT|INSERT|UPDATE|DELETE|FROM|WHERE)\b", body, re.I):
            return body
        return '""' + "\n" * body.count("\n")

    s = re.sub(r'"""[\s\S]*?"""', _blank, s)
    s = re.sub(r"'''[\s\S]*?'''", _blank, s)
    out = []
    for line in s.splitlines():
        if line.lstrip().startswith("#"):
            out.append("")
            continue
        out.append(re.sub(r"\s+#.*$", "", line))
    return "\n".join(out)


# ============================================================
# B 档 · 七个入账源必须停写
# ============================================================

WRITE_PATHS = [
    "services/dealer_inventory_resale.py",
    "services/settlement_orchestrator.py",
    "api/agent_workbench_api.py",
    "services/offline_allocation.py",
    "services/agent_rebate.py",
    "services/bonus_grants.py",
]


@pytest.mark.parametrize("rel", WRITE_PATHS)
def test_no_writes_to_credit_wallet(rel):
    """🔴 安全锁：不得再有任何一处往信用钱包写。

    只要还剩一个入账源，拆除就白做 —— 新客户会继续被开出第二本账。
    """
    code = _code(rel)
    assert "allocate_credit(" not in code, f"{rel} 仍在调 allocate_credit 写信用钱包"
    assert "revoke_credit(" not in code, f"{rel} 仍在调 revoke_credit 动信用钱包"
    assert not re.search(r"UPDATE\s+customer_agent_credit_wallets", code), \
        f"{rel} 仍在直接 UPDATE 信用钱包"
    assert not re.search(r"INSERT\s+INTO\s+customer_credit_transactions", code), \
        f"{rel} 仍在写信用流水"


def test_dealer_resale_no_longer_moves_wallet_to_credit():
    """B1：user 149 看到的 -19,500 就是这段写的，必须整段消失。"""
    code = _code("services/dealer_inventory_resale.py")
    assert "v35_migrated_to_customer_credit" not in code, \
        "仍在写迁移流水 —— 两本账的头号入账源还活着"


def test_settlement_orchestrator_no_longer_reverse_deducts_wallet():
    """B2：充值主路径不得再"反扣 user_wallets 再塞进信用钱包"。"""
    code = _code("services/settlement_orchestrator.py")
    assert "v35_migrated_to_customer_credit" not in code
    assert "GREATEST(0, paid_points - " not in code, "仍在反扣客户 user_wallets"


def test_grant_helper_maps_pools_consistently_with_migration():
    """新的授予落点必须与阶段①迁移【同口径】，否则迁移与新增各记一套。"""
    code = _code("services/customer_entitlement.py")
    assert "paid_points = paid_points + " in code
    assert "bonus_points = bonus_points + " in code
    # 授予/回收都要显式失败而不是静默建行
    assert code.count("没有钱包行，拒绝静默处理") >= 2


def test_revoke_never_drives_balance_negative():
    """回收必须按当前余额夹紧 —— 客户可能已经花掉一部分。"""
    code = _code("services/customer_entitlement.py")
    assert "min(paid_points, before[" in code.replace('"', "'").replace("'", "'") or \
           "take_paid = min(" in code, "回收没有按余额夹紧"


# ============================================================
# A 档 · 不改会重复发钱 / 天天告警
# ============================================================

def test_rebate_idempotency_key_moved_to_platform_ledger():
    """🔴 安全锁（最危险的一条）：返利幂等键必须搬到 point_transactions。

    原来幂等标记由 allocate_credit 写进 customer_credit_transactions。
    那张表停写后查询恒为空 → 支付二次回调会【重复返利】，给服务商多发钱。
    幂等查询与写入必须同源，否则等于没有幂等。
    """
    code = _code("services/agent_rebate.py")
    assert "FROM customer_credit_transactions" not in code, \
        "返利幂等仍查已停写的信用流水表 —— 二次回调会重复发钱"
    assert "FROM point_transactions" in code, "返利幂等键没搬到平台流水"
    # 写入侧也必须落在同一张表，否则查得到写不进/写得进查不到
    assert "grant_to_customer(" in code


def test_inventory_audit_equation_drops_stopped_ledger():
    """A1：守恒式不得再依赖已停写的信用流水，否则会随时间单向漂移。

    🔴 2026-07-29 返修改口径：本条原来还断言 `"allocated_out" in eq`。
    那个名字在生产里【根本没有被赋过值】—— 这条静态锁把一个必然 NameError
    的等式认证成了"合规"，对账因此连续静默死亡。教训：断言"字符串在不在"
    不等于断言"代码跑得起来"。名字级断言换成账实相符式的两个量，
    “用了但没绑定”的通用形态由 tests/test_inventory_audit_equation_2026_07_29.py
    的 AST 锁 + 真跑 PG 的行为锁负责。
    """
    code = _code("services/inventory_audit.py")
    m = re.search(r"diff_total = .*?\n(?:.*?\n)?", code)
    assert m
    eq = m.group(0)
    assert "platform_consumed" not in eq, "守恒式仍含已停写账本的消费项"
    assert "refunded_or_revoked" not in eq, "守恒式仍含已停写账本的回收项"
    assert "wallet_total" in eq and "ledger_total" in eq, \
        "守恒式没换成账实相符式（钱包三池 − 全量流水）"


def test_fund_recovery_keeps_historical_v35_lookup():
    """A3：新工单不再走 v35，但历史工单的已退金额仍要查得到 —— 不能删。"""
    code = _code("db/fund_recovery_db.py")
    assert "customer_credit_transactions" in code, \
        "删掉了历史 v35 工单的已退金额查询 → 会重复退款"


# ============================================================
# E 档 · 客户/代理可见的余额不得静默变 0
# ============================================================

@pytest.mark.parametrize("rel,label", [
    ("api/customer_workbench_api.py", "客户额度页"),
    ("api/agent_finance_api.py", "代理端客户额度"),
    ("services/customer_binding.py", "客户绑定列表"),
])
def test_customer_facing_balance_reads_single_ledger(rel, label):
    """🔴 这些是【客户/代理直接看得见】的数字。读信用钱包 = 迁移后显示 0。"""
    code = _code(rel)
    assert "FROM customer_agent_credit_wallets" not in code, \
        f"{label} 仍读信用钱包，迁移后会静默显示 0"
    assert "user_wallets" in code, f"{label} 没有改读 user_wallets"


def test_customer_workbench_transactions_read_platform_ledger():
    """客户流水页同理 —— 读停写的表会显示空列表。"""
    code = _code("api/customer_workbench_api.py")
    assert "FROM point_transactions" in code
    m = re.search(r"SELECT[^;]*?FROM customer_credit_transactions", code)
    assert m is None, "客户流水页仍读已停写的信用流水表"


def test_frontend_merge_still_correct_without_changes():
    """前端零改动即正确：三池归 0 后 paidPointsDisplayed 自然退化成 paid_points。

    这条锁住"别去动前端" —— 迁移后 cc.toolCreditPoints/publishCreditPoints 恒 0，
    合并公式结果就等于 user_wallets 的值。改前端反而会引入不一致。
    """
    src = (ROOT / "frontend" / "src" / "context" / "WalletContext.tsx").read_text(encoding="utf-8")
    assert "paidPointsDisplayed = effectiveState.paidPoints + cc.toolCreditPoints + cc.publishCreditPoints" in src, \
        "前端合并公式被改动了 —— 单账本下它本来就该保持原样"
