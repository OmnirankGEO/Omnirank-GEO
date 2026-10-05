"""真 Redis 判据目标的守卫 —— **全仓只此一份**。

为什么要抽出来
--------------
这组守卫(只许 loopback / 只许 db=1 / 只许贴了标签的一次性容器 / 端口映射必须对上)
原来长在 ``tests/test_progress_bus_real_redis.py`` 里,而那个模块在 **import 期**
就读自己的两个 env。于是别的包想复用就必须"在 import 它之前设好环境变量"——
「谁先设 env」变成隐式顺序依赖,复用代价高到上一版我宁可**抄一遍**。
抄一遍 = 同一组守卫存在两处,而漂掉的那一处不会有任何信号。

所以这里做两件事:
  ① env **函数内懒读**(这正是当初被迫抄写的根因),调用方各自带自己的变量名;
  ② 守卫本体只此一份,两个调用点都 import 它。

🔴 守卫不是洁癖,是防"把判据打到生产/共享 Redis 上":
   loopback + 一次性容器标签 + 端口映射逐条对上,任何一条不成立都**当场失败**,
   **不 skip** —— 选中了真 Redis 判据却没有可信目标,那是错误,不是"跳过"。
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import NamedTuple
from urllib.parse import urlparse

#: 一次性测试容器的命名前缀与标签(与仓库现役约定一致,别再发明第二套)。
CONTAINER_PREFIX = "omnirank-progress-test-"
CONTAINER_LABEL = "omnirank.progress-bus-test"
CONTAINER_LABEL_VALUE = "true"


class RedisTarget(NamedTuple):
    host: str
    port: int
    db: int
    container: str


def resolve_target(dsn_env: str, container_env: str, *,
                   dsn_default: str = "", container_default: str = "") -> RedisTarget:
    """从 env **现读**目标并逐条守住。

    `dsn_env` 传的变量名建议以 ``_DSN`` 结尾:18 包分母那道「读了没落值」的闸
    识别正则是 ``DSN|DATABASE|DB_URL|_DB$|TEST_DB``,叫 ``*_REDIS_URL``
    会整个漏出它的视野。
    """
    dsn = os.getenv(dsn_env, dsn_default)
    container = os.getenv(container_env, container_default)
    assert dsn, f"选中真 Redis 判据就必须给 {dsn_env}(没有可信目标 = 错误,不是 skip)"
    parsed = urlparse(dsn)
    assert parsed.scheme == "redis", f"{dsn_env} 必须是 redis:// —— 实得 {dsn!r}"
    assert parsed.hostname in {"127.0.0.1", "localhost", "::1"}, \
        f"只许 loopback 目标(拒生产/共享 Redis):{parsed.hostname!r}"
    assert parsed.username is None and parsed.password is None, \
        "一次性测试目标不该带凭据 —— 带了就说明指向的不是它"
    db = int((parsed.path or "/0").lstrip("/") or "0")
    assert db == 1, "progress bus 判据只用 db=1"
    assert parsed.port is not None, f"{dsn_env} 必须显式带端口"
    assert container.startswith(CONTAINER_PREFIX), \
        f"{container_env} 必须以 {CONTAINER_PREFIX!r} 开头 —— 实得 {container!r}"
    return RedisTarget(parsed.hostname, parsed.port, db, container)


def verify_ephemeral_container(target: RedisTarget) -> str:
    """把 loopback 目标**绑定到一个显式贴了标签的一次性容器**,返回容器 id。

    光有 loopback 不够:本机可能同时跑着别的 Redis(生产镜像的、别的窗口的)。
    标签 + 端口映射逐条对上,才敢往里写。
    """
    out = subprocess.run(["docker", "inspect", target.container],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, f"目标容器不在:{out.stderr.strip()}"
    inspected = json.loads(out.stdout)
    assert len(inspected) == 1
    container = inspected[0]
    assert container["Name"] == f"/{target.container}"
    assert container["State"]["Running"] is True, "目标容器没在跑"
    labels = (container.get("Config") or {}).get("Labels") or {}
    assert labels.get(CONTAINER_LABEL) == CONTAINER_LABEL_VALUE, \
        "目标 Redis 没贴一次性测试容器标签 —— 拒绝把判据打到来路不明的实例上"
    bindings = ((container.get("NetworkSettings") or {}).get("Ports") or {}).get("6379/tcp") or []
    expected_hosts = {"127.0.0.1", "::1"}
    if target.host == "localhost":
        def _host_matches(mapped):
            return mapped in expected_hosts
    else:
        def _host_matches(mapped):
            return mapped == target.host
    assert any(_host_matches(b.get("HostIp")) and int(b.get("HostPort", "0")) == target.port
               for b in bindings), \
        "Redis DSN 与那个贴标签容器的 loopback 端口映射对不上"
    return container["Id"]


def bind_progress_bus(pb, target: RedisTarget, *, port: int | None = None, monkeypatch=None):
    """把 `cache.progress_bus` 指到目标(或指到 `port` 给的死端口模拟失联)。

    复位走 progress_bus 自己的 ``_reset_for_test()`` —— 不在判据侧手写一遍
    "该清哪几个全局",那份清单迟早跟本体漂。
    """
    values = {"_REDIS_HOST": target.host,
              "_REDIS_PORT": target.port if port is None else port,
              "_REDIS_DB": target.db}
    for name, value in values.items():
        if monkeypatch is not None:
            monkeypatch.setattr(pb, name, value)
        else:
            setattr(pb, name, value)
    pb._reset_for_test()
