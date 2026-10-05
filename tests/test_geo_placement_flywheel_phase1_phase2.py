from pathlib import Path


def test_geo_flywheel_frontend_surfaces_job_counts_outside_calibration_tab():
    text = Path("frontend/src/pages/Admin/GeoPlacementFlywheel.tsx").read_text(encoding="utf-8")

    assert "function summarizeJobResult" in text
    assert "toast.warning" in text
    assert "保存 0 条" in text
    assert text.index("{lastJob && (") < text.index("<Tabs defaultValue=\"media\"")
    assert "当前是否影响线上" in text
    assert "不影响线上" in text
    assert "技术明细（仅排查问题时查看）" in text
    assert text.index("技术明细（仅排查问题时查看）") < text.index("JSON.stringify(lastJob")
    assert "数据健康" in text
    assert "人工启用" in text
    assert "数据健康状态" in text
    assert "原始引用记录" in text
    assert "正文可用文章" in text
    assert "保存为待审核数据" in text
    assert "当前使用" in text
    assert "写入 shadow" not in text
    assert "shadow active" not in text
    assert "shadow / approved / active" not in text
    assert "当前 active 版本" not in text


def test_source_signal_loader_keeps_completed_legacy_rounds_without_total_articles_seen(monkeypatch):
    from scripts import rebuild_geo_source_signals_shadow as rebuild_mod

    captured = {}

    class FakeCursor:
        def execute(self, sql, params):
            captured["sql"] = sql
            captured["params"] = params
            assert "rnd.status = 'completed'" in sql
            assert "summary_json->>'total_articles_seen'" in sql
            assert "rnd.summary_json ? 'total_articles_seen'" in sql

        def fetchall(self):
            return [{
                "id": 1,
                "industry": "旅游酒店",
                "query": "亲子酒店推荐",
                "engine": "qwen",
                "cite_url": "https://travel.example.com/guide",
                "cite_title": "亲子酒店攻略",
                "cite_position": 1,
                "search_rank": 1,
                "batch_id": "round_legacy_tourism",
                "total_sources_in_answer": 8,
            }]

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def close(self):
            return None

    monkeypatch.setattr(rebuild_mod, "get_connection", lambda: FakeConnection())

    rows = rebuild_mod.load_raw_rows(industry="旅游酒店", limit=5)

    assert rows[0]["industry"] == "旅游酒店"
    assert "旅游酒店" in captured["params"]
    assert captured["params"][-1] == 5


def test_media_entity_normalization_and_reference_only_boundary():
    from services.media_entity_flywheel import (
        build_media_entity_seed,
        compute_media_entity_shadow_score,
        industry_filter_values,
        normalize_domain,
    )

    assert normalize_domain("https://www.example.com/path?a=1") == "example.com"
    assert normalize_domain("r.jina.ai/http://m.example.com/story") == "example.com"
    assert "旅游酒店" in industry_filter_values("tourism_hotel")
    assert "tourism_hotel" in industry_filter_values("旅游酒店")

    entity = build_media_entity_seed({
        "name": "携程旅行",
        "domain": "https://www.ctrip.com/",
        "industry": "旅游酒店",
        "aliases": ["携程", "Trip.com"],
    })
    score = compute_media_entity_shadow_score(
        entity=entity,
        citation_rollup={
            "answer_adopted_count": 8,
            "cited_count": 12,
            "prompt_count": 10,
            "engine_count": 3,
        },
        inventory_matches=[],
        outcome_rollup={},
    )

    assert score["reference_status"] == "reference_only"
    assert score["is_purchasable"] is False
    assert "只作为写作参考" in " ".join(score["reasons"])


def test_frontend_industry_options_share_backend_recognized_values():
    import re

    from services.media_entity_flywheel import industry_filter_values, normalize_industry_key

    root = Path(__file__).resolve().parents[1]
    shared = root / "frontend" / "src" / "lib" / "geoIndustries.ts"
    text = shared.read_text(encoding="utf-8")

    values = re.findall(r"value:\s*['\"]([^'\"]+)['\"]", text)

    assert values
    assert "real-estate" in values
    assert "home-decor" in values
    assert "tourism-hotel" in values
    assert "房产" not in values
    assert "房地产" not in values
    assert len(values) == len(set(values))

    for value in values:
        normalized = normalize_industry_key(value)
        filters = industry_filter_values(value)
        assert normalized
        if value == "general":
            assert filters == []
            continue
        if value != "general":
            assert normalized != value
        assert filters, value
        assert value in filters


