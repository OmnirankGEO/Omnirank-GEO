"""动作落点锁(2026-08-08 补 · 小榜运营回答原来一个跳转按钮都出不来)

背景:前端只认服务端签发的 `target_route`(不拼、不猜、不兜底旧路径),
而 `translate_action()` 只出 action_id/label/icon/confirmation/enabled ——
**不带路由**。于是 `answer_operations` 照抄卡片动作时,动作全是"有名字没落点",
右下角气泡里的运营回答只有文字,一个按钮都渲染不出来。

🔴 这一组锁刻意不去断言"某个动作的路由等于 '/publish'" —— 那只是把实现抄一遍,
   实现改成 '/pub' 锁跟着改就永远绿。真正要守的两条性质是:
     1. 落点必须是 **App.tsx 里真实存在的路由**(指向不存在的页面 = 把人送到 404);
     2. 落点必须来自**地图**,而地图与 App.tsx 有交叉核验 —— 所以路由串不许在
        别处出现第二份。
   外加两条分布/边界锁,防止把映射写成"人人有份"或"人人没有"。
"""

from __future__ import annotations

import pytest

from services import gap_assistant, gap_operation_map as omap
from tests.gap_plan_2026_08_08.conftest import AGENT_USER, make_app


class _FakeRequest:
    def __init__(self, user):
        class _State:
            pass
        self.state = _State()
        self.state.user = dict(user)
        self.state.organization_identity = None


def _assistant_actions(quote_id: int, request_id: str) -> list[dict]:
    ans = gap_assistant.build_answer(
        question="这个客户今天先做什么？",
        context_refs={"quote_id": quote_id},
        request=_FakeRequest(AGENT_USER),
        assistant_request_id=request_id,
        actor_user_id=4001,
    )
    assert ans is not None
    return list(ans.actions)


# ════════════════════════════════════════════════════════════════
# 1. 映射表本身的边界
# ════════════════════════════════════════════════════════════════

def test_every_mapped_action_lands_on_a_route_that_really_exists():
    """🔴 落点必须在 App.tsx 声明的路由里 —— 交叉核验,不是自我印证。"""
    declared = omap.declared_frontend_routes()
    assert declared, "扫不到 App.tsx 的路由 → 这条锁会退化成恒真"
    for action_id in omap.action_operation_ids():
        landing = omap.route_for_action(action_id)
        assert landing, f"{action_id} 映射了 operation 却取不到落点"
        assert landing["target_route"] in declared, (
            f"{action_id} 的落点 {landing['target_route']} 在 App.tsx 里不存在 —— 会把人送到 404"
        )


def test_mapping_points_at_operations_that_are_still_live():
    """映射的目标必须是仍在生效的地图条目(下线条目 resolve 不到就不该给按钮)。"""
    live = {e.operation_id for e in omap.all_operations()}
    for action_id, operation_id in omap.action_operation_ids().items():
        assert operation_id in live, f"{action_id} 指向已下线/不存在的入口 {operation_id}"


def test_unmapped_action_gets_no_route_at_all():
    """🔴 反向对照:没映射的动作**必须**取不到落点。

    没有这一条,把 route_for_action 写成"查不到就回默认页"照样全绿 ——
    而那正是「宁可没按钮也不给八成不对的落点」要防的事。
    """
    assert omap.route_for_action("explain_plan") is None
    assert omap.route_for_action("open_assistant_advice") is None
    assert omap.route_for_action("keep_suggestion") is None
    assert omap.route_for_action("a_action_that_does_not_exist") is None


def test_write_actions_are_never_mapped():
    """🔴🔴 写类动作永远不许有落点 —— 有了就等于让小榜把人直接送到执行面前。"""
    mapped = set(omap.action_operation_ids())
    write_actions = {"open_writing_task", "submit_publication_link",
                     "mark_domain_unavailable", "request_quote_capacity"}
    assert not (mapped & write_actions), f"写类动作被映射了落点:{mapped & write_actions}"
    # 更强的一条:映射表的键必须全部落在助手允许签发的导航动作集合里
    assert mapped <= gap_assistant._NAVIGATION_ACTIONS, (
        f"映射表里有助手根本不该签发的动作:{mapped - gap_assistant._NAVIGATION_ACTIONS}"
    )


def test_routes_are_not_duplicated_outside_the_map():
    """🔴 路由串只许有一份:助手模块自己不许出现任何路由字面量。

    出现第二份 = 绕过了地图与 App.tsx 的交叉核验(地图改了它不会跟着改)。
    """
    from pathlib import Path
    src = Path(gap_assistant.__file__).read_text(encoding="utf-8")
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    import re
    hits = re.findall(r"""['"]/(?:pricing|publish|writing|monitoring|my-clients)[^'"]*['"]""", body)
    assert not hits, f"gap_assistant 里出现了路由字面量:{hits}"


# ════════════════════════════════════════════════════════════════
# 2. 真链路:助手出来的动作确实带上了落点
# ════════════════════════════════════════════════════════════════

