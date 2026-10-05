"""#150 §3.2 返修二 · 打**真 SQL**,不再把承重的东西桩掉。

## 我连着两版栽在同一个地方

返修一补了「API 出口」的行为臂,但那组把 `resolve_quote_for_confirmed_keyword`
**monkeypatch 掉了** —— 于是:

  · 把归属谓词里的 `AND q.brand_id = %s` 换成 `AND %s IS NOT NULL`
    (跨品牌全放行)⇒ **21 条全绿**;
  · 把 `create_post` 的 INSERT 里 `confirmed_keyword_id` 那一列删掉
    (只留同名参数)⇒ **21 条全绿** ——
    结构臂是 `"confirmed_keyword_id" in ast.unparse(fn)`,**参数名就能喂饱它**,
    而行为臂又把 `create_post` 整个桩掉了。

🔴 同一个病的第三种形态:**我把要验的东西打了桩,然后去锁它的调用方**。
   前两次是「锁钉在抽出来的 helper 上、调用点没人守」,这次是反过来 ——
   调用点守住了,被调用的那段真逻辑一次都没跑。

⇒ 本文件只做一件事:让**真 SQL 在真 schema 上跑**。
   桩只打到 `get_connection`(把连接指向一次性库),
   谓词与 INSERT 本身**原样执行**。
"""

from __future__ import annotations

import os
import pathlib
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import RealDictCursor  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
MIGRATION = REPO / "db" / "migration_055_geo_douyin_post_quote_binding_2026_09_08.sql"
PROD_SCHEMA = pathlib.Path(os.getenv(
    "GDQ_PROD_SCHEMA_SQL", r"C:/AI-Test/.deploy_toolkit/prod_schema_2026-09-05.sql"))
DSN = os.getenv("TEST_DATABASE_URL")

BRAND_X, BRAND_Y = 90001, 90002


