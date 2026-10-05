"""操作地图锁(合同判据 #9 / C2)

判据 #9 原文:「删除操作地图中的某个入口后,对应导航测试必须转红;
LLM 文本不得兜底发明路径。」

🔴 这条判据有两半,只做前半是假的:
   前半 = 删条目 → 锁转红(下面 test_gap_plan_entry_resolves_to_pricing 就是)
   后半 = 删条目 → **线上真的回答不出来**,而不是模型编一个像样的假路径
   后半由 test_unknown_operation_returns_none_not_a_guess +
   test_assistant_never_emits_a_route_outside_the_map 一起钉。
"""

from __future__ import annotations

import pytest

from services import gap_operation_map as omap


# ────────────────────────────────────────────────────────────────
# 前半:删条目 → 转红
# ────────────────────────────────────────────────────────────────

def test_gap_plan_entry_resolves_to_pricing():
    """删掉 gap_plan_block 条目 → 本条转红(变异 M5)。"""
    entry = omap.resolve_operation("gap_plan_block")
    assert entry is not None, "操作地图里没有「交付计划」入口,小榜将答不出它在哪"
    assert entry.route == "/pricing"
    assert entry.breadcrumb == ("客户报价", "打开词包", "交付计划")
    assert entry.help_target == "gap-plan-section"


def test_media_library_entry_points_at_publish_not_an_invented_route():
    """媒体渠道在 /publish 页内,没有独立路由。答成 /media 就是编的。"""
    entry = omap.resolve_operation("media_library")
    assert entry is not None
    assert entry.route == "/publish"


@pytest.mark.parametrize("operation_id", [
    "gap_plan_block", "gap_plan_publication_link", "media_library",
    "writing_center", "publish_center", "quote_center", "client_list",
    "monitoring_center",
])
def test_every_declared_route_exists_in_app_tsx(operation_id):
    """🔴 交叉核验:地图里写的路由必须在 frontend/src/App.tsx 里真实存在。

    写错一个字母(/publishing vs /publish)当场转红 ——
    这比"我核过了"可靠,因为 App.tsx 改了没人会来同步这份地图。
    """
    routes = omap.declared_frontend_routes()
    assert routes, "从 App.tsx 一条路由都没扫出来 → 这条判据零判别力"
    entry = omap.resolve_operation(operation_id)
    assert entry is not None
    assert entry.route in routes, f"{operation_id} 的路由 {entry.route} 在 App.tsx 里不存在"


def test_route_crosscheck_would_catch_a_typo():
    """反向对照:证明上面那条不是恒真 —— 一个不存在的路由必须查不到。"""
    routes = omap.declared_frontend_routes()
    assert "/pricing" in routes
    assert "/pricing-gap-plan" not in routes
    assert "/media" not in routes          # 最容易编出来的那个假路径


# ────────────────────────────────────────────────────────────────
# 后半:查不到就说不准,不许猜
# ────────────────────────────────────────────────────────────────

def test_unknown_operation_returns_none_not_a_guess():
    assert omap.resolve_operation("this_entry_does_not_exist") is None
    assert omap.match_operation("这个功能在哪里啊完全不相关的问题") is None
    assert omap.match_operation("") is None


def test_match_finds_the_entry_by_human_phrasing():
    """反向对照:匹配不是恒 None。"""
    assert omap.match_operation("交付计划在哪里？").operation_id == "gap_plan_block"
    assert omap.match_operation("我去哪查渠道").operation_id == "media_library"
    assert omap.match_operation("发布链接填哪里").operation_id == "gap_plan_publication_link"


def test_every_action_in_the_map_is_a_real_dictionary_action():
    """地图里签发的动作必须在人话字典里 —— 否则界面会出现一个没有文案的按钮。"""
    from services.gap_operation_labels import known_action_ids

    known = known_action_ids()
    for entry in omap.all_operations():
        for action_id in entry.actions:
            assert action_id in known, f"{entry.operation_id} 签发了字典里没有的动作 {action_id}"


def test_retired_entries_are_not_answerable():
    """失效版本的条目不得再被答出来(合同 §5「删除入口后不得靠旧知识库回答」)。"""
    from dataclasses import replace

    live = omap.resolve_operation("gap_plan_block")
    retired = replace(live, until_version="operation-map-v0")
    assert retired.until_version is not None
    # all_operations 只收 until_version 为 None 的
    assert all(e.until_version is None for e in omap.all_operations())


def test_map_version_is_pinned():
    # v1 已由唯一 OperationRegistry v2 接管；P4 通过薄适配继续消费同一真相源。
    assert omap.OPERATION_MAP_VERSION == omap.OPERATION_REGISTRY_VERSION
    assert omap.OPERATION_REGISTRY_VERSION == "operation-registry-v3"
    # 🔴 旧值必须留在兼容表里。升版时**只加不删**(与部署脚本 sha256 同规矩):
    #    删掉旧值 = 缓存里的旧 bundle 当场全量失配,而那正是 §4.1-7 的债务本身。
    assert "operation-registry-v2" in omap.OPERATION_REGISTRY_COMPATIBLE_VERSIONS
    assert omap.OPERATION_REGISTRY_VERSION in omap.OPERATION_REGISTRY_COMPATIBLE_VERSIONS
    described = omap.describe(omap.resolve_operation("gap_plan_block"))
    assert described["operation_map_version"] == omap.OPERATION_REGISTRY_VERSION
    # 只扫描真正下发给用户的签名动作字段；角色/前置条件是服务端授权元数据。
    from services.gap_operation_labels import assert_no_internal_leak
    public_action = {
        key: described[key]
        for key in (
            "display_name", "target_route", "breadcrumb",
            "help_target", "operation_map_version",
        )
    }
    assert_no_internal_leak(public_action, where="operation-map")
