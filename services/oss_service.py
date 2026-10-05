"""
阿里云 OSS 服务封装（KYC 私有 Bucket 专用）

职责:
- 上传身份证照片到私有 bucket `omnirank-kyc`
- 生成临时签名 URL（15 分钟有效期）供 admin / 用户本人查看
- 删除对象（定时清理 / 用户撤回时）

设计:
- Lazy init: 首次调用才实例化 Bucket, 启动失败不阻塞 server
- 仅服务于 KYC 场景, 不与现有 omnirank-static 公共桶混用
- 敏感凭据全部从环境变量读取, 代码内无任何硬编码

环境变量:
- OSS_ACCESS_KEY_ID       RAM omnirank-kyc 子账号的 access key id
- OSS_ACCESS_KEY_SECRET   同上的 secret
- OSS_KYC_BUCKET          bucket 名称（默认 omnirank-kyc）
- OSS_KYC_ENDPOINT        内网/公网 endpoint（默认 oss-cn-shenzhen.aliyuncs.com）
- OSS_FEEDBACK_BUCKET     问题反馈截图 bucket 名称（建议私有 bucket）
- OSS_FEEDBACK_ENDPOINT   问题反馈截图 bucket endpoint
- OSS_FEEDBACK_URL_TTL_SECONDS  管理端查看反馈截图的签名链接有效期（默认 10 年）
"""

import logging
import os
import time
from typing import Optional

logger = logging.getLogger("GEO-OSS")


class OSSConfigError(RuntimeError):
    """OSS 未配置或配置错误"""


class OSSUploadError(RuntimeError):
    """OSS 上传失败"""


class OSSAccessError(RuntimeError):
    """OSS 访问失败（签名/下载/删除）"""


_bucket_singleton = None
_bucket_name: Optional[str] = None
_feedback_bucket_singleton = None
_feedback_bucket_name: Optional[str] = None


def _get_feedback_bucket():
    """获取问题反馈截图专用私有 bucket 句柄（单例）。

    优先 OSS_FEEDBACK_BUCKET / OSS_FEEDBACK_ENDPOINT（建议 omnirank-feedback-private）;
    未配置则 fallback 到 KYC 的 OSS_KYC_BUCKET / OSS_KYC_ENDPOINT,保证向后兼容。
    AccessKey 复用 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET。bucket 必须私有。
    """
    global _feedback_bucket_singleton, _feedback_bucket_name
    if _feedback_bucket_singleton is not None:
        return _feedback_bucket_singleton, _feedback_bucket_name

    access_key_id = os.getenv("OSS_ACCESS_KEY_ID", "").strip()
    access_key_secret = os.getenv("OSS_ACCESS_KEY_SECRET", "").strip()
    bucket_name = (os.getenv("OSS_FEEDBACK_BUCKET", "").strip()
                   or os.getenv("OSS_KYC_BUCKET", "").strip())
    endpoint = (os.getenv("OSS_FEEDBACK_ENDPOINT", "").strip()
                or os.getenv("OSS_KYC_ENDPOINT", "").strip()
                or "oss-cn-shenzhen.aliyuncs.com")

    if not access_key_id or not access_key_secret:
        raise OSSConfigError(
            "OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET 未配置，无法初始化反馈截图私有 bucket"
        )
    if not bucket_name:
        raise OSSConfigError(
            "OSS_FEEDBACK_BUCKET（或兼容的 OSS_KYC_BUCKET）未配置，无法上传反馈截图"
        )

    try:
        import oss2
    except ImportError as e:
        raise OSSConfigError(f"oss2 SDK 未安装: {e}")

    auth = oss2.Auth(access_key_id, access_key_secret)
    bucket = oss2.Bucket(auth, endpoint, bucket_name)
    _feedback_bucket_singleton = bucket
    _feedback_bucket_name = bucket_name
    logger.info(f"[OSS] 反馈截图 bucket 初始化成功: {bucket_name} @ {endpoint}")
    return bucket, bucket_name


def feedback_signed_url_ttl_seconds() -> int:
    """问题反馈截图管理端查看链接有效期。

    默认按“永久可看”的产品诉求给 10 年，但 bucket 仍保持私有，不改公开读。
    如果后续要收紧，只改 OSS_FEEDBACK_URL_TTL_SECONDS 即可。
    """
    raw = os.getenv("OSS_FEEDBACK_URL_TTL_SECONDS", "").strip()
    if not raw:
        return 10 * 365 * 24 * 60 * 60
    try:
        value = int(raw)
    except ValueError as e:
        raise ValueError("OSS_FEEDBACK_URL_TTL_SECONDS 必须是整数秒") from e
    if value <= 0:
        raise ValueError("OSS_FEEDBACK_URL_TTL_SECONDS 必须大于 0")
    return value


