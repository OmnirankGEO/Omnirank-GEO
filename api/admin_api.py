"""
RBAC 管理员 API
用户管理、角色管理、审计日志查询

所有端点需要 users:admin 或 is_admin 权限
"""

from fastapi import APIRouter, Request, Response, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime
from decimal import Decimal

from db.auth_db import (
    # 用户
    create_user, get_user, update_user,
    deactivate_user,
    set_user_roles,
    # 角色
    create_role, get_role, list_roles, update_role, delete_role,
    # 审计
    create_audit_log, list_audit_logs, get_audit_log,
    # 元数据
    get_all_modules,
)
from auth.perm_cache import invalidate_cache
from services.admin_client_scope import (
    AdminClientScopeError,
    get_admin_client_scope,
    replace_admin_client_scope,
)
from services.organization_contract import OrganizationError, require_governance_audit_access
from config.settings_manager import load_settings, save_settings, ConfirmCodeEntry

import json
import logging
import uuid
from auth.user_ctx import current_user_id
logger = logging.getLogger("GEO-Admin-API")

router = APIRouter(prefix="/api/admin", tags=["管理员"])


# ========== 请求模型 ==========

class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=50)
    password: str = Field(..., min_length=6, max_length=128)
    display_name: str = Field(..., min_length=1, max_length=100)
    role_ids: List[int] = Field(default=[])
    client_brand_ids: List[int] = Field(default=[])


class UpdateUserRequest(BaseModel):
    display_name: Optional[str] = None
    is_active: Optional[int] = None
    avatar_url: Optional[str] = None


class SetUserRolesRequest(BaseModel):
    role_ids: List[int]


class SetUserClientsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_kind: Literal[
        "legacy_unrestricted", "legacy_selected", "organization_assigned"
    ]
    expected_scope_kind: Literal[
        "legacy_unrestricted", "legacy_selected", "organization_assigned"
    ]
    expected_version: int = Field(..., ge=0)
    etag: str = Field(..., min_length=32, max_length=128)
    brand_ids: List[int] = Field(default_factory=list, max_length=10000)
    request_id: str = Field(..., min_length=8, max_length=128)
    reason: str = Field(..., min_length=1, max_length=500)


class CreateRoleRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=50)
    display_name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="")
    permissions: List[List[str]] = Field(default=[])  # [["writing","read"], ["social","write"]]


class UpdateRoleRequest(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None
    permissions: Optional[List[List[str]]] = None


class AdminSetAgentLevelRequest(BaseModel):
    level: int = Field(..., ge=0, le=2, description="代理等级: 0/1/2")
    reason: str = Field(..., min_length=1, max_length=200, description="操作原因")


class SetQuoteMarkupRequest(BaseModel):
    quote_markup_ratio: float = Field(..., ge=1.0, le=5.0, description="报价系数 [1.0, 5.0]")
    reason: str = Field(..., min_length=1, max_length=200, description="操作原因")


class SetPurchasePricingOverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_catalog_version: str = Field(..., pattern=r"^agent-purchase-v[1-9][0-9]*$")
    wholesale_numer: int = Field(..., gt=0, description="进货系数分子(进货折扣·admin only)")
    wholesale_denom: int = Field(..., gt=0, description="进货系数分母")
    reason: str = Field(..., min_length=1, max_length=200, description="操作原因")


class RenewChannelTierGrantRequest(BaseModel):
    extra_months: int = Field(default=12, ge=1, le=120, description="续期月份")
    owner_type: Optional[str] = Field(default=None, pattern="^(agent|customer)$", description="可选归属校验")
    owner_id: Optional[int] = Field(default=None, ge=1, description="可选归属校验")


class _StrictAdminModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChannelTierRuleRequest(_StrictAdminModel):
    min_yuan: Decimal = Field(..., ge=0)
    bonus_rate: Decimal = Field(..., ge=0, le=1)
    is_enabled: bool = True
    description: str = Field(..., min_length=1, max_length=120)


class ChannelTierRulesRequest(_StrictAdminModel):
    certified: ChannelTierRuleRequest
    preferred: ChannelTierRuleRequest
    strategic: ChannelTierRuleRequest


class FoundingConfigRequest(_StrictAdminModel):
    cap: int = Field(..., ge=0)
    first_order_extra_bonus: Decimal = Field(..., ge=0, le=1)
    min_first_order_yuan: Decimal = Field(..., ge=0)


class MarginThresholdsRequest(_StrictAdminModel):
    loss_heavy_bps: int
    loss_light_bps: int
    healthy_bps: int
    profit_excellent_bps: int
    hard_block_bps: int


class ChannelTierConfigRequest(_StrictAdminModel):
    expected_catalog_version: str = Field(..., pattern=r"^agent-purchase-v[1-9][0-9]*$")
    agent_tier_config: Optional[ChannelTierRulesRequest] = None
    founding: Optional[FoundingConfigRequest] = None
    bonus_validity_months: Optional[int] = Field(None, ge=1, le=120)
    k_default: Optional[Decimal] = Field(None, ge=Decimal("0.1"), le=Decimal("20"))
    margin_label_thresholds: Optional[MarginThresholdsRequest] = None


class SetChannelTierOverrideRequest(BaseModel):
    tier: Optional[str] = Field(default=None, pattern="^(certified|preferred|strategic)$")
    clear: bool = False
    until: Optional[datetime] = None
    note: Optional[str] = Field(default=None, max_length=300)


# ========== 工具函数 ==========

def _require_admin(request: Request) -> dict:
    """要求管理员权限"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _require_governance_audit(request: Request) -> dict:
    user = _require_admin(request)
    try:
        require_governance_audit_access(is_platform_admin=bool(user.get("is_admin")))
    except OrganizationError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.as_detail("admin-audit")) from exc
    return user


def _get_client_ip(request: Request) -> str:
    """获取客户端 IP"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _mask_phone(phone: Optional[str]) -> str:
    """手机号脱敏: 138****2688"""
    if not phone or len(phone) < 7:
        return phone or ""
    return phone[:3] + "****" + phone[-4:]


# ==========================================
# 公开报告 v3 narrative 管理
# ==========================================

@router.get("/report-v3/narrative/status", summary="公开报告 v3 narrative 队列状态")
async def admin_report_v3_narrative_status(request: Request):
    _require_admin(request)
    from db.connection import get_connection
    from services.narrative_budget import get_monthly_budget_snapshot

    settings = load_settings()
    budget = get_monthly_budget_snapshot()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT status, COUNT(*) AS count
            FROM narrative_enrichment_locks
            GROUP BY status
        """)
        status_counts = {row["status"]: int(row["count"]) for row in (cursor.fetchall() or [])}
        cursor.execute("""
            SELECT COUNT(*) AS count
            FROM narrative_enrichment_locks
            WHERE status = 'running'
              AND last_heartbeat_at < NOW() - INTERVAL '10 minutes'
        """)
        stuck_row = cursor.fetchone() or {}
        cursor.execute("""
            SELECT AVG(EXTRACT(EPOCH FROM (completed_at - created_at))) AS avg_seconds
            FROM narrative_enrichment_locks
            WHERE completed_at IS NOT NULL
        """)
        avg_row = cursor.fetchone() or {}
    finally:
        conn.close()

    monthly_limit = float(getattr(settings, "llm_narrative_monthly_yuan", 100.0) or 0)
    alert_line = float(getattr(settings, "llm_narrative_alert_yuan", 80.0) or 0)
    spent = float(budget.get("total_yuan") or 0)
    return {
        "success": True,
        "settings": {
            "report_v3_enabled": bool(getattr(settings, "report_v3_enabled", False)),
            "report_v3_auto_enrich": bool(getattr(settings, "report_v3_auto_enrich", False)),
            "llm_narrative_monthly_yuan": monthly_limit,
            "llm_narrative_alert_yuan": alert_line,
            "llm_narrative_max_concurrent": int(getattr(settings, "llm_narrative_max_concurrent", 3) or 3),
        },
        "budget": {
            **budget,
            "limit_yuan": monthly_limit,
            "alert_yuan": alert_line,
            "alerting": bool(alert_line and spent >= alert_line),
            "blocked": bool(monthly_limit and spent >= monthly_limit),
        },
        "queue": {
            "status_counts": status_counts,
            "stuck_running": int(stuck_row.get("count") or 0),
            "avg_seconds": float(avg_row.get("avg_seconds") or 0),
        },
    }


@router.post("/report-v3/narrative/backfill", summary="批量补排公开报告 v3 narrative")
async def admin_report_v3_narrative_backfill(request: Request, limit: int = 20):
    _require_admin(request)
    from db.connection import get_connection

    limit = max(1, min(int(limit or 20), 100))
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT dr.id
            FROM diagnosis_records dr
            LEFT JOIN narrative_enrichment_locks nel ON nel.diagnosis_id = dr.id
            WHERE dr.report_v2_version = 'v2'
              AND dr.report_v2_modules_jsonb IS NOT NULL
              AND (nel.status IS NULL OR nel.status = 'failed_full')
            ORDER BY dr.created_at DESC
            LIMIT %s
        """, (limit,))
        rows = cursor.fetchall() or []
        ids = [int(row["id"]) for row in rows]
        for diagnosis_id in ids:
            cursor.execute("""
                INSERT INTO narrative_enrichment_locks (diagnosis_id, status, retries, created_at, updated_at)
                VALUES (%s, 'pending', 0, NOW(), NOW())
                ON CONFLICT (diagnosis_id) DO UPDATE SET
                    status = CASE
                        WHEN narrative_enrichment_locks.status IN ('succeeded', 'succeeded_partial')
                        THEN narrative_enrichment_locks.status
                        ELSE 'pending'
                    END,
                    updated_at = NOW()
            """, (diagnosis_id,))
        conn.commit()
    finally:
        conn.close()

    return {"success": True, "enqueued": ids, "count": len(ids)}


@router.post("/report-v3/narrative/run-once", summary="手动运行一次公开报告 v3 narrative 队列")
async def admin_report_v3_narrative_run_once(request: Request, limit: int = 3):
    _require_admin(request)
    from services.narrative_job_runner import run_pending_once

    results = await run_pending_once(limit=max(1, min(int(limit or 3), 10)))
    return {"success": True, "results": results}


# ==========================================
# 在线看板
# ==========================================

