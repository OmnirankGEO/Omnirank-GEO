"""#74 P0:争议裁决 settle 侧必须把 escrow 释放的额度写回客户钱包。

在 `2aef3b59b`(2026-07-27 单账本收敛)之前,core 的 `allocate_credit` 承担客户入账;
那笔以「客户的算力留在 user_wallets(complete_recharge 已入账)」为由删掉了它 ——
**这句话对充值主路径成立,对争议路径不成立**:`create_escrow` 在 hold 时
`UPDATE user_wallets … paid_points - base` 把钱**反扣走了**。
于是 keep_old / reassign 两种裁决下,客户的 base+bonus 直接蒸发,而
`dispute_settlement` 的日志逐字写「客户额度 +N」、模块 docstring 写「守恒」——
**都描述了一件没发生的事**。

🔴 **环境前提(实测踩到)**:`recharge_orders.settlement_mode` 由**手工迁移脚本**
   `scripts/migration_v35_factory_inventory_2026_05_26.sql:57` 建,**不在应用启动的
   自动迁移重放里**。⇒ 任何「从自动迁移新建」的环境都没有这列,而
   `dispute_settlement.py:112` 无条件 UPDATE 它 —— 那种环境下每次裁决都会炸。
   生产有(手工跑过),所以不是线上缺陷;但它是**部署脆弱点**,已报 Review。

⚠️ **本文件的边界**(写明,免得被读成比实际更宽):
   这里打桩了 `record_v35_core_settlement` 与两个税率取数 —— 它们各有自己的判据。
   本文件只锁 **escrow → user_wallets 这一跳的守恒**,那正是 #74 修的东西。
"""

from __future__ import annotations

import json
import os

import psycopg2
import psycopg2.extras
import pytest

from db import dispute_escrow_db as ESCROW
from services import dispute_settlement as DS

BASE, BONUS = 700, 30


@pytest.fixture
def p74_db():
    """🔴 自带连接,**不请求本包的 `db` 夹具**。

    本包 session 夹具会 `DROP SCHEMA public CASCADE` 重建 —— 指向别的库会毁掉它。
    这里只读写自己插的几行,并在最后整笔 rollback,不留残留。
    """
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.fail("没有 TEST_DATABASE_URL —— 记「未评估」,不是通过")
    conn = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _stub_core(monkeypatch):
    monkeypatch.setattr(
        "services.settlement_orchestrator.record_v35_core_settlement",
        lambda cursor, **kw: {"breakdown": {"factory_cents": 0, "agent_settlement_cents": 0},
                              "ledger_id": 1})
    monkeypatch.setattr("services.agent_pricing.get_agent_tax_rate_bps", lambda cur, uid: 0)
    monkeypatch.setattr("services.agent_pricing.get_agent_tax_mode", lambda cur, uid: "none")


def _world(cur, *, paid=5000, bonus=500):
    """一个客户 + 一个钱包 + 一笔 held escrow。"""
    cur.execute("INSERT INTO users (username, password_hash, display_name) "
                "VALUES (%s,%s,%s) RETURNING id",
                ("p74-cust", "x", "P74 客户"))
    customer = cur.fetchone()["id"]
    cur.execute("INSERT INTO users (username, password_hash, display_name) "
                "VALUES (%s,%s,%s) RETURNING id",
                ("p74-agent", "x", "P74 服务商"))
    agent = cur.fetchone()["id"]
    cur.execute("INSERT INTO user_wallets (user_id, paid_points, bonus_points) VALUES (%s,%s,%s)",
                (customer, paid, bonus))
    cur.execute(
        "INSERT INTO dispute_escrow (order_id, dispute_id, customer_user_id, order_agent_user_id,"
        " amount_cents, base_points, bonus_points, pricing_snapshot, status)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'held') RETURNING *",
        ("P74-ORDER", None, customer, agent, 9900, BASE, BONUS,
         json.dumps({"tool_points": BASE, "customer_paid_cents": 9900,
                     "wholesale_cents": 5000, "payment_method": "wechat_pay"})))
    return customer, agent, dict(cur.fetchone())


def _wallet(cur, uid):
    cur.execute("SELECT paid_points, bonus_points FROM user_wallets WHERE user_id=%s", (uid,))
    r = cur.fetchone()
    return int(r["paid_points"]), int(r["bonus_points"])


