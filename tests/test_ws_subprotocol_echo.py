# -*- coding: utf-8 -*-
"""[Review-CTO 2026-07-27] WS 子协议回选判别锁(Owner 生产实测"重连中"根因)。

08052a2c 起前端用 ['omnirank-auth', token] 子协议鉴权,RFC 6455 要求服务端 101
回选其一;server.py ConnectionManager.connect 原来裸 accept() → 浏览器判握手
失败 → 实时通道自 07-20 静默死亡,全站诊断进度全靠 5s 轮询,顶部永远"重连中"。

源码级断言(真握手需 DB+JWT 全套,部署后由浏览器绿点验收):
  1. accept 必须带 subprotocol 参数;
  2. 回选值必须是 omnirank-auth 且以客户端 offered 为条件(不能无脑硬编码,
     否则不带子协议的 legacy query-token 客户端会被回一个它没请求的协议)。
"""
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8")


def test_manager_accept_echoes_offered_subprotocol():
    assert "await websocket.accept(subprotocol=subprotocol)" in SRC, (
        "ConnectionManager.connect 必须回选子协议,裸 accept() 会让带子协议的"
        "浏览器握手失败,前端永远'重连中'"
    )
    assert 'subprotocol = "omnirank-auth" if offered' in SRC, (
        "回选必须以客户端 offered 为条件;对 legacy 无子协议客户端必须回 None"
    )


def test_no_bare_accept_left_in_manager():
    # ConnectionManager.connect 区域内不允许再出现裸 accept()
    start = SRC.index("async def connect(self, session_id")
    seg = SRC[start:start + 1500]
    assert "websocket.accept()" not in seg, "connect 里仍有裸 accept()"
