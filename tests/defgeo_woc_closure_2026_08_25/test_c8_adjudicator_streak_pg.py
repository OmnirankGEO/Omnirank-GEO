"""C-8 · 审查员「连续 N 单」不许把同一单数两次(Codex 终审 P2)。

被测的那一格:049 是 append-only 审计表,**一单自动处置会落两行**带
``failure_cause`` 的记录(``decided`` + ``executed``/``execution_failed``;
崩在中间重跑还会更多)。而 ``same_cause_streak`` 逐**行**数 ⇒ 每单被数两次
⇒ 阈值 N 在**第 N/2 单**就触发全局升级(阈值配 6 时第 3 单就停止自动处置)。

判据两组,与工单逐字对应:
  · 同一单双行只计 1;
  · N-1 单不触发、N 单触发。

🔴 049 行由**生产** ``write_adjudication`` 写(它自己会双写 049 + audit),
   不由夹具直插:夹具自己造"两行"只能证明去重逻辑对我造的形状有效,
   证不了生产真的落两行 —— 而"生产落几行"正是这个缺陷的成因。
"""

from __future__ import annotations

import pytest

from services import settlement_adjudicator as adj
from tests.defgeo_woc_closure_2026_08_25.conftest import connect

CAUSE = "provider_timeout"
OTHER_CAUSE = "wallet_unreachable"


def _write_one_order(run_token: str, *, cause: str, decision: str = "commit") -> None:
    """走**生产**那条「先落 decided、执行后再落 executed」的顺序,产出两行。

    形态逐字照 ``_adjudicate_one``:它先 ``write_adjudication(phase="decided")``、
    提交后再 ``write_adjudication(phase="executed")``。
    """
    from db.connection import get_db

    with get_db() as conn:
        adj.write_adjudication(
            conn.cursor(), run_token=run_token, phase="decided", decision=decision,
            escalation_code=None, failure_cause=cause, frozen_points=100,
            evidence={"failure_cause": cause}, outcome="判据夹具")
    with get_db() as conn:
        adj.write_adjudication(
            conn.cursor(), run_token=run_token, phase="executed", decision=decision,
            escalation_code=None, failure_cause=cause, frozen_points=100,
            evidence={"failure_cause": cause}, outcome='{"ok": true}')


def _clear() -> None:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM diagnosis_settlement_adjudications")
        cur.execute("DELETE FROM diagnosis_settlement_audit")
        conn.commit()
    finally:
        conn.close()


def _streak(cause: str = CAUSE, *, current: str | None = None) -> int:
    conn = connect()
    try:
        cur = conn.cursor()
        n = adj.same_cause_streak(cur, cause, current_run_token=current)
        conn.rollback()
        return int(n)
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _clean_ledger():
    """🔴 分母清理:049 是 append-only,上一条判据攒下的行会让本条从半路开始数。

    本仓记过「共享队列判据先清分母」——不清的话「N-1 不触发」会被前一条
    判据的行推过阈值,而那种红与被测代码无关。
    """
    _clear()
    yield
    _clear()


# ══════════════════════════════════════════════════════════════════════════
# A. 同一单双行只计 1
# ══════════════════════════════════════════════════════════════════════════
def test_c8_00_production_really_writes_two_rows_per_order() -> None:
    """判别力前置:先证明**生产确实落两行**。

    没有这一条,下面"只计 1"可能只是因为生产其实只落一行 —— 那时去重
    逻辑从没被执行过,判据却全绿。缺陷的成因必须先被证明存在。
    """
    _write_one_order("wocC8-premise", cause=CAUSE)
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT phase FROM diagnosis_settlement_adjudications "
            " WHERE run_token=%s AND failure_cause IS NOT NULL ORDER BY id",
            ("wocC8-premise",))
        phases = [r["phase"] for r in cur.fetchall()]
        conn.rollback()
    finally:
        conn.close()
    assert phases == ["decided", "executed"], (
        f"生产没有落两行(实得 {phases})—— 本组判据失去判别力,缺陷成因已变")


def test_c8_01_one_order_counts_once_not_twice() -> None:
    """🔴 本项的本体判据:一单(两行)+ 当前这一单 = streak 2,不是 3。

    拆红:把去重(``seen`` / ``run_token``)摘掉 ⇒ 得 3 ⇒ 本条红。
    """
    _write_one_order("wocC8-a", cause=CAUSE)
    assert _streak() == 2, "同一单被数了两次"


def test_c8_02_the_current_order_is_never_double_counted() -> None:
    """本单**自己**已有 049 行时(崩在 decided 与 execute 之间的重跑),
    它只能被那个 ``+1`` 数一次。

    不排除当前单的话,同一个双计缺陷会换个地方复发:循环数它一次、
    ``+1`` 再数一次。
    """
    _write_one_order("wocC8-self", cause=CAUSE)
    assert _streak(current="wocC8-self") == 1, (
        "当前这一单被自己的历史行多数了一次")


def test_c8_03_a_different_cause_breaks_the_streak() -> None:
    """「连续」不是「总数」:中间夹一单别的原因就断。"""
    _write_one_order("wocC8-x1", cause=CAUSE)
    _write_one_order("wocC8-x2", cause=OTHER_CAUSE)
    _write_one_order("wocC8-x3", cause=CAUSE)
    # 最近的是 x3(同因)⇒ 数 1;再往前是 x2(异因)⇒ 断。+1 当前单 = 2
    assert _streak() == 2, "连续性判断被双计或被跨因串起来了"


