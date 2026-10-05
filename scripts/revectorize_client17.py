"""
重新向量化客户17的知识库
用法: cd geo_agentscope && python scripts/revectorize_client17.py
"""
import asyncio
import sys
import os

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    from services.knowledge_pipeline import get_knowledge_pipeline

    brand_id = 17
    kb_file = os.path.join("data", "knowledge", "clients", str(brand_id), "揭阳东山片区高品质现房楼盘全面解析.pdf")

    if not os.path.exists(kb_file):
        print(f"[ERROR] 文件不存在: {kb_file}")
        return

    # 读取原文
    with open(kb_file, "r", encoding="utf-8") as f:
        content = f.read()
    print(f"[INFO] 已读取知识库文件: {len(content)} 字符")

    # 删除旧的 LanceDB 数据
    import shutil
    lance_dir = os.path.join("data", "vectordb", f"knowledge_client_{brand_id}.lance")
    if os.path.exists(lance_dir):
        shutil.rmtree(lance_dir)
        print(f"[INFO] 已删除旧向量库: {lance_dir}")

    # 重新处理
    pipeline = get_knowledge_pipeline()
    print("[INFO] 开始重新处理（LLM清洗 + 向量化）...")

    result = await pipeline.process_document(
        content=content,
        filename="揭阳东山片区高品质现房楼盘全面解析.pdf",
        brand_id=brand_id,
        skip_clean=False
    )

    if result.success:
        print(f"[SUCCESS] 向量化完成!")
        print(f"  - Chunks: {result.chunk_count}")
        print(f"  - Knowledge Points: {result.knowledge_point_count}")
        print(f"  - Keywords: {result.keywords}")
        print(f"  - 耗时: {result.processing_time_ms}ms")
    else:
        print(f"[ERROR] 处理失败: {result.error}")
        print("[INFO] 尝试跳过LLM清洗，直接向量化原文...")

        # 删除可能残留的数据
        if os.path.exists(lance_dir):
            shutil.rmtree(lance_dir)

        result2 = await pipeline.process_document(
            content=content,
            filename="揭阳东山片区高品质现房楼盘全面解析.pdf",
            brand_id=brand_id,
            skip_clean=True  # 跳过清洗直接分块向量化
        )
        if result2.success:
            print(f"[SUCCESS] 向量化完成（跳过清洗）!")
            print(f"  - Chunks: {result2.chunk_count}")
        else:
            print(f"[ERROR] 再次失败: {result2.error}")


if __name__ == "__main__":
    asyncio.run(main())
