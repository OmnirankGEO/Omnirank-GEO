"""
数据库迁移 v5 - 顾问对话记忆功能
创建对话会话表和消息历史表
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "geo_diagnosis.db"


def migrate():
    """执行迁移"""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    print("📦 开始迁移 v5: 顾问对话记忆功能...")
    
    # 1. 创建对话会话表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS advisor_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT UNIQUE NOT NULL,
            advisor_id TEXT NOT NULL,
            title TEXT,
            message_count INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    print("  ✅ 创建表 advisor_conversations")
    
    # 2. 创建对话消息表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS advisor_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            context TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (conversation_id) REFERENCES advisor_conversations(conversation_id)
        )
    """)
    print("  ✅ 创建表 advisor_messages")
    
    # 3. 创建索引
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_conversations_advisor 
        ON advisor_conversations(advisor_id)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_conversation 
        ON advisor_messages(conversation_id)
    """)
    print("  ✅ 创建索引")
    
    conn.commit()
    conn.close()
    
    print("✅ 迁移 v5 完成!")


if __name__ == "__main__":
    migrate()
