#!/usr/bin/env python3
"""
[CTO-15.23 2026-05-05] confirmed_keywords (quote_id, keyword) UNIQUE 约束 + 清污

背景:
  add-keyword endpoint 加防重前 · prod 已有重复数据
  老板:quote 287 有 6 条但应是 3 条(每 keyword 重复 2 次)
  根因:client 重复点击 / mark_paid 多次跑 / 早期无防重

修复 commit: 3740f38f (add-keyword endpoint API 层防重)
此脚本: 清污历史重复 + ADD CONSTRAINT UNIQUE 防 DB 级重复(双保险)

⚠️ FK 重定向(2f131702 之后老板反馈 P0):
  prod --apply 失败 · 因 confirmed_keywords.id 被以下 FK 引用:
    - topics.keyword_id (153 行)
    - monitoring_results.confirmed_keyword_id
  DELETE 重复行前必须 UPDATE FK 指向 keep_id · 否则 FK 拦截

操作步骤(单事务原子):
  1. 检查 UNIQUE 约束是否已存在(可重跑安全)
  2. 预览重复组
  3. 算 remap 临时表(old_id → keep_id)
  4. UPDATE topics.keyword_id = keep_id
  5. UPDATE monitoring_results.confirmed_keyword_id = keep_id
  6. DELETE confirmed_keywords WHERE id IN (重复非保留)
  7. UPDATE quotes.total_keywords -= 重复数(按 quote_id 分组)
  8. ALTER ADD CONSTRAINT UNIQUE (quote_id, keyword)
  失败任意一步 → ROLLBACK · 全部还原

用法:
  默认 dry-run:
    docker exec omnirank-ai python3 /app/scripts/migration_uniq_confirmed_keywords_2026_05_05.py

  真执行:
    docker exec omnirank-ai python3 /app/scripts/migration_uniq_confirmed_keywords_2026_05_05.py --apply
"""
import argparse
import sys
from datetime import datetime

sys.path.insert(0, '/app')

from db.connection import get_connection


CONSTRAINT_NAME = 'uq_ck_quote_keyword'


# 找重复(预览用)
PREVIEW_DUPS_SQL = """
SELECT quote_id, keyword, COUNT(*) AS dup_count, ARRAY_AGG(id ORDER BY id) AS ids
FROM confirmed_keywords
GROUP BY quote_id, keyword
HAVING COUNT(*) > 1
ORDER BY dup_count DESC, quote_id, keyword
"""

# 算 remap 临时表(old_id → keep_id) · keep_id 是每组最早 id
CREATE_REMAP_SQL = """
CREATE TEMP TABLE _ck_remap ON COMMIT DROP AS
WITH dups AS (
    SELECT MIN(id) AS keep_id, quote_id, keyword
    FROM confirmed_keywords
    GROUP BY quote_id, keyword
    HAVING COUNT(*) > 1
)
SELECT ck.id AS old_id, d.keep_id, d.quote_id
FROM confirmed_keywords ck
JOIN dups d
  ON ck.quote_id = d.quote_id
 AND ck.keyword = d.keyword
 AND ck.id != d.keep_id
"""

# FK 重定向 1:topics
REMAP_TOPICS_SQL = """
UPDATE topics
SET keyword_id = r.keep_id
FROM _ck_remap r
WHERE topics.keyword_id = r.old_id
"""

# FK 重定向 2:monitoring_results
REMAP_MONITORING_SQL = """
UPDATE monitoring_results
SET confirmed_keyword_id = r.keep_id
FROM _ck_remap r
WHERE monitoring_results.confirmed_keyword_id = r.old_id
"""

# DELETE 重复行(FK 已重定向 · 不会被拦)
DEDUP_DELETE_SQL = """
DELETE FROM confirmed_keywords
WHERE id IN (SELECT old_id FROM _ck_remap)
"""

# 同步 quotes.total_keywords -= 重复数
ADJUST_QUOTES_SQL = """
WITH dup_count_per_quote AS (
    SELECT quote_id, COUNT(*) AS extra_count
    FROM _ck_remap
    GROUP BY quote_id
)
UPDATE quotes
SET total_keywords = GREATEST(0, COALESCE(total_keywords, 0) - dc.extra_count)
FROM dup_count_per_quote dc
WHERE quotes.id = dc.quote_id
"""

