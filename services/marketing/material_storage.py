"""
services/marketing/material_storage.py — 营销物料自有存储(图落自有存储 · §D.1)

复用 services/image_storage 的 Pillow 变体逻辑(_save_variant/_has_alpha/_ext_of),
换独立目录 uploads/marketing-materials/{owner_key}/(需在 nginx.conf 加同名 location 白名单)。
未来换 OSS 只改这一层(与 image_storage 同 OSS-swap-seam 约定)。

owner_key 用 f"u{user_id}"(用户物料)或 f"p{case_id}"(军师 platform 物料)。
"""
import io
import hashlib
import base64
import logging
import os
import uuid
from pathlib import Path

from services.image_storage import _has_alpha, _save_variant  # 复用变体逻辑

logger = logging.getLogger("GEO-Marketing-Storage")

_BASE_DIR = "uploads/marketing-materials"
_PUBLIC_PREFIX = "/uploads/marketing-materials"
_PRIVATE_MATERIAL_PREFIX = "private://marketing-materials"
_THUMB_MAX = 360
_SAFE_MAX = 1600


def _private_upload_root() -> Path:
    """Persistent application data that is outside every Nginx static root."""
    configured = os.environ.get("MARKETING_PRIVATE_UPLOAD_ROOT", "/app/data/marketing-private")
    return Path(configured).resolve()


def _private_qr_dir() -> Path:
    """Return the private QR root on the persistent application-data volume.

    Production mounts ``/app/data`` durably across blue/green containers and
    Nginx exposes only selected ``/app/uploads`` children. Keeping originals
    under ``/app/data`` makes a guessed static URL structurally unreachable.
    Tests may point the root at a throwaway persistent-volume fixture.
    """
    return _private_upload_root() / "qr-inputs"


def _private_material_dir() -> Path:
    return _private_upload_root() / "geo-materials"


def _reencode_stripped(image_bytes: bytes):
    """Decode, fix orientation, and re-encode without EXIF/metadata.

    Raw upload/provider bytes are never persisted: every saved image goes
    through Pillow re-encoding (no ``exif``/``info`` metadata carried over),
    so EXIF tags, internal paths and tool metadata cannot leak. Returns
    ``(clean_bytes, ext, width, height, image)``. Failure is closed — the
    caller must not fall back to storing the original bytes.
    """
    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(image_bytes)) as probe:
            fmt = str(probe.format or "").upper()
            image = ImageOps.exif_transpose(probe)
            image.load()
    except Exception as exc:
        raise ValueError("material_image_invalid") from exc
    # ICC 也是元数据:Pillow 保存时会从 im.info 回写 icc_profile(PNG 明确如此),
    # 里面可能带设备型号/软件名/版权字段——保存前显式剔除并传空。
    image.info.pop("icc_profile", None)
    buffer = io.BytesIO()
    if fmt == "JPEG" and not _has_alpha(image):
        if image.mode != "RGB":
            image = image.convert("RGB")
        image.save(buffer, "JPEG", quality=92, optimize=True, icc_profile=b"")
        ext = "jpg"
    elif fmt == "WEBP":
        image.save(buffer, "WEBP", quality=92, method=6, icc_profile=b"")
        ext = "webp"
    else:
        image.save(buffer, "PNG", optimize=True, icc_profile=b"")
        ext = "png"
    width, height = image.size
    return buffer.getvalue(), ext, int(width), int(height), image


def qr_reference_data_uri(owner_key: str, reference_id: str, expected_sha256: str) -> tuple[str, bytes]:
    """Resolve an owner's private upload to a bounded provider-safe data URI.

    The persistent relative path is never sent to a remote provider and cannot
    be used to traverse into another tenant's upload directory.
    """
    safe_owner = "".join(ch for ch in str(owner_key) if ch.isalnum() or ch in "._-")[:40] or "anon"
    filename = str(reference_id)
    if not filename or "/" in filename or "\\" in filename or filename in {".", ".."}:
        raise ValueError("qr_reference_path_invalid")
    path = (_private_qr_dir() / safe_owner / filename).resolve()
    owner_dir = (_private_qr_dir() / safe_owner).resolve()
    if path.parent != owner_dir or not path.is_file():
        raise ValueError("qr_reference_missing")
    data = path.read_bytes()
    if not data or len(data) > 5 * 1024 * 1024:
        raise ValueError("qr_reference_size_invalid")
    if not hashlib.sha256(data).hexdigest() == str(expected_sha256 or ""):
        raise ValueError("qr_reference_hash_mismatch")
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as image:
            mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(str(image.format or "").upper(), "")
    except Exception as exc:
        raise ValueError("qr_reference_type_invalid") from exc
    if not mime:
        raise ValueError("qr_reference_type_invalid")
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}", data


