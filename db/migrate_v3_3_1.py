"""
V3.3.1 身份模型 migration 执行入口(Python wrapper)

用法:
    python -m db.migrate_v3_3_1            # 跑 migration_007_identity_v3_3_1.sql
    python -m db.migrate_v3_3_1 --rollback # 跑 rollback_007_identity_v3_3_1.sql
    python -m db.migrate_v3_3_1 --verify   # 仅校验(不改 schema)
    python -m db.migrate_v3_3_1 --dry-run  # 跑在事务里 ROLLBACK · 验证 SQL 语法

V3.3.1 规范:
- 幂等 · 重跑 0 ERROR
- 不允许 prod 直跑 · 必须 staging dry-run 通过
- feature flags 默认 OFF · 不影响生产

关联:
- docs/DECISION/身份模型V3_C端社媒_GEO邀请制_2026-05-11.md
- .planning/phases/07-social-studio-subscription/RED_LINES.md
"""

import logging
import os
import re
import sys
import argparse
from pathlib import Path

# 复用既有连接池
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.connection import get_db  # noqa

logger = logging.getLogger("GEO-Migrate-V3.3.1")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

MIGRATION_FILE = Path(__file__).parent / "migration_007_identity_v3_3_1.sql"
ROLLBACK_FILE = Path(__file__).parent / "rollback_007_identity_v3_3_1.sql"

VERIFY_QUERIES = [
    (
        "7 张新表",
        """
        SELECT COUNT(*) AS c FROM information_schema.tables
         WHERE table_name IN (
            'service_fee_records','service_fee_settlements',
            'service_fee_conversion_orders','service_fee_clawback_pending',
            'service_fee_conversion_quota','invite_codes','failed_service_fee_jobs'
         )
        """,
        7,
    ),
    (
        # Codex 六审 P0 + 七审 P1:source verify 拆"必选 2 表" + "可选 subscription_orders"
        # 防 recharge_orders.source 缺失 + subscription_orders.source 凑数 假通过
        "source 字段 · 必选(point_transactions + recharge_orders)",
        """
        SELECT COUNT(*) AS c FROM information_schema.columns
         WHERE table_name IN ('point_transactions','recharge_orders')
           AND column_name='source'
        """,
        2,
    ),
    (
        # 可选 · subscription_orders V3.1 未部时不存在 · count=0 时 warning(不算 FAIL)
        "source 字段 · 可选(subscription_orders)",
        """
        SELECT COUNT(*) AS c FROM information_schema.columns
         WHERE table_name='subscription_orders' AND column_name='source'
        """,
        1,
        False,  # required=False · count=0 仅 warning
    ),
    (
        "V3.3.1 feature flags",
        "SELECT COUNT(*) AS c FROM system_settings WHERE key LIKE 'V3_3_1_%'",
        7,
    ),
    (
        "service_fee_* settings",
        "SELECT COUNT(*) AS c FROM system_settings WHERE key LIKE 'service_fee_%' OR key='referral_bonus_rate'",
        11,
    ),
    (
        "UNIQUE 3 列约束",
        "SELECT COUNT(*) AS c FROM pg_indexes WHERE indexname='uniq_service_fee_order_type_user'",
        1,
    ),
    (
        "新 role(geo_user_basic / geo_agent_full / finance_reviewer)",
        "SELECT COUNT(*) AS c FROM roles WHERE name IN ('geo_user_basic','geo_agent_full','finance_reviewer')",
        3,
    ),
]


