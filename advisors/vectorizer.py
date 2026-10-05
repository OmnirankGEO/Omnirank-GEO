"""
向量化服务
使用 DashScope text-embedding-v3 生成向量，LanceDB 存储和检索
"""

import os
import asyncio
from typing import Optional
from pathlib import Path
import httpx


# Embedding 模型配置
EMBEDDING_API_BASE = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"
EMBEDDING_MODEL = "text-embedding-v4"  # 升级v4：性能提升15%-40%
EMBEDDING_DIM = 1024  # v4支持64-2048维，保持1024维


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


# lancedb 延迟加载：模块级 import 会在 lance_namespace 版本不兼容时崩溃，
# 影响所有不依赖向量检索的 advisor 调用链。改为首次使用时加载。
_KnowledgeChunk = None
_CorpusChunk = None


def _ensure_lance_models():
    """延迟创建 LanceModel 子类，首次调用时 import lancedb"""
    global _KnowledgeChunk, _CorpusChunk
    if _KnowledgeChunk is not None:
        return
    from lancedb.pydantic import LanceModel, Vector

    class KnowledgeChunk(LanceModel):
        """知识库文档块模型"""
        id: str
        advisor_id: str
        filename: str
        chunk_id: int
        content: str
        source: Optional[str] = None
        vector: Vector(EMBEDDING_DIM)  # type: ignore

    class CorpusChunk(LanceModel):
        """语料向量块模型（用于语义检索用户语料）"""
        id: str
        profile_id: str
        corpus_id: str
        chunk_id: int
        content: str
        source_name: Optional[str] = None
        content_type: Optional[str] = None
        vector: Vector(EMBEDDING_DIM)  # type: ignore

    _KnowledgeChunk = KnowledgeChunk
    _CorpusChunk = CorpusChunk


