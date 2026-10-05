"""
知识库导入脚本 — 读取蒸馏后的知识文件，自动 embedding，写入 PostgreSQL knowledge_chunks 表

支持两种格式：
  - JSONL（每行一条 JSON）
  - JSON Array（标准 JSON 数组）

用法:
    python scripts/import_knowledge.py data/distill_test/xinyi-formula_test.jsonl
    python scripts/import_knowledge.py docs/知识库/星壹/合并输出/distilled_knowledge.json
    python scripts/import_knowledge.py data/knowledge_distilled/  # 导入目录下所有 JSONL/JSON
"""

import os
import sys
import json
import asyncio
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")


async def get_embeddings(texts: list[str], batch_size: int = 10) -> list[list[float]]:
    """批量生成向量（DashScope text-embedding-v4，限制 10 条/批）"""
    import httpx

    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY 未设置")

    url = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"
    all_vectors = []

    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }, json={
                "model": "text-embedding-v4",
                "input": {"texts": batch},
                "parameters": {"dimension": 1024, "text_type": "document"},
            })
            resp.raise_for_status()
            data = resp.json()

        embeddings = data.get("output", {}).get("embeddings", [])
        # 按 text_index 排序
        embeddings.sort(key=lambda x: x.get("text_index", 0))
        all_vectors.extend([e["embedding"] for e in embeddings])

    return all_vectors


def import_chunks(chunks: list[dict], conn):
    """批量导入到 PostgreSQL"""
    cursor = conn.cursor()

    inserted = 0
    skipped = 0
    for chunk in chunks:
        chunk_id = chunk.get("chunk_id", "")
        if not chunk_id:
            skipped += 1
            continue

        # Upsert: 同一 chunk_id 更新
        try:
            embedding = chunk.get("_embedding")
            embedding_str = f"[{','.join(str(v) for v in embedding)}]" if embedding else None

            cursor.execute("""
                INSERT INTO knowledge_chunks (
                    chunk_id, advisor_id, profile_id, title, content, content_type,
                    applicable_to, keywords, use_when, dont_use_when,
                    related_concepts, golden_quotes, metadata,
                    source_file, source_section, embedding
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s::vector
                )
                ON CONFLICT (chunk_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    content = EXCLUDED.content,
                    content_type = EXCLUDED.content_type,
                    applicable_to = EXCLUDED.applicable_to,
                    keywords = EXCLUDED.keywords,
                    use_when = EXCLUDED.use_when,
                    dont_use_when = EXCLUDED.dont_use_when,
                    related_concepts = EXCLUDED.related_concepts,
                    golden_quotes = EXCLUDED.golden_quotes,
                    metadata = EXCLUDED.metadata,
                    source_file = EXCLUDED.source_file,
                    source_section = EXCLUDED.source_section,
                    embedding = EXCLUDED.embedding,
                    updated_at = NOW()
            """, (
                chunk_id,
                chunk.get("advisor_id", ""),
                chunk.get("profile_id"),
                chunk.get("title", ""),
                chunk.get("content", ""),
                chunk.get("content_type", "knowledge"),
                json.dumps(chunk.get("applicable_to", []), ensure_ascii=False),
                json.dumps(chunk.get("keywords", []), ensure_ascii=False),
                chunk.get("use_when", ""),
                chunk.get("dont_use_when", ""),
                json.dumps(chunk.get("related_concepts", []), ensure_ascii=False),
                json.dumps(chunk.get("golden_quotes", []), ensure_ascii=False),
                json.dumps(chunk.get("metadata", {}), ensure_ascii=False),
                chunk.get("source_file", ""),
                chunk.get("source_section", ""),
                embedding_str,
            ))
            inserted += 1
        except Exception as e:
            print(f"  ⚠️ 导入失败 {chunk_id}: {e}")
            conn.rollback()
            skipped += 1
            continue

    conn.commit()
    return inserted, skipped


