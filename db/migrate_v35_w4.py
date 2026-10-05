"""V3.5 W4 migration: disputes + allocations + audit runs/diffs"""
import argparse, logging, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.connection import get_db  # noqa

logger = logging.getLogger("GEO-Migrate-V35-W4")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
MIGRATION_FILE = SCRIPTS_DIR / "migration_v35_w4_2026_05_26.sql"
ROLLBACK_FILE = SCRIPTS_DIR / "rollback_v35_w4_2026_05_26.sql"

VERIFY = [
    ("W4 4 张表", """
        SELECT COUNT(*) AS c FROM information_schema.tables
         WHERE table_name IN ('customer_agent_binding_disputes',
                              'agent_inventory_allocations',
                              'inventory_audit_runs',
                              'inventory_audit_diffs')
    """, 4),
]


def _run(p: Path) -> None:
    with get_db() as c:
        with c.cursor() as cur: cur.execute(p.read_text(encoding="utf-8"))
        c.commit()
    logger.info(f"✓ {p.name}")


def _dry(p: Path) -> None:
    with get_db() as c:
        try:
            with c.cursor() as cur: cur.execute(p.read_text(encoding="utf-8"))
            c.rollback()
            logger.info(f"✓ DRY-RUN PASS · {p.name}")
        except Exception:
            c.rollback(); raise


def _verify() -> bool:
    ok = True
    with get_db() as c:
        for name, sql, expected in VERIFY:
            with c.cursor() as cur:
                cur.execute(sql)
                row = cur.fetchone()
                a = row[0] if isinstance(row, tuple) else (row.get("c") if row else 0)
                if a == expected: logger.info(f"✓ {name}: {a}")
                else: logger.error(f"✗ {name}: 期望 {expected} · 实际 {a}"); ok = False
    return ok


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--rollback", action="store_true")
    p.add_argument("--verify", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    if a.dry_run: _dry(MIGRATION_FILE); return 0
    if a.verify: return 0 if _verify() else 1
    if a.rollback: _run(ROLLBACK_FILE); return 0
    _run(MIGRATION_FILE)
    return 0 if _verify() else 1


if __name__ == "__main__":
    sys.exit(main())
