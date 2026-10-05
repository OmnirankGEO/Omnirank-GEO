"""判别性回归 · 我方(SELF)资金修复锁

覆盖:
- GEO-R6-CAN-011: meijiehezi 批量代发 · 扣费后 create_order 失败必须退费+作废已建单
- GEO-R6-CAN-013: managed 全品牌托管 · 扣费额与子项充值额服务端一致性校验(防 charge!=fund 套利)

源码文本区间断言(不依赖 DB/不 import 应用),回退修复对应断言失败。
跑: pytest tests/regression/test_fix_fund_selfset.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MEIJIE = (ROOT / "api" / "meijiehezi_api.py").read_text(encoding="utf-8")
MANAGED = (ROOT / "api" / "managed_campaign_api.py").read_text(encoding="utf-8")


def _window(src: str, anchor: str, n: int = 60) -> str:
    i = src.find(anchor)
    assert i != -1, f"未找到锚点 {anchor}"
    return "\n".join(src[i:].splitlines()[:n])


class TestR6Can011ChargeBeforeOrder:
    def test_order_loop_wrapped_with_refund_compensation(self):
        # 锚定唯一的 R6-CAN-011 修复标记(批量购物车 handler,非同名的其它 deduct 站点)
        w = _window(MEIJIE, "[GEO-R6-CAN-011 P2]", 45)
        assert "except Exception as _create_err" in w, \
            "create_order 循环必须包 try/except 捕获持久化失败"
        assert 'refund_points(user_id, "media_proxy_publish"' in w, \
            "R6-CAN-011: create_order 失败必须退回本批扣费"
        assert "raise HTTPException" in w, "R6-CAN-011: 退费后必须抛错(不吞错)"

    def test_created_orders_cancelled_on_failure(self):
        # 批量路径(多订单)失败时须作废已建订单;单发/短视频路径只 1 订单无需作废。
        # 锚定批量路径的作废循环。
        w = _window(MEIJIE, "作废已建订单,防被 scheduler 误提交", 12)
        assert "update_order_item_status" in w and "failed" in w, \
            "R6-CAN-011: 批量失败时须作废已建订单防 scheduler 误提交"

    def test_all_three_charge_paths_compensated(self):
        # R6-CAN-011 是跨 3 个 handler 同根因(单发/批量/短视频),3 处 media_proxy_publish
        # 扣费站点都必须有补偿。marker 至少出现 3 次。
        assert MEIJIE.count("[GEO-R6-CAN-011 P2]") >= 3, \
            "R6-CAN-011: 单发/批量/短视频三条扣费路径都须补偿(marker>=3)"


class TestR6Can013PricingConsistency:
    def test_server_side_charge_equals_fund(self):
        w = _window(MANAGED, "[GEO-R6-CAN-013 P1]", 20)
        assert "_server_pkg_total" in w, "R6-CAN-013: 须服务端合计子项充值额"
        assert "PRICING_MISMATCH" in w, "R6-CAN-013: 扣费额与子项合计不符须拒绝"
        # 一致性校验(_server_pkg_total 定义)必须在本 handler 扣费(final_total_yuan 站点)之前
        guard_idx = MANAGED.find("[GEO-R6-CAN-013 P1]")
        charge_idx = MANAGED.find("amount_yuan=req.final_total_yuan")
        assert 0 < guard_idx < charge_idx, "R6-CAN-013: 一致性校验必须在扣费之前"
