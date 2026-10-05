"""
行业知识公共库采集器 — 三层知识体系的 L1(行业) + L2(品类) 采集

用法:
    from tools.industry_knowledge_collector import (
        collect_industry_knowledge,  # L1 行业层
        collect_category_knowledge,  # L2 品类层
        get_industry_context,        # 读取三层知识合并
        deep_analyze_user,           # L3 用户层深度解析
    )
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("IndustryKB")

L1_TTL_DAYS = 90   # 行业层缓存90天
L2_TTL_DAYS = 30   # 品类层缓存30天

# 单点超时（防 LLM/搜索 hang 致整个深度解析永远卡死）
# 值合理性：qwen3-max 普通响应 5-15s，慢响应 30-50s，60s 留余量
# metaso 搜索通常 3-10s，30s 已是极端阈值
LLM_TIMEOUT = 60
SEARCH_TIMEOUT = 30


async def _advisor_generate_safe(prompt: str) -> str:
    """advisor_generate 的超时安全包装，超时即抛 TimeoutError 让上层 except 走退款分支"""
    from services.llm.advisor_llm import advisor_generate
    return await asyncio.wait_for(advisor_generate(prompt), timeout=LLM_TIMEOUT)


async def _metaso_search_safe(query: str, size: int = 8):
    """metaso_web_search 的超时安全包装"""
    from tools.search.metaso_mcp import metaso_web_search
    from tools.search.provider_router import search_scenario

    # [Provider 层 2026-07-28] 调研抓取是灰度第一批场景键:默认(无 env)仍全 metaso,
    # 行为零变化;开 SEARCH_PROVIDER_RESEARCH=doubao 后由入口内部路由,本函数签名不变。
    with search_scenario("research"):
        return await asyncio.wait_for(metaso_web_search(query, size=size), timeout=SEARCH_TIMEOUT)


def _get_conn():
    from db.connection import get_connection
    return get_connection()


# ========== 读取 ==========

def get_industry_knowledge(industry: str, category: str = None, level: str = None) -> Optional[dict]:
    """从公共库读取行业/品类知识，过期返回 None"""
    if not industry:
        return None
    conn = _get_conn()
    try:
        cur = conn.cursor()
        if category and level != "industry":
            cur.execute(
                "SELECT knowledge, expires_at FROM industry_knowledge WHERE level='category' AND industry=%s AND category=%s",
                (industry, category)
            )
        else:
            cur.execute(
                "SELECT knowledge, expires_at FROM industry_knowledge WHERE level='industry' AND industry=%s AND category IS NULL",
                (industry,)
            )
        row = cur.fetchone()
        if not row:
            return None
        # 检查过期
        expires = row.get("expires_at")
        if expires and isinstance(expires, datetime) and expires < datetime.now():
            return None  # 过期
        knowledge = row["knowledge"]
        if isinstance(knowledge, str):
            knowledge = json.loads(knowledge)
        # 计数+1
        if category:
            cur.execute("UPDATE industry_knowledge SET search_count = search_count + 1 WHERE level='category' AND industry=%s AND category=%s", (industry, category))
        else:
            cur.execute("UPDATE industry_knowledge SET search_count = search_count + 1 WHERE level='industry' AND industry=%s AND category IS NULL", (industry,))
        conn.commit()
        return knowledge
    except Exception as e:
        logger.warning(f"读取行业知识失败: {e}")
        return None
    finally:
        conn.close()


def _save_knowledge(level: str, industry: str, category: str, knowledge: dict, ttl_days: int):
    """保存到公共库"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        expires = datetime.now() + timedelta(days=ttl_days)
        cur.execute("""
            INSERT INTO industry_knowledge (level, industry, category, knowledge, expires_at, generated_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (level, industry, category) DO UPDATE SET
                knowledge = EXCLUDED.knowledge,
                expires_at = EXCLUDED.expires_at,
                generated_at = NOW(),
                version = industry_knowledge.version + 1
        """, (level, industry, category or None, json.dumps(knowledge, ensure_ascii=False), expires))
        conn.commit()
        logger.info(f"保存行业知识: {level}/{industry}/{category or '-'}")
    except Exception as e:
        conn.rollback()
        logger.error(f"保存行业知识失败: {e}")
    finally:
        conn.close()


# ========== L1 行业层采集 ==========

async def collect_industry_knowledge(industry: str) -> dict:
    """采集 L1 行业层知识（v3.7 道法术器分层 · 按 TTL 选择性采集 + 叠加合并）

    v3.7 行为:
    - 道层(market_overview/policy_redlines/industry_terms/common_categories): 永不过期,仅缺失才采
    - 法层(top_brands/jargon/counter_consensus): 120 天 TTL,过期重采 + 叠加去重
    - 术层(authority_sources): 45 天 TTL,过期重采 + 叠加去重
    - 旧版本归档到 _v37_meta.archived_history (最多 5 个)
    """
    from tools.knowledge_layers import (
        needs_refresh, merge_with_layers, stamp_layers_refreshed,
    )

    logger.info(f"[L1] 开始采集: {industry}")
    existing = get_industry_knowledge(industry, level="industry") or {}

    # 判分层
    needs_dao = needs_refresh(existing, "dao")
    needs_fa = needs_refresh(existing, "fa")
    needs_shu = needs_refresh(existing, "shu")

    if not (needs_dao or needs_fa or needs_shu):
        logger.info(f"[L1 v3.7] {industry} 三层全部命中缓存,跳过采集")
        return existing

    logger.info(f"[L1 v3.7] {industry} 需采集 dao={needs_dao} fa={needs_fa} shu={needs_shu}")
    new_data = {}

    # === 道层 (LLM 推理通识) ===
    if needs_dao:
        l1_prompt = f"""你是行业分析专家。分析「{industry}」行业,输出JSON:
{{
  "market_overview": "行业概述(50字)",
  "common_categories": ["3-8个细分品类名称"],
  "policy_redlines": ["政策法规红线"],
  "content_redlines": ["短视频内容创作红线"],
  "industry_terms": ["8-15个行业常用术语"]
}}
只输出JSON。"""
        try:
            raw = await _advisor_generate_safe(l1_prompt)
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text
            if text.endswith("```"):
                text = text.rsplit("```", 1)[0]
            dao_data = json.loads(text.strip())
            new_data.update(dao_data)
            logger.info(f"[L1 v3.7 道] 采集 {list(dao_data.keys())}")
        except Exception as e:
            logger.warning(f"[L1 v3.7 道] LLM 失败: {e}")
            try:
                from json_repair import repair_json
                dao_data = json.loads(repair_json(text.strip()))
                new_data.update(dao_data)
            except Exception:
                if not existing.get("market_overview"):
                    new_data["market_overview"] = f"{industry}行业"
                    new_data["common_categories"] = []

    # === 法层基础 (Metaso 搜索 + LLM 提炼 top_brands/market_trend) ===
    if needs_fa:
        try:
            result = await _metaso_search_safe(f"{industry} 行业市场格局 头部品牌 2025 2026", size=8)
            search_text = ""
            if not isinstance(result, Exception) and hasattr(result, "content") and result.content:
                for item in result.content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        search_text = item.get("text", "")
                        break
            if search_text:
                summary_prompt = f"""根据搜索结果,提取「{industry}」行业的头部品牌和市场趋势。输出JSON:
{{"top_brands": ["品牌名(定位)"], "market_trend": "趋势一句话"}}
搜索结果:{search_text[:2000]}
只输出JSON。"""
                raw2 = await _advisor_generate_safe(summary_prompt)
                text2 = raw2.strip()
                if text2.startswith("```"):
                    text2 = text2.split("\n", 1)[1]
                if text2.endswith("```"):
                    text2 = text2.rsplit("```", 1)[0]
                extra = json.loads(text2.strip())
                new_data.update(extra)
                logger.info(f"[L1 v3.7 法-基础] 采集 top_brands/market_trend")
        except Exception as e:
            logger.warning(f"[L1 v3.7 法-基础] 失败: {e}")

    # === 法层 + 术层 v3.6 raw 素材池 (3 AI 投票) ===
    fa_v36_tasks = []
    shu_v36_tasks = []
    if needs_fa:
        fa_v36_tasks = [_collect_l1_jargon(industry), _collect_l1_counter(industry)]
    if needs_shu:
        shu_v36_tasks = [_collect_l1_authority(industry)]

    v36_results = []
    if fa_v36_tasks or shu_v36_tasks:
        try:
            v36_results = await asyncio.gather(*(fa_v36_tasks + shu_v36_tasks), return_exceptions=True)
        except Exception as e:
            logger.warning(f"[L1 v3.7 法/术 v3.6] 失败(不阻塞): {e}")

    idx = 0
    if needs_fa:
        jargon = v36_results[idx] if idx < len(v36_results) else None
        if isinstance(jargon, list) and jargon:
            new_data["industry_jargon"] = jargon
            logger.info(f"[L1 v3.7 法] industry_jargon: {len(jargon)} 条")
        idx += 1
        counter = v36_results[idx] if idx < len(v36_results) else None
        if isinstance(counter, list) and counter:
            new_data["counter_consensus"] = counter
            logger.info(f"[L1 v3.7 法] counter_consensus: {len(counter)} 条")
        idx += 1
    if needs_shu:
        authority = v36_results[idx] if idx < len(v36_results) else None
        if isinstance(authority, list) and authority:
            new_data["authority_sources"] = authority
            logger.info(f"[L1 v3.7 术] authority_sources: {len(authority)} 条")
        idx += 1

    # === v3.7 智能合并 + 归档旧版 + 标记刷新 ===
    final = merge_with_layers(existing, new_data, archive_old=bool(existing))
    refreshed_layers = []
    if needs_dao:
        refreshed_layers.append("dao")
    if needs_fa:
        refreshed_layers.append("fa")
    if needs_shu:
        refreshed_layers.append("shu")
    stamp_layers_refreshed(final, refreshed_layers)

    _save_knowledge("industry", industry, None, final, L1_TTL_DAYS)
    logger.info(f"[L1 v3.7] 完成: {industry} · 刷新 {refreshed_layers} · 品类={final.get('common_categories', [])}")
    return final


