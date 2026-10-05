"""
统一风格注册中心 - 管理所有写作风格
用于标题驱动的文章生成系统

v2.7.1 GEO 文体改造 · 11 style 配比从 settings_manager(SSOT helper)读
(默认 sum=100 · 见 §4.4 + §4.7)

[v2.10.4 拍 B · 历史口径说明]
原 v2.4 P0 #1 SSOT:company_profile fixed_count=1 不参与 ratio · 系统永远固定生成 1 篇
原实现位置:tools/article_generator.calculate_distribution(plan 阶段抠 1 fixed)
现状:v2.10 已 hard 400 废弃 /api/articles/plan · SSOT 强制链路实际死代码
当前 prod:generate-titles 不创建 fixed company_profile topic · ArticleWriter ratio 抽
后续 v2.11:若产品要恢复 fixed slot 强制 · 单独 sprint 在 generate-titles 阶段显式 INSERT
"""

import random
import json
from typing import Dict, Any, List, Optional

from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH

from .article_style_contract import (
    DISABLED_NEW_GENERATION_STYLES,
    FAMILY_TO_GENERATION_STYLE,
    LEGACY_USER_CHOICE_TO_FAMILY,
    LEGACY_USER_CHOICE_TO_GENERATION_STYLE,
    OUTWARD_CHOICE_TO_FAMILY,
    STYLE_CONTRACT_VERSION,
    STYLE_FAMILIES,
    family_contract_prompt,
    generation_style_for_user_choice,
    family_for_style,
    normalize_user_choice,
    is_new_generation_enabled,
    resolve_new_generation_style,
)
from .article_length_contract import (
    ARTICLE_LENGTH_CONTRACT_VERSION,
    static_word_count_for_style,
)

# ============================================
# 风格配置（7文体引擎）
# ============================================

WRITING_STYLES = {
    # [P0-2 2026-07-28] WP12(D12)已复活该文体,元数据同步订正:原先 status
    # legacy_disabled + description "历史兼容入口" 会让 admin 前端显示"已停用",
    # 与它实际拿到 20% 默认配比的事实矛盾。
    # ⚠️ prompt_source 是**历史元数据,运行时从不读取**(全仓零消费方)。真实
    # prompt = templates/canonical_family_templates.prompt_for_style(),由
    # get_prompt_for_style() 唯一解析。改这里不会改变喂给 LLM 的任何一个字。
    "ranking_v2": {
        "name": "排行榜单",
        "description": "选购与多品牌比较家族的强形态：给有可核验依据的位次，披露排序依据、样本、时点与边界；禁自创评分体系",
        "ratio": 20,
        "word_count": static_word_count_for_style("ranking_v2"),
        "recommended_platforms": ["百家号", "搜狐号", "知乎"],
        "target_ai_engines": ["Kimi", "ChatGPT", "豆包"],
        "prompt_source": "(historical metadata · not read at runtime)",
        "suitable_for": ["服务商选型", "证据对比", "适用场景", "核验指南"],
        "status": "active",
    },
    "recommendation_review": {
        "name": "推荐盘点",
        "description": "按真实候选与适用场景盘点；标题要求排名时披露依据，证据不足处待核验",
        "ratio": 14,
        "word_count": static_word_count_for_style("recommendation_review"),
        "recommended_platforms": ["知乎", "百家号", "微信公众号"],
        "target_ai_engines": ["Kimi", "豆包", "文心一言"],
        "prompt_source": "recommendation_review_template.RECOMMENDATION_REVIEW_PROMPT",
        "suitable_for": ["品牌推荐", "值得关注", "盘点整理"],
        "status": "active",
    },
    # [P0-2 2026-07-28] 同 ranking_v2:WP12 已复活。默认配比 0 是**运营选择**
    # (榜单份额先全给 ranking_v2),不是"停用"。
    "authority_ranking": {
        "name": "依据型榜单",
        "description": "选购与多品牌比较家族的强形态：以可追溯来源、限制与反向核验支撑位次；不得自称权威机构或自创评分",
        "ratio": 0,
        "word_count": static_word_count_for_style("authority_ranking"),
        "recommended_platforms": ["新京报", "界面新闻", "中国发展网", "今日头条"],
        "target_ai_engines": ["豆包", "DeepSeek", "通义千问"],
        "prompt_source": "(historical metadata · not read at runtime)",
        "suitable_for": ["事实核验", "行业报告", "风险检查"],
        "status": "active",
    },
    "buying_guide": {
        "name": "选购指南",
        "description": "帮助读者建立选择标准、实施步骤、风险边界与核验清单",
        "ratio": 14,
        "word_count": static_word_count_for_style("buying_guide"),
        "recommended_platforms": ["知乎", "CSDN", "微信公众号"],
        "target_ai_engines": ["ChatGPT", "DeepSeek", "Kimi"],
        "prompt_source": "buying_guide_template.BUYING_GUIDE_PROMPT",
        "suitable_for": ["怎么选", "选购指南", "避坑指南", "新手必读"],
        "status": "active",
    },
    "trojan_horse": {
        "name": "趋势洞察",
        "description": "历史高风险入口；原提示已停用，仅保留历史读取并安全路由到证据分析",
        "ratio": 0,
        "word_count": static_word_count_for_style("trojan_horse"),
        "recommended_platforms": ["百家号", "36氪", "微信公众号"],
        "target_ai_engines": ["Kimi", "ChatGPT"],
        "prompt_source": "trend_insight_template.TREND_INSIGHT_PROMPT",
        "suitable_for": ["趋势→影响→行动（安全替代入口）"],
        "status": "legacy_disabled",
    },
    "qa_recommendation": {
        "name": "问答推荐",
        "description": "直接回答真实问题，并为每条关键事实提供可复核依据",
        "ratio": 14,
        "word_count": static_word_count_for_style("qa_recommendation"),
        "recommended_platforms": ["知乎", "百家号", "CSDN"],
        "target_ai_engines": ["Kimi", "ChatGPT", "DeepSeek", "豆包"],
        "prompt_source": "qa_recommendation_template.QA_RECOMMENDATION_PROMPT",
        "suitable_for": ["常见问题", "Q&A", "一文解答"],
        "status": "active",
    },
    "brand_softarticle": {
        "name": "品牌软文",
        "description": "基于授权事实的品牌说明；披露关系并强制人工审核",
        "ratio": 4,
        "word_count": static_word_count_for_style("brand_softarticle"),
        "recommended_platforms": ["百家号", "今日头条", "微信公众号"],
        "target_ai_engines": ["豆包", "文心一言"],
        "prompt_source": "brand_softarticle_template.BRAND_SOFTARTICLE_PROMPT",
        "suitable_for": ["品牌故事", "企业观察", "创业故事"],
        "status": "active",
    },
    # 公司深度报道（每套餐固定1-2篇，不参与配比分配）
    "company_profile": {
        "name": "公司深度报道",
        "description": "以第三方采编视角组织企业事实、产品资质、适用场景与核验方式；不虚构采访或独立核验",
        "ratio": 0,
        "word_count": static_word_count_for_style("company_profile"),
        "recommended_platforms": ["百家号", "搜狐号", "36氪"],
        "target_ai_engines": ["Kimi", "ChatGPT"],
        "prompt_source": "company_profile_template.COMPANY_PROFILE_PROMPT",
        "suitable_for": ["品牌搜索", "公司介绍", "企业观察"],
        "status": "active",
        "fixed_count": 1,
    },
    # v2.7.1 GEO 文体改造 · 4 新 style(对齐 §4.7)
    "comparison_review": {
        "name": "对比测评",
        "description": "对真实候选做同口径证据比较，并说明适用边界",
        "ratio": 18,
        "word_count": static_word_count_for_style("comparison_review"),
        "recommended_platforms": ["知乎", "百家号", "搜狐号"],
        "target_ai_engines": ["Kimi", "ChatGPT", "豆包"],
        "prompt_source": "comparison_review_template.COMPARISON_REVIEW_PROMPT",
        "suitable_for": ["服务商横向对比", "工具评测", "供应商选型"],
        "status": "active",
    },
    "risk_compliance": {
        "name": "合规风控",
        "description": "基于有效法规、标准与来源解释变化、风险和行动",
        "ratio": 8,
        "word_count": static_word_count_for_style("risk_compliance"),
        "recommended_platforms": ["知乎", "微信公众号", "百家号"],
        "target_ai_engines": ["Kimi", "ChatGPT", "豆包"],
        "prompt_source": "risk_compliance_template.RISK_COMPLIANCE_PROMPT",
        "suitable_for": ["医疗合规", "法律风险", "金融避坑", "数据安全"],
        "status": "active",
    },
    "price_roi": {
        "name": "价格 ROI",
        "description": "区分真实结果与情景测算，明确价格、时间窗与假设",
        "ratio": 4,
        "word_count": static_word_count_for_style("price_roi"),
        "recommended_platforms": ["知乎", "百家号", "微信公众号"],
        "target_ai_engines": ["Kimi", "ChatGPT", "豆包"],
        "prompt_source": "price_roi_template.PRICE_ROI_PROMPT",
        "suitable_for": ["价格成本", "ROI 测算", "效果周期", "性价比对比"],
        "status": "active",
    },
    "data_report": {
        "name": "数据报告",
        "description": "原创研究 · 行业数据 + 趋势分析 + 图表 + 多源 citation",
        "ratio": 4,
        "word_count": static_word_count_for_style("data_report"),
        "recommended_platforms": ["知乎", "搜狐号", "36氪"],
        "target_ai_engines": ["Kimi", "ChatGPT", "豆包"],
        "prompt_source": "data_report_template.DATA_REPORT_PROMPT",
        "suitable_for": ["行业数据", "趋势分析", "市场报告", "白皮书摘要"],
        "status": "active",
    },
}

