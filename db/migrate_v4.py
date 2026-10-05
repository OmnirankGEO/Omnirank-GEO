"""
数据库迁移 v4 - 选题去重功能
添加选题历史追踪字段
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "geo_diagnosis.db"


def migrate():
    """执行v4迁移：选题去重功能"""
    print("🚀 开始 v4 迁移：选题去重功能...")
    
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    # 获取现有列
    cursor.execute("PRAGMA table_info(social_topics)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    
    # 1. 添加 title_hash 字段
    if "title_hash" not in existing_columns:
        try:
            cursor.execute("ALTER TABLE social_topics ADD COLUMN title_hash TEXT")
            print("  ✅ 添加 title_hash 字段")
        except Exception as e:
            print(f"  ⚠️ title_hash 字段添加失败: {e}")
    else:
        print("  ⏭️ title_hash 字段已存在")
    
    # 2. 添加 skipped_at 字段
    if "skipped_at" not in existing_columns:
        try:
            cursor.execute("ALTER TABLE social_topics ADD COLUMN skipped_at TIMESTAMP")
            print("  ✅ 添加 skipped_at 字段")
        except Exception as e:
            print(f"  ⚠️ skipped_at 字段添加失败: {e}")
    else:
        print("  ⏭️ skipped_at 字段已存在")
    
    # 3. 添加 view_count 字段
    if "view_count" not in existing_columns:
        try:
            cursor.execute("ALTER TABLE social_topics ADD COLUMN view_count INTEGER DEFAULT 0")
            print("  ✅ 添加 view_count 字段")
        except Exception as e:
            print(f"  ⚠️ view_count 字段添加失败: {e}")
    else:
        print("  ⏭️ view_count 字段已存在")
    
    # 4. 添加 last_shown_at 字段
    if "last_shown_at" not in existing_columns:
        try:
            cursor.execute("ALTER TABLE social_topics ADD COLUMN last_shown_at TIMESTAMP")
            print("  ✅ 添加 last_shown_at 字段")
        except Exception as e:
            print(f"  ⚠️ last_shown_at 字段添加失败: {e}")
    else:
        print("  ⏭️ last_shown_at 字段已存在")
    
    # 5. 为现有选题生成 title_hash
    print("  📝 为现有选题生成 title_hash...")
    cursor.execute("SELECT id, title FROM social_topics WHERE title_hash IS NULL")
    rows = cursor.fetchall()
    
    import hashlib
    updated = 0
    for row in rows:
        topic_id, title = row
        if title:
            title_hash = hashlib.md5(title.strip().lower().encode()).hexdigest()
            cursor.execute("UPDATE social_topics SET title_hash = ? WHERE id = ?", 
                          (title_hash, topic_id))
            updated += 1
    
    print(f"  ✅ 更新了 {updated} 个选题的 title_hash")
    
    # 6. 创建索引加速查询
    try:
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_topics_project_hash 
            ON social_topics(project_id, title_hash)
        """)
        print("  ✅ 创建索引 idx_topics_project_hash")
    except Exception as e:
        print(f"  ⚠️ 创建索引失败: {e}")
    
    try:
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_topics_project_status 
            ON social_topics(project_id, status)
        """)
        print("  ✅ 创建索引 idx_topics_project_status")
    except Exception as e:
        print(f"  ⚠️ 创建索引失败: {e}")
    
    # 7. 创建选题向量表（Phase 2）
    print("  📊 创建选题向量表...")
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS topic_embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER UNIQUE,
                project_id INTEGER,
                embedding TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (topic_id) REFERENCES social_topics(id)
            )
        """)
        print("  ✅ 创建 topic_embeddings 表")
    except Exception as e:
        print(f"  ⚠️ topic_embeddings 表创建失败: {e}")
    
    try:
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_topic_emb_project 
            ON topic_embeddings(project_id)
        """)
        print("  ✅ 创建索引 idx_topic_emb_project")
    except Exception as e:
        print(f"  ⚠️ 创建索引失败: {e}")
    
    conn.commit()
    conn.close()
    
    print("✅ v4 迁移（含Phase 2）完成！")


def verify():
    """验证迁移结果"""
    print("\n🔍 验证 v4 迁移...")
    
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    cursor.execute("PRAGMA table_info(social_topics)")
    columns = {row[1] for row in cursor.fetchall()}
    
    required = ["title_hash", "skipped_at", "view_count", "last_shown_at"]
    all_ok = True
    
    for col in required:
        if col in columns:
            print(f"  ✅ {col} 存在")
        else:
            print(f"  ❌ {col} 不存在")
            all_ok = False
    
    conn.close()
    return all_ok


if __name__ == "__main__":
    migrate()
    verify()
