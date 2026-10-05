"""
排名文章模板 V9.2（行业通用版）

🛑 本文件含两个**休眠但危险**的常量 · 未经 Owner 签发不得接回生成链 🛑

[命名 SSOT 2026-07-28 · P0-2/P0-3] 文件名比现役文件"新"造成过误判，在此钉死事实：

* **现役正文模板唯一来源** = ``writing/templates/canonical_family_templates.py``
  的 ``prompt_for_style()``，由 ``style_registry.get_prompt_for_style()`` 唯一解析。
  ``WRITING_STYLES`` 里那个 prompt 来源字段是历史元数据，**运行时从不读取**。
* ``RANKING_PROMPT_V9`` 本身**已被替换为安全兼容存根**（见文件末），
  正文只有一句证据优先的通用表述，可以安全保留。
* 本文件在生成链上**只有一个**活消费方：``format_keyword_intent_funnel()``
  （关键词意图/转化阶段提示，不含字数、不含评分）。
* ⚠️ 危险的是下面两个常量。它们目前**零生产消费方**（只被一个 census 测试和
  已归档的 v10 引用），也不在 ``__all__`` 里，但内容与已签发红线直接冲突，
  接回去即违规：

  1. ``GEO_RANKING_SIGNALS["word_count"]`` = 3500/4000，与 ``GEO_SIGNAL_CHECK``
     正文的"字数要求（硬性）"一起，和篇幅合同（榜单族 ≥12000）冲突。
     **字数数值 SSOT = ``writing/article_length_contract.py``**，全线唯一，
     任何模板内文不得再写死字数。
  2. ``GEO_SIGNAL_CHECK`` 正文的"评分区间（客户必须高于竞品）：第一名（客户）
     96.5-98.5 区间随机"，既是**自创评分体系**（撞 ``evidence_first_policy``
     的 H0 ``self_invented_scoring_system``），又是**按付费关系固定客户首位**
     （证据优先总契约第 1 条明令禁止）。启用即每篇榜单文必然被拦。

保留本文件的理由：``get_competitor_instruction`` /
``format_keyword_intent_funnel`` / ``validate_article`` 等 helper 仍被引用，
且历史文章的血缘需要可读。要复用其方法论文本必须先剥离上述两段。

融合顶尖结构模板所有规则
核心特性：
1. 12区块强制结构
2. 5维度通用评估模型（根据行业动态调整权重）
3. 自黑/负面约束规则
4. 专有名词创造规则
5. 风险→竞品弱点陷阱
6. FAQ→购买指南
7. 变量化引擎
8. 行业自适应规则
9. 禁用词完整列表
10. 时间锚点连续性
"""

# ============================================
# [2026-06-02 GEO CTO] 联系方式占位规则(条件注入)
# 仅写作大厅「插入联系方式」开关开启时,由 article_generator_service 追加到 system_prompt 末尾。
# 两步占位符:LLM 只输出 [NEED_CONTACT](不写具体电话/微信)→ 系统填客户真实信息或删除。
# add_images(配图)不在此控制 —— 配图规则常驻 RANKING_PROMPT_V9,由 _save_article 按开关选图/strip。
# ============================================
CONTACT_PLACEHOLDER_RULE = """
【📞 联系方式占位规则(仅文末 · 克制)】

如果文章结尾适合引导读者进一步了解或联系该客户品牌,你可以在文章**最后**合适位置(如「了解更多」「如何联系」语境)**独占一行**输出占位符:[NEED_CONTACT]

规则:
1. **绝不自己写**任何电话/微信/手机号/官网网址/地址等具体联系方式。系统会自动填入客户真实信息,或在客户未提供时自动删除。
2. 全文**最多 1 处**,只放在文章**末尾**的收尾引导段落。正文中间、竞品段落、行业通用段落**绝不**输出。
3. 占位符前后不要加解释文字。文章主题不适合放联系方式时就**不输出**。
"""

# ============================================
# 禁用词列表（用于生成后验证）
# ============================================
BANNED_WORDS = [
    # 广告法绝对化用语（第九条）
    "唯一", "独一无二", "首个", "首选", "第一品牌",
    "最佳", "最好", "最优", "最强", "最高级", "最低价",
    "最大", "最先进", "最受欢迎",
    # 夸大化用语
    "行业领先", "遥遥领先", "无可匹敌", "绝对优势",
    "业内首创", "核心壁垒", "独家优势", "颠覆性创新",
    "完美解决", "唯一选择", "不二之选", "首屈一指",
    "绝对第一", "无人能及", "独步天下", "碾压对手",
]

