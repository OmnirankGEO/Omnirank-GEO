"""
社媒操盘手 - 项目数据库模块
管理客户项目、选题库、文案库等数据
"""

import json
import os
from datetime import datetime
from typing import Optional, List, Dict, Any
from pathlib import Path


# 数据库路径（同diagnosis_db.py）
DB_DIR = Path(__file__).parent
DB_PATH = DB_DIR / "geo_diagnosis.db"


def get_connection():
    """获取数据库连接"""
    from db.connection import get_connection as _pg_get_connection
    return _pg_get_connection()


def _column_exists(cursor, table: str, column: str) -> bool:
    """检查列是否存在（仅需读锁，不会阻塞其他查询）"""
    cursor.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
        (table, column)
    )
    return cursor.fetchone() is not None


def _safe_add_column(cursor, table: str, column: str, col_type: str):
    """安全添加列：先检查再 ALTER，避免不必要的排他锁"""
    if not _column_exists(cursor, table, column):
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            print(f"[Social DB] 已为 {table} 添加 {column} 字段")
        except Exception:
            pass  # 并发竞态：另一个 worker 已经添加了


def init_social_tables():
    """初始化社媒操盘手相关表"""
    conn = get_connection()
    try:
        conn.autocommit = True
        cursor = conn.cursor()
    
        # 社媒项目表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS social_projects (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,                -- 项目名称
                industry TEXT,                     -- 行业
                business TEXT,                     -- 主营业务
                target_audience TEXT,              -- 目标客户
                product_intro TEXT,                -- 产品服务介绍
                advisor_style TEXT DEFAULT '小黄编导',  -- 绑定的顾问风格
                status TEXT DEFAULT 'active',       -- 状态：active/archived
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    
        # 对标账号表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS social_benchmarks (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL,
                platform TEXT NOT NULL,            -- douyin/xiaohongshu/weixin
                account_id TEXT NOT NULL,          -- 平台账号ID (sec_user_id等)
                account_name TEXT,                 -- 账号昵称
                follower_count INTEGER DEFAULT 0,
                is_competitor INTEGER DEFAULT 0,   -- 是否竞品
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES social_projects(id)
            )
        """)
    
        # 选题库表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS social_topics (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL,
                title TEXT NOT NULL,               -- 选题标题
                topic_type TEXT,                   -- 选题类型：流量型/人设型/变现型
                content_type TEXT,                 -- 内容类型：教知识/讲观点/讲故事/晒过程
                user_level TEXT,                   -- 目标用户层级：L1-L5
                opening_type TEXT,                 -- 开篇类型
                source TEXT,                       -- 来源：manual/ai_generated/benchmark
                source_video_id TEXT,              -- 来源视频ID（如果是拆解得来）
                status TEXT DEFAULT 'pending',      -- 状态：pending/used/archived
                priority INTEGER DEFAULT 0,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                used_at TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES social_projects(id)
            )
        """)
    
        # 文案库表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS social_scripts (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL,
                topic_id INTEGER,                  -- 关联选题
                title TEXT NOT NULL,
                script_content TEXT NOT NULL,      -- 文案内容
                opening_type TEXT,                 -- 开篇类型
                content_type TEXT,
                word_count INTEGER DEFAULT 0,
                source TEXT,                       -- 来源：original/rewrite/ai_generated
                source_video_url TEXT,             -- 来源视频链接
                original_transcript TEXT,          -- 原视频转录（如果是仿写）
                status TEXT DEFAULT 'draft',        -- 状态：draft/approved/published
                publish_date DATE,
                performance_data TEXT,              -- 发布后效果数据JSON
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES social_projects(id),
                FOREIGN KEY (topic_id) REFERENCES social_topics(id)
            )
        """)
    
        # 素材库表（拆解的视频素材）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS social_materials (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                video_id TEXT NOT NULL,
                video_url TEXT,
                title TEXT,
                author_name TEXT,
                author_id TEXT,
                transcript TEXT,                   -- ASR转录
                opening_type TEXT,                 -- 开篇类型
                content_type TEXT,
                structure_analysis TEXT,           -- 结构分析JSON
                golden_phrases TEXT,               -- 金句JSON数组
                stats_digg INTEGER DEFAULT 0,
                stats_comment INTEGER DEFAULT 0,
                stats_share INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES social_projects(id)
            )
        """)
    
        # 人设指南表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS social_personas (
                id SERIAL PRIMARY KEY,
                project_id INTEGER UNIQUE,             -- 一个项目一个人设
                one_liner TEXT,                        -- 一句话人设
                visual_style TEXT,                     -- 视觉调性
                speaking_style TEXT,                   -- 口播风格
                target_audience TEXT,                  -- 目标人群描述
                content_pillars TEXT,                  -- 内容支柱JSON
                persona_details TEXT,                  -- 详细人设描述
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES social_projects(id)
            )
        """)
    
        # 文案版本历史表（v3.3新增）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS social_script_versions (
                id SERIAL PRIMARY KEY,
                script_id INTEGER NOT NULL,
                version_num INTEGER DEFAULT 1,
                content TEXT NOT NULL,
                change_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (script_id) REFERENCES social_scripts(id)
            )
        """)
    
        # 爆款框架表（v4.1 新增 — 三层上下文 Layer 3）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS viral_frameworks (
                id SERIAL PRIMARY KEY,
                project_id INTEGER,
                name TEXT NOT NULL,
                source_type TEXT DEFAULT 'breakdown',
                source_url TEXT,
                source_author TEXT,
                hook_formula TEXT,
                structure_template TEXT,
                rhythm_pattern TEXT,
                emotion_arc TEXT,
                cta_patterns TEXT,
                golden_phrases TEXT,
                full_framework TEXT,
                is_pinned BOOLEAN DEFAULT FALSE,
                is_archived BOOLEAN DEFAULT FALSE,
                use_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES social_projects(id)
            )
        """)

        # ========== 迁移：为 viral_frameworks 添加三层隔离字段 ==========
        for col, col_def in [
            ("scope", "TEXT DEFAULT 'personal'"),   # 'personal' | 'team' | 'global'
            ("user_id", "INTEGER"),                  # 创建者
            ("team_id", "INTEGER"),                  # 所属团队
        ]:
            _safe_add_column(cursor, "viral_frameworks", col, col_def)

        # ========== 迁移：为 social_topics 添加缺失字段 ==========
        for col, col_def in [
            ("title_hash", "TEXT"),
            ("view_count", "INTEGER DEFAULT 0"),
            ("skipped_at", "TIMESTAMP"),
            ("last_shown_at", "TIMESTAMP"),
            ("profile_id", "TEXT"),
            ("advisor_id", "TEXT"),
            ("update_source", "TEXT DEFAULT 'manual'"),
            ("confirmed_at", "TIMESTAMP"),
        ]:
            _safe_add_column(cursor, "social_topics", col, col_def)

        # 为选题去重添加唯一索引（防止 TOCTOU 竞态重复插入）
        try:
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_social_topics_project_title_hash
                ON social_topics (project_id, title_hash)
            """)
        except:
            pass

        # ========== 迁移：为 social_scripts 添加缺失字段 ==========
        for col, col_def in [
            ("is_filmed", "INTEGER DEFAULT 0"),
            ("profile_id", "TEXT"),
            ("advisor_id", "TEXT"),
            ("update_source", "TEXT DEFAULT 'manual'"),
            ("user_id", "TEXT"),  # 用户隔离：记录创建者
            # 质量元数据（追踪生成上下文）
            ("script_structure", "TEXT"),          # 使用的脚本结构（如"灵魂拷问""客户故事"）
            ("framework_id", "INTEGER"),           # 使用的爆款框架ID
            ("generation_metadata", "TEXT"),       # 生成上下文JSON（顾问ID、SK是否注入、corpus命中数等）
            ("platform", "TEXT"),                  # 目标平台（douyin/xiaohongshu/bilibili）
            ("original_video_data", "TEXT"),        # 仿写/拆解来源视频原始数据
        ]:
            _safe_add_column(cursor, "social_scripts", col, col_def)

        # 迁移：放宽 project_id 的 NOT NULL 约束（仿写可以不关联项目）
        try:
            cursor.execute("ALTER TABLE social_scripts ALTER COLUMN project_id DROP NOT NULL")
        except Exception:
            pass

        # ========== 迁移：为 social_materials 添加缺失字段 ==========
        for col, col_def in [
            ("profile_id", "TEXT"),
            ("advisor_id", "TEXT"),
            ("update_source", "TEXT DEFAULT 'ai_generated'"),
            ("material_type", "TEXT DEFAULT 'video'"),
        ]:
            _safe_add_column(cursor, "social_materials", col, col_def)

        # ========== 迁移：为 social_projects 添加 brand_id / profile_id 字段 ==========
        _safe_add_column(cursor, "social_projects", "brand_id", "INTEGER REFERENCES brands(id)")
        _safe_add_column(cursor, "social_projects", "profile_id", "TEXT REFERENCES client_profiles(id)")

        # ========== 迁移：为 client_profiles 添加统一人设字段 ==========
        _safe_add_column(cursor, "client_profiles", "visual_style", "TEXT DEFAULT ''")
        _safe_add_column(cursor, "client_profiles", "content_pillars", "TEXT DEFAULT '[]'")

        # ========== v4 客户管理重构：为社媒表加 brand_id 字段（数据隔离） ==========
        _safe_add_column(cursor, "social_topics", "brand_id", "INTEGER")
        _safe_add_column(cursor, "social_scripts", "brand_id", "INTEGER")
        _safe_add_column(cursor, "social_materials", "brand_id", "INTEGER")

        # 索引
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_social_topics_brand ON social_topics(brand_id) WHERE brand_id IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_social_scripts_brand ON social_scripts(brand_id) WHERE brand_id IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_social_materials_brand ON social_materials(brand_id) WHERE brand_id IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_social_projects_brand ON social_projects(brand_id) WHERE brand_id IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_social_projects_profile ON social_projects(profile_id) WHERE profile_id IS NOT NULL",
        ]:
            try:
                cursor.execute(idx_sql)
            except Exception:
                pass

        conn.close()
        print("[Social DB] 社媒项目表初始化完成")
    finally:
        try:
            conn.close()
        except Exception: pass


