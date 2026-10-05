# -*- coding: utf-8 -*-
"""WO_286 · 行业大类字典 GET /api/industry-taxonomy 必须对「已登录的非管理员」放行。

病灶:WO_267 加了这个只读端点,没登记进 auth/module_mapping.py 的 ROUTE_PREFIX_MAP ⇒
resolve_permission 回 "__unmapped__" ⇒ 全局中间件 fail-closed,只放管理员;
QA 用服务商 / 客户账号实测 403 {"code":"UNMAPPED_ROUTE"},管理员 200。
⇒ 服务商品牌页的行业大类下拉取不到字典。

两格:
  ① 映射:resolve_permission('/api/industry-taxonomy') 不再是 "__unmapped__",且就是 None(仅需认证,与 /api/c-end 同形)。
  ② 真请求:真 server.app(不跑 startup 钩子)+ TestClient;测试库里临时建一个**非管理员**账号签 JWT ⇒ 200 且有字典;
     匿名 ⇒ 401(登录要求没被顺手放掉)。账号用完即删。
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
PROBE_USER = 992861


def _path():
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))


def test_route_is_mapped_to_auth_only():
    _path()
    from auth.module_mapping import resolve_permission

    got = resolve_permission("/api/industry-taxonomy")
    assert got != "__unmapped__", "行业大类字典仍是未映射路由 ⇒ 非管理员 403 UNMAPPED_ROUTE"
    assert got is None, f"应只需认证(None),实为 {got!r}"


@pytest.fixture
def non_admin_user():
    dsn = os.environ.get("TEST_DATABASE_URL") or ""
    if "test" not in dsn:
        pytest.skip("需要名字里带 test 的库")
    import psycopg2

    c = psycopg2.connect(dsn)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("INSERT INTO users (id, username, password_hash, display_name, is_active) "
                    "VALUES (%s, %s, 'x', %s, 1) ON CONFLICT (id) DO NOTHING",
                    (PROBE_USER, "wo286_probe", "wo286_probe"))
    try:
        yield PROBE_USER
    finally:
        with c.cursor() as cur:
            for sql in ("DELETE FROM user_roles WHERE user_id = %s", "DELETE FROM users WHERE id = %s"):
                try:
                    cur.execute(sql, (PROBE_USER,))
                except Exception:  # noqa: BLE001 - 表形状因库而异,清理尽力而为
                    pass
        c.close()


def test_non_admin_gets_200_and_anonymous_gets_401(non_admin_user):
    _path()
    os.environ.setdefault("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    from fastapi.testclient import TestClient

    from auth.jwt_utils import create_jwt, decode_jwt
    from server import app

    token = create_jwt(non_admin_user)
    assert token, "签不出 JWT(测试账号没建成?)"
    payload = decode_jwt(token) or {}
    roles = [r.get("name") for r in payload.get("roles", []) if isinstance(r, dict)]
    assert "admin" not in roles and not payload.get("is_admin"), f"探针账号不该是管理员:{roles}"

    client = TestClient(app)  # 不用 with ⇒ 不跑 startup 钩子
    r = client.get("/api/industry-taxonomy", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, f"非管理员拿到 {r.status_code}:{r.text[:200]}"
    assert r.json(), "200 但字典为空"
    anon = client.get("/api/industry-taxonomy")
    assert anon.status_code == 401, f"匿名应 401,实为 {anon.status_code}:{anon.text[:200]}"