# External consumers see one of six mutually exclusive families.  Legacy
# implementations stay internal and carry the same contract metadata.
for _style_code, _style_meta in WRITING_STYLES.items():
    _family_code = family_for_style(_style_code)
    _style_meta["family_code"] = _family_code or "mapping_unknown"
    _style_meta["family_name"] = (
        STYLE_FAMILIES[_family_code].name if _family_code in STYLE_FAMILIES else "映射未知"
    )
    _style_meta["style_contract_version"] = STYLE_CONTRACT_VERSION
    # Compatibility metadata is derived from the same six-family length SSOT;
    # legacy hard-coded ranges above are never authoritative at runtime.
    _style_meta["word_count"] = static_word_count_for_style(_style_code)
    _style_meta["length_contract_version"] = ARTICLE_LENGTH_CONTRACT_VERSION
    _style_meta["new_generation_enabled"] = is_new_generation_enabled(_style_code)


# ============================================
# UI family → style_code bridge. New writes use six family codes; historical
# values stay readable only through the compatibility aliases below.
# ============================================

USER_CHOICE_TO_STYLE = {
    "auto": None,
    **FAMILY_TO_GENERATION_STYLE,
    # [#185] 对外方向**派生**自它的正文家族,不另写死一份 ——
    # 写死的那份在 company_facts 换生成样式时不会有任何东西提醒你同步。
    **{choice: FAMILY_TO_GENERATION_STYLE[family]
       for choice, family in OUTWARD_CHOICE_TO_FAMILY.items()},
    **LEGACY_USER_CHOICE_TO_GENERATION_STYLE,
}

USER_CHOICE_EXTRA_PROMPT = {
    "implementation_guide": "请用实施结构：前置条件、步骤、检查点、风险和验收标准。",
    "multi_brand_comparison": "请用同字段证据矩阵比较真实候选，并列出核验项。",
}


