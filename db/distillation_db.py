"""
数据蒸馏系统数据库模块 V4.2

功能：
- keyword_insights 表：按关键词存储 LLM 蒸馏的结构化竞争情报
- brand_aliases 表：品牌归一化别名映射

设计原则：
- brand_id 为主查询参数（与全局 ClientContext 统一）
- 追加式时序设计：每次监测 INSERT 新行，不覆盖
- 三层分层：真相层(monitoring_results) → 连接层(keyword_insights) → 应用层(API聚合)
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

# 数据库路径（与 monitoring_db 共享同一个 geo_diagnosis.db）
DB_DIR = Path(__file__).parent
DB_PATH = DB_DIR / "geo_diagnosis.db"


def get_connection():
    """获取数据库连接"""
    from db.connection import get_connection as _pg_get_connection
    return _pg_get_connection()


# ==========================================
# keyword_insights CRUD
# ==========================================

def save_keyword_insight(
    task_id: int,
    brand_id: int,
    keyword: str,
    client_id: Optional[str] = None,
    platforms_analyzed: str = "[]",
    brands_found: str = "[]",
    sources_cited: str = "[]",
    response_patterns: str = "{}",
    client_position: str = "{}",
    optimization_hints: str = "[]",
    llm_model: str = "qwen3.7-max",
    llm_tokens: int = 0,
    algorithm_version: str = "v4.2",
    quality_flag: str = "auto",
    feedback_note: Optional[str] = None,
) -> int:
    """
    保存单条关键词蒸馏洞察
    
    使用 INSERT OR REPLACE 实现幂等（基于 UNIQUE(task_id, keyword)）
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO keyword_insights
            (task_id, brand_id, client_id, keyword,
             platforms_analyzed, brands_found, sources_cited,
             response_patterns, client_position, optimization_hints,
             llm_model, llm_tokens, algorithm_version,
             quality_flag, feedback_note, distilled_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (task_id, keyword) DO UPDATE SET
                brand_id = EXCLUDED.brand_id,
                client_id = EXCLUDED.client_id,
                platforms_analyzed = EXCLUDED.platforms_analyzed,
                brands_found = EXCLUDED.brands_found,
                sources_cited = EXCLUDED.sources_cited,
                response_patterns = EXCLUDED.response_patterns,
                client_position = EXCLUDED.client_position,
                optimization_hints = EXCLUDED.optimization_hints,
                llm_model = EXCLUDED.llm_model,
                llm_tokens = EXCLUDED.llm_tokens,
                algorithm_version = EXCLUDED.algorithm_version,
                quality_flag = EXCLUDED.quality_flag,
                feedback_note = EXCLUDED.feedback_note,
                distilled_at = EXCLUDED.distilled_at
            RETURNING id
        """, (
            task_id, brand_id, client_id, keyword,
            platforms_analyzed, brands_found, sources_cited,
            response_patterns, client_position, optimization_hints,
            llm_model, llm_tokens, algorithm_version,
            quality_flag, feedback_note,
            datetime.now().isoformat(),
        ))

        insight_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return insight_id
    finally:
        try:
            conn.close()
        except Exception: pass


def get_keyword_insights(
    brand_id: int,
    days: int = 30,
    keyword: Optional[str] = None,
    quality_filter: Optional[str] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """
    获取关键词蒸馏洞察
    
    Args:
        brand_id: 品牌 ID
        days: 时间范围（天）
        keyword: 可选，筛选特定关键词
        quality_filter: 可选，筛选质量标记 (auto/verified/rejected)
        limit: 最大返回数
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        query = """
            SELECT * FROM keyword_insights
            WHERE brand_id = %s
              AND distilled_at >= NOW() - INTERVAL '%s days'
        """
        params = [brand_id, days]

        if keyword:
            query += " AND keyword = %s"
            params.append(keyword)

        if quality_filter:
            query += " AND quality_flag = %s"
            params.append(quality_filter)

        query += " ORDER BY distilled_at DESC LIMIT %s"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        return [_parse_insight_row(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def get_latest_insights(
    brand_id: int,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """获取最新一次监测的蒸馏结果"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 找最新的 task_id
        cursor.execute("""
            SELECT task_id FROM keyword_insights
            WHERE brand_id = %s
            ORDER BY distilled_at DESC LIMIT 1
        """, (brand_id,))
        row = cursor.fetchone()
    
        if not row:
            conn.close()
            return []
    
        latest_task_id = row["task_id"]
    
        cursor.execute("""
            SELECT * FROM keyword_insights
            WHERE task_id = %s AND brand_id = %s
            ORDER BY keyword
        """, (latest_task_id, brand_id))
    
        rows = cursor.fetchall()
        conn.close()
        return [_parse_insight_row(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def get_keyword_trend(
    brand_id: int,
    keyword: str,
    days: int = 90,
) -> List[Dict[str, Any]]:
    """
    获取特定关键词的时序趋势
    
    返回该关键词在不同时间点的蒸馏结果，用于趋势分析
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM keyword_insights
            WHERE brand_id = %s AND keyword = %s
              AND distilled_at >= NOW() - INTERVAL '%s days'
            ORDER BY distilled_at ASC
        """, (brand_id, keyword, days))

        rows = cursor.fetchall()
        conn.close()
        return [_parse_insight_row(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def update_quality_flag(
    insight_id: int,
    quality_flag: str,
    feedback_note: Optional[str] = None,
) -> bool:
    """
    更新蒸馏结果的质量标记（用户反馈回路）
    
    Args:
        insight_id: 洞察 ID
        quality_flag: 'verified' 或 'rejected'
        feedback_note: 用户标注说明
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE keyword_insights
            SET quality_flag = %s, feedback_note = %s
            WHERE id = %s
        """, (quality_flag, feedback_note, insight_id))

        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def get_insight_brand_id(insight_id: int) -> Optional[int]:
    """获取洞察记录所属的 brand_id"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT brand_id FROM keyword_insights WHERE id = %s", (insight_id,))
        row = cursor.fetchone()
        conn.close()
        return row["brand_id"] if row else None
    finally:
        try:
            conn.close()
        except Exception: pass


def get_competitor_radar(brand_id: int, days: int = 30) -> Dict[str, Any]:
    """
    竞品雷达数据：从 keyword_insights 聚合心智份额
    
    统计每个品牌在所有关键词中的出现频次、平均排名等
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT brands_found, client_position, keyword, distilled_at
            FROM keyword_insights
            WHERE brand_id = %s
              AND distilled_at >= NOW() - INTERVAL '%s days'
              AND quality_flag != 'rejected'
            ORDER BY distilled_at DESC
        """, (brand_id, days))

        rows = cursor.fetchall()
        conn.close()

        # 聚合品牌统计
        brand_stats = {}
        total_keywords = len(rows)
    
        for row in rows:
            try:
                brands = json.loads(row["brands_found"])
            except (json.JSONDecodeError, TypeError):
                continue
        
            for brand in brands:
                name = brand.get("name", "")
                if not name:
                    continue
            
                if name not in brand_stats:
                    brand_stats[name] = {
                        "brand_name": name,
                        "mention_count": 0,
                        "ranks": [],
                        "platforms": set(),
                        "top1_count": 0,
                        "top3_count": 0,
                    }
            
                stats = brand_stats[name]
                stats["mention_count"] += 1
                stats["platforms"].update(brand.get("platforms_mentioned", []))
            
                for platform, rank in brand.get("ranks", {}).items():
                    stats["ranks"].append(rank)
                    if rank == 1:
                        stats["top1_count"] += 1
                    if rank <= 3:
                        stats["top3_count"] += 1

        # 计算聚合指标
        result_brands = []
        total_mentions = sum(s["mention_count"] for s in brand_stats.values())
    
        for name, stats in sorted(brand_stats.items(), key=lambda x: x[1]["mention_count"], reverse=True)[:20]:
            avg_rank = round(sum(stats["ranks"]) / len(stats["ranks"]), 1) if stats["ranks"] else None
            result_brands.append({
                "brand_name": name,
                "mention_count": stats["mention_count"],
                "share_pct": round(stats["mention_count"] / total_mentions * 100, 1) if total_mentions > 0 else 0,
                "avg_rank": avg_rank,
                "top1_count": stats["top1_count"],
                "top3_count": stats["top3_count"],
                "platform_count": len(stats["platforms"]),
            })

        return {
            "brands": result_brands,
            "total_mentions": total_mentions,
            "total_keywords": total_keywords,
            "period_days": days,
        }
    finally:
        try:
            conn.close()
        except Exception: pass


def get_insight_stats(brand_id: int, days: int = 30) -> Dict[str, Any]:
    """获取蒸馏统计概览"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                COUNT(*) as total_insights,
                COUNT(DISTINCT keyword) as unique_keywords,
                COUNT(DISTINCT task_id) as total_tasks,
                SUM(llm_tokens) as total_tokens,
                AVG(llm_tokens) as avg_tokens,
                SUM(CASE WHEN quality_flag = 'auto' THEN 1 ELSE 0 END) as auto_count,
                SUM(CASE WHEN quality_flag = 'verified' THEN 1 ELSE 0 END) as verified_count,
                SUM(CASE WHEN quality_flag = 'rejected' THEN 1 ELSE 0 END) as rejected_count,
                SUM(CASE WHEN quality_flag = 'degraded' THEN 1 ELSE 0 END) as degraded_count,
                MIN(distilled_at) as first_distilled,
                MAX(distilled_at) as last_distilled
            FROM keyword_insights
            WHERE brand_id = %s
              AND distilled_at >= NOW() - INTERVAL '%s days'
        """, (brand_id, days))

        row = cursor.fetchone()
        conn.close()

        if row:
            return {
                "total_insights": row["total_insights"] or 0,
                "unique_keywords": row["unique_keywords"] or 0,
                "total_tasks": row["total_tasks"] or 0,
                "total_tokens": row["total_tokens"] or 0,
                "avg_tokens": round(row["avg_tokens"] or 0),
                "auto_count": row["auto_count"] or 0,
                "verified_count": row["verified_count"] or 0,
                "rejected_count": row["rejected_count"] or 0,
                "degraded_count": row["degraded_count"] or 0,
                "first_distilled": row["first_distilled"],
                "last_distilled": row["last_distilled"],
            }
        return {"total_insights": 0}
    finally:
        try:
            conn.close()
        except Exception: pass


def get_roi_verification(brand_id: int) -> Dict[str, Any]:
    """
    效果验证：基准期 vs 当前期对比
    
    从 keyword_insights 的 client_position 字段聚合
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        periods = {}
        for label, start_offset, end_offset in [
            ("baseline", "-60 days", "-30 days"),
            ("current", "-30 days", "0 days"),
        ]:
            cursor.execute("""
                SELECT client_position
                FROM keyword_insights
                WHERE brand_id = %s
                  AND distilled_at >= NOW() + INTERVAL %s
                  AND distilled_at < NOW() + INTERVAL %s
                  AND quality_flag != 'rejected'
            """, (brand_id, start_offset, end_offset))

            total = 0
            mentioned = 0
            ranks = []
        
            for row in cursor.fetchall():
                total += 1
                try:
                    cp = json.loads(row["client_position"])
                    if cp.get("mentioned_in", 0) > 0:
                        mentioned += 1
                    if cp.get("best_rank"):
                        ranks.append(cp["best_rank"])
                except (json.JSONDecodeError, TypeError):
                    pass
        
            detection_rate = round(mentioned / total * 100, 1) if total > 0 else 0
            avg_rank = round(sum(ranks) / len(ranks), 1) if ranks else None
        
            periods[label] = {
                "total_keywords": total,
                "mentioned_count": mentioned,
                "detection_rate": detection_rate,
                "avg_best_rank": avg_rank,
            }

        conn.close()

        # 计算变化
        delta = {}
        for key in ["detection_rate"]:
            b = periods["baseline"].get(key, 0) or 0
            c = periods["current"].get(key, 0) or 0
            delta[key] = round(c - b, 1)
    
        if periods["baseline"].get("avg_best_rank") and periods["current"].get("avg_best_rank"):
            delta["avg_best_rank"] = round(
                periods["current"]["avg_best_rank"] - periods["baseline"]["avg_best_rank"], 1
            )

        return {
            "baseline": periods["baseline"],
            "current": periods["current"],
            "delta": delta,
        }
    finally:
        try:
            conn.close()
        except Exception: pass


# ==========================================
# brand_aliases CRUD
# ==========================================

def get_all_brand_aliases() -> Dict[str, List[str]]:
    """获取所有品牌别名映射 {canonical_name: [alias1, alias2, ...]}"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("SELECT canonical_name, alias FROM brand_aliases ORDER BY canonical_name")
    
        result = {}
        for row in cursor.fetchall():
            name = row["canonical_name"]
            if name not in result:
                result[name] = []
            result[name].append(row["alias"])
    
        conn.close()
        return result
    finally:
        try:
            conn.close()
        except Exception: pass


def get_known_brand_names() -> List[str]:
    """获取所有已知品牌标准名（用于 LLM Prompt）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT canonical_name FROM brand_aliases")
        names = [row["canonical_name"] for row in cursor.fetchall()]
        conn.close()
        return names
    finally:
        try:
            conn.close()
        except Exception: pass


def add_brand_alias(
    canonical_name: str,
    alias: str,
    brand_id: Optional[int] = None,
    source: str = "manual",
) -> bool:
    """添加品牌别名"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        try:
            cursor.execute(
                """INSERT INTO brand_aliases
                   (canonical_name, alias, brand_id, source)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                (canonical_name, alias, brand_id, source),
            )
            conn.commit()
            added = cursor.rowcount > 0
            conn.close()
            return added
        except Exception as e:
            conn.close()
            print(f"[Distillation DB] 添加品牌别名失败: {e}")
            return False
    finally:
        try:
            conn.close()
        except Exception: pass


def normalize_brand_name(name: str) -> str:
    """通过别名表归一化品牌名"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute(
            "SELECT canonical_name FROM brand_aliases WHERE alias = %s",
            (name,),
        )
        row = cursor.fetchone()
        conn.close()
    
        return row["canonical_name"] if row else name
    finally:
        try:
            conn.close()
        except Exception: pass


# ==========================================
# 辅助函数
# ==========================================

def _parse_insight_row(row) -> Dict[str, Any]:
    """解析单行 keyword_insights 数据"""
    item = dict(row)
    
    # 解析 JSON 字段
    json_fields = [
        "platforms_analyzed", "brands_found", "sources_cited",
        "response_patterns", "client_position", "optimization_hints",
    ]
    for field in json_fields:
        if item.get(field):
            try:
                item[field] = json.loads(item[field])
            except (json.JSONDecodeError, TypeError):
                pass
    
    return item


# 初始化（表已在 Phase A 创建，此处仅验证）
if __name__ == "__main__":
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='keyword_insights'")
    ki = cursor.fetchone()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='brand_aliases'")
    ba = cursor.fetchone()
    
    conn.close()
    print(f"keyword_insights: {'✅ 存在' if ki else '❌ 不存在'}")
    print(f"brand_aliases: {'✅ 存在' if ba else '❌ 不存在'}")