def test_frontend_industry_dropdowns_use_shared_options():
    root = Path(__file__).resolve().parents[1]
    geo_page = (root / "frontend" / "src" / "pages" / "Admin" / "GeoPlacementFlywheel.tsx").read_text(encoding="utf-8")
    writing_settings = (root / "frontend" / "src" / "components" / "WritingSettingsDialog.tsx").read_text(encoding="utf-8")

    assert "@/lib/geoIndustries" in geo_page
    assert "@/lib/geoIndustries" in writing_settings
    assert "GEO_INDUSTRY_OPTIONS_WITH_GENERAL.map" in geo_page
    assert "GEO_INDUSTRY_OPTIONS_WITH_GENERAL.map" in writing_settings
    assert "useState('旅游酒店')" not in geo_page
    assert "placeholder=\"输入行业，如：旅游酒店\"" not in geo_page
    assert "placeholder=\"general / manufacturing / education\"" not in writing_settings
    assert "房地产',\n  '房产'" not in geo_page


def test_geo_flywheel_writing_strategy_follows_industry_scope():
    """[B3-2 · §8 stale 更新] 旧断言锁死"写作策略使用全部文章(general)"是被 B3-2 修掉的 bug 口径。
    新意图:写作策略作用域跟随右上角行业选择器(general 仍是默认选项),生成/列表/审核/启用全链路带选中行业。
    """
    root = Path(__file__).resolve().parents[1]
    geo_page = (root / "frontend" / "src" / "pages" / "Admin" / "GeoPlacementFlywheel.tsx").read_text(encoding="utf-8")

    # 不再写死 general 作用域常量
    assert "const WRITING_STRATEGY_SCOPE = 'general'" not in geo_page
    # 写作作用域从 industry 选择器状态派生(general 仍是默认)
    assert "const queryWritingScope = useMemo(() => encodeURIComponent(industry.trim())" in geo_page
    assert "GEO_INDUSTRY_OPTIONS_WITH_GENERAL" in geo_page
    assert "useState('general')" in geo_page
    # 生成/重建/分析 POST 体带选中行业,不再写死常量
    assert "industry_key: industry," in geo_page
    assert "industry_key: WRITING_STRATEGY_SCOPE" not in geo_page
    # 列表/active/analyze 仍按 queryWritingScope(现已跟随 industry 选择器)
    assert "`/writing/strategy-versions?industry_key=${queryWritingScope}" in geo_page
    assert "`/writing/strategy-active?industry_key=${queryWritingScope}" in geo_page
    assert "/writing/article-structure/analyze?industry_key=${queryWritingScope}" in geo_page


def test_general_scope_is_treated_as_all_industries():
    from services.media_entity_flywheel import industry_filter_values, is_all_industry_scope

    for value in ("", "general", "all", "all_articles", "通用/全部行业"):
        assert is_all_industry_scope(value)
        assert industry_filter_values(value) == []


def test_article_structure_general_scope_queries_all_articles(monkeypatch):
    import services.article_structure_analysis as article_structure_analysis

    seen: dict[str, object] = {"queries": [], "params": []}

    class FakeCursor:
        def execute(self, query, params):
            seen["queries"].append(query)
            seen["params"].append(params)

        def fetchall(self):
            return []

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(article_structure_analysis, "get_connection", lambda: FakeConnection())

    industry_key, industry_values, rows = article_structure_analysis._load_rows("general", limit=10, min_chars=100)

    assert industry_key == "general"
    assert industry_values == []
    assert rows == []
    assert len(seen["queries"]) == 2
    assert "article.primary_industry = ANY" not in seen["queries"][0]
    assert "raw.industry = ANY" not in seen["queries"][0]
    assert "FROM articles article" in seen["queries"][1]
    assert seen["params"] == [(100, 10), (100, 10)]
    assert seen["closed"] is True


def test_source_signal_rollup_general_scope_queries_all_industries(monkeypatch):
    import db.geo_source_signals_db as source_db

    seen: dict[str, object] = {}

    class FakeCursor:
        def execute(self, query, params):
            seen["query"] = query
            seen["params"] = params

        def fetchall(self):
            return []

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(source_db, "get_connection", lambda: FakeConnection())

    assert source_db.list_source_signal_rollup(industry_key="general", limit=7) == []
    assert "WHERE industry_key = %s" not in seen["query"]
    assert seen["params"] == [7]
    assert seen["closed"] is True