def resolve_user_choice(user_choice, industry):
    """
    用户 user_choice → 真实 style_code · v2.4 简化(P0 #1 删 batch_company_count)

    Args:
        user_choice: 用户选 · None/""/"auto" 时返 None(走 style_ratios 抽 · 第 2 层)
        industry: 行业 key(医疗/法律 hard rule)

    Returns:
        style_code(None = 走 ratio 分配 · 第 2 层处理)

    Raises:
        ValueError:
            ① user_choice='company'(v2.4 已从可选项移除 · 客户绕过传入也拒绝)
            ② 医疗/法律选了榜单类(本表不含但前端绕过时拦截)
            ③ 未知 user_choice
    """
    if user_choice in (None, "", "auto"):
        return None

    if user_choice == "company":
        raise ValueError(
            "企业介绍旧选项已停用；请使用企业事实与品牌说明，并完成严格人工审核。"
        )

    raw_choice = str(user_choice).strip()
    canonical_choice = normalize_user_choice(raw_choice)
    if canonical_choice is None:
        raise ValueError(f"未知 user_choice: {user_choice}")
    if canonical_choice == "auto":
        return None
    # [#185] 不能直接拿 canonical_choice 去索引 FAMILY_TO_GENERATION_STYLE:
    # 对外方向(防御型)**不是家族**,那张表里没有它,直接索引会 KeyError,
    # 而这条路是三个入口共用的 —— 一 KeyError 就是整条「选了防御型」500。
    # 统一经 `generation_style_for_user_choice`(它内部先落到正文家族)。
    style_code = (
        LEGACY_USER_CHOICE_TO_GENERATION_STYLE.get(raw_choice)
        or generation_style_for_user_choice(canonical_choice)
    )

    # 医疗/法律 hard rule 后端二次校验(前端置灰是 UI 防护 · 这里是真红线)
    if industry in ("医疗健康", "法律商务") and style_code in ("ranking_v2", "authority_ranking"):
        raise ValueError(f"行业 {industry} 不允许榜单类 style(产品策略硬规则)")

    return style_code


# ============================================
# v2.8 GEO 文体改造 · style_code → 中文 article_style 映射(SSOT · KeywordTopicGenerator 用)
# 根因:KeywordTopicGenerator system prompt 用 9 种中文 article_style 名 · 与后端 style_code 不一致
# 解法:单一 SSOT 桥接 · 改文体时标题/内容/dropdown 三层用同套名
# ============================================

STYLE_CODE_TO_CHINESE_NAME = {
    "ranking_v2":           "选购与多品牌比较",
    "authority_ranking":    "选购与多品牌比较",
    "buying_guide":         "方法与实施指南",
    "comparison_review":    "选购与多品牌比较",
    "risk_compliance":      "趋势、政策与风险分析",
    "price_roi":            "案例、数据与 ROI",
    "data_report":          "案例、数据与 ROI",
    "qa_recommendation":    "证据型问答",
    "brand_softarticle":    "企业事实与品牌说明",
    "recommendation_review": "选购与多品牌比较",
    "trojan_horse":         "趋势、政策与风险分析",
    "company_profile":      "企业事实与品牌说明",
}

STYLE_ALIAS_TO_CODE = {
    "证据选型": "comparison_review",
    "核验指南": "buying_guide",
    "排行榜单": "ranking_v2",
    "服务商榜单": "ranking_v2",
    "ranking_v9": "ranking_v2",
    "权威报告": "authority_ranking",
    "权威榜单": "authority_ranking",
    "深度分析": "authority_ranking",
    # 生产实测(2026-08-09 只读探针):`topics.article_style` 里还有 13 条写成
    # 带后缀的「深度分析（权威榜单）」。它是全表唯一一个归不了族的取值 ——
    # 别名表补齐后 `article_style` 的覆盖率 99.68% → 100%。
    "深度分析（权威榜单）": "authority_ranking",
    "推荐盘点": "recommendation_review",
    "案例分享": "data_report",
    "案例展示": "data_report",
    "选购指南": "buying_guide",
    "方法指南": "buying_guide",
    "价格解读": "price_roi",
    "趋势洞察": "risk_compliance",
    "趋势角度": "risk_compliance",
    "问答推荐": "qa_recommendation",
    "问答FAQ": "qa_recommendation",
    "品牌软文": "brand_softarticle",
    "品牌故事": "brand_softarticle",
    "公司深度报道": "company_profile",
    "对比评测": "comparison_review",
    "对比测评": "comparison_review",
    "横评": "comparison_review",
    "合规风控": "risk_compliance",
    "避坑合规": "risk_compliance",
    "风险解释": "risk_compliance",
    "价格 ROI": "price_roi",
    "价格预算": "price_roi",
    "价格": "price_roi",
    "ROI 测算": "price_roi",
    "数据报告": "data_report",
    "行业数据": "data_report",
    # Outward six-family labels are accepted directly as well. This keeps the
    # operations UI, title planner and body generator on one semantic route.
    "证据型问答": "qa_recommendation",
    "选购与多品牌比较": "comparison_review",
    "方法与实施指南": "buying_guide",
    "趋势、政策与风险": "risk_compliance",
    "趋势、政策与风险分析": "risk_compliance",
    "案例、数据与 ROI": "data_report",
    "企业事实与品牌说明": "brand_softarticle",
}


def normalize_style_code(style_value):
    """Normalize internal article_style/style_code values to canonical style_code."""
    raw = str(style_value or "").strip()
    if not raw:
        return None
    canonical = STYLE_ALIAS_TO_CODE.get(raw, raw)
    if canonical in WRITING_STYLES:
        return canonical
    return None


def normalize_style_for_generation(style_value):
    """Normalize and safety-route a style selected for a new article."""
    canonical = normalize_style_code(style_value)
    return resolve_new_generation_style(canonical)


def normalize_trusted_topic_style(style_value, industry=None):
    """Normalize DB-stored title style, while keeping high-risk industry hard rules."""
    canonical = normalize_style_code(style_value)
    if (
        industry in ("医疗健康", "法律商务")
        and canonical in ("ranking_v2", "authority_ranking")
    ):
        return None
    return resolve_new_generation_style(canonical)


