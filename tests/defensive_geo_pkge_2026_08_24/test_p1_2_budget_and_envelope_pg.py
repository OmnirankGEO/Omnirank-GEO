"""终审 P1-2 —— 「preview 对任何输入必 500」的**根因 + 信封**两半(工单③)。

═══════════════════════════════════════════════════════════════════════
🔴 先证伪工单给的根因,再修真的那个
═══════════════════════════════════════════════════════════════════════
工单原文把 P1-2 描述成 preview 端点的问题。真跑下来根因不在 preview 里:

    preview → publish_funding.check_and_lock_budget
            → store.get_budget 返回 None
            → FundingError → handler 兜底 except → 受控 500

而 ``store.insert_budget`` 的**生产调用者集合是空的**(见 test_00)。
表永远是空的 ⇒ 任何输入都会走到那个 raise ⇒ "任何输入必 500"。

所以两半都要判:
  · **根因**:预算快照要有真的签发者(activation 物化器),而且它得在 cron 里;
  · **信封**:即使真的取不到,也必须 typed + 有下一步,而不是裸 500。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from services.defensive_geo.publish import execution_budget_policy as _policy
from services.defensive_geo.publish import store as _store

from tests.defensive_geo_pkge_2026_08_24 import _seed
from tests.defensive_geo_pkge_2026_08_24._census import (
    production_callers as _production_callers,
    scanned_file_count as _scanned_file_count,
)
from tests.defensive_geo_pkge_2026_08_24.conftest import connect

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def _base(_schema) -> Iterator[None]:                     # noqa: ANN001
    # 🔴 必须在 dump 装完之后再 import auth.brand_access:它的模块体会触发
    #    db.diagnosis_db 的 init_db 发 DDL。放模块导入期会撞 dump;
    #    拖到某条判据的事务里第一次触发,则是 2026-08-10 那次单线程自死锁。
    import auth.brand_access  # noqa: F401,PLC0415

    _seed.install_base_rows()
    yield


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    from api import defensive_publish_api

    app = FastAPI()
    app.include_router(defensive_publish_api.router)

    @app.middleware("http")
    async def _inject_user(request: Request, call_next):  # noqa: ANN001
        who = request.headers.get("X-Test-Identity", "a")
        request.state.user = dict(_seed.IDENTITIES[who])
        return await call_next(request)

    with TestClient(app) as c:
        yield c


def _preview_body(ctx: dict[str, Any], *, name: str) -> dict[str, Any]:
    revision_id, article_hash = _seed.seed_article(
        brand=ctx["brandId"], body=f"包E 判据正文 {name} · 一段可发布的稿子。")
    return {
        "planItemKey": f"plan_{name}",
        "articleRevisionId": revision_id,
        "expectedArticleHash": article_hash,
        "acceptedSnapshotId": ctx["acceptedSnapshotId"],
        "serviceProjectionId": _policy.service_projection_id_for(ctx["acceptedSnapshotId"]),
        "brandId": ctx["brandId"],
    }


def _post_preview(client: TestClient, body: dict[str, Any], *, who: str = "a"):
    return client.post(
        "/api/defensive-geo/publish/decision-snapshots/preview",
        headers={"Idempotency-Key": "pkge-" + uuid.uuid4().hex,
                 "X-Test-Identity": who},
        json=body,
    )


# ══════════════════════════════════════════════════════════════════════════
# 00 · 根因的**机械**证据
# ══════════════════════════════════════════════════════════════════════════
def test_00_budget_snapshot_has_a_production_issuer() -> None:
    """🔴 P1-2 的根因锁:``insert_budget`` 必须有生产调用点。

    包E 之前它是**零**(排 tests 后只剩定义处)—— 那才是"任何输入必 500"。
    拆红:把 ``activation_materializer.materialize_one`` 里那一跳删掉 ⇒ 本条红。
    """
    callers = _production_callers("insert_budget")
    assert callers, (
        "store.insert_budget 零生产调用点 —— defgeo_provider_execution_budgets "
        "永远是空表,于是 check_and_lock_budget 对任何输入都 raise,"
        "preview 必 500。这就是终审 P1-2 的根因"
    )
    assert not any(c.startswith("tests/") for c in callers), (
        f"只有测试在调:{sorted(callers)} —— 死函数的标准形态")


def test_00b_detector_can_see_a_symbol_that_really_has_no_caller() -> None:
    """探测器活性自证 —— **两向**都验。

    · 一个**确实不存在**的符号必须返回空集(规则太宽会把什么都算成调用);
    · 扫描面本身必须非空(规则太窄 / 目录写错时,上面那条"非空"是假绿)。
    """
    assert _production_callers("pkge_definitely_not_a_real_function") == set()
    assert _scanned_file_count() > 100, (
        f"census 只收到 {_scanned_file_count()} 条调用记录 —— 扫描面多半是空的,"
        "那样『有没有生产调用点』这句话没有分母")


# ══════════════════════════════════════════════════════════════════════════
# 01 · 信封:取不到预算 ⇒ typed,不是 500
# ══════════════════════════════════════════════════════════════════════════
def test_01_missing_budget_is_typed_not_500(client: TestClient) -> None:
    """🔴 成对判据的**前**一半:预算还没签发时,preview 必须 typed 拒绝。

    这一格刻意**不跑物化器** —— 它就是终审复现到的那个状态。
    """
    ctx = _seed.seed_accepted_snapshot(publications=2, activate=True)
    _seed.mark_activation_materialized(ctx["acceptedSnapshotId"])   # 只关掉队列,不签预算
    resp = _post_preview(client, _preview_body(ctx, name="nobudget"))

    assert resp.status_code != 500, (
        f"预算缺失仍然是裸 500 —— P1-2 没修:{resp.text[:600]}")
    detail = resp.json().get("detail") or {}
    assert detail.get("code") == "EXECUTION_BUDGET_NOT_READY", (
        f"信封 code 不对:{resp.text[:600]}")
    assert resp.status_code == 409, resp.status_code
    assert detail.get("retryable") is True, "她什么都不用改,等一下再点就行 ⇒ 必须 retryable"
    # §0.5.6 铁律:任何阻塞必须自带解决方案。
    action = detail.get("nextAction") or {}
    assert action.get("kind") and action.get("label"), f"没有下一步 = 死路:{detail}"
    explanation = detail.get("publicExplanation") or ""
    assert explanation and "500" not in explanation, explanation
    for jargon in ("budget", "snapshot", "FundingError", "provider"):
        assert jargon not in explanation, f"对客文案漏了工程词 {jargon!r}:{explanation}"


def test_02_after_materializer_runs_preview_succeeds(client: TestClient) -> None:
    """🔴 成对判据的**后**一半:物化器跑过之后,同样的输入 200。

    两条合起来才证明"根因被修掉了"而不是"错误信封被换了个说法"。
    """
    from services.defensive_geo import activation_materializer as _mat

    ctx = _seed.seed_accepted_snapshot(publications=2, activate=True)
    before = _seed.counts()
    result = _mat.materialize_pending()
    assert result["materialized"] >= 1, f"物化器一条都没物化:{result}"
    after = _seed.counts()
    assert after[_store.BUDGET_TABLE] > before[_store.BUDGET_TABLE], (
        "物化器跑了但预算表没多行 —— 它没真签发")

    resp = _post_preview(client, _preview_body(ctx, name="withbudget"))
    assert resp.status_code == 200, f"物化之后 preview 仍然失败:{resp.text[:800]}"
    body = resp.json()
    assert body.get("slotAdmission") == "snapshot_ready", body


def test_03_materializer_is_zero_freeze(client: TestClient) -> None:
    """ACT-06/13 逐字:activation 本身**新增执行算力 freeze = 0**。

    尺子活性由 ``counts()`` 在别处证明(test_02 看到 budget 计数真的会动),
    这里只判方向:钱包与冻结行都不许动。
    """
    from services.defensive_geo import activation_materializer as _mat

    ctx = _seed.seed_accepted_snapshot(publications=3, activate=True)
    w_before = _seed.wallet(ctx["tenantOwnerId"])
    c_before = _seed.counts()
    _mat.materialize_pending()
    w_after = _seed.wallet(ctx["tenantOwnerId"])
    c_after = _seed.counts()

    assert w_after == w_before, f"物化动了钱包:{w_before} → {w_after}"
    assert c_after["point_freezes"] == c_before["point_freezes"], "物化产生了冻结行"
    assert c_after[_store.BUDGET_TABLE] > c_before[_store.BUDGET_TABLE], (
        "这一轮没签出预算 —— 上面两条「没动钱」就没有被测对象了")


# ══════════════════════════════════════════════════════════════════════════
# 04 · 预算口径:**算术对账**,不是"看着差不多"
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("publications", [1, 2, 5])
def test_04_cap_is_derived_not_invented(publications: int) -> None:
    """cap = 合同承诺发布篇数 × 目录最高单价。两个乘数**都**能驱动结果。

    🔴 判据驱动的是那一行,不是自己构造中间值:``publications`` 变,cap 必须跟着变。
       只测一个值的话,「cap 永远等于某个常数」这类实现照样全绿。
    """
    ctx = _seed.seed_accepted_snapshot(publications=publications, activate=False)
    conn = connect()
    try:
        cur = conn.cursor()
        draft = _policy.derive(
            cur, tenant_owner_id=ctx["tenantOwnerId"],
            accepted_snapshot_id=ctx["acceptedSnapshotId"])
        conn.rollback()
    finally:
        conn.close()

    assert draft.scope_cap_points == publications * _seed.MAX_UNIT_POINTS, (
        f"cap 对不上:实得 {draft.scope_cap_points},"
        f"期望 {publications} × {_seed.MAX_UNIT_POINTS}")
    assert draft.global_cap_points >= draft.scope_cap_points, (
        "044 的 CHECK 要求 scope_cap <= global_cap")
    assert draft.derivation["committedPublications"] == publications
    assert draft.derivation["ceilingKind"] == _policy.CEILING_KIND


def test_05_cap_reads_contract_minimum_not_capacity() -> None:
    """DEL-01:承诺量与容量**分开取数**。

    §19 第 11 发变异正是「拿 required_articles / capacity 同时表示最低和上限」。
    这里造一份 capacity 与 minimum 不同的计划,cap 必须跟着 **minimum** 走。
    """
    plan = _seed.delivery_plan(publications=2)
    # 把容量抬到 5(每格仍是 1 篇,这里直接改 header 侧的对照量),
    # 承诺量保持 2 —— cap 必须还是 2 × 单价。
    plan["contract_minimums"]["articles"] = 5
    assert _policy.committed_publications(plan) == 2, (
        "committed_publications 读到了 articles 而不是 publications —— 两个数被混用了")

    plan["contract_minimums"]["publications"] = 4
    assert _policy.committed_publications(plan) == 4, "承诺量变了,读出来的数没变"


def test_06_budget_id_is_deterministic_so_replay_cannot_double_issue() -> None:
    """幂等承重在 **PRIMARY KEY** 上,不在 Python 的 if 上。

    同一份推导两次 ⇒ 同一个 id ⇒ 第二次插撞主键。
    """
    ctx = _seed.seed_accepted_snapshot(publications=2, activate=False)
    conn = connect()
    try:
        cur = conn.cursor()
        a = _policy.derive(cur, tenant_owner_id=ctx["tenantOwnerId"],
                           accepted_snapshot_id=ctx["acceptedSnapshotId"])
        b = _policy.derive(cur, tenant_owner_id=ctx["tenantOwnerId"],
                           accepted_snapshot_id=ctx["acceptedSnapshotId"])
        conn.rollback()
    finally:
        conn.close()
    assert a.execution_budget_snapshot_id == b.execution_budget_snapshot_id
    assert a.budget_hash == b.budget_hash

    # 反向:换一个 accepted snapshot 必须换 id(否则 id 与内容无关 = 假幂等)
    other = _seed.seed_accepted_snapshot(publications=2, activate=False)
    conn = connect()
    try:
        cur = conn.cursor()
        c = _policy.derive(cur, tenant_owner_id=other["tenantOwnerId"],
                           accepted_snapshot_id=other["acceptedSnapshotId"])
        conn.rollback()
    finally:
        conn.close()
    assert c.execution_budget_snapshot_id != a.execution_budget_snapshot_id


def test_07_materialize_twice_does_not_issue_two_budgets() -> None:
    """重放:同一条 activation 物化两次,预算只有一张。"""
    from services.defensive_geo import activation_materializer as _mat

    ctx = _seed.seed_accepted_snapshot(publications=2, activate=True)
    _mat.materialize_pending()
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT COUNT(*) AS n FROM {_store.BUDGET_TABLE} WHERE accepted_snapshot_id = %s",
            (ctx["acceptedSnapshotId"],))
        first = int(cur.fetchone()["n"])
        conn.rollback()
    finally:
        conn.close()
    assert first == 1, f"第一次物化就签出了 {first} 张预算"

    # 手动把队列行放回 pending,强制再物化一次(模拟租约超时后被别人重新领走)
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE defgeo_activation_outbox SET status='pending', materialized_at=NULL, "
            "available_at=NOW() WHERE accepted_snapshot_id = %s",
            (ctx["acceptedSnapshotId"],))
        conn.commit()
    finally:
        conn.close()
    _mat.materialize_pending()

    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT COUNT(*) AS n FROM {_store.BUDGET_TABLE} WHERE accepted_snapshot_id = %s",
            (ctx["acceptedSnapshotId"],))
        again = int(cur.fetchone()["n"])
        conn.rollback()
    finally:
        conn.close()
    assert again == 1, f"重放后变成了 {again} 张预算 —— 幂等没兜住"


def test_08_plan_without_publication_commitment_is_refused() -> None:
    """承诺 0 篇发布 ⇒ **不签**发布预算(而不是签一个 0 上限或猜一个数)。"""
    ctx = _seed.seed_accepted_snapshot(publications=0, activate=False)
    conn = connect()
    try:
        cur = conn.cursor()
        with pytest.raises(_policy.BudgetPolicyError):
            _policy.derive(cur, tenant_owner_id=ctx["tenantOwnerId"],
                           accepted_snapshot_id=ctx["acceptedSnapshotId"])
        conn.rollback()
    finally:
        conn.close()
