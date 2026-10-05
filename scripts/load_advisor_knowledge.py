"""
顾问知识库快速加载脚本

用途：
1. 为新顾问快速导入知识库文件
2. 检查现有顾问的知识库状态
3. 测试知识库检索功能

使用方法：
    python scripts/load_advisor_knowledge.py <advisor_id> [--init] [--test <query>]

示例：
    # 初始化并加载xuehui-copywriting顾问的知识库
    python scripts/load_advisor_knowledge.py xuehui-copywriting --init
    
    # 测试检索功能
    python scripts/load_advisor_knowledge.py xuehui-copywriting --test "短视频开篇怎么写"
    
    # 查看知识库状态
    python scripts/load_advisor_knowledge.py xuehui-copywriting --status
"""

import os
import sys
import asyncio
import argparse
from pathlib import Path

# 确保项目根目录在path中
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


async def load_knowledge_from_folder(advisor_id: str, knowledge_path: Path):
    """从文件夹加载所有知识库文件"""
    from advisors import get_advisor
    
    advisor = get_advisor(advisor_id)
    if not advisor:
        print(f"❌ 顾问不存在: {advisor_id}")
        return False
    
    print(f"\n📚 加载顾问知识库: {advisor.name} ({advisor_id})")
    print(f"   知识库路径: {knowledge_path}")
    
    if not knowledge_path.exists():
        print(f"❌ 知识库路径不存在: {knowledge_path}")
        return False
    
    # 支持的文件类型
    supported_extensions = ['.md', '.txt', '.json', '.jsonl']
    loaded_count = 0
    
    import json as _json

    for file_path in sorted(knowledge_path.iterdir()):
        if file_path.suffix.lower() not in supported_extensions:
            continue

        # 跳过 .bak 备份文件
        if '.bak' in file_path.name:
            continue

        print(f"\n   📄 加载文件: {file_path.name}")

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                raw = f.read()

            # JSON 文件特殊处理：提取 chunks 数组中的文本
            content = raw
            if file_path.suffix.lower() == '.json':
                try:
                    data = _json.loads(raw)
                    if isinstance(data, dict) and 'chunks' in data:
                        chunks_list = data['chunks']
                        # 过滤掉 PDF 二进制内容（以 %PDF 开头或含大量乱码）
                        text_chunks = []
                        for c in chunks_list:
                            if isinstance(c, str) and not c.startswith('%PDF'):
                                # 检测是否是可读文本（中文或英文字符占比 > 30%）
                                readable = sum(1 for ch in c[:200] if ch.isalpha() or '\u4e00' <= ch <= '\u9fff')
                                if len(c) > 0 and readable / max(len(c[:200]), 1) > 0.3:
                                    text_chunks.append(c)
                        if text_chunks:
                            content = '\n\n'.join(text_chunks)
                            print(f"      📋 从 JSON 提取 {len(text_chunks)}/{len(chunks_list)} 个文本块")
                        else:
                            print(f"      ⚠️ JSON 中无可用文本块（可能是 PDF 二进制），跳过")
                            continue
                except _json.JSONDecodeError:
                    pass  # 不是有效 JSON，当普通文本处理

            # 直接用 vector_store 索引，不用 add_document（它会覆盖原文件）
            from advisors.vectorizer import get_vector_store
            chunks = advisor._chunk_text(content, chunk_size=500)
            chunk_dicts = [{"content": c, "chunk_id": i} for i, c in enumerate(chunks)]
            vector_store = get_vector_store()
            result = await vector_store.add_chunks(
                advisor_id=advisor_id,
                chunks=chunk_dicts,
                filename=file_path.stem,
                source=file_path.name,
            )

            if result.get("success"):
                print(f"      ✅ 成功！分块数: {result.get('chunk_count', 0)}")
                loaded_count += 1
            else:
                print(f"      ❌ 失败: {result.get('error', 'Unknown error')}")

        except Exception as e:
            print(f"      ❌ 读取失败: {e}")
    
    print(f"\n✅ 加载完成！成功加载 {loaded_count} 个文件")
    return True


async def test_retrieval(advisor_id: str, query: str):
    """测试知识库检索"""
    from advisors import get_advisor
    
    advisor = get_advisor(advisor_id)
    if not advisor:
        print(f"❌ 顾问不存在: {advisor_id}")
        return
    
    print(f"\n🔍 测试知识库检索: {advisor.name}")
    print(f"   查询: {query}")
    print("-" * 50)
    
    results = await advisor.retrieve_knowledge(query, top_k=5)
    
    if not results:
        print("⚠️ 没有检索到相关结果！")
        print("   可能原因：")
        print("   1. 知识库为空（请先运行 --init 加载知识库）")
        print("   2. 查询与知识库内容不匹配")
        return
    
    print(f"✅ 检索到 {len(results)} 条相关结果：\n")
    for i, result in enumerate(results, 1):
        print(f"【{i}】相似度: {result.get('score', 0):.2f}")
        print(f"    来源: {result.get('filename', 'N/A')}")
        print(f"    内容: {result.get('content', '')[:200]}...")
        print()


async def show_status(advisor_id: str):
    """显示知识库状态"""
    from advisors import get_advisor
    from advisors.vectorizer import get_vector_store
    
    advisor = get_advisor(advisor_id)
    if not advisor:
        print(f"❌ 顾问不存在: {advisor_id}")
        return
    
    print(f"\n📊 顾问知识库状态: {advisor.name} ({advisor_id})")
    print("-" * 50)
    
    # 获取向量库统计
    vector_store = get_vector_store()
    stats = vector_store.get_stats(advisor_id)
    
    print(f"   顾问名称: {advisor.name}")
    print(f"   API提供商: {advisor.api_provider}")
    print(f"   模型: {advisor.model_name}")
    print(f"   知识库路径: {advisor.knowledge_path}")
    print(f"   向量块数量: {stats.get('chunk_count', 0)}")
    
    # 检查知识库文件夹
    if advisor.knowledge_path.exists():
        files = list(advisor.knowledge_path.glob('*'))
        print(f"   本地文件数: {len(files)}")
        for f in files[:5]:
            print(f"      - {f.name}")
        if len(files) > 5:
            print(f"      ... 还有 {len(files) - 5} 个文件")


def main():
    parser = argparse.ArgumentParser(description='顾问知识库管理工具')
    parser.add_argument('advisor_id', help='顾问ID，如 xuehui-copywriting')
    parser.add_argument('--init', action='store_true', help='初始化并加载知识库')
    parser.add_argument('--test', type=str, help='测试检索功能')
    parser.add_argument('--status', action='store_true', help='显示知识库状态')
    
    args = parser.parse_args()
    
    # 默认知识库路径
    knowledge_path = project_root / 'data' / 'advisors' / args.advisor_id / 'knowledge'
    
    if args.init:
        asyncio.run(load_knowledge_from_folder(args.advisor_id, knowledge_path))
    elif args.test:
        asyncio.run(test_retrieval(args.advisor_id, args.test))
    elif args.status:
        asyncio.run(show_status(args.advisor_id))
    else:
        # 默认显示状态
        asyncio.run(show_status(args.advisor_id))


if __name__ == '__main__':
    main()
