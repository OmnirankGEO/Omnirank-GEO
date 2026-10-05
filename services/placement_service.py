"""
媒体投放建议服务
根据文章内容、AI引擎偏好、媒体价格，给出优先投放建议
"""

import json
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH
import logging
import os
import re
import uuid
import asyncio
import httpx
import pandas as pd
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Dict, Optional, Any

# WO_267:行业大类字典 SSOT —— 本模块 ①媒体词 / ②调研行业 / ③头部白名单 三处都从这里取
from services.industry_taxonomy import (
    OTHER_KEY, get_category, head_fallback_key_for, media_keywords_for, media_keywords_table,
    research_target, resolve_industry,
)

logger = logging.getLogger(__name__)

# ==========================================================================
# AI引擎偏好矩阵（2026.03 基于量子位智库、白杨SEO实测、博查API数据）
# 数据来源：量子位智库30题*3prompt*7引擎=630有效数据、白杨SEO引用分析
# ==========================================================================

# 四大核心引擎市占率权重（用于计算加权覆盖分）
# 豆包MAU 2.26亿(#1)，Kimi ~2.6亿(#1-#2)，DeepSeek ~1.35亿(#3)，千问(增长中)
ENGINE_WEIGHTS = {
    "豆包": 0.35,      # 字节系，市占率最高
    "Kimi": 0.25,      # Moonshot，长文本场景强
    "DeepSeek": 0.22,  # 推理/编程场景强，增长快
    "千问": 0.18,      # 阿里系，与阿里生态绑定
}

# AI引擎-平台偏好矩阵（早期三方数据，仅供参考）
# 注意：实际投放评分优先使用 get_industry_engine_scores() 从 geo_engine_stats 读取的真实调研数据
# 此矩阵仅在无调研数据的行业中作为兜底参考，不直接参与评分计算
AI_PLATFORM_PREFERENCES = {
    "豆包": {
        # 50-60%引用来自字节系（今日头条+抖音+抖音百科）
        "top_sources": {
            "今日头条": 0.22,     # 头条号是核心引用源
            "抖音": 0.25,         # 短视频转文字引用
            "知乎": 0.10,
            "搜狐": 0.08,
            "网易": 0.07,
            "澎湃新闻": 0.05,
            "CSDN": 0.05,
            "百度百科": 0.04,
            "B站": 0.03,
        },
        "avg_citations": 8,
        "ecosystem": "字节系",
        "strategy": "字节生态优先（头条+抖音占~50%），其次UGC社区和门户"
    },
    "Kimi": {
        # 知乎占25-30%，UGC占70%，无生态偏向
        "top_sources": {
            "知乎": 0.28,         # 最大单一来源
            "CSDN": 0.15,
            "澎湃新闻": 0.08,
            "新浪": 0.07,
            "36氪": 0.06,
            "博客园": 0.05,
            "B站": 0.05,
            "百度百科": 0.04,
            "掘金": 0.03,
            "微信公众号": 0.03,
        },
        "avg_citations": 12,
        "ecosystem": None,
        "strategy": "UGC+新闻双驱动，知乎占比最高，引用精准度最好"
    },
    "DeepSeek": {
        # 新闻媒体25-30%，知乎15-20%，无生态偏向
        "top_sources": {
            "知乎": 0.18,
            "澎湃新闻": 0.10,
            "新浪": 0.08,
            "网易新闻": 0.07,
            "CSDN": 0.10,
            "界面新闻": 0.06,
            "36氪": 0.05,
            "百度百科": 0.05,
            "博客园": 0.03,
            "B站": 0.03,
        },
        "avg_citations": 30,
        "ecosystem": None,
        "strategy": "引用量最大(20-50条)，偏好权威新闻+行业垂直站+UGC"
    },
    "千问": {
        # 新闻门户60%+，阿里生态处理交易类查询
        "top_sources": {
            "网易新闻": 0.14,
            "搜狐": 0.10,
            "新浪财经": 0.09,
            "知乎": 0.10,
            "澎湃新闻": 0.07,
            "腾讯网": 0.08,
            "36氪": 0.05,
            "CSDN": 0.04,
            "今日头条": 0.04,
            "百度百科": 0.03,
        },
        "avg_citations": 7,
        "ecosystem": "阿里系",
        "strategy": "新闻门户为主(60%+)，深度搜索模式才显示引用"
    },
}

# 引擎别名归一 SQL · 跟 api/research_monitor_citations_api.py:_ENGINE_NORMALIZE_SQL 同口径
# raw.engine 历史脏数据有 8 种别名 (doubao/豆包/kimi/Kimi/deepseek/DeepSeek/qwen/千问)
# 直接 COUNT(DISTINCT engine) 会算出 5-8 · 实际真引擎数 ≤ 4 · 必须 CASE 归一
# 2026-06 老板复核 placement_service 4 处 COUNT(DISTINCT engine) 漏归一时加 · 跟 P14-v5
# total_engines 修过的那次同款 bug · 但当时只修了 1 处 · 这次补完剩 4 处
_ENGINE_NORMALIZE_SQL = """
    CASE
        WHEN LOWER(engine) IN ('doubao', '豆包') THEN '豆包'
        WHEN LOWER(engine) IN ('kimi') THEN 'Kimi'
        WHEN LOWER(engine) IN ('deepseek') THEN 'DeepSeek'
        WHEN LOWER(engine) IN ('qwen', '千问') THEN '千问'
        ELSE engine
    END
"""


# 媒体平台 → 覆盖引擎映射（兜底数据，当geo_engine_stats无行业数据时使用）
# 基于GEO调研7,557条真实引用记录(2026-03)更新
# score = 基于实测引用率的加权分数（引用率×覆盖引擎数×引擎权重）
# 知乎实测79.6%引用率 | 搜狐45.6% | 今日头条47.8% | 新浪48.1% | 抖音61.3%
PLATFORM_ENGINE_COVERAGE = {
    "知乎":     {"engines": ["豆包", "Kimi", "DeepSeek", "千问"], "score": 32.0, "type": "UGC社区"},
    "抖音":     {"engines": ["豆包", "Kimi", "DeepSeek"],         "score": 22.0, "type": "字节系"},
    "搜狐":     {"engines": ["豆包", "Kimi", "DeepSeek", "千问"], "score": 18.0, "type": "门户"},
    "今日头条":  {"engines": ["豆包", "DeepSeek", "千问"],         "score": 17.0, "type": "字节系"},
    "新浪":     {"engines": ["Kimi", "DeepSeek", "千问"],         "score": 15.0, "type": "门户"},
    "百度":     {"engines": ["豆包", "Kimi", "DeepSeek", "千问"], "score": 14.0, "type": "搜索引擎"},
    "网易新闻":  {"engines": ["豆包", "DeepSeek", "千问"],         "score": 10.0, "type": "门户"},
    "CSDN":     {"engines": ["豆包", "Kimi", "DeepSeek", "千问"], "score": 9.5,  "type": "技术社区"},
    "什么值得买":{"engines": ["豆包", "Kimi", "DeepSeek"],         "score": 8.0,  "type": "消费社区"},
    "B站":      {"engines": ["豆包", "Kimi", "DeepSeek"],         "score": 5.5,  "type": "视频UGC"},
    "澎湃新闻":  {"engines": ["豆包", "Kimi", "DeepSeek", "千问"], "score": 6.3,  "type": "权威媒体"},
    "腾讯网":    {"engines": ["豆包", "千问"],                     "score": 5.0,  "type": "门户"},
    "36氪":     {"engines": ["Kimi", "DeepSeek", "千问"],         "score": 3.5,  "type": "科技商业"},
    "百度百科":  {"engines": ["豆包", "Kimi", "DeepSeek", "千问"], "score": 3.2,  "type": "百科"},
    "微信公众号": {"engines": ["Kimi", "千问"],                    "score": 2.5,  "type": "自媒体"},
    "博客园":   {"engines": ["Kimi", "DeepSeek"],                 "score": 1.9,  "type": "技术社区"},
    "掘金":     {"engines": ["Kimi"],                             "score": 0.8,  "type": "技术社区"},
    "界面新闻":  {"engines": ["DeepSeek"],                        "score": 1.3,  "type": "财经媒体"},
    "新浪财经":  {"engines": ["千问"],                             "score": 1.6,  "type": "财经媒体"},
}

