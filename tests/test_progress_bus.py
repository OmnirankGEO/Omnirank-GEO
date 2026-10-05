"""
B1 进度总线单测(WORKERS=4 · SPEC §6/§6b)

- 纯逻辑(circuit breaker / key 助手)任何环境可跑。
- Redis 语义(seq 单调 / 原子快照+发布 / 跨"worker"扇出 / 快照读)用
  fakeredis+lupa 隔离执行；依赖或 Lua 缺失必须明确失败，禁止 skip 假绿。
不依赖 pytest-asyncio(async 部分用 asyncio.run 在同步 test 里驱动)。
"""
import asyncio
import ast
from pathlib import Path
import time

import pytest

from cache import progress_bus as pb


_ROOT = Path(__file__).resolve().parents[1]


# ---------------- 纯逻辑:circuit breaker ----------------

def test_breaker_opens_after_threshold_and_recovers():
    b = pb._Breaker(fail_threshold=3, open_seconds=60.0)
    t = [1000.0]
    assert b.is_open(t[0]) is False
    b.record_fail(t[0]); b.record_fail(t[0])
    assert b.is_open(t[0]) is False           # 未达阈值
    b.record_fail(t[0])                        # 第 3 次 → open
    assert b.is_open(t[0]) is True
    assert b.is_open(t[0] + 59.0) is True      # 冷却期内仍 open
    assert b.is_open(t[0] + 61.0) is False     # 冷却结束 → half-open 放行探测
    b.record_ok()                              # 探测成功 → 连续计数清零
    assert b.is_open(t[0] + 62.0) is False


def test_breaker_success_resets_consecutive():
    b = pb._Breaker(fail_threshold=3, open_seconds=60.0)
    b.record_fail(1.0); b.record_fail(1.0)
    b.record_ok()                              # 清零
    b.record_fail(1.0); b.record_fail(1.0)
    assert b.is_open(1.0) is False             # 只累计到 2,未 open


def test_key_helpers_and_ttl():
    assert pb._snap_key("s1") == "task_status:s1"   # 沿用旧键(轮询兼容)
    assert pb._seq_key("s1") == "progress:seq:s1"
    assert pb._chan_key("s1") == "progress:ch:s1"
    assert pb.SNAP_TTL >= 1800                       # 修 §3.3:>= 诊断 1800s 上限


def test_publish_and_subscriber_clients_use_isolated_timeout_policies(monkeypatch):
    """长期订阅必须有独立、无读超时的 client；发布 client 仍须有限超时。"""
    import redis.asyncio as aioredis

    created = []

    class RecordingRedis:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            created.append(self)

        async def ping(self):
            return True

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(aioredis, "Redis", RecordingRedis)
    pb._reset_for_test()

    async def _run():
        publisher = await pb._get_async_redis()
        subscriber = await pb._create_subscriber_redis()
        await subscriber.aclose()
        return publisher, subscriber

    publisher, subscriber = asyncio.run(_run())
    assert publisher is not subscriber
    assert publisher.kwargs["db"] == subscriber.kwargs["db"] == 1
    assert publisher.kwargs["socket_timeout"] is not None
    assert publisher.kwargs["socket_timeout"] > 0
    assert subscriber.kwargs["socket_connect_timeout"] == 2
    assert subscriber.kwargs["socket_timeout"] is None
    assert subscriber.kwargs["socket_keepalive"] is True
    assert subscriber.kwargs["health_check_interval"] > 0


def test_subscriber_failure_does_not_reset_publish_client(monkeypatch):
    """反复订阅失败不得关闭/熔断发布端，且发布操作仍可成功。"""
    stop = asyncio.Event()
    subscriber_closes = []
    retries = []

    class FailingPubSub:
        async def psubscribe(self, _pattern):
            raise ConnectionError("subscriber-only failure")

        async def aclose(self):
            subscriber_closes.append("pubsub")

    class SubscriberRedis:
        def pubsub(self):
            return FailingPubSub()

        async def aclose(self):
            subscriber_closes.append("client")

    class PublishRedis:
        def __init__(self):
            self.closed = False
            self.seq = 0

        async def incr(self, _key):
            assert self.closed is False
            self.seq += 1
            return self.seq

        def register_script(self, _script):
            async def _execute(**_kwargs):
                assert self.closed is False
                return 1

            return _execute

        async def aclose(self):
            self.closed = True

    publish_client = PublishRedis()
    pb._reset_for_test()
    pb._async_redis = publish_client
    breaker_before = (pb._breaker._consecutive, pb._breaker._opened_at)

    async def _new_subscriber():
        return SubscriberRedis()

    async def _retry_then_stop(_stop_event, delay):
        retries.append(delay)
        if len(retries) == 3:
            stop.set()
            return True
        return False

    monkeypatch.setattr(pb, "_create_subscriber_redis", _new_subscriber, raising=False)
    monkeypatch.setattr(pb, "_wait_for_retry_or_stop", _retry_then_stop)

    async def _run():
        await pb.run_subscriber(lambda _sid, _msg: None, stop_event=stop)
        breaker_after_subscriber = (pb._breaker._consecutive, pb._breaker._opened_at)
        seq = await pb.publish("publisher-still-healthy", {"progress": 1})
        return breaker_after_subscriber, seq

    breaker_after_subscriber, seq = asyncio.run(_run())

    assert pb._async_redis is publish_client
    assert pb._async_redis_retry_at == 0.0
    assert publish_client.closed is False
    assert breaker_after_subscriber == breaker_before
    assert seq == 1
    assert retries == [1.0, 2.0, 4.0]
    assert subscriber_closes == ["pubsub", "client"] * 3