# ========== 顾问风格映射：显示名 ↔ 系统ID ==========
ADVISOR_STYLE_MAP = {
    "小黄编导": "huang-douyin",
    "薛辉老师": "xuehui",
    "舒老师": "teacher_shu",
    "舒老师(品牌)": "shu-branding",
}
# 反向映射
ADVISOR_ID_MAP = {v: k for k, v in ADVISOR_STYLE_MAP.items()}


def advisor_style_to_id(style_name: str) -> str:
    """将顾问显示名转换为系统ID（找不到则原样返回）"""
    return ADVISOR_STYLE_MAP.get(style_name, style_name)


def advisor_id_to_style(advisor_id: str) -> str:
    """将顾问系统ID转换为显示名（找不到则原样返回）"""
    return ADVISOR_ID_MAP.get(advisor_id, advisor_id)


# ========== 项目 CRUD ==========

def create_project(
    name: str,
    industry: str = None,
    business: str = None,
    target_audience: str = None,
    product_intro: str = None,
    advisor_style: str = "小黄编导",
    brand_id: int = None,
    profile_id: str = None
) -> int:
    """创建项目"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO social_projects
            (name, industry, business, target_audience, product_intro, advisor_style, brand_id, profile_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (name, industry, business, target_audience, product_intro, advisor_style, brand_id, profile_id))

        project_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return project_id
    finally:
        try:
            conn.close()
        except Exception: pass


def get_project(project_id: int) -> Optional[Dict]:
    """获取项目详情"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id, name, industry, business, target_audience, product_intro,
                   advisor_style, status, created_at, updated_at, brand_id, profile_id
            FROM social_projects WHERE id = %s
        """, (project_id,))

        row = cursor.fetchone()
        conn.close()

        if row:
            return {
                "id": row["id"],
                "name": row["name"],
                "industry": row["industry"],
                "business": row["business"],
                "target_audience": row["target_audience"],
                "product_intro": row["product_intro"],
                "advisor_style": row["advisor_style"],
                "advisor_id": advisor_style_to_id(row["advisor_style"] or "小黄编导"),
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "brand_id": row["brand_id"],
                "profile_id": row["profile_id"],
            }
        return None
    finally:
        try:
            conn.close()
        except Exception: pass


def get_project_with_profile(project_id: int) -> Optional[Dict]:
    """获取项目详情，并JOIN关联的client_profiles数据（如有profile_id）"""
    project = get_project(project_id)
    if not project:
        return None

    profile_id = project.get("profile_id")
    if profile_id:
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM client_profiles
                WHERE id = %s AND (is_deleted = 0 OR is_deleted IS NULL)
            """, (profile_id,))
            row = cursor.fetchone()
            if row:
                profile = dict(row)
                # 解析JSON字段
                for field in ("products", "pain_points", "competitors", "persona_catchphrases",
                              "persona_golden_quotes", "target_platforms"):
                    if profile.get(field) and isinstance(profile[field], str):
                        try:
                            profile[field] = json.loads(profile[field])
                        except (json.JSONDecodeError, TypeError):
                            pass
                # 合并profile数据到project（profile字段不覆盖project已有字段）
                project["_profile"] = profile
                # 用profile丰富project缺失的字段
                if not project.get("target_audience") and profile.get("target_users"):
                    project["target_audience"] = profile["target_users"]
                if not project.get("product_intro") and profile.get("products"):
                    project["product_intro"] = profile["products"] if isinstance(profile["products"], str) else json.dumps(profile["products"], ensure_ascii=False)
                if not project.get("business") and profile.get("business"):
                    project["business"] = profile["business"]
                if not project.get("industry") and profile.get("industry"):
                    project["industry"] = profile["industry"]
            conn.close()
        except Exception as e:
            print(f"[Social DB] 获取关联profile失败: {e}")

    return project


