"""知识库身份隔离 RBAC（管理员 / 服务商 / 普通用户 / 组织员工）。

覆盖：
- kb_chunks.visible_to 列按身份路由（agent / normal_user / both）
- 共享页里 [仅代理] 字段收紧到 agent，普通用户池不可见
- _resolve_kb_identity 身份判定（admin / agent / normal_user）
- 老调用 identity=None 向后兼容（= 全非管理池）

🔴 这是 RBAC 旁路必测：普通用户绝不能通过小榜检索到代理专属内容
（佣金/出厂价/利润/结算/提现/白标/客户归属）。
"""

from types import SimpleNamespace

from db.kb_db import insert_chunk, clear_system_chunks, init_kb_tables
from api.xiaobang_api import (
    bm25_search,
    _inject_route_page_card,
    _select_pool,
    _resolve_kb_identity,
    invalidate_kb_cache,
    _ensure_cache_loaded,
)


def _seed():
    init_kb_tables()
    clear_system_chunks()
    # 共享页（both）公共内容
    insert_chunk(
        source_type="sys_page", source_slug="/shared", source_title="共享页",
        content="共享页公共说明SHARED", route="/shared", category="system",
        is_admin_only=False, token_keywords=["共享"], origin="manual", visible_to="both",
    )
    # 共享页里被收紧到 agent 的字段（字段级内容闸）
    insert_chunk(
        source_type="sys_field", source_slug="/shared#报价系数", source_title="共享页",
        content="报价系数：代理私有加价AGENTSECRET", route="/shared", category="system",
        is_admin_only=False, token_keywords=["报价系数"], origin="manual", visible_to="agent",
    )
    # 代理独有页
    insert_chunk(
        source_type="sys_page", source_slug="/agent-only", source_title="代理页",
        content="佣金提现COMMISSION", route="/agent-only", category="system",
        is_admin_only=False, token_keywords=["佣金"], origin="manual", visible_to="agent",
    )
    # 普通用户独有页
    insert_chunk(
        source_type="sys_page", source_slug="/normal-only", source_title="普通页",
        # [R3-P7 ①] 原文是「充值额度RECHARGE」。这条夹具只把字符串当标记用
        #(断言打的是 "RECHARGE"),而「充值额度」是裁定 ② 域的死池名 ——
        # 运行时术语门(db/kb_db.py 的 writer)现在会拒绝它。改标记不改语义。
        content="充值算力RECHARGE", route="/normal-only", category="system",
        is_admin_only=False, token_keywords=["充值"], origin="manual", visible_to="normal_user",
    )
    invalidate_kb_cache()


def _contents(pool):
    return " ".join(c.get("content", "") for c in pool)


def test_normal_user_pool_excludes_agent_content():  # 🔴 核心 RBAC
    _seed()
    _ensure_cache_loaded()
    txt = _contents(_select_pool(is_admin=False, identity="normal_user"))
    assert "AGENTSECRET" not in txt   # 共享页 [仅代理] 字段不可见
    assert "COMMISSION" not in txt    # 代理独有页不可见
    assert "RECHARGE" in txt          # 普通用户独有页可见
    assert "SHARED" in txt            # 共享页公共内容可见
    clear_system_chunks()


def test_agent_pool_scope():
    _seed()
    _ensure_cache_loaded()
    txt = _contents(_select_pool(is_admin=False, identity="agent"))
    assert "AGENTSECRET" in txt
    assert "COMMISSION" in txt
    assert "SHARED" in txt
    assert "RECHARGE" not in txt      # 普通用户独有页不进代理池
    clear_system_chunks()


def test_route_card_injection_respects_identity():
    _seed()
    # 普通用户拿不到代理独有页卡
    assert _inject_route_page_card("/agent-only", is_admin=False, identity="normal_user") is None
    assert _inject_route_page_card("/agent-only", is_admin=False, identity="agent") is not None
    # 代理也拿不到普通用户独有页卡（visible_to=normal_user）
    assert _inject_route_page_card("/normal-only", is_admin=False, identity="agent") is None
    assert _inject_route_page_card("/normal-only", is_admin=False, identity="normal_user") is not None
    # admin 全可见
    assert _inject_route_page_card("/agent-only", is_admin=True) is not None
    clear_system_chunks()


def test_resolve_identity():
    assert _resolve_kb_identity({"is_admin": True}) == (True, "admin")
    assert _resolve_kb_identity({"is_admin": False, "agent_level": 2}) == (False, "agent")
    assert _resolve_kb_identity({"is_admin": False, "agent_level": 0}) == (False, "normal_user")
    # agent_level 缺失 + 无 user_id → 查库降级 0 → normal_user（fail-safe）
    assert _resolve_kb_identity({"is_admin": False}) == (False, "normal_user")


def test_member_agent_level_zero_uses_capability_filtered_agent_knowledge():
    init_kb_tables()
    clear_system_chunks()
    insert_chunk(
        source_type="sys_page", source_slug="/pricing", source_title="员工报价帮助",
        content="报价操作说明MEMBERQUOTE", route="/pricing", category="system",
        is_admin_only=False, token_keywords=["报价"], origin="manual", visible_to="agent",
    )
    insert_chunk(
        source_type="sys_page", source_slug="/publish", source_title="员工发布帮助",
        content="发布操作说明MEMBERPUBLISH", route="/publish", category="system",
        is_admin_only=False, token_keywords=["发布"], origin="manual", visible_to="agent",
    )
    invalidate_kb_cache()
    member = SimpleNamespace(
        is_member=True,
        is_owner=False,
        capabilities=frozenset({"clients.read_assigned", "quote.read_own"}),
    )
    user = {"is_admin": False, "agent_level": 0, "user_id": 71}

    assert _resolve_kb_identity(user, member) == (False, "agent")
    quote = bm25_search(
        "报价", is_admin=False, identity="agent", user=user,
        organization_identity=member,
    )
    publish = bm25_search(
        "发布", is_admin=False, identity="agent", user=user,
        organization_identity=member,
    )
    assert quote and "MEMBERQUOTE" in quote[0][0]["content"]
    assert publish == []
    assert _inject_route_page_card(
        "/pricing", False, "agent", user=user, organization_identity=member,
    ) is not None
    assert _inject_route_page_card(
        "/publish", False, "agent", user=user, organization_identity=member,
    ) is None
    clear_system_chunks()


def test_legacy_identity_none_backward_compatible():
    _seed()
    _ensure_cache_loaded()
    txt = _contents(_select_pool(is_admin=False, identity=None))
    # 老调用 identity=None = 全非管理池，看到所有非 admin 内容（不破坏既有行为）
    assert "AGENTSECRET" in txt and "COMMISSION" in txt and "RECHARGE" in txt
    clear_system_chunks()
