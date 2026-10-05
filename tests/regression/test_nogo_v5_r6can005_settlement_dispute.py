"""[v5 req2] GEO-R6-CAN-005 · 结算归属冲突 dispute_hold · PG 判别性行为测试。

覆盖:
- 服务商 X/Y 并发绑定:客户先绑 X,再来 Y → upsert 返回 dispute_marked,binding 保留 X + dispute_status=pending;
- 订单 agent=Y 与最终绑定服务商 X 不一致 → SettlementOrchestrator.route() 返 'dispute_hold';
- 三账守恒:dispute_hold 后【不写 agent_revenue_ledger、不划 customer_agent_credit_wallets、只把订单标 hold】;
- 归属一致(order.agent == binding.agent)不触发 hold。

判别性:去掉 route() 的 _attribution_conflict / _hold_for_dispute 守卫 →
  route 会走 _record_factory_settlement(V3.5 锁定单)→ 需 agent 库存/收益表 → 抛错 / 写行 → 本测试失败。
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

DB = resolve_test_db_url()
pytestmark = pytest.mark.skipif(not DB, reason="需 TEST_DATABASE_URL(本机 test/throwaway 库)")

C_USER, AGENT_X, AGENT_Y = 9001, 8001, 8002


def _conn():
    c = psycopg2.connect(DB); c.cursor_factory = psycopg2.extras.RealDictCursor
    return c


@pytest.fixture(autouse=True)
def _schema():
    require_destructive_allowed(); _assert_safe_test_db(DB)
    with _conn() as c:
        cur = c.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT)")
        cur.execute("""CREATE TABLE IF NOT EXISTS customer_agent_bindings (
            id BIGSERIAL PRIMARY KEY, customer_user_id INTEGER NOT NULL UNIQUE,
            agent_user_id INTEGER NOT NULL, binding_source TEXT, source_token TEXT,
            bound_at TIMESTAMPTZ DEFAULT NOW(), dispute_status TEXT, dispute_note TEXT)""")
        # customer_agent_bindings 可能被别的测试/init 先建成缺列版(CREATE IF NOT EXISTS no-op)→ 防御 ALTER 补齐本测试需要的列
        for _cb, _ct in (("source_token", "TEXT"), ("binding_source", "TEXT"),
                         ("dispute_status", "TEXT"), ("dispute_note", "TEXT")):
            cur.execute(f"ALTER TABLE customer_agent_bindings ADD COLUMN IF NOT EXISTS {_cb} {_ct}")
        cur.execute("""CREATE TABLE IF NOT EXISTS recharge_orders (
            id TEXT PRIMARY KEY, user_id INTEGER, agent_user_id INTEGER,
            order_type TEXT, sku_template_id INTEGER, binding_source TEXT, source_token TEXT,
            amount_cents INTEGER DEFAULT 0, base_points INTEGER DEFAULT 0, bonus_points INTEGER DEFAULT 0,
            payment_method TEXT, settlement_mode TEXT, pricing_snapshot_jsonb JSONB)""")
        # recharge_orders 可能被别的测试先建成缺列版 → ALTER 补齐本测试需要的列(CREATE IF NOT EXISTS 对已存在表是 no-op)
        for _c, _t in (("agent_user_id","INTEGER"),("sku_template_id","INTEGER"),("binding_source","TEXT"),
                       ("source_token","TEXT"),("base_points","INTEGER"),("bonus_points","INTEGER"),
                       ("amount_cents","INTEGER"),("settlement_mode","TEXT"),("payment_method","TEXT"),
                       ("order_type","TEXT"),("user_id","INTEGER"),("pricing_snapshot_jsonb","JSONB")):
            cur.execute(f"ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS {_c} {_t}")
        # 结算副作用表(空 · 守恒断言:dispute_hold 后仍 0 行)· 全列版(与 escrow settle 用的一致 · 防跨测试缺列碰撞)
        cur.execute("CREATE TABLE IF NOT EXISTS agent_revenue_ledger (id BIGSERIAL PRIMARY KEY, agent_user_id INTEGER, "
                    "customer_user_id INTEGER, recharge_order_id TEXT, customer_paid_cents INTEGER)")
        cur.execute("CREATE TABLE IF NOT EXISTS customer_agent_credit_wallets (customer_user_id INTEGER PRIMARY KEY, agent_user_id INTEGER)")
        cur.execute("CREATE TABLE IF NOT EXISTS referral_links (referred_id INTEGER, level INTEGER)")
        # [v6] dispute_hold 现会建 escrow(+ 对真 user_wallets 反扣 · C_USER 无 wallet 行 → no-op · 不影响守恒断言)
        from db.dispute_escrow_db import init_dispute_escrow_tables
        init_dispute_escrow_tables(cur)
        for t in ("customer_agent_bindings", "recharge_orders", "agent_revenue_ledger",
                  "customer_agent_credit_wallets", "referral_links", "dispute_escrow"):
            cur.execute(f"DELETE FROM {t}")
        # users 桩(recharge_orders/user_wallets 可能带 FK → users)
        for uid in (C_USER, AGENT_X, AGENT_Y):
            try:
                cur.execute("INSERT INTO users (id, username) VALUES (%s,%s) ON CONFLICT (id) DO NOTHING", (uid, f"u{uid}"))
            except Exception:
                c.rollback()
        cur.execute("ALTER TABLE user_wallets ADD COLUMN IF NOT EXISTS agent_level INTEGER DEFAULT 0")
        cur.execute(
            """INSERT INTO user_wallets(user_id,agent_level)
               VALUES (%s,0),(%s,1),(%s,1)
               ON CONFLICT(user_id) DO UPDATE SET agent_level=EXCLUDED.agent_level""",
            (C_USER, AGENT_X, AGENT_Y),
        )
        cur.execute("INSERT INTO system_settings (key,value) VALUES ('V35_FACTORY_INVENTORY_ENABLED','true') "
                    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value")
        cur.execute("INSERT INTO system_settings (key,value) VALUES ('LEGACY_REFERRAL_V32_ENABLED','disabled') "
                    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value")
        c.commit()
    yield
    # [v6 修] 只 DELETE 数据 · 不 DROP 共享表(customer_agent_credit_wallets/recharge_orders 等被别的测试用 · DROP 会连累它们)
    with _conn() as c:
        cur = c.cursor()
        for t in ("customer_agent_bindings", "recharge_orders", "agent_revenue_ledger",
                  "customer_agent_credit_wallets", "referral_links", "dispute_escrow"):
            try:
                cur.execute(f"DELETE FROM {t}")
            except Exception:
                c.rollback()
        cur.execute("DELETE FROM user_wallets WHERE user_id IN (%s,%s,%s)", (C_USER, AGENT_X, AGENT_Y))
        c.commit()


def _insert_order(cur, oid, agent_id):
    cur.execute("""INSERT INTO recharge_orders
        (id, user_id, agent_user_id, order_type, sku_template_id, binding_source, amount_cents, base_points, bonus_points)
        VALUES (%s,%s,%s,'customer_recharge',777,'ref_link',10000,1300,0)""", (oid, C_USER, agent_id))


def _order_dict(oid, agent_id):
    return {"id": oid, "user_id": C_USER, "agent_user_id": agent_id,
            "order_type": "customer_recharge", "sku_template_id": 777,
            "binding_source": "ref_link", "source_token": None,
            "amount_cents": 10000, "base_points": 1300, "bonus_points": 0,
            "payment_method": "wechat_pay"}


def test_attribution_conflict_helper():
    from services.settlement_orchestrator import SettlementOrchestrator
    f = SettlementOrchestrator._attribution_conflict
    assert f(AGENT_Y, {"agent_user_id": AGENT_X}) is True, "order Y ≠ 绑定 X → 冲突"
    assert f(AGENT_X, {"agent_user_id": AGENT_X}) is False, "order X == 绑定 X → 不冲突"
    assert f(None, {"agent_user_id": AGENT_X}) is False, "无订单归属 → 不冲突"
    assert f(AGENT_Y, None) is False, "无绑定 → 不冲突(交常规路由)"


def test_concurrent_xy_binding_marks_dispute_keeps_original():
    """服务商 X/Y 并发绑定:先 X 后 Y → Y 返 dispute_marked · binding 保留 X · dispute_status=pending。"""
    from services.customer_binding import upsert_customer_agent_binding, get_customer_binding
    with _conn() as c:
        cur = c.cursor()
        r1 = upsert_customer_agent_binding(
            cur, customer_user_id=C_USER, agent_user_id=AGENT_X,
            binding_source="admin_manual", source_token="explicit-service-x",
        )
        assert r1["action"] == "inserted"
        r2 = upsert_customer_agent_binding(
            cur, customer_user_id=C_USER, agent_user_id=AGENT_Y,
            binding_source="admin_manual", source_token="explicit-service-y",
        )
        assert r2["action"] == "dispute_marked", f"冲突应标 dispute_marked: {r2}"
        assert r2["agent_user_id"] == AGENT_X, "最终绑定必须保留原代理 X(不覆盖成 Y)"
        b = get_customer_binding(cur, C_USER)
        assert b["agent_user_id"] == AGENT_X and b["dispute_status"] == "pending"
        c.commit()


def test_mismatch_order_returns_dispute_hold_and_conserves():
    """订单 agent=Y ≠ 绑定 X → route()='dispute_hold' · 三账守恒(收益/客户额度 0 行 · 订单标 hold)。"""
    from services.customer_binding import upsert_customer_agent_binding
    from services.settlement_orchestrator import SettlementOrchestrator
    with _conn() as c:
        cur = c.cursor()
        upsert_customer_agent_binding(
            cur, customer_user_id=C_USER, agent_user_id=AGENT_X,
            binding_source="admin_manual", source_token="explicit-service-x",
        )
        _insert_order(cur, "ord-Y-1", AGENT_Y)
        c.commit()
        mode = SettlementOrchestrator().route(cur, order=_order_dict("ord-Y-1", AGENT_Y),
                                              user={"user_id": C_USER, "id": C_USER}, pricing_snapshot=None)
        c.commit()
        assert mode == "dispute_hold", f"归属冲突订单必须 dispute_hold,实际 {mode}"
        cur.execute("SELECT settlement_mode FROM recharge_orders WHERE id='ord-Y-1'")
        assert cur.fetchone()["settlement_mode"] == "dispute_hold"
        # 🔴 三账守恒:不写 agent_revenue_ledger · 不划 customer_agent_credit_wallets
        cur.execute("SELECT COUNT(*) AS n FROM agent_revenue_ledger")
        assert cur.fetchone()["n"] == 0, "dispute_hold 不得写代理收益 ledger"
        cur.execute("SELECT COUNT(*) AS n FROM customer_agent_credit_wallets")
        assert cur.fetchone()["n"] == 0, "dispute_hold 不得划客户额度"


def test_matching_order_agent_not_held():
    """订单 agent=X == 绑定 X → 不进 dispute_hold(交常规 V3.5 路由 · 此处只验守卫不误伤)。"""
    from services.customer_binding import upsert_customer_agent_binding
    from services.settlement_orchestrator import SettlementOrchestrator
    with _conn() as c:
        cur = c.cursor()
        upsert_customer_agent_binding(
            cur, customer_user_id=C_USER, agent_user_id=AGENT_X,
            binding_source="admin_manual", source_token="explicit-service-x",
        )
        c.commit()
        # 直接验守卫:一致不冲突(route 一致路径会进 _record_factory_settlement · 需重表,不在本例跑全)
        from services.customer_binding import get_customer_binding
        b = get_customer_binding(cur, C_USER)
        assert SettlementOrchestrator._attribution_conflict(AGENT_X, b) is False