def _set_snapshot(cur, esc, snap: dict):
    """🔴 快照必须写进**库里那一行**。

    `settle_escrow_to_agent` 的 snap 取自 CAS 的 `RETURNING pricing_snapshot`,
    **不读**调用方传进来的 esc 字典。改 `esc["pricing_snapshot"]` 到不了被测代码 ——
    我第一版就是这么写的,`test_credited_amount_comes_from_the_escrow_row`
    因此**因为错误的原因而通过**:它以为自己改了快照,其实什么都没改。
    """
    cur.execute("UPDATE dispute_escrow SET pricing_snapshot = %s::jsonb WHERE id = %s",
                (json.dumps(snap, ensure_ascii=False), esc["id"]))


def test_settle_credits_the_customer_wallet(p74_db, monkeypatch):
    """🔴 核心不变量:escrow 出账 ⇒ 客户钱包等额入账。修复前这里恒 (0, 0)。"""
    _stub_core(monkeypatch)
    with p74_db.cursor() as cur:
        customer, agent, esc = _world(cur)
        before = _wallet(cur, customer)
        assert DS.settle_escrow_to_agent(cur, esc, agent, admin_id=1) is True
        after = _wallet(cur, customer)
    assert (after[0] - before[0], after[1] - before[1]) == (BASE, BONUS), (
        "settle 后客户钱包 delta=%r,应为 (%d, %d) —— 额度蒸发" % (
            (after[0] - before[0], after[1] - before[1]), BASE, BONUS))


def test_refund_credits_the_same_amount(p74_db):
    """反向对照:refund 侧本来就对。两侧 delta 必须一致 —— 差异即不对称。"""
    with p74_db.cursor() as cur:
        customer, _agent, esc = _world(cur)
        before = _wallet(cur, customer)
        assert ESCROW.refund_escrow(cur, esc, admin_id=1) is True
        after = _wallet(cur, customer)
    assert (after[0] - before[0], after[1] - before[1]) == (BASE, BONUS)


def test_repeat_settle_does_not_credit_twice(p74_db, monkeypatch):
    """幂等:CAS 已在,重复裁决必须 no-op 且**钱包不再变**。

    只断言返回 False 不够 —— 返回值与钱包是两件事。
    """
    _stub_core(monkeypatch)
    with p74_db.cursor() as cur:
        customer, agent, esc = _world(cur)
        assert DS.settle_escrow_to_agent(cur, esc, agent, admin_id=1) is True
        mid = _wallet(cur, customer)
        assert DS.settle_escrow_to_agent(cur, esc, agent, admin_id=1) is False
        after = _wallet(cur, customer)
    assert after == mid, "重复裁决又入账了一次:%r → %r" % (mid, after)


def test_credited_amount_comes_from_the_escrow_row(p74_db, monkeypatch):
    """🔴 金额取 **escrow 行本身**,不重算、不读当前价。

    把快照里的价格字段全改成另一套值,入账额**必须不变** ——
    退款/结算一律按原订单不可变快照单跳反转。
    """
    _stub_core(monkeypatch)
    with p74_db.cursor() as cur:
        customer, agent, esc = _world(cur)
        _set_snapshot(cur, esc, {"tool_points": 1, "customer_paid_cents": 1,
                                 "wholesale_cents": 1, "payment_method": "alipay"})
        before = _wallet(cur, customer)
        assert DS.settle_escrow_to_agent(cur, esc, agent, admin_id=1) is True
        after = _wallet(cur, customer)
    assert (after[0] - before[0], after[1] - before[1]) == (BASE, BONUS), (
        "入账额随快照价格变了 —— 说明它在重算,而不是按 escrow 行反转")


def test_missing_wallet_row_refuses_to_settle(p74_db, monkeypatch):
    """🔴 防蒸发自证:钱包行缺失 ⇒ UPDATE 0 行 ⇒ 必须 raise,拒绝坐实 settled。

    与 `refund_escrow:139` 同解。没有这条,escrow 会被标成 settled 而钱哪儿都不在,
    且函数返回 True、日志报成功。
    """
    _stub_core(monkeypatch)
    with p74_db.cursor() as cur:
        customer, agent, esc = _world(cur)
        cur.execute("DELETE FROM user_wallets WHERE user_id=%s", (customer,))
        with pytest.raises(RuntimeError, match="防资金蒸发"):
            DS.settle_escrow_to_agent(cur, esc, agent, admin_id=1)


