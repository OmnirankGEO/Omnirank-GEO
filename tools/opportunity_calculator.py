"""
AI搜索机会成本计算器
基于5118 API流量数据 + Ahrefs研究转化率

功能：
1. 查询行业关键词的5118流量指数
2. 计算AI搜索月流量
3. 生成漏斗估算
4. 计算年机会成本
"""
from typing import Optional
from dataclasses import dataclass
from .api_5118 import get_5118_client


@dataclass
class FunnelConfig:
    """漏斗配置（基于Ahrefs研究数据）"""
    click_rate: float = 0.02      # AI推荐后点击率 2%（比传统低75%）
    inquiry_rate: float = 0.25    # 点击后咨询转化率 25%（AI用户质量高）
    deal_rate: float = 0.20       # 咨询后成交率 20%
    ai_penetration: float = 0.365 # AI搜索渗透率 36.5%
    # 行业化标签
    step3_label: str = "用户点击搜索品牌"
    step4_label: str = "产生咨询意向"
    step5_label: str = "最终成交"
    step4_unit: str = "个"
    step5_unit: str = "单"
    industry_type: str = "通用"


# 行业漏斗配置表
INDUSTRY_FUNNEL_CONFIGS = {
    # 线下零售/商场/奥特莱斯：搜索→到店→消费
    # 数据来源：Google研究显示76%"near me"搜索者24h内到店；本地搜索28%产生购买
    "零售": FunnelConfig(
        click_rate=0.03,       # AI推荐后了解品牌意向 3%
        inquiry_rate=0.50,     # 到店率 50%（Google: 本地搜索到店率72-76%, 保守取值）
        deal_rate=0.70,        # 到店后消费率 70%（Google: 本地搜索购买率78%, 保守取值）
        step3_label="搜索后了解品牌",
        step4_label="实际到店",
        step5_label="到店消费",
        step4_unit="人次",
        step5_unit="人次",
        industry_type="线下零售"
    ),
    # 汽车/大宗消费：搜索→询价→试驾→成交
    "汽车": FunnelConfig(
        click_rate=0.025,
        inquiry_rate=0.30,
        deal_rate=0.10,
        step3_label="用户点击了解",
        step4_label="留资询价",
        step5_label="成交",
        step4_unit="条",
        step5_unit="单",
        industry_type="汽车"
    ),
    # B2B/企业服务：搜索→留资→商务沟通→签约
    "企业服务": FunnelConfig(
        click_rate=0.02,
        inquiry_rate=0.25,
        deal_rate=0.15,
        step3_label="用户点击了解",
        step4_label="留资咨询",
        step5_label="签约合作",
        step4_unit="条",
        step5_unit="单",
        industry_type="企业服务"
    ),
    # 餐饮/本地生活：搜索→种草→到店（到店率更高）
    "餐饮": FunnelConfig(
        click_rate=0.04,       # 餐饮搜索更高频
        inquiry_rate=0.55,     # 到店率 55%（餐饮搜索→到店比零售更冲动）
        deal_rate=0.80,        # 到店后消费率 80%（餐饮到店几乎必消费）
        step3_label="搜索后种草",
        step4_label="实际到店",
        step5_label="到店消费",
        step4_unit="人次",
        step5_unit="人次",
        industry_type="本地生活"
    ),
}

# 行业关键词→漏斗配置映射
_INDUSTRY_KEYWORDS = {
    "零售": ["奥特莱斯", "购物中心", "商场", "折扣店", "百货", "超市", "便利店", "零售"],
    "汽车": ["汽车", "租车", "二手车", "4S店", "车行", "新能源车"],
    "企业服务": ["SaaS", "企业", "B2B", "咨询", "服务商", "外包", "代运营"],
    "餐饮": ["餐饮", "餐厅", "火锅", "奶茶", "烘焙", "咖啡"],
}


