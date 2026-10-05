"""报价投放组合 API（WO_QUOTE_MEDIA_MIX_DYNAMIC_2026-08-12 v2 · P0）。

🔴 [2026-08-12 Review P1-1 返修] 本文件存在的唯一理由:把 `services/quote_media_mix`
   **真正接进报价链**。判红原文:「后端算法零生产调用;前端仍使用固定全局比例,
   抖音份额固定为 0,却显示"按行业和目标 AI 调整"」——
   算法自洽 ≠ 接线通了,这两件事必须分别有判据。

为什么是**新模块**而不是塞进 `api/selection_api.py`:
   那个文件被 p1capacity / orphanmon / gapplanreloc / debtclose 等多个在途包声明占用,
   往里加 hunk 是明知故犯的撞车。新建只读端点零撞车,也不改任何既有报价响应体。

零越权(工单 §1.2 / §2):
   · 不产生也不修改总额度 —— `capacity_total` 从既有报价数据读出来,原样带回;
   · 不碰价格、成本、系数、资金账本、已签报价;
   · 权限走既有 `require_quote_access`,不新造鉴权口径。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger("GEO-QuoteMediaMixAPI")

router = APIRouter(prefix="/api/quotes", tags=["报价投放组合"])

#: 没有"主战引擎"这个业务字段时的目标引擎(生产实测四引擎全量跑)。
#: 🔴 全仓实测不存在 target_engine / primary_engine 字段(2026-08-12),
#:    所以这里是**默认全量**,不是"用户选了这四个"。文案侧据此不得宣称"按目标 AI 调整"。
DEFAULT_TARGET_ENGINES = ("deepseek", "doubao", "kimi", "qwen")


def _capacity_from_pricing(pricing_data: Any, tier: str) -> int:
    """从既有报价数据里读**已确定**的交付额度。本函数只读不算。"""
    if not isinstance(pricing_data, dict):
        return 0
    keywords = pricing_data.get("keywords") or []
    total = 0
    for kw in keywords:
        if not isinstance(kw, dict):
            continue
        block = kw.get(tier) or kw.get("standard") or {}
        try:
            total += int(block.get("articles") or 0)
        except (TypeError, ValueError):
            continue
    return total


@router.get("/{quote_id}/media-mix")
async def get_quote_media_mix(quote_id: int, request: Request, tier: str = "standard",
                              perspective: Optional[str] = None):
    """返回该报价的投放组合建议 + 每个数字的来源（工单 §5 合同）。

    只读。任何下游数据出问题都降级返回,**不阻断报价主链**（工单 §1.1.5）。
    """
    from auth.brand_access import require_quote_access
    from db.diagnosis_db import get_session_by_quote
    from services.quote_media_mix import load_pool, plan_media_mix

    require_quote_access(request, quote_id, allow_null=False)

    session = await asyncio.to_thread(get_session_by_quote, quote_id)
    if not session:
        raise HTTPException(status_code=404, detail="该报价单没有关联选词会话")

    industry = (session.get("industry") or "").strip() or None
    pricing_data = session.get("pricing_data")
    if isinstance(pricing_data, str):
        import json
        try:
            pricing_data = json.loads(pricing_data)
        except Exception:
            pricing_data = None

    capacity_total = _capacity_from_pricing(pricing_data, tier)
    pool = await asyncio.to_thread(load_pool)
    # [WO_225-c1 §8.2] 发布口径。不传 = 自媒体为主(Owner 2026-09-15 ③:
    #   写作中心默认显示换算后条数)。口径只改**条数估算**,不改 mix/总槽数/价。
    #   🔴 不认识的值直接 400,不静默回落 —— 前端传错拼写时,
    #      静默回落会让页面显示另一个口径的条数而看不出任何异常。
    from services.media_slot_conversion import DEFAULT_PERSPECTIVE, PERSPECTIVES
    if perspective is not None and perspective not in PERSPECTIVES:
        raise HTTPException(status_code=400,
                            detail="perspective 只能是 %s" % (", ".join(PERSPECTIVES),))
    payload = plan_media_mix(
        capacity_total, industry=industry,
        target_engines=list(DEFAULT_TARGET_ENGINES), pool=pool,
        perspective=perspective or DEFAULT_PERSPECTIVE,
    )
    payload["quote_id"] = quote_id
    payload["tier"] = tier
    # 🔴 目标引擎是**系统默认全量**,不是用户选的 —— 前端据此决定能不能说"按目标 AI 调整"
    payload["target_engines_are_default"] = True
    return payload


@router.post("/{quote_id}/media-mix/record")
async def record_quote_media_mix(quote_id: int, request: Request, tier: str = "standard",
                                 perspective: Optional[str] = None):
    """把当前组合决策记进报价快照，供飞轮 7/14/30 天回查消费（工单 §1.1.4）。

    幂等：组合没变就一条都不写（append-only 表不能靠"反正只是加一行"糊过去）。
    """
    from auth.brand_access import require_quote_access
    from services.quote_media_mix import record_media_mix_snapshot

    require_quote_access(request, quote_id, allow_null=False)
    user = getattr(request.state, "user", None) or {}
    actor_user_id = user.get("user_id") or user.get("id")
    if not actor_user_id:
        raise HTTPException(status_code=401, detail="未登录")

    payload = await get_quote_media_mix(quote_id, request, tier=tier, perspective=perspective)
    result = await asyncio.to_thread(
        record_media_mix_snapshot, quote_id, payload, actor_user_id=int(actor_user_id),
    )
    return {"success": True, "mix": payload, "record": result}
