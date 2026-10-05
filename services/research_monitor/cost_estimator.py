"""
跑批成本估算

Phase 9 (2026-05-25) 更新:
- AI 调用按平台拆 (豆包按次 · 其余按 token 均价 · 单价为示例值)
- LLM 清洗 + LLM 评分已删 (改规则清洗 0 成本 · 删 LLM 评分)
- Jina 爬取: 按 url 计(示例值)

各项单价(参数在 geo_research_config 表 future-extensible):
- 各平台单价见下方 AI_CALL_PER_PLATFORM(示例值)
- OSS: 忽略不计
"""
import math
from typing import Dict


# Phase 9: 按平台拆单价 (跟 round_runner.COST_PER_AI_CALL_BY_PLATFORM 必须保持一致)
AI_CALL_PER_PLATFORM = {
    'doubao':   0.10,
    'deepseek': 0.03,
    'qwen':     0.03,
    'kimi':     0.04,
}

UNIT_PRICES = {
    'ai_call_per_unit': sum(AI_CALL_PER_PLATFORM.values()) / len(AI_CALL_PER_PLATFORM),  # 兜底均价
    'ai_call_per_round_4_platforms': sum(AI_CALL_PER_PLATFORM.values()),  # 每题(4 平台一起)
    'jina_per_url': 0.01,
    'llm_clean_per_article': 0.0,   # Phase 9 已删 LLM 清洗
    'llm_score_per_article': 0.0,   # Phase 9 已删 LLM 评分
}

URL_FILTER_KEEP_RATIO = 0.6
ARTICLE_PASS_3K_CHARS_RATIO = 0.5


def estimate_round_cost(
    industry_count: int,
    prompts_per_industry: int,
    platforms_count: int,
    estimated_url_per_call: float,
) -> Dict:
    """
    估算单轮跑批成本。

    返回:
    {
        'total_yuan': 预估总成本,
        'breakdown': {
            'ai_fetch_yuan': AI 调用费,
            'jina_crawl_yuan': Jina 爬取费,
            'llm_clean_yuan': [Phase 9 deprecated · 规则清洗 0 成本] LLM 清洗费 · 留 0 占位兼容老调用方,
            'llm_score_yuan': [Phase 9 deprecated · 评分已删] LLM 评分费 · 同上,
        },
        'estimated_calls': N,
        'estimated_urls_after_filter': N,
        'estimated_articles_in_library': N (P12-fix-v4 改名 · 旧 estimated_articles_pending_review),
    }
    """
    # 入参校验(防 A.5/A.6 调用时崩)
    for name, val in [
        ('industry_count', industry_count),
        ('prompts_per_industry', prompts_per_industry),
        ('platforms_count', platforms_count),
        ('estimated_url_per_call', estimated_url_per_call),
    ]:
        if val is None:
            raise ValueError(f"{name} 不能为 None")
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            raise TypeError(f"{name} 必须是 int/float, 收到 {type(val).__name__}")
        if val < 0:
            raise ValueError(f"{name} 不能为负: {val}")

    if industry_count == 0 or prompts_per_industry == 0 or platforms_count == 0:
        return {
            'total_yuan': 0.0,
            'breakdown': {
                'ai_fetch_yuan': 0.0,
                'jina_crawl_yuan': 0.0,
                'llm_clean_yuan': 0.0,
                'llm_score_yuan': 0.0,
            },
            'estimated_calls': 0,
            'estimated_urls_after_filter': 0,
            # P12-fix-v4 (2026-05-26): 字段改名对齐 P09 in_library 语义 · 旧 'estimated_articles_pending_review'
            'estimated_articles_in_library': 0,
        }

    total_calls = industry_count * prompts_per_industry * platforms_count

    # Phase 9: AI 调用费按 4 平台合算 (每题 4 平台一起跑, 一题 ¥0.38)
    # 若 platforms_count=4 (默认所有平台) 用 _per_round_4_platforms 准确单价
    # 否则按均价兜底
    if platforms_count == 4:
        ai_fetch_yuan = industry_count * prompts_per_industry * UNIT_PRICES['ai_call_per_round_4_platforms']
    else:
        ai_fetch_yuan = total_calls * UNIT_PRICES['ai_call_per_unit']

    raw_urls = total_calls * estimated_url_per_call
    deduplicated_urls = raw_urls * 0.5
    crawled_urls = deduplicated_urls * URL_FILTER_KEEP_RATIO

    jina_crawl_yuan = crawled_urls * UNIT_PRICES['jina_per_url']

    articles_with_content = crawled_urls * 0.9

    llm_clean_yuan = articles_with_content * UNIT_PRICES['llm_clean_per_article']

    # P12-fix-v4: 变量改名 pending_review_articles → in_library_articles (对齐 P09 stage5 新出口语义)
    # ARTICLE_PASS_3K_CHARS_RATIO 保留兼容 · 实际门槛由 article_min_chars_for_review 配置决定
    in_library_articles = articles_with_content * ARTICLE_PASS_3K_CHARS_RATIO
    llm_score_yuan = in_library_articles * UNIT_PRICES['llm_score_per_article']

    total = ai_fetch_yuan + jina_crawl_yuan + llm_clean_yuan + llm_score_yuan

    return {
        'total_yuan': round(total, 2),
        'breakdown': {
            'ai_fetch_yuan': round(ai_fetch_yuan, 2),
            'jina_crawl_yuan': round(jina_crawl_yuan, 2),
            'llm_clean_yuan': round(llm_clean_yuan, 2),
            'llm_score_yuan': round(llm_score_yuan, 2),
        },
        'estimated_calls': total_calls,
        'estimated_urls_after_filter': int(crawled_urls),
        # P12-fix-v4 (2026-05-26): 字段改名对齐 P09 · 旧 'estimated_articles_pending_review'
        'estimated_articles_in_library': int(in_library_articles),
    }