class PlacementService:
    """投放建议核心服务"""

    def get_completed_articles(self, quote_id: int) -> List[Dict]:
        """获取项目下所有已完成文章"""
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute('''
                -- [Review-CTO 2026-07-27 · Owner:发布中心每次加载都很慢] 列表不再
                -- 携带正文列:全仓核过零消费方(PublishCenter 映射不含正文,
                -- 详情/预览走单独接口;PlacementRecommendation/generate_recommendations
                -- 也不用)。此前每篇 5-15k 字全文 × N 篇进列表 + 逐篇正则清洗 + JSON
                -- 传输,全是白扔的开销。
                SELECT t.id as topic_id, t.optimized_title as title,
                       t.original_keyword as keyword,
                       t.article_style as article_style,
                       COALESCE(a.style_code, t.style_code, t.article_style) as style_code,
                       -- [#185] 方向取**题上的** user_choice。
                       -- 🔴 不能用上面那个 style_code 派生:防御型与普通
                       --    company_facts 篇的 style_code **逐字相同**
                       --    (都是 brand_softarticle —— 两者共用同一套六段模板,
                       --     这是有意的)。用 style_code 派生会把两者合并,
                       --    而合并之后没有任何东西会报错,只是筛选筛不出来。
                       t.user_choice as user_choice,
                       a.style_family, t.article_id, t.status,
                       a.word_count, a.created_at as article_created_at,
                       a.article_review_status, a.article_human_review_status,
                       a.article_review, a.evidence_manifest_hash,
                       a.publication_profile, a.platform_review
                FROM topics t
                LEFT JOIN articles a ON a.id = t.article_id
                WHERE t.quote_id = %s AND t.status = 'completed'
                ORDER BY t.original_keyword, t.id
            ''', (quote_id,))
            rows = [dict(r) for r in c.fetchall()]

            # [#185] 文章级「是不是防御型」——**由所属题的 user_choice 派生**,
            # 不由 style_code 派生(见上面那段 SQL 注释)。发布中心的筛选 chip
            # 读这一个布尔,不去自己解析 user_choice 的取值。
            for row in rows:
                row["is_defensive"] = (
                    str(row.get("user_choice") or "") == "defensive_company")

            # Also get existing recommendations
            c.execute('''
                SELECT topic_id, recommended_outlets, total_estimated_cost,
                       strategy_tier, llm_analysis, created_at
                FROM placement_recommendations
                WHERE quote_id = %s
                ORDER BY created_at DESC
            ''', (quote_id,))
            recs = {}
            for r in c.fetchall():
                tid = r['topic_id']
                if tid not in recs:  # keep latest
                    recs[tid] = dict(r)

            from services.article_review_gate import evaluate_publication_eligibility
            for row in rows:
                article_id = row.get("article_id")
                try:
                    eligibility = evaluate_publication_eligibility(int(article_id), cursor=c)
                except Exception as exc:
                    eligibility = {
                        "eligible": False,
                        "reason": "review_state_unavailable",
                        "message": f"文章审核状态暂不可用：{exc}",
                        # 判不出来就 fail-closed 当硬门(不可用 != 可发)。
                        "review_state": "not_run",
                        "advisory_state": "none",
                        "advisory_open_count": 0,
                        "publication_h0_state": "operator_hard",
                    }
                row["publication_eligible"] = bool(eligibility.get("eligible"))
                row["publication_eligibility_reason"] = eligibility.get("reason")
                row["publication_eligibility_message"] = eligibility.get("message")
                # [三态拆分 2026-07-31 · §4.1] 发布中心(PublishCenter)就吃这个端点,
                # 三态必须一起给,否则徽章只能靠 article_review_status 猜。
                row["review_state"] = eligibility.get("review_state")
                row["advisory_state"] = eligibility.get("advisory_state")
                row["advisory_open_count"] = eligibility.get("advisory_open_count")
                row["publication_h0_state"] = eligibility.get("publication_h0_state")
        finally:
            conn.close()

        # [2026-07-27] 列表已不带正文,清洗随之取消——清洗属于详情/发布路径
        # (保存链三处已接 sanitize,详情接口自带清洗),列表层重复清洗纯烧 CPU。
        for row in rows:
            rec = recs.get(row['topic_id'])
            if rec:
                row['recommendation'] = {
                    'outlets': json.loads(rec['recommended_outlets']) if rec['recommended_outlets'] else [],
                    'total_cost': rec['total_estimated_cost'],
                    'tier': rec['strategy_tier'],
                    'analysis': rec['llm_analysis'],
                    'generated_at': rec['created_at']
                }
            else:
                row['recommendation'] = None

        return rows

    def get_publish_outcome_records(
        self,
        user_id: int,
        brand_id: Optional[int] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict]:
        """查询投放结果回流记录，供投放管理中心和代理回查。"""
        from db.publish_db import list_publish_outcomes

        return list_publish_outcomes(
            user_id=user_id,
            brand_id=brand_id,
            limit=limit,
            offset=offset,
        )

    def get_publish_decision_snapshots(
        self,
        user_id: int,
        brand_id: Optional[int] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict]:
        """查询用户确认时的投放快照，支持历史投放回查。"""
        from db.publish_db import list_publish_decision_snapshots

        return list_publish_decision_snapshots(
            user_id=user_id,
            brand_id=brand_id,
            limit=limit,
            offset=offset,
        )

    def get_media_outlets(self, category: str = None, ai_engine: str = None,
                         geo_only: bool = False, max_price: float = None,
                         search: str = None, limit: int = 100) -> List[Dict]:
        """查询媒体知识库"""
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()

            conditions = []
            params = []

            if category:
                conditions.append("category LIKE %s")
                params.append(f"%{category}%")
            if ai_engine:
                conditions.append("ai_engines_covered LIKE %s")
                params.append(f'%"{ai_engine}"%')
            if geo_only:
                conditions.append("geo_confirmed = 1")
            if max_price is not None:
                conditions.append("sivp_price <= %s")
                params.append(max_price)
            if search:
                conditions.append("(name LIKE %s OR platform LIKE %s)")
                params.extend([f"%{search}%", f"%{search}%"])

            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

            c.execute(f'''
                SELECT * FROM media_outlets {where}
                ORDER BY ai_coverage_count DESC, geo_confirmed DESC, sivp_price ASC
                LIMIT %s
            ''', params + [limit])

            rows = [dict(r) for r in c.fetchall()]
            conn.close()
            return rows
        finally:
            try:
                conn.close()
            except Exception: pass

    # 行业关键词映射：用于匹配媒体的category和name
    # 注意：关键词必须≥2字且足够精准，避免"房"匹配"厨房"、"家居"匹配"家纺"
    # 🔴 WO_267:从行业大类字典派生(`config/industry_taxonomy.json` 的 media_keywords),
    #    键 = 大类 key(与另外几处同键)。原来这里手写 11 类、按「类名是不是行业串的子串」取词,
    #    光伏 / 工业 / 物流这些大类根本不在表里。
    INDUSTRY_KEYWORDS = media_keywords_table()

    def _extract_industry_keywords(self, industry: str, brand: Optional[dict] = None) -> List[str]:
        """从行业字段提取搜索关键词:先判大类(主类 + 次类,吃品牌上下文),再取字典里的媒体词"""
        if not industry:
            return []
        keywords = media_keywords_for(resolve_industry(industry, brand=brand))
        # 如果没匹配到预设行业，用行业名直接拆分
        if not keywords:
            for part in industry.replace('/', ' ').replace('、', ' ').split():
                part = part.strip()
                if len(part) >= 2:
                    keywords.append(part)
        return list(set(keywords))

    # DB平台类目 → 调研标准名的映射（仅限1对1确定关系，不含模糊类目）
    PLATFORM_DB_MAPPING = {
        '今日头条': '今日头条',
        '知乎号': '知乎',
        '搜狐网': '搜狐',
        '百家号': '百度',
        '网易号': '网易',
        '新浪号': '新浪网',
        '腾讯号': '腾讯新闻',
        '微信公众号': '微信公众平台',
        '哔哩哔哩': '哔哩哔哩',
        '凤凰号': '凤凰网',
        '微博': '微博',
        '东方财富号': '东方财富',
        '豆瓣': '豆瓣',
        # 不映射: 房产家居/财经商业/新闻资讯等模糊类目，这些靠名称匹配
    }

    # 调研平台名 → media_outlets.platform 类目的映射
    RESEARCH_TO_MEDIA_CATEGORY = {
        '知乎': '知乎号', '今日头条': '今日头条', '搜狐': '搜狐网',
        '搜狐网': '搜狐网', '手机搜狐网': '搜狐网',
        '百度': '百家号', '百度百科': '百家号', '百家号': '百家号',
        '网易': '网易号', '163网易免费邮': '网易号', '手机网易网': '网易号',
        '网易新闻客户端': '网易号',
        '新浪网': '新浪号', '新浪': '新浪号', '新浪财经': '新浪号',
        '手机新浪网': '新浪号',
        '微信公众平台': '微信公众号', '腾讯新闻': '腾讯号',
        '腾讯网': '腾讯号', 'QQ': '腾讯号',
        '哔哩哔哩': '哔哩哔哩', 'B站': '哔哩哔哩',
        '微博': '微博', '凤凰网': '凤凰号',
        '东方财富': '东方财富号', '东方财富网': '东方财富号',
        '豆瓣': '豆瓣',
        # 垂直行业 → 通用类目
        '房天下': '房产家居', '安居客': '房产家居', '吉屋': '房产家居',
        '吉屋网': '房产家居', '乐居': '房产家居', '贝壳找房': '房产家居',
        '链家': '房产家居', '住小帮': '房产家居', '幸福里': '房产家居',
        '楼盘网': '房产家居', '上海房天下': '房产家居', '全国房天下': '房产家居',
        '太平洋汽车': '汽车网站', '汽车之家': '汽车网站', '懂车帝': '懂车帝',
        '抖音': '今日头条', '抖音百科': '今日头条',  # 字节系归入头条
        '澎湃新闻': '新闻资讯', '界面新闻': '新闻资讯',
        '央广网': '新闻资讯', '人民网': '新闻资讯',
        '第一财经': '财经商业', '每日经济新闻': '财经商业',
        '和讯网': '财经商业', '同花顺': '财经商业',
        '齐家网': '房产家居', '土巴兔装修网': '房产家居',
        'CSDN': 'IT科技',
    }

    # 默认GEO高价值平台（无调研数据时使用）
    GEO_DEFAULT_PLATFORMS = [
        '知乎号', '今日头条', '网易号', '百家号',
        '搜狐网', '微信公众号', '腾讯号', '哔哩哔哩',
    ]

    def _match_research_industry(self, industry: str, *, brand: Optional[dict] = None) -> tuple:
        """匹配调研行业并返回 (industry_scores, matched_research_industry)

        🔴 WO_267:不再做子串 / 双字 bigram 模糊匹配 —— 旧实现把「装修建材」拆出「建材」「修建」
           去撞任意长串,再在所有命中里挑引擎分最多的那个;光伏 / 电气 / 工业品牌就这样被拉进建筑
           (客户投诉的就是这个)。现在只有两条路:
           ① 行业串**恰好**是某个有数据的调研行业名 ⇒ 直接用(这是身份,不是模糊);
           ② 否则行业大类字典判大类(`brand` 提供品牌上下文:名称 / 备注 / 种子词)→ 该大类的
              调研行 **slug** → `geo_research_industries` 那一行(管理员能改 name、不能改 slug)
              → 用该行**当前名**取引擎分。
           大类没有调研行 / 行已停用 / 判不出 ⇒ ({}, '') —— **不许再匹到邻居**。调用方本来就把空分数
           当「无调研数据」走默认;要把「尚未开通」说给用户的调用方用 `research_status()`。
        """
        if not industry:
            return {}, ''
        industry_scores = self.get_industry_engine_scores(industry)
        if industry_scores:
            return industry_scores, industry
        _res, row = self._research_row_for(industry, brand=brand)
        if not row or not row.get('active'):
            return {}, ''
        scores = self.get_industry_engine_scores(row['name'])
        return (scores, row['name']) if scores else ({}, '')

    def _research_row_for(self, industry: str, *, brand: Optional[dict] = None) -> tuple:
        """行业串 → (大类判定, 字典指定的调研行 {id,name,active} 或 None)。按 slug 取行,只读。"""
        res = resolve_industry(industry, brand=brand)
        slug, _doc_name = research_target(res.category_key)
        if res.category_key == OTHER_KEY or not slug:
            return res, None
        from services.research_monitor.industry_registry import research_row_by_slug
        return res, research_row_by_slug(slug)

    def research_status(self, industry: str, *, brand: Optional[dict] = None) -> dict:
        """给**非付费**入口(投放候选 / 推荐)说清「这个行业的调研开没开」(WO_267 §4)。

        status:`open` 字典指定的调研行存在且启用 · `not_open` 判出了大类但没有调研行或已停用
        (对用户说「该行业的调研尚未开通」)· `unknown` 判不出大类。
        付费点亮路径不走这里(Review 09-23 Q2:付费即点亮,不显示、不阻断)。
        """
        res, row = self._research_row_for(industry or '', brand=brand)
        cat = get_category(res.category_key)
        if res.category_key == OTHER_KEY:
            status = 'unknown'
        elif row and row.get('active'):
            status = 'open'
        else:
            status = 'not_open'
        return {
            'status': status,
            'category_key': res.category_key,
            'category_name': cat.name if cat else '',
            'research_industry': row['name'] if (row and row.get('active')) else '',
        }

    def _build_research_prompt_section(self, industry_scores: dict, matched_industry: str) -> str:
        """构建LLM Prompt中的调研数据摘要段"""
        if not industry_scores:
            return """## AI引擎引用偏好（通用参考数据）
- 豆包(MAU 2.26亿): 字节系(今日头条+抖音)占~50%，知乎10%，搜狐8%
- Kimi(MAU 2.6亿): 知乎28%，CSDN 15%，澎湃8%
- DeepSeek(MAU 1.35亿): 知乎18%，CSDN 10%，澎湃10%
- 千问: 网易14%，搜狐10%，知乎10%
注意：以上为通用数据，无该行业的专项调研"""

        # 从调研数据生成摘要
        # 按score排序取top 15
        top_platforms = sorted(industry_scores.items(),
                               key=lambda x: x[1]['score'], reverse=True)[:15]

        lines = [f"## 该行业AI引擎引用数据（来自「{matched_industry}」调研，实测数据）"]
        lines.append(f"以下数据来自真实GEO调研，建议作为投放参考之一：")
        lines.append("")

        # 按引擎分组
        engine_data = {}
        for pname, pdata in top_platforms:
            for eng, rate in pdata.get('citation_rates', {}).items():
                if eng not in engine_data:
                    engine_data[eng] = []
                engine_data[eng].append((pname, rate))

        for eng in ['豆包', 'Kimi', 'DeepSeek', '千问']:
            if eng in engine_data:
                platforms = sorted(engine_data[eng], key=lambda x: x[1], reverse=True)[:5]
                plist = '、'.join(f"{p}({r:.0%})" for p, r in platforms)
                lines.append(f"- {eng}常引用: {plist}")

        lines.append("")
        lines.append("**高价值平台TOP5（综合加权分）：**")
        for i, (pname, pdata) in enumerate(top_platforms[:5]):
            engines = '+'.join(pdata.get('engines', []))
            lines.append(f"  {i+1}. {pname} (分数{pdata['score']}, 覆盖{engines})")

        return '\n'.join(lines)

    def _get_candidate_outlets(self, keyword: str, style_code: str = None,
                                budget: str = 'budget', industry: str = '',
                                brand: Optional[dict] = None) -> List[Dict]:
        """获取候选媒体并按GEO价值评分排序

        核心策略：调研数据驱动候选池构建 + 评分
        1. 先加载调研评分矩阵（决定哪些平台有价值）
        2. 根据调研数据动态构建候选池（而非固定8平台）
        3. 用调研数据评分排序
        """
        from db.diagnosis_db import get_connection

        price_limits = {'budget': 50, 'balanced': 200, 'comprehensive': 1000}
        max_price = price_limits.get(budget, 50)
        industry_kws = self._extract_industry_keywords(industry, brand=brand)

        # ====================================================================
        # Step 1: 加载调研评分矩阵（提前到候选池构建之前）
        # ====================================================================
        industry_scores, matched_research_industry = self._match_research_industry(industry, brand=brand)
        use_research_data = len(industry_scores) > 0
        # 只取 TOP 20 平台用于评分（其余为噪音）
        if use_research_data and len(industry_scores) > 20:
            top_items = sorted(industry_scores.items(), key=lambda x: x[1]['score'], reverse=True)[:20]
            scoring_matrix = dict(top_items)
        else:
            scoring_matrix = industry_scores if use_research_data else PLATFORM_ENGINE_COVERAGE
        engine_weights = self._get_engine_weights()

        logger.info(f"评分矩阵来源: {'调研数据' if use_research_data else '硬编码默认'} "
                    f"(quotes行业={industry}, 匹配调研行业={matched_research_industry or '无'}, 平台数={len(scoring_matrix)})")

        # ====================================================================
        # Step 2: 根据调研数据动态构建候选池
        # ====================================================================
        conn = get_connection()
        try:
            c = conn.cursor()
            seen_ids = set()
            all_outlets = []

            _outlet_cols = '''id, name, media_type, platform, sivp_price,
                              ai_engines_covered, ai_coverage_count, geo_confirmed,
                              geo_notes, category, notes'''

            def _add_rows(cursor_rows):
                for r in cursor_rows:
                    d = dict(r)
                    if d['id'] not in seen_ids:
                        seen_ids.add(d['id'])
                        all_outlets.append(d)

            # ---- Phase 1: 平台播种（批量查询优化） ----
            if use_research_data:
                top_research = sorted(scoring_matrix.items(),
                                      key=lambda x: x[1]['score'], reverse=True)[:20]
                seed_categories = set()
                name_search_platforms = []
                for rp_name, rp_data in top_research:
                    media_cat = self.RESEARCH_TO_MEDIA_CATEGORY.get(rp_name)
                    if media_cat:
                        seed_categories.add(media_cat)
                    if len(rp_name) >= 2 and not rp_name.endswith('.com') and not rp_name.endswith('.cn'):
                        name_search_platforms.append(rp_name)
                for dp in self.GEO_DEFAULT_PLATFORMS:
                    seed_categories.add(dp)
            else:
                seed_categories = set(self.GEO_DEFAULT_PLATFORMS)
                name_search_platforms = []

            # 批量查询：一次 SQL 取所有类目的候选
            if seed_categories:
                placeholders = ','.join('%s' for _ in seed_categories)
                c.execute(f'''
                    SELECT {_outlet_cols} FROM media_outlets
                    WHERE platform IN ({placeholders}) AND sivp_price > 0 AND sivp_price <= %s
                    ORDER BY sivp_price ASC
                ''', list(seed_categories) + [max_price])
                # 每个平台最多取15条
                cat_counts = {}
                for r in c.fetchall():
                    d = dict(r)
                    plat = d.get('platform', '')
                    cat_counts[plat] = cat_counts.get(plat, 0) + 1
                    if cat_counts[plat] <= 15 and d['id'] not in seen_ids:
                        seen_ids.add(d['id'])
                        all_outlets.append(d)

            # 按调研平台名逐个搜索媒体名称
            # 只搜索"专业平台"（如房天下、和讯网），跳过通用渠道（今日头条、搜狐）
            # 因为通用渠道已被 Phase 1a 类目查询覆盖，名称搜索只会引入噪音
            # （如"看杭州（今日头条）"实际是杭州地方号，不是今日头条本身）
            if name_search_platforms:
                for rp in name_search_platforms[:10]:
                    media_cat = self.RESEARCH_TO_MEDIA_CATEGORY.get(rp)
                    if media_cat and media_cat in seed_categories and media_cat in self.GEO_DEFAULT_PLATFORMS:
                        continue  # 通用渠道已被类目查询覆盖，跳过
                    c.execute(f'''
                        SELECT {_outlet_cols} FROM media_outlets
                        WHERE (name LIKE %s OR category LIKE %s)
                          AND sivp_price > 0 AND sivp_price <= %s
                        ORDER BY sivp_price ASC
                        LIMIT 5
                    ''', [f'%{rp}%', f'%{rp}%', max_price])
                    _add_rows(c.fetchall())

            # ---- Phase 2: 行业垂直补充 ----
            if industry_kws:
                kw_conditions = ' OR '.join(
                    f"category LIKE %s OR name LIKE %s" for _ in industry_kws
                )
                kw_params = []
                for kw in industry_kws:
                    kw_params.extend([f"%{kw}%", f"%{kw}%"])
                c.execute(f'''
                    SELECT {_outlet_cols} FROM media_outlets
                    WHERE ({kw_conditions})
                      AND sivp_price > 0 AND sivp_price <= %s
                    ORDER BY sivp_price ASC
                    LIMIT 30
                ''', kw_params + [max_price])
                _add_rows(c.fetchall())

            # Phase 3 已移除：之前基于 ai_coverage_count（媒体平台自报数据，非真实GEO调研）

        finally:
            conn.close()

        if not all_outlets:
            return []

        # ====================================================================
        # Step 3: 评分（调研数据驱动）
        # ====================================================================
        # 预构建"调研平台名 → 评分"的快速查找表，用于名称匹配
        # 按score降序排列，这样第一个匹配就是最相关的
        sorted_matrix = sorted(scoring_matrix.items(),
                               key=lambda x: x[1]['score'], reverse=True)

        # ---- 地理感知：提取目标城市，惩罚其他城市的地方媒体 ----
        _GEO_NAMES = (
            # 直辖市
            '北京', '上海', '天津', '重庆',
            # 省会及主要城市
            '广州', '深圳', '杭州', '南京', '成都', '武汉', '西安', '苏州',
            '长沙', '郑州', '东莞', '佛山', '合肥', '青岛', '济南', '昆明',
            '福州', '厦门', '南昌', '无锡', '大连', '沈阳', '哈尔滨', '长春',
            '石家庄', '太原', '南宁', '贵阳', '海口', '兰州', '银川', '西宁',
            '珠海', '中山', '惠州', '汕头', '揭阳', '潮州', '泉州', '温州',
            '宁波', '烟台', '常州', '徐州', '南通', '洛阳', '绵阳', '芜湖',
            # 省份简称（常见于媒体名）
            '陕西', '山西', '山东', '河北', '河南', '湖北', '湖南', '广东',
            '广西', '江西', '江苏', '浙江', '安徽', '福建', '四川', '云南',
            '贵州', '甘肃', '青海', '吉林', '辽宁', '黑龙江', '海南', '内蒙古',
            # 常见区名（出现在地方媒体名中）
            '南岸', '渝北', '朝阳', '海淀', '浦东', '龙岗', '宝安', '番禺',
            '闵行', '松江', '余杭', '萧山',
        )
        target_city = ''
        # 从关键词和行业中提取目标城市
        search_text = f"{keyword} {industry}"
        for city in _GEO_NAMES:
            if city in search_text:
                target_city = city
                break
        # 非地域性关键词不做地域惩罚（如"全屋定制"没有城市名，不应惩罚地方媒体）

        for outlet in all_outlets:
            score = 0.0
            match_tags = []

            try:
                engines = json.loads(outlet.get('ai_engines_covered', '[]'))
            except (json.JSONDecodeError, TypeError):
                engines = []
            outlet['_engines'] = engines

            platform = outlet.get('platform', '') or ''
            name = outlet.get('name', '')
            category = outlet.get('category', '') or ''

            # 1. 平台评分：直接匹配 > 类目映射 > 名称搜索
            platform_match = None
            matched_rp_name = ''
            is_direct_match = False  # 媒体名直接包含调研平台名

            if use_research_data:
                # 1a. 直接匹配（最高优先级）：媒体名核心部分包含调研平台名
                #     "房天下家居网"包含"房天下"→直接命中 ✅
                #     "看杭州（今日头条）"→"今日头条"仅在括号里=同步渠道，不算直接命中 ✗
                core_name = re.split(r'[（(]', name)[0]  # 取括号前的核心名称
                for pname, pdata in sorted_matrix:
                    if len(pname) >= 2 and pname in core_name:
                        platform_match = pdata
                        matched_rp_name = pname
                        is_direct_match = True
                        break

            # 1b. 类目映射（中等优先级）：搜狐网→搜狐，知乎号→知乎
            if not platform_match:
                std_platform = self.PLATFORM_DB_MAPPING.get(platform)
                if std_platform and std_platform in scoring_matrix:
                    platform_match = scoring_matrix[std_platform]
                    matched_rp_name = std_platform

            # 1c. 名称模糊搜索（最低优先级）
            if not platform_match:
                for pname, pdata in sorted_matrix:
                    if len(pname) >= 2 and (pname in platform or pname in category):
                        platform_match = pdata
                        matched_rp_name = pname
                        break

            if platform_match:
                base_score = platform_match['score']
                # 从调研数据获取真实引擎覆盖（替代vendor自报数据）
                outlet['_research_engines'] = platform_match.get('engines', [])
                if is_direct_match:
                    score += base_score * 2
                    match_tags.append(f"🎯调研直接命中:{matched_rp_name}")
                else:
                    score += base_score
                    source_tag = "调研" if use_research_data else "默认"
                    match_tags.append(f"平台:{matched_rp_name}({source_tag})")

            # 2. 行业匹配加分（关键词匹配 + 平台分类匹配）
            is_industry = False
            if industry_kws:
                # 2a. 平台分类直接匹配（如 platform="房产家居" 对房地产行业）
                for kw in industry_kws:
                    if kw in platform:
                        is_industry = True
                        score += 15
                        match_tags.append("★行业匹配")
                        break
                # 2b. 媒体名/分类匹配
                if not is_industry:
                    for kw in industry_kws:
                        if kw in category or kw in name:
                            is_industry = True
                            score += 15
                            match_tags.append("★行业匹配")
                            break

            # 3. 地理惩罚/加分
            if target_city:
                outlet_name_core = re.split(r'[（(]', name)[0]
                has_other_city = False
                for city in _GEO_NAMES:
                    if city in outlet_name_core and city != target_city:
                        has_other_city = True
                        score *= 0.2  # 其他城市的地方媒体，大幅降权
                        match_tags.append(f"⚠️地域不匹配:{city}")
                        break
                if target_city in outlet_name_core:
                    score += 10
                    match_tags.append(f"📍本地:{target_city}")

            # geo_confirmed 是媒体平台自报数据，不作为评分依据

            # 4. 性价比
            price = outlet.get('sivp_price', 1) or 1
            outlet['_geo_score'] = round(score, 1)
            outlet['_value_score'] = round(score / (price / 10 + 1), 2)
            outlet['_match_tags'] = match_tags
            outlet['_is_industry'] = is_industry

        # ========== 排序：行业优先 → GEO评分 → 性价比 ==========
        all_outlets.sort(key=lambda x: (
            -int(x.get('_is_industry', False)),
            -x.get('_geo_score', 0),
            -x.get('_value_score', 0),
        ))

        # ========== 多样性过滤：每个平台最多3个 ==========
        results = []
        platform_counts = {}
        max_per_platform = 3

        for outlet in all_outlets:
            plat = outlet.get('platform', '') or '其他'
            count = platform_counts.get(plat, 0)
            if count >= max_per_platform:
                continue
            platform_counts[plat] = count + 1
            results.append(outlet)
            if len(results) >= 60:
                break

        return results

    async def generate_recommendations(self, quote_id: int, budget: str = 'budget') -> Dict:
        """为项目所有已完成文章生成投放建议"""
        articles = self.get_completed_articles(quote_id)
        if not articles:
            return {"success": False, "message": "没有已完成的文章"}

        # Get project info
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT brand_name, industry, brand_id FROM quotes WHERE id=%s", (quote_id,))
            quote = c.fetchone()
            conn.close()

            if not quote:
                return {"success": False, "message": "项目不存在"}

            brand_name = quote['brand_name']
            industry = quote['industry'] or ''

            # Get candidate media outlets (industry-aware)
            # [WO_267] 报价挂着品牌:品牌名 / 备注 / 种子词当行业判定上下文(光伏客户的 industry 列写的是建筑)
            brand_row = None
            if quote.get('brand_id'):
                try:
                    from db.diagnosis_db import get_brand_by_id
                    brand_row = get_brand_by_id(int(quote['brand_id']))
                except Exception as e:
                    logger.warning(f"[industry] 读品牌上下文失败 quote={quote_id}: {e}")
            candidates = self._get_candidate_outlets(brand_name, budget=budget, industry=industry,
                                                     brand=brand_row)
            if not candidates:
                return {"success": False, "message": "媒体知识库为空，请先上传媒体数据"}

            # 获取调研数据用于LLM Prompt（复用候选中已计算的数据）
            industry_scores, matched_research_industry = self._match_research_industry(industry, brand=brand_row)
            prompt_scores = industry_scores  # 已在候选阶段裁剪到 TOP 20

            # Group articles by keyword for efficiency
            keyword_groups = {}
            for art in articles:
                kw = art.get('keyword', '未分类')
                keyword_groups.setdefault(kw, []).append(art)

            candidates_text = self._format_candidates(candidates[:15])

            # 构建调研数据摘要
            research_section = self._build_research_prompt_section(prompt_scores, matched_research_industry)

            # 并行处理每个关键词组
            async def _process_keyword_group(keyword: str, arts: List[Dict]) -> List[Dict]:
                titles = [a['title'] for a in arts]

                prompt = f"""你是一位GEO（AI搜索优化）投放策略专家。请根据以下信息，为每篇文章推荐更合适的3-5个投放平台。

    ## 项目信息
    - 品牌: {brand_name}
    - 行业: {industry}
    - 关键词: {keyword}
    - 预算级别: {budget} ({'试投控费' if budget == 'budget' else '稳妥推荐' if budget == 'balanced' else '权威增强'})

    ## 待投放文章
    {chr(10).join(f'{i+1}. {t}' for i, t in enumerate(titles))}

    ## 可选媒体平台（已按GEO调研数据+行业匹配度排序，排名靠前的平台更有价值）
    {candidates_text}

    {research_section}

    ## 输出格式要求
    对每篇文章，严格输出如下JSON格式:
    ```json
    [
      {{
        "article_index": 1,
        "title": "文章标题",
        "recommendations": [
          {{
            "platform": "平台名称",
            "outlet_name": "具体媒体名",
            "price": 10,
            "ai_engines": ["豆包", "DeepSeek"],
            "match_reason": "简短理由",
            "priority": 1
          }}
        ],
        "total_cost": 50,
        "strategy_note": "投放策略说明"
      }}
    ]
    ```

    ## 关键约束（必须严格遵守）
    1. **调研数据优先**：GEO分较高的媒体历史引用信号更强，请优先参考高分媒体
    2. **行业匹配第一**：{industry}行业的文章，必须优先选择该行业垂直类媒体
    3. **平台多样性**：每篇推荐3-5个平台，必须分散到不同平台类型（不能全是搜狐号），确保覆盖至少3个AI引擎
    4. **从排名靠前的候选中选**：候选列表已按调研数据排序，优先从前15名中选择
    5. 在预算范围内兼顾引擎覆盖的加权得分（豆包权重最高）
    7. 只输出JSON，不要其他说明"""

                try:
                    llm_result = await self._call_llm(prompt)
                    parsed = self._parse_llm_recommendations(llm_result, arts, candidates)
                    self._save_recommendations(quote_id, parsed, budget, llm_result)
                    return parsed
                except Exception as e:
                    logger.error(f"LLM推荐生成失败 (keyword={keyword}): {e}")
                    fallbacks = []
                    for art in arts:
                        fallback = self._rule_based_recommendation(art, candidates, budget)
                        self._save_recommendations(quote_id, [fallback], budget, f"规则推荐(LLM失败): {e}")
                        fallbacks.append(fallback)
                    return fallbacks

            # asyncio.gather 并行调用所有关键词组
            group_results = await asyncio.gather(
                *[_process_keyword_group(kw, arts) for kw, arts in keyword_groups.items()],
                return_exceptions=True
            )

            results = []
            for i, gr in enumerate(group_results):
                if isinstance(gr, Exception):
                    kw = list(keyword_groups.keys())[i]
                    logger.error(f"关键词组 {kw} 并行处理异常: {gr}")
                    # fallback
                    for art in list(keyword_groups.values())[i]:
                        results.append(self._rule_based_recommendation(art, candidates, budget))
                else:
                    results.extend(gr)

            return {
                "success": True,
                "count": len(results),
                "recommendations": results,
                "budget_tier": budget
            }
        finally:
            try:
                conn.close()
            except Exception: pass

    def _format_candidates(self, candidates: List[Dict]) -> str:
        """格式化候选媒体列表给LLM（含GEO评分和匹配标签）"""
        lines = []
        for c in candidates:
            geo_tag = ""  # geo_confirmed是平台自报数据，不展示
            # 优先用调研引擎数据
            engines = c.get('_research_engines', [])
            if not engines:
                try:
                    engines = json.loads(c.get('ai_engines_covered', '[]'))
                except (json.JSONDecodeError, TypeError):
                    engines = []
            ai_tag = f" 覆盖:{','.join(engines)}" if engines else ""
            price = c.get('sivp_price', '?')
            category = c.get('category', '')
            cat_tag = f" [{category}]" if category else ""
            tags = ' '.join(c.get('_match_tags', []))
            score = c.get('_geo_score', 0)
            score_tag = f" GEO分:{score}" if score else ""
            lines.append(f"- {c['name']} [{c.get('platform', c.get('media_type', ''))}] {price}元{cat_tag}{geo_tag}{ai_tag}{score_tag} {tags}")
        return '\n'.join(lines)

    async def _call_llm(self, prompt: str) -> str:
        """调用LLM — 优先qwen-plus(百炼DashScope)，备选Kimi"""
        ds_key = os.environ.get("DASHSCOPE_API_KEY")
        deepseek_key = os.environ.get("DEEPSEEK_API_KEY")

        providers = []
        # 优先: qwen3.6-flash via DashScope(已开通,速度快)
        if ds_key:
            providers.append(("DashScope/qwen3.6-flash", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                              ds_key, "qwen3.6-flash", {"temperature": 0.3, "enable_thinking": False}))
        # [CTO-15.23 2026-05-09 Kimi 月烧 ¥1500 治理] 投放推荐场景不需要 $web_search · 改 DeepSeek 官方 Flash 档兜底
        # 🔴 当时写的名字是 deepseek-v4-flash;2026-09-14 官方改名 deepseek-flash(同一档);名字统一由 config/deepseek_models 出。档位与成本口径未变。
        # 🔴 下一行元组里的 'DeepSeek/v4-flash' 是**展示标签**(带斜杠,不是模型 ID),另立单再说
        # 老板拍板:Kimi 仅诊断/监测必用 web_search · 投放推荐改 deepseek(¥1/M vs Kimi ¥4/M · 75% 折扣)
        if deepseek_key:
            providers.append(("DeepSeek/v4-flash", "https://api.deepseek.com/v1/chat/completions",
                              deepseek_key, DEEPSEEK_OFFICIAL_FLASH, {"temperature": 0.3, "thinking": {"type": "disabled"}}))

        if not providers:
            raise ValueError("DASHSCOPE_API_KEY 和 DEEPSEEK_API_KEY 均未设置")

        self._last_llm_model = None
        last_error = None
        for name, url, key, model, extra in providers:
            try:
                from tools.llm_call_tracker import infer_platform_from_url, llm_track, usage_from_response_payload

                async with httpx.AsyncClient(timeout=60) as client:
                    body = {
                        "model": model,
                        "messages": [
                            {"role": "system", "content": "你是GEO投放策略专家，只输出JSON格式结果。不要输出思考过程。"},
                            {"role": "user", "content": prompt}
                        ],
                        **extra,
                    }
                    async with llm_track(
                        "placement_recommendation",
                        infer_platform_from_url(url),
                        model=model,
                        metadata={"provider": name},
                    ) as tracker:
                        resp = await client.post(url, headers={"Authorization": f"Bearer {key}"}, json=body)
                        if resp.status_code == 200:
                            data_for_usage = resp.json()
                            input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data_for_usage)
                            tracker.record(
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                cached_tokens=cached_tokens,
                                success=True,
                            )
                        else:
                            tracker.record(success=False, error_msg=f"HTTP {resp.status_code}: {resp.text[:200]}")
                    data = resp.json()
                    if 'error' in data:
                        logger.warning(f"{name}返回错误: {data['error']}")
                        last_error = ValueError(f"{name}: {data['error']}")
                        continue
                    content = data['choices'][0]['message']['content']
                    self._last_llm_model = model
                    logger.info(f"LLM调用成功 via {name}, 返回{len(content)}字符")
                    return content
            except Exception as e:
                logger.warning(f"{name}调用失败: {e}")
                last_error = e
                continue

        raise last_error or ValueError("所有LLM调用均失败")

    def _parse_llm_recommendations(self, llm_text: str, articles: List[Dict],
                                     candidates: List[Dict]) -> List[Dict]:
        """解析LLM返回的JSON推荐"""
        import re

        # 1. 去除 <think>...</think> 标签（部分模型会输出思考过程）
        cleaned = re.sub(r'<think>[\s\S]*?</think>', '', llm_text).strip()

        # 2. 提取JSON（优先从 ```json 代码块中提取）
        json_match = re.search(r'```json?\s*([\s\S]*?)```', cleaned)
        if json_match:
            json_str = json_match.group(1).strip()
        else:
            # 尝试直接找 [ 开头的JSON数组
            arr_match = re.search(r'(\[[\s\S]*\])', cleaned)
            if arr_match:
                json_str = arr_match.group(1).strip()
            else:
                json_str = cleaned

        # 3. 解析JSON（先尝试直接解析，失败则用 json_repair 修复）
        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.warning(f"LLM JSON直接解析失败: {e}, 尝试 json_repair...")
            try:
                from json_repair import repair_json
                repaired = repair_json(json_str, return_objects=True)
                if repaired:
                    parsed = repaired
                    logger.info(f"json_repair 修复成功, 类型: {type(parsed).__name__}")
                else:
                    raise ValueError("json_repair 返回空结果")
            except Exception as e2:
                logger.warning(f"json_repair 也失败: {e2}, 原文前300字: {json_str[:300]}")
                return [self._rule_based_recommendation(a, candidates, 'budget') for a in articles]

        # 4. 确保parsed是列表
        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list):
            logger.warning(f"LLM返回非列表类型: {type(parsed)}")
            return [self._rule_based_recommendation(a, candidates, 'budget') for a in articles]

        # 5. 构建结果
        results = []
        for item in parsed:
            if not isinstance(item, dict):
                logger.warning(f"跳过非dict项: {type(item)}")
                continue
            idx = item.get('article_index', 1) - 1
            if 0 <= idx < len(articles):
                art = articles[idx]
                results.append({
                    'topic_id': art['topic_id'],
                    'article_id': art.get('article_id'),
                    'title': art['title'],
                    'keyword': art.get('keyword'),
                    'recommendations': item.get('recommendations', []),
                    'total_cost': item.get('total_cost', 0),
                    'strategy_note': item.get('strategy_note', '')
                })

        if not results:
            logger.warning("LLM结果解析后为空，回退到规则推荐")
            return [self._rule_based_recommendation(a, candidates, 'budget') for a in articles]

        return results

    def _rule_based_recommendation(self, article: Dict, candidates: List[Dict],
                                     budget: str) -> Dict:
        """规则兜底推荐（LLM失败时）—— 按GEO评分排序，确保平台多样性"""
        recs = []
        used_platforms = set()

        def _add_rec(c, reason, priority):
            # 只使用调研引擎数据，vendor 自报的 ai_engines_covered 不可信
            engines = c.get('_research_engines', [])
            # vendor 数据仅在调研完全无覆盖时用于显示（标记为未验证）
            if not engines:
                try:
                    vendor_engines = json.loads(c.get('ai_engines_covered', '[]'))
                    engines = vendor_engines  # 显示用，但不影响评分
                except (json.JSONDecodeError, TypeError):
                    engines = []
            recs.append({
                "platform": c.get('platform', ''),
                "outlet_name": c['name'],
                "price": c.get('sivp_price', 0),
                "ai_engines": engines,
                "match_reason": reason,
                "priority": priority
            })
            used_platforms.add(c.get('platform', ''))

        # 候选已按GEO评分排序，确保多样性
        # Step 1: 行业匹配（最多2个，且不同平台）
        for c in candidates:
            if len(recs) >= 2:
                break
            if c.get('_is_industry') and c.get('platform', '') not in used_platforms:
                _add_rec(c, f"行业垂直 - {c.get('category', '')}", 0)

        # Step 2: 补充高分平台（按GEO评分，确保不同平台各有1个）
        for c in candidates:
            if len(recs) >= 4:
                break
            platform = c.get('platform', '')
            if platform not in used_platforms:
                score = c.get('_geo_score', 0)
                tags = c.get('_match_tags', [])
                source = '调研' if any('调研' in t for t in tags) else '默认'
                _add_rec(c, f"高GEO分平台({source}) 分{score}", 1)

        # Step 3: 按GEO评分补满，跳过已用平台
        for c in candidates:
            if len(recs) >= 5:
                break
            platform = c.get('platform', '')
            if platform in used_platforms:
                continue
            score = c.get('_geo_score', 0)
            _add_rec(c, f"GEO分{score}", 2)

        total = sum(r['price'] for r in recs if r.get('price'))
        return {
            'topic_id': article['topic_id'],
            'article_id': article.get('article_id'),
            'title': article['title'],
            'keyword': article.get('keyword'),
            'recommendations': recs,
            'total_cost': total,
            'strategy_note': '规则推荐（LLM不可用）'
        }

    def _save_recommendations(self, quote_id: int, results: List[Dict],
                                tier: str, llm_text: str):
        """保存推荐结果到数据库"""
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()

            for r in results:
                c.execute('''
                    INSERT INTO placement_recommendations
                    (quote_id, topic_id, article_id, recommended_outlets,
                     total_estimated_cost, strategy_tier, llm_analysis, llm_model)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ''', (
                    quote_id,
                    r.get('topic_id'),
                    r.get('article_id'),
                    json.dumps(r.get('recommendations', []), ensure_ascii=False),
                    r.get('total_cost', 0),
                    tier,
                    llm_text[:2000] if llm_text else None,
                    getattr(self, '_last_llm_model', None) or 'qwen3.6-plus'
                ))

            conn.commit()
            conn.close()
        finally:
            try:
                conn.close()
            except Exception: pass

    # ==================== 数据上传与分析 ====================

    async def process_upload(self, file_path: str, file_name: str,
                              file_type: str, task_id: str = None) -> Dict:
        """处理上传的媒体数据文件"""
        if not task_id:
            task_id = str(uuid.uuid4())

        # Create log entry
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute('''
                INSERT INTO media_analysis_log (task_id, file_name, file_type, status)
                VALUES (%s, %s, %s, 'processing')
            ''', (task_id, file_name, file_type))
            conn.commit()
            conn.close()

            try:
                if file_type in ('csv', 'xlsx'):
                    result = await self._process_tabular_file(file_path, file_type, task_id)
                elif file_type == 'zip':
                    result = await self._process_zip_file(file_path, task_id)
                else:
                    raise ValueError(f"不支持的文件类型: {file_type}")

                # Generate insights with LLM
                insights = await self._generate_insights(result, task_id)

                # Update log
                conn = get_connection()
                c = conn.cursor()
                c.execute('''
                    UPDATE media_analysis_log
                    SET status='completed', completed_at=%s,
                        new_outlets_count=%s, updated_outlets_count=%s,
                        discoveries=%s, insights=%s, total_rows=%s, processed_rows=%s
                    WHERE task_id=%s
                ''', (
                    datetime.now().isoformat(),
                    result.get('new_count', 0),
                    result.get('updated_count', 0),
                    json.dumps(result.get('discoveries', []), ensure_ascii=False),
                    insights,
                    result.get('total_rows', 0),
                    result.get('processed_rows', 0),
                    task_id
                ))
                conn.commit()
                conn.close()

                return {"success": True, "task_id": task_id, **result}

            except Exception as e:
                logger.error(f"数据处理失败: {e}")
                conn = get_connection()
                c = conn.cursor()
                c.execute('''
                    UPDATE media_analysis_log
                    SET status='failed', error_message=%s, completed_at=%s
                    WHERE task_id=%s
                ''', (str(e), datetime.now().isoformat(), task_id))
                conn.commit()
                conn.close()
                return {"success": False, "task_id": task_id, "error": str(e)}
        finally:
            try:
                conn.close()
            except Exception: pass

    async def _process_tabular_file(self, file_path: str, file_type: str,
                                      task_id: str) -> Dict:
        """处理CSV/XLSX表格文件"""
        if file_type == 'csv':
            df = pd.read_csv(file_path, encoding='utf-8-sig')
        else:
            df = pd.read_excel(file_path)

        total_rows = len(df)
        columns = list(df.columns)

        # Auto-detect file type by columns
        file_schema = self._detect_schema(columns)

        new_count = 0
        updated_count = 0
        chunk_size = 10000
        discoveries = []

        from db.diagnosis_db import get_connection

        for start in range(0, total_rows, chunk_size):
            chunk = df.iloc[start:start + chunk_size]

            conn = get_connection()
            c = conn.cursor()

            for _, row in chunk.iterrows():
                result = self._upsert_outlet(c, row, file_schema)
                if result == 'new':
                    new_count += 1
                elif result == 'updated':
                    updated_count += 1

            conn.commit()
            conn.close()

            # Update progress
            conn = get_connection()
            c = conn.cursor()
            c.execute('''
                UPDATE media_analysis_log SET processed_rows=%s, chunk_count=%s
                WHERE task_id=%s
            ''', (min(start + chunk_size, total_rows), (start // chunk_size) + 1, task_id))
            conn.commit()
            conn.close()

        # Find discoveries
        if new_count > 0:
            discoveries.append(f"新增 {new_count} 个媒体到知识库")
        if updated_count > 0:
            discoveries.append(f"更新 {updated_count} 个媒体信息")

        # Check for GEO-capable outlets
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) as cnt FROM media_outlets WHERE geo_confirmed=1")
            geo_count = c.fetchone()['cnt']
            discoveries.append(f"当前共 {geo_count} 个GEO确认媒体")
            conn.close()

            return {
                "total_rows": total_rows,
                "processed_rows": total_rows,
                "new_count": new_count,
                "updated_count": updated_count,
                "discoveries": discoveries,
                "columns_detected": columns
            }
        finally:
            try:
                conn.close()
            except Exception: pass

    def _detect_schema(self, columns: List[str]) -> Dict:
        """自动检测文件的列映射"""
        schema = {}
        col_lower = {c.lower(): c for c in columns}

        # Name column
        for key in ['媒体名称', '名称', 'name', '媒体']:
            if key in col_lower:
                schema['name'] = col_lower[key]
                break

        # Platform column
        for key in ['平台', 'platform', '频道类型']:
            if key in col_lower:
                schema['platform'] = col_lower[key]
                break

        # Price columns
        for key in ['sivp', 'sivp价格']:
            if key in col_lower:
                schema['sivp_price'] = col_lower[key]
                break
        if 'sivp_price' not in schema:
            for key in ['普通会员', '普通会员价格', 'price', '价格']:
                if key in col_lower:
                    schema['sivp_price'] = col_lower[key]
                    break

        # Category
        for key in ['频道类型', '行业分类', 'category', '分类']:
            if key in col_lower:
                schema['category'] = col_lower[key]
                break

        # Region
        for key in ['地区', 'region', '区域']:
            if key in col_lower:
                schema['region'] = col_lower[key]
                break

        # Notes
        for key in ['备注', 'notes', '说明']:
            if key in col_lower:
                schema['notes'] = col_lower[key]
                break

        # News source
        for key in ['新闻源', 'news_source']:
            if key in col_lower:
                schema['news_source'] = col_lower[key]
                break

        # Determine media_type
        if '平台' in col_lower and col_lower['平台'] in columns:
            # Self-media file has '平台' column with values like '今日头条', '百家号'
            schema['media_type'] = 'self_media'
        else:
            schema['media_type'] = 'traditional'

        return schema

    def _upsert_outlet(self, cursor, row, schema: Dict) -> str:
        """插入或更新一个媒体记录"""
        name = str(row.get(schema.get('name', ''), '')).strip()
        if not name or name == 'nan':
            return 'skip'

        platform = str(row.get(schema.get('platform', ''), '')).strip()
        if platform == 'nan':
            platform = ''
        media_type = schema.get('media_type', 'traditional')

        # Parse price
        try:
            price_val = row.get(schema.get('sivp_price', ''), 0)
            sivp_price = float(price_val) if pd.notna(price_val) else 0
        except (ValueError, TypeError):
            sivp_price = 0

        category = str(row.get(schema.get('category', ''), '')).strip()
        if category == 'nan':
            category = ''
        region = str(row.get(schema.get('region', ''), '')).strip()
        if region == 'nan':
            region = ''
        notes = str(row.get(schema.get('notes', ''), '')).strip()
        if notes == 'nan':
            notes = ''

        # Check for GEO/AI keywords in notes
        geo_confirmed = 0
        ai_engines = []
        geo_notes_parts = []

        notes_lower = notes.lower()
        if 'geo' in notes_lower or 'ai收录' in notes_lower:
            geo_confirmed = 1
            geo_notes_parts.append(notes[:100])

        if 'deepseek' in notes_lower:
            ai_engines.append('DeepSeek')
            geo_confirmed = 1
        if '豆包' in notes_lower:
            ai_engines.append('豆包')
            geo_confirmed = 1
        if '文心' in notes_lower:
            ai_engines.append('文心一言')
            geo_confirmed = 1
        if '千问' in notes_lower or '通义' in notes_lower:
            ai_engines.append('千问')
            geo_confirmed = 1
        if 'kimi' in notes_lower:
            ai_engines.append('Kimi')
            geo_confirmed = 1

        # Also infer AI coverage from platform name (based on known data)
        platform_lower = platform.lower() if platform else ''
        name_lower = name.lower()

        for pname, info in PLATFORM_ENGINE_COVERAGE.items():
            if pname.lower() in platform_lower or pname.lower() in name_lower:
                for eng in info['engines']:
                    if eng not in ai_engines:
                        ai_engines.append(eng)

        # Check news source
        news_source_val = str(row.get(schema.get('news_source', ''), '')).strip()
        baidu_news = 1 if '百度新闻源' in news_source_val else 0

        ai_engines_json = json.dumps(ai_engines, ensure_ascii=False)
        geo_notes_str = '; '.join(geo_notes_parts) if geo_notes_parts else None

        # Try insert, on conflict update
        try:
            cursor.execute('''
                INSERT INTO media_outlets
                (name, media_type, platform, sivp_price, ai_engines_covered,
                 ai_coverage_count, geo_confirmed, geo_notes, category, region,
                 baidu_news_source, notes, data_source, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'upload', CURRENT_TIMESTAMP)
                ON CONFLICT(name, platform, media_type) DO UPDATE SET
                    sivp_price = excluded.sivp_price,
                    ai_engines_covered = CASE
                        WHEN length(excluded.ai_engines_covered) > length(media_outlets.ai_engines_covered)
                        THEN excluded.ai_engines_covered ELSE media_outlets.ai_engines_covered END,
                    ai_coverage_count = GREATEST(media_outlets.ai_coverage_count, excluded.ai_coverage_count),
                    geo_confirmed = GREATEST(media_outlets.geo_confirmed, excluded.geo_confirmed),
                    geo_notes = COALESCE(excluded.geo_notes, media_outlets.geo_notes),
                    notes = excluded.notes,
                    updated_at = CURRENT_TIMESTAMP
            ''', (name, media_type, platform, sivp_price, ai_engines_json,
                  len(ai_engines), geo_confirmed, geo_notes_str, category, region,
                  baidu_news, notes, ))

            return 'new' if cursor.rowcount == 1 else 'updated'
        except Exception as e:
            logger.debug(f"Upsert failed for {name}: {e}")
            return 'skip'

    async def _process_zip_file(self, file_path: str, task_id: str) -> Dict:
        """处理ZIP压缩文件"""
        import zipfile
        import tempfile

        total_results = {"total_rows": 0, "processed_rows": 0, "new_count": 0,
                        "updated_count": 0, "discoveries": []}

        with zipfile.ZipFile(file_path, 'r') as zf:
            for name in zf.namelist():
                if name.endswith(('.csv', '.xlsx')):
                    with tempfile.NamedTemporaryFile(suffix=Path(name).suffix, delete=False) as tmp:
                        tmp.write(zf.read(name))
                        tmp_path = tmp.name

                    try:
                        file_type = 'csv' if name.endswith('.csv') else 'xlsx'
                        result = await self._process_tabular_file(tmp_path, file_type, task_id)

                        total_results["total_rows"] += result.get("total_rows", 0)
                        total_results["processed_rows"] += result.get("processed_rows", 0)
                        total_results["new_count"] += result.get("new_count", 0)
                        total_results["updated_count"] += result.get("updated_count", 0)
                        total_results["discoveries"].extend(result.get("discoveries", []))
                    finally:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass

        return total_results

    async def _generate_insights(self, result: Dict, task_id: str) -> str:
        """用LLM生成数据分析洞察"""
        try:
            discoveries = result.get('discoveries', [])
            prompt = f"""分析以下媒体数据导入结果，给出简短洞察（2-3条关键发现）：

导入统计：
- 总行数: {result.get('total_rows', 0)}
- 新增媒体: {result.get('new_count', 0)}
- 更新媒体: {result.get('updated_count', 0)}
- 发现: {json.dumps(discoveries, ensure_ascii=False)}

请用中文输出，每条洞察一行，格式为 "- 发现: ..."
"""
            return await self._call_llm(prompt)
        except Exception as e:
            return f"自动分析暂不可用: {e}"

    def get_upload_status(self, task_id: str) -> Dict:
        """获取上传处理状态"""
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT * FROM media_analysis_log WHERE task_id=%s", (task_id,))
            row = c.fetchone()
            conn.close()

            if not row:
                return {"success": False, "message": "任务不存在"}

            return {
                "success": True,
                **dict(row),
                "discoveries": json.loads(row['discoveries']) if row['discoveries'] else [],
            }
        finally:
            try:
                conn.close()
            except Exception: pass

    def get_analysis_logs(self, limit: int = 20) -> List[Dict]:
        """获取分析日志列表"""
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute('''
                SELECT * FROM media_analysis_log
                ORDER BY created_at DESC LIMIT %s
            ''', (limit,))
            rows = [dict(r) for r in c.fetchall()]
            conn.close()

            for r in rows:
                if r.get('discoveries'):
                    r['discoveries'] = json.loads(r['discoveries'])

            return rows
        finally:
            try:
                conn.close()
            except Exception: pass

    def get_media_stats(self) -> Dict:
        """获取媒体知识库统计"""
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()

            c.execute("SELECT COUNT(*) as total FROM media_outlets")
            total = c.fetchone()['total']

            c.execute("SELECT COUNT(*) as cnt FROM media_outlets WHERE geo_confirmed=1")
            geo = c.fetchone()['cnt']

            c.execute("SELECT COUNT(*) as cnt FROM media_outlets WHERE ai_coverage_count >= 2")
            multi_ai = c.fetchone()['cnt']

            c.execute("SELECT media_type, COUNT(*) as cnt FROM media_outlets GROUP BY media_type")
            by_type = {r['media_type']: r['cnt'] for r in c.fetchall()}

            c.execute("SELECT AVG(sivp_price) as avg_price FROM media_outlets WHERE sivp_price > 0")
            avg_price = c.fetchone()['avg_price'] or 0

            conn.close()

            return {
                "total_outlets": total,
                "geo_confirmed": geo,
                "multi_ai_coverage": multi_ai,
                "by_type": by_type,
                "avg_price": round(avg_price, 1)
            }
        finally:
            try:
                conn.close()
            except Exception: pass


    # ==================== GEO调研数据导入与聚合 ====================

    def import_research_csv(self, file_path: str, researcher: str = '', industry: str = '') -> Dict:
        """导入GEO调研数据CSV — 自动识别三种格式

        格式A（实际调研格式 — 含完整URL）：
          序号, 查询内容, AI平台, 引用编号, 引用标题, 引用链接, 引用来源平台, 引用摘要

        格式B（宽表模板 — 每行一次搜索）：
          行业, 测试词, AI引擎, 引用来源1~8, 总引用数, 备注

        格式C（长表 — 每行一条引用）：
          行业, 测试词, AI引擎, 引用平台, 引用位置

        自动识别格式，导入后自动触发聚合计算。
        """
        from db.diagnosis_db import get_connection
        import logging
        logger = logging.getLogger("GEO-Placement")

        # 尝试多种编码读取CSV
        df = None
        _enc_used = ''
        for enc in ('utf-8-sig', 'utf-8', 'gbk', 'gb18030', 'latin-1'):
            try:
                df = pd.read_csv(file_path, encoding=enc)
                _enc_used = enc
                logger.info(f"CSV parsed with encoding={enc}, shape={df.shape}, columns={list(df.columns)}")
                break
            except (UnicodeDecodeError, UnicodeError):
                continue
        if df is None or df.empty:
            return {"inserted": 0, "industries": [], "engines": [], "error": "无法解析CSV文件编码"}

        columns = [c.strip() for c in df.columns]
        df.columns = columns

        # 修复列数不匹配问题：
        # CSV表头可能用中文逗号（，）连接了两个列名（如"引用摘要，回答原文"），
        # 但数据行用英文逗号分隔，导致表头8列、数据9列，pandas把序号列当成index。
        # 检测方法：用csv.reader读第一行数据看实际列数
        import csv as _csv
        try:
            with open(file_path, 'r', encoding=_enc_used) as _f:
                _reader = _csv.reader(_f)
                _raw_header = next(_reader)
                _raw_row1 = next(_reader)
                if len(_raw_row1) > len(_raw_header):
                    # 列数不匹配 — 尝试拆分含中文逗号的表头
                    new_headers = []
                    for h in _raw_header:
                        if '，' in h:
                            new_headers.extend(h.split('，'))
                        else:
                            new_headers.append(h)
                    if len(new_headers) == len(_raw_row1):
                        # 用修正后的列名重新读取
                        new_headers = [h.strip() for h in new_headers]
                        df = pd.read_csv(file_path, encoding=_enc_used, names=new_headers, header=0)
                        columns = list(df.columns)
                        logger.info(f"CSV columns fixed: {len(_raw_header)} -> {len(new_headers)}, columns={columns}")
        except Exception as _e:
            logger.warning(f"Column fix check failed (non-critical): {_e}")

        batch_id = f"research_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        inserted = 0
        skipped_dup = 0
        industries_seen = set()
        engines_seen = set()

        conn = get_connection()
        try:
            c = conn.cursor()

            # 记录批次
            c.execute('''
                INSERT INTO geo_research_batches (batch_id, industry, engine, researcher, status)
                VALUES (%s, '', '', %s, 'processing')
            ''', (batch_id, researcher))

            # 构建已有数据指纹集合，用于去重
            c.execute('SELECT industry, query, engine, cited_platform, cite_position FROM geo_research_raw')
            existing_keys = set()
            for r in c.fetchall():
                existing_keys.add((r['industry'], r['query'], r['engine'], r['cited_platform'], r['cite_position']))

            # --- 自动识别格式 ---
            has_real_research = '引用来源平台' in columns and '查询内容' in columns
            is_wide = any('引用来源' in col and '平台' not in col for col in columns)

            if has_real_research:
                # 格式A：实际调研数据
                # 标准8列：序号,查询内容,AI平台,引用编号,引用标题,引用链接,引用来源平台,引用摘要/回答原文
                # 实际可能9列（引用摘要和回答原文用英文逗号分开）
                query_col = '查询内容'
                engine_col = 'AI平台'
                platform_col = '引用来源平台'
                position_col = '引用编号'
                url_col = '引用链接' if '引用链接' in columns else None
                title_col = '引用标题' if '引用标题' in columns else None
                industry_col = '行业' if '行业' in columns else None

                # 处理引用摘要和回答原文列名
                # CSV可能有"引用摘要，回答原文"(中文逗号合并为一列) 或分开的列
                excerpt_col = None
                answer_col = None
                for col in columns:
                    if '引用摘要' in col or '答案摘要' in col:
                        excerpt_col = col
                    if '回答原文' in col and col != excerpt_col:
                        answer_col = col

                # 如果只有合并列"引用摘要，回答原文"，按索引取（数据行可能有额外列）
                use_index_fallback = excerpt_col and not answer_col and len(columns) < df.shape[1]

                for idx, row in df.iterrows():
                    query = str(row.get(query_col, '')).strip()
                    engine = str(row.get(engine_col, '')).strip()
                    platform = str(row.get(platform_col, '')).strip()

                    if not query or query == 'nan' or not engine or engine == 'nan':
                        continue
                    if not platform or platform == 'nan':
                        continue

                    # 行业：优先CSV列 → 参数指定 → 默认"通用"
                    row_industry = ''
                    if industry_col:
                        row_industry = str(row.get(industry_col, '')).strip()
                    if not row_industry or row_industry == 'nan':
                        row_industry = industry if industry else '通用'

                    position = 0
                    try:
                        position = int(row.get(position_col, 0) or 0)
                    except (ValueError, TypeError):
                        pass

                    cite_url = ''
                    if url_col:
                        cite_url = str(row.get(url_col, '')).strip()
                        if cite_url == 'nan':
                            cite_url = ''

                    cite_title = ''
                    if title_col:
                        cite_title = str(row.get(title_col, '')).strip()
                        if cite_title == 'nan':
                            cite_title = ''

                    # 提取引用摘要和回答原文
                    cite_excerpt = ''
                    answer_text = ''
                    if use_index_fallback:
                        # 数据行有9列但表头只有8列，pandas会生成额外列
                        # 第7列(index 7) = 引用摘要, 第8列(index 8) = 回答原文
                        vals = row.values.tolist()
                        if len(vals) > 7:
                            cite_excerpt = str(vals[7] if vals[7] is not None else '').strip()
                            if cite_excerpt == 'nan':
                                cite_excerpt = ''
                        if len(vals) > 8:
                            answer_text = str(vals[8] if vals[8] is not None else '').strip()
                            if answer_text == 'nan':
                                answer_text = ''
                    else:
                        if excerpt_col:
                            cite_excerpt = str(row.get(excerpt_col, '')).strip()
                            if cite_excerpt == 'nan':
                                cite_excerpt = ''
                        if answer_col:
                            answer_text = str(row.get(answer_col, '')).strip()
                            if answer_text == 'nan':
                                answer_text = ''

                    # 规范化平台名（URL形式转可读域名）
                    if platform.startswith('http'):
                        from urllib.parse import urlparse
                        try:
                            parsed = urlparse(platform)
                            domain = parsed.netloc.replace('www.', '').replace('m.', '')
                            # 常见域名→中文名映射
                            domain_map = {
                                'toutiao.com': '今日头条', 'iesdouyin.com': '抖音',
                                'zhihu.com': '知乎', 'sohu.com': '搜狐',
                                'sina.com': '新浪', 'sina.com.cn': '新浪', 'sina.cn': '新浪',
                                'cj.sina.com.cn': '新浪财经', 'cj.sina.cn': '新浪财经',
                                'baidu.com': '百度', 'baijiahao.baidu.com': '百家号',
                                'bilibili.com': 'B站', '163.com': '网易',
                                'weixin.qq.com': '微信', 'mp.weixin.qq.com': '微信公众号',
                                'douban.com': '豆瓣', 'csdn.net': 'CSDN',
                                'jianshu.com': '简书', '36kr.com': '36氪',
                                'thepaper.cn': '澎湃新闻', 'cls.cn': '财联社',
                                'anjuke.com': '安居客', 'lianjia.com': '链家',
                                'ke.com': '贝壳找房', 'fang.com': '房天下',
                            }
                            platform = domain_map.get(domain, domain)
                        except Exception:
                            pass

                    # 数据质量过滤：排除答案文本误入平台名（超长非URL中文文本）
                    if len(platform) > 100 and not platform.startswith('http'):
                        continue

                    industries_seen.add(row_industry)
                    engines_seen.add(engine)

                    # 去重检查
                    dup_key = (row_industry, query, engine, platform, position)
                    if dup_key in existing_keys:
                        skipped_dup += 1
                        continue
                    existing_keys.add(dup_key)

                    c.execute('''
                        INSERT INTO geo_research_raw
                        (industry, query, engine, cited_platform, cite_position, cite_url,
                         cite_title, cite_excerpt, answer_text, batch_id, researcher)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ''', (row_industry, query, engine, platform, position, cite_url,
                          cite_title, cite_excerpt, answer_text, batch_id, researcher))
                    inserted += 1

            elif is_wide:
                # 格式B：宽表模板
                for _, row in df.iterrows():
                    row_industry = str(row.get('行业', '')).strip()
                    query = str(row.get('测试词', '')).strip()
                    engine = str(row.get('AI引擎', '')).strip()

                    if not row_industry or not query or not engine:
                        continue
                    if row_industry == 'nan' or query == 'nan' or engine == 'nan':
                        continue

                    industries_seen.add(row_industry)
                    engines_seen.add(engine)

                    for i in range(1, 9):
                        col_name = f'引用来源{i}'
                        if col_name not in columns:
                            continue
                        platform = str(row.get(col_name, '')).strip()
                        if not platform or platform == 'nan' or platform == '':
                            continue
                        dup_key = (row_industry, query, engine, platform, i)
                        if dup_key in existing_keys:
                            skipped_dup += 1
                            continue
                        existing_keys.add(dup_key)
                        c.execute('''
                            INSERT INTO geo_research_raw
                            (industry, query, engine, cited_platform, cite_position, batch_id, researcher)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ''', (row_industry, query, engine, platform, i, batch_id, researcher))
                        inserted += 1

            else:
                # 格式C：长表
                for _, row in df.iterrows():
                    row_industry = str(row.get('行业', industry or '通用')).strip()
                    query = str(row.get('测试词', row.get('查询内容', ''))).strip()
                    engine = str(row.get('AI引擎', row.get('AI平台', ''))).strip()
                    platform = str(row.get('引用平台', row.get('引用来源平台', ''))).strip()

                    if not query or query == 'nan' or not engine or engine == 'nan':
                        continue
                    if not platform or platform == 'nan':
                        continue
                    if row_industry == 'nan':
                        row_industry = industry or '通用'

                    position = 0
                    try:
                        position = int(row.get('引用位置', row.get('引用编号', 0)) or 0)
                    except (ValueError, TypeError):
                        pass

                    industries_seen.add(row_industry)
                    engines_seen.add(engine)

                    dup_key = (row_industry, query, engine, platform, position)
                    if dup_key in existing_keys:
                        skipped_dup += 1
                        continue
                    existing_keys.add(dup_key)

                    c.execute('''
                        INSERT INTO geo_research_raw
                        (industry, query, engine, cited_platform, cite_position, batch_id, researcher)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ''', (row_industry, query, engine, platform, position, batch_id, researcher))
                    inserted += 1

            # 更新批次记录
            c.execute('''
                UPDATE geo_research_batches
                SET industry=%s, engine=%s, query_count=%s, status='completed', completed_at=%s
                WHERE batch_id=%s
            ''', (
                ','.join(sorted(industries_seen)),
                ','.join(sorted(engines_seen)),
                inserted,
                datetime.now().isoformat(),
                batch_id
            ))

            # P14-v7 (2026-05-27 review MEDIUM): ensure_research_industry 必须在 commit 之前
            # 且失败必须 raise · 让事务回滚 · 防止 raw 写入成功但 industries 缺行的脱节状态
            # (旧版 commit 后再 ensure + warning 吞异常 · 严格说不是强一致)
            from services.research_monitor.industry_registry import ensure_research_industry
            for _ind in industries_seen:
                _ind_id = ensure_research_industry(_ind, conn=conn)
                if _ind_id is None:
                    raise RuntimeError(
                        f"[import_research_csv] ensure_research_industry({_ind!r}) 失败 · 已回滚"
                    )

            conn.commit()
            conn.close()

            # 触发聚合计算 (commit 之后 · 不影响事务)
            updated_stats = self.aggregate_research_stats(list(industries_seen))

            return {
                "success": True,
                "batch_id": batch_id,
                "inserted": inserted,
                "skipped_dup": skipped_dup,
                "industries": sorted(industries_seen),
                "engines": sorted(engines_seen),
                "stats_updated": updated_stats
            }
        finally:
            try:
                conn.close()
            except Exception: pass

    def cleanup_research_data(self, industry: str = None) -> Dict:
        """清理调研数据：去重 + 移除坏数据（平台名过长/明显是正文的记录）

        返回清理结果统计
        """
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()

            removed_bad = 0
            removed_dup = 0

            # 1. 移除坏数据：平台名超过100字符且非URL的（明显是答案文本误入）
            if industry:
                c.execute("DELETE FROM geo_research_raw WHERE LENGTH(cited_platform) > 100 AND cited_platform NOT LIKE 'http%%' AND industry = %s", (industry,))
            else:
                c.execute("DELETE FROM geo_research_raw WHERE LENGTH(cited_platform) > 100 AND cited_platform NOT LIKE 'http%%'")
            removed_bad = c.rowcount

            # 2. 去重：保留每组 (industry, query, engine, cited_platform, cite_position) 中 id 最大的一条
            if industry:
                c.execute('''
                    DELETE FROM geo_research_raw WHERE id NOT IN (
                        SELECT MAX(id) FROM geo_research_raw
                        WHERE industry = %s
                        GROUP BY industry, query, engine, cited_platform, cite_position
                    ) AND industry = %s
                ''', (industry, industry))
            else:
                c.execute('''
                    DELETE FROM geo_research_raw WHERE id NOT IN (
                        SELECT MAX(id) FROM geo_research_raw
                        GROUP BY industry, query, engine, cited_platform, cite_position
                    )
                ''')
            removed_dup = c.rowcount

            conn.commit()

            # 剩余记录数
            if industry:
                c.execute('SELECT COUNT(*) as cnt FROM geo_research_raw WHERE industry = %s', (industry,))
            else:
                c.execute('SELECT COUNT(*) as cnt FROM geo_research_raw')
            remaining = c.fetchone()['cnt']

            conn.close()

            # 重新聚合
            industries = [industry] if industry else None
            self.aggregate_research_stats(industries)

            return {
                "removed_bad": removed_bad,
                "removed_dup": removed_dup,
                "remaining": remaining,
            }
        finally:
            try:
                conn.close()
            except Exception: pass

    def delete_industry_data(self, industry: str) -> Dict:
        """删除指定行业的全部调研数据"""
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()

            c.execute('SELECT COUNT(*) as cnt FROM geo_research_raw WHERE industry = %s', (industry,))
            count = c.fetchone()['cnt']

            c.execute('DELETE FROM geo_research_raw WHERE industry = %s', (industry,))
            c.execute('DELETE FROM geo_engine_stats WHERE industry = %s', (industry,))

            conn.commit()
            conn.close()

            return {"deleted": count, "industry": industry}
        finally:
            try:
                conn.close()
            except Exception: pass

    def list_industry_batches(self, industry: str) -> list:
        """列出指定行业的所有上传批次（含 year_month 与按月权重）

        权重 = 月份覆盖值 (geo_month_weights) 或 默认 decay^months_ago。
        rows_for_industry = 该批次实际在该行业下贡献的行数（不是批次总行数，
        因为一个 CSV 可能同时含多个行业）。
        """
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute('''
                SELECT b.batch_id, b.industry AS all_industries, b.engine, b.researcher, b.status,
                       b.created_at, b.completed_at,
                       (SELECT COUNT(*) FROM geo_research_raw r
                        WHERE r.batch_id = b.batch_id AND r.industry = %s) AS rows_for_industry
                FROM geo_research_batches b
                WHERE EXISTS (
                    SELECT 1 FROM geo_research_raw r2
                    WHERE r2.batch_id = b.batch_id AND r2.industry = %s
                )
                ORDER BY b.created_at DESC
            ''', (industry, industry))
            rows = [dict(r) for r in c.fetchall()]

            config = self._load_aggregation_config()
            decay = config['decay_factor']
            overrides = self._load_month_weights([industry])
            now = datetime.now()
            for r in rows:
                ts = r.get('created_at')
                if isinstance(ts, str):
                    ym = ts[:7]
                elif ts is not None and hasattr(ts, 'strftime'):
                    ym = ts.strftime('%Y-%m')
                    r['created_at'] = ts.isoformat()
                    if r.get('completed_at') and hasattr(r['completed_at'], 'isoformat'):
                        r['completed_at'] = r['completed_at'].isoformat()
                else:
                    ym = None
                r['year_month'] = ym
                if ym:
                    r['weight'] = round(
                        overrides.get((industry, ym), self._default_weight(self._months_ago(ym, now), decay)),
                        4,
                    )
                else:
                    r['weight'] = None
            return rows
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def delete_batch(self, batch_id: str) -> Dict:
        """删除单个批次的所有 raw 数据 + 批次记录，并触发受影响行业重聚合"""
        from db.diagnosis_db import get_connection
        conn = get_connection()
        industries_affected = []
        deleted = 0
        try:
            c = conn.cursor()
            c.execute("SELECT 1 FROM geo_research_batches WHERE batch_id = %s", (batch_id,))
            if not c.fetchone():
                return {"deleted": 0, "industries": [], "found": False}

            c.execute("SELECT DISTINCT industry FROM geo_research_raw WHERE batch_id = %s", (batch_id,))
            industries_affected = [r['industry'] for r in c.fetchall()]

            c.execute("DELETE FROM geo_research_raw WHERE batch_id = %s", (batch_id,))
            deleted = c.rowcount or 0
            c.execute("DELETE FROM geo_research_batches WHERE batch_id = %s", (batch_id,))
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass

        # 重聚合（独立于上面的事务，失败不影响删除本身）
        if industries_affected:
            try:
                self.aggregate_research_stats(industries_affected)
            except Exception as e:
                # 重聚合失败不阻断撤回成功，但要在返回里标识
                return {"deleted": deleted, "industries": industries_affected,
                        "found": True, "reaggregate_error": str(e)}

        return {"deleted": deleted, "industries": industries_affected, "found": True}

    def get_all_industries(self) -> list:
        """获取数据库中所有已有行业列表"""
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute('SELECT DISTINCT industry FROM geo_research_raw ORDER BY industry')
            industries = [r['industry'] for r in c.fetchall()]
            conn.close()
            return industries
        finally:
            try:
                conn.close()
            except Exception: pass

    # ============================================================
    # 月份权重聚合（2026-04-30 引入）
    # 设计文档：docs/2026-04-30-调研数据月份权重-设计.md
    # ============================================================

    def _load_aggregation_config(self) -> Dict[str, Any]:
        """读 geo_aggregation_config，返回 {decay_factor: float, min_queries_per_month: int}"""
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT key, value FROM geo_aggregation_config")
            cfg = {r['key']: r['value'] for r in c.fetchall()}
            return {
                'decay_factor':          float(cfg.get('decay_factor', '0.3')),
                'min_queries_per_month': int(cfg.get('min_queries_per_month', '10')),
            }
        finally:
            try: conn.close()
            except Exception: pass

    def _load_month_weights(self, industries: List[str] = None) -> Dict[tuple, float]:
        """读 geo_month_weights，返回 {(industry, year_month): weight}"""
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()
            if industries:
                placeholders = ','.join(['%s'] * len(industries))
                c.execute(
                    f"SELECT industry, year_month, weight FROM geo_month_weights "
                    f"WHERE industry IN ({placeholders})",
                    list(industries),
                )
            else:
                c.execute("SELECT industry, year_month, weight FROM geo_month_weights")
            return {(r['industry'], r['year_month']): float(r['weight'])
                    for r in c.fetchall()}
        finally:
            try: conn.close()
            except Exception: pass

    @staticmethod
    def _default_weight(months_ago: int, decay: float = 0.3) -> float:
        """默认指数衰减权重：当月 1.0、上月 decay、上上月 decay²..."""
        if months_ago < 0:
            return 1.0           # 未来日期保护
        return decay ** months_ago

    @staticmethod
    def _months_ago(year_month: str, ref: datetime = None) -> int:
        """计算 'YYYY-MM' 距离 ref（默认 now）多少个月"""
        ref = ref or datetime.now()
        y, m = map(int, year_month.split('-'))
        return (ref.year - y) * 12 + (ref.month - m)

    def aggregate_research_stats(self, industries: List[str] = None) -> int:
        """从 geo_research_raw 按月加权聚合 → 写入 geo_engine_stats

        新算法（2026-04-30）：
          1. 每月每 (行业, 引擎, 平台) 独立算引用率
          2. 跨月按权重加权平均（权重 = 覆盖值 ?? decay^months_ago）
          3. 月题数 < min_queries_per_month 时整月跳过
        下游表结构（geo_engine_stats）不变，仅数值变化。
        """
        from db.diagnosis_db import get_connection
        from collections import defaultdict

        config = self._load_aggregation_config()
        overrides = self._load_month_weights(industries)
        min_q = config['min_queries_per_month']
        decay = config['decay_factor']
        now = datetime.now()

        conn = get_connection()
        try:
            c = conn.cursor()

            # industries=None 时从 raw 表派生出全部行业，确保 0) 步只清"实际参与本次聚合的行业"
            if not industries:
                c.execute("SELECT DISTINCT industry FROM geo_research_raw")
                industries = [r['industry'] for r in c.fetchall()]
                if not industries:
                    return 0

            placeholders = ','.join(['%s'] * len(industries))
            industry_filter = f'WHERE industry IN ({placeholders})'
            params: list = list(industries)

            # 0) 清空目标行业的旧 stats（确保被新算法判定为 0 引用的平台不会残留）
            c.execute(
                f"DELETE FROM geo_engine_stats {industry_filter}",
                params,
            )

            # 1) 每月每 (行业, 引擎) 的去重 query 数（即"题数"）
            # [B1-1] engine 归一：round_runner 写入的是小写别名(doubao/kimi/...)，
            #        历史行还有中文名(豆包/千问)；下游 ENGINE_WEIGHTS / 前端矩阵都按中文键。
            #        聚合层必须套 _ENGINE_NORMALIZE_SQL，否则 geo_engine_stats 同一引擎裂成多行、
            #        且 get_industry_engine_scores 的 engine_weights.get(eng,0.1) 永远 fallback 0.1。
            c.execute(f'''
                SELECT industry, {_ENGINE_NORMALIZE_SQL} AS engine,
                       to_char(created_at, 'YYYY-MM') AS ym,
                       COUNT(DISTINCT query) AS q_count
                FROM geo_research_raw
                {industry_filter}
                GROUP BY industry, {_ENGINE_NORMALIZE_SQL}, ym
            ''', params)
            query_counts = {(r['industry'], r['engine'], r['ym']): r['q_count']
                            for r in c.fetchall()}

            # 2) 每月每 (行业, 引擎, 平台) 的被引用 query 数 + 平均位置
            # [B1-1] 同上归一 engine（与 Query1 一致，两个 dict 才能按同一 canonical key 对齐）。
            # [B1-2] 过滤空名平台哨兵行：cited_platform='' 是"该 prompt 已跑过但零引用"的哨兵，
            #        它应保留在 raw 表(承载题数语义)，但绝不能作为一个"平台"进入 geo_engine_stats。
            #        只过滤 Query2(平台聚合)；Query1(题数分母)保留哨兵行，题数口径不变。
            c.execute(f'''
                SELECT industry, {_ENGINE_NORMALIZE_SQL} AS engine, cited_platform,
                       to_char(created_at, 'YYYY-MM') AS ym,
                       COUNT(DISTINCT query) AS cite_count,
                       AVG(cite_position) AS avg_pos
                FROM geo_research_raw
                {industry_filter}
                  AND cited_platform IS NOT NULL AND cited_platform <> ''
                GROUP BY industry, {_ENGINE_NORMALIZE_SQL}, cited_platform, ym
            ''', params)
            # platform_data[(ind, eng)][platform][ym] = (cite_count, avg_pos)
            # AVG(cite_position) 在 PostgreSQL 对 INTEGER 列返回 numeric → psycopg2 映射为 Decimal,
            # 与下游 pos_num/pos_denom (float) 做 += 会抛 TypeError, 在边界 cast 成 float 一次根治。
            platform_data: Dict[tuple, Dict[str, Dict[str, tuple]]] = defaultdict(lambda: defaultdict(dict))
            for r in c.fetchall():
                platform_data[(r['industry'], r['engine'])][r['cited_platform']][r['ym']] = (
                    int(r['cite_count'] or 0), float(r['avg_pos'] or 0),
                )

            # 3) 计算每个 (行业, 引擎) 的合格月份及其权重（共用作分母）
            #    合格 = 该月题数 >= min_q 且权重 > 0
            ie_months: Dict[tuple, list] = defaultdict(list)
            for (ind, eng, ym), q_cnt in query_counts.items():
                if q_cnt < min_q:
                    continue
                w = overrides.get((ind, ym), self._default_weight(self._months_ago(ym, now), decay))
                if w <= 0:
                    continue
                ie_months[(ind, eng)].append((ym, q_cnt, w))

            # 4) 聚合：分母 = 该 (行业, 引擎) 所有合格月份权重和
            #         分子 = 该平台在每个合格月份的引用率 × 权重之和
            #    （平台在某月没被引用 → rate = 0，仍参与分母）
            updated = 0
            for (ind, eng), months in ie_months.items():
                total_w = sum(w for _, _, w in months)
                if total_w <= 0:
                    continue
                latest_q = max(q for _, q, _ in months)

                for plat, ym_data in platform_data.get((ind, eng), {}).items():
                    num = 0.0
                    total_cite = 0
                    pos_num, pos_denom = 0.0, 0.0
                    for ym, q_cnt, w in months:
                        cite_cnt, avg_pos = ym_data.get(ym, (0, 0))
                        rate = cite_cnt / q_cnt
                        num += rate * w
                        total_cite += cite_cnt
                        pos_num += avg_pos * cite_cnt
                        pos_denom += cite_cnt

                    if total_cite == 0:
                        continue                  # 平台从未被引用 → 不写入

                    citation_rate = num / total_w
                    avg_position = (pos_num / pos_denom) if pos_denom > 0 else 0

                    c.execute('''
                        INSERT INTO geo_engine_stats
                        (industry, engine, platform, citation_count, total_queries,
                         citation_rate, avg_position, sample_queries, last_updated)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT(industry, engine, platform) DO UPDATE SET
                            citation_count = excluded.citation_count,
                            total_queries  = excluded.total_queries,
                            citation_rate  = excluded.citation_rate,
                            avg_position   = excluded.avg_position,
                            sample_queries = excluded.sample_queries,
                            last_updated   = excluded.last_updated
                    ''', (ind, eng, plat, total_cite, latest_q,
                          round(citation_rate, 4), round(avg_position, 1), latest_q,
                          now.isoformat()))
                    updated += 1

            conn.commit()
            return updated
        finally:
            try: conn.close()
            except Exception: pass

    def get_industry_engine_scores(self, industry: str) -> Dict:
        """获取指定行业的引擎-平台评分矩阵（从调研数据计算）

        返回格式同 PLATFORM_ENGINE_COVERAGE，但基于真实调研数据：
        {
            "知乎": {"engines": ["豆包","Kimi",...], "score": 17.0, "type": "UGC社区",
                     "citation_rates": {"豆包": 0.32, "Kimi": 0.48, ...}},
            ...
        }
        如果该行业没有调研数据，返回空 dict（调用方应 fallback 到硬编码常量）。
        """
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()

            c.execute('''
                SELECT platform, engine, citation_rate, citation_count, total_queries
                FROM geo_engine_stats
                WHERE industry = %s AND citation_rate > 0
                ORDER BY citation_rate DESC
            ''', (industry,))
            rows = c.fetchall()
            conn.close()

            if not rows:
                return {}

            # 读取引擎权重（优先DB，fallback硬编码）
            engine_weights = self._get_engine_weights()

            # 按平台聚合
            platform_data: Dict[str, Dict] = {}
            for r in rows:
                plat = r['platform']
                eng = r['engine']
                rate = r['citation_rate']

                if plat not in platform_data:
                    platform_data[plat] = {
                        'engines': [],
                        'citation_rates': {},
                        'score': 0.0,
                        'type': self._guess_platform_type(plat),
                    }
                if eng not in platform_data[plat]['engines']:
                    platform_data[plat]['engines'].append(eng)
                platform_data[plat]['citation_rates'][eng] = rate

                # 加权分 = 引擎权重 × 引用概率 × 100
                w = engine_weights.get(eng, 0.1)
                platform_data[plat]['score'] += w * rate * 100

            # 四舍五入
            for plat in platform_data:
                platform_data[plat]['score'] = round(platform_data[plat]['score'], 1)

            return platform_data
        finally:
            try:
                conn.close()
            except Exception: pass

    def _get_engine_weights(self) -> Dict[str, float]:
        """引擎权重:在硬编码默认之上叠加 geo_engine_weights 的审核值(DB 覆盖同名引擎)。

        [B3-1] 改为"合并"而非"DB 有值就只用 DB":否则一旦只审核通过 1 个引擎的候选,
        geo_engine_weights 只有 1 行 → 其余引擎会掉到 .get(eng,0.1) 兜底,破坏评分。
        DB 空时(prod 现状)返回纯硬编码,与改前逐字节一致。
        """
        from db.diagnosis_db import get_connection
        weights = dict(ENGINE_WEIGHTS)  # 全量默认
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT engine, weight FROM geo_engine_weights")
            for r in c.fetchall():
                try:
                    weights[r['engine']] = float(r['weight'])
                except (TypeError, ValueError):
                    continue
            return weights
        finally:
            try:
                conn.close()
            except Exception: pass

    def _guess_platform_type(self, platform: str) -> str:
        """推断平台类型"""
        type_map = {
            '知乎': 'UGC社区', '头条': '字节系', '今日头条': '字节系',
            '搜狐': '门户', '网易': '门户', '新浪': '门户', '腾讯': '门户',
            'CSDN': '技术社区', '博客园': '技术社区', '掘金': '技术社区',
            '澎湃': '权威媒体', '界面': '财经媒体', '36氪': '科技商业',
            'B站': '视频UGC', '哔哩哔哩': '视频UGC', '抖音': '字节系',
            '百度': '百科', '百家号': '百度系', '微信': '自媒体',
        }
        for key, ptype in type_map.items():
            if key in platform:
                return ptype
        return '其他'

    def get_research_summary(self) -> Dict:
        """获取调研数据总览

        P11 (2026-05-26) 发布参谋重设计 · 增加:
          - total_engines: 已调研的 AI 引擎数 (DISTINCT engine)
          - last_updated_at: 全局最近调研时间 (MAX(geo_research_raw.created_at))
          - by_industry[].last_round_at: 单行业最近调研时间 (给行业新鲜度用)

        recent_batches 字段保留兼容 · 前端不再展示但 admin 后台仍可能引用
        """
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()

            c.execute("SELECT COUNT(*) as cnt FROM geo_research_raw")
            total_records = c.fetchone()['cnt']

            c.execute("SELECT COUNT(DISTINCT industry) as cnt FROM geo_research_raw")
            total_industries = c.fetchone()['cnt']

            # P11 新增: 已调研引擎数 (4 卡片用)
            # 2026-05-27 fix: raw.engine 历史脏数据(豆包/doubao/Kimi/kimi/千问/qwen/DeepSeek/deepseek 八种别名)
            # 用 CASE 归一到 4 个规范引擎再 DISTINCT · 分子不会再超过 4
            c.execute("""
                SELECT COUNT(DISTINCT CASE
                    WHEN LOWER(engine) IN ('doubao', '豆包') THEN 'doubao'
                    WHEN LOWER(engine) IN ('kimi') THEN 'kimi'
                    WHEN LOWER(engine) IN ('deepseek') THEN 'deepseek'
                    WHEN LOWER(engine) IN ('qwen', '千问') THEN 'qwen'
                END) as cnt
                FROM geo_research_raw
            """)
            total_engines = c.fetchone()['cnt']

            # P11 新增: 全局最近调研时间
            # 优先 batches.completed_at (跑批完成时间) · fallback raw.created_at (老 CSV 数据)
            c.execute("""
                SELECT GREATEST(
                    COALESCE((SELECT MAX(completed_at) FROM geo_research_batches), 'epoch'::timestamp),
                    COALESCE((SELECT MAX(created_at) FROM geo_research_raw), 'epoch'::timestamp)
                ) AS last_updated_at
            """)
            last_updated_at_row = c.fetchone()
            last_updated_at = last_updated_at_row['last_updated_at'] if last_updated_at_row else None

            # batch_count 用 COUNT(DISTINCT batch_id) 准确算 (替代前端用 recent_batches LIMIT 10
            # 过滤导致老行业批次数偏低的 bug, 见 fix/research-batch-count-mismatch-2026-05-06)
            # P11 新增: last_round_at = MAX(created_at) per industry · 给左侧行业新鲜度徽章用
            c.execute(f'''
                SELECT industry, COUNT(DISTINCT query) as queries,
                       COUNT(DISTINCT {_ENGINE_NORMALIZE_SQL}) as engines, COUNT(*) as citations,
                       COUNT(DISTINCT batch_id) as batch_count,
                       MAX(created_at) as last_round_at
                FROM geo_research_raw
                GROUP BY industry ORDER BY citations DESC
            ''')
            by_industry = [dict(r) for r in c.fetchall()]

            c.execute('''
                SELECT * FROM geo_research_batches
                ORDER BY created_at DESC LIMIT 10
            ''')
            batches = [dict(r) for r in c.fetchall()]

            conn.close()

            return {
                "total_records": total_records,
                "total_industries": total_industries,
                "total_engines": total_engines,           # P11
                "last_updated_at": last_updated_at,        # P11
                "by_industry": by_industry,
                "recent_batches": batches,
            }
        finally:
            try:
                conn.close()
            except Exception: pass

    # ========================================================================
    # P11 (2026-05-26) 发布参谋重设计 · 2 个安全 API
    # 严格不返回 answer_text / cite_url / 文章原文 · 给代理/销售看的决策视图
    # ========================================================================

    def get_industry_content_type_stats(self, industry: str) -> Dict:
        """统计某行业文章库的内容类型分布(P11 发布参谋用)

        口径(必须严格,不许漏):
          - geo_research_articles.primary_industry = $1
          - review_status IN ('in_library', 'imported_to_reference')
          - expired IS NOT TRUE (含 NULL)
          - GROUP BY content_type
        不要把 auto_skipped/rejected/crawled 算进代理决策图 · 这些是中间状态

        返回:
          {
              "industry": str,
              "total": int,              # 入库文章总数
              "by_type": [               # 按数量降序
                  {"content_type": "article", "count": 12, "ratio": 0.6},
                  ...
              ],
              "uncategorized_count": int  # content_type IS NULL 的数量(老数据没分类)
          }
        """
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute('''
                SELECT
                    COALESCE(content_type, '__null__') AS content_type,
                    COUNT(*) AS cnt
                  FROM geo_research_articles
                 WHERE primary_industry = %s
                   AND review_status IN ('in_library', 'imported_to_reference')
                   AND (expired = FALSE OR expired IS NULL)
                 GROUP BY content_type
                 ORDER BY cnt DESC
            ''', (industry,))
            rows = c.fetchall()
            total = sum(r['cnt'] for r in rows) if rows else 0

            by_type = []
            uncategorized_count = 0
            for r in rows:
                ct = r['content_type']
                cnt = r['cnt']
                if ct == '__null__':
                    uncategorized_count = cnt
                    continue
                by_type.append({
                    "content_type": ct,
                    "count": cnt,
                    "ratio": round(cnt / total, 4) if total > 0 else 0.0,
                })
            return {
                "industry": industry,
                "total": total,
                "by_type": by_type,
                "uncategorized_count": uncategorized_count,
            }
        finally:
            try: conn.close()
            except Exception: pass

    def get_industry_prompts_preview(self, industry: str, limit: int = 50) -> Dict:
        """安全的调研题目预览(P11 发布参谋用 · 严禁返回 AI 原文 / 引用 URL / 摘要 / 标题)

        数据源优先级:
          1. geo_research_prompts JOIN geo_research_industries WHERE name=$1 AND active=TRUE
             (新流 · 管理员配置的 prompts · 带 source 字段)
          2. geo_research_raw DISTINCT query WHERE industry=$1
             (老 CSV 流的 fallback · 没有 prompt 表映射)

        engine_count = 命中过该 prompt 的引擎数
        citation_count = 该 prompt/query 在 raw 表里的引用总条数

        返回:
          {
              "industry": str,
              "source": "prompts_table" | "raw_fallback",
              "total": int,
              "items": [
                  {"prompt": str, "engine_count": int, "citation_count": int},
                  ...
              ]
          }

        ⚠️ 安全红线 (由 tests/api/test_placement_research_p11.py 锁住):
            本函数 SQL 字面 SELECT 子句不许出现 AI 原文 / URL / 摘要 / 标题等敏感字段
        """
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()
            # 1. 先看 prompts 表
            # 子查询里 _ENGINE_NORMALIZE_SQL 引用 r.engine · 这里查询字段是 r.engine 但 helper 写的是 engine
            # 改成 r.engine 引用 (SQL CASE LOWER(r.engine) ...) 需要在 helper 里换前缀 · 或这里用相同列名让别名生效
            # 偷懒法: subquery 但开销 · 直接复用 helper 因为 raw 表只有一个 engine 列 · 无歧义
            c.execute(f'''
                SELECT
                    p.prompt_text AS prompt,
                    COUNT(DISTINCT (
                        CASE
                            WHEN LOWER(r.engine) IN ('doubao', '豆包') THEN '豆包'
                            WHEN LOWER(r.engine) IN ('kimi') THEN 'Kimi'
                            WHEN LOWER(r.engine) IN ('deepseek') THEN 'DeepSeek'
                            WHEN LOWER(r.engine) IN ('qwen', '千问') THEN '千问'
                            ELSE r.engine
                        END
                    )) AS engine_count,
                    COUNT(r.id) AS citation_count
                  FROM geo_research_prompts p
                  JOIN geo_research_industries i ON i.id = p.industry_id
                  LEFT JOIN geo_research_raw r ON r.query = p.prompt_text
                                              AND r.industry = i.name
                 WHERE i.name = %s
                   AND p.active = TRUE
                   AND i.active = TRUE
                 GROUP BY p.id, p.prompt_text, p.sort_order
                 ORDER BY p.sort_order ASC, p.id ASC
                 LIMIT %s
            ''', (industry, limit))
            prompt_rows = c.fetchall() or []
            if prompt_rows:
                items = [
                    {
                        "prompt": r['prompt'],
                        "engine_count": int(r['engine_count'] or 0),
                        "citation_count": int(r['citation_count'] or 0),
                    }
                    for r in prompt_rows
                ]
                return {
                    "industry": industry,
                    "source": "prompts_table",
                    "total": len(items),
                    "items": items,
                }

            # 2. fallback raw DISTINCT query
            c.execute(f'''
                SELECT
                    query AS prompt,
                    COUNT(DISTINCT {_ENGINE_NORMALIZE_SQL}) AS engine_count,
                    COUNT(*) AS citation_count
                  FROM geo_research_raw
                 WHERE industry = %s
                 GROUP BY query
                 ORDER BY citation_count DESC, query ASC
                 LIMIT %s
            ''', (industry, limit))
            raw_rows = c.fetchall() or []
            items = [
                {
                    "prompt": r['prompt'],
                    "engine_count": int(r['engine_count'] or 0),
                    "citation_count": int(r['citation_count'] or 0),
                }
                for r in raw_rows
            ]
            return {
                "industry": industry,
                "source": "raw_fallback",
                "total": len(items),
                "items": items,
            }
        finally:
            try: conn.close()
            except Exception: pass

    def get_research_query_details(self, industry: str) -> Dict:
        """获取行业下每个查询词的详细引用数据（含标题、摘要、原文）

        返回格式：
        {
            "queries": [
                {
                    "query": "2026年买房注意事项",
                    "engines": {
                        "豆包": {
                            "answer_text": "完整AI回答原文...",
                            "citations": [
                                {"position": 1, "title": "...", "platform": "...", "url": "...", "excerpt": "..."},
                                ...
                            ]
                        },
                        "Kimi": { ... }
                    }
                },
                ...
            ],
            "total_queries": 25,
            "total_citations": 1772
        }
        """
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()

            c.execute('''
                SELECT query, engine, cite_position, cite_title, cited_platform,
                       cite_url, cite_excerpt, answer_text
                FROM geo_research_raw
                WHERE industry = %s
                ORDER BY query, engine, cite_position
            ''', (industry,))
            rows = c.fetchall()
            conn.close()

            if not rows:
                return {"queries": [], "total_queries": 0, "total_citations": 0}

            # 按 query 分组，再按 engine 分组
            from collections import OrderedDict
            query_map = OrderedDict()
            for r in rows:
                q = r['query']
                eng = r['engine']
                if q not in query_map:
                    query_map[q] = {}
                if eng not in query_map[q]:
                    query_map[q][eng] = {
                        'answer_text': '',
                        'citations': []
                    }
                # answer_text 取第一个非空值（所有citation共享同一answer）
                if r['answer_text'] and not query_map[q][eng]['answer_text']:
                    query_map[q][eng]['answer_text'] = r['answer_text']

                query_map[q][eng]['citations'].append({
                    'position': r['cite_position'],
                    'title': r['cite_title'] or '',
                    'platform': r['cited_platform'],
                    'url': r['cite_url'] or '',
                    'excerpt': r['cite_excerpt'] or '',
                })

            queries = [
                {'query': q, 'engines': engines}
                for q, engines in query_map.items()
            ]

            return {
                "queries": queries,
                "total_queries": len(queries),
                "total_citations": len(rows),
            }
        finally:
            try:
                conn.close()
            except Exception: pass

    def get_research_source_analysis(self, industry: str) -> Dict:
        """分析行业引用来源：哪些文章被多个引擎共同引用

        返回：
        - cross_engine_sources: 被多引擎引用的来源（URL去重）
        - engine_unique_sources: 各引擎独占来源
        - top_cited_articles: 被引用次数最多的具体文章
        """
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()

            # 按URL分组，看被哪些引擎引用
            # 2026-06 · engine 字段先归一 · STRING_AGG 和 COUNT(DISTINCT) 都用归一后的值
            # 否则 "doubao,豆包,kimi,Kimi" 这种花式 STRING_AGG 输出 + engine_count 算出 5-8
            c.execute(f'''
                SELECT cite_url,
                       COALESCE(MIN(NULLIF(cite_title, '')), '') AS cite_title,
                       COALESCE(MIN(NULLIF(cited_platform, '')), '') AS cited_platform,
                       STRING_AGG(DISTINCT {_ENGINE_NORMALIZE_SQL}, ',') as engines,
                       COUNT(DISTINCT {_ENGINE_NORMALIZE_SQL}) as engine_count,
                       COUNT(DISTINCT query) as query_count
                FROM geo_research_raw
                WHERE industry = %s AND cite_url IS NOT NULL AND cite_url != ''
                GROUP BY cite_url
                ORDER BY engine_count DESC, query_count DESC
            ''', (industry,))
            url_rows = c.fetchall()

            # 按平台统计各引擎的引用数
            c.execute('''
                SELECT cited_platform, engine, COUNT(*) as cnt
                FROM geo_research_raw
                WHERE industry = %s
                GROUP BY cited_platform, engine
                ORDER BY cnt DESC
            ''', (industry,))
            platform_engine_rows = c.fetchall()

            conn.close()

            # 交叉引用来源（被2+引擎引用的文章）
            cross_engine = []
            single_engine = {}
            for r in url_rows:
                item = {
                    'url': r['cite_url'],
                    'title': r['cite_title'] or '',
                    'platform': r['cited_platform'],
                    'engines': r['engines'].split(',') if r['engines'] else [],
                    'engine_count': r['engine_count'],
                    'query_count': r['query_count'],
                }
                if r['engine_count'] >= 2:
                    cross_engine.append(item)
                else:
                    eng = item['engines'][0] if item['engines'] else '未知'
                    if eng not in single_engine:
                        single_engine[eng] = []
                    single_engine[eng].append(item)

            # 平台×引擎矩阵
            platform_matrix = {}
            for r in platform_engine_rows:
                plat = r['cited_platform']
                if plat not in platform_matrix:
                    platform_matrix[plat] = {}
                platform_matrix[plat][r['engine']] = r['cnt']

            return {
                "cross_engine_sources": cross_engine[:50],
                "engine_unique_sources": {k: v[:20] for k, v in single_engine.items()},
                "platform_engine_matrix": platform_matrix,
                "total_urls": len(url_rows),
                "cross_engine_count": len(cross_engine),
            }
        finally:
            try:
                conn.close()
            except Exception: pass

    # ==================== 知识库数据核查（矛盾检测）====================

    def check_knowledge_usage(self, quote_id: int) -> Dict:
        """核查文章是否篡改/编错了客户知识库中已有的数据

        核心逻辑：不查"有没有覆盖全部知识点"，而查"引用的数据有没有和知识库矛盾"
        - 知识库说9000/平 → 文章写12000/平 → 严重错误
        - 知识库提到雅栖酒店 → 文章没提 → 没问题（不是每篇都要提）
        - 文章出现知识库没有的数据 → 可能是AI语料或搜索结果，不管

        V14新增：
        - 竞品名称核查：检查文章中的公司名是否来自已验证竞品列表
        - 竞品分数平衡核查：检查评分差距是否过大（绅士原则）
        """
        from db.diagnosis_db import get_connection
        import json as _json

        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT brand_id, brand_name FROM quotes WHERE id=%s", (quote_id,))
            quote = c.fetchone()
            if not quote:
                return {"success": False, "message": "项目不存在"}

            brand_id = quote['brand_id']
            brand_name = quote['brand_name']

            # 加载竞品列表和模式
            comp_mode = 'evidence_only'
            db_competitors = []
            try:
                c.execute("SELECT competitor_list, competitor_mode FROM quotes WHERE id=%s", (quote_id,))
                comp_row = c.fetchone()
                if comp_row:
                    comp_mode = comp_row['competitor_mode'] or 'evidence_only'
                    comp_list_raw = comp_row['competitor_list'] or ''
                    if comp_list_raw and comp_mode == 'real':
                        db_competitors = _json.loads(comp_list_raw)
            except Exception:
                pass

            c.execute('''
                SELECT t.id as topic_id, t.optimized_title as title,
                       t.original_keyword as keyword, a.content, a.id as article_id
                FROM topics t
                LEFT JOIN articles a ON a.id = t.article_id
                WHERE t.quote_id = %s AND t.status = 'completed'
            ''', (quote_id,))
            articles = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()

        if not articles:
            return {"success": False, "message": "没有已完成的文章"}

        # 加载知识库中的结构化事实
        kb_facts = self._extract_kb_facts(brand_id)
        has_knowledge = len(kb_facts) > 0

        # 提取已验证竞品名称列表（用于名称核查）
        # 🔴 2026-08-08:原来这里还收集一份 `comp_profiles`,**全文零读取方** ——
        #    死变量,删。画像的正经用法在 `client_knowledge.load_confirmed_competitors`
        #    (带出来) → 榜单企业卡 prompt(当选材背景)→ R9 闸(查有没有整段搬运)。
        verified_comp_names = []
        if db_competitors and comp_mode == 'real':
            for item in db_competitors:
                if isinstance(item, dict):
                    name = item.get('name', '')
                    if name:
                        verified_comp_names.append(name)

        # ========== V17: 混合核查（正则预筛 + LLM批量 + 缓存） ==========
        results = []

        # 第一轮：正则核查（KB事实矛盾 + 竞品预筛）
        need_llm_articles = []  # 需要LLM深度核查的文章
        for art in articles:
            content = art.get('content') or ''
            check = self._check_contradictions(content, kb_facts, brand_name)
            art['_base_check'] = check

            # 正则预筛：文章中是否可能包含竞品名称或评分
            if verified_comp_names and content:
                needs_llm = self._quick_prefilter(content, verified_comp_names, brand_name)
                if needs_llm:
                    need_llm_articles.append(art)
                else:
                    art['_base_check']['verified'].append("竞品预筛: 未发现需核查的竞品名称")

        # 第二轮：LLM批量核查（带缓存）
        if need_llm_articles:
            logger.info(f"[V17] 预筛后 {len(need_llm_articles)}/{len(articles)} 篇需要LLM核查")
            llm_results = self._batch_llm_check(
                need_llm_articles, verified_comp_names, brand_name
            )
        else:
            llm_results = {}

        # 第三轮：合并结果
        for art in articles:
            check = art.pop('_base_check')
            topic_id = art['topic_id']

            if topic_id in llm_results:
                comp_check = llm_results[topic_id]
                check['contradictions'].extend(comp_check.get('contradictions', []))
                check['verified'].extend(comp_check.get('verified', []))

                # 重新判定状态（取最严重的）
                all_contradictions = check['contradictions']
                all_verified = check['verified']
                high = [c for c in all_contradictions if c.get('severity') == 'high']
                if high:
                    check['status'] = 'fail'
                    check['message'] = f"发现 {len(high)} 处严重问题（含竞品核查），投放前必须修正"
                elif all_contradictions:
                    check['status'] = 'warning'
                    check['message'] = f"发现 {len(all_contradictions)} 处问题，建议人工核查"
                elif all_verified:
                    check['status'] = 'pass'
                    check['message'] = f"已验证 {len(all_verified)} 项（含竞品名称和分数）"

            # M 方案 (CTO-15.23 2026-05-06):核查 issue 入库持久化 + 给每条带 issue_id + dismissed flag
            # 用户可单独勾选/忽略 · LLM fix 时按 issue_id 过滤 · 误报累积长期复用
            try:
                from db.diagnosis_db import upsert_kb_check_issue, list_kb_issues_for_quote
                contradictions = check.get('contradictions') or []
                for c_item in contradictions:
                    if not isinstance(c_item, dict):
                        continue
                    rec = upsert_kb_check_issue(
                        quote_id=quote_id,
                        topic_id=art['topic_id'],
                        article_id=art.get('article_id'),
                        issue_type=c_item.get('type', 'unknown'),
                        kb_data=str(c_item.get('kb_data', '') or ''),
                        article_data=str(c_item.get('article_data', '') or ''),
                        context=str(c_item.get('context', '') or ''),
                        severity=str(c_item.get('severity', 'medium') or 'medium'),
                    )
                    c_item['issue_id'] = rec['id']
                    c_item['dismissed'] = rec['dismissed']
            except Exception as _persist_err:
                logger.warning(f"[kb-check] issue 持久化失败 · 不影响核查结果: {_persist_err}")

            results.append({
                'topic_id': art['topic_id'],
                'article_id': art.get('article_id'),
                'title': art['title'],
                'keyword': art.get('keyword'),
                **check
            })

        total = len(results)
        passed = sum(1 for r in results if r['status'] == 'pass')
        warning = sum(1 for r in results if r['status'] == 'warning')
        failed = sum(1 for r in results if r['status'] == 'fail')

        return {
            "success": True,
            "brand_id": brand_id,
            "brand_name": brand_name,
            "has_knowledge": has_knowledge,
            "knowledge_facts_count": len(kb_facts),
            "competitor_mode": comp_mode,
            "competitor_count": len(verified_comp_names),
            "total_articles": total,
            "passed": passed,
            "warning": warning,
            "failed": failed,
            "articles": results
        }

    def _extract_kb_facts(self, brand_id: int) -> List[Dict]:
        """从知识库提取结构化事实对：(上下文关键词, 数值/数据)

        例如: {"context": "均价", "value": "9000", "unit": "元/m²", "raw": "均价约9000元/m²"}
        """
        knowledge_dir = Path(f"data/knowledge/clients/{brand_id}")
        if not knowledge_dir.exists():
            return []

        # 拼接全部知识库文本
        full_text = ''
        try:
            import lancedb
            db = lancedb.connect("data/vectordb")
            table_name = f"knowledge_client_{brand_id}"
            # [2026-06-11 修分页bug] table_names() 默认分页(返10)漏表·limit 拿全防误判"无数据"
            if table_name in db.table_names(limit=100000):
                tbl = db.open_table(table_name)
                df = tbl.to_pandas()
                full_text = '\n'.join(str(row.get('content', '')) for _, row in df.iterrows())
        except Exception as e:
            logger.warning(f"加载向量知识库失败: {e}")

        if not full_text:
            for f in knowledge_dir.glob("*"):
                if f.suffix in ('.md', '.txt'):
                    full_text += '\n' + f.read_text(encoding='utf-8', errors='ignore')

        if not full_text:
            return []

        facts = []

        # 1. 价格类事实: "均价约9000元/m²", "约107万起"
        for m in re.finditer(r'([\u4e00-\u9fff]{0,6})\s*[约为是]*\s*(\d+[\.\d]*)\s*(元/?m²|元/平|万起?|元起?|亿)', full_text):
            context_chars = m.group(1).strip()
            # 取前后20字作为上下文
            start = max(0, m.start() - 20)
            end = min(len(full_text), m.end() + 20)
            surrounding = full_text[start:end].replace('\n', ' ')
            facts.append({
                'type': 'price',
                'context': context_chars or surrounding[:15],
                'value': m.group(2),
                'unit': m.group(3),
                'raw': m.group(0).strip(),
                'surrounding': surrounding
            })

        # 2. 面积类: "97m²", "119m²", "97-220m²"
        for m in re.finditer(r'(\d+[\.\d]*)\s*[-~至]\s*(\d+[\.\d]*)\s*(m²|㎡|平米|平方米)', full_text):
            start = max(0, m.start() - 15)
            surrounding = full_text[start:m.end() + 15].replace('\n', ' ')
            facts.append({
                'type': 'area_range',
                'context': surrounding[:20],
                'value': f"{m.group(1)}-{m.group(2)}",
                'unit': m.group(3),
                'raw': m.group(0),
                'surrounding': surrounding,
                'min': float(m.group(1)),
                'max': float(m.group(2))
            })

        for m in re.finditer(r'(\d+[\.\d]*)\s*(m²|㎡|平米|平方米)', full_text):
            # 跳过已经在range里匹配过的
            start = max(0, m.start() - 20)
            surrounding = full_text[start:m.end() + 15].replace('\n', ' ')
            facts.append({
                'type': 'area',
                'context': surrounding[:20],
                'value': m.group(1),
                'unit': m.group(2),
                'raw': m.group(0),
                'surrounding': surrounding
            })

        # 3. 梯户比: "两梯两户", "两梯四户"
        for m in re.finditer(r'([一二两三四]梯[一二两三四]户)', full_text):
            start = max(0, m.start() - 15)
            surrounding = full_text[start:m.end() + 15].replace('\n', ' ')
            facts.append({
                'type': 'layout',
                'context': surrounding[:20],
                'value': m.group(1),
                'unit': '',
                'raw': m.group(1),
                'surrounding': surrounding
            })

        # 4. 楼幢数/层数: "X幢", "X层"
        for m in re.finditer(r'(\d+)\s*(幢|栋|层高?|层)', full_text):
            start = max(0, m.start() - 15)
            surrounding = full_text[start:m.end() + 10].replace('\n', ' ')
            facts.append({
                'type': 'building',
                'context': surrounding[:20],
                'value': m.group(1),
                'unit': m.group(2),
                'raw': m.group(0),
                'surrounding': surrounding
            })

        # 去重（按raw字段）
        seen = set()
        unique_facts = []
        for f in facts:
            key = f['raw']
            if key not in seen:
                seen.add(key)
                unique_facts.append(f)

        return unique_facts

    def _check_contradictions(self, content: str, kb_facts: List[Dict],
                                brand_name: str) -> Dict:
        """核查文章与知识库的数据矛盾

        核心思路：只检查文章中**提到客户品牌**附近的数据是否与知识库一致
        - 文章提到"万汇广场均价9000" vs 知识库"均价约9000" → 一致 ✓
        - 文章提到"万汇广场均价12000" vs 知识库"均价约9000" → 矛盾 ✗
        - 文章提到"碧桂园均价11000" → 不是客户品牌，不管
        - 知识库有"雅栖酒店"但文章没提 → 不管，不是每篇都要写
        """
        if not content:
            return {
                'status': 'fail',
                'contradictions': [],
                'verified': [],
                'message': '文章内容为空'
            }

        if not kb_facts:
            brand_parts = re.split(r'[・·\s]', brand_name)
            mentioned = any(p in content for p in brand_parts if len(p) >= 2)
            return {
                'status': 'warning' if not mentioned else 'pass',
                'contradictions': [],
                'verified': [],
                'message': '客户未上传知识库，无法自动核查数据' if not mentioned
                           else '客户未上传知识库，建议上传后再核查'
            }

        # 提取品牌名关键词，用于定位文章中与客户相关的段落
        brand_keywords = [p for p in re.split(r'[・·\s\-]', brand_name) if len(p) >= 2]

        contradictions = []
        verified = []

        # ===== 预处理：按类型+单位聚合KB事实为范围 =====
        # 同一项目不同户型/楼栋有不同价格和面积，应聚合为范围而非逐一比对

        # 聚合价格: {unit: (min_val, max_val)}
        price_ranges = {}
        for f in kb_facts:
            if f['type'] == 'price':
                unit = f['unit']
                try:
                    val = float(f['value'])
                    if val < 5:
                        continue  # 过滤掉明显不是价格的小数（如百分比）
                    if unit not in price_ranges:
                        price_ranges[unit] = [val, val]
                    else:
                        price_ranges[unit][0] = min(price_ranges[unit][0], val)
                        price_ranges[unit][1] = max(price_ranges[unit][1], val)
                except ValueError:
                    pass

        # 聚合面积: 所有area_range合并为一个总范围
        area_mins = [f.get('min', 0) for f in kb_facts if f['type'] == 'area_range']
        area_maxs = [f.get('max', 0) for f in kb_facts if f['type'] == 'area_range']
        kb_area_min = min(area_mins) if area_mins else 0
        kb_area_max = max(area_maxs) if area_maxs else 0

        # 梯户比: 直接收集
        layout_values = [f['value'] for f in kb_facts if f['type'] == 'layout']

        seen = set()

        # ===== 1. 价格检查（按单位聚合后检查）=====
        for unit, (kb_min, kb_max) in price_ranges.items():
            pattern = r'(\d+[\.\d]*)\s*' + re.escape(unit).replace(r'\/', '/?')
            brand_nearby = self._find_brand_nearby_data(
                content, brand_keywords, unit, pattern
            )
            for art_full, art_num_str, art_snippet in brand_nearby:
                try:
                    art_num = float(art_num_str)
                    if art_num < 5:
                        continue  # 过滤掉明显不是价格的匹配（如利率"1.8"）
                    # 过滤掉与KB范围不在同一数量级的匹配
                    # 如KB是7600-9000元（单价），200元（首付差/装修补贴）不应比对
                    # 如KB是73-109万起（总价），13万起（首付）不应比对
                    if art_num < kb_min * 0.3:
                        continue  # 低于KB最小值30%的不太可能是同类价格
                    dedup_key = f"price_{unit}_{art_num}"
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)
                    # 文章价格在KB范围内（含15%容差）→ OK
                    range_min = kb_min * 0.85
                    range_max = kb_max * 1.15
                    if range_min <= art_num <= range_max:
                        verified.append(f"价格 {art_full} ✓ (KB范围{kb_min}-{kb_max}{unit})")
                    else:
                        contradictions.append({
                            'type': '价格矛盾',
                            'kb_data': f"{kb_min}-{kb_max}{unit}" if kb_min != kb_max else f"{kb_min}{unit}",
                            'article_data': art_full,
                            'context': art_snippet[:60],
                            'severity': 'high'
                        })
                except ValueError:
                    pass

        # ===== 2. 面积范围检查（聚合后检查）=====
        if kb_area_min > 0:
            brand_nearby = self._find_brand_nearby_data(
                content, brand_keywords, 'm²',
                r'(\d+[\.\d]*)\s*[-~至]\s*(\d+[\.\d]*)\s*(m²|㎡|平米)'
            )
            for art_full, _, art_snippet in brand_nearby:
                m = re.search(r'(\d+[\.\d]*)\s*[-~至]\s*(\d+[\.\d]*)', art_full)
                if not m:
                    continue
                art_min, art_max = float(m.group(1)), float(m.group(2))
                dedup_key = f"area_{art_min}-{art_max}"
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                # 文章范围在KB总范围内（含小容差）→ OK
                if art_min >= kb_area_min - 5 and art_max <= kb_area_max + 5:
                    verified.append(f"面积 {art_min:.0f}-{art_max:.0f}m² ✓")
                elif art_min < kb_area_min - 15 or art_max > kb_area_max + 15:
                    contradictions.append({
                        'type': '面积矛盾',
                        'kb_data': f"{kb_area_min}-{kb_area_max}m²",
                        'article_data': f"{art_min:.0f}-{art_max:.0f}m²",
                        'context': art_snippet[:60],
                        'severity': 'medium'
                    })

        # ===== 3. 梯户比检查 =====
        for lv in layout_values:
            if lv in content:
                verified.append(f"梯户比 {lv} ✓")

        # 去重 verified
        verified = list(dict.fromkeys(verified))

        # 判定
        high = [c for c in contradictions if c['severity'] == 'high']
        if high:
            status = 'fail'
            message = f"发现 {len(high)} 处价格/数据与知识库矛盾，投放前必须修正"
        elif contradictions:
            status = 'warning'
            message = f"发现 {len(contradictions)} 处数据差异，建议人工核查"
        elif verified:
            status = 'pass'
            message = f"已验证 {len(verified)} 项数据与知识库一致"
        else:
            status = 'pass'
            message = "文章未涉及知识库中的具体数据，无矛盾"

        return {
            'status': status,
            'contradictions': contradictions,
            'verified': verified,
            'message': message
        }

    # ========== V17: 混合核查引擎（预筛 + 批量LLM + 缓存） ==========

    # 类级缓存：content_hash -> check_result
    _llm_check_cache: Dict[str, Dict] = {}

    def _quick_prefilter(self, content: str, verified_names: List[str],
                         brand_name: str) -> bool:
        """正则预筛：快速判断文章是否可能包含竞品名称或评分

        返回 True = 需要LLM深度核查，False = 可以跳过
        """
        # 1. 检查是否有评分模式（XX.X分）
        score_pattern = re.compile(r'\d{2,3}[\.\d]*\s*分')
        has_scores = bool(score_pattern.search(content))

        # 2. 检查是否包含已验证竞品名称的任何片段（取每个名称的核心2-4字）
        has_comp_hint = False
        for name in verified_names:
            # 取名称核心部分（去掉城市前缀等）
            core = name
            for prefix in ['揭阳', '广州', '深圳', '北京', '上海', '杭州', '成都', '武汉']:
                if core.startswith(prefix):
                    core = core[len(prefix):]
                    break
            # 只要核心部分2字以上出现在文章中就标记
            if len(core) >= 2 and core in content:
                has_comp_hint = True
                break

        # 3. 检查是否有排名/榜单类关键词（说明文章在做排名评测）
        rank_keywords = ['排名', '排行', '榜单', 'TOP', 'Top', '评分', '评测',
                         '推荐指数', '综合得分', '综合评分', '第一名', '第二名',
                         '冠军', '亚军', '入围']
        has_rank = any(kw in content for kw in rank_keywords)

        # 只要有评分+排名 或 有竞品名称暗示，就需要LLM核查
        return (has_scores and has_rank) or has_comp_hint

    # [GEO-R9-CAN-006] 缓存键版本号：核查 prompt / 模型变化时递增，防止旧结果串用
    _LLM_CHECK_CACHE_VERSION = "v17-qwen37max-1"

    def _content_hash(self, content: str, verified_names: Optional[List[str]] = None,
                      brand_name: str = "") -> str:
        """生成文章核查缓存键。

        [GEO-R9-CAN-006] 缓存键必须绑定品牌上下文：不同品牌的 verified_names
        （已验证竞品清单）不同，同一篇文章对不同品牌的核查结论可能相反。
        若仅用文章内容前缀做键，品牌 B 会串用品牌 A 的通过/矛盾判定，导致
        跨品牌误判。故键中纳入品牌名 + 已验证名单 + 模型/prompt 版本，并对
        全文而非 8000 字前缀取哈希。
        """
        import hashlib
        # 已验证名单排序后参与哈希，保证顺序无关且品牌隔离
        names_key = "\x01".join(sorted(verified_names or []))
        key_material = chr(31).join([
            self._LLM_CHECK_CACHE_VERSION,
            brand_name or "",
            names_key,
            content or "",  # 全文哈希，不再截断 8000 字前缀
        ])
        return hashlib.md5(key_material.encode('utf-8')).hexdigest()

    def _batch_llm_check(self, articles: List[Dict], verified_names: List[str],
                         brand_name: str) -> Dict[int, Dict]:
        """批量LLM核查：将多篇文章合并为少量API调用

        策略：
        - 先查缓存，跳过已核查的文章
        - 剩余文章按 ~6000字/批 合并（每篇取前2000字摘要）
        - 每批1次API调用，解析后拆分结果

        返回: {topic_id: {'contradictions': [...], 'verified': [...]}}
        """
        import json as _json

        result_map = {}

        # 1. 缓存命中检查
        uncached = []
        for art in articles:
            content = art.get('content') or ''
            # [GEO-R9-CAN-006] 缓存键绑定品牌 + 已验证名单，避免跨品牌串用判定
            h = self._content_hash(content, verified_names, brand_name)
            if h in self._llm_check_cache:
                logger.debug(f"[V17] 缓存命中: {art.get('title', '')[:20]}")
                result_map[art['topic_id']] = self._llm_check_cache[h]
            else:
                uncached.append(art)

        if not uncached:
            logger.info("[V17] 全部命中缓存，无需调用LLM")
            return result_map

        # 2. 获取 DashScope API Key（用于 qwen3.7-max）
        api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        if not api_key:
            logger.warning("[V17] DashScope API Key 未配置，跳过LLM核查")
            return result_map

        # 3. 分批：每批最多5篇，每篇取前1500字
        BATCH_SIZE = 5
        EXCERPT_LEN = 1500
        batches = []
        for i in range(0, len(uncached), BATCH_SIZE):
            batches.append(uncached[i:i + BATCH_SIZE])

        logger.info(f"[V17] {len(uncached)}篇文章分{len(batches)}批调用qwen3.7-max...")

        # 4. 逐批调用（批间延迟2秒避免429）
        import time as _time
        for batch_idx, batch in enumerate(batches):
            if batch_idx > 0:
                _time.sleep(2)
            try:
                batch_result = self._call_kimi_batch(
                    batch, verified_names, brand_name, api_key, EXCERPT_LEN
                )
                # 拆分结果到各文章
                for art in batch:
                    topic_id = art['topic_id']
                    art_key = str(topic_id)
                    llm_data = batch_result.get(art_key, {})
                    check = self._parse_llm_result(llm_data, verified_names, brand_name)
                    result_map[topic_id] = check
                    # 写入缓存
                    # [GEO-R9-CAN-006] 与命中检查同键：绑定品牌 + 已验证名单
                    h = self._content_hash(art.get('content', ''), verified_names, brand_name)
                    self._llm_check_cache[h] = check

                logger.info(f"[V17] 第{batch_idx+1}批完成 ({len(batch)}篇)")
            except Exception as e:
                logger.warning(f"[V17] 第{batch_idx+1}批LLM调用失败: {e}")
                for art in batch:
                    result_map[art['topic_id']] = {'contradictions': [], 'verified': []}

        return result_map

    def _call_kimi_batch(self, batch: List[Dict], verified_names: List[str],
                         brand_name: str, api_key: str, excerpt_len: int) -> Dict:
        """调用 qwen3.7-max 批量核查多篇文章"""
        import json as _json

        # 构建批量文章内容
        articles_text = ""
        for art in batch:
            content = art.get('content', '')[:excerpt_len]
            topic_id = art['topic_id']
            title = art.get('title', '')
            articles_text += f"\n\n---【文章 {topic_id}】{title}---\n{content}"

        prompt = f"""你是一个文章审核专家。请分析以下{len(batch)}篇文章，对每篇分别完成两项任务：

## 任务1：提取竞品名称
从每篇文章中找出所有**作为排名/推荐/评测对象**的公司名、楼盘名或品牌名。

注意区分：
- ✅ 要提取的：被排名的公司/楼盘/品牌（如"揭阳中骏世界城""万泰春天·悦府""保利锦城"）
- ❌ 不要提取的：章节标题（如"综合评分与能力画像""数据来源与局限"）、评测方法名（如"混合数据验证机制"）、评级等级名（如"S级""A级"）

然后检查每个名称是否在已验证列表中（模糊匹配即可，如"中骏世界城"匹配"揭阳中骏世界城"）：
已验证列表：{', '.join(verified_names)}
客户品牌（不需要核查）：{brand_name}

## 任务2：提取评分
从每篇文章中找出每个被排名对象的**核心/综合评分**（XX.X分格式）。
- 只提取实际赋予某个公司/楼盘的评分
- 不要提取评级标准说明中的分数（如"S级≥90分""B级70-79分"是标准定义，不是实际评分）

## 输出格式（严格JSON，按文章ID分组）
```json
{{
  "{batch[0]['topic_id']}": {{
    "competitors": [{{"name": "名称", "verified": true/false, "context": "前后10字"}}],
    "scores": [{{"name": "名称", "score": 92.0}}]
  }},
  ...其他文章ID...
}}
```

只输出JSON，不要输出其他内容。

## 待审核文章
{articles_text}"""

        import time as _time

        # 带重试的 API 调用（429 过载时等待重试）
        max_retries = 2
        for attempt in range(max_retries + 1):
            from tools.llm_call_tracker import llm_track_sync, usage_from_response_payload

            with llm_track_sync(
                caller="placement_competitor_audit",
                platform="dashscope",
                model="qwen3.7-max",
                metadata={"attempt": attempt + 1},
            ) as tracker:
                resp = httpx.post(
                    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": "qwen3.7-max",
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1,
                        "max_tokens": 4000,
                        "response_format": {"type": "json_object"},
                        "enable_thinking": False,
                    },
                    timeout=120.0
                )
                if resp.status_code == 200:
                    data_for_usage = resp.json()
                    input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data_for_usage)
                    tracker.record(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cached_tokens=cached_tokens,
                        success=True,
                    )
                else:
                    tracker.record(success=False, error_msg=f"HTTP {resp.status_code}: {resp.text[:200]}")
            if resp.status_code == 429 and attempt < max_retries:
                wait = 5 * (attempt + 1)
                logger.warning(f"[V17] qwen3.7-max 429 过载，等待{wait}秒后重试...")
                _time.sleep(wait)
                continue
            break

        if resp.status_code != 200:
            raise ValueError(f"qwen3.7-max API 返回 {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")

        # qwen3.7-max 可能输出 think 标签
        if "<think>" in text:
            import re as _re
            text = _re.sub(r'<think>[\s\S]*?</think>', '', text).strip()

        # 提取 JSON（容错 markdown 代码块等格式）
        text = text.strip()
        if "```json" in text:
            text = text.split("```json", 1)[1]
            if "```" in text:
                text = text.split("```", 1)[0]
        elif "```" in text:
            parts = text.split("```")
            if len(parts) >= 3:
                text = parts[1]
        text = text.strip()

        try:
            return _json.loads(text)
        except _json.JSONDecodeError:
            # 尝试修复常见 JSON 问题
            # 1. 找到第一个 { 到最后一个 }
            start = text.find('{')
            end = text.rfind('}')
            if start >= 0 and end > start:
                json_str = text[start:end + 1]
                try:
                    return _json.loads(json_str)
                except _json.JSONDecodeError:
                    # 2. 尝试修复尾部逗号
                    json_str = re.sub(r',\s*}', '}', json_str)
                    json_str = re.sub(r',\s*]', ']', json_str)
                    try:
                        return _json.loads(json_str)
                    except _json.JSONDecodeError:
                        pass
            raise ValueError(f"无法解析LLM返回的JSON: {text[:300]}")

    def _parse_llm_result(self, llm_data: Dict, verified_names: List[str],
                          brand_name: str = '') -> Dict:
        """将单篇文章的LLM提取结果转换为核查结论"""
        contradictions = []
        verified = []

        # 标准化函数：统一中点符号用于模糊匹配
        def normalize(s: str) -> str:
            return s.replace('・', '·').replace('‧', '·').replace('．', '·').lower().strip()

        norm_verified = [normalize(n) for n in verified_names]
        norm_brand_parts = [normalize(p) for p in re.split(r'[・·\s\-]', brand_name) if len(p) >= 2]

        # --- 竞品名称核查 ---
        for item in llm_data.get('competitors', []):
            name = item.get('name', '').strip()
            if not name:
                continue

            norm_name = normalize(name)

            # 排除客户品牌自身（LLM有时会把客户品牌也提取出来）
            is_brand = any(bp in norm_name or norm_name in bp for bp in norm_brand_parts if len(bp) >= 2)
            if is_brand:
                verified.append(f"客户品牌 {name} ✓ (自动跳过)")
                continue

            # 模糊匹配已验证列表（标准化后对比）
            is_verified = item.get('verified', False)
            if not is_verified:
                # 二次检查：LLM可能因符号差异判断为未验证
                for nv in norm_verified:
                    if nv in norm_name or norm_name in nv:
                        is_verified = True
                        break

            if is_verified:
                verified.append(f"竞品名称 {name} ✓ (已验证)")
            else:
                contradictions.append({
                    'type': '竞品名称未验证',
                    'kb_data': f"已验证列表: {', '.join(verified_names[:5])}{'...' if len(verified_names) > 5 else ''}",
                    'article_data': name,
                    'context': item.get('context', '')[:60],
                    'severity': 'high'
                })

        # --- 评分平衡核查 ---
        scores_data = llm_data.get('scores', [])
        if scores_data:
            score_values = [s.get('score', 0) for s in scores_data if s.get('score')]
            if len(score_values) >= 2:
                max_s = max(score_values)
                min_s = min(score_values)
                gap = max_s - min_s

                if gap > 7:
                    contradictions.append({
                        'type': '评分差距过大',
                        'kb_data': "绅士原则: 最高分与最低分差距应≤7分",
                        'article_data': f"最高{max_s}分 vs 最低{min_s}分，差距{gap:.1f}分",
                        'context': f"文章中{len(score_values)}家公司评分，范围{min_s}-{max_s}",
                        'severity': 'medium'
                    })
                else:
                    verified.append(f"评分差距 {gap:.1f}分 ✓ (符合绅士原则≤7分)")

                low = [s for s in score_values if s < 85]
                if low:
                    contradictions.append({
                        'type': '竞品评分过低',
                        'kb_data': "绅士原则: 入榜公司评分应≥87分",
                        'article_data': f"发现{len(low)}个低于85分的评分: {', '.join(f'{s}' for s in low[:3])}",
                        'context': "使用真实竞品名时，低分可能引发同行不满",
                        'severity': 'medium'
                    })
                elif all(s >= 87 for s in score_values):
                    verified.append("所有评分≥87分 ✓ (竞品体面)")

        return {'contradictions': contradictions, 'verified': verified}

    # ========== V15: 一键修复 ==========

    async def fix_article_issues(self, quote_id: int, topic_ids: List[int] = None) -> Dict:
        """对核查发现问题的文章进行精准局部修复

        流程：
        1. 重新运行核查，获取每篇文章的具体问题
        2. 只对有问题的文章调用 LLM 做定向修复
        3. 更新数据库中的文章内容
        """
        import json as _json
        from db.diagnosis_db import get_connection

        # Step 1: 运行核查获取问题清单
        check_result = self.check_knowledge_usage(quote_id)
        if not check_result.get('success'):
            return {"success": False, "message": check_result.get('message', '核查失败')}

        # 筛选有问题的文章
        articles_to_fix = []
        for art in check_result.get('articles', []):
            if not art.get('contradictions'):
                continue
            if topic_ids and art['topic_id'] not in topic_ids:
                continue
            articles_to_fix.append(art)

        if not articles_to_fix:
            return {"success": True, "message": "没有需要修复的文章", "fixed": 0}

        # 加载竞品列表（用于修复指导）
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT competitor_list, competitor_mode, brand_name FROM quotes WHERE id=%s", (quote_id,))
            q = c.fetchone()
            comp_list_raw = q['competitor_list'] or '[]' if q else '[]'
            comp_mode = q['competitor_mode'] or 'evidence_only' if q else 'evidence_only'
            brand_name = q['brand_name'] or '' if q else ''
            db_competitors = _json.loads(comp_list_raw) if comp_list_raw else []
            verified_names = [c['name'] for c in db_competitors if isinstance(c, dict) and c.get('name')]
        finally:
            conn.close()

        # Step 2: 逐篇并发修复
        fixed_results = await self._fix_articles_batch(
            quote_id, articles_to_fix, verified_names, brand_name, comp_mode
        )

        return {
            "success": True,
            "message": f"修复完成: {sum(1 for r in fixed_results if r['fixed'])} 篇成功",
            "fixed": sum(1 for r in fixed_results if r['fixed']),
            "total": len(articles_to_fix),
            "details": fixed_results,
        }

    async def _fix_articles_batch(self, quote_id: int, articles: List[Dict],
                                   verified_names: List[str], brand_name: str,
                                   comp_mode: str) -> List[Dict]:
        """并发修复多篇文章"""
        import asyncio

        semaphore = asyncio.Semaphore(3)  # 最多3篇并发

        async def fix_one(art):
            async with semaphore:
                return await self._fix_single_article(
                    quote_id, art, verified_names, brand_name, comp_mode
                )

        return await asyncio.gather(*[fix_one(a) for a in articles])

    async def _fix_single_article(self, quote_id: int, art: Dict,
                                   verified_names: List[str], brand_name: str,
                                   comp_mode: str) -> Dict:
        """修复单篇文章的具体问题
        [M 方案 · CTO-15.23 2026-05-06]
        - contradictions 里 dismissed=True 的不进 fix_instructions(用户标记误报)
        - 但仍把 dismissed 的 context 显式列入"禁动列表" PROMPT · LLM 整篇重写时不要动
        - 修复成功后调 mark_kb_issue_fixed(issue_id) 标记 fixed_at
        """
        from db.diagnosis_db import get_connection, mark_kb_issue_fixed
        import re

        topic_id = art['topic_id']
        article_id = art.get('article_id')
        title = art.get('title', '')
        contradictions = art.get('contradictions', [])

        if not article_id:
            return {"topic_id": topic_id, "title": title, "fixed": False, "reason": "无文章ID"}

        # M 方案 · 拆 dismissed / active
        active_contradictions = [c for c in contradictions if not c.get('dismissed')]
        dismissed_contradictions = [c for c in contradictions if c.get('dismissed')]

        if not active_contradictions:
            return {"topic_id": topic_id, "title": title, "fixed": False,
                    "reason": "全部 issue 已被用户忽略 · 无需修复",
                    "dismissed_count": len(dismissed_contradictions)}

        # 读取文章内容
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT content FROM articles WHERE id=%s", (article_id,))
            row = c.fetchone()
            content = row['content'] if row else ''
        finally:
            conn.close()

        if not content:
            return {"topic_id": topic_id, "title": title, "fixed": False, "reason": "文章内容为空"}

        # 构建修复指令(只对 active 的 issue · dismissed 的不放进 instruction)
        fix_instructions = self._build_fix_instructions(
            active_contradictions, verified_names, brand_name,
            dismissed_contradictions=dismissed_contradictions,
        )

        # 调用 LLM 修复
        try:
            fixed_content = await self._call_llm_fix(content, fix_instructions, title)
        except Exception as e:
            return {"topic_id": topic_id, "title": title, "fixed": False, "reason": f"LLM调用失败: {e}"}

        if not fixed_content or len(fixed_content) < len(content) * 0.7:
            return {"topic_id": topic_id, "title": title, "fixed": False, "reason": "修复结果异常（内容缩水）"}

        # V15: 修复后同样清除 [字段X] 标记
        fixed_content = re.sub(r'\[字段\d+\]\s*', '', fixed_content)

        # [W1 返工 2026-08-08] 第 4 个正文写入点:粗体伪标题 → 真 `##`。
        #
        # 这条路径是子代理扫全仓时扫出来的,原工单只点了三条保存路径。它同样是
        # **整篇 LLM 重写**后直接 `UPDATE articles SET content`,伪标题概率一点不比
        # 另三条低,所以格式修复必须接。
        #
        # 🔴 自曝(不在本包修,另立单):这条路径**绕过了全部正文清洗** ——
        # 没有 `sanitize_article_for_save`、没有 `_normalize_article_title_and_h1`、
        # 没有来源清洗。那是比伪标题更大的一个洞,但改它会动到"一键修复"这条链的
        # 既有行为,不该顺手塞进一个格式返工包里。
        try:
            from writing.markdown_heading_repair import (
                promote_orphan_h3,
                repair_pseudo_headings,
            )

            fixed_content, _repaired_headings = repair_pseudo_headings(fixed_content)
            fixed_content, _promoted_h3 = promote_orphan_h3(fixed_content)
            if _repaired_headings or _promoted_h3:
                logger.info(
                    "[kb-fix] article=%s 小标题格式修复:粗体转 ## %d 处 · ### 提级 %d 处",
                    article_id, len(_repaired_headings), _promoted_h3,
                )
        except Exception:  # noqa: BLE001 - 格式修复绝不阻断修复链
            logger.warning("[kb-fix] 小标题格式修复跳过(不阻断)", exc_info=True)

        # 更新数据库
        conn = get_connection()
        try:
            c = conn.cursor()

            # [P0 代发内容漂移 · 根因阻断] 发布窗口内不得改稿。
            #
            # 下单时冻结了一份稿件快照;这里若把 articles.content 改掉,提交/重试就会
            # 与快照对不上 = 内容漂移,并一路走到"重试耗尽 → 退款",用户再下单又撞同一
            # 堵墙。自动优化是**质量改进,不是急事**,让路到发布结束再做没有任何损失。
            #
            # 刻意不做"改完自动重建快照":那等于把客户已确认并已扣费的版本悄悄换掉。
            # issue 不标记已修复(见下方 return 前的 mark 逻辑被跳过),下轮会重新捞起来。
            from services.publication_content_drift import article_has_active_publication

            if article_has_active_publication(c, article_id):
                logger.info(
                    "[kb-fix] article=%s 有在途发布任务,自动优化暂缓(发布完成后会重新捞起)",
                    article_id,
                )
                return {
                    "topic_id": topic_id, "title": title, "fixed": False,
                    "deferred": True,
                    "reason": "文章有进行中的发布任务,已暂缓自动优化 · 发布完成后会自动重试",
                }

            c.execute("UPDATE articles SET content=%s, updated_at=NOW() WHERE id=%s",
                      (fixed_content, article_id))
            from services.article_review_gate import refresh_article_review

            refresh_article_review(article_id, cursor=c)
            conn.commit()
        finally:
            conn.close()

        # M 方案 · 标记 active issues 已修复(audit trail)
        for c_item in active_contradictions:
            iid = c_item.get('issue_id')
            if iid:
                try:
                    mark_kb_issue_fixed(iid)
                except Exception as _mfix_err:
                    logger.debug(f"[kb-fix] mark_kb_issue_fixed({iid}) 异常 · 忽略: {_mfix_err}")

        return {
            "topic_id": topic_id,
            "title": title,
            "fixed": True,
            "issues_count": len(active_contradictions),
            "dismissed_count": len(dismissed_contradictions),
            "original_length": len(content),
            "fixed_length": len(fixed_content),
        }

    def _build_fix_instructions(self, contradictions: List[Dict],
                                 verified_names: List[str], brand_name: str,
                                 dismissed_contradictions: Optional[List[Dict]] = None) -> str:
        """根据问题清单生成具体的修复指令

        [M 方案 · CTO-15.23 2026-05-06]
        dismissed_contradictions:用户已勾选"忽略"的 issue · 不进 instruction · 但显式列入"禁动列表"
        让 LLM 整篇重写时不要改动这些位置(否则即使没指令 · LLM 也可能附带调整)
        """
        lines = []
        for i, c in enumerate(contradictions, 1):
            issue_type = c.get('type', '')
            kb_data = c.get('kb_data', '')
            article_data = c.get('article_data', '')
            context = c.get('context', '')

            if issue_type == '竞品名称未验证':
                lines.append(
                    f"{i}. 【竞品名称】文章中出现未验证的公司名「{article_data}」，"
                    f"请用已验证竞品列表中最相近的公司名替换，替换后需同步调整该公司的描述内容使其与真实情况一致。"
                    f"如果找不到相近的，请从已验证列表中选一个合适的替换。"
                )
            elif issue_type == '评分差距过大':
                lines.append(
                    f"{i}. 【评分平衡】{kb_data}。当前情况：{article_data}。"
                    f"请调整各公司评分，确保：客户品牌（{brand_name}）93-95.5分，"
                    f"第2名91-93.5分，第3名89.5-92分，第4名88-90.5分，其余87-89.5分。"
                    f"调整分数时必须同步调整分数明细（如有），保持加总正确。"
                )
            elif issue_type == '竞品评分过低':
                lines.append(
                    f"{i}. 【评分过低】{kb_data}。{article_data}。"
                    f"请将所有低于87分的评分提升至87-89.5分区间，"
                    f"同时调整分数明细保持加总正确。排名顺序不变。"
                )
            elif issue_type == '价格矛盾':
                lines.append(
                    f"{i}. 【价格错误】知识库数据：{kb_data}，文章写成：{article_data}。"
                    f"位置：{context}。请将文章中的错误价格改为知识库的正确数据。"
                )
            elif issue_type == '面积矛盾':
                lines.append(
                    f"{i}. 【面积错误】知识库数据：{kb_data}，文章写成：{article_data}。"
                    f"位置：{context}。请将文章中的错误面积改为知识库的正确数据。"
                )
            else:
                lines.append(
                    f"{i}. 【{issue_type}】知识库：{kb_data} → 文章：{article_data}。{context}"
                )

        instruction = "\n".join(lines)

        if verified_names:
            instruction += f"\n\n已验证竞品名称列表：{', '.join(verified_names)}"

        # M 方案 · 显式禁动列表(用户标记为误报的位置)
        if dismissed_contradictions:
            dismiss_lines = []
            for c in dismissed_contradictions:
                ctx = (c.get('context') or c.get('article_data') or '').strip()
                if ctx:
                    dismiss_lines.append(f"- 「{ctx[:80]}」(用户已确认此处无问题)")
            if dismiss_lines:
                instruction += (
                    "\n\n## ⚠️ 用户标记的误报位置 · 必须保持原样不要改"
                    "\n以下文本片段经用户审核确认无问题 · 即使你的判断认为有错也必须保持原样:\n"
                    + "\n".join(dismiss_lines)
                    + "\n修改其他位置时不要附带调整这些片段"
                )

        return instruction

    async def _call_llm_fix(self, content: str, fix_instructions: str, title: str) -> str:
        """调用 LLM 对文章进行定向修复"""
        from openai import AsyncOpenAI
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from writing.llm_utils import get_llm_config, API_URLS

        api_url, api_key, model, provider = get_llm_config("geo_article", "writing")
        if not api_key:
            raise ValueError("LLM API Key 未配置")

        base_url = api_url
        if base_url.endswith("/chat/completions"):
            base_url = base_url[:-len("/chat/completions")]

        system_prompt = """你是一个资深文章修复编辑。你的任务是对已发布的文章进行**精准局部修复**。

## 核心原则
1. **只改有问题的地方**：不要改动文章的标题、结构、段落划分、语气风格
2. **最小化修改**：能改一个词就不改一句话，能改一句话就不改一段话
3. **保持连贯**：修改后的内容必须与上下文自然衔接，读起来没有突兀感
4. **数据准确**：修复后的数据必须与修复指令中的要求完全一致
5. **分数明细**：如果文章中有评分明细（如各维度分数相加=总分），调整总分时必须同步调整明细，确保加总正确

## 严禁行为
- 不要改变文章主旨和论点
- 不要增删段落
- 不要改变排版格式（Markdown格式保持不变）
- 不要加入新的信息或数据（除非修复指令明确要求）
- 不要在输出中包含任何说明性文字，直接输出修复后的完整文章"""

        user_message = f"""## 需要修复的问题

{fix_instructions}

## 原文（请在此基础上只修改上述问题，其余内容完全保持不变）

{content}"""

        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=300.0
        )

        from writing.llm_utils import get_thinking_disabled_params
        from tools.llm_call_tracker import infer_platform_from_url, llm_track, usage_from_response_payload

        async with llm_track(
            "placement_content_fix",
            provider or infer_platform_from_url(api_url, default="unknown"),
            model=model,
            metadata={"title": title[:120]},
        ) as tracker:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message}
                ],
                temperature=0.3,  # 低温度确保稳定性
                max_tokens=16000,
                extra_body=get_thinking_disabled_params(api_url, model)
            )
            input_tokens, output_tokens, cached_tokens = usage_from_response_payload(response)
            _has_content = bool(
                getattr(response, "choices", None)
                and response.choices[0].message
                and (response.choices[0].message.content or "").strip()
            )
            tracker.record(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                success=_has_content,
                error_msg=None if _has_content else "LLM returned empty content",
            )

        result = response.choices[0].message.content or ''

        # 去除可能的思考标签
        if '</think>' in result:
            result = result.split('</think>')[-1].strip()

        return result.strip()

    def _find_brand_nearby_data(self, content: str, brand_keywords: List[str],
                                  unit_hint: str, pattern: str) -> List[tuple]:
        """在文章中品牌名附近（前后200字）查找匹配pattern的数据

        确保只比对与客户品牌相关的数据，不误伤其他品牌/楼盘的数据
        """
        results = []
        seen = set()

        for kw in brand_keywords:
            for bm in re.finditer(re.escape(kw), content):
                # 品牌名前后200字窗口
                start = max(0, bm.start() - 200)
                end = min(len(content), bm.end() + 200)
                window = content[start:end]

                for dm in re.finditer(pattern, window):
                    full_match = dm.group(0)
                    if full_match in seen:
                        continue
                    seen.add(full_match)
                    snippet_start = max(0, dm.start() - 20)
                    snippet = window[snippet_start:dm.end() + 20]
                    # 返回(full_match, first_capture_group, snippet)
                    # first_capture_group 通常是数值部分，方便直接float()
                    group1 = dm.group(1) if dm.lastindex and dm.lastindex >= 1 else full_match
                    results.append((full_match, group1, snippet))

        return results

    def _find_nearby_numbers(self, content: str, context_kw: str,
                               unit: str) -> List[tuple]:
        """在文章中查找与知识库同一语境下的数值

        例如：context_kw="均价", unit="元/m²"
        → 找文章中"均价"附近50字内出现的"数字+元/m²"
        """
        results = []
        if not context_kw or len(context_kw) < 1:
            return results

        # 找context_kw在文章中的所有出现位置
        for m in re.finditer(re.escape(context_kw), content):
            # 在该位置前后80字范围内找数字+单位
            start = max(0, m.start() - 40)
            end = min(len(content), m.end() + 80)
            window = content[start:end]

            # 简化unit匹配
            unit_pattern = re.escape(unit).replace(r'\/', '/?')
            for nm in re.finditer(r'(\d+[\.\d]*)\s*' + unit_pattern, window):
                context_snippet = window[max(0, nm.start()-10):nm.end()+10]
                results.append((nm.group(1), context_snippet))

        return results


