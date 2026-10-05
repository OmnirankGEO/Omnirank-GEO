"""
关键词主题包聚类引擎

将平铺的关键词列表聚类为 {业务 × 城市 × 场景} 主题包，
并在每个包内区分核心监控词和附赠覆盖词。

输入: keyword_value_scorer.score_keywords() 的评分结果列表
输出: 主题包列表，含核心词/附赠词/变体词/三档定价汇总

也提供 extract_business_lines() 用于从行业信息中提取业务线列表。
"""

import json
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH
import re

# [P1 容量合同 2026-08-08] 篇数唯一取数出口
from tools.pricing_bands import (
    LEGACY_MISSING_CAPACITY_DEFAULT as _CAPACITY_MISSING_DEFAULT,
    normalize_article_capacity as _normalize_capacity_ssot,
)


def _normalize_article_capacity(raw) -> int:
    """聚类快照里的容量上限透传:显式 0(覆盖词)保留 · 缺失走具名兜底常量。"""
    return _normalize_capacity_ssot(raw, when_missing=_CAPACITY_MISSING_DEFAULT)


# ========================================
# 常量
# ========================================

# 核心词选取上限（每包）— 宁多勿少，让客户自己取舍
MAX_CORE_KEYWORDS = 10
MIN_CORE_KEYWORDS = 1

# 语义去重：同组内相似度阈值
DEDUP_SIMILARITY_THRESHOLD = 0.85

# core_score 中的 intent 权重
# 注意: informational 从 0.3 提升到 0.6 — 配合 intent override 修复后，
# 仍被标记为 informational 的词确实价值较低，但 0.3 过于惩罚
INTENT_WEIGHT = {
    "transactional": 1.5,
    "commercial": 1.2,
    "informational": 0.6,
    "navigational": 0.5,
}

# core_score 中的搜索量因子
def _volume_factor(search_volume: int) -> float:
    if search_volume >= 200:
        return 1.0
    if search_volume >= 50:
        return 0.6
    return 0.2

# 变体词后缀模板
VARIANT_SUFFIXES = {
    "找服务商": ["哪家好", "哪家靠谱", "推荐", "排名", "排行榜", "公司推荐", "十大品牌"],
    "问价格": ["多少钱", "价格", "收费标准", "费用", "报价"],
    "做决策": ["怎么选", "注意事项", "避坑指南", "优缺点", "对比"],
    "找渠道": ["哪里有", "去哪里", "哪个平台", "附近"],
}


# ========================================
# Step 1: LLM 语义聚类（直接分组，不再逐词打标签）
# ========================================

CLUSTER_GROUP_PROMPT = """你是一个关键词聚类专家。请将以下{count}个关键词按**用户真实搜索需求**分组。

行业背景: {industry}
品牌: {brand_name}
{city_constraint}

关键词列表:
{keywords}

## 核心原则: 按「客户真实需求」分组,不是按「措辞差异」分组

### 必须 SPLIT 的硬规则(任意一条命中即拆,不许合并!)
1. **不同城市** — 深圳 vs 东莞 vs 惠州 是不同市场,必须拆(本地竞争对手不同)
2. **不同产品规格/子品类** — 10吨叉车 vs 3吨叉车 / 工业 PCB vs 消费 PCB / 进口红酒 vs 国产红酒
3. **不同服务环节** — 安装 vs 维保 vs 加装(完全不同业务流)
4. **不同客户群体** — 企业 vs 个人 / B2B vs B2C
5. **不同教学/服务模式** — 1对1 vs 小班 vs 网课
6. **任何客户在签约前会问"你们是哪种"的差异**

### 可以 MERGE 的情况
- 同城市 + 同产品 + 不同问法("哪家好" / "哪家靠谱" / "推荐" / "排名" / "公司推荐")
- 仅修饰词差异("高端" / "专业" / "靠谱的" 同 + 同一服务)
- "深圳福田XX" + "深圳南山XX" + "深圳XX" 同城子区域可合并

### 包名要求
- 必须明确体现 city + 产品/服务类型,如「深圳别墅电梯询价」
- ❌ 跨城禁止用省名简化("广东"、"江苏" 都是错的)
- ❌ 包名不带地名(全国类除外)

请严格按JSON格式返回,不要任何其他文字:
{{
  "groups": [
    {{"name": "包名", "keywords": ["关键词1", "关键词2"]}}
  ]
}}"""


# ========================================
# Step 1a: 城市硬桶(代码层 · Layer 1 防御)
# 在调 LLM 之前先按城市硬分组,根本杜绝跨城误合并
# ========================================

# 与 keyword_expander.CITY_TO_PROVINCE 同步 · 单数据源不行因为循环导入,这里是显式克隆
_KNOWN_CITIES = [
    # 直辖市
    "北京", "上海", "天津", "重庆",
    # 广东
    "广州", "深圳", "东莞", "佛山", "珠海", "中山", "惠州", "汕头", "湛江",
    # 长三角
    "杭州", "宁波", "温州", "绍兴", "嘉兴", "台州", "金华", "湖州",
    "南京", "苏州", "无锡", "常州", "徐州", "南通", "盐城", "扬州", "镇江",
    # 北方
    "济南", "青岛", "烟台", "潍坊", "临沂", "济宁", "威海",
    "石家庄", "唐山", "保定", "邯郸", "秦皇岛",
    "郑州", "洛阳", "开封", "新乡", "南阳",
    "西安", "太原", "兰州", "银川",
    "沈阳", "大连", "长春", "哈尔滨",
    # 中部
    "武汉", "襄阳", "宜昌",
    "长沙", "株洲", "湘潭", "衡阳",
    "合肥", "芜湖", "蚌埠",
    "南昌", "赣州", "九江",
    # 西南
    "成都", "绵阳", "德阳", "宜宾",
    "昆明", "曲靖", "大理",
    "贵阳", "遵义",
    # 福建
    "福州", "厦门", "泉州", "漳州",
    # 其他
    "南宁", "桂林", "柳州", "海口", "三亚",
    "呼和浩特", "乌鲁木齐",
]


def _detect_city(keyword: str) -> str:
    """从关键词中提取城市名 · 返回城市或空字符串"""
    for city in _KNOWN_CITIES:
        if city in keyword:
            return city
    return ""


def _split_by_city(keywords: list[str]) -> dict[str, list[str]]:
    """
    城市硬桶 · 按关键词中的城市名分组

    Returns:
        dict[city_or_'__nocity__', list[keyword]]

    城市边界由代码保证 · 100% 不会跨城合并
    """
    buckets: dict[str, list[str]] = {}
    for kw in keywords:
        city = _detect_city(kw) or "__nocity__"
        buckets.setdefault(city, []).append(kw)
    return buckets


