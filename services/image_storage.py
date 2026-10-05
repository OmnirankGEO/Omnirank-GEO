"""
客户图片素材 · 本地存储服务(image_storage)

- 原图保真保留 + Pillow 生成缩略图(列表用)+ 安全尺寸图(文章/发布展示用·避免超大原图)
- 本地落盘 uploads/article-images/{brand_id}/(与 uploads/avatars 同约定·相对 CWD=/app·nginx /uploads/ 对外)
- 不复用 KYC 私有 OSS(那是敏感证件私有桶)· 这里是「本就要对外公开」的文章配图
- 抽象成 save_brand_image / delete_brand_image_files,未来换 OSS 只改这一层

2026-06-02 GEO CTO · 客户资料中心图片素材能力
"""

import io
import uuid
import logging
from pathlib import Path

logger = logging.getLogger("GEO-ImageStorage")

# 相对 CWD(容器内 /app)· nginx location /uploads/ root /app 对外
_BASE_DIR = "uploads/article-images"
_PUBLIC_PREFIX = "/uploads/article-images"

_THUMB_MAX = 360       # 缩略图最长边(列表卡片)
_SAFE_MAX = 1600       # 安全尺寸图最长边(文章/发布展示·避免超大原图直出)
_ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "gif", "bmp"}


def _ext_of(filename: str) -> str:
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else "png"
    return ext if ext in _ALLOWED_EXT else "png"


def _has_alpha(img) -> bool:
    return img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)


def _save_variant(img, dir_path: Path, stem: str, max_side: int) -> str:
    """按最长边等比缩放 + 保存。有透明通道 → PNG(保 LOGO),否则 → JPEG(quality 85)。返回文件名。"""
    from PIL import Image
    im = img.copy()
    im.thumbnail((max_side, max_side), Image.LANCZOS)
    if _has_alpha(im):
        fname = f"{stem}.png"
        im.save(dir_path / fname, "PNG", optimize=True)
    else:
        if im.mode != "RGB":
            im = im.convert("RGB")
        fname = f"{stem}.jpg"
        im.save(dir_path / fname, "JPEG", quality=85, optimize=True)
    return fname


def save_brand_image(brand_id: int, image_bytes: bytes, original_filename: str = "image.png") -> dict:
    """
    存一张客户图片:原图保真 + 缩略图 + 安全尺寸图。
    返回 storage_key / thumbnail_key / safe_size_key / public_url(相对) / width / height。
    public_url 指向「安全尺寸图」(对外展示用压缩图·发布时拼 PUBLIC_BASE_URL 绝对化)。
    """
    from PIL import Image, ImageOps

    ext = _ext_of(original_filename)
    stem = uuid.uuid4().hex
    rel_dir = f"{_BASE_DIR}/{brand_id}"
    dir_path = Path(rel_dir)
    dir_path.mkdir(parents=True, exist_ok=True)

    # 1) 原图保真写盘
    orig_name = f"{stem}.{ext}"
    (dir_path / orig_name).write_bytes(image_bytes)
    storage_key = f"{rel_dir}/{orig_name}"

    # 2) Pillow 读图(先摆正 EXIF 方向)生成缩略图 + 安全尺寸图
    thumbnail_key = safe_size_key = None
    width = height = None
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)
        width, height = img.size
        thumb_name = _save_variant(img, dir_path, f"{stem}_thumb", _THUMB_MAX)
        safe_name = _save_variant(img, dir_path, f"{stem}_safe", _SAFE_MAX)
        thumbnail_key = f"{rel_dir}/{thumb_name}"
        safe_size_key = f"{rel_dir}/{safe_name}"
    except Exception as e:
        logger.warning(f"[image_storage] 缩略图/安全图生成失败(原图已存): {e}")

    # public_url 优先安全尺寸图(压缩图);生成失败兜底原图
    public_name = (safe_size_key or storage_key).rsplit("/", 1)[-1]
    public_url = f"{_PUBLIC_PREFIX}/{brand_id}/{public_name}"

    return {
        "storage_key": storage_key,
        "thumbnail_key": thumbnail_key,
        "safe_size_key": safe_size_key,
        "public_url": public_url,
        "width": width,
        "height": height,
    }


def delete_brand_image_files(*keys):
    """物理删图(软删后异步清理用)。带路径穿越防护:只删 uploads/article-images 下的文件。"""
    base = Path(_BASE_DIR).resolve()
    for key in keys:
        if not key:
            continue
        try:
            p = Path(key).resolve()
            if p.is_relative_to(base) and p.is_file():
                p.unlink()
        except Exception as e:
            logger.warning(f"[image_storage] 删除文件失败 {key}: {e}")
