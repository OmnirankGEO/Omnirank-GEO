"""
GEO知识库导入脚本

将GEO技术文献导入到"全域上榜"公司档案的业务知识库
"""

import asyncio
import os
import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# 加载环境变量
from dotenv import load_dotenv
load_dotenv(project_root / ".env")


async def find_or_create_profile(profile_name: str = "全域上榜"):
    """查找或创建公司档案"""
    from db.profile_db import list_profiles, create_profile
    
    profiles = list_profiles()
    for p in profiles:
        if profile_name in p.get('name', ''):
            print(f"[导入] ✅ 找到档案: {p['name']} (ID: {p['id']})")
            return p['id']
    
    # 创建新档案
    print(f"[导入] 📝 创建新档案: {profile_name}")
    profile_id = create_profile({
        "name": profile_name,
        "industry": "AI搜索优化",
        "business": "GEO (Generative Engine Optimization) 生成式引擎优化服务",
        "target_users": "想在AI搜索中获得曝光的企业",
    })
    print(f"[导入] ✅ 新档案创建成功 (ID: {profile_id})")
    return profile_id


async def import_knowledge_file(profile_id: str, file_path: Path):
    """导入单个知识库文件"""
    from tools.knowledge_rag import add_knowledge_to_profile
    from db.profile_db import add_knowledge_file
    import uuid
    from datetime import datetime
    
    if not file_path.exists():
        print(f"[导入] ❌ 文件不存在: {file_path}")
        return False
    
    # 读取文件内容
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    if not content.strip():
        print(f"[导入] ⚠️ 文件内容为空: {file_path.name}")
        return False
    
    # 生成文件ID
    file_id = str(uuid.uuid4())[:8]
    
    print(f"[导入] 📄 处理文件: {file_path.name} ({len(content)} 字符)")
    
    try:
        # 向量化存储
        result = await add_knowledge_to_profile(
            profile_id=profile_id,
            file_id=file_id,
            filename=file_path.name,
            content=content
        )
        
        if result.get("success"):
            # 记录到数据库
            file_info = {
                "id": file_id,
                "name": file_path.name,
                "path": str(file_path),
                "size": len(content),
                "chunk_count": result.get("chunk_count", 0),
                "created_at": datetime.now().isoformat()
            }
            add_knowledge_file(profile_id, file_info)
            print(f"[导入] ✅ 成功: {file_path.name} -> {result.get('chunk_count', 0)} 个向量块")
            return True
        else:
            print(f"[导入] ❌ 失败: {result.get('error', '未知错误')}")
            return False
    except Exception as e:
        print(f"[导入] ❌ 异常: {e}")
        return False


async def main():
    """主函数"""
    print("\n" + "="*60)
    print("GEO知识库导入工具")
    print("="*60 + "\n")
    
    # 1. 查找或创建档案
    profile_id = await find_or_create_profile("全域上榜")
    
    # 2. 定义要导入的知识库文件
    knowledge_dir = project_root / "knowledge"
    
    files_to_import = [
        knowledge_dir / "GEO优化规则库（完整版）.md",
        knowledge_dir / "GEO发布平台大全.md",
        knowledge_dir / "AI平台引用偏好.md",
        knowledge_dir / "部分平台内容风格.md",
        knowledge_dir / "AI对话平台→内容平台映射（完整版）.md",
        knowledge_dir / "2026年用户搜索意图与热门标题公式知识库.md",
        knowledge_dir / "案例库" / "GEO排名文章高分样本库.md",
    ]
    
    # 3. 逐个导入
    success_count = 0
    total_count = len(files_to_import)
    
    print(f"\n[导入] 开始导入 {total_count} 个知识库文件...")
    print("-"*60)
    
    for file_path in files_to_import:
        if await import_knowledge_file(profile_id, file_path):
            success_count += 1
    
    # 4. 汇总
    print("\n" + "="*60)
    print(f"导入完成: {success_count}/{total_count} 成功")
    print(f"档案ID: {profile_id}")
    print("="*60 + "\n")
    
    # 5. 测试检索
    print("[测试] 验证知识库检索...")
    from tools.knowledge_rag import search_profile_knowledge
    
    test_queries = [
        "什么是GEO优化",
        "如何让AI推荐我的品牌",
        "AI平台内容发布策略",
    ]
    
    for query in test_queries:
        results = await search_profile_knowledge(profile_id, query, top_k=2)
        print(f"\n  Query: {query}")
        for r in results:
            print(f"    - [{r['score']:.2f}] {r['content'][:60]}...")
    
    print("\n[完成] 🎉 GEO知识库导入完毕！")


if __name__ == "__main__":
    asyncio.run(main())
