"""
里程碑 A.7 Group 1 · 行业管理 API 单测

策略:
  - 用 FastAPI TestClient + 一个最小 app
  - 通过 middleware 注入 request.state.user 模拟身份
  - patch api.research_monitor_industry_api.get_connection 为 FakeConn
    完全 mock SQL 行为, 不连真 PG

覆盖:
  - GET 默认仅 active / include_inactive=true 全返
  - POST 成功 / 重名 409 / 缺字段 422 / slug 含大写 422
  - PUT 部分更新成功 / 改 name 重名 409 / 不存在 404
  - DELETE 成功 / 已 inactive 幂等 / 不存在 404
  - reorder 成功 / 含不存在 id → 整批回滚 + 404
  - 未登录 → 401 / 非 admin → 403
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple
from unittest.mock import patch

import pytest

# 项目根
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 在 import 业务模块前做最小环境保险
os.environ.setdefault("DATABASE_URL", "postgresql://stub:stub@localhost:5432/stub")

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402


# ============================================================
# Fake DB —— 完全模拟 cursor.execute / fetchone / fetchall
# ============================================================


class FakeCursor:
    """
    简易 cursor, 由 FakeConn 注入预设响应队列。
    每次 execute 会:
      1) 把 (sql, params) append 到 conn.executed
      2) 从 conn.responses 头部取下一个响应作为本轮结果
         响应可以是 dict / list / "RAISE:<msg>" / None
    fetchone/fetchall 返回最近一次 execute 的结果(含 RETURNING)。
    """

    def __init__(self, conn: "FakeConn"):
        self.conn = conn
        self._last: Any = None
        self.rowcount = 0
        self.closed = False

    def execute(self, sql: str, params: Tuple = ()):
        if self.closed:
            raise RuntimeError("cursor 已关闭")
        self.conn.executed.append((sql, params))
        if not self.conn.responses:
            self._last = None
            self.rowcount = 0
            return
        resp = self.conn.responses.pop(0)
        if isinstance(resp, str) and resp.startswith("RAISE:"):
            raise RuntimeError(resp[6:])
        if isinstance(resp, dict):
            self._last = resp
            self.rowcount = 1
        elif isinstance(resp, list):
            self._last = resp
            self.rowcount = len(resp)
        elif resp is None:
            self._last = None
            self.rowcount = 0
        else:
            # int 表示 rowcount(纯 UPDATE 不返回行)
            self._last = None
            self.rowcount = int(resp)

    def fetchone(self):
        if isinstance(self._last, dict):
            return self._last
        if isinstance(self._last, list):
            return self._last[0] if self._last else None
        return None

    def fetchall(self):
        if isinstance(self._last, list):
            return self._last
        if isinstance(self._last, dict):
            return [self._last]
        return []

    def close(self):
        self.closed = True


class FakeConn:
    """假连接, 给定一组按调用顺序消费的响应。"""

    def __init__(self, responses: Optional[List[Any]] = None):
        self.responses: List[Any] = list(responses or [])
        self.executed: List[Tuple[str, Tuple]] = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


# ============================================================
# App fixture
# ============================================================


def _build_app(user: Optional[dict]):
    """构造一个带 router 的最小 app, 通过 middleware 注入 user。"""
    from api.research_monitor_industry_api import router

    app = FastAPI()

    class _InjectUserMW(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.user = user
            return await call_next(request)

    app.add_middleware(_InjectUserMW)
    app.include_router(router)
    return app


@pytest.fixture
def admin_client():
    app = _build_app({"id": 1, "username": "admin_test", "is_admin": True})
    return TestClient(app)


@pytest.fixture
def normal_client():
    app = _build_app({"id": 2, "username": "user_test", "is_admin": False})
    return TestClient(app)


@pytest.fixture
def anon_client():
    app = _build_app(None)
    return TestClient(app)


def _patch_conn(fake: FakeConn):
    """patch get_connection 返回指定 FakeConn。"""
    return patch(
        "api.research_monitor_industry_api.get_connection",
        return_value=fake,
    )


def _row(id_: int, name: str, slug: str, sort_order: int = 0, active: bool = True) -> dict:
    return {
        "id": id_,
        "name": name,
        "slug": slug,
        "sort_order": sort_order,
        "active": active,
        "created_at": datetime(2026, 5, 7, 10, 0, 0),
        "updated_at": datetime(2026, 5, 7, 10, 0, 0),
    }


# ============================================================
# 1. GET /industries
# ============================================================


def test_list_industries_active_only(admin_client):
    """默认 include_inactive=False, 只返回 active 行。"""
    fake = FakeConn(responses=[
        [_row(1, "母婴", "muying"), _row(2, "教育", "edu")]
    ])
    with _patch_conn(fake):
        resp = admin_client.get("/api/admin/research-monitor/industries")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["industries"]) == 2
    # 校验 SQL 带了 active=TRUE
    sql, _ = fake.executed[0]
    assert "active = TRUE" in sql


def test_list_industries_include_inactive(admin_client):
    """include_inactive=true 时不带 active 过滤。"""
    fake = FakeConn(responses=[
        [_row(1, "母婴", "muying", active=True),
         _row(3, "金融", "finance", active=False)]
    ])
    with _patch_conn(fake):
        resp = admin_client.get(
            "/api/admin/research-monitor/industries?include_inactive=true"
        )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["industries"]) == 2
    sql, _ = fake.executed[0]
    # P14-v6: SQL 现在含 prompts 子查询 `p.active = TRUE` · 老断言"active = TRUE 不在 sql"
    # 不再适用 · 改成精确断言 industries 表自己的 active 过滤被关掉
    # (顶层 WHERE 不能含 i.active = TRUE · 否则 inactive 行业取不到)
    assert "WHERE i.active = TRUE" not in sql, \
        "include_inactive=true 时顶层 WHERE 不能再过 i.active = TRUE"
    assert "ORDER BY i.sort_order" in sql or "ORDER BY sort_order" in sql


# ============================================================
# 2. POST /industries
# ============================================================


def test_create_industry_ok(admin_client):
    """新建成功: 第 1 次查重无结果, 第 2 次 INSERT RETURNING 行。"""
    fake = FakeConn(responses=[
        None,  # 查重: 无重名
        _row(10, "宠物", "pet"),  # INSERT RETURNING
    ])
    with _patch_conn(fake):
        resp = admin_client.post(
            "/api/admin/research-monitor/industries",
            json={"name": "宠物", "slug": "pet", "sort_order": 5, "active": True},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["industry"]["id"] == 10
    assert body["industry"]["name"] == "宠物"
    assert fake.committed is True


def test_create_industry_duplicate_name(admin_client):
    """重名 → 409。"""
    fake = FakeConn(responses=[
        {"id": 9, "name": "母婴", "slug": "muying"},  # 查重命中
    ])
    with _patch_conn(fake):
        resp = admin_client.post(
            "/api/admin/research-monitor/industries",
            json={"name": "母婴", "slug": "newslug"},
        )
    assert resp.status_code == 409, resp.text
    assert "已存在" in resp.json()["detail"]
    assert fake.committed is False


def test_create_industry_missing_name(admin_client):
    """P13-v7 后: slug/sort_order 后端自动生成 · 唯一必填是 name · 缺 name → 422"""
    resp = admin_client.post(
        "/api/admin/research-monitor/industries",
        json={},
    )
    assert resp.status_code == 422, resp.text


def test_create_industry_ignores_extra_fields(admin_client):
    """P13-v7 + P14-v7 后: CreateIndustryRequest 只接 name + active
    多传字段 (slug/sort_order/等) 被 Pydantic 静默忽略 · 不再 422
    """
    fake = FakeConn(responses=[
        None,                                              # 查重无结果
        _row(11, "宠物", "ind_abcdef123456", active=True), # INSERT RETURNING (slug 后端生成)
    ])
    with _patch_conn(fake):
        resp = admin_client.post(
            "/api/admin/research-monitor/industries",
            json={"name": "宠物", "slug": "Pet", "sort_order": 99},
        )
    # 关键: 多余字段被 Pydantic ignore · 不再 422 · 应该 200
    assert resp.status_code == 200, \
        f"P13-v7 后多余字段应被 ignore · 不该 422 · 实际 {resp.status_code}: {resp.text[:200]}"
    assert resp.json()["industry"]["name"] == "宠物"


# ============================================================
# 3. PUT /industries/{id}
# ============================================================


def test_update_industry_partial_ok(admin_client):
    """P13-v7 后: 可更新字段仅 name + active · 改 active 成功"""
    fake = FakeConn(responses=[
        {"id": 5, "name": "母婴", "slug": "muying"},  # 校验存在
        _row(5, "母婴", "muying", active=False),       # UPDATE RETURNING
    ])
    with _patch_conn(fake):
        resp = admin_client.put(
            "/api/admin/research-monitor/industries/5",
            json={"active": False},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["industry"]["active"] is False
    assert fake.committed is True


def test_update_industry_sort_order_no_longer_allowed(admin_client):
    """P13-v7 后: sort_order 不在 _ALLOWED_INDUSTRY_UPDATE_FIELDS · 单独传返 400"""
    # 不需要 patch_conn · endpoint 在校验阶段就 400
    resp = admin_client.put(
        "/api/admin/research-monitor/industries/5",
        json={"sort_order": 99},
    )
    assert resp.status_code == 400, resp.text


def test_update_industry_name_conflict(admin_client):
    """改 name 撞别人 → 409。"""
    fake = FakeConn(responses=[
        {"id": 5, "name": "母婴", "slug": "muying"},  # 存在
        {"id": 7, "name": "教育", "slug": "edu"},  # 重名命中(其它 id)
    ])
    with _patch_conn(fake):
        resp = admin_client.put(
            "/api/admin/research-monitor/industries/5",
            json={"name": "教育"},
        )
    assert resp.status_code == 409
    assert "教育" in resp.json()["detail"]
    assert fake.committed is False


def test_update_industry_not_found(admin_client):
    """id 不存在 → 404。"""
    fake = FakeConn(responses=[None])  # 第 1 次查不到
    with _patch_conn(fake):
        resp = admin_client.put(
            "/api/admin/research-monitor/industries/9999",
            json={"name": "新名字"},
        )
    assert resp.status_code == 404


# ============================================================
# 4. DELETE /industries/{id}
# ============================================================


def test_delete_industry_ok(admin_client):
    """正常软删。"""
    fake = FakeConn(responses=[
        {"id": 5, "active": True},  # 存在且 active
        1,  # UPDATE rowcount=1
    ])
    with _patch_conn(fake):
        resp = admin_client.delete("/api/admin/research-monitor/industries/5")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"deleted": True, "industry_id": 5}
    assert fake.committed is True


def test_delete_industry_idempotent(admin_client):
    """已 inactive 再删 → 200 幂等, 不发 UPDATE。"""
    fake = FakeConn(responses=[
        {"id": 5, "active": False},  # 已 inactive
    ])
    with _patch_conn(fake):
        resp = admin_client.delete("/api/admin/research-monitor/industries/5")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True, "industry_id": 5}
    # 没有 UPDATE 调用
    assert len(fake.executed) == 1
    assert fake.committed is False


def test_delete_industry_not_found(admin_client):
    """id 不存在 → 404。"""
    fake = FakeConn(responses=[None])
    with _patch_conn(fake):
        resp = admin_client.delete("/api/admin/research-monitor/industries/9999")
    assert resp.status_code == 404


# ============================================================
# 5. POST /industries/reorder
# ============================================================


def test_reorder_ok(admin_client):
    """所有 id 都存在, 单事务 UPDATE 全部, 返回 updated 总数。"""
    items = [{"id": 1, "sort_order": 10}, {"id": 2, "sort_order": 20}]
    fake = FakeConn(responses=[
        [{"id": 1}, {"id": 2}],  # 存在性查询
        1,  # UPDATE 1 行
        1,  # UPDATE 1 行
    ])
    with _patch_conn(fake):
        resp = admin_client.post(
            "/api/admin/research-monitor/industries/reorder",
            json={"items": items},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"updated": 2}
    assert fake.committed is True


def test_reorder_missing_id_rolls_back(admin_client):
    """有不存在 id → 整批 404, 不 commit。"""
    items = [{"id": 1, "sort_order": 10}, {"id": 9999, "sort_order": 20}]
    fake = FakeConn(responses=[
        [{"id": 1}],  # 只查到 id=1, 9999 缺失
    ])
    with _patch_conn(fake):
        resp = admin_client.post(
            "/api/admin/research-monitor/industries/reorder",
            json={"items": items},
        )
    assert resp.status_code == 404, resp.text
    assert "9999" in resp.json()["detail"]
    assert fake.committed is False
    assert fake.rolled_back is True


# ============================================================
# 6. 鉴权
# ============================================================


def test_unauth_returns_401(anon_client):
    """未登录 → 401。"""
    resp = anon_client.get("/api/admin/research-monitor/industries")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "未登录"


def test_non_admin_returns_403(normal_client):
    """非 admin → 403。"""
    resp = normal_client.get("/api/admin/research-monitor/industries")
    assert resp.status_code == 403
    assert "管理员" in resp.json()["detail"]


def test_non_admin_cannot_delete(normal_client):
    """非 admin 删除也要被拒 403。"""
    resp = normal_client.delete("/api/admin/research-monitor/industries/1")
    assert resp.status_code == 403


def test_non_admin_cannot_reorder(normal_client):
    """非 admin reorder 也要被拒 403。"""
    resp = normal_client.post(
        "/api/admin/research-monitor/industries/reorder",
        json={"items": [{"id": 1, "sort_order": 5}]},
    )
    assert resp.status_code == 403