ADD_CONSTRAINT_SQL = f"""
ALTER TABLE confirmed_keywords
ADD CONSTRAINT {CONSTRAINT_NAME} UNIQUE (quote_id, keyword)
"""

CHECK_CONSTRAINT_EXISTS_SQL = """
SELECT 1
FROM information_schema.table_constraints
WHERE table_name = 'confirmed_keywords'
  AND constraint_name = %s
  AND constraint_type = 'UNIQUE'
"""


def main():
    parser = argparse.ArgumentParser(description='confirmed_keywords UNIQUE migration with FK remap')
    parser.add_argument('--apply', action='store_true',
                        help='真执行 FK remap + DELETE + ALTER(默认仅 dry-run)')
    args = parser.parse_args()

    mode = '【真执行】' if args.apply else '【dry-run】'
    print(f"\n{'=' * 70}")
    print(f"  confirmed_keywords UNIQUE 约束 migration {mode}")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 70}\n")

    conn = get_connection()
    # 关键:整个 migration 走单事务(autocommit=False) · 失败回滚保证 prod 安全
    conn.autocommit = False
    try:
        cur = conn.cursor()

        # Step 0: 检查约束
        cur.execute(CHECK_CONSTRAINT_EXISTS_SQL, (CONSTRAINT_NAME,))
        constraint_exists = cur.fetchone() is not None
        if constraint_exists:
            print(f"✓ UNIQUE 约束 {CONSTRAINT_NAME} 已存在 · 跳过 ALTER")
            print(f"   (但仍会扫描重复 · 防有人 DROP CONSTRAINT 后又乱写)\n")

        # Step 1: 预览重复
        print("【Step 1】扫描重复数据 (quote_id, keyword)\n")
        cur.execute(PREVIEW_DUPS_SQL)
        dups = [dict(r) for r in cur.fetchall()]

        if not dups:
            print("✓ 无重复数据\n")
        else:
            total_extra = sum(d['dup_count'] - 1 for d in dups)
            print(f"⚠ 发现 {len(dups)} 组重复 · 共 {total_extra} 条多余记录\n")
            for d in dups[:15]:
                print(f"  quote_id={d['quote_id']} keyword=「{d['keyword']}」 "
                      f"重复 {d['dup_count']} 次 · ids={list(d['ids'])} "
                      f"(保留最早 id={min(d['ids'])})")
            if len(dups) > 15:
                print(f"  ... 还有 {len(dups) - 15} 组未展示")
            print()

        # Step 2: dry-run 时模拟 remap 看 FK 影响行数
        if not args.apply:
            if dups:
                # 创建临时 remap 表(只在事务内)
                cur.execute(CREATE_REMAP_SQL)
                cur.execute("SELECT COUNT(*) AS n FROM _ck_remap")
                remap_count = cur.fetchone()['n']
                # FK 影响预览
                cur.execute(
                    "SELECT COUNT(*) AS n FROM topics t JOIN _ck_remap r ON t.keyword_id = r.old_id"
                )
                topics_affected = cur.fetchone()['n']
                cur.execute(
                    "SELECT COUNT(*) AS n FROM monitoring_results m JOIN _ck_remap r ON m.confirmed_keyword_id = r.old_id"
                )
                mr_affected = cur.fetchone()['n']
                print(f"【Step 2】FK 影响预览(dry-run · 实际不会 commit):")
                print(f"  remap 行数: {remap_count}")
                print(f"  topics.keyword_id 待重定向: {topics_affected} 行")
                print(f"  monitoring_results.confirmed_keyword_id 待重定向: {mr_affected} 行")
                print(f"  confirmed_keywords 待删除: {remap_count} 行")
                print()
                conn.rollback()  # dry-run 干净撤销
            print(f"[dry-run] 加 --apply 真执行")
            if not constraint_exists and not dups:
                print(f"  注:无重复但缺 UNIQUE · --apply 会直接 ALTER")
            conn.close()
            return 0

        # ===== 真执行(单事务) =====
        if not dups and constraint_exists:
            print("✓ 已是终态(无重复 + 约束已加) · 无需执行")
            conn.close()
            return 0

        try:
            print(f"\n【Step 2】创建 remap 临时表(单事务)\n")
            cur.execute(CREATE_REMAP_SQL)
            cur.execute("SELECT COUNT(*) AS n FROM _ck_remap")
            remap_count = cur.fetchone()['n']
            print(f"  remap 行数: {remap_count}")

            if remap_count > 0:
                print(f"\n【Step 3】UPDATE topics.keyword_id = keep_id")
                cur.execute(REMAP_TOPICS_SQL)
                print(f"  ✓ topics 重定向 {cur.rowcount} 行")

                print(f"\n【Step 4】UPDATE monitoring_results.confirmed_keyword_id = keep_id")
                cur.execute(REMAP_MONITORING_SQL)
                print(f"  ✓ monitoring_results 重定向 {cur.rowcount} 行")

                print(f"\n【Step 5】DELETE confirmed_keywords 重复行")
                cur.execute(DEDUP_DELETE_SQL)
                deleted_count = cur.rowcount
                print(f"  ✓ DELETE {deleted_count} 行")

                print(f"\n【Step 6】UPDATE quotes.total_keywords -= 重复数")
                cur.execute(ADJUST_QUOTES_SQL)
                print(f"  ✓ quotes 同步 {cur.rowcount} 行")
            else:
                print("  无重复需清污 · 跳过 Step 3-6")

            if not constraint_exists:
                print(f"\n【Step 7】添加 UNIQUE 约束 {CONSTRAINT_NAME}")
                cur.execute(ADD_CONSTRAINT_SQL)
                print(f"  ✓ ALTER ADD CONSTRAINT")

            # 全部成功 → COMMIT
            conn.commit()
            print(f"\n{'=' * 70}")
            print(f"  ✅ 单事务 COMMIT 成功")
            print(f"{'=' * 70}\n")
        except Exception as e:
            conn.rollback()
            print(f"\n❌ 任意一步失败 · 单事务 ROLLBACK · 数据不变: {e}")
            import traceback
            traceback.print_exc()
            return 1

        # Step 8: 校验
        print(f"【Step 8】最终校验\n")
        cur.execute(PREVIEW_DUPS_SQL)
        remaining = cur.fetchall()
        if remaining:
            print(f"⚠ 仍有 {len(remaining)} 组重复(异常 · 请人工排查)")
            return 1
        else:
            print(f"✓ 0 重复")

        cur.execute(CHECK_CONSTRAINT_EXISTS_SQL, (CONSTRAINT_NAME,))
        if cur.fetchone():
            print(f"✓ UNIQUE 约束 {CONSTRAINT_NAME} 已生效")
        else:
            print(f"⚠ UNIQUE 约束未生效")
            return 1

        # 校验 FK 完整性(无孤儿)
        cur.execute("""
            SELECT COUNT(*) AS n
            FROM topics t
            LEFT JOIN confirmed_keywords ck ON t.keyword_id = ck.id
            WHERE t.keyword_id IS NOT NULL AND ck.id IS NULL
        """)
        orphan_topics = cur.fetchone()['n']
        cur.execute("""
            SELECT COUNT(*) AS n
            FROM monitoring_results m
            LEFT JOIN confirmed_keywords ck ON m.confirmed_keyword_id = ck.id
            WHERE m.confirmed_keyword_id IS NOT NULL AND ck.id IS NULL
        """)
        orphan_mr = cur.fetchone()['n']
        if orphan_topics or orphan_mr:
            print(f"⚠ 出现孤儿 FK · topics={orphan_topics} monitoring_results={orphan_mr}")
            return 1
        else:
            print(f"✓ FK 完整 · 0 孤儿")

        cur.close()
        conn.close()
        return 0
    except Exception as e:
        print(f"\n❌ 脚本异常: {e}")
        import traceback
        traceback.print_exc()
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return 1


if __name__ == '__main__':
    sys.exit(main())
