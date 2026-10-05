"""双价目表 SSOT 后端测试 —— 映射 SPEC §15 金标准场景 + 守恒 + 防重放。

覆盖:catalog 版本化/§11 护栏、price_quote 报价防篡改/一单一报价/过期、
     channel 复利解析/成环/深度/收益台账幂等、快照分离、account codes、flags。
运行:TEST_DATABASE_URL=postgresql://postgres:throw@localhost:55432/ssottest pytest tests/pricing_ssot -q
"""
import json

import psycopg2
import pytest

from db.connection import get_db
from services import pricing_catalog as pc
from services import price_quote as pq
from services import channel_pricing as ch
from services import account_codes as ac
from services.price_quote import QuoteError
from services.channel_pricing import ChannelError
from config import pricing_ssot_flags as flags


def _retail_draft(scope="SV-A", code="retail-1", final=180000, floor=100000, base=None, mult=10000, points=195000):
    # catalog 现在一律后端算 final = ceil(base*mult/10000);默认 base=final,mult=10000 → computed==final
    if base is None:
        base = final
    return pc.create_draft_version(
        catalog_type="retail", scope_key=scope, version_code=code,
        entries=[{"product_code": "credit_basic", "base_price_cents": base, "multiplier_bps": mult,
                  "final_price_cents": final, "paid_points": points, "cost_floor_cents": floor,
                  "usage_example_version": "consume-1"}])


def _proc_draft(scope="PLATFORM_BASE", code="proc-1", final=120000, points=195000):
    pricing_config = {
        "wholesale_numer": final,
        "wholesale_denom": points,
        "agent_purchase_bonus_rate": 0,
        "bonus_validity_months": 12,
        "founding": {"cap": 10, "min_first_order_yuan": 500,
                     "first_order_extra_bonus": 0},
        "agent_tier_config": {},
    }
    version_id = pc.create_draft_version(
        catalog_type="procurement", scope_key=scope, version_code=code,
        entries=[{"product_code": "credit_basic", "base_price_cents": final, "multiplier_bps": 10000,
                  "final_price_cents": final, "paid_points": points}],
        calc_meta={"pricing_config_snapshot": pricing_config},
    )
    source_ref = {
        "kind": "agent_purchase_option",
        "option_id": "credit_basic",
        "amount_cents": final,
        "option": {"option_id": "credit_basic", "amount_cents": final,
                   "reward_eligible": False},
    }
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE pricing_catalog_entries SET source_ref_jsonb=%s::jsonb WHERE version_id=%s",
            (json.dumps(source_ref), version_id),
        )
    return version_id


def _insert_quote_backed_paid_order(
    cur, quote, order_id: str, *, refund_status=None, refund_completed: bool = False,
):
    """Seed legacy lifecycle tests without violating the new complete quote-anchor CHECK."""
    snapshot = pq.quote_order_pricing_snapshot(quote, required=True)
    cur.execute(
        """INSERT INTO recharge_orders (
             id,user_id,amount_cents,base_points,bonus_points,payment_method,payment_status,
             refund_status,refund_completed_at,price_quote_id,pricing_catalog_version,
             pricing_snapshot_jsonb
           ) VALUES (
             %s,%s,%s,%s,%s,'wechat','paid',%s,
             CASE WHEN %s THEN NOW() ELSE NULL END,%s,%s,%s::jsonb
           )""",
        (
            order_id, quote["buyer_user_id"], quote["final_price_cents"],
            quote["points_granted"], quote["bonus_points"], refund_status,
            refund_completed, quote["quote_id"], quote["catalog_version"],
            json.dumps(snapshot, ensure_ascii=False),
        ),
    )


# =============================== catalog + §11 ===============================
def test_publish_and_read_single_open_version():
    vid = _retail_draft()
    ver = pc.publish_version(vid, approved_by=1)
    assert ver["status"] == "published"
    cat = pc.get_published_catalog("retail", "SV-A")
    assert cat["items"][0]["final_price_cents"] == 180000
    # §15.2#8 republish → old archived, only one open
    vid2 = _retail_draft(code="retail-2", final=190000)
    pc.publish_version(vid2, approved_by=1)
    cat2 = pc.get_published_catalog("retail", "SV-A")
    assert cat2["items"][0]["final_price_cents"] == 190000
    versions = pc.list_versions("retail", "SV-A")
    statuses = sorted(v["status"] for v in versions)
    assert statuses == ["archived", "published"]  # exactly one published


def test_gate_below_cost_blocked():
    vid = _retail_draft(final=50000, floor=100000)  # below cost
    with pytest.raises(ValueError, match="低于有效成本底线"):
        pc.publish_version(vid, approved_by=1)


def test_gate_extreme_high_needs_approval():
    vid = _retail_draft(base=1000, final=100000, floor=1000, mult=1000000)  # 990000 bps > hard_block 300000
    with pytest.raises(ValueError, match="人工审批"):
        pc.publish_version(vid, approved_by=None)
    ver = pc.publish_version(vid, approved_by=99)  # with approver
    assert ver["status"] == "published"


def test_gate_zero_price_or_points_blocked():
    vid = pc.create_draft_version(catalog_type="retail", scope_key="SV-Z", version_code="z1",
        entries=[{"product_code": "credit_basic", "base_price_cents": 0, "multiplier_bps": 10000,
                  "final_price_cents": 0, "paid_points": 195000, "cost_floor_cents": 0}])
    with pytest.raises(ValueError):
        pc.publish_version(vid, approved_by=1)


def test_rollback_republishes_old_content():
    v1 = _retail_draft(code="r1", final=180000); pc.publish_version(v1, approved_by=1)
    v2 = _retail_draft(code="r2", final=200000); pc.publish_version(v2, approved_by=1)
    pc.rollback_to(v1, new_version_code="r3-rollback", created_by=1, approved_by=1)
    cat = pc.get_published_catalog("retail", "SV-A")
    assert cat["items"][0]["final_price_cents"] == 180000  # back to v1 content
    assert cat["version"]["version_code"] == "r3-rollback"  # as NEW version (history kept)


# =============================== price_quote ===============================
def test_quote_from_ssot_and_shared_spec():
    """§15.2#1 同一商品在进货页和客户页获得相同算力数量。"""
    pc.publish_version(_retail_draft(), approved_by=1)
    pc.publish_version(_proc_draft(), approved_by=1)
    rq = pq.issue_quote(quote_type="retail", scope_key="SV-A", product_code="credit_basic", buyer_user_id=5)
    proc = pq.issue_procurement_quote(dealer_id=7, product_code="credit_basic")
    assert rq["points_granted"] == proc["points_granted"] == 195000  # shared商品规格算力一致


def test_quote_anti_tamper_and_buyer_bind():
    """§15.2#2 客户最终价格由后端计算,篡改前端金额无效。"""
    pc.publish_version(_retail_draft(), approved_by=1)
    q = pq.issue_quote(quote_type="retail", scope_key="SV-A", product_code="credit_basic", buyer_user_id=5)
    with get_db() as conn:
        cur = conn.cursor()
        with pytest.raises(QuoteError, match="不一致"):
            pq.lock_and_validate(cur, q["quote_id"], buyer_user_id=5, quote_type="retail", expected_final_cents=1)
    with get_db() as conn:
        cur = conn.cursor()
        with pytest.raises(QuoteError, match="买方"):
            pq.lock_and_validate(cur, q["quote_id"], buyer_user_id=999, quote_type="retail")


