"""[单1 · WP6 集成缺口]组织能力 → 平台模块 推导的判别。

Owner 点名的五条攻击面逐条锁死:
1. 映射表越权面 —— 任何能力组合都推不出后台管理面,也推不出 delete 级;
2. 角色分工不越界 —— 销售拿不到写作写权限,交付拿不到报价权限;
3. operator 伪造 —— 非成员 / 停用席位 / 组织停用 一律推导为空(fail-closed);
4. revoke 时效 —— 改角色能力必须顶掉平台侧 users.permission_version;
5. 不伪造服务商身份 —— 推导链一个字都不碰 agent_level。
"""
import inspect

import pytest

from auth import organization_module_derivation as omd
from services.organization_contract import (
    DEFAULT_MEMBER_CAPABILITIES,
    DELEGABLE_CAPABILITIES,
    DELIVERY_ROLE_CAPABILITIES,
    OWNER_ONLY_CAPABILITIES,
    READONLY_ROLE_CAPABILITIES,
    SALES_ROLE_CAPABILITIES,
)


# ---------------------------------------------------------------- 攻击面 1:越权

def test_no_capability_combination_can_reach_admin_surface():
    """把**所有**可委派能力一起给一个角色,也不能推出后台管理面。"""
    derived = omd.derive_module_permissions(DELEGABLE_CAPABILITIES)
    modules = {grant.split(":", 1)[0] for grant in derived}
    for forbidden in ("users", "roles", "audit", "settings", "ai_agents", "social"):
        assert forbidden not in modules, f"{forbidden} 模块被推导出来了 = 越权"


def test_delete_level_is_never_granted():
    """DELETE 路由要 delete 级(auth/module_mapping.get_required_level),一律不发。"""
    derived = omd.derive_module_permissions(DELEGABLE_CAPABILITIES)
    assert not [g for g in derived if g.endswith(":delete")]


def test_owner_only_capabilities_are_not_mapped():
    """owner-only(资金/治理面)能力不得出现在映射表里。"""
    assert not (set(omd.CAPABILITY_MODULE_GRANTS) & set(OWNER_ONLY_CAPABILITIES))


def test_unknown_capability_is_ignored_not_expanded():
    """能力表可能先于映射表上线 —— 那时必须少给,而不是崩或乱给。"""
    assert omd.derive_module_permissions({"totally.unknown.capability"}) == frozenset()


def test_every_delegable_capability_must_take_an_explicit_position():
    """新增可委派能力必须显式表态(给模块 / 显式空),防止"加了却忘了决定"。

    这条由导入期自检保证;这里再断一次,让失败信息落在测试里而不是 import 期。
    """
    missing = set(DELEGABLE_CAPABILITIES) - set(omd.CAPABILITY_MODULE_GRANTS)
    assert not missing, f"未表态的新能力: {sorted(missing)}"


# ---------------------------------------------------------------- 攻击面 2:角色分工

def test_sales_and_delivery_do_not_cross_over():
    sales = omd.derive_module_permissions(SALES_ROLE_CAPABILITIES)
    delivery = omd.derive_module_permissions(DELIVERY_ROLE_CAPABILITIES)

    # 销售:能报价、能跑诊断;**不能**写作发文
    assert "quote:write" in sales
    assert "diagnosis:write" in sales
    assert "writing:write" not in sales

    # 交付:能写作;**不能**碰报价
    assert "writing:write" in delivery
    assert not [g for g in delivery if g.startswith("quote:")]


def test_readonly_role_gets_no_write_anywhere():
    readonly = omd.derive_module_permissions(READONLY_ROLE_CAPABILITIES)
    assert readonly, "只读角色至少应能打开页面"
    assert not [g for g in readonly if not g.endswith(":read")]


def test_default_member_role_is_a_subset_of_sales_plus_delivery():
    """默认成员角色不应凭空多出销售/交付都没有的权限面。"""
    default = omd.derive_module_permissions(DEFAULT_MEMBER_CAPABILITIES)
    union = omd.derive_module_permissions(
        set(SALES_ROLE_CAPABILITIES) | set(DELIVERY_ROLE_CAPABILITIES)
    )
    assert default <= union


# ---------------------------------------------------------------- 攻击面 3:伪造 / fail-closed

class _FakeIdentity:
    def __init__(self, *, is_member=True, membership_status="active",
                 organization_status="active", capabilities=()):
        self.is_member = is_member
        self.membership_status = membership_status
        self.organization_status = organization_status
        self.capabilities = frozenset(capabilities)
        self.principal_user_id = 7
        self.organization_id = 3
        self.role_id = 5


def _patch_identity(monkeypatch, identity):
    import db.organization_db as odb

    monkeypatch.setattr(odb, "resolve_identity", lambda *a, **k: identity)