# Singleton
_service = None
def get_placement_service() -> PlacementService:
    global _service
    if _service is None:
        _service = PlacementService()
    return _service


# ======================================================================
# 发布推荐：调研数据 × mhz_media 交叉匹配
# 供 api/publish_api.py 调用
# ======================================================================

def recommend_for_publish(industry: str = '', keywords: str = '',
                          article_type: str = '', limit: int = 8) -> list:
    """
    为发布场景生成媒体推荐。
    核心逻辑：从 GEO 调研数据获取高引用率平台 → 与 mhz_media 交叉匹配 → 只推荐"调研好 AND 能买到"的媒体。

    Returns:
        [
            {
                "media_id": 12345,          # mhz_media.id
                "media_name": "搜狐网资讯",
                "platform": "搜狐网",
                "our_price_points": 23400,
                "our_price_yuan": 180.0,
                "inclusion_rate": "92%",
                "avg_publish_time": "2h",
                "engines": ["豆包", "Kimi", "DeepSeek"],
                "geo_score": 32.0,
                "reason": "豆包+Kimi+DeepSeek 三引擎覆盖，引用率最高",
                "data_updated": "2026-03",
            },
            ...
        ]
    """
    # 注:原 v1 路径曾有历史 NameError(recommendations/mhz_media 未初始化 → /media/recommend 生产 500);
    #   已在下方初始化处彻底修复(fail-soft),flag 关时行为从"500"变为"正常返回推荐"(严格改善)。
    from db.publish_db import get_media_list

    svc = get_placement_service()

    # 1. 获取该行业的调研评分矩阵
    industry_scores, matched_industry = svc._match_research_industry(industry)

    if not industry_scores:
        # 无调研数据，使用硬编码默认
        industry_scores = PLATFORM_ENGINE_COVERAGE

    # 按分数排序取 top 平台
    top_platforms = sorted(industry_scores.items(), key=lambda x: x[1]['score'], reverse=True)[:20]

    # 2-3. 交叉匹配（_match_media/_match_wemedia 内部直接 SQL 查）
    used_media_ids = set()
    recommendations = []
    # [彻底修 2026-07-03] 历史重构漏了 recommendations / mhz_media 两处初始化 → 原 v1 路径 NameError。
    #   flag 关 / 非灰度行业时 100% 触发;publish_api /media/recommend 无 try 兜底 → 生产 500。
    #   mhz_media = 可发 GEO 的媒介盒子库存池,与调研高引用平台按名交叉匹配(fail-soft:载入失败降级空池)。
    # [媒体平衡 T1 修 2026-07-29] 原来传 can_geo=True —— 但生产 mhz_media 全表 17712 行
    #   can_geo 恒为 0(2026-07-29 只读实证),该过滤器命中 0 行 → v1 库存池**长期恒空**,
    #   与调研平台的交叉匹配从来没生效过。改按"在售"口径拉池(与 v2 `_match_media` 同义:
    #   is_active),让搜狐/网易/新浪等在售位真正可见。fail-soft 行为不变。
    try:
        mhz_media = get_media_list(page=1, limit=500).get("media", []) or []
    except Exception as _mm_err:
        logger.warning(f"[recommend_for_publish] 载入 mhz_media 失败,降级空池: {_mm_err}")
        mhz_media = []

    # 平台名称标准化映射（调研中的名字 → mhz_media 中可能出现的名字）
    RESEARCH_TO_MHZ_KEYWORDS = {
        '知乎': ['知乎'],
        '搜狐': ['搜狐', 'sohu'],
        '百家号': ['百家号', '百家'],
        '今日头条': ['今日头条', '头条'],
        '抖音': ['抖音'],
        '微博': ['微博', '新浪微博'],
        '网易': ['网易'],
        '凤凰': ['凤凰'],
        '腾讯': ['腾讯'],
        '新浪': ['新浪'],
        '澎湃': ['澎湃'],
        '人民网': ['人民网'],
        '央广网': ['央广'],
        '光明网': ['光明'],
        '和讯': ['和讯'],
        '东方财富': ['东方财富'],
        '雪球': ['雪球'],
        '36氪': ['36氪', '36kr'],
    }

    # 文章类型偏好（影响推荐理由）
    TYPE_PREFERENCE = {
        'authority': '权威排名类适合门户站，增强权威感',
        'faq': '问答类适合 UGC 平台，天然匹配',
        'trend': '趋势类适合新闻源媒体，时效性强',
        'case_study': '案例类适合垂直行业站，精准引用',
        'pitfall': '测评类适合 UGC 平台，用户视角更自然',
    }

    for rp_name, rp_data in top_platforms:
        if len(recommendations) >= limit:
            break

        score = rp_data.get('score', 0)
        engines = rp_data.get('engines', [])

        # 找到 mhz_media 中匹配的媒体
        match_keywords = RESEARCH_TO_MHZ_KEYWORDS.get(rp_name, [rp_name])

        matched_media = []
        for m in mhz_media:
            if m['id'] in used_media_ids:
                continue
            m_name = m.get('media_name') or ''
            m_platform = m.get('platform') or ''
            for kw in match_keywords:
                if kw in m_name or kw in m_platform:
                    matched_media.append(m)
                    break

        # 从匹配到的媒体中选性价比最高的
        if matched_media:
            # 按价格排序取价格较低的资源
            matched_media.sort(key=lambda x: x.get('price') or x.get('our_price_points') or 999999)
            best = matched_media[0]
            used_media_ids.add(best['id'])

            # 构建推荐理由
            engine_str = '+'.join(engines[:3]) if engines else '未知'
            base_reason = f"{engine_str} 覆盖，调研引用率高"
            if article_type and article_type in TYPE_PREFERENCE:
                base_reason += f"；{TYPE_PREFERENCE[article_type]}"

            recommendations.append({
                "media_id": best['id'],
                "media_name": best['media_name'],
                "platform": best.get('platform', ''),
                "category": best.get('category', ''),
                "price": float(best.get('price') or best.get('price_vip') or 0),
                "our_price_points": best.get('our_price_points') or 0,
                "our_price_yuan": float(best.get('our_price_yuan') or 0),
                "inclusion_rate": best.get('inclusion_rate', ''),
                "avg_publish_time": best.get('avg_publish_time', ''),
                "engines": engines,
                "geo_score": score,
                "reason": base_reason,
                "data_updated": matched_industry or "默认数据",
            })

    # 4. 如果调研匹配不够，补充 can_geo=1 的媒体
    if len(recommendations) < limit:
        geo_result = get_media_list(
            page=1, limit=limit * 2,
            sort_by="our_price_points", sort_dir="asc",
        )
        for m in geo_result.get("media", []):
            if len(recommendations) >= limit:
                break
            if m['id'] in used_media_ids:
                continue
            used_media_ids.add(m['id'])
            recommendations.append({
                "media_id": m['id'],
                "media_name": m['media_name'],
                "platform": m.get('platform', ''),
                "category": m.get('category', ''),
                "price": float(m.get('price') or m.get('price_vip') or 0),
                "our_price_points": m.get('our_price_points') or 0,
                "our_price_yuan": float(m.get('our_price_yuan') or 0),
                "inclusion_rate": m.get('inclusion_rate', ''),
                "avg_publish_time": m.get('avg_publish_time', ''),
                "engines": [],
                "geo_score": 0,
                "reason": "GEO 可发媒体，性价比推荐",
                "data_updated": "",
            })

    return recommendations


