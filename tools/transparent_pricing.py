"""
透明定价系统 V3 - 基于真实成本+曝光概率

核心逻辑：
1. 调用秘塔搜索获取竞品数量
2. 计算目标占比所需发布篇数
3. 计算发布成本+利润加成
4. 计算预期曝光概率
5. 生成透明报价单

定价公式：
  需要篇数 = 目标占比 × 竞品数 / (1 - 目标占比)
  总成本 = 需要篇数 × 单篇成本
  售价 = 总成本 × markup(默认 1.0=成本·服务商自设利润 · §4.5/决策5 2026-06-06)
"""
import asyncio
import math
from datetime import datetime
from typing import Optional
import sys
sys.path.insert(0, 'geo_agentscope')
from tools.competition_analyzer import search_metaso, distill_result


# ========================================
# 成本配置 - 从系统设置读取
# ========================================

def get_cost_config() -> dict:
    """从系统设置获取成本配置

    [D1 2026-06-05] ¥60/篇 = content_creation(20) + media_publish(30) + operation(10)(get_cost_per_article 求和)。
      这三项是 settings_manager 的【admin 可调字段】(SSOT = 设置 JSON 文件 · UI save_settings 写文件即时生效),
      非写死 — 已满足「¥60 显式化 + admin 可调」。Q8 说的 system_settings DB 表无此行属误诊:settings_manager
      用 JSON 文件持久化,不读 system_settings 表。下方 except 是 settings 加载失败的硬兜底(¥60 不丢)。
      不改 get_dynamic_cost_per_article 的 35-350 权威浮动逻辑。
    """
    try:
        from config.settings_manager import get_current_settings
        settings = get_current_settings()
        return {
            "content_creation": settings.quote_content_cost,
            "media_publish": settings.quote_media_cost,
            "operation": settings.quote_operation_cost,
            "markup_ratio": settings.quote_markup_ratio,
            "ai_reference_count": settings.quote_ai_reference_count,
        }
    except Exception:
        # 降级为默认值(硬兜底 · = ¥60/篇 · settings 加载失败时不阻断报价)
        return {
            "content_creation": 20,
            "media_publish": 30,
            "operation": 10,
            "markup_ratio": 1.0,  # [§4.5/决策5 2026-06-06] 默认回成本(settings 加载失败兜底)
            "ai_reference_count": 4,
        }


# 保留兼容性 - 部分直接引用COST_CONFIG的代码
COST_CONFIG = get_cost_config()


def get_cost_per_article() -> int:
    """获取单篇成本（固定值，向后兼容）"""
    config = get_cost_config()
    return (
        config["content_creation"] +
        config["media_publish"] +
        config["operation"]
    )


# ========================================
# 动态成本 — 根据竞品来源权威度计算加权单篇成本
# ========================================

# 各级别媒体的真实成本（基于外部发布通道27,000+媒体底价 + 内容制作30元overhead）
# 数据来源：内部渠道价格表 2026-03 软文表(15,365个)+自媒体表(10,000个)
# 媒体费取GEO可发中位数，非普通发稿价（GEO可发媒体仅占1.2%，有3.5x溢价）
# [E1 2026-06-05 假设·待校准] 各档成本/overhead 为内部价格表估值 · 上线后按真实采买成本回灌校准 · 本轮不改数值
MEDIA_TIER_COSTS = {
    "S":      350.0,   # 央媒（人民网/新华网/光明网 频道发稿中位737元 → GEO实用取300+overhead50）
    "A":      240.0,   # 行业头部（36氪/虎嗅/钛媒体 中位200元 + overhead40）
    "B":       65.0,   # 门户同步号（搜狐/网易/知乎 GEO可发中位35元 + overhead30）
    "C":       45.0,   # 垂直/头条号（今日头条/CSDN 中位15元 + overhead30）
    "D":       50.0,   # 普通门户（新闻资讯类 中位20元 + overhead30）
    "E":       35.0,   # 自媒体号/SEO站（百家号/UC头条 中位10元 + overhead25）
    "social":  50.0,   # 社媒（B站10元/微博44元/微信75元 加权~20元 + overhead30）
}