async def _cluster_keywords_with_llm(
    keywords: list[str],
    brand_name: str,
    industry: str,
    city_hint: str = "",
) -> list[dict]:
    """
    用 LLM 对关键词做语义聚类 · 走 deepseek-v4-flash 直连 · thinking=OFF

    Args:
        keywords: 关键词列表(已经过城市硬桶,同一桶内同城市)
        brand_name: 品牌名
        industry: 行业
        city_hint: 当前桶的城市名(用于 prompt 强约束)

    Returns:
        [{"name": "包名", "keywords": ["词1", "词2"]}, ...]
    """
    import os
    import httpx

    if not keywords:
        return []

    # 单词直接成包,不调 LLM
    if len(keywords) == 1:
        return [{"name": keywords[0][:10], "keywords": list(keywords)}]

    from services.llm.deepseek_key_pool import has_deepseek_key, adeepseek_post_with_failover
    kw_list_str = "\n".join(f"{i + 1}. {kw}" for i, kw in enumerate(keywords))

    city_constraint = ""
    if city_hint and city_hint != "__nocity__":
        city_constraint = (
            f"\n## ⚠️ 当前批次只允许 **{city_hint}** 一个城市\n"
            f"包名必须以「{city_hint}」开头 · 不许出现其他城市/省份名"
        )

    prompt = CLUSTER_GROUP_PROMPT.format(
        count=len(keywords),
        industry=industry,
        brand_name=brand_name,
        city_constraint=city_constraint,
        keywords=kw_list_str,
    )

    response = ""
    if has_deepseek_key():
        try:
            # [failover 2026-06-11] 多 key 失败自动换下一个重试(单 key=直调·向后兼容)·内含 llm_track
            resp = await adeepseek_post_with_failover(
                {
                    "model": DEEPSEEK_OFFICIAL_FLASH,
                    "messages": [{"role": "user", "content": prompt}],
                    # [CTO-15.23 2026-05-11 Bug E] temperature 0.2 → 0.1 同品牌算价稳定
                    "temperature": 0.1,
                    "max_tokens": 2000,
                    "stream": False,
                    "thinking": {"type": "disabled"},
                },
                track_name="keyword_cluster_grouping",
                track_model=DEEPSEEK_OFFICIAL_FLASH,
            )
            response = resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"  V4-Flash 调用失败 {e} → fallback")

    # Fallback: multi_llm_caller(已升级到 V4-Flash + 多源后备)
    if not response:
        try:
            from tools.multi_llm_caller import call_llm_with_fallback
            response = await call_llm_with_fallback(prompt, verbose=False)
        except Exception as e:
            print(f"  Fallback 也失败 {e} · 单包兜底")
            return [{"name": city_hint or "综合优化", "keywords": list(keywords)}]

    try:
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        if not json_match:
            raise ValueError("LLM 返回中未找到 JSON 对象")

        result = json.loads(json_match.group())
        groups = result.get("groups", [])

        # 验证: 确保每个 group 有 name 和 keywords
        valid_groups = []
        all_assigned = set()
        for g in groups:
            name = g.get("name", "")
            kw_list = g.get("keywords", [])
            if not name or not kw_list:
                continue
            # 模糊匹配 LLM 返回的关键词到原始列表
            matched = []
            for kw in kw_list:
                m = _fuzzy_match_keyword(kw, keywords)
                if m in keywords and m not in all_assigned:
                    matched.append(m)
                    all_assigned.add(m)
            if matched:
                valid_groups.append({"name": name, "keywords": matched})

        # 处理未被分配的关键词
        unassigned = [kw for kw in keywords if kw not in all_assigned]
        if unassigned:
            if valid_groups:
                largest = max(valid_groups, key=lambda g: len(g["keywords"]))
                largest["keywords"].extend(unassigned)
                print(f"  [聚类] {len(unassigned)} 个未分配词归入「{largest['name']}」")
            else:
                valid_groups.append({"name": city_hint or "综合优化", "keywords": unassigned})

        return valid_groups

    except Exception as e:
        print(f"  LLM 聚类解析失败: {e},使用单包兜底")
        return [{"name": city_hint or "综合优化", "keywords": list(keywords)}]


def _merge_small_groups(groups: list[dict], min_size: int = 3) -> list[dict]:
    """合并过小的碎片包（< min_size 个词）到名称最接近的大包"""
    if len(groups) <= 1:
        return groups

    big = [g for g in groups if len(g["keywords"]) >= min_size]
    small = [g for g in groups if len(g["keywords"]) < min_size]

    if not small:
        return groups
    if not big:
        # 所有包都太小，合并为一个
        all_kws = []
        for g in groups:
            all_kws.extend(g["keywords"])
        return [{"name": groups[0]["name"], "keywords": all_kws}]

    for sg in small:
        # 找名称最相关的大包（简单字符重叠）
        best_match = max(big, key=lambda bg: len(set(bg["name"]) & set(sg["name"])))
        best_match["keywords"].extend(sg["keywords"])
        print(f"  [聚类] 碎片包「{sg['name']}」({len(sg['keywords'])}词) 并入「{best_match['name']}」")

    return big


def _fuzzy_match_keyword(llm_kw: str, original_keywords: list[str]) -> str:
    """LLM 返回的关键词文本可能和原始略有不同，做模糊匹配"""
    if llm_kw in original_keywords:
        return llm_kw
    for original in original_keywords:
        if llm_kw in original or original in llm_kw:
            return original
    return llm_kw



# ========================================
# Step 2: 将 LLM 聚类结果与评分数据关联
# ========================================

def _build_groups_from_llm_clusters(
    llm_groups: list[dict],
    scored_keywords: list[dict],
) -> list[tuple[str, list[dict]]]:
    """
    将 LLM 聚类结果与 scored_keywords 数据关联。
    返回 [(group_name, [scored_keyword_dicts])]
    """
    kw_map = {kw["keyword"]: kw for kw in scored_keywords}
    result = []
    for group in llm_groups:
        name = group["name"]
        group_kws = []
        for kw_text in group["keywords"]:
            if kw_text in kw_map:
                group_kws.append(kw_map[kw_text])
        if group_kws:
            result.append((name, group_kws))
    return result


# ========================================
# Step 3: 组内语义去重
# ========================================

DEDUP_PROMPT = """以下关键词属于同一个主题组（{group_desc}）。请判断哪些词是"同义重复"——用户需求完全一样，只是问法不同。

关键词:
{keywords}

判定规则：
- "公司推荐"和"哪家好"和"排名"是同义（都是找服务商）
- "公司推荐"和"价格/多少钱"不是同义（一个找公司，一个问价格）
- "公司推荐"和"配司机"不是同义（不同服务形态）

只需把【同义重复】的词分到同一组：语义完全一样、只是问法不同的归一组；语义不同的各自单独成组。
保留组内哪个词由系统按价值决定，你不用判断。

严格按JSON格式返回（groups 的每个子数组是一组同义词）:
{{"groups": [["词A", "词B"], ["词C"]]}}"""


async def _dedup_within_group(
    group_keywords: list[dict],
    group_desc: str,
) -> tuple[list[dict], list[dict]]:
    """
    组内语义去重。返回 (保留的词列表, 被去重的词列表)。
    组内词数 <= 2 时跳过 LLM，直接全部保留。
    """
    if len(group_keywords) <= 2:
        return group_keywords, []

    from tools.multi_llm_caller import call_llm_with_fallback

    kw_list_str = "\n".join(
        f"- {kw['keyword']}"
        for kw in group_keywords
    )

    prompt = DEDUP_PROMPT.format(
        group_desc=group_desc,
        keywords=kw_list_str,
    )

    # [2026-06-07 锚词=售价最高 · 决策1A] 同义组保留哪个 = 代码层确定性按 selling_price 降序选
    #   (并列按 required_articles 降序),LLM 只负责"哪些词同义"的语义分组,不再让 LLM 按搜索量挑。
    #   理由:锚词(被监测/计价的那个)应是同义簇里售价最贵 + 文章数最多的;便宜同义词归覆盖(免费/不监测)。
    def _keeper_rank(kw_data: dict) -> tuple:
        return (kw_data.get("selling_price", 0) or 0, kw_data.get("required_articles", 0) or 0)

    try:
        response = await call_llm_with_fallback(prompt, verbose=False)
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        if not json_match:
            return group_keywords, []

        result = json.loads(json_match.group())
        groups = result.get("groups")
        if not isinstance(groups, list) or not groups:
            # LLM 未给同义分组 → 安全兜底:全部保留(不误删/不误并)
            return group_keywords, []

        kept = []
        deduped = []
        used_ids = set()  # 已被某个同义组认领的 kw_data · 防一词进多组

        for grp in groups:
            if not isinstance(grp, list):
                continue
            members = []
            for name in grp:
                if not isinstance(name, str):
                    continue
                # 模糊匹配组员名 → kw_data(沿用原模糊匹配口径)
                for kw_data in group_keywords:
                    if id(kw_data) in used_ids:
                        continue
                    k = kw_data["keyword"]
                    if k == name or k in name or name in k:
                        members.append(kw_data)
                        used_ids.add(id(kw_data))
                        break
            if not members:
                continue
            # 同义组内:售价最高(并列 required_articles 最高)= 锚词 · 其余 → 覆盖(免费/不监测)
            members.sort(key=_keeper_rank, reverse=True)
            kept.append(members[0])
            deduped.extend(members[1:])

        # 未被任何同义组认领的词(各自独立 · 非同义重复)→ 全部保留
        for kw_data in group_keywords:
            if id(kw_data) not in used_ids:
                kept.append(kw_data)

        # 安全兜底:至少保留1个
        if not kept:
            kept = [group_keywords[0]]
            deduped = group_keywords[1:]

        return kept, deduped

    except Exception as e:
        print(f"  组内去重失败({group_desc}): {e}，保留全部")
        return group_keywords, []


# ========================================
# Step 4: 核心词选取
# ========================================

