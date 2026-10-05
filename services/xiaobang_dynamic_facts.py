"""小榜「动态事实」只读适配层(工单 §4 P1-5 / §6 包 C③ / §8 S09)。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 它存在的理由

静态知识(概念、流程、字段含义、非时变排错步骤)可以写进 KB;
**时变事实**(功能消耗多少算力、套餐、余额、席位、权限、开关)不行 ——
写进去就会在后台改配置后**继续对用户讲旧口径**,而且没有任何人会收到通知。
这正是 35 班之前烂掉的机制,也是本包 corpus lint 在现役语料里抓到 18 处的那类。

## 三条硬规矩

1. **只读**。本模块只 SELECT,不写任何表、不扣费、不建任务。
   `db/wallet_db.py` 是零 diff 保护文件 —— 这里**只调用**它的读函数,一个字不改。
2. **拿不到就说拿不到**。任何异常/缺表/缺行 → 返回 `available=False` +
   一个人话原因,**绝不**回退到常量。
   工单 §8 S09 原话:「源不可用给 O1 重试/人工,禁止固定 130/300 等常量兜底」。
   —— 回退常量是最坏的一种:用户拿到一个**看起来对**的错数字。
3. **不背数字**。本模块自身不含任何价格常量(判据 `test_module_contains_no_price_constants`
   逐字扫源码钉这一条)。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

_LOGGER = logging.getLogger("GEO-XiaobangDynamicFacts")

#: 对外统一术语。DP-B4:只许「算力」。
COMPUTE_UNIT = "算力"


@dataclass(frozen=True)
class DynamicFact:
    """一条动态事实的取数结果。

    `available=False` 时 `amount` 必须是 None —— 不允许「不可用但带个数」这种
    半吊子状态,那正是常量兜底混进来的入口。
    """
    key: str
    available: bool
    amount: Optional[int] = None
    unit: str = COMPUTE_UNIT
    display_name: str = ""
    unavailable_reason: str = ""

    def __post_init__(self):
        if not self.available and self.amount is not None:
            raise ValueError("不可用的动态事实不许带数值(这是常量兜底的入口)")


#: 源不可用时给用户的话 —— O1:说清楚 + 给出口,不报错误码。
UNAVAILABLE_COPY = (
    "我这会儿读不到最新的{what},没法给你一个准数。"
    "你可以在功能页面下单前的确认页看到当次要用多少{unit};"
    "要是那边也不对,点下方把问题提交给工作人员,我把这次的情况一起带过去。"
)


def unavailable(key: str, what: str, reason: str) -> DynamicFact:
    return DynamicFact(key=key, available=False, unavailable_reason=reason,
                       display_name=what)


def feature_price(feature_code: str) -> DynamicFact:
    """读一个功能的**当前**算力消耗。

    走 `db.wallet_db.get_feature_pricing`(现役只读函数),不复制它的 SQL ——
    复制一份 SQL 就是第二套计费口径,工单 §7.4 明令禁止。
    """
    try:
        from db.wallet_db import get_feature_pricing
        row = get_feature_pricing(feature_code)
    except Exception as exc:  # 缺表/缺行/连不上库,一律 O1
        _LOGGER.warning("[xiaobang] feature_price(%s) 取数失败: %s", feature_code, exc)
        return unavailable(feature_code, "功能消耗", type(exc).__name__)

    if not isinstance(row, dict):
        return unavailable(feature_code, "功能消耗", "unexpected_row_shape")

    # 现役表里两列都可能承载消耗:cost_compute(新)/ cost_points(旧)。
    # 取值时**不猜**:两列都没有就是取不到,不编。
    raw = row.get("cost_compute")
    if raw is None:
        raw = row.get("cost_points")
    try:
        amount = int(raw)
    except (TypeError, ValueError):
        return unavailable(feature_code, "功能消耗", "no_cost_column")

    return DynamicFact(
        key=feature_code,
        available=True,
        amount=amount,
        display_name=str(row.get("feature_name") or feature_code),
    )


def describe(fact: DynamicFact) -> str:
    """把一条动态事实渲染成给用户看的一句话。

    可用 → 明确标注这是**当前**值(工单 §4 P1-5:必须注明「当前账户/当前状态」);
    不可用 → O1 文案,带重试与人工出口。
    """
    if fact.available:
        return "{0}当前消耗 {1} {2}(以下单前确认页显示的为准)。".format(
            fact.display_name, fact.amount, fact.unit)
    return UNAVAILABLE_COPY.format(what=fact.display_name or "价格", unit=COMPUTE_UNIT)