def test_c8_04_the_streak_is_counted_from_the_newest_end() -> None:
    """🔴 [外选 EXTC-13]「连续」要从**最近**那一端数起,不是从最老那一端。

    改动前唯一的混因夹具 ``test_c8_03`` 的序列是 ``CAUSE, OTHER, CAUSE`` ——
    **回文**:从新数与从旧数都得 2,方向零判别力。把 ``ORDER BY id DESC``
    写成 ``ASC`` 不会让任何判据变红,而后果是:

      · 正在发生的系统性故障(最近 N 单同因、更早历史混因)永远数不满阈值
        ⇒ 升级熔断不触发,自动处置在故障中继续放行结算;
      · 反向,远古已解决的一串同因会永久顶着阈值;
      · ``LIMIT 50`` 还会把窗口锁死在最老 50 行上,表越长越假。

    非对称序列(按 id 升序:``OTHER, CAUSE, CAUSE``)把两端的答案分开:
      · 从新端数 —— CAUSE、CAUSE,遇 OTHER 断 ⇒ 2,+1 当前单 = **3**;
      · 从旧端数 —— 第一行就是 OTHER,当场断 ⇒ 0,+1 = **1**。

    拆红:``ORDER BY id DESC`` → ``ASC`` ⇒ 本条红(实得 1)。
    """
    _write_one_order("wocC8-asym1", cause=OTHER_CAUSE)    # 最老
    _write_one_order("wocC8-asym2", cause=CAUSE)
    _write_one_order("wocC8-asym3", cause=CAUSE)          # 最新
    assert _streak() == 3, (
        "连续同因不是从最近那一端数的 —— 正在发生的故障会数不满阈值,"
        "熔断不触发")


def test_c8_05_the_mirror_sequence_gives_the_other_answer() -> None:
    """镜像臂:同样三单、顺序反过来 ⇒ 答案必须**不同**。

    这一条与上一条合起来才证明"方向"这条轴真的被测到了:
    单看上一条,一个恒返 3 的实现同样全绿。
    """
    _write_one_order("wocC8-mir1", cause=CAUSE)           # 最老
    _write_one_order("wocC8-mir2", cause=CAUSE)
    _write_one_order("wocC8-mir3", cause=OTHER_CAUSE)     # 最新
    assert _streak() == 1, (
        "最近那一单是异因,连续同因应当当场断(只剩当前这一单)")


# ══════════════════════════════════════════════════════════════════════════
# B. N-1 不触发、N 触发
# ══════════════════════════════════════════════════════════════════════════
COEFFS = {"max_frozen_points": 100000, "same_cause_limit": 4}


def _evidence() -> dict:
    """一份**其它维度全部合格**的证据包 —— 让唯一变量是 streak。"""
    return {
        "failure_cause": CAUSE,
        "missing": [],
        "frozen_points": 500,
        "freeze_row": {"status": "frozen", "amount": 500},
        "product": {"diagnosis_id": 1, "articles": 1},
        "delivery_verdict": {"version": "diagnosis-min-sample-v1",
                             "outcome": "sufficient", "billable_ratio": 1.0},
    }


def test_c8_10_n_minus_one_orders_do_not_escalate() -> None:
    """N-1 单同因 ⇒ **不**升级(阈值 4 ⇒ 前 3 单照常自动处置)。

    改动前:落 2 单就有 4 行 ⇒ streak=5 ⇒ 已经触发。这一条钉住那个提前量。
    """
    for i in range(COEFFS["same_cause_limit"] - 2):       # 造 N-2 单历史
        _write_one_order(f"wocC8-n{i}", cause=CAUSE)
    streak = _streak(current="wocC8-now")                 # 历史 N-2 + 当前 1 = N-1
    assert streak == COEFFS["same_cause_limit"] - 1, streak

    verdict = adj.adjudicate(_evidence(), COEFFS, streak)
    assert verdict["decision"] != "escalate", verdict
    assert verdict["escalation_code"] is None, verdict


def test_c8_11_the_nth_order_escalates() -> None:
    """第 N 单同因 ⇒ 升级,理由码 ``same_cause_streak``。

    与上一条**只差一单**:边界两侧各一发,才证明阈值打在正确的位置上
    (只验触发那一侧的话,一个"永远触发"的实现同样全绿)。
    """
    for i in range(COEFFS["same_cause_limit"] - 1):       # 造 N-1 单历史
        _write_one_order(f"wocC8-m{i}", cause=CAUSE)
    streak = _streak(current="wocC8-now")                 # 历史 N-1 + 当前 1 = N
    assert streak == COEFFS["same_cause_limit"], streak

    verdict = adj.adjudicate(_evidence(), COEFFS, streak)
    assert verdict["decision"] == "escalate", verdict
    assert verdict["escalation_code"] == adj.ESCALATE_SAME_CAUSE_STREAK, verdict


def test_c8_12_escalated_rows_still_count_as_an_occurrence() -> None:
    """升级那一档**没有** decided 行,但它仍然是同因的一单。

    这一条解释了为什么去重按 ``run_token`` 而不是按 ``phase='decided'``:
    后者会把升级过的单整单漏掉,于是"连续"在真正出事的那几单上反而断链。
    """
    from db.connection import get_db

    with get_db() as conn:
        adj.write_adjudication(
            conn.cursor(), run_token="wocC8-esc", phase="escalated",
            decision="escalate", escalation_code=adj.ESCALATE_SAME_CAUSE_STREAK,
            failure_cause=CAUSE, frozen_points=100,
            evidence={"failure_cause": CAUSE}, outcome="判据夹具")
    assert _streak() == 2, "升级过的那一单没有被算成一次同因发生"
