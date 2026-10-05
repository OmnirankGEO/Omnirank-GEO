"""[Deploy-CTO NO-GO finding 3+4] 知识库删除 admin 闸 + 软刷新 fail-closed · 行为单测(非纯 source-inspection)。

finding 3:非 admin 调 delete_role_document / delete_knowledge(非 client kb_type) → 必须 403(在触 rag 前)。
finding 4:_failclosed_fallback 必须剥离 permissions/roles/client_brand_ids + is_admin,保留身份字段。
判别性:删 _require_admin 调用 → f3 不再 403;回退到"保留旧权限" → f4 断言失败。
"""
from __future__ import annotations
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _req(is_admin=False, user_id=5):
    return SimpleNamespace(state=SimpleNamespace(
        user={"is_admin": is_admin, "user_id": user_id, "permissions": [], "roles": []}))


# ---------------- finding 4 v3 · 软刷新失败 → 503 不 call_next ----------------
# 说明:503 返回内联在 ASGI 中间件 dispatch 里(需完整 ASGI+JWT+mock DB 才能行为级触发),
# 此处用【结构级判别】锁住关键不变式:软刷新 except 块直接 return 503 AUTH_REFRESH_UNAVAILABLE,
# 且在 return 前不 call_next、不再用 permissions=[] 的假 fail-closed。回退到 call_next/清空权限 → 断言失败。

MW_SRC = (ROOT / "auth" / "middleware.py").read_text(encoding="utf-8")


def test_f4_refresh_failure_returns_503_not_fake_failclosed():
    i = MW_SRC.find("soft-refresh 异常")
    assert i != -1, "未定位软刷新异常兜底块"
    block = MW_SRC[i:i + 1400]
    assert "status_code=503" in block, "finding4: 软刷新失败必须直接返回 503"
    assert "AUTH_REFRESH_UNAVAILABLE" in block, "finding4: 503 必须带 code=AUTH_REFRESH_UNAVAILABLE"
    # 废弃"清空 permissions"的假 fail-closed(auth-only 路由挡不住)· 匹配调用形式,非注释里的字样
    assert "_failclosed_fallback(" not in MW_SRC, "假 fail-closed helper 必须移除(不得再被调用)"


def test_f4_no_callnext_before_503_return():
    """软刷新 except 块内、503 return 之前不得【调用】call_next(request)(否则 auth-only 路由漏放行)。"""
    i = MW_SRC.find("soft-refresh 异常")
    j = MW_SRC.find("AUTH_REFRESH_UNAVAILABLE", i)
    assert i != -1 and j != -1
    between = MW_SRC[i:j]
    # 匹配实际调用 call_next(request),不匹配注释里"绝不 call_next"字样
    assert "call_next(request)" not in between, "软刷新失败到 503 之间不得 call_next(auth-only 路由必须一并挡住)"


# ---------------- finding 3 · 知识库删除 admin 闸 ----------------

@pytest.mark.asyncio
async def test_f3_delete_role_document_requires_admin():
    from api.knowledge_api import delete_role_document
    with pytest.raises(HTTPException) as ei:
        await delete_role_document("advisor", "adv1", "f.txt", _req(is_admin=False))
    assert ei.value.status_code in (401, 403), f"非 admin 删角色知识库应 403,实际 {ei.value.status_code}"


@pytest.mark.asyncio
async def test_f3_delete_knowledge_nonclient_requires_admin():
    from api.knowledge_api import delete_knowledge
    with pytest.raises(HTTPException) as ei:
        await delete_knowledge("role", "adv1", "f.txt", _req(is_admin=False), role_type="advisor")
    assert ei.value.status_code in (401, 403), f"非 admin 删非 client 知识库应 403,实际 {ei.value.status_code}"
