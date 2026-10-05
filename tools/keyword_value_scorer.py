"""
关键词三维交叉验证评分系统

三个信号源：
1. 5118 数据（稳定）：搜索量、SEM出价、竞价公司数 → 词的商业价值 + 竞争烈度
2. 秘塔搜索（一次快照）：现有内容数、平台分布 → 内容供给密度
3. LLM 判断（确定性推理）：搜索意图分类、漏斗阶段 → 词的转化潜力

最终输出：
- difficulty_score (难度系数 0.85-2.0)：词的竞争难度（含搜索量、竞价公司数、有效竞争池）
- value_score (价值系数 0.8-2.0)：词的商业价值（含SEM出价、搜索意图、漏斗阶段）
- keyword_price = required_articles × cost × markup × value_score × difficulty_score
"""
import asyncio
import json
import math
import re
from typing import Optional

# [P1 容量合同 2026-08-08] 篇数唯一取数出口(本文件曾就地复述主合同 5/7/10)
from tools.pricing_bands import tier_article_capacity

# [M4 SSOT 2026-06-07 · 老板拍板] 单个关键词最低售价 = ¥400 · 全仓单一权威源
#   口径:所有词【一视同仁·含品牌词·不豁免】最低 ¥400(老板"任何投放词低于400风险都高")。
#   取消原 全国词350/地域词120 区分 → 统一 400。batch_pricing/selection_api/pricing_auditor 均 import 此处。
#   ⚠️ 只改常量·不回溯存量 keyword_price_cache(老客户价不突变·靠 7 天 TTL 自然过期重算)。
MIN_KEYWORD_PRICE = 400
MIN_KEYWORD_PRICE_NATIONAL = 400  # = MIN_KEYWORD_PRICE(一视同仁·保留别名兼容现有引用)


# ========================================
# 1. 5118 数据层（稳定信号）
# ========================================

async def fetch_5118_batch(keywords: list[str]) -> dict[str, dict]:
    """
    批量获取5118关键词数据（并行分片）

    Returns:
        {关键词: {search_volume, sem_price, competition, bidword_company_count}}
    """
    from tools.api_5118 import get_5118_client

    client = await get_5118_client()
    results = {}

    # 5118 每批最多50个 → 并行发出所有分片
    batches = [keywords[i:i + 50] for i in range(0, len(keywords), 50)]

    async def fetch_chunk(batch: list[str]):
        response = await client.get_keyword_search_volume(batch)
        chunk = {}
        if response.get("success"):
            for item in response["keywords"]:
                kw = item["keyword"]
                chunk[kw] = {
                    "search_volume": item.get("index", 0) + item.get("mobile_index", 0),
                    "sem_price": item.get("sem_price", 0),
                    "competition": item.get("competition", 0),
                    "bidword_company_count": item.get("bidword_company_count", 0),
                }
        return chunk

    chunk_results = await asyncio.gather(*[fetch_chunk(b) for b in batches])
    for chunk in chunk_results:
        results.update(chunk)

    # 没查到的词填默认值
    for kw in keywords:
        if kw not in results:
            results[kw] = {
                "search_volume": 0,
                "sem_price": 0,
                "competition": 0,
                "bidword_company_count": 0,
            }

    return results


# ========================================
# 2. 秘塔数据层（一次快照，可缓存）
# ========================================

# 关键词模式匹配 — 用于智能兜底
#
# [SSOT business-governance-master §9.5/§9.6/§17.1] 下面的 _HIGH_COMMERCIAL_PATTERNS /
# _INFO_PATTERNS 仅服务 estimate_competition_from_keyword 的「竞品数量」定价兜底
# （秘塔无结果时的数值估算），属定价启发式，**不是**付费交付资格判定的第二套引擎。
# 付费交付资格的唯一权威是 services.commercial_query_policy（见 api/selection_api 提交
# 分区）。按 §9.6，这里的定价数值不得被内容审核逻辑改动，故保留原样。
_HIGH_COMMERCIAL_PATTERNS = [
    "推荐", "哪家好", "哪家强", "排名", "排行", "怎么选",
    "对比", "评测", "TOP", "靠谱", "正规", "榜单",
    "服务商", "公司推荐", "十大", "前十",
]
_INFO_PATTERNS = [
    "怎么做", "攻略", "流程", "步骤", "教程", "是什么", "什么意思",
]


def estimate_competition_from_keyword(keyword: str, demand_signal: float | None = None) -> int:
    """
    根据关键词模式估算竞品数量（当秘塔搜索无结果时兜底）

    基于调研数据（378篇被AI引用文章）的经验基线：
    - 含商业意图词（推荐/排名/哪家好）→ 默认 10 个竞品
    - 含信息型词（怎么做/攻略/流程）→ 默认 3 个竞品
    - 其他 → 默认 5 个竞品

    Args:
        demand_signal: [工单 2026-07-26 P1-6] 该词的真实需求证据强度（5118 搜索量 /
            SEM 出价 / 投放家数之和）。**不传 = 完全保持原行为**（逐值零变化）。
            传 0 表示"这个词没有任何真实需求数据"：此时**禁止**再因为词里带了
            "推荐/哪家好"这类商业模式就把竞品数从 5 抬到 10/15 —— 那正是生产上
            "越废越贵"（街道废词 ¥5760 > 城市真词 ¥3250）的兜底路径来源。
            没人搜的词不可能因为写法像商业词就真有 15 家竞品。
    """
    commercial_hits = sum(1 for p in _HIGH_COMMERCIAL_PATTERNS if p in keyword)
    info_hits = sum(1 for p in _INFO_PATTERNS if p in keyword)

    # [D3 · SSOT business-governance-master §9.5/§9.6] 竞品数兜底值由 admin 经
    # system_settings.pricing_config 治理(config.pricing_config)。默认严格等于原硬编码
    # 15/10/3/5 → DB 无配置时逐值零变化(§9.6);DB 不可用时 except 回落默认(不阻塞)。
    try:
        from config.pricing_config import get_pricing_config

        _counts = get_pricing_config().get("competition_fallback_counts") or {}
    except Exception:
        _counts = {}
    strong = int(_counts.get("strong", 15))
    commercial = int(_counts.get("commercial", 10))
    info = int(_counts.get("info", 3))
    other = int(_counts.get("other", 5))

    # [工单 P1-6] 零需求证据 → 不许靠"商业词模式"上调竞品数(只许下调)
    if demand_signal is not None and float(demand_signal) <= 0:
        if info_hits >= 1:
            return min(info, other)
        return other

    if commercial_hits >= 2:
        return strong  # 强商业意图
    elif commercial_hits >= 1:
        return commercial  # 商业意图
    elif info_hits >= 1:
        return info   # 信息查询型
    else:
        return other   # 其他/未知