async def process_file(filepath: Path, conn) -> dict:
    """处理单个知识文件（支持 JSONL 和 JSON Array 两种格式）"""
    chunks = []
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read().strip()

    if not content:
        return {"file": filepath.name, "total": 0, "inserted": 0, "skipped": 0}

    # JSON Array（.json）或 JSONL（.jsonl）自动检测
    if content.startswith("["):
        try:
            chunks = json.loads(content)
            if not isinstance(chunks, list):
                chunks = [chunks]
        except json.JSONDecodeError as e:
            print(f"  ⚠️ JSON Array 解析失败: {e}")
    else:
        for line in content.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                chunk = json.loads(line)
                chunks.append(chunk)
            except json.JSONDecodeError as e:
                print(f"  ⚠️ JSONL 行解析失败: {e}")

    if not chunks:
        return {"file": filepath.name, "total": 0, "inserted": 0, "skipped": 0}

    print(f"  {len(chunks)} 个知识块，生成向量...")

    # 生成 embedding（title + content 拼接作为 embedding 输入）
    texts = [f"{c.get('title', '')}。{c.get('content', '')}" for c in chunks]
    try:
        vectors = await get_embeddings(texts)
        for i, chunk in enumerate(chunks):
            chunk["_embedding"] = vectors[i]
        print(f"  向量生成完成 ({len(vectors)} 个)")
    except Exception as e:
        print(f"  ⚠️ 向量生成失败: {e}")
        return {"file": filepath.name, "total": len(chunks), "inserted": 0, "skipped": len(chunks), "error": str(e)}

    # 导入 PostgreSQL
    inserted, skipped = import_chunks(chunks, conn)

    return {"file": filepath.name, "total": len(chunks), "inserted": inserted, "skipped": skipped}


async def main():
    parser = argparse.ArgumentParser(description="知识库导入脚本")
    parser.add_argument("path", help="JSONL 文件或目录路径")
    parser.add_argument("--dry-run", action="store_true", help="只验证不导入")
    args = parser.parse_args()

    target = Path(args.path)
    if target.is_dir():
        files = sorted(list(target.glob("*.jsonl")) + list(target.glob("*.json")))
    elif target.is_file():
        files = [target]
    else:
        print(f"❌ 路径不存在: {target}")
        sys.exit(1)

    if not files:
        print("❌ 没有找到 JSONL/JSON 文件")
        sys.exit(1)

    print(f"📦 准备导入 {len(files)} 个文件")

    if args.dry_run:
        total = 0
        for f in files:
            with open(f, "r", encoding="utf-8") as fh:
                count = sum(1 for line in fh if line.strip())
            print(f"  {f.name}: {count} 个知识块")
            total += count
        print(f"\n总计: {total} 个知识块（dry-run，未导入）")
        return

    # 连接数据库
    from db.connection import get_connection
    conn = get_connection()

    try:
        results = []
        for filepath in files:
            print(f"\n{'='*50}")
            print(f"导入: {filepath.name}")
            result = await process_file(filepath, conn)
            results.append(result)
            print(f"  → 导入 {result['inserted']} / 跳过 {result['skipped']}")

        print(f"\n{'='*50}")
        print("导入完成")
        print(f"{'='*50}")
        total_inserted = sum(r["inserted"] for r in results)
        total_skipped = sum(r["skipped"] for r in results)
        print(f"  总计: {total_inserted} 导入, {total_skipped} 跳过")

        # 验证
        cursor = conn.cursor()
        cursor.execute("SELECT advisor_id, COUNT(*) FROM knowledge_chunks WHERE is_active = true GROUP BY advisor_id")
        rows = cursor.fetchall()
        print(f"\n📊 当前知识库:")
        for row in rows:
            print(f"  {row['advisor_id']}: {row['count']} 条")
    finally:
        conn.close()


if __name__ == "__main__":
    asyncio.run(main())
