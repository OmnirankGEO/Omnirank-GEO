from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "analyze_article_structure_patterns_2026_06_17.py"
SERVICE_FILE = ROOT / "services" / "article_structure_analysis.py"
DB_FILE = ROOT / "db" / "writing_style_flywheel_db.py"
API_FILE = ROOT / "api" / "writing_style_flywheel_api.py"
FRONTEND_FILE = ROOT / "frontend" / "src" / "pages" / "Admin" / "GeoPlacementFlywheel.tsx"


def test_article_structure_analysis_script_is_read_only_and_adoption_first():
    script_text = SCRIPT.read_text(encoding="utf-8")
    assert "from services.article_structure_analysis import analyze_article_structure_patterns" in script_text

    text = SERVICE_FILE.read_text(encoding="utf-8")
    upper = text.upper()

    assert "INSERT INTO" not in upper
    assert "UPDATE " not in upper
    assert "DELETE FROM" not in upper
    assert "DROP " not in upper
    assert "TRUNCATE" not in upper
    assert "source_signal_by_url" in text
    assert "GROUP BY source_url, industry_key" in text
    assert "signal_tier = 'answer_adopted'" in text
    assert "signal_tier = 'search_result_only'" in text
    assert "adopted_group" in text
    assert "cited_group" in text
    assert "search_only_control_group" in text
    assert "production_takeover" in text


def test_style_feature_rebuild_exposes_article_structure_without_new_table():
    db_text = DB_FILE.read_text(encoding="utf-8")

    assert "extract_article_structure_features" in db_text
    assert "article_structure_summary" in db_text
    assert "writing_style_feature_snapshots" in db_text
    assert "CREATE TABLE IF NOT EXISTS article_structure" not in db_text


def test_admin_api_exposes_dry_run_article_structure_endpoint():
    api_text = API_FILE.read_text(encoding="utf-8")

    assert '"/writing/article-structure/analyze"' in api_text
    assert "ArticleStructureAnalyzeRequest" in api_text
    assert "services.article_structure_analysis import analyze_article_structure_patterns" in api_text
    assert "scripts.analyze_article_structure_patterns" not in api_text
    assert "article_structure_analysis" in api_text
    assert "dry_run: bool = True" in api_text
    assert "production_takeover" in api_text


def test_frontend_has_operator_friendly_article_structure_research_view():
    page = FRONTEND_FILE.read_text(encoding="utf-8")

    assert "文章结构研究" in page
    assert "已读取文章" in page
    assert "采纳组占比" in page
    assert "对照组占比" in page
    assert "结构提升倍数" in page
    assert "样本观察中" in page
    assert "写作策略使用全部文章" in page
    assert ">answer_adopted<" not in page
    assert ">search_result_only<" not in page


def test_general_article_structure_falls_back_to_generated_articles():
    text = SERVICE_FILE.read_text(encoding="utf-8")

    assert "GENERATED_ARTICLES_ALL_SQL" in text
    assert "FROM articles article" in text
    assert "generated_article_count" in text
    assert "geo_research_articles + geo_research_source_signals + articles" in text
