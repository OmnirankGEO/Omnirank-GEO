"""
里程碑 A.7 Group 4 · 调研监测配置 API 单测

策略:
  - FastAPI TestClient + 一个最小 app(用 middleware 注入 request.state.user)
  - mock db.connection.get_connection 返回伪 conn/cur,不打真实 DB
  - 覆盖鉴权 / GET / 单项 GET / PUT 类型校验 / POST reset 共 14 个 case
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

# 把项目根加入 sys.path
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ============================================================
# 工具: 构造 fake conn/cur(模拟 RealDictCursor)
# ============================================================

def _make_conn(rows_queue=None, rowcount=1):
    """
    rows_queue: list, 按 cur.execute 顺序消费; 每项是 fetchone/fetchall 的返回值
                None 表示该次 execute 不期望被 fetch
    rowcount: cur.rowcount 返回值
    """
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    cur.rowcount = rowcount

    queue = list(rows_queue or [])

    def _execute(*args, **kwargs):
        cur._next_row = queue.pop(0) if queue else None

    cur.execute.side_effect = _execute
    cur.fetchone.side_effect = lambda: cur._next_row if not isinstance(cur._next_row, list) else (cur._next_row[0] if cur._next_row else None)
    cur.fetchall.side_effect = lambda: cur._next_row if isinstance(cur._next_row, list) else ([] if cur._next_row is None else [cur._next_row])

    return conn, cur


# ============================================================
# 测试用 app
# ============================================================

def _build_app(is_admin: bool = True, logged_in: bool = True):
    app = FastAPI()

    class _InjectUserMW(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if logged_in:
                request.state.user = {
                    "id": 9_777_001,
                    "username": "admin_test" if is_admin else "user_test",
                    "is_admin": is_admin,
                }
            return await call_next(request)

    app.add_middleware(_InjectUserMW)
    from api.research_monitor_config_api import router
    app.include_router(router)
    return app


@pytest.fixture
def admin_client():
    return TestClient(_build_app(is_admin=True))


@pytest.fixture
def normal_client():
    return TestClient(_build_app(is_admin=False))


@pytest.fixture
def anon_client():
    return TestClient(_build_app(logged_in=False))


# ============================================================
# 1) 鉴权
# ============================================================

def test_anon_get_config_blocked(anon_client):
    r = anon_client.get("/api/admin/research-monitor/config")
    assert r.status_code == 401, r.text


def test_normal_user_get_config_blocked(normal_client):
    r = normal_client.get("/api/admin/research-monitor/config")
    assert r.status_code == 403, r.text


def test_normal_user_put_config_blocked(normal_client):
    r = normal_client.put(
        "/api/admin/research-monitor/config/budget_per_round_yuan",
        json={"value": 500},
    )
    assert r.status_code == 403, r.text


def test_normal_user_reset_blocked(normal_client):
    r = normal_client.post(
        "/api/admin/research-monitor/config/reset",
        json={"confirm_code": "RESET_RESEARCH_MONITOR_CONFIG"},
    )
    assert r.status_code == 403, r.text


# ============================================================
# 2) GET /config 全列表
# ============================================================

def test_admin_list_configs(admin_client):
    fake_now = datetime(2026, 5, 7, 10, 0, 0)
    rows = [
        {"key": "budget_per_round_yuan", "value_json": 350, "description": "单轮预算上限(元)",
         "updated_by": "seed", "updated_at": fake_now},
        {"key": "circuit_breaker_consecutive", "value_json": 50, "description": "连续失败 N 次熔断",
         "updated_by": "seed", "updated_at": fake_now},
    ]
    conn, cur = _make_conn(rows_queue=[rows])
    with patch("db.connection.get_connection", return_value=conn):
        r = admin_client.get("/api/admin/research-monitor/config")
    assert r.status_code == 200, r.text
    data = r.json()
    assert "configs" in data
    assert len(data["configs"]) == 2
    assert data["configs"][0]["key"] == "budget_per_round_yuan"
    assert data["configs"][0]["value"] == 350


# ============================================================
# 3) GET /config/{key}
# ============================================================

def test_admin_get_single_config(admin_client):
    fake_now = datetime(2026, 5, 7, 10, 0, 0)
    row = {
        "key": "budget_per_round_yuan",
        "value_json": 350,
        "description": "单轮预算上限(元)",
        "updated_by": "seed",
        "updated_at": fake_now,
    }
    conn, _ = _make_conn(rows_queue=[row])
    with patch("db.connection.get_connection", return_value=conn):
        r = admin_client.get("/api/admin/research-monitor/config/budget_per_round_yuan")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["key"] == "budget_per_round_yuan"
    assert body["value"] == 350


def test_admin_get_unknown_key_404(admin_client):
    conn, _ = _make_conn(rows_queue=[None])
    with patch("db.connection.get_connection", return_value=conn):
        r = admin_client.get("/api/admin/research-monitor/config/no_such_key")
    assert r.status_code == 404, r.text


# ============================================================
# 4) PUT /config/{key} 类型校验
# ============================================================

def test_put_unknown_key_returns_404(admin_client):
    # unknown key 在校验前就 404,不需要走 DB(但 mock 防意外)
    conn, _ = _make_conn(rows_queue=[None])
    with patch("db.connection.get_connection", return_value=conn):
        r = admin_client.put(
            "/api/admin/research-monitor/config/no_such_key",
            json={"value": 123},
        )
    assert r.status_code == 404, r.text


def test_put_budget_string_rejected_400(admin_client):
    r = admin_client.put(
        "/api/admin/research-monitor/config/budget_per_round_yuan",
        json={"value": "not_a_number"},
    )
    assert r.status_code == 400, r.text
    assert "类型错误" in r.json()["detail"] or "type" in r.json()["detail"].lower()


def test_put_circuit_breaker_consecutive_negative_400(admin_client):
    r = admin_client.put(
        "/api/admin/research-monitor/config/circuit_breaker_consecutive",
        json={"value": -1},
    )
    assert r.status_code == 400, r.text
    assert "≥" in r.json()["detail"] or ">=" in r.json()["detail"]


def test_put_circuit_breaker_rate_above_one_400(admin_client):
    r = admin_client.put(
        "/api/admin/research-monitor/config/circuit_breaker_rate",
        json={"value": 1.5},
    )
    assert r.status_code == 400, r.text


def test_put_budget_happy_path(admin_client):
    fake_now = datetime(2026, 5, 7, 10, 0, 0)
    old_row = {
        "key": "budget_per_round_yuan",
        "value_json": 350,
        "description": "单轮预算上限(元)",
        "updated_by": "seed",
        "updated_at": fake_now,
    }
    new_row = {
        "key": "budget_per_round_yuan",
        "value_json": 500,
        "description": "单轮预算上限(元)",
        "updated_by": "9777001",
        "updated_at": fake_now,
    }
    # 两次 fetchone: SELECT FOR UPDATE -> old, UPDATE RETURNING -> new
    conn, _ = _make_conn(rows_queue=[old_row, new_row])
    with patch("db.connection.get_connection", return_value=conn):
        r = admin_client.put(
            "/api/admin/research-monitor/config/budget_per_round_yuan",
            json={"value": 500, "note": "扩容预算"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["value"] == 500
    assert body["old_value"] == 350
    assert body["key"] == "budget_per_round_yuan"


def test_put_bool_rejected(admin_client):
    # 防 bool-as-int
    r = admin_client.put(
        "/api/admin/research-monitor/config/circuit_breaker_consecutive",
        json={"value": True},
    )
    assert r.status_code == 400, r.text


# ============================================================
# 5) POST /config/reset
# ============================================================

def test_reset_wrong_confirm_code_403(admin_client):
    r = admin_client.post(
        "/api/admin/research-monitor/config/reset",
        json={"confirm_code": "WRONG"},
    )
    assert r.status_code == 403, r.text


def test_reset_all_keys_returns_10(admin_client):
    # 取原值: 返回 10 行(模拟全部 seed 都在)
    old_rows = [
        {"key": k, "value_json": v}
        # P13-v13 (2026-05-27 HIGH review fix): 旧值锁删 (clean_attempts_max / undo_window_minutes / min_chars=3000)
        # 新值锁加: 4 个 model_* + min_chars=100 · 共 12 个 (跟 DEFAULT_CONFIGS 一致)
        for k, v in {
            'circuit_breaker_consecutive': 50,
            'circuit_breaker_rate': 0.5,
            'circuit_breaker_min_processed': 100,
            'budget_per_round_yuan': 350,
            'budget_per_month_yuan': 1000,
            'article_oss_ttl_days': 180,
            'lock_stale_minutes': 5,
            'article_min_chars_for_review': 100,                              # 3000 → 100 (Phase 9)
            'model_doubao_app': 'doubao-seed-2-0-lite-260215',
            'model_deepseek_via_dashscope': 'deepseek-v4-flash',
            'model_qwen_default': 'qwen-plus-latest',
            'model_kimi_via_dashscope': 'kimi/kimi-k2.6',
        }.items()
    ]
    # 队列: SELECT(取原值) -> old_rows; DELETE -> None; 12 次 INSERT -> None * 12
    queue = [old_rows, None] + [None] * 12
    conn, _ = _make_conn(rows_queue=queue)
    with patch("db.connection.get_connection", return_value=conn):
        r = admin_client.post(
            "/api/admin/research-monitor/config/reset",
            json={"confirm_code": "RESET_RESEARCH_MONITOR_CONFIG"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reset_count"] == 12
    assert len(body["keys"]) == 12
    assert "budget_per_round_yuan" in body["keys"]
    # P13-v13 锁: 4 个 model_* 必须在 reset 范围内
    for model_key in ['model_doubao_app', 'model_deepseek_via_dashscope',
                      'model_qwen_default', 'model_kimi_via_dashscope']:
        assert model_key in body["keys"], f"reset 必须含 {model_key}"
    # 旧 deprecated key 必须不在
    assert 'clean_attempts_max' not in body["keys"], "deprecated key 不能回退"
    assert 'undo_window_minutes' not in body["keys"], "deprecated key 不能回退"


def test_reset_specific_key(admin_client):
    old_rows = [{"key": "budget_per_round_yuan", "value_json": 999}]
    # SELECT -> old_rows; DELETE -> None; INSERT -> None
    queue = [old_rows, None, None]
    conn, _ = _make_conn(rows_queue=queue)
    with patch("db.connection.get_connection", return_value=conn):
        r = admin_client.post(
            "/api/admin/research-monitor/config/reset",
            json={
                "confirm_code": "RESET_RESEARCH_MONITOR_CONFIG",
                "keys": ["budget_per_round_yuan"],
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reset_count"] == 1
    assert body["keys"] == ["budget_per_round_yuan"]


def test_reset_unknown_key_404(admin_client):
    r = admin_client.post(
        "/api/admin/research-monitor/config/reset",
        json={
            "confirm_code": "RESET_RESEARCH_MONITOR_CONFIG",
            "keys": ["budget_per_round_yuan", "no_such_key"],
        },
    )
    assert r.status_code == 404, r.text


def test_reset_empty_keys_400(admin_client):
    r = admin_client.post(
        "/api/admin/research-monitor/config/reset",
        json={
            "confirm_code": "RESET_RESEARCH_MONITOR_CONFIG",
            "keys": [],
        },
    )
    assert r.status_code == 400, r.text
