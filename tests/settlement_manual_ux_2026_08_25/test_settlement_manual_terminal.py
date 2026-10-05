"""【settlement-manual-ux】结算转人工必须给用户一个**终态**,不能停在 99% 转圈。

## 这一单在修什么

P0-3c 交回件②:结算转人工的几档里,有的发 `done:false`「正在完成结算」,
有的**什么都不发** —— 前端只认 `terminal/done`,于是永远转圈。
「绝不假装已退」是对的(Owner 已拍:外部副作用已发生 ⇒ 不自动 release,转人工;
**本单一行不碰这个资金语义**),但「什么都不说」是另一回事。

## 判据形态照 2026-08-25 新规

每一条都真的 `await server.run_diagnosis_task(...)`(复用 P0-3c 的 `_world`/`drive` 台架),
真库、真资金原语。**不是**断言"源码里有那行 send_message"——那种锁在
`_dr` 未绑定的 NameError 面前是绿的(P0-3b 实证)。

## 分档(AST 机械枚举 `run_diagnosis_task` 的 11 个 SSE 发送点得出,不是手数)

| 档 | 触发 | 本单前 | 本单后 |
|----|------|--------|--------|
| identity 复核 | 品牌识别可疑 | ✅ 已有终态 | 一个字没改,**加判据钉住** |
| A | commit 判成转人工(多池无快照等) | ❌ done:false | ✅ 转人工终态 |
| B | 外层 except 释放未即时终态且已转人工 | ❌ done:false | ✅ 转人工终态 |
| C | 内层 cex 释放亦失败 | ❌ **什么都不发** | ✅ 转人工终态 |

**配对的必须不命中**:真的"还在重试、交 sweeper"(`commit_pending`/`release_pending`)
**必须继续 done:false** —— 那种单 sweeper 还会推真终态,提前宣布终态就是骗用户。
"""
from __future__ import annotations

import asyncio

import pytest

from tests.p03c_org_guards_2026_08_25._world import legacy_world, observe, org_world
from tests.p03c_org_guards_2026_08_25.test_settlement_dispatch_runtime import _Recorder, drive


def degraded_verdict(*, planned=32, succeeded=8):
    """降级交付判定 —— 用**真生产者**造,不手写 dict(手写会漂成生产不供的形状)。"""
    from workflows.diagnosis_workflow import evaluate_delivery_verdict
    v = evaluate_delivery_verdict({
        "engines_tested": ["qwen"], "total_tests": succeeded, "total_planned": planned,
        "total_failed": planned - succeeded,
        "summary": {"total_planned": planned, "total_tests": succeeded,
                    "total_failed": planned - succeeded},
    })
    return {"version": v.version, "outcome": v.outcome, "planned": v.planned,
            "succeeded": v.succeeded, "coverage_ratio": round(v.coverage_ratio, 4),
            "billable_ratio": round(v.billable_ratio, 4),
            "failed_platforms": list(v.failed_platforms), "message": v.message}


def terminals(sent):
    return [p for p in sent if p.get("done") is True and p.get("terminal") is True]


def manual_terminals(sent):
    return [p for p in sent if p.get("needs_manual_review") is True]


def _drive_failing(server, world, monkeypatch, *, fail_impl=None, fail_dispatch=False):
    """org 臂的两条失败路径 —— 与 P0-3c 同款驱动,只是这里要看**发了什么**。"""
    rec = _Recorder(server.manager)
    monkeypatch.setattr(server, "manager", rec)

    async def _boom(*a, **kw):
        raise RuntimeError("settlement-ux 人造失败")

    async def _ok_impl(*a, **kw):
        return world["snapshot"]

    monkeypatch.setattr(server, "_run_diagnosis_impl", _boom if fail_impl else _ok_impl)
    if fail_dispatch:
        import services.diagnosis_runs as _dr_mod
        monkeypatch.setattr(_dr_mod, "dispatch_success_settlement", _boom)

    request = server.DiagnosisRequest(
        brand_name="P03C客户", industry="测试行业", keywords=["词"], brand_id=world["brand_id"])
    asyncio.run(server.run_diagnosis_task(
        request, world["session_id"], world["uid"], run_token=world["run_token"],
        slot_mode="none",
        organization_identity=world.get("organization_identity"),
        organization_charge_id=world.get("organization_charge_id"),
        organization_charge_points=world.get("organization_charge_points"),
        organization_claim_token=world.get("organization_claim_token")))
    return rec.sent, observe(world["dsn"], world)


# ===========================================================================
# 档 A · commit 判成转人工(多池 + 无拆分快照 + 降级交付)
# ===========================================================================

