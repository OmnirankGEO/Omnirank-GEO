from __future__ import annotations

from pathlib import Path

import pytest

from services.smart_article_supplement import (
    SupplementPricingUnavailable,
    SupplementInputs,
    VALID_EXISTING_STATUSES,
    _load_article_point_cost,
    allocate_supplement_styles,
    build_supplement_preview,
)


ROOT = Path(__file__).resolve().parents[2]


def test_retryable_failed_topic_still_occupies_a_supplement_slot():
    assert "failed" in VALID_EXISTING_STATUSES


def test_pricing_read_failure_blocks_preview(monkeypatch):
    import db.wallet_db

    monkeypatch.setattr(
        db.wallet_db,
        "get_feature_pricing",
        lambda _feature: (_ for _ in ()).throw(RuntimeError("isolated pricing outage")),
    )

    with pytest.raises(SupplementPricingUnavailable):
        _load_article_point_cost()


def test_explicit_free_price_is_not_confused_with_pricing_failure(monkeypatch):
    import db.wallet_db

    monkeypatch.setattr(
        db.wallet_db,
        "get_feature_pricing",
        lambda _feature: {"feature_code": "article_gen", "cost_points": 0},
    )

    assert _load_article_point_cost() == 0


@pytest.mark.parametrize("count", [1, 2, 3, 4, 10])
def test_largest_remainder_supplement_counts_sum_exactly(count: int):
    allocation = allocate_supplement_styles(
        supplement_count=count,
        target_ratios={"guide": 0.5, "comparison": 0.3, "case": 0.2},
        locked_existing={},
    )
    assert sum(allocation.values()) == count


def test_zero_ratio_style_never_receives_article():
    allocation = allocate_supplement_styles(
        supplement_count=4,
        target_ratios={"guide": 0.7, "comparison": 0.3, "risk": 0.0},
        locked_existing={},
    )
    assert allocation.get("risk", 0) == 0


def test_existing_skew_is_corrected_against_post_supplement_total():
    allocation = allocate_supplement_styles(
        supplement_count=4,
        target_ratios={"guide": 0.5, "comparison": 0.5},
        locked_existing={"guide": 4},
    )
    assert allocation == {"guide": 0, "comparison": 4}


def test_plan_gap_is_filled_before_rate_gap():
    preview = build_supplement_preview(
        SupplementInputs(
            keyword_id=9,
            keyword="酒店推荐",
            quote_id=3,
            industry="酒店",
            required_articles=4,
            valid_existing_articles=1,
            target_rate=60,
            recent_rate=0,
            existing_style_counts={},
            target_style_ratios={"guide": 1.0},
            point_cost=390,
        )
    )
    assert preview.suggested_articles == 3
    assert preview.reason_code == "plan_gap"
    assert preview.estimated_points == 1170


def test_completed_plan_below_target_scales_with_gap_and_bounds():
    preview = build_supplement_preview(
        SupplementInputs(
            keyword_id=9,
            keyword="酒店推荐",
            quote_id=3,
            industry="酒店",
            required_articles=4,
            valid_existing_articles=4,
            target_rate=60,
            recent_rate=15,
            existing_style_counts={"guide": 4},
            target_style_ratios={"guide": 0.5, "comparison": 0.5},
            point_cost=390,
        )
    )
    assert preview.suggested_articles == 3
    assert 1 <= preview.suggested_articles <= 10
    assert preview.reason_code == "rate_gap"


def test_fallback_title_keeps_each_locked_body_style():
    from writing.keyword_topic_generator import KeywordTopicGenerator
    from writing.style_registry import resolve_user_choice_to_chinese_style

    style_plan = [
        {"keyword_id": 9, "slot_index": 0, "user_choice": "guide"},
        {"keyword_id": 9, "slot_index": 1, "user_choice": "comparison"},
        {"keyword_id": 9, "slot_index": 2, "user_choice": "case"},
    ]
    generator = KeywordTopicGenerator(
        keywords=[{"id": 9, "keyword": "酒店推荐", "required_articles": 3}],
        brand_name="测试品牌",
        industry="酒店",
        style_plan=style_plan,
    )

    # [标题 AI-only 2026-08-17] `_fallback_batch` 退役 → `_pending_batch`。
    # 本锁验的是「每槽锁定的正文文体不被打乱」,与标题文本无关。
    generated = generator._pending_batch(generator.keywords)

    assert [item["slot_index"] for item in generated] == [0, 1, 2]
    assert [item["article_style"] for item in generated] == [
        resolve_user_choice_to_chinese_style(plan["user_choice"], "酒店")
        for plan in style_plan
    ]


