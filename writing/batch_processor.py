"""
批处理器 - 并发生成多篇文章
"""

import asyncio
import os
from datetime import datetime
from typing import Dict, Any, List
from .config import CONCURRENT_CONFIG
from .distiller import DistillerPipeline
from .topic_dispatcher import TopicDispatcher
from .article_writer import ArticleWriter


class BatchProcessor:
    """批处理器 - 协调完整的文章生成流程"""
    
    def __init__(
        self,
        diagnosis_data: Dict[str, Any],
        test_mode: bool = True,
        output_dir: str = "output/articles"
    ):
        self.diagnosis_data = diagnosis_data
        self.test_mode = test_mode
        self.output_dir = output_dir
        self.config = CONCURRENT_CONFIG
        
        # 确保输出目录存在
        os.makedirs(output_dir, exist_ok=True)
    
    async def run(self) -> Dict[str, Any]:
        """执行完整的文章生成流程"""
        start_time = datetime.now()
        brand_name = self.diagnosis_data.get("brand_name", "未知品牌")
        
        print(f"\n{'='*50}")
        print(f"🚀 开始为 [{brand_name}] 生成GEO文章")
        print(f"   模式: {'测试模式(10篇)' if self.test_mode else '正式模式(36篇)'}")
        print(f"{'='*50}\n")
        
        # Stage 1-3: 蒸馏管道
        print("📊 Step 1: 运行蒸馏管道...")
        distiller = DistillerPipeline(self.diagnosis_data)
        distilled_data = await distiller.run()
        
        # Stage 4: 选题分发 - V9增强：传入诊断原始数据
        print("\n📝 Step 2: 生成文章选题...")
        dispatcher = TopicDispatcher(
            distilled_data=distilled_data,
            diagnosis_data=self.diagnosis_data,  # V9：传入诊断原始数据
            test_mode=self.test_mode
        )
        topics = await dispatcher.generate_topics()
        print(f"   共生成 {len(topics)} 个选题")
        
        # Stage 5: 并发写作
        print(f"\n✍️ Step 3: 并发生成文章 (并发数: {self.config['max_concurrent_writers']})...")
        articles = await self._write_articles_concurrent(topics, distilled_data)
        
        # 保存文章
        print("\n💾 Step 4: 保存文章...")
        saved_files = await self._save_articles(articles, brand_name)
        
        # 统计
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        
        result = {
            "brand_name": brand_name,
            "total_articles": len(articles),
            "saved_files": saved_files,
            "duration_seconds": duration,
            "topics": topics,
            "distilled_data": distilled_data
        }
        
        print(f"\n{'='*50}")
        print(f"✅ 完成！共生成 {len(articles)} 篇文章")
        print(f"   耗时: {duration:.1f}秒")
        print(f"   保存至: {self.output_dir}/")
        print(f"{'='*50}\n")
        
        return result
    
    async def _write_articles_concurrent(
        self,
        topics: List[Dict[str, Any]],
        distilled_data: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """并发生成文章"""
        # V2注意：ArticleWriter支持brand_id参数检索客户知识库
        # 本测试类暂未传入brand_id，如需使用请从diagnosis_data获取
        writer = ArticleWriter(distilled_data)
        max_concurrent = self.config["max_concurrent_writers"]
        
        # 使用信号量控制并发
        semaphore = asyncio.Semaphore(max_concurrent)
        
        async def write_with_semaphore(topic):
            async with semaphore:
                return await writer.write(topic)
        
        # 并发执行
        tasks = [write_with_semaphore(topic) for topic in topics]
        articles = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 过滤失败的
        valid_articles = []
        for i, article in enumerate(articles):
            if isinstance(article, Exception):
                print(f"   ⚠️ 文章 {i+1} 生成失败: {article}")
            else:
                valid_articles.append(article)
        
        return valid_articles
    
    async def _save_articles(
        self,
        articles: List[Dict[str, Any]],
        brand_name: str
    ) -> List[str]:
        """保存文章到文件"""
        saved_files = []
        
        # 创建品牌子目录
        brand_dir = os.path.join(self.output_dir, brand_name.replace(" ", "_"))
        os.makedirs(brand_dir, exist_ok=True)
        
        for article in articles:
            # 生成文件名
            article_type = article.get("type", "unknown")
            article_id = article.get("id", 0)
            title = article.get("title", "未命名")[:30].replace("/", "_").replace("\\", "_")
            
            filename = f"{article_id:02d}_{article_type}_{title}.md"
            filepath = os.path.join(brand_dir, filename)
            
            # 写入文件
            content = self._format_article(article)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            
            saved_files.append(filepath)
            print(f"   ✅ {filename}")
        
        return saved_files
    
    def _format_article(self, article: Dict[str, Any]) -> str:
        """格式化文章内容"""
        metadata = f"""---
title: {article.get('title', '')}
type: {article.get('type', '')}
platform: {article.get('platform', '')}
keywords: {', '.join(article.get('keywords', []))}
word_count: {article.get('word_count', 0)}
generated_at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
---

"""
        return metadata + article.get("content", "")


# 测试入口
async def run_test():
    """运行测试"""
    from dotenv import load_dotenv
    load_dotenv()
    
    # 模拟诊断数据
    test_diagnosis = {
        "brand_name": "驰鲸科技",
        "industry": "TikTok B2B出海代运营",
        "geo_score": {"total_score": 18, "level": "空白"},
        "business_context": "专注TikTok B2B出海代运营，服务工厂出海客户，帮助中国制造业通过TikTok获取海外客户",
        "competitor_data": {
            "competitors": [
                {"name": "卧兔网络", "strengths": ["规模大"], "limitations": ["主攻C端"]},
                {"name": "PandaMobo", "strengths": ["资源多"], "limitations": ["价格偏高"]}
            ]
        },
        "content_insights": "客户在TikTok有一定内容积累，但缺乏专业的B2B获客方法论"
    }
    
    processor = BatchProcessor(
        diagnosis_data=test_diagnosis,
        test_mode=True,
        output_dir="output/articles"
    )
    
    result = await processor.run()
    return result


if __name__ == "__main__":
    asyncio.run(run_test())
