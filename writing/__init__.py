"""
GEO文章写作模块
自动生成高质量、易被AI引用的文章

架构:
1. Distiller Pipeline (蒸馏管道) - 提取客户核心信息
2. Topic Dispatcher (选题分发) - 生成文章选题列表  
3. Article Writer (文章撰写) - 并发生成文章
"""

from .config import ARTICLE_DISTRIBUTION, TEST_MODE_CONFIG
from .distiller import DistillerPipeline
from .topic_dispatcher import TopicDispatcher
from .article_writer import ArticleWriter
from .batch_processor import BatchProcessor

__all__ = [
    "ARTICLE_DISTRIBUTION",
    "TEST_MODE_CONFIG", 
    "DistillerPipeline",
    "TopicDispatcher",
    "ArticleWriter",
    "BatchProcessor"
]