# ========== L2 品类层采集 ==========

async def collect_category_knowledge(industry: str, category: str) -> dict:
    """采集 L2 品类层知识（v3.7 道法术器分层 · L2 无道层 + 法/术按 TTL + 器层永远叠加）

    v3.7 行为:
    - 法层(typical_products/target_audience/differentiation_angles/price_range): 120 天 TTL
    - 术层(conversion_paths/hot_formats/content_types/cta_templates/platform_tips): 45 天 TTL
    - 器层(user_voices_pool/case_evidence_pool): 老板拍板"持续叠加",每次 deep_analyze 触发都采
      去重按 source_url(避免同一网页重复采),实际新素材增量入池
    """
    from tools.knowledge_layers import (
        needs_refresh, merge_with_layers, stamp_layers_refreshed,
    )

    logger.info(f"[L2] 开始采集: {industry}·{category}")
    existing = get_industry_knowledge(industry, category) or {}

    needs_fa = needs_refresh(existing, "fa")
    needs_shu = needs_refresh(existing, "shu")
    # 器层永远采(老板设计:每次有人付费触发就给行业叠加新素材)
    always_collect_qi = True

    new_data = {}

    # === 法+术 (一次 LLM prompt 出全部字段, 任一层需刷新就跑) ===
    if needs_fa or needs_shu:
        l2_prompt = f"""你是{industry}行业的{category}细分领域专家。分析这个品类,输出JSON:
{{
  "typical_products": ["典型产品/服务 4-6个"],
  "price_range": "人均消费范围",
  "target_audience": {{
    "primary": "核心客群画像(年龄/职业/场景)",
    "secondary": "次要客群",
    "decision_factors": ["4-6个决策因素"]
  }},
  "conversion_paths": ["3-4个获客转化路径"],
  "content_types": ["4-6个适合的短视频内容类型"],
  "differentiation_angles": ["3-5个差异化切入角度"],
  "cta_templates": ["2-3个评论区/结尾行动引导模板"]
}}
只输出JSON。"""
        text = ""
        try:
            raw = await _advisor_generate_safe(l2_prompt)
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1]
            if text.endswith("```"):
                text = text.rsplit("```", 1)[0]
            base_data = json.loads(text.strip())
            new_data.update(base_data)
            logger.info(f"[L2 v3.7 法+术] 采集 {list(base_data.keys())}")
        except Exception as e:
            logger.warning(f"[L2 v3.7 法+术] LLM 失败: {e}")
            try:
                from json_repair import repair_json
                base_data = json.loads(repair_json(text.strip())) if text else {}
                new_data.update(base_data)
            except Exception:
                pass

        # 术层补充: hot_formats / platform_tips (Metaso + 二次 LLM)
        if needs_shu:
            try:
                result = await _metaso_search_safe(f"{category} 获客 引流 短视频 运营策略", size=8)
                search_text = ""
                if not isinstance(result, Exception) and hasattr(result, "content") and result.content:
                    for item in result.content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            search_text = item.get("text", "")
                            break
                if search_text:
                    hot_prompt = f"""根据搜索结果,总结「{category}」的热门内容形式和获客打法。输出JSON:
{{"hot_formats": ["当前爆款内容形式"], "platform_tips": "平台运营建议"}}
搜索结果:{search_text[:2000]}
只输出JSON。"""
                    raw2 = await _advisor_generate_safe(hot_prompt)
                    text2 = raw2.strip()
                    if text2.startswith("```"):
                        text2 = text2.split("\n", 1)[1]
                    if text2.endswith("```"):
                        text2 = text2.rsplit("```", 1)[0]
                    extra = json.loads(text2.strip())
                    new_data.update(extra)
                    logger.info(f"[L2 v3.7 术-补充] 采集 hot_formats/platform_tips")
            except Exception as e:
                logger.warning(f"[L2 v3.7 术-补充] 失败: {e}")

    # === 器层 v3.6 raw 素材池 (永远叠加 · 老板拍板) ===
    if always_collect_qi:
        try:
            voices, cases = await asyncio.gather(
                _collect_l2_user_voices(industry, category),
                _collect_l2_case_evidence(industry, category),
                return_exceptions=True,
            )
            if isinstance(voices, list) and voices:
                new_data["user_voices_pool"] = voices
                logger.info(f"[L2 v3.7 器] user_voices_pool: {len(voices)} 条 (待去重叠加)")
            if isinstance(cases, list) and cases:
                new_data["case_evidence_pool"] = cases
                logger.info(f"[L2 v3.7 器] case_evidence_pool: {len(cases)} 条 (待去重叠加)")
        except Exception as e:
            logger.warning(f"[L2 v3.7 器] 失败(不阻塞): {e}")

    if not new_data and existing:
        # 全部命中 + 没器层数据时(理论上 always_collect_qi=True 不会到这里)
        logger.info(f"[L2 v3.7] {industry}·{category} 全部命中,跳过")
        return existing

    # === v3.7 智能合并 + 归档旧版 + 标记刷新 ===
    final = merge_with_layers(existing, new_data, archive_old=bool(existing))
    refreshed_layers = []
    if needs_fa:
        refreshed_layers.append("fa")
    if needs_shu:
        refreshed_layers.append("shu")
    stamp_layers_refreshed(final, refreshed_layers)

    # 器层总叠加但不主动 refresh meta(避免误判 qi 过期)
    _save_knowledge("category", industry, category, final, L2_TTL_DAYS)
    logger.info(f"[L2 v3.7] 完成: {industry}·{category} · 刷新 {refreshed_layers} · 器层叠加")
    return final


# ========== L3 用户层深度解析 ==========

