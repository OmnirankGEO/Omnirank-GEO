"""Fail-closed quality gates for GEO copy, images, and QR references."""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
from typing import Callable, Optional

from PIL import Image, ImageOps


_PHONE_RE = re.compile(r"(?:1[3-9]\d{9})|(?:0\d{2,3}[- ]?\d{7,8})")


def _qr_values(image_bytes: bytes) -> list[str]:
    """Decode every QR payload.  Decoder unavailability is a hard failure."""
    try:
        import zxingcpp
    except ImportError as exc:  # pragma: no cover - packaging contract catches this
        raise RuntimeError("qr_decoder_unavailable") from exc
    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
        values = []
        for barcode in zxingcpp.read_barcodes(image):
            value = str(getattr(barcode, "text", "") or "").strip()
            if value and value not in values:
                values.append(value)
        return values
    except Exception as exc:
        raise ValueError("invalid_qr_image") from exc


def qr_payload_hash(payload: str) -> str:
    return hashlib.sha256(str(payload).encode("utf-8")).hexdigest()


def validate_qr_reference(image_bytes: bytes) -> dict:
    """Accept only an image with exactly one decodable user-provided QR."""
    if not image_bytes or len(image_bytes) > 5 * 1024 * 1024:
        raise ValueError("qr_file_size_invalid")
    values = _qr_values(image_bytes)
    if len(values) != 1:
        raise ValueError("qr_requires_exactly_one_decodable_code")
    payload = values[0]
    return {
        "payload_hash": qr_payload_hash(payload),
        "payload_preview": (payload[:20] + "…") if len(payload) > 20 else payload,
    }


def _reference_secret() -> bytes:
    secret = (os.environ.get("JWT_SECRET_KEY") or os.environ.get("SECRET_KEY") or "").strip()
    if not secret:
        raise RuntimeError("qr_reference_signing_unavailable")
    return secret.encode("utf-8")


def sign_qr_reference(
    *, user_id: int, reference_id: str, payload_hash: str,
    organization_id: Optional[int] = None,
) -> str:
    """上传凭证签名;组织身份上传把 organization_id 绑进签名(2026-07-23 P1-1)。

    个人身份(organization_id=None)报文与历史三段式完全一致,存量行为不变;
    组织上传生成四段式签名,篡改/跨组织重用都会校验失败。
    """
    org_part = "" if organization_id is None else str(int(organization_id))
    message = f"{int(user_id)}\n{reference_id}\n{payload_hash}\n{org_part}".encode("utf-8")
    return hmac.new(_reference_secret(), message, hashlib.sha256).hexdigest()


def verify_qr_reference_token(
    *, user_id: int, reference_id: str, payload_hash: str, reference_token: str,
    organization_id: Optional[int] = None,
) -> bool:
    try:
        expected = sign_qr_reference(
            user_id=user_id, reference_id=reference_id, payload_hash=payload_hash,
            organization_id=organization_id,
        )
    except RuntimeError:
        return False
    return hmac.compare_digest(expected, str(reference_token or ""))


def verify_generated_qr(image_bytes: bytes, expected_payload_hash: str) -> dict:
    """Generated QR must decode to the exact uploaded payload, never look-alike."""
    try:
        values = _qr_values(image_bytes)
    except (RuntimeError, ValueError) as exc:
        return {"passed": False, "flags": [{"code": str(exc)}]}
    hashes = [qr_payload_hash(value) for value in values]
    passed = len(hashes) == 1 and hashes[0] == str(expected_payload_hash or "")
    return {
        "passed": passed,
        "flags": [] if passed else [{"code": "qr_payload_mismatch_or_unreadable"}],
        "decoded_count": len(values),
    }


