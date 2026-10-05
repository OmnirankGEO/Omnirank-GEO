"""
小榜 embedding helper · DashScope text-embedding-v4

不依赖 pgvector · embedding 存 JSONB(list of float)字段 · Python 端算 cosine。
145 chunks 规模下,Python cosine 5ms 完事;规模 >5000 再考虑迁 pgvector。

用法:
  # 索引时(批量):
  vectors = await embed_texts(["问题1", "问题2", ...], text_type="document")

  # 查询时(单条):
  vec = await embed_one(user_query, text_type="query")
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from typing import Optional

import httpx

logger = logging.getLogger("xiaobang-embed")

DASHSCOPE_EMBED_URL = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"
EMBEDDING_MODEL = os.getenv("XIAOBANG_EMBED_MODEL", "text-embedding-v4")
EMBEDDING_DIM = 1024
BATCH_SIZE = 10  # DashScope 单次请求最多 10 条
TIMEOUT_S = 30.0


def _api_key() -> str:
    return os.getenv("DASHSCOPE_API_KEY", "").strip()


async def embed_texts(
    texts: list[str],
    text_type: str = "document",
    max_retries: int = 2,
) -> Optional[list[list[float]]]:
    """批量算 embedding · 返回 list[list[float]] 跟 texts 同序

    text_type:
      'document' · 索引时给 KB 用
      'query'    · 查询时给用户问题用(DashScope 双塔模型 query/doc 用不同 prefix)

    失败返回 None · 调用方应能 fallback 到纯 BM25
    """
    if not texts:
        return []
    key = _api_key()
    if not key:
        logger.warning("[xiaobang-embed] DASHSCOPE_API_KEY 未配置 · 跳过 embedding")
        return None

    out: list[list[float]] = []
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        for i in range(0, len(texts), BATCH_SIZE):
            batch = texts[i : i + BATCH_SIZE]
            body = {
                "model": EMBEDDING_MODEL,
                "input": {"texts": batch},
                "parameters": {
                    "dimension": EMBEDDING_DIM,
                    "text_type": text_type,
                },
            }
            attempt = 0
            while attempt <= max_retries:
                try:
                    resp = await client.post(DASHSCOPE_EMBED_URL, headers=headers, json=body)
                    if resp.status_code == 200:
                        data = resp.json()
                        embeddings = data.get("output", {}).get("embeddings", [])
                        embeddings.sort(key=lambda x: x.get("text_index", 0))
                        out.extend([e["embedding"] for e in embeddings])
                        break
                    elif resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                        await asyncio.sleep(2 ** attempt)
                        attempt += 1
                        continue
                    else:
                        logger.warning(
                            "[xiaobang-embed] HTTP %d · 跳过 batch %d(共 %d 条) · resp=%s",
                            resp.status_code, i // BATCH_SIZE, len(batch), resp.text[:200],
                        )
                        return None
                except Exception as e:
                    if attempt < max_retries:
                        await asyncio.sleep(2 ** attempt)
                        attempt += 1
                        continue
                    logger.warning("[xiaobang-embed] 异常 · 跳过 batch: %s", e)
                    return None
    return out if len(out) == len(texts) else None


async def embed_one(text: str, text_type: str = "query") -> Optional[list[float]]:
    """算单条 embedding · 查询时用 · 失败返回 None"""
    if not text or not text.strip():
        return None
    vecs = await embed_texts([text], text_type=text_type)
    if not vecs:
        return None
    return vecs[0]


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    """Python 端算 cosine 相似度 · 输入空向量返回 0.0"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))