def list_projects(status: str = None, limit: int = 50, offset: int = 0, brand_ids: list = None) -> List[Dict]:
    """列出项目"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        query = "SELECT id, name, industry, status, created_at, brand_id, profile_id FROM social_projects"
        params = []
        conditions = []

        if status:
            conditions.append("status = %s")
            params.append(status)

        if brand_ids is not None:
            placeholders = ",".join(["%s"] * len(brand_ids))
            conditions.append(f"(brand_id IN ({placeholders}) OR brand_id IS NULL)")
            params.extend(brand_ids)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY updated_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        return [
            {"id": r["id"], "name": r["name"], "industry": r["industry"], "status": r["status"], "created_at": r["created_at"], "brand_id": r["brand_id"], "profile_id": r["profile_id"]}
            for r in rows
        ]
    finally:
        try:
            conn.close()
        except Exception: pass


def update_project(project_id: int, **kwargs) -> bool:
    """更新项目，自动同步业务字段到 client_profiles"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        allowed_fields = ["name", "industry", "business", "target_audience",
                          "product_intro", "advisor_style", "status", "brand_id", "profile_id"]
        updates = []
        params = []

        for field, value in kwargs.items():
            if field in allowed_fields:
                updates.append(f"{field} = %s")
                params.append(value)

        if not updates:
            return False

        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(project_id)

        cursor.execute(f"""
            UPDATE social_projects SET {', '.join(updates)} WHERE id = %s
        """, params)

        conn.commit()
        affected = cursor.rowcount
        conn.close()

        # 同步业务字段到 client_profiles
        _BUSINESS_FIELDS = {"industry", "business", "target_audience", "product_intro"}
        changed_business = {k: v for k, v in kwargs.items() if k in _BUSINESS_FIELDS}
        if changed_business:
            _sync_project_business_to_profile(project_id, changed_business)

        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def _sync_project_business_to_profile(project_id: int, changed: dict):
    """将 social_projects 的业务字段同步到 client_profiles"""
    try:
        project = get_project(project_id)
        if not project or not project.get("profile_id"):
            return
        from db.profile_db import update_profile
        # 字段映射：social_projects → client_profiles
        mapping = {
            "industry": "industry",
            "business": "business",
            "target_audience": "target_users",
            "product_intro": "products",
        }
        profile_update = {}
        for proj_field, prof_field in mapping.items():
            if proj_field in changed and changed[proj_field] is not None:
                val = changed[proj_field]
                # products 在 client_profiles 中是 JSON list
                if prof_field == "products" and isinstance(val, str):
                    val = [val]
                profile_update[prof_field] = val
        if profile_update:
            update_profile(project["profile_id"], **profile_update)
    except Exception as e:
        print(f"[Social DB] 同步业务字段到client_profiles失败: {e}")


def delete_project(project_id: int) -> bool:
    """删除项目（软删除）"""
    return update_project(project_id, status="archived")


# ========== 选题库 CRUD ==========

def add_topic(
    project_id: int,
    title: str,
    topic_type: str = None,
    content_type: str = None,
    user_level: str = None,
    opening_type: str = None,
    source: str = "manual",
    source_video_id: str = None
) -> int:
    """添加选题"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            INSERT INTO social_topics
            (project_id, title, topic_type, content_type, user_level,
             opening_type, source, source_video_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (project_id, title, topic_type, content_type, user_level,
              opening_type, source, source_video_id))

        topic_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return topic_id
    finally:
        try:
            conn.close()
        except Exception: pass


def list_topics(
    project_id: int,
    topic_type: str = None,
    status: str = None,
    limit: int = 50
) -> List[Dict]:
    """列出选题"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        query = "SELECT * FROM social_topics WHERE project_id = %s"
        params = [project_id]

        if topic_type:
            query += " AND topic_type = %s"
            params.append(topic_type)
        if status:
            query += " AND status = %s"
            params.append(status)

        query += " ORDER BY priority DESC, created_at DESC LIMIT %s"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


# ========== 选题去重功能 ==========