def test_subscriber_accept_then_drop_flapping_keeps_exponential_backoff(monkeypatch):
    """连接刚订阅就掉线不能把退避洗回 1s，否则 Redis 抖动会形成日志/重连风暴。"""
    stop = asyncio.Event()
    delays = []

    class FlappingPubSub:
        def __init__(self):
            self.pattern = None
            self.acked = False

        async def psubscribe(self, _pattern):
            self.pattern = _pattern

        async def get_message(self, **_kwargs):
            if not self.acked:
                self.acked = True
                return {"type": "psubscribe", "channel": self.pattern, "data": 1}
            raise ConnectionError("accept-then-drop")

        async def aclose(self):
            return None

    class FlappingClient:
        def pubsub(self):
            return FlappingPubSub()

        async def aclose(self):
            return None

    async def _new_subscriber():
        return FlappingClient()

    async def _record_retry(_stop_event, delay):
        delays.append(delay)
        if len(delays) == 7:
            stop.set()
            return True
        return False

    monkeypatch.setattr(pb, "_create_subscriber_redis", _new_subscriber)
    monkeypatch.setattr(pb, "_wait_for_retry_or_stop", _record_retry)
    asyncio.run(pb.run_subscriber(lambda _sid, _msg: None, stop_event=stop))
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_close_async_resource_prefers_aclose():
    calls = []

    class Resource:
        async def aclose(self):
            calls.append("aclose")

        def close(self):
            calls.append("deprecated-close")

    asyncio.run(pb._close_async_resource(Resource()))
    assert calls == ["aclose"]


def test_blackhole_psubscribe_times_out_cleans_up_and_preserves_publisher(monkeypatch):
    """TCP 接通但订阅握手不回包时必须超时退避，且不得污染发布 client。"""
    stop = asyncio.Event()
    retries = []
    state = {"pubsub_closed": False, "client_closed": False}

    class HangingPubSub:
        async def psubscribe(self, _pattern):
            await asyncio.Event().wait()

        async def aclose(self):
            state["pubsub_closed"] = True

    class HangingClient:
        def pubsub(self):
            return HangingPubSub()

        async def aclose(self):
            state["client_closed"] = True

    publish_client = object()
    pb._reset_for_test()
    pb._async_redis = publish_client

    async def _new_subscriber():
        return HangingClient()

    async def _stop_after_retry(_stop_event, delay):
        retries.append(delay)
        stop.set()
        return True

    monkeypatch.setattr(pb, "_SUBSCRIBER_HANDSHAKE_TIMEOUT", 0.01)
    monkeypatch.setattr(pb, "_create_subscriber_redis", _new_subscriber)
    monkeypatch.setattr(pb, "_wait_for_retry_or_stop", _stop_after_retry)
    asyncio.run(pb.run_subscriber(lambda _sid, _msg: None, stop_event=stop))
    assert retries == [pb._SUBSCRIBER_RECONNECT_INITIAL]
    assert state == {"pubsub_closed": True, "client_closed": True}
    assert pb._async_redis is publish_client
    assert pb._async_redis_retry_at == 0.0


def test_blackhole_ping_times_out_and_closes_subscriber_client(monkeypatch):
    """专用 client 的 readiness ping 也必须受握手上限约束。"""
    import redis.asyncio as aioredis

    state = {"closed": False}

    class HangingRedis:
        def __init__(self, **_kwargs):
            pass

        async def ping(self):
            await asyncio.Event().wait()

        async def aclose(self):
            state["closed"] = True

    monkeypatch.setattr(aioredis, "Redis", HangingRedis)
    monkeypatch.setattr(pb, "_SUBSCRIBER_HANDSHAKE_TIMEOUT", 0.01)

    async def _run():
        with pytest.raises(asyncio.TimeoutError):
            await pb._create_subscriber_redis()

    asyncio.run(_run())
    assert state["closed"] is True


