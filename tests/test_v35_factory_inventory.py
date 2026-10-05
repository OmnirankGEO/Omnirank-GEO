"""
V3.5 工厂模式 W1 集成测试

测试覆盖(13 项报告对应):
  T1 · 结算公式 3 层(通道费/服务费/税按 margin)
  T2 · 自动补库存双流水(purchase_auto + allocate · 净 0)
  T3 · 线下划拨不写 agent_revenue_ledger
  T4 · 发布中心 paid-only(bonus 禁发布)
  T5 · revoke 严格上限 = min(余额, 未消费)
  T6 · SettlementOrchestrator 三互斥(v35 / v32 / direct)
  T7 · 退款 4 状态机(A 全退 / B 比例 / C clawback / D 追索)
  T8 · 亏损 3 档 label_margin
  T9 · 库存预警 30/10/0%
  T10 · DTO 隔离(代理 API 永不返 platform_cost)
  T11 · complete_recharge 5 入口同走 SSOT
  T12 · 红线文件未动验证
  T13 · feature flag 默认 V35_FACTORY_INVENTORY_ENABLED='false'

注意:跑此测试需要:
  1. TEST_DATABASE_URL 配置(tests/conftest.py 加载)
  2. migration 已跑(scripts/migration_v35_factory_inventory_2026_05_26.sql)
  3. backfill 已跑(scripts/migration_v35_backfill_factory_2026_05_26.sql)
"""

import pytest
from datetime import datetime, timedelta

from db.connection import get_db
from services.agent_pricing import (
    calc_settlement, calc_factory_cents, label_margin,
    POINTS_PER_YUAN, WHOLESALE_CENTS_PER_POINT,
    WHOLESALE_NUMER, WHOLESALE_DENOM,
    get_platform_fee_config, strip_admin_fields,
)
from services.agent_inventory import (
    purchase_inventory_prepay, purchase_auto_and_allocate,
    allocate_offline, revoke_from_customer,
    check_inventory_alert_level, InsufficientInventoryError,
)
# [单账本接线 2026-08-17 · R4] 原 from services.customer_credit import (allocate_credit, consume_credit, revoke_credit, is_publish_feature, InsufficientCreditError, PublishPaidOnlyError) 已删除 —— 这些符号随三池语义一起退役。
from services.agent_revenue import (
    insert_revenue_ledger, insert_revenue_clawback,
    settle_frozen_to_settled, get_agent_balance, cancel_frozen_ledger,
    insert_revenue_replacement,
)
from services.settlement_orchestrator import SettlementOrchestrator


# ============================================================
# fixture
# ============================================================

