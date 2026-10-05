"""
蒸馏管道 - 从诊断数据中提取客户核心信息
增强版：结合知识库提取正确的行业术语定义
"""

import logging
import os
import httpx
import asyncio
from typing import Dict, Any

from services.runtime_file_alarm import report_missing

logger = logging.getLogger("GEO-Distiller")


# LLM配置 - 统一使用 get_llm_config()（已移除硬编码常量）

# 知识库路径
KNOWLEDGE_BASE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "knowledge")

async def call_llm(system_prompt: str, user_message: str, temperature: float = 0.5) -> str:
    """调用LLM - 从settings.json读取配置"""
    from .llm_utils import get_llm_config, get_thinking_disabled_params

    # 使用写作板块默认配置（蒸馏是写作的前置步骤）
    api_url, api_key, model, provider = get_llm_config("topic_planning", "writing")

    if not api_key:
        return f"[LLM未配置] 请设置 {provider.upper()}_API_KEY"

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        "temperature": temperature,
        "max_tokens": 2000
    }
    body.update(get_thinking_disabled_params(api_url, model))
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                api_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json=body
            )
            response.raise_for_status()
            result = response.json()
            return result["choices"][0]["message"]["content"]
    except Exception as e:
        return f"[LLM调用失败] {provider}/{model}: {str(e)}"


# ========================================
# Stage 1: Client Profile Distiller
# ========================================
CLIENT_PROFILE_PROMPT = """你是品牌分析专家。根据以下诊断数据，提取客户核心画像。

输出JSON格式：
{
    "company_name": "公司名称",
    "industry": "行业简称（2-6个字，如：豪车租赁、AI搜索优化、医美整形）",
    "core_business": "核心业务一句话描述",
    "target_customer": "目标客户群体",
    "geographic_focus": "地域重点（如：深圳、全国）",
    "target_cities": ["城市1", "城市2"],
    "service_keywords": ["关键词1", "关键词2", "关键词3"],
    "seed_keywords": ["种子关键词1", "种子关键词2", "种子关键词3", "种子关键词4", "种子关键词5"]
}

【重要说明】
- industry：必须是简洁的行业名称（2-6个字），不是描述句
- target_cities：从数据中提取客户服务的目标城市
- seed_keywords：适合关键词扩展的种子词（用户场景词），不要包含竞品名称

只输出JSON，不要其他文字。"""


# ========================================
# Stage 2: Selling Points Distiller (增强版V2)
# 增加真实数据提取，用于文章引用
# ========================================
SELLING_POINTS_PROMPT = """你是营销文案专家。根据以下客户信息，提取核心卖点、行业术语定义和**真实可验证的数据点**。

要求：
1. 找出客户解决的**绝对痛点**（竞品无法解决的）
2. 提炼3-5个差异化卖点
3. 每个卖点要有具体数据或案例支撑
4. 【重要】从客户数据中提取该行业的核心术语定义和正确指标
5. 【关键】从平台数据中提取**真实可验证的数据点**（如：抖音视频数量、互动数据、账号粉丝数等）

输出JSON格式：
{
    "absolute_pain_point": "客户解决的绝对痛点",
    "selling_points": [
        {"point": "卖点1", "evidence": "支撑数据/案例"},
        {"point": "卖点2", "evidence": "支撑数据/案例"}
    ],
    "unique_value": "一句话核心价值主张",
    "use_scenarios": ["使用场景1", "使用场景2", "使用场景3"],
    "industry_terms": {
        "core_concept_definition": "该行业核心概念的正确定义（从知识库中提取）",
        "correct_metrics": ["正确的评价指标1", "正确的评价指标2"],
        "outdated_metrics": ["过时/错误的指标（应避免使用）"],
        "key_technical_terms": ["核心专业术语1", "核心专业术语2", "核心专业术语3"]
    },
    "real_data_points": {
        "platform_stats": {
            "douyin_video_count": "从数据中提取的抖音视频数量",
            "xiaohongshu_note_count": "从数据中提取的小红书笔记数量",
            "top_engagement": "单条内容最高互动数（点赞+评论+分享）",
            "average_engagement": "平均互动数据"
        },
        "verifiable_examples": [
            {
                "source": "平台名称",
                "metric": "具体指标",
                "value": "真实数值",
                "url": "可验证的链接（如有）"
            }
        ],
        "time_range": "数据时间范围（如：2026年1月）"
    }
}

【重要】use_scenarios要求：
- 提取客户服务的真实使用场景，而非广告语
- 场景应该是名词或短语，如："商务出行"、"旅游用车"、"婚庆接送"、"机场接机"
- 不要写成句子式的描述

只输出JSON，不要其他文字。"""


