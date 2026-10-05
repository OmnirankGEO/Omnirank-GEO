"""[资金边界]员工席位的钱面规则。

三条业务真相,逐条锁死:

1. **员工代老板报价,必须用老板的加价率。**
   「报价永远用本人」(2026-06-08 老板拍板)针对的是**邀请人 → 被邀请人**这条分销链:
   下游是独立经营者。**组织员工不是下游经营者,是同一家公司的手。**
   若按"本人"解析,员工 agent_level 恒 0、无自设系数 → 报价静默回落平台底价,
   老板配的加价被丢掉,员工发出去的每一单都不赚钱。

2. **员工不能看老板的进货成本和毛利。**
   `pricing.cost_and_margin_view` 属 OWNER_ONLY,角色配不上去。但选词会话详情走
   公开前缀 `/api/s/`,组织守卫不执行 —— 员工凭 created_by 就能拿未脱敏视图。

3. **钱面路由绝不能进员工可达清单。**
   所有资金保护目前都靠"不在路由契约里"这一件事。任何人将来加一条 `_p(...)`
   就会静默把资金路由变成员工可达。这里立一道回归护栏。
"""
import inspect
import re

import pytest


# ---------------------------------------------------------------- 1. 报价用老板的系数

def test_employee_quote_uses_owner_pricing(monkeypatch):
    import services.quote_pricing_preferences as qp

    class _Id:
        is_member = True
        membership_status = "active"
        organization_status = "active"
        principal_user_id = 900

    import db.organization_db as odb
    monkeypatch.setattr(odb, "resolve_identity", lambda *a, **k: _Id())
    assert qp.resolve_quote_pricing_user_id(151) == 900, "员工报价没用老板的系数 → 每单掉回平台底价"


def test_owner_and_ordinary_users_are_unchanged(monkeypatch):
    """老板本人、普通服务商、被邀请客户 —— 一律维持「本人」语义。"""
    import services.quote_pricing_preferences as qp
    import db.organization_db as odb

    class _Owner:
        is_member = False
        membership_status = "active"
        organization_status = "active"
        principal_user_id = 900

    monkeypatch.setattr(odb, "resolve_identity", lambda *a, **k: _Owner())
    assert qp.resolve_quote_pricing_user_id(900) == 900

    monkeypatch.setattr(odb, "resolve_identity", lambda *a, **k: None)
    assert qp.resolve_quote_pricing_user_id(151) == 151


@pytest.mark.parametrize("field,value", [
    ("membership_status", "suspended"),
    ("organization_status", "suspended"),
])
def test_inactive_seat_falls_back_to_self(monkeypatch, field, value):
    import services.quote_pricing_preferences as qp
    import db.organization_db as odb

    class _Id:
        is_member = True
        membership_status = "active"
        organization_status = "active"
        principal_user_id = 900

    identity = _Id()
    setattr(identity, field, value)
    monkeypatch.setattr(odb, "resolve_identity", lambda *a, **k: identity)
    assert qp.resolve_quote_pricing_user_id(151) == 151


def test_resolution_failure_falls_back_to_self(monkeypatch):
    """组织体系异常绝不能把定价权错给别人。"""
    import services.quote_pricing_preferences as qp
    import db.organization_db as odb

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(odb, "resolve_identity", boom)
    assert qp.resolve_quote_pricing_user_id(151) == 151


def test_distribution_chain_semantics_untouched():
    """分销链的「不继承上级」必须原样保留 —— 本次只加组织席位这一个例外。"""
    import services.quote_pricing_preferences as qp

    # 看**编译后实际引用的名字**,不受注释/文档串干扰
    referenced = set(qp.resolve_quote_pricing_user_id.__code__.co_names)
    assert "resolve_owning_agent" not in referenced, "报价中心不得继承分销上级"
    assert "_organization_principal_for" in referenced


# ---------------------------------------------------------------- 2. 成本毛利不给员工看

def test_cost_margin_view_is_denied_to_seat_members():
    """读源码而非 import —— selection_api 在导入期建表,不该把判别绑到库 schema 上。"""
    from pathlib import Path

    src = (Path(__file__).parent.parent / "api" / "selection_api.py").read_text(encoding="utf-8")
    body = src[src.index("def _is_owning_agent_for_session"):]
    body = body[: body.index("\ndef ", 10)]
    assert "is_organization_seat_member" in body, "员工可拿未脱敏视图 = 看到老板进货成本"
    # 必须挡在 created_by 判定**之前**
    assert body.index("is_organization_seat_member") < body.index('session.get("created_by")'), \
        "顺序错了:员工会先被 created_by 放行"


def test_seat_detection_fails_closed_toward_masking():
    """解析失败时必须按"是员工"处理(脱敏),不能反过来放行。"""
    from auth import principal_identity as pi

    src = inspect.getsource(pi.is_organization_seat_member)
    tail = src[src.index("except Exception"):]
    assert "return True" in tail, "异常时返回 False 会放行员工看成本"


def test_non_org_user_is_not_treated_as_seat(monkeypatch):
    """普通服务商本人不属于任何组织 —— 不能被误判成员工而看不到自己的成本。"""
    from auth import principal_identity as pi
    import db.organization_db as odb

    monkeypatch.setattr(odb, "resolve_identity", lambda *a, **k: None)
    assert pi.is_organization_seat_member(151) is False


def test_seat_member_is_detected(monkeypatch):
    """反证:上面的 False 不是因为函数恒返回 False。"""
    from auth import principal_identity as pi
    import db.organization_db as odb

    class _Id:
        is_member = True

    monkeypatch.setattr(odb, "resolve_identity", lambda *a, **k: _Id())
    assert pi.is_organization_seat_member(151) is True


# ---------------------------------------------------------------- 3. 钱面路由护栏

_MONEY_PATH = re.compile(
    r"^/api/(agent|wallet|pricing|service-fee|dealer-resale|operation-packages|partner|withdrawal)"
)


def test_no_money_route_is_reachable_by_employees():
    """资金面路由一旦进入员工可达清单,老板的钱和毛利就暴露了。

    唯一豁免:`GET /api/referral/whitelabel`(只读对外品牌,无任何金额字段)。
    """
    from services.organization_route_contract import MEMBER_GEO_ROUTE_POLICIES

    offenders = [
        f"{p.method} {p.path_template}"
        for p in MEMBER_GEO_ROUTE_POLICIES
        if _MONEY_PATH.match(p.path_template)
    ]
    assert not offenders, f"资金面路由被放进员工可达清单: {offenders}"


def test_referral_namespace_only_exposes_readonly_whitelabel():
    from services.organization_route_contract import MEMBER_GEO_ROUTE_POLICIES

    referral = [(p.method, p.path_template) for p in MEMBER_GEO_ROUTE_POLICIES
                if p.path_template.startswith("/api/referral")]
    assert referral == [("GET", "/api/referral/whitelabel")], \
        f"referral 面只应放只读白标,实际: {referral}"


def test_money_namespaces_absent_from_guard_allowlist():
    """守卫的命名空间白名单里不得出现资金域。"""
    from middleware.organization_guard import MEMBER_ALLOWED_NAMESPACES

    for namespace in MEMBER_ALLOWED_NAMESPACES:
        assert not _MONEY_PATH.match(namespace), f"资金命名空间被整体放行: {namespace}"
