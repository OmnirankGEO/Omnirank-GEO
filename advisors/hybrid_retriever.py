"""
混合检索引擎 — pgvector 向量检索 + BM25 全文检索 + RRF 融合 + Reranker 精排

替代现有 LanceDB 纯向量检索，提供更精准的知识库检索能力。
"""

import os
import json
import asyncio
import logging
import re
from typing import Optional

import httpx

logger = logging.getLogger("HybridRetriever")

# DashScope embedding 配置
EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_DIM = 1024
EMBEDDING_URL = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"

# Rerank 配置
RERANK_MODEL = os.getenv("SOCIAL_RERANK_MODEL", "qwen3-rerank")
RERANK_URL = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"

# 检索参数
DEFAULT_VECTOR_TOP_K = 20      # 向量粗检索数量
DEFAULT_BM25_TOP_K = 20        # BM25 粗检索数量
DEFAULT_FINAL_TOP_K = 5        # 最终返回数量
RELEVANCE_THRESHOLD = 0.3      # Reranker 相关性阈值（0.3 保留更多候选，宁多勿漏）
RRF_K = 60                     # RRF 融合参数


def _keyword_terms(query: str, limit: int = 14) -> list[str]:
    """Extract practical fallback terms for Chinese ILIKE retrieval."""
    text = str(query or "").lower()
    stop = {"为什么", "不能", "可以", "一个", "这个", "那个", "怎么", "如何", "专业", "边界"}
    terms: list[str] = []
    priority_terms = [
        "皮肤", "护理", "敏感肌", "屏障", "医美", "健康", "医生", "治疗", "根治", "副作用",
        "一次", "见效", "永久", "改善", "承诺", "风险", "案例", "广告", "爆量", "成交",
        "法律", "事故", "证据", "赔偿", "考公", "面试", "保过", "留学", "移民", "获批",
    ]
    terms.extend(term for term in priority_terms if term in text)
    for part in re.findall(r"[a-z0-9][a-z0-9_\-]{1,}|[\u4e00-\u9fff]{2,24}", text):
        if re.fullmatch(r"[\u4e00-\u9fff]+", part) and len(part) > 6:
            for n in (3, 2):
                for i in range(0, len(part) - n + 1):
                    terms.append(part[i:i + n])
        else:
            terms.append(part)
    unique: list[str] = []
    seen = set()
    for term in terms:
        if term in stop or term in seen:
            continue
        seen.add(term)
        unique.append(term)
        if len(unique) >= limit:
            break
    return unique


async def _embed(text: str, *, text_type: str = "query") -> Optional[list[float]]:
    """生成单条文本的向量"""
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        return None

    try:
        from tools.llm_call_tracker import llm_track, usage_from_response_payload

        async with httpx.AsyncClient(timeout=60) as client:
            async with llm_track(
                "hybrid_retriever_embedding",
                "dashscope",
                model=EMBEDDING_MODEL,
                metadata={"text_type": text_type},
            ) as tracker:
                resp = await client.post(EMBEDDING_URL, headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                }, json={
                    "model": EMBEDDING_MODEL,
                    "input": {"texts": [text]},
                    "parameters": {"dimension": EMBEDDING_DIM, "text_type": text_type},
                })
                data = resp.json()
                input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data)
                tracker.record(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    success=resp.status_code < 400,
                    error_msg=None if resp.status_code < 400 else resp.text[:200],
                )
            resp.raise_for_status()

        embeddings = data.get("output", {}).get("embeddings", [])
        if embeddings:
            return embeddings[0]["embedding"]
    except Exception as e:
        logger.warning(f"Embedding 生成失败: {e}")

    return None


async def _rerank(query: str, documents: list[dict], top_n: int = 5) -> list[dict]:
    """调用 DashScope gte-rerank 重排序"""
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key or not documents:
        return documents[:top_n]

    try:
        # 构建 rerank 输入
        doc_texts = [d.get("content", d.get("title", ""))[:2000] for d in documents]
        from tools.llm_call_tracker import llm_track, usage_from_response_payload

        async with httpx.AsyncClient(timeout=60) as client:
            async with llm_track(
                "hybrid_retriever_rerank",
                "dashscope",
                model=RERANK_MODEL,
                metadata={"documents": len(documents), "top_n": top_n},
            ) as tracker:
                resp = await client.post(RERANK_URL, headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                }, json={
                    "model": RERANK_MODEL,
                    "input": {
                        "query": query,
                        "documents": doc_texts,
                    },
                    "parameters": {
                        "top_n": min(top_n * 2, len(documents)),  # 多取一些再过滤
                        "return_documents": False,
                    },
                })
                data = resp.json()
                input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data)
                tracker.record(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    success=resp.status_code < 400,
                    error_msg=None if resp.status_code < 400 else resp.text[:200],
                )
            resp.raise_for_status()

        results = data.get("output", {}).get("results", [])
        if not results:
            return documents[:top_n]

        # 按 relevance_score 排序，过滤低相关性
        reranked = []
        for r in results:
            idx = r.get("index", 0)
            score = r.get("relevance_score", 0)
            if score >= RELEVANCE_THRESHOLD and idx < len(documents):
                doc = documents[idx].copy()
                doc["rerank_score"] = score
                reranked.append(doc)

        reranked.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
        if reranked:
            return reranked[:top_n]

        logger.info("Rerank 过滤后为空，降级返回融合召回结果")
        return documents[:top_n]

    except Exception as e:
        logger.warning(f"Rerank 失败，降级为粗排结果: {e}")
        return documents[:top_n]


