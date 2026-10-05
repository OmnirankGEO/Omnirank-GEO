"""判别性 DB 行为测试 · billing.py 部分退款 + 按 charge_id 退款(老板批)

GEO-R6-CAN-009: refund_points 加 amount 上限 → 部分成功场景只退失败部分(不全额、不为0)。
GEO-R2-CAN-039: refund_points 加 charge_tx_id → 精确退指定那笔 consume(不靠 newest-by-feature)。

真 DB 行为(throwaway PostgreSQL @5433):seed consume → 调 refund_points → 断言钱包守恒。
回退修复(去掉 amount 封顶)→ test_partial_refund_caps_to_amount 必失败(会退全额)。
需 TEST_DATABASE_URL 指向已 init_wallet_tables 的 throwaway 库。
"""
from __future__ import annotations
import os
import sys
from pathlib import Path
import pytest
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB, reason="需 TEST_DATABASE_URL")


def _conn():
    c = psycopg2.connect(DB)
    c.cursor_factory = psycopg2.extras.RealDictCursor
    return c


def _seed(cur, uid, paid=1000, feature="_test_refund_feat", cost=100, consume=500):
    cur.execute("DELETE FROM point_transactions WHERE user_id=%s", (uid,))
    cur.execute("DELETE FROM user_wallets WHERE user_id=%s", (uid,))
    cur.execute("DELETE FROM users WHERE id=%s", (uid,))
    cur.execute("DELETE FROM customer_agent_credit_wallets WHERE customer_user_id=%s", (uid,))
    cur.execute("INSERT INTO users (id, username) VALUES (%s,%s)", (uid, f"u{uid}"))
    cur.execute("INSERT INTO user_wallets (user_id, paid_points, bonus_points, commission_points, deduction_preference) VALUES (%s,%s,0,0,'default')",
                (uid, paid))
    cur.execute("INSERT INTO feature_pricing (feature_code, cost_points, feature_name) VALUES (%s,%s,%s) ON CONFLICT (feature_code) DO UPDATE SET cost_points=EXCLUDED.cost_points",
                (feature, cost, feature))
    # 一笔 consume(paid)-consume
    cur.execute("""INSERT INTO point_transactions (user_id, type, point_type, amount, balance_after, feature_code, source)
                   VALUES (%s,'consume','paid',%s,%s,%s,'balance_deduction') RETURNING id""",
                (uid, -consume, paid - consume, feature))
    return cur.fetchone()["id"]


async def _refund(uid, feature, **kw):
    from middleware.billing import refund_points
    return await refund_points(uid, feature, "test", **kw)


@pytest.mark.asyncio
async def test_partial_refund_caps_to_amount():
    """R6-CAN-009: 扣 500,部分退款 amount=200 → 钱包只 +200(不 +500),守恒。"""
    uid = 990101
    # paid=0 表示扣后余额(用户曾有 500,扣 500 → 0),consume tx 记 -500
    with _conn() as c:
        cur = c.cursor(); _seed(cur, uid, paid=0, consume=500); c.commit()
    res = await _refund(uid, "_test_refund_feat", amount=200)
    assert res["success"] and res["refunded"] == 200, f"部分退款应退 200,实际 {res}"
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT paid_points FROM user_wallets WHERE user_id=%s", (uid,))
        paid = cur.fetchone()["paid_points"]
    assert paid == 200, f"钱包应 = 200(扣后0 + 退200),实际 {paid} —— 回退修复会退全额=500"


@pytest.mark.asyncio
async def test_full_refund_default_unchanged():
    """向后兼容:不传 amount → 全额退(行为不变)。"""
    uid = 990102
    with _conn() as c:
        cur = c.cursor(); _seed(cur, uid, paid=0, consume=500); c.commit()
    res = await _refund(uid, "_test_refund_feat")
    assert res["success"] and res["refunded"] == 500, f"默认应全额退 500,实际 {res}"


@pytest.mark.asyncio
async def test_partial_refund_idempotent():
    """部分退款后再退同笔 → 已被 order_id 引用标记,不重复退(防双退)。"""
    uid = 990103
    with _conn() as c:
        cur = c.cursor(); _seed(cur, uid, paid=0, consume=500); c.commit()
    r1 = await _refund(uid, "_test_refund_feat", amount=200)
    assert r1["refunded"] == 200
    r2 = await _refund(uid, "_test_refund_feat", amount=200)
    assert not r2["success"] or r2.get("refunded", 0) == 0, f"重复退应被幂等拦,实际 {r2}"


@pytest.mark.asyncio
async def test_refund_by_charge_tx_id():
    """R2-CAN-039: 两笔 consume,按 charge_tx_id 精确退第一笔,不误退第二笔。"""
    uid = 990104
    with _conn() as c:
        cur = c.cursor()
        tx1 = _seed(cur, uid, paid=0, consume=300)
        # 第二笔同 feature consume
        cur.execute("""INSERT INTO point_transactions (user_id, type, point_type, amount, balance_after, feature_code, source)
                       VALUES (%s,'consume','paid',%s,%s,%s,'balance_deduction') RETURNING id""",
                    (uid, -400, 0, "_test_refund_feat"))
        tx2 = cur.fetchone()["id"]
        c.commit()
    res = await _refund(uid, "_test_refund_feat", charge_tx_id=tx1)
    assert res["success"] and res["refunded"] == 300, f"按 charge_tx_id 应退第一笔 300,实际 {res}"
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM point_transactions WHERE user_id=%s AND type='refund' AND order_id=%s", (uid, str(tx2)))
        assert cur.fetchone()["n"] == 0, "第二笔 tx2 不应被退(精确退只退指定 charge)"