# 来源权威度分级（与 pricing_auditor.py 共用标准）
_S_DOMAINS = ["people.com.cn", "xinhuanet.com", "cctv.com", "gov.cn", "china.com.cn"]
_A_DOMAINS = ["36kr.com", "iyiou.com", "jiqizhixin.com", "huxiu.com", "tmtpost.com"]
_B_DOMAINS = ["zhihu.com", "baijiahao.baidu.com", "sohu.com", "163.com", "qq.com", "sina.com.cn"]
_C_DOMAINS = ["csdn.net", "cnblogs.com", "jianshu.com", "toutiao.com", "bilibili.com"]
_SOCIAL_DOMAINS = ["douyin.com", "xiaohongshu.com", "weibo.com"]


def _classify_domain_authority(domain: str) -> str:
    """根据域名判断权威等级"""
    if not domain:
        return "D"
    for d in _S_DOMAINS:
        if d in domain:
            return "S"
    for d in _A_DOMAINS:
        if d in domain:
            return "A"
    for d in _B_DOMAINS:
        if d in domain:
            return "B"
    for d in _C_DOMAINS:
        if d in domain:
            return "C"
    for d in _SOCIAL_DOMAINS:
        if d in domain:
            return "social"
    return "D"


def _analyze_source_authority(classified_results: list[dict]) -> dict:
    """
    分析搜索结果中各来源的权威等级分布

    只统计A类（竞品）和B类（资讯）的来源，因为这些是AI引用的主要候选
    """
    tier_counts = {}
    for r in classified_results:
        cat = r.get("category", "D")
        if cat not in ("A", "B"):
            continue
        domain = r.get("domain", "")
        tier = _classify_domain_authority(domain)
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
    return tier_counts


async def fetch_metaso_batch(
    keywords: list[str],
    concurrency: int = 15,
    cached: dict = None
) -> dict[str, dict]:
    """
    批量获取秘塔搜索数据（支持缓存）+ 批量LLM内容分类

    优化后流程（大幅减少LLM调用次数）：
    Phase 1: 并行搜索 + 规则预处理（semaphore=15，纯I/O）
    Phase 2: 批量LLM分类（每8个关键词一次LLM调用，50词→7次调用）
    Phase 3: 统计聚合（纯本地计算）

    Returns:
        {关键词: {content_count, competition_count, category_counts, recent_count, top_platforms}}
    """
    from tools.competition_analyzer import (
        search_metaso, distill_result, batch_classify_results_with_llm,
    )
    from tools.metaso_health import metaso_result_error

    results = {}
    new_keywords = []

    # 先从缓存恢复
    if cached:
        for kw in keywords:
            if kw in cached:
                results[kw] = cached[kw]
            else:
                new_keywords.append(kw)
    else:
        new_keywords = list(keywords)

    if not new_keywords:
        return results

    # ========== Phase 1: 并行搜索 + 蒸馏（纯I/O，高并发） ==========
    semaphore = asyncio.Semaphore(concurrency)
    fallback_results = {}   # 搜索失败的词 → 兜底数据
    search_results = {}     # 搜索成功的词 → distilled列表

    async def search_one(kw: str):
        async with semaphore:
            result = await search_metaso(kw, size=100)
            # [止血 2026-08-04] 原守卫只认 "error" 键 —— 而秘塔业务级失败(余额不足
            #   errCode=3000)走 HTTP 200 通道返回,压根没有 "error" 键,判据对这种
            #   失败形状没有判别力 → 兜底被绕过 → webpages 缺失 → A/B/C 全 0 →
            #   effective_competition = max(1, 0) = 1(地板值)静默进报价。
            #   改判「结果可用性」:error 键 / errCode 非 0 / 响应体不是搜索结果形状。
            #   信封完整的 webpages=[] 是冷门词真·零结果,不判失败(见 metaso_health)。
            unusable = metaso_result_error(result)
            if unusable:
                smart_default = estimate_competition_from_keyword(kw)
                print(f"    [{kw}] 秘塔结果不可用({unusable})，兜底竞品数={smart_default}")
                fallback_results[kw] = {
                    "content_count": 0,
                    "competition_count": smart_default,
                    "effective_competition": smart_default,
                    "category_counts": {"A": 0, "B": 0, "C": 0, "D": 0},
                    "recent_count": 0,
                    "top_platforms": [],
                    "source": "fallback",
                    # 留痕:让下游/复盘能分辨「真的零竞争」与「测不到所以兜底」
                    "fallback_reason": unusable,
                }
                return
            webpages = result.get("webpages", [])
            distilled = [distill_result(wp) for wp in webpages]
            search_results[kw] = {"distilled": distilled, "webpage_count": len(webpages)}

    search_tasks = [search_one(kw) for kw in new_keywords]
    await asyncio.gather(*search_tasks)

    # 搜索失败的词直接放入结果
    results.update(fallback_results)

    # ========== Phase 2: 批量LLM分类（大幅减少调用次数） ==========
    if search_results:
        keyword_distilled = {kw: data["distilled"] for kw, data in search_results.items()}
        print(f"    批量LLM分类: {len(keyword_distilled)}个关键词...")
        classified_map = await batch_classify_results_with_llm(keyword_distilled)
    else:
        classified_map = {}

    # ========== Phase 3: 统计聚合 ==========
    for kw, data in search_results.items():
        classified = classified_map.get(kw, data["distilled"])
        webpage_count = data["webpage_count"]

        category_counts = {"A": 0, "B": 0, "C": 0, "D": 0}
        recent_count = 0
        platform_stats = {}
        for d in classified:
            cat = d.get("category", "D")
            category_counts[cat] = category_counts.get(cat, 0) + 1
            platform = d.get("platform", "其他")
            platform_stats[platform] = platform_stats.get(platform, 0) + 1
            if d.get("is_recent"):
                recent_count += 1

        competition_count = category_counts["A"]
        # 有效竞争池：A类100%权重 + B类50%权重 + C类30%权重
        # AI引擎从所有内容中引用，不仅仅是A类竞品
        effective_competition = max(1,
            category_counts["A"] +
            int(category_counts.get("B", 0) * 0.5) +
            int(category_counts.get("C", 0) * 0.3)
        )
        top_platforms = [
            p for p, _ in sorted(platform_stats.items(), key=lambda x: -x[1])[:3]
            if p != "其他"
        ]
        source_authority = _analyze_source_authority(classified)

        print(f"    [{kw}] 总{webpage_count}条 → A类(竞品){category_counts['A']} "
              f"B类(资讯){category_counts['B']} C类(百科){category_counts['C']} "
              f"D类(噪声){category_counts['D']} → 有效竞争池{effective_competition}")

        results[kw] = {
            "content_count": webpage_count,
            "competition_count": max(competition_count, 1),
            "effective_competition": effective_competition,
            "category_counts": category_counts,
            "recent_count": recent_count,
            "top_platforms": top_platforms,
            "source_authority": source_authority,
        }

    return results


# ========================================
# 2.5 规则兜底：修正 LLM 意图误分类
# ========================================

