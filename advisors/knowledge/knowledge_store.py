"""
知识库存储封装
后续可替换为 LanceDB / Chroma / FAISS 等向量数据库
"""

import os
import json
from pathlib import Path
from typing import Optional


class KnowledgeStore:
    """
    知识库存储 - 简单版本
    
    当前使用关键词索引，后续可替换为向量数据库:
    - LanceDB (AgentScope推荐)
    - Chroma
    - FAISS
    """
    
    def __init__(self, store_path: str):
        self.store_path = Path(store_path)
        self.store_path.mkdir(parents=True, exist_ok=True)
        self.documents = []
        self.index = []
        self._load()
    
    def _load(self):
        """加载已有文档"""
        index_file = self.store_path / "index.json"
        if index_file.exists():
            with open(index_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.documents = data.get("documents", [])
                self.index = data.get("index", [])
    
    def _save(self):
        """保存索引"""
        index_file = self.store_path / "index.json"
        with open(index_file, "w", encoding="utf-8") as f:
            json.dump({
                "documents": self.documents,
                "index": self.index,
            }, f, ensure_ascii=False, indent=2)
    
    def add_document(
        self, 
        content: str, 
        filename: str,
        metadata: dict = None
    ) -> int:
        """
        添加文档
        
        Returns:
            分块数量
        """
        # 分块
        chunks = self._chunk_text(content)
        
        # 添加到文档列表
        doc_info = {
            "filename": filename,
            "chunk_count": len(chunks),
            "metadata": metadata or {},
        }
        self.documents.append(doc_info)
        
        # 添加到索引
        for i, chunk in enumerate(chunks):
            self.index.append({
                "doc_id": len(self.documents) - 1,
                "chunk_id": i,
                "content": chunk,
                "keywords": self._extract_keywords(chunk),
            })
        
        self._save()
        return len(chunks)
    
    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """搜索相关文档"""
        query_keywords = set(self._extract_keywords(query))
        
        scored = []
        for item in self.index:
            item_keywords = set(item.get("keywords", []))
            overlap = len(query_keywords & item_keywords)
            if overlap > 0:
                scored.append({
                    "content": item["content"],
                    "filename": self.documents[item["doc_id"]]["filename"],
                    "score": overlap,
                })
        
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]
    
    def _chunk_text(self, text: str, chunk_size: int = 500) -> list[str]:
        """文本分块"""
        paragraphs = text.split("\n\n")
        chunks = []
        current = ""
        
        for para in paragraphs:
            if len(current) + len(para) < chunk_size:
                current += para + "\n\n"
            else:
                if current:
                    chunks.append(current.strip())
                current = para + "\n\n"
        
        if current:
            chunks.append(current.strip())
        
        return chunks
    
    def _extract_keywords(self, text: str) -> list[str]:
        """提取关键词"""
        import re
        words = re.findall(r'[\u4e00-\u9fa5a-zA-Z]+', text)
        return [w for w in words if len(w) > 1]
    
    def clear(self):
        """清空知识库"""
        self.documents = []
        self.index = []
        self._save()
