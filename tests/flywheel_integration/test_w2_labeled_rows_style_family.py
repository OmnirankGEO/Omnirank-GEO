"""W2 语料行标签补齐契约。

生产 geo_research_articles 没有 style_family 列,蒸馏/看板不能直接读 row["style_family"]
假装接上 family 轴;load_labeled_article_rows 必须从标题/正文/结构特征补出次轴。
"""
from __future__ import annotations


def test_labeled_rows_enrich_style_family_when_db_has_no_family_column(monkeypatch):
    from services import article_structure_analysis as asa

    raw_rows = [
        {
            "id": 1,
            "url": "https://example.com/a",
            "domain": "example.com",
            "title": "深圳装修公司排名怎么选?避坑指南",
            "primary_industry": "装修",
            "content_type": "article",
            "intent_type": "ranking",
            "inline_cleaned_content": "如何选择装修公司? 先看口碑、价格、施工流程和避坑清单。" * 40,
            "cleaned_char_count": 1200,
            "is_adopted": 1,
            "is_cited": 0,
            "is_search_only": 0,
        },
        {
            "id": 2,
            "url": "https://example.com/b",
            "domain": "example.com",
            "title": "真实业主案例:80平装修复盘",
            "primary_industry": "装修",
            "content_type": "article",
            "intent_type": None,
            "inline_cleaned_content": "业主案例复盘,从预算到落地,记录项目过程和客户反馈。" * 40,
            "cleaned_char_count": 1200,
            "is_adopted": 0,
            "is_cited": 0,
            "is_search_only": 1,
        },
    ]
    monkeypatch.setattr(asa, "_load_rows", lambda *a, **k: ("general", [], raw_rows))

    _industry, rows = asa.load_labeled_article_rows("general", limit=10, min_chars=100, oss_backfill_cap=0)

    assert rows[0]["style_family"] == "guide"
    assert rows[0]["intent_type"] == "ranking"
    assert rows[1]["style_family"] == "case"
    assert rows[1]["intent_type"] is None


def test_labeled_rows_preserve_upstream_style_family(monkeypatch):
    from services import article_structure_analysis as asa

    raw_rows = [
        {
            "id": 3,
            "url": "https://example.com/c",
            "domain": "example.com",
            "title": "普通标题",
            "primary_industry": "装修",
            "content_type": "article",
            "intent_type": "ranking",
            "style_family": "case",
            "inline_cleaned_content": "普通正文。" * 120,
            "cleaned_char_count": 1200,
            "is_adopted": 1,
            "is_cited": 0,
            "is_search_only": 0,
        },
    ]
    monkeypatch.setattr(asa, "_load_rows", lambda *a, **k: ("general", [], raw_rows))

    _industry, rows = asa.load_labeled_article_rows("general", limit=10, min_chars=100, oss_backfill_cap=0)

    assert rows[0]["style_family"] == "case"