def get_industry_funnel(industry: str) -> FunnelConfig:
    """根据行业智能匹配漏斗配置"""
    industry_lower = industry.lower()
    for category, keywords in _INDUSTRY_KEYWORDS.items():
        for kw in keywords:
            if kw in industry_lower:
                print(f"   🎯 行业漏斗匹配: {industry} → {category}（{INDUSTRY_FUNNEL_CONFIGS[category].industry_type}）")
                return INDUSTRY_FUNNEL_CONFIGS[category]
    # 未匹配到则用通用配置
    print(f"   📊 行业漏斗: {industry} → 通用配置")
    return FunnelConfig()



@dataclass
class OpportunityResult:
    """机会成本计算结果"""
    keyword: str
    
    # 5118原始数据
    pc_index: int = 0
    mobile_index: int = 0
    sem_price: float = 0.0
    
    # 计算结果
    traditional_monthly: int = 0   # 传统搜索月流量
    ai_monthly: int = 0            # AI搜索月流量
    
    # 漏斗数据
    clicks: int = 0                # 点击品牌次数
    inquiries: int = 0             # 产生咨询数
    deals_monthly: int = 0         # 月成交数
    deals_yearly: int = 0          # 年成交数
    
    # 机会成本（需要乘以客单价）
    opportunity_cost_formula: str = ""
    
    def to_dict(self) -> dict:
        return {
            "keyword": self.keyword,
            "pc_index": self.pc_index,
            "mobile_index": self.mobile_index,
            "sem_price": self.sem_price,
            "traditional_monthly": self.traditional_monthly,
            "ai_monthly": self.ai_monthly,
            "clicks": self.clicks,
            "inquiries": self.inquiries,
            "deals_monthly": self.deals_monthly,
            "deals_yearly": self.deals_yearly,
            "opportunity_cost_formula": self.opportunity_cost_formula
        }


class OpportunityCalculator:
    """AI搜索机会成本计算器"""
    
    def __init__(self, config: Optional[FunnelConfig] = None):
        self.config = config or FunnelConfig()
    
    async def calculate_from_keyword(self, keyword: str) -> OpportunityResult:
        """
        从关键词查询5118并计算机会成本
        
        公式：
        - 传统搜索月流量 = (PC指数 + 移动指数) × 30
        - AI搜索月流量 = 传统月流量 × AI渗透率(36.5%)
        - 点击数 = AI月流量 × 点击率(2%)
        - 咨询数 = 点击数 × 咨询率(25%)
        - 月成交 = 咨询数 × 成交率(20%)
        - 年流失 = 月成交 × 12
        """
        result = OpportunityResult(keyword=keyword)
        
        try:
            # 调用5118 API
            client = await get_5118_client()
            api_result = await client.get_keyword_search_volume([keyword])
            
            if api_result.get("success") and api_result.get("keywords"):
                data = api_result["keywords"][0]
                result.pc_index = data.get("index", 0)
                result.mobile_index = data.get("mobile_index", 0)
                result.sem_price = data.get("sem_price", 0.0)
            else:
                # API失败时使用默认值（行业平均）
                print(f"⚠️ 5118 API查询失败，使用默认值: {keyword}")
                result.pc_index = 1000
                result.mobile_index = 1000
        
        except Exception as e:
            print(f"❌ 5118 API异常: {e}")
            result.pc_index = 1000
            result.mobile_index = 1000
        
        # 计算漏斗
        result = self._calculate_funnel(result)
        
        return result
    
    def calculate_from_index(
        self, 
        keyword: str,
        pc_index: int, 
        mobile_index: int,
        sem_price: float = 0.0
    ) -> OpportunityResult:
        """
        直接从指数计算（不调用API）
        用于已有5118数据的场景
        """
        result = OpportunityResult(
            keyword=keyword,
            pc_index=pc_index,
            mobile_index=mobile_index,
            sem_price=sem_price
        )
        return self._calculate_funnel(result)
    
    def _calculate_funnel(self, result: OpportunityResult) -> OpportunityResult:
        """计算漏斗数据"""
        config = self.config
        
        # Step 1: 传统搜索月流量
        daily_index = result.pc_index + result.mobile_index
        result.traditional_monthly = daily_index * 30
        
        # Step 2: AI搜索月流量
        result.ai_monthly = int(result.traditional_monthly * config.ai_penetration)
        
        # Step 3: 漏斗计算
        result.clicks = int(result.ai_monthly * config.click_rate)
        result.inquiries = int(result.clicks * config.inquiry_rate)
        result.deals_monthly = int(result.inquiries * config.deal_rate)
        result.deals_yearly = result.deals_monthly * 12
        
        # Step 4: 机会成本公式
        result.opportunity_cost_formula = f"客单价 × {result.deals_yearly} = 年机会成本"
        
        return result
    
    async def calculate_batch(self, keywords: list[str]) -> list[OpportunityResult]:
        """批量计算多个关键词"""
        results = []
        
        # 批量查询5118
        client = await get_5118_client()
        api_result = await client.get_keyword_search_volume(keywords[:50])
        
        if api_result.get("success"):
            for data in api_result.get("keywords", []):
                result = OpportunityResult(
                    keyword=data.get("keyword", ""),
                    pc_index=data.get("index", 0),
                    mobile_index=data.get("mobile_index", 0),
                    sem_price=data.get("sem_price", 0.0)
                )
                result = self._calculate_funnel(result)
                results.append(result)
        
        return results
    
    def generate_report_section(self, result: OpportunityResult) -> str:
        """
        生成报告用的漏斗Markdown
        """
        return f"""**AI搜索流量漏斗估算**（关键词：{result.keyword}）

| 漏斗层级 | 被AI推荐的品牌 | 您的品牌 |
|:---------|:--------------:|:--------:|
| ① 月搜索量（AI渠道） | {result.ai_monthly:,}次 | {result.ai_monthly:,}次 |
| ② 被AI推荐提及 | 每次都出现 | **0次** |
| ③ 用户点击搜索品牌（2%） | {result.clicks:,}次 | 0次 |
| ④ 产生咨询意向（25%） | {result.inquiries}个 | 0个 |
| ⑤ 最终成交（20%） | **{result.deals_monthly}单/月** | **0单** |

> 📊 **数据来源**：
> - 流量指数来自5118（PC:{result.pc_index} + 移动:{result.mobile_index}）
> - AI渗透率36.5%，点击率2%，咨询转化25%（Ahrefs研究）

> ⚠️ **年流失**：{result.deals_monthly}单/月 × 12个月 = **{result.deals_yearly}单/年**
> 
> **按您的客单价算一下**：{result.opportunity_cost_formula}"""


