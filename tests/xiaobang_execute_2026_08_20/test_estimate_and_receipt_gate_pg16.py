"""窗G 判据 —— 小榜「先示预估、确认后开始」+ POR-13 回执守卫(Owner 2026-08-24)。

Owner 原话:**小榜可真执行、必收费,但执行前必须示算力预估、用户确认后才开始。**

🔴 为什么这批判据长在 ``tests/xiaobang_execute_2026_08_20/`` 而不是自己开一个目录
==============================================================================
因为它们要打的是**现役那条真链**:真 HTTP → 真 handler → 真 PG16 → 真回执 →
真 claim → 真 freeze → 真订单行。这套夹具(``conftest.py`` 的 ``live_db`` / ``world``)
已经把这条链跑通并被上一班验过。另起一个目录就意味着再搭半套夹具 ——
而本仓所有的假绿几乎都出在"第二套半成品夹具"上。

判据与工单四条的对应
--------------------
| 工单判据                | 本文件                                                     |
|------------------------|-----------------------------------------------------------|
| 估价零副作用锁          | ``test_estimate_stage_has_zero_funding_side_effects``      |
| 同收据重放幂等          | ``test_same_receipt_replay_is_one_charge_not_two``         |
| 无收据执行必拒          | ``test_execute_without_a_receipt_is_refused``              |
| (KB 零社媒负向锁)       | 在 ``tests/defensive_geo_w4_2026_08_22/test_wp8_xiaobang.py``|

🔴 「outbox 增量 0」这一格的分母,我按**小榜这条链真的会写的表**取
--------------------------------------------------------------
工单写的是「point_freezes/outbox 增量 0」。census 实测:本仓**没有**一张叫
``outbox`` 的表;名字含 outbox 的有 ``notification_outbox`` /
``defgeo_activation_outbox``(零资金列)/ ``defgeo_publish_outbox``,
三者语义各不相同,而**小榜 execute 一张都不写**。
小榜这条链真正的资金落点是 ``point_freezes`` + ``point_transactions`` +
``user_wallets.frozen_points`` + ``mhz_publish_order*`` + ``publish_idempotency_keys``。
所以这里按后者取分母,并在交付单里把这一处口径差异显式上报 ——
照着工单字面去数一张这条链根本不写的表,会得到一个**恒 0 的假绿**。
"""
from __future__ import annotations

import uuid

import pytest

from tests.xiaobang_execute_2026_08_20.conftest import (
    MEDIA_FULL,
    MEDIA_NO_IMAGE,
    MEDIA_OK,
    OWNER_UID,
    PUBLISH_POINTS,
    conn_for,
    one,
    rows,
    wallet,
)
from services.defensive_geo.xiaobang import drift_notice as DN
from tests.xiaobang_execute_2026_08_20.test_five_phase_execute_pg16 import (
    PREFIX,
    _client,
    _confirm,
    _execute,
    _prepare,
)



# ══════════════════════════════════════════════════════════════════════════
# 分母 + 活性自证
# ══════════════════════════════════════════════════════════════════════════
#: 小榜 execute 真正会写的资金/命令面。**逐张点名**,不写"outbox"这种含糊词。
_COUNTED_TABLES = (
    "point_freezes",
    "point_transactions",
    "mhz_publish_orders",
    "mhz_publish_order_items",
    "publish_idempotency_keys",
    "xiaobang_confirmation_receipts",
)


def _counts(dsn) -> dict[str, int]:
    """副作用分母。**短连接、立刻关** —— 不把事务跨到 HTTP 调用上。"""
    conn = conn_for(dsn)
    try:
        cur = conn.cursor()
        out = {}
        for t in _COUNTED_TABLES:
            cur.execute("SELECT COUNT(*) AS n FROM " + t)
            out[t] = int(cur.fetchone()["n"])
        cur.execute("SELECT frozen_points FROM user_wallets WHERE user_id = %s", (OWNER_UID,))
        out["frozen_points"] = int(cur.fetchone()["frozen_points"])
        return out
    finally:
        conn.close()


def _delta(before, after) -> dict[str, int]:
    return {k: after[k] - before[k] for k in before}


def test_counting_surfaces_exist(world):
    """🔴 判据可用性关:被数的每一张表都必须真的在。

    表不在时 :func:`_counts` 会抛;但如果哪天有人给它加了 try/except,
    「零副作用」会静默退化成「零查询」—— 那时所有 delta 都是 0,全绿。
    (本仓 2026-08-21 记过这个形态。)
    """
    conn = conn_for(world["dsn"])
    try:
        cur = conn.cursor()
        for t in _COUNTED_TABLES + ("user_wallets",):
            cur.execute("SELECT to_regclass(%s) AS r", ("public." + t,))
            assert cur.fetchone()["r"] is not None, (
                "计数表 " + t + " 不存在 —— 零副作用判据会恒绿")
    finally:
        conn.close()


def test_the_price_is_non_zero_so_zero_freeze_is_discriminating(world):
    """🔴 判别力前置条件:价必须非 0。

    ``freeze_points`` 在价为 0 时**不产生** ``point_freezes`` 行 —— 那会让
    「冻结增量为 0」既像对也像错,零判别力(本仓记过)。
    """
    assert PUBLISH_POINTS > 0, PUBLISH_POINTS
    got = one(world["dsn"],
              "SELECT our_price_points FROM mhz_short_video WHERE id = %s", (MEDIA_OK,))
    assert int(got["our_price_points"]) == PUBLISH_POINTS, got


# ══════════════════════════════════════════════════════════════════════════
# ① 估价段:免费、零资金副作用
# ══════════════════════════════════════════════════════════════════════════
def test_estimate_stage_has_zero_funding_side_effects(monkeypatch, world):
    """🔴 工单判据①:**估价段零副作用。**

    估价段 = 现役 ``prepare``。它必须报得出价(那是它的全部意义),
    同时**一分钱不动、一条命令不建、一张回执不签**。

    把 ``prepare`` 里任何一处改成"顺手冻一下",这条当场红。
    """
    client = _client(monkeypatch, world)
    before = _counts(world["dsn"])

    prepared = _prepare(client, world, media_id=MEDIA_OK)
    assert prepared.status_code == 200, prepared.text[:600]
    body = prepared.json()
    # 🔴 先证明这一跳**真的走到了报价**:没报出价的话,下面的"零冻结"
    #    只是"什么都没发生",不是"估价了但没花钱" —— 零判别力。
    assert body["compute_quote"]["amount"] == PUBLISH_POINTS, body.get("compute_quote")

    d = _delta(before, _counts(world["dsn"]))
    assert d["point_freezes"] == 0, "估价段冻了钱:" + str(d)
    assert d["point_transactions"] == 0, "估价段动了流水:" + str(d)
    assert d["frozen_points"] == 0, "估价段动了钱包冻结额:" + str(d)
    assert d["mhz_publish_orders"] == 0, "估价段建了投放单:" + str(d)
    assert d["mhz_publish_order_items"] == 0, "估价段建了投放项:" + str(d)
    assert d["publish_idempotency_keys"] == 0, "估价段占了幂等键:" + str(d)
    assert d["xiaobang_confirmation_receipts"] == 0, (
        "估价段签了确认回执 —— 估价不需要回执,签了就等于替用户点了确认:" + str(d))


def test_the_estimate_sentence_owner_asked_for_is_actually_on_the_wire(monkeypatch, world):
    """🔴 Owner 2026-08-24 逐字句式:「本次预计消耗你的算力 X,确认后开始」。

    ``compute_quote`` 是机读的,她读不懂;要上屏的是这一句。
    判据打**响应体**,不是打 copy registry —— 打 registry 只能证明"文案写过",
    证明不了"它进了这个端点的出参"。

    🔴 [对抗复审订正] 原措辞写的是"真的被下发到**那个页面上**" —— **越界了**:
       本条只证明它进了响应体,**没有**证明前端渲染了它。实测:全仓前端零消费方,
       且确认页走的 prefill 端点根本不投影 compute_estimate。前端接入属后续。
    """
    client = _client(monkeypatch, world)
    prepared = _prepare(client, world, media_id=MEDIA_OK)
    est = prepared.json()["compute_estimate"]

    assert est["confirmable"] is True, est
    assert est["amount"] == PUBLISH_POINTS, est
    assert est["unit"] == "算力", est
    assert est["headline"] == (
        "本次预计消耗你的算力 " + str(PUBLISH_POINTS) + "，确认后开始"), est


