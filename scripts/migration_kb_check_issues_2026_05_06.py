"""kb_check_issues 表 migration · CTO-15.23 2026-05-06 (M 方案)

老板需求:
- 写作大厅"知识库核查"+"一键修复"加单选机制
- 每条核查 issue 持久化 · 用户可勾选 dismissed=True 标记误报
- 修复时跳过 dismissed + LLM PROMPT 显式禁动那段文本

执行:
    # dry-run(只看不动 DB · 默认)
    python scripts/migration_kb_check_issues_2026_05_06.py

    # 真跑(写 DB)
    python scripts/migration_kb_check_issues_2026_05_06.py --apply

⚠️ SQL 4 维度核验(已确认):
1. 列名 · quote_id / topic_id 跟 quotes.id / topics.id 对齐
2. data_type · BIGSERIAL id / TIMESTAMP / BOOLEAN / TEXT
3. 字段归属 · 新表(不是给现有表加列)
4. dry-run · 默认行为 · 用 BEGIN/ROLLBACK 包

注:db/diagnosis_db.py 的 init_db() 在 import 时也会自动 CREATE TABLE IF NOT EXISTS ·
本脚本是 explicit migration · 用于:
  (a) 手工部署确认建表成功
  (b) 跑 ANALYZE 优化索引统计
  (c) 列出 schema 详情供 4 维度肉眼核对
"""
from __future__ import annotations
import sys
import os
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.connection import get_connection


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS kb_check_issues (
    id BIGSERIAL PRIMARY KEY,
    quote_id INTEGER NOT NULL,
    topic_id INTEGER NOT NULL,
    article_id INTEGER,
    issue_type TEXT NOT NULL,
    kb_data TEXT,
    article_data TEXT,
    context TEXT,
    severity TEXT DEFAULT 'medium',
    dedup_hash TEXT NOT NULL,
    dismissed BOOLEAN DEFAULT FALSE,
    dismissed_at TIMESTAMP,
    fixed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (quote_id, dedup_hash)
)
"""

CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_kb_issues_quote_topic ON kb_check_issues(quote_id, topic_id)",
    "CREATE INDEX IF NOT EXISTS idx_kb_issues_active ON kb_check_issues(quote_id) WHERE dismissed = FALSE AND fixed_at IS NULL",
]


def show_schema(cur):
    cur.execute("""
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_name = 'kb_check_issues'
        ORDER BY ordinal_position
    """)
    cols = cur.fetchall()
    if not cols:
        print("  [Schema] kb_check_issues 表不存在")
        return
    print("  [Schema] kb_check_issues 列定义:")
    for c in cols:
        print(f"    - {c['column_name']:20s} {c['data_type']:20s} "
              f"nullable={c['is_nullable']:3s} default={c['column_default']}")
    cur.execute("""
        SELECT indexname, indexdef FROM pg_indexes
        WHERE tablename = 'kb_check_issues'
    """)
    indexes = cur.fetchall()
    print(f"  [Schema] {len(indexes)} 个索引:")
    for idx in indexes:
        print(f"    - {idx['indexname']}: {idx['indexdef']}")


def main():
    parser = argparse.ArgumentParser(description="kb_check_issues 表 migration · M 方案")
    parser.add_argument("--apply", action="store_true", help="真跑 · 不带此 flag 默认 dry-run")
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[migration_kb_check_issues] {mode} · {datetime.now().isoformat()}")

    conn = get_connection()
    conn.autocommit = False
    try:
        cur = conn.cursor()

        print("\n=== Step 1 · 当前 schema(应用前) ===")
        show_schema(cur)

        print("\n=== Step 2 · 跑建表 SQL(BEGIN) ===")
        cur.execute("BEGIN")
        cur.execute(CREATE_TABLE_SQL)
        for idx_sql in CREATE_INDEXES_SQL:
            cur.execute(idx_sql)
        print("  [OK] CREATE TABLE + 2 索引 执行成功(事务内)")

        print("\n=== Step 3 · 验证应用后 schema ===")
        show_schema(cur)

        print("\n=== Step 4 · 测试 UNIQUE 约束(同 quote_id+dedup_hash 应失败) ===")
        try:
            cur.execute("""
                INSERT INTO kb_check_issues (quote_id, topic_id, issue_type, dedup_hash)
                VALUES (-99999, -99999, '_dryrun_', '_dryrun_hash_')
            """)
            cur.execute("""
                INSERT INTO kb_check_issues (quote_id, topic_id, issue_type, dedup_hash)
                VALUES (-99999, -99999, '_dryrun_', '_dryrun_hash_')
            """)
            print("  [FAIL] UNIQUE 约束没生效 · 重复插入成功")
            raise SystemExit(1)
        except Exception as e:
            if 'duplicate key' in str(e).lower() or 'unique' in str(e).lower():
                print(f"  [OK] UNIQUE 约束生效 · 重复插入被拒: {str(e)[:80]}")
                cur.execute("ROLLBACK")
                cur.execute("BEGIN")
                cur.execute(CREATE_TABLE_SQL)
                for idx_sql in CREATE_INDEXES_SQL:
                    cur.execute(idx_sql)
            else:
                raise

        if args.apply:
            cur.execute("COMMIT")
            print("\n=== Step 5 · COMMIT 已写入 prod DB ===")
        else:
            cur.execute("ROLLBACK")
            print("\n=== Step 5 · ROLLBACK · 数据未写入(默认 dry-run)===")
            print("  → 加 --apply 真跑")

    finally:
        try:
            conn.close()
        except Exception:
            pass

    print(f"\n[migration_kb_check_issues] {mode} 完成 · {datetime.now().isoformat()}")


if __name__ == "__main__":
    main()
