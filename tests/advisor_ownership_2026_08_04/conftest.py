"""[ADVISOR-OWNERSHIP] 测试替身 —— 不连库。

直接调端点函数(而不是起 TestClient),因为 `request.state.user` 本来就由全局中间件注入,
在单测里造一个 stub request 比把整套中间件拉起来更精确,也不会把中间件的行为混进判据。
"""
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for _p in (_ROOT, os.path.dirname(__file__)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# 替身类与常量拆到 _advisor_helpers(模块名唯一,避免与别的测试包 conftest 撞名)
from _advisor_helpers import FakeConn  # noqa: E402


@pytest.fixture()
def mod():
    import api.advisor_api as m
    # 归属列自愈是进程级缓存;单测里直接置位,免得每个用例都去数那两条 DDL
    m._OWNERSHIP_SCHEMA_READY = True
    return m


@pytest.fixture()
def patch_conn(monkeypatch, mod):
    """把 advisor_api 里的 get_connection 换成假连接,返回 setter。"""
    holder = {}

    def _install(rows_queue=None):
        conn = FakeConn(rows_queue)
        holder["conn"] = conn
        monkeypatch.setattr(mod, "get_connection", lambda: conn)
        return conn

    return _install
