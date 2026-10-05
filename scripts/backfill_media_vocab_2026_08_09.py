#!/usr/bin/env python3
"""
媒体词表存量回填 · WO_VOCAB_CONVERGENCE_2026-08-09 §3-L2(地域)+ §3-L5(价格档)

🔴 顺序铁律:**先落闸,再回填**。写入侧闸(tools/media_vocab_normalize 接进
   _upsert_media / _upsert_wemedia / publish_db.upsert_media / bulk_upsert_media /
   import_mhz_media)必须先上线,否则回填完下一次同步立刻把「综合全国」「海外」写回来,
   而你还以为已经收敛了。本脚本启动时会**自检闸是否在位**,不在位直接拒跑。

🔴 这是 UPDATE(不是 additive),纪律四条:
   1. 默认 --dry-run:不加 --apply 一行都不写
   2. --apply 前必须先 pg_dump 备份(脚本会检查 --backup-verified 标志,没有就拒跑)
   3. 每一批都在显式事务里跑,--dry-run 走 BEGIN … ROLLBACK 自证(真跑一遍 SQL 再回滚,
      拿到的是**真实受影响行数**,不是估算)
   4. 打印回滚 SQL(反向 UPDATE),贴进交付单

两件事(可用 --only 单独跑):
  area  : mhz_media.area   综合全国→全国 / 海外→全球
          mhz_wemedia.province 同上(该表无「全球」值,归一后会首次出现)
  slot  : mhz_media.listing_slot 从 resource_type_name / category 回填(原列值不动)

用法:
    python scripts/backfill_media_vocab_2026_08_09.py                    # dry-run 全部
    python scripts/backfill_media_vocab_2026_08_09.py --only slot        # dry-run 只看价格档
    python scripts/backfill_media_vocab_2026_08_09.py --apply --backup-verified
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.connection import get_connection  # noqa: E402
from tools.media_vocab_normalize import AREA_SYNONYMS, LISTING_SLOTS  # noqa: E402


# ---------------------------------------------------------------- 闸自检

def assert_write_gate_installed() -> None:
    """写入侧闸不在位就拒跑 —— 回填的前提是"以后不会再写回来"。

    判据打在**接线**上(写入函数的源码里真的调了归一),不是"模块 import 得到"
    —— 本仓踩过五次「函数写好了但没接进路径」。
    """
    import inspect
    from db import meijiehezi_db, publish_db

    checks = [
        ("db/meijiehezi_db._upsert_media", meijiehezi_db._upsert_media, "normalize_media_row"),
        ("db/meijiehezi_db._upsert_wemedia", meijiehezi_db._upsert_wemedia, "normalize_wemedia_row"),
        ("db/publish_db.upsert_media", publish_db.upsert_media, "detect_listing_slot"),
        ("db/publish_db.bulk_upsert_media", publish_db.bulk_upsert_media, "detect_listing_slot"),
    ]
    missing = []
    for label, func, needle in checks:
        try:
            src = inspect.getsource(func)
        except OSError:
            missing.append(f"{label}(取不到源码)")
            continue
        if needle not in src:
            missing.append(f"{label} 里没有 {needle}")

    # (开源版:第 5 个写入点是供应商价目表导入脚本,不在开源仓;这里只查上面四个。)
    if missing:
        print("🔴 写入侧闸不在位,拒绝回填(先落闸再回填,顺序不能反):")
        for m in missing:
            print("   -", m)
        sys.exit(2)
    print("✅ 写入侧闸自检通过(四个写入点都接了归一)")


# ---------------------------------------------------------------- SQL

AREA_STATEMENTS = [
    (
        "mhz_media.area",
        "UPDATE mhz_media SET area = %s WHERE trim(area) = %s",
        "UPDATE mhz_media SET area = %s WHERE trim(area) = %s   -- 回滚(⚠️ 会把本来就是目标值的行一并改回,见下方说明)",
    ),
    (
        "mhz_wemedia.province",
        "UPDATE mhz_wemedia SET province = %s WHERE trim(province) = %s",
        "UPDATE mhz_wemedia SET province = %s WHERE trim(province) = %s",
    ),
]

SLOT_SQL = """
UPDATE mhz_media
   SET listing_slot = COALESCE(
         NULLIF(CASE WHEN trim(COALESCE(resource_type_name,'')) = ANY(%(slots)s)
                     THEN trim(resource_type_name) END, ''),
         NULLIF(CASE WHEN trim(COALESCE(category,'')) = ANY(%(slots)s)
                     THEN trim(category) END, ''))
 WHERE listing_slot IS DISTINCT FROM COALESCE(
         NULLIF(CASE WHEN trim(COALESCE(resource_type_name,'')) = ANY(%(slots)s)
                     THEN trim(resource_type_name) END, ''),
         NULLIF(CASE WHEN trim(COALESCE(category,'')) = ANY(%(slots)s)
                     THEN trim(category) END, ''))
   AND (trim(COALESCE(resource_type_name,'')) = ANY(%(slots)s)
        OR trim(COALESCE(category,'')) = ANY(%(slots)s))