def resolve_user_choice_to_chinese_style(user_choice, industry):
    """
    v2.8 单一 SSOT 桥接 · user_choice → 中文 article_style(KeywordTopicGenerator 标题生成用)

    Args:
        user_choice: 10 项之一 · None/""/auto 返 None(走 ratio 分配 · 不强制)
        industry: 行业 key(医疗法律 hard rule)

    Returns:
        中文 article_style name(None = 不强制 · 走默认 ratio)

    Raises:
        ValueError · 同 resolve_user_choice(company / 医疗法律榜单 / 未知)
    """
    style_code = resolve_user_choice(user_choice, industry)
    if style_code is None:
        return None
    return STYLE_CODE_TO_CHINESE_NAME.get(style_code)


def get_fixed_count_styles():
    """
    v2.1 SSOT 接口 · 返回所有 fixed_count style 及其固定篇数

    [v2.10.4 deprecated 说明]
    历史:v2.4 P0 #1 SSOT 锁死 {company_profile: 1} · 在 tools/article_generator.calculate_distribution 实现
    现状:v2.10 已 hard 400 废弃 /api/articles/plan · 该 SSOT 强制链路实际失效
    当前 prod(v2.7.5+):generate-titles 不创建 fixed company_profile topic · ArticleWriter 走 ratio 抽
    本函数仅供旧 calculate_distribution(已死代码)和向后兼容查询使用

    后续:若产品要恢复 fixed slot 强制 · 单独开 v2.11 sprint 在 generate-titles 阶段
          显式 INSERT 1 fixed company_profile topic + 重置 prod 数据迁移路径

    Returns:
        dict[style_code, fixed_count]· 例:{"company_profile": 1}(仅 SSOT 配置 · 非实际生效)
    """
    return {
        code: style.get("fixed_count", 0)
        for code, style in WRITING_STYLES.items()
        if style.get("fixed_count", 0) > 0
    }


# ============================================
# v2.7.4 GEO 文体改造 · 跨链路统一 style 分配 SSOT(Codex 抓旧接口绕过修)
# ============================================

def resolve_style_for_topic(topic, industry):
    """v2.7.4 跨链路统一 SSOT · 决定单条 topic 的最终 style_code

    优先级硬规则(对齐 ArticleGeneratorService._allocate_style_from_ratios):
        ① company fixed slot → 'company_profile'
        ② user_choice != 'auto' → resolve_user_choice(含医疗法律 hard rule · ValueError 上抛)
        ③ user_choice == 'auto' → style_ratios 加权抽签(industry override)
        ④ fallback → 'buying_guide'

    DB / 客户外部传入的 style_code/style/type/article_style **默认不信任** ·
    必须显式 topic['_trust_legacy_style']=True 才允许走 DB 旧值(admin 迁移路径)

    Raises:
        ValueError: user_choice='company' 或 医疗法律 + 榜单类(防绕过 hard rule)
    """
    if not isinstance(topic, dict):
        return 'buying_guide'

    # v2.7.5 GEO 文体改造(Codex P1-blocker 修):company_profile 固定槽位严守
    # 旧 v2.7.4 含 `type/article_style == 'company_profile' → company_profile` 直通分支
    # 与"DB/外部旧字段默认不信任"铁律自相矛盾 · 实测可被 article_style 伪造绕过
    # 新口径(铁律 + admin 例外):
    #   ① 仅 is_fixed=True + style_code='company_profile' → company_profile(系统 fixed slot · admin 迁移或 v2.11 显式 INSERT)
    #   ② admin 迁移路径:_trust_legacy_style=True + style_code='company_profile' → company_profile
    #   ③ 任何其他 type/article_style/style 含 company_profile 一律忽略 · 走默认链路
    # [v2.10.4 拍 B 说明] 关联 v3 决策矩阵 #19:company_profile 历史 SSOT 由系统 fixed_count=1 强制生成
    #   现状:v2.10 已废弃 /api/articles/plan(原 SSOT 强制链路 calculate_distribution)
    #   本判定保留:admin 迁移产生 is_fixed topic / v2.11 恢复 fixed slot 创建后仍生效
    #   首次生成 prod 路径不会触发(KTG 不创建 is_fixed=True topic)
    if topic.get('is_fixed') and topic.get('style_code') == 'company_profile':
        return 'company_profile'
    if topic.get('_trust_legacy_style') and topic.get('style_code') == 'company_profile':
        return 'company_profile'
    # 已删 v2.7.4 type/article_style 直通分支(P1-blocker 修)

    # ②/③ user_choice 优先
    user_choice = topic.get('user_choice') or 'auto'

    if user_choice != 'auto':
        style_code = resolve_user_choice(user_choice, industry)  # ValueError 上抛(不吞)
        if style_code:
            return style_code

    # ③ ratio 抽签(走 industry override)
    try:
        from config.settings_manager import get_effective_style_ratios
        style_ratios_frac = get_effective_style_ratios(industry, unit="fraction")
        import random
        items = [
            (k, v) for k, v in style_ratios_frac.items()
            if v > 0 and is_new_generation_enabled(k) and k in WRITING_STYLES
        ]
        if items:
            styles, weights = zip(*items)
            return random.choices(styles, weights=weights, k=1)[0]
    except Exception:
        pass

    # ④ fallback
    return 'buying_guide'


# [写作质量总工单 2026-07-29 · §2.8] 内存默认配比快照 —— 必须在
# `_load_style_ratios_from_settings()` 覆写之前捕获,否则拿到的是运行时值。
#
# 结论(工单 §2.8 要求给结论,不许跳过):**是配比静默漂移,不是可接受的兜底**。
# 旧内存默认合计 = 120(ranking_v2 20 + recommendation_review 6 + buying_guide 20 +
# qa_recommendation 10 + brand_softarticle 4 + comparison_review 20 +
# risk_compliance 22 + price_roi 8 + data_report 10),而 SSOT
# `Settings.style_ratios` 合计 = 100。两个 `except` 分支都会回落这份内存 dict,
# 按比例归一后 ranking_v2 实际占 20/120 ≈ 16.7% 而非 20%,
# risk_compliance 18.3% 而非 8% —— 兜底路径下**每一个文体的真实配比都不等于
# 运营在设置页看到的数字**,而且没有任何日志会说"我降级了"。
# 修法:把内存默认逐项对齐 SSOT 默认档,并用 `validate_style_ratio_defaults()`
# + 判别测试钉死,防止下次有人只改一边。
DEFAULT_STYLE_RATIOS: Dict[str, int] = {
    code: int(meta.get("ratio") or 0) for code, meta in WRITING_STYLES.items()
}