def test_missing_psubscribe_ack_times_out_and_cleans_up(monkeypatch):
    """命令写出但服务端不回订阅确认时不能误报 ready。"""
    stop = asyncio.Event()
    state = {"pubsub_closed": False, "client_closed": False}

    class MissingAckPubSub:
        async def psubscribe(self, _pattern):
            return None

        async def get_message(self, **_kwargs):
            await asyncio.sleep(1)
            return None

        async def aclose(self):
            state["pubsub_closed"] = True

    class MissingAckClient:
        def pubsub(self):
            return MissingAckPubSub()

        async def aclose(self):
            state["client_closed"] = True

    async def _new_subscriber():
        return MissingAckClient()

    async def _stop_after_retry(_stop_event, _delay):
        stop.set()
        return True

    monkeypatch.setattr(pb, "_SUBSCRIBER_HANDSHAKE_TIMEOUT", 0.01)
    monkeypatch.setattr(pb, "_create_subscriber_redis", _new_subscriber)
    monkeypatch.setattr(pb, "_wait_for_retry_or_stop", _stop_after_retry)
    asyncio.run(pb.run_subscriber(lambda _sid, _msg: None, stop_event=stop))
    assert state == {"pubsub_closed": True, "client_closed": True}


def test_established_half_open_missing_pong_reconnects_without_poisoning_publisher(monkeypatch):
    """已确认订阅后的静默丢包必须由 nonce PING/PONG 截止时间打断。"""
    stop = asyncio.Event()
    clock = [0.0]
    pings = []
    retries = []
    state = {"pubsub_closed": False, "client_closed": False}
    publisher = object()

    class HalfOpenPubSub:
        def __init__(self):
            self.pattern = None
            self.acked = False

        async def psubscribe(self, pattern):
            self.pattern = pattern

        async def get_message(self, **_kwargs):
            if not self.acked:
                self.acked = True
                return {"type": "psubscribe", "channel": self.pattern, "data": 1}
            clock[0] += 1.0
            await asyncio.sleep(0)
            return None

        async def ping(self, nonce):
            pings.append(nonce)  # 写入成功，但模拟 firewall DROP：永远没有对应 PONG。

        async def aclose(self):
            state["pubsub_closed"] = True

    class HalfOpenClient:
        def pubsub(self):
            return HalfOpenPubSub()

        async def aclose(self):
            state["client_closed"] = True

    pb._reset_for_test()
    pb._async_redis = publisher

    async def _new_subscriber():
        return HalfOpenClient()

    async def _stop_after_retry(_stop_event, delay):
        retries.append(delay)
        stop.set()
        return True

    monkeypatch.setattr(pb.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(pb, "_SUBSCRIBER_HEARTBEAT_INTERVAL", 2.0)
    monkeypatch.setattr(pb, "_SUBSCRIBER_HEARTBEAT_TIMEOUT", 2.0)
    monkeypatch.setattr(pb, "_create_subscriber_redis", _new_subscriber)
    monkeypatch.setattr(pb, "_wait_for_retry_or_stop", _stop_after_retry)
    asyncio.run(pb.run_subscriber(lambda _sid, _msg: None, stop_event=stop))

    assert pings and pings[0].startswith("progress-bus:")
    assert retries == [pb._SUBSCRIBER_RECONNECT_INITIAL]
    assert state == {"pubsub_closed": True, "client_closed": True}
    assert pb._async_redis is publisher
    assert pb._async_redis_retry_at == 0.0


class _FakeEventApp:
    def __init__(self):
        self.handlers = {}

    def on_event(self, event_name):
        def _register(function):
            self.handlers[event_name] = function
            return function

        return _register


def _load_progress_lifecycle_functions():
    """保留并执行 FastAPI decorators，避免 import server 触发 DB 初始化。"""
    source = (_ROOT / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {
        "_canonical_process_role",
        "_is_progress_bus_web_role",
        "_start_progress_bus_subscriber",
        "_stop_progress_bus_subscriber",
    }
    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            nodes.append(node)
    assert {node.name for node in nodes} == wanted
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    app = _FakeEventApp()
    namespace = {"app": app}
    exec(compile(module, str(_ROOT / "server.py"), "exec"), namespace)
    assert app.handlers == {
        "startup": namespace["_start_progress_bus_subscriber"],
        "shutdown": namespace["_stop_progress_bus_subscriber"],
    }
    return namespace, app.handlers


def test_progress_subscriber_lifecycle_hooks_are_registered():
    namespace, handlers = _load_progress_lifecycle_functions()
    assert handlers["startup"] is namespace["_start_progress_bus_subscriber"]
    assert handlers["shutdown"] is namespace["_stop_progress_bus_subscriber"]


@pytest.mark.parametrize("role", ["", "web", "cron", "prestart"])
def test_process_role_accepts_only_canonical_values(role):
    namespace, _ = _load_progress_lifecycle_functions()
    assert namespace["_canonical_process_role"](role) == role


@pytest.mark.parametrize("role", ["backup", "WEB", " cron ", "typo"])
def test_process_role_rejects_unknown_or_noncanonical_values(role):
    namespace, _ = _load_progress_lifecycle_functions()
    with pytest.raises(RuntimeError, match="invalid ROLE"):
        namespace["_canonical_process_role"](role)


@pytest.mark.parametrize(
    ("role", "expected_starts"),
    [("web", 1), ("", 1), ("cron", 0), ("prestart", 0)],
)
def test_progress_subscriber_startup_role_matrix(role, expected_starts):
    """ROLE 未设置兼容 web；cron/prestart 必须没有订阅任务。"""
    namespace, handlers = _load_progress_lifecycle_functions()
    started = []

    class FakeAsyncio:
        @staticmethod
        def create_task(coro):
            started.append(coro)
            return FakeTask()

    class FakeTask:
        def done(self):
            return False

    class FakeBus:
        @staticmethod
        def run_subscriber(dispatch):
            return ("subscriber", dispatch)

    class FakeManager:
        dispatch = object()

    messages = []

    class FakeLogger:
        @staticmethod
        def info(message):
            messages.append(message)

    namespace.update(
        _SCHED_ROLE=role,
        _progress_bus_subscriber_task=None,
        asyncio=FakeAsyncio,
        _progress_bus=FakeBus,
        manager=FakeManager,
        logger=FakeLogger,
    )
    asyncio.run(handlers["startup"]())
    assert len(started) == expected_starts
    if expected_starts:
        assert any("web 启动" in message for message in messages)
    else:
        assert any("非 web 跳过" in message for message in messages)


def test_progress_subscriber_startup_is_idempotent_per_worker():
    """重复触发 startup hook 不能让同一 worker 同时持有两条 pattern subscription。"""
    namespace, handlers = _load_progress_lifecycle_functions()
    started = []
    messages = []

    class FakeTask:
        def done(self):
            return False

    class FakeAsyncio:
        @staticmethod
        def create_task(_coro):
            task = FakeTask()
            started.append(task)
            return task

    class FakeBus:
        @staticmethod
        def run_subscriber(dispatch):
            return ("subscriber", dispatch)

    class FakeManager:
        dispatch = object()

    class FakeLogger:
        @staticmethod
        def info(message):
            messages.append(message)

    namespace.update(
        _SCHED_ROLE="web",
        _progress_bus_subscriber_task=None,
        asyncio=FakeAsyncio,
        _progress_bus=FakeBus,
        manager=FakeManager,
        logger=FakeLogger,
    )
    asyncio.run(handlers["startup"]())
    asyncio.run(handlers["startup"]())
    assert len(started) == 1
    assert any("跳过重复启动" in message for message in messages)


@pytest.mark.parametrize("outcome", ["cancelled", "success", "failed"])
def test_progress_subscriber_shutdown_clears_and_awaits_task(outcome):
    namespace, handlers = _load_progress_lifecycle_functions()
    messages = []

    class FakeTask:
        def __init__(self):
            self.cancel_calls = 0

        def cancel(self):
            self.cancel_calls += 1

        def __await__(self):
            async def _resolve():
                if outcome == "cancelled":
                    raise asyncio.CancelledError
                if outcome == "failed":
                    raise RuntimeError("subscriber crashed")
                return None

            return _resolve().__await__()

    class FakeAsyncio:
        CancelledError = asyncio.CancelledError

    class FakeLogger:
        @staticmethod
        def info(message):
            messages.append(("info", message))

        @staticmethod
        def warning(message):
            messages.append(("warning", message))

    task = FakeTask()
    namespace.update(
        _progress_bus_subscriber_task=task,
        asyncio=FakeAsyncio,
        logger=FakeLogger,
    )
    asyncio.run(handlers["shutdown"]())

    assert namespace["_progress_bus_subscriber_task"] is None
    assert task.cancel_calls == 1
    assert any(level == "info" and "已关闭" in message for level, message in messages)
    if outcome == "failed":
        assert any(level == "warning" and "subscriber crashed" in message for level, message in messages)
    else:
        assert not any(level == "warning" for level, _ in messages)


def test_progress_subscriber_shutdown_without_task_is_noop():
    namespace, handlers = _load_progress_lifecycle_functions()

    class FakeAsyncio:
        CancelledError = asyncio.CancelledError

    namespace.update(
        _progress_bus_subscriber_task=None,
        asyncio=FakeAsyncio,
        logger=object(),
    )
    asyncio.run(handlers["shutdown"]())
    assert namespace["_progress_bus_subscriber_task"] is None


# ---------------- Redis 语义:fakeredis 打桩 ----------------

def _fakeredis_pair():
    """返回共享同一隔离 FakeServer 的 async/sync client；依赖缺失直接失败。"""
    import fakeredis
    import fakeredis.aioredis as fake_async
    server = fakeredis.FakeServer()
    aclient = fake_async.FakeRedis(server=server, decode_responses=True)
    sclient = fakeredis.FakeStrictRedis(server=server, decode_responses=True)
    aclient._test_fake_server = server
    return aclient, sclient


def _install_fakeredis(monkeypatch):
    pair = _fakeredis_pair()
    aclient, sclient = pair
    server = aclient._test_fake_server
    # [seq 守卫] publish/set_snapshot_sync 现走守卫 Lua → 需 fakeredis Lua(lupa);缺失即失败。
    try:
        sclient.register_script("return 1")(keys=[], args=[])
    except Exception as e:
        pytest.fail(f"fakeredis Lua 不可用({e}) · 需安装 lupa，禁止 skip 假绿")
    pb._reset_for_test()

    async def _get_async():
        return aclient

    # 每次订阅重连都创建独立 fake client，并追踪 psubscribe ready/资源关闭。
    # 这既保留 FakeServer 的隔离语义，也防测试把发布 client 误当订阅 client 共用。
    import fakeredis.aioredis as fake_async
    subscriber_states = []

    async def _create_subscriber():
        inner = fake_async.FakeRedis(server=server, decode_responses=True)
        state = {
            "ready": asyncio.Event(),
            "client_closed": False,
            "pubsub_closed": False,
            "heartbeat_pings": 0,
        }
        subscriber_states.append(state)

        class TrackedPubSub:
            def __init__(self):
                self.inner = inner.pubsub()

            async def psubscribe(self, pattern):
                await self.inner.psubscribe(pattern)

            async def get_message(self, **kwargs):
                message = await self.inner.get_message(**kwargs)
                if message and message.get("type") == "psubscribe":
                    state["ready"].set()
                return message

            async def ping(self, nonce):
                state["heartbeat_pings"] += 1
                return await self.inner.ping(nonce)

            async def aclose(self):
                state["pubsub_closed"] = True
                await self.inner.aclose()

        class TrackedSubscriber:
            def pubsub(self):
                return TrackedPubSub()

            async def aclose(self):
                state["client_closed"] = True
                await inner.aclose()

        return TrackedSubscriber()

    def _get_sync():
        return sclient

    monkeypatch.setattr(pb, "_get_async_redis", _get_async)
    monkeypatch.setattr(pb, "_create_subscriber_redis", _create_subscriber)
    monkeypatch.setattr(pb, "_get_sync_redis", _get_sync)
    aclient._test_fake_server = server
    aclient._test_subscriber_states = subscriber_states
    return aclient, sclient


def test_publish_assigns_monotonic_seq_and_writes_snapshot(monkeypatch):
    _install_fakeredis(monkeypatch)

    async def _run():
        s1 = await pb.publish("sid1", {"stage": "collecting", "progress": 10, "message": "一"})
        s2 = await pb.publish("sid1", {"stage": "scoring", "progress": 50, "message": "二"})
        s3 = await pb.publish("sid1", {"stage": "done", "progress": 100, "message": "三", "done": True}, terminal=True)
        return s1, s2, s3

    s1, s2, s3 = asyncio.run(_run())
    assert (s1, s2, s3) == (1, 2, 3)                 # INCR 单调
    snap = pb.get_snapshot_sync("sid1")               # 同步快照读(轮询路径)
    assert snap is not None
    assert snap["seq"] == 3                            # 快照=最新态
    assert snap["progress"] == 100                     # 数字不被 cjson 强转(仍是 int 100)
    assert snap["message"] == "三"                     # 中文不乱码
    assert snap["terminal"] is True                    # HC3:终态带 terminal 标记


def test_publish_isolated_seq_per_session(monkeypatch):
    _install_fakeredis(monkeypatch)

    async def _run():
        await pb.publish("A", {"progress": 1})
        await pb.publish("B", {"progress": 1})
        sa = await pb.publish("A", {"progress": 2})
        return sa

    sa = asyncio.run(_run())
    assert sa == 2                                     # 每 sid 独立 seq(A 的第 2 条 = 2,不受 B 影响)


def test_subscriber_fans_out_published_message(monkeypatch):
    """模拟"另一 worker"的订阅循环收到本 worker 发布的消息(跨 worker 扇出核心)。"""
    aclient, _ = _install_fakeredis(monkeypatch)
    received = []

    async def _run():
        stop = asyncio.Event()

        async def dispatch(sid, msg):
            received.append((sid, msg))
            if len(received) >= 1:
                stop.set()

        sub = asyncio.create_task(pb.run_subscriber(dispatch, stop_event=stop))
        while not aclient._test_subscriber_states:
            await asyncio.sleep(0.01)
        await asyncio.wait_for(
            aclient._test_subscriber_states[0]["ready"].wait(), timeout=2.0
        )
        await pb.publish("sidX", {"stage": "scoring", "progress": 42, "message": "扇出"})
        try:
            await asyncio.wait_for(stop.wait(), timeout=3.0)
        finally:
            sub.cancel()
            try:
                await sub
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())
    assert received, "订阅循环未收到发布消息(跨 worker 扇出失败)"
    sid, msg = received[0]
    assert sid == "sidX"
    assert msg["progress"] == 42 and msg["message"] == "扇出" and msg["seq"] == 1


def test_subscriber_stays_alive_for_12_idle_seconds_then_receives(monkeypatch, caplog):
    """隔离 FakeServer+lupa：空闲超过旧 5s timeout 两倍后仍在线并收到第 12 秒消息。"""
    aclient, _ = _install_fakeredis(monkeypatch)
    received = []

    async def _run():
        async def dispatch(sid, msg):
            received.append((sid, msg))

        task = asyncio.create_task(pb.run_subscriber(dispatch))
        while not aclient._test_subscriber_states:
            await asyncio.sleep(0.01)
        state = aclient._test_subscriber_states[0]
        await asyncio.wait_for(state["ready"].wait(), timeout=2.0)
        await asyncio.sleep(12.1)
        assert not task.done(), "订阅者在 12 秒空闲窗内退出"
        assert len(aclient._test_subscriber_states) == 1, "空闲期发生了隐蔽重连"
        assert state["heartbeat_pings"] >= 1, "12 秒空闲期未执行应用层 PING/PONG"
        await pb.publish("idle12", {"progress": 12, "message": "after-idle"})
        for _ in range(200):
            if received:
                break
            await asyncio.sleep(0.01)
        assert received
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert state["pubsub_closed"] is True
        assert state["client_closed"] is True

    caplog.set_level("WARNING", logger="GEO-ProgressBus")
    asyncio.run(_run())
    assert received[0][0] == "idle12"
    assert received[0][1]["message"] == "after-idle"
    reconnect_logs = [
        record.getMessage() for record in caplog.records
        if "订阅循环断开" in record.getMessage() or "Timeout reading" in record.getMessage()
    ]
    assert reconnect_logs == []


def test_four_subscribers_fan_out_and_release_all_connections(monkeypatch):
    """同一隔离 FakeServer 上四个独立 subscriber 空闲后必须全部收到并完整释放。"""
    aclient, _ = _install_fakeredis(monkeypatch)
    received = []

    async def _run():
        tasks = []
        for worker in range(4):
            async def dispatch(sid, msg, worker=worker):
                received.append((worker, sid, msg))
            tasks.append(asyncio.create_task(pb.run_subscriber(dispatch)))

        for _ in range(200):
            if len(aclient._test_subscriber_states) == 4:
                break
            await asyncio.sleep(0.01)
        assert len(aclient._test_subscriber_states) == 4
        await asyncio.wait_for(
            asyncio.gather(*(state["ready"].wait() for state in aclient._test_subscriber_states)),
            timeout=2.0,
        )
        await asyncio.sleep(12.1)
        assert all(not task.done() for task in tasks)
        assert len(aclient._test_subscriber_states) == 4, "空闲期四订阅者发生了隐蔽重连"
        assert all(
            state["heartbeat_pings"] >= 1
            for state in aclient._test_subscriber_states
        ), "四条订阅连接未全部完成应用层 PING/PONG"
        await pb.publish("fanout4", {"progress": 44, "message": "four"})
        for _ in range(200):
            if len(received) == 4:
                break
            await asyncio.sleep(0.01)
        assert len(received) == 4
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        assert all(state["pubsub_closed"] for state in aclient._test_subscriber_states)
        assert all(state["client_closed"] for state in aclient._test_subscriber_states)

    asyncio.run(_run())
    assert {worker for worker, _, _ in received} == {0, 1, 2, 3}
    assert all(sid == "fanout4" and msg["message"] == "four" for _, sid, msg in received)


def test_subscriber_reconnects_after_isolated_redis_failure(monkeypatch):
    """FakeServer 断开/恢复后要重建专用连接、重新 psubscribe，并收到恢复后的最新消息。"""
    aclient, _ = _install_fakeredis(monkeypatch)
    server = aclient._test_fake_server
    received = []
    monkeypatch.setattr(pb, "_SUBSCRIBER_RECONNECT_INITIAL", 0.05)
    monkeypatch.setattr(pb, "_SUBSCRIBER_RECONNECT_MAX", 0.1)

    async def _run():
        async def dispatch(sid, msg):
            received.append((sid, msg))

        task = asyncio.create_task(pb.run_subscriber(dispatch))
        while not aclient._test_subscriber_states:
            await asyncio.sleep(0.01)
        first = aclient._test_subscriber_states[0]
        await asyncio.wait_for(first["ready"].wait(), timeout=2.0)

        server.connected = False
        for _ in range(200):
            if len(aclient._test_subscriber_states) >= 2 and first["client_closed"]:
                break
            await asyncio.sleep(0.01)
        assert len(aclient._test_subscriber_states) >= 2
        assert first["pubsub_closed"] and first["client_closed"]

        server.connected = True
        recovered = None
        for _ in range(300):
            ready_states = [
                state for state in aclient._test_subscriber_states[1:]
                if state["ready"].is_set()
            ]
            if ready_states:
                recovered = ready_states[-1]
                break
            await asyncio.sleep(0.01)
        assert recovered is not None, "Redis 恢复后未重新 psubscribe"

        await pb.publish("recovered", {"progress": 88, "message": "latest-after-recovery"})
        for _ in range(200):
            if received:
                break
            await asyncio.sleep(0.01)
        assert received
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert recovered["pubsub_closed"] and recovered["client_closed"]

    try:
        asyncio.run(_run())
    finally:
        server.connected = True
    assert received[0][0] == "recovered"
    assert received[0][1]["message"] == "latest-after-recovery"


def test_get_snapshot_sync_none_when_absent(monkeypatch):
    _install_fakeredis(monkeypatch)
    assert pb.get_snapshot_sync("nope") is None


# ---------------- seq 回退守卫(boss 审包预警 #3)----------------

def test_guard_rejects_stale_lower_seq(monkeypatch):
    """直接驱动守卫 Lua:模拟 sweeper 旧 seq 覆盖任务写者新 seq → 必须被拒。"""
    import json as _json
    _, sclient = _install_fakeredis(monkeypatch)
    script = sclient.register_script(pb._PUBLISH_GUARDED_LUA)
    keys = [pb._snap_key("g"), pb._snapseq_key("g"), pb._seq_key("g"), pb._chan_key("g")]
    r5 = script(keys=keys, args=[_json.dumps({"seq": 5, "p": 50}), 5, 0, pb.SNAP_TTL, pb._TERM_BASE], client=sclient)
    r3 = script(keys=keys, args=[_json.dumps({"seq": 3, "p": 30}), 3, 0, pb.SNAP_TTL, pb._TERM_BASE], client=sclient)
    assert int(r5) == 1 and int(r3) == 0               # seq5 写入,seq3 回退被拒
    snap = pb.get_snapshot_sync("g")
    assert snap["seq"] == 5 and snap["p"] == 50          # 快照仍是 seq5,未被 seq3 覆盖


def test_publish_terminal_is_sticky_against_later_nonterminal(monkeypatch):
    """服务端对称 HC3:终态写入后,更高 seq 的非终态不得覆盖终态快照。"""
    _install_fakeredis(monkeypatch)

    async def _run():
        await pb.publish("t1", {"progress": 10})                                  # seq1 非终态
        await pb.publish("t1", {"progress": 50, "done": True}, terminal=True)      # seq2 终态
        await pb.publish("t1", {"progress": 70})                                   # seq3 非终态 → 必须被拒

    asyncio.run(_run())
    snap = pb.get_snapshot_sync("t1")
    assert snap["seq"] == 2 and snap["progress"] == 50 and snap["terminal"] is True


# ---------------- Redis 故障恢复(boss foundation-fix #1 · 禁永久 broken latch)----------------

def test_no_permanent_latch_cooldown(monkeypatch):
    """_mark_*_broken 用冷却时间戳退避,非永久 bool latch:冷却过后 get 可再次尝试重连。"""
    pb._reset_for_test()
    clk = [100.0]
    monkeypatch.setattr(pb.time, "monotonic", lambda: clk[0])
    pb._mark_async_broken()
    assert pb._async_redis is None
    assert pb._async_redis_retry_at == 100.0 + pb._REDIS_RECONNECT_COOLDOWN  # 冷却窗
    # 冷却期内:retry_at 未到(不是永久 True 闩)
    assert clk[0] < pb._async_redis_retry_at
    # 前进过冷却:允许再次尝试(旧代码 _broken=True 会永久 None)
    clk[0] = 100.0 + pb._REDIS_RECONNECT_COOLDOWN + 1
    assert clk[0] >= pb._async_redis_retry_at
    # sync 侧同理
    pb._mark_sync_broken()
    assert pb._sync_redis is None and pb._sync_redis_retry_at > 0


def test_publish_resumes_after_transient_failure(monkeypatch):
    """瞬时故障(incr 抛一次)→ publish 返 None + 标 broken;后续 publish 恢复(非永久降级)。"""
    aclient, _ = _install_fakeredis(monkeypatch)
    calls = {"n": 0}
    real_incr = aclient.incr

    async def flaky_incr(k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("transient")
        return await real_incr(k)

    monkeypatch.setattr(aclient, "incr", flaky_incr)

    async def _run():
        s1 = await pb.publish("rec", {"progress": 10})     # 第 1 次:incr 抛 → None(fail-soft)
        s2 = await pb.publish("rec", {"progress": 20})     # 第 2 次:恢复
        return s1, s2

    s1, s2 = asyncio.run(_run())
    assert s1 is None            # 瞬时故障 fail-soft(incr 抛在自增前 → 未烧 seq)
    assert s2 == 1               # 恢复:第 2 次成功 · 取到首个真实 seq(非永久降级)
    assert pb.get_snapshot_sync("rec")["progress"] == 20


def test_guard_reject_does_not_publish(monkeypatch):
    """[1c] 守卫拒绝(陈旧/回退 seq)的消息不得继续 PUBLISH —— 用同步 pubsub 精确断言。"""
    import json as _json
    _, sclient = _install_fakeredis(monkeypatch)
    script = sclient.register_script(pb._PUBLISH_GUARDED_LUA)
    keys = [pb._snap_key("np"), pb._snapseq_key("np"), pb._seq_key("np"), pb._chan_key("np")]
    ps = sclient.pubsub()
    ps.subscribe(pb._chan_key("np"))
    ps.get_message(timeout=1)  # 吃掉 subscribe 确认

    def _drain():
        got = None
        for _ in range(5):
            m = ps.get_message(timeout=0.2)
            if m and m.get("type") == "message":
                got = m.get("data")
        return got

    script(keys=keys, args=[_json.dumps({"seq": 5}), 5, 0, pb.SNAP_TTL, pb._TERM_BASE], client=sclient)
    m1 = _drain()
    script(keys=keys, args=[_json.dumps({"seq": 3}), 3, 0, pb.SNAP_TTL, pb._TERM_BASE], client=sclient)
    m2 = _drain()
    assert m1 is not None and '"seq"' in m1     # seq5 接受 → 发布
    assert m2 is None                            # seq3 被守卫拒 → 未发布(不扇出陈旧)


def test_sync_queued_set_does_not_rollback_progress(monkeypatch):
    """set_snapshot_sync(queued/陈旧)在 publish 已接管后不得回退快照。"""
    _install_fakeredis(monkeypatch)

    async def _run():
        await pb.publish("q1", {"progress": 30})       # publish 接管 · snapseq=1

    asyncio.run(_run())
    pb.set_snapshot_sync("q1", {"progress": 0, "stage": "queued"})   # queued 迟到 → 守卫拒
    snap = pb.get_snapshot_sync("q1")
    assert snap["progress"] == 30                       # 未被 queued 回退

    # 但终态 error 仍可覆盖(is_terminal 例外)
    pb.set_snapshot_sync("q1", {"error": "boom", "done": True})
    snap2 = pb.get_snapshot_sync("q1")
    assert snap2.get("error") == "boom"


def test_publish_returns_rejected_for_stale_after_terminal(monkeypatch):
    """[返工 实时] 终态发布后,迟到的中间态被守卫 Lua 拒 → publish 返 PUBLISH_REJECTED(0)·
    调用方(send_message)据此**不本地直投**,防终态页倒退回"处理中"。"""
    _install_fakeredis(monkeypatch)

    async def _run():
        s1 = await pb.publish("sidR", {"stage": "collecting", "progress": 10}, terminal=False)
        st = await pb.publish("sidR", {"stage": "done", "progress": 100, "done": True}, terminal=True)
        # 终态之后的中间态(乱序/迟到)→ 守卫拒
        s3 = await pb.publish("sidR", {"stage": "late", "progress": 50}, terminal=False)
        return s1, st, s3

    s1, st, s3 = asyncio.run(_run())
    assert isinstance(s1, int) and s1 > 0            # 中间态接受
    assert isinstance(st, int) and st > 0            # 终态接受
    assert s3 == pb.PUBLISH_REJECTED                 # 终态后迟到中间态 → REJECTED(不本地直投)
    # 快照仍是终态(未被迟到中间态回退)
    snap = pb.get_snapshot_sync("sidR")
    assert snap.get("done") is True and snap.get("progress") == 100
