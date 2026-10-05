"""
关键词竞争度分析模块 V3 - LLM内容分类 + 秘塔搜索API

核心逻辑：
1. 调用秘塔搜索API获取最多100条网页结果
2. LLM内容分类：将每条结果分为A/B/C/D四类
   A类：推广文/SEO竞品（排名、推荐、哪家好、榜单、评测 = 直接竞争对手）
   B类：行业资讯/新闻报道（正规媒体的行业文章）
   C类：百科/知识内容（知识性、教育性内容）
   D类：无关/噪声（与关键词无实质关联的内容）
3. 仅A类内容计入竞争对手数量，用于定价

秘塔API返回的关键字段：
- title: 标题
- link: 链接
- score: 相关度评分 (high/medium/low)
- snippet/summary: 摘要
- position: 排名位置
- date: 发布日期
- authors: 作者（可选）
"""
import asyncio
import aiohttp
import re
import os
import json
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv
from tools.llm_call_tracker import llm_track
from tools.metaso_health import metaso_result_error, record_metaso_result

load_dotenv('geo_agentscope/.env')
load_dotenv('.env')  # 兼容从 geo_agentscope 目录运行


# ========================================
# 配置
# ========================================

METASO_API_KEY = os.getenv("METASO_API_KEY", "")
METASO_ENDPOINT = "https://metaso.cn/api/v1/search"

# 高权重平台（AI引用概率更高的来源）
HIGH_WEIGHT_DOMAINS = {
    # 科技/商业媒体
    "36kr.com": "科技媒体",
    "iyiou.com": "亿欧网",
    "jiqizhixin.com": "机器之心",
    # 新闻门户
    "sina.cn": "新浪",
    "sina.com.cn": "新浪",
    "163.com": "网易",
    "sohu.com": "搜狐",
    "qq.com": "腾讯新闻",
    # 社媒
    "zhihu.com": "知乎",
    "bilibili.com": "B站",
    # 百科
    "baike.baidu.com": "百度百科",
    "baidu.com": "百度",
    # 博客
    "cnblogs.com": "博客园",
    "csdn.net": "CSDN",
}

# 竞争内容识别模式
COMPETITION_PATTERNS = [
    r"TOP\s*\d+", r"排行榜", r"排名", r"推荐",
    r"哪家好", r"怎么选", r"服务商", r"公司推荐",
    r"榜单", r"评测", r"对比"
]


# ========================================
# 秘塔 API 调用
# ========================================