# ==================== V2 推荐引擎（垂直/通用分组 + 自媒体） ====================

# 所有行业都高引用的通用平台，排除后剩下的才是"行业垂直"
# [媒体平衡 T2 · 2026-07-29] 手写的 13 名单已废除。
# "主干 vs 垂类"改由 citation_domain_weights 的域族先验(带 role)驱动:
# 主干 = portal / tech_community / ugc_qa 三个角色 —— 这三类就是飞轮实测的
# 被引主干(搜狐 1969 / 网易 1483 / 博客园 961 / 知乎 619 / 头条 425)。
# 老名单把博客园、CSDN 归进"垂直",于是技术社区永远进不了通用位;新口径按角色分。
def _is_trunk_platform(platform_name: str, *, media_balance_enabled: bool = False) -> bool:
    """通用主干判定（替代 ``_GENERIC_PLATFORMS`` 成员测试）。

    [灰度 2026-07-29] ``media_balance_enabled=False`` 时回到上线前的老名单成员测试，
    分组结果与本批改动前逐位一致。

    [2026-07-28 C-3] 默认值 True → **False**。原默认值意味着"漏传即走新口径"，
    与整套灰度 fail-closed 的设计意图相反。5 个生产调用点今天都显式传参所以不是 bug，
    但那是给下一个改这里的人埋的坑：新增调用点忘了传，就会在灰度关闭状态下悄悄走新分组。
    """
    name = str(platform_name or "")
    if not media_balance_enabled:
        from services.media_balance_gate import LEGACY_GENERIC_PLATFORMS

        return name in LEGACY_GENERIC_PLATFORMS
    try:
        from services.citation_domain_weights import is_trunk_domain

        return is_trunk_domain("", name)
    except Exception:
        return False

