"""
品牌访问权限校验工具
在 API 端点中调用，确保非管理员用户只能访问自己分配的品牌数据
"""
from fastapi import Request, HTTPException
from typing import Optional, List
import logging

# 🔴 [R4 ④ 2026-08-20] 这三个符号原来是**函数体内惰性 import**(第 200/218/251 行)。
#   惰性 import 在这里不是省启动时间,是**定时炸弹**:`db.diagnosis_db` 的模块体会执行
#   `init_db()`(该文件末尾),而 init_db 会发 `ALTER TABLE quotes …` 这类 DDL。
#   一旦「本进程第一次 import 它」恰好发生在某个**已经开着的事务**里,
#   那条 ALTER 要 AccessExclusiveLock,却在等同一个线程自己调用栈上方的读事务 ——
#   **单线程自死锁,不超时不报错**。
#   R3 已把这条路径完整取证:portal_token_authority.py:148 → 本文件 :251
#   → diagnosis_db.py:8573 模块体 init_db → :222 → _safe_add_column → ALTER。
#   上提到模块顶层后,这次 import 发生在**没有任何打开事务**的加载期,炸弹拆除。
#   (根上的风险仍是「import 一个模块 = 改 schema」,那是架构级改动,见 R3 finding 建议 3。)
from db.diagnosis_db import get_diagnosis_by_id, get_quote
from auth.user_ctx import current_user_id

logger = logging.getLogger("GEO-BrandAccess")


def _require_organization_artifact(request: Request, artifact_type: str, artifact_id) -> None:
    organization_identity = getattr(request.state, "organization_identity", None)
    if organization_identity is None or not organization_identity.is_member:
        return
    from services.organization_artifacts import require_artifact_access
    from services.organization_contract import OrganizationError
    try:
        require_artifact_access(
            organization_identity, artifact_type=artifact_type, artifact_id=artifact_id
        )
    except OrganizationError as exc:
        request_id = getattr(request.state, "organization_request_id", "unknown")
        raise HTTPException(status_code=exc.http_status, detail=exc.as_detail(request_id)) from exc


def filter_organization_artifact_rows(
    request: Request,
    artifact_type: str,
    rows: list,
    *,
    id_key: str = "id",
) -> list[dict]:
    """HTTP adapter for organization artifact list isolation."""
    organization_identity = getattr(request.state, "organization_identity", None)
    if organization_identity is None or not organization_identity.is_member:
        return [dict(row) for row in rows]
    from services.organization_artifacts import filter_accessible_artifact_rows
    from services.organization_contract import OrganizationError
    try:
        return filter_accessible_artifact_rows(
            organization_identity,
            artifact_type=artifact_type,
            rows=rows,
            id_key=id_key,
        )
    except OrganizationError as exc:
        request_id = getattr(request.state, "organization_request_id", "unknown")
        raise HTTPException(status_code=exc.http_status, detail=exc.as_detail(request_id)) from exc


def _is_brand_owner(user_id: int, brand_id: int) -> bool:
    """查询 brand_id 的 owner_user_id 是否是当前用户（自有品牌）"""
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT owner_user_id FROM brands "
                "WHERE id = %s AND (is_deleted IS NULL OR is_deleted = FALSE)",
                (brand_id,),
            )
            row = cur.fetchone()
            cur.close()
            return bool(row and row.get("owner_user_id") == user_id)
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"_is_brand_owner 查询失败 brand_id={brand_id}: {e}")
        return False


