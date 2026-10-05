"""Regression locks for FIX-2 · api/agent_finance_api.py [GEO-R2-CAN-001].

Source-inspection based (no server.py import, no DB). Asserts the repaired
behavior: a transient query failure is distinguished from a genuine zero via a
per-section ``unavailable`` flag rather than collapsing exceptions to {} -> 0.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

SRC = (
    Path(__file__).resolve().parents[2] / "api" / "agent_finance_api.py"
).read_text(encoding="utf-8")


def test_safe_row_returns_unavailable_sentinel_on_failure():
    """[GEO-R2-CAN-001] _safe_row must mark failures with __unavailable__, not a bare {}."""
    # The except branch must return the sentinel dict (regress -> plain `return {}` fails this).
    assert '{"__unavailable__": True}' in SRC, (
        "_safe_row exception path must return the __unavailable__ sentinel, "
        "so a transient failure is not indistinguishable from an empty ledger"
    )
    # Guard: the sentinel must live in an except block (failure), not the empty-rows path.
    m = re.search(r"except Exception[^\n]*:\n(?:[^\n]*\n){0,4}?[^\n]*__unavailable__", SRC)
    assert m, "__unavailable__ sentinel must be emitted from an except branch"


def test_settlement_branch_tracks_unavailable():
    """[GEO-R2-CAN-001] The balance/settlement except branch must set a degraded flag."""
    assert "settlement_unavailable = True" in SRC, (
        "settlement failure must flip settlement_unavailable, "
        "distinguishing DB failure from a real 0 payout pool"
    )
    assert "settlement_unavailable = False" in SRC, "settlement_unavailable must be initialized"


def test_every_section_exposes_unavailable_flag():
    """[GEO-R2-CAN-001] pnl/settlement/inventory/customers each carry an unavailable flag."""
    assert '"unavailable": pnl_unavailable' in SRC
    assert '"unavailable": settlement_unavailable' in SRC
    assert '"unavailable": inv_unavailable' in SRC
    assert '"unavailable": cust_unavailable' in SRC


def test_top_level_degraded_aggregates_sections():
    """[GEO-R2-CAN-001] Top-level degraded flag ORs all section failures."""
    m = re.search(r'"degraded":\s*([^\n,]+)', SRC)
    assert m, "response must expose a top-level degraded flag"
    expr = m.group(1)
    for token in ("pnl_unavailable", "settlement_unavailable", "inv_unavailable", "cust_unavailable"):
        assert token in expr, f"degraded must include {token}"


def test_section_flags_derive_from_sentinel():
    """[GEO-R2-CAN-001] inventory/customers unavailable flags read the sentinel key."""
    assert 'inv_unavailable = bool(inv.get("__unavailable__"))' in SRC
    assert 'cust_unavailable = bool(cust.get("__unavailable__"))' in SRC
    assert 'pnl_unavailable = bool(pnl.get("__unavailable__"))' in SRC
