"""
A.7 Group 3 - 调研监测跑批 admin API 单测

用 FastAPI TestClient + mock get_connection + mock service 函数,不依赖真 DB。

覆盖:
- GET 列表 status 过滤 / 分页 / 参数校验
- GET 详情 (mock get_round_status + COUNT SQL)
- POST manual-trigger (预算 ok / 超支 409 / BackgroundTask 调用)
- POST cancel (running ok / completed 409 / 不存在 404)
- POST resume (failed_resumable ok / completed 409)
- GET cost (多项 by_item 聚合)
- 鉴权 (未登录 401 / 非 admin 403)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ==================== Mock Connection 工具 ====================

class MockCursor:
    """
    简易游标 mock:按 execute 调用顺序依次返回 results 列表中的结果。
    每个 result 是 list[dict] (fetchall) 或 dict (fetchone) 或 int (rowcount)。

    用法:
      cur = MockCursor([
          {'cnt': 5},                  # fetchone
          [{'round_id': 'r1', ...}],   # fetchall
      ])
    """

    def __init__(self, results: List[Any]):
        self._results = list(results)
        self._current = None
        self._rowcount_overrides: Dict[int, int] = {}
        self._exec_count = 0
        self.executed_sql: List[str] = []
        self.executed_params: List[Any] = []

    def execute(self, sql, params=None):
        self.executed_sql.append(sql)
        self.executed_params.append(params)
        if self._results:
            self._current = self._results.pop(0)
        else:
            self._current = None
        self._exec_count += 1

    def fetchone(self):
        if isinstance(self._current, list):
            return self._current[0] if self._current else None
        return self._current

    def fetchall(self):
        if isinstance(self._current, list):
            return self._current
        if self._current is None:
            return []
        return [self._current]

    @property
    def rowcount(self):
        # 默认 1, 用例可覆盖
        idx = self._exec_count - 1
        return self._rowcount_overrides.get(idx, 1)

    def set_rowcount_for(self, exec_index: int, value: int):
        self._rowcount_overrides[exec_index] = value


class MockConnection:
    def __init__(self, cursor: MockCursor):
        self._cursor = cursor
        self.committed = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def make_mock_conn(*results) -> MockConnection:
    return MockConnection(MockCursor(list(results)))


# ==================== App fixtures ====================

def _build_app(user: Optional[Dict] = None) -> FastAPI:
    app = FastAPI()
    from api.research_monitor_round_api import router

    @app.middleware("http")
    async def inject_user(request, call_next):
        if user is not None:
            request.state.user = user
        return await call_next(request)

    app.include_router(router)
    return app


@pytest.fixture
def admin_client():
    app = _build_app({"id": 1, "name": "admin_test", "is_admin": True})
    return TestClient(app)


@pytest.fixture
def normal_user_client():
    app = _build_app({"id": 2, "name": "user_test", "is_admin": False})
    return TestClient(app)


@pytest.fixture
def anon_client():
    app = _build_app(user=None)
    return TestClient(app)


# ==================== 鉴权 ====================

class TestAuth:
    def test_未登录_返_401(self, anon_client):
        r = anon_client.get("/api/admin/research-monitor/rounds")
        assert r.status_code == 401
        assert "未登录" in r.json()["detail"]

    def test_非_admin_返_403(self, normal_user_client):
        r = normal_user_client.get("/api/admin/research-monitor/rounds")
        assert r.status_code == 403
        assert "管理员" in r.json()["detail"]


# ==================== GET /rounds 列表 ====================

class TestListRounds:
    def test_默认列表_无过滤(self, admin_client):
        conn = make_mock_conn(
            {"cnt": 2},  # COUNT
            [
                {
                    "round_id": "round_001",
                    "status": "completed",
                    "triggered_by": "cron",
                    "triggered_user_id": None,
                    "current_stage": "stage_8",
                    "started_at": datetime(2026, 5, 1, 10, 0, 0),
                    "finished_at": datetime(2026, 5, 1, 14, 0, 0),
                    "last_heartbeat_at": datetime(2026, 5, 1, 14, 0, 0),
                    "progress_json": {"fetched": 100},
                    "summary_json": {"raw_inserted": 50},
                },
                {
                    "round_id": "round_002",
                    "status": "running",
                    "triggered_by": "manual",
                    "triggered_user_id": 1,
                    "current_stage": "stage_3",
                    "started_at": datetime(2026, 5, 7, 10, 0, 0),
                    "finished_at": None,
                    "last_heartbeat_at": datetime(2026, 5, 7, 10, 5, 0),
                    "progress_json": None,
                    "summary_json": None,
                },
            ],
        )
        with patch("api.research_monitor_round_api.get_connection", return_value=conn):
            r = admin_client.get("/api/admin/research-monitor/rounds")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2
        assert body["limit"] == 20
        assert body["offset"] == 0
        assert len(body["rounds"]) == 2
        assert body["rounds"][0]["round_id"] == "round_001"
        assert body["rounds"][0]["progress_json"] == {"fetched": 100}

    def test_status_过滤_running(self, admin_client):
        conn = make_mock_conn({"cnt": 1}, [{
            "round_id": "round_002",
            "status": "running",
            "triggered_by": "manual",
            "triggered_user_id": 1,
            "current_stage": "stage_3",
            "started_at": datetime(2026, 5, 7, 10, 0, 0),
            "finished_at": None,
            "last_heartbeat_at": datetime(2026, 5, 7, 10, 5, 0),
            "progress_json": None,
            "summary_json": None,
        }])
        with patch("api.research_monitor_round_api.get_connection", return_value=conn):
            r = admin_client.get("/api/admin/research-monitor/rounds?status=running")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        # 验证 SQL 含 status = %s
        assert any("status = %s" in s for s in conn._cursor.executed_sql)

    def test_分页_limit_offset(self, admin_client):
        conn = make_mock_conn({"cnt": 100}, [])
        with patch("api.research_monitor_round_api.get_connection", return_value=conn):
            r = admin_client.get("/api/admin/research-monitor/rounds?limit=10&offset=20")
        assert r.status_code == 200
        body = r.json()
        assert body["limit"] == 10
        assert body["offset"] == 20

    def test_非法_status_返_400(self, admin_client):
        r = admin_client.get("/api/admin/research-monitor/rounds?status=unknown_status")
        assert r.status_code == 400

    def test_非法_triggered_by_返_400(self, admin_client):
        r = admin_client.get("/api/admin/research-monitor/rounds?triggered_by=alien")
        assert r.status_code == 400

    def test_limit_超限_返_400(self, admin_client):
        r = admin_client.get("/api/admin/research-monitor/rounds?limit=999")
        assert r.status_code == 400

    def test_offset_负数_返_400(self, admin_client):
        r = admin_client.get("/api/admin/research-monitor/rounds?offset=-1")
        assert r.status_code == 400


# ==================== GET /rounds/{round_id} 详情 ====================

class TestRoundDetail:
    def test_详情_成功(self, admin_client):
        round_id = "round_test_001"
        round_dict = {
            "round_id": round_id,
            "batch_id": "batch_x",
            "status": "completed",
            "current_stage": "stage_8",
            "progress_json": {"fetched": 100},
            "started_at": datetime(2026, 5, 1, 10, 0),
            "last_heartbeat_at": datetime(2026, 5, 1, 14, 0),
            "finished_at": datetime(2026, 5, 1, 14, 0),
            "summary_json": {"raw_inserted": 50},
        }
        conn = make_mock_conn(
            {"cnt": 42},   # call_count
            {"cnt": 30},   # article_count
            {"total": 130.5},  # cost_total
        )
        with patch("api.research_monitor_round_api.get_round_status", return_value=round_dict), \
             patch("api.research_monitor_round_api.get_round_snapshot",
                   return_value={"industries": [{"id": 1, "name": "金融"}]}), \
             patch("api.research_monitor_round_api.get_connection", return_value=conn):
            r = admin_client.get(f"/api/admin/research-monitor/rounds/{round_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["round_id"] == round_id
        assert body["call_count"] == 42
        assert body["article_count"] == 30
        assert body["cost_total_yuan"] == 130.5
        assert body["snapshot_json"] == {"industries": [{"id": 1, "name": "金融"}]}

    def test_详情_round_不存在_404(self, admin_client):
        with patch("api.research_monitor_round_api.get_round_status", return_value=None):
            r = admin_client.get("/api/admin/research-monitor/rounds/nope")
        assert r.status_code == 404


# ==================== POST /rounds/manual-trigger ====================

class TestManualTrigger:
    def test_预算_ok_启动成功(self, admin_client):
        # 第 1 次 conn: 拉行业 + prompts (2 个 execute)
        # 第 2 次 conn: 写 manual_note (note 为空时不会触发,这里 note 给空)
        conn1 = make_mock_conn(
            [{"id": 1, "name": "金融", "slug": "finance"}],   # industries
            [{"id": 10, "industry_id": 1, "prompt_text": "Q1", "sort_order": 0, "is_sensitive": False}],  # prompts
        )

        with patch("api.research_monitor_round_api.check_month_budget",
                   return_value={"ok": True, "spent": 100, "limit": 1000, "remaining": 900, "reason": None}), \
             patch("api.research_monitor_round_api.get_connection", side_effect=[conn1]), \
             patch("api.research_monitor_round_api.create_round_with_snapshot",
                   return_value="round_new_001") as mock_create, \
             patch("api.research_monitor_round_api.BackgroundTasks.add_task") as mock_bg:
            r = admin_client.post(
                "/api/admin/research-monitor/rounds/manual-trigger",
                json={"industry_ids": None},
            )

        assert r.status_code == 200
        body = r.json()
        assert body["round_id"] == "round_new_001"
        assert body["status"] == "pending"
        assert body["industries_count"] == 1
        assert body["prompts_count"] == 1
        # create_round_with_snapshot 被调用
        mock_create.assert_called_once()
        ck = mock_create.call_args
        assert ck.kwargs["triggered_by"] == "manual"
        assert ck.kwargs["triggered_user_id"] == 1
        # BackgroundTasks 被调用
        assert mock_bg.called

    def test_月预算超支_409(self, admin_client):
        with patch("api.research_monitor_round_api.check_month_budget",
                   return_value={
                       "ok": False, "spent": 1100, "limit": 1000, "remaining": -100,
                       "reason": "月度预算超支 ¥1100/¥1000"
                   }):
            r = admin_client.post(
                "/api/admin/research-monitor/rounds/manual-trigger",
                json={},
            )
        assert r.status_code == 409
        body = r.json()
        assert body["detail"]["code"] == "month_budget_exhausted"

    def test_无_active_行业_400(self, admin_client):
        conn1 = make_mock_conn(
            [],  # industries 空
        )
        with patch("api.research_monitor_round_api.check_month_budget",
                   return_value={"ok": True, "spent": 0, "limit": 1000, "remaining": 1000, "reason": None}), \
             patch("api.research_monitor_round_api.get_connection", return_value=conn1):
            r = admin_client.post(
                "/api/admin/research-monitor/rounds/manual-trigger",
                json={},
            )
        assert r.status_code == 400

    def test_指定_industry_ids_过滤(self, admin_client):
        conn1 = make_mock_conn(
            [{"id": 5, "name": "教育", "slug": "edu"}],
            [{"id": 50, "industry_id": 5, "prompt_text": "Q5", "sort_order": 0, "is_sensitive": False}],
        )
        with patch("api.research_monitor_round_api.check_month_budget",
                   return_value={"ok": True, "spent": 0, "limit": 1000, "remaining": 1000, "reason": None}), \
             patch("api.research_monitor_round_api.get_connection", return_value=conn1), \
             patch("api.research_monitor_round_api.create_round_with_snapshot",
                   return_value="round_new_002"):
            r = admin_client.post(
                "/api/admin/research-monitor/rounds/manual-trigger",
                json={"industry_ids": [5]},
            )
        assert r.status_code == 200
        # 验证 SQL 用 ANY(%s) 过滤
        first_sql = conn1._cursor.executed_sql[0]
        assert "id = ANY(%s)" in first_sql

    def test_无_active_prompts_400(self, admin_client):
        conn1 = make_mock_conn(
            [{"id": 5, "name": "教育", "slug": "edu"}],
            [],
        )
        with patch("api.research_monitor_round_api.check_month_budget",
                   return_value={"ok": True, "spent": 0, "limit": 1000, "remaining": 1000, "reason": None}), \
             patch("api.research_monitor_round_api.get_connection", return_value=conn1):
            r = admin_client.post(
                "/api/admin/research-monitor/rounds/manual-trigger",
                json={"industry_ids": [5]},
            )
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "no_active_prompts"

    def test_已有_running_round_409(self, admin_client):
        from services.research_monitor.round_state import RoundAlreadyRunningError

        conn1 = make_mock_conn(
            [{"id": 1, "name": "金融", "slug": "finance"}],
            [{"id": 10, "industry_id": 1, "prompt_text": "Q1", "sort_order": 0, "is_sensitive": False}],
        )
        with patch("api.research_monitor_round_api.check_month_budget",
                   return_value={"ok": True, "spent": 0, "limit": 1000, "remaining": 1000, "reason": None}), \
             patch("api.research_monitor_round_api.get_connection", return_value=conn1), \
             patch("api.research_monitor_round_api.create_round_with_snapshot",
                   side_effect=RoundAlreadyRunningError("已有运行中的调研跑批")):
            r = admin_client.post(
                "/api/admin/research-monitor/rounds/manual-trigger",
                json={"industry_ids": None},
            )
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "round_already_running"


# ==================== POST /rounds/{round_id}/cancel ====================

class TestCancelRound:
    def test_running_状态_可取消(self, admin_client):
        round_id = "round_cancel_001"
        conn = make_mock_conn({"updated": True})  # UPDATE
        with patch("api.research_monitor_round_api.get_round_status",
                   return_value={"round_id": round_id, "status": "running"}), \
             patch("api.research_monitor_round_api.get_connection", return_value=conn):
            r = admin_client.post(f"/api/admin/research-monitor/rounds/{round_id}/cancel")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "cancelled"
        assert body["cancelled_from"] == "running"

    def test_pending_状态_可取消(self, admin_client):
        round_id = "round_cancel_002"
        conn = make_mock_conn({"updated": True})
        with patch("api.research_monitor_round_api.get_round_status",
                   return_value={"round_id": round_id, "status": "pending"}), \
             patch("api.research_monitor_round_api.get_connection", return_value=conn):
            r = admin_client.post(f"/api/admin/research-monitor/rounds/{round_id}/cancel")
        assert r.status_code == 200

    def test_completed_状态_409(self, admin_client):
        round_id = "round_done"
        with patch("api.research_monitor_round_api.get_round_status",
                   return_value={"round_id": round_id, "status": "completed"}):
            r = admin_client.post(f"/api/admin/research-monitor/rounds/{round_id}/cancel")
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "not_cancellable"

    def test_round_不存在_404(self, admin_client):
        with patch("api.research_monitor_round_api.get_round_status", return_value=None):
            r = admin_client.post("/api/admin/research-monitor/rounds/missing/cancel")
        assert r.status_code == 404

    def test_竞态_rowcount_0_409(self, admin_client):
        """先查到 running 但 UPDATE 时已变(rowcount=0)"""
        round_id = "round_race"
        conn = make_mock_conn({"updated": True})
        conn._cursor.set_rowcount_for(0, 0)  # UPDATE rowcount=0
        with patch("api.research_monitor_round_api.get_round_status",
                   return_value={"round_id": round_id, "status": "running"}), \
             patch("api.research_monitor_round_api.get_connection", return_value=conn):
            r = admin_client.post(f"/api/admin/research-monitor/rounds/{round_id}/cancel")
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "race_status_changed"


# ==================== POST /rounds/{round_id}/resume ====================

class TestResumeRound:
    def test_failed_resumable_可续跑(self, admin_client):
        round_id = "round_resume_001"
        with patch("api.research_monitor_round_api.get_round_status",
                   return_value={"round_id": round_id, "status": "failed_resumable"}), \
             patch("api.research_monitor_round_api.check_month_budget",
                   return_value={"ok": True, "spent": 100, "limit": 1000, "remaining": 900, "reason": None}), \
             patch("api.research_monitor_round_api.get_round_snapshot",
                   return_value={"industries": [{"id": 1, "name": "金融"}],
                                 "prompts_by_industry": {"1": [{"id": 10, "text": "Q1"}]}}), \
             patch("api.research_monitor_round_api.mark_round_resume_requested",
                   return_value=True) as mock_mark, \
             patch("api.research_monitor_round_api.BackgroundTasks.add_task") as mock_bg:
            r = admin_client.post(f"/api/admin/research-monitor/rounds/{round_id}/resume")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "pending"
        assert body["resumed_from"] == "stage_1"
        mock_mark.assert_called_once_with(round_id, requested_by=1)
        assert mock_bg.called

    def test_月预算超支_不可续跑_409(self, admin_client):
        round_id = "round_resume_budget"
        with patch("api.research_monitor_round_api.get_round_status",
                   return_value={"round_id": round_id, "status": "failed_resumable"}), \
             patch("api.research_monitor_round_api.get_round_snapshot",
                   return_value={"industries": [{"id": 1, "name": "金融"}],
                                 "prompts_by_industry": {"1": [{"id": 10, "text": "Q1"}]}}), \
             patch("api.research_monitor_round_api.check_month_budget",
                   return_value={
                       "ok": False,
                       "spent": 1100,
                       "limit": 1000,
                       "remaining": -100,
                       "reason": "月度预算超支 ¥1100/¥1000",
                   }), \
             patch("api.research_monitor_round_api.BackgroundTasks.add_task") as mock_bg:
            r = admin_client.post(f"/api/admin/research-monitor/rounds/{round_id}/resume")
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "month_budget_exhausted"
        mock_bg.assert_not_called()

    def test_已有_active_round_不可续跑_409(self, admin_client):
        from services.research_monitor.round_state import RoundAlreadyRunningError

        round_id = "round_resume_active"
        with patch("api.research_monitor_round_api.get_round_status",
                   return_value={"round_id": round_id, "status": "failed_resumable"}), \
             patch("api.research_monitor_round_api.check_month_budget",
                   return_value={"ok": True, "spent": 0, "limit": 1000, "remaining": 1000, "reason": None}), \
             patch("api.research_monitor_round_api.get_round_snapshot",
                   return_value={"industries": [{"id": 1, "name": "金融"}],
                                 "prompts_by_industry": {"1": [{"id": 10, "text": "Q1"}]}}), \
             patch("api.research_monitor_round_api.mark_round_resume_requested",
                   side_effect=RoundAlreadyRunningError("已有运行中的调研跑批")), \
             patch("api.research_monitor_round_api.BackgroundTasks.add_task") as mock_bg:
            r = admin_client.post(f"/api/admin/research-monitor/rounds/{round_id}/resume")
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "round_already_running"
        mock_bg.assert_not_called()

    def test_resume_竞态_status_changed_409(self, admin_client):
        round_id = "round_resume_race"
        with patch("api.research_monitor_round_api.get_round_status",
                   return_value={"round_id": round_id, "status": "failed_resumable"}), \
             patch("api.research_monitor_round_api.check_month_budget",
                   return_value={"ok": True, "spent": 0, "limit": 1000, "remaining": 1000, "reason": None}), \
             patch("api.research_monitor_round_api.get_round_snapshot",
                   return_value={"industries": [{"id": 1, "name": "金融"}],
                                 "prompts_by_industry": {"1": [{"id": 10, "text": "Q1"}]}}), \
             patch("api.research_monitor_round_api.mark_round_resume_requested",
                   return_value=False), \
             patch("api.research_monitor_round_api.BackgroundTasks.add_task") as mock_bg:
            r = admin_client.post(f"/api/admin/research-monitor/rounds/{round_id}/resume")
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "race_status_changed"
        mock_bg.assert_not_called()

    def test_completed_不可续跑_409(self, admin_client):
        with patch("api.research_monitor_round_api.get_round_status",
                   return_value={"round_id": "x", "status": "completed"}):
            r = admin_client.post("/api/admin/research-monitor/rounds/x/resume")
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "not_resumable"

    def test_round_不存在_404(self, admin_client):
        with patch("api.research_monitor_round_api.get_round_status", return_value=None):
            r = admin_client.post("/api/admin/research-monitor/rounds/missing/resume")
        assert r.status_code == 404

    def test_snapshot_缺失_409(self, admin_client):
        round_id = "round_no_snap"
        with patch("api.research_monitor_round_api.get_round_status",
                   return_value={"round_id": round_id, "status": "failed_resumable"}), \
             patch("api.research_monitor_round_api.get_round_snapshot", return_value=None):
            r = admin_client.post(f"/api/admin/research-monitor/rounds/{round_id}/resume")
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "snapshot_missing"


# ==================== GET /rounds/{round_id}/cost ====================

class TestRoundCost:
    def test_成本聚合_多_item(self, admin_client):
        round_id = "round_cost_001"
        conn = make_mock_conn([
            {"item": "doubao", "total": 130.5, "records": 425},
            {"item": "qwen-plus", "total": 80.0, "records": 200},
            {"item": "deepseek", "total": 40.0, "records": 100},
        ])
        with patch("api.research_monitor_round_api.get_round_status",
                   return_value={"round_id": round_id, "status": "completed"}), \
             patch("api.research_monitor_round_api.get_round_cost", return_value=250.5), \
             patch("api.research_monitor_round_api.get_connection", return_value=conn), \
             patch("api.research_monitor_round_api.check_month_budget",
                   return_value={"ok": True, "spent": 250.5, "limit": 1000,
                                 "remaining": 749.5, "reason": None}):
            r = admin_client.get(f"/api/admin/research-monitor/rounds/{round_id}/cost")
        assert r.status_code == 200
        body = r.json()
        assert body["round_id"] == round_id
        assert body["total_yuan"] == 250.5
        assert len(body["by_item"]) == 3
        assert body["by_item"][0]["item"] == "doubao"
        assert body["by_item"][0]["total"] == 130.5
        assert body["by_item"][0]["records"] == 425
        assert body["month_budget_remaining"] == 749.5

    def test_round_不存在_404(self, admin_client):
        with patch("api.research_monitor_round_api.get_round_status", return_value=None):
            r = admin_client.get("/api/admin/research-monitor/rounds/missing/cost")
        assert r.status_code == 404

    def test_无成本明细_返回空_by_item(self, admin_client):
        round_id = "round_no_cost"
        conn = make_mock_conn([])
        with patch("api.research_monitor_round_api.get_round_status",
                   return_value={"round_id": round_id, "status": "pending"}), \
             patch("api.research_monitor_round_api.get_round_cost", return_value=0.0), \
             patch("api.research_monitor_round_api.get_connection", return_value=conn), \
             patch("api.research_monitor_round_api.check_month_budget",
                   return_value={"ok": True, "spent": 0, "limit": 1000,
                                 "remaining": 1000, "reason": None}):
            r = admin_client.get(f"/api/admin/research-monitor/rounds/{round_id}/cost")
        assert r.status_code == 200
        body = r.json()
        assert body["total_yuan"] == 0.0
        assert body["by_item"] == []


# ==================== A.7 Phase 1 修复审计 · 新增覆盖 ====================


class _RaisingConn:
    """conn mock: cursor.execute 第一次抛指定异常, 验证 rollback 被调用。"""

    def __init__(self, exc: Exception):
        self._exc = exc
        self.rollback_called = False
        self.closed = False
        self.committed = False
        self._cursor = MagicMock()
        # cursor.execute 抛异常
        self._cursor.execute = MagicMock(side_effect=self._exc)
        self._cursor.fetchone = MagicMock(return_value=None)
        self._cursor.fetchall = MagicMock(return_value=[])
        self._cursor.rowcount = 0
        self._cursor.close = MagicMock()

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rollback_called = True

    def close(self):
        self.closed = True


class TestRollbackOnDbError:
    """C-2: finally 兜底 rollback / C-3: detail generic"""

    def test_round_detail_db_error_calls_rollback(self, admin_client):
        """get_round_detail SQL 异常 → finally 触发 rollback (防连接池污染)."""
        round_id = "round_rollback_001"
        conn = _RaisingConn(RuntimeError("connection lost during count"))
        with patch("api.research_monitor_round_api.get_round_status",
                   return_value={"round_id": round_id, "status": "completed"}), \
             patch("api.research_monitor_round_api.get_round_snapshot",
                   return_value={}), \
             patch("api.research_monitor_round_api.get_connection", return_value=conn):
            r = admin_client.get(f"/api/admin/research-monitor/rounds/{round_id}")
        assert r.status_code == 500, r.text
        # 关键: finally rollback 必须被触发
        assert conn.rollback_called is True
        assert conn.closed is True
        # detail 必须 generic 不含异常细节
        detail = r.json().get("detail", "")
        assert "connection lost" not in detail
        assert "RuntimeError" not in detail


class TestManualTriggerValidation:
    """W-7: industry_ids max_length=100"""

    def test_manual_trigger_industry_ids_too_long_returns_422(self, admin_client):
        """传 101 个 industry_id → Pydantic 422 (max_length=100)."""
        too_many = list(range(1, 102))  # 101 个
        r = admin_client.post(
            "/api/admin/research-monitor/rounds/manual-trigger",
            json={"industry_ids": too_many},
        )
        assert r.status_code == 422, r.text

    def test_manual_trigger_industry_ids_exactly_100_passes_validation(self, admin_client):
        """正好 100 个 → 通过 Pydantic 校验 (业务侧再处理)."""
        exactly_100 = list(range(1, 101))
        # 月预算 ok 但行业列表为空 → 走到 400 (没有 active 行业), 证明已过 Pydantic
        conn1 = make_mock_conn([])  # industries 空
        with patch("api.research_monitor_round_api.check_month_budget",
                   return_value={"ok": True, "spent": 0, "limit": 1000,
                                 "remaining": 1000, "reason": None}), \
             patch("api.research_monitor_round_api.get_connection", return_value=conn1):
            r = admin_client.post(
                "/api/admin/research-monitor/rounds/manual-trigger",
                json={"industry_ids": exactly_100},
            )
        # 不是 422 即证 Pydantic 放过, 业务层返 400
        assert r.status_code != 422
        assert r.status_code == 400


class TestCreateRoundFailureDetailGeneric:
    """C-3: create_round_with_snapshot 失败 detail 不暴露 e."""

    def test_create_round_failure_detail_is_generic(self, admin_client):
        """create_round_with_snapshot 抛异常 → 500 + detail 不含 e."""
        conn1 = make_mock_conn(
            [{"id": 1, "name": "金融", "slug": "finance"}],
            [{"id": 10, "industry_id": 1, "prompt_text": "Q1",
              "sort_order": 0, "is_sensitive": False}],
        )
        secret_msg = "FATAL: snapshot_oss_bucket_name='internal-bucket' 写失败"
        with patch("api.research_monitor_round_api.check_month_budget",
                   return_value={"ok": True, "spent": 0, "limit": 1000,
                                 "remaining": 1000, "reason": None}), \
             patch("api.research_monitor_round_api.get_connection", return_value=conn1), \
             patch("api.research_monitor_round_api.create_round_with_snapshot",
                   side_effect=RuntimeError(secret_msg)):
            r = admin_client.post(
                "/api/admin/research-monitor/rounds/manual-trigger",
                json={"industry_ids": None},
            )
        assert r.status_code == 500, r.text
        detail = r.json().get("detail", "")
        assert detail == "创建 round 失败"
        # 内部细节绝不能泄漏
        assert "snapshot_oss_bucket_name" not in detail
        assert "internal-bucket" not in detail
        assert "FATAL" not in detail
        assert "RuntimeError" not in detail