def test_the_fixture_would_notice_a_silent_no_op(p74_db, monkeypatch):
    """正样本自证:夹具本身能分辨「入账了」与「没入账」。

    否则上面几条的 delta 断言可能只是恒真。
    """
    with p74_db.cursor() as cur:
        customer, _agent, _esc = _world(cur)
        before = _wallet(cur, customer)
        after = _wallet(cur, customer)
    assert after == before, "什么都没做时 delta 应为 0"
    assert before != (0, 0), "夹具没真的建出余额 —— delta 断言会恒真"


# ══════════════════════════════════════════════════════════════════════════
# ③ hold 精确扣:不钳位,不足则拒(否则 settle/refund 按全额回补 = 造钱)
# ══════════════════════════════════════════════════════════════════════════

def _mk_customer(cur, *, paid, bonus, tag="p74x"):
    cur.execute("INSERT INTO users (username, password_hash, display_name) "
                "VALUES (%s,%s,%s) RETURNING id", (tag, "x", tag))
    uid = cur.fetchone()["id"]
    cur.execute("INSERT INTO user_wallets (user_id, paid_points, bonus_points) VALUES (%s,%s,%s)",
                (uid, paid, bonus))
    return uid


def test_hold_deducts_exactly_and_does_not_clamp(p74_db):
    """余额充足 ⇒ 精确扣 base/bonus,不多不少。"""
    with p74_db.cursor() as cur:
        uid = _mk_customer(cur, paid=1000, bonus=100)
        ESCROW.create_escrow(cur, order_id="P74-EXACT", dispute_id=None, customer_user_id=uid,
                             order_agent_user_id=uid, bound_agent_user_id=None,
                             amount_cents=9900, base_points=BASE, bonus_points=BONUS,
                             pricing_snapshot={})
        assert _wallet(cur, uid) == (1000 - BASE, 100 - BONUS)


@pytest.mark.parametrize("paid,bonus,why", [
    (BASE - 1, 100, "paid 不足"),
    (1000, BONUS - 1, "bonus 不足"),
])
def test_hold_refuses_when_balance_is_short(p74_db, paid, bonus, why):
    """🔴 余额不足 ⇒ **拒**,不静默少扣。

    原来是 `GREATEST(0, paid_points - base)`:少扣了,而 settle/refund 按 escrow
    记的**全额**回补 ⇒ 凭空造算力。防御一件不该发生的事,而防御方式本身会造钱。
    """
    with p74_db.cursor() as cur:
        uid = _mk_customer(cur, paid=paid, bonus=bonus, tag="p74short")
        with pytest.raises(RuntimeError, match="反扣失败"):
            ESCROW.create_escrow(cur, order_id="P74-SHORT", dispute_id=None, customer_user_id=uid,
                                 order_agent_user_id=uid, bound_agent_user_id=None,
                                 amount_cents=9900, base_points=BASE, bonus_points=BONUS,
                                 pricing_snapshot={})


def test_hold_refuses_when_the_wallet_row_is_missing(p74_db):
    """钱包行缺失 ⇒ UPDATE 0 行 ⇒ 拒。escrow 记了全额而钱从没被扣走,同样是造钱。"""
    with p74_db.cursor() as cur:
        cur.execute("INSERT INTO users (username, password_hash, display_name) "
                    "VALUES (%s,%s,%s) RETURNING id", ("p74nowallet", "x", "n"))
        uid = cur.fetchone()["id"]        # 故意不建 user_wallets 行
        with pytest.raises(RuntimeError, match="反扣失败"):
            ESCROW.create_escrow(cur, order_id="P74-NOWALLET", dispute_id=None, customer_user_id=uid,
                                 order_agent_user_id=uid, bound_agent_user_id=None,
                                 amount_cents=9900, base_points=BASE, bonus_points=BONUS,
                                 pricing_snapshot={})


def test_the_refusal_message_separates_the_two_causes(p74_db):
    """两种原因处置相同(都拒),但**报文要分得开** —— 排查方向完全不同。"""
    with p74_db.cursor() as cur:
        uid = _mk_customer(cur, paid=1, bonus=1, tag="p74why")
        try:
            ESCROW.create_escrow(cur, order_id="P74-WHY", dispute_id=None, customer_user_id=uid,
                                 order_agent_user_id=uid, bound_agent_user_id=None,
                                 amount_cents=9900, base_points=BASE, bonus_points=BONUS,
                                 pricing_snapshot={})
            pytest.fail("余额不足竟然放行了")
        except RuntimeError as err:
            assert "余额不足" in str(err), "报文没说清是余额不足:%s" % err
            assert "钱包行不存在" not in str(err), "把余额不足报成了行不存在"