@pytest.mark.parametrize("media_id", [MEDIA_NO_IMAGE, MEDIA_FULL])
def test_every_unquoted_estimate_says_the_money_is_untouched(monkeypatch, world, media_id):
    """🔴 Owner:**失败态把钱说死。**

    她看到"算不出来"的第一反应是"那我是不是已经被扣了"。
    不回答这个问题的文案等于没写(§0.5.5 U-2「没有扣除任何算力」这半句永远显式说)。
    另按 §0.5.6 铁律:任何阻塞必须自带解决方案 ⇒ 必须有下一步。
    """
    client = _client(monkeypatch, world)
    prepared = _prepare(client, world, media_id=media_id)
    est = prepared.json()["compute_estimate"]

    assert est["confirmable"] is False, est
    assert "amount" not in est, "报不出价却给了数字 —— 她会拿它做决定:" + str(est)
    assert "没有扣除任何算力" in est["headline"], est
    assert est["next_action"].strip(), "没有下一步 = 死路(§0.5.6):" + str(est)


# ══════════════════════════════════════════════════════════════════════════
# ② 没有预估不许确认 —— 但不许抢领域的话
# ══════════════════════════════════════════════════════════════════════════
def test_confirm_is_refused_when_nobody_can_explain_the_missing_estimate(monkeypatch, world):
    """🔴 Owner 铁律的承重那一格:**没人解释得了的「估不出价」,不许她按确认。**

    🔴 为什么这条用 monkeypatch 把报价打空,而不是种一个"没有价的账号"
    ----------------------------------------------------------------
    实跑发现:本仓**造不出**「资格核得过但没有价」的账号 ——
    ``xiaobang_channel_eligibility.resolve_channel_option`` 在 ``our_price_points``
    为空时回落 markup 定价,两列都清空时最后那个 ``int(... or 0)`` 还会兜成 **0**。
    也就是说经由发布链,``compute_quote_amount`` 对一个可用账号**永远不是 None**。

    ⇒ confirm 里那道门在**今天的发布链上不可达**;它守的是
      「第二个 execute adapter 接进来、而它的领域给不出价」那一天。
      所以这一条把**入口条件**(报价为空)注进去,驱动 handler 里那一行真的执行 ——
      注的是输入,不是那一行的输出(判据不许自己构造被测行的产物)。

    另外两个必须一起断的:回执**没签**(回执就是授权)、钱**没动**。
    """
    import api.xiaobang_operations_api as ops

    # 报价打空,但**渠道仍然可用** ⇒ prepare 冻结的 estimate_domain_blocked=False
    # ⇒ 没有任何下游能解释,门必须在她按确认之前拦住。
    monkeypatch.setattr(ops, "_channel_compute_quote",
                        lambda quote, quote_meta, channel: (None, {"quote_state": "unavailable"}))
    monkeypatch.setattr(ops, "_resolve_compute_quote",
                        lambda cursor, contract: (None, {"quote_state": "unavailable"}))

    client = _client(monkeypatch, world)
    prepared = _prepare(client, world, media_id=MEDIA_OK)
    assert prepared.status_code == 200, prepared.text[:600]
    body = prepared.json()
    assert "compute_quote" not in body, "没有价却报了价:" + str(body.get("compute_quote"))
    assert body["compute_estimate"]["confirmable"] is False, body["compute_estimate"]

    before = _counts(world["dsn"])
    got = _confirm(client, body["intent_id"], int(body["intent_revision"]))

    assert got.status_code == 409, got.text[:600]
    detail = got.json()["detail"]
    assert detail["error_code"] == "COMPUTE_QUOTE_MISSING", detail
    assert "没有扣除任何算力" in detail["message"], detail
    assert detail["next_action"].strip(), detail

    d = _delta(before, _counts(world["dsn"]))
    assert d["xiaobang_confirmation_receipts"] == 0, (
        "拒绝了却把回执签了 —— 回执就是授权,签了就等于她确认过:" + str(d))
    assert d["point_freezes"] == 0 and d["frozen_points"] == 0, str(d)


@pytest.mark.parametrize("media_id,expected_message", [
    (MEDIA_NO_IMAGE, "这个账号发不了图文,换一个能发图文的"),
    (MEDIA_FULL, "这个账号今天的额度用完了,换一个或者明天再发"),
])
def test_confirm_does_not_steal_the_domains_answer(monkeypatch, world, media_id,
                                                   expected_message):
    """🔴 反向:**领域能说得更准的时候,这道门必须闭嘴。**

    本仓已有签发过的裁定:「必须是渠道资格那条码,不是"还没算出算力"。
    后者是答非所问:他选的号发不了图文,该听见的是这件事。」

    所以这两档必须仍然 confirm 得过,并由执行段给出**这一档自己的**那句人话。
    把 confirm 那道门改成"凡没数字就拦",这条当场红 —— 它守的正是
    "我修一个洞的时候没有顺手把另一件对的事弄坏"。
    """
    client = _client(monkeypatch, world)
    prepared = _prepare(client, world, media_id=media_id)
    body = prepared.json()
    confirmed = _confirm(client, body["intent_id"], int(body["intent_revision"]))
    assert confirmed.status_code == 200, confirmed.text[:600]

    got = _execute(client, body["intent_id"], int(confirmed.json()["intent_revision"]),
                   request_id="steal-" + uuid.uuid4().hex[:8])
    assert got.status_code == 409, got.text[:600]
    detail = got.json()["detail"]
    assert detail["error_code"] == "PUBLISH_CHANNEL_NOT_ELIGIBLE", detail
    assert detail["message"] == expected_message, detail


# ══════════════════════════════════════════════════════════════════════════
# ③ 无回执不执行 / 同回执重放幂等(POR-13)
# ══════════════════════════════════════════════════════════════════════════
def test_execute_without_a_receipt_is_refused(monkeypatch, world):
    """🔴 工单判据③:**没有确认回执,不许执行。**

    跳过 confirm 直接 execute。POR-13 末句:聊天里的「好的」不是授权 ——
    只有页面上真点出来的那张回执才是。拒绝之外还必须**零副作用**:
    只验状态码会完全漏掉"返回 409 但钱已经冻了"。
    """
    client = _client(monkeypatch, world)
    prepared = _prepare(client, world, media_id=MEDIA_OK)
    body = prepared.json()

    before = _counts(world["dsn"])
    got = _execute(client, body["intent_id"], int(body["intent_revision"]),
                   request_id="noreceipt-" + uuid.uuid4().hex[:8])

    assert got.status_code == 409, got.text[:600]
    assert got.json()["detail"]["error_code"] == "CONFIRMATION_REQUIRED", got.json()

    d = _delta(before, _counts(world["dsn"]))
    assert d["point_freezes"] == 0 and d["frozen_points"] == 0, "没确认却冻了钱:" + str(d)
    assert d["mhz_publish_order_items"] == 0, "没确认却建了投放项:" + str(d)
    assert d["publish_idempotency_keys"] == 0, str(d)


def test_same_receipt_replay_is_one_charge_not_two(monkeypatch, world):
    """🔴 工单判据②:**同回执重放 = 同一个答案,不是第二次扣费。**

    幂等键 = 确认回执(``xiaobang_publish_execute`` 逐字)。所以第二发必须:
      · 回放**同一个** command_id;
      · ``replayed`` 为真;
      · 钱**一分都不再动**(判据打冻结终态,不打返回值)。
    """
    client = _client(monkeypatch, world)
    prepared = _prepare(client, world, media_id=MEDIA_OK)
    body = prepared.json()
    confirmed = _confirm(client, body["intent_id"], int(body["intent_revision"]))
    assert confirmed.status_code == 200, confirmed.text[:600]
    revision = int(confirmed.json()["intent_revision"])

    before = wallet(world["dsn"])
    request_id = "replay-" + uuid.uuid4().hex[:8]
    first = _execute(client, body["intent_id"], revision, request_id=request_id)
    assert first.status_code == 200, first.text[:800]
    after_first = wallet(world["dsn"])
    assert int(after_first["frozen_points"]) - int(before["frozen_points"]) == PUBLISH_POINTS

    mid = _counts(world["dsn"])
    second = _execute(client, body["intent_id"], int(first.json()["intent_revision"]),
                      request_id=request_id)
    assert second.status_code == 200, second.text[:800]
    assert second.json()["replayed"] is True, second.json()

    linked = rows(world["dsn"],
                  "SELECT execution_id FROM xiaobang_operation_intents WHERE intent_id = %s",
                  (body["intent_id"],))
    assert len(linked) == 1 and linked[0]["execution_id"], linked
    assert len(rows(world["dsn"], "SELECT request_id FROM publish_idempotency_keys")) == 1, (
        "重放占了第二个幂等键 —— 那不是幂等,是第二条腿")
    d = _delta(mid, _counts(world["dsn"]))
    assert d["point_freezes"] == 0, "重放又冻了一笔:" + str(d)
    assert d["frozen_points"] == 0, "重放又冻了一笔:" + str(d)
    assert d["mhz_publish_order_items"] == 0, "重放又建了一项:" + str(d)
    assert len(rows(world["dsn"], "SELECT id FROM mhz_publish_order_items"
                                  " WHERE media_id = %s", (MEDIA_OK,))) == 1


