"""
LLM驱动的通用长尾词矩阵生成器 V2.0

核心特性：
1. LLM动态生成维度池（适应任何行业）
2. 通用提示词模板
3. 从客户数据中提取行业特征
4. 专注榜单/排名类长尾词（95%触发AI推荐）

使用方式：
    generator = LLMDrivenLongtailGenerator(client_data)
    result = await generator.generate_full_matrix()
"""

import os
import json
import httpx
import asyncio
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime

# LLM配置
DASHSCOPE_API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
MODEL_NAME = "deepseek-v4-flash"  # 2026-05-09 升级 v3.2 → v4-flash · 长尾批量速度优先


# ============= 通用提示词模板 =============

DIMENSION_EXTRACTION_PROMPT = """你是一位专业的长尾词研究专家。请根据以下客户数据，为该行业生成长尾词维度池。

【客户信息】
{client_info}

【任务】
分析客户所在行业的特征，生成6个维度的长尾词池，用于后续生成榜单/排名类文章标题。

【核心原则】
1. 所有长尾词必须是"推荐/排名"类 - 这样AI搜索时才会输出公司名
2. 长尾词结尾词必须能触发AI推荐：哪家好、推荐、排名、TOP、有哪些靠谱的
3. 维度要贴合该行业的实际用户搜索习惯

【⚠️ AI平台维度严格限定】
platforms_or_scenarios 如果涉及AI平台，只能使用以下7个真实存在的主流AI搜索平台：
- DeepSeek（深度求索）
- Kimi（月之暗面）
- 豆包（字节跳动）
- 千问/通义千问（阿里）
- 文心一言（百度）
- ChatGPT（OpenAI）
- Gemini（Google）

❌ 禁止编造不存在的AI平台（如"快手AI搜索"、"抖音AI搜索"、"小红书AI搜索"等）
✅ 如果行业不涉及AI平台，platforms_or_scenarios应该用该行业的真实场景/渠道

【输出格式】
严格输出JSON，不要其他文字：
```json
{{
    "industry_name": "客户所在行业名称",
    "core_words": ["主词1", "主词2", "主词3", "主词4", "主词5"],
    "dimensions": {{
        "regions": {{
            "cities": ["上海", "深圳", "北京", "广州", "杭州", "成都", "武汉", "南京"],
            "areas": ["华南", "华东", "华北", "长三角", "珠三角", "大湾区"]
        }},
        "verticals": ["该行业下的细分领域1", "细分领域2", "细分领域3", "细分领域4", "细分领域5", "细分领域6"],
        "platforms_or_scenarios": ["平台/场景1", "平台/场景2", "平台/场景3", "平台/场景4", "平台/场景5"],
        "company_sizes": ["中小企业", "大企业", "初创公司", "上市公司", "集团企业"],
        "needs": ["需求场景1", "需求场景2", "需求场景3", "需求场景4", "需求场景5"],
        "features": ["效果好的", "靠谱的", "性价比高的", "专业的", "有案例的", "口碑好的"]
    }},
    "ranking_suffixes": ["哪家好", "推荐", "服务商推荐", "公司推荐", "排名", "TOP榜单", "有哪些靠谱的"],
    "title_templates": {{
        "tier1_region": [
            "{{年份}}年{{地域}}{{行业}}服务商图谱：{{N}}大厂商评估与选型",
            "{{年份}}年{{地域}}{{行业}}服务商能力画像与企业选型建议"
        ],
        "tier1_vertical": [
            "{{年份}}年{{细分领域}}{{行业}}服务商推荐：专业厂商评估",
            "{{细分领域}}企业如何选择{{行业}}服务商？{{年份}}年选型指南"
        ],
        "tier2_region_vertical": [
            "{{年份}}年{{地域}}{{细分领域}}{{行业}}服务商推荐：{{N}}家厂商能力评估",
            "{{细分领域}}行业选{{地域}}{{行业}}服务商：{{年份}}年选型避坑指南"
        ],
        "tier2_size_need": [
            "{{规模}}如何选择{{行业}}服务商？{{年份}}年{{需求}}型服务商对比",
            "{{规模}}{{需求}}首选的{{行业}}服务商：{{年份}}年能力评估报告"
        ],
        "tier2_platform_vertical": [
            "{{平台}}+{{细分领域}}{{行业}}服务商推荐：{{年份}}年专业厂商评估",
            "如何在{{平台}}获得{{细分领域}}推荐？{{年份}}年{{行业}}服务商选型"
        ]
    }}
}}
```

【示例 - 包车行业】
如果客户是"豪车租赁/包车"行业：
- core_words: ["包车", "豪车配司机", "豪车配驾", "接送机", "豪车租赁"]
- verticals: ["埃尔法", "奔驰V260", "迈巴赫", "商务接待", "机场接送", "婚车"]
- platforms_or_scenarios: ["虹桥机场", "浦东机场", "迪士尼", "商务出行", "婚庆用车"]（注意：包车行业不涉及AI平台）
- needs: ["长途包车", "日租", "半日租", "机场接送", "企业通勤"]

【示例 - GEO优化行业】
如果客户是"GEO生成式引擎优化"行业：
- core_words: ["GEO优化", "AI搜索优化", "生成式引擎优化", "AI可见度提升"]
- verticals: ["外贸", "B2B", "电商", "品牌营销", "跨境电商"]
- platforms_or_scenarios: ["DeepSeek", "Kimi", "豆包", "千问", "文心一言", "ChatGPT", "Gemini"]（仅限这7个主流AI平台）
- needs: ["品牌曝光", "精准获客", "口碑建设", "竞品超越"]

请根据客户实际行业生成对应的维度池："""


