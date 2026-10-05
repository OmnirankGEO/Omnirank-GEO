"""
数据库迁移 - 添加 client_profiles.structured_knowledge 列
修复：5维度结构化业务知识保存后刷新丢失的问题
"""

import os
import sys
import psycopg2
from dotenv import load_dotenv

# 加载 .env
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))


def get_connection():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set in .env")
    return psycopg2.connect(url)


def migrate():
    conn = get_connection()
    try:
        cursor = conn.cursor()

        print("🚀 开始迁移 - 添加 structured_knowledge 列...")

        # 检查列是否已存在
        cursor.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'client_profiles' AND column_name = 'structured_knowledge'
        """)
        if cursor.fetchone():
            print("⏭️  structured_knowledge 列已存在，跳过")
        else:
            cursor.execute("ALTER TABLE client_profiles ADD COLUMN structured_knowledge TEXT")
            conn.commit()
            print("✅ 添加 structured_knowledge TEXT 列成功")

        cursor.close()
        conn.close()
        print("✅ 迁移完成！")
    finally:
        try:
            conn.close()
        except Exception: pass


if __name__ == "__main__":
    migrate()
