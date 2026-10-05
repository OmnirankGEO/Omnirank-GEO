# -*- coding: utf-8 -*-
"""WO_242 · 让「这一条查询失败」真的只影响这一条。

═══════════════════════════════════════════════════════════════════════
为什么需要它
═══════════════════════════════════════════════════════════════════════
PostgreSQL 里**一条语句失败会把整个事务置成 aborted**,之后每一条语句都返回
`current transaction is aborted, commands ignored until end of transaction block`。

于是这个看着最稳妥的写法:

```python
try:
    cur.execute("SELECT * FROM 某张可能不存在的表 ...")
except Exception:
    return 0, 0          # 「优雅降级」
```

**不是降级,是把调用方的事务打废了。** 后面每一条语句都会失败,
而它们各自的 `except` 会把失败也吞掉 ⇒ **整段静默失效,页面照常 200**。

生产实例(台账 09-19 13:20):`services/quote_numeric_repair.py`
查不存在的 `quote_packages` → 事务判废 → 紧接着的
`SELECT 1 FROM keyword_selection_sessions` 与 **`UPDATE quotes SET total_articles=…`
一起静默失败**(09-18 一天 7 次)⇒ **修复路径三个月零写入**。

🔴 中间那一步格外毒:会话探测失败后落 `_has_pending_session = False`,
   而 `False` 的含义是「没有进行中的会话 ⇒ **可以写**」——
   事务被打废反而让代码**以为**可以写,然后写失败。**两次吞异常叠在一起,方向相反。**

═══════════════════════════════════════════════════════════════════════
为什么是 SAVEPOINT,不是 `to_regclass` 预探
═══════════════════════════════════════════════════════════════════════
预探只挡「表不存在」这一种。列名变了、类型不兼容、权限不足、语句超时 ——
任何一种都会把事务打废,而预探一个都挡不住。
**SAVEPOINT 挡的是「失败」本身,不是某一种失败的原因。**

(本仓既有判断:复用调用方游标时,**唯一的隔离手段是 SAVEPOINT** ——
 见 a-swallowed-query-error-without-rollback-kills-every-later-query-on-that-connection。)

🔴 连接若是 autocommit,`SAVEPOINT` 无事务可依附会直接报错。
   那种模式下单条失败本来就不会污染别的语句 ⇒ **降级成裸 try**。
   (`api/brand_api.py:615-616` 那条注释说的就是这件事:池化连接可能被
    前序只读/DDL 调成 autocommit,裸用 SAVEPOINT 会当场 500。)

═══════════════════════════════════════════════════════════════════════
本文件是这个 helper 的**唯一规范出处**
═══════════════════════════════════════════════════════════════════════
先把事实说准(我第一版在这里写错过,已订正):
**`SAVEPOINT` 在本仓是既有的常用手法 —— 41 个生产文件里都有**,绝大多数是就地
`cur.execute("SAVEPOINT …")`,那样用完全正常,不是重复实现。

真正稀少的是**把它包成上下文管理器**这一种:改动前全仓**只有一处**
`services/settlement_adjudicator.py:_savepoint`(正确,含 autocommit 降级,
被结算证据包 4 处使用)。**本模块照抄它的语义**——它是被生产验证过的那一份——
只是给它一个公共的家,好让 `services/` 之外的模块不必从结算模块里 import 私有名。

🔴 **本单不动 `settlement_adjudicator`**:那是资金路径,收敛两份实现要单独评审。
   它已登记在 `tests/txn_savepoint_2026_09_19/` 里;**再出现第四个同形 helper
   且未登记会红**(就地 `SAVEPOINT` 用法不受影响,那不是本锁管的事)。
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

logger = logging.getLogger("GEO-TxnGuard")

#: 吞掉一条查询时的固定前缀 —— 巡检按它 grep。
#: 🔴 吞掉本身必须留痕:没有痕迹的降级就是「另一条无人看管的主路」
#:    (WO_240 那一课)。这里不去重、不限流。
MARKER = "TXN_GUARD_SWALLOWED"


@contextmanager
def savepoint(cur, name: str):
    """在调用方事务里开一个保存点,失败时只回滚到这里,**连接仍然可用**。

    异常照旧向外抛 —— 本 helper 只负责「事务还能用」,
    **要不要吞是调用方的事**(吞的地方请用 `swallow()`,它会留痕)。
    """
    nested = True
    try:
        cur.execute("SAVEPOINT " + name)
    except Exception:                                    # noqa: BLE001
        nested = False                                   # autocommit:无事务可依附
    try:
        yield
    except Exception:
        if nested:
            cur.execute("ROLLBACK TO SAVEPOINT " + name)
        raise
    else:
        if nested:
            cur.execute("RELEASE SAVEPOINT " + name)


@contextmanager
def swallow(cur, name: str, *, where: str):
    """`savepoint` + 吞掉异常 + **出声**。

    用在「这一条取不到就算了,但后面还要继续用这个连接」的地方。
    与裸 `try/except` 的区别只有一个,而那一个就是本单的全部:
    **裸 try 之后连接是废的,这里之后连接是好的。**
    """
    try:
        with savepoint(cur, name):
            yield
    except Exception as exc:                             # noqa: BLE001
        try:
            logger.warning("%s where=%s reason=%s detail=%s",
                           MARKER, where, type(exc).__name__, str(exc)[:200])
        except Exception:                                # noqa: BLE001
            pass                                         # 出声失败不许影响业务
