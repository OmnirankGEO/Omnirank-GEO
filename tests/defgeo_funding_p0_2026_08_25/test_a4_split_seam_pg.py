"""【A-4 = Codex P1-4】多池拆分在 confirm→run 的接缝上不许丢。

修之前:``_freeze_exact`` 只把 ``freeze_id`` 上抛,``start_run_in_caller_txn``
也不接 split —— 而 billing 的 ``freeze_points`` **已经返回**了
``physical_split_snapshot``(含权威 ``order``)。后果:
部分履约结算读不到拆分 → ``reserved_split_order_unknown`` → ``settlement_manual``,
资金长期悬挂。

``order``(先扣哪个池、余额退回哪个池)是**冻结当时钱包扣费偏好**的产物,
事后从冻结行推不出来 —— 这一刻不落列,就永远没有了。所以这里两层都验:
  ① 落列了没有(结构 + 值);
  ② 落了之后**真的**能让多池单自动按比例扣(端到端),
     而把这一列清空(= 透传被摘掉的形态)时,同一条单会转人工。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from tests.defgeo_funding_p0_2026_08_25 import _world as W

pytestmark = pytest.mark.integration

COST = 650
BONUS = 300          # 两个池都出钱 ⇒ 没有权威 order 就**推不出来**该先扣哪个
PAID = 100_000
#: [E2-1] 非终止小数样本:5/13 = 0.3846153846…
PLANNED = 13
SUCCEEDED = 5
RATIO = SUCCEEDED / PLANNED
#: 🔴 手算字面量:650 × 5 = 3250;3250 ÷ 13 = 250(13 × 250 = 3250,整除)。
#:    旧 float 路径算出 249 —— 这一位就是「删掉整数修复即红」。
EXPECTED_PARTIAL = 250


@pytest.fixture()
def multi_pool_world(db, migrated_dsn, monkeypatch):
    """一个 **bonus + paid 两个池都出钱** 的真冻结。

    单池冻结是证不了 A-4 的:只有一个池出钱时,order 怎么排结果都一样,
    P0-3 的既有逻辑本来就能自动按比例 —— 判据会**恒绿**。
    """
    tenant, _a, _b = W.fresh_uids(3)
    with db.cursor() as cur:
        W.ensure_user(cur, tenant, "p0fix_split_%d" % tenant)
        W.ensure_wallet(cur, tenant, paid=PAID, bonus=BONUS)
        W.set_pricing(cur, COST)
        brand_id = W.new_brand(cur, tenant)
    monkeypatch.setattr("auth.brand_access.require_brand_access", lambda *a, **k: None)
    return {"tenant": tenant, "brand_id": brand_id}


@pytest.fixture()
def client():
    return TestClient(W.make_app(), raise_server_exceptions=False)


def _confirm(client, w):
    prev = W.make_preview(client, w["tenant"], w["brand_id"])
    r = W.confirm(client, w["tenant"], prev)
    assert r.status_code == 200, r.text
    return r.json()["diagnosisCommandId"]


def _split_of(run):
    raw = run.get("reserved_split_snapshot_jsonb")
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _degraded_snapshot(diagnosis_id):
    from services.diagnosis_sample_contract import OUTCOME_DEGRADED, SAMPLE_CONTRACT_VERSION
    return {"type": "complete", "done": True, "terminal": True,
            "diagnosis_id": int(diagnosis_id),
            "delivery_verdict": {"outcome": OUTCOME_DEGRADED,
                                 "version": SAMPLE_CONTRACT_VERSION,
                                 "planned": PLANNED, "succeeded": SUCCEEDED,
                                 "coverage_ratio": round(RATIO, 4),
                                 "billable_ratio": round(RATIO, 4)}}


# ═══════════════════════════════════════════════════════════════════════════
# ① 落列了没有
# ═══════════════════════════════════════════════════════════════════════════
def test_confirm_persists_a_usable_split_snapshot(client, multi_pool_world, db, live_server):
    """confirm 之后 run 行的 ``reserved_split_snapshot_jsonb`` 必须非空、可用。

    "非空"不够 —— 必须是**结算侧真能吃**的形状:
    三池之和 == 冻结总额,且 ``order`` 是 billing 认的两种排法之一
    (``middleware/billing._actual_and_release_split`` 会拒别的)。
    存一个 billing 必 raise 的 split,与没存一样坏,而且更难发现。
    """
    _ = live_server
    token = _confirm(client, multi_pool_world)
    run = W.run_row(db, token)
    split = _split_of(run)

    assert split, (
        "confirm 之后 reserved_split_snapshot_jsonb 仍是空的 —— 拆分在 "
        "confirm→run 的接缝上丢了(Codex P1-4)")
    pools = {k: int(split.get(k) or 0) for k in ("bonus", "commission", "paid")}
    assert sum(pools.values()) == COST, (
        "拆分之和 %r ≠ 冻结总额 %r" % (sum(pools.values()), COST))
    assert sorted(split.get("order") or []) in (
        ["bonus", "commission", "paid"], ["commission", "paid"]), (
        "order=%r 不是 billing 认的两种形态 —— 送进资金原语会当场 raise"
        % (split.get("order"),))

    fz = W.freeze_row(db, run["freeze_id"])
    assert pools == {"bonus": int(fz["amount_bonus"] or 0),
                     "commission": int(fz["amount_commission"] or 0),
                     "paid": int(fz["amount_paid"] or 0)}, (
        "快照与真冻结行的三池对不上 —— 那是一份错的快照,比没有更危险")
    # 判别力自证:两个池都出钱,否则这条判据换成单池也会绿。
    assert len([v for v in pools.values() if v > 0]) >= 2, (
        "夹具只造出了单池冻结(%r)—— 单池不需要 order 也能算,本文件全体失去判别力" % pools)


# ═══════════════════════════════════════════════════════════════════════════
# ② 端到端:落了列 ⇒ 多池单真的自动按比例扣
# ═══════════════════════════════════════════════════════════════════════════
def test_multi_pool_partial_delivery_settles_proportionally(
        client, multi_pool_world, db, live_server):
    """defgeo 入口造出来的多池单 + 降级交付 ⇒ 按比例部分扣,**不转人工**。"""
    _ = live_server
    from services.diagnosis_runs import commit_run

    token = _confirm(client, multi_pool_world)
    run = W.run_row(db, token)
    did = W.seed_product(db, run)
    before = W.wallet(db, multi_pool_world["tenant"])

    out = asyncio.run(commit_run(token, _degraded_snapshot(did)))
    assert out.get("terminal") == "committed", (
        "多池降级单没能自动结算:%r —— reserved_split_order_unknown 就是拆分丢了的签名" % (out,))

    fz = W.freeze_row(db, run["freeze_id"])
    assert fz["status"] == "committed", fz
    after = W.wallet(db, multi_pool_world["tenant"])
    released_back = ((after["paid_points"] - before["paid_points"])
                     + (after["bonus_points"] - before["bonus_points"])
                     + (after["commission_points"] - before["commission_points"]))
    assert after["frozen_points"] == before["frozen_points"] - COST, (after, before)
    assert released_back == COST - EXPECTED_PARTIAL, (
        "退回 %r,应为 %r(= 冻结 %r − 已履约 %r)"
        % (released_back, COST - EXPECTED_PARTIAL, COST, EXPECTED_PARTIAL))


def test_wiping_the_split_sends_the_same_order_to_manual(
        client, multi_pool_world, db, live_server):
    """配对的必须不命中:把这一列清空(= 透传被摘掉的形态)⇒ 同一条单转人工。

    这条证明的是**这一列真的承重** —— 没有它,上面那条绿只能说明"结算能跑",
    说明不了"是这一列让它跑起来的"。
    """
    _ = live_server
    from services.diagnosis_runs import commit_run

    token = _confirm(client, multi_pool_world)
    run = W.run_row(db, token)
    did = W.seed_product(db, run)
    with db.cursor() as cur:
        cur.execute("UPDATE diagnosis_runs SET reserved_split_snapshot_jsonb=NULL "
                    "WHERE run_token=%s", (token,))

    out = asyncio.run(commit_run(token, _degraded_snapshot(did)))
    assert out.get("terminal") == "settlement_manual", (
        "拆分被清空之后仍然自动动了钱:%r —— 那说明扣费顺序是猜出来的" % (out,))
    assert out.get("reason") == "reserved_split_order_unknown", out
    assert W.freeze_row(db, run["freeze_id"])["status"] == "frozen", "转人工却动了钱"


# ═══════════════════════════════════════════════════════════════════════════
# ③ 结构面:接缝上的三件事实必须一起落
# ═══════════════════════════════════════════════════════════════════════════
def test_start_run_accepts_and_forwards_all_three_facts(live_server):
    """AST:``start_run_in_caller_txn`` 必须**同时**把 split 与 payer 交给
    ``_persist_freeze_handle``。

    值层面的判据只能覆盖它跑到的那条分支;这一条覆盖"接线还在不在"。
    """
    _ = live_server
    import ast
    import inspect
    import textwrap

    import services.diagnosis_runs as dr

    fn = ast.parse(textwrap.dedent(inspect.getsource(dr.start_run_in_caller_txn))).body[0]
    names = {a.arg for a in fn.args.kwonlyargs}
    assert {"split_snapshot", "payer_user_id"} <= names, (
        "start_run_in_caller_txn 少了参数 %r" % (sorted(names),))

    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_persist_freeze_handle"]
    assert calls, "分母为 0 —— 它已经不调回填函数了,这条判据会恒绿"
    forwarded = False
    for call in calls:
        kw = {k.arg for k in call.keywords}
        positional = {ast.dump(a) for a in call.args}
        if "payer_user_id" in kw and any("split_snapshot" in s for s in positional | {
                ast.dump(k.value) for k in call.keywords}):
            forwarded = True
    assert forwarded, "回填调用没有同时带上 split 与 payer —— 接缝又断了"


def test_confirm_hands_the_three_facts_over(live_server):
    """AST:confirm 里 ``_freeze_exact`` 的返回值必须是四元,且四元都被用上。"""
    _ = live_server
    import ast
    import inspect
    import textwrap

    import api.defensive_geo_api as mod

    fn = ast.parse(textwrap.dedent(inspect.getsource(mod.confirm_run_preview))).body[0]
    targets = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Await):
            call = node.value.value
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) \
                    and call.func.id == "_freeze_exact":
                targets = node.targets
    assert targets, "confirm 里找不到 `await _freeze_exact(...)` 的赋值 —— 分母坏了"
    tup = targets[0]
    assert isinstance(tup, ast.Tuple) and len(tup.elts) == 4, (
        "_freeze_exact 的返回值不是四元(句柄/freeze_id/split/payer):%r" % (ast.dump(tup),))
    unpacked = {e.id for e in tup.elts if isinstance(e, ast.Name)}
    assert {"split_snapshot", "payer_user_id"} <= unpacked, unpacked

    starts = [n for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id == "start_run_in_caller_txn"]
    assert starts, "confirm 不再调 start_run_in_caller_txn —— 分母坏了"
    kw = {k.arg for k in starts[0].keywords}
    assert {"split_snapshot", "payer_user_id"} <= kw, (
        "confirm 拿到了三件事实却没交出去:%r" % (sorted(kw),))
