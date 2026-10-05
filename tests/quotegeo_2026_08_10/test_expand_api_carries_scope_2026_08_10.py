"""§6.3 API 层锁:`POST /api/keywords/expand` 真的带上 city / business_scope / scope_lock,
并且响应里的默认选择就是三轴决策的结果。

R4 原文说"不是前端漏传"——前端确实传了。所以这里要证明的是**后端收到之后有没有用**:
请求带业务范围 vs 不带业务范围,同一批候选的默认选择必须不同。
只断"能返 200"是零判别力的。

🔴 真跑 handler:走 FastAPI TestClient 打真实路由,不是直接调 expand_keywords。
🔴 `server.py` 在 import 期建表/初始化连接池 → 必须在 import 之前把 DATABASE_URL
   钉到一次性 loopback 测试库(同 tests/custfb_2026_08_09 的做法)。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PG_URL = os.environ.get("TEST_DATABASE_URL")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

if not PG_URL:
    pytest.skip("需要 TEST_DATABASE_URL(一次性 loopback 测试库)", allow_module_level=True)
_parsed = urlsplit(PG_URL)
if (_parsed.hostname or "").lower() not in LOOPBACK_HOSTS:
    pytest.skip(f"只允许 loopback 一次性容器 DSN:{_parsed.hostname}", allow_module_level=True)
_base_db = (_parsed.path or "").lstrip("/").lower()
if "prod" in _base_db or "test" not in _base_db:
    pytest.skip(f"基础库名必须含 test 且不含 prod:{_base_db}", allow_module_level=True)

# ── 生产同构 schema:必须在 import server 之前建好并钉进 DATABASE_URL ──────────
# 🔴 为什么不用 `init_db()` 自举的那套表:实测它建出来的 `users` 没有 unique(username)、
#    也缺 `get_user()` 要 JOIN 的几张表,于是 `create_jwt()` 里第一条 SQL 就把
#    池化连接打进 aborted transaction,后面每条 SQL 都报
#    `InFailedSqlTransaction` —— 看起来像我的测试写错了,其实是夹具与生产不同构。
#    改用**生产整库 schema 快照**(`tests/orphanmon_2026_08_10/prod_schema_snapshot.sql`,
#    2026-08-10 随 P0 孤儿监测词包一起提交进仓)。
import psycopg2  # noqa: E402
from psycopg2 import sql as _sql  # noqa: E402

_SNAPSHOT = ROOT / "tests" / "orphanmon_2026_08_10" / "prod_schema_snapshot.sql"
if not _SNAPSHOT.exists():
    pytest.skip(f"缺生产 schema 快照:{_SNAPSHOT}", allow_module_level=True)

_API_DB = (_base_db + "_quotegeo_api")[:60]
_admin = psycopg2.connect(PG_URL)
_admin.autocommit = True
with _admin.cursor() as _cur:
    _cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (_API_DB,))
    _fresh = _cur.fetchone() is None
    if _fresh:
        _cur.execute(_sql.SQL("CREATE DATABASE {}").format(_sql.Identifier(_API_DB)))
_admin.close()

_API_URL = PG_URL.rsplit("/", 1)[0] + "/" + _API_DB
if _fresh:
    _raw = _SNAPSHOT.read_text(encoding="utf-8")
    # pg_dump 的元命令(\restrict 等)psycopg2 执行不了;`CREATE SCHEMA public` 已存在;
    # 且 dump 会把 search_path 置空 → 必须显式设回 public(三条都是实测踩出来的)。
    _ddl = "\n".join(
        line for line in _raw.splitlines()
        if not line.startswith("\\") and line.strip() != "CREATE SCHEMA public;"
    )
    _conn = psycopg2.connect(_API_URL)
    _conn.autocommit = True
    with _conn.cursor() as _cur:
        _cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        _cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        _cur.execute("SET search_path TO public")
        _cur.execute(_ddl)
    _conn.close()

os.environ["DATABASE_URL"] = _API_URL
PG_URL = _API_URL

import server  # noqa: E402  (import 期建表 —— 必须排在 DATABASE_URL 之后)
from fastapi.testclient import TestClient  # noqa: E402

from services.keyword_delivery_decision import DELIVERY_POLICY_VERSION  # noqa: E402

CANDIDATES = [
    "深圳龙岗商场招商电话",
    "深圳龙岗建材市场有哪些",
    "商场推荐",
    "商业综合体设计公司",
    "商场是什么",
]

SCOPE_LOCK = {
    "service_market": ["深圳"],
    "market_level": "district",
    "business_type": "B2C",
    "buyer_persona": "本地商业项目招商负责人",
    "real_query_seeds": [],
    "sub_regions": ["龙岗", "龙岗区"],
    "provinces": ["广东"],
    "source": "manual",
}


@pytest.fixture(scope="module")
def auth_headers() -> dict:
    """真建一个服务商账号并签真 JWT —— 路由上的 RBAC 中间件照常跑,不绕过。"""
    import psycopg2
    from psycopg2.extras import RealDictCursor

    from auth.jwt_utils import create_jwt

    conn = psycopg2.connect(PG_URL, cursor_factory=RealDictCursor)
    conn.autocommit = True
    # 🔴 users.username **没有** unique 约束(实测 \d users:只有 users_pkey),
    #    所以不能用 ON CONFLICT —— 先查后插。
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE username = %s",
                    ("quotegeo_probe_2026_08_10",))
        row = cur.fetchone()
        if row:
            user_id = row["id"]
        else:
            cur.execute(
                "INSERT INTO users (username, password_hash, display_name, is_active) "
                "VALUES (%s, %s, %s, 1) RETURNING id",
                ("quotegeo_probe_2026_08_10", "x", "扩词API锁探针"),
            )
            user_id = cur.fetchone()["id"]
    # 真授权:建角色 + role_permissions(quote:write)+ user_roles 绑定。
    # 🔴 不 monkeypatch 权限函数 —— 那样这条链路就没被测到,上面的 401 反向对照也白搭。
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM roles WHERE name = %s", ("quotegeo_probe_role",))
        row = cur.fetchone()
        if row:
            role_id = row["id"]
        else:
            cur.execute(
                "INSERT INTO roles (name, display_name) VALUES (%s, %s) RETURNING id",
                ("quotegeo_probe_role", "扩词API锁角色"),
            )
            role_id = cur.fetchone()["id"]
        for module, level in (("quote", "write"), ("keyword", "write"), ("brand", "write")):
            cur.execute(
                "SELECT 1 FROM role_permissions WHERE role_id = %s AND module = %s AND level = %s",
                (role_id, module, level),
            )
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO role_permissions (role_id, module, level) VALUES (%s, %s, %s)",
                    (role_id, module, level),
                )
        cur.execute("SELECT 1 FROM user_roles WHERE user_id = %s AND role_id = %s",
                    (user_id, role_id))
        if cur.fetchone() is None:
            cur.execute("INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s)",
                        (user_id, role_id))
    conn.close()
    token = create_jwt(user_id)
    assert token, "签发 JWT 失败 —— 锁失效,不是通过"
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def client():
    return TestClient(server.app, raise_server_exceptions=False)


def test_endpoint_really_requires_auth(client, auth_headers):
    """反向对照:不带 token 必须 401。

    没有这一条,下面所有 200 断言都可能是在一个"根本没有鉴权"的路由上跑的。
    """
    response = client.post("/api/keywords/expand", json={"core_keywords": ["x"]})
    assert response.status_code == 401, response.text


@pytest.fixture(autouse=True)
def _stub_upstreams(monkeypatch):
    """只替换两个**外部数据源**;路由、鉴权、三轴决策全部真跑。"""
    from tools import keyword_expander as ke

    async def fake_llm_expand(*_a, **_k):
        return [(kw, "行业核心") for kw in CANDIDATES]

    async def fake_5118(*_a, **_k):
        return []

    async def fake_drill(*_a, **_k):
        return {"sub_regions": [{"name": "龙岗区"}, {"name": "龙岗"}], "region_aliases": []}

    monkeypatch.setattr(ke.KeywordExpander, "_llm_expand", fake_llm_expand, raising=False)
    monkeypatch.setattr(ke.KeywordExpander, "_fetch_5118", fake_5118, raising=False)
    monkeypatch.setattr(ke.KeywordExpander, "_5118_expand", fake_5118, raising=False)
    monkeypatch.setattr(ke.KeywordExpander, "_drill_region", fake_drill, raising=False)
    monkeypatch.setattr(ke.KeywordExpander, "llm_api_key", "test-key", raising=False)
    # 范围锁定裁决:请求里已带 scope_lock,不让它去查库/调 LLM
    monkeypatch.setattr(
        server, "_resolve_quote_scope_lock",
        lambda request: _async_value(dict(request.scope_lock or SCOPE_LOCK)),
        raising=False,
    )


def _async_value(value):
    async def _coro():
        return value
    return _coro()


def _post(client, headers, **overrides) -> dict:
    payload = {
        "core_keywords": ["商场招商"],
        "industry": "商业地产运营",
        "city": "广东省深圳市龙岗区",
        "business_scope": "商场运营、品牌招商、商铺租赁、餐饮入驻、家具建材经营",
        "target_count": 60,
        "scope_lock": SCOPE_LOCK,
    }
    payload.update(overrides)
    response = client.post("/api/keywords/expand", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _index(body: dict) -> dict:
    rows = {}
    for row in body.get("keywords", []):
        rows[row["keyword"]] = row
    for row in body.get("rejected_keywords", []):
        rows.setdefault(row["keyword"], row)
    return rows


def test_response_carries_the_three_axes(client, auth_headers):
    rows = _index(_post(client, auth_headers))
    for keyword in CANDIDATES:
        assert keyword in rows, f"「{keyword}」不在 API 响应里 = 被静默丢弃"
        row = rows[keyword]
        for field in ("commercial_intent", "business_scope", "geo_scope",
                      "reason_code", "reason_group", "reason_text",
                      "human_override_allowed", "delivery_policy_version"):
            assert field in row, (keyword, field, sorted(row))
        assert row["delivery_policy_version"] == DELIVERY_POLICY_VERSION


def test_default_selection_matches_the_screenshots_after_fix(client, auth_headers):
    rows = _index(_post(client, auth_headers))
    assert rows["深圳龙岗商场招商电话"]["default_selected"] is True
    assert rows["深圳龙岗建材市场有哪些"]["default_selected"] is True
    assert rows["商场推荐"]["default_selected"] is False
    assert rows["商场推荐"]["reason_group"] == "geo_too_broad"
    assert rows["商业综合体设计公司"]["default_selected"] is False
    assert rows["商业综合体设计公司"]["reason_group"] == "scope_mismatch"
    assert rows["商场是什么"]["reason_group"] == "knowledge"


def test_business_scope_actually_changes_the_answer(client, auth_headers):
    """成对判据:只改 business_scope 一个字段,默认选择必须变。

    这是"后端有没有真的用 business_scope"的唯一硬证据 —— 只断 200 证明不了任何事。
    """
    with_scope = _index(_post(client, auth_headers))
    without_scope = _index(_post(client, auth_headers, business_scope=""))
    assert with_scope["商业综合体设计公司"]["business_scope"] == "mismatched"
    assert without_scope["商业综合体设计公司"]["business_scope"] == "uncertain"


def test_scope_lock_market_level_actually_changes_the_answer(client, auth_headers):
    """成对判据:只改 scope_lock.market_level,地域轴必须翻。"""
    local = _index(_post(client, auth_headers))
    national = _index(_post(
        client, auth_headers,
        city="",
        scope_lock={**SCOPE_LOCK, "market_level": "national", "service_market": [],
                    "sub_regions": [], "provinces": []},
    ))
    assert local["商场推荐"]["geo_scope"] == "too_broad"
    assert national["商场推荐"]["geo_scope"] == "matched"
    assert local["商场推荐"]["default_selected"] is False
    assert national["商场推荐"]["default_selected"] is True


def test_legacy_fields_still_present_for_old_clients(client, auth_headers):
    """T6 兼容性:老字段还在,且由三轴派生(混部期老前端不能白屏)。"""
    rows = _index(_post(client, auth_headers))
    row = rows["商场推荐"]
    for field in ("geo_recommend", "scope_match", "default_selected",
                  "rejection_reason", "policy_version"):
        assert field in row, sorted(row)
    assert row["geo_recommend"] is True      # 商业上成立
    assert row["scope_match"] is False       # 但地域过宽
