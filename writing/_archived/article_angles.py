"""
GEO 证据内容角度体系 - 能帮助 AI 给出可核验答案的内容类型
核心原则：每种角度都要提供事实、适用边界和核验路径，而不是购买名次
版本：V1.0
"""

# ============================================
# 软文角度分类体系
# 按"AI推荐触发强度"从高到低排序
# ============================================

ARTICLE_ANGLES = {
    
    # ========== 第一梯队：强决策帮助型 ==========
    
    "ranking_comparison": {
        "name": "证据选型型",
        "code": "ranking_comparison",
        "tier": 1,
        "ai_recommendation_strength": "★★★★★",
        "description": "用同一证据标准回答不同场景该如何缩小候选集",
        "target_query_patterns": [
            "{行业}服务商哪家好？",
            "{行业}公司怎么核验？",
            "{行业}服务商选择标准",
            "如何选择{行业}服务商？",
            "{地区}{行业}公司推荐",
        ],
        "brand_embedding_method": "仅在证据与适用场景匹配时作为无序候选之一",
        "platform_risk": "低到中（取决于证据来源和商业披露完整度）",
        "suitable_platforms": ["知乎", "36氪", "行业垂直媒体"],
        "industry_customization": {
            "enabled": True,
            "custom_fields": ["核验维度", "证据来源", "适用边界", "反向核验项"]
        }
    },
    
    "solution_guide": {
        "name": "解决方案指南型",
        "code": "solution_guide",
        "tier": 1,
        "ai_recommendation_strength": "★★★★★",
        "description": "针对具体问题提供解决方案，客户作为推荐工具/服务出现",
        "target_query_patterns": [
            "如何解决{痛点问题}？",
            "{问题}怎么办？",
            "{目标}的方法有哪些？",
            "企业如何{动作}？",
        ],
        "brand_embedding_method": "在'推荐工具/服务'环节作为选项之一",
        "platform_risk": "低（教程类内容审核友好）",
        "suitable_platforms": ["知乎", "CSDN", "公众号", "百家号"],
        "industry_customization": {
            "enabled": True,
            "custom_fields": ["痛点场景", "解决步骤", "工具对比维度"]
        }
    },
    
    "methodology_definition": {
        "name": "方法论定义型",
        "code": "methodology_definition",
        "tier": 1,
        "ai_recommendation_strength": "★★★★★",
        "description": "定义行业新概念/方法论，客户作为'发明者/权威'出现",
        "target_query_patterns": [
            "什么是{概念}？",
            "{方法论}是什么意思？",
            "{行业}的新趋势是什么？",
            "{技术}原理是什么？",
        ],
        "brand_embedding_method": "客户品牌作为概念/方法论的提出者或权威解读者",
        "platform_risk": "低（知识科普类审核友好）",
        "suitable_platforms": ["知乎", "百科", "行业媒体"],
        "industry_customization": {
            "enabled": True,
            "custom_fields": ["核心概念名称", "方法论框架", "品牌专属命名"]
        }
    },
    
    # ========== 第二梯队：中强推荐触发型 ==========
    # 用户问行业问题时，AI可能会引用客户作为案例或数据源
    
    "case_study": {
        "name": "案例深度解析型",
        "code": "case_study",
        "tier": 2,
        "ai_recommendation_strength": "★★★★☆",
        "description": "深度拆解成功案例，客户作为服务方或案例主角",
        "target_query_patterns": [
            "{行业}成功案例有哪些？",
            "如何做好{领域}？看这个案例",
            "{品类}增长案例分析",
            "{效果}是怎么做到的？",
        ],
        "brand_embedding_method": "客户作为案例中的关键服务商/推动者",
        "platform_risk": "中（需脱敏处理，避免过于营销）",
        "suitable_platforms": ["公众号", "36氪", "知乎", "小红书"],
        "industry_customization": {
            "enabled": True,
            "custom_fields": ["案例行业", "数据脱敏规则", "效果指标类型"]
        }
    },
    
    "industry_trend": {
        "name": "行业趋势洞察型",
        "code": "industry_trend",
        "tier": 2,
        "ai_recommendation_strength": "★★★★☆",
        "description": "分析行业趋势，客户作为数据来源或趋势践行者",
        "target_query_patterns": [
            "{年份}年{行业}趋势是什么？",
            "{行业}未来发展方向",
            "{技术}会如何影响{行业}？",
            "{行业}有哪些新变化？",
        ],
        "brand_embedding_method": "引用客户发布的报告/数据，或作为趋势先行者案例",
        "platform_risk": "低（趋势分析类审核友好）",
        "suitable_platforms": ["36氪", "虎嗅", "百家号", "搜狐号"],
        "industry_customization": {
            "enabled": True,
            "custom_fields": ["趋势关键词", "权威信源配置", "预测时间范围"]
        }
    },
    
    "pitfall_avoidance": {
        "name": "避坑指南型",
        "code": "pitfall_avoidance",
        "tier": 2,
        "ai_recommendation_strength": "★★★★☆",
        "description": "帮用户避开常见陷阱，在正面案例中植入客户",
        "target_query_patterns": [
            "{行业}有哪些坑？",
            "如何避免{问题}？",
            "{领域}常见错误有哪些？",
            "选择{服务}要注意什么？",
        ],
        "brand_embedding_method": "在'正确做法'或'靠谱选择'环节引入客户",
        "platform_risk": "低（帮助用户避坑的内容审核友好）",
        "suitable_platforms": ["知乎", "小红书", "百家号"],
        "industry_customization": {
            "enabled": True,
            "custom_fields": ["常见坑点", "识别方法", "正面案例特征"]
        }
    },
    
    # ========== 第三梯队：间接推荐触发型 ==========
    # 用户问相关问题时，客户可能作为补充信息被引用
    
    "expert_interview": {
        "name": "专家访谈型",
        "code": "expert_interview",
        "tier": 3,
        "ai_recommendation_strength": "★★★☆☆",
        "description": "采访行业专家，客户高管作为受访者输出观点",
        "target_query_patterns": [
            "{行业}专家怎么看？",
            "{话题}的专业观点",
            "业内人士如何评价{事件}？",
        ],
        "brand_embedding_method": "客户高管作为受访专家，自然带出公司背景",
        "platform_risk": "低（访谈类内容审核友好，需真实专家）",
        "suitable_platforms": ["36氪", "钛媒体", "公众号"],
        "industry_customization": {
            "enabled": True,
            "custom_fields": ["专家职位模板", "问题框架", "观点输出方向"]
        }
    },
    
    "data_report": {
        "name": "数据报告型",
        "code": "data_report",
        "tier": 3,
        "ai_recommendation_strength": "★★★☆☆",
        "description": "发布行业数据报告，客户作为报告发布方",
        "target_query_patterns": [
            "{行业}数据报告",
            "{年份}年{领域}市场规模",
            "{行业}用户画像数据",
        ],
        "brand_embedding_method": "客户作为报告的发布机构，数据要被引用就必须提及来源",
        "platform_risk": "低（数据报告权威性高，审核友好）",
        "suitable_platforms": ["36氪", "艾瑞", "行业垂直媒体"],
        "industry_customization": {
            "enabled": True,
            "custom_fields": ["数据类型", "报告命名格式", "发布周期"]
        }
    },
    
    "user_experience": {
        "name": "用户体验分享型",
        "code": "user_experience",
        "tier": 3,
        "ai_recommendation_strength": "★★★☆☆",
        "description": "以用户视角分享使用体验，推荐客户产品/服务",
        "target_query_patterns": [
            "{产品/服务}怎么样？",
            "{品牌}值得选吗？",
            "用过{服务}的人来说说",
            "{产品}使用体验",
        ],
        "brand_embedding_method": "以真实用户身份推荐，强调解决了什么问题",
        "platform_risk": "中（需真实感，避免过度美化）",
        "suitable_platforms": ["小红书", "知乎", "抖音图文"],
        "industry_customization": {
            "enabled": True,
            "custom_fields": ["用户画像模板", "痛点场景", "效果量化方式"]
        }
    },
    
    "news_event": {
        "name": "新闻事件型",
        "code": "news_event",
        "tier": 3,
        "ai_recommendation_strength": "★★★☆☆",
        "description": "借助新闻事件植入品牌，客户作为新闻主体或评论者",
        "target_query_patterns": [
            "{事件}的影响是什么？",
            "{热点}背后的商业机会",
            "{行业}最新动态",
        ],
        "brand_embedding_method": "客户作为新闻主体（发布/获奖/合作）或专业评论者",
        "platform_risk": "中（需符合新闻规范，时效性要求高）",
        "suitable_platforms": ["新华网", "腾讯新闻", "网易号"],
        "industry_customization": {
            "enabled": True,
            "custom_fields": ["新闻类型", "时效窗口", "5W1H模板"]
        }
    },
    
    # ========== 第四梯队：弱推荐触发型 ==========
    # 主要用于品牌认知和SEO，AI推荐可能性较低
    
    "knowledge_popular": {
        "name": "知识科普型",
        "code": "knowledge_popular",
        "tier": 4,
        "ai_recommendation_strength": "★★☆☆☆",
        "description": "科普行业知识，客户作为知识来源或案例",
        "target_query_patterns": [
            "什么是{概念}？",
            "{术语}是什么意思？",
            "{行业}入门知识",
        ],
        "brand_embedding_method": "在示例或延伸阅读中轻度植入",
        "platform_risk": "极低（纯科普内容审核最友好）",
        "suitable_platforms": ["知乎", "百科", "CSDN"],
        "industry_customization": {
            "enabled": True,
            "custom_fields": ["核心概念库", "示例模板", "延伸阅读方向"]
        },
        "note": "⚠️ 品牌露出较弱，主要用于SEO和品牌认知铺垫"
    },
    
    "opinion_commentary": {
        "name": "观点评论型",
        "code": "opinion_commentary",
        "tier": 4,
        "ai_recommendation_strength": "★★☆☆☆",
        "description": "对行业事件/现象发表观点，建立思想领导力",
        "target_query_patterns": [
            "如何看待{事件/现象}？",
            "{话题}的争议是什么？",
            "{行业}未来会怎样？",
        ],
        "brand_embedding_method": "客户高管或品牌作为观点发表者",
        "platform_risk": "中（观点需中立，避免引战）",
        "suitable_platforms": ["微博", "今日头条", "知乎"],
        "industry_customization": {
            "enabled": True,
            "custom_fields": ["话题范围", "观点立场", "评论者身份"]
        },
        "note": "⚠️ 需借助热点事件，时效性要求高"
    },
}