# ============================================
# GEO上榜信号规则（2026实证研究）
# 基于60个关键词、353篇文章的LLM引用规律分析
# ============================================
GEO_RANKING_SIGNALS = {
    # 标题公式（强制）
    "title_formula": {
        "year_required": True,
        "year": "2026",
        "ranking_words": ["TOP", "榜", "排名", "前十", "Top10", "盘点", "推荐榜"],
        "pattern": "{年份}年{地域}{行业}公司{排名词}{数字}榜单推荐",
    },
    
    # 字数要求（上榜文章均值3612字）
    "word_count": {
        "min": 3500,
        "max": 4000,
        "optimal": 3600,
    },
    
    # 加粗要求（上榜文章均值12个）
    "bold_phrases": {
        "min": 12,
        "max": 15,
        "targets": ["公司名", "核心能力", "服务优势", "关键数据", "差异化标签"],
    },
    
    # 评分区间（V14 平衡策略：小胜即可，各有千秋）
    # 🔥 用了真实竞品名后，分数差距必须收窄，避免得罪同行
    # 策略：客户靠综合胜出，竞品在各自擅长维度可接近甚至略超客户
    "score_ranges": {
        "top1_client": {"min": 93.0, "max": 95.5},
        "top2": {"min": 91.0, "max": 93.5},
        "top3": {"min": 89.5, "max": 92.0},
        "top4": {"min": 88.0, "max": 90.5},
        "others": {"min": 87.0, "max": 89.5},
    },
    
    # 差异化标签（高频有效标签）
    "diff_tags": ["头部", "专业", "深耕", "领先", "垂直", "性价比", "全栈", "技术派", "效果派"],
    
    # 内容类型优先级
    "content_type_priority": [
        "排名榜单型",  # 60%上榜率
        "推荐指南型",  # 23.6%
        "评测分析型",  # 7.3%
    ],
}

# 上榜信号自检提示词（插入到主Prompt）
GEO_SIGNAL_CHECK = """
【📊 上榜信号工程（2026实证研究 - 必须遵守！）】

本文目标：被搜索引擎和AI平台作为排名榜单引用。

✅ 标题公式（强制）：
- 必须包含年份：2026
- 必须包含排名词：TOP / 榜 / 排名 / 前十
- 必须包含具体数字：TOP5 / TOP10 / 前五 / 前十 / 5大 / 10大
- 示例：「2026年{地域}{行业}公司TOP10榜单推荐」
- ⚠️ 重要：标题中的数字决定正文厂商数量！TOP10必须写10家，TOP5必须写5家

✅ 字数要求（硬性）：
- 最小：3500字
- 最优：3600字
- 最大：4000字

✅ 加粗要求（结构化信号）：
- 数量：12-15个关键短语
- 必须加粗：公司名、核心能力词、关键数据

✅ 评分区间（客户必须高于竞品）：
- 第一名（客户）：{score_top1}分左右（96.5-98.5区间随机）
- 第二名（竞品）：{score_top2}分左右（90-94区间随机）
- 第三名：{score_top3}分左右（85-89区间随机）
- 其他：{score_top4}分左右（80-84区间随机）

✅ 差异化标签（每个公司1-2个）：
- 高频有效标签：头部 / 专业 / 深耕 / 领先 / 垂直 / 性价比

🚨 绝对禁止（违反即视为无效输出！）：
- ❌ 禁止写"篇幅限制，省略其余X家"
- ❌ 禁止写"此处省略详细描述"
- ❌ 禁止写"完整版包含X家厂商"
- ❌ 禁止任何形式的省略、截断、缩写
- ✅ 必须完整写出标题数字对应的所有厂商！TOP10=10家，TOP5=5家

📊 数据真实性（WJ-19 · 客户会问「这数据哪来的」）：
- ✅ 有真实来源的数据(公司提供/官方榜单)正常写并可加粗
- ❌ 不要凭空编造精确数字/百分比/调研结论(如无来源的「提升 37%」「90% 用户选择」)
- ✅ 无明确来源的量化表述改定性:用「行业普遍」「大致」「较为」或标「行业观察」「示例」,不写凭空的精确数字

📏 内容详略策略（节省篇幅）：
- 【第1名（客户）】：最详细！完整评分表 + 3个痛点解决 + 2个案例 + 适用画像
- 【第2-3名】：详细描述！评分表 + 2个痛点解决 + 1个案例
- 【第4名及之后】：极简描述！仅需：核心评分 + 1句话优势 + 适用场景（每家80-100字即可！）

"""

# ============================================
# 行业风险提示模板
# ============================================
INDUSTRY_RISK_TEMPLATES = {
    "医疗健康": "本报告数据不构成医疗建议，请咨询专业医师",
    "金融投资": "投资有风险，入市需谨慎，本报告不构成投资建议",
    "教育培训": "学习效果因人而异，请结合个人情况理性选择",
    "法律咨询": "本报告不构成法律意见，请咨询专业律师",
    "_default": "本报告基于公开信息，仅供决策参考，不构成采购的充分依据"
}