async def deep_analyze_user(profile: dict) -> dict:
    """L3 深度解析(500 积分,v3.6 CTO-15.2 升级)

    v3.6 架构改动(2026-04-19):
      - 调价 260 → 500(commit 1, db/wallet_db.py)
      - 改走 deep_analyze_user_v36 (L1+L2 公共库 + 多 AI 投票 + L3 用户特化)
      - 老 8 维搜索 + 综合推理保留为 fallback(v3.6 失败时降级)

    成本:
      - L1+L2 缓存命中(同品类第 N 用户): ¥0.3
      - L1 命中 L2 miss(同行业新品类): ¥1.8
      - L1+L2 全 miss(全新行业): ¥3.85

    历史 v3.8 CTO-15.0 老逻辑保留:
      - 搜索关键词 8 维(本函数下方)
      - 综合推理 9 字段(本函数下方)
      - 现作为 v3.6 失败时的 fallback
    """
    # v3.6 主路径
    try:
        return await deep_analyze_user_v36(profile)
    except Exception as e:
        logger.warning(f"[L3] v3.6 失败,降级老 8 维搜索逻辑: {e}")
    # === v3.6 失败 fallback: 老 v3.8 8 维搜索逻辑(保留供降级) ===
    industry = profile.get("industry", "")
    category = profile.get("category", "")
    city = profile.get("city", "") or ""
    products = profile.get("products", "")
    business = profile.get("business", "")

    logger.info(f"[L3] 深度解析: {city}·{industry}·{category}")

    # 读 L1+L2 作为基础
    l1 = get_industry_knowledge(industry, level="industry") or {}
    l2 = get_industry_knowledge(industry, category) or {}

    brief = {"city": city, "source": "deep_analysis", "generated_at": datetime.now().isoformat()}

    # 多维度本地知识搜索（v3.8 扩 3 → 8 关键词）
    if city:
        local_cat = category or industry

        async def _search_extract(query):
            try:
                result = await _metaso_search_safe(query, size=8)
                st = ""
                if hasattr(result, 'content') and result.content:
                    for item in result.content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            st = item.get("text", "")
                            break
                return st
            except Exception:
                return ""

        # v3.8: 8 维搜索（覆盖竞品/选址/本地趋势/AI 搜索/爆款形态/用户痛点/案例/定价）
        search_tasks = {
            "competitors": f"{city} {local_cat} 竞品 热门店铺 排名 2026",
            "district": f"{city} {local_cat} 商圈 选址 人流量",
            "local_trends": f"{city} {local_cat} 抖音 本地生活 团购 探店 2026",
            "ai_search_geo": f"{local_cat} GEO 生成式引擎优化 AI 搜索 推荐 DeepSeek Kimi 豆包",
            "hot_formats": f"{local_cat} 爆款内容 短视频 选题 模板 2026",
            "pain_points": f"{local_cat} 客户 抱怨 痛点 差评 问题 需求",
            "cases": f"{local_cat} 成功案例 品牌 增长 案例拆解 方法论",
            "pricing_policy": f"{city} {local_cat} 价格 行情 政策 合规 2026",
        }

        tasks = [_search_extract(q) for q in search_tasks.values()]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)
        search_results = {}
        for (key, _), result in zip(search_tasks.items(), results_list):
            search_results[key] = result if isinstance(result, str) else ""

        # 合并搜索结果让 LLM 分析（v3.8 每段更长 1200 字，总量上限 8000 字覆盖 8 维）
        combined_search = ""
        for key, text in search_results.items():
            if text:
                combined_search += f"\n【{key}】{text[:1200]}\n"

        if combined_search:
            try:
                # v3.8 本地分析扩字段（加 regional_kw_ideas/top_cases/content_pain_points）
                local_prompt = f"""你是 GEO 行业分析师。根据下方搜索结果（含竞品/商圈/爆款/GEO/痛点/案例/定价），深度分析「{city}·{local_cat}」本地市场，产出可直接写文章/拍视频的素材。

输出 JSON（每项尽量详细，不要只一句话）：
{{
  "city_context": "{city}本地市场概况（~100字：市场规模/玩家集中度/消费特点/政策背景）",
  "local_competitors": ["本地竞品1(名称·特点·打法·知名原因)", "..."5-8个],
  "local_audience": "本地客群特征和消费习惯（~80字，含典型画像/决策链路/购买场景）",
  "local_platform_tips": "本地平台运营建议（分平台说：抖音本地生活 / 大众点评 / 小红书各该怎么做，~120字）",
  "regional_kw_ideas": ["{city}+{local_cat}相关的地域关键词 8-12 个（长尾词、用户真实搜索用语）"],
  "top_cases": ["本地/同行业典型案例 3-5 个，每条含：公司名 + 做了什么 + 结果/数据"],
  "content_pain_points": ["目标客户最常抱怨/犹豫/搜索的痛点问题 5-8 个（以用户原话/搜索词形式，不要概括）"],
  "top_content_formats": ["当前爆款的内容形态 4-6 个（含平台+形式+示例标题）"],
  "acquisition_paths": ["本地获客路径 3-5 条（每条含：渠道/关键动作/转化环节/预期成本区间）"]
}}

用户信息：城市={city}，品类={local_cat}，产品={products or '未提供'}，业务={business or '未提供'}

搜索结果：
{combined_search[:8000]}

只输出 JSON，每项字数要撑起"260 积分的价值"，不要敷衍。"""
                raw = await _advisor_generate_safe(local_prompt)
                text = raw.strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[1]
                if text.endswith("```"):
                    text = text.rsplit("```", 1)[0]
                local = json.loads(text.strip())
                brief.update(local)
            except Exception as e:
                logger.warning(f"[L3] 本地分析失败: {e}")
                try:
                    from json_repair import repair_json
                    local = json.loads(repair_json(text.strip()))
                    brief.update(local)
                except Exception:
                    pass

    # LLM 综合推理（v3.8 从 4 字段扩到 9 字段 · 对齐前端 13 字段审核工作流）
    try:
        synth_prompt = f"""你是{industry}行业的资深运营顾问 + GEO（生成式引擎优化）策略专家。用户已经提供了品牌/城市/产品信息，请结合行业知识库（下方）产出**可直接落地**的个性化建议。

特别注意：老板花了 260 积分，每条建议要**具体到行动**（例如"拍XX类型短视频每周3条，标题套模板'{city}第一家...'"），不要写"做好内容"这种废话。

输出 JSON（每项尽量详细，字段不可省略）：
{{
  "my_audience": "推荐的精准目标客群画像（~80字：人群特征 + 消费能力 + 决策动机）",
  "target_users": "用户画像（年龄/地域/职业/收入/家庭结构/典型场景）",
  "my_differentiation": "推荐的差异化定位（~80字：对手做什么 你做什么 为什么用户选你）",
  "differentiation_hints": ["可直接用于内容的差异化素材 5-8 条（具体卖点/数据/独家优势）"],
  "content_strategy": "多轨内容策略：(1) 短视频方向——发什么类型的视频(平台+形式+周频)；(2) GEO 优化——如何让 DeepSeek/Kimi/豆包 AI 搜索推荐你(文章结构/引用链/关键词嵌入/权威背书)；(3) SEO 传统搜索——哪些长尾词优先布局。分段详述~200字",
  "my_conversion": "推荐的获客转化路径（具体到 N 步漏斗 + 每步关键动作）"
}}

用户信息：
- 行业：{industry}，品类：{category}，城市：{city}
- 产品：{products or '未提供'}
- 业务：{business or '未提供'}

行业知识库（L1+L2）：
- 价格带：{l2.get('price_range', l1.get('price_range', '未知'))}
- 核心客群：{l2.get('target_audience', {}).get('primary', l1.get('target_audience', {}).get('primary', '未知'))}
- 获客路径：{', '.join(l2.get('conversion_paths', l1.get('conversion_paths', []))[:3])}
- 差异化角度：{', '.join(l2.get('differentiation_angles', l1.get('differentiation_angles', []))[:3])}
- 头部品牌：{', '.join(l1.get('top_brands', [])[:5])}
- 内容红线：{', '.join(l1.get('content_redlines', [])[:3])}

只输出 JSON，每项建议具体可落地。"""
        raw = await _advisor_generate_safe(synth_prompt)
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        synth = json.loads(text.strip())
        brief.update(synth)
    except Exception as e:
        logger.warning(f"[L3] 综合推理失败: {e}")
        try:
            from json_repair import repair_json
            synth = json.loads(repair_json(text.strip()))
            brief.update(synth)
        except Exception:
            pass

    logger.info(f"[L3] 完成: {city}·{industry}·{category} · 字段数={len([k for k in brief if k not in ('city','source','generated_at')])}")
    return brief


# ========== 合并三层知识 ==========

