"""Regression guard: customer refunds are tied to the direct order, not an upstream signoff."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_obsolete_upstream_signoff_schema_and_api_are_removed():
    db_source = (ROOT / "db" / "refund_work_order_db.py").read_text(encoding="utf-8")
    api_source = (ROOT / "api" / "refund_work_order_api.py").read_text(encoding="utf-8")
    wallet_source = (ROOT / "api" / "wallet_api.py").read_text(encoding="utf-8")
    combined = db_source + api_source + wallet_source
    assert "agent_signoff_confirmed" not in combined
    assert "agent_signoff_note" not in combined
    assert "service_provider_signoff" not in combined
    assert '@router.post("/{work_order_id}/signoff")' not in api_source


def test_work_order_execution_passes_reason_and_evidence_not_fake_consent():
    source = (ROOT / "api" / "refund_work_order_api.py").read_text(encoding="utf-8")
    start = source.index("async def execute_refund_work_order")
    end = source.find("\n@router", start + 10)
    block = source[start: end if end > 0 else len(source)]
    assert "reason_category=" in block
    assert "evidence=" in block
    assert "signoff" not in block.lower()
