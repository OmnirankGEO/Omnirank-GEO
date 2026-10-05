"""V3.5 W3 migration: agent_factory_agreements + recharge_orders.sku_template_id"""
import argparse, logging, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.connection import get_db  # noqa

logger = logging.getLogger("GEO-Migrate-V35-W3")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
MIGRATION_FILE = SCRIPTS_DIR / "migration_v35_w3_2026_05_26.sql"
ROLLBACK_FILE = SCRIPTS_DIR / "rollback_v35_w3_2026_05_26.sql"

VERIFY_QUERIES = [
    ("agent_factory_agreements 表",
     "SELECT COUNT(*) AS c FROM information_schema.tables WHERE table_name='agent_factory_agreements'",
     1),
    # [r12 P1] 列级断言 · 5 关键列(防表名同 schema 异)
    ("agent_factory_agreements 5 关键列",
     "SELECT COUNT(*) AS c FROM information_schema.columns "
     "WHERE table_name='agent_factory_agreements' "
     "AND column_name IN ('agent_user_id','status','content_hash','signed_ip','signed_ua')",
     5),
    # W1 资产 · W3 仅依赖(不拥)
    ("recharge_orders.sku_template_id(W1 资产 · W3 依赖)",
     "SELECT COUNT(*) AS c FROM information_schema.columns "
     "WHERE table_name='recharge_orders' AND column_name='sku_template_id'",
     1),
    # W3 独占索引
    ("idx_recharge_sku 索引",
     "SELECT COUNT(*) AS c FROM pg_indexes WHERE indexname='idx_recharge_sku'",
     1),
]


def _run_sql(sql_path: Path) -> None:
    logger.info(f"执行 {sql_path.name}")
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_path.read_text(encoding="utf-8"))
        conn.commit()
    logger.info(f"✓ {sql_path.name}")


def _run_dry_run(sql_path: Path) -> None:
    logger.info(f"DRY-RUN: {sql_path.name}")
    with get_db() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(sql_path.read_text(encoding="utf-8"))
            conn.rollback()
            logger.info(f"✓ DRY-RUN PASS · {sql_path.name}")
        except Exception:
            conn.rollback()
            raise


def _verify() -> bool:
    ok = True
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
                    ok = False
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.dry_run: _run_dry_run(MIGRATION_FILE); return 0
    if args.verify: return 0 if _verify() else 1
    if args.rollback: _run_sql(ROLLBACK_FILE); return 0
    _run_sql(MIGRATION_FILE)
    return 0 if _verify() else 1


if __name__ == "__main__":
    sys.exit(main())
