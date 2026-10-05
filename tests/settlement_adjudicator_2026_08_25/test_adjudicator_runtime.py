"""结算 AI 审查员 · 运行时判据(2026-08-25 新规:入口真执行)。

动钱的臂**全部从真 cron 面 `run_diagnosis_sweep()` 驱动** —— 不是直调
`run_adjudication_tick()`。理由是本仓 2026-08-25 那次上线事故:
92 条判据 + 27 发变异全绿,调用点一条 `NameError` 照样每单必炸,
因为**没有一条判据真的执行过入口**。

真 PG、真 `point_freezes` 冻结行、真 `verify_and_resolve_manual` CAS。
"""
from __future__ import annotations

import asyncio

import pytest

from tests.settlement_adjudicator_2026_08_25._world import (
    break_freeze_handle,
    clear_coefficients,
    manual_world,
    observe,
    seed_streak,
    set_coefficients,
    wallet,
)


def sweep():
    """跑一次**真** cron 面。审查员挂在它第 5 段。"""
    from services.diagnosis_runs import run_diagnosis_sweep
    return asyncio.run(run_diagnosis_sweep())


def _adj(o, phase):
    return [a for a in o["adjudications"] if a["phase"] == phase]


# ===========================================================================
# 双向 · 该动的动
# ===========================================================================

def test_product_present_and_within_limit_is_auto_committed(live_server, live_dsn):
    """证据齐 + 限额内 + 产物在库 ⇒ 自动判交付,run 迁出 settlement_manual。

    **钱不在这一步动** —— 审查员只把 run CAS 到 commit_pending,
    金额与已履约比例由 sweeper 既有的 `_partial_commit_points` 在后续 tick 算。
    """
    set_coefficients(live_dsn, max_frozen_points=10_000)
    w = manual_world(live_dsn, frozen=650, with_product=True)

    sweep()
    o = observe(live_dsn, w["run_token"])

    assert o["run"]["run_status"] == "commit_pending", o["run"]
    assert [a["decision"] for a in _adj(o, "decided")] == ["commit"], o["adjudications"]
    assert [a["decision"] for a in _adj(o, "executed")] == ["commit"], o["adjudications"]
    # 配对的必须不命中:自动处置**不许**产出升级记录
    assert _adj(o, "escalated") == [], o["adjudications"]
    # 规则版本必须落库(退款争议要能答"哪版规则判的")
    assert all(a["rule_version"] for a in o["adjudications"]), o["adjudications"]


def test_no_product_is_auto_released(live_server, live_dsn):
    """零交付证据 ⇒ 全额释放(CAS 到 release_pending)。"""
    set_coefficients(live_dsn, max_frozen_points=10_000)
    w = manual_world(live_dsn, frozen=650, with_product=False)

    sweep()
    o = observe(live_dsn, w["run_token"])

    assert o["run"]["run_status"] == "release_pending", o["run"]
    assert [a["decision"] for a in _adj(o, "executed")] == ["release"], o["adjudications"]


def test_committed_run_reaches_true_terminal_and_notifies_the_user(live_server, live_dsn):
    """端到端:审查员判交付 → 下一次 sweep 真结算到终态 → **用户拿到通知**。

    这条钉住的是「自动处置完成 → 用户侧闭环」。用户通知**不是本包发的** ——
    既有 `_cas()` 在终态同事务里发(本包一个字没改),本条是那条既有接线的**锁**:
    哪天它被拆了,承诺给用户的"回到这里就能看到结果"会静默失效而没人知道。
    """
    set_coefficients(live_dsn, max_frozen_points=10_000)
    w = manual_world(live_dsn, frozen=650, with_product=True)

    sweep()                                   # 审查员:settlement_manual → commit_pending
    assert observe(live_dsn, w["run_token"])["run"]["run_status"] == "commit_pending"
    sweep()                                   # sweeper:commit_pending → 真终态
    o = observe(live_dsn, w["run_token"])

    assert o["run"]["run_status"] in ("committed", "released"), o["run"]
    users = [e for e in o["outbox"] if e["recipient_kind"] == "user"]
    assert users, "自动处置到真终态却零用户通知 —— UX 单承诺的『回到这里就能看到结果』断了:%r" % (o["outbox"],)


