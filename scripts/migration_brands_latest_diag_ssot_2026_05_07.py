"""brands.latest_diagnosis_id + latest_score SSOT backfill · CTO-15.23 2026-05-07

体检师 v2 (P0-1): 13 行 brands.latest_score IS DISTINCT FROM diagnosis_records.total_score

根因:
v2 SSOT 同步代码(services/diagnosis_report_v2.py:418-435 旧版)只补 latest_score
不同步 latest_diagnosis_id · 当 v1 update_brand_stats 失败(被 except 吞)时:
  - latest_diagnosis_id 黏滞老 diag
  - latest_score 已被 v2 更新成新 funnel_total
  - JOIN ON dr.id = b.latest_diagnosis_id 拿老 diag 的 total_score
  → b.latest_score IS DISTINCT FROM dr.total_score 真值

修法 2 处:
1. 代码:services/diagnosis_report_v2.py 同步时加 latest_diagnosis_id 列(同 commit)
2. 数据:本脚本一次性 backfill 历史脏数据

执行:
    # dry-run(默认 · BEGIN/ROLLBACK 包)
    python scripts/migration_brands_latest_diag_ssot_2026_05_07.py

    # 真跑(COMMIT 写 DB)
    python scripts/migration_brands_latest_diag_ssot_2026_05_07.py --apply

⚠️ SQL 4 维度核验(已确认):
1. 列名 · brands.{latest_diagnosis_id, latest_score, updated_at} 全在
   diagnosis_records.{id, brand_id, total_score, created_at} 全在
2. data_type · INTEGER / TIMESTAMP 标准
3. 字段归属 · UPDATE...FROM 跨表 join 标准 PG 语法 · brand_id 在 diagnosis_records
4. dry-run · 已证 15 行待修(2026-05-07 prod) · UPDATE 后 verify 查询 0 行

输出:
- before:列出当前 mismatch 行数
- update:UPDATE 影响行数
- verify:UPDATE 后 mismatch 行数(必须 0)
"""
from __future__ import annotations
import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.connection import get_connection


VERIFY_SQL = """
SELECT COUNT(*) AS mismatched
FROM brands b
JOIN diagnosis_records dr ON dr.id = b.latest_diagnosis_id
WHERE b.latest_score IS NOT NULL
  AND b.latest_score IS DISTINCT FROM dr.total_score
"""

BACKFILL_SQL = """
UPDATE brands b
SET latest_diagnosis_id = sub.id,
    latest_score = sub.total_score,
    updated_at = CURRENT_TIMESTAMP
FROM (
    SELECT DISTINCT ON (brand_id) id, brand_id, total_score
    FROM diagnosis_records
    WHERE brand_id IS NOT NULL
    ORDER BY brand_id, created_at DESC
) sub
WHERE b.id = sub.brand_id
  AND (b.latest_diagnosis_id IS DISTINCT FROM sub.id
       OR b.latest_score IS DISTINCT FROM sub.total_score)
"""


def run(apply: bool):
    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute("BEGIN")

        cur.execute(VERIFY_SQL)
        before = cur.fetchone()
        before_count = before["mismatched"] if isinstance(before, dict) else before[0]
        print(f"[before] mismatch rows: {before_count}")

        cur.execute(BACKFILL_SQL)
        affected = cur.rowcount
        print(f"[update] affected rows: {affected}")

        cur.execute(VERIFY_SQL)
        after = cur.fetchone()
        after_count = after["mismatched"] if isinstance(after, dict) else after[0]
        print(f"[after]  mismatch rows: {after_count}")

        if after_count != 0:
            print("⚠️ verify FAILED · still has mismatch · ROLLBACK")
            cur.execute("ROLLBACK")
            return 1

        if apply:
            cur.execute("COMMIT")
            print("✅ COMMIT · backfill applied")
        else:
            cur.execute("ROLLBACK")
            print("ℹ️ dry-run · ROLLBACK · 加 --apply 真跑")
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真跑 COMMIT(默认 dry-run ROLLBACK)")
    args = parser.parse_args()
    sys.exit(run(args.apply))
