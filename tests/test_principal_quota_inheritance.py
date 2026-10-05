"""[F-1]额度/归属按商业主体(principal)判定 的判别。

Owner 点名的两条:
- owner 是服务商时,员工建第 N 个客户**成功**,且品牌**归属 owner**;
- owner 是普通用户时,**仍然**受 1 个品牌限制(继承的是能力,不是凭空获得)。

外加本轮自查发现的前置阻断与归属陷阱:
- POST /api/my-clients 此前未被组织路由契约分类 → 员工在守卫层就 403,改额度也走不到;
- 员工建的品牌若写 user_clients,等于在组织授权体系外开影子通道(席位开户时刚清过它)。
"""
import inspect
import types

import pytest

from auth import principal_identity as pi


class _Identity:
    def __init__(self, *, is_member=True, principal_user_id=900):
        self.is_member = is_member
        self.principal_user_id = principal_user_id
        self.organization_id = 3
        self.membership_id = 11


def _request(identity=None):
    return types.SimpleNamespace(state=types.SimpleNamespace(organization_identity=identity))


def _patch_wallet(monkeypatch, levels: dict):
    import db.wallet_db as wallet_db

    monkeypatch.setattr(
        wallet_db, "get_wallet_balance", lambda uid: {"agent_level": levels.get(int(uid), 0)}
    )


# ---------------------------------------------------------------- 额度继承

def test_employee_inherits_provider_owner_level(monkeypatch):
    """owner 是服务商(L2)→ 员工判定拿到 2,额度不再被 402 拦。"""
    _patch_wallet(monkeypatch, {900: 2, 151: 0})
    level = pi.effective_agent_level(_request(_Identity()), fallback_user_id=151)
    assert level == 2


def test_employee_of_plain_owner_stays_limited(monkeypatch):
    """owner 只是普通用户 → 员工仍是 0,1 个品牌的限制照旧生效。"""
    _patch_wallet(monkeypatch, {900: 0, 151: 0})
    level = pi.effective_agent_level(_request(_Identity()), fallback_user_id=151)
    assert level == 0


def test_non_member_behaviour_is_unchanged(monkeypatch):
    """非组织用户走原路径:看自己的等级。"""
    _patch_wallet(monkeypatch, {151: 1})
    assert pi.effective_agent_level(_request(None), fallback_user_id=151) == 1


def test_wallet_failure_is_fail_closed(monkeypatch):
    """取不到钱包时按 0 处理 —— 宁可拦住,不可错放。"""
    import db.wallet_db as wallet_db

    def boom(_uid):
        raise RuntimeError("wallet down")

    monkeypatch.setattr(wallet_db, "get_wallet_balance", boom)
    assert pi.effective_agent_level(_request(_Identity()), fallback_user_id=151) == 0


def test_operating_for_agent_is_false_for_owner_himself(monkeypatch):
    """owner 本人是服务商,但他不是"代"谁作业 —— 这个标记必须为假。"""
    _patch_wallet(monkeypatch, {151: 2})
    assert pi.operating_for_agent(_request(None), fallback_user_id=151) is False


def test_operating_for_agent_true_only_for_seat_of_provider(monkeypatch):
    _patch_wallet(monkeypatch, {900: 1, 151: 0})
    assert pi.operating_for_agent(_request(_Identity()), fallback_user_id=151) is True
    _patch_wallet(monkeypatch, {900: 0, 151: 0})
    assert pi.operating_for_agent(_request(_Identity()), fallback_user_id=151) is False


# ---------------------------------------------------------------- 归属与前置阻断

