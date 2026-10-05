# -*- coding: utf-8 -*-
"""WO_224-c1 §3.2 锁 —— 豆包并发闸。

Deploy 224-d1 实证:同秒并发 6–13 个**全部被 429 拒**,同一个 task 的四引擎
发起时刻差**全部 = 0.0 秒**(完全并行扇出)。⇒ 根因是并发打爆**账号级 QPS**,
不是配额;按小时均值看会得出「量不大」的错觉。

本文件的四条腿:
  · 身份 —— 同一个事件循环必须拿到**同一把**信号量(每次新建 = 等于没限);
  · 效力 —— 12 路并发,峰值不超上限;
  · 接线 —— 闸真的被**出网点**持有(不是只定义在模块里);
  · 系数 —— 上限从后台可调的登记键取,不是代码里另写一份数。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import doubao_concurrency as D          # noqa: E402


# ════════════════════════════════════════════════════════════════
# 身份:同一个循环同一把
# ════════════════════════════════════════════════════════════════

def test_the_same_loop_gets_the_same_semaphore():
    """🔴 每次新建一把 = **等于没限**:每个调用者各有一份预算,合起来无上限。

    光看「并发没超」是看不出来的 —— 串行跑的判据在「没限」下也照样绿。
    所以要钉的是**对象身份**。
    """
    async def _run():
        D.reset_for_tests()
        a = D.semaphore_for_current_loop()
        b = D.semaphore_for_current_loop()
        c = D.semaphore_for_current_loop()
        return a, b, c

    a, b, c = asyncio.run(_run())
    assert a is b is c, "同一个事件循环里拿到了不同的信号量对象 —— 上限形同虚设"


def test_a_different_loop_gets_its_own_semaphore():
    """反向对照:换一个事件循环必须换一把。

    `asyncio.Semaphore` 不能跨循环共用(跨了会抛)。这条同时是**诚实声明**:
    本闸保证的是「同一循环内不超」,不是进程总量 —— 见模块抬头。
    """
    async def _one():
        return D.semaphore_for_current_loop()

    D.reset_for_tests()
    first = asyncio.run(_one())
    second = asyncio.run(_one())
    assert first is not second


# ════════════════════════════════════════════════════════════════
# 效力:12 路并发,峰值不超上限
# ════════════════════════════════════════════════════════════════

def test_twelve_concurrent_callers_never_exceed_the_limit(monkeypatch):
    """🔴 工单 §3.2 判据:并发 12 个模拟调用,峰值并发 ≤ 上限、被拒 0。

    上限用一个**小**值(2)显式喂进来 —— 用生产默认值的话,
    默认值恰好 ≥ 12 时这条锁会变成恒真而没人发现。
    """
    LIMIT = 2
    monkeypatch.setattr(D, "get_limit", lambda: LIMIT)

    peak = {"now": 0, "max": 0}
    rejected = {"n": 0}

    async def _one():
        try:
            async with D.doubao_slot():
                peak["now"] += 1
                peak["max"] = max(peak["max"], peak["now"])
                await asyncio.sleep(0.02)      # 压住一会儿,让并发真的叠起来
                peak["now"] -= 1
        except Exception:                      # noqa: BLE001
            rejected["n"] += 1

    async def _run():
        D.reset_for_tests()
        # 🔴 自带超时:槽泄漏时这里会**永远等下去**,而挂死与「还在跑」分不开 ——
        #    仪器必须能说出「我没跑完」,不能只会沉默。实测过:注毒
        #    「异常路径不放槽」让整个注毒 harness 卡到 1800s 超时才退,
        #    那一发的读数因此拿不到。12 路 × 20ms、上限 2 ⇒ 理论 ~0.12s,给 10s 绰绰有余。
        await asyncio.wait_for(
            asyncio.gather(*[_one() for _ in range(12)]), timeout=10)

    try:
        asyncio.run(_run())
    except asyncio.TimeoutError:
        raise AssertionError(
            "12 路并发 10 秒没跑完 —— 槽只进不出(泄漏),闸把活挡死了。"
            "峰值 %s / 当前仍占 %s" % (peak["max"], peak["now"]))
    assert rejected["n"] == 0, "被拒 %d 个 —— 闸把活挡死了,不是限流" % rejected["n"]
    assert peak["max"] <= LIMIT, "峰值并发 %d 超过上限 %d" % (peak["max"], LIMIT)
    assert peak["max"] >= 2, (
        "峰值只有 %d —— 12 路并发压不出并发,这条锁在测串行,没有判别力" % peak["max"])
    assert peak["now"] == 0, "跑完还有 %d 个槽没放回去 —— 泄漏" % peak["now"]


def test_the_slot_is_released_even_when_the_body_raises():
    """🔴 异常路径也要放槽。不放就是把上限一点点吃光:

    表现为「跑着跑着全卡住」,而每一条日志都正常。
    """
    LIMIT = 1

    async def _body():
        D.reset_for_tests()
        sem = None
        for _ in range(3):
            try:
                async with D.doubao_slot() as _slot:
                    sem = _slot._sem
                    raise RuntimeError("body 炸了")
            except RuntimeError:
                pass
        return sem

    async def _run():
        # 🔴 自带超时:上限=1 且异常不放槽时,**第二次 acquire 永远等** ——
        #    挂死与「还在跑」分不开。实测过:注毒 harness 因为这条卡到超时,
        #    那一发的读数直接拿不到(仪器要能说出「我没跑完」)。
        return await asyncio.wait_for(_body(), timeout=10)

    try:
        sem = asyncio.run(_run())
    except asyncio.TimeoutError:
        raise AssertionError(
            "三次异常之后拿不到槽了 —— 异常路径没放槽,上限被一点点吃光")
    assert sem is not None
    # 三次异常之后信号量必须仍是满的(能立刻再拿到)
    async def _probe():
        return sem.locked()
    assert asyncio.run(_probe()) is False, "异常路径没放槽,上限被吃光了"


# ════════════════════════════════════════════════════════════════
# 接线:闸真的被出网点持有(不是只定义)
# ════════════════════════════════════════════════════════════════

def test_the_gate_is_actually_held_by_the_only_outbound_point():
    """🔴 「定义了一把信号量」和「出网那行真的持着它」是两件事。

    本条**真调** `_query_doubao_search_impl`,把 `httpx.AsyncClient` 桩掉,
    在桩里问「此刻信号量是不是被占着」。
    只 grep 源码里有没有 `doubao_slot` 是**源码切片**,不执行分支,抓不到
    「import 了但没 async with」这种(WO_221 那次的 Pc 就是这么活下来的)。
    """
    import httpx

    import tools.ai_visibility.ai_tester as T

    held = {"seen": False, "checked": False}

    class _FakeClient:
        async def __aenter__(self):
            held["checked"] = True
            sem = D.semaphore_for_current_loop()
            held["seen"] = sem.locked()        # 上限=1 时:占着就是 locked
            raise RuntimeError("到此为止 —— 本锁只问「进来时槽被占着没有」")

        async def __aexit__(self, *a):
            return False

    # 🔴 没 key 的话 `_query_doubao_search_impl` 在出网**之前**就 return 了
    #    (:880 `if not api_key`),于是这条锁会「绿得像通过」其实什么都没走到。
    #    ——「出网那行根本没被走到」这句断言就是为它准备的,第一次跑正是它判的红。
    _orig_cfg = dict(T.DOUBAO_CONFIG)
    T.DOUBAO_CONFIG["api_key"] = "wo224-lock-fake-key"
    T.DOUBAO_CONFIG["seed_api_key"] = "wo224-lock-fake-key"

    async def _run():
        D.reset_for_tests()
        import services.doubao_concurrency as _D
        _orig = _D.get_limit
        _D.get_limit = lambda: 1               # 上限 1 ⇒ 占住就 locked
        _orig_client = httpx.AsyncClient
        httpx.AsyncClient = lambda *a, **k: _FakeClient()
        try:
            # 🔴 自带超时:`_query_doubao_search_impl` 里有 3 次重试,
            #    槽泄漏时**第二次 acquire 永远等** —— 挂死而不是红。
            #    这同时说明泄漏在生产里不是「变慢」,是把整个请求挂死。
            await asyncio.wait_for(
                T._query_doubao_search_impl("判据问法", "判据品牌"), timeout=10)
        except Exception:                      # noqa: BLE001
            pass
        finally:
            httpx.AsyncClient = _orig_client
            _D.get_limit = _orig

    try:
        asyncio.run(_run())
    finally:
        T.DOUBAO_CONFIG.clear()
        T.DOUBAO_CONFIG.update(_orig_cfg)
    assert held["checked"], "出网那行根本没被走到 —— 这条锁测的是空气"
    assert held["seen"] is True, "出网时并没有持着并发槽 —— 闸只是定义在那里"


# ════════════════════════════════════════════════════════════════
# 系数:从后台可调的登记键取
# ════════════════════════════════════════════════════════════════

def test_the_limit_comes_from_the_registered_tunable_key():
    """🔴 上限是**系统系数**,登记在 `ADMIN_SETTINGS_DEFAULTS`,后台可调。

    这里**不断言具体数值**(工单 §3.2:文档不记数),只断言:
      · 键已登记(没登记的话 `get_admin_setting` 直接返 default,后台改了不生效);
      · 兜底值从登记表取,不是模块里另写一份;
      · 后台返什么就用什么。
    """
    from db.social_preferences_db import ADMIN_SETTINGS_DEFAULTS, ADMIN_SETTINGS_KEYS

    assert D.SETTING_KEY in ADMIN_SETTINGS_KEYS, (
        "键没登记 —— `get_admin_setting` 会直接返 default,后台怎么改都不生效")
    assert D._registered_default() == int(ADMIN_SETTINGS_DEFAULTS[D.SETTING_KEY][0]), (
        "兜底值不是从登记表取的 —— 一个系数两处字面量,改一处就静默分叉")
    assert D._registered_default() >= 1


def test_the_limit_follows_the_backend_value(monkeypatch):
    """后台给什么就用什么;给不出/给了非法值 ⇒ 回落登记默认值(**更保守**那侧)。"""
    import db.social_preferences_db as S

    monkeypatch.setattr(S, "get_admin_setting", lambda k, c=str, d=None: 7)
    assert D.get_limit() == 7

    monkeypatch.setattr(S, "get_admin_setting", lambda k, c=str, d=None: 0)
    assert D.get_limit() == D._registered_default(), "0 是非法上限,必须回落而不是变成「不限」"

    def _boom(*a, **k):
        raise RuntimeError("配置读不到")

    monkeypatch.setattr(S, "get_admin_setting", _boom)
    assert D.get_limit() == D._registered_default(), "读不到时没回落到登记默认值"


def test_the_registered_default_falls_back_to_serial_not_to_a_big_number():
    """🔴 登记表读不到时,兜底必须落到**更保守**那一侧(串行 1),不是一个大数。

    Review 2026-09-15 复审的探索性注毒 P8(`_registered_default` 回落 999)**存活** ——
    我钉过「后台值非法要回落」,却没钉「**回落到哪一侧**」。
    两条是不同的面:前者管「用不用后台值」,后者管「用不上时按几并发打出去」。

    回落方向不对称,所以只能朝小的那边:
      · 回落到 1 —— 最坏是慢(串行),账号不会被打爆;
      · 回落到一个大数 —— 正好在「配置读不到」这种异常时**放开并发**,
        而 429 的根因就是并发。异常路径把限流变成放行,是最糟的一种回落。
    """
    import db.social_preferences_db as S

    real_default = D._registered_default()

    class _NoKey(dict):
        def __getitem__(self, k):
            raise KeyError(k)

    original = S.ADMIN_SETTINGS_DEFAULTS
    S.ADMIN_SETTINGS_DEFAULTS = _NoKey()
    try:
        fallen = D._registered_default()
    finally:
        S.ADMIN_SETTINGS_DEFAULTS = original

    assert fallen == 1, (
        "登记表读不到时没退化成串行:%s —— 异常路径把限流变成了放行" % fallen)
    assert fallen <= real_default, (
        "兜底值(%s)比登记值(%s)还大 —— 回落朝着放开并发的方向" % (fallen, real_default))

    # 读不到登记表时,对外的 get_limit 也必须跟着保守
    S.ADMIN_SETTINGS_DEFAULTS = _NoKey()

    def _boom(*a, **k):
        raise RuntimeError("后台也读不到")

    real_get = S.get_admin_setting
    S.get_admin_setting = _boom
    try:
        assert D.get_limit() == 1, "两头都读不到时上限不是 1:%s" % D.get_limit()
    finally:
        S.ADMIN_SETTINGS_DEFAULTS = original
        S.get_admin_setting = real_get

    # 🔴 夹紧那一段也要真走一遍:登记值被写成 0(「0 = 不限」是很自然的误解)时,
    #    `max(1, ...)` 必须把它拉回 1。
    #    不走这一臂的话,把 `max(1, ...)` 删掉判据照样绿 —— 它对当前登记值(>1)是
    #    **冗余守卫**,而冗余目标上的毒仍绿不代表锁没牙,代表这一臂没人走。
    S.ADMIN_SETTINGS_DEFAULTS = dict(original)
    S.ADMIN_SETTINGS_DEFAULTS[D.SETTING_KEY] = ("0", "int", "判据临时置 0")
    try:
        assert D._registered_default() == 1, (
            "登记值 0 没被夹回 1 —— 「0 = 不限」这个误解会直接变成不限并发:%s"
            % D._registered_default())
    finally:
        S.ADMIN_SETTINGS_DEFAULTS = original
