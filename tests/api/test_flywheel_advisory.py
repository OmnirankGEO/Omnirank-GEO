from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException


class _State:
    def __init__(self, user):
        self.user = user


class _Request:
    def __init__(self, user):
        self.state = _State(user)


class FakeCursor:
    def __init__(self, mode="admin_shadow"):
        self.mode = mode
        self.result = None
        self.rows = []
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "FROM geo_research_config" in sql and "SELECT" in sql:
            self.result = {"value_json": f'"{self.mode}"'}
            self.rows = []
        elif "FROM geo_research_round" in sql and "status IN" in sql:
            self.result = {
                "round_id": "round_test_001",
                "batch_id": "batch_round_test_001",
                "status": "completed",
                "started_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
                "finished_at": datetime(2026, 7, 1, 1, tzinfo=timezone.utc),
                "summary_json": {"total_cost_yuan": 18.16},
            }
            self.rows = []
        elif "FROM geo_research_raw" in sql:
            self.result = {"cnt": 763}
            self.rows = []
        elif "COUNT(*) AS stats_rows" in sql:
            self.result = {
                "stats_rows": 365,
                "latest_stats_at": datetime(2026, 7, 1, 2, tzinfo=timezone.utc),
                "candidate_count": 42,
            }
            self.rows = []
        elif "WHERE status = %s" in sql and "failed_resumable" in str(params):
            self.result = {"cnt": 0}
            self.rows = []
        elif "FROM geo_engine_stats" in sql:
            self.result = None
            self.rows = [
                {
                    "industry": "GEO优化服务",
                    "engine": "doubao",
                    "platform": "example.com",
                    "citation_count": 12,
                    "total_queries": 20,
                    "citation_rate": 0.6,
                    "avg_position": 2.5,
                    "sample_queries": ["GEO 服务怎么选"],
                    "last_updated": datetime(2026, 7, 1, 2, tzinfo=timezone.utc),
                }
            ]
        elif "INSERT INTO geo_research_config" in sql:
            self.result = None
            self.rows = []
        else:
            self.result = {}
            self.rows = []

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, mode="admin_shadow"):
        self.mode = mode
        self.closed = False
        self.committed = False
        self.rolled_back = False
        self.cursor_obj = FakeCursor(mode)

    def cursor(self, *args, **kwargs):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_candidates_empty_when_mode_off(monkeypatch):
    from services import flywheel_advisory

    monkeypatch.setattr(flywheel_advisory, "get_connection", lambda: FakeConnection(mode="off"))

    result = flywheel_advisory.get_advisory_candidates(industry="GEO优化服务", limit=20)

    assert result["mode"] == "off"
    assert result["items"] == []
    assert result["observability"]["raw_rows"] == 763
    assert result["observability"]["stats_rows"] == 365
    assert result["production_takeover"] is False


def test_candidates_available_in_admin_shadow(monkeypatch):
    from services import flywheel_advisory

    monkeypatch.setattr(flywheel_advisory, "get_connection", lambda: FakeConnection(mode="admin_shadow"))

    result = flywheel_advisory.get_advisory_candidates(industry="GEO优化服务", limit=20)

    assert result["mode"] == "admin_shadow"
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["source"]["type"] == "flywheel_stats"
    assert item["source"]["round_id"] == "round_test_001"
    assert item["citation_rate"] == 0.6


def test_observability_endpoint_requires_admin():
    from api.media_entity_flywheel_api import flywheel_advisory_observability

    with pytest.raises(HTTPException) as exc:
        asyncio.run(flywheel_advisory_observability(_Request({"id": 2, "is_admin": False})))
    assert exc.value.status_code == 403


def test_advisory_mode_requires_confirmation():
    from api.media_entity_flywheel_api import FlywheelAdvisoryModeRequest, update_flywheel_advisory_mode

    req = FlywheelAdvisoryModeRequest(mode="advisory", confirm="")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(update_flywheel_advisory_mode(req, _Request({"id": 1, "is_admin": True})))
    assert exc.value.status_code == 409


