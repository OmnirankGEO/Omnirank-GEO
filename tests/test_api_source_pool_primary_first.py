# -*- coding: utf-8 -*-
"""primary-first + failover 单测(2026-06-11 打广告高并发 API 韧性)

纯逻辑·不调真 API·mock do_request。验证:
  1. 主 key(原单 key 变量)永远 keys[0]·去重保序(STEP14 把原 key append 末尾·需重排到首)
  2. pick_key primary_first attempt 按序 [主, 备用1, 备用2 ...]
  3. throttle 主 key 超速 → 返 None(调用方 attempt+1 试下一个)·主 key 可配高配额桶 primary_qpm
  4. acall_with_failover primary_first:主优先·主失败换备用·试遍全部
  5. round-robin(primary_first=False)向后兼容完全不变
  6. 单 key 向后兼容 = 调一次(deepseek 多 key 轮询同理)

直接 python 跑(内置 __main__·绕过 pytest conftest DB gate):
  python tests/test_api_source_pool_primary_first.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.api_source_pool import SourcePool, acall_with_failover


def _run(coro):
    return asyncio.run(coro)


# ===== SourcePool 主 key 重排 =====
def test_primary_key_goes_first():
    # 原 key="orig"·池 [a,b,orig](orig 在末尾·STEP14 append)→ 重排 orig 到 keys[0]
    pool = SourcePool("t", ["a", "b", "orig"], qpm_per_source=100, primary_key="orig")
    assert pool.keys[0] == "orig", pool.keys
    assert pool.primary_key == "orig"
    assert set(pool.keys) == {"a", "b", "orig"} and len(pool.keys) == 3


def test_primary_key_prepend_when_absent():
    pool = SourcePool("t", ["a", "b"], qpm_per_source=100, primary_key="orig")
    assert pool.keys == ["orig", "a", "b"]


def test_no_primary_key_keeps_order():
    pool = SourcePool("t", ["a", "b"], qpm_per_source=100)
    assert pool.keys == ["a", "b"] and pool.primary_key is None


# ===== pick_key primary_first =====
def test_pick_primary_first_attempt_order():
    pool = SourcePool("t", ["a", "b", "orig"], qpm_per_source=100, primary_key="orig")
    assert pool.pick_key(throttle=False, primary_first=True, attempt=0) == "orig"
    assert pool.pick_key(throttle=False, primary_first=True, attempt=1) == "a"
    assert pool.pick_key(throttle=False, primary_first=True, attempt=2) == "b"
    assert pool.pick_key(throttle=False, primary_first=True, attempt=3) is None  # 越界


def test_pick_primary_first_throttle_exhaust():
    # primary_qpm=1:主 key 桶容量 1·取一次后超速返 None·备用各默认桶仍可用
    pool = SourcePool("t", ["orig", "a"], qpm_per_source=100, primary_key="orig", primary_qpm=1)
    assert pool.pick_key(throttle=True, primary_first=True, attempt=0) == "orig"
    assert pool.pick_key(throttle=True, primary_first=True, attempt=0) is None  # 主桶=1 耗尽
    assert pool.pick_key(throttle=True, primary_first=True, attempt=1) == "a"   # 备用可用


# ===== round-robin 向后兼容 =====
def test_round_robin_backward_compat():
    pool = SourcePool("t", ["a", "b", "c"], qpm_per_source=100)
    picks = [pool.pick_key(throttle=False) for _ in range(6)]
    assert picks == ["a", "b", "c", "a", "b", "c"], picks


# ===== acall_with_failover primary_first =====
def test_failover_primary_first_main_ok():
    pool = SourcePool("t", ["a", "orig"], qpm_per_source=100, primary_key="orig")
    calls = []
    async def do(key):
        calls.append(key)
        return "ok:" + key
    r = _run(acall_with_failover(pool, do, throttle=False, primary_first=True))
    assert r == "ok:orig" and calls == ["orig"]  # 主优先·成功不换


def test_failover_primary_first_main_fail_switch():
    pool = SourcePool("t", ["a", "b", "orig"], qpm_per_source=100, primary_key="orig")
    calls = []
    async def do(key):
        calls.append(key)
        if key == "orig":
            raise RuntimeError("orig down")
        return "ok:" + key
    r = _run(acall_with_failover(pool, do, throttle=False, primary_first=True))
    assert r == "ok:a" and calls == ["orig", "a"]  # 主失败 → 换备用


def test_failover_primary_first_all_fail():
    pool = SourcePool("t", ["a", "orig"], qpm_per_source=100, primary_key="orig")
    calls = []
    async def do(key):
        calls.append(key)
        raise RuntimeError(key + " down")
    raised = False
    try:
        _run(acall_with_failover(pool, do, throttle=False, primary_first=True))
    except RuntimeError:
        raised = True
    assert raised and set(calls) == {"orig", "a"}  # 试遍全部仍失败 → raise


def test_failover_primary_first_throttle_skip_to_backup():
    # 主桶=1:第一次 failover 主 key 调用(do 失败)→ 但这里测 throttle 超速跳过
    # primary_qpm=1·主 key 先被 acquire 用掉·do 成功;再来一次(模拟并发第二请求)主超速跳备用
    pool = SourcePool("t", ["orig", "a"], qpm_per_source=100, primary_key="orig", primary_qpm=1)
    async def do(key):
        return "ok:" + key
    r1 = _run(acall_with_failover(pool, do, throttle=True, primary_first=True))
    assert r1 == "ok:orig"  # 第一次主 key
    r2 = _run(acall_with_failover(pool, do, throttle=True, primary_first=True))
    assert r2 == "ok:a"     # 主超速 → 跳备用(不提前退出)


# ===== 单 key 向后兼容(deepseek 多 key 同理) =====
def test_failover_single_key_backward_compat():
    pool = SourcePool("t", ["only"], qpm_per_source=100, primary_key="only")
    calls = []
    async def do(key):
        calls.append(key)
        return "ok"
    r = _run(acall_with_failover(pool, do, throttle=False, primary_first=True))
    assert r == "ok" and calls == ["only"]  # 1 key = 调一次·零额外开销


def test_failover_round_robin_compat():
    pool = SourcePool("t", ["a", "b"], qpm_per_source=100)
    calls = []
    async def do(key):
        calls.append(key)
        if len(calls) == 1:
            raise RuntimeError("first fail")
        return "ok:" + key
    r = _run(acall_with_failover(pool, do, throttle=False, primary_first=False))
    assert r.startswith("ok:") and len(calls) == 2  # 失败换下一个


def test_failover_empty_pool_uses_fallback_key():
    pool = SourcePool("t", [], qpm_per_source=100)
    async def do(key):
        return "ok:" + key
    r = _run(acall_with_failover(pool, do, fallback_key="fb"))
    assert r == "ok:fb"  # 空池 → 兜底单 key


# ===== deepseek 多 key 轮询(env 加 key 即生效·零代码改) =====
def test_deepseek_keys_rotation():
    from services.llm import deepseek_key_pool as dk
    os.environ["DEEPSEEK_API_KEYS"] = "k1,k2,k3,k1"  # 含重复
    try:
        keys = dk.get_deepseek_api_keys()
        assert keys == ["k1", "k2", "k3"], keys  # 去重保序
        picks = [dk.pick_deepseek_api_key() for _ in range(6)]
        assert set(picks) == {"k1", "k2", "k3"}  # round-robin 覆盖全部
    finally:
        del os.environ["DEEPSEEK_API_KEYS"]


def test_deepseek_failover_multi_key():
    from services.llm import deepseek_key_pool as dk
    os.environ["DEEPSEEK_API_KEYS"] = "k1,k2,k3"
    try:
        calls = []
        async def do(key):
            calls.append(key)
            if len(calls) < 3:
                raise RuntimeError("key fail")
            return "ok:" + key
        r = _run(dk.adeepseek_call_with_failover(do))
        assert r.startswith("ok:") and len(calls) == 3  # 前两 key 失败 → 第三成功
    finally:
        del os.environ["DEEPSEEK_API_KEYS"]


if __name__ == "__main__":
    import traceback
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for name, fn in fns:
        try:
            fn()
            print("  PASS " + name)
            passed += 1
        except Exception:
            print("  FAIL " + name)
            traceback.print_exc()
    print("\n%d/%d passed" % (passed, len(fns)))
    sys.exit(0 if passed == len(fns) else 1)