TITLE_GENERATION_PROMPT = """你是一位深谙用户心理的内容策略师。请根据以下维度池，生成{count}个**贴合真实搜索场景**的榜单/排名类文章标题。

【维度池数据】
{dimensions_json}

【⚠️ 客户区域锁定 - 非常重要】
客户公司位于：{client_location}

区域使用策略：
- **60%的标题必须使用客户所在区域**（如客户在深圳，则使用"深圳/华南/珠三角/大湾区"）
- **40%的标题可以使用其他区域**，但这类标题不能推荐"XX家公司"，而是用"行业观察"、"趋势分析"的角度
- **关键：当推荐具体公司时，地域必须与客户一致！**

示例（假设客户在深圳）：
✅ 正确："深圳GEO优化公司哪家靠谱？实地聊了7家，只推荐这3个"
✅ 正确："华南制造业老板：同行靠GEO接到AI推荐订单了，你还在等什么？"
✅ 正确："大湾区企业注意：AI搜索流量已经超过百度了！这5家GEO公司能帮你"
✅ 正确（其他区域但不推荐公司）："上海电商都在做GEO了，2026年这个趋势你必须懂"
❌ 错误："长三角这3家GEO公司专治这个"（把竞品定位到长三角了！）

【核心洞察 - 真人怎么问AI】
用户用AI搜索时，不会说"2026年华南GEO优化服务商图谱"，而是会说：
- "怎么让DeepSeek/Kimi推荐我的品牌？"
- "AI时代不知道怎么推广，有什么新方法吗？"
- "深圳有哪些靠谱的GEO优化公司？最好有案例的"
- "同行都在做GEO了，我不懂要不要做？"

【标题风格要求】
1. **口语化** - 像真人在聊天，不像研报
2. **痛点驱动** - 戳中焦虑/好奇心
3. **场景具体** - 有地域/行业/平台的具象场景
4. **悬念感** - 让人想点进去看
5. **混合风格** - 不要都是一个套路

【标题公式参考】（灵活使用，不要全用一种）

**痛点+解决方案型：**
- "AI搜索搜不到你的品牌？试试这5家{{客户区域}}GEO公司"
- "同行都在Kimi/DeepSeek有曝光，你还没做GEO？"
- "{{行业}}企业获客难？2026年GEO可能是突破口"

**好奇心触发型：**
- "深度拆解：{{客户区域}}5大GEO优化公司到底怎么做的"
- "AI时代的"作弊"获客方式：GEO优化深度评测"
- "为什么问Kimi推荐{{行业}}品牌，它只推那几家？"

**直接疑问型（匹配搜索习惯）：**
- "怎么让豆包/千问推荐我的品牌？找这类公司就对了"
- "{{客户区域}}GEO优化公司哪家靠谱？有案例的那种"
- "{{行业}}做GEO效果怎么样？值不值得投？"

**对比评测型：**
- "{{客户区域}}{{行业}}GEO服务商对比：我们实际考察了7家"
- "这5家GEO公司我都咨询过，说说真实感受"
- "问了3个AI推荐{{行业}}服务商，结果都不一样"

**紧迫感型：**
- "2026年了还没做GEO？看看{{客户区域}}同行已经怎么"作弊"了"
- "{{行业}}公司注意：AI搜索流量已经超过百度了"
- "错过抖音后别再错过GEO：{{客户区域}}服务商实测"

【禁止】
- ❌ 太正式的标题（如"服务商图谱"、"能力画像"、"企业选型建议"）
- ❌ 重复使用同一种公式
- ❌ 没有情绪钩子的干巴标题
- ❌ 不接地气的学术表达
- ❌ 在非客户区域推荐"XX家公司"（这会把竞品定位到其他区域！）

【输出格式】
严格输出JSON数组：
```json
[
    {{
        "id": 1,
        "keyword": "用户可能搜索的关键词/问题",
        "title": "贴合口语的自然标题",
        "tier": "tier1或tier2",
        "dimension": "使用的维度类型",
        "style": "痛点型/好奇型/疑问型/对比型/紧迫型",
        "type": "ranking"
    }},
    ...
]
```

请生成{count}个**风格多样、贴近真人搜索**的标题："""


