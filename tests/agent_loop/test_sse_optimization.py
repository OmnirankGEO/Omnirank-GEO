"""SSE 优化 2026-05-16 回归测试.

3 项优化:
1. 🔴 aiohttp ClientSession singleton shutdown hook 防 ResourceWarning
(原第 2、3 项是社媒工作台对话页的前端静态检查,页面随开源 E3 整删,两格退役 [开源 E3 · 前端 · 2026-10-01 · WO_322])
"""

from __future__ import annotations

from pathlib import Path


def test_api_5118_has_close_helper():
    """🔴 api_5118 必须暴露 close_5118_client async 函数 · server shutdown 调."""
    src = Path("tools/api_5118.py").read_text(encoding="utf-8")
    assert "async def close_5118_client" in src, "api_5118 必须有 close_5118_client helper"
    assert "_client = None" in src, "close 后 _client 必须归 None · 防关闭后再用"


def test_server_shutdown_calls_5118_close():
    """🔴 server.py shutdown event 必须调 close_5118_client · 防 aiohttp leak warning."""
    src = Path("server.py").read_text(encoding="utf-8")
    assert '@app.on_event("shutdown")' in src, "server.py 必须有 shutdown event handler"
    assert "close_5118_client" in src, "shutdown handler 必须调 close_5118_client"


