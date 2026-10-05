"""外选审计坐实的判据洞 —— 逐发补判据(EXTSEL_WOA · 2026-08-26)。

独立变异审计员对 `b65850214` + `58c089ee2` 出了 15 发外选变异,其中 **10 发存活**:
EXTA-01/02/03/04/05/06/07/08/09/14。存活 = 我那 41 条判据的洞,不是代码没问题。

洞的形态高度集中,值得先说清楚,因为补法完全由形态决定:

  · **某一方向零样本**(01/03/09):fail-closed 的 except 臂 / 比对的另一个方向
    从来没有样本踩过。修复写对了,但「写对了」这件事没有判据在守。
  · **某一态零样本**(07/08/14):from-状态集只被单一状态踩过、区间断言没有边界
    样本、三态检查只造了两态。少测的那一态恰好是防篡改/防呆的那一态。
  · **分母漏项**(02):`requires_paid_points` 进了 hash,却没有任何判据翻转它。
    「手写分母漏掉的那一项不会让任何判据变红」的教科书形态。
  · **census 够不到的层**(05/06):`test_payer_census` 的 offender 判定只看
    **调用点 segment**;毒在**赋值语句**上,segment 里只剩干净的局部名 `payer`,
    三条 census 判据全绿。而功能面当时根本没有 payer≠owner 的退款判据。
    🔴 Review 明令:05/06 必须**驱动真退款链**,不许加字符串锁 —— 加锁只会
    把 census 的盲区往外挪一格,下一发毒在别的语句上照样看不见。
  · **笛卡尔格空缺**(04):夹具永远供 `amount`,生产可能不供
    (本仓「夹具供了生产不会供的东西」的镜像面)。

每条判据都按本包既有纪律配**配对的必须不命中**:只证「该红时红」会放过恒抛/恒拒
的坏实现,只证「该绿时绿」会放过恒放行的坏实现。
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest
from fastapi.testclient import TestClient

from tests.defgeo_funding_p0_2026_08_25 import _world as W

pytestmark = pytest.mark.integration

COST = 650
COST_LOWERED = 500          # ← 外选 EXTA-03 打的就是这个方向(既有判据只有涨价臂)


@pytest.fixture()
def world(db, migrated_dsn, monkeypatch):
    """与 a2 同构的个人腿世界。价目是全局单行,跑完必须还原。"""
    tenant, _a, _b = W.fresh_uids(3)
    with db.cursor() as cur:
        W.ensure_user(cur, tenant, "extsel_payer_%d" % tenant)
        W.ensure_wallet(cur, tenant, paid=1_000_000)
        W.set_pricing(cur, COST)
        brand_id = W.new_brand(cur, tenant)
    monkeypatch.setattr("auth.brand_access.require_brand_access", lambda *a, **k: None)
    yield {"tenant": tenant, "brand_id": brand_id, "dsn": migrated_dsn}
    with db.cursor() as cur:
        W.set_pricing(cur, COST)
        cur.execute("UPDATE feature_pricing SET requires_paid_points=false "
                    "WHERE feature_code=%s", (W.FEATURE,))


@pytest.fixture()
def client():
    return TestClient(W.make_app(), raise_server_exceptions=False)


def _disable_version_gate(monkeypatch):
    """把保险丝①(版本闸)打回**修复前**的恒定常量形态,好让请求真的走到保险丝②。

    🔴 顺序要紧:必须在 **make_preview 之前** 打。版本闸比的是
       `preview 行里存的串` vs `confirm 时刻算出来的串` —— preview 之后才打,
       preview 存的是真串、confirm 算出常量,两者不等,①**照样 409**,
       ②仍然没被执行过。本仓「两把锁叠同一路径:存活时别以为是自己那把在守」。
       口径与 a2 的同名 helper 逐字一致(那边已按此顺序用了三处)。
    """
    import api.defensive_geo_api as mod

    # [E2-2] 同 a2:补丁点搬到 canonical 单点(confirm 已不再调
    #   `_live_pricing_catalog_version`,补丁钉在旧接缝上会让这些判据恒 409)。
    monkeypatch.setattr(mod, "_pricing_catalog_version_from_row",
                        lambda *a, **k: "extsel-frozen-const")


# ═══════════════════════════════════════════════════════════════════════════
# EXTA-01 · 价目 **DB 异常臂** 必须 fail-closed(既有判据只驱动了 row-None 臂)
# ═══════════════════════════════════════════════════════════════════════════
def _poisoned_connection(dsn):
    """一条**真** psycopg2 连接,事务已被真实错误打废。

    为什么不用假对象:被测的是「execute 抛真实数据库异常时那条 except 臂干什么」。
    真连接 + 真 `InFailedSqlTransaction` 与生产形态一致(库抖动/改价窗口/连接被打断),
    而且 `finally: conn.close()` 也照常走得通 —— 假对象会把 finally 这一段一起绕开。
    """
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1/0")            # 真错误 → 事务进 aborted 态
    except psycopg2.Error:
        pass
    return conn


def test_x01_a_real_database_error_reading_the_catalog_is_fail_closed(
        world, live_server, monkeypatch):
    """价目**读取抛异常**时必须抛 `PricingCatalogUnreadable`,不许回落成任何串。

    洞在哪:`test_unreadable_catalog_is_fail_closed_not_a_fallback` 用的是一个
    **不存在的 feature_code** —— 那走的是 `row is None` 那条 `if`,
    **根本没进 try/except**。except 臂(库真的读不动)从上线起零样本。

    坏结果:库抖动/改价窗口里,preview 与 confirm 各自算出**同一个**回落串 ⇒
    保险丝①在最需要它的那一刻静默失效。交付文自己写了「回落等于把 P0-2 原样放回去」,
    只是那句话当时落在了没人测的那条臂上。
    """
    _ = live_server
    import api.defensive_geo_api as mod
    import db.connection as dbconn

    poisoned = _poisoned_connection(world["dsn"])
    # 自证夹具:这条连接**确实**会让 SELECT 抛,否则下面那条会因为错误的原因绿。
    with pytest.raises(psycopg2.Error):
        poisoned.cursor().execute("SELECT 1")

    monkeypatch.setattr(dbconn, "get_connection", lambda *a, **k: poisoned)
    try:
        with pytest.raises(mod.PricingCatalogUnreadable) as err:
            mod._live_pricing_catalog_version(W.FEATURE)
    finally:
        try:
            poisoned.close()
        except Exception:
            pass
    assert "价目读取失败" in str(err.value) or W.FEATURE in str(err.value), (
        "抛了,但抛的不是「读取失败」那一支:%s" % err.value)


def test_x01b_a_healthy_catalog_still_returns_a_derived_version(world, live_server):
    """配对的必须不命中:库好的时候必须**照常算得出来**。

    少了它,一个「无条件抛 PricingCatalogUnreadable」的实现也能让上面那条绿 ——
    而那样每一次 preview/confirm 都会翻成 POLICY_UNAVAILABLE,功能整个停摆。
    """
    _ = (world, live_server)
    import api.defensive_geo_api as mod

    got = mod._live_pricing_catalog_version(W.FEATURE)
    assert isinstance(got, str) and got, "健康库上没算出版本串"
    assert "unreadable" not in got.lower(), (
        "健康库上算出来的版本串里带 unreadable(%r)—— 像是回落分支的产物" % got)


# ═══════════════════════════════════════════════════════════════════════════
# EXTA-02 · requires_paid_points 也必须进版本(hash 分母漏项)
# ═══════════════════════════════════════════════════════════════════════════
def _set_requires_paid(db, value):
    with db.cursor() as cur:
        cur.execute("UPDATE feature_pricing SET requires_paid_points=%s "
                    "WHERE feature_code=%s", (bool(value), W.FEATURE))
        assert cur.rowcount == 1, "价目行不在,这条判据在守空气"


def test_x02_flipping_requires_paid_points_moves_the_catalog_version(
        world, db, live_server):
    """翻转 `requires_paid_points` ⇒ 版本串必须变;翻回去 ⇒ 必须变回来。

    洞在哪:`test_catalog_version_moves_when_the_real_price_moves` 只动 `cost_points`。
    全包**没有任何一条判据翻转过 requires_paid_points**,而它是 hash 分母的第三项。

    坏结果:`requires_paid_points` 决定冻结**从哪几个池扣钱**(源码 docstring 原话)。
    改它不改版本 ⇒ 她在确认页看到的「从哪个池扣」与实际冻结不一致,confirm 照常放行。
    这是「手写分母漏掉的那一项不会让任何判据变红」的原样复现:少的那一项不会让
    既有的 11 条 a2 判据里任何一条红。

    下半条(翻回去要变回来)是配对的必须不命中:少了它,一个每次调用都返随机串的
    实现也能让上半条绿,而那会让所有 confirm 恒 409。
    """
    _ = live_server
    from api.defensive_geo_api import _live_pricing_catalog_version as ver

    _set_requires_paid(db, False)
    before = ver(W.FEATURE)
    _set_requires_paid(db, True)
    after = ver(W.FEATURE)
    assert before != after, (
        "翻转 requires_paid_points 后版本串没变(%r)—— 它掉出了 canonical 分母,"
        "改「从哪个池扣钱」这件事在确认这一步是看不见的" % before)
    _set_requires_paid(db, False)
    assert ver(W.FEATURE) == before, (
        "翻回 false 后版本没回到 %r —— 版本不是内容的函数" % before)


# ═══════════════════════════════════════════════════════════════════════════
# EXTA-03 · 金额保险丝必须**双向**(既有两条 fuse 判据只有涨价臂)
# ═══════════════════════════════════════════════════════════════════════════
def test_x03_the_amount_fuse_also_fires_when_the_freeze_is_cheaper(
        client, world, db, live_server, monkeypatch):
    """确认 650、实冻 500(**降价**竞态)必须整笔回滚,不许成交。

    洞在哪:`..._rolls_the_whole_confirm_back` 与 `..._also_covers_the_platform_leg`
    造的都是 `COST_RAISED > COST` 的**涨价**臂;等价臂与 0 价臂在 `>` 下同样绿。
    所以把 `!=` 松成 `>` —— 只砍掉「少冻」这一个方向 —— 11 条 a2 判据一条都不红。

    坏结果:账面 650、物理冻结 500,响应仍显示 650 ⇒ 账面与物理脱钩,
    平台少收,且后续部分履约按**错的基数**算比例。
    """
    _ = live_server
    _disable_version_gate(monkeypatch)                   # 🔴 必须在 preview 之前
    prev = W.make_preview(client, world["tenant"], world["brand_id"])
    assert prev["exactTotalPoints"] == COST, prev

    with db.cursor() as cur:                             # 真降价:freeze 会按 500 冻
        W.set_pricing(cur, COST_LOWERED)

    before = W.Counts(db)
    r = W.confirm(client, world["tenant"], prev)
    after = W.Counts(db)

    assert r.status_code >= 400, (
        "实冻 %d ≠ 确认 %d,confirm 却成交了(%s)—— 金额保险丝只守了涨价一个方向"
        % (COST_LOWERED, COST, r.status_code))
    assert before.delta(after) == (0, 0, 0), (
        "被拒了但留下了副作用 runs/freezes/outbox=%s —— 必须整笔回滚"
        % (before.delta(after),))


def test_x03b_a_matching_freeze_still_confirms(client, world, db, live_server, monkeypatch):
    """配对的必须不命中:金额对得上时必须照常成交。

    少了它,一个「无条件拒」的保险丝也能让上面那条绿 —— 而那样没有一单能确认。
    """
    _ = live_server
    _disable_version_gate(monkeypatch)
    prev = W.make_preview(client, world["tenant"], world["brand_id"])
    r = W.confirm(client, world["tenant"], prev)
    assert r.status_code == 200, (
        "价格一分没动,confirm 却被拒了(%s %s)" % (r.status_code, r.text[:200]))


# ═══════════════════════════════════════════════════════════════════════════
# EXTA-04 · billing 返回体**缺 amount** 时必须 fail-closed
# ═══════════════════════════════════════════════════════════════════════════
def test_x04_a_freeze_result_without_an_amount_is_refused_not_assumed_equal(
        client, world, db, live_server, monkeypatch):
    """`freeze_points` 返回体里没有 `amount` 时,保险丝②必须**拒**,不许假定等额。

    洞在哪:现役 billing 恒返 `amount`,全部判据里它都在且非缺;0 价判据下
    `0 or 0` 与 `0 or _exact(=0)` 同值。所以把 `or 0` 改成 `or _exact_confirmed`
    —— 对「读不到实冻额」这一形态从守门变成放行 —— 零判据会红。

    坏结果:哪天 billing 版本漂移 / 新腿接入不带这个键,保险丝②对**最该拦的那一形态**
    (根本不知道实际冻了多少)自动通过。本仓「夹具供了生产不会供的东西」的镜像:
    这里是夹具永远供、生产可能不供。

    造法:**真的冻**(调真 freeze_points),只把返回体里的 `amount` 摘掉 ——
    只改「被测代码看到什么」,不改「钱怎么动」。
    """
    _ = live_server
    import middleware.billing as billing

    real = billing.freeze_points

    async def _no_amount(*a, **kw):
        got = dict(await real(*a, **kw))
        got.pop("amount", None)
        return got

    monkeypatch.setattr(billing, "freeze_points", _no_amount)

    _disable_version_gate(monkeypatch)
    prev = W.make_preview(client, world["tenant"], world["brand_id"])
    before = W.Counts(db)
    r = W.confirm(client, world["tenant"], prev)
    after = W.Counts(db)

    assert r.status_code >= 400, (
        "billing 没告诉我实际冻了多少,confirm 却成交了(%s)—— "
        "保险丝②把「读不到」当成了「对得上」" % r.status_code)
    assert before.delta(after) == (0, 0, 0), (
        "被拒了但留下副作用 runs/freezes/outbox=%s" % (before.delta(after),))


# ═══════════════════════════════════════════════════════════════════════════
# EXTA-05 / EXTA-06 · payer≠owner 的**真退款链**
#
# 🔴 Review 明令:这两条必须驱动真链,不许加字符串锁。理由值得写下来 ——
#    这两发之所以存活,正是因为 `test_payer_census` 的 offender 判定只看
#    **调用点 segment**:毒打在**赋值语句**上(`payer = run.get("owner_user_id")`),
#    三条 execute 的 segment 里只剩一个干净的局部名 `payer`,census 反而认它合规。
#    再加一把「赋值语句也要扫」的字符串锁,只是把盲区往外挪一格 ——
#    下一发毒在别的语句形态上照样看不见。能钉死它的只有**钱真的退给了谁**。
#
# 前置事实一律真造:真冻结(freeze_points)、真结算(commit_run → CMT consume 流水)、
# 真退款(middleware.billing.refund_points → type='refund' 流水)。
# 只有 `run_status='delivery_repair_pending'` 这一跳是直接摆的 —— 它的真实来路是
# 「已结算但产物被并发删」(_finalize 里 vis_rows==0 那一支),与本判据要验的
# 「退款核验按谁定位」无关,造那个竞态只会让判据变脆。refund_pending 标记本身
# 仍走**真** repair_delivery(writeoff)。
# ═══════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def platform_world(db, migrated_dsn, monkeypatch):
    """admin 发起 → 平台承担腿:payer(平台直营账号) **≠** owner(admin 租户)。"""
    admin_uid, platform_uid, _ = W.fresh_uids(3)
    with db.cursor() as cur:
        W.ensure_user(cur, admin_uid, "extsel_admin_%d" % admin_uid, admin=True)
        W.ensure_user(cur, platform_uid, "extsel_plat_%d" % platform_uid)
        W.ensure_wallet(cur, admin_uid, paid=1_000_000)
        W.ensure_wallet(cur, platform_uid, paid=1_000_000)
        W.set_pricing(cur, COST)
        brand_id = W.new_brand(cur, admin_uid)
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", str(platform_uid))
    monkeypatch.setattr("auth.brand_access.require_brand_access", lambda *a, **k: None)
    return {"admin_uid": admin_uid, "platform_uid": platform_uid, "brand_id": brand_id}


def _committed_platform_run(client, db, w):
    """平台腿:confirm → 真冻结 → 真结算(committed)。返回 run 行。"""
    import asyncio

    from services.diagnosis_runs import commit_run

    prev = W.make_preview(client, w["admin_uid"], w["brand_id"], admin=True)
    r = W.confirm(client, w["admin_uid"], prev, admin=True)
    assert r.status_code == 200, r.text
    token = r.json()["diagnosisCommandId"]
    run = W.run_row(db, token)
    assert int(run["payer_user_id"]) == w["platform_uid"] != int(run["owner_user_id"]), (
        "这条判据的前提是 payer≠owner,实际 payer=%r owner=%r —— 前提不成立就白测了"
        % (run.get("payer_user_id"), run.get("owner_user_id")))
    W.seed_product(db, run)
    out = asyncio.run(commit_run(token, {"type": "complete", "done": True}))
    assert out.get("terminal") == "committed", out
    return W.run_row(db, token)


def _to_refund_pending(db, run, operator="extsel-ops"):
    """真 writeoff 打出 refund_pending 标记(只有 run_status 那一跳是摆的)。"""
    from services.diagnosis_runs import repair_delivery

    with db.cursor() as cur:
        cur.execute("UPDATE diagnosis_runs SET run_status='delivery_repair_pending' "
                    "WHERE run_token=%s", (run["run_token"],))
        assert cur.rowcount == 1
    out = repair_delivery(run["run_token"], operator, "writeoff", "产物不可恢复 · 判据造场")
    assert out.get("ok"), out
    row = W.run_row(db, run["run_token"])
    assert (row["last_settlement_error"] or "").startswith("refund_pending:"), row
    return row


def _real_refund(db, run, payer_uid):
    """走**真** refund_points 退这一笔 committed 扣费,返回退款流水 id。"""
    import asyncio

    from middleware.billing import refund_points

    with db.cursor() as cur:
        cur.execute("SELECT id, amount FROM point_transactions WHERE type='consume' "
                    "AND user_id=%s AND order_id LIKE %s",
                    (int(payer_uid), "CMT%d-%%" % int(run["freeze_id"])))
        consumes = cur.fetchall()
    assert consumes, (
        "平台钱包上找不到本冻结的 CMT 扣费流水 —— 结算那一步没真扣,后面全是空转")
    charge_tx_id = int(consumes[0]["id"])

    asyncio.run(refund_points(int(payer_uid), W.FEATURE, "extsel 判据:真退款",
                              charge_tx_id=charge_tx_id, ledger_type="legacy"))
    with db.cursor() as cur:
        cur.execute("SELECT id, amount, point_type FROM point_transactions "
                    "WHERE type='refund' AND user_id=%s ORDER BY id DESC", (int(payer_uid),))
        refunds = cur.fetchall()
    assert refunds, "refund_points 跑完却没有 type=refund 流水 —— 退款没真发生"
    return int(refunds[0]["id"])


def test_x05_the_refund_verification_locates_the_freeze_by_payer_not_owner(
        client, platform_world, db, live_server):
    """平台承担腿的**真实退款**必须核得出来 —— 核验链按 payer 定位。

    洞在哪:`_verify_ledger_refund` 里 payer 是在**赋值语句**上取的,
    census 的 offender 判定只看调用点 segment ⇒ 把它改回 `run.get("owner_user_id")`,
    三条 census 判据全绿;而功能面当时没有任何 payer≠owner 的退款判据
    (defgeo 包与 delivery_integrity 都只驱动过 republish)。

    坏结果:冻结行、CMT 扣费流水、refund 流水**全在平台钱包上**,拿 owner(租户)去查
    三处都查空 ⇒ 人工确认退款恒 fail-closed 拒 ⇒ 平台真退出去的钱永远核销不掉,
    run 永久卡 refund_pending + 每小时 stuck 告警。个人腿 owner==payer,全绿如常 ——
    所以这个 bug 只在平台腿上现形,而平台腿此前只跑过全额结算。
    """
    _ = live_server
    from services.diagnosis_runs import repair_delivery

    run = _committed_platform_run(client, db, platform_world)
    run = _to_refund_pending(db, run)
    tx = _real_refund(db, run, platform_world["platform_uid"])

    out = repair_delivery(run["run_token"], "extsel-ops", "confirm_refund",
                          "真退款流水已核对 · 判据造场", refund_tx_id=str(tx))
    assert out.get("ok"), (
        "平台腿的真退款核验失败:%r —— 冻结/扣费/退款三条流水都在平台钱包(uid=%s)上,"
        "核验链却按 owner(uid=%s)定位"
        % (out, platform_world["platform_uid"], run["owner_user_id"]))


def test_x06_the_refund_record_says_the_money_went_back_to_the_payer(
        client, platform_world, db, live_server):
    """`diagnosis_refund_records` 那一列记的是「钱退回**谁的钱包**」= payer。

    洞在哪:这条 INSERT 的 SQL 里不含 census 认的任何一个表名 mark
    ⇒ **根本不进 census 的分母**,offender 检查看不见它。

    坏结果:平台腿退款被记成「退给了租户」—— 资金审计轨迹指错人:
    对账时平台的退款凭空消失,租户名下多出一笔从没发生过的退款。
    钱本身退对了(refund_points 是按 payer 退的),**错的是账**,
    所以任何只看钱包余额的判据都抓不到它。
    """
    _ = live_server
    from services.diagnosis_runs import repair_delivery

    run = _committed_platform_run(client, db, platform_world)
    run = _to_refund_pending(db, run)
    tx = _real_refund(db, run, platform_world["platform_uid"])
    out = repair_delivery(run["run_token"], "extsel-ops", "confirm_refund",
                          "真退款流水已核对 · 判据造场", refund_tx_id=str(tx))
    assert out.get("ok"), out

    with db.cursor() as cur:
        cur.execute("SELECT owner_user_id, points FROM diagnosis_refund_records "
                    "WHERE run_token=%s", (run["run_token"],))
        rec = cur.fetchone()
    assert rec is not None, "confirm 说成功了,退款记录却没落库"
    assert int(rec["owner_user_id"]) == int(platform_world["platform_uid"]), (
        "退款记录记成退给了 uid=%s,而钱实际退回的是平台钱包 uid=%s —— "
        "这一列的语义是「钱退回谁的钱包」,不是「这单挂在谁名下」"
        % (rec["owner_user_id"], platform_world["platform_uid"]))


def test_x05b_a_refund_transaction_from_another_run_is_still_refused(
        client, platform_world, db, live_server):
    """配对的必须不命中:别人家的退款流水**不许**核销本 run。

    少了它,一个「无条件返 ok」的核验也能让 x05 绿 —— 而那正是这段代码最怕的形态
    (手填凭证标记已退)。这条同时钉住:x05 的绿不是因为核验被架空了。
    """
    _ = live_server
    from services.diagnosis_runs import repair_delivery

    run_a = _committed_platform_run(client, db, platform_world)
    run_a = _to_refund_pending(db, run_a)
    _real_refund(db, run_a, platform_world["platform_uid"])

    run_b = _committed_platform_run(client, db, platform_world)
    run_b = _to_refund_pending(db, run_b)
    tx_b = _real_refund(db, run_b, platform_world["platform_uid"])

    # 拿 B 的退款流水去核销 A —— 必须拒。
    out = repair_delivery(run_a["run_token"], "extsel-ops", "confirm_refund",
                          "拿别的 run 的退款流水来核销 · 应当被拒", refund_tx_id=str(tx_b))
    assert not out.get("ok"), (
        "用 run_b 的退款流水核销了 run_a(%r)—— 一笔退款流水被两个 run 各核销一次,"
        "对账时会凭空多出一笔退款" % out)


# ═══════════════════════════════════════════════════════════════════════════
# EXTA-07 / 08 / 09 / 14 · 「某一态零样本」四发
#
# 这四发的共同形态:被测的判断**写对了**,但它的某一个输入态从上线起没有样本 ——
# from-状态集只被单一状态踩过、区间断言两端都没样本、except 臂零样本、
# 三态检查只造了两态。少测的那一态每次都恰好是**防呆/防篡改**的那一态。
# ═══════════════════════════════════════════════════════════════════════════
from tests.p03c_org_guards_2026_08_25 import _world as P03C          # noqa: E402
from tests.p03c_org_guards_2026_08_25.test_settlement_dispatch_runtime import (  # noqa: E402
    drive,
)

RATIO = 0.25
FROZEN_MULTI = 650


def _verdict(ratio, *, planned=4, succeeded=1):
    """[E2-1] 判定形状照生产持久化那两处逐字来,含 planned/succeeded 整数计数。

    `ratio` 仍是入参:x08 那条要验的就是「比例越界(0.0)必须转人工」,
    而越界检查发生在取整数计数**之前**,所以那条判据仍然打得到它要打的那一行。

    🔴 默认计数 1/4 与默认 RATIO(0.25)**精确自洽**。E2-1 加了一道
       「ratio 与整数计数交叉校验」,给一组不自洽的(比如 ratio=0.25 配 9/13)
       会让判定在 `ratio_counts_disagree` 就转人工 —— 那样下游那些判据会为
       **错误的原因**通过(x14 就这么中过一次)。
    """
    from services.diagnosis_sample_contract import OUTCOME_DEGRADED, SAMPLE_CONTRACT_VERSION
    return {"outcome": OUTCOME_DEGRADED, "version": SAMPLE_CONTRACT_VERSION,
            "planned": planned, "succeeded": succeeded,
            "coverage_ratio": round(ratio, 4), "billable_ratio": ratio}


def _payload(world, *, verdict=None, identity_suspected=False):
    payload = dict(world["snapshot"])
    payload["delivery_verdict"] = verdict
    payload["identity_suspicion"] = {"suspected": True} if identity_suspected else None
    return payload


def test_x07_an_org_run_already_in_commit_pending_still_reaches_manual(
        live_server, migrated_dsn, db):
    """结算**重试**路径:run 已经在 `commit_pending` 时再判不可判,必须仍然进人工队列。

    洞在哪:a3 全部九条都由 `server.run_diagnosis_task` **首跑**驱动,
    run 走到 org 臂时恒为 `running`。所以把 CAS 的 from-状态集从
    `["running", "commit_pending"]` 砍成 `["running"]`,九条一条都不红。

    坏结果:CAS 变 no-op —— run **没有**进 `settlement_manual`、不进人工队列,
    charge link 停在 `reserved` 但没有人会来裁 ⇒ 组织预算被无限期占住,
    而后台看不见这一单(与「僵尸订阅被我写成真在跑」同形)。

    🔴 断言必须落在**库里那一行**,不能落在返回值上:
       返回值 `{"terminal": "settlement_manual"}` 是**无条件**构造的,
       CAS 命中 0 行它照样这么返。这就是本仓「终态禁 `ready if ok` 布尔中继」。
    """
    import asyncio

    from services.diagnosis_runs import dispatch_success_settlement

    w = P03C.org_world(migrated_dsn)
    with db.cursor() as cur:                 # 生产来路 = 第一次已推进 commit_pending 后重入
        cur.execute("UPDATE diagnosis_runs SET run_status='commit_pending' "
                    "WHERE run_token=%s", (w["run_token"],))
        assert cur.rowcount == 1
    _ = live_server

    out = asyncio.run(dispatch_success_settlement(
        run_token=w["run_token"], session_id=w["session_id"],
        diagnosis_id=int(w["diagnosis_id"]),
        snapshot=_payload(w, identity_suspected=True),
        organization_charge_id=w["organization_charge_id"],
        organization_charge_points=w["organization_charge_points"],
        organization_claim_token=w["organization_claim_token"],
        brand_id=w["brand_id"]))

    assert out.get("terminal") == "settlement_manual", out
    row = W.run_row(db, w["run_token"])
    assert row["run_status"] == "settlement_manual", (
        "返回值说进了人工,库里那一行还停在 %r —— CAS 的 from-状态集里没有 "
        "commit_pending,重试路径上这一跳是 no-op。这一单不会出现在任何人工队列里,"
        "组织预算却被 reserved 占着" % row["run_status"])


def test_x08_a_zero_billable_ratio_goes_to_manual_instead_of_charging_one_point(
        live_server, migrated_dsn, monkeypatch, db):
    """`billable_ratio = 0.0`(判定降级但一点没测成)必须转人工,**不许**扣 1 点。

    洞在哪:a3 只用了 `RATIO=0.25` 一个**内点**;组①的零成功走的是更早的
    `require_complete_product` 那道闸,根本不经过这一行。所以把
    `0.0 < ratio` 松成 `0.0 <= ratio`,区间断言的两端都没有样本会红。

    坏结果:`max(1, int(total * 0.0)) = 1` ⇒ **零交付也扣钱**,
    而且绕开了「既不多收也不少收 → 转人工」的口径。

    只驱动个人腿即可:两条腿共用同一个 `_partial_commit_points`
    (这件事本身由 a3 的 `test_org_arm_calls_the_shared_predicate_not_a_second_copy` 钉住),
    所以边界样本打在任一条腿上都会踩到同一行。
    """
    _ = db
    w = P03C.legacy_world(migrated_dsn, frozen=FROZEN_MULTI)
    _sent, after = drive(live_server, w, monkeypatch,
                         impl_result=_payload(w, verdict=_verdict(0.0)))

    assert after["run"]["run_status"] == "settlement_manual", (
        "ratio=0.0 的单落到了 %r —— 区间下界被放开了,零交付照样扣钱"
        % after["run"]["run_status"])
    assert "ratio_out_of_range" in (after["run"]["last_settlement_error"] or ""), (
        "转人工了,但理由是 %r —— 不是比例越界那一支,这条判据没打在边界上"
        % after["run"]["last_settlement_error"])
    assert after["freeze"]["status"] == "frozen", (
        "冻结已经动过(%r)—— 转人工的那一档要求钱既不扣也不退"
        % after["freeze"]["status"])


def test_x08b_an_in_range_ratio_still_settles_proportionally(
        live_server, migrated_dsn, monkeypatch, db):
    """配对的必须不命中:区间**内**的比例必须照常按比例结算。

    少了它,一个「把整个区间判断改成恒拒」的实现也能让上面那条绿 ——
    而那样所有降级单都会堆进人工队列。
    """
    _ = db
    w = P03C.legacy_world(migrated_dsn, frozen=FROZEN_MULTI)
    _sent, after = drive(live_server, w, monkeypatch,
                         impl_result=_payload(w, verdict=_verdict(RATIO)))
    assert after["freeze"]["status"] == "committed", (
        "区间内的比例没能自动结算:%r" % after["freeze"]["status"])


def test_x09_an_exception_inside_the_shared_predicate_never_charges_the_full_ceiling(
        live_server, migrated_dsn, monkeypatch, db):
    """共用谓词抛任何异常时,org 臂必须**转人工**,不许静默按预留上限全额扣。

    洞在哪:九条 a3 判据全走正常返回路径,没有一发把 `_partial_commit_points`
    打成抛异常;等价基线比对也只盖全履约那一档。所以把 except 臂里的
    `_org_err` 从 `partial_calc_exception:...` 改成 `None`,零判据会红。

    坏结果:schema 漂 / contract 版本变 / 一个 `NameError` —— 任何一个都会让
    org 臂**静默按上限全额扣**,P0-3 的原病灶借 except 臂原地复活。
    这是本仓「兜底 except 把 NameError 吞成正常分支」的资金版。
    """
    _ = db
    import services.diagnosis_runs as DR

    w = P03C.org_world(migrated_dsn)
    ceiling = int(w["organization_charge_points"])

    def _boom(*a, **kw):
        raise RuntimeError("extsel 判据:谓词内部炸了")

    monkeypatch.setattr(DR, "_partial_commit_points", _boom)
    _sent, after = drive(live_server, w, monkeypatch,
                         impl_result=_payload(w, verdict=_verdict(RATIO)))

    charge = after["charge"]
    assert charge["status"] != "committed" or int(charge["actual_points"] or 0) != ceiling, (
        "谓词抛了异常,org 臂却按预留上限 %r 全额扣了 —— except 臂把异常吞成了"
        "「无判定 ⇒ 全额」,而这一档的口径是既不多收也不少收" % ceiling)
    assert after["run"]["run_status"] == "settlement_manual", (
        "谓词抛异常后 run 落到 %r,应当隔离转人工" % after["run"]["run_status"])
    assert "partial_calc_exception" in (after["run"]["last_settlement_error"] or ""), (
        "转人工了但理由是 %r —— 不是 except 臂那一支"
        % after["run"]["last_settlement_error"])


def test_x14_a_split_snapshot_that_disagrees_with_the_freeze_row_goes_to_manual(
        live_server, migrated_dsn, monkeypatch, db):
    """拆分快照**在**、但与冻结行**对不上** ⇒ 不可信 ⇒ 转人工,不许拿它去动钱。

    洞在哪:a4 只造了两态 —— 「快照存在且自洽」与「快照被清空」。
    第三态(快照在但与冻结行不等)从来没有样本,所以把
    `if snap_pools != pools:` 改成 `if False and ...`,五条 a4 判据一条都不红。

    坏结果:那一列被改过 / 冻结行被动过时,**不可信的拆分**会被直接送进
    `commit_freeze` 做部分扣费 —— 客户三池(赠送/佣金/现金)之间的钱可以被
    一次列改写挪走。「快照与冻结行对不上 → 不猜」这句口径被静默拆掉。

    配对的必须不命中在 a4:`test_multi_pool_partial_delivery_settles_proportionally`
    证的正是「快照自洽时必须自动按比例」——两条合起来才排除「恒转人工」的坏实现。
    """
    import json as _json

    # 三个池都要给全:`legacy_world` 是按 pools["commission"] 直取的(缺键 KeyError)。
    w = P03C.legacy_world(migrated_dsn, frozen=FROZEN_MULTI,
                          split={"bonus": 100, "commission": 0, "paid": FROZEN_MULTI - 100})
    # 篡改快照:总额仍然对得上(650),但**分布**与冻结行不同 —— 正是"列被改过"那一态。
    tampered = {"bonus": 200, "commission": 0, "paid": FROZEN_MULTI - 200,
                "order": ["bonus", "commission", "paid"]}
    with db.cursor() as cur:
        cur.execute("UPDATE diagnosis_runs SET reserved_split_snapshot_jsonb=%s "
                    "WHERE run_token=%s", (_json.dumps(tampered), w["run_token"]))
        assert cur.rowcount == 1
        cur.execute("SELECT amount_bonus, amount_paid FROM point_freezes WHERE id=%s",
                    (int(w["freeze_id"]),))
        real = cur.fetchone()
    assert int(real["amount_bonus"]) != tampered["bonus"], (
        "篡改后的快照和冻结行竟然一致(bonus 都是 %r)—— 这条判据没造出第三态"
        % real["amount_bonus"])

    _sent, after = drive(live_server, w, monkeypatch,
                         impl_result=_payload(w, verdict=_verdict(RATIO)))

    assert after["run"]["run_status"] == "settlement_manual", (
        "快照与冻结行对不上,却仍然落到 %r —— 那笔部分扣费是拿一份不可信的拆分做的"
        % after["run"]["run_status"])
    # 🔴 必须断言**理由**,不能只断言「进了人工」。E2-1 加了一道 ratio×计数交叉校验之后,
    #    一个不自洽的判定同样会进人工 —— 那时这条判据会为**错误的原因**通过,
    #    而「快照与冻结行对不上」这件事其实没人在守了。
    assert "reserved_split" in (after["run"]["last_settlement_error"] or ""), (
        "进人工了,但理由是 %r —— 不是拆分对账那一支,这条判据没打在它该打的地方"
        % after["run"]["last_settlement_error"])
    assert after["freeze"]["status"] == "frozen", (
        "冻结已经被动过(%r)—— 不可信的拆分不许送进资金原语"
        % after["freeze"]["status"])
