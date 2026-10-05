"""
关键词分级系统 - 数据结构定义

词级说明：
- tier1 (一级词/核心词): 行业+核心诉求，高竞争，需20-30篇内容攻坚
- tier2 (二级词/区域词): 行业+地域，中竞争，需10-15篇内容
- tier3 (三级词/长尾词): 场景/细分需求，低竞争，需3-5篇内容
"""

from typing import Dict, List, TypedDict

# ============= 词级定义 =============

class TierConfig(TypedDict):
    """词级配置"""
    name: str              # 中文名称
    name_en: str           # 英文名称
    competition: str       # 竞争度
    articles_needed: int   # 每词所需文章数
    article_types: List[str]  # 适用的文章类型
    type_ratios: List[float]  # 各类型占比


# ============= 【策略调整】只聚焦排名类 =============
# 
# 重要洞察：只有"推荐/排名"类问题，AI才会输出公司名
# - "XX哪家好？" → AI会推荐具体公司 ✅
# - "XX效果怎么样？" → AI讲方法论，不提公司 ❌
# - "XX多少钱？" → AI讲价格区间，不提公司 ❌
# 
# 因此：所有内容都应生成为ranking/recommendation类型

KEYWORD_TIERS: Dict[str, TierConfig] = {
    "tier1": {
        "name": "一级词（核心词）",
        "name_en": "Core Keywords",
        "competition": "高",
        "articles_needed": 12,  # 高竞争词需要更多内容
        "article_types": ["ranking", "case"],  # 只保留排名+案例
        "type_ratios": [0.95, 0.05]  # 排名95% + 案例5%（作为背书）
    },
    "tier2": {
        "name": "二级词（区域词）",
        "name_en": "Regional Keywords",
        "competition": "中",
        "articles_needed": 6,  # 中竞争词
        "article_types": ["ranking", "case"],
        "type_ratios": [0.95, 0.05]  # 排名95% + 案例5%
    },
    "tier3": {
        "name": "三级词（长尾词）",
        "name_en": "Long-tail Keywords",
        "competition": "低",
        "articles_needed": 2,  # 低竞争词快速占位
        "article_types": ["ranking", "case"],  # 改为排名优先
        "type_ratios": [0.95, 0.05]  # 排名95% + 案例5%
    }
}


# 文章类型描述（只保留2类）
# 移除qa类型原因：AI回答问答类问题时不会输出公司名
ARTICLE_TYPE_INFO = {
    "ranking": {
        "name": "榜单排名",
        "description": "权威TOP榜单、服务商评测、推荐对比",
        "ai_friendly": "⭐⭐⭐⭐⭐",
        "purpose": "让AI推荐时输出客户公司名",
        "triggers": ["哪家好", "推荐", "TOP", "排名", "最好"]
    },
    "case": {
        "name": "案例背书",
        "description": "客户案例、成功故事（仅作为排名文章的背书素材）",
        "ai_friendly": "⭐⭐⭐",  # 降级：AI回答时不直接引用公司名
        "purpose": "增加可信度，辅助排名文章"
    }
    # qa类型已移除：AI回答问答类问题时不会输出公司名
}


# ============= 套餐配额 =============

class PackageQuota(TypedDict):
    """套餐配额"""
    tier1: int           # 一级词数量
    tier2: int           # 二级词数量
    tier3: int           # 三级词数量
    total_articles: int  # 总发布量（含社媒）
    geo_articles: int    # GEO文章数（总量的50%）
    name: str            # 套餐名称
    price: int           # 月费


PACKAGE_QUOTAS: Dict[str, PackageQuota] = {
    "trial": {
        "name": "试用版",
        "tier1": 0,
        "tier2": 1,
        "tier3": 4,
        "total_articles": 40,
        "geo_articles": 20,   # GEO文章20篇，社媒20条
        "price": 3800
    },
    "starter": {
        "name": "入门版",
        "tier1": 0,
        "tier2": 3,
        "tier3": 7,
        "total_articles": 80,
        "geo_articles": 40,   # GEO文章40篇，社媒40条
        "price": 7800
    },
    "standard": {
        "name": "标准版",
        "tier1": 2,
        "tier2": 6,
        "tier3": 12,
        "total_articles": 160,
        "geo_articles": 80,   # GEO文章80篇，社媒80条
        "price": 12800
    },
    "pro": {
        "name": "专业版",
        "tier1": 5,
        "tier2": 15,
        "tier3": 20,
        "total_articles": 300,
        "geo_articles": 150,  # GEO文章150篇，社媒150条
        "price": 19800
    },
    "enterprise": {
        "name": "霸榜版",
        "tier1": 10,
        "tier2": 30,
        "tier3": 40,
        "total_articles": 500,
        "geo_articles": 250,  # GEO文章250篇，社媒250条
        "price": 35000
    }
}