def test_writing_style_rebuild_general_scope_queries_all_articles(monkeypatch):
    import db.writing_style_flywheel_db as writing_db

    seen: dict[str, object] = {}

    class FakeCursor:
        def execute(self, query, params):
            seen["query"] = query
            seen["params"] = params

        def fetchall(self):
            return []

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(writing_db, "get_connection", lambda: FakeConnection())

    result = writing_db.rebuild_style_feature_snapshots_from_articles("general", limit=11, min_chars=123, dry_run=True)

    assert result["loaded"] == 0
    assert "article.primary_industry = ANY" not in seen["query"]
    assert "raw.industry = ANY" not in seen["query"]
    assert "WHERE industry_key = %s" not in seen["query"]
    assert seen["params"] == (123, 11)
    assert seen["closed"] is True


def test_purchasable_inventory_beats_reference_only_when_evidence_is_equal():
    from services.media_entity_flywheel import (
        build_media_entity_seed,
        compute_media_entity_shadow_score,
    )

    entity = build_media_entity_seed({"name": "马蜂窝", "domain": "mafengwo.cn", "industry": "旅游酒店"})
    rollup = {"answer_adopted_count": 2, "cited_count": 4, "prompt_count": 6, "engine_count": 2}
    purchasable = compute_media_entity_shadow_score(
        entity=entity,
        citation_rollup=rollup,
        inventory_matches=[{
            "media_source": "media",
            "inventory_id": 1001,
            "media_name": "马蜂窝旅游频道",
            "price_yuan": 120,
            "is_active": True,
            "is_purchasable": True,
            "match_confidence": 0.92,
        }],
        outcome_rollup={"published_count": 3, "citation_lift_30d": 5},
    )
    reference = compute_media_entity_shadow_score(
        entity=entity,
        citation_rollup=rollup,
        inventory_matches=[],
        outcome_rollup={"published_count": 3, "citation_lift_30d": 5},
    )

    assert purchasable["is_purchasable"] is True
    assert purchasable["shadow_score"] > reference["shadow_score"]
    assert purchasable["reference_status"] == "purchasable"


def test_raw_inventory_without_verified_match_is_fail_closed_reference_only():
    from services.media_entity_flywheel import build_media_entity_seed, compute_media_entity_shadow_score

    entity = build_media_entity_seed({"name": "携程", "domain": "ctrip.com", "industry": "旅游酒店"})
    score = compute_media_entity_shadow_score(
        entity=entity,
        citation_rollup={"answer_adopted_count": 4, "cited_count": 7, "prompt_count": 10, "engine_count": 3},
        inventory_matches=[{
            "media_name": "低价套餐随机发布",
            "domain": "unrelated-spam.cn",
            "price_yuan": 999,
        }],
        outcome_rollup={},
    )

    assert score["reference_status"] == "reference_only"
    assert score["is_purchasable"] is False
    assert score["inventory_score"] == 0


def test_source_signal_weights_answer_adoption_highest_and_balances_long_source_lists():
    from services.research_monitor.source_signal_weighting import (
        SourceSignal,
        aggregate_source_signals,
        signal_weight,
    )

    assert signal_weight("answer_adopted") > signal_weight("cited_source")
    assert signal_weight("cited_source") > signal_weight("search_result_only")
    assert signal_weight("search_result_only") > signal_weight("crawled_reference_only")

    short_answer = SourceSignal(
        source_url="https://example.com/a",
        engine="kimi",
        prompt_id="p1",
        signal_tier="cited_source",
        source_position=1,
        total_sources_in_answer=7,
    )
    long_answer = SourceSignal(
        source_url="https://example.com/a",
        engine="doubao",
        prompt_id="p2",
        signal_tier="cited_source",
        source_position=1,
        total_sources_in_answer=35,
    )
    short = aggregate_source_signals([short_answer])
    long = aggregate_source_signals([long_answer])

    assert short[0]["balanced_weight"] > long[0]["balanced_weight"]
    assert long[0]["balanced_weight"] > 0


def test_cite_url_only_stays_search_result_not_cited_or_adopted():
    from services.research_monitor.source_signal_classifier import classify_source_signal

    signal = classify_source_signal({
        "cite_url": "https://www.163.com/news/a.html",
        "cite_title": "网易新闻",
        "answer_text": "这段回答提到了 163 邮箱和新闻，但没有任何答案角标采纳。",
        "engine": "deepseek",
        "query": "旅游酒店怎么选",
        "industry": "旅游酒店",
        "cite_position": 3,
    })

    assert signal.signal_tier == "search_result_only"
    assert signal.source_position == 3


