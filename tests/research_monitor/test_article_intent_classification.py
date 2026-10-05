"""P15 · 文章意图分类。

覆盖 8 类枚举、DeepSeek JSON 归一化、跑批接线和文章库 API。
"""
import inspect
from pathlib import Path


def test_intent_classifier_normalizes_deepseek_json():
    from services.research_monitor.article_intent_classifier import (
        normalize_intent_result,
        ARTICLE_INTENT_TYPES,
    )

    result = normalize_intent_result({
        "intent_type": "ranking",
        "confidence": 0.93,
        "reason": "标题和正文都是 TOP10 推荐榜单",
    }, model="deepseek-v4-flash")

    assert set(ARTICLE_INTENT_TYPES) == {
        "ranking", "tutorial", "long_form", "comparison",
        "data_report", "policy", "definition", "faq",
    }
    assert result.intent_type == "ranking"
    assert result.confidence == 0.93
    assert result.model == "deepseek-v4-flash"
    assert "TOP10" in result.reason


def test_round_runner_has_stage45_intent_classification():
    import services.research_monitor.round_runner as rr

    assert hasattr(rr, "stage45_classify_article_intents")
    src = inspect.getsource(rr._run_pipeline)
    assert "stage45_classify_article_intents" in src
    assert src.index("stage4_clean_articles") < src.index("stage45_classify_article_intents") < src.index("stage5_filter_by_char_count")


def test_articles_api_exposes_intent_distribution_and_manual_update():
    import api.research_monitor_articles_api as articles_api

    src = inspect.getsource(articles_api)
    assert "/articles/intent-distribution" in src
    assert "/articles/{article_id}/intent" in src
    assert "intent_type" in src
    assert "intent_confidence" in src


def test_backfill_script_uses_deepseek_classifier_and_dry_run():
    src = Path("scripts/backfill_article_intent_type.py").read_text(encoding="utf-8")
    assert "classify_article_intent" in src
    assert "--dry-run" in src
    assert "intent_type IS NULL" in src
    assert "intent_classified_at = NOW()" in src



def test_intent_distribution_percent_uses_classified_total_not_all_articles():
    """?????????????????,???????????"""
    src = Path("api/research_monitor_articles_api.py").read_text(encoding="utf-8")
    assert "classified_total" in src
    assert "count / classified_total" in src
    assert '"classified_total"' in src
    assert "unclassified_count" in src


def test_intent_cost_env_uses_safe_float_parser():
    """???? env ?????? backend import ??"""
    src = Path("services/research_monitor/round_runner.py").read_text(encoding="utf-8")
    assert "INTENT_CLASSIFY_COST_PER_ARTICLE = _safe_float_env(" in src
    assert "float(os.getenv(\"INTENT_CLASSIFY_COST_PER_ARTICLE\"" not in src


def test_intent_type_migration_adds_check_constraint():
    """DB ????? 8 ??? intent_type,???/SQL ????"""
    src = Path("scripts/migration_article_intent_type.sql").read_text(encoding="utf-8")
    assert "geo_research_articles_intent_type_check" in src
    assert "CHECK" in src
    for t in ["ranking", "tutorial", "long_form", "comparison", "data_report", "policy", "definition", "faq"]:
        assert t in src
