"""[补充工单 2026-08-06] 账号来源字段必须能过**真出口 DTO**。

🔴 这条锁是被一次真事故逼出来的:
`87c29cdf` 已 PASS 并在飞,但它的 §3 锁全打在 `_relationships()` 这一层。
`schemas/admin_user_governance.py` 的 DTO 是 `extra="forbid"`,而那个包
给 registration 加了 `account_origin` / `organization` 两个键、
并把 `source` 设成 `"organization_invite"` —— **三条都过不了 DTO**。
后果:组织操作员账号(#161 / #159,正是 Owner 报的那两个)点开详情页直接 500,
比原来那句误导性的「无邀请记录」更糟。

教训一句话:**锁打在函数层不够,要打在真正的出口上。**
所以这里不测 `_relationships()`,而是把它的真实产物喂给真正的响应模型。
"""
import datetime
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from schemas.admin_user_governance import (  # noqa: E402
    AdminUserListItem,
    RegistrationAttribution,
)


class _Cur:
    def __init__(self, membership):
        self._m = membership
        self._rows = []

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        if "FROM organization_memberships m" in flat:
            self._rows = [self._m] if self._m else []
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def _relationships_for(membership, created="2026-08-04"):
    import importlib
    gov = importlib.import_module("services.admin_user_governance")
    gov._actor = lambda cur, uid: {
        "user_id": int(uid), "username": f"u{uid}", "display_name": f"用户{uid}",
        "is_active": True, "business_identity": "ordinary_user", "company": None,
    }
    user = {"id": 161, "created_at": datetime.datetime.fromisoformat(created + "T00:00:00")}
    return gov._relationships(_Cur(membership), user,
                              {"commercial_binding": 1, "channel_relationship": 1})


_MEMBER = {
    "organization_id": 2, "status": "active", "is_owner": False, "role_id": 7,
    "joined_at": datetime.datetime(2026, 8, 4, 11, 24),
    "org_name": "测试一下", "owner_user_id": 46,
}


def test_detail_dto_accepts_organization_member_registration():
    """🔴 组织操作员账号的详情载荷必须能过 DTO —— 过不了就是 500。"""
    reg = _relationships_for(_MEMBER)["registration"]
    assert reg["source"] == "organization_invite"
    parsed = RegistrationAttribution.model_validate(reg)  # 过不了会抛 ValidationError
    assert parsed.account_origin == "organization_member"
    assert parsed.organization is not None
    assert parsed.organization.name == "测试一下"


def test_detail_dto_still_accepts_self_signup_registration():
    """必须不命中:自助注册那条路不许被这次扩容改坏。"""
    reg = _relationships_for(None)["registration"]
    parsed = RegistrationAttribution.model_validate(reg)
    assert parsed.account_origin == "self_signup"
    assert parsed.organization is None


def test_detail_dto_still_forbids_unknown_keys():
    """🔴 反向对照:DTO 仍然是严格的 —— 别为了让它过就把 extra 放开。

    这条要是绿不了,说明我把闸整个关掉了,那比字段没加更糟。
    """
    reg = _relationships_for(None)["registration"]
    reg["totally_unexpected_key"] = 1
    with pytest.raises(Exception):
        RegistrationAttribution.model_validate(reg)


def test_list_dto_accepts_origin_fields():
    """列表行 DTO 同样是 extra=forbid —— 不声明就是整个列表接口 500。"""
    item = {
        "user_id": 161, "username": "demo42-alpha", "display_name": "尚", "phone": None,
        "is_active": True, "business_identity": "ordinary_user", "platform_access": "standard",
        "total_points": 0, "customer_count": 0, "brand_count": 0,
        "service_mode": "platform_direct",
        "account_origin": "organization_member", "organization_name": "测试一下",
        "needs_attention": False, "attention_label": None,
        "created_at": None, "last_active_at": None, "versions": {},
    }
    parsed = AdminUserListItem.model_validate(item)
    assert parsed.account_origin == "organization_member"
    assert parsed.organization_name == "测试一下"


def test_list_dto_still_forbids_unknown_keys():
    """反向对照:列表 DTO 也必须仍然严格。"""
    item = {
        "user_id": 1, "username": "u", "display_name": "u", "is_active": True,
        "business_identity": "ordinary_user", "platform_access": "standard",
        "total_points": 0, "customer_count": 0, "brand_count": 0,
        "service_mode": "platform_direct", "needs_attention": False,
        "versions": {}, "some_unknown_field": 1,
    }
    with pytest.raises(Exception):
        AdminUserListItem.model_validate(item)
