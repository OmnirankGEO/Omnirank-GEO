"""Commercial authority boundary for production customer-portal credentials."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

from fastapi import HTTPException, Request

from db.connection import get_db

# 🔴 [R4 ④ 2026-08-20] 下面四个符号原来全是**函数体内惰性 import**,而且都在
#   `with get_db()` 的读事务**里面**执行(96 / 125 / 137 / 147 行)。
#   R3 取证:其中 `auth.brand_access` 这一条会连锁 import `db.diagnosis_db`,
#   后者模块体执行 init_db() → `ALTER TABLE quotes …` 要 AccessExclusiveLock,
#   而它在等的正是本函数自己那个还开着的读事务 ⇒ **单线程自死锁,不超时不报错**。
#   只上提 brand_access 内部那层不够 —— 炸弹会挪到「本文件首次 import brand_access」
#   这一层(实测仍 rc=124)。四条一起上提,才真的把 import 挪出事务。
#   环已核:这四个模块都不反向 import 本模块。
# 🔴 上提**模块**而不是 `from X import Y` 上提符号 —— 这一步是实测逼出来的:
#   第一版写成 `from services.admin_cross_tenant_governance import record_admin_read`,
#   名字在**导入时**就绑死了,于是
#   tests/admin_user_governance/test_cross_tenant_governance.py 里的
#     monkeypatch.setattr(cross_tenant_governance, "record_admin_read", ...)
#   再也打不到本模块 ⇒ 双臂 A/B 当场多出一条新增红
#   (test_portal_token_authority_audits_admin_ignores_demo_and_accepts_owner)。
#   改成「上提模块 + 调用时取属性」:既保住 ④ 要的「import 发生在加载期、不在事务里」,
#   又保住测试的 monkeypatch 缝。四个模块均已核无反向 import 环。
import auth.brand_access as _brand_access
import db.organization_db as _organization_db
import services.admin_cross_tenant_governance as _admin_governance


logger = logging.getLogger("GEO-Portal-Authority")


def _request_id(request: Request) -> str:
    return str(
        getattr(request.state, "request_id", None)
        or getattr(request.state, "organization_request_id", None)
        or request.headers.get("X-Request-ID")
        or uuid.uuid4().hex
    )[:180]


def _deny(request: Request, *, code: str = "PORTAL_TOKEN_AUTHORITY_REQUIRED") -> None:
    request_id = _request_id(request)
    logger.warning(
        "portal_token_authority_denied %s",
        json.dumps({
            "event": "portal_token_authority_denied",
            "code": code,
            "request_id": request_id,
            "user_id": int((getattr(request.state, "user", None) or {}).get("user_id") or 0),
        }, sort_keys=True),
    )
    raise HTTPException(
        status_code=403,
        detail={
            "code": code,
            "message": "仅客户商业所有者或已正式分配并获授权的成员可管理客户门户",
            "request_id": request_id,
        },
        headers={"X-Error-Code": code, "Cache-Control": "no-store"},
    )


def require_portal_token_authority(
    request: Request, *, quote_id: Optional[int] = None, brand_id: Optional[int] = None,
    action: str = "portal.token.read",
) -> dict[str, Any]:
    """Require live commercial ownership or an active assigned member capability.

    Platform administrators use the dedicated audited governance path. Demo
    grants remain irrelevant to this predicate and can never mint credentials.
    """
    user = getattr(request.state, "user", None) or {}
    user_id = int(user.get("user_id") or user.get("id") or 0)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")
    if quote_id is None and brand_id is None:
        _deny(request)

    with get_db() as conn:
        cur = conn.cursor()
        if quote_id is not None:
            cur.execute(
                """SELECT q.id AS quote_id,b.id AS brand_id,q.brand_name,b.owner_user_id
                   FROM public.quotes q
                   JOIN public.brands b ON b.id=q.brand_id
                   WHERE q.id=%s AND (b.is_deleted IS NULL OR b.is_deleted=FALSE)""",
                (int(quote_id),),
            )
        else:
            cur.execute(
                """SELECT q.id AS quote_id,q.brand_id,q.brand_name,b.owner_user_id
                   FROM public.brands b
                   LEFT JOIN LATERAL (
                     SELECT id,brand_id,brand_name FROM public.quotes
                     WHERE brand_id=b.id ORDER BY created_at DESC,id DESC LIMIT 1
                   ) q ON TRUE
                   WHERE b.id=%s AND (b.is_deleted IS NULL OR b.is_deleted=FALSE)""",
                (int(brand_id),),
            )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="资源不存在")
        result = dict(row)
        resolved_brand_id = int(result["brand_id"] or brand_id or 0)
        owner_user_id = int(result["owner_user_id"] or 0)
        if owner_user_id == user_id:
            return result

        if user.get("is_admin"):

            forwarded = request.headers.get("X-Forwarded-For")
            ip_address = (
                forwarded.split(",")[0].strip()
                if forwarded
                else (request.client.host if request.client else "unknown")
            )
            _admin_governance.record_admin_read(
                actor_user_id=user_id,
                actor_username=user.get("username"),
                action=action,
                subject_kind="customer_portal_credential",
                subject_id=int(result["quote_id"]) if result.get("quote_id") else None,
                request_id=_request_id(request),
                reason="平台管理员处理客户门户凭证",
                before={
                    "quote_id": result.get("quote_id"),
                    "brand_id": resolved_brand_id,
                    "owner_user_id": owner_user_id,
                },
                after={"authority": "platform_admin_governance"},
                ip_address=ip_address,
            )
            return result

        identity = getattr(request.state, "organization_identity", None)
        if identity is None:
            try:
                identity = _organization_db.resolve_identity(user_id, request_id=_request_id(request))
            except Exception:
                identity = None
        if identity is not None and identity.is_member:
            if identity.organization_status != "active" or identity.membership_status != "active":
                _deny(request, code="PORTAL_TOKEN_MEMBERSHIP_INACTIVE")
            if int(identity.principal_user_id) != owner_user_id:
                _deny(request)
            if "reports.share_external" not in identity.capabilities:
                _deny(request, code="PORTAL_TOKEN_CAPABILITY_REQUIRED")

            if resolved_brand_id not in _organization_db.assigned_brand_ids(identity, cursor=cur):
                _deny(request, code="PORTAL_TOKEN_ASSIGNMENT_REQUIRED")
            return result

        # Existing service-provider accounts predate organization memberships.
        # Their commercial customer assignments remain authoritative through
        # the same quote/brand predicate used by the rest of the live product.
        # Demo grants do not enter that predicate.
        try:
            _brand_access.require_quote_access(request, int(result["quote_id"]), allow_null=False)
        except HTTPException:
            _deny(request)
        return result
