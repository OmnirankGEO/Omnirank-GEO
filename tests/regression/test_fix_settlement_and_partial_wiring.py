"""判别性回归 · R2-CAN-040 结算守恒钳制 + R6-CAN-009 article_gen 部分退款接线(老板批)

source-inspection 判别锁:回退修复对应断言失败。
"""
from __future__ import annotations
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SETTLE = (ROOT / "services" / "settlement_orchestrator.py").read_text(encoding="utf-8")
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")

# [v7 finding1 重构] canonical 5 步落账收敛到 record_v35_core_settlement · fund-source 特有部分
#   (锁价 factory / user_wallets 守恒钳制 + 反扣)仍在 _record_factory_settlement。守恒断言 scope 到该方法体。
_i0 = SETTLE.find("def _record_factory_settlement")
_i1 = SETTLE.find("def _record_legacy_settlement")
FACTORY_BODY = SETTLE[_i0:_i1] if 0 <= _i0 < _i1 else SETTLE


class TestR2Can040ConservationClamp:
    def test_credit_equals_revoke_clamp(self):
        assert "GEO-R2-CAN-040" in FACTORY_BODY, "缺 R2-CAN-040 标记"
        # [v7] 钳制额透传给 canonical core 的 credit_*_points(credit==revoke 守恒)· core 内 allocate_credit 用它
        #   锚定实际调用 `core = record_v35_core_settlement(`(非注释提及)
        core_i = FACTORY_BODY.find("core = record_v35_core_settlement(")
        assert core_i != -1, "R2-CAN-040: _record_factory_settlement 必须委托 record_v35_core_settlement 唯一算法"
        core_call = FACTORY_BODY[core_i:]
        assert "credit_tool_points=_clamp_tool" in core_call and "credit_publish_points=_clamp_publish" in core_call \
            and "credit_bonus_points=_clamp_bonus" in core_call, \
            "R2-CAN-040: 客户入账必须用钳制额(credit==revoke 守恒)透传 core"
        # revoke 必须 = 钳制额
        assert "revoke_paid = _clamp_paid" in FACTORY_BODY and "revoke_bonus = _clamp_bonus" in FACTORY_BODY, \
            "R2-CAN-040: user_wallets 反扣额必须 = 客户入账钳制额(守恒)"

    def test_no_raise_in_main_txn(self):
        # 钳制→反扣段(RECON 到 core 调用前)不得有 raise 语句(主事务内 raise 会回滚充值致客户丢积分);注释"raise"字样不算
        import re
        i = FACTORY_BODY.find("GEO-R2-CAN-040")
        core_idx = FACTORY_BODY.find("core = record_v35_core_settlement(", i)
        block = FACTORY_BODY[i:core_idx]
        raise_stmts = re.findall(r"\n\s+raise\s+\w", block)
        assert not raise_stmts, f"R2-CAN-040: 钳制段不得有 raise 语句(会致客户丢积分),发现 {raise_stmts}"

    def test_read_balance_before_allocate(self):
        # [v7] 余额 FOR UPDATE 读(钳制)必须在 canonical core 调用(其内 allocate_credit 入账)之前(先钳制再入账)
        bal_idx = FACTORY_BODY.find("FOR UPDATE")
        core_idx = FACTORY_BODY.find("core = record_v35_core_settlement(")
        assert 0 < bal_idx < core_idx, "R2-CAN-040: 余额读(钳制)必须先于 record_v35_core_settlement 调用(先钳制再入账)"


class TestR6Can009PartialRefundWiring:
    def test_article_gen_partial_refund_wired(self):
        # generate_task 内部分失败(_err>0)必须调 refund_points(amount=_err×base)
        i = SERVER.find("async def generate_task():")
        assert i != -1
        block = SERVER[i: i + 4000]
        assert "GEO-R6-CAN-009" in block, "article_gen 缺 R6-CAN-009 部分退款接线"
        assert "_err * _base_ag" in block, "R6-CAN-009: 部分退款额须 = _err × base"
        assert "amount=_partial_refund" in block, "R6-CAN-009: 须用 refund_points 的 amount 部分退款上限"