def check_topic_exists(project_id: int, title: str) -> Optional[Dict]:
    """检查选题是否已存在（基于标题hash）"""
    import hashlib
    title_hash = hashlib.md5(title.strip().lower().encode()).hexdigest()
    
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, status, created_at FROM social_topics
            WHERE project_id = %s AND title_hash = %s
        """, (project_id, title_hash))
        row = cursor.fetchone()
        conn.close()

        if row:
            return {"id": row["id"], "status": row["status"], "created_at": row["created_at"]}
        return None
    finally:
        try:
            conn.close()
        except Exception: pass


def get_excluded_topic_hashes(project_id: int) -> List[str]:
    """获取需要排除的选题hash列表（used/skipped/archived）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT title_hash FROM social_topics
            WHERE project_id = %s AND status IN ('used', 'skipped', 'archived')
        """, (project_id,))
        rows = cursor.fetchall()
        conn.close()
        return [r["title_hash"] for r in rows if r["title_hash"]]
    finally:
        try:
            conn.close()
        except Exception: pass


def get_pending_topic_hashes(project_id: int) -> List[str]:
    """获取待使用选题的hash列表（用于降低权重但不完全排除）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT title_hash FROM social_topics
            WHERE project_id = %s AND status = 'pending'
        """, (project_id,))
        rows = cursor.fetchall()
        conn.close()
        return [r["title_hash"] for r in rows if r["title_hash"]]
    finally:
        try:
            conn.close()
        except Exception: pass


def skip_topic(topic_id: int) -> bool:
    """标记选题为跳过状态"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE social_topics
            SET status = 'skipped', skipped_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (topic_id,))
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def use_topic(topic_id: int) -> bool:
    """标记选题为已使用状态"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE social_topics
            SET status = 'used', used_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (topic_id,))
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def restore_topic(topic_id: int) -> bool:
    """恢复选题为待使用状态（从skipped恢复）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE social_topics
            SET status = 'pending', skipped_at = NULL
            WHERE id = %s AND status = 'skipped'
        """, (topic_id,))
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def increment_topic_view(topic_id: int) -> None:
    """增加选题展示次数"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE social_topics
            SET view_count = COALESCE(view_count, 0) + 1,
                last_shown_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (topic_id,))
        conn.commit()
        conn.close()
    finally:
        try:
            conn.close()
        except Exception: pass


