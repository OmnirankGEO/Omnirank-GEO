"""[BUG-P0-1] 代发退款不与扣费对账 · 回归单测(纯函数 + 静态守护 · 不依赖真 DB)

根因:admin 免扣下单 deduct=0,但 create_order 丢失 admin_exempt 标志 →
订单失败仍按名义 cost_points 全额退 = 钱包凭空注入(prod user1/10 admin 全凭空 ~17万)。
修:① 下单落 admin_exempt + actually_deducted_points;② 退款唯一入口 refund_for_publish_order
   按订单维度 cap(_publish_refund_cap):实退 ≤ max(0, 真实扣费 - 已退),admin 免扣订单退 0。
"""
from pathlib import Path

from db.meijiehezi_db import _publish_refund_cap as cap


def test_admin_exempt_order_refunds_zero():
    # admin 免扣订单 actually_deducted=0 → 退 0(核心:防凭空注入)
    assert cap(975, 0, 0) == 0


def test_legacy_order_null_keeps_original():
    # 老订单无对账字段(None)→ 维持原行为退请求额(历史凭空靠回收 SQL 清)
    assert cap(975, None, 0) == 975


def test_normal_full_refund():
    # 真扣 3900 未退过 → 全额可退
    assert cap(3900, 3900, 0) == 3900


def test_partial_already_refunded():
    # 真扣 3900,已退 2400 → 只能再退 1500
    assert cap(3900, 3900, 2400) == 1500


def test_already_refunded_full_caps_zero():
    # 已退满 → 再退 0
    assert cap(975, 975, 975) == 0


def test_over_request_capped_to_deducted():
    # 请求额超真实扣费 → 夹到真实扣费
    assert cap(99999, 3900, 0) == 3900


def test_already_exceeds_deducted_failclosed():
    # 防御:已退 > 真实扣费(脏数据)→ cap 不为负,返回 0
    assert cap(975, 3900, 9999) == 0


def test_never_exceeds_deducted_minus_refunded():
    # 任意请求额,实退恒 ≤ max(0, deducted - already) 且 ≤ 请求额
    for req in (0, 100, 3900, 99999):
        for ded in (0, 3900):
            for alr in (0, 1000, 5000):
                out = cap(req, ded, alr)
                assert out <= req
                assert out <= max(0, ded - alr)
                assert out >= 0


def test_source_落标志_and_cap_wired():
    # 静态守护:下单落 admin_exempt/actually_deducted + 退款走 cap,防未来重构漏接
    db_src = Path(__file__).resolve().parents[1].joinpath("db", "meijiehezi_db.py").read_text(encoding="utf-8")
    api_src = Path(__file__).resolve().parents[1].joinpath("api", "meijiehezi_api.py").read_text(encoding="utf-8")
    # create_order 写入两个对账列
    assert "actually_deducted_points" in db_src and "admin_exempt" in db_src
    # 退款唯一入口调 cap
    assert "_publish_refund_cap(" in db_src, "退款入口必须走 cap,防凭空注入复发"
    # 两处下单都把 admin_exempt 传入 create_order
    assert api_src.count('admin_exempt=bool(billing_result.get("admin_exempt"))') >= 2
