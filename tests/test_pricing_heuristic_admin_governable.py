"""D3 · SSOT business-governance-master §9.5 / §9.6.

The competitor-count fallback heuristic in tools.keyword_value_scorer (the
residual `_INFO_PATTERNS` second classifier flagged as DRIFT-A) is resolved not
by deleting it (which would change pricing numbers, forbidden by §9.6) but by
making it an ADMIN-governable setting (system_settings.pricing_config):

  * §9.6 — with NO admin config, the values are byte-for-byte the original
    hardcoded 15/10/3/5 (zero pricing change on rollout);
  * §9.5 — an admin CAN govern the heuristic (it is no longer a rogue hardcoded
    classifier silently driving pricing).
"""
from tools import keyword_value_scorer


def test_defaults_equal_original_hardcoded_values(monkeypatch):
    monkeypatch.setattr("config.pricing_config.get_pricing_config", lambda: {})
    est = keyword_value_scorer.estimate_competition_from_keyword
    assert est("推荐排名榜单") == 15   # >=2 commercial signals -> strong
    assert est("推荐一下") == 10        # 1 commercial signal -> commercial
    assert est("怎么做") == 3           # info signal only
    assert est("蓝色气球") == 5          # neither -> other


def test_admin_config_governs_the_heuristic(monkeypatch):
    monkeypatch.setattr(
        "config.pricing_config.get_pricing_config",
        lambda: {"competition_fallback_counts": {"strong": 22, "commercial": 12, "info": 2, "other": 7}},
    )
    est = keyword_value_scorer.estimate_competition_from_keyword
    assert est("推荐排名榜单") == 22
    assert est("推荐一下") == 12
    assert est("怎么做") == 2
    assert est("蓝色气球") == 7


def test_db_unavailable_falls_back_to_defaults(monkeypatch):
    def boom():
        raise RuntimeError("no db")

    monkeypatch.setattr("config.pricing_config.get_pricing_config", boom)
    # must not raise; must return the safe defaults
    assert keyword_value_scorer.estimate_competition_from_keyword("推荐一下") == 10
