"""Redis 不可达时的降级代价 —— 熔断必须真的熔断,不是"超时短了一点"。

生产实证(门八跑台 · csweep 隔离套):Redis 不可达时**每个已鉴权请求** 8~16 秒
(`/api/auth/me` 12.86s,`/status` 24.20s,而 `/docs` 0.006s)。
机制:`cache/redis_client.get_redis()` 有 client 就原样返回,而 `redis_get` 等
在操作失败时**只 log**,既不作废 client 也不进冷却 ⇒ 那条冷却分支永远到不了,
每次操作都付满 socket_timeout(× retry_on_timeout 重试)。

🔴 分母是**所有已鉴权端点**,不是进度页:限流器 `auth/global_rate_limiter`
   拿 `get_redis()` 的 client **自己**跑 eval/pipeline,它在每个请求上;
   只包住本模块那几个函数,主腿一次都盖不到。所以判据打的是**客户端本身**。
"""

from __future__ import annotations

import importlib
import time

import pytest
import redis.exceptions as rexc


@pytest.fixture
def rc(monkeypatch):
    """每条判据一份干净模块态 —— 全局单例会把上一条的冷却带进下一条。"""
    mod = importlib.import_module("cache.redis_client")
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_redis_client", None, raising=False)
    monkeypatch.setattr(mod, "_redis_available", True, raising=False)
    monkeypatch.setattr(mod, "_redis_retry_after", 0.0, raising=False)
    return mod


class _Boom(Exception):
    """非 Redis 的普通异常 —— 用来证明它**不**该熔断。"""


class _FakeRedis:
    """替身只负责"成功/失败",不替被测代码做任何判断。

    🔴 [P1-1] ``exc`` 可配:熔断与否取决于**异常类别**,替身不能替被测代码
       决定这件事,只能如实抛出调用方要它抛的那一类。默认用真传输错误
       ``redis.exceptions.ConnectionError`` —— 上一版默认 ``_Boom``(普通异常),
       等于把"所有异常都熔断"写进了判据,Codex 因此判它没有判别力。
    """

    def __init__(self, fail: bool = False, exc=None):
        self.fail = fail
        self.exc = exc or rexc.ConnectionError
        self.calls = 0

    def ping(self):
        self.calls += 1
        if self.fail:
            raise self.exc("redis down")
        return True

    def get(self, key):
        self.calls += 1
        if self.fail:
            raise self.exc("redis down")
        return "v"

    def pipeline(self):
        return _FakePipe(self)


class _FakePipe:
    def __init__(self, parent):
        self.parent = parent

    def execute(self):
        self.parent.calls += 1
        if self.parent.fail:
            raise self.parent.exc("redis down")
        return [1]


def _install(rc, monkeypatch, fake):
    """让 get_redis() 建出替身 —— 不碰真 Redis。"""
    class _Lib:
        exceptions = rexc          # 🔴 替身只替"建连工厂",不替异常分类学
        class Redis:
            @staticmethod
            def from_url(*a, **kw):
                fake.ctor_kwargs = kw
                return fake
    monkeypatch.setitem(__import__("sys").modules, "redis", _Lib)
    return fake


def test_a_failed_operation_invalidates_the_client_and_opens_the_cooldown(rc, monkeypatch):
    """操作失败 ⇒ 作废 client + 进冷却。这是整条修复的承重点。"""
    fake = _install(rc, monkeypatch, _FakeRedis())
    client = rc.get_redis()
    assert client is not None, "第一次应当连上,否则下面测的不是「连上之后才死」"

    fake.fail = True
    with pytest.raises(Exception):
        client.get("k")          # 调用方拿到的异常**照旧**,语义不变

    assert rc._redis_client is None, "操作失败后 client 没被作废 —— 冷却分支永远到不了"
    assert rc._redis_retry_after > time.time(), "没有进冷却"


