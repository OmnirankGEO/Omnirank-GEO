"""P0 · 推荐组合包恒空 · 真实生产行形态 fixture。

🔴 验收硬条件(Review 裁定 REVIEW_VERDICT_PUBFILTER_B_2026-08-08.md §新P0):
   锁必须打在「**真实生产数据形态**喂进去,组合包非空」上。
   上一个包(pubfilter)的 conftest 为了让自己的锁非空,被迫把 pc_weight/m_weight/
   inclusion_rate/authority 手喂拉满(注释里自认了)——那种 fixture 恰恰证明不了
   生产数据能不能出组合包。本套件用 replica 快照的 **1290 条真实行**,一个字段不改。

schema 初始化顺序与生产一致(server.py:1561 init_publish_tables → :1592 init_mhz_tables),
理由见 pubfilter 包的 test_schema_parity:两份 `CREATE TABLE IF NOT EXISTS mhz_media`
并存,谁先跑谁赢,`inclusion_rate` 的 text/integer 就是判别列。

运行前置:
    TEST_DATABASE_URL=postgresql://geo_admin:pubtest@127.0.0.1:55640/geo_pubrec_test
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

EXPECTED_DB_SUFFIX = "geo_pubrec_test"
FIXTURE = Path(__file__).parent / "replica_pool_snapshot.sql"


@pytest.fixture(scope="session", autouse=True)
def _schema():
    url = os.environ.get("DATABASE_URL", "")
    if not url.rstrip("/").endswith(EXPECTED_DB_SUFFIX):
        raise RuntimeError(
            "本套件会 TRUNCATE 媒体池与 mhz 表,只允许跑在专用测试库上。\n"
            f"当前 DATABASE_URL 尾部不是 {EXPECTED_DB_SUFFIX}:{url!r}"
        )
    from db.auth_db import init_auth_db
    from db.meijiehezi_db import init_mhz_tables
    from db.publish_db import init_publish_tables

    init_auth_db()
    init_publish_tables()
    init_mhz_tables()
    yield


def _load_snapshot() -> int:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        for table in ("media_effective_pool", "mhz_media", "mhz_wemedia"):
            cur.execute(f"TRUNCATE TABLE {table}")
        cur.execute(FIXTURE.read_text(encoding="utf-8"))
        conn.commit()
        cur.execute("SELECT count(*) AS n FROM media_effective_pool")
        return int(cur.fetchone()["n"])
    finally:
        conn.close()


@pytest.fixture(scope="session")
def real_pool(_schema):
    """把 replica 快照灌进测试库。session 级:1290 行不必每个用例重灌。"""
    loaded = _load_snapshot()
    assert loaded == 1290, f"fixture 只灌进 {loaded} 行 —— 分母不对,后面所有数都不能信"
    yield loaded


@pytest.fixture()
def mutable_pool(real_pool):
    """给需要**改数据**做反向对照的用例。

    🔴 第一版用 `conn.rollback()` 收尾 —— 收不回来:被测函数
       `get_effective_pool_candidates` 开的是**自己的连接**,不 commit 它根本看不见改动;
       一 commit,rollback 就什么也回滚不了,后面同 session 的用例全跑在被改过的库上。
       改成收尾**重灌快照**,并当场核对行数 —— 恢复要有判据,不能"我以为回滚了"。
    """
    from db.connection import get_connection

    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()
        assert _load_snapshot() == 1290, "反向对照跑完没能把快照恢复回去"
