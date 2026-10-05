from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_purchase_defaults_and_margin_thresholds_are_configured(monkeypatch):
    import copy
    import config.pricing_config as pricing_config

    monkeypatch.setattr(
        pricing_config,
        "get_pricing_config",
        lambda: copy.deepcopy(pricing_config._PRICING_DEFAULTS),
    )
    get_agent_purchase_options = pricing_config.get_agent_purchase_options
    get_margin_label_thresholds = pricing_config.get_margin_label_thresholds

    amounts = [row["amount_cents"] for row in get_agent_purchase_options()]
    assert amounts == [50000, 100000, 300000, 500000, 1000000, 3000000]

    thresholds = get_margin_label_thresholds()
    assert thresholds["loss_heavy_bps"] == -4000
    assert thresholds["profit_excellent_bps"] > thresholds["healthy_bps"]
    assert thresholds["hard_block_bps"] > thresholds["profit_excellent_bps"]
    assert thresholds["hard_block_bps"] == 250000

    for row in get_agent_purchase_options():
        assert row["base_points"] == 0
        assert row["bonus_points"] == 0


def test_channel_tier_config_validator_writes_complete_safe_object():
    from services.channel_tier_admin import normalize_channel_tier_config

    normalized = normalize_channel_tier_config(
        {
            "agent_tier_config": {
                "preferred": {"min_yuan": 3500, "bonus_rate": 0.55}
            }
        }
    )
    assert set(normalized["agent_tier_config"]) == {"certified", "preferred", "strategic"}
    assert normalized["agent_tier_config"]["preferred"]["min_yuan"] == "3500"
    assert normalized["agent_tier_config"]["certified"]["bonus_rate"] == "0.1"
    assert normalized["agent_tier_config"]["certified"]["is_enabled"] is True
    assert normalized["agent_tier_config"]["certified"]["description"]

    with pytest.raises(ValueError):
        normalize_channel_tier_config(
            {
                "agent_tier_config": {
                    "certified": {"min_yuan": 500, "bonus_rate": 0.1},
                    "preferred": {"min_yuan": 400, "bonus_rate": 0.15},
                }
            }
        )
    with pytest.raises(ValueError):
        normalize_channel_tier_config(
            {"agent_tier_config": {"certified": {"min_yuan": 500, "bonus_rate": 1.2}}}
        )
    with pytest.raises(ValueError):
        normalize_channel_tier_config(
            {"agent_purchase_options": [{"amount_cents": 100000}, {"amount_cents": 50000}]}
        )


def test_purchase_projection_honors_active_agent_tier_override(monkeypatch):
    import config.pricing_config as pricing_config
    import services.agent_pricing as agent_pricing
    import services.channel_tier as channel_tier

    class Cursor:
        def execute(self, query, params=None):
            self.query = query
            self.params = params

        def fetchone(self):
            return {
                "tier_override": "strategic",
                "tier_override_until": None,
            }

    monkeypatch.setattr(agent_pricing, "calc_prepay_points", lambda amount_cents, agent_user_id=None: 1000)
    monkeypatch.setattr(
        pricing_config,
        "get_agent_tier_config",
        lambda: {
            "certified": {"min_yuan": 500, "bonus_rate": 0.10},
            "preferred": {"min_yuan": 3000, "bonus_rate": 0.15},
            "strategic": {"min_yuan": 10000, "bonus_rate": 0.30},
        },
    )

    projection = channel_tier.compute_purchase_bonus_projection(
        Cursor(),
        agent_user_id=1001,
        amount_cents=50000,
        rolling_before_yuan=0,
    )
    assert projection["natural_projected_tier"] == "certified"
    assert projection["projected_tier"] == "strategic"
    assert projection["tier_source"] == "admin_override"
    assert projection["bonus_points"] == 700


def test_channel_tier_migration_adds_override_columns_and_verifies_them():
    migration = read("scripts/migration_channel_tier_2026_06_28.sql")
    runner = read("db/migrate_channel_tier.py")

    for column in ("tier_override", "tier_override_until", "tier_override_by", "tier_override_note"):
        assert column in migration
        assert column in runner
    assert "ADD COLUMN IF NOT EXISTS tier_override" in migration
    assert "agent_channel_tier_state override columns" in runner


