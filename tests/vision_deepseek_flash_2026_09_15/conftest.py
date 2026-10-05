# -*- coding: utf-8 -*-
"""本包大部分判据靠桩;但**模型解析那一条必须打真库**。

🔴 理由是实证:13 条桩判据全绿的同时,走真实代码路径打真厂商返回的是
   `qwen3.6-flash / dashscope` —— 因为共享键 `vision_model` 被
   `ensure_schema()` **播种进库**,模块里的兜底默认永远轮不到。
   桩掉 `resolve_vision_model` 的判据**按定义看不见这件事**。
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def live_db():
    dsn = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    if "test" not in dsn:
        pytest.skip("需要一个名字里带 test 的库(避免误打生产)")
    os.environ["DATABASE_URL"] = dsn
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import db.connection as dbconn
    dbconn.DATABASE_URL = dsn
    dbconn._pool = None
    from db.social_preferences_db import ensure_schema
    ensure_schema()
    return dsn