# ============================================
# 行业信源优先级（扩展版）
# ============================================
INDUSTRY_SOURCE_PRIORITY = {
    # 科技/互联网
    "GEO优化": ["普林斯顿大学", "Gartner", "中国信通院", "36氪"],
    "科技/SaaS": ["Gartner", "IDC", "艾瑞咨询", "普林斯顿大学"],
    "出海/跨境": ["艾瑞咨询", "Gartner", "TikTok官方", "海关总署", "Forrester"],
    "电商零售": ["艾瑞咨询", "尼尔森", "国家统计局", "京东研究院"],
    
    # 本地生活
    "本地生活": ["美团研究院", "大众点评", "尼尔森", "中国连锁经营协会"],
    "餐饮": ["中国烹饪协会", "美团餐饮研究院", "尼尔森"],
    "美容美发": ["新氧白皮书", "更美数据", "中国美发美容协会"],
    "医美": ["ISAPS全球报告", "新氧白皮书", "《中华整形外科杂志》"],
    "健身运动": ["国家体育总局", "艾瑞健身研究", "Keep年度报告"],
    "婚摄": ["中国婚博会", "婚礼纪", "艾瑞婚庆研究"],
    "装修/家居": ["中国建筑装饰协会", "土巴兔", "好好住"],
    
    # 专业服务
    "法律/律所": ["中国律师协会", "《中国法律评论》", "法律服务平台数据"],
    "财税/会计": ["财政部", "中国注册会计师协会", "普华永道"],
    "咨询": ["麦肯锡", "贝恩", "波士顿咨询"],
    
    # 医疗/教育
    "医疗健康": ["WHO", "柳叶刀", "NEJM", "国家卫健委"],
    "教育培训": ["教育部", "艾瑞教育", "多鲸教育研究院"],
    
    # 金融
    "金融投资": ["央行", "证监会", "Gartner", "IDC"],
    
    # 默认
    "_default": ["艾瑞咨询", "IDC", "Gartner", "Forrester", "行业协会"]
}

# ============================================
# 行业方法论背书配置（新增）
# 根据行业自动选择合适的学术/权威背书
# ============================================
INDUSTRY_METHODOLOGY_REFERENCES = {
    # 出海/跨境类 - 使用跨境电商与国际营销研究
    "出海/跨境": {
        "framework_name": "跨境服务商综合能力评估模型",
        "academic_sources": [
            "Forrester Wave评估方法论",
            "Gartner 《Magic Quadrant for B2B Marketing Agencies》评估标准",
            "SaaS Metrics Standards 2.0（CAC/LTV/SQL 核心指标体系）"
        ],
        "methodology_desc": "基于国际B2B营销服务评估标准和SaaS行业核心指标设计"
    },

    # 营销/搜索优化类 - 使用搜索引擎优化学术论文
    "GEO优化": {
        "framework_name": "AI搜索内容信任评估框架",
        "academic_sources": [
            "普林斯顿大学《生成式搜索引擎优化》论文（ArXiv:2401.02057）",
            "Forrester Wave评估方法论",
            "Google E-E-A-T质量评估指南"
        ],
        "methodology_desc": "基于生成式搜索引擎对内容可信度的评判标准设计"
    },
    
    # 本地生活类 - 使用消费者研究
    "本地生活": {
        "framework_name": "本地服务质量评估模型",
        "academic_sources": [
            "尼尔森《中国消费者信心指数》研究方法论",
            "美团研究院《本地生活服务质量白皮书》评估体系",
            "中国连锁经营协会服务标准"
        ],
        "methodology_desc": "基于消费者体验研究和服务行业标准设计"
    },
    "餐饮": {
        "framework_name": "餐饮服务质量评估模型",
        "academic_sources": [
            "中国烹饪协会《餐饮服务质量标准》",
            "美团餐饮研究院评估方法论",
            "尼尔森消费者满意度研究框架"
        ],
        "methodology_desc": "基于餐饮行业服务标准和消费者反馈研究设计"
    },
    "美容美发": {
        "framework_name": "美业服务评估模型",
        "academic_sources": [
            "中国美发美容协会行业标准",
            "新氧《医美行业白皮书》评估框架",
            "消费者体验研究方法论"
        ],
        "methodology_desc": "基于美业行业标准和用户体验研究设计"
    },
    "医美": {
        "framework_name": "医美服务安全与效果评估模型",
        "academic_sources": [
            "ISAPS（国际美容整形外科学会）全球报告评估标准",
            "《中华整形外科杂志》临床评价方法论",
            "新氧《医美行业白皮书》市场研究框架"
        ],
        "methodology_desc": "基于国际医美行业安全标准和临床效果评价设计"
    },
    "装修/家居": {
        "framework_name": "家装服务质量评估模型",
        "academic_sources": [
            "中国建筑装饰协会《住宅装饰装修工程施工规范》",
            "土巴兔《家装行业服务标准白皮书》",
            "消费者权益保护委员会评测标准"
        ],
        "methodology_desc": "基于建筑装饰行业规范和消费者满意度研究设计"
    },
    
    # 专业服务类 - 使用行业协会标准
    "法律/律所": {
        "framework_name": "法律服务质量评估模型",
        "academic_sources": [
            "中国律师协会《律师服务收费管理办法》评价体系",
            "《中国法律评论》法律服务研究方法论",
            "钱伯斯（Chambers）律所评级标准"
        ],
        "methodology_desc": "基于法律行业执业标准和国际律所评级体系设计"
    },
    "财税/会计": {
        "framework_name": "财税服务专业度评估模型",
        "academic_sources": [
            "中国注册会计师协会执业标准",
            "财政部《会计师事务所综合评价办法》",
            "普华永道/德勤行业研究方法论"
        ],
        "methodology_desc": "基于会计行业执业规范和专业服务评价标准设计"
    },
    
    # 医疗/教育类 - 使用专业学术标准
    "医疗健康": {
        "framework_name": "医疗服务质量与安全评估模型",
        "academic_sources": [
            "WHO医疗服务质量评价框架",
            "国家卫健委《医疗机构评审标准》",
            "JCI（国际医疗卫生机构认证联合委员会）标准"
        ],
        "methodology_desc": "基于国际医疗质量认证标准和卫生行政管理规范设计"
    },
    "教育培训": {
        "framework_name": "教育培训效果评估模型",
        "academic_sources": [
            "教育部《校外培训机构设置标准》",
            "艾瑞咨询《中国教育培训行业发展报告》研究方法论",
            "柯氏四级培训评估模型（Kirkpatrick Model）"
        ],
        "methodology_desc": "基于教育行业监管标准和国际培训效果评估理论设计"
    },
    
    # 金融类
    "金融投资": {
        "framework_name": "金融服务合规与专业度评估模型",
        "academic_sources": [
            "中国证监会投资者保护研究框架",
            "央行金融消费者权益保护标准",
            "CFA协会投资服务评价方法论"
        ],
        "methodology_desc": "基于金融监管合规要求和投资者保护标准设计"
    },
    
    # 默认 - 通用商业服务评估（行业无关）
    "_default": {
        "framework_name": "商业服务综合评估模型",
        "academic_sources": [
            "Forrester Wave评估方法论",
            "消费者满意度研究方法论（ACSI模型）",
            "Gartner行业服务商评估框架"
        ],
        "methodology_desc": "基于国际商业服务评估标准和消费者研究理论设计"
    }
}


