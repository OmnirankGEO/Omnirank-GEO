"""
蒸馏触发器 V4.2

在监测任务完成后异步触发 LLM 蒸馏：
1. 获取任务结果
2. 按关键词分组（同一关键词 × N 平台）
3. 调用 LLM distiller 批量蒸馏
4. 保存到 keyword_insights
5. 检测异常并写入 notifications

调用方式：asyncio.create_task(trigger_distillation(task_id, client_id))
"""

import json
import logging
from typing import Optional, Dict, List, Any
from collections import defaultdict

from db.connection import get_connection

logger = logging.getLogger("distiller.trigger")


# ==========================================
# 1. 数据准备：从监测结果按关键词分组
# ==========================================

def _get_brand_name(brand_id: int) -> Optional[str]:
    """通过 brand_id 直接获取品牌名（统一化后不再需要反查）"""
    try:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM brands WHERE id = %s", (brand_id,))
            row = cursor.fetchone()
            conn.close()
            return row["name"] if row else None
        finally:
            try:
                conn.close()
            except Exception: pass
    except Exception as e:
        logger.error(f"查询品牌名失败 (brand_id={brand_id}): {e}")
        return None


def _get_quote_ids_for_brand(brand_id: int) -> list:
    """brand_id → [quote_id, ...]  用于获取 confirmed_keywords"""
    try:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM quotes WHERE brand_id = %s", (brand_id,))
            ids = [row["id"] for row in cursor.fetchall()]
            conn.close()
            return ids
        finally:
            try:
                conn.close()
            except Exception: pass
    except Exception as e:
        logger.error(f"查询 quote_ids 失败 (brand_id={brand_id}): {e}")
        return []


def _get_known_brands() -> List[str]:
    """从 brand_aliases 表获取所有已知品牌标准名"""
    try:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT canonical_name FROM brand_aliases")
            brands = [row["canonical_name"] for row in cursor.fetchall()]
            conn.close()
            return brands
        finally:
            try:
                conn.close()
            except Exception: pass
    except Exception as e:
        logger.error(f"获取已知品牌失败: {e}")
        return []