def test_a_receipt_bound_to_an_older_revision_is_refused(monkeypatch, world):
    """🔴 POR-13 的 ``command`` 维在 execute 侧真的有区分力。

    回执与 ``(intent_id, intent_revision)`` 一一对应。把 intent 推到下一版之后
    再执行,守卫必须认出"你确认的是上一版"。

    🔴 这条同时是**接线锁**:它打的是
       ``services.defensive_geo.xiaobang.receipt_guard.assert_receipt_usable``
       —— 在窗G 之前那个模块是只有判据在 import 的死函数。
       把 ``_assert_confirmation_present`` 里那次委派拆掉,这条当场红。
    """
    client = _client(monkeypatch, world)
    prepared = _prepare(client, world, media_id=MEDIA_OK)
    body = prepared.json()
    confirmed = _confirm(client, body["intent_id"], int(body["intent_revision"]))
    assert confirmed.status_code == 200, confirmed.text[:600]
    revision = int(confirmed.json()["intent_revision"])

    # 把回执留在旧版上:直接把 intent 推一版(等价于内容/权限在确认后又变过)。
    conn = conn_for(world["dsn"])
    try:
        conn.cursor().execute(
            "UPDATE xiaobang_operation_intents SET intent_revision = intent_revision + 1"
            " WHERE intent_id = %s", (body["intent_id"],))
    finally:
        conn.close()

    before = _counts(world["dsn"])
    got = _execute(client, body["intent_id"], revision + 1,
                   request_id="stale-" + uuid.uuid4().hex[:8])

    assert got.status_code == 409, got.text[:600]
    detail = got.json()["detail"]
    assert detail["error_code"] == "CONFIRMATION_REQUIRED", detail
    assert "上一版" in detail["message"], (
        "拒是拒了,但没说清「你确认的是上一版」—— 守卫那一支没被走到:" + str(detail))

    d = _delta(before, _counts(world["dsn"]))
    assert d["point_freezes"] == 0 and d["frozen_points"] == 0, str(d)


# ══════════════════════════════════════════════════════════════════════════
# ④ POR-13 hash 三维:与**当前**事实比,不再自比较(段二② · Owner 2026-08-24 批)
# ══════════════════════════════════════════════════════════════════════════
def _set_media(dsn, media_id, **cols) -> None:
    conn = conn_for(dsn)
    try:
        sets = ", ".join(k + " = %s" for k in cols)
        conn.cursor().execute(
            "UPDATE mhz_short_video SET " + sets + " WHERE id = %s",
            tuple(cols.values()) + (media_id,))
    finally:
        conn.close()


def test_drift_reason_axis_is_fully_covered_by_copy(monkeypatch, world):
    """🔴 分母锁:``drift_reason`` **可能返回的每一个值**都必须有文案桶。

    分母从 ``services/xiaobang_intent.py`` 的**源码**机械枚举 ``return "..."``
    字面量,不手抄 —— 手抄漏掉的那一项不会让任何判据变红,而线上会把
    一个裸枚举送上屏(§0.5.5 U-1 验收红)。
    """
    import ast as _ast
    import inspect

    from services import xiaobang_intent as _intent

    src = inspect.getsource(_intent.drift_reason)
    tree = _ast.parse(src.lstrip())
    # [对抗复审订正] 第一版只收 Return+Constant,而 drift_reason 里 6 个 actor 维
    # 走的是 `return column`(ast.Name)—— 那一版分母只有 3/9,删掉任意一个
    # authority 维的文案都不会红,锁是**恒绿**的。改成收函数体内全部字符串
    # 字面量:正好是 3 个 return 字面量 + for 循环元组里的 6 个列名。
    literals = {n.value for n in _ast.walk(tree)
                if isinstance(n, _ast.Constant) and isinstance(n.value, str)}
    literals = {v for v in literals if v and v.isidentifier()}
    assert len(literals) >= 9, (
        "分母只有 %d 个(应 >= 9:3 个 return 字面量 + 6 个 actor 列名)—— "
        "枚举本身有洞,这条判据接近恒真:%s" % (len(literals), sorted(literals)))
    missing = sorted(literals - set(DN.DRIFT_REASONS))
    assert not missing, (
        "drift_reason 会返回这些值,但 drift_notice 没有登记文案桶:" + str(missing))
    for reason in sorted(literals):
        notice = DN.drift_notice(reason)
        assert notice["message"].strip() and notice["next_action"].strip(), (reason, notice)


def test_object_drift_between_prepare_and_confirm_is_refused_in_plain_words(monkeypatch, world):
    """🔴 **真反例**:prepare 之后对象本身变了 → 确认被拦 + 说的是「内容变了」。

    在窗G 段二之前,这一格**恒绿地放行** —— 两个调用点都把 ``row`` 自己的列
    传回给 ``drift_reason``,三个 hash 分支结构性恒 False。
    把 ``object_manifest_hash`` 那一维改回自比较,这条当场红。

    🔴 漂移源为什么用 ``data_updated_at`` 而不是"把账号停用"(实跑订正)
    -----------------------------------------------------------------
    实测:把 ``can_tuwen`` 改 0 只让**报价**消失,``object_manifest`` 逐字未变 ——
    它压根没覆盖资格位(``_object_manifest`` 自己的 docstring 就写着
    「渠道资格、频控、库存版本要等发布域 adapter 接入后补……manifest 少一项,
    漂移就少一条判据」)。拿那个当"对象漂移"会打成价格漂移,判据就说了假话。

    ``data_updated_at`` 是 manifest **真正覆盖**的那一项(对象数据水位),
    它变了就是"她刚才看的那份内容已经不是现在这份"。这里改的是**产线的输入**
    (授权上下文吐出来的事实),不是断言。
    """
    import api.xiaobang_operations_api as ops

    client = _client(monkeypatch, world)
    prepared = _prepare(client, world, media_id=MEDIA_OK)
    assert prepared.status_code == 200, prepared.text[:600]
    body = prepared.json()

    class _MovedCtx:
        owner_user_id = OWNER_UID

        def public_context(self):
            return {"brand_id": world["brand_id"], "brand_name": "甲品牌",
                    "geo_post_id": world["post_id"],
                    # 对象数据水位往前走了 —— 她刚才看的那份已经不是现在这份
                    "data_updated_at": "2026-08-24T09:30:00"}

    monkeypatch.setattr(ops, "_authorized_context",
                        lambda request, refs, page="": _MovedCtx())

    before = _counts(world["dsn"])
    got = _confirm(client, body["intent_id"], int(body["intent_revision"]))

    assert got.status_code == 409, got.text[:600]
    detail = got.json()["detail"]
    assert detail["error_code"] == "OBJECT_OR_AUTHORITY_DRIFT", detail
    assert detail["reason_code"] == "object_manifest_hash", (
        "漂移原因不是对象 —— 这一维没被真的比对:" + str(detail))
    assert detail["drift_kind"] == "content_changed", detail
    assert "没有扣除任何算力" in detail["message"], detail
    assert detail["next_action"] == "看看有什么变化", detail

    d = _delta(before, _counts(world["dsn"]))
    assert d["xiaobang_confirmation_receipts"] == 0, (
        "拦下了却把回执签了 —— 回执就是授权:" + str(d))
    assert d["point_freezes"] == 0 and d["frozen_points"] == 0, str(d)


