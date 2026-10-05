"""#96 新增:按需的真库 fixture。

🔴 本包既有判据都是 monkeypatch 的,不连库。这个 fixture **只在被请求时**才建连接,
   不在 import / collection 期做任何事 —— 包级 conftest 会作用于全包,
   在这里连库会把一批本来不需要库的判据也拖下水。
"""
from __future__ import annotations

import os

import pytest


def _dsn() -> str:
    dsn = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    if "test" not in dsn:
        pytest.skip("需要名字带 test 的库(避免误打生产)", allow_module_level=False)
    return dsn


@pytest.fixture
def db():
    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(_dsn(), cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()
