"""
阿里云 OSS 封装 (research_monitor 专用)

把文章 markdown 存到 omnirank-research-articles bucket。

环境变量:
- OSS_ACCESS_KEY_ID
- OSS_ACCESS_KEY_SECRET
- OSS_RESEARCH_ARTICLES_BUCKET (默认 omnirank-research-articles)
- OSS_RESEARCH_ARTICLES_ENDPOINT (默认 oss-cn-shenzhen.aliyuncs.com 公网, 生产 .env 可覆盖成 -internal)

设计:
- OssHelper 单例: 复用 oss2.Bucket 连接, 避免每次 upload/download 重建
- upload/download 失败 logger.warning 不抛异常, 返 False/None 让上层决定怎么处理
  (round_runner 批处理多文章时, 个别失败不应阻断整批)

A.4 review fix(2026-05-05):
- C3 endpoint 默认改公网, 本地 / CI 跑得通
- I1 upload_markdown 模块函数返 dict {'ok','oss_key','error'} 跟其他模块一致
- I6 单例 lazy init 加 threading.Lock 防 async 并发首次调用重复实例化
"""
import os
import logging
import threading
from typing import Dict, Optional

try:
    import oss2
except ImportError:  # pragma: no cover - exercised by CI collection without OSS SDK
    oss2 = None

logger = logging.getLogger("GEO-ResearchMonitor.OSS")


def _require_oss2():
    if oss2 is None:
        raise RuntimeError("oss2 SDK 未安装, 请安装 requirements 里的 oss2 后再启用 OSS 功能")
    return oss2


class OssHelper:
    """OSS 单例 helper, 复用 Bucket 连接"""

    _instance: Optional['OssHelper'] = None
    _lock = threading.Lock()

    def __init__(self):
        access_key_id = os.getenv('OSS_ACCESS_KEY_ID', '').strip()
        access_key_secret = os.getenv('OSS_ACCESS_KEY_SECRET', '').strip()
        bucket_name = os.getenv('OSS_RESEARCH_ARTICLES_BUCKET', 'omnirank-research-articles').strip()
        # C3: 默认公网 endpoint, 本地 / CI 跑得通; 生产 .env 可覆盖成 -internal 走 ECS 内网
        endpoint = os.getenv(
            'OSS_RESEARCH_ARTICLES_ENDPOINT',
            'oss-cn-shenzhen.aliyuncs.com',
        ).strip()

        if not access_key_id or not access_key_secret:
            raise RuntimeError("OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET 未设置")

        self.bucket_name = bucket_name
        self.endpoint = endpoint
        oss2_sdk = _require_oss2()
        auth = oss2_sdk.Auth(access_key_id, access_key_secret)
        self.bucket = oss2_sdk.Bucket(auth, endpoint, bucket_name)

    @classmethod
    def get_instance(cls) -> 'OssHelper':
        # I6: double-checked locking 防 async 并发首次调用重复实例化
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def upload(self, oss_key: str, content: str) -> bool:
        """简单 upload 接口, 返 bool · 给 helper 内部 / 简单调用复用"""
        try:
            result = self.bucket.put_object(oss_key, content.encode('utf-8'))
            return getattr(result, 'status', 0) == 200
        except Exception as e:
            logger.warning(f"OSS upload 失败 key={oss_key}: {e}")
            return False

    def download(self, oss_key: str) -> Optional[str]:
        try:
            obj = self.bucket.get_object(oss_key)
            return obj.read().decode('utf-8')
        except Exception as e:
            logger.warning(f"OSS download 失败 key={oss_key}: {e}")
            return None


def upload_markdown(oss_key: str, content: str) -> Dict:
    """
    上传 markdown 到 OSS。

    I1: 返回 dict 跟其他模块(crawler / cleaner / scorer)一致, round_runner 拿得到失败 detail。
    返回: {'ok': bool, 'oss_key': str, 'error': Optional[str]}
    """
    try:
        helper = OssHelper.get_instance()
    except RuntimeError as e:
        logger.error(f"OSS helper init 失败: {e}")
        return {'ok': False, 'oss_key': oss_key, 'error': str(e)}

    try:
        result = helper.bucket.put_object(oss_key, content.encode('utf-8'))
        if getattr(result, 'status', 0) == 200:
            return {'ok': True, 'oss_key': oss_key, 'error': None}
        return {
            'ok': False,
            'oss_key': oss_key,
            'error': f"OSS status {getattr(result, 'status', 'unknown')}",
        }
    except Exception as e:
        logger.warning(f"OSS upload 失败 key={oss_key}: {e}")
        return {'ok': False, 'oss_key': oss_key, 'error': str(e)}


def download_markdown(oss_key: str) -> Optional[str]:
    """
    从 OSS 下载 markdown。
    成功返 str, 失败 (找不到 / 网络错) 返 None。
    """
    try:
        helper = OssHelper.get_instance()
    except RuntimeError:
        return None
    return helper.download(oss_key)


def generate_oss_key_for_article(
    prefix: str,
    year_month: str,
    domain: str,
    url_hash: str,
    version_key: str = "",
) -> str:
    """
    生成文章 OSS key。

    格式: {prefix}/{year_month}/{domain}/{url_hash}.md
    例: raw/2026-05/digiwin.com/abc123.md
    """
    suffix = f"/{version_key}" if version_key else ""
    return f"{prefix}/{year_month}/{domain}/{url_hash}{suffix}.md"
