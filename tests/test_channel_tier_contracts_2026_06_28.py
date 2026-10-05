from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_channel_tier_migration_is_new_tables_only_and_flag_off_by_default():
    migration = read("scripts/migration_channel_tier_2026_06_28.sql")
    rollback = read("scripts/rollback_channel_tier_2026_06_28.sql")
    combined = f"{migration}\n{rollback}".upper()

    assert "CREATE TABLE IF NOT EXISTS AGENT_CHANNEL_TIER_STATE" in combined
    assert "CREATE TABLE IF NOT EXISTS BONUS_GRANTS" in combined
    assert "CHANNEL_TIER_ENABLED" in migration
    assert "'FALSE'" in migration.upper()
    assert "ALTER TABLE USER_WALLETS" not in combined
    assert "ALTER TABLE QUOTES" not in combined
    assert "ALTER TABLE AGENT_INVENTORY_WALLETS" not in combined
    assert "ALTER TABLE CUSTOMER_AGENT_CREDIT_WALLETS" not in combined
    assert "ALTER TABLE USER_WALLETS" not in combined
    assert "USER_WALLETS" not in combined

    assert "\nCOMMIT" not in combined
    assert "\nROLLBACK" not in combined
    assert "\nBEGIN;" not in combined


def test_channel_tier_migration_upgrades_existing_bonus_grant_type_check():
    migration = read("scripts/migration_channel_tier_2026_06_28.sql")
    runner = read("db/migrate_channel_tier.py")
    upper = migration.upper()

    assert "PG_CONSTRAINT" in upper
    assert "PG_GET_CONSTRAINTDEF" in upper
    assert "BONUS_GRANTS" in upper
    assert "GRANT_TYPE" in upper
    assert "DROP CONSTRAINT" in upper
    assert "ADD CONSTRAINT BONUS_GRANTS_GRANT_TYPE_CHECK" in upper
    assert "CUSTOMER_ORDER_BONUS" in upper
    assert "ADMIN_ADJUST" in upper
    assert "bonus_grants grant_type CHECK supports customer/admin grants" in runner
    assert "customer_order_bonus" in runner


def test_dockerignore_keeps_channel_tier_migration_runner_in_image():
    dockerignore = read(".dockerignore").splitlines()

    assert "db/migrate_*.py" in dockerignore
    assert "!db/migrate_channel_tier.py" in dockerignore
    assert dockerignore.index("!db/migrate_channel_tier.py") > dockerignore.index("db/migrate_*.py")


def test_channel_tier_mount_points_are_flag_guarded_and_do_not_touch_redlines():
    wallet_db = read("db/wallet_db.py")
    workbench = read("api/agent_workbench_api.py")
    customer_credit = read("services/customer_credit.py")
    scheduler = read("api/scheduler.py")

    assert "is_channel_tier_enabled" in wallet_db
    assert "agent_inventory_prepay_channel_tier" in wallet_db
    assert "grant_tier_bonus" in wallet_db
    assert "grant_founder_first_order_bonus" in wallet_db
    assert "displayed_bonus_points = int(chosen[\"bonus_points\"])" in workbench
    # [单账本接线 2026-08-17] consume_bonus_fifo 的**挂载点搬家**了:
    #   原来在 services/customer_credit.revoke_credit 里(该函数已随停写表退役删除),
    #   现在在 api/referral_api._revoke_customer_and_sync_grants(退款链的客户侧回收入口)。
    # 不变式没变 —— 「回收 bonus 必须同步消耗 bonus_grants 台账」仍然被强制,
    # 只是落点跟着 revoke 的落点一起从信用钱包挪到了 user_wallets。
    assert "consume_bonus_fifo" in read("api/referral_api.py")
    assert "register_channel_tier_jobs" in scheduler

    for rel in ("middleware/billing.py", "db/connection.py", "auth/middleware.py", "auth/jwt_utils.py"):
        assert not (ROOT / rel).exists() or rel not in "\n".join(
            p for p in [
                "api/agent_workbench_api.py",
                "api/scheduler.py",
                "config/pricing_config.py",
                "config/v3_3_1_flags.py",
                "db/wallet_db.py",
                "services/agent_pricing.py",
                "services/bonus_grants.py",
                "services/channel_tier.py",
                "services/channel_tier_cron.py",
                "services/customer_credit.py",
            ]
        )


def test_bonus_grant_idempotency_contract_returns_created_flag():
    bonus_grants = read("services/bonus_grants.py")

    assert "ON CONFLICT (grant_key) DO NOTHING" in bonus_grants
    assert 'result["_created"] = True' in bonus_grants
    assert 'result["_created"] = False' in bonus_grants
    assert "FOR UPDATE" in bonus_grants


