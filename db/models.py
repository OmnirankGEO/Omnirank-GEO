"""
行业分类体系和数据模型
"""

def get_industry_category(industry: str, brand: dict = None) -> str:
    """根据详细行业推断行业大类 —— 返回行业大类字典的**新 key**(WO_267)。

    🔴 这是 `brands.industry_category` / `diagnosis_records.industry_category` 的真实写入方
       (`db/diagnosis_db.py` 保存诊断时算一次,新建品牌与「品牌该列为空才回填」都用它)。
       旧实现在本文件手写一张 11 类表、按子串匹配,「"ai" in "maintenance"」这种 ASCII 误中也算;
       那张表已删,11 个旧类名全部进了 `config/industry_taxonomy.json` 的 legacy_names。
       存量旧值**不回填**(Owner 09-19),读侧用 `services.industry_taxonomy.translate_legacy` 翻译。
    `brand`(可空):品牌行,提供名称 / 备注 / 种子词当判定上下文。判不出 ⇒ "other"。
    """
    from services.industry_taxonomy import resolve_industry

    return resolve_industry(industry or "", brand=brand).category_key


# 等级定义
SCORE_LEVELS = {
    (0, 20): "空白",
    (21, 40): "起步",
    (41, 60): "成长",
    (61, 80): "成熟",
    (81, 100): "领先"
}

def get_level_from_score(score: int) -> str:
    """根据分数获取等级"""
    for (min_score, max_score), level in SCORE_LEVELS.items():
        if min_score <= score <= max_score:
            return level
    return "未知"
