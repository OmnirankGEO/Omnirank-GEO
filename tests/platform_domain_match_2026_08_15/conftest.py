"""[平台域口径 2026-08-15] 只放 fixture;助手在 platdomain_fixtures.py。

🔴 助手不放这里,是因为仓内多个目录都写 `from conftest import ...`,
   同一次收集里会互相抢占(实测与 flywheel_binding_liveness 目录同跑必 ImportError)。
"""
from __future__ import annotations

import pytest

from platdomain_fixtures import cleanup  # noqa: F401
from db.media_entity_flywheel_db import init_media_entity_flywheel_tables


@pytest.fixture(autouse=True)
def _tables():
    init_media_entity_flywheel_tables()
    cleanup()
    yield
    cleanup()