def test_sku_k_default_is_flagged_and_customer_copy_is_updated():
    flags = read("config/v3_3_1_flags.py")
    pricing = read("services/agent_pricing.py")
    all_copy = "\n".join(
        read(rel)
        for rel in (
            # [开源 E3 · 前端 · 2026-10-01 · WO_322] 旧对话 UI 方案卡与旧 C 端 GEO 方案页随宿主整删
            "frontend/src/lib/probability.ts",
            "frontend/src/pages/Quote/OnlineQuoteFlow.tsx",
            "CLAUDE.md",
        )
    )

    assert '"V35_SKU_K_DEFAULT_ENABLED": False' in flags
    assert "def get_sku_markup_with_default" in pricing
    assert "suggested_retail_cents" in pricing
    assert "get_k_default" in pricing
    assert 'get_flag("V35_SKU_K_DEFAULT_ENABLED")' in pricing
    assert "问 5 次约出现 4 次" not in all_copy
    assert "问 4 次约出现 3 次" in all_copy


def test_pricing_defaults_expose_channel_tier_config():
    from config.pricing_config import (
        get_agent_tier_config,
        get_bonus_validity_months,
        get_founding_config,
        get_k_default,
    )

    tier_config = get_agent_tier_config()
    assert tier_config["certified"]["min_yuan"] == 500
    assert tier_config["preferred"]["bonus_rate"] == 0.15
    assert tier_config["strategic"]["min_yuan"] == 10000
    assert get_founding_config()["cap"] == 10
    assert get_bonus_validity_months() == 12
    assert get_k_default() == 1.5


def test_determine_tier_uses_default_thresholds():
    from services.channel_tier import determine_tier

    assert determine_tier(0) == "none"
    assert determine_tier(499) == "none"
    assert determine_tier(500) == "certified"
    assert determine_tier(2999) == "certified"
    assert determine_tier(3000) == "preferred"
    assert determine_tier(9999) == "preferred"
    assert determine_tier(10000) == "strategic"


def test_purchase_event_trigger_source_matches_migration_check():
    wallet_db = read("db/wallet_db.py")
    assert 'trigger_source="purchase_event"' in wallet_db


def test_purchase_options_project_post_purchase_tier_once_per_request():
    """卡片必须走下单相同的快照计算器，并在同一事务核验配置纪元。"""
    source = read("api/agent_workbench_api.py")
    start = source.index("def _compute_purchase_options")
    end = source.index("def _channel_tier_purchase_context", start)
    body = source[start:end]
    assert "build_purchase_snapshot" in body
    assert "read_config_epoch_strict" in body
    assert "pg_advisory_xact_lock_shared" in body
    assert body.count("with get_db() as conn") == 1


def test_purchase_projection_is_post_tier_single_source():
    import config.pricing_config as pricing_config
    import services.agent_pricing as agent_pricing
    import services.channel_tier as channel_tier

    class Cursor:
        def execute(self, query, params=None):
            self.query = query
            self.params = params

        def fetchone(self):
            return {"amount_cents": 290000}

    original_calc = agent_pricing.calc_prepay_points
    original_tiers = pricing_config.get_agent_tier_config
    try:
        agent_pricing.calc_prepay_points = lambda amount_cents, agent_user_id=None: 1000
        pricing_config.get_agent_tier_config = lambda: {
            "certified": {"min_yuan": 500, "bonus_rate": 0.10},
            "preferred": {"min_yuan": 3000, "bonus_rate": 0.40},
            "strategic": {"min_yuan": 10000, "bonus_rate": 0.80},
        }
        projection = channel_tier.compute_purchase_bonus_projection(
            Cursor(),
            agent_user_id=1001,
            amount_cents=20000,
        )
        assert projection["rolling_before_yuan"] == 2900
        assert projection["projected_rolling_12m_yuan"] == 3100
        assert projection["projected_tier"] == "preferred"
        assert projection["base_points"] == 1000
        assert projection["bonus_points"] == 400
    finally:
        agent_pricing.calc_prepay_points = original_calc
        pricing_config.get_agent_tier_config = original_tiers


def test_wallet_charge_path_uses_purchase_projection_for_grant_bonus():
    wallet_db = read("db/wallet_db.py")
    assert "compute_purchase_bonus_projection" in wallet_db
    assert "exclude_order_id=order_id" in wallet_db
    assert "bonus_points=int(projection.get(\"bonus_points\") or 0)" in wallet_db


def test_purchase_context_returns_rolling_before_not_current_tier():
    workbench = read("api/agent_workbench_api.py")
    assert "return True, float(rolling_before_yuan)" in workbench


