"""`progress_bus` 同步 client 的构造参数 —— 短超时 + 关重试,必须有人守。

🔴 [P1-3 · Codex 终审] 这两个参数**一条判据都没有**:Codex 把整个
   `cache/progress_bus.py` 还原到生产底 blob `d188e3a13`(即撤掉本次全部改动)
   ⇒ progress_bus 那 45 条 **全绿**。也就是说这半个修复从上线起就没有守卫。

🔴 只锁**同步快照读**那一个 client。另两个构造点各有各的道理,不许被"统一"掉:
   · `_get_async_redis`(:174)publish 用,`socket_timeout=5`;
   · `_create_subscriber_redis`(:209)长期订阅,**故意** `socket_timeout=None`
     —— 给它套 0.5s 会让每条长订阅半秒一断。
   所以下面有一条**反向臂**专门钉住"订阅 client 不许被短超时化"。
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def bus(monkeypatch):
    mod = importlib.import_module("cache.progress_bus")
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_sync_redis", None, raising=False)
    monkeypatch.setattr(mod, "_sync_redis_retry_at", 0.0, raising=False)
    return mod


def _capture_sync_ctor(bus, monkeypatch):
    """真调 `_get_sync_redis()`,把 `redis.Redis(...)` 的实参截下来。

    🔴 行为锁不是源码锁:截的是**运行时真正传给构造器的那份 kwargs**,
       所以"改了默认值"「换了变量名」「把值算错」都逃不掉。
    """
    seen = {}

    class _FakeClient:
        def ping(self):
            return True

    class _Lib:
        @staticmethod
        def Redis(**kwargs):
            seen.update(kwargs)
            return _FakeClient()

    monkeypatch.setitem(__import__("sys").modules, "redis", _Lib)
    client = bus._get_sync_redis()
    assert client is not None, "同步 client 没建出来 —— 这条判据没驱动到构造那一行"
    assert seen, "构造器一次都没被调到"
    return seen


def test_the_sync_client_uses_the_short_timeout_on_both_axes(bus, monkeypatch):
    """连接超时与读超时都必须等于 `_SYNC_OP_TIMEOUT_S`。

    两个轴都要钉:只钉 connect 的话,"连上之后 Redis 死了"那一半仍然付满读超时,
    而生产实证的 8~16s 正是这一半(门八跑台:`/api/auth/me` 12.86s)。
    """
    seen = _capture_sync_ctor(bus, monkeypatch)
    want = bus._SYNC_OP_TIMEOUT_S
    assert seen.get("socket_connect_timeout") == want, seen
    assert seen.get("socket_timeout") == want, seen
    assert 0.1 <= float(want) <= 2.0, (
        f"_SYNC_OP_TIMEOUT_S={want} 不在合理区间 —— 压到 0.1s 以下会把抖动当死")


def test_the_sync_client_does_not_retry_on_timeout(bus, monkeypatch):
    """降级路上关重试:重试把"判死"的代价乘上倍数,而这条路的价值就是尽快判死。"""
    seen = _capture_sync_ctor(bus, monkeypatch)
    assert seen.get("retry_on_timeout") is False, (
        f"降级路上还开着重试:{seen.get('retry_on_timeout')!r} —— "
        f"注意 None/缺省也不算关(redis-py 默认 False,但默认会随版本变,"
        f"这里要的是**显式**关掉)")


def test_both_modules_read_the_same_timeout_env_var(bus):
    """两个模块必须吃**同一个**环境变量 —— 各写一个数,调参时必然漏掉一处。

    这条钉的是 `progress_bus` 抬头那句注释("两处一个值")的**行为**,
    不是那句注释本身:注释传不出去,门才传得出去。
    """
    import os

    import cache.redis_client as rc

    assert bus._SYNC_OP_TIMEOUT_S == rc._REDIS_OP_TIMEOUT_S, (
        f"两个模块的降级超时不一致:{bus._SYNC_OP_TIMEOUT_S} vs {rc._REDIS_OP_TIMEOUT_S}")

    os.environ["REDIS_OP_TIMEOUT_S"] = "1.25"
    try:
        importlib.reload(bus)
        importlib.reload(rc)
        assert bus._SYNC_OP_TIMEOUT_S == 1.25, "progress_bus 没读 REDIS_OP_TIMEOUT_S"
        assert rc._REDIS_OP_TIMEOUT_S == 1.25, "redis_client 没读 REDIS_OP_TIMEOUT_S"
    finally:
        os.environ.pop("REDIS_OP_TIMEOUT_S", None)
        importlib.reload(bus)
        importlib.reload(rc)


def test_the_long_lived_subscriber_is_not_short_timeouted(bus):
    """反向臂:长期订阅 client **不许**被短超时化。

    没有这条,"把三个构造点统一成短超时"会全绿,而它会让每条长订阅半秒一断 ——
    那是比原缺陷更坏的回归,且症状(进度页断流)与 Redis 抖动难以区分。
    """
    import ast
    import inspect

    src = inspect.getsource(bus._create_subscriber_redis)
    call = next(n for n in ast.walk(ast.parse(src.lstrip()))
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "Redis")
    kw = {k.arg: k.value for k in call.keywords}
    st = kw.get("socket_timeout")
    assert isinstance(st, ast.Constant) and st.value is None, (
        f"订阅 client 的 socket_timeout 被改成了 {ast.dump(st) if st else '缺失'} —— "
        f"长期订阅必须无读超时")
