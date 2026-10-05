from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")


def _handler(name: str, next_marker: str) -> str:
    start = SERVER.index(name)
    end = SERVER.index(next_marker, start)
    return SERVER[start:end]


def test_legacy_brand_hard_delete_is_retired_before_any_body_or_database_access():
    block = _handler("async def api_delete_brand", '@app.post("/api/brands/merge")')
    retired = block.index("brand_hard_delete_retired")
    assert "status_code=410" in block[: retired + 120]
    assert "await request.json" not in block
    assert "get_connection" not in block
    assert "DELETE FROM" not in block
    assert "DELETE /api/my-clients/" in block[: retired + 300]


def test_generated_topics_cannot_be_physically_deleted_or_detached_by_reset():
    delete_block = _handler("def api_delete_topic", "class RegenerateRequest")
    assert "topic_has_article_fact" in delete_block
    assert delete_block.index("topic_has_article_fact") < delete_block.index('cur.execute("DELETE FROM topics')

    reset_block = _handler("def api_reset_to_pending", "def _require_article_access")
    assert "generated_articles_require_rewrite" in reset_block
    assert "SET status = 'pending', reviewed_at = NULL, completed_at = NULL" in reset_block
    assert "article_id = NULL" not in reset_block
