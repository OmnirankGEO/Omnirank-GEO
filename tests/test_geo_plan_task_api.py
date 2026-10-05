"""
Phase 4 PLAN 01 · C 端 GEO 方案异步任务 API 单测

测试 12 个 + M3 completeness 3 个 = 15 个,对齐 PLAN 01 Task 2 <behavior>.
[开源 E3 · B3c G4 · 2026-09-28] C 端 GEO 方案任务 API(/api/geo-plan/start-task 等 5 条)整文件删除,
  TestGeoPlanTaskAPI 12 格随之退役;本文件只剩 M3 completeness 3 格(/api/brands/{id}/completeness,品牌 router 在役)。

策略:
  - 通过 DB 直接造测试 brand + user + profile + industry_knowledge
  - 用 FastAPI TestClient + 一个最小 FakeApp (不加全局 middleware) 测 router 本身
  - user 身份通过手动注入 request.state.user 模拟 auth 中间件

运行:
  python -m pytest <本文件> -x -v(只剩 TestBrandCompletenessAPI)
"""
from __future__ import annotations

import os
import sys
import json
from pathlib import Path

try:
    import pytest
except ImportError:
    class _PytestStub:
        @staticmethod
        def skip(msg): raise Exception(f'SKIP: {msg}')
        @staticmethod
        def fail(msg): raise AssertionError(msg)
        class raises:
            def __init__(self, exc): self.exc = exc
            def __enter__(self): return self
            def __exit__(self, t, v, tb):
                if t is None or not issubclass(t, self.exc):
                    raise AssertionError(f'expected {self.exc.__name__}, got {t}')
                return True
    pytest = _PytestStub()

# 项目根目录
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 测试用 ID (9_000_1xx 段给 API 单测)
_TEST_USER_A = 9_000_101
_TEST_USER_B = 9_000_102
_TEST_BRAND_A = None  # setup 时赋值
_TEST_BRAND_B = None


def _get_conn():
    from db.connection import get_connection
    return get_connection()


# ============================================================
# DB fixtures
# ============================================================


def _ensure_test_users():
    """确保测试 user 在 users 表 (RBAC 要查 owner_user_id,brand 表 FK 不强约束但最好有)."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        for uid in (_TEST_USER_A, _TEST_USER_B):
            cur.execute(
                """
                INSERT INTO users (id, username, password_hash, display_name, email, created_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (id) DO NOTHING
                """,
                (uid, f"test_u{uid}", "x", f"test_u{uid}", f"test{uid}@example.com"),
            )
        conn.commit()
    except Exception:
        # users 表字段可能变动,测试用户存在即可,忽略 INSERT 失败
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _ensure_test_brand(user_id: int, industry: str = "餐饮", name_prefix: str = "测试品牌") -> int:
    """造一个 brand 属于 user_id,返 brand_id."""
    import shortuuid
    brand_name = f"{name_prefix}_{user_id}_{shortuuid.uuid()[:6]}"
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO brands (name, owner_user_id, brand_type, industry, is_deleted, created_at, updated_at)
            VALUES (%s, %s, 'self', %s, FALSE, NOW(), NOW())
            RETURNING id
            """,
            (brand_name, user_id, industry),
        )
        bid = cur.fetchone()["id"]
        conn.commit()
        return int(bid)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _cleanup_test_brands():
    """清理测试数据."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM geo_plan_tasks WHERE user_id IN (%s, %s)",
            (_TEST_USER_A, _TEST_USER_B),
        )
        cur.execute(
            "DELETE FROM client_profiles WHERE brand_id IN "
            "(SELECT id FROM brands WHERE owner_user_id IN (%s, %s))",
            (_TEST_USER_A, _TEST_USER_B),
        )
        cur.execute(
            "DELETE FROM brands WHERE owner_user_id IN (%s, %s)",
            (_TEST_USER_A, _TEST_USER_B),
        )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _ensure_industry_knowledge_table():
    """确保 industry_knowledge 表存在 (本地测试 DB 可能没建过)."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS industry_knowledge (
                id SERIAL PRIMARY KEY,
                level VARCHAR(10) NOT NULL,
                industry VARCHAR(100) NOT NULL,
                category VARCHAR(100),
                knowledge JSONB NOT NULL,
                source VARCHAR(50) DEFAULT 'auto',
                version INTEGER DEFAULT 1,
                generated_at TIMESTAMP DEFAULT NOW(),
                expires_at TIMESTAMP,
                search_count INTEGER DEFAULT 0,
                correction_count INTEGER DEFAULT 0,
                UNIQUE(level, industry, category)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ik_lookup ON industry_knowledge(level, industry, category)")
        conn.commit()
    except Exception:
        try: conn.rollback()
        except Exception: pass
    finally:
        try: conn.close()
        except Exception: pass