# ============================================================
# [R 批 · U3] 自助单行业调研 计价(SPEC §3.5-P0-5)
#
# 为什么要单独一层:上面的 estimate_round_cost 只含「调研 AI 调用 + Jina 爬取」,
# 漏了自助轮同样会跑的下游 —— L3 答案实体抽取(每题一次 LLM)、飞轮桥接 LLM glue、
# 以及失败重试余量。直接拿 estimate_round_cost 给用户报价会低估、亏本。故这里在
# base 之上「包一层加足」:+ L3 + 桥接余量,再 × 安全系数(重试)。
#
# 定价节量级校准(与 SPEC 定价节对齐,允许因常量微调有 ±1 个 10-桶出入):
#   base(n) = estimate_round_cost(1, n, 4, 10).total_yuan = n × ¥0.62(¥0.38 AI + ¥0.24 Jina)
#   L3/桥接 随文章量 ∝ 题数,故按题摊(per-prompt),不是一次性 flat:
#     每题额外 = L3 0.07 + 桥接 0.05 = ¥0.12 → 每题综合成本 ≈ ¥0.74
#   × 安全系数 1.15 → 每题 ≈ ¥0.851 成本 → 售价 ×2 ×130 ≈ 221 算力/题
#   ⇒ 6 题 ≈ ¥5.11 → 1330 · 2 题 ≈ ¥1.70 → 450 · 20 题 ≈ ¥17.02 → 4430(量级对齐)
#
# 🔴 LLM 只做行业/意图语义判断,一分钱数字都不产:本文件所有价格纯常量/SQL 口径。
# ============================================================

# L3 答案实体抽取:每 answer-group 一次 deepseek-v4-flash 抽实体(~¥0.07/次 量级)
SELFSERVE_L3_YUAN_PER_PROMPT = 0.07
# [GEO-R1-CAN-090] L3 抽取按 answer-group 计费,不是按题:一题被 4 引擎回答会产生
# 最多 4 个 answer-group(fetch_answer_groups_for_extraction GROUP BY engine_norm,batch_key,answer_md5),
# 故每题 L3 调用数 ∝ 引擎数,而不是 1。用 4 平台(与本函数 platforms_count=4 一致)作 worst-case 上界,
# 避免低估亏本。安全系数照旧整轮再乘一次。
SELFSERVE_L3_ENGINE_COUNT = 4
# 飞轮桥接 LLM glue + 少量重试余量(小额,随文章量按题摊)
SELFSERVE_BRIDGE_YUAN_PER_PROMPT = 0.05
# 重试余量安全系数(整轮乘一次)
SELFSERVE_SAFETY_MULT = 1.15

# 护栏:自助轮题数区间(端点层会把 ValueError 转成友好错)
SELFSERVE_MIN_PROMPTS = 1
SELFSERVE_MAX_PROMPTS = 20


def estimate_selfserve_round_cost(
    prompt_count: int,
    estimated_url_per_call: float = 10.0,
) -> Dict:
    """自助单行业调研成本估算(base 调研+Jina 之上包 L3 + 桥接 + 安全系数)。

    参数:
      prompt_count           : 勾选题目数(护栏 1..20,越界 raise ValueError)
      estimated_url_per_call : 每次 AI 调用预估 URL 数(与 estimate_round_cost 同口径,默认 10)

    返回:
      {
        'cost_yuan': 综合成本(含 L3 + 桥接 + ×1.15 安全系数),
        'breakdown': {research_yuan, l3_yuan, bridge_yuan, safety_mult},
        'prompt_count': N,
      }
    """
    n = int(prompt_count)
    if n < SELFSERVE_MIN_PROMPTS or n > SELFSERVE_MAX_PROMPTS:
        raise ValueError(
            f"prompt_count 必须在 {SELFSERVE_MIN_PROMPTS}..{SELFSERVE_MAX_PROMPTS} 之间, 收到 {n}"
        )

    # 单行业(industry_count=1) · 4 平台全量 · base = 调研 AI + Jina
    base = estimate_round_cost(
        industry_count=1,
        prompts_per_industry=n,
        platforms_count=4,
        estimated_url_per_call=estimated_url_per_call,
    )
    research_yuan = base['total_yuan']
    # [GEO-R1-CAN-090] 每题 L3 抽取按 answer-group 数计:题数 × 引擎数(worst-case 每引擎独立答案)
    l3_yuan = round(SELFSERVE_L3_YUAN_PER_PROMPT * n * SELFSERVE_L3_ENGINE_COUNT, 2)
    bridge_yuan = round(SELFSERVE_BRIDGE_YUAN_PER_PROMPT * n, 2)

    cost_yuan = round((research_yuan + l3_yuan + bridge_yuan) * SELFSERVE_SAFETY_MULT, 2)

    return {
        'cost_yuan': cost_yuan,
        'breakdown': {
            'research_yuan': research_yuan,
            'l3_yuan': l3_yuan,
            'bridge_yuan': bridge_yuan,
            'safety_mult': SELFSERVE_SAFETY_MULT,
        },
        'prompt_count': n,
    }


def selfserve_price_points(cost_yuan: float) -> int:
    """成本(元)→ 售价算力点。

    定价规则:售价 = 成本 × 2(毛利)→ × 130(元→算力汇率)→ 向上取整到 10 的整数倍。
    (与代理端报价「成本×markup→转算力」同源思路;此处 markup=2.0 面向自助散户。)
    """
    if cost_yuan is None or cost_yuan <= 0:
        return 0
    return math.ceil(float(cost_yuan) * 2 * 130 / 10) * 10