def test_operations_answer_now_carries_a_landing_for_mapped_actions(db, no_media_recommender):
    """报价 372(容量 0)的运营回答里,可映射的动作必须带 target_route。"""
    actions = _assistant_actions(372, "req-route-372")
    mapped = set(omap.action_operation_ids())
    present = [a for a in actions if a["action_id"] in mapped]
    if not present:
        pytest.skip("这张单当前的动作里没有可映射项 —— 由下面的分布锁负责覆盖")
    declared = omap.declared_frontend_routes()
    for action in present:
        assert action.get("target_route"), f"{action['action_id']} 仍然没有落点"
        assert action["target_route"] in declared


def test_landing_comes_from_the_map_not_from_a_hand_written_string(db, no_media_recommender):
    """落点必须与地图当场算出来的一致(而不是助手里另写了一份)。"""
    for quote_id, rid in ((372, "req-src-372"), (900, "req-src-900")):
        for action in _assistant_actions(quote_id, rid):
            expected = omap.route_for_action(action["action_id"])
            if expected is None:
                continue
            assert action.get("target_route") == expected["target_route"]
            assert action.get("help_target") == expected["help_target"]


def test_write_actions_still_have_no_landing_in_a_real_answer(db, no_media_recommender):
    """🔴 端到端复核:真答案里不许出现"写类动作 + 落点"这种组合。"""
    for quote_id, rid in ((372, "req-w-372"), (900, "req-w-900")):
        for action in _assistant_actions(quote_id, rid):
            if action["action_id"] in {"open_writing_task", "submit_publication_link",
                                       "mark_domain_unavailable"}:
                pytest.fail(f"助手签发了写类动作:{action['action_id']}")


def test_some_actions_have_a_landing_and_some_do_not(db, no_media_recommender):
    """🔴 分布锁:带落点与不带落点**都要出现过**。

    少了任一边,"人人有份"和"人人没有"这两种写死实现都能全绿。
    覆盖面同时取导航回答(必带)与运营回答(部分带)。
    """
    seen = set()
    nav = gap_assistant.answer_navigation("媒体库在哪")
    assert nav is not None
    seen.update(bool(a.get("target_route")) for a in nav.actions)
    for quote_id, rid in ((372, "req-dist-372"), (900, "req-dist-900")):
        seen.update(bool(a.get("target_route")) for a in _assistant_actions(quote_id, rid))
    assert seen == {True, False}, f"落点分布退化了:{seen}"


def test_a_preset_landing_is_never_overwritten_by_the_action_table():
    """🔴 已有落点不许被按动作类型查表覆盖 —— **构造用例**。

    变异分诊留痕(2026-08-08):第一版这条锁打在"混合问题"的真链路上,
    拆掉 `_with_routes` 里那道"已有 target_route 就原样返回"的守卫,锁**没转红** ——
    因为真链路上带落点的动作(`go_to_signed_route`)根本不在映射表里,
    没有东西能覆盖它。那是**空操作变异**,不是锁弱。
    要让这条守卫真的被验到,必须构造一个"既在映射表里、又已经带了别的落点"的动作。
    """
    mapped_id = next(iter(omap.action_operation_ids()))
    table_landing = omap.route_for_action(mapped_id)["target_route"]
    preset = "/monitoring" if table_landing != "/monitoring" else "/my-clients"
    assert preset != table_landing, "构造失效:预设落点必须与查表落点不同,否则判据零判别力"

    out = gap_assistant._with_routes([
        {"action_id": mapped_id, "label": "x", "enabled": True, "target_route": preset},
    ])
    assert out[0]["target_route"] == preset, (
        f"已有落点被查表覆盖了({preset} → {out[0]['target_route']})"
    )


def test_navigation_landing_survives_a_mixed_question(db, no_media_recommender):
    """混合问题的真链路复核:导航回答按**问题**匹配出的精确落点要活到最后。"""
    ans = gap_assistant.build_answer(
        question="媒体库在哪？这个客户今天先做什么？",
        context_refs={"quote_id": 372},
        request=_FakeRequest(AGENT_USER),
        assistant_request_id="req-mixed-route",
        actor_user_id=4001,
    )
    assert ans is not None
    nav_action = next((a for a in ans.actions if a["action_id"] == "go_to_signed_route"), None)
    assert nav_action is not None, "混合回答里应当保留一个导航动作"
    expected = omap.describe(omap.match_operation("媒体库在哪"))
    assert nav_action["target_route"] == expected["target_route"]


