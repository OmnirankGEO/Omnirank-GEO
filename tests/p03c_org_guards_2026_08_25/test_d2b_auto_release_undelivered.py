"""#113 D2-b · unknown + 供应商成功但**未交付齐** ⇒ 自动释放冻结(资金语义变更)。

Owner 2026-09-06 两次拍板:
  ① 批准 D2-b(「供应商已成功但**未交付** ⇒ 释放冻结」);
  ② **放宽到部分交付也退**(见下)。

## 这是给保守口径开的一条窄门,不是放宽它

`_quarantine_locked_charge` 的原话:

    "An exception, timeout, or lost response after the provider boundary
     cannot prove that no work was performed."

它说的是**不能证明**。窄门要求拿出**能证明**的落库读数:

    delivered < expected               还没交付齐(= not full)
    provider_succeeded_attempts >= 1   供应商真出了东西(钱确实花了)
    in_flight_attempts          == 0   没有在途 —— 还有 attempt 在跑就可能还会交付

🔴 `delivered / expected` 由 `geo_factory.delivery_state` **一处判定**,
   与 `_settle_geo` 共用同一把尺 —— 窄门按 `not full` 决定退不退钱,
   终态按 `partial` 决定 job 写 succeeded 还是 failed;两处各算一遍就会出现
   「退了钱却标成功」或「标了部分成功却没退钱」,而这两种不一致都不会有东西报错。

## 🔴 放宽(②)的依据不是新规则

仓里正常路径 `_settle_geo` 对 partial 走的就是
`release_freeze("GEO 内容包部分成功整单免单")`。窄门只是让**恢复路径**用上
同一条既有规则。`full` 那一格**没有**放宽 —— 它才是真会赔钱的那格。

旧的「三计数 + assets == 0」证据已废弃;原先钉旧口径的两格
(`test_any_delivery_refuses_the_gate`)**改写**成
`test_full_delivery_refuses_the_gate` + `test_partial_delivery_is_admitted_after_the_widening`,
不是删除 —— 只删不补的话「放宽」就只剩正向臂。
## 本文件的重点是否定臂

资金判据最容易写成「能退钱」——那只证明了路是通的。
真正要守的是「**什么时候不许退**」:部分交付、证据缺失、租约还活着、
状态不是 unknown。这几条错一条,就是拿真钱去赌一个推断。
所以下面每条正向臂都配一条**零写入**否定臂:不仅要拒绝,还要证明**一分钱没动**。
"""

from __future__ import annotations

import asyncio
import io
import re
from pathlib import Path

import pytest

from tests.p03c_org_guards_2026_08_25._world import conn, org_world  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]

#: 🔴 [② Owner 2026-09-06 拍板放宽] 证据形状从「三计数 + 零交付」改成
#:    「delivered / expected + provider 成功 + 无在途」,窄门条件 = `not full`。
#:    依据不是新规则:仓里正常路径 `_settle_geo` 对 partial 走的就是
#:    `release_freeze("GEO 内容包部分成功整单免单")`。窄门只是让恢复路径
#:    用上同一条既有规则。
GOOD_EVIDENCE = {
    "delivered": 0,
    "expected": 2,
    "provider_succeeded_attempts": 1,
    "in_flight_attempts": 0,
}
#: 部分交付(795 的真形):2 件里交了 1 件 —— 放宽后**也放**。
PARTIAL_EVIDENCE = {**GOOD_EVIDENCE, "delivered": 1}
#: 全部交付 —— 任何时候都**不放**(用户拿全了,该扣钱)。
FULL_EVIDENCE = {**GOOD_EVIDENCE, "delivered": 2}


# ══════════════════════════════════════════════════════════════
# 1. 证据契约(纯函数 · 不碰库)
# ══════════════════════════════════════════════════════════════

def _assert_evidence(ev):
    from services.organization_billing import _assert_undelivered_evidence
    return _assert_undelivered_evidence(ev)


def test_good_evidence_passes_and_is_normalised_to_ints():
    assert _assert_evidence(GOOD_EVIDENCE) == GOOD_EVIDENCE


@pytest.mark.parametrize("missing", sorted(GOOD_EVIDENCE))
def test_any_missing_key_is_refused(missing):
    """三项缺一不可 —— 少一项就等于少一条独立证据。"""
    from services.organization_billing import OrganizationError
    ev = {k: v for k, v in GOOD_EVIDENCE.items() if k != missing}
    with pytest.raises(OrganizationError) as e:
        _assert_evidence(ev)
    assert e.value.code == "ORG_AUTO_RELEASE_EVIDENCE_INCOMPLETE"