@router.get("/online-users", summary="获取在线用户列表（近1分钟活跃）")
async def admin_online_users(request: Request):
    """
    返回近 1 分钟活跃用户 + 每人最近 10 分钟的最后一条 audit_log。
    数据来源：users.last_active_at / users.current_path / audit_logs
    用于 TV 大屏在线看板。
    """
    _require_admin(request)

    from db.connection import get_connection
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.id, u.username, u.display_name, u.phone,
                   u.last_active_at, u.current_path,
                   (SELECT json_build_object(
                               'action', action,
                               'module', module,
                               'summary', summary,
                               'entity_type', entity_type,
                               'created_at', created_at
                           )
                    FROM audit_logs
                    WHERE user_id = u.id
                      AND created_at > NOW() - INTERVAL '10 minutes'
                    ORDER BY created_at DESC
                    LIMIT 1) AS last_action
            FROM users u
            WHERE u.last_active_at > NOW() - INTERVAL '1 minute'
              AND u.is_active = 1
            ORDER BY u.last_active_at DESC
            LIMIT 50
        """)
        rows = cursor.fetchall()
        cursor.close()
    finally:
        conn.close()

    users = []
    for r in rows:
        phone_masked = _mask_phone(r.get("phone")) if r.get("phone") else ""
        name = r.get("display_name") or phone_masked or r.get("username") or f"用户{r['id']}"
        users.append({
            "id": r["id"],
            "name": name,
            "phone_masked": phone_masked,
            "current_path": r.get("current_path") or "",
            "last_active_at": r["last_active_at"].isoformat() if r.get("last_active_at") else None,
            "last_action": r.get("last_action"),
        })

    return {
        "online_count": len(users),
        "users": users,
    }


# ==========================================
# 用户管理
# ==========================================

@router.get("/users")
async def admin_list_users(
    request: Request,
    # 搜索
    q: Optional[str] = None,
    # 筛选
    status: Optional[str] = None,           # active / inactive / all
    agent_level: Optional[int] = None,      # 0 / 1 / 2(精确匹配)
    agent_level_min: Optional[int] = None,  # >= 该值(服务商候选下拉用 agent_level_min=1 · 含 L2+)
    role_id: Optional[int] = None,
    register_source: Optional[str] = None,  # direct / referral
    province: Optional[str] = None,
    city: Optional[str] = None,
    gender: Optional[str] = None,           # male / female / unknown
    has_paid: Optional[bool] = None,        # True=已充值, False=未充值
    activity: Optional[str] = None,         # active_7d / active_30d / inactive_30d / never
    created: Optional[str] = None,          # today / week / month
    tag_id: Optional[int] = None,
    # 分页
    page: int = 1,
    page_size: int = 50,
    # 排序
    sort_by: Optional[str] = "created_at",  # created_at / last_login_at / total_points / total_recharged / last_active_at
    sort_dir: Optional[str] = "desc",       # asc / desc
    # 兼容旧调用
    include_inactive: bool = False,
):
    """用户列表 — 分页 + 搜索 + 多维筛选 + 排序"""
    _require_admin(request)

    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()

        # ====== 动态构建 WHERE ======
        wheres = []
        params = []

        # 状态
        if status == "active":
            wheres.append("u.is_active = 1")
        elif status == "inactive":
            wheres.append("u.is_active = 0")
        elif not include_inactive and status != "all":
            # 默认兼容旧行为
            pass  # 不加过滤，返回全部

        # 搜索 — 多字段 ILIKE
        if q and q.strip():
            search = f"%{q.strip()}%"
            wheres.append("""(
                u.username ILIKE %s OR u.display_name ILIKE %s
                OR u.phone ILIKE %s OR u.email ILIKE %s
                OR u.real_name ILIKE %s OR u.company ILIKE %s
                OR u.wechat_id ILIKE %s
                OR CAST(u.id AS TEXT) = %s
            )""")
            params.extend([search, search, search, search, search, search, search, q.strip()])

        # 代理等级
        if agent_level is not None:
            wheres.append("COALESCE(w.agent_level, 0) = %s")
            params.append(agent_level)
        if agent_level_min is not None:
            wheres.append("COALESCE(w.agent_level, 0) >= %s")
            params.append(agent_level_min)

        # 角色
        if role_id is not None:
            wheres.append("EXISTS (SELECT 1 FROM user_roles ur2 WHERE ur2.user_id = u.id AND ur2.role_id = %s)")
            params.append(role_id)

        # 注册来源
        if register_source:
            wheres.append("COALESCE(u.register_source, 'direct') = %s")
            params.append(register_source)

        # 地区
        if province:
            wheres.append("u.register_province ILIKE %s")
            params.append(f"%{province}%")
        if city:
            wheres.append("(u.city ILIKE %s OR u.register_city ILIKE %s)")
            params.extend([f"%{city}%", f"%{city}%"])

        # 性别
        if gender:
            wheres.append("COALESCE(u.gender, 'unknown') = %s")
            params.append(gender)

        # 付费状态
        if has_paid is True:
            wheres.append("COALESCE(w.total_recharged, 0) > 0")
        elif has_paid is False:
            wheres.append("COALESCE(w.total_recharged, 0) = 0")

        # 活跃度
        if activity == "active_7d":
            wheres.append("COALESCE(u.last_active_at, u.last_login_at) >= CURRENT_TIMESTAMP - INTERVAL '7 days'")
        elif activity == "active_30d":
            wheres.append("COALESCE(u.last_active_at, u.last_login_at) >= CURRENT_TIMESTAMP - INTERVAL '30 days'")
        elif activity == "inactive_30d":
            wheres.append("COALESCE(u.last_active_at, u.last_login_at) < CURRENT_TIMESTAMP - INTERVAL '30 days'")
        elif activity == "never":
            wheres.append("u.last_login_at IS NULL")

        # 注册时间快捷筛选(运营控制台下钻用)
        if created == "today":
            wheres.append("u.created_at >= CURRENT_DATE")
        elif created == "week":
            wheres.append("u.created_at >= CURRENT_DATE - INTERVAL '7 days'")
        elif created == "month":
            wheres.append("u.created_at >= DATE_TRUNC('month', CURRENT_DATE)")

        # 标签
        if tag_id is not None:
            wheres.append("EXISTS (SELECT 1 FROM user_tag_assignments uta WHERE uta.user_id = u.id AND uta.tag_id = %s)")
            params.append(tag_id)

        where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""

        # ====== 排序 ======
        SORT_MAP = {
            "created_at": "u.created_at",
            "last_login_at": "u.last_login_at",
            "last_active_at": "COALESCE(u.last_active_at, u.last_login_at)",
            "total_points": "(COALESCE(w.paid_points, 0) + COALESCE(w.bonus_points, 0))",
            "total_recharged": "COALESCE(w.total_recharged, 0)",
            "agent_level": "COALESCE(w.agent_level, 0)",
        }
        sort_col = SORT_MAP.get(sort_by, "u.created_at")
        direction = "ASC" if sort_dir == "asc" else "DESC"
        order_sql = f"ORDER BY {sort_col} {direction} NULLS LAST"

        # ====== 分页 ======
        page_size = min(max(page_size, 10), 200)
        offset = (max(page, 1) - 1) * page_size

        # ====== 主查询 ======
        base_sql = f"""
            FROM users u
            LEFT JOIN user_wallets w ON w.user_id = u.id
            {where_sql}
        """

        # 总数
        cur.execute(f"SELECT COUNT(*) as cnt {base_sql}", params)
        total = cur.fetchone()["cnt"]

        # 数据
        cur.execute(f"""
            SELECT u.id, u.username, u.display_name, u.is_active,
                   u.must_change_password, u.avatar_url,
                   u.real_name, u.email, u.phone, u.wechat_id,
                   u.company, u.job_title, u.industry, u.city, u.gender, u.bio,
                   u.register_ip, u.register_city, u.register_province, u.register_source,
                   u.extension_authorized, u.profile_completed_at,
                   u.created_at, u.last_login_at, u.last_active_at,
                   COALESCE(w.paid_points, 0) as paid_points,
                   COALESCE(w.bonus_points, 0) as bonus_points,
                   COALESCE(w.paid_points, 0) + COALESCE(w.bonus_points, 0) as total_points,
                   COALESCE(w.agent_level, 0) as agent_level,
                   COALESCE(w.total_recharged, 0) as total_recharged
            {base_sql}
            {order_sql}
            LIMIT %s OFFSET %s
        """, params + [page_size, offset])
        rows = [dict(r) for r in cur.fetchall()]

        # 附加角色 + 客户 + 推荐码 + 标签
        user_ids = [r["id"] for r in rows]
        if user_ids:
            # 角色
            cur.execute("""
                SELECT ur.user_id, r.id, r.name, r.display_name
                FROM user_roles ur JOIN roles r ON r.id = ur.role_id
                WHERE ur.user_id = ANY(%s)
            """, (user_ids,))
            role_map: dict = {}
            for r in cur.fetchall():
                role_map.setdefault(r["user_id"], []).append({"id": r["id"], "name": r["name"], "display_name": r["display_name"]})

            # 客户
            cur.execute("SELECT user_id, brand_id FROM user_clients WHERE user_id = ANY(%s)", (user_ids,))
            client_map: dict = {}
            for r in cur.fetchall():
                client_map.setdefault(r["user_id"], []).append(r["brand_id"])

            # 推荐码
            ref_codes: dict = {}
            try:
                cur.execute("SELECT user_id, code FROM referral_codes WHERE user_id = ANY(%s)", (user_ids,))
                for r in cur.fetchall():
                    ref_codes[r["user_id"]] = r["code"]
            except Exception:
                pass

            # 标签
            tag_map: dict = {}
            try:
                cur.execute("""
                    SELECT uta.user_id, t.id, t.name, t.color
                    FROM user_tag_assignments uta JOIN user_tags t ON t.id = uta.tag_id
                    WHERE uta.user_id = ANY(%s)
                """, (user_ids,))
                for r in cur.fetchall():
                    tag_map.setdefault(r["user_id"], []).append({"id": r["id"], "name": r["name"], "color": r["color"]})
            except Exception:
                pass

            for u in rows:
                uid = u["id"]
                u.pop("password_hash", None)
                u["roles"] = role_map.get(uid, [])
                u["is_admin"] = any(r["name"] == "admin" for r in u["roles"])
                u["client_brand_ids"] = client_map.get(uid, [])
                u["client_count"] = len(u["client_brand_ids"])
                u["referral_code"] = ref_codes.get(uid, "")
                u["tags"] = tag_map.get(uid, [])

        # ====== 筛选器元数据（首次加载用） ======
        # 省份列表 + 标签列表 供前端下拉
        meta = {}
        try:
            cur.execute("SELECT DISTINCT register_province FROM users WHERE register_province IS NOT NULL AND register_province != '' ORDER BY register_province")
            meta["provinces"] = [r["register_province"] for r in cur.fetchall()]
            cur.execute("SELECT id, name, color FROM user_tags ORDER BY name")
            meta["tags"] = [dict(r) for r in cur.fetchall()]
        except Exception:
            meta["provinces"] = []
            meta["tags"] = []

        return {
            "success": True,
            "users": rows,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
            "meta": meta,
        }
    finally:
        conn.close()


@router.post("/users")
async def admin_create_user(req: CreateUserRequest, request: Request):
    """创建新用户"""
    admin = _require_admin(request)

    # 新用户不再由运营选择任何 RBAC 角色。social_ops 仅作为“普通用户”鉴权兼容底座；
    # 内部管理员必须在用户创建后通过独立的 platform-access CAS 接口授予并留下完整证据。
    if req.role_ids:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ROLE_ASSIGNMENT_REQUIRES_GOVERNANCE_API",
                "message": "新用户只创建为普通用户；管理员权限请在创建后单独治理",
            },
        )
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM roles WHERE name='social_ops'")
        compatibility_role = cur.fetchone()
        if not compatibility_role:
            raise HTTPException(status_code=500, detail="普通用户兼容权限模板缺失")
        effective_role_ids = [int(compatibility_role["id"])]
    finally:
        conn.close()

    user_id = create_user(
        username=req.username,
        password=req.password,
        display_name=req.display_name,
        role_ids=effective_role_ids,
        client_brand_ids=req.client_brand_ids,
        must_change_password=1  # 新用户首登必须改密
    )

    if not user_id:
        return {"success": False, "error": f"用户名 '{req.username}' 已存在"}

    # 🔴 [#139 · 2026-09-07] admin 建号也要建钱包行,与注册路径
    #    (`api/auth_api.py:549`)同形。以前只有自助注册那条路建 ——
    #    admin 建出来的账号没有 `user_wallets` 行,
    #    `admin_user_governance._assert_identity_ssot` 全表扫描一命中就 raise,
    #    **整个管理端用户列表拒读**(Owner 2026-09-07 报的现象)。
    #    `create_user` 已提交,此处是提交后调用,`get_or_create_wallet` 幂等。
    try:
        from db.wallet_db import get_or_create_wallet

        get_or_create_wallet(int(user_id))
    except Exception as exc:      # noqa: BLE001
        # 不阻断建号(人已经建出来了),但**不静默** —— 打 ERROR 并说清后果。
        # 残留由列表侧的按行降级兜住(该行标 unverified + attention)。
        logger.error(
            "[#139] admin 建号后建钱包行失败 user=%s —— 该账号在管理端会显示为"
            "「尚未初始化钱包」,本人登录一次即自动补齐:%s", user_id, exc)

    # 审计日志
    create_audit_log(
        user_id=admin["user_id"],
        username=admin["username"],
        action="create_user",
        module="users",
        entity_type="user",
        entity_id=user_id,
        summary=f"创建用户: {req.username} ({req.display_name})",
        after={"username": req.username, "display_name": req.display_name,
               "role_ids": effective_role_ids, "client_brand_ids": req.client_brand_ids},
        ip_address=_get_client_ip(request)
    )

    user = get_user(user_id)
    logger.info(f"用户创建: {req.username} by {admin['username']}")
    return {"success": True, "user": user}


# [GEO-R1-CAN-047] :int 路径转换器,防止静态路由 /users/export 被此动态路由抢匹配(422)
@router.get("/users/{user_id:int}")
async def admin_get_user(user_id: int, request: Request):
    """获取用户详情"""
    _require_admin(request)
    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"success": True, "user": user}


@router.get("/users/{user_id}/detail")
async def admin_get_user_detail(user_id: int, request: Request):
    """管理员查看用户完整画像（财务+推荐链+使用统计+画像+品牌）"""
    _require_admin(request)
    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    from db.connection import get_connection
    import json
    conn = get_connection()
    cur = conn.cursor()
    result = {"user": user}

    try:
        # 财务数据
        cur.execute("SELECT * FROM user_wallets WHERE user_id = %s", (user_id,))
        wallet = cur.fetchone()
        cur.execute("""
            SELECT * FROM recharge_orders WHERE user_id = %s ORDER BY created_at DESC LIMIT 20
        """, (user_id,))
        recharge_orders = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT * FROM point_transactions WHERE user_id = %s ORDER BY created_at DESC LIMIT 30
        """, (user_id,))
        transactions = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT SUM(ABS(amount)) as consumed FROM point_transactions
            WHERE user_id = %s AND type = 'consume'
            AND created_at >= DATE_TRUNC('month', CURRENT_DATE)
        """, (user_id,))
        month_consumed = cur.fetchone()

        result["finance"] = {
            "wallet": dict(wallet) if wallet else {},
            "recharge_orders": recharge_orders,
            "recent_transactions": transactions,
            "month_consumed": month_consumed["consumed"] if month_consumed else 0,
        }

        # 推荐链
        cur.execute("""
            SELECT rl.referred_id, rl.level, rl.commission_rate, rl.created_at,
                   u.display_name, u.username,
                   COALESCE(w.total_recharged, 0) as recharged
            FROM referral_links rl
            JOIN users u ON rl.referred_id = u.id
            LEFT JOIN user_wallets w ON w.user_id = u.id
            WHERE rl.referrer_id = %s
            ORDER BY rl.level, rl.created_at DESC
        """, (user_id,))
        refs = [dict(r) for r in cur.fetchall()]
        direct = [r for r in refs if r["level"] == 1]
        indirect = [r for r in refs if r["level"] == 2]

        # 佣金总额
        cur.execute("""
            SELECT COALESCE(SUM(amount), 0) as total
            FROM point_transactions
            WHERE user_id = %s AND type IN ('commission', 'bonus')
            AND description LIKE '%%佣金%%'
        """, (user_id,))
        commission = cur.fetchone()

        result["referrals"] = {
            "direct": direct,
            "indirect": indirect,
            "total_commission_points": commission["total"] if commission else 0,
            "chain_depth": max([r["level"] for r in refs], default=0),
        }

        # 使用统计
        cur.execute("""
            SELECT feature_code, COUNT(*) as count, SUM(ABS(amount)) as points
            FROM point_transactions
            WHERE user_id = %s AND type = 'consume'
            GROUP BY feature_code
            ORDER BY count DESC LIMIT 15
        """, (user_id,))
        features = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT COUNT(*) as total FROM point_transactions WHERE user_id = %s AND type = 'consume'", (user_id,))
        total_ops = cur.fetchone()

        cur.execute("""
            SELECT EXTRACT(HOUR FROM created_at)::int as hour, COUNT(*) as count
            FROM point_transactions
            WHERE user_id = %s AND created_at >= CURRENT_DATE - INTERVAL '30 days'
            GROUP BY hour ORDER BY count DESC LIMIT 5
        """, (user_id,))
        active_hours = [dict(r) for r in cur.fetchall()]

        # 发布统计（迁到新表）
        cur.execute("""
            SELECT COUNT(*) as total,
                   COUNT(*) FILTER (WHERE poi.status = 'published') as published,
                   COUNT(*) FILTER (WHERE poi.status = 'rejected') as rejected
            FROM mhz_publish_order_items poi
            JOIN mhz_publish_orders po ON poi.order_id = po.id
            WHERE po.user_id = %s
        """, (user_id,))
        pub = cur.fetchone()

        result["usage"] = {
            "total_operations": total_ops["total"] if total_ops else 0,
            "top_features": features,
            "active_hours": active_hours,
            "publish_stats": dict(pub) if pub else {},
        }

        # 创作者画像
        cur.execute("""
            SELECT cp.personality_profile, cp.persona_positioning, cp.persona_tone,
                   cp.business, cp.creator_type
            FROM client_profiles cp
            JOIN brands b ON cp.brand_id = b.id
            JOIN user_clients uc ON uc.brand_id = b.id
            WHERE uc.user_id = %s AND cp.is_deleted = 0
            ORDER BY cp.updated_at DESC LIMIT 1
        """, (user_id,))
        profile = cur.fetchone()
        if profile:
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
                "creator_type": profile.get("creator_type", ""),
            }

        # 品牌列表
        cur.execute("""
            SELECT b.id, b.name, b.industry, b.diagnosis_count, b.latest_score,
                   (SELECT COUNT(*) FROM article_generations ag
                    JOIN diagnosis_records dr ON ag.diagnosis_id = dr.id
                    WHERE dr.brand_name = b.name) as article_count
            FROM brands b
            JOIN user_clients uc ON uc.brand_id = b.id
            WHERE uc.user_id = %s
        """, (user_id,))
        result["brands"] = [dict(r) for r in cur.fetchall()]

        # 团队
        cur.execute("""
            SELECT t.team_name, t.team_code, tm.role,
                   (SELECT COUNT(*) FROM team_members WHERE team_id = t.id AND status = 'active') as member_count
            FROM team_members tm
            JOIN teams t ON tm.team_id = t.id
            WHERE tm.user_id = %s AND tm.status = 'active' LIMIT 1
        """, (user_id,))
        team = cur.fetchone()
        result["team"] = dict(team) if team else None

    except Exception as e:
        logger.warning(f"获取用户详情失败: {e}")
    finally:
        conn.close()

    return {"success": True, **result}


@router.put("/users/{user_id}")
async def admin_update_user(user_id: int, req: UpdateUserRequest, request: Request):
    """修改用户基本信息"""
    admin = _require_admin(request)

    # 获取修改前的状态
    before = get_user(user_id)
    if not before:
        raise HTTPException(status_code=404, detail="用户不存在")

    updates = req.dict(exclude_none=True)
    if not updates:
        return {"success": False, "error": "没有要修改的字段"}

    success = update_user(user_id, **updates)

    # [GEO-R1-CAN-046] 若改动了 is_active(尤其禁用),必须递增 permission_version。
    # 否则中间件在版本匹配时信任 JWT 生命周期内的账号状态(is_active 不在 JWT 中),
    # 通过通用 PUT 编辑路径禁用的用户会话最长 7 天才失效。镜像 deactivate_user 行为。
    if success and 'is_active' in updates:
        from db.connection import get_db
        with get_db() as conn:
            _cur = conn.cursor()
            _cur.execute(
                "UPDATE users SET permission_version = permission_version + 1 WHERE id = %s",
                (user_id,)
            )

    if success:
        # 审计日志
        create_audit_log(
            user_id=admin["user_id"],
            username=admin["username"],
            action="update_user",
            module="users",
            entity_type="user",
            entity_id=user_id,
            summary=f"修改用户: {before['username']}",
            before={k: before.get(k) for k in updates},
            after=updates,
            ip_address=_get_client_ip(request)
        )

    return {"success": success, "user": get_user(user_id)}


@router.delete("/users/{user_id}")
async def admin_delete_user(user_id: int, request: Request):
    """删除用户（不可删除自己和 admin 角色用户）"""
    admin = _require_admin(request)

    # 获取目标用户
    target = get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")

    # 不能删除自己
    if admin["user_id"] == user_id:
        raise HTTPException(status_code=400, detail="不能删除自己的账号")

    # 不能删除 admin 角色用户
    if target.get("is_admin"):
        raise HTTPException(status_code=400, detail="不能删除管理员账号")

    # 执行删除（递归级联清理多级外键引用，再删用户）
    # 根因：admin_delete_user 原只删「直接引用 users 的一级表」且遍历顺序不定；subscription 等系统是
    # 多级嵌套外键 + ON DELETE 动作不一致（CASCADE/NO ACTION/SET NULL 混用），删 user_social_subscriptions
    # 时子表 subscription_usage_events 仍引用 subscription_id → 违反外键删不动。改为递归级联删除。
    from db.connection import get_db
    from psycopg2 import sql

    def _list_child_fks(cursor, parent_schema: str, parent_table: str):
        """返回引用 parent_schema.parent_table(id) 的子表外键。
        完整三元组 join（catalog+schema+name）避免跨 schema 约束重名误匹配；限定 public schema。
        返回 [(child_schema, child_table, child_column, delete_rule_upper), ...]。"""
        cursor.execute("""
            SELECT tc.table_schema AS child_schema,
                   tc.table_name   AS child_table,
                   kcu.column_name AS child_column,
                   rc.delete_rule  AS delete_rule
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON  tc.constraint_catalog = kcu.constraint_catalog
                AND tc.constraint_schema  = kcu.constraint_schema
                AND tc.constraint_name    = kcu.constraint_name
            JOIN information_schema.referential_constraints rc
                ON  tc.constraint_catalog = rc.constraint_catalog
                AND tc.constraint_schema  = rc.constraint_schema
                AND tc.constraint_name    = rc.constraint_name
            JOIN information_schema.constraint_column_usage ccu
                ON  rc.unique_constraint_catalog = ccu.constraint_catalog
                AND rc.unique_constraint_schema  = ccu.constraint_schema
                AND rc.unique_constraint_name    = ccu.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = 'public'
              AND ccu.table_schema = %s
              AND ccu.table_name = %s
              AND ccu.column_name = 'id'
        """, (parent_schema, parent_table))
        return [(r['child_schema'], r['child_table'], r['child_column'], (r['delete_rule'] or '').upper())
                for r in cursor.fetchall()]

    def _delete_cascade(cursor, schema: str, table: str, column: str, value, counts: dict, depth: int = 0):
        """删除 schema.table 中 column=value 的行；先递归清空引用这些行(其 id)的子表，再删自己。
        - SET NULL/SET DEFAULT 子表跳过（DB 自动置空，如被删用户是别人推荐人→对方记录保留）；
        - CASCADE/NO ACTION/RESTRICT 子表手动递归删（穿透 CASCADE 层清深层 RESTRICT 孙表）；
        - 自引用 FK：SET NULL/SET DEFAULT 跳过；其余 raise（不静默跳过）。当前 schema 唯一自引用
          geo_research_articles.primary_article_id 为 SET NULL，正常不命中；此防御针对未来若出现
          RESTRICT/NO ACTION 自引用时层级数据被静默漏删。
        标识符全部走 psycopg2.sql.Identifier，不用 f-string 拼接表名/列名。"""
        if depth > 20:
            raise RuntimeError(f"外键级联过深(疑似循环引用): {schema}.{table}")
        recurse_children = []
        for (cs, ct, cc, rule) in _list_child_fks(cursor, schema, table):
            if rule in ('SET NULL', 'SET DEFAULT'):
                continue
            if cs == schema and ct == table:
                raise RuntimeError(
                    f"检测到未处理的自引用外键 {cs}.{ct}.{cc}(delete_rule={rule})，需人工确认删除策略")
            recurse_children.append((cs, ct, cc))
        if recurse_children:
            cursor.execute(
                sql.SQL("SELECT id FROM {}.{} WHERE {} = %s").format(
                    sql.Identifier(schema), sql.Identifier(table), sql.Identifier(column)),
                (value,))
            ids = [r['id'] for r in cursor.fetchall()]
            for (cs, ct, cc) in recurse_children:
                for _id in ids:
                    _delete_cascade(cursor, cs, ct, cc, _id, counts, depth + 1)
        cursor.execute(
            sql.SQL("DELETE FROM {}.{} WHERE {} = %s").format(
                sql.Identifier(schema), sql.Identifier(table), sql.Identifier(column)),
            (value,))
        affected = cursor.rowcount or 0
        if affected:
            key = f"{schema}.{table}"
            counts[key] = counts.get(key, 0) + affected
            logger.info(
                f"[delete_user cascade] depth={depth} {schema}.{table}.{column}={value} -> {affected} rows")

    deleted_counts: dict = {}
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            # 所有直接引用 users(id) 的外键表，逐个递归级联删除
            for (cs, ct, cc, rule) in _list_child_fks(cursor, 'public', 'users'):
                # SET NULL/SET DEFAULT（如推荐人关系）交给 DB 自动置空，不删对方数据
                if rule in ('SET NULL', 'SET DEFAULT'):
                    continue
                _delete_cascade(cursor, cs, ct, cc, user_id, deleted_counts)
            cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
            affected_users = cursor.rowcount or 0
            deleted_counts['public.users'] = affected_users
            success = affected_users > 0
    except Exception as e:
        logger.error(f"[delete_user] user_id={user_id} 级联删除失败: {e}")
        raise HTTPException(status_code=500, detail=f"删除失败: {e}")

    logger.info(f"[delete_user] user_id={user_id} 完成 success={success} deleted_counts={deleted_counts}")

    if success:
        # 失效权限缓存
        invalidate_cache(user_id)

        # 审计日志（含级联删除统计 deleted_counts，便于事后核对清理了哪些关联数据）
        create_audit_log(
            user_id=admin["user_id"],
            username=admin["username"],
            action="delete_user",
            module="users",
            entity_type="user",
            entity_id=user_id,
            summary=f"删除用户: {target['username']} ({target['display_name']}) · 级联清理 {sum(deleted_counts.values())} 行 / {len(deleted_counts)} 表",
            before={"username": target["username"], "display_name": target["display_name"]},
            after={"deleted_counts": deleted_counts},
            ip_address=_get_client_ip(request)
        )

    return {"success": success, "deleted_counts": deleted_counts}


@router.put("/users/{user_id}/roles")
async def admin_set_user_roles(user_id: int, req: SetUserRolesRequest, request: Request):
    """历史角色写入口已停用；业务身份与平台权限使用独立 CAS API。"""
    _require_admin(request)
    raise HTTPException(
        status_code=410,
        detail={
            "code": "LEGACY_ROLE_WRITER_RETIRED",
            "message": "历史角色仅保留只读兼容，不能再分配",
            "migrate_to": "/api/admin/user-governance/users/{user_id}/platform-access",
        },
    )

@router.get("/users/{user_id}/clients")
async def admin_get_user_clients(user_id: int, request: Request, response: Response):
    """Return an explicit legacy/organization client-scope contract."""
    _require_admin(request)
    try:
        result = get_admin_client_scope(user_id)
    except AdminClientScopeError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.as_detail()) from exc
    response.headers["ETag"] = f'"{result["etag"]}"'
    return result


@router.put("/users/{user_id}/clients")
async def admin_set_user_clients(
    user_id: int,
    req: SetUserClientsRequest,
    request: Request,
    response: Response,
):
    """CAS-update client access without changing owner, payer, or referral."""
    admin = _require_admin(request)
    before_user = get_user(user_id)
    if not before_user:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "用户不存在"})
    try:
        before = get_admin_client_scope(user_id)
        result = replace_admin_client_scope(
            admin_user_id=int(admin["user_id"]),
            target_user_id=user_id,
            scope_kind=req.scope_kind,
            expected_scope_kind=req.expected_scope_kind,
            expected_version=req.expected_version,
            expected_etag=req.etag,
            brand_ids=req.brand_ids,
            request_id=req.request_id,
            reason=req.reason,
        )
    except AdminClientScopeError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.as_detail()) from exc
    except OrganizationError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.as_detail(req.request_id)) from exc

    invalidate_cache(user_id)
    response.headers["ETag"] = f'"{result["etag"]}"'
    create_audit_log(
        user_id=admin["user_id"],
        username=admin["username"],
        action="set_user_client_scope",
        module="users",
        entity_type="user",
        entity_id=user_id,
        summary=f"修改客户访问范围: {before_user['username']}",
        before={
            "scope_kind": before["scope_kind"],
            "brand_ids": before["brand_ids"],
            "version": before["version"],
        },
        after={
            "scope_kind": result["scope_kind"],
            "brand_ids": result["brand_ids"],
            "version": result["version"],
            "request_id": req.request_id,
            "reason": req.reason,
        },
        ip_address=_get_client_ip(request),
    )
    return result


@router.post("/users/{user_id}/reset-password")
async def admin_reset_password(user_id: int, request: Request):
    """Retired non-atomic endpoint; use versioned governance instead."""
    _require_admin(request)
    raise HTTPException(
        status_code=410,
        detail={
            "code": "PASSWORD_RESET_ENDPOINT_RETIRED",
            "message": "请在用户管理的密码与账号安全中重置密码",
            "migrate_to": f"/api/admin/user-governance/users/{int(user_id)}/password",
        },
    )


# ==========================================
# 插件授权管理 —— [WO_273 · 2026-09-23] 已退役
# ==========================================
# 原 POST /users/{user_id}/extension-auth(切换 users.extension_authorized*)随浏览器插件后端整体删除。
# 列与存量数据不动;用户列表 / 详情响应里照旧带 extension_authorized(只读,前端不再消费)。


# ==========================================
# 角色管理
# ==========================================

@router.get("/roles")
async def admin_list_roles(request: Request):
    """获取所有角色列表"""
    _require_admin(request)
    roles = list_roles()
    return {"success": True, "roles": roles, "total": len(roles)}


@router.post("/roles")
async def admin_create_role(req: CreateRoleRequest, request: Request):
    """创建新角色"""
    admin = _require_admin(request)

    permissions = [(p[0], p[1]) for p in req.permissions if len(p) == 2]
    role_id = create_role(
        name=req.name,
        display_name=req.display_name,
        description=req.description,
        permissions=permissions
    )

    if not role_id:
        return {"success": False, "error": f"角色名 '{req.name}' 已存在"}

    create_audit_log(
        user_id=admin["user_id"],
        username=admin["username"],
        action="create_role",
        module="users",
        entity_type="role",
        entity_id=role_id,
        summary=f"创建角色: {req.display_name}",
        after={"name": req.name, "display_name": req.display_name,
               "permissions": req.permissions},
        ip_address=_get_client_ip(request)
    )

    role = get_role(role_id)
    logger.info(f"角色创建: {req.name} by {admin['username']}")
    return {"success": True, "role": role}


@router.get("/roles/{role_id}")
async def admin_get_role(role_id: int, request: Request):
    """获取角色详情"""
    _require_admin(request)
    role = get_role(role_id)
    if not role:
        raise HTTPException(status_code=404, detail="角色不存在")
    return {"success": True, "role": role}


@router.put("/roles/{role_id}")
async def admin_update_role(role_id: int, req: UpdateRoleRequest, request: Request):
    """修改角色（含权限矩阵）"""
    admin = _require_admin(request)

    before = get_role(role_id)
    if not before:
        raise HTTPException(status_code=404, detail="角色不存在")

    permissions = None
    if req.permissions is not None:
        permissions = [(p[0], p[1]) for p in req.permissions if len(p) == 2]

    success = update_role(
        role_id,
        display_name=req.display_name,
        description=req.description,
        permissions=permissions
    )

    if success:
        create_audit_log(
            user_id=admin["user_id"],
            username=admin["username"],
            action="update_role",
            module="users",
            entity_type="role",
            entity_id=role_id,
            summary=f"修改角色: {before['display_name']}",
            before={"permissions": before.get("permission_strings")},
            after={"permissions": req.permissions} if req.permissions else None,
            ip_address=_get_client_ip(request)
        )

    return {"success": success, "role": get_role(role_id)}


@router.delete("/roles/{role_id}")
async def admin_delete_role(role_id: int, request: Request):
    """删除角色（系统角色不可删）"""
    admin = _require_admin(request)

    before = get_role(role_id)
    if not before:
        raise HTTPException(status_code=404, detail="角色不存在")

    success, message = delete_role(role_id)

    if success:
        create_audit_log(
            user_id=admin["user_id"],
            username=admin["username"],
            action="delete_role",
            module="users",
            entity_type="role",
            entity_id=role_id,
            summary=f"删除角色: {before['display_name']}",
            before=before,
            ip_address=_get_client_ip(request)
        )

    return {"success": success, "message": message}


# ==========================================
# 审计日志
# ==========================================

@router.get("/audit-logs")
async def admin_get_audit_logs(
    request: Request,
    page: int = 1,
    page_size: int = 50,
    user_id: Optional[int] = None,
    action: Optional[str] = None,
    module: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
):
    """查询审计日志（分页 + 筛选）"""
    _require_governance_audit(request)

    result = list_audit_logs(
        page=page,
        page_size=min(page_size, 100),  # 最大 100 条/页
        user_id=user_id,
        action=action,
        module=module,
        start_date=start_date,
        end_date=end_date
    )

    return {"success": True, **result}


@router.get("/audit-logs/{log_id}")
async def admin_get_audit_log_detail(log_id: int, request: Request):
    """获取单条审计日志详情"""
    _require_governance_audit(request)
    log = get_audit_log(log_id)
    if not log:
        raise HTTPException(status_code=404, detail="日志不存在")
    return {"success": True, "log": log}


# ==========================================
# A.4 (CTO-15.9 session 3 · 2026-04-25) · auditor 拒绝率监督看板
# ==========================================

@router.get("/auditor/tuning-suggestions")
async def admin_auditor_tuning_suggestions(
    request: Request,
    days: int = 30,
    threshold: float = 0.4,
    min_samples: int = 5,
):
    """C2.3 (CTO-15.9 session 3 · 2026-04-25 · M2 §A.4 后续 prompt 调优反馈循环)

    基于 audit_logs 拒绝率 · 自动产出 prompt 调优文案建议:
      - 拒绝率 > threshold 行业 → "建议把 prompt 中关于 industry 的策略改…"
      - 高 deviation_pct(平均偏差大)→ "建议引入更宽松的 industry_median p50 baseline…"
      - 全行业全部拒绝 → "整个 auditor seed 可能整体偏严 · 建议放宽 10-15%"

    返结构化 suggestions 给运营运用 · 不自动改 prompt(老板批 · 半自动)
    """
    _require_admin(request)
    days = max(1, min(days, 365))
    threshold = max(0.0, min(threshold, 1.0))

    from db.connection import get_connection
    from datetime import datetime, timedelta
    import json as _json

    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT after_snapshot FROM audit_logs
            WHERE action = 'auditor_suggestion'
              AND created_at >= %s
              AND after_snapshot IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 5000
            """,
            (cutoff,),
        )
        rows = cur.fetchall()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    by_industry: dict = {}
    global_total = 0
    global_rejected = 0
    for r in rows:
        snap_raw = r.get("after_snapshot")
        if not snap_raw:
            continue
        try:
            snap = _json.loads(snap_raw) if isinstance(snap_raw, str) else snap_raw
        except (ValueError, TypeError):
            continue
        ind = (snap.get("industry") or "未知").strip() or "未知"
        accepted = bool(snap.get("accepted"))
        dev = int(snap.get("deviation_pct") or 0)
        b = by_industry.setdefault(ind, {
            "industry": ind, "total": 0, "rejected": 0, "deviation_sum": 0,
        })
        b["total"] += 1
        if not accepted:
            b["rejected"] += 1
        b["deviation_sum"] += dev
        global_total += 1
        if not accepted:
            global_rejected += 1

    # 生成调优建议
    suggestions: list[dict] = []

    # 全局判断 · 总拒绝率高
    if global_total >= min_samples * 3:
        global_rate = global_rejected / global_total
        if global_rate > threshold:
            suggestions.append({
                "scope": "global",
                "severity": "high",
                "issue": f"全平台 auditor 拒绝率 {global_rate * 100:.1f}% > {threshold * 100:.0f}%",
                "recommendation": (
                    # [v2.1 2026-06-11] 行业基线已切动态(prod paid 反推 + LLM 估 · 静态字典已删)
                    "行业基线已切动态(prod paid 反推 + LLM 估)· 建议:① 复核 industry_baseline_dynamic 的 LLM 估值"
                    "是否整体偏严 · ② 或在 industry_median_check 的 suggestion 文案改为更软"
                ),
                "evidence": {
                    "global_total": global_total,
                    "global_rejected": global_rejected,
                    "global_rejection_rate": round(global_rate, 3),
                },
            })

    # 单行业判断
    for ind, b in by_industry.items():
        if b["total"] < min_samples:
            continue
        rate = b["rejected"] / b["total"] if b["total"] else 0
        avg_dev = b["deviation_sum"] / b["total"] if b["total"] else 0
        if rate > threshold:
            suggestions.append({
                "scope": "industry",
                "industry": ind,
                "severity": "high" if rate > 0.6 else "medium",
                "issue": f"行业「{ind}」拒绝率 {rate * 100:.1f}% (样本 {b['total']}) · 平均偏差 {avg_dev:+.0f}%",
                "recommendation": (
                    # [v2.1 2026-06-11] 静态行业字典已删 · 基线 = prod paid 动态反推 + LLM 估
                    f"建议:① 行业「{ind}」基线已动态化 · 复核该行业 paid 样本量是否足 5(不足时走 LLM 估可能偏严)· "
                    f"② tools/keyword_expander 的 _INDUSTRY_TEMPLATE_ADDONS 检查是否覆盖该行业 · "
                    f"③ 如代理普遍坚持高价 · 行业本身可能溢价空间大 · 默认 baseline 偏严"
                ),
                "evidence": {
                    "rejection_rate": round(rate, 3),
                    "avg_deviation_pct": round(avg_dev, 1),
                    "sample": b["total"],
                },
            })
        elif avg_dev > 50 and b["total"] >= min_samples:
            # 偏差大但接受率高 → 代理普遍报高价 · 可能代理在压客户
            suggestions.append({
                "scope": "industry",
                "industry": ind,
                "severity": "low",
                "issue": f"行业「{ind}」平均偏差 {avg_dev:+.0f}% 偏高但接受率正常",
                "recommendation": (
                    f"建议:监测代理对高偏差报价的解释 · 若长期高于行业中位 50% 且代理坚持 · "
                    f"可能 industry_median seed 已过时 · 老板可决定上调"
                ),
                "evidence": {
                    "rejection_rate": round(rate, 3),
                    "avg_deviation_pct": round(avg_dev, 1),
                    "sample": b["total"],
                },
            })

    suggestions.sort(key=lambda s: (
        {"high": 0, "medium": 1, "low": 2}.get(s.get("severity"), 3),
        -s.get("evidence", {}).get("rejection_rate", 0),
    ))

    return {
        "window_days": days,
        "threshold": threshold,
        "min_samples": min_samples,
        "total_decisions": global_total,
        "suggestions_count": len(suggestions),
        "suggestions": suggestions,
    }