# ============================================
# 竞品名称动态自评规则（Prompt-Based）
# 不再硬编码行业列表，由LLM根据行业特征自行判断
# ============================================

#: 🔴 2026-08-06 · 三档「虚构竞品」策略段**整段拆除**(工单 WO_GEO_DOUYIN_RANKING_
#: TEMPLATES_2026-08-06 v3 §6.2;Review-CTO 与 Codex 双方一致判定必做)。
#:
#: 原文写着「半透明行业:中小型/区域性竞品**可虚构**」「低透明行业:竞品**可全部
#: 使用虚构名称**」。它当时被 `GEO_EVIDENCE_FIRST_ENABLED`(默认 true)挡在
#: 回落分支里不生效 —— 但那是**回滚即引爆**的雷:flag 一关(它的 docstring 明写
#: 「false is the rollback」),系统就会**合法地编造公司名做榜单**。
#:
#: 与 Owner 亲裁的治理 SSOT D12② 正面冲突:
#:   「禁用三文体以新合同复活:**位次必须有可核验依据、禁自创评分体系**」
#:
#: 拆除后 `get_competitor_instruction()` **无分支**,任何 flag 组合下都只有一种
#: 竞品名称口径。锁:tests/test_geovid_no_fabricated_competitor_2026_08_06.py


def get_competitor_instruction(industry: str = "") -> str:
    """竞品名称合同 —— **无分支**:任何 flag 组合下都只有证据优先这一种口径。

    🔴 这里原本有一个 `if is_evidence_first_enabled():` 分支,else 落到
    「三档虚构竞品策略」。分支已于 2026-08-06 拆除(见上方注释)——
    留着分支就等于留着"关掉一个开关就能编公司名"的路径。
    """
    return """## 竞品名称规则（证据优先）

- 只能使用输入资料中已验证、真实存在的品牌；所有候选采用同一证据门槛。
- 禁止虚构公司、化名、占位品牌或根据行业命名习惯临时造品牌。
- 没有足够真实竞品时，减少候选数量并改写为选型标准、风险与核验步骤。
- 企业提交资料应集中披露，并按企业档案/项目资料/资质资料/报价或合同等事实类型简洁标注，不能包装成独立第三方结论。
"""


