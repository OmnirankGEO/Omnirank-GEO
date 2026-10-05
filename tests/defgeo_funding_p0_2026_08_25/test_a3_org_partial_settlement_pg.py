"""【A-3 = Codex P0-3】组织腿必须与个人腿共用同一个「按真实履约算 actual」的 SSOT。

修之前:``dispatch_success_settlement`` 的 org 臂恒传 ``reserved_ceiling_points``。
诊断结果里明明带着 ``delivery_verdict``(降级比例)与 ``identity_suspicion``
(疑似认错品牌),组织分支**一个都不消费** —— 只有 25% 平台测成、或者压根没认出
这个品牌,预留 650 照样实扣 650。个人钱包腿早就有部分履约扣费,组织腿没有。

判据形态:**四组 × 两臂**,全部**真库真原语**(真 ``settle_charge`` / 真
``commit_freeze`` / 真终态落库),由 ``server.run_diagnosis_task`` 本体驱动 ——
被替掉的只有跑四引擎那一步(真跑要花钱调 LLM)和 SSE 传输。

  组① 0% 成功    → 上游 ``require_complete_product`` 拦下,走 release,**不结算**
  组② 部分成功    → 两臂都按比例(ceiling/冻结额 × billable_ratio)
  组③ 身份疑点    → 两臂都隔离转人工,钱**既不扣也不退**
  组④ 全部成功    → 两臂都全额(行为与修之前一致 —— 这是"没改坏"的那一半)

🔴 组②的 org 臂就是这次 P0 的靶心:它在修之前会是 650。
"""
from __future__ import annotations

import json
import uuid

import pytest

from tests.p03c_org_guards_2026_08_25 import _world as P03C
from tests.p03c_org_guards_2026_08_25.test_settlement_dispatch_runtime import drive

pytestmark = pytest.mark.integration

#: [E2-1] 分账样本换成**非终止小数**:9/13 = 0.6923076923…
#: 旧样本 0.25 是二进制可精确表示的,float 与整数两式恒等 —— 那样的样本
#: 对「分账用不用整数」**零判别力**(工单 E2-1 第 3 条点名的就是这件事)。
PLANNED = 13
SUCCEEDED = 9
FROZEN = 650
#: 展示口径仍留 ratio(只做交叉校验,不参与算钱)。
RATIO = SUCCEEDED / PLANNED
#: 两臂的预留总额刻意相同 —— 「共用同一个 SSOT」这句话要能被**逐点比对**证实,
#: 而不是各自算出各自的数再各自断言。
#: 🔴 **手算的字面量**,不是用实现的公式算出来的。
#:    650 × 9 = 5850;5850 ÷ 13 = 450(13 × 450 = 5850,整除,无余数)。
#:    判据不许构造自己的期望 —— 写成 `(FROZEN*SUCCEEDED)//PLANNED` 就是把被测公式
#:    抄进判据,实现改错了判据也跟着错(本仓铁律,工单 E2-1 第 3 条逐字要求)。
#:    对照:旧的 float 路径会算出 449(round(9/13,4)=0.6923 → int(650×0.6923)),
#:    所以这个字面量同时是「删掉整数修复即红」的那一位。
EXPECTED_PARTIAL = 450


def _complete_payload(world, *, verdict=None, identity_suspected=False):
    """impl 返回的完成 payload —— 形状照 ``settlement_signals_from_result`` 落进去的那两个键。

    🔴 键名不猜:``_partial_commit_points`` 读的是顶层 ``delivery_verdict``,
       ``_identity_suspected`` 认的是 ``identity_suspicion.suspected is True``。
       夹具供一个生产不会供的键,判据会**恒绿**(本仓 2026-08-20 实录)。
    """
    payload = dict(world["snapshot"])
    payload["delivery_verdict"] = verdict
    payload["identity_suspicion"] = {"suspected": True} if identity_suspected else None
    return payload


def _degraded_verdict():
    from services.diagnosis_sample_contract import OUTCOME_DEGRADED, SAMPLE_CONTRACT_VERSION
    # [E2-1] 形状照**生产持久化那两处**逐字来(workflows/diagnosis_workflow.py):
    #   version / outcome / planned / succeeded / coverage_ratio / billable_ratio / …
    # 少给 planned+succeeded 就是在造一个生产不会出现的旧快照 —— 那会让判据
    # 走到「转人工」而不是分账,红得莫名其妙(本仓「夹具供了生产不会供的东西」的反面)。
    return {"outcome": OUTCOME_DEGRADED, "version": SAMPLE_CONTRACT_VERSION,
            "planned": PLANNED, "succeeded": SUCCEEDED,
            "coverage_ratio": round(RATIO, 4), "billable_ratio": round(RATIO, 4)}