def test_arm_a_commit_routed_to_manual_emits_a_user_visible_terminal(
        live_server, live_dsn, monkeypatch):
    """必须命中:run 真的落 settlement_manual,且用户真的收到**终态**。"""
    w = legacy_world(live_dsn, engines=["qwen"], successful_tests=8,
                     split={"bonus": 300, "commission": 0, "paid": 350})
    snap = dict(w["snapshot"], delivery_verdict=degraded_verdict())
    sent, after = drive(live_server, w, monkeypatch, impl_result=snap)

    assert after["run"]["run_status"] == "settlement_manual", after
    assert "reserved_split_order_unknown" in (after["run"]["last_settlement_error"] or ""), after
    assert after["freeze"]["status"] == "frozen", "转人工时绝不能已经动过钱(资金语义本单不碰)"

    got = manual_terminals(sent)
    assert got, "档A 没有给用户任何终态 —— 前端会停在 99% 一直转:%r" % (sent,)
    assert len(got) == 1, "同一单发了多条转人工终态:%r" % (got,)
    p = got[0]
    assert p["done"] is True and p["terminal"] is True, p
    assert p["type"] not in ("complete", "error"), "转人工既不是成交也不是故障:%r" % p["type"]
    assert p["code"] == "settlement_manual", p


def test_arm_a_copy_is_human_and_carries_no_engineering_words(live_server):
    """文案锁:人话 + 术语铁律。工程词一个都不许上屏。"""
    from services.diagnosis_runs import SETTLEMENT_MANUAL_MESSAGE as M
    for junk in ("charge", "unknown", "release", "token", "quarantine", "settlement",
                 "null", "None", "积分", "额度"):
        assert junk not in M, "用户面文案出现工程词/禁用术语 %r:%s" % (junk, M)
    assert "人工" in M and "冻结" in M, M
    # 配对的必须不命中:别把"已退回"这种没发生的事说出去(这一档钱还挂着)。
    assert "已退" not in M, "这一档钱没退,不许说已退:" + M


def test_arm_a_copy_does_not_promise_the_page_updates_itself(live_server):
    """负锁:**不许承诺页面会自己更新** —— 因为它不会。

    这一档发的是 terminal 事件,前端收到就**停轮询**(WS 关掉、轮询 clearInterval)。
    所以"核实完成会自动更新"是句假承诺:人工核实完成后这个页面不会原地刷新。
    Owner 2026-08-25 定稿据此把后半句改成「回到这里就能看到结果」。

    这条是**语义锁**,与下面那条逐字锁分工不同:逐字锁盯"有没有漂",
    这条盯"漂成了哪一类错" —— 将来 Owner 再改文案(逐字锁跟着更新)时,
    这条仍然拦得住任何形态的"它会自己更新"承诺。
    """
    from services.diagnosis_runs import SETTLEMENT_MANUAL_MESSAGE as M
    for lie in ("自动更新", "自动刷新", "实时更新", "会自动"):
        assert lie not in M, (
            "文案承诺页面自己更新,但收到终态就停轮询、页面不会原地刷新 —— "
            "假承诺 %r:%s" % (lie, M))


def test_arm_a_copy_is_owner_approved_verbatim(live_server):
    """逐字锁:Owner 2026-08-25 定稿原文,一个字都不许漂。

    用户面文案的改动权在 Owner 不在我们 —— 上面两条属性锁挡的是"类错误",
    挡不住"合规但没人批过"的改写(换个说法、加句安慰、调标点都能全绿通过)。
    这条把定稿钉成字节。**要改先拿 Owner 新口径,再改这一行。**
    """
    from services.diagnosis_runs import SETTLEMENT_MANUAL_MESSAGE as M
    approved = (
        "结算转人工核实中,费用已冻结、不会多扣;"
        "核实完成后,回到这里就能看到结果,无需操作。"
    )
    assert M == approved, "文案偏离 Owner 定稿:\n  实际=%s\n  定稿=%s" % (M, approved)


# ===========================================================================
# 档 B / C · org 失败两路(释放被隔离 → 转人工)
# ===========================================================================

def test_arm_b_outer_except_manual_emits_terminal(live_server, live_dsn, monkeypatch):
    """外层 except:诊断本身抛 → 释放被隔离 → run 落 settlement_manual → 必须给终态。"""
    w = org_world(live_dsn)
    sent, after = _drive_failing(live_server, w, monkeypatch, fail_impl=True)

    assert after["run"]["run_status"] == "settlement_manual", after
    got = manual_terminals(sent)
    assert got, "档B 没有给用户任何终态:%r" % (sent,)
    assert got[0]["code"] == "settlement_manual", got[0]
    # 配对的必须不命中:这一档钱**没有**退,绝不能发出任何"已退回"的话
    assert all("已退" not in str(p.get("message", "")) for p in got), got