def test_flywheel_advisory_routes_registered():
    from api.media_entity_flywheel_api import router

    paths = [route.path for route in router.routes]
    assert "/api/admin/geo-placement-flywheel/advisory/observability" in paths
    assert "/api/admin/geo-placement-flywheel/advisory/candidates" in paths
    assert "/api/admin/geo-placement-flywheel/advisory-mode" in paths


def test_age_hours_accepts_naive_timestamp():
    # 回归 P0-1: geo_engine_stats.last_updated 是 TIMESTAMP(无时区),psycopg2 返回
    # naive datetime;aware-naive 相减曾抛 TypeError 使 advisory 端点 500。
    from services.flywheel_advisory import _age_hours

    naive = _age_hours(datetime(2026, 7, 1, 9, 4, 54))
    aware = _age_hours(datetime(2026, 7, 1, 9, 4, 54, tzinfo=timezone.utc))
    assert isinstance(naive, float) and naive >= 0
    assert isinstance(aware, float) and aware >= 0
    assert _age_hours(None) is None


def test_observability_with_naive_stats_timestamp(monkeypatch):
    # 回归 P0-1 端到端: 观测聚合遇到 naive MAX(last_updated) 不再崩。
    from services import flywheel_advisory

    class NaiveCursor(FakeCursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if "COUNT(*) AS stats_rows" in sql:
                self.result = {
                    "stats_rows": 365,
                    "latest_stats_at": datetime(2026, 7, 1, 2, 0, 0),  # naive
                    "candidate_count": 42,
                }

    class NaiveConnection(FakeConnection):
        def __init__(self):
            super().__init__(mode="admin_shadow")
            self.cursor_obj = NaiveCursor("admin_shadow")

    monkeypatch.setattr(flywheel_advisory, "get_connection", lambda: NaiveConnection())
    result = flywheel_advisory.get_flywheel_observability()
    assert result["stats_rows"] == 365
    assert isinstance(result["latest_stats_age_hours"], float)


def test_candidates_industry_filter_expands_aliases(monkeypatch):
    # 回归 P1-1: 归一化 slug 必须展开为含中文行业名的 ANY 数组,且 LOWER 两侧比较。
    from services import flywheel_advisory

    conn = FakeConnection(mode="admin_shadow")
    monkeypatch.setattr(flywheel_advisory, "get_connection", lambda: conn)

    flywheel_advisory.get_advisory_candidates(industry="tourism_hotel", limit=20)

    stats_calls = [(sql, params) for sql, params in conn.cursor_obj.executed if "FROM geo_engine_stats" in sql]
    assert stats_calls, "未发起 geo_engine_stats 查询"
    sql, params = stats_calls[-1]
    assert "LOWER(industry) = ANY(%s)" in sql
    industry_array = params[0]
    assert isinstance(industry_array, list)
    assert "旅游酒店" in industry_array
    assert "tourism_hotel" in industry_array


def test_candidates_general_scope_has_no_industry_filter(monkeypatch):
    from services import flywheel_advisory

    conn = FakeConnection(mode="admin_shadow")
    monkeypatch.setattr(flywheel_advisory, "get_connection", lambda: conn)

    flywheel_advisory.get_advisory_candidates(industry="", limit=20)

    stats_calls = [(sql, params) for sql, params in conn.cursor_obj.executed if "FROM geo_engine_stats" in sql]
    assert stats_calls
    sql, params = stats_calls[-1]
    assert "LOWER(industry) = ANY(%s)" not in sql
    assert len(params) == 4  # platform×3 + limit,无行业数组


def test_shadow_score_accepts_decimal_confidence():
    # 回归 P0-2: DB NUMERIC confidence(Decimal)×float 系数曾抛 TypeError 使 approve 500。
    from decimal import Decimal

    from services.media_entity_flywheel import compute_media_entity_shadow_score

    result = compute_media_entity_shadow_score(
        entity={"entity_key": "me_test", "canonical_name": "测试媒体", "confidence": Decimal("0.70")},
        citation_rollup={"answer_adopted_count": 3, "cited_count": 5, "prompt_count": 20, "engine_count": 2},
        inventory_matches=[],
    )
    assert isinstance(result["shadow_score"], float)
    assert result["shadow_score"] > 0