def test_answer_adopted_requires_explicit_adoption_flag_not_title_substring():
    from services.research_monitor.source_signal_classifier import classify_source_signal

    search_only = classify_source_signal({
        "cite_url": "https://travel.example.com/guide",
        "cite_title": "深圳亲子酒店攻略",
        "answer_text": "深圳亲子酒店攻略里常见的判断维度包括位置、早餐和亲子设施。",
        "engine": "qwen",
        "query": "深圳亲子酒店推荐",
        "industry": "旅游酒店",
        "cite_position": 1,
    })
    adopted = classify_source_signal({
        "cite_url": "https://travel.example.com/guide",
        "cite_title": "深圳亲子酒店攻略",
        "answer_text": "回答明确使用了第 1 条来源。",
        "is_answer_cited": True,
        "adoption_rank": 1,
        "engine": "qwen",
        "query": "深圳亲子酒店推荐",
        "industry": "旅游酒店",
        "cite_position": 1,
    })

    assert search_only.signal_tier == "search_result_only"
    assert adopted.signal_tier == "answer_adopted"


def test_answer_cited_survives_failed_body_quality_as_cited_source():
    from services.research_monitor.source_signal_classifier import classify_source_signal

    signal = classify_source_signal({
        "cite_url": "https://travel.example.com/guide",
        "cite_title": "深圳亲子酒店攻略",
        "answer_text": "回答明确使用了第 1 条来源。",
        "is_answer_cited": True,
        "adoption_rank": 1,
        "engine": "qwen",
        "query": "深圳亲子酒店推荐",
        "industry": "旅游酒店",
        "cite_position": 1,
        "clean_status": "failed",
        "cleaned_char_count": 10,
    })

    assert signal.signal_tier == "cited_source"
    assert signal.metadata["adoption_rank"] == 1


def test_answer_cited_does_not_rescue_rejected_or_blacklisted_sources():
    from services.research_monitor.source_signal_classifier import classify_source_signal

    base = {
        "cite_url": "https://spam.example.com/guide",
        "cite_title": "垃圾来源",
        "answer_text": "回答明确使用了第 1 条来源。",
        "is_answer_cited": True,
        "adoption_rank": 1,
        "engine": "qwen",
        "query": "深圳亲子酒店推荐",
        "industry": "旅游酒店",
        "cite_position": 1,
    }

    rejected = classify_source_signal({**base, "review_status": "rejected"})
    blacklisted = classify_source_signal({**base, "domain_tier": "blacklist"})

    assert rejected.signal_tier == "rejected_noise"
    assert blacklisted.signal_tier == "rejected_noise"


def test_deepseek_qwen_answer_markers_set_adoption_flags():
    from services.research_monitor.platforms import mark_answer_cited_sources

    marked = mark_answer_cited_sources(
        "综合来看，第二条来源更贴近问题 [2]，第一条只是背景。",
        [
            {"url": "https://a.example.com", "title": "A", "rank": 1},
            {"url": "https://b.example.com", "title": "B", "rank": 2},
        ],
        raw={},
    )

    assert marked[0]["is_answer_cited"] is False
    assert marked[1]["is_answer_cited"] is True
    assert marked[1]["adoption_rank"] == 2


def test_chinese_adjacent_answer_markers_set_adoption_flags_without_code_indexes():
    from services.research_monitor.platforms import mark_answer_cited_sources

    marked = mark_answer_cited_sources(
        "据携程介绍[2]，来源[1]显示酒店亲子设施更关键。代码示例 arr[3] 不应算引用。",
        [
            {"url": "https://a.example.com", "title": "A", "rank": 1},
            {"url": "https://b.example.com", "title": "B", "rank": 2},
            {"url": "https://c.example.com", "title": "C", "rank": 3},
        ],
        raw={},
    )

    assert marked[0]["is_answer_cited"] is True
    assert marked[0]["adoption_rank"] == 1
    assert marked[1]["is_answer_cited"] is True
    assert marked[1]["adoption_rank"] == 2
    assert marked[2]["is_answer_cited"] is False
    assert marked[2]["adoption_rank"] is None


def test_duplicate_source_url_can_match_original_answer_marker_rank():
    from services.research_monitor.platforms import mark_answer_cited_sources

    marked = mark_answer_cited_sources(
        "这条结论采用了搜索列表第二条来源[2]。",
        [
            {
                "url": "https://a.example.com",
                "title": "A",
                "rank": 1,
                "answer_ranks": [1, 2],
            },
        ],
        raw={},
    )

    assert marked[0]["is_answer_cited"] is True
    assert marked[0]["adoption_rank"] == 2


