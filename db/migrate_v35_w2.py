"""
V3.5 W2 · 后台 + API + DTO 隔离 migration 执行入口

用法:
    python -m db.migrate_v35_w2            # 跑 migration
    python -m db.migrate_v35_w2 --rollback # 回滚
    python -m db.migrate_v35_w2 --verify   # 仅校验
    python -m db.migrate_v35_w2 --dry-run  # 跑在事务里 ROLLBACK

改动:
1. recharge_orders 加 order_type / source_token / binding_source 三列
2. agent_settlement_requests 加 wire_transfer_no 列
3. agent_revenue_ledger 加可提 items 索引
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.connection import get_db  # noqa

logger = logging.getLogger("GEO-Migrate-V35-W2")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
MIGRATION_FILE = SCRIPTS_DIR / "migration_v35_w2_2026_05_26.sql"
ROLLBACK_FILE = SCRIPTS_DIR / "rollback_v35_w2_2026_05_26.sql"

VERIFY_QUERIES = [
    (
        "recharge_orders 3 列",
        """
        SELECT COUNT(*) AS c FROM information_schema.columns
         WHERE table_name='recharge_orders'
           AND column_name IN ('order_type','source_token','binding_source')
        """,
        3,
    ),
    (
        "agent_settlement_requests 6 列(W2 扩)",
        """
        SELECT COUNT(*) AS c FROM information_schema.columns
         WHERE table_name='agent_settlement_requests'
           AND column_name IN ('wire_transfer_no','approved_by_user_id','approved_at',
                               'rejected_by_user_id','rejected_at','reject_reason')
        """,
        6,
    ),
    (
        "agent_revenue_ledger 可提索引",
        """
        SELECT COUNT(*) AS c FROM pg_indexes
         WHERE indexname='idx_agent_revenue_available'
        """,
        1,
    ),
]


def _run_sql(sql_path: Path) -> None:
    logger.info(f"执行 SQL: {sql_path.name}")
    sql = sql_path.read_text(encoding="utf-8")
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    logger.info(f"✓ {sql_path.name} 完成")


def _run_dry_run(sql_path: Path) -> None:
    logger.info(f"DRY-RUN: {sql_path.name}(事务内执行 + ROLLBACK)")
    sql = sql_path.read_text(encoding="utf-8")
    with get_db() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.rollback()
            logger.info(f"✓ DRY-RUN PASS · {sql_path.name}")
        except Exception:
            conn.rollback()
            raise


def _verify() -> bool:
    all_ok = True
    with get_db() as conn:
        for name, sql, expected in VERIFY_QUERIES:
            with conn.cursor() as cur:
                cur.execute(sql)
                row = cur.fetchone()
                actual = row[0] if isinstance(row, tuple) else (row.get("c") if row else 0)
                if actual == expected:
                    logger.info(f"✓ {name}: {actual}")
                else:
                    logger.error(f"✗ {name}: 期望 {expected} · 实际 {actual}")
                    all_ok = False
    return all_ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.dry_run:
        _run_dry_run(MIGRATION_FILE)
        return 0

    if args.verify:
        return 0 if _verify() else 1

    if args.rollback:
        _run_sql(ROLLBACK_FILE)
        return 0 if _verify() else 0

    _run_sql(MIGRATION_FILE)
    return 0 if _verify() else 1


if __name__ == "__main__":
    sys.exit(main())