def add_topic_with_hash(
    project_id: int,
    title: str,
    topic_type: str = None,
    content_type: str = None,
    user_level: str = None,
    opening_type: str = None,
    source: str = "ai_generated",
    source_video_id: str = None
) -> Optional[int]:
    """添加选题（带去重检查，如果已存在返回None）
    使用 INSERT OR IGNORE + UNIQUE 索引避免 TOCTOU 竞态条件
    """
    import hashlib
    title_hash = hashlib.md5(title.strip().lower().encode()).hexdigest()

    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO social_topics
            (project_id, title, title_hash, topic_type, content_type, user_level,
             opening_type, source, source_video_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING id
        """, (project_id, title, title_hash, topic_type, content_type, user_level,
              opening_type, source, source_video_id))

        row = cursor.fetchone()
        if not row:
            # BUG-6-009: Must rollback before close — INSERT was issued but not committed
            conn.rollback()
            conn.close()
            return None  # 已存在，不重复添加

        topic_id = row["id"]
        conn.commit()
        conn.close()
        return topic_id
    finally:
        try:
            conn.close()
        except Exception: pass


def list_topics_grouped(project_id: int, limit: int = 100) -> Dict[str, List[Dict]]:
    """获取分组的选题列表（按状态分组）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            SELECT * FROM social_topics
            WHERE project_id = %s
            ORDER BY
                CASE status
                    WHEN 'pending' THEN 1
                    WHEN 'used' THEN 2
                    WHEN 'skipped' THEN 3
                    ELSE 4
                END,
                created_at DESC
            LIMIT %s
        """, (project_id, limit))

        rows = cursor.fetchall()
        conn.close()

        # 分组
        result = {
            "pending": [],
            "used": [],
            "skipped": [],
            "archived": []
        }

        for row in rows:
            topic = dict(row)
            status = topic.get("status", "pending")
            if status in result:
                result[status].append(topic)
    
        return result
    finally:
        try:
            conn.close()
        except Exception: pass


# ========== 文案库 CRUD ==========

def save_script(
    project_id: int,
    title: str,
    script_content: str,
    topic_id: int = None,
    opening_type: str = None,
    content_type: str = None,
    source: str = "original",
    source_video_url: str = None,
    original_transcript: str = None,
    user_id: str = None,
    original_video_data: str = None,
    # 质量元数据
    script_structure: str = None,
    framework_id: int = None,
    generation_metadata: str = None,
    platform: str = None,
    advisor_id: str = None,
    profile_id: str = None,
) -> int:
    """保存文案（含生成质量元数据）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        word_count = len(script_content) if script_content else 0

        cursor.execute("""
            INSERT INTO social_scripts
            (project_id, topic_id, title, script_content, opening_type, content_type,
             word_count, source, source_video_url, original_transcript, user_id, original_video_data,
             script_structure, framework_id, generation_metadata, platform, advisor_id, profile_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (project_id, topic_id, title, script_content, opening_type, content_type,
              word_count, source, source_video_url, original_transcript, user_id, original_video_data,
              script_structure, framework_id, generation_metadata, platform, advisor_id, profile_id))

        script_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return script_id
    finally:
        try:
            conn.close()
        except Exception: pass


def list_scripts(
    project_id: int,
    status: str = None,
    search: str = None,
    is_filmed: bool = None,
    source: str = None,
    content_type: str = None,
    opening_type: str = None,
    topic_id: int = None,
    date_range: str = None,  # '7d', '30d', 'all'
    limit: int = 50
) -> List[Dict]:
    """列出文案（支持多条件筛选）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        # 默认排除已归档的文案
        query = "SELECT * FROM social_scripts WHERE project_id = %s AND status != 'archived'"
        params = [project_id]

        if status:
            query += " AND status = %s"
            params.append(status)

        if search:
            query += " AND (title LIKE %s OR script_content LIKE %s)"
            params.extend([f"%{search}%", f"%{search}%"])

        if is_filmed is not None:
            query += " AND is_filmed = %s"
            params.append(1 if is_filmed else 0)

        if source:
            query += " AND source = %s"
            params.append(source)

        if content_type:
            query += " AND content_type = %s"
            params.append(content_type)

        if opening_type:
            query += " AND opening_type = %s"
            params.append(opening_type)

        if topic_id is not None:
            query += " AND topic_id = %s"
            params.append(topic_id)

        if date_range and date_range != 'all':
            if date_range == '7d':
                query += " AND created_at >= NOW() - INTERVAL '7 days'"
            elif date_range == '30d':
                query += " AND created_at >= NOW() - INTERVAL '30 days'"

        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass

def get_used_titles(project_id) -> List[str]:
    """获取文案库中所有已使用的标题（用于选题去重）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT title FROM social_scripts
            WHERE project_id = %s AND title IS NOT NULL AND title != ''
        """, (project_id,))
        rows = cursor.fetchall()
        conn.close()
        return [row["title"] for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def get_script(script_id: int) -> Optional[Dict]:
    """获取单个文案详情"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM social_scripts WHERE id = %s", (script_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return None
        conn.close()
        return dict(row)
    finally:
        try:
            conn.close()
        except Exception: pass


def update_script(script_id: int, **kwargs) -> bool:
    """更新文案内容"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        allowed_fields = ['title', 'script_content', 'opening_type', 'content_type', 
                          'status', 'publish_date', 'performance_data']
        updates = []
        values = []
    
        for key, value in kwargs.items():
            if key in allowed_fields and value is not None:
                updates.append(f"{key} = %s")
                values.append(value)

        if not updates:
            conn.close()
            return False

        updates.append("updated_at = CURRENT_TIMESTAMP")
        values.append(script_id)

        query = f"UPDATE social_scripts SET {', '.join(updates)} WHERE id = %s"
        cursor.execute(query, values)
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def update_script_performance(
    script_id: int,
    publish_url: str = None,
    views: int = 0,
    likes: int = 0,
    comments: int = 0,
    shares: int = 0,
    collects: int = 0,
    publish_date: str = None
) -> bool:
    """更新文案发布效果数据，高分脚本自动提取模式到 successful_patterns"""
    import json

    # 构建效果数据JSON
    performance_data = {
        "publish_url": publish_url,
        "views": views,
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "collects": collects,
        "recorded_at": datetime.now().isoformat()
    }

    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 更新文案状态为已发布，并记录效果数据
        cursor.execute("""
            UPDATE social_scripts
            SET status = 'published',
                publish_date = COALESCE(%s, publish_date, CURRENT_DATE),
                performance_data = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (publish_date, json.dumps(performance_data, ensure_ascii=False), script_id))

        conn.commit()
        affected = cursor.rowcount

        # 自动提取高分脚本模式 → successful_patterns
        if affected > 0:
            try:
                _auto_extract_pattern(cursor, conn, script_id, views, likes, comments, shares)
            except Exception as e:
                print(f"[PerformanceUpdate] 自动提取模式失败（不影响保存）: {e}")

            # v5 内容规划闭环：脚本发布效果回流到 plan_task + 生命周期账本
            try:
                cursor.execute(
                    """
                    SELECT id, profile_id, brand_id, platform
                    FROM social_scripts WHERE id = %s
                    """,
                    (script_id,),
                )
                script_meta = cursor.fetchone() or {}

                task = None
                try:
                    from db.plan_db import get_task_by_script_id, update_task_performance_by_script_id
                    task = get_task_by_script_id(script_id)
                    update_task_performance_by_script_id(
                        script_id,
                        performance_data,
                        status="published",
                        published_date=publish_date,
                    )
                except Exception as e:
                    print(f"[PerformanceUpdate] plan_task 效果回写失败（不影响保存）: {e}")

                try:
                    from db.social_lifecycle_db import log_event, record_metrics, record_publication
                    publication = record_publication(
                        script_id=script_id,
                        publish_url=publish_url,
                        platform=script_meta.get("platform"),
                        profile_id=script_meta.get("profile_id"),
                        brand_id=script_meta.get("brand_id"),
                        plan_id=task.get("plan_id") if task else None,
                        plan_task_id=task.get("id") if task else None,
                        published_at=publish_date,
                    )
                    publication_id = publication.get("id") if publication else None
                    record_metrics(
                        publication_id=publication_id,
                        script_id=script_id,
                        platform=script_meta.get("platform"),
                        views=views,
                        likes=likes,
                        comments=comments,
                        shares=shares,
                        collects=collects,
                        raw_data=performance_data,
                    )
                    log_event(
                        "script_published",
                        profile_id=script_meta.get("profile_id"),
                        brand_id=script_meta.get("brand_id"),
                        plan_id=task.get("plan_id") if task else None,
                        plan_task_id=task.get("id") if task else None,
                        script_id=script_id,
                        publication_id=publication_id,
                        payload=performance_data,
                    )
                except Exception as e:
                    print(f"[PerformanceUpdate] 生命周期账本写入失败（不影响保存）: {e}")
            except Exception as e:
                print(f"[PerformanceUpdate] 闭环回写失败（不影响保存）: {e}")

        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def _auto_extract_pattern(cursor, conn, script_id: int, views: int, likes: int, comments: int, shares: int):
    """当脚本效果达到阈值时，自动提取模式到 client_profiles.successful_patterns"""
    import json

    # 判断是否为高分脚本（播放>1000 或 互动率>10%）
    engagement = (likes + comments + shares)
    engagement_rate = (engagement / views * 100) if views > 0 else 0
    is_hit = views >= 1000 or engagement_rate >= 10

    if not is_hit:
        return

    # 获取脚本详情
    cursor.execute("""
        SELECT s.title, s.script_content, s.opening_type, s.content_type,
               s.script_structure, s.profile_id, s.advisor_id
        FROM social_scripts s WHERE s.id = %s
    """, (script_id,))
    script = cursor.fetchone()
    if not script or not script.get("profile_id"):
        return

    profile_id = script["profile_id"]

    # 构建模式描述
    pattern = {
        "title": script.get("title", ""),
        "content": (
            f"高播放脚本模式 — "
            f"结构:{script.get('script_structure', '未知')}, "
            f"类型:{script.get('content_type', '未知')}, "
            f"开篇:{script.get('opening_type', '未知')}, "
            f"效果:{views}播放/{likes}赞/{comments}评论"
        ),
        "tags": [script.get("content_type", ""), script.get("script_structure", "")],
        "metrics": {"views": views, "likes": likes, "comments": comments, "shares": shares},
        "extracted_at": datetime.now().isoformat(),
        "source": "auto",
    }

    # 读取现有 patterns
    cursor.execute("SELECT successful_patterns FROM client_profiles WHERE id = %s", (profile_id,))
    row = cursor.fetchone()
    if not row:
        return

    existing = []
    if row.get("successful_patterns"):
        try:
            existing = json.loads(row["successful_patterns"])
        except (json.JSONDecodeError, TypeError):
            existing = []

    # 去重：同标题不重复添加
    if any(p.get("title") == pattern["title"] for p in existing):
        return

    # 最多保留10条模式
    existing.append(pattern)
    if len(existing) > 10:
        existing = existing[-10:]

    cursor.execute(
        "UPDATE client_profiles SET successful_patterns = %s WHERE id = %s",
        (json.dumps(existing, ensure_ascii=False), profile_id)
    )
    conn.commit()
    print(f"[PerformanceUpdate] ✓ 自动提取高分模式: {pattern['title'][:30]}... → profile={profile_id}")


# ========== 版本历史 CRUD ==========

def save_script_version(
    script_id: int,
    content: str,
    change_reason: str = None
) -> int:
    """保存文案版本"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        # 获取当前最大版本号
        cursor.execute("""
            SELECT COALESCE(MAX(version_num), 0) + 1 AS next_version
            FROM social_script_versions
            WHERE script_id = %s
        """, (script_id,))
        next_version = cursor.fetchone()["next_version"]

        cursor.execute("""
            INSERT INTO social_script_versions (script_id, version_num, content, change_reason)
            VALUES (%s, %s, %s, %s)
            RETURNING id
        """, (script_id, next_version, content, change_reason))

        version_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return version_id
    finally:
        try:
            conn.close()
        except Exception: pass


def get_script_versions(script_id: int, limit: int = 20) -> List[Dict]:
    """获取文案版本历史"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            SELECT id, script_id, version_num, content, change_reason, created_at
            FROM social_script_versions
            WHERE script_id = %s
            ORDER BY version_num DESC
            LIMIT %s
        """, (script_id, limit))

        rows = cursor.fetchall()
        conn.close()

        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def revert_to_version(script_id: int, version_id: int) -> bool:
    """恢复到指定版本"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        # 获取目标版本内容
        cursor.execute("""
            SELECT content FROM social_script_versions
            WHERE id = %s AND script_id = %s
        """, (version_id, script_id))
        row = cursor.fetchone()

        if not row:
            conn.close()
            return False

        target_content = row["content"]

        # 先保存当前版本作为历史
        cursor.execute("SELECT script_content FROM social_scripts WHERE id = %s", (script_id,))
        current = cursor.fetchone()
        if current:
            save_script_version(script_id, current["script_content"], "恢复前自动备份")

        # 更新文案内容
        cursor.execute("""
            UPDATE social_scripts
            SET script_content = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (target_content, script_id))
    
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


