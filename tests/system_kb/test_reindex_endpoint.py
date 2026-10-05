"""
tests/system_kb/test_reindex_endpoint.py

Task 5：验证 POST /api/admin/xiaobang/reindex-system 端点处理函数。

直接测处理函数（不需要起 TestClient / JWT），用最小 fake request 注入
request.state.user，断言：
  - 管理员 → ok=True, inserted>=2, "/pricing" in routes
  - 非管理员 / 未登录 → HTTPException 403
"""
import types
import asyncio

import pytest
from fastapi import HTTPException

from db.kb_db import clear_system_chunks


def _fake_request(user):
    """构造最小 fake request，带 state.user。"""
    return types.SimpleNamespace(state=types.SimpleNamespace(user=user))


# ---- 管理员：正常重建 ----------------------------------------

@pytest.mark.asyncio
async def test_admin_reindex_system_ok():
    """管理员调用 → ok=True, inserted>=2, /pricing 在 routes 中。"""
    from api.xiaobang_api import admin_reindex_system

    clear_system_chunks()
    req = _fake_request({"is_admin": True, "id": 1})
    result = await admin_reindex_system(req)

    assert result["ok"] is True
    assert result["inserted"] >= 2
    assert "/pricing" in result["routes"]
    # counts 应包含 sys_page 键
    assert "sys_page" in result["counts"] or any("sys" in k for k in result["counts"])


# ---- 非管理员：403 ------------------------------------------

@pytest.mark.asyncio
async def test_non_admin_raises_403():
    """is_admin=False → HTTPException 403。"""
    from api.xiaobang_api import admin_reindex_system

    req = _fake_request({"is_admin": False, "id": 2})
    with pytest.raises(HTTPException) as exc_info:
        await admin_reindex_system(req)
    assert exc_info.value.status_code == 403


# ---- 未登录（user=None）：403 --------------------------------

@pytest.mark.asyncio
async def test_no_user_raises_403():
    """未登录（user=None）→ HTTPException 403。"""
    from api.xiaobang_api import admin_reindex_system

    req = _fake_request(None)
    with pytest.raises(HTTPException) as exc_info:
        await admin_reindex_system(req)
    assert exc_info.value.status_code == 403
