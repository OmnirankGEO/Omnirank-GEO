"""
GEO 调研监测 - 行业管理 Admin API (里程碑 A.7 Group 1)

功能:
  对 geo_research_industries 表(17 行业)提供 5 个管理接口:
    GET    /industries           列表 (默认仅 active)
    POST   /industries           新建
    PUT    /industries/{id}      编辑 (部分更新)
    DELETE /industries/{id}      软删 (active=FALSE,幂等)
    POST   /industries/reorder   批量改 sort_order (单事务)

权限: 全部需要 admin (request.state.user.is_admin)
SQL: 全部 %s 参数化, 出错统一 rollback + HTTP 500
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from db.connection import get_connection
from auth.user_ctx import current_user_id

logger = logging.getLogger("GEO-ResearchMonitor.IndustryAPI")

router = APIRouter(prefix="/api/admin/research-monitor", tags=["调研监测-行业"])


# UPDATE SET 拼接白名单: 防御未来 Pydantic 模型扩字段时拼到非法列
_ALLOWED_INDUSTRY_UPDATE_FIELDS = {'name', 'active'}


# ========== 工具 ==========

def _user_id(user: dict) -> int:
    """从 `request.state.user` 取用户 id。

    🔴 [#78 2026-09-05] **不能裸读 user 的 id 键**(两种写法都不行):
    鉴权中间件三条路径(`auth/middleware.py` JWT payload / soft-refresh 支 / 门户支)
    放的都是 **`user_id`**,没有一条放 `id` —— 所以裸读 `.get("id")` **恒为 None**、
    裸读 `["id"]` 恒 KeyError。本文件原来 12 处全是前者:`reviewed_by` 永远写不进,
    日志里的 admin_id 永远是 None,而**不报错**,所以一直没人发现。
    """
    return int(user.get("id") or user.get("user_id") or 0)


def _require_admin(request: Request) -> dict:
    """要求管理员身份,返回 user dict."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _row_to_dict(row: dict) -> dict:
    """统一序列化 industry 行(把时间转 isoformat)."""
    if row is None:
        return None
    out = {
        "id": row["id"],
        "name": row["name"],
        "slug": row["slug"],
        "sort_order": row["sort_order"],
        "active": row["active"],
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }
    # P14-v6 (2026-05-27): /industries 加 active_prompt_count 透出 · 跑批 dialog
    # 用它禁用无 prompts 行业 · 显式 .get 保留兼容 (其他 endpoint 复用 _row_to_dict)
    if "active_prompt_count" in row:
        out["active_prompt_count"] = int(row["active_prompt_count"] or 0)
    return out


# ========== 请求模型 ==========

class CreateIndustryRequest(BaseModel):
    """P13-v7 (2026-05-27 老板): 删 slug + sort_order 入参
       - slug 后端自动生成 (ind_<unix_ms> · 满足 DB UNIQUE + ASCII 正则)
       - sort_order 默认 0 (老板说没人调)
       DB 列保留 (避免破坏性 migration · 老 backfill 脚本仍用)
    """
    name: str = Field(..., min_length=1, max_length=100)
    active: bool = True


class UpdateIndustryRequest(BaseModel):
    """P13-v7: 删 slug + sort_order 入参 · 仅允许改 name + active
    (slug 唯一不能改 · sort_order 没业务意义)"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    active: Optional[bool] = None


class ReorderItem(BaseModel):
    id: int = Field(..., ge=1)
    sort_order: int


class ReorderRequest(BaseModel):
    items: List[ReorderItem] = Field(..., max_length=200)


class UpdateAliasIndustryRequest(BaseModel):
    """R2 · admin 改判行业别名归属 (SPEC §3-R3.4 人工兜底)."""
    industry_id: int = Field(..., ge=1)


# ========== 1. GET 列表 ==========

@router.get("/industries", summary="行业列表")
async def list_industries(request: Request, include_inactive: bool = False):
    """
    返回行业列表。
    - 默认仅返回 active=TRUE
    - include_inactive=true 时全返(含已软删)
    - 排序: sort_order ASC, name ASC
    """
    _require_admin(request)

    conn = get_connection()
    try:
        cur = conn.cursor()
        # P14-v6 (2026-05-27 review): 跑批 dialog 要展示每行业 active prompts 数 ·
        # 无 prompts 的行业前端禁用勾选 + 提示 "请先配置 Prompts"
        # LEFT JOIN COUNT 子查询 (避免 LEFT JOIN GROUP BY 重复)
        if include_inactive:
            cur.execute(
                """
                SELECT i.id, i.name, i.slug, i.sort_order, i.active,
                       i.created_at, i.updated_at,
                       COALESCE((
                           SELECT COUNT(*) FROM geo_research_prompts p
                            WHERE p.industry_id = i.id AND p.active = TRUE
                       ), 0) AS active_prompt_count
                FROM geo_research_industries i
                ORDER BY i.sort_order ASC, i.name ASC
                """
            )
        else:
            cur.execute(
                """
                SELECT i.id, i.name, i.slug, i.sort_order, i.active,
                       i.created_at, i.updated_at,
                       COALESCE((
                           SELECT COUNT(*) FROM geo_research_prompts p
                            WHERE p.industry_id = i.id AND p.active = TRUE
                       ), 0) AS active_prompt_count
                FROM geo_research_industries i
                WHERE i.active = TRUE
                ORDER BY i.sort_order ASC, i.name ASC
                """
            )
        rows = cur.fetchall()

        # R2-1 (2026-07-05): 数据新鲜度 · 一次聚合每行业最近一次调研落库时间。
        # geo_research_raw.industry 存的是行业中文名, 与 geo_research_industries.name 字符串对齐
        # (见 diagnosis_db.py COMMENT ON COLUMN geo_research_industries.name), 故按 name 映射。
        # 聚合失败不阻断行业列表 · 降级为全 null (向后兼容: 老环境若无 geo_research_raw 亦不崩)。
        last_research_map: dict = {}
        try:
            cur.execute(
                "SELECT industry, MAX(created_at) AS last_at "
                "FROM geo_research_raw GROUP BY industry"
            )
            for lr in (cur.fetchall() or []):
                ind_name = lr.get("industry") if isinstance(lr, dict) else lr[0]
                last_at = lr.get("last_at") if isinstance(lr, dict) else lr[1]
                if ind_name is not None:
                    last_research_map[ind_name] = last_at.isoformat() if last_at else None
        except Exception as agg_err:
            logger.warning("[行业列表] last_research_at 聚合失败 (降级 null): %s", agg_err)
            try:
                conn.rollback()
            except Exception:
                pass
        cur.close()
    except HTTPException:
        raise
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("[行业列表] 查询失败: %s", e)
        raise HTTPException(status_code=500, detail="查询行业列表失败")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    out_list = []
    for r in rows:
        d = _row_to_dict(r)
        if d is not None:
            # R2-1: 按行业中文名映射最近调研时间 (ISO 或 null)
            d["last_research_at"] = last_research_map.get(r["name"])
        out_list.append(d)
    return {"industries": out_list}


# ========== 2. POST 新建 ==========

@router.post("/industries", summary="新建行业")
async def create_industry(request: Request, payload: CreateIndustryRequest):
    """
    新建行业 · 老板拍板只填 name + active (slug 自动生成)
    name 重复返回 409

    P14-v7 (2026-05-27 review fix): slug 改用 services.research_monitor.industry_registry.slug_for_name
    deterministic md5(name) 前 12 位 · 跟批量 ensure/migration backfill 一套口径 ·
    替换旧毫秒方案 (虽然人工点击同毫秒概率低但应统一收口)
    """
    user = _require_admin(request)
    from services.research_monitor.industry_registry import slug_for_name
    name_clean = payload.name.strip()
    auto_slug = slug_for_name(name_clean)
    auto_sort_order = 0

    conn = get_connection()
    try:
        cur = conn.cursor()
        # 仅查 name 重复 (slug 自动生成不会冲)
        cur.execute(
            "SELECT id, name FROM geo_research_industries WHERE name = %s",
            (name_clean,),
        )
        existed = cur.fetchone()
        if existed:
            cur.close()
            raise HTTPException(status_code=409, detail=f"行业名称已存在: {name_clean}")

        cur.execute(
            """
            INSERT INTO geo_research_industries (name, slug, sort_order, active, created_at, updated_at)
            VALUES (%s, %s, %s, %s, NOW(), NOW())
            RETURNING id, name, slug, sort_order, active, created_at, updated_at
            """,
            (name_clean, auto_slug, auto_sort_order, payload.active),
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
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
        logger.exception("[行业新建] 失败 admin_id=%s name=%s: %s",
                         current_user_id(user), payload.name, e)
        raise HTTPException(status_code=500, detail="新建行业失败")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    logger.info("[行业新建] 成功 admin_id=%s industry_id=%s name=%s slug=%s",
                current_user_id(user), row["id"], row["name"], row["slug"])
    return {"industry": _row_to_dict(row)}


# ========== 3. PUT 编辑 ==========

@router.put("/industries/{industry_id}", summary="编辑行业")
async def update_industry(request: Request, industry_id: int, payload: UpdateIndustryRequest):
    """
    部分更新行业(只更新非 None 字段)。
    - 不存在 → 404
    - name/slug 重名(排除自身) → 409
    - 同步刷新 updated_at
    """
    user = _require_admin(request)

    # 至少传 1 个字段
    update_fields = {
        k: v for k, v in payload.model_dump(exclude_unset=True).items()
        if v is not None
    }
    if not update_fields:
        raise HTTPException(status_code=400, detail="请至少传 1 个待更新字段")

    conn = get_connection()
    try:
        cur = conn.cursor()
        # 1) 校验存在
        cur.execute(
            "SELECT id, name, slug FROM geo_research_industries WHERE id = %s",
            (industry_id,),
        )
        current = cur.fetchone()
        if not current:
            cur.close()
            raise HTTPException(status_code=404, detail=f"行业不存在: id={industry_id}")

        # 2) 重名校验(排除自身)
        check_name = update_fields.get("name")
        check_slug = update_fields.get("slug")
        if check_name or check_slug:
            cur.execute(
                """
                SELECT id, name, slug FROM geo_research_industries
                WHERE id <> %s AND (
                    (%s::TEXT IS NOT NULL AND name = %s)
                    OR (%s::TEXT IS NOT NULL AND slug = %s)
                )
                """,
                (industry_id, check_name, check_name, check_slug, check_slug),
            )
            dup = cur.fetchone()
            if dup:
                cur.close()
                if check_name and dup["name"] == check_name:
                    raise HTTPException(status_code=409, detail=f"行业名称已存在: {check_name}")
                raise HTTPException(status_code=409, detail=f"行业 slug 已存在: {check_slug}")

        # 3) 拼 SET 子句 (字段白名单防御 SQL 注入)
        set_parts = []
        params = []
        for k, v in update_fields.items():
            assert k in _ALLOWED_INDUSTRY_UPDATE_FIELDS, f"非法更新字段: {k}"
            set_parts.append(f"{k} = %s")
            params.append(v)
        set_parts.append("updated_at = NOW()")
        params.append(industry_id)

        sql = f"""
            UPDATE geo_research_industries
            SET {', '.join(set_parts)}
            WHERE id = %s
            RETURNING id, name, slug, sort_order, active, created_at, updated_at
        """
        cur.execute(sql, tuple(params))
        row = cur.fetchone()
        conn.commit()
        cur.close()
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
        logger.exception("[行业编辑] 失败 admin_id=%s industry_id=%s: %s",
                         current_user_id(user), industry_id, e)
        raise HTTPException(status_code=500, detail="编辑行业失败")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    logger.info("[行业编辑] 成功 admin_id=%s industry_id=%s 字段=%s",
                current_user_id(user), industry_id, list(update_fields.keys()))
    return {"industry": _row_to_dict(row)}


# ========== 4. DELETE 软删 ==========

@router.delete("/industries/{industry_id}", summary="软删行业")
async def delete_industry(request: Request, industry_id: int):
    """
    软删 = active=FALSE。
    - 不存在 → 404
    - 已 inactive 再删 → 200 ok 幂等
    """
    user = _require_admin(request)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, active FROM geo_research_industries WHERE id = %s",
            (industry_id,),
        )
        current = cur.fetchone()
        if not current:
            cur.close()
            raise HTTPException(status_code=404, detail=f"行业不存在: id={industry_id}")

        # 已 inactive 直接幂等返回
        if not current["active"]:
            cur.close()
            logger.info("[行业软删] 幂等 admin_id=%s industry_id=%s 已 inactive",
                        current_user_id(user), industry_id)
            return {"deleted": True, "industry_id": industry_id}

        cur.execute(
            """
            UPDATE geo_research_industries
            SET active = FALSE, updated_at = NOW()
            WHERE id = %s
            """,
            (industry_id,),
        )
        conn.commit()
        cur.close()
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
        logger.exception("[行业软删] 失败 admin_id=%s industry_id=%s: %s",
                         current_user_id(user), industry_id, e)
        raise HTTPException(status_code=500, detail="软删行业失败")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    logger.info("[行业软删] 成功 admin_id=%s industry_id=%s",
                current_user_id(user), industry_id)
    return {"deleted": True, "industry_id": industry_id}


# ========== 5. POST reorder 批量排序 ==========

@router.post("/industries/reorder", summary="批量调整行业排序")
async def reorder_industries(request: Request, payload: ReorderRequest):
    """
    批量更新 sort_order。单事务,任一 id 不存在则整批回滚 404。
    """
    user = _require_admin(request)

    if not payload.items:
        return {"updated": 0}

    item_ids = [it.id for it in payload.items]

    conn = get_connection()
    try:
        cur = conn.cursor()
        # 1) 一次性查存在性
        cur.execute(
            "SELECT id FROM geo_research_industries WHERE id = ANY(%s)",
            (item_ids,),
        )
        found = {r["id"] for r in cur.fetchall()}
        missing = [iid for iid in item_ids if iid not in found]
        if missing:
            cur.close()
            raise HTTPException(
                status_code=404,
                detail=f"以下行业不存在,整批未生效: {missing}",
            )

        # 2) 单事务批量 UPDATE
        updated = 0
        for it in payload.items:
            cur.execute(
                """
                UPDATE geo_research_industries
                SET sort_order = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (it.sort_order, it.id),
            )
            updated += cur.rowcount
        conn.commit()
        cur.close()
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
        logger.exception("[行业排序] 失败 admin_id=%s items=%d: %s",
                         current_user_id(user), len(payload.items), e)
        raise HTTPException(status_code=500, detail="批量调整排序失败")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    logger.info("[行业排序] 成功 admin_id=%s 更新行数=%d",
                current_user_id(user), updated)
    return {"updated": updated}


