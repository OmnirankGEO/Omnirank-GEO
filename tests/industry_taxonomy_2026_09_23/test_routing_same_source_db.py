# -*- coding: utf-8 -*-
"""WO_267 · 读写同源(要库:TEST_DATABASE_URL 指向用生产 schema 快照灌的测试库)。

Review 09-23 裁定:付费点亮调研(写路径 `industry_resolver`)与发布中心主榜(读路径
`industry_canonical.resolve_readonly`)对**同一原文 + 同一品牌上下文**必须判出**同一调研行**,
顺序统一为 admin 人工别名 > 行业大类字典 > LLM 别名。否则「付费跑完榜必点亮」断:
光伏客户付费数据落「新能源」,主榜却按一条旧 LLM 别名去查「装修建材」。

夹具:995 的形状(industry 列 = 建筑装饰…、品牌其它列含光伏)+ 一条**人造的旧 LLM 别名**
(原文 → 装修建材,就是投诉那一下会沉淀下来的那条)。测试行由本模块自建自删。
"""
from __future__ import annotations

import asyncio

import pytest

RAW_995 = "建筑装饰、装修和其他建筑业"
PV_BRAND = {"name": "某光伏科技", "notes": "", "seed_keywords": ["屋顶光伏"]}

ROWS = {  # 名称 → (slug, active)。slug 取自行业大类字典(连接键)。
    "装修建材": ("ind_6f1ccb372a62", True),
    "新能源": ("ind_8a68be7b3f4f", False),     # 种子脚本补的状态:停用
    "电梯行业": ("ind_5dd9ae9339e0", True),
}


def _conn():
    from db.connection import get_connection
    return get_connection()


@pytest.fixture
def env():
    from db.research_selfserve_db import _normalize_alias_text

    conn = _conn()
    cur = conn.cursor()
    slugs = [s for s, _ in ROWS.values()]
    cur.execute("SELECT id FROM geo_research_industries WHERE slug = ANY(%s) OR name = ANY(%s)",
                (slugs, list(ROWS)))
    stale = [r["id"] for r in cur.fetchall()]
    if stale:
        cur.execute("DELETE FROM geo_research_industry_aliases WHERE industry_id = ANY(%s)", (stale,))
        cur.execute("DELETE FROM geo_research_industries WHERE id = ANY(%s)", (stale,))
    ids = {}
    for name, (slug, active) in ROWS.items():
        cur.execute("INSERT INTO geo_research_industries (name, slug, sort_order, active) "
                    "VALUES (%s, %s, 999, %s) RETURNING id", (name, slug, active))
        ids[name] = cur.fetchone()["id"]
    aliases = [
        (RAW_995, ids["装修建材"], "llm"),                 # 投诉那一下沉淀下来的旧 LLM 别名
        ("某某判不出的行业xyz", ids["装修建材"], "llm"),   # 字典判不出时,LLM 别名照常生效
        ("装修公司", ids["电梯行业"], "admin"),           # 人工改判压过字典(字典会判建筑)
    ]
    keys = []
    for raw, iid, by in aliases:
        k = _normalize_alias_text(raw)
        keys.append(k)
        cur.execute("DELETE FROM geo_research_industry_aliases WHERE normalized_alias = %s", (k,))
        cur.execute("INSERT INTO geo_research_industry_aliases (normalized_alias, industry_id, confidence, resolved_by) "
                    "VALUES (%s, %s, 0.9, %s)", (k, iid, by))
    # 「装修建材」有调研数据 —— 旧 bigram 匹配器(不看品牌)会把光伏客户拉到它上面;
    #   有了这条,「恢复 bigram 回退」这发毒才有东西可咬(没数据时新旧都返回空,判据会假绿)。
    cur.execute("DELETE FROM geo_engine_stats WHERE industry = %s AND platform = %s", ("装修建材", "wo267-知乎"))
    cur.execute("INSERT INTO geo_engine_stats (industry, engine, platform, citation_count, total_queries, citation_rate) "
                "VALUES (%s, %s, %s, 3, 10, 0.3)", ("装修建材", "豆包", "wo267-知乎"))
    cur.execute("INSERT INTO geo_research_raw (industry, query, engine, cited_platform) VALUES (%s, %s, %s, %s)",
                ("装修建材", "wo267-test-query", "豆包", "wo267-知乎"))
    conn.commit()
    try:
        yield ids
    finally:
        cur.execute("DELETE FROM geo_engine_stats WHERE platform = %s", ("wo267-知乎",))
        cur.execute("DELETE FROM geo_research_raw WHERE query = %s", ("wo267-test-query",))
        cur.execute("DELETE FROM geo_research_industry_aliases WHERE normalized_alias = ANY(%s) "
                    "OR industry_id = ANY(%s)", (keys, list(ids.values())))
        cur.execute("DELETE FROM geo_research_industries WHERE slug = ANY(%s) OR id = ANY(%s)",
                    (slugs + ["ind_daf8ab762b39", "ind_7b313851174d", "ind_1444ec3386d3"], list(ids.values())))
        conn.commit()
        conn.close()


def _write(raw, brand=None, category_key=None):
    from services.research_monitor.industry_resolver import resolve_or_create_industry
    return asyncio.run(resolve_or_create_industry(raw, allow_llm=False, persist=False, taxonomy=True,
                                                  brand=brand, category_key=category_key))


def _read(raw, brand=None):
    from services.industry_canonical import resolve_readonly
    return resolve_readonly(raw, taxonomy=True, brand=brand)


# ══════════════════════════════════════════════════════════════════
# 读写同源
# ══════════════════════════════════════════════════════════════════