def test_label_margin_reads_config_and_treats_high_profit_as_positive(monkeypatch):
    import config.pricing_config as pricing_config
    from services.agent_pricing import label_margin

    monkeypatch.setattr(
        pricing_config,
        "get_margin_label_thresholds",
        lambda: {
            "loss_heavy_bps": -4000,
            "loss_light_bps": -1000,
            "healthy_bps": 2000,
            "profit_excellent_bps": 8000,
            "hard_block_bps": 30000,
        },
    )

    assert label_margin(2500, 1000) == ("profit_excellent", "allowed")
    assert label_margin(4000, 1000) == ("margin_anomaly_blocked", "rejected")
    assert label_margin(-600, 1000) == ("loss_heavy", "rejected")
    assert label_margin(100, 0) == ("invalid_factory", "rejected")


def test_default_service_value_packages_are_not_hard_blocked_by_margin_config():
    from services.agent_pricing import calc_factory_cents, label_margin
    from services.channel_tier_admin import default_channel_sku_packages

    for row in default_channel_sku_packages():
        factory_cents = calc_factory_cents(row["points_granted"])
        margin_cents = row["suggested_retail_cents"] - factory_cents
        label, action = label_margin(margin_cents, factory_cents)
        assert action == "allowed", (row["template_code"], label, action, margin_cents, factory_cents)


def test_frontend_margin_labels_cover_channel_tier_labels():
    source = read("frontend/src/pages/Agent/PricingCenter.tsx")

    for key in ("profit_good", "profit_excellent", "margin_anomaly_blocked", "invalid_factory"):
        assert key in source


def test_channel_tier_admin_hides_dead_point_inputs():
    source = read("frontend/src/pages/Admin/ChannelTierAdmin.tsx")

    assert "updatePurchaseOption(index, 'base_points'" not in source
    assert "updatePurchaseOption(index, 'bonus_points'" not in source
    assert "实际到账算力由实时进货折扣和渠道等级自动计算" in source


def test_channel_tier_verify_only_checks_flag_key_exists():
    runner = read("db/migrate_channel_tier.py")

    assert "CHANNEL_TIER_ENABLED key exists" in runner
    assert "WHERE key='CHANNEL_TIER_ENABLED' AND value='false'" not in runner


def test_default_channel_sku_packages_are_service_value_packages():
    from services.channel_tier_admin import default_channel_sku_packages

    packages = default_channel_sku_packages()
    assert len(packages) == 7
    codes = {row["template_code"] for row in packages}
    assert "basic_experience" not in codes
    assert {"scenario_trial", "scenario_validation", "addon_writing"}.issubset(codes)
    # addon 包 template_code 必须与既有 sku_templates(migration_v35_backfill_factory)对齐,
    # 否则 seed 会 INSERT 重复包而非 UPDATE 旧包(单篇写作=addon_writing / 监测=addon_monitor)。
    addon_codes = {row["template_code"] for row in packages if row["sku_type"] == "addon_pack"}
    assert addon_codes == {"addon_diagnosis", "addon_writing", "addon_monitor"}
    validation = next(row for row in packages if row["template_code"] == "scenario_validation")
    assert validation["suggested_retail_cents"] == 248000
    assert "算力" not in validation["default_subtitle"]
    assert "进货" not in validation["default_capability_pitch"]


def test_admin_channel_tier_endpoints_are_registered():
    admin_api = read("api/admin_api.py")
    frontend_api = read("frontend/src/lib/v35w2Api.ts")

    for route in (
        "/pricing/channel-tier-config",
        "/channel-tier/agents",
        "/channel-tier/founder-seats",
        "/pricing/default-sku-packages/seed",
    ):
        assert route in admin_api
    assert "channelTierConfigGet" in frontend_api
    assert "channelTierAgents" in frontend_api
