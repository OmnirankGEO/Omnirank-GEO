"""Focused pure-contract tests for the pricing publication adapter."""

from config.pricing_config import merge_pricing_config
from services.pricing_publication import (
    _build_procurement_plan,
    _load_retail_source_rows,
    _retail_product_code,
)


def test_procurement_source_contract_and_cash_reward_snapshot():
    config = merge_pricing_config({}, include_env=False)
    plan = _build_procurement_plan(config, config_epoch=7)

    assert plan["catalog_type"] == "procurement"
    assert plan["scope_key"] == "PLATFORM_BASE"
    assert plan["entries"]
    first = plan["entries"][0]
    source = first["source_ref_jsonb"]
    assert source == {
        "kind": "agent_purchase_option",
        "option_id": source["option_id"],
        "amount_cents": source["amount_cents"],
        "option": source["option"],
    }
    assert source["option"]["is_enabled"] is True
    assert first["paid_points"] == (
        first["final_price_cents"]
        * int(config["wholesale_denom"])
        // int(config["wholesale_numer"])
    )

    snapshot = plan["calc_meta"]["pricing_config_snapshot"]
    assert set((
        "wholesale_numer",
        "wholesale_denom",
        "agent_purchase_catalog_version",
        "agent_purchase_options",
        "agent_purchase_bonus_rate",
        "agent_tier_config",
        "founding",
        "bonus_validity_months",
    )).issubset(snapshot)
    assert plan["calc_meta"]["config_epoch"] == 7


def test_procurement_fingerprint_is_idempotent_per_epoch_and_epoch_sensitive():
    config = merge_pricing_config({}, include_env=False)
    first = _build_procurement_plan(config, config_epoch=11)
    retry = _build_procurement_plan(config, config_epoch=11)
    next_epoch = _build_procurement_plan(config, config_epoch=12)

    assert first["source_fingerprint"] == retry["source_fingerprint"]
    assert first["version_code"] == retry["version_code"]
    assert first["source_fingerprint"] != next_epoch["source_fingerprint"]


class _RetailSourceCursor:
    def __init__(self, overrides, templates):
        self.overrides = overrides
        self.templates = templates
        self.rows = []

    def execute(self, sql, params=None):
        if "FROM agent_sku_overrides o" in sql:
            self.rows = list(self.overrides)
        elif "FROM sku_templates" in sql:
            self.rows = list(self.templates)
        else:  # pragma: no cover - makes an unexpected query fail loudly
            raise AssertionError(sql)

    def fetchall(self):
        return list(self.rows)


def test_retail_visibility_uses_only_explicit_canonical_packages_without_fallback():
    cursor = _RetailSourceCursor(
        overrides=[
            {
                "override_id": 101,
                "agent_user_id": 9,
                "sku_template_id": 1,
                "source_template_id": 1,
                "retail_sku_id": "RSKU-PROVIDER-ONE",
                "retail_sku_version": 3,
                "retail_cents": 5000,
                "custom_name": "Provider One",
                "override_active": True,
                "deleted_at": None,
                "override_updated_at": None,
                "template_code": "one",
                "sku_type": "credit_pack",
                "default_name": "Default One",
                "points_granted": 1000,
                "template_wholesale_cents": 800,
                "suggested_retail_cents": 3000,
                "template_active": True,
                "template_updated_at": None,
            },
            # An inactive canonical row is not sellable and no template fallback exists.
            {
                "override_id": 202,
                "agent_user_id": 9,
                "sku_template_id": 2,
                "source_template_id": 2,
                "retail_sku_id": "RSKU-PROVIDER-TWO",
                "retail_sku_version": 4,
                "retail_cents": 6000,
                "custom_name": "Provider Two",
                "override_active": False,
                "deleted_at": None,
                "override_updated_at": None,
                "template_code": "two",
                "sku_type": "credit_pack",
                "default_name": "Default Two",
                "points_granted": 2000,
                "template_wholesale_cents": 1600,
                "suggested_retail_cents": 4000,
                "template_active": True,
                "template_updated_at": None,
            },
        ],
        templates=[
            {"sku_template_id": 1, "template_code": "one", "sku_type": "credit_pack",
             "default_name": "Default One",
             "points_granted": 1000, "template_wholesale_cents": 800,
             "suggested_retail_cents": 3000, "template_updated_at": None},
            {"sku_template_id": 2, "template_code": "two", "sku_type": "credit_pack",
             "default_name": "Default Two",
             "points_granted": 2000, "template_wholesale_cents": 1600,
             "suggested_retail_cents": 4000, "template_updated_at": None},
            {"sku_template_id": 3, "template_code": "three", "sku_type": "credit_pack",
             "default_name": "Default Three",
             "points_granted": 3000, "template_wholesale_cents": 2400,
             "suggested_retail_cents": 7000, "template_updated_at": None},
        ],
    )

    result = _load_retail_source_rows(cursor, 9)
    sellable = result["sellable_rows"]
    assert [(row["retail_sku_id"], row["source_kind"]) for row in sellable] == [
        ("RSKU-PROVIDER-ONE", "agent_retail_sku"),
    ]
    assert sellable[0]["custom_name"] == "Provider One"
    assert result["override_state"]


def test_retail_product_identity_is_deterministic():
    retail_code = _retail_product_code(9, retail_sku_id="RSKU-CANONICAL-101")
    assert retail_code == _retail_product_code(9, retail_sku_id="RSKU-CANONICAL-101")
    assert retail_code != _retail_product_code(9, retail_sku_id="RSKU-CANONICAL-102")
