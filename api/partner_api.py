"""
代理申请审核制 API（v1.1）

10 个端点:
  1. GET  /api/partner/about                         介绍页 + flag 状态（flag off 可读，返 enabled=false）
  2. GET  /api/partner/apply/status                  当前申请状态 + 月度次数
  3. POST /api/partner/apply/upload-id-card          上传身份证 + OCR（multipart/form-data）
  4. POST /api/partner/apply                         提交申请（含 AI 审核 + 协议签署，事务化）
  5. POST /api/partner/apply/{app_id}/withdraw       撤回申请
  6. GET  /api/partner/agreement/{version}           协议全文（Markdown）
  7. GET  /api/partner/agreements/mine               我签过的协议列表
  8. GET  /api/partner/id-card-view/{app_id}/{side}  签发临时 URL（只能看自己的）
  9. GET  /api/partner/stats                         服务方权益入口
 10. POST /api/partner/downgrade                     主动退出服务商

Feature flag: PARTNER_APPLY_ENABLED
  - false（默认）: 写操作端点返 404 + /about 返 {"enabled": false}
  - true:         正常流程

前置依赖已就位:
  - db/partner_schema.py                   3 表 + agent_verified 字段
  - services/oss_service.py                私有 bucket 上传 + 临时 URL
  - services/aliyun_ocr_service.py         DashScope vanchin/deepseek-ocr
  - services/kyc_crypto.py                 AES + HMAC + mask
  - services/agent_review_ai.py            7 项风险审核

对 middleware/billing.py 零依赖: 申请过程本身不扣费。
"""

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import date
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from db.connection import get_connection

from api.refusal_log import refuse

logger = logging.getLogger("GEO-Partner-API")

router = APIRouter(prefix="/api/partner", tags=["代理计划"])


# ==================== 常量 ====================

CURRENT_AGREEMENT_VERSION = "v2.3"
MONTHLY_APPLY_LIMIT = 3                  # 30 天内被拒/撤回不超过 3 次
UPLOAD_MAX_BYTES = 10 * 1024 * 1024      # 身份证照片上限 10MB
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png"}
ID_CARD_PATTERN = re.compile(r"^\d{17}[0-9X]$")

# 协议文件目录（项目根/docs/条款/服务商申请协议_{version}.md · 历史回退 代理合作协议_{version}.md）
_AGREEMENT_DIR = Path(__file__).resolve().parent.parent / "docs" / "条款"

# v1.1-law-fix: 法务要求 —— 申请人必须满 18 岁
MIN_APPLICANT_AGE = 18

# v1.1-law-fix: 短信验证码 purpose 标识
SMS_PURPOSE = "partner_sign"


# ==================== Pydantic 模型 ====================

class PartnerApplyRequest(BaseModel):
    real_name: str = Field(..., min_length=2, max_length=30)
    id_card_no: str = Field(..., min_length=18, max_length=18)
    id_card_front_key: str = Field(..., min_length=1)
    id_card_back_key: str = Field(..., min_length=1)
    id_card_selfie_key: Optional[str] = None
    promotion_scenes: List[str] = Field(default_factory=list)
    expected_monthly_customers: str = Field(..., min_length=1)
    remark: Optional[str] = None
    agreement_version: str = Field(default=CURRENT_AGREEMENT_VERSION)
    signed_name: str = Field(..., min_length=2, max_length=30)
    # v1.1-law-fix: 短信验证码 (电子签名可靠性要件 + 法务要求)
    sms_code: str = Field(..., min_length=4, max_length=8, description="绑定手机收到的短信验证码")
    # v1.1-law-fix: 敏感信息单独同意 (《个保法》28 条要求)
    consent_sensitive_info: bool = Field(..., description="已单独勾选同意处理身份证等敏感个人信息")


class SendSmsRequest(BaseModel):
    """发送短信验证码请求 (兜底给用户填, 通常用账号绑定手机)"""
    # 允许前端显式传，未传则用账号绑定手机
    phone: Optional[str] = Field(default=None, min_length=11, max_length=11)


# ==================== Helpers ====================

def _get_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def _flag_enabled() -> bool:
    return os.getenv("PARTNER_APPLY_ENABLED", "false").lower() == "true"


