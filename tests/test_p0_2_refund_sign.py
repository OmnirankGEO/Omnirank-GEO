"""[BUG-P0-2] legacy 充值退款负数 consume 符号修复 · 回归单测(纯函数 · 不依赖真 DB)

根因:point_transactions.consume 的 amount 恒负(prod 实证 953/953 全负)。
原 SUM(amount) 得负数 → unused = total - 负 = 加法 → 膨胀(消费越多退越多)。
prod 铁证:user28 858 分订单,消费 12870 → 旧公式算出可退 13728 / ratio 16.0(>原单 16 倍)。
修:两处退款路径(request_refund / check_refund_eligibility)SQL 改 SUM(ABS(amount)) +
   共用 _compute_refund_unused_ratio 单点权威(unused 夹 [0,total] · ratio 夹 [0,1])。
"""
from pathlib import Path

from api.wallet_api import _compute_refund_unused_ratio as calc


def test_normal_unused_full():
    # 未消费 → 全额可退
    assert calc(858, 0) == (858, 1.0)


def test_partial_consume():
    unused, ratio = calc(858, 500)
    assert unused == 358
    assert abs(ratio - 358 / 858) < 1e-9


def test_prod_user28_overconsume_no_inflation():
    # prod 实证:858 分订单消费 12870 → 修复后 unused=0 不可退(修复前 13728 / ratio 16)
    unused, ratio = calc(858, 12870)
    assert unused == 0
    assert ratio == 0.0


def test_negative_consumed_failclosed():
    # fail-closed 兜底:即便上游 SQL 漏改 ABS 传入负 consumed,min 上限也夹回 total,绝不膨胀
    unused, ratio = calc(858, -12870)
    assert unused == 858          # 夹回 total,不是 13728
    assert ratio == 1.0           # 夹回 1.0,不是 16.0


def test_exact_consume_zero_unused():
    assert calc(858, 858) == (0, 0.0)


def test_zero_total_no_divzero():
    # total=0 不除零
    assert calc(0, 0) == (0, 0.0)
    assert calc(0, 100) == (0, 0.0)


def test_ratio_never_exceeds_one():
    # 任意 consumed,ratio 恒夹在 [0,1]、unused 恒夹在 [0,total]
    for c in (-999999, -1, 0, 1, 500, 858, 999999):
        unused, ratio = calc(858, c)
        assert 0.0 <= ratio <= 1.0
        assert 0 <= unused <= 858


def test_both_refund_paths_use_sum_abs():
    # 静态守护:两处退款 SQL 都用 SUM(ABS(amount)) + 走单点权威 helper,防未来漏改一处复发 P0-2
    src = Path(__file__).resolve().parents[1].joinpath("api", "wallet_api.py").read_text(encoding="utf-8")
    assert src.count("SUM(ABS(amount))") >= 2, "两处退款 consume 求和必须 SUM(ABS),防负数膨胀"
    assert src.count("_compute_refund_unused_ratio(total_order_points") >= 2, "两处退款须共用单点权威 helper"
