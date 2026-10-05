"""复用 P0-3c 的运行时台架 —— 真 PG + **真 import server**。

不另起一套:同一个台架两处维护必有一处漂(本仓老教训)。
P0-3c 的 conftest 已经解决了「判据目录零条执行 server.py」那个结构性根因,
本单直接站在它上面。
"""
from tests.p03c_org_guards_2026_08_25.conftest import (  # noqa: F401
    db,
    live_dsn,
    live_server,
)