def test_inside_the_cooldown_redis_is_not_touched_at_all(rc, monkeypatch):
    """冷却窗内 `get_redis()` 直接返 None,且**一次都不碰 Redis**。

    🔴 这条是"变快到底是熔断还是只是超时缩短"的判别臂:
       只看耗时的话,把 timeout 压到 1ms 也会"变快",但每次仍在敲一个死掉的
       Redis;真熔断的标志是**调用计数不再增长**。
    """
    fake = _install(rc, monkeypatch, _FakeRedis())
    client = rc.get_redis()
    fake.fail = True
    with pytest.raises(Exception):
        client.get("k")

    calls_after_break = fake.calls
    for _ in range(5):
        assert rc.get_redis() is None
    assert fake.calls == calls_after_break, (
        f"冷却窗内仍在碰 Redis:{calls_after_break} → {fake.calls}")


def test_a_pipeline_failure_also_opens_the_cooldown(rc, monkeypatch):
    """限流器走的是 `pipeline()`/`eval` —— 它的失败也必须熔断。

    只包住本模块的 redis_get/redis_set,这一路一次都盖不到,
    而它在**每个已鉴权请求**上(这正是主腿)。
    """
    fake = _install(rc, monkeypatch, _FakeRedis())
    client = rc.get_redis()
    fake.fail = True
    pipe = client.pipeline()
    with pytest.raises(Exception):
        pipe.execute()
    assert rc._redis_client is None, "pipeline 失败没有熔断 —— 主腿仍然每次付超时"
    assert rc._redis_retry_after > time.time()


def test_a_successful_operation_never_opens_the_cooldown(rc, monkeypatch):
    """必须不命中的那一半:成功的操作不许把连接熔掉。

    没有这条,「永远熔断」也能让上面三条全绿。
    """
    fake = _install(rc, monkeypatch, _FakeRedis())
    client = rc.get_redis()
    assert client.get("k") == "v"
    assert rc._redis_client is not None
    assert rc._redis_retry_after == 0.0


def test_the_degraded_path_uses_short_timeouts_and_no_retry(rc, monkeypatch):
    """降级路上的代价上限:短超时 + 关重试。

    跑台实测正常 GET p99 = 0.21ms,0.5s ≈ 其 2400 倍 —— 抖动误判的余量足够,
    又能让"死了"在半秒内判出来。**不许压到 100ms 以下**(会把抖动当死)。
    """
    fake = _install(rc, monkeypatch, _FakeRedis())
    rc.get_redis()
    kw = getattr(fake, "ctor_kwargs", {})
    assert kw.get("retry_on_timeout") is False, (
        f"降级路上还开着重试 —— 判死的代价被乘上倍数:{kw}")
    for key in ("socket_connect_timeout", "socket_timeout"):
        v = kw.get(key)
        assert v is not None and 0.1 <= float(v) <= 2.0, (
            f"{key}={v} 不在合理区间[0.1,2.0]s")