def test_non_member_derives_nothing(monkeypatch):
    """组织 owner 走的是平台自己的角色体系,不能被这里放大。"""
    _patch_identity(monkeypatch, _FakeIdentity(is_member=False,
                                               capabilities=SALES_ROLE_CAPABILITIES))
    assert omd.derive_for_user(151) == frozenset()


@pytest.mark.parametrize("field,value", [
    ("membership_status", "suspended"),
    ("membership_status", "leaving"),
    ("organization_status", "suspended"),
])
def test_inactive_seat_or_org_derives_nothing(monkeypatch, field, value):
    """停用/离职中的席位、停用的组织 —— 一律不推导。"""
    identity = _FakeIdentity(capabilities=SALES_ROLE_CAPABILITIES)
    setattr(identity, field, value)
    _patch_identity(monkeypatch, identity)
    assert omd.derive_for_user(151) == frozenset()


def test_derivation_failure_falls_back_to_empty_not_to_everything(monkeypatch):
    """组织体系抛异常时必须**少给**(空集),绝不能因为异常而放行。"""
    import db.organization_db as odb

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(odb, "resolve_identity", boom)
    assert omd.derive_for_user(151) == frozenset()


def test_active_member_actually_derives(monkeypatch):
    """反证:上面几条的"空集"不是因为函数恒返回空。"""
    _patch_identity(monkeypatch, _FakeIdentity(capabilities=SALES_ROLE_CAPABILITIES))
    assert "quote:write" in omd.derive_for_user(151)


# ---------------------------------------------------------------- 攻击面 4:revoke 时效

def test_role_capability_change_rotates_platform_permission_version():
    """改角色能力必须顶掉 users.permission_version。

    只 bump 组织内部的 capability_version 管不到平台侧的 JWT / 权限缓存
    (perm_cache 60s + Redis 300s),会留下数分钟的旧权限窗口。
    """
    from services import organization_service as svc

    src = inspect.getsource(svc.update_role)
    assert "_rotate_permission_version_for_role" in src, "update_role 未使平台权限失效"

    src_override = inspect.getsource(svc.set_member_overrides)
    assert "_rotate_permission_version_for_membership" in src_override, \
        "set_member_overrides 未使平台权限失效(deny 就在这一层)"


def test_role_rotation_covers_every_seat_of_that_role():
    """按 role_id 找人,不能只顶当前活跃席位 —— 恢复后的席位不能沿用旧授权。"""
    from services import organization_service as svc

    src = inspect.getsource(svc._rotate_permission_version_for_role)
    assert "FROM organization_memberships WHERE role_id" in src
    assert "status" not in src.split("SELECT user_id")[1].split(")")[0], \
        "刻意不按 status 过滤,避免恢复席位沿用旧授权"


# ---------------------------------------------------------------- 攻击面 5:不伪造服务商

def test_derivation_never_touches_agent_level():
    """服务商身份是资金语义(提现/佣金/进货价系数),推导链一个字都不能碰。"""
    src = inspect.getsource(omd)
    # operator_context_for 里**读**了 principal 的 agent_level 用于 operating_for_agent,
    # 但绝不能出现对 agent_level 的写入/赋值给当前用户。
    assert 'user["agent_level"]' not in src
    assert "UPDATE user_wallets" not in src
    assert "SET agent_level" not in src


def test_operating_for_agent_is_separate_from_agent_level():
    """`operating_for_agent` 必须是独立字段,不得靠改写 agent_level 实现。"""
    import api.auth_api as auth_api

    src = inspect.getsource(auth_api.get_me)
    assert 'user["operating_for_agent"]' in src
    # /me 里对 agent_level 的赋值只能是那一处读钱包得来的真值
    assert src.count('user["agent_level"] = ') == 1


def test_money_surfaces_stay_owner_identity_only_in_frontend():
    """资金面(库存/进货价/结算/推广)不得因"代作业"被放行。"""
    from pathlib import Path

    root = Path(__file__).parent.parent
    app_tsx = (root / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    guard = (root / "frontend" / "src" / "components" / "auth" / "ProtectedRoute.tsx").read_text(
        encoding="utf-8"
    )

    # 4 条服务商资金面路由必须全部打上 ownerIdentityOnly
    assert app_tsx.count("requiresAgent ownerIdentityOnly") == 4
    assert "<ProtectedRoute requiresAgent>" not in app_tsx, "存在未打标的 requiresAgent 路由"
    # 门本身必须真的消费这个标记
    assert "operatingForAgent && !ownerIdentityOnly" in guard


def test_version_is_declared_and_surfaced():
    assert omd.MODULE_DERIVATION_VERSION
    src = inspect.getsource(omd.operator_context_for)
    assert "derivation_version" in src