# ══════════════════════════════════════════════════════════════════════════
# P3 税制:优先读快照冻的;结给别人时现读并记明溯源
# ══════════════════════════════════════════════════════════════════════════

def test_hold_freezes_the_order_agents_tax_into_the_snapshot(p74_db, monkeypatch):
    """裁决常在数月后 —— 现读等于拿今天的税制结算几个月前的订单。"""
    monkeypatch.setattr("services.agent_pricing.get_agent_tax_rate_bps", lambda cur, uid: 1234)
    monkeypatch.setattr("services.agent_pricing.get_agent_tax_mode", lambda cur, uid: "inclusive")
    with p74_db.cursor() as cur:
        uid = _mk_customer(cur, paid=5000, bonus=500, tag="p74tax")
        ESCROW.create_escrow(cur, order_id="P74-TAX", dispute_id=None, customer_user_id=uid,
                             order_agent_user_id=uid, bound_agent_user_id=None,
                             amount_cents=9900, base_points=BASE, bonus_points=BONUS,
                             pricing_snapshot={"tool_points": BASE})
        cur.execute("SELECT pricing_snapshot FROM dispute_escrow WHERE order_id='P74-TAX'")
        snap = cur.fetchone()["pricing_snapshot"]
    snap = json.loads(snap) if isinstance(snap, str) else snap
    assert snap["order_agent_tax_rate_bps"] == 1234
    assert snap["order_agent_tax_mode"] == "inclusive"
    assert snap["order_agent_user_id"] == uid, "没记清冻的是**谁的**税 —— reassign 时会张冠李戴"
    assert snap["tool_points"] == BASE, "原快照字段被覆盖了"


def _boom_tax(monkeypatch):
    """显式把税表查询打成失败 —— 前提由判据制造,不由库状态制造。"""
    def _raise(*_a, **_k):
        raise RuntimeError('agent_tax_profiles 不可用(注入)')
    monkeypatch.setattr('services.agent_pricing.get_agent_tax_rate_bps', _raise)


def test_tax_lookup_failure_does_not_poison_the_transaction(p74_db, monkeypatch):
    """🔴🔴 税制取数失败**不许毒化事务** —— 这条钉的是我自己写出来的缺陷。

    第一版 `_freeze_tax` 是 `try/except` 包住税表查询。`except` 只吞得掉 **Python 异常**,
    吞不掉 **PostgreSQL 的事务中毒状态**:一条失败的 SELECT 之后,同一事务里后续
    每一句都 `InFailedSqlTransaction`。而本函数跑在**充值主事务**内 ⇒
    税表读不到会连累**整笔充值回滚**,代码表面上却「优雅降级了」。
    本仓 `_hold_for_dispute` 三行之外就写着同一条教训 —— **注释传不出去**,
    所以用 SAVEPOINT(会自己生效)+ 本判据。

    🔴 **失败是显式注入的,不是靠库状态**(Review 订正):上一版依赖测试库里
    没有 `agent_tax_profiles`。在有那张表的库上查询会**成功**,
    「查询失败」这个前提根本不发生 —— 判据仍绿,但**绿的是另一件事**。
    靠环境制造前提 = 让同一条判据在不同库上考不同的题。
    """
    _boom_tax(monkeypatch)
    with p74_db.cursor() as cur:
        uid = _mk_customer(cur, paid=5000, bonus=500, tag='p74poison')
        eid = ESCROW.create_escrow(
            cur, order_id='P74-POISON', dispute_id=None, customer_user_id=uid,
            order_agent_user_id=uid, bound_agent_user_id=None,
            amount_cents=9900, base_points=BASE, bonus_points=BONUS,
            pricing_snapshot={'tool_points': BASE})
        assert eid, '税制取不到就不建 escrow 了 —— 降级降过头'
        # 🔴 事务还能用吗?这一句就是「没中毒」的证据。
        cur.execute('SELECT pricing_snapshot FROM dispute_escrow WHERE id = %s', (eid,))
        snap = cur.fetchone()['pricing_snapshot']
        assert _wallet(cur, uid) == (5000 - BASE, 500 - BONUS), '反扣没生效'
    snap = json.loads(snap) if isinstance(snap, str) else snap
    assert snap['tool_points'] == BASE, '原快照字段丢了'
    for k in ('order_agent_user_id', 'order_agent_tax_rate_bps', 'order_agent_tax_mode'):
        assert k not in snap, '冻结失败却留下了半填的 %s' % k


