# -*- coding: utf-8 -*-
"""WO_240 · 让「进入 fallback」这件事出声。**行为零变化**。

═══════════════════════════════════════════════════════════════════════
为什么有这个模块
═══════════════════════════════════════════════════════════════════════
2026-04-11 → 05-25,`server.py` 里这一行按 **390** 扣了 36 笔真客户算力:

    pricing = get_feature_pricing("topic_gen")
    base    = int(pricing.get("cost_points", 390))     # 目录现值 80

它**不需要目录"出问题"才生效** —— 只需要取价那一步没走通,而那一步走没走通,
**没有任何东西会出声**。于是它咬了六周、扣的主要是付费算力、没有一个人发现:
因为对每一层观察者来说系统都在"正常工作" —— 有价、有扣费、有回执。
**fallback 不会报错,这正是它的定义。**

所以本模块不改任何取价结果,只做一件事:**取到的是写死的那个数时,出一行声。**

🔴 **不去重、不限流。** 去重会让「查日志查不到」同时意味着"没发生"和"发生过但被吞了",
   而这两件事的处置相反(本仓 derive-the-emission-condition-before-reading-absence:
   读"日志里没有"之前必须能答出"什么条件才出声")。这些都是资金路径,量很低,
   宁可吵。**出声条件只有一个:走了 fallback。**

🔴 **出声本身绝不能改变行为**:emit 全程包在 try/except 里,日志坏了也不许影响扣费。

═══════════════════════════════════════════════════════════════════════
`priced()` 与原写法**逐字等价**
═══════════════════════════════════════════════════════════════════════
    原:  int(pricing.get("cost_points", 390))
    新:  int(priced(pricing, "cost_points", 390, feature=..., where=...))

包括这三种容易被"顺手改好"的边角,一个都没动:

| 情形 | 原行为 | 本模块 |
|---|---|---|
| `pricing` 是 None | `AttributeError` | **照样 AttributeError** |
| 键在、值是 `None` | 返回 `None`(随后 `int(None)` 抛) | **照样返回 None** |
| 键不在 | 返回字面量 | 返回字面量 **+ 出声** |

🔴 第一行那个 `AttributeError` 是**故意保留**的。把 `pricing.get(...)` 顺手写成
   `(pricing or {}).get(...)` 看着像"更健壮",实际是把 fail-fast 改成 fail-soft
   —— 正是本单要治的病。调用点原来写的是哪种,就还是哪种。

🔴 第二行同理:`.get(k, d)` 在「键在、值为 None」时返回 `None` 而不是 `d`。
   把这两种压成一种会**改变扣费金额**,而两边看起来都"对"。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Optional

logger = logging.getLogger("GEO-Fallback")

#: 日志行固定前缀。改这个串等于改对外契约。
#:
#: 🔴 **接收方点名**:`C:/AI-Test/.deploy_toolkit/_rv_fallback_listen.sh`(只读巡检)。
#:    它按**字段**读(`feature` / `where` / `used` / `reason`),不按形状读 ——
#:    改字段名它会红,改日志排版不会。
#:    它分三态:发射器没上线 ⇒ **退 3**(不给一个看起来像通过的 0)·
#:    在线且窗口内没发生 ⇒ 退 0 · 发生过但日志被部署冲掉 ⇒ 打印容器启动时刻。
#:
#: 🔴 为什么要点名而不是写「巡检会 grep 它」:2026-09-18 本模块刚上线时,
#:    `FALLBACK_FIRED` 全仓只出现一次 —— 就是这一行定义本身。
#:    发射器装好了、判据全绿、**接收端根本不存在**,而注释里写着有人在听。
#:    「只活在注释里的约束传不出去」,同一天撞第二次。
MARKER = "FALLBACK_FIRED"

_MISSING = object()


def fired(
    *,
    feature: str,
    where: str,
    key: str,
    used: Any,
    reason: str,
    detail: Optional[str] = None,
) -> None:
    """记一行:某处取不到权威值,用了写死的 `used`。

    字段(结构化,一行一个 JSON,别改字段名 —— 巡检按字段读不按形状读):
      · `feature` 计费目录里的 feature_code("这是给谁定价")
      · `where`   `文件:函数` (**不写行号** —— 行号一改就对不上,函数名稳定)
      · `key`     取的哪个字段
      · `used`    这次实际用掉的写死值
      · `reason`  见下表 —— **巡检按它分类,新增一种要同步 `_rv_fallback_listen.sh`**
      · `detail`  自由文本(异常类名 / 目录现值 / 比对结果)

    | reason | 意思 | 该查什么 |
    |---|---|---|
    | `missing_key` | 取价结果里没这个键 | 目录那一行的形状变了 |
    | `lookup_raised` | 取价整个抛了 | 连不上库 / 这个 code 不在目录里 |
    | `unknown_feature` | 连影子目录里都没有 ⇒ 按一个谁也没定过的价 | **这个功能根本没定过价** |
    | `copy_diverged` | 对客文案里那份副本与目录现值对不上 | 谁改了目录没改文案 |
    | `item_dropped` | 对客列表里**少了一项**(不是数错) | 短一项不会报错,只能靠这行看见 |

    🔴 这五种的处置方向**互不相同**,压成一种(比如统一叫"降级")会让
       日志读起来像同一件事;而「按错价收了钱」和「少列了一项」需要找的人都不一样。
    """
    try:
        payload = {
            "feature": feature, "where": where, "key": key,
            "used": used, "reason": reason,
        }
        if detail:
            payload["detail"] = detail
        logger.warning("%s %s", MARKER, json.dumps(payload, ensure_ascii=False,
                                                   default=str))
    except Exception:                                    # noqa: BLE001
        # 🔴 出声失败不许影响扣费。这里**不能**再 raise,也不能 print ——
        #    这一行正跑在计费路径上。
        pass


def priced(
    mapping: Mapping[str, Any],
    key: str,
    default: Any,
    *,
    feature: str,
    where: str,
) -> Any:
    """等价于 `mapping.get(key, default)`,但取到 `default` 时出一行声。

    `mapping` 为 None 时照旧抛 `AttributeError` —— 见模块抬头,那是故意的。
    """
    value = mapping.get(key, _MISSING)
    if value is _MISSING:
        fired(feature=feature, where=where, key=key, used=default,
              reason="missing_key")
        return default
    return value


def raised(
    *,
    feature: str,
    where: str,
    key: str,
    used: Any,
    exc: BaseException,
) -> None:
    """`except` 分支里用:取价整个抛了,退到写死值。

    与 `missing_key` 分开记 —— 两者的排查方向完全不同:
    一个是"表里没这行",一个是"根本没连上表"。
    """
    fired(feature=feature, where=where, key=key, used=used,
          reason="lookup_raised", detail=type(exc).__name__)


def copy_diverged(
    *,
    feature: str,
    where: str,
    used: Any,
    catalog: Any,
) -> None:
    """对客文案里那份**目录值的副本**与目录现值对不上了。

    用在「按钮级确认扣费」这种**先对客户说一个数、再由别处真扣**的地方:
    客户是**按那个数点的确认**,副本一漂,当场对客说错话。
    这里只出声、**不改显示的数** —— 改对客数值属五类之一(对客改动),等 Owner。
    """
    fired(feature=feature, where=where, key="cost_points", used=used,
          reason="copy_diverged", detail="catalog=%r" % (catalog,))
