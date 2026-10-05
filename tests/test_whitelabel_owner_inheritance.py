"""[白标继承]团队长设置的对外品牌必须覆盖名下全部子账号。

Owner 要求:母账号(团队长)设的对外品牌,要能覆盖所有子账号(员工席位)。

判定原则:对外品牌属于**商业主体**,不属于操作者。
员工"看得到、用得上、改不了" —— 读按 principal,写是 owner 专属。

本文件锁死三件事:
1. 读白标的每个员工可达路径都按 principal 取,不按登录 uid;
2. 写白标(PUT / logo)员工一律拒,且给出 §13 合同(有下一步,不是干拒);
3. 会**冻结**进客户可见物的品牌(报价快照)必须冻团队长的 —— 冻错就永久错。
"""
import inspect
import types

import pytest

from auth import principal_identity as pi


class _Identity:
    def __init__(self, principal_user_id=900):
        self.is_member = True
        self.principal_user_id = principal_user_id


def _request(identity=None):
    return types.SimpleNamespace(state=types.SimpleNamespace(organization_identity=identity))


# ---------------------------------------------------------------- 归属判定

def test_seat_member_branding_resolves_to_owner():
    assert pi.resolve_branding_principal_user_id(_request(_Identity(900)),
                                                 fallback_user_id=151) == 900


def test_non_member_branding_is_unchanged():
    assert pi.resolve_branding_principal_user_id(_request(None), fallback_user_id=151) == 151


def test_branding_is_declared_in_scope_unlike_money_surfaces():
    """helper 的边界声明必须明确:白标在范围内,提现/佣金不在。"""
    doc = pi.__doc__ or ""
    assert "对外品牌" in doc and "白标" in doc
    assert "提现" in doc and "佣金" in doc


# ---------------------------------------------------------------- 读:按 principal

def test_whitelabel_get_reads_owner_row_not_session_row():
    import api.referral_api as ref

    src = inspect.getsource(ref.get_whitelabel)
    assert "resolve_branding_principal_user_id" in src
    assert 'WHERE user_id = %s", (_brand_owner_id,)' in src
    # 绝不能再按登录用户取品牌行
    assert 'FROM whitelabel_settings WHERE user_id = %s", (user["user_id"],)' not in src


def test_seat_member_gets_readonly_flags_not_an_uneditable_form():
    """员工拿到的是只读态标记,而不是一个改了保存不了的表单。"""
    import api.referral_api as ref

    src = inspect.getsource(ref.get_whitelabel)
    assert '"managed_by_owner"' in src
    assert '"editable"' in src


def test_poster_uses_owner_brand():
    from pathlib import Path

    src = (Path(__file__).parent.parent / "api" / "share_api.py").read_text(encoding="utf-8")
    assert "_get_whitelabel(brand_owner_user_id)" in src
    assert "_get_whitelabel(user_id)" not in src


def test_published_article_byline_uses_owner_brand():
    import api.publish_api as pub

    src = inspect.getsource(pub._resolve_agent_brand_name)
    assert "resolve_branding_principal_user_id" in src


@pytest.mark.parametrize("module_name", [
    "api.content_api", "api.xiaobang_api",  # 社媒主路径 router 随 E3 删
])
def test_backoffice_skin_uses_owner_brand(module_name):
    """员工看到的工作台皮肤也应是所属服务商的品牌,不是平台默认。"""
    import importlib

    module = importlib.import_module(module_name)
    src = inspect.getsource(module)
    assert 'owner_user_id=int(uid))' not in src, "仍按登录 uid 取品牌"
    assert "_branding_principal(request, uid)" in src


# ---------------------------------------------------------------- 写:owner 专属

@pytest.mark.parametrize("func_name", ["update_whitelabel", "upload_whitelabel_logo"])
def test_write_paths_reject_seat_members_with_contract(func_name):
    import api.referral_api as ref

    func = getattr(ref, func_name, None)
    assert func is not None, f"{func_name} 不存在,判别失效"
    src = inspect.getsource(func)
    assert "WHITELABEL_OWNER_ONLY" in src, "员工可写 → 会在自己 uid 下建出第二份品牌"
    # §13:必须给下一步,不能干拒
    for field in ("reason", "impact", "repair_hint", "actions"):
        assert f'"{field}"' in src


def test_guard_layer_blocks_writes_before_the_endpoint():
    """守卫层与端点内硬闸双层:PUT / logo 在路由契约里必须仍未分类。"""
    from services.organization_route_contract import match_member_geo_route

    assert match_member_geo_route("GET", "/api/referral/whitelabel") is not None
    assert match_member_geo_route("PUT", "/api/referral/whitelabel") is None
    assert match_member_geo_route("POST", "/api/referral/whitelabel/logo") is None


def test_read_route_uses_a_capability_existing_roles_already_hold():
    """存量角色无需补授权就能生效 —— 否则等于对现有组织"没修"。"""
    from services.organization_contract import (
        DELIVERY_ROLE_CAPABILITIES,
        READONLY_ROLE_CAPABILITIES,
        SALES_ROLE_CAPABILITIES,
    )
    from services.organization_route_contract import match_member_geo_route

    capability = match_member_geo_route("GET", "/api/referral/whitelabel").capability
    for template in (SALES_ROLE_CAPABILITIES, DELIVERY_ROLE_CAPABILITIES,
                     READONLY_ROLE_CAPABILITIES):
        assert capability in template, f"{capability} 不在某档角色模板里 → 该档员工看不到品牌"


def test_whitelabel_manage_stays_owner_only():
    from services.organization_contract import OWNER_ONLY_CAPABILITIES

    assert "whitelabel.manage" in OWNER_ONLY_CAPABILITIES


# ---------------------------------------------------------------- 冻结物:冻对主体

def test_quote_snapshot_freezes_owner_brand_and_owner_subject():
    """报价快照进客户可见的 H5/PDF 且**冻结** —— 冻错就永久错。"""
    import api.referral_api as ref

    src = inspect.getsource(ref.generate_agent_quote)
    assert "resolve_branding_principal_user_id" in src
    # 品牌来源
    assert 'FROM whitelabel_settings WHERE user_id = %s", (quote_owner_user_id,)' in src
    # 报价主体本身也归商业主体(否则后续暂停/合规复核会查错人)
    assert "(quote_id, quote_owner_user_id," in src
