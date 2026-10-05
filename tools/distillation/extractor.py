"""
数据蒸馏特征提取管道 v1.0

从 AI 平台的原始回复 (full_response) 中提取结构化洞察：
- 品牌提取（复用 ai_tester 的品牌变体生成逻辑）
- 排名计算（区分 explicit / positional / mention_only）
- URL 提取
- 结构特征分析
- 情感分析

设计原则：
- 分层提取：正则(~65%) → 模板(~20%) → LLM(~15%)
- 每条数据记录 extraction_method 和 confidence
"""

import re
from typing import List, Dict, Optional, Tuple, Any


# ==========================================
# 品牌变体生成（复用 ai_tester 逻辑）
# ==========================================

# 常见企业后缀
_COMPANY_SUFFIXES = [
    "科技", "技术", "公司", "集团", "有限公司", "有限责任公司",
    "网络", "信息", "数字", "传媒", "咨询", "服务",
]

# 知名品牌别名映射
_BRAND_ALIAS_MAP = {
    "小米": ["Xiaomi", "MI", "Redmi", "红米"],
    "华为": ["Huawei", "HUAWEI", "荣耀", "Honor"],
    "苹果": ["Apple", "iPhone", "iPad"],
    "腾讯": ["Tencent", "微信", "WeChat", "QQ"],
    "阿里": ["Alibaba", "阿里巴巴", "淘宝", "天猫"],
    "字节": ["ByteDance", "抖音", "TikTok", "字节跳动"],
    "百度": ["Baidu", "BAIDU"],
    "京东": ["JD", "JD.com"],
    "美团": ["Meituan"],
    "拼多多": ["Pinduoduo", "PDD"],
}


def generate_brand_variants(brand_name: str) -> List[str]:
    """
    生成品牌名称变体（复用 ai_tester._generate_brand_variants 逻辑）
    例：全域上榜科技 → [全域上榜科技, 全域上榜]
    """
    if not brand_name:
        return []

    variants = [brand_name]

    # 去后缀得到核心品牌
    core_brand = brand_name
    for suffix in _COMPANY_SUFFIXES:
        if brand_name.endswith(suffix):
            core_brand = brand_name[:-len(suffix)]
            break

    if core_brand and core_brand != brand_name and len(core_brand) >= 2:
        variants.append(core_brand)

    # 查找别名
    for cn_brand, aliases in _BRAND_ALIAS_MAP.items():
        if cn_brand in core_brand or core_brand in cn_brand:
            variants.extend(aliases)
            break

    return list(dict.fromkeys(variants))  # 去重保序


# ==========================================
# 品牌提取
# ==========================================

# 有序列表模式：1. **品牌名** 或 1. 品牌名 或 - **品牌名**
_LIST_PATTERN = re.compile(
    r'(?:^|\n)\s*(?:\d+[\.\)、]|[-*])\s*\*{0,2}\s*'
    r'([^\n*（(：:]{2,30}?)'
    r'(?:\*{0,2})\s*(?:[（(：:—\-–]|\n)',
    re.MULTILINE
)

# URL 模式
_URL_PATTERN = re.compile(
    r'https?://[^\s\)）\]」》,，。、\n]+',
    re.IGNORECASE
)


def extract_brands(
    full_response: str,
    known_brands: Optional[List[str]] = None
) -> List[str]:
    """
    从 AI 回复中提取被提及的品牌/公司名

    策略：
    1. 已知品牌精确匹配（如果提供了 known_brands）
    2. 有序列表中的品牌名提取（正则）
    3. 通用公司名模式匹配

    Returns:
        去重的品牌名列表，按出现顺序排列
    """
    if not full_response:
        return []

    brands_found = []
    text = full_response

    # 策略 1：已知品牌精确匹配
    if known_brands:
        for brand in known_brands:
            variants = generate_brand_variants(brand)
            for variant in variants:
                if variant.lower() in text.lower():
                    brands_found.append(brand)  # 统一使用原始品牌名
                    break

    # 策略 2：有序列表中提取
    list_matches = _LIST_PATTERN.findall(text)
    for match in list_matches:
        name = match.strip().strip("*").strip()
        # 过滤掉太短或明显不是品牌名的
        # 过滤掉太短或明显不是品牌名的
        if len(name) >= 2 and len(name) <= 20 and '**' not in name:
            # 过滤常见非品牌词
            skip_words = [
                "优点", "缺点", "特点", "功能", "总结", "建议", "注意",
                "方法", "步骤", "原因", "案例", "方案", "服务", "产品",
                "价格", "费用", "成本", "效果", "结论", "分析",
            ]
            if not any(w in name for w in skip_words):
                brands_found.append(name)

    # 策略 3：通用公司名模式（"XX公司"、"XX科技"）
    company_pattern = re.compile(
        r'(?:["「])?([^\n"「」"]{2,15}(?:科技|公司|集团|网络|传媒|咨询))(?:["」])?'
    )
    for match in company_pattern.findall(text):
        name = match.strip()
        if len(name) >= 3:
            brands_found.append(name)

    # 去重保序
    seen = set()
    result = []
    for b in brands_found:
        b_lower = b.lower().strip()
        if b_lower not in seen and len(b_lower) >= 2:
            seen.add(b_lower)
            result.append(b)

    return result


