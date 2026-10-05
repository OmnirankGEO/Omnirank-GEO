"""
实时知识处理流水线

功能：
1. LLM清洗 - 使用 qwen3.6-plus 提取知识点
2. 向量化 - 使用 text-embedding-v4 生成向量
3. 存储 - LanceDB 存储向量索引

模型配置:
- 清洗: qwen3.6-plus (RPM=600)
- 向量: text-embedding-v4 (RPM=1800, 批次=10)
- 重排: qwen3-vl-rerank (RPM=600)
"""

import os
from dotenv import load_dotenv
load_dotenv()  # 加载.env文件

import json
import asyncio
import hashlib
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from dashscope import Generation
import dashscope

# 配置
dashscope.base_http_api_url = 'https://dashscope.aliyuncs.com/api/v1'
BASE_DIR = Path(__file__).resolve().parent.parent

# 模型配置（从 settings 读取，未配时 fallback）
# 2026-05-11 老板拍板:KB 清洗模型从 qwen3.6-plus 换 deepseek-v4-flash(对齐蒸馏 + 小榜对话)
#   · 仍走 DashScope compatible-mode(api_key=DASHSCOPE_API_KEY 不换)
#   · 向量化模型 text-embedding-v4 保持不动(下面 EMBEDDING_MODEL · 老板明示)
#   · prod 若已在 settings.json 配 kb_llm_clean_model · 仍以 settings 为准 ·
#     Deploy-CTO 需同步把 settings.json 的 kb_llm_clean_model 改 'deepseek-v4-flash' · 或删该字段走代码 fallback
def _get_clean_model():
    try:
        from config.settings_manager import get_current_settings
        return get_current_settings().kb_llm_clean_model or "deepseek-v4-flash"
    except Exception:
        return "deepseek-v4-flash"

def _get_max_retries():
    try:
        from config.settings_manager import get_current_settings
        return get_current_settings().llm_global_max_retries or 2
    except Exception:
        return 2

EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_API = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"
EMBEDDING_DIM = 1024
RERANK_MODEL = "qwen3-vl-rerank"
RERANK_API = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"

# 并行配置
EMBEDDING_BATCH_SIZE = 10  # API限制最大10
EMBEDDING_CONCURRENT = 15  # RPM=1800 → 可支持较高并发
MAX_RETRIES = _get_max_retries()


# [P0 2026-06-04] text-embedding-v4 单条上限 8192 token · 超长文本硬切防 400 "Range of input length should be [1, 8192]"
# 保守 6000 字符/块(中文≈6000 token · 英文更少 · 均 < 8192 留余量)
MAX_EMBED_CHARS = 6000