class LLMDrivenLongtailGenerator:
    """LLM驱动的通用长尾词生成器"""
    
    def __init__(self, client_data: Dict[str, Any], client_location: str = None):
        """
        初始化生成器
        
        Args:
            client_data: 客户数据，可以是蒸馏后的数据或原始输入
            client_location: 客户公司所在地（如"深圳"），用于区域锁定
        """
        self.client_data = client_data
        self.api_key = os.getenv("DASHSCOPE_API_KEY")
        self.dimensions = None  # LLM生成的维度池
        
        # 提取客户位置（优先使用传入参数，否则从数据中提取）
        self.client_location = client_location or self._extract_client_location()
    
    def _extract_client_location(self) -> str:
        """从客户数据中提取公司位置"""
        # 优先从additional_info中提取
        additional_info = self.client_data.get("additional_info", "")
        if "深圳" in additional_info:
            return "深圳/华南/珠三角/大湾区"
        if "上海" in additional_info:
            return "上海/华东/长三角"
        if "北京" in additional_info:
            return "北京/华北"
        if "杭州" in additional_info:
            return "杭州/华东/长三角"
        if "广州" in additional_info:
            return "广州/华南/珠三角/大湾区"
        
        # 从client_profile提取
        profile = self.client_data.get("client_profile", {})
        if isinstance(profile, str):
            import json
            try:
                profile = json.loads(profile)
            except:
                pass
        if isinstance(profile, dict):
            location = profile.get("location", "") or profile.get("city", "")
            if location:
                return location
        
        # 默认返回全国
        return "全国"
    
    async def _call_llm(self, prompt: str, max_tokens: int = 4000) -> str:
        """调用LLM"""
        if not self.api_key:
            raise ValueError("请设置 DASHSCOPE_API_KEY 环境变量")
        
        from tools.llm_call_tracker import llm_track, usage_from_response_payload

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with llm_track("longtail_matrix", "dashscope", model=MODEL_NAME) as tracker:
                response = await client.post(
                    DASHSCOPE_API_URL,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": MODEL_NAME,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.7,
                        "max_tokens": max_tokens
                    }
                )
                if response.status_code == 200:
                    result_for_usage = response.json()
                    input_tokens, output_tokens, cached_tokens = usage_from_response_payload(result_for_usage)
                    tracker.record(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cached_tokens=cached_tokens,
                        success=True,
                    )
                else:
                    tracker.record(success=False, error_msg=f"HTTP {response.status_code}: {response.text[:200]}")
            response.raise_for_status()
            result = response.json()
            return result["choices"][0]["message"]["content"]
    
    def _extract_client_info(self) -> str:
        """从客户数据中提取关键信息"""
        info_parts = []
        
        # 品牌名
        brand = self.client_data.get("brand_name", "")
        if brand:
            info_parts.append(f"品牌名称：{brand}")
        
        # 行业
        industry = self.client_data.get("industry", "")
        if not industry and "client_profile" in self.client_data:
            profile = self.client_data["client_profile"]
            if isinstance(profile, str):
                try:
                    profile = json.loads(profile)
                except:
                    pass
            if isinstance(profile, dict):
                industry = profile.get("industry", "")
        if industry:
            info_parts.append(f"所在行业：{industry}")
        
        # 核心业务
        core_business = self.client_data.get("core_business", "")
        if not core_business and "client_profile" in self.client_data:
            profile = self.client_data["client_profile"]
            if isinstance(profile, dict):
                core_business = profile.get("core_business", "")
        if core_business:
            info_parts.append(f"核心业务：{core_business}")
        
        # 关键词
        keywords = self.client_data.get("keywords", [])
        if keywords:
            info_parts.append(f"核心关键词：{', '.join(keywords[:5])}")
        
        # 目标客户
        target = self.client_data.get("target_customers", "")
        if not target and "client_profile" in self.client_data:
            profile = self.client_data["client_profile"]
            if isinstance(profile, dict):
                target = profile.get("target_customer", "")
        if target:
            info_parts.append(f"目标客户：{target}")
        
        # 卖点
        if "selling_points" in self.client_data:
            sp = self.client_data["selling_points"]
            if isinstance(sp, str) and len(sp) > 100:
                info_parts.append(f"核心卖点摘要：{sp[:500]}...")
        
        return "\n".join(info_parts) if info_parts else "未提供详细信息"
    
    async def extract_dimensions(self) -> Dict[str, Any]:
        """Step 1: 使用LLM提取行业维度池"""
        print("🔍 Step 1: 使用LLM分析行业特征，生成维度池...")
        
        client_info = self._extract_client_info()
        prompt = DIMENSION_EXTRACTION_PROMPT.format(client_info=client_info)
        
        content = await self._call_llm(prompt)
        
        # 解析JSON
        import re
        clean = re.sub(r'```json\s*|\s*```', '', content).strip()
        self.dimensions = json.loads(clean)
        
        print(f"   ✅ 识别行业: {self.dimensions.get('industry_name', '未知')}")
        print(f"   ✅ 主词: {', '.join(self.dimensions.get('core_words', [])[:3])}...")
        print(f"   ✅ 细分领域: {len(self.dimensions.get('dimensions', {}).get('verticals', []))}个")
        print(f"   ✅ 平台/场景: {len(self.dimensions.get('dimensions', {}).get('platforms_or_scenarios', []))}个")
        
        return self.dimensions
    
    async def generate_titles(self, count: int = 40) -> List[Dict[str, Any]]:
        """Step 2: 使用LLM生成标题"""
        if not self.dimensions:
            await self.extract_dimensions()
        
        print(f"\n📝 Step 2: 使用LLM生成{count}个榜单类标题...")
        
        prompt = TITLE_GENERATION_PROMPT.format(
            dimensions_json=json.dumps(self.dimensions, ensure_ascii=False, indent=2),
            count=count,
            client_location=self.client_location
        )
        
        print(f"   📍 客户区域锁定: {self.client_location}")
        
        content = await self._call_llm(prompt, max_tokens=8000)
        
        # 解析JSON
        import re
        clean = re.sub(r'```json\s*|\s*```', '', content).strip()
        titles = json.loads(clean)
        
        print(f"   ✅ 生成标题: {len(titles)}个")
        
        # 统计维度分布
        tier1_count = sum(1 for t in titles if t.get("tier") == "tier1")
        tier2_count = sum(1 for t in titles if t.get("tier") == "tier2")
        print(f"   ✅ 一级词标题: {tier1_count}个")
        print(f"   ✅ 二级词标题: {tier2_count}个")
        
        return titles
    
    async def generate_full_matrix(self, title_count: int = 40) -> Dict[str, Any]:
        """完整流程：维度提取 + 标题生成"""
        print("\n" + "=" * 60)
        print("🚀 LLM驱动的通用长尾词矩阵生成器")
        print("=" * 60)
        
        # Step 1: 提取维度
        dimensions = await self.extract_dimensions()
        
        # Step 2: 生成标题
        titles = await self.generate_titles(title_count)
        
        # 统计
        tier1_titles = [t for t in titles if t.get("tier") == "tier1"]
        tier2_titles = [t for t in titles if t.get("tier") == "tier2"]
        
        result = {
            "generated_at": datetime.now().isoformat(),
            "industry": dimensions.get("industry_name", ""),
            "dimensions": dimensions,
            "tier1_titles": tier1_titles,
            "tier2_titles": tier2_titles,
            "all_titles": titles,
            "summary": {
                "total_titles": len(titles),
                "tier1_count": len(tier1_titles),
                "tier2_count": len(tier2_titles),
                "core_words": dimensions.get("core_words", []),
                "verticals_count": len(dimensions.get("dimensions", {}).get("verticals", [])),
                "platforms_count": len(dimensions.get("dimensions", {}).get("platforms_or_scenarios", []))
            }
        }
        
        print("\n" + "=" * 60)
        print("📊 生成完成!")
        print(f"   - 行业: {result['industry']}")
        print(f"   - 总标题: {result['summary']['total_titles']}个")
        print(f"   - 一级词: {result['summary']['tier1_count']}个")
        print(f"   - 二级词: {result['summary']['tier2_count']}个")
        print("=" * 60)
        
        return result