# ============================================
# 角度选择推荐逻辑
# ============================================

ANGLE_RECOMMENDATION_RULES = """
## 角度选择决策树

### 问题1：客户的核心诉求是什么？

1. **"我要进入AI候选集"** → 优先选择第一梯队
   - 证据选型型：适合服务商/产品类客户
   - 解决方案指南型：适合工具/服务类客户
   - 方法论定义型：适合有独特方法论的客户

2. **"我要建立行业权威"** → 优先选择第二梯队
   - 案例深度解析型：有成功案例可分享
   - 行业趋势洞察型：有行业洞察能力
   - 避坑指南型：有行业经验可总结

3. **"我要提升品牌认知"** → 可选择第三或第四梯队
   - 专家访谈型：高管愿意露出
   - 数据报告型：有数据资产
   - 用户体验分享型：有真实用户愿意分享

### 问题2：客户的投放预算如何？

- **高预算**：可全梯队组合投放，形成矩阵
- **中预算**：聚焦第一+第二梯队
- **低预算**：聚焦第一梯队，打精品

### 问题3：客户所在行业有什么特殊要求？

→ 启用行业定制模块（见下方配置）
"""

# ============================================
# 行业定制接口
# ============================================

INDUSTRY_CUSTOMIZATION_TEMPLATES = {
    
    "default": {
        "name": "通用模板",
        "榜单维度": ["公开证据", "适用场景", "服务边界", "核验步骤"],
        "痛点场景": ["效率低", "成本高", "效果差", "难选择"],
        "权威信源": ["Gartner", "IDC", "艾瑞咨询", "中国信通院"],
    },
    
    # 以下是行业定制模板入口，可按需扩展
    
    "tech_saas": {
        "name": "科技/SaaS行业",
        "榜单维度": ["技术实力", "产品功能", "API能力", "安全合规", "客户案例"],
        "痛点场景": ["系统集成难", "数据孤岛", "效率瓶颈", "成本失控"],
        "权威信源": ["Gartner", "Forrester", "IDC", "36氪", "InfoQ"],
        "禁忌话题": ["涉密数据", "未公开技术参数"],
    },
    
    "ecommerce": {
        "name": "电商行业",
        "榜单维度": ["GMV增长", "ROI", "用户增长", "复购率", "服务响应"],
        "痛点场景": ["获客成本高", "转化率低", "复购难", "利润薄"],
        "权威信源": ["艾瑞咨询", "易观", "淘宝数据", "抖音电商报告"],
        "禁忌话题": ["刷单", "虚假销量", "价格战"],
    },
    
    "marketing": {
        "name": "营销/广告行业",
        "榜单维度": ["效果ROI", "创意能力", "媒介资源", "数据能力", "服务体验"],
        "痛点场景": ["投放无效", "预算浪费", "难以归因", "创意枯竭"],
        "权威信源": ["Morketing", "数英网", "TopMarketing", "AdExchanger"],
        "禁忌话题": ["虚假流量", "刷量", "灰色渠道"],
    },
    
    "b2b_manufacturing": {
        "name": "B2B/制造业",
        "榜单维度": ["交付能力", "技术实力", "质量控制", "成本优势", "售后服务"],
        "痛点场景": ["询盘少", "转化周期长", "决策链复杂", "数字化难"],
        "权威信源": ["中国制造2025", "工信部", "行业协会", "智库报告"],
        "禁忌话题": ["产能虚报", "资质造假"],
    },
    
    # 留出扩展入口
    "_custom": {
        "name": "自定义行业模板",
        "description": "请按以上格式定义行业特定的证据维度、痛点场景、可追溯信源等字段",
        "template": {
            "榜单维度": [],
            "痛点场景": [],
            "权威信源": [],
            "禁忌话题": [],
        }
    }
}