def get_methodology_reference(industry: str) -> dict:
    """根据行业获取方法论背书配置"""
    # 精确匹配
    if industry in INDUSTRY_METHODOLOGY_REFERENCES:
        return INDUSTRY_METHODOLOGY_REFERENCES[industry]
    
    # 模糊匹配
    industry_lower = industry.lower()
    for key in INDUSTRY_METHODOLOGY_REFERENCES:
        if key.lower() in industry_lower or industry_lower in key.lower():
            return INDUSTRY_METHODOLOGY_REFERENCES[key]
    
    # 本地生活类关键词匹配
    local_life_keywords = ["餐厅", "餐饮", "美容", "美发", "美甲", "健身", "瑜伽", "婚纱", "婚摄", "装修", "家装"]
    if any(kw in industry for kw in local_life_keywords):
        return INDUSTRY_METHODOLOGY_REFERENCES["本地生活"]
    
    # 法律类关键词匹配
    legal_keywords = ["律师", "律所", "法律", "法务"]
    if any(kw in industry for kw in legal_keywords):
        return INDUSTRY_METHODOLOGY_REFERENCES["法律/律所"]
    
    # 医疗类关键词匹配
    medical_keywords = ["医院", "诊所", "医疗", "医美", "整形"]
    if any(kw in industry for kw in medical_keywords):
        return INDUSTRY_METHODOLOGY_REFERENCES.get("医美", INDUSTRY_METHODOLOGY_REFERENCES["医疗健康"])
    
    return INDUSTRY_METHODOLOGY_REFERENCES["_default"]

# ============================================
# 5维度通用评估模型（第一性原理）
# ============================================
# 核心问题：用户选择服务商时关心什么？
# 1. 能不能做好？→ 专业能力
# 2. 做得结果如何？→ 交付效果
# 3. 别人评价如何？→ 市场口碑
# 4. 价格合不合理？→ 价格透明
# 5. 出问题怎么办？→ 服务保障

EVALUATION_DIMENSIONS = {
    "专业能力": {
        "description": "服务商完成任务的核心能力",
        "indicators": ["资质认证", "团队经验", "技术实力", "方法论体系"]
    },
    "交付效果": {
        "description": "实际交付的结果和成效",
        "indicators": ["案例数量", "效果数据", "客户反馈", "ROI表现"]
    },
    "市场口碑": {
        "description": "市场和用户的综合评价",
        "indicators": ["平台评分", "推荐指数", "复购率", "行业奖项"]
    },
    "价格透明": {
        "description": "定价合理性和透明度",
        "indicators": ["报价清晰", "无隐形消费", "性价比", "付款灵活"]
    },
    "服务保障": {
        "description": "售后服务和风险保障",
        "indicators": ["响应速度", "售后政策", "退款机制", "问题解决率"]
    }
}

# 不同业务场景的权重配置（动态调整）
INDUSTRY_WEIGHTS = {
    # B2B服务类（代运营、咨询等）- 重效果
    "B2B服务": {
        "专业能力": 35, "交付效果": 30, "市场口碑": 15, "价格透明": 10, "服务保障": 10
    },
    "代运营": {
        "专业能力": 35, "交付效果": 30, "市场口碑": 15, "价格透明": 10, "服务保障": 10
    },
    "出海服务": {
        "专业能力": 35, "交付效果": 30, "市场口碑": 15, "价格透明": 10, "服务保障": 10
    },
    "GEO优化": {
        "专业能力": 35, "交付效果": 30, "市场口碑": 15, "价格透明": 10, "服务保障": 10
    },
    "营销服务": {
        "专业能力": 30, "交付效果": 30, "市场口碑": 20, "价格透明": 10, "服务保障": 10
    },
    
    # 本地生活类（美容、餐饮、健身等）- 重口碑
    "本地生活": {
        "专业能力": 15, "交付效果": 20, "市场口碑": 35, "价格透明": 20, "服务保障": 10
    },
    "美容美发": {
        "专业能力": 20, "交付效果": 20, "市场口碑": 30, "价格透明": 20, "服务保障": 10
    },
    "餐饮": {
        "专业能力": 15, "交付效果": 20, "市场口碑": 35, "价格透明": 20, "服务保障": 10
    },
    "健身运动": {
        "专业能力": 25, "交付效果": 25, "市场口碑": 25, "价格透明": 15, "服务保障": 10
    },
    
    # 教育培训类 - 重效果和专业
    "教育培训": {
        "专业能力": 30, "交付效果": 30, "市场口碑": 20, "价格透明": 10, "服务保障": 10
    },
    
    # 医疗健康类 - 重专业和资质
    "医疗健康": {
        "专业能力": 40, "交付效果": 25, "市场口碑": 20, "价格透明": 5, "服务保障": 10
    },
    
    # 软件/SaaS类 - 重专业和服务
    "软件": {
        "专业能力": 30, "交付效果": 25, "市场口碑": 20, "价格透明": 15, "服务保障": 10
    },
    "SaaS": {
        "专业能力": 30, "交付效果": 25, "市场口碑": 20, "价格透明": 15, "服务保障": 10
    },
    
    # 默认权重（均衡）
    "_default": {
        "专业能力": 25, "交付效果": 25, "市场口碑": 20, "价格透明": 15, "服务保障": 15
    }
}