# ==========================================
# 排名计算
# ==========================================

def find_brand_rank(
    brands_in_response: List[str],
    target_brand: str,
    full_response: str
) -> Tuple[Optional[int], Optional[str]]:
    """
    计算目标品牌在 AI 回复中的排名

    Returns:
        (rank, rank_type):
        - (1, 'explicit')     — "第一名是 X"
        - (3, 'positional')   — 列表中第 3 个出现
        - (None, 'mention_only') — 仅被提及，无排名
        - (None, None)        — 未出现
    """
    if not target_brand or not full_response:
        return (None, None)

    target_variants = generate_brand_variants(target_brand)
    text_lower = full_response.lower()

    # 检测是否被提及
    is_mentioned = False
    for variant in target_variants:
        if variant.lower() in text_lower:
            is_mentioned = True
            break

    if not is_mentioned:
        return (None, None)

    # 策略 1：检测显式排名 — "第一名是X"、"排名第1的是X"
    rank_number_map = {
        "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
        "1": 1, "2": 2, "3": 3, "4": 4, "5": 5,
    }
    for variant in target_variants:
        # "第X名是 BRAND" 或 "排名第X BRAND"
        for cn_num, num in rank_number_map.items():
            patterns = [
                rf'第{cn_num}[名位].*?{re.escape(variant)}',
                rf'排名第{cn_num}.*?{re.escape(variant)}',
                rf'{re.escape(variant)}.*?排名第{cn_num}',
                rf'{re.escape(variant)}.*?位居第{cn_num}',
            ]
            for pattern in patterns:
                if re.search(pattern, full_response, re.IGNORECASE):
                    return (num, 'explicit')

    # 策略 2：列表位置排名
    for i, brand in enumerate(brands_in_response):
        for variant in target_variants:
            if variant.lower() in brand.lower() or brand.lower() in variant.lower():
                return (i + 1, 'positional')

    # 策略 3：仅提及
    return (None, 'mention_only')


# ==========================================
# URL 提取
# ==========================================

def extract_urls(full_response: str) -> List[str]:
    """提取 AI 回复中引用的 URL"""
    if not full_response:
        return []

    urls = _URL_PATTERN.findall(full_response)

    # 去重保序 + 清洗尾部标点
    seen = set()
    result = []
    for url in urls:
        # 去掉尾部可能的标点
        url = url.rstrip(".,;:!?。，；：！？")
        if url not in seen:
            seen.add(url)
            result.append(url)

    return result


# ==========================================
# 结构特征分析
# ==========================================

def analyze_structure(full_response: str) -> Dict[str, Any]:
    """
    分析 AI 回复的结构特征

    Returns:
        {
            "has_list": bool,        # 是否有有序/无序列表
            "has_table": bool,       # 是否有表格
            "has_headers": bool,     # 是否有标题
            "has_bold": bool,        # 是否有加粗
            "list_count": int,       # 列表项数量
            "paragraph_count": int,  # 段落数
            "char_count": int,       # 字符总数
            "has_scores": bool,      # 是否有评分
            "has_urls": bool,        # 是否引用了 URL
        }
    """
    if not full_response:
        return {
            "has_list": False, "has_table": False, "has_headers": False,
            "has_bold": False, "list_count": 0, "paragraph_count": 0,
            "char_count": 0, "has_scores": False, "has_urls": False,
        }

    text = full_response

    # 有序列表: 1. 或 1) 或 1、
    ordered_list = re.findall(r'(?:^|\n)\s*\d+[\.\)、]', text)
    # 无序列表: - 或 * 开头
    unordered_list = re.findall(r'(?:^|\n)\s*[-*]\s+\S', text)
    list_count = len(ordered_list) + len(unordered_list)

    # 表格: | 分隔
    has_table = bool(re.search(r'\|.+\|.+\|', text))

    # 标题: # 或 ## 开头
    has_headers = bool(re.search(r'(?:^|\n)#{1,4}\s+', text))

    # 加粗: **text**
    has_bold = '**' in text

    # 段落: 双换行分隔
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]

    # 评分: X分 或 X/10 或 X/100
    has_scores = bool(re.search(r'\d+\.?\d*\s*[/／]\s*(?:10|100|5)|(?:\d+\.?\d*)\s*分', text))

    # URL
    has_urls = bool(_URL_PATTERN.search(text))

    return {
        "has_list": list_count > 0,
        "has_table": has_table,
        "has_headers": has_headers,
        "has_bold": has_bold,
        "list_count": list_count,
        "paragraph_count": len(paragraphs),
        "char_count": len(text),
        "has_scores": has_scores,
        "has_urls": has_urls,
    }