def _compute_core_score(kw_data: dict) -> float:
    """计算核心词评分，含 intent 兜底修正"""
    value_score = kw_data.get("value_score", 1.0)
    search_prob = kw_data.get("search_probability", 0.5)
    intent = kw_data.get("intent", "informational")
    search_volume = kw_data.get("search_volume", 0)
    keyword = kw_data.get("keyword", "")

    # 兜底: 如果 intent 仍然是 informational 但关键词明显含商业信号，升级权重
    if intent == "informational" and keyword:
        commercial_signals = ["推荐", "哪家好", "排名", "排行", "前十", "哪家靠谱",
                              "公司推荐", "品牌推荐", "哪里有", "找哪家", "哪家专业"]
        transactional_signals = ["多少钱", "价格", "报价", "费用", "一天多少", "怎么租"]
        if any(s in keyword for s in transactional_signals):
            intent = "transactional"
        elif any(s in keyword for s in commercial_signals):
            intent = "commercial"

    iw = INTENT_WEIGHT.get(intent, 0.5)
    vf = _volume_factor(search_volume)

    return value_score * search_prob * iw * vf


def _select_core_keywords(
    kept_keywords: list[dict],
    deduped_keywords: list[dict],
) -> tuple[list[dict], list[dict]]:
    """
    从保留的关键词中选出核心词，其余归为附赠词。
    被去重的词直接归附赠。
    """
    # 计算 core_score 并排序
    for kw in kept_keywords:
        kw["core_score"] = round(_compute_core_score(kw), 4)
    kept_sorted = sorted(kept_keywords, key=lambda x: x["core_score"], reverse=True)

    core = []
    covered_from_kept = []

    for kw in kept_sorted:
        if len(core) < MAX_CORE_KEYWORDS:
            core.append(kw)
        else:
            covered_from_kept.append(kw)

    # 至少要有1个核心词
    if not core and kept_sorted:
        core = [kept_sorted[0]]
        covered_from_kept = kept_sorted[1:]

    # [B2 2026-06-05] 相关搜索/覆盖词 = 「可能覆盖参考」(老板定稿口径)
    #   发已选核心词内容时【可能】顺带覆盖到这些相关搜索;不单独监测、不承诺达标、不计价
    #   想单独监测某相关词 → 升级为独立核心词单独计价
    covered = []
    for kw in deduped_keywords:
        covered.append({**kw, "merge_reason": "与核心词问法相近 · 可能顺带覆盖"})
    for kw in covered_from_kept:
        covered.append({**kw, "merge_reason": "相关搜索参考 · 可能顺带覆盖"})

    return core, covered


# ========================================
# Step 5: 变体词膨胀
# ========================================

def _extract_stem(keyword: str, city: str) -> str:
    """提取关键词主干（去掉城市名和常见后缀）"""
    stem = keyword
    if city and city != "全国":
        stem = stem.replace(city, "")

    # 去掉常见后缀（长后缀在前，避免短后缀先匹配导致残留）
    suffixes_to_strip = [
        # 长复合后缀
        "哪家价格实在", "哪家价格便宜", "哪家比较好", "哪家最靠谱",
        "公司推荐", "收费标准", "十大品牌", "排行榜", "避坑指南",
        "前十名", "注意事项",
        # 中等后缀
        "哪家好", "哪家靠谱", "推荐", "排名", "靠谱吗", "好不好",
        "多少钱", "价格", "费用", "报价",
        "怎么选", "如何选", "优缺点", "对比",
        "哪里有", "去哪里", "哪个平台", "附近",
        # 短尾
        "一天", "一次",
    ]
    # 可能有多层后缀，循环剥离
    changed = True
    while changed:
        changed = False
        for suffix in suffixes_to_strip:
            if stem.endswith(suffix):
                stem = stem[:-len(suffix)]
                changed = True
                break

    return stem.strip()


# 每个核心词最多生成的变体数（避免相似词干生成大量冗余）
MAX_VARIANTS_PER_CORE = 8
# 每个主题包最多展示的变体总数
MAX_VARIANTS_PER_CLUSTER = 30


def generate_display_variants(
    core_keywords: list[dict],
    existing_covered: list[str],
    city: str,
) -> tuple[list[dict], int]:
    """
    为核心词生成展示用变体词。

    返回 (变体词列表, 总变体数包括隐藏的)
    变体词不参与定价，不进入 confirmed_keywords，只用于前端展示。
    """
    existing_set = set(existing_covered)
    core_set = set(kw["keyword"] for kw in core_keywords)

    all_variants = []
    seen_stems = set()  # 去重相同词干
    seen = set()

    for kw_data in core_keywords:
        stem = _extract_stem(kw_data["keyword"], city)
        if not stem:
            continue

        # 跳过已生成过相同词干的变体（避免 "商务接待用车" 和 "商务接待用车出租" 产生重复变体）
        if stem in seen_stems:
            continue
        seen_stems.add(stem)

        prefix = f"{city}{stem}" if city and city != "全国" else stem
        per_core_count = 0

        for category, suffixes in VARIANT_SUFFIXES.items():
            for suffix in suffixes:
                if per_core_count >= MAX_VARIANTS_PER_CORE:
                    break
                variant = f"{prefix}{suffix}"
                if variant in existing_set or variant in core_set or variant in seen:
                    continue
                seen.add(variant)
                all_variants.append({
                    "keyword": variant,
                    "source": "generated_variant",
                    "upgradeable": False,
                    "variant_category": category,
                })
                per_core_count += 1

    total_count = len(all_variants)
    # 截断到每包上限
    display_variants = all_variants[:MAX_VARIANTS_PER_CLUSTER]
    return display_variants, total_count


# ========================================
# 主入口: cluster_keywords
# ========================================