def test_quote_one_order_only():
    """§15.2#16 同一报价并发两次只产生一个订单。"""
    pc.publish_version(_retail_draft(), approved_by=1)
    q = pq.issue_quote(quote_type="retail", scope_key="SV-A", product_code="credit_basic", buyer_user_id=5)
    with get_db() as conn:
        cur = conn.cursor()
        pq.lock_and_validate(cur, q["quote_id"], buyer_user_id=5, quote_type="retail")
        assert pq.consume_quote(cur, q["quote_id"], "order-1") is True
    with get_db() as conn:
        cur = conn.cursor()
        with pytest.raises(QuoteError, match="已被使用"):
            pq.lock_and_validate(cur, q["quote_id"], buyer_user_id=5, quote_type="retail")


def test_quote_used_order_unique_db_guard():
    """DB 排他:两个报价不能引用同一订单。"""
    pc.publish_version(_retail_draft(), approved_by=1)
    q1 = pq.issue_quote(quote_type="retail", scope_key="SV-A", product_code="credit_basic", buyer_user_id=5)
    q2 = pq.issue_quote(quote_type="retail", scope_key="SV-A", product_code="credit_basic", buyer_user_id=5)
    with get_db() as conn:
        cur = conn.cursor()
        pq.consume_quote(cur, q1["quote_id"], "same-order")
    with get_db() as conn:
        cur = conn.cursor()
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute(
                """UPDATE price_quotes
                   SET status='consumed', used_order_id=%s, consumed_at=NOW()
                   WHERE quote_id=%s""",
                ("same-order", q2["quote_id"]),
            )


def test_quote_expired_rejected():
    """§15.2#7/§9.2.6 报价过期不得续算。"""
    pc.publish_version(_retail_draft(), approved_by=1)
    # 本批 migration 后报价时间锚已由 DB trigger 锁死；用负 TTL 生成天然过期报价，
    # 不再靠测试直改 immutable expires_at。
    q = pq.issue_quote(quote_type="retail", scope_key="SV-A", product_code="credit_basic", buyer_user_id=5, ttl_seconds=-1)
    with get_db() as conn:
        cur = conn.cursor()
        with pytest.raises(QuoteError, match="过期"):
            pq.lock_and_validate(cur, q["quote_id"], buyer_user_id=5, quote_type="retail")


def test_quote_refused_when_no_catalog():
    """§15.2#7 无价目 fail-closed(不回退硬编码)。"""
    with pytest.raises(QuoteError, match="拒绝报价"):
        pq.issue_quote(quote_type="retail", scope_key="SV-NONE", product_code="credit_basic", buyer_user_id=5)


# =============================== channel ===============================
def test_channel_compound_and_beneficiary():
    """§15.2#4/#13 直属链复利 + 只认直属受益人。"""
    ch.create_relationship(buyer_dealer_id=10, upstream_channel_account_id=20, relationship_version="c1", cost_multiplier_bps=12000)
    ch.create_relationship(buyer_dealer_id=20, upstream_channel_account_id=30, relationship_version="c2", cost_multiplier_bps=11000)
    res = ch.resolve_effective_cost_basis(10, 100000)
    # root 100000 → *1.1 (20) → *1.2 (10) : ceil(ceil(100000*1.1)*1.2)=ceil(110000*1.2)=132000
    assert res["effective_cost_cents"] == 132000
    assert res["beneficiary_user_id"] == 20 and res["depth"] == 2  # 直属=20,间接30无收益


def test_channel_cycle_rejected_write_and_runtime():
    """§15.2#18 成环拒绝。"""
    ch.create_relationship(buyer_dealer_id=1, upstream_channel_account_id=2, relationship_version="a")
    ch.create_relationship(buyer_dealer_id=2, upstream_channel_account_id=3, relationship_version="b")
    with pytest.raises(ChannelError, match="成环"):
        ch.create_relationship(buyer_dealer_id=3, upstream_channel_account_id=1, relationship_version="cyc")


def test_channel_self_reference_rejected():
    with pytest.raises(ChannelError, match="自己归属自己"):
        ch.create_relationship(buyer_dealer_id=5, upstream_channel_account_id=5, relationship_version="self")


def test_channel_single_active_and_switch():
    ch.create_relationship(buyer_dealer_id=10, upstream_channel_account_id=20, relationship_version="v1")
    ch.create_relationship(buyer_dealer_id=10, upstream_channel_account_id=30, relationship_version="v2")  # switch
    rel = ch.get_active_relationship(10)
    assert rel["upstream_channel_account_id"] == 30 and rel["relationship_version"] == "v2"


def test_channel_revenue_ledger_idempotent_and_reverse():
    """§15.3 下级进货 = 买方 paid + 渠道收益台账;幂等 + 可冲销。"""
    with get_db() as conn:
        cur = conn.cursor()
        id1 = ch.record_channel_revenue(cur, recharge_order_id="o1", buyer_dealer_id=10, beneficiary_user_id=20,
                                        upstream_cost_basis_cents=100000, buyer_paid_cents=132000)
        id2 = ch.record_channel_revenue(cur, recharge_order_id="o1", buyer_dealer_id=10, beneficiary_user_id=20,
                                        upstream_cost_basis_cents=100000, buyer_paid_cents=132000)
        assert id1 is not None and id2 is None  # 幂等
        cur.execute("SELECT channel_revenue_cents,status FROM channel_revenue_ledger WHERE recharge_order_id='o1'")
        row = cur.fetchone()
        assert row["channel_revenue_cents"] == 32000 and row["status"] == "recorded"
        assert ch.reverse_channel_revenue(cur, "o1") == 1


def test_channel_disabled_flat_by_default():
    """总闸关(默认)=扁平只认第一层:即便有关系,procurement 报价用 platform base。"""
    pc.publish_version(_proc_draft(final=120000), approved_by=1)
    ch.create_relationship(buyer_dealer_id=7, upstream_channel_account_id=8, relationship_version="v1", cost_multiplier_bps=15000)
    # flag off → issue_procurement_quote ignores chain → final == platform base
    assert flags.channel_pricing_enabled() is False
    q = pq.issue_procurement_quote(dealer_id=7, product_code="credit_basic")
    assert q["final_price_cents"] == 120000


# =============================== snapshot separation (P0-2) ===============================
def test_settlement_snapshot_does_not_clobber_pricing_snapshot():
    """P0-2 结算写 settlement_snapshot_jsonb,pricing_snapshot_jsonb 保持不变。"""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""INSERT INTO recharge_orders(id,user_id,amount_cents,base_points,bonus_points,pricing_snapshot_jsonb)
                       VALUES ('ord-s',5,180000,195000,0,%s)""", (json.dumps({"wholesale_cents": 120000, "product_code": "credit_basic"}),))
    # simulate settlement writing the breakdown to the NEW column
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE recharge_orders SET settlement_snapshot_jsonb=%s WHERE id='ord-s'",
                    (json.dumps({"agent_settlement_cents": 90000, "ledger_id": 1}),))
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pricing_snapshot_jsonb, settlement_snapshot_jsonb FROM recharge_orders WHERE id='ord-s'")
        row = cur.fetchone()
        assert row["pricing_snapshot_jsonb"]["wholesale_cents"] == 120000  # 原始定价证据未被覆盖
        assert row["settlement_snapshot_jsonb"]["agent_settlement_cents"] == 90000  # 结算证据分列


# =============================== account codes + flags ===============================
def test_account_codes_random_nonenumerable_stable():
    """§16 Q32 随机不可枚举 + 稳定 + 后端可反查(仅后端)。"""
    sv = ac.get_or_create_service_code(12345)
    assert sv.startswith("SV-") and ac.get_or_create_service_code(12345) == sv  # 稳定
    assert "12345" not in sv  # 非递增 user_id
    chc = ac.get_or_create_channel_code(12345)
    assert chc.startswith("CH-") and ac.resolve_user_by_channel_code(chc) == 12345


def test_flags_default_off():
    assert flags.dual_ssot_enabled() is False
    assert flags.channel_pricing_enabled() is False
    assert flags.quote_required() is False


# =============================== quote consume strengthening (出口审核 #4/#5) ===============================
def test_quote_locks_product_points_and_hash():
    """审核#4/#5:consume 校验除金额外锁 product_code/points,且重算 calculation_hash 比对。"""
    pc.publish_version(_retail_draft(), approved_by=1)
    q = pq.issue_quote(quote_type="retail", scope_key="SV-A", product_code="credit_basic", buyer_user_id=5)
    with get_db() as conn:
        cur = conn.cursor()
        with pytest.raises(QuoteError, match="商品"):
            pq.lock_and_validate(cur, q["quote_id"], buyer_user_id=5, quote_type="retail", expected_product_code="other")
    with get_db() as conn:
        cur = conn.cursor()
        with pytest.raises(QuoteError, match="算力"):
            pq.lock_and_validate(cur, q["quote_id"], buyer_user_id=5, quote_type="retail", expected_points=1)
    # 本批 migration 把纵深防御前移到 DB：报价 immutable 字段连被篡改落库都不允许。
    with pytest.raises(psycopg2.DatabaseError):
        with get_db() as conn:
            conn.cursor().execute(
                "UPDATE price_quotes SET final_price_cents=1 WHERE quote_id=%s", (q["quote_id"],)
            )