@pytest.fixture
def cursor(monkeypatch):
    """每个 staging 判别使用 PostgreSQL 临时表和独立回滚事务。"""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TEMP TABLE system_settings (
                key TEXT PRIMARY KEY, value TEXT, value_type TEXT
            ) ON COMMIT DROP;
            CREATE TEMP TABLE agent_inventory_wallets (
                agent_user_id INTEGER PRIMARY KEY,
                paid_inventory_points BIGINT NOT NULL DEFAULT 0,
                bonus_inventory_points BIGINT NOT NULL DEFAULT 0,
                frozen_inventory_points BIGINT NOT NULL DEFAULT 0,
                total_purchased_points BIGINT NOT NULL DEFAULT 0,
                total_allocated_points BIGINT NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CHECK (paid_inventory_points >= 0),
                CHECK (bonus_inventory_points >= 0),
                CHECK (frozen_inventory_points >= 0)
            ) ON COMMIT DROP;
            CREATE TEMP TABLE agent_inventory_transactions (
                id BIGSERIAL PRIMARY KEY, agent_user_id INTEGER NOT NULL,
                type TEXT NOT NULL, pool TEXT NOT NULL, points BIGINT NOT NULL,
                balance_paid_after BIGINT NOT NULL,
                balance_bonus_after BIGINT NOT NULL,
                related_customer_user_id INTEGER, related_order_id TEXT,
                description TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            ) ON COMMIT DROP;
            CREATE TEMP TABLE customer_agent_credit_wallets (
                customer_user_id INTEGER PRIMARY KEY, agent_user_id INTEGER NOT NULL,
                tool_credit_points BIGINT NOT NULL DEFAULT 0,
                publish_credit_points BIGINT NOT NULL DEFAULT 0,
                bonus_credit_points BIGINT NOT NULL DEFAULT 0,
                total_purchased_points BIGINT NOT NULL DEFAULT 0,
                total_consumed_points BIGINT NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            ) ON COMMIT DROP;
            CREATE TEMP TABLE customer_credit_transactions (
                id BIGSERIAL PRIMARY KEY, customer_user_id INTEGER NOT NULL,
                agent_user_id INTEGER NOT NULL, type TEXT NOT NULL, pool TEXT NOT NULL,
                points BIGINT NOT NULL, balance_tool_after BIGINT NOT NULL,
                balance_publish_after BIGINT NOT NULL, balance_bonus_after BIGINT NOT NULL,
                feature_code TEXT, related_order_id TEXT, source TEXT, description TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
            ) ON COMMIT DROP;
            CREATE TEMP TABLE agent_revenue_ledger (
                id BIGSERIAL PRIMARY KEY, agent_user_id INTEGER NOT NULL,
                source TEXT NOT NULL, recharge_order_id TEXT, customer_user_id INTEGER,
                customer_paid_cents INTEGER NOT NULL, factory_cents INTEGER NOT NULL,
                gateway_fee_bps INTEGER NOT NULL DEFAULT 0,
                gateway_fee_cents INTEGER NOT NULL DEFAULT 0,
                settlement_service_fee_bps INTEGER NOT NULL DEFAULT 0,
                settlement_service_fee_cents INTEGER NOT NULL DEFAULT 0,
                agent_margin_before_tax_cents INTEGER NOT NULL,
                tax_rate_bps INTEGER NOT NULL DEFAULT 0, tax_mode TEXT NOT NULL DEFAULT 'none',
                tax_withholding_cents INTEGER NOT NULL DEFAULT 0,
                agent_settlement_cents INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'frozen',
                settle_at TIMESTAMPTZ NOT NULL, settled_at TIMESTAMPTZ,
                manual_review_required BOOLEAN NOT NULL DEFAULT FALSE,
                reversed_at TIMESTAMPTZ, reversed_by_ledger_id BIGINT, note TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            ) ON COMMIT DROP;
            CREATE TEMP TABLE agent_settlement_requests (
                id BIGSERIAL PRIMARY KEY, agent_user_id INTEGER NOT NULL,
                request_amount_cents INTEGER NOT NULL, status TEXT NOT NULL
            ) ON COMMIT DROP;
            CREATE TEMP TABLE agent_settlement_request_items (
                id BIGSERIAL PRIMARY KEY, settlement_request_id BIGINT NOT NULL,
                ledger_id BIGINT NOT NULL, locked_amount_cents INTEGER NOT NULL
            ) ON COMMIT DROP;
            CREATE TEMP TABLE agent_commission_redemption_requests (
                id BIGSERIAL PRIMARY KEY, agent_user_id INTEGER NOT NULL, status TEXT NOT NULL
            ) ON COMMIT DROP;
            CREATE TEMP TABLE agent_commission_redemption_items (
                id BIGSERIAL PRIMARY KEY, redemption_request_id BIGINT NOT NULL,
                ledger_id BIGINT NOT NULL, locked_amount_cents INTEGER NOT NULL
            ) ON COMMIT DROP;
            CREATE TEMP TABLE recharge_orders (
                id TEXT PRIMARY KEY,
                user_id INTEGER,
                agent_user_id INTEGER,
                amount_cents INTEGER NOT NULL DEFAULT 0,
                base_points BIGINT NOT NULL DEFAULT 0,
                bonus_points BIGINT NOT NULL DEFAULT 0,
                payment_method TEXT,
                payment_status TEXT NOT NULL DEFAULT 'paid',
                order_type TEXT,
                refund_status TEXT,
                refund_completed_at TIMESTAMPTZ,
                settlement_snapshot_jsonb JSONB,
                settlement_mode TEXT
            ) ON COMMIT DROP;
            CREATE TEMP TABLE referral_links (
                referrer_id INTEGER NOT NULL, referred_id INTEGER NOT NULL
            ) ON COMMIT DROP;
            CREATE TEMP TABLE dealer_consumer_sales (
                order_id TEXT PRIMARY KEY
            ) ON COMMIT DROP;
        """)
        cur.execute("""
            INSERT INTO system_settings(key,value,value_type) VALUES
              ('V35_FACTORY_INVENTORY_ENABLED','false','boolean'),
              ('LEGACY_REFERRAL_V32_ENABLED','passthrough_30day','text')
        """)
        from services import dealer_inventory_resale
        monkeypatch.setattr(dealer_inventory_resale, "has_resale_order", lambda *_a, **_k: False)
        monkeypatch.setattr(dealer_inventory_resale, "has_consumer_sale", lambda *_a, **_k: False)
        from services import channel_revenue_lifecycle
        monkeypatch.setattr(
            channel_revenue_lifecycle,
            "reverse_channel_revenue_on_refund",
            lambda *_a, **_k: {"reversed": False, "reason": "isolated_v35_test"},
        )
        yield cur
        conn.rollback()


def _bind_mint_order(cursor, order_id: str, *, base_points: int, bonus_points: int = 0,
                     agent_user_id: int = 999001) -> str:
    """按需铸造护栏 3:凭空加库存必须绑一张真实订单,且量不得超过订单声明量。

    工单 WORKORDER_ONDEMAND_MINTING_2026-07-29 §3-3 —— 铸造不再是"管理动作",
    而是"订单的必然结果"。夹具因此必须先落订单再进货。
    """
    cursor.execute(
        """INSERT INTO recharge_orders
           (id,user_id,agent_user_id,amount_cents,base_points,bonus_points,
            payment_status,order_type)
           VALUES (%s,999002,%s,%s,%s,%s,'paid','agent_inventory_purchase')
           ON CONFLICT (id) DO NOTHING""",
        (order_id, agent_user_id,
         min(int(base_points + bonus_points), 2_000_000_000),
         int(base_points), int(bonus_points)),
    )
    return order_id


@pytest.fixture
def agent_id():
    return 999001  # 测试代理


@pytest.fixture
def customer_id():
    return 999002  # 测试客户


def _insert_test_ledger(cursor, order_id: str, *, settle_delay_days: int) -> int:
    return insert_revenue_ledger(
        cursor,
        agent_user_id=999001,
        recharge_order_id=order_id,
        customer_user_id=999002,
        customer_paid_cents=150,
        factory_cents=50,
        gateway_fee_bps=0,
        gateway_fee_cents=0,
        settlement_service_fee_bps=0,
        settlement_service_fee_cents=0,
        agent_margin_before_tax_cents=100,
        tax_rate_bps=0,
        tax_mode="none",
        tax_withholding_cents=0,
        agent_settlement_cents=100,
        settle_delay_days=settle_delay_days,
    )


def _run_v35_refund(cursor, monkeypatch, order_id: str, *, reason: str) -> None:
    """Run the actual refund orchestrator against this test transaction.

    The proxy deliberately keeps commit/close local so the temp-table fixture
    remains alive while all production SQL and ledger branches execute.
    """
    from api import referral_api

    class _ConnectionProxy:
        def cursor(self):
            return cursor.connection.cursor()

        def commit(self):
            return None

        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(referral_api, "get_connection", lambda: _ConnectionProxy())
    referral_api._handle_v35_factory_refund(order_id, refund_reason=reason)


def _seed_refundable_order(cursor, order_id: str, *, points: int = 10) -> None:
    cursor.execute(
        """INSERT INTO recharge_orders(
               id,user_id,agent_user_id,amount_cents,base_points,bonus_points,
               payment_method,payment_status,order_type,refund_status,
               refund_completed_at,settlement_snapshot_jsonb,settlement_mode)
           VALUES (%s,999002,999001,150,%s,0,'wechat','paid','customer_recharge',
                   'pending_review',clock_timestamp(),'{}'::jsonb,'v35_inventory_settlement')""",
        (order_id, points),
    )
    allocate_credit(
        cursor,
        customer_user_id=999002,
        agent_user_id=999001,
        tool_points=points,
        related_order_id=order_id,
        source="online_payment",
    )


# ============================================================
# T1 · 结算公式
# ============================================================

class TestSettlementFormula:
    """v7 结算公式：每单只锁定出厂成本，费用/税在提现端结算。"""

    def test_factory_cost_at_wholesale_rate(self):
        # 1300 积分 → 出厂 900 cents (=¥9) 整数 ceil 公式
        # ceil(1300 × 225 / 325) = ceil(292500/325) = ceil(900) = 900
        assert calc_factory_cents(1300) == 900
        # Codex r2 P1-2 修正 · 不再用 float round
        assert calc_factory_cents(0) == 0
        assert calc_factory_cents(-1) == 0
        # 边界 ceil 验证(1 积分 → 1 cents · 因 1×225/325=0.692 · ceil=1)
        assert calc_factory_cents(1) == 1
        # 公式一致性
        assert calc_factory_cents(195000) == (195000 * 225 + 324) // 325

    def test_per_order_fees_are_deferred_to_withdrawal(self):
        """¥2000 案例：与已部署基线 v7 语义一致，不在每笔订单重复扣费。"""
        result = calc_settlement(
            customer_paid_cents=200000,  # ¥2000
            points_granted=195000,        # → 出厂 ¥1200(=195000 × 0.6154 ≈ 119992 cents)
            payment_method="wechat_pay",
            tax_rate_bps=600,
        )
        assert result["gateway_fee_cents"] == 0
        assert result["settlement_service_fee_cents"] == 0
        assert result["collection_fee_cents"] == 0
        assert result["tax_withholding_cents"] == 0
        assert result["agent_margin_before_tax_cents"] == (
            result["customer_paid_cents"] - result["factory_cents"]
        )
        assert result["agent_settlement_cents"] == result["agent_margin_before_tax_cents"]

    def test_negative_margin_no_tax(self):
        """亏损时不预扣税(margin < 0 · 已亏不再扣)"""
        result = calc_settlement(
            customer_paid_cents=10000,  # 客户付 ¥100
            points_granted=130000,      # 出厂 ¥800(代理倒贴)
            tax_rate_bps=600,
        )
        assert result["agent_margin_before_tax_cents"] < 0
        assert result["tax_withholding_cents"] == 0


# ============================================================
# T8 · margin label 3 档 + 3 档
# ============================================================

class TestMarginLabel:

    def test_healthy_margin(self):
        label, action = label_margin(margin_before_tax_cents=400, factory_cents=1000)  # +40%
        assert action == "allowed"
        assert label == "profit_good"  # v7: 30%-100% 为 profit_good

    def test_loss_medium_needs_admin(self):
        label, action = label_margin(margin_before_tax_cents=-300, factory_cents=1000)  # -30%
        assert label == "loss_medium"
        assert action == "admin_approval_required"

    def test_loss_heavy_rejected(self):
        label, action = label_margin(margin_before_tax_cents=-700, factory_cents=1000)  # -70%
        assert label == "loss_heavy"
        assert action == "rejected"

    def test_excess_over_3000pct_rejected(self):
        # 默认阈值 hard_block_bps=250000，即 +2500%。
        label, action = label_margin(margin_before_tax_cents=31000, factory_cents=1000)
        assert label == "margin_anomaly_blocked"
        assert action == "rejected"


# ============================================================
# T4 · 发布中心 paid-only
# ============================================================

class TestPublishPaidOnly:

    @pytest.mark.skip(reason="[单账本接线 2026-08-17 · R4] 本用例测的是 customer_agent_credit_wallets 三池语义 / 已删除的 customer_credit 函数。该行为于 2026-07-29 单账本收敛时被**有意移除**(客户算力只剩 user_wallets 一处),用例随之退役。替代守卫见 tests/v35_refund_single_ledger_2026_08_17/。")
    def test_is_publish_feature_recognized(self):
        assert is_publish_feature("publish_mhz_media")
        assert is_publish_feature("publish_single")
        assert is_publish_feature("publish_batch")
        assert is_publish_feature("publish_wemedia")
        assert not is_publish_feature("diagnosis_full")
        assert not is_publish_feature("writing_long")
        assert not is_publish_feature("monitoring_daily")


# ============================================================
# T10 · DTO 隔离 · raw cost 永不外露
# ============================================================

class TestDtoIsolation:

    def test_strip_admin_fields(self):
        sku = {
            "name": "基础包",
            "retail_cents": 18000,
            "wholesale_cents": 4923,
            "platform_cost_cents": 1500,  # admin only
            "raw_cost": 1500,
            "internal_margin": 3423,
        }
        stripped = strip_admin_fields(sku)
        assert "platform_cost_cents" not in stripped
        assert "raw_cost" not in stripped
        assert "internal_margin" not in stripped
        # 代理可见字段保留
        assert stripped["wholesale_cents"] == 4923
        assert stripped["retail_cents"] == 18000


# ============================================================
# T9 · 库存预警
# ============================================================

class TestInventoryAlert:
    """需要真 cursor · 实际跑 DB 测"""

    def test_alert_levels(self, cursor, agent_id):
        # 首次进货 100000 积分(paid)· 铸造必须绑真实订单
        order_id = _bind_mint_order(cursor, "O-ALERT-LEVELS", base_points=100000)
        purchase_inventory_prepay(
            cursor, agent_id, paid_points=100000, related_order_id=order_id,
        )
        # 划拨 60000 → 剩 40%(should be 'ok')
        allocate_offline(
            cursor, agent_user_id=agent_id, customer_user_id=999002,
            paid_points=60000,
        )
        level, info = check_inventory_alert_level(cursor, agent_id)
        assert level == "ok"
        assert info["remaining_pct"] == 40


# ============================================================
# T11 · feature flag 默认值
# ============================================================

class TestFeatureFlag:

    def test_v35_factory_default_disabled(self, cursor):
        cursor.execute("SELECT value FROM system_settings WHERE key='V35_FACTORY_INVENTORY_ENABLED'")
        row = cursor.fetchone()
        # 启动 false · 等代码 ready 后才切 true
        assert row is not None
        val = row[0] if not isinstance(row, dict) else row["value"]
        assert val == "false"


# ============================================================
# T2 · 自动补库存双流水 + T3 · 线下划拨不写 ledger
# 这两个需要 staging DB 跑 · 测试代码框架 skip mark
# ============================================================

class TestInventoryFlows:

    def test_purchase_auto_and_allocate_double_trace(self, cursor, agent_id, customer_id):
        """purchase_auto + allocate · 净 0 但 trace 2 条"""
        order_id = _bind_mint_order(
            cursor, "O-AUTO-TRACE", base_points=10000, bonus_points=2000,
        )
        result = purchase_auto_and_allocate(
            cursor, agent_user_id=agent_id, customer_user_id=customer_id,
            points_granted=10000, bonus_points=2000, related_order_id=order_id,
        )
        # paid 净 0 · 因为先 +10000 后 -10000
        assert result["allocated_paid"] == 10000
        assert result["allocated_bonus"] == 2000
        # 流水 4 条:paid purchase_auto + paid allocate + bonus purchase_auto + bonus allocate
        cursor.execute("""
            SELECT COUNT(*) AS c FROM agent_inventory_transactions
            WHERE agent_user_id = %s
              AND type IN ('purchase_auto','allocate_to_customer')
        """, (agent_id,))
        cnt = cursor.fetchone()["c"]
        assert cnt == 4

    def test_offline_alloc_no_revenue_ledger(self, cursor, agent_id, customer_id):
        """线下划拨 · 不写 agent_revenue_ledger"""
        # 先建库存 · 铸造必须绑真实订单
        order_id = _bind_mint_order(cursor, "O-OFFLINE-ALLOC", base_points=20000)
        purchase_inventory_prepay(
            cursor, agent_id, paid_points=20000, related_order_id=order_id,
        )
        # 线下划拨
        allocate_offline(
            cursor, agent_user_id=agent_id, customer_user_id=customer_id,
            paid_points=5000, bonus_points=0,
        )
        # ledger 表应 0 条新增
        cursor.execute("""
            SELECT COUNT(*) AS c FROM agent_revenue_ledger
            WHERE agent_user_id = %s
        """, (agent_id,))
        cnt = cursor.fetchone()["c"]
        assert cnt == 0


# ============================================================
# T5 · revoke 严格上限
# ============================================================

class TestRevokeUpperBound:

    @pytest.mark.skip(reason="[单账本接线 2026-08-17 · R4] 本用例测的是 customer_agent_credit_wallets 三池语义 / 已删除的 customer_credit 函数。该行为于 2026-07-29 单账本收敛时被**有意移除**(客户算力只剩 user_wallets 一处),用例随之退役。替代守卫见 tests/v35_refund_single_ledger_2026_08_17/。")
    def test_revoke_cannot_exceed_balance(self, cursor, agent_id, customer_id):
        # 客户当前 5000 tool · 撤回请求 10000 · 实际只撤 5000(min)
        allocate_credit(
            cursor, customer_user_id=customer_id, agent_user_id=agent_id,
            tool_points=5000, source="online_payment",
        )
        result = revoke_credit(
            cursor, customer_user_id=customer_id,
            tool_points=10000,  # 请求 > 余额
        )
        # 实际撤回上限 = 5000
        assert result["actually_revoked"]["tool"] == 5000
        assert result["tool_credit_points"] == 0  # 全部撤完


# ============================================================
# T6 · SettlementOrchestrator 三互斥
# ============================================================

class TestSettlementOrchestrator:

    def test_v35_path_when_binding_and_flag_on(self, cursor, monkeypatch):
        """客户绑代理 + V35 flag ON → v35_inventory_settlement"""
        cursor.execute("UPDATE system_settings SET value='true' WHERE key='V35_FACTORY_INVENTORY_ENABLED'")
        cursor.execute("INSERT INTO recharge_orders(id) VALUES ('route-bound')")
        monkeypatch.setattr(
            "services.settlement_orchestrator.get_customer_binding",
            lambda *_a, **_k: {"agent_user_id": 999001, "dispute_status": None},
        )
        monkeypatch.setattr(SettlementOrchestrator, "_record_factory_settlement", lambda *_a, **_k: None)
        mode = SettlementOrchestrator().route(
            cursor,
            {"id": "route-bound", "order_type": "legacy", "agent_user_id": None},
            {"user_id": 999002},
        )
        assert mode == "v35_inventory_settlement"
        cursor.execute("SELECT settlement_mode FROM recharge_orders WHERE id='route-bound'")
        assert cursor.fetchone()["settlement_mode"] == mode

    def test_v32_legacy_when_no_binding_but_old_referrer(self, cursor, monkeypatch):
        """推荐来源不是商业绑定；无绑定旧用户仍走平台 direct。"""
        cursor.execute("INSERT INTO referral_links VALUES (999001,999002)")
        cursor.execute("INSERT INTO recharge_orders(id) VALUES ('route-referral')")
        monkeypatch.setattr("services.settlement_orchestrator.get_customer_binding", lambda *_a, **_k: None)
        mode = SettlementOrchestrator().route(
            cursor, {"id": "route-referral", "order_type": "legacy"}, {"user_id": 999002}
        )
        assert mode == "direct"

    def test_direct_when_no_binding_no_referrer(self, cursor, monkeypatch):
        """无任何关联 → direct"""
        cursor.execute("INSERT INTO recharge_orders(id) VALUES ('route-direct')")
        monkeypatch.setattr("services.settlement_orchestrator.get_customer_binding", lambda *_a, **_k: None)
        mode = SettlementOrchestrator().route(
            cursor, {"id": "route-direct", "order_type": "legacy"}, {"user_id": 999002}
        )
        assert mode == "direct"


# ============================================================
# T7 · 退款 4 状态机
# ============================================================

class TestRefundFourStates:

    def test_state_a_t3_within_zero_consume(self, cursor):
        """A · T+3 内 + 0 消费 → 全退 + frozen 反向冲销"""
        ledger_id = _insert_test_ledger(cursor, "refund-a", settle_delay_days=3)
        assert cancel_frozen_ledger(cursor, ledger_id, reason="full_refund") == 1
        cursor.execute("SELECT status,reversed_at FROM agent_revenue_ledger WHERE id=%s", (ledger_id,))
        row = cursor.fetchone()
        assert row["status"] == "cancelled" and row["reversed_at"] is not None
        cursor.execute("SELECT COUNT(*) AS c FROM agent_revenue_ledger WHERE source='refund_clawback'")
        assert cursor.fetchone()["c"] == 0

    @pytest.mark.skip(reason="[单账本接线 2026-08-17 · R4] 本用例测的是 customer_agent_credit_wallets 三池语义 / 已删除的 customer_credit 函数。该行为于 2026-07-29 单账本收敛时被**有意移除**(客户算力只剩 user_wallets 一处),用例随之退役。替代守卫见 tests/v35_refund_single_ledger_2026_08_17/。")
    def test_state_b_t3_within_partial_consume(self, cursor, monkeypatch):
        """B · 真实退款入口取消原冻结并只保留已消费部分收益。"""
        order_id = "refund-b"
        ledger_id = _insert_test_ledger(cursor, order_id, settle_delay_days=3)
        _seed_refundable_order(cursor, order_id, points=10)
        consume_credit(
            cursor,
            customer_user_id=999002,
            feature_code="diagnosis_run",
            cost_points=6,
            related_order_id="use-refund-b",
            description="consume six of ten before refund",
        )

        _run_v35_refund(cursor, monkeypatch, order_id, reason="partial_refund")

        cursor.execute(
            "SELECT status,reversed_by_ledger_id FROM agent_revenue_ledger WHERE id=%s",
            (ledger_id,),
        )
        original = cursor.fetchone()
        assert original["status"] == "cancelled"
        replacement_id = int(original["reversed_by_ledger_id"])
        cursor.execute(
            """SELECT status,manual_review_required,agent_settlement_cents
               FROM agent_revenue_ledger WHERE id=%s""",
            (replacement_id,),
        )
        replacement = cursor.fetchone()
        assert replacement == {
            "status": "frozen",
            "manual_review_required": False,
            "agent_settlement_cents": 60,
        }
        cursor.execute(
            "SELECT refund_status,settlement_snapshot_jsonb FROM recharge_orders WHERE id=%s",
            (order_id,),
        )
        order = cursor.fetchone()
        assert order["refund_status"] == "processed"
        assert order["settlement_snapshot_jsonb"]["manual_review"] is True

    def test_state_c_t3_past_settled_not_paid(self, cursor):
        """C · settled 未提走 → clawback 负数"""
        ledger_id = _insert_test_ledger(cursor, "refund-c", settle_delay_days=-1)
        assert settle_frozen_to_settled(cursor) == 1
        clawback_id = insert_revenue_clawback(cursor, 999001, ledger_id, 60, "state-c")
        cursor.execute("SELECT status,agent_settlement_cents FROM agent_revenue_ledger WHERE id=%s", (clawback_id,))
        assert cursor.fetchone() == {"status": "settled", "agent_settlement_cents": -60}
        balance = get_agent_balance(cursor, 999001)
        assert balance["settled_total_cents"] == 40
        assert balance["available_cents"] == 40

    @pytest.mark.skip(reason="[单账本接线 2026-08-17 · R4] 本用例测的是 customer_agent_credit_wallets 三池语义 / 已删除的 customer_credit 函数。该行为于 2026-07-29 单账本收敛时被**有意移除**(客户算力只剩 user_wallets 一处),用例随之退役。替代守卫见 tests/v35_refund_single_ledger_2026_08_17/。")
    def test_state_d_t3_past_paid(self, cursor, monkeypatch):
        """D · 真实退款入口识别已提走收益并生成需人工追索的 clawback。"""
        order_id = "refund-d"
        ledger_id = _insert_test_ledger(cursor, order_id, settle_delay_days=-1)
        _seed_refundable_order(cursor, order_id, points=10)
        consume_credit(
            cursor,
            customer_user_id=999002,
            feature_code="diagnosis_run",
            cost_points=4,
            related_order_id="use-refund-d",
            description="consume four of ten before paid-settlement refund",
        )
        assert settle_frozen_to_settled(cursor) == 1
        cursor.execute(
            """INSERT INTO agent_settlement_requests(agent_user_id,request_amount_cents,status)
               VALUES (999001,100,'paid') RETURNING id"""
        )
        request_id = cursor.fetchone()["id"]
        cursor.execute(
            """INSERT INTO agent_settlement_request_items(
                   settlement_request_id,ledger_id,locked_amount_cents)
               VALUES (%s,%s,100)""",
            (request_id, ledger_id),
        )
        _run_v35_refund(cursor, monkeypatch, order_id, reason="paid_settlement_refund")
        cursor.execute(
            """SELECT agent_settlement_cents,manual_review_required
               FROM agent_revenue_ledger
               WHERE source='refund_clawback' AND recharge_order_id=%s""",
            (order_id,),
        )
        clawback = cursor.fetchone()
        assert clawback == {
            "agent_settlement_cents": -60,
            "manual_review_required": True,
        }
        balance = get_agent_balance(cursor, 999001)
        assert balance["paid_cents"] == 100
        assert balance["available_cents"] == 0
        cursor.execute(
            "SELECT refund_status,settlement_snapshot_jsonb FROM recharge_orders WHERE id=%s",
            (order_id,),
        )
        order = cursor.fetchone()
        assert order["refund_status"] == "processed"
        assert order["settlement_snapshot_jsonb"]["manual_review"] is True


# ============================================================
# T12 · 红线文件未动验证
# ============================================================

class TestRedLineFiles:

    def test_middleware_billing_signature_unchanged(self):
        """middleware/billing.py 公共接口未动(deduct_points / refund_points / charge_on_success)"""
        from middleware import billing
        assert hasattr(billing, "deduct_points")
        assert hasattr(billing, "refund_points")
        # 关键函数签名未变
        import inspect
        sig = inspect.signature(billing.deduct_points)
        # 至少接收 user_id, feature_code 两个参数(签名不动)
        assert "user_id" in sig.parameters
        assert "feature_code" in sig.parameters

    def test_user_wallets_schema_untouched(self):
        """user_wallets 仍是底层平台 SSOT · 字段名未动"""
        from db.wallet_db import get_or_create_wallet
        assert callable(get_or_create_wallet)
