"""
CTO-15.23 2026-05-09 · 历史 monitoring_token_usage → llm_call_log 迁移

把过去 30 天 monitoring_token_usage 表的数据补写到 llm_call_log 表
让 admin LLM cost dashboard 一开就能看 7-30 天历史数据(对账 Moonshot/火山控制台)

用法:
    # dry run(看会迁多少行 · 不写入)
    python scripts/migrate_monitoring_token_usage_to_llm_log_2026_05_09.py --dry-run

    # 实际执行
    python scripts/migrate_monitoring_token_usage_to_llm_log_2026_05_09.py --execute --days 30

幂等性:
- WHERE NOT EXISTS 查重(同 created_at + platform + caller='monitoring' 不重复)
- 重复跑安全 · 不会双写
"""

import argparse
import sys
from pathlib import Path

# 让 script 在仓根跑能 import db.connection
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db.connection import get_connection


def migrate(days: int, dry_run: bool) -> int:
    """迁移最近 days 天数据 · 返回迁移行数"""
    sql_count = """
        SELECT COUNT(*) AS cnt
        FROM monitoring_token_usage mtu
        LEFT JOIN monitoring_tasks mt ON mtu.task_id = mt.id
        WHERE mtu.created_at >= NOW() - (%s || ' days')::INTERVAL
          AND NOT EXISTS (
            SELECT 1 FROM llm_call_log llm
            WHERE llm.created_at = mtu.created_at
              AND llm.platform = mtu.platform
              AND llm.caller = 'monitoring'
          )
    """
    sql_insert = """
        INSERT INTO llm_call_log
            (created_at, caller, platform, model,
             input_tokens, output_tokens, estimated_cost,
             brand_id, quote_id, success)
        SELECT
            mtu.created_at,
            'monitoring' AS caller,
            mtu.platform,
            NULL AS model,
            mtu.input_tokens,
            mtu.output_tokens,
            COALESCE(mtu.estimated_cost, 0)::numeric(10,6) AS estimated_cost,
            mt.brand_id,
            mt.quote_id,
            TRUE AS success
        FROM monitoring_token_usage mtu
        LEFT JOIN monitoring_tasks mt ON mtu.task_id = mt.id
        WHERE mtu.created_at >= NOW() - (%s || ' days')::INTERVAL
          AND NOT EXISTS (
            SELECT 1 FROM llm_call_log llm
            WHERE llm.created_at = mtu.created_at
              AND llm.platform = mtu.platform
              AND llm.caller = 'monitoring'
          )
    """

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql_count, (str(days),))
        count_row = cur.fetchone()
        will_migrate = int(count_row["cnt"]) if count_row else 0

        print(f"[migrate] 待迁 {will_migrate} 行(过去 {days} 天 · 排除已迁的)")

        if dry_run:
            print("[migrate] DRY-RUN 模式 · 不写入")
            return will_migrate

        if will_migrate == 0:
            print("[migrate] 无待迁数据 · 跳过")
            return 0

        cur.execute(sql_insert, (str(days),))
        affected = cur.rowcount
        conn.commit()
        print(f"[migrate] ✅ 写入 {affected} 行到 llm_call_log")
        return affected
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="迁移 monitoring_token_usage → llm_call_log")
    parser.add_argument("--days", type=int, default=30, help="迁移最近 N 天数据 (default 30)")
    parser.add_argument("--dry-run", action="store_true", help="只看不写")
    parser.add_argument("--execute", action="store_true", help="实际执行写入")
    args = parser.parse_args()

    if not args.dry_run and not args.execute:
        print("[error] 必须指定 --dry-run 或 --execute")
        sys.exit(1)

    if args.days < 1 or args.days > 365:
        print(f"[error] --days 必须在 [1, 365] · 实际 {args.days}")
        sys.exit(1)

    rows = migrate(days=args.days, dry_run=args.dry_run)
    sys.exit(0 if rows >= 0 else 1)


if __name__ == "__main__":
    main()