def test_channel_revenue_wired_on_settlement(monkeypatch):
    """审核#3:进货结算接线 _record_channel_revenue_if_applicable → 台账入账;退款 reverse。"""
    import config.pricing_ssot_flags as flg
    # 开 CHANNEL flag
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE system_settings SET value='true' WHERE key='CHANNEL_PRICING_ENABLED'")
    flg.invalidate()
    from services import config_epoch; config_epoch.read_config_epoch(force=True)
    try:
        pc.publish_version(_proc_draft(final=120000, points=195000), approved_by=1)
        ch.create_relationship(buyer_dealer_id=10, upstream_channel_account_id=20, relationship_version="cr", cost_multiplier_bps=12000)
        q = pq.issue_procurement_quote(dealer_id=10, product_code="credit_basic")
        assert q["channel_beneficiary_user_id"] == 20  # 直属受益人
        from db.wallet_db import _record_channel_revenue_if_applicable
        with get_db() as conn:
            cur = conn.cursor()
            _record_channel_revenue_if_applicable(cur, {"id": "ord-ch", "price_quote_id": q["quote_id"]})
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT channel_beneficiary_user_id, buyer_paid_cents, upstream_cost_basis_cents, channel_revenue_cents, status FROM channel_revenue_ledger WHERE recharge_order_id='ord-ch'")
            row = cur.fetchone()
            assert row and row["channel_beneficiary_user_id"] == 20
            # 现金锚定 120000;关系系数把到账算力相应降低。
            # 直属卖方根成本 100000，余下 20000 是本跳收益。
            assert row["buyer_paid_cents"] == 120000 and row["upstream_cost_basis_cents"] == 100000
            assert row["channel_revenue_cents"] == 20000 and row["status"] == "recorded"
        # reverse
        with get_db() as conn:
            cur = conn.cursor()
            ch.reverse_channel_revenue(cur, "ord-ch")
            cur.execute("SELECT status FROM channel_revenue_ledger WHERE recharge_order_id='ord-ch'")
            assert cur.fetchone()["status"] == "reversed"
    finally:
        with get_db() as conn:
            conn.cursor().execute("UPDATE system_settings SET value='false' WHERE key='CHANNEL_PRICING_ENABLED'")
        flg.invalidate()


def test_retry_channel_revenue_compensates_record_and_reverse():
    """[v9 · Deploy-CTO NO-GO P1-2] fund_recovery_processor._retry_channel_revenue 幂等补偿【真机器端到端】:
    record → 台账 recorded;reverse → reversed;二者重复调用仍 True(ON CONFLICT DO NOTHING / status='recorded' 幂等)。
    这是渠道收益 fail-open → exactly-once 耐久补偿的真报价机器验证(regression 因缺渠道表只能 monkeypatch _do_record)。"""
    import config.pricing_ssot_flags as flg
    from services.fund_recovery_processor import _retry_channel_revenue
    with get_db() as conn:
        conn.cursor().execute("UPDATE system_settings SET value='true' WHERE key='CHANNEL_PRICING_ENABLED'")
    flg.invalidate()
    from services import config_epoch; config_epoch.read_config_epoch(force=True)
    try:
        pc.publish_version(_proc_draft(final=120000, points=195000), approved_by=1)
        ch.create_relationship(buyer_dealer_id=10, upstream_channel_account_id=20,
                               relationship_version="cr", cost_multiplier_bps=12000)
        q = pq.issue_procurement_quote(dealer_id=10, product_code="credit_basic")
        # seed recharge_orders 绑定该 procurement 报价(补偿重试按 order 反查报价再记账)
        with get_db() as conn:
            cur = conn.cursor()
            _insert_quote_backed_paid_order(cur, q, "ord-retry9")
        # record 补偿(此刻台账尚无该 order 行)· [v10 item3] 返回三态字符串
        assert _retry_channel_revenue({"kind": "record", "ref_key": "ord-retry9",
                                       "payload": {"order_id": "ord-retry9"}}) == "done"
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT status FROM channel_revenue_ledger WHERE recharge_order_id='ord-retry9'")
            assert cur.fetchone()["status"] == "recorded", "🔴 record 补偿必须真记账"
        # record 幂等:再跑仍 done(ON CONFLICT DO NOTHING)
        assert _retry_channel_revenue({"kind": "record", "payload": {"order_id": "ord-retry9"}}) == "done"
        # reverse 补偿
        assert _retry_channel_revenue({"kind": "reverse", "payload": {"order_id": "ord-retry9"}}) == "done"
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT status FROM channel_revenue_ledger WHERE recharge_order_id='ord-retry9'")
            assert cur.fetchone()["status"] == "reversed", "🔴 reverse 补偿必须真冲销"
        # reverse 幂等:已 reversed 再跑仍 done(无 recorded 残留)
        assert _retry_channel_revenue({"kind": "reverse", "payload": {"order_id": "ord-retry9"}}) == "done"
    finally:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE system_settings SET value='false' WHERE key='CHANNEL_PRICING_ENABLED'")
            cur.execute("DELETE FROM recharge_orders WHERE id='ord-retry9'")
        flg.invalidate()