# ============= 辅助函数 =============

def get_tier_config(tier: str) -> TierConfig:
    """获取词级配置"""
    return KEYWORD_TIERS.get(tier, KEYWORD_TIERS["tier3"])


def get_package_quota(package: str) -> PackageQuota:
    """获取套餐配额"""
    return PACKAGE_QUOTAS.get(package, PACKAGE_QUOTAS["standard"])


def calculate_articles_needed(tiered_keywords: Dict[str, List[str]]) -> int:
    """计算关键词所需的总文章数"""
    total = 0
    for tier, keywords in tiered_keywords.items():
        tier_config = get_tier_config(tier)
        total += len(keywords) * tier_config["articles_needed"]
    return total


def validate_keywords_for_package(
    tiered_keywords: Dict[str, List[str]], 
    package: str
) -> tuple[bool, str]:
    """
    验证关键词配置是否符合套餐限制
    
    Returns:
        (is_valid, error_message)
    """
    quota = get_package_quota(package)
    
    tier1_count = len(tiered_keywords.get("tier1", []))
    tier2_count = len(tiered_keywords.get("tier2", []))
    tier3_count = len(tiered_keywords.get("tier3", []))
    
    if tier1_count > quota["tier1"]:
        return False, f"一级词超限: {tier1_count}/{quota['tier1']}"
    if tier2_count > quota["tier2"]:
        return False, f"二级词超限: {tier2_count}/{quota['tier2']}"
    if tier3_count > quota["tier3"]:
        return False, f"三级词超限: {tier3_count}/{quota['tier3']}"
    
    articles_needed = calculate_articles_needed(tiered_keywords)
    if articles_needed > quota["total_articles"]:
        return False, f"总文章数超限: {articles_needed}/{quota['total_articles']}"
    
    return True, ""


def distribute_articles_by_tier(
    tiered_keywords: Dict[str, List[str]],
    total_articles: int
) -> Dict[str, Dict[str, int]]:
    """
    按词级分配文章数量
    
    Returns:
        {
            "keyword1": {"tier": "tier1", "articles": 25, "types": {...}},
            ...
        }
    """
    distribution = {}
    
    for tier, keywords in tiered_keywords.items():
        tier_config = get_tier_config(tier)
        articles_per_kw = tier_config["articles_needed"]
        article_types = tier_config["article_types"]
        
        # 每个文章类型分配的数量
        articles_per_type = articles_per_kw // len(article_types)
        remainder = articles_per_kw % len(article_types)
        
        for kw in keywords:
            type_distribution = {}
            for i, article_type in enumerate(article_types):
                count = articles_per_type + (1 if i < remainder else 0)
                type_distribution[article_type] = count
            
            distribution[kw] = {
                "tier": tier,
                "tier_name": tier_config["name"],
                "total_articles": articles_per_kw,
                "type_distribution": type_distribution
            }
    
    return distribution


# ============= 测试 =============

if __name__ == "__main__":
    # 测试套餐验证
    # 【重要】所有关键词必须是"推荐类"，这样AI才会输出公司名
    test_keywords = {
        "tier1": ["GEO优化哪家好", "GEO服务商推荐", "AI营销公司TOP5"],
        "tier2": ["深圳GEO服务商推荐", "杭州GEO公司哪家好", "上海GEO服务商排名"],
        "tier3": ["中小企业GEO服务商推荐", "B2B行业GEO公司哪家好", "性价比高的GEO服务商"]
    }
    
    print("=== 套餐验证测试 ===")
    for package in PACKAGE_QUOTAS:
        is_valid, error = validate_keywords_for_package(test_keywords, package)
        quota = get_package_quota(package)
        print(f"{quota['name']}: {'✅ 通过' if is_valid else f'❌ {error}'}")
    
    print("\n=== 文章分配测试 ===")
    distribution = distribute_articles_by_tier(test_keywords, 160)
    for kw, config in distribution.items():
        print(f"【{config['tier_name']}】{kw}")
        print(f"  总文章数: {config['total_articles']}")
        print(f"  类型分配: {config['type_distribution']}")