def test_price_drift_between_prepare_and_confirm_says_it_is_the_price(monkeypatch, world):
    """🔴 **区分两种下一步**(Owner 点名):内容没动、只有价变了 ⇒ 说「价格变了」。

    压成一句「有变化」的话,她只能把两种情况都当成"又出问题了"重走全流程。
    """
    client = _client(monkeypatch, world)
    prepared = _prepare(client, world, media_id=MEDIA_OK)
    body = prepared.json()
    assert body["compute_quote"]["amount"] == PUBLISH_POINTS, body.get("compute_quote")

    # 只动价,不动资格 ⇒ manifest 不变(价不在 manifest 里),只有报价 hash 变
    _set_media(world["dsn"], MEDIA_OK, our_price_points=PUBLISH_POINTS + 777)

    before = _counts(world["dsn"])
    got = _confirm(client, body["intent_id"], int(body["intent_revision"]))

    assert got.status_code == 409, got.text[:600]
    detail = got.json()["detail"]
    assert detail["reason_code"] == "compute_quote_hash", (
        "漂移原因不是价格 —— 说明这一维没被真的比对:" + str(detail))
    assert detail["drift_kind"] == "price_changed", detail
    assert detail["next_action"] == "看新的算力预估", detail
    assert "没有扣除任何算力" in detail["message"], detail

    d = _delta(before, _counts(world["dsn"]))
    assert d["xiaobang_confirmation_receipts"] == 0, str(d)
    assert d["point_freezes"] == 0 and d["frozen_points"] == 0, str(d)


def test_after_re_preparing_she_can_confirm_again(monkeypatch, world):
    """🔴 铁律(§0.5.6):**拦下来必须有活路。**

    价格变了之后重新准备一次 → 拿到**新的**预估 → 确认必须通过。
    只验"会被拦"而不验"拦完还能继续",等于把一条死路当成修好了。
    """
    client = _client(monkeypatch, world)
    first = _prepare(client, world, media_id=MEDIA_OK).json()
    _set_media(world["dsn"], MEDIA_OK, our_price_points=PUBLISH_POINTS + 777)

    blocked = _confirm(client, first["intent_id"], int(first["intent_revision"]))
    assert blocked.status_code == 409, blocked.text[:400]

    # 重新准备(新的 prepare_request_id ⇒ 新意图,按当前事实重新估价)
    again = _prepare(client, world, media_id=MEDIA_OK)
    assert again.status_code == 200, again.text[:600]
    body = again.json()
    assert body["replayed"] is False, "命中了旧幂等行 —— 这条判据没走到当前 producer"
    assert body["compute_quote"]["amount"] == PUBLISH_POINTS + 777, body["compute_quote"]
    assert body["compute_estimate"]["headline"] == (
        "本次预计消耗你的算力 " + str(PUBLISH_POINTS + 777) + "，确认后开始")

    ok = _confirm(client, body["intent_id"], int(body["intent_revision"]))
    assert ok.status_code == 200, ok.text[:600]
    assert ok.json()["confirmation_receipt_id"], ok.json()


def test_a_clean_confirm_is_not_falsely_flagged_as_drift(monkeypatch, world):
    """🔴 反向:**什么都没变的时候,重算必须给出一模一样的 hash。**

    没有这一条,上面三条"会被拦"的判据在一个恒判漂移的实现下也全绿 ——
    而那种实现会让**每一次正常确认**都失败。
    """
    client = _client(monkeypatch, world)
    body = _prepare(client, world, media_id=MEDIA_OK).json()
    got = _confirm(client, body["intent_id"], int(body["intent_revision"]))
    assert got.status_code == 200, got.text[:600]


def test_a_tampered_frozen_payload_no_longer_matches_its_own_hash(monkeypatch, world):
    """🔴 ``payload_hash`` 这一维:**冻结的入参必须对得上它自己的指纹。**

    这一维与另外两维不同 —— 入参在 prepare 之后就不再由客户端重发,
    所以它不会因为"外面的世界变了"而漂移。它守的是另一件事:
    **有人动了服务端冻结的那份入参**(直接改库 / 改缓存 / 未来某条代码路径
    顺手改了 preview),而 ``payload_hash`` 还停在旧值。

    在窗G 段二之前这一格是自比较(``row['payload_hash']`` 传回给自己),
    也就是说篡改**永远测不出来**。这条判据直接改库制造篡改,
    把 ``payload_hash`` 那一维改回自比较 ⇒ 当场红。
    """
    import json

    client = _client(monkeypatch, world)
    prepared = _prepare(client, world, media_id=MEDIA_OK)
    assert prepared.status_code == 200, prepared.text[:600]
    body = prepared.json()

    conn = conn_for(world["dsn"])
    try:
        cur = conn.cursor()
        cur.execute("SELECT preview FROM xiaobang_operation_intents WHERE intent_id = %s",
                    (body["intent_id"],))
        preview = dict(cur.fetchone()["preview"] or {})
        canonical = dict(preview.get("canonical_input") or {})
        assert canonical, "prepare 没有冻结 canonical_input —— 这条判据没有分母"
        selection = dict(canonical.get("selection") or {})
        assert selection, "冻结入参里没有 selection —— 分母为空"
        # 篡改:把冻结入参改成指向另一个对象,但**不动** payload_hash 那一列
        selection["brand_id"] = int(selection.get("brand_id") or 0) + 100000
        canonical["selection"] = selection
        preview["canonical_input"] = canonical
        cur.execute("UPDATE xiaobang_operation_intents SET preview = %s WHERE intent_id = %s",
                    (json.dumps(preview), body["intent_id"]))
    finally:
        conn.close()

    before = _counts(world["dsn"])
    got = _confirm(client, body["intent_id"], int(body["intent_revision"]))

    assert got.status_code == 409, got.text[:600]
    detail = got.json()["detail"]
    assert detail["reason_code"] == "payload_hash", (
        "篡改了冻结入参却没被 payload_hash 那一维抓到:" + str(detail))
    assert detail["drift_kind"] == "content_changed", detail
    assert "没有扣除任何算力" in detail["message"], detail

    d = _delta(before, _counts(world["dsn"]))
    assert d["xiaobang_confirmation_receipts"] == 0, str(d)
    assert d["point_freezes"] == 0 and d["frozen_points"] == 0, str(d)


# ══════════════════════════════════════════════════════════════════════════
# ⑤ 计费代号统一(段二③ · Owner 2026-08-24 批 · 方向 = media_proxy_publish 唯一码)
# ══════════════════════════════════════════════════════════════════════════
#: 收敛作用域 = **小榜/防御 GEO 发布链**这几个文件。负向锁只在这个范围内成立 ——
#: 全仓零残留做不到,也**不该**做:见下面登记在案的合法保留点。
_CONVERGED_SCOPE: tuple[str, ...] = (
    "services/gap_operation_map.py",
    "services/defensive_geo/publish/publish_funding.py",
    "api/geo_image_note_api.py",
    "services/xiaobang_publish_execute.py",
    "services/geo_douyin/publish_coordinator.py",
    # 🔴 [Review 2026-08-24 裁定条件① · 作用域扩大一格] 组织路由声明**收进来了**。
    #    先前把它列在"合法保留点"里,理由是「改它会改变组织预算归属」——
    #    Review census 证伪了那个理由:这一格现无运行期消费方(详见
    #    MemberRoutePolicy.billing_feature 的字段注释与下面那条一致性锁)。
    #    理由被证伪 ⇒ 保留点不成立 ⇒ 它属于收敛作用域。
    "services/organization_route_contract.py",
)

#: 🔴 **登记在案的合法保留点** —— 每条都写清楚"为什么删掉反而更糟"。
#:    (与 kb_entries.INCIDENTAL_SOCIAL_MENTIONS 同一个套路:
#:     结构性负向锁 + 显式登记,而不是把真话删成假话。)
_LEGITIMATE_OLD_CODE_SITES: tuple[dict, str] = (
    {"path": "db/wallet_db.py",
     "why": "PRICING_DATA 里两个 SKU 都在,且都是现役目录条目。它还是七保护文件之一,本包零 diff。"},
    # ⚠️ services/organization_route_contract.py 曾登记在这里,理由是
    #    「改它会改变组织预算归属」。那条理由被 Review 2026-08-24 的 census 证伪
    #    (该字段无运行期消费方),已移进 _CONVERGED_SCOPE。
    #    留下这段是因为"登记表悄悄变短"比"登记表写错"更难被发现。
    {"path": "services/organization_contract.py",
     "why": "两个 SKU 各自映射到 publish.execute 的能力表,删一个会让那条 SKU 失去能力映射。"},
    {"path": "tools/transparent_pricing.py",
     "why": "那是**人民币成本**配置键(¥/篇),与算力消耗目录是两个 SSOT。"
            "08_billing.md §3.2 明令两者不得混称为同一个「价格」。"},
)


def _code_lines(rel: str) -> list[tuple[int, str]]:
    """去掉整行注释后的代码行。判**代码**不判注释 —— 本包的说明性注释里
    必然会提到旧码(讲清楚为什么改),把注释也算进去就变成了「引用裁决原文
    会让裸串结构锚判红」那个形态(本仓记过)。"""
    import io as _io
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[2]
    out = []
    with _io.open(root / rel, encoding="utf-8", newline="") as f:
        for i, line in enumerate(f, 1):
            if line.lstrip().startswith("#"):
                continue
            out.append((i, line))
    return out