_WHITELABEL_LOGO_PREFIX = "/uploads/whitelabel-logos/"
_WHITELABEL_LOGO_MAX_BYTES = 3 * 1024 * 1024


def whitelabel_logo_data_uri(logo_url: str) -> str:
    """本地存储的白标 logo → provider 安全 data URI(照 QR 垫图链路口径)。

    只接受 ``/uploads/whitelabel-logos/`` 前缀(referral_api 白标 logo 上传落盘
    位置),路径解析后必须仍在该目录内(防穿越);公网 https URL 由调用方直接
    透传,不经本函数。任何失败(缺失/超限/非图片)抛 ValueError,由调用方
    静默降级为纯文字品牌名——logo 读取失败绝不阻断生成。
    """
    value = str(logo_url or "").strip().split("?", 1)[0].split("#", 1)[0]
    if not value.startswith(_WHITELABEL_LOGO_PREFIX) or "\\" in value:
        raise ValueError("logo_reference_invalid")
    relative = value[len(_WHITELABEL_LOGO_PREFIX):]
    base = Path(_WHITELABEL_LOGO_PREFIX.strip("/")).resolve()
    path = (base / relative).resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ValueError("logo_reference_invalid") from exc
    if not path.is_file():
        raise ValueError("logo_reference_missing")
    data = path.read_bytes()
    if not data or len(data) > _WHITELABEL_LOGO_MAX_BYTES:
        raise ValueError("logo_reference_size_invalid")
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as image:
            mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(str(image.format or "").upper(), "")
    except Exception as exc:
        raise ValueError("logo_reference_type_invalid") from exc
    if not mime:
        raise ValueError("logo_reference_type_invalid")
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def save_qr_reference(owner_key: str, image_bytes: bytes) -> dict:
    """Store a validated QR input outside every public uploads prefix.

    The upload is re-encoded (EXIF/metadata stripped) before it is persisted;
    the re-encoded image must still decode to the exact same QR payload,
    otherwise the reference is rejected instead of stored.
    """
    import hashlib
    from PIL import Image, ImageOps

    if not image_bytes or len(image_bytes) > 5 * 1024 * 1024:
        raise ValueError("qr_reference_size_invalid")
    from services.marketing.quality_assurance import validate_qr_reference

    try:
        validation = validate_qr_reference(image_bytes)
    except RuntimeError:
        raise
    except Exception as exc:
        raise ValueError("qr_reference_type_invalid") from exc
    try:
        with Image.open(io.BytesIO(image_bytes)) as probe:
            image = ImageOps.exif_transpose(probe)
            image.load()
        image.info.pop("icc_profile", None)  # 与 _reencode_stripped 同口径:ICC 一并剥离
        buffer = io.BytesIO()
        image.save(buffer, "PNG", optimize=True, icc_profile=b"")
        clean = buffer.getvalue()
    except Exception as exc:
        raise ValueError("qr_reference_type_invalid") from exc
    if validate_qr_reference(clean)["payload_hash"] != validation["payload_hash"]:
        raise ValueError("qr_reference_type_invalid")
    safe_owner = "".join(ch for ch in str(owner_key) if ch.isalnum() or ch in "._-")[:40] or "anon"
    owner_dir = _private_qr_dir() / safe_owner
    owner_dir.mkdir(parents=True, exist_ok=True)
    reference_id = f"{uuid.uuid4().hex}.png"
    path = owner_dir / reference_id
    path.write_bytes(clean)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return {
        "reference_id": reference_id,
        "size_bytes": len(clean),
        "sha256": hashlib.sha256(clean).hexdigest(),
    }


