"""services/ai_ops/prod_readonly.py 测试(SELECT-only 守卫,无 DB)。"""
import pytest

from services.ai_ops import prod_readonly as ro


def test_accepts_readonly_queries():
    ro.assert_select_only("SELECT 1")
    ro.assert_select_only("  select id from users where id = %s ")
    ro.assert_select_only("WITH x AS (SELECT 1) SELECT * FROM x")
    ro.assert_select_only("EXPLAIN SELECT * FROM ai_ops_tasks")


@pytest.mark.parametrize("bad", [
    "UPDATE users SET is_admin = true",
    "DELETE FROM ai_ops_tasks",
    "DROP TABLE ai_ops_tasks",
    "SELECT 1; DROP TABLE ai_ops_tasks",   # 多语句
    "TRUNCATE ai_ops_tasks",
    "insert into t values (1)",
    "SELECT 1; SELECT 2",
    "",
])
def test_rejects_writes_and_multistatement(bad):
    with pytest.raises(ro.NotReadOnly):
        ro.assert_select_only(bad)


def test_readonly_sql_not_configured(monkeypatch):
    monkeypatch.delenv("AI_OPS_READONLY_DATABASE_URL", raising=False)
    with pytest.raises(ro.ReadOnlyNotConfigured):
        ro.readonly_sql("SELECT 1")


def test_container_status_gated(monkeypatch):
    monkeypatch.setenv("AI_OPS_SSH_RUNNER_ENABLED", "false")
    result = ro.container_status()
    assert result["configured"] is False
