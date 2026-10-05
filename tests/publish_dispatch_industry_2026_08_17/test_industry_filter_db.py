"""行业筛选的**真库**行为锁 —— 归一化在 SQL 这一层真的生效了吗。

WO_PUBLISH_DISPATCH_STATUS_AND_INDUSTRY_2026-08-17 Part② R1/R2。

`test_media_industry_taxonomy.py` 锁的是纯函数(映射本身对不对);
本文件锁的是**接线**:`list_wemedia(industry="家居")` 会不会真把
`房产家居` / `家居家装/综合` 这些原始串的媒体一起查出来,以及 facet 计数
是不是跟着当前平台筛选走。本仓有过「函数改对了但没接上,整包惰性」的前科,
所以这两层分开锁。

夹具行数刻意取自生产真形态(斜杠组合 + 单值词混排),不是三条理想数据。
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PG_URL = os.environ.get("TEST_DATABASE_URL")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
SCHEMA_SQL = Path(__file__).resolve().parent / "prod_schema_2026-08-17.sql"

# 生产真形态:同一个「家居」概念散在 4 种写法里,而且分属两个平台。
WEMEDIA_ROWS = [
    # (id, toutiao_name, platform, industry, province, price)
    (910001, "家居号甲", "今日头条", "家居", "广东", 10.0),
    (910002, "家居号乙", "今日头条", "房产家居", "广东", 12.0),
    (910003, "家居号丙", "百家号", "家居家装/综合", "北京", 14.0),
    (910004, "家居号丁", "百家号", "房产", "北京", 16.0),
    (910005, "萌宠号甲", "今日头条", "萌宠/测评", "广东", 8.0),
    (910006, "汽车号甲", "百家号", "汽车/生活/综合", "上海", 20.0),
    (910007, "无行业号", "百家号", "", "上海", 20.0),
]


def _skip_unless_throwaway_pg():
    if not PG_URL:
        pytest.skip("需要 TEST_DATABASE_URL(一次性 loopback 测试库)")
    parsed = urlsplit(PG_URL)
    if (parsed.hostname or "").lower() not in LOOPBACK_HOSTS:
        pytest.skip(f"只允许 loopback 一次性容器 DSN:{parsed.hostname}")
    base_db = (parsed.path or "").lstrip("/").lower()
    if "prod" in base_db or "test" not in base_db:
        pytest.skip(f"基础库名必须含 test 且不含 prod:{base_db}")


@pytest.fixture(scope="module")
def db():
    _skip_unless_throwaway_pg()
    assert SCHEMA_SQL.exists(), f"缺 schema 快照:{SCHEMA_SQL}"
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor

    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    name = f"pubind_test_{uuid.uuid4().hex[:10]}"
    assert "test" in name and "prod" not in name
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    parsed = urlsplit(PG_URL)
    url = urlunsplit((parsed.scheme, parsed.netloc, f"/{name}", parsed.query, parsed.fragment))

    raw = SCHEMA_SQL.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in raw.splitlines()
        if not line.startswith("\\") and not line.startswith("CREATE SCHEMA public;")
    )
    conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
    conn.autocommit = True
    with conn.cursor() as cur:
        for _ext in ("vector", "pg_trgm"):
            cur.execute(f"CREATE EXTENSION IF NOT EXISTS {_ext}")
        cur.execute(body)
        cur.execute("SET search_path TO public")
        cur.execute("DELETE FROM mhz_wemedia")
        for mid, nm, plat, ind, prov, price in WEMEDIA_ROWS:
            cur.execute(
                "INSERT INTO mhz_wemedia (id, toutiao_name, platform, industry, province, "
                "price, is_active, hidden_by_dedupe) VALUES (%s,%s,%s,%s,%s,%s,TRUE,FALSE)",
                (mid, nm, plat, ind, prov, price))
    try:
        yield type("DB", (), {
            "url": url,
            "connect": staticmethod(lambda: psycopg2.connect(url, cursor_factory=RealDictCursor)),
            "conn": conn,
        })
    finally:
        conn.close()
        with admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        admin.close()


@pytest.fixture
def mdb(db, monkeypatch):
    import db.meijiehezi_db as m
    monkeypatch.setattr(m, "_get_conn", db.connect)
    return m


def test_E1_selecting_l1_returns_all_member_media(mdb):
    """选「家居」→ 4 种写法的媒体一个不少(旧口径只能命中逐字相同的那一条)。"""
    res = mdb.list_wemedia(industry="家居", limit=50)
    got = {r["id"] for r in res["media"]}
    assert got == {910001, 910002, 910003, 910004}, f"实际拿到 {sorted(got)}"
    assert res["total"] == 4
    # 反向对照:旧口径的逐字串仍然照旧只命中它自己(老链接不被改坏)
    legacy = mdb.list_wemedia(industry="家居家装/综合", limit=50)
    assert {r["id"] for r in legacy["media"]} == {910003}


def test_E1b_multi_component_media_is_reachable_from_every_bucket(mdb):
    """`汽车/生活/综合` 从三个大类都点得到 —— 归纳不能把媒体归丢。"""
    for key in ("汽车", "生活", "综合"):
        got = {r["id"] for r in mdb.list_wemedia(industry=key, limit=50)["media"]}
        assert 910006 in got, f"大类「{key}」点不到 910006"


def test_E2_facets_follow_the_current_platform_filter(mdb):
    """切平台后 chip 重算:百家号下没有萌宠 → 该 chip 不下发(成对:今日头条下有)。"""
    bjh = {c["key"]: c["count"] for c in mdb.get_wemedia_filters(platform="百家号")["industries"]}
    jrtt = {c["key"]: c["count"] for c in mdb.get_wemedia_filters(platform="今日头条")["industries"]}
    assert "萌宠" not in bjh, "百家号下 0 家的「萌宠」被渲染了"
    assert jrtt.get("萌宠") == 1, "今日头条下有 1 家萌宠,chip 却不见了(那就是恒隐藏)"
    # 家居:百家号 2 家(910003/910004)· 今日头条 2 家(910001/910002)
    assert bjh.get("家居") == 2 and jrtt.get("家居") == 2
    # 数量必须与点进去看到的条数一致 —— 这是「计数不说谎」的判据
    for plat, table in (("百家号", bjh), ("今日头条", jrtt)):
        for key, n in table.items():
            real = mdb.list_wemedia(platform=plat, industry=key, limit=50)["total"]
            assert real == n, f"{plat}/{key}: chip 说 {n},列表实际 {real}"


def test_E3_facet_excludes_industry_itself(mdb):
    """facet 分母不含 industry —— 否则选中一个大类后其余全变 0,换不回去。"""
    keys = {c["key"] for c in mdb.get_wemedia_filters()["industries"]}
    assert {"家居", "萌宠", "汽车"} <= keys


def test_E4_empty_industry_media_occupies_no_chip(mdb):
    """没填行业的那条(910007)不该被算进任何 chip,也不该凭空多出一个大类。"""
    total_in_chips = sum(c["count"] for c in mdb.get_wemedia_filters()["industries"])
    # 逐条归属:910001 家居=1 · 910002 家居=1 · 910003 家居+综合=2 · 910004 家居=1
    #          910005 萌宠+生活=2 · 910006 汽车+生活+综合=3 · 910007 空行业=0 → 合计 10
    assert total_in_chips == 10, f"chip 计数总和 {total_in_chips},与逐条归属不符"
    # 反向对照:空行业那条真的一个大类都不属于(不是「碰巧数字对上」)
    assert 910007 not in {
        r["id"] for key in ("家居", "萌宠", "汽车", "生活", "综合", "其他")
        for r in mdb.list_wemedia(industry=key, limit=50)["media"]
    }


def test_E5_unknown_l1_key_yields_empty_not_everything(mdb):
    """手工拼 URL 传一个当前 0 家的大类 → 空列表,而不是「筛选被忽略」返回全部。"""
    res = mdb.list_wemedia(platform="百家号", industry="萌宠", limit=50)
    assert res["total"] == 0
    # 成对:同一平台下有家的大类必须非空(证明上一条不是恒空)
    assert mdb.list_wemedia(platform="百家号", industry="家居", limit=50)["total"] == 2
