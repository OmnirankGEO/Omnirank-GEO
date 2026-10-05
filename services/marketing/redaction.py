"""Redaction engine for deal-showcase ("晒成交") private materials.

Inputs are images already stored under the private upload root.  Automatic
detection uses the configured structured vision describe (OCR text + privacy
risk flags); when no reliable bounding boxes are available, PII is located by
regex over the OCR text and mapped to whole-line regions estimated from the
image geometry.  Regions are pixelated with Pillow (``strength`` = pixel block
size tier).  Manual regions, per-region restore and strength adjustment are
first-class; a separate preview image is generated for every state change.

Fail-soft contract: when detection is unavailable the engine returns zero
automatic regions plus an explicit ``detection="unavailable"`` note — manual
redaction is never blocked.  Original bytes never leave the private root;
preview and confirmed output are stored under distinct private keys.
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
import uuid
from typing import Optional

logger = logging.getLogger("GEO-Deal-Redaction")

REGION_KINDS = (
    "name", "avatar", "phone", "wechat", "address", "idcard", "bank",
    "order_no", "contract_no", "seal", "signature", "customer_name",
    "amount", "other",
)
REDACTION_SOURCES = ("auto", "manual")
# strength = 像素块大小档位(块越大越糊)
STRENGTH_BLOCKS = {"light": 10, "medium": 20, "heavy": 36}
STRENGTHS = tuple(STRENGTH_BLOCKS)
DEFAULT_STRENGTH = "medium"
REDACTION_STATUSES = ("none", "detected", "previewed", "confirmed")
REDACTION_ACTIONS = ("auto", "add", "restore", "strength", "acknowledge", "confirm")

_MAX_AUTO_REGIONS = 60
# 检测能力边界(诚实化):当前 vision 结构化输出只有 OCR 文本+risk_flags,
# 没有 bounding box,头像/签名/公章三类永无自动区域,必须人工框选复核。
MANUAL_REQUIRED_KINDS = ("avatar", "seal", "signature")


def default_redaction_state() -> dict:
    return {
        "auto_regions": [],
        "manual_regions": [],
        "restored_ids": [],
        "strength": DEFAULT_STRENGTH,
        "preview_key": "",
        "preview_sha256": "",
        "output_key": "",
        "output_sha256": "",
        "status": "none",
        "detection": "",
        "manual_required_kinds": [],
        "manual_reviewed": False,
    }


def normalize_strength(value: str) -> str:
    strength = str(value or DEFAULT_STRENGTH).strip().lower()
    if strength not in STRENGTH_BLOCKS:
        raise ValueError("redaction_strength_invalid")
    return strength


def normalize_region_kind(value: str) -> str:
    kind = str(value or "other").strip().lower()
    return kind if kind in REGION_KINDS else "other"


def _new_region(kind: str, box: list, *, source: str) -> dict:
    return {
        "id": uuid.uuid4().hex[:16],
        "kind": normalize_region_kind(kind),
        "box": [int(v) for v in list(box or [0, 0, 0, 0])[:4]],
        "source": source if source in REDACTION_SOURCES else "manual",
        "active": True,
    }


def add_manual_region(state: dict, *, kind: str, box: list) -> dict:
    region = _new_region(kind, box, source="manual")
    if not any(region["box"]):
        raise ValueError("redaction_box_invalid")
    state.setdefault("manual_regions", []).append(region)
    return region


def restore_region(state: dict, region_id: str) -> dict:
    """Deactivate one region (manual or auto); it stays auditable."""
    region_id = str(region_id or "").strip()
    for bucket in ("auto_regions", "manual_regions"):
        for region in state.get(bucket) or []:
            if str(region.get("id")) == region_id:
                region["active"] = False
                restored = state.setdefault("restored_ids", [])
                if region_id not in restored:
                    restored.append(region_id)
                return region
    raise ValueError("redaction_region_not_found")


def active_regions(state: dict) -> list[dict]:
    """Automatic + manual regions stack; restored ids are excluded."""
    restored = set(state.get("restored_ids") or [])
    regions: list[dict] = []
    for bucket in ("auto_regions", "manual_regions"):
        for region in state.get(bucket) or []:
            if region.get("active") and str(region.get("id")) not in restored:
                regions.append(region)
    return regions


# ---------------------------------------------------------------------------
# 自动检测:OCR 文本 + 正则 PII 检测 + 整行区域估计
# ---------------------------------------------------------------------------
# 手机号补国际码/空格横线变体;座机保留原口径
_PHONE_RE = re.compile(r"(?:\+?86[- ]?)?1[3-9]\d[- ]?\d{4}[- ]?\d{4}|0\d{2,3}[- ]?\d{7,8}")
_WECHAT_RE = re.compile(r"(?:微信|wechat|wx)\s*(?:号)?\s*[:：]?\s*[A-Za-z0-9_\-]{5,20}", re.IGNORECASE)
_IDCARD_RE = re.compile(r"\d{17}[\dXx]")
_BANK_RE = re.compile(r"\d{16,19}")
_ORDER_RE = re.compile(r"订单\s*(?:号|编号|no\.?)?\s*[:：#]?\s*[A-Za-z0-9\-]{5,}", re.IGNORECASE)
_CONTRACT_RE = re.compile(r"合同\s*(?:编号|号)?\s*[:：#]?\s*[A-Za-z0-9\u4e00-\u9fff\-]{3,}", re.IGNORECASE)
# 地址放宽:关键词(地址/收货/送货/住址)或 省市区县+路街道号栋单元室 组合
_ADDRESS_RE = re.compile(r"(?:地址|收货|送货|住址)\s*[:：]?\s*[\u4e00-\u9fff0-9]{4,}|[\u4e00-\u9fff]{2}(?:省|市|区|县).{2,30}(?:路|街|道|号|栋|单元|室)")
# 金额补口语写法:"5万"/"尾款3万"/"到账8w"/"12k"/"五千块" 都要命中;
# 中文数字为简单映射(数字+十百千万收尾,或单字+元/块),非全量解析器,
# "(?![零一二三四五六七八九两])" 排除"万一"这类日常词误判。
_AMOUNT_RE = re.compile(
    r"金额|¥|￥|\d+(?:\.\d+)?\s*(?:万元|万|元|块|千|w|W|k|K)"
    r"|[零一二三四五六七八九两十]{1,8}[十百千万](?![零一二三四五六七八九两])(?:元|块|块钱)?"
    r"|[零一二三四五六七八九两](?:元|块)"
)
_CUSTOMER_RE = re.compile(r"客户\s*(?:名称|姓名)?\s*[:：]\s*[\u4e00-\u9fff]{2,4}|[\u4e00-\u9fff]{1,3}(?:总|经理|老板)")
_NAME_RE = re.compile(r"(?:姓名|名字)\s*[:：]\s*[\u4e00-\u9fff]{2,4}")

# ASCII 人名/英文头衔(2026-07-23 外部审查 P1-4):"Alice Smith (CEO)"、
# "Contact Mr. Zhang"、"张伟 CEO" 这类混合语境都要命中整行打码。
_ASCII_EXEC_TITLE = r"(?:CEO|CTO|COO|CFO|CMO|CIO|VP|GM|Manager|Director|President|Chairman|Founder)"
_ASCII_NAME_WITH_TITLE_RE = re.compile(
    # "Alice Smith (CEO)" / "Alice Smith, CEO" / "Alice (Manager)"
    r"[A-Z][A-Za-z]{1,19}(?:\s+[A-Z][A-Za-z]{1,19})?\s*[,(]?\s*" + _ASCII_EXEC_TITLE + r"\b\)?"
    # "Mr. Zhang" / "Ms Alice Smith" / "Dr. Lee"
    r"|(?:Mr|Ms|Mrs|Miss|Dr|Prof)\.?\s+[A-Z][A-Za-z]{1,19}(?:\s+[A-Z][A-Za-z]{1,19})?"
    # CJK name + English title: "张伟 CEO"
    r"|[一-龥]{2,4}\s*" + _ASCII_EXEC_TITLE + r"\b"
)
# Bare two-word capitalized ASCII name ("Alice Smith"): OCR 隐私场景按
# fail-safe 方向打码,误打可人工恢复(restore 是一等能力)。
_ASCII_BARE_NAME_RE = re.compile(r"\b[A-Z][a-z]{1,19}\s+[A-Z][a-z]{1,19}\b")

_LINE_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("idcard", _IDCARD_RE),
    ("phone", _PHONE_RE),
    ("wechat", _WECHAT_RE),
    ("order_no", _ORDER_RE),
    ("contract_no", _CONTRACT_RE),
    ("bank", _BANK_RE),
    ("address", _ADDRESS_RE),
    ("customer_name", _CUSTOMER_RE),
    ("customer_name", _ASCII_NAME_WITH_TITLE_RE),
    ("name", _NAME_RE),
    ("name", _ASCII_BARE_NAME_RE),
    ("amount", _AMOUNT_RE),
)


def regions_from_ocr(ocr_text: str, *, width: int, height: int) -> list[dict]:
    """Map OCR lines that match PII patterns to whole-line pixel regions."""
    width, height = int(width or 0), int(height or 0)
    if width <= 0 or height <= 0:
        return []
    lines = [line for line in str(ocr_text or "").splitlines() if line.strip()]
    if not lines:
        return []
    line_height = max(height / len(lines), 1.0)
    pad = max(int(line_height * 0.15), 1)
    regions: list[dict] = []
    for index, line in enumerate(lines[:_MAX_AUTO_REGIONS]):
        kind = next((name for name, pattern in _LINE_RULES if pattern.search(line)), None)
        if kind is None:
            continue
        top = max(int(index * line_height) - pad, 0)
        bottom = min(int((index + 1) * line_height) + pad, height)
        regions.append(_new_region(kind, [0, top, width, max(bottom - top, 1)], source="auto"))
    return regions


async def auto_detect(image_bytes: bytes, *, width: int, height: int) -> dict:
    """Vision OCR → PII regions. Unavailable detection degrades to empty + note.

    能力边界诚实化:avatar/seal/signature 没有任何自动检测路径(vision 无
    bbox),每次检测都随结果返回 ``manual_required_kinds`` 提示人工复核。
    """
    manual_kinds = list(MANUAL_REQUIRED_KINDS)
    try:
        from tools.vision.image_describe import describe_image_structured

        vision = await describe_image_structured(image_bytes, "deal-material.png")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[redaction] 视觉检测异常(不阻断手动打码): %s", exc)
        return {"regions": [], "ocr_text": "", "detection": "unavailable",
                "manual_required_kinds": manual_kinds}
    ocr_text = str((vision or {}).get("ocr_text") or "")
    if not (vision or {}).get("vision_ok"):
        return {"regions": [], "ocr_text": ocr_text, "detection": "unavailable",
                "manual_required_kinds": manual_kinds}
    regions = regions_from_ocr(ocr_text, width=width, height=height)
    risk_flags = set((vision or {}).get("risk_flags") or [])
    if risk_flags.intersection({"privacy", "phone"}) and not regions:
        # 视觉确认有隐私风险但行估计没定位到:整图提示人工,不猜测区域。
        return {"regions": [], "ocr_text": ocr_text, "detection": "risk_flagged_manual_required",
                "manual_required_kinds": manual_kinds}
    return {"regions": regions, "ocr_text": ocr_text, "detection": "ocr_regex",
            "manual_required_kinds": manual_kinds}


# ---------------------------------------------------------------------------
# 像素化渲染(PIL · strength=块大小档位)
# ---------------------------------------------------------------------------
def _clamp_box(box: list, width: int, height: int) -> tuple[int, int, int, int]:
    x, y, w, h = (int(v) for v in list(box or [0, 0, 0, 0])[:4])
    x = min(max(x, 0), width)
    y = min(max(y, 0), height)
    w = min(max(w, 0), width - x)
    h = min(max(h, 0), height - y)
    return x, y, w, h


def render_redacted(image_bytes: bytes, regions: list[dict], *, strength: str = DEFAULT_STRENGTH) -> bytes:
    """Pixelate every active region and return re-encoded PNG bytes."""
    from PIL import Image

    block = STRENGTH_BLOCKS[normalize_strength(strength)]
    try:
        with Image.open(io.BytesIO(image_bytes)) as probe:
            image = probe.convert("RGB")
            image.load()
    except Exception as exc:
        raise ValueError("redaction_image_invalid") from exc
    width, height = image.size
    for region in regions or []:
        x, y, w, h = _clamp_box(region.get("box"), width, height)
        if w <= 0 or h <= 0:
            continue
        area = image.crop((x, y, x + w, y + h))
        small = area.resize((max(w // block, 1), max(h // block, 1)), Image.BILINEAR)
        image.paste(small.resize((w, h), Image.NEAREST), (x, y))
    buffer = io.BytesIO()
    # PNG 保存会从 im.info 回写 icc_profile(Pillow PngImagePlugin):保存前显式
    # 剔除并传空,ICC 里可能带设备型号/软件名/版权字段。
    image.info.pop("icc_profile", None)
    image.save(buffer, "PNG", optimize=True, icc_profile=b"")  # 不传 exif:预览/成品同样剥离元数据
    return buffer.getvalue()


def confirm_requires_manual_review(state: dict) -> bool:
    """risk_flagged_manual_required 检测结果下,confirm 前必须人工复核过一遍。

    人工复核的判据:显式 acknowledge,或检测后新增过手动区域(复核行为本身)。
    """
    if str((state or {}).get("detection") or "") != "risk_flagged_manual_required":
        return False
    return not bool((state or {}).get("manual_reviewed"))


# ---------------------------------------------------------------------------
# 私有存储读写(预览图与成品图分开 key;原图永不出私有根)
# ---------------------------------------------------------------------------
def save_redacted_image(owner_key: str, image_bytes: bytes, *, filename: str) -> dict:
    from services.marketing import material_storage

    return material_storage.save_material_image(owner_key, image_bytes, filename, private=True)


def read_private_image(ref: str, *, expected_sha256: str = "") -> tuple[bytes, str]:
    """Read a private material reference after API-side authorization."""
    from services.marketing import material_storage

    data, mime = material_storage.read_material_public_reference(str(ref or ""))
    if expected_sha256 and hashlib.sha256(data).hexdigest() != str(expected_sha256):
        raise ValueError("redaction_image_hash_mismatch")
    return data, mime


def redacted_data_uri(ref: str, *, expected_sha256: str = "") -> str:
    """Provider-safe data URI for a confirmed redacted material (垫图用)."""
    import base64

    data, mime = read_private_image(ref, expected_sha256=expected_sha256)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("redaction_image_too_large")
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def redacted_ref_for_generation(material: dict) -> Optional[dict]:
    """The generation chain only consumes confirmed redaction output."""
    redaction = (material or {}).get("redaction") or {}
    if redaction.get("status") != "confirmed":
        return None
    ref = str(redaction.get("output_key") or redaction.get("preview_key") or "")
    if not ref:
        return None
    return {"ref": ref, "sha256": str(redaction.get("output_sha256") or redaction.get("preview_sha256") or "")}
