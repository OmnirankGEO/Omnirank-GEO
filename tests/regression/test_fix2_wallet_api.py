"""Regression guard for FIX-2 review of api/wallet_api.py.

Round-2 scope is a funds/concurrency-sensitive file. Every candidate in the
spec touches money-conservation, refund state machines, revenue attribution,
a schema migration, or a sibling business file (finance_api.py). Per the
repair discipline for fund/concurrency files, none of these may be
"hard-fixed" inline -- they are escalated to manual fund/concurrency review.

These are source-inspection assertions (no server/DB import). They assert:
  1. the pre-existing SAFETY guards that cap the severity of the skipped
     findings are still intact (so a future fund-fix must not regress them);
  2. the risky patterns are still present unchanged (proving the findings were
     NOT silently/partially fund-mutated in this round).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "api" / "wallet_api.py").read_text(encoding="utf-8")


# --- GEO-R1-CAN-038 (skipped: needs-manual-fund-review) --------------------
# The client can no longer submit a settlement principal. Both legacy SKU and
# quoted paths derive it from the canonical resolver.
def test_can038_settlement_principal_is_server_derived():
    start = SRC.index("class RechargeRequest")
    end = SRC.index("class PaymentCallbackRequest", start)
    request_model = SRC[start:end]
    assert "agent_user_id" not in request_model
    assert "resolve_commercial_relationship" in SRC
    assert "_relationship.service_user_id" in SRC


# --- GEO-R1-CAN-114 (fixed: schema migration + crash-safe fund channel routing)
# Refund execution must now prove the actual provider after channel fallback.
def test_can114_actual_channel_is_persisted_for_original_route_refund():
    assert "actual_channel = \"xunhupay\"" in SRC
    assert "SET actual_payment_channel=%s" in SRC
    initial_route = SRC.index('_persist_actual_payment_channel(str(order["id"]), actual_channel)')
    native_provider = SRC.index("await create_native_order(", initial_route)
    assert initial_route < native_provider
    fallback_route = SRC.index('_persist_actual_payment_channel(str(order["id"]), "xunhupay")')
    fallback_provider = SRC.index("await create_xunhupay_order(", fallback_route)
    assert fallback_route < fallback_provider
    migration = (ROOT / "scripts" / "migration_direct_service_refund_agreements_2026_07_15.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS actual_payment_channel" in migration


# --- GEO-R1-CAN-116 (skipped: needs-manual-fund-review, lot accounting) -----
# Refund consumption is still the simplified "sum of all consume rows after
# paid_at"; a lot-scoped rewrite is a money-conservation change left to manual.
def test_can116_consumption_still_simplified_not_lot_scoped():
    assert "type='consume'" in SRC or "type = 'consume'" in SRC
    assert "created_at > " in SRC  # keyed off paid_at cutoff, not lot id


# --- GEO-R8-CAN-012 · v12: xunhupay CD/RD/UD 退款回调状态机 + fail-closed --
# [live-blue P0-14 2026-07-12 · v11 2026-07-13] 分支走 _flag_channel_refund:
#   CD=已退款 refund_effective=True(pending_review + 冲销)· RD=退款中 refund_effective=False(pending·不冲),
#   遵守 refund-admin-ticket-only:只 flag 待人工工单,绝不内联自动冲账/扣负积分。
# 本测试断言三不变式:①非动帐 ack ②CD/RD 用 refund_effective 区分 ③未落库回非 success 让渠道重试。
def test_can012_xunhupay_refund_callback_no_inline_fund_mutation():
    m = re.search(r'elif status in \("CD", "RD", "UD"\):(.*?)else:', SRC, re.DOTALL)
    assert m, "CD/RD/UD branch not found"
    branch = m.group(1)
    # ack 机制:flag 待人工退款工单(P0-14)—— 不静默动钱
    assert "_flag_channel_refund" in branch, "CD/RD 分支应走 _flag_channel_refund(非动帐 ack)"
    assert "order_id," in branch and "status," in branch, \
        "CD/RD/UD 必须交统一状态机处理"
    assert "payment_transaction_id=transaction_id" in branch, \
        "回调 transaction_id 只能作为原支付交易号，不能冒充退款单号"
    assert "provider_refund_id=transaction_id" not in branch
    assert "external_refund_id=transaction_id" not in branch
    # [v11 F2] fail-closed:未耐久落库回非 success 让渠道重试(复用同级已验证的 "fail"/400 契约响应)
    assert 'PlainTextResponse("fail", status_code=400)' in branch, "未落库必须回 fail/400 让渠道重试(禁 fail-open)"
    # 分支内绝不内联资金冲账 / 动用户积分
    assert "clawback" not in branch.lower(), "退款回调分支不得内联 clawback"
    assert "deduct_points" not in branch and "add_points" not in branch, \
        "退款回调分支不得内联动用户积分(退款走 admin 工单)"


# --- v12: RD/CD 证据闸必须覆盖所有 admin 内账反向入口 ----------------------
def test_v12_external_refund_states_use_shared_evidence_guard():
    assert "def _require_external_refund_evidence_cur" in SRC
    assert SRC.count("_require_external_refund_evidence_cur(") >= 5, \
        "V3.5/预付/普通退款/资格检查必须共用 CD/人工证据守卫"
    assert "refund_completed_at" in SRC, "可信 CD 证据必须从订单持久字段读取"


def test_v12_xunhupay_refund_callbacks_verify_order_amount_too():
    assert 'status in (STATUS_PAID, "CD", "RD", "UD")' in SRC
    assert "_verify_callback_consistency(" in SRC


def test_v12_xunhupay_unknown_status_fails_closed():
    m = re.search(
        r'@router\.post\("/xunhupay-callback"\)(.*?)@router\.get\("/order-status/',
        SRC,
        re.DOTALL,
    )
    assert m
    body = m.group(1)
    assert '虎皮椒回调未知状态' in body
    unknown_tail = body.split('虎皮椒回调未知状态', 1)[1]
    assert 'PlainTextResponse("fail", status_code=400)' in unknown_tail


# --- GEO-R1-CAN-045 (skipped: fix belongs to finance_api.py, out of file) ---
# The offending order_type value is written here; the missing classification
# case lives in finance_api._classify_recharge_path (a file out of scope this
# round). Assert the value is still produced here unchanged.
def test_can045_order_type_value_unchanged_here():
    assert 'v35_order_type = "customer_recharge_direct"' in SRC


# --- GEO-R7-CAN-015 (skipped: needs-manual-fund-review, refund amount write)-
# admin_complete_refund still writes refund_completed_at only into
# pricing_snapshot_jsonb; promoting it to top-level column + writing the
# canonical refunded_amount_cents is a fund-amount write left to manual.
def test_can015_complete_refund_still_writes_into_jsonb_only():
    m = re.search(
        r'@router\.post\("/refund/\{order_id\}/complete"\).*?return \{"success": True',
        SRC, re.DOTALL,
    )
    assert m, "admin_complete_refund body not found"
    body = m.group(0)
    assert "'refund_completed_at', NOW()::text" in body  # still jsonb-only
    # admin gate + row lock preserved (existing safety)
    assert 'if not user.get("is_admin", False)' in body
    assert "FOR UPDATE" in body
    # confirm the top-level fund columns were NOT introduced this round
    assert "SET refund_status = 'completed',\n                refund_completed_at" not in body
    assert "refunded_amount_cents =" not in body