# GEO 场景中明确指向"找服务商"的关键词模式
_COMMERCIAL_OVERRIDE_PATTERNS = [
    "推荐", "哪家好", "哪家靠谱", "排名", "排行", "前十", "前五", "十大",
    "公司推荐", "品牌推荐", "服务商推荐", "哪家强", "哪家专业",
    "口碑好", "口碑排名", "评价好", "靠谱的",
    "哪里有", "去哪里", "去哪", "哪个平台", "找哪家",
]
_TRANSACTIONAL_OVERRIDE_PATTERNS = [
    "多少钱", "价格", "报价", "收费", "费用", "一天多少",
    "怎么租", "怎么预约", "预约", "下单", "租一天",
]
_INFORMATIONAL_CONFIRM_PATTERNS = [
    "是什么意思", "是什么$", "什么是", "怎么办理", "需要什么条件",
    "流程是", "的流程", "注意事项", "注意什么", "有什么坑",
    "能不能", "会不会", "有什么区别", "和.*区别",
]


def _override_intent_by_pattern(
    keyword: str, llm_intent: str, llm_funnel: str
) -> tuple[str, str]:
    """
    规则兜底：当 LLM 返回 informational 但关键词明显含商业信号时，强制修正。
    只升级不降级 — 不会把 commercial/transactional 降为 informational。

    [SSOT business-governance-master §3.3/§9.5] 本函数产出的 intent 仅作为
    CommercialQueryPolicy.evaluate() 的 intent_hint 与漏斗/定价权重，**从属于**
    统一文本引擎：引擎判定为商业时其结论优先，hint 无法把商业词降级出付费交付
    （见 commercial_query_policy.evaluate 的引擎优先逻辑 + 反向判别测试
    tests/geo_observation 之外的 test_commercial_policy_hint_cannot_override_engine）。
    因此本函数不是付费交付资格的第二套判定引擎。
    """
    # 先检查是否为真正的 informational（确认模式匹配）
    for pattern in _INFORMATIONAL_CONFIRM_PATTERNS:
        if re.search(pattern, keyword):
            return llm_intent, llm_funnel  # 确认是 informational，不覆盖

    # 如果 LLM 已经判定为 commercial/transactional，只修正 funnel
    if llm_intent != "informational":
        # 修正 funnel: 含价格词应该是 decision 而非 awareness
        for p in _TRANSACTIONAL_OVERRIDE_PATTERNS:
            if p in keyword:
                return llm_intent, "decision"
        for p in _COMMERCIAL_OVERRIDE_PATTERNS:
            if p in keyword:
                return llm_intent, max(llm_funnel, "consideration", key=lambda x: {"awareness": 0, "consideration": 1, "decision": 2}.get(x, 0))
        return llm_intent, llm_funnel

    # LLM 说 informational，但关键词有明确商业信号 → 强制升级
    for p in _TRANSACTIONAL_OVERRIDE_PATTERNS:
        if p in keyword:
            return "transactional", "decision"

    for p in _COMMERCIAL_OVERRIDE_PATTERNS:
        if p in keyword:
            return "commercial", "consideration"

    return llm_intent, llm_funnel


# ========================================
# 3. LLM 批量意图分类（一次调用）
# ========================================

async def classify_keywords_with_llm(
    keywords: list[str],
    industry: str = "",
    need_market_estimate: bool = False,
) -> dict[str, dict]:
    """
    用LLM对关键词批量做意图分类

    Args:
        need_market_estimate: 当5118数据缺失时，让LLM额外估算搜索量和SEM出价

    Returns:
        {关键词: {intent, funnel_stage, search_probability, [estimated_search_volume, estimated_sem_price, estimated_bidword_companies]}}
    """
    from tools.multi_llm_caller import call_llm_with_fallback

    # 构造批量分类prompt
    kw_list_str = "\n".join(f"{i + 1}. {kw}" for i, kw in enumerate(keywords))

    # 基础分析字段
    fields_desc = """请对每个关键词判断:
1. intent（搜索意图）: informational(信息查询) / commercial(商业比较) / transactional(交易决策)

   **GEO场景特殊规则（必须遵守！）**：
   这些关键词用于 GEO（AI搜索优化），核心判断标准是：**AI收到这个问题后，回答里会不会列出具体的公司/品牌/服务商？**
   - 含"推荐/哪家好/排名/排行/前十/哪家靠谱/公司推荐" → 一律 commercial 或 transactional（用户在找服务商！）
   - 含"多少钱/价格/报价/租一天" → transactional（用户要下单了！）
   - 含"哪里有/去哪里/哪个平台" → commercial（用户在找渠道！）
   - 只有纯知识型（"是什么/流程/注意事项/政策"）才是 informational
   **错误示例**："深圳商务租车公司排名前十" 不是 informational！用户问排名就是要找公司，应该是 commercial。
   **错误示例**："深圳租埃尔法带司机一天多少钱" 不是 informational！用户问价格就是要租车，应该是 transactional。

2. funnel_stage（漏斗阶段）: awareness(认知) / consideration(考虑) / decision(决策)
   - 含"推荐/排名/哪家好/对比" → consideration（在比较选择）
   - 含"多少钱/价格/预约/怎么租" → decision（准备下单）
   - 含"是什么/了解/科普" → awareness（刚了解）

3. search_probability（真实搜索可能性，0.1-1.0）: 真实用户在AI搜索引擎中输入这个词的概率有多大？
   - 0.8-1.0: 非常自然的搜索词，用户经常会这样搜
   - 0.5-0.7: 比较常见的搜索方式
   - 0.2-0.4: 不太自然，但偶尔会搜
   - 0.1: 几乎不会有人这样搜"""

    # 5118数据缺失时，增加市场估算字段
    if need_market_estimate:
        fields_desc += """
4. estimated_search_volume（估算月搜索量，整数）: 根据你对该行业的了解，估算这个关键词在搜索引擎中的月搜索量
   - 考虑行业规模、用户群体大小、关键词具体程度
   - 核心大词: 1000-10000+
   - 常见长尾词: 100-1000
   - 小众/地域词: 10-100
   - 极小众: 0-10
5. estimated_sem_price（估算SEM点击出价，浮点数，单位元）: 如果有人在搜索引擎投放这个关键词广告，每次点击大概多少钱？
   - 高商业价值词（金融、医疗、法律）: 5-30元
   - 中等商业价值词（教育、房产、装修）: 2-10元
   - 低商业价值词（资讯、知识）: 0.5-3元
6. estimated_bidword_companies（估算竞价公司数，整数）: 有多少家公司会投放这个关键词的广告？
   - 热门行业词: 50-500+
   - 一般行业词: 5-50
   - 小众/长尾词: 0-5"""

    example_fields = '"intent": "commercial", "funnel_stage": "consideration", "search_probability": 0.7'
    if need_market_estimate:
        example_fields += ', "estimated_search_volume": 500, "estimated_sem_price": 3.5, "estimated_bidword_companies": 20'

    prompt = f"""你是搜索意图分析和市场研究专家。请对以下{len(keywords)}个关键词进行分析。
{f'行业背景: {industry}' if industry else ''}

关键词列表:
{kw_list_str}

{fields_desc}

请严格按JSON数组格式返回，不要任何其他文字:
[
  {{"keyword": "关键词1", {example_fields}}},
  ...
]"""

    try:
        response = await call_llm_with_fallback(prompt, verbose=False)

        # 提取JSON
        json_match = re.search(r'\[.*\]', response, re.DOTALL)
        if not json_match:
            raise ValueError("No JSON array found in LLM response")

        items = json.loads(json_match.group())

        results = {}
        for item in items:
            kw = item.get("keyword", "")
            # 模糊匹配：LLM可能返回略有不同的关键词文本
            matched_kw = kw
            if kw not in keywords:
                for original_kw in keywords:
                    if kw in original_kw or original_kw in kw:
                        matched_kw = original_kw
                        break

            entry = {
                "intent": item.get("intent", "informational"),
                "funnel_stage": item.get("funnel_stage", "awareness"),
                "search_probability": max(0.1, min(1.0, float(item.get("search_probability", 0.5)))),
            }
            if need_market_estimate:
                entry["estimated_search_volume"] = max(0, int(item.get("estimated_search_volume", 50)))
                entry["estimated_sem_price"] = max(0, float(item.get("estimated_sem_price", 1.0)))
                entry["estimated_bidword_companies"] = max(0, int(item.get("estimated_bidword_companies", 5)))

            results[matched_kw] = entry

        # 规则兜底：修正 LLM 明显错误的 intent 分类
        for kw_text, entry in results.items():
            entry["intent"], entry["funnel_stage"] = _override_intent_by_pattern(
                kw_text, entry["intent"], entry["funnel_stage"]
            )

        # 填充未匹配的词
        for kw in keywords:
            if kw not in results:
                entry = {
                    "intent": "informational",
                    "funnel_stage": "awareness",
                    "search_probability": 0.5,
                }
                if need_market_estimate:
                    entry["estimated_search_volume"] = 50
                    entry["estimated_sem_price"] = 1.0
                    entry["estimated_bidword_companies"] = 5
                results[kw] = entry

        return results

    except Exception as e:
        print(f"  LLM关键词分类失败: {e}，使用默认值")
        defaults = {}
        for kw in keywords:
            entry = {
                "intent": "commercial",
                "funnel_stage": "consideration",
                "search_probability": 0.5,
            }
            if need_market_estimate:
                entry["estimated_search_volume"] = 50
                entry["estimated_sem_price"] = 1.0
                entry["estimated_bidword_companies"] = 5
            defaults[kw] = entry
        return defaults


