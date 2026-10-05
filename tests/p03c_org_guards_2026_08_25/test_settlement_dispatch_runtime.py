"""【P0-3c 件2 · R1-A】结算分派段的**运行时**判据 —— 从 server 本体驱动。

## 与 P0-3b 的根本区别

P0-3b 的 92 条判据全部驱动 `services/` 里的纯函数,`server.py` 只被 `ast.parse` 读过。
于是「那一行跑起来会不会抛」这类问题**一条判据都答不了**,`_dr` 未绑定的 NameError
带着 92 全绿上线,每单诊断必挂。

这里的每一条都真的 `await server.run_diagnosis_task(...)`:
真库、真 `commit_run` / 真 `settle_charge`、真 run 行终态。
被替掉的只有 `_run_diagnosis_impl`(上游跑四引擎那一步 · 真跑要花钱调 LLM)
和 `manager`(SSE 传输)—— **被测的分派段本身一行没被替**。
"""
from __future__ import annotations

import asyncio

import pytest

from tests.p03c_org_guards_2026_08_25._world import legacy_world, observe, org_world


class _Recorder:
    """记录 SSE,其余属性透传给真 manager。"""

    def __init__(self, real):
        self._real = real
        self.sent = []

    async def send_message(self, sid, payload):
        self.sent.append(payload)

    def clear_task(self, sid):
        pass

    def _set_status(self, sid, payload):
        pass

    def __getattr__(self, k):
        return getattr(self._real, k)


def drive(server, world, monkeypatch, *, impl_result=None):
    """真跑 `run_diagnosis_task`,返回 (发出去的 SSE, 库里的终态)。"""
    rec = _Recorder(server.manager)
    monkeypatch.setattr(server, "manager", rec)

    async def _fake_impl(request, session_id, creator_user_id=None, run_token=None,
                         organization_identity=None, **_extra_kw):
        # 只替上游"跑诊断"这一步。它在生产里返回完成 payload,这里照样返回完成 payload。
        # [合流 2026-08-25 Review-CTO] `**_extra_kw`:防御线给 _run_diagnosis_impl
        # 穿了新参 ai_engines(引擎保真族),窄签名桩在合并树上让每单 TypeError
        # 走失败路径、7 条判据齐红。桩只替"四引擎跑"这一步,对透传参数一律收下不用
        # ——同类窄签名断裂就此免疫(本目录其余桩本就是 *a, **kw)。
        return impl_result if impl_result is not None else world["snapshot"]

    monkeypatch.setattr(server, "_run_diagnosis_impl", _fake_impl)

    request = server.DiagnosisRequest(
        brand_name="P03C客户", industry="测试行业", keywords=["词"],
        brand_id=world["brand_id"])
    asyncio.run(server.run_diagnosis_task(
        request, world["session_id"], world["uid"],
        run_token=world["run_token"], slot_mode="none",
        organization_identity=world.get("organization_identity"),
        organization_charge_id=world.get("organization_charge_id"),
        organization_charge_points=world.get("organization_charge_points"),
        organization_claim_token=world.get("organization_claim_token")))
    return rec.sent, observe(world["dsn"], world)


# ===========================================================================
# 两条臂:各自走到**正确的原语**,终态真落库
# ===========================================================================

def test_non_org_arm_settles_through_the_legacy_freeze_primitive(live_server, live_dsn, monkeypatch):
    """非 org 单:分派必须走 `commit_run` → legacy 冻结真的 committed、钱真的扣。"""
    w = legacy_world(live_dsn)
    sent, after = drive(live_server, w, monkeypatch)

    assert after["freeze"]["status"] == "committed", after
    assert after["run"]["run_status"] in ("committed", "commit_pending"), after
    assert after["wallet"]["frozen_points"] == 0, "冻结额没落地"
    # 成功终态必须发出去(退款窗口内不许提前发,但成交后必须发)
    assert any(p.get("type") == "complete" and p.get("done") for p in sent), sent


def test_org_arm_settles_through_the_organization_charge_primitive(live_server, live_dsn, monkeypatch):
    """org 单:分派必须走 `settle_charge` → charge link 真的进 committed。

    配对的必须不命中:org 臂**没有** legacy 冻结行,所以绝不能顺手把 `commit_run` 也调了。
    """
    w = org_world(live_dsn)
    sent, after = drive(live_server, w, monkeypatch)

    assert after["charge"]["status"] == "committed", after
    assert after["run"]["run_status"] != "released", after
    assert any(p.get("type") == "complete" and p.get("done") for p in sent), sent


# ===========================================================================
# 失败两臂:让 4 处守卫**全部**有运行时判据,而不是只有静态锁
#
# P0-3b 的 A6b 之所以能在 92 条全绿下活着,就是因为这些分支只被 AST 读过、
# 没被跑过。这里把外层 except(release_charge)与内层 cex(release_org_cex)
# 都真跑一遍 —— 反转任一处守卫,org 的钱会挂在 charge link 上没人退。
# ===========================================================================