# ══════════════════════════════════════════════════════════════════════════
# [P1-1 · Codex 终审] 熔断只许对「连不上 / 传不动」发生
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("exc_cls,should_break", [
    # 传输层:服务器没答话 ⇒ 连接确实不能用了 ⇒ 熔断
    (rexc.ConnectionError, True),
    (rexc.TimeoutError, True),
    (rexc.BusyLoadingError, True),        # ConnectionError 子类:服务器还没起来
    (ConnectionResetError, True),         # socket 层(OSError 子类)
    # 应答层:服务器**答了话**,只是这条命令不行 ⇒ 连接是活的 ⇒ 不许熔断
    (rexc.NoScriptError, False),          # 🔴 就是这一个把生产打成 fail-open 的
    (rexc.ResponseError, False),
    (rexc.DataError, False),
    (rexc.WatchError, False),
    (_Boom, False),                       # 非 Redis 的普通异常
])
def test_only_transport_failures_open_the_breaker(rc, monkeypatch, exc_cls, should_break):
    """异常类别矩阵 —— 这是本条修复的**判别力**所在。

    🔴 上一版只抛一个通用 ``_Boom`` 并断言"熔断了",等于把**「所有异常都熔断」
       写进了判据**。而 redis-py 把 ``NoScriptError`` 当**正常恢复流程**用:
       ``Script.__call__`` 先 ``evalsha`` → 撞 NoScript → ``script_load`` →
       再 ``evalsha`` 成功。冷缓存首调 / Redis 重启 / ``SCRIPT FLUSH`` 之后必走。
       于是脚本明明恢复了,client 却已作废 + 进 30s 冷却 ⇒ ``get_redis()`` 返 None
       ⇒ ``auth/global_rate_limiter`` fail-open(登录限流退化成进程内)、
       ``cache/diagnosis_slots`` 与 ``cache/leader_lock`` 整片降级。

    🔴 两个方向都必须钉:只钉"该熔断的熔断了",把 breaker 写成恒真也全绿。
    """
    fake = _install(rc, monkeypatch, _FakeRedis(exc=exc_cls))
    client = rc.get_redis()
    assert client is not None

    fake.fail = True
    with pytest.raises(exc_cls):
        client.get("k")              # 异常一律**原样抛给调用方**,语义不变

    if should_break:
        assert rc._redis_client is None, f"{exc_cls.__name__} 是传输失败,却没熔断"
        assert rc._redis_retry_after > time.time()
    else:
        assert rc._redis_client is not None, (
            f"{exc_cls.__name__} 是服务器的**应答**(连接活着),却把 client 熔了 —— "
            f"共享 client 一作废,所有拿 get_redis() 的模块一起 fail-open")
        assert rc._redis_retry_after == 0.0, f"{exc_cls.__name__} 不该开冷却"


def test_the_script_recovery_round_trip_survives_the_breaker(rc, monkeypatch):
    """把 redis-py 那一遭**照原样走一遍**:NoScript → script_load → 成功。

    不只断言"NoScriptError 不熔断",而是断言**整个恢复往返之后**
    client 还活着、冷却没开、下一次操作仍然真的走 Redis。
    这才是生产上那一幕(冷缓存首调 / SCRIPT FLUSH 后)的形状。
    """
    class _ScriptRedis(_FakeRedis):
        def __init__(self):
            super().__init__()
            self.loaded = False
            self.evalsha_calls = 0

        def evalsha(self, sha, numkeys, *args):
            self.evalsha_calls += 1
            self.calls += 1
            if not self.loaded:
                raise rexc.NoScriptError("NOSCRIPT No matching script")
            return 1

        def script_load(self, script):
            self.calls += 1
            self.loaded = True
            return "deadbeef"

    fake = _install(rc, monkeypatch, _ScriptRedis())
    client = rc.get_redis()

    # redis-py 5.2.1 Script.__call__ 的逐字形状
    try:
        out = client.evalsha("deadbeef", 0)
    except rexc.NoScriptError:
        sha = client.script_load("return 1")
        out = client.evalsha(sha, 0)

    assert out == 1, "脚本没恢复成功 —— 那这条判据测的不是恢复路径"
    assert fake.evalsha_calls == 2, "没有真的走「先失败再重试」那一遭"
    assert rc._redis_client is not None, (
        "脚本恢复成功了,client 却被作废 —— 正常恢复路径被当成断连")
    assert rc._redis_retry_after == 0.0, "恢复成功却开了冷却"
    assert rc.get_redis() is not None, (
        "get_redis() 返 None ⇒ global_rate_limiter fail-open,登录限流退化成进程内")


def test_a_transport_failure_after_a_script_recovery_still_breaks(rc, monkeypatch):
    """必须不命中的那一半:放行应答层异常之后,传输层异常仍要熔断。

    没有这条,把 breaker 改成"永不熔断"也能让上面那条绿。
    """
    fake = _install(rc, monkeypatch, _FakeRedis(exc=rexc.NoScriptError))
    client = rc.get_redis()
    fake.fail = True
    with pytest.raises(rexc.NoScriptError):
        client.get("k")
    assert rc._redis_client is not None       # 应答层:放行

    fake.exc = rexc.ConnectionError           # 同一个 client,换成真断连
    with pytest.raises(rexc.ConnectionError):
        client.get("k")
    assert rc._redis_client is None, "应答层放行之后,传输层也不熔断了 —— 熔断被关死"
    assert rc._redis_retry_after > time.time()