def test_the_converged_scope_has_no_old_billing_code_left():
    """🔴 **负向锁**:收敛作用域内不许再出现旧码。

    分母是 :data:`_CONVERGED_SCOPE`(逐文件点名),**不是全仓** ——
    全仓零残留做不到:``db/wallet_db.py`` 是七保护文件、组织路由声明的是另一条链、
    透明报价那处根本是人民币口径。每一条合法保留都登记在
    :data:`_LEGITIMATE_OLD_CODE_SITES` 并写明"删掉为什么更糟"。
    """
    import re as _re
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[2]
    pattern = _re.compile(r"media_publish(?![_a-zA-Z0-9])")

    for rel in _CONVERGED_SCOPE:
        assert (root / rel).is_file(), "作用域里的文件不存在 —— 负向锁分母是空的:" + rel
        hits = [(n, ln.strip()[:100]) for n, ln in _code_lines(rel) if pattern.search(ln)]
        assert not hits, rel + " 的代码里还留着旧计费代号:" + str(hits)


def test_the_negative_lock_would_actually_fire():
    """🔴 活性自证:上面那把锁**认得出**旧码。

    没有这一条,把正则写错(比如漏掉分组)会让负向锁恒绿,
    而恒绿的负向锁和没有锁是一回事。
    """
    import re as _re

    pattern = _re.compile(r"media_publish(?![_a-zA-Z0-9])")
    assert pattern.search('billing_feature="media_publish",'), "锁认不出旧码 —— 它是恒绿的"
    assert not pattern.search('billing_feature="media_proxy_publish",'), (
        "锁把新码也判红了 —— 那会逼下一个人去改对的东西")
    assert not pattern.search("from services.media_publish_success import x"), (
        "锁误伤了 media_publish_success 这个模块名")


def test_every_legitimate_old_code_site_is_registered_and_explained():
    """登记在案的保留点必须**真的还在**,且每条都写了理由。

    census 不许比现实活得久:文件没了还留在表里,说明这张表在骗人。
    """
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[2]
    assert _LEGITIMATE_OLD_CODE_SITES, "登记表为空 —— 那这条判据没有分母"
    for item in _LEGITIMATE_OLD_CODE_SITES:
        path = root / item["path"]
        assert path.is_file(), "登记表里的文件已不存在:" + item["path"]
        assert len(item["why"]) > 20, "没写清为什么保留:" + item["path"]


def _charged_codes_by_route(rel: str, prefix: str) -> dict:
    """从 handler 的 **AST** 里取"这条路由真正扣的是哪个码"。

    🔴 期望值**不手写**:手写就是我把答案抄两遍,然后锁住"我抄得一致"——
       那证明不了声明与实扣一致(本仓记过「判据自己构造被测行的输出」)。
       这里一侧读路由表、另一侧读 handler 源码,两个独立信源比对。

    返回 ``{(METHOD, 全路径): {扣费码, ...}}``。一个 handler 里可能有多处扣费
    (退费不算 —— 只收 ``deduct_points``),所以值是集合。
    """
    import ast as _ast
    import io as _io
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[2]
    with _io.open(root / rel, encoding="utf-8", newline="") as f:
        tree = _ast.parse(f.read())

    out: dict = {}
    for node in _ast.walk(tree):
        if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        routes = []
        for deco in node.decorator_list:
            if not isinstance(deco, _ast.Call) or not isinstance(deco.func, _ast.Attribute):
                continue
            method = deco.func.attr.upper()
            if method not in ("POST", "GET", "PUT", "DELETE", "PATCH"):
                continue
            if deco.args and isinstance(deco.args[0], _ast.Constant) \
                    and isinstance(deco.args[0].value, str):
                routes.append((method, prefix + deco.args[0].value))
        if not routes:
            continue
        codes = set()
        for inner in _ast.walk(node):
            if not isinstance(inner, _ast.Call):
                continue
            name = getattr(inner.func, "id", None) or getattr(inner.func, "attr", None)
            if name != "deduct_points":
                continue
            # deduct_points(user_id, "<feature_code>", ...) —— 码在第 2 个位置参数
            if len(inner.args) >= 2 and isinstance(inner.args[1], _ast.Constant) \
                    and isinstance(inner.args[1].value, str):
                codes.add(inner.args[1].value)
        if codes:
            for key in routes:
                out.setdefault(key, set()).update(codes)
    return out


def test_the_declared_billing_matches_what_the_handler_charges():
    """🔴 **一致性锁**(Review 2026-08-24 裁定条件① 指定):路由表**声明**的计费码
    必须等于该路由 handler **真正扣**的码。

    为什么需要这把锁而不是老那条「组织预算路由没变」:
    老那条锁的是「声明值原样不动」,理由是「动它会改变组织预算归属」。
    Review 的 census 证伪了那个理由 —— ``MemberRoutePolicy.billing_feature``
    **没有任何运行期消费方**(守卫只取 capability;塞进 ``request.state`` 后
    全仓无读取;组织预算走 handler 显式传给 ``deduct_points`` 的参数)。
    锁一个没人读的值"原样不动",锁住的是一个**错值**:声明写 A、实扣是 B。

    真正该守的是**对齐**:这一格迟早会被接线,接线那天拿到的必须是真值。
    """
    from services.organization_route_contract import MEMBER_GEO_ROUTE_POLICIES

    charged = _charged_codes_by_route("api/meijiehezi_api.py", "/api/meijiehezi")
    assert charged, "从 handler 里一个扣费码都没取到 —— 这条判据没有分母"

    declared = {(p.method, p.path_template): p.billing_feature
                for p in MEMBER_GEO_ROUTE_POLICIES}
    assert declared, "路由表为空 —— 这条判据没有分母"

    checked = []
    for key, codes in sorted(charged.items()):
        if key not in declared:
            continue          # 没登记进员工席位路由表的,不在本锁的作用域内
        assert len(codes) == 1, "一条路由扣了多个码,得先说清哪个才是它的:" + str((key, codes))
        assert declared[key] == next(iter(codes)), (
            "声明与实扣不一致 " + str(key) + ":声明 " + str(declared[key])
            + " / 实扣 " + str(codes))
        checked.append(key)

    # 分母下限:这两条 legacy 媒体发布路由**必须**在覆盖里。
    # 没有这一行,handler 改名/装饰器换写法会让上面的循环一条都不跑而判据全绿。
    assert ("POST", "/api/meijiehezi/publish") in checked, checked
    assert ("POST", "/api/meijiehezi/publish/batch") in checked, checked


def test_the_charge_extractor_really_reads_the_handler_literal():
    """🔴 活性自证:提取器读到的是 **handler 源码里那个字面量**。

    这一条守的是上面那把锁最可能的失效形态:提取器返回空 dict ⇒ 循环一条不跑 ⇒
    "声明与实扣一致"恒成立。喂一段自造源码,答案只可能来自被解析的文本本身
    (``CODE_FROM_HANDLER`` 这个串在本仓其它任何地方都不存在)。
    ``deduct_points`` 的位置一挪、装饰器写法一变,这里当场红。
    """
    import io as _io
    import tempfile
    from pathlib import Path as _Path

    tmp = _Path(tempfile.mkdtemp()) / "fake_api.py"
    with _io.open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write("@router.post('/pay')\n"
                "async def pay():\n"
                "    await deduct_points(uid, 'CODE_FROM_HANDLER', extra_cost=1)\n"
                "    await refund_points(uid, 'NOT_A_CHARGE')\n")

    got = _charged_codes_by_route(str(tmp), "/x")
    assert got == {("POST", "/x/pay"): {"CODE_FROM_HANDLER"}}, got


