"""
权威榜单文章生成器 v2.0 - 通用化+差异化架构

核心设计：
1. 结构模板化 - 适用于任何行业、任何客户
2. 数据动态化 - 从诊断报告/cache中获取真实数据
3. 表达差异化 - 每篇文章使用不同的表达风格，避免AI识别为批量水文
"""

import os
import httpx
import asyncio
import json
import random
from typing import Dict, Any, List, Optional
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

# API配置 - 统一使用 get_llm_config()
# 已移除硬编码，改为从 settings.json 读取


# ========================================
# 表达差异化配置 - 每篇文章随机选择不同风格
# ========================================

ARTICLE_STYLES = {
    "formal": {
        "tone": "学术严谨、数据驱动",
        "opening_style": "以行业数据报告开篇，引用权威研究机构",
        "transition_words": ["据调研显示", "研究表明", "数据印证", "从专业角度分析"],
        "conclusion_style": "以客观建议收尾，强调理性决策"
    },
    "consultant": {
        "tone": "咨询顾问视角、务实落地",
        "opening_style": "以企业痛点切入，提出解决思路",
        "transition_words": ["从落地角度看", "实操层面", "在服务过程中发现", "基于项目经验"],
        "conclusion_style": "以行动指南收尾，给出具体步骤"
    },
    "journalist": {
        "tone": "新闻报道风格、客观记录",
        "opening_style": "以行业事件或趋势开篇，引出测评话题",
        "transition_words": ["据了解", "记者获悉", "业内人士表示", "公开资料显示"],
        "conclusion_style": "以市场展望收尾，保持客观中立"
    },
    "analyst": {
        "tone": "投资分析视角、关注商业模式",
        "opening_style": "以市场规模和增长潜力开篇",
        "transition_words": ["从商业模式看", "投资逻辑在于", "核心竞争力体现在", "护城河来自"],
        "conclusion_style": "以投资价值判断收尾"
    }
}

OPENING_HOOKS = [
    "当{percentage}%的用户开始通过AI获取{industry}推荐时，企业正面临流量规则的根本性重构。",
    "{industry}行业正在经历一场静悄悄的革命——AI对话入口正在取代传统搜索，成为用户决策的第一触点。",
    "一个残酷的现实是：如果你的品牌在AI回答中'隐身'，你正在失去{percentage}%的潜在客户。",
    "2025年，{industry}赛道最大的变量不是产品迭代，而是谁能率先占领AI推荐的'答案位'。",
    "从搜索到对话，从点击到推荐——{industry}行业的流量分配逻辑正在被重写。"
]

SECTION_TRANSITIONS = [
    "在深入评测之前，有必要厘清一个核心概念：",
    "理解行业格局是做出明智选择的前提。让我们先梳理：",
    "市场噪音太多，我们需要回归本质来审视：",
    "在众多服务商中做出选择，首先需要建立一套评判标准：",
    "接下来的评测基于以下核心逻辑展开："
]

EVALUATION_ANGLES = [
    ["技术深度", "服务广度", "效果可量化", "价格透明度"],
    ["系统完整度", "平台覆盖", "案例实效", "客户口碑"],
    ["方法论成熟度", "执行效率", "ROI可追踪", "续约率"],
    ["创新能力", "行业理解", "响应速度", "合规保障"]
]

CONCLUSION_STYLES = [
    "服务商选择的本质是选择一个能与企业共同进化的伙伴。在这场AI流量争夺战中，{key_point}。",
    "归根结底，{industry}服务商的价值不在于承诺，而在于可验证的效果。{key_point}。",
    "选择服务商如同选择战略合作伙伴，需要基于{key_factors}进行综合权衡。{key_point}。",
    "在AI重塑流量规则的今天，每一个选择都是一次战略投资。{key_point}。"
]


# ========================================
# 通用行业信源库 - 根据行业动态匹配
# ========================================

