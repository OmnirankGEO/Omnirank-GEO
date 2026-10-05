"""§0.5.5 U-1 / U-2 / U-10:对外文案单一 SSOT + 状态词全量补译。

用户基准 = 40 岁非技术销售。判据要防的两件事:

  ① **漏译** —— 某个状态没有人话,前端只好把枚举原样显示。
     分母**不手抄**:从 run_status 投影与资金矩阵的**代码**里现取所有可能上屏的枚举,
     逐个要求 registry 里有译文。新加一个状态忘了补译 → 必红。
  ② **漏出内部词** —— 后端图省事把 enum 塞进 label。
     判**形态**不判词表(补一个词漏三个词那种判法本仓明令禁止)。
"""

from __future__ import annotations

import pytest

from services.defensive_geo.copy_registry import (
    COPY_REGISTRY_VERSION,
    CopyNotRegistered,
    PublicCopyLeak,
    assert_public_copy_clean,
    census,
    try_user_label,
    user_label,
)
from services.defensive_geo.funding_projection import funding_policies
from services.defensive_geo.run_status_projection import known_statuses, project


def test_registry_version_is_pinned():
    # 🔴 升版规矩:「改任何一条译文都必须升版」—— 它逼每一次动文案都显式改一次
    #    版本号,而不是让旧前端缓存着旧句子却对不上账。
    #    [合流 2026-08-24 Review-CTO] v3(F/G/E 累积)× v2(包H)在合并树汇成 v4。
    #    [门八第二发现 2026-08-30] 改了 insufficient_points 那句(原文承诺「题单不会丢」,
    #    实测去充值回来草稿全没了)⇒ 按本条规矩升 v5。
    #    [#58 2026-09-04] v7:新增 idempotency_key_missing / refresh_and_retry。
    #    [#62 2026-09-05] v8:新增 question_plan_superseded(confirm 时题单已被改版)。
    #    [#63 2026-09-05] v9:新增 five_card_summary_unavailable(五卡投影为空的回落说明)。
    #    [#97 2026-09-05] v10:新增动作 confirm_run_preview(open 态的正确出口)。
    #    [#157/#150 2026-09-08 · 42aebc640] v11:拒绝文案进 registry(题面超限等带占位符的模板)。
    #    后端 copy_registry.py 与前端 defensiveGeoCopy.ts 当笔已同步 v11,只有本行钉子没跟(09-28 补)。
    #    🔴 SOP(#85):升版**同笔**必跑 scripts/defgeo_census/emit_frontend_copy.py
    #    并跑 tests/defensive_geo_pkgh_2026_08_23/test_pkgh_cross_layer.py —— 只同步本行不够,
    #    #58 与 #62 两次都是只同步了本行,前端生成物漂到 v6 还上了线。
    #    它与 question_plan_not_runnable **刻意不复用**:处境不同(一开始选旧版 vs
    #    决定期间被改版),她要做的事也不同,且这条必须带「没有扣除任何算力」。
    #    新增也算升版事由 —— 见 copy_registry 顶部 v6 的先例(它就是为新增升的)。
    assert COPY_REGISTRY_VERSION == "defensive-geo-copy-v11"
    assert census()["version"] == COPY_REGISTRY_VERSION


# ══════════════════════ ① 漏译:分母从代码现取 ═════════════════════════════
def _live_run_states() -> set[str]:
    """所有**真的会被投影出来**的 runState —— 从 run_status 投影现算,不手抄。"""
    out = {project(s).run_state for s in known_statuses()}
    out.add(project("never_seen_value").run_state)   # quarantined 这一支
    return out


def _live_funding_states() -> set[str]:
    out = {project(s).funding_state for s in known_statuses()}
    out.add(project("never_seen_value").funding_state)
    return out


def test_run_state_denominator_is_alive():
    """判据可用性关:分母非空且含关键锚,否则下面是零圈通过。"""
    states = _live_run_states()
    assert len(states) >= 6, f"runState 分母只有 {states}"
    assert {"completed", "quarantined", "needs_action"} <= states


@pytest.mark.parametrize("state", sorted(_live_run_states()))
def test_every_live_run_state_has_user_copy(state):
    """🔴 U-2 主锁:一切可能上屏的 runState 必须有 userLabel。"""
    label = user_label("run_state", state)
    assert_public_copy_clean(label, field=f"run_state.{state}")


@pytest.mark.parametrize("state", sorted(_live_funding_states()))
def test_every_live_funding_state_has_user_copy(state):
    label = user_label("funding_state", state)
    assert_public_copy_clean(label, field=f"funding_state.{state}")


@pytest.mark.parametrize("policy", sorted(funding_policies()))
def test_every_funding_policy_has_user_copy(policy):
    """付款方要让她一眼看出「这笔钱谁出」。"""
    assert_public_copy_clean(user_label("funding_policy", policy), field=f"funding_policy.{policy}")


@pytest.mark.parametrize("code", ["open", "expired", "consumed"])
def test_every_preview_lifecycle_has_user_copy(code):
    assert_public_copy_clean(user_label("preview_lifecycle", code), field=f"lifecycle.{code}")


@pytest.mark.parametrize("mode", ["defensive", "offensive", "hybrid"])
def test_every_mode_has_the_adjudicated_name(mode):
    """U-1 裁定名逐字。废除的别名不许复活。"""
    expected = {"defensive": "先守住品牌", "offensive": "主动抢推荐", "hybrid": "两条线一起看"}
    assert user_label("mode", mode) == expected[mode]


