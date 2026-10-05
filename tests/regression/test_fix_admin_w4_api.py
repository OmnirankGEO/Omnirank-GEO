"""
回归判别锁 · api/admin_w4_api.py

GEO-R1-CAN-039 (P1 · customer-wallet-stale-agent):
    resolve_dispute 的 reassign 分支只改 customer_agent_bindings.agent_user_id,
    从不同步 customer_agent_credit_wallets.agent_user_id → split-brain:
    consume_credit / refund 仍按旧 agent 归属。
    修复标志:reassign 分支内在同事务对 customer_agent_credit_wallets 也 UPDATE agent_user_id。

source-inspection 判别锁:回退修复则断言失败。不依赖 DB / 不 import server.py。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "api" / "admin_w4_api.py").read_text(encoding="utf-8")


def _reassign_block() -> str:
    """截取 reassign 分支到 else 之间的源码文本"""
    m = re.search(
        r'if req\.action == "reassign":(.*?)else:',
        SRC,
        re.DOTALL,
    )
    assert m, "未找到 reassign 分支源码"
    return m.group(1)


def test_reassign_syncs_wallet_agent():
    """reassign 分支必须同步 customer_agent_credit_wallets.agent_user_id"""
    block = _reassign_block()
    # 必须在 reassign 分支内对钱包表做 UPDATE ... SET agent_user_id
    assert "customer_agent_credit_wallets" in block, (
        "reassign 分支未触及 customer_agent_credit_wallets · 钱包 agent 会 stale"
    )
    assert re.search(
        r"UPDATE\s+customer_agent_credit_wallets\s+SET\s+agent_user_id",
        block,
        re.IGNORECASE,
    ), "reassign 分支未 UPDATE customer_agent_credit_wallets SET agent_user_id"


def test_wallet_update_scoped_by_customer():
    """钱包同步必须 WHERE customer_user_id(PK)· 不误伤其它客户"""
    block = _reassign_block()
    wallet_stmt = re.search(
        r"UPDATE\s+customer_agent_credit_wallets.*?WHERE\s+customer_user_id\s*=\s*%s",
        block,
        re.IGNORECASE | re.DOTALL,
    )
    assert wallet_stmt, "钱包 UPDATE 未按 customer_user_id 限定"


def test_fix_marker_present():
    """修复标记注释存在"""
    assert "GEO-R1-CAN-039" in SRC


def test_keep_old_branch_does_not_touch_wallet():
    """keep_old / reject 分支不应改钱包 agent(仅 reassign 改绑)"""
    m = re.search(r"else:(.*?)conn\.commit\(\)", SRC, re.DOTALL)
    assert m, "未找到 else 分支"
    else_block = m.group(1)
    assert "customer_agent_credit_wallets" not in else_block, (
        "keep_old/reject 分支不应改动钱包归属"
    )
