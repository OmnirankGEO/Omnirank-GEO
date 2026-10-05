"""[Deploy-CTO NO-GO v3 finding 1] V3.5 精确退款真接通 · 判别性 DB 行为测试(throwaway PG)。

复现 Deploy-CTO 反证:v2 里 V3.5 deduct 返回 charge_tx_id=None、拆分流水 related_order_id=NULL。
v3 修复:deduct 先生成 order_id 传 consume_credit → 拆分流水共享 related_order_id;deduct 返回真实
charge_tx_id;退款按 related_order_id 组精确退,不再靠"最近3笔+5秒窗"。

判别性:把 billing.py V3.5 分支的 order_id 生成挪回 consume 之后(不传 related_order_id)→
test_v35_split_group_refund_whole 会退不全 / 或 test_no_cross_order_bleed 会串退。
需 throwaway PG(customer_agent_credit_wallets / customer_credit_transactions / feature_pricing / users)。
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

AGENT = 860001
FEATURE = "_v35_tool_feat"   # 非 publish feature → bonus 优先 + tool 补差(可造 bonus+tool 拆分)


def _conn():
    c = psycopg2.connect(DB)
    c.cursor_factory = psycopg2.extras.RealDictCursor
    return c


def _seed_v35(cur, uid, tool=1000, bonus=1000):
    cur.execute("DELETE FROM customer_credit_transactions WHERE customer_user_id=%s", (uid,))
    cur.execute("DELETE FROM customer_agent_credit_wallets WHERE customer_user_id=%s", (uid,))
    cur.execute("DELETE FROM users WHERE id=%s", (uid,))
    cur.execute("INSERT INTO users (id, username) VALUES (%s,%s)", (uid, f"v35u{uid}"))
    cur.execute(
        "INSERT INTO customer_agent_credit_wallets (customer_user_id, agent_user_id, tool_credit_points, publish_credit_points, bonus_credit_points) "
        "VALUES (%s,%s,%s,0,%s)", (uid, AGENT, tool, bonus))
    cur.execute("INSERT INTO feature_pricing (feature_code, cost_points, feature_name) VALUES (%s,%s,%s) "
                "ON CONFLICT (feature_code) DO UPDATE SET cost_points=EXCLUDED.cost_points", (FEATURE, 500, FEATURE))


async def _deduct(uid):
    from middleware.billing import deduct_points
    return await deduct_points(uid, FEATURE)


async def _refund(uid, **kw):
    from middleware.billing import refund_points
    return await refund_points(uid, FEATURE, "v35-test", **kw)


@pytest.mark.asyncio
async def test_v35_deduct_returns_real_charge_tx_id():
    """V3.5 deduct 必须返回真实 charge_tx_id(非 None)且 charge_tx_ids 非空。"""
    uid = 861001
    with _conn() as c:
        cur = c.cursor(); _seed_v35(cur, uid, tool=1000, bonus=0); c.commit()
    res = await _deduct(uid)
    assert res.get("channel") == "v35", f"应走 V3.5 通道,实际 {res}"
    assert res.get("charge_tx_id") is not None, f"🔴 V3.5 deduct 必须返回真实 charge_tx_id,实际 {res}"
    assert res.get("charge_tx_ids"), "charge_tx_ids 应非空"
    # 且流水 related_order_id = 返回的 order_id(不再 NULL)
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT related_order_id FROM customer_credit_transactions WHERE id=%s", (res["charge_tx_id"],))
        assert cur.fetchone()["related_order_id"] == res["order_id"], "拆分流水必须带 related_order_id(非 NULL)"


@pytest.mark.asyncio
async def test_v35_split_group_refund_whole():
    """bonus100+tool400 一次扣(同 order_id)→ 按 charge_tx_id 退 → 必须退整组 500 · 钱包守恒。"""
    uid = 861002
    with _conn() as c:
        cur = c.cursor(); _seed_v35(cur, uid, tool=400, bonus=100); c.commit()
    charge = await _deduct(uid)  # cost 500 = bonus100 + tool400
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT tool_credit_points, bonus_credit_points FROM customer_agent_credit_wallets WHERE customer_user_id=%s", (uid,))
        w0 = cur.fetchone()
    assert w0["bonus_credit_points"] == 0 and w0["tool_credit_points"] == 0, f"扣后应清零,实际 {w0}"
    res = await _refund(uid, charge_tx_id=charge["charge_tx_id"])
    assert res.get("success") and res.get("refunded") == 500, f"🔴 拆分组按 charge_tx_id 应退整组 500,实际 {res}"
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT tool_credit_points, bonus_credit_points FROM customer_agent_credit_wallets WHERE customer_user_id=%s", (uid,))
        w1 = cur.fetchone()
    assert w1["bonus_credit_points"] == 100 and w1["tool_credit_points"] == 400, f"整组退回 · 守恒复原,实际 {w1}"


@pytest.mark.asyncio
async def test_v35_no_cross_order_bleed_within_5s():
    """5 秒内两个独立订单(各扣一次)→ 按 order1 的 charge_tx_id 退 → 只退 order1,order2 不被串退。"""
    uid = 861003
    with _conn() as c:
        cur = c.cursor(); _seed_v35(cur, uid, tool=2000, bonus=0); c.commit()
    charge1 = await _deduct(uid)   # order1: tool 500
    charge2 = await _deduct(uid)   # order2: tool 500(5 秒内)
    assert charge1["order_id"] != charge2["order_id"]
    res = await _refund(uid, charge_tx_id=charge1["charge_tx_id"])
    assert res.get("refunded") == 500, f"只退 order1 的 500,实际 {res}"
    # order2 的 consume 不应被退(其 related_order_id 未出现在 refund 引用里)
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM customer_credit_transactions "
                    "WHERE customer_user_id=%s AND type='refund' AND related_order_id=%s",
                    (uid, str(charge2["charge_tx_id"])))
        assert cur.fetchone()["n"] == 0, "🔴 order2 不得被串退(5 秒窗跨订单误并是旧 bug)"


@pytest.mark.asyncio
async def test_v35_refund_idempotent():
    """同一 charge 重复退 → 第二次 no-op(不双退)。"""
    uid = 861004
    with _conn() as c:
        cur = c.cursor(); _seed_v35(cur, uid, tool=500, bonus=0); c.commit()
    charge = await _deduct(uid)
    r1 = await _refund(uid, charge_tx_id=charge["charge_tx_id"])
    assert r1.get("refunded") == 500
    r2 = await _refund(uid, charge_tx_id=charge["charge_tx_id"])
    assert not r2.get("success") or r2.get("refunded", 0) == 0, f"重复退必须被幂等拦,实际 {r2}"
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT tool_credit_points FROM customer_agent_credit_wallets WHERE customer_user_id=%s", (uid,))
        assert cur.fetchone()["tool_credit_points"] == 500, "幂等:钱包只回一次 500"