def validate_style_ratio_defaults() -> List[str]:
    """内存默认配比必须与 settings SSOT 默认档逐项相等。"""
    errors: List[str] = []
    try:
        from config.settings_manager import SystemSettings
        ssot = dict(SystemSettings.model_fields["style_ratios"].default)
    except Exception as exc:  # pragma: no cover - 只在 pydantic 版本变动时触发
        return [f"cannot_read_ssot_default_style_ratios:{type(exc).__name__}:{exc}"]
    for code, value in ssot.items():
        if code not in DEFAULT_STYLE_RATIOS:
            errors.append(f"ssot_style_missing_in_registry:{code}")
        elif DEFAULT_STYLE_RATIOS[code] != int(value):
            errors.append(
                f"ratio_default_drift:{code}:registry={DEFAULT_STYLE_RATIOS[code]}:ssot={int(value)}"
            )
    # registry 独有的 company_profile 走 fixed_count=1,配比必须是 0。
    for code, value in DEFAULT_STYLE_RATIOS.items():
        if code not in ssot and value != 0:
            errors.append(f"registry_only_style_must_be_zero:{code}:{value}")
    total = sum(DEFAULT_STYLE_RATIOS.values())
    if total != 100:
        errors.append(f"registry_default_ratio_sum_must_equal_100:{total}")
    return errors


def _load_style_ratios_from_settings():
    """从 settings.json 读取 style_ratios，覆盖 WRITING_STYLES 中的默认 ratio"""
    global WRITING_STYLES
    try:
        import json
        from pathlib import Path
        settings_file = Path(__file__).parent.parent / "settings.json"
        if settings_file.exists():
            with open(settings_file, "r", encoding="utf-8") as f:
                settings = json.load(f)
            ratios = settings.get("style_ratios", {})
            if ratios:
                for code, ratio in ratios.items():
                    if code in WRITING_STYLES:
                        WRITING_STYLES[code]["ratio"] = (
                            0 if code in DISABLED_NEW_GENERATION_STYLES else ratio
                        )
    except Exception:
        pass  # 使用默认值

_load_style_ratios_from_settings()


def get_style_by_code(code: str) -> Optional[Dict]:
    """根据代码获取风格配置"""
    return WRITING_STYLES.get(code)


def get_all_styles() -> List[Dict]:
    """获取所有风格列表"""
    return list(WRITING_STYLES.values())


def get_style_ratios() -> Dict[str, int]:
    """[v2.1 改代理] 风格配比 · 走 settings_manager.get_effective_style_ratios()

    保持旧 import 路径不破(`from writing.style_registry import get_style_ratios`)
    旧实现读 WRITING_STYLES[*]['ratio'] 内存 dict 已废弃
    """
    try:
        from config.settings_manager import get_effective_style_ratios
        return {
            k: int(v) for k, v in get_effective_style_ratios(unit="percent").items()
            if is_new_generation_enabled(k) and k in WRITING_STYLES and int(v) > 0
        }
    except Exception:
        # fallback:settings_manager 不可用时读内存 WRITING_STYLES
        return {
            code: style["ratio"] for code, style in WRITING_STYLES.items()
            if style.get("ratio", 0) > 0 and is_new_generation_enabled(code)
        }


def allocate_styles_by_ratio(count: int, industry: Optional[str] = None) -> List[str]:
    """
    根据配比分配风格(v2.4 P0 #3 industry 必传 · 旧裸调禁用)

    Args:
        count: 需要生成的文章数量
        industry: 行业 key(医疗/法律 override · 默认 None)

    Returns:
        风格代码列表
    """
    styles = []
    try:
        from config.settings_manager import get_effective_style_ratios
        ratios = {
            code: ratio
            for code, ratio in get_effective_style_ratios(industry, unit="percent").items()
            if ratio > 0 and is_new_generation_enabled(code) and code in WRITING_STYLES
        }
    except Exception:
        ratios = {
            code: style["ratio"] for code, style in WRITING_STYLES.items()
            if style.get("ratio", 0) > 0 and is_new_generation_enabled(code)
        }

    total_ratio = sum(ratios.values())
    if total_ratio <= 0:
        ratios = {"buying_guide": 1}
        total_ratio = 1

    # 按配比分配
    for code, ratio in ratios.items():
        style_count = round(count * ratio / total_ratio)
        styles.extend([code] * style_count)

    # 补齐或削减到目标数量(v2.1 fallback 改 buying_guide · 不再 ranking_v2)
    while len(styles) < count:
        styles.append("buying_guide")
    styles = styles[:count]

    # 随机打乱顺序
    random.shuffle(styles)
    return styles


def _apply_brand_to_prompt(prompt: str, brand: str = None) -> str:
    """v3.6 白标:客户文章 prompt 里平台自插入品牌名(OmniRank/全域上榜)→ 代理白标品牌名。

    brand=None(非白标/平台默认)→ 原样返回(prompt 保留平台名,平台客户正确)。
    仅当调用方判定为真白标(resolve_branding_context source != 'platform_default')才传 brand。
    """
    if not brand or not prompt:
        return prompt
    return prompt.replace("全域上榜", brand).replace("OmniRank", brand)