# ============= 测试 =============
async def test_with_omnirank_data():
    """使用全域上榜数据测试"""
    from dotenv import load_dotenv
    load_dotenv()
    
    # 加载蒸馏数据
    distilled_file = "output/omnirank_full_pipeline/20260112_081506/02_distilled_data.json"
    
    if os.path.exists(distilled_file):
        print(f"� 加载蒸馏数据: {distilled_file}")
        with open(distilled_file, "r", encoding="utf-8") as f:
            client_data = json.load(f)
    else:
        print("⚠️ 使用默认测试数据")
        client_data = {
            "brand_name": "全域上榜",
            "industry": "GEO生成式引擎优化",
            "keywords": ["GEO优化", "AI搜索优化", "生成式引擎优化"],
            "core_business": "GEO生成式引擎优化 + 社媒搜索优化",
            "target_customers": "中大型企业"
        }
    
    # 生成（全域上榜是深圳公司）
    generator = LLMDrivenLongtailGenerator(
        client_data, 
        client_location="深圳/华南/珠三角/大湾区"
    )
    result = await generator.generate_full_matrix(title_count=40)
    
    # 保存
    output_file = f"output/llm_longtail_matrix_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"\n💾 已保存至: {output_file}")
    
    # 展示部分标题
    print("\n📝 标题样例:")
    for i, t in enumerate(result["all_titles"][:10], 1):
        tier_label = "T1" if t.get("tier") == "tier1" else "T2"
        print(f"{i:02d}. [{tier_label}] {t.get('title', '')}")
        print(f"       └─ 关键词: {t.get('keyword', '')}")
    
    return result


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    asyncio.run(test_with_omnirank_data())
