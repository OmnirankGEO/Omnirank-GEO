from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_subscription_plans_is_public_exact_path_only():
    """Pricing plans are public, but the subscription API prefix stays protected."""
    text = (ROOT / "auth" / "middleware.py").read_text(encoding="utf-8")

    assert '"/api/subscription/plans"' in text
    assert '"/api/subscription/"' not in text
