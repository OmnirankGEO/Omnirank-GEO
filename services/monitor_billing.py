"""监测订阅的**计费主体**解析 —— 一处口径。

[WO_ORPHAN_MONITOR_FIX 2026-08-10 ①]

C 端确认报价要真建监测订阅,而订阅是**每天扣费**的(`monitoring_keyword_daily`)。
钱必须落到**品牌 owner(服务商)**头上 —— C 端客户 token-only、不登录,
没有"操作者"可言;真落到客户头上就是收不到钱,落到 admin 头上就是白烧。

口径与 `server.py::_resolve_monitor_billing_user`(2026-06-10 P0-4 返修版)逐字一致:
`brands.owner_user_id`,**fail-closed** —— 无 brand / 孤儿 brand / owner 为 NULL /
软删 / 查询失败,一律返 None,调用方必须拒绝创建订阅,**禁止回落操作者**
(旧版回落 operator 导致 prod 18/19 订阅挂 user_id=1、admin 免扣、每天白烧四引擎)。

🔴 为什么不直接 import server.py 那个 helper:
  1. `api/selection_api.py` 被 `server.py` 反向导入,直接 import 会成环;
  2. `tests/test_p04_monitor_billing_owner_2026_06_10.py` 是把 server.py 里那段
     **源码文本抠出来 `exec()`** 跑真值表的 —— 把它的函数体改成"转调共享模块",
     那段 exec 会当场 NameError,一条已上线的资金 P0 回归测试会被我改红。
  所以 server.py 那份**一个字不动**,这里是给新调用方(以及未来收敛)的共享实现,
  并由 `tests/orphanmon_2026_08_10/` 里的一致性锁钉死两份行为必须相同(全真值表逐格比对)。

🔴 本模块的函数接**调用方的 cursor**:计费主体解析要跟"标 is_monitored + 建订阅"
  在同一个事务里读,才不会出现"读 owner 时还在、写订阅时已改"的缝。
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("GEO-MonitorBilling")

__all__ = ["resolve_monitor_billing_user_with_cursor"]


def resolve_monitor_billing_user_with_cursor(cur, brand_id) -> Optional[int]:
    """监测订阅计费主体 = `brands.owner_user_id`;拿不准一律返 None(fail-closed)。

    参数:
      cur      —— 调用方事务里的 cursor(本函数只读,不 commit / 不 rollback)
      brand_id —— 品牌 id;为空直接 None

    返回:owner_user_id(int) 或 None。**None 的语义是"不许建订阅"**,不是"用别人顶上"。
    """
    if not brand_id:
        logger.warning("[monitor-billing] brand_id 为空 · fail-closed 拒开通")
        return None
    try:
        cur.execute(
            "SELECT owner_user_id FROM brands WHERE id = %s AND COALESCE(is_deleted, FALSE) = FALSE",
            (brand_id,),
        )
        row = cur.fetchone()
    except Exception as exc:
        logger.warning(
            "[monitor-billing] owner 查询失败 · fail-closed 拒开通 brand=%s: %s", brand_id, exc)
        return None
    owner = None
    if row is not None:
        owner = row.get("owner_user_id") if hasattr(row, "get") else row[0]
    if not owner:
        logger.warning(
            "[monitor-billing] brand=%s 无 owner(NULL/孤儿/软删)· fail-closed 拒开通", brand_id)
        return None
    return int(owner)