# [CTO-15.23 2026-05-18 v2-F] 行业头部白名单 fallback
# 用于:geo_engine_stats 无匹配行业时兜底 / 用户行业拼写极偏(如"高端男装定制")时强保推荐
# SSH 实证(2026-05-18):mhz_media.geo_rank_platform 字段对一线品牌 87% 缺失 ·
# 媒介盒子供应方未维护汽车之家/齐家网/房天下/太平洋汽车的引擎覆盖标注 ·
# 因此除了 geo_engine_stats 真引用数据 · 必须叠加静态行业头部白名单 + 多维打分。
INDUSTRY_HEAD_FALLBACK = {
    '汽车': ['汽车之家', '太平洋汽车', '懂车帝', '易车', '爱卡汽车'],
    # [WO_267 · 待策展] 物流运输从「汽车」拆出(Review 09-23 裁定①):运输/物流/货运/客运
    #   以前被 keyword_map 算进汽车,拿的就是上面这 5 家。先**原样复制**,保证拆分当天
    #   这些客户的推荐不变;物流自己的头部媒体待运营策展后替换。
    '物流运输': ['汽车之家', '太平洋汽车', '懂车帝', '易车', '爱卡汽车'],
    '装修建材': ['齐家网', '房天下', '搜房网', '太平洋家居', '土巴兔', '住小帮'],
    '房地产': ['房天下', '吉屋', '乐居', '搜狐焦点', '齐家网'],
    '教育培训': ['中国教育在线', '新东方在线', '学信网', '环球网校', '高顿教育'],
    '科技数码': ['中关村在线', 'IT之家', '36氪', 'CSDN', '太平洋电脑'],
    '医疗健康': ['39健康', '丁香园', '丁香医生', '寻医问药'],
    '食品餐饮': ['美团', '大众点评', '中国食品报', '中国餐饮网'],
    '时尚美妆': ['ELLE', '美丽说', '瑞丽网'],
    '母婴亲子': ['宝宝树', '亲宝宝', '摇篮网'],
    '旅游酒店': ['携程', '马蜂窝', '去哪儿', '驴妈妈'],
    '金融理财': ['和讯', '东方财富', '雪球', '新浪财经'],
    '法律商务': ['华律网', '找法网', '法律快车'],
    '电商零售': ['什么值得买', '小红书', '京东', '淘宝'],
    '文娱游戏': ['哔哩哔哩', '游民星空', '17173', 'TapTap'],
}