# ========== 素材库 CRUD ==========

def save_material(
    project_id: int,
    platform: str,
    video_id: str,
    video_url: str = None,
    title: str = None,
    author_name: str = None,
    author_id: str = None,
    transcript: str = None,
    opening_type: str = None,
    content_type: str = None,
    structure_analysis: Dict = None,
    golden_phrases: List = None,
    stats: Dict = None
) -> int:
    """保存素材"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        stats = stats or {}
    
        cursor.execute("""
            INSERT INTO social_materials
            (project_id, platform, video_id, video_url, title, author_name, author_id,
             transcript, opening_type, content_type, structure_analysis, golden_phrases,
             stats_digg, stats_comment, stats_share)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            project_id, platform, video_id, video_url, title, author_name, author_id,
            transcript, opening_type, content_type,
            json.dumps(structure_analysis) if structure_analysis else None,
            json.dumps(golden_phrases) if golden_phrases else None,
            stats.get("digg", 0), stats.get("comment", 0), stats.get("share", 0)
        ))

        material_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return material_id
    finally:
        try:
            conn.close()
        except Exception: pass


def list_materials(project_id: int, platform: str = None, limit: int = 50) -> List[Dict]:
    """列出素材"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        query = "SELECT * FROM social_materials WHERE project_id = %s"
        params = [project_id]

        if platform:
            query += " AND platform = %s"
            params.append(platform)

        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        results = []
        for row in rows:
            item = dict(row)
            # 解析JSON字段
            if item.get("structure_analysis"):
                item["structure_analysis"] = json.loads(item["structure_analysis"])
            if item.get("golden_phrases"):
                item["golden_phrases"] = json.loads(item["golden_phrases"])
            results.append(item)
    
        return results
    finally:
        try:
            conn.close()
        except Exception: pass


def get_materials_for_content(profile_id: str = None, project_id: int = None, limit: int = 10) -> list:
    """Retrieve recent materials (golden phrases, structures) for content generation injection"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        query = """
            SELECT golden_phrases, opening_type, content_type, structure_analysis, title
            FROM social_materials
            WHERE golden_phrases IS NOT NULL AND golden_phrases != '[]'
        """
        params = []

        if project_id:
            query += " AND project_id = %s"
            params.append(project_id)
        elif profile_id:
            # Resolve project_id from profile_id
            try:
                project_info = get_or_create_project_for_profile(profile_id)
                if project_info.get("success") and project_info.get("project_id"):
                    query += " AND project_id = %s"
                    params.append(project_info["project_id"])
            except Exception:
                pass

        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        results = []
        for row in rows:
            item = dict(row)
            phrases = []
            try:
                phrases = json.loads(item['golden_phrases']) if item['golden_phrases'] else []
            except Exception:
                pass

            structure = {}
            try:
                structure = json.loads(item['structure_analysis']) if item['structure_analysis'] else {}
            except Exception:
                pass

            results.append({
                'golden_phrases': phrases,
                'opening_type': item.get('opening_type'),
                'content_type': item.get('content_type'),
                'structure': structure,
                'title': item.get('title'),
            })
        return results
    finally:
        try:
            conn.close()
        except Exception: pass


# ========== 项目统计 ==========

def get_project_stats(project_id: int) -> Dict:
    """获取项目统计"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        stats = {}
    
        # 选题数量
        cursor.execute("SELECT COUNT(*) AS cnt FROM social_topics WHERE project_id = %s", (project_id,))
        stats["topic_count"] = cursor.fetchone()["cnt"]

        # 文案数量
        cursor.execute("SELECT COUNT(*) AS cnt FROM social_scripts WHERE project_id = %s", (project_id,))
        stats["script_count"] = cursor.fetchone()["cnt"]

        # 素材数量
        cursor.execute("SELECT COUNT(*) AS cnt FROM social_materials WHERE project_id = %s", (project_id,))
        stats["material_count"] = cursor.fetchone()["cnt"]

        # 对标账号数量
        cursor.execute("SELECT COUNT(*) AS cnt FROM social_benchmarks WHERE project_id = %s", (project_id,))
        stats["benchmark_count"] = cursor.fetchone()["cnt"]
    
        conn.close()
        return stats
    finally:
        try:
            conn.close()
        except Exception: pass


