"""
RBAC 认证 API
提供登录、获取当前用户、刷新权限、修改密码等接口
"""

from fastapi import APIRouter, Request, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, List

from auth.jwt_utils import create_jwt, decode_jwt, refresh_jwt
from auth.rate_limiter import check_rate_limit, record_failed_attempt, clear_attempts
from db.auth_db import (
    get_user, update_user, update_user_password, get_user_permissions,
    get_user_by_username, get_user_permission_version, update_last_login, verify_password
)

import json
import logging
from auth.user_ctx import current_user_id
logger = logging.getLogger("GEO-Auth-API")

router = APIRouter(prefix="/api/auth", tags=["认证"])


# ========== 请求模型 ==========

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    password: str = Field(..., min_length=1, max_length=128)


class RegisterRequest(BaseModel):
    phone: str = Field(..., min_length=11, max_length=11, description="手机号作为用户名")
    password: str = Field(..., min_length=6, max_length=128)
    display_name: Optional[str] = Field(None, max_length=50)
    referral_code: Optional[str] = Field(None, max_length=20)
    sms_code: Optional[str] = Field(None, max_length=6, description="短信验证码（启用短信注册时必填）")
    terms_accepted: bool = False
    privacy_accepted: bool = False
    terms_version: Optional[str] = Field(None, max_length=20)
    privacy_version: Optional[str] = Field(None, max_length=20)


class UserTermsAcceptanceRequest(BaseModel):
    terms_accepted: bool = False
    terms_version: Optional[str] = Field(None, max_length=20)
    surface: str = Field(default="customer-recharge", max_length=100)


def validate_registration_agreements(req) -> None:
    from services.legal_agreements import PRIVACY_VERSION, USER_TERMS_VERSION

    if not req.terms_accepted or req.terms_version != USER_TERMS_VERSION:
        raise HTTPException(status_code=400, detail="请阅读并同意当前版本的《用户服务协议》")
    if not req.privacy_accepted or req.privacy_version != PRIVACY_VERSION:
        raise HTTPException(status_code=400, detail="请阅读并同意当前版本的《隐私政策》")


class SMSSendRequest(BaseModel):
    phone: str = Field(..., min_length=11, max_length=11)
    captcha_id: Optional[str] = Field(None)
    captcha_answer: Optional[str] = Field(None)
    purpose: str = Field("register", description="register/login/reset_password")


class SMSLoginRequest(BaseModel):
    phone: str = Field(..., min_length=11, max_length=11)
    sms_code: str = Field(..., min_length=6, max_length=6)


class AgreementSupplementAcceptanceRequest(BaseModel):
    agreement_session_token: str = Field(..., min_length=40, max_length=8000)
    terms_accepted: bool = False
    privacy_accepted: bool = False
    terms_version: str = Field(..., min_length=1, max_length=20)
    privacy_version: str = Field(..., min_length=1, max_length=20)


class AgreementSupplementSessionRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6, max_length=128)


# ========== 工具函数 ==========