def get_industry_context(profile: dict) -> str:
    """读取三层知识，合并为可注入 prompt 的文本"""
    industry = profile.get("industry", "")
    category = profile.get("category", "")
    if not industry:
        return ""

    l1 = get_industry_knowledge(industry, level="industry") or {}
    l2 = get_industry_knowledge(industry, category) if category else {}
    l2 = l2 or {}
    l3 = profile.get("industry_brief")
    if isinstance(l3, str):
        try:
            l3 = json.loads(l3)
        except (ValueError, TypeError):
            l3 = {}
    l3 = l3 or {}

    parts = []

    # L1 行业层
    if l1:
        lines = [f"## 行业通识（{industry}）"]
        if l1.get("market_overview"):
            lines.append(f"- 概况：{l1['market_overview']}")
        if l1.get("market_trend"):
            lines.append(f"- 趋势：{l1['market_trend']}")
        if l1.get("top_brands"):
            lines.append(f"- 头部品牌：{', '.join(l1['top_brands'][:5])}")
        if l1.get("content_redlines"):
            lines.append(f"- 内容红线：{', '.join(l1['content_redlines'][:4])}")
        if l1.get("industry_terms"):
            lines.append(f"- 行业术语：{', '.join(l1['industry_terms'][:8])}")
        # v3.6 新字段
        jargon = l1.get("industry_jargon") or []
        if jargon:
            jargon_str = ", ".join(j.get("term", "") for j in jargon[:12] if j.get("term"))
            if jargon_str:
                lines.append(f"- 行业黑话(v3.6)：{jargon_str}")
        authority = l1.get("authority_sources") or []
        if authority:
            auth_lines = []
            for a in authority[:5]:
                src = a.get("source", "")
                concl = a.get("conclusion", "")[:80]
                if src and concl:
                    auth_lines.append(f"  · {src}: {concl}")
            if auth_lines:
                lines.append("- 权威信源(v3.6,引用时带 url):\n" + "\n".join(auth_lines))
        counter = l1.get("counter_consensus") or []
        if counter:
            counter_lines = [f"  · {c.get('insight', '')[:100]}" for c in counter[:5] if c.get("insight")]
            if counter_lines:
                lines.append("- 行业反共识/潜规则(v3.6,可作钩子素材):\n" + "\n".join(counter_lines))
        parts.append("\n".join(lines))

    # L2 品类层（有品类时才注入，覆盖 L1 的通用信息）
    if l2:
        label = f"{industry}·{category}" if category else industry
        lines = [f"## 品类特征（{label}）"]
        # 用户有的字段不注入（L3覆盖）
        if not l3.get("my_audience"):
            aud = l2.get("target_audience", {})
            if aud.get("primary"):
                lines.append(f"- 核心客群：{aud['primary']}")
            if aud.get("decision_factors"):
                lines.append(f"- 决策因素：{', '.join(aud['decision_factors'][:4])}")
        if not profile.get("products") and l2.get("typical_products"):
            lines.append(f"- 典型产品：{', '.join(l2['typical_products'][:5])}")
        if l2.get("price_range"):
            lines.append(f"- 价格带：{l2['price_range']}")
        if l2.get("conversion_paths"):
            lines.append(f"- 获客路径：{' / '.join(l2['conversion_paths'][:3])}")
        if l2.get("content_types"):
            lines.append(f"- 适合内容：{', '.join(l2['content_types'][:4])}")
        if l2.get("differentiation_angles"):
            lines.append(f"- 差异化角度：{', '.join(l2['differentiation_angles'][:4])}")
        if l2.get("cta_templates"):
            lines.append(f"- CTA参考：{' / '.join(l2['cta_templates'][:2])}")
        if l2.get("hot_formats"):
            lines.append(f"- 当前爆款：{', '.join(l2['hot_formats'][:4])}")
        # v3.6 新字段
        voices = l2.get("user_voices_pool") or []
        if voices:
            voice_lines = [f"  · {v.get('quote', '')[:120]}" for v in voices[:6] if v.get("quote")]
            if voice_lines:
                lines.append("- 真实用户原话(v3.6,FAQ/钩子素材):\n" + "\n".join(voice_lines))
        cases = l2.get("case_evidence_pool") or []
        if cases:
            case_lines = []
            for c in cases[:6]:
                subj = c.get("subject", "")
                result = c.get("result", "")[:80]
                if subj and result:
                    case_lines.append(f"  · {subj}: {result}")
            if case_lines:
                lines.append("- 真实案例数据(v3.6,论据砖块):\n" + "\n".join(case_lines))
        parts.append("\n".join(lines))

    # L3 用户层（覆盖公共数据）
    if l3:
        lines = ["## 用户定制信息"]
        if l3.get("city_context"):
            lines.append(f"- 本地市场：{l3['city_context']}")
        if l3.get("my_audience"):
            lines.append(f"- 精准客群：{l3['my_audience']}")
        if l3.get("my_differentiation"):
            lines.append(f"- 差异化定位：{l3['my_differentiation']}")
        if l3.get("my_conversion"):
            lines.append(f"- 获客路径：{l3['my_conversion']}")
        if l3.get("content_strategy"):
            lines.append(f"- 内容策略：{l3['content_strategy']}")
        if l3.get("local_competitors"):
            lines.append(f"- 本地竞品：{', '.join(l3['local_competitors'][:4])}")
        if l3.get("local_districts"):
            lines.append(f"- 热门商圈：{l3['local_districts']}")
        if l3.get("local_platform_tips"):
            lines.append(f"- 本地平台运营：{l3['local_platform_tips']}")
        if l3.get("local_hot_topics"):
            topics_list = l3["local_hot_topics"] if isinstance(l3["local_hot_topics"], list) else [l3["local_hot_topics"]]
            lines.append(f"- 本地热门话题：{', '.join(topics_list[:4])}")
        if l3.get("local_audience"):
            lines.append(f"- 本地客群特征：{l3['local_audience']}")
        parts.append("\n".join(lines))

    return "\n\n".join(parts)


# ========== 众包矫正回写 ==========

def apply_user_correction(industry: str, category: str, field: str, old_value: str, new_value: str):
    """[deprecated v3.6] 用 apply_correction_to_l1_l2 替代（commit 4 实现）"""
    logger.info(f"[矫正] {industry}/{category}: {field} '{old_value}' -> '{new_value}'")


# ============================================================================
# v3.6 升级模块（CTO-15.2 · 2026-04-19）
#
# 5 类 raw 素材 + 多 AI 投票审核 + L1/L2/L3 飞轮架构
#
# 设计思路:
#   - L1 行业层(90 天 TTL): jargon + authority_sources + counter_consensus
#     全行业用户共用,首个用户拓荒后 90 天内同行业用户全免费复用
#   - L2 品类层(30 天 TTL): user_voices_pool + case_evidence_pool
#     同品类用户共用,30 天内同品类用户免费复用
#   - L3 用户层: service_scope + 用户特化字段(my_差异化/本地竞品/真案例)
#     仅本品牌专属
#
# 飞轮成本(按 ¥/次,实测 prototype 数据):
#   - 第 1 用户(全新行业品类): ¥3.85 拓荒 (full L1+L2+L3)
#   - 第 N 用户(同行业同品类): ¥0.3 (L1/L2 缓存 + L3 用户特化)
#   - 第 N 用户(同行业新品类): ¥1.8 (L2 重新采 + L3)
#
# Prompt 哲学(老板拍板): 原材料,不是二次加工 -- 厨师怎么做菜是下游的事
# ============================================================================


# v3.6 metaso 双层 JSON 解析(老版 collector 的 _metaso_search_safe 拿到的是外层 wrap, 没 parse 双层 -- 历史 bug)
async def _v36_metaso_pages(query: str, size: int = 5) -> list:
    """metaso 召回 + 提取 raw webpages(双层 JSON 解析)"""
    try:
        from tools.search.metaso_mcp import metaso_web_search
        from tools.search.provider_router import search_scenario

        # [Provider 层 2026-07-28] 调研抓取场景键(灰度第一批):默认全 metaso 零变化。
        with search_scenario("research"):
            r = await asyncio.wait_for(
                metaso_web_search(query, scope="webpage", include_summary=True, include_raw_content=True, size=size),
                timeout=SEARCH_TIMEOUT,
            )
        for item in r.content:
            if isinstance(item, dict) and item.get("type") == "text":
                outer = json.loads(item.get("text", "{}"))
                results = outer.get("results", [])
                if results and isinstance(results[0], dict):
                    inner_text = results[0].get("text", "{}")
                    inner = json.loads(inner_text) if isinstance(inner_text, str) else inner_text
                    pages = inner.get("webpages", [])
                    return [
                        {
                            "title": p.get("title", "") or "",
                            "url": p.get("link", "") or "",
                            "content": p.get("content", "") or "",
                            "snippet": p.get("snippet", "") or "",
                        }
                        for p in pages
                        if p.get("content")
                    ]
    except Exception as e:
        logger.warning(f"[v3.6 metaso] {query[:40]}: {str(e)[:80]}")
    return []


