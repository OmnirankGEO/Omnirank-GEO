"""工单B 判据的**真链驱动** —— 与包E 主链同形,只留本单要用的那几段。

夹具里**没有被测对象的替身**:

  · app      = 真 ``api.defensive_publish_api.router``;
  · DB       = conftest 装好的「生产 pg_dump + 迁移(含本包拥有的 051)」一次性库;
  · 钱       = 真 ``middleware.billing.freeze_points`` 写真 ``point_freezes``;
  · 结算     = 真 ``middleware.billing.commit_freeze`` / ``release_freeze``;
  · 派发/收敛 = 真 ``publish_worker`` / 真 ``reconciler``。

**唯一注入的是 provider 边界那一跳**(``provider_call``),走参数不走分支。

🔴 B-1 的判据要看「billing 失败会怎样」,而真 billing 在真库上不会主动失败。
   所以那几条臂**只替换 billing 那两个原语**(``middleware.billing.commit_freeze``
   / ``release_freeze``),其余一律真跑 —— 替的是"钱那一跳的返回形状",
   不是被测的判断逻辑本身(判断逻辑正是被测对象)。替身的返回形状**逐字照抄**
   ``middleware/billing.py`` 里那四种真返回(见 ``BILLING_SHAPES``),
   夹具不许发明生产不会返回的形状。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from services.defensive_geo.publish import execution_budget_policy as _policy
from services.defensive_geo.publish import publish_outbox as _dispatch
from services.defensive_geo.publish import publish_worker as _worker

from tests.defgeo_wob_publish_funding_2026_08_25 import _seed


# ══════════════════════════════════════════════════════════════════════════
# 🔴 billing 的**真返回形状**。逐字取自 middleware/billing.py:
#    :1537 / :1554 / :1591 / :1594(commit_freeze)与 :1741/:1758/:1795/:1797
#    (release_freeze)。夹具只许用这张表里的形状 —— 供生产不会供的东西
#    (例如 ``{}`` 或 ``None``)会让判据去测一个不存在的场景。
# ══════════════════════════════════════════════════════════════════════════
BILLING_SHAPES: dict[str, dict[str, Any]] = {
    # R3:找不到冻结记录(sweeper 白退过、或句柄错表)
    "not_found": {"success": False, "reason": "未找到冻结记录"},
    # R2:跨表撞号歧义 —— 拒绝自动结算
    "ambiguous": {"success": False, "reason": "freeze 跨表撞号歧义,需人工核",
                  "ambiguous": True},
    # R2:幂等返回**相反**终态
    "idempotent_released": {"success": True, "idempotent": True, "status": "released"},
    "idempotent_committed": {"success": True, "idempotent": True, "status": "committed"},
    # R1:幂等返回**同向**终态(= 钱已经按这个方向动过了)
    "idempotent_same_commit": {"success": True, "idempotent": True, "status": "committed"},
    "idempotent_same_release": {"success": True, "idempotent": True, "status": "released"},
}


def fake_billing(shape: str):
    """替 ``commit_freeze`` / ``release_freeze`` 的那一跳。记录被调了几次。"""
    calls: list[dict[str, Any]] = []

    async def _call(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return dict(BILLING_SHAPES[shape])

    _call.calls = calls                                   # type: ignore[attr-defined]
    return _call


def raising_billing(exc: Exception):
    calls: list[dict[str, Any]] = []

    async def _call(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        raise exc

    _call.calls = calls                                   # type: ignore[attr-defined]
    return _call


# ══════════════════════════════════════════════════════════════════════════
# fake provider —— 三态,与真适配器同形
# ══════════════════════════════════════════════════════════════════════════
def order_ref_for(order_sn: str, publish_command_id: str) -> str:
    """夹具与断言**共用**的派生规则。上游单号一单一个(051 上有唯一索引)。"""
    return f"{order_sn}-{str(publish_command_id)[-8:]}"


def accepted(order_sn: str = "WOB-SN") -> _dispatch.ProviderCall:
    def _call(command):                                   # noqa: ANN001
        return _dispatch.ProviderResult(
            kind="accepted", raw_table="mhz_publish_order_items",
            raw_column="status", raw_value="submitted",
            detail=order_ref_for(order_sn, command.get("publish_command_id") or ""))
    return _call


def fixed_ref(order_sn: str) -> _dispatch.ProviderCall:
    """**故意**对每一条命令返回同一个上游单号 —— B-5 要测的正是这一格。"""
    def _call(_command):                                  # noqa: ANN001
        return _dispatch.ProviderResult(
            kind="accepted", raw_table="mhz_publish_order_items",
            raw_column="status", raw_value="submitted", detail=order_sn)
    return _call


def rejected() -> _dispatch.ProviderCall:
    def _call(_command):                                  # noqa: ANN001
        return _dispatch.ProviderResult(
            kind="rejected_no_effect", raw_table="mhz_publish_order_items",
            raw_column="status", raw_value="rejected", detail="上游权威拒稿")
    return _call


def must_not_be_called() -> _dispatch.ProviderCall:
    def _call(_command):                                  # noqa: ANN001
        raise AssertionError("这条路径一次外调都不该发生")
    return _call


# ══════════════════════════════════════════════════════════════════════════
# 走到 confirm 的那一段
# ══════════════════════════════════════════════════════════════════════════
def ready_context(name: str, *, publications: int = 2,
                  tenant: int = _seed.TENANT_A, brand: int | None = None) -> dict[str, Any]:
    from services.defensive_geo import activation_materializer as _mat

    ctx = _seed.seed_accepted_snapshot(publications=publications, activate=True,
                                       tenant=tenant,
                                       brand=brand if brand is not None else _seed.BRAND_A)
    out = _mat.materialize_pending()
    assert out["materialized"] >= 1, f"物化器没物化:{out}"
    revision_id, article_hash = _seed.seed_article(
        brand=ctx["brandId"], body=f"工单B 判据正文 {name} · 这是一段可以发布的稿子。")
    ctx.update({
        "planItemKey": f"plan_{name}_{uuid.uuid4().hex[:6]}",
        "articleRevisionId": revision_id,
        "expectedArticleHash": article_hash,
        "serviceProjectionId": _policy.service_projection_id_for(ctx["acceptedSnapshotId"]),
    })
    return ctx


def preview(client: TestClient, ctx: dict[str, Any], *, who: str = "a") -> dict[str, Any]:
    resp = client.post(
        "/api/defensive-geo/publish/decision-snapshots/preview",
        headers={"Idempotency-Key": "wob-p-" + uuid.uuid4().hex, "X-Test-Identity": who},
        json={
            "planItemKey": ctx["planItemKey"],
            "articleRevisionId": ctx["articleRevisionId"],
            "expectedArticleHash": ctx["expectedArticleHash"],
            "acceptedSnapshotId": ctx["acceptedSnapshotId"],
            "serviceProjectionId": ctx["serviceProjectionId"],
            "brandId": ctx["brandId"],
        },
    )
    assert resp.status_code == 200, f"preview 失败:{resp.text[:900]}"
    return resp.json()


def confirm(client: TestClient, snapshot_response: dict[str, Any], *, who: str = "a"):
    """三个值全部从**冻结面**里取,不是判据自己拼。"""
    frozen = snapshot_response["snapshot"]
    return client.post(
        f"/api/defensive-geo/publish/decision-snapshots/{frozen['decisionSnapshotId']}/confirm",
        headers={"Idempotency-Key": "wob-c-" + uuid.uuid4().hex, "X-Test-Identity": who},
        json={"expectedHash": frozen["canonicalHash"],
              "expectedVersion": frozen["snapshotVersion"]},
    )


def confirmed_command(client: TestClient, name: str, *, who: str = "a",
                      tenant: int = _seed.TENANT_A,
                      brand: int | None = None) -> dict[str, Any]:
    ctx = ready_context(name, tenant=tenant, brand=brand)
    snap = preview(client, ctx, who=who)["snapshotResponse"]
    resp = confirm(client, snap, who=who)
    assert resp.status_code == 200, f"confirm 失败:{resp.text[:900]}"
    ctx["publishCommandId"] = resp.json()["publishCommandId"]
    return ctx


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def drain(rounds: int = 8) -> None:
    """🔴 **共享队列的分母清理**(本仓记过:共享队列判据先清分母)。

    ``reconcile_once`` 是全局的 —— 它会顺手把别的判据留下的命令也收敛掉,
    于是「本条判据前后钱包差多少」里混进别人家的账。跑到一轮零 action 为止。
    """
    for _ in range(rounds):
        out = run(_worker.reconcile_tick(limit=200))
        if not out["actions"]:
            return


def drain_outbox(provider_call=None, rounds: int = 8) -> None:
    """把 outbox 排空 —— ``dispatch_pending`` 是批量的,别人的行会占满我的批次。"""
    call = provider_call or accepted("WOB-DRAIN")
    for _ in range(rounds):
        out = run(_worker.dispatch_pending(limit=50, provider_call=call))
        if not out["dispatched"] and not out["deferred"] and not out.get("quarantined"):
            return


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


@pytest.fixture(scope="module", autouse=True)
def base(_schema) -> Iterator[None]:                      # noqa: ANN001
    import auth.brand_access  # noqa: F401,PLC0415

    _seed.install_base_rows()
    yield
