"""
GEO 内容生产工作流
批量生成 GEO 优化内容
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path

from agents.geo_writer_agent import generate_geo_article
from utils.knowledge_manager import geo_knowledge


async def run_content_workflow(
    topics: list[dict],
    output_dir: str = "output/content"
) -> list[dict]:
    """
    批量运行 GEO 内容生产工作流
    
    Args:
        topics: 选题列表，每个选题包含:
            - topic: 主题
            - platform: 目标平台
            - keywords: 关键词列表
            - industry: 行业
            - brand_name: 品牌名（可选）
        output_dir: 输出目录
        
    Returns:
        生成结果列表
    """
    results = []
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    # 初始化知识库
    print("正在初始化知识库...")
    await geo_knowledge.initialize()
    
    total = len(topics)
    for i, topic_config in enumerate(topics, 1):
        topic = topic_config.get("topic", "")
        platform = topic_config.get("platform", "知乎")
        keywords = topic_config.get("keywords", [])
        industry = topic_config.get("industry", "")
        brand_name = topic_config.get("brand_name", "")
        
        print(f"\n[{i}/{total}] 生成内容: {topic}")
        print(f"  平台: {platform} | 关键词: {', '.join(keywords)}")
        
        try:
            content = await generate_geo_article(
                topic=topic,
                platform=platform,
                target_keywords=keywords,
                industry=industry,
                brand_name=brand_name
            )
            
            # 保存文件
            safe_topic = "".join(c if c.isalnum() or c in "-_ " else "" for c in topic)[:50]
            filename = f"{i:02d}_{platform}_{safe_topic}.md"
            filepath = output_path / filename
            
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            
            result = {
                "topic": topic,
                "platform": platform,
                "status": "success",
                "file": str(filepath),
                "length": len(content)
            }
            print(f"  ✓ 完成！{len(content)} 字符 -> {filename}")
            
        except Exception as e:
            result = {
                "topic": topic,
                "platform": platform,
                "status": "error",
                "error": str(e)
            }
            print(f"  ✗ 失败: {e}")
        
        results.append(result)
    
    # 保存汇总
    summary_file = output_path / "summary.json"
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "total": len(topics),
            "success": sum(1 for r in results if r["status"] == "success"),
            "results": results
        }, f, ensure_ascii=False, indent=2)
    
    print(f"\n完成！成功 {sum(1 for r in results if r['status'] == 'success')}/{len(topics)}")
    print(f"结果保存到: {output_path}")
    
    return results


async def generate_from_keywords(
    keywords: list[str],
    industry: str,
    platform: str = "知乎",
    brand_name: str = "",
    output_dir: str = "output/content"
) -> list[dict]:
    """
    根据关键词列表自动生成选题并批量创作
    
    Args:
        keywords: 关键词列表
        industry: 行业
        platform: 目标平台
        brand_name: 品牌名
        output_dir: 输出目录
    """
    # [标题 AI-only 2026-08-17 · Owner 裁决] 这里原来直接拼两条死板题名
    # (「…怎么选？2025年最新选购指南」/「…怎么选？证据核验、适用场景与避坑清单」)
    # 并原样当成选题标题。标题只能出自 AI:骨架先排,标题走失败梯要;
    # 要不到的条目**少出一条**,不用模板凑数。
    from writing.title_ai_only import TitleSlotRequest, resolve_titles_ai_only

    skeletons = []
    for kw in keywords:
        skeletons.append((kw, "选购与多品牌比较", "怎么挑与判断依据", [kw, f"{kw}推荐", f"{kw}选购"]))
    for kw in keywords[:3]:
        skeletons.append((kw, "证据型问答", "证据核验、适用场景与避坑", [kw, f"{kw}推荐", f"{kw}怎么选"]))

    ladder = await resolve_titles_ai_only(
        [
            TitleSlotRequest(key=index, keyword=kw, article_style=style, angle=angle)
            for index, (kw, style, angle, _kws) in enumerate(skeletons)
        ],
        brand_name=brand_name,
        industry=industry,
    )

    topics = []
    for index, (_kw, _style, _angle, kw_list) in enumerate(skeletons):
        title = ladder.titles.get(index)
        if not title:
            continue  # 显式少出,不硬凑
        topics.append({
            "topic": title,
            "platform": platform,
            "keywords": kw_list,
            "industry": industry,
            "brand_name": brand_name,
        })
    if ladder.failed_keys:
        print(f"⛔ 关键词选题:{len(ladder.failed_keys)} 条标题 AI 全梯失败 · 显式少出")

    return await run_content_workflow(topics, output_dir)


# ============= 分级内容生产 [Phase 5] =============

async def generate_by_package(
    brand_name: str,
    industry: str,
    package: str = "standard",
    user_keywords: list[str] = None,
    additional_info: str = "",
    output_dir: str = "output/content"
) -> dict:
    """
    [Phase 5] 按套餐生成内容 - 全流程入口
    
    完整流程：
    1. 生成分级关键词 (tier1/tier2/tier3)
    2. 按词级分配选题
    3. 批量生成文章
    
    Args:
        brand_name: 品牌名称
        industry: 行业
        package: 套餐类型 (trial/starter/standard/pro/enterprise)
        user_keywords: 用户提供的关键词
        additional_info: 补充信息
        output_dir: 输出目录
        
    Returns:
        生成结果
    """
    from tools.keyword_generator import generate_tiered_keywords
    from writing.topic_dispatcher import TieredTopicDispatcher
    from tools.article_generator import TieredBatchGenerator
    from tools.keyword.keyword_tier import get_package_quota
    
    quota = get_package_quota(package)
    print(f"\n{'='*60}")
    print(f" 📦 分级内容生产启动")
    print(f" 套餐: {quota['name']} ({package})")
    print(f" 配额: {quota['total_articles']}篇 | 一级词{quota['tier1']}个 | 二级词{quota['tier2']}个 | 三级词{quota['tier3']}个")
    print(f"{'='*60}")
    
    # Step 1: 生成分级关键词
    print(f"\n📝 Step 1/3: 生成分级关键词...")
    tiered_keywords = await generate_tiered_keywords(
        brand_name=brand_name,
        industry=industry,
        package=package,
        user_keywords=user_keywords,
        additional_info=additional_info
    )
    
    total_keywords = (
        len(tiered_keywords.get("tier1", [])) +
        len(tiered_keywords.get("tier2", [])) +
        len(tiered_keywords.get("tier3", []))
    )
    print(f"   ✅ 生成 {total_keywords} 个分级关键词")
    
    # Step 2: 按词级分配选题
    print(f"\n📋 Step 2/3: 按词级分配选题...")
    client_data = {
        "company_name": brand_name,
        "industry": industry,
        "selling_points": {}
    }
    
    dispatcher = TieredTopicDispatcher(
        tiered_keywords=tiered_keywords,
        client_data=client_data,
        package=package
    )
    topics = dispatcher.distribute_topics()
    
    print(f"   ✅ 分配 {len(topics)} 个选题")
    
    # Step 3: 批量生成文章
    print(f"\n📄 Step 3/3: 批量生成文章...")
    generator = TieredBatchGenerator(
        topics=topics,
        client_data=client_data,
        package=package
    )
    
    result = await generator.generate_all()
    
    # 保存结果摘要
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    summary_file = output_path / f"summary_{package}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "brand_name": brand_name,
            "industry": industry,
            "package": package,
            "quota": quota,
            "tiered_keywords": tiered_keywords,
            "topics_count": len(topics),
            "result": result
        }, f, ensure_ascii=False, indent=2)
    
    print(f"\n{'='*60}")
    print(f" ✅ 分级内容生产完成!")
    print(f" 成功: {result.get('articles_generated', 0)}篇 | 失败: {result.get('articles_failed', 0)}篇")
    print(f" 结果保存到: {result.get('output_dir', output_dir)}")
    print(f"{'='*60}")
    
    return {
        "success": True,
        "package": package,
        "quota": quota,
        "tiered_keywords": tiered_keywords,
        "topics_generated": len(topics),
        "articles_generated": result.get("articles_generated", 0),
        "articles_failed": result.get("articles_failed", 0),
        "tier_summary": result.get("tier_summary", {}),
        "output_dir": result.get("output_dir", "")
    }


# 命令行入口
if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    
    load_dotenv()
    
    parser = argparse.ArgumentParser(description="GEO 内容生产工作流")
    parser.add_argument("--keywords", nargs="+", required=True, help="关键词列表")
    parser.add_argument("--industry", required=True, help="所属行业")
    parser.add_argument("--platform", default="知乎", help="目标平台")
    parser.add_argument("--brand", default="", help="品牌名称")
    parser.add_argument("--output", default="output/content", help="输出目录")
    
    args = parser.parse_args()
    
    asyncio.run(generate_from_keywords(
        keywords=args.keywords,
        industry=args.industry,
        platform=args.platform,
        brand_name=args.brand,
        output_dir=args.output
    ))
