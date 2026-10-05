"""Call the real legacy /api/articles/plan handler against an isolated DB.

The server module has startup-side schema behavior, so this validator requires
an explicitly isolated local database via ``GEO_ARTICLE_PLAN_ROUTE_TEST_DSN``.
It never starts the ASGI lifespan or an external provider.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _dsn() -> str:
    value = os.getenv("GEO_ARTICLE_PLAN_ROUTE_TEST_DSN", "").strip()
    if not value:
        raise RuntimeError("GEO_ARTICLE_PLAN_ROUTE_TEST_DSN is required")
    lowered = value.lower()
    if "127.0.0.1" not in lowered and "localhost" not in lowered:
        raise RuntimeError("legacy route validator accepts only a local isolated database")
    return value


async def main() -> None:
    dsn = _dsn()
    os.environ["DATABASE_URL"] = dsn
    os.environ["TEST_DATABASE_URL"] = dsn
    os.environ.pop("ROLE", None)

    import auth.brand_access as brand_access
    import db.connection as connection_module
    import db.diagnosis_db as diagnosis_db
    import writing.distiller as distiller_module
    import writing.topic_dispatcher as dispatcher_module
    import server
    from writing.evidence_first_policy import evaluate_content_trust

    # The import above is the actual disaster-recovery bootstrap.  Prove its
    # canonical runtime relationship with a real write, not just table order.
    conn = connection_module.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO topics DEFAULT VALUES RETURNING id")
        topic_id = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO articles(topic_id,title,content) VALUES (%s,'safe','safe body') RETURNING id",
            (topic_id,),
        )
        article_id = int(cur.fetchone()["id"])
        cur.execute("UPDATE topics SET article_id=%s WHERE id=%s", (article_id, topic_id))
        cur.execute(
            "SELECT a.id FROM topics t JOIN articles a ON a.id=t.article_id WHERE t.id=%s",
            (topic_id,),
        )
        assert int(cur.fetchone()["id"]) == article_id
        cur.execute("SELECT 1 AS hit FROM article_generations WHERE id=%s", (article_id,))
        assert cur.fetchone() is None
        conn.rollback()
    finally:
        conn.close()

    brand_access.require_diagnosis_access = lambda *_args, **_kwargs: None
    diagnosis_db.get_client_materials = lambda *_args, **_kwargs: {}
    server.calculate_distribution = lambda count, _industry=None: {
        "authority_ranking": {"count": max(1, count // 2), "ratio": 0.5},
        "deep_comparison": {"count": count - max(1, count // 2), "ratio": 0.5},
    }
    server.get_diagnosis_by_id = lambda _diagnosis_id: {
        "id": 1,
        "brand_id": None,
        "brand_name": "测试客户",
        "industry": "生命科学",
        "keywords": json.dumps(["细胞治疗药物研发和生产隔离器"], ensure_ascii=False),
        "raw_data_json": "{}",
        "total_score": 0,
        "level": "",
    }

    class _Distiller:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self):
            return {"client_profile": "{}", "selling_points": "{}", "competitor_analysis": "{}"}

    distiller_module.DistillerPipeline = _Distiller

    class _ShortUnsafeDispatcher:
        def __init__(self, *_args, **_kwargs):
            pass

        async def generate_topics(self):
            return [{
                "title": "2026年隔离器服务商TOP10权威测评",
                "keyword": "细胞治疗药物研发和生产隔离器",
                "type": "authority_ranking",
            }]

    dispatcher_module.TopicDispatcher = _ShortUnsafeDispatcher
    response = await server.plan_articles(
        server.ArticlePlanRequest(diagnosis_id=1, total_count=4), object()
    )
    assert len(response["topics"]) == 4, response
    assert all(not evaluate_content_trust(topic["title"], "").hard for topic in response["topics"])

    class _FailedDispatcher:
        def __init__(self, *_args, **_kwargs):
            pass

        async def generate_topics(self):
            raise RuntimeError("main and fallback LLM unavailable")

    dispatcher_module.TopicDispatcher = _FailedDispatcher
    for missing_keys in (False, True):
        if missing_keys:
            for key in ("DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY"):
                os.environ.pop(key, None)
        response = await server.plan_articles(
            server.ArticlePlanRequest(diagnosis_id=1, total_count=3), object()
        )
        assert len(response["topics"]) == 3, response
        assert all(not evaluate_content_trust(topic["title"], "").hard for topic in response["topics"])

    route = next(
        route for route in server.app.routes
        if getattr(route, "path", None) == "/api/articles/plan"
        and "POST" in getattr(route, "methods", set())
    )
    assert route.endpoint is server.plan_articles

    # Exercise the registered retired billed route through FastAPI routing.
    # Every former side effect is replaced with a trap so a regression cannot
    # deduct, refund, call an LLM, read a quote, or write topics before 410.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import middleware.billing as billing

    side_effects: list[str] = []

    def _sync_trap(name):
        def _trap(*_args, **_kwargs):
            side_effects.append(name)
            raise AssertionError(f"retired route executed forbidden side effect: {name}")
        return _trap

    async def _async_trap(*_args, **_kwargs):
        side_effects.append("billing_or_llm")
        raise AssertionError("retired route executed billing or LLM")

    server.get_keywords_by_quote = _sync_trap("quote_read")
    server.save_topics = _sync_trap("topic_write")
    billing.check_balance_only = _async_trap
    billing.deduct_points = _async_trap
    billing.refund_points = _async_trap
    setattr(dispatcher_module.TopicDispatcher, "generate_single_title", _async_trap)

    retired_route = next(
        route for route in server.app.routes
        if getattr(route, "path", None) == "/api/topics/generate"
        and "POST" in getattr(route, "methods", set())
    )
    route_app = FastAPI()
    route_app.router.routes.append(retired_route)
    response = TestClient(route_app).post(
        "/api/topics/generate",
        json={"quote_id": 987654321},
    )
    assert response.status_code == 410, response.text
    assert response.json() == {
        "detail": {
            "code": "TOPICS_GENERATE_RETIRED",
            "message": "旧选题生成接口已退役，请使用写作大厅生成标题。",
            "replacement": "POST /api/writing/generate-titles",
        }
    }
    assert side_effects == []
    print("PASS | real legacy article-plan safety + retired billed topic route 410")


if __name__ == "__main__":
    asyncio.run(main())