# ========== 6. 行业别名归并审核 (R2 · SPEC §3-R3.4 人工兜底) ==========
# 路由结构:
#   GET /industries/aliases          → 列 AI/人工归并记录 (join 行业名)
#   PUT /industries/aliases/{alias}  → admin 改判归属行业
# 注: 路径为「/industries/aliases[...]」双段, 与 PUT/DELETE /industries/{id} 单段互不冲突。

@router.get("/industries/aliases", summary="行业别名归并记录 (AI 自动 + 人工改判)")
async def list_aliases(request: Request):
    """列出用户行业原文 → 标准行业的归并记录, join 行业中文名, 供 admin 审核/改判。"""
    _require_admin(request)
    from db.research_selfserve_db import list_industry_aliases

    try:
        aliases = list_industry_aliases()
    except Exception as e:
        logger.exception("[别名列表] 查询失败: %s", e)
        raise HTTPException(status_code=500, detail="查询别名列表失败")

    out = []
    for a in aliases:
        d = dict(a)
        for k in ("created_at", "updated_at"):
            v = d.get(k)
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
        # confidence NUMERIC(4,3) → float (Decimal 不可直接 JSON 序列化)
        if d.get("confidence") is not None:
            try:
                d["confidence"] = float(d["confidence"])
            except (TypeError, ValueError):
                d["confidence"] = None
        out.append(d)
    return {"aliases": out}