def test_the_landing_actually_reaches_the_api_payload(db, no_media_recommender):
    """🔴 接线锁:落点要能一路走到端点出参,不是只在函数返回值里正确。

    (本包 2026-08-08 刚踩过:后端算对了、前端把它丢了。判据必须打在出口上。)
    """
    from fastapi.testclient import TestClient

    client = TestClient(make_app(AGENT_USER), raise_server_exceptions=False)
    page = client.get("/api/quotes/372/delivery-plan")
    assert page.status_code == 200
    ans = _assistant_actions(372, "req-payload-372")
    meta = gap_assistant.build_answer(
        question="这个客户今天先做什么？",
        context_refs={"quote_id": 372},
        request=_FakeRequest(AGENT_USER),
        assistant_request_id="req-payload-meta",
        actor_user_id=4001,
    ).as_meta()
    emitted = meta["gap_assistant"]["actions"]
    assert [a["action_id"] for a in emitted] == [a["action_id"] for a in ans]
    # as_meta() 会过出参泄漏闸;落点必须**活着穿过**那道闸,不被当成内部字段剔掉
    for action, source in zip(emitted, ans):
        assert action.get("target_route") == source.get("target_route")



# ════════════════════════════════════════════════════════════════
# 3. 兜底落点:运营回答必须**总有地方可去**
#
# 🔴 这一组是本轮真正解决问题的那一半。补完 action→operation 映射之后,
#    2026-08-08 实测报价 372 / 900 的运营回答**仍然一个按钮都没有** ——
#    因为助手只取 focus 那一张卡,而 focus 的两种常见态给出的恰好是
#    explain_plan / keep_suggestion 这两个不该有落点的同屏动作。
#    带落点的动作只长在非 focus 的卡上,助手永远摆不出来。
# ════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("quote_id", [372, 900])
def test_every_operations_answer_has_at_least_one_place_to_go(quote_id, db, no_media_recommender):
    """🔴 真实样本两张单都必须有可点的落点(这是本轮的验收本体)。"""
    actions = _assistant_actions(quote_id, f"req-landing-{quote_id}")
    landed = [a for a in actions if a.get("target_route")]
    assert landed, (
        f"报价 {quote_id} 的运营回答一个可跳转的动作都没有 —— "
        f"右下角气泡里只有文字。实得:{[a['action_id'] for a in actions]}"
    )
    declared = omap.declared_frontend_routes()
    for action in landed:
        assert action["target_route"] in declared


@pytest.mark.parametrize("quote_id", [372, 900])
def test_the_card_action_still_comes_first(quote_id, db, no_media_recommender):
    """兜底落点排在**最后** —— 卡片自己的动作优先,它只是保证"总有地方可去"。"""
    ids = [a["action_id"] for a in _assistant_actions(quote_id, f"req-order-{quote_id}")]
    assert ids[-1] == gap_assistant._PLAN_LANDING_ACTION, ids
    assert len(ids) >= 2, f"卡片自己的动作被兜底落点挤掉了:{ids}"


def test_plan_landing_is_never_duplicated(db, no_media_recommender):
    """卡片本来就带这个动作时不许再追加一份。"""
    from services import gap_assistant as ga
    snapshot = {
        "summary": {"next_step": "先确认渠道"},
        "capacity": {},
        "items": [{
            "status": {"code": "ready_to_execute"},
            "actions": [{"action_id": ga._PLAN_LANDING_ACTION, "label": "去这个客户的交付计划",
                         "enabled": True}],
            "rationale": {},
        }],
    }
    ids = [a["action_id"] for a in ga.answer_operations(snapshot).actions]
    assert ids.count(ga._PLAN_LANDING_ACTION) == 1, ids


def test_plan_landing_is_not_a_write_action():
    """🔴 兜底落点必须是纯导航:它是唯一一个被无条件追加的动作,
    一旦它带副作用,等于给每个运营回答都挂了一个可以直接执行的按钮。"""
    from services.gap_operation_labels import translate_action
    action = translate_action(gap_assistant._PLAN_LANDING_ACTION)
    assert action["confirmation"] is False, "需要确认 = 它不是纯导航"
    assert gap_assistant._PLAN_LANDING_ACTION in gap_assistant._NAVIGATION_ACTIONS


def test_mapped_actions_do_fire_when_the_focus_card_carries_them(db, no_media_recommender):
    """构造用例:focus 卡带可映射动作时,那条动作也必须拿到落点。

    🔴 必须构造 —— 真实样本 372/900 的 focus 卡**恰好**都不带可映射动作
       (实测:capacity_zero → keep_suggestion,ready_to_execute → explain_plan),
       只靠真数据跑,这条映射永远不会被触发一次,锁等于恒真。
    """
    from services import gap_assistant as ga
    snapshot = {
        "summary": {"next_step": "先确认一个可以进入的发布渠道"},
        "capacity": {},
        "items": [{
            "status": {"code": "hold_until_domain_access_confirmed"},
            "actions": [{"action_id": "open_media_library", "label": "去媒体库查渠道",
                         "enabled": True}],
            "rationale": {},
        }],
    }
    actions = ga.answer_operations(snapshot).actions
    media = next(a for a in actions if a["action_id"] == "open_media_library")
    expected = omap.route_for_action("open_media_library")
    assert media.get("target_route") == expected["target_route"]
    assert media.get("help_target") == expected["help_target"]
