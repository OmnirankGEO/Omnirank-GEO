"""Mobile layout guards for the agent pricing center.

The pricing SKU cards sit near the mobile browser toolbar. Unprefixed grid
stretching plus flex spacers can create a large empty-looking card body on
phones, especially when the floating assistant button overlaps the list.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_pricing_center_sku_cards_do_not_stretch_mobile_rows():
    src = (ROOT / "frontend/src/pages/Agent/PricingCenter.tsx").read_text(encoding="utf-8")

    assert "grid grid-cols-1 items-start gap-4 sm:grid-cols-2" in src
    assert "flex h-full flex-col" not in src
    assert 'CardContent className="flex flex-1 flex-col gap-3 text-sm"' not in src
    assert 'className="mt-auto pt-1"' not in src


def test_agent_fab_respects_mobile_safe_area_bottom():
    src = (ROOT / "frontend/src/components/agent/AgentFAB.tsx").read_text(encoding="utf-8")

    assert "env(safe-area-inset-bottom" in src
    assert "bottom: pos.y" not in src
