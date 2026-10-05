"""诊断链品牌归属判据底座 —— 真 PG16 一次性库。

🔴 为什么要真库,不用夹具
------------------------
被测的那件事是「会不会**多出一行** brands」。行数是库里的事实,
用假仓储替身测,替身怎么写答案就怎么变(夹具替被测代码干活 ⇒ 恒绿)。

🔴 唯一性约束必须与生产同形
--------------------------
生产实查只有 ``brands_pkey`` 与
``brands_name_owner_key UNIQUE (name, owner_user_id) WHERE is_deleted = false``
—— 唯一性是**按 owner** 的、且**只管活行**;跨 owner 同名**合法**
(不同服务商服务同一家企业,生产上一个名字 9 个 owner)。
本缺陷正是踩在这条合法性上:admin 代跑他人品牌时按「名字 + 发起人」建档,
约束不拦、静默多出一条副本。库名兜底(全局 UNIQUE)会让这条判据**测不到**,
所以这里显式建生产那一条**部分唯一索引**。

🔴 安全栓:库名必须同时含 ``diagbrand`` 与 ``test``(本底座 DROP SCHEMA)。
"""

from __future__ import annotations

import os

import psycopg2
import psycopg2.extras
import pytest

DEFAULT_THROWAWAY_URL = (
    "postgresql://geo_admin:testpw@localhost:55498/geo_diagbrand_test"
)

_REQUIRED_DB_TOKENS = ("diagbrand", "test")

#: 🔴 如实措辞:这是 ``brands`` 的**列子集**,只含被测链真正读写的那几列,
#:   不是生产 DDL 的副本;没有任何判据拿它当分母。
#:   列名/类型对齐 ``db/brands_schema.py`` 与 ``db/diagnosis_db.get_or_create_brand``。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS public.brands (
    id                SERIAL PRIMARY KEY,
    name              TEXT NOT NULL,
    is_deleted        BOOLEAN DEFAULT FALSE,
    industry          TEXT,
    industry_category TEXT,
    owner_user_id     INTEGER,
    brand_type        TEXT,
    brand_code        TEXT,
    is_test           BOOLEAN DEFAULT FALSE,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS brands_name_owner_key
    ON public.brands (name, owner_user_id) WHERE is_deleted = false;
"""


#: 🔴 `get_or_create_brand` 自己开连接(`db.connection.get_connection()`),
#:   不收 cursor。所以要在业务模块 import 之前把 DATABASE_URL 指到本包的一次性库,
#:   否则判据会跑在**别的库**上而四个信号全正常(本仓为这个形态记过账)。
def _url() -> str:
    url = os.getenv("DIAGBRAND_TEST_DB_URL") or DEFAULT_THROWAWAY_URL
    dbname = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    missing = [t for t in _REQUIRED_DB_TOKENS if t not in dbname]
    if missing:
        raise RuntimeError(
            f"拒绝在库名 {dbname!r} 上跑:安全栓要求库名同时含 "
            f"{_REQUIRED_DB_TOKENS},缺 {missing} —— 本底座会 DROP SCHEMA public。")
    return url


def _assert_fixture_actually_ran(cur) -> None:
    """活性自证:没有这一条,空库上跑的判据会全绿(被测对象根本不存在)。"""
    cur.execute("SELECT to_regclass('public.brands') AS t")
    if cur.fetchone()["t"] is None:
        raise RuntimeError("底座装完却查不到 brands —— 判据没有被测对象")
    cur.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname='public' "
        "AND indexname='brands_name_owner_key'")
    if cur.fetchone() is None:
        raise RuntimeError(
            "部分唯一索引没建起来 —— 「同 owner 同名复用 / 跨 owner 同名新建」"
            "这条分岔在本库上测不到,判据会恒绿")


@pytest.fixture(scope="session")
def diagbrand_db():
    conn = psycopg2.connect(_url(), cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    with conn.cursor() as cur:
        # 冷库:上一轮残留行会污染行数断言,而行数正是本包的被测量。
        cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
        cur.execute("CREATE SCHEMA public")
        cur.execute(_SCHEMA)
        _assert_fixture_actually_ran(cur)
    yield conn
    conn.close()


@pytest.fixture
def cur(diagbrand_db):
    """每条判据一张空 brands 表 —— 行数是本包的被测量,残留行会污染它。

    🔴 用 DELETE 不用 TRUNCATE:import ``workflows.diagnosis_workflow`` 会带起
       一段 schema 自举,建出 ``diagnosis_records`` 并对 ``brands`` 加外键,
       此后 ``TRUNCATE brands`` 直接 FeatureNotSupported。
       (实测:前两条判据在自举**之前**跑,TRUNCATE 还成功 —— 也就是同一个
        fixture 在同一轮里前绿后炸,很像"某几条判据自己有问题"。)
       ``TRUNCATE ... CASCADE`` 能过,但它会连子表一起清空,超出本包该动的范围。
    """
    with diagbrand_db.cursor() as c:
        c.execute("DELETE FROM public.brands")
        yield c


# 🔴 必须在任何业务模块 import 之前生效(conftest 早于 test 模块加载)。
os.environ["DATABASE_URL"] = _url()