# ========================================
# 4. 三维评分合成
# ========================================

# 意图价值映射
INTENT_VALUE = {
    "transactional": 1.5,   # 交易型：用户要买东西，最值钱
    "commercial": 1.2,      # 商业型：用户在比较，值钱
    "informational": 0.8,   # 信息型：用户只是了解，价值较低
}

# 漏斗阶段价值映射
FUNNEL_VALUE = {
    "decision": 1.4,        # 决策阶段：即将转化
    "consideration": 1.1,   # 考虑阶段：有意向
    "awareness": 0.8,       # 认知阶段：刚了解
}


def compute_difficulty_score(
    five118_data: dict,
    metaso_data: dict,
) -> float:
    """
    计算难度系数 (0.85 - 2.0)

    三个维度加权：
    - 5118搜索量 (50%): 搜索量越大 → 词越值钱，越难竞争（主导定价区分度）
    - 有效竞争池 (25%): A类竞品+B类资讯×0.5+C类百科×0.3
    - 5118竞价公司数 (25%): 多少公司在投这个词 → 商业竞争烈度

    关键设计：范围 0.85-2.0
    - 低于1.0 = 小幅折扣（低热度低竞争，但保证毛利率不低于35%）
    - 高于1.0 = 溢价（高热度高竞争的词更值钱）
    """
    # --- 5118搜索量 → 0-1 归一化（权重最高）---
    search_volume = five118_data.get("search_volume", 0)
    # 平方根归一化：50→0.16, 300→0.39, 1000→0.71, 2000+=1.0
    if search_volume <= 0:
        volume_norm = 0
    else:
        volume_norm = min(1.0, (search_volume / 2000) ** 0.5)

    # --- 有效竞争池 → 0-1 归一化 ---
    competition_count = metaso_data.get("effective_competition", metaso_data.get("competition_count", 1))
    # 1条=0, 15条=0.5, 30条+=1.0
    competition_norm = min(1.0, (competition_count - 1) / 29)

    # --- 5118竞价公司数 → 0-1 归一化 ---
    bidword_companies = five118_data.get("bidword_company_count", 0)
    # 0家=0, 5家=0.5, 10家+=1.0
    company_norm = min(1.0, bidword_companies / 10)

    # 加权合成（搜索量权重最高，50%主导定价区分度）
    raw_score = (
        volume_norm * 0.50 +
        competition_norm * 0.25 +
        company_norm * 0.25
    )

    # 映射到 0.85 - 2.0（保证低热度词不压毛利，高热度词溢价）
    return 0.85 + raw_score * 1.15


def compute_value_score(
    five118_data: dict,
    llm_data: dict,
    keyword: str = "",
) -> float:
    """
    计算价值系数 (0.8 - 2.0)

    三个维度：
    - 5118 SEM出价：别人愿意为这个词付多少钱（客观市场价值）
    - LLM意图分类：搜索意图越接近交易端，价值越高
    - 关键词模式：含高商业意图词的关键词额外加分
    """
    # --- SEM出价 → 价值倍率 ---
    sem_price = five118_data.get("sem_price", 0)
    if sem_price <= 0:
        # 5118无数据时，用LLM估算值兜底
        # 商业/交易意图的新词上限放宽到0.6（这些词往往因为太新才没5118数据）
        estimated = llm_data.get("estimated_sem_price", 0)
        if estimated > 0:
            intent_for_cap = llm_data.get("intent", "informational")
            max_llm_boost = 0.6 if intent_for_cap in ("commercial", "transactional") else 0.4
            sem_factor = 1.0 + min(max_llm_boost, estimated / 37.5)
        else:
            sem_factor = 1.0
    else:
        sem_factor = 1.0 + min(0.8, sem_price / 18.75)

    # --- LLM意图 → 价值倍率 ---
    intent = llm_data.get("intent", "informational")
    funnel = llm_data.get("funnel_stage", "awareness")
    search_prob = llm_data.get("search_probability", 0.5)

    intent_factor = INTENT_VALUE.get(intent, 1.0)
    funnel_factor = FUNNEL_VALUE.get(funnel, 1.0)

    # 搜索概率作为折扣: 没人搜的词再值钱也没用
    prob_factor = 0.7 + 0.3 * search_prob  # 0.77 - 1.0

    # --- 关键词模式加分（高商业意图词）---
    pattern_bonus = 1.0
    if keyword:
        matched = sum(1 for p in _HIGH_COMMERCIAL_PATTERNS if p in keyword)
        if matched >= 2:
            pattern_bonus = 1.15  # 含2个以上商业意图词
        elif matched >= 1:
            pattern_bonus = 1.08  # 含1个商业意图词

    # 综合：SEM市场价 × 意图 × 漏斗 × 搜索概率 × 关键词模式
    # 然后归一化到 0.8-2.0 区间
    raw = sem_factor * intent_factor * funnel_factor * prob_factor * pattern_bonus


    # raw 范围大约 0.5 - 3.6，映射到 0.8 - 2.0
    return max(0.8, min(2.0, raw))