def test_prepare_can_actually_find_the_catalog_price_now(monkeypatch, world):
    """🔴 ③ 的主判据:**prepare 查的是这条链末端真正冻结的那个码。**

    ⚠️ 口径更正(2026-08-24 · 撤回本判据早先写的说法):收敛前的后果**不是**
    「``get_feature_pricing`` 每次 ValueError ⇒ 报价档恒 unavailable ⇒ 数字全靠
    渠道兜底」。生产 ``feature_pricing`` 里**两个码都在**
    (``db/wallet_db.seed_feature_pricing`` 同时种两行;Review 2026-08-24 生产
    只读实证:两者 ``cost_points`` 均 = 0)⇒ 生产上查得到。「恒 unavailable」
    只在**没种那一行的测试库**里成立 —— 夹具产物,不是生产缺陷。
    那条根因是我写错的,已就地更正,判据本身的断言没变。

    收敛真正的价值:prepare 查的必须是 execute 末端真正冻结的那个码。两行今天
    恰好都是 0,数字上看不出差别;哪天有人给其中一个配了真价,上屏数字与实扣
    数字就会静默劈叉 —— 而用户是按上屏那个数字按下确认的。

    ⚠️ 口径说清楚:``external`` 档**本来就不由目录出数字**
    (``_resolve_compute_quote`` 对这一档一律返回 ``pending_domain_adapter``,
    理由写在它自己的 docstring 里:真实算力取决于所选媒体)。
    所以「查得到」的可判形态是**报价档从 unavailable 变成 pending_domain_adapter**
    —— 那正是"目录里找到了这个码"与"根本没这个码"的分界。
    """
    import api.xiaobang_operations_api as ops
    from services import gap_operation_map as omap

    entry = omap.resolve("publish_center") if hasattr(omap, "resolve") else None
    contract = entry.command_contract if entry is not None else None
    if contract is None:
        for e in omap.commandable_operations():
            if e.operation_id == "publish_center":
                contract = e.command_contract
    assert contract is not None, "取不到 publish_center 合同 —— 判据没有分母"
    assert contract.billing_feature == "media_proxy_publish", contract.billing_feature

    conn = conn_for(world["dsn"])
    try:
        cur = conn.cursor()
        cur.execute("SELECT cost_points FROM feature_pricing WHERE feature_code = %s",
                    (contract.billing_feature,))
        row = cur.fetchone()
        assert row is not None, (
            "目录里没有 " + contract.billing_feature + " 这一行 —— "
            "生产侧要靠发车单里的幂等 ops 步骤补上(是数据不是 schema)")
        quote, meta = ops._resolve_compute_quote(cur, contract)
    finally:
        conn.close()

    assert meta["quote_state"] == "pending_domain_adapter", (
        "目录价仍然查不到(quote_state=" + str(meta.get("quote_state")) + ")—— "
        "收敛没生效,或者目录里缺这一行")


# ══════════════════════════════════════════════════════════════════════════
# ⑥ 死函数转活:frozen_reasons / kb_entries 接线(段二① · Owner 2026-08-24 批)
# ══════════════════════════════════════════════════════════════════════════
_FROZEN_TEXT = "这家媒体在你所在行业被 AI 引用得比较多，适合先投一篇打底。"


def _seed_frozen_reason(dsn, *, quote_id: str, brand_id: int) -> None:
    conn = conn_for(dsn)
    try:
        conn.cursor().execute(
            "INSERT INTO defgeo_xiaobang_frozen_reasons"
            " (reason_ref, tenant_owner_user_id, brand_id, subject_kind, subject_ref,"
            "  reason_code, public_text, next_action_kind, reasons_version)"
            " VALUES (%s,%s,%s,'quote_snapshot',%s,'industry_citation',%s,'view_quote',%s)"
            " ON CONFLICT (reason_ref) DO NOTHING",
            ("rsnpkgg" + quote_id, OWNER_UID, brand_id, quote_id, _FROZEN_TEXT,
             "defgeo-xiaobang-frozen-reasons-v1"))
    finally:
        conn.close()