def test_retry_channel_revenue_refund_status_precise_gate():
    """[v9 · 对抗审【两轮】] record 补偿按 refund_status 精确门控(第2轮抓 truthy 过宽 → 收窄):
      - 真退款态('processed')→ 跳过 + 收口(返 True · 不建幽灵 recorded 行);
      - 款未退态('failed'/'rejected'=订单成立)→ 正常补记(返 True · recorded · 渠道收益仍欠不可漏);
      - 在途态('pending')→ requeue(返 False · 不记不收口 · 等退款落终态)。
    删收窄(改回 truthy `if _rs:`)→ 'failed' 订单被误跳过漏记 → 转红。"""
    import config.pricing_ssot_flags as flg
    from services.fund_recovery_processor import _retry_channel_revenue
    with get_db() as conn:
        conn.cursor().execute("UPDATE system_settings SET value='true' WHERE key='CHANNEL_PRICING_ENABLED'")
    flg.invalidate()
    from services import config_epoch; config_epoch.read_config_epoch(force=True)

    def _seed_order(oid, refund_status):
        # 目录 + 渠道关系只建一次(见 try 首行);此处仅发报价 + 插订单
        q = pq.issue_procurement_quote(dealer_id=10, product_code="credit_basic")
        with get_db() as conn:
            cur = conn.cursor()
            _insert_quote_backed_paid_order(cur, q, oid, refund_status=refund_status)

    def _ledger_status(oid):
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT status FROM channel_revenue_ledger WHERE recharge_order_id=%s", (oid,))
            r = cur.fetchone()
            return r["status"] if r else None

    try:
        pc.publish_version(_proc_draft(final=120000, points=195000), approved_by=1)
        ch.create_relationship(buyer_dealer_id=10, upstream_channel_account_id=20,
                               relationship_version="cr", cost_multiplier_bps=12000)
        # A) 真退款('processed')→ 跳过 + 收口(done)· 不建幽灵行
        _seed_order("ord-rs-processed", "processed")
        assert _retry_channel_revenue({"kind": "record", "payload": {"order_id": "ord-rs-processed"}}) == "done"
        assert _ledger_status("ord-rs-processed") is None, "🔴 真退款订单绝不补记渠道收益(无幽灵 recorded 行)"

        # B) 款未退('failed' = 退款失败·订单仍 paid)→ 正常补记(done)· 渠道收益仍欠不可漏
        _seed_order("ord-rs-failed", "failed")
        assert _retry_channel_revenue({"kind": "record", "payload": {"order_id": "ord-rs-failed"}}) == "done"
        assert _ledger_status("ord-rs-failed") == "recorded", "🔴 退款失败(款未退)订单必须正常补记(truthy 过宽会漏)"

        # C) 在途('pending')→ [v10 item3] defer(只延后不累计重试)· 不记不收口 · 等退款落终态
        _seed_order("ord-rs-pending", "pending")
        assert _retry_channel_revenue({"kind": "record", "payload": {"order_id": "ord-rs-pending"}}) == "defer", \
            "🔴 退款在途 → defer 等终态(不过早误判·不累计重试)"
        assert _ledger_status("ord-rs-pending") is None, "在途不记账"
    finally:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE system_settings SET value='false' WHERE key='CHANNEL_PRICING_ENABLED'")
            cur.execute("DELETE FROM recharge_orders WHERE id IN ('ord-rs-processed','ord-rs-failed','ord-rs-pending')")
        flg.invalidate()


def test_record_channel_revenue_ignores_realtime_flag():
    """[v10 item4] 🔴 结算不受【回调时刻实时 flag】影响:报价生成时 CHANNEL 开→下单绑定 procurement 报价快照,
    之后 flag 关(模拟回调前被关/缓存失效)→ _record_channel_revenue_if_applicable 仍必须按快照记账。
    删"去 flag 门控"(改回 flag 关就 return)→ 不记账 → 转红。"""
    import config.pricing_ssot_flags as flg
    from db.wallet_db import _record_channel_revenue_if_applicable
    # 1) flag 开 · 生成 procurement 报价(报价生成本就需 flag)
    with get_db() as conn:
        conn.cursor().execute("UPDATE system_settings SET value='true' WHERE key='CHANNEL_PRICING_ENABLED'")
    flg.invalidate()
    from services import config_epoch; config_epoch.read_config_epoch(force=True)
    pc.publish_version(_proc_draft(final=120000, points=195000), approved_by=1)
    ch.create_relationship(buyer_dealer_id=10, upstream_channel_account_id=20, relationship_version="cr", cost_multiplier_bps=12000)
    q = pq.issue_procurement_quote(dealer_id=10, product_code="credit_basic")
    with get_db() as conn:
        cur = conn.cursor()
        _insert_quote_backed_paid_order(cur, q, "ord-flagoff")
    # 2) 🔴 回调前 flag 关(+ 失效缓存)· 结算义务不受影响
    with get_db() as conn:
        conn.cursor().execute("UPDATE system_settings SET value='false' WHERE key='CHANNEL_PRICING_ENABLED'")
    flg.invalidate()
    config_epoch.read_config_epoch(force=True)
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM recharge_orders WHERE id='ord-flagoff'")
            order = dict(cur.fetchone())
            _record_channel_revenue_if_applicable(cur, order)   # flag 关也必须记账
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT status FROM channel_revenue_ledger WHERE recharge_order_id='ord-flagoff'")
            row = cur.fetchone()
            assert row and row["status"] == "recorded", "🔴 flag 关也必须按 procurement 报价快照记渠道收益"
    finally:
        with get_db() as conn:
            conn.cursor().execute("DELETE FROM recharge_orders WHERE id='ord-flagoff'")
        flg.invalidate()


def test_canonical_reverse_on_refund_gated_by_stage():
    """[v10 item6] 🔴 canonical reverse_channel_revenue_on_refund 按 refund_status 阶段门控:
    已生效(processed/completed/pending_review)→ 冲销;在途(pending/approved)/款未退 → 不提前冲销。
    删阶段门控(无条件冲) → pending 也被冲 → 转红。"""
    import config.pricing_ssot_flags as flg
    from services.channel_revenue_lifecycle import reverse_channel_revenue_on_refund
    with get_db() as conn:
        conn.cursor().execute("UPDATE system_settings SET value='true' WHERE key='CHANNEL_PRICING_ENABLED'")
    flg.invalidate()
    from services import config_epoch; config_epoch.read_config_epoch(force=True)
    pc.publish_version(_proc_draft(final=120000, points=195000), approved_by=1)
    ch.create_relationship(buyer_dealer_id=10, upstream_channel_account_id=20, relationship_version="cr", cost_multiplier_bps=12000)

    def _seed_recorded(oid, refund_status, *, cd_evidence=False):
        q = pq.issue_procurement_quote(dealer_id=10, product_code="credit_basic")
        from db.wallet_db import _do_record_channel_revenue
        with get_db() as conn:
            cur = conn.cursor()
            _insert_quote_backed_paid_order(
                cur, q, oid, refund_status=refund_status, refund_completed=cd_evidence
            )
            cur.execute("SELECT * FROM recharge_orders WHERE id=%s", (oid,))
            _do_record_channel_revenue(cur, dict(cur.fetchone()))

    def _ledger_status(oid):
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT status FROM channel_revenue_ledger WHERE recharge_order_id=%s", (oid,))
            r = cur.fetchone(); return r["status"] if r else None

    try:
        # A) processed → 冲销
        _seed_recorded("ord-rev-proc", "processed")
        with get_db() as conn:
            cur = conn.cursor()
            res = reverse_channel_revenue_on_refund(cur, "ord-rev-proc")
        assert res["reversed"] is True and _ledger_status("ord-rev-proc") == "reversed", "🔴 processed 必冲销"
        # B) pending(在途)→ 不冲销
        _seed_recorded("ord-rev-pend", "pending")
        with get_db() as conn:
            cur = conn.cursor()
            res = reverse_channel_revenue_on_refund(cur, "ord-rev-pend")
        assert res["reversed"] is False and _ledger_status("ord-rev-pend") == "recorded", "🔴 在途 pending 绝不提前冲销"
        # C) pending_review + CD 证据(现金已退)→ 冲销
        _seed_recorded("ord-rev-pr", "pending_review", cd_evidence=True)
        with get_db() as conn:
            cur = conn.cursor()
            reverse_channel_revenue_on_refund(cur, "ord-rev-pr")
        assert _ledger_status("ord-rev-pr") == "reversed", "🔴 pending_review(现金已退)必冲销"
        # D) pending_review 只是状态字符串、无 CD/人工凭证 → canonical 也必须 fail-closed
        _seed_recorded("ord-rev-pr-no-proof", "pending_review")
        with get_db() as conn:
            cur = conn.cursor()
            with pytest.raises(ValueError, match="缺少可信 CD"):
                reverse_channel_revenue_on_refund(cur, "ord-rev-pr-no-proof")
        assert _ledger_status("ord-rev-pr-no-proof") == "recorded", \
            "🔴 pending_review 无证据不得冲销渠道收益"
    finally:
        with get_db() as conn:
            conn.cursor().execute("UPDATE system_settings SET value='false' WHERE key='CHANNEL_PRICING_ENABLED'")
            conn.cursor().execute("DELETE FROM recharge_orders WHERE id IN ('ord-rev-proc','ord-rev-pend','ord-rev-pr','ord-rev-pr-no-proof')")
        flg.invalidate()