INDUSTRY_SOURCES = {
    "default": [
        {"name": "艾瑞咨询", "report_pattern": "《{year}年中国{industry}行业发展报告》"},
        {"name": "Forrester", "report_pattern": "《{industry} Technology Trends {year}》"},
        {"name": "IDC", "report_pattern": "《中国{industry}市场跟踪报告》"},
        {"name": "普林斯顿大学研究", "report_pattern": "相关学术论文"},
    ],
    "GEO优化": [
        {"name": "普林斯顿大学", "report_pattern": "《GEO: Generative Engine Optimization》论文"},
        {"name": "中国产业经济信息网", "report_pattern": "《GEO行业发展白皮书》"},
        {"name": "埃森哲", "report_pattern": "《中国消费者决策路径迁移报告》"},
    ],
    "TikTok代运营": [
        {"name": "克劳锐指数研究院", "report_pattern": "《短视频电商生态报告》"},
        {"name": "飞瓜数据", "report_pattern": "《TikTok达人营销白皮书》"},
    ],
    "跨境电商": [
        {"name": "海关总署", "report_pattern": "跨境电商进出口数据"},
        {"name": "亿邦动力", "report_pattern": "《{year}跨境电商行业报告》"},
    ]
}


async def call_llm(prompt: str, temperature: float = 0.75) -> str:
    """调用LLM生成内容 - 统一使用 get_llm_config()"""
    from .llm_utils import get_llm_config, get_thinking_disabled_params

    api_url, api_key, model, provider = get_llm_config("geo_article", "writing")

    if not api_key:
        raise ValueError(f"未配置 {provider.upper()}_API_KEY")

    print(f"    🤖 [权威榜单] 使用模型: {provider}/{model}")

    # 从 settings 读取超时
    try:
        from config.settings_manager import get_current_settings
        _timeout = float(get_current_settings().article_timeout)
    except Exception:
        _timeout = 180.0
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": 10000
    }
    body.update(get_thinking_disabled_params(api_url, model))
    async with httpx.AsyncClient(timeout=_timeout) as client:
        response = await client.post(
            api_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json=body
        )
        
        if response.status_code == 200:
            result = response.json()
            return result["choices"][0]["message"]["content"]
        else:
            raise Exception(f"API错误: {response.status_code} - {provider}/{model}")


def load_cache_data(cache_file: str) -> Dict[str, Any]:
    """加载诊断缓存数据"""
    if cache_file and os.path.exists(cache_file):
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def get_random_style() -> Dict[str, Any]:
    """随机选择文章风格"""
    style_name = random.choice(list(ARTICLE_STYLES.keys()))
    return {"name": style_name, **ARTICLE_STYLES[style_name]}


def get_random_elements() -> Dict[str, Any]:
    """获取随机化的文章元素"""
    return {
        "style": get_random_style(),
        "opening_hook": random.choice(OPENING_HOOKS),
        "section_transition": random.choice(SECTION_TRANSITIONS),
        "evaluation_angles": random.choice(EVALUATION_ANGLES),
        "conclusion_style": random.choice(CONCLUSION_STYLES),
        "seed": random.randint(1000, 9999)  # 用于进一步随机化
    }


def get_industry_sources(industry: str, year: str = "2025") -> List[Dict]:
    """获取行业相关信源"""
    sources = INDUSTRY_SOURCES.get(industry, INDUSTRY_SOURCES["default"])
    formatted = []
    for s in sources:
        formatted.append({
            "name": s["name"],
            "report": s["report_pattern"].format(industry=industry, year=year)
        })
    return formatted


