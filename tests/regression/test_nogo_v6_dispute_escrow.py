"""[v6 req2 + v7 finding1 · P0] dispute_hold escrow 生命周期 + 裁决结算走【唯一 canonical 链】· 行为测试。

老板决策:keep_old→结算X · reassign→结算Y · reject→退款客户 · hold 下单即冻结进 escrow(反扣 user_wallets 不可消费)。

[v7 finding1 · P0 铁律] 裁决结算【禁止手写第二套资金算法】· 必须复用 record_v35_core_settlement:
  purchase_auto_and_allocate → allocate_credit → calc_settlement → insert_revenue_ledger → settlement_snapshot。
判别性(逐字段守恒 · 用【生产真实 schema】):
  - test_settle_with_snapshot_perfield_conservation:snapshot tool=500/publish=800/wholesale=6150 →
    裁决后客户额度 tool=500 / publish=800(非旧 v6 的 tool=1300/publish=0)· factory=6150 锁价(非重算 924/800)·
    结算=客户付款−6150 · 库存双流水 4 条 · 额度分轨 3 条。删回手写第二套算法 → 本测试转红。
  - test_hold_reverses_wallet_unconsumable:删 create_escrow 反扣 → 转红。
"""
from __future__ import annotations
import sys
from pathlib import Path
import pytest
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _dbsafe import resolve_test_db_url, require_destructive_allowed, _assert_safe_test_db  # noqa: E402
from _v35_settle_schema import provision_v35_settlement_schema, SETTLE_TABLES  # noqa: E402

DB = resolve_test_db_url()
pytestmark = pytest.mark.skipif(not DB, reason="需 TEST_DATABASE_URL(本机 test/throwaway 库)")

CUST, AGENT_X, AGENT_Y = 6101, 5101, 5102
BASE, BONUS, CENTS = 1300, 200, 10000


def _conn():
    c = psycopg2.connect(DB); c.cursor_factory = psycopg2.extras.RealDictCursor
    return c


@pytest.fixture(autouse=True)
def _schema():
    require_destructive_allowed(); _assert_safe_test_db(DB)
    with _conn() as c:
        c.autocommit = True
        cur = c.cursor()
        provision_v35_settlement_schema(cur)
        for t in SETTLE_TABLES:
            cur.execute(f"DELETE FROM {t}")
        for uid in (CUST, AGENT_X, AGENT_Y):
            cur.execute("INSERT INTO users (id, username) VALUES (%s,%s) ON CONFLICT (id) DO NOTHING", (uid, f"u{uid}"))
        # 客户已入账 base+bonus(complete_recharge 主事务先入账)
        cur.execute("INSERT INTO user_wallets (user_id, paid_points, bonus_points) VALUES (%s,%s,%s) "
                    "ON CONFLICT (user_id) DO UPDATE SET paid_points=EXCLUDED.paid_points, bonus_points=EXCLUDED.bonus_points",
                    (CUST, BASE, BONUS))
    yield
    with _conn() as c:
        c.autocommit = True
        cur = c.cursor()
        for t in SETTLE_TABLES:
            cur.execute(f"DELETE FROM {t}")


def _wallet():
    with _conn() as c:
        cur = c.cursor(); cur.execute("SELECT paid_points, bonus_points FROM user_wallets WHERE user_id=%s", (CUST,))
        return cur.fetchone()


def _credit():
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT agent_user_id, tool_credit_points, publish_credit_points, bonus_credit_points "
                    "FROM customer_agent_credit_wallets WHERE customer_user_id=%s", (CUST,))
        return cur.fetchone()


def _ledger(agent):
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT customer_paid_cents, factory_cents, agent_settlement_cents, COUNT(*) OVER() AS n "
                    "FROM agent_revenue_ledger WHERE agent_user_id=%s", (agent,))
        return cur.fetchone()


def _escrow_status(oid):
    with _conn() as c:
        cur = c.cursor(); cur.execute("SELECT status, resolved_agent_user_id FROM dispute_escrow WHERE order_id=%s", (oid,))
        return cur.fetchone()


def _count(table, where, params):
    with _conn() as c:
        cur = c.cursor(); cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params)
        return cur.fetchone()["n"]


def _hold(order_id="ord-1", dispute_id=901, snapshot=None, base=BASE, bonus=BONUS, cents=CENTS):
    from db.dispute_escrow_db import create_escrow
    with _conn() as c:
        cur = c.cursor()
        # 建对应 recharge_orders 行(write_order_snapshot 目标)
        cur.execute("INSERT INTO recharge_orders (id, user_id, amount_cents, base_points, bonus_points, settlement_mode) "
                    "VALUES (%s,%s,%s,%s,%s,'dispute_hold') ON CONFLICT (id) DO NOTHING",
                    (order_id, CUST, cents, base, bonus))
        eid = create_escrow(cur, order_id=order_id, dispute_id=dispute_id, customer_user_id=CUST,
                            order_agent_user_id=AGENT_Y, bound_agent_user_id=AGENT_X,
                            amount_cents=cents, base_points=base, bonus_points=bonus, pricing_snapshot=snapshot)
        c.commit(); return eid


