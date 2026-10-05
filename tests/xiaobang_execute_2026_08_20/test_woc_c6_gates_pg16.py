"""工单 C-6 · 小榜两个确认闸(Codex 终审 P1-14)。**真 HTTP + 真库**。

两件事一起坏在确认这一跳上:

  (a) **旧 intent 确认 A 执行 B** —— ``_recomputed_drift_hashes`` 在
      ``preview.canonical_input`` 缺失时把 ``row`` 自己的列原样返回,
      ``drift_reason`` 那三支比较于是 ``x == x``,**结构性恒真**。
      也就是说存量 intent 上「确认的对象 == 现在要执行的对象」这句话
      从来没有被验过;
  (b) **0 算力被当成可确认报价** —— ``frozen_estimate_amount`` 只挡 ``None``,
      ``0`` 原样返回 ⇒ 屏幕上写「本次预计消耗你的算力 0,确认后开始」⇒
      她确认 ⇒ execute ⇒ ``freeze_points`` 的 ``total_cost == 0`` 短路返
      零句柄 ⇒ ``FreezeProducedNoHandle`` ⇒ 对外**稳定 500**。

🔴 本文件挂在 ``tests/xiaobang_execute_2026_08_20`` 而不是工单C 自己的包里,
   是因为这里已经有一套**真 HTTP 打真库**的五阶段底座。
   把判据挂在纯函数上会重演本仓记过的那一条:
   「抽纯函数让逻辑可判,调用点反而没人守」。
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.xiaobang_execute_2026_08_20.conftest import (
    MEDIA_OK, OWNER_UID, PUBLISH_POINTS, conn_for, one, rows,
)

PREFIX = "/api/xiaobang/operations/publish_center"


def _client(monkeypatch, world):
    """与本包既有判据**同一个**客户端构造(键名/形状逐字相同)。"""
    import api.xiaobang_operations_api as ops

    class _Ctx:
        owner_user_id = OWNER_UID

        def public_context(self):
            return {"brand_id": world["brand_id"], "brand_name": "甲品牌",
                    "geo_post_id": world["post_id"], "data_updated_at": None}

    monkeypatch.setattr(ops, "_authorized_context", lambda request, refs, page="": _Ctx())

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.user = {"user_id": OWNER_UID, "username": "owner_xbexec",
                              "is_admin": False, "permissions": ["publish:write"]}
        request.state.organization_identity = None
        return await call_next(request)

    app.include_router(ops.router)
    return TestClient(app, raise_server_exceptions=False)


def _prepare(client, world, *, media_id=MEDIA_OK):
    return client.post(PREFIX + "/prepare", json={
        "prepare_request_id": "wocc6-" + uuid.uuid4().hex[:12],
        "selection": {"brand_id": world["brand_id"], "geo_post_id": world["post_id"],
                      "channel_option_id": "svideo:" + str(media_id)},
    })


def _confirm(client, intent_id, revision):
    return client.post(PREFIX + "/intents/" + intent_id + "/confirm",
                       json={"intent_revision": revision,
                             "user_action_challenge": "user-clicked-confirm"})


def _execute(client, intent_id, revision, *, request_id):
    return client.post(PREFIX + "/intents/" + intent_id + "/execute",
                       json={"intent_revision": revision,
                             "execution_request_id": request_id})


def _strip_canonical_input(dsn, intent_id) -> None:
    """把 intent 打回**窗G 之前**的存量形态:preview 里没有 canonical_input。

    🔴 这不是"造一个假形状":窗G 之前建的 intent 的 preview 里确实没有这一格,
       而它们在 TTL 内仍然可以被确认/执行。改的是那一格本身,不是别的。
    """
    row = one(dsn, "SELECT preview FROM xiaobang_operation_intents WHERE intent_id=%s",
              (intent_id,))
    preview = dict(row["preview"] or {})
    preview.pop("canonical_input", None)
    conn = conn_for(dsn)
    try:
        conn.cursor().execute(
            "UPDATE xiaobang_operation_intents SET preview=%s::jsonb WHERE intent_id=%s",
            (json.dumps(preview, ensure_ascii=False), intent_id))
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# (a) 旧 intent 的自比较旁路 —— confirm 与 execute **两处都要封**
# ══════════════════════════════════════════════════════════════════════════
def test_c6_00_premise_a_fresh_intent_really_carries_canonical_input(
        monkeypatch, live_db, world) -> None:
    """前提自证:新建的 intent **确实带** canonical_input。

    没有这一条,下面"缺了就拒"可能只是因为**所有** intent 都缺 ——
    那时正常确认全被拒,而判据看起来是绿的。
    """
    client = _client(monkeypatch, world)
    body = _prepare(client, world).json()
    row = one(live_db, "SELECT preview FROM xiaobang_operation_intents WHERE intent_id=%s",
              (body["intent_id"],))
    assert (row["preview"] or {}).get("canonical_input"), row["preview"]


def test_c6_01_legacy_intent_cannot_be_confirmed(monkeypatch, live_db, world) -> None:
    """🔴 缺 canonical_input ⇒ confirm **拒**,不再拿恒真比较冒充"已核对"。

    拆红:把 ``_assert_reverifiable(live)`` 从 ``operation_confirm`` 里摘掉
    ⇒ 确认照过 ⇒ 本条红。
    """
    client = _client(monkeypatch, world)
    body = _prepare(client, world).json()
    _strip_canonical_input(live_db, body["intent_id"])

    res = _confirm(client, body["intent_id"], int(body["intent_revision"]))
    assert res.status_code == 409, res.text[:600]
    assert res.json()["detail"]["error_code"] == "INTENT_NOT_REVERIFIABLE", res.text[:600]
    assert res.json()["detail"]["next_action"] == "重新准备", res.text[:600]
    # 零副作用:回执一张都不许签(确认在本系统里就是授权)
    assert rows(live_db, "SELECT 1 FROM xiaobang_confirmation_receipts") == []


def test_c6_02_legacy_intent_cannot_be_executed_either(
        monkeypatch, live_db, world) -> None:
    """🔴 execute 侧**同一道门**。

    只在 confirm 拦的话,本次上线**之前**已经拿到回执的那批存量 intent
    仍然可以执行 —— 而它们恰恰是证不了「确认对象 == 执行对象」的那一批。
    所以先正常确认(带 canonical_input),再打回存量形态,然后执行。

    拆红:把 execute 侧那一行摘掉 ⇒ 执行照过 ⇒ 本条红。
    """
    client = _client(monkeypatch, world)
    body = _prepare(client, world).json()
    confirmed = _confirm(client, body["intent_id"], int(body["intent_revision"]))
    assert confirmed.status_code == 200, confirmed.text[:600]
    revision = int(confirmed.json()["intent_revision"])

    _strip_canonical_input(live_db, body["intent_id"])

    # 🔴 零副作用按**增量**判,不按"表里必须是空的":``world`` 夹具自己就种了
    #    一条基线 ``mhz_publish_orders``(它是 geo post 的既有订单)。
    #    拿绝对零当断言会把夹具的种子行算成"这次执行的副作用",
    #    那种红与被测代码无关(我第一版就是这么红的)。
    before_orders = len(rows(live_db, "SELECT 1 FROM mhz_publish_orders"))
    before_freezes = len(rows(live_db, "SELECT 1 FROM point_freezes"))

    res = _execute(client, body["intent_id"], revision,
                   request_id="wocc6-" + uuid.uuid4().hex[:12])
    assert res.status_code == 409, res.text[:600]
    assert res.json()["detail"]["error_code"] == "INTENT_NOT_REVERIFIABLE", res.text[:600]
    assert len(rows(live_db, "SELECT 1 FROM mhz_publish_orders")) == before_orders
    assert len(rows(live_db, "SELECT 1 FROM point_freezes")) == before_freezes


def test_c6_03_the_object_is_really_rechecked_at_confirm(
        monkeypatch, live_db, world) -> None:
    """🔴 「确认的对象 == 现在这个对象」——**真的被验了**。

    这一条是 (a) 的正面兑现:prepare 之后把投放价改掉,
    confirm 必须以**漂移**拒绝并且一张回执都不签。

    改动前,只要 intent 是存量形态,这条路径**结构上不可能红**
    (自比较恒真)。所以本条与上面两条是一组:
    上面证明旁路被封,这一条证明封了之后真检查是有效的。

    🔴 为什么打 confirm 而不是 execute:execute 侧的领域资格门
    (``execute_publish_image_note`` 第 ① 步)排在**漂移之后但更靠近用户**,
    改账号资格会先命中 ``PUBLISH_CHANNEL_NOT_ELIGIBLE`` —— 那是**另一条**
    正确的路,不是本条要验的东西。两把锁叠在同一路径上时,
    「相关判据全绿」证明不了哪一把在守(本仓记过)。
    """
    client = _client(monkeypatch, world)
    body = _prepare(client, world).json()

    # prepare 之后改价:compute_quote_hash 必须跟着变 ⇒ 漂移命中
    conn = conn_for(live_db)
    try:
        conn.cursor().execute(
            "UPDATE mhz_short_video SET our_price_points=%s WHERE id=%s",
            (PUBLISH_POINTS + 5000, MEDIA_OK))
    finally:
        conn.close()

    res = _confirm(client, body["intent_id"], int(body["intent_revision"]))
    assert res.status_code == 409, res.text[:600]
    detail = res.json()["detail"]
    assert detail["error_code"] == "OBJECT_OR_AUTHORITY_DRIFT", detail
    assert detail["reason_code"] == "compute_quote_hash", detail
    assert rows(live_db, "SELECT 1 FROM xiaobang_confirmation_receipts") == []


def test_c6_04_a_legacy_intent_would_have_passed_the_same_drift(
        monkeypatch, live_db, world) -> None:
    """判别力配对:**同一个**漂移场景下,存量形态走的是另一条码。

    上一条证明"改了对象会被漂移拦下";本条证明改动**之前**那批 intent
    在同一场景下拿到的不是漂移拒绝 —— 现在它拿到的是
    ``INTENT_NOT_REVERIFIABLE``(我们承认自己核不了),
    而不是一个恒真比较给出的"没有漂移,放行"。
    """
    client = _client(monkeypatch, world)
    body = _prepare(client, world).json()
    _strip_canonical_input(live_db, body["intent_id"])

    conn = conn_for(live_db)
    try:
        conn.cursor().execute(
            "UPDATE mhz_short_video SET our_price_points=%s WHERE id=%s",
            (PUBLISH_POINTS + 5000, MEDIA_OK))
    finally:
        conn.close()

    res = _confirm(client, body["intent_id"], int(body["intent_revision"]))
    assert res.status_code == 409, res.text[:600]
    assert res.json()["detail"]["error_code"] == "INTENT_NOT_REVERIFIABLE", res.text[:600]
    assert rows(live_db, "SELECT 1 FROM xiaobang_confirmation_receipts") == []


# ══════════════════════════════════════════════════════════════════════════
# (b) 0 算力 —— Owner 2026-08-25 选项 A:0 价不可确认
# ══════════════════════════════════════════════════════════════════════════
def _zero_price(dsn) -> None:
    """把那个账号的投放价压成 0。

    🔴 这是**可达的生产形态**,不是造一个不可能的库:
       ``services/media_price_projection.resolve_price_points`` 的三档取价
       都只在 > 0 时返回,兜底 ``yuan_to_points`` 在 v<=0 时返 0 ——
       没有进货价 / markup 配成 0 / 价格字段脏,都会落到 0。
    """
    conn = conn_for(dsn)
    try:
        conn.cursor().execute(
            "UPDATE mhz_short_video SET our_price_points=0, price=0 WHERE id=%s",
            (MEDIA_OK,))
    finally:
        conn.close()


def test_c6_10_zero_price_is_not_shown_as_confirmable(
        monkeypatch, live_db, world) -> None:
    """🔴 屏幕上不许再出现「本次预计消耗你的算力 0,确认后开始」。

    拆红:把 ``frozen_estimate_amount`` 的 ``amount <= 0`` 改回只挡 None
    ⇒ ``confirmable`` 变 True ⇒ 本条红。
    """
    _zero_price(live_db)
    client = _client(monkeypatch, world)
    body = _prepare(client, world).json()

    estimate = body["compute_estimate"]
    assert estimate["confirmable"] is False, estimate
    assert "amount" not in estimate, estimate
    assert "确认后开始" not in estimate["headline"], estimate
    assert estimate.get("next_action"), estimate


def test_c6_11_zero_price_confirm_is_refused_with_zero_side_effect(
        monkeypatch, live_db, world) -> None:
    """0 价 ⇒ confirm 409 ``COMPUTE_QUOTE_MISSING``,回执一张不签。

    「确认」在本系统里就是授权;没有有效数字的授权不是授权。
    """
    _zero_price(live_db)
    client = _client(monkeypatch, world)
    body = _prepare(client, world).json()

    res = _confirm(client, body["intent_id"], int(body["intent_revision"]))
    assert res.status_code == 409, res.text[:600]
    assert res.json()["detail"]["error_code"] == "COMPUTE_QUOTE_MISSING", res.text[:600]
    assert rows(live_db, "SELECT 1 FROM xiaobang_confirmation_receipts") == []


def test_c6_12_the_intent_really_carried_a_zero_not_a_null(
        monkeypatch, live_db, world) -> None:
    """判别力:被拒的那一条上,库里存的是 **0**,不是 NULL。

    没有这一条,上面两条可能只是在测「没报过价被拒」那条老路径 ——
    而 0 与 None 分开正是本项要改的东西。
    """
    from services.defensive_geo.xiaobang.compute_estimate import (
        frozen_estimate_amount, raw_estimate_amount,
    )

    _zero_price(live_db)
    client = _client(monkeypatch, world)
    body = _prepare(client, world).json()
    row = one(live_db,
              "SELECT compute_quote_amount FROM xiaobang_operation_intents "
              " WHERE intent_id=%s", (body["intent_id"],))
    assert row["compute_quote_amount"] == 0, row
    assert raw_estimate_amount(row) == 0, row
    assert frozen_estimate_amount(row) is None, row


def test_c6_13_execute_side_refuses_with_409_not_a_500(live_db) -> None:
    """execute 侧的纵深门:0 价 ⇒ **409 typed**,不是 500。

    改动前这条路径的终点是 ``FreezeProducedNoHandle`` ⇒
    ``SETTLEMENT_HANDLE_INVALID`` ⇒ HTTP **500**(裸系统错)。
    她看到的是"提交失败,请稍后重试",而重试一万次都是同一个结果。

    🔴 直接调 adapter 本体:上游 confirm 现在已经拦住了这一档,所以
       正常 HTTP 路径走不到这里 —— 但纵深防御必须自己可判,
       不能因为"平时触发不到"就没有判据(本仓记过「不可达是强断言」)。
    """
    from services.xiaobang_intent import IntentError
    from services.xiaobang_publish_execute import execute_publish_image_note

    intent_row = {
        "intent_id": "wocc6-zero",
        "compute_quote_amount": 0,
        "preview": {"execution_binding": {"channel_option_id": "svideo:" + str(MEDIA_OK),
                                          "media_id": MEDIA_OK},
                    "form_prefill": {"geo_post_id": 1}},
    }
    conn = conn_for(live_db)
    try:
        cur = conn.cursor()
        with pytest.raises(IntentError) as exc:
            execute_publish_image_note(
                cur, intent_row=intent_row, receipt={"receipt_id": "xcr_zero"},
                identity={"tenant_owner_user_id": OWNER_UID},
                settlement={}, daily_limit=10)
    finally:
        conn.close()
    assert exc.value.code == "COMPUTE_QUOTE_MISSING", exc.value.code
    assert exc.value.http_status == 409, exc.value.http_status


def test_c6_14_a_normal_priced_intent_is_still_confirmable(
        monkeypatch, live_db, world) -> None:
    """反向对照:正常价的意图**照常**可确认。

    没有这一条,上面那几条可能只是因为把所有意图都拒了 ——
    "拒绝一切"与"只拒 0 价"在只验拒绝那一侧的判据里长得一样。
    """
    client = _client(monkeypatch, world)
    body = _prepare(client, world).json()
    assert body["compute_estimate"]["confirmable"] is True, body["compute_estimate"]
    assert body["compute_estimate"]["amount"] == PUBLISH_POINTS, body["compute_estimate"]
    res = _confirm(client, body["intent_id"], int(body["intent_revision"]))
    assert res.status_code == 200, res.text[:600]