@pytest.fixture(scope="module")
def live(request):
    """一次性库:生产 dump + 本迁移 + 几行夹具。

    ⚠️ skip **不是通过** —— 这一组是「真 SQL 真的跑过」的唯一保障。
    """
    if not DSN or not PROD_SCHEMA.is_file():
        pytest.skip("需要 TEST_DATABASE_URL 与生产 schema dump(缺则本组未验证)")
    name = "gdq_live_%s" % uuid.uuid4().hex[:8]
    root = DSN.rsplit("/", 1)[0]
    admin = psycopg2.connect(root + "/postgres")
    admin.autocommit = True
    admin.cursor().execute('CREATE DATABASE "%s"' % name)
    admin.close()
    dsn = root + "/" + name

    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    conn.autocommit = True
    cur = conn.cursor()
    for role in ("ai_ops_runner", "geo_readonly"):
        cur.execute("DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=%s)"
                    " THEN EXECUTE format('CREATE ROLE %%I', %s); END IF; END$$",
                    (role, role))
    cur.execute("\n".join(
        l for l in PROD_SCHEMA.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not l.startswith("\\restrict") and not l.startswith("\\unrestrict")))
    cur.execute("SET search_path = public")
    cur.execute(MIGRATION.read_text(encoding="utf-8"))

    # 🔴 真 schema 有真外键(`quotes.brand_id → brands`)—— 品牌行必须先造。
    #    这正是打真库的价值:夹具库上没有这条 FK,造不出这个约束,
    #    也就发现不了「品牌不存在时报价根本插不进去」。
    for bid, nm in ((BRAND_X, "测试品牌 X"), (BRAND_Y, "测试品牌 Y")):
        cur.execute("INSERT INTO brands (id, name) VALUES (%s,%s)"
                    " ON CONFLICT (id) DO NOTHING", (bid, nm))

    # `confirmed_keywords.monitoring_product_version` 是 NOT NULL + 默认值,
    # 而那个默认值本身有外键指向产品矩阵表 —— 默认值指向一行**必须存在**的数据。
    # 这类"默认值带外键"的组合在夹具库里根本不存在,只有打真 schema 才会撞见。
    cur.execute("SELECT column_default FROM information_schema.columns"
                " WHERE table_name='confirmed_keywords'"
                " AND column_name='monitoring_product_version'")
    _default = (cur.fetchone() or {}).get("column_default") or ""
    _version = _default.split("'")[1] if "'" in _default else "monitoring-unified5-v1"
    cur.execute("INSERT INTO monitoring_product_platform_matrices (version, platforms)"
                " VALUES (%s, %s) ON CONFLICT DO NOTHING", (_version, "[]"))

    # 夹具:两个品牌各一张报价 + 各一个确认词;再加一张 draft 报价。
    ids = {}
    for key, brand, status in (("x", BRAND_X, "confirmed"),
                               ("y", BRAND_Y, "confirmed"),
                               ("draft", BRAND_X, "draft"),
                               ("paid", BRAND_X, "paid")):
        cur.execute("INSERT INTO quotes (brand_id, status) VALUES (%s,%s) RETURNING id",
                    (brand, status))
        qid = cur.fetchone()["id"]
        cur.execute("INSERT INTO confirmed_keywords (quote_id, keyword)"
                    " VALUES (%s,%s) RETURNING id", (qid, "深圳 GEO 优化"))
        ids[key] = {"quote_id": qid, "ck_id": cur.fetchone()["id"]}
    conn.close()
    try:
        yield dsn, ids
    finally:
        admin = psycopg2.connect(root + "/postgres")
        admin.autocommit = True
        admin.cursor().execute('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % name)
        admin.close()


@pytest.fixture
def wired(live, monkeypatch):
    """只把 `get_connection` 指向一次性库 —— **谓词与 INSERT 原样跑**。"""
    dsn, ids = live
    import db.connection as dbc
    import db.geo_douyin_db as gddb

    def _conn(*a, **k):
        return psycopg2.connect(dsn, cursor_factory=RealDictCursor)

    # 🔴 **在名字被查找的地方打桩**。`db/geo_douyin_db.py` 在模块顶层
    #    `from db.connection import get_connection` —— 它持的是自己的引用,
    #    只打 `db.connection` 那个名字对它无效。
    #    ⚠️ 这个洞**单跑时看不见**:单跑本文件时 geo_douyin_db 还没被导入,
    #       补丁碰巧在它 import 之前生效;整包跑(别的用例先导了它)才暴露。
    #       「单跑绿、合跑红」几乎总是这类绑定问题,不是被测对象的问题。
    monkeypatch.setattr(dbc, "get_connection", _conn)
    monkeypatch.setattr(gddb, "get_connection", _conn, raising=False)
    return ids


# ══════════════════ (a) 归属谓词:真 SQL

def _resolve(ck_id, brand_id):
    import asyncio

    from services.geo_douyin.topic_distiller import (
        resolve_quote_for_confirmed_keyword)
    return asyncio.run(resolve_quote_for_confirmed_keyword(
        int(ck_id), brand_id=int(brand_id)))


def test_a_keyword_of_another_brand_resolves_to_none(wired):
    """🔴 Review 那发毒的靶心:品牌 X 的词用品牌 Y 去查 ⇒ None。

    毒:把 `AND q.brand_id = %s` 换成 `AND %s IS NOT NULL`(跨品牌全放行)
    ⇒ 本条红。上一版这条谓词**一次都没跑过**(被 monkeypatch 掉了)。
    """
    ids = wired
    assert _resolve(ids["x"]["ck_id"], BRAND_Y) is None, (
        "品牌 Y 查到了品牌 X 的词 —— 记账会跨租户串")


def test_its_own_confirmed_keyword_resolves_to_the_quote(wired):
    """正样本臂:自己的词、报价 confirmed ⇒ 返回报价号。

    只证「别人的查不到」不够 —— 一个永远返 None 的实现同样能让上一条变绿,
    而那会让谁都下不了单。
    """
    ids = wired
    assert _resolve(ids["x"]["ck_id"], BRAND_X) == ids["x"]["quote_id"]


def test_a_paid_quote_also_resolves(wired):
    """`paid` 与 `confirmed` 同闸 —— 与 `load_purchased_keywords` 一份口径。"""
    ids = wired
    assert _resolve(ids["paid"]["ck_id"], BRAND_X) == ids["paid"]["quote_id"]


def test_a_quote_in_any_other_status_does_not_resolve(wired):
    """🔴 状态闸:报价还没确认(draft)⇒ None。

    没有这条,客户还没确认的报价就能被拿来记账。
    """
    ids = wired
    assert _resolve(ids["draft"]["ck_id"], BRAND_X) is None


def test_a_nonexistent_keyword_resolves_to_none(wired):
    """不存在的词身份 ⇒ None(前端传了脏值也不该记出一个报价号)。"""
    assert _resolve(999_999_99, BRAND_X) is None


# ══════════════════ (b) 落库:真 INSERT

def test_create_post_really_persists_both_columns(wired):
    """🔴 Review 第二发毒的靶心:两列**真的进了 INSERT**。

    毒:把 `confirmed_keyword_id` 从 INSERT 的列清单里删掉(只留同名参数)
    ⇒ 本条红。上一版的结构臂是 `"confirmed_keyword_id" in ast.unparse(fn)` ——
    **参数名就能喂饱它**;而行为臂又把 `create_post` 整个桩掉了。
    """
    ids = wired
    from db.geo_douyin_db import create_post

    post_id = create_post(created_by=7, brand_id=BRAND_X, keyword="深圳 GEO 优化",
                          quote_id=ids["x"]["quote_id"],
                          confirmed_keyword_id=ids["x"]["ck_id"])
    import db.connection as dbc
    conn = dbc.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT quote_id, confirmed_keyword_id FROM geo_douyin_posts"
                    " WHERE id = %s", (post_id,))
        row = dict(cur.fetchone())
    finally:
        conn.close()
    assert row["confirmed_keyword_id"] == ids["x"]["ck_id"], (
        "词身份没落库 —— 记账挂不上,而下单看起来完全正常:%r" % row)
    assert row["quote_id"] == ids["x"]["quote_id"], row


def test_a_manual_keyword_persists_nulls(wired):
    """反向臂:手填词(不传两列)⇒ 落库两列为 NULL,不是被填了个默认值。

    没有这条,一个"缺省填 0"的实现会让手填词凭空挂到 quote 0 上。
    """
    from db.geo_douyin_db import create_post

    post_id = create_post(created_by=7, brand_id=BRAND_X, keyword="我自己想的词")
    import db.connection as dbc
    conn = dbc.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT quote_id, confirmed_keyword_id FROM geo_douyin_posts"
                    " WHERE id = %s", (post_id,))
        row = dict(cur.fetchone())
    finally:
        conn.close()
    assert row["quote_id"] is None and row["confirmed_keyword_id"] is None, row
