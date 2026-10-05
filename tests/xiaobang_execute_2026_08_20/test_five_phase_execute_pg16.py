"""WO-B ②③:五阶段跑到 **execute**,真 PG16 全链 + 资金终态 + 资格翻转矩阵。

## 判据形状(工单逐条)

| 工单要求 | 本文件 |
|----------|--------|
| 真 PG16 五阶段全链 | `test_five_phase_chain_reaches_a_real_publish_command` |
| 崩溃重放一发 | `test_crash_before_commit_leaves_nothing_and_replays_clean` |
| 幂等二发(replayed 断言) | `test_second_execute_is_a_replay_not_a_second_charge` |
| 🔴 资金判据打「冻结终态==意图」不打返回值 | `test_frozen_amount_equals_the_intent_quote`(读库,不看回包) |
| 资格翻转矩阵 ×(prepare/execute) | `test_eligibility_matrix_at_prepare` / `..._at_execute` |

## 🔴 为什么资金判据不看返回值

`execute` 返回 200 只证明「这次调用没报错」。幂等重放也返回 200 ——
而重放的语义恰恰是「我这次**没有**动钱」。所以资金判据一律打**终态**:

    mhz_publish_order_items.reserved_amount / freeze_id / billing_mode
    user_wallets.frozen_points

并且断言它 **== intent 冻结时那个报价**(`compute_quote_amount`),
不是「等于服务端此刻算出来的数」—— 后者是自己跟自己比,永远相等。
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.xiaobang_execute_2026_08_20.conftest import (
    MEDIA_FULL, MEDIA_NO_IMAGE, MEDIA_OK, OWNER_UID, PUBLISH_POINTS,
    one, rows, wallet,
)

PREFIX = "/api/xiaobang/operations/publish_center"


def _client(monkeypatch, world, *, is_admin=False):
    """真 HTTP + 真库。只替「哪个 brand 归谁」,其余全走真的。

    🔴 `request.state.user` 用**生产形状**(`user_id`,没有 `id`)——
       中间件写的就是这个键(`auth/jwt_utils.create_jwt` 的 payload)。
       夹具写 `{"id": …}` 的话,`_actor_binding` 那个 KeyError 会被夹具兜住,
       而生产上五阶段端点全 500。这一条前提本身就是本包修掉的一个洞。
    """
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
                              "is_admin": is_admin, "permissions": ["publish:write"]}
        request.state.organization_identity = None
        return await call_next(request)

    app.include_router(ops.router)
    return TestClient(app, raise_server_exceptions=False)


def _prepare(client, world, *, media_id=MEDIA_OK, request_id=None):
    return client.post(PREFIX + "/prepare", json={
        "prepare_request_id": request_id or ("xbexec-" + uuid.uuid4().hex[:12]),
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


def _run_to_confirmed(client, world, *, media_id=MEDIA_OK):
    prepared = _prepare(client, world, media_id=media_id)
    assert prepared.status_code == 200, prepared.text[:600]
    body = prepared.json()
    assert body.get("replayed") is False, "幂等命中了旧行 —— 这条判据没走到当前 producer"
    intent_id = body["intent_id"]
    confirmed = _confirm(client, intent_id, int(body["intent_revision"]))
    assert confirmed.status_code == 200, confirmed.text[:600]
    return intent_id, int(confirmed.json()["intent_revision"]), body


# ══════════════════════════════════════════════════════════════════════════
# ② 五阶段全链
# ══════════════════════════════════════════════════════════════════════════

def test_five_phase_chain_reaches_a_real_publish_command(monkeypatch, world):
    """🔴 主链:prepare → confirm → execute,落成一条**真的**投放 command。

    每一跳都是真的:HTTP → handler → 真库 intent → 真回执 → 真 claim →
    真 freeze → 真订单行。把 `executable` 改回 False,这条当场红。
    """
    client = _client(monkeypatch, world)
    intent_id, revision, prepared = _run_to_confirmed(client, world)

    # 🔴 ③ 的直接产物:发布类**报得出算力**了(在这之前恒 pending_domain_adapter)
    assert prepared["compute_quote"]["amount"] == PUBLISH_POINTS, prepared["compute_quote"]

    before = wallet(world["dsn"])
    got = _execute(client, intent_id, revision, request_id="exec-" + uuid.uuid4().hex[:12])
    assert got.status_code == 200, got.text[:800]
    assert got.json()["intent_state"] == "execution_linked", got.json()

    # 领域侧真的建了单(读库,不看回包)
    items = rows(world["dsn"],
                 "SELECT * FROM mhz_publish_order_items WHERE media_id = %s", (MEDIA_OK,))
    assert len(items) == 1, items
    item = items[0]
    assert int(item["source_geo_post_id"]) == world["post_id"], item
    assert int(item["source_post_revision_id"]) == world["revision_id"], item
    assert str(item["billing_mode"]) == "freeze_per_item", item

    after = wallet(world["dsn"])
    assert int(after["frozen_points"]) - int(before["frozen_points"]) == PUBLISH_POINTS


def test_frozen_amount_equals_the_intent_quote(monkeypatch, world):
    """🔴🔴 资金判据:**冻结终态 == 意图**,不是"返回了 success"。

    用户按下确认时看到的那个数字(intent 的 `compute_quote_amount`),
    和真正被冻住的那笔钱,必须是同一个。分别读两处再比,不读回包。
    """
    client = _client(monkeypatch, world)
    intent_id, revision, _ = _run_to_confirmed(client, world)
    assert _execute(client, intent_id, revision,
                    request_id="exec-" + uuid.uuid4().hex[:12]).status_code == 200

    intent = one(world["dsn"],
                 "SELECT compute_quote_amount, execution_id, intent_state, domain_ref"
                 "  FROM xiaobang_operation_intents WHERE intent_id = %s", (intent_id,))
    item = one(world["dsn"],
               "SELECT reserved_amount, cost_points, freeze_id, freeze_table,"
               "       settlement_status, payer_user_id"
               "  FROM mhz_publish_order_items WHERE media_id = %s", (MEDIA_OK,))

    assert intent["compute_quote_amount"] is not None, "意图根本没报价 —— 这条判据没有分母"
    assert int(item["reserved_amount"]) == int(intent["compute_quote_amount"]), (item, intent)
    assert int(item["cost_points"]) == int(intent["compute_quote_amount"]), (item, intent)
    # 句柄必须完整:freeze_id 为空的"成功"就是 P0-6 的零记账形态
    assert item["freeze_id"], item
    assert item["freeze_table"], item
    assert int(item["payer_user_id"]) == OWNER_UID, item
    assert str(item["settlement_status"]) == "frozen", item
    # 协调层只记引用,不复制领域终态
    assert intent["execution_id"] == "pubcmd_" + _receipt_id(world["dsn"], intent_id), intent
    assert dict(intent["domain_ref"]).get("settlement_state") == "reserved", intent


def _command_items(dsn):
    """本次执行真正建出来的订单项。

    🔴 **不能**直接 `SELECT id FROM mhz_publish_order_items`:夹具为了把
       `MEDIA_FULL` 那档的今日额度用满,先塞了几行占额度的 item ——
       它们没有 `command_request_id`。分不清这两种行的话,
       「零副作用」判据会被夹具自己的行打红(而那不是产品行为)。
    """
    return rows(dsn, "SELECT id FROM mhz_publish_order_items"
                     " WHERE command_request_id IS NOT NULL", ())


def _receipt_id(dsn, intent_id):
    return one(dsn, "SELECT receipt_id FROM xiaobang_confirmation_receipts"
                    " WHERE intent_id = %s", (intent_id,))["receipt_id"]


def test_second_execute_is_a_replay_not_a_second_charge(monkeypatch, world):
    """🔴 幂等二发:第二次必须 `replayed=True`,而且**钱没有再动一次**。

    幂等的语义是「第二次给同一个答案」,不是「第二次别再做一遍」。
    所以两件事都要断:回包的 `replayed` 位 + 资金/订单终态没变。
    """
    client = _client(monkeypatch, world)
    intent_id, revision, _ = _run_to_confirmed(client, world)
    request_id = "exec-" + uuid.uuid4().hex[:12]

    first = _execute(client, intent_id, revision, request_id=request_id)
    assert first.status_code == 200, first.text[:600]
    assert first.json().get("replayed") is False, first.json()
    after_first = wallet(world["dsn"])

    # 🔴 重试的客户端手里还是**原来那个** revision(它没收到第一次的响应)。
    #    拿 first 返回的新 revision 去发是"上帝视角",打不到真实重放形态。
    second = _execute(client, intent_id, revision, request_id=request_id)
    assert second.status_code == 200, second.text[:600]
    assert second.json().get("replayed") is True, (
        "第二发不是回放 —— 幂等键没生效,这一笔会被扣两次")
    assert second.json().get("command_id") == first.json().get("execution_id")         or second.json().get("command_id"), second.json()

    # 🔁 反向对照:**换一个** execution_request_id 不许拿到这份回放
    #    (回放的键是执行请求 id,不是"状态已经是 execution_linked")。
    other = _execute(client, intent_id, revision, request_id="other-" + uuid.uuid4().hex[:8])
    assert other.status_code == 409, other.text[:300]

    assert len(_command_items(world["dsn"])) == 1, "回放建了第二个订单项"
    assert wallet(world["dsn"]) == after_first, "回放又冻了一次钱"


def test_crash_before_commit_leaves_nothing_and_replays_clean(monkeypatch, world):
    """🔴 崩溃重放:commit 之前崩 → **零残留**;重放一发照样成功。

    崩溃点选在 `link_execution` 之后的**同一事务内**(用一个非 IntentError 异常),
    也就是「claim 有了、钱冻了、单建了,但还没 commit」那一格 ——
    这是最贵的那个窗口:如果 claim 先落地而副作用回滚,重放会拿到
    `replayed=True` 却什么都没做,用户永远等不到结果。
    """
    import api.xiaobang_operations_api as ops

    client = _client(monkeypatch, world)
    intent_id, revision, _ = _run_to_confirmed(client, world)
    before = wallet(world["dsn"])

    boom = {"n": 0}
    real_link = ops.link_execution

    def _crash(*args, **kwargs):
        boom["n"] += 1
        raise RuntimeError("模拟进程在 commit 之前崩了")

    monkeypatch.setattr(ops, "link_execution", _crash)
    crashed = _execute(client, intent_id, revision, request_id="crash-" + uuid.uuid4().hex[:8])
    assert boom["n"] == 1, "崩溃点没被走到 —— 这条判据打空了"
    assert crashed.status_code >= 500, crashed.status_code

    # 零残留:订单、幂等 claim、钱,一样都不许留下
    assert _command_items(world["dsn"]) == []
    assert rows(world["dsn"], "SELECT request_id FROM publish_idempotency_keys", ()) == []
    assert wallet(world["dsn"]) == before, "崩溃回滚之后钱还冻着"
    # 回执也必须回滚成"没消费" —— 否则用户的确认被一次失败的调用作废了
    receipt = one(world["dsn"], "SELECT consumed_at FROM xiaobang_confirmation_receipts"
                                " WHERE intent_id = %s", (intent_id,))
    assert receipt["consumed_at"] is None, receipt

    # 🔁 重放一发:恢复真实现之后,同一个 intent 照样能跑完
    monkeypatch.setattr(ops, "link_execution", real_link)
    again = _execute(client, intent_id, revision, request_id="retry-" + uuid.uuid4().hex[:8])
    assert again.status_code == 200, again.text[:600]
    assert again.json().get("replayed") is False, again.json()
    assert len(_command_items(world["dsn"])) == 1


# ══════════════════════════════════════════════════════════════════════════
# 反向对照:没确认 / 价目变了 —— 都必须零冻结
# ══════════════════════════════════════════════════════════════════════════

def test_execute_without_confirmation_is_refused_with_zero_side_effect(monkeypatch, world):
    """🔴 external 档没确认不执行。**且零冻结零建单**。"""
    client = _client(monkeypatch, world)
    prepared = _prepare(client, world)
    assert prepared.status_code == 200, prepared.text[:600]
    body = prepared.json()
    before = wallet(world["dsn"])

    got = _execute(client, body["intent_id"], int(body["intent_revision"]),
                   request_id="noconfirm-" + uuid.uuid4().hex[:8])
    assert got.status_code == 409, got.text[:400]
    assert got.json()["detail"]["error_code"] == "CONFIRMATION_REQUIRED", got.json()
    assert _command_items(world["dsn"]) == []
    assert wallet(world["dsn"]) == before


def test_price_drift_between_prepare_and_execute_refuses_with_zero_freeze(monkeypatch, world):
    """🔴 prepare 与 execute 之间价目变了 → 拒绝,零冻结。

    这条打的是「冻结额 == 意图」的**另一侧**:不是"冻多了"而是"冻的不是他确认的那个数"。
    改的是账号售价(真实漂移源),不是去改断言。
    """
    client = _client(monkeypatch, world)
    intent_id, revision, prepared = _run_to_confirmed(client, world)
    assert prepared["compute_quote"]["amount"] == PUBLISH_POINTS
    before = wallet(world["dsn"])

    conn = __import__("psycopg2").connect(world["dsn"])
    conn.autocommit = True
    conn.cursor().execute("UPDATE mhz_short_video SET our_price_points = %s WHERE id = %s",
                          (PUBLISH_POINTS + 777, MEDIA_OK))
    conn.close()

    # 🔴 探针:漂移之后交给产线的那个数**必须仍是意图冻结的旧价**。
    #    没有这一条,「把 expected_total 改成执行时现算」这个变异会**存活** ——
    #    漂移会被另一把锁(冻结的价格指纹)先拦住,于是 409 照样出现,
    #    判据证明的是"漂移会被拦",不是"拦它的是我以为的那一行"(实拆演练抓到的)。
    import services.geo_douyin.publish_batch_core as core

    captured = {}
    real_core = core.materialize_publish_batch

    def _spy(cur, **kwargs):
        captured["expected_total"] = kwargs.get("expected_total_price_points")
        return real_core(cur, **kwargs)

    monkeypatch.setattr(core, "materialize_publish_batch", _spy)

    got = _execute(client, intent_id, revision, request_id="drift-" + uuid.uuid4().hex[:8])
    assert captured.get("expected_total") == PUBLISH_POINTS, (
        "交给产线的是执行时现算的新价,不是用户确认时那个旧价:" + str(captured))
    assert got.status_code == 409, got.text[:400]
    assert got.json()["detail"]["error_code"] == "PRICE_CHANGED", got.json()
    assert _command_items(world["dsn"]) == []
    assert wallet(world["dsn"]) == before


# ══════════════════════════════════════════════════════════════════════════
# ③ 资格翻转矩阵 ×(prepare / execute)
# ══════════════════════════════════════════════════════════════════════════

#: 三档 × 两阶段 = 六格。分母写成表,少一格看得见。
ELIGIBILITY_MATRIX = [
    ("有格", MEDIA_OK, True, None),
    ("无格(发不了图文)", MEDIA_NO_IMAGE, False, "这个账号发不了图文,换一个能发图文的"),
    ("过期(今日额度用完)", MEDIA_FULL, False, "这个账号今天的额度用完了,换一个或者明天再发"),
]


@pytest.mark.parametrize("label,media_id,eligible,note", ELIGIBILITY_MATRIX)
def test_eligibility_matrix_at_prepare(monkeypatch, world, label, media_id, eligible, note):
    """prepare 侧:三档都**如实说出结论**,并且都**不拦**(选渠道可以稍后做)。

    🔴 不可用的那两档必须**不报价**:报了就会有人拿那个数字做决定,
       而这一笔根本发不出去。
    """
    client = _client(monkeypatch, world)
    prepared = _prepare(client, world, media_id=media_id)
    assert prepared.status_code == 200, prepared.text[:600]     # prepare 不拦

    served = client.get("/api/xiaobang/operations/intents/"
                        + prepared.json()["intent_id"] + "/prefill")
    assert served.status_code == 200, served.text[:600]
    channel = served.json()["channel"]
    assert channel["verified"] is True, (label, channel)         # 核过了
    assert channel["eligible"] is eligible, (label, channel)
    assert channel["channel_option_id"] == "svideo:" + str(media_id), channel
    if note:
        assert channel["eligibility_note"] == note, (label, channel)
        # 🔴 不可用 ⇒ **整个 compute_quote 键都不该出现**(`_intent_dto` 只在
        #    有金额时才加这个键)。断言"amount is None"会 KeyError 而不是判红,
        #    那是判据自己写错了口径,不是产品行为。
        assert "compute_quote" not in prepared.json(), (
            label + ":不可用的账号却报了价")
    else:
        assert prepared.json()["compute_quote"]["amount"] == PUBLISH_POINTS


@pytest.mark.parametrize("label,media_id,eligible,note", ELIGIBILITY_MATRIX)
def test_eligibility_matrix_at_execute(monkeypatch, world, label, media_id, eligible, note):
    """execute 侧:有格才放行,无格/过期一律**人话拒绝 + 零冻结**。

    🔴 execute 必须**现场再核一次**:资格会在 prepare 与 execute 之间变
       (号被停用、今天额度被别的单用完)。只信 prepare 那一刻的快照 =
       拿一个几分钟前的结论去花钱。
    """
    client = _client(monkeypatch, world)
    prepared = _prepare(client, world, media_id=media_id)
    body = prepared.json()
    confirmed = _confirm(client, body["intent_id"], int(body["intent_revision"]))
    assert confirmed.status_code == 200, confirmed.text[:600]
    before = wallet(world["dsn"])

    got = _execute(client, body["intent_id"], int(confirmed.json()["intent_revision"]),
                   request_id="matrix-" + uuid.uuid4().hex[:8])
    if eligible:
        assert got.status_code == 200, got.text[:800]
        assert len(rows(world["dsn"], "SELECT id FROM mhz_publish_order_items"
                                      " WHERE media_id = %s", (media_id,))) == 1
        return

    assert got.status_code == 409, got.text[:600]
    detail = got.json()["detail"]
    # 🔴 必须是**渠道资格**那条码,不是"还没算出算力"。
    #    后者是答非所问:他选的号发不了图文,该听见的是这件事。
    assert detail["error_code"] == "PUBLISH_CHANNEL_NOT_ELIGIBLE", (label, detail)
    # 🔴 逐字钉**这一档自己的**人话。原来只断言"不是纯 ASCII" —— 太松:
    #    把 execute 侧那道渠道复核整个摘掉,产线下游的 `CapacityExceeded` 会给出
    #    一句通用的"今天发不了了",它同样不是 ASCII ⇒ 变异存活(实拆演练抓到过)。
    #    钉住逐档措辞,「发不了图文」与「今天额度满了」就再也不能互相冒充。
    assert detail["message"] == note, (label, detail)
    assert _command_items(world["dsn"]) == []
    assert wallet(world["dsn"]) == before, label


def test_an_unparseable_channel_option_is_still_not_verified(monkeypatch, world):
    """🔴 线上既有取值(`douyin_main` 这类)解析不出账号 → 仍然是「还没核」。

    这一条守的是**向后兼容**:③ 之前那三档降级文案与它们的 PW 判据
    打的就是这种输入,不能因为接了真值就把它们变成另一种表现。
    """
    client = _client(monkeypatch, world)
    prepared = client.post(PREFIX + "/prepare", json={
        "prepare_request_id": "legacy-" + uuid.uuid4().hex[:12],
        "selection": {"brand_id": world["brand_id"], "geo_post_id": world["post_id"],
                      "channel_option_id": "douyin_main"},
    })
    assert prepared.status_code == 200, prepared.text[:600]
    served = client.get("/api/xiaobang/operations/intents/"
                        + prepared.json()["intent_id"] + "/prefill")
    channel = served.json()["channel"]
    assert channel["verified"] is False, channel
    assert "eligible" not in channel, channel          # 没核过就不下结论
    assert channel["channel_option_id"] == "douyin_main", channel

    # 执行段**必拦**:到了真要发的那一刻还解析不出账号,不能猜一个。
    body = prepared.json()
    confirmed = _confirm(client, body["intent_id"], int(body["intent_revision"]))
    got = _execute(client, body["intent_id"], int(confirmed.json()["intent_revision"]),
                   request_id="legacy-exec-" + uuid.uuid4().hex[:8])
    assert got.status_code == 409, got.text[:400]
    assert _command_items(world["dsn"]) == []


def test_the_amount_handed_to_the_production_line_is_the_intent_quote(monkeypatch, world):
    """🔴🔴 直接钉住那一行:交给产线的 `expected_total_price_points`
    **就是 intent 冻结的那个报价**,不是执行时现算的数。

    ## 为什么要有这一条(实拆演练的产物)

    把 `expected_total_price_points=int(expected_points)` 改成执行时现算的
    `channel.price_points`,上面那批判据**全绿** —— 因为价格漂移会先被
    另一把锁(冻结时的价格指纹 `PriceFingerprintMismatch`)拦住。
    也就是说那批判据证明的是"漂移会被拦",不是"拦它的是我以为的那一行"。

    判据要**驱动**被测那一行,不能靠别的锁替它挡。所以这里在产线入口设探针,
    把真正传进去的数与库里 intent 的 `compute_quote_amount` 直接比。
    """
    import services.geo_douyin.publish_batch_core as core

    captured = {}
    real_core = core.materialize_publish_batch

    def _spy(cur, **kwargs):
        captured["expected_total"] = kwargs.get("expected_total_price_points")
        return real_core(cur, **kwargs)

    monkeypatch.setattr(core, "materialize_publish_batch", _spy)

    client = _client(monkeypatch, world)
    intent_id, revision, _ = _run_to_confirmed(client, world)
    got = _execute(client, intent_id, revision, request_id="spy-" + uuid.uuid4().hex[:10])
    assert got.status_code == 200, got.text[:600]

    assert "expected_total" in captured, "探针没被走到 —— 这条判据打空了"
    intent = one(world["dsn"],
                 "SELECT compute_quote_amount FROM xiaobang_operation_intents"
                 "  WHERE intent_id = %s", (intent_id,))
    assert intent["compute_quote_amount"] is not None, "意图没报价 —— 判据没有分母"
    assert int(captured["expected_total"]) == int(intent["compute_quote_amount"]), (
        "交给产线的金额不是意图冻结的那个报价:"
        + str(captured["expected_total"]) + " vs " + str(intent["compute_quote_amount"]))