def test_native_annotations_set_adoption_flags_by_url():
    from services.research_monitor.platforms import mark_answer_cited_sources

    marked = mark_answer_cited_sources(
        "原生 annotations 比文本角标更可信。",
        [
            {"url": "https://a.example.com", "title": "A", "rank": 1},
            {"url": "https://b.example.com/path", "title": "B", "rank": 2},
        ],
        raw={
            "output": [{
                "content": [{
                    "annotations": [{"url": "https://b.example.com/path"}],
                }],
            }],
        },
    )

    assert marked[0]["is_answer_cited"] is False
    assert marked[1]["is_answer_cited"] is True
    assert marked[1]["adoption_rank"] == 2


def test_writing_style_strategy_stays_shadow_until_reviewed():
    from services.writing_style_feature_extractor import extract_writing_style_features
    from services.writing_strategy_service import build_strategy_candidate

    features = extract_writing_style_features({
        "title": "2026旅游酒店怎么选？7家平台对比指南",
        "cleaned_content": "一、榜单对比\n二、避坑指南\n三、价格和服务案例\n常见问题 FAQ",
        "intent_type": "comparison",
        "content_type": "article",
        "domain": "example.com",
    })
    candidate = build_strategy_candidate(
        industry_key="tourism_hotel",
        style_features=[features],
        source_signals=[{"balanced_weight": 0.8, "signal_tier": "answer_adopted"}],
        outcome_signals=[{"citation_lift_30d": 3, "publish_status": "published"}],
    )
    second_candidate = build_strategy_candidate(
        industry_key="旅游酒店",
        style_features=[features],
        source_signals=[{"balanced_weight": 0.8, "signal_tier": "answer_adopted"}],
        outcome_signals=[{"citation_lift_30d": 3, "publish_status": "published"}],
    )

    assert features["style_family"] in {"comparison", "guide"}
    assert candidate["status"] == "shadow"
    assert candidate["requires_admin_review"] is True
    assert "不得自动替换线上写作策略" in candidate["guardrails"]
    assert candidate["industry_key"] == "tourism_hotel"
    assert second_candidate["industry_key"] == "tourism_hotel"
    assert candidate["strategy_version"] != second_candidate["strategy_version"]


def test_strategy_activate_and_rollback_archive_previous_active_and_write_audit(monkeypatch):
    from db import writing_style_flywheel_db as writing_db

    strategies = {
        1: {"id": 1, "industry_key": "tourism_hotel", "status": "active", "reviewed_by": 7},
        2: {"id": 2, "industry_key": "tourism_hotel", "status": "approved", "reviewed_by": 8},
    }
    audits: list[tuple] = []

    class FakeCursor:
        def __init__(self):
            self.row = None

        def execute(self, sql, params=()):
            if "SELECT * FROM writing_strategy_versions WHERE id" in sql:
                self.row = strategies.get(int(params[0]))
                return
            if "pg_advisory_xact_lock" in sql:
                self.row = None
                return
            if "SET status = 'archived'" in sql:
                industry_key, target_id = params
                for row in strategies.values():
                    if row["industry_key"] == industry_key and row["status"] == "active" and row["id"] != target_id:
                        row["status"] = "archived"
                        row["archived_at"] = "now"
                self.row = None
                return
            if "SET status = 'active'" in sql:
                reviewer_id = int(params[1])
                target_id = int(params[-1])
                row = strategies[target_id]
                row["status"] = "active"
                row["activated_by"] = reviewer_id
                row["activated_at"] = "now"
                if "archived_at = NULL" in sql:
                    row["archived_at"] = None
                self.row = row
                return
            if "INSERT INTO writing_strategy_audit_events" in sql:
                audits.append(tuple(params))
                self.row = None
                return
            raise AssertionError(f"unexpected SQL: {sql}")

        def fetchone(self):
            return self.row

    class FakeDb:
        def __init__(self):
            self.cursor_obj = FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return self.cursor_obj

    monkeypatch.setattr(writing_db, "get_db", lambda: FakeDb())

    activated = writing_db.activate_strategy_version(2, reviewer_id=99, note="审核通过，启用新版")

    assert activated["id"] == 2
    assert strategies[1]["status"] == "archived"
    assert strategies[1]["archived_at"] == "now"
    assert strategies[2]["status"] == "active"
    assert strategies[2]["activated_by"] == 99
    assert audits[-1][2] == "activate"
    assert audits[-1][3] == 99
    assert audits[-1][4] == "approved"
    assert audits[-1][5] == "active"

    rolled_back = writing_db.rollback_strategy_version(1, reviewer_id=99, note="回滚到上一版")

    assert rolled_back["id"] == 1
    assert strategies[1]["status"] == "active"
    assert strategies[1]["archived_at"] is None
    assert strategies[2]["status"] == "archived"
    assert audits[-1][2] == "rollback"
    assert audits[-1][4] == "archived"
    assert audits[-1][5] == "active"