# ===========================================================================
# 双向 · 不该动的绝不动(升级三触发,各一条正臂)
# ===========================================================================

def _assert_untouched(o, w, before):
    """升级臂的公共断言:**零资金变动 + 状态没迁走**。"""
    assert o["run"]["run_status"] == "settlement_manual", o["run"]
    assert o["freeze"]["status"] == "frozen", "升级不许动钱:%r" % (o["freeze"],)
    assert _adj(o, "decided") == [], "升级臂绝不能产出 decided 行:%r" % (o["adjudications"],)
    assert _adj(o, "executed") == [], "升级臂绝不能执行:%r" % (o["adjudications"],)


def test_over_limit_escalates_without_touching_money(live_server, live_dsn):
    """超限额 ⇒ 不碰 CAS、不动钱、升级。门控落在**冻结额**上,commit/release 两向同门。"""
    set_coefficients(live_dsn, max_frozen_points=100)
    w = manual_world(live_dsn, frozen=650, with_product=True)   # 有产物 = 本该判 commit
    before = wallet(live_dsn, w["uid"])

    sweep()
    o = observe(live_dsn, w["run_token"])

    _assert_untouched(o, w, before)
    esc = _adj(o, "escalated")
    assert [a["escalation_code"] for a in esc] == ["over_limit"], o["adjudications"]
    assert wallet(live_dsn, w["uid"]) == before, "升级臂钱包不许有任何变化"


def test_incomplete_evidence_escalates(live_server, live_dsn):
    """FreezeHandle 打断 ⇒ 证据不全 ⇒ 升级,不猜。"""
    set_coefficients(live_dsn, max_frozen_points=10_000)
    w = manual_world(live_dsn, frozen=650, with_product=True)
    break_freeze_handle(live_dsn, w["run_token"])

    sweep()
    o = observe(live_dsn, w["run_token"])

    assert o["run"]["run_status"] == "settlement_manual", o["run"]
    assert [a["escalation_code"] for a in _adj(o, "escalated")] == ["evidence_incomplete"], \
        o["adjudications"]
    assert _adj(o, "executed") == [], o["adjudications"]


def test_same_cause_streak_escalates_as_a_bug_not_a_refund(live_server, live_dsn):
    """同一失败原因连续 ≥N ⇒ 停止自动处置、按 bug 报。

    这一条是**反直觉但最重要**的:这单本身证据齐、限额内、有产物,单看它应该自动交付。
    但它是同一个故障的第 N 单 —— 系统性故障不许被一单一退(或一单一交付)悄悄放血。
    """
    cause = "streaky_failure"
    set_coefficients(live_dsn, max_frozen_points=10_000, same_cause_limit=3)
    seed_streak(live_dsn, failure_cause=cause, n=3)
    w = manual_world(live_dsn, frozen=650, with_product=True, failure_cause=cause)

    sweep()
    o = observe(live_dsn, w["run_token"])

    assert o["run"]["run_status"] == "settlement_manual", o["run"]
    assert [a["escalation_code"] for a in _adj(o, "escalated")] == ["same_cause_streak"], \
        o["adjudications"]
    assert o["freeze"]["status"] == "frozen", "系统性故障档更不许动钱"


# ===========================================================================
# fail-closed / 幂等 / 通知
# ===========================================================================

def test_without_coefficients_the_adjudicator_does_nothing(live_server, live_dsn):
    """**未配系数 ⇒ 零动作**(fail-closed)。

    装上这个包但没配后台系数的生产环境必须是**零行为变化** ——
    而不是"用一个代码里写死的默认值去动别人的钱"。
    """
    clear_coefficients(live_dsn)
    w = manual_world(live_dsn, frozen=650, with_product=True)

    sweep()
    o = observe(live_dsn, w["run_token"])

    assert o["run"]["run_status"] == "settlement_manual", o["run"]
    assert o["adjudications"] == [], "未配系数却产生了裁定记录:%r" % (o["adjudications"],)
    assert o["freeze"]["status"] == "frozen"


