"""包 A~E 判据底座。

🔴 安全栓与 `tests/xiaobang_vnext_2026_08_18/conftest.py` **同形态,不另造一套**:
   库名必须同时含 ``xbsolve`` 与 ``test``。
   · 指不到生产(生产库名 ``geo_agentscope``);
   · 指不到别的包的库(别人的库名不含 ``xbsolve``);
   · 双树 A/B 时两臂可以各有一个(所以不写死单个 URL ——
     2026-08-15 实测:写死单 URL ⇒ A/B 两臂 0 junit,而「没跑起来」
     和「跑了全过」在退出码上一模一样)。
"""

from __future__ import annotations

import os

import pytest

DEFAULT_THROWAWAY_URL = (
    "postgresql://geo_admin:testpw@localhost:55451/geo_xbsolve_test"
)

_REQUIRED_DB_TOKENS = ("xbsolve", "test")

_configured = os.environ.get("TEST_DATABASE_URL", "").split("?", 1)[0]
_dbname = _configured.rsplit("/", 1)[-1].lower()
_missing = [t for t in _REQUIRED_DB_TOKENS if t not in _dbname]
if not _configured or _missing:
    raise RuntimeError(
        "xiaobang_solution_first 判据锁死在本包一次性库上:库名必须同时含 {0};"
        "实得 {1!r}(缺 {2})。单跑用 {3}".format(
            _REQUIRED_DB_TOKENS, _configured, _missing, DEFAULT_THROWAWAY_URL
        )
    )

EXACT_THROWAWAY_URL = _configured
os.environ["DATABASE_URL"] = EXACT_THROWAWAY_URL


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: 需要拉起子进程的判据")


@pytest.fixture(scope="session")
def throwaway_db_url() -> str:
    return EXACT_THROWAWAY_URL
