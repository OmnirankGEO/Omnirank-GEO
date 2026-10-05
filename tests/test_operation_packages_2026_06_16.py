"""报价经营包 · 身份脱敏 + 持久化 + 客户公开 token 单测(2026-06-16 切片 → 2026-06-17 持久化批)。

核心:
  - allowlist 投影 + 客户面 0 内部字段泄露 + role 派发 + 401(纯函数 / 轻量 async)。
  - 持久化:首访 seed-copy · 账号隔离(owner_user_id 钳每条 CRUD)· 编辑白名单 · 软删。
  - 客户公开 token:有效→200 / 无效→404 / 过期→安全态 / 测试账号→404。
  - 管理端鉴权:未登录 401 / 非服务商 403 / 非己 403 / 不存在 404。
不连真 DB:API 测 monkeypatch DB 层;DB 层测用 fake cursor 捕获 SQL 断言隔离/白名单/seed。
跑法:ALLOW_NONTEST_DB=1 TEST_DATABASE_URL=占位 PYTHONIOENCODING=utf-8 \
       python -m pytest tests/test_operation_packages_2026_06_16.py -q
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.operation_packages as OP        # noqa: E402
import api.operation_packages_api as API      # noqa: E402
import db.operation_packages_db as DB         # noqa: E402

# 客户面绝不可见的内部字段子串(出现即泄露)
_LEAK_MARKERS = ("base_cost", "base_margin", "base_suggested", "sub_cost", "sub_margin",
                 "sub_suggested", "agent_space", "cost_multiplier", "markup", "platform_floor",
                 "credit_inventory")


def _has_leak(d: dict) -> list:
    return [k for k in d if any(m in k for m in _LEAK_MARKERS)]


def _run(coro):
    return asyncio.run(coro)


def _fake_row(id=1, owner=7, name="单项试跑包", enabled=True, sort_order=0,
              template_key="single_trial", cpmin=None, cpmax=None):
    return {"id": id, "owner_user_id": owner, "template_key": template_key, "name": name,
            "scope": "1 条本地长尾搜索项", "search_item_min": 1, "search_item_max": 1,
            "fit_scene": "单点试水", "customer_copy": "试跑一轮",
            "base_cost_min": 180, "base_cost_max": 300,
            "base_suggested_price_min": 599, "base_suggested_price_max": 799,
            "base_margin_min": 299, "base_margin_max": 619,
            "sub_cost_min": 300, "sub_cost_max": 399,
            "sub_suggested_price_min": 599, "sub_suggested_price_max": 799,
            "sub_margin_min": 200, "sub_margin_max": 500,
            "customer_price_min": cpmin, "customer_price_max": cpmax,
            "enabled": enabled, "sort_order": sort_order, "status": "active"}


def _fake_token(owner=7, token="TK", is_test=0):
    return {"id": 1, "owner_user_id": owner, "token": token, "is_active": 1,
            "is_test": is_test, "expires_at": None}


class _State:
    user = None


class _Req:
    def __init__(self, user):
        self.state = _State()
        self.state.user = user


# ============================================================
# 1. 默认包数据完整 + §3 毛利角点自洽 + 命名锁定
# ============================================================

def test_default_packages_shape():
    assert len(OP.DEFAULT_PACKAGES) == 4
    keys = {p["key"] for p in OP.DEFAULT_PACKAGES}
    assert keys == {"single_trial", "three_validate", "first_month", "multi_operate"}


def test_default_package_names_locked():
    # 命名拍板:沿用设计文档 §6 + 已上线切片(客户可见文案 · 不新增第二套)
    names = [p["name"] for p in OP.DEFAULT_PACKAGES]
    assert names == ["单项试跑包", "三项验证包", "首月启动包", "多项经营包"]


def test_base_margin_corner_consistency():
    for p in OP.DEFAULT_PACKAGES:
        assert p["base_margin_min"] == p["base_suggested_price_min"] - p["base_cost_max"]
        assert p["base_margin_max"] == p["base_suggested_price_max"] - p["base_cost_min"]


# ============================================================
# 2. 客户投影:0 内部字段泄露(核心安全)
# ============================================================

def test_customer_projection_no_internal_leak():
    for p in OP.DEFAULT_PACKAGES:
        c = OP.project_for_customer(p)
        assert _has_leak(c) == [], f"客户面泄露内部字段: {_has_leak(c)}"
        assert set(c) <= {"key", "name", "scope", "search_item_min", "search_item_max",
                          "fit_scene", "customer_copy", "search_item_count",
                          "customer_price_min", "customer_price_max"}
        assert c["name"] and c["customer_copy"] and "customer_price_min" in c


def test_customer_price_consistency_default_baseline():
    for p in OP.DEFAULT_PACKAGES:
        c = OP.project_for_customer(p)
        assert c["customer_price_min"] == p["base_suggested_price_min"]
        assert c["customer_price_max"] == p["base_suggested_price_max"]
    p0 = OP.DEFAULT_PACKAGES[0]
    c = OP.project_for_customer(p0, 888, 1288)
    assert c["customer_price_min"] == 888 and c["customer_price_max"] == 1288
    assert _has_leak(c) == []


def test_strip_belt_removes_forbidden():
    dirty = {"name": "x", "base_cost_min": 100, "sub_margin_max": 500, "cost_multiplier_x": 2.0}
    clean = OP.strip_internal_package_fields(dirty)
    assert clean == {"name": "x"}


# ============================================================
# 3. 普通用户 / 服务商 / admin 投影边界
# ============================================================

def test_normal_user_sees_own_cost_not_upstream():
    for p in OP.DEFAULT_PACKAGES:
        n = OP.project_for_normal_user(p)
        assert "base_cost_min" in n and "base_suggested_price_min" in n and "base_margin_min" in n
        assert "sub_cost_min" not in n and "agent_space_min" not in n
        assert not any(k.startswith("sub_") for k in n)


def test_agent_sees_sub_and_space_not_other_agents():
    for p in OP.DEFAULT_PACKAGES:
        a = OP.project_for_agent(p)
        assert "base_cost_min" in a and "sub_cost_min" in a
        assert "agent_space_min" in a and "agent_space_max" in a and "agent_space_note" in a
        assert a["agent_space_min"] == max(0, p["sub_cost_min"] - p["base_cost_max"])
        assert a["agent_space_max"] == p["sub_cost_max"] - p["base_cost_min"]
        assert 0 <= a["agent_space_min"] <= a["agent_space_max"]


def test_admin_sees_all():
    for p in OP.DEFAULT_PACKAGES:
        ad = OP.project_for_admin(p)
        assert ad == p


def test_project_packages_dispatch_and_failclosed():
    assert len(OP.project_packages("admin")) == 4
    unknown = OP.project_packages("???")
    assert all(_has_leak(c) == [] for c in unknown)


def test_project_packages_customer_prices_malformed_safe():
    for bad in ({"single_trial": 888}, {"single_trial": (1, 2, 3)},
                {"single_trial": "x"}, {"single_trial": None}):
        res = OP.project_packages("customer", bad)
        assert len(res) == 4
        assert res[0]["customer_price_min"] == OP.DEFAULT_PACKAGES[0]["base_suggested_price_min"]
        assert all(_has_leak(c) == [] for c in res)
    ok = OP.project_packages("customer", {"single_trial": (888, 1288)})
    assert ok[0]["customer_price_min"] == 888 and ok[0]["customer_price_max"] == 1288


# ============================================================
# 4. 持久化行投影(DB 行 → 身份视图)
# ============================================================

def test_persisted_agent_projection_has_manage_fields():
    a = OP.project_persisted_for_agent(_fake_row(id=5, owner=7))
    # 管理字段(可编辑/启用/排序)+ 成本口径 + 经营空间
    for f in ("id", "enabled", "sort_order", "template_key", "customer_price_min",
              "agent_space_min", "base_cost_min", "sub_cost_min", "key", "name"):
        assert f in a, f"agent 视图缺字段 {f}"
    assert a["id"] == 5 and a["enabled"] is True


def test_persisted_customer_projection_no_leak_no_id_owner():
    c = OP.project_persisted_for_customer(_fake_row(id=5, owner=7, cpmin=1280, cpmax=1680))
    assert _has_leak(c) == []
    # 客户面绝不见 id / owner / 模板键归属之外的内部
    assert "id" not in c and "owner_user_id" not in c and "status" not in c
    assert c["customer_price_min"] == 1280 and c["customer_price_max"] == 1680


def test_persisted_customer_price_falls_back_to_base():
    c = OP.project_persisted_for_customer(_fake_row(cpmin=None, cpmax=None))
    assert c["customer_price_min"] == 599 and c["customer_price_max"] == 799


def test_persisted_custom_copy_key_synth():
    # template_key=NULL(自定义副本)→ key 合成 pkg_{id}(给前端稳定 key)
    c = OP.project_persisted_for_customer(_fake_row(id=42, template_key=None))
    assert c["key"] == "pkg_42"


# ============================================================
# 5. API role 判定 + 登录态列表(monkeypatch DB · 不连真 DB)
# ============================================================

def test_role_for_admin_agent_normal_none(monkeypatch):
    assert API._role_for(_Req(None)) is None
    assert API._role_for(_Req({"is_admin": True})) == "admin"
    monkeypatch.setattr(API, "_agent_level", lambda uid: 2)
    assert API._role_for(_Req({"user_id": 9, "is_admin": False})) == "agent"
    monkeypatch.setattr(API, "_agent_level", lambda uid: 0)
    assert API._role_for(_Req({"user_id": 9, "is_admin": False})) == "normal"


def test_list_packages_401_when_anonymous():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _run(API.list_packages(_Req(None)))
    assert ei.value.status_code == 401


def test_list_agent_seeds_and_persisted_projection(monkeypatch):
    seeded = {}
    monkeypatch.setattr(API, "_agent_level", lambda uid: 1)
    monkeypatch.setattr(API, "ensure_seeded", lambda uid: seeded.setdefault("uid", uid))
    monkeypatch.setattr(API, "db_list_packages",
                        lambda uid, enabled_only=False: [_fake_row(id=1, owner=7)])
    res = _run(API.list_packages(_Req({"user_id": 7, "is_admin": False})))
    assert seeded["uid"] == 7                      # 首访 seed
    assert res["role"] == "agent" and res["persisted"] is True
    assert res["packages"][0]["id"] == 1 and "agent_space_min" in res["packages"][0]
    assert res["economics_note"]                   # 0.6 营销测算文案(服务商可见)


def test_list_normal_readonly_no_persistence(monkeypatch):
    monkeypatch.setattr(API, "_agent_level", lambda uid: 0)
    res = _run(API.list_packages(_Req({"user_id": 3, "is_admin": False})))
    assert res["role"] == "normal" and res["persisted"] is False and len(res["packages"]) == 4
    assert all("sub_cost_min" not in p and "agent_space_min" not in p for p in res["packages"])


def test_list_admin_full(monkeypatch):
    res = _run(API.list_packages(_Req({"is_admin": True})))
    assert res["role"] == "admin" and len(res["packages"]) == 4
    assert "base_cost_min" in res["packages"][0]
    assert res["economics_note"] is None           # 0.6 文案不发给 admin 客户面无关角色


# ============================================================
# 6. 管理端鉴权:401 / 403 / 404
# ============================================================

def test_management_401_anonymous():
    from fastapi import HTTPException
    coros = [
        API.update_package_ep(1, API.PackageUpdate(name="x"), _Req(None)),
        API.delete_package_ep(1, _Req(None)),
        API.copy_package_ep(1, _Req(None)),
        API.reorder_ep(API.ReorderBody(ids=[1]), _Req(None)),
        API.share_token_ep(_Req(None)),
    ]
    for c in coros:
        with pytest.raises(HTTPException) as ei:
            _run(c)
        assert ei.value.status_code == 401


def test_management_403_normal_user(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(API, "_agent_level", lambda uid: 0)   # 普通用户
    with pytest.raises(HTTPException) as ei:
        _run(API.reorder_ep(API.ReorderBody(ids=[1]), _Req({"user_id": 3, "is_admin": False})))
    assert ei.value.status_code == 403


def test_patch_404_not_found(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(API, "_agent_level", lambda uid: 1)
    monkeypatch.setattr(API, "get_package", lambda pid: None)
    with pytest.raises(HTTPException) as ei:
        _run(API.update_package_ep(99, API.PackageUpdate(name="x"),
                                   _Req({"user_id": 7, "is_admin": False})))
    assert ei.value.status_code == 404


def test_patch_403_not_owner(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(API, "_agent_level", lambda uid: 1)
    monkeypatch.setattr(API, "get_package", lambda pid: {"id": pid, "owner_user_id": 999})
    with pytest.raises(HTTPException) as ei:
        _run(API.update_package_ep(5, API.PackageUpdate(name="x"),
                                   _Req({"user_id": 7, "is_admin": False})))
    assert ei.value.status_code == 403


def test_patch_success_owner(monkeypatch):
    monkeypatch.setattr(API, "_agent_level", lambda uid: 1)
    monkeypatch.setattr(API, "get_package", lambda pid: {"id": pid, "owner_user_id": 7})
    monkeypatch.setattr(API, "update_package",
                        lambda pid, owner, fields: _fake_row(id=pid, owner=owner,
                                                             name=fields.get("name", "x")))
    res = _run(API.update_package_ep(5, API.PackageUpdate(name="新名"),
                                     _Req({"user_id": 7, "is_admin": False})))
    assert res["success"] and res["package"]["id"] == 5 and res["package"]["name"] == "新名"


def test_copy_success_owner(monkeypatch):
    monkeypatch.setattr(API, "_agent_level", lambda uid: 1)
    monkeypatch.setattr(API, "get_package", lambda pid: {"id": pid, "owner_user_id": 7})
    monkeypatch.setattr(API, "copy_package",
                        lambda pid, owner: _fake_row(id=99, owner=owner, template_key=None,
                                                     name="单项试跑包(副本)"))
    res = _run(API.copy_package_ep(5, _Req({"user_id": 7, "is_admin": False})))
    assert res["success"] and res["package"]["id"] == 99
    assert res["package"]["template_key"] is None


def test_delete_success_owner(monkeypatch):
    monkeypatch.setattr(API, "_agent_level", lambda uid: 1)
    monkeypatch.setattr(API, "get_package", lambda pid: {"id": pid, "owner_user_id": 7})
    monkeypatch.setattr(API, "soft_delete_package", lambda pid, owner: True)
    res = _run(API.delete_package_ep(5, _Req({"user_id": 7, "is_admin": False})))
    assert res["success"] is True


def test_admin_can_manage_others(monkeypatch):
    # admin 越过 owner 校验(is_admin 豁免)
    monkeypatch.setattr(API, "get_package", lambda pid: {"id": pid, "owner_user_id": 999})
    monkeypatch.setattr(API, "soft_delete_package", lambda pid, owner: True)
    res = _run(API.delete_package_ep(5, _Req({"user_id": 1, "is_admin": True})))
    assert res["success"] is True


# ============================================================
# 7. 客户公开 token 视图:200 / 404 / 过期 / 测试账号
# ============================================================

def test_public_no_token_default_customer(monkeypatch):
    res = _run(API.list_packages_public(_Req(None)))
    assert res["role"] == "customer" and len(res["packages"]) == 4
    for c in res["packages"]:
        assert _has_leak(c) == [] and "customer_price_min" in c


def test_public_valid_token_owner_packages(monkeypatch):
    monkeypatch.setattr(API, "resolve_share_token",
                        lambda t: {"owner_user_id": 7, "is_test": False, "expired": False})
    monkeypatch.setattr(API, "db_list_packages",
                        lambda uid, enabled_only=False: [_fake_row(id=1, owner=7, cpmin=1280, cpmax=1680)]
                        if enabled_only else [])
    res = _run(API.list_packages_public(_Req(None), token="goodtok"))
    assert res["role"] == "customer" and len(res["packages"]) == 1
    c = res["packages"][0]
    assert _has_leak(c) == [] and "id" not in c and "owner_user_id" not in c
    assert c["customer_price_min"] == 1280


def test_public_invalid_token_404(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(API, "resolve_share_token", lambda t: None)
    with pytest.raises(HTTPException) as ei:
        _run(API.list_packages_public(_Req(None), token="bad"))
    assert ei.value.status_code == 404


def test_public_expired_token_safe_state(monkeypatch):
    monkeypatch.setattr(API, "resolve_share_token",
                        lambda t: {"owner_user_id": 7, "is_test": False, "expired": True})
    res = _run(API.list_packages_public(_Req(None), token="exp"))
    assert res.get("expired") is True and res["packages"] == []


def test_public_test_account_token_404(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(API, "resolve_share_token",
                        lambda t: {"owner_user_id": 7, "is_test": True, "expired": False})
    with pytest.raises(HTTPException) as ei:
        _run(API.list_packages_public(_Req(None), token="testacct"))
    assert ei.value.status_code == 404


def test_share_token_success_and_test_flag(monkeypatch):
    captured = {}
    monkeypatch.setattr(API, "_agent_level", lambda uid: 1)
    monkeypatch.setattr(API, "ensure_seeded", lambda uid: None)
    monkeypatch.setattr(API, "get_or_create_share_token",
                        lambda uid, is_test=False: (captured.update(is_test=is_test)
                                                    or {"token": "TK123", "is_test": 1 if is_test else 0}))
    # 真实服务商
    res = _run(API.share_token_ep(_Req({"user_id": 7, "is_admin": False, "username": "agent_real"})))
    assert res["token"] == "TK123" and res["share_path"] == "/packages/TK123"
    assert captured["is_test"] is False and res["is_test"] is False
    # 测试账号(名字含「测试」)→ is_test=True 不外露
    _run(API.share_token_ep(_Req({"user_id": 8, "is_admin": False, "username": "测试账号A"})))
    assert captured["is_test"] is True


# ============================================================
# 8. DB 层:账号隔离 + 编辑白名单 + seed 初始化(fake cursor 捕获 SQL · 不连真 DB)
# ============================================================

class _FakeCursor:
    def __init__(self, one_results=None, all_results=None, rowcount=1):
        self.calls = []
        self._ones = list(one_results or [])
        self._all = all_results if all_results is not None else []
        self.rowcount = rowcount

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self._ones.pop(0) if self._ones else None

    def fetchall(self):
        return self._all


class _FakeConn:
    def __init__(self, cursor):
        self._cur = cursor
        self.autocommit = False

    def cursor(self):
        return self._cur

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install_fake(monkeypatch, cursor):
    conn = _FakeConn(cursor)
    monkeypatch.setattr(DB, "_get_db", lambda: conn)
    monkeypatch.setattr(DB, "_conn", lambda: conn)
    return conn


def _nospace(s):
    return s.replace(" ", "").replace("\n", "")


def test_db_list_packages_owner_isolation(monkeypatch):
    cur = _FakeCursor(all_results=[_fake_row(id=1, owner=42)])
    _install_fake(monkeypatch, cur)
    rows = DB.list_packages(42)
    sql, params = cur.calls[-1]
    assert "owner_user_id=%s" in _nospace(sql)
    assert "status='active'" in _nospace(sql)
    assert 42 in params and len(rows) == 1


def test_db_list_enabled_only(monkeypatch):
    cur = _FakeCursor(all_results=[])
    _install_fake(monkeypatch, cur)
    DB.list_packages(42, enabled_only=True)
    sql, _ = cur.calls[-1]
    assert "enabled=TRUE" in _nospace(sql)


def test_db_update_whitelist_coercion_and_isolation(monkeypatch):
    cur = _FakeCursor(one_results=[_fake_row(id=5, owner=42, name="新")])
    _install_fake(monkeypatch, cur)
    out = DB.update_package(5, 42, {"name": "新", "customer_price_min": "888.6",
                                    "base_cost_min": 99, "bogus": 1, "enabled": False})
    sql, params = cur.calls[-1]
    # 白名单:内部成本列 / 未知列不进 SET
    assert "base_cost_min" not in sql and "bogus" not in sql
    assert "name=%s" in sql and "customer_price_min=%s" in sql and "enabled=%s" in sql
    assert 889 in params                      # "888.6" → int(round)=889
    assert False in params                     # enabled=False 写入(非被 None 过滤)
    # 隔离:WHERE 钳 id + owner
    assert "owner_user_id=%s" in _nospace(sql) and 42 in params and 5 in params
    assert out["id"] == 5


def test_db_update_noop_returns_current(monkeypatch):
    cur = _FakeCursor(one_results=[_fake_row(id=5, owner=42)])
    _install_fake(monkeypatch, cur)
    out = DB.update_package(5, 42, {"bogus_only": 1})   # 无合法字段 → get_package
    assert out["id"] == 5
    assert all("UPDATE" not in c[0] for c in cur.calls)  # 不发 UPDATE


def test_db_soft_delete_isolation(monkeypatch):
    cur = _FakeCursor(rowcount=1)
    _install_fake(monkeypatch, cur)
    ok = DB.soft_delete_package(5, 42)
    sql, params = cur.calls[-1]
    assert "status='deleted'" in _nospace(sql)
    assert "owner_user_id=%s" in _nospace(sql) and 42 in params and 5 in params
    assert ok is True


def test_db_reorder_isolation(monkeypatch):
    cur = _FakeCursor(rowcount=1)
    _install_fake(monkeypatch, cur)
    n = DB.reorder_packages(42, [3, 1, 2])
    updates = [c for c in cur.calls if c[0].strip().upper().startswith("UPDATE")]
    assert len(updates) == 3 and n == 3
    for sql, params in updates:
        assert "owner_user_id=%s" in _nospace(sql) and 42 in params


def test_db_ensure_seeded_inserts_four_when_empty(monkeypatch):
    cur = _FakeCursor(one_results=[None])      # 预检:无已有行
    _install_fake(monkeypatch, cur)
    DB.ensure_seeded(42)
    inserts = [c for c in cur.calls if "INSERT INTO operation_packages" in c[0]]
    assert len(inserts) == 4
    for sql, params in inserts:
        assert "ON CONFLICT DO NOTHING" in sql
        assert params[0] == 42                 # owner_user_id 第一列(隔离)
    seeded_keys = {p[2] for _, p in inserts}   # template_key 第三列
    assert seeded_keys == {"single_trial", "three_validate", "first_month", "multi_operate"}


def test_db_ensure_seeded_noop_when_exists(monkeypatch):
    cur = _FakeCursor(one_results=[{"x": 1}])   # 预检:已有行 → 不重复 seed(账号隔离/幂等)
    _install_fake(monkeypatch, cur)
    DB.ensure_seeded(42)
    assert [c for c in cur.calls if "INSERT" in c[0]] == []


def test_db_share_token_create_when_absent(monkeypatch):
    cur = _FakeCursor(one_results=[None, _fake_token(owner=42, token="NEW")])
    _install_fake(monkeypatch, cur)
    tok = DB.get_or_create_share_token(42, is_test=False)
    assert tok["token"] == "NEW"
    ins = [c for c in cur.calls if "INSERT INTO operation_package_share_tokens" in c[0]]
    assert ins and "ON CONFLICT DO NOTHING" in ins[0][0]


def test_db_share_token_reuse_existing(monkeypatch):
    cur = _FakeCursor(one_results=[_fake_token(owner=42, token="OLD", is_test=0)])
    _install_fake(monkeypatch, cur)
    tok = DB.get_or_create_share_token(42, is_test=False)
    assert tok["token"] == "OLD"
    assert [c for c in cur.calls if "INSERT" in c[0]] == []   # 幂等:不重复建


def test_db_resolve_token_none_when_missing(monkeypatch):
    cur = _FakeCursor(one_results=[None])
    _install_fake(monkeypatch, cur)
    assert DB.resolve_share_token("bad") is None


def test_db_resolve_token_returns_owner(monkeypatch):
    cur = _FakeCursor(one_results=[{"owner_user_id": 42, "is_test": 0, "expires_at": None}])
    _install_fake(monkeypatch, cur)
    r = DB.resolve_share_token("good")
    assert r["owner_user_id"] == 42 and r["is_test"] is False and r["expired"] is False


# ============================================================
# 9. [复审修] middleware 级:/public 必须在全局 auth 白名单
#    token 走 query 参数 → 路径仍是 /public(已白名单)· 不需改 middleware。
# ============================================================

def test_public_path_in_global_auth_whitelist():
    import auth.middleware as MW

    def _is_public(path):
        return path in MW.PUBLIC_PATHS or (
            path.startswith(MW.PUBLIC_PREFIXES) and not MW._is_portal_protected(path))

    assert _is_public("/api/operation-packages/public") is True
    # token 走 query(?token=) → 路径不变 → 仍命中白名单
    assert _is_public("/api/operation-packages/public") is True
    # 登录态 + 管理端点仍需鉴权:不放开整个 prefix
    assert _is_public("/api/operation-packages") is False
    assert _is_public("/api/operation-packages/5") is False
    assert "/api/operation-packages/public" in MW.PUBLIC_PATHS
    assert "/api/operation-packages" not in MW.PUBLIC_PATHS