def _compact_text(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", str(value or "")).lower()


def _expected_size_ratio(size: str) -> Optional[float]:
    try:
        width, height = (int(part) for part in str(size).split(":"))
        if width > 0 and height > 0:
            return width / height
    except (TypeError, ValueError):
        pass
    return None


async def visual_qa(
    image_bytes: bytes,
    *,
    size: str,
    exact_copy: dict,
    brand: dict,
    evidence: dict,
    contact: dict,
    before_vision: Optional[Callable[[], object]] = None,
    internal_lineage_values: Optional[list[str]] = None,
    forbidden_brand_name: Optional[str] = None,
) -> dict:
    """Check geometry, exact facts/contact and OCR before an image is deliverable.

    分层口径(Owner 2026-07-22):返回 ``{passed, errors, warnings}``,``passed``
    只看 errors。errors 只装法律红线与功能正确性硬失败(图片不可用、尺寸
    不符、QR payload/hash 与用户上传不一致、内部标识泄漏、质检服务不可用);
    品牌名/逐字文案/无证据数字/联系方式提醒全部进 warnings——提醒不阻断。

    The existing configured vision model supplies OCR/risk evidence.  If it is
    unavailable, the gate fails closed so a visually unchecked image can never
    become a final asset(功能正确性,非合规审查).

    ``forbidden_brand_name`` is the anonymize-mode reverse check: the frozen
    snapshot brand name must never appear in the generated image (the provider
    brand is blanked so ``brand_name_missing`` skips, but the name can still
    leak in via copy).  Presence in OCR records a warning(提醒,不再硬失败).
    """
    from services.marketing.content_center import error_entry, warning_entry

    errors: list[dict] = []
    warnings: list[dict] = []
    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes)))
        width, height = image.size
        expected_ratio = _expected_size_ratio(size)
        if not width or not height or (expected_ratio and abs(width / height - expected_ratio) > 0.04):
            errors.append(error_entry(
                "channel_dimensions_invalid", actual=[width, height], expected=size,
            ))
    except Exception:
        return {
            "passed": False,
            "errors": [error_entry("image_decode_failed")],
            "warnings": [],
        }

    from tools.vision.image_describe import describe_image_structured

    vision = await describe_image_structured(
        image_bytes, "geo-content.png", before_provider_call=before_vision,
    )
    if not vision.get("vision_ok"):
        errors.append(error_entry("visual_qa_unavailable"))
        ocr_text = ""
    else:
        ocr_text = str(vision.get("ocr_text") or "")
        observed = _compact_text(ocr_text)
        for field, value in exact_copy.items():
            expected = _compact_text(value)
            if expected and expected not in observed:
                warnings.append(warning_entry("exact_copy_missing", field=field))
        brand_name = _compact_text(brand.get("name") or brand.get("brand_name") or "")
        if brand_name and brand_name not in observed:
            warnings.append(warning_entry("brand_name_missing"))
        # 匿名晒单反向检测(与 brand_name_missing 同源):冻结快照品牌名出现在
        # 成图 → 提醒用户检查(Owner 2026-07-22:提醒不阻断)。
        forbidden_name = _compact_text(forbidden_brand_name)
        if forbidden_name and forbidden_name in observed:
            warnings.append(warning_entry("brand_name_forbidden_when_anonymous"))

        allowed_numbers = set()
        for source in (exact_copy, evidence):
            allowed_numbers.update(re.findall(r"\d+(?:\.\d+)?%?", json.dumps(source, ensure_ascii=False)))
        unexpected = sorted(set(re.findall(r"\d+(?:\.\d+)?%?", ocr_text)) - allowed_numbers)
        if unexpected:
            warnings.append(warning_entry("visual_number_without_evidence", detail=unexpected))

    risk_flags = set(vision.get("risk_flags") or [])
    mode = contact.get("mode")
    if mode == "none":
        try:
            decoded = _qr_values(image_bytes)
            if decoded:
                warnings.append(warning_entry("contact_visible_when_disabled", detail="local_qr_detected"))
        except (RuntimeError, ValueError) as exc:
            # 解码器不可用/图片不可读:功能正确性硬失败,不属合规提醒。
            errors.append(error_entry(str(exc)))
        if risk_flags.intersection({"qrcode", "phone"}) or _PHONE_RE.search(ocr_text) or "二维码" in ocr_text:
            warnings.append(warning_entry("contact_visible_when_disabled"))
    elif mode == "text" and _compact_text(contact.get("text")) not in _compact_text(ocr_text):
        warnings.append(warning_entry("contact_text_missing"))
    elif mode == "qr":
        qr = verify_generated_qr(
            image_bytes,
            str((contact.get("qr_reference") or {}).get("payload_hash") or ""),
        )
        if not qr["passed"]:
            # 功能正确性硬失败:成品 QR payload/hash 必须与用户上传一致。
            errors.extend(error_entry(str(flag.get("code") or "qr_payload_mismatch_or_unreadable")) for flag in qr["flags"])

    for value in internal_lineage_values or []:
        compact = _compact_text(value)
        if compact and compact in _compact_text(ocr_text):
            # 数据隔离是安全底线,不是合规审查:保持硬拦截。
            errors.append(error_entry("internal_lineage_visible"))

    return {
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
        "ocr_text": ocr_text[:2000],
        "width": width,
        "height": height,
    }
