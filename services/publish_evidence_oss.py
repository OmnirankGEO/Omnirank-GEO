"""
publish_evidence_oss · 代理发布截图 OSS 直传服务

CTO-15.9 session 3 · 2026-04-25 · 配 P0.4b.2(老板今天建好 bucket)

职责:
- 发布截图私有 bucket(omnirank-publish-evidence)签名/直传
- 浏览器直传(PUT 预签名 URL)· 不经过我们后端 · 省带宽
- 客户门户/admin 临时签名 URL(15 分钟)查看截图

设计:
- 复用现有 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET(omnirank-kyc 子账号 · 已有 OSSFullAccess)
- 独立 bucket(omnirank-publish-evidence · 与 KYC 隔离)
- Lazy init · 配置缺失时降级返友好错误 · 不阻塞 ManualPublicationForm 提交

环境变量:
- OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET 复用 KYC
- OSS_PUBLISH_BUCKET 默认 'omnirank-publish-evidence'
- OSS_PUBLISH_ENDPOINT 默认 'oss-cn-shenzhen.aliyuncs.com'(同 KYC region)
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Optional

logger = logging.getLogger("GEO-PublishEvidenceOSS")


class PublishEvidenceConfigError(RuntimeError):
    """OSS 未配置或配置错误"""


class PublishEvidenceUploadError(RuntimeError):
    """OSS 上传失败"""


class PublishEvidenceAccessError(RuntimeError):
    """OSS 访问失败(签名/下载)"""


_bucket_singleton = None
_bucket_name: Optional[str] = None


def _get_bucket():
    """Lazy 获取 publish-evidence bucket 句柄(单例 · 复用 KYC 同子账号)"""
    global _bucket_singleton, _bucket_name

    if _bucket_singleton is not None:
        return _bucket_singleton, _bucket_name

    access_key_id = os.getenv("OSS_ACCESS_KEY_ID", "").strip()
    access_key_secret = os.getenv("OSS_ACCESS_KEY_SECRET", "").strip()
    bucket_name = os.getenv("OSS_PUBLISH_BUCKET", "omnirank-publish-evidence").strip()
    endpoint = os.getenv("OSS_PUBLISH_ENDPOINT", "oss-cn-shenzhen.aliyuncs.com").strip()

    if not access_key_id or not access_key_secret:
        raise PublishEvidenceConfigError(
            "OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET 未配置 · 复用 KYC 子账号 · 老板检查 .env"
        )

    try:
        import oss2
    except ImportError as e:
        raise PublishEvidenceConfigError(
            f"oss2 SDK 未安装 · requirements.txt 应含 oss2: {e}"
        )

    auth = oss2.Auth(access_key_id, access_key_secret)
    bucket = oss2.Bucket(auth, endpoint, bucket_name)

    _bucket_singleton = bucket
    _bucket_name = bucket_name
    logger.info(f"[PublishEvidence OSS] bucket 初始化: {bucket_name} @ {endpoint}")
    return bucket, bucket_name


def build_evidence_key(quote_id: int, article_id: Optional[int], ext: str = "jpg") -> str:
    """构造发布截图 OSS key

    规范:publications/{quote_id}/{article_id_or_manual}_{ts}_{rand}.{ext}
    - quote_id 必填(隔离客户)
    - article_id 可选(没有时用 'manual' 占位)
    - ts 秒级时间戳 + 8 位 hex · 防同秒碰撞
    - 扩展名:jpg / jpeg / png / webp(发布截图常见格式)
    """
    if not quote_id or quote_id <= 0:
        raise ValueError("quote_id 必填")
    if ext.lower() not in ("jpg", "jpeg", "png", "webp"):
        raise ValueError(f"不支持的扩展名: {ext}")
    ts = int(time.time())
    rand = secrets.token_hex(4)
    article_part = str(article_id) if article_id else "manual"
    return f"publications/{quote_id}/{article_part}_{ts}_{rand}.{ext.lower()}"


def generate_upload_url(
    oss_key: str,
    content_type: str = "image/jpeg",
    expires_seconds: int = 600,
) -> dict:
    """生成 PUT 预签名 URL(浏览器直传 · 10 分钟)

    前端用法:
      fetch(uploadUrl, { method: 'PUT', body: imageBlob, headers: {'Content-Type': 'image/jpeg'} })

    Args:
        oss_key: 通过 build_evidence_key 构造
        content_type: image/jpeg | image/png | image/webp
        expires_seconds: 默认 600(10 分钟够选图+上传)

    Returns:
        {
          "upload_url": "https://...预签名 URL",
          "oss_key": "publications/...",
          "expires_in": 600,
          "content_type": "image/jpeg",
          "method": "PUT",
        }
    """
    if content_type not in ("image/jpeg", "image/png", "image/webp"):
        raise ValueError(f"不支持的 content_type: {content_type}")
    if expires_seconds <= 0 or expires_seconds > 1800:
        raise ValueError("expires_seconds 必须在 (0, 1800] 范围内")

    try:
        bucket, _ = _get_bucket()
        # PUT 签名 · 客户端必须用同样的 Content-Type
        url = bucket.sign_url(
            "PUT",
            oss_key,
            expires_seconds,
            slash_safe=True,
            headers={"Content-Type": content_type},
        )
        return {
            "upload_url": url,
            "oss_key": oss_key,
            "expires_in": expires_seconds,
            "content_type": content_type,
            "method": "PUT",
        }
    except PublishEvidenceConfigError:
        raise
    except Exception as e:
        logger.error(f"[PublishEvidence OSS] 签名 PUT URL 失败 key={oss_key}: {type(e).__name__}: {e}")
        raise PublishEvidenceAccessError(f"签名失败: {e}") from e


def generate_view_url(oss_key: str, expires_seconds: int = 900) -> str:
    """生成 GET 临时签名 URL(15 分钟 · 客户/admin 查截图)"""
    if expires_seconds <= 0 or expires_seconds > 3600:
        raise ValueError("expires_seconds 必须在 (0, 3600] 范围内")
    try:
        bucket, _ = _get_bucket()
        return bucket.sign_url("GET", oss_key, expires_seconds, slash_safe=True)
    except PublishEvidenceConfigError:
        raise
    except Exception as e:
        logger.error(f"[PublishEvidence OSS] 签名 GET URL 失败 key={oss_key}: {type(e).__name__}: {e}")
        raise PublishEvidenceAccessError(f"签名失败: {e}") from e


def delete_evidence(oss_key: str) -> bool:
    """删除截图(代理撤回 / 90 天清理)· 容错:404 视为成功"""
    try:
        bucket, _ = _get_bucket()
        bucket.delete_object(oss_key)
        logger.info(f"[PublishEvidence OSS] 删除 key={oss_key}")
        return True
    except PublishEvidenceConfigError:
        raise
    except Exception as e:
        msg = str(e).lower()
        if "404" in msg or "no such key" in msg or "notfound" in msg.replace(" ", ""):
            return True
        logger.warning(f"[PublishEvidence OSS] 删除失败 key={oss_key}: {e}")
        return False


def is_configured() -> bool:
    """轻量探测 · 不实际连 OSS"""
    return bool(
        os.getenv("OSS_ACCESS_KEY_ID", "").strip()
        and os.getenv("OSS_ACCESS_KEY_SECRET", "").strip()
    )
