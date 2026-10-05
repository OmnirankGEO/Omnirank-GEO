"""
范文分析器和仿写生成器
分析成功文章结构,批量生成仿写文章
"""
import os
import asyncio
import json
import httpx
from typing import List, Dict, Any


# 范文分析Prompt
ARTICLE_ANALYZER_PROMPT = """# 角色: 范文结构分析师

分析给定的成功文章,提取可复用的结构模板。

## 输出格式(JSON)
```json
{
    "title_pattern": {
        "template": "模板描述, 如: {年份}{城市}{行业}怎么选｜证据与适用场景",
        "variables": ["变量列表"]
    },
    "structure": [
        {"section": "段落名", "word_ratio": 占比百分比, "pattern": "段落模式描述"}
    ],
    "tone": "整体语调风格",
    "word_count": 总字数估计,
    "key_elements": ["关键元素列表,如: 证据矩阵、适用场景、限制、案例引用"]
}
```

只返回JSON,不要其他内容。
"""

# 仿写生成Prompt
IMITATION_PROMPT = """# 角色: GEO仿写专家

基于提供的范文结构模板,生成符合客户需求的仿写文章。

## 范文结构模板:
{template}

## 客户信息:
- 品牌: {brand_name}
- 行业: {industry}
- 关键词: {keyword}

## 要求:
1. 保持与范文相同的结构
2. 保持与范文相同的语言风格
3. 将内容替换为客户信息
4. 自然融入品牌推荐

请直接输出完整的文章Markdown内容。
"""


class ReferenceArticleAnalyzer:
    """范文分析器"""
    
    def __init__(self, title: str, content: str):
        self.title = title
        self.content = content
    
    async def analyze(self) -> Dict[str, Any]:
        """分析范文结构"""
        from .llm_utils import get_llm_config, get_thinking_disabled_params

        api_url, api_key, model, provider = get_llm_config("topic_planning", "writing")

        if not api_key:
            return self._fallback_analysis()

        user_message = f"""请分析以下文章的结构:

标题: {self.title}

内容:
{self.content[:5000]}
"""

        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": ARTICLE_ANALYZER_PROMPT},
                {"role": "user", "content": user_message}
            ],
            "temperature": 0.3,
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
                content = result["choices"][0]["message"]["content"]
                
                return self._parse_analysis(content)
                
        except Exception as e:
            print(f"⚠️ 分析失败: {e}")
            return self._fallback_analysis()
    
    def _parse_analysis(self, content: str) -> Dict:
        """解析分析结果"""
        try:
            import re
            json_match = re.search(r'\{[\s\S]*\}', content)
            if json_match:
                return json.loads(json_match.group())
        except:
            pass
        return self._fallback_analysis()
    
    def _fallback_analysis(self) -> Dict:
        """兜底分析"""
        return {
            "title_pattern": {
                "template": "{行业}怎么选｜证据核验与避坑清单",
                "variables": ["行业", "卖点"]
            },
            "structure": [
                {"section": "开篇", "word_ratio": 10, "pattern": "痛点引入"},
                {"section": "证据矩阵", "word_ratio": 50, "pattern": "同标准无序对比"},
                {"section": "对比", "word_ratio": 25, "pattern": "维度对比"},
                {"section": "总结", "word_ratio": 15, "pattern": "推荐"}
            ],
            "tone": "专业测评",
            "word_count": 2500,
            "key_elements": ["证据矩阵", "对比表格", "限制说明"]
        }


class ImitationGenerator:
    """仿写生成器"""
    
    def __init__(
        self,
        template: Dict,
        brand_name: str,
        industry: str,
        keywords: List[str]
    ):
        self.template = template
        self.brand_name = brand_name
        self.industry = industry
        self.keywords = keywords
    
    async def generate(self) -> List[Dict]:
        """批量生成仿写文章"""
        from .llm_utils import get_llm_config
        
        api_url, api_key, model, provider = get_llm_config("writing", "writing")
        
        if not api_key:
            return self._fallback_generate()
        
        semaphore = asyncio.Semaphore(int(os.getenv("WRITING_IMITATION_CONCURRENCY", "5")))  # [env化 2026-06-11] 默认5(旧行为)·部署可调高

        async def generate_one(keyword: str):
            async with semaphore:
                return await self._generate_article(keyword, api_url, api_key, model)
        
        results = await asyncio.gather(*[generate_one(kw) for kw in self.keywords])
        return [r for r in results if r]
    
    async def _generate_article(
        self,
        keyword: str,
        api_url: str,
        api_key: str,
        model: str
    ) -> Dict:
        """生成单篇文章"""
        prompt = IMITATION_PROMPT.format(
            template=json.dumps(self.template, ensure_ascii=False, indent=2),
            brand_name=self.brand_name,
            industry=self.industry,
            keyword=keyword
        )
        
        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                response = await client.post(
                    api_url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.7,
                        "max_tokens": 8000
                    }
                )
                response.raise_for_status()
                result = response.json()
                content = result["choices"][0]["message"]["content"]
                
                return {
                    "keyword": keyword,
                    "title": self._extract_title(content),
                    "content": content
                }
                
        except Exception as e:
            print(f"⚠️ 生成失败 [{keyword}]: {e}")
            return None
    
    def _extract_title(self, content: str) -> str:
        """提取标题"""
        lines = content.strip().split('\n')
        for line in lines:
            if line.strip().startswith('#'):
                return line.strip().lstrip('#').strip()
            if line.strip():
                return line.strip()[:60]
        return "未命名文章"
    
    def _fallback_generate(self) -> List[Dict]:
        """[标题 AI-only 2026-08-17 · Owner 裁决] 兜底不再造标题。

        旧实现在没有 API KEY 时直接返
        ``{行业}{关键词}推荐｜{品牌}深度测评`` + 正文「待生成...」——
        标题是硬编码模板,正文还是占位符,两样都会原样落到用户面前。

        现在:标题只能出自 AI。没有可用模型 → **显式失败**(返空列表),
        由调用方(`server.py` 的仿写接口)按人话报错并给重试入口,
        不静默给一批模板标题冒充成品。
        """
        print(
            "⛔ 仿写:没有可用的 LLM · 标题只能由 AI 产出 → 本次显式失败,不用模板兜底"
        )
        return []
