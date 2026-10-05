"""[WO-KYB D0-b] 测试替身 —— 不连库。"""
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for _p in (_ROOT, os.path.dirname(__file__)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture()
def proj():
    import services.media_price_projection as m
    return m