async def cluster_keywords(
    scored_keywords: list[dict],
    brand_name: str,
    industry: str,
) -> dict:
    """
    主题包聚类引擎。

    Args:
        scored_keywords: keyword_value_scorer.score_keywords() 的输出列表，
            每个 dict 含 keyword, value_score, difficulty_score, search_volume,
            intent, search_probability, selling_price, required_articles 等
        brand_name: 品牌名称
        industry: 行业

    Returns:
        {
            "clusters": [...],           # 主题包列表
            "unclustered_keywords": [...],  # 无法归类的散词
            "stats": {                   # 统计信息
                "total_keywords": int,
                "cluster_count": int,
                "total_core": int,
                "total_covered": int,
                "total_variants": int,
            }
        }
    """
    if not scored_keywords:
        return {"clusters": [], "unclustered_keywords": [], "stats": {}}

    keywords_text = [kw["keyword"] for kw in scored_keywords]

    # ====================================================
    # [CTO-15.23 2026-05-05 P0 fix] 双层防御:跨城/跨子类误合并
    # Layer 1: 城市硬桶(代码层) — 100% 杜绝跨城合并
    # Layer 2: 守门员 LLM(cluster_gatekeeper) — 对每个候选包独立复核
    # ====================================================

    # Step 1a: 城市硬桶 · 把全量关键词按城市拆开,每桶独立聚类
    city_buckets = _split_by_city(keywords_text)
    print(f"  [聚类] Step 1a: 城市硬桶 → {len(city_buckets)} 桶: {list(city_buckets.keys())}")

    # Step 1b: 每桶单独调 LLM 聚类
    llm_groups = []
    for city, bucket_kws in city_buckets.items():
        print(f"  [聚类] Step 1b: 桶「{city}」({len(bucket_kws)}词) 调 V4-Flash...")
        sub_groups = await _cluster_keywords_with_llm(
            bucket_kws, brand_name, industry, city_hint=city
        )
        llm_groups.extend(sub_groups)
    print(f"  [聚类] Step 1b: LLM 返回 {len(llm_groups)} 个候选包")

    # Step 1c: 守门员复核 · 对每个候选包独立判定 merge/split
    print(f"  [聚类] Step 1c: 守门员 V4-Flash 复核 {len(llm_groups)} 个候选包...")
    from tools.cluster_gatekeeper import gatekeeper_check

    refined_groups = []
    for g in llm_groups:
        kws = g.get("keywords", [])
        name = g.get("name", "")
        if len(kws) <= 1:
            refined_groups.append(g)
            continue
        # 守门员判定
        sub_partitions = await gatekeeper_check(kws)
        if len(sub_partitions) == 1:
            # merge · 保留原包
            refined_groups.append(g)
        else:
            # split · 按守门员建议拆分,包名保留前缀 + 序号
            for idx, part in enumerate(sub_partitions, 1):
                refined_groups.append({
                    "name": f"{name}-{idx}" if len(sub_partitions) > 1 else name,
                    "keywords": part,
                })
            print(f"  [聚类] 守门员拆「{name}」: {len(kws)}词 → {len(sub_partitions)}组")

    print(f"  [聚类] Step 1c: 守门员后剩 {len(refined_groups)} 个最终包")

    # Step 2: 关联评分数据
    groups = _build_groups_from_llm_clusters(refined_groups, scored_keywords)

    # Step 3+4+5+6: 逐组处理
    clusters = []
    total_core = 0
    total_covered = 0
    total_variants = 0

    for cluster_name, group_kws in groups:
        # Step 3: 组内去重
        kept, deduped = await _dedup_within_group(group_kws, cluster_name)

        # Step 4: 核心词选取（价值最高的词作为价格锚）
        core, covered = _select_core_keywords(kept, deduped)

        # Step 5: 包命名（直接用 LLM 给的名称）
        # 从核心词中提取城市（用于变体词生成）
        city = ""
        for kw in core:
            kw_text = kw["keyword"]
            for c in ["北京", "上海", "广州", "深圳", "杭州", "成都", "重庆", "武汉",
                       "南京", "苏州", "天津", "西安", "长沙", "郑州", "东莞", "佛山",
                       "宁波", "青岛", "沈阳", "大连", "合肥", "厦门", "昆明", "福州"]:
                if c in kw_text:
                    city = c
                    break
            if city:
                break

        # Step 6: 变体词膨胀
        covered_keywords_text = [kw["keyword"] for kw in covered]
        variants, variant_count = generate_display_variants(
            core, covered_keywords_text, city
        )

        # 构建包数据
        core_data = []
        for kw in core:
            core_data.append({
                "keyword": kw["keyword"],
                "core_score": kw.get("core_score", 0),
                "search_volume": kw.get("search_volume", 0),
                "value_score": kw.get("value_score", 1.0),
                "difficulty_score": kw.get("difficulty_score", 1.0),
                "intent": kw.get("intent", "informational"),
                "search_probability": kw.get("search_probability", 0.5),
                "is_selected": True,
                "selling_price": kw.get("selling_price", 0),
                "required_articles": _normalize_article_capacity(kw.get("required_articles")),
                "effective_competition": kw.get("effective_competition", 1),
                "geo_multiplier": kw.get("geo_multiplier", 1.0),
                "is_broad": kw.get("is_broad", False),
                "cost_per_article": kw.get("cost_per_article", 60),
            })

        covered_data = []
        for kw in covered:
            covered_data.append({
                "keyword": kw["keyword"],
                "source": "expanded",
                "merge_reason": kw.get("merge_reason", ""),
                "upgradeable": True,
                "selling_price": kw.get("selling_price", 0),
                "required_articles": _normalize_article_capacity(kw.get("required_articles")),
                "value_score": kw.get("value_score", 1.0),
                "difficulty_score": kw.get("difficulty_score", 1.0),
                "search_volume": kw.get("search_volume", 0),
                "intent": kw.get("intent", "informational"),
                "effective_competition": kw.get("effective_competition", 1),
                "geo_multiplier": kw.get("geo_multiplier", 1.0),
                "is_broad": kw.get("is_broad", False),
                "cost_per_article": kw.get("cost_per_article", 60),
            })

        # 追加变体词到附赠列表
        covered_data.extend(variants)

        cluster = {
            "cluster_name": cluster_name,
            "business_tag": cluster_name,
            "city_tag": city or "全国",
            "scenario_tag": "通用",
            "description": f"覆盖{cluster_name}相关的AI搜索",
            "core_keywords": core_data,
            "covered_keywords": covered_data,
            "variant_count": variant_count,
            "core_keyword_count": len(core_data),
            "covered_keyword_count": len(covered),
            "is_selected": True,
        }

        clusters.append(cluster)
        total_core += len(core_data)
        total_covered += len(covered)
        total_variants += variant_count

    # 按核心词总价值降序排列
    clusters.sort(
        key=lambda c: sum(kw.get("selling_price", 0) for kw in c["core_keywords"]),
        reverse=True,
    )

    # ========================================
    # [CTO-15.23 2026-05-11 Bug D] 聚类后处理 · 防散词
    # 老板报"很多词条单独成 cluster · 合并的词不一定准确"
    # 规则:
    # 1. <3 词的小包合并到价值最高的大包(同类合并)· 不超过 6 包
    # 2. >6 包 时把价值最低的小包合并到第 5 大包
    # 3. 单核心词的"散包"全部合并到主包
    # ========================================
    MIN_CORE_PER_CLUSTER = 3   # 至少 3 核心词才独立成包
    MAX_CLUSTERS = 6            # 最多 6 个包
    if len(clusters) > 1:
        # 找最大包作为合并目标
        def cluster_total_price(c):
            return sum(kw.get("selling_price", 0) for kw in c.get("core_keywords", []))

        # 第一轮:小包(<MIN_CORE_PER_CLUSTER)合并到最大包
        keepers = []
        merge_to_main = []
        clusters_sorted = sorted(clusters, key=cluster_total_price, reverse=True)
        for idx, c in enumerate(clusters_sorted):
            core_count = len(c.get("core_keywords", []))
            if core_count >= MIN_CORE_PER_CLUSTER:
                keepers.append(c)
            else:
                merge_to_main.append(c)
                print(f"  [聚类后处理] 小包 '{c.get('cluster_name')}' 仅 {core_count} 核心词 → 合并到主包")

        if keepers and merge_to_main:
            main_pack = keepers[0]
            for small in merge_to_main:
                main_pack.setdefault("core_keywords", []).extend(small.get("core_keywords", []))
                main_pack.setdefault("covered_keywords", []).extend(small.get("covered_keywords", []))
                main_pack.setdefault("display_keywords", []).extend(small.get("display_keywords", []))
            clusters = keepers
        elif keepers:
            clusters = keepers
        # 全部都是小包(没 keepers)就保留原始 · 避免空包

        # 第二轮:超出 MAX_CLUSTERS 时把末尾合并到第 MAX_CLUSTERS-1 包
        if len(clusters) > MAX_CLUSTERS:
            clusters_sorted = sorted(clusters, key=cluster_total_price, reverse=True)
            keep = clusters_sorted[:MAX_CLUSTERS - 1]
            tail = clusters_sorted[MAX_CLUSTERS - 1:]
            # tail 全部合并到 keep[-1]
            target = keep[-1]
            for t in tail:
                target.setdefault("core_keywords", []).extend(t.get("core_keywords", []))
                target.setdefault("covered_keywords", []).extend(t.get("covered_keywords", []))
                target.setdefault("display_keywords", []).extend(t.get("display_keywords", []))
                print(f"  [聚类后处理] 包数超 {MAX_CLUSTERS} · 末尾包 '{t.get('cluster_name')}' 合并到 '{target.get('cluster_name')}'")
            clusters = keep

        # 重新排序最终结果
        clusters.sort(key=cluster_total_price, reverse=True)

    print(f"  [聚类] 完成: {len(clusters)} 个主题包, {total_core} 个核心词, "
          f"{total_covered} 个附赠词, {total_variants} 个变体词")

    return {
        "clusters": clusters,
        "unclustered_keywords": [],
        "stats": {
            "total_keywords": len(scored_keywords),
            "cluster_count": len(clusters),
            "total_core": total_core,
            "total_covered": total_covered,
            "total_variants": total_variants,
        },
    }


# ========================================
# 业务线提取（用于选词页第一步）
# ========================================