def save_material_image(owner_key: str, image_bytes: bytes, original_filename: str = "poster.png",
                        *, private: bool = False) -> dict:
    """存一张营销物料成品图。返回 storage_key/thumbnail_key/public_url/width/height/size_bytes/sha256。

    原图字节不再原样落盘:统一重编码(EXIF/元数据剥离,方向先摆正),
    解码失败直接报错——宁可失败也不存带元数据的原始字节。
    """
    import hashlib

    clean_bytes, ext, width, height, img = _reencode_stripped(image_bytes)
    stem = uuid.uuid4().hex
    safe_owner = "".join(ch for ch in str(owner_key) if ch.isalnum() or ch in "._-")[:40] or "anon"
    if private:
        dir_path = (_private_material_dir() / safe_owner).resolve()
        relative_dir = ""
    else:
        relative_dir = f"{_BASE_DIR}/{safe_owner}"
        dir_path = Path(relative_dir)
    dir_path.mkdir(parents=True, exist_ok=True)

    orig_name = f"{stem}.{ext}"
    (dir_path / orig_name).write_bytes(clean_bytes)
    storage_key = (
        str((dir_path / orig_name).resolve())
        if private else f"{relative_dir}/{orig_name}"
    )

    thumbnail_key = None
    try:
        thumb_name = _save_variant(img, dir_path, f"{stem}_thumb", _THUMB_MAX)
        thumbnail_key = (
            str((dir_path / thumb_name).resolve())
            if private else f"{relative_dir}/{thumb_name}"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[material_storage] 缩略图生成失败(原图已存): %s", e)

    url_prefix = _PRIVATE_MATERIAL_PREFIX if private else _PUBLIC_PREFIX
    public_url = f"{url_prefix}/{safe_owner}/{orig_name}"
    thumb_url = (f"{url_prefix}/{safe_owner}/{Path(thumbnail_key).name}"
                 if thumbnail_key else public_url)
    return {
        "storage_key": storage_key,
        "reference_id": orig_name,
        "thumbnail_key": thumbnail_key,
        "public_url": public_url,
        "thumbnail_url": thumb_url,
        "width": width,
        "height": height,
        "size_bytes": len(clean_bytes),
        "sha256": hashlib.sha256(clean_bytes).hexdigest(),
    }


def material_reference_exists(stored: dict) -> bool:
    """Validate a recovery reference without accepting arbitrary filesystem paths."""
    key = str((stored or {}).get("storage_key") or "")
    if not key:
        return False
    path = Path(key).resolve()
    allowed_bases = (Path(_BASE_DIR).resolve(), _private_material_dir())
    if not any(_is_relative_to(path, base) for base in allowed_bases):
        return False
    return path.is_file() and path.stat().st_size > 0


def read_material_public_reference(public_url: str) -> tuple[bytes, str]:
    """Read an owned material after API authorization; reject non-local refs."""
    value = str(public_url or "")
    public_prefix = f"{_PUBLIC_PREFIX}/"
    private_prefix = f"{_PRIVATE_MATERIAL_PREFIX}/"
    if value.startswith(private_prefix):
        base = _private_material_dir()
        relative = value[len(private_prefix):]
    elif value.startswith(public_prefix):
        base = Path(_BASE_DIR).resolve()
        relative = value[len(public_prefix):]
    else:
        raise ValueError("material_storage_reference_invalid")
    if not relative or "\\" in relative:
        raise ValueError("material_storage_reference_invalid")
    path = (base / relative).resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ValueError("material_storage_reference_invalid") from exc
    if not path.is_file():
        raise ValueError("material_storage_reference_missing")
    data = path.read_bytes()
    if not data or len(data) > 25 * 1024 * 1024:
        raise ValueError("material_storage_reference_size_invalid")
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(path.suffix.lower())
    if not mime:
        raise ValueError("material_storage_reference_type_invalid")
    return data, mime


def delete_material_reference(public_url: str) -> bool:
    """落盘后事务失败的孤儿清理:按 owned 引用删主文件及同源缩略图。

    与 read_material_public_reference 同口径做前缀/路径校验,绝不接受任意
    路径;引用非法或文件不存在返回 False(清理尽力而为,不向上抛)。
    """
    value = str(public_url or "")
    public_prefix = f"{_PUBLIC_PREFIX}/"
    private_prefix = f"{_PRIVATE_MATERIAL_PREFIX}/"
    if value.startswith(private_prefix):
        base = _private_material_dir()
        relative = value[len(private_prefix):]
    elif value.startswith(public_prefix):
        base = Path(_BASE_DIR).resolve()
        relative = value[len(public_prefix):]
    else:
        return False
    if not relative or "\\" in relative:
        return False
    path = (base / relative).resolve()
    try:
        path.relative_to(base)
    except ValueError:
        return False
    removed = False
    # 缩略图由 _save_variant 生成({stem}_thumb.png|jpg),与原图同目录。
    candidates = [
        path,
        path.with_name(f"{path.stem}_thumb.png"),
        path.with_name(f"{path.stem}_thumb.jpg"),
    ]
    for candidate in candidates:
        try:
            candidate.unlink()
            removed = True
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("[material_storage] 孤儿清理失败 %s: %s", candidate, exc)
    return removed


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False