def test_a_bool_cannot_masquerade_as_a_count():
    """🔴 `True` 是 `int` 的子类 —— 不显式挡掉的话它会被当成 1 悄悄通过。

    这条不是洁癖:证据是从别的模块传进来的,传参方哪天把
    `provider_succeeded_attempts` 写成 `bool(rows)`,窄门就会在
    「有任意一行 attempt」时成立,而不是「供应商真的成功过」。
    """
    from services.organization_billing import OrganizationError
    with pytest.raises(OrganizationError) as e:
        _assert_evidence({**GOOD_EVIDENCE, "provider_succeeded_attempts": True})
    assert e.value.code == "ORG_AUTO_RELEASE_EVIDENCE_INVALID"


def test_no_provider_success_is_refused():
    """供应商没成功 ⇒ 不适用本窄门(那是另一类:钱可能压根没花)。"""
    from services.organization_billing import OrganizationError
    with pytest.raises(OrganizationError) as e:
        _assert_evidence({**GOOD_EVIDENCE, "provider_succeeded_attempts": 0})
    assert e.value.code == "ORG_AUTO_RELEASE_NO_PROVIDER_SUCCESS"


def test_full_delivery_refuses_the_gate():
    """🔴 放宽后仍然守住的那条线:**交付齐了就不许自动退**。

    旧口径是「有任何一件交付过就拒」(`delivered_assets != 0`),
    Owner 2026-09-06 放宽成 `not full` —— partial 也放。
    但 `full` 这一格**没有**放宽,而且它才是真正会赔钱的那格:
    用户东西拿全了还把钱退回去。
    """
    from services.organization_billing import OrganizationError
    with pytest.raises(OrganizationError) as e:
        _assert_evidence(FULL_EVIDENCE)
    assert e.value.code == "ORG_AUTO_RELEASE_FULLY_DELIVERED"


def test_partial_delivery_is_admitted_after_the_widening():
    """🔴 放宽本身的正向臂 —— **795 的真形必须能过闸**。

    这条与 `test_full_delivery_refuses_the_gate` 是一对:
    只证「full 被拒」证明不了「partial 被放」,反之亦然。
    没有这一对,把窄门写成「全都拒」或「全都放」各有一半判据是绿的。
    """
    assert _assert_evidence(PARTIAL_EVIDENCE) == PARTIAL_EVIDENCE


def test_in_flight_generation_refuses_the_gate():
    """🔴 还有 attempt 在 pending/running ⇒ 供应商**可能还会交付**,
    此时退钱等于免费执行。这条是放宽之后新增的保守闸。
    """
    from services.organization_billing import OrganizationError
    with pytest.raises(OrganizationError) as e:
        _assert_evidence({**GOOD_EVIDENCE, "in_flight_attempts": 1})
    assert e.value.code == "ORG_AUTO_RELEASE_STILL_IN_FLIGHT"


def test_a_zero_expected_is_refused_not_silently_admitted():
    """🔴 `expected == 0` 时 `delivered < expected` 恒假、`>=` 恒真 ——
    **两个方向都不是保守**。必须显式拒绝,不让边界值决定钱的方向。
    """
    from services.organization_billing import OrganizationError
    with pytest.raises(OrganizationError) as e:
        _assert_evidence({**GOOD_EVIDENCE, "expected": 0})
    assert e.value.code == "ORG_AUTO_RELEASE_EVIDENCE_INVALID"

# ══════════════════════════════════════════════════════════════
# 2. 真库臂:钱**真的**回去了 / 真的没动
# ══════════════════════════════════════════════════════════════

def _quarantine(dsn, charge_id, *, lease_expired=True):
    """把真 charge 推进 unknown,并按需把租约弄过期。

    直接写列而不是走 `_quarantine_locked_charge`:后者要求外部副作用已开始,
    而本文件要测的是**释放**那一段,不是隔离那一段。
    隔离本身另有判据(#113 D2-a)。
    """
    c = conn(dsn)
    cur = c.cursor()
    lease = "NOW() - INTERVAL '1 hour'" if lease_expired else "NOW() + INTERVAL '1 hour'"
    cur.execute(
        "UPDATE organization_charge_links SET status='unknown',"
        " external_side_effect_started_at=COALESCE(external_side_effect_started_at,NOW()),"
        " lease_until=" + lease + " WHERE id=%s", (int(charge_id),))
    cur.execute("SELECT status, lease_until FROM organization_charge_links WHERE id=%s",
                (int(charge_id),))
    row = dict(cur.fetchone())
    c.close()
    assert row["status"] == "unknown", row
    return row


