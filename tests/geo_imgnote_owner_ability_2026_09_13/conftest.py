"""#193 P0 · 组织 owner 发不了图文 —— 判据包夹具。

本包**不碰库**:被测的是 `_granted_abilities` 这个纯谓词与 `_guard` 的分支。
只是 import `api.geo_image_note_api` 时链上的模块要一个 DSN,所以给一个。
"""
import os

_DSN = "postgresql://geo_admin:testpw@localhost:55492/geo_c14_184d2_test"


def pytest_configure(config):
    os.environ.setdefault("TEST_DATABASE_URL", _DSN)
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