def test_arm_c_inner_cex_release_failed_emits_terminal(live_server, live_dsn, monkeypatch):
    """内层 cex 且释放亦失败:P0-3c 时这一档**一个字都不发**,现在必须给终态。

    这条就是 P0-3c 那条行为快照判据的**继任者**(那条断言的是"不发终态",
    本单把行为改了,按继任规矩由这条钉住新行为)。
    """
    w = org_world(live_dsn)
    sent, after = _drive_failing(live_server, w, monkeypatch, fail_dispatch=True)

    assert after["run"]["run_status"] == "settlement_manual", after
    got = manual_terminals(sent)
    assert got, "档C 仍然什么都不发 —— 前端停在 99% 一直转:%r" % (sent,)
    assert len(got) == 1, got
    assert got[0]["type"] == "settlement_manual", got[0]
    assert "error" not in got[0], (
        "带 error 键会被前端渲染成红色故障横幅 + 「重新诊断」按钮,"
        "而这一档重试无意义且可能双花:%r" % got[0])


# ===========================================================================
# 已有终态那一档:不改,但**钉住**(别只修一档)
# ===========================================================================

def test_identity_review_arm_still_emits_its_own_terminal_unchanged(
        live_server, live_dsn, monkeypatch):
    """品牌识别复核档本单一个字没改 —— 它本来就有终态,这里钉住它别被顺手改坏。"""
    from services.diagnosis_runs import IDENTITY_REVIEW_MESSAGE
    w = legacy_world(live_dsn, engines=["qwen"], successful_tests=8)
    snap = dict(w["snapshot"],
                identity_suspicion={"suspected": True, "verdict": "suspect"},
                delivery_verdict=degraded_verdict())
    sent, after = drive(live_server, w, monkeypatch, impl_result=snap)

    assert after["run"]["run_status"] == "settlement_manual", after
    got = manual_terminals(sent)
    assert len(got) == 1, got
    assert got[0]["message"] == IDENTITY_REVIEW_MESSAGE, (
        "识别复核档的文案被改了 —— 本单不该碰它:%r" % got[0].get("message"))


# ===========================================================================
# 配对的必须不命中:真的"还在重试"绝不能被宣布成终态
# ===========================================================================

def test_still_retrying_must_not_be_announced_as_terminal(live_server, live_dsn, monkeypatch):
    """成功成交的单不该出现任何转人工终态(证明上面几条不是"逢单就发")。"""
    w = legacy_world(live_dsn)
    sent, after = drive(live_server, w, monkeypatch)
    assert after["run"]["run_status"] in ("committed", "commit_pending"), after
    assert not manual_terminals(sent), "正常成交单被宣布成转人工:%r" % (sent,)
    assert terminals(sent), "正常成交单反而没终态了:%r" % (sent,)


def test_run_is_settlement_manual_reads_the_row_not_the_branch(live_server, live_dsn):
    """`run_is_settlement_manual` 必须**读 run 行**。

    它是档B/档C 用来区分"确定终态"与"还在重试"的唯一依据;
    如果它恒真,`commit_pending` 那种还在重试的单会被提前宣布终态。
    """
    from services.diagnosis_runs import run_is_settlement_manual
    w = legacy_world(live_dsn)                      # 新造的 run 是 running
    assert run_is_settlement_manual(w["run_token"]) is False
    from db.connection import get_db
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE diagnosis_runs SET run_status='settlement_manual' WHERE run_token=%s",
            (w["run_token"],))
    assert run_is_settlement_manual(w["run_token"]) is True
    assert run_is_settlement_manual("run_does_not_exist_p03ux") is False


# ===========================================================================
# 台架自检:两个测试包共用同一个库 —— 分裂了就在这里报真因
# ===========================================================================

def test_the_session_db_build_counter_is_wired(live_server, live_dsn):
    """**接线锁**(不是行为锁,这点必须说清楚)。

    本包 re-export 了 p03c 的 `live_dsn`/`live_server`。pytest 按「名字 + 所在 conftest」
    解析 fixture,拿到的是**同名但不同**的对象;不做进程级单例就会**建两个库**,
    而 `import server` 的 `init_db` 只对先建的那个跑一次 —— 后建的那个 org 就绪门恒 503。
    症状:两个包**单跑各自全绿、一合跑 4 条 org 判据齐红**,报错指向 `readiness()`,
    离真因隔两层。

    ## 这条判据只锁「计数器接上了」,执法在别处

    真正的执法是 p03c conftest **建库处**的那句 `raise`(第二次建库当场抛,
    并把真因写在异常里)。放在那里是因为分裂只在那一刻可见:
    本包先跑,本包的判据执行时第二个库**还没被建出来**,怎么数都是 1。

    我为这条写坏过两版,都是"看起来在守、实际零区分力":
      · 第一版查「本包这个库 migration_ok 是不是 true」—— 本包那个库恰好是好的,恒绿;
      · 第二版数「会话库个数」—— 被执行顺序绕过,恒绿。
    两版都是靠**看变异被谁判红**(而不是看它死没死)才发现的:
    摘掉单例那发毒,判红的自始至终是 p03c 那 4 条,这条从没红过。
    """
    import os
    assert os.environ.get("P03C_SESSION_DB_BUILDS_INPROC") == "1", (
        "建库计数器没接上或不是 1 —— 分裂守卫失效:%r"
        % os.environ.get("P03C_SESSION_DB_BUILDS_INPROC"))