BUSINESS_LINE_PROMPT = """你是一个行业业务分析专家。请根据以下客户信息,提取该客户对外提供的**独立业务线/服务类型**。

## 客户信息
- 品牌: {brand_name}
- 行业: {industry}
- 城市: {city}
- 核心业务词(代理输入,必须 100% 覆盖): {core_keywords}
- 业务范围: {business_scope}

## ⚠️ 铁律 (违反即错!)
1. **核心业务词中明确出现的所有产品/服务名词必须 100% 列在 business_lines 中**
   - 如核心词含"别墅电梯/观光电梯/载货电梯",输出必须有 3 个对应业务线,不许遗漏任何一个
   - 即使你认为某产品"次要" / "可合并",也不许省略 — 代理输入说明客户做这块业务
   - 不许把不同产品换成你认为"客户可能也做"的扩展业务(如把"别墅电梯"换成"维保")
2. **业务线 = 客户可以独立售卖/报价的一项服务** — 不是关键词,但与核心词产品类型对齐
3. **地区/区县/街道/推荐/价格/案例不是业务线** — 只能作为 example_scenarios 放进对应业务线
   - "深圳福田区全屋定制哪家靠谱" 应归入 "全屋定制/全屋定制设计",绝不能新建 "福田区全屋定制"
   - "罗湖区榻榻米定制推荐" 如属于全屋定制服务范围,应放入全屋定制业务的 example_scenarios
4. 每业务线一个简洁中文名(3-8 字) + 一句话描述 + 2-3 个示例场景

## 可以做的扩展
- 如果核心词只覆盖部分业务,可补充 1-2 条扩展业务(如"维保")作为附加项,但**绝不替换核心词中的业务**

## ✅ 正确示例
核心业务词: 深圳观光电梯, 深圳别墅电梯, 深圳载货电梯, 东莞观光电梯
→ 业务线必须包含: 观光电梯销售/安装、**别墅电梯销售/安装**、载货电梯销售/安装(3 个核心业务都在)
→ 可补充: 电梯维保(选填,不算核心)

核心业务词: 深圳工业 PCB 打样, 深圳消费电子 PCB 打样
→ 业务线: 工业 PCB 打样、消费电子 PCB 打样

## ❌ 错误示例 (绝不许)
核心业务词包含"别墅电梯",但输出业务线只有"观光电梯/载货电梯/维保/加装/改造" — 漏了别墅电梯,客户在选词页看不到 → 销售流失!
核心业务词包含"深圳福田区全屋定制哪家靠谱",但输出业务线有"福田区全屋定制" — 把地区长尾当业务线 → 客户不知道该选什么!

请严格按JSON格式返回(不要任何其他文字):
{{
  "business_lines": [
    {{
      "name": "业务线名称",
      "description": "一句话描述目标客户和核心价值",
      "example_scenarios": ["场景1", "场景2"]
    }}
  ]
}}"""


_BUSINESS_QUERY_SUFFIXES = [
    "哪家好", "哪家靠谱", "哪家专业", "哪家便宜", "哪家价格实在",
    "公司推荐", "推荐", "排名", "排行榜", "前十名", "十大品牌",
    "案例实景", "案例", "效果图", "实景图",
    "多少钱", "价格", "报价", "费用", "收费标准",
    "怎么选", "如何选", "注意事项", "避坑指南", "优缺点", "对比",
    "哪里有", "去哪里", "哪个平台", "附近",
    "厂家", "工厂", "供应商",
    "?", "？", "。", "！",
]

_BUSINESS_LINE_CITIES = list(dict.fromkeys(_KNOWN_CITIES + [
    # _KNOWN_CITIES 之外的地级市 · 兼容历史 validator 行为
    "揭阳", "梅州", "潮州", "韶关", "茂名", "肇庆", "阳江", "清远", "云浮",
    "汕尾", "河源", "莆田", "三明", "龙岩", "南平", "宁德", "淄博",
    "枣庄", "廊坊", "舟山", "衢州", "丽水", "泰州", "连云港", "淮安", "宿迁",
]))

_COMMON_DISTRICT_PREFIXES = [
    # 深圳
    "福田", "罗湖", "南山", "宝安", "龙岗", "龙华", "盐田", "光明", "坪山", "大鹏",
    # 广州
    "天河", "越秀", "海珠", "荔湾", "白云", "黄埔", "番禺", "花都", "南沙", "从化", "增城",
    # 北京/上海/杭州/成都等常见区名
    "朝阳", "海淀", "丰台", "昌平", "浦东", "徐汇", "静安", "闵行", "余杭", "萧山",
    "武侯", "锦江", "青羊", "成华", "高新",
]

_BUSINESS_GENERIC_SUFFIXES = [
    "相关业务", "业务", "设计服务", "定制服务", "装修服务", "装饰服务",
    "销售安装", "销售/安装", "安装服务", "服务", "解决方案", "优化方案", "方案",
    "设计", "定制", "装修", "装饰", "推荐",
]

_BUSINESS_MODIFIER_PREFIXES = [
    "高性价比", "性价比高", "高端", "中高端", "平价", "低价", "便宜",
    "靠谱", "正规", "优质", "精选", "本地", "附近",
]

_BROAD_DISTINCTIVE_TERMS = {"全屋", "整屋", "整体", "本地", "附近"}

_BUSINESS_STOP_FRAGMENTS = {
    "全屋", "整屋", "整体", "定制", "家具", "公司", "服务", "哪家", "推荐",
    "品牌", "口碑", "排名", "方案", "业务", "安装", "销售", "厂家", "专业",
    "深圳", "电梯", "相关", "哪个", "哪里", "靠谱",
}


def _strip_business_query_suffix(term: str) -> str:
    """去掉关键词末尾的问法/意图词,保留产品服务主体。"""
    cleaned = (term or "").strip()
    changed = True
    while changed:
        changed = False
        for suffix in _BUSINESS_QUERY_SUFFIXES:
            if cleaned.endswith(suffix):
                cleaned = cleaned[:-len(suffix)].strip()
                changed = True
                break
    return cleaned


def _strip_business_geo_prefix(term: str) -> tuple[str, bool]:
    """去掉城市 + 区县/街道前缀,返回(cleaned, 是否去过地域)。"""
    cleaned = (term or "").strip()
    stripped_geo = False

    for city in sorted(_BUSINESS_LINE_CITIES, key=len, reverse=True):
        prefixes = (f"{city}市", city)
        for prefix in prefixes:
            if cleaned.startswith(prefix) and len(cleaned) > len(prefix):
                cleaned = cleaned[len(prefix):].strip()
                stripped_geo = True
                break
        if stripped_geo:
            break

    if stripped_geo:
        # 只有已经识别到城市时才用泛化区县正则,避免把"社区养老"误删成"养老"。
        while True:
            next_cleaned = re.sub(
                r"^[\u4e00-\u9fff]{2,8}(?:开发区|高新区|经开区|新区|街道|区|县|镇|乡)",
                "",
                cleaned,
                count=1,
            ).strip()
            if not next_cleaned or next_cleaned == cleaned:
                break
            cleaned = next_cleaned
    else:
        for district in sorted(_COMMON_DISTRICT_PREFIXES, key=len, reverse=True):
            prefixes = (
                f"{district}开发区", f"{district}高新区", f"{district}经开区",
                f"{district}新区", f"{district}街道", f"{district}区",
                f"{district}县", f"{district}镇", f"{district}乡",
            )
            matched = next((p for p in prefixes if cleaned.startswith(p) and len(cleaned) > len(p)), None)
            if matched:
                cleaned = cleaned[len(matched):].strip()
                stripped_geo = True
                break

    return cleaned.strip(), stripped_geo


def _strip_business_modifier_prefix(term: str) -> tuple[str, bool]:
    cleaned = (term or "").strip()
    for prefix in sorted(_BUSINESS_MODIFIER_PREFIXES, key=len, reverse=True):
        if cleaned.startswith(prefix) and len(cleaned) > len(prefix) + 1:
            return cleaned[len(prefix):].strip(), True
    return cleaned, False


def _normalize_business_product_term(term: str) -> str:
    """把地域长尾关键词归一成产品/服务名。"""
    cleaned = _strip_business_query_suffix(term)
    cleaned, _ = _strip_business_geo_prefix(cleaned)
    cleaned, _ = _strip_business_modifier_prefix(cleaned)
    return cleaned.strip()


def _business_distinctive_term(term: str) -> str:
    """提取用于父子业务覆盖判断的核心名词。"""
    cleaned = _normalize_business_product_term(term)
    changed = True
    while changed:
        changed = False
        for suffix in _BUSINESS_GENERIC_SUFFIXES:
            if cleaned.endswith(suffix) and len(cleaned) > len(suffix) + 1:
                cleaned = cleaned[:-len(suffix)].strip()
                changed = True
                break
    return cleaned


def _append_example_scenario(line: dict, scenario: str, limit: int = 8) -> None:
    """把原始长尾词挂到父业务的示例里,避免后续关键词分配失去依据。"""
    scenario = (scenario or "").strip()
    if not scenario:
        return
    examples = line.setdefault("example_scenarios", [])
    if not isinstance(examples, list):
        examples = [str(examples)]
        line["example_scenarios"] = examples
    if scenario not in examples and len(examples) < limit:
        examples.append(scenario)


