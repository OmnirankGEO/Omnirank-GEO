"""
GEO诊断数据库模块
PostgreSQL存储诊断历史记录
"""

import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from pathlib import Path

from psycopg2.errors import UniqueViolation, DuplicateColumn

from .models import get_industry_category, get_level_from_score
from services.industry_taxonomy import display_name, filter_values
from .schema_guard import (add_column_if_missing, alter_column_type_if_changed,
                           replace_in_list_check_if_changed)
# brands.latest_* 唯一写入口径。该模块**零项目内依赖**(只 import typing),
# 故 db → services 这条方向不会成环。
from services.brand_latest_ssot import sync_brand_latest

# [P1 容量合同 2026-08-08] 篇数唯一取数出口。tools.pricing_bands 是纯函数模块
# (只 import logging/math/typing),不反向依赖 db,故可在此模块级 import 不成环。
from tools.pricing_bands import (
    LEGACY_MISSING_CAPACITY_DEFAULT as _CAPACITY_MISSING_DEFAULT,
    normalize_article_capacity as _normalize_article_capacity,
)

logger = logging.getLogger("GEO-DiagnosisDB")

# 数据库文件路径
DB_DIR = Path(__file__).parent
DB_PATH = DB_DIR / "geo_diagnosis.db"


def get_connection():
    """获取数据库连接"""
    from db.connection import get_connection as _pg_get_connection
    return _pg_get_connection()


# 🔴 [R5 2026-08-20] _safe_add_column 家族 + brands schema SSOT 已搬到
#   db/brands_schema.py(零副作用叶子模块),这里**只是把名字拿回来**,不是第二份定义。
#   搬家理由:本模块的模块体最后一行是 init_db() —— 夹具 import 这里就等于建 150 张表,
#   窄出口(ensure_brands_schema)必须能被单独 import 才有意义。
from db.brands_schema import (  # noqa: F401  (下面 init_db 与全仓调用方都用这些名字)
    _column_exists,
    _safe_add_column,
    _safe_add_column_optional,
    _SAFE_ADD_SKIPPED_TABLES,
    _BRANDS_CREATE_TABLE_SQL,
    _BRANDS_SELF_HEAL_COLUMNS,
    _BRANDS_INDEX_STATEMENTS,
    ensure_brands_schema,
    execute_index_guarded,
    _INDEX_SKIPPED,
)

from .review_decision_schema import widen_review_decision_check  # noqa: E402  (启动链用)