class VectorStore:
    """向量存储服务"""
    
    def __init__(self, db_path: str = "data/vectordb"):
        import lancedb
        _ensure_lance_models()
        self.db_path = Path(db_path)
        self.db_path.mkdir(parents=True, exist_ok=True)
        self.db = lancedb.connect(str(self.db_path))
        self._tables = {}
    
    def _get_table(self, advisor_id: str):
        """获取或创建顾问的知识库表

        优先使用新版命名 knowledge_advisor_{id}（knowledge_pipeline.py 生成），
        找不到才 fallback 到旧版 knowledge_{id}。
        """
        # 检查缓存
        cache_key = f"_resolved_{advisor_id}"
        if cache_key in self._tables:
            return self._tables[cache_key]

        try:
            # 获取已有表列表
            result = self.db.list_tables()
            existing_tables = result.tables if hasattr(result, 'tables') else list(result)
        except Exception as e:
            print(f"[VectorStore] list_tables 失败: {e}")
            existing_tables = []

        # 优先：新版命名（knowledge_pipeline.py 产生的表）
        new_name = f"knowledge_advisor_{advisor_id}"
        old_name = f"knowledge_{advisor_id}"

        for name in [new_name, old_name]:
            if name in existing_tables:
                try:
                    table = self.db.open_table(name)
                    self._tables[cache_key] = table
                    return table
                except Exception as e:
                    print(f"[VectorStore] open_table({name}) 失败: {e}，尝试下一个")
                    continue

        # 都打不开，创建空表
        # 注意: lancedb 0.29 在表不存在时 mode="overwrite" 会 rust panic，
        # 必须用 mode="create"
        try:
            table = self.db.create_table(
                new_name,
                schema=_KnowledgeChunk,
                mode="create"
            )
            self._tables[cache_key] = table
            print(f"[VectorStore] 创建空表: {new_name}")
            return table
        except Exception as e:
            print(f"[VectorStore] 创建表失败: {e}")
            return None
    
    async def add_chunks(
        self,
        advisor_id: str,
        chunks: list[dict],
        filename: str,
        source: str = None
    ) -> dict:
        """
        添加文档块到知识库
        
        Args:
            advisor_id: 顾问ID
            chunks: 文档块列表 [{"content": "...", "chunk_id": 0}, ...]
            filename: 文件名
            source: 来源标注
        
        Returns:
            {"success": True, "chunk_count": N}
        """
        if not chunks:
            return {"success": True, "chunk_count": 0}
        
        # 批量生成向量
        contents = [c["content"] for c in chunks]
        vectors = await self._batch_embed(contents)
        
        if not vectors:
            return {"success": False, "error": "向量化失败"}
        
        # 构建数据
        data = []
        for i, chunk in enumerate(chunks):
            data.append({
                "id": f"{advisor_id}_{filename}_{chunk.get('chunk_id', i)}",
                "advisor_id": advisor_id,
                "filename": filename,
                "chunk_id": chunk.get("chunk_id", i),
                "content": chunk["content"],
                "source": source or filename,
                "vector": vectors[i],
            })
        
        # 存入LanceDB
        table = self._get_table(advisor_id)
        if table is None:
            return {"success": False, "error": "无法打开知识库表"}
        table.add(data)
        
        return {"success": True, "chunk_count": len(data)}
    
    async def search(
        self,
        advisor_id: str,
        query: str,
        top_k: int = 5
    ) -> list[dict]:
        """
        语义检索相关知识
        
        Args:
            advisor_id: 顾问ID
            query: 查询内容
            top_k: 返回数量
        
        Returns:
            相关文档列表
        """
        # 生成查询向量
        query_vector = await self._embed(query)
        if not query_vector:
            return []
        
        # 检索
        table = self._get_table(advisor_id)
        if table is None:
            return []
        try:
            results = (
                table.search(query_vector)
                .limit(top_k)
                .to_list()
            )
        except Exception as e:
            print(f"[VectorStore] 检索失败: {e}")
            return []
        
        # 格式化返回
        return [
            {
                "content": r["content"],
                "filename": r["filename"],
                "source": r.get("source", ""),
                "score": 1 - r.get("_distance", 0),  # 转换为相似度
            }
            for r in results
        ]
    
    async def _embed(self, text: str) -> Optional[list[float]]:
        """生成单条文本的向量"""
        vectors = await self._batch_embed([text])
        return vectors[0] if vectors else None
    
    async def _batch_embed(self, texts: list[str]) -> Optional[list[list[float]]]:
        """
        批量生成向量（使用DashScope API）
        
        text-embedding-v4: 批量限制10条/次, RPM=1800, TPM=1200000
        """
        api_key = os.getenv("DASHSCOPE_API_KEY")
        if not api_key:
            print("[VectorStore] 错误: 未配置 DASHSCOPE_API_KEY")
            return None
        
        # [P0 2026-06-04] 超长 text 硬切子块全部向量化, 末尾平均池化保持输入↔输出 1:1(防 8192 token 上限 400)
        sub_texts, _spans = [], []
        for _t in texts:
            _subs = _hard_split_text(_t)
            _spans.append((len(sub_texts), len(_subs)))
            sub_texts.extend(_subs)

        all_vectors = []
        batch_size = 10  # DashScope text-embedding-v4 限制为10条/批

        for i in range(0, len(sub_texts), batch_size):
            batch = sub_texts[i:i + batch_size]
            
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            
            body = {
                "model": EMBEDDING_MODEL,
                "input": {"texts": batch},
                "parameters": {
                    "dimension": EMBEDDING_DIM,
                    "text_type": "document",
                }
            }
            
            max_retries = 2
            for attempt in range(max_retries):
                try:
                    from tools.llm_call_tracker import llm_track, usage_from_response_payload

                    async with httpx.AsyncClient(timeout=60) as client:
                        async with llm_track(
                            "advisor_vector_embedding",
                            "dashscope",
                            model=EMBEDDING_MODEL,
                            metadata={"batch_size": len(batch), "attempt": attempt + 1},
                        ) as tracker:
                            response = await client.post(
                                EMBEDDING_API_BASE,
                                headers=headers,
                                json=body,
                            )
                            data = response.json()
                            input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data)
                            tracker.record(
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                cached_tokens=cached_tokens,
                                success=response.status_code < 400,
                                error_msg=None if response.status_code < 400 else response.text[:200],
                            )

                        if response.status_code == 200:
                            embeddings = data.get("output", {}).get("embeddings", [])
                            # 按 text_index 排序确保顺序正确
                            embeddings.sort(key=lambda x: x.get("text_index", 0))
                            batch_vectors = [e["embedding"] for e in embeddings]
                            all_vectors.extend(batch_vectors)
                            break  # 成功，跳出重试
                        else:
                            print(f"[VectorStore] Embedding失败(尝试{attempt+1}): {response.status_code} - {response.text[:200]}")
                            if attempt < max_retries - 1:
                                import asyncio
                                await asyncio.sleep(1)
                            else:
                                return None
                except Exception as e:
                    print(f"[VectorStore] Embedding请求失败(尝试{attempt+1}): {e}")
                    if attempt < max_retries - 1:
                        import asyncio
                        await asyncio.sleep(1)
                    else:
                        return None

        # 子块向量按 spans 平均池化回原 texts 数量(保持 1:1)
        if len(all_vectors) != len(sub_texts):
            return None
        return [_mean_pool(all_vectors[s:s + c]) for s, c in _spans]
    
    # ==========================================
    # 语料向量化 (corpus)
    # ==========================================

    def _get_corpus_table(self, profile_id: str):
        """获取语料向量表（按 profile 隔离）"""
        table_name = f"corpus_{profile_id}"
        cache_key = f"_corpus_{profile_id}"
        if cache_key in self._tables:
            return self._tables[cache_key]

        existing = self.db.list_tables().tables
        if table_name in existing:
            table = self.db.open_table(table_name)
        else:
            table = self.db.create_table(table_name, schema=_CorpusChunk, mode="create")
        self._tables[cache_key] = table
        return table

    async def add_corpus(
        self,
        profile_id: str,
        corpus_id: str,
        chunks: list[dict],
        source_name: str = "",
    ) -> dict:
        """
        添加语料块到向量库

        chunks: [{"content": "...", "content_type": "raw|quote|insight", "chunk_id": 0}]
        """
        if not chunks:
            return {"success": True, "chunk_count": 0}

        contents = [c["content"] for c in chunks]
        vectors = await self._batch_embed(contents)
        if not vectors:
            return {"success": False, "error": "语料向量化失败"}

        data = []
        for i, chunk in enumerate(chunks):
            data.append({
                "id": f"{profile_id}_{corpus_id}_{chunk.get('chunk_id', i)}",
                "profile_id": profile_id,
                "corpus_id": corpus_id,
                "chunk_id": chunk.get("chunk_id", i),
                "content": chunk["content"],
                "source_name": source_name,
                "content_type": chunk.get("content_type", "raw"),
                "vector": vectors[i],
            })

        table = self._get_corpus_table(profile_id)
        table.add(data)
        return {"success": True, "chunk_count": len(data)}

    async def search_corpus(
        self,
        profile_id: str,
        query: str,
        top_k: int = 5,
    ) -> list[dict]:
        """语义检索用户语料"""
        query_vector = await self._embed(query)
        if not query_vector:
            return []

        table = self._get_corpus_table(profile_id)
        try:
            results = table.search(query_vector).limit(top_k).to_list()
        except Exception as e:
            print(f"[VectorStore] 语料检索失败: {e}")
            return []

        return [
            {
                "content": r["content"],
                "source_name": r.get("source_name", ""),
                "content_type": r.get("content_type", "raw"),
                "score": 1 - r.get("_distance", 0),
            }
            for r in results
        ]

    def delete_document(self, advisor_id: str, filename: str) -> bool:
        """删除指定文档的所有块"""
        table = self._get_table(advisor_id)
        try:
            safe_filename = filename.replace("'", "''")
            table.delete(f"filename = '{safe_filename}'")
            return True
        except Exception as e:
            print(f"[VectorStore] 删除失败: {e}")
            return False
    
    def get_stats(self, advisor_id: str) -> dict:
        """获取知识库统计"""
        table = self._get_table(advisor_id)
        try:
            count = table.count_rows()
            return {"chunk_count": count}
        except:
            return {"chunk_count": 0}


# 全局实例
_vector_store: Optional[VectorStore] = None


def get_vector_store() -> VectorStore:
    """获取向量存储实例"""
    global _vector_store
    if _vector_store is None:
        _vector_store = VectorStore()
    return _vector_store