def _business_line_text(line: dict) -> str:
    parts = [
        str(line.get("name", "")),
        str(line.get("description", "")),
    ]
    parts.extend(str(s) for s in (line.get("example_scenarios") or []))
    return " ".join(p for p in parts if p)


def _specific_fragments(text: str) -> set[str]:
    normalized = _business_distinctive_term(text)
    fragments: set[str] = set()
    for size in (4, 3, 2):
        for i in range(max(0, len(normalized) - size + 1)):
            frag = normalized[i:i + size]
            if (
                frag
                and frag not in _BUSINESS_STOP_FRAGMENTS
                and not any(stop in frag for stop in _BUSINESS_STOP_FRAGMENTS)
            ):
                fragments.add(frag)
    return fragments


def _is_umbrella_space_business_line(line: dict) -> bool:
    text = _business_line_text(line)
    return (
        any(anchor in text for anchor in ("全屋", "整屋", "整体", "全案", "空间"))
        and any(marker in text for marker in ("涵盖", "包括", "包含", "覆盖", "一站式", "整体"))
    )


def _business_line_match_score(product: str, line: dict) -> int:
    """给历史自动补充业务线找最像的既有父业务。"""
    normalized_product = _normalize_business_product_term(product)
    product_distinctive = _business_distinctive_term(normalized_product)
    line_text = _business_line_text(line)

    if _is_product_covered(normalized_product, line_text):
        return 100

    score = 0
    line_terms = []
    for raw in [line.get("name", ""), line.get("description", ""), *(line.get("example_scenarios") or [])]:
        term = _normalize_business_product_term(str(raw))
        distinctive = _business_distinctive_term(term)
        line_terms.extend(t for t in (term, distinctive) if t)

    for term in line_terms:
        if term in _BROAD_DISTINCTIVE_TERMS or term in _BUSINESS_STOP_FRAGMENTS:
            continue
        if len(term) >= 3 and term in normalized_product:
            score = max(score, len(term) * 3)
        if len(product_distinctive) >= 3 and product_distinctive in term:
            score = max(score, len(product_distinctive) * 3)

    line_text_normalized = " ".join(_normalize_business_product_term(str(part)) for part in [
        line.get("name", ""),
        line.get("description", ""),
        *(line.get("example_scenarios") or []),
    ])
    shared_fragments = _specific_fragments(normalized_product)
    for frag in shared_fragments:
        if frag in line_text_normalized:
            score += len(frag)

    return score


def _find_best_business_line(product: str, lines: list[dict], exclude_index: int, skipped: set[int]) -> tuple[dict | None, int | None]:
    best_line = None
    best_index = None
    best_score = 0
    for target_idx, candidate in enumerate(lines):
        if target_idx == exclude_index or target_idx in skipped:
            continue
        score = _business_line_match_score(product, candidate)
        if score > best_score:
            best_score = score
            best_line = candidate
            best_index = target_idx
    if best_score >= 4:
        return best_line, best_index
    return None, None


def _find_covering_business_line(product: str, business_lines: list[dict], exclude_index: int | None = None) -> dict | None:
    for idx, line in enumerate(business_lines):
        if exclude_index is not None and idx == exclude_index:
            continue
        if _is_product_covered(product, _business_line_text(line)):
            return line
    return None


def _extract_product_terms_with_sources(keywords: list[str]) -> list[tuple[str, str]]:
    seen = set()
    products = []
    for kw in keywords:
        original = str(kw or "").strip()
        term = _normalize_business_product_term(original)
        if term and term not in seen:
            seen.add(term)
            products.append((term, original))
    return products


def _line_is_geo_or_auto_supplement(line: dict) -> bool:
    name = str(line.get("name", ""))
    cleaned = _strip_business_query_suffix(name)
    cleaned, had_geo = _strip_business_geo_prefix(cleaned)
    _cleaned, had_modifier = _strip_business_modifier_prefix(cleaned)
    return bool(line.get("_auto_supplemented")) or had_geo or had_modifier


def _merge_business_line_into(target: dict, source: dict) -> None:
    target["is_selected"] = bool(target.get("is_selected")) or bool(source.get("is_selected"))
    for scenario in source.get("example_scenarios") or []:
        _append_example_scenario(target, str(scenario))
    source_name = str(source.get("name", "")).strip()
    if source_name:
        _append_example_scenario(target, source_name)


def _normalize_business_lines_with_id_map(business_lines: list[dict]) -> tuple[list[dict], dict[int, int]]:
    """
    合并 validator/LLM 产生的地域长尾业务线。

    示例: "福田区全屋定制" → 并入 "全屋定制设计";
    "高性价比全屋定制" → 并入 "全屋定制设计";
    父业务明确涵盖衣柜/榻榻米等子服务时,子服务也并入父业务。
    如果没有父业务,则改名为 "全屋定制",避免前端出现区县词自立门户。
    """
    if not business_lines:
        return business_lines, {}

    lines = [dict(line) for line in business_lines]
    original_ids: list[int | None] = []
    seen_original_ids: set[int] = set()
    for line in lines:
        raw_id = line.get("id")
        if isinstance(raw_id, bool):
            normalized_old_id = None
        elif isinstance(raw_id, int):
            normalized_old_id = raw_id
        elif isinstance(raw_id, str) and raw_id.strip().isdigit():
            normalized_old_id = int(raw_id.strip())
        else:
            normalized_old_id = None
        if normalized_old_id is not None:
            if normalized_old_id in seen_original_ids:
                raise ValueError(
                    f"duplicate business line id: {normalized_old_id}"
                )
            seen_original_ids.add(normalized_old_id)
        original_ids.append(normalized_old_id)
    skipped: set[int] = set()
    merge_target_by_index: dict[int, int] = {}

    for idx, line in enumerate(lines):
        if idx in skipped:
            continue

        product = _normalize_business_product_term(str(line.get("name", "")))
        if not product:
            continue
        is_variant_line = _line_is_geo_or_auto_supplement(line)

        target = None
        target_index = None
        if line.get("_auto_supplemented"):
            target, target_index = _find_best_business_line(product, lines, idx, skipped)
        if target is None:
            for target_idx, candidate in enumerate(lines):
                if target_idx == idx or target_idx in skipped:
                    continue
                candidate_covers_product = _is_product_covered(product, _business_line_text(candidate))
                if candidate_covers_product and (is_variant_line or _is_umbrella_space_business_line(candidate)):
                    target = candidate
                    target_index = target_idx
                    break

        if target is not None:
            _merge_business_line_into(target, line)
            skipped.add(idx)
            if target_index is not None:
                merge_target_by_index[idx] = target_index
        elif is_variant_line and product != line.get("name"):
            line["name"] = product
            if line.get("_auto_supplemented"):
                line["description"] = f"{product}相关业务"

    kept_indices = [idx for idx, _line in enumerate(lines) if idx not in skipped]
    old_id_to_new_id: dict[int, int] = {}
    merged = []
    for new_id, idx in enumerate(kept_indices, start=1):
        line = lines[idx]
        old_id = original_ids[idx]
        line["id"] = new_id
        if old_id is not None:
            old_id_to_new_id[old_id] = new_id
        merged.append(line)

    for old_idx, target_idx in merge_target_by_index.items():
        old_id = original_ids[old_idx]
        target_old_id = original_ids[target_idx]
        if old_id is not None and target_old_id is not None and target_old_id in old_id_to_new_id:
            old_id_to_new_id[old_id] = old_id_to_new_id[target_old_id]

    return merged, old_id_to_new_id


def _merge_geo_variant_business_lines(business_lines: list[dict]) -> list[dict]:
    return _normalize_business_lines_with_id_map(business_lines)[0]


def _extract_product_terms_from_keywords(keywords: list[str]) -> list[str]:
    """
    从关键词中提取"产品/服务"名词候选 · 用于后置 validator 校验

    简单规则: 去掉城市名 + 通用问法后缀,剩下的核心名词作为产品名
    示例:
      "深圳别墅电梯哪家好？" → "别墅电梯"
      "深圳工业PCB打样推荐"  → "工业PCB打样"
      "上海10吨叉车租赁价格" → "10吨叉车租赁"

    返回去重后的产品名候选列表
    """
    return [product for product, _source in _extract_product_terms_with_sources(keywords)]


