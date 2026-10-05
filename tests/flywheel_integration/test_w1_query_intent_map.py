"""W1 语料标签层 · 纯逻辑单测(DB-free,随处可跑)。

覆盖:
  - intent_style_map: 8×(6+空) 组合全部指向真实 style_code;稳定/幂等;人工可改。
  - query_intent_hash: strip/lower 归一稳定、industry 隔离。
  - query_intent 标签复用 article intent 8 类(SSOT,不造第 2 份口径)。
  - OSS 回读上限参数化:cap=0 不回读、默认值保持 dashboard 40 字节一致。
"""
from __future__ import annotations

import pytest

from writing.intent_style_map import (
    ALL_INTENT_TYPES,
    ALL_STYLE_FAMILIES,
    describe_mapping,
    resolve_style_code,
)
from writing.style_registry import WRITING_STYLES


def test_all_mapping_targets_are_real_style_codes():
    valid = set(WRITING_STYLES.keys())
    for row in describe_mapping():
        assert row["style_code"] in valid, f"映射到不存在的文体: {row}"


def test_resolver_is_total_and_deterministic():
    # 未知 intent + 未知 family → 全局兜底,永不抛
    assert resolve_style_code("nonsense", "nonsense") in WRITING_STYLES
    # 幂等
    assert resolve_style_code("ranking") == resolve_style_code("RANKING", "")
    assert resolve_style_code("  ranking  ") == "ranking_v2"


def test_family_override_reaches_otherwise_unreachable_styles():
    # (ranking, case) → recommendation_review;(ranking, guide) → authority_ranking
    assert resolve_style_code("ranking", "case") == "recommendation_review"
    assert resolve_style_code("ranking", "guide") == "authority_ranking"


def test_every_intent_and_family_has_a_default():
    for intent in ALL_INTENT_TYPES:
        assert resolve_style_code(intent) in WRITING_STYLES
    for family in ALL_STYLE_FAMILIES:
        assert resolve_style_code("nonsense", family) in WRITING_STYLES


def test_query_intent_hash_stable_and_scoped():
    from db.writing_query_intent_db import query_intent_hash

    # strip + lower 归一:两写法同 hash
    assert query_intent_hash("  Legal ", " 律师哪家好 ") == query_intent_hash("legal", "律师哪家好")
    # industry 隔离:同 query 不同行业不同 hash
    assert query_intent_hash("legal", "x") != query_intent_hash("medical", "x")
    # 32 位 md5
    assert len(query_intent_hash("a", "b")) == 32


def test_query_intent_labels_reuse_article_intent_ssot():
    from services.research_monitor.article_intent_classifier import ARTICLE_INTENT_TYPES
    from services.research_monitor.query_intent_classifier import QUERY_INTENT_TYPES

    assert QUERY_INTENT_TYPES == ARTICLE_INTENT_TYPES  # 不造第 2 份口径


def test_query_intent_cost_estimate_monotonic():
    from services.research_monitor.query_intent_classifier import estimate_query_intent_cost

    assert estimate_query_intent_cost(0)["est_cost_cny"] == 0
    assert estimate_query_intent_cost(1000)["est_cost_cny"] > estimate_query_intent_cost(100)["est_cost_cny"]


def test_oss_backfill_cap_parameterized():
    """cap=0 → 一条都不回读(即便有 oss_key);默认值仍是 dashboard 的 40。"""
    from services.article_structure_analysis import _MAX_OSS_BACKFILL, _backfill_oss_bodies

    assert _MAX_OSS_BACKFILL == 40  # dashboard 默认不变
    rows = [{"id": i, "oss_key_cleaned": f"k{i}", "inline_cleaned_content": ""} for i in range(5)]
    # cap=0:直接跳过(不触发任何 OSS 下载),回读 0 条
    assert _backfill_oss_bodies(rows, oss_backfill_cap=0) == 0
    assert all(not r.get("cleaned_content") for r in rows)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
