"""Loopback-only integration checks for the WORKERS=4 progress subscriber.

Run explicitly against an isolated Redis instance, for example::

    PROGRESS_BUS_TEST_REDIS_URL=redis://127.0.0.1:16379/1 \
    PROGRESS_BUS_TEST_REDIS_CONTAINER=omnirank-progress-test-<id> \
    python -m pytest --noconftest tests/test_progress_bus_real_redis.py -q

The loopback, exact port mapping, and Docker label guards deliberately reject
production or shared local targets. Selecting this file without a verified
ephemeral target is an error, never a skip.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from urllib.parse import urlparse

import pytest
import redis.asyncio as aioredis

from cache import progress_bus as pb
from tests._shared.progress_redis_guard import (
    RedisTarget, bind_progress_bus, resolve_target, verify_ephemeral_container,
)


#: 🔴 [P2-RUNTIME-1 2026-08-31] 这两个名字**只在函数里读**(见 tests/_shared/progress_redis_guard)。
#:    原来它们是模块级 `os.getenv(...)`,于是别的包想复用这组守卫就必须
#:    "在 import 本模块之前设好 env" —— 隐式顺序依赖,复用代价高到别人宁可抄一份。
#:    守卫本体已抽到 `tests/_shared/progress_redis_guard.py`,此处只留变量名。
_REDIS_URL_ENV = "PROGRESS_BUS_TEST_REDIS_URL"
_CONTAINER_ENV = "PROGRESS_BUS_TEST_REDIS_CONTAINER"


def _redis_url() -> str:
    return os.getenv(_REDIS_URL_ENV, "")


def _target() -> RedisTarget:
    """解析 + 守卫。**本体在 `tests/_shared/progress_redis_guard`,此处不留第二份。**"""
    return resolve_target(_REDIS_URL_ENV, _CONTAINER_ENV)


async def _wait_until(predicate, *, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition did not become true before timeout")


async def _pubsub_client_count(control) -> int:
    return len(await control.client_list(_type="pubsub"))


async def _pubsub_count_is(control, expected: int) -> bool:
    return await _pubsub_client_count(control) == expected


def _install_loopback_target(monkeypatch) -> str:
    target = _target()
    container_id = verify_ephemeral_container(target)
    bind_progress_bus(pb, target, monkeypatch=monkeypatch)
    return container_id


def test_real_redis_four_subscribers_survive_12s_fan_out_and_release(monkeypatch, caplog):
    """Four real sockets stay subscribed while idle, all receive, then all release."""
    _install_loopback_target(monkeypatch)
    received: list[tuple[int, str, dict]] = []

    async def _run() -> tuple[int, int]:
        control = aioredis.from_url(
            _redis_url(),
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        tasks = []
        try:
            assert await _pubsub_client_count(control) == 0

            for worker in range(4):
                async def dispatch(sid, message, worker=worker):
                    received.append((worker, sid, message))

                tasks.append(asyncio.create_task(pb.run_subscriber(dispatch)))

            await _wait_until(
                lambda: _pubsub_count_is(control, 4),
                timeout=5.0,
            )
            numpat_before = int(await control.pubsub_numpat())
            await asyncio.sleep(12.1)
            assert all(not task.done() for task in tasks)
            assert await _pubsub_client_count(control) == 4

            payload = json.dumps(
                {"seq": 1, "progress": 44, "message": "real-four"},
                ensure_ascii=False,
            )
            await control.publish(pb._chan_key("real-four"), payload)

            async def _all_received() -> bool:
                return len(received) == 4

            await _wait_until(_all_received, timeout=3.0)
            return numpat_before, int(await control.pubsub_numpat())
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                assert all(isinstance(result, asyncio.CancelledError) for result in results)

            async def _all_released() -> bool:
                return await _pubsub_count_is(control, 0)

            await _wait_until(_all_released, timeout=3.0)
            await control.aclose()

    caplog.set_level("WARNING", logger="GEO-ProgressBus")
    numpat_before, numpat_after = asyncio.run(_run())

    assert {worker for worker, _, _ in received} == {0, 1, 2, 3}
    assert all(sid == "real-four" for _, sid, _ in received)
    assert all(message["message"] == "real-four" for _, _, message in received)
    assert numpat_before == numpat_after == 1  # NUMPAT counts the shared pattern, not clients.
    assert not any(
        "Timeout reading" in record.getMessage() or "订阅循环断开" in record.getMessage()
        for record in caplog.records
    )


def test_real_redis_disconnect_restart_resubscribes(monkeypatch):
    """Stopping and restarting only the named test container rebuilds the subscription."""
    container_id = _install_loopback_target(monkeypatch)
    attempts = 0
    received: list[tuple[str, dict]] = []
    create_subscriber = pb._create_subscriber_redis

    async def tracked_create_subscriber():
        nonlocal attempts
        attempts += 1
        return await create_subscriber()

    monkeypatch.setattr(pb, "_create_subscriber_redis", tracked_create_subscriber)
    monkeypatch.setattr(pb, "_SUBSCRIBER_RECONNECT_INITIAL", 0.1)
    monkeypatch.setattr(pb, "_SUBSCRIBER_RECONNECT_MAX", 0.2)
    monkeypatch.setattr(pb, "_SUBSCRIBER_HANDSHAKE_TIMEOUT", 0.5)

    def _docker(*args: str) -> None:
        subprocess.run(
            ["docker", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )

    async def _run() -> None:
        control = aioredis.from_url(
            _redis_url(),
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        task = None
        container_running = True
        try:
            await control.ping()

            async def dispatch(sid, message):
                received.append((sid, message))

            task = asyncio.create_task(pb.run_subscriber(dispatch))
            await _wait_until(lambda: _pubsub_count_is(control, 1), timeout=5.0)

            await asyncio.to_thread(_docker, "stop", "--time", "1", container_id)
            container_running = False

            async def _retry_started() -> bool:
                return attempts >= 2 and not task.done()

            await _wait_until(_retry_started, timeout=6.0)
            await asyncio.to_thread(_docker, "start", container_id)
            container_running = True

            async def _redis_ready() -> bool:
                try:
                    return bool(await control.ping())
                except Exception:
                    return False

            await _wait_until(_redis_ready, timeout=10.0)
            await _wait_until(lambda: _pubsub_count_is(control, 1), timeout=10.0)

            payload = json.dumps(
                {"seq": 2, "progress": 88, "message": "real-recovered"},
                ensure_ascii=False,
            )
            await control.publish(pb._chan_key("real-recovered"), payload)

            async def _received() -> bool:
                return bool(received)

            await _wait_until(_received, timeout=3.0)
        finally:
            if not container_running:
                await asyncio.to_thread(_docker, "start", container_id)
            if task is not None:
                task.cancel()
                result = await asyncio.gather(task, return_exceptions=True)
                assert isinstance(result[0], asyncio.CancelledError)
            await control.aclose()

    asyncio.run(_run())
    assert attempts >= 2
    assert received == [
        ("real-recovered", {"seq": 2, "progress": 88, "message": "real-recovered"})
    ]