def _is_product_covered(prod: str, existing: str) -> bool:
    """
    判断产品名是否被既有 business_lines 文本覆盖
    [CTO-15.23 2026-05-05] 老板"揭阳商务酒店"反馈:原 `prod not in existing` 太严格
      LLM 已合并出"商务出差酒店推荐服务"·但"商务酒店"不完全包含 → 误判 missing → 重复补全

    规则:
      1. 完整包含 → 覆盖
      2. 长度 < 4 → 必须完整包含
      3. 长度 ≥ 4 → 滑动 3 字窗口 · 60% 命中率算覆盖(语义子集)
    """
    prod = _normalize_business_product_term(prod)
    existing = (existing or "").strip()
    if not prod:
        return True
    if prod in existing:
        return True

    distinctive = _business_distinctive_term(prod)
    if (
        distinctive not in _BROAD_DISTINCTIVE_TERMS
        and len(distinctive) >= 2
        and distinctive in existing
    ):
        return True

    if len(prod) < 4:
        return False
    windows = [prod[i:i + 3] for i in range(len(prod) - 2)]
    if not windows:
        return False
    hits = sum(1 for w in windows if w in existing)
    return hits / len(windows) >= 0.6


def _validate_business_lines_coverage(
    business_lines: list[dict],
    core_keywords: list[str],
) -> list[dict]:
    """
    后置 validator: 校验 LLM 输出的 business_lines 是否覆盖 core_keywords 中的所有产品名

    缺失的产品名 → 自动补一个 business_line + 日志报警
    LLM 守门员模式 · LLM 漏 → 代码兜底
    [CTO-15.23 2026-05-05] 修文案/命名/覆盖判断 3 处:
    - 命名去硬后缀"销售安装"(并非所有行业都是销售安装型 · 如酒店/咨询/维修)
    - description 去技术术语"validator 自动补充(LLM 漏)"(用户不该看到内部用语)
    - 覆盖判断改 fuzzy(原字符串完全包含太严格 · 误判 LLM 已合并的语义子集)
    """
    if not core_keywords:
        return business_lines

    product_sources = _extract_product_terms_with_sources(core_keywords)
    if not product_sources:
        return business_lines

    missing_products: dict[str, list[str]] = {}
    for prod, source_keyword in product_sources:
        matched_line = _find_covering_business_line(prod, business_lines)
        if matched_line:
            _append_example_scenario(matched_line, source_keyword)
        else:
            missing_products.setdefault(prod, []).append(source_keyword or prod)

    if missing_products:
        print(
            f"  [业务线 Validator] LLM 漏了 {len(missing_products)} 个产品: "
            f"{', '.join(missing_products.keys())} · 自动补充"
        )
        next_id = max((bl.get("id", 0) for bl in business_lines), default=0) + 1
        for prod, sources in missing_products.items():
            business_lines.append({
                "id": next_id,
                "name": prod,                       # 直接用产品名 · 不加硬后缀
                "description": f"{prod}相关业务",   # 去技术术语
                "example_scenarios": sources[:3] or [prod],
                "is_selected": False,
                "_auto_supplemented": True,         # 后端标记 · 前端不展示这字段
            })
            next_id += 1

    return _merge_geo_variant_business_lines(business_lines)


# [CTO-15.23 2026-05-11 P0#2] 关键词→业务线分配 prompt
# 老板痛点:客户选业务线后,后端把所有 45 个 keyword 全标 selected · 不分业务线 → "客户已确认 44 个"误导
# 修法:LLM 把每个关键词分配到最匹配的业务线 · 客户选业务线后只 select 属于该业务线的 keyword
KEYWORD_BL_ASSIGN_PROMPT = """你是行业业务分析专家。请把下列关键词逐个分配到最匹配的业务线。

## 客户
- 品牌: {brand_name}
- 行业: {industry}

## 业务线列表
{business_lines_desc}

## 待分配关键词
{keyword_lines}

## 铁律
1. 每个关键词必须分配到上面列表中的 1 个 business_line_id
2. 选**语义最相似 / 包含核心词 / 该业务线下客户会搜的意图**的业务线
3. 不能漏关键词 · 不能多分配
4. 不许新建业务线 · 只能用上面给的 ID

请严格按JSON格式返回(不要任何其他文字):
{{
  "assignments": [
    {{"keyword": "关键词原文", "business_line_id": 1}},
    {{"keyword": "关键词原文", "business_line_id": 2}}
  ]
}}"""


async def assign_keywords_to_business_lines(
    keywords: list[dict],
    business_lines: list[dict],
    brand_name: str = "",
    industry: str = "",
) -> dict[int, int]:
    """[CTO-15.23 2026-05-11 P0#2] 把每个关键词分配到 business_line_id

    Args:
        keywords: keywords_snapshot 格式 [{"id": N, "keyword": "..."}]
        business_lines: extract_business_lines 输出 [{"id": N, "name": "...", "description": "...", "example_scenarios": [...]}]
        brand_name: 品牌名
        industry: 行业

    Returns:
        {keyword_id: business_line_id} 映射(每个 keyword 都有归属)
    """
    if not keywords or not business_lines:
        return {}

    # 单业务线兜底:全分配到该业务线
    if len(business_lines) == 1:
        bl_id = business_lines[0].get("id")
        if bl_id is None:
            return {}
        return {kw["id"]: bl_id for kw in keywords if "id" in kw}

    # 构建 prompt
    bl_desc_lines = []
    for bl in business_lines:
        bl_desc_lines.append(f"- ID {bl.get('id')}: 「{bl.get('name', '')}」 {bl.get('description', '')}")
        if bl.get('example_scenarios'):
            bl_desc_lines.append(f"  示例: {', '.join(str(s) for s in bl['example_scenarios'][:5])}")
    bl_desc_str = "\n".join(bl_desc_lines)

    kw_lines = "\n".join(f"- 「{kw['keyword']}」" for kw in keywords if kw.get("keyword"))

    prompt = KEYWORD_BL_ASSIGN_PROMPT.format(
        brand_name=brand_name or "客户",
        industry=industry or "综合服务",
        business_lines_desc=bl_desc_str,
        keyword_lines=kw_lines,
    )

    response = ""
    import os
    import httpx
    from services.llm.deepseek_key_pool import has_deepseek_key, adeepseek_post_with_failover
    if has_deepseek_key():
        try:
            # [failover 2026-06-11] 多 key 失败自动换下一个重试(单 key=直调·向后兼容)·内含 llm_track
            resp = await adeepseek_post_with_failover(
                {
                    "model": DEEPSEEK_OFFICIAL_FLASH,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                    "max_tokens": 4000,
                    "stream": False,
                    "thinking": {"type": "disabled"},
                },
                track_name="keyword_business_line_assignment",
                track_model=DEEPSEEK_OFFICIAL_FLASH,
            )
            response = resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"  [关键词分配] V4-Flash 异常 {e} → fallback")

    if not response:
        try:
            from tools.multi_llm_caller import call_llm_with_fallback
            response = await call_llm_with_fallback(prompt, verbose=False)
        except Exception as e:
            print(f"  [关键词分配] Fallback 也失败: {e}")
            response = ""

    # 解析 LLM 返回
    mapping: dict[int, int] = {}
    kw_text_to_id = {kw["keyword"]: kw["id"] for kw in keywords if kw.get("keyword") and "id" in kw}
    valid_bl_ids = {bl["id"] for bl in business_lines if "id" in bl}

    if response:
        try:
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                for item in result.get("assignments", []):
                    kw_text = (item.get("keyword") or "").strip()
                    bl_id = item.get("business_line_id")
                    if kw_text in kw_text_to_id and isinstance(bl_id, int) and bl_id in valid_bl_ids:
                        mapping[kw_text_to_id[kw_text]] = bl_id
        except Exception as e:
            print(f"  [关键词分配] JSON 解析失败: {e}")

    # Validator 兜底:漏掉的 keyword 用字符串匹配兜底
    fallback_count = 0
    bl_terms = []
    for bl in business_lines:
        terms = [str(bl.get("name", ""))]
        for s in bl.get("example_scenarios") or []:
            terms.append(str(s))
        bl_terms.append((bl.get("id"), [t.lower() for t in terms if t]))

    for kw in keywords:
        kid = kw.get("id")
        if kid is None or kid in mapping:
            continue
        kw_text = (kw.get("keyword") or "").lower()
        if not kw_text:
            continue
        # 字符串相似度兜底
        best_bl = None
        best_score = 0
        for bl_id, terms in bl_terms:
            if bl_id is None:
                continue
            score = 0
            for t in terms:
                if not t:
                    continue
                if t in kw_text:
                    score += len(t)  # 越长权重越大
                elif len(t) >= 2 and any(t[i:i+2] in kw_text for i in range(len(t) - 1)):
                    score += 1
            if score > best_score:
                best_score = score
                best_bl = bl_id
        if best_bl is None:
            best_bl = business_lines[0].get("id")
        if best_bl is not None:
            mapping[kid] = best_bl
            fallback_count += 1

    if fallback_count > 0:
        print(f"  [关键词分配] {fallback_count} 个关键词 LLM 漏分 · 字符串兜底已补")
    print(f"  [关键词分配] 完成 · {len(mapping)}/{len(keywords)} 个 keyword 已分配业务线")
    return mapping


