"""
里程碑 A.7 Group 2 · prompts 管理 API 单测

策略:
  - FastAPI TestClient + 一个最小 app
  - 通过 middleware 注入 request.state.user 模拟身份
  - patch api.research_monitor_prompts_api.get_connection 为 FakeConn
    完全 mock SQL 行为, 不连真 PG

覆盖:
  - GET 含/不含 active 过滤 / industry_id 缺失 422 / industry 不存在 404
  - POST 重复 409 / industry 不存在 404 / 成功
  - PUT 部分更新 / 改 prompt_text 重复 409 / 不存在 404
  - DELETE 软删 / 已 inactive 幂等 / 不存在 404
  - reorder 成功 / 跨行业污染 400 / industry 不存在 404
  - bulk_create 列表内重复 409 / 与现有重复 409 / sort_order 自动递增 / 全成功 / 单次 > 500 422
  - toggle FALSE→TRUE / TRUE→FALSE / 不存在 404
  - 未登录 401 / 非 admin 403

P12-fix-v2 (2026-05-26): 删 25 行业上限相关用例 · 加正反试 "超 25 仍可创建/启用/批量"
"""

from __future__ import annotations

import importlib.util
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

# 在 import 业务模块前做最小环境保险 (业务代码 import 时连接池 lazy 初始化)
os.environ.setdefault("DATABASE_URL", "postgresql://stub:stub@localhost:5432/stub")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402


# ============================================================
# 直接 importlib 加载目标 module, 绕开 api/__init__.py 的 employee_api
# 链(它会触发 init_db 真连 PG)。
# ============================================================


def _load_prompts_api_module():
    """通过文件路径加载 api/research_monitor_prompts_api.py, 不走 api package。"""
    module_name = "api.research_monitor_prompts_api"
    if module_name in sys.modules:
        return sys.modules[module_name]
    file_path = ROOT / "api" / "research_monitor_prompts_api.py"
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_PROMPTS_API = _load_prompts_api_module()


# ============================================================
# Fake DB —— 完全模拟 cursor.execute / fetchone / fetchall
# ============================================================


