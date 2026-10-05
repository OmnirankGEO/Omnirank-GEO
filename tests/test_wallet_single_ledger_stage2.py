"""[单账本收敛 · 阶段② · 2026-07-27] 拆除 billing.py 的 V3.5 分流 —— 判别测试

工单：docs/AI-CONTEXT/WALLET_SINGLE_LEDGER_WORKORDER_2026-07-27.md (a80b4a4e) §5 验收 6/7

Owner 2026-07-27：「不能有两本账，用户只有充值算力和赠送算力」。
阶段①已把 5 个客户的信用额度并回 user_wallets；本批拆掉按身份分流的代码。

本文件是**双向**锁：
  A. 删干净了 —— 扣费/预检/退款/冻结不得再按 customer_agent_credit_wallets 分流
  B. 没删过头 —— commit_freeze / release_freeze 的历史冻结路由必须保留（工单 §4.2）

🔴 工单 §5 第 7 条要求的三个变异必须转红，对应：
  · 把 _is_v35_customer 分支加回来        → test_no_identity_based_ledger_routing
  · 冻结句柄改回"现场猜"                  → test_freeze_routing_by_table_not_identity
  · 迁移映射改错（tool → bonus）          → test_migration_maps_tool_and_publish_to_paid
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BILLING = ROOT / "middleware" / "billing.py"


def _billing_src() -> str:
    return BILLING.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """只看可执行代码。

    注释与 docstring 里提到 v35 是【允许的】—— 拆除时特意留了大量"为什么删这段"的说明，
    还保留了 _route_freeze_table 那段解释 BUG-P2 历史事故的 docstring。
    断言必须只针对可执行代码，否则我们自己写的说明会把自己的锁弄红。
    保留行结构（不压成一行），下面的函数体正则还要用。
    """
    # 三引号块（docstring / 多行字符串）整体替换成占位，保持行数不变
    def _blank(m: re.Match) -> str:
        return '""' + "\n" * m.group(0).count("\n")

    src = re.sub(r'"""[\s\S]*?"""', _blank, src)
    src = re.sub(r"'''[\s\S]*?'''", _blank, src)
    lines = []
    for line in src.splitlines():
        if line.lstrip().startswith("#"):
            lines.append("")
            continue
        lines.append(re.sub(r"\s+#.*$", "", line))
    return "\n".join(lines)


# ============================================================
# A · 删干净了：不得再按身份分流
# ============================================================

def test_v35_helper_functions_removed():
    """五个 V3.5 专用 helper 必须从模块里消失（不是留着不调用）。"""
    import middleware.billing as billing

    for gone in (
        "_is_v35_customer",
        "_v35_check_credit_only",
        "_v35_credit_error_to_http",
        "_refund_v35_customer_credit",
        "_v35_confirmed_refund_points",
    ):
        assert not hasattr(billing, gone), f"{gone} 仍在 billing 模块里 —— 双账本没拆干净"


def test_no_identity_based_ledger_routing():
    """🔴 安全锁：可执行代码里不得再出现按 customer_agent_credit_wallets 判身份的分流。

    这是"两本账"的根 —— 只要还有一处按身份选账本，客户的钱就会再次被劈开。
    """
    code = _strip_comments(_billing_src())
    assert "_is_v35_customer" not in code, "billing 可执行代码仍在按身份判定账本"
    assert "SELECT 1 FROM customer_agent_credit_wallets" not in code, \
        "billing 仍在查信用钱包判身份"
    # consume_credit 是信用钱包的扣费入口，扣费链不得再调用它
    assert "consume_credit(" not in code, "扣费仍在走 customer_credit.consume_credit"


def test_billing_no_longer_imports_customer_credit_write_apis():
    """写侧 API 不得再被 import —— 留着 import 就有人会再用。"""
    code = _strip_comments(_billing_src())
    for api in ("consume_credit", "refund_credit", "allocate_credit", "revoke_credit"):
        assert f"import {api}" not in code and f"    {api}," not in code, \
            f"billing 仍 import 信用钱包写 API: {api}"


@pytest.mark.parametrize("fn_name", ["check_balance_only", "deduct_points", "freeze_points"])
def test_core_billing_paths_have_no_v35_branch(fn_name):
    """三个入口的函数体里不得再有 v35 分支。"""
    code = _strip_comments(_billing_src())
    m = re.search(rf"async def {fn_name}\(.*?(?=\nasync def |\ndef )", code, re.DOTALL)
    assert m, f"未找到 {fn_name} 函数体"
    body = m.group(0)
    assert "_is_v35_customer" not in body, f"{fn_name} 仍按身份分流"
    assert "customer_agent_credit_wallets" not in body, f"{fn_name} 仍读信用钱包"


def test_refund_keeps_ledger_type_param_but_not_routing():
    """ledger_type 形参保留（外部调用方仍在传），但不得再用它选账本。"""
    src = _billing_src()
    m = re.search(r"async def refund_points\(.*?(?=\nasync def |\ndef )", src, re.DOTALL)
    assert m
    body = m.group(0)
    assert "ledger_type" in body, "ledger_type 形参被删了 —— 会断掉 server.py 的文章恢复链"
    code = _strip_comments(body)
    assert "_use_v35_ledger" not in code, "退款仍在按账本类型分流"
    assert "_refund_v35_customer_credit" not in code


# ============================================================
# B · 没删过头：历史冻结仍要能结算（工单 §4.2）
# ============================================================

def test_freeze_routing_by_table_not_identity():
    """🔴 安全锁：commit/release 必须按冻结记录【实际所在表】路由，不是按客户当前身份。

    这是 BUG-P2 修过的真实事故：freeze 在 legacy 创建后客户开通 V3.5 →
    按当前身份路由就会去 customer_credit_freezes 找，找不到句柄。
    拆除时如果把这套机制一并删掉，切换瞬间的在途冻结就会失联。
    """
    import middleware.billing as billing

    assert hasattr(billing, "_route_freeze_table"), \
        "_route_freeze_table 被删了 —— 在途冻结会失去按表路由的能力"

    src = _billing_src()
    m = re.search(r"def _route_freeze_table\(.*?(?=\nasync def |\ndef )", src, re.DOTALL)
    assert m
    body = m.group(0)
    # 它必须仍然会查两张表
    assert "point_freezes" in body and "customer_credit_freezes" in body, \
        "_route_freeze_table 不再同时认两张冻结表"
    # 且不得改成按身份猜
    assert "_is_v35_customer" not in _strip_comments(body), \
        "冻结路由退回了'按当前身份猜'——正是 BUG-P2 的原始形态"


@pytest.mark.parametrize("fn_name", ["commit_freeze", "release_freeze"])
def test_commit_release_keep_v35_settlement_path(fn_name):
    """历史 v35 冻结必须仍能 commit/release —— 只是不再【新建】。"""
    code = _strip_comments(_billing_src())
    m = re.search(rf"async def {fn_name}\(.*?(?=\nasync def |\ndef )", code, re.DOTALL)
    assert m, f"未找到 {fn_name}"
    body = m.group(0)
    assert '_route == "v35"' in body, f"{fn_name} 丢了 v35 结算分支 —— 在途冻结会结算不了"
    assert ("commit_customer_freeze" in body) or ("release_customer_freeze" in body), \
        f"{fn_name} 不再调用信用冻结的结算函数"


def test_freeze_points_no_longer_creates_v35_freezes():
    """不再【新建】v35 冻结（与上一条互补：能结算旧的，但不产生新的）。"""
    code = _strip_comments(_billing_src())
    m = re.search(r"async def freeze_points\(.*?(?=\nasync def |\ndef )", code, re.DOTALL)
    assert m
    body = m.group(0)
    assert "freeze_customer_credit" not in body, "仍在新建 v35 冻结记录"


# ============================================================
# C · 阶段①迁移映射（工单 §5 第 7 条第三个变异）
# ============================================================

MIGRATE_SQL = ROOT / "scripts" / "wallet_credit_merge_migrate_2026_07_27.sql"


def test_migration_maps_tool_and_publish_to_paid():
    """🔴 安全锁：tool + publish → paid_points；bonus_credit → bonus_points。

    映射搞反（比如 tool 进 bonus）后果很实在：bonus 不能用于发布，
    客户的充值算力会变成不能发布的赠送算力。
    """
    sql = MIGRATE_SQL.read_text(encoding="utf-8")
    assert "v_merge_paid  := r.tool_credit_points + r.publish_credit_points;" in sql, \
        "tool+publish 必须并入 paid_points"
    assert "v_merge_bonus := r.bonus_credit_points;" in sql, \
        "bonus_credit 必须并入 bonus_points"
    assert "paid_points  = paid_points  + v_merge_paid" in sql
    assert "bonus_points = bonus_points + v_merge_bonus" in sql


def test_migration_is_idempotent_by_order_id():
    sql = MIGRATE_SQL.read_text(encoding="utf-8")
    assert "'credit_merge:' || r.customer_user_id" in sql, "缺幂等键"
    assert "type = 'credit_ledger_merge' AND order_id = v_key" in sql, "幂等检查没按幂等键查"
    assert "[SKIP]" in sql, "重跑时应跳过而不是重复入账"


def test_migration_refuses_silent_fallback():
    """找不到钱包行必须显式失败 —— 工单 §4.3 禁止静默回落。"""
    sql = MIGRATE_SQL.read_text(encoding="utf-8")
    assert "RAISE EXCEPTION" in sql
    assert "没有 user_wallets 行" in sql


def test_migration_and_rollback_stop_on_error():
    """psql autocommit 下前一条 RAISE EXCEPTION 拦不住后一条 —— 必须有 ON_ERROR_STOP。"""
    for path in (MIGRATE_SQL, ROOT / "scripts" / "wallet_credit_merge_rollback_2026_07_27.sql"):
        assert "ON_ERROR_STOP on" in path.read_text(encoding="utf-8"), \
            f"{path.name} 缺 ON_ERROR_STOP，保护会形同虚设"


def test_rollback_never_touches_original_ledger():
    """回滚只删本次迁移自己写的记录，历史流水一行不动。"""
    sql = (ROOT / "scripts" / "wallet_credit_merge_rollback_2026_07_27.sql").read_text(encoding="utf-8")
    deletes = re.findall(r"DELETE FROM (\w+)[^;]*;", sql, re.DOTALL)
    assert set(deletes) <= {"point_transactions", "customer_credit_transactions"}, deletes
    # 两条 DELETE 都必须带本次迁移的键
    for stmt in re.findall(r"DELETE FROM [^;]+;", sql, re.DOTALL):
        assert "merge_key" in stmt or "credit_ledger_merge" in stmt, \
            f"存在不带迁移键的 DELETE，可能误删历史流水: {stmt[:80]}"
