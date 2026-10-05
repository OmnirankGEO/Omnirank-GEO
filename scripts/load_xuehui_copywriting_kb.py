import sys
from pathlib import Path
import asyncio
from dotenv import load_dotenv

# 添加项目路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# 加载环境变量
load_dotenv(project_root / '.env')

from advisors.base_advisor import BaseAdvisor
import json


async def load_knowledge_base():
    """加载薛辉文案知识库"""
    
    print("=" * 60)
    print("薛辉文案知识库加载工具")
    print("=" * 60)
    
    # 知识库文档列表（完整10个文件）
    knowledge_docs = [
        "01_core_methodology.md",
        "02_script_structures.md",
        "03_classic_plot_devices.md",
        "04_practical_techniques.md",
        "05_topic_title_methodology.md",
        "06_hook_methodology.md",
        "07_cta_conversion_methodology.md",
        "08_persona_embedding_methodology.md",
        "09_dynamic_structure_selection.md",
        "10_middle_content_methodology.md"
    ]
    
    # 配置路径
    config_path = project_root / "data/advisors/xuehui-copywriting/config.json"
    knowledge_dir = project_root / "data/advisors/xuehui-copywriting/knowledge"
    
    # 加载配置
    print(f"\n1. 加载配置文件: {config_path}")
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)
    print(f"   ✅ 配置加载成功")
    print(f"   - Advisor ID: {config['advisor_id']}")
    print(f"   - Advisor名称: {config['name']}")
    print(f"   - 模型: {config['model']}")
    
    # 初始化Advisor
    print(f"\n2. 初始化BaseAdvisor...")
    advisor = BaseAdvisor(
        advisor_id=config['advisor_id'],
        name=config['name'],
        base_prompt=config['system_prompt'],
        description=config.get('description', ''),
        model_name=config['model'],
        temperature=config.get('temperature', 0.7)
    )
    print(f"   ✅ Advisor初始化成功")
    
    # 加载文档到知识库
    print(f"\n3. 加载知识库文档...")
    total_chunks = 0
    
    for doc_name in knowledge_docs:
        doc_path = knowledge_dir / doc_name
        
        if not doc_path.exists():
            print(f"   ⚠️  文档不存在，跳过: {doc_name}")
            continue
        
        print(f"\n   处理: {doc_name}")
        
        # 读取文档内容
        with open(doc_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 使用BaseAdvisor的add_document方法
        result = await advisor.add_document(
            content=content,
            filename=doc_name,
            file_type='md',
            source=f"薛辉老师短视频营销课程 - {doc_name}"
        )
        
        if result.get('success'):
            chunk_count = result.get('chunk_count', 0)
            total_chunks += chunk_count
            print(f"      ✅ {doc_name}: {chunk_count} 个分块")
        else:
            print(f"      ❌ 加载失败: {result.get('error', '未知错误')}")
    
    print(f"\n{'=' * 60}")
    print(f"✅ 知识库加载完成！")
    print(f"   - 文档总数: {len(knowledge_docs)}")
    print(f"   - 分块总数: {total_chunks}")
    print(f"{'=' * 60}")
    
    print(f"\n✅ 薛辉文案知识库已成功加载到 '{config['advisor_id']}' advisor中！")
    print(f"   可以在内容工坊中使用该advisor进行文案创作指导。")


if __name__ == "__main__":
    try:
        asyncio.run(load_knowledge_base())
    except Exception as e:
        print(f"\n❌ 加载失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
