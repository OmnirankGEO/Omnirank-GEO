# -*- coding: utf-8 -*-
"""「平台直营账号不需要进货」这句话的**唯一出处**。

[WO_254 2026-09-20] 这个模块之所以存在,是因为 WO_241 甲把这句话放错了地方:

  · 后端在 `GET /api/agent/inventory/purchase-options` 上出声(409 + 原话),实测无误;
  · 而进货中心那一屏**根本不打这个端点** —— 它打的是
    `GET /api/pricing/procurement/catalog`,那边对 admin 回的是
    `PRICE_CONFIGURATION_UNAVAILABLE`「价格配置暂不可用」;
  · 于是屏幕上显示的是前端的诚实兜底句「尚未取到平台口径说明」,
    **明示原话一次都没到过屏幕**。

🔴 形状:**信号由做事方发出,而不是由被服务方收到**。
   「端点回了 409 + 原话」证明的只是"我们发出去的东西是对的";
   要证明的是"那一屏上出现了这句话" —— 而那一屏读的是另一个端点。
   我的判据当时用夹具直接喂这个 code 证"逐字出现",夹具**没有经过**页面真正打的那条路。
   (作者的毒来自作者的判据:我按自己的接线方式出题,于是接错线这件事出不了题。)

所以这里只放**一份**码与文案,两个端点都从这里取:
同一句话写两处,迟早有一处跟不上,而两处各自看都"对"。
"""
from __future__ import annotations

from fastapi import HTTPException

#: 前端按这个 code 分支渲染(`pages/Agent/InventoryCenter.tsx`),改名要两边一起改。
PLATFORM_DIRECT_NO_PROCUREMENT_CODE = "PLATFORM_DIRECT_NO_PROCUREMENT"

#: 🔴 **全仓唯一一份**。判据有一格钉住它在代码里只出现这一处字面量。
PLATFORM_DIRECT_NO_PROCUREMENT_MESSAGE = "平台直营账号不需要进货"

#: 会向进货中心那一屏交付这个 code 的后端端点。**页面真正打的那个必须在里面**。
#: 判据按这张表逐个核;新增消费方就在这里登记,别再让"出声点"和"被消费点"错位。
PLATFORM_DIRECT_NOTICE_ENDPOINTS = (
    "/api/pricing/procurement/catalog",      # 进货中心开屏就打(usePricingSSOT)
    "/api/agent/inventory/purchase-options",  # 下单弹层打
)


def platform_direct_no_procurement() -> HTTPException:
    """平台直营走到进货路径上 —— 409 + 那句话。

    返回异常而不是直接抛:调用方各自 `raise`,栈上看得见是谁拒的。
    """
    return HTTPException(
        409,
        detail={
            "code": PLATFORM_DIRECT_NO_PROCUREMENT_CODE,
            "message": PLATFORM_DIRECT_NO_PROCUREMENT_MESSAGE,
        },
    )


def is_platform_direct_actor(user: dict) -> bool:
    """这个登录用户在经营后台里是不是"平台直营"身份。

    🔴 口径与 `api/agent_workbench_api.py::_require_agent` 同一条:
       **管理员**进经营后台时被换成平台直营经营账号(`operating_context="platform_direct"`)。
       那边拿的是解析后的主体字典(有 `operating_context`),这里拿的是原始登录用户 ——
       两个入口手里的东西不同,但**判别规则只有这一条**,判据里有一格钉住它俩等价。
    """
    return bool((user or {}).get("is_admin", False))
