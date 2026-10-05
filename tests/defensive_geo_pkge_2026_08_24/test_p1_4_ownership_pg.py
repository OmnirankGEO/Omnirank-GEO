"""终审 P1-4 —— ``acceptedSnapshotId`` 的对象级归属(工单③,成对判据)。

═══════════════════════════════════════════════════════════════════════
🔴 先证伪工单给的根因(本仓铁律:工单给的根因也要先证伪)
═══════════════════════════════════════════════════════════════════════
终审把 P1-4 记成「``GET /publish/delivery-todo`` **无归属过滤**」。
我在车头 ``fa8aecc50`` 与初审 SHA ``842bc1388`` 上都逐字复核过那条查询:

    WHERE s.tenant_owner_id = %s AND s.accepted_snapshot_id = %s

**行级归属过滤一直都在**。照字面去"补一个 tenant 过滤"会补出第二个同义谓词
(同一谓词写两处,必有一处没人验),而真正的洞原封不动。

真正缺的是**对象级**那一层:``acceptedSnapshotId`` 从来没有被验证过属于调用方。
  · ``preview`` 只验 ``brandId``;``acceptedSnapshotId`` 直接进 slot 派生与
    ``_accepted_snapshot_hash``(``WHERE id = %s``,零归属条件)⇒
    甲可以用**自己的品牌** + **乙的那一版客户承诺**建出 slot 与 command;
  · ``delivery-todo`` 行级过滤挡住了"看到别人的行",但没挡住"拿别人的 id 来问" ——
    返回 200 空列表,与本族其余五处「跨租户同形 404」的口径也不一致。

所以本文件的成对判据打的是**归属**,不是"有没有 WHERE 子句":
  · 本人问自己的 ⇒ 200;
  · 他人问同一个 ⇒ 404,且与"这个 id 根本不存在"**同形**(不给存在性预言机)。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from services.defensive_geo.publish import execution_budget_policy as _policy

from tests.defensive_geo_pkge_2026_08_24 import _seed


@pytest.fixture(scope="module", autouse=True)
def _base(_schema) -> Iterator[None]:                     # noqa: ANN001
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


def _todo(client: TestClient, accepted_snapshot_id: int, *, who: str):
    return client.get(
        "/api/defensive-geo/publish/delivery-todo",
        params={"acceptedSnapshotId": accepted_snapshot_id},
        headers={"X-Test-Identity": who},
    )


def _preview(client: TestClient, ctx: dict[str, Any], *, who: str, brand: int):
    revision_id, article_hash = _seed.seed_article(
        brand=brand, body=f"包E 归属判据正文 {uuid.uuid4().hex[:8]}")
    return client.post(
        "/api/defensive-geo/publish/decision-snapshots/preview",
        headers={"Idempotency-Key": "pkge-own-" + uuid.uuid4().hex,
                 "X-Test-Identity": who},
        json={
            "planItemKey": "plan_ownership",
            "articleRevisionId": revision_id,
            "expectedArticleHash": article_hash,
            "acceptedSnapshotId": ctx["acceptedSnapshotId"],
            "serviceProjectionId": _policy.service_projection_id_for(
                ctx["acceptedSnapshotId"]),
            "brandId": brand,
        },
    )


# ══════════════════════════════════════════════════════════════════════════
# 00 · 先把「工单说的那条过滤本来就在」这件事钉成判据
# ══════════════════════════════════════════════════════════════════════════
def test_00_row_level_tenant_filter_was_already_there() -> None:
    """结构锚:``delivery-todo`` 的行级过滤是**存量**,不是本包补的。

    钉住它是为了两件事:
      ① 记录我对工单根因的证伪(它一直都在);
      ② 谁哪天把它删了要红 —— 对象级那一层不能顶替行级这一层,两层都要。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "api" / "defensive_publish_api.py").read_text(
        encoding="utf-8", errors="replace")
    marker = "WHERE s.tenant_owner_id = %s AND s.accepted_snapshot_id = %s"
    assert marker in src, "delivery-todo 的行级归属过滤不见了"


# ══════════════════════════════════════════════════════════════════════════
# 01-02 · delivery-todo 成对:本人见 / 他人不见
# ══════════════════════════════════════════════════════════════════════════
def test_01_owner_can_read_own_delivery_todo(client: TestClient) -> None:
    """本人问自己的 ⇒ 200(**不许 404,也不许 500**)。

    没有这一半,「他人 404」可以被一个"对谁都 404"的实现满足。
    """
    ctx = _seed.seed_accepted_snapshot(tenant=_seed.TENANT_A, brand=_seed.BRAND_A,
                                       publications=2, activate=False)
    resp = _todo(client, ctx["acceptedSnapshotId"], who="a")
    assert resp.status_code == 200, f"本人读自己的待办被拒:{resp.text[:500]}"
    body = resp.json()
    assert "items" in body and "pendingCount" in body, body