# 发布分配策略 — 基于GEO调研7,557条AI引用记录的实际来源分布
# 核心发现：AI引擎最常引用 B级(知乎79.6%/搜狐45.6%/新浪48.1%) 和 C级(今日头条47.8%)
# S/A级央媒虽权威但AI引用率极低，GEO投放中应大幅降低比例
# 原则：以AI实际引用的高ROI平台为主力，适度配置权威媒体做信任背书
_TIER_DISTRIBUTIONS = {
    "S":      {"S": 0.15, "A": 0.15, "B": 0.35, "C": 0.15, "social": 0.20},
    "A":      {"A": 0.10, "B": 0.40, "C": 0.20, "D": 0.10, "social": 0.20},
    "B":      {"B": 0.40, "C": 0.25, "E": 0.15, "social": 0.20},
    "C":      {"B": 0.30, "C": 0.30, "E": 0.20, "social": 0.20},
    "D":      {"B": 0.25, "C": 0.25, "D": 0.15, "E": 0.15, "social": 0.20},
    "E":      {"B": 0.20, "C": 0.20, "E": 0.30, "social": 0.30},
    "social": {"B": 0.15, "C": 0.15, "E": 0.20, "social": 0.50},
}


def get_dynamic_cost_per_article(source_authority_distribution: dict = None) -> float:
    """
    根据竞品来源权威度分布，计算加权平均单篇成本

    Args:
        source_authority_distribution: 秘塔/AI探测结果中各权威等级的数量分布
            例: {"B": 5, "C": 3, "D": 8, "social": 2}
            如果为空或None，返回固定成本（向后兼容）

    Returns:
        加权平均单篇成本
    """
    if not source_authority_distribution:
        return float(get_cost_per_article())

    # 找到竞品来源的中位权威等级
    tiers_ordered = ["S", "A", "B", "C", "D", "E", "social"]
    total = sum(source_authority_distribution.get(t, 0) for t in tiers_ordered)

    if total == 0:
        return float(get_cost_per_article())

    cumulative = 0
    primary_tier = "D"  # 默认
    for tier in tiers_ordered:
        cumulative += source_authority_distribution.get(tier, 0)
        if cumulative >= total * 0.5:
            primary_tier = tier
            break

    # 获取对应的发布分配策略
    distribution = _TIER_DISTRIBUTIONS.get(primary_tier, _TIER_DISTRIBUTIONS["D"])

    # 计算加权平均成本
    blended_cost = sum(
        MEDIA_TIER_COSTS.get(tier, 26.25) * ratio
        for tier, ratio in distribution.items()
    )

    return round(blended_cost, 1)


# ========================================
# 计算函数
# ========================================

def calculate_required_articles(competitor_count: int, target_share: float) -> int:
    """
    计算目标占比所需发布篇数
    
    公式: 需要篇数 = 目标占比 × 竞品数 / (1 - 目标占比)
    
    Args:
        competitor_count: 竞品数量
        target_share: 目标占比 (0-1)
    
    Returns:
        需要发布的文章数
    """
    if target_share >= 1:
        target_share = 0.9
    if target_share <= 0:
        target_share = 0.1
    
    required = target_share * competitor_count / (1 - target_share)
    return max(1, math.ceil(required))


def calculate_exposure_probability(
    competitor_count: int,
    our_articles: int,
    ai_ref_count: int = 4
) -> float:
    """
    计算至少出现1次的概率
    
    使用超几何分布计算
    
    Args:
        competitor_count: 竞品数量
        our_articles: 我们的文章数
        ai_ref_count: AI每次引用的条数
    
    Returns:
        至少出现1次的概率 (0-1)
    """
    total = competitor_count + our_articles
    if total == 0 or ai_ref_count == 0:
        return 0
    
    # 简化计算：使用期望值近似
    # 期望被引用次数 = ai_ref_count × (我们的占比)
    our_share = our_articles / total
    expected_mentions = ai_ref_count * our_share
    
    # 至少1次的概率 ≈ 1 - e^(-expected)（泊松近似）
    probability = 1 - math.exp(-expected_mentions)
    
    return min(0.99, probability)  # 上限99%