@router.put("/industries/aliases/{alias_id}", summary="admin 改判别名归属行业")
async def update_alias(request: Request, alias_id: int, payload: UpdateAliasIndustryRequest):
    """admin 改判某别名归属到指定行业 (resolved_by 置 'admin' + 记 reviewed_by)。"""
    user = _require_admin(request)
    from db.research_selfserve_db import update_alias_industry

    # 校验目标行业存在 (改判到不存在的 industry_id 会踩 FK / 造脏映射)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM geo_research_industries WHERE id = %s",
            (payload.industry_id,),
        )
        exists = cur.fetchone()
        cur.close()
        if not exists:
            raise HTTPException(status_code=404, detail=f"目标行业不存在: id={payload.industry_id}")
    except HTTPException:
        raise
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("[别名改判] 校验目标行业失败 alias_id=%s: %s", alias_id, e)
        raise HTTPException(status_code=500, detail="校验目标行业失败")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    try:
        update_alias_industry(alias_id, payload.industry_id, reviewed_by=current_user_id(user))
    except Exception as e:
        logger.exception("[别名改判] 失败 admin_id=%s alias_id=%s: %s",
                         current_user_id(user), alias_id, e)
        raise HTTPException(status_code=500, detail="改判别名失败")

    logger.info("[别名改判] 成功 admin_id=%s alias_id=%s → industry_id=%s",
                current_user_id(user), alias_id, payload.industry_id)
    return {"ok": True, "alias_id": alias_id, "industry_id": payload.industry_id}
