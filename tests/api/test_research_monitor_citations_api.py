"""
P14 · 调研监测 引用明细 API (C2 引用明细)

锁住红线 (review 反馈):
  1. 3 个 endpoint 都 admin-only (anon 401 / normal 403)
  2. router 已注册到 server.py
  3. industries-summary / queries / query-detail 字段结构符合契约
  4. query-detail 返回 article_id 字段 (cites LEFT JOIN article_citations)
  5. engine 归一: 'doubao'/'豆包' 都映射到 '豆包'
  6. 验证 SQL 层做了窗口分页 (不再是全表回填到 Python)
  7. 前端 article_id deeplink 闭环 (CitationsPanel → ?tab=articles&article_id=N → ArticlesPanel)
  8. placement_api /details + /sources 也必须 admin-only (返 answer_text/cite_url 不能给代理)
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ============================================================
# 工具: fake conn/cur (跟其他 RM 测试同款)
# ============================================================

def _make_conn(rows_queue=None, rowcount=1):
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    cur.rowcount = rowcount
    queue = list(rows_queue or [])
    executed_sql = []
    executed_params = []

    def _execute(sql, params=None):
        executed_sql.append(sql)
        executed_params.append(params)
        cur._next_row = queue.pop(0) if queue else None

    cur.execute.side_effect = _execute
    cur.fetchone.side_effect = lambda: cur._next_row if not isinstance(cur._next_row, list) else (cur._next_row[0] if cur._next_row else None)
    cur.fetchall.side_effect = lambda: cur._next_row if isinstance(cur._next_row, list) else ([] if cur._next_row is None else [cur._next_row])
    conn.executed_sql = executed_sql
    conn.executed_params = executed_params
    return conn, cur


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
    from api.research_monitor_citations_api import router
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
# 1) 鉴权 (3 endpoint × {anon, normal} = 6 case · 锁 admin-only)
# ============================================================

class TestAdminOnly:
    def test_anon_industries_summary_401(self, anon_client):
        r = anon_client.get("/api/admin/research-monitor/citations/industries-summary")
        assert r.status_code == 401, r.text

    def test_normal_industries_summary_403(self, normal_client):
        r = normal_client.get("/api/admin/research-monitor/citations/industries-summary")
        assert r.status_code == 403, r.text

    def test_anon_queries_401(self, anon_client):
        r = anon_client.get("/api/admin/research-monitor/citations/queries?industry=房地产")
        assert r.status_code == 401, r.text

    def test_normal_queries_403(self, normal_client):
        r = normal_client.get("/api/admin/research-monitor/citations/queries?industry=房地产")
        assert r.status_code == 403, r.text

    def test_anon_query_detail_401(self, anon_client):
        r = anon_client.get(
            "/api/admin/research-monitor/citations/query-detail?industry=房地产&query=test"
        )
        assert r.status_code == 401, r.text

    def test_normal_query_detail_403(self, normal_client):
        r = normal_client.get(
            "/api/admin/research-monitor/citations/query-detail?industry=房地产&query=test"
        )
        assert r.status_code == 403, r.text


# ============================================================
# 2) industries-summary 字段契约
# ============================================================

def test_industries_summary_shape(admin_client):
    fake_now = datetime(2026, 5, 27, 10, 0, 0)
    rows = [
        {"industry": "房地产", "query_count": 29, "engine_count": 4,
         "citation_count": 3073, "last_cited_at": fake_now},
        {"industry": "汽车", "query_count": 25, "engine_count": 4,
         "citation_count": 2900, "last_cited_at": fake_now},
    ]
    conn, _ = _make_conn(rows_queue=[rows])
    with patch("api.research_monitor_citations_api.get_connection", return_value=conn):
        r = admin_client.get("/api/admin/research-monitor/citations/industries-summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "industries" in body and "total" in body
    assert body["total"] == 2
    assert body["industries"][0]["industry"] == "房地产"
    assert body["industries"][0]["engine_count"] == 4
    assert body["industries"][0]["last_cited_at"].startswith("2026-05-27")


# ============================================================
# 3) queries 字段契约 + engine 过滤校验
# ============================================================

def test_queries_basic_shape(admin_client):
    fake_now = datetime(2026, 5, 27, 10, 0, 0)
    # SELECT total → 1 行, SELECT 主表 → 1 行, SELECT engine_dist → 4 行
    total_row = {"c": 2}
    main_rows = [
        {"query": "烂尾楼怎么维权", "citation_count": 150, "engine_count": 4,
         "last_cited_at": fake_now},
        {"query": "买房摇号是什么流程", "citation_count": 143, "engine_count": 4,
         "last_cited_at": fake_now},
    ]
    dist_rows = [
        {"query": "烂尾楼怎么维权", "engine_norm": "豆包", "cnt": 38},
        {"query": "烂尾楼怎么维权", "engine_norm": "Kimi", "cnt": 42},
        {"query": "买房摇号是什么流程", "engine_norm": "DeepSeek", "cnt": 30},
    ]
    conn, _ = _make_conn(rows_queue=[total_row, main_rows, dist_rows])
    with patch("api.research_monitor_citations_api.get_connection", return_value=conn):
        r = admin_client.get(
            "/api/admin/research-monitor/citations/queries?industry=房地产"
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["industry"] == "房地产"
    assert body["total"] == 2
    assert len(body["queries"]) == 2
    q0 = body["queries"][0]
    assert q0["query"] == "烂尾楼怎么维权"
    assert q0["engines"] == {"豆包": 38, "Kimi": 42}


def test_queries_invalid_engine_400(admin_client):
    r = admin_client.get(
        "/api/admin/research-monitor/citations/queries?industry=房地产&engine=doubao"
    )
    # doubao 是脏名 · 必须用归一名 (前端 chip 也只暴露归一名)
    assert r.status_code == 400, r.text


def test_queries_industry_required(admin_client):
    r = admin_client.get("/api/admin/research-monitor/citations/queries")
    assert r.status_code == 422  # FastAPI 缺参数返 422


# ============================================================
# 4) query-detail 字段契约 + article_id + SQL 分页验证 (核心)
# ============================================================

def test_query_detail_returns_article_id_and_engine_groups(admin_client):
    fake_now = datetime(2026, 5, 27, 10, 0, 0)
    # Step 1: 聚合 + 窗口 → answers 列表
    # 1 个 engine '豆包' 的 1 个 answer
    step1_rows = [
        {"engine_norm": "豆包", "batch_id": "batch_001", "answer_md5": "abc123",
         "answer_text": "房地产合规要点...", "raw_engine": "doubao",
         "created_at": fake_now, "rn": 1, "total_in_engine": 3},
    ]
    # Step 2: cites 回查 (含 article_id LEFT JOIN)
    step2_rows = [
        {"raw_id": 100, "engine_norm": "豆包", "batch_id": "batch_001",
         "answer_md5": "abc123",
         "cited_platform": "知乎", "cite_position": 1,
         "cite_url": "https://zhihu.com/q/123",
         "cite_title": "如何看待房地产新政",
         "cite_excerpt": "近期房地产新政...",
         "article_id": 5566},  # MEDIUM fix · 已返回 article_id
        {"raw_id": 101, "engine_norm": "豆包", "batch_id": "batch_001",
         "answer_md5": "abc123",
         "cited_platform": "微博", "cite_position": 2,
         "cite_url": "https://weibo.com/p/456",
         "cite_title": None, "cite_excerpt": None,
         "article_id": None},  # 未归并到文章库 · 应为 None
    ]
    # answer_rows 非空 + batch_ids 非空 → 只走第一条 cite SQL
    conn, _ = _make_conn(rows_queue=[step1_rows, step2_rows])
    with patch("api.research_monitor_citations_api.get_connection", return_value=conn):
        r = admin_client.get(
            "/api/admin/research-monitor/citations/query-detail"
            "?industry=房地产&query=test_query"
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["industry"] == "房地产"
    assert body["query"] == "test_query"
    # 默认模式 · 4 个 engine 都出现 (空的 total_answers=0)
    engines = {g["engine"]: g for g in body["engines"]}
    assert set(engines.keys()) == {"豆包", "Kimi", "DeepSeek", "千问"}
    # 豆包 应该有 1 个 answer
    doubao = engines["豆包"]
    assert doubao["total_answers"] == 3
    assert doubao["has_more"] is True  # 3 > 0+1
    assert len(doubao["answers"]) == 1
    ans = doubao["answers"][0]
    # MEDIUM fix · cite 含 article_id 字段 (一个有 · 一个 None)
    assert ans["cite_count"] == 2
    assert ans["cites"][0]["article_id"] == 5566
    assert ans["cites"][1]["article_id"] is None
    assert ans["raw_engine"] == "doubao"  # 原始 engine 字段保留


def test_query_detail_sql_uses_window_pagination(admin_client):
    """HIGH fix · 验证 SQL 层用了 ROW_NUMBER 窗口 + LIMIT · 不再全表 Python 切片"""
    conn, _ = _make_conn(rows_queue=[[], []])
    with patch("api.research_monitor_citations_api.get_connection", return_value=conn):
        r = admin_client.get(
            "/api/admin/research-monitor/citations/query-detail"
            "?industry=房地产&query=q&answers_per_engine=5"
        )
    assert r.status_code == 200, r.text
    sqls = conn.executed_sql
    # 第一句必须含 ROW_NUMBER + PARTITION BY engine_norm (窗口分页)
    assert any("ROW_NUMBER" in s and "PARTITION BY engine_norm" in s for s in sqls), \
        "query-detail Step 1 必须在 SQL 层用 ROW_NUMBER 窗口 · 不能全表回填 Python"


def test_query_detail_order_by_nulls_last(admin_client):
    """LOW fix · 验证 ORDER BY created_at DESC NULLS LAST · 防 legacy NULL 时间戳排前面"""
    conn, _ = _make_conn(rows_queue=[[], []])
    with patch("api.research_monitor_citations_api.get_connection", return_value=conn):
        admin_client.get(
            "/api/admin/research-monitor/citations/query-detail"
            "?industry=x&query=y"
        )
    sqls = conn.executed_sql
    win_sqls = [s for s in sqls if "ROW_NUMBER" in s]
    assert win_sqls, "必须有窗口分页 SQL"
    assert any("NULLS LAST" in s for s in win_sqls), \
        "窗口 ORDER BY created_at DESC 必须带 NULLS LAST"


def test_query_detail_cite_sql_left_joins_article_citations(admin_client):
    """MEDIUM fix · 验证 cite 回查 SQL 用 LEFT JOIN geo_research_article_citations 取 article_id"""
    fake_now = datetime(2026, 5, 27)
    step1_rows = [
        {"engine_norm": "豆包", "batch_id": "b1", "answer_md5": "m1",
         "answer_text": "x", "raw_engine": "doubao", "created_at": fake_now,
         "rn": 1, "total_in_engine": 1},
    ]
    conn, _ = _make_conn(rows_queue=[step1_rows, []])
    with patch("api.research_monitor_citations_api.get_connection", return_value=conn):
        admin_client.get(
            "/api/admin/research-monitor/citations/query-detail"
            "?industry=房地产&query=q"
        )
    cite_sqls = [s for s in conn.executed_sql if "cite_url" in s and "LEFT JOIN" in s.upper()]
    assert cite_sqls, "cite 回查 SQL 必须 LEFT JOIN article_citations"
    assert any("article_id" in s for s in cite_sqls), \
        "cite SQL 必须 SELECT ac.article_id"


def test_query_detail_invalid_engine_400(admin_client):
    r = admin_client.get(
        "/api/admin/research-monitor/citations/query-detail"
        "?industry=房地产&query=q&engine=doubao"
    )
    assert r.status_code == 400, r.text


# ============================================================
# 5) engine 归一 (锁住 _normalize_engine 行为)
# ============================================================

def test_normalize_engine_helper():
    from api.research_monitor_citations_api import _normalize_engine, CANONICAL_ENGINES
    assert _normalize_engine("doubao") == "豆包"
    assert _normalize_engine("豆包") == "豆包"
    assert _normalize_engine("Kimi") == "Kimi"
    assert _normalize_engine("kimi") == "Kimi"
    assert _normalize_engine("DeepSeek") == "DeepSeek"
    assert _normalize_engine("deepseek") == "DeepSeek"
    assert _normalize_engine("qwen") == "千问"
    assert _normalize_engine("千问") == "千问"
    assert _normalize_engine("") == ""
    assert _normalize_engine(None) == ""
    assert set(CANONICAL_ENGINES) == {"豆包", "Kimi", "DeepSeek", "千问"}


# ============================================================
# 6) router 已注册到 server.py
# ============================================================

def test_router_registered_in_server():
    """读 server.py 字符串确认 P14 模块名出现在注册列表"""
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "'api.research_monitor_citations_api'" in src or \
           '"api.research_monitor_citations_api"' in src, \
           "server.py 必须包含 P14 router 注册"


# ============================================================
# 7) 前端 article_id deeplink 闭环 (inspect 测试 · 锁文件内容)
# ============================================================
# 前端没 React 单测框架 · 用源码 inspect 验关键引线没断

_FRONTEND_RM = ROOT / "frontend" / "src" / "pages" / "Admin" / "ResearchMonitor"


def test_articles_panel_accepts_initial_article_id_prop():
    src = (_FRONTEND_RM / "ArticlesPanel.tsx").read_text(encoding="utf-8")
    assert "initialArticleId" in src, \
        "ArticlesPanel 必须接收 initialArticleId prop (引用明细跳过来用)"
    assert "ArticlesPanelProps" in src, "ArticlesPanel 必须有 props 类型定义"
    # 必须用 prop 初始化 selectedArticleId
    assert "useState<number | null>(initialArticleId ?? null)" in src, \
        "selectedArticleId 必须用 initialArticleId 初始化"
    # 必须监听 prop 变化同步
    assert "initialArticleId !== selectedArticleId" in src, \
        "必须有 useEffect 监听 initialArticleId 变化"


def test_research_monitor_index_passes_article_id_to_articles_panel():
    src = (_FRONTEND_RM / "index.tsx").read_text(encoding="utf-8")
    assert "article_id" in src, "index.tsx 必须读 URL ?article_id"
    assert "urlArticleId" in src
    assert "initialArticleId={urlArticleId}" in src, \
        "ArticlesPanel 必须收到 initialArticleId={urlArticleId}"


def test_citations_panel_navigates_with_article_id():
    src = (_FRONTEND_RM / "CitationsPanel.tsx").read_text(encoding="utf-8")
    assert "tab=articles&article_id=" in src, \
        "CitationsPanel 文章库按钮必须跳 ?tab=articles&article_id=N"


# ============================================================
# 8) placement_api /details + /sources 必须 admin-only
#    (review 反馈: 这俩端点返回 answer_text/cite_url/cite_excerpt
#     · 前端发布参谋不调 · 但代理可从 Network 手抓 · 后端必须拦)
# ============================================================

def test_placement_research_details_endpoint_is_admin_only():
    src = (ROOT / "api" / "placement_api.py").read_text(encoding="utf-8")
    # 锁 /details endpoint 函数体含 _require_admin(request)
    import re
    m = re.search(
        r'@router\.get\("/research/industry/\{industry\}/details".*?\n'
        r'async def get_industry_query_details\([^)]*\):.*?(?=\n@router|\nasync def )',
        src, re.DOTALL,
    )
    assert m, "找不到 /research/industry/{industry}/details endpoint 函数体"
    body = m.group(0)
    assert "_require_admin(request)" in body, \
        "/details endpoint 必须调 _require_admin(request) · 返 answer_text 不能给代理"


def test_placement_research_sources_endpoint_is_admin_only():
    src = (ROOT / "api" / "placement_api.py").read_text(encoding="utf-8")
    import re
    m = re.search(
        r'@router\.get\("/research/industry/\{industry\}/sources".*?\n'
        r'async def get_industry_source_analysis\([^)]*\):.*?(?=\n@router|\nasync def )',
        src, re.DOTALL,
    )
    assert m, "找不到 /research/industry/{industry}/sources endpoint 函数体"
    body = m.group(0)
    assert "_require_admin(request)" in body, \
        "/sources endpoint 必须调 _require_admin(request) · 返 url/excerpt 不能给代理"
