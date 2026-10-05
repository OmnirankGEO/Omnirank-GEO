"""
Channel tier incentive foundation migration runner.

Usage:
    python -m db.migrate_channel_tier
    python -m db.migrate_channel_tier --dry-run
    python -m db.migrate_channel_tier --verify
    python -m db.migrate_channel_tier --rollback
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.connection import get_db  # noqa: E402

logger = logging.getLogger("GEO-Migrate-ChannelTier")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
MIGRATION_FILE = SCRIPTS_DIR / "migration_channel_tier_2026_06_28.sql"
ROLLBACK_FILE = SCRIPTS_DIR / "rollback_channel_tier_2026_06_28.sql"

VERIFY_QUERIES = [
    (
        "4 channel tier tables",
        """
        SELECT COUNT(*) AS c FROM information_schema.tables
         WHERE table_name IN (
           'agent_channel_tier_state','agent_tier_change_log',
           'founder_seats','bonus_grants'
         )
        """,
        4,
    ),
    (
        "CHANNEL_TIER_ENABLED key exists",
        """
        SELECT COUNT(*) AS c FROM system_settings
         WHERE key='CHANNEL_TIER_ENABLED'
        """,
        1,
    ),
    (
        "bonus grant reconcile view",
        """
        SELECT COUNT(*) AS c FROM information_schema.views
         WHERE table_name='v_bonus_grant_reconcile'
        """,
        1,
    ),
    (
        "agent_channel_tier_state override columns",
        """
        SELECT COUNT(*) AS c
          FROM information_schema.columns
         WHERE table_name='agent_channel_tier_state'
           AND column_name IN (
             'tier_override','tier_override_until',
             'tier_override_by','tier_override_note'
           )
        """,
        4,
    ),
    (
        "bonus_grants grant_type CHECK supports customer/admin grants",
        """
        SELECT COUNT(*) AS c
          FROM pg_constraint con
          JOIN pg_class rel ON rel.oid = con.conrelid
         WHERE rel.relname = 'bonus_grants'
           AND con.contype = 'c'
           AND pg_get_constraintdef(con.oid) ILIKE '%%grant_type%%'
           AND pg_get_constraintdef(con.oid) ILIKE '%%customer_order_bonus%%'
           AND pg_get_constraintdef(con.oid) ILIKE '%%admin_adjust%%'
        """,
        1,
    ),
]


def _run_sql_file(sql_path: Path, dry_run: bool = False) -> None:
    if not sql_path.exists():
        logger.error("SQL file not found: %s", sql_path)
        sys.exit(2)
    sql = sql_path.read_text(encoding="utf-8")
    logger.info("Loading SQL %s (%s chars)", sql_path.name, len(sql))
    with get_db() as conn:
        cur = conn.cursor()
        if dry_run:
            logger.info("dry-run: wrapper transaction will be rolled back")
            cur.execute("BEGIN")
        try:
            cur.execute(sql)
            if dry_run:
                cur.execute("ROLLBACK")
                logger.info("dry-run passed and rolled back")
            else:
                conn.commit()
                logger.info("migration committed")
        except Exception:
            if dry_run:
                cur.execute("ROLLBACK")
            logger.exception("SQL execution failed")
            raise


def _verify() -> bool:
    ok = True
    with get_db() as conn:
        cur = conn.cursor()
        for name, sql, expected in VERIFY_QUERIES:
            cur.execute(sql)
            row = cur.fetchone()
            actual = row["c"] if isinstance(row, dict) else row[0]
            if int(actual) != int(expected):
                ok = False
                logger.error("%s expected=%s actual=%s", name, expected, actual)
            else:
                logger.info("%s OK (%s)", name, actual)
    return ok


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    if args.verify:
        if not _verify():
            sys.exit(1)
        return
    _run_sql_file(ROLLBACK_FILE if args.rollback else MIGRATION_FILE, dry_run=args.dry_run)
    if not args.rollback and not args.dry_run and not _verify():
        sys.exit(1)


if __name__ == "__main__":
    main()