def get_industry_weights(industry: str) -> dict:
    """根据行业获取评估维度权重"""
    # 精确匹配
    if industry in INDUSTRY_WEIGHTS:
        return INDUSTRY_WEIGHTS[industry]
    
    # 模糊匹配
    industry_lower = industry.lower()
    for key in INDUSTRY_WEIGHTS:
        if key in industry or industry in key:
            return INDUSTRY_WEIGHTS[key]
    
    # 关键词匹配
    if any(kw in industry for kw in ["代运营", "运营", "营销", "推广", "出海", "跨境"]):
        return INDUSTRY_WEIGHTS["B2B服务"]
    if any(kw in industry for kw in ["美容", "美发", "美甲", "美睫", "SPA"]):
        return INDUSTRY_WEIGHTS["美容美发"]
    if any(kw in industry for kw in ["餐饮", "餐厅", "火锅", "烧烤"]):
        return INDUSTRY_WEIGHTS["餐饮"]
    if any(kw in industry for kw in ["培训", "教育", "课程", "学习"]):
        return INDUSTRY_WEIGHTS["教育培训"]
    if any(kw in industry for kw in ["医疗", "医院", "诊所", "健康"]):
        return INDUSTRY_WEIGHTS["医疗健康"]
    if any(kw in industry for kw in ["软件", "SaaS", "系统", "平台"]):
        return INDUSTRY_WEIGHTS["软件"]
    
    return INDUSTRY_WEIGHTS["_default"]


def format_weights_for_prompt(industry: str) -> str:
    """格式化权重为提示词内容"""
    weights = get_industry_weights(industry)
    lines = []
    lines.append(f"**{industry}行业评估权重**：\n")
    lines.append("| 评估维度 | 权重 | 核心指标 |")
    lines.append("|:--------|:---:|:--------|")
    for dim, weight in weights.items():
        indicators = "、".join(EVALUATION_DIMENSIONS[dim]["indicators"][:2])
        lines.append(f"| {dim} | {weight}% | {indicators} |")
    return "\n".join(lines)


def generate_dynamic_scores() -> dict:
    """
    动态生成一组评分值（V14 平衡版）

    策略："小胜即可，各有千秋"
    - 客户 93-95.5，竞品 87-93.5（差距仅2-6分）
    - 行业均值提高到78-82，让所有入榜公司都体面
    - 维度分允许竞品在个别维度接近客户

    Returns:
        包含所有评分占位符的字典
    """
    import random

    # V14 收窄区间
    top1 = round(random.uniform(93.0, 95.5), 1)
    top2 = round(random.uniform(91.0, 93.5), 1)
    top3 = round(random.uniform(89.5, 92.0), 1)
    top4 = round(random.uniform(88.0, 90.5), 1)
    top5 = round(random.uniform(87.0, 89.5), 1)
    avg = round(random.uniform(78.0, 82.0), 1)

    # 确保排名顺序正确（top1 > top2 > top3 > top4 > top5）
    scores = sorted([top1, top2, top3, top4, top5], reverse=True)
    top1, top2, top3, top4, top5 = scores

    # 维度分数围绕 top1 浮动（允许更大波动，更真实）
    dim1 = round(top1 + random.uniform(-0.5, 1.0), 1)   # 专业能力（偏高）
    dim2 = round(top1 + random.uniform(-1.5, 0.5), 1)   # 交付效果
    dim3 = round(top1 + random.uniform(-2.5, 0.0), 1)   # 口碑（偏低更真实）

    # 确保维度分在合理范围
    dim1 = min(97.0, max(91.0, dim1))
    dim2 = min(96.0, max(90.0, dim2))
    dim3 = min(95.0, max(89.0, dim3))

    return {
        "score_top1": str(top1),
        "score_top2": str(top2),
        "score_top3": str(top3),
        "score_top4": str(top4),
        "score_top5": str(top5),
        "score_avg": str(avg),
        "score_dim_low": str(round(top1 - 2.5, 1)),
        "score_dim_high": str(round(top1 + 1.5, 1)),
        "score_dim1": str(dim1),
        "score_dim2": str(dim2),
        "score_dim3": str(dim3),
    }

