"""
数据库迁移脚本：添加知识库字段

运行方式：
    python scripts/migrate_add_knowledge_files.py
"""

import sqlite3
from pathlib import Path

# 数据库路径
DB_PATH = Path(__file__).parent.parent / "db" / "geo_diagnosis.db"


def migrate():
    """添加knowledge_files字段到client_profiles表"""
    print(f"📦 开始迁移数据库: {DB_PATH}")
    
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    # 检查字段是否已存在
    cursor.execute("PRAGMA table_info(client_profiles)")
    columns = [col[1] for col in cursor.fetchall()]
    
    if 'knowledge_files' in columns:
        print("✅ knowledge_files 字段已存在，跳过迁移")
        conn.close()
        return True
    
    # 添加字段
    try:
        cursor.execute("""
            ALTER TABLE client_profiles 
            ADD COLUMN knowledge_files TEXT
        """)
        conn.commit()
        print("✅ 成功添加 knowledge_files 字段")
    except Exception as e:
        print(f"❌ 迁移失败: {e}")
        conn.close()
        return False
    
    conn.close()
    return True


if __name__ == "__main__":
    success = migrate()
    exit(0 if success else 1)