async def _v36_review_one_page(category_key: str, page: dict, industry: str, prompt_template: str) -> Optional[dict]:
    """让多 AI 同时审核一条 raw page,投票 ≥1 通过(2 AI 制)"""
    from services.multi_ai_voter import multi_ai_review
    content = page["content"][:2200]
    if not content or len(content) < 50:
        return None
    prompt = prompt_template.format(industry=industry, title=page["title"][:80], content=content, url=page["url"])
    result = await multi_ai_review(prompt, threshold=1)
    if not result["passed"]:
        return None
    items = result["items"]
    for item in items:
        if isinstance(item, dict):
            item["__source_url"] = page["url"]
            item["__source_title"] = page["title"]
    return {"items": items, "vote": result["vote_pass_count"]}


# ========== L1 行业层 v3.6 新增 3 类 ==========

V36_AUTHORITY_PROMPT = """你是「{industry}」行业素材采集员,从下方网页**摘录有明确机构出处的权威结论**(原材料不二次加工)。

铁律:
- ✅ 必须有明确机构/报告/标准/政府文件出处(艾瑞/QuestMobile/信通院/T/CMCA xxx/工信部 等)
- ✅ conclusion 字段保留**原文结论**(>=20 字)不要二次概括
- ❌ 记者/作者个人观点 → 不算
- ❌ 营销话术 → 不算
- 必须与「{industry}」行业相关,其他行业 skip
- 不相关时 items 必须为空数组

只输出 JSON,不要 markdown:
{{"is_relevant_industry": true/false, "reasoning": "20字内", "items": [{{"source": "机构/报告全名", "conclusion": "结论原文", "raw_quote": "原文片段"}}]}}

网页标题: {title}
网页正文:
{content}"""

V36_COUNTER_REVIEW_PROMPT = """你是「{industry}」行业资深从业者,从下方网页**提取行业内反共识/潜规则/内幕**(大众不知但业内都知道,可做爆款短视频钩子)。

铁律:
- ✅ 必须是「{industry}」行业内部的反共识(其他行业的请 skip)
- ✅ insight 字段保留**网页原文表述**(>=40 字),保留作者语气
- ✅ evidence 字段保留**支撑数据或事实原文**(>=20 字)
- ❌ 通用商业道理("营销要避免炒作"") → 不算
- ❌ 营销话术("我家最专业") → 不算

判定法: 把"{industry}"换成其他行业还成立的 → 不是行业反共识。

只输出 JSON: {{"is_relevant_industry": true/false, "reasoning": "20字内", "items": [{{"insight": "原文", "evidence": "支撑事实原文", "raw_quote": "原文段落"}}]}}

网页标题: {title}
网页正文:
{content}"""

V36_JARGON_DIRECT_PROMPT = """你是「{industry}」行业资深从业者。列出该行业的**专业术语/黑话/缩写**,要求是圈内才用的。

铁律:
- ✅ 必须是「{industry}」行业专属术语
- ❌ 通用商业黑话(赋能/抓手/闭环/颗粒度/拉齐水位/底层逻辑/复盘/打法) → 严格不要
- ❌ 通用网络词(内卷/破圈) → 不要
- 至少 15 条,最多 30 条

输出 JSON: {{"items": [{{"term": "术语", "meaning": "圈外人能懂的解释(20字内)", "jargon_type": "技术术语|业务黑话|缩写简称"}}]}}"""


async def _collect_l1_authority(industry: str) -> list:
    """L1 权威源池(metaso + 多 AI 投票) -- 全行业共用"""
    queries = [
        f"{industry} 白皮书 报告 2025",
        f"{industry} 行业研究 协会 数据",
        f"{industry} 政策 规范 标准",
        f"{industry} 艾瑞 易观 信通院",
    ]
    fetch_tasks = [_v36_metaso_pages(q, size=4) for q in queries]
    pages_lists = await asyncio.gather(*fetch_tasks, return_exceptions=True)
    pages = []
    seen_urls = set()
    for plist in pages_lists:
        if isinstance(plist, list):
            for p in plist:
                if p["url"] not in seen_urls:
                    seen_urls.add(p["url"])
                    pages.append(p)
    sem = asyncio.Semaphore(20)

    async def _bounded(p):
        async with sem:
            return await _v36_review_one_page("authority", p, industry, V36_AUTHORITY_PROMPT)

    review_results = await asyncio.gather(*[_bounded(p) for p in pages], return_exceptions=True)
    items = []
    for r in review_results:
        if r and not isinstance(r, Exception):
            items.extend(r["items"])
    return items


async def _collect_l1_counter(industry: str) -> list:
    """L1 反共识池(metaso + 多 AI 投票) -- 全行业共用"""
    queries = [
        f"{industry} 潜规则 内幕 真相",
        f"{industry} 误区 大众不知 真相",
        f"{industry} 行业黑幕 知乎",
        f"{industry} 反常识 业内人才知道",
    ]
    fetch_tasks = [_v36_metaso_pages(q, size=4) for q in queries]
    pages_lists = await asyncio.gather(*fetch_tasks, return_exceptions=True)
    pages = []
    seen_urls = set()
    for plist in pages_lists:
        if isinstance(plist, list):
            for p in plist:
                if p["url"] not in seen_urls:
                    seen_urls.add(p["url"])
                    pages.append(p)
    sem = asyncio.Semaphore(20)

    async def _bounded(p):
        async with sem:
            return await _v36_review_one_page("counter", p, industry, V36_COUNTER_REVIEW_PROMPT)

    review_results = await asyncio.gather(*[_bounded(p) for p in pages], return_exceptions=True)
    items = []
    for r in review_results:
        if r and not isinstance(r, Exception):
            items.extend(r["items"])
    return items


async def _collect_l1_jargon(industry: str) -> list:
    """L1 行业术语黑话(多 AI 直接答 + 取并集) -- 全行业共用"""
    from services.multi_ai_voter import multi_ai_direct
    prompt = V36_JARGON_DIRECT_PROMPT.format(industry=industry)
    result = await multi_ai_direct(prompt)
    # 取并集(下游 max 整合时去重),每条标 source_ai
    return result["all_items"]


# ========== L2 品类层 v3.6 新增 2 类 ==========

V36_USER_VOICES_PROMPT = """你是「{industry}/{category}」品类素材采集员,从下方网页**只提取真实用户/客户口语化原话**(原材料不二次加工)。

铁律:
- ✅ raw_quote 必须是用户/客户原话(含口语化"家人们"/emoji/求推荐)
- ✅ 必须与「{industry}/{category}」品类相关
- ❌ 文章作者口吻 / 编辑代客户提问 / 营销话术 → 不算
- ❌ 其他品类用户原话 → 不算
- 不相关时 items 必须为空数组

只输出 JSON: {{"is_relevant_industry": true/false, "reasoning": "20字内", "items": [{{"quote": "原话", "context": "出处类型(知乎/小红书/微博/论坛/博客评论/其他)"}}]}}

网页标题: {title}
网页正文:
{content}"""

V36_CASE_EVIDENCE_PROMPT = """你是「{industry}/{category}」品类素材采集员,从下方网页**摘录真实案例数据**(必须含具体数字)。

铁律:
- ✅ 必须有具体数字(增长率%/绝对数/降本%/转化率%/金额/时长)
- ✅ 必须有明确主体(公司/品牌名,匿名"某"也可)
- ✅ 必须与「{industry}/{category}」品类相关
- ❌ 没有数字的"成功案例"叙述 → 不算
- ❌ 其他品类案例 → 不算

只输出 JSON: {{"is_relevant_industry": true/false, "reasoning": "20字内", "items": [{{"subject": "主体", "action": "动作原文", "result": "数据结果原文(含数字)", "time": "时间或空", "raw_quote": "原文片段"}}]}}

网页标题: {title}
网页正文:
{content}"""


async def _collect_l2_user_voices(industry: str, category: str) -> list:
    """L2 品类级用户原话池(metaso + 多 AI 投票)"""
    prefix = f"{category}" if category else industry
    queries = [
        f"{prefix} 怎么样 真实评价",
        f"{prefix} 坑 后悔 踩雷",
        f"{prefix} 知乎 推荐 经验",
        f"{prefix} 小红书 测评",
    ]
    fetch_tasks = [_v36_metaso_pages(q, size=4) for q in queries]
    pages_lists = await asyncio.gather(*fetch_tasks, return_exceptions=True)
    pages = []
    seen = set()
    for plist in pages_lists:
        if isinstance(plist, list):
            for p in plist:
                if p["url"] not in seen:
                    seen.add(p["url"])
                    pages.append(p)
    industry_label = f"{industry}/{category}" if category else industry
    sem = asyncio.Semaphore(20)

    async def _bounded(p):
        async with sem:
            return await _v36_review_one_page(
                "user_voices", p, industry_label,
                V36_USER_VOICES_PROMPT.replace("{category}", category or industry).replace("{industry}", industry)
            )

    review_results = await asyncio.gather(*[_bounded(p) for p in pages], return_exceptions=True)
    items = []
    for r in review_results:
        if r and not isinstance(r, Exception):
            items.extend(r["items"])
    return items