def test_strategy_activation_api_requires_note_and_green_health(monkeypatch):
    import pytest
    from fastapi import HTTPException

    from api import writing_style_flywheel_api as writing_api

    with pytest.raises(HTTPException) as empty_note:
        writing_api._require_operation_note("   ")
    assert empty_note.value.status_code == 400
    assert empty_note.value.detail == "review_note_required"

    monkeypatch.setattr(
        writing_api,
        "get_flywheel_data_health",
        lambda **_: {"can_activate": False, "summary": "历史补录存在重复调用记录"},
    )
    with pytest.raises(HTTPException) as blocked:
        writing_api._ensure_strategy_health_allows_activation("tourism_hotel")
    assert blocked.value.status_code == 409
    assert blocked.value.detail["code"] == "health_gate_blocked"

    monkeypatch.setattr(
        writing_api,
        "get_flywheel_data_health",
        lambda **_: {"can_activate": True, "summary": "数据可用"},
    )
    assert writing_api._ensure_strategy_health_allows_activation("tourism_hotel")["can_activate"] is True


def test_rebuild_writing_style_features_from_research_articles(monkeypatch):
    from db import writing_style_flywheel_db as writing_db

    executed: list[tuple[str, tuple]] = []
    inserted: list[dict] = []

    class FakeCursor:
        def execute(self, sql, params=()):
            executed.append((sql, params))

        def fetchall(self):
            return [
                {
                    "id": 101,
                    "url": "https://travel.example.com/a",
                    "domain": "travel.example.com",
                    "title": "2026旅游酒店怎么选？7家平台对比指南",
                    "primary_industry": "旅游酒店",
                    "content_type": "article",
                    "intent_type": "comparison",
                    "inline_cleaned_content": "一、榜单对比\n二、避坑指南\n三、价格和服务案例\n常见问题 FAQ\n" * 40,
                    "cleaned_char_count": 1200,
                    "review_status": "in_library",
                    "clean_status": "cleaned",
                    "total_citation_count": 7,
                },
                {
                    "id": 102,
                    "url": "https://hotel.example.com/b",
                    "domain": "hotel.example.com",
                    "title": "亲子酒店避坑攻略",
                    "primary_industry": "旅游酒店",
                    "content_type": "article",
                    "intent_type": "guide",
                    "inline_cleaned_content": "如何选择亲子酒店？先看位置、早餐、设施和取消政策。\n" * 50,
                    "cleaned_char_count": 900,
                    "review_status": "in_library",
                    "clean_status": "cleaned",
                    "total_citation_count": 3,
                },
            ]

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def close(self):
            return None

    monkeypatch.setattr(writing_db, "get_connection", lambda: FakeConnection())

    def fake_upsert(snapshot):
        inserted.append(snapshot)
        return {"id": len(inserted), **snapshot}

    monkeypatch.setattr(writing_db, "upsert_style_feature_snapshot", fake_upsert)

    result = writing_db.rebuild_style_feature_snapshots_from_articles("旅游酒店", limit=10, dry_run=False)

    assert result["industry_key"] == "tourism_hotel"
    assert result["sampled"] == 2
    assert result["written"] == 2
    assert result["skipped"] == 0
    assert inserted[0]["industry_key"] == "tourism_hotel"
    assert inserted[0]["features"]["style_family"] in {"comparison", "guide"}
    assert "geo_research_articles" in executed[0][0]
    assert "primary_industry = ANY" in executed[0][0]


def test_flywheel_schema_is_idempotent_and_redline_free():
    root = Path(__file__).resolve().parents[1]
    migration = root / "scripts" / "migration_geo_placement_flywheel_2026_06_12.sql"
    text = migration.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS geo_media_entities" in text
    assert "CREATE TABLE IF NOT EXISTS geo_research_source_signals" in text
    assert "ADD COLUMN IF NOT EXISTS is_answer_cited" in text
    assert "ADD COLUMN IF NOT EXISTS adoption_rank" in text
    assert "CREATE TABLE IF NOT EXISTS writing_strategy_versions" in text
    assert "strategy_version VARCHAR(240)" in text
    assert "ALTER COLUMN strategy_version TYPE VARCHAR(240)" in text
    assert "idx_writing_strategy_one_active_per_industry" in text
    assert "WHERE status = 'active'" in text
    assert "activated_by BIGINT" in text
    assert "archived_at TIMESTAMPTZ" in text
    assert "CREATE TABLE IF NOT EXISTS writing_strategy_audit_events" in text
    assert "DROP TABLE" not in text.upper()
    assert "TRUNCATE" not in text.upper()