def test_retired_aliases_are_not_reachable():
    """必须不命中:U-1 废除的三个别名不在任何译文里。

    「不在表里就取不到,取不到就上不了屏」—— 这条把那句话钉成判据。
    """
    all_copy = " ".join(
        v for table in census()["entries"].values() for v in table.values()
    )
    for retired in ("主动获客", "主动推荐", "一起做"):
        assert retired not in all_copy, f"废除别名 {retired!r} 复活了"


def test_missing_translation_raises_instead_of_falling_back():
    """🔴 漏译必须**抛**,不许回落成裸枚举。

    回落会让「忘了补译」表现成界面上一个英文单词 —— 那是 U-1 的验收红,
    却不会有任何东西因此变红。
    """
    with pytest.raises(CopyNotRegistered) as exc:
        user_label("run_state", "some_future_state")
    assert "验收红" in str(exc.value) or "补译" in str(exc.value)


def test_idempotency_conflict_is_deliberately_untranslated():
    """U-2 明确:``IDEMPOTENCY_CONFLICT`` 由前端静默处理,**永不上屏**。

    所以它刻意没有译文 —— 取不到就上不了屏,比写一句「请勿重复提交」更硬。
    """
    assert try_user_label("reason", "idempotency_conflict") is None
    assert try_user_label("reason", "IDEMPOTENCY_CONFLICT") is None


def test_preview_expired_copy_is_verbatim_from_the_adjudication():
    """U-2 逐字裁定过的两句,原样核对 —— 「没有扣除任何算力」这半句最要紧。"""
    assert user_label("reason", "preview_expired") == "刚才那一步已过期，请重新发起；没有扣除任何算力。"
    assert user_label("run_state", "quarantined") == "结果待平台核实，费用已冻结、不会多扣（无需操作）"


# ══════════════════════ ② 漏出内部词:判形态 ══════════════════════════════
def test_all_registered_copy_passes_the_leak_gate():
    """registry 自己先过一遍自己的门 —— 分母 = census 全量,不抽样。"""
    data = census()
    assert data["total"] >= 40, f"registry 只有 {data['total']} 条,疑似没抽全"
    for kind, table in data["entries"].items():
        for code, text in table.items():
            assert_public_copy_clean(text, field=f"{kind}.{code}")


@pytest.mark.parametrize("bad", [
    "体检状态:settlement_pending",       # snake_case 枚举直接上屏
    "错误码 PREVIEW_EXPIRED，请重试",     # SCREAMING_SNAKE
    "命中 H0-MONEY 规则",                # 决策等级
    "run_status 异常",                   # 内部列名
    "canonical_hash 不匹配",
    "billing_mode=exempt",
    "",                                  # 空 = 让她自己猜
    "   ",
    None,
    123,
])
def test_leak_gate_rejects_internal_shapes(bad):
    """🔴 每条「必须命中」都配一条「必须不命中」—— 这一组就是不命中那半。"""
    with pytest.raises(PublicCopyLeak):
        assert_public_copy_clean(bad, field="probe")


@pytest.mark.parametrize("good", [
    "正在体检",
    "结果待平台核实，费用已冻结、不会多扣（无需操作）",
    "刚才那一步已过期，请重新发起；没有扣除任何算力。",
    "少测几个问题",
])
def test_leak_gate_accepts_real_human_copy(good):
    """反向对照:真人话必须放行,否则上面那组可能只是**恒拒**(零判别力)。"""
    assert assert_public_copy_clean(good) == good


def test_gate_does_not_choke_on_legitimate_product_words():
    """再一条反向对照:含英文品牌/平台名的正常文案不许误伤。

    没有这条,以后有人为了让判据变绿而把门放宽,反而把真枚举放过去。
    """
    assert_public_copy_clean("豆包这次没答上来，不计入本次统计")
    assert_public_copy_clean("AI 体检已完成，共测了 8 个问题")


# ══════════════════════ 铁律:任何阻塞都自带解决方案 ═══════════════════════
_BLOCKING_REASONS = [
    "approval_required", "approval_rejected", "insufficient_points",
    "preview_expired", "already_consumed", "policy_unavailable",
    "question_plan_not_runnable", "snapshot_changed",
]


@pytest.mark.parametrize("reason", _BLOCKING_REASONS)
def test_every_blocking_reason_explains_and_points_somewhere(reason):
    """§0.5.6 铁律:「任何阻塞与错误必须自带解决方案」。

    人话解释非空 + 零内部词。**光有 code 没有解释 = 死路**。
    """
    explanation = user_label("reason", reason)
    assert_public_copy_clean(explanation, field=f"reason.{reason}")
    assert len(explanation) >= 8, f"{reason} 的解释太短,等于没解释:{explanation!r}"


@pytest.mark.parametrize("kind", [
    "wait", "view_result", "create_diagnosis_preview", "reduce_plan", "top_up",
    "request_approval", "request_budget_approval", "contact_owner", "contact_support",
    "view_existing_command", "review_identity", "change_plan", "review_question_plan",
])
def test_every_next_action_kind_has_a_button_label(kind):
    """typed nextAction 必须有「点哪」的字。只有 label 没有 target 是死动作;
    只有 target 没有 label 同样是死动作 —— 她看不见按钮上写什么。"""
    assert_public_copy_clean(user_label("action", kind), field=f"action.{kind}")