def _client_with_quote(monkeypatch, world, quote_id: str):
    """与现役 ``_client`` 同形,只多给上下文一个 quote_id —— 那是冻结理由挂靠的对象。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import api.xiaobang_operations_api as ops

    class _Ctx:
        """🔴 **刻意不定义 owner_user_id** —— 生产的
        ``services.customer_operation_plan.AuthorizedAssistantContext``(L117-157)
        根本没有这个字段。第一版夹具自造了它,于是
        ``_frozen_reason_facts`` 在生产上恒返 [](接线等于没接)而判据全绿。
        夹具长成生产的样子,这个洞才回不来。
        """

        def public_context(self):
            return {"brand_id": world["brand_id"], "brand_name": "甲品牌",
                    "geo_post_id": world["post_id"], "quote_id": quote_id,
                    "data_updated_at": None}

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


def test_the_frozen_reason_actually_reaches_the_wire(monkeypatch, world):
    """🔴 段二①:**冻结理由真的被读出来并下发了。**

    在窗G 段二之前 ``operation_prepare`` 传的是 ``reason_facts=[]`` 写死 ⇒
    ``recommendation_reasons`` **恒空**,``frozen_reasons`` 整个模块是
    只有判据在 import 的死函数。

    把 ``reason_facts=_frozen_reason_facts(...)`` 改回 ``[]``,这条当场红 ——
    这就是工单要的「从调用方摘掉 → 红」。
    """
    quote_id = "q-pkgg-" + uuid.uuid4().hex[:8]
    _seed_frozen_reason(world["dsn"], quote_id=quote_id, brand_id=world["brand_id"])
    client = _client_with_quote(monkeypatch, world, quote_id)

    got = client.post(PREFIX + "/prepare", json={
        "prepare_request_id": "xbfr-" + uuid.uuid4().hex[:12],
        "selection": {"brand_id": world["brand_id"], "geo_post_id": world["post_id"],
                      "quote_id": quote_id,
                      "channel_option_id": "svideo:" + str(MEDIA_OK)},
    })
    assert got.status_code == 200, got.text[:800]
    reasons = got.json()["recommendation_reasons"]
    assert reasons, "冻结理由没被读出来 —— 接线没生效(或分母是空的)"
    assert any(r.get("text") == _FROZEN_TEXT for r in reasons), reasons


def test_a_reason_with_money_in_it_is_refused_not_softened(monkeypatch, world):
    """§9.6:小榜**不下发**任何价格/算力/百分比。

    🔴 [对抗复审订正] 第一版只断言 `status_code != 200` —— **裸 500 恰好满足它**,
       于是那条判据把一个 G-4 违规锁成了「通过」。实测:`FrozenReasonError`
       是 ValueError 子类,不被 prepare 的 `except IntentError` 捕获,直接逃成 500。

    现在的正确形态:prepare **仍然 200**(展示性字段不该绑架用户主流程),
    但那一批理由**一条都不出**。同一个对象上**同时种一条好理由和一条坏理由** ——
    好的那条证明分母非空,「一条都不出」才与「本来就没有理由」区分得开。
    """
    quote_id = 'q-money-' + uuid.uuid4().hex[:8]
    _seed_frozen_reason(world['dsn'], quote_id=quote_id, brand_id=world['brand_id'])
    conn = conn_for(world['dsn'])
    try:
        conn.cursor().execute(
            'INSERT INTO defgeo_xiaobang_frozen_reasons'
            ' (reason_ref, tenant_owner_user_id, brand_id, subject_kind, subject_ref,'
            '  reason_code, public_text, next_action_kind, reasons_version)'
            " VALUES (%s,%s,%s,'quote_snapshot',%s,'bad',%s,'view_quote','v1')",
            ('rsnbad' + quote_id, OWNER_UID, world['brand_id'], quote_id,
             '投这家能提升 40% 的推荐率'))
    finally:
        conn.close()

    client = _client_with_quote(monkeypatch, world, quote_id)
    got = client.post(PREFIX + '/prepare', json={
        'prepare_request_id': 'xbfr2-' + uuid.uuid4().hex[:12],
        'selection': {'brand_id': world['brand_id'], 'geo_post_id': world['post_id'],
                      'quote_id': quote_id,
                      'channel_option_id': 'svideo:' + str(MEDIA_OK)},
    })
    assert got.status_code == 200, (
        '一条展示性理由不合法就把整条链打成非 200 —— 那是裸 500 那一类:'
        + got.text[:400])
    assert got.json()['recommendation_reasons'] == [], (
        '坏理由在场时必须整批不下发(好坏混排会让人以为剩下的都审过了):'
        + str(got.json()['recommendation_reasons']))
    assert '40%' not in got.text, '把那句话原样回显了:' + got.text[:300]
    assert _FROZEN_TEXT not in got.text, (
        '只挑掉了坏的、留下好的 —— 这会让人以为剩下的都审过了')


def test_no_defensive_xiaobang_module_is_a_dead_function_any_more():
    """🔴 **消费者 census**:三个模块都必须有**生产侧**调用方。

    工单原话:仅测试调用 = 仍是死函数 = 不算完成。

    分母 = 全仓 ``*.py`` 里 ``api/`` + ``services/`` + ``tools/`` 三个目录
    (tests/ 与模块自身排除),机械扫 import 语句,不手抄。
    """
    import re as _re
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[2]
    modules = ("compute_estimate", "receipt_guard", "frozen_reasons",
               "kb_entries", "drift_notice")
    prod_dirs = ("api", "services", "tools")

    callers: dict[str, list[str]] = {m: [] for m in modules}
    # [对抗复审订正] 第一版用正则扫文本 —— **注释里提一句就能满足它**
    # (实测:整块 import 删掉、只留注释,census 仍全绿)。改成 AST 取真 import。
    import ast as _ast

    for d in prod_dirs:
        for path in (root / d).rglob('*.py'):
            rel = path.relative_to(root).as_posix()
            if '/defensive_geo/xiaobang/' in rel:
                continue
            try:
                tree = _ast.parse(path.read_text(encoding='utf-8', errors='ignore'))
            except SyntaxError:
                continue
            for m in modules:
                if m in _imported_xiaobang_modules(tree):
                    callers[m].append(rel)

    dead = sorted(m for m, c in callers.items() if not c)
    assert not dead, (
        "这些模块在生产侧零调用方 —— 仍是只有判据在用的死函数:" + str(dead)
        + "；实测调用方=" + str({k: v for k, v in callers.items() if v}))


def _imported_xiaobang_modules(tree) -> set:
    """从 AST 里取真正被 import 的 defensive_geo.xiaobang 子模块名。

    注释与字符串**骗不过它** —— 这正是第一版正则栽的地方。
    """
    import ast as _ast

    out = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom) and node.module:
            if node.module.endswith('defensive_geo.xiaobang'):
                out.update(a.name for a in node.names)
            elif '.defensive_geo.xiaobang.' in '.' + node.module + '.':
                out.add(node.module.rsplit('.', 1)[-1])
        elif isinstance(node, _ast.Import):
            for a in node.names:
                if '.defensive_geo.xiaobang.' in '.' + a.name + '.':
                    out.add(a.name.rsplit('.', 1)[-1])
    return out


def test_the_census_would_actually_notice_a_dead_module():
    """活性自证:census **认得出真 import,也不被注释/字符串骗过**。

    第一版这条只探了正则,而正则恰恰是被骗的那一环 —— 活性自证探错了地方,
    等于给一把恒绿的锁盖了章。现在直接喂三份源码给**同一个**取数函数。
    """
    import ast as _ast

    real = 'from services.defensive_geo.xiaobang.receipt_guard import assert_receipt_usable'
    assert 'receipt_guard' in _imported_xiaobang_modules(_ast.parse(real))

    only_comment = '# from services.defensive_geo.xiaobang.receipt_guard import x'
    assert 'receipt_guard' not in _imported_xiaobang_modules(_ast.parse(only_comment)), (
        '注释就能满足 census —— 那把锁是恒绿的')

    in_string = chr(88) + " = " + chr(39) + "services.defensive_geo.xiaobang.receipt_guard" + chr(39)
    assert 'receipt_guard' not in _imported_xiaobang_modules(_ast.parse(in_string)), (
        '字符串就能满足 census')


def test_the_kb_index_chain_really_calls_the_geo_only_gate(monkeypatch):
    """🔴 接线锁:**索引构建链真的会调那道闸**(不是只有判据在调)。

    做法 = 把闸换成"必抛",然后跑**真的** ``reindex_system``(单页、零 embedding)。
    链上真的调了 ⇒ 解析失败 ⇒ fail-closed 抛 ``SystemKbReleaseAborted``、一个字节都不写库。
    把 ``_assert_kb_page_is_geo_only(fp, card)`` 那一行从 ``reindex_system`` 里摘掉 ⇒
    这条当场红。

    🔴 刻意不 mock 数据库:这一跳在闸抛之后**根本走不到写库**,
       所以"没碰库"本身就是 fail-closed 的证据。
    """
    import tools.xiaobang_system_kb as skb

    calls = []

    def _boom(path, card):
        calls.append(path)
        raise AssertionError("gate-probe")

    monkeypatch.setattr(skb, "_assert_kb_page_is_geo_only", _boom)

    with pytest.raises(skb.SystemKbReleaseAborted) as excinfo:
        skb.reindex_system(pages_glob="knowledge/system_kb/pages/defensive-geo.md",
                           with_embeddings=False)

    assert calls, "闸根本没被调用 —— 接线是断的"
    assert "gate-probe" in str(excinfo.value), str(excinfo.value)


def test_every_real_kb_page_passes_the_geo_only_gate():
    """🔴 正样本 + 不误伤:现役**每一页**都必须过闸。

    只有负样本的闸是危险的 —— 一个写得太宽的判定会把整个知识库拦死,
    而那种失败只会在重建 release 的时候才现形(那时已经晚了)。
    分母 = 索引链自己的 glob 现扫,不手抄页名。
    """
    import glob as _glob
    from pathlib import Path as _Path

    import tools.xiaobang_system_kb as skb

    root = _Path(__file__).resolve().parents[2]
    files = [f for f in _glob.glob(str(root / "knowledge/system_kb/pages/*.md"))
             if not _Path(f).name.startswith("_")]
    assert len(files) >= 20, "KB 页分母只有 %d 个 —— 太少,疑似扫错目录" % len(files)

    for fp in files:
        card = skb.parse_final_page(fp)
        skb._assert_kb_page_is_geo_only(fp, card)


def test_the_geo_only_gate_refuses_a_social_route():
    """🔴 负样本:教用户去社媒页的条目,一律拒(Owner 2026-08-18 XIAOBANG-GEO-ONLY)。

    判的是**结构**(路由),不是措辞 —— 措辞锁会逼下一个人去删一句真话。
    """
    from services.defensive_geo.xiaobang.kb_entries import KbEntryError

    import tools.xiaobang_system_kb as skb

    with pytest.raises(KbEntryError):
        skb._assert_kb_page_is_geo_only(
            "knowledge/system_kb/pages/defensive-geo.md",
            {"route": "/social/studio", "page_name": "社媒工作台"})


def test_every_real_kb_page_has_the_front_matter_the_rebuild_requires():
    """🔴 **一页坏掉 = 整条 KB 重建发不出去**(fail-closed),所以逐页都要有分母。

    实测缺陷(窗G 段二①抓到):``knowledge/system_kb/pages/defensive-geo.md``
    自 ``dd3cd3cdb`` 起就没有 front-matter 的 ``route``。``reindex_system`` 对
    「缺 route/page_name」是**升级成解析失败**的(它的注释逐字写着"实测现役 26 页
    全都有这两项,所以零误伤"),而解析失败 ⇒ ``SystemKbReleaseAborted`` ⇒
    **上一 release 保持在线、新 release 永远发不出去**。

    也就是说:那一页写了、进了 git、也在 glob 下,但整个知识库从那天起没能重建过 ——
    比"这一页问不出来"严重一个量级,而此前没有任何判据在守。
    """
    import glob as _glob
    from pathlib import Path as _Path

    import tools.xiaobang_system_kb as skb

    root = _Path(__file__).resolve().parents[2]
    files = [f for f in _glob.glob(str(root / "knowledge/system_kb/pages/*.md"))
             if not _Path(f).name.startswith("_")]
    assert len(files) >= 20, "分母只有 %d 页 —— 疑似扫错目录" % len(files)

    broken = []
    for fp in files:
        card = skb.parse_final_page(fp)
        if not str(card.get("route") or "").strip():
            broken.append((_Path(fp).name, "缺 route"))
        elif not str(card.get("page_name") or "").strip():
            broken.append((_Path(fp).name, "缺 page_name"))
    assert not broken, (
        "这些页会让**整条** KB 重建 fail-closed 放弃(不是只有它自己问不出来):"
        + str(broken))


def test_a_declared_page_outside_the_index_glob_aborts_the_rebuild(monkeypatch):
    """🔴 「页写了但不进索引」这个假绿形态,由**索引链自己**拦。

    分母是索引构建链的 glob,不是一份文件清单 —— 所以这条把一个**不在 glob 下**
    的声明页塞进 ``NEW_KB_PAGES``,断言重建当场拒绝。
    把 ``assert_pages_are_indexed`` 那一行从 ``reindex_system`` 里摘掉 ⇒ 这条红。
    """
    from services.defensive_geo.xiaobang import kb_entries as kb

    import tools.xiaobang_system_kb as skb

    monkeypatch.setattr(kb, "NEW_KB_PAGES", ("docs/not-in-the-index.md",))
    with pytest.raises(kb.KbEntryError):
        skb.reindex_system(pages_glob="knowledge/system_kb/pages/*.md",
                           with_embeddings=False)


# ==========================================================================
# ⑦ 对抗复审自查补的两把锁
# ==========================================================================
def test_an_expired_quote_never_says_confirm_to_start(monkeypatch, world):
    """🔴 报价过期后,人话句子**不许**还写「确认后开始」。

    实测缺陷(本包自查):GET intent 在过期时会把 `quote_state` 覆盖成 'expired',
    而第一版 `build_public_estimate` 不知道过期 —— 同一个响应里机读键说过期、
    上屏句子说可以开始,**前端渲染的是后者**。而且 registry 里那两条 expired
    文案永远取不到(死文案)。

    把 `expired=quote_expired(row)` 那个入参去掉,这条当场红。
    """
    client = _client(monkeypatch, world)
    body = _prepare(client, world, media_id=MEDIA_OK).json()
    assert body['compute_estimate']['confirmable'] is True, body['compute_estimate']

    conn = conn_for(world['dsn'])
    try:
        conn.cursor().execute(
            'UPDATE xiaobang_operation_intents'
            " SET compute_quote_expires_at = NOW() - interval '1 hour'"
            ' WHERE intent_id = %s', (body['intent_id'],))
    finally:
        conn.close()

    got = client.get(PREFIX + '/intents/' + body['intent_id'])
    assert got.status_code == 200, got.text[:400]
    dto = got.json()
    est = dto['compute_estimate']
    assert dto.get('quote_state') == 'expired', dto.get('quote_state')
    assert est['confirmable'] is False, est
    assert '确认后开始' not in est['headline'], (
        '机读键说过期、上屏句子说可以开始 —— 前端渲染的是后者:' + str(est))
    assert '已经过期' in est['headline'] or '过期' in est['headline'], est
    assert '没有扣除任何算力' in est['headline'], est
    assert 'amount' not in est, '过期了还给数字,她会以为按下去就是这个价:' + str(est)
    assert est['next_action'].strip(), est


def test_execute_freezes_the_media_price_under_the_production_catalog_shape(monkeypatch, world):
    """🔴🔴 **Owner 口径的落点**:先示预估 → 用户确认 → 真冻到**屏上那个数**。

    这条判据在 R1 是 ``xfail(strict)`` —— 它把一个**存量 P0** 钉在明处:
    ``publish_batch_core._freeze`` 调 ``freeze_one_item`` 时不传 ``extra_cost``,
    而 ``media_proxy_publish`` 是动态定价 SKU、目录价就是 0
    ⇒ ``total_cost = 0 + 0 = 0`` ⇒ billing 在开事务前 return free/零句柄
    ⇒ ``FreezeProducedNoHandle`` ⇒ ``SETTLEMENT_HANDLE_INVALID`` **HTTP 500**。
    Owner 2026-08-24 批「修」,R2 已修(`publish_batch_core.py` 的 ``extra_cost=int(amount)``)
    ⇒ 本条**按它自己的设计翻正为常驻正向判据**。

    🔴 三点刻意:

    ① **目录价不再由本判据现改**。R1 时夹具把目录价与媒体价拉平,所以这条得
       自己 UPDATE 回 0 才能看见真相。R2 把那个拉平口径整个撤了(conftest 的
       ``PUBLISH_CATALOG_POINTS``),夹具本身就是生产形态 ⇒ 这里**改为断言**
       它确实是 0。夹具替被测代码干活是本仓的头号假绿源,断言比现改诚实。

    ② **右边不写 PUBLISH_POINTS,写「屏上真的出现过的那个数」**。
       写常量的话,判据锁的是「冻结额 == 我在夹具里写的数」;
       Owner 要的是「冻结额 == 用户看到并据此按下确认的那个数」。
       所以先从 confirm 前的 DTO 里把 ``compute_estimate.amount`` 取出来
       (那正是 §0.5.5 U-2 要求的必答时刻上屏的数字),再拿它跟冻结增量比。

    ③ 顺带钉住**钱真的动了**:``point_freezes`` 多一行、``frozen_points``
       增量等于那个数。只断言 200 的话,「返回 200 但一分没冻」照样过 ——
       那恰恰是修之前那个洞的镜像。
    """
    catalog = one(world['dsn'],
                  'SELECT cost_points FROM feature_pricing WHERE feature_code = %s',
                  ('media_proxy_publish',))
    assert catalog is not None, '目录里没有这个码 —— freeze 会以 ValueError 失败,不是本条要测的事'
    assert int(catalog['cost_points']) == 0, (
        '夹具不是生产形态了(生产 media_proxy_publish.cost_points = 0,'
        'Review 2026-08-24 只读实证)。目录价一旦非 0,这条判据测的就不是生产:'
        + str(catalog))

    client = _client(monkeypatch, world)
    body = _prepare(client, world, media_id=MEDIA_OK).json()

    # ① 用户在确认屏上看到的那个数(§0.5.5 U-2 的必答时刻)
    shown = body['compute_estimate']
    assert shown.get('confirmable') is True, shown
    displayed = int(shown['amount'])
    assert displayed > 0, '预估是 0 的话,下面「冻结额 == 预估额」会被 0 == 0 白送:' + str(shown)
    assert str(displayed) in shown['headline'], (
        '机读的 amount 与上屏句子里的数字不是同一个 —— 用户按的是句子:' + str(shown))

    confirmed = _confirm(client, body['intent_id'], int(body['intent_revision']))
    assert confirmed.status_code == 200, confirmed.text[:400]

    before = wallet(world['dsn'])
    before_counts = _counts(world['dsn'])
    got = _execute(client, body['intent_id'], int(confirmed.json()['intent_revision']),
                   request_id='prodshape-' + uuid.uuid4().hex[:8])
    assert got.status_code == 200, (
        '生产目录形态下真链没跑通(修之前这里是 SETTLEMENT_HANDLE_INVALID 500):'
        + got.text[:600])

    after = wallet(world['dsn'])
    d = _delta(before_counts, _counts(world['dsn']))
    assert d['point_freezes'] == 1, '没产生冻结句柄(零句柄的「成功」):' + str(d)
    assert int(after['frozen_points']) - int(before['frozen_points']) == displayed, (
        '冻结额 != 确认屏展示额。屏上 ' + str(displayed) + ',实冻 '
        + str(int(after['frozen_points']) - int(before['frozen_points'])))


def test_a_nonzero_catalog_base_refuses_loudly_instead_of_overcharging(monkeypatch, world):
    """🔴 上一条的**反面**:目录基价若被改成非 0,必须**响亮拒单 + 一分不冻**。

    修法是 ``extra_cost = 确认价``,前提是这个 SKU 的目录基价 = 0
    (动态定价,钱全在媒体目录里)。这个前提不该是一句**没人验的注释** ——
    有人哪天给 ``media_proxy_publish`` 配了真价,``total = base + 确认价``
    就会 **> 用户看到的数**。

    本条锁住那一刻的行为:``freeze_one_item`` 内部的
    ``amount != expected_points`` 核对会抛 ``FundingHandleInvalid`` ⇒
    execute 翻成 ``SETTLEMENT_HANDLE_INVALID``(有 message、有 next_action,
    不是裸 500)⇒ **宁可拒单,也不静默多扣**。
    """
    conn = conn_for(world['dsn'])
    try:
        conn.cursor().execute(
            'UPDATE feature_pricing SET cost_points = %s WHERE feature_code = %s',
            (777, 'media_proxy_publish'))
    finally:
        conn.close()

    client = _client(monkeypatch, world)
    body = _prepare(client, world, media_id=MEDIA_OK).json()
    confirmed = _confirm(client, body['intent_id'], int(body['intent_revision']))
    assert confirmed.status_code == 200, confirmed.text[:400]

    before = wallet(world['dsn'])
    before_counts = _counts(world['dsn'])
    got = _execute(client, body['intent_id'], int(confirmed.json()['intent_revision']),
                   request_id='basenonzero-' + uuid.uuid4().hex[:8])

    assert got.status_code != 200, (
        '目录基价非 0 时冻的钱会比屏上多 —— 这一发必须拒单:' + got.text[:600])
    detail = got.json().get('detail') or got.json()
    assert detail.get('error_code') == 'SETTLEMENT_HANDLE_INVALID', detail
    assert str(detail.get('message') or '').strip(), '拒单没给人话:' + str(detail)
    assert str(detail.get('next_action') or '').strip(), '拒单没给下一步(§0.5.6):' + str(detail)

    after = wallet(world['dsn'])
    d = _delta(before_counts, _counts(world['dsn']))
    assert int(after['frozen_points']) == int(before['frozen_points']), (
        '拒单了还冻了钱:' + str((before, after)))
    assert d['point_freezes'] == 0, '拒单了还留下冻结行:' + str(d)
