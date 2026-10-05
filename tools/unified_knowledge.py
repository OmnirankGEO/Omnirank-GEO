"""
统一知识库服务 - Unified Knowledge RAG
支持角色/客户两层知识库检索（身份隔离，非知识库隔离）

创建时间: 2026-02-02
安全原则: 不修改任何现有数据库表结构
"""

import os
import json
import hashlib
from pathlib import Path
from typing import Dict, List, Any, Optional
from datetime import datetime

# 基础路径配置
BASE_DIR = Path(__file__).parent.parent
KNOWLEDGE_ROOT = BASE_DIR / "data" / "knowledge"
VECTORDB_ROOT = BASE_DIR / "data" / "vectordb"


class UnifiedKnowledgeRAG:
    """
    统一知识库检索服务

    两层知识库（身份隔离）：
    1. 角色层（roles）- 顾问/员工私有知识
    2. 客户层（clients）- 按brand_id隔离的客户资料

    检索优先级：角色私有 > 客户库
    """

    def __init__(self):
        self.clients_path = KNOWLEDGE_ROOT / "clients"
        self.roles_path = KNOWLEDGE_ROOT / "roles"
        self.public_path = KNOWLEDGE_ROOT / "public"  # 公共知识库（所有用户共享）

        # 确保目录存在
        self._ensure_directories()

        # 缓存已加载的文档（避免重复读取）
        self._document_cache: Dict[str, List[Dict]] = {}

    def _ensure_directories(self):
        """确保知识库目录存在"""
        for path in [self.clients_path, self.roles_path, self.public_path]:
            path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _is_safe_component(value) -> bool:
        """[GEO-R1-CAN-147] 校验单级路径组件：非空、无路径分隔符、非 '.'/'..'、非绝对路径。

        用于阻断 caller-controlled 的 kb_id/role_type 做路径穿越（绝对路径重置 join、
        '..' 逃逸根目录），配合 _assert_contained 双保险。
        """
        s = str(value).strip()
        if not s or s in (".", ".."):
            return False
        if "/" in s or "\\" in s or "\x00" in s:
            return False
        if os.path.isabs(s):
            return False
        # Windows 盘符 (C:) / UNC 前缀防护
        if len(s) >= 2 and s[1] == ":":
            return False
        return True

    @staticmethod
    def _assert_contained(target_dir: Path, root: Path) -> None:
        """[GEO-R1-CAN-147] 确保 target_dir 解析后仍在 root 之内，越界抛 ValueError。"""
        resolved = target_dir.resolve()
        root_resolved = root.resolve()
        resolved.relative_to(root_resolved)  # 越界则 raise ValueError

    def _resolve_target_dir(self, kb_type: str, kb_id: str = None, role_type: str = None):
        """根据 kb_type 返回对应的目录，统一处理 public/client/role 三类。

        Returns:
            (target_dir, error_msg) — 错误时 target_dir=None, error_msg 非空
        """
        if kb_type == "public":
            return self.public_path, None
        if kb_type == "client":
            if not kb_id:
                return None, "client 类型需要指定 kb_id"
            # [GEO-R1-CAN-147] kb_id 直接拼进路径，必须校验为安全单级组件 + 越界兜底
            if not self._is_safe_component(kb_id):
                return None, "非法的 kb_id"
            target = self.clients_path / str(kb_id).strip()
            try:
                self._assert_contained(target, self.clients_path)
            except ValueError:
                return None, "非法的 kb_id（路径越界）"
            return target, None
        if kb_type == "role":
            if not role_type or not kb_id:
                return None, "role 类型需要指定 role_type 和 kb_id"
            # [GEO-R1-CAN-147] role_type/kb_id 均 caller-controlled 拼进路径，逐段校验 + 越界兜底
            if not self._is_safe_component(role_type) or not self._is_safe_component(kb_id):
                return None, "非法的 role_type 或 kb_id"
            target = self.roles_path / f"{str(role_type).strip()}s" / str(kb_id).strip()
            try:
                self._assert_contained(target, self.roles_path)
            except ValueError:
                return None, "非法的 role_type 或 kb_id（路径越界）"
            return target, None
        return None, f"未知的知识库类型: {kb_type}"
    
    # ==================== 检索方法 ====================
    
    async def retrieve(
        self,
        query: str,
        role_type: str = None,      # "advisor" | "employee"
        role_id: str = None,        # 如 "huang-douyin" | "marketing_director"
        brand_id: int = None,       # 客户ID
        top_k: int = 5,
        use_client: bool = True,    # 是否查客户库
        use_role: bool = True,      # 是否查角色库
    ) -> List[Dict[str, Any]]:
        """
        分层检索：角色库 → 客户库

        Args:
            query: 检索查询
            role_type: 角色类型 advisor/employee
            role_id: 角色ID
            brand_id: 客户ID（用于获取客户专属知识）
            top_k: 返回结果数量
            use_client: 是否使用客户知识库
            use_role: 是否使用角色私有库

        Returns:
            检索结果列表，每项包含 content, source, score, kb_type
        """
        all_results = []

        # 1. 角色私有库（最高优先级）
        if use_role and role_type and role_id:
            role_results = await self._search_role_kb(role_type, role_id, query, top_k)
            for r in role_results:
                r["kb_type"] = "role"
                r["priority"] = 1
            all_results.extend(role_results)
            print(f"[UnifiedKB] 🔍 角色库({role_type}/{role_id}): {len(role_results)}条结果")

        # 2. 客户知识库
        if use_client and brand_id:
            client_results = await self._search_client_kb(brand_id, query, top_k)
            for r in client_results:
                r["kb_type"] = "client"
                r["priority"] = 2
            all_results.extend(client_results)
            print(f"[UnifiedKB] 🔍 客户库(brand_id={brand_id}): {len(client_results)}条结果")

        # 去重、排序、截断
        final_results = self._dedupe_and_rank(all_results, top_k)
        print(f"[UnifiedKB] ✅ 最终返回: {len(final_results)}条结果")

        return final_results

    async def _search_client_kb(self, brand_id: int, query: str, top_k: int) -> List[Dict]:
        """搜索客户知识库（优先返回_system_prompts.md术语提示）"""
        client_path = self.clients_path / str(brand_id)
        results = []

        # ✅ 优先级1：读取_system_prompts.md（术语提示文件）
        system_prompts_file = client_path / "_system_prompts.md"
        if client_path.exists() and system_prompts_file.exists():
            try:
                content = system_prompts_file.read_text(encoding="utf-8")
                results.append({
                    "content": content[:2000],  # 术语提示全量返回
                    "full_content": content,
                    "source": "_system_prompts.md",
                    "path": str(system_prompts_file),
                    "score": 999.0,  # 最高优先级
                    "kb_type": "system_prompt",
                })
                print(f"[UnifiedKB] ✅ 加载客户术语提示: {len(content)} 字符")
            except Exception as e:
                print(f"[UnifiedKB] ⚠️ 读取术语提示失败: {e}")

        # 优先级2：LanceDB 向量检索（精准匹配多个chunk）
        vector_results = await self._search_lancedb(brand_id, query, top_k)
        if vector_results:
            print(f"[UnifiedKB] 🔍 LanceDB向量检索: {len(vector_results)}条结果")
            results.extend(vector_results)

        # 优先级3：文本检索补充（向量检索不足时兜底）
        if client_path.exists() and len(vector_results) < top_k:
            search_results = await self._search_directory(client_path, query, top_k)
            # 过滤掉_system_prompts.md和与向量结果重复的内容
            search_results = [r for r in search_results if r.get("source") != "_system_prompts.md"]
            # 去重：检查内容前100字符是否已存在
            existing_prefixes = {r["content"][:100] for r in results}
            for r in search_results:
                if r["content"][:100] not in existing_prefixes:
                    results.append(r)

        return results[:top_k]

    async def _search_lancedb(self, brand_id: int, query: str, top_k: int) -> List[Dict]:
        """从 LanceDB 向量库中检索（当文本检索无结果时的兜底）"""
        try:
            import lancedb
            if not VECTORDB_ROOT.exists():
                return []

            db = lancedb.connect(str(VECTORDB_ROOT))
            table_name = f"knowledge_client_{brand_id}"
            try:
                table = db.open_table(table_name)
            except Exception:
                return []

            # 使用 embedding API 将 query 转为向量
            query_vector = await self._embed_query(query)
            if not query_vector:
                # 向量化失败，降级为全文扫描 LanceDB 表中的内容
                try:
                    all_data = table.to_pandas()
                    if all_data.empty:
                        return []
                    # 简单关键词匹配
                    import re
                    chinese_chars = re.findall(r'[\u4e00-\u9fff]{2,}', query.lower())
                    results = []
                    for _, row in all_data.iterrows():
                        content = str(row.get("content", "")).lower()
                        hits = sum(1 for kw in chinese_chars if kw in content)
                        if hits > 0:
                            results.append({
                                "content": str(row.get("content", ""))[:800],
                                "source": str(row.get("filename", "LanceDB")),
                                "score": hits / max(len(chinese_chars), 1),
                            })
                    results.sort(key=lambda x: x["score"], reverse=True)
                    return results[:top_k]
                except Exception:
                    return []

            # 向量近似搜索
            search_results = table.search(query_vector).limit(top_k).to_pandas()
            results = []
            for _, row in search_results.iterrows():
                results.append({
                    "content": str(row.get("content", ""))[:800],
                    "source": str(row.get("filename", "LanceDB")),
                    "score": float(1.0 / (1.0 + row.get("_distance", 1.0))),  # 距离→相似度
                })
            return results

        except Exception as e:
            print(f"[UnifiedKB] ⚠️ LanceDB检索失败: {e}")
            return []

    async def _embed_query(self, query: str) -> list:
        """将查询文本转为向量"""
        try:
            import httpx
            api_key = os.environ.get("DASHSCOPE_API_KEY", "")
            if not api_key:
                return []

            from tools.llm_call_tracker import llm_track, usage_from_response_payload

            async with httpx.AsyncClient(timeout=15) as client:
                async with llm_track(
                    "unified_knowledge_embedding",
                    "dashscope",
                    model="text-embedding-v4",
                    metadata={"text_type": "query"},
                ) as tracker:
                    response = await client.post(
                        "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json"
                        },
                        json={
                            "model": "text-embedding-v4",
                            "input": {"texts": [query]},
                            "parameters": {"dimension": 1024, "text_type": "query"}
                        }
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
                    if embeddings:
                        return embeddings[0]["embedding"]
            return []
        except Exception:
            return []
    
    async def _search_role_kb(self, role_type: str, role_id: str, query: str, top_k: int) -> List[Dict]:
        """搜索角色私有库"""
        role_path = self.roles_path / f"{role_type}s" / role_id  # advisors/huang-douyin
        if not role_path.exists():
            return []
        return await self._search_directory(role_path, query, top_k)
    
    async def _search_directory(self, dir_path: Path, query: str, top_k: int) -> List[Dict]:
        """
        在指定目录中搜索文档（中文优化版：使用字符匹配和核心词提取）
        
        TODO: 后续可接入向量检索（LanceDB）
        """
        results = []
        
        if not dir_path.exists():
            return results
        
        # 加载目录下所有文档
        documents = self._load_documents_from_dir(dir_path)
        
        # ===== 中文检索优化 =====
        # 1. 提取核心关键词（去除常见虚词）
        stopwords = {'的', '是', '在', '了', '和', '与', '或', '有', '为', '年', '个', '吗', '呢', '啊', '哪', '什么', '怎么', '如何', 
                     '一', '二', '三', '四', '五', '六', '七', '八', '九', '十', '大', '小', '多少'}
        
        # 清理查询：提取中文词和英文词
        query_clean = query.lower()
        # 提取所有有意义的词（2字符以上的连续片段）
        import re
        chinese_chars = re.findall(r'[\u4e00-\u9fff]{2,}', query_clean)
        english_words = re.findall(r'[a-z]{2,}|[0-9]+', query_clean)
        
        # 过滤停用词
        query_keywords = [w for w in chinese_chars + english_words if w not in stopwords]
        
        # 添加原始查询中的核心部分（如：全域上榜、GEO、优化）
        if not query_keywords:
            query_keywords = [query_clean[:50]]  # fallback
        
        print(f"[UnifiedKB] 🔍 检索关键词: {query_keywords[:10]}")
        
        for doc in documents:
            content = doc.get("content", "").lower()
            
            # 计算匹配度：关键词在内容中出现的次数
            total_matches = 0
            keyword_hits = 0
            
            for kw in query_keywords:
                count = content.count(kw)
                if count > 0:
                    keyword_hits += 1
                    total_matches += min(count, 10)  # 单词最多计10次，避免过度权重
            
            if keyword_hits > 0:
                # 综合评分：命中词数 + 总匹配次数（归一化）
                score = keyword_hits / len(query_keywords) + (total_matches / 50)
                results.append({
                    "content": doc["content"][:800],  # 提高截取长度
                    "full_content": doc["content"],
                    "source": doc["filename"],
                    "path": str(doc["path"]),
                    "score": score,
                    "keyword_hits": keyword_hits,
                })
        
        # 按匹配度排序
        results.sort(key=lambda x: x["score"], reverse=True)
        
        return results[:top_k]
    
    def _load_documents_from_dir(self, dir_path: Path) -> List[Dict]:
        """加载目录下所有文档"""
        cache_key = str(dir_path)
        
        # 检查缓存
        if cache_key in self._document_cache:
            return self._document_cache[cache_key]
        
        documents = []
        
        # 支持的文件类型（上传流程会将二进制文档提取为文本再保存）
        supported_extensions = {".md", ".txt", ".json", ".jsonl", ".pdf", ".doc", ".docx", ".pptx", ".csv"}
        
        for file_path in dir_path.rglob("*"):
            if file_path.is_file() and file_path.suffix.lower() in supported_extensions:
                try:
                    content = file_path.read_text(encoding="utf-8")
                    documents.append({
                        "filename": file_path.name,
                        "path": file_path,
                        "content": content,
                        "size": len(content),
                    })
                except Exception as e:
                    print(f"[UnifiedKB] ⚠️ 无法读取文件 {file_path}: {e}")
        
        # 缓存结果
        self._document_cache[cache_key] = documents
        
        return documents
    
    def _dedupe_and_rank(self, results: List[Dict], top_k: int) -> List[Dict]:
        """去重（基于内容hash）、排序（优先级+得分）、截断"""
        seen_hashes = set()
        unique_results = []
        
        # 先按优先级排序，同优先级按得分排序
        results.sort(key=lambda x: (x.get("priority", 99), -x.get("score", 0)))
        
        for r in results:
            # 计算内容hash用于去重
            content_hash = hashlib.md5(r.get("content", "").encode()).hexdigest()[:16]
            if content_hash not in seen_hashes:
                seen_hashes.add(content_hash)
                unique_results.append(r)
                
                if len(unique_results) >= top_k:
                    break
        
        return unique_results
    
    # ==================== 管理方法 ====================
    
    def add_document(
        self,
        kb_type: str,           # "public" | "client" | "role"
        kb_id: str,             # brand_id 或 role_id
        role_type: str = None,  # "advisor" | "employee"（仅kb_type=role时需要）
        filename: str = None,
        content: str = None,
    ) -> Dict[str, Any]:
        """
        添加文档到知识库
        
        Args:
            kb_type: 知识库类型
            kb_id: 知识库ID
            role_type: 角色类型（仅role库需要）
            filename: 文件名
            content: 文件内容
        
        Returns:
            {"success": True, "path": "..."}
        """
        try:
            target_dir, err = self._resolve_target_dir(kb_type, kb_id, role_type)
            if err:
                return {"success": False, "error": err}

            # [GEO-R1-CAN-147] filename 同样 caller-controlled，basename 化防止 '..'/绝对路径穿越
            safe_name = Path(str(filename or "").replace("\\", "/")).name
            if not safe_name or safe_name in (".", ".."):
                return {"success": False, "error": "非法文件名"}

            # 确保目录存在
            target_dir.mkdir(parents=True, exist_ok=True)

            # 写入文件
            file_path = target_dir / safe_name
            file_path.write_text(content, encoding="utf-8")
            
            # 清除缓存
            cache_key = str(target_dir)
            if cache_key in self._document_cache:
                del self._document_cache[cache_key]
            
            return {
                "success": True,
                "path": str(file_path),
                "size": len(content),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def delete_document(
        self,
        kb_type: str,
        kb_id: str = None,
        role_type: str = None,
        filename: str = None,
    ) -> Dict[str, Any]:
        """删除知识库中的文档"""
        try:
            target_dir, err = self._resolve_target_dir(kb_type, kb_id, role_type)
            if err:
                return {"success": False, "error": err}

            safe_filename = (filename or "").replace("\\", "/").strip().lstrip("/")
            parts = [part for part in safe_filename.split("/") if part and part != "."]
            if not parts or any(part == ".." for part in parts):
                return {"success": False, "error": "非法文件路径"}
            safe_filename = "/".join(parts)

            # 支持相对路径（如 cleaned/xxx.json）
            file_path = target_dir / safe_filename
            
            # 如果直接路径不存在，尝试在子目录中搜索
            if not file_path.exists():
                for f in target_dir.rglob(Path(safe_filename).name):
                    if f.is_file():
                        file_path = f
                        break
            
            if not file_path.exists():
                return {"success": False, "error": "文件不存在"}
            
            file_path.unlink()

            if kb_type == "client" and kb_id and safe_filename:
                self._delete_client_vectors(str(kb_id), Path(safe_filename).name)
            
            # 清除缓存
            cache_key = str(target_dir)
            if cache_key in self._document_cache:
                del self._document_cache[cache_key]
            
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _delete_client_vectors(self, brand_id: str, filename: str) -> None:
        """同步删除客户 LanceDB 向量块，避免文件删了但写作仍检索到旧资料。"""
        try:
            import lancedb

            if not VECTORDB_ROOT.exists():
                return

            db = lancedb.connect(str(VECTORDB_ROOT))
            table_name = f"knowledge_client_{brand_id}"
            try:
                table = db.open_table(table_name)
            except Exception:
                return

            escaped = filename.replace("\\", "\\\\").replace('"', '\\"')
            table.delete(f'filename = "{escaped}"')
        except Exception as e:
            print(f"[UnifiedKB] ⚠️ 删除客户向量失败: brand_id={brand_id}, filename={filename}, err={e}")
    
    def list_documents(
        self,
        kb_type: str,
        kb_id: str = None,
        role_type: str = None,
    ) -> Dict[str, Any]:
        """列出知识库中的文档"""
        try:
            target_dir, err = self._resolve_target_dir(kb_type, kb_id, role_type)
            if err:
                return {"success": False, "error": err}

            if not target_dir.exists():
                return {"success": True, "documents": [], "total": 0}
            
            documents = []
            for file_path in target_dir.rglob("*"):
                if file_path.is_file():
                    stat = file_path.stat()
                    documents.append({
                        "filename": file_path.name,
                        "relative_path": str(file_path.relative_to(target_dir)),
                        "size": stat.st_size,
                        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    })
            
            return {
                "success": True,
                "documents": documents,
                "total": len(documents),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def get_stats(self) -> Dict[str, Any]:
        """获取知识库统计信息"""
        stats = {
            "clients": {},
            "roles": {
                "advisors": {},
                "employees": {},
            },
        }
        
        # 统计客户库
        if self.clients_path.exists():
            for client_dir in self.clients_path.iterdir():
                if client_dir.is_dir():
                    stats["clients"][client_dir.name] = self._count_files(client_dir)
        
        # 统计角色库
        advisors_path = self.roles_path / "advisors"
        if advisors_path.exists():
            for role_dir in advisors_path.iterdir():
                if role_dir.is_dir():
                    stats["roles"]["advisors"][role_dir.name] = self._count_files(role_dir)
        
        employees_path = self.roles_path / "employees"
        if employees_path.exists():
            for role_dir in employees_path.iterdir():
                if role_dir.is_dir():
                    stats["roles"]["employees"][role_dir.name] = self._count_files(role_dir)
        
        return stats
    
    def _count_files(self, dir_path: Path) -> Dict[str, int]:
        """统计目录中的文件数和总大小"""
        if not dir_path.exists():
            return {"file_count": 0, "total_size": 0}
        
        file_count = 0
        total_size = 0
        
        for file_path in dir_path.rglob("*"):
            if file_path.is_file():
                file_count += 1
                total_size += file_path.stat().st_size
        
        return {"file_count": file_count, "total_size": total_size}
    
    def clear_cache(self):
        """清除文档缓存"""
        self._document_cache.clear()


# ==================== 全局单例 ====================

_unified_rag: Optional[UnifiedKnowledgeRAG] = None


def get_unified_rag() -> UnifiedKnowledgeRAG:
    """获取统一知识库服务单例"""
    global _unified_rag
    if _unified_rag is None:
        _unified_rag = UnifiedKnowledgeRAG()
    return _unified_rag


# ==================== 导出 ====================

__all__ = [
    "UnifiedKnowledgeRAG",
    "get_unified_rag",
]
