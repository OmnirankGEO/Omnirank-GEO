"""
A2: 创建 keyword_insights 表
V4.2 蒸馏系统核心数据表

设计要点：
- 追加式时序设计（每次监测 INSERT，不覆盖）
- (task_id, keyword) UNIQUE 约束（幂等，崩溃可重跑）
- quality_flag + feedback_note 支持用户反馈回路
- client_id 预留多客户隔离
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "db" / "geo_diagnosis.db"

CREATE_KEYWORD_INSIGHTS = """
CREATE TABLE IF NOT EXISTS keyword_insights (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    
    -- 来源关联
    task_id         INTEGER NOT NULL,           -- 监测任务 ID
    brand_id        INTEGER,                    -- 客户品牌 ID
    client_id       TEXT,                       -- 客户 ID（多客户隔离预留）
    keyword         TEXT NOT NULL,              -- 监测关键词
    
    -- LLM 蒸馏结果（核心 JSON 字段）
    platforms_analyzed  TEXT DEFAULT '[]',      -- JSON: ["doubao","kimi",...]
    brands_found        TEXT DEFAULT '[]',      -- JSON: 归一化后的品牌列表
    sources_cited       TEXT DEFAULT '[]',      -- JSON: 信源列表
    response_patterns   TEXT DEFAULT '{}',      -- JSON: 回复结构/偏好
    client_position     TEXT DEFAULT '{}',      -- JSON: 客户品牌表现
    optimization_hints  TEXT DEFAULT '[]',      -- JSON: 优化建议
    
    -- 蒸馏元数据
    llm_model           TEXT DEFAULT 'qwen3.7-max',
    llm_tokens          INTEGER DEFAULT 0,
    algorithm_version   TEXT DEFAULT 'v4.2',
    
    -- 用户反馈回路
    quality_flag        TEXT DEFAULT 'auto'     -- auto / verified / rejected
                        CHECK(quality_flag IN ('auto', 'verified', 'rejected', 'degraded')),
    feedback_note       TEXT,                   -- 用户标注说明
    
    -- 时间戳
    distilled_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    -- 约束
    UNIQUE(task_id, keyword)                    -- 幂等：同一任务同一关键词只蒸馏一次
);
"""

CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_ki_brand_id ON keyword_insights(brand_id);",
    "CREATE INDEX IF NOT EXISTS idx_ki_keyword ON keyword_insights(keyword);",
    "CREATE INDEX IF NOT EXISTS idx_ki_distilled_at ON keyword_insights(distilled_at);",
    "CREATE INDEX IF NOT EXISTS idx_ki_task_id ON keyword_insights(task_id);",
    "CREATE INDEX IF NOT EXISTS idx_ki_quality ON keyword_insights(quality_flag);",
]


def main():
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    
    print("=" * 50)
    print("A2: 创建 keyword_insights 表")
    print("=" * 50)
    
    # 检查是否已存在
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='keyword_insights'")
    if c.fetchone():
        print("⚠️ keyword_insights 表已存在，跳过创建")
        conn.close()
        return
    
    # 建表
    c.execute(CREATE_KEYWORD_INSIGHTS)
    print("✅ 已创建 keyword_insights 表")
    
    # 建索引
    for idx_sql in CREATE_INDEXES:
        c.execute(idx_sql)
    print(f"✅ 已创建 {len(CREATE_INDEXES)} 个索引")
    
    conn.commit()
    
    # 验证
    c.execute("SELECT sql FROM sqlite_master WHERE name='keyword_insights'")
    schema = c.fetchone()[0]
    print(f"\n--- 表结构 ---\n{schema}")
    
    c.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_ki_%'")
    indexes = [row[0] for row in c.fetchall()]
    print(f"\n--- 索引 ---\n{indexes}")
    
    conn.close()
    print("\n✅ A2 完成")


if __name__ == "__main__":
    main()