def _require_flag_on():
    """写操作入口: flag off 时伪装成"功能不存在" (404, 与 nginx 对齐)"""
    if not _flag_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


def _load_agreement_text(version: str) -> str:
    """安全读取协议文件 (防路径注入 + 只允许 v\\d+(\\.\\d+)* 版本号)"""
    if not re.fullmatch(r"v[0-9]+(\.[0-9]+)*", version):
        raise HTTPException(status_code=404, detail="协议版本不存在")
    # 优先读「服务商申请协议」(v2.3+);回退「代理合作协议」(历史 v2.0 等)· 历史版本保留可查
    for _name in (f"服务商申请协议_{version}.md", f"代理合作协议_{version}.md"):
        path = _AGREEMENT_DIR / _name
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise HTTPException(status_code=404, detail="协议版本不存在")


def _client_ip(request: Request) -> str:
    # 优先取代理链末端 (nginx X-Real-IP / X-Forwarded-For 首段)
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    real = request.headers.get("X-Real-IP")
    if real:
        return real.strip()
    if request.client:
        return request.client.host or ""
    return ""


def _device_fingerprint(request: Request) -> str:
    return request.headers.get("X-Device-Fingerprint", "")[:200]


def _user_agent(request: Request) -> str:
    return request.headers.get("User-Agent", "")[:500]


def _extract_birthday_from_idcard(id_card_no: str) -> Optional[date]:
    """从身份证号提取出生日期（第 7-14 位 = YYYYMMDD）

    v1.1-law-fix: 未成年人申请拦截使用, 纯数字校验 + 日期合法性校验.
    """
    if not id_card_no or len(id_card_no) != 18:
        return None
    body = id_card_no[6:14]
    if not body.isdigit():
        return None
    try:
        return date(int(body[:4]), int(body[4:6]), int(body[6:8]))
    except ValueError:
        return None


def _is_adult(id_card_no: str) -> bool:
    """身份证号对应的自然人是否已满 18 岁"""
    bd = _extract_birthday_from_idcard(id_card_no)
    if bd is None:
        return False
    today = date.today()
    age = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
    return age >= MIN_APPLICANT_AGE


