"""报价经营包 DELETE 被全局 RBAC(默认 delete 级)误拦修复 · 单测(2026-06-17)。

根因:auth/module_mapping.py get_required_level 对 DELETE 默认要 "delete" 级权限,
服务商无系统级 delete → 请求在进入端点前被全局 RBAC 拦成 403(端点内已有 owner 归属校验)。
修法:DELETE_WRITE_OVERRIDES 加精确前缀 "/api/operation-packages/" → DELETE 只需 write。
不放开 public · 不把路径设 None · 不绕过端点鉴权。

跑法:ALLOW_NONTEST_DB=1 TEST_DATABASE_URL=占位 PYTHONIOENCODING=utf-8 \
       python -m pytest tests/test_operation_packages_delete_rbac_2026_06_17.py -q
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth.module_mapping as MM       # noqa: E402
import auth.middleware as MW           # noqa: E402
import api.operation_packages_api as API  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _State:
    user = None


class _Req:
    def __init__(self, user):
        self.state = _State()
        self.state.user = user


# ============================================================
# 1+2. module_mapping 级:DELETE 降到 write · PATCH 仍 write
# ============================================================

def test_delete_operation_package_requires_write_not_delete():
    assert MM.get_required_level("DELETE", "/api/operation-packages/123") == "write"


def test_patch_operation_package_still_write():
    assert MM.get_required_level("PATCH", "/api/operation-packages/123") == "write"


def test_override_is_exact_prefix_only():
    # 精确前缀:其他资源的 DELETE 不受影响(仍默认 delete)
    assert MM.get_required_level("DELETE", "/api/brands/9") == "delete"
    # /public 上没有 DELETE 业务,但前缀也覆盖它(无害);bare 路径不被 /{id} 前缀误命中
    assert MM.get_required_level("DELETE", "/api/operation-packages") == "delete"
    assert "/api/operation-packages/" in MM.DELETE_WRITE_OVERRIDES


# ============================================================
# 3. /public 仍 public · /api/operation-packages 未登录仍 401
# ============================================================

def test_public_still_whitelisted_and_root_still_auth():
    def _is_public(path):
        return path in MW.PUBLIC_PATHS or (
            path.startswith(MW.PUBLIC_PREFIXES) and not MW._is_portal_protected(path))

    assert _is_public("/api/operation-packages/public") is True      # 仍公开
    assert _is_public("/api/operation-packages") is False            # 根路径仍需鉴权
    assert _is_public("/api/operation-packages/123") is False        # 写端点仍需鉴权


def test_root_endpoint_401_when_anonymous():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _run(API.list_packages(_Req(None)))
    assert ei.value.status_code == 401


# ============================================================
# 4. 端点级 DELETE:未登录 401 / 普通用户 403 / 非 owner 403 / owner success
# ============================================================

def test_delete_endpoint_401_anonymous():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _run(API.delete_package_ep(1, _Req(None)))
    assert ei.value.status_code == 401


def test_delete_endpoint_403_normal_user(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(API, "_agent_level", lambda uid: 0)   # 普通用户
    with pytest.raises(HTTPException) as ei:
        _run(API.delete_package_ep(5, _Req({"user_id": 3, "is_admin": False})))
    assert ei.value.status_code == 403


def test_delete_endpoint_403_non_owner_agent(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(API, "_agent_level", lambda uid: 1)   # 服务商
    monkeypatch.setattr(API, "get_package", lambda pid: {"id": pid, "owner_user_id": 999})
    with pytest.raises(HTTPException) as ei:
        _run(API.delete_package_ep(5, _Req({"user_id": 7, "is_admin": False})))
    assert ei.value.status_code == 403


def test_delete_endpoint_success_owner_agent(monkeypatch):
    monkeypatch.setattr(API, "_agent_level", lambda uid: 1)
    monkeypatch.setattr(API, "get_package", lambda pid: {"id": pid, "owner_user_id": 7})
    monkeypatch.setattr(API, "soft_delete_package", lambda pid, owner: True)
    res = _run(API.delete_package_ep(5, _Req({"user_id": 7, "is_admin": False})))
    assert res["success"] is True
