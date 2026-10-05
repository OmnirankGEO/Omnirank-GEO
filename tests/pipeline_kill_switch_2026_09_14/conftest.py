"""WO_213 c1 判据包夹具。

本包**不写库**,但 import 链会建连接池、要求库真的存在
(`api.geo_douyin_api` 的 import 链会连库)。借一个已建好的空库即可。
"""
import os

_DSN = "postgresql://geo_admin:testpw@localhost:55492/geo_c14_196_test"


def pytest_configure(config):
    os.environ.setdefault("TEST_DATABASE_URL", _DSN)
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
