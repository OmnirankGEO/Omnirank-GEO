-- 知识库架构升级：knowledge_chunks 表
-- pgvector 向量检索 + tsvector 全文检索（BM25）
-- 执行前先备份：docker exec omnirank-db pg_dump -U geo_admin geo_agentscope > backup.sql

-- 确保 pgvector 扩展已启用
CREATE EXTENSION IF NOT EXISTS vector;

-- 主表
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id            SERIAL PRIMARY KEY,
    advisor_id    VARCHAR(64) NOT NULL,          -- 顾问ID（如 xinyi-formula, huang-douyin）
    profile_id    VARCHAR(64),                   -- 用户级知识（可空，顾问级为NULL）
    chunk_id      VARCHAR(128) UNIQUE NOT NULL,  -- 全局唯一ID（如 xinyi-formula_001）
    title         VARCHAR(200) NOT NULL,         -- 知识点标题（用于 BM25 精确匹配）
    content       TEXT NOT NULL,                 -- 蒸馏后正文
    content_type  VARCHAR(32) DEFAULT 'knowledge', -- methodology/case/rule/trend/framework
    applicable_to JSONB DEFAULT '[]',            -- 适用场景标签数组
    keywords      JSONB DEFAULT '[]',            -- 检索关键词数组
    use_when      TEXT DEFAULT '',                -- 什么时候该用
    dont_use_when TEXT DEFAULT '',                -- 什么时候不该用
    related_concepts JSONB DEFAULT '[]',          -- 关联知识点
    golden_quotes JSONB DEFAULT '[]',            -- 原话金句
    metadata      JSONB DEFAULT '{}',            -- 扩展元数据
    source_file   VARCHAR(255) DEFAULT '',        -- 来源文件
    source_section VARCHAR(255) DEFAULT '',       -- 来源位置
    embedding     vector(1024),                  -- pgvector 向量（DashScope text-embedding-v4）
    tsv           tsvector,                      -- 全文检索向量
    created_at    TIMESTAMP DEFAULT NOW(),
    updated_at    TIMESTAMP DEFAULT NOW(),
    is_active     BOOLEAN DEFAULT TRUE           -- 软删除/过期标记
);

-- 向量索引（HNSW，高精度余弦距离）
CREATE INDEX IF NOT EXISTS idx_kc_embedding
    ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

-- 全文检索索引（中文分词用 simple 配置，PostgreSQL 内置）
CREATE INDEX IF NOT EXISTS idx_kc_tsv ON knowledge_chunks USING gin(tsv);

-- 复合索引
CREATE INDEX IF NOT EXISTS idx_kc_advisor ON knowledge_chunks(advisor_id, content_type, is_active);
CREATE INDEX IF NOT EXISTS idx_kc_profile ON knowledge_chunks(profile_id) WHERE profile_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_kc_chunk_id ON knowledge_chunks(chunk_id);

-- 自动更新 tsvector（title + content + keywords 合并）
CREATE OR REPLACE FUNCTION kc_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.tsv :=
        setweight(to_tsvector('simple', COALESCE(NEW.title, '')), 'A') ||
        setweight(to_tsvector('simple', COALESCE(NEW.content, '')), 'B') ||
        setweight(to_tsvector('simple', COALESCE(
            (SELECT string_agg(kw, ' ') FROM jsonb_array_elements_text(COALESCE(NEW.keywords, '[]'::jsonb)) AS kw),
            ''
        )), 'A');
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS kc_tsv_update ON knowledge_chunks;
CREATE TRIGGER kc_tsv_update
    BEFORE INSERT OR UPDATE ON knowledge_chunks
    FOR EACH ROW EXECUTE FUNCTION kc_tsv_trigger();

-- 验证
SELECT 'knowledge_chunks 表创建成功' AS status;
