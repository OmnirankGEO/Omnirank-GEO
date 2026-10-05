"""
Task 3 · route 页面卡注入 + 按角色选池 RBAC
测试 _inject_route_page_card(current_page, is_admin) 的 RBAC 安全边界。
"""

from db.kb_db import insert_chunk, clear_system_chunks, init_kb_tables
from api.xiaobang_api import _inject_route_page_card, invalidate_kb_cache, _canonical_route


def _seed():
    init_kb_tables()
    clear_system_chunks()
    insert_chunk(
        source_type="sys_page",
        source_slug="/pricing",
        source_title="报价方案",
        content="给客户生成三档报价。",
        route="/pricing",
        category="system",
        is_admin_only=False,
        token_keywords=["报价"],
        origin="manual",
    )
    insert_chunk(
        source_type="sys_page",
        source_slug="/admin/foo",
        source_title="后台Foo",
        content="管理端页面。",
        route="/admin/foo",
        category="system",
        is_admin_only=True,
        token_keywords=["后台"],
        origin="manual",
    )
    invalidate_kb_cache()


def test_agent_gets_pricing_card():
    _seed()
    card = _inject_route_page_card("/pricing", is_admin=False)
    assert card and card["route"] == "/pricing"


def test_agent_cannot_get_admin_card():  # 🔴 RBAC 旁路必测
    _seed()
    assert _inject_route_page_card("/admin/foo", is_admin=False) is None
    card_admin = _inject_route_page_card("/admin/foo", is_admin=True)
    assert card_admin and card_admin["route"] == "/admin/foo"
    clear_system_chunks()


def test_canonical_route_alias():
    """重定向别名归一（来源 App.tsx <Navigate>）。"""
    assert _canonical_route("/dashboard/today") == "/dashboard"
    assert _canonical_route("/dashboard/today/") == "/dashboard"
    assert _canonical_route("/account") == "/account/profile"
    assert _canonical_route("/m3/help") == "/help"
    assert _canonical_route("/pricing") == "/pricing"  # 非别名原样返回


def test_route_injection_resolves_alias():
    """小榜 current_page 传重定向前的 /dashboard/today，应命中 /dashboard 页面卡。"""
    init_kb_tables()
    clear_system_chunks()
    insert_chunk(
        source_type="sys_page", source_slug="/dashboard", source_title="首页工作台",
        content="首页工作台总览。", route="/dashboard", category="system",
        is_admin_only=False, token_keywords=["首页"], origin="manual", visible_to="both",
    )
    invalidate_kb_cache()
    card = _inject_route_page_card("/dashboard/today", is_admin=False, identity="agent")
    assert card and card["route"] == "/dashboard"
    clear_system_chunks()
