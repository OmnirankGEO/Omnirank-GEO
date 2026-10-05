# -*- coding: utf-8 -*-
"""豆包出网并发闸(WO_224-c1 §3.2)。

═══════════════════════════════════════════════════════════════════
🔴 根因是**并发打爆账号 QPS**,不是配额

Deploy 224-d1 只读实测(2026-09-15):
  · 逐日失败率 32%–48%(当天 33.3%),**同秒并发 6–13 个全部被拒**;
  · 同一个 task 的四引擎发起时刻差**全部 = 0.0 秒** —— 完全并行扇出;
  · 格数 4 → 56(= 4 × 关键词数)。
⇒ 429 是**账号级 QPS**(不是单 key、不是当日配额)。
   按小时均值看会得出「量不大」的错觉 —— 要看**同秒并发数**。

🔴 闸放在哪:`tools/ai_visibility/ai_tester._query_doubao_search_impl`

  那是豆包**唯一的出网点**:`query_doubao_search` 的三条路径
  (enhanced / enhanced 失败后 fallback standard / 直接 standard)全过它,
  全仓再无第四个调用者(`git grep _query_doubao_search_impl` = 3 调用 + 1 定义)。
  放在监测的 `PlatformAdapter` 那一层是不够的:**诊断线**
  (`ai_tester` 自己的 5 处)与情感分类(`tools/sentiment_classifier.py:99`)
  打的是**同一个账号**,漏掉它们,账号级 QPS 照样被打爆 ——
  而监测这边的读数会显示「闸生效了」。

🔴 能保证什么、不能保证什么(不许含糊)

  能:**同一个事件循环内**的并发不超过上限。Deploy 实测的那个形态
      (一个 task 的四引擎 × N 关键词在**一次**扇出里并行)正是同一个循环,
      所以这条正对根因。
  不能:跨事件循环 / 跨进程的总量。本仓多处用 `asyncio.run(...)` 与
      `new_loop.run_until_complete(...)`,每次是新循环 ⇒ 各自一份预算。
      若将来两个循环真的同时在跑,总并发可达 N × 上限。
      真正的账号级总量闸需要跨进程协调(Redis 令牌桶之类),**不在本单范围**;
      429 仍走既有重试。这一条写在这里,是为了不让下一个人以为它已经解决了。
═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Tuple  # noqa: F401

logger = logging.getLogger("GEO-DoubaoConcurrency")

#: 后台可调系数的键。与 `geo_vision_model` 同法(WO_220-c1 的先例):
#: 值登记在 `db.social_preferences_db.ADMIN_SETTINGS_DEFAULTS`,`ensure_schema()` 播种进库,
#: 运营在后台改。
#: 🔴 数值**不写进文档**(工单 §3.2:「默认值为系统系数、后台可调,文档不记数」)。
SETTING_KEY = "geo_doubao_max_concurrency"


def _registered_default() -> int:
    """兜底值**从登记表取**,不在本模块另写一份数字。

    🔴 一个系数两处字面量,改一处就静默分叉 —— 这正是 WO_225 那次
       「判据从被测常量推导 = 数值没钉」的对偶面:实现从两个源取值 = 数值没定。
       登记表读不到(模块结构变了)⇒ 1:退化成串行,慢但**不会**打爆账号;
       回落必须落在「更保守」那一侧。
    """
    try:
        from db.social_preferences_db import ADMIN_SETTINGS_DEFAULTS
        return max(1, int(ADMIN_SETTINGS_DEFAULTS[SETTING_KEY][0]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[doubao] 读不到登记默认值(%s),退化成串行", exc)
        return 1


def get_limit() -> int:
    """当前并发上限。后台值 > 登记默认值 > 1。

    🔴 读不到不抛:配置读不到不该让识图/监测整条挂掉(与 `vision_routing` 同纪律)。
       但回落方向是**更小**(更保守),不是更大 —— 放大上限正是要防的那件事。
    """
    fallback = _registered_default()
    try:
        from db.social_preferences_db import get_admin_setting
        raw = get_admin_setting(SETTING_KEY, int, fallback)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[doubao] 读后台并发上限失败(%s),用登记默认值", exc)
        return fallback
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return fallback
    return value if value >= 1 else fallback


#: 每个事件循环一把信号量。`asyncio.Semaphore` 在不同循环间不可共用。
#:
#: 🔴 键是**循环对象本身**(弱引用),不是 `id(loop)`。
#:    第一版用 `id(loop)`,判据 `test_a_different_loop_gets_its_own_semaphore` 当场判红:
#:    CPython 会**复用已回收对象的地址** —— `asyncio.run()` 跑完循环被回收,
#:    下一次 `asyncio.run()` 的新循环拿到**同一个 id**,于是取回一把绑在**死循环**上的
#:    信号量。那把信号量一旦被 await 就会抛(跨循环),而症状出现在业务代码里。
#:    `WeakKeyDictionary` 让条目随循环一起消失,不会有陈旧复用。
_SEMAPHORES: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def semaphore_for_current_loop() -> "asyncio.Semaphore":
    """取当前事件循环那把信号量。**同一个循环必须拿到同一个对象**。

    🔴 每次新建一把 = 等于没限(每个调用者各有一份预算,合起来无上限)。
       判据 `test_the_same_loop_gets_the_same_semaphore` 钉的就是这条身份,
       注毒「每次 new 一把」必须转红 —— 光看「并发没超」是看不出来的:
       串行跑的判据在「没限」下也照样绿。
    """
    loop = asyncio.get_running_loop()
    cached = _SEMAPHORES.get(loop)
    if cached is not None:
        return cached[0]
    limit = get_limit()
    sem = asyncio.Semaphore(limit)
    _SEMAPHORES[loop] = (sem, limit)
    logger.info("[doubao] 本事件循环并发上限已装载(loop=%s)", id(loop))
    return sem


def reset_for_tests() -> None:
    """只给判据用:清掉每循环缓存,让下一次调用重新读上限。"""
    _SEMAPHORES.clear()


class _Slot:
    """`async with doubao_slot():` —— 进出各一次,异常也放。"""

    __slots__ = ("_sem",)

    def __init__(self) -> None:
        self._sem = None

    async def __aenter__(self):
        self._sem = semaphore_for_current_loop()
        await self._sem.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        # 🔴 无论成功失败都要放 —— 不放就是把上限一点点吃光,
        #    表现为「跑着跑着全卡住」,而每一条日志都正常。
        if self._sem is not None:
            self._sem.release()
        return False


def doubao_slot() -> "_Slot":
    """取一个并发槽。用法:`async with doubao_slot(): ...`"""
    return _Slot()
