"""Migration 008 · 报价 LLM-first 改造支撑层 · Python wrapper

用法:
    python -m db.migrate_008_pricing_llm_first            # 跑 migration
    python -m db.migrate_008_pricing_llm_first --rollback # 回滚
    python -m db.migrate_008_pricing_llm_first --verify   # 校验

规范:
- 幂等 · 重跑 0 ERROR
- prod 必须 --force 跑(防误触)
- 不影响现有数据 · 仅 ADD COLUMN + ADD CONSTRAINT + INSERT system_settings
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.connection import get_db  # noqa: E402

logger = logging.getLogger("GEO-Migrate-008")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

MIGRATION_FILE = Path(__file__).parent / "migration_008_pricing_llm_first.sql"
ROLLBACK_FILE = Path(__file__).parent / "rollback_008_pricing_llm_first.sql"

VERIFY_QUERIES: list[tuple[str, str, int]] = [
    (
        "独立表 keyword_price_cache_llm 已建",
        """SELECT COUNT(*) AS c FROM information_schema.tables
            WHERE table_name='keyword_price_cache_llm'""",
        1,
    ),
    (
        "新表 should_quote 列存在",
        """SELECT COUNT(*) AS c FROM information_schema.columns
            WHERE table_name='keyword_price_cache_llm' AND column_name='should_quote'""",
        1,
    ),
    (
        "新表 business_scope_hash 列存在(防跨 scope 串台)",
        """SELECT COUNT(*) AS c FROM information_schema.columns
            WHERE table_name='keyword_price_cache_llm' AND column_name='business_scope_hash'""",
        1,
    ),
    (
        "新表 business_scope_hash NOT NULL DEFAULT 'no_scope'(round-4 P2 DB 硬约束)",
        """SELECT COUNT(*) AS c FROM information_schema.columns
            WHERE table_name='keyword_price_cache_llm'
              AND column_name='business_scope_hash'
              AND is_nullable='NO'
              AND column_default LIKE '%no_scope%'""",
        1,
    ),
    (
        "新表 UNIQUE(brand_name, keyword, business_scope_hash) 已建",
        """SELECT COUNT(*) AS c FROM pg_constraint
            WHERE conrelid='keyword_price_cache_llm'::regclass AND contype='u'""",
        1,
    ),
    (
        "老 keyword_price_cache 表 UNIQUE 未变(代码 rollback 安全)",
        """SELECT COUNT(*) AS c FROM pg_constraint
            WHERE conrelid='keyword_price_cache'::regclass
              AND contype='u'
              AND conname IN ('keyword_price_cache_brand_name_keyword_key',
                              'keyword_price_cache_brand_keyword_key')""",
        1,
    ),
    (
        "system_settings 5 个 LLM flag",
        """SELECT COUNT(*) AS c FROM system_settings WHERE key LIKE 'LLM_FIRST_PRICING%'""",
        5,
    ),
    (
        "LLM_FIRST_PRICING_ENABLED 默认 false",
        """SELECT COUNT(*) AS c FROM system_settings
            WHERE key='LLM_FIRST_PRICING_ENABLED' AND value='false'""",
        1,
    ),
]


def run_sql_file(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"SQL not found: {path}")
    sql = path.read_text(encoding="utf-8")
    logger.info("Running %s (%d chars)", path.name, len(sql))
    with get_db() as conn:
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
            logger.info("OK %s 执行成功", path.name)
        except Exception as exc:
            conn.rollback()
            logger.exception("FAIL %s · rollback: %s", path.name, exc)
            raise


def verify() -> bool:
    all_pass = True
    with get_db() as conn:
        with conn.cursor() as cur:
            for name, sql, expected in VERIFY_QUERIES:
                cur.execute(sql)
                row = cur.fetchone()
                actual = row["c"] if isinstance(row, dict) else row[0]
                ok = actual >= expected
                status = "OK" if ok else "FAIL"
                logger.info("%s %s · expected >= %d · actual = %d", status, name, expected, actual)
                if not ok:
                    all_pass = False
    return all_pass


def main() -> None:
    p = argparse.ArgumentParser(description="Migration 008 · pricing LLM-first")
    p.add_argument("--rollback", action="store_true")
    p.add_argument("--verify", action="store_true")
    p.add_argument("--force", action="store_true")
    args = p.parse_args()

    env = os.environ.get("APP_ENV", "dev")
    if env == "prod" and not args.verify and not args.force:
        logger.error("APP_ENV=prod 禁止直接跑 · 需 --force 双签")
        sys.exit(2)

    if args.verify:
        sys.exit(0 if verify() else 1)
    if args.rollback:
        logger.warning("ROLLBACK · 5 分钟可逆 · 已确认?")
        run_sql_file(ROLLBACK_FILE)
        return
    run_sql_file(MIGRATION_FILE)
    if verify():
        logger.info("Migration 008 + verify ALL PASS")
    else:
        logger.error("Migration 008 完成但 verify 失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