def test_xunhupay_rd_inflight_no_reverse_cd_reverses_with_rd_to_cd_migration():
    """[v11 F1] 🔴 虎皮椒 RD(退款中)=IN_FLIGHT 不冲销 · CD(已退款)=REFUND_EFFECTIVE 冲销 · 覆盖 RD→CD 迁移。
    退回旧口径(RD/CD 都写 pending_review → RD 被提前冲销)→ RD 台账 recorded 断言转红。"""
    import config.pricing_ssot_flags as flg
    from api.wallet_api import _flag_channel_refund
    from db.wallet_db import _do_record_channel_revenue
    with get_db() as conn:
        conn.cursor().execute("UPDATE system_settings SET value='true' WHERE key='CHANNEL_PRICING_ENABLED'")
    flg.invalidate()
    from services import config_epoch; config_epoch.read_config_epoch(force=True)
    pc.publish_version(_proc_draft(final=120000, points=195000), approved_by=1)
    ch.create_relationship(buyer_dealer_id=10, upstream_channel_account_id=20, relationship_version="cr", cost_multiplier_bps=12000)

    def _seed_recorded(oid):
        q = pq.issue_procurement_quote(dealer_id=10, product_code="credit_basic")
        with get_db() as conn:
            cur = conn.cursor()
            _insert_quote_backed_paid_order(cur, q, oid)
            cur.execute("SELECT * FROM recharge_orders WHERE id=%s", (oid,))
            _do_record_channel_revenue(cur, dict(cur.fetchone()))

    def _both(oid):
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT refund_status FROM recharge_orders WHERE id=%s", (oid,))
            rs = cur.fetchone()["refund_status"]
            cur.execute("SELECT status FROM channel_revenue_ledger WHERE recharge_order_id=%s", (oid,))
            lr = cur.fetchone()
            return rs, (lr["status"] if lr else None)

    try:
        # 1) RD(退款中)→ pending(IN_FLIGHT)· 台账仍 recorded(不提前冲)
        _seed_recorded("ord-xh-rdcd")
        assert _flag_channel_refund("ord-xh-rdcd", "RD", "12.00", channel="xunhupay") is True
        rs, ls = _both("ord-xh-rdcd")
        assert rs == "channel_refunding", f"🔴 RD 必落 IN_FLIGHT(channel_refunding·非复用平台 pending)· 实际 {rs}"
        assert ls == "recorded", f"🔴 RD(退款中)绝不提前冲销渠道收益 · 实际 {ls}"
        # 2) RD→CD 迁移:CD 从 pending 推进到 pending_review 并冲销
        assert _flag_channel_refund(
            "ord-xh-rdcd", "CD", "12.00", channel="xunhupay",
            provider_refund_id="XHP-RDCD-1",
        ) is True
        rs2, ls2 = _both("ord-xh-rdcd")
        assert rs2 == "pending_review", f"🔴 RD→CD 必推进到 pending_review · 实际 {rs2}"
        assert ls2 == "reversed", f"🔴 CD(已退款)必冲销渠道收益 · 实际 {ls2}"
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT refund_completed_at FROM recharge_orders WHERE id='ord-xh-rdcd'")
            assert cur.fetchone()["refund_completed_at"] is not None, "🔴 验签 CD 必持久化可信退款证据"
        # 3) 全新订单直接 CD → 直接冲销(CD 才冲)
        _seed_recorded("ord-xh-cd")
        _flag_channel_refund(
            "ord-xh-cd", "CD", "12.00", channel="xunhupay",
            provider_refund_id="XHP-CD-1",
        )
        rs3, ls3 = _both("ord-xh-cd")
        assert rs3 == "pending_review" and ls3 == "reversed", "🔴 直接 CD 必冲销"
    finally:
        with get_db() as conn:
            conn.cursor().execute("UPDATE system_settings SET value='false' WHERE key='CHANNEL_PRICING_ENABLED'")
            conn.cursor().execute("DELETE FROM recharge_orders WHERE id IN ('ord-xh-rdcd','ord-xh-cd')")
        flg.invalidate()


def test_xunhupay_refund_idempotent_and_ordering_matrix():
    """[v11 P1-1] 🔴 退款状态机幂等 + 乱序矩阵:重复 RD / 重复 CD / CD→RD 乱序 / 退款失败保收益。
    冲销恰一次且【不可恢复已冲销收益】· 退款失败(REVENUE_OWED)保原收益不冲。"""
    import config.pricing_ssot_flags as flg
    from api.wallet_api import _flag_channel_refund
    from services.channel_revenue_lifecycle import reverse_channel_revenue_on_refund
    from db.wallet_db import _do_record_channel_revenue
    with get_db() as conn:
        conn.cursor().execute("UPDATE system_settings SET value='true' WHERE key='CHANNEL_PRICING_ENABLED'")
    flg.invalidate()
    from services import config_epoch; config_epoch.read_config_epoch(force=True)
    pc.publish_version(_proc_draft(final=120000, points=195000), approved_by=1)
    ch.create_relationship(buyer_dealer_id=10, upstream_channel_account_id=20, relationship_version="cr", cost_multiplier_bps=12000)

    def _seed_recorded(oid, refund_status=None):
        q = pq.issue_procurement_quote(dealer_id=10, product_code="credit_basic")
        with get_db() as conn:
            cur = conn.cursor()
            _insert_quote_backed_paid_order(cur, q, oid, refund_status=refund_status)
            cur.execute("SELECT * FROM recharge_orders WHERE id=%s", (oid,))
            _do_record_channel_revenue(cur, dict(cur.fetchone()))

    def _both(oid):
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT refund_status FROM recharge_orders WHERE id=%s", (oid,))
            rs = cur.fetchone()["refund_status"]
            cur.execute("SELECT status FROM channel_revenue_ledger WHERE recharge_order_id=%s", (oid,))
            lr = cur.fetchone()
            return rs, (lr["status"] if lr else None)

    try:
        # A) 重复 RD:两次 RD → 恒 pending · 台账恒 recorded(不冲)
        _seed_recorded("ord-mx-a")
        _flag_channel_refund("ord-mx-a", "RD", "1", channel="xunhupay")
        _flag_channel_refund("ord-mx-a", "RD", "1", channel="xunhupay")
        assert _both("ord-mx-a") == ("channel_refunding", "recorded"), "🔴 重复 RD 幂等(channel_refunding)· 不冲销"
        # RD→CD:冲销一次
        _flag_channel_refund(
            "ord-mx-a", "CD", "1", channel="xunhupay",
            provider_refund_id="XHP-MX-A",
        )
        assert _both("ord-mx-a") == ("pending_review", "reversed"), "🔴 CD 冲销一次"
        # 重复 CD(幂等):不二次冲 · 收益不恢复
        _flag_channel_refund(
            "ord-mx-a", "CD", "1", channel="xunhupay",
            provider_refund_id="XHP-MX-A",
        )
        assert _both("ord-mx-a") == ("pending_review", "reversed"), "🔴 重复 CD 幂等 · 收益不恢复为 recorded"
        # 乱序:CD 后到的 RD 不得降级/恢复已冲销收益
        _flag_channel_refund("ord-mx-a", "RD", "1", channel="xunhupay")
        assert _both("ord-mx-a") == ("pending_review", "reversed"), "🔴 乱序迟到 RD 不得把已生效降级/恢复收益"

        # B) 退款失败保收益:REVENUE_OWED(failed)→ canonical 不冲销 · 台账仍 recorded
        _seed_recorded("ord-mx-b", refund_status="failed")
        with get_db() as conn:
            cur = conn.cursor()
            res = reverse_channel_revenue_on_refund(cur, "ord-mx-b")
        assert res["reversed"] is False, "🔴 退款失败(REVENUE_OWED)绝不冲销"
        assert _both("ord-mx-b")[1] == "recorded", "🔴 退款失败必保原渠道收益(recorded)"

        # C) RD→UD:退出在途态到 failed,不冲渠道收益;重复 UD 幂等。
        _seed_recorded("ord-mx-c")
        assert _flag_channel_refund("ord-mx-c", "RD", "1", channel="xunhupay") is True
        assert _flag_channel_refund("ord-mx-c", "UD", "1", channel="xunhupay") is True
        assert _both("ord-mx-c") == ("failed", "recorded"), "🔴 UD 必退出在途且保留收益"
        assert _flag_channel_refund("ord-mx-c", "UD", "1", channel="xunhupay") is True

        # 未知订单绝不能 ACK success。
        assert _flag_channel_refund("ord-does-not-exist", "CD", "1", channel="xunhupay") is False
    finally:
        with get_db() as conn:
            conn.cursor().execute("UPDATE system_settings SET value='false' WHERE key='CHANNEL_PRICING_ENABLED'")
            conn.cursor().execute("DELETE FROM recharge_orders WHERE id IN ('ord-mx-a','ord-mx-b','ord-mx-c')")
        flg.invalidate()


