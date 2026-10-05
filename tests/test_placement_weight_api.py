"""
test_placement_weight_api — 5 个管理员配置 API 行为与鉴权测试

覆盖：
  - 非管理员调任何配置接口 → 403
  - GET /config 返回默认 {decay_factor: 0.3, min_queries_per_month: 10}
  - PUT /config 写入后再读取能拿到新值
  - GET /weights?industry=xxx 包含每月题数 / 默认权重 / 是否覆盖
  - PUT /weights 写入覆盖 → DELETE 清除 → 恢复默认
"""
import pytest
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware


# ----------------------------------------------------------------
# 测试用 FastAPI app
# ----------------------------------------------------------------

def _build_app(is_admin: bool):
    """装一个最小 app，挂 placement_router，自动把测试用户注入 request.state.user"""
    app = FastAPI()

    class _InjectUserMW(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.user = {
                "user_id": 9_100_999,
                "username": "test_admin" if is_admin else "test_user",
                "is_admin": is_admin,
            }
            return await call_next(request)

    app.add_middleware(_InjectUserMW)
    from api.placement_api import router as placement_router
    app.include_router(placement_router)
    return app


@pytest.fixture
def admin_client():
    return TestClient(_build_app(is_admin=True))


@pytest.fixture
def normal_client():
    return TestClient(_build_app(is_admin=False))


# ----------------------------------------------------------------
# 1) 鉴权
# ----------------------------------------------------------------

def test_non_admin_blocked_on_all_config_endpoints(normal_client):
    assert normal_client.get("/api/placement/research/config").status_code == 403
    assert normal_client.put("/api/placement/research/config", json={}).status_code == 403
    assert normal_client.get("/api/placement/research/weights?industry=装修建材").status_code == 403
    assert normal_client.put("/api/placement/research/weights", json={
        "industry": "装修建材", "year_month": "2026-02", "weight": 0.5
    }).status_code == 403
    assert normal_client.delete(
        "/api/placement/research/weights?industry=装修建材&year_month=2026-02"
    ).status_code == 403


# ----------------------------------------------------------------
# 2) GET /config
# ----------------------------------------------------------------

def test_admin_get_default_config(admin_client, db_with_clean_research):
    r = admin_client.get("/api/placement/research/config")
    assert r.status_code == 200
    data = r.json()
    assert data["decay_factor"] == pytest.approx(0.3)
    assert data["min_queries_per_month"] == 10


# ----------------------------------------------------------------
# 3) PUT /config 写后读
# ----------------------------------------------------------------

def test_admin_update_config(admin_client, db_with_clean_research):
    r = admin_client.put(
        "/api/placement/research/config",
        json={"decay_factor": 0.4, "min_queries_per_month": 15},
    )
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True

    g = admin_client.get("/api/placement/research/config").json()
    assert g["decay_factor"] == pytest.approx(0.4)
    assert g["min_queries_per_month"] == 15


def test_admin_update_config_validates_range(admin_client, db_with_clean_research):
    # decay_factor 必须 0 < x <= 1
    r = admin_client.put("/api/placement/research/config", json={"decay_factor": 1.5})
    assert r.status_code == 400
    r = admin_client.put("/api/placement/research/config", json={"decay_factor": 0})
    assert r.status_code == 400
    # min_queries_per_month >= 1
    r = admin_client.put("/api/placement/research/config", json={"min_queries_per_month": 0})
    assert r.status_code == 400


# ----------------------------------------------------------------
# 4) GET /weights — 行业月份明细
# ----------------------------------------------------------------

def test_get_weights_includes_default_and_count(admin_client, db_with_clean_research):
    # 喂数据：装修建材 当月 25 题、上上月 25 题
    now = datetime.now()
    cur_ym = now.strftime("%Y-%m")
    y, m = now.year, now.month - 2
    if m <= 0: y, m = y - 1, m + 12
    old_ym = f"{y:04d}-{m:02d}"

    c = db_with_clean_research.cursor()
    for ym in (cur_ym, old_ym):
        for i in range(25):
            c.execute(
                "INSERT INTO geo_research_raw (industry, engine, query, cited_platform, created_at) "
                "VALUES (%s, %s, %s, %s, %s::timestamp)",
                ("装修建材", "豆包", f"q{i}", "知乎", f"{ym}-15 12:00:00"),
            )
    db_with_clean_research.commit()

    r = admin_client.get("/api/placement/research/weights?industry=装修建材")
    assert r.status_code == 200, r.text
    rows = r.json()
    by_ym = {row["year_month"]: row for row in rows}
    assert cur_ym in by_ym and old_ym in by_ym
    assert by_ym[cur_ym]["query_count"] == 25
    assert by_ym[cur_ym]["default_weight"] == pytest.approx(1.0)
    assert by_ym[old_ym]["default_weight"] == pytest.approx(0.09, abs=0.001)
    assert all(row["is_overridden"] is False for row in rows)
    assert all(row["below_min_threshold"] is False for row in rows)


# ----------------------------------------------------------------
# 5) PUT/DELETE /weights — 覆盖 + 恢复
# ----------------------------------------------------------------

def test_put_then_delete_weight_override(admin_client, db_with_clean_research):
    # 先放点数据，让该 (行业,月份) 在 GET 里出现
    c = db_with_clean_research.cursor()
    for i in range(25):
        c.execute(
            "INSERT INTO geo_research_raw (industry, engine, query, cited_platform, created_at) "
            "VALUES (%s, %s, %s, %s, %s::timestamp)",
            ("装修建材", "豆包", f"q{i}", "房天下", "2026-02-15 12:00:00"),
        )
    db_with_clean_research.commit()

    # PUT 覆盖
    r = admin_client.put("/api/placement/research/weights", json={
        "industry": "装修建材", "year_month": "2026-02", "weight": 0.0, "note": "作废",
    })
    assert r.status_code == 200, r.text

    rows = admin_client.get("/api/placement/research/weights?industry=装修建材").json()
    feb = next(row for row in rows if row["year_month"] == "2026-02")
    assert feb["is_overridden"] is True
    assert feb["override_weight"] == pytest.approx(0.0)

    # DELETE 恢复
    r = admin_client.delete(
        "/api/placement/research/weights?industry=装修建材&year_month=2026-02"
    )
    assert r.status_code == 200, r.text

    rows = admin_client.get("/api/placement/research/weights?industry=装修建材").json()
    feb = next(row for row in rows if row["year_month"] == "2026-02")
    assert feb["is_overridden"] is False
    assert feb["override_weight"] is None


def test_put_weight_validates_range(admin_client, db_with_clean_research):
    r = admin_client.put("/api/placement/research/weights", json={
        "industry": "装修建材", "year_month": "2026-02", "weight": -0.1,
    })
    assert r.status_code == 400
    r = admin_client.put("/api/placement/research/weights", json={
        "industry": "装修建材", "year_month": "2026-02", "weight": 11,
    })
    assert r.status_code == 400
