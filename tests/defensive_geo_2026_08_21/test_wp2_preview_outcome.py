"""#97 · `canConfirm` 与 `nextAction` 必须互斥,且只在**一处**派生。

缺陷:`lifecycle` 只有 open / expired / consumed 三值,而两个端点里各写了一遍
同样的三元链,其 `else` 分支正是 **open** —— 给的却是 `wait`(「稍等,正在体检」),
同一份响应里 `canConfirm` 又是 true。「你可以确认」和「请等待」同时说。

🔴 两档处置**相反**:一个要她点确认,一个要她别动。压成一档时没有任何症状 ——
   响应 200、字段齐全、文案合法,只有人读到才发现自相矛盾。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: 分母 = lifecycle 的**全部**取值,从 copy 登记表机械取,不手写清单。
def _all_lifecycles() -> list[str]:
    from services.defensive_geo.copy_registry import _PREVIEW_LIFECYCLE
    return sorted(_PREVIEW_LIFECYCLE)


def _row(lifecycle: str) -> dict:
    return {"lifecycle": lifecycle, "preview_id": "prev-1",
            "question_plan_id": "plan-1", "consumed_command_id": "cmd-1"}


def test_lifecycle_denominator_is_exactly_three():
    """分母自证:少一个值,下面的参数化就悄悄少验一档。"""
    assert _all_lifecycles() == ["consumed", "expired", "open"], _all_lifecycles()


@pytest.mark.parametrize("lifecycle,expect_confirm", [
    ("open", True), ("expired", False), ("consumed", False)])
def test_can_confirm_is_pinned_per_lifecycle(lifecycle, expect_confirm):
    from api.defensive_geo_api import _preview_outcome
    can, _ = _preview_outcome(_row(lifecycle))
    assert can is expect_confirm


def test_open_gets_a_confirm_exit_not_a_wait():
    """open(待确认)的出口必须是「去确认」,不是「等着」。"""
    from api.defensive_geo_api import _preview_outcome
    _, action = _preview_outcome(_row("open"))
    assert action["kind"] == "confirm_run_preview", action


@pytest.mark.parametrize("lifecycle", _all_lifecycles())
def test_confirmable_and_wait_are_never_both_true(lifecycle):
    """🔁 反臂:**任何** lifecycle 都不许同时「能确认」且「让她等」。

    这条是本单的不变式本体 —— 上面按档钉死的那几条会随产品口径变,
    而「两句互相矛盾的话不能同时说」不会变。
    """
    from api.defensive_geo_api import _preview_outcome
    can, action = _preview_outcome(_row(lifecycle))
    assert not (can and action["kind"] == "wait"), (lifecycle, can, action)


def test_the_wait_action_is_gone_from_this_endpoint_module():
    """🔁 接线锁:该模块里不许再出现 `_action("wait"` —— 它是这个 bug 的本体。"""
    src = (ROOT / "api" / "defensive_geo_api.py").read_text(encoding="utf-8")
    assert '_action("wait"' not in src, "wait 又回来了"


def test_outcome_is_derived_in_exactly_one_place():
    """🔴 单点派生锁:两个端点必须都调 `_preview_outcome`,不许各写一遍。

    这个 bug 本身就是**两份各写一遍**的产物 —— 改两份而不抽一处,
    下一次只会改一处,而漂开那天不会有任何判据变红。
    分母 = 模块里对 `canConfirm=` 的**全部**赋值点,机械枚举。
    """
    src = (ROOT / "api" / "defensive_geo_api.py").read_text(encoding="utf-8")
    sites = [ln.strip() for ln in src.splitlines() if "canConfirm=" in ln]
    assert sites, "一个 canConfirm 赋值点都没有 —— 分母为空,这条什么都没验"
    for s in sites:
        assert s == "canConfirm=can_confirm,", f"有人绕开单点派生:{s}"
    assert src.count("def _preview_outcome(") == 1, "派生函数被写了第二份"


def test_the_open_label_does_not_tell_her_to_wait():
    """对客文案层:open 的出口文案不许是「稍等」那一族。

    🔴 与上面的 kind 断言分开:kind 改对了而文案仍写「稍等,正在体检」,
       她看到的还是矛盾 —— 用户读的是文案,不是 kind。
    """
    from services.defensive_geo.copy_registry import user_label
    label = user_label("action", "confirm_run_preview")
    assert "稍等" not in label and "等" not in label, label
    assert label.strip(), "新动作没登记文案"