def test_channel_refund_transition_table_is_single_terminal_safe():
    from services.channel_revenue_lifecycle import channel_refund_transition

    assert channel_refund_transition(None, "RD")["target"] == "channel_refunding"
    assert channel_refund_transition("channel_refunding", "UD")["target"] == "failed"
    assert channel_refund_transition("pending", "UD")["ack"] == "failclosed"
    assert channel_refund_transition("approved", "UD")["ack"] == "failclosed"
    assert channel_refund_transition("channel_refunding", "CD")["target"] == "pending_review"
    assert channel_refund_transition("pending_review", "CD")["action"] == "noop"
    assert channel_refund_transition("pending_review", "CD")["reverse"] is True
    assert channel_refund_transition("pending_review", "UD")["ack"] == "failclosed"
    assert channel_refund_transition("mystery", "CD")["ack"] == "failclosed"


def test_xunhupay_refund_callback_fail_closed_on_persist_failure(monkeypatch):
    """[v11 F2] 🔴 冲销/写库未耐久落库 → _flag_channel_refund 返 False(fail-closed)· 整事务回滚(refund_status 不半落)。
    调用方据此回非 success 让渠道重试。改回吞异常返 True(fail-open)→ 转红。"""
    import services.channel_revenue_lifecycle as L
    from api.wallet_api import _flag_channel_refund

    def _boom(cur, oid):
        raise RuntimeError("simulated persist failure")
    monkeypatch.setattr(L, "reverse_channel_revenue_on_refund", _boom)
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO recharge_orders (id,user_id,amount_cents,base_points,bonus_points,payment_method,payment_status) "
            "VALUES ('ord-failclose',10,1000,0,0,'wechat','paid') ON CONFLICT (id) DO NOTHING")
    try:
        ok = _flag_channel_refund(
            "ord-failclose", "CD", "10.00", channel="xunhupay",
            provider_refund_id="XHP-FAILCLOSE",
        )
        assert ok is False, "🔴 持久化失败必返 False(fail-closed · 让渠道重试)"
        with get_db() as conn:
            cur = conn.cursor(); cur.execute("SELECT refund_status FROM recharge_orders WHERE id='ord-failclose'")
            assert cur.fetchone()["refund_status"] is None, "🔴 fail-closed 必整事务回滚(refund_status 不半落)"
    finally:
        with get_db() as conn:
            conn.cursor().execute("DELETE FROM recharge_orders WHERE id='ord-failclose'")


# =============================== order↔quote cutover (闭环) ===============================
def test_order_create_atomically_consumes_quote():
    """切流闭环:create_recharge_order 传 price_quote_id → 订单创建 + 报价原子消费;
    同报价再下单 → 抛异常 + 不留孤儿订单(原子回滚)。"""
    from db.wallet_db import create_recharge_order
    from db.connection import get_db
    pc.publish_version(_retail_draft(), approved_by=1)
    q = pq.issue_quote(quote_type="retail", scope_key="SV-A", product_code="credit_basic", buyer_user_id=5)
    order = create_recharge_order(
        user_id=5, order_id="ord-cut-1", amount_cents=q["final_price_cents"],
        base_points=q["points_granted"], bonus_points=q["bonus_points"], payment_method="wechat",
        price_quote_id=q["quote_id"], quote_type="retail", pricing_catalog_version=q["catalog_version"],
    )
    assert order["id"] == "ord-cut-1" and order["price_quote_id"] == q["quote_id"]
    # quote now consumed, bound to the order
    q2 = pq.get_quote(q["quote_id"])
    assert q2["status"] == "consumed" and q2["used_order_id"] == "ord-cut-1"
    # replay same quote → reject + NO orphan order
    with pytest.raises(Exception):
        create_recharge_order(
            user_id=5, order_id="ord-cut-2", amount_cents=q["final_price_cents"],
            base_points=q["points_granted"], bonus_points=q["bonus_points"], payment_method="wechat",
            price_quote_id=q["quote_id"], quote_type="retail",
        )
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM recharge_orders WHERE id='ord-cut-2'")
        assert cur.fetchone()["n"] == 0  # 原子回滚:无孤儿订单


# =============================== P0-14 deadlock regression (出口审核 BLOCKING) ===============================
def test_p0_14_pending_review_can_complete_and_initiate():
    """出口审核阻断项:pending_review 不得成为退款死角。
    (a) 退款工单完成 guard(真实 complete_refund_work_order + save_execution_result)必须【含 pending_review】能前进,
        且必带 RETURNING 命中校验(P1-3 防假完成);
    (b) _v35_admin_refund 与 check_refund_eligibility 白名单必须含 pending_review。
    [v11 集中审核修] 原 (a) 手抄内联旧 guard SQL 自跑 → 真 guard 改动测不到(假绿)· 改为 inspect.getsource 驱动真实函数。"""
    import inspect
    # (a) 驱动【真实】共享完成 guard:pending_review 只有具备 CD/人工证据才可完成；
    #     两个入口都必须复用它，并保留 RETURNING 命中校验(P1-3)。
    import api.refund_work_order_api as rwa
    import db.refund_work_order_db as rwd
    guard_src = inspect.getsource(rwd.assert_refund_completion_allowed_cur)
    evidence_src = inspect.getsource(rwd.assert_external_refund_evidence_cur)
    assert "assert_external_refund_evidence_cur" in guard_src
    assert 'status == "pending_review"' in evidence_src
    assert "refund_completed_at" in evidence_src and "has_manual_evidence" in evidence_src, \
        "pending_review 必须由可信 CD 回调时间或人工实际退款凭证支撑"
    assert 'status == "channel_refunding"' in evidence_src and "has_manual_evidence" in evidence_src, \
        "channel_refunding 无实际退款凭证时必须拒绝提前反向内账"
    for _fn in (rwa.complete_refund_work_order, rwd.save_execution_result):
        _s = inspect.getsource(_fn)
        assert "assert_refund_completion_allowed_cur" in _s, \
            f"{_fn.__name__} 必须复用退款证据共享守卫"
        assert "RETURNING id" in _s and "fetchone()" in _s and "is None" in _s, \
            f"{_fn.__name__} 必须 RETURNING 命中校验(P1-3 防 failed/rejected 假完成)"
    # (b) 断言两处 Python 白名单确实含 pending_review(锁防回退)
    import api.wallet_api as wa
    src_init = inspect.getsource(wa._v35_admin_refund)
    assert '"pending_review"' in src_init or "'pending_review'" in src_init, "initiate 白名单缺 pending_review"
    src_elig = inspect.getsource(wa.check_refund_eligibility)
    assert "pending_review" in src_elig, "eligibility 白名单缺 pending_review"


