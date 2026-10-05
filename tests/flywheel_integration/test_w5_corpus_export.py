"""W5 corpus-export · 单测(DB-free · mock 加载 + 审计)。

覆盖:只含正文/来源/标题(剥 oss_key/内部id/权重/特征)、fail-soft 跳空正文、日限截断。
"""
from __future__ import annotations

import pytest


def _rows():
    return ("general", [
        {"id": 1, "group_key": "adopted_group", "intent_type": "ranking", "title": "采纳A",
         "url": "https://a.com/1", "domain": "a.com", "body": "正文A" * 100,
         "oss_key_cleaned": "SECRET_OSS_KEY", "source_weight": 9.9, "features": {"x": True}},
        {"id": 2, "group_key": "adopted_group", "intent_type": "ranking", "title": "采纳B(无正文)",
         "url": "https://b.com/2", "domain": "b.com", "body": "",  # OSS 未回读 → fail-soft 跳过
         "oss_key_cleaned": "K2", "source_weight": 1.0, "features": {}},
        {"id": 3, "group_key": "search_only_control_group", "intent_type": "ranking", "title": "对照C",
         "url": "https://c.com/3", "domain": "c.com", "body": "对照正文" * 100},
    ])


def _patch(monkeypatch, today=0):
    import services.writing_corpus_export as ce
    monkeypatch.setattr(ce, "load_labeled_article_rows", lambda *a, **k: _rows())
    monkeypatch.setattr(ce, "get_today_export_count", lambda: today)
    monkeypatch.setattr(ce, "record_export", lambda *a, **k: None)
    return ce


def test_export_strips_internal_fields_and_failsoft(monkeypatch):
    ce = _patch(monkeypatch)
    res = ce.export_corpus(industry_key="general", signal_layer="adopted", limit=50, actor_id=1)
    assert res["count"] == 1  # 只有 采纳A 有正文;采纳B 无正文被 fail-soft 跳过
    assert res["skipped_no_body"] == 1
    item = res["items"][0]
    assert set(item.keys()) == {"title", "source_url", "domain", "signal_layer", "content"}
    # 内部字段绝不出现
    dumped = str(res)
    assert "SECRET_OSS_KEY" not in dumped and "source_weight" not in dumped and "oss_key" not in dumped


def test_export_signal_layer_control(monkeypatch):
    ce = _patch(monkeypatch)
    res = ce.export_corpus(industry_key="general", signal_layer="control", limit=50)
    assert res["count"] == 1 and res["items"][0]["title"] == "对照C"


def test_export_daily_cap(monkeypatch):
    from db.writing_corpus_export_db import DAILY_EXPORT_CAP
    ce = _patch(monkeypatch, today=DAILY_EXPORT_CAP)  # 今日已达上限
    res = ce.export_corpus(industry_key="general", signal_layer="adopted", limit=50)
    assert res["capped"] is True and res["count"] == 0 and res["daily_remaining"] == 0


def test_export_respects_remaining(monkeypatch):
    from db.writing_corpus_export_db import DAILY_EXPORT_CAP
    ce = _patch(monkeypatch, today=DAILY_EXPORT_CAP - 1)  # 只剩 1 篇额度
    res = ce.export_corpus(industry_key="general", signal_layer="adopted", limit=50)
    assert res["count"] <= 1 and res["daily_remaining"] == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
