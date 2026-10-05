"""包E · R1 返修 ①:钱向真值表的**值级**锁 + 两条危险方向臂。

═══════════════════════════════════════════════════════════════════════
🔴 这个文件为什么必须存在 —— Review 拿一发毒把它逼出来了
═══════════════════════════════════════════════════════════════════════
验收变异逐字是:``publish_settlement._DIRECTION`` 里 ``"unknown"`` 的钱向
从 ``hold_or_quarantine`` 改成 ``"release"``。含义是「供应商结果未知的单
自动退款」—— §12.1 点名禁止的那一格,也是本仓最贵的资金形态之一
(钱退了、稿子可能已经发出去了)。

我亲手注了这发毒(不是引用 Review 的话当证据):

  · 包E 51 条判据 —— **51 全绿,毒活着**;
  · 窗C ``test_wp6_settlement_pure.py`` —— 5 条红(它有逐字值级锁)。

所以本包不是"被别人守住了就等于验过了":包E 自己接的执行器、收敛器第 7 项、
以及客户面的 slot 投影**全都读这张表**,而包E 的判据一条都够不到它。
「别人的闸绿 ≠ 你被守住」。

═══════════════════════════════════════════════════════════════════════
🔴 三条臂,各打不同的一行
═══════════════════════════════════════════════════════════════════════
① **值级锁**:四个 ``hold_or_quarantine`` 行**逐行**钉死(参数化 = 一行一个
   节点,哪一格塌了一眼看得见),外加机械分母(一态不多一态不少)与
   反向对照(release/commit 两族仍各归各位 —— 证明这不是"全表都判 hold"
   的恒真断言)。
② **资金出口臂**:``release_exact`` / ``commit_exact`` 对这四个态必须**抛**。
   这条打的是钱真正出去的那道门,不只是一张 dict。Review 点名的
   ``_assert_direction`` 单元臂也在这里(公开出口 + 私有守卫各一发)。
③ **客户面臂**:``classify_command`` 对这四个态必须投影成 ``outcome_unknown``,
   **不许**是任何一种"已退款"态。这一格 w3 只覆盖了
   ``(unknown, pending_reconciliation, legal=False)`` 一个点 —— 而毒在
   ``legal_rule_hit=True`` 与 ``funding_state='released'`` 两个方向上都活着。
   所以这里走**笛卡尔积矩阵**,不是举例。
"""

from __future__ import annotations

import asyncio

import pytest

from services.defensive_geo.publish import publish_funding as pf
from services.defensive_geo.publish import publish_settlement as ps
from services.defensive_geo.publish import publish_slot as slot

#: 🔴 「未知/冲突」四态 —— 一分钱都不许动的那一族(§12.1)。
HOLD_STATES = ("rejected_unknown", "failed_unknown", "unknown", "conflict")

#: 反向对照用:确实**可以**退款 / 确实**可以**入账的两族。
RELEASE_STATES = ("rejected_no_effect", "failed_no_effect")
COMMIT_STATES = ("verified_published",)

#: 客户面上任何一种"已退款"的说法。未知态落进这里 = 对客户撒谎。
REFUNDED_SLOT_STATES = ("ordinary_no_effect_released", "legal_no_effect_released")

#: 判据里出现的 funding 取值 —— 含一个**本不该出现**的 ``released``:
#: 万一钱被别处错退了,客户面也不许因此改口说"这单没发、已退款"。
FUNDING_AXIS = ("frozen", "pending_reconciliation", "quarantined", "released")


# ══════════════════════════════════════════════════════════════════════════
# ① 值级锁 —— 四行逐行
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("state", HOLD_STATES)
def test_r1_01_hold_or_quarantine_row_is_pinned_value_by_value(state: str) -> None:
    """这一行的钱向逐字 = ``hold_or_quarantine``,且**显式不是** release/commit。"""
    direction = ps.settlement_direction(state)
    assert direction == "hold_or_quarantine", (
        f"canonical {state!r} 的钱向被改成了 {direction!r} —— "
        "结果未知的单只能 hold/quarantine(§12.1)"
    )
    assert direction != "release", f"{state!r} 变成了自动退款 —— 稿子可能已经发出去了"
    assert direction != "commit", f"{state!r} 变成了自动入账 —— 还没核实就扣钱"


def test_r1_02_direction_table_discriminates_between_the_three_families() -> None:
    """反向对照 + 机械分母:证明上面那四条不是「全表都判 hold」的恒真断言。"""
    directions = ps.census()["directions"]

    # 分母从 census 取,不手抄 —— 新加一个 canonical 态却忘了归类,这里会红。
    assert set(directions) == set(ps.CANONICAL_STATES)
    assert {s for s, d in directions.items() if d == "hold_or_quarantine"} == set(HOLD_STATES), (
        "hold_or_quarantine 这一族的成员变了 —— 一态不多一态不少"
    )

    # 反向对照:另外两族仍各归各位(否则"四个都是 hold"可能只是因为全表都是 hold)。
    for state in RELEASE_STATES:
        assert ps.settlement_direction(state) == "release", f"{state} 本该可以退款"
    for state in COMMIT_STATES:
        assert ps.settlement_direction(state) == "commit", f"{state} 本该可以入账"
    assert len({directions[s] for s in HOLD_STATES + RELEASE_STATES + COMMIT_STATES}) == 3, (
        "三族的钱向塌成了同一个值 —— 上面的断言就没有判别力了"
    )