async def extract_business_lines(
    brand_name: str,
    industry: str,
    city: str = "",
    core_keywords: list[str] = None,
    business_scope: str = "",
) -> list[dict]:
    """
    从客户信息中提取业务线列表。

    [CTO-15.23 2026-05-05 P0 fix] 三层防御:
      L1: prompt 加铁律(core_keywords 必须 100% 覆盖)
      L2: 切 deepseek-v4-flash 直连 + thinking=OFF + temperature=0.2
      L3: 后置 validator 兜底(LLM 漏 → 代码补)

    Returns:
        [{"id": 1, "name": "...", "description": "...", "example_scenarios": [...]}]
    """
    import os
    import httpx

    prompt = BUSINESS_LINE_PROMPT.format(
        brand_name=brand_name,
        industry=industry,
        city=city or "全国",
        core_keywords="、".join(core_keywords) if core_keywords else "未指定",
        business_scope=business_scope or "未指定",
    )

    response = ""
    from services.llm.deepseek_key_pool import has_deepseek_key, adeepseek_post_with_failover
    if has_deepseek_key():
        try:
            # [failover 2026-06-11] 多 key 失败自动换下一个重试(单 key=直调·向后兼容)·内含 llm_track
            resp = await adeepseek_post_with_failover(
                {
                    "model": DEEPSEEK_OFFICIAL_FLASH,
                    "messages": [{"role": "user", "content": prompt}],
                    # [CTO-15.23 2026-05-11 Bug E] temperature 0.2 → 0.1 同品牌算价稳定
                    "temperature": 0.1,
                    "max_tokens": 1500,
                    "stream": False,
                    "thinking": {"type": "disabled"},
                },
                track_name="keyword_business_line_generation",
                track_model=DEEPSEEK_OFFICIAL_FLASH,
            )
            response = resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"  [业务线] V4-Flash 异常 {e} → fallback")

    # Fallback: multi_llm_caller (已升级到 V4-Flash 多源后备)
    if not response:
        try:
            from tools.multi_llm_caller import call_llm_with_fallback
            response = await call_llm_with_fallback(prompt, verbose=False)
        except Exception as e:
            print(f"  [业务线] Fallback 也失败: {e}")
            response = ""

    business_lines = []
    if response:
        try:
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                lines = result.get("business_lines", [])
                for i, line in enumerate(lines):
                    name = line.get("name", "").strip()
                    if not name:
                        continue
                    business_lines.append({
                        "id": i + 1,
                        "name": name,
                        "description": line.get("description", ""),
                        "example_scenarios": line.get("example_scenarios", []),
                        "is_selected": False,  # 默认不选,客户自己选
                    })
        except Exception as e:
            print(f"  [业务线] JSON 解析失败: {e}")

    # 后置 validator: 校验 core_keywords 覆盖度 + 自动补漏(L3 兜底)
    if business_lines and core_keywords:
        business_lines = _validate_business_lines_coverage(business_lines, core_keywords)

    # 完全失败兜底: 至少基于 core_keywords 出 N 条
    # [CTO-15.23 2026-05-05] name 去硬后缀"销售安装" · 跟 _validate_business_lines_coverage 对齐
    if not business_lines:
        if core_keywords:
            products = _extract_product_terms_from_keywords(core_keywords)
            for i, prod in enumerate(products[:8]):  # 最多 8 条
                business_lines.append({
                    "id": i + 1,
                    "name": prod,
                    "description": f"{prod}相关业务",
                    "example_scenarios": [prod],
                    "is_selected": False,
                    "_auto_supplemented": True,
                })
        if not business_lines:
            business_lines = [{
                "id": 1,
                "name": industry or "综合服务",
                "description": f"{brand_name}的核心业务",
                "example_scenarios": [],
                "is_selected": True,
            }]

    business_lines = _merge_geo_variant_business_lines(business_lines)

    print(f"  [业务线] 提取出 {len(business_lines)} 条: {', '.join(bl['name'] for bl in business_lines)}")
    return business_lines


# ========================================
# 测试入口
# ========================================

if __name__ == "__main__":
    import asyncio

    # 模拟 score_keywords 输出
    test_keywords = [
        {"keyword": "上海劳斯莱斯幻影租赁公司推荐", "value_score": 1.73, "difficulty_score": 1.2,
         "search_volume": 200, "intent": "commercial", "search_probability": 0.8,
         "selling_price": 1789, "required_articles": 8, "effective_competition": 15,
         "geo_multiplier": 1.0, "is_broad": False, "cost_per_article": 65.0},
        {"keyword": "上海劳斯莱斯幻影租赁哪家好", "value_score": 1.65, "difficulty_score": 1.15,
         "search_volume": 150, "intent": "commercial", "search_probability": 0.75,
         "selling_price": 1520, "required_articles": 7, "effective_competition": 12,
         "geo_multiplier": 1.0, "is_broad": False, "cost_per_article": 65.0},
        {"keyword": "上海劳斯莱斯幻影自驾租一天多少钱", "value_score": 1.4, "difficulty_score": 1.0,
         "search_volume": 80, "intent": "transactional", "search_probability": 0.7,
         "selling_price": 1200, "required_articles": 5, "effective_competition": 8,
         "geo_multiplier": 1.0, "is_broad": False, "cost_per_article": 60.0},
        {"keyword": "上海迈巴赫S480租赁公司推荐", "value_score": 1.6, "difficulty_score": 1.1,
         "search_volume": 120, "intent": "commercial", "search_probability": 0.75,
         "selling_price": 1650, "required_articles": 7, "effective_competition": 10,
         "geo_multiplier": 1.0, "is_broad": False, "cost_per_article": 65.0},
        {"keyword": "上海迈巴赫婚车租赁哪家价格实在", "value_score": 1.5, "difficulty_score": 1.05,
         "search_volume": 90, "intent": "transactional", "search_probability": 0.8,
         "selling_price": 1400, "required_articles": 6, "effective_competition": 8,
         "geo_multiplier": 1.0, "is_broad": False, "cost_per_article": 60.0},
        {"keyword": "上海埃尔法租赁带司机公司推荐", "value_score": 1.55, "difficulty_score": 1.1,
         "search_volume": 100, "intent": "commercial", "search_probability": 0.7,
         "selling_price": 1500, "required_articles": 6, "effective_competition": 9,
         "geo_multiplier": 1.0, "is_broad": False, "cost_per_article": 65.0},
    ]

    async def main():
        result = await cluster_keywords(test_keywords, "一路顺风出行", "豪车租赁")
        print(f"\n{'='*60}")
        print(f"聚类结果: {result['stats']}")
        for i, cluster in enumerate(result["clusters"]):
            print(f"\n--- 主题包 {i+1}: {cluster['cluster_name']} ---")
            print(f"  核心词 ({cluster['core_keyword_count']}个):")
            for kw in cluster["core_keywords"]:
                print(f"    [V] {kw['keyword']}  score:{kw['core_score']}  vol:{kw['search_volume']}  Y{kw['selling_price']}")
            real_covered = [c for c in cluster["covered_keywords"] if c.get("source") != "generated_variant"]
            variants = [c for c in cluster["covered_keywords"] if c.get("source") == "generated_variant"]
            if real_covered:
                print(f"  附赠词 ({len(real_covered)}个):")
                for kw in real_covered:
                    print(f"    - {kw['keyword']}  ({kw.get('merge_reason', '')})")
            print(f"  变体词: {cluster['variant_count']}个 (展示前{min(3, len(variants))}个)")
            for v in variants[:3]:
                print(f"    > {v['keyword']}")

    asyncio.run(main())
