"""报价经营包(GEO 域)API · 持久化 + 身份投影 + 客户公开分享 token(2026-06-17)。

设计口径: docs/AI-CONTEXT/PRICING_OPERATION_PACKAGES_PLAN_2026-06-16.md
本批(持久化 + 前端):
  - 服务商(agent_level>=1)拥有自己的经营包行(首访按默认模板 seed-copy · 可编辑/启用/排序/复制/软删)。
  - 普通用户/Admin 读只读默认模板投影(本批不持久化普通用户)。
  - 客户公开视图 /public?token=XXX:有效 token→200+客户白名单字段;无效 token→404;过期→安全过期态;测试账号不外露。
身份边界(§8):平台/Admin → 全字段;服务商 → 成本口径+下级建议+经营空间+管理字段;
  普通用户 → 自己成本+建议售价+毛利;客户公开 → 仅客户白名单(无成本/毛利/系数/id/owner)。
鉴权:登录态写端点未登录 401 / 越权 403 / 对象不存在 404 / 非服务商管理 403。
红线:未碰 auth/middleware.py —— 客户 token 走 query 参数(路径仍为已白名单的 /public)。
"""
import logging
import re
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from tools.operation_packages import (
    ECONOMICS_NOTE,
    project_packages,
    project_persisted_for_agent,
    project_persisted_for_customer,
)
from db.operation_packages_db import (
    copy_package,
    ensure_seeded,
    get_or_create_share_token,
    get_package,
    list_packages as db_list_packages,
    reorder_packages,
    resolve_share_token,
    soft_delete_package,
    update_package,
)

logger = logging.getLogger("GEO-OperationPackages")

router = APIRouter(prefix="/api/operation-packages", tags=["报价经营包"])

# 测试账号识别(M3 §5 口径):名字含 测试|test|_demo|_test|验收 → 不外露给客户公开链接
_TEST_NAME_RE = re.compile(r"测试|验收|_demo|_test|\btest\b|\bdemo\b", re.IGNORECASE)


def _agent_level(user_id) -> int:
    """查 user_wallets.agent_level(全仓既定模式 · request.state.user 不带此字段)。
    查不到/异常 → 0(普通用户 · fail-closed 到最小权限)。"""
    if not user_id:
        return 0
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT agent_level FROM user_wallets WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            return 0
        lvl = row.get("agent_level") if isinstance(row, dict) else row[0]
        return int(lvl or 0)
    except Exception as exc:
        logger.debug("agent_level 查询失败(降级普通用户): %s", exc)
        return 0


def _role_for(request: Request):
    """登录态 → role 字符串。未登录返回 None(调用方决定 401)。role ∈ {'admin','agent','normal'}。"""
    user = getattr(request.state, "user", None)
    if not user:
        return None
    if user.get("is_admin"):
        return "admin"
    if _agent_level(user.get("user_id")) >= 1:
        return "agent"
    return "normal"


def _get_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def _uid(user: dict):
    return user.get("user_id") or user.get("id")


def _require_agent(request: Request) -> dict:
    """要求登录 + (agent_level>=1 或 admin),否则 401 / 403。管理经营包专用。"""
    user = _get_user(request)
    if not (user.get("is_admin") or _agent_level(_uid(user)) >= 1):
        raise HTTPException(status_code=403, detail="仅服务方可管理经营包")
    return user


def _is_test_owner(user: dict) -> bool:
    for f in ("username", "display_name"):
        v = user.get(f)
        if v and _TEST_NAME_RE.search(str(v)):
            return True
    return False


def _authorize_own_package(request: Request, pkg_id: int) -> tuple:
    """登录 + 服务商 + 取目标包并校验归属。返回 (user, pkg)。
    未登录 401 / 非服务商 403 / 不存在 404 / 非己 403。"""
    user = _require_agent(request)
    pkg = get_package(pkg_id)
    if not pkg:
        raise HTTPException(status_code=404, detail="经营包不存在")
    if not user.get("is_admin") and pkg.get("owner_user_id") != _uid(user):
        raise HTTPException(status_code=403, detail="无权操作此经营包")
    return user, pkg


# ========== 请求模型 ==========
class PackageUpdate(BaseModel):
    name: Optional[str] = None
    scope: Optional[str] = None
    fit_scene: Optional[str] = None
    customer_copy: Optional[str] = None
    search_item_min: Optional[int] = None
    search_item_max: Optional[int] = None
    customer_price_min: Optional[int] = None
    customer_price_max: Optional[int] = None
    enabled: Optional[bool] = None
    sort_order: Optional[int] = None


class ReorderBody(BaseModel):
    ids: List[int]