def test_995_shape_write_and_read_land_on_the_same_row_despite_an_old_llm_alias(env):
    w, r = _write(RAW_995, PV_BRAND), _read(RAW_995, PV_BRAND)
    assert w["industry_id"] == r.industry_id == env["新能源"], (w, r)
    assert w["industry_key"] == r.industry_key
    assert w["resolved_by"] == "taxonomy" and w["category"]["key"] == "new_energy"
    assert w["category"]["secondary_key"] == "construction"


def test_same_raw_without_brand_context_still_same_source(env):
    """不带上下文:两边都判建筑(字典)—— 压过那条旧 LLM 别名,但两边一致。"""
    w, r = _write(RAW_995), _read(RAW_995)
    assert w["industry_id"] == r.industry_id == env["装修建材"]


def test_context_free_elevator_maintenance_still_lights(env):
    """「电梯维保 → 电梯行业」这类不靠上下文的必须仍然会亮(Review Q3 要求一格)。"""
    w, r = _write("电梯维保"), _read("电梯维保")
    assert w["industry_id"] == r.industry_id == env["电梯行业"]
    assert r.merged


def test_admin_alias_beats_the_dictionary_on_both_paths(env):
    w, r = _write("装修公司"), _read("装修公司")
    assert w["industry_id"] == r.industry_id == env["电梯行业"]


def test_llm_alias_still_used_when_the_dictionary_cannot_decide(env):
    w, r = _write("某某判不出的行业xyz"), _read("某某判不出的行业xyz")
    assert w["industry_id"] == r.industry_id == env["装修建材"]


def test_user_reselected_category_wins(env):
    w = _write(RAW_995, PV_BRAND, category_key="construction")
    assert w["industry_id"] == env["装修建材"] and w["category"]["source"] == "override"


def test_callers_that_do_not_opt_in_keep_v10(env):
    """没开级 0 的调用方(包 B 的 freeze_industry 不带品牌时、不带 brand_id 的主榜)逐字不变:
    仍然先查别名 —— 所以带品牌的路径必须显式开(A 的主榜请求带 brand_id)。"""
    from services.industry_canonical import resolve_readonly
    assert resolve_readonly(RAW_995).industry_id == env["装修建材"]


def test_front_order_is_declared_once():
    from services.industry_routing import FRONT_ORDER
    assert FRONT_ORDER == ("alias_admin", "taxonomy", "alias_llm")


# ══════════════════════════════════════════════════════════════════
# 非付费入口的「尚未开通」+ 付费即点亮
# ══════════════════════════════════════════════════════════════════

def test_research_status_honest_not_open_then_open_after_lighting(env):
    from services.placement_service import PlacementService
    from services.research_monitor.industry_registry import ensure_taxonomy_research_row

    svc = PlacementService.__new__(PlacementService)
    st = svc.research_status(RAW_995, brand=PV_BRAND)
    assert st["status"] == "not_open" and st["category_key"] == "new_energy"
    # ② 不许匹到邻居:新能源没开 ⇒ 空分数、空行业名(不是装修建材)
    assert svc._match_research_industry(RAW_995, brand=PV_BRAND) == ({}, "")
    # 对照臂:同一原文不带品牌 ⇒ 字典判建筑 ⇒ 装修建材有数据就拿得到(证明上面的空是路由判对了,不是没数据)
    scores, name = svc._match_research_industry(RAW_995)
    assert name == "装修建材" and scores
    # 付费即点亮:停用行翻 active
    rid, created, activated = ensure_taxonomy_research_row("ind_8a68be7b3f4f", "新能源")
    assert (rid, created, activated) == (env["新能源"], False, True)
    assert svc.research_status(RAW_995, brand=PV_BRAND)["status"] == "open"
    # 再来一次:已 active ⇒ 什么都不改
    assert ensure_taxonomy_research_row("ind_8a68be7b3f4f", "新能源") == (env["新能源"], False, False)
    assert svc.research_status("装修建材")["status"] == "open"


def test_paid_lighting_creates_the_row_by_dictionary_slug_when_missing(env):
    from services.research_monitor.industry_registry import ensure_taxonomy_research_row, research_row_by_slug

    assert research_row_by_slug("ind_daf8ab762b39") is None
    rid, created, activated = ensure_taxonomy_research_row("ind_daf8ab762b39", "工业制造")
    assert created and not activated and rid
    row = research_row_by_slug("ind_daf8ab762b39")
    assert row["name"] == "工业制造" and row["active"] is True


def test_seed_script_inserts_only_inactive_rows_and_is_idempotent(env):
    """(d) 新补的调研行一律 active=false;重跑不改任何东西。"""
    import importlib.util
    from pathlib import Path

    from services.research_monitor.industry_registry import research_row_by_slug

    path = Path(__file__).resolve().parents[2] / "scripts" / "seed_industry_research_rows_2026_09_23.py"
    spec = importlib.util.spec_from_file_location("_wo267_seed", path)
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    assert "FALSE" in seed.INSERT_SQL and "ON CONFLICT DO NOTHING" in seed.INSERT_SQL
    assert seed.main(["--apply"]) == 0
    for slug in ("ind_daf8ab762b39", "ind_7b313851174d", "ind_1444ec3386d3"):
        assert research_row_by_slug(slug)["active"] is False, slug
    # 新能源那行 env 已建(停用)⇒ 按 slug 跳过,不翻 active
    assert research_row_by_slug("ind_8a68be7b3f4f")["active"] is False
    assert seed.main(["--apply"]) == 0