def _ensure_industry_knowledge(industry: str, has_l1: bool = True):
    """造一条 industry_knowledge L1 数据或清掉."""
    _ensure_industry_knowledge_table()
    conn = _get_conn()
    try:
        cur = conn.cursor()
        if has_l1:
            knowledge = {
                "market_overview": "测试行业概述",
                "industry_terms": ["术语1", "术语2"],
                "top_brands": [{"name": "brandA"}, {"name": "brandB"}],
            }
            from datetime import datetime, timedelta
            expires = datetime.now() + timedelta(days=90)
            cur.execute(
                """
                INSERT INTO industry_knowledge (level, industry, category, knowledge, expires_at, generated_at)
                VALUES ('industry', %s, NULL, %s::jsonb, %s, NOW())
                ON CONFLICT (level, industry, category) DO UPDATE
                  SET knowledge = EXCLUDED.knowledge,
                      expires_at = EXCLUDED.expires_at,
                      generated_at = NOW()
                """,
                (industry, json.dumps(knowledge, ensure_ascii=False), expires),
            )
        else:
            cur.execute(
                "DELETE FROM industry_knowledge WHERE level='industry' AND industry=%s AND category IS NULL",
                (industry,),
            )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# FastAPI 客户端 fixture
# ============================================================


def _build_test_app(as_user_id: int = _TEST_USER_A):
    """装一个最小 FastAPI app,挂 geo_plan_task_router + brand_api.get_brand_completeness,
    middleware 用本测试专用的,自动把 as_user_id 注入 request.state.user."""
    from fastapi import FastAPI, Request
    from starlette.middleware.base import BaseHTTPMiddleware

    app = FastAPI()

    class _InjectUserMW(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            # 从 header 取 X-Test-User,否则用 default as_user_id
            uid_hdr = request.headers.get("x-test-user-id")
            if uid_hdr:
                uid = int(uid_hdr)
            else:
                uid = as_user_id
            request.state.user = {
                "user_id": uid,
                "username": f"test_u{uid}",
                "is_admin": False,
            }
            return await call_next(request)

    app.add_middleware(_InjectUserMW)

    # brand_api 里 get_brand_completeness 依赖 _get_user,同样拿 request.state.user
    from api.brand_api import router as brand_router
    app.include_router(brand_router)

    return app


def _client_for_user(user_id: int):
    """给某 user 建一个 TestClient."""
    from fastapi.testclient import TestClient
    app = _build_test_app(as_user_id=user_id)
    return TestClient(app)


# ============================================================
# Fixtures (session 级一次,测试结束全部清)
# ============================================================


class _Ctx:
    brand_a: int = 0
    brand_b: int = 0


_ctx = _Ctx()


def setup_module(module):
    _cleanup_test_brands()
    _ensure_test_users()
    _ctx.brand_a = _ensure_test_brand(_TEST_USER_A, industry="餐饮")
    _ctx.brand_b = _ensure_test_brand(_TEST_USER_B, industry="餐饮")
    _ensure_industry_knowledge("餐饮", has_l1=True)
    _ensure_industry_knowledge("虚空行业9999", has_l1=False)


def teardown_module(module):
    _cleanup_test_brands()


def _fresh_test():
    """每个 test 前清 tasks,保留 brand."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM geo_plan_tasks WHERE user_id IN (%s, %s)",
            (_TEST_USER_A, _TEST_USER_B),
        )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# 12 个 API 单测 (对齐 <behavior>)
# ============================================================


# ============================================================
# M3 Fix · brand completeness endpoint 3 个测试
# ============================================================


class TestBrandCompletenessAPI:

    def setup_method(self, m):
        _fresh_test()

    def teardown_method(self, m):
        _fresh_test()

    # M3 - 1. happy
    def test_brand_completeness_happy(self):
        # owner 查自己 brand
        ca = _client_for_user(_TEST_USER_A)
        r = ca.get(f"/api/brands/{_ctx.brand_a}/completeness")
        assert r.status_code == 200, r.text
        body = r.json()
        assert isinstance(body["score"], int)
        assert 0 <= body["score"] <= 100
        assert isinstance(body["groups"], dict)
        # utils/brand_completeness.py 4 组: identity / business / marketing / deep_analysis
        for k in ("identity", "business", "marketing", "deep_analysis"):
            assert k in body["groups"]
        assert isinstance(body["missing"], list)

    # M3 - 2. RBAC 403
    def test_brand_completeness_rbac_403(self):
        # user A 查 user B 的 brand
        ca = _client_for_user(_TEST_USER_A)
        r = ca.get(f"/api/brands/{_ctx.brand_b}/completeness")
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "brand_access_denied"

    # M3 - 3. 404
    def test_brand_completeness_not_found_404(self):
        ca = _client_for_user(_TEST_USER_A)
        r = ca.get("/api/brands/9999999999/completeness")
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "brand_not_found"


# ============================================================
# 独立跑:python <本文件>(只跑 TestBrandCompletenessAPI)
# ============================================================

if __name__ == "__main__":
    setup_module(None)
    try:
        comp = TestBrandCompletenessAPI()
        for m in [x for x in dir(comp) if x.startswith("test_")]:
            comp.setup_method(m)
            try:
                getattr(comp, m)()
                print(f"[PASS] TestBrandCompletenessAPI::{m}")
            except Exception as e:
                print(f"[FAIL] TestBrandCompletenessAPI::{m}: {type(e).__name__}: {e}")
            finally:
                comp.teardown_method(m)
    finally:
        teardown_module(None)