def generate_feedback_signed_url(oss_key: str, expires_seconds: int | None = None) -> str:
    """对反馈截图 key 生成签名 URL · 针对反馈 bucket 签名。"""
    ttl = expires_seconds if expires_seconds is not None else feedback_signed_url_ttl_seconds()
    if ttl <= 0:
        raise ValueError("expires_seconds 必须大于 0")
    try:
        bucket, _ = _get_feedback_bucket()
        return bucket.sign_url("GET", oss_key, ttl, slash_safe=True)
    except OSSConfigError:
        raise
    except Exception as e:
        logger.error(f"[OSS] 反馈截图签名失败 key={oss_key}: {type(e).__name__}: {e}")
        raise OSSAccessError(f"签名失败: {e}") from e


def feedback_oss_configured() -> bool:
    """反馈截图 OSS 是否可用(AccessKey + 任一 bucket 名)。"""
    has_key = bool(os.getenv("OSS_ACCESS_KEY_ID", "").strip()
                   and os.getenv("OSS_ACCESS_KEY_SECRET", "").strip())
    has_bucket = bool(os.getenv("OSS_FEEDBACK_BUCKET", "").strip()
                      or os.getenv("OSS_KYC_BUCKET", "").strip())
    return has_key and has_bucket


def _get_bucket():
    """Lazy 获取 OSS Bucket 句柄（单例）"""
    global _bucket_singleton, _bucket_name

    if _bucket_singleton is not None:
        return _bucket_singleton, _bucket_name

    access_key_id = os.getenv("OSS_ACCESS_KEY_ID", "").strip()
    access_key_secret = os.getenv("OSS_ACCESS_KEY_SECRET", "").strip()
    bucket_name = os.getenv("OSS_KYC_BUCKET", "omnirank-kyc").strip()
    endpoint = os.getenv("OSS_KYC_ENDPOINT", "oss-cn-shenzhen.aliyuncs.com").strip()

    if not access_key_id or not access_key_secret:
        raise OSSConfigError(
            "OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET 未配置，无法初始化 KYC 私有 bucket"
        )

    try:
        import oss2
    except ImportError as e:
        raise OSSConfigError(
            f"oss2 SDK 未安装，请确认 requirements.txt 包含 oss2 依赖: {e}"
        )

    auth = oss2.Auth(access_key_id, access_key_secret)
    bucket = oss2.Bucket(auth, endpoint, bucket_name)

    _bucket_singleton = bucket
    _bucket_name = bucket_name
    logger.info(f"[OSS] KYC bucket 初始化成功: {bucket_name} @ {endpoint}")
    return bucket, bucket_name


def build_id_card_key(user_id: int, side: str, ext: str = "jpg") -> str:
    """构造身份证照片 OSS key

    规范: applications/{user_id}/{side}_{ts}_{rand}.{ext}
    - side:  'front' / 'back' / 'selfie'
    - ts:    秒级时间戳
    - rand:  8 位 hex (secrets.token_hex 4 字节), 避免同用户同秒碰撞
    - 不含 application_id: 上传时 application 尚未创建, 提交申请时 key 直接入库
    """
    import secrets
    if side not in ("front", "back", "selfie"):
        raise ValueError(f"不支持的 side: {side}")
    if ext.lower() not in ("jpg", "jpeg", "png"):
        raise ValueError(f"不支持的扩展名: {ext}")
    ts = int(time.time())
    rand = secrets.token_hex(4)
    return f"applications/{user_id}/{side}_{ts}_{rand}.{ext.lower()}"


def build_bug_feedback_key(user_id: int, ext: str = "jpg") -> str:
    """构造问题反馈截图 OSS key。

    规范: feedback/{user_id}/{ts}_{rand}.{ext}
    """
    import secrets
    normalized = ext.lower().lstrip(".")
    if normalized == "jpeg":
        normalized = "jpg"
    if normalized not in ("jpg", "png", "webp", "gif", "bmp"):
        raise ValueError(f"不支持的扩展名: {ext}")
    ts = int(time.time())
    rand = secrets.token_hex(4)
    return f"feedback/{user_id}/{ts}_{rand}.{normalized}"