async def _collect_l2_case_evidence(industry: str, category: str) -> list:
    """L2 品类级真实案例池(metaso + 多 AI 投票)"""
    prefix = f"{category}" if category else industry
    queries = [
        f"{industry} 案例 增长 数据 ROI",
        f"{prefix} 成功案例 真实数据",
        f"{prefix} 客户 转化 实测",
        f"{industry} 月增长 效果 实战",
    ]
    fetch_tasks = [_v36_metaso_pages(q, size=4) for q in queries]
    pages_lists = await asyncio.gather(*fetch_tasks, return_exceptions=True)
    pages = []
    seen = set()
    for plist in pages_lists:
        if isinstance(plist, list):
            for p in plist:
                if p["url"] not in seen:
                    seen.add(p["url"])
                    pages.append(p)
    industry_label = f"{industry}/{category}" if category else industry
    sem = asyncio.Semaphore(20)

    async def _bounded(p):
        async with sem:
            return await _v36_review_one_page(
                "case_evidence", p, industry_label,
                V36_CASE_EVIDENCE_PROMPT.replace("{category}", category or industry).replace("{industry}", industry)
            )

    review_results = await asyncio.gather(*[_bounded(p) for p in pages], return_exceptions=True)
    items = []
    for r in review_results:
        if r and not isinstance(r, Exception):
            items.extend(r["items"])
    return items


# ========== L3 用户层 v3.6 重写 ==========

V36_SCOPE_PROMPT = """你是业务范围分析专家。判定品牌服务范围。

注意 city 是注册地不一定是服务范围,只看 business/products/target_users:
- local = 必须到店上门(餐饮/美容/医疗/家政)
- national = 全国线上(SaaS/电商/咨询/数字营销)
- hybrid = 全国可服务且明确本地定位(连锁/有强城市定位的 SaaS)

品牌信息:
- 品牌名: {brand_name}
- 行业: {industry} | 品类: {category}
- 业务: {business}
- 产品: {products}
- 客群: {target_users}
- 注册城市(参考): {city}

只输出 JSON: {{"scope": "local|national|hybrid", "reasoning": "40字内", "use_city_prefix": false}}"""

V36_USER_FOCUS_PROMPT = """你是「{industry}」行业资深运营顾问。基于品牌信息 + 行业公共素材,产出**仅本品牌特化**的 4 个字段。

不要重复行业公共素材(术语/案例/权威源/反共识 — 这些已在 L1/L2 公共库),只产出**这家公司独有**的内容。

品牌信息:
- 品牌名: {brand_name}
- 行业/品类: {industry} / {category}
- 城市: {city} (服务范围: {scope})
- 业务: {business}
- 产品: {products}
- 客群: {target_users}

输出 JSON,不要 markdown:
{{
  "my_differentiation": "推荐的差异化定位(~80字: 对手做什么/你做什么/为什么用户选你)",
  "my_audience_segment": "推荐的精准目标客群(~80字: 在该行业大客群里聚焦哪一群人 + 决策动机)",
  "my_local_competitors": ["本地竞品 3-5 条 (仅 scope=local/hybrid 时填,national 留空数组)"],
  "my_real_cases": ["该品牌真实案例 3-5 条占位 (用户自己后续填,这里 LLM 给推荐方向: '可以收集 XX 类型案例如 XX')"]
}}"""


# 2026-05-11 Phase 2 V37 升级版 Prompt(加 L1/L2 真材料 + Metaso 真证据注入)
V36_SCOPE_PROMPT_V37 = """你是业务范围分析专家。基于品牌信息 + L1 行业头部品牌 + Metaso 实时市场证据,判定品牌服务范围。

注意 city 是注册地不一定是服务范围,综合以下信息判断:
- local = 必须到店上门(餐饮/美容/医疗/家政)· 或同城竞品密集
- national = 全国线上(SaaS/电商/咨询/数字营销)· 或行业头部已是全国玩家
- hybrid = 全国可服务且明确本地定位(连锁/有强城市定位的 SaaS)

品牌信息:
- 品牌名: {brand_name}
- 行业: {industry} | 品类: {category}
- 业务: {business}
- 产品: {products}
- 客群: {target_users}
- 注册城市(参考): {city}

L1 行业头部品牌(参考竞争格局):
{top_brands}

Metaso 实时市场证据(同行业/同城竞品标题):
{competition_evidence}

要求 reasoning 含真证据(如"行业头部 X/Y/Z 皆全国 SaaS · 故 national" / "同城品类 N 家本地竞品 · 故 local")。

只输出 JSON: {{"scope": "local|national|hybrid", "reasoning": "60字内 · 含具体证据", "use_city_prefix": false}}"""


V36_USER_FOCUS_PROMPT_V37 = """你是「{industry}」行业资深运营顾问。你的任务是基于品牌信息 + L1/L2 公共素材 + Metaso 市场实证,产出**仅本品牌特化**的 4 个字段。

⚠ 关键原则:
1. **不要凭空捏造** - 所有内容必须基于下方提供的素材或品牌信息推导
2. **不要重复 L1/L2 公共素材原文** - 这些素材是"参考线索",你要在其基础上做"本品牌独有"的判断
3. **真实案例字段必须基于 L2 case_evidence_pool 真材料** - 不是 LLM 现编占位文案

# 品牌信息
- 品牌名: {brand_name}
- 行业/品类: {industry} / {category}
- 城市: {city} (服务范围: {scope})
- 业务: {business}
- 产品: {products}
- 客群: {target_users}

# L1 权威信息源(真行业权威机构 + 结论)
{authority_block}

# L1 行业黑话(自然嵌入文案)
{jargon_block}

# L2 案例数据池(真实案例 · 用于推导本品牌可比对的真案例方向)
{cases_block}

# L2 用户原话池(真客户声音 · 用于推导本品牌精准客群的真实决策动机)
{voices_block}

# Metaso 市场实证(本地竞品 / 行业头部页面)
{market_evidence}

# 输出 JSON · 不要 markdown
{{
  "my_differentiation": "差异化定位(~100字:对手做什么/你做什么/为什么用户选你 · 必须引用 1 条 L1 authority_sources 结论或 Metaso 市场证据作为对照)",
  "my_audience_segment": "精准目标客群(~100字:在行业大客群里聚焦哪一群人 · 必须基于 L2 user_voices_pool 用户原话推导真实决策动机 · 引用 1-2 条原话)",
  "my_local_competitors": ["本地竞品 3-5 条 · 仅 scope=local/hybrid 时填 · 优先取 Metaso market_evidence 标题里的真品牌名 · national 时留空数组 []"],
  "my_real_cases": ["真案例 3-5 条 · 必须基于 L2 case_evidence_pool 的真数据推导 · 格式:'同行业 XX 公司做 XX 拿到 XX 数据 · 你可以参考做 XX'(case_evidence_pool 为空时填占位'建议自己补 X 类案例')"]
}}"""