def test_escalation_is_idempotent_second_pass_does_nothing(live_server, live_dsn):
    """同单跑两遍:第二遍**零动作零通知**(WO 幂等要求)。

    没有这道判断的话,卡住的单会每 2 分钟被重新升级一次:
    裁定表被刷爆 + admin 每 2 分钟收一次同样的通知。
    """
    set_coefficients(live_dsn, max_frozen_points=100)
    w = manual_world(live_dsn, frozen=650, with_product=True)

    sweep()
    first = observe(live_dsn, w["run_token"])
    sweep()
    second = observe(live_dsn, w["run_token"])

    assert len(first["adjudications"]) == 1, first["adjudications"]
    assert second["adjudications"] == first["adjudications"], \
        "第二遍又落了一行裁定:%r" % (second["adjudications"],)
    assert second["outbox"] == first["outbox"], \
        "第二遍又推了一次通知:%r" % (second["outbox"],)


def test_escalation_notifies_admins_only_never_the_user(live_server, live_dsn):
    """升级 ⇒ admin 扇出;**绝不推给用户** —— 升级理由里带内部系数(限额/阈值)。"""
    set_coefficients(live_dsn, max_frozen_points=100)
    w = manual_world(live_dsn, frozen=650, with_product=True)

    sweep()
    o = observe(live_dsn, w["run_token"])

    esc_events = [e for e in o["outbox"]
                  if e["terminal_state"] == "settlement_manual_adjudicator_escalated"]
    assert esc_events, "升级了却零 admin 通知:%r" % (o["outbox"],)
    assert all(e["recipient_kind"] == "admin" for e in esc_events), \
        "升级通知推给了非 admin:%r" % (esc_events,)


def test_auto_disposal_does_not_page_admins(live_server, live_dsn):
    """配对的必须不命中:**自动处置项不推 admin**(告警极简铁律:只有要人动手的才响)。"""
    set_coefficients(live_dsn, max_frozen_points=10_000)
    w = manual_world(live_dsn, frozen=650, with_product=True)

    sweep()
    o = observe(live_dsn, w["run_token"])

    assert _adj(o, "executed"), "这一臂本该自动处置:%r" % (o["adjudications"],)
    esc_events = [e for e in o["outbox"]
                  if e["terminal_state"] == "settlement_manual_adjudicator_escalated"]
    assert esc_events == [], "自动处置却惊动了 admin:%r" % (esc_events,)


# ===========================================================================
# Review 点名的两条
# ===========================================================================

def test_double_write_is_atomic_049_failure_rolls_back_the_audit_row(live_server, live_dsn,
                                                                    monkeypatch):
    """注入判据:049 写失败 ⇒ 既有 audit 行**同回滚**(两轨要么都在要么都不在)。

    毒下在 049 的 INSERT 之后、audit 之前是没用的(那只证明顺序);
    要证明**同事务**,必须让第二写失败并检查第一写有没有留下来 —— 所以毒下在
    `_write_settlement_audit` 上,再回头看 049 那行在不在。
    """
    set_coefficients(live_dsn, max_frozen_points=10_000)
    w = manual_world(live_dsn, frozen=650, with_product=True)

    import services.diagnosis_runs as dr

    # 🔴 第一版这里是"无条件 raise",**零区分力**:把 `cur=cur` 改成 `cur=None`
    #    (即改成独立连接 best-effort 写)之后它照样 raise、照样回滚,判据照样绿 ——
    #    撕锁实测 U12 存活抓出来的。要证明**同事务**,毒必须能分辨传没传游标:
    #      传了真游标 → raise(模拟同事务写失败)⇒ 调用方 with 块回滚 ⇒ 049 行不该在;
    #      传的是 None → 什么都不做(模拟既有实现里 best-effort 吞异常)⇒ 049 行会留下 ⇒ 判据红。
    seen = []

    def _audit_probe(run_token, operator, action, detail, cur=None):
        seen.append(cur is not None)
        if cur is None:
            return                       # 独立连接档:既有实现是 best-effort 吞掉
        raise RuntimeError("injected: same-transaction audit write failed")

    monkeypatch.setattr(dr, "_write_settlement_audit", _audit_probe)
    sweep()   # 审查员内部异常被 tick 兜底,不炸 sweeper

    o = observe(live_dsn, w["run_token"])
    assert seen, "审查员一次都没写 audit —— 双写只剩一轨"
    assert all(seen), "audit 不是用调用方游标写的(拿到 cur=None)⇒ 两轨不同事务"
    assert o["adjudications"] == [], \
        "audit 写失败了,049 行却留了下来 —— 两轨不同事务:%r" % (o["adjudications"],)
    assert o["run"]["run_status"] == "settlement_manual", "留痕失败还把状态迁走了:%r" % (o["run"],)