def test_hold_reverses_wallet_unconsumable():
    """🔴 hold 下单即冻结进 escrow:反扣 user_wallets → 客户争议期消费不到(已消费竞态天然防)。"""
    _hold()
    w = _wallet()
    assert w["paid_points"] == 0 and w["bonus_points"] == 0, "🔴 hold 后 user_wallets 必须被反扣(不可消费)"
    assert _escrow_status("ord-1")["status"] == "held"


def test_hold_idempotent_no_double_reverse():
    _hold("ord-2")
    from db.dispute_escrow_db import create_escrow
    with _conn() as c:
        cur = c.cursor()
        cur.execute("UPDATE user_wallets SET paid_points=500 WHERE user_id=%s", (CUST,)); c.commit()
        r = create_escrow(cur, order_id="ord-2", dispute_id=901, customer_user_id=CUST,
                          order_agent_user_id=AGENT_Y, bound_agent_user_id=AGENT_X,
                          amount_cents=CENTS, base_points=BASE, bonus_points=BONUS)
        c.commit()
    assert r is None, "同 order 重复 create → no-op"
    assert _wallet()["paid_points"] == 500, "幂等:不重复反扣"


def test_settle_with_snapshot_perfield_conservation_and_locked_factory():
    """🔴🔴 [P0 核心判别] 裁决结算继承快照分轨 + 锁价 factory · 逐字段守恒 · 走 canonical 链。

    snapshot tool=500/publish=800/bonus=200/wholesale=6150/customer_paid=10000 · keep_old→X:
      - 客户额度 tool=500 · publish=800 · bonus=200(非旧 v6 的 tool=1500/publish=0)
      - ledger factory=6150(快照锁价 · 非重算 ceil(1500*225/325)=1039)· settlement=10000−6150=3850
      - 库存双流水 4 条 · 额度分轨 3 条 · escrow settled
    删回手写第二套(dump tool + 硬编码全局出厂系数)→ tool!=500 或 factory!=6150 → 转红。
    """
    snap = {"tool_points": 500, "publish_points": 800, "bonus_points": 200,
            "wholesale_cents": 6150, "customer_paid_cents": 10000, "points_granted": 1500,
            "payment_method": "wechat_pay"}
    _hold("ord-p0", snapshot=snap)
    from db.dispute_escrow_db import settle_escrow, list_held_for_dispute
    with _conn() as c:
        cur = c.cursor()
        held = list_held_for_dispute(cur, 901)
        assert len(held) == 1
        assert settle_escrow(cur, held[0], AGENT_X, admin_id=1) is True
        c.commit()

    cr = _credit()
    assert cr["agent_user_id"] == AGENT_X
    assert cr["tool_credit_points"] == 500, f"🔴 tool 必须=500(继承快照分轨)· 实得 {cr['tool_credit_points']}"
    assert cr["publish_credit_points"] == 800, f"🔴 publish 必须=800(非全塞 tool)· 实得 {cr['publish_credit_points']}"
    assert cr["bonus_credit_points"] == 200

    lg = _ledger(AGENT_X)
    assert lg is not None and lg["n"] == 1, "X 得该单收益 1 条"
    assert lg["factory_cents"] == 6150, f"🔴 factory 必须=快照锁价 6150(非重算)· 实得 {lg['factory_cents']}"
    assert lg["customer_paid_cents"] == 10000
    assert lg["agent_settlement_cents"] == 10000 - 6150, f"🔴 结算=客户付款−factory=3850 · 实得 {lg['agent_settlement_cents']}"

    # 库存双流水(purchase_auto + allocate · paid + bonus 各 2 条)+ 额度分轨(tool/publish/bonus 各 1 条)
    assert _count("agent_inventory_transactions", "agent_user_id=%s", (AGENT_X,)) == 4, "🔴 库存双流水缺失(手写算法无库存流水)"
    assert _count("customer_credit_transactions", "customer_user_id=%s AND type='allocate'", (CUST,)) == 3, "🔴 额度分轨 3 轨流水缺失"

    assert _escrow_status("ord-p0")["status"] == "settled"
    # 守恒:escrow 释放(base+bonus=1500)== 客户额度总入账(500+800+200=1500)
    assert cr["tool_credit_points"] + cr["publish_credit_points"] + cr["bonus_credit_points"] == BASE + BONUS


def test_settle_keep_old_to_X_no_snapshot():
    """无快照(老单)· keep_old→X:base 全入 tool(publish=0)· ledger 1 条 · 守恒(向后兼容)。"""
    _hold("ord-3")
    from db.dispute_escrow_db import settle_escrow, list_held_for_dispute
    with _conn() as c:
        cur = c.cursor()
        assert settle_escrow(cur, list_held_for_dispute(cur, 901)[0], AGENT_X, admin_id=1) is True; c.commit()
    assert _escrow_status("ord-3")["status"] == "settled"
    cr = _credit()
    assert cr["agent_user_id"] == AGENT_X and cr["tool_credit_points"] == BASE and cr["bonus_credit_points"] == BONUS
    assert cr["publish_credit_points"] == 0
    assert _ledger(AGENT_X)["n"] == 1
    assert cr["tool_credit_points"] + cr["publish_credit_points"] + cr["bonus_credit_points"] == BASE + BONUS


