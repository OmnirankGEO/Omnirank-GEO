"""Existing directory/facet/API contracts against isolated PG16, own rows always rolled back."""
import asyncio
import ast
import os
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from db import meijiehezi_db as db


@pytest.fixture
def market(monkeypatch):
    dsn = os.environ.get("GEO_FLOW_TEST_DSN")
    if not dsn:
        pytest.skip("explicit isolated local PG16 required")
    assert urlparse(dsn).hostname in {"127.0.0.1", "localhost"}
    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    cur = conn.cursor()
    cur.execute("SHOW server_version_num")
    assert int(cur.fetchone()["server_version_num"]) // 10000 == 16
    cur.execute("SELECT COUNT(*) AS n FROM mhz_short_video WHERE id BETWEEN 1900101 AND 1900400")
    assert cur.fetchone()["n"] == 0  # exact target range; never overwrite another fixture
    class Borrowed:
        def cursor(self):
            return conn.cursor()
        def close(self):
            pass  # functions close borrowed handle; fixture owns rollback
    monkeypatch.setattr(db, "_get_conn", lambda: Borrowed())
    serial = 1900100
    def add(**changes):
        nonlocal serial
        serial += 1
        values = dict(id=serial, media_name=f"rollback-market-{serial}", platform="抖音", industry="旅游",
                      location="rollback上海", price=2, fans_num=5000, account_auth="rollback蓝V",
                      authority_media=1, can_modify=1, can_tuwen=1, blacklist=0, is_active=True)
        values.update(changes)
        cur.execute("""INSERT INTO mhz_short_video(id,media_name,platform,industry,location,price,fans_num,
            account_auth,authority_media,can_modify,can_tuwen,blacklist,is_active)
            VALUES(%(id)s,%(media_name)s,%(platform)s,%(industry)s,%(location)s,%(price)s,%(fans_num)s,
            %(account_auth)s,%(authority_media)s,%(can_modify)s,%(can_tuwen)s,%(blacklist)s,%(is_active)s)""", values)
        return serial
    def read(**kw):
        return db.list_short_video(search="rollback-market-", platform="抖音", can_tuwen=1, **kw)
    try:
        yield add, read, cur
    finally:
        conn.rollback()
        conn.close()


def test_market_pages_full_pool_stable_same_price_ties(market):
    add, read, _ = market
    wanted = [add() for _ in range(55)]
    add(platform="微博"); add(can_tuwen=0); add(is_active=False); add(blacklist=1)
    pages = [read(page=p, limit=20, sort_by="price") for p in (1, 2, 3)]
    assert [p["total"] for p in pages] == [55, 55, 55]
    assert [r["id"] for p in pages for r in p["media"]] == wanted
    assert len({r["id"] for p in pages for r in p["media"]}) == 55


@pytest.mark.parametrize("args,expected", [
    ({"industry": "旅游"}, [0, 1]), ({"location": "rollback北京"}, [1]),
    ({"price_min": 3, "price_max": 5}, [1]), ({"fans_min": 8000, "fans_max": 12000}, [1]),
    ({"account_auth": "rollback普通"}, [2]), ({"authority_media": 0}, [2]),
    ({"can_modify": 0}, [2]),
    ({"industry": "旅游", "location": "rollback北京", "price_min": 3, "price_max": 5,
      "fans_min": 8000, "account_auth": "rollback蓝V", "authority_media": 1, "can_modify": 1}, [1]),
    ({"location": "没有这个地区"}, []),
])
def test_combined_filters_use_same_list_denominator(market, args, expected):
    add, read, _ = market
    ids = [add(), add(location="rollback北京", price=4, fans_num=10000),
           add(industry="IT科技", price=6, fans_num=15000, account_auth="rollback普通", authority_media=0, can_modify=0)]
    result = read(**args)
    assert result["total"] == len(expected)
    assert [r["id"] for r in result["media"]] == [ids[i] for i in expected]
    assert read()["total"] == 3  # clearing conditions restores full scope