def test_adjudicator_and_human_admin_are_mutually_exclusive_exactly_one_wins(
        live_server, live_dsn):
    """CAS 互斥:人工 admin 先处置同一单 ⇒ 审查员**零动作且不崩**(恰一个成功)。

    走的是 admin 处置的同一把 CAS(只认 `settlement_manual`),所以人先动完之后
    审查员的 `verify_and_resolve_manual` 只会拿到 ok=False,而不是把钱再动一遍。
    """
    set_coefficients(live_dsn, max_frozen_points=10_000)
    w = manual_world(live_dsn, frozen=650, with_product=True)

    from services.diagnosis_runs import verify_and_resolve_manual
    human = verify_and_resolve_manual(
        w["run_token"], "human_admin(uid=1)", "legacy", int(w["freeze_id"]), "release", "人工先处置")
    assert human.get("ok"), human

    sweep()   # 审查员随后扫到:这单已经不在 settlement_manual
    o = observe(live_dsn, w["run_token"])

    assert o["run"]["run_status"] in ("release_pending", "released"), \
        "人工处置的结果被审查员覆盖了:%r" % (o["run"],)
    assert _adj(o, "executed") == [], "审查员在人工处置后还执行了一次:%r" % (o["adjudications"],)


# ===========================================================================
# 撕锁实测存活后补的四条(每一条都对应一发活下来的变异)
# ===========================================================================

def test_audit_row_says_it_was_the_ai_not_a_human(live_server, live_dsn):
    """审计行必须写明**是审查员判的**,不是人判的。

    补这条的由来:活性对照(把 `OPERATOR` 改掉)**存活** —— 说明没有任何判据
    在守这一列。而事后对账时,它是区分「AI 自动处置」与「人工处置」的唯一凭据;
    丢了这个区分,一批自动处置出问题时根本分不清责任面。
    """
    import services.settlement_adjudicator as _mod
    # 🔴 **不能**写 `a["operator"] == _mod.OPERATOR`:那是拿被测模块自己的常量去断言
    #    它自己 —— 常量被改掉时判据两边同时跟着改,恒绿。撕锁实测:活性对照
    #    (把 OPERATOR 改成别的值)在那种写法下**存活**。所以这里钉**字面量**。
    OPERATOR = "ai_adjudicator"
    assert _mod.OPERATOR == OPERATOR, (
        "审查员在审计轨里的署名被改了(%r)—— 对账靠这个值区分 AI 判的和人判的,"
        "改它等于让历史记录对不上号" % (_mod.OPERATOR,))
    set_coefficients(live_dsn, max_frozen_points=10_000)
    w = manual_world(live_dsn, frozen=650, with_product=True)

    sweep()
    o = observe(live_dsn, w["run_token"])

    mine = [a for a in o["audit"] if a["operator"] == OPERATOR]
    assert mine, "审计轨里没有一行署名审查员(operator=%r):%r" % (OPERATOR, o["audit"])

    # ① 审查员自己的两轨记录(decided / executed)必须能按 action 前缀捞出来。
    own = [a for a in mine if a["action"].startswith("adjudicator_")]
    assert {a["action"] for a in own} >= {"adjudicator_decided", "adjudicator_executed"}, \
        "审查员自己的审计行不全或前缀不对,事后按 action 捞不出来:%r" % (own,)

    # ② **闸门那一行也必须署名审查员**:`verify_and_resolve_manual` 会自己写一行
    #    `manual_resolve_single`。它是"钱按谁的决定动的"那一行 —— 如果它署名成
    #    普通 admin 或 system,对账时就分不出这批处置是 AI 判的还是人判的。
    gate = [a for a in mine if a["action"].startswith("manual_resolve")]
    assert gate, "闸门审计行没有署名审查员 —— 对账分不出 AI/人:%r" % (o["audit"],)


