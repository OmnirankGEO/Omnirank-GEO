"""
薛辉知识库向量化脚本 (优化版 - 并行处理)

将预处理生成的MD文件向量化并存入LanceDB
增加并行处理能力，同时处理多个文件
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# 加载环境变量
from dotenv import load_dotenv
load_dotenv(project_root / ".env")

import asyncio
import os
import httpx
import lancedb
from typing import Optional, List, Dict, Any
from lancedb.pydantic import LanceModel, Vector


# 配置
ADVISOR_ID = "xuehui-copywriting"
KNOWLEDGE_DIR = Path("data/advisors/xuehui-copywriting/knowledge/breakdown")
EMBEDDING_API_BASE = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"
EMBEDDING_MODEL = "text-embedding-v4"  # 升级v4：性能提升15%-40%
EMBEDDING_DIM = 1024  # v4支持64-2048维

# 并行配置（v4 RPM=1800，可提高并行度）
CONCURRENT_FILES = 15  # 同时处理的文件数
BATCH_SIZE = 10        # 每次API调用的文本数（API限制最大10）
MAX_RETRIES = 3        # 重试次数


class KnowledgeChunk(LanceModel):
    """知识库文档块模型"""
    id: str
    advisor_id: str
    filename: str
    chunk_id: int
    content: str
    source: Optional[str] = None
    vector: Vector(EMBEDDING_DIM)  # type: ignore


async def batch_embed(texts: List[str], api_key: str, semaphore: asyncio.Semaphore) -> Optional[List[List[float]]]:
    """批量生成向量（带信号量控制并发）"""
    all_vectors = []
    
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i:i + BATCH_SIZE]
        
        for retry in range(MAX_RETRIES):
            async with semaphore:  # 控制并发
                try:
                    async with httpx.AsyncClient(timeout=120) as client:
                        response = await client.post(
                            EMBEDDING_API_BASE,
                            headers={
                                "Authorization": f"Bearer {api_key}",
                                "Content-Type": "application/json",
                            },
                            json={
                                "model": EMBEDDING_MODEL,
                                "input": {"texts": batch},
                                "parameters": {
                                    "dimension": EMBEDDING_DIM,
                                    "text_type": "document",
                                }
                            }
                        )
                        
                        if response.status_code == 200:
                            data = response.json()
                            embeddings = data.get("output", {}).get("embeddings", [])
                            embeddings.sort(key=lambda x: x.get("text_index", 0))
                            batch_vectors = [e["embedding"] for e in embeddings]
                            all_vectors.extend(batch_vectors)
                            break  # 成功，跳出重试循环
                        elif response.status_code == 429:
                            # 限流，等待后重试
                            wait_time = 2 ** retry
                            print(f"  ⚠️ 限流，等待 {wait_time}s 后重试...")
                            await asyncio.sleep(wait_time)
                        else:
                            print(f"  ❌ Embedding失败: {response.status_code}")
                            return None
                except Exception as e:
                    if retry < MAX_RETRIES - 1:
                        await asyncio.sleep(1)
                    else:
                        print(f"  ❌ 请求失败: {e}")
                        return None
    
    return all_vectors


async def process_single_file(
    md_file: Path, 
    db: Any,
    table_name: str,
    api_key: str,
    semaphore: asyncio.Semaphore
) -> Dict[str, Any]:
    """处理单个MD文件"""
    try:
        content = md_file.read_text(encoding='utf-8')
        
        # 分块（按段落）
        chunks = []
        paragraphs = content.split('\n\n')
        current_chunk = ""
        chunk_id = 0
        
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            
            if len(current_chunk) + len(para) > 800:
                if current_chunk:
                    chunks.append({
                        "content": current_chunk.strip(),
                        "chunk_id": chunk_id
                    })
                    chunk_id += 1
                current_chunk = para
            else:
                current_chunk += "\n\n" + para if current_chunk else para
        
        if current_chunk.strip():
            chunks.append({
                "content": current_chunk.strip(),
                "chunk_id": chunk_id
            })
        
        if not chunks:
            return {"success": True, "chunks": 0, "filename": md_file.name}
        
        # 向量化
        contents = [c["content"] for c in chunks]
        vectors = await batch_embed(contents, api_key, semaphore)
        
        if not vectors or len(vectors) != len(chunks):
            return {"success": False, "error": "向量化失败", "filename": md_file.name}
        
        # 构建数据
        data = []
        for i, chunk in enumerate(chunks):
            data.append({
                "id": f"{ADVISOR_ID}_{md_file.name}_{chunk['chunk_id']}",
                "advisor_id": ADVISOR_ID,
                "filename": md_file.name,
                "chunk_id": chunk["chunk_id"],
                "content": chunk["content"],
                "source": f"breakdown/{md_file.name}",
                "vector": vectors[i],
            })
        
        # 存入LanceDB
        table = db.open_table(table_name)
        table.add(data)
        
        return {"success": True, "chunks": len(data), "filename": md_file.name}
        
    except Exception as e:
        return {"success": False, "error": str(e), "filename": md_file.name}


async def vectorize_knowledge():
    """向量化知识库（并行处理）"""
    print("=" * 60)
    print("薛辉知识库向量化 (并行优化版)")
    print("=" * 60)
    
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        print("❌ 未配置 DASHSCOPE_API_KEY")
        return
    
    # 收集所有MD文件
    md_files = list(KNOWLEDGE_DIR.glob("*.md"))
    raw_md_files = list((KNOWLEDGE_DIR / "raw").glob("*.md")) if (KNOWLEDGE_DIR / "raw").exists() else []
    all_files = md_files + raw_md_files
    
    print(f"\n📁 找到 {len(all_files)} 个MD文件待向量化")
    print(f"⚙️  并行度: {CONCURRENT_FILES} 文件")
    
    if not all_files:
        print("❌ 没有找到MD文件！")
        return
    
    # 初始化LanceDB
    db_path = Path("data/vectordb")
    db_path.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(db_path))
    
    table_name = f"knowledge_{ADVISOR_ID}"
    if table_name not in db.table_names():
        db.create_table(table_name, schema=KnowledgeChunk, mode="overwrite")
    
    # 创建信号量控制并发
    semaphore = asyncio.Semaphore(CONCURRENT_FILES)
    
    # 并行处理
    total_chunks = 0
    success_count = 0
    error_count = 0
    
    # 分批并行处理
    batch_size = CONCURRENT_FILES * 2
    for batch_start in range(0, len(all_files), batch_size):
        batch_files = all_files[batch_start:batch_start + batch_size]
        
        tasks = [
            process_single_file(f, db, table_name, api_key, semaphore)
            for f in batch_files
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for result in results:
            if isinstance(result, Exception):
                error_count += 1
                print(f"  ❌ 异常: {result}")
            elif result.get("success"):
                success_count += 1
                total_chunks += result.get("chunks", 0)
            else:
                error_count += 1
                print(f"  ❌ {result.get('filename')}: {result.get('error')}")
        
        # 进度显示
        processed = batch_start + len(batch_files)
        print(f"  进度: {processed}/{len(all_files)} 文件 | "
              f"成功: {success_count} | 失败: {error_count} | 知识块: {total_chunks}")
    
    # 统计
    print()
    print("=" * 60)
    print("向量化完成!")
    print("=" * 60)
    print(f"  成功文件: {success_count}")
    print(f"  失败文件: {error_count}")
    print(f"  总知识块: {total_chunks}")
    
    # 验证
    try:
        table = db.open_table(table_name)
        count = table.count_rows()
        print(f"\n📊 知识库当前状态:")
        print(f"  chunk_count: {count}")
    except Exception as e:
        print(f"  验证失败: {e}")


if __name__ == "__main__":
    asyncio.run(vectorize_knowledge())