class FakeCursor:
    """
    简易 cursor:
      - execute 把 (sql, params) append 到 conn.executed
      - 从 conn.responses 头部取下一个响应作为本轮结果
        响应可以是 dict / list / int(rowcount) / None / "RAISE:<msg>"
      - fetchone / fetchall 返回最近一次 execute 结果(含 RETURNING)
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
    router = _PROMPTS_API.router

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
    """patch get_connection 返指定 FakeConn (用 patch.object 避免触发 api/__init__.py)"""
    return patch.object(_PROMPTS_API, "get_connection", return_value=fake)


def _prompt_row(id_: int, industry_id: int, text: str,
                sort_order: int = 0, active: bool = True) -> dict:
    return {
        "id": id_,
        "industry_id": industry_id,
        "prompt_text": text,
        "sort_order": sort_order,
        "active": active,
        "created_at": datetime(2026, 5, 7, 10, 0, 0),
        "updated_at": datetime(2026, 5, 7, 10, 0, 0),
    }


def _industry_exists_row():
    """industry 存在的探测响应"""
    return {"?column?": 1}


# ============================================================
# 1. GET /prompts
# ============================================================


def test_list_prompts_active_only(admin_client):
    """默认 include_inactive=False, 只返回 active 行。"""
    fake = FakeConn(responses=[
        _industry_exists_row(),  # industry 探测
        [_prompt_row(1, 5, "问题 1", 0, True),
         _prompt_row(2, 5, "问题 2", 1, True)],
    ])
    with _patch_conn(fake):
        resp = admin_client.get(
            "/api/admin/research-monitor/prompts?industry_id=5"
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["industry_id"] == 5
    assert body["count"] == 2
    assert len(body["prompts"]) == 2
    # 校验 SQL 带了 active=TRUE
    sql, _ = fake.executed[1]
    assert "active = TRUE" in sql


def test_list_prompts_include_inactive(admin_client):
    """include_inactive=true 时不带 active 过滤。"""
    fake = FakeConn(responses=[
        _industry_exists_row(),
        [_prompt_row(1, 5, "问题 1", active=True),
         _prompt_row(2, 5, "禁用", active=False)],
    ])
    with _patch_conn(fake):
        resp = admin_client.get(
            "/api/admin/research-monitor/prompts?industry_id=5&include_inactive=true"
        )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["prompts"]) == 2
    sql, _ = fake.executed[1]
    assert "active = TRUE" not in sql
    assert "ORDER BY sort_order" in sql


def test_list_prompts_missing_industry_id(admin_client):
    """不传 industry_id → 422。"""
    resp = admin_client.get("/api/admin/research-monitor/prompts")
    assert resp.status_code == 422


def test_list_prompts_industry_not_found(admin_client):
    """industry 不存在 → 404。"""
    fake = FakeConn(responses=[None])  # 探测失败
    with _patch_conn(fake):
        resp = admin_client.get(
            "/api/admin/research-monitor/prompts?industry_id=999"
        )
    assert resp.status_code == 404


# ============================================================
# 2. POST /prompts
# ============================================================


def test_create_prompt_ok(admin_client):
    """新建成功: industry 探测 + 重复查 + INSERT RETURNING。
    P12-fix-v2: 不再 mock active count (后端删了 _count_active_prompts 这一步)。
    """
    fake = FakeConn(responses=[
        _industry_exists_row(),         # industry 探测
        None,                           # 重复查: 无
        _prompt_row(20, 5, "新问题", 7, True),  # INSERT RETURNING
    ])
    with _patch_conn(fake):
        resp = admin_client.post(
            "/api/admin/research-monitor/prompts",
            json={
                "industry_id": 5,
                "prompt_text": "新问题",
                "sort_order": 7,
                "active": True,
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["prompt"]["id"] == 20
    assert body["prompt"]["prompt_text"] == "新问题"
    assert fake.committed is True


def test_create_prompt_industry_not_found(admin_client):
    """industry 不存在 → 404。"""
    fake = FakeConn(responses=[None])
    with _patch_conn(fake):
        resp = admin_client.post(
            "/api/admin/research-monitor/prompts",
            json={"industry_id": 999, "prompt_text": "x"},
        )
    assert resp.status_code == 404
    assert fake.committed is False


def test_create_prompt_no_limit_above_25(admin_client):
    """P12-fix-v2 正向锁: 行业已有 100 active prompts 仍可继续创建 · 不再有 25 上限"""
    fake = FakeConn(responses=[
        _industry_exists_row(),
        None,                                    # 重复查: 无
        _prompt_row(101, 5, "第 101 条", 0, True),  # INSERT RETURNING
    ])
    with _patch_conn(fake):
        resp = admin_client.post(
            "/api/admin/research-monitor/prompts",
            json={"industry_id": 5, "prompt_text": "第 101 条"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["prompt"]["id"] == 101
    assert fake.committed is True


def test_create_prompt_source_code_no_25_business_limit():
    """P12-fix-v2 锁: 源码不再有 MAX_PROMPTS_PER_INDUSTRY 业务上限常量"""
    import api.research_monitor_prompts_api as mod
    # 行业 prompts 数量不再限 · 只保留单条文本 + 单次批量 + reorder 单次
    assert not hasattr(mod, 'MAX_PROMPTS_PER_INDUSTRY'), (
        "P12-fix-v2: MAX_PROMPTS_PER_INDUSTRY 不应再存在 · "
        "老板拍板行业 prompts 数量不设业务上限"
    )
    # 但批量提交单次防御性上限要在
    assert hasattr(mod, 'MAX_BULK_CREATE_PROMPTS'), "P12-fix-v2: 应有单次批量上限常量"
    assert mod.MAX_BULK_CREATE_PROMPTS >= 100, "单次批量上限应较宽松 (≥100)"


def test_create_prompt_duplicate_text(admin_client):
    """同 industry 内 prompt_text 已存在 → 409。"""
    fake = FakeConn(responses=[
        _industry_exists_row(),
        {"?column?": 1},  # 重复查命中 (P12-fix-v2 删了 active count 这一 query)
    ])
    with _patch_conn(fake):
        resp = admin_client.post(
            "/api/admin/research-monitor/prompts",
            json={"industry_id": 5, "prompt_text": "已有问题"},
        )
    assert resp.status_code == 409
    assert "重复" in resp.json()["detail"] or "存在" in resp.json()["detail"]
    assert fake.committed is False


def test_create_prompt_text_too_long(admin_client):
    """prompt_text 超过 500 字符 → 422 (Pydantic max_length)。"""
    resp = admin_client.post(
        "/api/admin/research-monitor/prompts",
        json={"industry_id": 5, "prompt_text": "a" * 501},
    )
    assert resp.status_code == 422


# ============================================================
# 3. PUT /prompts/{id} - 部分更新
# ============================================================


def test_update_prompt_partial_ok(admin_client):
    """只改 sort_order 成功。"""
    fake = FakeConn(responses=[
        # 取现有: industry_id=5
        {"id": 10, "industry_id": 5, "prompt_text": "原问题",
         "sort_order": 0, "active": True},
        # UPDATE RETURNING
        _prompt_row(10, 5, "原问题", 9, True),
    ])
    with _patch_conn(fake):
        resp = admin_client.put(
            "/api/admin/research-monitor/prompts/10",
            json={"sort_order": 9},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["prompt"]["sort_order"] == 9
    assert fake.committed is True


def test_update_prompt_text_conflict(admin_client):
    """改 prompt_text 撞同 industry 内重复 → 409。"""
    fake = FakeConn(responses=[
        # 取现有
        {"id": 10, "industry_id": 5, "prompt_text": "原问题",
         "sort_order": 0, "active": True},
        # 重复查命中
        {"?column?": 1},
    ])
    with _patch_conn(fake):
        resp = admin_client.put(
            "/api/admin/research-monitor/prompts/10",
            json={"prompt_text": "撞车的问题"},
        )
    assert resp.status_code == 409
    assert fake.committed is False


def test_update_prompt_activate_no_limit_above_25(admin_client):
    """P12-fix-v2 正向锁: PUT active=TRUE 不再因 active >= 25 拒绝
    行业 prompts 数量不设业务上限 · inactive → active 总能成
    """
    fake = FakeConn(responses=[
        {"id": 10, "industry_id": 5, "prompt_text": "原问题",
         "sort_order": 0, "active": False},
        # P12-fix-v2: 不再查 active count · 直接 UPDATE RETURNING
        _prompt_row(10, 5, "原问题", 0, True),
    ])
    with _patch_conn(fake):
        resp = admin_client.put(
            "/api/admin/research-monitor/prompts/10",
            json={"active": True},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["prompt"]["active"] is True
    assert fake.committed is True


def test_update_prompt_not_found(admin_client):
    """目标不存在 → 404。"""
    fake = FakeConn(responses=[None])
    with _patch_conn(fake):
        resp = admin_client.put(
            "/api/admin/research-monitor/prompts/999",
            json={"sort_order": 1},
        )
    assert resp.status_code == 404


def test_update_prompt_no_fields_returns_current(admin_client):
    """未传任何字段 → 直接返当前。"""
    fake = FakeConn(responses=[
        # 取现有
        {"id": 10, "industry_id": 5, "prompt_text": "原",
         "sort_order": 0, "active": True},
        # 再次 SELECT 返完整行
        _prompt_row(10, 5, "原", 0, True),
    ])
    with _patch_conn(fake):
        resp = admin_client.put(
            "/api/admin/research-monitor/prompts/10",
            json={},
        )
    assert resp.status_code == 200
    assert resp.json()["prompt"]["id"] == 10


# ============================================================
# 4. DELETE /prompts/{id} - 软删, 幂等
# ============================================================


def test_delete_prompt_ok(admin_client):
    """active=TRUE 软删成功。"""
    fake = FakeConn(responses=[
        {"id": 10, "active": True},  # 现有
        1,                           # UPDATE rowcount=1
    ])
    with _patch_conn(fake):
        resp = admin_client.delete("/api/admin/research-monitor/prompts/10")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active"] is False
    assert body["already_inactive"] is False
    assert fake.committed is True


def test_delete_prompt_idempotent(admin_client):
    """已 inactive 再 DELETE → 200 ok 幂等, 不再 UPDATE。"""
    fake = FakeConn(responses=[
        {"id": 10, "active": False},  # 已 inactive
    ])
    with _patch_conn(fake):
        resp = admin_client.delete("/api/admin/research-monitor/prompts/10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is False
    assert body["already_inactive"] is True
    # 没真做 UPDATE
    assert len(fake.executed) == 1


def test_delete_prompt_not_found(admin_client):
    """不存在 → 404。"""
    fake = FakeConn(responses=[None])
    with _patch_conn(fake):
        resp = admin_client.delete("/api/admin/research-monitor/prompts/999")
    assert resp.status_code == 404


# ============================================================
# 5. POST /prompts/reorder - 批量改 sort_order
# ============================================================


def test_reorder_ok(admin_client):
    """所有 id 属同 industry, 单事务全 UPDATE。"""
    items = [{"id": 10, "sort_order": 0},
             {"id": 11, "sort_order": 1},
             {"id": 12, "sort_order": 2}]
    fake = FakeConn(responses=[
        _industry_exists_row(),
        # SELECT id, industry_id WHERE id = ANY(...)
        [{"id": 10, "industry_id": 5},
         {"id": 11, "industry_id": 5},
         {"id": 12, "industry_id": 5}],
        # 3 个 UPDATE
        1, 1, 1,
    ])
    with _patch_conn(fake):
        resp = admin_client.post(
            "/api/admin/research-monitor/prompts/reorder",
            json={"industry_id": 5, "items": items},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 3
    assert fake.committed is True


def test_reorder_cross_industry_pollution(admin_client):
    """有 id 不属于该 industry → 整批 rollback + 400。"""
    items = [{"id": 10, "sort_order": 0},
             {"id": 11, "sort_order": 1}]
    fake = FakeConn(responses=[
        _industry_exists_row(),
        [{"id": 10, "industry_id": 5},
         {"id": 11, "industry_id": 7}],  # id=11 跨行业
    ])
    with _patch_conn(fake):
        resp = admin_client.post(
            "/api/admin/research-monitor/prompts/reorder",
            json={"industry_id": 5, "items": items},
        )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert "bad_items" in body["detail"]
    bad = body["detail"]["bad_items"]
    assert any(b["id"] == 11 and b["reason"] == "wrong_industry" for b in bad)
    assert fake.committed is False
    assert fake.rolled_back is True


def test_reorder_industry_not_found(admin_client):
    """industry 不存在 → 404。"""
    fake = FakeConn(responses=[None])
    with _patch_conn(fake):
        resp = admin_client.post(
            "/api/admin/research-monitor/prompts/reorder",
            json={"industry_id": 999, "items": [{"id": 1, "sort_order": 0}]},
        )
    assert resp.status_code == 404


# ============================================================
# 6. POST /prompts/bulk-create
# ============================================================


def test_bulk_create_ok(admin_client):
    """全部成功, sort_order 自动递增 (max=2 → 3, 4, 5)。
    P12-fix-v2: 不再 mock active count (后端删了)。
    """
    fake = FakeConn(responses=[
        _industry_exists_row(),       # industry 探测
        [],                           # 现有 prompt_text 列表 (empty 即可避免 dup)
        {"max_so": 2},                # 当前 max sort_order
        # 3 个 INSERT RETURNING
        _prompt_row(100, 5, "新 1", 3, True),
        _prompt_row(101, 5, "新 2", 4, True),
        _prompt_row(102, 5, "新 3", 5, True),
    ])
    with _patch_conn(fake):
        resp = admin_client.post(
            "/api/admin/research-monitor/prompts/bulk-create",
            json={"industry_id": 5, "prompts": ["新 1", "新 2", "新 3"]},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] == 3
    assert body["industry_id"] == 5
    sort_orders = [p["sort_order"] for p in body["prompts"]]
    assert sort_orders == [3, 4, 5]
    assert fake.committed is True


def test_bulk_create_no_limit_above_25(admin_client):
    """P12-fix-v2 正向锁: 现有 20 + 新 10 = 30 仍 OK · 不再 409
    行业 prompts 数量不设业务上限 · 单次提交 30 < MAX_BULK_CREATE_PROMPTS(500) OK
    """
    fake = FakeConn(responses=[
        _industry_exists_row(),       # industry 探测
        [],                           # 现有 prompt_text 列表
        {"max_so": 20},               # 当前 max sort_order (假设已有 20 条)
    ] + [
        _prompt_row(100 + i, 5, f"q{i}", 21 + i, True) for i in range(10)
    ])
    with _patch_conn(fake):
        resp = admin_client.post(
            "/api/admin/research-monitor/prompts/bulk-create",
            json={"industry_id": 5, "prompts": [f"q{i}" for i in range(10)]},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] == 10
    assert fake.committed is True


def test_bulk_create_single_request_exceeds_500(admin_client):
    """P12-fix-v2: 单次提交 501 条 > MAX_BULK_CREATE_PROMPTS(500) → 422 Pydantic 拦截
    这是单次请求保护 · 不是行业总数限制 · 分批 (500 + N) 即可
    """
    fake = FakeConn(responses=[])
    with _patch_conn(fake):
        resp = admin_client.post(
            "/api/admin/research-monitor/prompts/bulk-create",
            json={"industry_id": 5, "prompts": [f"q{i}" for i in range(501)]},
        )
    # Pydantic 422 not 409 · 这是 request shape 校验 (max_length=MAX_BULK_CREATE_PROMPTS)
    assert resp.status_code == 422


def test_bulk_create_internal_duplicate(admin_client):
    """列表内自身有重复 → 409 (不查 DB)。"""
    # 列表内重复在第一道关 (在 conn 之前), 不应触发 DB
    fake = FakeConn(responses=[])
    with _patch_conn(fake):
        resp = admin_client.post(
            "/api/admin/research-monitor/prompts/bulk-create",
            json={"industry_id": 5, "prompts": ["a", "b", "a"]},
        )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "duplicates" in detail
    assert "a" in detail["duplicates"]


def test_bulk_create_dup_with_existing(admin_client):
    """与 DB 已有 prompt_text 重复 → 409。
    P12-fix-v2: 不再 mock active count (后端删了)。
    """
    fake = FakeConn(responses=[
        _industry_exists_row(),
        # 现有 texts: 含 "已有问题"
        [{"prompt_text": "已有问题"}, {"prompt_text": "另一个"}],
    ])
    with _patch_conn(fake):
        resp = admin_client.post(
            "/api/admin/research-monitor/prompts/bulk-create",
            json={"industry_id": 5, "prompts": ["新词", "已有问题"]},
        )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "duplicates" in detail
    assert "已有问题" in detail["duplicates"]
    assert fake.committed is False


# ============================================================
# 7. POST /prompts/{id}/toggle
# ============================================================


def test_toggle_active_to_inactive(admin_client):
    """TRUE → FALSE。"""
    fake = FakeConn(responses=[
        {"id": 10, "industry_id": 5, "active": True},
        1,  # UPDATE
    ])
    with _patch_conn(fake):
        resp = admin_client.post("/api/admin/research-monitor/prompts/10/toggle")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"prompt_id": 10, "active": False}
    assert fake.committed is True


def test_toggle_inactive_to_active(admin_client):
    """FALSE → TRUE 成功。
    P12-fix-v2: 不再 mock active count (后端删了)。
    """
    fake = FakeConn(responses=[
        {"id": 10, "industry_id": 5, "active": False},
        1,            # UPDATE
    ])
    with _patch_conn(fake):
        resp = admin_client.post("/api/admin/research-monitor/prompts/10/toggle")
    assert resp.status_code == 200
    assert resp.json() == {"prompt_id": 10, "active": True}


def test_toggle_inactive_to_active_no_limit_above_25(admin_client):
    """P12-fix-v2 正向锁: 行业已有 100 active prompts · toggle FALSE → TRUE 仍可成
    不再因 active >= 25 拒绝
    """
    fake = FakeConn(responses=[
        {"id": 10, "industry_id": 5, "active": False},
        # P12-fix-v2: 不再查 active count · 直接 UPDATE
        1,
    ])
    with _patch_conn(fake):
        resp = admin_client.post("/api/admin/research-monitor/prompts/10/toggle")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"prompt_id": 10, "active": True}


def test_toggle_not_found(admin_client):
    """不存在 → 404。"""
    fake = FakeConn(responses=[None])
    with _patch_conn(fake):
        resp = admin_client.post("/api/admin/research-monitor/prompts/999/toggle")
    assert resp.status_code == 404


# ============================================================
# RBAC: 401 / 403
# ============================================================


def test_unauth_returns_401(anon_client):
    """未登录 → 401。"""
    # 不会到 DB
    resp = anon_client.get("/api/admin/research-monitor/prompts?industry_id=1")
    assert resp.status_code == 401


def test_non_admin_returns_403(normal_client):
    """非 admin → 403。"""
    resp = normal_client.get("/api/admin/research-monitor/prompts?industry_id=1")
    assert resp.status_code == 403


def test_non_admin_cannot_create(normal_client):
    """非 admin 无权 POST。"""
    resp = normal_client.post(
        "/api/admin/research-monitor/prompts",
        json={"industry_id": 1, "prompt_text": "x"},
    )
    assert resp.status_code == 403


def test_non_admin_cannot_bulk_create(normal_client):
    """非 admin 无权 bulk-create。"""
    resp = normal_client.post(
        "/api/admin/research-monitor/prompts/bulk-create",
        json={"industry_id": 1, "prompts": ["a"]},
    )
    assert resp.status_code == 403


def test_non_admin_cannot_toggle(normal_client):
    """非 admin 无权 toggle。"""
    resp = normal_client.post("/api/admin/research-monitor/prompts/1/toggle")
    assert resp.status_code == 403


# ============================================================
# A.7 Phase 1 修复审计 · 新增覆盖
# ============================================================


def test_prompts_get_db_error_returns_500_with_generic_detail(admin_client):
    """C-1 修复: GET /prompts SQL 异常 → 500 + detail 不含 SQL/异常信息。

    模拟 industry 探测后第二次 cursor.execute 抛 RuntimeError, 验证:
      - 状态码 500
      - detail 是 "查询 prompts 失败" generic, 不含 e 类名/SQL 关键字
    """
    fake = FakeConn(responses=[
        _industry_exists_row(),                # industry 探测通过
        "RAISE:psycopg2.errors.SyntaxError simulated detail",
    ])
    with _patch_conn(fake):
        resp = admin_client.get(
            "/api/admin/research-monitor/prompts?industry_id=5"
        )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    detail = body.get("detail", "")
    assert detail == "查询 prompts 失败"
    # 内部异常细节绝不能进 detail
    assert "psycopg2" not in detail
    assert "SyntaxError" not in detail
    assert "simulated" not in detail
    assert "RuntimeError" not in detail


def test_create_prompt_db_error_returns_500_with_generic_detail(admin_client):
    """C-3 修复: POST /prompts INSERT 异常 → detail generic 不含 e。
    P12-fix-v2: 不再 mock active count (后端删了 _count_active_prompts)。
    """
    fake = FakeConn(responses=[
        _industry_exists_row(),                # industry 探测
        None,                                  # 重复查: 无
        "RAISE:UniqueViolation column foo_secret_internal",  # INSERT 异常
    ])
    with _patch_conn(fake):
        resp = admin_client.post(
            "/api/admin/research-monitor/prompts",
            json={"industry_id": 5, "prompt_text": "新问题"},
        )
    assert resp.status_code == 500, resp.text
    detail = resp.json().get("detail", "")
    assert detail == "创建 prompt 失败"
    # 严禁泄漏内部字段名/异常名
    assert "foo_secret_internal" not in detail
    assert "UniqueViolation" not in detail
    assert "RuntimeError" not in detail
    # 失败必须 rollback
    assert fake.rolled_back is True
    assert fake.committed is False
