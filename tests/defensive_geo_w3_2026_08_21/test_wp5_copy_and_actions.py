"""发布面文案 SSOT 与 action registry(§0.5.5 U-1/U-2/U-10、POR-16、UI-36)。

这一组判据回答一个问题:**她点得到、看得懂吗?**

- U-1:对外文案收口进版本化 copy registry;**内部枚举裸串上屏 = 红**;
- U-10:对外文案 census 从 registry **机械导出**,不手抄;
- POR-16:每个 recovery action 有合法 actionRef / capability / typed target,
  label-only 与 null target 的 dead CTA 一律拒绝。
"""

from __future__ import annotations

import pytest

from services.defensive_geo.copy_registry import (
    PublicCopyLeak,
    assert_public_copy_clean,
    try_user_label,
)
from services.defensive_geo.publish import action_registry as ar
from services.defensive_geo.publish import decision_snapshot as ds
from services.defensive_geo.publish import settlement_review as sr


# ══════════════════════════════════════════════════════════════════════════
# U-1 / U-10:每一个可能上屏的 action 都必须有登记文案,且文案不含内部词
# ══════════════════════════════════════════════════════════════════════════
def test_every_registered_action_has_clean_copy():
    """分母 = action registry 的 census,**不手抄**。

    少一条 = 那条路径一旦被走到就抛 RuntimeError(端点里 ``_label`` 取不到就抛);
    文案带内部词 = U-1 的红。两种都在这里当场暴露。
    """
    kinds = ar.census()["actionKinds"]
    assert kinds, "action registry 是空的 —— 这条锁的分母是零"
    missing = []
    for kind in kinds:
        label = try_user_label("action", kind)
        if not label:
            missing.append(kind)
            continue
        assert_public_copy_clean(label, field=f"action.{kind}")
    assert not missing, (
        f"这些 action 没有登记文案:{missing} —— "
        "端点走到它们时会抛,而那通常正是最少被走到的错误分支"
    )


def test_copy_cleanliness_check_has_discriminating_power():
    """判别力自证:内部枚举裸串必须被 ``assert_public_copy_clean`` 挑出来。

    没有这一条,上面那条在「检查被写成恒真」时也会绿。
    """
    with pytest.raises(PublicCopyLeak):
        assert_public_copy_clean("publish_decision_not_confirmable", field="probe")
    with pytest.raises(PublicCopyLeak):
        assert_public_copy_clean("PUBLISH_DECISION_NOT_CONFIRMABLE", field="probe")
    # 反向:正常人话必须通过(否则上面那条是恒抛,同样零判别力)
    assert assert_public_copy_clean("确认这个媒体方案", field="probe")


def test_admin_review_actions_have_copy():
    """Z-1 的三条处置动作也要有人话 —— admin 面同样不许裸枚举上屏。"""
    for action in sr.ADMIN_ACTIONS:
        label = sr.ACTION_LABELS[action]
        assert label and label.strip()
        assert_public_copy_clean(label, field=f"review.{action}")


# ══════════════════════════════════════════════════════════════════════════
# POR-16 / UI-36:typed action 的四项合同
# ══════════════════════════════════════════════════════════════════════════
def test_action_build_rejects_dead_cta_forms():
    """三种 dead CTA 形态逐个必抛。"""
    ok_target = {"kind": "publish_command", "id": "pcmd_x"}
    # ① 未注册的 kind
    with pytest.raises(ar.ActionRegistryError):
        ar.build("totally_made_up", label="点我", target=ok_target)
    # ② label-only(空 label)
    with pytest.raises(ar.ActionRegistryError):
        ar.build("wait", label="", target=ok_target)
    # ③ null target id
    with pytest.raises(ar.ActionRegistryError):
        ar.build("wait", label="稍等", target={"kind": "publish_command", "id": ""})
    # ④ 错 target kind
    with pytest.raises(ar.ActionRegistryError):
        ar.build("wait", label="稍等", target={"kind": "quote", "id": "1"})
    # 反向:合法组合必须成功(证明不是恒抛)
    action = ar.build("wait", label="稍等", target=ok_target)
    assert action["capability"] == "view_publish_status"
    assert action["actionRef"].endswith(":pcmd_x")


