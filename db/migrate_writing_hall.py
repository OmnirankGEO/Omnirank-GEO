"""
数据库迁移: 写作大厅功能所需字段
"""
import sys
sys.path.insert(0, 'geo_agentscope')
from db.diagnosis_db import get_connection

def migrate():
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        print("🔄 开始数据库迁移...")
    
        # 1. quotes表增加writing_status字段
        try:
            cursor.execute("ALTER TABLE quotes ADD COLUMN writing_status TEXT DEFAULT 'pending'")
            print("✅ quotes.writing_status 已添加")
        except Exception as e:
            if "duplicate column" in str(e).lower():
                print("⏭️ quotes.writing_status 已存在")
            else:
                print(f"⚠️ quotes.writing_status: {e}")
    
        # 2. confirmed_keywords表增加字段
        try:
            cursor.execute("ALTER TABLE confirmed_keywords ADD COLUMN required_articles INTEGER DEFAULT 1")
            print("✅ confirmed_keywords.required_articles 已添加")
        except Exception as e:
            if "duplicate column" in str(e).lower():
                print("⏭️ confirmed_keywords.required_articles 已存在")
            else:
                print(f"⚠️ confirmed_keywords.required_articles: {e}")
    
        try:
            cursor.execute("ALTER TABLE confirmed_keywords ADD COLUMN recommended_platforms TEXT")
            print("✅ confirmed_keywords.recommended_platforms 已添加")
        except Exception as e:
            if "duplicate column" in str(e).lower():
                print("⏭️ confirmed_keywords.recommended_platforms 已存在")
            else:
                print(f"⚠️ confirmed_keywords.recommended_platforms: {e}")
    
        # 3. topics表增加字段
        try:
            cursor.execute("ALTER TABLE topics ADD COLUMN regenerate_count INTEGER DEFAULT 0")
            print("✅ topics.regenerate_count 已添加")
        except Exception as e:
            if "duplicate column" in str(e).lower():
                print("⏭️ topics.regenerate_count 已存在")
            else:
                print(f"⚠️ topics.regenerate_count: {e}")
    
        # 4. 创建范文库表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS reference_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            source_url TEXT,
            platform TEXT,
            industry TEXT,
            intent_type TEXT,
            analysis TEXT,
            success_proof TEXT,
            use_count INTEGER DEFAULT 0,
            success_rate REAL DEFAULT 0,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        print("✅ reference_articles 表已创建")
    
        # 5. 创建仿写记录表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS imitation_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference_id INTEGER,
            quote_id INTEGER,
            generated_count INTEGER,
            success_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (reference_id) REFERENCES reference_articles(id),
            FOREIGN KEY (quote_id) REFERENCES quotes(id)
        )
        """)
        print("✅ imitation_records 表已创建")
    
        # 6. 创建仿写文章表（存储仿写生成的文章）
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS imitated_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imitation_record_id INTEGER NOT NULL,
            reference_id INTEGER NOT NULL,
            quote_id INTEGER NOT NULL,
            keyword TEXT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            word_count INTEGER,
            status TEXT DEFAULT 'draft',
            revision_notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (imitation_record_id) REFERENCES imitation_records(id),
            FOREIGN KEY (reference_id) REFERENCES reference_articles(id),
            FOREIGN KEY (quote_id) REFERENCES quotes(id)
        )
        """)
        print("✅ imitated_articles 表已创建")
    
        # 7. reference_articles添加updated_at字段
        try:
            cursor.execute("ALTER TABLE reference_articles ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
            print("✅ reference_articles.updated_at 已添加")
        except Exception as e:
            if "duplicate column" in str(e).lower():
                print("⏭️ reference_articles.updated_at 已存在")
            else:
                print(f"⚠️ reference_articles.updated_at: {e}")
    
        conn.commit()
        conn.close()
        print("\n✅ 数据库迁移完成!")
    finally:
        try:
            conn.close()
        except Exception: pass

if __name__ == "__main__":
    migrate()