def test_p0_2_refund_metadata_uses_settlement_snapshot_sql():
    """P0-2 真回归:退款元信息 merge SQL(admin_complete_refund/_handle_v35_factory_refund 内联)
    必须写 settlement_snapshot_jsonb,pricing_snapshot_jsonb 保持不变。驱动真实 SQL 语句本身。"""
    import json as _json
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""INSERT INTO recharge_orders(id,user_id,amount_cents,base_points,bonus_points,payment_status,pricing_snapshot_jsonb)
                       VALUES ('ord-rf',1,10000,130,0,'paid',%s)""", (_json.dumps({"wholesale_cents": 8000, "product_code": "credit_basic"}),))
    # 驱动 wallet_api.py:1841 的真实退款元信息 merge(verbatim)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""UPDATE recharge_orders
                       SET refund_status='completed',
                           settlement_snapshot_jsonb = COALESCE(settlement_snapshot_jsonb,'{}'::jsonb)
                               || jsonb_build_object('refund_completed_at', NOW()::text, 'refund_complete_note', %s)
                       WHERE id='ord-rf'""", ("test-note",))
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pricing_snapshot_jsonb, settlement_snapshot_jsonb FROM recharge_orders WHERE id='ord-rf'")
        row = cur.fetchone()
        # 原始定价证据 byte-stable(退款没往里写)
        assert row["pricing_snapshot_jsonb"] == {"wholesale_cents": 8000, "product_code": "credit_basic"}
        assert "refund_completed_at" not in (row["pricing_snapshot_jsonb"] or {})
        # 退款证据落在 settlement_snapshot
        assert row["settlement_snapshot_jsonb"]["refund_complete_note"] == "test-note"


# =============================== review-hardening regression ===============================
def test_idempotent_requote_after_consume_no_500():
    """审核P1:同 idem key 在报价被消费/过期后再报价不再 500(UNIQUE 索引已改非唯一)。"""
    pc.publish_version(_retail_draft(), approved_by=1)
    q1 = pq.issue_quote(quote_type="retail", scope_key="SV-A", product_code="credit_basic",
                        buyer_user_id=5, idempotency_key="dedupe-k")
    # same key + same product while issued → dedup returns same quote
    q1b = pq.issue_quote(quote_type="retail", scope_key="SV-A", product_code="credit_basic",
                         buyer_user_id=5, idempotency_key="dedupe-k")
    assert q1b["quote_id"] == q1["quote_id"]
    # consume it, then re-quote same key → must NOT raise (fresh quote issued)
    with get_db() as conn:
        cur = conn.cursor()
        pq.lock_and_validate(cur, q1["quote_id"], buyer_user_id=5, quote_type="retail")
        pq.consume_quote(cur, q1["quote_id"], "ord-idem")
    q2 = pq.issue_quote(quote_type="retail", scope_key="SV-A", product_code="credit_basic",
                        buyer_user_id=5, idempotency_key="dedupe-k")
    assert q2["quote_id"] != q1["quote_id"] and q2["status"] == "issued"


def test_channel_multiplier_and_conservation_checks():
    """审核P2:直接 INSERT 零系数 + 破坏守恒恒等式都被 DB CHECK 拒绝。"""
    with get_db() as conn:
        cur = conn.cursor()
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("""INSERT INTO channel_pricing_relationships(buyer_dealer_id,upstream_channel_account_id,relationship_version,cost_multiplier_bps)
                           VALUES (1,2,'v',0)""")
    with get_db() as conn:
        cur = conn.cursor()
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("""INSERT INTO channel_revenue_ledger(channel_beneficiary_user_id,buyer_dealer_id,recharge_order_id,upstream_cost_basis_cents,buyer_paid_cents,channel_revenue_cents)
                           VALUES (20,10,'bad-ord',1000,1500,9999)""")  # 9999 != 1500-1000


def test_create_draft_always_computes_final():
    """审核P2:草稿 final 一律后端算(base×mult),忽略 caller 传入的错 final。"""
    vid = pc.create_draft_version(catalog_type="procurement", scope_key="PLATFORM_BASE", version_code="p-final",
        entries=[{"product_code": "credit_basic", "base_price_cents": 10000, "multiplier_bps": 10000,
                  "final_price_cents": 1, "paid_points": 195000}])  # caller lies final=1
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT final_price_cents FROM pricing_catalog_entries WHERE version_id=%s", (vid,))
        assert cur.fetchone()["final_price_cents"] == 10000  # base×1.0, not the bogus 1


def test_channel_depth_cap_on_create():
    """审核P3:超深关系写入被拒(否则报价期永久失败)。"""
    for i in range(1, 11):  # chain 1->2->...->11 = depth 10 (MAX)
        ch.create_relationship(buyer_dealer_id=i, upstream_channel_account_id=i + 1, relationship_version=f"d{i}")
    with pytest.raises(ChannelError, match=r"超过(?:技术上限| 10 层)"):
        ch.create_relationship(buyer_dealer_id=11, upstream_channel_account_id=12, relationship_version="d11")


def test_quote_int_overflow_guard():
    """审核P2:超大 quantity 导致 final 溢出 INTEGER 前 fail-closed(不落 DB 500)。"""
    pc.publish_version(_retail_draft(final=180000, floor=100000), approved_by=1)
    with pytest.raises(QuoteError, match="上限"):
        pq.issue_quote(quote_type="retail", scope_key="SV-A", product_code="credit_basic",
                       buyer_user_id=5, quantity=100000)  # 180000*100000 = 1.8e10 > int32


def test_order_create_rejects_tampered_amount():
    """切流:前端传的金额与报价不一致 → 事务内校验拒绝 + 回滚。"""
    from db.wallet_db import create_recharge_order
    from db.connection import get_db
    pc.publish_version(_retail_draft(), approved_by=1)
    q = pq.issue_quote(quote_type="retail", scope_key="SV-A", product_code="credit_basic", buyer_user_id=5)
    with pytest.raises(Exception):
        create_recharge_order(
            user_id=5, order_id="ord-tamper", amount_cents=1,  # 篡改金额
            base_points=q["points_granted"], bonus_points=0, payment_method="wechat",
            price_quote_id=q["quote_id"], quote_type="retail",
        )
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM recharge_orders WHERE id='ord-tamper'")
        assert cur.fetchone()["n"] == 0
        # quote 未被消费(校验先失败)
        assert pq.get_quote(q["quote_id"])["status"] == "issued"


# =============================== 残留清理(出口审核收口后) ===============================
def _enable_channel():
    with get_db() as conn:
        conn.cursor().execute("UPDATE system_settings SET value='true' WHERE key='CHANNEL_PRICING_ENABLED'")
    flags.invalidate()
    from services import config_epoch
    config_epoch.read_config_epoch(force=True)


