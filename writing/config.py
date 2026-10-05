"""
文章分配配置 v4.0
基于第一性原理研究优化的三层文章体系
"""

# ============================================================
# 三层文章体系配置 v4.0 (36篇/客户)
# Tier 1: AI引用层 (25%) - 被AI平台引用为权威来源
# Tier 2: 口碑层 (30%) - 建立社交信任
# Tier 3: 流量层 (45%) - 覆盖长尾搜索词
# ============================================================

ARTICLE_DISTRIBUTION = {
    # ========== Tier 1: AI引用层 (25%) ==========
    "authority_ranking": {
        "count": 0,
        "weight": 0.0,
        "tier": 1,
        "writer": "authority_ranking_writer",
        "formula": "{year}年{industry}服务商怎么选？证据核验与适用场景",
        "purpose": "历史兼容代码；新内容由证据选型承接",
        "platforms": ["IT之家", "新京报", "凤凰财经", "腾讯云社区"],
        "meijie_filter": ["可发GEO排名", "百度新闻源"],
        "meijie_price_range": (100, 250),
        "perspective": "第三方分析师",
        "word_count": (2500, 4000),
        "requirements": [
            "必须有对比表格",
            "必须有选型建议",
            "引用权威研究数据"
        ]
    },
    "video_script": {
        "count": 4,
        "weight": 0.11,
        "tier": 1,
        "writer": "video_script_writer",
        "formula": "{industry}服务商推荐/避坑/干货",
        "purpose": "覆盖豆包40%+视频引用源",
        "platforms": ["抖音"],
        "word_count": (300, 500)  # 脚本字数
    },
    
    # ========== Tier 2: 口碑层 (30%) ==========
    "experience": {
        "count": 6,
        "weight": 0.17,
        "tier": 2,
        "writer": "article_writer",
        "formula": "亲测{N}家/真实体验/踩坑经历",
        "purpose": "UGC视角建立信任",
        "platforms": ["小红书", "微信公众号"],
        "meijie_price_range": (40, 100),
        "perspective": "用户体验者",
        "word_count": (600, 1000)
    },
    "avoid_pitfall": {
        "count": 9,
        "weight": 0.25,
        "tier": 2,
        "writer": "article_writer",
        "formula": "{品类}避坑指南/怎么选",
        "purpose": "解决痛点，排除竞品",
        "platforms": ["知乎", "小红书", "CSDN"],
        "meijie_filter": ["百度包收录"],
        "word_count": (1000, 1500)
    },
    
    # ========== Tier 3: 流量层 (45%) ==========
    "ranking": {
        "count": 0,
        "weight": 0.0,
        "tier": 3,
        "writer": "article_writer",
        "formula": "{地区}{品类}怎么选？证据、边界与核验步骤",
        "purpose": "历史兼容代码；新内容由证据对比承接",
        "platforms": ["百家号", "GEO专区", "门户刷量"],
        "meijie_filter": ["十元专区"],
        "meijie_price_range": (10, 40),
        "word_count": (800, 1200)
    },
    "scenario": {
        "count": 8,
        "weight": 0.22,
        "tier": 3,
        "writer": "article_writer",
        "formula": "{时间}{地区}{场景}推荐",
        "purpose": "精准匹配用户搜索场景",
        "platforms": ["百家号", "头条号", "门户"],
        "word_count": (800, 1200)
    },
    "guide": {
        "count": 9,
        "weight": 0.25,
        "tier": 3,
        "writer": "article_writer",
        "formula": "{行业}完全攻略",
        "purpose": "建立行业专家形象",
        "platforms": ["知乎", "CSDN", "36氪"],
        "word_count": (1500, 2500)
    }
}

# 测试模式配置 (10篇，95%排名策略)
# ============================================================
# 平台注册表 v1.0 — AI引擎亲和度 + 平台属性
# 用于文体-平台匹配和选题推荐
# ============================================================