def _group_results_by_keyword(results: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """
    将监测结果按关键词分组
    
    输入: [{'keyword': 'xxx', 'platform': 'doubao', 'full_response': '...', ...}]
    输出: {'关键词A': {'doubao': 'response1', 'kimi': 'response2'}, ...}
    """
    grouped = defaultdict(dict)
    
    for result in results:
        keyword = result.get("keyword", "")
        platform = result.get("platform", "")
        full_response = result.get("full_response", "")
        
        if keyword and platform and full_response:
            grouped[keyword][platform] = full_response
    
    logger.info(f"分组结果: {len(grouped)} 个关键词, "
                f"共 {sum(len(v) for v in grouped.values())} 条响应")
    
    return dict(grouped)


# ==========================================
# 2. 异常检测
# ==========================================

def _detect_anomalies(
    keyword_results: List[tuple],
    brand_name: str,
    task_id: int,
) -> List[Dict[str, str]]:
    """
    检测蒸馏结果中的异常
    
    规则：
    1. 竞品突入：新品牌在 ≥3 平台出现
    2. 客户消失：客户品牌在 ≥2 平台未被提及
    3. 高权重信源：同一 URL 被 ≥3 平台引用
    
    Returns: [{"type": "...", "message": "...", "severity": "warning|critical"}]
    """
    alerts = []
    
    for keyword, result, tokens, quality in keyword_results:
        if quality == "degraded":
            continue
        
        # 规则 1: 客户品牌消失
        cp = result.client_position
        if cp.total_platforms > 0 and cp.mentioned_in == 0:
            alerts.append({
                "type": "brand_disappeared",
                "message": f"⚠️ 关键词「{keyword}」：客户品牌「{brand_name}」"
                           f"在 {cp.total_platforms} 个平台均未被提及",
                "severity": "critical",
            })
        elif cp.total_platforms > 2 and cp.mentioned_in <= 1:
            alerts.append({
                "type": "brand_declining",
                "message": f"⚠️ 关键词「{keyword}」：客户品牌仅在 "
                           f"{cp.mentioned_in}/{cp.total_platforms} 个平台被提及",
                "severity": "warning",
            })
        
        # 规则 2: 竞品突入（在 ≥3 平台排名前 3 的新品牌）
        for brand in result.brands:
            if brand.name == brand_name:
                continue
            top3_count = sum(1 for r in brand.ranks.values() if r <= 3)
            if top3_count >= 3 and len(brand.platforms_mentioned) >= 3:
                alerts.append({
                    "type": "competitor_surge",
                    "message": f"💡 关键词「{keyword}」：竞品「{brand.name}」"
                               f"在 {len(brand.platforms_mentioned)} 个平台 TOP3",
                    "severity": "warning",
                })
        
        # 规则 3: 高权重信源
        for source in result.sources_cited:
            if len(source.cited_by_platforms) >= 3:
                alerts.append({
                    "type": "high_weight_source",
                    "message": f"💡 关键词「{keyword}」：信源「{source.url_or_reference[:50]}」"
                               f"被 {len(source.cited_by_platforms)} 个平台引用",
                    "severity": "info",
                })
    
    return alerts


def _write_notifications(alerts: List[Dict[str, str]], task_id: int, brand_id: int):
    """将异常写入 notifications 表
    
    notifications 表 schema: (client_id, type, title, content, related_id, is_read)
    """
    if not alerts:
        return
    
    try:
        conn = get_connection()
        try:
            cursor = conn.cursor()

            # 检查 notifications 表是否存在
            cursor.execute(
                "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'notifications')"
            )
            if not cursor.fetchone()["exists"]:
                logger.warning("notifications 表不存在，跳过预警写入")
                conn.close()
                return

            for alert in alerts:
                severity_icon = {"critical": "🚨", "warning": "⚠️", "info": "💡"}.get(
                    alert.get("severity", "info"), "💡"
                )
                title = f"{severity_icon} 蒸馏预警: {alert['type']}"
                content = alert.get("message", "")

                cursor.execute(
                    """INSERT INTO notifications
                       (client_id, type, title, content, related_id, is_read)
                       VALUES (%s, %s, %s, %s, %s, 0)""",
                    (
                        brand_id,
                        alert["type"],
                        title,
                        content,
                        task_id,
                    ),
                )

            conn.commit()
            conn.close()
            logger.info(f"已写入 {len(alerts)} 条预警到 notifications")
        finally:
            try:
                conn.close()
            except Exception: pass
    except Exception as e:
        logger.error(f"写入预警失败: {e}")


# ==========================================
# 3. 主触发器
# ==========================================

async def trigger_distillation(task_id: int, brand_id: int):
    """
    异步蒸馏触发器 V5.0（brand_id 统一化）
    
    完整流程：
    1. 直接通过 brand_id 获取品牌名
    2. 获取任务结果 + 按关键词分组
    3. 批量 LLM 蒸馏
    4. 保存到 keyword_insights
    5. 异常检测 → notifications
    """
    try:
        print(f"[Distillation V5.0] 开始蒸馏: task_id={task_id}, brand_id={brand_id}")
        
        # 1. 直接获取品牌名（不再反查 quote）
        brand_name = _get_brand_name(brand_id)
        
        if not brand_name:
            print(f"[Distillation] 未找到品牌信息，跳过 (brand_id={brand_id})")
            return
        
        # 2. 获取任务结果
        from db.monitoring_db import get_aggregate_task_results
        results = get_aggregate_task_results(task_id)
        
        if not results:
            print(f"[Distillation] 任务无结果，跳过 (task_id={task_id})")
            return
        
        # 3. 按关键词分组
        keyword_groups = _group_results_by_keyword(results)
        print(f"[Distillation] {len(keyword_groups)} 个关键词待蒸馏")
        
        if not keyword_groups:
            print(f"[Distillation] 无有效关键词分组，跳过")
            return
        
        # 4. 获取已知品牌列表
        known_brands = _get_known_brands()
        # 确保客户品牌在列表中
        if brand_name not in known_brands:
            known_brands.insert(0, brand_name)
        
        # 5. 批量 LLM 蒸馏
        from tools.distillation.distiller import distill_batch
        
        batch_results = await distill_batch(
            keyword_groups=keyword_groups,
            client_brand=brand_name,
            known_brands=known_brands,
        )
        
        # 6. 保存到 keyword_insights
        from db.distillation_db import save_keyword_insight
        
        saved_count = 0
        for keyword, result, tokens, quality in batch_results:
            try:
                save_keyword_insight(
                    task_id=task_id,
                    brand_id=brand_id,
                    client_id=str(brand_id),  # @deprecated: 兼容期双写
                    keyword=keyword,
                    platforms_analyzed=json.dumps(
                        list(keyword_groups.get(keyword, {}).keys()),
                        ensure_ascii=False,
                    ),
                    brands_found=json.dumps(
                        [b.model_dump() for b in result.brands],
                        ensure_ascii=False,
                    ),
                    sources_cited=json.dumps(
                        [s.model_dump() for s in result.sources_cited],
                        ensure_ascii=False,
                    ),
                    response_patterns=json.dumps(
                        result.response_patterns.model_dump(),
                        ensure_ascii=False,
                    ),
                    client_position=json.dumps(
                        result.client_position.model_dump(),
                        ensure_ascii=False,
                    ),
                    optimization_hints=json.dumps(
                        result.optimization_hints,
                        ensure_ascii=False,
                    ),
                    llm_tokens=tokens,
                    quality_flag=quality,
                )
                saved_count += 1
            except Exception as e:
                logger.error(f"保存洞察失败 [{keyword}]: {e}")
        
        print(f"[Distillation] 蒸馏完成: {saved_count}/{len(batch_results)} 条已保存")
        
        # 7. 异常检测
        alerts = _detect_anomalies(batch_results, brand_name, task_id)
        if alerts:
            print(f"[Distillation] 发现 {len(alerts)} 条异常预警")
            _write_notifications(alerts, task_id, brand_id)
        
        # 8. 更新新发现的品牌别名
        _update_brand_aliases(batch_results)
        
        print(f"[Distillation V4.2] 全流程完成 ✅")
        
    except Exception as e:
        print(f"[Distillation] 蒸馏失败: {e}")
        import traceback
        traceback.print_exc()


def _update_brand_aliases(batch_results: List[tuple]):
    """将 LLM 新发现的品牌自动添加到 brand_aliases（待人工确认）"""
    new_brands = set()
    for keyword, result, tokens, quality in batch_results:
        if quality == "degraded":
            continue
        for brand_name in result.new_brands_discovered:
            if brand_name.strip():
                new_brands.add(brand_name.strip())
    
    if not new_brands:
        return
    
    try:
        conn = get_connection()
        try:
            cursor = conn.cursor()

            added = 0
            for brand in new_brands:
                try:
                    cursor.execute(
                        """INSERT INTO brand_aliases
                           (canonical_name, alias, source)
                           VALUES (%s, %s, 'auto_discovered')
                           ON CONFLICT DO NOTHING""",
                        (brand, brand),
                    )
                    if cursor.rowcount > 0:
                        added += 1
                except Exception:
                    pass

            conn.commit()
            conn.close()

            if added:
                logger.info(f"新发现 {added} 个品牌已添加到 brand_aliases")
        finally:
            try:
                conn.close()
            except Exception: pass
    except Exception as e:
        logger.error(f"更新品牌别名失败: {e}")