def demo_readable_brand_id(request: Request) -> Optional[int]:
    """[D10 · SSOT v2.0 §8.2] 本请求可**只读**投影的演示授权品牌 id,没有则 None。

    D10 把演示从「冻结快照另做一套视图」改为「真实 handler 的受控实时只读投影」:
    演示用户打开被授权品牌的任何功能面,看到与 owner 相同的当天真实数据。要做到
    这一点,演示授权必须能进入**这一层**(唯一授权层),否则真 handler 会按登录用户
    过滤,演示用户只会看到自己名下(空)的数据,而不是被授权品牌的数据。

    三条硬约束(缺一即不授权,fail-closed):
      1. 请求上必须有中间件登记过的**活跃**演示上下文(`resolve_demo_case_access`
         已校验 grant 有效期/状态);
      2. **只读**:仅 GET/HEAD/OPTIONS。任何写方法一律不授权(演示零写入是 H0);
      3. **精确品牌**:只授权 `context.brand_id` 这一个品牌。owner 名下可能有多个
         品牌,授权范围按**品牌**不按**用户**——绝不因为同属一个 owner 就放行。

    返回值只表示"可只读投影",不代表任何商业权限:不授予写、provider、任务、
    资金、公开凭证签发能力(那些在 §13 写闸与 `assert_side_effects_allowed` 终端
    fence 拒绝)。
    """
    context = getattr(request.state, "demo_access_context", None)
    if context is None:
        return None
    try:
        method = str(request.method or "").upper()
    except Exception:
        return None
    if method not in {"GET", "HEAD", "OPTIONS"}:
        return None
    try:
        brand_id = int(getattr(context, "brand_id", 0) or 0)
    except (TypeError, ValueError):
        return None
    return brand_id if brand_id > 0 else None


