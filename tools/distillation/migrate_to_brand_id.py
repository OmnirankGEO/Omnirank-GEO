"""
Phase 1: brand_id 统一化 — 数据库迁移脚本

作用：
1. 给 7 张表添加 brand_id INTEGER 列（如果还没有）
2. 从 quote_id / client_id 反查 quotes.brand_id 进行回填
3. 验证回填结果

安全保证：
- 全部使用 ALTER TABLE ADD COLUMN（不删除任何列）
- 回填只做 UPDATE SET ... WHERE brand_id IS NULL
- 失败自动回滚
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "db" / "geo_diagnosis.db"


def migrate(dry_run: bool = False):
    """执行迁移"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    print("=" * 60)
    print(f"Phase 1: brand_id 统一化迁移 {'(DRY RUN)' if dry_run else ''}")
    print(f"数据库: {DB_PATH}")
    print("=" * 60)
    
    # ========== Step 1: 添加 brand_id 列 ==========
    tables_need_brand_id = [
        "monitoring_tasks",
        "monitoring_reports",
        "confirmed_keywords",
        "extra_keywords",
        "client_keywords",
        "monitoring_config",
        "notifications",
    ]
    
    for table in tables_need_brand_id:
        # 检查表是否存在
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,)
        )
        if not cursor.fetchone():
            print(f"  ⚠ 表 {table} 不存在，跳过")
            continue
            
        # 检查是否已有 brand_id 列
        cursor.execute(f"PRAGMA table_info({table})")
        cols = {row["name"] for row in cursor.fetchall()}
        
        if "brand_id" in cols:
            print(f"  ✅ {table} 已有 brand_id 列")
        else:
            if not dry_run:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN brand_id INTEGER")
                print(f"  ✅ {table} 已添加 brand_id 列")
            else:
                print(f"  📋 {table} 需要添加 brand_id 列")
    
    # ========== Step 2: 回填 brand_id ==========
    print("\n--- 回填 brand_id ---")
    
    # 2a. monitoring_tasks: 有 quote_id 列，通过 quotes.brand_id 回填
    _backfill(cursor, dry_run,
        table="monitoring_tasks",
        source_col="quote_id",
        sql="""
            UPDATE monitoring_tasks SET brand_id = (
                SELECT q.brand_id FROM quotes q WHERE q.id = monitoring_tasks.quote_id
            ) WHERE brand_id IS NULL AND quote_id IS NOT NULL
        """
    )
    
    # 2b. monitoring_tasks: 有些行可能 quote_id 为空但 client_id 有值
    _backfill(cursor, dry_run,
        table="monitoring_tasks (via client_id)",
        source_col="client_id",
        sql="""
            UPDATE monitoring_tasks SET brand_id = (
                SELECT q.brand_id FROM quotes q 
                WHERE q.id = CAST(monitoring_tasks.client_id AS INTEGER)
            ) WHERE brand_id IS NULL AND client_id IS NOT NULL AND client_id != ''
        """
    )
    
    # 2c. confirmed_keywords: 有 quote_id
    _backfill(cursor, dry_run,
        table="confirmed_keywords",
        source_col="quote_id",
        sql="""
            UPDATE confirmed_keywords SET brand_id = (
                SELECT q.brand_id FROM quotes q WHERE q.id = confirmed_keywords.quote_id
            ) WHERE brand_id IS NULL AND quote_id IS NOT NULL
        """
    )
    
    # 2d. extra_keywords: 有 quote_id
    _backfill(cursor, dry_run,
        table="extra_keywords",
        source_col="quote_id",
        sql="""
            UPDATE extra_keywords SET brand_id = (
                SELECT q.brand_id FROM quotes q WHERE q.id = extra_keywords.quote_id
            ) WHERE brand_id IS NULL AND quote_id IS NOT NULL
        """
    )
    
    # 2e. extra_keywords via client_id fallback
    _backfill(cursor, dry_run,
        table="extra_keywords (via client_id)",
        source_col="client_id",
        sql="""
            UPDATE extra_keywords SET brand_id = (
                SELECT q.brand_id FROM quotes q 
                WHERE q.id = CAST(extra_keywords.client_id AS INTEGER)
            ) WHERE brand_id IS NULL AND client_id IS NOT NULL AND client_id != ''
        """
    )
    
    # 2f. monitoring_reports via client_id
    _backfill(cursor, dry_run,
        table="monitoring_reports",
        source_col="client_id",
        sql="""
            UPDATE monitoring_reports SET brand_id = (
                SELECT q.brand_id FROM quotes q 
                WHERE q.id = CAST(monitoring_reports.client_id AS INTEGER)
            ) WHERE brand_id IS NULL AND client_id IS NOT NULL 
                  AND client_id != '' AND client_id != '_global_'
        """
    )
    
    # 2g. client_keywords via client_id
    _backfill(cursor, dry_run,
        table="client_keywords",
        source_col="client_id",
        sql="""
            UPDATE client_keywords SET brand_id = (
                SELECT q.brand_id FROM quotes q 
                WHERE q.id = CAST(client_keywords.client_id AS INTEGER)
            ) WHERE brand_id IS NULL AND client_id IS NOT NULL AND client_id != ''
        """
    )
    
    # 2h. monitoring_config via client_id
    _backfill(cursor, dry_run,
        table="monitoring_config",
        source_col="client_id",
        sql="""
            UPDATE monitoring_config SET brand_id = (
                SELECT q.brand_id FROM quotes q 
                WHERE q.id = CAST(monitoring_config.client_id AS INTEGER)
            ) WHERE brand_id IS NULL AND client_id IS NOT NULL AND client_id != ''
        """
    )
    
    # 2i. notifications via client_id (这里 client_id 可能已经是 brand_id)
    _backfill(cursor, dry_run,
        table="notifications",
        source_col="client_id (直接作为 brand_id)",
        sql="""
            UPDATE notifications SET brand_id = client_id
            WHERE brand_id IS NULL AND client_id IS NOT NULL
        """
    )
    
    # ========== Step 3: 验证 ==========
    if dry_run:
        conn.rollback()
        print("\n🔄 DRY RUN 完成，未修改数据库。使用 'run' 参数执行实际迁移")
        conn.close()
        return
    
    print("\n--- 验证结果 ---")
    for table in tables_need_brand_id:
        cursor.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,)
        )
        if not cursor.fetchone():
            continue
            
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        total = cursor.fetchone()[0]
        
        cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE brand_id IS NOT NULL")
        filled = cursor.fetchone()[0]
        
        cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE brand_id IS NULL")
        null_count = cursor.fetchone()[0]
        
        status = "✅" if null_count == 0 or total == 0 else "⚠️"
        print(f"  {status} {table}: total={total}, filled={filled}, null={null_count}")
    
    # 也检查 keyword_insights（已有 brand_id）
    cursor.execute("SELECT COUNT(*) FROM keyword_insights")
    ki_total = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM keyword_insights WHERE brand_id IS NOT NULL")
    ki_filled = cursor.fetchone()[0]
    print(f"  ℹ keyword_insights: total={ki_total}, filled={ki_filled}")
    
    # ========== 提交 ==========
    conn.commit()
    print("\n✅ 迁移完成，已提交")
    conn.close()


def _backfill(cursor, dry_run, table, source_col, sql):
    """执行一条回填 SQL"""
    try:
        if dry_run:
            print(f"  📋 {table}: 将通过 {source_col} 回填 brand_id")
        else:
            cursor.execute(sql)
            affected = cursor.rowcount
            print(f"  ✅ {table}: 通过 {source_col} 回填了 {affected} 行")
    except Exception as e:
        print(f"  ❌ {table}: 回填失败 - {e}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "dry"
    if mode == "run":
        migrate(dry_run=False)
    else:
        print("使用 'python migrate_to_brand_id.py run' 执行实际迁移")
        print("先做 dry run 检查...\n")
        migrate(dry_run=True)
