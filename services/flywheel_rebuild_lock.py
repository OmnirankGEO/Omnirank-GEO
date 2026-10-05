"""[B1-6] 飞轮 rebuild/analyze 端点的后台化 + 互斥守卫。

背景:两个飞轮 admin API(api/media_entity_flywheel_api.py / api/writing_style_flywheel_api.py)
里的 rebuild/analyze POST 端点原本在 `async def` 里直接同步执行(最高 20 万行扫描),
WORKERS=1 时独占事件循环数十秒~分钟 → 冻结全站(连 /health 都卡)。

本模块提供一个统一守卫:
  1. 把同步重建挪到线程池(`asyncio.to_thread`)执行,解除事件循环阻塞。
     连接池是 psycopg2 ThreadedConnectionPool(线程安全),每个重建函数各自 get_connection(),
     不跨线程共享连接 → to_thread 安全(见 db/connection.py)。
  2. 同 key 互斥:同一重建正在进行时,第二次触发立即返回 {"status":"in_progress",...},
     不并行执行(防双击/自动刷新导致并发重复重建)。

红线合规:本模块不 import/调用任何 billing/charge/freeze/deduct;纯并发工具。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable

# 进行中的重建 key 集合。WORKERS=1 单进程:检查/加入/移除都在事件循环线程内发生,
# 天然串行;仍加锁做防御(未来多线程入口也安全)。
_INFLIGHT: set[str] = set()
_LOCK = threading.Lock()


async def run_rebuild_guarded(key: str, fn: Callable[..., Any], /, **kwargs) -> Any:
    """守卫地跑一个同步重建/分析函数。

    - `key`:互斥键(同 key 不并行)。
    - `fn`:同步函数;`kwargs` 透传给它。若传入无参闭包,直接 `run_rebuild_guarded(key, closure)`。
    - 正在进行中 → 立即返回 in_progress dict(不阻塞、不排队)。
    - 否则在线程池执行 fn 并返回其结果;finally 里释放 key。
    """
    with _LOCK:
        if key in _INFLIGHT:
            return {
                "status": "in_progress",
                "message": "该重建/分析正在进行中，请稍后再试（避免并发重复重建）。",
                "key": key,
            }
        _INFLIGHT.add(key)
    try:
        return await asyncio.to_thread(lambda: fn(**kwargs))
    finally:
        with _LOCK:
            _INFLIGHT.discard(key)


def is_rebuild_running(key: str) -> bool:
    """只读探测某 key 是否进行中(供只读端点/调试用)。"""
    with _LOCK:
        return key in _INFLIGHT