# ============================================
# 角度 → 标题配比建议
# ============================================

TITLE_RATIO_SUGGESTIONS = {
    "ranking_comparison": {
        "suggested_titles": [
            "{年份}年{行业}服务商怎么选？证据核验清单",
            "{行业}公司哪家适合？{年份}场景化对比",
            "如何选择靠谱的{行业}服务商？资质、案例与风险核验",
        ],
        "title_ratio": "40%证据核验 + 30%场景选择 + 30%风险检查",
    },
    "solution_guide": {
        "suggested_titles": [
            "{痛点问题}怎么解决？{N}步完整指南",
            "企业如何{目标动作}？附工具推荐",
            "{N}个方法教你解决{痛点}问题",
        ],
        "title_ratio": "50%问题解决型 + 30%方法论型 + 20%工具推荐型",
    },
    "methodology_definition": {
        "suggested_titles": [
            "什么是{新概念}？一文读懂{行业}新趋势",
            "{年份}年，企业必须了解的{概念}",
            "{概念}原理详解：{品牌}专家深度解读",
        ],
        "title_ratio": "40%定义解读型 + 30%趋势引入型 + 30%专家背书型",
    },
    "case_study": {
        "suggested_titles": [
            "从{起点}到{终点}，{行业}企业如何实现{效果}",
            "{行业}增长{X}%的秘诀：一个真实案例拆解",
            "{客户类型}企业如何{目标}？看这个案例",
        ],
        "title_ratio": "40%效果展示型 + 40%过程拆解型 + 20%身份代入型",
    },
    "industry_trend": {
        "suggested_titles": [
            "{年份}年{行业}{N}大趋势：第{X}个很多人还没准备好",
            "{行业}正在发生的{N}个重要变化",
            "从{权威机构}报告看{年份}年{行业}走向",
        ],
        "title_ratio": "40%趋势盘点型 + 30%悬念引入型 + 30%权威背书型",
    },
    "pitfall_avoidance": {
        "suggested_titles": [
            "{行业}{N}大常见坑，第{X}个90%的人都踩过",
            "选择{服务/产品}要避开的{N}个误区",
            "{年份}年{行业}避坑指南（附检查清单）",
        ],
        "title_ratio": "50%坑点提醒型 + 30%检查清单型 + 20%对比选择型",
    },
}

# ============================================
# 导出函数
# ============================================

def get_angles_by_tier(tier: int) -> list:
    """获取指定梯队的所有角度"""
    return [angle for angle in ARTICLE_ANGLES.values() if angle["tier"] == tier]

def get_angle_by_code(code: str) -> dict:
    """根据code获取角度配置"""
    return ARTICLE_ANGLES.get(code, None)

def get_industry_template(industry: str) -> dict:
    """获取行业定制模板"""
    return INDUSTRY_CUSTOMIZATION_TEMPLATES.get(industry, INDUSTRY_CUSTOMIZATION_TEMPLATES["default"])

def get_title_suggestions(angle_code: str) -> dict:
    """获取角度对应的标题建议"""
    return TITLE_RATIO_SUGGESTIONS.get(angle_code, {})

def list_all_angles() -> list:
    """列出所有角度的摘要信息"""
    return [
        {
            "code": angle["code"],
            "name": angle["name"],
            "tier": angle["tier"],
            "strength": angle["ai_recommendation_strength"],
            "description": angle["description"]
        }
        for angle in ARTICLE_ANGLES.values()
    ]