def test_the_freeze_actually_writes_all_three_keys_when_it_works(p74_db, monkeypatch):
    """🔴 正样本臂:**不注入失败时三键必须齐全**。

    没有这一臂,上一条会在「冻结逻辑整个被删掉」时**照样绿** ——
    「失败时不留半填键」与「从来不写这三个键」在那条断言下同形。
    """
    monkeypatch.setattr('services.agent_pricing.get_agent_tax_rate_bps', lambda c, u: 777)
    monkeypatch.setattr('services.agent_pricing.get_agent_tax_mode', lambda c, u: 'ok_mode')
    with p74_db.cursor() as cur:
        uid = _mk_customer(cur, paid=5000, bonus=500, tag='p74ok')
        eid = ESCROW.create_escrow(
            cur, order_id='P74-OKTAX', dispute_id=None, customer_user_id=uid,
            order_agent_user_id=uid, bound_agent_user_id=None,
            amount_cents=9900, base_points=BASE, bonus_points=BONUS,
            pricing_snapshot={'tool_points': BASE})
        cur.execute('SELECT pricing_snapshot FROM dispute_escrow WHERE id = %s', (eid,))
        snap = cur.fetchone()['pricing_snapshot']
    snap = json.loads(snap) if isinstance(snap, str) else snap
    assert snap['order_agent_tax_rate_bps'] == 777
    assert snap['order_agent_tax_mode'] == 'ok_mode'
    assert snap['order_agent_user_id'] == uid


def _capture_core(monkeypatch):
    """把 core 换成记录入参的桩 —— 它是税制真正流向的地方。"""
    seen = {}

    def _fake(cursor, **kw):
        seen.update(kw)
        return {"breakdown": {"factory_cents": 0, "agent_settlement_cents": 0}, "ledger_id": 1}

    monkeypatch.setattr("services.settlement_orchestrator.record_v35_core_settlement", _fake)
    monkeypatch.setattr("services.agent_pricing.get_agent_tax_rate_bps", lambda cur, uid: 9999)
    monkeypatch.setattr("services.agent_pricing.get_agent_tax_mode", lambda cur, uid: "live_mode")
    return seen


def test_settle_uses_the_frozen_tax_when_the_agent_is_unchanged(p74_db, monkeypatch):
    """🔴 [#74 P3] 结给**同一个** agent ⇒ 必须用快照冻的税制,不是今天的配置。

    没有这条,「读快照」这件事在判据下完全不可观测 —— 实测:
    把 `if _use_frozen:` 改成 `if False:`(恒现读)时,13 条判据**全绿**。
    现读的坏处不会立刻显形:数月后裁决,用今天的税率结算几个月前的订单,
    账面自洽、无人报错。
    """
    seen = _capture_core(monkeypatch)
    with p74_db.cursor() as cur:
        customer, agent, esc = _world(cur)
        _set_snapshot(cur, esc, {
            "tool_points": BASE, "customer_paid_cents": 9900, "wholesale_cents": 5000,
            "payment_method": "wechat_pay",
            "order_agent_user_id": agent, "order_agent_tax_rate_bps": 1234,
            "order_agent_tax_mode": "frozen_mode"})
        assert DS.settle_escrow_to_agent(cur, esc, agent, admin_id=1) is True
    assert seen["tax_rate_bps"] == 1234, "用了现读的税率(实得 %r)" % seen["tax_rate_bps"]
    assert seen["tax_mode"] == "frozen_mode"


def test_settle_falls_back_to_live_tax_when_reassigned_to_another_agent(p74_db, monkeypatch):
    """🔴 反臂:`reassign` 结给**另一个** agent ⇒ 冻的是 A 的税,套到 B 身上是错的。

    这条与上一条成对。只有上一条时,把「同一个 agent」这个条件删掉也不会红 ——
    那样 B 会被按 A 的税率结算,而账面同样自洽。
    """
    seen = _capture_core(monkeypatch)
    with p74_db.cursor() as cur:
        customer, agent, esc = _world(cur)
        other = _mk_customer(cur, paid=0, bonus=0, tag="p74other")
        _set_snapshot(cur, esc, {
            "order_agent_user_id": agent, "order_agent_tax_rate_bps": 1234,
            "order_agent_tax_mode": "frozen_mode"})
        assert DS.settle_escrow_to_agent(cur, esc, other, admin_id=1) is True
    assert seen["tax_rate_bps"] == 9999, "把 A 的冻结税率套到了 B 身上"
    assert seen["tax_mode"] == "live_mode"