# ==========================================
# 情感分析（复用 ai_tester 逻辑，适配蒸馏场景）
# ==========================================

# 正面关键词（GEO 服务推荐场景）
_POSITIVE_KEYWORDS = [
    "推荐", "首选", "最佳", "最好", "优秀", "出色", "领先", "顶级",
    "性价比高", "值得", "专业", "权威", "可靠", "信赖",
    "效果好", "效果显著", "成功", "满意", "好评", "口碑好",
    "创新", "前沿", "先进", "成熟", "完善",
]

# 负面关键词
_NEGATIVE_KEYWORDS = [
    "不推荐", "不建议", "差", "一般", "普通", "落后", "过时",
    "风险", "问题多", "争议", "质疑", "价格贵", "性价比低",
    "不成熟", "不完善", "缺乏", "不足",
]


def analyze_sentiment(
    full_response: str,
    target_brand: str
) -> Dict[str, Any]:
    """
    分析品牌在 AI 回复中的情感倾向

    Returns:
        {"label": "positive"|"neutral"|"negative", "score": -1.0~1.0}
    """
    if not full_response or not target_brand:
        return {"label": "neutral", "score": 0.0}

    variants = generate_brand_variants(target_brand)
    text_lower = full_response.lower()

    positive_count = 0
    negative_count = 0

    for variant in variants:
        variant_lower = variant.lower()
        pos = 0
        while True:
            pos = text_lower.find(variant_lower, pos)
            if pos == -1:
                break

            # 品牌前后各 150 字符的上下文
            ctx_start = max(0, pos - 150)
            ctx_end = min(len(text_lower), pos + len(variant_lower) + 150)
            context = text_lower[ctx_start:ctx_end]

            for kw in _POSITIVE_KEYWORDS:
                if kw in context:
                    positive_count += 1
                    break

            for kw in _NEGATIVE_KEYWORDS:
                if kw in context:
                    negative_count += 1
                    break

            pos += 1

    total = positive_count + negative_count
    if total == 0:
        return {"label": "neutral", "score": 0.0}

    score = round((positive_count - negative_count) / total, 2)
    if score > 0.2:
        label = "positive"
    elif score < -0.2:
        label = "negative"
    else:
        label = "neutral"

    return {"label": label, "score": score}


# ==========================================
# 推荐强度评估
# ==========================================

def assess_recommendation_strength(
    full_response: str,
    target_brand: str,
    client_rank: Optional[int],
    sentiment_label: str
) -> int:
    """
    综合评估推荐强度 (1-10)

    考虑因素：
    - 排名位置
    - 情感倾向
    - 描述篇幅
    - 是否被特别推荐
    """
    score = 5  # 基础分

    # 排名加减分
    if client_rank == 1:
        score += 3
    elif client_rank and client_rank <= 3:
        score += 2
    elif client_rank and client_rank <= 5:
        score += 1

    # 情感加减分
    if sentiment_label == "positive":
        score += 1
    elif sentiment_label == "negative":
        score -= 2

    # 特别推荐关键词
    variants = generate_brand_variants(target_brand)
    text_lower = full_response.lower()
    strong_recommend = ["强烈推荐", "首选", "最佳选择", "第一推荐", "首推"]
    for variant in variants:
        if variant.lower() in text_lower:
            for sr in strong_recommend:
                if sr in full_response:
                    score += 1
                    break
            break

    return max(1, min(10, score))


# ==========================================
# 统一入口：extract_insights
# ==========================================

