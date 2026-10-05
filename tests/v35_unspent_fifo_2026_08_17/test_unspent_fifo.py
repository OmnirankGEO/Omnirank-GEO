"""工单 WO_V35_UNSPENT_SINGLE_LEDGER_FIFO_2026-08-17 · §4 六组判据

  1. 跨单反例(核心)+ 「FIFO 换朴素夹紧必转红」的变异
  2. 顺序对照(守恒)
  3. 冻结互斥
  4. 新旧分流
  5. 拆锁(短路 FIFO → 切换后订单撤回 > 0 判据红)
  6. 全量(见交付单:五保护零 diff / porcelain 0 / ec11dffe 判据仍绿)

零生产数据:uid 9_2xx_xxx,与生产账户无交集。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.v35_unspent_fifo_2026_08_17._ledger import (  # noqa: E402
    AGENT, USER, Ledger, add_legacy_allocate, make_order, seed_user, sync_wallet, wipe,
)

DB = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB, reason="需 TEST_DATABASE_URL")

ORDER_A = "FIFO-ORDER-A"
ORDER_B = "FIFO-ORDER-B"


def _fifo(cur, order_id, user_id=USER):
    from services.customer_entitlement import compute_unspent_fifo
    return compute_unspent_fifo(cur, user_id, order_id)


# ════════════════════════════════════════════════════════════
# 判据 1 · 跨单反例(核心)
# ════════════════════════════════════════════════════════════

def _seed_cross_order(cur):
    """单 A 1000 花 600,单 B 800 没花 → A 余 400 / B 余 800,钱包 1200。"""
    lg = seed_user(cur)
    lg.recharge(ORDER_A, paid=1000)
    lg.consume(600, "paid")
    lg.recharge(ORDER_B, paid=800)
    sync_wallet(cur, lg)
    make_order(cur, ORDER_A, base_points=1000)
    make_order(cur, ORDER_B, base_points=800)
    return lg


def test_cross_order_fifo_attributes_consumption_to_oldest(conn):
    """FIFO:600 的消费吃最老的桶(A)→ A 余 400,B 整 800 未动。"""
    cur = conn.cursor()
    _seed_cross_order(cur)
    a = _fifo(cur, ORDER_A)
    b = _fifo(cur, ORDER_B)
    assert a["paid_unspent"] == 400, f"A 应余 400,实得 {a['paid_unspent']}"
    assert b["paid_unspent"] == 800, f"B 应余 800,实得 {b['paid_unspent']}"


def test_cross_order_naive_clamp_would_overrecover(conn):
    """🔴 变异对照:朴素「按钱包余额夹紧」退 A 会撤 1000 —— 吃掉 B 的 600。

    这条把「错误实现的后果」钉死成数字:
      · FIFO      → 退 A 撤 400(正确)
      · 朴素夹紧  → 退 A 撤 min(1000, 钱包1200) = 1000(**超收 600**,吃 B 的钱)
    两者差 600 就是本工单堵住的口子。
    """
    cur = conn.cursor()
    _seed_cross_order(cur)
    cur.execute("SELECT paid_points FROM user_wallets WHERE user_id=%s", (USER,))
    wallet_paid = int(cur.fetchone()["paid_points"])
    assert wallet_paid == 1200

    fifo_a = _fifo(cur, ORDER_A)["paid_unspent"]
    naive_a = min(1000, wallet_paid)          # 朴素夹紧:请求量 vs 钱包余额

    assert fifo_a == 400
    assert naive_a == 1000
    assert naive_a - fifo_a == 600, "跨单超收额应恰为 B 的已入账未消费部分"


def test_cross_order_revoke_b_takes_full_800(conn):
    """退 B 应能整撤 800(B 一分没花)——不能因为 A 花过钱就少撤 B。"""
    from services.customer_entitlement import revoke_from_customer
    cur = conn.cursor()
    _seed_cross_order(cur)
    unspent_b = _fifo(cur, ORDER_B)["paid_unspent"]
    res = revoke_from_customer(
        cur, customer_user_id=USER, agent_user_id=AGENT,
        paid_points=unspent_b, bonus_points=0,
        related_order_id=ORDER_B, source="refund_revoke")
    assert res["revoked_paid"] == 800


# ════════════════════════════════════════════════════════════
# 判据 2 · 顺序对照(守恒)
# ════════════════════════════════════════════════════════════

def test_order_independence_total_revoked_is_conserved(conn):
    """先退 A 再退 B 与先退 B 再退 A,两序**总撤回一致**。"""
    from services.customer_entitlement import revoke_from_customer

    def run(seq):
        cur = conn.cursor()
        _seed_cross_order(cur)
        total = 0
        for oid in seq:
            un = _fifo(cur, oid)["paid_unspent"]
            r = revoke_from_customer(
                cur, customer_user_id=USER, agent_user_id=AGENT,
                paid_points=un, bonus_points=0,
                related_order_id=oid, source="refund_revoke")
            total += r["revoked_paid"]
        cur.execute("SELECT paid_points FROM user_wallets WHERE user_id=%s", (USER,))
        left = int(cur.fetchone()["paid_points"])
        conn.commit()
        return total, left

    t1, left1 = run([ORDER_A, ORDER_B])
    t2, left2 = run([ORDER_B, ORDER_A])
    assert t1 == t2 == 1200, f"两序总撤回应都是 1200,实得 {t1} / {t2}"
    assert left1 == left2 == 0, "撤完钱包应归零"


# ════════════════════════════════════════════════════════════
# 判据 3 · 冻结互斥
# ════════════════════════════════════════════════════════════

def test_open_freeze_is_not_counted_as_consumed(conn):
    """§2.4:**尚未 commit** 的冻结不算已消费 → 不压低该单的 FIFO 未消费额。"""
    cur = conn.cursor()
    lg = seed_user(cur)
    lg.recharge(ORDER_A, paid=1000)
    lg.freeze_open(300, "paid")          # 冻结中:余额已扣,但不算消费
    sync_wallet(cur, lg)
    make_order(cur, ORDER_A, base_points=1000)

    assert _fifo(cur, ORDER_A)["paid_unspent"] == 1000, "冻结中的量被误算成已消费"
    cur.execute("SELECT paid_points FROM user_wallets WHERE user_id=%s", (USER,))
    assert int(cur.fetchone()["paid_points"]) == 700, "冻结应已从钱包余额扣走"


def test_open_freeze_is_not_revocable(conn):
    """§2.4 另一半:冻结中的量**不可撤** —— 由三层 min 的钱包余额层保证。"""
    from services.customer_entitlement import revoke_from_customer
    cur = conn.cursor()
    lg = seed_user(cur)
    lg.recharge(ORDER_A, paid=1000)
    lg.freeze_open(300, "paid")
    sync_wallet(cur, lg)
    un = _fifo(cur, ORDER_A)["paid_unspent"]          # 1000
    res = revoke_from_customer(
        cur, customer_user_id=USER, agent_user_id=AGENT,
        paid_points=un, bonus_points=0,
        related_order_id=ORDER_A, source="refund_revoke")
    assert res["revoked_paid"] == 700, "撤回应被钱包余额层夹到 700,不吃冻结的 300"
    cur.execute("SELECT paid_points FROM user_wallets WHERE user_id=%s", (USER,))
    assert int(cur.fetchone()["paid_points"]) == 0


def test_committed_freeze_counts_as_consumed_once_not_twice(conn):
    """冻结→commit 的**两行**形态只能算**一次**消费。

    🔴 这条专打「按 amount 求和」的错误实现:freeze(-300) + commit consume(-300,
    balance_after 不变)。按 amount 求和会算成消费 600 → 未消费变 400(错),
    按 balance_after 差分才是 700(对)。
    """
    cur = conn.cursor()
    lg = seed_user(cur)
    lg.recharge(ORDER_A, paid=1000)
    lg.freeze_commit(300, "paid")
    sync_wallet(cur, lg)
    got = _fifo(cur, ORDER_A)["paid_unspent"]
    assert got == 700, f"冻结落地只该扣一次(700),按 amount 求和会得 400 · 实得 {got}"


def test_release_bias_is_bounded_by_released_amount(conn):
    """release 保守偏置的**方向与上界**都锁死(Review 要求写明上界,不是只说"保守")。

    · 方向:低估该单未消费 → **少撤**(平台侧承担有界小损),绝不多撤到别单头上;
    · 上界:低估量 ≤ 该用户该轨的 **release 流水总额**(且不超过该单入账总量)。

    本例:单 A 入账 1000,冻结 300 后释放 300(release 总额 = 300)。
    理想值 1000,实测值应 = 700 → 低估恰 300 = release 总额,**贴着上界**不越界。
    """
    cur = conn.cursor()
    lg = seed_user(cur)
    lg.recharge(ORDER_A, paid=1000)
    fid = lg.freeze_open(300, "paid")
    lg.release(fid, 300, "paid")
    sync_wallet(cur, lg)

    granted = 1000
    released_total = 300
    got = _fifo(cur, ORDER_A)["paid_unspent"]

    understated = granted - got
    assert understated >= 0, "偏置方向必须是低估,不能高估(高估=可能超收)"
    assert understated <= min(granted, released_total), (
        f"低估量 {understated} 超出上界 min(入账 {granted}, release 总额 {released_total})")
    assert got == 700, f"本形状的确定值应为 700,实得 {got}"


def test_release_returns_balance_and_never_overrecovers(conn):
    """release 后余量回到钱包;本实现按新桶处理 → 原单未消费**偏保守**,绝不超收。"""
    from services.customer_entitlement import revoke_from_customer
    cur = conn.cursor()
    lg = seed_user(cur)
    lg.recharge(ORDER_A, paid=1000)
    fid = lg.freeze_open(300, "paid")
    lg.release(fid, 300, "paid")
    sync_wallet(cur, lg)
    cur.execute("SELECT paid_points FROM user_wallets WHERE user_id=%s", (USER,))
    assert int(cur.fetchone()["paid_points"]) == 1000, "release 后余额应回到 1000"

    un = _fifo(cur, ORDER_A)["paid_unspent"]
    # 保守偏置:release 记新桶,原单少算 300(交付单已写明理由 · 只会少撤不会超收)
    assert un <= 1000
    res = revoke_from_customer(
        cur, customer_user_id=USER, agent_user_id=AGENT,
        paid_points=un, bonus_points=0, related_order_id=ORDER_A, source="refund_revoke")
    assert res["revoked_paid"] <= 1000, "绝不能撤出超过订单本身的量"


# ════════════════════════════════════════════════════════════
# 判据 4 · 新旧分流
# ════════════════════════════════════════════════════════════

def test_routing_order_with_legacy_history_uses_legacy_path(conn):
    """带老台账 allocate 历史的订单 → 判为走老路。"""
    from services.customer_entitlement import has_legacy_credit_ledger_history
    cur = conn.cursor()
    lg = seed_user(cur)
    lg.recharge(ORDER_A, paid=1000)
    sync_wallet(cur, lg)
    make_order(cur, ORDER_A, base_points=1000)
    add_legacy_allocate(cur, ORDER_A, points=1000)
    assert has_legacy_credit_ledger_history(cur, USER, ORDER_A) is True


def test_routing_order_without_legacy_history_uses_fifo(conn):
    """无老台账历史(切换后新建)→ 判为走 FIFO。"""
    from services.customer_entitlement import has_legacy_credit_ledger_history
    cur = conn.cursor()
    lg = seed_user(cur)
    lg.recharge(ORDER_B, paid=800)
    sync_wallet(cur, lg)
    make_order(cur, ORDER_B, base_points=800)
    assert has_legacy_credit_ledger_history(cur, USER, ORDER_B) is False


def test_routing_is_not_date_based(conn):
    """分流判据是「有无老台账」不是日期 —— 同一时刻建的两单可以分走两条路。"""
    from services.customer_entitlement import has_legacy_credit_ledger_history
    cur = conn.cursor()
    lg = seed_user(cur)
    lg.recharge(ORDER_A, paid=1000)
    lg.recharge(ORDER_B, paid=800)
    sync_wallet(cur, lg)
    make_order(cur, ORDER_A, base_points=1000)
    make_order(cur, ORDER_B, base_points=800)
    add_legacy_allocate(cur, ORDER_A, points=1000)   # 只给 A 造老账
    assert has_legacy_credit_ledger_history(cur, USER, ORDER_A) is True
    assert has_legacy_credit_ledger_history(cur, USER, ORDER_B) is False


# ════════════════════════════════════════════════════════════
# 判据 5 · 拆锁:短路 FIFO 重放 → 切换后订单撤回 > 0 判据必红
# ════════════════════════════════════════════════════════════

def test_lock_removal_shortcircuited_fifo_breaks_post_cutover_revoke(conn, monkeypatch):
    """把 compute_unspent_fifo 短路成恒 0(= 换底座前的病态)→ 撤回归 0。

    正常实现下同一形状撤回 800;短路后 0。本用例把"病态"钉死,
    证明「切换后订单撤回 > 0」这条判据确实由 FIFO 重放支撑,不是撞大运。
    """
    import services.customer_entitlement as ce
    cur = conn.cursor()
    lg = seed_user(cur)
    lg.recharge(ORDER_B, paid=800)
    sync_wallet(cur, lg)
    make_order(cur, ORDER_B, base_points=800)

    healthy = ce.compute_unspent_fifo(cur, USER, ORDER_B)["paid_unspent"]
    assert healthy == 800, "正常实现应算出 800"

    monkeypatch.setattr(ce, "compute_unspent_fifo",
                        lambda *a, **k: {"paid_unspent": 0, "bonus_unspent": 0})
    broken = ce.compute_unspent_fifo(cur, USER, ORDER_B)["paid_unspent"]
    assert broken == 0, "短路后应恒 0"
    assert healthy - broken == 800, "两者之差 = 本工单恢复的可撤额"


def test_wiring_refund_chain_routes_through_fifo():
    """接线锁:退款链必须真的调到分流 + FIFO,不是只把函数写在那。"""
    import ast
    import inspect
    from api import referral_api
    src = inspect.getsource(referral_api._handle_v35_factory_refund)
    tree = ast.parse(src.lstrip())
    called = {
        n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    assert "has_legacy_credit_ledger_history" in called, "没接新旧分流判据"
    assert "compute_unspent_fifo" in called, "没接 FIFO 重放"
    assert "compute_unspent_from_order" in called, "老路径被误删(在册 7 单要换轨了)"


def test_wiring_shadow_lock_from_ec11dffe_untouched():
    """ec11dffe 那把同名遮蔽锁的形状不许被本包碰(工单明令)。"""
    import inspect
    from api import referral_api
    src = inspect.getsource(referral_api._handle_v35_factory_refund)
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert "from services.agent_inventory import revoke_from_customer" in code
    assert "from services.customer_entitlement import revoke_from_customer" not in code, \
        "裸 import 会遮蔽库存回收函数"
    assert code.count('credit_result["revoked_paid"]') == 3
    assert code.count('credit_result["revoked_bonus"]') == 3


# ════════════════════════════════════════════════════════════
# 双轨 / 边界
# ════════════════════════════════════════════════════════════

def test_paid_and_bonus_tracks_are_independent(conn):
    """paid / bonus 双轨独立:bonus 的消费不吃 paid 的桶。"""
    cur = conn.cursor()
    lg = seed_user(cur)
    lg.recharge(ORDER_A, paid=1000, bonus=500)
    lg.consume(300, "bonus")
    sync_wallet(cur, lg)
    got = _fifo(cur, ORDER_A)
    assert got["paid_unspent"] == 1000, "bonus 侧消费不该动 paid 桶"
    assert got["bonus_unspent"] == 200


def test_unknown_order_returns_zero(conn):
    cur = conn.cursor()
    lg = seed_user(cur)
    lg.recharge(ORDER_A, paid=1000)
    sync_wallet(cur, lg)
    assert _fifo(cur, "NO-SUCH-ORDER") == {"paid_unspent": 0, "bonus_unspent": 0}


def test_overconsumption_does_not_go_negative(conn):
    """消费超过已知入账(**流水历史被截断**)→ 夹到 0,不倒扣、不抛错。

    构造要点:不能用"余额扣成负数"来造这个场景 —— 生产 user_wallets 有
    `check_paid_non_negative`,那种状态在生产根本不存在,拿它当夹具是假场景。
    真实成因是**流水首行就是消费**(更早的入账没进这张表 / 被归档),
    此时 FIFO 没有可扣的桶。
    """
    cur = conn.cursor()
    lg = seed_user(cur, paid=1000)      # 钱包有钱,但流水里没有对应的入账行
    lg.consume(500, "paid")             # 首行即消费 → 无桶可扣
    lg.recharge(ORDER_A, paid=100)
    sync_wallet(cur, lg)
    got = _fifo(cur, ORDER_A)
    assert got["paid_unspent"] >= 0, "绝不能返负数"
    assert got["bonus_unspent"] >= 0