# 一线门户白名单(portal_tier_bonus 用)
_TOP_PORTAL_MEDIAS = {
    '网易网', '新华网', '人民网', '光明网', '凤凰网', '央视网',
    '新浪网', '中华网', '环球网', '国际在线', '中国广播网', '中国网',
}

# v2-F 默认最低价格门槛(<¥5 区 SSH 实证 0/616 有任何质量信号 · 100% 冒名小站)
_V2F_MIN_PRICE = 5.0

# [v2-F BUG 5 修] SQL 层粗排表达式 · 让候选池 LIMIT 截断时优先保留高质量
# 实证:中国教育在线 24 SKU 老 ORDER BY price ASC LIMIT 12 漏掉¥140 主站(全 6/6 引擎)
# 改:engine×10 + auth×5 + 甜点价×3 + ¥15-30 ×1 粗排 · Python 端 _quality_score_v2f 再细排
_V2F_SQL_PREORDER = (
    "(CASE WHEN geo_rank_platform LIKE '%%a%%' THEN 1 ELSE 0 END +"
    " CASE WHEN geo_rank_platform LIKE '%%b%%' THEN 1 ELSE 0 END +"
    " CASE WHEN geo_rank_platform LIKE '%%c%%' THEN 1 ELSE 0 END +"
    " CASE WHEN geo_rank_platform LIKE '%%d%%' THEN 1 ELSE 0 END +"
    " CASE WHEN geo_rank_platform LIKE '%%e%%' THEN 1 ELSE 0 END +"
    " CASE WHEN geo_rank_platform LIKE '%%f%%' THEN 1 ELSE 0 END) * 10"
    " + CASE WHEN authority_media = 1 THEN 5 ELSE 0 END"
    " + CASE WHEN price BETWEEN 30 AND 60 THEN 3 WHEN price BETWEEN 15 AND 30 THEN 1 ELSE 0 END"
)


