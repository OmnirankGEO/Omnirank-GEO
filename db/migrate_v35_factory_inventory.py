"""
V3.5 GEO 工具额度工厂模式 · 代理库存 + 平台代收结算 migration 执行入口

用法:
    python -m db.migrate_v35_factory_inventory            # 跑 migration
    python -m db.migrate_v35_factory_inventory --rollback # 回滚
    python -m db.migrate_v35_factory_inventory --verify   # 仅校验(不改 schema)
    python -m db.migrate_v35_factory_inventory --dry-run  # 跑在事务里 ROLLBACK · 验证语法

V3.5 规范:
- 幂等 · 重跑 0 ERROR
- 不允许 prod 直跑 · 必须 staging dry-run 通过
- feature flag `V35_FACTORY_INVENTORY_ENABLED='false'` 启动 · 不影响生产
- 旧 `LEGACY_REFERRAL_V32_ENABLED='passthrough_30day'` 过渡期 30 天保留

关联:
- docs/AI-CONTEXT/V35_FACTORY_INVENTORY_MODEL_v6_2026-05-26.md
- memory feedback_v35_factory_inventory_model_v6
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.connection import get_db  # noqa

logger = logging.getLogger("GEO-Migrate-V35")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
MIGRATION_FILE = SCRIPTS_DIR / "migration_v35_factory_inventory_2026_05_26.sql"
ROLLBACK_FILE = SCRIPTS_DIR / "rollback_v35_factory_inventory_2026_05_26.sql"
BACKFILL_FILE = SCRIPTS_DIR / "migration_v35_backfill_factory_2026_05_26.sql"

# (name, sql, expected_count, [required=True])
VERIFY_QUERIES = [
    (
        "11 张新表",
        """
        -- [历史账本只读 · 2026-08-17] customer_agent_credit_wallets 已于 2026-07-29 停写,
        -- 此处只做**建表存在性**校验(不读数据)。将来若删表,本清单与
        -- scripts/deploy-blue-green.sh 的 schema 门禁必须同批改。
        SELECT COUNT(*) AS c FROM information_schema.tables
         WHERE table_name IN (
            'sku_templates','agent_sku_overrides','agent_inventory_wallets',
            'agent_inventory_transactions','customer_agent_credit_wallets',
            'customer_credit_transactions','customer_agent_bindings',
            'agent_revenue_ledger','agent_settlement_requests',
            'agent_settlement_request_items','agent_tax_profiles'
         )
        """,
        11,
    ),
    (
        "feature_pricing 扩列(出厂价 SSOT)",
        """
        SELECT COUNT(*) AS c FROM information_schema.columns
         WHERE table_name='feature_pricing'
           AND column_name IN ('wholesale_cents','wholesale_points','platform_cost_cents')
        """,
        3,
    ),
    (
        "mhz_media 扩列(发布出厂价)",
        """
        SELECT COUNT(*) AS c FROM information_schema.columns
         WHERE table_name='mhz_media'
           AND column_name IN ('wholesale_cents','wholesale_points','platform_cost_cents')
        """,
        3,
    ),
    (
        "users 加 referred_by_agent_id + agent_bound_at",
        """
        SELECT COUNT(*) AS c FROM information_schema.columns
         WHERE table_name='users'
           AND column_name IN ('referred_by_agent_id','agent_bound_at')
        """,
        2,
    ),
    (
        "recharge_orders 扩 13 列",
        """
        SELECT COUNT(*) AS c FROM information_schema.columns
         WHERE table_name='recharge_orders'
           AND column_name IN (
             'agent_user_id','sku_template_id','factory_cents','agent_revenue_cents',
             'agent_margin_before_tax_cents','gateway_fee_bps','gateway_fee_cents',
             'settlement_service_fee_bps','settlement_service_fee_cents',
             'tax_rate_bps','tax_withholding_cents','pricing_snapshot_jsonb','settlement_mode'
           )
        """,
        13,
    ),
    (
        "system_settings 配置 4 项",
        """
        SELECT COUNT(*) AS c FROM system_settings
         WHERE key IN (
           'platform_fee_config','LEGACY_REFERRAL_V32_ENABLED',
           'V35_FACTORY_INVENTORY_ENABLED','agent_inventory_alert_config'
         )
        """,
        4,
    ),
    (
        "feature flag · V35_FACTORY_INVENTORY_ENABLED 启动 false",
        """
        SELECT COUNT(*) AS c FROM system_settings
         WHERE key='V35_FACTORY_INVENTORY_ENABLED' AND value='false'
        """,
        1,
    ),
    (
        "feature flag · LEGACY_REFERRAL_V32_ENABLED 启动 passthrough_30day",
        """
        SELECT COUNT(*) AS c FROM system_settings
         WHERE key='LEGACY_REFERRAL_V32_ENABLED' AND value='passthrough_30day'
        """,
        1,
    ),
    (
        "agent_inventory_wallets CHECK 约束(防负余额)",
        """
        SELECT COUNT(*) AS c FROM information_schema.check_constraints
         WHERE constraint_name LIKE '%agent_inventory_wallets%'
        """,
        3,
        False,  # 可选 verify · PostgreSQL CHECK 约束名可能不可预测
    ),
    (
        "agent_revenue_ledger 3 层费率字段",
        """
        SELECT COUNT(*) AS c FROM information_schema.columns
         WHERE table_name='agent_revenue_ledger'
           AND column_name IN (
             'gateway_fee_bps','gateway_fee_cents',
             'settlement_service_fee_bps','settlement_service_fee_cents',
             'tax_rate_bps','tax_withholding_cents',
             'agent_margin_before_tax_cents','agent_settlement_cents'
           )
        """,
        8,
    ),
    (
        "key 索引建立(快查)",
        """
        SELECT COUNT(*) AS c FROM pg_indexes
         WHERE indexname IN (
           'idx_users_referred_agent','idx_recharge_agent','idx_recharge_settlement_mode',
           'idx_sku_templates_type','idx_agent_sku_overrides_agent',
           'idx_agent_inv_tx_agent','idx_agent_inv_tx_order',
           'idx_customer_agent_credit_agent','idx_customer_credit_tx_customer',
           'idx_customer_credit_tx_order','idx_customer_bindings_agent',
           'idx_agent_revenue_agent','idx_agent_revenue_settle_due','idx_agent_revenue_order',
           'idx_settlement_requests_agent','idx_settlement_requests_status',
           'idx_settlement_items_request'
         )
        """,
        17,
    ),
]


def _run_sql_file(sql_path: Path, dry_run: bool = False):
    """跑 SQL 文件 · dry-run 强制 ROLLBACK"""
    if not sql_path.exists():
        logger.error(f"SQL 文件不存在: {sql_path}")
        sys.exit(2)

    sql = sql_path.read_text(encoding="utf-8")
    logger.info(f"读取 SQL: {sql_path.name} · {len(sql)} chars")

    with get_db() as conn:
        cur = conn.cursor()
        if dry_run:
            logger.info("⚙️ dry-run 模式 · 跑后强制 ROLLBACK")
            cur.execute("BEGIN")
        try:
            cur.execute(sql)
            if dry_run:
                cur.execute("ROLLBACK")
                logger.info("✅ dry-run PASS · 已 ROLLBACK · 不留改动")
            else:
                conn.commit()
                logger.info("✅ migration COMMIT")
        except Exception as e:
            if dry_run:
                cur.execute("ROLLBACK")
            logger.exception(f"❌ SQL 执行失败: {e}")
            raise


def _run_dry_run_combined(sql_paths):
    """[Codex r5 P1-1 修正] 多 SQL 文件在同一事务跑 dry-run · 一次性 ROLLBACK

    问题:之前分次 dry-run · 第一段(migration)ROLLBACK 后第二段(backfill)在干净库
    找不到 migration 新建的 sku_templates 表 · 直接报错

    修法:同事务跑全部 SQL · 最后 ROLLBACK · 保证依赖链 staging 上能验
    """
    if not sql_paths:
        return
    for sql_path in sql_paths:
        if not sql_path.exists():
            logger.error(f"SQL 文件不存在: {sql_path}")
            sys.exit(2)

    with get_db() as conn:
        cur = conn.cursor()
        logger.info(f"⚙️ dry-run 同事务 · {len(sql_paths)} 个 SQL 文件连续跑后 ROLLBACK")
        cur.execute("BEGIN")
        try:
            for sql_path in sql_paths:
                sql = sql_path.read_text(encoding="utf-8")
                logger.info(f"  读取 SQL: {sql_path.name} · {len(sql)} chars")
                cur.execute(sql)
                logger.info(f"  ✅ {sql_path.name} 执行 OK(事务内)")
            cur.execute("ROLLBACK")
            logger.info("✅ dry-run combined PASS · 已 ROLLBACK · 不留改动")
        except Exception as e:
            cur.execute("ROLLBACK")
            logger.exception(f"❌ dry-run combined 失败: {e}")
            raise


def _verify():
    """校验 migration 后的 schema 状态"""
    all_pass = True
    with get_db() as conn:
        cur = conn.cursor()
        for item in VERIFY_QUERIES:
            name = item[0]
            sql = item[1]
            expected = item[2]
            required = item[3] if len(item) > 3 else True

            try:
                cur.execute(sql)
                row = cur.fetchone()
                actual = row["c"] if isinstance(row, dict) else row[0]
            except Exception as e:
                logger.error(f"❌ {name} · 查询失败: {e}")
                if required:
                    all_pass = False
                continue

            if actual == expected:
                logger.info(f"✅ {name}: {actual}/{expected}")
            elif required:
                logger.error(f"❌ {name}: {actual}/{expected} · FAIL")
                all_pass = False
            else:
                logger.warning(f"⚠️  {name}: {actual}/{expected} · 可选 · 不阻断")

    if all_pass:
        logger.info("=" * 60)
        logger.info("✅ V3.5 migration verify ALL PASS")
        logger.info("=" * 60)
    else:
        logger.error("=" * 60)
        logger.error("❌ V3.5 migration verify FAIL · 检查上面 ❌ 项")
        logger.error("=" * 60)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rollback", action="store_true", help="回滚 migration")
    parser.add_argument("--verify", action="store_true", help="仅校验 schema · 不改")
    parser.add_argument("--dry-run", action="store_true", help="跑事务内 ROLLBACK · 验证语法")
    parser.add_argument("--backfill", action="store_true",
                        help="仅跑 backfill SQL (sku_templates 初始数据 + feature_pricing/mhz_media 出厂价)")
    parser.add_argument("--all", action="store_true",
                        help="跑 migration + backfill + verify(staging 推荐)")
    args = parser.parse_args()

    if args.verify:
        logger.info("🔍 verify 模式")
        _verify()
        return

    if args.rollback:
        logger.warning("⚠️ 回滚 V3.5 migration · 数据将丢失!")
        confirm = input("输入 'ROLLBACK' 确认:").strip()
        if confirm != "ROLLBACK":
            logger.info("取消")
            sys.exit(0)
        _run_sql_file(ROLLBACK_FILE, dry_run=False)
        return

    if args.backfill:
        logger.info("📦 backfill 模式 · 跑 SKU 模板 + 出厂价")
        _run_sql_file(BACKFILL_FILE, dry_run=args.dry_run)
        return

    if args.dry_run:
        # [Codex r5 P1-1] 同事务连续 dry-run · 否则 backfill 在干净 DB 找不到 migration 新表
        logger.info("🧪 dry-run 模式 · migration + backfill 同事务连续跑")
        _run_dry_run_combined([MIGRATION_FILE, BACKFILL_FILE])
        return

    if args.all:
        logger.info("🚀 跑 V3.5 migration + backfill + verify")
        _run_sql_file(MIGRATION_FILE, dry_run=False)
        logger.info("=" * 60)
        _run_sql_file(BACKFILL_FILE, dry_run=False)
        logger.info("=" * 60)
        _verify()
        return

    logger.info("🚀 跑 V3.5 migration(不含 backfill · 加 --all 自动跑 backfill+verify)")
    _run_sql_file(MIGRATION_FILE, dry_run=False)
    logger.info("=" * 60)
    logger.info("跑 verify ...")
    _verify()


if __name__ == "__main__":
    main()