def test_facets_use_content_platform_pool_and_l1_matches_list(market):
    add, read, _ = market
    add(); add(industry="IT科技")
    add(can_tuwen=0, location="rollback视频地区", account_auth="rollback视频认证", industry="汽车交通")
    add(platform="微博", location="rollback微博地区", account_auth="rollback微博认证", industry="汽车交通")
    add(blacklist=1, location="rollback黑名单地区", account_auth="rollback黑名单认证", industry="汽车交通")
    facets = db.get_short_video_filters(search="rollback-market-", platform="抖音", can_tuwen=1)
    for word in ("rollback视频地区", "rollback微博地区", "rollback黑名单地区"):
        assert word not in facets["locations"]
    for word in ("rollback视频认证", "rollback微博认证", "rollback黑名单认证"):
        assert word not in facets["account_auths"]
    assert facets["platforms"] == ["抖音"]
    assert {i["key"]: i["count"] for i in facets["industries"]} == {"科技数码": 1, "旅游": 1}
    for facet in facets["industries"]:
        assert read(industry=facet["key"])["total"] == facet["count"]


def test_facets_combination_and_legacy_video_defaults(market):
    add, read, _ = market
    add(); add(price=4); add(can_tuwen=0, industry="汽车交通")
    scoped = db.get_short_video_filters(search="rollback-market-", platform="抖音", can_tuwen=1, price_min=3)
    assert scoped["industries"] == [{"key": "旅游", "count": 1}]
    assert read(price_min=3)["total"] == 1
    legacy = db.list_short_video(search="rollback-market-", platform="抖音")
    assert legacy["total"] == 3
    assert "汽车" in {f["key"] for f in db.get_short_video_filters(search="rollback-market-")["industries"]}


def test_sort_and_search_are_database_behaviors(market):
    add, _, _ = market
    a = add(media_name="rollback-market-目标甲", price=2, fans_num=100)
    b = add(media_name="rollback-market-目标乙", price=5, fans_num=200)
    add()
    result = db.list_short_video(search="rollback-market-目标", platform="抖音", can_tuwen=1, sort_by="price", sort_dir="desc")
    assert result["total"] == 2 and [r["id"] for r in result["media"]] == [b, a]
    assert [r["id"] for r in db.list_short_video(search="rollback-market-目标", sort_by="fans_num", sort_dir="desc")["media"]] == [b, a]


def test_existing_api_points_and_can_tuwen_reach_same_pg_pool(market, monkeypatch):
    # Execute the actual two endpoint functions, not a reimplementation. Importing the
    # whole app bootstraps migrations; that is outside a rollback-only directory test.
    # The root task separately verifies the complete running HTTP stack.
    from services.media_price_projection import points_to_yuan, project_rows, public_sort_key
    tree = ast.parse(Path("api/meijiehezi_api.py").read_text(encoding="utf-8-sig"))
    functions = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                 and node.name in {"api_list_short_video", "api_short_video_filters"}]
    assert len(functions) == 2
    for fn in functions:
        fn.decorator_list = []
    def project(result, markup):
        project_rows(result["media"], markup)
        return result
    context = dict(Request=object, _get_user=lambda _: {"id": 3}, _get_publish_markup=lambda: 1.5,
                   _points_to_yuan=points_to_yuan, _public_sort_key=public_sort_key,
                   _project_catalog=project, list_short_video=db.list_short_video,
                   get_short_video_filters=db.get_short_video_filters)
    exec(compile(ast.Module(body=functions, type_ignores=[]), "actual-directory-handlers", "exec"), context)
    add, _, _ = market
    chosen = add(price=2)
    add(price=4); add(can_tuwen=0, price=2, industry="汽车交通")
    response = asyncio.run(context["api_list_short_video"](None, search="rollback-market-", platform="抖音", can_tuwen=1, points_min=390, points_max=390))
    assert response["total"] == 1 and response["media"][0]["id"] == chosen
    assert response["media"][0]["price_points"] == 390 and "price" not in response["media"][0]
    facets = asyncio.run(context["api_short_video_filters"](None, search="rollback-market-", platform="抖音", can_tuwen=1, points_min=390, points_max=390))
    assert facets["industries"] == [{"key": "旅游", "count": 1}]
