"""[Deploy-CTO NO-GO finding 1] refund 全链接线 · 拆分组精确退 · 判别性 DB 行为测试(throwaway PG)。

复现 Deploy-CTO 报告的资金少退:bonus100 + paid400 同 order_id 拆分扣费,按 charge_tx_id 退 →
🔴 旧实现 `WHERE id=%s` 只退 charge_tx_id 那一行(bonus 100)· 少退 paid 400 = 资金少退。
✅ 修复:charge_tx_id 分支反查 order_id → 退【整个拆分组】500。
判别性:把 middleware/billing.py charge_tx_id 分支 SQL 改回 `WHERE id=%s` → 本文件
        test_split_group_refunds_whole_group 失败(只退 100)。

另测 deduct_points 返回不可变 charge_tx_id(finding 1 的"deduct 没返回"部分)。
需 TEST_DATABASE_URL / DATABASE_URL 指向已 bootstrap 的 throwaway 库(user_wallets/point_transactions/feature_pricing)。
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


def _reset(cur, uid):
    cur.execute("DELETE FROM point_transactions WHERE user_id=%s", (uid,))
    cur.execute("DELETE FROM user_wallets WHERE user_id=%s", (uid,))
    cur.execute("DELETE FROM users WHERE id=%s", (uid,))
    cur.execute("DELETE FROM customer_agent_credit_wallets WHERE customer_user_id=%s", (uid,))


def _seed_split(cur, uid, feature="_nogo_split_feat"):
    """曾有 bonus100 + paid400,已全扣(扣后余额都 0),两笔 consume 共享同一 order_id。"""
    _reset(cur, uid)
    cur.execute("INSERT INTO users (id, username) VALUES (%s,%s)", (uid, f"u{uid}"))
    cur.execute("INSERT INTO user_wallets (user_id, paid_points, bonus_points, commission_points, deduction_preference) "
                "VALUES (%s,0,0,0,'default')", (uid,))
    cur.execute("INSERT INTO feature_pricing (feature_code, cost_points, feature_name) VALUES (%s,%s,%s) "
                "ON CONFLICT (feature_code) DO UPDATE SET cost_points=EXCLUDED.cost_points", (feature, 500, feature))
    order_id = f"NOGO_SPLIT_{uid}"
    cur.execute("""INSERT INTO point_transactions (user_id, type, point_type, amount, balance_after, feature_code, order_id, source)
                   VALUES (%s,'consume','bonus',-100,0,%s,%s,'balance_deduction') RETURNING id""",
                (uid, feature, order_id))
    tx_bonus = cur.fetchone()["id"]
    cur.execute("""INSERT INTO point_transactions (user_id, type, point_type, amount, balance_after, feature_code, order_id, source)
                   VALUES (%s,'consume','paid',-400,0,%s,%s,'balance_deduction') RETURNING id""",
                (uid, feature, order_id))
    tx_paid = cur.fetchone()["id"]
    return tx_bonus, tx_paid, feature


async def _refund(uid, feature, **kw):
    from middleware.billing import refund_points
    return await refund_points(uid, feature, "nogo-test", **kw)


@pytest.mark.asyncio
async def test_split_group_refunds_whole_group():
    """核心:bonus100+paid400 同 order_id,按 bonus 笔 charge_tx_id 退 → 必须退【整组 500】,非只 100。"""
    uid = 991201
    with _conn() as c:
        cur = c.cursor()
        tx_bonus, tx_paid, feature = _seed_split(cur, uid)
        c.commit()
    res = await _refund(uid, feature, charge_tx_id=tx_bonus)
    assert res.get("success"), f"退款应成功,实际 {res}"
    assert res.get("refunded") == 500, (
        f"🔴 拆分组按 charge_tx_id 应退整组 500,实际退 {res.get('refunded')} "
        f"—— 旧 `WHERE id=%s` 单行实现只退 100(资金少退)")
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT paid_points, bonus_points FROM user_wallets WHERE user_id=%s", (uid,))
        w = cur.fetchone()
    assert w["bonus_points"] == 100 and w["paid_points"] == 400, \
        f"整组退回:bonus 应=100 · paid 应=400,实际 {w}"


@pytest.mark.asyncio
async def test_split_group_via_paid_leg_also_whole():
    """对称:按 paid 笔(非首笔)的 charge_tx_id 退,仍退整组 500(order_id 反查不依赖是哪一腿)。"""
    uid = 991202
    with _conn() as c:
        cur = c.cursor()
        tx_bonus, tx_paid, feature = _seed_split(cur, uid)
        c.commit()
    res = await _refund(uid, feature, charge_tx_id=tx_paid)
    assert res.get("success") and res.get("refunded") == 500, \
        f"按 paid 腿退也应退整组 500,实际 {res}"


@pytest.mark.asyncio
async def test_deduct_points_returns_charge_tx_id():
    """finding 1 · deduct 部分:deduct_points 必须返回不可变 charge_tx_id 且指向真实 consume 笔。
    判别性:删 return dict 里的 'charge_tx_id' 键 → 断言失败。"""
    uid = 991203
    feature = "_nogo_deduct_feat"
    with _conn() as c:
        cur = c.cursor()
        _reset(cur, uid)
        cur.execute("INSERT INTO users (id, username) VALUES (%s,%s)", (uid, f"u{uid}"))
        cur.execute("INSERT INTO user_wallets (user_id, paid_points, bonus_points, commission_points, deduction_preference) "
                    "VALUES (%s,1000,0,0,'default')", (uid,))
        cur.execute("INSERT INTO feature_pricing (feature_code, cost_points, feature_name) VALUES (%s,%s,%s) "
                    "ON CONFLICT (feature_code) DO UPDATE SET cost_points=EXCLUDED.cost_points", (feature, 100, feature))
        c.commit()
    from middleware.billing import deduct_points
    res = await deduct_points(uid, feature)
    assert res.get("charge_tx_id") is not None, f"deduct_points 必须返回 charge_tx_id,实际 {res}"
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT type FROM point_transactions WHERE id=%s AND user_id=%s", (res["charge_tx_id"], uid))
        row = cur.fetchone()
    assert row and row["type"] == "consume", "charge_tx_id 应指向真实 consume 笔"


def test_article_gen_wires_charge_tx_id_to_refunds():
    """[Deploy-CTO finding 1 · "生产退款调用也没有传它"] 源码级兜底:article_gen 批必须捕获
    deduct 的 charge_tx_id 并透传到部分/全额退款(防有人移除接线 → 回退 newest-by-feature 退错批)。"""
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    assert '_ag_charge_txid = (_ag_charge or {}).get("charge_tx_id")' in server, \
        "article_gen 扣费必须捕获 charge_tx_id"
    # 部分退款 + 全额退款两处都必须透传 charge_tx_id
    assert server.count("charge_tx_id=_ag_charge_txid") >= 2, \
        "article_gen 部分退款 + 全额失败退款都必须透传 charge_tx_id"


@pytest.mark.asyncio
async def test_charge_then_precise_refund_end_to_end():
    """端到端:deduct 得 charge_tx_id → refund(charge_tx_id=…) 退回全额,钱包守恒复原。"""
    uid = 991204
    feature = "_nogo_e2e_feat"
    with _conn() as c:
        cur = c.cursor()
        _reset(cur, uid)
        cur.execute("INSERT INTO users (id, username) VALUES (%s,%s)", (uid, f"u{uid}"))
        cur.execute("INSERT INTO user_wallets (user_id, paid_points, bonus_points, commission_points, deduction_preference) "
                    "VALUES (%s,300,0,0,'default')", (uid,))
        cur.execute("INSERT INTO feature_pricing (feature_code, cost_points, feature_name) VALUES (%s,%s,%s) "
                    "ON CONFLICT (feature_code) DO UPDATE SET cost_points=EXCLUDED.cost_points", (feature, 300, feature))
        c.commit()
    from middleware.billing import deduct_points
    charge = await deduct_points(uid, feature)
    res = await _refund(uid, feature, charge_tx_id=charge["charge_tx_id"])
    assert res.get("success") and res.get("refunded") == 300, f"精确退应退 300,实际 {res}"
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT paid_points FROM user_wallets WHERE user_id=%s", (uid,))
        assert cur.fetchone()["paid_points"] == 300, "扣 300 再精确退 300 → 钱包复原 300(守恒)"