# ========================================
# 便捷函数
# ========================================
async def calculate_opportunity(keyword: str) -> OpportunityResult:
    """计算单个关键词的机会成本"""
    calculator = OpportunityCalculator()
    return await calculator.calculate_from_keyword(keyword)


async def calculate_opportunity_batch(keywords: list[str]) -> list[OpportunityResult]:
    """批量计算机会成本"""
    calculator = OpportunityCalculator()
    return await calculator.calculate_batch(keywords)


def calculate_opportunity_from_index(
    keyword: str,
    pc_index: int,
    mobile_index: int
) -> OpportunityResult:
    """从已有的5118指数计算（同步方法）"""
    calculator = OpportunityCalculator()
    return calculator.calculate_from_index(keyword, pc_index, mobile_index)


# ========================================
# 测试
# ========================================
if __name__ == "__main__":
    import asyncio
    
    async def test():
        print("测试机会成本计算器...")
        
        # 测试1: 从指数直接计算
        calc = OpportunityCalculator()
        result = calc.calculate_from_index("豪车租赁", 1849, 1848)
        print(f"\n关键词: {result.keyword}")
        print(f"传统月流量: {result.traditional_monthly:,}")
        print(f"AI月流量: {result.ai_monthly:,}")
        print(f"月成交: {result.deals_monthly}")
        print(f"年流失: {result.deals_yearly}")
        print(f"\n报告片段:\n{calc.generate_report_section(result)}")
        
        # 测试2: 调用5118 API
        # result2 = await calculate_opportunity("GEO优化")
        # print(f"\n5118查询结果: {result2.to_dict()}")
    
    asyncio.run(test())
