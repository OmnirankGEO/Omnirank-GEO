"""真 Redis 臂 —— 复刻生产的脚本调用形,证明恢复路径不会被熔断打死。

🔴 需要一个真 Redis(替身证不了这一格:redis-py 的 ``Script.__call__`` 到底
   经不经过我们的代理、``SCRIPT FLUSH`` 之后到底抛不抛 NoScript,都是**它**的行为,
   不是我能用替身规定的)。

    docker run -d --name omnirank-redis-degrade-test         --label omnirank.redis-degrade-test=true         -p 127.0.0.1:56379:6379 redis:7-alpine

   🔴 端口**只绑 loopback**(``-p 127.0.0.1:56379:6379``,不是 ``-p 56379:6379``):
      后者会把一个无密码 Redis 暴露到局域网。照 progress-bus 测试床的规矩:
      标签 + loopback + 独立 db。下面 ``_require_live_redis`` 会核 host 是不是
      回环 —— 判据不许连一个来路不明的 Redis。

   地址可用 ``REDIS_DEGRADE_TEST_URL`` 覆盖(默认 ``redis://127.0.0.1:56379/1``)。
   连不上时本文件**红**而不是 skip —— skip 与"跑过了且没问题"同形,
   而这一格恰恰是 Codex 判 NO-GO 的那一格,不许用 skip 糊过去。
"""

from __future__ import annotations

import importlib
import os

import pytest
import redis
import redis.exceptions as rexc

URL = os.getenv("REDIS_DEGRADE_TEST_URL", "redis://127.0.0.1:56379/1")
DEAD_URL = os.getenv("REDIS_DEGRADE_DEAD_URL", "redis://127.0.0.1:56999/1")

_LUA = "redis.call('SET', KEYS[1], ARGV[1]); return redis.call('GET', KEYS[1])"


@pytest.fixture
def rc(monkeypatch):
    """干净模块态 + 指向真 Redis。"""
    mod = importlib.import_module("cache.redis_client")
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_redis_client", None, raising=False)
    monkeypatch.setattr(mod, "_redis_available", True, raising=False)
    monkeypatch.setattr(mod, "_redis_retry_after", 0.0, raising=False)
    monkeypatch.setenv("REDIS_URL", URL)
    return mod


def _require_live_redis():
    from urllib.parse import urlparse

    host = urlparse(URL).hostname or ""
    assert host in ("127.0.0.1", "::1", "localhost"), (
        f"测试 Redis 指向 {host} —— 判据只许连回环上的一次性实例,"
        f"不许连来路不明的 Redis(更不许连生产)")
    try:
        redis.Redis.from_url(URL, socket_connect_timeout=2).ping()
    except Exception as exc:                       # noqa: BLE001 —— 要把原因报出来
        pytest.fail(
            f"连不上测试 Redis {URL}:{exc}\n"
            "起一个:docker run -d --name redisdeg-test -p 56379:6379 redis:7-alpine\n"
            "(本条不 skip:skip 与「跑过且没问题」同形,而这正是 NO-GO 的那一格)")


def test_the_production_script_call_shape_survives_a_script_flush(rc):
    """生产调用形:``register_script`` 之后**显式传 client=代理**,再 SCRIPT FLUSH。

    🔴 形状必须逐字复刻,否则测的是另一条路:
       ``register_script`` 经代理拿到的是 raw 的绑定方法 ⇒ Script 绑 raw ⇒
       不传 ``client=`` 的话 NoScript 根本不经过代理,这条判据会**恒绿**。
       本仓 7 处调用全都显式传 ``client=r``
       (``auth/rate_limiter:166`` / ``diagnosis_slots:121,140,169`` /
        ``leader_lock:137,160,177``)—— 那才是生产的形状。
    """
    _require_live_redis()
    proxy = rc.get_redis()
    assert proxy is not None, f"连不上 {URL}"

    # 🔴 活性探针:证明 NoScriptError **真的穿过了代理的 except 分支**。
    #    没有它,这条判据在"根本没走代理"时也全绿 —— 我自己打这一发毒时实测过:
    #    去掉下面那个 client=proxy,19 条照样 passed。那时它证的是"什么都没发生",
    #    与"熔断被正确放行"同形。``_breaker_error_types()`` 只在 ``_wrapped`` 的
    #    except 里被调用,所以它被调到 = 异常确实经过了代理。
    seen: list[str] = []
    _real_types = rc._breaker_error_types

    def _spy():
        import sys as _sys
        exc = _sys.exc_info()[1]
        if exc is not None:
            seen.append(type(exc).__name__)
        return _real_types()

    rc._breaker_error_types = _spy
    try:
        script = proxy.register_script(_LUA)
        assert script(keys=["degrade:probe"], args=["v1"], client=proxy) == "v1"

        # 冷缓存/重启/SCRIPT FLUSH 之后的那一幕:下一次调用必然先撞 NoScript
        proxy.script_flush()
        out = script(keys=["degrade:probe"], args=["v2"], client=proxy)
    finally:
        rc._breaker_error_types = _real_types

    assert "NoScriptError" in seen, (
        f"NoScriptError 没有经过代理(代理 except 分支见到的是 {seen})—— "
        f"这条判据没有测到被修的那条路。生产 7 处调用都显式传 client=代理,"
        f"少传这一个 kwarg,Script 就绑回 raw,整条判据变成恒绿")
    assert out == "v2", "脚本没自恢复 —— 这条判据没测到恢复路径"
    assert rc._redis_client is proxy, (
        "脚本恢复成功,共享 client 却被换掉/作废了 —— 同一性断言,不是 not None:"
        "作废后 get_redis() 会重连出**另一个** client,只断言 not None 会被蒙混过去")
    assert rc._redis_retry_after == 0.0, (
        "正常恢复路径开了冷却 ⇒ 30s 内 get_redis() 返 None ⇒ "
        "global_rate_limiter fail-open,登录限流退化成进程内")
    assert rc.get_redis() is proxy


def test_a_real_unreachable_redis_still_opens_the_breaker(rc, monkeypatch):
    """配对的**必须命中**:真连不上时,熔断照旧。

    没有这条,把熔断整个关掉也能让上面那条绿。
    """
    monkeypatch.setenv("REDIS_URL", DEAD_URL)
    assert rc.get_redis() is None, f"{DEAD_URL} 居然连上了 —— 换一个没人监听的端口"
    assert rc._redis_retry_after > 0.0, "建连失败没进冷却"


def test_a_live_client_that_loses_its_server_breaks_on_the_next_call(rc):
    """连上之后**服务器没了**:下一次操作必须熔断(传输层)。

    把 raw 的连接池换成指向一个没人监听的端口 —— 于是异常是 **redis-py 真抛的**,
    不是我编的。"redis-py 在这种情况下抛哪一类"正是本条要证的东西,不能由我规定。
    """
    _require_live_redis()
    proxy = rc.get_redis()
    assert proxy is not None
    raw = object.__getattribute__(proxy, "_r")
    raw.connection_pool = redis.ConnectionPool.from_url(
        DEAD_URL, socket_connect_timeout=0.5, socket_timeout=0.5)

    with pytest.raises((rexc.ConnectionError, rexc.TimeoutError, OSError)):
        proxy.get("degrade:probe")
    assert rc._redis_client is None, "服务器没了却不熔断 —— 每个请求都要付满超时"
    assert rc._redis_retry_after > 0.0
