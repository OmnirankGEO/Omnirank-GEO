"""
社媒操盘手 v3.0 数据库迁移脚本
将现有 social_projects 升级为 client_profiles 架构
"""

import sqlite3
import json
from datetime import datetime
from pathlib import Path

# 数据库路径
DB_DIR = Path(__file__).parent
DB_PATH = DB_DIR / "geo_diagnosis.db"


def get_connection():
    """获取数据库连接"""
    return sqlite3.connect(str(DB_PATH))


def migrate_v3():
    """执行 v3.0 迁移"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        print("🚀 开始 v3.0 数据库迁移...")
    
        # ========== 1. 创建 client_profiles 表（如果不存在则创建） ==========
        print("📋 1/5 创建 client_profiles 表...")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS client_profiles (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
            
                -- 🔗 关联诊断系统品牌
                brand_id INTEGER,                      -- 关联brands表
            
                -- 🏢 公司信息
                industry TEXT,
                business TEXT,
                products TEXT,                         -- JSON数组
                target_users TEXT,
                pain_points TEXT,                      -- JSON数组
                competitors TEXT,                      -- JSON数组
            
                -- 👤 IP人设
                persona_positioning TEXT,
                persona_tone TEXT,
                persona_catchphrases TEXT,             -- JSON数组
                persona_background TEXT,
                persona_golden_quotes TEXT,            -- JSON数组
            
                -- 📊 数据反馈闭环
                target_platforms TEXT,                 -- JSON数组 ["douyin", "xiaohongshu"]
                successful_patterns TEXT,              -- JSON: 历史成功套路
                negative_feedback TEXT,                -- JSON: 失败教训
                brand_constraints TEXT,                -- 品牌禁忌
            
                -- 🗑️ 档案管理
                is_deleted INTEGER DEFAULT 0,
                deleted_at TIMESTAMP,
            
                -- 元数据
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            
                FOREIGN KEY (brand_id) REFERENCES brands(id)
            )
        """)
    
        # ========== 2. 升级 advisors 表 ==========
        print("🧠 2/5 升级 advisors 表...")
    
        # 检查表是否存在
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='advisors'")
        if cursor.fetchone():
            # 表已存在，添加新字段
            new_columns = [
                ("specialty", "TEXT"),
                ("knowledge_base_path", "TEXT"),
                ("last_kb_update", "TIMESTAMP"),
            ]
            for col_name, col_type in new_columns:
                try:
                    cursor.execute(f"ALTER TABLE advisors ADD COLUMN {col_name} {col_type}")
                    print(f"   + 添加 {col_name} 字段")
                except sqlite3.OperationalError:
                    print(f"   - {col_name} 字段已存在")
        else:
            # 表不存在，创建新表
            cursor.execute("""
                CREATE TABLE advisors (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    specialty TEXT,
                    knowledge_base_path TEXT,
                    avatar_url TEXT,
                    is_active INTEGER DEFAULT 1,
                    last_kb_update TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            print("   + 创建 advisors 表")

    
        # ========== 3. 给 social_topics 添加新字段 ==========
        print("📝 3/5 升级 social_topics 表...")
        try:
            cursor.execute("ALTER TABLE social_topics ADD COLUMN profile_id TEXT")
        except sqlite3.OperationalError:
            print("   - profile_id 字段已存在")
    
        try:
            cursor.execute("ALTER TABLE social_topics ADD COLUMN advisor_id TEXT")
        except sqlite3.OperationalError:
            print("   - advisor_id 字段已存在")
    
        try:
            cursor.execute("ALTER TABLE social_topics ADD COLUMN update_source TEXT DEFAULT 'manual'")
        except sqlite3.OperationalError:
            print("   - update_source 字段已存在")
    
        # ========== 4. 给 social_scripts 添加新字段 ==========
        print("✍️ 4/5 升级 social_scripts 表...")
        try:
            cursor.execute("ALTER TABLE social_scripts ADD COLUMN profile_id TEXT")
        except sqlite3.OperationalError:
            print("   - profile_id 字段已存在")
    
        try:
            cursor.execute("ALTER TABLE social_scripts ADD COLUMN advisor_id TEXT")
        except sqlite3.OperationalError:
            print("   - advisor_id 字段已存在")
    
        try:
            cursor.execute("ALTER TABLE social_scripts ADD COLUMN update_source TEXT DEFAULT 'manual'")
        except sqlite3.OperationalError:
            print("   - update_source 字段已存在")
    
        # ========== 5. 给 social_materials 添加新字段 ==========
        print("📦 5/5 升级 social_materials 表...")
        try:
            cursor.execute("ALTER TABLE social_materials ADD COLUMN profile_id TEXT")
        except sqlite3.OperationalError:
            print("   - profile_id 字段已存在")
    
        try:
            cursor.execute("ALTER TABLE social_materials ADD COLUMN advisor_id TEXT")
        except sqlite3.OperationalError:
            print("   - advisor_id 字段已存在")
    
        try:
            cursor.execute("ALTER TABLE social_materials ADD COLUMN update_source TEXT DEFAULT 'ai_generated'")
        except sqlite3.OperationalError:
            print("   - update_source 字段已存在")
    
        try:
            cursor.execute("ALTER TABLE social_materials ADD COLUMN material_type TEXT DEFAULT 'video'")
        except sqlite3.OperationalError:
            print("   - material_type 字段已存在")
    
        conn.commit()
        conn.close()
    
        print("")
        print("=" * 50)
        print("✅ v3.0 数据库迁移完成！")
        print("=" * 50)
        print("")
        print("新增表：")
        print("  - client_profiles (客户档案表)")
        print("  - advisors (顾问表)")
        print("")
        print("升级表：")
        print("  - social_topics: +profile_id, +advisor_id, +update_source")
        print("  - social_scripts: +profile_id, +advisor_id, +update_source")
        print("  - social_materials: +profile_id, +advisor_id, +update_source, +material_type")
        print("")
    finally:
        try:
            conn.close()
        except Exception: pass


def verify_migration():
    """验证迁移结果"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        print("🔍 验证迁移结果...")
    
        # 检查 client_profiles 表
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='client_profiles'")
        if cursor.fetchone():
            print("  ✅ client_profiles 表存在")
        else:
            print("  ❌ client_profiles 表不存在")
    
        # 检查 advisors 表
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='advisors'")
        if cursor.fetchone():
            cursor.execute("SELECT COUNT(*) FROM advisors")
            count = cursor.fetchone()[0]
            print(f"  ✅ advisors 表存在，共 {count} 个顾问")
        else:
            print("  ❌ advisors 表不存在")
    
        # 检查 social_topics 新字段
        cursor.execute("PRAGMA table_info(social_topics)")
        columns = [col[1] for col in cursor.fetchall()]
        new_cols = ["profile_id", "advisor_id", "update_source"]
        for col in new_cols:
            if col in columns:
                print(f"  ✅ social_topics.{col} 字段存在")
            else:
                print(f"  ❌ social_topics.{col} 字段不存在")
    
        conn.close()
    finally:
        try:
            conn.close()
        except Exception: pass


if __name__ == "__main__":
    migrate_v3()
    print("")
    verify_migration()
