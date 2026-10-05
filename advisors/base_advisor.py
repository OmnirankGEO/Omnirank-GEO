"""
顾问基类
基于AgentScope RAG实现带知识库的顾问Agent
"""

import os
import sys
import json
import asyncio
from typing import Optional, Any
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class BaseAdvisor:
    """
    顾问基类 - 带RAG知识库的角色扮演Agent
    
    特点：
    1. 可喂入大量资料（PDF、TXT、MD）
    2. 对话时自动检索相关知识
    3. 支持角色扮演（如乔布斯、马斯克）
    4. 支持5个API提供商：DASHSCOPE、DEEPSEEK、OPENROUTER、KIMI、DOUBAO
    """
    
    # API提供商配置
    API_PROVIDERS = {
        "dashscope": {
            "env_key": "DASHSCOPE_API_KEY",
            "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "default_model": "qwen3.7-max",
        },
        "deepseek": {
            "env_key": "DEEPSEEK_API_KEY",
            "api_base": "https://api.deepseek.com/v1/chat/completions",
            "default_model": "deepseek-chat",
        },
        "openrouter": {
            "env_key": "OPENROUTER_API_KEY",
            "api_base": "https://openrouter.ai/api/v1/chat/completions",
            "default_model": "anthropic/claude-sonnet-4.6",
        },
        "kimi": {
            "env_key": "KIMI_API_KEY",
            "api_base": "https://api.moonshot.cn/v1/chat/completions",
            # [census ⑦a 2026-08-23] 原来这里是 "qwen3.7-max" —— **Moonshot 端点配 Qwen 模型名**,
            #   而上一行注释还自称 "default 仍为 qwen3-max",三个名字互不相同(三重错位)。
            #   可达性查清了(工单要求先验证再动):`config["default_model"]` 只在
            #   base_advisor 的 `self.model_name or config["default_model"]` 里被读到,
            #   而两条已知构造路径都保证 model_name 非空 ——
            #     · advisors/advisor_registry.py 兜底 "qwen3.7-max";
            #     · api/advisor_api.py 兜底 env 或 "deepseek-v4-flash"。
            #   所以它**当前读不到**;但"读不到"靠的是调用方兜底,不是结构保证:
            #   任何人直接 BaseAdvisor(api_provider="kimi") 不传 model_name 就会走到这里,
            #   然后把 Qwen 模型名发给 api.moonshot.cn → 400。
            #   既然改成端点配对的值零风险、留着是颗定时炸弹,按端点配对修:
            #   kimi-k2.6 是本仓 kimi provider 的现役模型(services/monitoring_lineage.py
            #   的 _PLATFORM_CONTRACT["kimi"]),且 PRICING_TABLE 里有精确行。
            # 注意: provider='kimi' 被选用时,调用方必须传 temperature=0.6(Kimi K2.6 强制限制)
            "default_model": "kimi-k2.6",
        },
        "doubao": {
            "env_key": "DOUBAO_API_KEY",
            "api_base": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
            "default_model": "doubao-seed-2-0-pro-260215",
        },
    }
    
    def __init__(
        self,
        advisor_id: str,
        name: str,
        base_prompt: str,
        avatar: str = "👤",
        description: str = "",
        # LLM配置
        api_provider: str = "dashscope",    # API提供商
        model_name: str = "qwen3.7-max",      # 可自定义模型名称
        enable_web_search: bool = False,    # 是否开启联网搜索
        temperature: float = 0.7,
        max_tokens: int = 4000,
        # 知识库
        knowledge_path: str = None,
        brand_id: int = None,
    ):
        self.advisor_id = advisor_id
        self.name = name
        self.avatar = avatar
        self.description = description

        # 自动附加方法论到 base_prompt（固定注入，不依赖 RAG）
        methodology_text = ""
        try:
            methodology_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "advisor_methodology.json")
            if os.path.exists(methodology_path):
                with open(methodology_path, "r", encoding="utf-8") as f:
                    methodologies = json.load(f)
                m = methodologies.get(advisor_id, methodologies.get("_default", {}))
                methodology_text = m.get("methodology", "")
        except Exception:
            pass

        if methodology_text:
            self.base_prompt = f"{base_prompt}\n\n## 核心方法论（每次生成都遵循）\n{methodology_text}"
        else:
            self.base_prompt = base_prompt
        
        # LLM配置
        self.api_provider = api_provider
        self.model_name = model_name
        self.enable_web_search = enable_web_search
        self.temperature = temperature
        self.max_tokens = max_tokens
        
        self.brand_id = brand_id
        
        # 知识库路径
        if knowledge_path:
            self.knowledge_path = Path(knowledge_path)
        else:
            self.knowledge_path = Path(f"data/advisors/{advisor_id}/knowledge")
        
        # 确保目录存在
        self.knowledge_path.mkdir(parents=True, exist_ok=True)
        
        # 文档存储
        self.documents = []
        self.knowledge_index = None
        
    def get_profile(self) -> dict:
        """获取顾问资料卡"""
        return {
            "id": self.advisor_id,
            "name": self.name,
            "avatar": self.avatar,
            "description": self.description,
            "api_provider": self.api_provider,
            "model_name": self.model_name,
            "enable_web_search": self.enable_web_search,
            "document_count": len(self.documents),
            "knowledge_path": str(self.knowledge_path),
        }
    
    async def add_document(
        self, 
        content: str, 
        filename: str,
        file_type: str = "txt",
        source: str = None
    ) -> dict:
        """
        添加文档到知识库（向量化存储）
        
        Args:
            content: 文档内容
            filename: 文件名
            file_type: 文件类型 (pdf, txt, md, jsonl)
            source: 来源标注
        
        Returns:
            {"success": True, "chunk_count": N}
        """
        from advisors.vectorizer import get_vector_store
        
        # 分块处理
        chunks = self._chunk_text(content, chunk_size=500)
        chunk_dicts = [{"content": c, "chunk_id": i} for i, c in enumerate(chunks)]
        
        # 向量化并存入LanceDB
        vector_store = get_vector_store()
        result = await vector_store.add_chunks(
            advisor_id=self.advisor_id,
            chunks=chunk_dicts,
            filename=filename,
            source=source or filename,
        )
        
        if not result.get("success"):
            return result
        
        # 同时保存到本地JSON（备份）
        doc_info = {
            "filename": filename,
            "file_type": file_type,
            "chunk_count": len(chunks),
            "chunks": chunks,
        }
        self.documents.append(doc_info)
        
        doc_path = self.knowledge_path / f"{filename}.json"
        with open(doc_path, "w", encoding="utf-8") as f:
            json.dump(doc_info, f, ensure_ascii=False, indent=2)
        
        return {
            "success": True,
            "chunk_count": result.get("chunk_count", len(chunks)),
            "filename": filename,
        }
    
    def _chunk_text(self, text: str, chunk_size: int = 800, overlap: int = 100) -> list[str]:
        """
        将文本智能分块（语义分块：按标题 > 段落 > 句子）
        
        Args:
            text: 原始文本
            chunk_size: 每块最大字符数
            overlap: 块之间的重叠字符数（保留上下文）
        """
        if not text or len(text) == 0:
            return []
        
        text = text.replace('\r\n', '\n').strip()
        
        # 如果文本短于chunk_size，直接返回
        if len(text) <= chunk_size:
            return [text] if text else []
        
        chunks = []
        
        # 1. 优先按Markdown标题分割（# ## ### 开头的行）
        import re
        sections = re.split(r'\n(?=#+ )', text)
        
        for section in sections:
            section = section.strip()
            if not section:
                continue
            
            # 如果段落短于chunk_size，直接添加
            if len(section) <= chunk_size:
                chunks.append(section)
                continue
            
            # 2. 长段落按双换行分割
            paragraphs = section.split('\n\n')
            current_chunk = ""
            
            for para in paragraphs:
                para = para.strip()
                if not para:
                    continue
                
                # 如果段落本身超过chunk_size，按句子分割
                if len(para) > chunk_size:
                    # 先保存当前累积的chunk
                    if current_chunk:
                        chunks.append(current_chunk.strip())
                        current_chunk = ""
                    
                    # 3. 按句子分割（中英文句号、问号、感叹号）
                    sentences = re.split(r'([。！？.!?])', para)
                    sentence_chunk = ""
                    for i in range(0, len(sentences), 2):
                        sentence = sentences[i]
                        # 加上标点
                        if i + 1 < len(sentences):
                            sentence += sentences[i + 1]
                        
                        if len(sentence_chunk) + len(sentence) <= chunk_size:
                            sentence_chunk += sentence
                        else:
                            if sentence_chunk:
                                chunks.append(sentence_chunk.strip())
                            sentence_chunk = sentence
                    
                    if sentence_chunk.strip():
                        chunks.append(sentence_chunk.strip())
                        
                elif len(current_chunk) + len(para) + 2 <= chunk_size:
                    # 可以加入当前块
                    current_chunk += para + "\n\n"
                else:
                    # 当前块已满，保存并开始新块
                    if current_chunk:
                        chunks.append(current_chunk.strip())
                    current_chunk = para + "\n\n"
            
            # 保存最后一块
            if current_chunk.strip():
                chunks.append(current_chunk.strip())
        
        return chunks
    
    async def _rebuild_index(self):
        """重建知识索引（简单版本，后续可接入向量数据库）"""
        # 当前使用简单的关键词索引
        # 后续可替换为 LanceDB / Chroma / FAISS
        self.knowledge_index = []
        
        for doc in self.documents:
            for i, chunk in enumerate(doc.get("chunks", [])):
                self.knowledge_index.append({
                    "filename": doc["filename"],
                    "chunk_id": i,
                    "content": chunk,
                    "keywords": self._extract_keywords(chunk),
                })
    
    def _extract_keywords(self, text: str) -> list[str]:
        """提取关键词（简单版本）"""
        # 简单的关键词提取，后续可用更复杂的NLP
        import re
        words = re.findall(r'[\u4e00-\u9fa5a-zA-Z]+', text)
        # 过滤短词
        return [w for w in words if len(w) > 1]
    
    async def retrieve_knowledge(
        self,
        query: str,
        top_k: int = 5
    ) -> list[dict]:
        """
        检索相关知识 — 优先走 hybrid_retriever（pgvector+BM25+Reranker），
        如果 knowledge_chunks 表里没有该顾问的数据，降级到 LanceDB。
        """
        # 优先：hybrid_retriever（新架构）
        try:
            from advisors.hybrid_retriever import search_knowledge
            results = await search_knowledge(
                query=query,
                advisor_id=self.advisor_id,
                top_k=top_k,
            )
            if results:
                print(f"[BaseAdvisor] hybrid检索命中 {self.advisor_id}: {len(results)} 条 (query: {query[:30]})")
                return results
        except Exception as e:
            print(f"[BaseAdvisor] hybrid检索失败，降级LanceDB: {e}")

        # 降级：LanceDB（旧架构，兼容未迁移的顾问）
        if getattr(self, '_needs_index_rebuild', False):
            await self._rebuild_index()
            self._needs_index_rebuild = False

        try:
            from advisors.vectorizer import get_vector_store
            vector_store = get_vector_store()
            results = await vector_store.search(
                advisor_id=self.advisor_id,
                query=query,
                top_k=top_k,
            )
            return results
        except Exception as e:
            print(f"[BaseAdvisor] LanceDB检索也失败({self.advisor_id}): {e}")
            return []
    
    async def chat(
        self, 
        message: str, 
        context: str = ""
    ) -> dict:
        """
        与顾问对话
        
        Args:
            message: 用户消息
            context: 可选的额外上下文
        
        Returns:
            {"success": True, "response": "..."}
        """
        # 检索相关知识
        retrieved = await self.retrieve_knowledge(message, top_k=5)
        
        # 构建增强提示 - 限制知识库上下文长度（避免超过模型258K限制）
        knowledge_context = ""
        max_knowledge_chars = 30000  # 知识库最多30K字符
        if retrieved:
            knowledge_context = "# 相关资料\n\n"
            remaining_chars = max_knowledge_chars - len(knowledge_context)
            for i, doc in enumerate(retrieved, 1):
                source_name = doc.get("filename") or doc.get("source_file") or doc.get("title") or "知识库"
                doc_text = f"【资料{i}】来自{source_name}：\n{doc.get('content', '')}\n\n"
                if len(doc_text) > remaining_chars:
                    # 截断当前文档以适应限制
                    doc_text = doc_text[:remaining_chars] + "\n...(已截断)\n"
                    knowledge_context += doc_text
                    break
                knowledge_context += doc_text
                remaining_chars -= len(doc_text)
                if remaining_chars <= 0:
                    break
        
        # 构建完整提示
        full_prompt = f"""{self.base_prompt}

{knowledge_context}

{context}

用户问题：{message}

请以{self.name}的身份和思维方式回答。"""
        
        # 调用LLM
        response = await self._call_llm(full_prompt)
        
        return {
            "success": True,
            "response": response,
            "retrieved_docs": len(retrieved),
        }
    
    async def _call_llm(self, prompt: str) -> str:
        """调用LLM获取回复"""
        import httpx
        from tools.llm_call_tracker import llm_track, usage_from_response_payload
        
        # 获取模型配置
        config = self._get_model_config()
        
        if not config["api_key"]:
            return f"[错误: 未配置 {self.api_provider.upper()} API密钥]"
        
        # 构建请求头
        headers = {
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
        }
        
        # OpenRouter需要额外header
        if self.api_provider == "openrouter":
            headers["HTTP-Referer"] = "http://localhost"
            headers["X-Title"] = "OmniRank AI"
        
        # 构建请求体
        body = {
            "model": config["model_name"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.api_provider == "deepseek" and getattr(self, "thinking", None) in ("enabled", "disabled"):
            body["thinking"] = {"type": getattr(self, "thinking")}
        
        # Doubao需要处理endpoint
        api_base = config["api_base"]
        if self.api_provider == "doubao":
            import os
            endpoint_id = os.getenv("DOUBAO_ENDPOINT_ID", "")
            if endpoint_id:
                body["model"] = endpoint_id
        
        # [CTO-15.23 2026-05-09] Kimi K2.6 升级 · 硬约束保留(2.5 实测 · 2.6 待 Moonshot 官方确认)
        # temperature 只接受 0.6(其他值返 400) · thinking 必须关闭(tool_calls 缺 reasoning_content 返 400)
        if self.api_provider == "kimi":
            body["temperature"] = 0.6  # ⚠️ Kimi K2.6 硬约束 · 不要改
            body["thinking"] = {"type": "disabled"}

        # 联网搜索支持（除DeepSeek外都支持）
        if self.api_provider != "deepseek" and self.enable_web_search:
            if self.api_provider == "kimi":
                body["tools"] = [{"type": "builtin_function", "function": {"name": "$web_search"}}]
            elif self.api_provider == "dashscope":
                body["enable_search"] = True
            elif self.api_provider == "openrouter":
                # OpenRouter部分模型支持
                pass
            elif self.api_provider == "doubao":
                body["stream"] = False  # doubao web search需要配置
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    async with llm_track(
                        "advisor",
                        self.api_provider,
                        model=str(body.get("model") or config["model_name"]),
                        brand_id=getattr(self, "brand_id", None),
                        metadata={
                            "advisor_id": getattr(self, "advisor_id", None),
                            "attempt": attempt + 1,
                            "web_search": self.enable_web_search,
                        },
                    ) as tracker:
                        response = await client.post(
                            api_base,
                            headers=headers,
                            json=body,
                        )

                        if response.status_code == 200:
                            data = response.json()
                            input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data)
                            tracker.record(
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                cached_tokens=cached_tokens,
                                success=True,
                            )
                            return data["choices"][0]["message"]["content"]
                        elif response.status_code in (429, 503) and attempt < max_retries - 1:
                            tracker.record(success=False, error_msg=f"HTTP {response.status_code}: {response.text[:200]}")
                            # 限流/过载：尝试切备用 Key 再重试
                            backup_key = self._get_backup_key()
                            if backup_key and headers["Authorization"] != f"Bearer {backup_key}":
                                headers["Authorization"] = f"Bearer {backup_key}"
                                print(f"[BaseAdvisor] {response.status_code} 限流，切换备用Key重试")
                            else:
                                wait = (attempt + 1) * 3
                                print(f"[BaseAdvisor] {response.status_code} 限流，{wait}s后重试 (第{attempt+1}次)")
                                await asyncio.sleep(wait)
                            continue
                        else:
                            error_detail = response.text[:200] if response.text else ""
                            tracker.record(success=False, error_msg=f"HTTP {response.status_code}: {error_detail}")
                            return f"[调用失败: {response.status_code}] {error_detail}"
            except (httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                if attempt < max_retries - 1:
                    # 超时也尝试切备用 Key
                    backup_key = self._get_backup_key()
                    if backup_key and headers["Authorization"] != f"Bearer {backup_key}":
                        headers["Authorization"] = f"Bearer {backup_key}"
                        print(f"[BaseAdvisor] 超时，切换备用Key重试: {e}")
                    else:
                        wait = (attempt + 1) * 3
                        print(f"[BaseAdvisor] 超时，{wait}s后重试 (第{attempt+1}次): {e}")
                        await asyncio.sleep(wait)
                    continue
                return f"[错误: 超时] {str(e)}"
            except Exception as e:
                return f"[错误: {str(e)}]"
        return "[错误: 重试次数用尽]"

    async def _call_llm_stream(self, prompt: str):
        """Streaming version of _call_llm — yields content chunks"""
        import httpx
        from tools.llm_call_tracker import llm_track

        config = self._get_model_config()
        if not config["api_key"]:
            yield {"type": "error", "text": f"未配置 {self.api_provider.upper()} API密钥"}
            return

        headers = {
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
        }

        if self.api_provider == "openrouter":
            headers["HTTP-Referer"] = "http://localhost"
            headers["X-Title"] = "OmniRank AI"

        body = {
            "model": config["model_name"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        if self.api_provider == "deepseek" and getattr(self, "thinking", None) in ("enabled", "disabled"):
            body["thinking"] = {"type": getattr(self, "thinking")}

        api_base = config["api_base"]
        if self.api_provider == "doubao":
            import os
            endpoint_id = os.getenv("DOUBAO_ENDPOINT_ID", "")
            if endpoint_id:
                body["model"] = endpoint_id

        if self.api_provider == "kimi":
            body["temperature"] = 1
            body["thinking"] = {"type": "disabled"}

        if self.api_provider != "deepseek" and self.enable_web_search:
            if self.api_provider == "kimi":
                body["tools"] = [{"type": "builtin_function", "function": {"name": "$web_search"}}]
            elif self.api_provider == "dashscope":
                body["enable_search"] = True

        try:
            async with llm_track(
                "advisor_stream",
                self.api_provider,
                model=str(body.get("model") or config["model_name"]),
                brand_id=getattr(self, "brand_id", None),
                metadata={
                    "advisor_id": getattr(self, "advisor_id", None),
                    "web_search": self.enable_web_search,
                },
            ) as tracker:
                output_chars = 0
                async with httpx.AsyncClient(timeout=120) as client:
                    async with client.stream("POST", api_base, headers=headers, json=body) as response:
                        if response.status_code != 200:
                            error_text = ""
                            async for chunk in response.aiter_text():
                                error_text += chunk
                            tracker.record(success=False, error_msg=f"HTTP {response.status_code}: {error_text[:200]}")
                            yield {"type": "error", "text": f"调用失败: {response.status_code} {error_text[:200]}"}
                            return

                        async for line in response.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            data_str = line[6:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                import json as _json
                                chunk = _json.loads(data_str)
                                delta = chunk.get("choices", [{}])[0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    output_chars += len(content)
                                    yield {"type": "content", "text": content}
                            except Exception:
                                continue
                tracker.record(
                    input_tokens=max(1, len(prompt) // 3),
                    output_tokens=max(1, output_chars // 3),
                    success=True,
                )
        except Exception as e:
            yield {"type": "error", "text": f"流式调用失败: {str(e)}"}

    def _get_model_config(self) -> dict:
        """获取模型配置"""
        import os

        # 获取提供商配置
        provider = self.api_provider.lower()
        if provider not in self.API_PROVIDERS:
            provider = "dashscope"  # 默认

        config = self.API_PROVIDERS[provider]
        if provider == "deepseek":
            try:
                from services.llm.deepseek_key_pool import (
                    deepseek_role_for_model,
                    pick_deepseek_api_key,
                )

                role = deepseek_role_for_model(
                    self.model_name or config["default_model"],
                    getattr(self, "thinking", None),
                )
                api_key = pick_deepseek_api_key(role)
            except Exception:
                api_key = os.getenv(config["env_key"], "")
        else:
            api_key = os.getenv(config["env_key"], "")

        return {
            "api_base": config["api_base"],
            "api_key": api_key,
            "model_name": self.model_name or config["default_model"],
        }

    def _get_backup_key(self) -> str:
        """获取备用 API Key（同提供商的第二个 Key，用于限流切换）"""
        import os
        if self.api_provider.lower() == "deepseek":
            try:
                from services.llm.deepseek_key_pool import (
                    deepseek_role_for_model,
                    pick_deepseek_api_key,
                )

                return pick_deepseek_api_key(
                    deepseek_role_for_model(self.model_name, getattr(self, "thinking", None))
                )
            except Exception:
                return ""
        backup_map = {
            "dashscope": "DASHSCOPE_API_KEY_BACKUP",
            "doubao": "DOUBAO_SEED_API_KEY",  # 豆包有两个 key
        }
        env_name = backup_map.get(self.api_provider.lower())
        return os.getenv(env_name, "") if env_name else ""
    
    def load_documents(self):
        """从磁盘加载已有文档"""
        self.documents = []
        
        if not self.knowledge_path.exists():
            return
        
        for doc_file in self.knowledge_path.glob("*.json"):
            try:
                with open(doc_file, "r", encoding="utf-8") as f:
                    doc_info = json.load(f)
                    self.documents.append(doc_info)
            except Exception as e:
                print(f"[Advisor] 加载文档失败: {doc_file}, {e}")
        
        # 重建索引（在有事件循环时执行）
        try:
            loop = asyncio.get_running_loop()
            asyncio.create_task(self._rebuild_index())
        except RuntimeError:
            # 没有运行中的事件循环，标记需要重建索引
            self._needs_index_rebuild = True
    
    async def ask_with_context(
        self,
        question: str,
        company_data: dict = None,
        persona_data: dict = None,
        extra_context: str = "",
        raw_prompt: bool = False,
        skip_rag: bool = False,
    ) -> dict:
        """
        带三要素上下文的问答

        Args:
            question: 用户问题（或 raw_prompt=True 时的完整prompt）
            company_data: 公司信息
            persona_data: IP人设信息
            extra_context: 额外上下文
            raw_prompt: True 时跳过 context_engine 包装，直接使用 question
                        作为完整 prompt（默认仍会注入 RAG 知识检索结果）
            skip_rag: True 时不再重复检索顾问知识库，适合调用方已经完成
                      并行资料收集并把知识写入完整 prompt 的场景。

        Returns:
            {"success": True, "response": "..."}
        """
        from .context_engine import get_context_engine

        # 检索相关知识 — 用业务关键词做查询，不用 prompt 前 200 字（那是规则文本）
        search_query = question[:200]
        if company_data:
            parts = [
                company_data.get("industry", ""),
                company_data.get("business", ""),
                company_data.get("target_users", ""),
            ]
            biz_query = " ".join(p for p in parts if p)
            if biz_query:
                # 从 prompt 里提取选题（通常在"选题："后面）
                import re
                topic_match = re.search(r'选题[：:]\s*(.{5,50})', question)
                topic_text = topic_match.group(1).strip() if topic_match else ""
                search_query = f"{topic_text} {biz_query}".strip()[:200]
        knowledge_results = [] if skip_rag else await self.retrieve_knowledge(search_query, top_k=5)

        # 调试日志：知识库检索结果
        if skip_rag:
            print(f"\n[BaseAdvisor] 跳过重复RAG检索 (顾问: {self.name})")
        else:
            print(f"\n[BaseAdvisor] 知识库检索结果 (顾问: {self.name}):")
            print(f"  - 检索问题: {question[:100]}...")
            print(f"  - 检索到 {len(knowledge_results)} 条相关知识")
            if knowledge_results:
                for i, doc in enumerate(knowledge_results[:3], 1):
                    print(f"  - [{i}] {doc.get('filename', 'N/A')}: {doc.get('content', '')[:80]}...")
            else:
                print(f"  - 没有检索到相关知识，请检查知识库是否已加载。")

        if raw_prompt:
            # raw_prompt 模式：调用方已构建完整prompt，仅注入RAG知识
            full_prompt = question
            if knowledge_results:
                context_engine = get_context_engine()
                knowledge_ctx = context_engine.build_knowledge_context(knowledge_results)
                if knowledge_ctx:
                    full_prompt = f"{question}\n\n## 顾问专属知识参考\n{knowledge_ctx}"
        else:
            # 标准模式：由 context_engine 构建完整prompt
            context_engine = get_context_engine()
            full_prompt = context_engine.build_full_prompt(
                question=question,
                company_data=company_data,
                persona_data=persona_data,
                knowledge_results=knowledge_results,
                advisor_name=self.name,
                advisor_base_prompt=self.base_prompt,
                extra_context=extra_context
            )

        # 调用LLM
        response = await self._call_llm(full_prompt)

        return {
            "success": True,
            "response": response,
            "retrieved_docs": len(knowledge_results),
        }
    
    async def ask_with_context_stream(
        self,
        question: str,
        company_data: dict = None,
        persona_data: dict = None,
        extra_context: str = "",
        raw_prompt: bool = False,
    ):
        """
        流式版 ask_with_context — yield 知识检索状态 + LLM token chunks

        Yields:
            {"type": "status", "text": "..."}   — 阶段提示
            {"type": "content", "text": "..."}  — LLM 内容片段
            {"type": "error", "text": "..."}    — 错误
        """
        from .context_engine import get_context_engine

        yield {"type": "status", "text": "正在检索相关知识..."}
        knowledge_results = await self.retrieve_knowledge(question[:200], top_k=5)

        doc_count = len(knowledge_results)
        if doc_count:
            yield {"type": "status", "text": f"找到 {doc_count} 条相关方法论，正在构建脚本..."}
        else:
            yield {"type": "status", "text": "正在构建脚本..."}

        if raw_prompt:
            full_prompt = question
            if knowledge_results:
                context_engine = get_context_engine()
                knowledge_ctx = context_engine.build_knowledge_context(knowledge_results)
                if knowledge_ctx:
                    full_prompt = f"{question}\n\n## 顾问专属知识参考\n{knowledge_ctx}"
        else:
            context_engine = get_context_engine()
            full_prompt = context_engine.build_full_prompt(
                question=question,
                company_data=company_data,
                persona_data=persona_data,
                knowledge_results=knowledge_results,
                advisor_name=self.name,
                advisor_base_prompt=self.base_prompt,
                extra_context=extra_context,
            )

        async for chunk in self._call_llm_stream(full_prompt):
            yield chunk

    async def ask_module_question(
        self,
        module: str,
        question_id: str,
        variables: dict,
        company_data: dict = None,
        persona_data: dict = None,
    ) -> dict:
        """
        基于问题框架的模块化问答
        
        Args:
            module: 模块名称（rewrite/analyze/topic/script/review/talk/persona）
            question_id: 问题ID（如REWRITE_01）
            variables: 问题模板变量
            company_data: 公司信息
            persona_data: IP人设信息
        
        Returns:
            {"success": True, "response": "...", "question": "原始问题"}
        """
        from .context_engine import get_context_engine
        
        context_engine = get_context_engine()
        
        # 获取问题模板
        question_template = context_engine.get_question_template(module, question_id)
        if not question_template:
            return {
                "success": False,
                "error": f"未找到问题模板: {module}/{question_id}"
            }
        
        # 填充问题模板
        filled_question = context_engine.format_question(question_template, variables)
        
        # 检索相关知识
        knowledge_results = await self.retrieve_knowledge(filled_question, top_k=5)
        
        # 构建完整Prompt
        full_prompt = context_engine.build_full_prompt(
            question=filled_question,
            company_data=company_data,
            persona_data=persona_data,
            knowledge_results=knowledge_results,
            advisor_name=self.name,
        )
        
        # 调用LLM
        response = await self._call_llm(full_prompt)
        
        return {
            "success": True,
            "response": response,
            "question": filled_question,
            "question_id": question_id,
            "retrieved_docs": len(knowledge_results),
        }
    
    async def ask_all_module_questions(
        self,
        module: str,
        variables: dict,
        company_data: dict = None,
        persona_data: dict = None,
    ) -> dict:
        """
        执行模块的所有问题并合并结果
        
        Args:
            module: 模块名称
            variables: 所有问题共用的变量
            company_data: 公司信息
            persona_data: IP人设信息
        
        Returns:
            {"success": True, "results": [...]}
        """
        from .context_engine import get_context_engine
        
        context_engine = get_context_engine()
        questions = context_engine.get_module_questions(module)
        
        if not questions:
            return {
                "success": False,
                "error": f"未找到模块: {module}"
            }
        
        results = []
        for q in questions:
            result = await self.ask_module_question(
                module=module,
                question_id=q["id"],
                variables=variables,
                company_data=company_data,
                persona_data=persona_data,
            )
            results.append(result)
        
        return {
            "success": True,
            "module": module,
            "results": results,
        }
    
    def set_brand_id(self, brand_id: int):
        """动态设置客户ID（用于切换客户上下文）"""
        self.brand_id = brand_id
