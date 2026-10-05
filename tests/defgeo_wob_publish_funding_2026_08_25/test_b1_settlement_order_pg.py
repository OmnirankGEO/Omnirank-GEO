"""B-1(= Codex 终审 P0-4)· 业务账不得早于物理结算落终态。

═══════════════════════════════════════════════════════════════════════
🔴 这一族判据钉的那句话:**billing 失败 ⇒ 零终态写入**
═══════════════════════════════════════════════════════════════════════
改之前 ``commit_exact`` / ``release_exact`` 是

    _assert_direction(...)
    _mark_settled(cur, command)          # ← 业务终态标记在前
    return await commit_freeze(...)      # ← 物理结算在后,返回值原样上抛

而 7 个调用点一个都没看返回值,拿到就无条件
``bump_status(funding_state="committed"/"released")``。
于是 billing 返 ``{"success": False, "reason": "未找到冻结记录"}``、返
``ambiguous``、或幂等返回**相反终态**时,command 照样被写成"已扣/已退",
物理腿却还停在 frozen —— 而且 ``settled_at`` 一落,收敛器 ④⑦ 与 Z-1 队列
就都不会再看这一条。两套账各自往前走。

**删掉修复即红**长什么样:把 ``_settle_exact`` 里那句
``if not _write_terminal(...)`` 之前的裁决拆掉(或把顺序换回去),
本文件里 4 条"零终态"臂全部转红 —— 因为终态会重新出现。
撕锁清单见 ``scripts/mutation_runner_wob_publish_funding.py``。

🔴 真值表**照抄诊断链**(``services/diagnosis_runs.py::_do_settlement``),
   不发明第二套。``test_b1_90`` 机械对账两边的谓词。
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

from services.defensive_geo.publish import publish_funding as _pf
from services.defensive_geo.publish import store as _store

from tests.defgeo_wob_publish_funding_2026_08_25 import _seed
from tests.defgeo_wob_publish_funding_2026_08_25._chain import (   # noqa: F401
    BILLING_SHAPES, accepted, base, client, confirmed_command, drain, drain_outbox,
    fake_billing, must_not_be_called, raising_billing, rejected, run,
)
from tests.defgeo_wob_publish_funding_2026_08_25.conftest import connect

ROOT = Path(__file__).resolve().parents[2]


# ══════════════════════════════════════════════════════════════════════════
# 场景搭建:把一条命令推到「canonical 已落、钱还冻着、等收敛」的形状
# ══════════════════════════════════════════════════════════════════════════
def _awaiting_settlement(client, name: str, *, canonical: str) -> dict:
    """confirm → 派发 → 落 canonical outcome,**停在结算之前**。

    停法:直接把 canonical state 写成终局,``settled_at`` 保持 NULL ——
    这正是收敛器 ④ 的候选形状(kill window ④:worker 落了 outcome 就被杀)。
    """
    drain()
    drain_outbox()
    ctx = confirmed_command(client, name)
    cid = ctx["publishCommandId"]
    _store_bump(cid, canonical_publication_state=canonical)
    return ctx


def _store_bump(publish_command_id: str, **updates) -> None:
    conn = connect()
    try:
        _store.bump_status(conn.cursor(), publish_command_id=publish_command_id, **updates)
        conn.commit()
    finally:
        conn.close()


def _row(publish_command_id: str) -> dict:
    conn = connect()
    try:
        row = _store.get_command_any_tenant(
            conn.cursor(), publish_command_id=publish_command_id)
        assert row is not None
        return dict(row)
    finally:
        conn.close()


def _reconcile_with_billing(monkeypatch, *, commit=None, release=None) -> dict:
    """跑一轮真收敛器,只把 billing 那两个原语换成给定形状。"""
    import middleware.billing as _billing
    from services.defensive_geo.publish import publish_worker as _worker

    if commit is not None:
        monkeypatch.setattr(_billing, "commit_freeze", commit)
    if release is not None:
        monkeypatch.setattr(_billing, "release_freeze", release)
    return run(_worker.reconcile_tick(limit=200))


# ══════════════════════════════════════════════════════════════════════════
# ① 四条「零终态」臂 —— 真库、真链、真收敛器,只替 billing 的返回形状
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("shape,expect_verdict", [
    ("not_found", "retry"),              # R3:success=False
    ("ambiguous", "manual"),             # R2:跨表撞号
    ("idempotent_released", "manual"),   # R2:想 commit 却已 released
])
def test_b1_01_commit_that_did_not_settle_writes_no_terminal(
        client, monkeypatch, shape: str, expect_verdict: str) -> None:
    """物理 commit 没成功 ⇒ ``settled_at`` 仍是 NULL、资金态一格没动。

    🔴 这是本单最核心的那句话。三种失败形状逐字取自 ``middleware/billing.py``
       的真返回(``BILLING_SHAPES``),不是判据发明的。
    """
    ctx = _awaiting_settlement(client, f"b1c_{shape}", canonical="verified_published")
    cid = ctx["publishCommandId"]
    before = _row(cid)
    assert before["settled_at"] is None, "场景没搭对:开局就已结算"
    assert str(before["funding_state"]) == "frozen", before["funding_state"]

    fake = fake_billing(shape)
    actions = _reconcile_with_billing(monkeypatch, commit=fake)

    after = _row(cid)
    assert fake.calls, "billing 根本没被调 —— 这条判据在测空气"
    assert after["settled_at"] is None, (
        f"billing 返回 {BILLING_SHAPES[shape]} 却落了 settled_at —— "
        "业务账又跑到物理结算前面去了(P0-4 原样复活)")
    assert str(after["funding_state"]) != "committed", (
        f"billing 没成功却把 fundingState 写成 committed:{after['funding_state']}")
    assert str(after["command_state"]) != "completed", after["command_state"]

    kinds = {a["kind"] for a in actions["items"] if a["commandId"] == cid}
    assert kinds == {f"settlement_{expect_verdict}"}, (
        f"裁决没被如实上报:{kinds}(期望 settlement_{expect_verdict})")


@pytest.mark.parametrize("shape,expect_verdict", [
    ("not_found", "retry"),
    ("ambiguous", "manual"),
    ("idempotent_committed", "manual"),  # R2:想 release 却已 committed
])
def test_b1_02_release_that_did_not_settle_writes_no_terminal(
        client, monkeypatch, shape: str, expect_verdict: str) -> None:
    """物理 release 没成功 ⇒ 不许写"已退款"。

    对客户面来说这一格更贵:写成 released 等于告诉她"钱退了",而钱还冻着。
    """
    ctx = _awaiting_settlement(client, f"b1r_{shape}", canonical="rejected_no_effect")
    cid = ctx["publishCommandId"]
    assert _row(cid)["settled_at"] is None

    fake = fake_billing(shape)
    actions = _reconcile_with_billing(monkeypatch, release=fake)

    after = _row(cid)
    assert fake.calls, "billing 根本没被调 —— 这条判据在测空气"
    assert after["settled_at"] is None, "退款没成功却落了 settled_at"
    assert str(after["funding_state"]) != "released", (
        f"退款没成功却写成 released:{after['funding_state']}")
    kinds = {a["kind"] for a in actions["items"] if a["commandId"] == cid}
    assert kinds == {f"settlement_{expect_verdict}"}, kinds


def test_b1_03_billing_exception_writes_no_terminal(client, monkeypatch) -> None:
    """billing **抛异常** ⇒ retry,零终态。抛出来的都还没定论,不许当失败收口。"""
    ctx = _awaiting_settlement(client, "b1_exc", canonical="verified_published")
    cid = ctx["publishCommandId"]
    fake = raising_billing(RuntimeError("注入:billing 侧连接断了"))
    _reconcile_with_billing(monkeypatch, commit=fake)

    after = _row(cid)
    assert fake.calls, "billing 没被调"
    assert after["settled_at"] is None
    assert str(after["funding_state"]) == "frozen", after["funding_state"]
    assert int(after["settlement_attempts"]) >= 1, (
        "retry 没记次数 —— 「commit 重试到第几次转人工」就没有分母了")
    assert "settlement_exception" in str(after["last_settlement_error"] or "")


def test_b1_04_manual_verdict_lands_in_the_z1_queue(client, monkeypatch) -> None:
    """R2(ambiguous)⇒ 隔离 + **进 Z-1 人工队列**,不是只写一行日志。

    "转人工"如果不落在队列的候选集里,那句话就只是个说法。
    """
    from services.defensive_geo.publish import settlement_review as _rv

    ctx = _awaiting_settlement(client, "b1_manual", canonical="verified_published")
    cid = ctx["publishCommandId"]
    _reconcile_with_billing(monkeypatch, commit=fake_billing("ambiguous"))

    after = _row(cid)
    assert str(after["funding_state"]) == "quarantined", after["funding_state"]
    assert str(after["command_state"]) == "quarantined", after["command_state"]
    assert after["settled_at"] is None, "隔离不是结算 —— 不许落 settled_at"
    assert _rv.is_queue_member(after), "隔离了却进不了 Z-1 队列 —— 那条出口是装饰"
    assert "ambiguous" in str(after["last_settlement_error"] or "")


def test_b1_05_manual_command_is_not_reattempted_next_round(
        client, monkeypatch) -> None:
    """已隔离的命令**下一轮不再被自动处置** —— 否则每一轮都重复告警/重复动作。

    平台腿尤其致命:它的 ``fundingState`` 是常量,掉不出 ④ 的候选集,
    只能靠 ``command_state <> 'quarantined'`` 这道闸收口。
    """
    ctx = _awaiting_settlement(client, "b1_noloop", canonical="verified_published")
    cid = ctx["publishCommandId"]
    _reconcile_with_billing(monkeypatch, commit=fake_billing("ambiguous"))
    assert str(_row(cid)["command_state"]) == "quarantined"

    frozen_handle = int(_row(cid)["freeze_id"])
    second = fake_billing("ambiguous")
    actions = _reconcile_with_billing(monkeypatch, commit=second)
    touched = [a for a in actions["items"] if a["commandId"] == cid]
    assert not touched, f"已隔离的命令又被处置了一次:{touched}"
    assert all(int(c.get("freeze_id") or -1) != frozen_handle for c in second.calls), (
        "已隔离的命令又去动了一次钱 —— 每一轮重复动作 = 重复告警 + 重复处置")


# ══════════════════════════════════════════════════════════════════════════
# ② 成功那一侧仍然成立(反向对照 —— 不许为了"不写终态"把正路也堵死)
# ══════════════════════════════════════════════════════════════════════════
def test_b1_10_real_commit_still_reaches_the_terminal(client) -> None:
    """真 billing、真冻结:commit 之后终态齐全(settled_at + committed + completed)。"""
    ctx = _awaiting_settlement(client, "b1_ok_c", canonical="verified_published")
    cid = ctx["publishCommandId"]
    before_wallet = _seed.wallet(_seed.TENANT_A)

    from services.defensive_geo.publish import publish_worker as _worker
    run(_worker.reconcile_tick(limit=200))

    after = _row(cid)
    assert after["settled_at"] is not None, "结算成功却没落 settled_at"
    assert str(after["funding_state"]) == "committed", after["funding_state"]
    assert str(after["command_state"]) == "completed", after["command_state"]
    after_wallet = _seed.wallet(_seed.TENANT_A)
    assert after_wallet["frozen_points"] < before_wallet["frozen_points"], (
        "冻结额没下来 —— 物理腿其实没动")


def test_b1_11_real_release_still_reaches_the_terminal(client) -> None:
    """真退款那一侧同理:钱真的回到可用余额。"""
    ctx = _awaiting_settlement(client, "b1_ok_r", canonical="rejected_no_effect")
    cid = ctx["publishCommandId"]
    before = _seed.wallet(_seed.TENANT_A)

    from services.defensive_geo.publish import publish_worker as _worker
    run(_worker.reconcile_tick(limit=200))

    after = _row(cid)
    assert after["settled_at"] is not None
    assert str(after["funding_state"]) == "released", after["funding_state"]
    wallet_after = _seed.wallet(_seed.TENANT_A)
    assert wallet_after["paid_points"] > before["paid_points"], (
        "退款没回到可用余额 —— 只改了状态没动钱")


def test_b1_12_same_direction_idempotent_is_settled_not_manual(
        client, monkeypatch) -> None:
    """幂等返回**同向**终态 = 「钱已经按这个方向动过了」⇒ 允许收口。

    这是 P0-4 要求④的反面:顺序反转不许把幂等语义打破。
    """
    ctx = _awaiting_settlement(client, "b1_idem_same", canonical="verified_published")
    cid = ctx["publishCommandId"]
    _reconcile_with_billing(monkeypatch, commit=fake_billing("idempotent_same_commit"))

    after = _row(cid)
    assert after["settled_at"] is not None, (
        "同向幂等被当成失败了 —— 钱已经扣过,业务终态必须跟上,否则永远收不了口")
    assert str(after["funding_state"]) == "committed", after["funding_state"]


def test_b1_13_already_settled_command_is_a_noop(client, monkeypatch) -> None:
    """已结算的命令再调结算原语 = **no-op**:不动钱、不改状态。"""
    ctx = _awaiting_settlement(client, "b1_noop", canonical="verified_published")
    cid = ctx["publishCommandId"]
    from services.defensive_geo.publish import publish_worker as _worker
    run(_worker.reconcile_tick(limit=200))
    settled_row = _row(cid)
    assert settled_row["settled_at"] is not None

    import middleware.billing as _billing
    monkeypatch.setattr(_billing, "commit_freeze",
                        raising_billing(AssertionError("no-op 却去动了钱")))
    conn = connect()
    try:
        cur = conn.cursor()
        outcome = run(_pf.commit_exact(cur, settled_row, reason="重复结算探针"))
        conn.commit()
    finally:
        conn.close()
    assert outcome.settled and outcome.reason == "already_settled_noop", outcome
    again = _row(cid)
    assert again["settled_at"] == settled_row["settled_at"], "重复结算改写了终态时间戳"
    assert int(again["status_version"]) == int(settled_row["status_version"]), (
        "no-op 却递增了 statusVersion —— 那不是 no-op")


# ══════════════════════════════════════════════════════════════════════════
# ③ statusVersion CAS(要求③:两个相反动作不得各自基于旧状态推进)
# ══════════════════════════════════════════════════════════════════════════
def test_b1_20_terminal_write_is_status_version_cas(client) -> None:
    """拿**过期的** statusVersion 去结算 ⇒ CAS 0 行 ⇒ retry,不写终态。"""
    ctx = _awaiting_settlement(client, "b1_cas", canonical="verified_published")
    cid = ctx["publishCommandId"]
    stale = _row(cid)
    _store_bump(cid, status_reason="别人先动了一手")     # statusVersion +1
    assert int(_row(cid)["status_version"]) > int(stale["status_version"])

    conn = connect()
    try:
        cur = conn.cursor()
        outcome = run(_pf.commit_exact(
            cur, stale, reason="拿旧版本结算", terminal_command_state="completed"))
        conn.commit()
    finally:
        conn.close()

    assert outcome.verdict == "retry" and outcome.reason == "terminal_cas_missed", outcome
    after = _row(cid)
    assert after["settled_at"] is None, "CAS 落空却仍然落了终态标记"
    assert str(after["funding_state"]) == "frozen", after["funding_state"]


def test_b1_21_admin_review_refuses_a_stale_disposition(client) -> None:
    """Z-1 人工处置拿旧状态推进 ⇒ typed 拒绝,**动钱之前**就被挡。

    Codex 复现的分裂:两个 admin 各自读到同一版,一个 commit 一个 release,
    两条都基于自己读到的旧状态往前走。
    """
    from services.defensive_geo.publish import settlement_review as _rv

    ctx = _awaiting_settlement(client, "b1_review_cas", canonical="unknown")
    cid = ctx["publishCommandId"]
    _store_bump(cid, funding_state="pending_reconciliation",
                command_state="settlement_pending")
    stale = _row(cid)
    _store_bump(cid, status_reason="另一个处置人先动了一手")

    conn = connect()
    try:
        cur = conn.cursor()
        with pytest.raises(_rv.ReviewError) as err:
            _rv._cas_bump(                                # noqa: SLF001
                cur, stale, admin_user_id=9703,
                canonical_publication_state="verified_published",
                status_reason="拿旧状态推进")
        conn.rollback()
    finally:
        conn.close()
    assert "刷新" in str(err.value) or "重新裁定" in str(err.value), str(err.value)
    after = _row(cid)
    assert after["settled_at"] is None, "被拒绝的处置却把账落了终态"
    assert str(after["canonical_publication_state"]) == "unknown", (
        "CAS 落空了却仍然把 canonical state 推了过去")


def test_b1_22_two_opposite_admin_actions_cannot_both_win(client) -> None:
    """相反的两个人工处置:**至多一个**成功,账本方向不分裂。"""
    from services.defensive_geo.publish import settlement_review as _rv

    ctx = _awaiting_settlement(client, "b1_review_split", canonical="unknown")
    cid = ctx["publishCommandId"]
    _store_bump(cid, funding_state="pending_reconciliation",
                command_state="settlement_pending")
    ok: list[str] = []
    for action in ("admin_commit", "admin_release"):
        conn = connect()
        try:
            cur = conn.cursor()
            try:
                run(_rv.apply_admin_action(
                    cur, publish_command_id=cid, action=action,
                    admin_user_id=9703, reason=None))
                conn.commit()
                ok.append(action)
            except _rv.ReviewError:
                conn.rollback()
        finally:
            conn.close()
    assert len(ok) == 1, f"两个相反处置成功了 {ok} —— 账本方向会分裂"
    after = _row(cid)
    assert str(after["funding_state"]) in ("committed", "released"), after["funding_state"]
    assert after["settled_at"] is not None, "成功的那一个没落终态标记"
    # 方向与实际动的那笔钱一致(不是"随便落一格")
    expect = "committed" if ok[0] == "admin_commit" else "released"
    assert str(after["funding_state"]) == expect, (
        f"处置 {ok[0]} 却落成 {after['funding_state']} —— 方向分裂")


# ══════════════════════════════════════════════════════════════════════════
# ④ 重试上限(不许把 P0-4 的 retry 变成另一种「无限挂钱」)
# ══════════════════════════════════════════════════════════════════════════
def test_b1_30_commit_retry_has_a_ceiling(client, monkeypatch) -> None:
    """commit 一直失败 ⇒ 达 ``SETTLE_MAX_ATTEMPTS`` 转人工,不无限重试。"""
    ctx = _awaiting_settlement(client, "b1_ceiling", canonical="verified_published")
    cid = ctx["publishCommandId"]
    for _ in range(_pf.SETTLE_MAX_ATTEMPTS + 1):
        if str(_row(cid)["command_state"]) == "quarantined":
            break
        _reconcile_with_billing(monkeypatch, commit=fake_billing("not_found"))

    after = _row(cid)
    assert str(after["command_state"]) == "quarantined", (
        f"commit 重试 {after['settlement_attempts']} 次仍没转人工 —— "
        "那是另一种「无限延期挂钱」")
    assert after["settled_at"] is None
    assert f"commit_attempts>={_pf.SETTLE_MAX_ATTEMPTS}" in str(
        after["last_settlement_error"] or "")


def test_b1_31_release_retry_has_no_ceiling(client, monkeypatch) -> None:
    """release **不设上限**(照抄诊断链):拦住退款只会让钱更回不来。"""
    ctx = _awaiting_settlement(client, "b1_rel_noceil", canonical="rejected_no_effect")
    cid = ctx["publishCommandId"]
    for _ in range(_pf.SETTLE_MAX_ATTEMPTS + 2):
        _reconcile_with_billing(monkeypatch, release=fake_billing("not_found"))

    after = _row(cid)
    assert int(after["settlement_attempts"]) > _pf.SETTLE_MAX_ATTEMPTS, (
        f"退款只试了 {after['settlement_attempts']} 次就停了")
    assert str(after["command_state"]) != "quarantined", (
        "release 被设了上限 —— 与诊断链语义不一致(那边 release 不设上限)")
    assert after["settled_at"] is None


# ══════════════════════════════════════════════════════════════════════════
# ⑤ 结构:真值表照抄诊断链 · 闭集 · 终态单写点
# ══════════════════════════════════════════════════════════════════════════
def test_b1_90_truth_table_matches_the_diagnosis_chain() -> None:
    """发布链的裁决谓词与诊断链 ``_do_settlement`` **逐条同构**。

    🔴 「照抄那套语义,不发明第二套」是工单逐字要求。这里机械对账四条:
       ambiguous / idempotent 冲突 / success is True / 其余 retry。
    """
    import services.diagnosis_runs as _dr

    diag = inspect.getsource(_dr._do_settlement)          # noqa: SLF001
    pub = inspect.getsource(_pf._settle_exact)            # noqa: SLF001
    for probe, why in (
        ('r.get("ambiguous")', "R2 ambiguous → 转人工"),
        ('r.get("idempotent")', "幂等冲突守卫"),
        ('r.get("success") is True', "R1 只认显式 True"),
        ('intent == "commit" and idem_status == "released"', "想 commit 已 released"),
        ('intent == "release" and idem_status == "committed"', "想 release 已 committed"),
    ):
        assert probe in diag, f"探针写废了:诊断链里找不到 {probe!r}"
        assert probe in pub, (
            f"发布链缺 {why} 那一格({probe!r})—— 工单要求照抄诊断链真值表")


def test_b1_91_verdicts_are_a_closed_set() -> None:
    """裁决是闭集;``SettlementOutcome.settled`` 只对 ``settled`` 为真。"""
    assert set(_pf.SETTLEMENT_VERDICTS) == {"settled", "retry", "manual"}
    for v in _pf.SETTLEMENT_VERDICTS:
        out = _pf.SettlementOutcome(v, "commit", "x", {})
        assert out.settled is (v == "settled"), v
    census = _pf.census()
    assert census["settlementVerdicts"] == list(_pf.SETTLEMENT_VERDICTS)
    assert census["terminalFundingState"] == {"commit": "committed", "release": "released"}


def test_b1_92_mark_settled_never_precedes_billing() -> None:
    """AST:``_settle_exact`` 里 billing 那一跳**在**终态写入之前。

    这是顺序本身的结构锁 —— 行为判据(01/02/03)钉的是后果,
    这一条钉的是"顺序换回去"这个动作本身。
    """
    import textwrap

    src = textwrap.dedent(inspect.getsource(_pf._settle_exact))   # noqa: SLF001
    tree = ast.parse(src)
    def _calls(scope, name: str) -> list[int]:
        out = []
        for n in ast.walk(scope):
            if isinstance(n, ast.Call):
                fn = (n.func.attr if isinstance(n.func, ast.Attribute)
                      else getattr(n.func, "id", ""))
                if fn == name:
                    out.append(n.lineno)
        return out

    billing = _calls(tree, "primitive")
    terminal = _calls(tree, "_write_terminal")
    assert len(billing) == 1, f"探针没找到唯一那一跳 billing:{billing}"
    assert terminal, "探针没找到终态写入 —— 写废了"

    # 🔴 有一处终态写入是**合法地**在 billing 之前的:``freeze_id is None``
    #    那条早退分支(0 元 / 平台腿没产生冻结行)—— 那条路上根本没有 billing
    #    可以失败。把它单独识别出来,剩下的必须全在 billing 之后。
    no_leg: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "freeze_id" in ast.dump(node.test):
            no_leg.update(_calls(node, "_write_terminal"))
    assert len(no_leg) == 1, (
        f"没识别出「无物理腿」那条早退分支({no_leg})—— 探针写废了")
    on_billing_path = [ln for ln in terminal if ln not in no_leg]
    assert on_billing_path, "有物理腿的那条路上一处终态写入都没有 —— 探针写废了"
    assert min(on_billing_path) > billing[0], (
        f"终态写入(行 {sorted(on_billing_path)})跑到了 billing"
        f"(行 {billing[0]})前面 —— 那正是 P0-4")


def test_b1_93_terminal_funding_states_agree_across_modules() -> None:
    """``store`` 与 ``publish_funding`` 两份终态清单必须逐字相等。"""
    assert set(_pf._TERMINAL_FUNDING_STATE.values()) == set(   # noqa: SLF001
        _store.SETTLEMENT_TERMINAL_FUNDING_STATES)
    with pytest.raises(_store.StoreError):
        _store.write_settlement_terminal(
            None, publish_command_id="x", funding_state="quarantined")


def test_b1_94_settlement_attempts_has_exactly_one_writer() -> None:
    """``settlement_attempts`` / ``last_settlement_error`` 只有一条 SQL 写点。"""
    pkg = ROOT / "services" / "defensive_geo"
    writers: set[str] = set()
    for path in sorted(pkg.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            blob = "\n".join(
                n.value for n in ast.walk(fn)
                if isinstance(n, ast.Constant) and isinstance(n.value, str))
            # 🔴 锚打在**语义**上,不打在 `+ 1` 这个字面量上:
            #    步长后来变成参数(转人工那一手 increment=False),
            #    钉死字面量的锁会在一次正当改动上红,而那不是缺陷。
            if re.search(r"SET\s+settlement_attempts\s*=", blob):
                writers.add(f"{rel}::{fn.name}")
    assert writers == {
        "services/defensive_geo/publish/store.py::bump_settlement_attempt",
    }, f"重试计数出现第二个写点:{sorted(writers)}"
    # 通用直写的门也不许开
    assert "settlement_attempts" not in inspect.getsource(_store.bump_status), (
        "bump_status 的 allowed 里出现了 settlement_attempts —— "
        "「重试到第几次」会有第二份真相")


def _settlement_call_sites(src: str) -> tuple[list[str], list[str]]:
    """机械枚举结算调用点,并判「返回值有没有被真的用起来」。

    判别(通用,不靠字面量):
      · 裸表达式语句 ``await _funding.commit_exact(...)`` ⇒ **没消费**;
      · ``return await ...``                              ⇒ 交上层判,算消费;
      · ``x = await ...`` 且 ``x`` 在同一函数体里被**读**过 ⇒ 消费;
        赋了值却再没人读 ⇒ 与丢掉等价,算没消费。
    """
    tree = ast.parse(src)
    sites: list[str] = []
    unconsumed: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        loads = {n.id for n in ast.walk(fn)
                 if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        parent: dict[int, ast.AST] = {}
        for node in ast.walk(fn):
            for child in ast.iter_child_nodes(node):
                parent[id(child)] = node
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("commit_exact", "release_exact")):
                continue
            tag = f"{fn.name}:{node.lineno}"
            sites.append(tag)
            stmt = parent.get(id(node))
            while stmt is not None and not isinstance(stmt, ast.stmt):
                stmt = parent.get(id(stmt))
            if isinstance(stmt, ast.Return):
                continue                                   # 交上层判
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                targets = ([stmt.target] if isinstance(stmt, ast.AnnAssign)
                           else list(stmt.targets))
                names = {t.id for t in targets if isinstance(t, ast.Name)}
                if names & loads:
                    continue                               # 被读过 = 真消费
            unconsumed.append(tag)
    return sorted(set(sites)), sorted(set(unconsumed))


def test_b1_95_every_settlement_call_site_consumes_the_verdict() -> None:
    """🔴 **机械枚举**每一个 ``commit_exact`` / ``release_exact`` 调用点,
    逐个核「有没有把返回值用起来」。

    ═══════════════════════════════════════════════════════════════════════
    这一条是工单 B-1 逐字要求的那句「全部调用方逐一过一遍」
    ═══════════════════════════════════════════════════════════════════════
    P0-4 的形态不是"某一个调用点忘了判",而是**七个全都没判**:
    ``await _funding.commit_exact(...)`` 是个裸表达式语句,返回值当场丢掉。

    分母**从源码机械枚举**,不手抄清单 —— 本仓记过:手写分母漏掉的那一项
    不会让任何判据变红。新增第八个调用点却忘了消费 verdict,这条当场红。
    """
    pkg = ROOT / "services" / "defensive_geo"
    total: list[str] = []
    bad: list[str] = []
    for path in sorted(pkg.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        sites, unconsumed = _settlement_call_sites(path.read_text(encoding="utf-8"))
        total += [f"{rel}::{t}" for t in sites]
        bad += [f"{rel}::{t}" for t in unconsumed]
    assert len(total) >= 7, f"只扫到 {len(total)} 个结算调用点 —— 探针写废了({total})"
    assert bad == [], (
        f"这些结算调用点把 verdict 丢掉了:{bad} —— "
        "P0-4 的形态就是「拿到返回值不看」:billing 返 success=false 也照写终态")


def test_b1_96_verdict_census_probe_is_alive() -> None:
    """反向对照:三种"没消费"的形态**都**要被抓到;正样本不许误判。

    合成样本 —— 判据不碰真源码(碰源码的是撕锁 runner)。
    """
    good_branch = (
        "async def f(cur, row):\n"
        "    outcome = await _funding.commit_exact(cur, row, reason='x')\n"
        "    if not outcome.settled:\n"
        "        return None\n"
        "    return outcome\n"
    )
    good_helper = (
        "async def f(cur, row, out):\n"
        "    outcome = await _funding.release_exact(cur, row, reason='x')\n"
        "    if not _record_unsettled(cur, row, outcome, out, 'frozen'):\n"
        "        return None\n"
    )
    good_return = (
        "async def f(cur, row):\n"
        "    return await _funding.commit_exact(cur, row, reason='x')\n"
    )
    bad_naked = (
        "async def f(cur, row):\n"
        "    await _funding.commit_exact(cur, row, reason='x')\n"
        "    return None\n"
    )
    bad_assigned_unused = (
        "async def f(cur, row):\n"
        "    outcome = await _funding.commit_exact(cur, row, reason='x')\n"
        "    return None\n"
    )
    for src, name in ((good_branch, "分支消费"), (good_helper, "交 helper 消费"),
                      (good_return, "return 交上层")):
        assert _settlement_call_sites(src)[1] == [], f"正样本({name})被误判成没消费"
    for src, name in ((bad_naked, "裸表达式"), (bad_assigned_unused, "赋了值没人读")):
        sites, bad = _settlement_call_sites(src)
        assert sites and bad, f"负样本({name})没被抓到 —— 探针写废了,上一条是空气"