def upload_id_card_image(
    oss_key: str,
    image_bytes: bytes,
    content_type: str = "image/jpeg",
) -> str:
    """上传身份证照片到私有 bucket

    Args:
        oss_key: 通过 build_id_card_key 构造
        image_bytes: 图片二进制
        content_type: image/jpeg | image/png

    Returns:
        oss_key (原样返回, 方便调用者链式操作)

    Raises:
        OSSUploadError 上传失败
    """
    if content_type not in ("image/jpeg", "image/png"):
        raise ValueError(f"不支持的 content_type: {content_type}")

    try:
        bucket, bucket_name = _get_bucket()
        headers = {
            "Content-Type": content_type,
            # bucket 级 SSE-OSS 已开启, 此 header 保证 object 级也是加密
            "x-oss-server-side-encryption": "AES256",
        }
        result = bucket.put_object(oss_key, image_bytes, headers=headers)
        if result.status != 200:
            raise OSSUploadError(f"put_object HTTP {result.status} key={oss_key}")
        logger.info(f"[OSS] 上传成功 key={oss_key} size={len(image_bytes)}B")
        return oss_key
    except OSSConfigError:
        raise
    except OSSUploadError:
        raise
    except Exception as e:
        logger.error(f"[OSS] 上传失败 key={oss_key}: {type(e).__name__}: {e}")
        raise OSSUploadError(f"上传失败: {e}") from e


def upload_bug_feedback_image(
    oss_key: str,
    image_bytes: bytes,
    content_type: str = "image/jpeg",
) -> str:
    """上传问题反馈截图到私有 bucket（OSS_FEEDBACK_BUCKET 优先,fallback KYC），返回 OSS key。"""
    if content_type not in ("image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"):
        raise ValueError(f"不支持的 content_type: {content_type}")

    try:
        bucket, _bucket_name = _get_feedback_bucket()
        headers = {
            "Content-Type": content_type,
            "x-oss-server-side-encryption": "AES256",
        }
        result = bucket.put_object(oss_key, image_bytes, headers=headers)
        if result.status != 200:
            raise OSSUploadError(f"put_object HTTP {result.status} key={oss_key}")
        logger.info(f"[OSS] 问题反馈截图上传成功 key={oss_key} size={len(image_bytes)}B")
        return oss_key
    except OSSConfigError:
        raise
    except OSSUploadError:
        raise
    except Exception as e:
        logger.error(f"[OSS] 问题反馈截图上传失败 key={oss_key}: {type(e).__name__}: {e}")
        raise OSSUploadError(f"上传失败: {e}") from e


def generate_signed_url(oss_key: str, expires_seconds: int = 900) -> str:
    """生成临时签名 URL（默认 15 分钟）

    仅在请求当时计算签名, 不预先生成. URL 到期后自动失效.

    Args:
        oss_key: OSS 对象 key
        expires_seconds: 有效期秒数, 默认 900 (15 分钟)

    Returns:
        带签名的 https URL

    Raises:
        OSSAccessError 签名失败
    """
    if expires_seconds <= 0 or expires_seconds > 3600:
        raise ValueError("expires_seconds 必须在 (0, 3600] 范围内（最长 1 小时）")

    try:
        bucket, _ = _get_bucket()
        url = bucket.sign_url("GET", oss_key, expires_seconds, slash_safe=True)
        return url
    except OSSConfigError:
        raise
    except Exception as e:
        logger.error(f"[OSS] 签名 URL 失败 key={oss_key}: {type(e).__name__}: {e}")
        raise OSSAccessError(f"签名失败: {e}") from e


def delete_oss_object(oss_key: str) -> bool:
    """删除 OSS 对象（清理 / 撤回用）

    Returns:
        True 删除成功或对象本来就不存在, False 其他异常

    设计为尽量容错: 清理任务批量删除时个别失败不应阻塞整批.
    """
    try:
        bucket, _ = _get_bucket()
        bucket.delete_object(oss_key)
        logger.info(f"[OSS] 删除成功 key={oss_key}")
        return True
    except OSSConfigError:
        raise
    except Exception as e:
        # oss2 对 404 也可能抛异常, 视为幂等成功
        msg = str(e).lower()
        if "404" in msg or "no such key" in msg or "notfound" in msg.replace(" ", ""):
            logger.info(f"[OSS] 删除幂等 key={oss_key} (对象不存在)")
            return True
        logger.warning(f"[OSS] 删除失败 key={oss_key}: {type(e).__name__}: {e}")
        return False


def is_configured() -> bool:
    """轻量探测 OSS 是否配置好（不实际连接，仅看环境变量）"""
    return bool(
        os.getenv("OSS_ACCESS_KEY_ID", "").strip()
        and os.getenv("OSS_ACCESS_KEY_SECRET", "").strip()
    )