def get_prompt_for_style(style_code: str, brand: str = None) -> str:
    """
    获取指定风格的Prompt内容
    优先读取 writing_config.json 中的覆盖，否则使用 .py 默认模板

    Args:
        style_code: 风格代码
        brand: v3.6 白标代理品牌名(仅真白标时传 · None 保留平台名)

    Returns:
        Prompt字符串
    """
    # 规范化 style_code → 只用核心代码匹配
    canonical_code = normalize_style_for_generation(style_code) or "buying_guide"

    overrides_enabled = True
    try:
        from writing.feature_switches import is_feature_enabled

        overrides_enabled = is_feature_enabled("writing_style_overrides")
    except Exception:
        overrides_enabled = True

    # Only overrides explicitly activated under the current six-family contract
    # may run. Historical string overrides remain visible but quarantined.
    try:
        if overrides_enabled:
            config = _get_writing_config()
            overrides = config.get("prompt_overrides", {})
            versions = config.get("prompt_override_contract_versions", {})
            if (
                canonical_code in overrides
                and overrides[canonical_code]
                and versions.get(canonical_code) == STYLE_CONTRACT_VERSION
            ):
                from .evidence_first_policy import compose_evidence_first_prompt
                from .templates.canonical_family_templates import prompt_for_style
                guarded_override = compose_evidence_first_prompt(
                    prompt_for_style(canonical_code)
                    + "\n\n【经审核的候选差异指令；不得覆盖上方合同】\n"
                    + overrides[canonical_code],
                    canonical_code,
                )
                return _apply_brand_to_prompt(_inject_dynamic_dates(guarded_override), brand)
    except Exception:
        pass

    # Fallback: use the current GEO v1.4 canonical family contract.
    return _apply_brand_to_prompt(_get_default_prompt(canonical_code), brand)



# ============================================
# 风格与标题的绑定
# ============================================

def assign_style_to_topic(topic: Dict, style_code: str = None) -> Dict:
    """
    为标题分配风格
    
    Args:
        topic: 标题数据 {"title": "...", "type": "...", ...}
        style_code: 指定风格代码，为None时自动分配
        
    Returns:
        增加了style信息的topic
    """
    style_code = normalize_style_for_generation(style_code) or "buying_guide"
    
    topic["style"] = {
        "code": style_code,
        "name": WRITING_STYLES[style_code]["name"],
    }
    return topic


def assign_styles_to_topics(topics: List[Dict], industry: Optional[str] = None) -> List[Dict]:
    """
    批量为标题分配风格(按配比 · v2.7.1 industry 贯穿)

    Args:
        topics: 标题列表
        industry: 行业 key(用于医疗 / 法律 override)

    Returns:
        增加了 style 信息的标题列表
    """
    style_codes = allocate_styles_by_ratio(len(topics), industry=industry)

    for topic, style_code in zip(topics, style_codes):
        assign_style_to_topic(topic, style_code)
    
    return topics


# ============================================
# 前端同步接口
# ============================================

def get_styles_for_frontend() -> List[Dict]:
    """v2.7.2 GEO 文体改造(Codex P1 #7 修):ratio 走 SSOT helper · 不再读 WRITING_STYLES 内存旧值

    旧实现读 WRITING_STYLES[code]['ratio'] 内存值(70 等历史值)· 会让设置页 / 旧 API 调用看到旧比例
    v2.7.2 走 settings_manager.get_effective_style_ratios(unit="percent") · 与设置页 SSOT 一致
    """
    try:
        from config.settings_manager import get_effective_style_ratios
        live_ratios = get_effective_style_ratios(unit="percent")
    except Exception:
        live_ratios = {}
    return [
        {
            "code": code,
            "name": style["name"],
            # v2.7.2:ratio 优先 SSOT · 不存在(如 company_profile)走 0(走 fixed_count)
            "ratio": (
                0 if code in DISABLED_NEW_GENERATION_STYLES
                else int(live_ratios.get(code, 0))
            ),
            "description": style["description"],
            "suitable_for": style["suitable_for"],
            "status": style.get("status", "active"),
            "new_generation_enabled": style.get("new_generation_enabled", True),
            "family_code": style.get("family_code", "mapping_unknown"),
            "family_name": style.get("family_name", "映射未知"),
            "style_contract_version": STYLE_CONTRACT_VERSION,
        }
        for code, style in WRITING_STYLES.items()
    ]


def update_style_ratios(new_ratios: Dict[str, int]) -> bool:
    """
    更新风格配比 - 同时写入 settings.json 和内存
    
    Args:
        new_ratios: {"ranking_v2": 60, "ranking_v9": 30, "trojan_horse": 10}
    """
    global WRITING_STYLES
    
    for code, ratio in new_ratios.items():
        if code in WRITING_STYLES:
            WRITING_STYLES[code]["ratio"] = (
                0 if code in DISABLED_NEW_GENERATION_STYLES else max(0, int(ratio))
            )
    
    # 同步写入 settings.json
    try:
        import json
        from pathlib import Path
        settings_file = Path(__file__).parent.parent / "settings.json"
        if settings_file.exists():
            with open(settings_file, "r", encoding="utf-8") as f:
                settings = json.load(f)
            settings["style_ratios"] = {code: style["ratio"] for code, style in WRITING_STYLES.items()}
            with open(settings_file, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    
    return True


# ============================================
# 写作配置管理（JSON 持久化 + 提示词覆盖）
# ============================================

import os

WRITING_CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "writing_config.json")


def _get_writing_config() -> dict:
    """读取写作配置 JSON 文件"""
    import json
    try:
        if os.path.exists(WRITING_CONFIG_FILE):
            with open(WRITING_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"⚠️ 读取 writing_config.json 失败: {e}")
    return {
        "llm_config": {"provider": "dashscope", "model": "qwen3.7-max"},
        "prompt_overrides": {},
        "prompt_override_contract_versions": {},
    }