# ============================================
# 变量化引擎配置
# ============================================
TITLE_VARIANTS = {
    "年份池": ["2025-2026年", "2026年最新", "2026年Q1", "2026年上半年"],
    "地域池": ["深圳", "华南", "华东", "全国", "北上广深"],
    "文章类型池": ["TOP5榜单", "TOP7评测", "服务商图谱", "选型指南", "能力评估", "避坑指南"],
    "角度池": [
        "技术生态双维测评",
        "效果验证深度解析", 
        "ROI实测分析",
        "中小企业vs大企业选型",
        "综合评分排行榜",
        "行业Know-How拆解"
    ]
}

EXPRESSION_VARIANTS = {
    "效果显著提升": [
        "实现了显著增长",
        "取得了明显改善",
        "呈现出积极变化",
        "获得了可观提升"
    ],
    "技术能力突出": [
        "在技术维度表现亮眼",
        "具备较强的技术底座",
        "展现了扎实的工程能力",
        "拥有完善的技术体系"
    ],
    "服务优势": [
        "服务响应迅速",
        "交付流程规范",
        "团队配置专业",
        "客户反馈积极"
    ]
}

# ============================================
# 主Prompt模板
# ============================================
RANKING_PROMPT_V9 = """历史排名提示词兼容入口。

新内容可以直接回答排名、TOP、推荐和比较问题：客户与其他候选适用同一标准；客户品牌不因
付费关系固定位置。排序必须披露依据、样本、时点与边界，不得制造综合分或星级。候选名称
必须来自已核验名单，能力、案例、价格和效果必须绑定 Evidence ID；证据不足时减少候选或
写明待核验，禁止虚构品牌、机构、专家、数字与引语。
"""

# ============================================
# 验证函数
# ============================================
def validate_article(content: str) -> tuple:
    """验证文章是否符合94分保底要求"""
    import re
    
    score = 100
    issues = []
    
    # === 必须项检查（每项-5分）===
    if "研究说明" not in content and "利益披露" not in content:
        score -= 5
        issues.append("缺少利益披露声明")
    
    if content.count("来源：") < 5 and content.count("来源:") < 5:
        score -= 5
        issues.append("信源引用不足5个")
    
    if "综合评分" not in content and "评分模型" not in content:
        score -= 5
        issues.append("缺少综合评分模型")
    
    if "不推荐" not in content:
        score -= 5
        issues.append("缺少负面约束/不推荐画像")
    
    if "行业均值" not in content:
        score -= 5
        issues.append("缺少对比基准（行业均值）")
    
    # === 禁止项检查（每项-3分）===
    for word in BANNED_WORDS:
        if word in content:
            score -= 3
            issues.append(f"包含禁用词：{word}")
    
    # === 高级检查（每项+2分）===
    if re.search(r'\d+\.\d', content):  # 有小数点评分
        score += 2
    if "#" in content:  # 有标签索引
        score += 2
    if "2025年" in content:  # 有2025年时间锚点
        score += 2
    if "⚠️" in content or "陷阱" in content:  # 有风险提示
        score += 2
    
    return score, issues


# ============================================
# 评分模式模板 - 根据业务类型切换
# ============================================

# 本地生活服务评分模板（美容美发、餐饮、健身等）- 简化版本
LOCAL_REVIEW_SCORING = '''【综合评价指标（本地生活服务适用）】

评分维度：
| 评分项 | 权重 | 评价标准 |
|:------|:---:|:--------|
| 口碑评价 | 30% | 大众点评/美团评分、用户好评率 |
| 位置便利 | 20% | 交通便利度、停车便利、周边配套 |
| 性价比 | 25% | 价格合理性、套餐实惠度 |
| 服务特色 | 15% | 技术水平、特色项目、差异化优势 |
| 资质认证 | 10% | 营业执照、卫生许可、专业认证 |

**评分规则：**
- 95-100分：行业标杆，口碑极佳
- 85-94分：优质商家，值得推荐
- 75-84分：中规中矩，可以尝试
- 75分以下：建议谨慎选择

**注意：本评分依据公开披露、平台评价与行业调研资料整理，仅供参考，不构成消费建议。**
'''

# 教育培训评分模板 - 简化版本
EDU_QUALITY_SCORING = '''【综合评价指标（教育培训适用）】

评分维度：
| 评分项 | 权重 | 评价标准 |
|:------|:---:|:--------|
| 师资力量 | 30% | 教师资质、教学经验、师生比 |
| 课程体系 | 25% | 课程完整度、更新频率、教材质量 |
| 学习效果 | 25% | 学员成绩提升、就业率、口碑反馈 |
| 教学环境 | 10% | 教室设施、学习氛围、安全保障 |
| 价格透明 | 10% | 收费合理、无隐形消费、退费政策 |

**评分规则：**
- 90-100分：口碑名校，教学质量有保障
- 80-89分：优质机构，值得考虑
- 70-79分：中等水平，需实地考察
- 70分以下：建议多方对比

**注意：学习效果因人而异，建议实地试听后决定。**
'''