def test_org_arm_failure_releases_the_charge_through_the_outer_except(
        live_server, live_dsn, monkeypatch):
    """外层 except 那处守卫:诊断本身抛 → org charge 必须被 release,钱不许挂着。"""
    w = org_world(live_dsn)

    rec = _Recorder(live_server.manager)
    monkeypatch.setattr(live_server, "manager", rec)

    async def _boom(*a, **kw):
        raise RuntimeError("p03c 人造诊断失败")

    monkeypatch.setattr(live_server, "_run_diagnosis_impl", _boom)
    request = live_server.DiagnosisRequest(
        brand_name="P03C客户", industry="测试行业", keywords=["词"], brand_id=w["brand_id"])
    asyncio.run(live_server.run_diagnosis_task(
        request, w["session_id"], w["uid"], run_token=w["run_token"], slot_mode="none",
        organization_identity=w["organization_identity"],
        organization_charge_id=w["organization_charge_id"],
        organization_charge_points=w["organization_charge_points"],
        organization_claim_token=w["organization_claim_token"]))

    after = observe(live_dsn, w)
    # 实测的真行为(不是我预期的那个):`mark_external_side_effect_started`(:2740)
    #   已经向 charge link 报过"外部副作用已开始",所以 release 被**拒绝并隔离**
    #   成 status='unknown' + run='settlement_manual' 交人工 —— 已经动过外部资源的单
    #   不许闭着眼自动退。判据按**真行为**写,不按我以为的写。
    assert after["charge"]["status"] == "unknown", (
        "org 单失败后没走到 org 释放路由(charge 停在 %r)—— 守卫方向反了的话"
        "这个分支根本不会执行,charge 会停在 reserved:%s" % (after["charge"]["status"], after))
    assert after["charge"]["status"] != "reserved", "org 释放分支压根没跑"
    assert after["run"]["run_status"] == "settlement_manual", after


def test_org_arm_settlement_exception_releases_the_charge_through_the_inner_cex(
        live_server, live_dsn, monkeypatch):
    """内层 `except Exception as cex` 那处守卫(P0-3b R-a):

    结算动作本身中途抛 → 外层 except 够不到(异常被这一层吞了),
    必须由这一层把 charge 释放掉。反转这处守卫 → 非 org 单拿 charge_link_id=None
    去调 release,org 单的钱则永远挂着。
    """
    w = org_world(live_dsn)

    rec = _Recorder(live_server.manager)
    monkeypatch.setattr(live_server, "manager", rec)

    async def _ok_impl(*a, **kw):
        return w["snapshot"]

    async def _settle_boom(**kw):
        raise RuntimeError("p03c 人造结算中途异常")

    monkeypatch.setattr(live_server, "_run_diagnosis_impl", _ok_impl)
    # 只让**分派**抛 —— 被测的是它外面那层 except 的 org 释放路由。
    import services.diagnosis_runs as _dr_mod
    monkeypatch.setattr(_dr_mod, "dispatch_success_settlement", _settle_boom)

    request = live_server.DiagnosisRequest(
        brand_name="P03C客户", industry="测试行业", keywords=["词"], brand_id=w["brand_id"])
    asyncio.run(live_server.run_diagnosis_task(
        request, w["session_id"], w["uid"], run_token=w["run_token"], slot_mode="none",
        organization_identity=w["organization_identity"],
        organization_charge_id=w["organization_charge_id"],
        organization_charge_points=w["organization_charge_points"],
        organization_claim_token=w["organization_claim_token"]))

    after = observe(live_dsn, w)
    # 同上:外部副作用已报 ⇒ 释放被隔离成 unknown 转人工,而不是静默 released。
    #   关键判别力在"charge **离开了 reserved**":守卫方向一反,org 单走不进这个分支,
    #   charge 会原地停在 reserved,既没退也没人管 —— 那正是 P0-3b R-a 要补的洞。
    assert after["charge"]["status"] == "unknown", (
        "结算中途异常后没走到内层 org 释放路由:%s" % after)
    assert after["charge"]["status"] != "reserved", "内层 cex 的 org 释放分支压根没跑"

    # 内层分支真的执行过的第二个独立证据:它给 admin 记了问题单。
    #   (`_BILLING_FAILURES` 是 server 模块内的进程内列表,按 run_token 过滤。)
    issues = [x for x in live_server._BILLING_FAILURES
              if x.get("run_token") == w["run_token"]]
    phases = {x.get("feature_code") for x in issues}
    assert "diagnosis_settlement:commit_exc_release_failed" in phases, (
        "内层 cex 的 org 释放路由没留下问题单 —— 说明那个分支没跑到:%r" % (phases,))

    # ── 判据继任(settlement-manual-ux 2026-08-25)────────────────────────────
    # 这条原来断言的是「这一档**不发**任何终态」—— 那是 P0-3c 交付时的**现状快照**,
    # 并在注释里写明"哪天有人改成发终态,这条会红并提醒同步更新"。
    # 那一天到了:`WO_SETTLEMENT_MANUAL_UX_2026-08-25` 把这一档改成发转人工终态
    # (前端原来停在 99% 无限转圈)。按「退役判据必须写继任者」的规矩,
    # 这条不删、改成钉住**新**行为,而且是**双向**的:
    #   · 不发 → 红(回归成无限转圈)
    #   · 发了但发错种类(complete/error / 多发一条)→ 也红
    terminals = [p for p in rec.sent if p.get("done") is True and p.get("terminal") is True]
    assert len(terminals) == 1, (
        "这一档必须**恰好**给用户一条终态(不发=前端永远转圈;多发=前端状态打架):%r"
        % (terminals,))
    t = terminals[0]
    assert t.get("needs_manual_review") is True and t.get("code") == "settlement_manual", (
        "终态种类不对 —— 这一档是「转人工」,不是成交也不是故障:%r" % t)
    assert t.get("type") not in ("complete", "error"), t
    assert "已退" not in str(t.get("message", "")), "这一档钱没退,不许说已退:%r" % t
