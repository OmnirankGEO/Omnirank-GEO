"""[FIND-MHZ-ORDERITEMS-2026-08-04] 测试替身 —— 不连库。"""
import os
import sys
import types

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for _p in (_ROOT, os.path.dirname(__file__)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class FakeRequest:
    """只提供 `request.state.user` —— `_get_user` 就读这一处。"""

    def __init__(self, user=None):
        self.state = types.SimpleNamespace()
        if user is not None:
            self.state.user = user


@pytest.fixture()
def fake_request():
    return FakeRequest


@pytest.fixture()
def mhz_api():
    import api.meijiehezi_api as mod
    return mod