def test_residual1_channel_revenue_pins_upstream_cost_no_drift():
    """残留①:进货报价钉死直属上游有效成本 → 结算读钉死值,期间改渠道系数不漂移。
    链 30←(11000)←20←(12000)←10:现金始终 120000，到账算力 147727，
    上游(20)钉死成本 100000。改 20←30 系数后结算仍按原快照收益 20000。"""
    _enable_channel()
    try:
        pc.publish_version(_proc_draft(final=120000, points=195000), approved_by=1)
        ch.create_relationship(buyer_dealer_id=20, upstream_channel_account_id=30, relationship_version="u", cost_multiplier_bps=11000)
        ch.create_relationship(buyer_dealer_id=10, upstream_channel_account_id=20, relationship_version="d", cost_multiplier_bps=12000)
        q = pq.issue_procurement_quote(dealer_id=10, product_code="credit_basic")
        assert q["final_price_cents"] == 120000
        assert q["points_granted"] == 147727
        assert q["upstream_cost_basis_cents"] == 100000  # 报价那刻钉死
        # 报价后有人改上游系数(漂移源)
        with get_db() as conn:
            conn.cursor().execute("UPDATE channel_pricing_relationships SET cost_multiplier_bps=13000 WHERE buyer_dealer_id=20")
        with get_db() as conn:
            cur = conn.cursor()
            from db.wallet_db import _record_channel_revenue_if_applicable
            _record_channel_revenue_if_applicable(cur, {"id": "ord-drift", "price_quote_id": q["quote_id"]})
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT upstream_cost_basis_cents, buyer_paid_cents, channel_revenue_cents FROM channel_revenue_ledger WHERE recharge_order_id='ord-drift'")
            row = cur.fetchone()
            assert row["upstream_cost_basis_cents"] == 100000
            assert row["buyer_paid_cents"] == 120000
            assert row["channel_revenue_cents"] == 20000
    finally:
        with get_db() as conn:
            conn.cursor().execute("UPDATE system_settings SET value='false' WHERE key='CHANNEL_PRICING_ENABLED'")
        flags.invalidate()


def test_residual2_order_rejects_quote_product_mismatch():
    """残留②:订单 sku 反查 template_code 与报价 product_code 不一致 → 拒绝 + 回滚(拿 A 报价买 B 商品)。"""
    from db.wallet_db import create_recharge_order
    pc.publish_version(_retail_draft(), approved_by=1)
    q = pq.issue_quote(quote_type="retail", scope_key="SV-A", product_code="credit_basic", buyer_user_id=5)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""INSERT INTO sku_templates(template_code,sku_type,default_name,points_granted,wholesale_cents)
                       VALUES ('credit_other','credit_pack','别的包',195000,120000)
                       ON CONFLICT (template_code) DO NOTHING RETURNING id""")
        r = cur.fetchone()
        other_id = r["id"] if r else None
    if other_id is None:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM sku_templates WHERE template_code='credit_other'")
            other_id = cur.fetchone()["id"]
    # 金额/算力都对(只商品不对)→ 必须命中商品校验而非金额/算力
    with pytest.raises(Exception, match="商品"):
        create_recharge_order(
            user_id=5, order_id="ord-pmm", amount_cents=q["final_price_cents"],
            base_points=q["points_granted"], bonus_points=q["bonus_points"], payment_method="wechat",
            sku_template_id=other_id, price_quote_id=q["quote_id"], quote_type="retail",
        )
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM recharge_orders WHERE id='ord-pmm'")
        assert cur.fetchone()["n"] == 0
        assert pq.get_quote(q["quote_id"])["status"] == "issued"  # 未消费


def test_residual4_eligibility_requires_external_refund_evidence():
    """残留④(行为化,非纯源码 tripwire):真调 check_refund_eligibility async handler。
        pending_review + 可信 CD 证据必须越过第一道 gate；无凭证 channel_refunding 与 processed 仍被挡。"""
    import asyncio
    import types
    from api.wallet_api import check_refund_eligibility
    req = types.SimpleNamespace(state=types.SimpleNamespace(user={"is_admin": True, "user_id": 1}),
                                client=types.SimpleNamespace(host="127.0.0.1"))
    with get_db() as conn:
        cur = conn.cursor()
        # pending_review 单:故意 commission_version='legacy' 让第二道 gate 兜底 → 若被第一道挡会返证据错误
        cur.execute("""INSERT INTO recharge_orders(id,user_id,amount_cents,base_points,bonus_points,payment_status,paid_at,refund_status,refund_completed_at,commission_version)
                       VALUES ('ord-elig-pr',1,10000,130,0,'paid',NOW(),'pending_review',NOW(),'legacy')""")
        cur.execute("""INSERT INTO recharge_orders(id,user_id,amount_cents,base_points,bonus_points,payment_status,paid_at,refund_status,commission_version)
                       VALUES ('ord-elig-proc',1,10000,130,0,'paid',NOW(),'processed','legacy')""")
        cur.execute("""INSERT INTO recharge_orders(id,user_id,amount_cents,base_points,bonus_points,payment_status,paid_at,refund_status,commission_version)
                       VALUES ('ord-elig-rd',1,10000,130,0,'paid',NOW(),'channel_refunding','legacy')""")
    r_pr = asyncio.run(check_refund_eligibility("ord-elig-pr", req))
    assert r_pr["eligible"] is False and "老订单" in r_pr["reason"]  # 越过 refund_status gate,到 legacy gate
    r_proc = asyncio.run(check_refund_eligibility("ord-elig-proc", req))
    assert r_proc["eligible"] is False and "已申请退款" in r_proc["reason"]  # 非白名单仍被第一道挡
    r_rd = asyncio.run(check_refund_eligibility("ord-elig-rd", req))
    assert r_rd["eligible"] is False and "无实际退款凭证" in r_rd["reason"], "🔴 RD 在途必须锁住 admin 内账反向"


def test_residual5_refund_updates_settlement_snapshot_not_pricing_snapshot_source():
    """残留⑤(源码 tripwire,补 verbatim 副本):两条真实退款路径的 UPDATE 必须写 settlement_snapshot_jsonb,
    绝不把退款元信息写回不可变的 pricing_snapshot_jsonb(§9.3 write-once)。防未来回退。"""
    import inspect
    import api.wallet_api as wa
    import api.referral_api as ra
    for fn in (wa.admin_complete_refund, ra._handle_v35_factory_refund):
        src = inspect.getsource(fn)
        assert "settlement_snapshot_jsonb = COALESCE(settlement_snapshot_jsonb" in src, f"{fn.__name__} 退款未写 settlement_snapshot"
        assert "pricing_snapshot_jsonb = COALESCE(pricing_snapshot_jsonb" not in src, f"{fn.__name__} 回退写了 pricing_snapshot"


def test_unbound_customer_uses_configured_platform_direct_service(monkeypatch):
    from api import pricing_ssot_api

    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", "10")
    platform_scope = "SV-ABCDEFGH"
    pc.publish_version(
        _retail_draft(scope=platform_scope, code="platform-direct-retail"),
        approved_by=1,
    )
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM customer_agent_bindings WHERE customer_user_id=%s", (12345,))
        cur.execute(
            "INSERT INTO public_account_codes(user_id,service_account_code) VALUES (%s,%s) "
            "ON CONFLICT(user_id) DO UPDATE SET service_account_code=EXCLUDED.service_account_code",
            (10, platform_scope),
        )
        conn.commit()

    relationship = pricing_ssot_api._resolve_service_principal(12345)
    assert relationship.service_user_id == 10
    assert relationship.resolution.value == "PLATFORM_DIRECT"
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS c FROM customer_agent_bindings WHERE customer_user_id=%s",
            (12345,),
        )
        row = cur.fetchone()
    assert int(row["c"] if isinstance(row, dict) else row[0]) == 0


def test_pending_locked_order_without_commercial_resolution_blocks_readiness():
    from services import pricing_readiness

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO recharge_orders(
                   id,user_id,amount_cents,payment_status,order_type,sku_template_id,
                   agent_user_id,pricing_snapshot_jsonb,created_at
               ) VALUES (
                   'legacy-pending-resolution',12345,180000,'pending','customer_recharge',1,
                   10,'{"amount_source":"sku_snapshot"}'::jsonb,NOW()
               )"""
        )
        result = pricing_readiness._check_order_snapshots(cur)

    assert result["ready"] is False
    assert result["pending_missing_commercial_resolution"] >= 1
    assert "legacy-pending-resolution" in result["pending_resolution_review_orders"]
    assert any(
        "commercial_resolution" in problem and "逐笔" in problem
        for problem in result["problems"]
    )