def _get_user_phone(user_id: int) -> Optional[str]:
    """从 users 表读取用户绑定手机号"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT phone FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        return (row or {}).get("phone") if row else None
    except Exception:
        return None
    finally:
        try: conn.close()
        except Exception: pass


# ==================== 0. Feature Flag 公开探测 ====================

@router.get("/flag")
async def get_partner_flag():
    """公开端点: 仅返回 PARTNER_APPLY_ENABLED 状态

    v1.1-law-fix 补丁: 前端 usePartnerFlag hook 专用.
    未登录 / token 失效也能访问, 避免因鉴权失败降级为 flag off 导致老代理入口泄露.
    端点已在 auth/middleware.py 白名单放行.
    """
    return {"enabled": _flag_enabled()}


# ==================== 1. 介绍页 ====================

@router.get("/about")
async def get_partner_about(request: Request):
    """介绍页数据 + flag 状态（flag off 时可读，返 enabled=false）"""
    _get_user(request)  # 需要登录

    if not _flag_enabled():
        return {
            "enabled": False,
            "message": "合作伙伴计划即将开放，敬请期待。",
        }

    return {
        "enabled": True,
        "current_agreement_version": CURRENT_AGREEMENT_VERSION,
        "benefits": {
            "l1_commission_rate": 0.18,
            "l2_commission_rate": 0.05,
            "bonus_inflation_rate": 0.20,
            "features": ["白标品牌", "客户 CRM", "推广物料库", "专属客服"],
        },
        "requirements": {
            "real_name_verify": True,
            "id_card_required": True,
            "agreement_required": True,
            "review_sla_days": 3,
            "monthly_apply_limit": MONTHLY_APPLY_LIMIT,
        },
    }


# ==================== 2. 申请状态 ====================

@router.get("/apply/status")
async def get_my_application_status(request: Request):
    """查当前用户的申请状态 + 月度次数"""
    _require_flag_on()
    user = _get_user(request)
    user_id = user["user_id"]

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, status, rejection_reason, ai_risk_flags, created_at, reviewed_at
            FROM agent_applications
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id,),
        )
        latest = cursor.fetchone()

        cursor.execute(
            """
            SELECT COUNT(*) AS cnt FROM agent_applications
            WHERE user_id = %s
              AND created_at >= NOW() - INTERVAL '30 days'
              AND status IN ('rejected', 'withdrawn')
            """,
            (user_id,),
        )
        recent_rejected = (cursor.fetchone() or {}).get("cnt", 0) or 0

        cursor.execute(
            "SELECT agent_level FROM user_wallets WHERE user_id = %s",
            (user_id,),
        )
        wallet_row = cursor.fetchone() or {}
        current_level = wallet_row.get("agent_level", 0) or 0

        in_review = latest and latest.get("status") in ("pending", "manual_review")
        can_apply_now = (
            current_level == 0
            and not in_review
            and recent_rejected < MONTHLY_APPLY_LIMIT
        )

        return {
            "current_agent_level": current_level,
            "latest_application": (
                {
                    "id": latest["id"],
                    "status": latest["status"],
                    "rejection_reason": latest.get("rejection_reason"),
                    "ai_risk_flags": latest.get("ai_risk_flags"),
                    "created_at": latest["created_at"].isoformat() if latest.get("created_at") else None,
                    "reviewed_at": latest["reviewed_at"].isoformat() if latest.get("reviewed_at") else None,
                }
                if latest
                else None
            ),
            "recent_rejected_count": recent_rejected,
            "monthly_apply_limit": MONTHLY_APPLY_LIMIT,
            "can_apply_now": can_apply_now,
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ==================== 3. 上传身份证 + OCR ====================

@router.post("/apply/upload-id-card")
async def upload_id_card(
    request: Request,
    side: str = Form(...),
    file: UploadFile = File(...),
):
    """上传身份证图片 → OSS 私有 bucket → OCR → 返回 key + OCR 结果

    前端拿到 key 和 OCR 结果后，展示给用户确认，然后在"提交申请"时把 key 传回来。
    """
    _require_flag_on()
    user = _get_user(request)
    user_id = user["user_id"]

    if side not in ("front", "back", "selfie"):
        raise HTTPException(400, "side 必须是 front / back / selfie")

    content_type = (file.content_type or "").lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(400, f"仅支持 JPEG/PNG 图片（收到 {content_type}）")

    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(400, "文件为空")
    if len(image_bytes) > UPLOAD_MAX_BYTES:
        raise HTTPException(413, f"文件过大（>{UPLOAD_MAX_BYTES // 1024 // 1024}MB）")

    ext = "png" if content_type == "image/png" else "jpg"

    from services.oss_service import build_id_card_key, upload_id_card_image, OSSConfigError, OSSUploadError
    try:
        oss_key = build_id_card_key(user_id=user_id, side=side, ext=ext)
        upload_id_card_image(oss_key=oss_key, image_bytes=image_bytes, content_type=content_type)
    except OSSConfigError as e:
        logger.error(f"[Partner] OSS 未配置: {e}")
        raise HTTPException(503, "OSS 服务未就绪，请联系管理员")
    except OSSUploadError as e:
        logger.error(f"[Partner] OSS 上传失败 user={user_id}: {e}")
        raise HTTPException(502, "上传失败，请稍后重试")

    # OCR（selfie 不做 OCR，直接返）
    ocr_result = None
    if side in ("front", "back"):
        from services.aliyun_ocr_service import recognize_id_card, OCRServiceError, OCRConfigError
        try:
            ocr_result = recognize_id_card(oss_key=oss_key, side=side)
        except OCRConfigError as e:
            logger.error(f"[Partner] OCR 未配置: {e}")
            raise HTTPException(503, "OCR 服务未就绪，请联系管理员")
        except OCRServiceError as e:
            # OCR 失败降级: 仍返 key, 让用户手填. 后端审核会看到置信度为空 → 自动进人工
            logger.warning(f"[Partner] OCR 失败 user={user_id} key={oss_key}: {e}")
            ocr_result = {"confidence": 0.0, "error": str(e)}

    return {
        "success": True,
        "oss_key": oss_key,
        "side": side,
        "ocr": ocr_result,
    }


# ==================== 3.5 发送短信验证码（电子签前置）====================

@router.post("/apply/send-sms")
async def send_apply_sms(req: SendSmsRequest, request: Request):
    """发送代理申请签署短信验证码（电子签名可靠性要件，法务要求）

    - 默认使用账号绑定手机号；前端如传 phone 则需与账号手机一致（防冒用）
    - purpose='partner_sign', TTL 300 秒, 错误 3 次后失效
    """
    _require_flag_on()
    user = _get_user(request)
    user_id = user["user_id"]

    bound_phone = _get_user_phone(user_id) or ""
    target_phone = (req.phone or bound_phone or "").strip()

    if not target_phone:
        raise HTTPException(400, "您的账号未绑定手机号，请先到「个人设置」绑定手机号")

    # 如前端显式传了 phone, 必须与账号绑定手机一致（防止借他人手机签约）
    if req.phone and bound_phone and req.phone != bound_phone:
        raise HTTPException(400, "仅允许使用账号绑定的手机号接收验证码")

    try:
        from auth.sms_service import send_sms_code
        result = send_sms_code(target_phone, purpose=SMS_PURPOSE)
    except Exception as e:
        logger.error(f"[Partner] 发送签约短信失败 user={user_id}: {e}")
        raise HTTPException(503, "短信服务暂时不可用，请稍后重试")

    if not result.get("success"):
        raise HTTPException(502, result.get("error") or "短信发送失败")

    # 返回脱敏手机号给前端展示
    masked = target_phone[:3] + "****" + target_phone[-4:]
    return {"success": True, "phone_mask": masked, "ttl_seconds": 300}


# ==================== 4. 提交申请 ====================

@router.post("/apply")
async def submit_application(req: PartnerApplyRequest, request: Request):
    """提交代理申请（5 段式事务：前置校验 → AI 审核 → 写申请 → 签协议 → 可能即时激活）"""
    _require_flag_on()
    user = _get_user(request)
    user_id = user["user_id"]

    # ========== 表单基础校验 ==========
    real_name = req.real_name.strip()
    id_card_no_plain = req.id_card_no.strip().upper()
    signed_name = req.signed_name.strip()

    if not ID_CARD_PATTERN.fullmatch(id_card_no_plain):
        raise HTTPException(400, "身份证号格式不合法（需 18 位，末位可为 X）")

    from services.aliyun_ocr_service import validate_id_card_checksum
    if not validate_id_card_checksum(id_card_no_plain):
        raise HTTPException(400, "身份证号校验码不通过（请核对是否录入正确）")

    if signed_name != real_name:
        raise HTTPException(400, "电子签名须与真实姓名完全一致")

    # v1.1-law-fix: 未成年人拦截（法律审核 3.20 项）
    if not _is_adult(id_card_no_plain):
        raise HTTPException(
            403,
            "本服务不面向未成年人。根据身份证信息，您尚未满 18 周岁，无法申请服务商合作。",
        )

    # v1.1-law-fix: 敏感个人信息单独同意（法律审核 3.18 项 + 《个保法》28 条）
    if not req.consent_sensitive_info:
        raise HTTPException(
            400,
            "需单独勾选同意处理身份证件等敏感个人信息后才能提交申请",
        )

    # v1.1-law-fix: 短信验证码校验（电子签名可靠性要件，法律审核 2.2 项）
    bound_phone = _get_user_phone(user_id) or ""
    if not bound_phone:
        raise HTTPException(400, "您的账号未绑定手机号，请先到「个人设置」绑定手机号")
    try:
        from auth.sms_service import verify_sms_code
        sms_result = verify_sms_code(bound_phone, req.sms_code.strip(), purpose=SMS_PURPOSE)
    except Exception as e:
        logger.error(f"[Partner] 短信校验异常 user={user_id}: {e}")
        raise HTTPException(503, "短信服务异常，请稍后重试")
    if not sms_result.get("success"):
        raise HTTPException(400, sms_result.get("error") or "短信验证码校验失败")

    # ========== 密钥与加密服务 ==========
    from services.kyc_crypto import (
        encrypt_id_card, hmac_id_card, mask_id_card,
        KYCCryptoConfigError, KYCCryptoError,
    )
    try:
        idno_encrypted = encrypt_id_card(id_card_no_plain)
        idno_hmac = hmac_id_card(id_card_no_plain)
        idno_mask = mask_id_card(id_card_no_plain)
    except KYCCryptoConfigError as e:
        logger.error(f"[Partner] KYC 密钥未配置: {e}")
        raise HTTPException(503, "KYC 加密服务未就绪，请联系管理员")
    except KYCCryptoError as e:
        logger.error(f"[Partner] KYC 加密失败: {e}")
        raise HTTPException(500, "加密失败")

    # 协议文本（签前哈希防篡改）
    agreement_text = _load_agreement_text(req.agreement_version)
    agreement_hash = hashlib.sha256(agreement_text.encode("utf-8")).hexdigest()

    # ========== DB 事务 ==========
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 前置 1: 已是代理
        cursor.execute(
            "SELECT agent_level FROM user_wallets WHERE user_id = %s",
            (user_id,),
        )
        wallet = cursor.fetchone()
        if wallet and (wallet.get("agent_level") or 0) >= 1:
            raise HTTPException(400, "您已是服务商，无需重复申请")

        # 前置 2: 有进行中的申请
        cursor.execute(
            """
            SELECT id FROM agent_applications
            WHERE user_id = %s AND status IN ('pending', 'manual_review')
            """,
            (user_id,),
        )
        if cursor.fetchone():
            raise HTTPException(400, "您已有审核中的申请，请耐心等待")

        # 前置 3: 月度 3 次
        cursor.execute(
            """
            SELECT COUNT(*) AS cnt FROM agent_applications
            WHERE user_id = %s
              AND created_at >= NOW() - INTERVAL '30 days'
              AND status IN ('rejected', 'withdrawn')
            """,
            (user_id,),
        )
        recent_rejected = (cursor.fetchone() or {}).get("cnt", 0) or 0
        if recent_rejected >= MONTHLY_APPLY_LIMIT:
            raise HTTPException(
                429,
                f"30 天内申请被拒或撤回已达 {MONTHLY_APPLY_LIMIT} 次，请在冷静期结束后再试",
            )

        # 前置 4: 身份证号唯一性（用 HMAC 查）
        cursor.execute(
            """
            SELECT user_id FROM agent_applications
            WHERE id_card_no_hmac = %s
              AND status = 'approved'
              AND user_id != %s
            """,
            (idno_hmac, user_id),
        )
        if cursor.fetchone():
            raise HTTPException(400, "该身份证号已绑定其他服务商账号")

        # ========== AI 审核 ==========
        # 从请求上下文里拿 OCR 结果（前端应该在 upload 步骤拿到后缓存，提交时...）
        # 实际方案：提交不带 OCR，后端再跑一次 OCR（幂等，消耗约 ¥0.04/次）
        # 这样协议简单，不需要前端回传大 OCR blob
        from services.aliyun_ocr_service import recognize_id_card, OCRServiceError
        try:
            ocr_front = recognize_id_card(oss_key=req.id_card_front_key, side="front")
        except OCRServiceError as e:
            logger.warning(f"[Partner] submit 阶段 OCR front 失败: {e}")
            ocr_front = {"confidence": 0.0}
        try:
            ocr_back = recognize_id_card(oss_key=req.id_card_back_key, side="back")
        except OCRServiceError as e:
            logger.warning(f"[Partner] submit 阶段 OCR back 失败: {e}")
            ocr_back = {"confidence": 0.0}

        from services.agent_review_ai import run_ai_review
        ai_result = run_ai_review(
            user_id=user_id,
            ocr_front=ocr_front,
            ocr_back=ocr_back,
            real_name=real_name,
            id_card_no_plain=id_card_no_plain,
            ip_address=_client_ip(request),
            device_fingerprint=_device_fingerprint(request),
            cursor=cursor,
        )

        initial_status = "auto_approved" if ai_result["verdict"] == "auto_approve" else "manual_review"

        # ========== 写申请记录 ==========
        ocr_consolidated = {"front": ocr_front, "back": ocr_back}
        cursor.execute(
            """
            INSERT INTO agent_applications (
                user_id, real_name,
                id_card_no_encrypted, id_card_no_hmac, id_card_no_mask,
                id_card_front_key, id_card_back_key, id_card_selfie_key,
                ocr_result, ocr_confidence,
                promotion_scenes, expected_monthly_customers, remark,
                status, ai_risk_flags,
                ip_address, device_fingerprint, user_agent
            )
            VALUES (
                %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s, %s
            )
            RETURNING id
            """,
            (
                user_id, real_name,
                idno_encrypted, idno_hmac, idno_mask,
                req.id_card_front_key, req.id_card_back_key, req.id_card_selfie_key,
                json.dumps(ocr_consolidated, ensure_ascii=False),
                min(
                    ocr_front.get("confidence") or 0.0,
                    ocr_back.get("confidence") or 0.0,
                ),
                json.dumps(req.promotion_scenes, ensure_ascii=False),
                req.expected_monthly_customers,
                req.remark,
                initial_status,
                json.dumps(ai_result["risk_flags"], ensure_ascii=False),
                _client_ip(request),
                _device_fingerprint(request),
                _user_agent(request),
            ),
        )
        app_id = cursor.fetchone()["id"]

        # ========== 协议签署 ==========
        cursor.execute(
            """
            INSERT INTO agent_agreements (
                user_id, application_id, version, signed_name,
                ip_address, device_fingerprint, user_agent, agreement_text_hash
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, version) DO UPDATE SET
                signed_at = NOW(),
                application_id = EXCLUDED.application_id,
                ip_address = EXCLUDED.ip_address,
                device_fingerprint = EXCLUDED.device_fingerprint,
                user_agent = EXCLUDED.user_agent,
                agreement_text_hash = EXCLUDED.agreement_text_hash
            """,
            (
                user_id, app_id, req.agreement_version, signed_name,
                _client_ip(request), _device_fingerprint(request), _user_agent(request),
                agreement_hash,
            ),
        )

        # ========== AI 自动通过 → 即时激活 ==========
        if initial_status == "auto_approved":
            cursor.execute(
                """
                UPDATE agent_applications
                SET status = 'approved',
                    reviewed_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (app_id,),
            )
            cursor.execute(
                """
                UPDATE user_wallets
                SET agent_level = 1,
                    agent_verified = TRUE,
                    updated_at = NOW()
                WHERE user_id = %s
                """,
                (user_id,),
            )
            logger.info(
                f"[Partner] 申请 {app_id} AI 自动通过, user={user_id} 激活代理身份"
            )

        conn.commit()

        if initial_status == "auto_approved":
            return {
                "success": True,
                "application_id": app_id,
                "status": "approved",
                "message": "审核通过，服务商身份已激活！",
            }

        return {
            "success": True,
            "application_id": app_id,
            "status": "manual_review",
            "message": "申请已提交，请留意审核结果通知",
            "sla_days": 3,
            "risk_flags_count": len(ai_result["risk_flags"]),
        }

    except HTTPException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.error(f"[Partner] 提交申请失败 user={user_id}: {type(e).__name__}: {e}")
        raise HTTPException(500, f"提交失败: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ==================== 5. 撤回申请 ====================

@router.post("/apply/{app_id}/withdraw")
async def withdraw_application(app_id: int, request: Request):
    """撤回申请（只能撤回 pending/manual_review 状态的自己的申请）"""
    _require_flag_on()
    user = _get_user(request)
    user_id = user["user_id"]

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE agent_applications
            SET status = 'withdrawn', updated_at = NOW()
            WHERE id = %s
              AND user_id = %s
              AND status IN ('pending', 'manual_review')
            RETURNING id
            """,
            (app_id, user_id),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(404, "申请不存在或已不可撤回")
        conn.commit()
        logger.info(f"[Partner] 用户 {user_id} 撤回申请 {app_id}")
        return {"success": True, "application_id": app_id, "status": "withdrawn"}
    except HTTPException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.error(f"[Partner] 撤回失败 app={app_id}: {e}")
        raise HTTPException(500, "撤回失败")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ==================== 6. 协议全文 ====================

@router.get("/agreement/{version}")
async def get_agreement_text(version: str, request: Request):
    """返回协议全文（Markdown）"""
    _get_user(request)  # 需要登录, 但 flag off 时也能读 (方便老代理查自己签过的 v1.0)
    text = _load_agreement_text(version)
    return {"version": version, "text": text}


# ==================== 7. 我签过的协议 ====================

@router.get("/agreements/mine")
async def list_my_agreements(request: Request):
    """列出当前用户签过的协议"""
    user = _get_user(request)  # flag off 时也能查自己的签署记录
    user_id = user["user_id"]

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, version, signed_name, signed_at, agreement_text_hash, application_id
            FROM agent_agreements
            WHERE user_id = %s
            ORDER BY signed_at DESC
            """,
            (user_id,),
        )
        rows = cursor.fetchall() or []
        return {
            "success": True,
            "agreements": [
                {
                    "id": r["id"],
                    "version": r["version"],
                    "signed_name": r["signed_name"],
                    "signed_at": r["signed_at"].isoformat() if r.get("signed_at") else None,
                    "agreement_text_hash": r["agreement_text_hash"],
                    "application_id": r.get("application_id"),
                }
                for r in rows
            ],
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ==================== 8. 签发临时 URL（查看自己的身份证）====================

@router.get("/id-card-view/{app_id}/{side}")
async def get_id_card_view_url(app_id: int, side: str, request: Request):
    """签发临时 URL 查看身份证 (15 分钟有效)

    权限: 只能看自己的申请记录 (admin 端点走 admin_api)
    """
    _require_flag_on()
    user = _get_user(request)
    user_id = user["user_id"]

    if side not in ("front", "back", "selfie"):
        raise HTTPException(400, "side 必须是 front / back / selfie")

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id_card_front_key, id_card_back_key, id_card_selfie_key
            FROM agent_applications
            WHERE id = %s AND user_id = %s
            """,
            (app_id, user_id),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(404, "申请不存在")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    key_map = {
        "front": row.get("id_card_front_key"),
        "back": row.get("id_card_back_key"),
        "selfie": row.get("id_card_selfie_key"),
    }
    oss_key = key_map.get(side)
    if not oss_key:
        raise HTTPException(404, f"该申请无 {side} 图片")

    from services.oss_service import generate_signed_url, OSSAccessError, OSSConfigError
    try:
        url = generate_signed_url(oss_key, expires_seconds=900)
    except OSSConfigError as e:
        raise HTTPException(503, f"OSS 服务未就绪: {e}")
    except OSSAccessError as e:
        raise HTTPException(500, f"签名失败: {e}")

    return {"success": True, "url": url, "expires_in": 900}


# ==================== 9. 代理权益入口 ====================

@router.get("/stats")
async def get_partner_stats(request: Request):
    """服务方个人统计 (白标 / CRM / 收益概览入口)
    v1_3 (CTO-15.1 2026-04-19 Round 2 P1 修复):
    - 原 500 根因: SELECT 字段可能在某些环境不存在 (agent_verified / commission_points) → psycopg2 直接抛 → FastAPI 500
    - 改用 defensive 两段 SELECT: 先查基础字段，再 try 扩展字段，字段不存在时 fallback
    """
    user = _get_user(request)
    user_id = user["user_id"]

    wallet: dict = {}
    pending_cnt = 0
    conn = get_connection()
    try:
        cursor = conn.cursor()
        # 基础字段（都存在）
        cursor.execute(
            "SELECT agent_level, paid_points, bonus_points FROM user_wallets WHERE user_id = %s",
            (user_id,),
        )
        row = cursor.fetchone()
        if row:
            wallet.update(dict(row))

        if (wallet.get("agent_level") or 0) < 1:
            raise HTTPException(403, "仅服务方可访问")

        # 扩展字段 defensive(某些老环境缺列)
        try:
            cursor.execute(
                "SELECT agent_verified, commission_points FROM user_wallets WHERE user_id = %s",
                (user_id,),
            )
            ext = cursor.fetchone()
            if ext:
                wallet.update(dict(ext))
        except Exception as e:
            logger.warning(f"partner/stats 扩展字段查询失败(非阻断): {e}")

        # 2026-04-26 P1-1 修复: 原查 commission_records.commission_user_id 列名不存在(应为 referrer_id)
        # 且 v3.2 起服务收益已迁到 pending_commissions,commission_records 是 v3.1 legacy
        # 改查真表 pending_commissions WHERE user_id=... AND status='pending'
        try:
            cursor.execute(
                "SELECT COUNT(*) AS pending_cnt FROM pending_commissions "
                "WHERE user_id = %s AND status = 'pending'",
                (user_id,),
            )
            pending_row = cursor.fetchone()
            pending_cnt = (dict(pending_row).get("pending_cnt") if pending_row else 0) or 0
        except Exception as e:
            logger.warning(f"partner/stats pending_commissions 查询失败(非阻断): {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return {
        "success": True,
        "agent_level": wallet.get("agent_level"),
        "agent_verified": wallet.get("agent_verified"),
        "commission_points": wallet.get("commission_points") or 0,
        "pending_commissions": pending_cnt,
        "entries": {
            "whitelabel": "/api/referral/whitelabel",
            "commissions": "/api/referral/commissions",
            "agreements": "/api/partner/agreements/mine",
        },
    }


# ==================== 10. 主动退代理 ====================

@router.post("/downgrade")
async def downgrade_self(request: Request):
    """服务商主动退出服务商身份 → 降为普通用户。

    🔴 这条路以前是一句裸 `UPDATE user_wallets SET agent_level=0`：没有依赖闸、
    没有乐观锁、没有权限版本递增、没有审计留痕。它能把账号打成
    「agent_level=0 但仍挂着现役渠道关系」的死态 —— 那之后
    `change_channel_relationship` 会以 CHANNEL_SUBJECT_MUST_BE_SERVICE_PROVIDER
    永久拒绝，关系再也终结不掉。现在统一走治理原语，与 admin 侧同一道闸。
    """
    _require_flag_on()
    user = _get_user(request)
    user_id = int(user["user_id"])

    from services.admin_user_governance import (
        GovernanceNotFound,
        GovernanceValidationError,
        GovernanceVersionConflict,
        change_business_identity,
        get_admin_user_detail,
    )

    try:
        detail = get_admin_user_detail(user_id)
    except GovernanceNotFound:
        raise refuse(logger, status=404, code="USER_NOT_FOUND",
                     detail="用户不存在", context={"user": user_id})
    if detail["overview"]["business_identity"] != "service_provider":
        raise refuse(logger, status=400, code="NOT_A_SERVICE_PROVIDER",
                     detail="当前不是服务商，无需退出", context={"user": user_id})

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT agent_level FROM user_wallets WHERE user_id=%s", (user_id,))
        previous_level = int((cursor.fetchone() or {}).get("agent_level") or 0)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    request_id = (
        request.headers.get("X-Request-ID") or f"partner-downgrade-{uuid.uuid4()}"
    )[:180]
    try:
        result = change_business_identity(
            user_id,
            "ordinary_user",
            expected_version=int(detail["overview"]["versions"]["business_identity"]),
            reason="服务商主动退出服务商身份",
            operator_user_id=user_id,
            operator_username=user.get("username"),
            request_id=request_id,
            ip_address=_client_ip(request),
        )
    except GovernanceValidationError as exc:
        detail_payload = {"code": exc.code, "message": str(exc)}
        detail_payload.update(getattr(exc, "details", None) or {})
        # 🔴 detail 原样传:这里是 dict,下面那条是**字符串** ——
        #    两种形状都是用户可见契约,本单只多写一行日志,不动形状。
        raise refuse(logger, status=409, code=exc.code, detail=detail_payload,
                     context={"user": user_id, "message": str(exc)}) from exc
    except GovernanceVersionConflict as exc:
        raise refuse(logger, status=409, code="GOVERNANCE_VERSION_CONFLICT",
                     detail="账号身份正在被其他操作变更，请刷新后重试",
                     context={"user": user_id}) from exc

    logger.info(f"[Partner] 用户 {user_id} 主动退出服务商 (agent_level {previous_level} → 0)")
    return {
        "success": True,
        "previous_level": previous_level,
        "current_level": 0,
        "version": result["version"],
        "message": "已退出服务商身份。已产生的服务收益结算不受影响。",
    }
