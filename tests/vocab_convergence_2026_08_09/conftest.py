"""词表收敛(地域归一 + 价格档拆列)· 真库 fixture。

🔴 为什么必须真库,不能用假 cursor:
   本单的核心风险是「加了列但 INSERT 没带上」/「带上了但列不存在」——
   假 cursor 对这两种错**全都照绿**(它不校验列名)。只有真 Postgres 会在
   列不存在时抛 UndefinedColumn、在 CHECK 违反时抛 CheckViolation。
   本仓已有五例「接线没接」,其中四例是被假替身放过去的。

schema 只建被测路径真读写的列(列名/类型抄自 2026-08-09 生产 information_schema 实测)。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")

MIGRATION = ROOT / "db" / "migration_031_media_listing_slot_2026_08_09.sql"
ROLLBACK = ROOT / "db" / "rollback_031_media_listing_slot_2026_08_09.sql"


def _assert_safe() -> None:
    if not TEST_DATABASE_URL:
        raise RuntimeError("TEST_DATABASE_URL required")
    name = TEST_DATABASE_URL.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if "test" not in name or "prod" in name:
        raise RuntimeError(f"unsafe test db name: {name!r}")


# 最小源 schema · 列名与 data_type 逐列抄自 2026-08-09 生产 information_schema 实测
#
# 🔴 第一版我把 avg_time / news_resource / entrance_level / weekend_publish /
#    authority_media / geo_rank / special_industry 全建成 TEXT,而生产是 **integer**;
#    价格列生产是 numeric 不是 real。在副本(真 schema)上跑端到端探针时当场
#    `InvalidTextRepresentation: invalid input syntax for type integer: ""` —— 锁在
#    类型不同构的 schema 上全绿,证明不了真库能写进去。这是 SQL 4 维核验的第 2 维。
BASE_SCHEMA = r"""
CREATE TABLE mhz_media (
    id                 INTEGER PRIMARY KEY,
    media_name         TEXT,
    category           TEXT,
    media_type         TEXT DEFAULT 'media',
    platform           TEXT,
    price_normal       NUMERIC, price_vip NUMERIC, price_svip NUMERIC,
    our_price_yuan     NUMERIC, our_price_points BIGINT,
    inclusion_rate     TEXT, avg_publish_time TEXT, publish_rate TEXT,
    pc_weight          INTEGER DEFAULT 0, mobile_weight INTEGER DEFAULT 0,
    news_source        TEXT, link_type TEXT, can_geo INTEGER DEFAULT 0,
    case_link          TEXT, remark TEXT,
    is_active          BOOLEAN DEFAULT TRUE,
    synced_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resource_type      TEXT DEFAULT '',
    resource_type_name TEXT DEFAULT '',
    avg_time           INTEGER DEFAULT 0,
    news_resource      INTEGER DEFAULT 0,
    entrance_level     INTEGER DEFAULT 0,
    entrance_link      TEXT DEFAULT '',
    portal_media       TEXT DEFAULT '',
    price1             NUMERIC DEFAULT 0, price2 NUMERIC DEFAULT 0,
    geo_rank           INTEGER DEFAULT 0,
    geo_rank_platform  TEXT DEFAULT '',
    weekend_publish    INTEGER DEFAULT 0,
    authority_media    INTEGER DEFAULT 0,
    special_industry   INTEGER DEFAULT 0,
    price              NUMERIC DEFAULT 0,
    area               TEXT DEFAULT '',
    m_weight           INTEGER DEFAULT 0
);
CREATE TABLE mhz_wemedia (
    id            INTEGER PRIMARY KEY,
    toutiao_name  TEXT DEFAULT '', platform TEXT DEFAULT '', industry TEXT DEFAULT '',
    province      TEXT DEFAULT '',
    fans_num      INTEGER DEFAULT 0, read_num INTEGER DEFAULT 0,
    price NUMERIC DEFAULT 0, price1 NUMERIC DEFAULT 0, price2 NUMERIC DEFAULT 0,
    video_price NUMERIC DEFAULT 0, weitoutiao_price NUMERIC DEFAULT 0,
    case_link TEXT DEFAULT '', entrance_link TEXT DEFAULT '', remark TEXT DEFAULT '',
    avg_time INTEGER DEFAULT 0, p_rate TEXT DEFAULT '',
    geo_rank INTEGER DEFAULT 0, geo_rank_platform TEXT DEFAULT '',
    quota INTEGER DEFAULT 0, authority_media INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE, synced_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE mhz_config (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE system_config (key TEXT PRIMARY KEY, value TEXT);
"""


def _exec_script(cur, sql: str) -> None:
    cur.execute(sql)


@pytest.fixture(scope="session")
def db_url() -> str:
    _assert_safe()
    return TEST_DATABASE_URL


@pytest.fixture()
def conn(db_url):
    """每个用例一套干净表(建表 → 跑 031 迁移 → 用例 → 删表)。"""
    # 🔴 必须与生产同构:db/connection.py 全线用 RealDictCursor,行是 dict-like。
    #    用默认 tuple cursor 会让 `r["col"]` 这类真实代码在测试里报 TypeError —— 那是
    #    测试环境与生产不同构造成的假红,反过来也可能造出假绿。
    c = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS mhz_media, mhz_wemedia, mhz_config, system_config CASCADE")
        _exec_script(cur, BASE_SCHEMA)
        _exec_script(cur, MIGRATION.read_text(encoding="utf-8"))
    yield c
    c.close()


@pytest.fixture()
def bare_conn(db_url):
    """**没跑迁移**的库 —— 用于证明"迁移漏跑时是响亮失败,不是静默"。"""
    # 🔴 必须与生产同构:db/connection.py 全线用 RealDictCursor,行是 dict-like。
    #    用默认 tuple cursor 会让 `r["col"]` 这类真实代码在测试里报 TypeError —— 那是
    #    测试环境与生产不同构造成的假红,反过来也可能造出假绿。
    c = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS mhz_media, mhz_wemedia, mhz_config, system_config CASCADE")
        _exec_script(cur, BASE_SCHEMA)
    yield c
    c.close()


class NonClosingConn:
    """挡住 close() 的连接代理。

    被测函数(publish_db.upsert_media 等)自己 finally: conn.close(),而 psycopg2 的
    close 是只读属性 monkeypatch 不掉 —— 用代理挡住,否则断言时连接已断。
    其余属性一律透传,不改变被测路径的任何行为。
    """

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.fixture()
def noclose(conn):
    """返回挡住 close 的代理(同一个底层连接)。"""
    return NonClosingConn(conn)


@pytest.fixture()
def media_row():
    """一条完整的 mhz_media 上游行(_upsert_media 的命名参数全集)。"""
    def _make(**over):
        row = {
            # 🔴 值的类型跟着生产 data_type 走:avg_time/news_resource/entrance_level/
            #    weekend_publish/authority_media/special_industry/geo_rank 都是 integer,
            #    传 "是"/"" 会在真库上 InvalidTextRepresentation(副本实测)。
            "id": 1, "media_name": "某某网", "price": 100.0, "price1": 90.0, "price2": 80.0,
            "area": "综合全国", "portal_media": "门户",
            "resource_type_name": "新闻资讯", "resource_type": "",
            "inclusion_rate": "90%", "publish_rate": "95%", "avg_time": 1,
            "pc_weight": 5, "m_weight": 5, "news_resource": 1, "link_type": "锚文本",
            "remark": "", "case_link": "", "geo_rank": 0, "geo_rank_platform": "",
            "entrance_level": 0, "entrance_link": "", "weekend_publish": 1,
            "authority_media": 0, "special_industry": 0,
        }
        row.update(over)
        return row
    return _make


@pytest.fixture()
def wemedia_row():
    def _make(**over):
        row = {
            "id": 1, "toutiao_name": "某某号", "platform": "头条", "industry": "综合",
            "province": "综合全国", "fans_num": 1000, "read_num": 5000,
            "price": 50.0, "price1": 40.0, "price2": 30.0,
            "video_price": 0.0, "weitoutiao_price": 0.0,
            "case_link": "", "entrance_link": "", "remark": "", "avg_time": 1,
            "p_rate": "90%", "geo_rank": 0, "geo_rank_platform": "",
            "quota": 0, "authority_media": 0,
        }
        row.update(over)
        return row
    return _make