# ========================================
# Stage 3: Competitor Analysis Distiller
# ========================================
COMPETITOR_ANALYSIS_PROMPT = """你是竞争分析专家。根据以下数据，分析竞品格局。

要求：
1. 识别3-5个真实竞品
2. 分析每个竞品的优势和局限
3. 定位客户与竞品的差异

输出JSON格式：
{
    "competitors": [
        {
            "name": "竞品名称",
            "strengths": ["优势1", "优势2"],
            "limitations": ["局限1"],
            "vs_client": "与客户相比的差异点"
        }
    ],
    "market_position": "客户在市场中的定位"
}

只输出JSON，不要其他文字。"""


class DistillerPipeline:
    """蒸馏管道 - 三阶段提取客户核心信息
    V3增强：支持从 cache 文件读取网页搜索和学术数据
    V6增强：优先使用客户上传的真实资料
    V8增强：正确解析嵌套的data字段结构
    """
    
    def __init__(self, diagnosis_data: Dict[str, Any], cache_file: str = None, client_materials: Dict[str, Any] = None, brand_id: int = None):
        # V8修复：诊断数据可能有嵌套的data字段，需要展平
        self.raw_diagnosis = diagnosis_data
        self.diagnosis_data = self._flatten_diagnosis_data(diagnosis_data)
        self.cache_file = cache_file
        self.cache_data = self._load_cache_data()
        self.client_materials = client_materials  # V6新增：客户上传的真实资料
        self.brand_id = brand_id  # V10新增：用于检索客户专属知识库
        self.client_profile = None
        self.selling_points = None
        self.competitor_analysis = None
    
    def _flatten_diagnosis_data(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """V8新增：将嵌套的data字段展平到顶级"""
        result = {}
        
        # 复制顶级字段
        for key in ['brand', 'brand_name', 'industry', 'keywords', 'session_id']:
            if key in raw_data:
                result[key] = raw_data[key]
        
        # 如果有嵌套的data字段，展平它
        if 'data' in raw_data and isinstance(raw_data['data'], dict):
            data = raw_data['data']
            result['business_context'] = data.get('business_context', '')
            result['competitor_data'] = data.get('competitor_analysis', {})
            result['platform_data'] = {
                'douyin': data.get('douyin', {}),
                'xiaohongshu': data.get('xiaohongshu', {})
            }
            result['ai_visibility'] = data.get('ai_visibility', {})
            result['content_insights'] = data.get('content_insights', {})
            result['asr_transcripts'] = data.get('asr_transcripts', [])
            result['industry_analysis'] = data.get('industry_analysis', {})
            result['web_search'] = data.get('web_search', {})
            result['scholar_search'] = data.get('scholar_search', {})
            
            # V9新增：从AI可见度测试中提取真正的业务竞品（mentioned_brands）
            ai_mentioned_brands = []
            ai_visibility = data.get('ai_visibility', {})
            detail_table = ai_visibility.get('detail_table', [])
            brand_name = raw_data.get('brand', raw_data.get('brand_name', ''))
            for row in detail_table:
                for engine, res in row.get('results', {}).items():
                    brands = res.get('mentioned_brands', [])
                    for b in brands:
                        # 排除自己的品牌名
                        if b and brand_name.lower() not in b.lower():
                            ai_mentioned_brands.append(b)
            # 去重并保留前10个
            result['ai_mentioned_brands'] = list(dict.fromkeys(ai_mentioned_brands))[:10]
        else:
            # 没有嵌套，直接使用原数据
            result.update(raw_data)
        
        # 确保brand_name存在
        if 'brand_name' not in result and 'brand' in result:
            result['brand_name'] = result['brand']
        
        return result
    
    def _load_cache_data(self) -> Dict[str, Any]:
        """加载cache数据（包含网页搜索、学术数据）"""
        if self.cache_file and os.path.exists(self.cache_file):
            try:
                import json
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"  ⚠️ 无法加载cache: {e}")
        return {}
    
    async def run(self) -> Dict[str, Any]:
        """执行完整蒸馏管道 - V7增强：客户资料作为LLM的优质输入"""
        
        # ============================================
        # V7修改：客户资料不应该跳过LLM，而是作为高质量输入
        # 有客户资料 → 传给LLM让它生成更好的标题
        # ============================================
        
        # Stage 1: 提取客户画像（如有客户资料则融合）
        print("  📊 Stage 1: 提取客户画像...")
        self.client_profile = await self._distill_client_profile()
        
        # Stage 2 + Stage 3: 并行执行（都只依赖Stage 1）
        print("  💡 Stage 2+3: 并行提取卖点和竞品分析...")
        self.selling_points, self.competitor_analysis = await asyncio.gather(
            self._distill_selling_points(),
            self._distill_competitors()
        )
        print("  ✅ 并行提取完成")
        
        # ============================================
        # V4增强：提取社媒真实数据用于文章写作
        # ============================================
        social_media_data = self._extract_social_media_data()
        
        # V5增强：加载案例库作为高质量文章参考
        case_examples = self._load_case_library()
        
        # V8增强：提取权威信源数据
        authoritative_sources = self._extract_authoritative_sources()
        
        # ============================================
        # V7增强：确保service_keywords使用诊断数据中的精准关键词
        # ============================================
        diagnosis_keywords = self.diagnosis_data.get("keywords", [])
        if isinstance(diagnosis_keywords, str):
            try:
                diagnosis_keywords = json.loads(diagnosis_keywords)
            except:
                diagnosis_keywords = [diagnosis_keywords] if diagnosis_keywords else []
        
        # 将精准关键词注入到client_profile
        if diagnosis_keywords:
            try:
                profile_dict = json.loads(self.client_profile) if isinstance(self.client_profile, str) else self.client_profile
                profile_dict["service_keywords"] = diagnosis_keywords
                self.client_profile = json.dumps(profile_dict, ensure_ascii=False)
            except:
                pass
        
        return {
            "client_profile": self.client_profile,
            "selling_points": self.selling_points,
            "competitor_analysis": self.competitor_analysis,
            # V4新增：社媒数据透传
            "social_media_data": social_media_data,
            "platform_data": self.diagnosis_data.get("platform_data", {}),
            "content_insights": self.diagnosis_data.get("content_insights", {}),
            "asr_transcripts": self.diagnosis_data.get("asr_transcripts", []),
            # V5新增：案例库范例
            "case_examples": case_examples,
            # V8新增：权威信源数据
            "authoritative_sources": authoritative_sources
        }
    
    def _use_client_materials(self) -> Dict[str, Any]:
        """V6新增：使用客户上传的真实资料（不调用LLM）"""
        import json
        
        materials = self.client_materials
        # 支持多种字段名（brand优先，兼容brand_name）
        brand_name = self.diagnosis_data.get("brand") or self.diagnosis_data.get("brand_name") or "客户品牌"
        industry = self.diagnosis_data.get("industry", "行业")
        
        # 构建客户画像（直接使用真实数据）
        # 获取诊断关键词用于选题（重要！）
        diagnosis_keywords = self.diagnosis_data.get("keywords", [])
        if isinstance(diagnosis_keywords, str):
            import json as json_module
            try:
                diagnosis_keywords = json_module.loads(diagnosis_keywords)
            except:
                diagnosis_keywords = [diagnosis_keywords]
        
        client_profile = json.dumps({
            "company_name": brand_name,
            "industry": industry,
            "service_keywords": diagnosis_keywords,  # 用于精准选题！
            "core_business": materials.get("company_intro", "")[:200],
            "target_customer": materials.get("service_area", ""),
            "founding_year": materials.get("founding_year"),
            "team_size": materials.get("team_size"),
        }, ensure_ascii=False)
        
        # 构建卖点（直接使用客户提供的真实卖点）
        selling_points = json.dumps({
            "core_selling_points": materials.get("core_selling_points", []),
            "unique_value": materials.get("unique_value", ""),
            "methodology": materials.get("methodology", ""),
            "case_studies": materials.get("case_studies", []),
            "pricing_tiers": materials.get("pricing_tiers", []),
            "testimonials": materials.get("testimonials", []),
            "credentials": materials.get("credentials", []),
            "source": "client_uploaded"  # 标记数据来源
        }, ensure_ascii=False)
        
        # 社媒数据还是从诊断数据中提取
        social_media_data = self._extract_social_media_data()
        case_examples = self._load_case_library()
        authoritative_sources = self._extract_authoritative_sources()
        
        print(f"  ✅ 已使用客户真实资料：{len(materials.get('core_selling_points', []))}个卖点，{len(materials.get('case_studies', []))}个案例")
        
        return {
            "client_profile": client_profile,
            "selling_points": selling_points,
            "competitor_analysis": "{}",  # 竞品分析仍需要从诊断数据中推理
            "social_media_data": social_media_data,
            "platform_data": self.diagnosis_data.get("platform_data", {}),
            "content_insights": self.diagnosis_data.get("content_insights", {}),
            "asr_transcripts": self.diagnosis_data.get("asr_transcripts", []),
            "case_examples": case_examples,
            "authoritative_sources": authoritative_sources,
            "has_client_materials": True  # 标记使用了客户资料
        }

    
    def _extract_social_media_data(self) -> Dict[str, Any]:
        """V4新增：提取社媒真实数据用于文章写作"""
        platform_data = self.diagnosis_data.get("platform_data", {})
        
        # 提取抖音热门视频
        douyin_top = []
        douyin_data = platform_data.get("douyin", {})
        if isinstance(douyin_data, dict):
            videos = douyin_data.get("top20", []) or douyin_data.get("videos", [])
            for v in videos[:5]:  # TOP 5
                douyin_top.append({
                    "title": v.get("desc", "")[:50],
                    "author": v.get("author", {}).get("nickname", ""),
                    "likes": v.get("stats", {}).get("digg", 0),
                    "url": v.get("url", "")
                })
        
        # 提取小红书热门笔记
        xhs_top = []
        xhs_data = platform_data.get("xiaohongshu", {})
        if isinstance(xhs_data, dict):
            notes = xhs_data.get("top20", []) or xhs_data.get("notes", [])
            for n in notes[:5]:  # TOP 5
                if n is None:
                    continue
                title = n.get("title") or ""  # 处理title为None的情况
                xhs_top.append({
                    "title": title[:50] if title else "",
                    "author": (n.get("user") or {}).get("nickname", ""),
                    "likes": (n.get("stats") or {}).get("likes", 0)
                })
        
        # 提取ASR转写结果
        asr_highlights = []
        asr_list = self.diagnosis_data.get("asr_transcripts", [])
        for asr in asr_list:
            # 确保asr是字典类型
            if not isinstance(asr, dict):
                continue
            if asr.get("status") == "success":
                asr_highlights.append({
                    "title": asr.get("title", ""),
                    "author": asr.get("author", ""),
                    "text_preview": asr.get("text", "")[:200],
                    "likes": asr.get("digg_count", 0)
                })
        
        return {
            "douyin_top_videos": douyin_top,
            "xiaohongshu_top_notes": xhs_top,
            "asr_highlights": asr_highlights,
            "has_social_data": bool(douyin_top or xhs_top or asr_highlights)
        }
    
    async def _distill_client_profile(self) -> str:
        """Stage 1: 提取客户画像 - V8增强：使用展平后的完整数据"""
        import json
        
        # 提取平台数据摘要
        platform_summary = ""
        platform_data = self.diagnosis_data.get('platform_data', {})
        if platform_data:
            douyin = platform_data.get('douyin', {})
            xhs = platform_data.get('xiaohongshu', {})
            if douyin.get('videos'):
                platform_summary += f"抖音视频数: {len(douyin.get('videos', []))}\n"
            if xhs.get('notes'):
                platform_summary += f"小红书笔记数: {len(xhs.get('notes', []))}\n"
        
        # 提取AI可见度数据
        ai_data = self.diagnosis_data.get('ai_visibility', {})
        ai_summary = ""
        if ai_data:
            ai_summary = f"AI引擎测试次数: {ai_data.get('total_tests', 0)}, 被引用率: {ai_data.get('mention_rate', 0):.1%}"
        
        # 提取行业分析
        industry_analysis = self.diagnosis_data.get('industry_analysis', {})
        industry_summary = ""
        if isinstance(industry_analysis, dict):
            industry_summary = industry_analysis.get('summary', '')[:500] if industry_analysis.get('summary') else ''
        
        user_data = f"""# 诊断数据

## 基础信息
- 品牌名：{self.diagnosis_data.get('brand_name', '未知')}
- 行业：{self.diagnosis_data.get('industry', '未知')}

## 业务背景
{self.diagnosis_data.get('business_context', '无业务背景信息')}

## 平台数据
{platform_summary or '暂无平台数据'}

## AI可见度
{ai_summary or '暂无AI可见度数据'}

## 行业分析
{industry_summary or '暂无行业分析数据'}
"""
        return await call_llm(CLIENT_PROFILE_PROMPT, user_data)
    
    async def _distill_selling_points(self) -> str:
        """Stage 2: 提取核心卖点（结合知识库 + 网页/学术数据 + 客户资料）"""
        # 读取知识库获取行业专业定义
        knowledge_content = self._load_knowledge_base()
        
        # 提取权威数据源
        authoritative_data = self._extract_authoritative_sources()
        
        # ✅ V7新增：融合客户上传的真实资料
        client_materials_text = ""
        if self.client_materials:
            import json
            cm = self.client_materials
            client_materials_text = f"""
# 📋 客户上传的真实资料（优先使用！）
- 公司简介：{cm.get('company_intro', '')}
- 一句话价值主张：{cm.get('unique_value', '')}
- 服务方法论：{cm.get('methodology', '')}
- 核心卖点：{json.dumps(cm.get('core_selling_points', []), ensure_ascii=False)}
- 成功案例：{json.dumps(cm.get('case_studies', []), ensure_ascii=False)}
- 客户好评：{json.dumps(cm.get('testimonials', []), ensure_ascii=False)}
"""
        
        user_data = f"""
# 客户画像
{self.client_profile}

# 业务信息
{self.diagnosis_data.get('business_context', '无业务背景信息')}

# 内容洞察
{self.diagnosis_data.get('content_insights', '无内容洞察')}
{client_materials_text}
# 📚 行业知识库参考（请结合此内容理解行业术语的正确定义）
{knowledge_content}

# 🌐 权威数据源（网页搜索 + 学术研究，可在文章中引用）
{authoritative_data}
"""
        return await call_llm(SELLING_POINTS_PROMPT, user_data)
    
    def _extract_authoritative_sources(self) -> str:
        """从 cache 中提取权威数据源（网页、学术）"""
        result = []
        
        # 1. 网页搜索数据
        web_data = self.cache_data.get('web_search', {}).get('data', {})
        if web_data:
            citations = web_data.get('citations', [])
            if citations:
                result.append("## 网页权威引用（来自秘塔搜索）")
                for i, c in enumerate(citations[:8]):  # TOP 8
                    source = c.get('source', 'N/A')
                    title = c.get('title', 'N/A')[:60]
                    result.append(f"- [{source}] {title}")
        
        # 2. 学术研究数据
        scholar_data = self.cache_data.get('scholar_search', {}).get('data', {})
        if scholar_data:
            papers = scholar_data.get('papers', [])
            if papers:
                result.append("\n## 学术研究引用")
                for i, p in enumerate(papers[:5]):  # TOP 5
                    title = p.get('title', 'N/A')[:60]
                    result.append(f"- {title}")
            
            # 学术分析摘要
            analysis = scholar_data.get('academic_analysis', {})
            if analysis:
                # 处理dict或string类型
                if isinstance(analysis, dict):
                    analysis_text = str(analysis.get('summary', analysis))[:500]
                else:
                    analysis_text = str(analysis)[:500]
                result.append(f"\n### 学术分析摘要\n{analysis_text}")
        
        # 3. 行业分析洞察
        industry_data = self.cache_data.get('industry_analysis', {}).get('data', {})
        if industry_data:
            insights = industry_data.get('insights', [])
            if insights:
                result.append("\n## 行业洞察")
                for insight in insights[:3]:
                    result.append(f"- {insight}")
        
        if not result:
            return "（无权威数据源，请使用合理可信的表述）"
        
        return "\n".join(result)
    
    def _load_knowledge_base(self) -> str:
        """加载客户专属知识库（V10升级：使用unified_knowledge RAG检索）"""
        
        # ========================================
        # V10升级：优先使用客户专属知识库（RAG检索）
        # ========================================
        if self.brand_id:
            try:
                import asyncio
                from tools.unified_knowledge import get_unified_rag
                rag = get_unified_rag()
                
                # 构建检索query：行业 + 品牌 + 关键业务
                brand_name = self.diagnosis_data.get('brand_name', '')
                industry = self.diagnosis_data.get('industry', '')
                search_query = f"{industry} {brand_name} 核心业务 卖点 优势"
                
                print(f"  📚 检索客户专属知识库 (brand_id={self.brand_id}): {search_query[:50]}...")
                
                # ✅ 修复：使用asyncio.create_task在已存在的event loop中运行
                async def _async_retrieve():
                    return await rag.retrieve(
                        query=search_query,
                        brand_id=self.brand_id,
                        top_k=8,
                        use_client=True,
                        use_role=False
                    )
                
                # 获取当前事件循环
                try:
                    loop = asyncio.get_running_loop()
                    # 在已存在的event loop中，创建task并等待
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        future = executor.submit(asyncio.run, _async_retrieve())
                        kb_results = future.result(timeout=30)
                except RuntimeError:
                    # 没有运行中的loop，直接用asyncio.run
                    kb_results = asyncio.run(_async_retrieve())
                
                # ✅ 修复：kb_results是List不是Dict
                if kb_results and len(kb_results) > 0:
                    print(f"  ✅ 检索到 {len(kb_results)} 条相关知识")
                    knowledge_text = "\n\n".join([
                        f"### {r.get('source', '知识点')}\n{r['content']}"
                        for r in kb_results[:8]
                    ])
                    return knowledge_text
                else:
                    print("  ⚠️ 客户知识库为空，使用通用知识库")
            except Exception as e:
                print(f"  ⚠️ 客户知识库检索失败: {e}，fallback到通用知识库")
        
        # ========================================
        # Fallback：返回通用写作指导（不注入任何特定行业知识）
        # 🔥 FIX: 之前硬编码加载"GEO优化规则库"导致非GEO行业客户
        #    的文章也被注入GEO内容（如"驰鲸"出海业务写出GEO文章）
        # ========================================
        industry = self.diagnosis_data.get("industry", "")
        brand_name = self.diagnosis_data.get("brand_name", "")
        return (
            f"（暂无{brand_name}的专属知识库，请基于{industry}行业通用知识撰写。\n"
            f"要求：围绕客户的实际业务 '{industry}' 展开，"
            f"引用该行业的权威数据和报告，切勿引用无关行业的内容。）"
        )
    
    def _load_case_library(self) -> str:
        """加载案例库 - 根据客户行业智能筛选范文
        
        🔥 FIX V2: 之前只检查文件名中的GEO关键词，但案例库文件名大多不含这些词。
        现在同时检查文件名和文件内容前500字中的GEO标识词，
        彻底阻止非GEO客户被GEO风格污染。
        """
        case_dir = os.path.join(KNOWLEDGE_BASE_DIR, "案例库")
        if not os.path.exists(case_dir):
            # 🔴 WO_272:原来这里安静地 return "" —— 07-12 起目录名是乱码,范文库一直是空的。
            report_missing(case_dir, what="Case library dir", logger=logger)
            return ""
        
        brand_name = self.diagnosis_data.get('brand_name', '')
        industry = self.diagnosis_data.get('industry', '')
        
        # GEO专属标识词（文件名 + 内容均检查）
        geo_keywords = ['GEO', '全域上榜', 'OmniRank', '潮树渔', '生成式引擎优化', 'CSYGEO', 'OGI']
        is_geo_client = 'GEO' in industry.upper() or 'geo' in industry.lower()
        
        examples = []
        priority_examples = []  # 品牌匹配的优先范文
        skipped_count = 0
        
        for filename in os.listdir(case_dir):
            if not filename.endswith('.md'):
                continue
            
            filepath = os.path.join(case_dir, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
            except:
                continue
            
            # 🔥 增强过滤：检查文件名 + 内容前500字中是否有GEO标识词
            if not is_geo_client:
                check_text = filename + content[:500]
                if any(kw in check_text for kw in geo_keywords):
                    skipped_count += 1
                    continue
            
            entry = f"## 参考范例：{filename}\n{content[:2000]}..."
            
            # 品牌名匹配的范文优先
            if brand_name and brand_name in filename:
                priority_examples.append(entry)
            else:
                examples.append(entry)
        
        if skipped_count > 0:
            print(f"    ℹ️ 案例库：跳过 {skipped_count} 个GEO专属范文（当前客户行业: {industry}）")
        
        # 优先使用品牌匹配范文，不足则用通用范文补齐
        final = priority_examples[:2]
        if len(final) < 2:
            final.extend(examples[:2 - len(final)])
        
        if final:
            return "\n\n【📚 高质量文章范例（请模仿此风格）】\n" + "\n\n".join(final)
        return ""
    
    async def _distill_competitors(self) -> str:
        """Stage 3: 分析竞品格局 - V8增强：更清晰展示竞品列表"""
        import json
        
        # 格式化竞品数据
        competitor_data = self.diagnosis_data.get('competitor_data', {})
        competitor_list = []
        if isinstance(competitor_data, dict):
            competitors = competitor_data.get('competitors', [])
            for comp in competitors[:10]:
                name = comp.get('name', comp.get('nickname', ''))
                platform = comp.get('platform', '')
                engagement = comp.get('total_engagement', 0)
                if name:
                    competitor_list.append(f"- {name} ({platform}, 互动量:{engagement})")
        
        competitor_text = '\n'.join(competitor_list) if competitor_list else '暂无竞品数据'
        
        # 格式化平台数据摘要
        platform_data = self.diagnosis_data.get('platform_data', {})
        platform_text = ""
        if platform_data:
            douyin = platform_data.get('douyin', {})
            xhs = platform_data.get('xiaohongshu', {})
            douyin_videos = douyin.get('videos', [])[:3]
            xhs_notes = xhs.get('notes', [])[:3]
            
            if douyin_videos:
                platform_text += "抖音热门视频:\n"
                for v in douyin_videos:
                    title = v.get('desc', v.get('title', ''))[:50]
                    platform_text += f"  - {title}\n"
            if xhs_notes:
                platform_text += "小红书热门笔记:\n"
                for n in xhs_notes:
                    title = n.get('desc', n.get('title', ''))[:50]
                    platform_text += f"  - {title}\n"
        
        user_data = f"""# 客户信息
- 品牌名：{self.diagnosis_data.get('brand_name', '未知')}
- 行业：{self.diagnosis_data.get('industry', '未知')}

# 客户画像
{self.client_profile}

# 核心卖点
{self.selling_points}

# 识别到的竞品列表
{competitor_text}

# 社媒热门内容
{platform_text or '暂无社媒数据'}
"""
        return await call_llm(COMPETITOR_ANALYSIS_PROMPT, user_data)


# 测试
if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv
    load_dotenv()
    
    # 模拟诊断数据
    test_data = {
        "brand_name": "驰鲸科技",
        "industry": "TikTok B2B出海代运营",
        "geo_score": {"total_score": 18, "level": "空白"},
        "business_context": "专注TikTok B2B出海代运营，服务工厂出海客户",
        "competitor_data": {"competitors": [{"name": "卧兔网络"}, {"name": "PandaMobo"}]}
    }
    
    async def test():
        pipeline = DistillerPipeline(test_data)
        result = await pipeline.run()
        print("\n=== 蒸馏结果 ===")
        print(result)
    
    asyncio.run(test())
