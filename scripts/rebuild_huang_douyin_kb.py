"""
重建社恐小黄知识库脚本
1. 清空旧向量表
2. 读取所有MD文件
3. 分块并向量化
4. 存入LanceDB
"""

import os
import sys
import asyncio
from pathlib import Path

# 添加项目路径
sys.path.insert(0, '/app')
os.chdir('/app')

from dotenv import load_dotenv
load_dotenv()


ADVISOR_ID = "huang-douyin"
KNOWLEDGE_DIR = Path(f"/app/data/advisors/{ADVISOR_ID}/knowledge")
DB_PATH = Path("/app/data/vectordb")

EMBEDDING_API = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"
EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_DIM = 1024
BATCH_SIZE = 10


async def get_embedding(texts: list[str]) -> list[list[float]]:
    """调用DashScope获取文本向量"""
    import httpx
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise ValueError("DASHSCOPE_API_KEY not set")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": EMBEDDING_MODEL,
        "input": {"texts": texts},
        "parameters": {"dimension": EMBEDDING_DIM}
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(EMBEDDING_API, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return [item["embedding"] for item in data["output"]["embeddings"]]


def chunk_markdown(text: str, chunk_size: int = 600, overlap: int = 80) -> list[dict]:
    """按Markdown标题分块，保证每块200-600字"""
    import re
    chunks = []

    # 按二级标题分割
    sections = re.split(r'\n(?=## )', text.strip())

    for i, section in enumerate(sections):
        section = section.strip()
        if not section:
            continue

        # 如果段落短于chunk_size，直接添加
        if len(section) <= chunk_size:
            chunks.append({"content": section, "chunk_id": len(chunks)})
            continue

        # 否则按段落进一步分割
        paragraphs = section.split('\n\n')
        current_chunk = ""

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            if len(current_chunk) + len(para) + 2 <= chunk_size:
                current_chunk = (current_chunk + "\n\n" + para).strip()
            else:
                if current_chunk:
                    chunks.append({"content": current_chunk, "chunk_id": len(chunks)})
                # 如果单个段落超过chunk_size，直接作为一个chunk
                if len(para) > chunk_size:
                    # 按句子分割
                    sentences = re.split(r'(?<=[。！？\n])', para)
                    temp = ""
                    for sent in sentences:
                        if len(temp) + len(sent) <= chunk_size:
                            temp += sent
                        else:
                            if temp:
                                chunks.append({"content": temp, "chunk_id": len(chunks)})
                            temp = sent
                    if temp:
                        chunks.append({"content": temp, "chunk_id": len(chunks)})
                    current_chunk = ""
                else:
                    current_chunk = para

        if current_chunk:
            chunks.append({"content": current_chunk, "chunk_id": len(chunks)})

    return chunks


async def rebuild_knowledge_base():
    """重建知识库"""
    print(f"\n{'='*60}")
    print(f"开始重建知识库: {ADVISOR_ID}")
    print(f"知识库目录: {KNOWLEDGE_DIR}")
    print(f"{'='*60}\n")

    # 1. 读取所有MD文件
    md_files = sorted(KNOWLEDGE_DIR.glob("*.md"))
    if not md_files:
        print("❌ 没有找到MD文件！")
        return False

    print(f"找到 {len(md_files)} 个知识库文件:")
    for f in md_files:
        print(f"  - {f.name} ({len(f.read_text(encoding='utf-8'))} 字符)")

    # 2. 分块
    all_chunks = []
    for md_file in md_files:
        content = md_file.read_text(encoding="utf-8")
        chunks = chunk_markdown(content)
        for chunk in chunks:
            chunk["filename"] = md_file.name
            chunk["source"] = md_file.stem
        all_chunks.extend(chunks)
        print(f"  {md_file.name}: {len(chunks)} 块")

    print(f"\n总计: {len(all_chunks)} 个文档块")

    # 3. 批量向量化
    print("\n开始向量化...")
    all_vectors = []

    for i in range(0, len(all_chunks), BATCH_SIZE):
        batch = all_chunks[i:i+BATCH_SIZE]
        texts = [c["content"] for c in batch]

        try:
            vectors = await get_embedding(texts)
            all_vectors.extend(vectors)
            print(f"  已处理 {min(i+BATCH_SIZE, len(all_chunks))}/{len(all_chunks)} 块")
        except Exception as e:
            print(f"  ❌ 向量化失败 (batch {i//BATCH_SIZE}): {e}")
            return False

        # 避免API限流
        if i + BATCH_SIZE < len(all_chunks):
            await asyncio.sleep(0.5)

    print(f"向量化完成: {len(all_vectors)} 个向量")

    # 4. 存储到LanceDB
    print("\n存储到LanceDB...")
    try:
        import lancedb
        from lancedb.pydantic import LanceModel, Vector
        from pydantic import Field
        from typing import Optional

        class KnowledgeChunk(LanceModel):
            id: str
            advisor_id: str
            filename: str
            chunk_id: int
            content: str
            source: Optional[str] = None
            vector: Vector(EMBEDDING_DIM)

        db = lancedb.connect(str(DB_PATH))
        table_name = f"knowledge_advisor_{ADVISOR_ID}"

        # 删除旧表
        try:
            existing = db.table_names()
        except Exception:
            existing = db.list_tables()
        if table_name in existing:
            db.drop_table(table_name)
            print(f"  已删除旧表: {table_name}")

        # 创建新表
        table = db.create_table(table_name, schema=KnowledgeChunk, mode="overwrite")

        # 插入数据
        rows = []
        for chunk, vector in zip(all_chunks, all_vectors):
            rows.append({
                "id": f"{ADVISOR_ID}_{chunk['filename']}_{chunk['chunk_id']}",
                "advisor_id": ADVISOR_ID,
                "filename": chunk["filename"],
                "chunk_id": chunk["chunk_id"],
                "content": chunk["content"],
                "source": chunk.get("source", chunk["filename"]),
                "vector": vector,
            })

        table.add(rows)
        print(f"  ✅ 成功插入 {len(rows)} 条记录到 {table_name}")

        # 验证
        count = table.count_rows()
        print(f"  验证: 表中共 {count} 条记录")

    except Exception as e:
        import traceback
        print(f"  ❌ LanceDB存储失败: {e}")
        traceback.print_exc()
        return False

    # 5. 保存JSON备份（让load_documents可以加载）
    print("\n保存JSON备份...")
    import json
    backup_data = {
        "filename": "knowledge_base_all",
        "file_type": "md",
        "chunk_count": len(all_chunks),
        "chunks": [c["content"] for c in all_chunks],
    }
    backup_path = KNOWLEDGE_DIR / "knowledge_base_all.json"
    backup_path.write_text(json.dumps(backup_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  已保存到: {backup_path}")

    print(f"\n{'='*60}")
    print("✅ 知识库重建完成！")
    print(f"  - 文件数: {len(md_files)}")
    print(f"  - 文档块: {len(all_chunks)}")
    print(f"  - 向量数: {len(all_vectors)}")
    print(f"{'='*60}\n")

    return True


if __name__ == "__main__":
    success = asyncio.run(rebuild_knowledge_base())
    sys.exit(0 if success else 1)
