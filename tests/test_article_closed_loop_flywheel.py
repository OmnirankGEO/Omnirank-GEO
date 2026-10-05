from services.article_evolution_cycle import load_correction_candidate_summary


class _Cursor:
    def __init__(self):
        self.sql = ""

    def execute(self, sql, params=()):
        self.sql = sql

    def fetchall(self):
        return [
            {"key": "factual_correction", "count": 3},
            {"key": "title_or_style_correction", "count": 2},
        ]


def test_agent_corrections_are_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ARTICLE_FLYWHEEL_CANDIDATE_ENABLED", raising=False)
    cursor = _Cursor()
    result = load_correction_candidate_summary(cursor)
    assert result == {
        "source": "disabled",
        "candidate_only": True,
        "automatic_template_writeback_allowed": False,
        "counts": {},
    }
    assert cursor.sql == ""


def test_agent_corrections_enter_only_the_manual_candidate_input(monkeypatch):
    monkeypatch.setenv("ARTICLE_FLYWHEEL_CANDIDATE_ENABLED", "true")
    cursor = _Cursor()
    result = load_correction_candidate_summary(cursor)
    assert result["source"] == "tenant_scoped_candidate_only"
    assert result["candidate_only"] is True
    assert result["automatic_template_writeback_allowed"] is False
    assert result["counts"] == {"factual_correction": 3, "title_or_style_correction": 2}
    assert "c.candidate_status='candidate'" in cursor.sql
    assert "c.occurred_at <= CURRENT_TIMESTAMP" in cursor.sql
    assert "COALESCE(b.is_deleted,FALSE)=FALSE" in cursor.sql
    assert "c.tenant_owner_user_id=COALESCE(q.owner_user_id,b.owner_user_id)" in cursor.sql