# ═══════════════════════════════════════════════════════════════════════════
# 组②:部分成功 —— 本次 P0 的靶心
# ═══════════════════════════════════════════════════════════════════════════
def test_group2_org_arm_settles_the_delivered_share_not_the_ceiling(
        live_server, migrated_dsn, monkeypatch, db):
    """org 单降级交付 ⇒ ``organization_charge_links.actual_points`` 必须是按比例的那个数。

    修之前这里恒等于 ``reserved_ceiling_points`` —— 只测成 25%,预留 650 实扣 650。
    """
    w = P03C.org_world(migrated_dsn)
    ceiling = int(w["organization_charge_points"])
    _sent, after = drive(live_server, w, monkeypatch,
                         impl_result=_complete_payload(w, verdict=_degraded_verdict()))

    # 手算:ceiling 650 × 9 = 5850;5850 ÷ 13 = 450。字面量,不抄实现公式。
    assert ceiling == FROZEN, "org 预留上限变了(%r),下面那个手算的期望就不适用了" % ceiling
    expected = EXPECTED_PARTIAL
    assert after["charge"]["status"] == "committed", after
    assert int(after["charge"]["actual_points"]) == expected, (
        "org 降级交付实扣 %r,应为按比例的 %r(预留上限 %r)—— "
        "恒等于上限就是 Codex P0-3 点名的那个形态"
        % (after["charge"]["actual_points"], expected, ceiling))
    assert int(after["charge"]["actual_points"]) < ceiling, (
        "按比例算出来的数竟然等于上限 —— 这条判据没有判别力了,先改 RATIO 再说")


def test_group2_personal_arm_settles_the_same_share_from_the_same_predicate(
        live_server, migrated_dsn, monkeypatch, db):
    """个人腿同一档:冻结按同一比例部分扣,余额释放。两臂结果**逐点相同**。

    这就是「共用同一个 SSOT」的可判形态:两条腿在同一份判定、同一个预留总额下
    必须算出同一个数。复制第二份实现时,两个数迟早会漂 —— 而漂的那一天没有
    判据会红,除非这里逐点比。
    """
    w = P03C.legacy_world(migrated_dsn, frozen=FROZEN)
    _sent, after = drive(live_server, w, monkeypatch,
                         impl_result=_complete_payload(w, verdict=_degraded_verdict()))

    assert after["freeze"]["status"] == "committed", after
    wallet = after["wallet"]
    assert wallet["frozen_points"] == 0, wallet
    # 🔴 钱包语义:``legacy_world`` 把 650 直接盖进 ``frozen_points``,``paid_points``
    #    没被扣过(生产里冻结当时已经从 paid 转进 frozen,夹具只是把终态摆好)。
    #    所以「扣了多少」不在 paid 的**减少**上,而在**退回**了多少:
    #      部分扣费 = frozen 出池 650,其中 162 转成消费、488 退回 paid。
    #    这个"退回额"恰恰是区分部分与全额的那一位,比"扣了多少"更有判别力。
    released_back = wallet["paid_points"] - w["paid"]
    assert released_back == FROZEN - EXPECTED_PARTIAL, (
        "个人腿降级交付退回 %r,应为 %r(= 冻结 %r − 已履约 %r)"
        % (released_back, FROZEN - EXPECTED_PARTIAL, FROZEN, EXPECTED_PARTIAL))
    assert released_back > 0, "退回额为 0 —— 那就是全额扣,这条判据没判别力了"


# ═══════════════════════════════════════════════════════════════════════════
# 组③:身份疑点 —— 两臂都不许自动结算
# ═══════════════════════════════════════════════════════════════════════════
def test_group3_org_arm_quarantines_on_identity_suspicion_instead_of_charging_full(
        live_server, migrated_dsn, monkeypatch, db):
    """疑似认错品牌 ⇒ org 单转人工,charge **不许**被 commit。

    自动全额结算等于拿我们自己的识别失误去扣客户组织的预算 ——
    个人腿早就是"既不自动扣也不自动退",组织腿照抄。
    """
    w = P03C.org_world(migrated_dsn)
    _sent, after = drive(live_server, w, monkeypatch,
                         impl_result=_complete_payload(w, identity_suspected=True))

    assert after["charge"]["status"] != "committed", (
        "身份疑点的单被自动结算了:%r" % (after["charge"],))
    assert after["charge"]["status"] == "reserved", (
        "charge 停在 %r —— 这一档应当原样冻着等人裁(既不扣也不退)"
        % after["charge"]["status"])
    assert after["run"]["run_status"] == "settlement_manual", after["run"]
    assert (after["run"]["last_settlement_error"] or "").startswith(
        "suspected_identity_failure"), after["run"]


def test_group3_personal_arm_quarantines_the_same_way(
        live_server, migrated_dsn, monkeypatch, db):
    """配对臂:个人腿同一档同样是转人工、冻结原样挂着。"""
    w = P03C.legacy_world(migrated_dsn, frozen=FROZEN)
    _sent, after = drive(live_server, w, monkeypatch,
                         impl_result=_complete_payload(w, identity_suspected=True))

    assert after["run"]["run_status"] == "settlement_manual", after["run"]
    assert after["freeze"]["status"] == "frozen", (
        "身份疑点这一档不许动钱,冻结却变成了 %r" % after["freeze"]["status"])
    assert after["wallet"]["frozen_points"] == FROZEN, after["wallet"]