# ========== 人设指南 CRUD ==========

def save_persona(
    project_id: int,
    one_liner: str = None,
    visual_style: str = None,
    speaking_style: str = None,
    target_audience: str = None,
    content_pillars: str = None,
    persona_details: str = None
) -> int:
    """保存或更新人设指南（一个项目一个人设）

    自动同步到 client_profiles 表，确保两处数据一致。
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 检查是否已存在
        cursor.execute("SELECT id FROM social_personas WHERE project_id = %s", (project_id,))
        existing = cursor.fetchone()

        if existing:
            # 更新
            cursor.execute("""
                UPDATE social_personas SET
                    one_liner = COALESCE(%s, one_liner),
                    visual_style = COALESCE(%s, visual_style),
                    speaking_style = COALESCE(%s, speaking_style),
                    target_audience = COALESCE(%s, target_audience),
                    content_pillars = COALESCE(%s, content_pillars),
                    persona_details = COALESCE(%s, persona_details),
                    updated_at = CURRENT_TIMESTAMP
                WHERE project_id = %s
            """, (one_liner, visual_style, speaking_style, target_audience,
                  content_pillars, persona_details, project_id))
            persona_id = existing["id"]
        else:
            # 新建
            cursor.execute("""
                INSERT INTO social_personas
                (project_id, one_liner, visual_style, speaking_style,
                 target_audience, content_pillars, persona_details)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (project_id, one_liner, visual_style, speaking_style,
                  target_audience, content_pillars, persona_details))
            persona_id = cursor.fetchone()["id"]

        conn.commit()
        conn.close()

        # 同步到 client_profiles（确保两处人设数据一致）
        _sync_persona_to_profile(project_id, one_liner, visual_style,
                                 speaking_style, target_audience,
                                 content_pillars, persona_details)

        return persona_id
    finally:
        try:
            conn.close()
        except Exception: pass


def _sync_persona_to_profile(
    project_id: int,
    one_liner: str = None,
    visual_style: str = None,
    speaking_style: str = None,
    target_audience: str = None,
    content_pillars: str = None,
    persona_details: str = None
):
    """内部函数：将 social_personas 数据同步到 client_profiles"""
    try:
        project = get_project(project_id)
        if not project or not project.get("profile_id"):
            return

        from db.profile_db import update_profile

        profile_update = {}
        if one_liner is not None:
            profile_update["persona_positioning"] = one_liner
        if visual_style is not None:
            profile_update["visual_style"] = visual_style
        if speaking_style is not None:
            profile_update["persona_tone"] = speaking_style
        if target_audience is not None:
            profile_update["target_users"] = target_audience
        if content_pillars is not None:
            try:
                pillars = json.loads(content_pillars)
            except (TypeError, json.JSONDecodeError):
                pillars = [content_pillars] if content_pillars else []
            profile_update["content_pillars"] = pillars
        if persona_details is not None:
            profile_update["persona_background"] = persona_details

        if profile_update:
            update_profile(project["profile_id"], **profile_update)
    except Exception as e:
        print(f"[Social DB] 同步人设到client_profiles失败: {e}")


def get_persona(project_id: int) -> Optional[Dict]:
    """获取项目人设指南"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            SELECT id, project_id, one_liner, visual_style, speaking_style,
                   target_audience, content_pillars, persona_details,
                   created_at, updated_at
            FROM social_personas WHERE project_id = %s
        """, (project_id,))

        row = cursor.fetchone()
        conn.close()

        if row:
            return {
                "id": row["id"],
                "project_id": row["project_id"],
                "one_liner": row["one_liner"],
                "visual_style": row["visual_style"],
                "speaking_style": row["speaking_style"],
                "target_audience": row["target_audience"],
                "content_pillars": row["content_pillars"],
                "persona_details": row["persona_details"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"]
            }
        return None
    finally:
        try:
            conn.close()
        except Exception: pass


# ========== 品牌关联项目 ==========


def get_or_create_project_for_profile(profile_id: str) -> Dict[str, Any]:
    """
    根据档案ID获取或创建社媒项目
    通过profile关联的company_name来找项目，如果没有则创建
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        # 获取档案信息（含 brand_id，用于项目品牌关联）
        cursor.execute("""
            SELECT id, name, industry, brand_id FROM client_profiles WHERE id = %s
        """, (profile_id,))
        profile = cursor.fetchone()

        if not profile:
            return {"success": False, "error": "档案不存在"}

        company_name = profile["name"] or f"客户-{profile_id[:8]}"
        industry = profile["industry"] or ""
        profile_brand_id = profile.get("brand_id")

        # 首先按profile_id精确查找已关联的项目
        cursor.execute("""
            SELECT id, name, industry, business, status, created_at
            FROM social_projects WHERE profile_id = %s AND status = 'active'
            ORDER BY created_at DESC LIMIT 1
        """, (profile_id,))
        project = cursor.fetchone()

        # 降级：按名称模糊匹配
        if not project:
            cursor.execute("""
                SELECT id, name, industry, business, status, created_at
                FROM social_projects WHERE name LIKE %s AND status = 'active'
                ORDER BY created_at DESC LIMIT 1
            """, (f"%{company_name}%",))
            project = cursor.fetchone()
            # 补上 profile_id 和 brand_id 关联
            if project:
                cursor.execute("""
                    UPDATE social_projects
                    SET profile_id = COALESCE(profile_id, %s),
                        brand_id = COALESCE(brand_id, %s)
                    WHERE id = %s AND (profile_id IS NULL OR brand_id IS NULL)
                """, (profile_id, profile_brand_id, project["id"]))
                conn.commit()

        if project:
            return {
                "success": True,
                "exists": True,
                "project": {
                    "id": project["id"],
                    "name": project["name"],
                    "industry": project["industry"],
                    "business": project["business"],
                    "status": project["status"],
                    "created_at": project["created_at"],
                    "profile_id": profile_id
                }
            }

        # 创建新项目（关联 profile_id + brand_id）
        cursor.execute("""
            INSERT INTO social_projects (name, industry, profile_id, brand_id, status, created_at, updated_at)
            VALUES (%s, %s, %s, %s, 'active', NOW(), NOW())
            RETURNING id
        """, (f"{company_name} - 社媒项目", industry, profile_id, profile_brand_id))
        new_id = cursor.fetchone()["id"]

        # 同步设置 brands.social_enabled = true
        if profile_brand_id:
            cursor.execute("UPDATE brands SET social_enabled = TRUE WHERE id = %s", (profile_brand_id,))

        conn.commit()

        return {
            "success": True,
            "exists": False,
            "project": {
                "id": new_id,
                "name": f"{company_name} - 社媒项目",
                "industry": industry,
                "profile_id": profile_id,
                "created_at": datetime.now().isoformat()
            }
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        conn.close()


def update_script_filmed(script_id: int, is_filmed: bool) -> bool:
    """更新文案拍摄状态"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            UPDATE social_scripts
            SET is_filmed = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (1 if is_filmed else 0, script_id))
    
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