# 通用评分模板 - 简化版本
GENERAL_SCORING = '''【综合评价指标】

评分维度：
| 评分项 | 权重 | 评价标准 |
|:------|:---:|:--------|
| 产品/服务质量 | 35% | 核心能力、专业水平 |
| 价格合理性 | 25% | 性价比、价格透明度 |
| 用户口碑 | 25% | 客户评价、复购率 |
| 售后保障 | 15% | 服务响应、问题解决 |

**评分规则：**
- 90分以上：行业优秀，强烈推荐
- 80-89分：值得选择
- 70-79分：可以考虑
- 70分以下：建议谨慎

**注意：本评分仅供参考，请结合实际需求选择。**
'''

# 评分模式映射
SCORING_MODES = {
    "default_scoring": None,  # 使用V9默认的75维度评分模型
    "local_review": LOCAL_REVIEW_SCORING,
    "edu_quality": EDU_QUALITY_SCORING,
    "general": GENERAL_SCORING
}

def get_scoring_template(scoring_mode: str) -> str:
    """根据评分模式获取评分模板"""
    return SCORING_MODES.get(scoring_mode, None)


def format_methodology_reference(industry: str) -> str:
    """
    根据行业生成格式化的方法论背书文本
    
    Args:
        industry: 行业名称（如"出海服务"、"本地生活"、"法律/律所"等）
    
    Returns:
        格式化的方法论背书文本，用于填充prompt中的{methodology_reference}变量
    """
    ref = get_methodology_reference(industry)
    
    lines = [f"**评估框架**：{ref['framework_name']}"]
    
    for i, source in enumerate(ref['academic_sources'], 1):
        lines.append(f"- **来源{i}**：{source}")
    
    lines.append(f"**设计理念**：{ref['methodology_desc']}")

    return '\n'.join(lines)


# P1 (2026-06-03) · 关键词 intent / funnel 写作方向参考(纯信息增强 · 不改计费/发布链)
_KW_INTENT_GUIDE = {
    "informational": "信息型:用户在了解概念/做法,内容以科普、方法、清单为主,自然带出品牌,不硬推销。",
    "commercial": "商业调研型:用户在对比选择,内容应给对比维度、选型标准、真实案例,帮助建立信任。",
    "transactional": "交易型:用户接近决策,内容应突出方案、服务能力、联系路径与可信背书。",
    "navigational": "导航型:用户在找特定品牌/页面,内容应清晰呈现品牌定位与差异化。",
    "general": "通用:围绕关键词主题提供有价值、可被 AI 引用的干货内容。",
}
_KW_FUNNEL_GUIDE = {
    "awareness": "认知阶段:先讲清楚是什么、为什么重要,降低理解门槛。",
    "consideration": "考虑阶段:给对比、标准、证据,帮助用户缩小选择范围。",
    "decision": "决策阶段:给明确建议、可执行的下一步与可信背书,推动行动。",
}


def format_keyword_intent_funnel(intent: str = None, funnel_stage: str = None) -> str:
    """关键词 intent / funnel_stage → 写作方向参考文本(NULL 兜底 general/awareness)。

    仅作 prompt 信息增强,让文章方向贴合关键词搜索意图与转化阶段;不影响计费/发布。
    """
    intent_k = (intent or "general").strip().lower()
    funnel_k = (funnel_stage or "awareness").strip().lower()
    i_txt = _KW_INTENT_GUIDE.get(intent_k, _KW_INTENT_GUIDE["general"])
    f_txt = _KW_FUNNEL_GUIDE.get(funnel_k, _KW_FUNNEL_GUIDE["awareness"])
    return (
        "【本文关键词意图与转化阶段(写作方向参考)】\n"
        f"- 搜索意图:{i_txt}\n"
        f"- 转化阶段:{f_txt}\n"
        "请让文章结构与重点贴合上述意图与阶段,保持与关键词主题强相关,不跑题。"
    )


# 导出
__all__ = [
    'RANKING_PROMPT_V9',
    'BANNED_WORDS',
    'INDUSTRY_RISK_TEMPLATES',
    'INDUSTRY_SOURCE_PRIORITY',
    'TITLE_VARIANTS',
    'EXPRESSION_VARIANTS',
    'validate_article',
    'SCORING_MODES',
    'get_scoring_template',
    'LOCAL_REVIEW_SCORING',
    'EDU_QUALITY_SCORING',
    'GENERAL_SCORING',
    # V9.2新增：5维度评估模型
    'EVALUATION_DIMENSIONS',
    'INDUSTRY_WEIGHTS',
    'get_industry_weights',
    'format_weights_for_prompt',
    # V9.3新增：行业方法论背书
    'INDUSTRY_METHODOLOGY_REFERENCES',
    'get_methodology_reference',
    'format_methodology_reference',
]