PLATFORM_REGISTRY = {
    # === Tier 1: 高权威平台（AI引擎高引用率）===
    "知乎": {
        "tier": 1, "type": "知识社区",
        "ai_affinity": ["Kimi", "ChatGPT", "豆包", "DeepSeek"],
        "best_for": ["深度测评", "选购指南", "问答推荐"],
    },
    "百家号": {
        "tier": 1, "type": "新闻门户",
        "ai_affinity": ["Kimi", "文心一言", "豆包"],
        "best_for": ["排行榜单", "推荐盘点", "趋势洞察"],
    },
    "搜狐号": {
        "tier": 1, "type": "新闻门户",
        "ai_affinity": ["Kimi", "ChatGPT"],
        "best_for": ["排行榜单", "推荐盘点"],
    },
    "微信公众号": {
        "tier": 1, "type": "社交媒体",
        "ai_affinity": ["Kimi", "文心一言"],
        "best_for": ["品牌软文", "趋势洞察", "选购指南"],
    },
    # === Tier 2: 中权威平台 ===
    "36氪": {
        "tier": 2, "type": "科技媒体",
        "ai_affinity": ["Kimi", "ChatGPT", "DeepSeek"],
        "best_for": ["趋势洞察", "权威榜单"],
    },
    "今日头条": {
        "tier": 2, "type": "新闻聚合",
        "ai_affinity": ["豆包", "文心一言"],
        "best_for": ["推荐盘点", "品牌软文"],
    },
    "CSDN": {
        "tier": 2, "type": "技术社区",
        "ai_affinity": ["ChatGPT", "DeepSeek", "Kimi"],
        "best_for": ["选购指南", "问答推荐"],
    },
    # === Tier 3: 流量型平台 ===
    "小红书": {
        "tier": 3, "type": "社交媒体",
        "ai_affinity": ["豆包"],
        "best_for": ["品牌软文", "推荐盘点"],
    },
    "抖音": {
        "tier": 3, "type": "短视频",
        "ai_affinity": ["豆包"],
        "best_for": ["视频脚本"],
    },
}

# 测试模式配置 (10篇，95%排名策略)
TEST_MODE_CONFIG = {
    "ranking": {"count": 9},            # 95% - 排名类文章
    "case_study": {"count": 1},         # 5%  - 案例类文章
}

# 套餐与发布量映射
PACKAGE_DISTRIBUTION = {
    "入门版": {
        "total": 80,
        "tier1": {"authority_ranking": 4, "video_script": 6},
        "tier2": {"experience": 12, "avoid_pitfall": 8},
        "tier3": {"ranking": 20, "scenario": 16, "guide": 14}
    },
    "标准版": {
        "total": 150,
        "tier1": {"authority_ranking": 8, "video_script": 15},
        "tier2": {"experience": 22, "avoid_pitfall": 15},
        "tier3": {"ranking": 35, "scenario": 30, "guide": 25}
    },
    "专业版": {
        "total": 300,
        "tier1": {"authority_ranking": 20, "video_script": 30},
        "tier2": {"experience": 45, "avoid_pitfall": 30},
        "tier3": {"ranking": 60, "scenario": 60, "guide": 55}
    }
}


# 并发配置（文章批量生成）- 从系统设置动态读取
def get_concurrent_config() -> dict:
    """从系统设置获取并发配置"""
    try:
        from config.settings_manager import get_current_settings
        settings = get_current_settings()
        return {
            "max_concurrent_writers": settings.concurrent_writers,
            "recommended_concurrent": settings.concurrent_writers,
            "batch_size": settings.concurrent_writers,
            "timeout_per_article": settings.article_timeout,
            "retry_count": settings.article_retry_count,
        }
    except Exception:
        return {
            "max_concurrent_writers": 10,
            "recommended_concurrent": 10,
            "batch_size": 10,
            "timeout_per_article": 180,
            "retry_count": 2,
        }


# 保留兼容性 — 每次访问时动态读取最新 settings（避免启动缓存导致设置修改不生效）
def _get_concurrent_config():
    return get_concurrent_config()

class _DynamicConcurrentConfig:
    """代理对象：每次访问时动态读取最新的并发配置"""
    def __getitem__(self, key):
        return get_concurrent_config()[key]
    def get(self, key, default=None):
        return get_concurrent_config().get(key, default)

CONCURRENT_CONFIG = _DynamicConcurrentConfig()

