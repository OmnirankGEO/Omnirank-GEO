"""W6 真实回环 · 单测。

纯逻辑(DB-free):回滚黄灯逻辑、age 解析、measure 归因、backfill dry_run。
指纹表往返需 DB(throwaway PG · DATABASE_URL)。
"""
from __future__ import annotations

import os

import pytest


# ---------------- W6.4 回滚黄灯逻辑(纯) ----------------
def test_rollback_suggestion_cases():
    from services.writing_outcome_backfill import evaluate_rollback_suggestion

    # 样本不足 → 不下结论
    r = evaluate_rollback_suggestion(new_rate=0.1, old_rate=0.5, new_sample=10, old_sample=40)
    assert r["suggest_rollback"] is False and r["confident"] is False
    # 旧版无基线 → 无法比较
    r = evaluate_rollback_suggestion(new_rate=0.1, old_rate=0.0, new_sample=40, old_sample=40)
    assert r["suggest_rollback"] is False
    # 降幅 >20% 且样本够 → 建议回滚
    r = evaluate_rollback_suggestion(new_rate=0.3, old_rate=0.5, new_sample=40, old_sample=40)
    assert r["suggest_rollback"] is True and r["drop_pct"] == 40.0
    # 降幅 <20% → 不回滚
    r = evaluate_rollback_suggestion(new_rate=0.46, old_rate=0.5, new_sample=40, old_sample=40)
    assert r["suggest_rollback"] is False and r["confident"] is True
    # 新版更好(负降幅)→ 不回滚
    r = evaluate_rollback_suggestion(new_rate=0.6, old_rate=0.5, new_sample=40, old_sample=40)
    assert r["suggest_rollback"] is False


def test_age_days_parsing():
    from services.writing_outcome_backfill import _age_days

    assert _age_days(None) is None
    assert _age_days("not-a-date") is None
    assert _age_days("2020-01-01T00:00:00Z") > 1000


def test_measure_flags_rollback(monkeypatch):
    import services.writing_outcome_backfill as ob

    # 新版 sha → 40 篇,旧版 sha → 40 篇(until_iso = 旧版 cohort 窗口锚,review fix 后新增)
    monkeypatch.setattr(ob, "list_articles_by_style_version_id",
                        lambda sha, since_days=None, until_iso=None: list(range(40)) if sha else [])

    def cite(ids, since):
        # 新版被引率低(10/40),旧版高(30/40)
        return {"articles": len(ids), "citations": 10 if len(ids) == 40 else 0, "insufficient_data": False}

    entry = {
        "style_code": "ranking_v2", "age_days": 35,
        "new": {"version_id": "vNEW", "prompt_sha256": "a" * 64},
        "old": {"version_id": "vOLD", "prompt_sha256": "b" * 64},
    }
    # 两版都 10/40 → rate 相等,不回滚;改造:让旧版更高
    def cite2(ids, since):
        sha_new = ids and ids == list(range(40))
        return {"articles": len(ids), "citations": 10, "insufficient_data": False}

    m = ob._measure(entry, 30, cite2)
    assert m["comparable"] is True
    assert m["new_rate"] == m["old_rate"]  # 相同计数 → 相等
    assert "rollback_suggestion" in m
    assert m.get("style_name")  # [review fix] 前端渲染人话名,measure 必须带 style_name


def test_measure_old_cohort_window_anchored_to_new_activation(monkeypatch):
    """[review fix] 旧版 cohort 窗口必须锚定新版 activated_at:
    旧版在新版上线(≥30天前)后已停产,「最近 N 天」窗口对它结构性为空 → 黄灯按构造永不可亮。"""
    import services.writing_outcome_backfill as ob

    calls = {}

    def fake_list(version_id, since_days=None, until_iso=None):
        calls[version_id] = {"since_days": since_days, "until_iso": until_iso}
        return list(range(40))

    monkeypatch.setattr(ob, "list_articles_by_style_version_id", fake_list)
    entry = {
        "style_code": "ranking_v2", "age_days": 35,
        "new": {"version_id": "vNEW", "activated_at": "2026-06-01T00:00:00+00:00"},
        "old": {"version_id": "vOLD"},
    }
    ob._measure(entry, 30, lambda ids, s: {"articles": len(ids), "citations": 1, "insufficient_data": False})
    assert calls["vNEW"]["until_iso"] is None  # 新版:最近 N 天(现在往回)
    assert calls["vOLD"]["until_iso"] == "2026-06-01T00:00:00+00:00"  # 旧版:锚定新版上线时刻往前
    assert calls["vOLD"]["since_days"] == 30


def test_measure_no_old_baseline(monkeypatch):
    import services.writing_outcome_backfill as ob

    monkeypatch.setattr(ob, "list_articles_by_style_version_id", lambda sha, since_days=None: [1, 2, 3])
    entry = {"style_code": "x", "age_days": 40, "new": {"version_id": "v", "prompt_sha256": "a" * 64}, "old": None}
    m = ob._measure(entry, 30, lambda ids, s: {"articles": len(ids), "citations": 0, "insufficient_data": True})
    assert m["comparable"] is False


def test_backfill_dry_run_no_write(monkeypatch):
    import services.writing_outcome_backfill as ob

    monkeypatch.setattr(ob, "_iter_active_versions", lambda min_age: [
        {"style_code": "ranking_v2", "age_days": 40,
         "new": {"version_id": "vNEW", "prompt_sha256": "a" * 64},
         "old": {"version_id": "vOLD", "prompt_sha256": "b" * 64}},
    ])
    monkeypatch.setattr(ob, "list_articles_by_style_version_id",
                        lambda sha, since_days=None, until_iso=None: list(range(35)))

    def cite(ids, since):
        return {"articles": len(ids), "citations": 5, "insufficient_data": True}

    res = ob.backfill_writing_outcomes(dry_run=True, citation_fn=cite)
    assert res["mode"] == "dry_run"
    assert res["eligible_versions"] == 1
    assert res["measures"][0]["comparable"] is True


# ---------------- 指纹表往返(需 DB) ----------------
@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="需 DATABASE_URL(throwaway PG)")
def test_fingerprint_roundtrip():
    from db.writing_fingerprint_db import (
        get_fingerprint,
        init_writing_fingerprint_tables,
        list_articles_by_style_version_id,
        record_article_fingerprint,
    )

    init_writing_fingerprint_tables()
    sha = "c" * 64
    ver = "ranking_v2_draft_TEST_990001"
    assert record_article_fingerprint(
        article_id=990001, style_code="ranking_v2", prompt_sha256=sha,
        structure_guidance_applied=True, industry_key="法律", style_version_id=ver,
    )
    fp = get_fingerprint(990001)
    assert fp and fp["style_code"] == "ranking_v2" and fp["structure_guidance_applied"] is True
    # W6 稳定归因键 = style_version_id(非渲染漂移的 prompt_sha256)
    assert 990001 in list_articles_by_style_version_id(ver)
    # 幂等覆盖(同 article_id 不重复插)
    record_article_fingerprint(article_id=990001, style_code="comparison_review", prompt_sha256=sha, style_version_id=ver)
    assert get_fingerprint(990001)["style_code"] == "comparison_review"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