def compute_required_articles(
    metaso_data: dict,
    target_share: float,
) -> int:
    """
    基于秘塔快照数据计算需要发布的文章数

    使用 competition_count（经过is_competition过滤的竞品数）作为基础
    """
    competition_count = metaso_data.get("effective_competition", metaso_data.get("competition_count", 1))
    competition_count = max(competition_count, 1)

    if target_share >= 1:
        target_share = 0.9
    if target_share <= 0:
        target_share = 0.1

    required = target_share * competition_count / (1 - target_share)
    return max(1, math.ceil(required))


# 中国地域标识（全部地级市+省份+区域，用于检测关键词是否包含地域前缀）
_CITY_PREFIXES = [
    # ---- 直辖市 ----
    "北京", "上海", "天津", "重庆",
    # ---- 广东 ----
    "广州", "深圳", "东莞", "佛山", "珠海", "惠州", "中山", "汕头",
    "江门", "湛江", "茂名", "肇庆", "梅州", "汕尾", "河源", "阳江",
    "清远", "潮州", "揭阳", "云浮", "韶关",
    # ---- 浙江 ----
    "杭州", "宁波", "温州", "绍兴", "嘉兴", "湖州", "金华", "衢州",
    "舟山", "台州", "丽水", "义乌",
    # ---- 江苏 ----
    "南京", "苏州", "无锡", "常州", "南通", "徐州", "镇江", "扬州",
    "泰州", "连云港", "淮安", "盐城", "宿迁", "昆山",
    # ---- 山东 ----
    "济南", "青岛", "烟台", "潍坊", "济宁", "泰安", "临沂", "德州",
    "聊城", "菏泽", "滨州", "枣庄", "东营", "威海", "日照", "淄博",
    # ---- 河南 ----
    "郑州", "洛阳", "开封", "安阳", "新乡", "许昌", "焦作", "南阳",
    "商丘", "信阳", "周口", "驻马店", "平顶山", "鹤壁", "濮阳",
    "漯河", "三门峡",
    # ---- 四川 ----
    "成都", "绵阳", "德阳", "宜宾", "南充", "乐山", "泸州", "达州",
    "眉山", "广安", "自贡", "攀枝花", "内江", "遂宁", "广元", "雅安",
    "巴中", "资阳",
    # ---- 湖北 ----
    "武汉", "宜昌", "襄阳", "荆州", "黄石", "十堰", "鄂州", "荆门",
    "孝感", "黄冈", "咸宁", "随州", "恩施",
    # ---- 湖南 ----
    "长沙", "株洲", "湘潭", "衡阳", "邵阳", "岳阳", "常德", "张家界",
    "益阳", "郴州", "永州", "怀化", "娄底", "湘西",
    # ---- 福建 ----
    "福州", "厦门", "泉州", "漳州", "莆田", "三明", "南平", "龙岩",
    "宁德",
    # ---- 安徽 ----
    "合肥", "芜湖", "蚌埠", "淮南", "马鞍山", "淮北", "铜陵", "安庆",
    "黄山", "滁州", "阜阳", "宿州", "六安", "亳州", "池州", "宣城",
    # ---- 河北 ----
    "石家庄", "唐山", "保定", "邯郸", "邢台", "承德", "张家口", "廊坊",
    "衡水", "沧州", "秦皇岛",
    # ---- 陕西 ----
    "西安", "咸阳", "宝鸡", "渭南", "汉中", "延安", "榆林", "安康",
    "商洛", "铜川",
    # ---- 辽宁 ----
    "沈阳", "大连", "鞍山", "抚顺", "本溪", "丹东", "锦州", "营口",
    "阜新", "辽阳", "盘锦", "铁岭", "朝阳", "葫芦岛",
    # ---- 江西 ----
    "南昌", "赣州", "九江", "景德镇", "萍乡", "新余", "鹰潭", "吉安",
    "宜春", "抚州", "上饶",
    # ---- 云南 ----
    "昆明", "曲靖", "玉溪", "保山", "昭通", "丽江", "普洱", "临沧",
    "大理", "红河", "文山", "西双版纳", "德宏",
    # ---- 贵州 ----
    "贵阳", "遵义", "六盘水", "安顺", "毕节", "铜仁",
    # ---- 广西 ----
    "南宁", "柳州", "桂林", "梧州", "北海", "防城港", "钦州", "贵港",
    "玉林", "百色", "贺州", "河池", "来宾", "崇左",
    # ---- 山西 ----
    "太原", "大同", "阳泉", "长治", "晋城", "朔州", "晋中", "运城",
    "忻州", "临汾", "吕梁",
    # ---- 内蒙古 ----
    "呼和浩特", "包头", "乌海", "赤峰", "通辽", "鄂尔多斯", "呼伦贝尔",
    "巴彦淖尔", "乌兰察布",
    # ---- 吉林 ----
    "长春", "吉林市", "四平", "辽源", "通化", "白山", "松原", "白城",
    "延边",
    # ---- 黑龙江 ----
    "哈尔滨", "齐齐哈尔", "鸡西", "鹤岗", "双鸭山", "大庆", "伊春",
    "佳木斯", "七台河", "牡丹江", "黑河", "绥化",
    # ---- 甘肃 ----
    "兰州", "嘉峪关", "金昌", "白银", "天水", "武威", "张掖", "平凉",
    "酒泉", "庆阳", "定西", "陇南",
    # ---- 海南 ----
    "海口", "三亚", "儋州",
    # ---- 宁夏 ----
    "银川", "石嘴山", "吴忠", "固原", "中卫",
    # ---- 青海 ----
    "西宁", "海东",
    # ---- 西藏 ----
    "拉萨", "日喀则", "昌都", "林芝", "山南",
    # ---- 新疆 ----
    "乌鲁木齐", "克拉玛依", "吐鲁番", "哈密", "喀什", "和田", "阿克苏",
    "库尔勒",
    # ---- 省份/自治区名 ----
    "广东", "浙江", "江苏", "山东", "河南", "四川", "湖北", "湖南",
    "福建", "安徽", "河北", "陕西", "辽宁", "江西", "云南", "贵州",
    "广西", "山西", "内蒙古", "新疆", "吉林", "黑龙江", "甘肃",
    "海南", "宁夏", "青海", "西藏", "台湾", "香港", "澳门",
    # ---- 区域标识 ----
    "华南", "华东", "华北", "华中", "西南", "东北", "西北",
    "江浙", "珠三角", "长三角", "大湾区", "京津冀",
    # ---- 常用区县/县级市（用户搜索时常省略"区/县"后缀） ----
    # 重庆区县
    "璧山", "合川", "永川", "涪陵", "大足", "万州", "江津", "南川",
    "綦江", "铜梁", "潼南", "荣昌", "长寿", "黔江", "开州", "梁平",
    "武隆", "丰都", "垫江", "忠县", "云阳", "奉节",
    # 北京区县
    "通州", "大兴", "房山", "密云", "延庆", "平谷", "怀柔", "门头沟",
    "顺义", "昌平", "丰台", "石景山", "亦庄",
    # 上海区县
    "松江", "嘉定", "青浦", "奉贤", "崇明", "金山", "宝山", "闵行",
    "浦东", "南汇", "临港",
    # 天津区县
    "武清", "宝坻", "蓟州", "静海", "宁河", "滨海",
    # 广深常用区
    "南山", "福田", "龙华", "龙岗", "坪山", "光明", "宝安", "盐田",
    "番禺", "花都", "增城", "从化", "黄埔", "白云", "天河", "海珠",
    # 成都区县
    "双流", "郫都", "温江", "新都", "青白江", "龙泉驿", "都江堰",
    "彭州", "邛崃", "崇州", "金堂", "大邑", "蒲江", "新津", "简阳",
    # 杭州区县
    "萧山", "余杭", "临平", "富阳", "临安", "桐庐", "建德", "淳安",
    # 南京区县
    "江宁", "浦口", "六合", "溧水", "高淳",
    # 苏州区县
    "吴江", "吴中", "相城", "张家港", "太仓", "常熟",
    # 武汉区
    "汉阳", "汉口", "武昌", "江夏", "黄陂", "新洲", "蔡甸", "东西湖",
    # 西安区县
    "长安", "临潼", "阎良", "高陵", "鄠邑", "蓝田", "周至",
    # 长沙区县
    "望城", "浏阳", "宁乡",
    # 其他常用县级市/区
    "余姚", "慈溪", "诸暨", "海宁", "桐乡", "平湖",  # 浙江
    "江阴", "宜兴", "溧阳", "如皋", "启东", "海门",  # 江苏
    "胶州", "即墨", "莱西", "平度", "章丘", "邹城",  # 山东
    "巩义", "新郑", "新密", "荥阳", "登封",  # 河南
]