async def _v36_classify_scope(profile: dict, l1: Optional[dict] = None) -> dict:
    """L3 service_scope 判定(deepseek-v4-pro + thinking · ~30s)
    2026-05-11 Phase 2 根治:加 L1 top_brands 注入 + Metaso 市场调研
      · 不再只看 business 模式 · 改成调研驱动
      · scope_reasoning 含真证据(行业头部 N 家 / 同城品类 M 家)
    """
    from services.multi_ai_voter import call_ai
    l1 = l1 or {}
    top_brands_text = ""
    top_brands = l1.get("top_brands") if isinstance(l1, dict) else None
    if isinstance(top_brands, list) and top_brands:
        names = [str(b.get("name") if isinstance(b, dict) else b)[:40] for b in top_brands[:8] if b]
        top_brands_text = "、".join([n for n in names if n])

    # Metaso 调研行业 + 城市竞争(可选 · 失败不阻塞)
    competition_evidence = ""
    city = profile.get("city", "") or ""
    industry = profile.get("industry", "") or ""
    category = profile.get("category", "") or ""
    if industry:
        try:
            queries = [f"{industry} {city} 同城 竞品" if city else f"{industry} 全国 头部 品牌", f"{industry} {category or '行业'} 市场规模 2025"]
            pages_lists = await asyncio.gather(
                *[_v36_metaso_pages(q, size=3) for q in queries[:2]],
                return_exceptions=True,
            )
            seen = set(); snippets = []
            for pl in pages_lists:
                if isinstance(pl, list):
                    for p in pl[:3]:
                        title = str(p.get("title", ""))[:60]
                        if title and title not in seen:
                            seen.add(title)
                            snippets.append(f"- {title}")
            if snippets:
                competition_evidence = "\n".join(snippets[:6])
        except Exception as e:
            logger.warning(f"[v3.6 L3 scope] Metaso 调研失败(不阻塞): {e}")

    prompt = V36_SCOPE_PROMPT_V37.format(
        brand_name=profile.get("brand_name", profile.get("name", "")),
        industry=industry,
        category=category,
        business=profile.get("business", "")[:200],
        products=str(profile.get("products", ""))[:200],
        target_users=str(profile.get("target_users", ""))[:200],
        city=city,
        top_brands=top_brands_text or "(L1 暂无)",
        competition_evidence=competition_evidence or "(Metaso 未返结果 · 按业务模式判定)",
    )
    raw = await call_ai(
        "deepseek-v4-pro", prompt, temperature=0.1, max_tokens=500, timeout=180.0,
        extra_body={"thinking": {"type": "enabled"}},
    )
    parsed = _parse_json_loose(raw)
    return parsed or {"scope": "national", "reasoning": "默认 national", "use_city_prefix": False}


async def _v36_user_focus(profile: dict, scope: str, l1: Optional[dict] = None, l2: Optional[dict] = None) -> dict:
    """L3 用户特化 4 字段(deepseek-v4-pro + thinking · ~120s)

    2026-05-11 Phase 2 根治(老板报"500 积分内容低质"):
      · 注入 L1 authority_sources(真权威源)+ industry_jargon(真行业黑话)
      · 注入 L2 case_evidence_pool(真案例数据)+ user_voices_pool(真客户原话)
      · Metaso 搜"本地竞品 / 行业头部 5 家"拿真材料(并发 2 query)
      · LLM 基于真材料生成 · 不再凭空捏造
      · thinking 让深度推理 · 配合真材料质量进一步提升
    """
    from services.multi_ai_voter import call_ai
    l1 = l1 or {}
    l2 = l2 or {}
    industry = profile.get("industry", "") or ""
    category = profile.get("category", "") or ""
    city = profile.get("city", "") or ""

    # 提取 L1 素材
    def _list_brief(items, key_name, limit=5, char_limit=100):
        out = []
        if not isinstance(items, list):
            return out
        for it in items[:limit]:
            if isinstance(it, dict):
                v = it.get(key_name)
                if v and str(v).strip():
                    out.append(str(v).strip()[:char_limit])
        return out

    authority_lines = []
    for a in (l1.get("authority_sources") or [])[:5]:
        if isinstance(a, dict):
            src = str(a.get("source", ""))[:40]
            con = str(a.get("conclusion", ""))[:120]
            if src and con:
                authority_lines.append(f"- {src}:{con}")
    authority_block = "\n".join(authority_lines) or "(L1 暂无)"

    jargon_terms = _list_brief(l1.get("industry_jargon"), "term", limit=8, char_limit=20)
    jargon_block = "、".join(jargon_terms) or "(L1 暂无)"

    case_lines = []
    for c in (l2.get("case_evidence_pool") or [])[:5]:
        if isinstance(c, dict):
            sub = str(c.get("subject", ""))[:40]
            res = str(c.get("result", ""))[:120]
            if res:
                case_lines.append(f"- {sub}:{res}" if sub else f"- {res}")
    cases_block = "\n".join(case_lines) or "(L2 暂无)"

    voice_lines = []
    for v in (l2.get("user_voices_pool") or [])[:5]:
        if isinstance(v, dict):
            q = str(v.get("quote", ""))[:120]
            ctx = str(v.get("context", ""))[:20]
            if q:
                voice_lines.append(f"- [{ctx or '用户'}] {q}" if ctx else f"- {q}")
    voices_block = "\n".join(voice_lines) or "(L2 暂无)"

    # Metaso 搜本地竞品 + 行业头部(失败不阻塞)
    market_evidence = ""
    if industry:
        try:
            base_queries = [f"{industry} {category or ''} 头部 公司".strip()]
            if scope in ("local", "hybrid") and city:
                base_queries.append(f"{city} {industry} {category or ''} 本地 竞品".strip())
            pages_lists = await asyncio.gather(
                *[_v36_metaso_pages(q, size=4) for q in base_queries[:2]],
                return_exceptions=True,
            )
            seen = set(); snippets = []
            for pl in pages_lists:
                if isinstance(pl, list):
                    for p in pl[:3]:
                        title = str(p.get("title", ""))[:80]
                        sn = str(p.get("snippet") or p.get("content", ""))[:140]
                        key = title[:40]
                        if title and key not in seen:
                            seen.add(key)
                            snippets.append(f"- {title}:{sn}")
            if snippets:
                market_evidence = "\n".join(snippets[:6])
        except Exception as e:
            logger.warning(f"[v3.6 L3 focus] Metaso 搜竞品失败(不阻塞): {e}")

    prompt = V36_USER_FOCUS_PROMPT_V37.format(
        brand_name=profile.get("brand_name", profile.get("name", "")),
        industry=industry,
        category=category,
        business=profile.get("business", "")[:300],
        products=str(profile.get("products", ""))[:300],
        target_users=str(profile.get("target_users", ""))[:300],
        city=city,
        scope=scope,
        authority_block=authority_block,
        jargon_block=jargon_block,
        cases_block=cases_block,
        voices_block=voices_block,
        market_evidence=market_evidence or "(Metaso 未返结果 · 仅基于品牌信息推理)",
    )
    raw = await call_ai(
        "deepseek-v4-pro", prompt, temperature=0.3, max_tokens=4000, timeout=300.0,
        extra_body={"thinking": {"type": "enabled"}},
    )
    return _parse_json_loose(raw) or {}


def _parse_json_loose(text: str) -> Optional[dict]:
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    try:
        return json.loads(text.strip())
    except Exception:
        try:
            from json_repair import repair_json
            return json.loads(repair_json(text.strip()))
        except Exception:
            return None


# ============================================================================
# A9 · v3.7 edited_fields 保护(CTO-15.9 2026-04-25)
# M1c T5 代理手编 MarketInsightCard 后 · deep_analyze 重采不得覆盖手编值
# ============================================================================

# 默认受保护字段(代理可编辑的 market_insight 3 键 + service_scope)
USER_EDITABLE_BRIEF_FIELDS = (
    "authority_sources",
    "hot_formats",
    "my_differentiation",
    "my_local_competitors",  # local_competitors 别名
    "local_competitors",
)


def preserve_user_edited_fields(
    old_brief: dict | None,
    new_brief: dict,
    protected: tuple = USER_EDITABLE_BRIEF_FIELDS,
) -> dict:
    """A9 · market_insight + 代理手编字段在重采时保留

    CTO-15.9 M1c T5 + A9:
    - 代理在 BrandDetailPage.MarketInsightCard 编辑 authority_sources / hot_formats /
      my_differentiation 后 · 深度解析重采不得直接覆盖(保留代理背书价值)
    - 如果 old_brief 里某字段非空 · 且是 protected 列表里的字段 · 用 old 覆盖 new
    - 如果 old 为空 · 用 new(首次填充场景)
    - 不保护 _v37_meta / generated_at / source 等元数据字段

    Args:
        old_brief: 当前 profile.industry_brief JSONB(可能是 dict · 字符串 · None)
        new_brief: deep_analyze 新产出
        protected: 需要保护的字段 tuple

    Returns:
        merged brief(dict · 含保护字段的 old 值 + 其他字段的 new 值)
    """
    if not isinstance(new_brief, dict):
        return {}
    # 兼容 old 是 JSON 字符串
    if isinstance(old_brief, str):
        try:
            old_brief = json.loads(old_brief) if old_brief.strip() else {}
        except Exception:
            old_brief = {}
    if not isinstance(old_brief, dict):
        return new_brief

    merged = dict(new_brief)
    for field in protected:
        old_val = old_brief.get(field)
        # 判断 old_val 是否"有内容"
        has_content = False
        if isinstance(old_val, str) and old_val.strip():
            has_content = True
        elif isinstance(old_val, list) and len(old_val) > 0:
            has_content = True
        elif isinstance(old_val, dict) and old_val:
            has_content = True
        elif old_val not in (None, "", [], {}):
            has_content = True

        if has_content:
            merged[field] = old_val
    return merged


