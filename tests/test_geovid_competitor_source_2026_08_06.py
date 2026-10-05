# -*- coding: utf-8 -*-
"""竞品取数锁 · WO_GEO_DOUYIN_RANKING_TEMPLATES_2026-08-06 v3 §6.1

两块:
  ① `ranking_source.fetch_ranking_candidates` —— 榜单候选走 answer_entities
  ② `topic_distiller.load_competitors` —— 蒸馏侧四个缺陷(keywords 收窄 /
     客户自我排除 / 时效闸 / 质量闸)
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib

import pytest

from services.geo_douyin import ranking_source as rsrc
from services.geo_douyin import topic_distiller as td


REPO = pathlib.Path(__file__).resolve().parent.parent
SRC_SOURCE = REPO / "services" / "geo_douyin" / "ranking_source.py"
SRC_DISTILL = REPO / "services" / "geo_douyin" / "topic_distiller.py"


# ===========================================================================
# ① 榜单候选源
# ===========================================================================

def test_candidates_come_from_answer_entities_not_keyword_insights():
    """榜单候选必须读行业级公共表,不读只覆盖 18 个品牌的客户私有表。"""
    src = SRC_SOURCE.read_text(encoding="utf-8")
    assert "geo_research_answer_entities" in src
    code = ast.parse(src)
    literals = [n.value for n in ast.walk(code)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    sql_blobs = [s for s in literals if "SELECT" in s.upper()]
    assert sql_blobs, "没找到 SQL"
    for blob in sql_blobs:
        assert "keyword_insights" not in blob, "榜单候选不该读 keyword_insights"


def test_candidate_query_has_a_time_window():
    src = SRC_SOURCE.read_text(encoding="utf-8")
    assert "days')::interval" in src and "created_at >=" in src


def test_candidate_carries_full_provenance_snapshot():
    """举证链五要素必须在 source 里**自带**,不是指针。"""
    src = SRC_SOURCE.read_text(encoding="utf-8")
    i = src.find('"source": {')
    assert i > 0, "没有 source 快照块"
    block = src[i: i + 900]
    for key in ("engine", "recommendation_rank", "extractor_version",
                "llm_model", "observed_at", "entity_row_ids"):
        assert f'"{key}"' in block, f"举证链缺 {key}"


def test_fetch_never_raises_and_returns_list_on_db_error(monkeypatch):
    """🔴 取不到候选**不许抛** —— 永不中断铁律,上层按 R5 降级。"""
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr("db.connection.get_connection", boom, raising=False)
    out = rsrc.fetch_ranking_candidates("电梯行业")
    assert out == []


def test_empty_industry_short_circuits():
    assert rsrc.fetch_ranking_candidates("") == []
    assert rsrc.fetch_ranking_candidates("   ") == []


def test_ranking_side_does_not_exclude_the_client_by_default():
    """🔴 反向对照:榜单侧**默认不排除客户** —— 有据分支要让客户进榜给实名次(D12④)。

    形态判据:`exclude_names` 默认必须是 None/空,不能默认塞客户名。
    """
    sig = inspect.signature(rsrc.fetch_ranking_candidates)
    assert sig.parameters["exclude_names"].default is None


# ===========================================================================
# ② 蒸馏侧 load_competitors 四缺陷
# ===========================================================================

def _load_competitors_sql() -> str:
    tree = ast.parse(SRC_DISTILL.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "load_competitors")
    blobs = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            blobs.append(node.value)
        elif isinstance(node, ast.JoinedStr):          # f-string
            blobs.append("".join(v.value for v in node.values
                                 if isinstance(v, ast.Constant)))
    return "\n".join(b for b in blobs if "SELECT" in b.upper() or "AND " in b)


def test_keywords_param_is_actually_used():
    """🔴 `keywords` 形参此前在函数体里从未被引用 —— 竞品名单与打哪个词无关。"""
    tree = ast.parse(SRC_DISTILL.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "load_competitors")
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert "keywords" in names, "keywords 形参仍然是摆设"
    assert "keyword = ANY" in _load_competitors_sql(), "SQL 没有按词收窄"


def test_time_window_gate_present():
    assert "distilled_at >=" in _load_competitors_sql()
    assert td.COMPETITOR_WINDOW_DAYS > 0


def test_quality_gate_present_and_excludes_rejected_and_degraded():
    sql = _load_competitors_sql()
    assert "quality_flag = ANY" in sql
    assert "rejected" not in td.COMPETITOR_QUALITY_FLAGS
    assert "degraded" not in td.COMPETITOR_QUALITY_FLAGS
    # 反向对照:正常值必须还在,否则这条闸把所有数据都挡掉了
    assert "auto" in td.COMPETITOR_QUALITY_FLAGS


def test_client_self_exclusion_is_wired_at_the_call_site():
    """自我排除得真的接上 —— 函数支持而调用点不传等于没做。"""
    tree = ast.parse(SRC_DISTILL.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "load_competitors"]
    assert calls, "找不到调用点"
    assert any(k.arg == "client_brand" for c in calls for k in c.keywords), \
        "调用点没传 client_brand —— 自我排除没接线"


def test_self_exclusion_matches_by_merge_key_not_exact_string():
    """`深圳市X有限公司` 与 `X` 是同一家 —— 精确串比会漏掉。"""
    tree = ast.parse(SRC_DISTILL.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "load_competitors")
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert "safe_merge_key" in names, "自我排除用的是裸字符串比较,会漏"


def test_load_competitors_never_raises(monkeypatch):
    """反向对照:读失败返回空列表,不抛(竞品取不到不该挡住蒸馏)。"""
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr("db.connection.get_connection", boom, raising=False)
    out = asyncio.run(td.load_competitors(1, ["kw"], client_brand="X"))
    assert out == []