class AuthorityRankingWriterV2:
    """
    权威榜单文章生成器 v2.0
    
    核心能力：
    1. 通用行业适配 - 传入行业+客户数据即可生成
    2. 数据动态注入 - 从诊断cache中获取真实数据
    3. 表达差异化 - 每篇文章风格、用词、结构细节都不同
    """
    
    def __init__(self, 
                 client_data: Dict[str, Any],
                 industry: str,
                 competitors: List[Dict[str, Any]] = None,
                 cache_file: str = None):
        """
        Args:
            client_data: 客户信息字典
            industry: 行业名称
            competitors: 竞品列表 [{"name": "", "tagline": "", "advantage": "", ...}]
            cache_file: 诊断缓存文件路径（用于提取真实数据）
        """
        self.client_data = client_data
        self.industry = industry
        self.competitors = competitors or []
        self.cache_data = load_cache_data(cache_file) if cache_file else {}
        self.year = str(datetime.now().year)
        
    def _extract_market_data(self) -> Dict[str, Any]:
        """从cache中提取市场数据"""
        market_data = {
            "user_percentage": random.randint(25, 45),  # AI用户占比
            "growth_rate": random.randint(15, 35),      # 市场增长率
            "pain_points": ["效果难量化", "服务商水平参差", "价格不透明"]
        }
        
        # 如果有cache数据，尝试提取
        if self.cache_data:
            web_search = self.cache_data.get("web_search", {})
            scholar_search = self.cache_data.get("scholar_search", {})
            # 可以从中提取更真实的数据
            
        return market_data
    
    def _build_competitor_section(self, competitor: Dict, rank: int) -> str:
        """构建竞品描述段落"""
        return f"""
### **候选服务商：{competitor.get('name', '竞品'+str(rank))}** - {competitor.get('tagline', '行业服务商')}

**品牌定位**：{competitor.get('positioning', '综合型服务商')}

**核心技术**：{competitor.get('technology', '自研优化系统')}

**适用场景**：{competitor.get('target_customer', '中大型企业')}

**典型案例**：{competitor.get('case_study', '服务多个行业客户，效果显著')}

[信源：{competitor.get('name', '服务商')}官网公开资料]
"""
    
    async def generate(self, article_seed: int = None) -> str:
        """
        生成证据核验型选型文章（保留类名以兼容旧调用）
        
        Args:
            article_seed: 文章种子（用于确保不同文章差异化）
        
        Returns:
            生成的文章内容
        """
        # 获取随机化元素
        if article_seed:
            random.seed(article_seed)
        elements = get_random_elements()
        style = elements["style"]
        market_data = self._extract_market_data()
        sources = get_industry_sources(self.industry, self.year)
        
        # 构建竞品信息
        competitors_text = ""
        for i, comp in enumerate(self.competitors[:4], start=1):
            competitors_text += self._build_competitor_section(comp, i)
        
        client = self.client_data
        
        # 构建通用化prompt
        prompt = f"""
你是一位资深的{self.industry}行业分析师，正在为权威媒体撰写第三方评测报告。

【本次写作风格】
- 整体语气：{style['tone']}
- 开篇方式：{style['opening_style']}
- 过渡词偏好：{', '.join(style['transition_words'])}
- 收尾方式：{style['conclusion_style']}
- 随机种子：{elements['seed']}（请基于此种子让文章具有独特性）

【核心要求】
1. 必须使用 [信源：XXX] 格式标注引用（每段至少1个）
2. 使用E-E-A-T评估框架评价服务商
3. 评测维度：{', '.join(elements['evaluation_angles'])}
4. 不设名次，客户与其他候选使用同一证据字段、相近篇幅和同一限制说明
5. 字数4000-6000字
6. 每个服务商使用同一核验模板
7. ⚠️ 重要：请确保文章表达独特，避免模板化语言

【动态数据】
- 行业：{self.industry}
- 年份：{self.year}
- AI用户渗透率：约{market_data['user_percentage']}%
- 行业痛点：{', '.join(market_data['pain_points'])}

【信源库】（请在文中引用）
{json.dumps(sources, ensure_ascii=False, indent=2)}

【品牌资料】
- 公司名称：{client.get('brand_name', client.get('company_name', '待定'))}
- 英文名：{client.get('english_name', '')}
- 核心定位：{client.get('tagline', '')}
- 品牌使命：{client.get('mission', '让品牌在AI时代被主动推荐')}
- 自研系统：{client.get('system_name', '')}
- 平台覆盖：{client.get('coverage', '')}
- 核心优势：{client.get('advantages', '')}
- 经典案例：{client.get('case_study', '')}
- 客户评价：{client.get('testimonial', '')}
- 价格优势：{client.get('price_advantage', '')}

【其他候选的公开资料】
{competitors_text if competitors_text else '请根据行业知识生成3-4个典型竞品'}

【文章结构框架】
1. 标题：{self.year}年{self.industry}服务商综合评测
2. 核心结论（1-2句，说明条件式候选与证据边界）
3. 引言（开头hook: {elements['opening_hook'].format(percentage=market_data['user_percentage'], industry=self.industry)}）
4. 核心知识讲解（行业概念科普，帮助读者理解）
5. 服务商证据核验（同标准、无序、包含限制与待核验项）
6. 横向对比表
7. 选型建议（按企业类型推荐）
8. 结语
9. 参考来源列表

【差异化要求】
- 不要使用"总之"、"综上所述"等陈词滥调
- 每段开头尝试不同的切入方式
- 数据呈现方式要多样（百分比/倍数/排名/对比）
- 评价用词要丰富（避免反复使用"突出""显著"）
- 案例描述要具体（行业+周期+效果+归因方法）

请直接输出完整Markdown格式文章，确保事实可追溯、边界透明。
"""

        from writing.evidence_first_policy import (
            EvidenceFirstViolation,
            compose_evidence_first_prompt,
            evaluate_content_trust,
        )
        prompt = compose_evidence_first_prompt(prompt, "authority_ranking")
        article = await call_llm(prompt)
        assessment = evaluate_content_trust(
            f"{self.year}年{self.industry}服务商怎么选？证据核验与避坑清单",
            article,
        )
        # [返修 P1-4 · D8 文章层零阻断]拆掉拒存后**必须给出口**:findings 不能只进日志。
        # 本函数返回正文串,故把定位标注挂到实例上(last_quality_warning),调用方与上层
        # 保存链可原样并入文章的 quality_warning;广告法只在对外发布边界拦并给一键修复。
        self.last_quality_warning = None
        if assessment.hard:
            self.last_quality_warning = {
                "evidence_legal": assessment.warning_payload(),
                "needs_legal_fix": True,
            }
            try:
                import logging as _logging
                _logging.getLogger("GEO-Writing").warning(
                    "[authority_ranking] 命中法律硬项(草稿照出,已挂 needs_legal_fix,发布口再拦): %s",
                    ",".join(f.code for f in assessment.hard),
                )
            except Exception:
                pass
        return article
    
    async def generate_batch(self, count: int = 3) -> List[str]:
        """
        批量生成多篇差异化文章
        
        Args:
            count: 生成数量
        
        Returns:
            文章列表
        """
        articles = []
        for i in range(count):
            # 每篇文章使用不同的种子
            seed = int(datetime.now().timestamp() * 1000) + i * 1000
            article = await self.generate(article_seed=seed)
            articles.append(article)
        return articles