@router.get("/auditor/rejection-rate")
async def admin_auditor_rejection_rate(
    request: Request,
    industry: Optional[str] = None,
    days: int = 30,
    min_samples: int = 5,
):
    """A.4 · 行业 auditor 建议拒绝率聚合

    数据源:audit_logs · action='auditor_suggestion'
    after_snapshot 含 {industry, level, accepted, deviation_pct, ...}

    拒绝率 > 40% 的行业 → 触发 M1b prompt 调优 / industry_median seed 校准

    Args:
        industry: 单行业筛选 · None=返所有行业聚合
        days: 时间窗(默认 30 天)
        min_samples: 行业最小样本数(< 阈值不出统计 · 防小样本噪音)

    Returns:
        {
          "window_days": 30,
          "min_samples": 5,
          "rows": [
            {
              "industry": "家装",
              "total": 38,
              "accepted": 22, "rejected": 16,
              "rejection_rate": 0.421,
              "avg_deviation_pct": 41,
              "level_breakdown": {"normal": 0, "warn": 12, "danger": 26},
              "needs_tuning": true  # rejection_rate > 0.4
            },
            ...
          ],
          "summary": {
            "industries_total": 12,
            "industries_needing_tuning": 3,
            "global_rejection_rate": 0.318
          }
        }
    """
    _require_admin(request)

    days = max(1, min(days, 365))
    min_samples = max(1, min_samples)

    from db.connection import get_connection
    from datetime import datetime, timedelta
    import json as _json

    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    conn = get_connection()
    try:
        cur = conn.cursor()
        if industry:
            cur.execute(
                """
                SELECT after_snapshot FROM audit_logs
                WHERE action = 'auditor_suggestion'
                  AND created_at >= %s
                  AND after_snapshot IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 5000
                """,
                (cutoff,),
            )
        else:
            cur.execute(
                """
                SELECT after_snapshot FROM audit_logs
                WHERE action = 'auditor_suggestion'
                  AND created_at >= %s
                  AND after_snapshot IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 5000
                """,
                (cutoff,),
            )
        rows = cur.fetchall()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # 解 snapshot · 按行业聚合
    by_industry: dict = {}
    global_total = 0
    global_rejected = 0

    for r in rows:
        snap_raw = r.get("after_snapshot")
        if not snap_raw:
            continue
        try:
            snap = _json.loads(snap_raw) if isinstance(snap_raw, str) else snap_raw
        except (ValueError, TypeError):
            continue

        ind = (snap.get("industry") or "未知").strip() or "未知"
        if industry and ind != industry:
            continue

        accepted = bool(snap.get("accepted"))
        level = snap.get("level") or "unknown"
        dev_pct = snap.get("deviation_pct") or 0

        bucket = by_industry.setdefault(ind, {
            "industry": ind,
            "total": 0,
            "accepted": 0,
            "rejected": 0,
            "deviation_sum": 0,
            "level_breakdown": {"normal": 0, "warn": 0, "danger": 0, "unknown": 0},
        })
        bucket["total"] += 1
        if accepted:
            bucket["accepted"] += 1
        else:
            bucket["rejected"] += 1
        bucket["deviation_sum"] += int(dev_pct)
        if level not in bucket["level_breakdown"]:
            bucket["level_breakdown"][level] = 0
        bucket["level_breakdown"][level] += 1

        global_total += 1
        if not accepted:
            global_rejected += 1

    # 算拒绝率 + 过滤小样本
    out_rows = []
    industries_needing_tuning = 0
    for ind, b in by_industry.items():
        if b["total"] < min_samples:
            continue
        rate = round(b["rejected"] / b["total"], 3) if b["total"] else 0
        avg_dev = round(b["deviation_sum"] / b["total"]) if b["total"] else 0
        needs = rate > 0.4
        if needs:
            industries_needing_tuning += 1
        out_rows.append({
            "industry": b["industry"],
            "total": b["total"],
            "accepted": b["accepted"],
            "rejected": b["rejected"],
            "rejection_rate": rate,
            "avg_deviation_pct": avg_dev,
            "level_breakdown": b["level_breakdown"],
            "needs_tuning": needs,
        })

    out_rows.sort(key=lambda x: (-x["rejection_rate"], -x["total"]))

    global_rate = round(global_rejected / global_total, 3) if global_total else 0

    return {
        "window_days": days,
        "min_samples": min_samples,
        "rows": out_rows,
        "summary": {
            "industries_total": len(out_rows),
            "industries_needing_tuning": industries_needing_tuning,
            "global_rejection_rate": global_rate,
            "global_total_decisions": global_total,
        },
    }


