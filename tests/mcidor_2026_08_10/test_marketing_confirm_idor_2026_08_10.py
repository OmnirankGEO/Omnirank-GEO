"""🔴 P0 · 营销确认端点跨租户越权(IDOR)热修 —— 正反成对行为锁。

工单 `C:\\AI-Test\\WO_P0_MARKETING_CONFIRM_IDOR_2026-08-10.md`(H0 第 3 类:租户隔离/隐私)。

复现的那条链(修复前 · 生产实测 200):
  任意登录用户 `GET /api/marketing-confirm/status/{别人家 brand_id}` → 200 + 真 confirm token
  → 该 token 喂公开门户 `GET /api/m/{token}` → 完整客户物料快照。

判据全部打在**真跑 handler + 真 PostgreSQL + 真 RBAC 中间件**上,不做源码字符串断言:
  锁1  跨租户 status → 404(不是 403:404 才不泄露"这个 brand 存在")
  锁2  **反向对照** 自己名下 brand → 200(否则锁1 可能只是"恒拒",零判别力)
  锁3  匿名(无 token)→ 401
  锁4  跨租户 resend → 404 **且库里那一行一个字没动**
       (只断 404 不够:先写后判的实现照样能返 404 而数据已改)
  锁5  跨租户 generate-link → 404 **且没有新 session 行落库**
  锁6  **反向对照** 自己名下 resend/generate-link 真的成功(证明上面不是恒拒)
  锁7  三个端点都真的接了同一把闸(接线锁打在接线上,不是函数上)
  锁8  公开门户 `/api/m/{token}` **保持公开**(token-only 是设计,本单不许把它改坏)

🔴 夹具用**生产整库 schema 快照**:`init_db()` 自举那套表缺 unique/缺表,
   会把池化连接打进 aborted transaction,看着像测试写错其实是夹具不同构(2026-08-10 实测)。
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

import psycopg2  # noqa: E402
from psycopg2 import sql as _sql  # noqa: E402
from psycopg2.extras import RealDictCursor  # noqa: E402

_SNAPSHOT = ROOT / "tests" / "orphanmon_2026_08_10" / "prod_schema_snapshot.sql"
if not _SNAPSHOT.exists():
    pytest.skip(f"缺生产 schema 快照:{_SNAPSHOT}", allow_module_level=True)

_DB = (_base_db + "_mcidor")[:60]
_admin = psycopg2.connect(PG_URL)
_admin.autocommit = True
with _admin.cursor() as _cur:
    _cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (_DB,))
    _fresh = _cur.fetchone() is None
    if _fresh:
        _cur.execute(_sql.SQL("CREATE DATABASE {}").format(_sql.Identifier(_DB)))
_admin.close()

_URL = PG_URL.rsplit("/", 1)[0] + "/" + _DB
if _fresh:
    _raw = _SNAPSHOT.read_text(encoding="utf-8")
    _ddl = "\n".join(
        line for line in _raw.splitlines()
        if not line.startswith("\\") and line.strip() != "CREATE SCHEMA public;"
    )
    _c = psycopg2.connect(_URL)
    _c.autocommit = True
    with _c.cursor() as _cur:
        _cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        _cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        _cur.execute("SET search_path TO public")
        _cur.execute(_ddl)
    _c.close()

os.environ["DATABASE_URL"] = _URL

import server  # noqa: E402  (import 期建表 —— 必须排在 DATABASE_URL 之后)
from fastapi.testclient import TestClient  # noqa: E402

from auth.jwt_utils import create_jwt  # noqa: E402

# 复刻事故现场的两方:
#   VICTIM = 被越权读取的真实客户(生产里是 QZQZ 662)
#   ATTACKER = 零模块权限、只拥有自己一个 brand 的普通账号(生产里是 QA u114)
ATTACKER_USER = "mcidor_attacker_2026_08_10"
VICTIM_USER = "mcidor_victim_2026_08_10"
VICTIM_TOKEN = "victim-token-must-not-leak"


def _conn():
    c = psycopg2.connect(_URL, cursor_factory=RealDictCursor)
    c.autocommit = True
    return c


def _ensure_user(cur, username: str) -> int:
    # 🔴 users.username 没有 unique 约束(实测 \d users 只有 users_pkey)→ 不能 ON CONFLICT
    cur.execute("SELECT id FROM users WHERE username = %s", (username,))
    row = cur.fetchone()
    if row:
        return row["id"]
    cur.execute(
        "INSERT INTO users (username, password_hash, display_name, is_active) "
        "VALUES (%s, 'x', %s, 1) RETURNING id",
        (username, username),
    )
    return cur.fetchone()["id"]


def _ensure_brand(cur, name: str, owner_user_id: int) -> int:
    cur.execute("SELECT id FROM brands WHERE name = %s AND owner_user_id = %s",
                (name, owner_user_id))
    row = cur.fetchone()
    if row:
        return row["id"]
    cur.execute(
        "INSERT INTO brands (name, owner_user_id) VALUES (%s, %s) RETURNING id",
        (name, owner_user_id),
    )
    return cur.fetchone()["id"]


@pytest.fixture(scope="module")
def world() -> dict:
    """两个账号 + 两个品牌 + 受害方的一条真 session(带一个绝不该泄露的 token)。"""
    from api.marketing_confirm_api import _ensure_table
    _ensure_table()

    conn = _conn()
    with conn.cursor() as cur:
        attacker_id = _ensure_user(cur, ATTACKER_USER)
        victim_id = _ensure_user(cur, VICTIM_USER)
        attacker_brand = _ensure_brand(cur, "攻击者自己的品牌", attacker_id)
        victim_brand = _ensure_brand(cur, "受害客户品牌", victim_id)

        # 受害方一条 pending session —— 就是生产里被读走的那种行
        cur.execute("DELETE FROM marketing_confirm_sessions WHERE brand_id IN (%s, %s)",
                    (victim_brand, attacker_brand))
        cur.execute(
            "INSERT INTO marketing_confirm_sessions "
            "(token, brand_id, status, materials_snapshot, expires_at) "
            "VALUES (%s, %s, 'pending', %s, NOW() + INTERVAL '7 days')",
            (VICTIM_TOKEN, victim_brand, '{"brand": {"name": "受害客户品牌"}}'),
        )
        # 攻击者自己也有一条 —— 反向对照要用(证明 200 路径是通的)
        cur.execute(
            "INSERT INTO marketing_confirm_sessions "
            "(token, brand_id, status, materials_snapshot, expires_at) "
            "VALUES (%s, %s, 'pending', %s, NOW() + INTERVAL '7 days')",
            ("attacker-own-token", attacker_brand, '{"brand": {"name": "攻击者自己的品牌"}}'),
        )
    conn.close()

    return {
        "attacker_id": attacker_id,
        "victim_id": victim_id,
        "attacker_brand": attacker_brand,
        "victim_brand": victim_brand,
        "attacker_headers": {"Authorization": f"Bearer {create_jwt(attacker_id)}"},
        "victim_headers": {"Authorization": f"Bearer {create_jwt(victim_id)}"},
    }


@pytest.fixture(scope="module")
def client():
    return TestClient(server.app, raise_server_exceptions=False)


def _session_row(brand_id: int) -> dict | None:
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT token, status, materials_snapshot, customer_notes, expires_at, updated_at "
            "FROM marketing_confirm_sessions WHERE brand_id = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (brand_id,),
        )
        row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def _session_count(brand_id: int) -> int:
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM marketing_confirm_sessions WHERE brand_id = %s",
                    (brand_id,))
        n = cur.fetchone()["n"]
    conn.close()
    return int(n)


# ============================================================================
# 锁1 / 锁2:status —— 正反成对
# ============================================================================

def test_cross_tenant_status_is_404_and_leaks_no_token(client, world):
    """锁1 —— 事故现场那一发。"""
    r = client.get(
        f"/api/marketing-confirm/status/{world['victim_brand']}",
        headers=world["attacker_headers"],
    )
    assert r.status_code == 404, r.text
    # 404 的 body 里也绝不许出现那个 token(防"状态码对了但 body 还是漏")
    assert VICTIM_TOKEN not in r.text, r.text


def test_own_brand_status_is_200(client, world):
    """锁2 反向对照 —— 没有这条,锁1 可能只是"恒拒",零判别力。"""
    r = client.get(
        f"/api/marketing-confirm/status/{world['attacker_brand']}",
        headers=world["attacker_headers"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_session"] is True, body
    assert body["token"] == "attacker-own-token", body


def test_victim_can_read_own_brand(client, world):
    """再一条反向对照:受害方读自己的品牌照常 200(修复没有误伤正主)。"""
    r = client.get(
        f"/api/marketing-confirm/status/{world['victim_brand']}",
        headers=world["victim_headers"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["token"] == VICTIM_TOKEN


def test_anonymous_status_is_401(client, world):
    """锁3:匿名不可达。

    工单 §4 要核的「是否连登录都不要求」—— 答案是要求(module_mapping 的
    `/api/marketing` 前缀把它映射成 None = 仅需认证,不是公开)。这条把结论钉住。
    """
    r = client.get(f"/api/marketing-confirm/status/{world['victim_brand']}")
    assert r.status_code == 401, r.text
    assert VICTIM_TOKEN not in r.text


# ============================================================================
# 锁4 / 锁5:写端点 —— 404 且**库里没动**
# ============================================================================

def test_cross_tenant_resend_is_404_and_does_not_touch_the_row(client, world):
    """锁4:resend 是**写**操作(重置 status/notes/patch/snapshot/expires)。

    只断 404 是不够的 —— 一个"先写后判"的实现照样能返 404 而数据已经被改。
    """
    before = _session_row(world["victim_brand"])
    r = client.post(
        f"/api/marketing-confirm/resend/{world['victim_brand']}",
        headers=world["attacker_headers"],
    )
    assert r.status_code == 404, r.text
    after = _session_row(world["victim_brand"])
    assert after == before, f"越权 resend 改动了受害方的行\nbefore={before}\nafter={after}"


def test_cross_tenant_generate_link_is_404_and_creates_no_row(client, world):
    """锁5:generate-link 的 brand_id 在 **POST body** —— 中间件安全网明确不读 body,
    所以这一条只能靠端点级闸拦住。它同时会 UPDATE 旧 session 为 expired,越权后果实。
    """
    before = _session_count(world["victim_brand"])
    before_row = _session_row(world["victim_brand"])
    r = client.post(
        "/api/marketing-confirm/generate-link",
        json={"brand_id": world["victim_brand"]},
        headers=world["attacker_headers"],
    )
    assert r.status_code == 404, r.text
    assert _session_count(world["victim_brand"]) == before, "越权 generate-link 落了新行"
    assert _session_row(world["victim_brand"]) == before_row, "越权 generate-link 改了旧行"


def test_own_brand_write_endpoints_still_work(client, world):
    """锁6 反向对照:自己名下的写端点必须真的成功。

    没有这条,上面两条可能只是"两个端点被我改成恒拒" —— 那是把功能修没了,不是修好了。
    """
    r = client.post(
        f"/api/marketing-confirm/resend/{world['attacker_brand']}",
        headers=world["attacker_headers"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True


# ============================================================================
# 锁7:接线 —— 三个端点都真的接了同一把闸
# ============================================================================

def test_all_three_sales_endpoints_are_wired_to_the_same_gate():
    """接线锁打在**接线**上:三个 handler 的函数体里都必须出现对闸的调用。

    只断"闸函数存在"是没用的(它可以一个调用方都没有 —— 本仓已有六例「接线没接」)。
    """
    import ast

    source = (ROOT / "api" / "marketing_confirm_api.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    handlers = {
        "generate_confirm_link": False,
        "resend_confirm": False,
        "get_confirm_status": False,
    }
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in handlers:
            for call in ast.walk(node):
                if isinstance(call, ast.Call):
                    name = getattr(call.func, "id", getattr(call.func, "attr", ""))
                    if name == "_require_brand_owner":
                        handlers[node.name] = True
    missing = [k for k, v in handlers.items() if not v]
    assert not missing, f"这些 handler 没接归属闸:{missing}"


def test_the_gate_really_delegates_to_require_brand_access():
    """闸本身必须走系统既有的 `require_brand_access`,不许另写一套判定。"""
    import ast

    source = (ROOT / "api" / "marketing_confirm_api.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    gate = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_require_brand_owner"
    )
    calls = {
        getattr(c.func, "id", getattr(c.func, "attr", ""))
        for c in ast.walk(gate) if isinstance(c, ast.Call)
    }
    assert "require_brand_access" in calls, calls


# ============================================================================
# 锁8:公开门户不许被改坏
# ============================================================================

def test_tokens_are_masked_in_logs(caplog, client, world):
    """修法 2b 的**真**落点:日志里不许出现明文 confirm token。

    这些 token 是公开门户的通行凭据 —— 日志落文件、进聚合器、被运维多方看到,
    等于一条不需要越权就能拿到有效 token 的旁路。
    成对:打码后的形态必须仍能用于排障(前 2 位 + 后 2 位可对账)。
    """
    import logging

    with caplog.at_level(logging.INFO, logger="GEO-MarketingConfirm"):
        r = client.post(
            f"/api/marketing-confirm/resend/{world['attacker_brand']}",
            headers=world["attacker_headers"],
        )
    assert r.status_code == 200, r.text
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "attacker-own-token" not in text, f"日志里出现了明文 token:\n{text}"
    # 反向对照:确实打了这条日志(否则"没出现明文"只是因为压根没记日志 = 零判别力)
    assert "重新发送确认链接" in text, text
    assert "at***en" in text, text


def test_public_material_portal_stays_public(client, world):
    """`/api/m/{token}` 是 token-only 的公开设计(工单 §3:堵上游,不动门户)。

    修完之后它必须**仍然**匿名可达 —— 否则就是把客户确认流程修没了。
    """
    r = client.get(f"/api/m/{VICTIM_TOKEN}")
    assert r.status_code == 200, r.text
    # 反向对照:不存在的 token 不能也返 200(否则上面这条零判别力)
    r2 = client.get("/api/m/definitely-not-a-real-token-xyz")
    assert r2.status_code != 200, r2.text