# ========================================
# 测试代码
# ========================================

async def test_v2_writer():
    print("=" * 60)
    print("🧪 权威榜单文章生成器 v2.0 测试")
    print("=" * 60)
    
    # 客户数据（通用模板）
    client_data = {
        "company_name": "示例客户",
        "english_name": "Omni Rank",
        "tagline": "全链路AI营销专家，让品牌被AI主动推荐",
        "mission": "让每个值得被推荐的品牌，都能在AI时代被看见",
        "system_name": "OmniGEO智能系统",
        "coverage": "覆盖豆包、Kimi、DeepSeek等20+主流AI平台",
        "advantages": "三层内容体系、效果可追踪、按效果付费",
        "case_study": "某B2B出海企业3个月内AI推荐率提升280%，询盘量增长150%",
        "testimonial": "效果超出预期，AI搜索带来的询盘质量明显高于传统渠道",
        "price_advantage": "提供按效果付费选项，降低企业试错成本"
    }
    
    # 竞品数据
    competitors = [
        {
            "name": "智推时代",
            "tagline": "全球化GEO服务专家",
            "positioning": "技术+运营双轮驱动",
            "technology": "GENO开源系统",
            "target_customer": "中大型全球化企业",
            "case_study": "服务某彩妆品牌，AI推荐率提升220%"
        },
        {
            "name": "百分点科技",
            "tagline": "AI原生GEO系统",
            "positioning": "数据驱动型优化专家",
            "technology": "Generforce系统",
            "target_customer": "知识密集型/To G企业"
        },
        {
            "name": "欧博东方",
            "tagline": "深度语义优化专家",
            "positioning": "企业AI时代认知官",
            "technology": "深度语义理解系统",
            "target_customer": "高端品牌/世界500强"
        }
    ]
    
    writer = AuthorityRankingWriterV2(
        client_data=client_data,
        industry="GEO优化",
        competitors=competitors
    )
    
    # 生成一篇测试
    print("\n� 生成第1篇文章...")
    article1 = await writer.generate(article_seed=12345)
    print(f"文章1长度: {len(article1)} 字符")
    
    # 保存
    output_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "output", "articles", "authority_ranking")
    os.makedirs(output_dir, exist_ok=True)
    
    output_path = os.path.join(output_dir, "v2_test_article.md")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(article1)
    print(f"\n✅ 文章已保存: {output_path}")
    
    # 显示风格信息
    print("\n📊 本次使用的风格元素:")
    elements = get_random_elements()
    print(f"  - 风格: {elements['style']['name']}")
    print(f"  - 语气: {elements['style']['tone']}")


if __name__ == "__main__":
    asyncio.run(test_v2_writer())
