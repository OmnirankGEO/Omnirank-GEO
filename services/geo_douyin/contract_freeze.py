"""逐项冻结适配器 —— 把 `middleware.billing` 的冻结包成完整句柄(规格 02 §8.2)。

## 为什么需要这一层(而不是直接调 freeze_points)

三件事只能在这一层做:

1. **P0-6 的落点**。`middleware/billing.py:1358-1361` 对 admin 直接返回
   `{"freeze_id": None, "amount": 0, "free": True, "admin_exempt": True}` ——
   **正是裁定 P0-6 禁止的零记账形态**。而那是保护文件,一行都不能改。
   解法:admin/平台侧操作**不以 admin 身份调用冻结**,而是把 payer 换成
   平台直营账号(`PLATFORM_DIRECT_SERVICE_USER_ID`)。该账号经
   `read_platform_direct_service_identity` 校验过 **不是 admin**
   (它显式拒绝 `is_admin` 的账号),所以走的是正常冻结路径 → 真实 freeze_id、
   真实金额、真实句柄。**零保护文件改动,零记账形态自然消失。**

2. **完整句柄的组装**。`freeze_points` 返回的是 `{freeze_id, amount, freeze_table, ...}`;
   规格 §3.7 要求持久化的是整组
   `payer_user_id + freeze_id + freeze_table + task_ref + reserved_amount + physical_split_snapshot`。
   少一格就可能在两表 id 撞号时 commit/release 到错的那一笔。

3. **cursor 透传**。`freeze_points(_cursor=...)` 支持 caller-owned 事务 ——
   这是 §8.2「入口 claim、reservation、订单项和全部 freeze 同一 PG 事务提交」
   的物理前提。不透传 cursor 就会自开连接,异常时回滚不了。
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from services.geo_douyin.contract_funding import (
    AUTHORITY_ADMIN_EXEMPT,
    ALLOWED_FREEZE_TABLES,
    FundingHandleInvalid,
    build_direct_handle,
)


class FreezeProducedNoHandle(RuntimeError):
    """冻结没有产出可用句柄。

    最常见的成因就是 **payer 恰好是 admin** —— 那条路径返回 `freeze_id=None`。
    走到这里说明 payer 解析错了(P0-6 要求 admin 路由到平台账),
    必须响亮失败而不是把 `freeze_id=None` 当成"免费成功"存进去。
    """


async def freeze_one_item(cur, *, payer_user_id: int, feature_code: str,
                          task_ref: str, brand_id: Optional[int],
                          expected_points: int, authority: str,
                          extra_cost: int = 0,
                          reason: str = "GEO 图文逐项冻结",
                          organization_context: Any = None) -> dict[str, Any]:
    """冻结**一项**,返回完整 direct 句柄。

    🔴 在调用方事务内(`_cursor=cur`)。异常时由调用方整体 rollback,
       provider 只能在 commit 之后开始 —— 本函数不发起任何外部调用。
    """
    from middleware.billing import freeze_points

    # 🔴 [返工 2026-08-18 · P0-02] 原来这里写死 `base_pricing_extra = 0`,
    #    于是**多卡加价从不进冻结额**:`feature_pricing` 只含首卡单价,
    #    额外卡的钱在预览里算了、在按钮上显示了、却没有被冻住。
    #    结果是"预览 470、冻结 390",差额在结算时才暴露 —— 而那时用户早已离开。
    #    加价必须与预览用**同一个** extra 走完 freeze→commit 全程。
    result = await freeze_points(
        int(payer_user_id), feature_code,
        task_ref=task_ref, brand_id=brand_id,
        extra_cost=int(extra_cost),
        reason=reason,
        _cursor=cur,
        _organization_context=organization_context,
    )
    result = dict(result or {})

    if result.get("admin_exempt"):
        # 🔴 走到这里 = payer 仍是 admin ⇒ P0-6 的零记账形态又出现了。
        #    不接受、不静默,直接抛 —— 让"忘了路由到平台账"这件事响亮。
        raise FreezeProducedNoHandle(
            f"payer={payer_user_id} 是管理员,冻结走了 admin 免单旁路(freeze_id=None)。"
            "裁定 P0-6:admin/平台侧必须路由到平台直营账号做真实记账,"
            "不得以零 handle 冒充已扣费。"
        )
    freeze_id = result.get("freeze_id")
    if not freeze_id:
        raise FreezeProducedNoHandle(
            f"冻结未产出 freeze_id(amount={result.get('amount')},"
            f"free={result.get('free')});不接受无句柄的『成功』"
        )

    freeze_table = str(result.get("freeze_table") or "")
    if freeze_table not in ALLOWED_FREEZE_TABLES:
        raise FundingHandleInvalid(
            f"未知 freeze_table {freeze_table!r};只允许 {sorted(ALLOWED_FREEZE_TABLES)}"
        )

    amount = int(result.get("amount") or 0)
    if amount != int(expected_points):
        raise FundingHandleInvalid(
            f"冻结额 {amount} 与权威价 {expected_points} 不一致(authority={authority});"
            "整项回滚并要求重新确认"
        )

    return build_direct_handle(
        payer_user_id=int(payer_user_id),
        freeze_id=int(freeze_id),
        freeze_table=freeze_table,
        task_ref=str(task_ref),
        reserved_amount=amount,
        physical_split_snapshot={
            "amount_paid": int(result.get("amount_paid") or 0),
            "amount_bonus": int(result.get("amount_bonus") or 0),
        },
    )


def describe_admin_routing() -> dict[str, str]:
    """给交付单/审计用的一句话说明:P0-6 是怎么在不碰保护文件的前提下落地的。"""
    return {
        "problem": "middleware/billing.freeze_points 对 admin 返回 freeze_id=None(零记账)",
        "constraint": "middleware/billing.py 是保护文件,零 diff",
        "solution": "admin/平台侧把 payer 换成平台直营账号(该账号经校验非 admin),"
                    "走正常冻结路径 → 真实句柄",
        "authority": AUTHORITY_ADMIN_EXEMPT,
    }
