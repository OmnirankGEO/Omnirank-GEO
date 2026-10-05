"""
v3.4 反哺机制 — brand_strategies 通用规律 → industry_knowledge L2 公共库

机制（v2 第四章）：
  1. 检查 share_consent（v2 默认 TRUE 静默开启）
  2. 脱敏：去掉品牌名 / 价格 / 内部数据
  3. 提取通用规律
  4. 写入 industry_knowledge.knowledge['successful_patterns']
  5. 累计 contributed_by_brand_count

效果（v2 战略）：
  - 同行业新用户上线即享受历史品牌沉淀
  - "数据贡献者" → "数据受益者"双向飞轮
  - 越多客户用 → 公共库越准 → 新客户体验越好
"""

import json
import logging
from typing import Optional

logger = logging.getLogger("GEO-Managed-Reflow")


# 脱敏关键词模式（任何包含这些的字段都视为敏感）
SENSITIVE_FIELDS = {
    "brand_name", "company_name", "client_name",
    "price", "cost", "yuan", "money",
    "contact", "phone", "email",
    "internal", "secret", "confidential",
}


async def reflow_to_industry_knowledge(brand_id: int, strategy: dict) -> Optional[dict]:
    """
    脱敏并反哺品牌策略到公共库

    Args:
        brand_id: 品牌 ID
        strategy: brand_strategies 表行（含 share_consent / shareable_patterns）

    Returns:
        写入 industry_knowledge 的记录 dict，或 None
    """
    if not strategy:
        return None

    if not strategy.get("share_consent", True):
        logger.info(f"[Reflow] 品牌 {brand_id} 未授权反哺，跳过")
        return None

    shareable = strategy.get("shareable_patterns")
    if not shareable:
        logger.info(f"[Reflow] 品牌 {brand_id} 无 shareable_patterns，跳过")
        return None

    # 1. 脱敏
    cleaned = _anonymize(shareable)

    # 2. 提取通用规律
    patterns = _extract_general_patterns(cleaned, strategy)

    # 3. 取品牌的 industry / category
    industry, category = _get_brand_industry_category(brand_id)
    if not industry:
        logger.warning(f"[Reflow] 品牌 {brand_id} 无行业信息，跳过反哺")
        return None

    # 4. 写入 industry_knowledge
    saved = _upsert_industry_pattern(
        industry=industry,
        category=category,
        patterns=patterns,
    )

    logger.info(f"[Reflow] 品牌 {brand_id} → 行业 {industry}/{category} 反哺完成")
    return saved


# ============================================================
# 脱敏
# ============================================================

def _anonymize(data: dict) -> dict:
    """递归去除敏感字段（品牌名/价格/联系方式等）"""
    if not isinstance(data, dict):
        return data

    cleaned: dict = {}
    for k, v in data.items():
        # 字段名敏感 → 跳过
        if any(s in k.lower() for s in SENSITIVE_FIELDS):
            continue
        # 字符串值含敏感模式 → 替换
        if isinstance(v, str):
            cleaned[k] = _anonymize_string(v)
        elif isinstance(v, dict):
            cleaned[k] = _anonymize(v)
        elif isinstance(v, list):
            cleaned[k] = [_anonymize(item) if isinstance(item, dict) else _anonymize_string(item) if isinstance(item, str) else item for item in v]
        else:
            cleaned[k] = v
    return cleaned


def _anonymize_string(text: str) -> str:
    """字符串脱敏（替换价格数字等）"""
    import re
    # 替换 ¥XX / X.X 万 等数字
    text = re.sub(r"¥\s*[\d,]+(\.\d+)?", "¥XXX", text)
    text = re.sub(r"\d+(\.\d+)?\s*万", "XX 万", text)
    # 替换电话邮箱
    text = re.sub(r"\d{11}", "[手机号]", text)
    text = re.sub(r"[\w._-]+@[\w.-]+\.\w+", "[邮箱]", text)
    return text


# ============================================================
# 提取规律
# ============================================================