def test_settle_no_wholesale_uses_per_agent_override(monkeypatch):
    """[v7 对抗审 P3] 快照缺 wholesale_cents 时,裁决结算 factory 回落必须用【per-agent 出厂 override】(与充值主路径同一 core 规则),
    非全局出厂系数 —— 消除两路径 factory 口径漂移。"""
    import services.agent_pricing as AP
    import services.agent_pricing_overrides as APO
    monkeypatch.setattr(APO, "get_agent_wholesale_override", lambda aid: (150, 325))  # 该 agent 有 per-agent 折扣
    monkeypatch.setattr(AP, "get_agent_wholesale_ratio", lambda aid: (150, 325))
    snap = {"tool_points": BASE, "publish_points": 0, "bonus_points": BONUS, "customer_paid_cents": 10000}  # 无 wholesale_cents
    _hold("ord-ov", snapshot=snap)
    from db.dispute_escrow_db import settle_escrow, list_held_for_dispute
    with _conn() as c:
        cur = c.cursor()
        settle_escrow(cur, list_held_for_dispute(cur, 901)[0], AGENT_X, admin_id=1); c.commit()
    # factory = ceil((base+bonus)*150/325)(per-agent 比例)· 非全局出厂系数
    expected = ((BASE + BONUS) * 150 + 324) // 325
    lg = _ledger(AGENT_X)
    assert lg["factory_cents"] == expected, f"🔴 缺快照锁价时应用 per-agent override 比例 · 期望 {expected} 实得 {lg['factory_cents']}"


def test_settle_reassign_to_Y():
    _hold("ord-4")
    from db.dispute_escrow_db import settle_escrow, list_held_for_dispute
    with _conn() as c:
        cur = c.cursor()
        settle_escrow(cur, list_held_for_dispute(cur, 901)[0], AGENT_Y, admin_id=1); c.commit()
    assert _credit()["agent_user_id"] == AGENT_Y, "reassign → 结算给 Y"
    assert _ledger(AGENT_Y)["n"] == 1


def test_reject_refund_to_customer_conservation():
    """reject → 退款:escrow −(base+bonus) → user_wallets 恢复 · 客户拿回可消费额度 · 谁都不结算。"""
    _hold("ord-5")
    assert _wallet()["paid_points"] == 0
    from db.dispute_escrow_db import refund_escrow, list_held_for_dispute
    with _conn() as c:
        cur = c.cursor()
        assert refund_escrow(cur, list_held_for_dispute(cur, 901)[0], admin_id=1) is True; c.commit()
    assert _escrow_status("ord-5")["status"] == "refunded"
    w = _wallet()
    assert w["paid_points"] == BASE and w["bonus_points"] == BONUS, "🔴 退款:user_wallets 恢复原额(可消费)"
    assert _credit() is None, "退款 → 无服务商额度(谁都不结算)"


def test_refund_missing_wallet_raises_no_silent_loss():
    """[v6 对抗审 P2] 客户 wallet 行缺失 → refund UPDATE 0 行 → 必须 raise(不坐实 refunded · 防资金蒸发)。"""
    _hold("ord-7")
    with _conn() as c:
        c.cursor().execute("DELETE FROM user_wallets WHERE user_id=%s", (CUST,)); c.commit()
    from db.dispute_escrow_db import refund_escrow, list_held_for_dispute
    with _conn() as c:
        cur = c.cursor()
        esc = list_held_for_dispute(cur, 901)[0]
        with pytest.raises(RuntimeError, match="CRITICAL"):
            refund_escrow(cur, esc, admin_id=1)
        c.rollback()
    assert _escrow_status("ord-7")["status"] == "held", "🔴 wallet 缺失时 escrow 必须保持 held(不静默 refunded)"


def test_duplicate_resolution_noop():
    """重复裁决:settle 后再 settle → 第二次 no-op(不二次结算)。"""
    _hold("ord-6")
    from db.dispute_escrow_db import settle_escrow, list_held_for_dispute
    with _conn() as c:
        cur = c.cursor()
        esc = list_held_for_dispute(cur, 901)[0]
        assert settle_escrow(cur, esc, AGENT_X, 1) is True; c.commit()
    with _conn() as c:
        cur = c.cursor()
        assert settle_escrow(cur, dict(esc), AGENT_Y, 1) is False, "🔴 重复裁决必须 no-op(不二次结算)"; c.commit()
    cr = _credit()
    assert cr["agent_user_id"] == AGENT_X and cr["tool_credit_points"] == BASE, "只结算了一次(给 X)"
    assert _ledger(AGENT_Y) is None, "Y 无收益(未二次结算)"
