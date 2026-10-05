"""P0-7 · 组织路由登记判据 + 「席位永不含社媒」负向锁。

裁定 P0-7:新增/既有图文端点必须同步登记 `MEMBER_GEO_ROUTE_POLICIES`
(交付操作员真可达 + 能力映射),否则守卫层 fail-closed 直接 403。

🔴 判据形态:打**匹配行为**(`match_member_geo_route` 拿真实 path 去匹配),
   不是断言「表里有这个字符串」—— 字符串在但正则编译不出来照样打不进去,
   本仓记过「门禁接了但接线是坏的」。
"""
from __future__ import annotations

import pytest

from services.organization_route_contract import (
    MEMBER_GEO_ROUTE_POLICIES,
    match_member_geo_route,
    route_contract_manifest,
)

# 本包新增/扩展的图文端点(与 02 §9.0、01/02/03 文档同一份清单)
GEO_IMAGE_NOTE_ROUTES: tuple[tuple[str, str, str], ...] = (
    # (method, 真实请求路径样本, 期望 capability)
    ("GET", "/api/geo-douyin/quotes/410/delivery-plan", "writing.read_own"),
)


@pytest.mark.parametrize("method,path,capability", GEO_IMAGE_NOTE_ROUTES)
def test_image_note_route_is_reachable_for_delivery_operator(method, path, capability):
    """正向:交付操作员能通过登记表打到这个路由,且绑定了正确能力。"""
    policy = match_member_geo_route(method, path)
    assert policy is not None, (
        f"{method} {path} 未登记 → 员工席位在守卫层 fail-closed 403,"
        "交付操作员点不动(裁定 P0-7)"
    )
    assert policy.capability == capability, (
        f"{method} {path} 绑定能力是 {policy.capability},期望 {capability}"
    )
    # 对象级二次授权检查的资源类型必须写明 —— 只有能力没有 resource_kind,
    # handler 里就没人负责"这个 quote 是不是你的"
    assert policy.resource_kind, f"{method} {path} 没绑 resource_kind"


@pytest.mark.parametrize("method,path", [
    # 反向对照 ①:同 prefix 下**未列出**的路径必须打不进去(不接受广义前缀匹配)。
    ("GET", "/api/geo-douyin/quotes/410/delivery-plan/secret"),
    ("POST", "/api/geo-douyin/quotes/410/delivery-plan"),
    ("DELETE", "/api/geo-douyin/quotes/410/delivery-plan"),
    # 反向对照 ②:非数字 quote id 不该被当成合法资源 id
    ("GET", "/api/geo-douyin/quotes/abc/delivery-plan"),
])
def test_unlisted_sibling_paths_stay_fail_closed(method, path):
    assert match_member_geo_route(method, path) is None, (
        f"{method} {path} 被放行了 —— 登记表退化成了前缀白名单,fail-closed 失效"
    )


def test_negative_lock_no_social_routes_in_employee_seat_table():
    """🔴 Owner 2026-08-17 负向锁:**员工席位永不含社媒板块**。

    这条不是本包引入的需求,是本包必须**不违反**的既有约束。把它写成可执行判据,
    这样以后任何人往这张 GEO 席位表里塞社媒路由都会当场红,而不是靠人记得。
    """
    forbidden_markers = (
        "/api/social", "/api/s-end", "/api/social-studio", "/api/social_mainpath",
        "/api/personality", "/api/myip", "/api/digital-human",
    )
    offenders = [
        (p.method, p.path_template)
        for p in MEMBER_GEO_ROUTE_POLICIES
        if any(marker in p.path_template.lower() for marker in forbidden_markers)
    ]
    assert not offenders, (
        "员工席位路由表里出现社媒板块路由(Owner 负向锁:席位=GEO 交付工具):\n%s" % offenders
    )


def test_negative_lock_actually_fires():
    """反向对照:负向锁对合成的社媒路由必须命中,否则上面那条全绿只是"表里恰好没有"。"""
    forbidden_markers = ("/api/social",)
    synthetic = ["/api/social/studio/scripts"]
    hits = [p for p in synthetic if any(m in p.lower() for m in forbidden_markers)]
    assert hits, "负向锁的匹配逻辑对真实违规样本零命中 = 恒真"


def test_manifest_exposes_the_new_routes():
    """登记表要能被导出成 contract(运维/审计读的是这个 manifest)。"""
    manifest = route_contract_manifest()
    assert isinstance(manifest, list) and manifest
    paths = {(row["method"], row["path_template"]) for row in manifest}
    assert ("GET", "/api/geo-douyin/quotes/{quote_id}/delivery-plan") in paths


def test_route_table_is_not_empty():
    """分母先立住:表为空的话上面所有"未命中=fail-closed"都是恒真。"""
    assert len(MEMBER_GEO_ROUTE_POLICIES) > 50