def test_02_other_tenant_gets_404_not_empty_200(client: TestClient) -> None:
    """他人问同一个 ⇒ **404**,不是 200 空列表。"""
    ctx = _seed.seed_accepted_snapshot(tenant=_seed.TENANT_A, brand=_seed.BRAND_A,
                                       publications=2, activate=False)
    resp = _todo(client, ctx["acceptedSnapshotId"], who="b")
    assert resp.status_code == 404, (
        f"跨租户读到了 {resp.status_code}:{resp.text[:500]} —— "
        "200 空列表与「你自己这一单没事可做」长得一模一样,归属闸等于没有")
    assert (resp.json().get("detail") or {}).get("code") == "OBJECT_NOT_FOUND", resp.text


def test_03_foreign_and_absent_are_byte_identical(client: TestClient) -> None:
    """🔴 跨租户 404 与"根本不存在" 404 必须**逐字节相同**。

    两者不同 = 存在性预言机:对方可以靠信封差异枚举出哪些 id 是真的。
    """
    ctx = _seed.seed_accepted_snapshot(tenant=_seed.TENANT_A, brand=_seed.BRAND_A,
                                       publications=1, activate=False)
    foreign = _todo(client, ctx["acceptedSnapshotId"], who="b")
    absent = _todo(client, 99_999_999, who="b")
    assert foreign.status_code == absent.status_code == 404
    assert json.loads(foreign.text) == json.loads(absent.text), (
        f"两种 404 的信封不同:\n跨租户 {foreign.text[:300]}\n不存在 {absent.text[:300]}")


# ══════════════════════════════════════════════════════════════════════════
# 10-12 · preview 同一把闸(真正的洞在这里)
# ══════════════════════════════════════════════════════════════════════════
def test_10_preview_refuses_someone_elses_accepted_snapshot(client: TestClient) -> None:
    """🔴 甲拿**自己的品牌** + **乙的客户承诺** ⇒ 必须 404。

    这一格是 P1-4 真正的洞:``brandId`` 验过了,``acceptedSnapshotId`` 没验。
    不修的话,甲能把发布对象绑到乙家的合同上 —— 而且钱是从甲的钱包冻。
    """
    lai = _seed.seed_accepted_snapshot(tenant=_seed.TENANT_B, brand=_seed.BRAND_B,
                                       publications=2, activate=False)
    resp = _preview(client, lai, who="a", brand=_seed.BRAND_A)
    assert resp.status_code == 404, (
        f"甲用乙的 acceptedSnapshotId 建出了 {resp.status_code}:{resp.text[:600]}")
    assert (resp.json().get("detail") or {}).get("code") == "OBJECT_NOT_FOUND", resp.text


def test_11_preview_owner_path_still_works(client: TestClient) -> None:
    """反向对照:本人用自己的那一份,同一条路径必须走得通。

    只有这一条在,上面那条 404 才不是"对谁都 404"。
    """
    from services.defensive_geo import activation_materializer as _mat

    ctx = _seed.seed_accepted_snapshot(tenant=_seed.TENANT_A, brand=_seed.BRAND_A,
                                       publications=2, activate=True)
    _mat.materialize_pending()
    resp = _preview(client, ctx, who="a", brand=_seed.BRAND_A)
    assert resp.status_code == 200, f"本人路径也被拦了:{resp.text[:700]}"


def test_12_ownership_gate_runs_before_any_slot_row_is_created(client: TestClient) -> None:
    """🔴 归属闸必须在 ``ensure_slot`` **之前**。

    放在之后 = "先按别人的合同建好格子再说":即使拒绝了,库里也多了一行
    绑错合同的 slot。本仓记过同形态:「门必须在 DELETE 之前」。
    """
    from services.defensive_geo.publish import store as _store

    lai = _seed.seed_accepted_snapshot(tenant=_seed.TENANT_B, brand=_seed.BRAND_B,
                                       publications=2, activate=False)
    before = _seed.counts()
    resp = _preview(client, lai, who="a", brand=_seed.BRAND_A)
    assert resp.status_code == 404
    after = _seed.counts()
    assert after[_store.SNAPSHOT_TABLE] == before[_store.SNAPSHOT_TABLE], "多了 snapshot"
    assert after["point_freezes"] == before["point_freezes"], "被拒绝的调用冻了钱"

    # slot 表单独数一次(counts() 里没有它)
    from tests.defensive_geo_pkge_2026_08_24.conftest import connect

    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT COUNT(*) AS n FROM {_store.SLOT_TABLE} WHERE accepted_snapshot_id = %s",
            (lai["acceptedSnapshotId"],))
        slots = int(cur.fetchone()["n"])
        conn.rollback()
    finally:
        conn.close()
    assert slots == 0, (
        f"被拒绝的跨租户 preview 仍然建出了 {slots} 个 slot —— 闸放在 ensure_slot 之后了")