def test_invalid_coefficients_are_treated_as_unconfigured(live_server, live_dsn):
    """系数**存在但非法**(≤0)⇒ 与未配置同等对待:零动作。

    补这条的由来:变异 U8(非法系数时给个宽松默认值)**存活** ——
    原来那条"未配系数"判据是把 key 全删,走的是更早的 `enabled` 分支,
    从没执行过"值非法"这条腿。fail-closed 有两个入口,判据只守了一个。
    """
    set_coefficients(live_dsn, max_frozen_points=0, same_cause_limit=0)
    w = manual_world(live_dsn, frozen=650, with_product=True)

    sweep()
    o = observe(live_dsn, w["run_token"])

    assert o["run"]["run_status"] == "settlement_manual", o["run"]
    assert o["adjudications"] == [], "系数非法却照样裁定了:%r" % (o["adjudications"],)
    assert o["freeze"]["status"] == "frozen"


def test_missing_freeze_row_escalates_instead_of_being_read_as_zero(live_server, live_dsn):
    """FreezeHandle **完整但指向不存在的冻结行** ⇒ 读不到冻结额 ⇒ 升级,**绝不当 0 放行**。

    补这条的由来:变异 U9(读不到冻结额时返回 1 而不是 None)**存活** ——
    原有的"证据不全"臂是把 freeze_id 置空,在更早的 `missing` 检查就升级了,
    `_frozen_amount` 返回 None 那条腿从来没被执行过。
    这两种"缺"不是一回事:handle 缺 = 结构性缺;handle 在但行不在 = 数据对不上。
    """
    from tests.settlement_adjudicator_2026_08_25._world import conn
    set_coefficients(live_dsn, max_frozen_points=10_000)
    w = manual_world(live_dsn, frozen=650, with_product=True)
    c = conn(live_dsn)
    c.cursor().execute(
        "UPDATE diagnosis_runs SET freeze_id=999000111 WHERE run_token=%s", (w["run_token"],))
    c.close()

    sweep()
    o = observe(live_dsn, w["run_token"])

    assert o["run"]["run_status"] == "settlement_manual", o["run"]
    esc = _adj(o, "escalated")
    assert [a["escalation_code"] for a in esc] == ["evidence_incomplete"], o["adjudications"]
    assert _adj(o, "executed") == [], "读不到冻结额却还是执行了:%r" % (o["adjudications"],)
    # 🔴 只断言 escalation_code **区分不出来**:真代码在「读不到冻结额」处升级,而
    #    "读不到就兜个数放行" 的毒会往下多走一步、在「冻结行不存在」处升级 ——
    #    两条路的 code 都是 evidence_incomplete。撕锁实测 U9 就是这么存活的。
    #    区分点在**落库的值**:读不到就该是 NULL,不是一个被兜底造出来的金额。
    assert esc[0]["frozen_points"] is None, (
        "读不到冻结额却往记录里写了个数 %r —— 那是个假金额,限额门控会拿它去放行"
        % (esc[0]["frozen_points"],))


def test_runs_a_human_is_currently_working_are_not_candidates(live_server, live_dsn):
    """人工**正在处置中**(manual_resolving · 租约在手)的单,审查员一根手指都不许碰。

    补这条的由来:变异 U14(把 manual_resolving 也纳入候选)**存活** ——
    原有的互斥判据用的是"人工**已处置完**"的单(状态已迁走,自然不是候选),
    没有覆盖"人工**在途**"。这两者的危险程度完全不同:后者是双执行者并发。
    """
    from tests.settlement_adjudicator_2026_08_25._world import conn
    set_coefficients(live_dsn, max_frozen_points=10_000)
    w = manual_world(live_dsn, frozen=650, with_product=True)
    c = conn(live_dsn)
    c.cursor().execute(
        "UPDATE diagnosis_runs SET run_status='manual_resolving', "
        "manual_resolution='commit', manual_resolution_token='human_token', "
        "manual_lease_until=NOW()+INTERVAL '10 minutes' WHERE run_token=%s", (w["run_token"],))
    c.close()

    sweep()
    o = observe(live_dsn, w["run_token"])

    assert o["run"]["run_status"] == "manual_resolving", \
        "审查员把人工正在处置的单抢走了:%r" % (o["run"],)
    assert o["adjudications"] == [], \
        "审查员对人工在途的单产生了裁定:%r" % (o["adjudications"],)
    assert o["freeze"]["status"] == "frozen"
