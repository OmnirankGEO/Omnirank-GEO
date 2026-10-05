"""小榜 vNext 判据底座。

🔴 库锁死在本包一次性容器上(与 tests/gap_plan_2026_08_08 / pricing_quote_wiring
同形态,不另造第二套)。理由不是洁癖:2026-08-15 有一次双臂 A/B **两边都是
0 junit**,原因就是库名不含 ``test`` 把 conftest 的安全栓全灭了,而
「没跑起来」和「跑了全过」在退出码上一模一样。所以这里锁死到**具体一个 URL**,
接不上就当场 RuntimeError,不静默降级。
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

ROOT = Path(__file__).resolve().parents[2]

#: 默认库(单跑时用)。
DEFAULT_THROWAWAY_URL = "postgresql://geo_admin:testpw@localhost:55441/geo_xbvnext_test"

#: 🔴 安全栓不是"一个写死的 URL",而是**两个必须同时成立的词**。
#: 写死单个 URL 的话,双树 A/B 就跑不了 —— 它要给两臂各一个独立库,
#: 而 A/B 恰恰是用来抓「两边都没跑起来却报无新增红」的那件工具
#: (2026-08-15 实测:库名不含 test → conftest 安全栓全灭 → 双臂 0 junit 假绿)。
#: 所以这里放宽到"库名必须含 xbvnext 且含 test":
#:   · 指不到生产(生产库名 geo_agentscope);
#:   · 指不到别的包的库(别人的库名不含 xbvnext);
#:   · 两臂可以各有一个。
_REQUIRED_DB_TOKENS = ("xbvnext", "test")

_configured = os.environ.get("TEST_DATABASE_URL", "").split("?", 1)[0]
_dbname = _configured.rsplit("/", 1)[-1].lower()
_missing = [t for t in _REQUIRED_DB_TOKENS if t not in _dbname]
if not _configured or _missing:
    raise RuntimeError(
        "xiaobang_vnext 测试锁死在本包一次性库上:库名必须同时含 {0};"
        "实得 {1!r}(缺 {2})。单跑用 {3}".format(
            _REQUIRED_DB_TOKENS, _configured, _missing, DEFAULT_THROWAWAY_URL
        )
    )

EXACT_THROWAWAY_URL = _configured
os.environ["DATABASE_URL"] = EXACT_THROWAWAY_URL

MIGRATIONS = (
    ROOT / "db" / "migration_038_xiaobang_intent_2026_08_18.sql",
    # 🔴 [窗G 段二①] 小榜 prepare 现在会读 defgeo_xiaobang_frozen_reasons
    #    (§9.6 只读冻结理由),那张表在 046 里建。046 是五张**全新**表、
    #    零 ALTER、体内零 DML,叠加安全。
    #    不装它 ⇒ 带 quote_id 的 prepare 会 UndefinedTable ——
    #    这**不是**夹具洁癖:生产 prestart 每次部署都会重放全部迁移,
    #    夹具少装一份就是在测一个生产上不存在的世界。
    ROOT / "db" / "migration_046_defgeo_monitoring_lineage_2026_08_22.sql",
)

#: 兼容旧引用(有判据按名取这一份)。
MIGRATION = MIGRATIONS[0]


def _connect():
    conn = psycopg2.connect(EXACT_THROWAWAY_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """schema **不手写**:直接跑要上线的那份迁移文件。

    手写一份精简 schema 就是第二套表定义 —— 列一漏、CHECK 一少,测试全绿而
    生产照样炸(本仓 2026-08-09 记过「测试 schema 类型不同构照样全绿」)。
    这里还顺带证明了迁移**能跑**,而不是只证明它躺在 manifest 里。
    """
    conn = _connect()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            for migration in MIGRATIONS:
                cur.execute(migration.read_text(encoding="utf-8"))
            # 🔴 活性自证:两份迁移都必须真的落地了。
            #    少了任何一张表,下面的判据会以"跟本包无关的理由"变红,
            #    或者更糟 —— 悄悄测了一个生产上不存在的世界。
            cur.execute("SELECT to_regclass('public.xiaobang_operation_intents') AS a,"
                        "       to_regclass('public.defgeo_xiaobang_frozen_reasons') AS b")
            row = cur.fetchone()
            got = (row["a"], row["b"]) if isinstance(row, dict) else (row[0], row[1])
            assert all(got), "迁移没全装上:" + str(got)
    finally:
        conn.close()
    yield


@pytest.fixture
def db():
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM xiaobang_confirmation_receipts")
            cur.execute("DELETE FROM xiaobang_operation_intents")
        conn.commit()
        yield conn
    finally:
        conn.rollback()
        conn.close()


def pytest_configure(config):
    """`slow` = 要起子进程/跑 node 的判据(--list 收集、护栏反向对照)。

    注册一下,免得 PytestUnknownMarkWarning 把真正的告警淹掉。
    """
    config.addinivalue_line("markers", "slow: 需要拉起 node 子进程的判据")