def _count_geo_engines(geo_rank_platform: str) -> int:
    """数 geo_rank_platform 字符串中 a-f 字母数 · z 不计入引擎覆盖"""
    if not geo_rank_platform:
        return 0
    codes = {c.strip().lower() for c in geo_rank_platform.split(',') if c.strip()}
    return sum(1 for c in 'abcdef' if c in codes)


def _quality_score_v2f(
    row: dict,
    industry_match_score: float = 0.5,
    citation_strength: float | None = None,
    success_factor: float | None = None,
) -> float:
    """[v2-F] 媒体多维质量加权打分(0-1.0)

    公式(老板已拍 v2-F):
      0.30 × geo_engine_coverage / 6
    + 0.20 × authority_bonus (authority_media + geo_rank)
    + 0.25 × price_sweet_spot
    + 0.15 × portal_tier_bonus
    + 0.10 × industry_match

    [WP12 P0-1 · Master SSOT v2.4 ③④] 当传入 ``citation_strength``(真实被引域
    权重)时切换到 v2-G 公式:**真实被引数据成为主排序信号**,人工 authority
    白名单从 0.20 降到 0.05。依据是飞轮实证 —— whitelist 篇均引用 1.51 /
    rank 5.29 vs gray 1.46 / 5.70,人工分级几乎无预测力;而我方发布域与真实
    被引域几乎不重叠,渠道错配才是闭环第一瓶颈。

      0.35 × citation_strength     ← 真实被引观测(主信号)
    + 0.20 × geo_engine_coverage / 6
    + 0.20 × price_sweet_spot
    + 0.12 × portal_tier_bonus
    + 0.08 × industry_match
    + 0.05 × authority_bonus       ← 人工白名单(降为辅助)

    ``citation_strength=None`` 时保持 v2-F 原式逐位不变,所以飞轮无数据的
    环境(新库/调研未跑)排序结果与本次改动前完全一致。
    """
    try:
        price = float(row.get('price') or 0)
    except (TypeError, ValueError):
        price = 0
    auth = int(row.get('authority_media') or 0)
    try:
        geo_rank = int(row.get('geo_rank') or 0)
    except (TypeError, ValueError):
        geo_rank = 0
    portal = (row.get('portal_media') or '').strip()
    engine_count = _count_geo_engines(row.get('geo_rank_platform') or '')

    # 1. 价格甜点区
    if 30 <= price <= 60:
        ps = 1.0
    elif 60 < price <= 150:
        ps = 0.7
    elif 15 <= price < 30:
        ps = 0.6
    elif 150 < price <= 500:
        ps = 0.5
    elif price > 500:
        ps = 0.4
    elif 5 <= price < 15:
        ps = 0.3
    else:  # <¥5 走默认硬过滤 · 这里给 0 保险
        ps = 0.0

    # 2. authority + geo_rank 加权
    auth_score = (0.5 if auth else 0.0) + (0.5 if geo_rank > 0 else 0.0)

    # 3. portal 层级
    if portal in _TOP_PORTAL_MEDIAS:
        pt = 1.0
    elif portal == '其他门户':
        pt = 0.5
    elif portal == '垂直媒体':
        pt = 0.3
    else:
        pt = 0.1

    # 4. GEO 引擎覆盖
    ge = engine_count / 6.0

    if citation_strength is None:
        # v2-F 原式(飞轮无被引数据时的兜底,逐位与改动前一致)
        base = (
            0.30 * ge
            + 0.20 * auth_score
            + 0.25 * ps
            + 0.15 * pt
            + 0.10 * industry_match_score
        )
    else:
        cs = max(0.0, min(1.0, float(citation_strength)))
        base = (
            0.35 * cs
            + 0.20 * ge
            + 0.20 * ps
            + 0.12 * pt
            + 0.08 * industry_match_score
            + 0.05 * auth_score
        )

    # [媒体平衡 T3 · 2026-07-29] 发布成功率因子。被引强 ≠ 发得进去:
    # 搜狐系实测拒稿 52.2%(67 单 published 9)。样本 <5 单的媒体 factor=1.0,
    # 所以 ``success_factor=None`` 或新媒体时本行为零变化。
    if success_factor is None:
        return base
    return base * max(0.0, min(1.2, float(success_factor)))


def _record_media_balance_shadow(*, user_id: int, brand_id: int, industry: str,
                                 matched_industry: str, media_type: str, keyword: str,
                                 applied_enabled: bool, gate_reason: str,
                                 applied_trunk: list, applied_vertical: list,
                                 shadow_trunk: list, shadow_vertical: list) -> None:
    """[2026-07-28 C-2] 落一条「新旧分组对照」存证。**只写不读，绝不影响推荐结果**。

    落在既有 `audit_logs`（`create_audit_log`）里而不是新建表：本单要求无迁移，
    而仓内"自愈式建表不跑就不建、失败还静默"是有前科的，能不新建就不新建。
    关联口径：``entity_type='brand' / entity_id=brand_id``，行业写进两份快照，
    按 quote 归因时用 brand + industry 对回去。

    整段 best-effort：任何异常都吞掉——存证不能反过来打断代理的推荐请求。
    """
    try:
        if applied_trunk == shadow_trunk and applied_vertical == shadow_vertical:
            return  # 两套一模一样(该行业没有域族/老名单分歧)→ 不写噪声行
        from db.auth_db import create_audit_log

        common = {
            "industry_raw": industry or "",
            "matched_industry": matched_industry or "",
            "media_type": media_type,
            "keyword": keyword or "",
            "gate_reason": gate_reason,
        }
        create_audit_log(
            user_id=user_id or None,
            action="media_balance_shadow_compare",
            module="media_balance",
            entity_type="brand",
            entity_id=brand_id or None,
            summary=(f"媒体平衡影子对照 · 生效口径={'新' if applied_enabled else '旧'}"
                     f"({gate_reason}) · 行业={matched_industry or industry or '-'}"
                     f" · {media_type} · 主干 {len(applied_trunk)}→{len(shadow_trunk)}"),
            # before = 未被采用的那套(影子)  /  after = 实际生效的那套
            before={**common, "variant": "new" if not applied_enabled else "legacy",
                    "trunk": shadow_trunk, "vertical": shadow_vertical},
            after={**common, "variant": "new" if applied_enabled else "legacy",
                   "trunk": applied_trunk, "vertical": applied_vertical},
        )
    except Exception as exc:  # pragma: no cover - 存证失败绝不影响主流程
        logger.warning(f"[media-balance-shadow] 落库失败(不影响推荐): {exc}")


def _research_status_safe(svc, industry: str, brand: Optional[dict]) -> Optional[dict]:
    """[WO_267 §4] 给非付费推荐入口附「该行业的调研开没开」。只是说明字段 ——
    取不到(库不通 / 调用方给的是替身服务)记 warning 返回 None,绝不影响推荐本身。"""
    try:
        return svc.research_status(industry or '', brand=brand)
    except Exception as e:
        logger.warning(f"[industry] research_status 不可用: {e}")
        return None


def recommend_for_publish_v2(industry: str = '', media_type: str = 'media',
                              limit: int = 8, user_id: int = 0,
                              keyword: str = '', brand_id: int = 0,
                              with_question_family: bool = True,
                              industry_brand: Optional[dict] = None) -> dict:
    """
    V2 推荐引擎 — 垂直/通用分组，支持软文(media)和自媒体(wemedia)。

    [CTO-15.23 2026-05-18 v2-F 升级] 接 user_id 用于 90 天去重 ·
    industry_scores 空时叠加 INDUSTRY_HEAD_FALLBACK 行业头部白名单兜底。

    [性能第二刀 §B 2026-07-28] ``with_question_family=False`` 时跳过 T2 问题族
    mix / 组合折算。**默认 True,老调用零变化。**
    端点里 media / wemedia 两次调用只差 ``media_type``,而问题族 mix 与
    ``media_type`` 无关 —— 两次算出来的是同一份,且 ``publish_api`` 只读
    ``media_result`` 那一份,wemedia 那份算完直接丢。生产 ``flywheel_judgment_log``
    里判定**成对出现**就是这个重复的指纹(131 行 / 15 个不同入参)。
    所以端点给 wemedia 传 False,省掉一整套聚合查询与一次多余的判定。

    Returns:
        {
            "vertical": [...],   # 行业垂直推荐（高参考价值）
            "generic": [...],    # 通用高引用推荐
            "matched_industry": "教育培训",
        }
    """
    from db.connection import get_connection

    svc = get_placement_service()
    # [WO_267] industry_brand = 调用方**已鉴权**后给的品牌行(名称 / 备注 / 种子词当判定上下文);
    #   brand_id 本身不带鉴权(灰度与去重用),所以不拿它去读品牌。
    industry_scores, matched_industry = svc._match_research_industry(industry, brand=industry_brand)
    research_status = _research_status_safe(svc, industry, industry_brand)

    if not industry_scores:
        industry_scores = PLATFORM_ENGINE_COVERAGE

    # 按分数排序所有平台
    all_platforms = sorted(industry_scores.items(), key=lambda x: x[1]['score'], reverse=True)

    # 分组：垂直 vs 通用
    # [灰度 2026-07-29] 新推荐口径默认关 · 白名单开(services/media_balance_gate.py)。
    # 关闭时:老名单分组 + 不乘成功率 + 不返回两块 → 与上线前逐位一致。
    # [2026-07-28 C-1] 放量维度改成行业：原始 industry 与归一后的 matched_industry 任一命中即算命中
    #   （调用侧不必猜配置里写的是哪个写法）。user_id/brand_id 仍传：前者是调试后门，后者兼容既有配置。
    from services.media_balance_gate import evaluate_gate

    _gate = evaluate_gate(
        user_id=user_id or None, brand_id=brand_id or None,
        industry_keys=[industry, matched_industry],
    )
    _mb_on, _mb_reason = _gate["enabled"], _gate["reason"]

    vertical_platforms = [(n, d) for n, d in all_platforms
                          if not _is_trunk_platform(n, media_balance_enabled=_mb_on)]
    generic_platforms = [(n, d) for n, d in all_platforms
                         if _is_trunk_platform(n, media_balance_enabled=_mb_on)]
    # [2026-07-28 C-2 shadow compare] 新旧两套分组都算出来，**推荐仍只用 `_mb_on` 那一套**。
    #   灰度只能回答"看不看得到"，回答不了"好不好"——灰度结束时手上只有开着那批的数据、没有对照组。
    #   分组是纯计算（不额外调 LLM、不额外花钱），所以两套都算、把没被采用的那套落库存证，
    #   一周后可直接跑「同样输入、新旧各推了什么、哪个实际发布成功率高」。
    _shadow_trunk = [n for n, _ in all_platforms
                     if _is_trunk_platform(n, media_balance_enabled=not _mb_on)]
    _shadow_vertical = [n for n, _ in all_platforms
                        if not _is_trunk_platform(n, media_balance_enabled=not _mb_on)]

    # [v2-F] 行业头部白名单兜底:geo_engine_stats 该行业引用数据稀疏 → 补强一线品牌
    # 例:用户行业"高端男装定制" matched 不到任何调研行业 · 此时全靠白名单避免 0 推荐
    head_names = _resolve_industry_head_fallback(industry, matched_industry)
    if head_names:
        existing = {n for n, _ in vertical_platforms}
        for hn in head_names:
            if hn not in existing:
                # 给白名单平台一个中等 score(20) 让它进推荐池但不压过 geo_engine_stats top 平台
                vertical_platforms.append((hn, {'score': 20, 'engines': [], 'citation_rate': 0}))

    # [v2-F] 90 天历史去重 · 拉该 user 已发过的 media_id 排除
    used_ids: set = set()
    if user_id:
        try:
            from db.meijiehezi_db import get_user_published_media_ids
            used_ids = get_user_published_media_ids(user_id, days=90)
        except Exception as e:
            logger.warning(f"[v2-F] 90 天去重拉取失败 user={user_id}: {e}")

    # R6-H/R5d handoff: if an industry has an admin-reviewed media flywheel
    # takeover policy and the runtime switch is enabled, official recommendation
    # can consume approved bindings.  All failure paths fall back to legacy v2.
    try:
        from services.media_flywheel_recommendation import recommend_from_media_flywheel

        flywheel_result = recommend_from_media_flywheel(
            industry=industry or matched_industry or "",
            media_type=media_type,
            limit=limit,
            exclude_media_ids=used_ids if media_type != "wemedia" else set(),
        )
        if flywheel_result.get("used"):
            flywheel_result["research_status"] = research_status
            return flywheel_result
    except Exception as e:
        logger.warning(f"[media-flywheel] takeover recommendation skipped: {e}")

    # [WP12 P0-1 · Master SSOT v2.4 ③④] 真实被引域权重成为主排序信号。
    # 表拉不到时 citation_table 为 None,下游 _quality_score_v2f 回落 v2-F 原式。
    citation_table = _load_citation_weight_table(industry or matched_industry or '')

    # [媒体平衡 T3 · 2026-07-29] 发布成功率表(180 天窗口 · 样本 <5 单 factor=1.0)。
    # 拉不到时为空 dict → 下游 success_factor=None → 评分逐位不变。
    # 灰度关时给空表 → 下游 success_factor 恒 None → 评分公式逐位回到上线前
    success_table = _load_media_success_table() if _mb_on else {}

    if media_type == 'wemedia':
        result = _match_wemedia(vertical_platforms, generic_platforms, matched_industry, limit,
                                raw_industry=industry, citation_table=citation_table,
                                success_table=success_table, media_balance_enabled=_mb_on)
    else:
        result = _match_media(vertical_platforms, generic_platforms, matched_industry, limit,
                              exclude_media_ids=used_ids, citation_table=citation_table,
                              success_table=success_table, media_balance_enabled=_mb_on)
    result["recommendation_source"] = "legacy"
    result["media_balance_enabled"] = _mb_on
    result["media_balance_reason"] = _mb_reason  # [C-4] 区分 configured_off / settings_unreadable / 未命中
    _record_media_balance_shadow(
        user_id=user_id, brand_id=brand_id, industry=industry,
        matched_industry=matched_industry, media_type=media_type, keyword=keyword,
        applied_enabled=_mb_on, gate_reason=_mb_reason,
        applied_trunk=[n for n, _ in generic_platforms],
        applied_vertical=[n for n, _ in vertical_platforms],
        shadow_trunk=_shadow_trunk, shadow_vertical=_shadow_vertical,
    )
    result.update(_citation_result_annotations(citation_table, result))
    # [P1-5b 2026-08-14] D6-B 媒体组合消费点:该品牌真实被引的发布域(账本
    # body_proof 口径,advisory)。样本不足 → available=False + 「暂无足够样本」,
    # 绝不显示空榜假象;查询失败同样 fail-soft(O1)。
    try:
        from services.reco_outcome_feedback import brand_media_feedback

        result["brand_reco_feedback"] = brand_media_feedback(int(brand_id or 0))
    except Exception as _brf_err:
        logger.warning(f"[D6-B] 品牌被引域反馈不可用: {_brf_err}")
        result["brand_reco_feedback"] = {"available": False, "reason": "aggregation_unavailable"}
    if _mb_on and with_question_family:
        # [媒体平衡 T2] 问题族真实被引 mix → 默认"主干 N + 垂类 M"组合(advisory 可改)。
        result.update(_question_family_annotations(
            keyword=keyword, industry=industry or matched_industry or '', limit=limit,
        ))
    else:
        # 灰度关(或调用方明示不要 T2):不算也不返回,顺带省掉两次飞轮聚合查询
        result["question_family_mix"] = None
        result["combination_plan"] = None
    result["research_status"] = research_status
    return result


def _load_media_success_table() -> dict:
    """[T3] 发布成功率表 · 任何失败都降级为空表(= 不惩罚任何媒体)。"""
    try:
        from services.media_publish_success import get_media_success_table

        return get_media_success_table()
    except Exception as e:
        logger.warning(f"[media-success] 成功率表不可用,推荐回落无成功率加权: {e}")
        return {}


def _success_factor_for(success_table: dict, media_name: str) -> float | None:
    """把成功率解析成评分因子；样本不足或无表时返回 None(零行为变化)。"""
    if not success_table:
        return None
    try:
        from services.media_publish_success import resolve_media_success

        stat = resolve_media_success(success_table, media_name)
        return stat.factor if stat.sample_sufficient else None
    except Exception:
        return None


def _success_descriptor(success_table: dict, media_name: str) -> dict:
    """成功率描述符(给推荐卡展示用)；无表/无样本时全部为 None,前端不渲染。"""
    if not success_table:
        return {"success_pct": None, "settled": 0, "factor": None, "sample_sufficient": False}
    try:
        from services.media_publish_success import resolve_media_success

        return resolve_media_success(success_table, media_name).as_dict()
    except Exception:
        return {"success_pct": None, "settled": 0, "factor": None, "sample_sufficient": False}


def _question_family_annotations(*, keyword: str, industry: str, limit: int) -> dict:
    """[T2] 问题族被引 mix + 默认组合。任何失败都不影响既有推荐结果。"""
    try:
        from services.question_family_mix import build_question_family_mix, plan_combination

        mix = build_question_family_mix(keyword=keyword, industry=industry)
        plan = plan_combination(mix, total_slots=max(2, int(limit or 8)))
        annotations = {
            "question_family_mix": mix.as_dict(),
            "combination_plan": plan.as_dict(),
        }
        # [三包合批 2026-07-29 · Review-CTO] 飞轮 B3 接入点。统计口径仍由 T2 的
        # 代码算(上面两行);B3 只接管"八个坑位给谁"这一步取舍,且判断点默认关
        # (FLYWHEEL_JUDGE_MEDIA_MIX_DECISION 未设置 → 原样返回 annotations)。
        #
        # [性能第二刀 §B 2026-07-28] 改走 **_cached 变体**:请求路径只读缓存,
        # 未命中就用 T2 代码算好的确定性组合并投递后台预热。改前是每次页面加载
        # 同步发一次真实 LLM(cProfile:httpx.post 1.65s),生产实测一天 131 次
        # 判定只覆盖 15 个不同 (industry, keyword)、并在 101 次时打满日额度。
        # 两个变体内部任何异常路径都返回原样入参,发布推荐不因智能层不可用而变差。
        from services.flywheel_media_mix_choice import refine_media_combination_cached

        return refine_media_combination_cached(annotations, keyword=keyword, industry=industry)
    except Exception as e:
        logger.warning(f"[question-mix] 组合折算不可用: {e}")
        return {"question_family_mix": None, "combination_plan": None}


def _citation_result_annotations(citation_table, result: dict) -> dict:
    """[WP12 P0-1] 给推荐结果附上被引权重摘要 + 渠道建议(全部 advisory)。"""
    if citation_table is None:
        return {
            "citation_weight_summary": None,
            "citation_ranking_signal": "legacy_v2f",
            "channel_advisories": [],
        }
    try:
        from services.citation_domain_weights import build_channel_advisories

        recommended_domains = [
            str(item.get("ai_citation_domain") or "")
            for group in ("vertical", "generic")
            for item in (result.get(group) or [])
        ]
        return {
            "citation_weight_summary": citation_table.summary(),
            "citation_ranking_signal": (
                "observed_citation_v2g" if citation_table.has_observed_data else "legacy_v2f"
            ),
            "channel_advisories": build_channel_advisories(
                citation_table, recommended_domains=recommended_domains
            ),
        }
    except Exception as e:  # pragma: no cover
        logger.warning(f"[citation-weights] annotation failed: {e}")
        return {
            "citation_weight_summary": None,
            "citation_ranking_signal": "legacy_v2f",
            "channel_advisories": [],
        }


def _load_citation_weight_table(industry: str):
    """[WP12 P0-1] 拉真实被引域权重表 · 任何失败都回落到 None(不阻断推荐)。"""
    try:
        from services.citation_domain_weights import get_citation_domain_weights

        table = get_citation_domain_weights(industry=str(industry or "").strip())
        return table if table.has_observed_data else table
    except Exception as e:  # pragma: no cover - defensive, publish must not break
        logger.warning(f"[citation-weights] table unavailable: {e}")
        return None