# ==========================================
# 元数据
# ==========================================

@router.get("/modules")
async def admin_get_modules(request: Request):
    """获取所有模块和权限级别（供前端权限矩阵编辑器使用）"""
    _require_admin(request)
    return {"success": True, "modules": get_all_modules()}


# ==========================================
# 积分钱包管理
# ==========================================

@router.get("/users/{user_id}/wallet")
async def admin_get_user_wallet(user_id: int, request: Request):
    """获取用户钱包详情 + 流水"""
    _require_admin(request)

    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    from db.wallet_db import get_or_create_wallet, get_transactions
    from db.connection import get_connection

    wallet = get_or_create_wallet(user_id)
    transactions = get_transactions(user_id, limit=30)

    # 充值订单
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM recharge_orders WHERE user_id = %s
            ORDER BY created_at DESC LIMIT 20
        """, (user_id,))
        recharge_orders = [dict(r) for r in cur.fetchall()]
    except Exception:
        recharge_orders = []
    finally:
        conn.close()

    # 推荐链 + 团队
    referrals = []
    team = None
    referral_code = ""
    try:
        conn2 = get_connection()
        cur2 = conn2.cursor()
        # 推荐码（在 referral_codes 表）
        try:
            cur2.execute("SELECT code FROM referral_codes WHERE user_id = %s", (user_id,))
            rc_row = cur2.fetchone()
            referral_code = rc_row["code"] if rc_row else ""
        except Exception:
            referral_code = ""
        # 下线
        cur2.execute("""
            SELECT rl.referred_id, rl.level, rl.commission_rate, rl.created_at,
                   u.display_name, u.username
            FROM referral_links rl
            JOIN users u ON rl.referred_id = u.id
            WHERE rl.referrer_id = %s
            ORDER BY rl.level, rl.created_at DESC LIMIT 50
        """, (user_id,))
        referrals = [dict(r) for r in cur2.fetchall()]
        # 团队
        cur2.execute("""
            SELECT t.team_name, t.team_code, tm.role,
                   (SELECT COUNT(*) FROM team_members WHERE team_id = t.id AND status = 'active') as member_count
            FROM team_members tm
            JOIN teams t ON tm.team_id = t.id
            WHERE tm.user_id = %s AND tm.status = 'active' LIMIT 1
        """, (user_id,))
        team_row = cur2.fetchone()
        team = dict(team_row) if team_row else None
        conn2.close()
    except Exception:
        pass

    return {
        "success": True,
        "balance": {
            "paid_points": wallet.get("paid_points", 0),
            "bonus_points": wallet.get("bonus_points", 0),
            "total": wallet.get("paid_points", 0) + wallet.get("bonus_points", 0),
        },
        "agent_level": wallet.get("agent_level", 0),
        "total_recharged": wallet.get("total_recharged", 0),
        "recent_transactions": transactions,
        "recharge_orders": recharge_orders,
        "referral_code": referral_code,
        "referrals": referrals,
        "team": team,
    }


def _retired_wallet_mutation(user_id: int, request: Request):
    _require_admin(request)
    raise HTTPException(
        status_code=410,
        detail={
            "code": "WALLET_MUTATION_ENDPOINT_RETIRED",
            "message": "请在用户管理的钱包与账单中使用最高管理员算力校正",
            "migrate_to": f"/api/admin/user-governance/users/{int(user_id)}/wallet-adjustment",
        },
    )


@router.post("/users/{user_id}/add-credits")
async def admin_add_credits_retired(user_id: int, request: Request):
    return _retired_wallet_mutation(user_id, request)


@router.post("/users/{user_id}/deduct-credits")
async def admin_deduct_credits_retired(user_id: int, request: Request):
    return _retired_wallet_mutation(user_id, request)


@router.post("/users/{user_id}/set-wallet")
async def admin_set_wallet_retired(user_id: int, request: Request):
    return _retired_wallet_mutation(user_id, request)


@router.post("/users/{user_id}/refund")
async def admin_refund_order(user_id: int, request: Request):
    """管理员按订单号退款"""
    admin = _require_admin(request)
    body = await request.json()
    order_id = body.get("order_id")
    reason = body.get("reason", "管理员退款")

    if not order_id:
        raise HTTPException(400, detail="请提供订单号 order_id")

    user = get_user(user_id)
    if not user:
        raise HTTPException(404, detail="用户不存在")

    from db.connection import get_db
    from db.wallet_db import insert_transaction

    with get_db() as conn:
        cursor = conn.cursor()

        # 查找该订单号的扣费记录
        cursor.execute("""
            SELECT * FROM point_transactions
            WHERE user_id = %s AND order_id = %s AND type = 'consume'
        """, (user_id, order_id))
        consume_txs = cursor.fetchall()

        if not consume_txs:
            raise HTTPException(404, detail=f"未找到订单 {order_id} 的扣费记录")

        # 检查是否已退款
        cursor.execute("""
            SELECT 1 FROM point_transactions
            WHERE user_id = %s AND type = 'refund' AND description LIKE %s
        """, (user_id, f"%{order_id}%"))
        if cursor.fetchone():
            raise HTTPException(400, detail="该订单已退款，请勿重复操作")

        # 锁定钱包
        cursor.execute("SELECT paid_points, bonus_points FROM user_wallets WHERE user_id = %s FOR UPDATE", (user_id,))

        total_refunded = 0
        for tx in consume_txs:
            refund_amount = abs(tx["amount"])
            col = "bonus_points" if tx["point_type"] == "bonus" else "paid_points"
            cursor.execute(f"""
                UPDATE user_wallets SET {col} = {col} + %s, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = %s RETURNING paid_points, bonus_points
            """, (refund_amount, user_id))
            result = cursor.fetchone()

            insert_transaction(cursor, user_id, "refund", tx["point_type"],
                              refund_amount, result[col], tx.get("feature_code"),
                              description=f"退款({order_id}): {reason}",
                              order_id=order_id)
            total_refunded += refund_amount

    create_audit_log(
        user_id=admin["user_id"], username=admin["username"],
        action="admin_refund", module="wallet",
        entity_type="user", entity_id=user_id,
        summary=f"退款 {order_id}: {total_refunded}积分 ({reason})",
        after={"order_id": order_id, "refunded": total_refunded, "reason": reason},
        ip_address=_get_client_ip(request)
    )

    return {"success": True, "order_id": order_id, "refunded": total_refunded}


# ==========================================
# 代理等级管理
# ==========================================

@router.put("/users/{user_id}/agent-level")
async def admin_set_agent_level(user_id: int, req: AdminSetAgentLevelRequest, request: Request):
    """旧等级写入口已停用；使用业务身份 CAS API。"""
    _require_admin(request)
    raise HTTPException(
        status_code=410,
        detail={
            "code": "LEGACY_AGENT_LEVEL_WRITER_RETIRED",
            "migrate_to": "/api/admin/user-governance/users/{user_id}/business-identity",
        },
    )

# ==========================================
# 推荐码管理（仅限超级管理员）
# ==========================================

SUPER_ADMINS = {"admin"}

class UpdateReferralCodeRequest(BaseModel):
    new_code: str = Field(min_length=3, max_length=20, description="新推荐码，3-20字符")


@router.put("/users/{user_id}/referral-code")
async def admin_update_referral_code(user_id: int, req: UpdateReferralCodeRequest, request: Request):
    """超级管理员修改用户推荐码（仅 admin / DemoUser）"""
    admin = _require_admin(request)
    if admin.get("username") not in SUPER_ADMINS:
        raise HTTPException(status_code=403, detail="仅超级管理员可修改推荐码")

    from db.connection import get_db
    new_code = req.new_code.strip().upper()

    with get_db() as conn:
        cursor = conn.cursor()
        # 检查新码是否已被占用
        # [P0 2026-08-05] 唯一性检查也必须折叠后比 —— 否则能造出两个折叠后相同的码,
        #   那会让归一化匹配把 A 的推荐算到 B 头上(资金归属错误)。
        from services.referral_code_normalize import fold_sql, normalize_code
        cursor.execute(
            f"SELECT user_id FROM referral_codes WHERE {fold_sql('code')} = {fold_sql('%s')} AND user_id != %s",
            (normalize_code(new_code), user_id))
        if cursor.fetchone():
            raise HTTPException(status_code=400, detail=f"推荐码 {new_code} 已被其他用户使用")

        # 查旧码
        cursor.execute("SELECT code FROM referral_codes WHERE user_id = %s", (user_id,))
        old_row = cursor.fetchone()
        old_code = old_row["code"] if old_row else None

        if old_code:
            cursor.execute("UPDATE referral_codes SET code = %s WHERE user_id = %s", (new_code, user_id))
        else:
            cursor.execute("INSERT INTO referral_codes (user_id, code) VALUES (%s, %s)", (user_id, new_code))

    create_audit_log(
        user_id=admin["user_id"],
        username=admin["username"],
        action="update_referral_code",
        module="referral",
        entity_type="user",
        entity_id=user_id,
        summary=f"推荐码修改: {old_code} → {new_code}",
        before={"referral_code": old_code},
        after={"referral_code": new_code},
        ip_address=_get_client_ip(request)
    )

    return {"success": True, "old_code": old_code, "new_code": new_code}


# ==========================================
# 模块授权覆盖
# ==========================================

@router.get("/users/{user_id}/module-overrides")
async def admin_get_module_overrides(user_id: int, request: Request):
    """历史模块覆盖只读兼容；没有真实鉴权消费者，不在请求中执行 DDL。"""
    _require_admin(request)
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('user_module_overrides') AS table_name")
        if not cur.fetchone()["table_name"]:
            return {"success": True, "overrides": {}, "deprecated": True, "consumer_count": 0}
        cur.execute(
            "SELECT module_name, granted FROM user_module_overrides WHERE user_id = %s",
            (user_id,)
        )
        overrides = {r["module_name"]: r["granted"] for r in cur.fetchall()}
        return {"success": True, "overrides": overrides, "deprecated": True, "consumer_count": 0}
    finally:
        conn.close()


@router.put("/users/{user_id}/module-overrides")
async def admin_set_module_overrides(user_id: int, request: Request):
    """历史模块覆盖写入口已停用；表和旧数据保留只读兼容。"""
    _require_admin(request)
    raise HTTPException(
        status_code=410,
        detail={"code": "MODULE_OVERRIDES_RETIRED", "message": "模块覆盖已废弃且没有鉴权消费者"},
    )

# ==========================================
# 佣金流水管理（管理员查看任意用户）
# ==========================================

@router.get("/users/{user_id}/commissions")
async def admin_get_user_commissions(user_id: int, request: Request, limit: int = 50):
    """管理员查看用户佣金流水"""
    _require_admin(request)
    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        # 汇总
        cur.execute("""
            SELECT
                COALESCE(SUM(commission_yuan), 0) AS total_earned,
                COALESCE(SUM(commission_yuan) FILTER (WHERE status = 'pending'), 0) AS pending,
                COALESCE(SUM(commission_yuan) FILTER (WHERE status = 'converted'), 0) AS converted,
                COUNT(*) AS record_count
            FROM commission_records WHERE referrer_id = %s
        """, (user_id,))
        stats = cur.fetchone()

        # 明细
        cur.execute("""
            SELECT cr.*, u.display_name AS referred_name, u.username AS referred_username
            FROM commission_records cr
            LEFT JOIN users u ON u.id = cr.referred_id
            WHERE cr.referrer_id = %s
            ORDER BY cr.created_at DESC LIMIT %s
        """, (user_id, min(limit, 100)))
        records = [dict(r) for r in cur.fetchall()]

        return {
            "success": True,
            "total_earned": float(stats["total_earned"]) if stats else 0,
            "pending": float(stats["pending"]) if stats else 0,
            "converted": float(stats["converted"]) if stats else 0,
            "record_count": stats["record_count"] if stats else 0,
            "records": records,
        }
    finally:
        conn.close()


# ==========================================
# 批量操作
# ==========================================

@router.post("/users/batch-toggle-active")
async def admin_batch_toggle_active(request: Request):
    """批量启用/禁用用户"""
    admin = _require_admin(request)
    body = await request.json()
    user_ids = body.get("user_ids", [])
    is_active = body.get("is_active", 0)

    if not user_ids or len(user_ids) > 100:
        raise HTTPException(400, "user_ids 必须非空且不超过100个")

    from db.connection import get_db
    with get_db() as conn:
        cursor = conn.cursor()
        # 排除管理员
        cursor.execute("""
            SELECT id FROM users
            WHERE id = ANY(%s) AND NOT EXISTS (
                SELECT 1 FROM user_roles ur JOIN roles r ON ur.role_id = r.id
                WHERE ur.user_id = users.id AND r.name = 'admin'
            )
        """, (user_ids,))
        valid_ids = [r["id"] for r in cursor.fetchall()]

        if valid_ids:
            # [GEO-R1-CAN-028] 同步递增 permission_version,使批量禁用的用户 JWT 立即失效。
            # 否则中间件在版本匹配时信任 is_active(不在 JWT 中),禁用最长 7 天才生效。
            cursor.execute(
                "UPDATE users SET is_active = %s, permission_version = permission_version + 1 WHERE id = ANY(%s)",
                (int(is_active), valid_ids)
            )

    create_audit_log(
        user_id=admin["user_id"],
        username=admin["username"],
        action="batch_toggle_active",
        module="users",
        entity_type="user",
        summary=f"批量{'启用' if is_active else '禁用'} {len(valid_ids)} 个用户",
        after={"user_ids": valid_ids, "is_active": is_active},
        ip_address=_get_client_ip(request)
    )

    return {"success": True, "affected": len(valid_ids), "skipped_admin": len(user_ids) - len(valid_ids)}


@router.get("/users/export")
async def admin_export_users(request: Request):
    """导出用户列表 CSV（流式分批，不全量加载内存）"""
    _require_admin(request)

    import csv, io
    from fastapi.responses import StreamingResponse
    from db.connection import get_connection

    BATCH_SIZE = 500

    def generate_csv():
        # BOM for Excel 中文兼容
        yield '\ufeff'

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["ID", "用户名", "显示名", "手机", "公司", "角色", "客户数",
                         "paid积分", "bonus积分", "服务方等级", "累计充值(元)", "状态",
                         "性别", "省份", "城市", "注册来源", "注册时间", "最后登录"])
        yield buf.getvalue()

        offset = 0
        while True:
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute("""
                    SELECT u.id, u.username, u.display_name, u.phone, u.company, u.is_active,
                           u.gender, u.register_province, u.city, u.register_source,
                           u.created_at, u.last_login_at,
                           COALESCE(w.paid_points, 0) as paid_points,
                           COALESCE(w.bonus_points, 0) as bonus_points,
                           COALESCE(w.agent_level, 0) as agent_level,
                           COALESCE(w.total_recharged, 0) as total_recharged
                    FROM users u LEFT JOIN user_wallets w ON w.user_id = u.id
                    ORDER BY u.id
                    LIMIT %s OFFSET %s
                """, (BATCH_SIZE, offset))
                rows = cur.fetchall()

                if not rows:
                    break

                # 批量查角色
                uids = [r["id"] for r in rows]
                cur.execute("""
                    SELECT ur.user_id, string_agg(r.display_name, '/') as roles
                    FROM user_roles ur JOIN roles r ON r.id = ur.role_id
                    WHERE ur.user_id = ANY(%s) GROUP BY ur.user_id
                """, (uids,))
                role_map = {r["user_id"]: r["roles"] for r in cur.fetchall()}

                cur.execute("""
                    SELECT user_id, COUNT(*) as cnt FROM user_clients
                    WHERE user_id = ANY(%s) GROUP BY user_id
                """, (uids,))
                client_map = {r["user_id"]: r["cnt"] for r in cur.fetchall()}
            finally:
                conn.close()

            buf = io.StringIO()
            writer = csv.writer(buf)
            for r in rows:
                writer.writerow([
                    r["id"], r["username"], r["display_name"],
                    r.get("phone", ""), r.get("company", ""),
                    role_map.get(r["id"], ""), client_map.get(r["id"], 0),
                    r["paid_points"], r["bonus_points"], r["agent_level"],
                    f'{r["total_recharged"] / 100:.0f}',
                    "启用" if r["is_active"] else "禁用",
                    {"male": "男", "female": "女"}.get(r.get("gender", ""), ""),
                    r.get("register_province", ""), r.get("city", ""),
                    r.get("register_source", ""),
                    str(r.get("created_at", "")),
                    str(r.get("last_login_at", "") or ""),
                ])
            yield buf.getvalue()

            if len(rows) < BATCH_SIZE:
                break
            offset += BATCH_SIZE

    return StreamingResponse(
        generate_csv(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=users_export_{__import__('datetime').date.today()}.csv"}
    )


# ==========================================
# 用户标签管理
# ==========================================

@router.get("/tags")
async def admin_list_tags(request: Request):
    """获取所有用户标签"""
    _require_admin(request)
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM user_tags ORDER BY name")
        tags = [dict(r) for r in cur.fetchall()]
        return {"success": True, "tags": tags}
    finally:
        conn.close()


@router.post("/tags")
async def admin_create_tag(request: Request):
    """创建标签"""
    admin = _require_admin(request)
    body = await request.json()
    name = body.get("name", "").strip()
    color = body.get("color", "#6b7280")
    if not name or len(name) > 20:
        raise HTTPException(400, "标签名 1-20 字符")

    from db.connection import get_db
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO user_tags (name, color) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING RETURNING id", (name, color))
        row = cur.fetchone()
        if not row:
            raise HTTPException(400, f"标签 '{name}' 已存在")
    return {"success": True, "id": row["id"], "name": name, "color": color}


@router.delete("/tags/{tag_id}")
async def admin_delete_tag(tag_id: int, request: Request):
    """删除标签（同时删除所有关联）"""
    _require_admin(request)
    from db.connection import get_db
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM user_tags WHERE id = %s", (tag_id,))
    return {"success": True}


@router.post("/users/{user_id}/tags")
async def admin_set_user_tags(user_id: int, request: Request):
    """设置用户标签（替换模式）"""
    _require_admin(request)
    body = await request.json()
    tag_ids = body.get("tag_ids", [])

    from db.connection import get_db
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM user_tag_assignments WHERE user_id = %s", (user_id,))
        for tid in tag_ids:
            cur.execute("INSERT INTO user_tag_assignments (user_id, tag_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (user_id, tid))
    return {"success": True}


# ==========================================
# 用户性别/扩展字段更新
# ==========================================

@router.put("/users/{user_id}/profile")
async def admin_update_user_profile(user_id: int, request: Request):
    """管理员修改用户扩展资料（性别/手机/邮箱/公司等）"""
    admin = _require_admin(request)
    user = get_user(user_id)
    if not user:
        raise HTTPException(404, "用户不存在")

    body = await request.json()
    ALLOWED = {"gender", "phone", "email", "real_name", "wechat_id", "company", "job_title", "industry", "city", "bio"}
    updates = {k: v for k, v in body.items() if k in ALLOWED}
    if not updates:
        return {"success": True, "message": "无变更"}

    from db.connection import get_db
    with get_db() as conn:
        cur = conn.cursor()
        set_parts = [f"{k} = %s" for k in updates]
        cur.execute(f"UPDATE users SET {', '.join(set_parts)} WHERE id = %s", list(updates.values()) + [user_id])

    create_audit_log(
        user_id=admin["user_id"], username=admin["username"],
        action="update_user_profile", module="users",
        entity_type="user", entity_id=user_id,
        summary=f"修改用户资料: {user['username']}",
        after=updates, ip_address=_get_client_ip(request)
    )
    return {"success": True}


# ==========================================
# 操作确认码管理
# ==========================================

# 允许设置确认码的 action 列表
ALLOWED_CONFIRM_ACTIONS = {
    "mark_paid": "收款确认码",
    # 未来可扩展:
    # "refund": "退款确认码",
    # "delete_brand": "删除客户确认码",
    # "batch_publish": "批量发布确认码",
}


class SetConfirmCodeRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=64)


@router.get("/confirm-codes")
async def admin_list_confirm_codes(request: Request):
    """获取所有确认码元信息（不含哈希）"""
    _require_admin(request)
    settings = load_settings()
    codes_raw = settings.confirm_codes or {}

    result = []
    for action, label in ALLOWED_CONFIRM_ACTIONS.items():
        entry_data = codes_raw.get(action, {})
        if isinstance(entry_data, dict):
            entry = ConfirmCodeEntry(**entry_data)
        else:
            entry = entry_data
        result.append({
            "action": action,
            "label": label,
            "has_code": bool(entry.code_hash) if isinstance(entry, ConfirmCodeEntry) else bool(entry_data.get("code_hash")),
            "updated_at": entry.updated_at if isinstance(entry, ConfirmCodeEntry) else entry_data.get("updated_at", ""),
            "updated_by": entry.updated_by if isinstance(entry, ConfirmCodeEntry) else entry_data.get("updated_by", ""),
        })

    return {"success": True, "codes": result}


@router.put("/confirm-codes/{action}")
async def admin_set_confirm_code(action: str, req: SetConfirmCodeRequest, request: Request):
    """设置或修改操作确认码"""
    admin = _require_admin(request)

    if action not in ALLOWED_CONFIRM_ACTIONS:
        raise HTTPException(400, f"不支持的操作类型: {action}")

    import hashlib
    from datetime import datetime

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    code_hash = hashlib.sha256(req.code.encode("utf-8")).hexdigest()

    settings = load_settings()
    codes = settings.confirm_codes or {}

    codes[action] = {
        "code_hash": code_hash,
        "updated_at": now,
        "updated_by": admin["username"],
        "fail_count": 0,
        "locked_until": "",
    }
    settings.confirm_codes = codes
    save_settings(settings)

    # 刷新缓存
    from config.settings_manager import reload_settings
    reload_settings()

    # 审计日志
    create_audit_log(
        user_id=admin["user_id"],
        username=admin["username"],
        action="set_confirm_code",
        module="settings",
        entity_type="confirm_code",
        summary=f"修改{ALLOWED_CONFIRM_ACTIONS[action]}: {action}",
        after={"action": action, "updated_at": now},
        ip_address=_get_client_ip(request)
    )

    logger.info(f"确认码修改: {action} by {admin['username']}")
    return {"success": True, "action": action, "updated_at": now}


@router.delete("/confirm-codes/{action}")
async def admin_delete_confirm_code(action: str, request: Request):
    """删除操作确认码（恢复为无需确认码）"""
    admin = _require_admin(request)

    if action not in ALLOWED_CONFIRM_ACTIONS:
        raise HTTPException(400, f"不支持的操作类型: {action}")

    settings = load_settings()
    codes = settings.confirm_codes or {}

    if action not in codes:
        raise HTTPException(404, "该操作未设置确认码")

    del codes[action]
    settings.confirm_codes = codes
    save_settings(settings)

    from config.settings_manager import reload_settings
    reload_settings()

    create_audit_log(
        user_id=admin["user_id"],
        username=admin["username"],
        action="delete_confirm_code",
        module="settings",
        entity_type="confirm_code",
        summary=f"删除{ALLOWED_CONFIRM_ACTIONS[action]}: {action}",
        ip_address=_get_client_ip(request)
    )

    logger.info(f"确认码删除: {action} by {admin['username']}")
    return {"success": True}


# ========== 真相画像（管理员专属） ==========

@router.get("/user-insights/{brand_id}", summary="获取用户真相画像")
def api_get_user_insights(brand_id: int, request: Request):
    """获取指定品牌关联的所有档案的真相画像（仅管理员）"""
    admin = _require_admin(request)

    from db.connection import get_connection
    import json

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, industry, persona_positioning,
                   admin_insight, admin_insight_level, admin_insight_updated_at
            FROM client_profiles
            WHERE brand_id = %s AND (is_deleted = 0 OR is_deleted IS NULL)
            ORDER BY created_at DESC
        """, (brand_id,))
        rows = cursor.fetchall()
        conn.close()

        profiles = []
        for r in rows:
            insight = None
            if r.get("admin_insight"):
                try:
                    insight = json.loads(r["admin_insight"])
                except (json.JSONDecodeError, TypeError):
                    insight = {"raw": r["admin_insight"]}

            profiles.append({
                "profile_id": r["id"],
                "name": r["name"],
                "industry": r.get("industry"),
                "persona_positioning": r.get("persona_positioning"),
                "insight_level": r.get("admin_insight_level") or "inactive",
                "insight_updated_at": str(r["admin_insight_updated_at"]) if r.get("admin_insight_updated_at") else None,
                "insight": insight,
                "one_line_truth": insight.get("one_line_truth") if insight else None,
            })

        return {"success": True, "profiles": profiles}
    finally:
        try:
            conn.close()
        except Exception: pass