def run_sql_file(path: Path, dry_run: bool = False) -> None:
    """执行 SQL 文件

    Codex 反馈修复:dry-run 真正安全模式 · 不再"包外层事务 ROLLBACK"
    (因为 SQL 文件内有 COMMIT · 内层 COMMIT 会真实生效 · 外层 ROLLBACK 无效)

    dry-run 改成:**只解析 SQL 不执行** + 拒绝执行任何 DDL ·
    要求 staging 备份后再真实跑(非 dry-run 模式)
    """
    if not path.exists():
        raise FileNotFoundError(f"SQL file not found: {path}")
    sql = path.read_text(encoding="utf-8")
    logger.info("Running %s (%d chars) dry_run=%s", path.name, len(sql), dry_run)

    if dry_run:
        # 真正的 dry-run:仅 lint / parse · 不执行
        logger.warning("⚠️ dry-run 模式 · 不会执行任何 SQL")
        logger.warning("⚠️ 原因:SQL 文件内 COMMIT 会让外层包裹的 ROLLBACK 失效 · 无法真正回滚")
        logger.warning("⚠️ 正确流程:")
        logger.warning("⚠️   1. staging DB pg_dump 备份")
        logger.warning("⚠️   2. 不带 --dry-run 真实跑 migration")
        logger.warning("⚠️   3. python -m db.migrate_v3_3_1 --verify 校验")
        logger.warning("⚠️   4. 任何问题立即跑 db/rollback_007_identity_v3_3_1.sql")
        # 基础语法校验(行数 · 顶层 BEGIN/COMMIT 配对 · 不连 DB)
        # Codex 八审 P2 修:用 regex 只数独占一行的 BEGIN; / COMMIT;,
        # 排除 PL/pgSQL `DO $$ BEGIN ... END $$;` 块内的 BEGIN(那是块开始关键字 · 不是事务)
        top_begin = re.findall(r"(?im)^[\t ]*BEGIN[\t ]*;[\t ]*$", sql)
        top_commit = re.findall(r"(?im)^[\t ]*COMMIT[\t ]*;[\t ]*$", sql)
        n_begin = len(top_begin)
        n_commit = len(top_commit)
        n_do_blocks = len(re.findall(r"(?im)^[\t ]*DO[\t ]+\$\$", sql))
        logger.info("dry-run lint: lines=%d · 顶层 BEGIN=%d · COMMIT=%d · DO 块=%d",
                    sql.count("\n") + 1, n_begin, n_commit, n_do_blocks)
        if n_begin != n_commit:
            logger.error("dry-run lint: 顶层 BEGIN/COMMIT 数量不匹配(%d/%d)· 检查 SQL", n_begin, n_commit)
            raise RuntimeError(f"SQL lint: 顶层 BEGIN/COMMIT 不匹配 ({n_begin}/{n_commit})")
        logger.info("✅ dry-run lint PASS · 实际执行需去掉 --dry-run")
        return

    with get_db() as conn:
        conn.autocommit = False  # SQL 文件自己管 BEGIN/COMMIT
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
            logger.info("✅ %s 执行成功", path.name)
        except Exception as exc:
            conn.rollback()
            logger.exception("❌ %s 失败 · 已回滚:%s", path.name, exc)
            raise


def verify() -> bool:
    """跑全套验证 · 必选项全 PASS 返回 True · 可选项不达标仅 warning

    VERIFY_QUERIES 项格式:
        (name, sql, expected)             · required=True(default)
        (name, sql, expected, required)   · required=False 时仅 warning · 不影响返回
    """
    all_pass = True
    with get_db() as conn:
        with conn.cursor() as cur:
            for item in VERIFY_QUERIES:
                if len(item) == 4:
                    name, sql, expected, required = item
                else:
                    name, sql, expected = item
                    required = True
                cur.execute(sql)
                row = cur.fetchone()
                actual = row["c"] if isinstance(row, dict) else row[0]
                ok = actual >= expected
                if ok:
                    status = "✅"
                elif required:
                    status = "❌"
                else:
                    status = "⚠️"
                tag = " (optional)" if not required else ""
                logger.info("%s %s%s · expected ≥ %d · actual = %d",
                            status, name, tag, expected, actual)
                if not ok and required:
                    all_pass = False
    return all_pass


def main():
    parser = argparse.ArgumentParser(description="V3.3.1 身份模型 migration runner")
    parser.add_argument("--rollback", action="store_true", help="执行 rollback 脚本(危险)")
    parser.add_argument("--verify", action="store_true", help="仅校验 · 不改 schema")
    parser.add_argument("--dry-run", action="store_true", help="dry-run(staging 用 · 不适合 prod)")
    parser.add_argument("--force", action="store_true", help="跳过 prod 拦截(慎用)")
    args = parser.parse_args()

    # prod 安全拦截
    env = os.environ.get("APP_ENV", "dev")
    if env == "prod" and not args.verify and not args.force:
        logger.error("❌ APP_ENV=prod 禁止直接跑 migration · 请加 --force 并双签")
        sys.exit(2)

    if args.verify:
        ok = verify()
        sys.exit(0 if ok else 1)

    if args.rollback:
        logger.warning("⚠️ ROLLBACK 模式 · 5 分钟可逆 · 已确认?")
        run_sql_file(ROLLBACK_FILE, dry_run=False)
        return

    run_sql_file(MIGRATION_FILE, dry_run=args.dry_run)
    logger.info("跑 --verify 校验:")
    if verify():
        logger.info("🎉 V3.3.1 migration + verify 全部 PASS")
    else:
        logger.error("❌ V3.3.1 migration 完成但 verify 失败 · 请检查")
        sys.exit(1)


if __name__ == "__main__":
    main()
