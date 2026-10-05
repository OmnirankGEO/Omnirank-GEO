"""
业务知识库 RAG 工具
用于客户档案关联的知识库向量检索

基于现有 advisors/vectorizer.py 实现
使用 DashScope text-embedding-v3 + LanceDB
"""

import os
import asyncio
import hashlib
from typing import Optional, List, Dict, Any
from pathlib import Path
from datetime import datetime
import httpx


# Embedding 模型配置（与 advisors/vectorizer.py 一致）
EMBEDDING_API_BASE = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"
EMBEDDING_MODEL = "text-embedding-v4"  # 升级v4：性能提升15%-40%
EMBEDDING_DIM = 1024  # text-embedding-v4 默认维度

# 知识库存储路径
KNOWLEDGE_BASE_DIR = Path(__file__).parent.parent / "data" / "knowledge" / "profiles"
VECTOR_DB_DIR = Path(__file__).parent.parent / "data" / "vectordb_profiles"


# lancedb 延迟加载（与 advisors/vectorizer.py 同理）
_ProfileKnowledgeChunk = None


def _ensure_profile_lance_model():
    global _ProfileKnowledgeChunk
    if _ProfileKnowledgeChunk is not None:
        return
    from lancedb.pydantic import LanceModel, Vector

    class ProfileKnowledgeChunk(LanceModel):
        """客户知识库文档块模型"""
        id: str
        profile_id: str
        file_id: str
        filename: str
        chunk_id: int
        content: str
        source: Optional[str] = None
        vector: Vector(EMBEDDING_DIM)  # type: ignore

    _ProfileKnowledgeChunk = ProfileKnowledgeChunk