def _get_current_user(request: Request) -> dict:
    """从 request.state 获取当前用户（中间件已注入）"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def _get_client_ip(request: Request) -> str:
    """Use only proxy-overwritten or transport-derived address evidence."""
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"


def _require_registration_agreements(*, user_id: int, auth_method: str) -> None:
    """Gate full-session delivery after credentials have been verified."""
    from db.connection import get_db
    from services.legal_agreements import has_current_registration_agreements
    from services.registration_agreement_supplement import create_agreement_session

    with get_db() as conn:
        complete = has_current_registration_agreements(conn.cursor(), user_id=int(user_id))
    if complete:
        return

    # 最小可观测(工单 §4.1):先留痕再拦。best-effort —— 记录器内部吞掉一切异常,
    # 绝不让"看不见"升级成"登不进"。
    from services.registration_agreement_gate_metrics import record_gate_trigger

    record_gate_trigger(user_id=int(user_id), auth_method=auth_method)

    session = create_agreement_session(user_id=int(user_id), auth_method=auth_method)
    raise HTTPException(
        status_code=428,
        headers={"Cache-Control": "no-store"},
        detail={
            "code": "REGISTRATION_AGREEMENTS_REQUIRED",
            "message": "为继续使用，请确认当前《用户服务协议》和《隐私政策》",
            "requires_agreement": True,
            **session,
        },
    )


def _resolve_registration_referral_code(req: RegisterRequest, request: Request) -> Optional[str]:
    """Resolve poster attribution server-side without exposing the referral code in URLs."""

    explicit = (req.referral_code or "").strip()
    if explicit:
        return explicit
    attribution = (request.cookies.get("omnirank_invite_attribution") or "").strip().lower()
    if not attribution or len(attribution) > 32 or not attribution.isalnum():
        return None
    try:
        import json
        from db.connection import get_connection

        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT metadata FROM short_links
                WHERE code = %s AND link_type IN ('register', 'register_social')
                """,
                (attribution,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        metadata = row.get("metadata") if isinstance(row, dict) and row else None
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        code = (metadata or {}).get("referral_code") if isinstance(metadata, dict) else None
        return str(code).strip()[:20] if code else None
    except Exception as exc:
        logger.warning("registration attribution lookup failed type=%s", type(exc).__name__)
        return None


def _read_attribution_short_code(request: Request) -> Optional[str]:
    """从归因 cookie(HttpOnly 优先,展示 cookie 兜底)读不透明短链 code。"""
    for cookie_name in ("omnirank_invite_attribution", "omnirank_invite_display"):
        value = (request.cookies.get(cookie_name) or "").strip().lower()
        if value and len(value) <= 32 and value.isalnum():
            return value
    return None


@router.get("/register/attribution")
async def get_register_attribution(request: Request):
    """注册页归因回显:返回邀请人显示名与识别状态。

    [WO_REFERRAL_CHAIN §2.1 2026-08-05]
    · 隐私口径不破:**绝不返回推荐码原文**,只返回显示名 + recognized 布尔;
    · 路径必须在 /api/auth/register 之下(HttpOnly 归因 cookie 的 Path 才会随请求带上);
    · 识别不到 / 查库异常 → recognized=false,前端走手填推荐码路径,本端点绝不 4xx/5xx
      阻断注册流程。
    """
    payload = {"recognized": False, "inviter_display_name": None}
    short_code = _read_attribution_short_code(request)
    if not short_code:
        return {"success": True, "data": payload}
    try:
        from db.connection import get_connection

        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT u.display_name, u.username
                FROM short_links sl
                JOIN referral_codes rc
                  ON rc.code = (sl.metadata ->> 'referral_code')
                JOIN users u ON u.id = rc.user_id
                WHERE sl.code = %s
                  AND sl.link_type IN ('register', 'register_social')
                """,
                (short_code,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        if row:
            name = (row.get("display_name") or "").strip()
            if not name:
                # display_name 缺失时不裸露 username(手机号),给中性称呼
                name = "你的邀请人"
            payload = {"recognized": True, "inviter_display_name": name}
    except Exception as exc:
        logger.warning("register attribution display lookup failed type=%s", type(exc).__name__)
    return {"success": True, "data": payload}


@router.post("/legal-agreements/accept")
async def accept_legal_agreements(req: UserTermsAcceptanceRequest, request: Request):
    """Persist a single-use, short-lived purchase acceptance with IP/UA evidence."""
    user = _get_current_user(request)
    user_id = int(user.get("user_id") or current_user_id(user))
    from services.legal_agreements import (
        USER_TERMS_CONTENT_HASH,
        USER_TERMS_VERSION,
        record_purchase_acceptance,
    )
    if not req.terms_accepted or req.terms_version != USER_TERMS_VERSION:
        raise HTTPException(status_code=400, detail="请阅读并同意当前版本的《用户服务协议》")
    from db.connection import get_db
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO agreement_signatures
                (user_id,agreement_type,agreement_version,content_hash,
                 ip_address,user_agent,evidence_jsonb)
            VALUES (%s,'user_terms',%s,%s,%s,%s,%s::jsonb)
            ON CONFLICT (user_id,agreement_type,agreement_version) DO UPDATE SET
                content_hash=EXCLUDED.content_hash,
                signed_at=NOW(),
                ip_address=EXCLUDED.ip_address,
                user_agent=EXCLUDED.user_agent,
                evidence_jsonb=EXCLUDED.evidence_jsonb
        """, (
            user_id, USER_TERMS_VERSION, USER_TERMS_CONTENT_HASH,
            _get_client_ip(request), request.headers.get("User-Agent", "")[:2000],
            json.dumps({"surface": req.surface, "explicit_acceptance": True}, ensure_ascii=False),
        ))
        try:
            acceptance = record_purchase_acceptance(
                cur, user_id=user_id, ip_address=_get_client_ip(request),
                user_agent=request.headers.get("User-Agent", ""), surface=req.surface,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        from datetime import datetime, timezone
        from services.notification_events import NotificationEventType, RecipientKind
        from services.notification_outbox import enqueue_notification_event

        enqueue_notification_event(
            cur,
            event_type=NotificationEventType.AGREEMENT_ACCEPTED,
            business_id=f"user:{user_id}:terms:{USER_TERMS_VERSION}",
            terminal_state="accepted",
            recipient_user_id=user_id,
            recipient_kind=RecipientKind.USER,
            facts={
                "business_no": f"TERMS-{USER_TERMS_VERSION}",
                "status": "已确认",
                "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "summary": "当前版本的用户服务协议已确认。",
            },
        )
        conn.commit()
    return {"success": True, "acceptance": acceptance}


@router.post("/registration-agreements/session")
async def reissue_registration_agreement_session(
    req: AgreementSupplementSessionRequest, request: Request
):
    """补签页自助重新取回协议确认凭证。**不签发任何业务 JWT。**

    为什么需要它(工单 §1.3.2 fail-safe):补签 session 只能由登录接口产出。一旦登录页
    没能把它交到补签页手里(payload 形态漂移 / sessionStorage 写失败),用户就再也拿不到
    第二份 —— "拦住 + 唯一出口坏了" 就等于永久锁死。这个端点把"再取一份凭证"变成
    用户自己能完成的动作。

    🔴 一条门禁都没有放宽:
      - 与 `/login` **同一套**凭证校验 + **同一套**限流,凭证错就是 401;
      - 协议已齐全的账号**不发凭证**,直接告诉它走正常登录(不泄漏账号是否存在之外的信息);
      - 返回体里没有 token —— 拿到凭证也拿不到任何业务权限,只能去 `/accept` 补签。
    """
    from db.connection import get_db
    from services.legal_agreements import has_current_registration_agreements
    from services.registration_agreement_supplement import create_agreement_session

    allowed, msg = check_rate_limit(req.username)
    if not allowed:
        logger.warning(f"补签凭证限流: {req.username} from {_get_client_ip(request)}")
        raise HTTPException(status_code=429, detail=msg)

    user_raw = get_user_by_username(req.username)
    if (
        not user_raw
        or not bool(user_raw.get("is_active", True))
        or not verify_password(req.password, user_raw.get("password_hash") or "")
    ):
        record_failed_attempt(req.username)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    clear_attempts(req.username)

    user_id = int(user_raw["id"])
    with get_db() as conn:
        complete = has_current_registration_agreements(conn.cursor(), user_id=user_id)
    if complete:
        # 不是错误,是"你不需要补签了"。给明确下一步,不给死路。
        return {
            "success": False,
            "code": "AGREEMENTS_ALREADY_CURRENT",
            "message": "该账号的协议已经是最新版本，直接登录即可。",
        }

    session = create_agreement_session(user_id=user_id, auth_method="password")
    logger.info(f"补签凭证重新签发: user_id={user_id} from {_get_client_ip(request)}")
    return {"success": True, **session}


@router.post("/registration-agreements/accept")
async def accept_registration_agreements(req: AgreementSupplementAcceptanceRequest, request: Request):
    """Complete legacy registration evidence, then and only then issue a full JWT."""
    from auth.jwt_utils import create_jwt
    from db.connection import get_db
    from services.legal_agreements import (
        PRIVACY_VERSION,
        USER_TERMS_VERSION,
        record_registration_agreement_acceptance,
    )
    from services.registration_agreement_supplement import (
        AgreementSessionError,
        decode_agreement_session,
    )

    if not req.terms_accepted or not req.privacy_accepted:
        raise HTTPException(status_code=400, detail="请分别阅读并同意《用户服务协议》和《隐私政策》")
    if req.terms_version != USER_TERMS_VERSION or req.privacy_version != PRIVACY_VERSION:
        raise HTTPException(status_code=409, detail="协议版本已更新，请重新验证身份并阅读最新协议")
    try:
        session = decode_agreement_session(req.agreement_session_token)
    except AgreementSessionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    user_id = int(session["user_id"])
    auth_method = str(session["auth_method"])
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT id,is_active FROM users WHERE id=%s FOR UPDATE", (user_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=401, detail="账号不存在，请重新登录")
            is_active = row.get("is_active") if isinstance(row, dict) else row[1]
            if not bool(is_active):
                raise HTTPException(status_code=403, detail="账号已停用，请联系管理员")

            record_registration_agreement_acceptance(
                cur,
                user_id=user_id,
                ip_address=_get_client_ip(request),
                user_agent=request.headers.get("User-Agent", ""),
                auth_method=auth_method,
                agreement_session_jti=str(session["jti"]),
            )
            if auth_method == "sms":
                cur.execute("UPDATE users SET phone_verified=TRUE WHERE id=%s", (user_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    token = create_jwt(user_id)
    if not token:
        raise HTTPException(status_code=500, detail="协议已确认，但登录会话签发失败，请重新登录")
    user = get_user(user_id) or {}
    update_last_login(user_id)
    return {
        "success": True,
        "token": token,
        "must_change_password": int(user.get("must_change_password") or 0) == 1,
    }


# [GEO-R1-CAN-022] 头像上传上限 + 真实类型嗅探,防超大 body/存储型 XSS(.html/.svg/.php)
AVATAR_MAX_BYTES = 5 * 1024 * 1024  # 5MB


def _sniff_image_ext(content: bytes) -> Optional[str]:
    """依据文件头 magic bytes 判定真实图片类型,返回安全后缀(白名单);非图片返回 None。
    忽略攻击者可控的 filename 后缀,避免 .html/.svg/.php 落盘造成存储型 XSS/任意文件。"""
    if not content or len(content) < 12:
        return None
    if content[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    return None


# ========== API 端点 ==========

@router.post("/register")
async def register(req: RegisterRequest, request: Request):
    """
    C端用户自助注册

    流程：
    1. 手机号作为 username 创建用户（must_change_password=0，不强制改密）
    2. 自动创建个人品牌（C端 RBAC 需要，用户无感）
    3. 绑定 user_clients（品牌归属）
    4. 发放 3888 bonus 体验包积分（create_user 内部已处理）
    5. 绑定推荐关系（如有推荐码）
    6. 签发 JWT Token（注册即登录，不跳回登录页）
    """
    from db.auth_db import create_user, get_user_by_username
    from db.diagnosis_db import get_or_create_brand
    from services.legal_agreements import registration_agreements

    phone = req.phone.strip()
    referral_code_to_bind = _resolve_registration_referral_code(req, request)

    validate_registration_agreements(req)

    # 校验手机号格式
    import re
    if not re.match(r'^1\d{10}$', phone):
        raise HTTPException(status_code=400, detail="手机号格式不正确")

    # 短信验证码校验（如果提供了sms_code）
    # [GEO-R1-CAN-117] 启用短信注册(SIGNUP_REQUIRE_SMS=true)时,缺码即 fail-closed 拒绝,
    #   防止未持有该手机号者用他人号建账号(占位/薅注册赠送积分/做监测越权基数)。
    #   默认 false 保持向后兼容(邀请制门禁已是主要防线);开关打开则强制持有证明。
    import os as _os_sms
    _require_sms = _os_sms.getenv("SIGNUP_REQUIRE_SMS", "false").strip().lower() == "true"
    if _require_sms and not req.sms_code:
        raise HTTPException(status_code=400, detail="注册需要短信验证码,请先获取验证码")
    if req.sms_code:
        from auth.sms_service import verify_sms_code
        sms_result = verify_sms_code(phone, req.sms_code, "register")
        if not sms_result["success"]:
            raise HTTPException(status_code=400, detail=sms_result["error"])

    # 检查手机号是否已注册
    existing = get_user_by_username(phone)
    if existing:
        raise HTTPException(status_code=409, detail="该手机号已注册，请直接登录")

    # ========== 纯邀请制准入校验（老板 2026-06-05 拍板）==========
    # "必须使用有效推荐码才能注册" · 在建用户/发积分之前拦截无效码,避免无效注册产生脏数据
    # 兼容两套码体系：invite_codes(V3.3.1 · verify_invite_code) + referral_codes(老体系 · 存在即有效)
    # Promotion uses opaque codes only; public user identifiers are never accepted.
    # kill-switch：SIGNUP_REQUIRE_REFERRAL=false 紧急回滚到自助注册(默认 true)
    import os as _os
    _require_referral = _os.getenv("SIGNUP_REQUIRE_REFERRAL", "true").strip().lower() == "true"
    if _require_referral:
        _signup_code = (referral_code_to_bind or "").strip()
        if not _signup_code:
            raise HTTPException(status_code=400, detail="注册需要有效的推荐码,请向邀请你的人索取后再注册")
        # 有效性校验：invite_codes 优先,查无再 fallback 老 referral_codes
        _code_ok = False
        _invite_hard_fail = False  # invite_codes 里存在但失效(过期/撤销)→ 明确拒绝,不 fallback(防绕过风控)
        try:
            from services.identity_service import (
                verify_invite_code,
                InviteCodeNotFoundError,
                InviteCodeError,
            )
            try:
                verify_invite_code(_signup_code)  # 存在 + 激活 + 未过期 → 有效
                _code_ok = True
            except InviteCodeNotFoundError:
                pass  # 不在 invite_codes · 下面 fallback 查老 referral_codes
            except InviteCodeError:
                _invite_hard_fail = True  # 过期/撤销等明确失效
        except Exception as _e:
            logger.warning(f"[注册-邀请制] verify_invite_code 异常,降级查 referral_codes: {_e}")
        if not _code_ok and not _invite_hard_fail:
            try:
                from db.connection import get_connection as _gc2
                _c2 = _gc2()
                _cur2 = _c2.cursor()
                # [P0 2026-08-05] 归一化比对:大小写 + 易混字符(O/0 · I/L/1)。
                #   生产事故:码 OR-HTLO7LVQ(字母O)被转录成 OR-HTL07LVQ(数字0)→ 注册当场被拒。
                #   折叠前已在生产库证过零碰撞(49 码折叠后仍 49 个唯一值)。
                from services.referral_code_normalize import fold_sql, normalize_code
                _cur2.execute(
                    f"SELECT user_id FROM referral_codes WHERE {fold_sql('code')} = {fold_sql('%s')}",
                    (normalize_code(_signup_code),))
                if _cur2.fetchone():
                    _code_ok = True
                _c2.close()
            except Exception as _e:
                logger.warning(f"[注册-邀请制] referral_codes 校验异常: {_e}")
        if not _code_ok:
            raise HTTPException(status_code=400, detail="推荐码无效或已失效,请确认后重试")

    display_name = req.display_name or f"用户{phone[-4:]}"

    # 1. 创建用户（内部自动发放 3888 体验包积分）
    user_id = create_user(
        username=phone,
        password=req.password,
        display_name=display_name,
        must_change_password=0,  # C端注册不强制改密
        agreement_acceptances=list(registration_agreements()),
        agreement_ip=_get_client_ip(request),
        agreement_ua=request.headers.get("User-Agent", "")[:2000],
    )
    if not user_id:
        raise HTTPException(status_code=500, detail="注册失败，请稍后重试")

    # 1b. 设置手机号和验证状态
    try:
        update_user(user_id, phone=phone, phone_verified=bool(req.sms_code))
    except Exception:
        pass  # 不影响注册

    # 1c. 初始化钱包 + 发放 3888 体验包积分(注册送)
    # Codex 四审 P1-2:V3.3.1 启用 + DISABLE_INSTANT_TRIAL_BONUS=true 时 skip 即时发
    # 决策书 §5.5:V3.3.1 改成分阶段释放(brand 资料 1000 / 首次诊断 1500 / 邀请验证 1388)
    # 分阶段实施前 · skip 即时发防黑产套利;分阶段实施后 admin 可改 flag=false 恢复
    try:
        from db.wallet_db import get_or_create_wallet, grant_trial_bonus
        get_or_create_wallet(user_id)

        skip_instant_bonus = False
        try:
            from config.v3_3_1_flags import is_instant_trial_bonus_disabled
            skip_instant_bonus = is_instant_trial_bonus_disabled()
        except Exception:
            pass

        if skip_instant_bonus:
            logger.info(
                f"[注册-V3.3.1] 用户 {user_id} 钱包已初始化 · "
                f"跳过即时 3888 发放(V3_3_1_DISABLE_INSTANT_TRIAL_BONUS=true · 等分阶段释放)"
            )
        else:
            grant_trial_bonus(user_id, amount=3888)
            logger.info(f"[注册] 用户 {user_id} 钱包已初始化 + 体验包 3888 积分已发放")
    except Exception as e:
        logger.warning(f"[注册] 体验包发放失败（不影响注册）: {e}")

    # 2. 自动创建个人品牌（C端用户无感，RBAC 权限体系需要）
    # 品牌名加 user_id 保证唯一，避免不同用户手机尾号相同导致品牌碰撞
    try:
        from db.connection import get_db
        with get_db() as conn:
            cur = conn.cursor()
            # [CTO-15.3 2026-04-20] 修 bug: 原硬塞 industry="内容创作" + category="自媒体"
            #   导致新用户在 C 端 Drawer 看到"营销资料完善度 60%"虚高(name+industry+底分)
            #   → 老板反馈"啥也没操作为什么 60%"
            #   修: industry/category 留空, 让用户通过 ensure_brand_fields 对话式补齐(commit 25)或在品牌详情页自己填
            #   新用户现在完善度 = name(20) + 底分(10) = 30% 合理"刚开始"状态
            # A.2 CTO-15.18 · 名字含"测试|test|_demo|_test|验收"自动 is_test=true
            # 用户注册时 display_name 一般是真用户 · 但运营 / QA 测试账号也走这条 → auto-detect 兜底
            from utils.is_test_brand import detect_is_test_for_new_brand
            # [P0-1 · 2026-07-26] 注册自建品牌名也要干净：display_name 是自由文本，
            #   带换行/标签串会让后续品牌识别恒不命中（brand 278 实证）。
            #   注册是核心流程，这里**只清洗不拒绝**（拒绝会把注册堵死 = 手册 §1.6
            #   事故 #7）；清洗后仍不合法就退回中性名，用户后面可以自己改。
            from utils.brand_name_hygiene import is_malformed_brand_name, normalize_brand_name

            _display_for_brand = normalize_brand_name(display_name)
            _brand_name = f"{_display_for_brand}的创作空间_{user_id}"
            if not _display_for_brand or is_malformed_brand_name(_brand_name):
                _brand_name = f"我的创作空间_{user_id}"
            _is_test = detect_is_test_for_new_brand(_brand_name)
            cur.execute("""
                INSERT INTO brands (name, industry, industry_category, brand_type, owner_user_id, status, is_test)
                VALUES (%s, %s, %s, 'self', %s, 'active', %s)
                RETURNING id
            """, (_brand_name, "", "", user_id, _is_test))
            brand_id = cur.fetchone()["id"]
            cur.execute("UPDATE brands SET brand_code = %s WHERE id = %s AND brand_code IS NULL",
                        (f"BRD-{brand_id:04d}", brand_id))

        # 3. 绑定用户到品牌
        from db.auth_db import set_user_clients, set_user_roles
        set_user_clients(user_id, [brand_id])
        logger.info(f"[注册] 用户 {user_id} 绑定品牌 {brand_id} (brand_type=self, owner={user_id})")

        # 注意：不在注册时创建 profile（空壳 profile 会导致欢迎页被跳过）
        # profile 在欢迎页 /api/my-brand/init 时创建，届时有行业信息

        # 4. 仅分配普通用户兼容角色。social_ops 的历史内部名保留，但其权限模板已覆盖
        # 现役普通用户能力；deprecated geo_writer 不再新分配，存量用户仍按旧 RBAC 读取。
        try:
            from db.connection import get_connection
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM roles WHERE name = 'social_ops'")
            role_rows = cursor.fetchall()
            conn.close()
            if role_rows:
                role_ids = [r["id"] for r in role_rows]
                set_user_roles(user_id, role_ids)
                logger.info(f"[注册] 用户 {user_id} 已分配普通用户兼容角色(social_ops)")
            else:
                logger.warning("[注册] 未找到 social_ops 角色 · 新用户可能无法访问功能")
        except Exception as e:
            logger.warning(f"[注册] 分配角色失败(不影响注册): {e}")
    except Exception as e:
        logger.warning(f"[注册] 创建个人品牌失败（不影响注册）: {e}")

    # 4. 记录注册邀请归属（不推断商业服务关系）
    if referral_code_to_bind:
        # V3.3.1(Codex 三审 P0-1 + 四审 P1-1):总开关 ON 时优先走 invite_codes 新流程 ·
        # 升级 agent_level + 写 referral_links + 标记 invite_codes.used_by_user_id
        #
        # Fallback 范围 严格限缩(Codex 四审 P1-1):
        #   - 仅 InviteCodeNotFoundError(查无此码 · 可能是 V3.1 老码)→ fallback 老 bind_referral
        #   - 过期 / 撤销 / 已用 / L1 配额超 → 明确失败 · 不 fallback · 不绑定推荐(防绕过 V3 风控)
        v3_handled = False  # 走过 V3.3.1 处理(成功或明确失败)· 不再 fallback
        v3_consumed = False  # 真实成功消费(不需要 fallback)
        try:
            from config.v3_3_1_flags import is_v3_3_1_enabled
            if is_v3_3_1_enabled():
                from services.identity_service import (
                    consume_invite_code,
                    InviteCodeNotFoundError,
                    InviteCodeExpiredError,
                    InviteCodeRevokedError,
                    InviteCodeAlreadyUsedError,
                    InviteCodeQuotaExceededError,
                    InviteCodeError,
                )
                try:
                    result = consume_invite_code(referral_code_to_bind, user_id)
                    logger.info(
                        "[注册-V3.3.1] 邀请归因已按注册规则处理 · "
                        "binding_action=%s",
                        result.get("commercial_binding_action", "unknown"),
                    )
                    v3_consumed = True
                    v3_handled = True
                except InviteCodeNotFoundError as exc:
                    # 查无此码 · 可能是老 V3.1 referral_codes 表里的码 · 允许 fallback 兼容
                    logger.warning(
                        f"[注册-V3.3.1] invite_codes 查无此码(fallback 老 bind_referral): {exc}"
                    )
                    # v3_handled 保持 False → 走 fallback
                except (InviteCodeExpiredError, InviteCodeRevokedError,
                        InviteCodeAlreadyUsedError, InviteCodeQuotaExceededError) as exc:
                    # V3 码无效但存在(过期/撤销/已用/配额超)
                    # 明确失败 · 不 fallback · 不绑定任何推荐(防绕过 V3 风控)
                    logger.warning(
                        f"[注册-V3.3.1] 邀请码 {referral_code_to_bind} 无效({type(exc).__name__})· "
                        f"不 fallback · 用户无推荐绑定: {exc}"
                    )
                    v3_handled = True  # 明确失败 · 不走 fallback
                except InviteCodeError as exc:
                    # 其他 V3 错误 · 保守按"明确失败"处理 · 不 fallback
                    logger.warning(
                        f"[注册-V3.3.1] consume_invite_code 其他错误 · 不 fallback: {exc}"
                    )
                    v3_handled = True
        except Exception as e:
            # 已通过 V3 码校验后，消费/归因异常不得降级成假成功。
            logger.error(
                "[注册-V3.3.1] 邀请注册事务失败 type=%s · 拒绝静默降级",
                type(e).__name__,
            )
            raise HTTPException(
                status_code=503,
                detail="注册服务暂不可用，请稍后重试；邀请归因不会重复写入",
            ) from e

        # 仅当 V3 未真实处理时(未启用 / hook 异常 / 老 V3.1 码)才 fallback
        if not v3_handled and not v3_consumed:
            try:
                from api.referral_api import bind_referral
                bind_referral(user_id, referral_code_to_bind)
            except Exception as e:
                logger.error(
                    "[注册] 老推荐码归因事务失败 type=%s · 拒绝静默降级",
                    type(e).__name__,
                )
                raise HTTPException(
                    status_code=503,
                    detail="注册服务暂不可用，请稍后重试；邀请归因不会重复写入",
                ) from e

    # 5. 签发 JWT Token（注册即登录）
    token = create_jwt(user_id)
    if not token:
        raise HTTPException(status_code=500, detail="注册成功但登录失败，请手动登录")

    # 6. 记录注册 IP 和地区
    try:
        ip = _get_client_ip(request)
        ip_updates = {"register_ip": ip, "phone": phone}
        if referral_code_to_bind:
            ip_updates["register_source"] = "referral"
        # 尝试 IP 地区解析（ip2region 可选）
        try:
            import subprocess
            # 简单方案：用系统命令或在线API，这里先记录IP，后续批量解析
            pass
        except Exception:
            pass
        update_user(user_id, **ip_updates)
    except Exception as e:
        logger.warning(f"[注册] IP记录失败（不影响注册）: {e}")

    user = get_user(user_id)
    logger.info(f"[注册] 新用户注册成功: {phone} (id={user_id}) from {_get_client_ip(request)}")

    # 7. 注册完成通知写耐久 outbox。这里是旧注册链的 post-commit 兼容桥：
    # 用户创建/邀请消费目前分属既有事务，本轮不改注册资金与商业关系算法。
    try:
        import hashlib
        from datetime import datetime, timezone
        from db.connection import get_db
        from services.notification_events import NotificationEventType, RecipientKind
        from services.notification_outbox import (
            enqueue_admin_notification_events,
            enqueue_notification_event,
        )

        occurred_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        public_registration_no = "REG-" + hashlib.sha256(
            f"registration:{int(user_id)}".encode("utf-8")
        ).hexdigest()[:10].upper()
        with get_db() as notification_conn:
            notification_cur = notification_conn.cursor()
            enqueue_admin_notification_events(
                notification_cur,
                event_type=NotificationEventType.ACCOUNT_REGISTERED,
                business_id=f"user:{int(user_id)}",
                terminal_state="registered",
                facts={
                    "business_no": public_registration_no,
                    "status": "注册完成",
                    "occurred_at": occurred_at,
                    "summary": "请在用户管理中查看账号状态。",
                },
            )
            if referral_code_to_bind:
                notification_cur.execute(
                    """SELECT referrer_id
                         FROM referral_links
                        WHERE referred_id=%s AND level=1
                        ORDER BY created_at ASC, referrer_id ASC
                        LIMIT 1""",
                    (int(user_id),),
                )
                referrer = notification_cur.fetchone()
                referrer_id = (
                    referrer.get("referrer_id") if isinstance(referrer, dict)
                    else referrer[0] if referrer else None
                )
                if referrer_id is not None:
                    enqueue_notification_event(
                        notification_cur,
                        event_type=NotificationEventType.REFERRAL_REGISTERED,
                        business_id=f"user:{int(user_id)}",
                        terminal_state="registered",
                        recipient_user_id=int(referrer_id),
                        recipient_kind=RecipientKind.USER,
                        facts={
                            "business_no": public_registration_no,
                            "status": "推荐注册完成",
                            "occurred_at": occurred_at,
                            "summary": "推荐记录已更新，请在推荐中心查看。",
                        },
                    )
    except Exception as e:
        logger.exception("[注册] 耐久通知 outbox 写入失败 type=%s", type(e).__name__)

    response = JSONResponse(content=jsonable_encoder({
        "success": True,
        "message": "注册成功",
        "token": token,
        "user": user,
    }))
    response.delete_cookie("omnirank_invite_attribution", path="/api/auth/register")
    response.delete_cookie("omnirank_invite_display", path="/")
    return response


@router.post("/login")
async def login(req: LoginRequest, request: Request):
    """
    用户登录
    
    - 验证用户名/密码
    - 检查登录限流
    - 成功返回 JWT Token + 用户信息
    """
    # 限流检查
    allowed, msg = check_rate_limit(req.username)
    if not allowed:
        logger.warning(f"登录限流: {req.username} from {_get_client_ip(request)}")
        raise HTTPException(status_code=429, detail=msg)

    # 只验证凭证。协议确认前不得生成或返回完整业务 JWT。
    user_raw = get_user_by_username(req.username)
    if (
        not user_raw
        or not bool(user_raw.get("is_active", True))
        or not verify_password(req.password, user_raw.get("password_hash") or "")
    ):
        record_failed_attempt(req.username)
        logger.info(f"登录失败: {req.username} from {_get_client_ip(request)}")
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    # 凭证成功，清除限流记录；协议门禁通过后才签发会话。
    clear_attempts(req.username)
    _require_registration_agreements(
        user_id=int(user_raw["id"]),
        auth_method="password",
    )

    token = create_jwt(int(user_raw["id"]))
    user = get_user(int(user_raw["id"]))
    if not token or not user:
        raise HTTPException(status_code=500, detail="登录失败")
    update_last_login(int(user_raw["id"]))
    logger.info(f"登录成功: {req.username} from {_get_client_ip(request)}")

    return {
        "success": True,
        "token": token,
        "user": user,
    }


@router.get("/me")
async def get_me(request: Request):
    """
    获取当前登录用户信息

    返回用户详情（角色、权限、分配的客户等）

    2026-04-20 commit 33 P1-A 修:
      - 老 `roles[].display_name` 可能是旧值"普通用户"(DB 未随升级更新)
      - 新加 `agent_level` + `agent_display_name` 字段基于 wallet.agent_level 派生
      - 同时 patch `roles[].display_name`(非 admin role 且 agent_level>=1 时 → "代理")
        让前端不需改就能看到正确身份
    """
    payload = _get_current_user(request)
    user = get_user(payload["user_id"])
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    # 派生 agent_level + agent_display_name(权威源 = user_wallets 表)
    agent_level = 0
    try:
        from db.wallet_db import get_wallet_balance
        wallet = get_wallet_balance(user.get("id")) or {}
        agent_level = int(wallet.get("agent_level", 0) or 0)
    except Exception as e:
        logger.warning(f"[auth/me] 读取 agent_level 失败: {e}")

    if user.get("is_admin"):
        user["agent_display_name"] = "管理员"
    elif agent_level >= 1:
        user["agent_display_name"] = "代理"
    else:
        user["agent_display_name"] = "普通用户"
    user["agent_level"] = agent_level

    # patch roles display_name 兜底(兼容老前端): 非 admin role + agent_level>=1 → "代理"
    for r in (user.get("roles") or []):
        if agent_level >= 1 and r.get("name") != "admin":
            r["display_name"] = "代理"

    # [单1 · WP6] 组织操作员上下文。
    # 权限串本身已在 db.auth_db.get_user_permissions() 里由组织能力推导并入
    # user["permissions"](前后端同一份);这里只补前端渲染/路由需要的身份说明。
    #
    # 关键界线:员工的 agent_level **保持他自己的真值**(通常 0)。服务商身份是
    # 资金语义(提现/佣金/进货价系数),员工不是服务商,绝不在这里伪造。
    # "能不能进服务商作业面"由 operating_for_agent(= 他代作业的商业主体是不是
    # 服务商)单独表达,消费方逐个显式接入,不搭 agent_level 的便车。
    try:
        from auth.organization_module_derivation import operator_context_for
        operator_context = operator_context_for(user.get("id"))
    except Exception as e:  # pragma: no cover - /me 不因组织体系异常而挂
        logger.warning(f"[auth/me] 读取组织操作员上下文失败: {e}")
        operator_context = None

    user["operator_context"] = operator_context
    user["operating_for_agent"] = bool(
        (operator_context or {}).get("operating_for_agent")
    )

    return {
        "success": True,
        "user": user
    }


@router.get("/profile")
async def get_profile(request: Request):
    """
    获取当前用户的完整资料（聚合多表数据）
    包含：基础信息 + 创作者画像 + 品牌列表 + 团队 + 钱包 + 使用统计
    """
    payload = _get_current_user(request)
    user_id = payload["user_id"]
    user = get_user(user_id)
    if not user:
        raise HTTPException(404, "用户不存在")

    result = {"user": user}

    # 创作者画像（从 client_profiles 聚合）
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()

            # 查找用户关联的 profile
            cur.execute("""
                SELECT cp.personality_profile, cp.persona_positioning, cp.persona_tone,
                       cp.business, cp.target_users, cp.creator_type, cp.content_frequency,
                       cp.monetization_goal, cp.follower_count
                FROM client_profiles cp
                JOIN brands b ON cp.brand_id = b.id
                JOIN user_clients uc ON uc.brand_id = b.id
                WHERE uc.user_id = %s AND cp.is_deleted = 0
                ORDER BY cp.updated_at DESC LIMIT 1
            """, (user_id,))
            profile = cur.fetchone()
            if profile:
                import json
                pp = profile.get("personality_profile")
                if pp and isinstance(pp, str):
                    try:
                        pp = json.loads(pp)
                    except Exception:
                        pp = {}
                result["personality"] = pp or {}
                result["creator_info"] = {
                    "positioning": profile.get("persona_positioning", ""),
                    "tone": profile.get("persona_tone", ""),
                    "business": profile.get("business", ""),
                    "target_users": profile.get("target_users", ""),
                    "creator_type": profile.get("creator_type", ""),
                    "content_frequency": profile.get("content_frequency", ""),
                    "monetization_goal": profile.get("monetization_goal", ""),
                    "follower_count": profile.get("follower_count", ""),
                }

            # 说话风格摘要
            cur.execute("""
                SELECT ps.style_profile, ps.top_quotes, ps.corpus_count
                FROM profile_style ps
                JOIN client_profiles cp ON ps.profile_id = cp.id
                JOIN brands b ON cp.brand_id = b.id
                JOIN user_clients uc ON uc.brand_id = b.id
                WHERE uc.user_id = %s
                ORDER BY ps.updated_at DESC LIMIT 1
            """, (user_id,))
            style = cur.fetchone()
            if style:
                result["speaking_style"] = {
                    "profile": style.get("style_profile"),
                    "top_quotes": style.get("top_quotes"),
                    "corpus_count": style.get("corpus_count", 0),
                }

            # 品牌列表
            # 与 /api/my-clients SSOT 对齐：过滤已软删 + 测试客户
            # 删除客户走 brands.is_deleted=TRUE,但 user_clients 关系不删,所以必须显式过滤
            # [返工2 修复净增量 P3] diagnosis_count/latest_score 不读 brands 冗余列(count 在装配期 +1 且 withheld
            #   不回退 → 含退款诊断只增不减;latest_score 装配期写 pending 分)· 改 published-only 子查询与 /api/my-clients 同源。
            cur.execute("""
                SELECT b.id, b.name, b.industry,
                       (SELECT COUNT(*) FROM diagnosis_records WHERE brand_id = b.id
                          AND (result_visibility IS NULL OR result_visibility = 'published')) as diagnosis_count,
                       -- [返工2 修复净增量2 P3] 不 COALESCE 归 0:裸子查询返 NULL(无 published 诊断时)· 与同源点
                       --   (m3 _BRAND_FIELDS / client-context list/detail)一致 · 且消除 ProfilePage `{score && ...}`
                       --   对 0 渲染多余"0"的 React 0-leak(NULL 才 falsy 隐藏 · 恢复本次修复前 b.latest_score 的 NULL 语义)。
                       (SELECT total_score FROM diagnosis_records WHERE brand_id = b.id
                          AND (result_visibility IS NULL OR result_visibility = 'published')
                          ORDER BY created_at DESC LIMIT 1) as latest_score
                FROM brands b
                JOIN user_clients uc ON uc.brand_id = b.id
                WHERE uc.user_id = %s
                  AND (b.is_deleted IS NULL OR b.is_deleted = FALSE)
                  AND (b.is_test IS NULL OR b.is_test = FALSE)
                ORDER BY b.updated_at DESC
            """, (user_id,))
            result["brands"] = [dict(r) for r in cur.fetchall()]

            # 团队
            cur.execute("""
                SELECT t.id, t.team_name, t.team_code, tm.role,
                       (SELECT COUNT(*) FROM team_members WHERE team_id = t.id AND status = 'active') as member_count
                FROM team_members tm
                JOIN teams t ON tm.team_id = t.id
                WHERE tm.user_id = %s AND tm.status = 'active'
                LIMIT 1
            """, (user_id,))
            team = cur.fetchone()
            result["team"] = dict(team) if team else None

            # 钱包
            cur.execute("SELECT * FROM user_wallets WHERE user_id = %s", (user_id,))
            wallet = cur.fetchone()
            result["wallet"] = dict(wallet) if wallet else {"paid_points": 0, "bonus_points": 0, "total_recharged": 0, "agent_level": 0}

            # 使用统计
            cur.execute("""
                SELECT feature_code, COUNT(*) as count
                FROM point_transactions
                WHERE user_id = %s AND type = 'consume'
                GROUP BY feature_code
                ORDER BY count DESC LIMIT 10
            """, (user_id,))
            result["top_features"] = [dict(r) for r in cur.fetchall()]

            cur.execute("SELECT COUNT(*) as total FROM point_transactions WHERE user_id = %s AND type = 'consume'", (user_id,))
            result["total_operations"] = cur.fetchone()["total"]

            # 推荐信息
            cur.execute("SELECT COUNT(*) as cnt FROM referral_links WHERE referrer_id = %s AND level = 1", (user_id,))
            result["direct_referrals"] = cur.fetchone()["cnt"]

            # 发布统计（迁到新表 mhz_publish_*）
            cur.execute("""
                SELECT COUNT(*) as total,
                       COUNT(*) FILTER (WHERE poi.status = 'published') as published,
                       COUNT(*) FILTER (WHERE poi.status = 'rejected') as rejected
                FROM mhz_publish_order_items poi
                JOIN mhz_publish_orders po ON poi.order_id = po.id
                WHERE po.user_id = %s
            """, (user_id,))
            pub = cur.fetchone()
            result["publish_stats"] = dict(pub) if pub else {"total": 0, "published": 0, "rejected": 0}

            conn.close()
        finally:
            try:
                conn.close()
            except Exception: pass
    except Exception as e:
        logger.warning(f"获取用户资料聚合数据失败: {e}")

    return {"success": True, **result}


@router.put("/profile")
async def update_profile(request: Request):
    """用户自助更新个人资料"""
    payload = _get_current_user(request)
    user_id = payload["user_id"]

    body = await request.json()

    # 只允许用户编辑这些字段
    editable = {"real_name", "email", "wechat_id", "company", "job_title",
                "industry", "city", "bio", "display_name", "quote_markup_ratio"}
    updates = {k: v for k, v in body.items() if k in editable and v is not None}

    quote_markup_ignored = False  # 未签/无权设系数时静默忽略该字段(不 403 整单)·回传标记+原因给客户反馈
    quote_markup_ignored_reason = None
    if "quote_markup_ratio" in updates:
        # [报价中心 · 2026-06-08 老板拍板] 报价系数是经营定价工具(永远本人 · 不继承上级):
        #   服务商 / admin 直接可设;普通用户已签《报价定价免责协议》+ 无平台 admin 强制系数 → 放行自设(签约即开通·不强制实名)。
        #   鉴权主体 = 当前操作用户 · 写入目标 = 同一用户自己的 users 行(无 IDOR)。
        from db.wallet_db import get_wallet_balance
        _u = get_user(user_id)
        _wallet = get_wallet_balance(user_id) or {}
        _is_agent = (_wallet.get("agent_level", 0) or 0) >= 1
        _is_admin = bool(_u and _u.get("is_admin"))
        _allow_quote_edit = _is_agent or _is_admin
        if not _allow_quote_edit:
            from services.agent_agreement import is_pricing_disclaimer_signed
            from services.agent_pricing_overrides import get_agent_quote_markup_override
            _has_admin_override = get_agent_quote_markup_override(user_id) is not None
            if is_pricing_disclaimer_signed(user_id) and not _has_admin_override:
                _allow_quote_edit = True
            elif _has_admin_override:
                # [对抗审计 high] 已签但平台强制系数 → 禁改 · 原因区分(不要误导"去签协议")
                quote_markup_ignored_reason = "平台已为你设定报价规则,当前暂不可自改"
            else:
                quote_markup_ignored_reason = "报价系数需先在报价中心签署《报价定价免责协议》后才能设置"
        if not _allow_quote_edit:
            updates.pop("quote_markup_ratio", None)
            quote_markup_ignored = True
        else:
            from services.quote_pricing_preferences import normalize_quote_markup_ratio
            updates["quote_markup_ratio"] = normalize_quote_markup_ratio(updates["quote_markup_ratio"])

    if not updates:
        if quote_markup_ignored:
            return {"success": True, "quote_markup_ignored": True,
                    "message": quote_markup_ignored_reason}
        return {"success": True, "message": "无需更新"}

    # 检查是否首次完善资料
    user = get_user(user_id)
    if "email" in updates and user:
        old_email = str(user.get("email") or "").strip().casefold()
        new_email = str(updates.get("email") or "").strip().casefold()
        if new_email != old_email:
            # Reset the value-bound proof in the same UPDATE as the new
            # address.  Invitation acceptance re-locks this row.
            updates["email_verified"] = False
            updates["email_verified_for"] = None
    if user and not user.get("profile_completed_at"):
        filled = sum(1 for k in ["real_name", "company", "industry", "city"] if updates.get(k) or (user.get(k) and k not in updates))
        if filled >= 3:
            from datetime import datetime
            updates["profile_completed_at"] = datetime.now()

    success = update_user(user_id, **updates)
    return {"success": success, "message": "资料已更新" if success else "更新失败",
            "quote_markup_ignored": quote_markup_ignored,
            "quote_markup_ignored_reason": quote_markup_ignored_reason}


@router.get("/quote-markup-preference")
async def get_quote_markup_preference(request: Request):
    """获取当前用户的报价系数偏好（只读聚合 · 报价中心展示用）。

    [报价中心 · 2026-06-08 老板拍板] 报价给终端客户永远用【本人】系数 · 绝不继承上级:
      - 服务商 / admin → can_edit=True · 展示自己设的有效系数
      - 普通用户(agent_level=0) → 已签《报价定价免责协议》→ can_edit=True 自设;未签 → needs_pricing_disclaimer=True 显签署门
      - 平台 admin override 存在 → 只读(can_edit=False)

    只读聚合,不改报价计算核心。优先级: admin override > 本人自设 > 平台默认(不继承上级)。
    客户侧绝不暴露 wholesale/cost/批发价/毛利明细 · margin_pct 仅是售价加成的整数百分比。
    """
    payload = _get_current_user(request)
    user_id = payload["user_id"]

    is_admin = bool(payload.get("is_admin"))

    from services.quote_pricing_preferences import (
        get_quote_markup_for_quote_viewer,
        get_user_quote_markup_ratio,
        _get_agent_level,
    )
    from services.agent_pricing_overrides import get_agent_quote_markup_override
    from services.agent_agreement import is_pricing_disclaimer_signed

    agent_level = _get_agent_level(user_id)
    is_provider = agent_level >= 1 or is_admin

    # [报价中心 · 2026-06-08 老板拍板] 报价系数永远用【本人】· 绝不继承上级 · 平台默认固定 1.0(上级只赚算力/进货链路)。
    #   口径:平台 admin override > 本人自设(>1.0)> 平台默认 1.0 · source ∈ {admin_override, self, default}(无 inherited)。
    #   普通用户(L0)须已签《报价定价免责协议》才能编辑;[返修点1] admin_override 存在时签约也改不了 → 不显签约门(显只读)。
    #   服务商 / admin 按原有豁免(不需另签本协议)。
    admin_override = get_agent_quote_markup_override(user_id)
    own_ratio = get_user_quote_markup_ratio(user_id)
    can_set = is_provider or is_pricing_disclaimer_signed(user_id)
    # [返修点1] admin_override 存在 → 平台强制·签约也改不了 → needs_pricing_disclaimer=False(显只读·不显签约门)
    needs_pricing_disclaimer = (not is_provider) and (admin_override is None) and (not can_set)
    effective_ratio = get_quote_markup_for_quote_viewer(user_id)  # admin > 本人自设(可用时)> 平台默认 1.0 · 永不继承

    if admin_override is not None:
        # 平台强制系数优先 · 只读(禁编辑)
        source = "admin_override"
        can_edit = False
    elif can_set and own_ratio is not None and own_ratio > 1.0:
        # [返修点2] 平台默认固定 1.0:仅 own>1.0 算本人自设;==1.0 视为平台默认(按成本基线)
        source = "self"
        can_edit = True
    else:
        source = "default"
        can_edit = can_set

    # margin_pct: 售价相对成本的加成百分比 · effective<=1 时 0
    margin_pct = 0
    if effective_ratio and effective_ratio > 1:
        margin_pct = round((effective_ratio - 1) / effective_ratio * 100)

    return {
        "effective_ratio": effective_ratio,
        "own_ratio": own_ratio,
        "admin_override": admin_override,
        "source": source,
        "can_edit": can_edit,
        "needs_pricing_disclaimer": needs_pricing_disclaimer,
        "margin_pct": margin_pct,
    }


@router.get("/pricing-disclaimer/status")
async def get_pricing_disclaimer_status(request: Request):
    """[报价定价免责协议 2026-06-08] 当前用户是否已签《报价定价免责协议》(所有登录用户可查)。
    前端报价中心据此决定:已签 → 开放自设系数/成本;未签 → 显示签署门。"""
    payload = _get_current_user(request)
    user_id = payload["user_id"]
    from db.connection import get_db
    from services.agent_agreement import get_agreement_status, PRICING_DISCLAIMER_VERSION
    with get_db() as conn:
        cur = conn.cursor()
        st = get_agreement_status(cur, user_id, PRICING_DISCLAIMER_VERSION)
    return {
        "version": PRICING_DISCLAIMER_VERSION,
        "signed": st.get("status") == "signed",
        "signed_at": st.get("signed_at"),
    }


@router.post("/pricing-disclaimer/sign")
async def sign_pricing_disclaimer(request: Request):
    """[报价定价免责协议 2026-06-08] 当前用户签署《报价定价免责协议》(所有登录用户可签·留痕 IP/UA)。
    签后才能在报价中心自设报价系数 / 单篇成本(配合 require_pricing_authority gate · 签约即开通不强制实名)。"""
    payload = _get_current_user(request)
    user_id = payload["user_id"]
    from db.connection import get_db
    from services.agent_agreement import sign_agreement, PRICING_DISCLAIMER_VERSION
    signed_ip = _get_client_ip(request)
    signed_ua = request.headers.get("User-Agent")
    with get_db() as conn:
        cur = conn.cursor()
        result = sign_agreement(
            cur, agent_user_id=user_id, version=PRICING_DISCLAIMER_VERSION,
            signed_ip=signed_ip, signed_ua=signed_ua,
        )
        conn.commit()
    return result


@router.post("/avatar")
async def upload_avatar(request: Request):
    """上传头像"""
    payload = _get_current_user(request)
    user_id = payload["user_id"]

    # [GEO-R1-CAN-022] 先按 Content-Length 粗筛,避免把超大 body 读进内存(fail-closed)
    try:
        _clen = int(request.headers.get("content-length") or 0)
    except (TypeError, ValueError):
        _clen = 0
    if _clen and _clen > AVATAR_MAX_BYTES:
        raise HTTPException(413, "图片过大,请上传 5MB 以内的图片")

    form = await request.form()
    file = form.get("file")
    if not file or not hasattr(file, "read"):
        raise HTTPException(400, "请选择图片")

    from pathlib import Path

    content = await file.read()

    # [GEO-R1-CAN-022] 大小硬闸(读入后再核一次,防止 Content-Length 缺失/伪造)
    if not content:
        raise HTTPException(400, "图片内容为空")
    if len(content) > AVATAR_MAX_BYTES:
        raise HTTPException(413, "图片过大,请上传 5MB 以内的图片")

    # [GEO-R1-CAN-022] 用 magic bytes 判定真实图片类型,后缀由服务端派生(忽略上传文件名)
    #   仅允许 JPG/PNG/GIF/WEBP;非图片直接拒绝,阻断存储型 XSS/任意文件落盘
    ext = _sniff_image_ext(content)
    if not ext:
        raise HTTPException(400, "仅支持 JPG/PNG/GIF/WEBP 图片")

    # 保存到 uploads/avatars/
    upload_dir = Path("uploads/avatars")
    upload_dir.mkdir(parents=True, exist_ok=True)

    filename = f"avatar_{user_id}{ext}"
    filepath = upload_dir / filename

    with open(filepath, "wb") as f:
        f.write(content)

    avatar_url = f"/uploads/avatars/{filename}"
    update_user(user_id, avatar_url=avatar_url)

    return {"success": True, "avatar_url": avatar_url}


@router.post("/refresh")
async def refresh_permissions(request: Request):
    """
    刷新权限（重新签发 JWT）
    
    当收到 permission_changed 响应时，前端自动调用此接口
    """
    payload = _get_current_user(request)
    new_token = refresh_jwt(payload["user_id"])

    if not new_token:
        raise HTTPException(status_code=500, detail="Token 刷新失败")

    user = get_user(payload["user_id"])

    return {
        "success": True,
        "token": new_token,
        "user": user
    }


@router.post("/change-password")
async def change_password(req: ChangePasswordRequest, request: Request):
    """
    修改密码
    
    - 首登强制修改时 old_password 为初始密码
    - 修改成功后 must_change_password 自动置为 0
    - permission_version 自动递增（使旧 JWT 失效）
    """
    payload = _get_current_user(request)

    # 验证旧密码
    from db.auth_db import get_user_by_username, verify_password
    user_raw = get_user_by_username(payload["username"])
    if not user_raw or not verify_password(req.old_password, user_raw["password_hash"]):
        return {"success": False, "error": "原密码错误"}

    # 新密码不能和旧密码相同
    if req.old_password == req.new_password:
        return {"success": False, "error": "新密码不能与原密码相同"}

    # 更新密码（同时清除 must_change_password，递增 permission_version）
    success = update_user_password(payload["user_id"], req.new_password)
    if not success:
        return {"success": False, "error": "密码修改失败"}

    # 签发新 Token
    new_token = create_jwt(payload["user_id"])

    logger.info(f"密码修改成功: {payload['username']}")

    return {
        "success": True,
        "message": "密码修改成功",
        "token": new_token
    }


@router.get("/permissions")
async def get_permissions(request: Request):
    """
    获取当前用户的权限列表
    
    返回 ['module:level', ...] 格式
    """
    payload = _get_current_user(request)

    if payload.get("is_admin"):
        # admin 拥有所有权限
        from db.auth_db import ALL_MODULES, ALL_LEVELS
        permissions = [f"{m}:{l}" for m in ALL_MODULES for l in ALL_LEVELS]
    else:
        permissions = get_user_permissions(payload["user_id"])

    return {
        "success": True,
        "permissions": permissions,
        "is_admin": payload.get("is_admin", False)
    }


# ==================== 短信验证码 ====================

@router.get("/captcha")
async def get_captcha():
    """获取图形验证码（算术题SVG）"""
    from auth.sms_captcha import generate_captcha
    return generate_captcha()


@router.post("/send-sms")
async def send_sms(req: SMSSendRequest, request: Request):
    """发送短信验证码"""
    import re
    phone = req.phone.strip()

    if not re.match(r'^1\d{10}$', phone):
        raise HTTPException(status_code=400, detail="请输入正确的手机号")

    # 注册和重置密码时需要图形验证码
    if req.purpose in ("register", "reset_password"):
        from auth.sms_captcha import verify_captcha
        if not verify_captcha(req.captcha_id, req.captcha_answer):
            raise HTTPException(status_code=400, detail="图形验证码错误")

    # 注册时检查手机号是否已注册
    if req.purpose == "register":
        from db.auth_db import get_user_by_phone
        if get_user_by_phone(phone):
            return {"success": False, "message": "该手机号已注册，请直接登录", "redirect": "login"}

    # 登录时检查手机号是否存在（不存在也可以，走自动注册）

    # 频率限制
    from auth.sms_rate_limiter import sms_limiter
    client_ip = _get_client_ip(request)

    ok, msg = sms_limiter.check_phone(phone)
    if not ok:
        return {"success": False, "message": msg}

    ok, msg = sms_limiter.check_ip(client_ip)
    if not ok:
        return {"success": False, "message": msg}

    # 发送
    from auth.sms_service import send_sms_code
    result = send_sms_code(phone, req.purpose)

    if result["success"]:
        sms_limiter.record(phone, client_ip)
        return {"success": True, "message": "验证码已发送", "cooldown": 60}
    else:
        return {"success": False, "message": result.get("error", "发送失败")}


@router.post("/login-sms")
async def login_sms(req: SMSLoginRequest, request: Request):
    """验证码快捷登录（只登录已完成协议确认的注册账户）"""
    import re
    phone = req.phone.strip()
    sms_code = req.sms_code.strip()

    if not re.match(r'^1\d{10}$', phone):
        raise HTTPException(status_code=400, detail="请输入正确的手机号")

    # 验证短信验证码
    from auth.sms_service import verify_sms_code
    result = verify_sms_code(phone, sms_code, "login")
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])

    # 查找用户（优先按 phone 字段，退回按 username=手机号）
    from db.auth_db import get_user_by_phone, get_user_by_username
    user = get_user_by_phone(phone) or get_user_by_username(phone)

    if not user:
        raise HTTPException(status_code=400, detail="该手机号未注册 · 请先阅读协议并完成注册")
    else:
        if not bool(user.get("is_active", True)):
            raise HTTPException(status_code=403, detail="账号已停用，请联系管理员")
        _require_registration_agreements(user_id=int(user["id"]), auth_method="sms")
        # 更新手机号验证状态
        update_user(user["id"], phone_verified=True)

    # 签发 JWT Token
    from auth.jwt_utils import create_jwt
    token = create_jwt(user["id"])
    if not token:
        raise HTTPException(status_code=500, detail="登录失败")

    update_last_login(user["id"])

    return {
        "success": True,
        "token": token,
        "user": {
            "id": user["id"],
            "display_name": user.get("display_name", ""),
            "phone": phone[:3] + "****" + phone[-4:],
        }
    }