# ═══════════════════════════════════════════════════════════════════════════
# 组④:全部成功 —— 行为与修之前一致(证明没改坏全履约那一档)
# ═══════════════════════════════════════════════════════════════════════════
def test_group4_org_arm_still_settles_the_full_ceiling_when_fully_delivered(
        live_server, migrated_dsn, monkeypatch, db):
    w = P03C.org_world(migrated_dsn)
    ceiling = int(w["organization_charge_points"])
    _sent, after = drive(live_server, w, monkeypatch)

    assert after["charge"]["status"] == "committed", after
    assert int(after["charge"]["actual_points"]) == ceiling, (
        "全履约的 org 单实扣 %r ≠ 上限 %r —— 按比例那条被误用到了全履约档上"
        % (after["charge"]["actual_points"], ceiling))


def test_group4_personal_arm_still_settles_in_full(live_server, migrated_dsn, monkeypatch, db):
    w = P03C.legacy_world(migrated_dsn, frozen=FROZEN)
    _sent, after = drive(live_server, w, monkeypatch)

    assert after["freeze"]["status"] == "committed", after
    # 全额扣:冻结全部转成消费,**一分都不退回**(对照上面那条的 488)。
    assert after["wallet"]["paid_points"] == w["paid"], (
        "全履约却退回了 %r —— 部分扣费那条被误用到全履约档上"
        % (after["wallet"]["paid_points"] - w["paid"],))
    assert after["wallet"]["frozen_points"] == 0, after["wallet"]


# ═══════════════════════════════════════════════════════════════════════════
# 组①:0% 成功 —— 上游就该拦下,绝不走到结算
# ═══════════════════════════════════════════════════════════════════════════
def test_group1_org_arm_zero_success_never_reaches_settlement(
        live_server, migrated_dsn, monkeypatch, db):
    """一个平台都没测成 ⇒ 上游 ``require_complete_product`` 抛,走 release 路由。

    关键在于 **charge 绝不能是 committed**:那意味着"没测出来还照收钱"。
    (实测的真行为是 ``unknown`` —— 外部副作用已报过,自动退被拒并隔离转人工。
     判据按**真行为**写,不按我以为的写;要害是它离开了 reserved 且没有被扣。)
    """
    w = P03C.org_world(migrated_dsn, successful_tests=0, total_score=0, level="隐形级")
    _sent, after = drive(live_server, w, monkeypatch)

    assert after["charge"]["status"] != "committed", (
        "零成功观测的单被结算了 —— 没测出来还收钱:%r" % (after["charge"],))
    assert after["charge"]["actual_points"] in (None, 0), after["charge"]


def test_group1_personal_arm_zero_success_is_refunded(
        live_server, migrated_dsn, monkeypatch, db):
    """配对臂:个人腿零成功观测 ⇒ 冻结**退回**,钱包复原。"""
    w = P03C.legacy_world(migrated_dsn, frozen=FROZEN, successful_tests=0,
                          total_score=0, level="隐形级")
    _sent, after = drive(live_server, w, monkeypatch)

    assert after["freeze"]["status"] == "released", (
        "零成功观测的单没退款,冻结停在 %r" % after["freeze"]["status"])
    assert after["wallet"]["frozen_points"] == 0, after["wallet"]
    assert after["wallet"]["paid_points"] == w["paid"] + FROZEN, (
        "退款没把冻结额**整笔**退回 paid 池:%r(应为 %r)"
        % (after["wallet"], w["paid"] + FROZEN))


# ═══════════════════════════════════════════════════════════════════════════
# 结构面:两臂**必须**是同一个谓词,不许有第二份实现
# ═══════════════════════════════════════════════════════════════════════════
def test_org_arm_calls_the_shared_predicate_not_a_second_copy(live_server):
    """AST:``dispatch_success_settlement`` 的 org 臂里必须有一次
    ``_partial_commit_points(...)`` 调用,而且**不许**把 ceiling 直接当
    ``actual_points`` 交出去。

    值层面的判据(组②)只能证"这次算对了";这一条钉住"算法只有一份"。
    两处各写一份时,两个数迟早会漂,而漂的那天没人会红。
    """
    _ = live_server
    import ast
    import inspect
    import textwrap

    import services.diagnosis_runs as dr

    fn = ast.parse(textwrap.dedent(inspect.getsource(dr.dispatch_success_settlement))).body[0]
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_partial_commit_points"]
    assert calls, "org 臂没有调共用谓词 —— 要么它没接线,要么有人复制了第二份实现"

    settle = [n for n in ast.walk(fn)
              if isinstance(n, ast.Call)
              and any(kw.arg == "actual_points" for kw in n.keywords)]
    assert settle, "找不到带 actual_points 的结算调用 —— 分母坏了,这条判据会恒绿"
    for node in settle:
        arg = [kw.value for kw in node.keywords if kw.arg == "actual_points"][0]
        src = ast.dump(arg)
        assert "organization_charge_points" not in src, (
            "actual_points 又被直接接到了 reserved ceiling 上 —— 那正是 P0-3 的形态")
    _ = (json, uuid)