# ══════════════════════════════════════════════════════════════════════════
# ② 资金出口臂 —— 钱真正出去的那道门
# ══════════════════════════════════════════════════════════════════════════
def _cmd(state: str) -> dict[str, object]:
    """一条**只带 canonical 事实**的命令。

    刻意不给 ``freeze_id``:方向自检在读句柄之前就该拦下来。
    如果哪天它变成"先看有没有物理腿、没有就放行",这里会红。
    """
    return {"canonical_publication_state": state}


@pytest.mark.parametrize("state", HOLD_STATES)
def test_r1_03_release_exact_refuses_every_unknown_state(state: str) -> None:
    """公开出口臂:``release_exact`` 对未知态必须抛,而不是"顺手退一下"。"""
    with pytest.raises(pf.FundingError) as err:
        asyncio.run(pf.release_exact(None, _cmd(state), reason="R1 危险方向臂"))
    assert state in str(err.value)


@pytest.mark.parametrize("state", HOLD_STATES)
def test_r1_04_commit_exact_refuses_every_unknown_state(state: str) -> None:
    """另一半:未知态也不许 commit —— 「既不 commit 也不 release」是两条,不是一条。"""
    with pytest.raises(pf.FundingError):
        asyncio.run(pf.commit_exact(None, _cmd(state), reason="R1 危险方向臂"))


@pytest.mark.parametrize("state", HOLD_STATES)
def test_r1_05_assert_direction_unit_arm_rejects_release(state: str) -> None:
    """Review 点名的单元臂:``_assert_direction(unknown, expected='release')`` 必抛。

    与上面两条的差别是**层次**:这条打私有守卫本身,上面两条打公开出口。
    两条都要 —— 出口哪天绕过守卫直接动钱,只有出口臂会红。
    """
    with pytest.raises(pf.FundingError):
        pf._assert_direction(_cmd(state), expected="release")     # noqa: SLF001


def test_r1_06_direction_guard_still_lets_the_legal_directions_through() -> None:
    """反向对照:该放行的必须放行 —— 否则"全都抛"也能让上面四条全绿。"""
    for state in RELEASE_STATES:
        pf._assert_direction(_cmd(state), expected="release")      # noqa: SLF001
    for state in COMMIT_STATES:
        pf._assert_direction(_cmd(state), expected="commit")       # noqa: SLF001

    # 方向对得上、但方向**串了**也必须抛(拿 commit 的态去 release)。
    with pytest.raises(pf.FundingError):
        pf._assert_direction(_cmd(COMMIT_STATES[0]), expected="release")   # noqa: SLF001
    with pytest.raises(pf.FundingError):
        pf._assert_direction(_cmd(RELEASE_STATES[0]), expected="commit")   # noqa: SLF001


# ══════════════════════════════════════════════════════════════════════════
# ③ 客户面臂 —— 未知单在她屏幕上不许显示成"已退款"
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("state", HOLD_STATES)
@pytest.mark.parametrize("legal_rule_hit", [False, True])
@pytest.mark.parametrize("funding_state", FUNDING_AXIS)
def test_r1_07_unknown_outcome_is_never_shown_as_refunded(
    state: str, legal_rule_hit: bool, funding_state: str,
) -> None:
    """笛卡尔积矩阵:未知态 × 法律位 × 钱位 —— 客户面恒为 ``outcome_unknown``。

    🔴 举例式判据在这里会漏:w3 只有 ``(unknown, pending_reconciliation, legal=False)``
       一个点,而毒在 ``legal_rule_hit=True``(→ ``legal_no_effect_released``)与
       ``funding_state='released'``(→ ``ordinary_no_effect_released``)两个方向上
       都活着。语义判别必须走矩阵。
    """
    got = slot.classify_command(
        canonical_publication_state=state,
        funding_state=funding_state,
        legal_rule_hit=legal_rule_hit,
    )
    assert got == "outcome_unknown", (
        f"({state}, legal={legal_rule_hit}, funding={funding_state}) 投影成了 {got!r}"
    )
    assert got not in REFUNDED_SLOT_STATES, (
        "结果未知的单在客户面显示成「已退款」—— 她会以为这篇没发,再下一单"
    )


def test_r1_08_refunded_slot_states_are_actually_reachable() -> None:
    """反向对照:那两个"已退款"态**确实存在且可达**。

    否则 ``got not in REFUNDED_SLOT_STATES`` 可能只是因为那两个字符串
    根本没人产出 —— 那条断言就是恒真的。
    """
    assert slot.classify_command(
        canonical_publication_state="failed_no_effect",
        funding_state="released", legal_rule_hit=False,
    ) == "ordinary_no_effect_released"
    assert slot.classify_command(
        canonical_publication_state="failed_no_effect",
        funding_state="released", legal_rule_hit=True,
    ) == "legal_no_effect_released"
    assert set(REFUNDED_SLOT_STATES) <= set(slot.SLOT_STATES)