def _charge_facts(dsn, charge_id):
    c = conn(dsn)
    cur = c.cursor()
    cur.execute(
        "SELECT status, actual_points, amount_total, within_limit_points, overage_points"
        "  FROM organization_charge_links WHERE id=%s", (int(charge_id),))
    charge = dict(cur.fetchone())
    cur.execute(
        "SELECT COALESCE(SUM(reserved_points),0) AS r FROM organization_spend_limits"
        " WHERE organization_id=(SELECT organization_id FROM organization_charge_links"
        "                        WHERE id=%s)", (int(charge_id),))
    reserved = int(cur.fetchone()["r"])
    cur.execute("SELECT status FROM organization_work_outbox WHERE charge_link_id=%s",
                (int(charge_id),))
    outbox = [dict(r)["status"] for r in cur.fetchall()]
    cur.execute(
        "SELECT COUNT(*) AS c FROM organization_audit_events"
        " WHERE action='billing.auto_release_undelivered' AND entity_id=%s",
        (str(int(charge_id)),))
    audits = int(cur.fetchone()["c"])
    c.close()
    return {"charge": charge, "org_reserved": reserved, "outbox": outbox, "audits": audits}


def _auto_release(charge_id, evidence=None):
    from services.organization_billing import auto_release_undelivered_charge
    return asyncio.run(auto_release_undelivered_charge(
        charge_link_id=int(charge_id),
        reason="pytest D2-b",
        evidence=evidence if evidence is not None else GOOD_EVIDENCE,
        recovery_identity="pytest:d2b"))


def test_the_narrow_gate_actually_gives_the_money_back(live_server, live_dsn):
    """正向臂:钱回去了,而且是**终态读数**说的,不是返回值说的。

    🔴 返回 success 不等于钱动了 —— 所以断言打在 charge / spend_limits /
       outbox / 审计四处落库读数上。
    """
    world = org_world(live_dsn)
    cid = world["organization_charge_id"]
    before = _charge_facts(live_dsn, cid)
    assert before["charge"]["status"] == "reserved", before
    assert before["org_reserved"] > 0, "夹具没冻住钱 —— 分母塌了,不是通过"

    _quarantine(live_dsn, cid)
    out = _auto_release(cid)
    assert out["status"] == "released", out

    after = _charge_facts(live_dsn, cid)
    assert after["charge"]["status"] == "released"
    assert int(after["charge"]["actual_points"]) == 0, "自动释放不许留下扣费"
    assert int(after["charge"]["amount_total"]) == 0
    assert after["org_reserved"] < before["org_reserved"], (
        "组织上限的 reserved_points 没减 —— 钱在账面上还占着")
    assert after["outbox"] and set(after["outbox"]) == {"cancelled"}, after["outbox"]
    assert after["audits"] == 1, "审计没写 —— 三个月后没人答得出凭什么自动退的"


def test_a_second_call_is_idempotent_and_does_not_double_credit(live_server, live_dsn):
    """幂等臂:重放一次读数**一字不差**。

    没有这条,恢复链每 60 秒跑一次会把 spend_limits 一路扣成负数。
    """
    world = org_world(live_dsn)
    cid = world["organization_charge_id"]
    _quarantine(live_dsn, cid)
    _auto_release(cid)
    first = _charge_facts(live_dsn, cid)
    out2 = _auto_release(cid)
    assert out2.get("replayed") is True, out2
    assert _charge_facts(live_dsn, cid) == first, "重放改变了读数"


