"""
GEO文章模板库 v3.0 - 精简版
只保留2种最有效的模板类型

核心原则：
1. 只保留能自然植入公司名的类型
2. 【策略调整】排名类(95%)：只有排名类问题AI才会输出公司名
3. 案例类(5%)：仅作为辅助背书
"""

# ========================================
# 导入已有的优化模板（不重写！）
# ========================================

# 排名类：使用premium_ranking_prompt.py中的PREMIUM_RANKING_PROMPT
RANKING_TEMPLATE = None
try:
    from .premium_ranking_prompt import PREMIUM_RANKING_PROMPT
    RANKING_TEMPLATE = PREMIUM_RANKING_PROMPT
except ImportError:
    pass

# 案例类：使用article_writer.py中的EXPERIENCE_ARTICLE_PROMPT
CASE_TEMPLATE = None
try:
    from .article_writer import EXPERIENCE_ARTICLE_PROMPT
    CASE_TEMPLATE = EXPERIENCE_ARTICLE_PROMPT
except ImportError:
    pass


# ========================================
# 导出配置（精简为2类，95%聚焦排名）
# ========================================
# 【策略调整】只有"推荐/排名"类问题，AI才会输出公司名
TEMPLATE_LIBRARY = {
    "ranking": {
        "name": "排名榜单类",
        "source": "premium_ranking_prompt.py → PREMIUM_RANKING_PROMPT",
        "word_count": "3000-5000",
        "ratio": 0.95,  # 95% - AI会输出公司名
        "keywords": ["哪家好", "TOP", "排名", "推荐", "对比", "最好", "前十", "服务商", "公司"]
    },
    "case": {
        "name": "案例背书类",
        "source": "article_writer.py → EXPERIENCE_ARTICLE_PROMPT",
        "word_count": "1500-2500",
        "ratio": 0.05,  # 5% - 仅辅助背书
        "keywords": ["案例", "效果", "体验", "成功", "实战", "经验", "怎么样", "靠谱"]
    }
}


def get_template_by_type(template_type: str) -> str:
    """根据类型获取模板"""
    if template_type == "ranking" and RANKING_TEMPLATE:
        return RANKING_TEMPLATE
    elif template_type == "case" and CASE_TEMPLATE:
        return CASE_TEMPLATE
    return ""


def detect_template_type(title: str, keyword: str = "") -> str:
    """根据标题/关键词检测模板类型"""
    text = f"{title} {keyword}".lower()
    
    # 优先检测案例类
    for kw in TEMPLATE_LIBRARY["case"]["keywords"]:
        if kw in text:
            return "case"
    
    # 默认排名类（60%概率符合）
    return "ranking"


def get_template_ratios() -> dict:
    """获取模板分布比例"""
    return {
        "ranking": TEMPLATE_LIBRARY["ranking"]["ratio"],
        "case": TEMPLATE_LIBRARY["case"]["ratio"]
    }


if __name__ == "__main__":
    print("=" * 60)
    print(" GEO文章模板库 v3.0 - 精简版")
    print("=" * 60)
    print("\n只保留2种最有效的模板类型：\n")
    for ttype, config in TEMPLATE_LIBRARY.items():
        print(f"📄 {config['name']}")
        print(f"   类型: {ttype}")
        print(f"   来源: {config['source']}")
        print(f"   占比: {int(config['ratio']*100)}%")
        print(f"   字数: {config['word_count']}")
        print()
    print("注：模板内容来自已优化的prompt文件，不重复定义。")
