"""
GEO 调研监测 - Prompts 管理 API (A.7 Group 2)

针对 geo_research_prompts 表的 admin CRUD 接口。
约束 (P12-fix-v2 2026-05-26 老板拍板):
  - 行业 prompts 总数不设业务上限 (跑批配额由 budget_guard 按月度预算限速)
  - 单条 prompt_text ≤ 500 字符 (MAX_PROMPT_TEXT_LENGTH)
  - 单次批量提交 ≤ 500 条 (MAX_BULK_CREATE_PROMPTS · Pydantic max_length 防御性)
  - 单次 reorder ≤ 500 条 (MAX_REORDER_BATCH · 同上)

7 个接口:
  GET    /api/admin/research-monitor/prompts                   列表 (按 industry_id)
  POST   /api/admin/research-monitor/prompts                   单条新建
  PUT    /api/admin/research-monitor/prompts/{prompt_id}       编辑
  DELETE /api/admin/research-monitor/prompts/{prompt_id}       软删 (active=FALSE)
  POST   /api/admin/research-monitor/prompts/reorder           批量改 sort_order
  POST   /api/admin/research-monitor/prompts/bulk-create       一次创建多条
  POST   /api/admin/research-monitor/prompts/{prompt_id}/toggle 启用/禁用
"""

from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Optional, List, Dict
import logging

from db.connection import get_connection
from auth.user_ctx import current_user_id

logger = logging.getLogger("GEO-ResearchMonitor.PromptsAPI")

router = APIRouter(prefix="/api/admin/research-monitor", tags=["调研监测-Prompts"])


# ==========================================
# 常量
# ==========================================

# P12-fix-v2 (2026-05-26 老板拍板): 每行业 prompts 数量不设业务上限
#   原因: 老板 spec 改为 "prompts 越多覆盖越广 · 跑批配额由月度预算控" · 不该硬限 25
#   只保留: 单条文本长度 500 + 单次批量提交大小 500 (防一次 POST 太大爆 DB/网络)
#   行业 prompts 总数 = 业务自由 · 跑批时由 budget_guard 按月度预算自然限速
MAX_PROMPT_TEXT_LENGTH = 500
# 单次批量提交防御性上限 (防一次粘贴几千条爆 · 跟"行业总上限"无关 · 多次提交不限)
MAX_BULK_CREATE_PROMPTS = 500
# Reorder 单次提交防御性上限 (前端拖拽不会几千 · 加个 sanity check)
MAX_REORDER_BATCH = 500

# UPDATE SET 拼接白名单: 防御未来 Pydantic 模型扩字段时拼到非法列
# 注意: industry_id 不在白名单 (PUT 不允许改 industry)
_ALLOWED_PROMPT_UPDATE_FIELDS = {'prompt_text', 'sort_order', 'active'}


# ==========================================
# 鉴权
# ==========================================