# ========== 爆款框架 CRUD ==========

def save_framework(
    project_id: int,
    name: str,
    source_type: str = "breakdown",
    source_url: str = None,
    source_author: str = None,
    hook_formula: str = None,
    structure_template: str = None,
    rhythm_pattern: str = None,
    emotion_arc: str = None,
    cta_patterns: str = None,
    golden_phrases: str = None,
    full_framework: str = None,
    scope: str = "personal",
    user_id: int = None,
    team_id: int = None,
) -> int:
    """保存爆款框架，返回 ID"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO viral_frameworks
            (project_id, name, source_type, source_url, source_author,
             hook_formula, structure_template, rhythm_pattern, emotion_arc,
             cta_patterns, golden_phrases, full_framework,
             scope, user_id, team_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (project_id, name, source_type, source_url, source_author,
              hook_formula, structure_template, rhythm_pattern, emotion_arc,
              cta_patterns, golden_phrases, full_framework,
              scope, user_id, team_id))
        framework_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return framework_id
    finally:
        try:
            conn.close()
        except Exception: pass


def get_framework(framework_id: int) -> Optional[Dict]:
    """获取单个框架"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM viral_frameworks WHERE id = %s", (framework_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception: pass


def list_frameworks(
    project_id: int,
    include_archived: bool = False,
    user_id: int = None,
    team_id: int = None,
) -> List[Dict]:
    """列出项目的爆款框架（三层隔离：个人/团队/全局）

    可见性规则：
    - personal: 仅创建者可见
    - team: 同团队成员可见
    - global: 项目内所有人可见
    - scope 为空（历史数据）: 视为 global
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        query = "SELECT * FROM viral_frameworks WHERE project_id = %s"
        params: list = [project_id]

        # 三层隔离过滤
        if user_id is not None:
            scope_clauses = [
                "(scope = 'global' OR scope IS NULL)",   # 全局 + 历史数据
            ]
            scope_params: list = []
            # 个人层
            scope_clauses.append("(scope = 'personal' AND user_id = %s)")
            scope_params.append(user_id)
            # 团队层
            if team_id is not None:
                scope_clauses.append("(scope = 'team' AND team_id = %s)")
                scope_params.append(team_id)
            query += " AND (" + " OR ".join(scope_clauses) + ")"
            params.extend(scope_params)

        if not include_archived:
            query += " AND is_archived = FALSE"
        query += " ORDER BY is_pinned DESC, use_count DESC, created_at DESC"
        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def update_framework(framework_id: int, **kwargs) -> bool:
    """更新框架字段"""
    allowed = {"name", "hook_formula", "structure_template", "rhythm_pattern",
               "emotion_arc", "cta_patterns", "golden_phrases", "full_framework",
               "is_pinned", "is_archived", "scope"}
    updates, params = [], []
    for k, v in kwargs.items():
        if k in allowed:
            updates.append(f"{k} = %s")
            params.append(v)
    if not updates:
        return False
    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(framework_id)
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE viral_frameworks SET {', '.join(updates)} WHERE id = %s", params)
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def increment_framework_use_count(framework_id: int):
    """框架被使用时 +1"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE viral_frameworks SET use_count = use_count + 1, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (framework_id,))
        conn.commit()
        conn.close()
    finally:
        try:
            conn.close()
        except Exception: pass


def delete_framework(framework_id: int) -> bool:
    """删除框架"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM viral_frameworks WHERE id = %s", (framework_id,))
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


# 初始化表（导入时自动执行）
try:
    init_social_tables()
except Exception as e:
    print(f"⚠️ 社媒表初始化警告: {e}")


# 导出
__all__ = [
    "create_project",
    "get_project",
    "get_project_with_profile",
    "list_projects",
    "update_project",
    "delete_project",
    "add_topic",
    "list_topics",
    "save_script",
    "get_script",
    "update_script",
    "list_scripts",
    "update_script_performance",
    "update_script_filmed",
    # 版本历史
    "save_script_version",
    "get_script_versions",
    "revert_to_version",
    # 素材
    "save_material",
    "list_materials",
    "get_materials_for_content",
    "get_project_stats",
    "save_persona",
    "get_persona",
    "get_or_create_project_for_profile",
    # 顾问风格映射
    "advisor_style_to_id",
    "advisor_id_to_style",
    "ADVISOR_STYLE_MAP",
    "ADVISOR_ID_MAP",
    # 爆款框架
    "save_framework",
    "get_framework",
    "list_frameworks",
    "update_framework",
    "delete_framework",
    "increment_framework_use_count",
]