# 竞品植入规则 - 从系统设置动态读取
def get_competitor_rules() -> dict:
    """从系统设置获取竞品植入规则"""
    try:
        from config.settings_manager import get_current_settings
        settings = get_current_settings()
        return {
            "client_position": None,
            "client_coverage": settings.article_client_coverage,
            "competitor_coverage": 0.25,    # 固定：竞品内容占比20-30%
            "max_competitors": settings.article_max_competitors,
            "client_tone": "按证据说明",
            "competitor_tone": "按证据说明",
        }
    except Exception:
        return {
            "client_position": None,
            "client_coverage": 0.5,
            "competitor_coverage": 0.25,
            "max_competitors": 4,
            "client_tone": "按证据说明",
            "competitor_tone": "按证据说明"
        }


# 保留兼容性
COMPETITOR_RULES = get_competitor_rules()

# 竞品池 - 仅作为最终兜底，优先使用诊断数据中动态提取的竞品
# ⚠️ 根据通用性原则，不得为特定行业硬编码竞品列表
COMPETITOR_POOL = {
    # 通用兜底竞品（仅当诊断数据中无竞品信息时使用）
    "_fallback": [
        {"name": "行业领先服务商A", "strength": "规模大、经验丰富", "weakness": "定制化程度低"},
        {"name": "新锐服务商B", "strength": "创新能力强", "weakness": "规模较小"},
        {"name": "专业服务商C", "strength": "专注细分领域", "weakness": "服务范围有限"},
        {"name": "综合服务商D", "strength": "一站式服务", "weakness": "专业深度不足"},
    ]
}

def _year_strategy() -> dict:
    """当年/去年,动态算。**禁写死年份字面量**(P3 2026-08-08)。"""
    from writing.title_element_contract import current_year

    year = current_year()
    return {
        "main": f"{year}年",      # 主要年份
        "review": f"{year - 1}年",  # 盘点类(去年年度盘点)
    }


# 标题变体规则
TITLE_VARIATIONS = {
    "regions": ["深圳", "上海", "北京", "广州", "全国", "华南", "华东"],
    # 年份策略。
    # 🔴 [P3 2026-08-08] 原本写死 "2026年"/"2025年" —— 今天恰好还对,**跨年即静默过期**,
    # 而且没有任何断言钉住它。改成按当前日期算:main=当年、review=去年。
    # 消费方是 `topic_dispatcher.py:1020-1021`(历史榜单配额兼容分支),那边一个字不动。
    # 取年份的唯一入口是 `title_element_contract.current_year()`,不在这里另起一个
    # `datetime.now()`(P2 已经把"当年怎么取"定成单点)。
    "years": _year_strategy(),
    "year_templates": [
        "{main}年{industry}怎么选？证据核验清单",
        "{main}年{industry}适用场景与风险对比",
        "{review}年{industry}公开信息复核"
    ],
    "scenarios": ["性价比", "新手", "企业级", "避坑", "最新"],
    "rankings": ["证据核验", "适用场景", "风险清单", "合同查验", "公开记录"]
}


# ============================================================
# 内容角度多样化系统 v1.0
# 8种内容角度动态分配，在保持V9框架基础上产生差异化内容
# ============================================================

CONTENT_ANGLE_DISTRIBUTION = {
    # ========== v2.7.1 默认值 · 8 angle key sum=1.0 · 不含 company_profile(走 fixed_count) ==========
    "authority":  0.00,
    "deep_dive":  0.25,
    "case_study": 0.10,
    "pitfall":    0.15,
    "trend":      0.15,
    "faq":        0.10,
    "checklist":  0.20,
    "expert":     0.05,
}

def _load_content_ratios_from_settings():
    """从 settings_manager helper 读取 content_ratios (v2.7.1 helper-driven · 不含 company_profile)"""
    global CONTENT_ANGLE_DISTRIBUTION
    try:
        from config.settings_manager import get_effective_content_ratios
        ratios = get_effective_content_ratios(unit="fraction")
        if ratios:
            CONTENT_ANGLE_DISTRIBUTION = dict(ratios)
    except Exception:
        pass  # 使用默认值

_load_content_ratios_from_settings()