def extract_insights(
    full_response: str,
    client_brand: str,
    known_competitors: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    从单条 AI 回复中提取全部蒸馏洞察

    Args:
        full_response: AI 平台的原始完整回复
        client_brand: 客户品牌名称
        known_competitors: 可选，已知竞品列表

    Returns:
        {
            "brands_mentioned": [...],
            "client_rank": int | None,
            "client_rank_type": str | None,
            "urls_cited": [...],
            "keywords_used": [...],
            "sentiment": "positive" | "neutral" | "negative",
            "recommendation_strength": 1-10,
            "raw_structure": {...},
            "extraction_method": "regex" | "template",
            "extraction_confidence": 0.0-1.0,
        }
    """
    if not full_response:
        return {
            "brands_mentioned": [],
            "client_rank": None,
            "client_rank_type": None,
            "urls_cited": [],
            "keywords_used": [],
            "sentiment": "neutral",
            "recommendation_strength": 0,
            "raw_structure": {},
            "extraction_method": "regex",
            "extraction_confidence": 0.0,
        }

    # 1. 品牌提取
    all_known = list(known_competitors or [])
    if client_brand and client_brand not in all_known:
        all_known.append(client_brand)
    brands = extract_brands(full_response, known_brands=all_known if all_known else None)

    # 2. 排名计算
    rank, rank_type = find_brand_rank(brands, client_brand, full_response)

    # 3. URL 提取
    urls = extract_urls(full_response)

    # 4. 结构分析
    structure = analyze_structure(full_response)

    # 5. 情感分析
    sentiment_result = analyze_sentiment(full_response, client_brand)

    # 6. 推荐强度
    strength = assess_recommendation_strength(
        full_response, client_brand, rank, sentiment_result["label"]
    )

    # 7. 关键词提取（简单：提取频繁出现的 2-4 字词）
    keywords = _extract_frequent_terms(full_response)

    # 8. 判断提取方法和置信度
    method, confidence = _assess_extraction_quality(
        brands, rank, rank_type, structure
    )

    return {
        "brands_mentioned": brands,
        "client_rank": rank,
        "client_rank_type": rank_type,
        "urls_cited": urls,
        "keywords_used": keywords,
        "sentiment": sentiment_result["label"],
        "recommendation_strength": strength,
        "raw_structure": structure,
        "extraction_method": method,
        "extraction_confidence": confidence,
    }


def _extract_frequent_terms(text: str, top_n: int = 10) -> List[str]:
    """提取高频关键词（简单 N-gram 方式）"""
    if not text:
        return []

    # 清理文本
    clean = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', ' ', text)
    clean = re.sub(r'\s+', ' ', clean).strip()

    # 2-4 字中文词提取
    terms = re.findall(r'[\u4e00-\u9fa5]{2,4}', clean)

    # 统计词频
    freq = {}
    stop_words = {
        "可以", "进行", "通过", "使用", "这个", "那个", "什么", "如何",
        "需要", "能够", "以及", "对于", "其中", "这些", "那些", "但是",
        "因为", "所以", "如果", "就是", "不是", "已经", "目前", "以下",
        "包括", "一般", "比较", "一个", "主要", "方面", "一些", "提供",
    }
    for term in terms:
        if term not in stop_words:
            freq[term] = freq.get(term, 0) + 1

    # 返回高频词
    sorted_terms = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    return [t[0] for t in sorted_terms[:top_n] if t[1] >= 2]


def _assess_extraction_quality(
    brands: List[str],
    rank: Optional[int],
    rank_type: Optional[str],
    structure: Dict[str, Any]
) -> Tuple[str, float]:
    """
    评估提取质量，返回 (method, confidence)

    - 有列表结构 + 品牌名清晰 → regex, 高置信
    - 无列表 + 品牌名模糊 → 可能需要 LLM（标记低置信）
    """
    confidence = 0.5  # 基础置信度

    # 结构化回复（有列表）→ 提取更可靠
    if structure.get("has_list"):
        confidence += 0.2
    if structure.get("has_bold"):
        confidence += 0.1

    # 品牌提取数量合理（1-10 个）
    if 1 <= len(brands) <= 10:
        confidence += 0.1
    elif len(brands) == 0:
        confidence -= 0.2

    # 排名类型
    if rank_type == "explicit":
        confidence += 0.15
    elif rank_type == "positional":
        confidence += 0.05

    # 限制范围
    confidence = max(0.1, min(1.0, round(confidence, 2)))

    # 低置信度标记为需要 LLM（预留）
    method = "regex" if confidence >= 0.5 else "regex_low_confidence"

    return method, confidence