def init_db():
    """初始化数据库，创建表"""
    conn = get_connection()
    try:
        conn.autocommit = True  # 每条语句独立事务，ALTER TABLE失败不影响后续
        cursor = conn.cursor()
    
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS diagnosis_records (
            -- 基础信息
            id SERIAL PRIMARY KEY,
            session_id TEXT UNIQUE NOT NULL,
            brand_name TEXT NOT NULL,
            industry TEXT NOT NULL,
            industry_category TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            keywords TEXT,
        
            -- 综合评分
            total_score INTEGER,
            level TEXT,
        
            -- 8维度分数
            web_search_score INTEGER,
            platform_score INTEGER,
            content_quality_score INTEGER,
            authority_score INTEGER,
            brand_ownership_score INTEGER,
            ai_visibility_score INTEGER,
            ai_citation_score INTEGER,
            update_frequency_score INTEGER,
        
            -- AI测试数据
            ai_total_tests INTEGER,
            ai_detected_count INTEGER,
            ai_mention_rate REAL,
            ai_engines_tested TEXT,
        
            -- 社媒数据
            douyin_video_count INTEGER,
            douyin_brand_count INTEGER,
            xhs_note_count INTEGER,
            xhs_brand_count INTEGER,
        
            -- 网页搜索数据
            web_result_count INTEGER,
            web_brand_direct_count INTEGER,
            web_authority_count INTEGER,
            scholar_count INTEGER,
        
            -- 品牌识别
            has_brand_presence SMALLINT,
            brand_account_count INTEGER,
        
            -- 竞品数据
            competitor_count INTEGER,
        
            -- 品牌知名度
            brand_recognition_level TEXT,
            data_anomaly SMALLINT,
            anomaly_reason TEXT,
        
            -- 报告文件
            report_md_path TEXT,
            report_json_path TEXT,
        
            -- 原始数据
            raw_data_json TEXT
        )
        """)
    
        # ========== 品牌管理表(建表+自愈补列+索引)==========
        # 🔴 [R5 2026-08-20] 三段已整体抽到模块级 ensure_brands_schema(),
        #   夹具与 init_db 共用同一行代码。这里**只剩一句调用**,别再往回内联。
        ensure_brands_schema(cursor)

        # ========== [P0-D 2026-06-14] 品牌信任资产快照表(独立批) ==========
        # 大证据 JSON 不堆 brands/client_profiles · 独立表 latest active 模型(唯一 partial 索引)。
        # canonical DDL 在 db/trust_asset_db.TRUST_ASSET_DDL_STATEMENTS · 此处启动时兜底幂等创建。
        # 🔴 [Codex#3 返修] 此 commit 含 schema 变更(启动即建新表+索引)· 报价行为 0 变化但【非纯代码】:
        #    Deploy 必须按 schema 批处理 = pg_dump 备份 → BEGIN;<DDL>;ROLLBACK; dry-run → 确认唯一/读索引
        #    创建安全(新表空·无锁风险)· 不可当"纯代码部署"。
        try:
            from db.trust_asset_db import TRUST_ASSET_DDL_STATEMENTS
            for _stmt in TRUST_ASSET_DDL_STATEMENTS:
                cursor.execute(_stmt)
        except Exception as _e:
            print(f"[init_db] brand_trust_asset_snapshot 创建跳过(忽略): {_e}")


        # ========== 新增：给diagnosis_records添加brand_id字段 ==========
        _safe_add_column(cursor, "diagnosis_records", "brand_id", "INTEGER REFERENCES brands(id)")

        # [Phase 9 2026-07-14] 双价目表发布/报价不可变快照启动兼容。
        # canonical DDL/约束仍由 migration_pricing_quote_wiring_2026_07_14.sql 管理；
        # 这里只补新列，避免 migration 尚未跑完的过渡环境在 SELECT 时直接 500。
        _safe_add_column_optional(cursor, "pricing_catalog_entries", "source_ref_jsonb", "JSONB")
        _safe_add_column_optional(cursor, "price_quotes", "pricing_snapshot_jsonb", "JSONB")

        # [R4 ② 2026-08-20] organization 自愈补列块**已整体移到 init_db 末尾**
        #   (原来排在这里,而它要补的 10 张表有 9 张的 CREATE TABLE 在其后,
        #    _safe_add_column 又静默吞异常 ⇒ 63 个列永远补不上。见文件末尾那段。)

        # ========== 新增：给brands添加brand_code唯一编号 ==========
        _safe_add_column(cursor, "brands", "brand_code", "TEXT")
    
        # 为现有品牌生成编号（如果没有）
        try:
            cursor.execute("SELECT id FROM brands WHERE brand_code IS NULL")
            brands_without_code = cursor.fetchall()
            for row in brands_without_code:
                brand_id = row['id']
                brand_code = f"BRD-{brand_id:04d}"
                cursor.execute("UPDATE brands SET brand_code = %s WHERE id = %s", (brand_code, brand_id))
            if brands_without_code:
                conn.commit()
        except Exception:
            pass
    
        # [21 班合后修 2026-08-10] 只改 CREATE TABLE 救不了**已经建过表**的库
        # (IF NOT EXISTS 是空操作)——CI / 老 staging / 预演重建都属这一类,
        # 它们照样会在下面建 partial index 时炸。补自愈,与同表其余列同一形态。
        _safe_add_column(cursor, "brands", "is_deleted", "BOOLEAN DEFAULT FALSE")

        # ========== 社媒操盘手标记 ==========
        _safe_add_column(cursor, "brands", "social_enabled", "BOOLEAN DEFAULT FALSE")

        # ========== 新增：给brands添加cities字段 ==========
        _safe_add_column(cursor, "brands", "cities", "TEXT")

        # [CTO-15.9 2026-04-25 A.8] business_type denormalized 缓存(SSOT 在 client_profiles · brands 是冗余加速)
        # 避免 hot-path JOIN client_profiles · 例如 batch_pricing / pricing_auditor 频繁调
        # PRD M1b §M1 v1.1 Codex 修订 · brands 加为长期可选(非 T1 migration)
        # 写入由 client_profiles UPDATE 时联动(应用层管理 · 不用触发器)
        _safe_add_column(cursor, "brands", "business_type", "VARCHAR(20) DEFAULT 'B2C'")
        _safe_add_column(cursor, "brands", "city_scope", "VARCHAR(20) DEFAULT 'local'")
        # C2.2 (CTO-15.9 session 3 · 2026-04-25 · M2 §Epic 4 竞品建档 schema)
        # 代理为 brand 维护 3-5 个核心竞品(用于 Module 4 真竞品分析 · 替 search_citations 启发式)
        # JSONB 数组:[{name, url?, note?, added_at}]
        _safe_add_column(cursor, "brands", "competitors_jsonb", "JSONB DEFAULT '[]'")

        # Phase E.7 (CTO-15.10 · 2026-04-27 · v2 §F2 代理记账区)
        # 代理为 brand 私人记账(玩法 B 客户付款不走平台 · 代理需要在系统外记账避免漏激活)
        # JSONB 形 {received_amount?, received_at?, note?, status?}
        # status 可选值:'unpaid' | 'partial' | 'paid' · 默认未填即 unpaid
        _safe_add_column(cursor, "brands", "agent_payment_note", "JSONB")

        # Phase F (CTO-15.10 · 2026-04-27 · 老板要求 · 关键词从启动诊断页搬到快录新客户页)
        # AI 帮填一次产 8-15 个高商业意图种子关键词 · 启动诊断时直接预填
        # JSONB 数组:["关键词1", "关键词2", ...]
        _safe_add_column(cursor, "brands", "seed_keywords", "JSONB DEFAULT '[]'::jsonb")

        # A.2 (CTO-15.18 · 2026-04-28 · PM 干预 类 A · 测试客户隔离根因 #3)
        # 老板红线:测试客户名(M3验收测试客户_20260427_深圳家装等)暴露在 C 端 5 公开链接 + 销售话术
        # → SaaS 设计 ABC:加 is_test boolean 字段 · 默认 ON 隐藏测试客户 · 真客户白名单固化
        # is_test=true:不出现在 sales/today / 客户池 / quotes list / monitor 默认视图
        # is_test=false:真客户(8 个白名单 + 后续新建)
        _safe_add_column(cursor, "brands", "is_test", "BOOLEAN DEFAULT FALSE")
        # admin 后台手动锁定 is_test 状态(防止应用层 auto-detect 误标)
        _safe_add_column(cursor, "brands", "is_test_locked", "BOOLEAN DEFAULT FALSE")

        # C.6 (CTO-15.18 · 2026-04-28 · PM 干预 类 C · 删除客户 soft delete + 7 天回收站)
        # 旧版有删除按钮 · M3 砍了 → 中坚代理回旧版根本动机之一(根因 #4 工具回归)
        # 加 deleted_reason 字段记录删除原因 · is_deleted 字段已有(legacy)
        _safe_add_column(cursor, "brands", "deleted_reason", "TEXT")
        _safe_add_column(cursor, "brands", "deleted_at", "TIMESTAMP")

        # 从已有报价回填brands.cities（一次性迁移）
        try:
            cursor.execute("""
                UPDATE brands SET cities = (
                    SELECT q.city FROM quotes q
                    WHERE q.brand_id = brands.id AND q.city IS NOT NULL AND q.city != ''
                    ORDER BY q.created_at DESC LIMIT 1
                )
                WHERE cities IS NULL OR cities = ''
            """)
            conn.commit()
        except Exception:
            pass

        # ========== 新增：诊断类型字段（销售版/技术版）==========
        _safe_add_column(cursor, "diagnosis_records", "diagnosis_type", "TEXT DEFAULT 'technical_full'")
        _safe_add_column(cursor, "diagnosis_records", "share_token", "VARCHAR(16)")

        # ========== 自定义诊断问题(2026-05-21 CTO-15.23)· 老板拍板 8 点 ==========
        # custom_questions:用户自定义检测问题列表(JSONB · 8 题套餐外每题 +100 积分)
        # total_questions_tested:本次诊断总题数(系统 8 + 自定义 N · 用于报告展示)
        _safe_add_column(cursor, "diagnosis_records", "custom_questions", "JSONB")
        _safe_add_column(cursor, "diagnosis_records", "total_questions_tested", "INTEGER DEFAULT 8")

        # ========== M2 报告 2.0 · 客户版/内部版双线落库(CTO-B 2026-04-26 W1) ==========
        # 决策点 2:落库 + 失效后重算策略 · 公开页快/稳/可追溯
        # 决策点 5:不静默降级 v1 · v2 异常时记 error 并显示"报告生成异常"
        _safe_add_column(cursor, "diagnosis_records", "report_v2_version", "VARCHAR(8) DEFAULT NULL")
        _safe_add_column(cursor, "diagnosis_records", "report_v2_internal_md", "TEXT")
        _safe_add_column(cursor, "diagnosis_records", "report_v2_client_md", "TEXT")
        _safe_add_column(cursor, "diagnosis_records", "report_v2_modules_jsonb", "JSONB")
        _safe_add_column(cursor, "diagnosis_records", "report_v2_generated_at", "TIMESTAMPTZ")
        _safe_add_column(cursor, "diagnosis_records", "report_v2_error", "TEXT")
        _safe_add_column(cursor, "diagnosis_records", "data_completeness_score", "INTEGER DEFAULT 0")
        _safe_add_column(cursor, "diagnosis_records", "data_completeness_breakdown", "JSONB")
        # PDF/HTML 渲染缓存(W5)· nullable · 失效后重算
        _safe_add_column(cursor, "diagnosis_records", "report_v2_internal_html_path", "TEXT")
        _safe_add_column(cursor, "diagnosis_records", "report_v2_client_html_path", "TEXT")
        _safe_add_column(cursor, "diagnosis_records", "report_v2_pdf_path", "TEXT")
        # [audit #7 返修] 软删列启动期建(原靠"历史某次 delete_client 自建"的运气 → 全新 init / 灾备库
        #   delete_client 撞 UndefinedColumn → 整笔软删回滚 + 500)。启动一次性建,非热路径。
        _safe_add_column(cursor, "diagnosis_records", "is_deleted", "BOOLEAN DEFAULT FALSE")
        _safe_add_column(cursor, "diagnosis_records", "deleted_at", "TIMESTAMP")
        # [返工2 修复净增量 P2] 结果可见性两列启动期建(SSOT = init_db · 不依赖 prestart 迁移先跑):
        #   diagnosis_records 只由 init_db 懒建(无 manifest 迁移),而可见性迁移块被 to_regclass 守卫 → 全新/空 DB
        #   的 fleet(ROLE=web/cron)启动期 diagnosis 守卫会因缺 result_visibility crash-loop
        #   (该守卫 2026-07-30 已迁到 services/startup_schema_guards.verify_diagnosis_schema_fail_closed,
        #    与 scripts/prestart.py 共用同一份清单)。
        #   init_db 在 server.py 导入期(早于 fail-closed 校验)跑 → 全新 DB 也自带这两列,校验通过。迁移块保留作已有库补齐。
        _safe_add_column(cursor, "diagnosis_records", "run_token", "TEXT")
        _safe_add_column(cursor, "diagnosis_records", "result_visibility", "TEXT")  # NULL=published(兼容)
        try:
            cursor.execute("""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                        WHERE conname='chk_diag_records_visibility' AND conrelid='diagnosis_records'::regclass) THEN
                        ALTER TABLE diagnosis_records ADD CONSTRAINT chk_diag_records_visibility CHECK (
                            result_visibility IS NULL OR result_visibility IN ('pending','published','withheld'));
                    END IF;
                END $$;
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_diag_records_run_token ON diagnosis_records (run_token)")
        except Exception:
            pass  # CHECK/索引非致命(可见性谓词不依赖 CHECK · 迁移块会补)

        # ========== 新增：文章生成记录表 ==========
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS article_generations (
            id SERIAL PRIMARY KEY,
            diagnosis_id INTEGER,           -- 关联诊断记录
            task_id TEXT,                   -- 批次ID (如 gen_20260113_123456)
            article_type TEXT,              -- ranking/case/qa
            title TEXT,                     -- 文章标题
            word_count INTEGER,             -- 字数
            file_path TEXT,                 -- 文件路径
            status TEXT DEFAULT 'success',  -- success/failed
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (diagnosis_id) REFERENCES diagnosis_records(id)
        )
        """)
    
        # ========== 新增：Token使用记录表 ==========
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS token_usage (
            id SERIAL PRIMARY KEY,
            task_id TEXT,                   -- 关联任务ID
            diagnosis_id INTEGER,           -- 关联诊断记录(可选)
            model_name TEXT,                -- qwen3-max / deepseek-v4-flash
            input_tokens INTEGER,           -- 输入Token
            output_tokens INTEGER,          -- 输出Token
            total_tokens INTEGER,           -- 总Token
            estimated_cost REAL,            -- 预估费用(元)
            operation_type TEXT,            -- diagnosis/article/review
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (diagnosis_id) REFERENCES diagnosis_records(id)
        )
        """)
    
        # ========== 新增：客户资料表 ==========
        # 存储客户上传的真实资料，用于文章生成
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS client_materials (
            id SERIAL PRIMARY KEY,
            diagnosis_id INTEGER,               -- 关联诊断记录
            brand_id INTEGER,                   -- 关联品牌
        
            -- 公司介绍
            company_intro TEXT,                 -- 公司简介（支持Markdown）
            founding_year INTEGER,              -- 成立年份
            team_size TEXT,                     -- 团队规模
            service_area TEXT,                  -- 服务范围/地域
        
            -- 核心卖点（客户自己总结的）
            core_selling_points TEXT,           -- JSON: [{"point": "卖点", "evidence": "证据"}]
            unique_value TEXT,                  -- 一句话价值主张
            methodology TEXT,                   -- 方法论/服务流程描述
        
            -- 成功案例（真实数据）
            case_studies TEXT,                  -- JSON: [{"client": "客户", "industry": "行业", "result": "效果", "timeline": "周期"}]
        
            -- 服务报价
            pricing_tiers TEXT,                 -- JSON: [{"name": "套餐名", "price": "X万/月", "includes": [...]}]
        
            -- 客户证言
            testimonials TEXT,                  -- JSON: [{"name": "张总", "title": "职位", "company": "公司", "quote": "..."}]
        
            -- 荣誉资质
            credentials TEXT,                   -- JSON: [{"type": "资质类型", "name": "证书名", "year": 2025}]
        
            -- 元信息
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        
            FOREIGN KEY (diagnosis_id) REFERENCES diagnosis_records(id),
            FOREIGN KEY (brand_id) REFERENCES brands(id)
        )
        """)
    
        # ========== 新增：报价单表 ==========
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS quotes (
            id SERIAL PRIMARY KEY,
            diagnosis_id INTEGER,               -- 关联诊断记录（可选）
            brand_id INTEGER,                   -- 关联品牌（可选）
            brand_name TEXT,                    -- 品牌名称
            industry TEXT,                      -- 行业
            city TEXT,                          -- 城市
            tier TEXT,                          -- entry/standard/flagship
            target_share REAL,                  -- 目标占比(三档)
            total_keywords INTEGER,             -- 关键词总数
            total_articles INTEGER,             -- 文章总数
            monthly_price REAL,                 -- 月度价格
            paid_amount REAL,                   -- 实收金额
            status TEXT DEFAULT 'draft',        -- draft/sent/confirmed/paid
            confirmed_at TIMESTAMP,
            paid_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (diagnosis_id) REFERENCES diagnosis_records(id),
            FOREIGN KEY (brand_id) REFERENCES brands(id)
        )
        """)
    
        # ========== 新增：确认关键词表 ==========
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS confirmed_keywords (
            id SERIAL PRIMARY KEY,
            quote_id INTEGER,                   -- 关联报价单
            keyword TEXT NOT NULL,              -- 关键词
            category TEXT,                      -- 问答词/价格词/地区词/长尾词
            tier TEXT,                          -- tier1-tier5
            base_price REAL,                    -- 基础价格
            city_premium REAL,                  -- 城市溢价系数
            final_price REAL,                   -- 最终价格
            competitor_count INTEGER,           -- 竞品数量
            status TEXT DEFAULT 'pending',      -- pending/writing/published
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (quote_id) REFERENCES quotes(id)
        )
        """)
    
        # ========== 新增：关键词报价缓存表（7天价格锁定）==========
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS keyword_price_cache (
            id SERIAL PRIMARY KEY,
            brand_name TEXT NOT NULL,
            keyword TEXT NOT NULL,
            -- 三维评分数据（完整缓存，避免重新查API）
            difficulty_score REAL,
            value_score REAL,
            competitor_count INTEGER,
            cost_per_article REAL,
            intent TEXT,
            funnel_stage TEXT,
            search_probability REAL,
            search_volume INTEGER,
            sem_price REAL,
            bidword_company_count INTEGER,
            content_count INTEGER,
            source_authority_json TEXT,
            recommended_platforms_json TEXT,
            data_source TEXT,
            markup_ratio REAL,
            -- 三个套餐的计算结果
            entry_price REAL,
            entry_articles INTEGER,
            standard_price REAL,
            standard_articles INTEGER,
            flagship_price REAL,
            flagship_articles INTEGER,
            -- 时间管理
            cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            UNIQUE(brand_name, keyword)
        )
        """)

        # ========== 新增：选题表 ==========
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS topics (
            id SERIAL PRIMARY KEY,
            keyword_id INTEGER,                 -- 关联确认关键词
            quote_id INTEGER,                   -- 关联报价单
            original_keyword TEXT,              -- 原始关键词
            optimized_title TEXT,               -- 优化后标题
            article_style TEXT,                 -- 文章风格
            article_id INTEGER,                 -- 关联生成的文章
            -- [WO_225-c1 §8.4b] 🔴 原注释写的是 draft/confirmed/writing/published:
            --   `confirmed` **全仓零写入点**,而它漏了 completed/pending/failed/
            --   regenerating/write_timeout 五个。照它写状态谓词会漏掉一半。
            --   代码真正会写进来的 8 个,SSOT 在
            --   `services/article_capacity_contract.ALL_TOPIC_STATUSES`,
            --   并由 test_status_vocabulary_is_complete 扫写入点逐项比对。
            status TEXT DEFAULT 'draft',        -- 见上:8 个状态,SSOT 在容量合同里
            confirmed_at TIMESTAMP,
            completed_at TIMESTAMP,             -- 文章完成时间
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (keyword_id) REFERENCES confirmed_keywords(id),
            FOREIGN KEY (quote_id) REFERENCES quotes(id)
        )
        """)
        # ``CREATE TABLE IF NOT EXISTS`` does not repair a partial legacy
        # table.  The v1.4 migration is additive too, so make the startup path
        # able to reach the FK block without guessing any historical value.
        # [WO_285b] 列缺失才 ALTER(先查目录):init_db 在请求路径上也会被调用,
        #   无条件 `ADD COLUMN IF NOT EXISTS` 每次都要拿 topics 的 ACCESS EXCLUSIVE,发车 pg_dump 期间会排队卡死。
        add_column_if_missing(cursor, "topics", "article_id", "INTEGER")
        # [WO_225-c1 §8.3] 媒体桶。**与 db/migration_061_media_slot_conversion_2026_09_15.sql
        #   逐字同形**(同名、同类型、同 CHECK 三值)。
        #   🔴 两条建表路径必须给出同一张表:生产走迁移,干净库与多个测试包走这段自愈 DDL。
        #      只改一边 = 另一边读这列时运行时 UndefinedColumn,而「表已建」的日志照样绿。
        add_column_if_missing(cursor, "topics", "media_bucket", "VARCHAR(40)")  # [WO_285b] 同上
        cursor.execute("""
        DO $topics_media_bucket_ck$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint
                            WHERE conname = 'ck_topics_media_bucket') THEN
                ALTER TABLE topics
                    ADD CONSTRAINT ck_topics_media_bucket
                    CHECK (media_bucket IS NULL OR media_bucket IN (
                        'focus_media_anchor',
                        'industry_platform_coverage',
                        'douyin_doubao_only'));
            END IF;
        END $topics_media_bucket_ck$
        """)

        # ========== 写作大厅文章主表 ==========
        # ``articles`` is the parent of the v1.4 review/experiment tables below.
        # Production already has this legacy table, so this DDL is a no-op there;
        # a clean database must create it before any startup migration adds
        # columns or declares foreign keys against it.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id SERIAL PRIMARY KEY,
            topic_id INTEGER REFERENCES topics(id),
            quote_id INTEGER REFERENCES quotes(id),
            title TEXT,
            content TEXT,
            word_count INTEGER DEFAULT 0,
            style TEXT,
            version INTEGER DEFAULT 1,
            reference_article TEXT,
            revision_note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        # Canonical runtime semantics: topics.article_id references articles.id.
        # Add the FK only after both sides exist.  Existing databases with any
        # legacy FK are left to the fail-closed v1.4 compatibility migration;
        # startup DDL must never guess historical identity or create two FKs.
        cursor.execute("""
        DO $topics_article_fk$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                  FROM pg_constraint c
                  JOIN pg_attribute a
                    ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
                 WHERE c.conrelid = 'topics'::regclass
                   AND c.contype = 'f'
                   AND a.attname = 'article_id'
            ) THEN
                ALTER TABLE topics
                    ADD CONSTRAINT topics_article_id_articles_fk
                    FOREIGN KEY (article_id) REFERENCES articles(id);
            END IF;
        END
        $topics_article_fk$;
        """)
    
        # 创建索引
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_brand_name ON diagnosis_records(brand_name)",
            "CREATE INDEX IF NOT EXISTS idx_industry ON diagnosis_records(industry)",
            "CREATE INDEX IF NOT EXISTS idx_industry_category ON diagnosis_records(industry_category)",
            "CREATE INDEX IF NOT EXISTS idx_created_at ON diagnosis_records(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_level ON diagnosis_records(level)",
            "CREATE INDEX IF NOT EXISTS idx_total_score ON diagnosis_records(total_score)",
            "CREATE INDEX IF NOT EXISTS idx_brand_recognition ON diagnosis_records(brand_recognition_level)",
            "CREATE INDEX IF NOT EXISTS idx_diagnosis_brand_id ON diagnosis_records(brand_id)",
            # brands 表索引已移入 _BRANDS_INDEX_STATEMENTS,由 ensure_brands_schema 建。
            # 文章表索引
            "CREATE INDEX IF NOT EXISTS idx_article_diagnosis_id ON article_generations(diagnosis_id)",
            "CREATE INDEX IF NOT EXISTS idx_article_task_id ON article_generations(task_id)",
            "CREATE INDEX IF NOT EXISTS idx_article_created_at ON article_generations(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_token_task_id ON token_usage(task_id)",
            "CREATE INDEX IF NOT EXISTS idx_token_created_at ON token_usage(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_token_model_name ON token_usage(model_name)",
            # client_materials表索引
            "CREATE INDEX IF NOT EXISTS idx_materials_diagnosis_id ON client_materials(diagnosis_id)",
            "CREATE INDEX IF NOT EXISTS idx_materials_brand_id ON client_materials(brand_id)",
            # quotes表索引
            "CREATE INDEX IF NOT EXISTS idx_quotes_brand_id ON quotes(brand_id)",
            "CREATE INDEX IF NOT EXISTS idx_quotes_status ON quotes(status)",
            "CREATE INDEX IF NOT EXISTS idx_quotes_created_at ON quotes(created_at)",
            # confirmed_keywords表索引
            "CREATE INDEX IF NOT EXISTS idx_ck_quote_id ON confirmed_keywords(quote_id)",
            "CREATE INDEX IF NOT EXISTS idx_ck_status ON confirmed_keywords(status)",
            # topics表索引
            "CREATE INDEX IF NOT EXISTS idx_topics_keyword_id ON topics(keyword_id)",
            "CREATE INDEX IF NOT EXISTS idx_topics_quote_id ON topics(quote_id)",
            "CREATE INDEX IF NOT EXISTS idx_topics_status ON topics(status)",
            # keyword_price_cache表索引
            "CREATE INDEX IF NOT EXISTS idx_kpc_brand_keyword ON keyword_price_cache(brand_name, keyword)",
            "CREATE INDEX IF NOT EXISTS idx_kpc_expires ON keyword_price_cache(expires_at)",
            # v1_2 全局共享锁索引（CTO-14.0 2026-04-19）：(industry, city, keyword) 跨代理/跨品牌共享
            "CREATE INDEX IF NOT EXISTS idx_kpc_industry_city_kw ON keyword_price_cache(industry, city, keyword) WHERE industry IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_kpc_keyword_expires ON keyword_price_cache(keyword, expires_at)",
            # employee_tasks表索引
            "CREATE INDEX IF NOT EXISTS idx_emp_tasks_employee_id ON employee_tasks(employee_id)",
            "CREATE INDEX IF NOT EXISTS idx_emp_tasks_status ON employee_tasks(status)",
            "CREATE INDEX IF NOT EXISTS idx_emp_tasks_created_at ON employee_tasks(created_at)",
        ]
    
        # ============================================
        # AI员工任务表
        # ============================================
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS employee_tasks (
            id SERIAL PRIMARY KEY,
            employee_id TEXT NOT NULL,              -- 员工ID (如 'data_collector')
            employee_name TEXT,                     -- 员工名称
            task_type TEXT DEFAULT 'single',        -- 'single' | 'meeting'
            task_content TEXT NOT NULL,             -- 任务内容
            context TEXT,                           -- 背景信息 (JSON)
            result TEXT,                            -- 执行结果
            status TEXT DEFAULT 'pending',          -- 'pending' | 'running' | 'completed' | 'failed'
            skills_used TEXT,                       -- 使用的技能 (JSON数组)
            execution_time REAL,                    -- 执行时间(秒)
            error_message TEXT,                     -- 错误信息
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        )
        """)
    
        # ============================================
        # AI会议记录表 (v2.0 - 完整会议管理)
        # ============================================
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS employee_meetings (
            id SERIAL PRIMARY KEY,
            meeting_id TEXT UNIQUE NOT NULL,        -- 会议唯一ID
        
            -- 会议基本信息
            topic TEXT NOT NULL,                    -- 会议议题
            context TEXT,                           -- 背景资料
        
            -- 关联客户/品牌
            brand_id INTEGER,                       -- 关联品牌ID（可选）
            brand_name TEXT,                        -- 品牌名称
            is_internal BOOLEAN DEFAULT false,       -- 是否公司内部会议
        
            -- 参会人员
            participants TEXT NOT NULL,             -- 参会员工 (JSON数组: ["员工名1", "员工名2"])
            participant_ids TEXT,                   -- 参会员工ID (JSON数组: ["emp_id1", "emp_id2"])
            moderator_id TEXT,                      -- 主持人ID
            moderator_name TEXT,                    -- 主持人名称
        
            -- 会议内容
            transcript TEXT,                        -- 讨论记录 (JSON数组)
            conclusion TEXT,                        -- 会议结论/纪要
            summary TEXT,                           -- 会议摘要（AI生成）
            action_items TEXT,                      -- 行动项 (JSON数组)
        
            -- 附件
            attachments TEXT,                       -- 附件信息 (JSON数组)
        
            -- 会议状态
            status TEXT DEFAULT 'pending',          -- 'pending' | 'in_progress' | 'completed' | 'cancelled'
            rounds INTEGER DEFAULT 2,               -- 讨论轮数
            duration REAL,                          -- 会议时长（秒）
        
            -- 标签（用于搜索）
            tags TEXT,                              -- 标签 (JSON数组: ["策略", "小红书"])
        
            -- 时间戳
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,                   -- 开始时间
            completed_at TIMESTAMP,                 -- 完成时间
        
            FOREIGN KEY (brand_id) REFERENCES brands(id)
        )
        """)
    
        # 会议表索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_meetings_brand ON employee_meetings(brand_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_meetings_status ON employee_meetings(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_meetings_created ON employee_meetings(created_at DESC)")
    
        # ============================================
        # 部门配置表
        # ============================================
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS departments (
            id TEXT PRIMARY KEY,                    -- 部门ID (如 'diagnosis')
            name TEXT NOT NULL,                     -- 部门名称 (如 '诊断部')
            icon TEXT DEFAULT '📁',                 -- 部门图标
            description TEXT,                       -- 部门描述
            sort_order INTEGER DEFAULT 0,           -- 排序
            is_active BOOLEAN DEFAULT true,            -- 是否启用
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
    
        # ============================================
        # 员工配置表 (数据库驱动)
        # ============================================
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS employee_configs (
            id TEXT PRIMARY KEY,                    -- 员工ID (如 'data_collector')
            name TEXT NOT NULL,                     -- 员工名称 (如 '数据采集员')
            department_id TEXT NOT NULL,            -- 所属部门ID
            description TEXT,                       -- 员工描述
            avatar TEXT DEFAULT '🤖',               -- 员工头像/图标
        
            -- 模型配置(2026-05-09 升级默认值 v3.2 → v4-flash · 历史 row 不变)
            model_id TEXT NOT NULL DEFAULT 'deepseek-v4-flash',
            temperature REAL DEFAULT 0.7,
            max_tokens INTEGER DEFAULT 4000,
        
            -- 系统提示词
            system_prompt TEXT,
        
            -- 技能配置 (JSON数组)
            skills TEXT DEFAULT '[]',
        
            -- MCP配置 (JSON对象)
            mcp_config TEXT DEFAULT '{}',
        
            -- 记忆类型
            memory_type TEXT DEFAULT 'temporary',   -- 'temporary' | 'persistent'
        
            -- 状态
            is_active BOOLEAN DEFAULT true,
            sort_order INTEGER DEFAULT 0,
        
            -- 使用场景: 'all' | 'single' | 'meeting' | 'workflow'
            usage_scope TEXT DEFAULT 'all',
        
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        
            FOREIGN KEY (department_id) REFERENCES departments(id)
        )
        """)
    
        # 添加新索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_emp_configs_dept ON employee_configs(department_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_emp_configs_active ON employee_configs(is_active)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_departments_active ON departments(is_active)")
    
        # ============================================
        # 顾问表 (RAG知识库)
        # ============================================
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS advisors (
            id TEXT PRIMARY KEY,                    -- 顾问ID (如 'jobs')
            name TEXT NOT NULL,                     -- 显示名称 (如 '乔布斯')
            avatar TEXT DEFAULT '👤',               -- 头像
            description TEXT,                       -- 简介
            base_prompt TEXT NOT NULL,              -- 基础提示词
            model_id TEXT DEFAULT 'qwen3.7-max',      -- 使用的模型
            knowledge_path TEXT,                    -- RAG知识库路径
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
    
        # 顾问文档表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS advisor_documents (
            id SERIAL PRIMARY KEY,
            advisor_id TEXT NOT NULL,               -- 关联顾问
            filename TEXT NOT NULL,                 -- 文件名
            file_type TEXT,                         -- 文件类型 (pdf, txt, md)
            chunk_count INTEGER,                    -- 分块数量
            file_size INTEGER,                      -- 文件大小(字节)
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (advisor_id) REFERENCES advisors(id)
        )
        """)
    
        # 顾问表：补全字段（新环境 CREATE TABLE 只有基础列，这些是后续迭代加的）
        _safe_add_column(cursor, "advisors", "api_provider", "TEXT DEFAULT 'dashscope'")
        _safe_add_column(cursor, "advisors", "model_name", "TEXT DEFAULT 'qwen3.7-max'")
        _safe_add_column(cursor, "advisors", "enable_web_search", "INTEGER DEFAULT 0")
        _safe_add_column(cursor, "advisors", "role", "TEXT DEFAULT 'expert'")
        _safe_add_column(cursor, "advisors", "specialty", "TEXT DEFAULT ''")
        _safe_add_column(cursor, "advisors", "industries", "JSONB DEFAULT '[]'")
        _safe_add_column(cursor, "advisors", "tags", "JSONB DEFAULT '[]'")
        _safe_add_column(cursor, "advisors", "credentials", "TEXT DEFAULT ''")
        _safe_add_column(cursor, "advisors", "greeting", "TEXT DEFAULT ''")
        _safe_add_column(cursor, "advisors", "quick_questions", "JSONB DEFAULT '[]'")
        _safe_add_column(cursor, "advisors", "knowledge_count", "INTEGER DEFAULT 0")
        _safe_add_column(cursor, "advisors", "public_name", "TEXT")
        _safe_add_column(cursor, "advisors", "source_name", "TEXT")
        _safe_add_column(cursor, "advisors", "identity_status", "TEXT DEFAULT 'draft'")
        _safe_add_column(cursor, "advisors", "identity_notes", "TEXT DEFAULT ''")
        _safe_add_column(cursor, "advisors", "identity_updated_at", "TIMESTAMP")

        # 顾问表索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_advisors_active ON advisors(is_active)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_advisor_docs_advisor ON advisor_documents(advisor_id)")

        # 系统配置表（key-value，默认操盘手等配置）
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_config (
            key VARCHAR(100) PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """)

        # ========== 新增：topics表添加reviewed_at字段 ==========
        _safe_add_column(cursor, "topics", "reviewed_at", "TIMESTAMP")

        # ========== confirmed_keywords表补齐缺失列 ==========
        for col, col_type in [
            ("required_articles", "INTEGER DEFAULT 1"),
            ("recommended_platforms", "TEXT"),
            ("intent", "TEXT DEFAULT 'informational'"),
            ("funnel_stage", "TEXT DEFAULT 'awareness'"),
        ]:
            _safe_add_column(cursor, "confirmed_keywords", col, col_type)

        # ========== Bug6修复：quotes表补齐缺失列 ==========
        for col, col_type in [
            ("markdown", "TEXT DEFAULT ''"),
            ("distilled_data", "TEXT"),
            ("writing_status", "TEXT DEFAULT 'pending'"),
            # 服务期管理
            ("service_start_date", "DATE"),
            ("service_end_date", "DATE"),
            ("service_months", "INTEGER DEFAULT 1"),
            ("service_status", "TEXT DEFAULT 'pending'"),  # pending/active/expiring/expired/paused
            # v1_2 (CTO-14.0 2026-04-19): 来源区分 C 端方案 vs 代理端报价，修 Bug 5 写作大厅断链
            ("source_type", "TEXT DEFAULT 'agent_quote'"),  # agent_quote / c_end_geo_plan
            ("owner_user_id", "INTEGER"),  # C 端持久化直接按 user 归属（兼容 brand_id）
            # P0.5 (CTO-15.7 2026-04-25 · Deploy-CTO 反馈后启动自检):
            # 空壳 quote 软删字段 · scripts/migration_phase1_p0.5.sql 若未跑 · 此处幂等补
            # 生产关键:get_quotes_list 和 /api/diagnosis/{id}/generate-quote 用 WHERE deleted_at IS NULL
            ("deleted_at", "TIMESTAMP DEFAULT NULL"),
            ("cleanup_reason", "TEXT DEFAULT NULL"),
            # Phase A.1 (CTO-15.11 2026-04-28):改价审计字段
            # 原改价端点改 paid_amount 时记录最近一次时间;[开源 E3 · WO_323 G3a · 2026-10-02] 端点已删,列按红线保留(存量 0 行有值)
            ("last_price_adjusted_at", "TIMESTAMP DEFAULT NULL"),
            # P2 (2026-06-03) distilled 来源治理:可证明报价/文章用了哪版客户资料(防过期蒸馏)
            ("distilled_version", "INTEGER DEFAULT NULL"),
            ("distilled_source_hash", "VARCHAR(64) DEFAULT NULL"),
            ("distilled_at", "TIMESTAMP DEFAULT NULL"),
            # Closed-loop sidecar: NULL means the exact legacy path.
            ("article_plan_writing_mode", "VARCHAR(32) DEFAULT NULL"),
            ("article_plan_enrolled_at", "TIMESTAMPTZ DEFAULT NULL"),
            ("article_plan_contract_version", "VARCHAR(80) DEFAULT NULL"),
            ("article_plan_enrolled_by", "INTEGER DEFAULT NULL"),
            ("article_plan_enrollment_run_id", "BIGINT DEFAULT NULL"),
            # [P1-4 引擎定向 2026-08-14] 写作项目级目标引擎(复用 geo_douyin 先例)。
            # 值域 = writing/engine_targeting.ARTICLE_TARGET_ENGINES 键;NULL/空 = 不定向。
            # 主线 SQL:scripts/migration_quotes_target_engine_2026_08_14.sql · 此处兜底防漏。
            ("target_engine", "VARCHAR(32) DEFAULT NULL"),
        ]:
            _safe_add_column(cursor, "quotes", col, col_type)

        for col, col_type in [
            ("delivery_slot_key", "UUID DEFAULT NULL"),
            ("plan_run_id", "BIGINT DEFAULT NULL"),
            ("target_question_snapshot_id", "BIGINT DEFAULT NULL"),
            ("article_plan_metadata_version", "VARCHAR(80) DEFAULT NULL"),
        ]:
            _safe_add_column(cursor, "topics", col, col_type)

        # P0.4 (CTO-15.7 2026-04-25 · Deploy-CTO 反馈后启动自检):
        # articles 首次发布时间 · scripts/migration_phase1_p0.4.sql 若未跑 · 此处幂等补
        # 生产关键:services/publication_facts.record_manual_publication UPDATE articles SET first_published_at
        _safe_add_column(cursor, "articles", "first_published_at", "TIMESTAMP DEFAULT NULL")

        # v2.7.1 GEO 文体改造 · articles 质量警告字段(主线 scripts/migration_articles_quality_warning.sql · 此处兜底防漏)
        _safe_add_column(cursor, "articles", "quality_warning", "JSONB DEFAULT NULL")

        # v2.7.1 GEO 文体改造 · articles 单一权威字段 style_code(新写优先此字段 · 旧 style 字段保留兼容)
        _safe_add_column(cursor, "articles", "style_code", "VARCHAR(64) DEFAULT NULL")
        # [开源 E10 · 2026-09-28] 这里不写 `DEFAULT NULL`:与之等价(没默认值就是 NULL),但 PG 会把它存成
        #   一个显式默认表达式(NULL::character varying),v1.4 schema 合同(geo_article_v14_schema_contract)按生产形状核,
        #   生产的这些列由迁移先建、没有默认值 ⇒ 空库冷启动时运行时先建 ⇒ 合同报 unexpected_default。生产库列早已在,本处跳过。
        # GEO article v1.4 A7/A8: generation and first-success publication lineage.
        # Historical rows remain NULL/legacy; no backfill pretends current content was published content.
        for col, col_type in [
            ("style_family", "VARCHAR(64)"),
            ("style_contract_version", "VARCHAR(80)"),
            ("style_version", "VARCHAR(80)"),
            ("generation_request_id", "TEXT"),
            ("generation_request_snapshot", "JSONB"),
            ("prompt_hash", "CHAR(64)"),
            ("evidence_pack", "JSONB"),
            ("evidence_manifest_hash", "CHAR(64)"),
            ("brand_fact_snapshot", "JSONB"),
            ("brand_snapshot_hash", "CHAR(64)"),
            ("article_review", "JSONB"),
            ("article_review_status", "VARCHAR(40) DEFAULT 'legacy_unreviewed'"),
            ("publication_profile", "VARCHAR(64) NOT NULL DEFAULT 'standard'"),
            ("platform_review", "JSONB"),
            ("article_human_review_status", "VARCHAR(40)"),
            ("article_human_reviewed_by", "INTEGER"),
            ("article_human_reviewed_at", "TIMESTAMPTZ"),
            ("article_human_review_reason", "TEXT"),
            ("current_content_hash", "CHAR(64)"),
            ("publication_snapshot", "JSONB"),
            ("publication_snapshot_hash", "CHAR(64)"),
            ("publication_snapshot_at", "TIMESTAMPTZ"),
            ("publication_snapshot_source", "VARCHAR(40)"),
            ("publication_snapshot_source_id", "BIGINT"),
            ("delivery_slot_key", "UUID"),
            ("article_revision_key", "CHAR(64)"),
            ("target_question_snapshot_id", "BIGINT"),
        ]:
            _safe_add_column(cursor, "articles", col, col_type)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS geo_article_review_events (
                id BIGSERIAL PRIMARY KEY,
                article_id BIGINT NOT NULL REFERENCES articles(id),
                actor_user_id INTEGER NOT NULL,
                decision VARCHAR(40) NOT NULL CHECK (decision IN ('approved', 'rejected', 'skipped')),
                reason TEXT NOT NULL,
                machine_review_status VARCHAR(40),
                machine_review_version VARCHAR(100),
                reviewed_content_hash CHAR(64),
                evidence_manifest_hash CHAR(64),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        _safe_add_column(cursor, "geo_article_review_events", "reviewed_content_hash", "CHAR(64)")
        _safe_add_column(cursor, "geo_article_review_events", "evidence_manifest_hash", "CHAR(64)")
        # [发布门三态拆分 2026-07-31 · §4.3 五字段留痕] before 态(决策前的人审状态)。
        # nullable / 无默认 / 无 CHECK / 不回填 —— 老行保持 NULL,旧代码不读它,
        # 回滚到上一版本也只是这一列没人写,不影响任何既有路径。
        _safe_add_column(
            cursor, "geo_article_review_events", "prior_human_review_status", "VARCHAR(40)",
        )
        widen_review_decision_check(cursor)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_geo_article_review_events_article "
            "ON geo_article_review_events(article_id, created_at DESC)"
        )
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS geo_article_gold_labels (
                id BIGSERIAL PRIMARY KEY,
                judge_kind VARCHAR(40) NOT NULL,
                source_id BIGINT NOT NULL,
                machine_label VARCHAR(64) NOT NULL,
                human_label VARCHAR(64) NOT NULL,
                input_snapshot JSONB NOT NULL,
                reviewer_user_id INTEGER NOT NULL,
                rationale TEXT NOT NULL,
                calibration_version VARCHAR(80) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (judge_kind, source_id, reviewer_user_id)
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_geo_article_gold_labels_kind "
            "ON geo_article_gold_labels(judge_kind, calibration_version, source_id)"
        )
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS geo_article_experiments (
                id BIGSERIAL PRIMARY KEY,
                experiment_key VARCHAR(40) NOT NULL UNIQUE,
                contract_version VARCHAR(80) NOT NULL,
                style_family VARCHAR(64) NOT NULL,
                hypothesis TEXT NOT NULL,
                single_change_dimension VARCHAR(80) NOT NULL,
                primary_metric VARCHAR(120) NOT NULL,
                baseline_version_id VARCHAR(200) NOT NULL,
                candidate_version_id VARCHAR(200) NOT NULL,
                scope JSONB NOT NULL DEFAULT '{}'::jsonb,
                min_arm_articles INTEGER NOT NULL DEFAULT 30,
                minimum_weeks INTEGER NOT NULL DEFAULT 4,
                frozen_config JSONB NOT NULL,
                state VARCHAR(40) NOT NULL DEFAULT 'preregistered',
                created_by INTEGER NOT NULL,
                approved_by INTEGER,
                approval_reason TEXT,
                approved_at TIMESTAMPTZ,
                started_at TIMESTAMPTZ,
                observation_end TIMESTAMPTZ,
                decision_by INTEGER,
                decision_reason TEXT,
                decision_snapshot JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_geo_article_experiments_candidate "
            "ON geo_article_experiments(candidate_version_id, state)"
        )
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS geo_article_experiment_assignments (
                id BIGSERIAL PRIMARY KEY,
                experiment_id BIGINT NOT NULL REFERENCES geo_article_experiments(id),
                article_id BIGINT REFERENCES articles(id),
                topic_id BIGINT NOT NULL REFERENCES topics(id),
                generation_request_id TEXT,
                arm VARCHAR(20) NOT NULL CHECK (arm IN ('control','candidate')),
                article_style_version VARCHAR(200) NOT NULL,
                assignment_hash CHAR(64) NOT NULL,
                assigned_by INTEGER NOT NULL,
                assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (experiment_id, topic_id),
                UNIQUE (experiment_id, article_id),
                UNIQUE (generation_request_id)
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_geo_article_experiment_assignments_topic "
            "ON geo_article_experiment_assignments(topic_id, experiment_id)"
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_geo_article_experiment_topic "
            "ON geo_article_experiment_assignments(experiment_id, topic_id) "
            "WHERE topic_id IS NOT NULL"
        )
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS geo_article_evolution_runs (
                id BIGSERIAL PRIMARY KEY,
                cycle_key VARCHAR(16) NOT NULL UNIQUE,
                cycle_version VARCHAR(80) NOT NULL,
                trigger_source VARCHAR(40) NOT NULL,
                state VARCHAR(40) NOT NULL,
                truth_level VARCHAR(40),
                data_health JSONB NOT NULL,
                corpus_summary JSONB NOT NULL,
                review_summary JSONB NOT NULL,
                experiment_summary JSONB NOT NULL,
                question_summary JSONB NOT NULL,
                cost_summary JSONB NOT NULL,
                recommendations JSONB NOT NULL,
                created_by INTEGER NOT NULL,
                started_at TIMESTAMPTZ NOT NULL,
                finished_at TIMESTAMPTZ,
                reviewed_by INTEGER,
                reviewed_at TIMESTAMPTZ,
                review_note TEXT
            )
        """)
        _safe_add_column(
            cursor, "geo_article_evolution_runs", "cost_summary",
            "JSONB NOT NULL DEFAULT '{}'::jsonb",
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_geo_article_evolution_runs_time "
            "ON geo_article_evolution_runs(started_at DESC)"
        )

        # P2 (2026-06-03) distilled 来源治理 · articles 记录用哪版蒸馏(与 quotes 一致 · 防过期溯源)
        _safe_add_column(cursor, "articles", "distilled_version", "INTEGER DEFAULT NULL")
        _safe_add_column(cursor, "articles", "distilled_source_hash", "VARCHAR(64) DEFAULT NULL")
        _safe_add_column(cursor, "articles", "distilled_at", "TIMESTAMP DEFAULT NULL")

        # P2 (2026-06-03) report_leads 幂等建表:全库历史无 CREATE TABLE(裸库依赖)·
        # 新库/测试库缺表会让代理端 /agent/leads 三处查询 500。此处幂等补建(prod 已存在则 no-op)。
        # 用途 = 代理端历史线索页兼容 · 绝不恢复客户面留资(/lead 永久 410 · 无任何客户面 CTA/表单)。
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS report_leads (
                    id SERIAL PRIMARY KEY,
                    diagnosis_id INTEGER,
                    phone VARCHAR(64),
                    company_name TEXT,
                    shared_by_user_id INTEGER,
                    status TEXT DEFAULT 'new',
                    created_at TIMESTAMP DEFAULT NOW(),
                    status_updated_at TIMESTAMP DEFAULT NULL
                )
            """)
        except Exception as _rle:
            print(f"[init_db] report_leads CREATE TABLE 异常(可能表已存在/并发): {_rle}")

        # P0.5 partial index 防软删过滤慢查询(ALTER TABLE 幂等 · CREATE INDEX IF NOT EXISTS 幂等)
        try:
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_quotes_deleted_at "
                "ON quotes(deleted_at) WHERE deleted_at IS NULL"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_articles_first_pub "
                "ON articles(first_published_at) WHERE first_published_at IS NOT NULL"
            )
            # v2.7.1:quality_warning 非空索引(admin 查质量警告文章快)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_articles_quality_warning_not_null "
                "ON articles((quality_warning IS NOT NULL)) WHERE quality_warning IS NOT NULL"
            )
        except Exception as _e:
            # 某些旧环境可能不支持 partial index · 不 block 启动
            print(f"  [WARN] P0.4/P0.5 partial index 创建失败(忽略): {_e}")

        # v1_2 migration: keyword_price_cache 加 industry/city 列（全局价格锁，不按品牌分 key）
        _safe_add_column(cursor, "keyword_price_cache", "industry", "TEXT")
        _safe_add_column(cursor, "keyword_price_cache", "city", "TEXT")

        # [D2 2026-06-05] keyword_price_cache 加算价底盘/复盘字段(§12 · additive 安全 · 老表不改现有列)
        #   目标:任何词「为什么是这个价」能 SQL 查出(keyword_type/竞争档/band/公式版本)· 不靠反推
        #   pricing_formula_version 供 D3 缓存失效判定(NULL 或 != CURRENT 视为 expired · 灰度/回滚)
        _safe_add_column(cursor, "keyword_price_cache", "keyword_type", "TEXT")
        _safe_add_column(cursor, "keyword_price_cache", "market_scope", "TEXT")
        _safe_add_column(cursor, "keyword_price_cache", "is_brand_keyword", "BOOLEAN")
        _safe_add_column(cursor, "keyword_price_cache", "is_broad", "BOOLEAN")
        _safe_add_column(cursor, "keyword_price_cache", "geo_multiplier", "REAL")
        _safe_add_column(cursor, "keyword_price_cache", "effective_competition", "INTEGER")
        _safe_add_column(cursor, "keyword_price_cache", "competition_band", "INTEGER")
        _safe_add_column(cursor, "keyword_price_cache", "pricing_formula_version", "TEXT")
        _safe_add_column(cursor, "keyword_price_cache", "raw_price_before_band", "REAL")
        _safe_add_column(cursor, "keyword_price_cache", "band_min", "REAL")
        _safe_add_column(cursor, "keyword_price_cache", "band_max", "REAL")
        _safe_add_column(cursor, "keyword_price_cache", "needs_review", "BOOLEAN")
        _safe_add_column(cursor, "keyword_price_cache", "classify_confidence", "REAL")
        _safe_add_column(cursor, "keyword_price_cache", "classify_reason", "TEXT")
        _safe_add_column(cursor, "keyword_price_cache", "classify_source", "TEXT")

        # [§4.2 2026-06-06] keyword_price_cache 加超红海标列(additive · BOOLEAN/REAL · SQL 4 维核验:
        #   列名 super_red_ocean/competition_ratio 全仓唯一 · super_red_ocean=BOOLEAN 同 is_broad/needs_review
        #   · competition_ratio=REAL 同 geo_multiplier(分数·非 INTEGER)· 归属 keyword_price_cache 非 _llm 表)
        _safe_add_column(cursor, "keyword_price_cache", "super_red_ocean", "BOOLEAN")
        _safe_add_column(cursor, "keyword_price_cache", "competition_ratio", "REAL")
        # [v2.1 2026-06-11] LLM 评估师算价底盘(true_competition/cost/media_tier/llm 双验/risk_flags)
        #   漏注册此列 → dev/CI/全新部署 init_db 后 save_keyword_prices_cache 每次写入静默失败
        #   (调用方 try/except 吞掉)→ 缓存永远写不进 → 每单全量重调双 LLM(Workflow 对抗验证 P1)
        _safe_add_column(cursor, "keyword_price_cache", "v2_assessor_data", "JSONB")

        # [完整修复 2026-06-13 Med2] LLM-first cache 表 assessor_version 列 init_db 自检兜底(belt-and-suspenders):
        #   该列由 scripts/migration_pricing_full_fix_2026_06_13.sql 加 · 此处兜底防 migration 漏跑/新环境时
        #   save_llm_keyword_prices_cache INSERT(assessor_version) 运行时报「column does not exist」。
        #   表本身由 migration_008_pricing_llm_first.sql 建;表不存在时 _safe_add_column 的 ALTER 失败被静默吞(安全)。
        _safe_add_column_optional(cursor, "keyword_price_cache_llm", "assessor_version", "TEXT")

        # 幂等 marker 登记（_migration_markers 表由其他模块先建，不存在也容错）
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS _migration_markers (
                    marker TEXT PRIMARY KEY,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    note TEXT
                )
            """)
            cursor.execute("SELECT 1 FROM _migration_markers WHERE marker = %s", ('v1_2_price_cache_global',))
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO _migration_markers (marker, note) VALUES (%s, %s)",
                    ('v1_2_price_cache_global', 'keyword_price_cache 加 industry/city 列，支持 (industry,city,keyword) 全局共享 7 天价格锁')
                )
            cursor.execute("SELECT 1 FROM _migration_markers WHERE marker = %s", ('pricing_v1_2_basis_cols',))
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO _migration_markers (marker, note) VALUES (%s, %s)",
                    ('pricing_v1_2_basis_cols', 'keyword_price_cache 加 D2 算价底盘/复盘字段(keyword_type/competition_band/band/pricing_formula_version 等) 2026-06-05')
                )
            cursor.execute("SELECT 1 FROM _migration_markers WHERE marker = %s", ('pricing_v1_3_super_red_ocean',))
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO _migration_markers (marker, note) VALUES (%s, %s)",
                    ('pricing_v1_3_super_red_ocean', 'keyword_price_cache 加 §4.2 超红海标列(super_red_ocean BOOLEAN / competition_ratio REAL) 2026-06-06')
                )
        except Exception:
            pass  # marker 登记失败不影响 ALTER 本体生效

        for idx_sql in indexes:
            # 🔴 [R5 2026-08-20] 不再裸 execute:既存窄表会让某一条 CREATE INDEX
            #   当场 UndefinedColumn,把整个 init_db 打断(后面几十张表全没建成)。
            #   守卫只跳「目标列不存在」这一种,并记账;全新空库上跳过集必须为空。
            execute_index_guarded(cursor, idx_sql)

        # ============================================
        # 媒体投放建议系统 (Placement Recommendation)
        # ============================================

        # 媒体知识库表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS media_outlets (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'traditional',
            platform TEXT,
            sivp_price REAL,
            min_price REAL,
            max_price REAL,
            ai_engines_covered TEXT DEFAULT '[]',
            ai_coverage_count INTEGER DEFAULT 0,
            geo_confirmed INTEGER DEFAULT 0,
            geo_notes TEXT,
            citation_count INTEGER DEFAULT 0,
            category TEXT,
            region TEXT,
            baidu_news_source INTEGER DEFAULT 0,
            suitable_industries TEXT DEFAULT '[]',
            data_source TEXT,
            source_url TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(name, platform, media_type)
        )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_media_geo ON media_outlets(geo_confirmed)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_media_ai_count ON media_outlets(ai_coverage_count)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_media_type ON media_outlets(media_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_media_category ON media_outlets(category)")

        # 投放建议表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS placement_recommendations (
            id SERIAL PRIMARY KEY,
            quote_id INTEGER NOT NULL,
            topic_id INTEGER,
            article_id INTEGER,
            recommended_outlets TEXT NOT NULL DEFAULT '[]',
            total_estimated_cost REAL,
            strategy_tier TEXT DEFAULT 'budget',
            llm_analysis TEXT,
            llm_model TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (quote_id) REFERENCES quotes(id)
        )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_placement_quote ON placement_recommendations(quote_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_placement_topic ON placement_recommendations(topic_id)")

        # 知识库核查 issue 持久化表(M 方案 · CTO-15.23 2026-05-06)
        # 老板需求:每条核查问题可单独勾选/忽略 · 不再一键全修 · 误报可累积
        # dedup_hash 防同一个问题重复入库(quote_id+topic_id+issue_type+kb_data+article_data 哈希)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS kb_check_issues (
            id BIGSERIAL PRIMARY KEY,
            quote_id INTEGER NOT NULL,
            topic_id INTEGER NOT NULL,
            article_id INTEGER,
            issue_type TEXT NOT NULL,
            kb_data TEXT,
            article_data TEXT,
            context TEXT,
            severity TEXT DEFAULT 'medium',
            dedup_hash TEXT NOT NULL,
            dismissed BOOLEAN DEFAULT FALSE,
            dismissed_at TIMESTAMP,
            fixed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (quote_id, dedup_hash)
        )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_kb_issues_quote_topic ON kb_check_issues(quote_id, topic_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_kb_issues_active ON kb_check_issues(quote_id) WHERE dismissed = FALSE AND fixed_at IS NULL")

        # 媒体分析日志表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS media_analysis_log (
            id SERIAL PRIMARY KEY,
            task_id TEXT UNIQUE NOT NULL,
            file_name TEXT,
            file_type TEXT,
            status TEXT DEFAULT 'pending',
            total_rows INTEGER DEFAULT 0,
            processed_rows INTEGER DEFAULT 0,
            chunk_count INTEGER DEFAULT 0,
            new_outlets_count INTEGER DEFAULT 0,
            updated_outlets_count INTEGER DEFAULT 0,
            discoveries TEXT,
            insights TEXT,
            previous_log_id INTEGER,
            delta_summary TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            error_message TEXT
        )
        """)

        # ============================================
        # GEO 引擎引用调研数据（行业蒸馏系统）
        # ============================================

        # 调研原始数据：每次搜索的完整引用记录
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS geo_research_raw (
            id SERIAL PRIMARY KEY,
            industry TEXT NOT NULL,
            query TEXT NOT NULL,
            engine TEXT NOT NULL,
            cited_platform TEXT NOT NULL,
            cite_position INTEGER DEFAULT 0,
            cite_url TEXT,
            cite_title TEXT,
            cite_excerpt TEXT,
            answer_text TEXT,
            is_answer_cited BOOLEAN DEFAULT FALSE,
            adoption_rank INTEGER,
            batch_id TEXT,
            researcher TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_geo_raw_industry ON geo_research_raw(industry)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_geo_raw_engine ON geo_research_raw(engine)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_geo_raw_query ON geo_research_raw(query, engine)")

        # 迁移：为旧表添加新列（如果不存在）
        for col, col_type in [
            ("cite_title", "TEXT"),
            ("cite_excerpt", "TEXT"),
            ("answer_text", "TEXT"),
            ("is_answer_cited", "BOOLEAN DEFAULT FALSE"),
            ("adoption_rank", "INTEGER"),
            ("provider", "VARCHAR(64) DEFAULT 'legacy_unknown'"),
            ("model", "VARCHAR(128) DEFAULT 'legacy_unknown'"),
            ("model_revision", "VARCHAR(128) DEFAULT 'legacy_unknown'"),
            ("surface", "VARCHAR(64) DEFAULT 'legacy_unknown'"),
            ("search_mode", "VARCHAR(64) DEFAULT 'legacy_unknown'"),
            ("prompt_snapshot", "TEXT"),
        ]:
            _safe_add_column(cursor, "geo_research_raw", col, col_type)

        # 聚合统计：行业×引擎×平台 的引用概率（由原始数据自动计算）
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS geo_engine_stats (
            id SERIAL PRIMARY KEY,
            industry TEXT NOT NULL,
            engine TEXT NOT NULL,
            platform TEXT NOT NULL,
            citation_count INTEGER DEFAULT 0,
            total_queries INTEGER DEFAULT 0,
            citation_rate REAL DEFAULT 0,
            avg_position REAL DEFAULT 0,
            sample_queries INTEGER DEFAULT 0,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(industry, engine, platform)
        )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_geo_stats_industry ON geo_engine_stats(industry)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_geo_stats_engine ON geo_engine_stats(engine)")

        # 引擎市占率权重（可由调研数据更新）
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS geo_engine_weights (
            id SERIAL PRIMARY KEY,
            engine TEXT NOT NULL UNIQUE,
            weight REAL NOT NULL DEFAULT 0.25,
            mau_millions REAL,
            source TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # 月份权重覆盖表（2026-04-30 新增：按月加权聚合）
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS geo_month_weights (
            id            SERIAL PRIMARY KEY,
            industry      TEXT NOT NULL,
            year_month    TEXT NOT NULL,
            weight        REAL NOT NULL CHECK (weight >= 0 AND weight <= 10),
            note          TEXT,
            updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_by    TEXT,
            UNIQUE(industry, year_month)
        )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_month_weights_industry ON geo_month_weights(industry)")

        # 全局聚合配置表（2026-04-30 新增）
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS geo_aggregation_config (
            key           TEXT PRIMARY KEY,
            value         TEXT NOT NULL,
            description   TEXT,
            updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_by    TEXT
        )
        """)
        cursor.execute("""
        INSERT INTO geo_aggregation_config (key, value, description) VALUES
          ('decay_factor',          '0.3', '月份指数衰减系数：当月权重 1.0、上月 decay、上上月 decay²；推荐 0.3'),
          ('min_queries_per_month', '10',  '某月题数低于此值则跳过该月聚合，避免单月样本不足导致波动')
        ON CONFLICT (key) DO NOTHING
        """)

        # 调研批次记录
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS geo_research_batches (
            id SERIAL PRIMARY KEY,
            batch_id TEXT UNIQUE NOT NULL,
            industry TEXT NOT NULL,
            engine TEXT,
            query_count INTEGER DEFAULT 0,
            researcher TEXT,
            notes TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        )
        """)

        # ============================================
        # 选词报价会话表
        # ============================================
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS keyword_selection_sessions (
            id                SERIAL PRIMARY KEY,
            token             TEXT UNIQUE NOT NULL,
            quote_id          INTEGER UNIQUE NOT NULL,
            brand_id          INTEGER NOT NULL,
            created_by        INTEGER,

            keywords_snapshot  TEXT NOT NULL,
            business_lines     TEXT,

            status            TEXT DEFAULT 'selecting',
            expires_at        TEXT NOT NULL,

            selected_keyword_ids TEXT,
            custom_keywords      TEXT,
            keywords_submitted_at TEXT,

            pricing_data         TEXT,
            selected_tier        TEXT,
            final_keyword_ids    TEXT,
            confirmed_at         TEXT,
            confirmed_total_price REAL,

            pending_keywords     TEXT,

            total_time_seconds   INTEGER,
            visit_count          INTEGER DEFAULT 0,
            hesitation_summary   TEXT,

            created_at TEXT DEFAULT (NOW()),
            updated_at TEXT DEFAULT (NOW()),

            FOREIGN KEY (quote_id) REFERENCES quotes(id),
            FOREIGN KEY (brand_id) REFERENCES brands(id)
        )
        """)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_kss_token ON keyword_selection_sessions(token)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_kss_quote ON keyword_selection_sessions(quote_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_kss_status ON keyword_selection_sessions(status)")

        # ============================================
        # 选词行为事件日志表
        # ============================================
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS keyword_interaction_logs (
            id          SERIAL PRIMARY KEY,
            session_id  INTEGER NOT NULL,
            phase       TEXT NOT NULL,

            event_type  TEXT NOT NULL,
            keyword_id  INTEGER,
            keyword_text TEXT,
            event_data  TEXT,

            client_ip   TEXT,
            user_agent  TEXT,
            created_at  TEXT DEFAULT (NOW()),

            FOREIGN KEY (session_id) REFERENCES keyword_selection_sessions(id)
        )
        """)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_kil_session ON keyword_interaction_logs(session_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_kil_session_kw ON keyword_interaction_logs(session_id, keyword_id)")

        # ========== 选词会话表补齐：销售确认+收款字段 ==========
        for col, col_type in [
            ("business_lines", "TEXT"),
            ("final_price", "REAL"),
            ("discount_info", "TEXT"),
            ("gift_keywords", "TEXT"),
            ("gift_articles", "INTEGER DEFAULT 0"),
            ("sales_notes", "TEXT"),
            ("sales_confirmed_at", "TEXT"),
            ("payment_received_at", "TEXT"),
            ("payment_overdue_notified_days", "TEXT DEFAULT '[]'"),
        ]:
            _safe_add_column(cursor, "keyword_selection_sessions", col, col_type)

        # ============================================
        # 主题包聚类表（主题包报价模式）
        # ============================================
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS keyword_clusters (
            id SERIAL PRIMARY KEY,

            -- 关联
            quote_id INTEGER REFERENCES quotes(id),
            session_token TEXT,

            -- 主题包标识
            cluster_name TEXT NOT NULL,
            business_tag TEXT NOT NULL,
            city_tag TEXT,
            scenario_tag TEXT NOT NULL DEFAULT '通用',
            description TEXT,

            -- 词数统计
            core_keyword_count INTEGER DEFAULT 0,
            covered_keyword_count INTEGER DEFAULT 0,

            -- 三档定价（核心词价格之和）
            price_entry INTEGER DEFAULT 0,
            price_standard INTEGER DEFAULT 0,
            price_flagship INTEGER DEFAULT 0,

            -- 三档篇数（核心词篇数之和）
            articles_entry INTEGER DEFAULT 0,
            articles_standard INTEGER DEFAULT 0,
            articles_flagship INTEGER DEFAULT 0,

            -- "原价"展示（核心+附赠全部词的价格之和，划线价）
            full_price_entry INTEGER DEFAULT 0,
            full_price_standard INTEGER DEFAULT 0,
            full_price_flagship INTEGER DEFAULT 0,

            -- 客户是否选中此包
            is_selected BOOLEAN DEFAULT TRUE,

            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_kc_quote_id ON keyword_clusters(quote_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_kc_session ON keyword_clusters(session_token)")

        # ========== 主题包：扩展现有表 ==========
        # confirmed_keywords: 加 cluster_id + is_core
        _safe_add_column(cursor, "confirmed_keywords", "cluster_id",
                         "INTEGER REFERENCES keyword_clusters(id)")
        _safe_add_column(cursor, "confirmed_keywords", "is_core",
                         "BOOLEAN DEFAULT TRUE")

        # keyword_selection_sessions: 加 clusters_data (JSON)
        _safe_add_column(cursor, "keyword_selection_sessions", "clusters_data", "TEXT")

        # C4.1 (CTO-15.9 session 3 · 2026-04-25 · M1b §P1.3b 主题包+交付映射)
        # 每主题包补全:推荐平台 / 完成周期 / 交付备注 → 报价页代理直接看 "做哪些 · 在哪发 · 多久完"
        _safe_add_column(cursor, "keyword_clusters", "platforms_jsonb",
                         "JSONB DEFAULT '[]'")  # ['知乎','百家号','搜狐'] 等
        _safe_add_column(cursor, "keyword_clusters", "weeks_to_complete",
                         "INTEGER DEFAULT 4")  # 一般 1-8 周
        _safe_add_column(cursor, "keyword_clusters", "delivery_notes", "TEXT")

        # topics: 加 cluster_id
        _safe_add_column(cursor, "topics", "cluster_id",
                         "INTEGER REFERENCES keyword_clusters(id)")

        # topics: 加 is_optimize（标记优化追加文章）
        _safe_add_column(cursor, "topics", "is_optimize", "BOOLEAN DEFAULT FALSE")
        # topics: 加 fail_reason（记录生成失败原因，前端展示）
        _safe_add_column(cursor, "topics", "fail_reason", "TEXT")
        # Stable article task projection.  Historical rows remain compatible:
        # nullable fields mean no recorded modern task; revision 0 is legacy.
        _safe_add_column(cursor, "topics", "generation_request_id", "VARCHAR(128)")
        _safe_add_column(cursor, "topics", "generation_revision", "INTEGER NOT NULL DEFAULT 0")
        _safe_add_column(cursor, "topics", "generation_operation", "VARCHAR(32)")
        _safe_add_column(cursor, "topics", "generation_error_code", "VARCHAR(80)")
        _safe_add_column(cursor, "topics", "generation_error_message", "TEXT")
        _safe_add_column(cursor, "topics", "generation_retryable", "BOOLEAN")
        _safe_add_column(cursor, "topics", "generation_failure_phase", "VARCHAR(32)")
        _safe_add_column(cursor, "topics", "generation_refund_status", "VARCHAR(32)")
        # [统一 R3 · 2026-07-23 §五] 失败投影血缘:记录本次生成尝试判定所用的
        # 法律禁止清单版本(与 evidence_first_policy 常量同源;旧行 NULL=版本未知,不回填)
        _safe_add_column(cursor, "topics", "generation_legal_catalog_version", "VARCHAR(64)")
        # v2.9 GEO 文体改造 · topics.user_choice 持久化(选题阶段强制文体 → 写作大厅 dropdown 改后联动重写 → 启动写作时按此 user_choice 走对应模板)
        # 主线 SQL:scripts/migration_topics_user_choice.sql · 此处兜底防漏
        # 值域:NULL/auto/guide/comparison/risk/price/data/qa/checklist/case/story · 10 项 · 无 company
        _safe_add_column(cursor, "topics", "user_choice", "VARCHAR(32) DEFAULT NULL")

        # v2.10 文章方向配比器 · topics.user_choice_source 来源标记
        # 主线 SQL:scripts/migration_v210_user_choice_source.sql · 此处兜底防漏
        # 值域:NULL / 'manual'(单篇手动锁定 · 优先级最高 · 批量配比不覆盖)
        #       / 'batch_uniform'(v2.8 "全部:X 文体" 路径)
        #       / 'batch_distribution'(v2.10 自定义配比)
        # 防"单篇 > 批量"优先级被覆盖 · 单篇改回 auto 必须清 source=NULL 进入可分配池
        _safe_add_column(cursor, "topics", "user_choice_source", "VARCHAR(32) DEFAULT NULL")

        # [写作卡死根治 2026-06-08] topics.writing_started_at · 进入 writing 的时刻
        # scheduler 卡死自愈用它做精确判据(created_at 是选题生成时间不准 · 老选题刚开始写会被误杀)
        # 置 writing 时 SET NOW() · 释放/回退时清 NULL · watchdog 用 COALESCE(writing_started_at, created_at)
        _safe_add_column(cursor, "topics", "writing_started_at", "TIMESTAMP DEFAULT NULL")

        # [WO R2 返工 2026-08-09] topics.regenerate_started_at · 进入 regenerating 的时刻
        # 与上面 writing_started_at 同构、同理由:created_at 是**选题生成时间**,
        # 拿它当重生成超时判据 → 30 天前的老选题刚点重新生成就会被看门狗当场误杀。
        # 三个入态点各写 NOW()(regenerate_topic / 智能补足建行 / 智能补足重试),
        # watchdog 用 COALESCE(regenerate_started_at, created_at) —— NULL 只剩
        # 本次上线前就已在途的存量行(其中就包含 6122-6133 这批真卡死的)。
        _safe_add_column(cursor, "topics", "regenerate_started_at", "TIMESTAMP DEFAULT NULL")

        # v2.10.4 fixed company_profile slot 落地(Codex 四审 P0-1):
        # is_fixed:BOOLEAN · 标记企业介绍固定槽位(系统自动生成 · 不进配比器)
        # style_code:VARCHAR(64) · 单一权威字段(v2.7.1 articles 已有 · 此处 topics 同步)
        # 联合判定:is_fixed=True AND style_code='company_profile' → ArticleWriter 走 fixed slot 路径
        # 跟 v2.4 P0 #1 锁定的 user_choice 10 项无 company 一致(用户不可选 · 系统固定)
        _safe_add_column(cursor, "topics", "is_fixed", "BOOLEAN DEFAULT FALSE")
        _safe_add_column(cursor, "topics", "style_code", "VARCHAR(64) DEFAULT NULL")

        # ============================================
        # M1b (CTO-15.17 · 2026-04-26)
        #   6 层关键词矩阵持久化 + 主题包销售叙事字段
        #   classifier 在 services/keyword_layer_classifier.py
        #   builder 在 services/theme_package_builder.py
        # ============================================
        # confirmed_keywords:加 6 层归属 + reason + confidence
        # · layer 取值:brand_defense / category_grab / scenario_decision /
        #              geo_conversion / competitor_intercept / evidence_trust
        # · NULL 时 GET /api/quotes/{id} on-the-fly classify 兜底
        _safe_add_column(cursor, "confirmed_keywords", "layer", "TEXT DEFAULT NULL")
        _safe_add_column(cursor, "confirmed_keywords", "layer_reason", "TEXT DEFAULT NULL")
        _safe_add_column(cursor, "confirmed_keywords", "layer_confidence", "TEXT DEFAULT NULL")

        # [CTO-15.23 2026-05-08 监测乱入 + 归档机制]
        # 客户截图报:报价 4 词 · 实际付费 1 词 · 但监测列表显示 4 词都"已检测"(浪费 Kimi)
        # 真因:get_client_keywords 拉 confirmed_keywords WHERE quote_id 全表 · 没过滤客户实际选词
        # 修法:加 is_monitored 字段 · selection_api confirm_quote 同步标记 · monitoring SQL 过滤
        _safe_add_column(cursor, "confirmed_keywords", "is_monitored", "BOOLEAN DEFAULT FALSE")
        _safe_add_column(cursor, "confirmed_keywords", "monitoring_status", "TEXT DEFAULT 'active'")
        # active   监测中
        # archived 归档(达标完成 / 服务期到 自动转 / 代理手动归档)
        # deleted  软删除(归档区手动删 · 防误删追溯)
        _safe_add_column(cursor, "confirmed_keywords", "archived_at", "TIMESTAMP DEFAULT NULL")
        _safe_add_column(cursor, "confirmed_keywords", "archive_reason", "TEXT DEFAULT NULL")
        # [§4.2 2026-06-06] 超红海词标(确认时从 pricing_data/聚类词反规范化落 · daily 达标计数排除·不进出现率保证)
        _safe_add_column(cursor, "confirmed_keywords", "super_red_ocean", "BOOLEAN DEFAULT FALSE")
        # compliance_complete  达标完成
        # service_expired      服务期到
        # manual               代理手动归档
        # renewed              续费重新激活后这条字段会被清空

        # 索引:监测列表过滤主路径 · is_monitored=TRUE AND monitoring_status='active'
        try:
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_ck_monitored_status
                ON confirmed_keywords(quote_id, is_monitored, monitoring_status)
                WHERE is_monitored = TRUE
            """)
        except Exception:
            pass

        # keyword_clusters:加销售叙事字段(给客户讲方案的故事)
        # · primary_layer / problem_to_solve / target_customer_question / monitoring_keywords
        # · evidence_reason / package_source(auto/manual/fallback)/ package_confidence
        _safe_add_column(cursor, "keyword_clusters", "primary_layer", "TEXT DEFAULT NULL")
        _safe_add_column(cursor, "keyword_clusters", "problem_to_solve", "TEXT DEFAULT NULL")
        _safe_add_column(cursor, "keyword_clusters", "target_customer_question", "TEXT DEFAULT NULL")
        _safe_add_column(cursor, "keyword_clusters", "monitoring_keywords_jsonb", "JSONB DEFAULT '[]'")
        _safe_add_column(cursor, "keyword_clusters", "evidence_reason", "TEXT DEFAULT NULL")
        _safe_add_column(cursor, "keyword_clusters", "package_source", "TEXT DEFAULT 'manual'")
        _safe_add_column(cursor, "keyword_clusters", "package_confidence", "TEXT DEFAULT 'medium'")

        # 幂等 marker 登记(_migration_markers 表上文已建)
        try:
            cursor.execute("SELECT 1 FROM _migration_markers WHERE marker = %s",
                           ('m1b_keyword_layer_theme_package',))
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO _migration_markers (marker, note) VALUES (%s, %s)",
                    ('m1b_keyword_layer_theme_package',
                     'M1b · 6 层关键词矩阵 + 主题包销售叙事字段 · CTO-15.17 2026-04-26')
                )
        except Exception:
            pass

        # ==================== GEO 调研监测 (Plan A 2026-05-06) ====================
        # 9 张新表(7 主 + 2 辅助)启动自检幂等创建,跟 scripts/migration_geo_research_monitor.sql 双写
        # spec: docs/superpowers/specs/2026-05-06-research-monitor-integration-design.md
        # 顺序:先父表(industries / round / articles),后子表(prompts / round_call / citations / review_log)

        # 1. 行业列表 (admin 可 CRUD,软删)
        try:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS geo_research_industries (
                    id BIGSERIAL PRIMARY KEY,
                    name VARCHAR(100) NOT NULL UNIQUE,
                    slug VARCHAR(100) NOT NULL UNIQUE,
                    sort_order INTEGER DEFAULT 0,
                    weight DECIMAL(4,2) DEFAULT 1.0,
                    active BOOLEAN DEFAULT TRUE,
                    ai_seed_done BOOLEAN DEFAULT FALSE,
                    version INTEGER DEFAULT 1,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            ''')
            cursor.execute("COMMENT ON TABLE geo_research_industries IS 'GEO 调研监测 - 行业列表 (admin 可 CRUD,软删)'")
            cursor.execute("COMMENT ON COLUMN geo_research_industries.name IS '行业名,跟 geo_research_raw.industry 字符串对齐'")
            cursor.execute("COMMENT ON COLUMN geo_research_industries.version IS '乐观锁版本号,每次 PATCH/DELETE 自增'")
            cursor.execute("COMMENT ON COLUMN geo_research_industries.updated_at IS '由应用层 UPDATE 时维护(无 trigger)'")
            print("[OK] geo_research_industries 表已创建")
        except Exception as e:
            print(f"[WARN] geo_research_industries: {e}")

        # 2. per-行业 prompts (admin 可 CRUD,软删)
        try:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS geo_research_prompts (
                    id BIGSERIAL PRIMARY KEY,
                    industry_id BIGINT NOT NULL REFERENCES geo_research_industries(id),
                    prompt_text TEXT NOT NULL,
                    sort_order INTEGER DEFAULT 0,
                    active BOOLEAN DEFAULT TRUE,
                    source VARCHAR(20) DEFAULT 'manual'
                        CHECK (source IN ('manual', 'seed_history', 'ai_generated')),
                    version INTEGER DEFAULT 1,
                    is_sensitive BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_geo_research_prompts_industry_active ON geo_research_prompts(industry_id, active)')
            cursor.execute("COMMENT ON TABLE geo_research_prompts IS 'GEO 调研监测 - per-行业 prompts (admin 可 CRUD,软删)'")
            cursor.execute("COMMENT ON COLUMN geo_research_prompts.is_sensitive IS '品牌相关 prompts 标 sensitive,预算降级时优先保留'")
            cursor.execute("COMMENT ON COLUMN geo_research_prompts.version IS '乐观锁版本号'")
            cursor.execute("COMMENT ON COLUMN geo_research_prompts.updated_at IS '由应用层 UPDATE 时维护(无 trigger)'")
            print("[OK] geo_research_prompts 表已创建")
        except Exception as e:
            print(f"[WARN] geo_research_prompts: {e}")

        # A15 additive question-evolution metadata. Purchased monitoring questions
        # remain in their existing tables and are never rewritten from this object.
        for col, col_type in [
            ("family_key", "VARCHAR(100)"),
            ("parent_prompt_id", "BIGINT"),
            ("question_version", "INTEGER NOT NULL DEFAULT 1"),
            ("query_kind", "VARCHAR(40) NOT NULL DEFAULT 'research'"),
            ("source_type", "VARCHAR(40) NOT NULL DEFAULT 'manual'"),
            ("hypothesis", "TEXT"),
            ("single_change_dimension", "TEXT"),
            ("experiment_group", "VARCHAR(40) NOT NULL DEFAULT 'shadow'"),
            ("evolution_status", "VARCHAR(40) NOT NULL DEFAULT 'draft'"),
            ("policy_version", "VARCHAR(80) NOT NULL DEFAULT 'question-evolution-v1'"),
            ("approved_by", "TEXT"),
            ("approved_at", "TIMESTAMPTZ"),
            ("activated_at", "TIMESTAMPTZ"),
            ("retired_at", "TIMESTAMPTZ"),
        ]:
            _safe_add_column(cursor, "geo_research_prompts", col, col_type)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS geo_question_evolution_events (
                id BIGSERIAL PRIMARY KEY,
                prompt_id BIGINT NOT NULL REFERENCES geo_research_prompts(id),
                actor_user_id INTEGER NOT NULL,
                action VARCHAR(40) NOT NULL,
                from_status VARCHAR(40),
                to_status VARCHAR(40),
                reason TEXT NOT NULL,
                policy_version VARCHAR(80) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_geo_question_evolution_events_prompt "
            "ON geo_question_evolution_events(prompt_id, created_at DESC)"
        )

        # 3. 跑批主记录 + 进度
        try:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS geo_research_round (
                    id BIGSERIAL PRIMARY KEY,
                    round_id VARCHAR(50) NOT NULL UNIQUE,
                    batch_id VARCHAR(50) NOT NULL,
                    triggered_by VARCHAR(30) NOT NULL
                        CHECK (triggered_by IN ('cron', 'manual', 'missed_cron_recovery', 'selfserve')),
                    triggered_user_id BIGINT,
                    industries_filter JSONB,
                    status VARCHAR(20) NOT NULL
                        CHECK (status IN ('pending', 'running', 'partial_success', 'failed', 'cancelled', 'completed', 'failed_resumable')),
                    current_stage VARCHAR(40),
                    progress_json JSONB,
                    snapshot_json JSONB,
                    last_heartbeat_at TIMESTAMPTZ,
                    started_at TIMESTAMPTZ,
                    finished_at TIMESTAMPTZ,
                    error_message TEXT,
                    summary_json JSONB,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_geo_research_round_status_started ON geo_research_round(status, started_at DESC)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_geo_research_round_batch_id ON geo_research_round(batch_id)')
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_geo_research_round_heartbeat ON geo_research_round(last_heartbeat_at) WHERE status = 'running'")
            cursor.execute("COMMENT ON TABLE geo_research_round IS 'GEO 调研监测 - 跑批主记录与进度'")
            cursor.execute("COMMENT ON COLUMN geo_research_round.snapshot_json IS '跑批启动时深拷贝的 industries+prompts 快照,运行中改动不影响本轮'")
            cursor.execute("COMMENT ON COLUMN geo_research_round.last_heartbeat_at IS '心跳时间,用于服务器重启检测 stale 跑批 (lease 5min)'")
            print("[OK] geo_research_round 表已创建")
        except Exception as e:
            print(f"[WARN] geo_research_round: {e}")

        # 4. 单条 prompt × 平台 调用日志
        try:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS geo_research_round_call (
                    id BIGSERIAL PRIMARY KEY,
                    round_id VARCHAR(50) NOT NULL REFERENCES geo_research_round(round_id) ON DELETE CASCADE,
                    industry_id BIGINT,
                    prompt_id BIGINT,
                    prompt_text TEXT,
                    platform VARCHAR(20) NOT NULL
                        CHECK (platform IN ('doubao', 'deepseek', 'qwen', 'kimi')),
                    status VARCHAR(20) NOT NULL
                        CHECK (status IN ('pending', 'success', 'failed', 'skipped')),
                    attempts SMALLINT DEFAULT 0,
                    raw_response_oss_key VARCHAR(500),
                    citations_count INTEGER DEFAULT 0,
                    error_message TEXT,
                    started_at TIMESTAMPTZ,
                    finished_at TIMESTAMPTZ
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_geo_research_round_call_round_status ON geo_research_round_call(round_id, status)')
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_geo_research_round_call_failed ON geo_research_round_call(status) WHERE status = 'failed'")
            cursor.execute("COMMENT ON TABLE geo_research_round_call IS 'GEO 调研监测 - 单条 prompt × 平台调用日志,单条 commit 保证可恢复'")
            print("[OK] geo_research_round_call 表已创建")
        except Exception as e:
            print(f"[WARN] geo_research_round_call: {e}")

        # 5. 爬取的文章 (URL 全局唯一,只爬一次)
        try:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS geo_research_articles (
                    id BIGSERIAL PRIMARY KEY,
                    url TEXT NOT NULL UNIQUE,
                    url_hash CHAR(40) NOT NULL UNIQUE,
                    domain VARCHAR(200) NOT NULL,
                    title TEXT,
                    primary_industry VARCHAR(100),
                    oss_key_raw VARCHAR(500),
                    oss_key_cleaned VARCHAR(500),
                    raw_char_count INTEGER,
                    cleaned_char_count INTEGER,
                    domain_tier VARCHAR(20) DEFAULT 'gray'
                        CHECK (domain_tier IN ('whitelist', 'gray', 'blacklist')),
                    cleanliness_score INTEGER,
                    score_reason TEXT,
                    clean_status VARCHAR(20) DEFAULT 'pending'
                        CHECK (clean_status IN ('pending', 'cleaned', 'failed')),
                    clean_model VARCHAR(50),
                    clean_attempts SMALLINT DEFAULT 0,
                    last_cleaned_at TIMESTAMPTZ,
                    -- P14.4 E (2026-06): VARCHAR(20)→32 · 'imported_to_reference' 长度 21 撑爆原 20 列
                    -- migration: scripts/migration_review_status_varchar32_2026-06-01.sql
                    review_status VARCHAR(32) DEFAULT 'crawled'
                        CHECK (review_status IN ('crawled', 'pending_review', 'auto_skipped', 'approved', 'rejected')),
                    review_note TEXT,
                    reviewed_by VARCHAR(50),
                    reviewed_at TIMESTAMPTZ,
                    reference_article_id BIGINT,
                    first_seen_round_id VARCHAR(50),
                    last_seen_at TIMESTAMPTZ DEFAULT NOW(),
                    fetched_at TIMESTAMPTZ DEFAULT NOW(),
                    expired BOOLEAN DEFAULT FALSE,
                    expired_at TIMESTAMPTZ,
                    locked_by VARCHAR(50),
                    locked_at TIMESTAMPTZ,
                    content_hash CHAR(64),
                    is_duplicate BOOLEAN DEFAULT FALSE,
                    primary_article_id BIGINT REFERENCES geo_research_articles(id) ON DELETE SET NULL
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_geo_research_articles_review_status_score ON geo_research_articles(review_status, cleanliness_score DESC)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_geo_research_articles_clean_status ON geo_research_articles(clean_status)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_geo_research_articles_domain ON geo_research_articles(domain)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_geo_research_articles_industry ON geo_research_articles(primary_industry)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_geo_research_articles_expired_fetched ON geo_research_articles(expired, fetched_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_geo_research_articles_locked ON geo_research_articles(locked_by) WHERE locked_by IS NOT NULL')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_geo_research_articles_content_hash ON geo_research_articles(content_hash) WHERE content_hash IS NOT NULL')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_geo_research_articles_primary_article ON geo_research_articles(primary_article_id) WHERE primary_article_id IS NOT NULL')
            _safe_add_column(cursor, "geo_research_articles", "total_citation_count", "INTEGER NOT NULL DEFAULT 0")
            _safe_add_column(cursor, "geo_research_articles", "score_attempts", "SMALLINT NOT NULL DEFAULT 0")
            # Phase 9 · 2026-05-25 · 内容类型 7 分类 (domain+url+text_chars 启发式 · 不强制 NOT NULL 兼容老数据)
            _safe_add_column(cursor, "geo_research_articles", "content_type", "VARCHAR(20)")
            # Phase 9 · 2026-05-26 · 老数据(本地 AI回答爬虫)迁移用 · OSS 不可达时存 cleaned 正文到 DB
            # 新跑批仍走 OSS (stage 4 不变) · 文章库 API 详情接口 fallback 读 inline
            _safe_add_column(cursor, "geo_research_articles", "inline_cleaned_content", "TEXT")
            # P15 · 文章意图 8 分类 (榜单/指南/长文/对比/报告/政策/百科/FAQ)
            _safe_add_column(cursor, "geo_research_articles", "intent_type", "VARCHAR(20)")
            _safe_add_column(cursor, "geo_research_articles", "intent_confidence", "NUMERIC(4,3)")
            _safe_add_column(cursor, "geo_research_articles", "intent_reason", "TEXT")
            _safe_add_column(cursor, "geo_research_articles", "intent_model", "VARCHAR(50)")
            _safe_add_column(cursor, "geo_research_articles", "intent_classified_at", "TIMESTAMPTZ")
            # JCC quality/provenance fields. Historical rows intentionally start JC0/legacy.
            _safe_add_column(cursor, "geo_research_articles", "corpus_grade", "VARCHAR(8) DEFAULT 'JC0'")
            _safe_add_column(cursor, "geo_research_articles", "canonical_body_hash", "CHAR(64)")
            _safe_add_column(cursor, "geo_research_articles", "body_hash_algorithm", "VARCHAR(80)")
            _safe_add_column(cursor, "geo_research_articles", "content_cluster_id", "VARCHAR(80)")
            _safe_add_column(cursor, "geo_research_articles", "body_boundary_version", "VARCHAR(80)")
            _safe_add_column(cursor, "geo_research_articles", "label_provenance_version", "VARCHAR(80)")
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS geo_research_corpus_label_events (
                    id BIGSERIAL PRIMARY KEY,
                    article_id BIGINT NOT NULL REFERENCES geo_research_articles(id),
                    from_grade VARCHAR(8) NOT NULL,
                    to_grade VARCHAR(8) NOT NULL,
                    labeler_version VARCHAR(80) NOT NULL,
                    direct_signal_count INTEGER NOT NULL,
                    evidence JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (article_id, labeler_version)
                )
            ''')
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_geo_research_corpus_label_events_time "
                "ON geo_research_corpus_label_events(created_at DESC)"
            )

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS geo_research_article_fetches (
                    id BIGSERIAL PRIMARY KEY,
                    fetch_event_key VARCHAR(64) NOT NULL UNIQUE,
                    article_id BIGINT REFERENCES geo_research_articles(id) ON DELETE SET NULL,
                    source_url TEXT NOT NULL,
                    normalized_url TEXT NOT NULL,
                    final_url TEXT,
                    url_hash CHAR(40),
                    parent_fetch_id BIGINT REFERENCES geo_research_article_fetches(id) ON DELETE SET NULL,
                    request_profile VARCHAR(80) NOT NULL,
                    request_profile_version VARCHAR(80) NOT NULL,
                    preset VARCHAR(40),
                    engine VARCHAR(40),
                    cache_policy VARCHAR(40),
                    timeout_seconds INTEGER,
                    token_budget INTEGER,
                    attempt_number INTEGER NOT NULL,
                    response_status VARCHAR(40) NOT NULL,
                    http_status INTEGER,
                    warning TEXT,
                    failure_reason VARCHAR(80),
                    published_time TIMESTAMPTZ,
                    fetched_at TIMESTAMPTZ NOT NULL,
                    latency_ms INTEGER,
                    usage_tokens INTEGER,
                    parser_version VARCHAR(80) NOT NULL,
                    raw_object_key TEXT,
                    raw_response_hash CHAR(64),
                    raw_body_hash CHAR(64),
                    body_object_key TEXT,
                    body_hash CHAR(64),
                    robots_policy VARCHAR(40),
                    robots_reason TEXT,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            ''')
            _safe_add_column(cursor, "geo_research_article_fetches", "raw_body_hash", "CHAR(64)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_geo_fetches_article_time ON geo_research_article_fetches(article_id, fetched_at DESC)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_geo_fetches_url_time ON geo_research_article_fetches(url_hash, fetched_at DESC)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_geo_research_articles_pending_top ON geo_research_articles(review_status, total_citation_count DESC, cleaned_char_count DESC) WHERE review_status = 'pending_review'")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_geo_research_articles_content_type ON geo_research_articles(content_type) WHERE content_type IS NOT NULL")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_geo_research_articles_intent_type ON geo_research_articles(intent_type) WHERE intent_type IS NOT NULL")
            try:
                cursor.execute("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1
                              FROM pg_constraint
                             WHERE conname = 'geo_research_articles_intent_type_check'
                        ) THEN
                            ALTER TABLE geo_research_articles
                                ADD CONSTRAINT geo_research_articles_intent_type_check
                                CHECK (intent_type IS NULL OR intent_type IN (
                                    'ranking', 'tutorial', 'long_form', 'comparison',
                                    'data_report', 'policy', 'definition', 'faq'
                                ));
                        END IF;
                    END $$;
                """)
            except Exception as e:
                print(f"[WARN] geo_research_articles.intent_type CHECK: {e}")
            # P14.4 E (2026-06-01) · review_status VARCHAR(20)→32 · 'imported_to_reference' (21字) 撑爆原 20
            # 之前症状: 加入参考库点击 → reference_articles 插入成功 → UPDATE review_status 报
            # "value too long for type character varying(20)" → 500 给前端
            # 幂等 · 已是 32 时 ALTER 不报错 · PostgreSQL 对增大列宽是非阻塞的
            try:
                # [WO_285b] 已是 varchar(32) 就不动:ALTER COLUMN TYPE 同样要 ACCESS EXCLUSIVE(init_db 在请求路径上)
                alter_column_type_if_changed(cursor, "geo_research_articles", "review_status",
                                             "VARCHAR(32)", "character varying(32)")
                print("[OK] geo_research_articles.review_status TYPE VARCHAR(32) [P14.4 E]")
            except Exception as _e_widen:
                print(f"[migration] WIDEN review_status 忽略: {_e_widen}")
            try:
                for _col in ("prev_review_status", "new_review_status"):  # [WO_285b] 同上
                    alter_column_type_if_changed(cursor, "geo_research_review_log", _col,
                                                 "VARCHAR(32)", "character varying(32)")
                print("[OK] geo_research_review_log.{prev,new}_review_status TYPE VARCHAR(32) [P14.4 E]")
            except Exception as _e_widen_log:
                print(f"[migration] WIDEN review_log status 忽略: {_e_widen_log}")
            # Phase 9 · 2026-05-25 · review_status CHECK 扩 in_library + imported_to_reference
            # 老 5 值保留兼容历史数据(P05f migration 时把 pending_review → in_library)
            # [WO_285b] 允许值集合与期望相同就不动,不同(或没有)才 DROP + ADD(init_db 在请求路径上,原先每次都 DROP+ADD)
            try:
                replace_in_list_check_if_changed(
                    cursor, "geo_research_articles", "geo_research_articles_review_status_check", "review_status",
                    ['crawled', 'pending_review', 'auto_skipped', 'approved', 'rejected',
                     'in_library', 'imported_to_reference'])
                print("[OK] geo_research_articles.review_status CHECK 已扩 in_library + imported_to_reference [Phase 9]")
            except Exception as _e_add:
                print(f"[migration] ADD review_status CHECK 失败(可能已是新版,忽略): {_e_add}")
            # 加 in_library partial index 给文章库 API 用(默认查询热路径)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_geo_research_articles_in_library ON geo_research_articles(first_seen_round_id, total_citation_count DESC) WHERE review_status = 'in_library'")
            # Phase 9 · 2026-05-25 · 老审核数据 migration (一次性 · 幂等)
            # pending_review (待审) + approved (审过同意) 都视为入文章库
            # rejected 保留原状态(管理员历史决策不动)
            try:
                cursor.execute("""
                    UPDATE geo_research_articles
                       SET review_status = 'in_library'
                     WHERE review_status IN ('pending_review', 'approved')
                """)
                migrated_rows = cursor.rowcount
                if migrated_rows > 0:
                    print(f"[Phase 9 migration] {migrated_rows} 行老 pending_review/approved → in_library")
            except Exception as _e_mig:
                print(f"[Phase 9 migration] 老审核数据迁移失败(忽略): {_e_mig}")
            cursor.execute("COMMENT ON INDEX idx_geo_research_articles_review_status_score IS '复合索引,前缀已覆盖按 review_status 单独查'")
            cursor.execute("COMMENT ON TABLE geo_research_articles IS 'GEO 调研监测 - 爬取的文章 (URL 全局唯一,跨轮命中复用)'")
            cursor.execute("COMMENT ON COLUMN geo_research_articles.content_hash IS 'sha256(cleaned_content[:1000]),用于正文去重'")
            cursor.execute("COMMENT ON COLUMN geo_research_articles.is_duplicate IS '是否为转载/重复正文,审核默认只显示 is_duplicate=false'")
            cursor.execute("COMMENT ON COLUMN geo_research_articles.primary_article_id IS '若 is_duplicate=true,指向首发 article 的 id'")
            cursor.execute("COMMENT ON COLUMN geo_research_articles.locked_by IS '审核操作行级锁:当前持锁 admin id,>5min 自动释放'")
            cursor.execute("COMMENT ON COLUMN geo_research_articles.total_citation_count IS '累计被引用次数(跨多轮多 AI),由 round_runner stage 7 之前重算'")
            cursor.execute("COMMENT ON COLUMN geo_research_articles.score_attempts IS '累计 LLM 评分失败次数,满阈值后 review_status 软着 auto_skipped 防卡死'")
            cursor.execute("COMMENT ON COLUMN geo_research_articles.content_type IS 'Phase 9 · 内容类型 7 分类: video/article/doc_tool/encyc/ecom/gov/other'")
            print("[OK] geo_research_articles 表已创建")
        except Exception as e:
            print(f"[WARN] geo_research_articles: {e}")

        # 6. 文章引用关联表 (多对多)
        try:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS geo_research_article_citations (
                    id BIGSERIAL PRIMARY KEY,
                    article_id BIGINT NOT NULL REFERENCES geo_research_articles(id) ON DELETE CASCADE,
                    round_id VARCHAR(50) NOT NULL REFERENCES geo_research_round(round_id) ON DELETE CASCADE,
                    industry_id BIGINT REFERENCES geo_research_industries(id),
                    prompt_id BIGINT REFERENCES geo_research_prompts(id),
                    platform VARCHAR(20) NOT NULL
                        CHECK (platform IN ('doubao', 'deepseek', 'qwen', 'kimi')),
                    raw_id BIGINT,
                    rank_in_response SMALLINT,
                    cited_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE (article_id, round_id, prompt_id, platform)
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_geo_research_citations_article ON geo_research_article_citations(article_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_geo_research_citations_round ON geo_research_article_citations(round_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_geo_research_citations_round_platform ON geo_research_article_citations(round_id, platform)')
            cursor.execute("COMMENT ON TABLE geo_research_article_citations IS 'GEO 调研监测 - 文章引用关联表 (多对多,每轮新增)'")
            print("[OK] geo_research_article_citations 表已创建")
        except Exception as e:
            print(f"[WARN] geo_research_article_citations: {e}")

        # 7. 审核日志 (审计 + 30 分钟撤销窗口)
        try:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS geo_research_review_log (
                    id BIGSERIAL PRIMARY KEY,
                    article_id BIGINT NOT NULL REFERENCES geo_research_articles(id) ON DELETE RESTRICT,
                    action VARCHAR(20) NOT NULL
                        CHECK (action IN ('approve', 'approve_edited', 'reject', 'reclean', 'undo', 'bulk_approve', 'bulk_reject')),
                    -- P14.4 E (2026-06): 跟 geo_research_articles.review_status 一致 · VARCHAR(32)
                    prev_review_status VARCHAR(32) NOT NULL,
                    new_review_status VARCHAR(32) NOT NULL,
                    reference_article_id_before BIGINT,
                    reference_article_id_after BIGINT,
                    reason VARCHAR(40),
                    note TEXT,
                    edited_content_oss_key VARCHAR(500),
                    operator_id VARCHAR(50) NOT NULL,
                    operator_name VARCHAR(100),
                    operated_at TIMESTAMPTZ DEFAULT NOW(),
                    bulk_id VARCHAR(50),
                    undone_by VARCHAR(50),
                    undone_at TIMESTAMPTZ
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_geo_research_review_log_article_time ON geo_research_review_log(article_id, operated_at DESC)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_geo_research_review_log_operator_time ON geo_research_review_log(operator_id, operated_at DESC)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_geo_research_review_log_undoable ON geo_research_review_log(operated_at, undone_by) WHERE undone_by IS NULL')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_geo_research_review_log_bulk_id ON geo_research_review_log(bulk_id) WHERE bulk_id IS NOT NULL')
            cursor.execute("COMMENT ON TABLE geo_research_review_log IS 'GEO 调研监测 - 审核日志 (审计 + 30 分钟撤销窗口实现)'")
            cursor.execute("COMMENT ON COLUMN geo_research_review_log.bulk_id IS '批量操作 ID,同一次 bulk-action 的 N 条 log 共享,单条操作为 NULL'")
            print("[OK] geo_research_review_log 表已创建")
        except Exception as e:
            print(f"[WARN] geo_research_review_log: {e}")

        # 8. 跑批成本日志表 (预算熔断 + 月度汇总)
        try:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS geo_research_cost_log (
                    id BIGSERIAL PRIMARY KEY,
                    round_id VARCHAR(50),
                    item VARCHAR(50) NOT NULL,
                    amount_yuan DECIMAL(10, 4) NOT NULL,
                    request_count INTEGER DEFAULT 0,
                    recorded_at TIMESTAMPTZ DEFAULT NOW()
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_geo_research_cost_log_round ON geo_research_cost_log(round_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_geo_research_cost_log_recorded_at ON geo_research_cost_log(recorded_at)')
            cursor.execute("COMMENT ON TABLE geo_research_cost_log IS 'GEO 调研监测 - 跑批成本日志(预算熔断 + 月度汇总用)'")
            print("[OK] geo_research_cost_log 表已创建")
        except Exception as e:
            print(f"[WARN] geo_research_cost_log: {e}")

        # 9. 系统配置表 (熔断阈值 / 月度预算上限 / TTL)
        try:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS geo_research_config (
                    key VARCHAR(100) PRIMARY KEY,
                    value_json JSONB NOT NULL,
                    description TEXT,
                    updated_by VARCHAR(50),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            ''')
            cursor.execute("COMMENT ON TABLE geo_research_config IS 'GEO 调研监测 - 系统配置(熔断阈值/预算上限/TTL 等运行时可调参数)'")
            cursor.execute("COMMENT ON COLUMN geo_research_config.updated_at IS '由应用层 UPDATE 时维护(无 trigger)'")
            print("[OK] geo_research_config 表已创建")
        except Exception as e:
            print(f"[WARN] geo_research_config: {e}")

        # 默认配置项 (ON CONFLICT 幂等)
        # Phase 9 (2026-05-26) · 审核流程删除后语义/默认值更新:
        #   - article_min_chars_for_review: 3000 → 100 · 旧"推审核门槛" → 新"入文章库门槛"
        #     (老板要求 100 字 · 配 stage 5 把 cleaned + >=100 字 + 非 dup 入 'in_library')
        #   - clean_attempts_max / undo_window_minutes: 直接物理删除
        #     (审核 + LLM 清洗都删了 · 留 config 行只会让管理员困惑)
        #   - lock_stale_minutes: 保留 · 管理员编辑文章库时用行级锁
        #   - model_* (4 项): platforms.py 从这里读模型名 · 不带价格(成本字典在 round_runner.py)
        try:
            cursor.execute('''
                INSERT INTO geo_research_config (key, value_json, description) VALUES
                    ('circuit_breaker_consecutive', '50', '连续失败 N 次熔断'),
                    ('circuit_breaker_rate', '0.5', '整体失败率阈值(超过即熔断)'),
                    ('circuit_breaker_min_processed', '100', '至少跑了 N 次才开始判失败率'),
                    ('budget_per_round_yuan', '350', '单轮预算上限(元)'),
                    ('budget_per_month_yuan', '1000', '月度预算上限(元)'),
                    ('article_oss_ttl_days', '180', 'OSS 文章保留天数'),
                    ('lock_stale_minutes', '5', '行级锁 stale 自动释放时间(分钟 · 管理员编辑文章库用)'),
                    ('article_min_chars_for_review', '100', '入文章库门槛(清洗后正文字数)'),
                    ('model_doubao_app', '"doubao-seed-2-0-lite-260215"', '豆包 doubao_app + ai_search 底座模型(env DOUBAO_APP_MODEL 可覆盖)'),
                    ('model_deepseek_via_dashscope', '"deepseek-v4-flash"', 'DeepSeek 走阿里百炼 dashscope generation 的模型名'),
                    ('model_qwen_default', '"qwen-plus-latest"', 'Qwen 默认模型(SystemSettings.research_monitor_tasks.platforms.qwen.model 优先)'),
                    ('model_kimi_via_dashscope', '"kimi/kimi-k2.6"', 'Kimi 走阿里百炼 dashscope chat/completions 兼容模式的模型名'),
                    -- P14-v10 (2026-05-28): 半月自动跑批 cron 配置 · 管理端可改 · 取代旧 env 开关
                    ('cron_enabled', 'true', '自动跑批总开关(true/false · 关掉后只能手动触发)'),
                    ('cron_days', '"1,16"', '每月哪几天自动跑(逗号分隔 · 1-28 · 默认 1+16 号)'),
                    ('cron_hour', '2', '自动跑批小时(0-23 · 北京时间 · 默认 02:00 凌晨)')
                    -- P14-v12 调研专用 key (已撤回 · 跟系统其他模块共用 DASHSCOPE_API_KEY)
                ON CONFLICT (key) DO NOTHING
            ''')
            # Phase 9 一次性 UPDATE 已有部署里的老值 (3000 → 100)
            # 仅当老 description 含"推送审核" (老语义) 才动 · 保护管理员已手动改的值
            cursor.execute('''
                UPDATE geo_research_config
                   SET value_json = '100'::jsonb,
                       description = '入文章库门槛(清洗后正文字数)'
                 WHERE key = 'article_min_chars_for_review'
                   AND description LIKE '%推送审核%'
            ''')
            # P14-v3 (2026-05-27): 再清一次老 description 残留的 "Phase 9 改 100" 工程备注
            # 老板看见后台 description 觉得是内部黑话 · 改成干净文案
            cursor.execute('''
                UPDATE geo_research_config
                   SET description = '入文章库门槛(清洗后正文字数)'
                 WHERE key = 'article_min_chars_for_review'
                   AND description LIKE '%Phase 9%'
            ''')
            if cursor.rowcount > 0:
                print(f"[Phase 9 migration] article_min_chars_for_review 3000 → 100 (老审核默认值)")
            # Phase 9 lock_stale_minutes 描述补全 (旧描述无"管理员编辑文章库"上下文)
            cursor.execute('''
                UPDATE geo_research_config
                   SET description = '行级锁 stale 自动释放时间(分钟 · 管理员编辑文章库用)'
                 WHERE key = 'lock_stale_minutes' AND description = '行级锁 stale 自动释放时间(分钟)'
            ''')
            # Phase 9 物理删除 deprecated 配置 (审核 + LLM 清洗已删 · 字段没人读)
            # 加 WHERE value_json 限定 · 避免管理员把这俩 key 改成 0 或自定义值后误删
            cursor.execute('''
                DELETE FROM geo_research_config
                 WHERE key = 'clean_attempts_max'
                   AND value_json IN ('3'::jsonb, '1'::jsonb)
            ''')
            if cursor.rowcount > 0:
                print(f"[Phase 9 migration] 删除 deprecated config: clean_attempts_max ({cursor.rowcount} 行)")
            cursor.execute('''
                DELETE FROM geo_research_config
                 WHERE key = 'undo_window_minutes'
                   AND value_json = '30'::jsonb
            ''')
            if cursor.rowcount > 0:
                print(f"[Phase 9 migration] 删除 deprecated config: undo_window_minutes ({cursor.rowcount} 行)")
            print("[OK] geo_research_config 默认配置已就绪")
        except Exception as e:
            print(f"[WARN] geo_research_config 默认配置: {e}")

        # 条件 FK: reference_articles 由另一子系统创建,存在时才补 FK
        try:
            cursor.execute('''
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'reference_articles')
                    THEN
                        ALTER TABLE reference_articles ADD COLUMN IF NOT EXISTS archive_reason VARCHAR(50);
                        ALTER TABLE reference_articles ADD COLUMN IF NOT EXISTS source_article_id BIGINT;
                        CREATE INDEX IF NOT EXISTS idx_reference_articles_source_article
                            ON reference_articles(source_article_id) WHERE source_article_id IS NOT NULL;
                    END IF;
                END$$;
            ''')
            cursor.execute('''
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'reference_articles')
                       AND NOT EXISTS (
                           SELECT 1 FROM information_schema.table_constraints
                           WHERE constraint_name = 'fk_geo_research_articles_reference_article_id'
                       )
                    THEN
                        ALTER TABLE geo_research_articles
                            ADD CONSTRAINT fk_geo_research_articles_reference_article_id
                            FOREIGN KEY (reference_article_id) REFERENCES reference_articles(id) ON DELETE SET NULL;
                    END IF;
                END$$;
            ''')
        except Exception as e:
            print(f"[WARN] geo_research_articles 条件 FK: {e}")

        # 幂等 marker 登记
        try:
            cursor.execute("SELECT 1 FROM _migration_markers WHERE marker = %s",
                           ('research_monitor_v1_initial',))
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO _migration_markers (marker, note) VALUES (%s, %s)",
                    ('research_monitor_v1_initial',
                     'GEO 调研监测 9 张表初版 (industries/prompts/round/round_call/articles/citations/review_log/cost_log/config)')
                )
        except Exception:
            pass
        # ==================== GEO 调研监测 (Plan A) END ====================

        # ==================== [R4 ② 2026-08-20] organization 自愈补列 ====================
        # 🔴 这一段原本在文件第 214 行附近,**排在绝大多数 CREATE TABLE 之前**。
        #   它要补的 10 张表里有 9 张那时还不存在,而当年的 _safe_add_column 把
        #   UndefinedTable 静默吞掉(注释写「并发竞态」)⇒ 63 个列一个都没补上,
        #   而且零日志。实证:空库跑完 init_db 后 quotes.organization_id 列数 = 0。
        #   连带代价是两轮排查(R2 夹具缺列 / R3 单线程自死锁)才追到底。
        #   ⇒ 现在整体挪到**所有 CREATE TABLE 之后**执行,这才是它真正会生效的位置。
        #   判据:tests/db_bootstrap_r4/ —— 空库单次 init_db 后逐列断言,非抽样。
        # [Organization internal seats 2026-07-20] Startup compatibility only.
        # The canonical constraints and indexes live in the dedicated migration;
        # these nullable columns keep mixed-version startup from selecting a
        # column that has not been added yet.  Existing single-user rows remain
        # organization_id=NULL and therefore retain their legacy semantics.
        _organization_artifact_tables = (
            "client_profiles", "client_materials", "diagnosis_records", "quotes",
            "keyword_selection_sessions", "article_generations", "articles",
            "media_publications", "monitoring_tasks", "monitoring_reports",
        )
        _organization_artifact_columns = (
            ("brand_id", "INTEGER"),
            ("organization_id", "BIGINT"),
            ("created_by_user_id", "INTEGER"),
            ("created_by_membership_id", "BIGINT"),
            ("created_by_actor_kind", "TEXT"),
            ("responsible_user_id", "INTEGER"),
            ("artifact_visibility", "TEXT"),
        )
        for _table in _organization_artifact_tables:
            for _column, _column_type in _organization_artifact_columns:
                _safe_add_column_optional(cursor, _table, _column, _column_type)
        for _column, _column_type in (
            ("organization_id", "BIGINT"),
            ("actor_user_id", "INTEGER"),
            ("actor_membership_id", "BIGINT"),
            ("actor_kind", "TEXT"),
            ("payer_user_id", "INTEGER"),
            ("approval_request_id", "BIGINT"),
        ):
            _safe_add_column_optional(cursor, "publish_orders", _column, _column_type)
        if _SAFE_ADD_SKIPPED_TABLES:
            # 🔴 跳过必须留痕:当年就是因为「跳过=静默」才让 63 个列消失了两个月。
            print(f"  [DB] 自愈补列跳过(表由别的模块/迁移拥有,尚未建): "
                  f"{sorted(_SAFE_ADD_SKIPPED_TABLES)}")
        if _INDEX_SKIPPED:
            # 🔴 同上:跳过必须留痕。全新空库上这里应当**一行都不打印**
            #   (判据 tests/db_bootstrap_r5/test_index_guard.py 钉死这一点)。
            print(f"  [DB] 建索引跳过(目标表/列不存在,通常是既存的窄表): "
                  f"{sorted(set((t, tuple(c)) for t, c in _INDEX_SKIPPED))}")
        # ==================== organization 自愈补列 END ====================

        conn.close()
        try:
            print(f"  [DB] Database initialized: {DB_PATH}")
        except UnicodeEncodeError:
            pass
    finally:
        try:
            conn.close()
        except Exception: pass


def _coerce_brand_presence_for_schema(cursor, present: object):
    """Bind the legacy BOOLEAN and canonical SMALLINT schemas without guessing."""
    cursor.execute(
        """
        SELECT format_type(a.atttypid, a.atttypmod) AS data_type
        FROM pg_attribute a
        WHERE a.attrelid = to_regclass('diagnosis_records')
          AND a.attname = 'has_brand_presence'
          AND a.attnum > 0
          AND NOT a.attisdropped
        """
    )
    type_row = cursor.fetchone()
    data_type = type_row.get("data_type") if type_row else None
    if data_type == "boolean":
        return bool(present)
    if data_type == "smallint":
        return 1 if present else 0
    raise RuntimeError(
        "diagnosis_records.has_brand_presence schema mismatch: "
        f"{data_type or 'missing'}"
    )


def _coerce_data_anomaly_for_schema(cursor, anomalous: object):
    """Bind data_anomaly for legacy BOOLEAN and canonical SMALLINT schemas."""
    cursor.execute(
        """
        SELECT format_type(a.atttypid, a.atttypmod) AS data_type
        FROM pg_attribute a
        WHERE a.attrelid = to_regclass('diagnosis_records')
          AND a.attname = 'data_anomaly'
          AND a.attnum > 0
          AND NOT a.attisdropped
        """
    )
    type_row = cursor.fetchone()
    data_type = type_row.get("data_type") if type_row else None
    if data_type == "boolean":
        return bool(anomalous)
    if data_type == "smallint":
        return 1 if anomalous else 0
    raise RuntimeError(
        "diagnosis_records.data_anomaly schema mismatch: "
        f"{data_type or 'missing'}"
    )


def save_diagnosis(results: dict, *, organization_identity=None) -> int:
    """
    保存诊断结果到数据库
    
    Args:
        results: diagnosis_workflow返回的完整结果
        
    Returns:
        插入记录的ID
    """
    init_db()  # 确保表存在
    
    conn = get_connection()
    try:
        cursor = conn.cursor()

        brand_profile_edit_allowed = organization_identity is None
        if organization_identity is not None:
            from services.organization_service import _lock_identity
            organization_identity = _lock_identity(cursor, organization_identity)
            organization_identity.require_active_organization()
            if organization_identity.is_member:
                organization_identity.require("diagnosis.run")
            brand_profile_edit_allowed = (
                organization_identity.is_owner
                or "clients.profile_edit" in organization_identity.capabilities
            )
    
        # 提取基础信息
        brand_name = results.get("brand", "")
        industry = results.get("industry", "")
        # [WO_267] 品牌名与本次诊断关键词当行业判定上下文(光伏客户的 industry 列写的是建筑)
        industry_category = get_industry_category(
            industry, brand={"name": brand_name, "seed_keywords": results.get("keywords") or []})
        session_id = results.get("session_id", "")
        keywords = json.dumps(results.get("keywords", []), ensure_ascii=False)
    
        # 提取GEO评分 - 兼容多种数据格式
        geo_score = results.get("geo_score", {})
        scores_data = results.get("scores", {})  # 另一种格式
    
        # 优先从geo_score获取，否则从scores获取
        total_score = geo_score.get("total_score") or scores_data.get("total_score", 0)
        level = geo_score.get("level") or scores_data.get("level", get_level_from_score(total_score))
    
        # 维度分数 - 兼容多种嵌套格式
        scores = geo_score.get("dimension_scores", {}) or geo_score.get("scores", {}) or scores_data.get("dimension_scores", {}) or scores_data
    
        # 提取AI数据
        data = results.get("data", {})
        ai_data = data.get("ai_visibility", {})
        ai_total_tests = ai_data.get("total_tests", 0)
        ai_detected_count = ai_data.get("detected_count", 0)
        ai_mention_rate = ai_data.get("overall_mention_rate", 0)
        ai_engines = json.dumps(ai_data.get("engines_tested", []), ensure_ascii=False)
    
        # 提取社媒数据
        douyin_data = data.get("douyin", {})
        xhs_data = data.get("xiaohongshu", {})
        douyin_videos = douyin_data.get("top20", [])
        xhs_notes = xhs_data.get("top20", [])
    
        brand_id = data.get("brand_identification", {})
    
        # 提取网页数据
        web_data = data.get("web_search", {})
    
        # 提取竞品数据
        comp_data = data.get("competitor_analysis", {})
    
        # 品牌知名度 - 兼容多种格式
        brand_recognition = geo_score.get("brand_recognition", {}) or scores_data.get("brand_recognition", {})
    
        # 报告路径
        report_md = results.get("report_file", "")
        report_json = results.get("json_file", "")
    
        # [NEW] 诊断类型 (sales_lite / technical_full)
        diagnosis_type = data.get("diagnosis_type", "technical_full")

        # [CTO-15.23 2026-05-21] 自定义诊断问题落库(双轨独立分)
        # custom_questions 在 input_params 里(workflow 写入)· 兜底从 ai_data.custom_visibility.questions 取
        input_params = results.get("input_params") or {}
        custom_q_raw = input_params.get("custom_questions") or (
            (ai_data.get("custom_visibility") or {}).get("questions")
            if isinstance(ai_data.get("custom_visibility"), dict) else None
        ) or []
        custom_questions_json = json.dumps(custom_q_raw, ensure_ascii=False) if custom_q_raw else None
        # total_questions_tested = 系统题数 + 自定义题数(默认 8 系统 · 无 custom 时仍 8)
        total_q_tested = len(ai_data.get("test_questions") or []) + len(custom_q_raw)
        if total_q_tested == 0:
            total_q_tested = 8  # 兜底:无 ai_data 时按套餐默认

        # 构建SQL
        sql = """
        INSERT INTO diagnosis_records (
            session_id, brand_name, industry, industry_category, keywords,
            total_score, level,
            web_search_score, platform_score, content_quality_score,
            authority_score, brand_ownership_score, ai_visibility_score,
            ai_citation_score, update_frequency_score,
            ai_total_tests, ai_detected_count, ai_mention_rate, ai_engines_tested,
            douyin_video_count, douyin_brand_count, xhs_note_count, xhs_brand_count,
            web_result_count, web_brand_direct_count, web_authority_count, scholar_count,
            has_brand_presence, brand_account_count,
            competitor_count,
            brand_recognition_level, data_anomaly, anomaly_reason,
            report_md_path, report_json_path,
            raw_data_json,
            diagnosis_type,
            custom_questions, total_questions_tested
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (session_id) DO UPDATE SET
            brand_name = EXCLUDED.brand_name,
            industry = EXCLUDED.industry,
            industry_category = EXCLUDED.industry_category,
            keywords = EXCLUDED.keywords,
            total_score = EXCLUDED.total_score,
            level = EXCLUDED.level,
            web_search_score = EXCLUDED.web_search_score,
            platform_score = EXCLUDED.platform_score,
            content_quality_score = EXCLUDED.content_quality_score,
            authority_score = EXCLUDED.authority_score,
            brand_ownership_score = EXCLUDED.brand_ownership_score,
            ai_visibility_score = EXCLUDED.ai_visibility_score,
            ai_citation_score = EXCLUDED.ai_citation_score,
            update_frequency_score = EXCLUDED.update_frequency_score,
            ai_total_tests = EXCLUDED.ai_total_tests,
            ai_detected_count = EXCLUDED.ai_detected_count,
            ai_mention_rate = EXCLUDED.ai_mention_rate,
            ai_engines_tested = EXCLUDED.ai_engines_tested,
            douyin_video_count = EXCLUDED.douyin_video_count,
            douyin_brand_count = EXCLUDED.douyin_brand_count,
            xhs_note_count = EXCLUDED.xhs_note_count,
            xhs_brand_count = EXCLUDED.xhs_brand_count,
            web_result_count = EXCLUDED.web_result_count,
            web_brand_direct_count = EXCLUDED.web_brand_direct_count,
            web_authority_count = EXCLUDED.web_authority_count,
            scholar_count = EXCLUDED.scholar_count,
            has_brand_presence = EXCLUDED.has_brand_presence,
            brand_account_count = EXCLUDED.brand_account_count,
            competitor_count = EXCLUDED.competitor_count,
            brand_recognition_level = EXCLUDED.brand_recognition_level,
            data_anomaly = EXCLUDED.data_anomaly,
            anomaly_reason = EXCLUDED.anomaly_reason,
            report_md_path = EXCLUDED.report_md_path,
            report_json_path = EXCLUDED.report_json_path,
            raw_data_json = EXCLUDED.raw_data_json,
            diagnosis_type = EXCLUDED.diagnosis_type,
            custom_questions = EXCLUDED.custom_questions,
            total_questions_tested = EXCLUDED.total_questions_tested
        RETURNING id
        """
    
        _brand_presence_db_value = _coerce_brand_presence_for_schema(
            cursor,
            brand_id.get("has_brand_presence", False),
        )
        _data_anomaly_db_value = _coerce_data_anomaly_for_schema(
            cursor,
            geo_score.get("data_anomaly", False),
        )

        values = (
            session_id, brand_name, industry, industry_category, keywords,
            total_score, level,
            scores.get("web_search_score", 0),
            scores.get("platform_score", 0),
            scores.get("content_quality_score", 0),
            scores.get("authority_score", 0),
            scores.get("brand_ownership_score", 0),
            scores.get("ai_visibility_score", 0),
            scores.get("ai_citation_score", 0),
            scores.get("update_frequency_score", 0),
            ai_total_tests,
            ai_detected_count,
            ai_mention_rate,
            ai_engines,
            len(douyin_videos),
            brand_id.get("douyin_content_count", 0),
            len(xhs_notes),
            brand_id.get("xiaohongshu_content_count", 0),
            web_data.get("result_count", 0),
            web_data.get("brand_direct_count", 0),
            len(web_data.get("authority_sources", [])),
            data.get("scholar_search", {}).get("result_count", 0),
            _brand_presence_db_value,
            brand_id.get("account_count", 0),
            comp_data.get("total_identified", 0),
            brand_recognition.get("level", ""),
            _data_anomaly_db_value,
            geo_score.get("anomaly_reason", ""),
            report_md,
            report_json,
            json.dumps(results, ensure_ascii=False, default=str),
            diagnosis_type,
            custom_questions_json,
            total_q_tested,
        )

        cursor.execute(sql, values)
        record_id = cursor.fetchone()["id"]

        requested_brand_id = results.get("brand_id")
        if organization_identity is not None:
            if requested_brand_id is None or int(requested_brand_id) <= 0:
                raise ValueError("organization diagnosis requires a trusted brand_id")
            cursor.execute(
                """
                SELECT id FROM brands
                WHERE id=%s AND owner_user_id=%s
                  AND COALESCE(is_deleted,FALSE)=FALSE
                FOR UPDATE
                """,
                (int(requested_brand_id), organization_identity.principal_user_id),
            )
            brand_row = cursor.fetchone()
            if not brand_row:
                from services.organization_contract import OrganizationError
                raise OrganizationError(
                    "ORG_BRAND_INACTIVE",
                    "诊断客户已删除、已转移或不属于当前老板",
                    http_status=409,
                )
            brand_db_id = int(brand_row["id"])
            cursor.execute(
                "UPDATE diagnosis_records SET brand_id = %s WHERE id = %s",
                (brand_db_id, record_id),
            )
            from services.organization_artifacts import stamp_artifact
            stamp_artifact(
                cursor,
                organization_identity,
                artifact_type="diagnosis",
                artifact_id=record_id,
                brand_id=brand_db_id,
                visibility="private",
            )
        else:
            # Preserve the legacy name/owner lookup when no organization
            # context exists.  Organization requests never enter this fallback.
            creator_uid = results.get("creator_user_id")
            brand_db_id = get_or_create_brand(brand_name, industry, industry_category, owner_user_id=creator_uid)
            cursor.execute("UPDATE diagnosis_records SET brand_id = %s WHERE id = %s", (brand_db_id, record_id))

        # CTO-15.23 2026-05-25 · 老板拍 C 全干 · 诊断 industry/industry_category 回写 brands(if 空)
        # · 既有 brand · industry 字段 NULL/'' 时用诊断推断值填补 · 不覆盖用户已设值
        # · get_or_create_brand 只在创建时传 industry · 已存在 brand 不更新 → Agent B P0-2 真实
        # · memory feedback_industry_l1l2_reuse_boost · 不让资料浪费
        if brand_db_id and industry and brand_profile_edit_allowed:
            try:
                cursor.execute(
                    """UPDATE brands
                       SET industry = %s, industry_category = COALESCE(NULLIF(industry_category, ''), %s)
                       WHERE id = %s AND (industry IS NULL OR industry = '')""",
                    (industry, industry_category, brand_db_id),
                )
            except Exception as _e:
                print(f"  [WARN] brand industry 回写失败(非阻塞): {_e}")

        # Keep the diagnosis row, actor stamp and brand summary in one commit.
        if brand_db_id:
            # [WO_BRAND_LATEST_CROSS_TENANT_WRITE 2026-08-08] 原写法是
            #   `UPDATE brands SET ... WHERE id=%s`,(total_score, record_id, brand_db_id)
            #   三个自由入参直接配对、**零校验** —— record_id 与 brand_db_id 不配对时当场串台,
            #   无任何报错。生产实测:42 个品牌的 latest_diagnosis_id 指向别人家的诊断。
            #   → 拆成两条:计数语义保持原样(挡下也照样算跑过一次),
            #     latest_* 走 SSOT 单点(归属 + 最新 + published-only 三重约束都在 SQL 里)。
            cursor.execute(
                """
                UPDATE brands SET
                    diagnosis_count=COALESCE(diagnosis_count,0)+1,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=%s
                """,
                (brand_db_id,),
            )
            _synced = sync_brand_latest(
                cursor, diagnosis_id=record_id, score=total_score, brand_id=brand_db_id
            )
            if _synced == 0:
                # 0 行 = 被守卫挡下,**正常防御结果**。正常保存路径不会走到这里:
                # 新插入的记录 created_at 取 DEFAULT CURRENT_TIMESTAMP、result_visibility 为 NULL
                # (语义=published),天然就是该品牌最新那份。
                print(
                    f"  [GUARD] brands.latest_* 未同步(守卫挡下):"
                    f"record_id={record_id} brand_db_id={brand_db_id} —— 二者不配对,或该诊断不是"
                    f"该品牌 published-only 最新那份"
                )
        conn.commit()
    
        print(f"  💾 诊断记录已保存: ID={record_id}, 品牌={brand_name}(brand_id={brand_db_id}), 评分={total_score}")
        return record_id
    finally:
        try:
            conn.close()
        except Exception: pass


def get_diagnosis_by_id(record_id: int) -> Optional[dict]:
    """按ID获取诊断记录"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM diagnosis_records WHERE id = %s", (record_id,))
        row = cursor.fetchone()
        conn.close()
    
        if row:
            result = dict(row)
            # 解析JSON字段
            if result.get("raw_data_json"):
                try:
                    raw_data = json.loads(result["raw_data_json"])
                    result["data"] = raw_data.get("data", {})
                    result["scores"] = raw_data.get("scores", {})
                    result["report"] = raw_data.get("report", "")  # [FIX] 添加report字段
                except:
                    result["data"] = {}
                    result["scores"] = {}
                    result["report"] = ""
            return result
        return None
    finally:
        try:
            conn.close()
        except Exception: pass


def update_diagnosis_report(record_id: int, report: str, report_style: str, report_version: str = None) -> bool:
    """
    更新诊断记录的报告内容
    
    Args:
        record_id: 诊断记录ID
        report: 新的报告内容
        report_style: 报告风格 ("sales" / "technical")
        report_version: 报告版本号
        
    Returns:
        是否更新成功
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        # 更新raw_data_json中的report字段
        cursor.execute("SELECT raw_data_json FROM diagnosis_records WHERE id = %s", (record_id,))
        row = cursor.fetchone()
        
        if row and row["raw_data_json"]:
            raw_data = json.loads(row["raw_data_json"])
            raw_data["report"] = report
            raw_data["report_style"] = report_style
            if report_version:
                raw_data["report_version"] = report_version
            
            cursor.execute("""
                UPDATE diagnosis_records
                SET raw_data_json = %s
                WHERE id = %s
            """, (json.dumps(raw_data, ensure_ascii=False, default=str), record_id))
            
            conn.commit()
            print(f"  💾 报告已更新: ID={record_id}, style={report_style}")
            return True
        
        return False
        
    except Exception as e:
        print(f"  [ERROR] 更新报告失败: {e}")
        return False
    finally:
        conn.close()


def get_diagnosis_by_session(session_id: str) -> Optional[dict]:
    """按session_id获取诊断记录"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM diagnosis_records WHERE session_id = %s", (session_id,))
        row = cursor.fetchone()
        conn.close()
    
        if row:
            return dict(row)
        return None
    finally:
        try:
            conn.close()
        except Exception: pass


def list_diagnoses(
    brand_name: str = None,
    brand_id: int = None,
    industry: str = None,
    industry_category: str = None,
    level: str = None,
    min_score: int = None,
    max_score: int = None,
    days: int = None,
    start_date: str = None,
    end_date: str = None,
    limit: int = 50,
    offset: int = 0,
    order_by: str = "created_at DESC"
) -> List[dict]:
    """
    列出诊断记录（支持多条件筛选）
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        conditions = []
        params = []

        if brand_name:
            conditions.append("brand_name LIKE %s")
            params.append(f"%{brand_name}%")

        if brand_id is not None:
            conditions.append("brand_id = %s")
            params.append(brand_id)

        if industry:
            conditions.append("industry LIKE %s")
            params.append(f"%{industry}%")

        if industry_category:
            # [WO_267] 同时匹配新大类 key 与映射到它的全部存量旧值(存量不回填)
            conditions.append("industry_category = ANY(%s)")
            params.append(filter_values(industry_category))

        if level:
            conditions.append("level = %s")
            params.append(level)

        if min_score is not None:
            conditions.append("total_score >= %s")
            params.append(min_score)

        if max_score is not None:
            conditions.append("total_score <= %s")
            params.append(max_score)

        if days:
            cutoff = datetime.now() - timedelta(days=days)
            conditions.append("created_at >= %s")
            params.append(cutoff.isoformat())

        if start_date:
            conditions.append("created_at >= %s")
            params.append(start_date)

        if end_date:
            conditions.append("created_at <= %s")
            params.append(end_date + " 23:59:59")

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        # [返工2 P1-2] /api/history + /api/diagnoses/search 经 list_diagnoses → 统一 published-only:
        #   排除 withheld(退款/失败)**与 pending(生成中占位)** —— pending 占位无分数不应现于代理侧列表。
        #   NULL=旧数据向后兼容(视为 published)。admin 审计需含全部另走显式入口(不复用本函数)。
        sql = f"""
        SELECT id, session_id, brand_name, industry, industry_category,
               total_score, level, created_at, report_md_path, keywords, diagnosis_type, brand_id
        FROM diagnosis_records
        WHERE {where_clause}
          AND (result_visibility IS NULL OR result_visibility = 'published')
        ORDER BY {order_by}
        LIMIT %s OFFSET %s
        """
    
        params.extend([limit, offset])
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
    
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def count_diagnoses(
    brand_name: str = None,
    brand_id: int = None,
    industry: str = None,
    level: str = None,
    days: int = None,
) -> int:
    """诊断记录计数（与 list_diagnoses 同条件，用于分页 total）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        conditions = []
        params = []
        if brand_name:
            conditions.append("brand_name LIKE %s")
            params.append(f"%{brand_name}%")
        if brand_id is not None:
            conditions.append("brand_id = %s")
            params.append(brand_id)
        if industry:
            conditions.append("industry LIKE %s")
            params.append(f"%{industry}%")
        if level:
            conditions.append("level = %s")
            params.append(level)
        if days:
            from datetime import timedelta
            cutoff = datetime.now() - timedelta(days=days)
            conditions.append("created_at >= %s")
            params.append(cutoff.isoformat())
        where_clause = " AND ".join(conditions) if conditions else "1=1"
        # [返工2 P1-2] 计数与 list_diagnoses 同口径 published-only(排 withheld+pending · 分页 total 一致)
        cursor.execute(
            f"SELECT COUNT(*) as cnt FROM diagnosis_records WHERE {where_clause} "
            "AND (result_visibility IS NULL OR result_visibility = 'published')", params)
        result = cursor.fetchone()
        return result["cnt"] if result else 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_statistics(days: int = None) -> dict:
    """
    获取统计信息
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        # [返工2 P1-2] 统计 published-only(排 withheld 退款/失败 + pending 生成中 · 否则污染 total/avg/分布)。
        #   admin 审计需含全部另走显式入口(不复用本函数)。
        _vis_pred = "(result_visibility IS NULL OR result_visibility = 'published')"
        params = []
        if days:
            cutoff = datetime.now() - timedelta(days=days)
            date_filter = f"WHERE {_vis_pred} AND created_at >= %s"
            params.append(cutoff.isoformat())
        else:
            date_filter = f"WHERE {_vis_pred}"

        # 总数和平均分
        cursor.execute(f"""
            SELECT COUNT(*) as total, AVG(total_score) as avg_score
            FROM diagnosis_records {date_filter}
        """, params)
        row = cursor.fetchone()
        total = row["total"]
        avg_score = row["avg_score"] or 0

        # 等级分布
        cursor.execute(f"""
            SELECT level, COUNT(*) as count
            FROM diagnosis_records {date_filter}
            GROUP BY level
        """, params)
        level_dist = {row["level"]: row["count"] for row in cursor.fetchall()}

        # 行业分布
        cursor.execute(f"""
            SELECT industry_category, COUNT(*) as count
            FROM diagnosis_records {date_filter}
            GROUP BY industry_category
            ORDER BY count DESC
        """, params)
        # [WO_267] 存量旧值与新 key 混在一列里 ⇒ 按大类中文名合并计数(认不出的原样保留)
        industry_dist = {}
        for row in cursor.fetchall():
            _name = display_name(row["industry_category"]) or row["industry_category"]
            industry_dist[_name] = industry_dist.get(_name, 0) + row["count"]
    
        conn.close()
    
        return {
            "total_records": total,
            "average_score": round(avg_score, 1),
            "level_distribution": level_dist,
            "industry_distribution": industry_dist
        }
    finally:
        try:
            conn.close()
        except Exception: pass


def search_diagnoses(keyword: str, limit: int = 20) -> List[dict]:
    """
    全文搜索（按品牌名和行业）
    """
    return list_diagnoses(brand_name=keyword, limit=limit) + \
           list_diagnoses(industry=keyword, limit=limit)


# ========== 文章生成记录相关函数 ==========

def save_article_generation(
    diagnosis_id: int,
    task_id: str,
    article_type: str,
    title: str,
    word_count: int,
    file_path: str,
    status: str = "success"
) -> int:
    """保存文章生成记录"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            INSERT INTO article_generations
            (diagnosis_id, task_id, article_type, title, word_count, file_path, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (diagnosis_id, task_id, article_type, title, word_count, file_path, status))

        record_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return record_id
    finally:
        try:
            conn.close()
        except Exception: pass


def get_articles_by_task(task_id: str) -> List[dict]:
    """按任务ID获取文章列表"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM article_generations
            WHERE task_id = %s
            ORDER BY created_at
        """, (task_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def get_articles_by_diagnosis(diagnosis_id: int) -> List[dict]:
    """按诊断ID获取文章列表"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM article_generations
            WHERE diagnosis_id = %s
            ORDER BY created_at DESC
        """, (diagnosis_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def get_diagnosis_id_by_article_path(path: str) -> Optional[int]:
    """[GEO 文章文件接口归属 · v3 canonical 反查] 按【规范化真实路径】反查所属 diagnosis_id。

    🔴 Deploy-CTO NO-GO finding 2:归属必须基于【实际要读/删的规范化路径】,不能基于调用者原始字符串。
    否则 `diagnosis_本人/../diagnosis_他人/secret.md` 会被"本人"字样骗过归属、realpath 却读到"他人"。

    实现:realpath 规范化输入 → 取 basename 窄化候选 → 对每个候选行的 file_path 也 realpath,
    仅当【规范化后严格相等】才认定归属(防 ../ 穿越、符号链接、跨租户 basename 碰撞)。
    找不到归属行 → None(调用方 fail-closed:无归属线索一律拒绝)。
    """
    if not path:
        return None
    try:
        target = os.path.realpath(path)
    except (ValueError, OSError):
        return None
    base = os.path.basename(target)
    if not base:
        return None
    conn = get_connection()
    try:
        cursor = conn.cursor()
        # basename 仅用于窄化候选;真正的归属判定是下面的 realpath 严格相等(安全闸)
        cursor.execute(
            "SELECT diagnosis_id, file_path FROM article_generations "
            "WHERE file_path LIKE %s AND diagnosis_id IS NOT NULL "
            "ORDER BY created_at DESC",
            ("%" + base,))
        rows = cursor.fetchall()
        conn.close()
        for row in rows:
            try:
                if os.path.realpath(row["file_path"]) == target:
                    return row["diagnosis_id"]
            except (ValueError, OSError):
                continue
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_article_statistics(days: int = None) -> dict:
    """获取文章生成统计"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        date_filter = ""
        params = []
        if days:
            cutoff = datetime.now() - timedelta(days=days)
            date_filter = "WHERE created_at >= %s"
            params.append(cutoff.isoformat())

        # 总数
        cursor.execute(f"""
            SELECT COUNT(*) as total, SUM(word_count) as total_words
            FROM article_generations {date_filter}
        """, params)
        row = cursor.fetchone()
        total = row["total"] or 0
        total_words = row["total_words"] or 0
    
        # 按类型分布
        cursor.execute(f"""
            SELECT article_type, COUNT(*) as count
            FROM article_generations {date_filter}
            GROUP BY article_type
        """, params)
        type_dist = {row["article_type"]: row["count"] for row in cursor.fetchall()}
    
        # 成功率
        cursor.execute(f"""
            SELECT status, COUNT(*) as count
            FROM article_generations {date_filter}
            GROUP BY status
        """, params)
        status_dist = {row["status"]: row["count"] for row in cursor.fetchall()}
        success_count = status_dist.get("success", 0)
        success_rate = (success_count / total * 100) if total > 0 else 0
    
        conn.close()
    
        return {
            "total_articles": total,
            "total_words": total_words,
            "type_distribution": type_dist,
            "success_rate": round(success_rate, 1)
        }
    finally:
        try:
            conn.close()
        except Exception: pass


# ========== Token使用记录相关函数 ==========

def calculate_token_cost(model_name: str, input_tokens: int, output_tokens: int) -> float:
    """计算Token费用 · legacy token_usage 也必须走 llm_call_tracker SSOT。"""
    from tools.llm_call_tracker import estimate_cost_for_model

    return round(estimate_cost_for_model(model_name, input_tokens, output_tokens), 4)


def save_token_usage(
    task_id: str,
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    operation_type: str = "article",
    diagnosis_id: int = None
) -> int:
    """保存Token使用记录"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        total_tokens = input_tokens + output_tokens
        estimated_cost = calculate_token_cost(model_name, input_tokens, output_tokens)
    
        cursor.execute("""
            INSERT INTO token_usage
            (task_id, diagnosis_id, model_name, input_tokens, output_tokens,
             total_tokens, estimated_cost, operation_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (task_id, diagnosis_id, model_name, input_tokens, output_tokens,
              total_tokens, estimated_cost, operation_type))

        record_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return record_id
    finally:
        try:
            conn.close()
        except Exception: pass


def get_token_usage_by_task(task_id: str) -> dict:
    """获取任务的Token使用汇总"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            SELECT 
                SUM(input_tokens) as total_input,
                SUM(output_tokens) as total_output,
                SUM(total_tokens) as total_tokens,
                SUM(estimated_cost) as total_cost,
                COUNT(*) as call_count
            FROM token_usage
            WHERE task_id = %s
        """, (task_id,))
    
        row = cursor.fetchone()
        conn.close()
    
        return {
            "input_tokens": row["total_input"] or 0,
            "output_tokens": row["total_output"] or 0,
            "total_tokens": row["total_tokens"] or 0,
            "estimated_cost": round(row["total_cost"] or 0, 2),
            "call_count": row["call_count"] or 0
        }
    finally:
        try:
            conn.close()
        except Exception: pass


def get_token_statistics(days: int = None) -> dict:
    """获取 LLM Token 使用统计 · UNION token_usage(老) + llm_call_log(新主源)

    [CTO-15.23 2026-05-17 老板报"发布管理成本没计入总成本"]
      根因:Dashboard /api/statistics 只读 token_usage(老 SDK · 30 天仅 87 行 ¥3.29) ·
      Codex a3b30c21+e4197e04 把 36 种 caller(article_writing/monitoring/placement_content_fix
      等)全埋点写 llm_call_log(4262 行 ¥128.82) · 总成本严重低估 → 发布管理 article_writing ¥1.29
      被排除在外
    修法:UNION ALL 两表 · 老 diagnosis_report data 保留 + 新 llm_call_log 全计入
      dry-run 验证 30 days:¥3.29 → ¥132.11 · 4349 calls · 含 article_writing/monitoring 等

    SQL 4 维度核验:
      - 列名 token_usage: model_name/operation_type · llm_call_log: model/caller
      - data_type estimated_cost 两表分别 real/numeric · UNION 自动 align numeric
      - 字段归属 两表独立 · 无 overlap(token_usage 仅老 diagnosis_report/batch_article_generation)
      - dry-run SSH 已跑通(BEGIN+ROLLBACK 等价 · SELECT 无副作用)
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        date_filter = ""
        params = []
        if days:
            cutoff = datetime.now() - timedelta(days=days)
            date_filter = "WHERE created_at >= %s"
            params.append(cutoff.isoformat())

        # 复用 CTE · 两表 UNION 抽取统一 schema · params 给两次
        cte = f"""
            WITH unified AS (
                SELECT input_tokens, output_tokens,
                       estimated_cost::numeric AS estimated_cost,
                       model_name AS model, operation_type, total_tokens
                FROM token_usage {date_filter}
                UNION ALL
                SELECT input_tokens, output_tokens, estimated_cost,
                       model, caller AS operation_type,
                       (COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0))::int AS total_tokens
                FROM llm_call_log {date_filter}
            )
        """
        union_params = params + params  # 两次 WHERE created_at >= %s

        # 总量
        cursor.execute(cte + """
            SELECT SUM(input_tokens) as total_input,
                   SUM(output_tokens) as total_output,
                   SUM(estimated_cost) as total_cost,
                   COUNT(*) as call_count
            FROM unified
        """, union_params)
        row = cursor.fetchone()

        # 按模型分布
        cursor.execute(cte + """
            SELECT model AS model_name,
                   SUM(total_tokens) as tokens,
                   SUM(estimated_cost) as cost
            FROM unified
            WHERE model IS NOT NULL
            GROUP BY model
        """, union_params)
        model_dist = {r["model_name"]: {
            "tokens": int(r["tokens"] or 0),
            "cost": round(float(r["cost"] or 0), 2)
        } for r in cursor.fetchall()}

        # 按操作类型分布
        cursor.execute(cte + """
            SELECT operation_type,
                   SUM(total_tokens) as tokens,
                   SUM(estimated_cost) as cost
            FROM unified
            WHERE operation_type IS NOT NULL
            GROUP BY operation_type
        """, union_params)
        operation_dist = {r["operation_type"]: {
            "tokens": int(r["tokens"] or 0),
            "cost": round(float(r["cost"] or 0), 2)
        } for r in cursor.fetchall()}

        conn.close()

        return {
            "total_input_tokens": int(row["total_input"] or 0),
            "total_output_tokens": int(row["total_output"] or 0),
            "total_cost": round(float(row["total_cost"] or 0), 2),
            "total_calls": int(row["call_count"] or 0),
            "model_distribution": model_dist,
            "operation_distribution": operation_dist
        }
    finally:
        try:
            conn.close()
        except Exception: pass


# ========== 品牌管理相关函数 ==========

def get_or_create_brand(brand_name: str, industry: str = None, industry_category: str = None, owner_user_id: int = None) -> int:
    """
    获取或创建品牌记录
    如果品牌已存在，返回其ID；否则创建新品牌并返回ID
    新品牌会自动生成唯一编号（如BRD-0001）
    使用 INSERT ... ON CONFLICT DO NOTHING + SELECT 避免 TOCTOU 竞态条件
    A.2 CTO-15.18 · 2026-04-28 · is_test auto-detect 接入(根因 #3 测试客户隔离)

    [P0-1 · 2026-07-26] 品牌名先过 hygiene 校验再入库。这是诊断链的兜底建档口，
    生产实证 brands.id=278 那种 "深圳驰鲸科技\n\n城市:深圳" 一旦从这里进库，
    品牌识别恒不命中 → 诊断 456/468 都是 0 分、客户付费两次拿废报告。
    抛 ValueError（BrandNameHygieneError 是其子类），由调用方转成带出口的 4xx。
    """
    from utils.brand_name_hygiene import validate_brand_name

    brand_name = validate_brand_name(brand_name)

    # A.2 名字含"测试|test|_demo|_test|验收"自动 is_test=true
    try:
        from utils.is_test_brand import detect_is_test_for_new_brand
        _is_test = detect_is_test_for_new_brand(brand_name)
    except Exception:
        _is_test = False

    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 原子操作：按 (name, owner_user_id) 去重 — 不同用户可以有同名品牌
        if owner_user_id:
            # 有 owner：按 (name + owner) 去重
            cursor.execute("""
                INSERT INTO brands (name, industry, industry_category, owner_user_id, brand_type, is_test)
                VALUES (%s, %s, %s, %s, 'client', %s)
                ON CONFLICT DO NOTHING
            """, (brand_name, industry, industry_category, owner_user_id, _is_test))

            if cursor.rowcount > 0:
                cursor.execute("SELECT id FROM brands WHERE name = %s AND owner_user_id = %s", (brand_name, owner_user_id))
                brand_id = cursor.fetchone()["id"]
                brand_code = f"BRD-{brand_id:04d}"
                cursor.execute("UPDATE brands SET brand_code = %s WHERE id = %s", (brand_code, brand_id))
                conn.commit()
            else:
                # 该用户已有同名品牌，复用
                cursor.execute("SELECT id FROM brands WHERE name = %s AND owner_user_id = %s AND (is_deleted = false OR is_deleted IS NULL)", (brand_name, owner_user_id))
                row = cursor.fetchone()
                if row:
                    brand_id = row["id"]
                else:
                    # 被软删除了，重新插入
                    cursor.execute("""
                        INSERT INTO brands (name, industry, industry_category, owner_user_id, brand_type, is_test)
                        VALUES (%s, %s, %s, %s, 'client', %s) RETURNING id
                    """, (brand_name, industry, industry_category, owner_user_id, _is_test))
                    brand_id = cursor.fetchone()["id"]
                    brand_code = f"BRD-{brand_id:04d}"
                    cursor.execute("UPDATE brands SET brand_code = %s WHERE id = %s", (brand_code, brand_id))
                    conn.commit()
        else:
            # 无 owner（兼容旧逻辑）：按 name 查找
            cursor.execute("SELECT id FROM brands WHERE name = %s AND (is_deleted = false OR is_deleted IS NULL) LIMIT 1", (brand_name,))
            row = cursor.fetchone()
            if row:
                brand_id = row["id"]
            else:
                cursor.execute("""
                    INSERT INTO brands (name, industry, industry_category, brand_type, is_test) VALUES (%s, %s, %s, 'client', %s) RETURNING id
                """, (brand_name, industry, industry_category, _is_test))
                brand_id = cursor.fetchone()["id"]
                brand_code = f"BRD-{brand_id:04d}"
                cursor.execute("UPDATE brands SET brand_code = %s WHERE id = %s", (brand_code, brand_id))
                conn.commit()

        conn.close()
        return brand_id
    finally:
        try:
            conn.close()
        except Exception: pass


def update_brand_stats(brand_id: int, diagnosis_id: int, score: int):
    """诊断完成后更新品牌统计信息。

    🔴 [2026-08-08 实测] 本函数在当前代码库里**零调用点** —— 全仓 grep(含
    `getattr` 动态派发口径)只命中定义本身与三处注释;`services/diagnosis_report_v2.py`
    的作者还专门写了「⚠️ 不能调 update_brand_stats」把它绕开。
    也就是说:`WO_BRAND_LATEST_CROSS_TENANT_WRITE` §3 把它列为「最危险的一个」是对的
    (签名确实是灾难),但**那 42 条跨品牌指向不可能是它写出来的** —— 它一次都没被调用过。

    保留而不删除的理由:它随时可能被重新接进调用链,那时零校验的签名就会立刻变成活的
    跨租户写入口。**加校验的成本远低于删函数的回归风险。**

    Args:
        brand_id: 品牌 id。
        diagnosis_id: 诊断 id。**必须真属于 brand_id,否则 latest_* 一行都不写。**
        score: 分数。

    Returns:
        latest_* 实际写入的行数(0 = 被守卫挡下)。计数列 `diagnosis_count` 不受守卫影响,
        语义与改动前一致。
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 计数与 latest_* 拆开:计数语义保持原样(诊断跑过就算一次),
        # latest_* 走 SSOT 单点(归属 + 最新 + published-only 三重约束都在 SQL 里)。
        cursor.execute("""
            UPDATE brands SET
                diagnosis_count = diagnosis_count + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (brand_id,))

        synced = sync_brand_latest(
            cursor, diagnosis_id=diagnosis_id, score=score, brand_id=brand_id
        )
        if synced == 0:
            logger.warning(
                "[update_brand_stats] brands.latest_* 未同步(守卫挡下):"
                "brand_id=%s diagnosis_id=%s —— 二者不配对,或该诊断不是该品牌 "
                "published-only 最新那份。计数列已照常 +1。",
                brand_id, diagnosis_id,
            )

        conn.commit()
        return synced
    finally:
        try:
            conn.close()
        except Exception: pass


def list_brands(
    search: str = None,
    industry_category: str = None,
    limit: int = 50,
    offset: int = 0,
    order_by: str = "updated_at DESC"
) -> List[dict]:
    """列出品牌（支持搜索和筛选）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        conditions = []
        params = []
    
        if search:
            conditions.append("(name LIKE %s OR company_name LIKE %s OR brand_code LIKE %s)")
            params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])

        if industry_category:
            # [WO_267] 同时匹配新大类 key 与映射到它的全部存量旧值(存量不回填)
            conditions.append("industry_category = ANY(%s)")
            params.append(filter_values(industry_category))

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        sql = f"""
            SELECT id, brand_code, name, company_name, industry, industry_category,
                   diagnosis_count, latest_score, latest_diagnosis_id,
                   contact, notes, cities, COALESCE(status, 'active') as status,
                   COALESCE(social_enabled, FALSE) as social_enabled,
                   created_at, updated_at
            FROM brands
            WHERE {where_clause}
            ORDER BY {order_by}
            LIMIT %s OFFSET %s
        """
    
        params.extend([limit, offset])
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
    
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def get_brand_by_id(brand_id: int) -> Optional[dict]:
    """按ID获取品牌详情"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM brands WHERE id = %s", (brand_id,))
        row = cursor.fetchone()
        conn.close()
    
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception: pass


def get_brand_diagnoses(brand_id: int, limit: int = 50) -> List[dict]:
    """获取品牌的所有诊断记录"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        # [返工2 P1-2] published-only:排除 withheld(退款/失败/中断)+ pending(生成中占位)· 客户侧只见正式发布诊断
        cursor.execute("""
            SELECT id, session_id, brand_name, industry, total_score, level,
                   created_at, report_md_path, result_visibility
            FROM diagnosis_records
            WHERE brand_id = %s
              AND (result_visibility IS NULL OR result_visibility = 'published')
            ORDER BY created_at DESC
            LIMIT %s
        """, (brand_id, limit))
    
        rows = cursor.fetchall()
        conn.close()
    
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def get_brand_trend(brand_id: int) -> dict:
    """
    获取品牌趋势数据（用于趋势图）
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        # [返工2 P1-2] 趋势图 published-only:排除 withheld(退款/失败)+ pending(生成中)· 否则污染客户分数曲线
        cursor.execute("""
            SELECT id, total_score, level, created_at,
                   web_search_score, platform_score, content_quality_score,
                   authority_score, brand_ownership_score, ai_visibility_score,
                   ai_citation_score, update_frequency_score
            FROM diagnosis_records
            WHERE brand_id = %s
              AND (result_visibility IS NULL OR result_visibility = 'published')
            ORDER BY created_at ASC
        """, (brand_id,))
    
        rows = cursor.fetchall()
        conn.close()
    
        if not rows:
            return {"diagnoses": [], "trend_insight": "暂无诊断数据"}
    
        diagnoses = [dict(row) for row in rows]
    
        # 生成趋势洞察
        if len(diagnoses) >= 2:
            first_score = diagnoses[0]["total_score"]
            latest_score = diagnoses[-1]["total_score"]
            change = latest_score - first_score
            change_pct = (change / first_score * 100) if first_score > 0 else 0
        
            if change > 0:
                trend_insight = f"{len(diagnoses)}次诊断，GEO评分从{first_score}分提升至{latest_score}分，增长{change_pct:.1f}%"
            elif change < 0:
                trend_insight = f"{len(diagnoses)}次诊断，GEO评分从{first_score}分下降至{latest_score}分，下降{abs(change_pct):.1f}%"
            else:
                trend_insight = f"{len(diagnoses)}次诊断，GEO评分保持{latest_score}分稳定"
        else:
            trend_insight = f"已完成{len(diagnoses)}次诊断，评分{diagnoses[0]['total_score']}分"
    
        return {
            "diagnoses": diagnoses,
            "total_count": len(diagnoses),
            "trend_insight": trend_insight
        }
    finally:
        try:
            conn.close()
        except Exception: pass


def migrate_existing_diagnoses():
    """Retired: a display name cannot safely determine diagnosis ownership."""
    raise RuntimeError(
        "P1 DIAGNOSIS_BRAND_ID_BACKFILL_MAPPING_REQUIRED: "
        "provide an explicit diagnosis_id -> brand_id mapping; brand_name is not an identity"
    )


# ========== 客户资料 CRUD ==========

def save_client_materials(
    diagnosis_id: int,
    materials: Dict[str, Any],
    *,
    organization_identity=None,
    expected_brand_id: Optional[int] = None,
) -> int:
    """
    保存客户资料
    
    Args:
        diagnosis_id: 诊断记录ID
        materials: 客户资料字典
        
    Returns:
        记录ID
    """
    init_db()
    conn = get_connection()
    try:
        cursor = conn.cursor()

        sync_profiles_allowed = organization_identity is None
        if organization_identity is not None:
            from services.organization_service import _lock_identity
            organization_identity = _lock_identity(cursor, organization_identity)
            organization_identity.require_active_organization()
            if organization_identity.is_member:
                organization_identity.require("materials.write")
            sync_profiles_allowed = (
                organization_identity.is_owner
                or "clients.profile_edit" in organization_identity.capabilities
            )

        # P1 修(staging blocker · CTO-F 2026-04-27):
        # brand_id 必须在 UPDATE 分支之前就解析好 · 否则走 UPDATE 时
        # _sync_materials_to_profiles(brand_id or ...) 会因 NameError 失败
        cursor.execute(
            """
            SELECT brand_id,organization_id FROM diagnosis_records
            WHERE id = %s FOR UPDATE
            """,
            (diagnosis_id,),
        )
        diag = cursor.fetchone()
        if not diag or not diag.get("brand_id"):
            from services.organization_contract import OrganizationError
            raise OrganizationError(
                "ORG_ARTIFACT_NOT_FOUND",
                "诊断记录不存在或尚未关联客户",
                http_status=404,
            )
        brand_id = int(diag["brand_id"])
        if expected_brand_id is not None and brand_id != int(expected_brand_id):
            from services.organization_contract import OrganizationError
            raise OrganizationError(
                "ORG_ARTIFACT_NOT_FOUND",
                "诊断记录不存在或无权访问",
                http_status=404,
            )
        cursor.execute(
            """
            SELECT id,owner_user_id,COALESCE(is_deleted,FALSE) AS is_deleted
            FROM brands WHERE id=%s FOR UPDATE
            """,
            (brand_id,),
        )
        brand = cursor.fetchone()
        if not brand or bool(brand["is_deleted"]):
            from services.organization_contract import OrganizationError
            raise OrganizationError("ORG_BRAND_INACTIVE", "客户已删除，不能保存资料", http_status=409)
        if organization_identity is not None:
            if (
                int(brand["owner_user_id"]) != int(organization_identity.principal_user_id)
                or (
                    diag.get("organization_id") is not None
                    and int(diag["organization_id"]) != int(organization_identity.organization_id)
                )
            ):
                from services.organization_contract import OrganizationError
                raise OrganizationError(
                    "ORG_ARTIFACT_NOT_FOUND",
                    "诊断记录不存在或无权访问",
                    http_status=404,
                )

        # 检查是否已存在
        cursor.execute(
            "SELECT id FROM client_materials WHERE diagnosis_id = %s FOR UPDATE",
            (diagnosis_id,),
        )
        existing = cursor.fetchone()

        if existing:
            # 更新
            cursor.execute("""
                UPDATE client_materials SET
                    company_intro = %s,
                    founding_year = %s,
                    team_size = %s,
                    service_area = %s,
                    core_selling_points = %s,
                    unique_value = %s,
                    methodology = %s,
                    case_studies = %s,
                    pricing_tiers = %s,
                    testimonials = %s,
                    credentials = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE diagnosis_id = %s
            """, (
                materials.get("company_intro"),
                materials.get("founding_year"),
                materials.get("team_size"),
                materials.get("service_area"),
                json.dumps(materials.get("core_selling_points", []), ensure_ascii=False),
                materials.get("unique_value"),
                materials.get("methodology"),
                json.dumps(materials.get("case_studies", []), ensure_ascii=False),
                json.dumps(materials.get("pricing_tiers", []), ensure_ascii=False),
                json.dumps(materials.get("testimonials", []), ensure_ascii=False),
                json.dumps(materials.get("credentials", []), ensure_ascii=False),
                diagnosis_id
            ))
            record_id = existing["id"]
        else:
            # 新增 · brand_id 已在函数顶部解析
            cursor.execute("""
                INSERT INTO client_materials (
                    diagnosis_id, brand_id,
                    company_intro, founding_year, team_size, service_area,
                    core_selling_points, unique_value, methodology,
                    case_studies, pricing_tiers, testimonials, credentials
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                diagnosis_id,
                brand_id,
                materials.get("company_intro"),
                materials.get("founding_year"),
                materials.get("team_size"),
                materials.get("service_area"),
                json.dumps(materials.get("core_selling_points", []), ensure_ascii=False),
                materials.get("unique_value"),
                materials.get("methodology"),
                json.dumps(materials.get("case_studies", []), ensure_ascii=False),
                json.dumps(materials.get("pricing_tiers", []), ensure_ascii=False),
                json.dumps(materials.get("testimonials", []), ensure_ascii=False),
                json.dumps(materials.get("credentials", []), ensure_ascii=False)
            ))
            record_id = cursor.fetchone()["id"]

        if organization_identity is not None:
            from services.organization_artifacts import stamp_artifact
            stamp_artifact(
                cursor,
                organization_identity,
                artifact_type="client_material",
                artifact_id=record_id,
                brand_id=brand_id,
                visibility="private",
            )

        if sync_profiles_allowed:
            profile_ids = _sync_materials_to_profiles_with_cursor(cursor, brand_id, materials)
            if organization_identity is not None:
                from services.organization_artifacts import stamp_artifact
                for profile_id in profile_ids:
                    stamp_artifact(
                        cursor,
                        organization_identity,
                        artifact_type="client_profile",
                        artifact_id=profile_id,
                        brand_id=brand_id,
                        visibility="private",
                    )

        conn.commit()
        return record_id
    finally:
        try:
            conn.close()
        except Exception: pass


def _profile_updates_from_materials(materials: Dict[str, Any]) -> Dict[str, Any]:
    updates: Dict[str, Any] = {}
    if materials.get("company_intro"):
        updates["company_intro"] = materials["company_intro"]
    if materials.get("unique_value"):
        updates["core_value"] = materials["unique_value"]
    if materials.get("core_selling_points"):
        points = materials["core_selling_points"]
        if isinstance(points, list):
            updates["selling_points"] = "; ".join(
                point.get("point", str(point)) if isinstance(point, dict) else str(point)
                for point in points
            )
        elif isinstance(points, str):
            updates["selling_points"] = points
    for source, target in (("case_studies", "success_cases"), ("testimonials", "testimonials")):
        value = materials.get(source)
        if value:
            updates[target] = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
    return updates


def _merge_material_structured_knowledge(existing_raw: Any, materials: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(existing_raw, str):
        try:
            existing = json.loads(existing_raw) if existing_raw.strip() else {}
        except (TypeError, ValueError):
            existing = {}
    elif isinstance(existing_raw, dict):
        existing = dict(existing_raw)
    else:
        existing = {}
    new_value = materials.get("structured_knowledge") or {}
    new_value = dict(new_value) if isinstance(new_value, dict) else {}
    if not new_value.get("cases") and materials.get("case_studies"):
        new_value["cases"] = materials.get("case_studies") or []
    if not new_value.get("differentiation") and materials.get("unique_value"):
        new_value["differentiation"] = {
            "usp": materials.get("unique_value") or "",
            "advantages": [],
        }
    merged = dict(existing)
    for key in ("differentiation", "products", "customers", "painPoints", "cases"):
        old_value = merged.get(key)
        if isinstance(old_value, dict) and any(old_value.values()):
            continue
        if isinstance(old_value, list) and old_value:
            continue
        if new_value.get(key):
            merged[key] = new_value[key]
    return merged


def _sync_materials_to_profiles_with_cursor(cursor, brand_id: int, materials: Dict[str, Any]) -> List[Any]:
    """Synchronize profile fields in the material caller's transaction."""
    updates = _profile_updates_from_materials(materials)
    merged_for_new_profile = _merge_material_structured_knowledge({}, materials)
    if not updates and not merged_for_new_profile:
        return []
    cursor.execute(
        """
        SELECT id,structured_knowledge FROM client_profiles
        WHERE brand_id=%s AND (is_deleted=0 OR is_deleted IS NULL)
        ORDER BY id FOR UPDATE
        """,
        (brand_id,),
    )
    locked_profiles = list(cursor.fetchall())
    profile_ids = [row["id"] for row in locked_profiles]
    if not profile_ids:
        cursor.execute("SELECT name,industry FROM brands WHERE id=%s", (brand_id,))
        brand = cursor.fetchone()
        if not brand:
            raise ValueError("brand disappeared during profile synchronization")
        profile_id = str(uuid.uuid4())[:8]
        create_updates = dict(updates)
        if merged_for_new_profile:
            create_updates["structured_knowledge"] = json.dumps(merged_for_new_profile, ensure_ascii=False)
        columns = ["id", "name", "industry", "brand_id", *create_updates.keys()]
        values = [profile_id, brand.get("name") or f"brand_{brand_id}", brand.get("industry") or "", brand_id, *create_updates.values()]
        cursor.execute(
            f"INSERT INTO client_profiles ({','.join(columns)}) VALUES ({','.join(['%s'] * len(values))})",
            values,
        )
        profile_ids = [profile_id]
    else:
        # Preserve previously confirmed dimensions independently for every
        # profile; new AI material only fills empty dimensions.
        for profile in locked_profiles:
            row_updates = dict(updates)
            merged = _merge_material_structured_knowledge(profile.get("structured_knowledge"), materials)
            existing = _merge_material_structured_knowledge(profile.get("structured_knowledge"), {})
            if merged != existing:
                row_updates["structured_knowledge"] = json.dumps(merged, ensure_ascii=False)
            if not row_updates:
                continue
            assignments = ",".join(f"{column}=%s" for column in row_updates)
            cursor.execute(
                f"UPDATE client_profiles SET {assignments},updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                [*row_updates.values(), profile["id"]],
            )
            if cursor.rowcount != 1:
                raise RuntimeError("client profile synchronization row count changed")
    return profile_ids


def save_profile_materials_for_brand(
    brand_id: int,
    materials: Dict[str, Any],
    *,
    organization_identity,
) -> List[Any]:
    """Atomically update profile-only materials for an organization request."""
    if organization_identity is None:
        raise ValueError("organization identity is required for profile-only material writes")
    conn = get_connection()
    try:
        cursor = conn.cursor()
        from services.organization_service import _lock_identity
        organization_identity = _lock_identity(cursor, organization_identity)
        organization_identity.require_active_organization()
        if organization_identity.is_member:
            organization_identity.require("clients.profile_edit")
        cursor.execute(
            """
            SELECT id,owner_user_id,COALESCE(is_deleted,FALSE) AS is_deleted
            FROM brands WHERE id=%s FOR UPDATE
            """,
            (int(brand_id),),
        )
        brand = cursor.fetchone()
        if (
            not brand
            or bool(brand["is_deleted"])
            or int(brand["owner_user_id"]) != int(organization_identity.principal_user_id)
        ):
            from services.organization_contract import OrganizationError
            raise OrganizationError(
                "ORG_BRAND_INACTIVE",
                "客户已删除、已转移或不属于当前老板",
                http_status=409,
            )
        profile_ids = _sync_materials_to_profiles_with_cursor(cursor, int(brand_id), materials)
        from services.organization_artifacts import stamp_artifact
        for profile_id in profile_ids:
            stamp_artifact(
                cursor,
                organization_identity,
                artifact_type="client_profile",
                artifact_id=profile_id,
                brand_id=int(brand_id),
                visibility="private",
            )
        conn.commit()
        return profile_ids
    finally:
        conn.close()


def _get_brand_id_for_diagnosis(diagnosis_id: int) -> Optional[int]:
    """获取诊断记录关联的品牌ID"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT brand_id FROM diagnosis_records WHERE id = %s", (diagnosis_id,))
        row = cursor.fetchone()
        conn.close()
        return row["brand_id"] if row else None
    finally:
        try:
            conn.close()
        except Exception: pass


def _get_brand_identity_for_profile(brand_id: int) -> Dict[str, str]:
    """读取品牌名/行业用于自动创建 client_profiles，name 以 brands.name 为准。"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT name, industry FROM brands WHERE id = %s", (brand_id,))
        row = cursor.fetchone()
        if not row:
            return {"name": f"brand_{brand_id}", "industry": ""}
        return {
            "name": row.get("name") or f"brand_{brand_id}",
            "industry": row.get("industry") or "",
        }
    finally:
        try:
            conn.close()
        except Exception: pass


def _sync_materials_to_profiles(brand_id: Optional[int], materials: dict):
    """将 client_materials 中的业务数据同步到关联的 client_profiles

    Phase 06 (CTO-15.23 2026-05-03 T7a) · 扩 unique_value → core_value 同步
    """
    if not brand_id:
        return
    try:
        from db.profile_db import create_profile, list_profiles, update_profile
        profiles = list_profiles(brand_ids=[brand_id])
        # 字段映射：client_materials → client_profiles
        profile_update = {}
        if materials.get("company_intro"):
            profile_update["company_intro"] = materials["company_intro"]
        # T7a · unique_value → core_value(诊断里的"独特价值"对应品牌的"核心价值主张")
        if materials.get("unique_value"):
            profile_update["core_value"] = materials["unique_value"]
        if materials.get("core_selling_points"):
            sp = materials["core_selling_points"]
            # 转为文本摘要
            if isinstance(sp, list):
                profile_update["selling_points"] = "; ".join(
                    p.get("point", str(p)) if isinstance(p, dict) else str(p) for p in sp
                )
            elif isinstance(sp, str):
                profile_update["selling_points"] = sp
        if materials.get("case_studies"):
            cs = materials["case_studies"]
            if isinstance(cs, list):
                profile_update["success_cases"] = json.dumps(cs, ensure_ascii=False) if not isinstance(cs, str) else cs
            elif isinstance(cs, str):
                profile_update["success_cases"] = cs
        if materials.get("testimonials"):
            t = materials["testimonials"]
            if isinstance(t, list):
                profile_update["testimonials"] = json.dumps(t, ensure_ascii=False) if not isinstance(t, str) else t
            elif isinstance(t, str):
                profile_update["testimonials"] = t
        if profile_update:
            if not profiles:
                brand_identity = _get_brand_identity_for_profile(brand_id)
                new_profile_id = create_profile(
                    name=brand_identity["name"],
                    industry=brand_identity["industry"],
                    brand_id=brand_id,
                    **profile_update,
                )
                profiles = list_profiles(brand_ids=[brand_id])
                if not profiles and new_profile_id:
                    profiles = [{"id": new_profile_id}]
            for p in profiles:
                # 注意:此处调 update_profile 会触发 _sync_profiles_to_materials
                # 但 update_profile 内已通过 _is_internal_sync flag 防止循环(见 T7b)
                update_profile(p["id"], _is_internal_sync=True, **profile_update)
    except Exception as e:
        print(f"[Diagnosis DB] _sync_materials_to_profiles 异常: {e}")


# ============================================================
# Phase 06 (CTO-15.23 2026-05-03 T7b)
# 反向同步 · client_profiles 改 → 写回 client_materials(若该 brand 有诊断)
# ============================================================

def _sync_profiles_to_materials(brand_id: Optional[int], profile_fields: dict):
    """将 client_profiles 修改的字段反向同步到关联 brand 最近一次诊断的 client_materials

    Phase 06 (CTO-15.23 2026-05-03 T7b) · 老板诉求"任一处改另一处刷新必看到最新值"

    实现策略:
      - 取该 brand 最近一次诊断(client_materials 主键 = diagnosis_id)
      - 没诊断 → 跳过(无 client_materials 记录)
      - 用 raw SQL 直接 UPDATE · 不走 save_client_materials 避免循环

    字段映射(client_profiles → client_materials):
      - company_intro → company_intro
      - core_value → unique_value
      - selling_points (string / JSON) → core_selling_points (JSON array of {point, evidence})
      - success_cases (string / JSON) → case_studies (JSON array)
      - testimonials (string / JSON) → testimonials (JSON array)
    """
    if not brand_id:
        return
    try:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            # 取最近一次诊断 ID([返工3 P2] 业务消费=挂物料 → published-only · 禁把 pending/withheld 当"最新")
            cursor.execute(
                "SELECT id FROM diagnosis_records WHERE brand_id = %s "
                "AND (result_visibility IS NULL OR result_visibility = 'published') "
                "ORDER BY created_at DESC LIMIT 1",
                (brand_id,),
            )
            row = cursor.fetchone()
            if not row:
                return  # 该 brand 没诊断 · 无 client_materials 记录 · 不需要同步
            diagnosis_id = row["id"]

            # 检查 client_materials 是否存在
            cursor.execute("SELECT id FROM client_materials WHERE diagnosis_id = %s", (diagnosis_id,))
            mat_row = cursor.fetchone()
            if not mat_row:
                return  # 诊断有 · 但还没填 client_materials · 不需要同步(避免无中生有)

            sets = []
            vals = []

            if profile_fields.get("company_intro"):
                sets.append("company_intro = %s")
                vals.append(profile_fields["company_intro"])

            if profile_fields.get("core_value"):
                sets.append("unique_value = %s")
                vals.append(profile_fields["core_value"])

            if profile_fields.get("selling_points"):
                sp = profile_fields["selling_points"]
                # 兼容 string / list / JSON-string-of-list
                if isinstance(sp, list):
                    sp_arr = sp
                elif isinstance(sp, str):
                    try:
                        parsed = json.loads(sp)
                        if isinstance(parsed, list):
                            sp_arr = parsed
                        else:
                            sp_arr = [{"point": sp, "evidence": ""}]
                    except (json.JSONDecodeError, TypeError):
                        # 老格式 · "; " 拼接 string
                        parts = [p.strip() for p in sp.split(";") if p.strip()]
                        sp_arr = [{"point": p, "evidence": ""} for p in parts]
                else:
                    sp_arr = []
                sets.append("core_selling_points = %s")
                vals.append(json.dumps(sp_arr, ensure_ascii=False))

            if profile_fields.get("success_cases"):
                sc = profile_fields["success_cases"]
                if isinstance(sc, list):
                    sc_arr = sc
                elif isinstance(sc, str):
                    try:
                        parsed = json.loads(sc)
                        sc_arr = parsed if isinstance(parsed, list) else []
                    except (json.JSONDecodeError, TypeError):
                        sc_arr = []
                else:
                    sc_arr = []
                sets.append("case_studies = %s")
                vals.append(json.dumps(sc_arr, ensure_ascii=False))

            if profile_fields.get("testimonials"):
                tm = profile_fields["testimonials"]
                if isinstance(tm, list):
                    tm_arr = tm
                elif isinstance(tm, str):
                    try:
                        parsed = json.loads(tm)
                        tm_arr = parsed if isinstance(parsed, list) else []
                    except (json.JSONDecodeError, TypeError):
                        tm_arr = []
                else:
                    tm_arr = []
                sets.append("testimonials = %s")
                vals.append(json.dumps(tm_arr, ensure_ascii=False))

            if not sets:
                return

            sets.append("updated_at = CURRENT_TIMESTAMP")
            vals.append(diagnosis_id)
            cursor.execute(
                f"UPDATE client_materials SET {', '.join(sets)} WHERE diagnosis_id = %s",
                vals,
            )
            conn.commit()
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        print(f"[Diagnosis DB] _sync_profiles_to_materials 异常: {e}")


def get_client_materials(diagnosis_id: int) -> Optional[Dict[str, Any]]:
    """
    获取客户资料
    
    Args:
        diagnosis_id: 诊断记录ID
        
    Returns:
        客户资料字典，不存在则返回None
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            SELECT * FROM client_materials WHERE diagnosis_id = %s
        """, (diagnosis_id,))
    
        row = cursor.fetchone()
        conn.close()
    
        if not row:
            return None
    
        # 解析JSON字段
        result = dict(row)
        for json_field in ["core_selling_points", "case_studies", "pricing_tiers", "testimonials", "credentials"]:
            if result.get(json_field):
                try:
                    result[json_field] = json.loads(result[json_field])
                except:
                    result[json_field] = []
    
        return result
    finally:
        try:
            conn.close()
        except Exception: pass


def get_client_materials_by_brand(brand_id: int) -> Optional[Dict[str, Any]]:
    """
    按品牌ID获取最新客户资料（用于同一品牌多次诊断的情况）
    
    Args:
        brand_id: 品牌ID
        
    Returns:
        最新的客户资料字典
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            SELECT * FROM client_materials
            WHERE brand_id = %s
            ORDER BY updated_at DESC
            LIMIT 1
        """, (brand_id,))
    
        row = cursor.fetchone()
        conn.close()
    
        if not row:
            return None
    
        result = dict(row)
        for json_field in ["core_selling_points", "case_studies", "pricing_tiers", "testimonials", "credentials"]:
            if result.get(json_field):
                try:
                    result[json_field] = json.loads(result[json_field])
                except:
                    result[json_field] = []
    
        return result
    finally:
        try:
            conn.close()
        except Exception: pass


def get_confirmed_materials(brand_id: int) -> Optional[Dict[str, Any]]:
    """
    获取客户已确认的营销资料快照。
    这是写作大厅和内容创作的唯一权威素材源。
    如果客户尚未确认，返回 None（调用方 fallback 到传统数据源）。
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT materials_snapshot, confirmed_at, customer_notes
            FROM marketing_confirm_sessions
            WHERE brand_id = %s AND status = 'confirmed'
            ORDER BY confirmed_at DESC LIMIT 1
        """, (brand_id,))
        row = cursor.fetchone()
        conn.close()

        if not row or not row.get('materials_snapshot'):
            return None

        snapshot = json.loads(row['materials_snapshot'])

        # 转换为 client_materials 兼容格式，供 DistillerPipeline 直接使用
        sp = snapshot.get('selling_points', {})
        return {
            'brand_id': brand_id,
            'company_intro': snapshot.get('company', {}).get('intro', ''),
            'unique_value': snapshot.get('company', {}).get('core_value', ''),
            'service_area': snapshot.get('company', {}).get('service_area', ''),
            'core_selling_points': sp.get('items') or [],
            'case_studies': snapshot.get('cases') or [],
            'testimonials': snapshot.get('testimonials') or [],
            'credentials': snapshot.get('credentials') or [],
            'methodology': snapshot.get('methodology', ''),
            '_confirmed_at': row.get('confirmed_at'),
            '_customer_notes': row.get('customer_notes', ''),
            '_is_confirmed': True,
            '_raw_snapshot': snapshot,
        }
    finally:
        try:
            conn.close()
        except Exception: pass


def _writing_materials_has_content(materials: Optional[Dict[str, Any]]) -> bool:
    """判断资料是否足够作为写作素材，避免空 JSON 误判可用。"""
    if not materials:
        return False
    for key in (
        "company_intro",
        "unique_value",
        "methodology",
        "core_selling_points",
        "case_studies",
        "testimonials",
        "credentials",
        "service_area",
    ):
        value = materials.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, (list, dict)) and value:
            return True
    return False


def get_writing_materials_by_brand(brand_id: int) -> Optional[Dict[str, Any]]:
    """
    获取写作可用的客户资料。
    已确认资料优先；未确认但销售已整理的资料可作为写作大厅兜底素材。
    """
    confirmed = get_confirmed_materials(brand_id)
    if _writing_materials_has_content(confirmed):
        confirmed["_material_source"] = "confirmed"
        return confirmed

    draft = get_client_materials_by_brand(brand_id)
    if _writing_materials_has_content(draft):
        draft["_is_confirmed"] = False
        draft["_material_source"] = "draft"
        return draft

    conn = get_connection()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT *
                FROM client_profiles
                WHERE brand_id = %s
                ORDER BY updated_at DESC NULLS LAST
                LIMIT 1
                """,
                (brand_id,),
            )
        except Exception:
            conn.rollback()
            cursor.execute("SELECT * FROM client_profiles WHERE brand_id = %s LIMIT 1", (brand_id,))

        profile = cursor.fetchone()
        if profile:
            profile = dict(profile)

            def _profile_list(value: Any, field_name: str) -> list:
                if not value:
                    return []
                if isinstance(value, list):
                    return value
                if isinstance(value, dict):
                    return [value]
                text = str(value).strip()
                if not text:
                    return []
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, list):
                        return parsed
                    if isinstance(parsed, dict):
                        return [parsed]
                except Exception:
                    pass
                return [{field_name: text}]

            profile_materials = {
                "brand_id": brand_id,
                "company_intro": profile.get("company_intro") or profile.get("business_summary") or "",
                "unique_value": profile.get("core_value") or profile.get("differentiation") or "",
                "service_area": profile.get("target_users") or profile.get("city_scope") or "",
                "core_selling_points": _profile_list(profile.get("selling_points"), "point"),
                "case_studies": _profile_list(profile.get("success_cases"), "result"),
                "testimonials": _profile_list(profile.get("testimonials"), "quote"),
                "credentials": [],
                "methodology": profile.get("content_direction") or profile.get("structured_knowledge") or "",
                "_is_confirmed": False,
                "_material_source": "client_profile",
            }
            if _writing_materials_has_content(profile_materials):
                return profile_materials
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return None


# ==========================================
#  报价系统 CRUD 函数
# ==========================================

def save_quote(quote_data: dict, *, cursor=None) -> int:
    """
    保存报价单
    
    Args:
        quote_data: 报价数据
        
    Returns:
        报价单ID
    """
    conn = None if cursor is not None else get_connection()
    try:
        cursor = cursor or conn.cursor()

        # 🔴 [工单 V4-A · Codex fix-of-fix P1-2a] 品牌级序列化点(见
        #    ``lock_brand_quote_serialization_point`` 的注释):与门户 token 轮换互斥。
        lock_brand_quote_serialization_point(cursor, quote_data.get("brand_id") or 0)

        cursor.execute("""
            INSERT INTO quotes (
                diagnosis_id, brand_id, brand_name, industry, city,
                tier, target_share, total_keywords, total_articles,
                monthly_price, status, markdown
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            quote_data.get("diagnosis_id"),
            quote_data.get("brand_id"),
            quote_data.get("brand_name"),
            quote_data.get("industry"),
            quote_data.get("city"),
            quote_data.get("tier", "standard"),
            quote_data.get("target_share", 0.20),
            quote_data.get("total_keywords", 0),
            quote_data.get("total_articles", 0),
            quote_data.get("monthly_price", 0),
            quote_data.get("status", "draft"),
            quote_data.get("markdown", "")
        ))

        quote_id = cursor.fetchone()["id"]

        # 同步更新品牌的cities字段
        brand_id = quote_data.get("brand_id")
        city = quote_data.get("city")
        if brand_id and city:
            try:
                cursor.execute(
                    "UPDATE brands SET cities = %s WHERE id = %s AND (cities IS NULL OR cities = '')",
                    (city, brand_id)
                )
            except Exception:
                pass

        if conn is not None:
            conn.commit()
            conn.close()
        return quote_id
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception: pass


def get_quote(quote_id: int) -> Optional[dict]:
    """获取报价单详情"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM quotes WHERE id = %s", (quote_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception: pass


def get_quotes_list(
    status: str = None,
    brand_name: str = None,
    brand_id: int = None,
    limit: int = 50,
    offset: int = 0,
    include_deleted: bool = False,  # P0.5b · 默认过滤软删
) -> dict:
    """获取报价单列表

    P0.5b (CTO-15.7 2026-04-24): 默认过滤 deleted_at IS NOT NULL 的软删 quote
    · 代理报价中心 / 客户门户不应看到 P0.5 清理的 157 份空壳
    · 管理后台若需查已删记录 · 传 include_deleted=True
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        query = "SELECT * FROM quotes WHERE 1=1"
        params = []

        # P0.5b 软删过滤(老板批 · 存量 ~154 空壳已 cleanup)
        if not include_deleted:
            query += " AND deleted_at IS NULL"

        if brand_id:
            query += " AND brand_id = %s"
            params.append(brand_id)
        if status:
            query += " AND status = %s"
            params.append(status)
        if brand_name:
            query += " AND brand_name LIKE %s"
            params.append(f"%{brand_name}%")

        # 获取总数
        count_query = query.replace("SELECT *", "SELECT COUNT(*) AS cnt")
        cursor.execute(count_query, params)
        total = cursor.fetchone()["cnt"]

        # 获取列表
        query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])
        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
    
        return {
            "total": total,
            "items": [dict(row) for row in rows]
        }
    finally:
        try:
            conn.close()
        except Exception: pass


def update_quote_status(quote_id: int, status: str) -> bool:
    """更新报价单状态 + 联动监测订阅(CTO-15.23 2026-05-09)

    监测订阅联动规则(老板拍板 · 消费一次扣一次模型):
    - paid                              : 不动监测订阅(代理在监测页自助开)
    - draft / confirmed / pending_payment: 防白嫖 · 取消所有 active 订阅(还没付款不该跑监测)
    - archived / cancelled / refunded   : 取消所有 active 订阅(状态退出 · 必须停止扣费)
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        if status == "confirmed":
            # [CTO-15.23 2026-05-29 D1/D2/D4 老板拍] confirmed = 服务已激活/开始履约。
            # 写【权威服务锚】service_start_date(NULL 才填 · 不覆盖真实开始日)· 单点修
            # "代理确认 quote 时锚未写入"根因 · 所有 confirm 路径必经此 SSOT。
            # paid_at 不在此强写(降级为财务字段 · 玩法 B 平台多半观测不到客户付款)。
            cursor.execute("""
                UPDATE quotes SET status = %s, confirmed_at = CURRENT_TIMESTAMP,
                       service_start_date = COALESCE(service_start_date, CURRENT_DATE)
                WHERE id = %s
            """, (status, quote_id))
        elif status == "paid":
            # paid 同写服务锚(单一锚口径 · 全链 COALESCE(service_start_date, paid_at))· paid_at 仍写(财务)
            cursor.execute("""
                UPDATE quotes SET status = %s, paid_at = CURRENT_TIMESTAMP,
                       service_start_date = COALESCE(service_start_date, CURRENT_DATE)
                WHERE id = %s
            """, (status, quote_id))
        else:
            cursor.execute("""
                UPDATE quotes SET status = %s WHERE id = %s
            """, (status, quote_id))

        conn.commit()
        conn.close()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # 监测订阅联动 hook · 失败不阻塞主流程
    try:
        _sync_keyword_monitor_state_on_status_change(quote_id, status)
    except Exception as e:
        try:
            import logging as _logging
            _logging.getLogger("GEO-Diagnosis-DB").warning(
                f"[update_quote_status] 监测订阅同步失败 quote={quote_id} status={status}: {e}"
            )
        except Exception:
            pass

    return True


def _sync_keyword_monitor_state_on_status_change(quote_id: int, status: str):
    """quote.status 变化时同步监测订阅(CTO-15.23 2026-05-09 老板拍板规则)"""
    if status in ("paid", "confirmed"):
        # [CTO-15.23 2026-05-29 D1/D4 老板拍] confirmed = 服务已激活 → 与 paid 同口径:
        # 不自动开监测(代理自助点)· 也【不取消】(代理已开的保留)· 不再把 confirmed 当未付草稿清理。
        return
    # 其余(draft/pending_payment/archived/cancelled/refunded/sent)= 未开始 / 已退出
    # → cancel 所有 active 订阅(防白嫖 + 状态退回清理)
    from db.monitoring_db import cancel_subscriptions_by_quote
    cancel_subscriptions_by_quote(quote_id, reason=f"quote_status_{status}")


def save_confirmed_keywords(quote_id: int, keywords: list[dict]) -> list[int]:
    """
    批量保存确认的关键词
    
    Args:
        quote_id: 报价单ID
        keywords: 关键词列表 [{"keyword": "...", "tier": "...", "final_price": ..., "required_articles": N, "recommended_platforms": [...]}]
        
    Returns:
        关键词ID列表
    """
    # [WO_DELIVERY_FLYWHEEL_CLOSURE §2.4.2 · 2026-08-06] 关键词卫生 —— **本处只留痕不阻断**。
    #
    # 🔴 边界说清楚(不假装全覆盖):人工输入的四个入口(append / replace / 写作大厅补词 /
    #    快速建项目)已在 server.py `_hygiene_gate_keywords` 处**硬拦**。本函数是诊断→选词
    #    流水线的自动产词路径,有 12 处调用方,在 DB 层抛 400 会打断主收入链;而且自动产词
    #    的地名来自 brands.cities 本身,真错了闸也会因为白名单继承而放行。
    #    因此这里的定位是**可观测性**:错字词进来时留一条 warning,别让它悄无声息。
    try:
        from services.keyword_hygiene import check_keywords as _kw_check

        _report = _kw_check(
            [str(k.get("keyword") or "") for k in (keywords or [])],
            with_history=False,   # 流水线路径不打额外的历史查询
        )
        if _report.get("blocking"):
            logger.warning(
                "[keyword-hygiene] 自动产词路径出现疑似错字(未阻断 · quote_id=%s): %s",
                quote_id, [i.get("message") for i in _report["blocking"]][:5],
            )
    except Exception as _he:  # noqa: BLE001
        logger.debug("[keyword-hygiene] 留痕检查跳过: %s", _he)

    conn = get_connection()
    try:
        cursor = conn.cursor()

        keyword_ids = []
        for kw in keywords:
            # 处理平台推荐(JSON转字符串)
            platforms = kw.get("recommended_platforms", [])
            platforms_json = json.dumps(platforms) if platforms else None
        
            # M1b (CTO-15.17 · 2026-04-26):若 caller 已分类传入 layer 字段则持久化
            # · 6 层 enum:brand_defense / category_grab / scenario_decision /
            #             geo_conversion / competitor_intercept / evidence_trust
            # · 没传则 layer/layer_reason/layer_confidence 留 NULL · GET 阶段 on-the-fly 兜底
            cursor.execute("""
                INSERT INTO confirmed_keywords (
                    quote_id, keyword, category, tier,
                    base_price, city_premium, final_price, competitor_count,
                    required_articles, recommended_platforms, intent, funnel_stage, status,
                    cluster_id, is_core, monitoring_query,
                    layer, layer_reason, layer_confidence,
                    super_red_ocean
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                quote_id,
                kw.get("keyword"),
                kw.get("category"),
                kw.get("tier"),
                kw.get("base_price", 0),
                kw.get("city_premium", 1.0),
                kw.get("final_price", kw.get("unit_price", 0)),
                kw.get("competitor_count", 0),
                # [P1 容量合同 2026-08-08] 落库的是**可交付容量上限**。走 SSOT 规整:
                #   显式 0(覆盖词)保留;缺失时兜底值来自具名常量,数值与上线前一致(1)。
                _normalize_article_capacity(
                    kw.get("required_articles"), when_missing=_CAPACITY_MISSING_DEFAULT),
                platforms_json,
                kw.get("intent", "informational"),
                kw.get("funnel_stage", "awareness"),
                "pending",
                kw.get("cluster_id"),       # NULL for flat mode
                kw.get("is_core", True),    # TRUE by default (backward compat)
                kw.get("monitoring_query"),  # P0.8 CTO-15.9 bug 3 二审修
                kw.get("layer"),             # M1b · NULL 时 GET 阶段兜底分类
                kw.get("layer_reason"),
                kw.get("layer_confidence"),
                bool(kw.get("super_red_ocean", False)),  # §4.2 超红海标 · 达标排除依据
            ))
            keyword_ids.append(cursor.fetchone()["id"])

        # 更新报价单的关键词总数
        # [audit P2 2026-06-10] 多主题包订单 mark_paid 每 cluster 调一次本函数,旧版覆写=本批词数
        # → total_keywords 只剩最后一个包的词数。改 COUNT 全量重算(幂等·与实际确认词永远一致)。
        cursor.execute("""
            UPDATE quotes SET total_keywords = (
                SELECT COUNT(*) FROM confirmed_keywords WHERE quote_id = %s
            ) WHERE id = %s
        """, (quote_id, quote_id))
    
        conn.commit()
        conn.close()
        return keyword_ids
    finally:
        try:
            conn.close()
        except Exception: pass


def save_keyword_cluster(quote_id: int, session_token: str, cluster: dict) -> int:
    """
    保存一个主题包到 keyword_clusters 表。

    Args:
        quote_id: 报价单ID
        session_token: 会话token
        cluster: 主题包数据（来自 clusters_data）

    Returns:
        新建的 cluster ID
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        pricing = cluster.get("pricing", {})

        cursor.execute("""
            INSERT INTO keyword_clusters (
                quote_id, session_token, cluster_name, business_tag, city_tag, scenario_tag,
                description, core_keyword_count, covered_keyword_count,
                price_entry, price_standard, price_flagship,
                articles_entry, articles_standard, articles_flagship,
                full_price_entry, full_price_standard, full_price_flagship,
                is_selected
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s
            )
            RETURNING id
        """, (
            quote_id, session_token,
            cluster.get("cluster_name", ""),
            cluster.get("business_tag", ""),
            cluster.get("city_tag"),
            cluster.get("scenario_tag", "通用"),
            cluster.get("description", ""),
            len([kw for kw in cluster.get("core_keywords", []) if kw.get("is_selected", True)]),
            cluster.get("covered_keyword_count", 0),
            pricing.get("entry", {}).get("core_price", 0),
            pricing.get("standard", {}).get("core_price", 0),
            pricing.get("flagship", {}).get("core_price", 0),
            pricing.get("entry", {}).get("core_articles", 0),
            pricing.get("standard", {}).get("core_articles", 0),
            pricing.get("flagship", {}).get("core_articles", 0),
            pricing.get("entry", {}).get("full_price", 0),
            pricing.get("standard", {}).get("full_price", 0),
            pricing.get("flagship", {}).get("full_price", 0),
            cluster.get("is_selected", True),
        ))
        cluster_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return cluster_id
    finally:
        try:
            conn.close()
        except Exception: pass


def clear_draft_keywords(quote_id: int):
    """清除报价单的草稿关键词（重新生成报价时覆盖旧数据）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM confirmed_keywords WHERE quote_id = %s AND status = 'pending'", (quote_id,))
        conn.commit()
        conn.close()
    finally:
        try:
            conn.close()
        except Exception: pass


def get_keywords_by_quote(quote_id: int) -> list[dict]:
    """获取报价单关联的关键词"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM confirmed_keywords
            WHERE quote_id = %s
            ORDER BY created_at
        """, (quote_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


# ==========================================
#  关键词报价缓存（7天价格锁定）
# ==========================================

PRICE_CACHE_TTL_DAYS = 7


def _normalize_brand_for_cache(name: str) -> str:
    """[CTO-15.23 2026-05-11 Bug B] brand_name 归一化作为缓存 key

    去空格 + 转小写 · 防 "QZQZ美学定制" vs "QZQZ 美学定制" 当 2 个不同 brand 缓存 miss 风暴
    DB 层兜底 · 即使 caller 不归一也保证一致
    """
    if not name:
        return ""
    import re
    return re.sub(r"\s+", "", str(name).strip()).lower()


def get_cached_keyword_prices(
    brand_name: str,
    keywords: list[str],
    industry: str | None = None,
    city: str | None = None,
) -> dict[str, dict]:
    """
    查询7天内已报过价的关键词缓存（v1_2 全局共享版）

    命中优先级：
      1. 新 key: (industry, city, keyword) — 跨代理/跨客户/跨品牌共享，每词取最新 cached_at
      2. 老 key 兜底: (brand_name, keyword) — 兼容历史数据/老调用方

    [CTO-15.23 2026-05-11 Bug B] brand_name 在 DB 层自动归一化(去空格+小写)
    防 "QZQZ美学定制" vs "QZQZ 美学定制" 缓存 miss

    [CTO-15.23 2026-05-12 Codex round-3 P0 修] 老 fn 完全恢复 round-1 前样子
      原因:LLM cache 改独立表 keyword_price_cache_llm
      老调用方 0 改动 0 影响 · 代码 rollback 安全
      LLM 路径走 db.diagnosis_db.get_llm_cached_keyword_prices(新独立 fn)

    Returns:
        {keyword: {完整评分数据 + 三套餐价格}} 仅返回未过期的缓存
    """
    if not keywords:
        return {}

    # [Bug B] DB 层归一化兜底
    brand_name = _normalize_brand_for_cache(brand_name) or brand_name

    # [D3 2026-06-05] 公式版本失效:pricing_formula_version 为 NULL 或 != CURRENT 的缓存视为 expired
    #   覆盖全局 Step1 + brand Step2 两条读路径 · 防 A 品牌旧公式全局命中喂给 B 品牌污染新口径 · 灰度/回滚靠它
    #   首次上线:存量缓存 formula_version=NULL 全部失效 → 按新公式 lazy 重算(替代 TRUNCATE · 红线禁 TRUNCATE)
    try:
        from tools.pricing_bands import CURRENT_PRICING_FORMULA_VERSION as _FORMULA_VER
    except Exception:
        # 哨兵值保证 100% miss(fail-soft 永远成立):若用陈旧真实版本号 · 会命中旧口径缓存行喂出旧价(Workflow 抓)
        _FORMULA_VER = "__formula_ver_import_failed__"

    conn = get_connection()
    try:
        cursor = conn.cursor()

        placeholders = ','.join(['%s'] * len(keywords))
        rows = []

        # Step 1: 优先走 (industry, city, keyword) 全局查询（命中率更高）· 加 formula_version 失效
        if industry and city:
            cursor.execute(f"""
                SELECT DISTINCT ON (keyword) * FROM keyword_price_cache
                WHERE industry = %s AND city = %s
                  AND pricing_formula_version = %s
                  AND keyword IN ({placeholders})
                  AND expires_at > CURRENT_TIMESTAMP
                ORDER BY keyword, cached_at DESC
            """, [industry, city, _FORMULA_VER] + keywords)
            rows = cursor.fetchall()

        # Step 2: 未命中的 keywords 走老 (brand_name, keyword) 兜底 · 同样判 formula_version
        hit_keywords = {dict(r)['keyword'] for r in rows}
        missing = [kw for kw in keywords if kw not in hit_keywords]
        if missing:
            miss_placeholders = ','.join(['%s'] * len(missing))
            cursor.execute(f"""
                SELECT * FROM keyword_price_cache
                WHERE brand_name = %s
                  AND pricing_formula_version = %s
                  AND keyword IN ({miss_placeholders})
                  AND expires_at > CURRENT_TIMESTAMP
            """, [brand_name, _FORMULA_VER] + missing)
            rows = list(rows) + list(cursor.fetchall())

        conn.close()

        result = {}
        for row in rows:
            row = dict(row)
            # 同 keyword 若出现重复（全局命中 + brand 兜底都命中），以全局命中为准（Step 1 先进 list）
            if row['keyword'] in result:
                continue
            result[row['keyword']] = {
                'difficulty_score': row['difficulty_score'],
                'value_score': row['value_score'],
                'competitor_count': row['competitor_count'],
                'cost_per_article': row['cost_per_article'],
                'intent': row['intent'],
                'funnel_stage': row['funnel_stage'],
                'search_probability': row['search_probability'],
                'search_volume': row['search_volume'],
                'sem_price': row['sem_price'],
                'bidword_company_count': row['bidword_company_count'],
                'content_count': row['content_count'],
                'source_authority': json.loads(row['source_authority_json']) if row['source_authority_json'] else {},
                'recommended_platforms': json.loads(row['recommended_platforms_json']) if row['recommended_platforms_json'] else [],
                'data_source': row['data_source'],
                'markup_ratio': row['markup_ratio'],
                # [价格锁承诺 2026-08-05 · WO_PRICE_LOCK_PROMISE] 透出这行缓存的真实到期时刻。
                #   报价渲染层据此显示锁期 —— 锁期日期只能来自这里,不许渲染层自己 now()+7。
                'expires_at': row.get('expires_at'),
                'entry_price': row['entry_price'],
                'entry_articles': row['entry_articles'],
                'standard_price': row['standard_price'],
                'standard_articles': row['standard_articles'],
                'flagship_price': row['flagship_price'],
                'flagship_articles': row['flagship_articles'],
                'cached_at': row['cached_at'],
                # [D2/G2 2026-06-05] 算价底盘/复盘字段 · 尤其 effective_competition(让 recalculate_for_tier 命中真值不 fallback)
                'effective_competition': row.get('effective_competition'),
                'keyword_type': row.get('keyword_type'),
                'market_scope': row.get('market_scope'),
                'is_brand_keyword': row.get('is_brand_keyword'),
                'is_broad': row.get('is_broad'),
                'geo_multiplier': row.get('geo_multiplier'),
                'competition_band': row.get('competition_band'),
                'pricing_formula_version': row.get('pricing_formula_version'),
                'raw_price_before_band': row.get('raw_price_before_band'),
                'band_min': row.get('band_min'),
                'band_max': row.get('band_max'),
                'needs_review': row.get('needs_review'),
                'classify_confidence': row.get('classify_confidence'),
                'classify_reason': row.get('classify_reason'),
                'classify_source': row.get('classify_source'),
                # [§4.2] 超红海标随缓存读回 · 否则全局 7 天价格锁命中后丢标 → 重进保证价 + 达标计数(灾难)
                'super_red_ocean': row.get('super_red_ocean', False),
                'competition_ratio': row.get('competition_ratio', 0.0),
                # [v2.1 2026-06-11] LLM 评估师算价底盘(JSONB · true_competition/cost/media_tier/llm 双验/risk_flags/guards)
                'v2_assessor_data': (json.loads(row['v2_assessor_data']) if isinstance(row.get('v2_assessor_data'), str) else row.get('v2_assessor_data')) or {},
            }

        return result
    finally:
        try:
            conn.close()
        except Exception: pass


def save_keyword_prices_cache(
    brand_name: str,
    scored_keywords: list[dict],
    industry: str | None = None,
    city: str | None = None,
):
    """
    保存关键词报价缓存（UPSERT: 已存在则更新，不存在则插入）

    [CTO-15.23 2026-05-11 Bug B] brand_name 在 DB 层自动归一化
    跟 get_cached_keyword_prices 一致 · 读写同步

    [CTO-15.23 2026-05-12 Codex round-3 P0 修] 回退 round-1/round-2 加的所有改动
      原因:LLM cache 改独立表 keyword_price_cache_llm · 老表完全不动
      老 fn 完全恢复 round-1 前样子 · 老调用方 0 改动 0 影响
      代码 rollback 安全(老镜像 / 老 ON CONFLICT 2 列 · 跟 DB 一致)
      LLM 路径走 db.diagnosis_db.save_llm_keyword_prices_cache(新独立 fn)

    [价格锁承诺 2026-08-05 · WO_PRICE_LOCK_PROMISE]
      Returns: 本次写入用的 expires_at(ISO 字符串)· 供报价层显示**真实**锁期。
      写入逻辑逐位不变(纯 additive 返回值 · 老调用方忽略返回值 0 影响)。
      抛异常时不返回 → 调用方拿不到锁期 → 不承诺(fail-closed)。
    """
    # [Bug B] DB 层归一化兜底
    brand_name = _normalize_brand_for_cache(brand_name) or brand_name

    conn = get_connection()
    try:
        cursor = conn.cursor()

        expires_at = (datetime.now() + timedelta(days=PRICE_CACHE_TTL_DAYS)).isoformat()

        for kw in scored_keywords:
            source_auth_json = json.dumps(kw.get('source_authority', {}), ensure_ascii=False)
            platforms_json = json.dumps(kw.get('recommended_platforms', []), ensure_ascii=False)

            cursor.execute("""
                INSERT INTO keyword_price_cache (
                    brand_name, keyword,
                    industry, city,
                    difficulty_score, value_score, competitor_count, cost_per_article,
                    intent, funnel_stage, search_probability,
                    search_volume, sem_price, bidword_company_count, content_count,
                    source_authority_json, recommended_platforms_json, data_source, markup_ratio,
                    entry_price, entry_articles,
                    standard_price, standard_articles,
                    flagship_price, flagship_articles,
                    effective_competition, keyword_type, market_scope, is_brand_keyword, is_broad,
                    geo_multiplier, competition_band, pricing_formula_version,
                    raw_price_before_band, band_min, band_max, needs_review,
                    classify_confidence, classify_reason, classify_source,
                    super_red_ocean, competition_ratio,
                    v2_assessor_data,
                    cached_at, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, %s)
                ON CONFLICT(brand_name, keyword) DO UPDATE SET
                    industry=excluded.industry,
                    city=excluded.city,
                    difficulty_score=excluded.difficulty_score,
                    value_score=excluded.value_score,
                    competitor_count=excluded.competitor_count,
                    cost_per_article=excluded.cost_per_article,
                    intent=excluded.intent,
                    funnel_stage=excluded.funnel_stage,
                    search_probability=excluded.search_probability,
                    search_volume=excluded.search_volume,
                    sem_price=excluded.sem_price,
                    bidword_company_count=excluded.bidword_company_count,
                    content_count=excluded.content_count,
                    source_authority_json=excluded.source_authority_json,
                    recommended_platforms_json=excluded.recommended_platforms_json,
                    data_source=excluded.data_source,
                    markup_ratio=excluded.markup_ratio,
                    entry_price=excluded.entry_price,
                    entry_articles=excluded.entry_articles,
                    standard_price=excluded.standard_price,
                    standard_articles=excluded.standard_articles,
                    flagship_price=excluded.flagship_price,
                    flagship_articles=excluded.flagship_articles,
                    effective_competition=excluded.effective_competition,
                    keyword_type=excluded.keyword_type,
                    market_scope=excluded.market_scope,
                    is_brand_keyword=excluded.is_brand_keyword,
                    is_broad=excluded.is_broad,
                    geo_multiplier=excluded.geo_multiplier,
                    competition_band=excluded.competition_band,
                    pricing_formula_version=excluded.pricing_formula_version,
                    raw_price_before_band=excluded.raw_price_before_band,
                    band_min=excluded.band_min,
                    band_max=excluded.band_max,
                    needs_review=excluded.needs_review,
                    classify_confidence=excluded.classify_confidence,
                    classify_reason=excluded.classify_reason,
                    classify_source=excluded.classify_source,
                    super_red_ocean=excluded.super_red_ocean,
                    competition_ratio=excluded.competition_ratio,
                    v2_assessor_data=excluded.v2_assessor_data,
                    cached_at=CURRENT_TIMESTAMP,
                    expires_at=excluded.expires_at
            """, (
                brand_name, kw['keyword'],
                industry, city,
                kw.get('difficulty_score', 1.0),
                kw.get('value_score', 1.0),
                kw.get('competitor_count', 1),
                kw.get('cost_per_article', 60),
                kw.get('intent', 'informational'),
                kw.get('funnel_stage', 'awareness'),
                kw.get('search_probability', 0.5),
                kw.get('search_volume', 0),
                kw.get('sem_price', 0),
                kw.get('bidword_company_count', 0),
                kw.get('content_count', 0),
                source_auth_json,
                platforms_json,
                kw.get('data_source', 'omnirank_geo'),
                kw.get('markup_ratio', 1.0),  # [§4.5/决策5] 默认回成本(缓存基线)
                kw.get('entry_price', 0),
                kw.get('entry_articles', 0),
                kw.get('standard_price', 0),
                kw.get('standard_articles', 0),
                kw.get('flagship_price', 0),
                kw.get('flagship_articles', 0),
                # [D2/G2 2026-06-05] 算价底盘/复盘字段 · 落 effective_competition(非只 competitor_count)
                kw.get('effective_competition', kw.get('competitor_count', 1)),
                kw.get('keyword_type'),
                kw.get('market_scope'),
                kw.get('is_brand_keyword'),
                kw.get('is_broad'),
                kw.get('geo_multiplier'),
                kw.get('competition_band'),
                kw.get('pricing_formula_version'),
                kw.get('raw_price_before_band'),
                kw.get('band_min'),
                kw.get('band_max'),
                kw.get('needs_review'),
                kw.get('classify_confidence'),
                kw.get('classify_reason'),
                kw.get('classify_source'),
                kw.get('super_red_ocean', False),
                kw.get('competition_ratio', 0.0),
                # [v2.1 2026-06-11] LLM 评估师底盘 JSONB(true_competition/cost/media_tier/llm 双验/risk_flags/guards)
                # recalculate_for_tier 用 true_competition + cost_per_article 现算各档(SSOT 公式 · 不锁成品价)
                json.dumps(kw.get('v2_assessor_data') or {}, ensure_ascii=False),
                expires_at,
            ))

        conn.commit()
        conn.close()
        # [价格锁承诺 2026-08-05] commit 之后才返回 —— 返回值 = 「这批词真锁住了,锁到这个时刻」
        return expires_at
    finally:
        try:
            conn.close()
        except Exception: pass


def clear_keyword_prices_cache(brand_name: str) -> int:
    """Retired: deleting cache rows by a mutable display name is unsafe."""
    raise RuntimeError(
        "P1 QUOTE_CACHE_BRAND_ID_NAMESPACE_REQUIRED: "
        "keyword_price_cache cannot be cleared until its write path is fully keyed by brand_id"
    )


# =====================================================================
# LLM-first 报价 cache · 独立表 keyword_price_cache_llm
# =====================================================================
# [CTO-15.23 2026-05-12 Codex round-3 P0]
#   独立表防 migration 让代码 rollback 失效(老板拍板)
#   单品牌内幂等(UNIQUE brand_name + keyword + business_scope_hash)
#   跨品牌不共享(round-3 P1:Phase 2 brand-specific)
#   business_scope_hash 防同品牌不同 business_scope 串台

def _compute_business_scope_hash(business_scope: str | None) -> str:
    """规范化 business_scope · 计算稳定 16 字符 hash 作 cache key 一部分

    空 business_scope → 返回稳定常量 'no_scope'(让 cache 仍可命中)
    """
    import hashlib
    scope = (business_scope or "").strip().lower()
    if not scope:
        return "no_scope"
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]


def save_llm_keyword_prices_cache(
    brand_name: str,
    llm_keywords: list[dict],
    industry: str | None = None,
    city: str | None = None,
    business_scope: str | None = None,
):
    """保存 LLM-first 报价 cache 到独立表 keyword_price_cache_llm

    [CTO-15.23 2026-05-12 Codex round-3] 独立表方案 · 老 keyword_price_cache 0 触碰

    Args:
        brand_name: 品牌名(_normalize_brand_for_cache 归一化)
        llm_keywords: LLMPricedKeyword.model_dump() list · 字段 *_yuan / *_0_5
        industry: 行业(只存 · 不参与 cache key · Phase 2 brand-specific)
        city: 城市(只存 · 不参与 cache key)
        business_scope: 业务范围 · 算 hash 参与 cache key

    [价格锁承诺 2026-08-05 · WO_PRICE_LOCK_PROMISE]
      Returns: 本次写入用的 expires_at(ISO 字符串);空入参 → None。
      同 save_keyword_prices_cache:纯 additive 返回值,写入逻辑逐位不变。
    """
    if not llm_keywords:
        return None
    brand_name = _normalize_brand_for_cache(brand_name) or brand_name
    scope_hash = _compute_business_scope_hash(business_scope)

    conn = get_connection()
    try:
        cursor = conn.cursor()
        expires_at = (datetime.now() + timedelta(days=PRICE_CACHE_TTL_DAYS)).isoformat()
        try:  # [hardening① 2026-06-13] 落 assessor 版本(=报价公式版本)· 供读路径软失效
            from tools.pricing_bands import CURRENT_PRICING_FORMULA_VERSION as _AV
        except Exception:
            _AV = None

        for kw in llm_keywords:
            should_quote = bool(kw.get("should_quote", False))
            entry_y = int(kw.get("entry_price_yuan", -1))
            std_y = int(kw.get("standard_price_yuan", -1))
            flag_y = int(kw.get("flagship_price_yuan", -1))

            cursor.execute("""
                INSERT INTO keyword_price_cache_llm (
                    brand_name, keyword,
                    industry, city, business_scope_hash,
                    intent, funnel_stage, value_score,
                    entry_price, standard_price, flagship_price,
                    should_quote, llm_reason_zh, business_line,
                    needs_review, review_reason, assessor_version,
                    cached_at, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, %s)
                ON CONFLICT (brand_name, keyword, business_scope_hash) DO UPDATE SET
                    industry = excluded.industry,
                    city = excluded.city,
                    intent = excluded.intent,
                    funnel_stage = excluded.funnel_stage,
                    value_score = excluded.value_score,
                    entry_price = excluded.entry_price,
                    standard_price = excluded.standard_price,
                    flagship_price = excluded.flagship_price,
                    should_quote = excluded.should_quote,
                    llm_reason_zh = excluded.llm_reason_zh,
                    business_line = excluded.business_line,
                    needs_review = excluded.needs_review,
                    review_reason = excluded.review_reason,
                    assessor_version = excluded.assessor_version,
                    cached_at = CURRENT_TIMESTAMP,
                    expires_at = excluded.expires_at
            """, (
                brand_name, kw["keyword"],
                industry, city, scope_hash,
                kw.get("intent", "informational"),
                kw.get("funnel", kw.get("funnel_stage", "awareness")),
                float(kw.get("value_score_0_5", 0.0)),
                entry_y, std_y, flag_y,
                should_quote,
                kw.get("reason_zh", kw.get("llm_reason_zh", "")),
                kw.get("business_line", ""),
                bool(kw.get("needs_review", False)),
                kw.get("review_reason", ""),
                _AV,
                expires_at,
            ))
        conn.commit()
        conn.close()
        # [价格锁承诺 2026-08-05] commit 之后才返回(同老表口径)
        return expires_at
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_llm_cached_keyword_prices(
    brand_name: str,
    keywords: list[str],
    business_scope: str | None = None,
) -> dict[str, dict]:
    """查 LLM-first cache · brand-specific(round-3 P1 · 不走跨品牌全局)

    Returns: {keyword: dict with LLMPricedKeyword fields} 仅未过期 + 同 brand + 同 business_scope_hash
    """
    if not keywords:
        return {}
    brand_name = _normalize_brand_for_cache(brand_name) or brand_name
    scope_hash = _compute_business_scope_hash(business_scope)

    conn = get_connection()
    try:
        cursor = conn.cursor()
        placeholders = ','.join(['%s'] * len(keywords))
        try:  # [hardening① 2026-06-13] 软失效:NULL(旧行 graceful)或当前版本才命中;旧非空版本 → miss 重评
            from tools.pricing_bands import CURRENT_PRICING_FORMULA_VERSION as _AV
        except Exception:
            _AV = None
        cursor.execute(f"""
            SELECT * FROM keyword_price_cache_llm
            WHERE brand_name = %s
              AND business_scope_hash = %s
              AND (assessor_version IS NULL OR assessor_version = %s)
              AND keyword IN ({placeholders})
              AND expires_at > CURRENT_TIMESTAMP
        """, [brand_name, scope_hash, _AV] + keywords)
        rows = cursor.fetchall()
        conn.close()

        result: dict[str, dict] = {}
        for row in rows:
            row = dict(row)
            result[row["keyword"]] = {
                "intent": row.get("intent") or "informational",
                "funnel_stage": row.get("funnel_stage") or "awareness",
                "value_score": row.get("value_score") or 0.0,
                "entry_price": row.get("entry_price"),
                "standard_price": row.get("standard_price"),
                "flagship_price": row.get("flagship_price"),
                "should_quote": bool(row.get("should_quote")) if row.get("should_quote") is not None else True,
                "llm_reason_zh": row.get("llm_reason_zh") or "",
                "business_line": row.get("business_line") or "",
                "needs_review": bool(row.get("needs_review") or False),
                "review_reason": row.get("review_reason") or "",
                "industry": row.get("industry") or "",
                "city": row.get("city") or "",
                "cached_at": row.get("cached_at"),
                # [价格锁承诺 2026-08-05 · WO_PRICE_LOCK_PROMISE] 同老表:透出真实到期时刻供报价层显示锁期
                "expires_at": row.get("expires_at"),
            }
        return result
    finally:
        try:
            conn.close()
        except Exception:
            pass


def clear_llm_keyword_prices_cache(brand_name: str, business_scope: str | None = None) -> int:
    """Retired: deleting LLM cache rows by a mutable display name is unsafe."""
    raise RuntimeError(
        "P1 QUOTE_CACHE_BRAND_ID_NAMESPACE_REQUIRED: "
        "keyword_price_cache_llm cannot be cleared until its write path is fully keyed by brand_id"
    )


def save_topics(quote_id: int, topics: list[dict]) -> list[int]:
    """
    批量保存选题
    
    Args:
        quote_id: 报价单ID
        topics: 选题列表 [{"keyword_id": ..., "original_keyword": ..., "optimized_title": ...}]
        
    Returns:
        选题ID列表
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT q.article_plan_writing_mode,COALESCE(q.owner_user_id,b.owner_user_id) AS owner_user_id "
            "FROM quotes q JOIN brands b ON b.id=q.brand_id WHERE q.id=%s FOR UPDATE OF q",
            (quote_id,),
        )
        quote_plan = cursor.fetchone() or {}
        slot_aware = quote_plan.get("article_plan_writing_mode") == "slot_aware_v1"
        plan_actor = int(quote_plan.get("owner_user_id") or 0)
    
        topic_ids = []
        for topic in topics:
            from writing.article_style_contract import resolve_new_generation_style

            article_style = resolve_new_generation_style(topic.get("article_style")) or "buying_guide"
            cursor.execute("""
                INSERT INTO topics (
                    keyword_id, quote_id, original_keyword,
                    optimized_title, article_style, status
                ) VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                topic.get("keyword_id"),
                quote_id,
                topic.get("original_keyword"),
                topic.get("optimized_title"),
                article_style,
                "draft"
            ))
            topic_id = int(cursor.fetchone()["id"])
            if slot_aware:
                if not plan_actor:
                    raise RuntimeError("slot_aware_quote_owner_missing")
                from services.article_closed_loop_metadata import bind_topic_to_slot

                bind_topic_to_slot(
                    cursor,
                    quote_id=quote_id,
                    topic_id=topic_id,
                    keyword_id=topic.get("keyword_id"),
                    actor_user_id=plan_actor,
                )
            topic_ids.append(topic_id)

        conn.commit()
        conn.close()
        return topic_ids
    finally:
        try:
            conn.close()
        except Exception: pass


def get_topics_by_quote(quote_id: int) -> list[dict]:
    """获取报价单关联的选题"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT t.*, ck.keyword as keyword_text
            FROM topics t
            LEFT JOIN confirmed_keywords ck ON t.keyword_id = ck.id
            WHERE t.quote_id = %s
            ORDER BY t.created_at
        """, (quote_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def update_topic_status(topic_id: int, status: str, article_id: int = None) -> bool:
    """更新选题状态"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        if status == "confirmed":
            cursor.execute("""
                UPDATE topics SET status = %s, confirmed_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (status, topic_id))
        elif article_id:
            cursor.execute("""
                UPDATE topics SET status = %s, article_id = %s
                WHERE id = %s
            """, (status, article_id, topic_id))
        else:
            cursor.execute("""
                UPDATE topics SET status = %s WHERE id = %s
            """, (status, topic_id))

        conn.commit()
        conn.close()
        return True
    finally:
        try:
            conn.close()
        except Exception: pass


def release_topic_writing(topic_id: int) -> bool:
    """[写作卡死根治 2026-06-08] 把卡在 writing 但还没出稿的选题放回 draft。

    护栏:仅 status='writing' AND article_id IS NULL 才释放(对齐 social release_task_writing 的
    'AND status=writing' + 'script_id IS NULL')· 防重复释放 + 防迟到失败覆盖已成稿。
    纯状态回退 · 写作是 charge_on_success(未启动=无 freeze)· 不涉及退费 · billing.py 0 碰。
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE topics SET status='draft', writing_started_at=NULL "
            "WHERE id=%s AND status='writing' AND article_id IS NULL",
            (topic_id,),
        )
        released = cursor.rowcount > 0
        conn.commit()
        return released
    finally:
        try:
            conn.close()
        except Exception: pass


def release_topics_writing_on_failure(topic_ids, quote_id: int = None) -> int:
    """批量释放(写作入口 commit→thread.start 窗口失败时调)· 逐条 best-effort。

    释放后若该 quote 已无 writing topics · 回退 quotes.writing_status 防项目态不一致
    (口径对齐线程内失败逻辑的 'titles_ready')。
    """
    released = 0
    for tid in (topic_ids or []):
        try:
            if release_topic_writing(tid):
                released += 1
        except Exception:
            pass
    if quote_id is not None and released > 0:
        try:
            conn = get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) AS cnt FROM topics WHERE quote_id=%s AND status='writing'",
                    (quote_id,),
                )
                row = cursor.fetchone()
                remaining = (row["cnt"] if row else 0) or 0
                if remaining == 0:
                    cursor.execute(
                        "UPDATE quotes SET writing_status='titles_ready' "
                        "WHERE id=%s AND writing_status='writing'",
                        (quote_id,),
                    )
                conn.commit()
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception:
            pass
    return released


def mark_topics_write_timeout(topic_ids, quote_id: int = None) -> int:
    """[写作卡死根治 2026-06-08 · 方案 B] 把"已扣费但写作未出稿"的卡死选题标 write_timeout。

    与回退 draft 的区别:write_timeout 不在 start-articles 的可接受状态白名单内
    (status IN draft/pending/titles_ready/failed),故不会被用户重选 → 防二次扣费(双扣)。
    钱待 V3.5 精确退款资金批处理。护栏 status='writing' AND article_id IS NULL(不碰已成稿)。
    保留 writing_started_at 供排查/退款批定位。释放后回退 quotes.writing_status 防项目态不一致。
    """
    marked = 0
    for tid in (topic_ids or []):
        try:
            conn = get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE topics SET status='write_timeout' "
                    "WHERE id=%s AND status='writing' AND article_id IS NULL",
                    (tid,),
                )
                if cursor.rowcount > 0:
                    marked += 1
                conn.commit()
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception:
            pass
    if quote_id is not None and marked > 0:
        try:
            conn = get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE quotes SET writing_status='titles_ready' "
                    "WHERE id=%s AND writing_status='writing' "
                    "AND NOT EXISTS (SELECT 1 FROM topics WHERE quote_id=%s AND status='writing')",
                    (quote_id, quote_id),
                )
                conn.commit()
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception:
            pass
    return marked


# ========== 写作大厅相关函数 ==========

def get_writing_projects(status: str = None, allowed_brand_ids: list = None,
                         brand_id: int = None) -> list[dict]:
    """获取写作大厅项目列表——按 quote 展示，列表数字与详情页同源。

    brand_id: 只看这一个客户的项目。**可选,不传 = 原行为(该账号可见的全部)**。
        🔴 2026-08-03 加(SSOT《GEO 内容生产是一条流水线》铁律 1:客户是唯一作用域)。
           在此之前左上角选谁,写作大厅列表一个字都不会变 —— 因为这里根本
           没有按客户过滤的能力,前端 effect 依赖 currentBrandId 重新拉的
           是同一份全量。
        🔴 它**不替代** allowed_brand_ids:那个是 RBAC(能不能看),
           这个是当前视角(想不想看)。两者是 AND,顺序不影响结果但语义不同,
           越权绝不能靠"没选这个客户"来防。
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # B2.1 (CTO-15.9 session 3 · 2026-04-25 · M1a 主链断点 1)
        # 老 bug:仅 'confirmed' · offline-mark-paid 改 'paid' 后 quote 从写作大厅消失
        # 修法:'confirmed' OR 'paid' 都纳入(支付前签约 / 支付后服务期)· 不漏 paid quote
        query = """
            WITH quote_rollup AS (
                SELECT q.id,
                       q.brand_id,
                       q.brand_name,
                       q.writing_status,
                       q.confirmed_at,
                       q.created_at,
                       COALESCE(b.industry, '') as industry,
                       COALESCE(kw.keyword_count, 0) as keyword_count,
                       COALESCE(kw.total_required_articles, 0) as total_required_articles,
                       -- [CTO-15.23 2026-05-28 fix] 写作大厅卡片金额端口接错修
                       -- 老:monthly_price 优先 → 显示 GEO 套餐月费(如 ¥41,172)· 跟详情页关键词价累加(如 ¥4,407)打架
                       -- 修:paid_amount(实付锁定)> keyword_price SUM(详情页对齐)> monthly_price(老数据兜底)
                       -- 字段名沿用 monthly_price 不破前端 binding · 语义改为"项目总额"(卡片专用)
                       COALESCE(
                           NULLIF(q.paid_amount, 0),
                           NULLIF(kw.keyword_price, 0),
                           NULLIF(q.monthly_price, 0),
                           0
                       ) as monthly_price
                FROM quotes q
                LEFT JOIN brands b ON b.id = q.brand_id
                LEFT JOIN LATERAL (
                    SELECT COUNT(*) as keyword_count,
                           COALESCE(SUM(COALESCE(required_articles, 1)), 0) as total_required_articles,
                           COALESCE(SUM(COALESCE(NULLIF(final_price, 0), NULLIF(selling_price, 0), NULLIF(base_price, 0), 0)), 0) as keyword_price
                    FROM confirmed_keywords ck
                    WHERE ck.quote_id = q.id
                      AND (ck.is_core IS NOT FALSE)
                ) kw ON TRUE
                WHERE q.status IN ('confirmed', 'paid')
                  AND (q.writing_status IS NULL OR q.writing_status != 'deleted')
        """
        # P1.4.2 (CTO-15.23 2026-05-04) · 过滤软删除的写作项目
        params = []

        # 用户品牌隔离
        if allowed_brand_ids is not None:
            if not allowed_brand_ids or allowed_brand_ids == [-1]:
                query += " AND FALSE"
            else:
                query += " AND q.brand_id IN (" + ",".join(["%s"] * len(allowed_brand_ids)) + ")"
                params.extend(allowed_brand_ids)

        # 当前视角:只看这一个客户。与 RBAC 是 AND —— 上面那道才是权限闸。
        if brand_id is not None:
            query += " AND q.brand_id = %s"
            params.append(int(brand_id))

        if status and status != 'all':
            if status == 'optimizing':
                # 筛选有优化选题的项目
                query += """ AND q.id IN (
                    SELECT DISTINCT t.quote_id FROM topics t
                    WHERE t.is_optimize = true
                )"""
            elif status == 'pending':
                query += " AND (q.writing_status = %s OR q.writing_status IS NULL)"
                params.append(status)
            else:
                query += " AND q.writing_status = %s"
                params.append(status)

        query += f"""
            )
            SELECT id,
                   brand_id,
                   brand_name,
                   industry,
                   ARRAY[id] as quote_ids,
                   COALESCE(writing_status, 'pending') as writing_status,
                   confirmed_at,
                   keyword_count,
                   total_required_articles,
                   monthly_price
            FROM quote_rollup
            ORDER BY
                CASE WHEN keyword_count > 0 THEN 0 ELSE 1 END,
                confirmed_at DESC NULLS LAST,
                created_at DESC NULLS LAST,
                id DESC
        """

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        # 返回格式兼容前端（id 用最适合进入写作的 quote_id，额外带 quote_ids 数组）
        result = []
        for row in rows:
            d = dict(row)
            d["id"] = d["quote_ids"][0]  # 主 quote_id
            d["keyword_count"] = int(d.get("keyword_count") or 0)
            d["total_required_articles"] = int(d.get("total_required_articles") or 0)
            d["monthly_price"] = float(d.get("monthly_price") or 0)
            result.append(d)
        return result
    finally:
        try:
            conn.close()
        except Exception: pass


def get_writing_project_detail(quote_id: int) -> dict:
    """获取写作项目详情,包含关键词和平台推荐"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        # 获取报价信息
        cursor.execute("SELECT * FROM quotes WHERE id = %s", (quote_id,))
        quote = cursor.fetchone()
        if not quote:
            conn.close()
            return None
    
        # 获取关键词列表（仅核心词 — 覆盖词不生产内容）
        cursor.execute("""
            SELECT ck.*, kc.cluster_name
            FROM confirmed_keywords ck
            LEFT JOIN keyword_clusters kc ON ck.cluster_id = kc.id
            WHERE ck.quote_id = %s AND (ck.is_core IS NOT FALSE)
            ORDER BY ck.final_price DESC
        """, (quote_id,))
        keywords = cursor.fetchall()

        # 获取已生成的选题（含文章版本号，用于标记重写）
        cursor.execute("""
            SELECT t.*,
                   a.version AS article_version,
                   a.updated_at AS article_updated_at,
                   a.style_family,
                   a.article_review_status,
                   a.article_human_review_status,
                   a.article_review,
                   a.evidence_manifest_hash,
                   a.quality_warning,
                   a.publication_profile,
                   a.platform_review
            FROM topics t
            LEFT JOIN articles a ON t.article_id = a.id
            WHERE t.quote_id = %s
            ORDER BY t.created_at
        """, (quote_id,))
        topic_rows = [dict(t) for t in cursor.fetchall()]

        # Publication eligibility is authoritative server output, not a client
        # reconstruction of machine/human review states or stale hashes.
        from services.article_review_gate import evaluate_publication_eligibility
        for topic in topic_rows:
            article_id = topic.get("article_id")
            if not article_id:
                topic["publication_eligible"] = False
                topic["publication_eligibility_reason"] = "article_not_generated"
                continue
            try:
                eligibility = evaluate_publication_eligibility(int(article_id), cursor=cursor)
            except Exception as exc:
                eligibility = {
                    "eligible": False,
                    "reason": "review_state_unavailable",
                    "message": f"文章审核状态暂不可用：{exc}",
                    # 判不出来就 fail-closed 当硬门(不可用 != 可发)。
                    "review_state": "not_run",
                    "advisory_state": "none",
                    "advisory_open_count": 0,
                    "publication_h0_state": "operator_hard",
                }
            topic["publication_eligible"] = bool(eligibility.get("eligible"))
            topic["publication_eligibility_reason"] = eligibility.get("reason")
            topic["publication_eligibility_message"] = eligibility.get("message")
            # [三态拆分 2026-07-31 · §4.1] 三个正交字段直传前端:没有它们,前端只能拿
            # eligible 一个 bool 去猜"是没审过还是被拦了",于是又会把"未审"渲染成
            # "审核通过"(这正是本单要消灭的那类假象)。
            topic["review_state"] = eligibility.get("review_state")
            topic["advisory_state"] = eligibility.get("advisory_state")
            topic["advisory_open_count"] = eligibility.get("advisory_open_count")
            topic["publication_h0_state"] = eligibility.get("publication_h0_state")

        # [工单 C-3 T2] findings 同类聚合视图(读取时计算·逐条结构原样保留,
        # 前端按类型卡渲染 + 一键修复本类;quality_warning 兼容不动)。
        # [span 级 AI 免费修复 2026-07-30 · §2.1] 同时给出「这一处能不能 AI 修」:
        # 医疗/法律/金融高风险缺的是**人工签发**不是措辞,前端据此不渲染 AI 修复
        # 按钮(端点用同一 repair_route 再拒一次)。
        from services.article_findings_aggregate import aggregate_article_findings
        from services.span_level_repair import article_ai_repair_blocked
        _quote_industry = str(dict(quote).get("industry") or "")
        for topic in topic_rows:
            _topic_title = str(topic.get("optimized_title") or topic.get("title") or "")
            try:
                topic["findings_aggregate"] = aggregate_article_findings(
                    topic.get("quality_warning"),
                    industry=_quote_industry,
                    title=_topic_title,
                )
            except Exception:
                topic["findings_aggregate"] = []
            try:
                topic["ai_repair_blocked"] = article_ai_repair_blocked(
                    _quote_industry, _topic_title,
                )
            except Exception:
                # fail-closed:判不出来就当高风险(宁可少给一个 AI 按钮,
                # 不可能因为异常反而放开高风险类)。
                topic["ai_repair_blocked"] = True

        conn.close()

        # ══════════════════════════════════════════════════════════════
        # [WO_225-c1 §8.4] 每词只读派生字段:**条**,不是槽
        # ══════════════════════════════════════════════════════════════
        # 🔴 `required_articles` 一个字都不改 —— 它是冻结在 `confirmed_keywords`
        #    上的授权**槽**数,全仓 32 个生产文件在读。改它等于改所有人的读数。
        #    这里新增的是**派生**读数,只有写作中心与图文顶栏消费。
        #
        # 🔴 两个数必须叫不同的名字(A 侧 §3b 文案锁):
        #      required_articles = 槽(对客户的合同分配)
        #      planned_posts_default = 条(按这单口径去发,默认打几条)
        #    同一屏上不许两个都叫「篇」。
        #
        # 🔴 `mixed` 口径没有**每词**的答案(mix 是整单的槽分布,不是逐词的),
        #    所以 mixed / 读不到口径 一律回落 k=1 = 老行为(planned == required)。
        #    回落必须落在「什么都没变」那一侧,不能凭空放大。
        try:
            from services.article_capacity_contract import CONSUMED_STATUSES
            from services.media_slot_conversion import default_posts_converter, quote_perspective
            _per = quote_perspective(quote_id)
            # 槽 -> 条只有一处(与 generate-titles 的 per_keyword_plan.slots 共用):mixed / None ⇒ 一条一槽
            _to_posts = default_posts_converter(quote_id)
            cursor.execute(
                "SELECT keyword_id, COUNT(*) AS cnt FROM topics "
                " WHERE quote_id = %s AND status = ANY(%s) GROUP BY keyword_id",
                (quote_id, list(CONSUMED_STATUSES)))
            _done = {r["keyword_id"]: int(r["cnt"] or 0)
                     for r in (cursor.fetchall() or []) if r is not None}
        except Exception as _wo225_exc:  # noqa: BLE001
            # 🔴 派生读数算不出来**不许**把整个写作大厅打掉(工单 §1.1.5 同一条纪律)。
            print("  [WO_225] 每词条数派生失败(回落老行为):%s" % (_wo225_exc,))
            _per, _to_posts, _done = None, None, {}
        _keyword_rows = []
        for _kw in keywords:
            _kwd = dict(_kw)
            # 🔴 复刻 `writing.keyword_topic_generator._required_article_count` 的老语义:
            #   NULL ⇒ 1(不是 0),显式 0 保留。派生字段和它必须逐字同一条规则,
            #   否则 k=1 的老单在 required_articles 为 NULL 的那一行从 1 篇变 0 篇。
            _raw_req = _kwd.get("required_articles")
            _slots = 1 if _raw_req is None else max(0, int(_raw_req))
            try:
                _kwd["planned_posts_default"] = _to_posts(_slots) if _to_posts else _slots
            except Exception:  # noqa: BLE001
                _kwd["planned_posts_default"] = _slots
            _kwd["posts_done"] = int(_done.get(_kwd.get("id")) or 0)
            _keyword_rows.append(_kwd)

        # ── [WO_225-c1'' · Review 2026-09-15] 项目级合计:服务端给,前端不 Σ ──
        # 🔴 合计 = **逐词值之和**,不是 `round_half_up(Σ槽 × bps / 10000)`。
        #    两者在 bps 非整数倍时会差:bps=45000、两个 7 槽的词 ⇒
        #    逐词 round_half_up(31.5)=32,和 = 64;而先求和再取整 = round(63.0) = 63。
        #    前端如果自己 `Σ round(...)`,它算的是前者;顶栏如果显示后者,
        #    **同一屏上两个数差 1**,而没有任何东西会报错。所以只有一条取整路径:
        #    逐词先取整,再相加,合计由服务端给出。
        # 🔴 `total_required_articles`(槽)一并给出,理由同上:
        #    `WritingHall.tsx:6781` 今天在前端 `reduce` 求 required_articles 之和,
        #    那是同一类病的另一半 —— 两个数都由服务端给,前端只渲染。
        #    名字仍然分得开:**槽**叫 total_required_articles,**条**叫
        #    total_planned_posts_default(A 侧 §3b 文案锁:同一屏不许都叫「篇」)。
        # 🔴 NULL ⇒ **1**,与同名字段在列表端点的口径逐字一致:
        #   `get_writing_projects` 用 `COALESCE(SUM(COALESCE(required_articles, 1)), 0)`。
        #   写成 `or 0` 会让同一个字段名在两个端点上给出两个数,
        #   而且与本函数自己的 `planned_posts_default`(已按 NULL⇒1)对不上 ——
        #   一个 required_articles 为 NULL 的词会变成「槽 0 条 1」。
        _total_slots = sum(
            (1 if k.get("required_articles") is None else max(0, int(k["required_articles"])))
            for k in _keyword_rows)
        _total_posts = sum(int(k.get("planned_posts_default") or 0) for k in _keyword_rows)

        return {
            "quote": dict(quote),
            "keywords": _keyword_rows,
            "topics": topic_rows,
            "total_required_articles": _total_slots,
            "total_planned_posts_default": _total_posts,
            # 口径给前端显示用(「按自媒体发布约需 N 条左右」)。
            # 🔴 不给 bps、不给桶内部名 —— 那两样连服务商页面都只在内部审核视图出现,
            #    客户面绝对不出(工单 §6 / 08_billing §12)。
            "delivery_perspective": _per,
        }
    finally:
        try:
            conn.close()
        except Exception: pass


def update_writing_status(quote_id: int, status: str) -> bool:
    """更新写作项目状态"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE quotes SET writing_status = %s WHERE id = %s
        """, (status, quote_id))
        conn.commit()
        conn.close()
        return True
    finally:
        try:
            conn.close()
        except Exception: pass


def save_topics_batch(quote_id: int, topics: list[dict], *, cursor=None,
                      only_keyword_ids=None, generation_request_id=None) -> list[int]:
    """批量保存选题（自动去重：先清除旧的draft标题，再插入新的）

    🔴 [Review 09-28] `only_keyword_ids`:只清这些词的 draft/pending/regenerating 旧标题。
       不给 = 老行为(整单清,即「整表重生成」)。generate-titles 带 per_keyword_plan 子集
       (「为新词生成标题」/ 单个新词「立即生成标题」)时必须给 —— 否则只给新词出题,
       却把别的词已付费、还没写的标题整单删掉(本机真浏览器实测:老词原有标题被删)。
    🔴 [WO_317 第五笔] `generation_request_id`:给了就盖在本批存下的选题上。
       「这个请求编号下有没有存下过选题」是接管被遗弃抢占的判据
       (title_batch_charge_registry.claim_batch);原来只有受理凭据带编号,
       成功存下的正式行不带 ⇒ 成功批次过了时限会被误判成「被遗弃」而免费重出。
    """
    conn = None if cursor is not None else get_connection()
    try:
        cursor = cursor or conn.cursor()

        # Existing enrolled quotes keep the compatibility adapter even after a
        # rollout flag is disabled.  Sending them back through destructive
        # legacy delete/reinsert would lose immutable delivery-slot identity.
        cursor.execute(
            "SELECT q.article_plan_writing_mode,COALESCE(q.owner_user_id,b.owner_user_id) AS owner_user_id "
            "FROM quotes q JOIN brands b ON b.id=q.brand_id WHERE q.id=%s FOR UPDATE OF q",
            (quote_id,),
        )
        _quote_plan_row = cursor.fetchone()
        _slot_aware = bool(_quote_plan_row and _quote_plan_row.get("article_plan_writing_mode") == "slot_aware_v1")
        _plan_actor = int((_quote_plan_row or {}).get("owner_user_id") or 0)

        # 1. 清除该项目下所有 draft/pending 状态的旧 topics（避免重复插入）
        if _slot_aware:
            deleted = 0
        elif only_keyword_ids is not None:
            cursor.execute("""
                DELETE FROM topics
                WHERE quote_id = %s AND status IN ('draft', 'pending', 'regenerating')
                  AND keyword_id = ANY(%s)
            """, (quote_id, [int(k) for k in only_keyword_ids]))
            deleted = cursor.rowcount
        else:
            cursor.execute("""
                DELETE FROM topics
                WHERE quote_id = %s AND status IN ('draft', 'pending', 'regenerating')
            """, (quote_id,))
            deleted = cursor.rowcount
        if deleted > 0:
            print(f"  [去重] 清除 quote_id={quote_id} 的 {deleted} 条旧标题")
    
        # 2. 内部去重（同一个 keyword_id + title 只保留一个）
        seen = set()
        unique_topics = []
        for topic in topics:
            key = (topic.get("keyword_id"), topic.get("optimized_title"))
            if key not in seen:
                seen.add(key)
                unique_topics.append(topic)
    
        if len(unique_topics) < len(topics):
            print(f"  [去重] 批次内去重：{len(topics)} → {len(unique_topics)}")

        if not unique_topics:
            cursor.execute("""
                UPDATE quotes SET writing_status = 'pending' WHERE id = %s
            """, (quote_id,))
            if conn is not None:
                conn.commit()
                conn.close()
            return []
    
        # 3. 插入新标题（含 cluster_id + v2.9 user_choice 持久化 + v2.10.2 user_choice_source 状态机)
        # [WO_225-c1 §8.3] 逐条分桶。桶决定「几条填满一个交付槽」,
        #   而默认生成条数也由同一个口径推出(§8.6)—— 两个数必须同源,
        #   否则 35 条写 NULL 桶 = 7 槽的单当场超 5 倍。单点见 plan_buckets 抬头。
        #   🔴 topic 自带 media_bucket(来自 per_keyword_plan)优先;读不到口径 ⇒ None = 老行为。
        from services.media_slot_conversion import plan_buckets
        _buckets = plan_buckets(quote_id, len(unique_topics), cursor=cursor)
        topic_ids = []
        _reused_topic_ids = []
        for _bidx, topic in enumerate(unique_topics):
            _bucket = topic.get("media_bucket") or (
                _buckets[_bidx] if _bidx < len(_buckets) else None)
            # v2.9:user_choice 从 batch 传入 · None=系统推荐(走 ratio)· 非 None 表示用户选了具体文体
            from writing.article_style_contract import normalize_user_choice
            _uc = normalize_user_choice(topic.get("user_choice"), allow_auto=False)
            from writing.article_style_contract import resolve_new_generation_style
            _article_style = resolve_new_generation_style(topic.get("article_style")) or "buying_guide"

            # v2.10.2 P1-6:user_choice_source 状态机全链路落地(Codex 二审 v2.10.1)
            # 来源标记:'manual' / 'batch_uniform' / 'batch_distribution' / NULL
            # 若 _uc 是 NULL · source 必须 NULL(防"auto + manual" 异常状态)
            _source = topic.get("user_choice_source")
            if _uc is None:
                _source = None  # 强制一致性
            elif _source not in ("manual", "batch_uniform", "batch_distribution"):
                _source = None  # 非法值兜底
            _existing_topic_id = None
            if _slot_aware:
                cursor.execute(
                    """
                    SELECT t.id
                      FROM geo_article_delivery_slots s
                      JOIN topics t ON t.id=s.topic_id
                     WHERE s.quote_id=%s AND s.keyword_id=%s AND s.current_state='active'
                       AND t.article_id IS NULL
                       AND t.status IN ('draft','pending','regenerating','failed')
                       AND t.id <> ALL(%s)
                     ORDER BY s.contract_ordinal LIMIT 1
                     FOR UPDATE OF s,t
                    """,
                    (quote_id, topic.get("keyword_id"), _reused_topic_ids),
                )
                _existing_topic = cursor.fetchone()
                _existing_topic_id = _existing_topic.get("id") if _existing_topic else None
            if _existing_topic_id:
                cursor.execute(
                    """
                    UPDATE topics
                       SET original_keyword=%s,optimized_title=%s,article_style=%s,status='draft',
                           cluster_id=%s,user_choice=%s,user_choice_source=%s,
                           generation_request_id=COALESCE(%s, generation_request_id)
                     WHERE id=%s AND quote_id=%s AND article_id IS NULL
                    RETURNING id
                    """,
                    (
                        topic.get("original_keyword"),
                        topic.get("optimized_title"),
                        _article_style,
                        topic.get("cluster_id"),
                        _uc,
                        _source,
                        generation_request_id,
                        _existing_topic_id,
                        quote_id,
                    ),
                )
                _saved_topic_id = cursor.fetchone()["id"]
            else:
                cursor.execute("""
                    INSERT INTO topics (
                        keyword_id, quote_id, original_keyword, optimized_title,
                        article_style, status, cluster_id, user_choice, user_choice_source,
                        media_bucket, generation_request_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    topic.get("keyword_id"),
                    quote_id,
                    topic.get("original_keyword"),
                    topic.get("optimized_title"),
                    _article_style,
                    "draft",
                    topic.get("cluster_id"),  # NULL for flat mode
                    _uc,  # v2.9 user_choice 持久化(None/10 项之一)
                    _source,  # v2.10.2 user_choice_source(NULL/manual/batch_uniform/batch_distribution)
                    _bucket,  # [WO_225-c1] 媒体桶;NULL = 一条一槽(老行为)
                    generation_request_id,  # [WO_317 第五笔] 不给 = NULL(老行为)
                ))
                _saved_topic_id = cursor.fetchone()["id"]
                if _slot_aware:
                    if not _plan_actor:
                        raise RuntimeError("slot_aware_quote_owner_missing")
                    from services.article_closed_loop_metadata import bind_topic_to_slot

                    bind_topic_to_slot(
                        cursor,
                        quote_id=quote_id,
                        topic_id=_saved_topic_id,
                        keyword_id=topic.get("keyword_id"),
                        actor_user_id=_plan_actor,
                    )
            _reused_topic_ids.append(_saved_topic_id)
            topic_ids.append(_saved_topic_id)

        # 更新写作状态
        cursor.execute("""
            UPDATE quotes SET writing_status = 'titles_ready' WHERE id = %s
        """, (quote_id,))
    
        if conn is not None:
            conn.commit()
            conn.close()
        return topic_ids
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception: pass


def update_topic_user_choice(topic_id: int, user_choice) -> bool:
    """v2.9 持久化用户选的文体(None/auto 存 NULL · 10 项之一存值)

    Args:
        topic_id: topic id
        user_choice: None/'auto'/'guide'/'comparison'/... 10 项之一(无 'company')
    Returns:
        bool · True=updated
    """
    from writing.article_style_contract import normalize_user_choice

    _uc = normalize_user_choice(user_choice, allow_auto=False)
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE topics SET user_choice = %s WHERE id = %s
        """, (_uc, topic_id))
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def update_topic_title(topic_id: int, new_title: str) -> bool:
    """更新选题标题"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE topics SET optimized_title = %s WHERE id = %s
        """, (new_title, topic_id))
        conn.commit()
        conn.close()
        return True
    finally:
        try:
            conn.close()
        except Exception: pass


def regenerate_topic(topic_id: int) -> bool:
    """标记选题需要重新生成"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        # [写作卡死根治 2026-06-08 · 纵深防双扣] write_timeout 选题不可重新生成洗回 pending 再扣费
        # (已扣费待退还算力 · 退还由资金批处理)· 与 start-articles 白名单口径对齐
        cursor.execute("""
            UPDATE topics SET
                status = 'regenerating',
                regenerate_count = regenerate_count + 1,
                regenerate_started_at = NOW()
            WHERE id = %s AND status <> 'write_timeout'
        """, (topic_id,))
        conn.commit()
        conn.close()
        return True
    finally:
        try:
            conn.close()
        except Exception: pass


# ========== 范文库相关函数 ==========

def save_reference_article(data: dict) -> int:
    """保存范文"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            INSERT INTO reference_articles (
                title, content, source_url, platform,
                industry, intent_type, analysis, success_proof
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            data.get("title"),
            data.get("content"),
            data.get("source_url"),
            data.get("platform"),
            data.get("industry"),
            data.get("intent_type"),
            json.dumps(data.get("analysis")) if data.get("analysis") else None,
            data.get("success_proof")
        ))

        ref_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return ref_id
    finally:
        try:
            conn.close()
        except Exception: pass


def get_reference_articles(
    industry: str = None, 
    intent_type: str = None,
    search: str = None,
    platform: str = None,
    time_range: str = None,
    min_usage: int = None
) -> list[dict]:
    """获取范文列表"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        query = "SELECT * FROM reference_articles WHERE status = 'active'"
        params = []
    
        # 综合搜索（标题/行业/平台模糊匹配）
        if search:
            query += " AND (title LIKE %s OR industry LIKE %s OR platform LIKE %s)"
            params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])

        # 平台筛选（精确匹配，如果单独使用）
        if platform:
            query += " AND platform = %s"
            params.append(platform)

        # 时间范围筛选
        if time_range:
            if time_range == "7d":
                query += " AND created_at >= NOW() - INTERVAL '7 days'"
            elif time_range == "30d":
                query += " AND created_at >= NOW() - INTERVAL '30 days'"
            elif time_range == "90d":
                query += " AND created_at >= NOW() - INTERVAL '90 days'"
            elif time_range == "180d":
                query += " AND created_at >= NOW() - INTERVAL '180 days'"
            elif time_range == "365d":
                query += " AND created_at >= NOW() - INTERVAL '365 days'"

        # 行业筛选
        if industry:
            query += " AND industry = %s"
            params.append(industry)

        # 意图类型筛选
        if intent_type:
            query += " AND intent_type = %s"
            params.append(intent_type)

        # 最低使用次数筛选
        if min_usage is not None:
            query += " AND use_count >= %s"
            params.append(min_usage)
    
        query += " ORDER BY use_count DESC, created_at DESC"
    
        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
    
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def get_reference_article(ref_id: int) -> dict:
    """获取单个范文详情"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("SELECT * FROM reference_articles WHERE id = %s", (ref_id,))
        row = cursor.fetchone()
        conn.close()
    
        if row:
            result = dict(row)
            if result.get("analysis"):
                result["analysis"] = json.loads(result["analysis"])
            return result
        return None
    finally:
        try:
            conn.close()
        except Exception: pass


def update_reference_use_count(ref_id: int) -> bool:
    """增加范文使用次数"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE reference_articles SET use_count = use_count + 1 WHERE id = %s
        """, (ref_id,))
        conn.commit()
        conn.close()
        return True
    finally:
        try:
            conn.close()
        except Exception: pass


def save_imitation_record(ref_id: int, quote_id: int, count: int) -> int:
    """保存仿写记录"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            INSERT INTO imitation_records (reference_id, quote_id, generated_count)
            VALUES (%s, %s, %s)
            RETURNING id
        """, (ref_id, quote_id, count))

        record_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return record_id
    finally:
        try:
            conn.close()
        except Exception: pass


def update_reference_article(ref_id: int, data: dict) -> bool:
    """更新范文"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        # 构建动态更新语句
        update_fields = []
        params = []
    
        field_mapping = {
            "title": "title",
            "content": "content",
            "source_url": "source_url",
            "platform": "platform",
            "industry": "industry",
            "intent_type": "intent_type",
            "success_proof": "success_proof"
        }
    
        for key, column in field_mapping.items():
            if key in data:
                update_fields.append(f"{column} = %s")
                params.append(data[key])

        # 处理analysis字段（需要JSON序列化）
        if "analysis" in data:
            update_fields.append("analysis = %s")
            params.append(json.dumps(data["analysis"]) if data["analysis"] else None)

        if not update_fields:
            conn.close()
            return False

        update_fields.append("updated_at = CURRENT_TIMESTAMP")
        params.append(ref_id)

        query = f"UPDATE reference_articles SET {', '.join(update_fields)} WHERE id = %s"
        cursor.execute(query, params)
    
        conn.commit()
        conn.close()
        return cursor.rowcount > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def delete_reference_article(ref_id: int) -> bool:
    """软删除范文（将status设为deleted）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            UPDATE reference_articles SET status = 'deleted', updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (ref_id,))
    
        conn.commit()
        success = cursor.rowcount > 0
        conn.close()
        return success
    finally:
        try:
            conn.close()
        except Exception: pass


def get_imitation_records_by_ref(ref_id: int) -> list[dict]:
    """获取范文的仿写记录列表"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            SELECT ir.*, q.brand_name, q.industry
            FROM imitation_records ir
            LEFT JOIN quotes q ON ir.quote_id = q.id
            WHERE ir.reference_id = %s
            ORDER BY ir.created_at DESC
        """, (ref_id,))
    
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def save_imitated_articles(record_id: int, ref_id: int, quote_id: int, articles: list[dict]) -> list[int]:
    """
    保存仿写生成的文章
    
    Args:
        record_id: 仿写记录ID
        ref_id: 范文ID  
        quote_id: 报价单ID
        articles: 文章列表 [{"keyword": ..., "title": ..., "content": ...}]
        
    Returns:
        文章ID列表
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        article_ids = []
        for article in articles:
            content = article.get("content", "")
            word_count = len(content) if content else 0
        
            cursor.execute("""
                INSERT INTO imitated_articles (
                    imitation_record_id, reference_id, quote_id,
                    keyword, title, content, word_count, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                record_id,
                ref_id,
                quote_id,
                article.get("keyword"),
                article.get("title"),
                content,
                word_count,
                "draft"
            ))
            article_ids.append(cursor.fetchone()["id"])
    
        conn.commit()
        conn.close()
        return article_ids
    finally:
        try:
            conn.close()
        except Exception: pass


def get_imitated_articles(record_id: int) -> list[dict]:
    """获取仿写记录关联的文章列表"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            SELECT * FROM imitated_articles
            WHERE imitation_record_id = %s
            ORDER BY created_at
        """, (record_id,))
    
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def get_imitated_article(article_id: int) -> dict:
    """获取单篇仿写文章详情"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("SELECT * FROM imitated_articles WHERE id = %s", (article_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception: pass


def update_imitated_article(article_id: int, data: dict) -> bool:
    """更新仿写文章"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        update_fields = []
        params = []
    
        if "title" in data:
            update_fields.append("title = %s")
            params.append(data["title"])
        if "content" in data:
            update_fields.append("content = %s")
            update_fields.append("word_count = %s")
            params.append(data["content"])
            params.append(len(data["content"]))
        if "status" in data:
            update_fields.append("status = %s")
            params.append(data["status"])
        if "revision_notes" in data:
            update_fields.append("revision_notes = %s")
            params.append(json.dumps(data["revision_notes"], ensure_ascii=False) if data["revision_notes"] else None)

        if not update_fields:
            conn.close()
            return False

        update_fields.append("updated_at = CURRENT_TIMESTAMP")
        params.append(article_id)

        query = f"UPDATE imitated_articles SET {', '.join(update_fields)} WHERE id = %s"
        cursor.execute(query, params)
    
        conn.commit()
        conn.close()
        return cursor.rowcount > 0
    finally:
        try:
            conn.close()
        except Exception: pass


# ============================================
# AI员工任务相关函数
# ============================================

def save_employee_task(
    employee_id: str,
    task_content: str,
    employee_name: str = None,
    task_type: str = "single",
    context: dict = None,
    status: str = "pending",
) -> int:
    """保存员工任务"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            INSERT INTO employee_tasks
            (employee_id, employee_name, task_type, task_content, context, status)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            employee_id,
            employee_name,
            task_type,
            task_content,
            json.dumps(context, ensure_ascii=False) if context else None,
            status,
        ))

        task_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return task_id
    finally:
        try:
            conn.close()
        except Exception: pass


def update_employee_task(
    task_id: int,
    status: str = None,
    result: str = None,
    skills_used: list = None,
    execution_time: float = None,
    error_message: str = None,
) -> bool:
    """更新员工任务状态"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        update_fields = []
        params = []
    
        if status:
            update_fields.append("status = %s")
            params.append(status)
            if status in ("completed", "failed"):
                update_fields.append("completed_at = CURRENT_TIMESTAMP")

        if result:
            update_fields.append("result = %s")
            params.append(result)

        if skills_used is not None:
            update_fields.append("skills_used = %s")
            params.append(json.dumps(skills_used, ensure_ascii=False))

        if execution_time is not None:
            update_fields.append("execution_time = %s")
            params.append(execution_time)

        if error_message:
            update_fields.append("error_message = %s")
            params.append(error_message)

        if not update_fields:
            return False

        params.append(task_id)
        query = f"UPDATE employee_tasks SET {', '.join(update_fields)} WHERE id = %s"
        cursor.execute(query, params)
    
        conn.commit()
        conn.close()
        return cursor.rowcount > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def get_employee_tasks(
    employee_id: str = None,
    status: str = None,
    limit: int = 20,
) -> List[dict]:
    """获取员工任务列表"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        query = "SELECT * FROM employee_tasks WHERE 1=1"
        params = []

        if employee_id:
            query += " AND employee_id = %s"
            params.append(employee_id)

        if status:
            query += " AND status = %s"
            params.append(status)

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


def get_employee_task(task_id: int) -> dict:
    """获取单个任务详情"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("SELECT * FROM employee_tasks WHERE id = %s", (task_id,))
        row = cursor.fetchone()
        conn.close()
    
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception: pass


# ============================================
# 部门管理函数
# ============================================

def init_default_departments():
    """初始化默认部门"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        default_departments = [
            ("leadership", "领导层", "👔", "负责战略规划、任务分配和团队协调", 0),
            ("diagnosis", "诊断部", "📊", "负责品牌GEO诊断分析", 1),
            ("content", "内容部", "✍️", "负责内容创作和选题策划", 2),
            ("support", "支持部", "📑", "负责报告交付和质量审核", 3),
        ]
    
        for dept_id, name, icon, desc, order in default_departments:
            cursor.execute("""
                INSERT INTO departments (id, name, icon, description, sort_order)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
            """, (dept_id, name, icon, desc, order))
    
        conn.commit()
        conn.close()
    finally:
        try:
            conn.close()
        except Exception: pass


def get_departments(include_inactive: bool = False) -> List[dict]:
    """获取所有部门"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        query = "SELECT * FROM departments"
        if not include_inactive:
            query += " WHERE is_active = 1"
        query += " ORDER BY sort_order"
    
        cursor.execute(query)
        rows = cursor.fetchall()
        conn.close()
    
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def get_department(dept_id: str) -> Optional[dict]:
    """获取单个部门"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("SELECT * FROM departments WHERE id = %s", (dept_id,))
        row = cursor.fetchone()
        conn.close()
    
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception: pass


def save_department(
    dept_id: str,
    name: str,
    icon: str = "📁",
    description: str = None,
    sort_order: int = 0,
    is_active: bool = True,
) -> bool:
    """保存或更新部门"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            INSERT INTO departments (id, name, icon, description, sort_order, is_active, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                icon = excluded.icon,
                description = excluded.description,
                sort_order = excluded.sort_order,
                is_active = excluded.is_active,
                updated_at = CURRENT_TIMESTAMP
        """, (dept_id, name, icon, description, sort_order, int(is_active)))

        conn.commit()
        conn.close()
        return True
    finally:
        try:
            conn.close()
        except Exception: pass


def delete_department(dept_id: str) -> bool:
    """删除部门（软删除）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("UPDATE departments SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (dept_id,))
    
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


# ============================================
# 员工配置管理函数
# ============================================

def init_default_employees():
    """初始化默认员工配置"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        # 检查是否已有员工配置
        cursor.execute("SELECT COUNT(*) AS cnt FROM employee_configs")
        count = cursor.fetchone()["cnt"]
    
        if count > 0:
            conn.close()
            return  # 已有配置，跳过初始化
    
        # 默认员工配置
        default_employees = [
            # 领导层
            {
                "id": "project_director",
                "name": "项目负责人",
                "department_id": "leadership",
                "avatar": "👔",
                "description": "团队的最高领导人和战略军师，负责会议主持、任务分配、进度跟踪和决策支持",
                "model_id": "deepseek-v4-pro",
                "temperature": 0.7,
                "skills": '["meeting_host", "task_assignment", "progress_tracking", "strategic_planning", "summary_reporting", "decision_support", "employee_coordination"]',
                "memory_type": "persistent",
            },
            # 诊断部
            {
                "id": "data_collector",
                "name": "数据采集员",
                "department_id": "diagnosis",
                "avatar": "📡",
                "description": "负责全网内容采集，包括抖音视频、小红书笔记、网页内容",
                "model_id": "deepseek-v4-flash",
                "temperature": 0.5,
                "skills": '["douyin_search", "xhs_search", "web_search"]',
                "mcp_config": '{"metaso": true}',
            },
            {
                "id": "ai_tester",
                "name": "AI测试员",
                "department_id": "diagnosis",
                "avatar": "🤖",
                "description": "负责AI可见度测试，检测品牌在各AI引擎中的表现",
                "model_id": "deepseek-v4-flash",
                "temperature": 0.3,
                "skills": '["ai_visibility_test", "citation_analysis"]',
            },
            {
                "id": "competitor_analyst",
                "name": "竞品分析师",
                "department_id": "diagnosis",
                "avatar": "📈",
                "description": "负责竞品识别、差距对比、爆款拆解",
                "model_id": "deepseek-v4-flash",
                "temperature": 0.5,
                "skills": '["competitor_identification", "gap_analysis", "viral_content_analysis"]',
            },
            {
                "id": "report_writer",
                "name": "报告撰稿人",
                "department_id": "diagnosis",
                "avatar": "📋",
                "description": "负责GEO诊断报告撰写和评分",
                "model_id": "deepseek-v4-pro",
                "temperature": 0.5,
                "skills": '["geo_scoring", "report_generation"]',
            },
            # 内容部
            {
                "id": "content_planner",
                "name": "内容策划师",
                "department_id": "content",
                "avatar": "🎯",
                "description": "负责选题规划、标题生成、热点挖掘",
                "model_id": "qwen3.7-max",
                "temperature": 0.7,
                "skills": '["topic_planning", "title_generation", "trend_mining"]',
                "memory_type": "persistent",
            },
            {
                "id": "content_writer",
                "name": "正文撰稿人",
                "department_id": "content",
                "avatar": "✍️",
                "description": "负责长文撰写、AI改写、去AI味",
                "model_id": "deepseek-v4-pro",
                "temperature": 0.7,
                "skills": '["article_writing", "content_rewriting", "deai_optimization"]',
            },
            {
                "id": "douyin_creator",
                "name": "抖音内容师",
                "department_id": "content",
                "avatar": "📱",
                "description": "负责抖音话术、钩子设计、脚本撰写",
                "model_id": "doubao-seed-1-8",
                "temperature": 0.8,
                "skills": '["hook_design", "script_writing", "douyin_copywriting"]',
            },
            {
                "id": "xhs_creator",
                "name": "小红书内容师",
                "department_id": "content",
                "avatar": "📕",
                "description": "负责种草文案、图文排版、标签策略",
                "model_id": "qwen3.7-max",
                "temperature": 0.7,
                "skills": '["seeding_copywriting", "cover_design", "hashtag_strategy"]',
            },
            # 支持部
            {
                "id": "ppt_specialist",
                "name": "PPT专员",
                "department_id": "support",
                "avatar": "📊",
                "description": "负责PPT生成、模板设计",
                "model_id": "deepseek-v4-pro",
                "temperature": 0.5,
                "skills": '["ppt_generation", "template_design"]',
            },
            {
                "id": "chief_editor",
                "name": "首席编辑",
                "department_id": "support",
                "avatar": "✏️",
                "description": "负责质量审核、语言统一、合规检查",
                "model_id": "deepseek-reasoner",
                "temperature": 0.3,
                "skills": '["quality_review", "style_unification", "compliance_check"]',
            },
            {
                "id": "industry_expert",
                "name": "行业专家",
                "department_id": "support",
                "avatar": "🧠",
                "description": "负责行业洞察、知识检索",
                "model_id": "qwen3.7-max",
                "temperature": 0.5,
                "skills": '["industry_insight", "knowledge_retrieval", "expert_consultation"]',
                "memory_type": "persistent",
            },
            {
                "id": "legal_advisor",
                "name": "法务专员",
                "department_id": "support",
                "avatar": "⚖️",
                "description": "负责合同起草、法律咨询、法规查询（联网）、风险评估",
                "model_id": "qwen3.7-max",
                "temperature": 0.3,
                "skills": '["contract_drafting", "legal_consultation", "law_search", "risk_assessment"]',
                "memory_type": "persistent",
            },
        ]
    
        for emp in default_employees:
            cursor.execute("""
                INSERT INTO employee_configs
                (id, name, department_id, avatar, description, model_id, temperature, skills, mcp_config, memory_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                emp["id"],
                emp["name"],
                emp["department_id"],
                emp.get("avatar", "🤖"),
                emp.get("description", ""),
                emp.get("model_id", "deepseek-v4-flash"),  # 2026-05-22 V3.2→V4 全切
                emp.get("temperature", 0.7),
                emp.get("skills", "[]"),
                emp.get("mcp_config", "{}"),
                emp.get("memory_type", "temporary"),
            ))
    
        conn.commit()
        conn.close()
    finally:
        try:
            conn.close()
        except Exception: pass


def get_employee_configs(
    department_id: str = None,
    include_inactive: bool = False,
) -> List[dict]:
    """获取员工配置列表"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        query = """
            SELECT e.*, d.name as department_name, d.icon as department_icon
            FROM employee_configs e
            LEFT JOIN departments d ON e.department_id = d.id
            WHERE 1=1
        """
        params = []
    
        if department_id:
            query += " AND e.department_id = %s"
            params.append(department_id)

        if not include_inactive:
            query += " AND e.is_active = 1"
    
        query += " ORDER BY d.sort_order, e.sort_order"
    
        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
    
        result = []
        for row in rows:
            emp = dict(row)
            # 解析JSON字段
            if emp.get("skills"):
                try:
                    emp["skills"] = json.loads(emp["skills"])
                except:
                    emp["skills"] = []
            if emp.get("mcp_config"):
                try:
                    emp["mcp_config"] = json.loads(emp["mcp_config"])
                except:
                    emp["mcp_config"] = {}
            result.append(emp)
    
        return result
    finally:
        try:
            conn.close()
        except Exception: pass


def get_employee_config(employee_id: str) -> Optional[dict]:
    """获取单个员工配置"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            SELECT e.*, d.name as department_name, d.icon as department_icon
            FROM employee_configs e
            LEFT JOIN departments d ON e.department_id = d.id
            WHERE e.id = %s
        """, (employee_id,))
        row = cursor.fetchone()
        conn.close()
    
        if not row:
            return None
    
        emp = dict(row)
        # 解析JSON字段
        if emp.get("skills"):
            try:
                emp["skills"] = json.loads(emp["skills"])
            except:
                emp["skills"] = []
        if emp.get("mcp_config"):
            try:
                emp["mcp_config"] = json.loads(emp["mcp_config"])
            except:
                emp["mcp_config"] = {}
    
        return emp
    finally:
        try:
            conn.close()
        except Exception: pass


def save_employee_config(
    employee_id: str,
    name: str,
    department_id: str,
    avatar: str = "🤖",
    description: str = None,
    model_id: str = "deepseek-v4-flash",
    temperature: float = 0.7,
    max_tokens: int = 4000,
    system_prompt: str = None,
    skills: list = None,
    mcp_config: dict = None,
    memory_type: str = "temporary",
    usage_scope: str = "all",
    is_active: bool = True,
    sort_order: int = 0,
) -> bool:
    """保存或更新员工配置"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        skills_json = json.dumps(skills or [], ensure_ascii=False)
        mcp_json = json.dumps(mcp_config or {}, ensure_ascii=False)
    
        cursor.execute("""
            INSERT INTO employee_configs
            (id, name, department_id, avatar, description, model_id, temperature, max_tokens,
             system_prompt, skills, mcp_config, memory_type, usage_scope, is_active, sort_order, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                department_id = excluded.department_id,
                avatar = excluded.avatar,
                description = excluded.description,
                model_id = excluded.model_id,
                temperature = excluded.temperature,
                max_tokens = excluded.max_tokens,
                system_prompt = excluded.system_prompt,
                skills = excluded.skills,
                mcp_config = excluded.mcp_config,
                memory_type = excluded.memory_type,
                usage_scope = excluded.usage_scope,
                is_active = excluded.is_active,
                sort_order = excluded.sort_order,
                updated_at = CURRENT_TIMESTAMP
        """, (
            employee_id, name, department_id, avatar, description, model_id,
            temperature, max_tokens, system_prompt, skills_json, mcp_json,
            memory_type, usage_scope, int(is_active), sort_order
        ))
    
        conn.commit()
        conn.close()
        return True
    finally:
        try:
            conn.close()
        except Exception: pass


def delete_employee_config(employee_id: str) -> bool:
    """删除员工配置（软删除）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            UPDATE employee_configs
            SET is_active = 0, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (employee_id,))
    
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


# ============================================
# 会议记录管理函数
# ============================================

def save_meeting(
    meeting_id: str,
    topic: str,
    participants: list[str],
    participant_ids: list[str] = None,
    moderator_id: str = None,
    moderator_name: str = None,
    brand_id: int = None,
    brand_name: str = None,
    is_internal: bool = False,
    context: str = None,
    transcript: list = None,
    conclusion: str = None,
    summary: str = None,
    action_items: list = None,
    attachments: list = None,
    status: str = "completed",
    rounds: int = 2,
    duration: float = None,
    tags: list = None,
) -> int:
    """保存会议记录"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            INSERT INTO employee_meetings
            (meeting_id, topic, context, brand_id, brand_name, is_internal,
             participants, participant_ids, moderator_id, moderator_name,
             transcript, conclusion, summary, action_items, attachments,
             status, rounds, duration, tags, started_at, completed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(meeting_id) DO UPDATE SET
                transcript = excluded.transcript,
                conclusion = excluded.conclusion,
                summary = excluded.summary,
                action_items = excluded.action_items,
                status = excluded.status,
                duration = excluded.duration,
                completed_at = CURRENT_TIMESTAMP
            RETURNING id
        """, (
            meeting_id,
            topic,
            context,
            brand_id,
            brand_name,
            int(is_internal),
            json.dumps(participants, ensure_ascii=False),
            json.dumps(participant_ids or [], ensure_ascii=False),
            moderator_id,
            moderator_name,
            json.dumps(transcript or [], ensure_ascii=False),
            conclusion,
            summary,
            json.dumps(action_items or [], ensure_ascii=False),
            json.dumps(attachments or [], ensure_ascii=False),
            status,
            rounds,
            duration,
            json.dumps(tags or [], ensure_ascii=False),
        ))

        conn.commit()
        meeting_db_id = cursor.fetchone()["id"]
        conn.close()
        return meeting_db_id
    finally:
        try:
            conn.close()
        except Exception: pass


def get_meeting(meeting_id: str) -> Optional[Dict]:
    """获取单个会议详情"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            SELECT m.*, b.name as brand_display_name
            FROM employee_meetings m
            LEFT JOIN brands b ON m.brand_id = b.id
            WHERE m.meeting_id = %s
        """, (meeting_id,))
        row = cursor.fetchone()
        conn.close()
    
        if not row:
            return None
    
        meeting = dict(row)
        # 解析JSON字段
        for field in ['participants', 'participant_ids', 'transcript', 'action_items', 'attachments', 'tags']:
            if meeting.get(field):
                try:
                    meeting[field] = json.loads(meeting[field])
                except:
                    meeting[field] = []
    
        return meeting
    finally:
        try:
            conn.close()
        except Exception: pass


def list_meetings(
    brand_id: int = None,
    is_internal: bool = None,
    status: str = None,
    search: str = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict]:
    """列出会议记录"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        query = """
            SELECT m.id, m.meeting_id, m.topic, m.brand_id, m.brand_name, m.is_internal,
                   m.participants, m.moderator_name, m.conclusion, m.summary,
                   m.status, m.rounds, m.duration, m.tags, m.created_at, m.completed_at,
                   b.name as brand_display_name
            FROM employee_meetings m
            LEFT JOIN brands b ON m.brand_id = b.id
            WHERE 1=1
        """
        params = []
    
        if brand_id is not None:
            query += " AND m.brand_id = %s"
            params.append(brand_id)

        if is_internal is not None:
            query += " AND m.is_internal = %s"
            params.append(int(is_internal))

        if status:
            query += " AND m.status = %s"
            params.append(status)

        if search:
            query += " AND (m.topic LIKE %s OR m.conclusion LIKE %s OR m.tags LIKE %s)"
            search_pattern = f"%{search}%"
            params.extend([search_pattern, search_pattern, search_pattern])

        query += " ORDER BY m.created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])
    
        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
    
        meetings = []
        for row in rows:
            meeting = dict(row)
            # 解析JSON字段
            for field in ['participants', 'tags']:
                if meeting.get(field):
                    try:
                        meeting[field] = json.loads(meeting[field])
                    except:
                        meeting[field] = []
            meetings.append(meeting)
    
        return meetings
    finally:
        try:
            conn.close()
        except Exception: pass


def count_meetings(
    brand_id: int = None,
    is_internal: bool = None,
    status: str = None,
    search: str = None,
) -> int:
    """统计会议数量"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        query = "SELECT COUNT(*) AS cnt FROM employee_meetings m WHERE 1=1"
        params = []
    
        if brand_id is not None:
            query += " AND m.brand_id = %s"
            params.append(brand_id)

        if is_internal is not None:
            query += " AND m.is_internal = %s"
            params.append(int(is_internal))

        if status:
            query += " AND m.status = %s"
            params.append(status)

        if search:
            query += " AND (m.topic LIKE %s OR m.conclusion LIKE %s OR m.tags LIKE %s)"
            search_pattern = f"%{search}%"
            params.extend([search_pattern, search_pattern, search_pattern])
    
        cursor.execute(query, params)
        count = cursor.fetchone()["cnt"]
        conn.close()
        return count
    finally:
        try:
            conn.close()
        except Exception: pass


def delete_meeting(meeting_id: str) -> bool:
    """删除会议记录"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("DELETE FROM employee_meetings WHERE meeting_id = %s", (meeting_id,))
    
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def get_meeting_stats() -> Dict:
    """获取会议统计信息"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        # 总会议数
        cursor.execute("SELECT COUNT(*) AS cnt FROM employee_meetings")
        total = cursor.fetchone()["cnt"]

        # 本周会议数
        cursor.execute("""
            SELECT COUNT(*) AS cnt FROM employee_meetings
            WHERE created_at >= NOW() - INTERVAL '7 days'
        """)
        this_week = cursor.fetchone()["cnt"]
    
        # 按状态统计
        cursor.execute("""
            SELECT status, COUNT(*) as count 
            FROM employee_meetings 
            GROUP BY status
        """)
        by_status = {row["status"]: row["count"] for row in cursor.fetchall()}

        # 内部vs客户会议
        cursor.execute("""
            SELECT is_internal, COUNT(*) as count 
            FROM employee_meetings 
            GROUP BY is_internal
        """)
        by_type = {row["is_internal"]: row["count"] for row in cursor.fetchall()}

        # 最活跃品牌
        cursor.execute("""
            SELECT brand_name, COUNT(*) as count 
            FROM employee_meetings 
            WHERE brand_name IS NOT NULL AND brand_name != ''
            GROUP BY brand_name 
            ORDER BY count DESC 
            LIMIT 5
        """)
        top_brands = [{"name": row["brand_name"], "count": row["count"]} for row in cursor.fetchall()]
    
        conn.close()
    
        return {
            "total": total,
            "this_week": this_week,
            "by_status": by_status,
            "internal_count": by_type.get(1, 0),
            "client_count": by_type.get(0, 0),
            "top_brands": top_brands,
        }
    finally:
        try:
            conn.close()
        except Exception: pass


# ============================================
# 顾问 CRUD 函数
# ============================================

def save_advisor(
    advisor_id: str,
    name: str,
    base_prompt: str,
    avatar: str = "👤",
    description: str = "",
    api_provider: str = "dashscope",
    model_name: str = "qwen3.7-max",
    enable_web_search: bool = False,
    knowledge_path: str = None,
    role: str = "writer",
    specialty: str = None,
    industries: str = None,
) -> bool:
    """保存或更新顾问"""
    init_db()
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        # 确保表有新字段
        _safe_add_column(cursor, "advisors", "api_provider", "TEXT DEFAULT 'dashscope'")
        _safe_add_column(cursor, "advisors", "model_name", "TEXT DEFAULT 'qwen3.7-max'")
        _safe_add_column(cursor, "advisors", "enable_web_search", "INTEGER DEFAULT 0")
        # Wave 2: 专家市场扩展字段
        _safe_add_column(cursor, "advisors", "tags", "JSONB DEFAULT '[]'")
        _safe_add_column(cursor, "advisors", "credentials", "TEXT DEFAULT ''")
        _safe_add_column(cursor, "advisors", "greeting", "TEXT DEFAULT ''")
        _safe_add_column(cursor, "advisors", "quick_questions", "JSONB DEFAULT '[]'")
        _safe_add_column(cursor, "advisors", "knowledge_count", "INTEGER DEFAULT 0")
    
        cursor.execute("""
            INSERT INTO advisors
            (id, name, avatar, description, base_prompt, api_provider, model_name, enable_web_search, knowledge_path, role, specialty, industries, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                name = EXCLUDED.name,
                avatar = EXCLUDED.avatar,
                description = EXCLUDED.description,
                base_prompt = EXCLUDED.base_prompt,
                api_provider = EXCLUDED.api_provider,
                model_name = EXCLUDED.model_name,
                enable_web_search = EXCLUDED.enable_web_search,
                knowledge_path = EXCLUDED.knowledge_path,
                role = EXCLUDED.role,
                specialty = EXCLUDED.specialty,
                industries = EXCLUDED.industries,
                updated_at = CURRENT_TIMESTAMP
        """, (advisor_id, name, avatar, description, base_prompt, api_provider, model_name, 1 if enable_web_search else 0, knowledge_path, role, specialty, industries))
    
        conn.commit()
        conn.close()
        return True
    finally:
        try:
            conn.close()
        except Exception: pass


def get_advisor_db(advisor_id: str) -> Optional[Dict]:
    """获取单个顾问"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM advisors WHERE id = %s", (advisor_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception: pass


def get_advisors(is_active: bool = True) -> List[Dict]:
    """获取顾问列表"""
    init_db()
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        if is_active:
            cursor.execute("SELECT * FROM advisors WHERE is_active = 1 ORDER BY created_at DESC")
        else:
            cursor.execute("SELECT * FROM advisors ORDER BY created_at DESC")
    
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def delete_advisor(advisor_id: str) -> bool:
    """删除顾问"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        # 先删除关联文档
        cursor.execute("DELETE FROM advisor_documents WHERE advisor_id = %s", (advisor_id,))
        # 再删除顾问
        cursor.execute("DELETE FROM advisors WHERE id = %s", (advisor_id,))
    
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def save_advisor_document(
    advisor_id: str,
    filename: str,
    file_type: str = "txt",
    chunk_count: int = 0,
    file_size: int = 0,
) -> int:
    """保存顾问文档记录"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            INSERT INTO advisor_documents
            (advisor_id, filename, file_type, chunk_count, file_size)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """, (advisor_id, filename, file_type, chunk_count, file_size))

        doc_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return doc_id
    finally:
        try:
            conn.close()
        except Exception: pass


def get_advisor_documents(advisor_id: str) -> List[Dict]:
    """获取顾问的文档列表"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM advisor_documents
            WHERE advisor_id = %s
            ORDER BY uploaded_at DESC
        """, (advisor_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


# ============================================
# 顾问对话历史
# ============================================

def _ensure_advisor_conversations_table():
    """确保对话表存在"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS advisor_conversations (
                id SERIAL PRIMARY KEY,
                advisor_id TEXT NOT NULL,
                -- 🔴 [#114] 生产这一列叫 `conversation_id`,不叫 `session_id`。
                --    本 DDL 带 IF NOT EXISTS,而生产表**早就存在** ⇒ 它永不触发;
                --    于是下面 `WHERE session_id = %s` 在生产必炸、在全新库上不炸。
                --    「按名判存的守卫」典型:表在,但不是你以为的那张。
                --    这里同步改成生产形,免得新建库与生产分叉出第二种 schema。
                conversation_id TEXT NOT NULL,
                title TEXT,
                message_count INTEGER DEFAULT 0,
                owner_user_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS advisor_messages (
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversation_id) REFERENCES advisor_conversations(id)
            )
        """)
        conn.commit()
        conn.close()
    finally:
        try:
            conn.close()
        except Exception: pass


# [开源 E3 · B2 · 2026-09-28] save_advisor_conversation / save_advisor_message 删:全仓 0 调用(顾问对话写入的调用方随社媒对话一并删除);
#   读侧 get_advisor_conversations / get_conversation_messages 与建表 _ensure_advisor_conversations_table 在役保留。


def get_advisor_conversations(advisor_id: str) -> List[Dict]:
    """获取顾问的所有对话列表"""
    _ensure_advisor_conversations_table()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT c.*,
                (SELECT COUNT(*) FROM advisor_messages WHERE conversation_id = c.id) as message_count
            FROM advisor_conversations c
            WHERE c.advisor_id = %s
            ORDER BY c.updated_at DESC
        """, (advisor_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def get_conversation_messages(conversation_id: int) -> List[Dict]:
    """获取对话的所有消息"""
    _ensure_advisor_conversations_table()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM advisor_messages
            WHERE conversation_id = %s
            ORDER BY created_at ASC
        """, (conversation_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


# ============================================
# 工作台/成果库 (Workspace)
# ============================================

def _ensure_workspace_tables():
    """确保工作台相关表存在"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        # 成果类型表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS result_types (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                icon TEXT DEFAULT '📄',
                color TEXT DEFAULT '#6366f1',
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    
        # 工作成果表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS work_results (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                result_type TEXT,
                brand_id INTEGER,
                brand_name TEXT,
                meeting_id TEXT,
                assignee_id TEXT,
                assignee_name TEXT,
                content TEXT,
                attachments TEXT,
                tags TEXT,
                keywords TEXT,
                status TEXT DEFAULT '待执行',
                priority INTEGER DEFAULT 2,
                due_date TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
        """)
    
        # 插入默认成果类型
        cursor.execute("SELECT COUNT(*) AS cnt FROM result_types")
        if cursor.fetchone()["cnt"] == 0:
            default_types = [
                ('营销计划', '📊', '#10b981', '品牌营销策略和执行计划'),
                ('执行方案', '📋', '#3b82f6', '具体执行步骤和操作指南'),
                ('诊断报告', '🔍', '#f59e0b', 'GEO诊断分析报告'),
                ('品牌简介', '🏢', '#8b5cf6', '品牌基本信息和定位'),
                ('竞品分析', '⚔️', '#ef4444', '竞争对手分析报告'),
                ('内容策略', '✍️', '#06b6d4', '内容创作方向和规划'),
                ('会议纪要', '📝', '#6366f1', '会议讨论记录和决议'),
                ('其他', '📁', '#71717a', '其他类型的工作成果'),
            ]
            cursor.executemany("""
                INSERT INTO result_types (name, icon, color, description)
                VALUES (%s, %s, %s, %s)
            """, default_types)
    
        conn.commit()
        conn.close()
    finally:
        try:
            conn.close()
        except Exception: pass


def get_result_types() -> List[Dict]:
    """获取所有成果类型"""
    _ensure_workspace_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM result_types ORDER BY id")
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def add_result_type(name: str, icon: str = "📄", color: str = "#6366f1", description: str = "") -> int:
    """添加成果类型"""
    _ensure_workspace_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO result_types (name, icon, color, description)
            VALUES (%s, %s, %s, %s)
            RETURNING id
        """, (name, icon, color, description))
        type_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return type_id
    finally:
        try:
            conn.close()
        except Exception: pass


def delete_result_type(type_id: int) -> bool:
    """删除成果类型"""
    _ensure_workspace_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM result_types WHERE id = %s", (type_id,))
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def get_work_results(
    status: str = None,
    result_type: str = None,
    brand_id: int = None,
    assignee_id: str = None,
    search: str = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict]:
    """获取工作成果列表，支持多维度筛选"""
    _ensure_workspace_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        query = "SELECT * FROM work_results WHERE 1=1"
        params = []
    
        if status:
            query += " AND status = %s"
            params.append(status)
        if result_type:
            query += " AND result_type = %s"
            params.append(result_type)
        if brand_id:
            query += " AND brand_id = %s"
            params.append(brand_id)
        if assignee_id:
            query += " AND assignee_id = %s"
            params.append(assignee_id)
        if search:
            # 模糊搜索：支持 "A B C" 多关键词
            keywords = search.strip().split()
            for kw in keywords:
                query += " AND (title LIKE %s OR keywords LIKE %s OR tags LIKE %s OR content LIKE %s)"
                like_kw = f"%{kw}%"
                params.extend([like_kw, like_kw, like_kw, like_kw])

        query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])
    
        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def create_work_result(
    title: str,
    result_type: str = None,
    brand_id: int = None,
    brand_name: str = None,
    meeting_id: str = None,
    assignee_id: str = None,
    assignee_name: str = None,
    content: str = None,
    attachments: list = None,
    tags: list = None,
    keywords: str = None,
    status: str = "待执行",
    priority: int = 2,
    due_date: str = None,
) -> int:
    """创建工作成果"""
    _ensure_workspace_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            INSERT INTO work_results (
                title, result_type, brand_id, brand_name, meeting_id,
                assignee_id, assignee_name, content, attachments, tags,
                keywords, status, priority, due_date
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            title, result_type, brand_id, brand_name, meeting_id,
            assignee_id, assignee_name, content,
            json.dumps(attachments) if attachments else None,
            json.dumps(tags) if tags else None,
            keywords, status, priority, due_date
        ))

        result_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return result_id
    finally:
        try:
            conn.close()
        except Exception: pass


def update_work_result(result_id: int, **fields) -> bool:
    """更新工作成果"""
    _ensure_workspace_tables()
    if not fields:
        return False
    
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        # 特殊处理JSON字段
        if 'attachments' in fields and isinstance(fields['attachments'], list):
            fields['attachments'] = json.dumps(fields['attachments'])
        if 'tags' in fields and isinstance(fields['tags'], list):
            fields['tags'] = json.dumps(fields['tags'])
    
        # 如果状态变为已完成，记录完成时间
        if fields.get('status') == '已完成':
            fields['completed_at'] = datetime.now().isoformat()
    
        set_clause = ", ".join([f"{k} = %s" for k in fields.keys()])
        query = f"UPDATE work_results SET {set_clause} WHERE id = %s"
    
        cursor.execute(query, list(fields.values()) + [result_id])
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def delete_work_result(result_id: int) -> bool:
    """删除工作成果"""
    _ensure_workspace_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM work_results WHERE id = %s", (result_id,))
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def get_work_result(result_id: int) -> Optional[Dict]:
    """获取单个工作成果"""
    _ensure_workspace_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM work_results WHERE id = %s", (result_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception: pass


def get_workspace_stats() -> Dict:
    """获取工作台统计"""
    _ensure_workspace_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        # 按状态统计
        cursor.execute("""
            SELECT status, COUNT(*) as count FROM work_results GROUP BY status
        """)
        by_status = {row["status"]: row["count"] for row in cursor.fetchall()}

        # 按类型统计
        cursor.execute("""
            SELECT result_type, COUNT(*) as count FROM work_results 
            WHERE result_type IS NOT NULL GROUP BY result_type
        """)
        by_type = {row["result_type"]: row["count"] for row in cursor.fetchall()}

        conn.close()
        return {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "by_type": by_type,
        }
    finally:
        try:
            conn.close()
        except Exception: pass


# ============================================
# 会议预设 (Meeting Presets)
# ============================================

def _ensure_meeting_presets_table():
    """确保会议预设表存在"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS meeting_presets (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                style TEXT,
                rounds INTEGER DEFAULT 2,
                co_moderator_id TEXT,
                co_moderator_name TEXT,
                weight_boost REAL DEFAULT 1.5,
                auto_execute BOOLEAN DEFAULT false,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    
        conn.commit()
        conn.close()
    finally:
        try:
            conn.close()
        except Exception: pass


def get_meeting_presets() -> List[Dict]:
    """获取所有会议预设"""
    _ensure_meeting_presets_table()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM meeting_presets ORDER BY created_at DESC")
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def save_meeting_preset(
    name: str,
    style: str = None,
    rounds: int = 2,
    co_moderator_id: str = None,
    co_moderator_name: str = None,
    weight_boost: float = 1.5,
    auto_execute: bool = False,
    description: str = None,
) -> int:
    """保存会议预设"""
    _ensure_meeting_presets_table()
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        # 如果存在则更新，不存在则插入
        cursor.execute("SELECT id FROM meeting_presets WHERE name = %s", (name,))
        existing = cursor.fetchone()

        if existing:
            cursor.execute("""
                UPDATE meeting_presets SET
                    style = %s, rounds = %s, co_moderator_id = %s, co_moderator_name = %s,
                    weight_boost = %s, auto_execute = %s, description = %s
                WHERE name = %s
            """, (style, rounds, co_moderator_id, co_moderator_name, weight_boost, auto_execute, description, name))
            preset_id = existing[0]
        else:
            cursor.execute("""
                INSERT INTO meeting_presets (name, style, rounds, co_moderator_id, co_moderator_name, weight_boost, auto_execute, description)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (name, style, rounds, co_moderator_id, co_moderator_name, weight_boost, auto_execute, description))
            preset_id = cursor.fetchone()["id"]
    
        conn.commit()
        conn.close()
        return preset_id
    finally:
        try:
            conn.close()
        except Exception: pass


def delete_meeting_preset(preset_id: int) -> bool:
    """删除会议预设"""
    _ensure_meeting_presets_table()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM meeting_presets WHERE id = %s", (preset_id,))
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


# ============================================
# 员工能力配置 (Employee Capabilities)
# ============================================

def _ensure_employee_capabilities_table():
    """确保员工能力配置表存在"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS employee_capabilities (
                id SERIAL PRIMARY KEY,
                employee_id TEXT NOT NULL,
                capability_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                target_name TEXT,
                enabled BOOLEAN DEFAULT true,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(employee_id, capability_type, target_id)
            )
        """)
    
        conn.commit()
        conn.close()
    finally:
        try:
            conn.close()
        except Exception: pass


def get_employee_capabilities(employee_id: str) -> Dict[str, List[Dict]]:
    """获取员工的能力配置"""
    _ensure_employee_capabilities_table()
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            SELECT * FROM employee_capabilities
            WHERE employee_id = %s AND enabled = 1
        """, (employee_id,))
        rows = cursor.fetchall()
        conn.close()
    
        # 按类型分组
        result = {
            "call_employee": [],
            "call_advisor": [],
            "execute_tool": [],
        }
        for row in rows:
            row_dict = dict(row)
            cap_type = row_dict.get("capability_type", "")
            if cap_type in result:
                result[cap_type].append({
                    "id": row_dict["id"],
                    "target_id": row_dict["target_id"],
                    "target_name": row_dict.get("target_name", ""),
                })
    
        return result
    finally:
        try:
            conn.close()
        except Exception: pass


def set_employee_capability(
    employee_id: str,
    capability_type: str,
    target_id: str,
    target_name: str = None,
    enabled: bool = True,
) -> int:
    """设置员工能力（如已存在则更新）"""
    _ensure_employee_capabilities_table()
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            INSERT INTO employee_capabilities
            (employee_id, capability_type, target_id, target_name, enabled)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(employee_id, capability_type, target_id) DO UPDATE SET
                target_name = EXCLUDED.target_name,
                enabled = EXCLUDED.enabled
            RETURNING id
        """, (employee_id, capability_type, target_id, target_name, enabled))

        cap_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return cap_id
    finally:
        try:
            conn.close()
        except Exception: pass


def remove_employee_capability(employee_id: str, capability_type: str, target_id: str) -> bool:
    """移除员工能力"""
    _ensure_employee_capabilities_table()
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            DELETE FROM employee_capabilities
            WHERE employee_id = %s AND capability_type = %s AND target_id = %s
        """, (employee_id, capability_type, target_id))
    
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def batch_update_employee_capabilities(
    employee_id: str,
    capabilities: Dict[str, List[str]],
) -> bool:
    """批量更新员工能力配置
    
    Args:
        employee_id: 员工ID
        capabilities: {
            "call_employee": ["emp1", "emp2"],
            "call_advisor": ["advisor1"],
            "execute_tool": ["tool1"],
        }
    """
    _ensure_employee_capabilities_table()
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        # 先删除该员工的所有能力
        cursor.execute("DELETE FROM employee_capabilities WHERE employee_id = %s", (employee_id,))

        # 批量插入新能力
        for cap_type, targets in capabilities.items():
            for target_id in targets:
                cursor.execute("""
                    INSERT INTO employee_capabilities
                    (employee_id, capability_type, target_id, enabled)
                    VALUES (%s, %s, %s, 1)
                """, (employee_id, cap_type, target_id))
        
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        return False
    finally:
        conn.close()


# ==========================================
#  选词报价会话 CRUD
# ==========================================

def create_selection_session(
    token: str,
    quote_id: int,
    brand_id: int,
    created_by: int,
    keywords_snapshot: str,
    expires_at: str,
) -> int:
    """创建选词会话，返回 session id"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM quotes WHERE id=%s AND brand_id=%s AND deleted_at IS NULL FOR UPDATE",
            (quote_id, brand_id),
        )
        if not cursor.fetchone():
            raise ValueError("QUOTE_ARCHIVED_OR_SCOPE_CHANGED")
        try:
            cursor.execute("""
                INSERT INTO keyword_selection_sessions
                    (token, quote_id, brand_id, created_by, keywords_snapshot, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (token, quote_id, brand_id, created_by, keywords_snapshot, expires_at))
        except UniqueViolation:
            # [P1-A 2026-07-30] 并发/重复提交同一 quote_id 时,唯一约束
            #   keyword_selection_sessions_quote_id_key 会打掉后到的那几个 INSERT。
            #   本入口在 check(get_session_by_quote) 与 INSERT 之间夹了一次 LLM
            #   await(extract_business_lines,秒级),所以同一批并发全都能通过前置检查,
            #   只有第一个 INSERT 成功 —— 剩下的把 UniqueViolation 抛到 Starlette
            #   ServerErrorMiddleware,回给用户 500 "Internal Server Error"。
            #   本入口契约本来就是幂等(方式 1 已存在则返 already_exists),所以这里
            #   把"输给并发"翻译成幂等信号,由调用方回吐已存在的那条 session。
            #   判别方式用行为(该 quote 确实已有 session)而非约束名,避免约束改名后失效;
            #   token 撞车等其它唯一冲突不满足该条件 → 原样抛出,不吞。
            conn.rollback()
            cursor.execute(
                "SELECT id FROM keyword_selection_sessions WHERE quote_id = %s",
                (quote_id,),
            )
            winner = cursor.fetchone()
            if not winner:
                raise
            raise ValueError("SESSION_EXISTS_FOR_QUOTE") from None
        session_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return session_id
    finally:
        try:
            conn.close()
        except Exception: pass


def list_selection_sessions(status_filter: str = None, limit: int = 50) -> list[dict]:
    """列出选词会话（销售端概览用）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        sql = """
            SELECT s.id, s.token, s.quote_id, s.brand_id, s.status, s.expires_at,
                   s.created_at, s.updated_at, s.keywords_submitted_at,
                   s.confirmed_at, s.sales_confirmed_at, s.payment_received_at,
                   s.confirmed_total_price, s.final_price, s.selected_tier,
                   q.brand_name, q.industry, q.city,
                   COALESCE((SELECT COUNT(*) FROM json_array_elements_text(
                       CASE WHEN s.selected_keyword_ids IS NOT NULL
                            AND s.selected_keyword_ids != ''
                            AND s.selected_keyword_ids != '[]'
                       THEN s.selected_keyword_ids::json
                       ELSE '[]'::json END
                   )), 0) +
                   COALESCE((SELECT COUNT(*) FROM json_array_elements_text(
                       CASE WHEN s.custom_keywords IS NOT NULL
                            AND s.custom_keywords != ''
                            AND s.custom_keywords != '[]'
                       THEN s.custom_keywords::json
                       ELSE '[]'::json END
                   )), 0) as selected_count
            FROM keyword_selection_sessions s
            LEFT JOIN quotes q ON s.quote_id = q.id
            WHERE q.deleted_at IS NULL
        """
        params = []
        if status_filter:
            sql += " AND s.status = %s"
            params.append(status_filter)
        sql += " ORDER BY s.updated_at DESC LIMIT %s"
        params.append(limit)
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def get_session_by_token(token: str) -> Optional[dict]:
    """根据 token 获取会话"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM keyword_selection_sessions WHERE token = %s", (token,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception: pass


# ══════════════════════════════════════════════════════════════════════════
# [工单 V4-A · Codex fix-of-fix P1-2] 品牌级序列化点 + 选词会话原子延期
# ══════════════════════════════════════════════════════════════════════════
#: advisory lock 的命名空间。用**双键**形式 `pg_advisory_xact_lock(ns, brand_id)`,
#: 不用单键:单键把整个 int8 空间和别的 advisory lock 使用者混在一起,
#: 撞上了会变成"莫名其妙的互相阻塞",而且查起来没有任何线索指向这里。
BRAND_QUOTE_LOCK_NS = 0x6771  # 'gq' —— geo quote


def lock_brand_quote_serialization_point(cursor, brand_id: int) -> None:
    """品牌级**序列化点**:同一品牌的「建报价」与「轮换客户 token」互斥。

    🔴 [Codex fix-of-fix P1-2a] 为什么必须有这么个东西
    ------------------------------------------------
    门户轮换要回答的问题是「她看到的那个报价**此刻仍然是**这个品牌的 canonical 吗」。
    默认 READ COMMITTED 下,这个问题只能用一次普通 SELECT 回答,而那个答案
    **在语句返回的一瞬间就可能过期**:并发事务可以在 SELECT 返回之后、
    轮换事务提交之前插入一份更新的报价并提交。Codex 的真 PG16 双连接反例就是这样:

        returned_object = quote A · current_canonical = quote B · A/B token 皆 active

    行锁救不了它:新报价是 **INSERT**,不存在可以被 `FOR UPDATE` 锁住的旧行。
    SERIALIZABLE 也救不了:T1(读 quotes 谓词 + 写 tokens)与 T2(插 quotes)之间
    只有**一条** rw-反依赖,不构成危险结构,PG 会认为「T1 先」是合法串行序而放行。
    所以只能显式约定一个**双方都要拿**的序列化点。

    🔴 谁必须拿它:生产上建报价的三处 + 轮换那一处。少拿一处,窗口就从那一处漏回来
    —— 这正是本仓反复记的「漏掉的那一处不会让任何判据变红」,
    所以配了一条机械 census 判据钉住调用点集合。

    锁是 **xact** 级:随事务提交/回滚自动释放,不需要也不允许手工 unlock
    (手工 unlock 一旦走进异常分支就会漏放,把整个品牌卡死)。
    """
    cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                   (BRAND_QUOTE_LOCK_NS, int(brand_id)))


#: 已进入商业推进的会话状态 —— 延期**绝不许**把它们改回选词态。
#:
#: 🔴 与 `api/selection_api._check_expired_locked` 的豁免集合同源(去掉 `expired` 本身)。
#:    这里是第二份**字面量**,所以配了一条判据从现役那一侧的 AST 机械抽出来逐字比
#:    —— 手抄的清单掉一档不会让任何判据变红,而掉的那一档就是可以被回退的那一档。
NON_REVERSIBLE_SELECTION_STATUSES = (
    "confirmed", "pending_payment", "active", "payment_overdue",
    "pricing_pending_review", "business_lines_submitted",
)


def extend_selection_session_atomically(token: str, *, days: int = 7) -> dict:
    """选词会话延期 —— **单事务 · 锁行 · 条件更新 · 检查 rowcount**。

    🔴 [Codex fix-of-fix P1-2b] 修的是什么
    -------------------------------------
    上一版是 `get_session_by_token()` → 在 Python 里算 → `update_session()`,
    **三条独立连接上的读→算→盲写**,`UPDATE … WHERE token=%s` 不带任何状态条件。
    Codex 真 PG16 反例:延期读到 `expired` 之后,并发流程把会话推进成 `confirmed`,
    延期请求随后仍把状态覆盖回 `selecting` ——

        read_status = expired · concurrent_status = confirmed · final_status = selecting

    **已确认的报价被重新打开**,这是真实的业务状态回退。

    现在:一条连接、一个事务,`SELECT … FOR UPDATE` 锁住那一行再判再写。
    并发推进要么排在我们前面(于是我们看到 confirmed 并拒绝),
    要么排在后面(于是它看到的是延期后的状态)。中间态不存在。

    返回 typed 结果而不是 bool:
      · `{"ok": True,  "status", "expires_at", "restored"}`
      · `{"ok": False, "reason": "not_found" | "status_not_extendable", "status"}`
    调用方据此给用户 typed 拒绝,而不是「看起来成功了但什么都没变」。
    """
    from datetime import datetime, timedelta

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT token, status, expires_at FROM keyword_selection_sessions "
            " WHERE token = %s FOR UPDATE", (token,))
        row = cursor.fetchone()
        if row is None:
            conn.rollback()
            return {"ok": False, "reason": "not_found", "status": None}
        row = dict(row)
        status = str(row.get("status") or "")
        if status in NON_REVERSIBLE_SELECTION_STATUSES:
            # 🔴 一行不写。「延期」对一个已确认/已付款的会话没有意义,
            #    而把它写回 selecting 是把商业事实倒回去。
            conn.rollback()
            return {"ok": False, "reason": "status_not_extendable", "status": status}

        current = row.get("expires_at")
        try:
            base = datetime.fromisoformat(str(current)) if current else datetime.now()
        except (TypeError, ValueError):
            base = datetime.now()
        new_exp = max(base, datetime.now()) + timedelta(days=int(days))
        new_exp_str = new_exp.strftime("%Y-%m-%d %H:%M:%S")

        # 🔴 条件更新:`WHERE token=%s AND status=%s` —— 状态是**进来时那一个**。
        #    行已经被 FOR UPDATE 锁住,所以这个条件在正常路径上必然成立;
        #    它在这里是**第二道保险**(万一将来有人把上面的锁拿掉,rowcount 会当场变 0),
        #    而不是装饰。
        restored = status == "expired"
        new_status = "selecting" if restored else status
        cursor.execute(
            "UPDATE keyword_selection_sessions "
            "   SET expires_at = %s, status = %s, updated_at = %s "
            " WHERE token = %s AND status = %s",
            (new_exp_str, new_status,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), token, status))
        affected = cursor.rowcount
        if affected != 1:
            conn.rollback()
            return {"ok": False, "reason": "status_not_extendable", "status": status}
        conn.commit()
        return {"ok": True, "status": new_status, "expires_at": new_exp_str,
                "restored": restored}
    except Exception:
        try:
            conn.rollback()
        except Exception:                                    # noqa: BLE001
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:                                    # noqa: BLE001
            pass


def get_session_by_quote(quote_id: int) -> Optional[dict]:
    """根据 quote_id 获取会话（1:1）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM keyword_selection_sessions WHERE quote_id = %s", (quote_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception: pass


def update_session(token: str, **fields):
    """更新会话的任意字段

    [CTO-15.23 2026-05-14 root-cause fix] 老板报"打开公开页日期变今天"根因之一:
      api/selection_api.py:154 每次访问 /api/s/{token} 调 update_session(visit_count++)
      老逻辑无条件 fields["updated_at"]=NOW() → session.updated_at 被纯访问污染
      数据语义错位:前端虽暂时不显示 · 但统计/报表/排序用此字段会失真
    修法:反向白名单 · 纯访问计数字段不刷 updated_at;业务字段照常刷
    """
    if not fields:
        return False
    # 纯访问/计数字段集合 · 这些字段单独更新时不刷 updated_at(防止公开页访问污染)
    ACCESS_ONLY_FIELDS = {'visit_count', 'last_visited_at'}
    if not all(k in ACCESS_ONLY_FIELDS for k in fields):
        # 至少有一个业务字段 · 走原逻辑刷 updated_at
        fields["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    set_clause = ", ".join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [token]
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE keyword_selection_sessions SET {set_clause} WHERE token = %s",
            values,
        )
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def delete_session(token: str) -> bool:
    """删除选词会话及其关联的行为日志"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        # 先查 session id
        cursor.execute("SELECT id FROM keyword_selection_sessions WHERE token = %s", (token,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return False
        session_id = row["id"]
        # 删除关联的行为日志
        cursor.execute("DELETE FROM keyword_interaction_logs WHERE session_id = %s", (session_id,))
        # 删除会话本身
        cursor.execute("DELETE FROM keyword_selection_sessions WHERE id = %s", (session_id,))
        conn.commit()
        conn.close()
        return True
    finally:
        try:
            conn.close()
        except Exception: pass


def save_interaction_events(
    session_id: int,
    events: list[dict],
    client_ip: str = None,
    user_agent: str = None,
):
    """批量保存行为事件"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        for ev in events:
            cursor.execute("""
                INSERT INTO keyword_interaction_logs
                    (session_id, phase, event_type, keyword_id, keyword_text, event_data, client_ip, user_agent)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                session_id,
                ev.get("phase", "selection"),
                ev["type"],
                ev.get("keyword_id"),
                ev.get("keyword_text"),
                json.dumps(ev.get("data", {}), ensure_ascii=False) if ev.get("data") else None,
                client_ip,
                user_agent,
            ))
        conn.commit()
        conn.close()
    finally:
        try:
            conn.close()
        except Exception: pass


def get_interaction_logs(session_id: int) -> list[dict]:
    """获取会话的所有行为日志"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM keyword_interaction_logs WHERE session_id = %s ORDER BY created_at",
            (session_id,),
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


# ============================================
# 知识库核查 issue 持久化 helpers (M 方案 · CTO-15.23 2026-05-06)
# ============================================

def _kb_issue_dedup_hash(topic_id: int, issue_type: str, kb_data: str, article_data: str) -> str:
    """生成 issue 去重 hash · 同一文章同一问题点重复 check 不重复入库 · 保留 dismissed 历史"""
    import hashlib
    raw = f"{topic_id}|{issue_type or ''}|{kb_data or ''}|{article_data or ''}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]


def upsert_kb_check_issue(quote_id: int, topic_id: int, article_id: Optional[int],
                           issue_type: str, kb_data: str, article_data: str,
                           context: str = '', severity: str = 'medium') -> dict:
    """入库 / 更新 kb 核查 issue · UNIQUE (quote_id, dedup_hash) 防重复
    返回 {id, dismissed} · 已 dismissed 的 issue 保留 flag · caller 决定是否过滤
    """
    dedup = _kb_issue_dedup_hash(topic_id, issue_type, kb_data, article_data)
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO kb_check_issues
                (quote_id, topic_id, article_id, issue_type, kb_data, article_data,
                 context, severity, dedup_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (quote_id, dedup_hash) DO UPDATE SET
                article_id = EXCLUDED.article_id,
                context = EXCLUDED.context,
                severity = EXCLUDED.severity,
                updated_at = NOW()
            RETURNING id, dismissed
            """,
            (quote_id, topic_id, article_id, issue_type, kb_data, article_data,
             context, severity, dedup),
        )
        row = c.fetchone()
        conn.commit()
        return {"id": row["id"], "dismissed": row["dismissed"]}
    finally:
        try:
            conn.close()
        except Exception: pass


def list_kb_issues_for_quote(quote_id: int, include_dismissed: bool = True,
                              include_fixed: bool = False) -> List[dict]:
    """列 quote 下所有 issue · caller 按 topic_id 自行 group"""
    conn = get_connection()
    try:
        c = conn.cursor()
        sql = "SELECT * FROM kb_check_issues WHERE quote_id = %s"
        params: list = [quote_id]
        if not include_dismissed:
            sql += " AND dismissed = FALSE"
        if not include_fixed:
            sql += " AND fixed_at IS NULL"
        sql += " ORDER BY topic_id, id"
        c.execute(sql, params)
        return [dict(r) for r in c.fetchall()]
    finally:
        try:
            conn.close()
        except Exception: pass


def dismiss_kb_issue(issue_id: int, dismissed: bool = True) -> bool:
    """用户标记 issue 为误报(dismissed=True)或恢复(False)· 不删 · 保留审计 trail"""
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            """
            UPDATE kb_check_issues
            SET dismissed = %s,
                dismissed_at = CASE WHEN %s THEN NOW() ELSE NULL END,
                updated_at = NOW()
            WHERE id = %s
            """,
            (dismissed, dismissed, issue_id),
        )
        ok = c.rowcount > 0
        conn.commit()
        return ok
    finally:
        try:
            conn.close()
        except Exception: pass


def mark_kb_issue_fixed(issue_id: int) -> bool:
    """LLM fix 完成后标记 fixed_at"""
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "UPDATE kb_check_issues SET fixed_at = NOW(), updated_at = NOW() WHERE id = %s",
            (issue_id,),
        )
        ok = c.rowcount > 0
        conn.commit()
        return ok
    finally:
        try:
            conn.close()
        except Exception: pass


# 自动初始化（PostgreSQL: CREATE TABLE IF NOT EXISTS 是幂等的）
init_db()