class _RecoveryCursor:
    def execute(self, _sql):
        pass

    def fetchall(self):
        return [
            {"quote_id": 31, "topic_ids": [101, 102]},
            {"quote_id": 32, "topic_ids": [103]},
        ]


class _RecoveryConnection:
    def cursor(self):
        return _RecoveryCursor()

    def close(self):
        pass


def test_scheduler_requeues_durable_regenerating_topics_after_restart(monkeypatch):
    import db.connection
    from services import optimize_title_jobs

    dispatched = []
    monkeypatch.setattr(db.connection, "get_connection", lambda: _RecoveryConnection())
    monkeypatch.setattr(
        optimize_title_jobs,
        "dispatch_optimize_title_job",
        lambda quote_id, topic_ids: dispatched.append((quote_id, topic_ids)) or True,
    )

    result = optimize_title_jobs.recover_optimize_title_jobs()

    assert dispatched == [(31, [101, 102]), (32, [103])]
    assert result == {"queued_quotes": 2, "dispatched_quotes": 2}


def test_server_contract_rejects_stale_plan_and_does_not_accept_client_counts():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    request_contract = source[source.index("class OptimizeGenerateRequest"):]
    request_contract = request_contract[:request_contract.index("def _require_supplement_keyword_access")]
    endpoint = source[source.index('@app.post("/api/writing/optimize-generate")'):]
    endpoint = endpoint[:endpoint.index('@app.post("/api/writing/optimize-retry/{topic_id}")')]

    assert "keyword_id: int" in request_contract
    assert "plan_version: str" in request_contract
    assert "plan_hash: str" in request_contract
    assert "client_request_id: str" in request_contract
    assert "keywords" not in request_contract
    assert "required_articles" not in request_contract
    assert "SUPPLEMENT_PLAN_STALE" in endpoint
    assert "ON CONFLICT (request_id) DO NOTHING" in endpoint
    assert "build_supplement_preview_from_db(conn, req.keyword_id, for_update=True)" in endpoint
    assert "asyncio.create_task" not in endpoint

    preview_endpoint = source[source.index('@app.get("/api/writing/optimize-preview/{keyword_id}")'):]
    preview_endpoint = preview_endpoint[:preview_endpoint.index('@app.post("/api/writing/optimize-generate")')]
    assert "except SupplementPricingUnavailable" in preview_endpoint
    assert '"code": "PRICING_UNAVAILABLE"' in preview_endpoint
    assert "算力价格暂时不可用，请刷新后重试" in preview_endpoint
    assert "503" in preview_endpoint

    frontend = (ROOT / "frontend/src/pages/Monitoring/components/KeywordTable.tsx").read_text(
        encoding="utf-8"
    )
    assert "data.detail?.message || data.detail" in frontend
    assert "Object.keys(supplementErrors).length > 0" in frontend


class _QuoteStatusCursor:
    def __init__(self):
        self.statements: list[str] = []

    def execute(self, sql, _params):
        self.statements.append(" ".join(sql.split()))


def test_quote_is_ready_only_when_every_title_in_the_job_succeeds():
    from services.optimize_title_jobs import _update_quote_writing_status

    failed = _QuoteStatusCursor()
    _update_quote_writing_status(failed, 3, generated_count=0, failed_count=2)
    assert len(failed.statements) == 1
    assert "status IN ('failed', 'regenerating')" in failed.statements[0]
    assert "THEN 'pending'" in failed.statements[0]

    partial = _QuoteStatusCursor()
    _update_quote_writing_status(partial, 3, generated_count=1, failed_count=1)
    assert len(partial.statements) == 1
    assert "EXISTS" in partial.statements[0]

    complete = _QuoteStatusCursor()
    _update_quote_writing_status(complete, 3, generated_count=2, failed_count=0)
    assert len(complete.statements) == 1
    assert "THEN 'titles_ready'" in complete.statements[0]

    no_change = _QuoteStatusCursor()
    _update_quote_writing_status(no_change, 3, generated_count=0, failed_count=0)
    assert no_change.statements == []

    source = (ROOT / "services/optimize_title_jobs.py").read_text(encoding="utf-8")
    mark_failed = source[source.index("def _mark_failed"):source.index("def _update_quote_writing_status")]
    assert "UPDATE quotes SET writing_status='pending'" in mark_failed


def test_retry_endpoint_rejects_regenerating_and_checks_atomic_transition():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    endpoint = source[source.index('@app.post("/api/writing/optimize-retry/{topic_id}")'):]
    endpoint = endpoint[:endpoint.index("# ========== 写作竞品清洗 ==========")]

    assert 'if topic["status"] != "failed"' in endpoint
    assert "if cur.rowcount != 1" in endpoint
    assert "状态已变化，请刷新后重试" in endpoint