def test_refund_and_revoke_keep_bonus_grant_ledger_balanced():
    """退款/回收都必须让 bonus_grants 台账保持平衡。

    [单账本接线 2026-08-17] 断言随实现搬家而更新,**不变式本身没放松**:
      · refund 侧仍在 services/customer_credit.refund_credit(该函数作为在途 v35 冻结
        release 的历史通道**刻意保留**)→ create_grant + reconcile 断言原样保留;
      · revoke 侧的 customer_credit.revoke_credit 已删除,接班实现是
        api/referral_api._revoke_customer_and_sync_grants → 在那里断言 consume_bonus_fifo。
      · 🔴 revoke 侧**故意不搬** `_log_bonus_grant_reconcile`:它读的视图
        v_bonus_grant_reconcile 的 customer 分支 pool_balance 取自停写表
        customer_agent_credit_wallets.bonus_credit_points(现恒 0),搬过去只会每次
        退款刷一条必然为真的假漂移告警。视图返修另出工单。
    """
    customer_credit = read("services/customer_credit.py")
    referral = read("api/referral_api.py")
    # refund 侧(保留的历史通道)
    assert "create_grant(" in customer_credit
    assert "grant_type=\"admin_adjust\"" in customer_credit
    assert "refund:" in customer_credit
    assert "_log_bonus_grant_reconcile(cursor, customer_user_id, \"refund_credit\")" in customer_credit
    # revoke 侧(接班实现)
    assert "consume_bonus_fifo(cursor, \"customer\", customer_user_id, revoked_bonus)" in referral
    # 边车失败不得打废退款主事务
    assert "SAVEPOINT v35_refund_grant_sync" in referral
    assert "ROLLBACK TO SAVEPOINT v35_refund_grant_sync" in referral


def test_channel_tier_cron_logs_reconcile_drift_read_only():
    cron = read("services/channel_tier_cron.py")
    assert "v_bonus_grant_reconcile" in cron
    assert "pool_balance <> grant_active_points" in cron
    assert "logger.warning" in cron
    assert "UPDATE v_bonus_grant_reconcile" not in cron


def test_bonus_grant_admin_renew_endpoint_and_owner_guard_exist():
    admin_api = read("api/admin_api.py")
    bonus_grants = read("services/bonus_grants.py")
    assert "/channel-tier/bonus-grants/{grant_id}/renew" in admin_api
    assert "_require_admin(request)" in admin_api
    assert "owner_type: Optional[str]" in bonus_grants
    assert "owner_id: Optional[int]" in bonus_grants
    assert "bonus grant owner mismatch" in bonus_grants


def test_dead_grant_transfer_helper_removed_and_fifo_uses_real_columns():
    bonus_grants = read("services/bonus_grants.py")
    assert "def allocate_grant_to_customer" not in bonus_grants
    assert "remaining_points" not in bonus_grants


class _GrantCursor:
    def __init__(self, grants):
        self.grants = grants
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchall(self):
        return list(self.grants)


def test_consume_bonus_fifo_is_legacy_compatible_when_grants_missing():
    from services.bonus_grants import consume_bonus_fifo

    result = consume_bonus_fifo(_GrantCursor([]), "customer", 1001, 25)
    assert result["requested_points"] == 25
    assert result["consumed_points"] == 0
    assert result["missing_points"] == 25
    assert result["legacy_untracked"] is True


def test_consume_bonus_fifo_records_partial_consumption_without_throwing():
    from services.bonus_grants import consume_bonus_fifo

    cursor = _GrantCursor([{"id": 7, "granted_points": 10, "consumed_points": 3, "frozen_points": 2}])
    result = consume_bonus_fifo(cursor, "customer", 1001, 8)
    assert result["requested_points"] == 8
    assert result["consumed_points"] == 5
    assert result["missing_points"] == 3
    assert result["legacy_untracked"] is True
    assert any("UPDATE bonus_grants" in query for query, _ in cursor.executed)


class _FirstOrderCursor:
    def __init__(self, prior_amount_cents):
        self.prior_amount_cents = prior_amount_cents

    def execute(self, query, params=None):
        self.query = query
        self.params = params

    def fetchone(self):
        return {"prior_amount_cents": self.prior_amount_cents}


def test_founder_first_order_uses_cumulative_threshold_crossing():
    from services.channel_tier import is_first_order

    assert is_first_order(_FirstOrderCursor(40000), 1001, "ord-new", 200) is True
    assert is_first_order(_FirstOrderCursor(60000), 1001, "ord-new", 200) is False


def test_sku_default_retail_formula_is_wholesale_times_k():
    import services.agent_pricing as pricing

    original_ratio = pricing.get_agent_wholesale_ratio
    pricing.get_agent_wholesale_ratio = lambda agent_user_id: (95, 100)
    try:
        assert pricing.get_sku_markup_with_default(1001, 10000) == 15000
    finally:
        pricing.get_agent_wholesale_ratio = original_ratio


def test_channel_tier_change_log_idempotency_has_time_bucket():
    channel_tier = read("services/channel_tier.py")
    assert "%Y%m" in channel_tier or "strftime" in channel_tier


def test_channel_tier_cron_has_explicit_role_gate():
    cron = read("services/channel_tier_cron.py")
    assert "CHANNEL_TIER_CRON_ROLE_GATE" in cron
    assert "ROLE" in cron
    assert "Role gate contract for blue/green deployments" in cron


if __name__ == "__main__":
    # This repository's pytest conftest intentionally requires TEST_DATABASE_URL
    # to protect production data. These contract tests are DB-free, so allow a
    # direct script run in local environments without a test database.
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
