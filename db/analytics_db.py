"""
运营分析表 — 记录 C 端 AI 推荐的数据驱动过程

目前只有 industry_mapping_log:
  追踪 one_click_geo_plan 里 LLM identify 的行业映射轨迹,
  运营可从中发现:
    1. 哪些品牌 LLM 映射到了 "通用" 兜底 → 下一批该采集的调研行业
    2. 哪些 LLM 原始输出被 bigram 兜底修正 → prompt 可加这些反例
    3. 缓存命中率 → 是否需要调整 TTL

表本身只收敛分析用, 不影响业务流程. 写入失败吞异常, 不抛.
"""

import json
import logging
from typing import Optional

from db.connection import get_db

logger = logging.getLogger("GEO-Analytics")

_INIT_MARKER = "v1_1_industry_mapping_log"


def init_analytics_tables():
    """创建运营分析表 (幂等)"""
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS _migration_markers (
                marker TEXT PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                note TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS industry_mapping_log (
                id BIGSERIAL PRIMARY KEY,
                brand_name TEXT NOT NULL,
                description_preview TEXT,            -- 最多 200 字, 超出截断
                llm_industry TEXT,                   -- LLM 原始输出的 industry
                final_industry TEXT NOT NULL,        -- 映射后最终用于 query 的 industry
                map_source TEXT NOT NULL,            -- llm_direct / bigram_fallback / general_fallback / error_fallback / cached
                cache_hit BOOLEAN NOT NULL DEFAULT FALSE,
                city TEXT,
                core_keywords_json TEXT,             -- JSON 数组序列化
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_imp_final_industry
            ON industry_mapping_log(final_industry, created_at DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_imp_map_source
            ON industry_mapping_log(map_source, created_at DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_imp_created
            ON industry_mapping_log(created_at DESC)
        """)

        cursor.execute(
            "SELECT 1 FROM _migration_markers WHERE marker = %s",
            (_INIT_MARKER,),
        )
        if not cursor.fetchone():
            cursor.execute(
                """
                INSERT INTO _migration_markers (marker, note)
                VALUES (%s, %s)
                ON CONFLICT (marker) DO NOTHING
                """,
                (_INIT_MARKER, "industry_mapping_log (C 端 AI 推荐数据驱动分析)"),
            )
            logger.info("[Analytics] industry_mapping_log 表已创建")


def log_industry_mapping(
    brand_name: str,
    description: str,
    llm_industry: str,
    final_industry: str,
    map_source: str,
    cache_hit: bool,
    city: str = "",
    core_keywords: Optional[list] = None,
) -> None:
    """记录一次品牌 → 调研行业的映射 (吞异常, 不影响业务)"""
    try:
        desc_preview = (description or "")[:200]
        core_kw_json = json.dumps(core_keywords or [], ensure_ascii=False)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO industry_mapping_log (
                    brand_name, description_preview,
                    llm_industry, final_industry, map_source, cache_hit,
                    city, core_keywords_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    brand_name[:200],
                    desc_preview,
                    (llm_industry or "")[:100],
                    final_industry[:100],
                    map_source[:50],
                    bool(cache_hit),
                    (city or "")[:100],
                    core_kw_json[:2000],
                ),
            )
    except Exception as e:
        logger.debug(f"[Analytics] log_industry_mapping 失败 (吞异常): {e}")