def _rrf_fusion(vector_results: list[dict], bm25_results: list[dict], k: int = RRF_K) -> list[dict]:
    """Reciprocal Rank Fusion — 合并向量和 BM25 检索结果"""
    scores = {}  # chunk_id → (rrf_score, doc)

    for rank, doc in enumerate(vector_results):
        cid = doc.get("chunk_id", doc.get("id", str(rank)))
        rrf_score = 1.0 / (k + rank + 1)
        if cid in scores:
            scores[cid] = (scores[cid][0] + rrf_score, doc)
        else:
            scores[cid] = (rrf_score, doc)

    for rank, doc in enumerate(bm25_results):
        cid = doc.get("chunk_id", doc.get("id", str(rank)))
        rrf_score = 1.0 / (k + rank + 1)
        if cid in scores:
            scores[cid] = (scores[cid][0] + rrf_score, scores[cid][1])
        else:
            scores[cid] = (rrf_score, doc)

    # 按 RRF 分数排序
    fused = [(score, doc) for score, doc in scores.values()]
    fused.sort(key=lambda x: x[0], reverse=True)

    results = []
    for score, doc in fused:
        doc["rrf_score"] = score
        results.append(doc)

    return results


async def hybrid_search(
    query: str,
    advisor_id: str,
    profile_id: Optional[str] = None,
    top_k: int = DEFAULT_FINAL_TOP_K,
    content_type: Optional[str] = None,
    use_rerank: bool = True,
) -> list[dict]:
    """
    混合检索：pgvector 向量 + BM25 全文 + RRF 融合 + Reranker 精排

    Args:
        query: 检索查询
        advisor_id: 顾问ID
        profile_id: 用户ID（可选，查用户级知识）
        top_k: 返回数量
        content_type: 过滤知识类型（methodology/case/rule/trend/framework）
        use_rerank: 是否使用 Reranker 精排

    Returns:
        按相关性排序的知识块列表
    """
    from db.connection import get_connection

    # 1. 生成查询向量
    query_vector = await _embed(query)
    if not query_vector:
        logger.warning("查询向量生成失败，仅使用 BM25")

    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 构建过滤条件
        filters = ["is_active = true"]
        params = []

        # advisor_id 过滤（支持查顾问级+用户级知识）
        if profile_id:
            filters.append("(advisor_id = %s OR profile_id = %s)")
            params.extend([advisor_id, profile_id])
        else:
            filters.append("advisor_id = %s")
            params.append(advisor_id)

        if content_type:
            filters.append("content_type = %s")
            params.append(content_type)

        where_clause = " AND ".join(filters)

        # 2. 向量检索 top-20
        vector_results = []
        if query_vector:
            vector_str = f"[{','.join(str(v) for v in query_vector)}]"
            vector_sql = f"""
                SELECT chunk_id, title, content, content_type,
                       applicable_to, keywords, golden_quotes,
                       use_when, source_file,
                       1 - (embedding <=> %s::vector) AS vector_score
                FROM knowledge_chunks
                WHERE {where_clause} AND embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            """
            try:
                cursor.execute(vector_sql, [vector_str] + params + [vector_str, DEFAULT_VECTOR_TOP_K])
                vector_results = [dict(row) for row in cursor.fetchall()]
            except Exception as e:
                conn.rollback()
                logger.warning(f"向量检索失败，继续使用 BM25/关键词检索: {e}")

        # 3. BM25 全文检索 top-20
        # 中文分词：用 simple 配置 + plainto_tsquery
        bm25_sql = f"""
            SELECT chunk_id, title, content, content_type,
                   applicable_to, keywords, golden_quotes,
                   use_when, source_file,
                   ts_rank_cd(tsv, plainto_tsquery('simple', %s), 32) AS bm25_score
            FROM knowledge_chunks
            WHERE {where_clause} AND tsv @@ plainto_tsquery('simple', %s)
            ORDER BY bm25_score DESC
            LIMIT %s
        """
        bm25_results = []
        try:
            cursor.execute(bm25_sql, [query] + params + [query, DEFAULT_BM25_TOP_K])
            bm25_results = [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            conn.rollback()
            logger.warning(f"BM25 检索失败，准备降级为 ILIKE 关键词检索: {e}")

        if not bm25_results:
            terms = _keyword_terms(query)
            if terms:
                keyword_clauses = []
                keyword_params = []
                for term in terms:
                    keyword_clauses.append("(content ILIKE %s OR title ILIKE %s)")
                    pattern = f"%{term}%"
                    keyword_params.extend([pattern, pattern])
                keyword_sql = f"""
                    SELECT chunk_id, title, content, content_type,
                           applicable_to, keywords, golden_quotes,
                           use_when, source_file,
                           0.1 AS bm25_score
                    FROM knowledge_chunks
                    WHERE {where_clause}
                      AND ({" OR ".join(keyword_clauses)})
                    ORDER BY updated_at DESC
                    LIMIT %s
                """
                try:
                    cursor.execute(keyword_sql, params + keyword_params + [DEFAULT_BM25_TOP_K * 2])
                    keyword_results = [dict(row) for row in cursor.fetchall()]
                    if keyword_results:
                        def _kw_score(doc: dict) -> float:
                            haystack = f"{doc.get('title', '')}\n{doc.get('content', '')}"
                            hits = sum(1 for term in terms if term and term in haystack)
                            return hits / max(1, min(len(terms), 10))

                        for doc in keyword_results:
                            doc["bm25_score"] = round(_kw_score(doc), 4)
                        keyword_results.sort(key=lambda item: item.get("bm25_score", 0), reverse=True)
                        bm25_results = keyword_results[:DEFAULT_BM25_TOP_K]
                        logger.info(f"ILIKE 关键词降级命中 {len(bm25_results)} 条: {terms[:6]}")
                except Exception as keyword_error:
                    conn.rollback()
                    logger.warning(f"ILIKE 关键词检索失败: {keyword_error}")

        if not bm25_results:
            ilike_sql = f"""
                SELECT chunk_id, title, content, content_type,
                       applicable_to, keywords, golden_quotes,
                       use_when, source_file,
                       0.1 AS bm25_score
                FROM knowledge_chunks
                WHERE {where_clause}
                  AND (content ILIKE %s OR title ILIKE %s)
                ORDER BY updated_at DESC
                LIMIT %s
            """
            like_query = f"%{query[:80]}%"
            cursor.execute(ilike_sql, params + [like_query, like_query, DEFAULT_BM25_TOP_K])
            bm25_results = [dict(row) for row in cursor.fetchall()]

    finally:
        conn.close()

    # 4. RRF 融合
    if vector_results and bm25_results:
        fused = _rrf_fusion(vector_results, bm25_results)
    elif vector_results:
        fused = vector_results
    elif bm25_results:
        fused = bm25_results
    else:
        return []

    # 5. Reranker 精排
    if use_rerank and fused:
        results = await _rerank(query, fused, top_n=top_k)
    else:
        results = fused[:top_k]

    # 6. 解析 JSONB 字段
    for r in results:
        for field in ["applicable_to", "keywords", "golden_quotes"]:
            val = r.get(field)
            if isinstance(val, str):
                try:
                    r[field] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    r[field] = []

    logger.info(
        f"[HybridRetriever] query='{query[:30]}' advisor={advisor_id} "
        f"vector={len(vector_results)} bm25={len(bm25_results)} "
        f"fused={len(fused)} final={len(results)}"
    )

    return results


async def search_knowledge(
    query: str,
    advisor_id: str,
    top_k: int = 5,
    **kwargs,
) -> list[dict]:
    """
    对外接口 — 替代 vectorizer.search()
    返回格式兼容现有代码：[{"content": "...", "score": 0.85, ...}]
    """
    results = await hybrid_search(query, advisor_id, top_k=top_k, **kwargs)

    # 兼容现有格式
    formatted = []
    for r in results:
        formatted.append({
            "content": r.get("content", ""),
            "title": r.get("title", ""),
            "score": r.get("rerank_score", r.get("rrf_score", r.get("vector_score", r.get("bm25_score", 0)))),
            "content_type": r.get("content_type", ""),
            "keywords": r.get("keywords", []),
            "applicable_to": r.get("applicable_to", []),
            "golden_quotes": r.get("golden_quotes", []),
            "source_file": r.get("source_file", ""),
        })

    return formatted
