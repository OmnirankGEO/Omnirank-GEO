"""Canonical commercial service routing and relationship write coordination.

Only ``customer_agent_bindings`` is the ordinary-customer commercial SSOT.
Referral attribution remains independent evidence; an eligible service-provider
invitation may establish the customer's first binding through the canonical
writer. Every relationship reader/writer uses the advisory lock in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, TypeVar

from db.connection import get_db
from schemas.public_contracts import AgentServiceDTO, CustomerServiceDTO
from db.xact_lock_guard import require_xact_scope


COMMERCIAL_BINDING_LOCK_NAMESPACE = 920716
COMMERCIAL_PROVIDER_LOCK_NAMESPACE = 920717


class CommercialServiceRoutingError(RuntimeError):
    def __init__(self, code: str, message: str, *, internal_detail: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.internal_detail = internal_detail or message


class RelationshipError(CommercialServiceRoutingError):
    """Internal details must never be returned by public APIs."""


class RelationshipConflict(RelationshipError):
    def __init__(self, internal_detail: str):
        super().__init__(
            "ACCOUNT_CONFIGURATION_REVIEW_REQUIRED",
            "当前账户配置需要平台处理，请稍后重试",
            internal_detail=internal_detail,
        )


class PlatformDirectUnavailable(RelationshipError):
    def __init__(self, internal_detail: str):
        super().__init__(
            "ACCOUNT_CONFIGURATION_UNAVAILABLE",
            "当前账户价格配置暂不可用，请稍后重试",
            internal_detail=internal_detail,
        )


class RelationshipResolution(str, Enum):
    BOUND = "BOUND"
    PLATFORM_DIRECT = "PLATFORM_DIRECT"


@dataclass(frozen=True)
class CommercialRelationship:
    customer_user_id: int
    service_user_id: int
    resolution: RelationshipResolution
    relationship_id: Optional[int] = None
    dispute_status: Optional[str] = None
    service_scope_key: Optional[str] = None

    @property
    def public_configuration_status(self) -> str:
        return "ready"

    def customer_dto(self) -> CustomerServiceDTO:
        return CustomerServiceDTO(
            configuration_status="ready",
            account_configured=True,
            dispute_pending=False,
        )

    def agent_dto(self) -> AgentServiceDTO:
        return AgentServiceDTO(configuration_status="ready")


def lock_commercial_binding_subject(cur, customer_user_id: int) -> None:
    """Serialize absence/presence checks with registration, admin and settlement."""
    require_xact_scope(cur, where="commercial_service_routing.lock_commercial_binding_subject")  # §1 硬闸:autocommit 下取事务锁=没锁
    cur.execute(
        "SELECT pg_advisory_xact_lock(%s,%s)",
        (COMMERCIAL_BINDING_LOCK_NAMESPACE, int(customer_user_id)),
    )


def lock_commercial_relationship(cursor, customer_user_id: int) -> None:
    """Compatibility name for the latest quote/refund relationship writers."""

    lock_commercial_binding_subject(cursor, int(customer_user_id))


def lock_commercial_provider_lifecycle(cur, provider_user_id: int) -> None:
    """Serialize provider assignment with platform-access eligibility changes."""
    require_xact_scope(cur, where="commercial_service_routing.lock_commercial_provider_lifecycle")  # §1 硬闸:autocommit 下取事务锁=没锁
    cur.execute(
        "SELECT pg_advisory_xact_lock(%s,%s)",
        (COMMERCIAL_PROVIDER_LOCK_NAMESPACE, int(provider_user_id)),
    )


def lock_pending_commercial_orders(cur, customer_user_id: int) -> int:
    """Lock and count pending customer orders before taking the subject lock."""
    cur.execute(
        """SELECT id FROM recharge_orders
           WHERE user_id=%s AND payment_status='pending'
             AND (order_type IS NULL OR order_type='customer_recharge')
           ORDER BY id FOR UPDATE""",
        (int(customer_user_id),),
    )
    return len(cur.fetchall() or [])


def _active(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "disabled", "inactive"}
    return bool(value)


def lock_commercial_provider_for_assignment(cur, provider_user_id: int) -> Dict[str, Any]:
    """Lock and validate the exact provider contract used by the resolver."""
    cur.execute(
        """SELECT u.id,u.is_active,COALESCE(w.agent_level,0) AS agent_level,
                  pac.service_account_code,
                  EXISTS(
                      SELECT 1 FROM user_roles ur JOIN roles r ON r.id=ur.role_id
                      WHERE ur.user_id=u.id AND r.name='admin'
                  ) AS is_admin
           FROM users u
           LEFT JOIN user_wallets w ON w.user_id=u.id
           LEFT JOIN public_account_codes pac ON pac.user_id=u.id
           WHERE u.id=%s
           FOR UPDATE OF u""",
        (int(provider_user_id),),
    )
    row = _as_dict(cur.fetchone())
    if not row or not _active(row.get("is_active")):
        raise RelationshipConflict("commercial service account is inactive")
    if int(row.get("agent_level") or 0) < 1:
        raise RelationshipConflict("commercial service account is invalid")
    if bool(row.get("is_admin")):
        raise RelationshipConflict("administrator personal account cannot be a commercial provider")
    if not row.get("service_account_code"):
        raise RelationshipConflict("commercial service price scope is not ready")
    return row


def _as_dict(row: Any) -> Dict[str, Any]:
    if not row:
        return {}
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row)
    except Exception:
        return {}


def _configured_user_id() -> Optional[int]:
    raw = (os.getenv("PLATFORM_DIRECT_SERVICE_USER_ID") or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise PlatformDirectUnavailable("platform direct service account configuration is invalid") from exc
    if value <= 0:
        raise PlatformDirectUnavailable("platform direct service account configuration is invalid")
    return value


def get_platform_direct_service_user_id() -> int:
    value = _configured_user_id()
    if value is None:
        raise PlatformDirectUnavailable("platform direct service account is not configured")
    return value


def read_platform_direct_service_identity(cur) -> Dict[str, Any]:
    """Resolve the dedicated platform-direct business identity for admin operations.

    This deliberately validates only the account identity contract.  Catalog
    readiness belongs to the pricing endpoint that consumes the identity; tying
    every admin business page to a published retail catalog would incorrectly
    block inventory, settlement, and promotion pages.
    """

    configured_id = get_platform_direct_service_user_id()
    row = _read_provider(cur, configured_id)
    if not row or not _active(row.get("is_active")):
        raise PlatformDirectUnavailable("platform direct service account is inactive")
    if int(row.get("agent_level") or 0) < 1:
        raise PlatformDirectUnavailable("platform direct service account is not a service provider")
    if bool(row.get("is_admin")):
        raise PlatformDirectUnavailable("administrator personal account cannot be platform direct")
    if not row.get("service_account_code"):
        raise PlatformDirectUnavailable("platform direct service price scope is not ready")
    return row


def _read_provider(cur, user_id: int) -> Optional[Dict[str, Any]]:
    cur.execute(
        """
        SELECT u.id, u.username, u.display_name, u.is_active,
               to_jsonb(u)->>'company' AS company,
               COALESCE(w.agent_level,0) AS agent_level,
               pac.service_account_code,
               EXISTS(
                   SELECT 1 FROM user_roles ur JOIN roles r ON r.id=ur.role_id
                   WHERE ur.user_id=u.id AND r.name='admin'
               ) AS is_admin
        FROM users u
        LEFT JOIN user_wallets w ON w.user_id=u.id
        LEFT JOIN public_account_codes pac ON pac.user_id=u.id
        WHERE u.id=%s
        """,
        (int(user_id),),
    )
    row = cur.fetchone()
    return _as_dict(row) if row else None


def platform_direct_readiness(*, cur=None) -> Dict[str, Any]:
    """Admin/internal read-only readiness; never creates or repairs accounts."""

    try:
        configured_id = _configured_user_id()
    except PlatformDirectUnavailable as exc:
        return {
            "configured": True,
            "ready": False,
            "status": "invalid",
            "label": "平台直营专用服务账号配置无效",
            "service_user": None,
            "checks": [str(exc)],
        }
    if configured_id is None:
        return {
            "configured": False,
            "ready": False,
            "status": "missing",
            "label": "尚未配置平台直营专用服务账号",
            "service_user": None,
            "checks": ["需由部署配置提供专用服务账号", "本任务不会创建生产账号或写入配置"],
        }

    def inspect(cursor):
        row = _read_provider(cursor, configured_id)
        checks = []
        quoteable_product_code = None
        published_version_code = None
        if not row:
            checks.append("配置账号不存在")
        else:
            if not _active(row.get("is_active")):
                checks.append("配置账号已停用")
            if int(row.get("agent_level") or 0) < 1:
                checks.append("配置账号不是服务商")
            if bool(row.get("is_admin")):
                checks.append("配置账号拥有管理员权限；禁止使用超级管理员个人账号")
            if not row.get("service_account_code"):
                checks.append("配置账号尚未准备零售价目服务编号")
            else:
                # Use the same published catalog reader and pure quote gates as the
                # live retail endpoint.  Merely owning an SV code is not readiness.
                from services import pricing_catalog
                from services.price_quote import preview_published_quote_entry

                version = pricing_catalog.get_published_version(
                    "retail", str(row["service_account_code"]), cur=cursor
                )
                if not version:
                    checks.append("该服务编号尚无已发布零售价目版本")
                else:
                    published_version_code = str(version.get("version_code") or "")
                    cursor.execute(
                        """SELECT * FROM pricing_catalog_entries
                           WHERE version_id=%s ORDER BY product_code""",
                        (int(version["id"]),),
                    )
                    entries = [dict(item) for item in (cursor.fetchall() or [])]
                    if not entries:
                        checks.append("已发布零售价目没有启用商品")
                    else:
                        quote_errors = []
                        quoteable_products = []
                        for entry in entries:
                            try:
                                preview = preview_published_quote_entry(
                                    quote_type="retail",
                                    entry=entry,
                                    product_code=str(entry["product_code"]),
                                    quantity=1,
                                )
                                from config.dealer_inventory_resale_flags import enabled as resale_enabled
                                if resale_enabled(cursor):
                                    source_seller = (preview.get("source_ref") or {}).get("agent_user_id")
                                    if source_seller is None or int(source_seller) != int(row["id"]):
                                        raise ValueError("零售价目服务主体与平台直营专用账号不一致")
                                    from services.dealer_inventory_resale import preview_consumer_quote_terms
                                    preview_consumer_quote_terms(
                                        cursor,
                                        seller_user_id=int(row["id"]),
                                        points=(
                                            int(preview["points_granted"])
                                            + int(preview["bonus_points"])
                                        ),
                                        catalog_reference_amount_cents=int(
                                            preview["final_price_cents"]
                                        ),
                                        pricing_version=published_version_code,
                                    )
                                quoteable_products.append(str(entry["product_code"]))
                            except Exception as exc:  # readiness must fail closed on any live quote gate
                                quote_errors.append(f"{entry.get('product_code')}: {exc}")
                        if quote_errors:
                            checks.append(f"已发布零售价目无法完整生成报价：{quote_errors[0]}")
                        elif quoteable_products:
                            quoteable_product_code = ",".join(quoteable_products)
        ready = bool(row) and not checks
        if ready:
            checks = [
                "账号存在且已启用",
                "业务身份为服务商",
                "未使用管理员个人账号",
                f"零售价目 {published_version_code} 已发布",
                f"商品 {quoteable_product_code} 可生成报价",
            ]
        service_user = None
        if row:
            service_user = {
                "user_id": int(row["id"]),
                "username": row.get("username"),
                "display_name": row.get("display_name") or row.get("username"),
                "is_active": _active(row.get("is_active")),
                "company": row.get("company"),
                "business_identity": (
                    "service_provider" if int(row.get("agent_level") or 0) >= 1 else "ordinary_user"
                ),
                "service_code": row.get("service_account_code"),
                "channel_code": None,
            }
        return {
            "configured": True,
            "ready": ready,
            "status": "ready" if ready else "invalid",
            "label": "平台直营专用服务账号已就绪" if ready else "平台直营专用服务账号未通过检查",
            "service_user": service_user,
            "checks": checks,
        }

    if cur is not None:
        return inspect(cur)
    with get_db() as conn:
        return inspect(conn.cursor())


def classify_commercial_relationship(
    *,
    customer_user_id: int,
    binding_rows: Iterable[Mapping[str, Any]],
    platform_direct_user_id: int,
) -> CommercialRelationship:
    """Classify preloaded rows; callers must never pass referral rows here."""

    rows = [dict(row) for row in binding_rows]
    if not rows:
        if int(platform_direct_user_id or 0) <= 0:
            raise PlatformDirectUnavailable("platform direct service account is invalid")
        return CommercialRelationship(
            customer_user_id=int(customer_user_id),
            service_user_id=int(platform_direct_user_id),
            resolution=RelationshipResolution.PLATFORM_DIRECT,
        )
    if len(rows) != 1:
        raise RelationshipConflict("multiple commercial bindings")

    row = rows[0]
    dispute = str(row.get("dispute_status") or "").strip().lower()
    if dispute not in ("", "resolved", "reverted"):
        raise RelationshipConflict("commercial binding is disputed")
    if not _active(row.get("agent_is_active")):
        raise RelationshipConflict("commercial service account is inactive")
    try:
        agent_level = int(row.get("agent_level") or 0)
        service_user_id = int(row.get("agent_user_id") or 0)
    except (TypeError, ValueError) as exc:
        raise RelationshipConflict("commercial binding is malformed") from exc
    if service_user_id <= 0 or agent_level < 1:
        raise RelationshipConflict("commercial service account is invalid")
    if bool(row.get("agent_is_admin")):
        raise RelationshipConflict("administrator personal account cannot be a commercial provider")
    if "service_account_code" in row and not row.get("service_account_code"):
        raise RelationshipConflict("commercial service price scope is not ready")

    return CommercialRelationship(
        customer_user_id=int(customer_user_id),
        service_user_id=service_user_id,
        resolution=RelationshipResolution.BOUND,
        relationship_id=int(row["id"]) if row.get("id") is not None else None,
        dispute_status=row.get("dispute_status"),
        service_scope_key=row.get("service_account_code"),
    )


def resolve_commercial_relationship(
    cursor,
    customer_user_id: int,
    *,
    for_update: bool = False,
) -> CommercialRelationship:
    """The sole relationship resolver used by pricing, wallet and settlement."""

    if for_update:
        lock_commercial_binding_subject(cursor, customer_user_id)
    lock_clause = " FOR UPDATE OF cab" if for_update else ""
    cursor.execute(
        f"""
        SELECT cab.id, cab.agent_user_id, cab.dispute_status,
               u.is_active AS agent_is_active,
               COALESCE(uw.agent_level, 0) AS agent_level,
               pac.service_account_code,
               EXISTS(
                   SELECT 1 FROM user_roles ur JOIN roles r ON r.id=ur.role_id
                   WHERE ur.user_id=cab.agent_user_id AND r.name='admin'
               ) AS agent_is_admin
        FROM customer_agent_bindings cab
        LEFT JOIN users u ON u.id=cab.agent_user_id
        LEFT JOIN user_wallets uw ON uw.user_id=cab.agent_user_id
        LEFT JOIN public_account_codes pac ON pac.user_id=cab.agent_user_id
        WHERE cab.customer_user_id=%s
        ORDER BY cab.id
        {lock_clause}
        """,
        (int(customer_user_id),),
    )
    rows = [_as_dict(row) for row in cursor.fetchall()]
    if rows:
        return classify_commercial_relationship(
            customer_user_id=int(customer_user_id),
            binding_rows=rows,
            platform_direct_user_id=0,
        )

    readiness = platform_direct_readiness(cur=cursor)
    if not readiness["ready"]:
        raise PlatformDirectUnavailable("platform direct service account is unavailable")
    service_user = readiness["service_user"]
    return CommercialRelationship(
        customer_user_id=int(customer_user_id),
        service_user_id=int(service_user["user_id"]),
        resolution=RelationshipResolution.PLATFORM_DIRECT,
        service_scope_key=service_user["service_code"],
    )


_T = TypeVar("_T")


def execute_under_commercial_relationship_fence(
    *,
    customer_user_id: int,
    expected_service_user_id: int,
    expected_source: str,
    writer: Callable[[], _T],
) -> _T:
    """Hold the subject lock until a quote-less order writer has committed."""
    with get_db() as conn:
        relationship = resolve_commercial_relationship(
            conn.cursor(), int(customer_user_id), for_update=True
        )
        source = (
            "explicit_binding"
            if relationship.resolution is RelationshipResolution.BOUND
            else "platform_direct"
        )
        if (
            int(relationship.service_user_id) != int(expected_service_user_id)
            or source != str(expected_source)
        ):
            raise RelationshipConflict("commercial service relationship changed")
        # The legacy wallet writer owns a separate transaction.  This outer
        # transaction deliberately remains open until it commits, serializing
        # absence/presence with the administrator CAS writer.
        return writer()


def read_locked_commercial_binding(cursor, customer_user_id: int) -> Optional[Dict[str, Any]]:
    """Return the raw locked projection needed by settlement, preserving conflicts."""

    lock_commercial_binding_subject(cursor, int(customer_user_id))
    cursor.execute(
        """
        SELECT id,agent_user_id,binding_source,source_token,bound_at,dispute_status
        FROM customer_agent_bindings
        WHERE customer_user_id=%s
        ORDER BY id
        FOR UPDATE
        """,
        (int(customer_user_id),),
    )
    rows = cursor.fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        return {
            "agent_user_id": None,
            "dispute_status": "configuration_conflict",
            "configuration_conflict": True,
        }
    return _as_dict(rows[0])


def resolve_effective_service_provider(
    customer_user_id: int,
    *,
    cur=None,
    lock_binding: bool = False,
) -> Dict[str, Any]:
    """Admin-compatible projection backed by the same canonical resolver."""

    def resolve(cursor):
        relationship = resolve_commercial_relationship(
            cursor,
            int(customer_user_id),
            for_update=lock_binding,
        )
        return {
            "provider_user_id": relationship.service_user_id,
            "service_scope_key": relationship.service_scope_key,
            "source": (
                "explicit_binding"
                if relationship.resolution is RelationshipResolution.BOUND
                else "platform_direct"
            ),
            "binding_id": relationship.relationship_id,
        }

    if cur is not None:
        return resolve(cur)
    with get_db() as conn:
        return resolve(conn.cursor())


def solidify_service_provider_invitation(
    cursor,
    customer_user_id: int,
    inviter_user_id: int,
    invite_code: str,
) -> Dict[str, str]:
    """Bind a new ordinary customer to a verified service-provider inviter.

    Referral attribution remains independently auditable in ``referral_links``;
    this function writes the commercial SSOT only after checking both identities.
    The raw invitation code is never stored.
    """
    import hashlib

    cursor.execute(
        """SELECT w.user_id AS wallet_user_id, w.agent_level AS customer_level
           FROM users u
           LEFT JOIN user_wallets w ON w.user_id=u.id
           WHERE u.id=%s""",
        (int(customer_user_id),),
    )
    row = cursor.fetchone()
    if not row:
        return {"action": "subject_unavailable"}
    data = _as_dict(row)
    if data.get("wallet_user_id") is None or data.get("customer_level") is None:
        return {"action": "subject_identity_unavailable"}
    if int(data.get("customer_level") or 0) >= 1:
        return {"action": "service_provider_requires_channel_relationship"}

    # Provider eligibility and assignment share one lifecycle fence. Without
    # this lock an admin could grant platform access or deactivate the inviter
    # between validation and the absent-row binding insert.
    lock_commercial_provider_lifecycle(cursor, int(inviter_user_id))
    try:
        lock_commercial_provider_for_assignment(cursor, int(inviter_user_id))
    except RelationshipConflict:
        return {"action": "inviter_not_eligible"}

    # The legacy referral endpoint can be reached after account creation. Never
    # change the service principal while a customer recharge is awaiting
    # payment; its quote/order snapshot must remain the settlement truth.
    if lock_pending_commercial_orders(cursor, int(customer_user_id)):
        return {"action": "pending_order_preserved"}

    token = "sha256:" + hashlib.sha256(str(invite_code).encode("utf-8")).hexdigest()
    # Registration attribution must never overwrite or dispute an existing
    # commercial decision. The subject advisory lock protects both the absent
    # row and the existing row until this transaction commits.
    lock_commercial_binding_subject(cursor, int(customer_user_id))
    # A quote writer may have committed while this transaction waited for the
    # absent-row advisory lock. Recheck under the shared customer fence.
    if lock_pending_commercial_orders(cursor, int(customer_user_id)):
        return {"action": "pending_order_preserved"}
    cursor.execute(
        """SELECT agent_user_id FROM customer_agent_bindings
           WHERE customer_user_id=%s FOR UPDATE""",
        (int(customer_user_id),),
    )
    existing = _as_dict(cursor.fetchone())
    if existing:
        if int(existing.get("agent_user_id") or 0) == int(inviter_user_id):
            return {"action": "noop"}
        return {"action": "existing_binding_preserved"}

    from services.customer_binding import upsert_customer_agent_binding

    result = upsert_customer_agent_binding(
        cursor,
        int(customer_user_id),
        int(inviter_user_id),
        "invite_code",
        token,
    )
    return {"action": str(result["action"])}


def public_relationship_http_error(exc: RelationshipError) -> tuple[int, Dict[str, str]]:
    if isinstance(exc, RelationshipConflict):
        return 409, {
            "code": "ACCOUNT_CONFIGURATION_REVIEW_REQUIRED",
            "message": "当前账户配置需要平台处理，请稍后重试",
        }
    return 503, {
        "code": "ACCOUNT_CONFIGURATION_UNAVAILABLE",
        "message": "当前账户价格配置暂不可用，请稍后重试",
    }