def test_add_client_attributes_brand_to_principal_not_operator():
    """品牌必须落在 owner 名下,否则会出现"挂员工名下、owner 看不见"的孤儿品牌。"""
    import api.brand_api as brand_api

    src = inspect.getsource(brand_api.add_client)
    assert "principal_user_id" in src
    # 建表插入的 owner_user_id 必须是 principal
    assert "req.industry, req.city, principal_user_id, _is_test" in src
    # 🔴 [WO_222-c0' 2026-09-15] 原第三句 `assert '""", (principal_user_id,))' in src`
    #   已退役。它钉的是**额度 COUNT 查询**那一行的文本,而 L0 额度本身已按
    #   Owner 直令整体撤销(普通账号权限 = 服务商,除经营后台)——
    #   被钉的那个东西不在了,判据就没有指称对象了
    #   (本仓 `criterion-must-have-a-referent-in-the-artifact`)。
    #
    #   🔴 它当时红的方式很误导:前两句(INSERT 用 principal_user_id)照旧为真,
    #   **归属没坏**,红的只是「那段被删掉的文本不见了」——
    #   读起来却像「撤额度把归属也带坏了」。判据钉住缺陷的形状时,
    #   它会**与正确的修法为敌**(`criterion-pinning-the-defect-fights-the-fix`)。
    #
    #   本条的真意图是「归属按 principal」:上面两句是文本腿,
    #   行为腿见 tests/l0_client_quota_lifted_2026_09_15/
    #   ::test_employee_path_still_attributes_to_the_principal
    #   —— 它建一个真品牌、回库读 owner_user_id,比文本锚硬。


def test_employee_created_brand_goes_through_org_assignment_not_legacy_table():
    """员工路径不得写 user_clients(席位开户时刚清过它 = 组织体系外的影子通道)。"""
    import api.brand_api as brand_api

    src = inspect.getsource(brand_api.add_client)
    assert "organization_brand_assignments" in src
    member_branch = src.split("if organization_member:")[1].split("else:")[0]
    # 只看真正的代码行 —— 注释里解释"为什么不写 user_clients"是允许的
    code_lines = [
        line for line in member_branch.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not [line for line in code_lines if "user_clients" in line]


def test_post_my_clients_is_classified_so_the_guard_lets_employees_through():
    """未分类路由在组织守卫层就是 403 —— 额度修了也走不到。"""
    from services.organization_route_contract import match_member_geo_route

    policy = match_member_geo_route("POST", "/api/my-clients")
    assert policy is not None, "POST /api/my-clients 未分类 → 员工必然 403"
    assert policy.capability == "clients.profile_edit"


def test_delete_client_stays_unclassified_least_privilege():
    """归档客户没有放开 —— 最小授权:员工能录不能删。"""
    from services.organization_route_contract import match_member_geo_route

    assert match_member_geo_route("DELETE", "/api/my-clients/12") is None


def test_sales_can_create_clients_delivery_cannot():
    """分工:销售录客户,交付不录 —— 由既有能力自然成立,无需给存量角色补授权。"""
    from services.organization_contract import (
        DELIVERY_ROLE_CAPABILITIES,
        SALES_ROLE_CAPABILITIES,
    )

    assert "clients.profile_edit" in SALES_ROLE_CAPABILITIES
    assert "clients.profile_edit" not in DELIVERY_ROLE_CAPABILITIES


# ---------------------------------------------------------------- 钱面不得放行

def test_helper_docstring_names_the_forbidden_surfaces():
    """提现/结算/佣金/进货价系数 必须在模块里被显式标为不适用。"""
    doc = pi.__doc__ or ""
    for surface in ("提现", "佣金", "进货价", "admin"):
        assert surface in doc


def test_withdrawal_gate_is_not_switched_to_principal():
    """资金出账口必须仍看操作者本人 —— 代作业不构成提现理由。"""
    from pathlib import Path

    src = (Path(__file__).parent.parent / "api" / "withdrawal_api.py").read_text(encoding="utf-8")
    assert "principal_identity" not in src
    assert "effective_agent_level" not in src


def test_workbench_seat_context_does_not_forge_agent_level():
    """经营后台放行员工时,主体换成 principal,但绝不写回员工的 agent_level。"""
    import api.agent_workbench_api as awb

    src = inspect.getsource(awb._require_agent)
    assert '"operating_context": "organization_seat"' in src
    assert "UPDATE user_wallets" not in src
    # 所属组织不是服务商时,员工同样进不去
    assert "所属服务商账号尚未开通经营后台" in src