def _hard_split_text(text: str, max_chars: int = MAX_EMBED_CHARS) -> list:
    """超长文本硬切成 ≤max_chars 的子块 · 优先按段落/换行/句末边界, 无边界则硬切 · 不丢尾"""
    if not text or len(text) <= max_chars:
        return [text]
    chunks = []
    remaining = text
    while len(remaining) > max_chars:
        cut = max_chars
        for sep in ('\n\n', '\n', '。', '！', '？', '；', '. '):
            idx = remaining.rfind(sep, max_chars // 2, max_chars)
            if idx > 0:
                cut = idx + len(sep)
                break
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks


def _mean_pool(vectors: list) -> list:
    """多个子块向量平均池化为 1 个 · 保持调用方 texts↔vectors 1:1"""
    n = len(vectors)
    if n <= 1:
        return vectors[0] if vectors else []
    return [sum(col) / n for col in zip(*vectors)]


@dataclass
class CleanedDocument:
    """清洗后的文档结构"""
    summary: str = ""
    knowledge_points: List[Dict[str, str]] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    chunks: List[Dict[str, Any]] = field(default_factory=list)
    original_content: str = ""
    error: Optional[str] = None


@dataclass
class ProcessResult:
    """处理结果"""
    success: bool
    original_path: str = ""
    cleaned_path: str = ""
    chunk_count: int = 0
    knowledge_point_count: int = 0
    keywords: List[str] = field(default_factory=list)
    error: Optional[str] = None
    processing_time_ms: int = 0


class KnowledgePipeline:
    """实时知识处理流水线"""
    
    def __init__(self, knowledge_root: Path = None):
        self.api_key = os.getenv("DASHSCOPE_API_KEY")
        if not self.api_key:
            raise ValueError("未配置 DASHSCOPE_API_KEY 环境变量")
        
        self.knowledge_root = knowledge_root or BASE_DIR / "data" / "knowledge"
        self.knowledge_root.mkdir(parents=True, exist_ok=True)
        
        # 信号量控制并发
        self._embed_semaphore = asyncio.Semaphore(EMBEDDING_CONCURRENT)
    
    async def process_document(
        self,
        content: str,
        filename: str,
        brand_id: int,
        skip_clean: bool = False
    ) -> ProcessResult:
        """
        完整处理流程
        
        Args:
            content: 原始文档内容
            filename: 文件名
            brand_id: 客户ID
            skip_clean: 是否跳过清洗（直接向量化原文）
        
        Returns:
            ProcessResult: 处理结果
        """
        start_time = datetime.now()

        # 🆕 大文件阈值：超过则自动跳过LLM清洗，避免API超时
        AUTO_SKIP_THRESHOLD = 20 * 1024  # 20KB
        content_size = len(content.encode('utf-8'))

        if content_size > AUTO_SKIP_THRESHOLD and not skip_clean:
            print(f"[知识处理] ⚠️ 客户文档过大 ({content_size/1024:.1f}KB > 20KB)，自动跳过LLM清洗")
            skip_clean = True

        try:
            # 1. 保存原文
            client_dir = self.knowledge_root / "clients" / str(brand_id)
            client_dir.mkdir(parents=True, exist_ok=True)

            original_path = client_dir / filename
            original_path.write_text(content, encoding="utf-8")

            # 2. LLM清洗
            if skip_clean:
                cleaned = CleanedDocument(
                    original_content=content,
                    chunks=self._simple_chunk(content)
                )
            else:
                cleaned = await self.llm_clean(content, filename)
                if cleaned.error:
                    # LLM失败时降级为简单分块
                    print(f"[知识处理] LLM清洗失败，降级为简单分块: {cleaned.error}")
                    cleaned = CleanedDocument(
                        original_content=content,
                        chunks=self._simple_chunk(content)
                    )
                
                # 保存清洗版
                cleaned_dir = client_dir / "cleaned"
                cleaned_dir.mkdir(exist_ok=True)
                cleaned_path = cleaned_dir / f"{filename.rsplit('.', 1)[0]}_cleaned.json"
                cleaned_path.write_text(
                    json.dumps({
                        "summary": cleaned.summary,
                        "knowledge_points": cleaned.knowledge_points,
                        "keywords": cleaned.keywords,
                        "chunk_count": len(cleaned.chunks)
                    }, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
            
            # 3. 向量化 & 存储
            chunks_to_embed = cleaned.chunks if cleaned.chunks else [{"content": content, "chunk_id": 0}]
            texts = [c["content"] for c in chunks_to_embed]
            
            vectors = await self.batch_embed(texts)
            if vectors is None:
                return ProcessResult(
                    success=False,
                    original_path=str(original_path),
                    error="向量化失败"
                )
            
            # 4. 存储到LanceDB
            await self._store_vectors(
                brand_id=brand_id,
                filename=filename,
                chunks=chunks_to_embed,
                vectors=vectors,
                keywords=cleaned.keywords
            )
            
            elapsed = (datetime.now() - start_time).total_seconds() * 1000
            
            return ProcessResult(
                success=True,
                original_path=str(original_path),
                cleaned_path=str(cleaned_path) if not skip_clean else "",
                chunk_count=len(chunks_to_embed),
                knowledge_point_count=len(cleaned.knowledge_points),
                keywords=cleaned.keywords,
                processing_time_ms=int(elapsed)
            )
            
        except Exception as e:
            return ProcessResult(
                success=False,
                error=str(e)
            )
    
    async def process_role_document(
        self,
        content: str,
        filename: str,
        role_type: str,  # "advisor" | "employee"
        role_id: str,
        skip_clean: bool = False
    ) -> ProcessResult:
        """
        角色知识库处理流程（与客户知识库共用清洗/向量化逻辑）
        
        Args:
            content: 原始文档内容
            filename: 文件名
            role_type: 角色类型 ("advisor" | "employee")
            role_id: 角色ID
            skip_clean: 是否跳过清洗（直接向量化原文）
        
        Returns:
            ProcessResult: 处理结果
            
        Note:
            超过100KB的文件自动跳过LLM清洗，避免API调用过多导致失败
        """
        start_time = datetime.now()
        
        # 🆕 100KB阈值：超过则自动跳过清洗
        AUTO_SKIP_THRESHOLD = 100 * 1024  # 100KB
        content_size = len(content.encode('utf-8'))
        
        if content_size > AUTO_SKIP_THRESHOLD and not skip_clean:
            print(f"[知识处理] ⚠️ 文件过大 ({content_size/1024:.1f}KB > 100KB)，自动跳过LLM清洗")
            skip_clean = True
        
        # [GEO-R1-CAN-129] 防目录逃逸：只保留 basename，杜绝 '../../evil' 逃出 role_dir
        # 上游 api/knowledge_api.py 曾用 file.filename 原值(未 _safe_filename)，此处做兜底防线
        filename = Path(filename or "未命名资料.txt").name.strip() or "未命名资料.txt"

        try:
            # 1. 保存原文（使用 roles/{role_type}s/{role_id}/ 路径）
            role_dir = self.knowledge_root / "roles" / f"{role_type}s" / role_id
            role_dir.mkdir(parents=True, exist_ok=True)

            original_path = role_dir / filename
            # [GEO-R1-CAN-129] 二次容器化校验：确认最终写入路径确实落在 role_dir 之内，否则拒绝
            role_root_resolved = role_dir.resolve()
            target_resolved = original_path.resolve()
            if role_root_resolved != target_resolved.parent and role_root_resolved not in target_resolved.parents:
                raise ValueError(f"非法文件名，写入路径逃逸: {filename}")
            original_path.write_text(content, encoding="utf-8")
            print(f"[知识处理] 角色文档已保存: {original_path}")
            
            # 2. LLM清洗（或跳过）
            cleaned_path = ""
            if skip_clean:
                print(f"[知识处理] 跳过LLM清洗，使用简单分块...")
                cleaned = CleanedDocument(
                    original_content=content,
                    chunks=self._simple_chunk(content)
                )
            else:
                cleaned = await self.llm_clean(content, filename)
                if cleaned.error:
                    print(f"[知识处理] LLM清洗失败，降级为简单分块: {cleaned.error}")
                    cleaned = CleanedDocument(
                        original_content=content,
                        chunks=self._simple_chunk(content)
                    )
                else:
                    # 保存清洗版
                    cleaned_dir = role_dir / "cleaned"
                    cleaned_dir.mkdir(exist_ok=True)
                    cleaned_path = cleaned_dir / f"{filename.rsplit('.', 1)[0]}_cleaned.json"
                    cleaned_path.write_text(
                        json.dumps({
                            "summary": cleaned.summary,
                            "knowledge_points": cleaned.knowledge_points,
                            "keywords": cleaned.keywords,
                            "chunk_count": len(cleaned.chunks)
                        }, ensure_ascii=False, indent=2),
                        encoding="utf-8"
                    )
            
            # 3. 向量化
            chunks_to_embed = cleaned.chunks if cleaned.chunks else [{"content": content, "chunk_id": 0}]
            texts = [c["content"] for c in chunks_to_embed]
            
            print(f"[知识处理] 开始向量化 {len(texts)} 个分块...")
            vectors = await self.batch_embed(texts)
            if vectors is None:
                return ProcessResult(
                    success=False,
                    original_path=str(original_path),
                    error="向量化失败"
                )
            
            # 4. 存储到LanceDB（使用角色专用表）
            await self._store_role_vectors(
                role_type=role_type,
                role_id=role_id,
                filename=filename,
                chunks=chunks_to_embed,
                vectors=vectors,
                keywords=cleaned.keywords
            )
            
            elapsed = (datetime.now() - start_time).total_seconds() * 1000
            
            print(f"[知识处理] 角色文档处理完成: {filename}, chunks={len(chunks_to_embed)}, time={int(elapsed)}ms")
            
            return ProcessResult(
                success=True,
                original_path=str(original_path),
                cleaned_path=str(cleaned_path) if cleaned_path else "",
                chunk_count=len(chunks_to_embed),
                knowledge_point_count=len(cleaned.knowledge_points) if hasattr(cleaned, 'knowledge_points') else 0,
                keywords=cleaned.keywords if hasattr(cleaned, 'keywords') else [],
                processing_time_ms=int(elapsed)
            )
            
        except Exception as e:
            import traceback
            print(f"[知识处理] 角色文档处理失败: {e}")
            traceback.print_exc()
            return ProcessResult(
                success=False,
                error=str(e)
            )
    
    
    async def llm_clean(self, content: str, filename: str) -> CleanedDocument:
        """
        LLM知识清洗（支持大文档自动拆分）
        
        对于大文档（>15000字符），先预分块再分批调用LLM，最后合并结果
        """
        # 大文档阈值：8000字符（约2700中文字）- 减小以避免LLM超时
        CHUNK_THRESHOLD = 8000
        # LLM并发数:实际用 qwen3.6-plus(L39 RPM=600)· 4 并发安全 · 不再用 Kimi
        # [CTO-15.23 2026-05-09] 注释纠正:此处历史误注 kimi-k2.5 · 实际从未走 Kimi
        LLM_CONCURRENCY = 4
        
        if len(content) <= CHUNK_THRESHOLD:
            # 小文档：直接处理
            return await self._llm_clean_single(content, filename)
        
        # 大文档：预分块后并行处理
        print(f"[知识处理] 大文档检测到 ({len(content)}字符)，启动并行清洗...")
        
        # 先按段落预分块
        pre_chunks = self._pre_chunk_for_llm(content, CHUNK_THRESHOLD)
        print(f"[知识处理] 预分块为 {len(pre_chunks)} 个部分，并发数: {LLM_CONCURRENCY}")
        
        # 使用信号量控制并发
        semaphore = asyncio.Semaphore(LLM_CONCURRENCY)
        
        async def process_chunk(i: int, chunk_content: str):
            async with semaphore:
                print(f"[知识处理] 清洗分块 {i+1}/{len(pre_chunks)}...")
                result = await self._llm_clean_single(chunk_content, f"{filename}_part{i+1}")
                return (i, result)
        
        # 并行调用LLM
        tasks = [process_chunk(i, chunk) for i, chunk in enumerate(pre_chunks)]
        results = await asyncio.gather(*tasks)
        
        # 按原始顺序收集结果
        results_sorted = sorted(results, key=lambda x: x[0])
        
        all_chunks = []
        all_keywords = set()
        all_knowledge_points = []
        summaries = []
        
        for i, result in results_sorted:
            if result.error:
                print(f"[知识处理] 分块 {i+1} 清洗失败: {result.error}，使用简单分块")
                # 失败时降级为简单分块
                simple_chunks = self._simple_chunk(pre_chunks[i])
                all_chunks.extend(simple_chunks)
            else:
                # 收集结果
                all_chunks.extend(result.chunks)
                all_keywords.update(result.keywords or [])
                all_knowledge_points.extend(result.knowledge_points or [])
                if result.summary:
                    summaries.append(result.summary)
        
        # 重新编排chunk_id
        for i, chunk in enumerate(all_chunks):
            chunk["chunk_id"] = i
        
        # 合并摘要（取前3个）
        combined_summary = "；".join(summaries[:3]) if summaries else ""
        
        print(f"[知识处理] 并行清洗完成，共 {len(all_chunks)} 个分块，{len(all_keywords)} 个关键词")
        
        return CleanedDocument(
            summary=combined_summary,
            knowledge_points=all_knowledge_points,
            keywords=list(all_keywords)[:20],  # 最多20个关键词
            chunks=all_chunks,
            original_content=content
        )
    
    def _pre_chunk_for_llm(self, content: str, max_size: int) -> List[str]:
        """
        为LLM清洗预分块（按Markdown标题或双换行分割）
        """
        import re
        chunks = []
        
        # 优先按一级/二级标题分割
        sections = re.split(r'\n(?=#{1,2} )', content)
        
        current = ""
        for section in sections:
            section = section.strip()
            if not section:
                continue
            
            if len(current) + len(section) <= max_size:
                current = f"{current}\n\n{section}" if current else section
            else:
                if current:
                    chunks.append(current)
                # 如果单个section超过max_size，再按段落分割
                if len(section) > max_size:
                    sub_chunks = self._split_by_paragraph(section, max_size)
                    chunks.extend(sub_chunks)
                else:
                    current = section
        
        if current.strip():
            chunks.append(current.strip())
        
        return chunks if chunks else [content]
    
    def _split_by_paragraph(self, text: str, max_size: int) -> List[str]:
        """按段落分割长文本"""
        paragraphs = text.split('\n\n')
        chunks = []
        current = ""
        
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            
            if len(current) + len(para) + 2 <= max_size:
                current = f"{current}\n\n{para}" if current else para
            else:
                if current:
                    chunks.append(current)
                    current = ""
                # [P0 2026-06-04] 超长无分隔段落硬切, 从源头不产生 > max_size 的块(配合 embedding 入口 6000 字符兜底)
                if len(para) > max_size:
                    chunks.extend(_hard_split_text(para, max_size))
                else:
                    current = para

        if current.strip():
            chunks.append(current.strip())

        return chunks
    
    async def _llm_clean_single(self, content: str, filename: str) -> CleanedDocument:
        """
        单块LLM清洗（原llm_clean核心逻辑）
        """
        system_prompt = """你是一个专业的知识库管理员。你的任务是分析用户提供的文档，提取核心知识点并结构化输出。

请按以下JSON格式输出（严格JSON，不要有多余内容）：
{
    "summary": "文档的一句话摘要（不超过50字）",
    "knowledge_points": [
        {
            "title": "知识点标题",
            "content": "知识点详细内容（保留关键信息）",
            "keywords": ["关键词1", "关键词2"]
        }
    ],
    "keywords": ["全文检索关键词1", "关键词2", "...最多10个"],
    "chunks": [
        {
            "content": "分块内容（每块300-800字，保持语义完整）",
            "chunk_id": 0
        }
    ]
}

注意：
1. 知识点要具体、可操作，避免空泛描述
2. 关键词要覆盖专业术语、方法论名称、核心概念
3. 分块时保持段落完整性，不要在句子中间断开"""

        user_prompt = f"""请分析以下文档并提取知识点：

文件名：{filename}

文档内容：
{content}"""

        try:
            # 使用设置中配置的清洗模型
            clean_model = _get_clean_model()
            # 2026-05-11 老板换模型 deepseek-v4-flash:dashscope SDK 主要走 qwen 原生 API
            # deepseek 系列必须走 OpenAI compatible-mode(已在 base_employee 验证)
            # qwen 系列保留 dashscope SDK 路径(成熟 · 不动)
            is_compatible_mode = clean_model.lower().startswith("deepseek")

            if is_compatible_mode:
                # OpenAI compatible-mode · 用 httpx 直连
                from tools.llm_call_tracker import llm_track, usage_from_response_payload

                async with httpx.AsyncClient(timeout=120) as client:
                    async with llm_track(
                        "knowledge_cleaning",
                        "dashscope",
                        model=clean_model,
                    ) as tracker:
                        http_resp = await client.post(
                            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                            headers={
                                "Authorization": f"Bearer {self.api_key}",
                                "Content-Type": "application/json",
                            },
                            json={
                                "model": clean_model,
                                "messages": [
                                    {"role": "system", "content": system_prompt},
                                    {"role": "user", "content": user_prompt},
                                ],
                            },
                        )
                        if http_resp.status_code == 200:
                            http_data_for_usage = http_resp.json()
                            input_tokens, output_tokens, cached_tokens = usage_from_response_payload(http_data_for_usage)
                            tracker.record(
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                cached_tokens=cached_tokens,
                                success=True,
                            )
                        else:
                            tracker.record(success=False, error_msg=f"HTTP {http_resp.status_code}: {http_resp.text[:200]}")
                if http_resp.status_code != 200:
                    return CleanedDocument(
                        original_content=content,
                        chunks=self._simple_chunk(content),
                        error=f"KB 清洗 LLM HTTP {http_resp.status_code}: {http_resp.text[:200]}",
                    )
                http_data = http_resp.json()
                result_text = (http_data.get("choices") or [{}])[0].get("message", {}).get("content", "")

                # 去除 think 标签
                if "<think>" in result_text:
                    import re as _re
                    result_text = _re.sub(r'<think>[\s\S]*?</think>', '', result_text).strip()

                # 跳过 dashscope status_code 校验 · 直接进 JSON 解析分支
                try:
                    json_start = result_text.find('{')
                    json_end = result_text.rfind('}') + 1
                    if json_start >= 0 and json_end > json_start:
                        json_str = result_text[json_start:json_end]
                        data = json.loads(json_str)

                        llm_chunks = data.get("chunks", [])
                        if len(llm_chunks) <= 1 and len(content) > 1000:
                            print(f"[知识处理] LLM仅返回{len(llm_chunks)}块(原文{len(content)}字)，降级为规则分块")
                            llm_chunks = self._simple_chunk(content)

                        return CleanedDocument(
                            summary=data.get("summary", ""),
                            knowledge_points=data.get("knowledge_points", []),
                            keywords=data.get("keywords", []),
                            chunks=llm_chunks,
                            original_content=content
                        )
                    else:
                        print(f"[知识处理] LLM返回无JSON，降级为规则分块")
                        return CleanedDocument(
                            original_content=content,
                            chunks=self._simple_chunk(content),
                            error="LLM返回内容中未找到JSON"
                        )
                except json.JSONDecodeError as e:
                    return CleanedDocument(
                        original_content=content,
                        chunks=self._simple_chunk(content),
                        error=f"JSON解析失败: {e}"
                    )

            # 走 dashscope SDK 原生路径(qwen 系列)
            from tools.llm_call_tracker import llm_track_sync

            with llm_track_sync(
                caller="knowledge_cleaning",
                platform="dashscope",
                model=clean_model,
            ) as tracker:
                # [并发-1 2026-06-10] 同步 dashscope SDK 调用移入 asyncio.to_thread:
                #   原在 async _llm_clean_single 内裸调,大文档 4 并发清洗会同步阻塞事件循环卡全场。
                import asyncio as _asyncio
                response = await _asyncio.to_thread(
                    lambda: dashscope.Generation.call(
                        api_key=self.api_key,
                        model=clean_model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        result_format="message",
                    )
                )
                usage = getattr(response, "usage", None)
                tracker.record(
                    input_tokens=int(getattr(usage, "input_tokens", 0) or getattr(usage, "prompt_tokens", 0) or 0) if usage else 0,
                    output_tokens=int(getattr(usage, "output_tokens", 0) or getattr(usage, "completion_tokens", 0) or 0) if usage else 0,
                    cached_tokens=0,
                    success=getattr(response, "status_code", 0) == 200,
                    error_msg=None if getattr(response, "status_code", 0) == 200 else str(getattr(response, "message", ""))[:200],
                )

            if response.status_code == 200:
                result_text = response.output.choices[0].message.content

                # 去除 think 标签
                if "<think>" in result_text:
                    import re as _re
                    result_text = _re.sub(r'<think>[\s\S]*?</think>', '', result_text).strip()

                # 解析JSON
                try:
                    json_start = result_text.find('{')
                    json_end = result_text.rfind('}') + 1
                    if json_start >= 0 and json_end > json_start:
                        json_str = result_text[json_start:json_end]
                        data = json.loads(json_str)

                        llm_chunks = data.get("chunks", [])
                        # 安全兜底：如果 LLM 返回的 chunks 太少（<=1）且原文够长，用 _simple_chunk 覆盖
                        if len(llm_chunks) <= 1 and len(content) > 1000:
                            print(f"[知识处理] LLM仅返回{len(llm_chunks)}块(原文{len(content)}字)，降级为规则分块")
                            llm_chunks = self._simple_chunk(content)

                        return CleanedDocument(
                            summary=data.get("summary", ""),
                            knowledge_points=data.get("knowledge_points", []),
                            keywords=data.get("keywords", []),
                            chunks=llm_chunks,
                            original_content=content
                        )
                    else:
                        # 没找到 JSON，降级
                        print(f"[知识处理] LLM返回无JSON，降级为规则分块")
                        return CleanedDocument(
                            original_content=content,
                            chunks=self._simple_chunk(content),
                            error="LLM返回内容中未找到JSON"
                        )
                except json.JSONDecodeError as e:
                    return CleanedDocument(
                        original_content=content,
                        chunks=self._simple_chunk(content),
                        error=f"JSON解析失败: {e}"
                    )
            else:
                return CleanedDocument(
                    original_content=content,
                    chunks=self._simple_chunk(content),
                    error=f"API错误: {response.code} - {response.message}"
                )

        except Exception as e:
            return CleanedDocument(
                original_content=content,
                chunks=self._simple_chunk(content),
                error=str(e)
            )
    
    def _simple_chunk(self, content: str, max_size: int = 1200) -> List[Dict]:
        """
        Markdown感知分块：
        1. 按##/###标题拆分为语义段落
        2. 每个chunk保留所属章节标题作为上下文前缀
        3. 超长段落按段落边界二次拆分
        4. 过短尾块合并到前一块
        """
        import re

        # 预处理：去掉独立的 --- 分隔线（不影响标题拆分）
        content = re.sub(r'\n---\s*\n', '\n\n', content)

        # 按二级/三级标题拆分（保留标题行）
        sections = re.split(r'\n(?=#{2,3} )', content)

        chunks = []
        chunk_id = 0
        current_h2 = ""  # 当前二级标题（章节上下文）

        for section in sections:
            section = section.strip()
            if not section:
                continue

            # 提取本段的标题
            header_match = re.match(r'^(#{2,3})\s+(.+)', section)
            if header_match:
                level = len(header_match.group(1))
                title = header_match.group(2).strip()
                if level == 2:
                    current_h2 = title

            # 如果段落在阈值内，直接作为一个chunk
            if len(section) <= max_size:
                # 过短的块合并：优先向前合并，首块时向后合并（后续处理）
                if len(section) < 150 and chunks:
                    chunks[-1]["content"] += "\n\n" + section
                else:
                    chunks.append({"content": section, "chunk_id": chunk_id, "section": current_h2})
                    chunk_id += 1
            else:
                # 超长段落：按双换行二次拆分，每个子块加章节前缀
                prefix = f"[{current_h2}] " if current_h2 else ""
                paragraphs = section.split('\n\n')
                current = ""

                for para in paragraphs:
                    para = para.strip()
                    if not para:
                        continue

                    if len(current) + len(para) + 2 > max_size:
                        if current:
                            # 非首块加章节前缀帮助检索
                            text = current.strip()
                            if not text.startswith('#') and prefix and chunk_id > 0:
                                text = prefix + text
                            chunks.append({"content": text, "chunk_id": chunk_id, "section": current_h2})
                            chunk_id += 1
                        current = para
                    else:
                        current = f"{current}\n\n{para}" if current else para

                if current.strip():
                    text = current.strip()
                    if not text.startswith('#') and prefix and chunk_id > 0:
                        text = prefix + text
                    # 过短尾块合并
                    if len(text) < 150 and chunks:
                        chunks[-1]["content"] += "\n\n" + text
                    else:
                        chunks.append({"content": text, "chunk_id": chunk_id, "section": current_h2})
                        chunk_id += 1

        # 后处理：如果首块过短（文档标题/元信息），合并到第二块
        if len(chunks) > 1 and len(chunks[0]["content"]) < 150:
            chunks[1]["content"] = chunks[0]["content"] + "\n\n" + chunks[1]["content"]
            chunks.pop(0)
            # 重编号 chunk_id
            for i, c in enumerate(chunks):
                c["chunk_id"] = i

        return chunks if chunks else [{"content": content, "chunk_id": 0}]
    
    async def batch_embed(self, texts: List[str]) -> Optional[List[List[float]]]:
        """
        批量向量化（并发）
        
        使用 text-embedding-v4，批次大小10，asyncio.gather 并发执行
        """
        if not texts:
            return []

        # [P0 2026-06-04] 超长 text 硬切子块全部向量化, 末尾平均池化保持 1:1(防 text-embedding-v4 8192 token 上限 400)
        sub_texts, _spans = [], []
        for _t in texts:
            _subs = _hard_split_text(_t)
            _spans.append((len(sub_texts), len(_subs)))
            sub_texts.extend(_subs)

        # 分批
        batches = [sub_texts[i:i + EMBEDDING_BATCH_SIZE] for i in range(0, len(sub_texts), EMBEDDING_BATCH_SIZE)]

        # 并发执行所有批次
        tasks = [self._embed_batch(batch) for batch in batches]
        results = await asyncio.gather(*tasks)

        # 合并结果
        all_vectors = []
        for vectors in results:
            if vectors is None:
                return None
            all_vectors.extend(vectors)

        # 子块向量按 spans 平均池化回原 texts 数量(保持 1:1)
        if len(all_vectors) != len(sub_texts):
            return None
        return [_mean_pool(all_vectors[s:s + c]) for s, c in _spans]
    
    async def _embed_batch(self, texts: List[str]) -> Optional[List[List[float]]]:
        """单批次向量化"""
        for retry in range(MAX_RETRIES):
            async with self._embed_semaphore:
                try:
                    from tools.llm_call_tracker import llm_track

                    async with httpx.AsyncClient(timeout=60) as client:
                        async with llm_track(
                            "knowledge_embedding",
                            "dashscope",
                            model=EMBEDDING_MODEL,
                            metadata={"text_count": len(texts)},
                        ) as tracker:
                            response = await client.post(
                                EMBEDDING_API,
                                headers={
                                    "Authorization": f"Bearer {self.api_key}",
                                    "Content-Type": "application/json"
                                },
                                json={
                                    "model": EMBEDDING_MODEL,
                                    "input": {"texts": texts},
                                    "parameters": {
                                        "dimension": EMBEDDING_DIM,
                                        "text_type": "document"
                                    }
                                }
                            )
                            tracker.record(
                                input_tokens=max(1, sum(len(t or "") for t in texts) // 3),
                                output_tokens=0,
                                success=response.status_code == 200,
                                error_msg=None if response.status_code == 200 else f"HTTP {response.status_code}: {response.text[:200]}",
                            )
                        
                        if response.status_code == 200:
                            data = response.json()
                            embeddings = data.get("output", {}).get("embeddings", [])
                            embeddings.sort(key=lambda x: x.get("text_index", 0))
                            return [e["embedding"] for e in embeddings]
                        elif response.status_code == 429:
                            wait = 2 ** retry
                            await asyncio.sleep(wait)
                        else:
                            print(f"Embedding失败: {response.status_code}")
                            return None
                            
                except Exception as e:
                    if retry < MAX_RETRIES - 1:
                        await asyncio.sleep(1)
                    else:
                        print(f"Embedding请求失败: {e}")
                        return None
        
        return None
    
    async def _store_vectors(
        self,
        brand_id: int,
        filename: str,
        chunks: List[Dict],
        vectors: List[List[float]],
        keywords: List[str]
    ):
        """存储向量到LanceDB"""
        try:
            import lancedb
            from lancedb.pydantic import LanceModel, Vector
            
            db_path = BASE_DIR / "data" / "vectordb"
            db_path.mkdir(parents=True, exist_ok=True)
            db = lancedb.connect(str(db_path))
            
            table_name = f"knowledge_client_{brand_id}"
            
            # 准备数据
            data = []
            for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
                doc_id = hashlib.md5(f"{filename}_{i}".encode()).hexdigest()[:16]
                data.append({
                    "id": doc_id,
                    "brand_id": brand_id,
                    "filename": filename,
                    "chunk_id": chunk.get("chunk_id", i),
                    "content": chunk["content"],
                    "keywords": ",".join(keywords),
                    "vector": vector
                })
            
            # 创建或追加表。LanceDB table_names 可能分页，直接 open 更稳。
            try:
                table = db.open_table(table_name)
            except Exception:
                db.create_table(table_name, data)
                return

            # 删除同文件的旧数据
            # [GEO-R4-CAN-020] 转义谓词，避免 filename 内含引号/反斜杠注入 LanceDB 过滤表达式
            #   （镜像 tools/unified_knowledge.py:_delete_client_vectors 的转义）
            try:
                escaped_filename = filename.replace("\\", "\\\\").replace('"', '\\"')
                table.delete(f'filename = "{escaped_filename}"')
            except:
                pass
            table.add(data)
            
        except Exception as e:
            print(f"存储向量失败: {e}")
            raise

    async def _store_role_vectors(
        self,
        role_type: str,
        role_id: str,
        filename: str,
        chunks: List[Dict],
        vectors: List[List[float]],
        keywords: List[str]
    ):
        """存储角色知识向量到LanceDB"""
        try:
            import lancedb
            
            db_path = BASE_DIR / "data" / "vectordb"
            db_path.mkdir(parents=True, exist_ok=True)
            db = lancedb.connect(str(db_path))
            
            # 使用角色专用表名: knowledge_advisor_{id} 或 knowledge_employee_{id}
            table_name = f"knowledge_{role_type}_{role_id}"
            
            # 准备数据
            data = []
            for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
                doc_id = hashlib.md5(f"{filename}_{i}".encode()).hexdigest()[:16]
                data.append({
                    "id": doc_id,
                    "role_type": role_type,
                    "role_id": role_id,
                    "filename": filename,
                    "chunk_id": chunk.get("chunk_id", i),
                    "content": chunk["content"],
                    "keywords": ",".join(keywords) if keywords else "",
                    "vector": vector
                })
            
            # 创建或追加表。LanceDB table_names 可能分页，直接 open 更稳。
            try:
                table = db.open_table(table_name)
            except Exception:
                db.create_table(table_name, data)
                print(f"[知识处理] 向量已存储到表: {table_name}, 共{len(data)}条记录")
                return

            # 删除同文件的旧数据
            # [GEO-R4-CAN-020] 转义谓词，避免 filename 内含引号/反斜杠注入 LanceDB 过滤表达式
            #   （镜像 tools/unified_knowledge.py:_delete_client_vectors 的转义）
            try:
                escaped_filename = filename.replace("\\", "\\\\").replace('"', '\\"')
                table.delete(f'filename = "{escaped_filename}"')
            except:
                pass
            table.add(data)
            
            print(f"[知识处理] 向量已存储到表: {table_name}, 共{len(data)}条记录")
            
        except Exception as e:
            print(f"存储角色向量失败: {e}")
            raise



class RerankService:
    """Rerank重排序服务"""
    
    def __init__(self):
        self.api_key = os.getenv("DASHSCOPE_API_KEY")
    
    async def rerank(
        self,
        query: str,
        documents: List[str],
        top_n: int = 5
    ) -> List[Dict[str, Any]]:
        """
        对检索结果进行重排序
        
        Args:
            query: 查询文本
            documents: 候选文档列表
            top_n: 返回top-N结果
        
        Returns:
            重排序后的结果列表
        """
        try:
            from tools.llm_call_tracker import llm_track

            async with httpx.AsyncClient(timeout=30) as client:
                async with llm_track(
                    "knowledge_rerank",
                    "dashscope",
                    model=RERANK_MODEL,
                    metadata={"document_count": len(documents), "top_n": top_n},
                ) as tracker:
                    response = await client.post(
                        RERANK_API,
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json"
                        },
                        json={
                            "model": RERANK_MODEL,
                            "input": {
                                "query": query,
                                "documents": documents
                            },
                            "parameters": {
                                "return_documents": True,
                                "top_n": top_n
                            }
                        }
                    )
                    tracker.record(
                        input_tokens=max(1, (len(query or "") + sum(len(d or "") for d in documents)) // 3),
                        output_tokens=0,
                        success=response.status_code == 200,
                        error_msg=None if response.status_code == 200 else f"HTTP {response.status_code}: {response.text[:200]}",
                    )
                
                if response.status_code == 200:
                    data = response.json()
                    results = data.get("output", {}).get("results", [])
                    return [
                        {
                            "index": r.get("index"),
                            "score": r.get("relevance_score"),
                            "document": r.get("document", {}).get("text", "")
                        }
                        for r in results
                    ]
                else:
                    print(f"Rerank失败: {response.status_code}")
                    return []
                    
        except Exception as e:
            print(f"Rerank请求失败: {e}")
            return []


# 单例
_pipeline_instance: Optional[KnowledgePipeline] = None
_rerank_instance: Optional[RerankService] = None


def get_knowledge_pipeline() -> KnowledgePipeline:
    """获取知识处理流水线单例"""
    global _pipeline_instance
    if _pipeline_instance is None:
        _pipeline_instance = KnowledgePipeline()
    return _pipeline_instance


def get_rerank_service() -> RerankService:
    """获取重排序服务单例"""
    global _rerank_instance
    if _rerank_instance is None:
        _rerank_instance = RerankService()
    return _rerank_instance