def require_brand_access(request: Request, brand_id: Optional[int], allow_null: bool = False) -> None:
    """
    校验当前用户是否有权访问指定 brand_id
    - 管理员：不限制
    - 非管理员 + 有分配客户：只能访问分配的品牌（或自己 owner 的品牌）
    - 非管理员 + 无分配客户：只能访问自己 owner_user_id 的品牌
    - brand_id 为 None + allow_null=False：记录警告并拒绝
    - brand_id 为 None + allow_null=True：跳过校验（用于诊断等无品牌关联的场景）

    Raises: HTTPException(403) if unauthorized

    ⚠️ 安全修复 2026-04-17 (P0-A):
       之前"未分配客户=无限制"是 P0 级漏洞 — L0 用户/刚注册用户 client_brand_ids 为空，
       可用任意 brand_id 访问 69 处 API 读取所有品牌数据。
       修复：无分配时 fallback 到 owner_user_id 校验，只能看自己创建的品牌。
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")

    # 管理员不限制
    if user.get("is_admin"):
        return

    organization_identity = getattr(request.state, "organization_identity", None)
    if organization_identity is not None:
        if brand_id is None:
            if allow_null:
                return
            raise HTTPException(
                status_code=403,
                detail="该资源未关联客户，无法验证组织权限",
                headers={"X-Error-Code": "ORG_BRAND_ID_MISSING"},
            )
        from db.organization_db import assigned_brand_ids
        if int(brand_id) in assigned_brand_ids(organization_identity):
            return
        # [D10] 组织成员同样可以持有演示授权(只读投影),否则"是某团队员工"会
        # 意外剥夺其演示可读性。授权范围仍是那一个被授权品牌。
        if int(brand_id) == demo_readable_brand_id(request):
            return
        raise HTTPException(status_code=404, detail="资源不存在")

    user_id = user.get("user_id") or current_user_id(user)
    allowed_brands: List[int] = _normalize_brand_ids(user.get("client_brand_ids", []))

    if brand_id is None:
        if allow_null:
            return
        logger.warning(
            f"品牌ID为空: user={user.get('username')} 尝试访问未关联品牌的资源"
        )
        raise HTTPException(
            status_code=403,
            detail="该资源未关联品牌，无法校验权限",
            headers={"X-Error-Code": "BRAND_ID_MISSING"}
        )

    # 1. 在分配列表里 → 放行
    if brand_id in allowed_brands:
        return

    # 2. 不在分配列表里，但是用户自己 owner 的品牌 → 放行（C 端用户/个人品牌）
    if user_id and _is_brand_owner(user_id, brand_id):
        return

    # 3. [D10 · SSOT v2.0 §8.2] 演示授权品牌的**只读**投影。
    #    v1.x 时代此处的不变式是「demo 永不进入本授权层,只由 /api/demo-cases 读
    #    冻结快照」;D10 已由 Owner 裁决反转为受控实时只读投影。授权仍然极窄:
    #    只读方法 + 只此一个被授权品牌(见 demo_readable_brand_id 三条硬约束),
    #    跨品牌照旧走下面的拒绝路径,与普通用户同一条代码,不存在演示专用弱检查。
    if brand_id == demo_readable_brand_id(request):
        return

    logger.warning(
        f"品牌访问被拒绝: user={user.get('username')} user_id={user_id} "
        f"requested brand_id={brand_id}, allowed={allowed_brands}"
    )
    raise HTTPException(status_code=404, detail="资源不存在")


def require_diagnosis_access(request: Request, diagnosis_id: int, allow_null: bool = False) -> dict:
    """校验用户是否有权访问指定诊断记录，返回记录数据

    [GEO-R1-CAN-139] 默认改为 allow_null=False(fail-closed)。
    diagnosis_records 无独立 owner 列，归属只能经 brand_id → brands.owner_user_id 反查；
    当 brand_id 为 NULL(brand 关联失败的残缺行)时无任何归属线索可校验，
    之前默认 allow_null=True 会对所有已登录非管理员 fail-open 放行 —
    任意用户可按可枚举 id 读/删/导出他人 NULL-brand 诊断(IDOR)。
    现默认 fail-closed 拒绝；仅 admin 可访问 NULL-brand 残缺行。
    如确有需放行 NULL 的既有调用(如 share token 场景)可显式传 allow_null=True。"""
    record = get_diagnosis_by_id(diagnosis_id)
    if not record:
        raise HTTPException(status_code=404, detail="诊断记录不存在")
    require_brand_access(request, record.get("brand_id"), allow_null=allow_null)
    _require_organization_artifact(request, "diagnosis", diagnosis_id)
    return record


def require_client_material_access(request: Request, diagnosis_id: int) -> dict:
    """Authorize customer material through the assigned customer, not author.

    A diagnosis is an employee output and therefore remains creator-private by
    default. Customer profile/material data is shared operational input for
    every employee assigned to that customer, so reusing
    ``require_diagnosis_access`` here would incorrectly make the input private
    to the diagnosis author.
    """
    record = get_diagnosis_by_id(diagnosis_id)
    if not record:
        raise HTTPException(status_code=404, detail="诊断记录不存在")
    require_brand_access(request, record.get("brand_id"), allow_null=False)
    return record


def require_report_access(request: Request, report_id: int, allow_null: bool = False) -> dict:
    """校验用户是否有权访问指定监测报告，返回报告数据

    [GEO-R1-CAN-141] 新增 allow_null 参数(默认 False · fail-closed)。
    monitoring_reports 无独立 owner 列，归属只能经 brand_id → brands.owner_user_id 反查；
    brand_id 为 NULL 时无归属线索可校验。之前此处硬编码 allow_null=True，
    对所有已登录非管理员 fail-open，任意用户可按可枚举 report_id
    读/改/删/AI 写/发送/导出他人 NULL-brand 报告(IDOR)。
    现默认 fail-closed 拒绝；仅 admin 可访问 NULL-brand 报告。
    如确需放行 NULL 的既有调用可显式传 allow_null=True。"""
    from db.monitoring_db import get_report_by_id
    report = get_report_by_id(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    require_brand_access(request, report.get("brand_id"), allow_null=allow_null)
    _require_organization_artifact(request, "monitoring_report", report_id)
    return report


def require_quote_access(request: Request, quote_id: int, allow_null: bool = True) -> dict:
    """校验用户是否有权访问指定报价单，返回报价单数据

    [audit #2 返修 E4] allow_null 参数(默认 True 兼容既有调用)。敏感读 / 写端点
    (如 /api/publications/{quote_id} 跨租户读投放记录)应传 allow_null=False → NULL-brand
    报价单无法验证归属时 fail-closed 拒绝。"""
    quote = get_quote(quote_id)
    if not quote:
        raise HTTPException(status_code=404, detail="报价单不存在")
    require_brand_access(request, quote.get("brand_id"), allow_null=allow_null)
    _require_organization_artifact(request, "quote", quote_id)
    return quote


def require_profile_access(request: Request, profile_id: str) -> dict:
    """校验用户是否有权访问指定档案，返回档案数据"""
    from db.profile_db import get_profile
    profile = get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="档案不存在")
    require_brand_access(request, profile.get("brand_id"), allow_null=False)
    return profile


def require_project_access(request: Request, project_id: int) -> dict:
    """校验用户是否有权访问指定项目，返回项目数据"""
    from db.social_project_db import get_project
    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    require_brand_access(request, project.get("brand_id"))
    return project


def _normalize_brand_ids(values) -> List[int]:
    ids: List[int] = []
    for value in values or []:
        try:
            brand_id = int(value)
        except (TypeError, ValueError):
            continue
        if brand_id > 0 and brand_id not in ids:
            ids.append(brand_id)
    return ids


def _owned_brand_ids(user_id: Optional[int]) -> List[int]:
    if not user_id:
        return []
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id FROM brands "
                "WHERE owner_user_id = %s AND (is_deleted IS NULL OR is_deleted = FALSE)",
                (user_id,),
            )
            return _normalize_brand_ids([row.get("id") for row in cur.fetchall()])
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"_owned_brand_ids 查询失败 user_id={user_id}: {e}")
        return []


def get_user_brand_filter(request: Request) -> Optional[List[int]]:
    """
    获取用户的品牌过滤列表
    - 管理员：返回 None（不限制）
    - 非管理员 + 有分配客户：返回限定的 brand_id 列表
    - 非管理员 + 无分配客户：返回 [] 空列表（显式隔离，不允许看任何数据）

    用于列表查询 API 自动过滤结果

    ⚠️ 安全修复 2026-04-08:
       之前"未分配客户返回 None"的行为是 P0 级数据泄漏。
       C 端用户刚注册时 client_brand_ids 可能短暂为空，这时必须返回空列表
       而不是不限制，否则能看到所有用户的数据。
    """
    user = getattr(request.state, "user", None)
    if not user:
        # 没有用户上下文 → 给一个不可能存在的 brand_id，什么也看不到
        return [-1]

    if user.get("is_admin"):
        return None  # admin 看全部

    # [D10 · SSOT v2.0 §8.2] 演示授权品牌进入**只读**列表范围。没有这一步,
    # 列表类真 handler 会按登录用户过滤 → 演示用户看到自己名下(空)的数据,
    # 而不是被授权品牌的实时数据,D10「看到与 owner 一样的每天真实数据」落空。
    # 仍然只加**那一个**被授权品牌,且仅只读方法(见 demo_readable_brand_id)。
    demo_brand = demo_readable_brand_id(request)

    organization_identity = getattr(request.state, "organization_identity", None)
    if organization_identity is not None:
        from db.organization_db import assigned_brand_ids
        ids = list(assigned_brand_ids(organization_identity) or [])
        if demo_brand is not None and demo_brand not in ids:
            ids.append(demo_brand)
        return ids or [-1]

    user_id = user.get("user_id") or current_user_id(user)
    allowed_brands = _normalize_brand_ids(user.get("client_brand_ids", []))
    owned_brands = _owned_brand_ids(user_id)
    real_brands = set(allowed_brands) | set(owned_brands)
    if demo_brand is not None:
        real_brands.add(demo_brand)
    filtered_brands = sorted(real_brands)
    if not filtered_brands:
        # 非管理员但无分配品牌 → 返回 [-1] 占位，确保过滤结果为空
        # 不能返回 [] 是因为某些调用方会把 [] 当成"不限制"
        return [-1]

    return filtered_brands