def _require_admin(request: Request) -> dict:
    """要求管理员权限, 复用全局 auth 中间件注入的 request.state.user"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ==========================================
# 工具函数
# ==========================================

def _industry_exists(cur, industry_id: int) -> bool:
    """校验 industry 存在 (不管 active, 只看记录在不在)"""
    cur.execute(
        "SELECT 1 FROM geo_research_industries WHERE id = %s",
        (industry_id,),
    )
    return cur.fetchone() is not None


def _row_to_prompt(row: dict) -> dict:
    """统一序列化 prompt 行 (timestamp 转 iso)"""
    if not row:
        return {}
    return {
        "id": row["id"],
        "industry_id": row["industry_id"],
        "prompt_text": row["prompt_text"],
        "sort_order": row["sort_order"],
        "active": row["active"],
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def _prompt_text_exists(cur, industry_id: int, prompt_text: str,
                        exclude_id: Optional[int] = None) -> bool:
    """校验同 industry 内是否已有相同 prompt_text (active 全部都查, 不只 active)"""
    if exclude_id is not None:
        cur.execute(
            "SELECT 1 FROM geo_research_prompts "
            "WHERE industry_id = %s AND prompt_text = %s AND id <> %s",
            (industry_id, prompt_text, exclude_id),
        )
    else:
        cur.execute(
            "SELECT 1 FROM geo_research_prompts "
            "WHERE industry_id = %s AND prompt_text = %s",
            (industry_id, prompt_text),
        )
    return cur.fetchone() is not None


# ==========================================
# 请求模型 (Pydantic v2 风格)
# ==========================================

class CreatePromptRequest(BaseModel):
    industry_id: int = Field(..., description="所属行业 id")
    prompt_text: str = Field(..., min_length=1, max_length=MAX_PROMPT_TEXT_LENGTH,
                             description="prompt 文本")
    sort_order: int = Field(default=0, description="排序权重, 小的在前")
    active: bool = Field(default=True, description="是否启用")


class UpdatePromptRequest(BaseModel):
    """部分更新, 不能改 industry_id"""
    prompt_text: Optional[str] = Field(default=None, min_length=1,
                                       max_length=MAX_PROMPT_TEXT_LENGTH)
    sort_order: Optional[int] = Field(default=None)
    active: Optional[bool] = Field(default=None)


class ReorderRequest(BaseModel):
    industry_id: int = Field(..., description="限制只在该行业内 reorder")
    items: List[Dict[str, int]] = Field(
        ..., max_length=MAX_REORDER_BATCH,
        description="[{\"id\": int, \"sort_order\": int}, ...] · 单次最多 500 条",
    )


class BulkCreateRequest(BaseModel):
    industry_id: int = Field(..., description="所属行业 id")
    # P12-fix-v2: 行业 prompts 总数不限 · 单次批量提交上限 500 (防一次过大)
    # 想加 1000 条? 分 2 次 POST 即可 · 不会被 \"行业总数\" 拒
    prompts: List[str] = Field(
        ..., min_length=1, max_length=MAX_BULK_CREATE_PROMPTS,
        description="prompt_text 列表, 内部不可重复, 单次最多 500 条",
    )


# ==========================================
# 1. GET /prompts - 列表
# ==========================================

@router.get("/prompts", summary="列出某行业的所有 prompts")
async def list_prompts(
    request: Request,
    industry_id: int = Query(..., description="必填, 行业 id"),
    include_inactive: bool = Query(False, description="是否包含 active=FALSE"),
):
    """
    按 industry_id 列出 prompts,
    排序: ORDER BY sort_order ASC, id ASC.
    industry_id 不存在 -> 404.
    """
    _require_admin(request)

    conn = get_connection()
    try:
        cur = conn.cursor()

        if not _industry_exists(cur, industry_id):
            raise HTTPException(status_code=404, detail=f"行业 {industry_id} 不存在")

        if include_inactive:
            cur.execute(
                """
                SELECT id, industry_id, prompt_text, sort_order, active,
                       created_at, updated_at
                FROM geo_research_prompts
                WHERE industry_id = %s
                ORDER BY sort_order ASC, id ASC
                """,
                (industry_id,),
            )
        else:
            cur.execute(
                """
                SELECT id, industry_id, prompt_text, sort_order, active,
                       created_at, updated_at
                FROM geo_research_prompts
                WHERE industry_id = %s AND active = TRUE
                ORDER BY sort_order ASC, id ASC
                """,
                (industry_id,),
            )
        rows = cur.fetchall()
        prompts = [_row_to_prompt(r) for r in rows]

        return {
            "industry_id": industry_id,
            "prompts": prompts,
            "count": len(prompts),
        }
    except HTTPException:
        # 业务 HTTPException(404 等)直透,不被 generic 500 吞掉
        raise
    except Exception as e:
        # SQL/连接异常:内部记完整,外部只 generic 不暴露 e
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception(
            f"[列表 prompts] 失败 industry_id={industry_id}: "
            f"{type(e).__name__}: {e}"
        )
        raise HTTPException(status_code=500, detail="查询 prompts 失败")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ==========================================
# 2. POST /prompts - 单条新建
# ==========================================

@router.post("/prompts", summary="新建一个 prompt")
async def create_prompt(request: Request, payload: CreatePromptRequest):
    """
    新建 prompt, 校验:
      - industry 存在
      - prompt_text 在该 industry 内不重复 (含 inactive)
      - prompt_text 长度 ≤ MAX_PROMPT_TEXT_LENGTH (500)
    (P12-fix-v2 2026-05-26 删: 行业 prompts 数量不再设业务上限)
    """
    user = _require_admin(request)

    conn = get_connection()
    try:
        cur = conn.cursor()

        if not _industry_exists(cur, payload.industry_id):
            raise HTTPException(status_code=404,
                                detail=f"行业 {payload.industry_id} 不存在")

        # P12-fix-v2: 删 25 上限 · 行业 prompts 数量不设业务上限 (老板拍板)
        # 跑批配额由月度预算自然限速 · 不在这层卡

        # 重复 prompt_text 校验 (含 inactive)
        if _prompt_text_exists(cur, payload.industry_id, payload.prompt_text):
            raise HTTPException(
                status_code=409,
                detail=f"该行业内已存在相同 prompt_text",
            )

        cur.execute(
            """
            INSERT INTO geo_research_prompts
                (industry_id, prompt_text, sort_order, active, created_at, updated_at)
            VALUES (%s, %s, %s, %s, NOW(), NOW())
            RETURNING id, industry_id, prompt_text, sort_order, active,
                      created_at, updated_at
            """,
            (payload.industry_id, payload.prompt_text,
             payload.sort_order, payload.active),
        )
        new_row = cur.fetchone()
        conn.commit()

        logger.info(
            f"[Prompts] admin={current_user_id(user)} 创建 prompt id={new_row['id']} "
            f"industry={payload.industry_id}"
        )
        return {"prompt": _row_to_prompt(new_row)}
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
        logger.exception(f"[Prompts] 创建失败: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="创建 prompt 失败")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ==========================================
# 3. PUT /prompts/{prompt_id} - 编辑 (部分更新)
# ==========================================

@router.put("/prompts/{prompt_id}", summary="编辑 prompt (部分更新, 不能改 industry_id)")
async def update_prompt(request: Request, prompt_id: int,
                        payload: UpdatePromptRequest):
    user = _require_admin(request)

    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute(
            "SELECT id, industry_id, prompt_text, sort_order, active "
            "FROM geo_research_prompts WHERE id = %s",
            (prompt_id,),
        )
        existing = cur.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=f"prompt {prompt_id} 不存在")

        # 改 prompt_text 时查同 industry 内重复
        if payload.prompt_text is not None and payload.prompt_text != existing["prompt_text"]:
            if _prompt_text_exists(cur, existing["industry_id"],
                                    payload.prompt_text, exclude_id=prompt_id):
                raise HTTPException(
                    status_code=409,
                    detail="该行业内已存在相同 prompt_text",
                )

        # P12-fix-v2 (2026-05-26): 删 25 上限 · inactive → active 不再因数量拒
        # 行业 prompts 数量不设业务上限 · 跑批配额由月度预算自然限速

        # 拼 SET 子句 (字段白名单防御 SQL 注入)
        set_parts: List[str] = []
        params: List = []
        # 用 dict 驱动 + assert 白名单, 任何字段非法立刻 500 (而不是拼到 SQL)
        update_map = {}
        if payload.prompt_text is not None:
            update_map['prompt_text'] = payload.prompt_text
        if payload.sort_order is not None:
            update_map['sort_order'] = payload.sort_order
        if payload.active is not None:
            update_map['active'] = payload.active
        for field_name, field_value in update_map.items():
            assert field_name in _ALLOWED_PROMPT_UPDATE_FIELDS, (
                f"非法更新字段: {field_name}"
            )
            set_parts.append(f"{field_name} = %s")
            params.append(field_value)

        if not set_parts:
            # 没传任何字段, 直接返当前
            cur.execute(
                """
                SELECT id, industry_id, prompt_text, sort_order, active,
                       created_at, updated_at
                FROM geo_research_prompts WHERE id = %s
                """,
                (prompt_id,),
            )
            return {"prompt": _row_to_prompt(cur.fetchone())}

        set_parts.append("updated_at = NOW()")
        params.append(prompt_id)

        cur.execute(
            f"""
            UPDATE geo_research_prompts
            SET {', '.join(set_parts)}
            WHERE id = %s
            RETURNING id, industry_id, prompt_text, sort_order, active,
                      created_at, updated_at
            """,
            tuple(params),
        )
        updated = cur.fetchone()
        conn.commit()

        logger.info(
            f"[Prompts] admin={current_user_id(user)} 更新 prompt id={prompt_id}"
        )
        return {"prompt": _row_to_prompt(updated)}
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
        logger.exception(f"[Prompts] 更新失败: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="更新 prompt 失败")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ==========================================
# 4. DELETE /prompts/{prompt_id} - 软删 (幂等)
# ==========================================

@router.delete("/prompts/{prompt_id}", summary="软删 prompt (active=FALSE, 幂等)")
async def delete_prompt(request: Request, prompt_id: int):
    """
    软删: UPDATE active=FALSE.
    已 inactive 再调一次 -> 200 ok 幂等.
    不存在 -> 404.
    """
    user = _require_admin(request)

    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute(
            "SELECT id, active FROM geo_research_prompts WHERE id = %s",
            (prompt_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"prompt {prompt_id} 不存在")

        if not row["active"]:
            # 已经是 inactive, 幂等返回
            return {"prompt_id": prompt_id, "active": False, "already_inactive": True}

        cur.execute(
            "UPDATE geo_research_prompts SET active = FALSE, updated_at = NOW() "
            "WHERE id = %s",
            (prompt_id,),
        )
        conn.commit()
        logger.info(
            f"[Prompts] admin={current_user_id(user)} 软删 prompt id={prompt_id}"
        )
        return {"prompt_id": prompt_id, "active": False, "already_inactive": False}
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
        logger.exception(f"[Prompts] 软删失败: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="软删 prompt 失败")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ==========================================
# 5. POST /prompts/reorder - 批量改 sort_order
# ==========================================

@router.post("/prompts/reorder", summary="批量重排 sort_order (限同一 industry 内)")
async def reorder_prompts(request: Request, payload: ReorderRequest):
    """
    单事务批量更新 sort_order.
    校验所有 id 都属于同一 industry_id, 任一不属于 -> 整批 rollback + 400.
    """
    user = _require_admin(request)

    if not payload.items:
        return {"updated": 0, "industry_id": payload.industry_id}

    # 校验 items 内字段
    for it in payload.items:
        if "id" not in it or "sort_order" not in it:
            raise HTTPException(
                status_code=400,
                detail="items 每条必须含 id 和 sort_order 字段",
            )

    conn = get_connection()
    try:
        cur = conn.cursor()

        if not _industry_exists(cur, payload.industry_id):
            raise HTTPException(status_code=404,
                                detail=f"行业 {payload.industry_id} 不存在")

        # 先验所有 id 都属于该 industry
        ids = [int(it["id"]) for it in payload.items]
        cur.execute(
            "SELECT id, industry_id FROM geo_research_prompts "
            "WHERE id = ANY(%s)",
            (ids,),
        )
        rows = cur.fetchall()
        found_ids = {r["id"]: r["industry_id"] for r in rows}

        # 缺失或跨行业 -> 拒绝
        bad_ids = []
        for pid in ids:
            if pid not in found_ids:
                bad_ids.append({"id": pid, "reason": "not_found"})
            elif found_ids[pid] != payload.industry_id:
                bad_ids.append({
                    "id": pid,
                    "reason": "wrong_industry",
                    "actual_industry_id": found_ids[pid],
                })
        if bad_ids:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "部分 prompt id 不属于指定 industry, 拒绝整批",
                    "industry_id": payload.industry_id,
                    "bad_items": bad_ids,
                },
            )

        # 单事务 UPDATE
        for it in payload.items:
            cur.execute(
                "UPDATE geo_research_prompts "
                "SET sort_order = %s, updated_at = NOW() WHERE id = %s",
                (int(it["sort_order"]), int(it["id"])),
            )
        conn.commit()

        logger.info(
            f"[Prompts] admin={current_user_id(user)} reorder industry={payload.industry_id} "
            f"count={len(payload.items)}"
        )
        return {"updated": len(payload.items), "industry_id": payload.industry_id}
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
        logger.exception(f"[Prompts] reorder 失败: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="reorder 失败")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ==========================================
# 6. POST /prompts/bulk-create - 一次创建多条
# ==========================================

@router.post("/prompts/bulk-create", summary="一次新建多条 prompts")
async def bulk_create_prompts(request: Request, payload: BulkCreateRequest):
    """
    单事务批量 INSERT.
    校验:
      - industry 存在
      - 列表内不重复
      - 列表内每条不与现有(含 inactive)重复
      - 单次提交条数 ≤ MAX_BULK_CREATE_PROMPTS (500 · Pydantic max_length)
    sort_order 自动从 现有 max(sort_order)+1 开始递增.
    (P12-fix-v2 2026-05-26 删: 行业 prompts 总数不再设业务上限 · 仅限单次提交大小)
    """
    user = _require_admin(request)

    # 列表内重复校验
    seen = set()
    dup_in_list = []
    for p in payload.prompts:
        if p in seen:
            dup_in_list.append(p)
        seen.add(p)
    if dup_in_list:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "提交的 prompts 列表内有重复",
                "duplicates": dup_in_list,
            },
        )

    # 单条长度校验 (Pydantic 不能逐条管 list[str], 这里手工)
    for p in payload.prompts:
        if not p or len(p) > MAX_PROMPT_TEXT_LENGTH:
            raise HTTPException(
                status_code=422,
                detail=f"prompt_text 长度必须 1-{MAX_PROMPT_TEXT_LENGTH}",
            )

    conn = get_connection()
    try:
        cur = conn.cursor()

        if not _industry_exists(cur, payload.industry_id):
            raise HTTPException(status_code=404,
                                detail=f"行业 {payload.industry_id} 不存在")

        # P12-fix-v2 (2026-05-26): 删 25 上限 · 行业 prompts 数量不设业务上限
        # 单次提交大小已由 Pydantic max_length=MAX_BULK_CREATE_PROMPTS 拦截 (500)

        # 与现有 (含 inactive) 重复校验
        cur.execute(
            "SELECT prompt_text FROM geo_research_prompts WHERE industry_id = %s",
            (payload.industry_id,),
        )
        existing_texts = {r["prompt_text"] for r in cur.fetchall()}
        dup_with_existing = [p for p in payload.prompts if p in existing_texts]
        if dup_with_existing:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "部分 prompt_text 与现有重复",
                    "duplicates": dup_with_existing,
                },
            )

        # 取 max sort_order
        cur.execute(
            "SELECT COALESCE(MAX(sort_order), -1) AS max_so "
            "FROM geo_research_prompts WHERE industry_id = %s",
            (payload.industry_id,),
        )
        max_so = int(cur.fetchone()["max_so"])

        # 单事务 INSERT
        created_rows = []
        for idx, prompt_text in enumerate(payload.prompts):
            cur.execute(
                """
                INSERT INTO geo_research_prompts
                    (industry_id, prompt_text, sort_order, active,
                     created_at, updated_at)
                VALUES (%s, %s, %s, TRUE, NOW(), NOW())
                RETURNING id, industry_id, prompt_text, sort_order, active,
                          created_at, updated_at
                """,
                (payload.industry_id, prompt_text, max_so + 1 + idx),
            )
            created_rows.append(cur.fetchone())
        conn.commit()

        logger.info(
            f"[Prompts] admin={current_user_id(user)} bulk-create industry={payload.industry_id} "
            f"count={len(created_rows)}"
        )
        return {
            "created": len(created_rows),
            "industry_id": payload.industry_id,
            "prompts": [_row_to_prompt(r) for r in created_rows],
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
        logger.exception(f"[Prompts] bulk-create 失败: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="bulk-create 失败")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ==========================================
# 7. POST /prompts/{prompt_id}/toggle - 启用/禁用切换
# ==========================================

@router.post("/prompts/{prompt_id}/toggle", summary="切换 prompt active 状态")
async def toggle_prompt(request: Request, prompt_id: int):
    """
    切换 active (TRUE -> FALSE 或 FALSE -> TRUE).
    不存在 -> 404.
    """
    user = _require_admin(request)

    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute(
            "SELECT id, industry_id, active FROM geo_research_prompts WHERE id = %s",
            (prompt_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"prompt {prompt_id} 不存在")

        new_active = not row["active"]

        # P12-fix-v2 (2026-05-26): 行业 prompts 数量不设业务上限 · toggle 不因数量拒
        cur.execute(
            "UPDATE geo_research_prompts SET active = %s, updated_at = NOW() "
            "WHERE id = %s",
            (new_active, prompt_id),
        )
        conn.commit()

        logger.info(
            f"[Prompts] admin={current_user_id(user)} toggle prompt id={prompt_id} "
            f"-> active={new_active}"
        )
        return {"prompt_id": prompt_id, "active": new_active}
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
        logger.exception(f"[Prompts] toggle 失败: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="toggle 失败")
    finally:
        try:
            conn.close()
        except Exception:
            pass
