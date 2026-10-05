"""GEO 方案 API (v3.2 Phase 2)

C 端豆包式对话的后端端点：
- POST /api/geo-plan/quick-cost: 单词成本估算（免费，获客钩子）
- POST /api/geo-plan/full-plan:  一键 GEO 方案（前 7 免费 + 后 3 锁定）
- POST /api/geo-plan/unlock:     解锁完整方案（扣 260 积分）

这些端点也被 CEndAgent 作为工具调用。
"""

import logging
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from auth.user_ctx import current_user_id

logger = logging.getLogger("GEO-PlanAPI")
router = APIRouter(prefix="/api/geo-plan", tags=["GEO方案"])


# ==================== 请求模型 ====================

class QuickCostRequest(BaseModel):
    keyword: str = Field(..., min_length=1, max_length=100)
    city: Optional[str] = None
    industry: Optional[str] = None
    target_rank: str = Field("top_10", pattern="^top_(3|10|30)$")


class FullPlanRequest(BaseModel):
    brand_name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    # [CTO-15.4 2026-04-20 P0-F] 老板实测发现 AI 生成 "赵胡子·贵阳肠旺面" 但
    # 用户真实品牌是 "老王家常菜小馆子(北京)" — LLM 完全瞎编品牌. 根因是
    # one_click_geo_plan 只接 brand_name 文本, 不读 DB 真实 brand.
    # 修: 前端/agent 传 brand_id → 后端优先从 DB 取真 name + profile 多维度
    brand_id: Optional[int] = None


class UnlockRequest(BaseModel):
    plan_id: Optional[str] = None
    brand_name: str


# ==================== 端点 ====================

def _get_user_id(request: Request) -> int:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user.get("user_id") or current_user_id(user)


@router.post("/quick-cost")
async def quick_cost(req: QuickCostRequest, request: Request):
    """单词成本估算 — 免费（获客钩子）

    对 C 端完全免费，平均成本 ~¥0.05（LLM 调用）
    有 Redis 级缓存：同用户同 keyword 5 分钟内返回缓存
    """
    user_id = _get_user_id(request)
    logger.info(f"[GeoPlan/quick-cost] user={user_id} keyword={req.keyword}")

    try:
        from tools.c_end_cost_estimate import estimate_user_cost
        result = await estimate_user_cost(
            keyword=req.keyword,
            city=req.city,
            industry=req.industry,
            target_rank=req.target_rank,
        )
        return {"success": True, "data": result}
    except Exception as e:
        logger.exception(f"[GeoPlan/quick-cost] 失败: {e}")
        raise HTTPException(status_code=500, detail=f"估算失败：{e}")


def _plan_cache_key(user_id: int, plan_id: str) -> str:
    return f"geo_plan:{user_id}:{plan_id}"


@router.post("/full-plan")
async def full_plan(req: FullPlanRequest, request: Request):
    """[CTO-15.5 Phase 4 PLAN 03 · 2026-04-20] 旧同步端点已切断 · 返 410 Gone.

    Q3 决策:C 端 GEO 方案重构为异步任务流,15min 超时 + zombie 清理 + 归档.
    前端当时统一走异步任务端点(PLAN 02),本端点禁用.
    [开源 E3 · B2 · 2026-09-28] 异步任务端点(start-task 等 5 条)已在 B3c 删除,
    回包不再给 migrate_to(指过去就是 404);code 不变,老客户端按 code 判下线照旧成立.

    旧逻辑见 git blame 本行之前的版本(主业务函数 one_click_geo_plan 仍保留给其他入口).

    Returns: 410 Gone + detail 含 code / message
    """
    raise HTTPException(
        status_code=410,
        detail={
            "code": "endpoint_removed",
            "message": "该端点已下线",
        },
    )


@router.post("/unlock")
async def unlock_plan(req: UnlockRequest, request: Request):
    """解锁完整 GEO 方案 — 扣 260 积分，返回所有关键词（含锁定的）

    2026-04-17 P1-E: 优先用 plan_id 从缓存取方案（保证词包与预览一致）；
    缓存缺失（过期/跨 worker）fallback 到重新生成（边缘 case，保留原行为）
    """
    user_id = _get_user_id(request)

    # 1. 扣费
    try:
        from middleware.billing import deduct_points
        deduct_result = await deduct_points(user_id, "geo_plan_unlock")
        if not deduct_result.get("success"):
            raise HTTPException(
                status_code=402,
                detail=deduct_result.get("error", "积分不足，无法解锁")
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"[GeoPlan/unlock] 扣费失败，走默认逻辑: {e}")
        pass

    # 2. 优先用 plan_id 取缓存
    cached = None
    if req.plan_id:
        try:
            from cache.redis_client import redis_get_json
            cached = redis_get_json(_plan_cache_key(user_id, req.plan_id))
        except Exception as e:
            logger.warning(f"[GeoPlan/unlock] 读缓存失败: {e}")

    try:
        if cached:
            # ✅ 缓存命中：词包跟预览时一致，只把 locked_keywords 合并入 keyword_package
            result = cached
            locked = result.get("locked_keywords", [])
            if locked:
                import asyncio
                from tools.c_end_cost_estimate import estimate_user_cost
                brand_info = result.get("brand_info", {}) or {}
                more_tasks = [
                    estimate_user_cost(
                        kw["keyword"] if isinstance(kw, dict) else kw,
                        city=brand_info.get("city"),
                        industry=brand_info.get("industry"),
                    )
                    for kw in locked
                ]
                more_results = await asyncio.gather(*more_tasks, return_exceptions=True)
                valid_more = [r for r in more_results if isinstance(r, dict) and "cost_breakdown" in r]
                result["keyword_package"] = result.get("keyword_package", []) + valid_more
            result["locked_keywords"] = []
            result["unlocked"] = True
            return {"success": True, "data": result, "source": "cached"}

        # 3. fallback: 无缓存时重新生成（缓存过期/跨 worker 场景）
        from tools.c_end_cost_estimate import one_click_geo_plan
        result = await one_click_geo_plan(
            brand_name=req.brand_name,
            description=None,
        )
        locked = result.get("locked_keywords", [])
        if locked:
            import asyncio
            from tools.c_end_cost_estimate import estimate_user_cost
            brand_info = result.get("brand_info", {}) or {}
            more_tasks = [
                estimate_user_cost(
                    kw["keyword"] if isinstance(kw, dict) else kw,
                    city=brand_info.get("city"),
                    industry=brand_info.get("industry"),
                )
                for kw in locked
            ]
            more_results = await asyncio.gather(*more_tasks, return_exceptions=True)
            valid_more = [r for r in more_results if isinstance(r, dict) and "cost_breakdown" in r]
            result["keyword_package"] = result.get("keyword_package", []) + valid_more
            result["locked_keywords"] = []
            result["unlocked"] = True

        return {"success": True, "data": result, "source": "regenerated"}
    except Exception as e:
        logger.exception(f"[GeoPlan/unlock] 失败: {e}")
        raise HTTPException(status_code=500, detail=f"解锁失败：{e}")