async def deep_analyze_user_v36(profile: dict) -> dict:
    """v3.6 L3 用户深度解析(500 积分,异步调用)

    架构:
      1. ensure L1 行业层存在(没有就采集 + 入库, 90 天 TTL,同行业用户共用)
      2. ensure L2 品类层存在(没有就采集 + 入库, 30 天 TTL,同品类用户共用)
      3. service_scope 判定(flash 5s)
      4. 用户特化 4 字段(max 30s · 仅产出本品牌独有内容)
      5. 拷贝 L1/L2 部分字段到 brief 兼容层(让 article_writer.py 等下游零改动)

    成本(实测):
      - L1+L2 缓存命中(同品类第 N 用户): 仅 step 3+4 ≈ ¥0.3
      - L1 命中 L2 miss(同行业新品类): + L2 采集 ≈ ¥1.8
      - L1+L2 全 miss(全新行业): + L1+L2 采集 ≈ ¥3.85
    """
    industry = profile.get("industry", "")
    category = profile.get("category", "")
    city = profile.get("city", "") or ""

    logger.info(f"[v3.6 L3] 深度解析: {profile.get('brand_name', profile.get('name', '?'))} · {industry}/{category}")
    t0 = time.time()

    # Step 1+2: ensure L1+L2 存在(并行)
    l1 = get_industry_knowledge(industry, level="industry") or {}
    l2 = get_industry_knowledge(industry, category) if category else {}
    l2 = l2 or {}

    fetch_tasks = []
    if not l1 and industry:
        fetch_tasks.append(("l1", collect_industry_knowledge(industry)))
    if not l2 and category:
        fetch_tasks.append(("l2", collect_category_knowledge(industry, category)))

    if fetch_tasks:
        results = await asyncio.gather(*[t[1] for t in fetch_tasks], return_exceptions=True)
        for (key, _), result in zip(fetch_tasks, results):
            if isinstance(result, dict):
                if key == "l1":
                    l1 = result
                else:
                    l2 = result
        logger.info(f"[v3.6 L3] L1/L2 拓荒完成 · 耗时 {time.time()-t0:.1f}s")

    # Step 3: service_scope 判定(2026-05-11 Phase 2:传 L1 让 scope 调研驱动)
    scope_info = await _v36_classify_scope(profile, l1=l1)
    scope = scope_info.get("scope", "national")

    # Step 4: 用户特化字段(2026-05-11 Phase 2:传 L1+L2 让 LLM 基于真材料 · 不再凭空捏造)
    focus = await _v36_user_focus(profile, scope, l1=l1, l2=l2)

    # Step 5: 装配 brief(向后兼容 + v3.6 新字段)
    # ⚠️ 2026-04-19 修复(commit 16): 老板截图反馈 differentiation_hints/content_strategy 跟
    # my_differentiation 字字相同(直接复制)→ 用户视觉感受 3 个字段重复 → 退款风险
    # 修法: 各字段独立来源,differentiation_hints 从 L1 counter_consensus + my_diff 拆点构造,
    #      content_strategy 拼接 scope/客群/术语形成独立策略文案

    my_diff_text = focus.get("my_differentiation", "") or ""
    my_audience = focus.get("my_audience_segment", "") or ""
    counter_pool = l1.get("counter_consensus") or []
    jargon_pool = l1.get("industry_jargon") or []
    hot_formats = l2.get("hot_formats") or []

    # differentiation_hints (list): 拆 my_differentiation 的句子 + 加 L1 counter_consensus 前 2 条作业内观点支撑
    diff_hints = []
    if my_diff_text:
        # 多分隔符切分(中英文句号 + 分号)
        _text = my_diff_text
        for sep in ['；', ';', '。']:
            _text = _text.replace(sep, '\n')
        for line in _text.split('\n'):
            line = line.strip()
            if len(line) >= 12:
                diff_hints.append(line)
    for c in counter_pool[:2]:
        insight = c.get("insight") if isinstance(c, dict) else None
        if insight and len(str(insight)) >= 12:
            diff_hints.append(str(insight)[:150])
    diff_hints = diff_hints[:5]  # 最多 5 条

    # content_strategy (string): 按 scope 生成独立策略文案,不再复制 my_differentiation
    strategy_parts = []
    if scope == "local":
        strategy_parts.append(f"地域聚焦{city or '本地'}客群,针对核心人群({my_audience[:40] or '小微企业主'})持续输出。")
    elif scope == "national":
        strategy_parts.append(f"全国线上交付,聚焦{my_audience[:40] or '行业核心客群'}。")
    else:  # hybrid
        strategy_parts.append(f"全国 + {city or '本地'}双轨策略,客群: {my_audience[:40] or '行业核心人群'}。")
    if hot_formats:
        formats_text = ', '.join(str(f)[:30] for f in hot_formats[:3] if f)
        if formats_text:
            strategy_parts.append(f"内容形态参考: {formats_text}。")
    if jargon_pool:
        terms = [j.get("term") for j in jargon_pool[:5] if isinstance(j, dict) and j.get("term")]
        if terms:
            strategy_parts.append(f"自然嵌入行业术语: {', '.join(terms)}。")
    content_strategy = " ".join(strategy_parts) if strategy_parts else my_diff_text[:150]

    # my_audience (string): "客群定位"语义,用 my_audience_segment(避免和 target_users profile 字段同名混淆)
    my_audience_value = my_audience or my_diff_text[:80]

    brief = {
        # ===== 元数据 =====
        "city": city,
        "source": "deep_analysis_v36",
        "generated_at": datetime.now().isoformat(),

        # ===== v3.6 新字段 =====
        "service_scope": scope_info.get("scope", "national"),
        "service_scope_reasoning": scope_info.get("reasoning", ""),

        # ===== L3 用户特化 =====
        "my_differentiation": my_diff_text,
        "my_audience_segment": my_audience,
        "my_local_competitors": focus.get("my_local_competitors", []) if scope in ("local", "hybrid") else [],
        "my_real_cases": focus.get("my_real_cases", []),

        # ===== 兼容老字段(从 L1/L2 拷贝 + 拼接生成,各字段独立避免重复) =====
        "differentiation_hints": diff_hints,                          # 修: list 不再是 my_diff 复制
        "top_cases": [c.get("raw_quote", "") for c in (l2.get("case_evidence_pool") or [])[:5] if c.get("raw_quote")],
        "content_pain_points": [v.get("quote", "") for v in (l2.get("user_voices_pool") or [])[:8] if v.get("quote")],
        "regional_kw_ideas": [j.get("term", "") for j in jargon_pool[:15] if j.get("term")],
        "my_audience": my_audience_value,
        "content_strategy": content_strategy,                          # 修: 独立拼接的策略文案
        "local_competitors": focus.get("my_local_competitors", []) if scope in ("local", "hybrid") else [],
        # 2026-05-11 Phase 2 补:L1 authority_sources 直接写入 brief 顶层
        # 老 bug:L1 采集了但 brief 没拷贝 · 前端"权威信息源"永远"暂无"
        "authority_sources": [
            {
                "source": str(a.get("source", ""))[:80],
                "conclusion": str(a.get("conclusion", ""))[:200],
                "url": str(a.get("__source_url", a.get("url", "")))[:200],
            }
            for a in (l1.get("authority_sources") or [])[:8]
            if isinstance(a, dict) and a.get("source") and a.get("conclusion")
        ],
        # 2026-05-11 Phase 2 补:hot_formats 直接从 L2 拷(不再依赖 LLM 现编)
        "hot_formats": [
            str(f).strip() for f in (l2.get("hot_formats") or [])[:8] if str(f).strip()
        ],
        # 2026-05-11 Phase 2 补:行业反共识(差异化对照点)
        "counter_consensus": [
            {
                "insight": str(c.get("insight", ""))[:200],
                "evidence": str(c.get("evidence", ""))[:200],
            }
            for c in (l1.get("counter_consensus") or [])[:5]
            if isinstance(c, dict) and c.get("insight")
        ],
    }

    logger.info(f"[v3.6 L3] 完成 · 耗时 {time.time()-t0:.1f}s · scope={scope} · 字段数={len(brief)}")
    return brief

