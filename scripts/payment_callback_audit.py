"""
[#6] 支付回调一致性 helper 单元测试

不需要起 HTTP 服务，直接单元层调用 _verify_callback_consistency。
用真实 recharge_orders 表里已存在的订单做参照。
"""
import os
import sys
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.wallet_api import _verify_callback_consistency
from db.connection import get_connection


def main():
    # 找一笔真实的已支付订单做参照
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, amount_cents FROM recharge_orders
        WHERE payment_status = 'paid' AND payment_method = 'wechat'
        LIMIT 1
    """)
    sample = cur.fetchone()
    conn.close()

    if not sample:
        print("⚠️  数据库无 wechat paid 订单，挑 simulate 渠道兜底")
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, amount_cents FROM recharge_orders
            WHERE payment_status = 'paid' LIMIT 1
        """)
        sample = cur.fetchone()
        conn.close()
        if not sample:
            print("❌ 数据库无 paid 订单，无法测试")
            return 1

    order_id = sample["id"]
    real_cents = sample["amount_cents"]
    print(f"参照订单: id={order_id}  本地金额={real_cents}分 ({real_cents/100:.2f}元)")
    print()

    cases = []

    # Case 1: 金额一致 → 应通过
    ok, reason = _verify_callback_consistency(
        order_id=order_id,
        callback_amount_cents=real_cents,
        callback_label="测试1-金额一致",
    )
    cases.append(("金额一致 → 应通过", ok, reason, True))

    # Case 2: 金额少 1 分 → 应拒绝（防 0.01 元伪造攻击）
    ok, reason = _verify_callback_consistency(
        order_id=order_id,
        callback_amount_cents=real_cents - 1,
        callback_label="测试2-金额少1分",
    )
    cases.append(("金额少 1 分 → 应拒绝", ok, reason, False))

    # Case 3: 金额多 1 分 → 应拒绝
    ok, reason = _verify_callback_consistency(
        order_id=order_id,
        callback_amount_cents=real_cents + 1,
        callback_label="测试3-金额多1分",
    )
    cases.append(("金额多 1 分 → 应拒绝", ok, reason, False))

    # Case 4: 不存在的订单 → 应拒绝
    ok, reason = _verify_callback_consistency(
        order_id="不存在的订单ID_XYZ",
        callback_amount_cents=100,
        callback_label="测试4-订单不存在",
    )
    cases.append(("订单不存在 → 应拒绝", ok, reason, False))

    # Case 5: extra_checks 商户号一致 → 应通过
    ok, reason = _verify_callback_consistency(
        order_id=order_id,
        callback_amount_cents=real_cents,
        callback_label="测试5-mchid一致",
        extra_checks={"mchid": ("1900000001", "1900000001")},
    )
    cases.append(("mchid 一致 → 应通过", ok, reason, True))

    # Case 6: 商户号不一致 → 应拒绝（防别的商户回调被混入）
    ok, reason = _verify_callback_consistency(
        order_id=order_id,
        callback_amount_cents=real_cents,
        callback_label="测试6-mchid不一致",
        extra_checks={"mchid": ("1900000001", "9999999999")},
    )
    cases.append(("mchid 不一致 → 应拒绝", ok, reason, False))

    # 输出
    print("=" * 80)
    print("[#6] _verify_callback_consistency 单元测试")
    print("=" * 80)
    passed = 0
    for label, ok, reason, expect_ok in cases:
        # 期望 ok 的取值跟实际相同 → 测试通过
        actual_pass = (ok == expect_ok)
        if actual_pass:
            passed += 1
        mark = "✅" if actual_pass else "❌"
        print(f"  {mark} {label}")
        print(f"     实际 ok={ok}  期望 ok={expect_ok}  详情={reason}")

    print()
    print("=" * 80)
    print(f"通过: {passed}/{len(cases)}")
    print("=" * 80)
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
