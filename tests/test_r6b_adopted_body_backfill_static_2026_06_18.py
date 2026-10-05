from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backfill_adopted_url_bodies_2026_06_18.py"


def _source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_r6b_body_backfill_is_dry_run_by_default_and_shadow_only():
    source = _source()

    assert "dry_run=not args.full" in source
    assert 'parser.add_argument("--full"' in source
    assert '"production_takeover": False' in source
    assert '"shadow_only": True' in source
    assert '"dry_run": dry_run' in source


def test_r6b_body_backfill_reuses_jina_crawler_without_global_proxy():
    source = _source()

    assert "from services.research_monitor.crawler import crawl_article" in source
    assert "JINA_HTTP_PROXY" not in source
    assert "HTTP_PROXY" not in source
    assert "HTTPS_PROXY" not in source
    assert "ALL_PROXY" not in source
    assert "os.environ[" not in source
    assert "stage3_crawl_articles" not in source
    assert "round_runner" not in source


def test_r6b_body_backfill_only_writes_research_articles_and_citation_links():
    source = _source()

    assert "geo_research_articles" in source
    assert "geo_research_article_citations" in source
    assert "geo_research_round" in source
    assert "raw_id" in source
    assert "rank_in_response" in source
    assert "inline_cleaned_content" in source
    assert "cleaned_char_count" in source
    assert "raw_char_count" in source
    assert "content_hash" in source
    assert "geo_research_raw" in source
    assert "geo_research_source_signals" in source
    assert "UPDATE geo_research_raw" not in source
    assert "upsert_source_signal" not in source
    assert "geo_answer_adoption_metrics" not in source
    assert "writing_strategy_versions" not in source
    assert "DELETE FROM" not in source
    assert "TRUNCATE" not in source
    assert "DROP " not in source


def test_r6b_body_backfill_does_not_treat_batch_id_as_round_id():
    source = _source()

    assert "raw.batch_id AS round_id" not in source
    assert "raw.batch_id AS batch_id" in source
    assert "rnd.round_id AS round_id" in source
    assert "LEFT JOIN geo_research_round rnd ON rnd.batch_id = raw.batch_id" in source
    assert "INSERT INTO geo_research_round" in source
    assert "round_r6b_" in source


def test_r6b_body_backfill_reports_operational_counters():
    source = _source()

    for key in (
        "would_crawl",
        "crawled_new",
        "reused_same_industry",
        "copied_cross_industry",
        "skipped_short",
        "skipped_jina_failed",
        "skipped_missing_round",
        "created_backfill_rounds",
        "by_domain",
        "by_industry",
        "cap_hit",
    ):
        assert f'"{key}"' in source


def test_r6b_body_backfill_uses_same_url_and_content_hash_helpers():
    source = _source()

    assert "from services.research_monitor.url_normalizer import compute_url_hash, normalize_url" in source
    assert "from services.research_monitor.content_hash import compute_content_hash" in source
    assert "ON CONFLICT DO NOTHING" in source
    assert "primary_industry" in source
