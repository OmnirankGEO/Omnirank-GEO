"""图文管线总闸的**唯一**出口(WO_213)。

Deploy 2026-09-14 逐路由实测:`GEO_DOUYIN_PIPELINE_ENABLED` 是生产上唯一的
图文开关,但它只盖住 38 条路由里的 **9 条** ——

  · `api/geo_douyin_api.py` 29 条路由,其中 9 条自己判了一下;
  · `api/geo_image_note_api.py`(合同链)9 条,**一条都没判**,
    其中包括**花供应商钱**的 `publish-batch`;
  · `contract_worker.run_tick` 与两份 scheduler 里的图文 job 也不认闸。

也就是说:今天没有「只停图文、代价可控」的开关。停 cron 容器会连带停掉
诊断 / 监测 / 结算 / 退款 —— 那不是停一个功能,是停半个系统。

🔴 **不逐条加判定**:逐条加等于给 38 条路由各写一遍同一个谓词,
   而「同一谓词写两处必有一处没人验」;更要紧的是**将来新增的路由**
   默认不受闸 —— 而合同链那 9 条正是这么漏掉的(它们是后加的)。
   改成挂在**路由器**上:新增路由自动受闸,想不受闸得显式绕开,
   而"显式绕开"是看得见的。
"""
from __future__ import annotations

from typing import Final

from fastapi import HTTPException

from services.geo_douyin.config import is_pipeline_enabled

#: 闸关时的错误码。与既有 `_COMING_SOON` 的**形状**对齐(status/message 两个键),
#: 前端原样上屏那句话,不自己编。
PIPELINE_DISABLED_CODE: Final = "PIPELINE_DISABLED"
PIPELINE_DISABLED_MESSAGE: Final = "图文制作暂时关闭了，稍后再来看看"


def pipeline_disabled_detail() -> dict:
    """闸关时的响应体。**一处定义**,HTTP 与 worker 共用同一句话。"""
    return {
        "status": "coming_soon",          # 与老的 `_COMING_SOON` 同键同值,前端不用改分支
        "code": PIPELINE_DISABLED_CODE,   # 新增:机器可判,便于埋点与判据
        "message": PIPELINE_DISABLED_MESSAGE,
    }


async def pipeline_gate() -> None:
    """路由器级依赖:闸关 ⇒ 503,**任何**路由都进不去(含 GET)。

    🔴 用 **503** 不用 200:老的 `_COMING_SOON` 返 200,于是「功能关了」
       和「功能跑通了」在状态码上分不出来 —— 埋点、网关、前端的错误分支
       全都看不见它。503 是"暂时不可用"的标准语义,且**可重试**。
       响应体仍带 `status: coming_soon`,前端既有分支照旧能认。

    🔴 GET 也拦:读端点看起来无害,但它们会把"这个功能还活着"的样子
       展示给用户 —— 用户接着就去点那个已经关掉的按钮。
       闸是"这个功能今天不提供",不是"今天不许写"。
    """
    if not is_pipeline_enabled():
        raise HTTPException(status_code=503, detail=pipeline_disabled_detail())


def pipeline_gate_open() -> bool:
    """给**非 HTTP** 的地方用(worker / scheduler):闸开返 True。

    与 `pipeline_gate` 读同一个 `is_pipeline_enabled()` —— 两处各读各的
    环境变量名,就会出现"HTTP 关了、后台还在跑"的半开状态。
    """
    return bool(is_pipeline_enabled())