def describe_probability(prob: float) -> str:
    """用自然语言描述概率(报价页话术 · 监测/交付/门户用精确%数据驱动)

    [2026-06-05] 阈值比例对齐(前后端统一 · 同 frontend probability.describeProbability / wangjieTerminology):
      三档目标出现率 50/65/75 → 问2次1次 / 问3次2次 / 问4次3次(各异 · 与「目标出现率 X%」并存 · 老板定稿)
    [老板订正 2026-06-05] 套餐解释只准 50/65/75 三档话术 · 「90%+ 几乎每次」禁出现在报价套餐/套餐卡片/合同交付口径。
      本函数被 generate_tiered_quotes(报价)调用 · 无法保证 prob<0.90 → ≥90% 一律改显【具体百分比】不说"几乎每次"。
    """
    if prob >= 0.90:
        # 不在报价/合同露"90%+ 几乎每次"兜底话术 → 统一显示具体百分比(老板定稿)
        return f"约 {min(99, round(prob * 100))}% 被 AI 提及"
    elif prob >= 0.73:
        return "问4次约出现3次"
    elif prob >= 0.58:
        return "问3次约出现2次"
    elif prob >= 0.43:
        return "问2次约出现1次"
    elif prob >= 0.25:
        return "问4次约出现1次"
    else:
        return "偶尔出现"


# ========================================
# 完整定价计算
# ========================================

def calculate_transparent_price(
    competitor_count: int,
    target_share: float = 0.20,
    cost_per_article: int = None
) -> dict:
    """
    透明定价计算
    
    Args:
        competitor_count: 竞品数量（来自秘塔搜索）
        target_share: 目标占比（默认25%）
        cost_per_article: 单篇成本（默认60元）
    
    Returns:
        完整定价信息
    """
    if cost_per_article is None:
        cost_per_article = get_cost_per_article()
    
    # 1. 计算需要发布篇数
    required_articles = calculate_required_articles(competitor_count, target_share)
    
    # 2. 计算成本
    total_cost = required_articles * cost_per_article
    
    # 3. 计算售价（加成）- 从系统设置读取
    config = get_cost_config()
    markup_ratio = config["markup_ratio"]
    selling_price = int(total_cost * markup_ratio)
    
    # 4. 计算曝光概率
    ai_ref = config["ai_reference_count"]
    exposure_prob = calculate_exposure_probability(
        competitor_count, required_articles, ai_ref
    )
    
    # 5. 计算最终占比
    final_share = required_articles / (competitor_count + required_articles)
    
    return {
        "competitor_count": competitor_count,
        "target_share": target_share,
        "required_articles": required_articles,
        "final_share": final_share,
        "cost_per_article": cost_per_article,
        "total_cost": total_cost,
        "markup_ratio": markup_ratio,
        "selling_price": selling_price,
        "exposure_probability": exposure_prob,
        "exposure_description": describe_probability(exposure_prob),
        "profit": selling_price - total_cost
    }


# ========================================
# 多档位报价
# ========================================

def generate_tiered_quotes(competitor_count: int) -> list[dict]:
    """生成多档位报价

    [前台3套餐红线 · 2026-06-05] 前台/客户报价页只有 3 套餐(入门/标准/旗舰 · selection_api.TIER_CONFIG)。
      「霸榜版」(SOV 0.50)= 内部全自动托管/老C端(c_end_cost_estimate)第 4 档 · 非前台套餐。
      本函数仅 transparent_pricing 内部 calculate_* + 老 C 端成本估算用;LIVE 主报价走 batch_pricing/cluster→TierSelector(3 档)。
      grep gate:霸榜版不得进 LIVE selection 3 套餐页。
    """
    tiers = [
        {"name": "入门版", "share": 0.10, "desc": "基础曝光"},
        {"name": "标准版", "share": 0.20, "desc": "稳定曝光"},
        {"name": "旗舰版", "share": 0.30, "desc": "高频曝光"},
        {"name": "霸榜版", "share": 0.50, "desc": "强势占位"},  # 内部托管/老C端 · 非前台套餐
    ]
    
    quotes = []
    for tier in tiers:
        price = calculate_transparent_price(competitor_count, tier["share"])
        price["tier_name"] = tier["name"]
        price["tier_desc"] = tier["desc"]
        quotes.append(price)
    
    return quotes


# ========================================
# Markdown报价单生成
# ========================================