async def search_metaso(query: str, size: int = 100) -> dict:  # [E2 2026-06-05] 默认100消除 latent size=50 回归面(payload min(size,100)封顶)
    """
    调用秘塔搜索API
    
    Args:
        query: 搜索关键词
        size: 返回结果数量（最多100）
    
    Returns:
        搜索结果字典
    """
    # [并发-2 2026-06-10] 多源限流池:round-robin 取一个有配额的 metaso key(多账号自动轮询·总配额=200×账号数 QPM)。
    #   超速 → 返回降级标记(不排队),上层 score_keywords 走经验兜底 estimate_competition_from_keyword。
    #   env 配 METASO_API_KEYS=k1,k2,k3 即自动分流;回落单 METASO_API_KEY 时行为不变。
    from tools.api_source_pool import get_metaso_pool, acall_with_failover
    _ms_pool = get_metaso_pool()
    if _ms_pool.size == 0:
        return {"error": "METASO_API_KEY not configured"}

    payload = {
        "q": query,
        "scope": "webpage",
        "size": min(size, 100),
        "includeSummary": True,         # 通过网页的摘要信息进行召回增强
        "conciseSnippet": True,         # 返回精简的原文匹配信息
        "includeRawContent": False,
    }

    # [failover 2026-06-11] 双保险:分流(轮询多账号)+ 容错(某账号失败/被封 → 自动换下一个账号重试·试遍全部)
    async def _do(_ms_key):
        headers = {"Authorization": f"Bearer {_ms_key}", "Content-Type": "application/json"}
        async with aiohttp.ClientSession() as session:
            async with llm_track(
                "metaso_competition_search", "metaso", model="search",
                metadata={"size": min(size, 100)},
            ) as tracker:
                async with session.post(
                    METASO_ENDPOINT, headers=headers, json=payload,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        # [止血 2026-08-04] HTTP 200 不等于业务成功:秘塔余额不足等业务级失败
                        #   走的就是 200 通道(body 里 errCode=3000)。只看 status 会:
                        #   记假绿 success=True + 不触发 failover(断血账号不切备用)
                        #   + 上层守卫判不出 → 竞争强度静默落地板值 1。
                        biz_err = metaso_result_error(data)
                        if biz_err:
                            tracker.record(success=False, error_msg=f"业务级失败: {biz_err}"[:200])
                            record_metaso_result(False, biz_err, source="competition_search")
                            raise RuntimeError(f"metaso 业务级失败: {biz_err}")  # raise → failover 换下一个账号
                        tracker.record(success=True)
                        record_metaso_result(True, source="competition_search")
                        return data
                    text = await response.text()
                    tracker.record(success=False, error_msg=f"HTTP {response.status}: {text[:200]}")
                    raise RuntimeError(f"Status {response.status}: {text[:200]}")  # raise → failover 换下一个账号

    try:
        # throttle=True:metaso 带 QPM 闸·跳过超速账号;primary_first=True:原 key(加过 QPS)优先吃满·满/失败才轮备用
        # 全部账号超速/失败 → 降级走经验兜底
        return await acall_with_failover(_ms_pool, _do, throttle=True, primary_first=True)
    except Exception as e:
        return {"error": str(e), "degraded": True}


# ========================================
# 数据蒸馏（无LLM成本）
# ========================================

def distill_result(result: dict) -> dict:
    """
    蒸馏单条搜索结果，提取竞争相关信息
    """
    url = result.get("link", result.get("url", ""))
    title = result.get("title", "")
    snippet = result.get("snippet", result.get("summary", ""))
    score = result.get("score", "medium")  # high/medium/low
    position = result.get("position", 99)
    date_str = result.get("date", "")
    
    # 判断来源权重
    domain = ""
    platform = "其他"
    is_high_weight = False
    if "://" in url:
        domain = url.split("://")[1].split("/")[0]
        for d, name in HIGH_WEIGHT_DOMAINS.items():
            if d in domain:
                is_high_weight = True
                platform = name
                break
    
    # 判断是否是竞品内容
    text = title + " " + snippet
    is_competition = any(re.search(p, text) for p in COMPETITION_PATTERNS)
    
    # 相关度得分
    score_map = {"high": 3, "medium": 2, "low": 1}
    score_value = score_map.get(score, 2)
    
    # 计算具体距今天数
    days_since_publish = None
    if date_str:
        try:
            if "年" in date_str:
                date_match = re.search(r"(\d{4})年(\d{1,2})月", date_str)
                if date_match:
                    year, month = int(date_match.group(1)), int(date_match.group(2))
                    content_date = datetime(year, month, 1)
                    days_since_publish = (datetime.now() - content_date).days
            elif "-" in date_str:
                content_date = datetime.strptime(date_str[:10], "%Y-%m-%d")
                days_since_publish = (datetime.now() - content_date).days
        except:
            pass
    
    is_recent = days_since_publish is not None and days_since_publish <= 90  # 3个月内
    
    return {
        "url": url,
        "title": title[:80],
        "domain": domain,
        "platform": platform,
        "score": score,
        "score_value": score_value,
        "position": position,
        "is_high_weight": is_high_weight,
        "is_competition": is_competition,
        "is_recent": is_recent,
        "days_since_publish": days_since_publish,  # 新增：具体天数
    }


async def classify_results_with_llm(
    keyword: str,
    results: list[dict],
    max_results: int = 30,
) -> list[dict]:
    """
    用LLM对秘塔搜索结果进行ABCD分类

    A类：推广文/SEO竞品 - 直接竞争对手（排名、推荐、哪家好、榜单、评测、对比文）
    B类：行业资讯/新闻 - 正规媒体的行业文章，非推广性质
    C类：百科/知识 - 教育性、知识性内容（百科、教程、定义解释）
    D类：无关/噪声 - 与关键词无实质关联或低质量内容

    Args:
        keyword: 搜索关键词
        results: distill_result处理后的结果列表
        max_results: 最多分类多少条（控制LLM成本）

    Returns:
        带category字段的结果列表
    """
    from tools.multi_llm_caller import call_llm_with_fallback

    # 只取前N条进行LLM分类，剩余的用规则兜底
    to_classify = results[:max_results]
    remaining = results[max_results:]

    if not to_classify:
        return results

    # 构造LLM输入：编号 + 标题 + 域名
    items_text = "\n".join(
        f"{i+1}. [{r['domain']}] {r['title']}"
        for i, r in enumerate(to_classify)
    )

    prompt = f"""你是SEO内容分类专家。用户搜索了「{keyword}」，以下是搜索引擎返回的网页结果。
请将每条结果**严格**分类为A/B/C/D四类。注意：A类标准非常严格，只有明确为推广目的创作的内容才算A类。

**A类（推广/SEO竞品）**——必须同时满足：
  ① 内容的核心目的是为某个品牌/公司做推广或引流
  ② 属于以下5种文体之一：
     - 推荐盘点文："XX推荐"、"XX公司推荐"、"为你推荐N家"
     - 深度测评文："XX对比评测"、"XX打分"、"多维度评估"
     - 选购指南文："XX怎么选"、"XX哪家好"、"选择指南"
     - 权威榜单文："TOP N"、"XX排行榜"、"XX排名"
     - 排名对比文："XX前十"、"XX榜单"、"哪家强"
  ③ 不是客观新闻报道，而是有明显的推广意图

**B类（行业资讯/新闻）**：
  - 正规媒体发布的行业新闻、市场分析、趋势报道
  - 虽然可能提到某些公司，但主要目的是报道而非推广
  - 政策解读、行业动态、市场数据报告

**C类（百科/知识/教程）**：
  - 百科词条、知识科普、操作教程、概念解释
  - 流程说明（如"XX流程"、"XX步骤"、"XX攻略"中的纯知识性内容）
  - 没有推荐具体品牌/公司的教育性内容

**D类（无关/噪声）**：
  - 与搜索关键词无实质关联
  - 低质量页面、导航页、登录页
  - 完全无法判断内容的结果

⚠️ 分类原则：宁可把不确定的分为B/C类，也不要轻易分为A类。只有你确信是为推广目的而创作的内容才分为A类。

搜索结果：
{items_text}

请严格按JSON数组格式返回每条的分类，不要任何其他文字：
[{{"id": 1, "cat": "A"}}, {{"id": 2, "cat": "B"}}, ...]"""

    try:
        response = await call_llm_with_fallback(prompt, verbose=False)

        # 提取JSON
        json_match = re.search(r'\[.*\]', response, re.DOTALL)
        if not json_match:
            raise ValueError("No JSON array in LLM response")

        classifications = json.loads(json_match.group())

        # 构建ID到分类的映射
        cat_map = {}
        for item in classifications:
            idx = item.get("id", 0) - 1  # 1-based to 0-based
            cat = item.get("cat", "D").upper()
            if cat not in ("A", "B", "C", "D"):
                cat = "D"
            if 0 <= idx < len(to_classify):
                cat_map[idx] = cat

        # 应用分类
        for i, r in enumerate(to_classify):
            r["category"] = cat_map.get(i, "D")
            r["is_competition"] = r["category"] == "A"

        # 剩余的用规则兜底
        for r in remaining:
            text = r["title"]
            if any(re.search(p, text) for p in COMPETITION_PATTERNS):
                r["category"] = "A"
                r["is_competition"] = True
            else:
                r["category"] = "B"
                r["is_competition"] = False

        return to_classify + remaining

    except Exception as e:
        print(f"  LLM内容分类失败: {e}，回退到规则分类")
        # 回退到原有规则
        for r in results:
            text = r["title"]
            if any(re.search(p, text) for p in COMPETITION_PATTERNS):
                r["category"] = "A"
                r["is_competition"] = True
            else:
                r["category"] = "B"
                r["is_competition"] = False
        return results


async def batch_classify_results_with_llm(
    keyword_results: dict[str, list[dict]],
    max_results_per_kw: int = 30,
    batch_size: int = 8,
) -> dict[str, list[dict]]:
    """
    批量LLM分类：将多个关键词的搜索结果合并到一次LLM调用中

    相比逐词调用 classify_results_with_llm()，50个关键词从50次LLM调用降到7次。

    Args:
        keyword_results: {关键词: distilled结果列表}
        max_results_per_kw: 每个关键词最多分类多少条
        batch_size: 每次LLM调用包含多少个关键词（8个 × 30条 = 240条，prompt可控）

    Returns:
        {关键词: 带category字段的结果列表}
    """
    from tools.multi_llm_caller import call_llm_with_fallback

    keywords = list(keyword_results.keys())
    all_classified = {}

    # 分批：每batch_size个关键词一组
    batches = [keywords[i:i + batch_size] for i in range(0, len(keywords), batch_size)]

    async def classify_batch(batch_kws: list[str]) -> dict[str, list]:
        """单批次LLM分类"""
        # 构造多关键词prompt
        sections = []
        kw_meta = {}  # 记录每个关键词的 to_classify / remaining 切分
        for kw in batch_kws:
            results = keyword_results[kw]
            to_classify = results[:max_results_per_kw]
            remaining = results[max_results_per_kw:]
            kw_meta[kw] = {"to_classify": to_classify, "remaining": remaining}

            if not to_classify:
                continue

            items_text = "\n".join(
                f"  {i+1}. [{r['domain']}] {r['title']}"
                for i, r in enumerate(to_classify)
            )
            sections.append(f'=== 关键词: "{kw}" ===\n{items_text}')

        if not sections:
            # 全部为空，规则兜底
            batch_result = {}
            for kw in batch_kws:
                for r in keyword_results[kw]:
                    text = r["title"]
                    if any(re.search(p, text) for p in COMPETITION_PATTERNS):
                        r["category"] = "A"
                        r["is_competition"] = True
                    else:
                        r["category"] = "B"
                        r["is_competition"] = False
                batch_result[kw] = keyword_results[kw]
            return batch_result

        combined_text = "\n\n".join(sections)

        prompt = f"""你是SEO内容分类专家。以下是多个关键词的搜索结果，请分别对每个关键词的结果进行ABCD分类。

**A类（推广/SEO竞品）**——必须同时满足：
  ① 内容的核心目的是为某个品牌/公司做推广或引流
  ② 属于以下文体之一：推荐盘点、深度测评、选购指南、权威榜单、排名对比
  ③ 不是客观新闻报道，而是有明显的推广意图

**B类（行业资讯/新闻）**：正规媒体发布的行业新闻、市场分析、趋势报道

**C类（百科/知识/教程）**：百科词条、知识科普、操作教程、概念解释

**D类（无关/噪声）**：与搜索关键词无实质关联或低质量内容

⚠️ 宁可分为B/C类，也不要轻易分为A类。只有确信是推广目的内容才分为A类。

{combined_text}

请严格按JSON对象格式返回，key为关键词，value为分类数组：
{{{", ".join(f'"{kw}": [{{"id": 1, "cat": "A"}}, ...]' for kw in batch_kws if kw_meta[kw]["to_classify"])}}}"""

        try:
            response = await call_llm_with_fallback(prompt, verbose=False)

            # 提取JSON对象
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if not json_match:
                raise ValueError("No JSON object in LLM response")

            classifications = json.loads(json_match.group())

            batch_result = {}
            for kw in batch_kws:
                meta = kw_meta[kw]
                to_classify = meta["to_classify"]
                remaining = meta["remaining"]

                # 从LLM结果中找到匹配的分类
                kw_cats = classifications.get(kw, [])
                cat_map = {}
                for item in kw_cats:
                    idx = item.get("id", 0) - 1
                    cat = item.get("cat", "D").upper()
                    if cat not in ("A", "B", "C", "D"):
                        cat = "D"
                    if 0 <= idx < len(to_classify):
                        cat_map[idx] = cat

                for i, r in enumerate(to_classify):
                    r["category"] = cat_map.get(i, "D")
                    r["is_competition"] = r["category"] == "A"

                for r in remaining:
                    text = r["title"]
                    if any(re.search(p, text) for p in COMPETITION_PATTERNS):
                        r["category"] = "A"
                        r["is_competition"] = True
                    else:
                        r["category"] = "B"
                        r["is_competition"] = False

                batch_result[kw] = to_classify + remaining

            return batch_result

        except Exception as e:
            print(f"  批量LLM分类失败: {e}，回退到规则分类")
            batch_result = {}
            for kw in batch_kws:
                for r in keyword_results[kw]:
                    text = r["title"]
                    if any(re.search(p, text) for p in COMPETITION_PATTERNS):
                        r["category"] = "A"
                        r["is_competition"] = True
                    else:
                        r["category"] = "B"
                        r["is_competition"] = False
                batch_result[kw] = keyword_results[kw]
            return batch_result

    # 并行执行所有批次的LLM调用
    batch_tasks = [classify_batch(b) for b in batches]
    batch_results = await asyncio.gather(*batch_tasks)

    for br in batch_results:
        all_classified.update(br)

    return all_classified


def analyze_competition(distilled_results: list[dict]) -> dict:
    """
    基于蒸馏结果分析竞争度
    """
    total = len(distilled_results)
    if total == 0:
        return {
            "competition_level": "蓝海",
            "competition_score": 0,
            "recommendation": "市场空白，基础价即可",
            "total_results": 0
        }
    
    # 统计关键指标
    high_weight_count = sum(1 for r in distilled_results if r["is_high_weight"])
    competition_count = sum(1 for r in distilled_results if r["is_competition"])
    recent_count = sum(1 for r in distilled_results if r["is_recent"])
    high_score_count = sum(1 for r in distilled_results if r["score"] == "high")
    
    # 平台分布
    platform_dist = {}
    for r in distilled_results:
        p = r["platform"]
        platform_dist[p] = platform_dist.get(p, 0) + 1
    
    # 计算竞争度评分（0-100）
    # 基于结果数量、质量、平台权重综合计算
    competition_score = min(100, (
        min(total, 50) * 0.5 +            # 结果数量（最多50条贡献25分）
        high_weight_count * 3 +            # 高权重平台每个3分
        competition_count * 2 +            # 竞品内容每个2分
        recent_count * 1.5 +               # 近期内容每个1.5分
        high_score_count * 2               # 高相关度每个2分
    ))
    
    # 判断竞争等级
    if competition_score < 20:
        level = "蓝海"
        rec = "市场空白，容易上榜，基础价即可"
        coeff = 1.0
    elif competition_score < 35:
        level = "低竞争"
        rec = "竞争较少，稳定投入可见效"
        coeff = 1.2
    elif competition_score < 50:
        level = "中等竞争"
        rec = "存在竞争，需要持续优质内容"
        coeff = 1.4
    elif competition_score < 70:
        level = "激烈竞争"
        rec = "竞争较激烈，需要多平台布局"
        coeff = 1.7
    else:
        level = "红海"
        rec = "竞争极其激烈，建议评估ROI"
        coeff = 2.0
    
    return {
        "competition_level": level,
        "competition_score": round(competition_score),
        "competition_coefficient": coeff,
        "total_results": total,
        "high_weight_count": high_weight_count,
        "competition_count": competition_count,
        "recent_count": recent_count,
        "high_score_count": high_score_count,
        "platform_distribution": platform_dist,
        "recommendation": rec,
    }


# ========================================
# 主函数：关键词竞争度分析
# ========================================

async def analyze_keyword_competition(keyword: str, base_price: int = 300) -> dict:
    """
    分析单个关键词的竞争度并建议定价
    """
    # 1. 搜索 · [E2 2026-06-05] size 50→100 与主算价路径(keyword_value_scorer:202)统一 · payload 已 min(size,100) 封顶
    search_result = await search_metaso(keyword, size=100)
    
    if "error" in search_result:
        return {
            "keyword": keyword,
            "error": search_result["error"],
            "competition": {"competition_level": "未知", "competition_score": 0},
            "pricing": {"suggested_price": base_price}
        }
    
    webpages = search_result.get("webpages", [])
    
    # 2. 蒸馏
    distilled = [distill_result(r) for r in webpages]
    
    # 3. 分析竞争度
    competition = analyze_competition(distilled)
    
    # 4. 计算定价
    suggested_price = int(base_price * competition["competition_coefficient"])
    
    return {
        "keyword": keyword,
        "search_results": len(webpages),
        "competition": competition,
        "pricing": {
            "base_price": base_price,
            "competition_coefficient": competition["competition_coefficient"],
            "suggested_price": suggested_price,
            "difficulty": competition["competition_level"]
        },
        "top_competitors": [
            {
                "title": d["title"],
                "platform": d["platform"],
                "score": d["score"],
                "is_competition": d["is_competition"]
            }
            for d in distilled[:10]
        ]
    }


async def batch_analyze_keywords(
    keywords: list[str],
    base_price: int = 300,
    concurrency: int = 3
) -> list[dict]:
    """
    批量分析关键词竞争度（并行）
    """
    semaphore = asyncio.Semaphore(concurrency)
    
    async def analyze_with_semaphore(kw):
        async with semaphore:
            return await analyze_keyword_competition(kw, base_price)
    
    tasks = [analyze_with_semaphore(kw) for kw in keywords]
    results = await asyncio.gather(*tasks)
    return results


# ========================================
# 测试
# ========================================

if __name__ == "__main__":
    import json
    
    async def main():
        keywords = [
            "GEO优化公司推荐",
            "深圳AI搜索优化公司哪家好",
            "社媒搜索优化怎么做",
        ]
        
        print("=" * 70)
        print("🔍 关键词竞争度分析 V2 - 基于秘塔搜索")
        print("=" * 70)
        
        results = await batch_analyze_keywords(keywords, base_price=400)
        
        for r in results:
            print(f"\n📌 {r['keyword']}")
            if "error" in r:
                print(f"   ❌ 错误: {r['error']}")
                continue
            c = r['competition']
            p = r['pricing']
            print(f"   搜索结果: {r['search_results']}条")
            print(f"   竞争度: {c['competition_level']} ({c['competition_score']}分)")
            print(f"   高权重来源: {c['high_weight_count']}条")
            print(f"   竞品内容: {c['competition_count']}条")
            print(f"   近期发布: {c['recent_count']}条")
            print(f"   平台分布: {c['platform_distribution']}")
            print(f"   💰 ¥{p['base_price']} × {p['competition_coefficient']}x = ¥{p['suggested_price']}")
        
        # 保存结果
        with open("competition_analysis_v2.json", "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print("\n✅ 详细结果已保存到 competition_analysis_v2.json")
    
    asyncio.run(main())
