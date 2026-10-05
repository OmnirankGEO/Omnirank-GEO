"""brand.is_test 回填脚本 · CTO-15.18 PM 干预 A.2

老板 Q3 裁决(2026-04-28):
- 真客户白名单(`is_test=false`): BRD-0094 / 0096 / 0099 / 0105 / 0107 / 0140 / 0152 / 0154 共 8 个
- 测试客户(`is_test=true`): BRD-0148 / 0270 / 0277 + 其他不在白名单的全部
- 名字含 "测试|test|_demo|_test|验收" substring 自动 true(双保险)

执行:
    # dry-run(只看不改)
    python scripts/migration_brand_is_test_backfill.py --dry-run

    # 实际跑(写 DB)
    python scripts/migration_brand_is_test_backfill.py

    # 强制覆盖 is_test_locked=true 的 brand(慎用)
    python scripts/migration_brand_is_test_backfill.py --force

⚠️ 红线:
- 跑前先 `docker exec omnirank-db pg_dump -U geo_admin -t brands geo_agentscope > backup_brands_$(date +%Y%m%d_%H%M).sql`
- 元指令 17:不动 schema 列名 / 不删数据 / 只 UPDATE is_test
"""
from __future__ import annotations
import sys
import os
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.connection import get_connection
from utils.is_test_brand import (
    REAL_CLIENT_BRAND_IDS,
    detect_is_test_for_existing_brand,
)


def main():
    parser = argparse.ArgumentParser(description="brand.is_test 回填脚本(老板 Q3 裁决 · 白名单)")
    parser.add_argument("--dry-run", action="store_true", help="只打印不写 DB")
    parser.add_argument("--force", action="store_true", help="强制覆盖 is_test_locked=true 的 brand(慎用)")
    args = parser.parse_args()

    print(f"[migration_brand_is_test_backfill] 开始 · {datetime.now().isoformat()}")
    print(f"  dry_run={args.dry_run} · force={args.force}")
    print(f"  真客户白名单(永远 is_test=false): {sorted(REAL_CLIENT_BRAND_IDS)}")

    conn = get_connection()
    try:
        cur = conn.cursor()

        # 拉所有 brand
        cur.execute("""
            SELECT id, name, COALESCE(is_test, FALSE) AS is_test, COALESCE(is_test_locked, FALSE) AS is_test_locked
            FROM brands
            ORDER BY id
        """)
        rows = cur.fetchall()

        print(f"[migration] 拉到 {len(rows)} 个 brand")

        to_set_false: list[dict] = []  # 真客户 → false
        to_set_true: list[dict] = []   # 测试 → true
        unchanged: list[dict] = []
        locked_skipped: list[dict] = []

        for row in rows:
            brand_id = row["id"]
            name = row["name"]
            current_is_test = bool(row["is_test"])
            locked = bool(row["is_test_locked"])

            if locked and not args.force:
                locked_skipped.append({"id": brand_id, "name": name, "is_test": current_is_test})
                continue

            target_is_test = detect_is_test_for_existing_brand(brand_id, name)

            if target_is_test == current_is_test:
                unchanged.append({"id": brand_id, "name": name, "is_test": current_is_test})
                continue

            if target_is_test:
                to_set_true.append({"id": brand_id, "name": name, "from": current_is_test})
            else:
                to_set_false.append({"id": brand_id, "name": name, "from": current_is_test})

        # 打印分组
        print(f"\n[migration] 分组结果:")
        print(f"  ✅ 真客户白名单(is_test=false): {len([r for r in rows if r['id'] in REAL_CLIENT_BRAND_IDS])} 个")
        print(f"  📝 待 SET is_test=true: {len(to_set_true)} 个")
        for r in to_set_true[:20]:
            print(f"      BRD-{r['id']:04d} | {r['name'][:50]}")
        if len(to_set_true) > 20:
            print(f"      ...还有 {len(to_set_true) - 20} 个")
        print(f"  📝 待 SET is_test=false(白名单生效): {len(to_set_false)} 个")
        for r in to_set_false:
            print(f"      BRD-{r['id']:04d} | {r['name'][:50]}")
        print(f"  ➖ 未变(已正确): {len(unchanged)} 个")
        print(f"  🔒 跳过(is_test_locked=true · 用 --force 覆盖): {len(locked_skipped)} 个")

        if args.dry_run:
            print(f"\n[migration] dry-run 模式 · 不写 DB · 结束")
            return

        # 实际执行
        if not (to_set_true or to_set_false):
            print(f"\n[migration] 无需变更 · 结束")
            return

        # 实际 UPDATE
        if to_set_true:
            ids_true = [r["id"] for r in to_set_true]
            placeholders = ",".join(["%s"] * len(ids_true))
            cur.execute(f"UPDATE brands SET is_test = TRUE WHERE id IN ({placeholders})", ids_true)
            print(f"  ✅ UPDATE is_test=TRUE 影响 {cur.rowcount} 行")

        if to_set_false:
            ids_false = [r["id"] for r in to_set_false]
            placeholders = ",".join(["%s"] * len(ids_false))
            cur.execute(f"UPDATE brands SET is_test = FALSE WHERE id IN ({placeholders})", ids_false)
            print(f"  ✅ UPDATE is_test=FALSE 影响 {cur.rowcount} 行")

        conn.commit()
        print(f"\n[migration] 提交完成 · {datetime.now().isoformat()}")

    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