def generate_quote_markdown(
    keyword: str,
    competitor_count: int,
    brand_name: str = "客户"
) -> str:
    """生成透明报价单Markdown"""
    
    quotes = generate_tiered_quotes(competitor_count)
    date_str = datetime.now().strftime("%Y年%m月%d日")
    
    md = f"""# GEO关键词优化透明报价单

**日期**: {date_str}  
**客户**: {brand_name}  
**目标关键词**: {keyword}

---

## 📊 市场竞争分析

当前市场上关于"{keyword}"的相关内容有 **{competitor_count}条**。

为了让您的品牌在AI搜索中被推荐，我们需要发布足够数量的优质内容来占据一定的市场声量。

---

## 💰 套餐报价

| 套餐 | 目标占比 | 发布篇数 | 月费 | 曝光效果 |
|:-----|:--------:|:--------:|-----:|:---------|
"""
    
    for q in quotes:
        md += f"| **{q['tier_name']}** | {int(q['target_share']*100)}% | {q['required_articles']}篇 | **￥{q['selling_price']:,}** | {q['exposure_description']} |\n"
    
    md += f"""
---

## 🎯 效果说明

以**标准版**为例：

- 发布 **{quotes[1]['required_articles']}篇** 优化文章
- 最终占比达到 **{int(quotes[1]['final_share']*100)}%**
- 用户问AI时，**{quotes[1]['exposure_description']}** 会推荐您的品牌
- 曝光概率：**{int(quotes[1]['exposure_probability']*100)}%**

---

## 📋 成本透明

| 项目 | 单价 | 说明 |
|:-----|-----:|:-----|
| 内容创作 | ￥{COST_CONFIG['content_creation']}/篇 | AI写作+人工审核 |
| 媒体发布 | ￥{COST_CONFIG['media_publish']}/篇 | 权威平台发布 |
| 运营管理 | ￥{COST_CONFIG['operation']}/篇 | 监测与优化 |
| **单篇成本** | **￥{get_cost_per_article()}/篇** | |

---

## ✅ 服务承诺

1. **效果可查**：每月提供AI搜索测试报告
2. **内容透明**：所有发布内容可随时查看
3. **退款保障**：效果不达标按比例退款

---

*报价有效期：7天*  
*生成时间：{datetime.now().isoformat()[:19]}*
"""
    
    return md


# ========================================
# 主函数
# ========================================

async def generate_keyword_quote(
    keyword: str,
    brand_name: str = "客户"
) -> tuple[dict, str]:
    """
    生成关键词的完整报价
    
    Args:
        keyword: 目标关键词
        brand_name: 品牌名称
    
    Returns:
        (报价数据, 报价单Markdown)
    """
    # 搜索竞品数量 · [E2 2026-06-05] size 50→100 与主算价路径统一(秘塔取100网页·不足按实际·payload 已 min(size,100) 封顶)
    result = await search_metaso(keyword, size=100)
    
    if "error" in result:
        return {"error": result["error"]}, ""
    
    webpages = result.get("webpages", [])
    competitor_count = len(webpages)
    
    # 生成报价
    quotes = generate_tiered_quotes(competitor_count)
    markdown = generate_quote_markdown(keyword, competitor_count, brand_name)
    
    return {
        "keyword": keyword,
        "competitor_count": competitor_count,
        "quotes": quotes
    }, markdown


# ========================================
# 测试
# ========================================

if __name__ == "__main__":
    import json
    
    async def main():
        keyword = "深圳GEO优化公司推荐"
        brand_name = "全域上榜"
        
        print("=" * 70)
        print(f"🔍 生成透明报价: {keyword}")
        print("=" * 70)
        
        data, md = await generate_keyword_quote(keyword, brand_name)
        
        if "error" in data:
            print(f"❌ 错误: {data['error']}")
            return
        
        print(f"\n竞品数量: {data['competitor_count']}条")
        print("\n📊 多档位报价:")
        for q in data['quotes']:
            print(f"   {q['tier_name']}: ￥{q['selling_price']:,}/月 "
                  f"({q['required_articles']}篇, {q['exposure_description']})")
        
        # 保存报价单
        with open("transparent_quote.md", "w", encoding="utf-8") as f:
            f.write(md)
        print("\n✅ 报价单已保存到 transparent_quote.md")
        
        # 保存数据
        with open("quote_data.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print("✅ 数据已保存到 quote_data.json")
    
    asyncio.run(main())