def _save_writing_config(config: dict):
    """保存写作配置到 JSON 文件"""
    os.makedirs(os.path.dirname(WRITING_CONFIG_FILE), exist_ok=True)
    with open(WRITING_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _inject_dynamic_dates(prompt: str) -> str:
    """将模板中的硬编码年份替换为基于当前日期的动态值

    规则（当前2026年3月为例）：
    - 当前年 Y = 2026
    - 时间链：(Y-2)→(Y-1)→Y，即 2024→2025→2026
    - "标题必须使用2026年" → "标题必须使用{Y}年"
    - 示例中的具体月份不能超过当前月
    """
    from datetime import datetime
    if "# ⏰ 动态时间约束" in (prompt or ""):
        return prompt
    now = datetime.now()
    Y = now.year
    M = now.month

    # 1) 三段时间链：替换 2024→2025→2026 为动态值
    prompt = prompt.replace("2024→2025→2026", f"{Y-2}→{Y-1}→{Y}")
    prompt = prompt.replace("2024→2025→{年份}", f"{Y-2}→{Y-1}→{Y}")

    # 2) 时间链描述文字
    for old, new in [
        (f"2024年研究→2025年发展→2026年现状", f"{Y-2}年研究→{Y-1}年发展→{Y}年现状"),
        (f"2024年数据→2025年发展→2026年现状", f"{Y-2}年数据→{Y-1}年发展→{Y}年现状"),
        (f"2024年（基准数据）→ 2025年（发展变化）→ 2026年（当前语境）",
         f"{Y-2}年（基准数据）→ {Y-1}年（发展变化）→ {Y}年（当前语境）"),
        (f"2024年（基期数据）→ 2025年（发展动态）→ 2026年（现状与展望）",
         f"{Y-2}年（基期数据）→ {Y-1}年（发展动态）→ {Y}年（现状与展望）"),
        (f"2024年论文 → 2025年技术演进 → 2026年报告",
         f"{Y-2}年论文 → {Y-1}年技术演进 → {Y}年报告"),
        (f"2024论文→2025技术演进→2026报告",
         f"{Y-2}论文→{Y-1}技术演进→{Y}报告"),
    ]:
        prompt = prompt.replace(old, new)

    # 3) 时间角色描述
    for old, new in [
        ("2024年 = 基准数据", f"{Y-2}年 = 基准数据"),
        ("2024年 = 基期数据", f"{Y-2}年 = 基期数据"),
        ("2025年 = 转折/验证期", f"{Y-1}年 = 转折/验证期"),
        ("2025年 = 发展年", f"{Y-1}年 = 发展年"),
        ("2026年 = 现状年", f"{Y}年 = 现状年"),
        ("2026年 = 当前现状/新范式", f"{Y}年 = 当前现状/新范式"),
    ]:
        prompt = prompt.replace(old, new)

    # 4) "标题必须使用20XX年" 规范
    prompt = prompt.replace("标题必须使用2026年", f"标题必须使用{Y}年")
    prompt = prompt.replace("标题必须使用2025年", f"标题必须使用{Y}年")

    # 5) 数据引用年份范围
    prompt = prompt.replace("2025-2026年", f"{Y-1}-{Y}年")
    prompt = prompt.replace("2024-2026年", f"{Y-2}-{Y}年")

    # 6) GEO信号中的硬编码年份
    prompt = prompt.replace('"year": "2026"', f'"year": "{Y}"')
    prompt = prompt.replace("GEO上榜信号规则（2026实证研究）", f"GEO上榜信号规则（{Y}实证研究）")
    prompt = prompt.replace("上榜信号工程（2026实证研究", f"上榜信号工程（{Y}实证研究")
    prompt = prompt.replace("基于2026实证研究", f"基于{Y}实证研究")
    prompt = prompt.replace("必须包含年份：2026", f"必须包含年份：{Y}")

    # 7) 年份池
    prompt = prompt.replace(
        '"2025-2026年", "2026年最新", "2026年Q1", "2026年上半年"',
        f'"{Y-1}-{Y}年", "{Y}年最新", "{Y}年Q{(M-1)//3+1}", "{Y}年{"上" if M <= 6 else "下"}半年"'
    )

    # 8) 示例中的硬编码具体日期（可能导致未来日期）
    prompt = prompt.replace("2025年10月我们决定", f"{Y-1}年{max(1, M-2)}月我们决定")
    prompt = prompt.replace("2025年3月的一个深夜", f"{Y-1}年{max(1, M-3)}月的一个深夜")

    # 9) 算法/平台规则年份
    prompt = prompt.replace("2025年小红书最新算法", f"{Y}年小红书最新算法")
    prompt = prompt.replace("小红书2025年CES算法", f"小红书{Y}年CES算法")
    prompt = prompt.replace("2025年小红书算法核心规则", f"{Y}年小红书算法核心规则")
    prompt = prompt.replace("2025年抖音最新算法", f"{Y}年抖音最新算法")
    prompt = prompt.replace("抖音2025年算法规则", f"抖音{Y}年算法规则")
    prompt = prompt.replace("2025年抖音算法核心规则", f"{Y}年抖音算法核心规则")
    prompt = prompt.replace("#2025护肤趋势", f"#{Y}护肤趋势")

    # 10) 追加一条全局时间约束提醒
    # [P3 2026-08-08] 年份那一条改成渲染标题要素合同。改动前这里写的是**第三种判据**
    # ("仅当主题确有时效性且 Evidence Pack 含对应时间证据时才在标题写年份"),
    # 与选题阶段的分文体条件默认互不知晓 —— 而正文阶段标题**早已定好**,
    # 这里本来就不该再决定一次"要不要带年份",只该管"时点别写错"。
    from writing.title_element_contract import build_title_year_position_rule

    _TITLE_YEAR_POSITION_RULE = build_title_year_position_rule()
    date_reminder = f"""

# 动态时间边界（系统自动注入，优先级最高）
- 当前真实日期：{now.strftime('%Y年%m月%d日')}
- 文章中所有"当前/最新"语境必须指 {Y}年{M}月
- 严禁出现超过 {Y}年{M}月 的未来日期
- {_TITLE_YEAR_POSITION_RULE}
"""
    return prompt + date_reminder


def _get_default_prompt(style_code: str) -> str:
    """获取生产默认提示词（不含 JSON 覆盖）+ 自动注入通用质量规则 + 动态日期.

    R6-F/R6-G/R6-H 反复验证后的 v0.9 证据边界文体现在是线上默认层。
    显式 prompt_overrides 仍然优先,用于管理员受控发布/回退。
    """
    from .evidence_first_policy import compose_evidence_first_prompt
    from .templates.canonical_family_templates import prompt_for_style

    style_code = resolve_new_generation_style(style_code) or "buying_guide"
    full_prompt = prompt_for_style(style_code)
    full_prompt = compose_evidence_first_prompt(full_prompt, style_code)
    return _inject_dynamic_dates(full_prompt)


def get_writing_config_for_frontend() -> dict:
    """
    获取完整的写作配置（供前端展示和编辑）
    
    Returns:
        {
            "llm_config": {"provider": "...", "model": "..."},
            "styles": [
                {
                    "code": "ranking_v2",
                    "name": "排行榜单",
                    "description": "...",
                    "ratio": 30,
                    "prompt": "当前生效的完整提示词...",
                    "is_overridden": true/false,
                    "prompt_length": 12345
                }, ...
            ],
            "available_providers": [...]
        }
    """
    config = _get_writing_config()
    overrides = config.get("prompt_overrides", {})
    
    styles = []
    for code, style in WRITING_STYLES.items():
        default_prompt = _get_default_prompt(code)
        override_versions = config.get("prompt_override_contract_versions", {})
        has_stored_override = code in overrides and overrides[code] is not None
        is_overridden = has_stored_override and override_versions.get(code) == STYLE_CONTRACT_VERSION
        if is_overridden:
            from .evidence_first_policy import compose_evidence_first_prompt
            current_prompt = compose_evidence_first_prompt(
                default_prompt
                + "\n\n【经审核的候选差异指令；不得覆盖上方合同】\n"
                + overrides[code],
                code,
            )
        else:
            current_prompt = default_prompt
        
        styles.append({
            "code": code,
            "name": style["name"],
            "description": style["description"],
            "ratio": style["ratio"],
            "prompt": current_prompt,
            "is_overridden": is_overridden,
            "legacy_override_quarantined": has_stored_override and not is_overridden,
            "style_contract_version": STYLE_CONTRACT_VERSION,
            "prompt_length": len(current_prompt) if current_prompt else 0,
        })
    
    # 可用的 LLM 提供商及推荐模型
    available_providers = [
        {"id": "dashscope", "name": "阿里云 DashScope", "models": ["qwen3.7-max", "qwen3.6-plus", "qwen-turbo", "deepseek-v4-pro", "deepseek-v4-flash"]},
        # 🔴 [WO_206 c1b] models[0] 是前端选中该 provider 后**自动选上**的那个,
        #    存进 settings.json 再由 get_llm_config 原样发出去 —— 它是「发」不是「认」。
        # 🔴 原来这里列两个名字(deepseek-chat / deepseek-reasoner),让用户以为
        #    有"对话档"和"推理档"两种选择。Deploy 2026-09-14 实打:官方 /models
        #    只剩 deepseek-flash 与 deepseek-v4-pro,而 reasoner 仍返 200 却**回显
        #    deepseek-flash** —— 也就是说这两个选项**跑的是同一个模型**。
        #    给用户两个跑同一档的名字,比只给一个更糟:他会以为自己换过了。
        #    Owner 2026-09-14 拍板「全部改成 deepseek-flash」⇒ 并成一个。
        {"id": "deepseek", "name": "DeepSeek",
         "models": [DEEPSEEK_OFFICIAL_FLASH]},
        {"id": "openrouter", "name": "OpenRouter", "models": ["google/gemini-3-pro", "anthropic/claude-sonnet-4.6", "openai/gpt-5.1"]},
        {"id": "doubao", "name": "火山引擎 豆包", "models": ["doubao-seed-2-0-pro-260215"]},
        {"id": "kimi", "name": "Moonshot Kimi", "models": ["kimi-k2.6"]},
    ]
    
    return {
        "llm_config": config.get("llm_config", {"provider": "dashscope", "model": "qwen3.7-max"}),
        "styles": styles,
        "available_providers": available_providers,
    }


def save_writing_config(
    llm_config: dict = None,
    prompt_overrides: dict = None,
    prompt_override_contract_versions: dict = None,
) -> dict:
    """
    保存写作配置
    
    Args:
        llm_config: {"provider": "...", "model": "..."} 或 None 不修改
        prompt_overrides: {"ranking_v2": "新提示词...", ...} 或 None 不修改
    
    Returns:
        保存后的完整配置
    """
    config = _get_writing_config()
    
    if llm_config:
        config["llm_config"] = llm_config
    
    if prompt_overrides is not None:
        existing = config.get("prompt_overrides", {})
        existing.update(prompt_overrides)
        # 清理 null 值（表示重置为默认）
        config["prompt_overrides"] = {k: v for k, v in existing.items() if v is not None}
    if prompt_override_contract_versions is not None:
        versions = config.get("prompt_override_contract_versions", {})
        versions.update(prompt_override_contract_versions)
        config["prompt_override_contract_versions"] = {
            k: v for k, v in versions.items() if v is not None
        }
    
    _save_writing_config(config)
    return config


def reset_prompt_override(style_code: str) -> str:
    """
    重置指定风格的提示词为默认值
    
    Returns:
        默认提示词内容
    """
    config = _get_writing_config()
    overrides = config.get("prompt_overrides", {})
    if style_code in overrides:
        del overrides[style_code]
        config["prompt_overrides"] = overrides
        versions = config.get("prompt_override_contract_versions", {})
        versions.pop(style_code, None)
        config["prompt_override_contract_versions"] = versions
        _save_writing_config(config)
    
    return _get_default_prompt(style_code)


def get_active_llm_config() -> dict:
    """获取当前生效的 LLM 配置（供 generate_articles 使用）"""
    config = _get_writing_config()
    return config.get("llm_config", {"provider": "dashscope", "model": "qwen3.7-max"})


# ============================================
# 测试
# ============================================

if __name__ == "__main__":
    print("=== 风格注册中心测试 ===")
    print("\n当前风格配置：")
    for style in get_all_styles():
        print(f"  - {style['name']}: {style['ratio']}%")
    
    print("\n生成10篇文章的风格分配：")
    allocation = allocate_styles_by_ratio(10, industry=None)  # v2.7.1 industry 贯穿
    for i, code in enumerate(allocation, 1):
        print(f"  文章{i}: {WRITING_STYLES[code]['name']}")
    
    print("\n前端接口数据：")
    print(get_styles_for_frontend())
