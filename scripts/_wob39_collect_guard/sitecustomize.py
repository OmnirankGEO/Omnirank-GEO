# -*- coding: utf-8 -*-
"""收集期封网:`pytest --collect-only` 期间任何 socket 连接一律抛异常。

装法:把本目录放进 `PYTHONPATH` —— `sitecustomize` 由解释器在启动时自动 import,
**早于 pytest、早于任何被收集模块**,所以模块级的连库/连 Redis 会当场炸。

为什么打 socket 层而不是逐库打桩(`psycopg2.connect` / `redis.Redis`):
  · 逐库打桩要先 import 那个库 —— 而 sitecustomize 里 import psycopg2 会拖慢每一次解释器启动,
    还会在没装该库的环境里自己炸掉。
  · socket 是**共同下游**:psycopg2 / redis / requests / httpx 全都从这里出去。
    打一处覆盖全部,而且新增的库自动被覆盖 —— 不用维护一份手写的库名单。
    (手写名单漏掉的那一项不会让任何判据变红。)

只在 `WOB39_BLOCK_SOCKETS=1` 时生效 —— 默认完全无副作用,不影响这棵树上别人的任何命令。
"""
import os

if os.environ.get("WOB39_BLOCK_SOCKETS") == "1":
    import socket

    _ALLOW_AF = {getattr(socket, "AF_UNIX", None)}  # 目前不放行任何族,留着做将来的白名单

    class CollectTimeNetworkAccess(RuntimeError):
        """收集期(import 期)发起了网络连接 —— 这就是本门要抓的东西。"""

    _orig_connect = socket.socket.connect
    _orig_connect_ex = socket.socket.connect_ex

    def _blocked(self, address, *a, **kw):
        raise CollectTimeNetworkAccess(
            f"WOB39: 收集期禁止网络连接,但有代码试图连 {address!r}。"
            f"含义:某个被收集的模块在 **import 期**就去连库/连 Redis/发 HTTP —— "
            f"它不是判据文件,是会自己干活的脚本。"
        )

    socket.socket.connect = _blocked
    socket.socket.connect_ex = _blocked
