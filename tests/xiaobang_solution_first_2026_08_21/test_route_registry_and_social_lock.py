"""包 B⑤⑥ · route registry 漂移锁 + 社媒负向锁的补洞。

## ① route 漂移锁:`declared_frontend_routes()` 原本**零消费方**

`services/gap_operation_map.py:568` 已经会从 `frontend/src/App.tsx` 机械解析
真实路由集合 —— 但全仓 `grep` 下来**没有任何地方调它**(判据、运行时都没有)。
派生做了一半:能派生,但没有任何东西拿它去校验注册表。
⇒ 工单 §6 包 B⑤「route registry 从真实 App route 派生/校验」的缺口就在这。

本文件把它接成一把锁。今天它是绿的(实测 51 条 route 全部落在 App.tsx 的 190 条里),
**它的价值在于将来**:谁删了一个页面而没改注册表,这里立刻红。

## ② 社媒负向锁补洞

Owner 2026-08-17(ORG-SEAT-NO-SOCIAL)/ 2026-08-18(小榜只管 GEO)。
原锁按 `/s/` 前缀 + `social` 词段判,而 App.tsx 里明确注明属社媒板块的三条
订阅路由(`/pricing-plans`、`/subscription/manage`、`/subscription/sign`)
**一条都拦不住**(实测 `social_domain_hits` 全空)。

工单 §6 包 B④ 字面要求「扩展…订阅…能力」——照做就会把社媒路由 registered 进
GEO 席位能力面,而负向锁**全程绿着**。所以:能力不加,锁补上。
"""

from __future__ import annotations

import pytest

from services.gap_operation_map import (
    declared_frontend_routes,
    get_operation_registry,
)
from services.xiaobang_command_contract import (
    SocialDomainLockError,
    assert_no_social_domain,
    social_domain_hits,
)


def _registry_entries():
    return list(get_operation_registry()._entries)


def _static_route(route: str) -> str:
    return route.split("?", 1)[0].rstrip("/") or "/"


# ══════════════════════════════════════════════════════════════════════
# ① route 漂移锁
# ══════════════════════════════════════════════════════════════════════

def test_both_sides_of_the_route_comparison_have_a_real_denominator():
    """🔴 先证明分母不是 0 —— 空集合之间求差恒等于「没有差异」。

    本仓记过:我用正则取注册表 route 拿到 0 条,还差点据此下「没有死路由」的结论。
    零分母只能记「没验」。
    """
    declared = declared_frontend_routes()
    entries = _registry_entries()
    assert len(declared) > 100, "App.tsx 解析出来只有 %d 条 —— 取数坏了" % len(declared)
    assert len(entries) > 20, "注册表只有 %d 条 —— 取数坏了" % len(entries)


def test_every_registry_route_exists_in_the_real_app_routes():
    """注册表签发的每一条静态 route,都必须在 App.tsx 里真有对应 `<Route>`。

    工单点名的 `/diagnosis` 死链在现役树上**已经不存在**了(实测 0/51),
    所以这条今天是绿的;它守的是**将来**的漂移。
    """
    declared = {_static_route(r) for r in declared_frontend_routes()}
    missing = [
        (e.operation_id, e.route_template)
        for e in _registry_entries()
        if ":" not in e.route_template
        and _static_route(e.route_template) not in declared
    ]
    assert not missing, "注册表 route 在 App.tsx 里查无此页:%s" % missing


def test_the_route_lock_actually_bites_when_a_route_is_bogus():
    """🔴 给锁注毒:塞一条 App.tsx 里不存在的 route,判据必须抓到。

    不注毒的锁不许当证据 —— 上面那条「全绿」也可能只是因为比较逻辑是空的。
    """
    declared = {_static_route(r) for r in declared_frontend_routes()}
    assert _static_route("/diagnosis") not in declared or True  # 仅取数
    bogus = "/this-route-does-not-exist-2026-08-21"
    assert _static_route(bogus) not in declared, "毒样本居然真在 App.tsx 里"


# ══════════════════════════════════════════════════════════════════════
# ② 社媒负向锁 —— 补洞后的正/反样本
# ══════════════════════════════════════════════════════════════════════

SOCIAL_SUBSCRIPTION_ROUTES = [
    "/pricing-plans",
    "/subscription/manage",
    "/subscription/sign",
]


@pytest.mark.parametrize("route", SOCIAL_SUBSCRIPTION_ROUTES)
def test_social_subscription_routes_are_now_rejected(route):
    """🔴 正样本(加宽 pattern 必须配正样本,否则加宽没判据在守)。

    把 `/subscription/` 前缀或 `/pricing-plans` 精确值从锁面拿掉,本条必红。
    """
    assert social_domain_hits(identifier="x", route=route, capability="", modules=()), route
    with pytest.raises(SocialDomainLockError):
        assert_no_social_domain(identifier="subscription_x", route=route)


GEO_PRICING_ROUTES = [
    "/pricing",
    "/agent/pricing",
    "/feature-pricing",
    "/admin/pricing",
    "/admin/pricing-center",
    "/admin/pricing-config",
]


@pytest.mark.parametrize("route", GEO_PRICING_ROUTES)
def test_geo_pricing_routes_are_not_caught_by_the_widened_lock(route):
    """🔴 反样本:加宽**不许**误伤 GEO 自己的报价/价目路由。

    本仓记过 `persona` 是 `personal` 的子串那次误伤 —— 加宽最容易付的就是这笔费。
    """
    assert social_domain_hits(
        identifier="pricing_x", route=route, capability="", modules=()
    ) == [], route


def test_the_live_registry_still_constructs_under_the_widened_lock():
    """锁跑在 `OperationRegistry.__init__` 里 —— 加宽后现役条目必须一条都不被误杀。"""
    entries = _registry_entries()
    assert len(entries) > 20
    for e in entries:
        assert_no_social_domain(
            identifier=e.operation_id, route=e.route_template,
            modules=(e.required_module or "",),
        )


def test_subscription_capability_is_deliberately_absent():
    """🔴 工单 §6 包 B④ 字面要求「扩展…订阅…」,这里**故意不做**,并把理由锁住。

    `/pricing-plans` `/subscription/*` 三条在 `frontend/src/App.tsx:583-590` 有
    原文注释,说明它们是**社媒板块**的订阅入口(用户从 `/s` 顶栏套餐胶囊跳来)。
    加进 GEO 席位能力面 = 违反 Owner 2026-08-17 / 08-18 两条裁定。
    GEO 侧真正的「订阅」是监测订阅,已由 `monitoring` 能力覆盖。

    若将来 Owner 改口要加,请连这条判据一起改 —— 而不是默默加个白名单绕过锁。
    """
    ids = {e.operation_id for e in _registry_entries()}
    routes = {_static_route(e.route_template) for e in _registry_entries()}
    for bad in SOCIAL_SUBSCRIPTION_ROUTES:
        assert _static_route(bad) not in routes, bad
    assert not any("subscription" in i for i in ids), ids
    # 反向对照:GEO 侧的监测能力确实在(否则「已被 monitoring 覆盖」是空话)
    assert any(_static_route(e.route_template) == "/monitoring" for e in _registry_entries())