# ========== 登录态:列表(首访 seed) ==========
@router.get("")
async def list_packages(request: Request):
    """登录态:按身份返回经营包。服务商=持久化行(首访 seed-copy)·普通/Admin=只读默认投影。未登录 401。"""
    role = _role_for(request)
    if role is None:
        raise HTTPException(status_code=401, detail="未登录")
    user = request.state.user
    uid = _uid(user)
    if role == "agent":
        ensure_seeded(uid)
        rows = db_list_packages(uid)
        packages = [project_persisted_for_agent(r) for r in rows]
    elif role == "admin":
        packages = project_packages("admin")
    else:  # normal
        packages = project_packages("normal")
    return {
        "success": True,
        "role": role,
        "persisted": role == "agent",
        "packages": packages,
        "economics_note": ECONOMICS_NOTE if role in ("agent", "normal") else None,
        "note": "经营包为算价/展示模板, 启用不扣费;真实扣费走现有算力。预计成本/毛利为测算展示。",
    }


# ========== 登录态:批量排序 / 生成分享 token(字面路径 · 放在 /{pkg_id} 前) ==========
@router.post("/reorder")
async def reorder_ep(body: ReorderBody, request: Request):
    """服务商:按 id 顺序重排自己的经营包。"""
    user = _require_agent(request)
    n = reorder_packages(_uid(user), body.ids)
    return {"success": True, "updated": n}


@router.post("/share-token")
async def share_token_ep(request: Request):
    """服务商:取/建客户公开分享 token(幂等·一个 active)。先 seed 保证链接有内容。"""
    user = _require_agent(request)
    uid = _uid(user)
    ensure_seeded(uid)
    tok = get_or_create_share_token(uid, is_test=_is_test_owner(user))
    if not tok:
        raise HTTPException(status_code=500, detail="生成分享链接失败")
    return {
        "success": True,
        "token": tok["token"],
        "share_path": f"/packages/{tok['token']}",
        "is_test": bool(tok.get("is_test")),
    }


# ========== 登录态:单包 编辑 / 复制 / 软删 ==========
@router.patch("/{pkg_id}")
async def update_package_ep(pkg_id: int, body: PackageUpdate, request: Request):
    """服务商:编辑自己的经营包(名称/售价范围/场景/搜索项数/客户文案/启用/排序)。"""
    user, pkg = _authorize_own_package(request, pkg_id)
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    updated = update_package(pkg_id, pkg["owner_user_id"], fields)
    if not updated:
        raise HTTPException(status_code=404, detail="经营包不存在")
    return {"success": True, "package": project_persisted_for_agent(updated)}


@router.post("/{pkg_id}/copy")
async def copy_package_ep(pkg_id: int, request: Request):
    """服务商:复制一个经营包为自定义新行(可改名/改价)。"""
    user, pkg = _authorize_own_package(request, pkg_id)
    new_row = copy_package(pkg_id, pkg["owner_user_id"])
    if not new_row:
        raise HTTPException(status_code=404, detail="经营包不存在")
    return {"success": True, "package": project_persisted_for_agent(new_row)}


@router.delete("/{pkg_id}")
async def delete_package_ep(pkg_id: int, request: Request):
    """服务商:软删自己的经营包。"""
    user, pkg = _authorize_own_package(request, pkg_id)
    ok = soft_delete_package(pkg_id, pkg["owner_user_id"])
    return {"success": bool(ok)}


# ========== 客户公开视图(匿名 · token 走 query 参数 · 路径仍为已白名单 /public) ==========
@router.get("/public")
async def list_packages_public(request: Request, token: Optional[str] = None):
    """客户公开视图:仅客户白名单字段(包名/场景/搜索项数/服务说明/对客售价)。无成本/毛利/系数。
    - 无 token:默认建议价基线(向后兼容原行为)。
    - 有效 token:返回该服务商已启用的经营包 + 其对客售价。
    - 无效 token → 404;过期 → 安全过期态;测试账号 → 404(不外露)。"""
    if not token:
        return {"success": True, "role": "customer", "packages": project_packages("customer")}
    resolved = resolve_share_token(token)
    if not resolved:
        raise HTTPException(status_code=404, detail="链接不存在")
    if resolved.get("expired"):
        return {"success": True, "role": "customer", "expired": True, "packages": []}
    if resolved.get("is_test"):
        raise HTTPException(status_code=404, detail="链接不存在")
    rows = db_list_packages(resolved.get("owner_user_id"), enabled_only=True)
    return {
        "success": True,
        "role": "customer",
        "packages": [project_persisted_for_customer(r) for r in rows],
    }
