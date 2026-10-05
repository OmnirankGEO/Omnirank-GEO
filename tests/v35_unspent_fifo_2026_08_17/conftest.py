"""单账本 FIFO 未消费口径 · conn fixture(形状构造在 _ledger.py)。"""
from __future__ import annotations

import os

import pytest

DB = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")


@pytest.fixture()
def conn():
    if not DB:
        pytest.skip("需 TEST_DATABASE_URL")
    import psycopg2
    import psycopg2.extras
    c = psycopg2.connect(DB)
    c.cursor_factory = psycopg2.extras.RealDictCursor
    yield c
    try:
        c.rollback()
    except Exception:
        pass
    c.close()
