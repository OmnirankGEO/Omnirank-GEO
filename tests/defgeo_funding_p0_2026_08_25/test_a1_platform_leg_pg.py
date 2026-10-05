"""【A-1 = Codex P0-1】平台承担腿:真冻结必须走到物理终态,payer 必须是平台账号。

修之前的三段链(全部亲核过,不是转述):
  ① ``funding_projection`` 把平台两格投影成 ``billing_mode='exempt'``;
  ② confirm 却对平台腿走**真** ``freeze_points(platform_uid, …)``,``point_freezes`` 真长一行;
  ③ ``commit_run/release_run`` 见 exempt 直接进 ``completed_exempt/failed_exempt``,
     **零 billing 调用** —— 交付完成、平台成本却回退为零,冻结再被通用
     ``freeze_sweeper`` 当僵尸释放。
  ④ 就算把 ③ 的旁路拆了,结算仍拿 ``owner_user_id``(租户)去定位冻结,
     而那一行在**平台**名下 —— 第二层照样结不掉。

所以本文件每条判据都成对:**必须命中**(钱真的按预期动了)+
**必须不命中**(把修复拆掉 / 把 payer 抹掉 → 同一条判据必红)。
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from tests.defgeo_funding_p0_2026_08_25 import _world as W

pytestmark = pytest.mark.integration

COST = 650


@pytest.fixture()
def platform_world(db, migrated_dsn, monkeypatch):
    """admin 发起 → 平台承担腿。平台直营服务账号是**另一个**真账号(非 admin、有钱包)。"""
    admin_uid, platform_uid, _ = W.fresh_uids(3)
    with db.cursor() as cur:
        W.ensure_user(cur, admin_uid, "p0fix_admin_%d" % admin_uid, admin=True)
        W.ensure_user(cur, platform_uid, "p0fix_platform_%d" % platform_uid)
        W.ensure_wallet(cur, admin_uid, paid=1_000_000)
        W.ensure_wallet(cur, platform_uid, paid=1_000_000)
        W.set_pricing(cur, COST)
        brand_id = W.new_brand(cur, admin_uid)
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", str(platform_uid))
    monkeypatch.setattr("auth.brand_access.require_brand_access", lambda *a, **k: None)
    return {"admin_uid": admin_uid, "platform_uid": platform_uid,
            "brand_id": brand_id, "dsn": migrated_dsn}


@pytest.fixture()
def platform_world_multi_pool(db, migrated_dsn, monkeypatch):
    """同上,但**平台钱包两个池都出钱**(bonus 300 + paid 其余)。

    存在的理由是撕锁抓到的两个存活:``_frozen_total_for_run`` /
    ``_reserved_split_for_run`` 改回读 owner 时,上面那几条判据**一条都不红** ——
    因为它们只跑全额 commit / release,根本不读冻结行的金额与三池。
    只有「平台腿 + 降级交付」这一格会同时踩到那两个读取。
    """
    admin_uid, platform_uid, _ = W.fresh_uids(3)
    with db.cursor() as cur:
        W.ensure_user(cur, admin_uid, "p0fix_madmin_%d" % admin_uid, admin=True)
        W.ensure_user(cur, platform_uid, "p0fix_mplat_%d" % platform_uid)
        W.ensure_wallet(cur, admin_uid, paid=1_000_000)
        W.ensure_wallet(cur, platform_uid, paid=100_000, bonus=300)
        W.set_pricing(cur, COST)
        brand_id = W.new_brand(cur, admin_uid)
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", str(platform_uid))
    monkeypatch.setattr("auth.brand_access.require_brand_access", lambda *a, **k: None)
    return {"admin_uid": admin_uid, "platform_uid": platform_uid,
            "brand_id": brand_id, "bonus": 300, "paid": 100_000}


@pytest.fixture()
def client():
    return TestClient(W.make_app(), raise_server_exceptions=False)


def _confirm_platform(client, w):
    prev = W.make_preview(client, w["admin_uid"], w["brand_id"], admin=True)
    r = W.confirm(client, w["admin_uid"], prev, admin=True)
    assert r.status_code == 200, r.text
    return r.json()


# ═══════════════════════════════════════════════════════════════════════════
# ① 平台腿 confirm 之后,run 行上三件事实齐备
# ═══════════════════════════════════════════════════════════════════════════
def test_platform_leg_run_is_settleable_and_records_the_real_payer(client, platform_world, db):
    """confirm 后:``billing_mode='paid'``(会走物理结算的那个模式)+ payer = 平台 UID
    + 冻结行真的长在平台钱包上 + task_ref 与 run 行**同一个串**。

    每一项都对应一个具体的坏结果:
      · billing_mode 还是 exempt → commit_run 短路,平台成本归零;
      · payer 为空/是租户   → commit_freeze 按 (id,user) 定位不到,永远结不掉;
      · task_ref 两个串     → 按三元组定位冻结的四处全部查空(降级扣费/人工退款核验全废)。
    """
    body = _confirm_platform(client, platform_world)
    run = W.run_row(db, body["diagnosisCommandId"])

    assert run is not None, "confirm 返回了 command,库里却没有这一行"
    assert run["billing_mode"] == "paid", (
        "平台腿仍然是 %r —— exempt 是 commit_run/release_run 的短路口令,"
        "走它就零 billing 调用,交付完成后平台成本回退为零(Codex P0-1)" % run["billing_mode"])
    assert run["payer_user_id"] == platform_world["platform_uid"], (
        "run 上的 payer 是 %r,应为平台 UID %r —— 结算按 payer 定位冻结,"
        "记错就永远结不掉(Codex P0-1 第二层)"
        % (run["payer_user_id"], platform_world["platform_uid"]))
    assert run["freeze_id"] is not None and run["freeze_backend"] == "legacy"
    assert run["run_status"] == "running", run["run_status"]

    fz = W.freeze_row(db, run["freeze_id"])
    assert fz is not None, "run 指着一个不存在的冻结 id"
    assert int(fz["user_id"]) == platform_world["platform_uid"], (
        "冻结长在 %r 名下,而不是平台账号 —— 那 exempt_recorded 就是假的" % fz["user_id"])
    assert int(fz["amount_total"]) == COST
    assert fz["task_ref"] == run["freeze_task_ref"], (
        "冻结的 task_ref=%r 与 run 行的 %r 不是同一个串 —— 凡是按 "
        "(id+user+task_ref) 定位冻结的地方全部查空" % (fz["task_ref"], run["freeze_task_ref"]))

    # 配对的必须不命中:客户(发起人)的钱包**一分没动**,exempt_recorded 才成立。
    assert W.wallet(db, platform_world["admin_uid"])["frozen_points"] == 0
    assert W.wallet(db, platform_world["platform_uid"])["frozen_points"] == COST


def test_platform_leg_response_still_tells_the_user_they_pay_nothing(client, platform_world):
    """``billingModeProjection`` 改成 paid **不等于**对用户说"你要付钱"。

    这两格是两件事:前者说「这一单要不要走物理结算」,后者说「你要不要掏钱」。
    翻译混了就会把平台承担说成客户自费。
    """
    body = _confirm_platform(client, platform_world)
    assert body["fundingState"] == "exempt_recorded", body
    assert body["fundingPolicy"] == "admin_platform_ledger", body
    assert body["billingModeProjection"] == "paid", body
    assert body["fundingHandle"]["kind"] == "platform_cost_ledger", body


# ═══════════════════════════════════════════════════════════════════════════
# ② 真的走到物理终态:commit / release 两个方向都验
# ═══════════════════════════════════════════════════════════════════════════
def test_platform_freeze_reaches_committed_through_the_real_settlement(
        client, platform_world, db, live_server):
    """成功路径:``commit_run`` 之后冻结必须是 **committed**、平台钱包真的少钱。

    这就是「删掉修复即红」的那一条:把投影改回 exempt(见下面那条反向判据),
    commit_run 会走 completed_exempt,冻结原地停在 frozen —— 本断言必红。
    """
    _ = live_server                       # 确保迁移已重放 + 模块已按真库绑好
    from services.diagnosis_runs import commit_run, mark_product_pending

    body = _confirm_platform(client, platform_world)
    token = body["diagnosisCommandId"]
    run = W.run_row(db, token)

    # 产物证明:结算前 require_complete_product 要读到一条真诊断记录。
    W.seed_product(db, run)
    out = asyncio.run(commit_run(token, {"type": "complete", "done": True}))
    assert out.get("ok") is True, out
    assert out.get("terminal") == "committed", (
        "平台腿终态是 %r —— completed_exempt 说明它又走了那条零 billing 的短路" % out.get("terminal"))

    fz = W.freeze_row(db, run["freeze_id"])
    assert fz["status"] == "committed", (
        "冻结停在 %r。平台已经交付,成本却没落地 —— 这正是 P0-1 的损失方向" % fz["status"])
    w = W.wallet(db, platform_world["platform_uid"])
    assert w["frozen_points"] == 0, "冻结额没出池:%r" % w
    assert w["paid_points"] == 1_000_000 - COST, (
        "平台钱包没真扣:%r(冻结出池但没转成消费 = 账面凭空少一笔)" % w)
    _ = mark_product_pending                # 引用保持 import 面真实(未用即删)


def test_platform_freeze_is_released_when_the_run_fails(client, platform_world, db, live_server):
    """失败路径同样必须走到物理终态 —— 释放,而不是永远挂着等 sweeper 收尸。"""
    _ = live_server
    from services.diagnosis_runs import release_run

    body = _confirm_platform(client, platform_world)
    token = body["diagnosisCommandId"]
    run = W.run_row(db, token)

    out = asyncio.run(release_run(token, reason="p0fix 人造失败"))
    assert out.get("ok") is True and out.get("terminal") == "released", out

    fz = W.freeze_row(db, run["freeze_id"])
    assert fz["status"] == "released", (
        "冻结停在 %r —— failed_exempt 那条短路会让它永远是 frozen" % fz["status"])
    w = W.wallet(db, platform_world["platform_uid"])
    assert w["frozen_points"] == 0 and w["paid_points"] == 1_000_000, (
        "平台钱包没退回原样:%r" % w)


# ═══════════════════════════════════════════════════════════════════════════
# ②b 平台腿 + **降级交付**:冻结额与三池拆分也必须按 payer 读
# ═══════════════════════════════════════════════════════════════════════════
def test_platform_leg_partial_delivery_reads_the_freeze_by_payer(
        client, platform_world_multi_pool, db, live_server):
    """平台腿降级交付 ⇒ 按比例部分扣,**不是**转人工。

    这一格同时踩到两个"按三元组读冻结行"的地方:
      · ``_frozen_total_for_run``  —— 读不到 → ``frozen_amount_unreadable``;
      · ``_reserved_split_for_run`` —— 读不到 → ``reserved_split_freeze_missing``。
    两者只要有一个改回读 owner,平台腿的降级单就会全部落进 settlement_manual,
    钱长挂。撕锁 S03/S04 存活正是因为此前没有这一格。
    """
    _ = live_server
    from services.diagnosis_runs import commit_run
    from services.diagnosis_sample_contract import OUTCOME_DEGRADED, SAMPLE_CONTRACT_VERSION

    w = platform_world_multi_pool
    prev = W.make_preview(client, w["admin_uid"], w["brand_id"], admin=True)
    r = W.confirm(client, w["admin_uid"], prev, admin=True)
    assert r.status_code == 200, r.text
    token = r.json()["diagnosisCommandId"]
    run = W.run_row(db, token)
    did = W.seed_product(db, run)

    fz = W.freeze_row(db, run["freeze_id"])
    funded = [k for k in ("amount_bonus", "amount_commission", "amount_paid")
              if int(fz[k] or 0) > 0]
    assert len(funded) >= 2, (
        "夹具只造出单池冻结(%r)—— 单池不需要 order 也能算,这条判据会失去一半判别力" % fz)

    # [E2-1] 样本换成 7/10:0.7 在二进制里是无限循环小数,
    #   650 × 0.7 = 454.99999999999994 → 旧式 int 得 **454**;
    #   整数精确 650 × 7 = 4550,4550 ÷ 10 = **455**。差 1,方向是**少收**。
    #   选它的另一个理由:70% 覆盖率是最普通的一档,不是构造出来的边角料。
    planned, succeeded = 10, 7
    ratio = succeeded / planned
    expected = 455          # 🔴 手算字面量(4550 ÷ 10),不抄实现公式
    out = asyncio.run(commit_run(token, {
        "type": "complete", "done": True, "terminal": True, "diagnosis_id": did,
        "delivery_verdict": {"outcome": OUTCOME_DEGRADED,
                             "version": SAMPLE_CONTRACT_VERSION,
                             "planned": planned, "succeeded": succeeded,
                             "coverage_ratio": round(ratio, 4),
                             "billable_ratio": round(ratio, 4)}}))
    assert out.get("terminal") == "committed", (
        "平台腿降级单没能自动结算:%r —— frozen_amount_unreadable / "
        "reserved_split_freeze_missing 就是「拿 owner 去读平台的冻结行」的签名" % (out,))

    after = W.wallet(db, w["platform_uid"])
    assert after["frozen_points"] == 0, after
    # 🔴 这条链的钱包语义与 p03c 的夹具不同:这里的冻结是**真 freeze_points 冻的**,
    #    冻结当时已经把 650 从 bonus/paid 转进了 frozen。所以"扣了多少"要看
    #    **可用余额总量**(bonus+paid)少了多少,而不是看某个池涨回来多少。
    #    (第一版写成"退回额",拿到的是 -162 —— 那正好是扣掉的数,方向反了。)
    spendable_before = w["paid"] + w["bonus"]
    spendable_after = after["paid_points"] + after["bonus_points"]
    assert spendable_before - spendable_after == expected, (
        "平台钱包实扣 %r,应为按履约整数分账的 %r(冻结 %r × %d/%d)· 余额 %r"
        % (spendable_before - spendable_after, expected, COST, succeeded, planned, after))
    assert expected < COST, "比例算出来等于全额 —— 这条判据没有判别力了"


def test_backfilling_the_handle_without_a_payer_must_not_wipe_the_real_one(
        client, platform_world, db, live_server):
    """``_persist_freeze_handle`` 不传 payer 时,必须**别动这一列**。

    这个函数有四个不传 payer 的既有调用方(sweeper 收尸定位 /
    ``activate_after_freeze`` 三处),它们只想回填冻结句柄。
    若那条 UPDATE 写成无条件 ``payer_user_id=%s``,不传就等于把 confirm 当时
    落好的真实 payer 抹成 NULL —— 平台腿的钱又变回拿 owner 去结算,
    也就是本单要修的 bug 借由「顺手回填一次句柄」原地复活。

    撕锁 S06(把 COALESCE 改成无条件写)此前**存活**,就是因为没有这一条:
    上面那些判据在 confirm 之后再没有任何路径回填过句柄。
    """
    _ = live_server
    from services.diagnosis_runs import _persist_freeze_handle

    body = _confirm_platform(client, platform_world)
    token = body["diagnosisCommandId"]
    run = W.run_row(db, token)
    assert run["payer_user_id"] == platform_world["platform_uid"], run

    # 照既有调用方的形态调:只给句柄三件套,不给 payer、不给 split。
    _persist_freeze_handle(token, int(run["freeze_id"]), str(run["freeze_backend"]))

    again = W.run_row(db, token)
    assert again["payer_user_id"] == platform_world["platform_uid"], (
        "回填一次句柄就把 payer 抹成了 %r —— 那一列必须是「不传就别动」"
        % (again["payer_user_id"],))
    assert again["freeze_id"] == run["freeze_id"], again


# ═══════════════════════════════════════════════════════════════════════════
# ③ 配对的必须不命中:把 payer 抹掉 ⇒ 结算定位不到,绝不静默"成功"
# ═══════════════════════════════════════════════════════════════════════════
def test_wiping_the_payer_makes_settlement_fail_loudly_not_silently(
        client, platform_world, db, live_server):
    """把 ``payer_user_id`` 置 NULL(= 迁移 050 漏跑 / 透传被摘掉的形态),
    结算会回落成 owner,于是**定位不到**那一行冻结。

    本条钉住两件事:
      · 那条回落**确实存在**(所以这一列不是装饰,是承重);
      · 定位不到时系统**不会**把冻结当成已结算 —— 冻结仍是 frozen,
        run 不是 committed。静默"成功"才是最贵的形态。
    """
    _ = live_server
    from services.diagnosis_runs import commit_run

    body = _confirm_platform(client, platform_world)
    token = body["diagnosisCommandId"]
    run = W.run_row(db, token)
    W.seed_product(db, run)
    with db.cursor() as cur:
        cur.execute("UPDATE diagnosis_runs SET payer_user_id=NULL WHERE run_token=%s", (token,))

    out = asyncio.run(commit_run(token, {"type": "complete", "done": True}))
    assert out.get("terminal") != "committed", (
        "payer 被抹掉之后仍然报 committed —— 那说明结算根本没在按 payer 定位冻结,"
        "或者更糟:它扣到了别人头上。out=%r" % (out,))
    fz = W.freeze_row(db, run["freeze_id"])
    assert fz["status"] == "frozen", (
        "定位不到冻结却把它改成了 %r —— 静默动钱" % fz["status"])
    assert W.wallet(db, platform_world["admin_uid"])["paid_points"] == 1_000_000, (
        "回落成 owner 之后竟然扣了**发起人**的钱 —— 那是扣错人")