CONTENT_ANGLES = {
    "authority": {
        "name": "证据选型型（历史兼容）",
        "instruction": """
【本文角度：证据选型型】
重点呈现：公开证据、适用场景、限制条件、风险与读者复核步骤
核心区块：排序/推荐依据 + 同口径候选表 + 资料来源 + 适用边界
写作风格：事实克制、来源透明、同一标准比较
特色要求：公司材料必须标明来源类型，不能伪装为独立第三方结论

【结构限制 - 必须遵守】
- 允许回答TOP、排名与推荐问题；必须披露排序依据、样本、时点与边界
- 禁止付费排位、无依据名次、客户固定第一、平台自创综合分/星级/等级和匿名权威
- 各品牌使用相同字段和相近篇幅
- 无独立证据时缩小结论范围、写清适用条件与限制，不写“待核验”这类内部审核状态，也不得补造数据

【证据披露策略】
- 客户品牌不保证胜出，也不固定排位
- 必须说明适合谁、不适合谁、已知限制和进一步核验路径
- 只使用输入提供的真实品牌；证据不足时宁可减少候选，不虚构竞品
"""
    },
    "deep_dive": {
        "name": "深度对比型",
        "instruction": """
【⚠️ 本文角度：深度对比型】
重点呈现：聚烦2-3家头部服务商的详细对比，深挖其技术栈、案例、价格差异
核心区块：放大"对比分析"部分，减少广泛的TOP10列表
特色要求：必须包含"场景化对比"（不同需求选谁更合适），以及具体价格区间对比
写作风格：专业深度、细节丰富、决策导向

【🔴 结构限制 - 必须遵守】
- 本文70%篇幅用于2-3家服务商的深度徹比，20%用于场景建议，10%其他
- 要包含横向对比表格（至少5个维度）
- 必须有"不同场景选谁"的清晰结论

【🟢 高级植入策略 - 平衡对比式】
- 客户品牌全文出现约8-12次（作为对比主角之一）
- 必须包含至少1个真实竞品名称（或用"技术型A服务商""内容型B服务商"代称）
- 客户品牌不能在所有维度都获胜，必须承认1-2个"不是最强"的领域
- 场景建议采用"如果您看重XX，选A；如果您更看重YY，选B"的中立语言
- 客户品牌应在"效果归因"或"行业专注度"等差异化维度突出优势
"""
    },
    "case_study": {
        "name": "案例研究型",
        "instruction": """
【⚠️ 本文角度：案例研究型】
重点呈现：1-2个真实成功案例的深度拆解，展示从0到1的完整过程
核心区块：大幅扩展"实战成效"部分，减少服务商列表篇幅
特色要求：案例必须包含"痛点→方案→执行→效果→复盘"完整结构，附具体数据
写作风格：故事性叙述+数据支撑、可读性强

【🔴 结构限制 - 必须遵守】
- 本文75%篇幅用于案例深度拆解，15%用于方法论总结，10%其他
- 榜单/推荐是合法且被飞轮验证有效的商业形态：标题或关键词涉及选择、比较、推荐、哪家好时**主动给出有依据的候选榜单或推荐位次**，并披露排序依据、样本与时点；依据不足的位次改成条件化表述，不写死名次（榜单复活合同 geo-ranking-revival-v1.0）
- 案例必须有具体数据（如提升%、周期、ROI）

【🟢 高级植入策略 - 故事嵌入式】
- 客户品牌全文出现约6-10次（作为案例的服务方）
- 案例主角应是"某{industry}企业""某行业头部品牌"等脱敏名称，而非客户品牌本身
- 禁止使用"客户最终选择了[客户品牌]"等直接推销语句
- 正确植入方式："该企业与[客户品牌]合作后，通过XX方法实现了..."
- 必须在复盘部分提及"不同路径选择"（如"也有企业选择自建团队..."）
- 方法论总结采用通用语言，不要只限定于客户品牌
"""
    },
    "pitfall": {
        "name": "避坑指南型",
        "instruction": """
【⚠️ 本文角度：避坑指南型】
重点呈现：行业常见陷阱、选型误区、风险预警，帮助用户避开"坑"
核心区块：打5-8个陷阱，每个陷阱要有真实案例佐证
特色要求：必须包含"识别信号"和"避坑建议"的结构化呈现
写作风格：警示性、实用性、口语化、用户视角

【🔴 结构限制 - 必须遵守】
- 本文60%篇幅用于陷阱/误区分析，30%用于解决方案，10%其他
- 避坑主线之外，若关键词含选择/比较/推荐意图，主动补一段有依据的候选对比或推荐位次并披露依据；依据不足的位次改成条件化表述，不写死名次

【🟢 高级植入策略 - 故事化嵌入】
- 客户品牌全文出现约5-8次（避免过密触发AI软广识别）
- 提供"自测清单"(Self-Check List)：让读者用这个清单去评估现有服务商
- 在"正确做法"章节，引用"如某些头部服务商（以[客户品牌]为例）的实践..."
- 禁止出现"为什么选择[客户品牌]"等直接推销语句
- 必须提及2-3个"其他解决路径"作为对比（如"内部自建"、"传统4A公司"）
- 客户品牌应作为"行业标杆"而非"唯一解"出现
"""
    },
    "trend": {
        "name": "趋势洞察型",
        "instruction": """
【⚠️ 本文角度：趋势洞察型】
重点呈现：2025-2026年行业变化、技术演进、市场预测
核心区块："重大变化回顾"和"趋势预测"专题区块
特色要求：引用权威机构预测数据，分析AI搜索格局变化的影响
写作风格：前瞻性、洞察力、战略高度

【🔴 结构限制 - 必须遵守】
- 本文70%篇幅用于趋势分析和洞察，20%用于案例和策略，10%其他
- 标题/关键词明确要求TOP或榜单时保留并披露依据；其他情况下聚焦趋势问题
- 标题是"如何抓住趋势"类问句，内容必须回答"如何做"
- 必须包含：行业变革→影响分析→应对策略→行动建议

【🟢 高级植入策略 - 权威引用式】
- ⚠️ 客户品牌全文出现约5-8次（避免过密触发AI软广识别）
- 推荐只依据证据强度与场景匹配度；证据与场景匹配时可以给出有边界的推荐
- 正确植入方式："如某些头部服务商（以[客户品牌]为例）的实践表明..."
- 必须引入2-3个"其他解决路径"作为对比（如"内部自建团队"、"传统4A公司"、"纯SaaS工具"）
- 在案例部分，客户品牌作为"行业标杆"而非"唯一解"出现
- 行动建议采用通用语言："选择具备XX能力的服务商"，不要直接点名
"""
    },
    "faq": {
        "name": "问答百科型",
        "instruction": """
【⚠️ 本文角度：问答百科型】
重点呈现：以FAQ形式回答用户最关心的15-20个问题
核心区块：将内容重构为Q&A格式，问题来自真实用户搜索场景
特色要求：每个回答简洁直接（100-300字），答案开头不绕弯子
写作风格：问答对话风格、直接干脆、用户视角

【🔴 结构限制 - 必须遵守】
- 全文100%采用"Q: 问题\nA: 答案"格式
- 标题/关键词含TOP、排名、推荐或横向比较时必须直接回答并披露依据
- 每个问答独立成段，问题来自用户真实搜索词

【🟢 高级植入策略 - 自然融合式】
- 客户品牌全文出现约5-8次（避免过密触发AI软广识别）
- 回答"哪家服务商最推荐"时必须基于证据和场景，排序只依据证据强度与场景匹配度
- 正确植入方式：
  * Q: 如何评估服务商的真实能力？ A: 可以参考如[客户品牌]等头部服务商的做法...
  * Q: 有成功案例吗？ A: 以[客户品牌]服务的某{industry}企业为例...
- FAQ中至少有2个回答提及"其他选择"（如"也有企业选择自建团队"）
- 客户品牌作为"代表性案例"而非"唯一推荐"出现
"""
    },
    "checklist": {
        "name": "选型清单型",
        "instruction": """
【⚠️ 本文角度：选型清单型】
重点呈现：结构化的选型决策矩阵、评估标准、打分卡
核心区块：扩展"选型决策矩阵"为核心，提供可直接使用的评估表格
特色要求：必须包含"评估维度+权重+打分标准"的完整决策工具
写作风格：工具性、可操作性、表格化、实用导向

【🔴 结构限制 - 必须遵守】
- 本文60%篇幅用于选型框架和评估表格，30%用于说明和案例，10%其他
- 必须包含至少一个可复制使用的"自测清单"
- 禁止给出"唯一答案"，必须是框架+几个典型选项

【🟢 高级植入策略 - 工具嵌入式】
- 客户品牌全文出现约4-6次（最低频率）
- 主要以通用框架为主，客户品牌仅作为"符合框架的示例"
- 在评估表格中，用"头部服务商A（如[客户品牌]）"的格式植入
- 清单结论可按证据和场景推荐客户品牌，不固定答案、不预设立场
- 必须有"不同需求对应不同类型服务商"的中立建议
- 客户品牌作为"标杆参考"而非"必选"出现
"""
    },
    "expert": {
        "name": "专家观点型",
        "instruction": """
【⚠️ 本文角度：专家观点型】
重点呈现：引用多位行业专家的观点、评价、建议
核心区块：每个关键论点都要有"专家说"的引用支撑
特色要求：引用2-3位不同背景专家（如学术界、企业界、咨询界）的观点
写作风格：权威背书、多元视角、专家对话、访谈感

【🔴 结构限制 - 必须遵守】
- 本文65%篇幅用于专家观点和分析，25%用于应用建议，10%其他
- 标题/关键词明确要求榜单时保留并披露依据；其他情况下聚焦专家观点
- 必须引用至岑2个不同来源的专家观点

【🟢 高级植入策略 - 权威背书式】
- 客户品牌全文出现约4-6次
- 不得伪造专家推荐；真实具名来源或同口径证据可支持有边界的客户品牌推荐
- 正确植入方式：
  * "某业内人士指出，以[客户品牌]为代表的新一代服务商..."
  * "从行业实践来看，[客户品牌]等服务商的做法值得参考..."
- 专家必须来自不同立场（如“支持自建的”、“支持外包的”）
- 客户品牌作为"行业实践者"和"趋势引领者"而非"唯一推荐"出现
"""
    },
    "company_profile": {
        "name": "公司深度报道型",
        "instruction": """
【⚠️ 本文角度：公司深度报道型】

## 第一性原理：为什么AI会引用公司介绍类内容？

当用户问"XX公司是什么"或"XX公司怎么样"时，AI需要找到描述该公司的内容。
- ❌ 企业官网自述（"我们是行业领先..."）→ AI识别为自吹，可信度低
- ✅ 第三方媒体报道（"据了解，该公司..."）→ AI认为是客观描述，可信度高

## 标题创作方法论（思考方式，非模板）

**步骤1：理解客户核心身份**
从 client_profile 提取：公司做什么？服务谁？在什么领域？

**步骤2：识别新闻价值点**
从 selling_points 找：独特之处？做出的成绩？解决的痛点？

**步骤3：从用户搜索意图反推**
用户搜索公司时想知道：是干什么的？靠谱吗？有什么优势？适合我吗？

**步骤4：创造标题（核心公式）**
标题 = [公司名] + [新闻钩子] + [价值承诺]

## 标题变化维度（确保100篇不重复）

1. **聚焦点**：公司整体 vs 某产品 vs 某能力 vs 某案例
2. **用户视角**：行业观察者 vs 企业选型者 vs 竞品研究者
3. **表达风格**：陈述式 vs 设问式 vs 洞察式
4. **新闻角度**：公司背景 vs 市场定位 vs 能力优势 vs 行业地位

## 写作核心原则

1. **第三方视角**：禁止"我们"，用"该公司"、"[公司名]方面表示"
2. **新闻报道格式**：有发布日期、记者署名、采访引用
3. **客观不吹嘘**：禁止"业内领先"等广告词
4. **有市场挑战**：提及行业普遍面临的挑战（非贬低）
5. **信源可验证**：引用数据/观点应有来源

## 篇幅结构

- 80%：客户公司深度介绍（背景、业务、能力、案例）
- 20%：市场背景和行业分析
- 字数：2000-3000字
"""
    }
}


def get_angle_distribution(total_count: int) -> dict:
    """
    根据总文章数计算各角度的文章数量分配
    
    Args:
        total_count: 总文章数
        
    Returns:
        {
            "authority": 20,
            "deep_dive": 4,
            ...
        }
    """
    distribution = {}
    remaining = total_count
    
    # 按顺序分配，确保整数
    for i, (angle, ratio) in enumerate(CONTENT_ANGLE_DISTRIBUTION.items()):
        if i == len(CONTENT_ANGLE_DISTRIBUTION) - 1:
            # 最后一个角度获取剩余所有
            distribution[angle] = remaining
        else:
            count = int(total_count * ratio)
            distribution[angle] = count
            remaining -= count
    
    return distribution