def test_a_live_lease_blocks_the_gate_and_moves_nothing(live_server, live_dsn):
    """🔴 零写入否定臂:租约还活着 ⇒ 拒绝,且**一分钱没动**。

    租约活着说明 worker 仍可能结算;此时释放会同时造成「免费执行」与「重复结算」。
    只断言抛异常是不够的 —— 要证明抛之前没有半截写入留下来。
    """
    from services.organization_billing import OrganizationError
    world = org_world(live_dsn)
    cid = world["organization_charge_id"]
    _quarantine(live_dsn, cid, lease_expired=False)
    before = _charge_facts(live_dsn, cid)
    with pytest.raises(OrganizationError) as e:
        _auto_release(cid)
    assert e.value.code == "ORG_CHARGE_LEASE_ACTIVE"
    assert _charge_facts(live_dsn, cid) == before, "被拒绝了,但库里已经动过手"


def test_full_delivery_blocks_the_gate_and_moves_nothing(live_server, live_dsn):
    """🔴 零写入否定臂:**交付齐了** ⇒ 拒绝,且一分钱没动。

    🔴 本条由旧的 `test_partial_delivery_blocks_the_gate_and_moves_nothing`
       **改写**而来,不是删除 —— Owner 2026-09-06 把窄门从「零交付」放宽到
       `not full`,于是 partial 从「必须拒」变成「必须放」。
       partial 那一格没有消失,它搬到了
       `test_partial_delivery_actually_gives_the_money_back` 里(方向相反)。
       只删不补的话,「放宽」这件事就只剩正向臂,没有任何东西守住 full。
    """
    from services.organization_billing import OrganizationError
    world = org_world(live_dsn)
    cid = world["organization_charge_id"]
    _quarantine(live_dsn, cid)
    before = _charge_facts(live_dsn, cid)
    with pytest.raises(OrganizationError) as e:
        _auto_release(cid, FULL_EVIDENCE)
    assert e.value.code == "ORG_AUTO_RELEASE_FULLY_DELIVERED"
    assert _charge_facts(live_dsn, cid) == before


def test_partial_delivery_actually_gives_the_money_back(live_server, live_dsn):
    """🔴 放宽的真库正向臂:**795 的真形**(2 件交了 1 件)钱必须回去。

    断言打在落库读数上,不打返回值 —— 返 success 不等于钱动了。
    """
    world = org_world(live_dsn)
    cid = world["organization_charge_id"]
    before = _charge_facts(live_dsn, cid)
    _quarantine(live_dsn, cid)
    out = _auto_release(cid, PARTIAL_EVIDENCE)
    assert out["status"] == "released", out
    after = _charge_facts(live_dsn, cid)
    assert after["charge"]["status"] == "released"
    assert int(after["charge"]["actual_points"]) == 0
    assert after["org_reserved"] < before["org_reserved"]
    assert after["audits"] == 1

def test_a_reserved_charge_is_not_auto_releasable(live_server, live_dsn):
    """还没进 unknown 的 charge 不走本路径(它有正常的 release 通道)。"""
    from services.organization_billing import OrganizationError
    world = org_world(live_dsn)
    cid = world["organization_charge_id"]
    before = _charge_facts(live_dsn, cid)
    with pytest.raises(OrganizationError) as e:
        _auto_release(cid)
    assert e.value.code == "ORG_CHARGE_NOT_AUTO_RELEASABLE"
    assert _charge_facts(live_dsn, cid) == before


# ══════════════════════════════════════════════════════════════
# 3. 接线:reconcile 必须**先试窄门再告警**
# ══════════════════════════════════════════════════════════════

def test_reconcile_tries_the_gate_before_giving_up():
    """顺序锁:窄门必须排在 `_alert_reconcile_gave_up` **之前**。

    反过来的话每次都会先惊动人,窄门就等于没开。
    按源码里两者的**出现次序**判,不是判"两个名字都在"——
    后者在顺序反了的时候同样为真。
    """
    import inspect

    from services.marketing import geo_factory

    src = inspect.getsource(geo_factory.reconcile_recoverable_geo_jobs)
    i_gate = src.find("_try_auto_release_undelivered")
    i_alert = src.find("_alert_reconcile_gave_up")
    assert i_gate != -1, "窄门没接进 reconcile"
    assert i_alert != -1, "D2-a 的告警不见了 —— 窄门不该替换掉它"
    assert i_gate < i_alert, "窄门排在了告警后面 —— 每次都会先惊动人"