# ==========================================
# 代理申请审核台（v1.1 审核制）
# ==========================================

class PartnerReviewRejectRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500, description="拒绝原因")


class PartnerReviewApproveRequest(BaseModel):
    notes: Optional[str] = Field(default=None, max_length=500)


class PartnerReviewMoreInfoRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=500, description="要求补充的内容")


@router.get("/partner/applications")
async def admin_list_partner_applications(
    request: Request,
    status: Optional[str] = "manual_review",
    limit: int = 50,
    offset: int = 0,
):
    """审核队列列表 (默认只看 manual_review)"""
    _require_admin(request)

    if status not in (None, "", "pending", "manual_review", "approved",
                      "rejected", "withdrawn", "auto_approved"):
        raise HTTPException(400, f"非法 status: {status}")
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    from db.connection import get_connection
    conn = get_connection()
    try:
        cursor = conn.cursor()
        where_clause = "WHERE status = %s" if status else ""
        params: list = [status] if status else []
        cursor.execute(
            f"""
            SELECT aa.id, aa.user_id, aa.real_name, aa.id_card_no_mask,
                   aa.promotion_scenes, aa.expected_monthly_customers, aa.remark,
                   aa.ocr_confidence, aa.ai_risk_flags,
                   aa.status, aa.rejection_reason,
                   aa.reviewed_by, aa.reviewed_at,
                   aa.ip_address, aa.device_fingerprint,
                   aa.created_at, aa.updated_at,
                   u.username, u.display_name
            FROM agent_applications aa
            LEFT JOIN users u ON u.id = aa.user_id
            {where_clause}
            ORDER BY aa.created_at DESC
            LIMIT %s OFFSET %s
            """,
            (*params, limit, offset),
        )
        rows = cursor.fetchall() or []

        cursor.execute(
            f"SELECT COUNT(*) AS cnt FROM agent_applications {where_clause}",
            tuple(params),
        )
        total = (cursor.fetchone() or {}).get("cnt", 0) or 0

        return {
            "success": True,
            "total": total,
            "limit": limit,
            "offset": offset,
            "applications": [
                {
                    "id": r["id"],
                    "user_id": r["user_id"],
                    "username": r.get("username"),
                    "display_name": r.get("display_name"),
                    "real_name": r["real_name"],
                    "id_card_no_mask": r["id_card_no_mask"],
                    "promotion_scenes": r.get("promotion_scenes"),
                    "expected_monthly_customers": r.get("expected_monthly_customers"),
                    "remark": r.get("remark"),
                    "ocr_confidence": float(r["ocr_confidence"]) if r.get("ocr_confidence") else None,
                    "ai_risk_flags": r.get("ai_risk_flags"),
                    "status": r["status"],
                    "rejection_reason": r.get("rejection_reason"),
                    "reviewed_by": r.get("reviewed_by"),
                    "reviewed_at": r["reviewed_at"].isoformat() if r.get("reviewed_at") else None,
                    "ip_address": r.get("ip_address"),
                    "device_fingerprint": r.get("device_fingerprint"),
                    "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                }
                for r in rows
            ],
        }
    finally:
        try: conn.close()
        except Exception: pass


@router.get("/partner/applications/{app_id}")
async def admin_get_partner_application(app_id: int, request: Request):
    """申请详情 (含 3 张身份证临时 URL, 审核员看完即过期)"""
    admin = _require_admin(request)

    from db.connection import get_connection
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT aa.*, u.username, u.display_name, u.phone, u.created_at AS user_created_at
            FROM agent_applications aa
            LEFT JOIN users u ON u.id = aa.user_id
            WHERE aa.id = %s
            """,
            (app_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(404, "申请不存在")
    finally:
        try: conn.close()
        except Exception: pass

    # 签发 3 张临时 URL（15 分钟）
    from services.oss_service import generate_signed_url, OSSAccessError, OSSConfigError
    signed_urls: dict = {}
    for side in ("front", "back", "selfie"):
        key = row.get(f"id_card_{side}_key")
        if not key:
            signed_urls[side] = None
            continue
        try:
            signed_urls[side] = generate_signed_url(key, expires_seconds=900)
        except (OSSConfigError, OSSAccessError) as e:
            logger.warning(f"[Admin-Partner] 签名 {side} URL 失败 app={app_id}: {e}")
            signed_urls[side] = None

    # 审计: 查看身份证属于敏感操作
    create_audit_log(
        user_id=admin["user_id"], username=admin["username"],
        action="view_partner_application", module="partner_review",
        entity_type="agent_application", entity_id=app_id,
        summary=f"查看代理申请 {app_id} 详情 (含身份证临时 URL)",
        ip_address=_get_client_ip(request),
    )

    return {
        "success": True,
        "application": {
            "id": row["id"],
            "user_id": row["user_id"],
            "username": row.get("username"),
            "display_name": row.get("display_name"),
            "phone": _mask_phone(row.get("phone")),
            "user_created_at": row["user_created_at"].isoformat() if row.get("user_created_at") else None,
            "real_name": row["real_name"],
            "id_card_no_mask": row["id_card_no_mask"],
            "promotion_scenes": row.get("promotion_scenes"),
            "expected_monthly_customers": row.get("expected_monthly_customers"),
            "remark": row.get("remark"),
            "ocr_result": row.get("ocr_result"),
            "ocr_confidence": float(row["ocr_confidence"]) if row.get("ocr_confidence") else None,
            "ai_risk_flags": row.get("ai_risk_flags"),
            "status": row["status"],
            "rejection_reason": row.get("rejection_reason"),
            "reviewed_by": row.get("reviewed_by"),
            "reviewed_at": row["reviewed_at"].isoformat() if row.get("reviewed_at") else None,
            "ip_address": row.get("ip_address"),
            "device_fingerprint": row.get("device_fingerprint"),
            "user_agent": row.get("user_agent"),
            "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        },
        "id_card_urls": signed_urls,
        "urls_expire_in": 900,
    }


@router.post("/partner/applications/{app_id}/approve")
async def admin_approve_partner_application(
    app_id: int,
    req: PartnerReviewApproveRequest,
    request: Request,
):
    """人工通过审核 → 立即激活代理身份"""
    admin = _require_admin(request)

    from db.connection import get_connection
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, user_id, status FROM agent_applications WHERE id = %s
            """,
            (app_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(404, "申请不存在")
        if row["status"] == "approved":
            return {"success": True, "application_id": app_id, "status": "approved",
                    "message": "已是通过状态"}
        if row["status"] not in ("pending", "manual_review"):
            raise HTTPException(400, f"当前状态 {row['status']} 不可通过")

        target_user_id = row["user_id"]

        cursor.execute(
            """
            UPDATE agent_applications
            SET status = 'approved',
                reviewed_by = %s,
                reviewed_at = NOW(),
                updated_at = NOW()
            WHERE id = %s
            """,
            (admin["user_id"], app_id),
        )
        cursor.execute(
            """
            UPDATE user_wallets
            SET agent_level = GREATEST(agent_level, 1),
                agent_verified = TRUE,
                updated_at = NOW()
            WHERE user_id = %s
            """,
            (target_user_id,),
        )
        conn.commit()
    except HTTPException:
        try: conn.rollback()
        except Exception: pass
        raise
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        logger.error(f"[Admin-Partner] 通过审核失败 app={app_id}: {e}")
        raise HTTPException(500, "操作失败")
    finally:
        try: conn.close()
        except Exception: pass

    create_audit_log(
        user_id=admin["user_id"], username=admin["username"],
        action="approve_partner_application", module="partner_review",
        entity_type="agent_application", entity_id=app_id,
        summary=f"通过代理申请 {app_id} → user_id={target_user_id} 激活代理身份",
        after={"notes": req.notes} if req.notes else None,
        ip_address=_get_client_ip(request),
    )
    logger.info(f"[Admin-Partner] 申请 {app_id} 通过, target={target_user_id}, by {admin['username']}")

    return {"success": True, "application_id": app_id, "status": "approved"}


@router.post("/partner/applications/{app_id}/reject")
async def admin_reject_partner_application(
    app_id: int,
    req: PartnerReviewRejectRequest,
    request: Request,
):
    """人工拒绝审核"""
    admin = _require_admin(request)

    from db.connection import get_connection
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, user_id, status FROM agent_applications WHERE id = %s",
            (app_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(404, "申请不存在")
        if row["status"] not in ("pending", "manual_review"):
            raise HTTPException(400, f"当前状态 {row['status']} 不可拒绝")

        target_user_id = row["user_id"]

        cursor.execute(
            """
            UPDATE agent_applications
            SET status = 'rejected',
                rejection_reason = %s,
                reviewed_by = %s,
                reviewed_at = NOW(),
                updated_at = NOW()
            WHERE id = %s
            """,
            (req.reason, admin["user_id"], app_id),
        )
        conn.commit()
    except HTTPException:
        try: conn.rollback()
        except Exception: pass
        raise
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        logger.error(f"[Admin-Partner] 拒绝审核失败 app={app_id}: {e}")
        raise HTTPException(500, "操作失败")
    finally:
        try: conn.close()
        except Exception: pass

    create_audit_log(
        user_id=admin["user_id"], username=admin["username"],
        action="reject_partner_application", module="partner_review",
        entity_type="agent_application", entity_id=app_id,
        summary=f"拒绝代理申请 {app_id} → user_id={target_user_id}",
        after={"reason": req.reason},
        ip_address=_get_client_ip(request),
    )
    logger.info(f"[Admin-Partner] 申请 {app_id} 拒绝, target={target_user_id}, by {admin['username']}")

    return {"success": True, "application_id": app_id, "status": "rejected"}


@router.post("/partner/applications/{app_id}/request-more")
async def admin_request_more_info(
    app_id: int,
    req: PartnerReviewMoreInfoRequest,
    request: Request,
):
    """要求用户补充材料

    现阶段简化: 保持状态 manual_review 不变, 把消息追加到 rejection_reason 字段
    (复用 rejection_reason 作为"审核备注"的展示渠道, 前端据此提示用户).
    用户可直接撤回后重新提交.
    """
    admin = _require_admin(request)

    from db.connection import get_connection
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, user_id, status FROM agent_applications WHERE id = %s",
            (app_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(404, "申请不存在")
        if row["status"] not in ("pending", "manual_review"):
            raise HTTPException(400, f"当前状态 {row['status']} 不可要求补材料")

        target_user_id = row["user_id"]

        cursor.execute(
            """
            UPDATE agent_applications
            SET rejection_reason = %s,
                updated_at = NOW()
            WHERE id = %s
            """,
            (f"[需补充] {req.message}", app_id),
        )
        conn.commit()
    except HTTPException:
        try: conn.rollback()
        except Exception: pass
        raise
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        logger.error(f"[Admin-Partner] 要求补材料失败 app={app_id}: {e}")
        raise HTTPException(500, "操作失败")
    finally:
        try: conn.close()
        except Exception: pass

    create_audit_log(
        user_id=admin["user_id"], username=admin["username"],
        action="request_more_partner_info", module="partner_review",
        entity_type="agent_application", entity_id=app_id,
        summary=f"要求代理申请 {app_id} 补充材料 (user_id={target_user_id})",
        after={"message": req.message},
        ip_address=_get_client_ip(request),
    )

    return {"success": True, "application_id": app_id, "status": "manual_review",
            "message": "已标记需补充材料"}


# ============================================================================
# v3.6 CTO-15.2 2026-04-19 · 行业知识公共库矫正回滚 admin 后台
# ============================================================================

@router.get("/industry-knowledge/corrections", summary="查询 L1/L2 矫正记录(v3.6)")
async def admin_list_industry_corrections(
    request: Request,
    industry: Optional[str] = None,
    category: Optional[str] = None,
    only_active: bool = True,
    limit: int = 50,
):
    """admin 看所有/某行业的矫正记录,默认仅显示未回滚的"""
    _require_admin(request)
    from db.industry_corrections_db import list_corrections
    items = list_corrections(industry=industry, category=category, only_active=only_active, limit=limit)
    return {"success": True, "items": items, "total": len(items)}


@router.post("/industry-knowledge/rollback/{correction_id}", summary="一键回滚某条矫正(v3.6)")
async def admin_rollback_industry_correction(correction_id: int, request: Request):
    """
    admin 一键回滚某条用户矫正,恢复 industry_knowledge 该字段为 old_value。

    回滚后该字段对全行业用户立即生效(下次 get_industry_knowledge 命中新版本)。
    """
    admin = _require_admin(request)
    admin_user_id = admin.get("user_id") or current_user_id(admin)
    from db.industry_corrections_db import rollback_correction
    try:
        result = rollback_correction(correction_id, admin_user_id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"回滚失败: {str(e)}")


# ============================================================================
# v3.8 · rerun/correct 反哺审计记录 (CTO-13.3 2026-04-19 · PLAN Q15 C5)
# ============================================================================

@router.get("/industry-knowledge/reverse-feed/list", summary="反哺记录列表(v3.8 C5)")
async def admin_list_reverse_feeds(
    request: Request,
    industry: Optional[str] = None,
    field_name: Optional[str] = None,
    source_type: Optional[str] = None,  # 'ai_rerun' | 'user_correct'
    only_active: bool = True,
    limit: int = 50,
):
    """
    admin 查询 industry_knowledge_reverse_feed 表 (v3.8 rerun/correct 反哺审计).

    场景:
    - 监控哪些用户 rerun / correct 在反哺共享池
    - 看 LLM 投票拒绝的理由 (AI 审核是否太严/太松)
    - 定位问题记录后一键回滚
    """
    _require_admin(request)
    from db.reverse_feed_db import list_reverse_feeds
    items = list_reverse_feeds(
        industry=industry, field_name=field_name,
        source_type=source_type, only_active=only_active, limit=limit,
    )
    return {"success": True, "items": items, "total": len(items)}


class ReverseFeedRollbackRequest(BaseModel):
    reason: Optional[str] = Field(None, description="回滚原因（运营复盘/审计用）")


@router.post("/industry-knowledge/reverse-feed/rollback/{feed_id}", summary="一键回滚某次反哺(v3.8 C5)")
async def admin_rollback_reverse_feed(
    feed_id: int,
    request: Request,
    data: Optional[ReverseFeedRollbackRequest] = None,
):
    """
    admin 回滚某次 rerun/correct 反哺: 把该条 new_items 从 industry_knowledge 共享池撤回.

    仅支持 list 类字段 (list_dedup / append_only) · overwrite 类字段无法精准回滚.
    回滚后该字段对全行业用户立即生效 (下次 get_industry_knowledge 命中新版本).
    """
    admin = _require_admin(request)
    admin_user_id = admin.get("user_id") or current_user_id(admin)
    from db.reverse_feed_db import rollback_reverse_feed
    try:
        result = rollback_reverse_feed(
            feed_id=feed_id,
            admin_user_id=admin_user_id,
            reason=(data.reason if data else None),
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"反哺回滚失败: {str(e)}")


class RebuildIndustryRequest(BaseModel):
    industry: str = Field(..., description="行业名(必填)")
    category: Optional[str] = Field(None, description="品类名(可选,只重建该 L2;不传则重建该行业 L1+所有 L2)")
    reason: Optional[str] = Field(None, description="重建理由(如 GPT-5 发布/AI 搜索引擎重新洗牌)")


@router.post("/industry-knowledge/rebuild", summary="行业洗牌一键重建(v3.7)")
async def admin_rebuild_industry_knowledge(data: RebuildIndustryRequest, request: Request):
    """
    v3.7 道法术器架构下的"行业洗牌"按钮。

    场景: 行业重大变革时(如 GPT-5 发布/搜索引擎算法重大变化)需要把整个行业的
    道法术器全部清空重建。普通到期 TTL 重采是"叠加",rebuild 是"清零"。

    行为:
    - 当前 industry_knowledge 整条记录的 knowledge 字段被 reset (旧版归档到 _v37_meta.archived_history,最多 5 个)
    - _v37_meta.rebuild_count + 1
    - 同行业用户下次 deep_analyze 触发会全量重采(类似首次拓荒成本)

    Body: {industry: str, category?: str, reason?: str}
    - 不传 category: 重建该行业的 L1 + 该行业下所有 L2(整行业洗牌)
    - 传 category: 仅重建该 L2(品类级洗牌)
    """
    admin = _require_admin(request)
    admin_user_id = admin.get("user_id") or current_user_id(admin)

    from db.connection import get_connection
    from tools.knowledge_layers import reset_industry_for_rebuild
    import json as _json

    conn = get_connection()
    try:
        cur = conn.cursor()
        affected = []

        if data.category:
            # 品类级 rebuild
            cur.execute(
                "SELECT id, knowledge FROM industry_knowledge WHERE level='category' AND industry=%s AND category=%s",
                (data.industry, data.category)
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, f"未找到 L2 条目: {data.industry}/{data.category}")
            knowledge = row["knowledge"]
            if isinstance(knowledge, str):
                knowledge = _json.loads(knowledge)
            new_knowledge = reset_industry_for_rebuild(knowledge, admin_user_id, data.reason)
            cur.execute(
                """UPDATE industry_knowledge
                   SET knowledge = %s, version = version + 1, generated_at = NOW()
                   WHERE id = %s""",
                (_json.dumps(new_knowledge, ensure_ascii=False), row["id"])
            )
            affected.append({"level": "category", "industry": data.industry, "category": data.category})
        else:
            # 行业级 rebuild: L1 + 所有 L2
            cur.execute(
                "SELECT id, level, category, knowledge FROM industry_knowledge WHERE industry=%s",
                (data.industry,)
            )
            rows = cur.fetchall()
            if not rows:
                raise HTTPException(404, f"未找到行业: {data.industry}")
            for r in rows:
                k = r["knowledge"]
                if isinstance(k, str):
                    k = _json.loads(k)
                new_k = reset_industry_for_rebuild(k, admin_user_id, data.reason)
                cur.execute(
                    """UPDATE industry_knowledge
                       SET knowledge = %s, version = version + 1, generated_at = NOW()
                       WHERE id = %s""",
                    (_json.dumps(new_k, ensure_ascii=False), r["id"])
                )
                affected.append({"level": r["level"], "industry": data.industry, "category": r["category"]})
        conn.commit()
        return {
            "success": True,
            "rebuilt_count": len(affected),
            "affected": affected,
            "reason": data.reason,
            "warning": (
                f"已清空 {len(affected)} 条 L1/L2 公共素材(归档到 _v37_meta.archived_history)。"
                "同行业用户下次 deep_analyze 触发会全量重采(类似首次拓荒成本)。"
            ),
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"重建失败: {str(e)}")
    finally:
        conn.close()


@router.get("/industry-knowledge/stats", summary="行业知识库飞轮监控(v3.6)")
async def admin_industry_knowledge_stats(request: Request):
    """
    监控 industry_knowledge 表的飞轮起飞情况:
      - 总条目数 (L1/L2 分别)
      - 总 search_count(被命中的总次数,反映复用程度)
      - 总 correction_count(用户矫正总数)
      - top 10 热门行业(按 search_count)
      - corrections 概况(总/未回滚/已回滚)

    用于运营观察 v3.6 飞轮起飞情况(同行业第 N 用户复用度)。
    """
    _require_admin(request)
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()

        # 1. 总条目 + 命中复用统计
        cur.execute("""
            SELECT level,
                   COUNT(*) AS entries,
                   COALESCE(SUM(search_count), 0) AS total_hits,
                   COALESCE(SUM(correction_count), 0) AS total_corrections,
                   MIN(generated_at) AS oldest,
                   MAX(generated_at) AS newest
            FROM industry_knowledge
            GROUP BY level
            ORDER BY level
        """)
        by_level = []
        for r in cur.fetchall():
            by_level.append({
                "level": r["level"],
                "entries": r["entries"],
                "total_hits": int(r["total_hits"] or 0),
                "total_corrections": int(r["total_corrections"] or 0),
                "oldest": r["oldest"].isoformat() if r["oldest"] else None,
                "newest": r["newest"].isoformat() if r["newest"] else None,
            })

        # 2. top 10 热门行业(按 search_count,反映飞轮起飞最猛的行业)
        cur.execute("""
            SELECT level, industry, category, search_count, correction_count, generated_at, expires_at
            FROM industry_knowledge
            WHERE search_count > 0
            ORDER BY search_count DESC
            LIMIT 10
        """)
        top_hits = [
            {
                "level": r["level"],
                "industry": r["industry"],
                "category": r["category"],
                "search_count": r["search_count"],
                "correction_count": r["correction_count"],
                "generated_at": r["generated_at"].isoformat() if r["generated_at"] else None,
                "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
                "is_expired": (r["expires_at"] < datetime.now()) if r["expires_at"] else False,
            }
            for r in cur.fetchall()
        ]

        # 3. corrections 概况
        cur.execute("""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE rolled_back_at IS NULL) AS active,
                   COUNT(*) FILTER (WHERE rolled_back_at IS NOT NULL) AS rolled_back
            FROM industry_knowledge_corrections
        """)
        c_row = cur.fetchone()
        corrections_summary = {
            "total": int(c_row["total"] or 0),
            "active": int(c_row["active"] or 0),
            "rolled_back": int(c_row["rolled_back"] or 0),
        }

        # 4. 飞轮指标: 平均每条目被多少用户复用(L1 期望 >> 1 才算飞轮起飞)
        l1_stats = next((s for s in by_level if s["level"] == "industry"), None)
        l2_stats = next((s for s in by_level if s["level"] == "category"), None)
        flywheel = {
            "l1_avg_reuse": round(l1_stats["total_hits"] / l1_stats["entries"], 2) if l1_stats and l1_stats["entries"] else 0,
            "l2_avg_reuse": round(l2_stats["total_hits"] / l2_stats["entries"], 2) if l2_stats and l2_stats["entries"] else 0,
            "interpretation": (
                "L1 平均复用 N 次 = 同行业有 N+1 个用户共享(N 次拓荒后命中) - "
                "目标 > 5 表明飞轮起飞,< 2 表明拓荒阶段"
            ),
        }

        return {
            "success": True,
            "by_level": by_level,
            "top_hits": top_hits,
            "corrections_summary": corrections_summary,
            "flywheel": flywheel,
        }
    finally:
        conn.close()


# ==========================================
# [CTO-15.23 2026-05-12 P1] 监测达标 backfill(全面修复 BUG)
# ==========================================

class BackfillComplianceRequest(BaseModel):
    quote_id: Optional[int] = Field(default=None, description="单 quote · None 刷全部")


@router.post("/monitoring/backfill-compliance")
async def admin_backfill_compliance(req: BackfillComplianceRequest, request: Request):
    """用当前 quote.tier 重算 keyword_compliance_log 历史 row 的 target_rate + is_compliant

    场景:
      - cron 历史跑时 tier 不同 / target_rate 漂移 / UI 显示矛盾(75% 显示未达标 / 32.1% 显示达标)
      - 老板手动触发刷新 · 立刻让 UI 与当前 tier 一致
    """
    admin = _require_admin(request)
    from db.monitoring_db import backfill_compliance_log_with_current_tier

    result = backfill_compliance_log_with_current_tier(quote_id=req.quote_id)

    create_audit_log(
        user_id=admin["user_id"],
        username=admin["username"],
        action="backfill_compliance",
        module="monitoring",
        entity_type="keyword_compliance_log",
        entity_id=req.quote_id or 0,
        summary=f"重算达标 log: quote={req.quote_id or 'ALL'} processed={result['processed']} updated={result['updated']}",
        before=None,
        after=result,
    )
    return {"success": True, **result}


# ==========================================
# 用户经营档案 (2026-06-05)
# 聚合 7 段画像 + 上级归属/报价系数/进货系数 admin 管理
# ==========================================

@router.get("/users/{user_id}/business-profile", summary="用户经营档案(7 段聚合)")
async def admin_get_user_business_profile(user_id: int, request: Request):
    """管理员查看用户经营档案:身份/归属关系/价格系数(含进货·admin only)/客户品牌/钱包/权限协议/审计。"""
    _require_admin(request)
    base_user = get_user(user_id)
    if not base_user:
        raise HTTPException(status_code=404, detail="用户不存在")

    from services.admin_business_profile import build_business_profile
    profile = build_business_profile(user_id, base_user)
    return {"success": True, **profile}


@router.put("/users/{user_id}/upstream-service-provider", summary="已停用：旧混合关系写入口")
async def admin_set_upstream_service_provider(
    user_id: int, request: Request
):
    """旧混合关系写入口已停用，不再同时改邀请归属与商业绑定。"""
    _require_admin(request)
    raise HTTPException(
        status_code=410,
        detail={
            "code": "AMBIGUOUS_RELATIONSHIP_WRITER_RETIRED",
            "message": "邀请归属为历史事实；商业服务归属请使用独立 CAS API",
            "migrate_to": "/api/admin/user-governance/users/{user_id}/commercial-service-binding",
        },
    )


def _assert_service_provider(user_id: int):
    """[P1] 定价覆盖(报价系数/进货系数)仅适用于服务商(agent_level>=1)· 防普通客户被配服务商价(与 pricing-coef override 身份校验口径一致)"""
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT agent_level FROM user_wallets WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        lvl = int((row["agent_level"] if isinstance(row, dict) else row[0]) or 0) if row else 0
    finally:
        conn.close()
    if lvl < 1:
        raise HTTPException(status_code=400, detail="定价配置仅适用于服务商(agent_level>=1)· 该用户非服务商,请先调整其身份")


@router.put("/users/{user_id}/quote-markup", summary="admin 强制覆盖报价系数")
async def admin_set_quote_markup(user_id: int, req: SetQuoteMarkupRequest, request: Request):
    """admin 强制覆盖用户报价系数(写 agent_pricing_overrides.quote_markup_override · ratio∈[1.0,5.0])。"""
    admin = _require_admin(request)

    target = get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    _assert_service_provider(user_id)  # [P1] 报价系数覆盖仅服务商

    from services.agent_pricing_overrides import get_agent_quote_markup_override, upsert_agent_pricing_override

    old_override = get_agent_quote_markup_override(user_id)

    upsert_agent_pricing_override(
        user_id,
        quote_markup_override=req.quote_markup_ratio,
        note=req.reason,
        admin_user_id=admin["user_id"],
    )

    create_audit_log(
        user_id=admin["user_id"],
        username=admin["username"],
        action="update",
        module="admin_user_profile",
        entity_type="user",
        entity_id=user_id,
        summary=f"覆盖报价系数: {old_override} → {req.quote_markup_ratio} · 原因: {req.reason}",
        before={"quote_markup_override": old_override},
        after={"quote_markup_override": req.quote_markup_ratio, "reason": req.reason},
        ip_address=_get_client_ip(request),
    )
    return {"success": True, "quote_markup_override": req.quote_markup_ratio}


@router.put("/users/{user_id}/purchase-pricing-override", summary="admin 覆盖进货系数(前台绝不暴露)")
async def admin_set_purchase_pricing_override(
    user_id: int, req: SetPurchasePricingOverrideRequest, request: Request
):
    """admin 强制覆盖用户进货折扣系数(agent_pricing_overrides.wholesale_numer/denom)。

    进货系数仅平台后台可见,前台绝不暴露。约束: numer/denom>0 且 numer<=denom(进货折扣<=1)。
    """
    admin = _require_admin(request)

    target = get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    _assert_service_provider(user_id)  # [P1] 进货系数覆盖仅服务商

    if req.wholesale_numer <= 0 or req.wholesale_denom <= 0:
        raise HTTPException(status_code=400, detail="进货系数 numer/denom 必须 > 0")
    if req.wholesale_numer > req.wholesale_denom:
        raise HTTPException(status_code=400, detail="进货折扣必须 <= 1(wholesale_numer <= wholesale_denom)")
    # [复检 low] 对齐 admin_factory_api 既有护栏 0.3<=ratio<=1.0 · 防经本端点写入极端折扣绕过下限
    if req.wholesale_numer / req.wholesale_denom < 0.3:
        raise HTTPException(status_code=400, detail="进货折扣不得低于 0.3(防极端折扣 · 与平台 admin 定价护栏一致)")

    from services.agent_pricing_overrides import (
        AgentPricingOverrideVersionConflict,
        write_agent_pricing_override_atomic,
    )

    request_id = (
        getattr(request.state, "request_id", None)
        or request.headers.get("X-Request-ID")
        or uuid.uuid4().hex
    )
    try:
        result = write_agent_pricing_override_atomic(
            user_id,
            wholesale_numer=req.wholesale_numer,
            wholesale_denom=req.wholesale_denom,
            note=req.reason,
            expected_catalog_version=req.expected_catalog_version,
            admin_user_id=admin["user_id"],
            admin_username=admin.get("username"),
            request_id=request_id,
            ip_address=_get_client_ip(request),
            audit_module="admin_user_purchase_pricing_override",
        )
    except AgentPricingOverrideVersionConflict as exc:
        raise HTTPException(
            409,
            detail={
                "code": "CATALOG_VERSION_CONFLICT",
                "current_catalog_version": exc.current_catalog_version,
            },
        ) from exc
    return {
        "success": True,
        "wholesale_numer": req.wholesale_numer,
        "wholesale_denom": req.wholesale_denom,
        "catalog_version": result["catalog_version"],
        "request_id": result["request_id"],
    }


@router.post("/channel-tier/bonus-grants/{grant_id}/renew", summary="admin 续期渠道奖励 grant")
async def admin_renew_channel_tier_bonus_grant(
    grant_id: int,
    req: RenewChannelTierGrantRequest,
    request: Request,
):
    """admin-only 续期渠道奖励 grant。

    owner_type/owner_id 是可选越权护栏:传入时必须与 grant 归属完全一致。
    """
    admin = _require_admin(request)

    from db.connection import get_db
    from services.bonus_grants import renew_grant
    from services.channel_tier import is_channel_tier_enabled

    with get_db() as conn:
        cursor = conn.cursor()
        if not is_channel_tier_enabled(cursor):
            raise HTTPException(status_code=400, detail="渠道激励功能未开启")

        try:
            grant = renew_grant(
                cursor,
                int(grant_id),
                extra_months=req.extra_months,
                owner_type=req.owner_type,
                owner_id=req.owner_id,
            )
        except ValueError as exc:
            conn.rollback()
            raise HTTPException(status_code=400, detail=str(exc))
        conn.commit()

    create_audit_log(
        user_id=admin["user_id"],
        username=admin["username"],
        action="update",
        module="channel_tier",
        entity_type="bonus_grant",
        entity_id=grant_id,
        summary=f"续期渠道奖励 grant: {grant_id} +{req.extra_months}个月",
        before=None,
        after={
            "grant_id": grant_id,
            "extra_months": req.extra_months,
            "owner_type": req.owner_type,
            "owner_id": req.owner_id,
        },
        ip_address=_get_client_ip(request),
    )

    expires_at = grant.get("expires_at")
    renewed_at = grant.get("renewed_at")
    return {
        "success": True,
        "grant": {
            "id": grant.get("id"),
            "owner_type": grant.get("owner_type"),
            "owner_id": grant.get("owner_id"),
            "expires_at": expires_at.isoformat() if hasattr(expires_at, "isoformat") else expires_at,
            "renewed_at": renewed_at.isoformat() if hasattr(renewed_at, "isoformat") else renewed_at,
        },
    }


@router.get("/pricing/channel-tier-config", summary="渠道等级动态配置")
async def admin_get_channel_tier_config(request: Request):
    _require_admin(request)
    from services.channel_tier_admin import channel_tier_config_snapshot
    from config.pricing_config import get_agent_purchase_catalog_version, merge_pricing_config
    from db.connection import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pg_advisory_xact_lock_shared(920713, 1)")
        cursor.execute("SELECT value FROM system_settings WHERE key='pricing_config'")
        row = cursor.fetchone()
        raw = row.get("value") if isinstance(row, dict) else (row[0] if row else {})
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                raw = {}
        effective = merge_pricing_config(raw if isinstance(raw, dict) else {})
        config = channel_tier_config_snapshot(effective)
        catalog_version = get_agent_purchase_catalog_version(effective)

    from config.pricing_config import get_pricing_env_override
    env = get_pricing_env_override() or {}
    keys = sorted({"agent_tier_config", "founding", "bonus_validity_months", "k_default", "margin_label_thresholds"}.intersection(env))
    return {
        "success": True,
        "config": config,
        "catalog_version": catalog_version,
        "environment_override": {
            "active": bool(keys), "keys": keys,
            "message": "环境配置覆盖中，渠道奖励配置只读" if keys else "",
        },
    }


@router.put("/pricing/channel-tier-config", summary="保存渠道等级动态配置")
async def admin_put_channel_tier_config(req: ChannelTierConfigRequest, request: Request):
    admin = _require_admin(request)
    from db.connection import get_db
    from services.channel_tier_admin import ChannelTierVersionConflict, write_channel_tier_config

    payload = req.model_dump(exclude_none=True)
    expected_catalog_version = payload.pop("expected_catalog_version")
    from config.pricing_config import get_pricing_env_override
    overridden = sorted(
        {"agent_tier_config", "founding", "bonus_validity_months", "k_default", "margin_label_thresholds"}
        .intersection(get_pricing_env_override() or {})
    )
    if overridden:
        raise HTTPException(
            423,
            detail={"code": "ENV_OVERRIDE_ACTIVE", "keys": overridden,
                    "message": "环境配置覆盖中，渠道奖励配置无法保存"},
        )
    request_id = (
        getattr(request.state, "request_id", None)
        or request.headers.get("X-Request-ID")
        or uuid.uuid4().hex
    )
    with get_db() as conn:
        cursor = conn.cursor()
        try:
            result = write_channel_tier_config(
                cursor,
                payload,
                expected_catalog_version=expected_catalog_version,
            )
            from services.config_epoch import read_config_epoch_strict
            if read_config_epoch_strict(cursor) != int(result["committed_epoch"]):
                raise RuntimeError("配置纪元回读不一致")
            from services.agent_inventory_pricing import insert_pricing_audit
            insert_pricing_audit(
                cursor,
                admin_user_id=admin["user_id"],
                admin_username=admin.get("username"),
                request_id=request_id,
                module="channel_tier",
                summary="更新渠道奖励配置",
                before_config=result["previous_config"],
                after_config=result["config"],
                previous_catalog_version=result["previous_catalog_version"],
                catalog_version=result["catalog_version"],
                ip_address=_get_client_ip(request),
            )
            conn.commit()
        except ChannelTierVersionConflict as exc:
            conn.rollback()
            raise HTTPException(
                409,
                detail={
                    "code": "CATALOG_VERSION_CONFLICT",
                    "current_catalog_version": exc.current_catalog_version,
                },
            )
        except ValueError as exc:
            conn.rollback()
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception:
            conn.rollback()
            raise

    # write_channel_tier_config 已在同一事务推进目录 revision 与配置纪元；这里只清本进程缓存。
    from config.pricing_config import invalidate_pricing_config_cache
    from services.config_epoch import accept_committed_epoch
    accept_committed_epoch(int(result["committed_epoch"]))
    invalidate_pricing_config_cache()
    return {
        "success": True,
        "config": result["config"],
        "catalog_version": result["catalog_version"],
        "request_id": request_id,
    }


@router.get("/channel-tier/agents", summary="渠道等级代理列表")
async def admin_list_channel_tier_agents(
    request: Request,
    limit: int = 50,
    offset: int = 0,
):
    _require_admin(request)
    from db.connection import get_db
    from services.channel_tier_admin import list_channel_tier_agents

    with get_db() as conn:
        cursor = conn.cursor()
        items = list_channel_tier_agents(cursor, limit=max(1, min(int(limit or 50), 200)), offset=max(0, int(offset or 0)))
    return {"success": True, "items": jsonable_encoder(items)}


@router.get("/channel-tier/founder-seats", summary="创始席位概览")
async def admin_channel_tier_founder_seats(request: Request):
    _require_admin(request)
    from db.connection import get_db
    from services.channel_tier_admin import founder_seat_summary

    with get_db() as conn:
        cursor = conn.cursor()
        summary = founder_seat_summary(cursor)
    return {"success": True, **summary}


@router.get("/channel-tier/history", summary="渠道等级变更历史")
async def admin_channel_tier_history(request: Request, limit: int = 100):
    _require_admin(request)
    from db.connection import get_db
    from services.channel_tier_admin import channel_tier_history

    with get_db() as conn:
        cursor = conn.cursor()
        items = channel_tier_history(cursor, limit=max(1, min(int(limit or 100), 300)))
    return {"success": True, "items": jsonable_encoder(items)}


@router.get("/channel-tier/agents/{agent_user_id}/history", summary="单代理渠道等级历史")
async def admin_channel_tier_agent_history(agent_user_id: int, request: Request, limit: int = 100):
    _require_admin(request)
    from db.connection import get_db
    from services.channel_tier_admin import channel_tier_history

    with get_db() as conn:
        cursor = conn.cursor()
        items = channel_tier_history(cursor, agent_user_id=int(agent_user_id), limit=max(1, min(int(limit or 100), 300)))
    return {"success": True, "agent_user_id": agent_user_id, "items": jsonable_encoder(items)}


@router.post("/channel-tier/agents/{agent_user_id}/evaluate", summary="手动重算渠道等级")
async def admin_evaluate_channel_tier_agent(agent_user_id: int, request: Request):
    admin = _require_admin(request)
    from db.connection import get_db
    from services.channel_tier import evaluate_and_apply_tier

    with get_db() as conn:
        cursor = conn.cursor()
        try:
            result = evaluate_and_apply_tier(
                cursor,
                int(agent_user_id),
                trigger_source="admin_manual",
                note=f"admin:{admin['user_id']} manual evaluate",
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    create_audit_log(
        user_id=admin["user_id"],
        username=admin["username"],
        action="evaluate",
        module="channel_tier",
        entity_type="agent_channel_tier_state",
        entity_id=agent_user_id,
        summary=f"手动重算渠道等级: agent={agent_user_id}",
        before=None,
        after=jsonable_encoder(result),
        ip_address=_get_client_ip(request),
    )
    return {"success": True, "state": jsonable_encoder(result)}


@router.post("/channel-tier/agents/{agent_user_id}/tier", summary="人工覆盖渠道等级")
async def admin_set_channel_tier_override(
    agent_user_id: int,
    req: SetChannelTierOverrideRequest,
    request: Request,
):
    admin = _require_admin(request)
    from db.connection import get_db
    from services.channel_tier_admin import set_agent_tier_override

    if not req.clear and not req.tier:
        raise HTTPException(status_code=400, detail="请选择要覆盖的等级或勾选清空覆盖")

    with get_db() as conn:
        cursor = conn.cursor()
        try:
            result = set_agent_tier_override(
                cursor,
                int(agent_user_id),
                tier=req.tier,
                clear=req.clear,
                until=req.until,
                note=req.note,
                admin_user_id=int(admin["user_id"]),
            )
            conn.commit()
        except ValueError as exc:
            conn.rollback()
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception:
            conn.rollback()
            raise

    create_audit_log(
        user_id=admin["user_id"],
        username=admin["username"],
        action="update",
        module="channel_tier",
        entity_type="agent_channel_tier_state",
        entity_id=agent_user_id,
        summary=f"{'清空' if req.clear else '设置'}渠道等级人工覆盖: agent={agent_user_id}",
        before=None,
        after=jsonable_encoder(result),
        ip_address=_get_client_ip(request),
    )
    return {"success": True, "state": jsonable_encoder(result)}


@router.post("/pricing/default-sku-packages/seed", summary="初始化渠道默认算力包")
async def admin_seed_default_channel_sku_packages(request: Request):
    admin = _require_admin(request)
    from db.connection import get_db
    from services.channel_tier_admin import seed_default_channel_sku_packages

    with get_db() as conn:
        cursor = conn.cursor()
        try:
            result = seed_default_channel_sku_packages(cursor)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    create_audit_log(
        user_id=admin["user_id"],
        username=admin["username"],
        action="seed",
        module="pricing",
        entity_type="sku_templates",
        entity_id=0,
        summary=f"初始化渠道默认算力包: {len(result.get('seeded') or [])} 个",
        before=None,
        after=jsonable_encoder(result),
        ip_address=_get_client_ip(request),
    )
    return {"success": True, **result}


# ========== [B4-2] 飞轮行业引用格局 → 定价影响 shadow 报告(放行门·只读) ==========
@router.get("/pricing-shadow/flywheel-landscape", summary="[B4-2] 飞轮行业引用格局定价影响 shadow 报告(只读·不写缓存·不翻flag)")
async def admin_flywheel_landscape_shadow(request: Request, industry: Optional[str] = None):
    """开 PRICING_P0D_TRUST_ASSET_ENABLED 前的放行门:各行业引用集中度 → 篇数难度因子 → 受影响在售词数。
    只算不落库,不写价格缓存,不翻 flag。"""
    _require_admin(request)
    import asyncio
    from services.pricing_shadow_report import build_flywheel_landscape_shadow_report
    data = await asyncio.to_thread(lambda: build_flywheel_landscape_shadow_report(industry=industry))
    return {"success": True, "data": data}


# ========== [P1-3 媒体形态分档 2026-08-14] media_domain_directory 治理入口 ==========
# 该表建于迁移 026,此前**没有任何 HTTP 写入口**(source='admin' 档只有 DB 约束)。
# 形态档(media_form)是发布渲染层的分档判据,必须能人工订正 —— 蒸馏 upsert 带
# WHERE source <> 'admin',人工订正后不会被 LLM 覆盖(既有语义,不另造)。

class MediaDirectoryPatchRequest(BaseModel):
    zh_name: Optional[str] = Field(default=None, max_length=120)
    one_liner: Optional[str] = Field(default=None, max_length=400)
    media_form: Optional[str] = Field(default=None, max_length=24)


@router.get("/media-domain-directory", summary="[P1-3] 媒体域名目录(含形态档)")
def admin_list_media_domain_directory(request: Request, query: Optional[str] = None, limit: int = 100):
    _require_admin(request)
    from db.connection import get_db
    limit = max(1, min(500, int(limit or 100)))
    term = str(query or "").strip().lower()
    where = "WHERE domain ILIKE %s OR zh_name ILIKE %s" if term else ""
    like_params = (f"%{term}%", f"%{term}%") if term else ()
    with get_db() as conn:
        c = conn.cursor()
        try:
            c.execute(
                "SELECT domain, zh_name, one_liner, source, COALESCE(media_form,'') AS media_form, updated_at "
                f"  FROM media_domain_directory {where} "
                " ORDER BY (zh_name = '') ASC, domain LIMIT %s",
                (*like_params, limit),
            )
        except Exception:
            # media_form 列未建(迁移未跑)→ 按旧列集返回,form 恒空(O1)。
            conn.rollback()
            c.execute(
                "SELECT domain, zh_name, one_liner, source, '' AS media_form, updated_at "
                f"  FROM media_domain_directory {where} "
                " ORDER BY (zh_name = '') ASC, domain LIMIT %s",
                (*like_params, limit),
            )
        rows = [dict(r) for r in c.fetchall() or []]
    return {"success": True, "entries": jsonable_encoder(rows), "total": len(rows)}


@router.patch("/media-domain-directory/{domain}", summary="[P1-3] 订正媒体目录条目(形态档/中文名)")
def admin_patch_media_domain_directory(domain: str, req: MediaDirectoryPatchRequest, request: Request):
    admin = _require_admin(request)
    from services.media_form_adaptation import VALID_MEDIA_FORMS, normalize_media_form
    from db.connection import get_db

    dom = str(domain or "").strip().lower()
    if dom.startswith("www."):
        dom = dom[4:]
    if not dom or "." not in dom:
        raise HTTPException(status_code=422, detail="域名不合法")
    form_raw = None if req.media_form is None else str(req.media_form).strip().lower()
    if form_raw:
        if normalize_media_form(form_raw) != form_raw:
            raise HTTPException(
                status_code=422,
                detail=f"media_form 只接受:{'、'.join(sorted(VALID_MEDIA_FORMS))},或空串清除分档。",
            )

    sets, params = [], []
    if req.zh_name is not None:
        sets.append("zh_name = %s")
        params.append(str(req.zh_name).strip())
    if req.one_liner is not None:
        sets.append("one_liner = %s")
        params.append(str(req.one_liner).strip())
    if req.media_form is not None:
        sets.append("media_form = %s")
        params.append(form_raw or None)
    if not sets:
        raise HTTPException(status_code=422, detail="至少提供一个要订正的字段")
    # 人工订正即定档:source 升为 admin,蒸馏 upsert(WHERE source<>'admin')不再覆盖。
    sets.append("source = 'admin'")
    sets.append("updated_at = NOW()")

    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT domain, zh_name, one_liner, source FROM media_domain_directory WHERE domain = %s", (dom,))
        before = c.fetchone()
        if before is None:
            # 目录还没有这个域名:INSERT(admin 直录,常见于分档先行于蒸馏)。
            c.execute(
                "INSERT INTO media_domain_directory (domain, zh_name, one_liner, source, media_form) "
                "VALUES (%s, %s, %s, 'admin', %s)",
                (dom, str(req.zh_name or "").strip(), str(req.one_liner or "").strip(), form_raw or None),
            )
        else:
            c.execute(
                f"UPDATE media_domain_directory SET {', '.join(sets)} WHERE domain = %s",
                (*params, dom),
            )
        conn.commit()

    create_audit_log(
        user_id=admin["user_id"], username=admin["username"],
        action="update", module="media", entity_type="media_domain_directory", entity_id=0,
        summary=f"订正媒体目录 {dom}: form={form_raw!r} zh_name={req.zh_name!r}",
        before=jsonable_encoder(dict(before) if before else None), after=jsonable_encoder(req.model_dump()),
        ip_address=_get_client_ip(request),
    )
    return {"success": True, "domain": dom, "media_form": form_raw or ""}
