"""判别性回归测试 · 第2轮 · api/finance_api.py 财务中心修复。

source-inspection 判别锁:直接读源码断言修复标志存在。修复被回退则断言失败。
不 import server.py / 不连 DB(财务端点全在线程池跑 sync DB · 无法离线执行)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

SRC = (Path(__file__).resolve().parents[2] / "api" / "finance_api.py").read_text(encoding="utf-8")


def _slice(marker: str, length: int = 4500) -> str:
    """截取某函数定义起始处的一段源码用于局部断言。"""
    i = SRC.index(marker)
    return SRC[i:i + length]


# ---------------------------------------------------------------------------
# GEO-R1-CAN-033 · 现金口径收入须冲减当期已完成退款(与权责口径对称)
# ---------------------------------------------------------------------------

def test_geo_r1_can_033_marker_present():
    assert "[GEO-R1-CAN-033]" in SRC


def test_geo_r1_can_033_overview_uses_cash_net():
    ov = _slice("async def overview")
    # overview 现金口径 revenue 走净额 cash_net,而非充值毛额 cash
    assert "cash_net" in ov, "overview 现金口径收入必须使用净额 cash_net"
    assert "else cash_net" in ov, "overview basis=cash 的 revenue 必须取 cash_net"
    assert "cash - refund_cash" in ov, "cash_net 必须 = cash - refund_cash"


def test_geo_r1_can_033_pnl_and_export_subtract_refund():
    pnl = _slice("async def pnl")
    assert "cash - _cash_refund" in pnl, "pnl 现金口径 revenue 必须冲减 _cash_refund"
    exp = _slice("async def export_csv")
    assert "cash - _cash_refund" in exp, "export 现金口径 revenue 必须冲减 _cash_refund"


# ---------------------------------------------------------------------------
# GEO-R1-CAN-081 · 媒体 COGS 只计已完成订单(status=2)· 排除退款/拒稿/撤回/待接单/发布中
# ---------------------------------------------------------------------------

def test_geo_r1_can_081_marker_present():
    assert "[GEO-R1-CAN-081]" in SRC


def test_geo_r1_can_081_media_cost_filters_completed():
    mc = _slice("def _media_cost", 1400)
    assert "status = 2" in mc, "_media_cost 必须过滤 status=2 已完成订单"
    assert "mhz_synced_orders WHERE status = 2" in mc


def test_geo_r1_can_081_cost_center_breakdown_same_filter():
    # _media_cost + cost-center 分媒体明细两处都必须带 status=2 过滤(否则合计 ≠ 明细)
    assert SRC.count("mhz_synced_orders WHERE status = 2") >= 2, \
        "COGS 总额与分媒体明细两处 SQL 都要 status=2 同口径"


# ---------------------------------------------------------------------------
# GEO-R1-CAN-052 · skipped (no_change_needed):故意 fail-soft · 只读面板 · 已 log warning
# GEO-R9-CAN-008 · skipped:跨账守恒方程(应付=已结算-已提现-已换算力)需人工资金复核
# 下面锁定"未被误改":liabilities 仍按既有 SSOT 结构保留(不在本轮机械改守恒逻辑)。
# ---------------------------------------------------------------------------

def test_untouched_findings_not_silently_altered():
    # GEO-R9-CAN-008 被 skip · liabilities 端点仍在 · 未被本轮改动破坏
    assert "async def liabilities" in SRC
    # GEO-R1-CAN-052 被判 no_change_needed · 故意 fail-soft 的文档说明仍在(有意保留)
    assert "只读展示绝不让整端点 500" in SRC