@pytest.mark.parametrize("evidence,expect_release,why", [
    ({"delivered": 0, "expected": 2, "provider_succeeded_attempts": 1,
      "in_flight_attempts": 0}, True,  "零交付 —— 放宽前后都放"),
    ({"delivered": 1, "expected": 2, "provider_succeeded_attempts": 1,
      "in_flight_attempts": 0}, True,  "部分交付 —— 放宽后才放(795 的真形)"),
    ({"delivered": 2, "expected": 2, "provider_succeeded_attempts": 1,
      "in_flight_attempts": 0}, False, "交付齐了 —— 任何时候都不放"),
    ({"delivered": 3, "expected": 2, "provider_succeeded_attempts": 1,
      "in_flight_attempts": 0}, False, "超交付 —— 也算 full"),
    ({"delivered": 0, "expected": 2, "provider_succeeded_attempts": 0,
      "in_flight_attempts": 0}, False, "供应商没成功 —— 另一类"),
    ({"delivered": 0, "expected": 2, "provider_succeeded_attempts": 1,
      "in_flight_attempts": 1}, False, "还有在途 —— 可能还会交付"),
    ({"delivered": 0, "expected": 0, "provider_succeeded_attempts": 1,
      "in_flight_attempts": 0}, False, "expected=0 —— 边界值不许决定钱的方向"),
    (None, False, "证据读不到 —— 保守"),
])
def test_the_gate_decides_from_the_evidence(monkeypatch, evidence, expect_release, why):
    """窄门的判定必须由**证据**驱动 —— 八格逐格走,不是只走能过的那格。

    🔴 放宽后这张表整体重写:第 2 行(部分交付)从 False 翻成 True,
       并补了「超交付」「在途」「expected=0」三格。`why` 列是给读的人的,
       也是给我自己的:每一格都要答得出「它为什么在这儿」。
    """
    from services.marketing import geo_factory

    called = []
    monkeypatch.setattr(geo_factory, "_undelivered_provider_success_evidence",
                        lambda job: evidence)

    async def _fake_release(**kw):
        called.append(kw)
        return {"status": "released"}

    import services.organization_billing as ob
    monkeypatch.setattr(ob, "auto_release_undelivered_charge", _fake_release)
    got = asyncio.run(geo_factory._try_auto_release_undelivered(
        job={"id": 1}, billing_ref="org:4242"))
    assert got is expect_release, (why, evidence, got)
    assert bool(called) is expect_release, why
    if expect_release:
        assert called[0]["charge_link_id"] == 4242
        assert called[0]["evidence"] == evidence

def test_a_legacy_freeze_ref_never_enters_the_gate(monkeypatch):
    """作用域锁:只认 `org:` 前缀。legacy 冻结不走这套状态机。

    反向对照:没有这条,「窄门不成立」与「作用域被悄悄扩大到 legacy」同形。
    """
    from services.marketing import geo_factory

    probed = []
    monkeypatch.setattr(geo_factory, "_undelivered_provider_success_evidence",
                        lambda job: probed.append(job) or dict(GOOD_EVIDENCE))
    assert asyncio.run(geo_factory._try_auto_release_undelivered(
        job={"id": 1}, billing_ref="legacy:99")) is False
    assert probed == [], "legacy ref 也去取证据了 —— 作用域漏了"


def test_a_failing_release_never_escapes_into_the_reconcile_loop(monkeypatch):
    """资金动作抛异常 ⇒ 返回 False 走人工路径,**不许**把整轮 reconcile 带塌。"""
    from services.marketing import geo_factory

    monkeypatch.setattr(geo_factory, "_undelivered_provider_success_evidence",
                        lambda job: dict(GOOD_EVIDENCE))

    async def _boom(**kw):
        raise RuntimeError("physical release failed")

    import services.organization_billing as ob
    monkeypatch.setattr(ob, "auto_release_undelivered_charge", _boom)
    assert asyncio.run(geo_factory._try_auto_release_undelivered(
        job={"id": 1}, billing_ref="org:1")) is False


# ══════════════════════════════════════════════════════════════
# 4. 漂移锁:三份释放腿实现必须保持语义一致
# ══════════════════════════════════════════════════════════════

