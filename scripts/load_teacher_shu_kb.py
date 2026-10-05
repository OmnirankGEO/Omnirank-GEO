"""从舒老师PDF课程提取文本并导入向量数据库(PDF 经 services.pdf_reader 读取)"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 加载.env环境变量
from dotenv import load_dotenv
load_dotenv()

import asyncio
from pathlib import Path

# 定义PDF文件路径
PDF_FILES = [
    r"c:\AI-Test\AgentsCope-07\New folder\舒老师课程（上）.pdf",
    r"c:\AI-Test\AgentsCope-07\New folder\舒老师课程（下）.docx.pdf",
]

def extract_pages(pdf_path) -> list:
    """每页非空文字层(去首尾空白),按页序。"""
    from services import pdf_reader
    pages = pdf_reader.page_texts(pdf_path)
    print(f"  - 共 {len(pages)} 页")
    return [t.strip() for t in pages if t.strip()]


async def main():
    try:
        from services import pdf_reader  # noqa: F401
    except ImportError:
        print("❌ PDF 解析库未安装，请运行: pip install -r requirements.txt")
        return
    
    from advisors.vectorizer import get_vector_store
    vs = get_vector_store()
    
    total_chunks = 0
    
    for pdf_path in PDF_FILES:
        if not Path(pdf_path).exists():
            print(f"⚠️ 文件不存在: {pdf_path}")
            continue
        
        print(f"\n📄 处理: {Path(pdf_path).name}")
        
        # 打开PDF并提取文本
        all_text = extract_pages(pdf_path)
        
        if not all_text:
            print(f"  - ⚠️ 没有提取到文本")
            continue
        
        # 合并所有文本，按段落分块
        full_text = "\n\n".join(all_text)
        paragraphs = [p.strip() for p in full_text.split('\n') if p.strip() and len(p.strip()) > 30]
        
        print(f"  - 提取了 {len(paragraphs)} 个段落")
        
        # 合并短段落（每块300-500字）
        chunks = []
        current_chunk = ""
        for para in paragraphs:
            if len(current_chunk) + len(para) < 400:
                current_chunk += para + "\n"
            else:
                if current_chunk:
                    chunks.append({"content": current_chunk.strip(), "chunk_id": len(chunks)})
                current_chunk = para + "\n"
        if current_chunk:
            chunks.append({"content": current_chunk.strip(), "chunk_id": len(chunks)})
        
        print(f"  - 合并为 {len(chunks)} 个chunks")
        
        # 导入向量数据库
        if chunks:
            batch_size = 10  # 减少批量大小避免API超时
            for i in range(0, len(chunks), batch_size):
                batch = chunks[i:i+batch_size]
                try:
                    result = await vs.add_chunks(
                        advisor_id="teacher_shu",
                        chunks=batch,
                        filename=Path(pdf_path).name,
                        source="舒老师课程"
                    )
                    if result.get("success"):
                        total_chunks += result.get("chunk_count", 0)
                        print(f"  - 已导入 {min(i+batch_size, len(chunks))}/{len(chunks)} chunks", end="\r")
                    else:
                        print(f"\n  - ⚠️ 导入失败: {result.get('error')}")
                except Exception as e:
                    print(f"\n  - ⚠️ 导入错误: {e}")
            print()
    
    # 最终验证
    stats = vs.get_stats("teacher_shu")
    print(f"\n✅ 完成！teacher_shu 向量数据库中现有 {stats.get('chunk_count')} 个chunks")

if __name__ == "__main__":
    asyncio.run(main())
