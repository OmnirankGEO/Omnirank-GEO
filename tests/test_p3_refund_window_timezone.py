"""Refund legal windows must use the database clock and separate B2C from B2B."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_consumer_seven_day_rule_uses_database_clock():
    source = (ROOT / "services" / "dealer_inventory_resale.py").read_text(encoding="utf-8")
    start = source.index("def request_consumer_refund_case")
    end = source.index("\ndef review_consumer_refund_case", start)
    block = source[start:end]
    assert "INTERVAL '7 days'" in block
    assert "NOW()" in block
    assert "datetime.utcnow" not in block
    assert '"b2b_72h_rule_applied": False' in block


def test_b2b_window_remains_database_clocked_and_separate():
    source = (ROOT / "services" / "dealer_inventory_resale.py").read_text(encoding="utf-8")
    start = source.index("def request_refund(")
    end = source.index("\ndef create_manual_refund_case", start)
    block = source[start:end]
    assert 'deadline = order.get("refund_deadline")' in block
    assert "clock_timestamp" in block
    assert "datetime.utcnow" not in block

    settlement_start = source.index("def settle_reserved_order(")
    settlement_end = source.index("\ndef settle_consumer_sale", settlement_start)
    settlement = source[settlement_start:settlement_end]
    assert "refund_deadline=%s + INTERVAL '72 hours'" in settlement
