"""
添加 client_profiles 表的 3 个新字段迁移脚本
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "geo_diagnosis.db"

def migrate():
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    new_columns = [
        ("story_type", "TEXT"),
        ("differentiation", "TEXT"),
        ("content_direction", "TEXT"),
    ]
    
    print("🚀 开始数据库迁移 - 添加 IP 人设字段...")
    
    for col_name, col_type in new_columns:
        try:
            cursor.execute(f"ALTER TABLE client_profiles ADD COLUMN {col_name} {col_type}")
            print(f"✅ 添加 {col_name} 字段成功")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e):
                print(f"⏭️ {col_name} 字段已存在")
            else:
                print(f"❌ 添加 {col_name} 失败: {e}")
    
    conn.commit()
    conn.close()
    print("✅ 数据库迁移完成！")

if __name__ == "__main__":
    migrate()