def test_flywheel_server_registration_is_shadow_and_admin_only():
    root = Path(__file__).resolve().parents[1]
    server = (root / "server.py").read_text(encoding="utf-8")
    media_api = (root / "api" / "media_entity_flywheel_api.py").read_text(encoding="utf-8")
    writing_api = (root / "api" / "writing_style_flywheel_api.py").read_text(encoding="utf-8")

    assert "api.media_entity_flywheel_api" in server
    assert "api.writing_style_flywheel_api" in server
    assert "_require_admin" in media_api and "需要管理员权限" in media_api
    assert "_require_admin" in writing_api and "需要管理员权限" in writing_api
    assert "shadow_only" in media_api
    assert "shadow_only" in writing_api


def test_phase1_phase2_admin_api_has_operational_workflow():
    root = Path(__file__).resolve().parents[1]
    media_api = (root / "api" / "media_entity_flywheel_api.py").read_text(encoding="utf-8")
    writing_api = (root / "api" / "writing_style_flywheel_api.py").read_text(encoding="utf-8")
    writing_db = (root / "db" / "writing_style_flywheel_db.py").read_text(encoding="utf-8")

    assert '"/health"' in media_api
    assert "get_flywheel_data_health" in media_api
    assert '"/source-signals/rollup"' in media_api
    assert '"/source-signals/rebuild"' in media_api
    assert '"/media/rebuild-from-source-signals"' in media_api
    assert '"/writing/style-features/rebuild"' in writing_api
    assert '"/writing/strategy-generate-from-data"' in writing_api
    assert '"/writing/strategy-versions/{strategy_id}/review"' in writing_api
    assert '"/writing/strategy-versions/{strategy_id}/activate"' in writing_api
    assert '"/writing/strategy-versions/{strategy_id}/rollback"' in writing_api
    assert '"/writing/strategy-versions/{strategy_id}/audit"' in writing_api
    assert "StrategyActivateRequest" in writing_api
    assert "review_note_required" in writing_api
    assert "health_gate_blocked" in writing_api
    assert "get_flywheel_data_health" in writing_api
    assert "list_strategy_audit_events" in writing_api
    assert "production_takeover" in writing_api
    assert "rebuild_style_feature_snapshots_from_articles" in writing_db
    assert "activate_strategy_version" in writing_db
    assert "rollback_strategy_version" in writing_db
    assert "writing_strategy_audit_events" in writing_db
    assert "INSERT INTO writing_strategy_audit_events" in writing_db
    assert "actor_id, from_status, to_status, note" in writing_db
    assert "pg_advisory_xact_lock" in writing_db
    assert "idx_writing_strategy_one_active_per_industry" in writing_db
    assert "status = 'active'" in writing_db
    assert "status = 'archived'" in writing_db


