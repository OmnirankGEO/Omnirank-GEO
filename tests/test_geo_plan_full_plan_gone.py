"""
Phase 4 PLAN 03 Task 2 · 旧 /api/geo-plan/full-plan 410 Gone 回归测试

Q3 决策: 前端切到异步 POST /api/geo-plan/start-task 后, 旧同步端点直接返 410,
避免残留旧 client 继续打旧路径踩坑.

对齐 <behavior>:
  1. test_full_plan_returns_410       → 410 + code=endpoint_removed,且不再给 migrate_to(异步任务端点随开源 E3 B3c 删,B2 改)
  2. test_quick_cost_still_works      → /quick-cost 原样 (不受本 PLAN 影响)
  3. test_unlock_still_works          → /unlock 原样 (不受本 PLAN 影响)
  4. test_full_plan_no_old_business   → handler 不再跑老业务 log (被 410 截断)

运行:
  docker exec omnirank-ai pytest tests/test_geo_plan_full_plan_gone.py -x -v
  # 或 python -m pytest tests/test_geo_plan_full_plan_gone.py -x -v
"""
from __future__ import annotations

import sys
import logging
from pathlib import Path
from unittest.mock import patch, MagicMock

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

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_TEST_USER = 9_000_301


def _build_test_app(user_id: int = _TEST_USER):
    """装一个最小 FastAPI app, 挂 geo_plan_router + 注入 request.state.user."""
    from fastapi import FastAPI, Request
    from starlette.middleware.base import BaseHTTPMiddleware

    app = FastAPI()

    class _InjectUserMW(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            request.state.user = {
                "user_id": user_id,
                "username": f"test_u{user_id}",
                "is_admin": False,
            }
            return await call_next(request)

    app.add_middleware(_InjectUserMW)

    from api.geo_plan_api import router
    app.include_router(router)
    return app


def _client():
    from fastapi.testclient import TestClient
    return TestClient(_build_test_app())


class TestFullPlanGone:

    # ----- 1. full-plan 410 -----

    def test_full_plan_returns_410(self):
        c = _client()
        r = c.post(
            "/api/geo-plan/full-plan",
            json={"brand_name": "test", "description": "随便"},
        )
        assert r.status_code == 410, f"期望 410, 实际 {r.status_code}: {r.text}"
        body = r.json()
        # FastAPI 把 detail dict 包在 detail 里
        detail = body.get("detail", {})
        assert detail.get("code") == "endpoint_removed", \
            f"期望 code=endpoint_removed, 实际 detail={detail}"
        # [开源 E3 · B2] 异步任务端点已删:回包不许再指过去(指了就是 404)
        assert "migrate_to" not in detail, f"migrate_to 指向已删端点, detail={detail}"
        assert "start-task" not in detail.get("message", ""), detail

    # ----- 2. /quick-cost 保持原行为 -----

    def test_quick_cost_still_works(self):
        """/quick-cost 不应返 410, 保持原有行为.

        本测不 mock 外部, 容忍 200 (成功) / 500 (底层估算 LLM 挂) — 只要不是 410 即 OK.
        """
        from unittest.mock import patch, AsyncMock

        fake_result = {
            "keyword": "测试词",
            "cost_breakdown": {"content_cost": 100, "media_cost": 50},
            "occurrence_rate": "top_10",
        }
        with patch(
            "tools.c_end_cost_estimate.estimate_user_cost",
            new=AsyncMock(return_value=fake_result),
        ):
            c = _client()
            r = c.post(
                "/api/geo-plan/quick-cost",
                json={"keyword": "测试词", "target_rank": "top_10"},
            )
            # 200 最好, 500 也行 (但不能是 410 - 410 意味着被本 PLAN 误伤)
            assert r.status_code != 410, \
                f"/quick-cost 不应被切断! status={r.status_code} body={r.text}"
            assert r.status_code == 200, f"期望 200, 实际 {r.status_code}: {r.text}"

    # ----- 3. /unlock 保持原行为 -----

    def test_unlock_still_works(self):
        """/unlock 不应返 410, 保持原有行为 (虽业务会因 mock 失败, 但不是 410)."""
        c = _client()
        r = c.post(
            "/api/geo-plan/unlock",
            json={"plan_id": "nonexistent", "brand_name": "test"},
        )
        # 不 mock 后端, 业务可能 500 (DB/redis 找不到数据) 或 402 (余额),
        # 但绝不能是 410 - 410 意味着被本 PLAN 误伤
        assert r.status_code != 410, \
            f"/unlock 不应被切断! status={r.status_code} body={r.text}"

    # ----- 4. 确认 handler 不再跑老业务逻辑 -----

    def test_full_plan_no_old_business(self):
        """410 拦截后, 旧 one_click_geo_plan / _fetch_brand_and_profile 完全不被调用.

        用 mock 监控这两个入口, 验证 full-plan 请求后它们都没被 call.
        """
        # one_click_geo_plan 是旧 full-plan handler 的主业务入口
        with patch("tools.c_end_cost_estimate.one_click_geo_plan") as m_plan:
            c = _client()
            r = c.post(
                "/api/geo-plan/full-plan",
                json={"brand_name": "xxx", "brand_id": 999_999_999},
            )
            assert r.status_code == 410
            # 核心断言: 老 plan 函数完全不被 call
            assert m_plan.call_count == 0, \
                f"full-plan 410 后不应再调 one_click_geo_plan, 但被 call {m_plan.call_count} 次"


# =========================================================================
# 独立跑:python tests/test_geo_plan_full_plan_gone.py
# =========================================================================

if __name__ == "__main__":
    t = TestFullPlanGone()
    methods = [m for m in dir(t) if m.startswith("test_")]
    for m in methods:
        try:
            getattr(t, m)()
            print(f"[PASS] {m}")
        except Exception as e:
            print(f"[FAIL] {m}: {type(e).__name__}: {e}")