def test_page_targets_are_closed_set():
    """``page`` 类目标只能是 allowlist 里的页 —— 开放页名等于给 dead CTA 开后门。"""
    with pytest.raises(ar.ActionRegistryError):
        ar.build("top_up", label="去充值算力", target={"kind": "page", "page": "somewhere"})
    assert ar.build("top_up", label="去充值算力",
                    target={"kind": "page", "page": "wallet"})["capability"] == "open_wallet"


def test_assert_registered_rejects_capability_swap():
    """交换两个**合法** capability 也必须被拒(§19 变异 181 同族)。"""
    action = ar.build("wait", label="稍等", target={"kind": "publish_command", "id": "pcmd_x"})
    ar.assert_registered(action)                      # 正样本
    swapped = {**action, "capability": "retry_publish_child"}   # 合法但不是这一条的
    with pytest.raises(ar.ActionRegistryError):
        ar.assert_registered(swapped)


def test_mutating_action_set_is_explicit():
    """会动钱/动对象的 action 必须被显式标出来(demo/只读面的分母)。"""
    mutating = ar.mutating_actions()
    # 这几条**必须**在里面 —— 它们各自会创建 command/freeze/outbox 或改对象。
    for kind in ("confirm_publish_decision", "override_publish_decision",
                 "retry_child", "cancel", "new_preview"):
        assert kind in mutating, f"{kind} 会产生副作用却没被标 mutating"
    # 这几条**必须不在** —— 它们是纯查看。
    for kind in ("wait", "view_status", "view_existing_command", "verify_outcome"):
        assert kind not in mutating, f"{kind} 是只读动作却被标成 mutating"


# ══════════════════════════════════════════════════════════════════════════
# §15.7 confirmability:每个 reasonCode 的 action/capability/target 逐值
# ══════════════════════════════════════════════════════════════════════════
def test_every_confirmability_reason_maps_to_registered_action():
    """九个 reasonCode 的 nextAction 必须都在 action registry 里。

    分母从 ``decision_snapshot.census()['reasonCodes']`` 取 —— 不手抄。
    """
    reasons = ds.census()["reasonCodes"]
    assert len(reasons) == 9, f"reasonCode 数量变了({len(reasons)}),判据要同步复核"
    for code, spec in reasons.items():
        kind = spec["nextActionKind"]
        registered = ar.spec(kind)
        assert registered.capability == spec["capability"], (
            f"reasonCode={code} 的 capability 在两处不一致:"
            f"decision_snapshot 说 {spec['capability']},registry 说 {registered.capability}"
        )
        assert spec["targetKind"] in registered.target_kinds, (
            f"reasonCode={code} 的 targetKind={spec['targetKind']!r} "
            f"不在 registry 登记的 {list(registered.target_kinds)} 里"
        )


def test_lifecycle_terminal_reasons_are_marked_inactive():
    """四个 lifecycle 终结态的 reason 必须标 inactive —— 它们不许再开 confirm。"""
    reasons = ds.census()["reasonCodes"]
    lifecycle_map = ds.census()["lifecycleReason"]
    assert set(lifecycle_map) == {"expired", "superseded", "cancelled", "consumed"}
    for code in lifecycle_map.values():
        assert reasons[code]["inactive"] is True, (
            f"{code} 是 lifecycle 终结态却没标 inactive —— "
            "它会带着 blockers/options 出现,等于重新开放了一条已经关掉的门"
        )
    # 反向:非终结的三个不许标 inactive(否则资金不足时不下发出口)
    for code in ("insufficient_points", "approval_required", "inventory_unavailable"):
        assert reasons[code]["inactive"] is False