# ========================================
# 5. 完整评分流程
# ========================================

async def score_keywords(
    keywords: list[str],
    industry: str = "",
    target_share: float = 0.20,
    cached_metaso: dict = None,
    concurrency: int = 10,
    brand_name: str = "",
    city: str = "",
    brand_aliases=None,
    cost_per_article_override: float | None = None,
    cost_multiplier: float = 1.0,
    trust_asset: dict | None = None,
) -> tuple[list[dict], dict]:
    """
    三维交叉验证评分

    Args:
        keywords: 关键词列表
        industry: 行业（供LLM参考）
        target_share: 目标占比
        cached_metaso: 缓存的秘塔数据
        concurrency: 秘塔并发数

    Returns:
        (scored_keywords, metaso_cache)
        scored_keywords: 每个词的完整评分和定价数据
        metaso_cache: 秘塔数据缓存（供前端回传复用）
    """
    from tools.transparent_pricing import get_cost_per_article, get_cost_config

    print(f"  [1/3] 查询5118数据（{len(keywords)}个词）...")
    print(f"  [2/3] 查询秘塔竞争度{'（使用缓存）' if cached_metaso else ''}...")
    print(f"  [3/3] LLM意图分类...")

    # 先获取5118数据，判断是否需要LLM估算兜底
    five118_task = fetch_5118_batch(keywords)
    metaso_task = fetch_metaso_batch(keywords, concurrency=concurrency, cached=cached_metaso)

    five118_data, metaso_data = await asyncio.gather(five118_task, metaso_task)

    # [工单 2026-07-26 P1-6 · 定价输入治理] 秘塔兜底 + 5118 零需求 = 这个词的竞争度
    #   完全是文本启发式猜的。此时禁止"商业词模式"把竞品数从 5 抬到 10/15
    #   (那条路径正是"越废越贵"的来源)。只下调不上调,且**不动任何定价公式**。
    downweighted_keywords: set[str] = set()
    for kw in keywords:
        ms = metaso_data.get(kw) or {}
        if str(ms.get("source") or "") != "fallback":
            continue
        f5 = five118_data.get(kw) or {}
        demand = (
            float(f5.get("search_volume") or 0)
            + float(f5.get("sem_price") or 0)
            + float(f5.get("bidword_company_count") or 0)
        )
        if demand > 0:
            continue
        capped = estimate_competition_from_keyword(kw, demand_signal=0)
        current = int(ms.get("effective_competition", ms.get("competition_count", capped)) or capped)
        if capped < current:
            ms = dict(ms)
            ms["competition_count"] = capped
            ms["effective_competition"] = capped
            ms["competition_downweighted"] = True
            metaso_data[kw] = ms
            downweighted_keywords.add(kw)
    if downweighted_keywords:
        print(
            f"  [定价输入治理] {len(downweighted_keywords)} 个无真实需求数据的词"
            f"竞争度降权(禁止凭编造竞争度定高价)"
        )

    # 检测5118数据是否有效：如果大部分关键词的三个核心指标全为0，启用LLM估算兜底
    valid_5118_count = sum(
        1 for kw in keywords
        if five118_data.get(kw, {}).get("search_volume", 0) > 0
        or five118_data.get(kw, {}).get("sem_price", 0) > 0
        or five118_data.get(kw, {}).get("bidword_company_count", 0) > 0
    )
    need_market_estimate = valid_5118_count < len(keywords) * 0.3  # 超过70%的词无5118数据
    if need_market_estimate:
        print(f"  ⚠️ 5118数据覆盖率低（{valid_5118_count}/{len(keywords)}），启用LLM市场估算兜底")

    llm_data = await classify_keywords_with_llm(keywords, industry, need_market_estimate=need_market_estimate)

    # 当5118无数据时，用LLM估算值回填（仅用于展示，不参与定价计算）
    if need_market_estimate:
        for kw in keywords:
            f5 = five118_data.get(kw, {})
            llm = llm_data.get(kw, {})
            if f5.get("search_volume", 0) == 0 and f5.get("sem_price", 0) == 0 and f5.get("bidword_company_count", 0) == 0:
                # 仅回填展示字段，定价相关字段保持0（与5118无数据时一致）
                f5["search_volume"] = llm.get("estimated_search_volume", 50)  # 展示用
                # sem_price 和 bidword_company_count 保持0，不影响定价
                f5["source"] = "llm_estimate"
                five118_data[kw] = f5

    # [v2.1 2026-06-11 · 成本驱动 LLM 评估师(老板拍商业逻辑)]
    #   客户价 = 篇数 × 单篇成本 × 服务商系数
    #   LLM 不猜价 · 只判物理量(真实竞争量级 + 投放媒体档次)· 价格由 pricing_bands.compute_v2_tier_price(SSOT)算
    #   双 LLM(deepseek-v4-flash + qwen3.6-flash)批量 8 词/批并发 · factory std 偏差 ≥15% → needs_review
    #   成本优先级:报价方案自设(固定原样生效) > max(系统动态, LLM 媒体档次建议) > 默认 ¥60
    from tools.pricing_llm_assessor import assess_keywords_pricing_batch, CURRENT_ASSESSOR_VERSION

    config = get_cost_config()
    markup_ratio = config["markup_ratio"]
    default_cost = get_cost_per_article()

    print(f"  数据采集完成 · v2.1 成本驱动 LLM 评估师({len(keywords)} 词 · 双 LLM 批量互验)...")

    # [P0-D 2026-06-14 · Codex#1 返修] trust_asset 由【上游 generate_*_quote 按 brand_id 取好】传入
    #   (绝不在此按 brand_name 猜 · 同名品牌串快照风险)· flag 关 / 无 brand_id → caller 传 None/missing。
    assessment_map = await assess_keywords_pricing_batch(
        keywords=keywords,
        brand_industry=industry,
        brand_name=brand_name,
        five118_data=five118_data,
        metaso_data=metaso_data,
        markup=markup_ratio,
        cost_per_article_override=cost_per_article_override,
        default_city=city,
        cost_multiplier=cost_multiplier,  # 进货倍率(扫码下级=上级 SKU 系数 · 自动估成本乘)
        trust_asset=trust_asset,          # [P0-D] 品牌级信任资产(flag 关 None · 报价 0 变化)
    )

    scored = []
    for kw in keywords:
        f5 = five118_data.get(kw, {})
        ms = metaso_data.get(kw, {})
        llm = llm_data.get(kw, {})
        a = assessment_map.get(kw) or {}

        keyword_type = a.get("keyword_type", "national_niche")
        cost_per_article = float(a.get("cost_per_article", default_cost))  # assessor 已解析最终成本
        true_comp = int(a.get("true_competition", 1))
        # [P1 容量合同 2026-08-08] 原写死的 7 是主合同 standard 档的**逐字拷贝**(改 SSOT 它不跟)
        required_articles = int(a.get("standard_articles") or tier_article_capacity("standard"))
        kw_needs_review = bool(a.get("needs_review", False))
        risk_flags = a.get("risk_flags", []) or []
        guards = a.get("guards", {}) or {}
        baseline = a.get("industry_baseline", {}) or {}

        # 老字段 difficulty/value 兼容映射(v2 不进价 · 纯展示/复盘:价 = 篇数 × 成本)
        diff_compat = round(0.85 + min(true_comp, 100) / 100 * 1.15, 2)
        value_compat = round(float(a.get("value_signal", 1.0)), 2)

        market_scope = a.get("city_tier") if a.get("city_tier") in (
            "tier1", "new_tier1", "tier2", "tier3", "tier4", "county", "township"
        ) else "national"

        # v2 扩展数据(单一结构 · 进 cache JSONB · recalculate_for_tier 读 true_competition 算 strong 档)
        v2_assessor_data = {
            "assessor_version": a.get("assessor_version", CURRENT_ASSESSOR_VERSION),
            "true_competition": true_comp,
            "measured_competition": int(a.get("measured_competition", 1)),
            "saturated_recall": bool(a.get("saturated_recall", False)),
            "media_tier_required": a.get("media_tier_required", "C"),
            "cost_per_article": round(cost_per_article, 1),
            "cost_multiplier": float(cost_multiplier or 1.0),  # 进货倍率随底盘落库(复盘"为何这个成本")
            "value_signal": value_compat,
            # [v2.2 P1 修] 生效价值乘数持久化(SSOT):recalc 优先读它 · 不再从 signal 反推
            #   (fallback 词 signal=1.0 但 vm=1.0 不溢价 · 反推会得 1.25 = 真实出价静默 +25% 漂价)
            "value_multiplier": float(a.get("value_multiplier", 1.0)),
            "industry_baseline": baseline,
            "llm": {
                "primary_ok": bool(a.get("llm_primary_ok", False)),
                "secondary_ok": bool(a.get("llm_secondary_ok", False)),
                "primary_std": int(a.get("llm_primary_std", 0)),
                "secondary_std": int(a.get("llm_secondary_std", 0)),
                "deviation_pct": float(a.get("llm_deviation_pct", 0.0)),
                "used": a.get("llm_used", "fallback"),
            },
            "risk_flags": risk_flags,
            "reasoning": a.get("reasoning", ""),
            "city": a.get("city"),
            "city_tier": a.get("city_tier", "national"),
            "guards": guards,
            # [v2.3 DELTA 3 返修 · Codex#1] ratio 观测进复盘记录(缓存 JSONB / 审计可查 · 不止影子脚本)。
            #   flags 全关时 assessor 恒返 None(compute_blowup_guards inert)→ 此处恒 None = 0 行为变化。
            "ratios": {
                "cost_ratio": a.get("cost_ratio"),
                "value_ratio": a.get("value_ratio"),
                "comp_ratio": a.get("comp_ratio"),
                "factory_ratio": a.get("factory_ratio"),
                # [P0-D] trust_ratio 第 4 观测 ratio(flag 关恒 None · 难度因子篇数侧)
                "trust_ratio": a.get("trust_ratio"),
            },
            # [P0-D 2026-06-14] 信任资产复盘字段(flag 关 / 无快照 → 恒 None/默认 = 0 行为变化)。
            "trust": {
                "source": a.get("trust_asset_source"),
                "trust_asset_score": a.get("trust_asset_score"),
                "citation_readiness_score": a.get("citation_readiness_score"),
                "trust_asset_needs_review": bool(a.get("trust_asset_needs_review", False)),
                "verified_labels": a.get("trust_verified_labels") or [],
                "missing_labels": a.get("trust_missing_labels") or [],
            },
        }

        scored.append({
            "keyword": kw,
            # [2026-06-11 老板拍 · 真实第一] 数据断供词标记(双 LLM 全挂/metaso 全挂)
            #   batch_pricing 据此从报价剥离 + 不写缓存;评估整体缺失(a 空)同样视为断供
            "price_unavailable": bool(a.get("price_unavailable", False)) or not a,
            "unavailable_reason": a.get("unavailable_reason") if a else "llm_unavailable",
            # 定价(标准档为代表 · selling = 出厂 × markup 末位 · markup 不进缓存)
            "required_articles": required_articles,
            "selling_price": int(a.get("selling_standard", 0)),
            "cost_per_article": round(cost_per_article, 1),
            "total_cost": round(required_articles * cost_per_article, 1),
            "markup_ratio": markup_ratio,
            # ===== 三档价字段(score_keywords 输出时 = 出厂层 markup=1)=====
            #   ⚠️ 此字段会被 _enrich_keywords_with_tier_prices 覆写成客户层(× markup)后才入 cache
            #   → cache 三列 = 默认 markup 客户层快照(展示用 · 跟 v1.3 语义一致)
            #   → recalculate_for_tier 不读此字段 · 一律从 v2_assessor_data 底盘 SSOT 现算(防双重 markup)
            "entry_price": float(a.get("entry_price", 0)),
            "standard_price": float(a.get("standard_price", 0)),
            "flagship_price": float(a.get("flagship_price", 0)),
            # [P1 容量合同 2026-08-08] 原写死的 5/7/10 = V2_TIER_MIN_ARTICLES 的第五份拷贝。
            #   走唯一取数出口后,主合同一改这里自动跟上(数值当前一字未变 = 零行为变化)。
            "entry_articles": int(a.get("entry_articles") or tier_article_capacity("entry")),
            "standard_articles": int(a.get("standard_articles") or tier_article_capacity("standard")),
            "flagship_articles": int(a.get("flagship_articles") or tier_article_capacity("flagship")),
            # 三维评分(老字段 · v2 纯展示不进价)
            "difficulty_score": diff_compat,
            "value_score": value_compat,
            # 地域 + keyword_type(LLM 判定)
            "is_broad": keyword_type in ("national_niche", "national_head"),
            "geo_multiplier": 1.0,   # v2 地理已内化进竞争/成本 · 不再连乘 · 落 1.0 表"未使用"
            "keyword_type": keyword_type,
            "market_scope": market_scope,
            "is_brand_keyword": keyword_type == "brand_owned",
            "geo_level": market_scope,
            "classify_confidence": float(a.get("confidence", 0.5)),
            "classify_reason": a.get("reasoning", "")[:200],
            "classify_source": "llm_assessor_v2",
            # 复盘字段(老列兼容:band_min/max 落 entry/flagship 出厂层)
            "competition_band": 0,
            "raw_price_before_band": float(a.get("standard_price", 0)),
            "band_min": float(a.get("entry_price", 0)),
            "band_max": float(a.get("flagship_price", 0)),
            "needs_review": kw_needs_review,
            # [完整修复 2026-06-13 High1] 透传爆价护栏布尔(Stage3 assessor 已产出 · 之前在此 scored 行掉了):
            #   guarantee_unavailable → 下游剥保证价(不进套餐总价 · 人工核);blowup_no_cache → 不写共享缓存。
            #   三 flag 全关时 assessor 恒返 False(compute_blowup_guards inert)→ 此处恒 False = 0 行为变化。
            "guarantee_unavailable": bool(a.get("guarantee_unavailable", False)),
            "blowup_no_cache": bool(a.get("blowup_no_cache", False)),
            # [收口 2026-06-15 Codex 返修] P0-A 非 db 快照/无数据回落成本 → _cacheable_rows 据此排除不写共享缓存。
            #   flag 全关时 assessor 恒 False = 0 行为变化。
            "cost_snapshot_uncacheable": bool(a.get("cost_snapshot_uncacheable", False)),
            # [v2.3 DELTA 3 返修 · Codex#1] ratio 观测落 scored 行(代理端/审计可见 · flags 全关恒 None)。
            #   selection_api._CUSTOMER_INTERNAL_KW_FIELDS 已含这 4 字段 → 客户面出口脱敏剥除(§10.3)。
            "cost_ratio": a.get("cost_ratio"),
            "value_ratio": a.get("value_ratio"),
            "comp_ratio": a.get("comp_ratio"),
            "factory_ratio": a.get("factory_ratio"),
            # [P0-D 2026-06-14] 信任资产落 scored 行(代理端/审计/影子可见 · flags 关恒 None/默认)。
            #   内部字段 → selection_api._CUSTOMER_INTERNAL_KW_FIELDS 客户面脱敏;labels = 人话可见。
            "trust_asset_source": a.get("trust_asset_source"),
            "trust_asset_score": a.get("trust_asset_score"),
            "citation_readiness_score": a.get("citation_readiness_score"),
            "trust_asset_needs_review": bool(a.get("trust_asset_needs_review", False)),
            "trust_ratio": a.get("trust_ratio"),
            "trust_verified_labels": a.get("trust_verified_labels") or [],
            "trust_missing_labels": a.get("trust_missing_labels") or [],
            "pricing_formula_version": a.get("assessor_version", CURRENT_ASSESSOR_VERSION),
            # 5118 原始数据
            "search_volume": f5.get("search_volume", 0),
            "sem_price": f5.get("sem_price", 0),
            "bidword_company_count": f5.get("bidword_company_count", 0),
            # 秘塔原始数据
            "competitor_count": ms.get("competition_count", 1),
            "effective_competition": ms.get("effective_competition", ms.get("competition_count", 1)),
            "content_count": ms.get("content_count", 0),
            # 超红海(assessor 内 detect_super_red_ocean 已判)
            "super_red_ocean": "super_red_ocean" in risk_flags,
            "super_red_ocean_level": guards.get("super_red_ocean", "none"),
            "competition_ratio": round(
                min(1.0, (ms.get("competition_count", 0) or 0) / max(ms.get("content_count", 1), 1)), 3
            ),
            "recommended_platforms": ms.get("top_platforms", []),
            "source_authority": ms.get("source_authority", {}),
            # 意图(LLM 意图分类原样落库给前端)
            "intent": llm.get("intent", "informational"),
            "funnel_stage": llm.get("funnel_stage", "awareness"),
            "search_probability": llm.get("search_probability", 0.5),
            # v2 扩展数据(单一结构 · save 落 JSONB · cache 读回还原)
            "v2_assessor_data": v2_assessor_data,
            # 数据来源标记
            "data_source": f5.get("source", "5118"),
            # [工单 P1-6] 定价输入可信度(measured / estimated / unavailable)+ 降权标记
            "pricing_input_quality": (
                "unavailable" if str(ms.get("source") or "") == "fallback"
                else ("estimated" if f5.get("source") == "llm_estimate" else "measured")
            ),
            "competition_downweighted": bool(ms.get("competition_downweighted")),
        })

    # [工单 P1-6] 地域价格倒挂自检:下级地域词(区/街道)比上级(城市)还贵 → 强制人工审。
    #   只加 needs_review 标记,**不改任何价格**(定价公式是红线)。
    try:
        from tools.keyword_price_inversion import annotate_price_inversions
        from services.quote_scope_lock import parse_region_hierarchy

        _hier = parse_region_hierarchy(city or "")
        inversions = annotate_price_inversions(
            scored,
            cities=_hier["cities"],
            sub_regions=_hier["sub_regions"],
            streets=_hier.get("streets") or [],
        )
        if inversions:
            print(f"  ⚠️ [价格倒挂自检] {len(inversions)} 个下级地域词价格高于上级 · 已标 needs_review")
            for item in inversions[:5]:
                print(f"      {item['keyword']} ¥{item['price']:.0f} > {item['compared_keyword']} ¥{item['compared_price']:.0f}")
    except Exception as _inv_err:  # pragma: no cover - 自检异常绝不阻断报价
        print(f"  [价格倒挂自检] 跳过(异常): {_inv_err}")

    # 构建秘塔缓存（供前端回传）
    metaso_cache = {kw: metaso_data[kw] for kw in keywords if kw in metaso_data}

    return scored, metaso_cache
