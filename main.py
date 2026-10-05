"""
GEO AgentScope 系统主入口
支持多种运行模式
"""

import os
import asyncio
import argparse
from pathlib import Path
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 设置项目根目录
PROJECT_ROOT = Path(__file__).parent
os.chdir(PROJECT_ROOT)


async def run_director():
    """运行 Director Agent 交互模式"""
    from agents import chat_with_director
    from utils.knowledge_manager import geo_knowledge
    
    print("\n" + "="*60)
    print("  GEO Director Agent - 智能助手")
    print("="*60)
    print("\n可以问我：")
    print("  • 帮我分析一下XX品牌的GEO表现")
    print("  • 帮我写一篇关于XX的知乎文章")
    print("  • 帮我制定XX品牌的内容策略")
    print("\n输入 'exit' 退出\n")
    
    # 初始化知识库
    print("正在初始化知识库...")
    await geo_knowledge.initialize()
    print("知识库初始化完成！\n")
    
    while True:
        user_input = input("你: ")
        if user_input.lower() == "exit":
            print("再见！")
            break
        
        if not user_input.strip():
            continue
        
        print("\nDirector 思考中...\n")
        response = await chat_with_director(user_input)
        print(f"Director: {response}\n")


async def run_diagnosis(brand_name: str, industry: str, keywords: list[str] = None):
    """运行 GEO 诊断分析"""
    from workflows import run_diagnosis_workflow
    
    print(f"\n{'='*60}")
    print(f"  GEO 诊断分析：{brand_name}")
    print(f"{'='*60}\n")
    
    result = await run_diagnosis_workflow(
        brand_name=brand_name,
        industry=industry,
        keywords=keywords or [],
        output_file=f"output/{brand_name}_diagnosis"
    )
    
    print("\n" + "-"*40)
    print(result.get("report", "诊断完成"))
    
    return result


async def run_content(
    topic: str = None,
    platform: str = "知乎",
    keywords: list[str] = None,
    industry: str = "",
    brand_name: str = ""
):
    """运行 GEO 内容生产"""
    if topic:
        # 单篇创作
        from agents import generate_geo_article
        from utils.knowledge_manager import geo_knowledge
        
        print(f"\n{'='*60}")
        print(f"  GEO 内容创作：{topic}")
        print(f"  目标平台：{platform}")
        print(f"{'='*60}\n")
        
        print("正在初始化知识库...")
        await geo_knowledge.initialize()
        
        print("正在生成内容...\n")
        result = await generate_geo_article(topic, platform, keywords or [], industry, brand_name)
        
        print("\n生成的内容：")
        print("-" * 40)
        print(result)
        
        return result
    else:
        # 批量创作
        from workflows import generate_from_keywords
        
        if not keywords:
            print("批量创作模式需要提供 --keywords 参数")
            return
        
        return await generate_from_keywords(
            keywords=keywords,
            industry=industry,
            platform=platform,
            brand_name=brand_name
        )


async def run_workflow(workflow_name: str, **kwargs):
    """运行指定工作流"""
    from workflows import run_diagnosis_workflow, run_content_workflow
    
    if workflow_name == "diagnosis":
        return await run_diagnosis_workflow(**kwargs)
    elif workflow_name == "content":
        return await run_content_workflow(**kwargs)
    else:
        print(f"未知工作流: {workflow_name}")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="GEO AgentScope 系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法:
  # 交互模式（推荐）
  python main.py --mode chat
  
  # 诊断分析
  python main.py --mode diagnosis --brand "品牌名" --industry "行业"
  
  # 内容创作
  python main.py --mode content --topic "主题" --platform "知乎" --industry "行业"
  
  # 批量内容
  python main.py --mode content --keywords "关键词1" "关键词2" --industry "行业"
        """
    )
    
    parser.add_argument(
        "--mode", 
        choices=["chat", "diagnosis", "content"], 
        default="chat",
        help="运行模式: chat(交互), diagnosis(诊断), content(内容)"
    )
    parser.add_argument("--brand", type=str, help="品牌名称")
    parser.add_argument("--industry", type=str, help="所属行业")
    parser.add_argument("--keywords", type=str, nargs="+", help="关键词列表")
    parser.add_argument("--topic", type=str, help="文章主题（内容模式）")
    parser.add_argument("--platform", type=str, default="知乎",
                       help="目标平台（内容模式），默认知乎")
    
    args = parser.parse_args()
    
    if args.mode == "chat":
        asyncio.run(run_director())
    
    elif args.mode == "diagnosis":
        if not args.brand:
            print("诊断模式需要提供 --brand 参数")
            return
        if not args.industry:
            print("诊断模式需要提供 --industry 参数")
            return
        asyncio.run(run_diagnosis(args.brand, args.industry, args.keywords))
    
    elif args.mode == "content":
        if not args.industry:
            print("内容模式需要提供 --industry 参数")
            return
        asyncio.run(run_content(
            topic=args.topic,
            platform=args.platform,
            keywords=args.keywords,
            industry=args.industry,
            brand_name=args.brand or ""
        ))


if __name__ == "__main__":
    main()