def _normalise(body: str) -> str:
    """剥注释、归一化空白与尾逗号 —— 只比**语义**,不比排版。"""
    out = []
    for line in body.split("\n"):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    t = re.sub(r"\s+", " ", " ".join(out))
    t = re.sub(r"\s*([(),])\s*", r"\1", t)
    t = t.replace(",)", ")")
    # 🔴 `int(...)` 包裹在这里是**保语义**的:被包的都是整数列
    #    (charge_link_id / link["reserved_ceiling_points"]),两种写法在库里
    #    产生完全相同的写入。归一掉它是为了让锁只对**行为**敏感,不对排版敏感 ——
    #    一把因为有人加了个 int() 就红的锁,会被下一个人当噪音关掉。
    #    ⚠️ 归一范围**限定**在这两种形状,不做通用 `int(` 剥离:
    #    通用剥离会把 `int(x/y)` 这类真会改语义的调用也抹平。
    t = t.replace("int(charge_link_id)", "charge_link_id")
    # 🔴 已知别名:D2-b 那份把 `link["automatic_plan_occurrence_id"]` 提成了局部
    #    变量 `occurrence_id`(early-return 需要)。这是**同一个值**,不是行为差异。
    #    归一它,让锁只对 SQL 与参数顺序敏感。
    #    ⚠️ 必须带左边界:`occurrence_id` 是 `automatic_plan_occurrence_id` 的**子串**,
    #    裸 replace 会把后者撑成 `link["automatic_plan_link["automatic_plan_...`。
    t = re.sub(r'(?<![A-Za-z0-9_])occurrence_id', 'link["automatic_plan_occurrence_id"]', t)
    t = re.sub(r'int\((link\["[a-z_]+"\])\)', r"\1", t)
    return t


def _slice(src: str, func: str, start_marker: str, end_marker: str) -> str:
    """切出两个标记之间的源码。

    🔴 端点**必须包含 end_marker 本身**。第一版用 `reserved_ceiling_points`
       当端点又不含它,而它正好落在 `int(link["reserved_ceiling_points"])`
       **内部** ⇒ 切出来是半截 `...(int(link["`,归一化再怎么写都对不上。
       症状长得像「两份实现分家了」,实际是尺子把 token 劈开了。
    """
    i = src.index("async def %s(" % func)
    a = src.index(start_marker, i)
    b = src.index(end_marker, a) + len(end_marker)
    return _normalise(src[a:b])


def test_the_three_release_implementations_have_not_drifted():
    """🔴 本仓有**三份**同语义的「释放计划 occurrence」实现:
    `release_charge` / `force_release_charge` 体内各一份,加上 D2-b 用的
    `_recover_plan_occurrence_after_release`。

    正确做法是抽成一处。**没有抽**,理由记在
    `_recover_plan_occurrence_after_release` 的 docstring 里:
    `force_release_charge` 的判据住在 `tests/organization_internal_seats/
    test_payer_policy_pg.py`,该包在本机跑不起来(迁移写死 `public.` 与它的
    per-run schema 冲突)。**重构一段我无法回归验证的资金代码,比留一份重复更危险。**

    代价由这条锁承担:三份归一化后必须逐字相同。任何一份被改都会当场变红
    并点名另外两份 —— 把「静默分家」换成「响的失败」。
    这不是消灭了那根轴,是给它装了报警;抽取仍是该做的事,只是要等能验的时候。
    """
    src = io.open(ROOT / "services" / "organization_billing.py", encoding="utf-8").read()
    # 🔴 三份**从同一条 SQL 起算**,不从各自的外层条件起算:
    #    D2-b 那份用 early-return,另两份用 `if link.get(...)` 包裹 ——
    #    那是控制流写法差异,不是行为差异。把起点对齐后,剩下的任何差异都是真的。
    M0 = "SELECT plan_id,planned_at"
    # ⚠️ 端点不能用 `occurrence["plan_id"]`:它在**第一条** SQL 里就出现了,
    #    切片会提前结束(实测只切到 400 字符,三份都是半截)。
    #    `occurrence["planned_at"]` 只在最后那条 UPDATE 的参数里出现一次。
    M1 = 'occurrence["planned_at"]'
    a = _slice(src, "release_charge", M0, M1)
    b = _slice(src, "force_release_charge", M0, M1)
    i = src.index("def _recover_plan_occurrence_after_release(")
    c = _normalise(src[src.index(M0, i):src.index(M1, i) + len(M1)])
    assert min(len(a), len(b), len(c)) > 900, (
        "切片太短,锚点很可能没命中 —— 分母塌了,不是通过:%d/%d/%d"
        % (len(a), len(b), len(c)))
    assert a == b, "release_charge 与 force_release_charge 的计划腿已分家"
    assert a == c, "D2-b 的计划腿与另外两份分家了 —— 三份必须同语义"