class BusinessKnowledgeRAG:
    """
    业务知识库 RAG 服务
    
    为每个客户档案提供独立的知识库向量检索能力
    """
    
    def __init__(self, profile_id: str):
        """
        初始化 RAG 服务
        
        Args:
            profile_id: 客户档案ID
        """
        self.profile_id = profile_id
        
        # 确保目录存在
        KNOWLEDGE_BASE_DIR.mkdir(parents=True, exist_ok=True)
        VECTOR_DB_DIR.mkdir(parents=True, exist_ok=True)
        
        # 知识库文件存储目录
        self.knowledge_dir = KNOWLEDGE_BASE_DIR / profile_id
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        
        # 向量数据库（延迟加载 lancedb）
        import lancedb
        _ensure_profile_lance_model()
        self.db = lancedb.connect(str(VECTOR_DB_DIR))
        self._table = None
    
    def _get_table(self):
        """获取或创建客户的知识库向量表"""
        if self._table is not None:
            return self._table
        
        table_name = f"profile_kb_{self.profile_id}"
        
        # [2026-06-11 修分页bug] table_names() 默认分页(返10)·表存在却漏列→误走 else overwrite 清空已上传数据·limit 拿全
        if table_name in self.db.table_names(limit=100000):
            self._table = self.db.open_table(table_name)
        else:
            # 创建空表
            self._table = self.db.create_table(
                table_name,
                schema=_ProfileKnowledgeChunk,
                mode="overwrite"
            )
        
        return self._table
    
    async def add_document(
        self,
        file_id: str,
        filename: str,
        content: str,
        chunk_size: int = 500,
        chunk_overlap: int = 50
    ) -> Dict[str, Any]:
        """
        添加文档到知识库
        
        Args:
            file_id: 文件ID
            filename: 文件名
            content: 文档内容
            chunk_size: 分块大小（字符数）
            chunk_overlap: 重叠大小
        
        Returns:
            {success: bool, chunk_count: int, error?: str}
        """
        if not content.strip():
            return {"success": False, "error": "文档内容为空"}
        
        # 分块
        chunks = self._split_content(content, chunk_size, chunk_overlap)
        if not chunks:
            return {"success": False, "error": "分块失败"}
        
        print(f"[BusinessKnowledgeRAG] 文档 {filename} 分割为 {len(chunks)} 个块")
        
        # 批量生成向量
        contents = [c["content"] for c in chunks]
        vectors = await self._batch_embed(contents)
        
        if not vectors:
            return {"success": False, "error": "向量化失败"}
        
        # 构建数据
        data = []
        for i, chunk in enumerate(chunks):
            data.append({
                "id": f"{self.profile_id}_{file_id}_{i}",
                "profile_id": self.profile_id,
                "file_id": file_id,
                "filename": filename,
                "chunk_id": i,
                "content": chunk["content"],
                "source": filename,
                "vector": vectors[i],
            })
        
        # 存入 LanceDB
        table = self._get_table()
        table.add(data)
        
        print(f"[BusinessKnowledgeRAG] 成功添加 {len(data)} 个向量块到知识库")
        
        return {"success": True, "chunk_count": len(data)}
    
    async def retrieve(
        self,
        query: str,
        top_k: int = 3,
        min_score: float = 0.3  # 🔧 降低阈值
    ) -> List[Dict[str, Any]]:
        """
        检索相关知识
        
        Args:
            query: 查询内容
            top_k: 返回数量
            min_score: 最低相似度阈值
        
        Returns:
            相关文档列表 [{content, filename, source, score}]
        """
        if not query.strip():
            return []
        
        # 生成查询向量
        query_vector = await self._embed(query)
        if not query_vector:
            print(f"[BusinessKnowledgeRAG] ⚠️ 向量化失败")
            return []
        
        # 检索
        table = self._get_table()
        try:
            results = (
                table.search(query_vector)
                .limit(top_k * 2)  # 🔧 多取一些再过滤
                .to_list()
            )
            print(f"[BusinessKnowledgeRAG] 原始检索到 {len(results)} 条")
        except Exception as e:
            print(f"[BusinessKnowledgeRAG] 检索失败: {e}")
            return []
        
        # 格式化返回（过滤低分结果）
        filtered_results = []
        for r in results:
            distance = r.get("_distance", 1.0)
            # LanceDB 使用 L2距离，距离越小越相似
            # 简单转换：score = max(0, 1 - distance/2)
            score = max(0, 1 - distance / 2)
            print(f"  - {r.get('filename', 'unknown')}: distance={distance:.3f}, score={score:.3f}")
            if score >= min_score:
                filtered_results.append({
                    "content": r["content"],
                    "filename": r["filename"],
                    "source": r.get("source", ""),
                    "score": score,
                })
        
        print(f"[BusinessKnowledgeRAG] 过滤后 {len(filtered_results)} 个相关结果 (阈值: {min_score})")
        
        return filtered_results[:top_k]  # 限制返回数量
    
    async def delete_document(self, file_id: str) -> bool:
        """
        删除指定文档的所有向量块
        
        Args:
            file_id: 文件ID
        
        Returns:
            是否成功
        """
        table = self._get_table()
        try:
            table.delete(f"file_id = '{file_id}'")
            print(f"[BusinessKnowledgeRAG] 已删除文件 {file_id} 的向量块")
            return True
        except Exception as e:
            print(f"[BusinessKnowledgeRAG] 删除失败: {e}")
            return False
    
    def get_stats(self) -> Dict[str, Any]:
        """获取知识库统计信息"""
        table = self._get_table()
        try:
            count = table.count_rows()
            return {
                "profile_id": self.profile_id,
                "chunk_count": count,
            }
        except:
            return {
                "profile_id": self.profile_id,
                "chunk_count": 0,
            }
    
    def _split_content(
        self,
        content: str,
        chunk_size: int,
        chunk_overlap: int
    ) -> List[Dict[str, Any]]:
        """
        将文档内容分割成块
        
        优先按段落分割，保持语义完整性
        """
        # 按段落分割
        paragraphs = content.split("\n\n")
        
        chunks = []
        current_chunk = ""
        chunk_id = 0
        
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            
            # 如果当前段落太长，需要进一步分割
            if len(para) > chunk_size:
                # 先保存当前累积的内容
                if current_chunk:
                    chunks.append({
                        "chunk_id": chunk_id,
                        "content": current_chunk.strip(),
                    })
                    chunk_id += 1
                    current_chunk = ""
                
                # 按句子分割长段落
                sentences = self._split_into_sentences(para)
                for sentence in sentences:
                    if len(current_chunk) + len(sentence) > chunk_size:
                        if current_chunk:
                            chunks.append({
                                "chunk_id": chunk_id,
                                "content": current_chunk.strip(),
                            })
                            chunk_id += 1
                            # 保留重叠部分
                            if chunk_overlap > 0 and len(current_chunk) > chunk_overlap:
                                current_chunk = current_chunk[-chunk_overlap:]
                            else:
                                current_chunk = ""
                    current_chunk += sentence + " "
            else:
                # 检查是否需要开始新块
                if len(current_chunk) + len(para) > chunk_size:
                    if current_chunk:
                        chunks.append({
                            "chunk_id": chunk_id,
                            "content": current_chunk.strip(),
                        })
                        chunk_id += 1
                        # 保留重叠部分
                        if chunk_overlap > 0 and len(current_chunk) > chunk_overlap:
                            current_chunk = current_chunk[-chunk_overlap:]
                        else:
                            current_chunk = ""
                
                current_chunk += para + "\n\n"
        
        # 保存最后一个块
        if current_chunk.strip():
            chunks.append({
                "chunk_id": chunk_id,
                "content": current_chunk.strip(),
            })
        
        return chunks
    
    def _split_into_sentences(self, text: str) -> List[str]:
        """按句子分割文本"""
        # 简单的句子分割
        import re
        sentences = re.split(r'(?<=[。！？.!?])\s*', text)
        return [s for s in sentences if s.strip()]
    
    async def _embed(self, text: str) -> Optional[List[float]]:
        """生成单条文本的向量"""
        vectors = await self._batch_embed([text])
        return vectors[0] if vectors else None
    
    async def _batch_embed(self, texts: List[str]) -> Optional[List[List[float]]]:
        """
        批量生成向量（使用 DashScope API）
        
        最多支持10条文本/次，超过则分批处理
        """
        api_key = os.getenv("DASHSCOPE_API_KEY")
        if not api_key:
            print("[BusinessKnowledgeRAG] 错误: 未配置 DASHSCOPE_API_KEY")
            return None
        
        all_vectors = []
        batch_size = 10  # DashScope text-embedding-v3 限制
        
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            
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
            
            try:
                from tools.llm_call_tracker import llm_track, usage_from_response_payload

                async with httpx.AsyncClient(timeout=60) as client:
                    async with llm_track(
                        "knowledge_rag_embedding",
                        "dashscope",
                        model=EMBEDDING_MODEL,
                        metadata={"batch_size": len(batch)},
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
                    else:
                        print(f"[BusinessKnowledgeRAG] Embedding失败: {response.status_code} - {response.text[:200]}")
                        return None
            except Exception as e:
                print(f"[BusinessKnowledgeRAG] Embedding请求失败: {e}")
                return None
        
        return all_vectors


# ========== 便捷函数 ==========

async def add_knowledge_to_profile(
    profile_id: str,
    file_id: str,
    filename: str,
    content: str
) -> Dict[str, Any]:
    """
    添加知识库文档到客户档案
    
    Args:
        profile_id: 档案ID
        file_id: 文件ID
        filename: 文件名
        content: 文档内容
    
    Returns:
        {success: bool, chunk_count: int}
    """
    rag = BusinessKnowledgeRAG(profile_id)
    return await rag.add_document(file_id, filename, content)


async def search_profile_knowledge(
    profile_id: str,
    query: str,
    top_k: int = 3
) -> List[Dict[str, Any]]:
    """
    检索客户档案的知识库
    
    Args:
        profile_id: 档案ID
        query: 查询内容
        top_k: 返回数量
    
    Returns:
        相关文档列表
    """
    rag = BusinessKnowledgeRAG(profile_id)
    return await rag.retrieve(query, top_k)


async def delete_knowledge_from_profile(profile_id: str, file_id: str) -> bool:
    """
    从客户档案删除知识库文档
    
    Args:
        profile_id: 档案ID
        file_id: 文件ID
    
    Returns:
        是否成功
    """
    rag = BusinessKnowledgeRAG(profile_id)
    return await rag.delete_document(file_id)


def get_profile_knowledge_stats(profile_id: str) -> Dict[str, Any]:
    """
    获取客户档案知识库统计
    
    Args:
        profile_id: 档案ID
    
    Returns:
        统计信息
    """
    rag = BusinessKnowledgeRAG(profile_id)
    return rag.get_stats()


# ========== 测试 ==========

if __name__ == "__main__":
    async def test():
        print("测试业务知识库 RAG...")
        
        # 测试档案
        test_profile_id = "test_profile"
        test_file_id = "test_file_001"
        
        # 测试内容
        test_content = """
        # GEO 生成式引擎优化指南
        
        ## 什么是 GEO
        
        GEO（Generative Engine Optimization）是针对 AI 搜索引擎的优化策略。
        它关注如何让内容更好地被 ChatGPT、Perplexity、Copilot 等 AI 工具理解和引用。
        
        ## 核心原则
        
        1. 结构化内容：使用清晰的标题层级
        2. 权威可信：提供数据来源和引用
        3. 语义完整：每个段落应包含完整的信息单元
        
        ## 实践技巧
        
        - 使用问答格式组织内容
        - 添加 Schema 结构化标记
        - 优化页面加载速度
        """
        
        # 添加文档
        rag = BusinessKnowledgeRAG(test_profile_id)
        result = await rag.add_document(test_file_id, "geo_guide.md", test_content)
        print(f"添加结果: {result}")
        
        # 检索测试
        results = await rag.retrieve("什么是 GEO 优化")
        print(f"检索结果: {len(results)} 条")
        for r in results:
            print(f"  - {r['filename']}: {r['content'][:50]}... (score: {r['score']:.2f})")
        
        # 统计
        stats = rag.get_stats()
        print(f"统计: {stats}")
        
        # 清理测试数据
        await rag.delete_document(test_file_id)
        print("测试完成，已清理测试数据")
    
    asyncio.run(test())