def _citation_descriptor_for_row(table, row: dict, platform_name: str = "") -> dict:
    """把一行媒体库存解析成被引强度描述(域 → 域族 → 中性先验)。"""
    if table is None:
        return {}
    try:
        from services.citation_domain_weights import resolve_domain_strength

        return resolve_domain_strength(
            table,
            domain=str(row.get("entrance_link") or row.get("domain") or ""),
            media_name=str(row.get("media_name") or row.get("account_name") or ""),
            platform_name=str(platform_name or row.get("platform") or ""),
        )
    except Exception as e:  # pragma: no cover
        logger.warning(f"[citation-weights] resolve failed: {e}")
        return {}


def _resolve_industry_head_fallback(raw_industry: str, matched_industry: str) -> list:
    """[v2-F] 解析行业头部白名单 INDUSTRY_HEAD_FALLBACK。

    🔴 WO_267:原来的第 2 步「双向子串」与第 3 步手写 keyword_map 都删了 ——
       子串会让「装修建材」这个键被任何含「建材」的长串命中,keyword_map 里单字别名
       「药」会把「农药」拉进医疗。现在:①白名单键精确命中(matched 就是调研行业名时);
       ②否则按行业大类字典判**主类**,取该大类的 `head_fallback_key`;③判不出 / 该大类
       没挂白名单 ⇒ 空(调用方本来就接受空)。
    """
    search = (matched_industry or raw_industry or '').strip()
    if not search:
        return []
    # 1. 精确命中
    if search in INDUSTRY_HEAD_FALLBACK:
        return list(INDUSTRY_HEAD_FALLBACK[search])
    # 2. 大类字典
    key = head_fallback_key_for(resolve_industry(search))
    return list(INDUSTRY_HEAD_FALLBACK.get(key, [])) if key else []


def _match_media(vertical_platforms: list, generic_platforms: list,
                 matched_industry: str, limit: int,
                 exclude_media_ids: set = None,
                 citation_table=None, success_table: dict | None = None,
                 media_balance_enabled: bool = False) -> dict:   # [C-3] fail-closed:漏传即老口径
    """软文媒体交叉匹配 — 直接 SQL 按平台名查，不预加载候选池"""
    from db.connection import get_connection

    RESEARCH_TO_MHZ = {
        '知乎': ['知乎'], '搜狐': ['搜狐', 'sohu'], '百家号': ['百家号', '百家'],
        '今日头条': ['今日头条', '头条'], '抖音': ['抖音'], '微博': ['微博', '新浪微博'],
        '网易': ['网易'], '凤凰': ['凤凰'], '腾讯': ['腾讯'], '新浪': ['新浪'],
        '澎湃': ['澎湃'], '人民网': ['人民网'], '央广网': ['央广'], '光明网': ['光明'],
        '和讯': ['和讯'], '东方财富': ['东方财富'], '雪球': ['雪球'], '36氪': ['36氪', '36kr'],
        '汽车之家': ['汽车之家'], '懂车帝': ['懂车帝'], '房天下': ['房天下'],
        '携程': ['携程'], 'CSDN': ['CSDN'], '住小帮': ['住小帮'],
        '中国教育在线': ['中国教育在线', '教育在线'], '齐家网': ['齐家'],
        '太平洋家居网': ['太平洋家居'], '土巴兔': ['土巴兔'],
    }

    # [v2-F] used_ids 起点 = 90 天历史去重(exclude_media_ids) · 每平台再追加避免重复
    used_ids: set = set(exclude_media_ids or set())
    conn = get_connection()
    try:
        cur = conn.cursor()

        def _find_best(platforms: list, max_count: int) -> list:
            results = []
            for rp_name, rp_data in platforms:
                if len(results) >= max_count:
                    break
                kws = RESEARCH_TO_MHZ.get(rp_name, [rp_name])
                # [v2-F] 直接 SQL 按每个关键词 ILIKE 查 + 拉打分所需字段(authority/geo_rank/portal/engine)
                # 默认 price >= 5(<¥5 SSH 实证 0/616 有质量信号 · 100% 冒名小站)
                # [BUG 5 修 2026-05-18] ORDER BY 用 _V2F_SQL_PREORDER 粗排 · 防 LIMIT 截掉好货
                or_clauses = " OR ".join(["media_name ILIKE %s"] * len(kws))
                params = [f"%{kw}%" for kw in kws]
                cur.execute(f"""
                    SELECT id, media_name, price, our_price_yuan, our_price_points,
                           resource_type_name, inclusion_rate,
                           authority_media, geo_rank, geo_rank_platform, portal_media, entrance_link
                    FROM mhz_media
                    WHERE is_active = true AND price >= 5.0
                          AND media_name NOT ILIKE '%%套餐%%'
                          AND resource_type_name != '套餐系列'
                          AND ({or_clauses})
                    ORDER BY {_V2F_SQL_PREORDER} DESC, price ASC
                    LIMIT 8
                """, params)
                # [v2-F] 候选先过 90 天去重 · 再用 _quality_score_v2f 多维加权排序 · 选 top 1
                # 公式:0.30 GEO 引擎覆盖 + 0.20 authority + 0.25 价格甜点 + 0.15 portal 层级 + 0.10 行业匹配
                candidates = []
                for row in cur.fetchall():
                    d = dict(row)
                    if d['id'] in used_ids:
                        continue
                    # [WP12 P0-1] 真实被引强度 · 表不可用时 descriptor 为空 →
                    # _quality_score_v2f(citation_strength=None) 走 v2-F 原式。
                    d['_citation'] = _citation_descriptor_for_row(citation_table, d, rp_name)
                    candidates.append(d)
                if not candidates:
                    continue
                # 多维加权排序(降序)
                candidates.sort(
                    key=lambda r: _quality_score_v2f(
                        r, citation_strength=(r.get('_citation') or {}).get('strength'),
                        success_factor=_success_factor_for(success_table, r.get('media_name') or ''),
                    ),
                    reverse=True,
                )
                d = candidates[0]
                used_ids.add(d['id'])
                citation = d.get('_citation') or {}
                engines = rp_data.get('engines', [])
                is_vertical = not _is_trunk_platform(rp_name, media_balance_enabled=media_balance_enabled)
                engine_count = _count_geo_engines(d.get('geo_rank_platform') or '')
                _success_factor = _success_factor_for(success_table, d.get('media_name') or '')
                _success_stat = _success_descriptor(success_table, d.get('media_name') or '')
                quality = _quality_score_v2f(
                    d, citation_strength=citation.get('strength'), success_factor=_success_factor,
                )
                # 推荐理由:行业真权威 + GEO 覆盖 / 一线门户 / 甜点价 / 多维择优
                portal = (d.get('portal_media') or '').strip()
                reason_bits = [f"{rp_name}"]
                if engines:
                    reason_bits.append(f"{'+'.join(engines[:3])} 引擎引用")
                if is_vertical:
                    reason_bits.append("行业垂直")
                if engine_count >= 4:
                    reason_bits.append(f"GEO {engine_count}/6 覆盖")
                if portal in _TOP_PORTAL_MEDIAS:
                    reason_bits.append(f"{portal}一线")
                price_val = float(d.get('price') or 0)
                if 30 <= price_val <= 60:
                    reason_bits.append("¥30-60 性价比甜点")
                # [WP12 P0-1] 被引强度写进推荐理由 —— 不藏在算法里(Owner 明示)
                if citation.get('source') == 'observed' and int(citation.get('citation_count') or 0) > 0:
                    reason_bits.append(
                        f"AI {citation.get('strength_label')}(近{(citation_table.window_days if citation_table else 90)}天被引 {citation.get('citation_count')} 次)"
                    )
                elif citation.get('family_label'):
                    reason_bits.append(f"{citation.get('family_label')}域族")
                # [T3] 成功率只在样本足够时出话术,避免用 2 单的噪声吓退代理
                if _success_stat.get('sample_sufficient') and _success_stat.get('success_pct') is not None:
                    reason_bits.append(
                        f"发布成功率 {_success_stat['success_pct']}%（近 180 天 {_success_stat['settled']} 单）"
                    )
                results.append({
                    "media_id": d['id'],
                    "media_name": d['media_name'],
                    "platform": rp_name,
                    "category": d.get('resource_type_name') or '',
                    "price": price_val,
                    "our_price_points": d.get('our_price_points') or 0,
                    "our_price_yuan": float(d.get('our_price_yuan') or 0),
                    "inclusion_rate": d.get('inclusion_rate') or '',
                    "authority_media": int(d.get('authority_media') or 0),
                    "geo_rank": int(d.get('geo_rank') or 0),
                    "geo_rank_platform": d.get('geo_rank_platform') or '',
                    "geo_engine_coverage": engine_count,
                    "portal_media": portal,
                    "entrance_link": d.get('entrance_link') or '',
                    "engines": engines,
                    "geo_score": rp_data.get('score', 0),
                    "citation_rate": rp_data.get('citation_rate', 0),
                    "quality_score_v2f": round(quality, 3),
                    # [WP12 P0-1] AI 引用强度对代理可见(不藏在算法里)
                    "ai_citation_strength": round(float(citation.get('strength') or 0), 3),
                    "ai_citation_label": citation.get('strength_label') or '',
                    "ai_citation_count": int(citation.get('citation_count') or 0),
                    "ai_citation_source": citation.get('source') or '',
                    "ai_citation_domain": citation.get('domain') or '',
                    "ai_citation_family": citation.get('family_label') or '',
                    # [媒体平衡 T3] 发布成功率对代理可见 —— 被引强 ≠ 发得进去
                    "publish_success_rate": _success_stat.get('success_pct'),
                    "publish_success_sample": _success_stat.get('settled'),
                    "publish_success_factor": _success_stat.get('factor'),
                    "reason": " · ".join(reason_bits),
                    "media_type": "media",
                })
            return results

        half = max(limit // 2, 2)
        vertical = _find_best(vertical_platforms, half)
        generic = _find_best(generic_platforms, limit - len(vertical))

        return {"vertical": vertical, "generic": generic, "matched_industry": matched_industry or ''}
    finally:
        conn.close()


def _match_wemedia(vertical_platforms: list, generic_platforms: list,
                   matched_industry: str, limit: int, raw_industry: str = '',
                   citation_table=None, success_table: dict | None = None,
                   media_balance_enabled: bool = False) -> dict:   # [C-3] fail-closed:漏传即老口径
    """自媒体推荐 — 按行业+性价比，不依赖垂直平台匹配"""
    from db.connection import get_connection

    # geo_engine_stats 行业 → mhz_wemedia.industry 标签
    _INDUSTRY_MAP = {
        '科技数码': '科技', '教育培训': '教育', '医疗健康': '健康', '房地产': '家居',
        '装修建材': '家居', '汽车': '汽车', '母婴亲子': '母婴', '时尚美妆': '时尚',
        '旅游酒店': '旅游', '文娱游戏': '游戏', '电商零售': '财经', '企业服务': '科技',
        'GEO': '科技',
    }

    wm_industry = _INDUSTRY_MAP.get(matched_industry, '')
    # fallback 1：用品牌原始 industry 文本匹配 _INDUSTRY_MAP 的 key
    search_text = raw_industry or matched_industry or ''
    if not wm_industry and search_text:
        for geo_ind, wm_ind in _INDUSTRY_MAP.items():
            if geo_ind in search_text or search_text in geo_ind:
                wm_industry = wm_ind
                break
    # fallback 2：用品牌原始 industry 直接匹配自媒体的行业标签
    if not wm_industry and search_text:
        _WM_INDUSTRIES = ['科技', '教育', '健康', '家居', '汽车', '母婴', '时尚', '旅游', '游戏', '财经', '新闻', '生活']
        for wi in _WM_INDUSTRIES:
            if wi in search_text:
                wm_industry = wi
                break
    # fallback 3：如果品牌行业含"设备/制造/工业/技术/软件/信息/数字/智能"，归为科技
    if not wm_industry and search_text:
        _TECH_KEYWORDS = ['设备', '制造', '工业', '技术', '软件', '信息', '数字', '智能', '电子', '芯片', '半导体', '互联网']
        if any(kw in search_text for kw in _TECH_KEYWORDS):
            wm_industry = '科技'

    conn = get_connection()
    try:
        cur = conn.cursor()

        def _build_rec(r: dict) -> dict:
            fans = r['fans_num'] or 0
            fans_str = f"{fans / 10000:.0f}亿" if fans >= 10000 else f"{fans}万" if fans >= 1 else '未知'
            # [WP12 P0-1] 自媒体同样标注真实被引强度(表不可用 → 空 dict → 字段为默认值)
            citation = _citation_descriptor_for_row(citation_table, r, r.get('platform') or '')
            reason = f"{r['platform']} · 粉丝 {fans_str}" + (
                f" · {r.get('industry') or wm_industry or ''}行业"
                if (r.get('industry') or wm_industry) else ''
            )
            if citation.get('source') == 'observed' and int(citation.get('citation_count') or 0) > 0:
                reason += f" · AI {citation.get('strength_label')}(被引 {citation.get('citation_count')} 次)"
            elif citation.get('family_label'):
                reason += f" · {citation.get('family_label')}域族"
            return {
                "media_id": r['id'], "media_name": r['media_name'],
                "platform": r['platform'] or '', "category": r['industry'] or '',
                "price": float(r['price'] or 0),
                "our_price_points": r['our_price_points'] or 0,
                "our_price_yuan": float(r['our_price_yuan'] or 0),
                "inclusion_rate": '', "engines": [], "geo_score": 0, "citation_rate": 0,
                "ai_citation_strength": round(float(citation.get('strength') or 0), 3),
                "ai_citation_label": citation.get('strength_label') or '',
                "ai_citation_count": int(citation.get('citation_count') or 0),
                "ai_citation_source": citation.get('source') or '',
                "ai_citation_domain": citation.get('domain') or '',
                "ai_citation_family": citation.get('family_label') or '',
                "reason": reason,
                "media_type": "wemedia", "fans_num": fans,
            }

        vertical = []
        generic = []

        # 行业精选：该行业自媒体，按性价比排序（价格/粉丝），每平台最多 2 个
        if wm_industry:
            cur.execute("""
                SELECT id, toutiao_name as media_name, platform, industry, fans_num, price,
                       our_price_yuan, our_price_points
                FROM mhz_wemedia
                WHERE is_active = true AND industry = %s AND price > 0 AND fans_num > 0
                ORDER BY (price / GREATEST(fans_num, 1)) ASC, fans_num DESC, id ASC
                LIMIT %s
            """, (wm_industry, limit * 4))
            plat_count: dict = {}
            for row in cur.fetchall():
                if len(vertical) >= limit:
                    break
                r = dict(row)
                plat = r['platform'] or ''
                if plat_count.get(plat, 0) >= 2:
                    continue
                plat_count[plat] = plat_count.get(plat, 0) + 1
                vertical.append(_build_rec(r))

        # 通用补充：优先同行业扩大范围，排除明显不相关的行业
        used_ids = {r['media_id'] for r in vertical}
        remaining = limit - len(vertical)
        if remaining > 0:
            # 如果有行业，通用补充也限制同行业或"新闻"（新闻是万金油）
            if wm_industry:
                cur.execute("""
                    SELECT id, toutiao_name as media_name, platform, industry, fans_num, price,
                           our_price_yuan, our_price_points
                    FROM mhz_wemedia
                    WHERE is_active = true AND price > 0 AND fans_num > 0
                          AND (industry = %s OR industry = '新闻' OR industry = '生活')
                    ORDER BY (price / GREATEST(fans_num, 1)) ASC, fans_num DESC, id ASC
                    LIMIT %s
                """, (wm_industry, remaining * 5))
            else:
                # 无行业时用"新闻"+"科技"+"生活"通用分类
                cur.execute("""
                    SELECT id, toutiao_name as media_name, platform, industry, fans_num, price,
                           our_price_yuan, our_price_points
                    FROM mhz_wemedia
                    WHERE is_active = true AND price > 0 AND fans_num > 0
                          AND industry IN ('新闻', '科技', '生活', '财经')
                    ORDER BY (price / GREATEST(fans_num, 1)) ASC, fans_num DESC, id ASC
                    LIMIT %s
                """, (remaining * 5,))
            for row in cur.fetchall():
                if len(generic) >= remaining:
                    break
                r = dict(row)
                if r['id'] not in used_ids:
                    used_ids.add(r['id'])
                    generic.append(_build_rec(r))

        return {"vertical": vertical, "generic": generic, "matched_industry": matched_industry or ''}
    finally:
        conn.close()


# ==================== 深度推荐（LLM 文章分析 → 重排行业候选池） ====================

# 文章类型 → 媒体特征偏好（用于对候选池重排）
_ARTICLE_TYPE_WEIGHTS = {
    'ranking': {
        # 排行榜/测评 → 门户权威站权重高
        'prefer_keywords': ['网', '新闻', '财经', '科技', '头条'],
        'boost_generic': True, 'boost_vertical': False,
    },
    'faq': {
        # 问答/攻略 → UGC 社区权重高
        'prefer_keywords': ['知乎', '什么值得买', '贴吧', '社区'],
        'boost_generic': False, 'boost_vertical': True,
    },
    'tutorial': {
        # 教程/指南 → 垂直行业站
        'prefer_keywords': ['装修', '家居', '齐家', '土巴兔', '住小帮', '房天下', 'CSDN'],
        'boost_generic': False, 'boost_vertical': True,
    },
    'news': {
        # 行业新闻 → 新闻源 + 门户
        'prefer_keywords': ['新闻', '日报', '网', '头条', '财经', '澎湃'],
        'boost_generic': True, 'boost_vertical': False,
    },
    'case_study': {
        # 案例分析 → 垂直行业站
        'prefer_keywords': ['房天下', '齐家', '家居', '装修', '汽车之家', '懂车帝'],
        'boost_generic': False, 'boost_vertical': True,
    },
    'review': {
        # 产品测评 → UGC + 垂直
        'prefer_keywords': ['什么值得买', '知乎', '测评', '太平洋'],
        'boost_generic': False, 'boost_vertical': True,
    },
}


def recommend_deep(industry: str, article_type: str, limit: int = 8, user_id: int = 0,
                   industry_brand: Optional[dict] = None) -> dict:
    """
    深度推荐 — 从行业全量候选池（263+ 平台）里根据文章类型重排后取 top。
    和默认推荐的区别：同一个行业，但排序权重不同 → 不同文章推出不同媒体。

    [v2-F 2026-05-18] 接 user_id 90 天去重 + 行业头部白名单兜底 + 多维打分 + ¥5 硬过滤。
    """
    from db.connection import get_connection

    svc = get_placement_service()
    industry_scores, matched_industry = svc._match_research_industry(industry, brand=industry_brand)
    research_status = _research_status_safe(svc, industry, industry_brand)
    if not industry_scores:
        industry_scores = PLATFORM_ENGINE_COVERAGE

    all_platforms = sorted(industry_scores.items(), key=lambda x: x[1]['score'], reverse=True)

    # [v2-F] 行业头部白名单兜底
    head_names = _resolve_industry_head_fallback(industry, matched_industry)
    if head_names:
        existing = {n for n, _ in all_platforms}
        for hn in head_names:
            if hn not in existing:
                all_platforms.append((hn, {'score': 20, 'engines': [], 'citation_rate': 0}))

    # [v2-F] 90 天历史去重
    exclude_ids: set = set()
    if user_id:
        try:
            from db.meijiehezi_db import get_user_published_media_ids
            exclude_ids = get_user_published_media_ids(user_id, days=90)
        except Exception as e:
            logger.warning(f"[v2-F] recommend_deep 90 天去重拉取失败 user={user_id}: {e}")

    # [2026-07-28 C-3 顺手修 · 生产 live bug] `media_balance_enabled` 在本函数里
    #   **从来没有被定义过**:不是参数、不是局部变量、模块也没有这个全局。
    #   下面 `_calc_score` 里的 `_is_trunk_platform(..., media_balance_enabled=media_balance_enabled)`
    #   编译成全局查找(co_names 含该名) → 每次调用 `recommend_deep()` 必抛 NameError。
    #   引入者就是灰度开关那一提交(`c3fdc8a3`),它给别处都接了线,唯独漏了这里;
    #   而 `recommend_deep` 是 `api/publish_api.py:747` 深度推荐端点的实现。
    #   现在按本函数手头有的维度(user_id + 行业)正常取灰度态。
    from services.media_balance_gate import is_media_balance_enabled
    media_balance_enabled = is_media_balance_enabled(
        user_id=user_id or None,
        industry_keys=[industry, matched_industry],
    )
    # [WO_274 · 2026-09-23 · 生产 live bug] 下面两个排序 lambda 用 `success_table`,本函数却从没定义它
    #   (v2 有,deep 漏了)⇒ 候选非空一排序就 NameError ⇒ deep_analyze_for_publish 外层 0 层 try ⇒ 500。
    #   与 v2 同一写法:灰度关时给空表 ⇒ success_factor 恒 None ⇒ 评分逐位回到上线前。
    success_table = _load_media_success_table() if media_balance_enabled else {}

    # 根据文章类型获取偏好
    type_pref = _ARTICLE_TYPE_WEIGHTS.get(article_type, {})
    prefer_kws = type_pref.get('prefer_keywords', [])
    boost_generic = type_pref.get('boost_generic', False)
    boost_vertical = type_pref.get('boost_vertical', False)

    # 重算每个平台得分 = 原始引用分 × 文章类型加成
    def _calc_score(name: str, data: dict) -> float:
        base = data.get('score', 0)
        bonus = 0
        for kw in prefer_kws:
            if kw in name:
                bonus += 5
                break
        is_generic = _is_trunk_platform(name, media_balance_enabled=media_balance_enabled)
        if boost_generic and is_generic:
            bonus += 3
        if boost_vertical and not is_generic:
            bonus += 3
        return base + bonus

    scored = [(n, d, _calc_score(n, d)) for n, d in all_platforms]
    scored.sort(key=lambda x: x[2], reverse=True)

    # 交叉匹配软文媒体
    RESEARCH_TO_MHZ = {
        '知乎': ['知乎'], '搜狐': ['搜狐', 'sohu'], '百家号': ['百家号', '百家'],
        '今日头条': ['今日头条', '头条'], '抖音': ['抖音'], '微博': ['微博'],
        '网易': ['网易'], '凤凰': ['凤凰'], '腾讯': ['腾讯'], '新浪': ['新浪'],
        '澎湃': ['澎湃'], '人民网': ['人民网'], '央广网': ['央广'],
        '和讯': ['和讯'], '东方财富': ['东方财富'], '36氪': ['36氪', '36kr'],
        '汽车之家': ['汽车之家'], '懂车帝': ['懂车帝'], '房天下': ['房天下'],
        '携程': ['携程'], 'CSDN': ['CSDN'], '住小帮': ['住小帮'],
        '齐家网': ['齐家'], '太平洋家居网': ['太平洋家居'], '土巴兔': ['土巴兔'],
        '什么值得买': ['什么值得买'], '吉屋': ['吉屋'],
    }

    conn = get_connection()
    try:
        cur = conn.cursor()
        matched_media = []
        # [v2-F] used_ids 起点 = 90 天历史去重(exclude_ids)
        used_ids: set = set(exclude_ids)

        for name, data, score in scored:
            if len(matched_media) >= limit:
                break
            kws = RESEARCH_TO_MHZ.get(name, [name])
            or_clauses = " OR ".join(["media_name ILIKE %s"] * len(kws))
            params = [f"%{kw}%" for kw in kws]
            # [v2-F] 默认 price >= 5(<¥5 区 100% 冒名小站)+ 拉多维打分字段
            # [BUG 5 修 2026-05-18] SQL 粗排防 LIMIT 截好货
            cur.execute(f"""
                SELECT id, media_name, price, our_price_yuan, our_price_points,
                       resource_type_name, inclusion_rate,
                       authority_media, geo_rank, geo_rank_platform, portal_media, entrance_link
                FROM mhz_media
                WHERE is_active = true AND price >= 5.0
                      AND media_name NOT ILIKE '%%套餐%%' AND resource_type_name != '套餐系列'
                      AND ({or_clauses})
                ORDER BY {_V2F_SQL_PREORDER} DESC, price ASC
                LIMIT 8
            """, params)
            # [v2-F] 候选先过 90 天去重 · 再多维加权排序选 top 1
            candidates = []
            for row in cur.fetchall():
                d = dict(row)
                if d['id'] in used_ids:
                    continue
                candidates.append(d)
            if not candidates:
                continue
            candidates.sort(
                key=lambda r: _quality_score_v2f(
                    r,
                    success_factor=_success_factor_for(success_table, r.get('media_name') or ''),
                ),
                reverse=True,
            )
            d = candidates[0]
            used_ids.add(d['id'])
            engines = data.get('engines', [])
            is_vertical = not _is_trunk_platform(name, media_balance_enabled=media_balance_enabled)
            engine_count = _count_geo_engines(d.get('geo_rank_platform') or '')
            portal = (d.get('portal_media') or '').strip()
            reason_bits = [name]
            if engines:
                reason_bits.append(f"{'+'.join(engines[:3])} 引擎引用")
            if is_vertical:
                reason_bits.append("行业垂直")
            if engine_count >= 4:
                reason_bits.append(f"GEO {engine_count}/6 覆盖")
            if portal in _TOP_PORTAL_MEDIAS:
                reason_bits.append(f"{portal}一线")
            price_val = float(d.get('price') or 0)
            if 30 <= price_val <= 60:
                reason_bits.append("¥30-60 性价比甜点")
            matched_media.append({
                "media_id": d['id'], "media_name": d['media_name'],
                "platform": name,
                "category": d.get('resource_type_name') or '',
                "price": price_val,
                "our_price_points": d.get('our_price_points') or 0,
                "our_price_yuan": float(d.get('our_price_yuan') or 0),
                "authority_media": int(d.get('authority_media') or 0),
                "geo_rank": int(d.get('geo_rank') or 0),
                "geo_rank_platform": d.get('geo_rank_platform') or '',
                "geo_engine_coverage": engine_count,
                "portal_media": portal,
                "entrance_link": d.get('entrance_link') or '',
                "engines": engines, "geo_score": score,
                "quality_score_v2f": round(_quality_score_v2f(
                    d, success_factor=_success_factor_for(success_table, d.get('media_name') or ''),
                ), 3),
                "reason": " · ".join(reason_bits),
                "media_type": "media",
            })

        # 自媒体沿用 V2（自媒体没有文章类型维度）
        # [C-3] 这里原先不传灰度态 → 吃 `_match_wemedia` 的 default=True → 灰度关闭时
        #   自媒体分组仍走新口径(fail-open)。默认值已改 False,这里再显式传,双保险。
        wm = _match_wemedia([], [], matched_industry, limit, raw_industry=industry,
                            media_balance_enabled=media_balance_enabled)
        matched_wemedia = [{**m, 'media_type': 'wemedia'} for m in (wm.get('vertical', []) + wm.get('generic', []))[:limit]]

        out = {"matched_industry": matched_industry or '', "media": matched_media, "wemedia": matched_wemedia,
               "research_status": research_status}
        return out
    finally:
        conn.close()