"""


def run(only: str, apply: bool) -> int:
    slots = sorted(LISTING_SLOTS)
    conn = get_connection()
    conn.autocommit = False
    total = 0
    try:
        cur = conn.cursor()

        if only in ("all", "area"):
            print("\n===== ① 地域同义归一(Owner 2026-08-09 批两组)=====")
            for label, sql, _rb in AREA_STATEMENTS:
                for src_value, dst_value in AREA_SYNONYMS.items():
                    cur.execute(sql, (dst_value, src_value))
                    n = cur.rowcount
                    total += n
                    print(f"  {label:24} 「{src_value}」→「{dst_value}」  {n} 行")

        if only in ("all", "slot"):
            print("\n===== ② 价格档回填 listing_slot(原列值不动)=====")
            cur.execute(SLOT_SQL, {"slots": slots})
            n = cur.rowcount
            total += n
            print(f"  mhz_media.listing_slot   回填 {n} 行(档位:{'/'.join(slots)})")
            cur.execute(
                "SELECT listing_slot, count(*) FROM mhz_media "
                "WHERE listing_slot IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
            )
            for row in cur.fetchall():
                v = row["listing_slot"] if isinstance(row, dict) else row[0]
                c = row["count"] if isinstance(row, dict) else row[1]
                print(f"      {v:8} {c} 行")

        if apply:
            conn.commit()
            print(f"\n✅ 已提交 · 共影响 {total} 行")
        else:
            conn.rollback()
            print(f"\n🔎 DRY-RUN 已回滚 · 若 --apply 将影响 {total} 行"
                  f"(以上行数是真跑一遍 SQL 拿到的 rowcount,不是估算)")
        return total
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def print_rollback_hints() -> None:
    print("""
────────────────── 回滚 ──────────────────
① 地域:反向 UPDATE 会把**本来就写作目标值**的行一并改回,不可逆地混进去。
   所以地域回滚的正确姿势是 **从 pg_dump 备份恢复这两列**,不是跑反向 UPDATE:
     pg_dump -U geo_admin -t mhz_media -t mhz_wemedia geo_agentscope > backup_YYYYMMDD_HHMM.sql
   (这正是 --apply 强制要求 --backup-verified 的原因。)
② 价格档:listing_slot 是新列,回滚 = 跑 db/rollback_031_media_listing_slot_2026_08_09.sql
   (先回退代码再删列,否则写入侧 INSERT 带该列会 500)。原列值从未被动过,零残留。
""")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写(默认 dry-run)")
    ap.add_argument("--backup-verified", action="store_true",
                    help="确认已 pg_dump 备份 mhz_media / mhz_wemedia(--apply 必需)")
    ap.add_argument("--only", choices=["all", "area", "slot"], default="all")
    args = ap.parse_args()

    if args.apply and not args.backup_verified:
        print("🔴 --apply 必须同时给 --backup-verified;先备份:")
        print("   docker exec omnirank-db pg_dump -U geo_admin -t mhz_media -t mhz_wemedia "
              "geo_agentscope > backup_mhz_$(date +%Y%m%d_%H%M).sql")
        sys.exit(2)

    assert_write_gate_installed()
    run(args.only, args.apply)
    print_rollback_hints()


if __name__ == "__main__":
    main()