def _extract_general_patterns(cleaned: dict, full_strategy: dict) -> dict:
    """
    从脱敏后的 shareable_patterns + 完整 strategy 中提取通用规律

    输出标准化结构：
    {
      "platform_effectiveness": {...},   # 平台效果排名
      "content_type_preference": "...",
      "industry_insights": ["..."],
    }
    """
    patterns: dict = {
        "platform_effectiveness": {},
        "content_type_preference": None,
        "industry_insights": [],
    }

    # 平台效果（top_platforms）— 已经是脱敏的统计数据，可直接用
    top_platforms = full_strategy.get("top_performing_platforms")
    if isinstance(top_platforms, dict):
        patterns["platform_effectiveness"] = top_platforms

    # 内容类型偏好
    pref_type = full_strategy.get("preferred_content_type")
    if pref_type:
        patterns["content_type_preference"] = pref_type

    # 已脱敏的 insights
    for k, v in cleaned.items():
        if isinstance(v, str) and v:
            patterns["industry_insights"].append(v)

    return patterns


# ============================================================
# 写入公共库
# ============================================================

def _get_brand_industry_category(brand_id: int) -> tuple[Optional[str], Optional[str]]:
    """读品牌的 industry / category"""
    from db.connection import get_connection

    conn = get_connection()
    try:
        cursor = conn.cursor()
        # 优先 client_profiles
        cursor.execute(
            "SELECT industry, category FROM client_profiles WHERE brand_id = %s LIMIT 1",
            (brand_id,),
        )
        row = cursor.fetchone()
        if row:
            return row.get("industry"), row.get("category")
        # fallback: brands 表
        cursor.execute(
            "SELECT industry FROM brands WHERE id = %s LIMIT 1",
            (brand_id,),
        )
        row = cursor.fetchone()
        if row:
            return row.get("industry"), None
        return None, None
    except Exception as e:
        logger.warning(f"_get_brand_industry_category 异常: {e}")
        return None, None
    finally:
        conn.close()


def _upsert_industry_pattern(
    industry: str,
    category: Optional[str],
    patterns: dict,
) -> Optional[dict]:
    """写入 industry_knowledge.knowledge['successful_patterns']"""
    from db.connection import get_db

    level = "category" if category else "industry"

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            # 读现有
            cursor.execute(
                """
                SELECT id, knowledge
                FROM industry_knowledge
                WHERE level = %s AND industry = %s
                  AND COALESCE(category, '') = COALESCE(%s, '')
                LIMIT 1
                """,
                (level, industry, category),
            )
            row = cursor.fetchone()

            if row:
                # 更新已有 — merge patterns
                existing = row.get("knowledge") or {}
                if isinstance(existing, str):
                    try:
                        existing = json.loads(existing)
                    except Exception:
                        existing = {}
                successful = existing.get("successful_patterns", {})
                # 合并 platform_effectiveness（按出现频次累加）
                if "platform_effectiveness" in patterns:
                    cur = successful.get("platform_effectiveness", {})
                    for k, v in patterns["platform_effectiveness"].items():
                        cur[k] = round((cur.get(k, 0) + float(v)) / 2, 2)  # 滑动平均
                    successful["platform_effectiveness"] = cur
                # 合并 industry_insights（去重）
                if "industry_insights" in patterns:
                    cur_list = successful.get("industry_insights", [])
                    for s in patterns["industry_insights"]:
                        if s not in cur_list:
                            cur_list.append(s)
                    successful["industry_insights"] = cur_list[-20:]  # 最多保留最近 20 条
                # content_type_preference 直接覆盖
                if patterns.get("content_type_preference"):
                    successful["content_type_preference"] = patterns["content_type_preference"]

                existing["successful_patterns"] = successful
                cursor.execute(
                    """
                    UPDATE industry_knowledge
                    SET knowledge = %s,
                        version = version + 1,
                        generated_at = NOW(),
                        correction_count = correction_count + 1
                    WHERE id = %s
                    RETURNING *
                    """,
                    (json.dumps(existing), row["id"]),
                )
            else:
                # 新建
                knowledge = {"successful_patterns": patterns}
                cursor.execute(
                    """
                    INSERT INTO industry_knowledge (
                      level, industry, category, knowledge, source,
                      version, generated_at, search_count, correction_count
                    )
                    VALUES (%s, %s, %s, %s, 'reflow_v3.4', 1, NOW(), 0, 1)
                    RETURNING *
                    """,
                    (level, industry, category, json.dumps(knowledge)),
                )
            saved = cursor.fetchone()
            return dict(saved) if saved else None
    except Exception as e:
        logger.warning(f"_upsert_industry_pattern 失败: {e}")
        return None