def test_phase1_phase2_frontend_admin_page_is_wired():
    root = Path(__file__).resolve().parents[1]
    app = (root / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    sidebar = (root / "frontend" / "src" / "components" / "layout" / "AppSidebar.tsx").read_text(encoding="utf-8")
    page = (root / "frontend" / "src" / "pages" / "Admin" / "GeoPlacementFlywheel.tsx").read_text(encoding="utf-8")

    assert "GeoPlacementFlywheel" in app
    assert 'admin/geo-placement-flywheel' in app
    assert "GEO 数据飞轮" in sidebar
    assert "一期：媒体/站点" in page
    assert "来源证据" in page
    assert "二期：写作策略" in page
    assert "校准记录" in page
    assert "当前是否影响线上" in page
    assert "只读预览" in page
    assert "保存为待审核数据" in page
    assert "需要管理员审核" in page
    assert "数据健康状态" in page
    assert "全部" in page
    assert "可投放" in page
    assert "仅作参考" in page
    assert "高证据" in page
    assert "低质量" in page
    assert "共享平台域名" in page
    assert "查看详情" in page
    assert "媒体/站点详情" in page
    assert "mediaDetailOpen" in page
    assert "setMediaDetailOpen(true)" in page
    assert "mediaDetailOpen && selectedMedia" in page
    assert "技术明细（仅排查问题时查看）" in page
    assert "可投放资源" in page
    assert "系统判断" in page
    assert "答案采纳" in page
    assert "明确引用" in page
    assert "搜索曝光" in page
    assert "原文参考" in page
    assert "下一步建议" in page
    assert "风险提示" in page
    assert "待审核 / 已审核 / 当前使用" in page
    assert "写入 shadow 表" not in page
    assert "shadow / approved / active" not in page
    assert "/media/rebuild-from-source-signals" in page
    assert "/writing/style-features/rebuild" in page
    assert "预览文章风格" in page
    assert "保存文章风格" in page
    assert "操作历史" in page
    assert "启用前确认" in page
    assert "回滚前确认" in page
    assert "必须填写操作备注" in page
    assert "/writing/strategy-generate-from-data" in page
    assert "/rollback" in page
    assert "/audit" in page
    assert "不会自动接管线上链路" in page


def test_flywheel_data_health_is_read_only_and_operator_friendly(monkeypatch):
    from db import media_entity_flywheel_db as media_db

    executed: list[str] = []

    class FakeCursor:
        def __init__(self):
            self.last_sql = ""

        def execute(self, sql, params=()):
            self.last_sql = sql
            executed.append(sql)

        def fetchone(self):
            sql = self.last_sql
            if "FROM geo_research_source_signals" in sql:
                return {
                    "source_signal_count": 100,
                    "answer_adoption_count": 8,
                    "explicit_citation_count": 12,
                    "search_exposure_count": 70,
                    "noise_count": 2,
                    "source_domain_count": 30,
                }
            if "FROM geo_research_raw" in sql:
                return {"raw_citation_rows": 160, "raw_answer_cited_count": 8}
            if "FROM geo_research_articles" in sql:
                return {
                    "article_count": 80,
                    "article_body_count": 65,
                    "failed_body_count": 1,
                }
            if "FROM geo_research_article_citations" in sql:
                return {"citation_rows": 160, "raw_id_missing": 0}
            if "FROM geo_research_round_call" in sql:
                return {"legacy_duplicate_groups": 0}
            if "FROM pg_indexes" in sql:
                return {"legacy_unique_index_exists": True}
            raise AssertionError(f"unexpected SQL: {sql}")

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def close(self):
            return None

    monkeypatch.setattr(media_db, "get_connection", lambda: FakeConnection())

    health = media_db.get_flywheel_data_health(
        industry_key="tourism_hotel",
        industry_values=["旅游酒店", "tourism_hotel"],
    )

    assert health["level"] == "green"
    assert health["can_preview"] is True
    assert health["can_save_shadow"] is True
    assert health["can_activate"] is True
    assert health["metrics"]["raw_citation_rows"] == 160
    assert health["metrics"]["article_body_count"] == 65
    assert health["metrics"]["answer_adoption_count"] == 8
    assert health["metrics"]["legacy_duplicate_groups"] == 0
    assert health["metrics"]["legacy_unique_index_exists"] is True
    joined_sql = "\n".join(executed).upper()
    assert "SELECT" in joined_sql
    assert "INSERT " not in joined_sql
    assert "UPDATE " not in joined_sql
    assert "DELETE " not in joined_sql


def test_flywheel_rejects_blank_entities_and_empty_source_urls():
    root = Path(__file__).resolve().parents[1]
    media_api = (root / "api" / "media_entity_flywheel_api.py").read_text(encoding="utf-8")
    media_db = (root / "db" / "media_entity_flywheel_db.py").read_text(encoding="utf-8")
    flywheel = (root / "services" / "media_entity_flywheel.py").read_text(encoding="utf-8")
    rebuild_script = (root / "scripts" / "rebuild_geo_source_signals_shadow.py").read_text(encoding="utf-8")

    assert "媒体实体名称或域名不能为空" in media_api
    assert "inventory_id(match) <= 0" in media_api
    assert "matched_inventory or req.inventory_matches" not in media_api
    assert 'match.get("is_purchasable", True)' not in media_db
    assert 'm.get("is_purchasable", True)' not in flywheel
    assert "if not signal.source_url" in rebuild_script
    assert "JOIN geo_research_round" in rebuild_script
    assert "rnd.status = 'completed'" in rebuild_script
    assert "total_articles_seen" in rebuild_script
    assert "COUNT(*) OVER" in rebuild_script
    assert "raw.is_answer_cited" in rebuild_script
    assert "raw.adoption_rank" in rebuild_script
    assert "raw.cite_position AS search_rank" in rebuild_script
    assert "art.review_status" in rebuild_script


def test_flywheel_does_not_touch_redline_files():
    root = Path(__file__).resolve().parents[1]
    forbidden = [
        root / "middleware" / "billing.py",
        root / "db" / "connection.py",
        root / "auth" / "middleware.py",
        root / "auth" / "jwt_utils.py",
    ]
    for path in forbidden:
        assert path.exists()
