"""Dealer inventory resale SSOT.

Legacy v1 orders represent one direct sale.  JIT v2 keeps that current-leg
contract but pins a complete upstream fulfillment plan beneath the same external
payment order.  Every hop owns real lots, cost and margin; refunds remain one leg.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from config.dealer_inventory_resale_flags import enabled as resale_flag_enabled
from db.connection import get_db
from db.xact_lock_guard import require_xact_scope


REFUND_WINDOW_HOURS = 72
MIN_MARKUP_BPS = 10000
MAX_CHAIN_DEPTH = 10
MAX_MONEY_CENTS = 2_000_000_000
DIGITAL_GOODS_POLICY_CODE = "DIGITAL_CREDIT_NO_UNCONDITIONAL_RETURN_V1"
MANDATORY_CONSUMER_REFUND_REASONS = frozenset({
    "statutory_seven_day",
    "duplicate_charge",
    "not_credited",
    "not_delivered",
    "system_failure",
    "legal_required",
})
NEGOTIATED_CONSUMER_REFUND_REASONS = frozenset({
    "change_of_mind",
    "service_dissatisfaction",
    "negotiated_other",
})
LEGACY_INVENTORY_TRANSACTION_TYPES = frozenset({
    "purchase_prepay", "purchase_auto", "purchase_admin_adjust",
    "purchase_from_commission", "allocate_to_customer",
    "allocate_to_customer_offline", "revoke_from_customer", "admin_adjust",
    "refund_clawback", "manufacturer_origin_in", "resale_transfer_out",
    "resale_transfer_in", "consumer_sale_out", "consumer_refund_in",
    "resale_refund_out", "resale_refund_in",
})
LEGACY_CUSTOMER_CREDIT_SOURCES = frozenset({
    "online_payment", "offline_allocation", "admin_adjust", "tool_consume",
    "refund_revoke", "agent_rebate", "tool_fail_refund",
    "diagnosis_delivery_refund", "direct_service_refund",
})
REQUIRED_TABLES = {
    "dealer_resale_global_settings",
    "dealer_resale_policies",
    "dealer_inventory_lots",
    "dealer_manufacturer_lot_issuances",
    "dealer_resale_orders",
    "dealer_inventory_lot_allocations",
    "dealer_inventory_transfers",
    "dealer_inventory_transfer_entries",
    "dealer_resale_profit_ledger",
    "dealer_consumer_sales",
    "dealer_consumer_lot_allocations",
    "dealer_consumer_transfer_entries",
    "dealer_resale_refunds",
    "consumer_refund_cases",
    "consumer_refund_lot_restorations",
    "service_refund_reserve_accounts",
    "service_refund_liability_ledger",
    "service_refund_funding_work_orders",
    "service_refund_cash_jobs",
    "external_refund_proof_registry",
    "purchase_agreement_acceptances",
    "purchase_agreement_acceptance_uses",
    "agreement_signatures",
    "agent_factory_agreements",
    "user_social_subscriptions",
    "subscription_refund_cases",
    "dealer_resale_promotions",
    "dealer_resale_fulfillment_plans",
    "dealer_resale_fulfillment_hops",
    "dealer_resale_fulfillment_allocations",
    "dealer_resale_hop_transfer_entries",
    "dealer_resale_hop_profit_ledger",
    "dealer_consumer_jit_refund_lots",
}
EXPECTED_RESALE_CONSTRAINT_CONTRACT_SHA256 = (
    "81b09501fba61fb5952d97a733c303e57ed9f98a210bb0a1e89ab464337f1024"
)
EXPECTED_RESALE_INDEX_CONTRACT_SHA256 = (
    "4f6d8a0ca277b622d64b198c22b7aa5e5fff4804f6149108dabc246ffab6bf0d"
)
EXPECTED_PURCHASE_INDEX_CONTRACT_SHA256 = (
    "8ef592ae0e1fac11f068413855e722f2e5c8a727622f2bad9fadd688a1e8d0a5"
)
EXPECTED_RESALE_TRIGGER_FUNCTIONS_SHA256 = (
    "dbe29a703b79bf55938265dd5b5b5b0080922f65b3111e042ac2922bac26171c"
)
EXPECTED_PURCHASE_TRIGGER_FUNCTION_SHA256 = (
    "1b5a45e639e1e7c65e574bf7c8a00a3f8c791edcaec771f1d450c4f0e7a00283"
)


def _constraint_table(table: str, *names: str) -> Dict[str, str]:
    """Declare the expected owner table for named migration constraints."""
    return {name: table for name in names}


# Object identity is part of the readiness contract.  PostgreSQL permits the
# same constraint name on different tables, so a name-only catalog lookup can
# be satisfied by an unrelated decoy object after the real guard is removed.
REQUIRED_CONSTRAINT_TABLES = {
    **_constraint_table(
        "agent_inventory_transactions", "agent_inventory_transactions_type_check",
    ),
    **_constraint_table(
        "consumer_refund_cases",
        "chk_consumer_refund_amounts", "chk_consumer_refund_cash_status",
        "chk_consumer_refund_decision_kind", "chk_consumer_refund_eligibility_object",
        "chk_consumer_refund_execution", "chk_consumer_refund_mandatory_evidence",
        "chk_consumer_refund_parties", "chk_consumer_refund_policy",
        "chk_consumer_refund_status", "consumer_refund_cases_source_order_id_fkey",
        "ux_consumer_refund_source",
    ),
    **_constraint_table(
        "consumer_refund_lot_restorations", "chk_consumer_refund_lot_values",
        "ux_consumer_refund_lot_restoration",
    ),
    **_constraint_table(
        "customer_credit_transactions", "customer_credit_transactions_source_check",
    ),
    **_constraint_table(
        "external_refund_proof_registry",
        "chk_external_refund_proof_nonblank", "chk_external_refund_proof_scope",
        "chk_external_refund_proof_provider", "chk_external_refund_proof_amount",
        "chk_external_refund_proof_evidence", "ux_external_refund_proof_key",
        "ux_external_refund_proof_local",
    ),
    **_constraint_table(
        "dealer_consumer_jit_refund_lots", "chk_dealer_consumer_jit_refund_values",
    ),
    **_constraint_table(
        "dealer_consumer_lot_allocations",
        "chk_dealer_consumer_allocation_refunded",
        "chk_dealer_consumer_allocation_status",
        "chk_dealer_consumer_allocation_values", "ux_dealer_consumer_allocation",
        "ux_dealer_consumer_allocation_seq",
    ),
    **_constraint_table(
        "dealer_consumer_sales",
        "chk_dealer_consumer_sale_allocations", "chk_dealer_consumer_sale_collector",
        "chk_dealer_consumer_sale_cost_positive", "chk_dealer_consumer_sale_markup",
        "chk_dealer_consumer_sale_money", "chk_dealer_consumer_sale_paid_fields",
        "chk_dealer_consumer_sale_parties", "chk_dealer_consumer_sale_payout_v2",
        "chk_dealer_consumer_sale_points", "chk_dealer_consumer_sale_policy",
        "chk_dealer_consumer_sale_refund_owner", "chk_dealer_consumer_sale_state",
        "chk_dealer_consumer_sale_version",
    ),
    **_constraint_table(
        "dealer_consumer_transfer_entries",
        "chk_dealer_consumer_transfer_cost", "chk_dealer_consumer_transfer_direction",
        "chk_dealer_consumer_transfer_sign", "fk_dealer_consumer_transfer_sale",
        "ux_dealer_consumer_transfer_side",
    ),
    **_constraint_table(
        "dealer_inventory_lot_allocations", "chk_dealer_lot_allocation_status",
        "chk_dealer_lot_allocation_values", "ux_dealer_lot_allocation",
        "ux_dealer_lot_allocation_seq",
    ),
    **_constraint_table(
        "dealer_inventory_lots",
        "chk_dealer_lot_acquisition_cost_positive", "chk_dealer_lot_cost_conservation",
        "chk_dealer_lot_evidence_object", "chk_dealer_lot_id_nonblank",
        "chk_dealer_lot_jit_lineage", "chk_dealer_lot_points_conservation",
        "chk_dealer_lot_points_positive", "chk_dealer_lot_pricing_version",
        "chk_dealer_lot_promotion_lineage_v2", "chk_dealer_lot_promotion_scope",
        "chk_dealer_lot_promotion_snapshot", "chk_dealer_lot_source_kind",
        "chk_dealer_lot_standard_reference", "chk_dealer_lot_status",
        "fk_dealer_lot_source_transfer",
    ),
    **_constraint_table(
        "dealer_inventory_transfer_entries", "chk_dealer_transfer_delta_sign",
        "chk_dealer_transfer_direction", "chk_dealer_transfer_entry_cost",
        "ux_dealer_transfer_entry",
    ),
    **_constraint_table(
        "dealer_inventory_transfers", "chk_dealer_transfer_cost_positive",
        "chk_dealer_transfer_money", "chk_dealer_transfer_parties",
        "chk_dealer_transfer_points", "chk_dealer_transfer_state",
    ),
    **_constraint_table(
        "dealer_manufacturer_lot_issuances", "chk_dealer_manufacturer_issue_evidence",
        "chk_dealer_manufacturer_issue_values",
    ),
    **_constraint_table(
        "dealer_resale_fulfillment_allocations",
        "chk_dealer_fulfillment_allocation_kind",
        "chk_dealer_fulfillment_allocation_state",
        "chk_dealer_fulfillment_allocation_values",
        "fk_dealer_fulfillment_allocation_hop", "ux_dealer_fulfillment_allocation_seq",
    ),
    **_constraint_table(
        "dealer_resale_fulfillment_hops", "chk_dealer_fulfillment_hop_edge",
        "chk_dealer_fulfillment_hop_money", "chk_dealer_fulfillment_hop_parties",
        "chk_dealer_fulfillment_hop_payout_v2",
        "chk_dealer_fulfillment_hop_promotion", "chk_dealer_fulfillment_hop_source",
        "chk_dealer_fulfillment_hop_state", "chk_dealer_fulfillment_hop_values",
        "ux_dealer_fulfillment_root_hop",
    ),
    **_constraint_table(
        "dealer_resale_fulfillment_plans", "chk_dealer_fulfillment_plan_identity",
        "chk_dealer_fulfillment_plan_json", "chk_dealer_fulfillment_plan_kind",
        "chk_dealer_fulfillment_plan_parties", "chk_dealer_fulfillment_plan_state",
        "chk_dealer_fulfillment_plan_values",
    ),
    **_constraint_table(
        "dealer_resale_global_settings", "chk_dealer_resale_default_markup",
        "chk_dealer_resale_global_singleton", "chk_dealer_resale_settings_row_version",
        "chk_dealer_resale_settings_version",
    ),
    **_constraint_table(
        "dealer_resale_hop_profit_ledger", "chk_dealer_hop_profit_money",
        "chk_dealer_hop_profit_parties", "chk_dealer_hop_profit_payout_v2",
        "chk_dealer_hop_profit_status", "fk_dealer_hop_profit_hop",
        "ux_dealer_hop_profit",
    ),
    **_constraint_table(
        "dealer_resale_hop_transfer_entries", "chk_dealer_hop_transfer_direction",
        "chk_dealer_hop_transfer_parties", "chk_dealer_hop_transfer_sign",
        "chk_dealer_hop_transfer_values", "fk_dealer_hop_transfer_hop",
        "ux_dealer_hop_transfer_direction", "ux_dealer_hop_transfer_entry",
    ),
    **_constraint_table(
        "dealer_resale_orders", "chk_dealer_resale_order_allocations",
        "chk_dealer_resale_order_collector", "chk_dealer_resale_order_cost_positive",
        "chk_dealer_resale_order_markup", "chk_dealer_resale_order_money",
        "chk_dealer_resale_order_paid_fields", "chk_dealer_resale_order_parties",
        "chk_dealer_resale_order_points", "chk_dealer_resale_order_refund_owner",
        "chk_dealer_resale_order_source", "chk_dealer_resale_order_source_allocations",
        "chk_dealer_resale_order_state", "chk_dealer_resale_order_versions",
        "fk_dealer_order_transfer", "ux_dealer_resale_order_quote",
        "ux_dealer_resale_order_transfer",
    ),
    **_constraint_table(
        "dealer_resale_policies", "chk_dealer_resale_policy_bounds",
        "chk_dealer_resale_policy_markup", "chk_dealer_resale_policy_row_version",
        "chk_dealer_resale_policy_version",
    ),
    **_constraint_table(
        "dealer_resale_profit_ledger", "chk_dealer_profit_cost_positive",
        "chk_dealer_profit_money", "chk_dealer_profit_parties",
        "chk_dealer_profit_status",
    ),
    **_constraint_table(
        "dealer_resale_promotions", "chk_dealer_resale_promo_discount",
        "chk_dealer_resale_promo_eligibility", "chk_dealer_resale_promo_identity",
        "chk_dealer_resale_promo_scope", "chk_dealer_resale_promo_status",
        "chk_dealer_resale_promo_version", "chk_dealer_resale_promo_window",
    ),
    **_constraint_table(
        "dealer_resale_refunds", "chk_dealer_refund_audit_object",
        "chk_dealer_refund_execution_money", "chk_dealer_refund_mandatory_evidence",
        "chk_dealer_refund_processing_cost", "chk_dealer_refund_status",
    ),
    **_constraint_table(
        "purchase_agreement_acceptance_uses", "chk_purchase_acceptance_kind",
        "ux_purchase_acceptance_global_once", "ux_purchase_acceptance_ref",
    ),
    **_constraint_table(
        "purchase_agreement_acceptances", "chk_purchase_acceptance_evidence",
        "chk_purchase_acceptance_nonblank",
    ),
    **_constraint_table("recharge_orders", "chk_recharge_actual_payment_channel"),
    **_constraint_table(
        "service_refund_cash_jobs", "chk_service_refund_cash_amount",
        "chk_service_refund_cash_evidence", "chk_service_refund_cash_route",
        "chk_service_refund_cash_status",
    ),
    **_constraint_table(
        "service_refund_funding_work_orders", "chk_service_refund_funding_resolution",
        "chk_service_refund_funding_shortage", "chk_service_refund_funding_status",
    ),
    **_constraint_table(
        "service_refund_liability_ledger", "chk_service_refund_liability_money",
        "chk_service_refund_liability_status",
    ),
    **_constraint_table(
        "service_refund_reserve_accounts", "chk_service_refund_reserve_values",
    ),
    **_constraint_table(
        "subscription_refund_cases", "chk_subscription_refund_decision",
        "chk_subscription_refund_json", "chk_subscription_refund_mandatory_evidence",
        "chk_subscription_refund_status", "fk_subscription_refund_subscription",
    ),
    **_constraint_table(
        "user_social_subscriptions", "fk_subscription_purchase_terms_acceptance",
    ),
}

STRICT_CONSTRAINT_DEFINITIONS = {
    "chk_dealer_consumer_sale_money": (
        "CHECK (seller_cost_basis_cents >= 0 AND sale_amount_cents > 0 "
        "AND margin_cents >= 0 AND sale_amount_cents = "
        "(seller_cost_basis_cents + margin_cents))"
    ),
    "chk_dealer_consumer_sale_payout_v2": (
        "CHECK (principal_recovery_cents IS NULL AND agent_payable_cents IS NULL "
        "OR principal_recovery_cents >= 0 AND principal_recovery_cents <= "
        "seller_cost_basis_cents AND agent_payable_cents = "
        "(principal_recovery_cents + margin_cents) AND agent_payable_cents <= sale_amount_cents)"
    ),
    "chk_dealer_fulfillment_hop_money": (
        "CHECK (seller_cost_basis_cents > 0 AND sale_amount_cents > 0 "
        "AND margin_cents >= 0 AND sale_amount_cents = "
        "(seller_cost_basis_cents + margin_cents))"
    ),
    "chk_dealer_fulfillment_hop_payout_v2": (
        "CHECK (principal_recovery_cents >= 0 AND principal_recovery_cents <= "
        "seller_cost_basis_cents AND (source_kind = 'platform_root'::text "
        "AND principal_recovery_cents = 0 AND agent_payable_cents = 0 OR "
        "source_kind = 'dealer_resale'::text AND agent_payable_cents = "
        "(principal_recovery_cents + margin_cents)))"
    ),
    # 按需铸造第一步(2026-07-29):平台厂家根跳的供给记在 mint_points,
    # 等式扩为 existing + shortfall + mint = points。存量行 mint_points 默认 0,等式不变。
    "chk_dealer_fulfillment_hop_values": (
        "CHECK (hop_seq >= 0 AND points > 0 AND existing_inventory_points >= 0 "
        "AND jit_shortfall_points >= 0 AND mint_points >= 0 "
        "AND (existing_inventory_points + jit_shortfall_points + mint_points) = points "
        "AND standard_reference_cents > 0)"
    ),
    "chk_dealer_hop_profit_money": (
        "CHECK (seller_cost_basis_cents > 0 AND sale_amount_cents > 0 "
        "AND principal_recovery_cents >= 0 AND margin_cents >= 0 "
        "AND platform_root_revenue_cents >= 0 AND sale_amount_cents = "
        "(seller_cost_basis_cents + margin_cents) AND principal_recovery_cents <= "
        "seller_cost_basis_cents AND platform_root_revenue_cents <= sale_amount_cents)"
    ),
    "chk_dealer_hop_profit_payout_v2": (
        "CHECK (platform_root_revenue_cents = sale_amount_cents "
        "AND principal_recovery_cents = 0 AND agent_payable_cents = 0 OR "
        "platform_root_revenue_cents = 0 AND agent_payable_cents = "
        "(principal_recovery_cents + margin_cents))"
    ),
    "chk_dealer_lot_cost_conservation": (
        "CHECK (acquisition_cost_cents >= 0 AND remaining_cost_cents >= 0 "
        "AND reserved_cost_cents >= 0 AND (remaining_cost_cents + "
        "reserved_cost_cents) <= acquisition_cost_cents)"
    ),
    "chk_dealer_lot_promotion_lineage_v2": (
        "CHECK (promotion_campaign_id IS NULL AND promotion_funding_scope IS NULL "
        "AND promotion_product_code IS NULL AND promotion_root_catalog_version IS NULL "
        "OR promotion_campaign_id IS NOT NULL AND (promotion_funding_scope = ANY "
        "(ARRAY['PLATFORM'::text, 'SELLER'::text])) AND promotion_product_code IS NOT NULL "
        "AND promotion_root_catalog_version IS NOT NULL AND btrim(promotion_product_code) "
        "<> ''::text AND btrim(promotion_root_catalog_version) <> ''::text "
        "AND promotion_root_catalog_version = pricing_version)"
    ),
}

STRICT_CONSTRAINT_DEFINITIONS.update({
    "chk_external_refund_proof_nonblank": (
        "CHECK (btrim(provider) <> ''::text AND provider = lower(btrim(provider)) "
        "AND btrim(external_refund_id) <> ''::text AND btrim(local_ref) <> ''::text)"
    ),
    "chk_external_refund_proof_scope": (
        "CHECK (refund_scope = ANY (ARRAY['consumer'::text, 'dealer_b2b'::text]))"
    ),
    "chk_external_refund_proof_provider": (
        "CHECK (provider = ANY (ARRAY['wechat'::text, 'xunhupay'::text]))"
    ),
    "chk_external_refund_proof_amount": "CHECK (amount_cents > 0)",
    "chk_external_refund_proof_evidence": (
        "CHECK (jsonb_typeof(evidence_jsonb) = 'object'::text)"
    ),
    "ux_external_refund_proof_key": "UNIQUE (provider, external_refund_id)",
    "ux_external_refund_proof_local": "UNIQUE (refund_scope, local_ref)",
    "chk_dealer_consumer_allocation_refunded": (
        "CHECK (refunded_points >= 0 AND refunded_points <= points AND "
        "refunded_cost_cents >= 0 AND refunded_cost_cents <= cost_basis_cents)"
    ),
    "chk_consumer_refund_amounts": (
        "CHECK (refund_amount_cents >= 0 AND revoked_paid_points >= 0 AND "
        "revoked_bonus_points >= 0 AND restored_inventory_points >= 0 AND "
        "revenue_reversed_cents >= 0 AND row_version >= 1)"
    ),
    "chk_consumer_refund_cash_status": (
        "CHECK (cash_status = ANY (ARRAY['not_started'::text, 'queued'::text, "
        "'provider_processing'::text, 'completed'::text, 'failed'::text, "
        "'manual_review'::text]))"
    ),
    "chk_consumer_refund_execution": (
        "CHECK (jsonb_typeof(execution_jsonb) = 'object'::text)"
    ),
    "chk_consumer_refund_lot_values": "CHECK (points > 0 AND cost_basis_cents >= 0)",
    "chk_consumer_refund_status": (
        "CHECK (status = ANY (ARRAY['requested'::text, 'service_review'::text, "
        "'platform_execution'::text, 'completed'::text, 'rejected'::text, "
        "'manual_review'::text]))"
    ),
    "chk_dealer_consumer_allocation_status": (
        "CHECK (status = ANY (ARRAY['reserved'::text, 'consumed'::text, "
        "'restored'::text, 'cancelled'::text]))"
    ),
    "chk_dealer_consumer_allocation_values": (
        "CHECK (allocation_seq >= 0 AND points > 0 AND cost_basis_cents >= 0)"
    ),
    "chk_dealer_consumer_jit_refund_values": (
        "CHECK (points > 0 AND cost_basis_cents > 0)"
    ),
    "chk_dealer_consumer_sale_cost_positive": (
        "CHECK (seller_cost_basis_cents > 0)"
    ),
    "chk_dealer_consumer_sale_paid_fields": (
        "CHECK ((state = ANY (ARRAY['reserved'::text, 'cancelled'::text])) AND "
        "paid_at IS NULL AND transfer_id IS NULL AND revenue_ledger_id IS NULL OR "
        "(state = ANY (ARRAY['paid'::text, 'refund_pending'::text, 'refunded'::text, "
        "'manual_review'::text])) AND paid_at IS NOT NULL AND transfer_id IS NOT NULL "
        "AND revenue_ledger_id IS NOT NULL)"
    ),
    "chk_dealer_consumer_sale_points": "CHECK (points > 0)",
    "chk_dealer_consumer_sale_state": (
        "CHECK (state = ANY (ARRAY['reserved'::text, 'paid'::text, "
        "'refund_pending'::text, 'refunded'::text, 'manual_review'::text, "
        "'cancelled'::text]))"
    ),
    "chk_dealer_consumer_transfer_cost": "CHECK (cost_basis_cents >= 0)",
    "chk_dealer_fulfillment_allocation_state": (
        "CHECK (status = ANY (ARRAY['reserved'::text, 'consumed'::text, "
        "'cancelled'::text, 'restored'::text]))"
    ),
    "chk_dealer_fulfillment_allocation_values": (
        "CHECK (allocation_seq >= 0 AND points > 0 AND cost_basis_cents > 0)"
    ),
    "chk_dealer_fulfillment_hop_state": (
        "CHECK (state = ANY (ARRAY['reserved'::text, 'settled'::text, "
        "'cancelled'::text, 'manual_review'::text]))"
    ),
    "chk_dealer_fulfillment_plan_state": (
        "CHECK (state = ANY (ARRAY['reserved'::text, 'settling'::text, "
        "'settled'::text, 'cancelled'::text, 'manual_review'::text]))"
    ),
    "chk_dealer_fulfillment_plan_values": (
        "CHECK (points > 0 AND standard_reference_cents > 0 AND "
        "final_sale_amount_cents > 0 AND plan_version = 2)"
    ),
    "chk_dealer_hop_profit_status": (
        "CHECK (status = ANY (ARRAY['pending'::text, 'available'::text, "
        "'reversed'::text]))"
    ),
    "chk_dealer_hop_transfer_values": (
        "CHECK (cost_basis_cents >= 0 AND balance_paid_after >= 0 AND "
        "btrim(transfer_id) <> ''::text)"
    ),
    "chk_dealer_lot_acquisition_cost_positive": (
        "CHECK (acquisition_cost_cents > 0)"
    ),
    "chk_dealer_lot_allocation_status": (
        "CHECK (status = ANY (ARRAY['reserved'::text, 'consumed'::text, "
        "'restored'::text, 'cancelled'::text]))"
    ),
    "chk_dealer_lot_allocation_values": (
        "CHECK (allocation_seq >= 0 AND points > 0 AND cost_basis_cents >= 0)"
    ),
    "chk_dealer_lot_points_conservation": (
        "CHECK (remaining_points >= 0 AND reserved_points >= 0 AND "
        "(remaining_points + reserved_points) <= original_points)"
    ),
    "chk_dealer_lot_points_positive": "CHECK (original_points > 0)",
    "chk_dealer_lot_status": (
        "CHECK (status = ANY (ARRAY['active'::text, 'consumed'::text, "
        "'refund_pending'::text, 'reversed'::text]))"
    ),
    "chk_dealer_manufacturer_issue_values": (
        "CHECK (points > 0 AND acquisition_cost_cents > 0 AND "
        "btrim(pricing_version) <> ''::text AND btrim(idempotency_key) <> ''::text)"
    ),
    "chk_dealer_profit_cost_positive": "CHECK (seller_cost_basis_cents > 0)",
    "chk_dealer_profit_money": (
        "CHECK (seller_cost_basis_cents >= 0 AND sale_amount_cents >= 0 AND "
        "margin_cents >= 0 AND sale_amount_cents = "
        "(seller_cost_basis_cents + margin_cents))"
    ),
    "chk_dealer_profit_status": (
        "CHECK (status = ANY (ARRAY['pending'::text, 'available'::text, "
        "'reversed'::text]))"
    ),
    "chk_dealer_refund_execution_money": (
        "CHECK (refund_amount_cents >= 0 AND attempt_count >= 0)"
    ),
    "chk_dealer_refund_processing_cost": (
        "CHECK (processing_cost_cents >= 0 AND "
        "jsonb_typeof(processing_cost_evidence_jsonb) = 'object'::text)"
    ),
    "chk_dealer_refund_status": (
        "CHECK (status = ANY (ARRAY['requested'::text, 'processing'::text, "
        "'completed'::text, 'rejected'::text, 'manual_review'::text]))"
    ),
    "chk_dealer_resale_order_cost_positive": (
        "CHECK (seller_cost_basis_cents > 0)"
    ),
    "chk_dealer_resale_order_money": (
        "CHECK (seller_cost_basis_cents >= 0 AND sale_amount_cents > 0 AND "
        "margin_cents >= 0 AND sale_amount_cents = "
        "(seller_cost_basis_cents + margin_cents))"
    ),
    "chk_dealer_resale_order_paid_fields": (
        "CHECK ((state = ANY (ARRAY['reserved'::text, 'cancelled'::text])) AND "
        "paid_at IS NULL AND refund_deadline IS NULL AND transfer_id IS NULL OR "
        "(state = ANY (ARRAY['paid'::text, 'refund_pending'::text, "
        "'refunded'::text])) AND paid_at IS NOT NULL AND refund_deadline IS NOT NULL "
        "AND transfer_id IS NOT NULL)"
    ),
    "chk_dealer_resale_order_points": "CHECK (points > 0)",
    "chk_dealer_resale_order_state": (
        "CHECK (state = ANY (ARRAY['reserved'::text, 'paid'::text, "
        "'refund_pending'::text, 'refunded'::text, 'cancelled'::text]))"
    ),
    "chk_dealer_resale_promo_status": (
        "CHECK (status = ANY (ARRAY['draft'::text, 'active'::text, "
        "'ended'::text, 'archived'::text]))"
    ),
    "chk_dealer_transfer_cost_positive": "CHECK (seller_cost_basis_cents > 0)",
    "chk_dealer_transfer_entry_cost": (
        "CHECK (cost_basis_cents >= 0 AND balance_paid_after >= 0)"
    ),
    "chk_dealer_transfer_money": (
        "CHECK (seller_cost_basis_cents >= 0 AND sale_amount_cents >= 0 AND "
        "margin_cents >= 0 AND sale_amount_cents = "
        "(seller_cost_basis_cents + margin_cents))"
    ),
    "chk_dealer_transfer_points": "CHECK (points > 0)",
    "chk_dealer_transfer_state": (
        "CHECK (state = ANY (ARRAY['settled'::text, 'refunded'::text]))"
    ),
    "chk_service_refund_cash_amount": (
        "CHECK (amount_cents > 0 AND attempt_count >= 0)"
    ),
    "chk_service_refund_cash_status": (
        "CHECK (status = ANY (ARRAY['queued'::text, 'provider_processing'::text, "
        "'completed'::text, 'failed'::text, 'manual_review'::text]))"
    ),
    "chk_service_refund_funding_status": (
        "CHECK (status = ANY (ARRAY['open'::text, 'recovering'::text, "
        "'manual_review'::text, 'closed'::text]))"
    ),
    "chk_service_refund_liability_money": (
        "CHECK (refund_amount_cents > 0 AND pending_settlement_offset_cents >= 0 "
        "AND reserve_offset_cents >= 0 AND negative_settlement_cents >= 0 AND "
        "refund_amount_cents = (pending_settlement_offset_cents + "
        "reserve_offset_cents + negative_settlement_cents))"
    ),
    "chk_service_refund_liability_status": (
        "CHECK (status = ANY (ARRAY['covered'::text, 'negative_settlement'::text, "
        "'recovered'::text, 'closed'::text]))"
    ),
    "chk_service_refund_reserve_values": (
        "CHECK (available_cents >= 0 AND row_version >= 1)"
    ),
    "chk_subscription_refund_status": (
        "CHECK (status = ANY (ARRAY['requested'::text, 'platform_review'::text, "
        "'approved'::text, 'cash_pending'::text, 'completed'::text, "
        "'rejected'::text, 'manual_review'::text]))"
    ),
    # UNIQUE/PRIMARY KEY constraints also pin their backing index column order.
    "ux_consumer_refund_source": "UNIQUE (source_order_id)",
    "ux_dealer_consumer_allocation": "UNIQUE (order_id, seller_lot_id)",
    "ux_dealer_consumer_allocation_seq": "UNIQUE (order_id, allocation_seq)",
    "ux_dealer_consumer_transfer_side": "UNIQUE (transfer_id, direction)",
    "ux_dealer_fulfillment_allocation_seq": (
        "UNIQUE (plan_id, hop_seq, allocation_seq)"
    ),
    "ux_dealer_fulfillment_root_hop": "UNIQUE (root_order_id, hop_seq)",
    "ux_dealer_hop_profit": "UNIQUE (plan_id, hop_seq)",
    "ux_dealer_hop_transfer_direction": "UNIQUE (plan_id, hop_seq, direction)",
    "ux_dealer_hop_transfer_entry": "UNIQUE (plan_id, hop_seq, entry_seq)",
    "ux_dealer_lot_allocation": "UNIQUE (order_id, seller_lot_id)",
    "ux_dealer_lot_allocation_seq": "UNIQUE (order_id, allocation_seq)",
    "ux_dealer_resale_order_quote": "UNIQUE (quote_id)",
    "ux_dealer_resale_order_transfer": "UNIQUE (transfer_id)",
    "ux_dealer_transfer_entry": (
        "UNIQUE (transfer_id, owner_agent_user_id, direction)"
    ),
    "ux_purchase_acceptance_global_once": "PRIMARY KEY (acceptance_id)",
    "ux_purchase_acceptance_ref": "UNIQUE (purchase_kind, purchase_ref_id)",
})

STRICT_INDEX_DEFINITIONS = {
    "ux_dealer_resale_external_ref_global": (
        "dealer_resale_refunds",
        True,
        "CREATE UNIQUE INDEX ux_dealer_resale_external_ref_global ON "
        "dealer_resale_refunds USING btree (lower(btrim(external_channel)), "
        "btrim(external_refund_id)) WHERE ((external_channel IS NOT NULL) AND "
        "(external_refund_id IS NOT NULL))",
        "((external_channel IS NOT NULL) AND (external_refund_id IS NOT NULL))",
    ),
    "idx_dealer_fulfillment_allocation_lot": (
        "dealer_resale_fulfillment_allocations",
        False,
        "CREATE INDEX idx_dealer_fulfillment_allocation_lot ON "
        "dealer_resale_fulfillment_allocations USING btree (source_lot_id) "
        "WHERE (source_lot_id IS NOT NULL)",
        "(source_lot_id IS NOT NULL)",
    ),
    "idx_dealer_fulfillment_hop_buyer": (
        "dealer_resale_fulfillment_hops",
        False,
        "CREATE INDEX idx_dealer_fulfillment_hop_buyer ON "
        "dealer_resale_fulfillment_hops USING btree (buyer_user_id, created_at DESC)",
        "",
    ),
    "idx_dealer_fulfillment_hop_seller": (
        "dealer_resale_fulfillment_hops",
        False,
        "CREATE INDEX idx_dealer_fulfillment_hop_seller ON "
        "dealer_resale_fulfillment_hops USING btree (seller_user_id, created_at DESC)",
        "",
    ),
    "idx_dealer_hop_profit_maturity": (
        "dealer_resale_hop_profit_ledger",
        False,
        "CREATE INDEX idx_dealer_hop_profit_maturity ON "
        "dealer_resale_hop_profit_ledger USING btree (status, available_at, profit_id)",
        "",
    ),
    "idx_dealer_hop_profit_seller": (
        "dealer_resale_hop_profit_ledger",
        False,
        "CREATE INDEX idx_dealer_hop_profit_seller ON "
        "dealer_resale_hop_profit_ledger USING btree (seller_user_id, created_at DESC)",
        "",
    ),
    "idx_dealer_lot_promotion_fifo_v2": (
        "dealer_inventory_lots",
        False,
        "CREATE INDEX idx_dealer_lot_promotion_fifo_v2 ON dealer_inventory_lots "
        "USING btree (owner_agent_user_id, promotion_campaign_id, "
        "promotion_product_code, promotion_root_catalog_version, acquired_at, lot_id) "
        "WHERE ((status = 'active'::text) AND (remaining_points > 0))",
        "((status = 'active'::text) AND (remaining_points > 0))",
    ),
    "idx_dealer_resale_promotion_window": (
        "dealer_resale_promotions",
        False,
        "CREATE INDEX idx_dealer_resale_promotion_window ON dealer_resale_promotions "
        "USING btree (status, started_at, expires_at)",
        "",
    ),
    "ux_dealer_lot_root_hop": (
        "dealer_inventory_lots",
        True,
        "CREATE UNIQUE INDEX ux_dealer_lot_root_hop ON dealer_inventory_lots "
        "USING btree (root_order_id, source_hop_seq) WHERE "
        "((root_order_id IS NOT NULL) AND (source_hop_seq IS NOT NULL))",
        "((root_order_id IS NOT NULL) AND (source_hop_seq IS NOT NULL))",
    ),
    "ux_dealer_resale_active_promotion": (
        "dealer_resale_promotions",
        True,
        "CREATE UNIQUE INDEX ux_dealer_resale_active_promotion ON "
        "dealer_resale_promotions USING btree (funding_scope, "
        "COALESCE(seller_user_id, 0), product_code, root_catalog_version) "
        "WHERE (status = 'active'::text)",
        "(status = 'active'::text)",
    ),
    "ux_dealer_lot_source_order": (
        "dealer_inventory_lots",
        True,
        "CREATE UNIQUE INDEX ux_dealer_lot_source_order ON dealer_inventory_lots "
        "USING btree (source_order_id) WHERE (source_order_id IS NOT NULL)",
        "(source_order_id IS NOT NULL)",
    ),
    "ux_recharge_terms_acceptance_once": (
        "recharge_orders",
        True,
        "CREATE UNIQUE INDEX ux_recharge_terms_acceptance_once ON recharge_orders "
        "USING btree (((pricing_snapshot_jsonb ->> 'terms_acceptance_id'::text))) "
        "WHERE (pricing_snapshot_jsonb ? 'terms_acceptance_id'::text)",
        "(pricing_snapshot_jsonb ? 'terms_acceptance_id'::text)",
    ),
    "ux_service_refund_provider_ref": (
        "service_refund_cash_jobs",
        True,
        "CREATE UNIQUE INDEX ux_service_refund_provider_ref ON "
        "service_refund_cash_jobs USING btree "
        "(original_payment_route, provider_refund_id) "
        "WHERE (provider_refund_id IS NOT NULL)",
        "(provider_refund_id IS NOT NULL)",
    ),
    "ux_service_refund_provider_ref_global": (
        "service_refund_cash_jobs",
        True,
        "CREATE UNIQUE INDEX ux_service_refund_provider_ref_global ON "
        "service_refund_cash_jobs USING btree (provider_refund_id) "
        "WHERE (provider_refund_id IS NOT NULL)",
        "(provider_refund_id IS NOT NULL)",
    ),
    "ux_subscription_terms_acceptance_once": (
        "user_social_subscriptions",
        True,
        "CREATE UNIQUE INDEX ux_subscription_terms_acceptance_once ON "
        "user_social_subscriptions USING btree (purchase_terms_acceptance_id) "
        "WHERE (purchase_terms_acceptance_id IS NOT NULL)",
        "(purchase_terms_acceptance_id IS NOT NULL)",
    ),
    "ux_consumer_refund_source": (
        "consumer_refund_cases", True,
        "CREATE UNIQUE INDEX ux_consumer_refund_source ON consumer_refund_cases "
        "USING btree (source_order_id)", "",
    ),
    "ux_dealer_consumer_allocation": (
        "dealer_consumer_lot_allocations", True,
        "CREATE UNIQUE INDEX ux_dealer_consumer_allocation ON "
        "dealer_consumer_lot_allocations USING btree (order_id, seller_lot_id)", "",
    ),
    "ux_dealer_consumer_allocation_seq": (
        "dealer_consumer_lot_allocations", True,
        "CREATE UNIQUE INDEX ux_dealer_consumer_allocation_seq ON "
        "dealer_consumer_lot_allocations USING btree (order_id, allocation_seq)", "",
    ),
    "ux_dealer_consumer_transfer_side": (
        "dealer_consumer_transfer_entries", True,
        "CREATE UNIQUE INDEX ux_dealer_consumer_transfer_side ON "
        "dealer_consumer_transfer_entries USING btree (transfer_id, direction)", "",
    ),
    "ux_dealer_fulfillment_allocation_seq": (
        "dealer_resale_fulfillment_allocations", True,
        "CREATE UNIQUE INDEX ux_dealer_fulfillment_allocation_seq ON "
        "dealer_resale_fulfillment_allocations USING btree "
        "(plan_id, hop_seq, allocation_seq)", "",
    ),
    "ux_dealer_fulfillment_root_hop": (
        "dealer_resale_fulfillment_hops", True,
        "CREATE UNIQUE INDEX ux_dealer_fulfillment_root_hop ON "
        "dealer_resale_fulfillment_hops USING btree (root_order_id, hop_seq)", "",
    ),
    "ux_dealer_hop_profit": (
        "dealer_resale_hop_profit_ledger", True,
        "CREATE UNIQUE INDEX ux_dealer_hop_profit ON "
        "dealer_resale_hop_profit_ledger USING btree (plan_id, hop_seq)", "",
    ),
    "ux_dealer_hop_transfer_direction": (
        "dealer_resale_hop_transfer_entries", True,
        "CREATE UNIQUE INDEX ux_dealer_hop_transfer_direction ON "
        "dealer_resale_hop_transfer_entries USING btree (plan_id, hop_seq, direction)", "",
    ),
    "ux_dealer_hop_transfer_entry": (
        "dealer_resale_hop_transfer_entries", True,
        "CREATE UNIQUE INDEX ux_dealer_hop_transfer_entry ON "
        "dealer_resale_hop_transfer_entries USING btree (plan_id, hop_seq, entry_seq)", "",
    ),
    "ux_dealer_lot_allocation": (
        "dealer_inventory_lot_allocations", True,
        "CREATE UNIQUE INDEX ux_dealer_lot_allocation ON "
        "dealer_inventory_lot_allocations USING btree (order_id, seller_lot_id)", "",
    ),
    "ux_dealer_lot_allocation_seq": (
        "dealer_inventory_lot_allocations", True,
        "CREATE UNIQUE INDEX ux_dealer_lot_allocation_seq ON "
        "dealer_inventory_lot_allocations USING btree (order_id, allocation_seq)", "",
    ),
    "ux_dealer_resale_order_quote": (
        "dealer_resale_orders", True,
        "CREATE UNIQUE INDEX ux_dealer_resale_order_quote ON dealer_resale_orders "
        "USING btree (quote_id)", "",
    ),
    "ux_dealer_resale_order_transfer": (
        "dealer_resale_orders", True,
        "CREATE UNIQUE INDEX ux_dealer_resale_order_transfer ON dealer_resale_orders "
        "USING btree (transfer_id)", "",
    ),
    "ux_dealer_transfer_entry": (
        "dealer_inventory_transfer_entries", True,
        "CREATE UNIQUE INDEX ux_dealer_transfer_entry ON "
        "dealer_inventory_transfer_entries USING btree "
        "(transfer_id, owner_agent_user_id, direction)", "",
    ),
    "ux_purchase_acceptance_global_once": (
        "purchase_agreement_acceptance_uses", True,
        "CREATE UNIQUE INDEX ux_purchase_acceptance_global_once ON "
        "purchase_agreement_acceptance_uses USING btree (acceptance_id)", "",
    ),
    "ux_purchase_acceptance_ref": (
        "purchase_agreement_acceptance_uses", True,
        "CREATE UNIQUE INDEX ux_purchase_acceptance_ref ON "
        "purchase_agreement_acceptance_uses USING btree "
        "(purchase_kind, purchase_ref_id)", "",
    ),
}


TRIGGER_CONTRACTS = {
    "trg_dealer_consumer_sale_immutable": (
        "dealer_consumer_sales", "O", "dealer_consumer_sale_immutable_guard",
        "CREATE TRIGGER trg_dealer_consumer_sale_immutable BEFORE UPDATE ON "
        "dealer_consumer_sales FOR EACH ROW EXECUTE FUNCTION "
        "dealer_consumer_sale_immutable_guard()",
    ),
    "trg_dealer_fulfillment_hop_immutable": (
        "dealer_resale_fulfillment_hops", "O",
        "dealer_resale_fulfillment_hop_immutable_guard",
        "CREATE TRIGGER trg_dealer_fulfillment_hop_immutable BEFORE UPDATE ON "
        "dealer_resale_fulfillment_hops FOR EACH ROW EXECUTE FUNCTION "
        "dealer_resale_fulfillment_hop_immutable_guard()",
    ),
    "trg_dealer_fulfillment_plan_immutable": (
        "dealer_resale_fulfillment_plans", "O",
        "dealer_resale_fulfillment_plan_immutable_guard",
        "CREATE TRIGGER trg_dealer_fulfillment_plan_immutable BEFORE UPDATE ON "
        "dealer_resale_fulfillment_plans FOR EACH ROW EXECUTE FUNCTION "
        "dealer_resale_fulfillment_plan_immutable_guard()",
    ),
    "trg_dealer_resale_order_immutable": (
        "dealer_resale_orders", "O", "dealer_resale_order_immutable_guard",
        "CREATE TRIGGER trg_dealer_resale_order_immutable BEFORE UPDATE ON "
        "dealer_resale_orders FOR EACH ROW EXECUTE FUNCTION "
        "dealer_resale_order_immutable_guard()",
    ),
    "trg_recharge_purchase_acceptance_use": (
        "recharge_orders", "O", "consume_purchase_acceptance_use",
        "CREATE TRIGGER trg_recharge_purchase_acceptance_use AFTER INSERT OR "
        "UPDATE OF pricing_snapshot_jsonb ON recharge_orders FOR EACH ROW "
        "EXECUTE FUNCTION consume_purchase_acceptance_use()",
    ),
    "trg_subscription_purchase_acceptance_use": (
        "user_social_subscriptions", "O", "consume_purchase_acceptance_use",
        "CREATE TRIGGER trg_subscription_purchase_acceptance_use AFTER INSERT OR "
        "UPDATE OF purchase_terms_acceptance_id ON user_social_subscriptions "
        "FOR EACH ROW EXECUTE FUNCTION consume_purchase_acceptance_use()",
    ),
}


class ResaleError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _row_dict(row, columns: Iterable[str] = ()) -> Dict[str, Any]:
    if not row:
        return {}
    if isinstance(row, dict):
        return dict(row)
    return dict(zip(columns, row))


def _json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _json_array(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = []
    return [dict(item) for item in value] if isinstance(value, list) else []


def _normalize_contract_ddl(value: Any) -> str:
    return "".join(str(value or "").lower().split()).replace('"', "").replace("public.", "")


def _normalize_schema_definition(value: Any) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").strip()).lower()
    return normalized.replace("public.", "").replace('"', "")


def _original_payment_route(recharge: Dict[str, Any]) -> str:
    """Prefer a persisted dispatch route, but do not let legacy 'unknown' hide the order method."""
    actual = str(recharge.get("actual_payment_channel") or "").strip().lower()
    method = str(recharge.get("payment_method") or "").strip().lower()
    return actual if actual and actual != "unknown" else (method or "unknown")


def _stable_id(prefix: str, value: str) -> str:
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24].upper()


def _ceil_bps(cents: int, bps: int) -> int:
    return (int(cents) * int(bps) + 9999) // 10000


def _ceil_ratio(value: int, numerator: int, denominator: int) -> int:
    value = int(value)
    numerator = int(numerator)
    denominator = int(denominator)
    if value < 0 or numerator < 0 or denominator <= 0:
        raise ResaleError("PRICE_RATIO_INVALID", "价格比例参数非法")
    return (value * numerator + denominator - 1) // denominator


def calculate_hop_sale(
    standard_reference_cents: int,
    edge_multiplier_bps: int,
    *, promotion_discount_bps: int = 10000,
) -> int:
    """Calculate one aggregate hop exactly once with integer ceiling.

    ``promotion_discount_bps`` is a payable multiplier (9000 = nine-tenths),
    not a percentage-off amount.  It is only passed on the sponsored hop; a
    platform campaign is applied to the root reference before this function is
    called for every edge.
    """
    reference = int(standard_reference_cents)
    multiplier = int(edge_multiplier_bps)
    discount = int(promotion_discount_bps)
    if reference <= 0 or multiplier < MIN_MARKUP_BPS or not 1 <= discount <= 10000:
        raise ResaleError("HOP_PRICE_INVALID", "标准参考价、关系倍率或促销折扣非法")
    sale = _ceil_ratio(reference, multiplier * discount, 10000 * 10000)
    if sale <= 0 or sale > MAX_MONEY_CENTS:
        raise ResaleError("SALE_AMOUNT_INVALID", "本次逐级转售金额超出允许范围")
    return sale


def proportional_reference(total_cents: int, part_points: int, total_points: int) -> int:
    """Allocate a hop's aggregate reference to one JIT shortfall without floats."""
    total = int(total_cents)
    part = int(part_points)
    points = int(total_points)
    if total <= 0 or part <= 0 or points <= 0 or part > points:
        raise ResaleError("REFERENCE_ALLOCATION_INVALID", "JIT 标准参考价分配非法")
    return _ceil_ratio(total, part, points)


def verify_funds_conservation(
    *, final_paid_cents: int, platform_root_revenue_cents: int,
    dealer_margin_cents: int, final_seller_margin_cents: int,
    historical_principal_cents: int = 0, explicit_fee_tax_cents: int = 0,
) -> Dict[str, int]:
    """Fail closed on any unassigned or duplicated cent.

    Historical-lot principal is separately attributed to its existing lineage;
    it is never paid to an ancestor again.  A fully-JIT chain has zero historical
    principal and therefore matches the business equation verbatim.
    """
    values = {
        "final_paid_cents": int(final_paid_cents),
        "platform_root_revenue_cents": int(platform_root_revenue_cents),
        "dealer_margin_cents": int(dealer_margin_cents),
        "final_seller_margin_cents": int(final_seller_margin_cents),
        "historical_principal_cents": int(historical_principal_cents),
        "explicit_fee_tax_cents": int(explicit_fee_tax_cents),
    }
    if min(values.values()) < 0:
        raise ResaleError("FUNDS_CONSERVATION_INVALID", "资金守恒输入含负数")
    assigned = sum(
        values[key] for key in (
            "platform_root_revenue_cents", "dealer_margin_cents",
            "final_seller_margin_cents", "historical_principal_cents",
            "explicit_fee_tax_cents",
        )
    )
    residual = values["final_paid_cents"] - assigned
    if residual != 0:
        raise ResaleError(
            "FUNDS_CONSERVATION_MISMATCH",
            f"资金分配不守恒：实付 {values['final_paid_cents']}，已归属 {assigned}，差额 {residual}",
        )
    return {**values, "assigned_cents": assigned, "residual_cents": 0}


def verify_persisted_funds_ledger(
    cur, *, root_order_id: str, require_withdrawable: bool = False,
) -> Dict[str, int]:
    """Verify the durable accounting rows, not only the immutable proof JSON.

    Platform-root revenue is an internal platform classification in the hop
    ledger.  Only non-platform historical principal plus margin may enter the
    agent withdrawal ledger.  JIT incoming cost was assigned to an earlier hop
    and therefore must never be paid to the current seller again.
    """
    cur.execute(
        """SELECT final_sale_amount_cents,state
           FROM dealer_resale_fulfillment_plans
           WHERE root_order_id=%s""",
        (str(root_order_id),),
    )
    plan = _row_dict(cur.fetchone())
    if not plan:
        raise ResaleError("JIT_PLAN_REQUIRED", "资金账本缺少 JIT 履约计划")
    cur.execute(
        """SELECT
             COALESCE(SUM(platform_root_revenue_cents),0) AS platform_root_cents,
             COALESCE(SUM(agent_payable_cents),0) AS hop_agent_payable_cents,
             COALESCE(SUM(CASE WHEN agent_payable_cents>0
                               AND revenue_ledger_id IS NULL THEN 1 ELSE 0 END),0)
                 AS missing_hop_revenue_count,
             COALESCE(SUM(CASE WHEN agent_payable_cents>0
                               AND status<>'available' THEN 1 ELSE 0 END),0)
                 AS unavailable_hop_count
           FROM dealer_resale_hop_profit_ledger WHERE root_order_id=%s""",
        (str(root_order_id),),
    )
    hop = _row_dict(cur.fetchone())
    cur.execute(
        """SELECT COALESCE(agent_payable_cents,0) AS consumer_agent_payable_cents,
                  revenue_ledger_id,state
           FROM dealer_consumer_sales WHERE order_id=%s""",
        (str(root_order_id),),
    )
    consumer = _row_dict(cur.fetchone())
    platform_root = int(hop.get("platform_root_cents") or 0)
    hop_payable = int(hop.get("hop_agent_payable_cents") or 0)
    consumer_payable = int(consumer.get("consumer_agent_payable_cents") or 0)
    expected = int(plan["final_sale_amount_cents"])
    durable_assigned = platform_root + hop_payable + consumer_payable
    if durable_assigned != expected:
        raise ResaleError(
            "PERSISTED_FUNDS_LEDGER_MISMATCH",
            f"持久化资金账本不守恒：实付 {expected}，真实分账 {durable_assigned}",
        )
    cur.execute(
        """SELECT COALESCE(SUM(l.agent_settlement_cents),0) AS amount
           FROM dealer_resale_hop_profit_ledger hp
           JOIN agent_revenue_ledger l ON l.id=hp.revenue_ledger_id
           WHERE hp.root_order_id=%s""",
        (str(root_order_id),),
    )
    actual_hop = int(_row_dict(cur.fetchone()).get("amount") or 0)
    actual_consumer = 0
    consumer_status = ""
    if consumer.get("revenue_ledger_id") is not None:
        cur.execute(
            "SELECT agent_settlement_cents,status FROM agent_revenue_ledger WHERE id=%s",
            (int(consumer["revenue_ledger_id"]),),
        )
        consumer_revenue = _row_dict(cur.fetchone())
        actual_consumer = int(consumer_revenue.get("agent_settlement_cents") or 0)
        consumer_status = str(consumer_revenue.get("status") or "")
    actual_agent = actual_hop + actual_consumer
    if require_withdrawable:
        missing = int(hop.get("missing_hop_revenue_count") or 0)
        unavailable = int(hop.get("unavailable_hop_count") or 0)
        if consumer_payable and (
            consumer.get("revenue_ledger_id") is None or consumer_status != "settled"
        ):
            missing += 1
        if missing or unavailable or actual_agent != hop_payable + consumer_payable:
            raise ResaleError(
                "WITHDRAWABLE_LEDGER_MISMATCH",
                "真实代理应付尚未完整进入可提现账本",
            )
    return {
        "final_paid_cents": expected,
        "platform_root_revenue_cents": platform_root,
        "hop_agent_payable_cents": hop_payable,
        "consumer_agent_payable_cents": consumer_payable,
        "durable_assigned_cents": durable_assigned,
        "actual_agent_ledger_cents": actual_agent,
        "residual_cents": expected - durable_assigned,
    }


def _order_xact_lock(cur, order_id: str) -> None:
    """Cross-worker financial serialization for one durable order."""
    require_xact_scope(cur, where="dealer_inventory_resale._order_xact_lock")  # §1 硬闸:autocommit 下取事务锁=没锁
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 913715))",
        (str(order_id),),
    )


def _canonical_refund_provider(provider: str) -> str:
    value = str(provider or "").strip().lower()
    if value in {"wechat_pay", "wechat_jsapi", "wechat_native"}:
        return "wechat"
    if value in {"xunhupay_manual", "xunhupay_callback"}:
        return "xunhupay"
    return value


def _required_refund_provider(provider: str) -> str:
    """Canonicalize the closed provider set used as the global proof namespace."""
    value = _canonical_refund_provider(provider)
    if value not in {"wechat", "xunhupay"}:
        raise ResaleError(
            "EXTERNAL_REFUND_PROVIDER_UNSUPPORTED",
            "外部退款凭证的支付通道不在受信任范围内",
        )
    return value


def _claim_external_refund_proof(
    cur, *, provider: str, external_refund_id: str, refund_scope: str,
    local_ref: str, amount_cents: int, evidence: Dict[str, Any],
) -> None:
    """Claim one provider proof globally across consumer and B2B refunds."""
    provider_name = _required_refund_provider(provider)
    external_id = str(external_refund_id or "").strip()
    scope = str(refund_scope or "").strip()
    reference = str(local_ref or "").strip()
    amount = int(amount_cents)
    if not provider_name or not external_id or scope not in {"consumer", "dealer_b2b"}:
        raise ResaleError("EXTERNAL_REFUND_EVIDENCE_REQUIRED", "外部退款凭证标识不完整")
    require_xact_scope(cur, where="dealer_inventory_resale._claim_external_refund_proof")  # §1 硬闸:autocommit 下取事务锁=没锁
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 913719))",
        (f"{provider_name}:{external_id}",),
    )
    cur.execute(
        """INSERT INTO external_refund_proof_registry(
               provider,external_refund_id,refund_scope,local_ref,amount_cents,evidence_jsonb
           ) VALUES (%s,%s,%s,%s,%s,%s::jsonb)
           ON CONFLICT DO NOTHING
           RETURNING proof_id""",
        (
            provider_name, external_id, scope, reference, amount,
            json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
        ),
    )
    if cur.fetchone():
        return
    cur.execute(
        """SELECT refund_scope,local_ref,amount_cents
           FROM external_refund_proof_registry
           WHERE provider=%s AND external_refund_id=%s FOR UPDATE""",
        (provider_name, external_id),
    )
    owner = _row_dict(cur.fetchone())
    if (
        str(owner.get("refund_scope") or "") != scope
        or str(owner.get("local_ref") or "") != reference
        or int(owner.get("amount_cents") or 0) != amount
    ):
        raise ResaleError(
            "REFUND_IDEMPOTENCY_CONFLICT",
            "该外部退款凭证已用于另一笔退款，禁止重复冲销",
        )


def _trusted_recharge_refund_proof(
    recharge: Dict[str, Any], *, expected_amount_cents: int
) -> Dict[str, Any]:
    """Return canonical signed provider proof persisted by the payment callback."""
    expected_amount = int(expected_amount_cents)
    snapshot = _json_object(recharge.get("settlement_snapshot_jsonb"))
    provider_name = _required_refund_provider(snapshot.get("refund_provider"))
    provider_ref = str(snapshot.get("provider_refund_id") or "").strip()
    evidence_kind = str(snapshot.get("refund_evidence_kind") or "").strip()
    trusted_kinds = {
        "wechat": {"signed_wechat_callback", "wechat_refund_query"},
        "xunhupay": {
            "signed_xunhupay_callback",
            "signed_xunhupay_refund_response",
        },
    }
    completed_at = recharge.get("refund_completed_at")
    if (
        str(recharge.get("refund_status") or "").lower() not in {"completed", "pending_review"}
        or completed_at is None
        or not provider_name
        or not provider_ref
        or evidence_kind not in trusted_kinds.get(provider_name, set())
        or (
            evidence_kind == "signed_xunhupay_callback"
            and snapshot.get("provider_refund_reference_kind")
            != "signed_xunhupay_refund_response"
        )
    ):
        raise ResaleError(
            "CONSUMER_REFUND_PROVIDER_PROOF_MISSING",
            "原支付渠道尚未留下可信退款终态、外部退款号和验签证据",
        )
    if int(recharge.get("refunded_amount_cents") or 0) != expected_amount:
        raise ResaleError("CONSUMER_REFUND_AMOUNT_MISMATCH", "支付渠道退款金额与已批准金额不一致")
    route = _original_payment_route(recharge)
    if route.startswith("wechat") and provider_name != "wechat":
        raise ResaleError("CONSUMER_REFUND_PROVIDER_MISMATCH", "退款终态与原微信支付路径不一致")
    if route == "xunhupay" and provider_name != "xunhupay":
        raise ResaleError("CONSUMER_REFUND_PROVIDER_MISMATCH", "退款终态与原虎皮椒支付路径不一致")
    if not route.startswith("wechat") and route != "xunhupay":
        raise ResaleError("CONSUMER_REFUND_PROVIDER_MISMATCH", "原支付路径没有可自动核验的退款回调")
    completed_at_value = (
        completed_at.isoformat() if hasattr(completed_at, "isoformat") else str(completed_at)
    )
    return {
        "provider": provider_name,
        "original_payment_route": route,
        "provider_refund_id": provider_ref,
        "refund_evidence_kind": evidence_kind,
        "provider_refund_reference_kind": str(
            snapshot.get("provider_refund_reference_kind") or ""
        ),
        "payment_transaction_id": str(snapshot.get("payment_transaction_id") or ""),
        "refund_completed_at": completed_at_value,
        "refunded_amount_cents": expected_amount,
        "source": "recharge_orders.settlement_snapshot_jsonb",
    }


def schema_status(cur) -> Dict[str, Any]:
    cur.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname=current_schema() AND tablename=ANY(%s)",
        (sorted(REQUIRED_TABLES),),
    )
    present = {
        (row.get("tablename") if isinstance(row, dict) else row[0])
        for row in cur.fetchall()
    }
    missing_tables = sorted(REQUIRED_TABLES - present)
    required_columns = {
        "recharge_orders": {
            "id", "user_id", "agent_user_id", "amount_cents", "base_points",
            "bonus_points", "payment_status", "paid_at", "refund_status",
            "refund_completed_at", "refunded_amount_cents", "pricing_snapshot_jsonb",
            "settlement_snapshot_jsonb", "pricing_catalog_version", "price_quote_id",
            "agent_inventory_writer_generation", "actual_payment_channel", "created_at",
        },
        "price_quotes": {
            "quote_id", "buyer_user_id", "quote_type", "catalog_version",
            "points_granted", "bonus_points", "final_price_cents", "status",
            "used_order_id", "pricing_snapshot_jsonb", "seller_policy_version",
        },
        "agent_inventory_wallets": {
            "agent_user_id", "paid_inventory_points", "bonus_inventory_points",
            "frozen_inventory_points", "total_purchased_points", "total_allocated_points",
        },
        "agent_revenue_ledger": {
            "id", "agent_user_id", "recharge_order_id", "customer_user_id",
            "customer_paid_cents", "factory_cents", "agent_margin_before_tax_cents",
            "agent_settlement_cents", "status", "settle_at", "settled_at",
            "manual_review_required", "reversed_at", "reversed_by_ledger_id",
        },
        "user_wallets": {"user_id", "paid_points", "bonus_points"},
        "customer_agent_credit_wallets": {
            "customer_user_id", "agent_user_id", "tool_credit_points",
            "publish_credit_points", "bonus_credit_points",
        },
        "customer_credit_transactions": {
            "customer_user_id", "agent_user_id", "type", "points", "related_order_id",
            "source",
        },
        "channel_pricing_relationships": {
            "buyer_dealer_id", "upstream_channel_account_id", "relationship_version",
            "cost_multiplier_bps", "status", "effective_to",
        },
        "dealer_resale_global_settings": {
            "platform_seller_user_id", "default_downstream_markup_bps",
            "settings_version", "row_version",
        },
        "agent_inventory_transactions": {
            "type", "transfer_id", "lot_id", "counterparty_user_id", "cost_basis_cents"
        },
        "dealer_inventory_lots": {
            "lot_id", "owner_agent_user_id", "original_points", "remaining_points",
            "reserved_points", "acquisition_cost_cents", "remaining_cost_cents",
            "reserved_cost_cents", "pricing_version", "quote_id", "root_order_id",
            "source_hop_seq", "standard_reference_cents", "promotion_campaign_id",
            "promotion_funding_scope", "promotion_snapshot_jsonb",
            "promotion_product_code", "promotion_root_catalog_version",
        },
        "dealer_manufacturer_lot_issuances": {
            "issue_id", "lot_id", "platform_seller_user_id", "points",
            "acquisition_cost_cents", "pricing_version", "idempotency_key", "evidence_jsonb",
        },
        "dealer_inventory_lot_allocations": {
            "allocation_id", "order_id", "seller_lot_id", "points", "cost_basis_cents", "status",
        },
        "dealer_inventory_transfers": {
            "transfer_id", "order_id", "seller_user_id", "buyer_user_id", "points",
            "seller_cost_basis_cents", "sale_amount_cents", "margin_cents", "state",
        },
        "dealer_inventory_transfer_entries": {
            "entry_id", "transfer_id", "owner_agent_user_id", "counterparty_user_id",
            "direction", "points_delta", "cost_basis_cents", "balance_paid_after",
        },
        "dealer_resale_orders": {
            "seller_user_id", "buyer_user_id", "points", "seller_lot_allocations",
            "seller_cost_basis_cents", "sale_amount_cents", "margin_cents",
            "downstream_markup_bps", "pricing_version", "quote_id", "payment_collector",
            "refund_responsible_user_id", "refund_deadline", "state",
        },
        "dealer_resale_refunds": {
            "refund_id", "order_id", "requested_by_user_id",
            "responsible_seller_user_id", "reason_category", "processing_cost_cents",
            "processing_cost_evidence_jsonb", "mandatory_evidence_jsonb", "status",
            "refund_amount_cents", "attempt_count", "last_error",
            "execution_updated_at",
        },
        "dealer_consumer_sales": {
            "seller_user_id", "consumer_user_id", "points", "seller_lot_allocations",
            "seller_cost_basis_cents", "sale_amount_cents", "margin_cents",
            "downstream_markup_bps", "pricing_version", "quote_id", "payment_collector",
            "refund_responsible_user_id", "digital_goods_policy_code",
            "digital_goods_acknowledged_at", "state", "revenue_ledger_id",
            "principal_recovery_cents", "agent_payable_cents",
        },
        "dealer_consumer_lot_allocations": {
            "allocation_id", "order_id", "seller_lot_id", "points", "cost_basis_cents",
            "refunded_points", "refunded_cost_cents", "status",
        },
        "dealer_consumer_transfer_entries": {
            "entry_id", "transfer_id", "order_id", "seller_user_id", "consumer_user_id",
            "direction", "points_delta", "cost_basis_cents",
        },
        "dealer_resale_profit_ledger": {
            "profit_id", "order_id", "seller_user_id", "buyer_user_id",
            "seller_cost_basis_cents", "sale_amount_cents", "margin_cents",
            "status", "available_at", "revenue_ledger_id",
        },
        "consumer_refund_lot_restorations": {
            "restoration_id", "case_id", "source_order_id", "seller_lot_id",
            "allocation_id", "points", "cost_basis_cents",
        },
        "service_refund_reserve_accounts": {
            "service_user_id", "available_cents", "row_version",
        },
        "service_refund_liability_ledger": {
            "liability_id", "case_id", "source_order_id", "service_user_id",
            "refund_amount_cents", "pending_settlement_offset_cents",
            "reserve_offset_cents", "negative_settlement_cents", "status",
        },
        "service_refund_funding_work_orders": {
            "work_order_id", "liability_id", "service_user_id", "shortage_cents",
            "status", "resolution_jsonb",
        },
        "consumer_refund_cases": {
            "case_id", "consumer_user_id", "responsible_service_user_id", "source_order_id",
            "reason_category", "decision_kind", "eligibility_jsonb", "status", "cash_status",
            "refund_amount_cents", "revoked_paid_points", "revoked_bonus_points",
            "restored_inventory_points", "revenue_reversed_cents", "row_version",
            "mandatory_evidence_status",
        },
        "service_refund_cash_jobs": {
            "cash_job_id", "case_id", "source_order_id", "consumer_user_id",
            "responsible_service_user_id", "amount_cents", "original_payment_route",
            "status", "provider_refund_id", "provider_evidence_jsonb", "attempt_count",
        },
        "external_refund_proof_registry": {
            "proof_id", "provider", "external_refund_id", "refund_scope", "local_ref",
            "amount_cents", "evidence_jsonb", "claimed_at",
        },
        "purchase_agreement_acceptances": {
            "acceptance_id", "user_id", "agreement_type", "agreement_version",
            "content_hash", "accepted_at", "ip_address", "user_agent", "surface",
            "evidence_jsonb",
        },
        "purchase_agreement_acceptance_uses": {
            "acceptance_id", "purchase_kind", "purchase_ref_id", "used_at",
        },
        "agreement_signatures": {
            "user_id", "agreement_type", "agreement_version", "content_hash",
            "ip_address", "user_agent", "evidence_jsonb",
        },
        "agent_factory_agreements": {
            "agent_user_id", "version", "status", "content_hash", "signed_at",
            "signed_ip", "signed_ua", "rejected_at", "rejected_reason",
        },
        "user_social_subscriptions": {"user_id", "order_id", "purchase_terms_acceptance_id"},
        "subscription_refund_cases": {
            "case_id", "subscription_id", "source_order_id", "consumer_user_id",
            "reason_category", "decision_kind", "mandatory_evidence_status", "status",
            "usage_snapshot_jsonb", "request_evidence_jsonb",
        },
        "dealer_resale_promotions": {
            "campaign_id", "funding_scope", "sponsor_user_id", "seller_user_id",
            "product_code", "root_catalog_version", "promotion_discount_bps",
            "eligibility_jsonb", "status", "campaign_version", "row_version",
            "started_at", "expires_at",
        },
        "dealer_resale_fulfillment_plans": {
            "plan_id", "root_order_id", "quote_id", "order_kind",
            "final_seller_user_id", "final_buyer_user_id", "points", "product_code",
            "root_catalog_version", "standard_reference_cents",
            "final_sale_amount_cents", "promotion_snapshot_jsonb",
            "immutable_snapshot_jsonb", "funds_conservation_jsonb", "idempotency_key",
            "plan_version", "state",
        },
        "dealer_resale_fulfillment_hops": {
            "plan_id", "root_order_id", "hop_seq", "seller_user_id", "buyer_user_id",
            "points", "existing_inventory_points", "jit_shortfall_points", "mint_points",
            "standard_reference_cents", "seller_cost_basis_cents", "sale_amount_cents",
            "principal_recovery_cents", "agent_payable_cents", "margin_cents",
            "edge_multiplier_bps", "relationship_version", "source_kind",
            "promotion_campaign_id", "promotion_discount_bps", "promotion_funding_scope",
            "promotion_sponsor_user_id", "promotion_snapshot_jsonb", "state",
            "transfer_id", "acquired_lot_id", "idempotency_key",
        },
        "dealer_resale_fulfillment_allocations": {
            "allocation_id", "plan_id", "hop_seq", "allocation_seq", "allocation_kind",
            "source_lot_id", "source_hop_seq", "points", "cost_basis_cents",
            "promotion_campaign_id", "status",
        },
        "dealer_resale_hop_transfer_entries": {
            "entry_id", "plan_id", "root_order_id", "hop_seq", "entry_seq",
            "transfer_id", "owner_agent_user_id", "counterparty_user_id", "direction",
            "points_delta", "lot_id", "cost_basis_cents", "balance_paid_after",
        },
        "dealer_resale_hop_profit_ledger": {
            "profit_id", "plan_id", "root_order_id", "hop_seq", "seller_user_id",
            "buyer_user_id", "seller_cost_basis_cents", "sale_amount_cents",
            "principal_recovery_cents", "margin_cents", "platform_root_revenue_cents",
            "agent_payable_cents", "status", "available_at", "available_since",
            "reversed_at", "revenue_ledger_id",
        },
        "dealer_consumer_jit_refund_lots": {
            "case_id", "source_order_id", "seller_user_id", "restored_lot_id",
            "points", "cost_basis_cents",
        },
    }
    missing_columns: List[str] = []
    column_contracts = {
        ("dealer_resale_hop_profit_ledger", "principal_recovery_cents"): ("bigint", "NO"),
        ("dealer_resale_hop_profit_ledger", "agent_payable_cents"): ("bigint", "NO"),
        ("dealer_resale_hop_profit_ledger", "platform_root_revenue_cents"): ("bigint", "NO"),
        ("dealer_resale_hop_profit_ledger", "margin_cents"): ("bigint", "NO"),
        ("dealer_resale_fulfillment_hops", "principal_recovery_cents"): ("bigint", "NO"),
        ("dealer_resale_fulfillment_hops", "agent_payable_cents"): ("bigint", "NO"),
        ("dealer_resale_fulfillment_hops", "seller_cost_basis_cents"): ("bigint", "NO"),
        ("dealer_resale_fulfillment_hops", "sale_amount_cents"): ("bigint", "NO"),
        ("dealer_consumer_sales", "principal_recovery_cents"): ("bigint", "YES"),
        ("dealer_consumer_sales", "agent_payable_cents"): ("bigint", "YES"),
        ("dealer_consumer_sales", "seller_cost_basis_cents"): ("bigint", "NO"),
        ("dealer_consumer_sales", "sale_amount_cents"): ("bigint", "NO"),
        ("dealer_inventory_lots", "pricing_version"): ("text", "NO"),
        ("dealer_inventory_lots", "promotion_product_code"): ("text", "YES"),
        ("dealer_inventory_lots", "promotion_root_catalog_version"): ("text", "YES"),
    }
    actual_column_contracts: Dict[tuple[str, str], tuple[str, str]] = {}
    for table, expected in required_columns.items():
        cur.execute(
            "SELECT column_name,data_type,is_nullable FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name=%s",
            (table,),
        )
        column_rows = [_row_dict(row, ("column_name", "data_type", "is_nullable")) for row in cur.fetchall()]
        have = {str(row.get("column_name")) for row in column_rows}
        for row in column_rows:
            key = (table, str(row.get("column_name")))
            if key in column_contracts:
                actual_column_contracts[key] = (
                    str(row.get("data_type")), str(row.get("is_nullable")),
                )
        missing_columns.extend(f"{table}.{col}" for col in sorted(expected - have))
    critical_column_shapes = {
        "dealer_resale_global_settings": {
            "row_version": ("bigint", "NO"),
        },
        "dealer_resale_policies": {
            "row_version": ("bigint", "NO"),
        },
        "dealer_inventory_lots": {
            "original_points": ("bigint", "NO"), "remaining_points": ("bigint", "NO"),
            "reserved_points": ("bigint", "NO"), "acquisition_cost_cents": ("bigint", "NO"),
            "remaining_cost_cents": ("bigint", "NO"), "reserved_cost_cents": ("bigint", "NO"),
            "evidence_jsonb": ("jsonb", "NO"),
        },
        "dealer_manufacturer_lot_issuances": {
            "points": ("bigint", "NO"), "acquisition_cost_cents": ("bigint", "NO"),
            "evidence_jsonb": ("jsonb", "NO"),
        },
        "dealer_resale_orders": {
            "points": ("bigint", "NO"), "seller_cost_basis_cents": ("bigint", "NO"),
            "sale_amount_cents": ("bigint", "NO"), "margin_cents": ("bigint", "NO"),
            "seller_lot_allocations": ("jsonb", "NO"),
        },
        "dealer_consumer_sales": {
            "points": ("bigint", "NO"), "seller_cost_basis_cents": ("bigint", "NO"),
            "sale_amount_cents": ("bigint", "NO"), "margin_cents": ("bigint", "NO"),
            "seller_lot_allocations": ("jsonb", "NO"),
        },
        "dealer_inventory_lot_allocations": {
            "points": ("bigint", "NO"), "cost_basis_cents": ("bigint", "NO"),
        },
        "dealer_inventory_transfers": {
            "points": ("bigint", "NO"), "seller_cost_basis_cents": ("bigint", "NO"),
            "sale_amount_cents": ("bigint", "NO"), "margin_cents": ("bigint", "NO"),
        },
        "dealer_inventory_transfer_entries": {
            "points_delta": ("bigint", "NO"), "cost_basis_cents": ("bigint", "NO"),
            "balance_paid_after": ("bigint", "NO"),
        },
        "dealer_consumer_lot_allocations": {
            "points": ("bigint", "NO"), "cost_basis_cents": ("bigint", "NO"),
            "refunded_points": ("bigint", "NO"), "refunded_cost_cents": ("bigint", "NO"),
        },
        "dealer_consumer_transfer_entries": {
            "points_delta": ("bigint", "NO"), "cost_basis_cents": ("bigint", "NO"),
        },
        "dealer_resale_profit_ledger": {
            "seller_cost_basis_cents": ("bigint", "NO"),
            "sale_amount_cents": ("bigint", "NO"), "margin_cents": ("bigint", "NO"),
        },
        "dealer_resale_refunds": {
            "processing_cost_cents": ("bigint", "NO"),
            "processing_cost_evidence_jsonb": ("jsonb", "NO"),
            "mandatory_evidence_jsonb": ("jsonb", "NO"),
        },
        "consumer_refund_lot_restorations": {
            "points": ("bigint", "NO"), "cost_basis_cents": ("bigint", "NO"),
        },
        "service_refund_reserve_accounts": {
            "available_cents": ("bigint", "NO"), "row_version": ("bigint", "NO"),
        },
        "service_refund_liability_ledger": {
            "refund_amount_cents": ("bigint", "NO"),
            "pending_settlement_offset_cents": ("bigint", "NO"),
            "reserve_offset_cents": ("bigint", "NO"),
            "negative_settlement_cents": ("bigint", "NO"),
        },
        "service_refund_funding_work_orders": {
            "shortage_cents": ("bigint", "NO"), "resolution_jsonb": ("jsonb", "NO"),
        },
        "service_refund_cash_jobs": {
            "amount_cents": ("bigint", "NO"), "provider_evidence_jsonb": ("jsonb", "NO"),
            "status": ("text", "NO"), "attempt_count": ("integer", "NO"),
        },
        "external_refund_proof_registry": {
            "amount_cents": ("bigint", "NO"), "evidence_jsonb": ("jsonb", "NO"),
            "provider": ("text", "NO"), "external_refund_id": ("text", "NO"),
            "refund_scope": ("text", "NO"), "local_ref": ("text", "NO"),
        },
        "consumer_refund_cases": {
            "refund_amount_cents": ("bigint", "NO"), "revoked_paid_points": ("bigint", "NO"),
            "revoked_bonus_points": ("bigint", "NO"), "restored_inventory_points": ("bigint", "NO"),
            "revenue_reversed_cents": ("bigint", "NO"), "row_version": ("bigint", "NO"),
            "eligibility_jsonb": ("jsonb", "NO"), "execution_jsonb": ("jsonb", "NO"),
        },
    }
    invalid_column_shapes: List[str] = []
    for table, expected in critical_column_shapes.items():
        cur.execute(
            """SELECT column_name,data_type,is_nullable FROM information_schema.columns
               WHERE table_schema=current_schema() AND table_name=%s""",
            (table,),
        )
        actual = {
            _row_dict(row)["column_name"]: (
                _row_dict(row)["data_type"], _row_dict(row)["is_nullable"]
            )
            for row in cur.fetchall()
        }
        for column, shape in expected.items():
            if actual.get(column) != shape:
                invalid_column_shapes.append(f"{table}.{column}")
    invalid_column_contracts = sorted(
        f"{table}.{column}"
        for (table, column), expected in column_contracts.items()
        if actual_column_contracts.get((table, column)) != expected
    )
    # Readiness is also the blue/green tamper detector.  Check every named
    # integrity constraint introduced by this migration, not a hand-picked
    # subset that could let a disabled money/state guard pass unnoticed.
    required_constraints = {
        "chk_consumer_refund_execution", "chk_consumer_refund_parties",
        "chk_consumer_refund_policy", "chk_consumer_refund_status",
        "chk_consumer_refund_decision_kind", "chk_consumer_refund_cash_status",
        "chk_consumer_refund_amounts", "chk_consumer_refund_eligibility_object",
        "chk_consumer_refund_mandatory_evidence",
        "chk_consumer_refund_lot_values", "chk_dealer_consumer_allocation_refunded",
        "chk_service_refund_reserve_values", "chk_service_refund_liability_money",
        "chk_service_refund_liability_status", "chk_service_refund_funding_shortage",
        "chk_service_refund_funding_status", "chk_service_refund_funding_resolution",
        "chk_service_refund_cash_amount", "chk_service_refund_cash_route",
        "chk_service_refund_cash_status", "chk_service_refund_cash_evidence",
        "chk_purchase_acceptance_nonblank", "chk_purchase_acceptance_evidence",
        "chk_purchase_acceptance_kind", "ux_purchase_acceptance_global_once",
        "ux_purchase_acceptance_ref", "chk_subscription_refund_decision",
        "chk_subscription_refund_mandatory_evidence", "chk_subscription_refund_status",
        "chk_subscription_refund_json", "fk_subscription_refund_subscription",
        "chk_recharge_actual_payment_channel", "fk_subscription_purchase_terms_acceptance",
        "chk_dealer_consumer_allocation_status", "chk_dealer_consumer_allocation_values",
        "chk_dealer_consumer_sale_allocations", "chk_dealer_consumer_sale_collector",
        "chk_dealer_consumer_sale_cost_positive", "chk_dealer_consumer_sale_markup",
        "chk_dealer_consumer_sale_money", "chk_dealer_consumer_sale_paid_fields",
        "chk_dealer_consumer_sale_parties", "chk_dealer_consumer_sale_points",
        "chk_dealer_consumer_sale_policy", "chk_dealer_consumer_sale_refund_owner",
        "chk_dealer_consumer_sale_state", "chk_dealer_consumer_sale_version",
        "chk_dealer_consumer_transfer_cost", "chk_dealer_consumer_transfer_direction",
        "chk_dealer_consumer_transfer_sign", "chk_dealer_lot_acquisition_cost_positive",
        "chk_dealer_lot_allocation_status", "chk_dealer_lot_allocation_values",
        "chk_dealer_lot_cost_conservation", "chk_dealer_lot_evidence_object",
        "chk_dealer_lot_id_nonblank", "chk_dealer_lot_points_conservation",
        "chk_dealer_lot_points_positive", "chk_dealer_lot_pricing_version",
        "chk_dealer_lot_source_kind", "chk_dealer_lot_status",
        "chk_dealer_manufacturer_issue_evidence", "chk_dealer_manufacturer_issue_values",
        "chk_dealer_profit_cost_positive", "chk_dealer_profit_money",
        "chk_dealer_profit_parties", "chk_dealer_profit_status",
        "chk_dealer_refund_audit_object", "chk_dealer_refund_status",
        "chk_dealer_refund_processing_cost", "chk_dealer_refund_mandatory_evidence",
        "chk_dealer_refund_execution_money",
        "chk_dealer_resale_default_markup", "chk_dealer_resale_global_singleton",
        "chk_dealer_resale_order_allocations", "chk_dealer_resale_order_collector",
        "chk_dealer_resale_order_cost_positive", "chk_dealer_resale_order_markup",
        "chk_dealer_resale_order_money", "chk_dealer_resale_order_paid_fields",
        "chk_dealer_resale_order_parties", "chk_dealer_resale_order_points",
        "chk_dealer_resale_order_refund_owner", "chk_dealer_resale_order_source",
        "chk_dealer_resale_order_source_allocations", "chk_dealer_resale_order_state",
        "chk_dealer_resale_order_versions", "chk_dealer_resale_policy_bounds",
        "chk_dealer_resale_policy_markup", "chk_dealer_resale_policy_row_version",
        "chk_dealer_resale_policy_version", "chk_dealer_resale_settings_row_version",
        "chk_dealer_resale_settings_version", "chk_dealer_transfer_cost_positive",
        "chk_dealer_transfer_delta_sign", "chk_dealer_transfer_direction",
        "chk_dealer_transfer_entry_cost", "chk_dealer_transfer_money",
        "chk_dealer_transfer_parties", "chk_dealer_transfer_points",
        "chk_dealer_transfer_state", "fk_dealer_consumer_transfer_sale",
        "fk_dealer_lot_source_transfer", "fk_dealer_order_transfer",
        "ux_consumer_refund_source", "ux_consumer_refund_lot_restoration",
        "ux_dealer_consumer_allocation",
        "ux_dealer_consumer_allocation_seq", "ux_dealer_consumer_transfer_side",
        "ux_dealer_lot_allocation", "ux_dealer_lot_allocation_seq",
        "ux_dealer_resale_order_quote", "ux_dealer_resale_order_transfer",
        "ux_dealer_transfer_entry", "consumer_refund_cases_source_order_id_fkey",
        "chk_external_refund_proof_nonblank", "chk_external_refund_proof_scope",
        "chk_external_refund_proof_provider",
        "chk_external_refund_proof_amount", "chk_external_refund_proof_evidence",
        "ux_external_refund_proof_key", "ux_external_refund_proof_local",
        "agent_inventory_transactions_type_check",
        "customer_credit_transactions_source_check",
        "chk_dealer_resale_promo_identity", "chk_dealer_resale_promo_scope",
        "chk_dealer_resale_promo_discount", "chk_dealer_resale_promo_window",
        "chk_dealer_resale_promo_status", "chk_dealer_resale_promo_version",
        "chk_dealer_resale_promo_eligibility", "chk_dealer_lot_jit_lineage",
        "chk_dealer_lot_standard_reference", "chk_dealer_lot_promotion_scope",
        "chk_dealer_lot_promotion_snapshot", "chk_dealer_lot_promotion_lineage_v2",
        "chk_dealer_fulfillment_plan_identity",
        "chk_dealer_fulfillment_plan_parties", "chk_dealer_fulfillment_plan_values",
        "chk_dealer_fulfillment_plan_kind", "chk_dealer_fulfillment_plan_state",
        "chk_dealer_fulfillment_plan_json", "chk_dealer_fulfillment_hop_parties",
        "chk_dealer_fulfillment_hop_values", "chk_dealer_fulfillment_hop_money",
        "chk_dealer_fulfillment_hop_payout_v2", "chk_dealer_fulfillment_hop_edge",
        "chk_dealer_fulfillment_hop_source",
        "chk_dealer_fulfillment_hop_promotion", "chk_dealer_fulfillment_hop_state",
        "fk_dealer_fulfillment_allocation_hop", "ux_dealer_fulfillment_allocation_seq",
        "chk_dealer_fulfillment_allocation_values", "chk_dealer_fulfillment_allocation_kind",
        "chk_dealer_fulfillment_allocation_state", "fk_dealer_hop_transfer_hop",
        "ux_dealer_hop_transfer_entry", "ux_dealer_hop_transfer_direction",
        "chk_dealer_hop_transfer_parties", "chk_dealer_hop_transfer_direction",
        "chk_dealer_hop_transfer_sign", "chk_dealer_hop_transfer_values",
        "fk_dealer_hop_profit_hop", "ux_dealer_hop_profit",
        "chk_dealer_hop_profit_parties", "chk_dealer_hop_profit_money",
        "chk_dealer_hop_profit_payout_v2", "chk_dealer_hop_profit_status",
        "chk_dealer_consumer_sale_payout_v2", "ux_dealer_fulfillment_root_hop",
        "chk_dealer_consumer_jit_refund_values",
    }
    def expected_constraint_table(name: str) -> str:
        exact = {
            "consumer_refund_cases_source_order_id_fkey": "consumer_refund_cases",
            "ux_consumer_refund_source": "consumer_refund_cases",
            "ux_consumer_refund_lot_restoration": "consumer_refund_lot_restorations",
            "ux_dealer_consumer_allocation": "dealer_consumer_lot_allocations",
            "ux_dealer_consumer_allocation_seq": "dealer_consumer_lot_allocations",
            "ux_dealer_consumer_transfer_side": "dealer_consumer_transfer_entries",
            "ux_dealer_lot_allocation": "dealer_inventory_lot_allocations",
            "ux_dealer_lot_allocation_seq": "dealer_inventory_lot_allocations",
            "ux_dealer_resale_order_quote": "dealer_resale_orders",
            "ux_dealer_resale_order_transfer": "dealer_resale_orders",
            "ux_dealer_transfer_entry": "dealer_inventory_transfer_entries",
            "fk_dealer_consumer_transfer_sale": "dealer_consumer_transfer_entries",
            "fk_dealer_lot_source_transfer": "dealer_inventory_lots",
            "fk_dealer_order_transfer": "dealer_resale_orders",
            "fk_subscription_refund_subscription": "subscription_refund_cases",
            "fk_subscription_purchase_terms_acceptance": "user_social_subscriptions",
            "chk_recharge_actual_payment_channel": "recharge_orders",
            "chk_consumer_refund_lot_values": "consumer_refund_lot_restorations",
            "chk_dealer_transfer_entry_cost": "dealer_inventory_transfer_entries",
            "chk_dealer_transfer_direction": "dealer_inventory_transfer_entries",
            "chk_dealer_transfer_delta_sign": "dealer_inventory_transfer_entries",
            "chk_external_refund_proof_nonblank": "external_refund_proof_registry",
            "chk_external_refund_proof_scope": "external_refund_proof_registry",
            "chk_external_refund_proof_provider": "external_refund_proof_registry",
            "chk_external_refund_proof_amount": "external_refund_proof_registry",
            "chk_external_refund_proof_evidence": "external_refund_proof_registry",
            "ux_external_refund_proof_key": "external_refund_proof_registry",
            "ux_external_refund_proof_local": "external_refund_proof_registry",
            "agent_inventory_transactions_type_check": "agent_inventory_transactions",
            "customer_credit_transactions_source_check": "customer_credit_transactions",
            "fk_dealer_fulfillment_allocation_hop": "dealer_resale_fulfillment_allocations",
            "ux_dealer_fulfillment_allocation_seq": "dealer_resale_fulfillment_allocations",
            "fk_dealer_hop_transfer_hop": "dealer_resale_hop_transfer_entries",
            "ux_dealer_hop_transfer_entry": "dealer_resale_hop_transfer_entries",
            "ux_dealer_hop_transfer_direction": "dealer_resale_hop_transfer_entries",
            "fk_dealer_hop_profit_hop": "dealer_resale_hop_profit_ledger",
            "ux_dealer_hop_profit": "dealer_resale_hop_profit_ledger",
            "ux_dealer_fulfillment_root_hop": "dealer_resale_fulfillment_hops",
            "chk_dealer_consumer_jit_refund_values": "dealer_consumer_jit_refund_lots",
        }
        if name in exact:
            return exact[name]
        prefixes = (
            ("chk_consumer_refund_", "consumer_refund_cases"),
            ("chk_dealer_resale_promo_", "dealer_resale_promotions"),
            ("chk_dealer_fulfillment_plan_", "dealer_resale_fulfillment_plans"),
            ("chk_dealer_fulfillment_hop_", "dealer_resale_fulfillment_hops"),
            ("chk_dealer_fulfillment_allocation_", "dealer_resale_fulfillment_allocations"),
            ("chk_dealer_hop_transfer_", "dealer_resale_hop_transfer_entries"),
            ("chk_dealer_hop_profit_", "dealer_resale_hop_profit_ledger"),
            ("chk_dealer_consumer_allocation_", "dealer_consumer_lot_allocations"),
            ("chk_dealer_consumer_sale_", "dealer_consumer_sales"),
            ("chk_dealer_consumer_transfer_", "dealer_consumer_transfer_entries"),
            ("chk_dealer_lot_allocation_", "dealer_inventory_lot_allocations"),
            ("chk_dealer_lot_", "dealer_inventory_lots"),
            ("chk_dealer_manufacturer_", "dealer_manufacturer_lot_issuances"),
            ("chk_dealer_profit_", "dealer_resale_profit_ledger"),
            ("chk_dealer_refund_", "dealer_resale_refunds"),
            ("chk_dealer_resale_policy_", "dealer_resale_policies"),
            ("chk_dealer_resale_order_", "dealer_resale_orders"),
            ("chk_dealer_resale_", "dealer_resale_global_settings"),
            ("chk_dealer_transfer_", "dealer_inventory_transfers"),
            ("chk_service_refund_reserve_", "service_refund_reserve_accounts"),
            ("chk_service_refund_liability_", "service_refund_liability_ledger"),
            ("chk_service_refund_funding_", "service_refund_funding_work_orders"),
            ("chk_service_refund_cash_", "service_refund_cash_jobs"),
            ("chk_purchase_acceptance_nonblank", "purchase_agreement_acceptances"),
            ("chk_purchase_acceptance_evidence", "purchase_agreement_acceptances"),
            ("chk_purchase_acceptance_", "purchase_agreement_acceptance_uses"),
            ("ux_purchase_acceptance_", "purchase_agreement_acceptance_uses"),
            ("chk_subscription_refund_", "subscription_refund_cases"),
        )
        for prefix, table in prefixes:
            if name.startswith(prefix):
                return table
        raise KeyError(name)

    # Keep the executable inventory coupled to the table-bound declaration.
    # The explicit literal above documents migration coverage; this comparison
    # makes any future list drift a fail-closed readiness blocker.
    constraint_inventory_drift = sorted(
        required_constraints.symmetric_difference(REQUIRED_CONSTRAINT_TABLES)
    )
    required_constraints = set(REQUIRED_CONSTRAINT_TABLES)
    cur.execute(
        """SELECT c.relname AS table_name,pc.conname,pc.convalidated,pc.contype,
                  pg_get_constraintdef(pc.oid,true) AS definition
           FROM pg_constraint pc JOIN pg_class c ON c.oid=pc.conrelid
           JOIN pg_namespace n ON n.oid=c.relnamespace
           WHERE n.nspname=current_schema() AND pc.conname=ANY(%s)""",
        (sorted(required_constraints),),
    )
    constraint_rows = [
        _row_dict(
            row,
            ("table_name", "conname", "convalidated", "contype", "definition"),
        )
        for row in cur.fetchall()
    ]
    constraint_objects = {
        (str(row.get("table_name")), str(row.get("conname"))): row
        for row in constraint_rows
    }
    constraint_definitions = {
        (str(row.get("table_name")), str(row.get("conname"))):
            _normalize_schema_definition(row.get("definition"))
        for row in constraint_rows
    }
    invalid_constraints = sorted(
        name for name, table in REQUIRED_CONSTRAINT_TABLES.items()
        if not constraint_objects.get((table, name))
        or not bool(constraint_objects[(table, name)].get("convalidated"))
    )
    invalid_constraint_objects = list(invalid_constraints)
    invalid_constraint_definitions = sorted(
        name for name, expected in STRICT_CONSTRAINT_DEFINITIONS.items()
        if constraint_definitions.get((REQUIRED_CONSTRAINT_TABLES[name], name))
           != _normalize_schema_definition(expected)
    )
    # Keep the legacy aggregate contract additive without conflating a missing
    # object with a present-but-tampered definition.  Exact names remain in the
    # strict field; older gates still receive a deterministic digest marker.
    if invalid_constraint_definitions:
        invalid_constraints = sorted(set(invalid_constraints).union(
            {"schema_contract_digest"},
        ))
    # The two legacy transaction whitelists are also compared as exact value sets.
    legacy_check_contracts = {
        "agent_inventory_transactions_type_check": (
            "agent_inventory_transactions", LEGACY_INVENTORY_TRANSACTION_TYPES,
        ),
        "customer_credit_transactions_source_check": (
            "customer_credit_transactions", LEGACY_CUSTOMER_CREDIT_SOURCES,
        ),
    }
    cur.execute(
        "SELECT c.conname,rel.relname AS table_name,"
        "pg_get_constraintdef(c.oid) AS definition "
        "FROM pg_constraint c JOIN pg_class rel ON rel.oid=c.conrelid "
        "JOIN pg_namespace ns ON ns.oid=rel.relnamespace "
        "WHERE ns.nspname=current_schema() AND c.conname=ANY(%s)",
        (sorted(legacy_check_contracts),),
    )
    legacy_definitions = {
        str(parsed.get("conname")): str(parsed.get("definition") or "")
        for raw in cur.fetchall()
        for parsed in [_row_dict(raw)]
        if str(parsed.get("table_name") or "")
           == legacy_check_contracts[str(parsed.get("conname"))][0]
    }
    invalid_legacy_check_values: List[str] = []
    for name, (_, expected_values) in legacy_check_contracts.items():
        actual_values = {
            value.replace("''", "'")
            for value in re.findall(r"'((?:''|[^'])*)'", legacy_definitions.get(name, ""))
        }
        if actual_values != set(expected_values):
            invalid_legacy_check_values.append(name)
    index_names = set(STRICT_INDEX_DEFINITIONS)
    cur.execute(
        """SELECT c.relname AS index_name,rel.relname AS table_name,
                  i.indisunique,i.indisvalid,i.indisready,
                  pg_get_indexdef(i.indexrelid) AS definition,
                  COALESCE(pg_get_expr(i.indpred,i.indrelid),'') AS predicate
           FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
           JOIN pg_class rel ON rel.oid=i.indrelid
           JOIN pg_namespace ns ON ns.oid=c.relnamespace
           WHERE ns.nspname=current_schema() AND c.relname=ANY(%s)""",
        (sorted(index_names),),
    )
    indexes = {
        str(row.get("index_name")): row for row in map(_row_dict, cur.fetchall())
    }
    required_indexes = {
        "ux_dealer_lot_source_order": "dealer_inventory_lots",
        "ux_recharge_terms_acceptance_once": "recharge_orders",
        "ux_subscription_terms_acceptance_once": "user_social_subscriptions",
        "ux_service_refund_provider_ref": "service_refund_cash_jobs",
        "ux_service_refund_provider_ref_global": "service_refund_cash_jobs",
        "ux_dealer_resale_external_ref_global": "dealer_resale_refunds",
        "ux_dealer_resale_active_promotion": "dealer_resale_promotions",
        "ux_dealer_lot_root_hop": "dealer_inventory_lots",
    }
    index_fragments = {
        "ux_dealer_lot_source_order": (("(source_order_id)",), ("source_order_idisnotnull",)),
        "ux_recharge_terms_acceptance_once": (
            ("pricing_snapshot_jsonb->>'terms_acceptance_id'::text",),
            ("pricing_snapshot_jsonb?'terms_acceptance_id'::text",),
        ),
        "ux_subscription_terms_acceptance_once": (
            ("(purchase_terms_acceptance_id)",), ("purchase_terms_acceptance_idisnotnull",),
        ),
        "ux_service_refund_provider_ref": (
            ("(original_payment_route,provider_refund_id)",), ("provider_refund_idisnotnull",),
        ),
        "ux_service_refund_provider_ref_global": (
            ("(provider_refund_id)",), ("provider_refund_idisnotnull",),
        ),
        "ux_dealer_resale_external_ref_global": (
            ("lower(btrim(external_channel))", "btrim(external_refund_id)"),
            ("external_channelisnotnull", "external_refund_idisnotnull"),
        ),
        "ux_dealer_resale_active_promotion": (
            (
                "(funding_scope,coalesce(seller_user_id,0),product_code,root_catalog_version)",
            ),
            ("status='active'::text",),
        ),
        "ux_dealer_lot_root_hop": (
            ("(root_order_id,source_hop_seq)",),
            ("root_order_idisnotnull", "source_hop_seqisnotnull"),
        ),
    }
    index_key_counts = {
        "ux_dealer_lot_source_order": 1,
        "ux_recharge_terms_acceptance_once": 1,
        "ux_subscription_terms_acceptance_once": 1,
        "ux_service_refund_provider_ref": 2,
        "ux_service_refund_provider_ref_global": 1,
        "ux_dealer_resale_external_ref_global": 2,
        "ux_dealer_resale_active_promotion": 4,
        "ux_dealer_lot_root_hop": 2,
    }
    def _index_contract_ok(name: str) -> bool:
        contract = indexes.get((required_indexes[name], name)) or {}
        definition_fragments, predicate_fragments = index_fragments[name]
        return (
            bool(contract.get("valid"))
            and bool(contract.get("unique"))
            and contract.get("key_count") == index_key_counts[name]
            and all(
            _normalize_contract_ddl(fragment) in str(contract.get("definition") or "")
            for fragment in definition_fragments
        ) and all(
            _normalize_contract_ddl(fragment) in str(contract.get("predicate") or "")
            for fragment in predicate_fragments
        ))
    invalid_indexes = sorted(
        name for name, (table, unique, _, _) in STRICT_INDEX_DEFINITIONS.items()
        if not indexes.get(name)
        or str(indexes[name].get("table_name")) != table
        or bool(indexes[name].get("indisunique")) != bool(unique)
        or not bool(indexes[name].get("indisvalid"))
        or not bool(indexes[name].get("indisready"))
    )
    invalid_index_definitions = sorted(
        name for name, (table, unique, definition, predicate)
        in STRICT_INDEX_DEFINITIONS.items()
        if not indexes.get(name)
        or str(indexes[name].get("table_name")) != table
        or bool(indexes[name].get("indisunique")) != bool(unique)
        or not bool(indexes[name].get("indisvalid"))
        or not bool(indexes[name].get("indisready"))
        or _normalize_schema_definition(indexes[name].get("definition"))
           != _normalize_schema_definition(definition)
        or _normalize_schema_definition(indexes[name].get("predicate"))
           != _normalize_schema_definition(predicate)
    )
    cur.execute(
        """SELECT tgname,tgenabled,rel.relname AS table_name,
                  proc.proname AS function_name,
                  pg_get_triggerdef(t.oid,true) AS definition
           FROM pg_trigger t
           JOIN pg_class rel ON rel.oid=t.tgrelid
           JOIN pg_namespace ns ON ns.oid=rel.relnamespace
           JOIN pg_proc proc ON proc.oid=t.tgfoid
           WHERE ns.nspname=current_schema() AND tgname=ANY(%s) AND NOT tgisinternal""",
        (sorted(TRIGGER_CONTRACTS),),
    )
    triggers = {str(row.get("tgname")): row for row in map(_row_dict, cur.fetchall())}
    invalid_triggers = sorted(
        name for name, (table, enabled, _, _) in TRIGGER_CONTRACTS.items()
        if not triggers.get(name)
        or str(triggers[name].get("table_name")) != table
        or str(triggers[name].get("tgenabled")) != enabled
    )
    invalid_trigger_definitions = sorted(
        name for name, (table, enabled, function_name, definition)
        in TRIGGER_CONTRACTS.items()
        if not triggers.get(name)
        or str(triggers[name].get("table_name")) != table
        or str(triggers[name].get("tgenabled")) != enabled
        or str(triggers[name].get("function_name")) != function_name
        or _normalize_schema_definition(triggers[name].get("definition"))
           != _normalize_schema_definition(definition)
    )
    blockers = [f"缺表 {name}" for name in missing_tables]
    blockers += [f"缺列 {name}" for name in missing_columns]
    blockers += [f"列类型或非空错误 {name}" for name in invalid_column_shapes]
    blockers += [f"列类型或 NULL 契约不符 {name}" for name in invalid_column_contracts]
    blockers += [f"readiness 约束清单漂移 {name}" for name in constraint_inventory_drift]
    blockers += [f"约束缺失或未验证 {name}" for name in invalid_constraints]
    blockers += [f"约束所属表或验证状态不符 {name}" for name in invalid_constraint_objects]
    blockers += [f"约束完整定义不符 {name}" for name in invalid_constraint_definitions]
    blockers += [f"旧流水 CHECK 白名单不完整或被篡改 {name}" for name in invalid_legacy_check_values]
    blockers += [f"唯一索引缺失或无效 {name}" for name in invalid_indexes]
    blockers += [f"索引定义或 predicate 不符 {name}" for name in invalid_index_definitions]
    blockers += [f"不可变触发器缺失或禁用 {name}" for name in invalid_triggers]
    blockers += [f"触发器所属表、时机或函数不符 {name}" for name in invalid_trigger_definitions]
    return {
        "ready": not blockers,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "invalid_column_shapes": invalid_column_shapes,
        "invalid_column_contracts": invalid_column_contracts,
        "constraint_inventory_drift": constraint_inventory_drift,
        "invalid_constraints": invalid_constraints,
        "invalid_constraint_objects": invalid_constraint_objects,
        "invalid_constraint_definitions": invalid_constraint_definitions,
        "invalid_legacy_check_values": sorted(invalid_legacy_check_values),
        "invalid_indexes": invalid_indexes,
        "invalid_index_definitions": invalid_index_definitions,
        "invalid_triggers": invalid_triggers,
        "invalid_trigger_definitions": invalid_trigger_definitions,
        "blockers": blockers,
    }


def purchase_agreement_schema_status(cur) -> Dict[str, Any]:
    """Unconditional schema gate for purchase flows used even when resale is off."""
    blockers: List[str] = []
    expected_columns = {
        "recharge_orders": {
            "pricing_snapshot_jsonb": ("jsonb", "YES"),
        },
        "purchase_agreement_acceptances": {
            "acceptance_id": ("text", "NO"), "user_id": ("integer", "NO"),
            "agreement_type": ("text", "NO"), "agreement_version": ("text", "NO"),
            "content_hash": ("text", "NO"), "accepted_at": ("timestamp with time zone", "NO"),
            "surface": ("text", "NO"), "evidence_jsonb": ("jsonb", "NO"),
        },
        "purchase_agreement_acceptance_uses": {
            "acceptance_id": ("text", "NO"), "purchase_kind": ("text", "NO"),
            "purchase_ref_id": ("text", "NO"), "used_at": ("timestamp with time zone", "NO"),
        },
        "user_social_subscriptions": {
            "purchase_terms_acceptance_id": ("text", "YES"),
        },
    }
    for table, expected in expected_columns.items():
        cur.execute(
            """SELECT column_name,data_type,is_nullable FROM information_schema.columns
               WHERE table_schema=current_schema() AND table_name=%s""",
            (table,),
        )
        actual = {
            _row_dict(row)["column_name"]: (
                _row_dict(row)["data_type"], _row_dict(row)["is_nullable"]
            )
            for row in cur.fetchall()
        }
        for column, shape in expected.items():
            if actual.get(column) != shape:
                blockers.append(f"列结构不合格 {table}.{column}")

    constraint_specs = {
        ("purchase_agreement_acceptances", "chk_purchase_acceptance_nonblank"),
        ("purchase_agreement_acceptances", "chk_purchase_acceptance_evidence"),
        ("purchase_agreement_acceptance_uses", "ux_purchase_acceptance_global_once"),
        ("purchase_agreement_acceptance_uses", "ux_purchase_acceptance_ref"),
        ("purchase_agreement_acceptance_uses", "chk_purchase_acceptance_kind"),
    }
    cur.execute(
        """SELECT c.relname AS table_name,pc.conname,pc.convalidated,pc.contype,
                  pg_get_constraintdef(pc.oid,true) AS definition
           FROM pg_constraint pc
           JOIN pg_class c ON c.oid=pc.conrelid
           JOIN pg_namespace n ON n.oid=c.relnamespace
           WHERE n.nspname=current_schema()"""
    )
    constraints = {
        (_row_dict(row)["table_name"], _row_dict(row)["conname"]): {
            "validated": bool(_row_dict(row)["convalidated"]),
            "type": str(_row_dict(row).get("contype") or ""),
            "definition": _normalize_contract_ddl(_row_dict(row).get("definition")),
        }
        for row in cur.fetchall()
    }
    exact_constraint_definitions = {
        ("purchase_agreement_acceptances", "chk_purchase_acceptance_nonblank"):
            "check(btrim(acceptance_id)<>''::textandbtrim(agreement_type)<>''::textand"
            "btrim(agreement_version)<>''::textandbtrim(content_hash)<>''::textand"
            "btrim(surface)<>''::text)",
        ("purchase_agreement_acceptances", "chk_purchase_acceptance_evidence"):
            "check(jsonb_typeof(evidence_jsonb)='object'::text)",
        ("purchase_agreement_acceptance_uses", "ux_purchase_acceptance_global_once"):
            "primarykey(acceptance_id)",
        ("purchase_agreement_acceptance_uses", "ux_purchase_acceptance_ref"):
            "unique(purchase_kind,purchase_ref_id)",
        ("purchase_agreement_acceptance_uses", "chk_purchase_acceptance_kind"):
            "check(purchase_kind=any(array['customer-recharge'::text,'subscription-checkout'::text]))",
    }
    for spec in constraint_specs:
        contract = constraints.get(spec) or {}
        expected_type = (
            "p" if spec[1] == "ux_purchase_acceptance_global_once"
            else "c" if spec[1].startswith("chk_")
            else "u"
        )
        if (
            not contract.get("validated")
            or contract.get("type") != expected_type
            or contract.get("definition") != exact_constraint_definitions[spec]
        ):
            blockers.append(f"约束缺失、错表或未验证 {spec[0]}.{spec[1]}")

    required_triggers = {
        ("recharge_orders", "trg_recharge_purchase_acceptance_use"),
        ("user_social_subscriptions", "trg_subscription_purchase_acceptance_use"),
    }
    cur.execute(
        """SELECT c.relname AS table_name,t.tgname,t.tgenabled,t.tgtype,
                  p.proname AS function_name,pn.nspname AS function_schema,
                  current_schema() AS expected_function_schema,
                  pg_get_triggerdef(t.oid,true) AS definition
           FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
           JOIN pg_proc p ON p.oid=t.tgfoid
           JOIN pg_namespace pn ON pn.oid=p.pronamespace
           JOIN pg_namespace n ON n.oid=c.relnamespace
           WHERE n.nspname=current_schema() AND NOT t.tgisinternal"""
    )
    triggers = {
        (_row_dict(row)["table_name"], _row_dict(row)["tgname"]): {
            "enabled": str(_row_dict(row)["tgenabled"]),
            "type": int(_row_dict(row).get("tgtype") or 0),
            "function": str(_row_dict(row).get("function_name") or ""),
            "function_schema": str(_row_dict(row).get("function_schema") or ""),
            "expected_function_schema": str(
                _row_dict(row).get("expected_function_schema") or ""
            ),
            "definition": _normalize_contract_ddl(_row_dict(row).get("definition")),
        }
        for row in cur.fetchall()
    }
    trigger_specs = {
        ("recharge_orders", "trg_recharge_purchase_acceptance_use"): (
            21, "consume_purchase_acceptance_use", "afterinsertorupdateofpricing_snapshot_jsonb",
        ),
        ("user_social_subscriptions", "trg_subscription_purchase_acceptance_use"): (
            21, "consume_purchase_acceptance_use", "afterinsertorupdateofpurchase_terms_acceptance_id",
        ),
    }
    for spec in required_triggers:
        contract = triggers.get(spec) or {}
        expected_type, expected_function, definition_fragment = trigger_specs[spec]
        if not (
            contract.get("enabled") in {"O", "A"}
            and contract.get("type") == expected_type
            and contract.get("function") == expected_function
            and contract.get("function_schema") == contract.get("expected_function_schema")
            and definition_fragment in str(contract.get("definition") or "")
        ):
            blockers.append(f"触发器缺失、错表或禁用 {spec[0]}.{spec[1]}")
    cur.execute(
        """SELECT n.nspname AS function_schema,p.proname AS function_name,
                  pg_get_function_identity_arguments(p.oid) AS identity_arguments,
                  pg_get_functiondef(p.oid) AS definition
           FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
           WHERE n.nspname=current_schema() AND p.proname='consume_purchase_acceptance_use'
           ORDER BY pg_get_function_identity_arguments(p.oid)"""
    )
    purchase_function_parts = [
        f"{_row_dict(row)['function_schema']}:{_row_dict(row)['function_name']}:"
        f"{_row_dict(row).get('identity_arguments') or ''}:"
        f"{_normalize_contract_ddl(_row_dict(row).get('definition'))}"
        for row in cur.fetchall()
    ]
    if hashlib.sha256("|".join(purchase_function_parts).encode("utf-8")).hexdigest() != (
        EXPECTED_PURCHASE_TRIGGER_FUNCTION_SHA256
    ):
        blockers.append("购买协议触发函数定义不合格")
    required_indexes = {
        ("recharge_orders", "ux_recharge_terms_acceptance_once"),
        ("user_social_subscriptions", "ux_subscription_terms_acceptance_once"),
    }
    cur.execute(
        """SELECT t.relname AS table_name,i.relname AS index_name,x.indisunique,x.indisvalid,
                  x.indnkeyatts AS key_count,
                  pg_get_indexdef(i.oid) AS definition,
                  pg_get_expr(x.indpred,x.indrelid) AS predicate
           FROM pg_index x JOIN pg_class i ON i.oid=x.indexrelid
           JOIN pg_class t ON t.oid=x.indrelid
           JOIN pg_namespace n ON n.oid=t.relnamespace
           WHERE n.nspname=current_schema()"""
    )
    indexes = {
        (_row_dict(row)["table_name"], _row_dict(row)["index_name"]): {
            "valid": bool(_row_dict(row)["indisunique"]) and bool(_row_dict(row)["indisvalid"]),
            "key_count": int(_row_dict(row).get("key_count") or 0),
            "definition": _normalize_contract_ddl(_row_dict(row).get("definition")),
            "predicate": _normalize_contract_ddl(_row_dict(row).get("predicate")),
        }
        for row in cur.fetchall()
    }
    index_specs = {
        ("recharge_orders", "ux_recharge_terms_acceptance_once"): (
            "pricing_snapshot_jsonb->>'terms_acceptance_id'::text",
            "pricing_snapshot_jsonb?'terms_acceptance_id'::text",
        ),
        ("user_social_subscriptions", "ux_subscription_terms_acceptance_once"): (
            "(purchase_terms_acceptance_id)", "purchase_terms_acceptance_idisnotnull",
        ),
    }
    for spec in required_indexes:
        contract = indexes.get(spec) or {}
        definition_fragment, predicate_fragment = index_specs[spec]
        if not (
            contract.get("valid")
            and contract.get("key_count") == 1
            and _normalize_contract_ddl(definition_fragment) in str(contract.get("definition") or "")
            and _normalize_contract_ddl(predicate_fragment) in str(contract.get("predicate") or "")
        ):
            blockers.append(f"唯一索引缺失、错表或无效 {spec[0]}.{spec[1]}")
    purchase_index_parts = [
        f"{table}:{name}:{contract['key_count']}:{contract['definition']}:{contract['predicate']}"
        for (table, name), contract in sorted(indexes.items())
        if (table, name) in required_indexes
    ]
    if hashlib.sha256("|".join(purchase_index_parts).encode("utf-8")).hexdigest() != (
        EXPECTED_PURCHASE_INDEX_CONTRACT_SHA256
    ):
        blockers.append("购买协议唯一索引定义不合格")
    return {"ready": not blockers, "blockers": blockers}


def assert_schema_ready(cur) -> None:
    status = schema_status(cur)
    if not status["ready"]:
        raise ResaleError("RESALE_SCHEMA_NOT_READY", "；".join(status["blockers"]))


def _global_settings(cur, *, for_update: bool = False) -> Dict[str, Any]:
    suffix = " FOR UPDATE" if for_update else ""
    cur.execute(
        "SELECT singleton_id, default_downstream_markup_bps, platform_seller_user_id, "
        "settings_version, row_version FROM dealer_resale_global_settings WHERE singleton_id=1" + suffix
    )
    row = cur.fetchone()
    if not row:
        raise ResaleError("RESALE_SETTINGS_MISSING", "逐级转售全局设置缺失")
    return _row_dict(row)


def save_global_settings(
    cur, *, platform_seller_user_id: int, default_downstream_markup_bps: int,
    settings_version: str, expected_row_version: int, updated_by: int,
    reason: str, request_id: str, updated_by_username: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    seller = int(platform_seller_user_id)
    markup = int(default_downstream_markup_bps)
    if seller <= 0 or markup < MIN_MARKUP_BPS or not str(settings_version).strip():
        raise ResaleError("RESALE_SETTINGS_INVALID", "厂家账号、默认下级系数或设置版本非法")
    if not str(reason or "").strip() or not str(request_id or "").strip():
        raise ResaleError("SETTINGS_AUDIT_REQUIRED", "管理员修改厂家设置必须提供原因和 request_id")
    cur.execute("SELECT 1 FROM users WHERE id=%s", (seller,))
    if not cur.fetchone():
        raise ResaleError("PLATFORM_SELLER_NOT_FOUND", "显式厂家账号不存在")
    cur.execute(
        "SELECT * FROM dealer_resale_global_settings WHERE singleton_id=1 FOR UPDATE"
    )
    current = _row_dict(cur.fetchone())
    if not current or int(current["row_version"]) != int(expected_row_version):
        raise ResaleError("SETTINGS_VERSION_CONFLICT", "逐级转售全局设置已变化，请刷新后重试")
    before = dict(current)
    cur.execute(
        """UPDATE dealer_resale_global_settings
           SET platform_seller_user_id=%s, default_downstream_markup_bps=%s,
               settings_version=%s, row_version=row_version+1,
               updated_by=%s, updated_at=NOW()
           WHERE singleton_id=1 RETURNING *""",
        (seller, markup, str(settings_version), int(updated_by)),
    )
    after = _row_dict(cur.fetchone())
    cur.execute(
        """INSERT INTO audit_logs(
               user_id,username,action,module,entity_type,entity_id,summary,
               before_snapshot,after_snapshot,ip_address,created_at
           ) VALUES (%s,%s,'update','dealer_resale_pricing','dealer_resale_settings',1,
                     %s,%s,%s,%s,NOW())""",
        (
            int(updated_by), updated_by_username,
            f"逐级转售全局设置更新 request_id={request_id} reason={str(reason).strip()}",
            json.dumps({"request_id": request_id, "settings": before}, ensure_ascii=False, default=str),
            json.dumps({"request_id": request_id, "settings": after}, ensure_ascii=False, default=str),
            ip_address,
        ),
    )
    return after


def _active_direct_seller(cur, buyer_user_id: int) -> Optional[Dict[str, Any]]:
    cur.execute(
        """SELECT upstream_channel_account_id AS seller_user_id, relationship_version
           FROM channel_pricing_relationships
           WHERE buyer_dealer_id=%s AND status='active' AND effective_to IS NULL""",
        (int(buyer_user_id),),
    )
    row = cur.fetchone()
    return _row_dict(row) if row else None


def _require_active_service_seller(cur, seller_user_id: int) -> None:
    """Fail closed when a direct seller was disabled or lost service status."""

    cur.execute(
        """SELECT u.is_active,COALESCE(w.agent_level,0) AS agent_level
           FROM users u LEFT JOIN user_wallets w ON w.user_id=u.id
           WHERE u.id=%s""",
        (int(seller_user_id),),
    )
    seller = _row_dict(cur.fetchone())
    active = seller.get("is_active")
    if isinstance(active, str):
        active = active.strip().lower() not in {"", "0", "false", "disabled", "inactive"}
    if not seller or not bool(active) or int(seller.get("agent_level") or 0) < 1:
        raise ResaleError(
            "SELLER_UNAVAILABLE",
            "当前进货报价不可用，请刷新后重试",
        )


def _resolve_seller(cur, buyer_user_id: int) -> Dict[str, Any]:
    direct = _active_direct_seller(cur, buyer_user_id)
    if direct:
        seller = int(direct["seller_user_id"])
        if seller == int(buyer_user_id):
            raise ResaleError("RESALE_SELF_SELLER", "直属卖方不能是买方本人")
        _require_active_service_seller(cur, seller)
        return {
            "seller_user_id": seller,
            "relationship_version": str(direct["relationship_version"]),
            "source_kind": "direct_resale",
        }
    settings = _global_settings(cur)
    platform_seller = settings.get("platform_seller_user_id")
    if platform_seller is None:
        raise ResaleError(
            "PLATFORM_SELLER_NOT_PREPARED",
            "买方没有直属上级且平台厂家账号未配置 · 拒绝报价",
        )
    if int(platform_seller) == int(buyer_user_id):
        raise ResaleError("RESALE_SELF_SELLER", "平台厂家账号不能与买方相同")
    return {
        "seller_user_id": int(platform_seller),
        "relationship_version": None,
        "source_kind": "platform_purchase",
    }


def get_effective_policy(cur, seller_user_id: int) -> Dict[str, Any]:
    cur.execute(
        """SELECT seller_user_id, downstream_markup_bps, authorized_min_markup_bps,
                  authorized_max_markup_bps, policy_version, row_version
           FROM dealer_resale_policies WHERE seller_user_id=%s""",
        (int(seller_user_id),),
    )
    row = cur.fetchone()
    if row:
        return _row_dict(row)
    settings = _global_settings(cur)
    return {
        "seller_user_id": int(seller_user_id),
        "downstream_markup_bps": int(settings["default_downstream_markup_bps"]),
        "authorized_min_markup_bps": MIN_MARKUP_BPS,
        "authorized_max_markup_bps": int(settings["default_downstream_markup_bps"]),
        "policy_version": str(settings["settings_version"]),
        "row_version": 0,
        "source": "admin_default",
    }


def save_policy_admin(
    cur, *, seller_user_id: int, downstream_markup_bps: int,
    min_markup_bps: int, max_markup_bps: int, policy_version: str,
    expected_row_version: int, updated_by: int, reason: str, request_id: str,
    updated_by_username: Optional[str] = None, ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    values = tuple(map(int, (downstream_markup_bps, min_markup_bps, max_markup_bps)))
    markup, min_bps, max_bps = values
    if min_bps < MIN_MARKUP_BPS or not min_bps <= markup <= max_bps:
        raise ResaleError("MARKUP_OUT_OF_BOUNDS", "下级售价系数必须在 admin 授权范围内且不低于 10000")
    if not str(policy_version).strip():
        raise ResaleError("POLICY_VERSION_REQUIRED", "policy_version 不能为空")
    if not str(reason or "").strip() or not str(request_id or "").strip():
        raise ResaleError("POLICY_AUDIT_REQUIRED", "管理员改价必须提供原因和 request_id")
    cur.execute("SELECT 1 FROM users WHERE id=%s", (int(seller_user_id),))
    if not cur.fetchone():
        raise ResaleError("POLICY_SELLER_NOT_FOUND", "下级售价策略所属经销商不存在")
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 913716))",
        (f"dealer-resale-policy:{int(seller_user_id)}",),
    )
    cur.execute(
        "SELECT * FROM dealer_resale_policies WHERE seller_user_id=%s FOR UPDATE",
        (int(seller_user_id),),
    )
    current = cur.fetchone()
    before = _row_dict(current) if current else None
    current_version = int((before.get("row_version") if before else 0) or 0)
    if int(expected_row_version) != current_version:
        raise ResaleError("POLICY_VERSION_CONFLICT", "下级售价策略已变化，请刷新后重试")
    cur.execute(
        """INSERT INTO dealer_resale_policies
           (seller_user_id, downstream_markup_bps, authorized_min_markup_bps,
            authorized_max_markup_bps, policy_version, row_version, updated_by, updated_at)
           VALUES (%s,%s,%s,%s,%s,1,%s,NOW())
           ON CONFLICT (seller_user_id) DO UPDATE SET
             downstream_markup_bps=EXCLUDED.downstream_markup_bps,
             authorized_min_markup_bps=EXCLUDED.authorized_min_markup_bps,
             authorized_max_markup_bps=EXCLUDED.authorized_max_markup_bps,
             policy_version=EXCLUDED.policy_version,
             row_version=dealer_resale_policies.row_version+1,
             updated_by=EXCLUDED.updated_by, updated_at=NOW()
           RETURNING *""",
        (int(seller_user_id), markup, min_bps, max_bps, str(policy_version), int(updated_by)),
    )
    after = _row_dict(cur.fetchone())
    cur.execute(
        """INSERT INTO audit_logs(
               user_id,username,action,module,entity_type,entity_id,summary,
               before_snapshot,after_snapshot,ip_address,created_at
           ) VALUES (%s,%s,'update','dealer_resale_pricing','dealer_resale_policy',%s,
                     %s,%s,%s,%s,NOW())""",
        (
            int(updated_by), updated_by_username, int(seller_user_id),
            f"经销商转售价策略更新 request_id={request_id} reason={str(reason).strip()}",
            json.dumps({"request_id": request_id, "policy": before}, ensure_ascii=False, default=str),
            json.dumps({"request_id": request_id, "policy": after}, ensure_ascii=False, default=str),
            ip_address,
        ),
    )
    return after


def save_policy_dealer(
    cur, *, seller_user_id: int, downstream_markup_bps: int,
    expected_row_version: int, policy_version: str,
) -> Dict[str, Any]:
    cur.execute(
        "SELECT * FROM dealer_resale_policies WHERE seller_user_id=%s FOR UPDATE",
        (int(seller_user_id),),
    )
    row = cur.fetchone()
    if not row:
        raise ResaleError("POLICY_ADMIN_AUTH_REQUIRED", "管理员尚未配置可调整范围")
    policy = _row_dict(row)
    if int(policy["row_version"]) != int(expected_row_version):
        raise ResaleError("POLICY_VERSION_CONFLICT", "下级售价策略已变化，请刷新后重试")
    markup = int(downstream_markup_bps)
    if not int(policy["authorized_min_markup_bps"]) <= markup <= int(policy["authorized_max_markup_bps"]):
        raise ResaleError("MARKUP_OUT_OF_BOUNDS", "下级售价系数超出管理员授权范围")
    if markup < MIN_MARKUP_BPS or not str(policy_version).strip():
        raise ResaleError("MARKUP_INVALID", "下级售价策略非法")
    cur.execute(
        """UPDATE dealer_resale_policies
           SET downstream_markup_bps=%s, policy_version=%s,
               row_version=row_version+1, updated_by=%s, updated_at=NOW()
           WHERE seller_user_id=%s RETURNING *""",
        (markup, str(policy_version), int(seller_user_id), int(seller_user_id)),
    )
    return _row_dict(cur.fetchone())


def _promotion_snapshot(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not row:
        return {}
    return {
        "campaign_id": str(row["campaign_id"]),
        "promotion_discount_bps": int(row["promotion_discount_bps"]),
        "funding_scope": str(row["funding_scope"]),
        "sponsor_user_id": (
            int(row["sponsor_user_id"]) if row.get("sponsor_user_id") is not None else None
        ),
        "seller_user_id": (
            int(row["seller_user_id"]) if row.get("seller_user_id") is not None else None
        ),
        "root_catalog_version": str(row["root_catalog_version"]),
        "product_code": str(row["product_code"]),
        "started_at": row["started_at"].isoformat(),
        "expires_at": row["expires_at"].isoformat(),
        "eligibility": _json_object(row.get("eligibility_jsonb")),
        "campaign_version": str(row["campaign_version"]),
        "row_version": int(row["row_version"]),
    }


def _normalize_promotion_eligibility(value: Any) -> Dict[str, Any]:
    """Validate and canonicalize the complete promotion predicate contract."""
    eligibility = _json_object(value)
    supported = {"buyer_user_ids", "min_points", "max_points", "order_kinds"}
    unknown = sorted(set(eligibility).difference(supported))
    if unknown:
        raise ResaleError(
            "PROMOTION_ELIGIBILITY_UNSUPPORTED",
            "促销资格包含未支持条件：" + ",".join(unknown),
        )
    def strict_positive_integer(raw: Any) -> int:
        if isinstance(raw, bool):
            raise ValueError
        if isinstance(raw, int):
            result = raw
        elif isinstance(raw, str) and re.fullmatch(r"[0-9]+", raw):
            result = int(raw)
        else:
            raise ValueError
        if result <= 0:
            raise ValueError
        return result

    normalized: Dict[str, Any] = {}
    if "buyer_user_ids" in eligibility:
        buyer_ids = eligibility["buyer_user_ids"]
        if not isinstance(buyer_ids, list) or not buyer_ids:
            raise ResaleError("PROMOTION_ELIGIBILITY_INVALID", "促销买方资格格式非法")
        try:
            normalized_ids = sorted({strict_positive_integer(value) for value in buyer_ids})
        except (TypeError, ValueError) as exc:
            raise ResaleError("PROMOTION_ELIGIBILITY_INVALID", "促销买方资格格式非法") from exc
        normalized["buyer_user_ids"] = normalized_ids

    try:
        minimum = strict_positive_integer(eligibility.get("min_points", 1))
        maximum = (
            strict_positive_integer(eligibility["max_points"])
            if "max_points" in eligibility else None
        )
    except (TypeError, ValueError) as exc:
        raise ResaleError("PROMOTION_ELIGIBILITY_INVALID", "促销算力资格格式非法") from exc
    if maximum is not None and maximum < minimum:
        raise ResaleError("PROMOTION_ELIGIBILITY_INVALID", "促销算力资格范围非法")
    if "min_points" in eligibility:
        normalized["min_points"] = minimum
    if "max_points" in eligibility:
        normalized["max_points"] = int(maximum)
    if "order_kinds" in eligibility:
        kinds = eligibility["order_kinds"]
        if not isinstance(kinds, list):
            raise ResaleError("PROMOTION_ELIGIBILITY_INVALID", "促销订单类型资格格式非法")
        normalized_kinds = sorted({str(value).upper() for value in kinds})
        if not normalized_kinds or not set(normalized_kinds).issubset({"B2B", "CONSUMER"}):
            raise ResaleError("PROMOTION_ELIGIBILITY_INVALID", "促销订单类型资格非法")
        normalized["order_kinds"] = normalized_kinds
    return normalized


def _promotion_is_eligible(
    row: Dict[str, Any], *, buyer_user_id: int, points: int, order_kind: str,
) -> bool:
    """Evaluate the deliberately small, auditable promotion eligibility contract.

    Unknown or malformed predicates fail closed.  This prevents an operator from
    believing a stored eligibility rule is enforced while the quote path silently
    ignores it.
    """
    eligibility = _normalize_promotion_eligibility(row.get("eligibility_jsonb"))
    buyer_ids = eligibility.get("buyer_user_ids")
    if buyer_ids is not None:
        if int(buyer_user_id) not in set(buyer_ids):
            return False
    minimum = int(eligibility.get("min_points", 1))
    maximum = int(eligibility.get("max_points", points))
    if not minimum <= int(points) <= maximum:
        return False
    kinds = eligibility.get("order_kinds")
    if kinds is not None:
        if str(order_kind).upper() not in set(kinds):
            return False
    return True


def save_promotion_admin(
    cur, *, campaign_id: str, funding_scope: str, seller_user_id: Optional[int],
    product_code: str, root_catalog_version: str, promotion_discount_bps: int,
    eligibility: Dict[str, Any], status: str, campaign_version: str,
    started_at: datetime, expires_at: datetime, expected_row_version: Optional[int],
    updated_by: int,
) -> Dict[str, Any]:
    """Create or activate one non-stackable promotion under optimistic locking."""
    campaign = str(campaign_id).strip()
    scope = str(funding_scope).upper().strip()
    product = str(product_code).strip()
    catalog = str(root_catalog_version).strip()
    version = str(campaign_version).strip()
    target_status = str(status).lower().strip()
    discount = int(promotion_discount_bps)
    if (
        not campaign or not product or not catalog or not version
        or max(map(len, (campaign, product, catalog, version))) > 128
    ):
        raise ResaleError("PROMOTION_INPUT_INVALID", "活动编号、商品、目录版本和活动版本不能为空")
    if scope not in {"PLATFORM", "SELLER"} or target_status not in {"draft", "active"}:
        raise ResaleError("PROMOTION_INPUT_INVALID", "活动资金归属或目标状态非法")
    if not isinstance(started_at, datetime) or not isinstance(expires_at, datetime):
        raise ResaleError("PROMOTION_INPUT_INVALID", "活动有效期必须是带时区时间")
    if (
        not 1 <= discount <= 10000
        or started_at.utcoffset() is None or expires_at.utcoffset() is None
        or started_at >= expires_at
    ):
        raise ResaleError("PROMOTION_INPUT_INVALID", "活动折扣或有效期非法")
    normalized_eligibility = _normalize_promotion_eligibility(eligibility)
    seller = int(seller_user_id) if seller_user_id is not None else None
    if scope == "PLATFORM":
        if seller is not None:
            raise ResaleError("PROMOTION_SCOPE_INVALID", "平台活动不能指定服务商出资人")
    else:
        if seller is None or seller <= 0:
            raise ResaleError("PROMOTION_SCOPE_INVALID", "服务商活动必须指定直属出资服务商")
        _require_active_service_seller(cur, seller)
    cur.execute(
        """SELECT 1 FROM pricing_catalog_versions v
           JOIN pricing_catalog_entries e ON e.version_id=v.id
           WHERE v.catalog_type='procurement' AND v.scope_key='PLATFORM_BASE'
             AND v.version_code=%s AND v.status='published' AND e.product_code=%s
           LIMIT 1""",
        (catalog, product),
    )
    if not cur.fetchone():
        raise ResaleError("PROMOTION_CATALOG_INVALID", "活动必须绑定已发布的平台进货目录商品")
    require_xact_scope(cur, where="dealer_inventory_resale.save_promotion_admin")  # §1 硬闸:autocommit 下取事务锁=没锁
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 913717))",
        (f"dealer-resale-promotion:{product}:{catalog}",),
    )
    cur.execute("SELECT * FROM dealer_resale_promotions WHERE campaign_id=%s FOR UPDATE", (campaign,))
    existing_raw = cur.fetchone()
    existing = _row_dict(existing_raw) if existing_raw else None
    desired = {
        "funding_scope": scope, "seller_user_id": seller, "product_code": product,
        "root_catalog_version": catalog, "promotion_discount_bps": discount,
        "eligibility_jsonb": normalized_eligibility, "status": target_status,
        "campaign_version": version, "started_at": started_at, "expires_at": expires_at,
    }
    if existing:
        same = all(
            (_json_object(existing.get(key)) == value if key == "eligibility_jsonb" else existing.get(key) == value)
            for key, value in desired.items()
        )
        if same:
            existing["idempotent"] = True
            return existing
        if str(existing["status"]) != "draft":
            raise ResaleError("PROMOTION_IMMUTABLE", "已生效或结束的活动快照不可修改")
        if expected_row_version is None or int(existing["row_version"]) != int(expected_row_version):
            raise ResaleError("PROMOTION_VERSION_CONFLICT", "活动已变化，请刷新后重试")
        cur.execute(
            "SELECT 1 FROM dealer_inventory_lots WHERE promotion_campaign_id=%s LIMIT 1",
            (campaign,),
        )
        if cur.fetchone():
            mutable_status_only = all(
                (_json_object(existing.get(key)) == value
                 if key == "eligibility_jsonb" else existing.get(key) == value)
                for key, value in desired.items() if key != "status"
            )
            if not mutable_status_only:
                raise ResaleError(
                    "PROMOTION_IMMUTABLE",
                    "活动批次发行后只能将同一快照从草稿激活，不能修改折扣或资格",
                )
    elif expected_row_version not in (None, 0):
        raise ResaleError("PROMOTION_VERSION_CONFLICT", "活动不存在或已变化，请刷新后重试")
    if target_status == "active":
        cur.execute(
            """SELECT campaign_id FROM dealer_resale_promotions
               WHERE campaign_id<>%s AND status='active' AND product_code=%s
                 AND root_catalog_version=%s AND started_at<%s AND expires_at>%s
                 AND (funding_scope='PLATFORM' OR %s='PLATFORM' OR seller_user_id=%s)
               LIMIT 1""",
            (campaign, product, catalog, expires_at, started_at, scope, seller),
        )
        if cur.fetchone():
            raise ResaleError("PROMOTION_STACKING_FORBIDDEN", "同一销售边命中的活动有效期不得重叠")
    payload = json.dumps(normalized_eligibility, ensure_ascii=False, separators=(",", ":"))
    if existing:
        cur.execute(
            """UPDATE dealer_resale_promotions SET
                 funding_scope=%s,sponsor_user_id=%s,seller_user_id=%s,product_code=%s,
                 root_catalog_version=%s,promotion_discount_bps=%s,eligibility_jsonb=%s::jsonb,
                 status=%s,campaign_version=%s,started_at=%s,expires_at=%s,
                 row_version=row_version+1,updated_by=%s,updated_at=clock_timestamp()
               WHERE campaign_id=%s RETURNING *""",
            (scope, seller, seller, product, catalog, discount, payload, target_status,
             version, started_at, expires_at, int(updated_by), campaign),
        )
    else:
        cur.execute(
            """INSERT INTO dealer_resale_promotions
               (campaign_id,funding_scope,sponsor_user_id,seller_user_id,product_code,
                root_catalog_version,promotion_discount_bps,eligibility_jsonb,status,
                campaign_version,started_at,expires_at,created_by,updated_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (campaign, scope, seller, seller, product, catalog, discount, payload,
             target_status, version, started_at, expires_at, int(updated_by), int(updated_by)),
        )
    return _row_dict(cur.fetchone())


def end_promotion_admin(
    cur, *, campaign_id: str, expected_row_version: int, updated_by: int,
) -> Dict[str, Any]:
    campaign = str(campaign_id).strip()
    cur.execute("SELECT * FROM dealer_resale_promotions WHERE campaign_id=%s FOR UPDATE", (campaign,))
    row = _row_dict(cur.fetchone())
    if not row:
        raise ResaleError("PROMOTION_NOT_FOUND", "活动不存在")
    if str(row["status"]) == "ended":
        row["idempotent"] = True
        return row
    if int(row["row_version"]) != int(expected_row_version):
        raise ResaleError("PROMOTION_VERSION_CONFLICT", "活动已变化，请刷新后重试")
    cur.execute(
        """UPDATE dealer_resale_promotions SET status='ended',row_version=row_version+1,
             updated_by=%s,updated_at=clock_timestamp() WHERE campaign_id=%s RETURNING *""",
        (int(updated_by), campaign),
    )
    return _row_dict(cur.fetchone())


def list_promotions_admin(cur, *, limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    cur.execute(
        """SELECT * FROM dealer_resale_promotions ORDER BY created_at DESC,campaign_id
           LIMIT %s OFFSET %s""",
        (int(limit), int(offset)),
    )
    items = [_row_dict(row) for row in cur.fetchall()]
    return {"count": len(items), "items": items}


def resolve_active_promotion(
    cur, *, product_code: str, root_catalog_version: str,
    final_seller_user_id: int, buyer_user_id: int, points: int,
    order_kind: str, for_update: bool = False,
) -> Dict[str, Any]:
    """Resolve at most one non-stackable promotion using the DB clock."""
    if for_update:
        # Serialize the no-row case with admin activation as well as locking an
        # existing campaign row.  A SELECT ... FOR UPDATE cannot lock the
        # absence of an active campaign, so without this shared namespace a
        # draft/new campaign could become active after order revalidation but
        # before that order transaction commits.
        require_xact_scope(cur, where="dealer_inventory_resale.resolve_active_promotion")  # §1 硬闸:autocommit 下取事务锁=没锁
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 913717))",
            (
                f"dealer-resale-promotion:{str(product_code)}:"
                f"{str(root_catalog_version)}",
            ),
        )
    suffix = " FOR UPDATE" if for_update else ""
    cur.execute(
        """SELECT * FROM dealer_resale_promotions
           WHERE status='active' AND product_code=%s AND root_catalog_version=%s
             AND started_at<=clock_timestamp() AND expires_at>clock_timestamp()
             AND (funding_scope='PLATFORM'
                  OR (funding_scope='SELLER' AND seller_user_id=%s))
           ORDER BY funding_scope,campaign_id""" + suffix,
        (str(product_code), str(root_catalog_version), int(final_seller_user_id)),
    )
    rows = [
        row for row in (_row_dict(raw) for raw in cur.fetchall())
        if _promotion_is_eligible(
            row, buyer_user_id=int(buyer_user_id), points=int(points),
            order_kind=str(order_kind),
        )
    ]
    if len(rows) > 1:
        raise ResaleError(
            "PROMOTION_STACKING_FORBIDDEN",
            "同一商品同时命中平台与服务商活动，禁止叠加，请管理员先结束其中一个活动",
        )
    return _promotion_snapshot(rows[0]) if rows else {}


def resolve_fulfillment_edges(
    cur, *, final_buyer_user_id: int, platform_seller_user_id: int,
) -> List[Dict[str, Any]]:
    """Resolve the stable root→buyer edge list from the relationship SSOT."""
    from services import channel_pricing

    buyer = int(final_buyer_user_id)
    platform = int(platform_seller_user_id)
    if buyer <= 0 or platform <= 0 or buyer == platform:
        raise ResaleError("RESALE_CHAIN_PARTIES_INVALID", "厂家与最终买方身份非法")
    try:
        near_to_far = channel_pricing.resolve_chain_snapshot(buyer, cur=cur)
    except channel_pricing.ChannelError as exc:
        if "不存在/停用/非服务商" in str(exc):
            raise ResaleError("SELLER_UNAVAILABLE", "当前进货卖方已停用或不再具备服务商资格") from exc
        raise ResaleError("RESALE_CHAIN_INVALID", str(exc)) from exc
    if len(near_to_far) > MAX_CHAIN_DEPTH:
        raise ResaleError("RESALE_CHAIN_TOO_DEEP", "逐级转售链超过最大深度")
    if any(int(rel["buyer_dealer_id"]) == platform for rel in near_to_far):
        raise ResaleError("PLATFORM_NOT_CHAIN_ROOT", "平台厂家不能拥有更上游关系")
    platform_positions = [
        idx for idx, rel in enumerate(near_to_far)
        if int(rel["upstream_channel_account_id"]) == platform
    ]
    if platform_positions and platform_positions[-1] != len(near_to_far) - 1:
        raise ResaleError("PLATFORM_NOT_CHAIN_ROOT", "渠道链越过平台厂家根节点")

    edges: List[Dict[str, Any]] = []
    far_to_near = list(reversed(near_to_far))
    top_buyer = (
        int(far_to_near[0]["upstream_channel_account_id"])
        if far_to_near else buyer
    )
    if top_buyer != platform:
        cur.execute("SELECT 1 FROM users WHERE id=%s", (platform,))
        if not cur.fetchone():
            raise ResaleError("PLATFORM_SELLER_NOT_FOUND", "显式平台厂家账号不存在")
        edges.append({
            "seller_user_id": platform,
            "buyer_user_id": top_buyer,
            "edge_multiplier_bps": 10000,
            "relationship_version": None,
            "relationship_id": None,
            "source_kind": "platform_root",
        })
    for rel in far_to_near:
        seller_id = int(rel["upstream_channel_account_id"])
        buyer_id = int(rel["buyer_dealer_id"])
        if edges and int(edges[-1]["buyer_user_id"]) != seller_id:
            raise ResaleError("RESALE_CHAIN_DISCONNECTED", "逐级转售关系链断裂")
        edges.append({
            "seller_user_id": seller_id,
            "buyer_user_id": buyer_id,
            "edge_multiplier_bps": int(rel["cost_multiplier_bps"]),
            "relationship_version": str(rel["relationship_version"]),
            "relationship_id": int(rel["relationship_id"]),
            "source_kind": "platform_root" if seller_id == platform else "dealer_resale",
        })
    if not edges or int(edges[-1]["buyer_user_id"]) != buyer:
        raise ResaleError("RESALE_CHAIN_DISCONNECTED", "逐级转售链未到达最终买方")
    if len(edges) > MAX_CHAIN_DEPTH + 1:
        raise ResaleError("RESALE_CHAIN_TOO_DEEP", "逐级转售链超过最大深度")
    return edges


def price_reference_path(
    *, platform_reference_amount_cents: int, edges: List[Dict[str, Any]],
    promotion: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Attach immutable normal/promoted references to root→buyer edges."""
    if not edges:
        raise ResaleError("RESALE_CHAIN_EMPTY", "逐级转售链为空")
    promo = dict(promotion or {})
    scope = str(promo.get("funding_scope") or "")
    discount = int(promo.get("promotion_discount_bps") or 10000)
    reference = int(platform_reference_amount_cents)
    if reference <= 0:
        raise ResaleError("QUOTE_INPUT_INVALID", "平台标准参考价必须大于 0")
    if scope == "PLATFORM":
        reference = _ceil_bps(reference, discount)
    priced: List[Dict[str, Any]] = []
    final_index = len(edges) - 1
    for index, raw in enumerate(edges):
        edge = dict(raw)
        edge_discount = 10000
        if scope == "SELLER":
            if int(promo.get("seller_user_id") or 0) != int(edges[-1]["seller_user_id"]):
                raise ResaleError("PROMOTION_SPONSOR_MISMATCH", "服务商活动出资人与最终卖方不一致")
            if index == final_index:
                edge_discount = discount
        sale = calculate_hop_sale(
            reference, int(edge["edge_multiplier_bps"]),
            promotion_discount_bps=edge_discount,
        )
        edge.update({
            "standard_reference_cents": reference,
            "normal_sale_reference_cents": calculate_hop_sale(
                reference, int(edge["edge_multiplier_bps"])
            ),
            "sale_reference_cents": sale,
            "promotion_discount_bps": edge_discount,
            "promotion": promo if (
                scope == "PLATFORM" or (scope == "SELLER" and index == final_index)
            ) else {},
        })
        priced.append(edge)
        reference = sale
    return priced


def fifo_available_allocations(
    cur, seller_user_id: int, points: int, *,
    platform_campaign_id: Optional[str] = None,
    platform_campaign_product_code: Optional[str] = None,
    platform_campaign_root_catalog_version: Optional[str] = None,
    exclude_source_kinds: Optional[Sequence[str]] = None,
    for_update: bool = False,
) -> Dict[str, Any]:
    """Read deterministic FIFO candidates and return a partial allocation.

    Platform-funded orders may consume only lots carrying that exact campaign;
    normal orders may consume both historical promotional and normal lots because
    their historical acquisition cost affects profit, never today's list price.
    """
    need = int(points)
    if need <= 0:
        raise ResaleError("POINTS_INVALID", "进货算力必须大于 0")
    params: List[Any] = [int(seller_user_id)]
    campaign_sql = ""
    if platform_campaign_id:
        if not platform_campaign_product_code or not platform_campaign_root_catalog_version:
            raise ResaleError(
                "PROMOTION_LOT_LINEAGE_REQUIRED",
                "平台活动库存必须同时锁定产品与根目录版本",
            )
        campaign_sql = (
            " AND promotion_campaign_id=%s AND promotion_funding_scope='PLATFORM'"
            " AND promotion_product_code=%s AND promotion_root_catalog_version=%s"
            " AND pricing_version=promotion_root_catalog_version"
        )
        params.extend((
            str(platform_campaign_id), str(platform_campaign_product_code),
            str(platform_campaign_root_catalog_version),
        ))
    exclude_sql = ""
    if exclude_source_kinds:
        exclude_sql = " AND source_kind <> ALL(%s)"
        params.append([str(kind) for kind in exclude_source_kinds])
    cur.execute(
        """SELECT lot_id,remaining_points,remaining_cost_cents,acquired_at,
                  promotion_campaign_id,promotion_funding_scope,promotion_snapshot_jsonb,
                  promotion_product_code,promotion_root_catalog_version,pricing_version
           FROM dealer_inventory_lots
           WHERE owner_agent_user_id=%s AND status='active' AND remaining_points>0"""
        + campaign_sql + exclude_sql + " ORDER BY acquired_at,lot_id"
        + (" FOR UPDATE" if for_update else ""),
        tuple(params),
    )
    allocations: List[Dict[str, Any]] = []
    for raw in cur.fetchall():
        row = _row_dict(raw)
        remaining_points = int(row["remaining_points"])
        take = min(need, remaining_points)
        if take <= 0:
            continue
        remaining_cost = int(row["remaining_cost_cents"])
        cost = remaining_cost if take == remaining_points else remaining_cost * take // remaining_points
        if cost <= 0:
            raise ResaleError("SELLER_COST_INVALID", "卖方库存批次成本分配为零")
        allocations.append({
            "allocation_kind": "existing_lot",
            "lot_id": str(row["lot_id"]),
            "points": take,
            "cost_basis_cents": cost,
            "promotion_campaign_id": row.get("promotion_campaign_id"),
            "promotion_funding_scope": row.get("promotion_funding_scope"),
            "promotion_product_code": row.get("promotion_product_code"),
            "promotion_root_catalog_version": row.get("promotion_root_catalog_version"),
        })
        need -= take
        if need == 0:
            break
    return {
        "allocations": allocations,
        "existing_inventory_points": int(points) - need,
        "jit_shortfall_points": need,
        "existing_cost_basis_cents": sum(
            int(item["cost_basis_cents"]) for item in allocations
        ),
    }


# 平台厂家账上「不可回收」的批次来源:admin 显式发行的原始批次与开账批次。
# 🔴 按需铸造上线后这两类是**休眠资产**(存量 1.3 亿即 manufacturer_origin),
#    平台跳永不消费它们 —— 否则铸造路径要等存量耗尽才第一次触发,
#    等于"上线即休眠",工单 §5/§8 要的「观察真实订单走通铸造路径」直接落空。
#    它们的处置是第二步 T3 销毁,不是被慢慢卖掉。
PLATFORM_DORMANT_SOURCE_KINDS = ("manufacturer_origin", "opening_balance")


def platform_supply_allocation(
    cur, platform_seller_user_id: int, points: int, *,
    platform_campaign_id: Optional[str] = None,
    platform_campaign_product_code: Optional[str] = None,
    platform_campaign_root_catalog_version: Optional[str] = None,
    for_update: bool = False,
) -> Dict[str, Any]:
    """T1 · 平台厂家根节点的供给:**先回收、再铸缺口**(工单 §1 + 复审补充)。

    取货顺序:
      1) 平台账上**可回收余额**(退款回流的 platform_purchase / direct_resale 批次)· FIFO
      2) 仍不足的缺口 → 当场铸出 allocation_kind='manufacturer_mint'

    🔴 为什么必须"先回收":按需铸造模型下平台余额应恒 0,而**退款会让它 > 0**
       (退款是交易撤销,算力回到供给方而非消失)。若总是铸新的,退回来的算力
       永远没人用 → 平台账上长期挂一堆"退回但永不使用"的算力,
       既不是 0 也不是有意义的数 —— 又长出一个新的 1.3 亿。

    🔴 为什么同时排除 manufacturer_origin/opening_balance:见
       PLATFORM_DORMANT_SOURCE_KINDS 的说明。

    返回形状与 fifo_available_allocations 兼容,额外带 mint_points。
    """
    from services.inventory_minting_guard import load_guard_config, mint_cost_basis_cents

    points = int(points)
    recycled = fifo_available_allocations(
        cur, int(platform_seller_user_id), points,
        platform_campaign_id=platform_campaign_id,
        platform_campaign_product_code=platform_campaign_product_code,
        platform_campaign_root_catalog_version=platform_campaign_root_catalog_version,
        exclude_source_kinds=PLATFORM_DORMANT_SOURCE_KINDS,
        for_update=for_update,
    )
    gap = int(recycled["jit_shortfall_points"])
    allocations = [dict(item) for item in recycled["allocations"]]
    mint_cost = 0
    if gap:
        # 成本基准取 floor 同口径,与切换前 FIFO 取到的 cost_basis 逐笔相等 → 逐跳毛利不变
        mint_cost = mint_cost_basis_cents(gap, load_guard_config(cur))
        allocations.append({
            "allocation_kind": "manufacturer_mint",
            "lot_id": None,
            "points": gap,
            "cost_basis_cents": mint_cost,
        })
    return {
        "allocations": allocations,
        "existing_inventory_points": int(recycled["existing_inventory_points"]),
        # 平台是链路根节点,永远没有"上游补货",缺口一律由铸造覆盖
        "jit_shortfall_points": 0,
        "mint_points": gap,
        "existing_cost_basis_cents": int(recycled["existing_cost_basis_cents"]) + mint_cost,
    }


def _canonical_plan(value: Dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _build_jit_fulfillment_plan(
    cur, *, final_buyer_user_id: int, points: int,
    platform_reference_amount_cents: int, pricing_version: str,
    product_code: str, order_kind: str, final_seller_user_id: Optional[int] = None,
    final_sale_amount_cents: Optional[int] = None,
    catalog_version_id: Optional[str] = None, catalog_entry_id: Optional[str] = None,
    for_update: bool = False, cash_anchor: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the immutable v2 plan without writing money or inventory.

    Demand is propagated from the final hop towards the manufacturer.  Existing
    FIFO lots satisfy a seller first; only that seller's shortfall creates the
    preceding hop.  Pricing is always the current published reference path,
    while FIFO acquisition cost is used only for the seller's true margin.
    """
    settings = _global_settings(cur)
    platform = int(settings.get("platform_seller_user_id") or 0)
    if platform <= 0:
        raise ResaleError("PLATFORM_SELLER_NOT_PREPARED", "平台厂家账号未配置")
    buyer = int(final_buyer_user_id)
    total_points = int(points)
    if total_points <= 0:
        raise ResaleError("POINTS_INVALID", "进货算力必须大于 0")
    consumer = str(order_kind).upper() == "CONSUMER"
    chain_buyer = int(final_seller_user_id) if consumer else buyer
    if consumer:
        if chain_buyer <= 0 or chain_buyer == buyer:
            raise ResaleError("CONSUMER_SALE_PARTIES_INVALID", "服务方与消费者身份非法")
        _require_active_service_seller(cur, chain_buyer)
    edges = resolve_fulfillment_edges(
        cur, final_buyer_user_id=chain_buyer,
        platform_seller_user_id=platform,
    )
    promotion = resolve_active_promotion(
        cur, product_code=str(product_code), root_catalog_version=str(pricing_version),
        final_seller_user_id=(chain_buyer if consumer else int(edges[-1]["seller_user_id"])),
        buyer_user_id=buyer, points=total_points,
        order_kind=("CONSUMER" if consumer else "B2B"),
        for_update=for_update,
    )
    pricing_promotion = (
        {} if consumer and promotion.get("funding_scope") == "SELLER" else promotion
    )
    if cash_anchor:
        from services.procurement_cash_anchor import CASH_ANCHOR_SEMANTIC_VERSION

        anchor = dict(cash_anchor)
        if (
            str(anchor.get("semantic_version") or "") != CASH_ANCHOR_SEMANTIC_VERSION
            or int(anchor.get("buyer_user_id") or 0) != buyer
            or int(anchor.get("paid_inventory_points") or 0) != total_points
            or int(anchor.get("payable_amount_cents") or 0) != int(final_sale_amount_cents or 0)
            or str(anchor.get("catalog_version") or "") != str(pricing_version)
            or int(anchor.get("platform_reference_amount_cents") or 0)
            != int(platform_reference_amount_cents)
        ):
            raise ResaleError("CASH_ANCHOR_INVALID", "金额锚定履约快照与报价输入不一致")
        if _canonical_plan(_json_object(anchor.get("promotion_snapshot"))) != _canonical_plan(promotion):
            raise ResaleError("CASH_ANCHOR_PROMOTION_STALE", "金额锚定活动快照已变化 · 请重新报价")
        anchor_hops = list(anchor.get("hop_costs") or [])
        if len(anchor_hops) != len(edges):
            raise ResaleError("CASH_ANCHOR_PATH_STALE", "金额锚定关系路径已变化 · 请重新报价")
        priced_edges = []
        for index, (raw_edge, raw_anchor_hop) in enumerate(zip(edges, anchor_hops)):
            edge = dict(raw_edge)
            anchor_hop = dict(raw_anchor_hop)
            comparable = (
                "seller_user_id", "buyer_user_id", "relationship_id",
                "relationship_version", "source_kind",
            )
            if any(
                str(edge.get(key)) != str(anchor_hop.get(key))
                for key in comparable
                if edge.get(key) is not None or anchor_hop.get(key) is not None
            ) or int(edge["edge_multiplier_bps"]) != int(anchor_hop.get("multiplier_bps") or 0):
                raise ResaleError(
                    "CASH_ANCHOR_PATH_STALE",
                    f"金额锚定第 {index + 1} 跳关系已变化 · 请重新报价",
                )
            edge.update({
                "standard_reference_cents": int(anchor_hop["standard_reference_cents"]),
                "normal_sale_reference_cents": int(anchor_hop["normal_sale_reference_cents"]),
                "sale_reference_cents": int(anchor_hop["sale_reference_cents"]),
                "promotion_discount_bps": int(anchor_hop.get("promotion_discount_bps") or 10000),
                "promotion": promotion if (
                    promotion.get("funding_scope") == "PLATFORM"
                    or (
                        promotion.get("funding_scope") == "SELLER"
                        and index == len(edges) - 1
                    )
                ) else {},
            })
            priced_edges.append(edge)
    else:
        priced_edges = price_reference_path(
            platform_reference_amount_cents=int(platform_reference_amount_cents),
            edges=edges, promotion=pricing_promotion,
        )
    platform_campaign = (
        str(promotion["campaign_id"])
        if promotion.get("funding_scope") == "PLATFORM" else None
    )

    consumer_inventory: Dict[str, Any] = {}
    demand = total_points
    if consumer:
        consumer_inventory = fifo_available_allocations(
            cur, chain_buyer, total_points,
            platform_campaign_id=platform_campaign,
            platform_campaign_product_code=(str(product_code) if platform_campaign else None),
            platform_campaign_root_catalog_version=(str(pricing_version) if platform_campaign else None),
            for_update=for_update,
        )
        demand = int(consumer_inventory["jit_shortfall_points"])
    reverse: Dict[int, Dict[str, Any]] = {}
    for index in range(len(priced_edges) - 1, -1, -1):
        if demand <= 0:
            break
        edge = priced_edges[index]
        if int(edge["seller_user_id"]) == platform:
            # 🔴 T1(工单 §1):平台厂家供给 = 先回收余额、再铸缺口。
            # "平台缺货" 是人为制造的故障模式,业务上不该存在,
            # 因此平台跳不再有 MANUFACTURER_INVENTORY_INSUFFICIENT 分支。
            allocation = platform_supply_allocation(
                cur, platform, demand,
                platform_campaign_id=platform_campaign,
                platform_campaign_product_code=(
                    str(product_code) if platform_campaign else None
                ),
                platform_campaign_root_catalog_version=(
                    str(pricing_version) if platform_campaign else None
                ),
                for_update=for_update,
            )
        else:
            allocation = fifo_available_allocations(
                cur, int(edge["seller_user_id"]), demand,
                platform_campaign_id=platform_campaign,
                platform_campaign_product_code=(str(product_code) if platform_campaign else None),
                platform_campaign_root_catalog_version=(
                    str(pricing_version) if platform_campaign else None
                ),
                for_update=for_update,
            )
        reverse[index] = {"points": demand, **allocation}
        demand = int(allocation["jit_shortfall_points"])
    if demand:
        # 到不了这里:resolve_fulfillment_edges 保证 edges[0].seller 必是平台厂家,
        # 而平台跳恒把 shortfall 归零。留 fail-closed 防御,绝不静默放行未覆盖需求。
        raise ResaleError(
            "JIT_PLAN_INVALID",
            f"履约需求未落到平台厂家铸造根节点：剩余 {demand} 算力无供给",
        )

    hops: List[Dict[str, Any]] = []
    previous_sale = 0
    historical_principal = 0
    for original_index in sorted(reverse):
        edge = dict(priced_edges[original_index])
        allocation = reverse[original_index]
        hop_points = int(allocation["points"])
        standard_reference = proportional_reference(
            int(edge["standard_reference_cents"]), hop_points, total_points,
        )
        normal_reference = proportional_reference(
            int(edge["normal_sale_reference_cents"]), hop_points, total_points,
        )
        sale_reference = proportional_reference(
            int(edge["sale_reference_cents"]), hop_points, total_points,
        )
        existing_cost = int(allocation["existing_cost_basis_cents"])
        shortfall = int(allocation["jit_shortfall_points"])
        minted = int(allocation.get("mint_points") or 0)
        incoming_cost = previous_sale if shortfall else 0
        seller_cost = existing_cost + incoming_cost
        if seller_cost <= 0:
            raise ResaleError("SELLER_COST_INVALID", "逐跳卖方真实成本无效")
        sale = sale_reference
        margin = sale - seller_cost
        if margin < 0:
            raise ResaleError(
                "NEGATIVE_MARGIN",
                f"第 {original_index} 跳售价低于卖方真实成本 · 拒绝报价",
            )
        hop_allocations = [dict(item) for item in allocation["allocations"]]
        if int(edge["seller_user_id"]) != platform:
            historical_principal += existing_cost
        if shortfall:
            if not hops:
                raise ResaleError("JIT_PLAN_INVALID", "厂家短缺不能由虚拟库存补足")
            hop_allocations.append({
                "allocation_kind": "jit_incoming",
                "lot_id": None,
                "source_hop_seq": len(hops) - 1,
                "points": shortfall,
                "cost_basis_cents": incoming_cost,
                "promotion_campaign_id": promotion.get("campaign_id"),
            })
        hop = {
            "hop_seq": len(hops),
            "chain_edge_index": original_index,
            "seller_user_id": int(edge["seller_user_id"]),
            "buyer_user_id": int(edge["buyer_user_id"]),
            "points": hop_points,
            "existing_inventory_points": int(allocation["existing_inventory_points"]),
            "jit_shortfall_points": shortfall,
            "mint_points": minted,
            "standard_reference_cents": standard_reference,
            "normal_sale_reference_cents": normal_reference,
            "seller_cost_basis_cents": seller_cost,
            "principal_recovery_cents": (
                0 if int(edge["seller_user_id"]) == platform else existing_cost
            ),
            "agent_payable_cents": (
                0 if int(edge["seller_user_id"]) == platform else existing_cost + margin
            ),
            "sale_amount_cents": sale,
            "margin_cents": margin,
            "edge_multiplier_bps": int(edge["edge_multiplier_bps"]),
            "relationship_version": edge.get("relationship_version"),
            "relationship_id": edge.get("relationship_id"),
            "source_kind": str(edge["source_kind"]),
            "promotion_discount_bps": int(edge.get("promotion_discount_bps") or 10000),
            "promotion": dict(edge.get("promotion") or {}),
            "allocations": hop_allocations,
        }
        hops.append(hop)
        previous_sale = sale

    if not hops and not consumer:
        raise ResaleError("JIT_PLAN_INVALID", "逐级履约计划为空")
    if cash_anchor and not consumer:
        target_sale = int(final_sale_amount_cents or 0)
        final_hop = hops[-1]
        current_sale = int(final_hop["sale_amount_cents"])
        if target_sale < current_sale or target_sale > MAX_MONEY_CENTS:
            raise ResaleError("CASH_ANCHOR_OVERSPEND", "金额锚定实付不足以覆盖逐跳成本")
        remainder = target_sale - current_sale
        final_hop["sale_amount_cents"] = target_sale
        final_hop["margin_cents"] = int(final_hop["margin_cents"]) + remainder
        if int(final_hop["seller_user_id"]) != platform:
            final_hop["agent_payable_cents"] = int(final_hop["agent_payable_cents"]) + remainder
        previous_sale = target_sale
    final_seller = chain_buyer if consumer else int(hops[-1]["seller_user_id"])
    consumer_leg: Dict[str, Any] = {}
    if consumer:
        customer_paid = int(final_sale_amount_cents or 0)
        if customer_paid <= 0 or customer_paid > MAX_MONEY_CENTS:
            raise ResaleError("SALE_AMOUNT_INVALID", "零售价目金额非法")
        existing_cost = int(consumer_inventory["existing_cost_basis_cents"])
        seller_cost = existing_cost + (int(hops[-1]["sale_amount_cents"]) if hops else 0)
        final_margin = customer_paid - seller_cost
        if final_margin < 0:
            raise ResaleError("NEGATIVE_MARGIN", "已发布零售价低于服务方当前进货成本")
        consumer_allocations = [
            dict(item) for item in consumer_inventory["allocations"]
        ]
        if int(consumer_inventory["jit_shortfall_points"]):
            consumer_allocations.append({
                "allocation_kind": "jit_incoming", "lot_id": None,
                "source_hop_seq": len(hops) - 1,
                "points": int(consumer_inventory["jit_shortfall_points"]),
                "cost_basis_cents": int(hops[-1]["sale_amount_cents"]),
                "promotion_campaign_id": promotion.get("campaign_id"),
            })
        historical_principal += existing_cost
        consumer_leg = {
            "seller_user_id": chain_buyer, "buyer_user_id": buyer,
            "points": total_points,
            "existing_inventory_points": int(consumer_inventory["existing_inventory_points"]),
            "jit_shortfall_points": int(consumer_inventory["jit_shortfall_points"]),
            "standard_reference_cents": customer_paid,
            "normal_sale_reference_cents": customer_paid,
            "seller_cost_basis_cents": seller_cost,
            "principal_recovery_cents": existing_cost,
            "agent_payable_cents": existing_cost + final_margin,
            "sale_amount_cents": customer_paid, "margin_cents": final_margin,
            "edge_multiplier_bps": 10000, "relationship_version": None,
            "relationship_id": None, "source_kind": "consumer_retail",
            "promotion_discount_bps": int(
                promotion.get("promotion_discount_bps") or 10000
            ),
            "promotion": promotion if promotion.get("funding_scope") == "SELLER" else {},
            "allocations": consumer_allocations,
        }
        previous_sale = customer_paid
        final_seller = chain_buyer

    platform_revenue = (
        int(hops[0]["sale_amount_cents"])
        if hops and int(hops[0]["seller_user_id"]) == platform else 0
    )
    nonplatform_margins = [
        int(hop["margin_cents"]) for hop in hops
        if int(hop["seller_user_id"]) != platform
    ]
    final_margin = int(consumer_leg.get("margin_cents") or 0)
    if not consumer and int(hops[-1]["seller_user_id"]) != platform:
        final_margin = int(hops[-1]["margin_cents"])
    dealer_margin = sum(nonplatform_margins) - (0 if consumer else final_margin)
    conservation = verify_funds_conservation(
        final_paid_cents=previous_sale,
        platform_root_revenue_cents=platform_revenue,
        dealer_margin_cents=dealer_margin,
        final_seller_margin_cents=final_margin,
        historical_principal_cents=historical_principal,
    )
    result = {
        "plan_version": 2, "order_kind": "CONSUMER" if consumer else "B2B",
        "product_code": str(product_code), "root_catalog_version": str(pricing_version),
        "catalog_version_id": str(catalog_version_id) if catalog_version_id else None,
        "catalog_entry_id": str(catalog_entry_id) if catalog_entry_id else None,
        "platform_seller_user_id": platform,
        "final_seller_user_id": final_seller, "final_buyer_user_id": buyer,
        "points": total_points,
        "standard_reference_cents": int(platform_reference_amount_cents),
        "final_sale_amount_cents": previous_sale,
        "promotion": promotion, "hops": hops, "consumer_leg": consumer_leg,
        "funds_conservation": conservation,
    }
    if cash_anchor:
        result["cash_anchor_semantic_version"] = str(cash_anchor["semantic_version"])
        result["cash_anchor_fingerprint"] = str(cash_anchor["cash_anchor_fingerprint"])
        result["cash_anchor"] = dict(cash_anchor)
    return result


def fifo_allocations(cur, seller_user_id: int, points: int) -> List[Dict[str, Any]]:
    need = int(points)
    if need <= 0:
        raise ResaleError("POINTS_INVALID", "进货算力必须大于 0")
    cur.execute(
        """SELECT lot_id, remaining_points, remaining_cost_cents, acquired_at
           FROM dealer_inventory_lots
           WHERE owner_agent_user_id=%s AND status='active' AND remaining_points>0
           ORDER BY acquired_at, lot_id""",
        (int(seller_user_id),),
    )
    allocations: List[Dict[str, Any]] = []
    for raw in cur.fetchall():
        row = _row_dict(raw)
        remaining_points = int(row["remaining_points"])
        take = min(need, remaining_points)
        if take <= 0:
            continue
        remaining_cost = int(row["remaining_cost_cents"])
        cost = remaining_cost if take == remaining_points else remaining_cost * take // remaining_points
        allocations.append({
            "lot_id": str(row["lot_id"]),
            "points": take,
            "cost_basis_cents": cost,
        })
        need -= take
        if need == 0:
            break
    if need:
        available = int(points) - need
        raise ResaleError(
            "SELLER_INVENTORY_INSUFFICIENT",
            f"直属卖方库存不足：需要 {int(points)}，可售 {available}",
        )
    return allocations


def build_cash_anchored_quote_terms(
    cur, *, buyer_user_id: int, payable_amount_cents: int,
    platform_points_numer: int, platform_points_denom: int,
    pricing_version: str, product_code: str = "CUSTOM_AMOUNT",
    catalog_version_id: Optional[str] = None, catalog_entry_id: Optional[str] = None,
    purchase_discount_snapshot: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Build the active B2B FIFO/JIT plan without changing buyer cash.

    The pure calculator fixes paid points first.  Existing resale machinery then
    proves that the current lots and JIT chain can deliver those exact points while
    every hop remains non-negative.  Tier/founder rewards are intentionally absent
    here and are calculated afterwards by the procurement snapshot builder.
    """

    if not resale_flag_enabled(cur):
        return None
    assert_schema_ready(cur)
    from services.procurement_cash_anchor import calculate_cash_anchor

    settings = _global_settings(cur)
    platform = int(settings.get("platform_seller_user_id") or 0)
    if platform <= 0:
        raise ResaleError("PLATFORM_SELLER_NOT_PREPARED", "平台厂家账号未配置")
    buyer = int(buyer_user_id)
    payable = int(payable_amount_cents)
    edges = resolve_fulfillment_edges(
        cur, final_buyer_user_id=buyer, platform_seller_user_id=platform,
    )
    final_seller = int(edges[-1]["seller_user_id"])

    def anchor_path(promotion: Dict[str, Any]) -> tuple[List[Dict[str, Any]], int]:
        scope = str(promotion.get("funding_scope") or "")
        discount = int(promotion.get("promotion_discount_bps") or 10000)
        platform_discount = discount if scope == "PLATFORM" else 10000
        steps: List[Dict[str, Any]] = []
        for index, edge in enumerate(edges):
            edge_discount = (
                discount
                if scope == "SELLER" and index == len(edges) - 1
                else 10000
            )
            if scope == "SELLER" and int(promotion.get("seller_user_id") or 0) != final_seller:
                raise ResaleError("PROMOTION_SPONSOR_MISMATCH", "服务商活动出资人与直属卖方不一致")
            steps.append({
                "seller_user_id": int(edge["seller_user_id"]),
                "buyer_user_id": int(edge["buyer_user_id"]),
                "relationship_id": edge.get("relationship_id"),
                "relationship_version": edge.get("relationship_version"),
                "source_kind": str(edge["source_kind"]),
                "multiplier_bps": int(edge["edge_multiplier_bps"]),
                "promotion_discount_bps": edge_discount,
            })
        return steps, platform_discount

    base_steps, _ = anchor_path({})
    base_anchor = calculate_cash_anchor(
        buyer_user_id=buyer,
        payable_amount_cents=payable,
        platform_points_numer=int(platform_points_numer),
        platform_points_denom=int(platform_points_denom),
        catalog_version=str(pricing_version),
        relationship_path=base_steps,
        purchase_discount_snapshot=purchase_discount_snapshot,
    )
    promotion = resolve_active_promotion(
        cur,
        product_code=str(product_code),
        root_catalog_version=str(pricing_version),
        final_seller_user_id=final_seller,
        buyer_user_id=buyer,
        points=int(base_anchor["paid_inventory_points"]),
        order_kind="B2B",
    )
    steps, platform_discount = anchor_path(promotion)
    anchor = calculate_cash_anchor(
        buyer_user_id=buyer,
        payable_amount_cents=payable,
        platform_points_numer=int(platform_points_numer),
        platform_points_denom=int(platform_points_denom),
        catalog_version=str(pricing_version),
        relationship_path=steps,
        platform_promotion_discount_bps=platform_discount,
        promotion_snapshot=promotion,
        purchase_discount_snapshot=purchase_discount_snapshot,
    )
    stable_promotion = resolve_active_promotion(
        cur,
        product_code=str(product_code),
        root_catalog_version=str(pricing_version),
        final_seller_user_id=final_seller,
        buyer_user_id=buyer,
        points=int(anchor["paid_inventory_points"]),
        order_kind="B2B",
    )
    if _canonical_plan(stable_promotion) != _canonical_plan(promotion):
        raise ResaleError(
            "CASH_ANCHOR_PROMOTION_UNSTABLE",
            "活动资格与金额锚定算力无法形成稳定快照 · 请调整活动资格",
        )

    plan = _build_jit_fulfillment_plan(
        cur,
        final_buyer_user_id=buyer,
        points=int(anchor["paid_inventory_points"]),
        platform_reference_amount_cents=int(anchor["platform_reference_amount_cents"]),
        pricing_version=str(pricing_version),
        product_code=str(product_code),
        order_kind="B2B",
        final_sale_amount_cents=payable,
        catalog_version_id=catalog_version_id,
        catalog_entry_id=catalog_entry_id,
        cash_anchor=anchor,
    )
    current = dict(plan["hops"][-1])
    if int(plan["final_sale_amount_cents"]) != payable:
        raise ResaleError("CASH_ANCHOR_AMOUNT_DRIFT", "逐级履约计划改变了实付金额")
    return {
        "resale_snapshot_version": 2,
        "resale_mode": True,
        "jit_fulfillment_mode": "RECURSIVE_FIFO",
        "seller_user_id": int(current["seller_user_id"]),
        "buyer_user_id": buyer,
        "points": int(anchor["paid_inventory_points"]),
        "seller_lot_allocations": current["allocations"],
        "seller_cost_basis_cents": int(current["seller_cost_basis_cents"]),
        "principal_recovery_cents": int(current["principal_recovery_cents"]),
        "agent_payable_cents": int(current["agent_payable_cents"]),
        "sale_amount_cents": payable,
        "margin_cents": int(current["margin_cents"]),
        "downstream_markup_bps": int(current["edge_multiplier_bps"]),
        "edge_multiplier_bps": int(current["edge_multiplier_bps"]),
        "standard_reference_cents": int(current["standard_reference_cents"]),
        "pricing_version": str(pricing_version),
        "seller_policy_version": str(current.get("relationship_version") or pricing_version),
        "relationship_version": current.get("relationship_version"),
        "payment_collector": "PLATFORM",
        "refund_responsible_user_id": int(current["seller_user_id"]),
        "source_kind": str(current["source_kind"]),
        "platform_reference_amount_cents": int(anchor["platform_reference_amount_cents"]),
        "cash_anchor_semantic_version": str(anchor["semantic_version"]),
        "cash_anchor_fingerprint": str(anchor["cash_anchor_fingerprint"]),
        "cash_anchor": anchor,
        "refund_window_hours": REFUND_WINDOW_HOURS,
        "fulfillment_plan": plan,
    }


def build_quote_terms(
    cur, *, buyer_user_id: int, points: int, platform_reference_amount_cents: int,
    pricing_version: str, product_code: str = "CUSTOM_AMOUNT",
    catalog_version_id: Optional[str] = None, catalog_entry_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not resale_flag_enabled(cur):
        return None
    assert_schema_ready(cur)
    points = int(points)
    platform_amount = int(platform_reference_amount_cents)
    if points <= 0 or platform_amount <= 0:
        raise ResaleError("QUOTE_INPUT_INVALID", "进货金额和算力必须大于 0")
    plan = _build_jit_fulfillment_plan(
        cur, final_buyer_user_id=int(buyer_user_id), points=points,
        platform_reference_amount_cents=platform_amount,
        pricing_version=str(pricing_version), product_code=str(product_code),
        order_kind="B2B", catalog_version_id=catalog_version_id,
        catalog_entry_id=catalog_entry_id,
    )
    current = dict(plan["hops"][-1])
    return {
        "resale_snapshot_version": 2,
        "resale_mode": True,
        "jit_fulfillment_mode": "RECURSIVE_FIFO",
        "seller_user_id": int(current["seller_user_id"]),
        "buyer_user_id": int(buyer_user_id),
        "points": points,
        "seller_lot_allocations": current["allocations"],
        "seller_cost_basis_cents": int(current["seller_cost_basis_cents"]),
        "principal_recovery_cents": int(current["principal_recovery_cents"]),
        "agent_payable_cents": int(current["agent_payable_cents"]),
        "sale_amount_cents": int(current["sale_amount_cents"]),
        "margin_cents": int(current["margin_cents"]),
        "downstream_markup_bps": int(current["edge_multiplier_bps"]),
        "edge_multiplier_bps": int(current["edge_multiplier_bps"]),
        "standard_reference_cents": int(current["standard_reference_cents"]),
        "pricing_version": str(pricing_version),
        "seller_policy_version": str(current.get("relationship_version") or pricing_version),
        "relationship_version": current.get("relationship_version"),
        "payment_collector": "PLATFORM",
        "refund_responsible_user_id": int(current["seller_user_id"]),
        "source_kind": str(current["source_kind"]),
        "platform_reference_amount_cents": platform_amount,
        "refund_window_hours": REFUND_WINDOW_HOURS,
        "fulfillment_plan": plan,
    }


def preview_consumer_quote_terms(
    cur, *, seller_user_id: int, points: int,
    catalog_reference_amount_cents: int, pricing_version: str,
    consumer_user_id: Optional[int] = None,
    digital_goods_acknowledged: bool = True, product_code: str = "RETAIL",
    platform_reference_amount_cents: Optional[int] = None,
    catalog_version_id: Optional[str] = None, catalog_entry_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Run the live consumer-resale price/inventory gates without writing.

    Both platform-direct readiness and quote issuance use this core so readiness
    cannot claim success when the configured service account has no sellable FIFO
    inventory or has an invalid resale policy. Historical FIFO cost affects only
    attributable margin; customer cash remains the published retail catalog amount.
    """
    if not resale_flag_enabled(cur):
        return None
    assert_schema_ready(cur)
    seller_id = int(seller_user_id)
    consumer_id = int(consumer_user_id or 0)
    points = int(points)
    catalog_amount = int(catalog_reference_amount_cents)
    if seller_id <= 0:
        raise ResaleError("CONSUMER_SALE_PARTIES_INVALID", "服务方身份非法")
    if consumer_user_id is not None and (
        consumer_id <= 0 or seller_id == consumer_id
    ):
        raise ResaleError("CONSUMER_SALE_PARTIES_INVALID", "服务方与消费者身份非法")
    _require_active_service_seller(cur, seller_id)
    if points <= 0 or catalog_amount <= 0:
        raise ResaleError("QUOTE_INPUT_INVALID", "零售金额和算力必须大于 0")
    if digital_goods_acknowledged is not True:
        raise ResaleError(
            "DIGITAL_GOODS_ACK_REQUIRED",
            "请先确认：算力属于即时交付的数字商品，不适用无理由退货；退款由 OmniRank 平台按适用规则统一受理",
        )
    if consumer_user_id is None:
        # Readiness has no customer identity and must not invent one merely to
        # resolve a JIT ancestry graph.  It probes the configured direct
        # service's currently sellable FIFO inventory and policy, read-only.
        allocations = fifo_allocations(cur, seller_id, points)
        cost_basis = sum(int(item["cost_basis_cents"]) for item in allocations)
        if cost_basis <= 0:
            raise ResaleError("SELLER_COST_INVALID", "服务方库存真实成本无效 · 拒绝报价")
        policy = get_effective_policy(cur, seller_id)
        markup = int(policy["downstream_markup_bps"])
        min_markup = int(policy["authorized_min_markup_bps"])
        max_markup = int(policy["authorized_max_markup_bps"])
        if (
            markup < MIN_MARKUP_BPS
            or min_markup < MIN_MARKUP_BPS
            or not min_markup <= markup <= max_markup
        ):
            raise ResaleError("MARKUP_INVALID", "服务方下级售价系数不在授权范围内")
        margin = catalog_amount - cost_basis
        if margin < 0:
            raise ResaleError("NEGATIVE_MARGIN", "已发布零售价低于真实库存成本")
        return {
            "consumer_resale_snapshot_version": 2,
            "consumer_resale_mode": True,
            "jit_fulfillment_mode": "DIRECT_FIFO_READINESS",
            "seller_user_id": seller_id,
            "points": points,
            "seller_lot_allocations": allocations,
            "seller_cost_basis_cents": cost_basis,
            "principal_recovery_cents": cost_basis,
            "agent_payable_cents": catalog_amount,
            "sale_amount_cents": catalog_amount,
            "margin_cents": margin,
            "downstream_markup_bps": markup,
            "edge_multiplier_bps": markup,
            "standard_reference_cents": cost_basis,
            "pricing_version": str(pricing_version),
            "seller_policy_version": str(policy["policy_version"]),
            "payment_collector": "PLATFORM",
            "refund_responsible_user_id": seller_id,
            "catalog_reference_amount_cents": catalog_amount,
            "platform_reference_amount_cents": int(
                platform_reference_amount_cents or catalog_amount
            ),
        }
    root_reference = int(platform_reference_amount_cents or catalog_amount)
    plan = _build_jit_fulfillment_plan(
        cur, final_buyer_user_id=consumer_id, final_seller_user_id=seller_id,
        points=points, platform_reference_amount_cents=root_reference,
        pricing_version=str(pricing_version), product_code=str(product_code),
        order_kind="CONSUMER", final_sale_amount_cents=catalog_amount,
        catalog_version_id=catalog_version_id, catalog_entry_id=catalog_entry_id,
    )
    current = dict(plan["consumer_leg"])
    return {
        "consumer_resale_snapshot_version": 2,
        "consumer_resale_mode": True,
        "jit_fulfillment_mode": "RECURSIVE_FIFO",
        "seller_user_id": seller_id,
        **({"consumer_user_id": consumer_id} if consumer_user_id is not None else {}),
        "points": points,
        "seller_lot_allocations": current["allocations"],
        "seller_cost_basis_cents": int(current["seller_cost_basis_cents"]),
        "principal_recovery_cents": int(current["principal_recovery_cents"]),
        "agent_payable_cents": int(current["agent_payable_cents"]),
        "sale_amount_cents": int(current["sale_amount_cents"]),
        "margin_cents": int(current["margin_cents"]),
        "downstream_markup_bps": int(current["edge_multiplier_bps"]),
        "edge_multiplier_bps": int(current["edge_multiplier_bps"]),
        "standard_reference_cents": int(current["standard_reference_cents"]),
        "pricing_version": str(pricing_version),
        "seller_policy_version": str(pricing_version),
        "payment_collector": "PLATFORM",
        "refund_responsible_user_id": seller_id,
        "catalog_reference_amount_cents": catalog_amount,
        "platform_reference_amount_cents": root_reference,
        "fulfillment_plan": plan,
    }


def build_consumer_quote_terms(
    cur, *, seller_user_id: int, consumer_user_id: int, points: int,
    catalog_reference_amount_cents: int, pricing_version: str,
    digital_goods_acknowledged: bool, product_code: str = "RETAIL",
    platform_reference_amount_cents: Optional[int] = None,
    catalog_version_id: Optional[str] = None, catalog_entry_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Price one service-provider → consumer sale from the seller's real FIFO cost."""
    seller_id = int(seller_user_id)
    consumer_id = int(consumer_user_id)
    if seller_id <= 0 or consumer_id <= 0 or seller_id == consumer_id:
        raise ResaleError("CONSUMER_SALE_PARTIES_INVALID", "服务方与消费者身份非法")
    if digital_goods_acknowledged is not True:
        raise ResaleError(
            "DIGITAL_GOODS_ACK_REQUIRED",
            "请先确认：算力属于即时交付的数字商品，不适用无理由退货；退款由平台按实际未消费情况审核",
        )
    terms = preview_consumer_quote_terms(
        cur,
        seller_user_id=seller_id,
        consumer_user_id=consumer_id,
        points=int(points),
        catalog_reference_amount_cents=int(catalog_reference_amount_cents),
        pricing_version=str(pricing_version),
        digital_goods_acknowledged=True,
        product_code=str(product_code),
        platform_reference_amount_cents=platform_reference_amount_cents,
        catalog_version_id=catalog_version_id,
        catalog_entry_id=catalog_entry_id,
    )
    if terms is None:
        return None
    return {
        **terms,
        "consumer_user_id": consumer_id,
        "digital_goods_policy_code": DIGITAL_GOODS_POLICY_CODE,
        "digital_goods_acknowledged": True,
    }


def _lock_and_revalidate_v2_plan(
    cur, *, snapshot: Dict[str, Any], order_kind: str,
) -> Dict[str, Any]:
    expected = _json_object(snapshot.get("fulfillment_plan"))
    if int(expected.get("plan_version") or 0) != 2:
        raise ResaleError("JIT_PLAN_REQUIRED", "订单缺少不可变 JIT 履约计划")
    catalog_version_id = expected.get("catalog_version_id")
    if catalog_version_id:
        cur.execute(
            """SELECT id,version_code,status,effective_to
               FROM pricing_catalog_versions WHERE id=%s FOR SHARE""",
            (int(catalog_version_id),),
        )
        version = _row_dict(cur.fetchone())
        if (
            not version or version.get("status") != "published"
            or version.get("effective_to") is not None
            or str(version.get("version_code")) != str(expected["root_catalog_version"])
        ):
            raise ResaleError("JIT_CATALOG_STALE", "已发布目录已变化 · 请重新报价")

    # Fixed financial lock order: promotion namespace/row -> inventory lots ->
    # inventory wallets. Manufacturer campaign issuance reads the campaign
    # before touching the platform wallet, so taking the promotion lock here
    # prevents the inverse wallet -> campaign wait cycle during order reserve.
    # The plan is rebuilt below and remains the sole semantic comparison.
    resolve_active_promotion(
        cur,
        product_code=str(expected["product_code"]),
        root_catalog_version=str(expected["root_catalog_version"]),
        final_seller_user_id=int(expected["final_seller_user_id"]),
        buyer_user_id=int(expected["final_buyer_user_id"]),
        points=int(expected["points"]),
        order_kind=str(order_kind),
        for_update=True,
    )

    lot_ids = sorted({
        str(item["lot_id"])
        for hop in list(expected.get("hops") or [])
        for item in list(hop.get("allocations") or [])
        if item.get("allocation_kind") == "existing_lot" and item.get("lot_id")
    } | {
        str(item["lot_id"])
        for item in list(_json_object(expected.get("consumer_leg")).get("allocations") or [])
        if item.get("allocation_kind") == "existing_lot" and item.get("lot_id")
    })
    if lot_ids:
        cur.execute(
            """SELECT lot_id FROM dealer_inventory_lots
               WHERE lot_id=ANY(%s) ORDER BY lot_id FOR UPDATE""",
            (lot_ids,),
        )
        if {str(row["lot_id"]) for row in cur.fetchall()} != set(lot_ids):
            raise ResaleError("JIT_INVENTORY_STALE", "报价库存批次已不存在 · 请重新报价")
    seller_ids = sorted({
        int(hop["seller_user_id"]) for hop in list(expected.get("hops") or [])
    } | ({int(expected["final_seller_user_id"])} if order_kind == "CONSUMER" else set()))
    for seller_id in seller_ids:
        _inventory_wallet(cur, seller_id)
    if seller_ids:
        cur.execute(
            """SELECT agent_user_id FROM agent_inventory_wallets
               WHERE agent_user_id=ANY(%s) ORDER BY agent_user_id FOR UPDATE""",
            (seller_ids,),
        )
        if {int(row["agent_user_id"]) for row in cur.fetchall()} != set(seller_ids):
            raise ResaleError("JIT_INVENTORY_WALLET_MISSING", "逐跳库存钱包缺失")

    cash_anchor = _json_object(expected.get("cash_anchor"))
    cash_anchor_mode = bool(expected.get("cash_anchor_semantic_version"))
    if cash_anchor_mode and not cash_anchor:
        raise ResaleError("CASH_ANCHOR_INVALID", "金额锚定报价缺少不可变计算快照")
    current = _build_jit_fulfillment_plan(
        cur,
        final_buyer_user_id=int(expected["final_buyer_user_id"]),
        final_seller_user_id=(
            int(expected["final_seller_user_id"]) if order_kind == "CONSUMER" else None
        ),
        points=int(expected["points"]),
        platform_reference_amount_cents=int(expected["standard_reference_cents"]),
        pricing_version=str(expected["root_catalog_version"]),
        product_code=str(expected["product_code"]), order_kind=order_kind,
        final_sale_amount_cents=(
            int(expected["final_sale_amount_cents"])
            if order_kind == "CONSUMER" or cash_anchor_mode else None
        ),
        catalog_version_id=(str(catalog_version_id) if catalog_version_id else None),
        catalog_entry_id=expected.get("catalog_entry_id"), for_update=True,
        cash_anchor=(cash_anchor if cash_anchor_mode else None),
    )
    if _canonical_plan(current) != _canonical_plan(expected):
        raise ResaleError(
            "JIT_QUOTE_STALE",
            "报价后的关系、倍率、活动、目录或库存已变化 · 请重新报价",
        )
    return expected


def _reserve_existing_allocation(
    cur, *, seller_id: int, allocation: Dict[str, Any], stale_code: str,
) -> None:
    cur.execute(
        """UPDATE dealer_inventory_lots
           SET remaining_points=remaining_points-%s,
               reserved_points=reserved_points+%s,
               remaining_cost_cents=remaining_cost_cents-%s,
               reserved_cost_cents=reserved_cost_cents+%s,updated_at=NOW()
           WHERE lot_id=%s AND owner_agent_user_id=%s AND status='active'
             AND remaining_points >= %s AND remaining_cost_cents >= %s""",
        (
            int(allocation["points"]), int(allocation["points"]),
            int(allocation["cost_basis_cents"]), int(allocation["cost_basis_cents"]),
            str(allocation["lot_id"]), int(seller_id), int(allocation["points"]),
            int(allocation["cost_basis_cents"]),
        ),
    )
    if cur.rowcount != 1:
        raise ResaleError(stale_code, "库存并发变化 · 请重新报价")


def _persist_v2_plan_and_reservations(
    cur, *, order_id: str, quote_id: str, plan: Dict[str, Any],
) -> Dict[str, Any]:
    plan_id = _stable_id("JFP", str(order_id))
    immutable = _canonical_plan(plan)
    cur.execute(
        """INSERT INTO dealer_resale_fulfillment_plans
           (plan_id,root_order_id,quote_id,order_kind,final_seller_user_id,
            final_buyer_user_id,points,product_code,root_catalog_version,
            standard_reference_cents,final_sale_amount_cents,promotion_snapshot_jsonb,
            immutable_snapshot_jsonb,funds_conservation_jsonb,idempotency_key)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s)""",
        (
            plan_id, str(order_id), str(quote_id), str(plan["order_kind"]),
            int(plan["final_seller_user_id"]), int(plan["final_buyer_user_id"]),
            int(plan["points"]), str(plan["product_code"]), str(plan["root_catalog_version"]),
            int(plan["standard_reference_cents"]), int(plan["final_sale_amount_cents"]),
            _canonical_plan(_json_object(plan.get("promotion"))), immutable,
            _canonical_plan(_json_object(plan.get("funds_conservation"))),
            f"jit-plan:{quote_id}",
        ),
    )
    reserved_by_seller: Dict[int, int] = {}
    for hop in list(plan.get("hops") or []):
        promo = _json_object(hop.get("promotion"))
        cur.execute(
            """INSERT INTO dealer_resale_fulfillment_hops
               (plan_id,root_order_id,hop_seq,seller_user_id,buyer_user_id,points,
                 existing_inventory_points,jit_shortfall_points,mint_points,
                 standard_reference_cents,
                 seller_cost_basis_cents,principal_recovery_cents,agent_payable_cents,
                 sale_amount_cents,margin_cents,edge_multiplier_bps,
                 relationship_version,source_kind,promotion_campaign_id,
                 promotion_discount_bps,promotion_funding_scope,promotion_sponsor_user_id,
                 promotion_snapshot_jsonb,idempotency_key)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)""",
            (
                plan_id, str(order_id), int(hop["hop_seq"]), int(hop["seller_user_id"]),
                int(hop["buyer_user_id"]), int(hop["points"]),
                int(hop["existing_inventory_points"]), int(hop["jit_shortfall_points"]),
                int(hop.get("mint_points") or 0),
                int(hop["standard_reference_cents"]), int(hop["seller_cost_basis_cents"]),
                int(hop["principal_recovery_cents"]), int(hop["agent_payable_cents"]),
                int(hop["sale_amount_cents"]), int(hop["margin_cents"]),
                int(hop["edge_multiplier_bps"]), hop.get("relationship_version"),
                str(hop["source_kind"]), promo.get("campaign_id"),
                int(hop.get("promotion_discount_bps") or 10000),
                promo.get("funding_scope"), promo.get("sponsor_user_id"),
                _canonical_plan(promo), f"jit-hop:{quote_id}:{int(hop['hop_seq'])}",
            ),
        )
        for seq, allocation in enumerate(list(hop.get("allocations") or [])):
            kind = str(allocation["allocation_kind"])
            cur.execute(
                """INSERT INTO dealer_resale_fulfillment_allocations
                   (plan_id,hop_seq,allocation_seq,allocation_kind,source_lot_id,
                    source_hop_seq,points,cost_basis_cents,promotion_campaign_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    plan_id, int(hop["hop_seq"]), seq, kind,
                    allocation.get("lot_id"), allocation.get("source_hop_seq"),
                    int(allocation["points"]), int(allocation["cost_basis_cents"]),
                    allocation.get("promotion_campaign_id"),
                ),
            )
            if kind == "existing_lot":
                seller_id = int(hop["seller_user_id"])
                _reserve_existing_allocation(
                    cur, seller_id=seller_id, allocation=allocation,
                    stale_code="JIT_INVENTORY_STALE",
                )
                reserved_by_seller[seller_id] = (
                    reserved_by_seller.get(seller_id, 0) + int(allocation["points"])
                )
    if plan["order_kind"] == "CONSUMER":
        leg = _json_object(plan.get("consumer_leg"))
        seller_id = int(leg["seller_user_id"])
        for allocation in list(leg.get("allocations") or []):
            if allocation.get("allocation_kind") != "existing_lot":
                continue
            _reserve_existing_allocation(
                cur, seller_id=seller_id, allocation=allocation,
                stale_code="CONSUMER_QUOTE_STALE",
            )
            reserved_by_seller[seller_id] = (
                reserved_by_seller.get(seller_id, 0) + int(allocation["points"])
            )
    for seller_id in sorted(reserved_by_seller):
        amount = reserved_by_seller[seller_id]
        cur.execute(
            """UPDATE agent_inventory_wallets
               SET paid_inventory_points=paid_inventory_points-%s,
                   frozen_inventory_points=frozen_inventory_points+%s,updated_at=NOW()
               WHERE agent_user_id=%s AND paid_inventory_points >= %s""",
            (amount, amount, seller_id, amount),
        )
        if cur.rowcount != 1:
            raise ResaleError("JIT_INVENTORY_STALE", "逐跳聚合库存不足 · 请重新报价")
    return {"plan_id": plan_id, "reserved_by_seller": reserved_by_seller}


def _reserve_v2_order(cur, *, order_id: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    cur.execute("SELECT * FROM dealer_resale_orders WHERE order_id=%s FOR UPDATE", (str(order_id),))
    existing = cur.fetchone()
    if existing:
        row = _row_dict(existing)
        if str(row["quote_id"]) != str(snapshot.get("price_quote_id")):
            raise ResaleError("RESALE_ORDER_CONFLICT", "订单已绑定另一份逐级转售报价")
        return row
    plan = _lock_and_revalidate_v2_plan(cur, snapshot=snapshot, order_kind="B2B")
    current = dict(plan["hops"][-1])
    cur.execute(
        """SELECT user_id,amount_cents,base_points,bonus_points,price_quote_id,payment_status
           FROM recharge_orders WHERE id=%s FOR UPDATE""", (str(order_id),),
    )
    recharge = _row_dict(cur.fetchone())
    if (
        not recharge or recharge["payment_status"] != "pending"
        or int(recharge["user_id"]) != int(plan["final_buyer_user_id"])
        or int(recharge["amount_cents"]) != int(plan["final_sale_amount_cents"])
        or int(recharge["base_points"]) != int(plan["points"])
        or str(recharge.get("price_quote_id") or "") != str(snapshot.get("price_quote_id") or "")
    ):
        raise ResaleError("RESALE_ORDER_SNAPSHOT_MISMATCH", "支付订单与 JIT 报价快照不一致")
    cur.execute(
        """SELECT buyer_user_id,final_price_cents,points_granted,bonus_points,status
           FROM price_quotes WHERE quote_id=%s FOR UPDATE""",
        (str(snapshot["price_quote_id"]),),
    )
    quote = _row_dict(cur.fetchone())
    if (
        not quote or quote["status"] != "issued"
        or int(quote["buyer_user_id"]) != int(plan["final_buyer_user_id"])
        or int(quote["final_price_cents"]) != int(plan["final_sale_amount_cents"])
        or int(quote["points_granted"]) != int(plan["points"])
    ):
        raise ResaleError("RESALE_QUOTE_MISMATCH", "持久化 JIT 报价不一致或已消费")
    source_kind = (
        "platform_purchase"
        if int(current["seller_user_id"]) == int(plan["platform_seller_user_id"])
        else "direct_resale"
    )
    allocations = list(current.get("allocations") or [])
    cur.execute(
        """INSERT INTO dealer_resale_orders
           (order_id,seller_user_id,buyer_user_id,points,seller_lot_allocations,
            seller_cost_basis_cents,sale_amount_cents,margin_cents,downstream_markup_bps,
            pricing_version,quote_id,relationship_version,payment_collector,
            refund_responsible_user_id,source_kind,state)
           VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,'PLATFORM',%s,%s,'reserved')
           RETURNING *""",
        (
            str(order_id), int(current["seller_user_id"]), int(current["buyer_user_id"]),
            int(current["points"]), _canonical_plan(allocations),
            int(current["seller_cost_basis_cents"]), int(current["sale_amount_cents"]),
            int(current["margin_cents"]), int(current["edge_multiplier_bps"]),
            str(plan["root_catalog_version"]), str(snapshot["price_quote_id"]),
            current.get("relationship_version"), int(current["seller_user_id"]), source_kind,
        ),
    )
    order = _row_dict(cur.fetchone())
    _persist_v2_plan_and_reservations(
        cur, order_id=str(order_id), quote_id=str(snapshot["price_quote_id"]), plan=plan,
    )
    return order


def _reserve_v2_consumer_sale(
    cur, *, order_id: str, snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    cur.execute("SELECT * FROM dealer_consumer_sales WHERE order_id=%s FOR UPDATE", (str(order_id),))
    existing = cur.fetchone()
    if existing:
        row = _row_dict(existing)
        if str(row["quote_id"]) != str(snapshot.get("price_quote_id")):
            raise ResaleError("CONSUMER_SALE_CONFLICT", "订单已绑定另一份零售报价")
        return row
    plan = _lock_and_revalidate_v2_plan(cur, snapshot=snapshot, order_kind="CONSUMER")
    leg = _json_object(plan.get("consumer_leg"))
    cur.execute(
        """SELECT user_id,agent_user_id,amount_cents,base_points,bonus_points,
                  price_quote_id,payment_status
           FROM recharge_orders WHERE id=%s FOR UPDATE""", (str(order_id),),
    )
    recharge = _row_dict(cur.fetchone())
    if (
        not recharge or recharge["payment_status"] != "pending"
        or int(recharge["user_id"]) != int(plan["final_buyer_user_id"])
        or int(recharge.get("agent_user_id") or 0) != int(plan["final_seller_user_id"])
        or int(recharge["amount_cents"]) != int(plan["final_sale_amount_cents"])
        or int(recharge["base_points"]) + int(recharge["bonus_points"]) != int(plan["points"])
        or str(recharge.get("price_quote_id") or "") != str(snapshot.get("price_quote_id") or "")
    ):
        raise ResaleError("CONSUMER_SALE_ORDER_MISMATCH", "消费者订单与 JIT 报价快照不一致")
    cur.execute(
        """SELECT buyer_user_id,final_price_cents,points_granted,bonus_points,status,used_order_id
           FROM price_quotes WHERE quote_id=%s FOR UPDATE""",
        (str(snapshot["price_quote_id"]),),
    )
    quote = _row_dict(cur.fetchone())
    if (
        not quote or quote["status"] != "consumed"
        or str(quote.get("used_order_id") or "") != str(order_id)
        or int(quote["buyer_user_id"]) != int(plan["final_buyer_user_id"])
        or int(quote["final_price_cents"]) != int(plan["final_sale_amount_cents"])
        or int(quote["points_granted"]) + int(quote["bonus_points"]) != int(plan["points"])
    ):
        raise ResaleError("CONSUMER_SALE_QUOTE_MISMATCH", "持久化零售 JIT 报价未原子消费")
    allocations = list(leg.get("allocations") or [])
    cur.execute(
        """INSERT INTO dealer_consumer_sales
           (order_id,seller_user_id,consumer_user_id,points,seller_lot_allocations,
             seller_cost_basis_cents,principal_recovery_cents,agent_payable_cents,
             sale_amount_cents,margin_cents,downstream_markup_bps,
             pricing_version,quote_id,payment_collector,refund_responsible_user_id,
             digital_goods_policy_code,digital_goods_acknowledged_at,state)
           VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,'PLATFORM',%s,%s,NOW(),'reserved')
           RETURNING *""",
        (
            str(order_id), int(leg["seller_user_id"]), int(leg["buyer_user_id"]),
            int(leg["points"]), _canonical_plan(allocations),
            int(leg["seller_cost_basis_cents"]), int(leg["principal_recovery_cents"]),
            int(leg["agent_payable_cents"]), int(leg["sale_amount_cents"]),
            int(leg["margin_cents"]), int(leg["edge_multiplier_bps"]),
            str(plan["root_catalog_version"]), str(snapshot["price_quote_id"]),
            int(leg["seller_user_id"]), str(snapshot["digital_goods_policy_code"]),
        ),
    )
    sale = _row_dict(cur.fetchone())
    _persist_v2_plan_and_reservations(
        cur, order_id=str(order_id), quote_id=str(snapshot["price_quote_id"]), plan=plan,
    )
    seq = 0
    for allocation in allocations:
        if allocation.get("allocation_kind") != "existing_lot":
            continue
        cur.execute(
            """INSERT INTO dealer_consumer_lot_allocations
               (order_id,seller_lot_id,allocation_seq,points,cost_basis_cents,status)
               VALUES (%s,%s,%s,%s,%s,'reserved')""",
            (
                str(order_id), str(allocation["lot_id"]), seq,
                int(allocation["points"]), int(allocation["cost_basis_cents"]),
            ),
        )
        seq += 1
    return sale


def reserve_consumer_sale(cur, *, order_id: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Reserve seller FIFO lots in the same transaction that consumes the retail quote."""
    if snapshot.get("consumer_resale_mode") is not True:
        raise ResaleError("CONSUMER_RESALE_SNAPSHOT_REQUIRED", "订单缺少消费者转售快照")
    assert_schema_ready(cur)
    if int(snapshot.get("consumer_resale_snapshot_version") or 0) == 2:
        return _reserve_v2_consumer_sale(cur, order_id=str(order_id), snapshot=snapshot)
    cur.execute("SELECT * FROM dealer_consumer_sales WHERE order_id=%s FOR UPDATE", (str(order_id),))
    existing = cur.fetchone()
    if existing:
        row = _row_dict(existing)
        if str(row["quote_id"]) != str(snapshot.get("price_quote_id")):
            raise ResaleError("CONSUMER_SALE_CONFLICT", "订单已绑定另一份零售报价")
        return row

    seller_id = int(snapshot["seller_user_id"])
    consumer_id = int(snapshot["consumer_user_id"])
    points = int(snapshot["points"])
    _require_active_service_seller(cur, seller_id)
    allocations = _json_array(snapshot.get("seller_lot_allocations"))
    cur.execute(
        """SELECT user_id, agent_user_id, amount_cents, base_points, bonus_points,
                  price_quote_id, payment_status
           FROM recharge_orders WHERE id=%s FOR UPDATE""",
        (str(order_id),),
    )
    recharge = _row_dict(cur.fetchone())
    if not recharge:
        raise ResaleError("RECHARGE_ORDER_NOT_FOUND", "消费者支付订单不存在")
    if (
        int(recharge["user_id"]) != consumer_id
        or int(recharge.get("agent_user_id") or 0) != seller_id
        or int(recharge["amount_cents"]) != int(snapshot["sale_amount_cents"])
        or int(recharge["base_points"]) + int(recharge["bonus_points"]) != points
        or str(recharge.get("price_quote_id") or "") != str(snapshot["price_quote_id"])
        or recharge["payment_status"] != "pending"
    ):
        raise ResaleError("CONSUMER_SALE_ORDER_MISMATCH", "消费者订单与零售报价快照不一致")
    cur.execute(
        """SELECT buyer_user_id, final_price_cents, points_granted, bonus_points,
                  status, used_order_id
           FROM price_quotes WHERE quote_id=%s FOR UPDATE""",
        (str(snapshot["price_quote_id"]),),
    )
    quote = _row_dict(cur.fetchone())
    if (
        not quote
        or int(quote["buyer_user_id"]) != consumer_id
        or int(quote["final_price_cents"]) != int(snapshot["sale_amount_cents"])
        or int(quote["points_granted"]) + int(quote["bonus_points"]) != points
        or quote["status"] != "consumed"
        or str(quote.get("used_order_id") or "") != str(order_id)
    ):
        raise ResaleError("CONSUMER_SALE_QUOTE_MISMATCH", "持久化零售报价未原子消费")

    locked: List[Dict[str, Any]] = []
    for expected in allocations:
        cur.execute(
            """SELECT lot_id, owner_agent_user_id, remaining_points,
                      remaining_cost_cents, status
               FROM dealer_inventory_lots WHERE lot_id=%s FOR UPDATE""",
            (str(expected["lot_id"]),),
        )
        lot = _row_dict(cur.fetchone())
        take = int(expected["points"])
        if (
            not lot or int(lot["owner_agent_user_id"]) != seller_id
            or lot["status"] != "active" or take <= 0
            or int(lot["remaining_points"]) < take
        ):
            raise ResaleError("CONSUMER_QUOTE_STALE", "服务方库存批次已变化 · 请重新报价")
        remaining_points = int(lot["remaining_points"])
        remaining_cost = int(lot["remaining_cost_cents"])
        cost = remaining_cost if take == remaining_points else remaining_cost * take // remaining_points
        if cost != int(expected["cost_basis_cents"]):
            raise ResaleError("CONSUMER_QUOTE_STALE", "服务方库存成本已变化 · 请重新报价")
        locked.append({"lot_id": str(lot["lot_id"]), "points": take, "cost_basis_cents": cost})
    if sum(item["points"] for item in locked) != points:
        raise ResaleError("CONSUMER_ALLOCATION_INVALID", "消费者零售算力分配不守恒")
    if sum(item["cost_basis_cents"] for item in locked) != int(snapshot["seller_cost_basis_cents"]):
        raise ResaleError("CONSUMER_ALLOCATION_INVALID", "消费者零售成本分配不守恒")
    wallet = _inventory_wallet(cur, seller_id)
    if int(wallet["paid_inventory_points"]) < points:
        raise ResaleError("SELLER_INVENTORY_INSUFFICIENT", "服务方聚合库存不足")

    cur.execute(
        """INSERT INTO dealer_consumer_sales
           (order_id,seller_user_id,consumer_user_id,points,seller_lot_allocations,
             seller_cost_basis_cents,principal_recovery_cents,agent_payable_cents,
             sale_amount_cents,margin_cents,downstream_markup_bps,
             pricing_version,quote_id,payment_collector,refund_responsible_user_id,
             digital_goods_policy_code,digital_goods_acknowledged_at,state)
           VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,'PLATFORM',%s,%s,NOW(),'reserved')
           RETURNING *""",
        (
            str(order_id), seller_id, consumer_id, points,
            json.dumps(locked, ensure_ascii=False, separators=(",", ":")),
            int(snapshot["seller_cost_basis_cents"]), int(snapshot["seller_cost_basis_cents"]),
            int(snapshot["sale_amount_cents"]), int(snapshot["sale_amount_cents"]),
            int(snapshot["margin_cents"]), int(snapshot["downstream_markup_bps"]),
            str(snapshot["pricing_version"]), str(snapshot["price_quote_id"]), seller_id,
            str(snapshot["digital_goods_policy_code"]),
        ),
    )
    sale = _row_dict(cur.fetchone())
    for seq, allocation in enumerate(locked):
        cur.execute(
            """UPDATE dealer_inventory_lots
               SET remaining_points=remaining_points-%s, reserved_points=reserved_points+%s,
                   remaining_cost_cents=remaining_cost_cents-%s,
                   reserved_cost_cents=reserved_cost_cents+%s, updated_at=NOW()
               WHERE lot_id=%s AND owner_agent_user_id=%s AND status='active'
                 AND remaining_points >= %s AND remaining_cost_cents >= %s""",
            (
                allocation["points"], allocation["points"], allocation["cost_basis_cents"],
                allocation["cost_basis_cents"], allocation["lot_id"], seller_id,
                allocation["points"], allocation["cost_basis_cents"],
            ),
        )
        if cur.rowcount != 1:
            raise ResaleError("CONSUMER_QUOTE_STALE", "服务方库存并发变化 · 请重新报价")
        cur.execute(
            """INSERT INTO dealer_consumer_lot_allocations
               (order_id,seller_lot_id,allocation_seq,points,cost_basis_cents,status)
               VALUES (%s,%s,%s,%s,%s,'reserved')""",
            (str(order_id), allocation["lot_id"], seq, allocation["points"], allocation["cost_basis_cents"]),
        )
    cur.execute(
        """UPDATE agent_inventory_wallets
           SET paid_inventory_points=paid_inventory_points-%s,
               frozen_inventory_points=frozen_inventory_points+%s, updated_at=NOW()
           WHERE agent_user_id=%s AND paid_inventory_points >= %s""",
        (points, points, seller_id, points),
    )
    if cur.rowcount != 1:
        raise ResaleError("SELLER_INVENTORY_INSUFFICIENT", "服务方库存预占失败")
    return sale


def reserve_order(cur, *, order_id: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    if not snapshot.get("resale_mode"):
        raise ResaleError("RESALE_SNAPSHOT_REQUIRED", "订单缺少逐级转售快照")
    assert_schema_ready(cur)
    if int(snapshot.get("resale_snapshot_version") or 0) == 2:
        return _reserve_v2_order(cur, order_id=str(order_id), snapshot=snapshot)
    cur.execute("SELECT * FROM dealer_resale_orders WHERE order_id=%s FOR UPDATE", (str(order_id),))
    existing = cur.fetchone()
    if existing:
        row = _row_dict(existing)
        if str(row["quote_id"]) != str(snapshot.get("price_quote_id")):
            raise ResaleError("RESALE_ORDER_CONFLICT", "订单已绑定另一份逐级转售报价")
        return row

    seller_id = int(snapshot["seller_user_id"])
    buyer_id = int(snapshot["buyer_user_id"])
    points = int(snapshot["points"])
    allocations = _json_array(snapshot.get("seller_lot_allocations"))
    source_kind = str(snapshot["source_kind"])
    if source_kind == "direct_resale":
        _require_active_service_seller(cur, seller_id)
    cur.execute(
        """SELECT user_id, amount_cents, base_points, bonus_points, price_quote_id,
                  payment_status
           FROM recharge_orders WHERE id=%s FOR UPDATE""",
        (str(order_id),),
    )
    recharge_order = _row_dict(cur.fetchone())
    if not recharge_order:
        raise ResaleError("RECHARGE_ORDER_NOT_FOUND", "支付订单不存在")
    if (
        int(recharge_order["user_id"]) != buyer_id
        or int(recharge_order["amount_cents"]) != int(snapshot["sale_amount_cents"])
        or int(recharge_order["base_points"]) + int(recharge_order["bonus_points"]) != points
        or str(recharge_order.get("price_quote_id") or "") != str(snapshot["price_quote_id"])
        or recharge_order["payment_status"] != "pending"
    ):
        raise ResaleError("RESALE_ORDER_SNAPSHOT_MISMATCH", "支付订单与逐级转售快照不一致")
    cur.execute(
        """SELECT buyer_user_id, final_price_cents, points_granted, bonus_points, status
           FROM price_quotes WHERE quote_id=%s FOR UPDATE""",
        (str(snapshot["price_quote_id"]),),
    )
    quote = _row_dict(cur.fetchone())
    if (
        not quote
        or int(quote["buyer_user_id"]) != buyer_id
        or int(quote["final_price_cents"]) != int(snapshot["sale_amount_cents"])
        or int(quote["points_granted"]) + int(quote["bonus_points"]) != points
        or quote["status"] != "issued"
    ):
        raise ResaleError("RESALE_QUOTE_MISMATCH", "持久化报价与逐级转售快照不一致或已消费")
    if source_kind in {"direct_resale", "platform_purchase"}:
        locked_allocations: List[Dict[str, Any]] = []
        for expected in allocations:
            cur.execute(
                """SELECT lot_id, owner_agent_user_id, remaining_points, remaining_cost_cents, status
                   FROM dealer_inventory_lots WHERE lot_id=%s FOR UPDATE""",
                (str(expected["lot_id"]),),
            )
            lot = _row_dict(cur.fetchone())
            if not lot or int(lot["owner_agent_user_id"]) != seller_id or lot["status"] != "active":
                raise ResaleError("RESALE_QUOTE_STALE", "卖方库存批次已变化 · 请重新报价")
            take = int(expected["points"])
            remaining_points = int(lot["remaining_points"])
            if take <= 0 or remaining_points < take:
                raise ResaleError("RESALE_QUOTE_STALE", "卖方库存不足 · 请重新报价")
            remaining_cost = int(lot["remaining_cost_cents"])
            cost = remaining_cost if take == remaining_points else remaining_cost * take // remaining_points
            if cost != int(expected["cost_basis_cents"]):
                raise ResaleError("RESALE_QUOTE_STALE", "卖方库存成本已变化 · 请重新报价")
            locked_allocations.append({
                "lot_id": str(lot["lot_id"]), "points": take, "cost_basis_cents": cost,
            })
        if sum(item["points"] for item in locked_allocations) != points:
            raise ResaleError("RESALE_ALLOCATION_INVALID", "报价批次算力不守恒")
        if sum(item["cost_basis_cents"] for item in locked_allocations) != int(snapshot["seller_cost_basis_cents"]):
            raise ResaleError("RESALE_ALLOCATION_INVALID", "报价批次成本不守恒")
        cur.execute(
            """SELECT paid_inventory_points, frozen_inventory_points
               FROM agent_inventory_wallets WHERE agent_user_id=%s FOR UPDATE""",
            (seller_id,),
        )
        wallet = _row_dict(cur.fetchone())
        if not wallet or int(wallet["paid_inventory_points"]) < points:
            raise ResaleError("SELLER_INVENTORY_INSUFFICIENT", "直属卖方聚合库存不足")
    else:
        raise ResaleError("RESALE_SOURCE_INVALID", "逐级转售来源非法")

    cur.execute(
        """INSERT INTO dealer_resale_orders
           (order_id, seller_user_id, buyer_user_id, points, seller_lot_allocations,
            seller_cost_basis_cents, sale_amount_cents, margin_cents, downstream_markup_bps,
            pricing_version, quote_id, relationship_version, payment_collector,
            refund_responsible_user_id, source_kind, state)
           VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,'PLATFORM',%s,%s,'reserved')
           RETURNING *""",
        (
            str(order_id), seller_id, buyer_id, points,
            json.dumps(allocations, ensure_ascii=False, separators=(",", ":")),
            int(snapshot["seller_cost_basis_cents"]), int(snapshot["sale_amount_cents"]),
            int(snapshot["margin_cents"]), int(snapshot["downstream_markup_bps"]),
            str(snapshot["pricing_version"]), str(snapshot["price_quote_id"]),
            snapshot.get("relationship_version"), seller_id, source_kind,
        ),
    )
    order = _row_dict(cur.fetchone())

    if source_kind in {"direct_resale", "platform_purchase"}:
        for seq, allocation in enumerate(allocations):
            cur.execute(
                """UPDATE dealer_inventory_lots
                   SET remaining_points=remaining_points-%s,
                       reserved_points=reserved_points+%s,
                       remaining_cost_cents=remaining_cost_cents-%s,
                       reserved_cost_cents=reserved_cost_cents+%s,
                       updated_at=NOW()
                   WHERE lot_id=%s AND owner_agent_user_id=%s AND status='active'
                     AND remaining_points >= %s AND remaining_cost_cents >= %s""",
                (
                    int(allocation["points"]), int(allocation["points"]),
                    int(allocation["cost_basis_cents"]), int(allocation["cost_basis_cents"]),
                    str(allocation["lot_id"]), seller_id,
                    int(allocation["points"]), int(allocation["cost_basis_cents"]),
                ),
            )
            if cur.rowcount != 1:
                raise ResaleError("RESALE_QUOTE_STALE", "卖方库存批次并发变化 · 请重新报价")
            cur.execute(
                """INSERT INTO dealer_inventory_lot_allocations
                   (order_id, seller_lot_id, allocation_seq, points, cost_basis_cents, status)
                   VALUES (%s,%s,%s,%s,%s,'reserved')""",
                (
                    str(order_id), str(allocation["lot_id"]), seq,
                    int(allocation["points"]), int(allocation["cost_basis_cents"]),
                ),
            )
        cur.execute(
            """UPDATE agent_inventory_wallets
               SET paid_inventory_points=paid_inventory_points-%s,
                   frozen_inventory_points=frozen_inventory_points+%s,
                   updated_at=NOW()
               WHERE agent_user_id=%s AND paid_inventory_points >= %s""",
            (points, points, seller_id, points),
        )
        if cur.rowcount != 1:
            raise ResaleError("SELLER_INVENTORY_INSUFFICIENT", "直属卖方库存预占失败")
    return order


def _cancel_v2_fulfillment_plan(cur, *, order_id: str, consumer: bool) -> bool:
    cur.execute(
        "SELECT * FROM dealer_resale_fulfillment_plans WHERE root_order_id=%s FOR UPDATE",
        (str(order_id),),
    )
    raw = cur.fetchone()
    if not raw:
        return False
    plan = _row_dict(raw)
    if plan["state"] == "cancelled":
        return True
    if plan["state"] != "reserved":
        raise ResaleError("JIT_PLAN_NOT_CANCELLABLE", "仅待支付 JIT 计划可释放")
    cur.execute(
        """SELECT a.*,h.seller_user_id FROM dealer_resale_fulfillment_allocations a
           JOIN dealer_resale_fulfillment_hops h
             ON h.plan_id=a.plan_id AND h.hop_seq=a.hop_seq
           WHERE a.plan_id=%s AND a.allocation_kind='existing_lot'
           ORDER BY a.source_lot_id FOR UPDATE OF a,h""", (str(plan["plan_id"]),),
    )
    rows = [_row_dict(row) for row in cur.fetchall()]
    restore: List[Dict[str, Any]] = [{
        "lot_id": row["source_lot_id"], "seller_id": int(row["seller_user_id"]),
        "points": int(row["points"]), "cost": int(row["cost_basis_cents"]),
    } for row in rows]
    if consumer:
        cur.execute(
            """SELECT a.*,s.seller_user_id FROM dealer_consumer_lot_allocations a
               JOIN dealer_consumer_sales s ON s.order_id=a.order_id
               WHERE a.order_id=%s AND a.status='reserved'
               ORDER BY a.seller_lot_id FOR UPDATE OF a,s""", (str(order_id),),
        )
        restore.extend({
            "lot_id": row["seller_lot_id"], "seller_id": int(row["seller_user_id"]),
            "points": int(row["points"]), "cost": int(row["cost_basis_cents"]),
        } for row in map(_row_dict, cur.fetchall()))
    by_seller: Dict[int, int] = {}
    for item in restore:
        cur.execute(
            """UPDATE dealer_inventory_lots
               SET remaining_points=remaining_points+%s,reserved_points=reserved_points-%s,
                   remaining_cost_cents=remaining_cost_cents+%s,
                   reserved_cost_cents=reserved_cost_cents-%s,status='active',updated_at=NOW()
               WHERE lot_id=%s AND owner_agent_user_id=%s
                 AND reserved_points >= %s AND reserved_cost_cents >= %s""",
            (
                item["points"], item["points"], item["cost"], item["cost"], item["lot_id"],
                item["seller_id"], item["points"], item["cost"],
            ),
        )
        if cur.rowcount != 1:
            raise ResaleError("JIT_RESERVATION_RELEASE_FAILED", "逐跳 lot 预占释放失败")
        by_seller[item["seller_id"]] = by_seller.get(item["seller_id"], 0) + item["points"]
    for seller_id in sorted(by_seller):
        points = by_seller[seller_id]
        cur.execute(
            """UPDATE agent_inventory_wallets
               SET paid_inventory_points=paid_inventory_points+%s,
                   frozen_inventory_points=frozen_inventory_points-%s,updated_at=NOW()
               WHERE agent_user_id=%s AND frozen_inventory_points >= %s""",
            (points, points, seller_id, points),
        )
        if cur.rowcount != 1:
            raise ResaleError("JIT_RESERVATION_RELEASE_FAILED", "逐跳聚合预占释放失败")
    cur.execute(
        """UPDATE dealer_resale_fulfillment_allocations SET status='cancelled'
           WHERE plan_id=%s AND status='reserved'""", (str(plan["plan_id"]),),
    )
    cur.execute(
        """UPDATE dealer_resale_fulfillment_hops SET state='cancelled',updated_at=NOW()
           WHERE plan_id=%s AND state='reserved'""", (str(plan["plan_id"]),),
    )
    if consumer:
        cur.execute(
            """UPDATE dealer_consumer_lot_allocations SET status='cancelled'
               WHERE order_id=%s AND status='reserved'""", (str(order_id),),
        )
    cur.execute(
        """UPDATE dealer_resale_fulfillment_plans
           SET state='cancelled',cancelled_at=NOW(),updated_at=NOW() WHERE plan_id=%s""",
        (str(plan["plan_id"]),),
    )
    return True


def cancel_reservation(cur, order_id: str, reason: str = "payment_intent_failed") -> bool:
    cur.execute("SELECT * FROM dealer_resale_orders WHERE order_id=%s FOR UPDATE", (str(order_id),))
    row = cur.fetchone()
    if not row:
        return False
    order = _row_dict(row)
    if order["state"] == "cancelled":
        return True
    if order["state"] != "reserved":
        raise ResaleError("RESALE_ORDER_NOT_CANCELLABLE", "仅待支付预占订单可释放")
    if _cancel_v2_fulfillment_plan(cur, order_id=str(order_id), consumer=False):
        cur.execute(
            "UPDATE dealer_resale_orders SET state='cancelled',updated_at=NOW() WHERE order_id=%s",
            (str(order_id),),
        )
        cur.execute(
            "UPDATE recharge_orders SET payment_status='cancelled' WHERE id=%s AND payment_status='pending'",
            (str(order_id),),
        )
        return True
    if order["source_kind"] in {"direct_resale", "platform_purchase"}:
        cur.execute(
            """SELECT * FROM dealer_inventory_lot_allocations
               WHERE order_id=%s ORDER BY allocation_seq FOR UPDATE""",
            (str(order_id),),
        )
        allocations = [_row_dict(item) for item in cur.fetchall()]
        for allocation in allocations:
            cur.execute(
                """UPDATE dealer_inventory_lots
                   SET remaining_points=remaining_points+%s,
                       reserved_points=reserved_points-%s,
                       remaining_cost_cents=remaining_cost_cents+%s,
                       reserved_cost_cents=reserved_cost_cents-%s,
                       status='active', updated_at=NOW()
                   WHERE lot_id=%s AND reserved_points >= %s AND reserved_cost_cents >= %s""",
                (
                    int(allocation["points"]), int(allocation["points"]),
                    int(allocation["cost_basis_cents"]), int(allocation["cost_basis_cents"]),
                    str(allocation["seller_lot_id"]), int(allocation["points"]),
                    int(allocation["cost_basis_cents"]),
                ),
            )
            if cur.rowcount != 1:
                raise ResaleError("RESERVATION_RELEASE_FAILED", "卖方批次预占释放失败")
        points = int(order["points"])
        cur.execute(
            """UPDATE agent_inventory_wallets
               SET paid_inventory_points=paid_inventory_points+%s,
                   frozen_inventory_points=frozen_inventory_points-%s,
                   updated_at=NOW()
               WHERE agent_user_id=%s AND frozen_inventory_points >= %s""",
            (points, points, int(order["seller_user_id"]), points),
        )
        if cur.rowcount != 1:
            raise ResaleError("RESERVATION_RELEASE_FAILED", "卖方聚合库存预占释放失败")
        cur.execute(
            "UPDATE dealer_inventory_lot_allocations SET status='cancelled' "
            "WHERE order_id=%s AND status='reserved'",
            (str(order_id),),
        )
    cur.execute(
        "UPDATE dealer_resale_orders SET state='cancelled', updated_at=NOW() WHERE order_id=%s",
        (str(order_id),),
    )
    cur.execute(
        "UPDATE recharge_orders SET payment_status='cancelled' WHERE id=%s AND payment_status='pending'",
        (str(order_id),),
    )
    return True


def cancel_consumer_reservation(
    cur, order_id: str, reason: str = "payment_intent_failed",
) -> bool:
    """Release a service seller's reserved lots after retail payment setup fails.

    The consumed quote is deliberately not revived: the customer must obtain a
    fresh quote, so no stale price or FIFO allocation can be replayed.
    """
    cur.execute("SELECT * FROM dealer_consumer_sales WHERE order_id=%s FOR UPDATE", (str(order_id),))
    row = cur.fetchone()
    if not row:
        return False
    sale = _row_dict(row)
    if sale["state"] == "cancelled":
        return True
    if sale["state"] != "reserved":
        raise ResaleError("CONSUMER_SALE_NOT_CANCELLABLE", "仅待支付的消费者库存预占可释放")
    if _cancel_v2_fulfillment_plan(cur, order_id=str(order_id), consumer=True):
        cur.execute(
            """UPDATE dealer_consumer_sales SET state='cancelled',updated_at=NOW()
               WHERE order_id=%s AND state='reserved'""", (str(order_id),),
        )
        cur.execute(
            """UPDATE recharge_orders SET payment_status='cancelled'
               WHERE id=%s AND payment_status='pending'""", (str(order_id),),
        )
        return True
    cur.execute(
        """SELECT * FROM dealer_consumer_lot_allocations
           WHERE order_id=%s ORDER BY allocation_seq FOR UPDATE""",
        (str(order_id),),
    )
    allocations = [_row_dict(item) for item in cur.fetchall()]
    if not allocations or sum(int(item["points"]) for item in allocations) != int(sale["points"]):
        raise ResaleError("CONSUMER_RESERVATION_RELEASE_FAILED", "消费者订单批次预占不守恒")
    for allocation in allocations:
        cur.execute(
            """UPDATE dealer_inventory_lots
               SET remaining_points=remaining_points+%s,
                   reserved_points=reserved_points-%s,
                   remaining_cost_cents=remaining_cost_cents+%s,
                   reserved_cost_cents=reserved_cost_cents-%s,
                   status='active', updated_at=clock_timestamp()
               WHERE lot_id=%s AND owner_agent_user_id=%s AND status='active'
                 AND reserved_points >= %s AND reserved_cost_cents >= %s""",
            (
                int(allocation["points"]), int(allocation["points"]),
                int(allocation["cost_basis_cents"]), int(allocation["cost_basis_cents"]),
                str(allocation["seller_lot_id"]), int(sale["seller_user_id"]),
                int(allocation["points"]), int(allocation["cost_basis_cents"]),
            ),
        )
        if cur.rowcount != 1:
            raise ResaleError("CONSUMER_RESERVATION_RELEASE_FAILED", "服务方批次预占释放失败")
    points = int(sale["points"])
    cur.execute(
        """UPDATE agent_inventory_wallets
           SET paid_inventory_points=paid_inventory_points+%s,
               frozen_inventory_points=frozen_inventory_points-%s,
               updated_at=clock_timestamp()
           WHERE agent_user_id=%s AND frozen_inventory_points >= %s""",
        (points, points, int(sale["seller_user_id"]), points),
    )
    if cur.rowcount != 1:
        raise ResaleError("CONSUMER_RESERVATION_RELEASE_FAILED", "服务方聚合库存预占释放失败")
    cur.execute(
        """UPDATE dealer_consumer_lot_allocations
           SET status='cancelled' WHERE order_id=%s AND status='reserved'""",
        (str(order_id),),
    )
    cur.execute(
        """UPDATE dealer_consumer_sales
           SET state='cancelled', updated_at=clock_timestamp()
           WHERE order_id=%s AND state='reserved'""",
        (str(order_id),),
    )
    if cur.rowcount != 1:
        raise ResaleError("CONSUMER_RESERVATION_RELEASE_FAILED", "消费者订单预占状态并发变化")
    cur.execute(
        """UPDATE recharge_orders SET payment_status='cancelled'
           WHERE id=%s AND payment_status='pending'""",
        (str(order_id),),
    )
    return True


def cancel_any_reservation(
    order_id: str, reason: str = "payment_intent_failed", *, cur=None,
) -> bool:
    """Idempotently release either B2B or consumer resale reservation."""
    if cur is None:
        with get_db() as conn:
            db_cur = conn.cursor()
            changed = cancel_any_reservation(order_id, reason, cur=db_cur)
            conn.commit()
            return changed
    cur.execute("SELECT to_regclass('public.dealer_consumer_sales') AS consumer_table, "
                "to_regclass('public.dealer_resale_orders') AS resale_table")
    tables = _row_dict(cur.fetchone())
    if tables.get("consumer_table") and cancel_consumer_reservation(cur, order_id, reason):
        return True
    if tables.get("resale_table") and cancel_reservation(cur, order_id, reason):
        return True
    return False


def release_stale_reservations(
    *, limit: int = 200, stale_after_minutes: int = 30, cur=None,
) -> Dict[str, Any]:
    """Identify stale reservations without changing money or inventory state.

    A local ``pending`` value cannot prove that the external channel did not
    collect cash: signed success callbacks may be delayed.  Releasing FIFO from
    this scheduler would create a paid-but-uncredited order.  Automatic release
    is therefore disabled until a channel-specific NOTPAY/CLOSED proof is
    persisted; candidates are returned for reconciliation only.
    """
    if cur is None:
        with get_db() as conn:
            db_cur = conn.cursor()
            result = release_stale_reservations(
                limit=limit, stale_after_minutes=stale_after_minutes, cur=db_cur,
            )
            return result
    limit = max(1, min(int(limit), 1000))
    minutes = max(10, min(int(stale_after_minutes), 24 * 60))
    cur.execute("SELECT to_regclass('public.dealer_consumer_sales') AS consumer_table, "
                "to_regclass('public.dealer_resale_orders') AS resale_table")
    tables = _row_dict(cur.fetchone())
    candidates: List[str] = []
    remaining = limit
    if tables.get("consumer_table"):
        cur.execute(
            """SELECT s.order_id
               FROM dealer_consumer_sales s
               JOIN recharge_orders r ON r.id=s.order_id
               WHERE s.state='reserved' AND r.payment_status='pending'
                 AND r.created_at <= clock_timestamp() - make_interval(mins => %s)
               ORDER BY r.created_at, s.order_id
               LIMIT %s""",
            (minutes, remaining),
        )
        consumer_ids = [str(_row_dict(row)["order_id"]) for row in cur.fetchall()]
        candidates.extend(consumer_ids)
        remaining -= len(consumer_ids)
    if remaining > 0 and tables.get("resale_table"):
        cur.execute(
            """SELECT o.order_id
               FROM dealer_resale_orders o
               JOIN recharge_orders r ON r.id=o.order_id
               WHERE o.state='reserved' AND r.payment_status='pending'
                 AND r.created_at <= clock_timestamp() - make_interval(mins => %s)
               ORDER BY r.created_at, o.order_id
               LIMIT %s""",
            (minutes, remaining),
        )
        resale_ids = [str(_row_dict(row)["order_id"]) for row in cur.fetchall()]
        candidates.extend(resale_ids)
    return {
        "released_count": 0,
        "order_ids": [],
        "reconciliation_required_count": len(candidates),
        "reconciliation_order_ids": candidates,
        "automatic_release_disabled": True,
        "db_clock": True,
    }


def has_resale_order(cur, order_id: str) -> bool:
    cur.execute("SELECT to_regclass('public.dealer_resale_orders') AS table_name")
    if not _row_dict(cur.fetchone()).get("table_name"):
        return False
    cur.execute("SELECT 1 FROM dealer_resale_orders WHERE order_id=%s", (str(order_id),))
    return cur.fetchone() is not None


def assert_resale_bonus_credit_allowed(cur, *, order_id: str, buyer_user_id: int) -> None:
    """Fail closed unless the paid/base resale transfer already settled.

    Tier and founder rewards are credited after the base transfer in the same
    payment transaction. This guard prevents a future bonus-only caller from
    using a merely reserved order as a free-credit capability.
    """
    cur.execute(
        """SELECT o.state,r.payment_status,o.buyer_user_id
             FROM dealer_resale_orders o
             JOIN recharge_orders r ON r.id=o.order_id
            WHERE o.order_id=%s
            FOR UPDATE OF o,r""",
        (str(order_id),),
    )
    row = _row_dict(cur.fetchone())
    if (
        not row
        or str(row.get("state") or "") != "paid"
        or str(row.get("payment_status") or "") != "paid"
        or int(row.get("buyer_user_id") or 0) != int(buyer_user_id)
    ):
        raise ResaleError(
            "RESALE_BONUS_BEFORE_PAID",
            "逐级转售基础库存尚未完成结算，禁止发放赠送库存",
        )


def _inventory_wallet(cur, user_id: int) -> Dict[str, Any]:
    from services.agent_inventory import get_or_create_inventory_wallet

    return get_or_create_inventory_wallet(cur, int(user_id))


def _insert_legacy_inventory_tx(
    cur, *, user_id: int, type_: str, points: int, paid_after: int, bonus_after: int,
    order_id: str, transfer_id: str, lot_id: Optional[str], counterparty_id: int,
    cost_basis_cents: int, description: str,
) -> None:
    cur.execute(
        """INSERT INTO agent_inventory_transactions
           (agent_user_id, type, pool, points, balance_paid_after, balance_bonus_after,
            related_order_id, description, transfer_id, lot_id, counterparty_user_id,
            cost_basis_cents)
           VALUES (%s,%s,'paid',%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            int(user_id), str(type_), int(points), int(paid_after), int(bonus_after),
            str(order_id), str(description), str(transfer_id), lot_id,
            int(counterparty_id), int(cost_basis_cents),
        ),
    )


def _mint_platform_supply(
    cur, *, plan: Dict[str, Any], hop: Dict[str, Any],
    allocations: List[Dict[str, Any]], mint_points: int, mint_cost_cents: int,
    paid_at: datetime, order_id: str,
) -> str:
    """T1 · 平台厂家按需铸造(工单 §1 + §3 四道护栏)。

    铸出 manufacturer_origin 批次 + manufacturer_origin_in 流水(**必带订单号**)+ 钱包入账;
    随后本跳的 transfer_out 立即把这批算力卖给下一跳,平台钱包净变化 0 ——
    与 bonus 侧 purchase_auto → allocate_to_customer 的双流水**同构**。

    🔴 铸造必须写流水,否则守恒等式(SUM(钱包三池) == SUM(流水))当场破。
    """
    from services import inventory_minting_guard as guard

    snapshot = _json_object(plan.get("immutable_snapshot_jsonb"))
    platform = int(snapshot.get("platform_seller_user_id") or 0)
    seller_id = int(hop["seller_user_id"])
    if platform <= 0:
        raise ResaleError("PLATFORM_SELLER_NOT_PREPARED", "履约计划快照缺少平台厂家账号")
    if seller_id != platform or str(hop["source_kind"]) != "platform_root":
        raise ResaleError("JIT_MINT_SOURCE_INVALID", "只有平台厂家根跳允许按需铸造")
    # 平台跳允许「已有余额(existing_lot) + 铸缺口(manufacturer_mint)」混合,
    # 但不许出现 jit_incoming —— 根节点没有上游可补货。
    if any(
        str(item["allocation_kind"]) not in ("manufacturer_mint", "existing_lot")
        for item in allocations
    ):
        raise ResaleError("JIT_MINT_SOURCE_INVALID", "平台厂家根跳不得由上游补货供给")

    try:
        guard.assert_mint_allowed(
            cur,
            source=guard.SOURCE_RESALE_PLATFORM_ROOT,
            agent_user_id=seller_id,
            pool=guard.POOL_PAID,
            points=int(mint_points),
            related_order_id=str(order_id),
            # 履约计划独立落库的铸造声明量:hops.mint_points
            # (与 allocations 的 manufacturer_mint 合计在调用点已比对过)
            plan_declared_points=int(hop["mint_points"]),
        )
    except guard.MintingGuardError as exc:
        raise ResaleError(exc.code, exc.message) from exc

    lot_id = _stable_id("DMM", f"{order_id}:{int(hop['hop_seq'])}")
    campaign = (
        hop.get("promotion_campaign_id")
        if str(hop.get("promotion_funding_scope") or "") == "PLATFORM" else None
    )
    catalog_version = str(plan["root_catalog_version"])
    evidence = {
        "mint_mode": "on_demand",
        "plan_id": str(plan["plan_id"]),
        "root_order_id": str(order_id),
        "hop_seq": int(hop["hop_seq"]),
        "workorder": "WORKORDER_ONDEMAND_MINTING_2026-07-29",
    }
    # 🔴 不写 root_order_id/source_hop_seq:那对列的唯一索引 ux_dealer_lot_root_hop
    # 属于**买方**这一跳产出的 JIT 批次(_settle_v2_fulfillment_hops 稍后就写同一对键),
    # 铸造批次占用会直接撞唯一键。铸造与订单的绑定由 evidence_jsonb +
    # manufacturer_origin_in 流水的 related_order_id/lot_id 承担(与 issue_manufacturer_lot 同形)。
    cur.execute(
        """INSERT INTO dealer_inventory_lots
           (lot_id,owner_agent_user_id,original_points,
            remaining_points,reserved_points,acquisition_cost_cents,remaining_cost_cents,
            reserved_cost_cents,acquired_at,status,source_kind,pricing_version,quote_id,
            evidence_jsonb,promotion_campaign_id,promotion_funding_scope,
            promotion_snapshot_jsonb,promotion_product_code,promotion_root_catalog_version)
           VALUES (%s,%s,%s,%s,0,%s,%s,0,%s,'active','manufacturer_origin',%s,%s,%s::jsonb,
                   %s,%s,%s::jsonb,%s,%s)""",
        (
            lot_id, seller_id, int(mint_points),
            int(mint_points), int(mint_cost_cents), int(mint_cost_cents), paid_at,
            catalog_version, str(plan["quote_id"]), _canonical_plan(evidence),
            campaign, ("PLATFORM" if campaign else None),
            _canonical_plan(_json_object(hop.get("promotion_snapshot_jsonb"))),
            (str(plan["product_code"]) if campaign else None),
            (catalog_version if campaign else None),
        ),
    )
    wallet = _inventory_wallet(cur, seller_id)
    paid_after = int(wallet["paid_inventory_points"]) + int(mint_points)
    bonus_after = int(wallet["bonus_inventory_points"])
    cur.execute(
        """UPDATE agent_inventory_wallets
           SET paid_inventory_points=paid_inventory_points+%s,
               total_purchased_points=total_purchased_points+%s,updated_at=NOW()
           WHERE agent_user_id=%s""",
        (int(mint_points), int(mint_points), seller_id),
    )
    if cur.rowcount != 1:
        raise ResaleError("JIT_MINT_WALLET_FAILED", "平台厂家按需铸造聚合库存入账失败")
    cur.execute(
        """INSERT INTO agent_inventory_transactions
           (agent_user_id,type,pool,points,balance_paid_after,balance_bonus_after,
            related_order_id,description,lot_id,cost_basis_cents)
           VALUES (%s,'manufacturer_origin_in','paid',%s,%s,%s,%s,%s,%s,%s)""",
        (
            seller_id, int(mint_points), paid_after, bonus_after, str(order_id),
            f"平台按需铸造 · plan={plan['plan_id']} · hop={int(hop['hop_seq'])}",
            lot_id, int(mint_cost_cents),
        ),
    )
    return lot_id


def _settle_v2_fulfillment_hops(
    cur, *, order_id: str, paid_at: datetime,
) -> Optional[Dict[str, Any]]:
    cur.execute(
        "SELECT * FROM dealer_resale_fulfillment_plans WHERE root_order_id=%s FOR UPDATE",
        (str(order_id),),
    )
    raw_plan = cur.fetchone()
    if not raw_plan:
        return None
    plan = _row_dict(raw_plan)
    if plan["state"] == "settled":
        from services.notification_events import NotificationEventType, RecipientKind
        from services.notification_outbox import enqueue_notification_event
        cur.execute(
            """SELECT * FROM dealer_resale_fulfillment_hops
               WHERE plan_id=%s ORDER BY hop_seq""", (str(plan["plan_id"]),),
        )
        settled_hops = [_row_dict(item) for item in cur.fetchall()]
        for settled_hop in settled_hops:
            hop_business_id = f"{order_id}:hop:{int(settled_hop['hop_seq'])}"
            hop_facts = {
                "business_no": str(order_id),
                "points": f"{int(settled_hop['points']):,}",
                "status": "本次库存交易已完成",
                "occurred_at": (
                    settled_hop.get("settled_at") or paid_at
                ).isoformat(timespec="seconds"),
            }
            for recipient_user_id, recipient_kind in (
                (int(settled_hop["seller_user_id"]), RecipientKind.TRADE_SELLER),
                (int(settled_hop["buyer_user_id"]), RecipientKind.TRADE_BUYER),
            ):
                enqueue_notification_event(
                    cur,
                    event_type=NotificationEventType.RESELL_COMPLETED,
                    business_id=hop_business_id,
                    terminal_state="completed",
                    recipient_user_id=recipient_user_id,
                    recipient_kind=recipient_kind,
                    facts=hop_facts,
                )
        cur.execute(
            """SELECT * FROM dealer_resale_fulfillment_hops
               WHERE plan_id=%s ORDER BY hop_seq DESC LIMIT 1""", (str(plan["plan_id"]),),
        )
        final = _row_dict(cur.fetchone())
        return {"idempotent": True, "plan": plan, "final_hop": final}
    if plan["state"] != "reserved":
        raise ResaleError("JIT_PLAN_STATE_INVALID", f"JIT 履约计划状态非法: {plan['state']}")
    cur.execute(
        """SELECT * FROM dealer_resale_fulfillment_hops
           WHERE plan_id=%s ORDER BY hop_seq FOR UPDATE""", (str(plan["plan_id"]),),
    )
    hops = [_row_dict(row) for row in cur.fetchall()]
    if not hops and plan["order_kind"] != "CONSUMER":
        raise ResaleError("JIT_PLAN_INVALID", "B2B JIT 履约计划没有逐跳记录")
    previous_lot_id: Optional[str] = None
    final_result: Dict[str, Any] = {}
    for hop in hops:
        hop_seq = int(hop["hop_seq"])
        if hop["state"] == "settled":
            previous_lot_id = str(hop.get("acquired_lot_id") or "") or None
            final_result = {"hop": hop, "lot_id": previous_lot_id}
            continue
        if hop["state"] != "reserved":
            raise ResaleError("JIT_HOP_STATE_INVALID", f"第 {hop_seq} 跳状态不可结算")
        cur.execute(
            """SELECT * FROM dealer_resale_fulfillment_allocations
               WHERE plan_id=%s AND hop_seq=%s ORDER BY allocation_seq FOR UPDATE""",
            (str(plan["plan_id"]), hop_seq),
        )
        allocations = [_row_dict(row) for row in cur.fetchall()]
        if not allocations or sum(int(row["points"]) for row in allocations) != int(hop["points"]):
            raise ResaleError("JIT_ALLOCATION_INVALID", f"第 {hop_seq} 跳库存分配不守恒")
        seller_id = int(hop["seller_user_id"])
        buyer_id = int(hop["buyer_user_id"])
        existing_points = int(hop["existing_inventory_points"])
        shortfall_points = int(hop["jit_shortfall_points"])
        # T1 · 平台厂家按需铸造:铸造量与成本都读**已落库**的 allocation 行,
        # 不信任何内存态;护栏 1 再拿它与 hops.points 这一独立行做精确比对。
        mint_allocations = [
            item for item in allocations
            if str(item["allocation_kind"]) == "manufacturer_mint"
        ]
        mint_points = sum(int(item["points"]) for item in mint_allocations)
        if mint_points != int(hop.get("mint_points") or 0):
            # 两张独立落库的表(hops 与 allocations)必须对上,对不上说明有人只改了一边
            raise ResaleError("JIT_MINT_PLAN_MISMATCH", "铸造量在履约计划两处记录不一致")
        minted_lot_id: Optional[str] = None
        if mint_points:
            minted_lot_id = _mint_platform_supply(
                cur, plan=plan, hop=hop, allocations=allocations,
                mint_points=mint_points,
                mint_cost_cents=sum(
                    int(item["cost_basis_cents"]) for item in mint_allocations
                ),
                paid_at=paid_at, order_id=str(order_id),
            )
        for allocation in allocations:
            if allocation["status"] != "reserved":
                raise ResaleError("JIT_ALLOCATION_STATE_INVALID", "逐跳预占状态异常")
            if allocation["allocation_kind"] == "manufacturer_mint":
                # 刚铸出的批次当场卖出 · 与 jit_incoming 同一原子转出形状
                cur.execute(
                    """UPDATE dealer_inventory_lots
                       SET remaining_points=remaining_points-%s,
                           remaining_cost_cents=remaining_cost_cents-%s,
                           status=CASE WHEN remaining_points=%s THEN 'consumed' ELSE 'active' END,
                           updated_at=NOW()
                       WHERE lot_id=%s AND owner_agent_user_id=%s AND status='active'
                         AND remaining_points >= %s AND remaining_cost_cents >= %s""",
                    (
                        int(allocation["points"]), int(allocation["cost_basis_cents"]),
                        int(allocation["points"]), minted_lot_id, seller_id,
                        int(allocation["points"]), int(allocation["cost_basis_cents"]),
                    ),
                )
                if cur.rowcount != 1:
                    raise ResaleError("JIT_MINT_LOT_INVALID", "按需铸造批次无法原子转出")
            elif allocation["allocation_kind"] == "existing_lot":
                cur.execute(
                    """UPDATE dealer_inventory_lots
                       SET reserved_points=reserved_points-%s,
                           reserved_cost_cents=reserved_cost_cents-%s,
                           status=CASE WHEN remaining_points=0 AND reserved_points=%s
                                       THEN 'consumed' ELSE 'active' END,updated_at=NOW()
                       WHERE lot_id=%s AND owner_agent_user_id=%s
                         AND reserved_points >= %s AND reserved_cost_cents >= %s""",
                    (
                        int(allocation["points"]), int(allocation["cost_basis_cents"]),
                        int(allocation["points"]), str(allocation["source_lot_id"]), seller_id,
                        int(allocation["points"]), int(allocation["cost_basis_cents"]),
                    ),
                )
                if cur.rowcount != 1:
                    raise ResaleError("JIT_RESERVATION_MISSING", "逐跳真实 lot 冻结量不足")
            else:
                if previous_lot_id is None or int(allocation["source_hop_seq"]) != hop_seq - 1:
                    raise ResaleError("JIT_LINEAGE_INVALID", "即时补货 lot 血缘断裂")
                cur.execute(
                    """UPDATE dealer_inventory_lots
                       SET remaining_points=remaining_points-%s,
                           remaining_cost_cents=remaining_cost_cents-%s,
                           status=CASE WHEN remaining_points=%s THEN 'consumed' ELSE 'active' END,
                           updated_at=NOW()
                       WHERE lot_id=%s AND owner_agent_user_id=%s AND status='active'
                         AND remaining_points >= %s AND remaining_cost_cents >= %s""",
                    (
                        int(allocation["points"]), int(allocation["cost_basis_cents"]),
                        int(allocation["points"]), previous_lot_id, seller_id,
                        int(allocation["points"]), int(allocation["cost_basis_cents"]),
                    ),
                )
                if cur.rowcount != 1:
                    raise ResaleError("JIT_LINEAGE_INVALID", "即时补货 lot 无法原子转出")
        # 铸出的算力立刻随本跳卖出 → 与 shortfall 同样从 paid 池扣回,
        # 平台钱包净变化 0(铸造 +N / 转出 -N 各一笔流水,守恒等式不动)。
        seller_debit_points = shortfall_points + mint_points
        cur.execute(
            """UPDATE agent_inventory_wallets
               SET paid_inventory_points=paid_inventory_points-%s,
                   frozen_inventory_points=frozen_inventory_points-%s,
                   total_allocated_points=total_allocated_points+%s,updated_at=NOW()
               WHERE agent_user_id=%s AND paid_inventory_points >= %s
                 AND frozen_inventory_points >= %s
               RETURNING paid_inventory_points,bonus_inventory_points""",
            (
                seller_debit_points, existing_points, int(hop["points"]), seller_id,
                seller_debit_points, existing_points,
            ),
        )
        seller_after = _row_dict(cur.fetchone())
        if not seller_after:
            raise ResaleError("JIT_RESERVATION_MISSING", "逐跳卖方聚合库存不足")
        buyer_wallet = _inventory_wallet(cur, buyer_id)
        buyer_paid_after = int(buyer_wallet["paid_inventory_points"]) + int(hop["points"])
        cur.execute(
            """UPDATE agent_inventory_wallets
               SET paid_inventory_points=%s,total_purchased_points=total_purchased_points+%s,
                   updated_at=NOW() WHERE agent_user_id=%s""",
            (buyer_paid_after, int(hop["points"]), buyer_id),
        )
        transfer_id = _stable_id("JHT", f"{order_id}:{hop_seq}")
        lot_id = _stable_id("JLT", f"{order_id}:{hop_seq}")
        promo = _json_object(hop.get("promotion_snapshot_jsonb"))
        lot_source = (
            "platform_purchase"
            if hop["source_kind"] == "platform_root" else "direct_resale"
        )
        is_final_b2b = (
            plan["order_kind"] == "B2B" and hop_seq == len(hops) - 1
        )
        cur.execute(
            """INSERT INTO dealer_inventory_lots
               (lot_id,owner_agent_user_id,source_order_id,root_order_id,source_hop_seq,
                original_points,remaining_points,reserved_points,acquisition_cost_cents,
                remaining_cost_cents,reserved_cost_cents,acquired_at,status,source_kind,
                 pricing_version,quote_id,evidence_jsonb,standard_reference_cents,
                 promotion_campaign_id,promotion_funding_scope,promotion_snapshot_jsonb,
                 promotion_product_code,promotion_root_catalog_version)
               VALUES (%s,%s,%s,%s,%s,%s,%s,0,%s,%s,0,%s,'active',%s,%s,%s,%s::jsonb,
                       %s,%s,%s,%s::jsonb,%s,%s)""",
            (
                lot_id, buyer_id, (str(order_id) if is_final_b2b else None), str(order_id),
                hop_seq, int(hop["points"]), int(hop["points"]),
                int(hop["sale_amount_cents"]), int(hop["sale_amount_cents"]), paid_at,
                lot_source, str(plan["root_catalog_version"]), str(plan["quote_id"]),
                _canonical_plan({"plan_id": str(plan["plan_id"]), "hop_seq": hop_seq,
                                 "transfer_id": transfer_id, "payment_collector": "PLATFORM"}),
                int(hop["standard_reference_cents"]), hop.get("promotion_campaign_id"),
                hop.get("promotion_funding_scope"), _canonical_plan(promo),
                (str(plan["product_code"]) if hop.get("promotion_campaign_id") else None),
                (str(plan["root_catalog_version"]) if hop.get("promotion_campaign_id") else None),
            ),
        )
        cur.execute(
            """INSERT INTO dealer_resale_hop_transfer_entries
               (plan_id,root_order_id,hop_seq,entry_seq,transfer_id,owner_agent_user_id,
                counterparty_user_id,direction,points_delta,lot_id,cost_basis_cents,
                balance_paid_after)
               VALUES (%s,%s,%s,0,%s,%s,%s,'transfer_out',%s,NULL,%s,%s),
                      (%s,%s,%s,1,%s,%s,%s,'transfer_in',%s,%s,%s,%s)""",
            (
                str(plan["plan_id"]), str(order_id), hop_seq, transfer_id, seller_id,
                buyer_id, -int(hop["points"]), int(hop["seller_cost_basis_cents"]),
                int(seller_after["paid_inventory_points"]),
                str(plan["plan_id"]), str(order_id), hop_seq, transfer_id, buyer_id,
                seller_id, int(hop["points"]), lot_id, int(hop["sale_amount_cents"]),
                buyer_paid_after,
            ),
        )
        _insert_legacy_inventory_tx(
            cur, user_id=seller_id, type_="resale_transfer_out", points=-int(hop["points"]),
            paid_after=int(seller_after["paid_inventory_points"]),
            bonus_after=int(seller_after["bonus_inventory_points"]), order_id=str(order_id),
            transfer_id=transfer_id, lot_id=None, counterparty_id=buyer_id,
            cost_basis_cents=int(hop["seller_cost_basis_cents"]), description="JIT 逐跳库存转出",
        )
        _insert_legacy_inventory_tx(
            cur, user_id=buyer_id, type_="resale_transfer_in", points=int(hop["points"]),
            paid_after=buyer_paid_after, bonus_after=int(buyer_wallet["bonus_inventory_points"]),
            order_id=str(order_id), transfer_id=transfer_id, lot_id=lot_id,
            counterparty_id=seller_id, cost_basis_cents=int(hop["sale_amount_cents"]),
            description="JIT 逐跳库存转入",
        )
        platform_revenue = (
            int(hop["sale_amount_cents"]) if hop["source_kind"] == "platform_root" else 0
        )
        cur.execute(
            """INSERT INTO dealer_resale_hop_profit_ledger
               (plan_id,root_order_id,hop_seq,seller_user_id,buyer_user_id,
                 seller_cost_basis_cents,sale_amount_cents,principal_recovery_cents,
                 margin_cents,platform_root_revenue_cents,agent_payable_cents,status,available_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s + INTERVAL '72 hours')""",
            (
                str(plan["plan_id"]), str(order_id), hop_seq, seller_id, buyer_id,
                int(hop["seller_cost_basis_cents"]), int(hop["sale_amount_cents"]),
                int(hop["principal_recovery_cents"]), int(hop["margin_cents"]),
                platform_revenue, int(hop["agent_payable_cents"]), paid_at,
            ),
        )
        cur.execute(
            """UPDATE dealer_resale_fulfillment_allocations
               SET status='consumed',consumed_at=NOW()
               WHERE plan_id=%s AND hop_seq=%s AND status='reserved'""",
            (str(plan["plan_id"]), hop_seq),
        )
        cur.execute(
            """UPDATE dealer_resale_fulfillment_hops
               SET state='settled',transfer_id=%s,acquired_lot_id=%s,settled_at=%s,updated_at=NOW()
               WHERE plan_id=%s AND hop_seq=%s""",
            (transfer_id, lot_id, paid_at, str(plan["plan_id"]), hop_seq),
        )
        from services.notification_events import NotificationEventType, RecipientKind
        from services.notification_outbox import enqueue_notification_event
        hop_business_id = f"{order_id}:hop:{hop_seq}"
        hop_facts = {
            "business_no": str(order_id),
            "points": f"{int(hop['points']):,}",
            "status": "本次库存交易已完成",
            "occurred_at": paid_at.isoformat(timespec="seconds"),
        }
        for recipient_user_id, recipient_kind in (
            (seller_id, RecipientKind.TRADE_SELLER),
            (buyer_id, RecipientKind.TRADE_BUYER),
        ):
            enqueue_notification_event(
                cur,
                event_type=NotificationEventType.RESELL_COMPLETED,
                business_id=hop_business_id,
                terminal_state="completed",
                recipient_user_id=recipient_user_id,
                recipient_kind=recipient_kind,
                facts=hop_facts,
            )
        previous_lot_id = lot_id
        final_result = {
            "hop": {**hop, "transfer_id": transfer_id, "acquired_lot_id": lot_id},
            "lot_id": lot_id, "buyer_paid_after": buyer_paid_after,
            "buyer_bonus": int(buyer_wallet["bonus_inventory_points"]),
            "seller_paid_after": int(seller_after["paid_inventory_points"]),
            "seller_bonus": int(seller_after["bonus_inventory_points"]),
        }
    cur.execute(
        """UPDATE dealer_resale_fulfillment_plans
           SET state='settled',settled_at=%s,updated_at=NOW() WHERE plan_id=%s""",
        (paid_at, str(plan["plan_id"])),
    )
    verify_persisted_funds_ledger(cur, root_order_id=str(order_id))
    return {"idempotent": False, "plan": plan, "final_hop": final_result.get("hop"), **final_result}


def settle_reserved_order(
    cur, *, buyer_user_id: int, total_points: int, order_id: str,
) -> Optional[Dict[str, Any]]:
    cur.execute("SELECT * FROM dealer_resale_orders WHERE order_id=%s FOR UPDATE", (str(order_id),))
    row = cur.fetchone()
    if not row:
        return None
    order = _row_dict(row)
    if int(order["buyer_user_id"]) != int(buyer_user_id) or int(order["points"]) != int(total_points):
        raise ResaleError("RESALE_CALLBACK_MISMATCH", "逐级转售回调买方或算力与订单不一致")
    if order["state"] in {"paid", "refund_pending", "refunded"}:
        if order["state"] == "paid":
            occurred = order.get("paid_at")
            cur.execute(
                "SELECT 1 FROM dealer_resale_fulfillment_plans WHERE root_order_id=%s",
                (str(order_id),),
            )
            if cur.fetchone() and occurred is not None:
                _settle_v2_fulfillment_hops(cur, order_id=str(order_id), paid_at=occurred)
            elif occurred is not None:
                from services.notification_events import NotificationEventType, RecipientKind
                from services.notification_outbox import enqueue_notification_event
                facts = {
                    "business_no": str(order_id),
                    "points": f"{int(order['points']):,}",
                    "status": "本次库存交易已完成",
                    "occurred_at": occurred.isoformat(timespec="seconds"),
                }
                for recipient_user_id, recipient_kind in (
                    (int(order["seller_user_id"]), RecipientKind.TRADE_SELLER),
                    (int(order["buyer_user_id"]), RecipientKind.TRADE_BUYER),
                ):
                    enqueue_notification_event(
                        cur,
                        event_type=NotificationEventType.RESELL_COMPLETED,
                        business_id=str(order_id),
                        terminal_state="completed",
                        recipient_user_id=recipient_user_id,
                        recipient_kind=recipient_kind,
                        facts=facts,
                    )
        wallet = _inventory_wallet(cur, buyer_user_id)
        return {**wallet, "resale_settled": True, "idempotent": True}
    if order["state"] != "reserved":
        raise ResaleError("RESALE_ORDER_STATE_INVALID", f"逐级转售订单状态非法: {order['state']}")

    cur.execute(
        "SELECT payment_status, amount_cents, paid_at, pricing_snapshot_jsonb "
        "FROM recharge_orders WHERE id=%s FOR UPDATE",
        (str(order_id),),
    )
    recharge = _row_dict(cur.fetchone())
    if recharge.get("payment_status") != "paid" or not recharge.get("paid_at"):
        raise ResaleError("RESALE_PAYMENT_NOT_FINAL", "支付订单尚未进入 paid 终态")
    if int(recharge["amount_cents"]) != int(order["sale_amount_cents"]):
        raise ResaleError("RESALE_PAYMENT_AMOUNT_MISMATCH", "支付金额与逐级转售订单快照不一致")

    cur.execute(
        "SELECT plan_id FROM dealer_resale_fulfillment_plans WHERE root_order_id=%s",
        (str(order_id),),
    )
    if cur.fetchone():
        settled = _settle_v2_fulfillment_hops(
            cur, order_id=str(order_id), paid_at=recharge["paid_at"],
        )
        if not settled or not settled.get("final_hop"):
            raise ResaleError("JIT_PLAN_INVALID", "B2B JIT 最终跳缺失")
        final = dict(settled["final_hop"])
        transfer_id = str(final["transfer_id"])
        lot_id = str(final["acquired_lot_id"])
        cur.execute(
            """INSERT INTO dealer_inventory_transfers
               (transfer_id,order_id,seller_user_id,buyer_user_id,points,
                seller_cost_basis_cents,sale_amount_cents,margin_cents,state)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'settled')""",
            (
                transfer_id, str(order_id), int(final["seller_user_id"]),
                int(final["buyer_user_id"]), int(final["points"]),
                int(final["seller_cost_basis_cents"]), int(final["sale_amount_cents"]),
                int(final["margin_cents"]),
            ),
        )
        cur.execute(
            """INSERT INTO dealer_inventory_transfer_entries
               (transfer_id,owner_agent_user_id,counterparty_user_id,direction,
                points_delta,lot_id,cost_basis_cents,balance_paid_after)
               VALUES (%s,%s,%s,'transfer_out',%s,NULL,%s,%s),
                      (%s,%s,%s,'transfer_in',%s,%s,%s,%s)""",
            (
                transfer_id, int(final["seller_user_id"]), int(final["buyer_user_id"]),
                -int(final["points"]), int(final["seller_cost_basis_cents"]),
                int(settled["seller_paid_after"]), transfer_id,
                int(final["buyer_user_id"]), int(final["seller_user_id"]),
                int(final["points"]), lot_id, int(final["sale_amount_cents"]),
                int(settled["buyer_paid_after"]),
            ),
        )
        cur.execute(
            """UPDATE dealer_resale_orders
               SET state='paid',transfer_id=%s,paid_at=%s,
                   refund_deadline=%s + INTERVAL '72 hours',updated_at=NOW()
               WHERE order_id=%s""",
            (transfer_id, recharge["paid_at"], recharge["paid_at"], str(order_id)),
        )
        wallet = _inventory_wallet(cur, int(order["buyer_user_id"]))
        return {
            **wallet, "resale_settled": True, "jit_fulfillment_settled": True,
            "transfer_id": transfer_id, "lot_id": lot_id,
        }

    seller_id = int(order["seller_user_id"])
    buyer_id = int(order["buyer_user_id"])
    points = int(order["points"])
    transfer_id = _stable_id("DTR", str(order_id))
    buyer_lot_id = _stable_id("DLT", str(order_id))
    seller_paid_after = 0
    seller_bonus_after = 0
    if order["source_kind"] in {"direct_resale", "platform_purchase"}:
        seller_wallet = _inventory_wallet(cur, seller_id)
        if int(seller_wallet["frozen_inventory_points"]) < points:
            raise ResaleError("SELLER_RESERVATION_MISSING", "直属卖方冻结库存不足")
        cur.execute(
            """UPDATE agent_inventory_wallets
               SET frozen_inventory_points=frozen_inventory_points-%s,
                   total_allocated_points=total_allocated_points+%s,
                   updated_at=NOW()
               WHERE agent_user_id=%s AND frozen_inventory_points >= %s
               RETURNING paid_inventory_points, bonus_inventory_points""",
            (points, points, seller_id, points),
        )
        seller_after = _row_dict(cur.fetchone())
        if not seller_after:
            raise ResaleError("SELLER_RESERVATION_MISSING", "直属卖方冻结库存结转失败")
        seller_paid_after = int(seller_after["paid_inventory_points"])
        seller_bonus_after = int(seller_after["bonus_inventory_points"])
        cur.execute(
            """SELECT * FROM dealer_inventory_lot_allocations
               WHERE order_id=%s ORDER BY allocation_seq FOR UPDATE""",
            (str(order_id),),
        )
        allocations = [_row_dict(item) for item in cur.fetchall()]
        if sum(int(item["points"]) for item in allocations) != points:
            raise ResaleError("RESALE_ALLOCATION_INVALID", "预占批次算力不守恒")
        for allocation in allocations:
            cur.execute(
                """UPDATE dealer_inventory_lots
                   SET reserved_points=reserved_points-%s,
                       reserved_cost_cents=reserved_cost_cents-%s,
                       status=CASE
                           WHEN remaining_points=0 AND reserved_points-%s=0 THEN 'consumed'
                           ELSE 'active' END,
                       updated_at=NOW()
                   WHERE lot_id=%s AND reserved_points >= %s AND reserved_cost_cents >= %s""",
                (
                    int(allocation["points"]), int(allocation["cost_basis_cents"]),
                    int(allocation["points"]), str(allocation["seller_lot_id"]),
                    int(allocation["points"]), int(allocation["cost_basis_cents"]),
                ),
            )
            if cur.rowcount != 1:
                raise ResaleError("SELLER_RESERVATION_MISSING", "卖方批次冻结结转失败")
        cur.execute(
            """UPDATE dealer_inventory_lot_allocations
               SET status='consumed', consumed_at=NOW()
               WHERE order_id=%s AND status='reserved'""",
            (str(order_id),),
        )

    buyer_wallet = _inventory_wallet(cur, buyer_id)
    buyer_new_paid = int(buyer_wallet["paid_inventory_points"]) + points
    buyer_bonus = int(buyer_wallet["bonus_inventory_points"])
    cur.execute(
        """UPDATE agent_inventory_wallets
           SET paid_inventory_points=%s,
               total_purchased_points=total_purchased_points+%s,
               updated_at=NOW()
           WHERE agent_user_id=%s""",
        (buyer_new_paid, points, buyer_id),
    )
    cur.execute(
        """INSERT INTO dealer_inventory_lots
           (lot_id, owner_agent_user_id, source_order_id, source_transfer_id,
            original_points, remaining_points, reserved_points,
            acquisition_cost_cents, remaining_cost_cents, reserved_cost_cents,
            acquired_at, status, source_kind, pricing_version, quote_id, evidence_jsonb)
           VALUES (%s,%s,%s,%s,%s,%s,0,%s,%s,0,%s,'active',%s,%s,%s,%s::jsonb)""",
        (
            buyer_lot_id, buyer_id, str(order_id), transfer_id, points, points,
            int(order["sale_amount_cents"]), int(order["sale_amount_cents"]),
            recharge["paid_at"], str(order["source_kind"]), str(order["pricing_version"]),
            str(order["quote_id"]), json.dumps({"payment_collector": "PLATFORM"}),
        ),
    )
    cur.execute(
        """INSERT INTO dealer_inventory_transfers
           (transfer_id, order_id, seller_user_id, buyer_user_id, points,
            seller_cost_basis_cents, sale_amount_cents, margin_cents, state)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'settled')""",
        (
            transfer_id, str(order_id), seller_id, buyer_id, points,
            int(order["seller_cost_basis_cents"]), int(order["sale_amount_cents"]),
            int(order["margin_cents"]),
        ),
    )
    cur.execute(
        """INSERT INTO dealer_inventory_transfer_entries
           (transfer_id, owner_agent_user_id, counterparty_user_id, direction,
            points_delta, lot_id, cost_basis_cents, balance_paid_after)
           VALUES (%s,%s,%s,'transfer_out',%s,NULL,%s,%s),
                  (%s,%s,%s,'transfer_in',%s,%s,%s,%s)""",
        (
            transfer_id, seller_id, buyer_id, -points,
            int(order["seller_cost_basis_cents"]), seller_paid_after,
            transfer_id, buyer_id, seller_id, points, buyer_lot_id,
            int(order["sale_amount_cents"]), buyer_new_paid,
        ),
    )
    _insert_legacy_inventory_tx(
        cur, user_id=seller_id, type_="resale_transfer_out", points=-points,
        paid_after=seller_paid_after, bonus_after=seller_bonus_after,
        order_id=str(order_id), transfer_id=transfer_id, lot_id=None,
        counterparty_id=buyer_id, cost_basis_cents=int(order["seller_cost_basis_cents"]),
        description="直属下级进货转出",
    )
    _insert_legacy_inventory_tx(
        cur, user_id=buyer_id, type_="resale_transfer_in", points=points,
        paid_after=buyer_new_paid, bonus_after=buyer_bonus,
        order_id=str(order_id), transfer_id=transfer_id, lot_id=buyer_lot_id,
        counterparty_id=seller_id, cost_basis_cents=int(order["sale_amount_cents"]),
        description="从直属卖方进货转入",
    )
    cur.execute(
        """INSERT INTO dealer_resale_profit_ledger
           (order_id, seller_user_id, buyer_user_id, seller_cost_basis_cents,
            sale_amount_cents, margin_cents, status, available_at)
           VALUES (%s,%s,%s,%s,%s,%s,'pending',%s + INTERVAL '72 hours')""",
        (
            str(order_id), seller_id, buyer_id, int(order["seller_cost_basis_cents"]),
            int(order["sale_amount_cents"]), int(order["margin_cents"]), recharge["paid_at"],
        ),
    )
    cur.execute(
        """UPDATE dealer_resale_orders
           SET state='paid', transfer_id=%s, paid_at=%s,
               refund_deadline=%s + INTERVAL '72 hours', updated_at=NOW()
           WHERE order_id=%s""",
        (transfer_id, recharge["paid_at"], recharge["paid_at"], str(order_id)),
    )
    from services.notification_events import NotificationEventType, RecipientKind
    from services.notification_outbox import enqueue_notification_event
    resale_facts = {
        "business_no": str(order_id),
        "points": f"{points:,}",
        "status": "本次库存交易已完成",
        "occurred_at": recharge["paid_at"].isoformat(timespec="seconds"),
    }
    for recipient_user_id, recipient_kind in (
        (seller_id, RecipientKind.TRADE_SELLER),
        (buyer_id, RecipientKind.TRADE_BUYER),
    ):
        enqueue_notification_event(
            cur,
            event_type=NotificationEventType.RESELL_COMPLETED,
            business_id=str(order_id),
            terminal_state="completed",
            recipient_user_id=recipient_user_id,
            recipient_kind=recipient_kind,
            facts=resale_facts,
        )
    return {
        "paid_inventory_points": buyer_new_paid,
        "bonus_inventory_points": buyer_bonus,
        "resale_settled": True,
        "transfer_id": transfer_id,
        "lot_id": buyer_lot_id,
    }


def has_consumer_sale(cur, order_id: str) -> bool:
    cur.execute("SELECT to_regclass('public.dealer_consumer_sales') AS table_name")
    if not _row_dict(cur.fetchone()).get("table_name"):
        return False
    cur.execute("SELECT 1 FROM dealer_consumer_sales WHERE order_id=%s", (str(order_id),))
    return cur.fetchone() is not None


def _settle_v2_consumer_inventory(
    cur, *, order_id: str, sale: Dict[str, Any], paid_at: datetime,
) -> Dict[str, Any]:
    upstream = _settle_v2_fulfillment_hops(cur, order_id=str(order_id), paid_at=paid_at)
    if upstream is None:
        raise ResaleError("JIT_PLAN_REQUIRED", "消费者 JIT 履约计划缺失")
    cur.execute(
        """SELECT immutable_snapshot_jsonb FROM dealer_resale_fulfillment_plans
           WHERE root_order_id=%s FOR UPDATE""", (str(order_id),),
    )
    plan = _json_object(_row_dict(cur.fetchone()).get("immutable_snapshot_jsonb"))
    leg = _json_object(plan.get("consumer_leg"))
    if (
        int(leg.get("seller_user_id") or 0) != int(sale["seller_user_id"])
        or int(leg.get("buyer_user_id") or 0) != int(sale["consumer_user_id"])
        or int(leg.get("points") or 0) != int(sale["points"])
        or int(leg.get("seller_cost_basis_cents") or 0) != int(sale["seller_cost_basis_cents"])
    ):
        raise ResaleError("CONSUMER_CALLBACK_MISMATCH", "消费者 JIT 本跳快照不一致")
    seller_id = int(sale["seller_user_id"])
    existing_points = int(leg.get("existing_inventory_points") or 0)
    shortfall = int(leg.get("jit_shortfall_points") or 0)
    cur.execute(
        """SELECT * FROM dealer_consumer_lot_allocations
           WHERE order_id=%s ORDER BY allocation_seq FOR UPDATE""", (str(order_id),),
    )
    allocations = [_row_dict(row) for row in cur.fetchall()]
    if sum(int(row["points"]) for row in allocations) != existing_points:
        raise ResaleError("CONSUMER_ALLOCATION_INVALID", "消费者真实 lot 预占不守恒")
    for allocation in allocations:
        if allocation["status"] != "reserved":
            raise ResaleError("CONSUMER_ALLOCATION_STATE_INVALID", "消费者真实 lot 预占状态异常")
        cur.execute(
            """UPDATE dealer_inventory_lots
               SET reserved_points=reserved_points-%s,reserved_cost_cents=reserved_cost_cents-%s,
                   status=CASE WHEN remaining_points=0 AND reserved_points=%s
                               THEN 'consumed' ELSE 'active' END,updated_at=NOW()
               WHERE lot_id=%s AND owner_agent_user_id=%s
                 AND reserved_points >= %s AND reserved_cost_cents >= %s""",
            (
                int(allocation["points"]), int(allocation["cost_basis_cents"]),
                int(allocation["points"]), str(allocation["seller_lot_id"]), seller_id,
                int(allocation["points"]), int(allocation["cost_basis_cents"]),
            ),
        )
        if cur.rowcount != 1:
            raise ResaleError("CONSUMER_RESERVED_INVENTORY_MISSING", "消费者真实 lot 冻结量不足")
    if shortfall:
        final_hop = dict(upstream.get("final_hop") or {})
        lot_id = str(final_hop.get("acquired_lot_id") or "")
        if not lot_id or int(final_hop.get("buyer_user_id") or 0) != seller_id:
            raise ResaleError("JIT_LINEAGE_INVALID", "消费者即时补货未到达直属服务方")
        jit_cost = int(final_hop["sale_amount_cents"])
        cur.execute(
            """UPDATE dealer_inventory_lots
               SET remaining_points=remaining_points-%s,remaining_cost_cents=remaining_cost_cents-%s,
                   status=CASE WHEN remaining_points=%s THEN 'consumed' ELSE 'active' END,
                   updated_at=NOW()
               WHERE lot_id=%s AND owner_agent_user_id=%s AND status='active'
                 AND remaining_points >= %s AND remaining_cost_cents >= %s""",
            (shortfall, jit_cost, shortfall, lot_id, seller_id, shortfall, jit_cost),
        )
        if cur.rowcount != 1:
            raise ResaleError("JIT_LINEAGE_INVALID", "消费者即时补货 lot 无法转出")
    cur.execute(
        """UPDATE dealer_consumer_lot_allocations SET status='consumed',consumed_at=NOW()
           WHERE order_id=%s AND status='reserved'""", (str(order_id),),
    )
    cur.execute(
        """UPDATE agent_inventory_wallets
           SET paid_inventory_points=paid_inventory_points-%s,
               frozen_inventory_points=frozen_inventory_points-%s,
               total_allocated_points=total_allocated_points+%s,updated_at=NOW()
           WHERE agent_user_id=%s AND paid_inventory_points >= %s
             AND frozen_inventory_points >= %s
           RETURNING paid_inventory_points,bonus_inventory_points""",
        (shortfall, existing_points, int(sale["points"]), seller_id, shortfall, existing_points),
    )
    after = _row_dict(cur.fetchone())
    if not after:
        raise ResaleError("CONSUMER_RESERVED_INVENTORY_MISSING", "服务方 JIT 聚合库存不足")
    return after


def settle_consumer_sale(
    cur, *, order_id: str, consumer_user_id: int, pricing_snapshot: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Settle one service-provider → consumer sale from the reserved immutable snapshot."""
    cur.execute("SELECT * FROM dealer_consumer_sales WHERE order_id=%s FOR UPDATE", (str(order_id),))
    row = cur.fetchone()
    if not row:
        return None
    sale = _row_dict(row)
    if int(sale["consumer_user_id"]) != int(consumer_user_id):
        raise ResaleError("CONSUMER_CALLBACK_MISMATCH", "消费者支付回调归属不一致")
    if sale["state"] in {"paid", "refund_pending", "refunded", "manual_review"}:
        return {"consumer_resale_settled": True, "idempotent": True}
    if sale["state"] != "reserved":
        raise ResaleError("CONSUMER_SALE_STATE_INVALID", "消费者零售订单不在可结算状态")
    cur.execute(
        """SELECT id,user_id,agent_user_id,amount_cents,base_points,bonus_points,
                  payment_status,paid_at,price_quote_id
           FROM recharge_orders WHERE id=%s FOR UPDATE""",
        (str(order_id),),
    )
    recharge = _row_dict(cur.fetchone())
    base_points = int(recharge.get("base_points") or 0)
    bonus_points = int(recharge.get("bonus_points") or 0)
    points = int(sale["points"])
    if (
        not recharge or recharge["payment_status"] != "paid" or not recharge.get("paid_at")
        or int(recharge["user_id"]) != int(consumer_user_id)
        or int(recharge.get("agent_user_id") or 0) != int(sale["seller_user_id"])
        or int(recharge["amount_cents"]) != int(sale["sale_amount_cents"])
        or base_points + bonus_points != points
        or str(recharge.get("price_quote_id") or "") != str(sale["quote_id"])
    ):
        raise ResaleError("CONSUMER_CALLBACK_MISMATCH", "消费者支付回调与订单快照不一致")
    if pricing_snapshot.get("consumer_resale_mode") is not True:
        raise ResaleError("CONSUMER_RESALE_SNAPSHOT_REQUIRED", "支付回调缺少消费者转售快照")
    for key, expected in (
        ("seller_user_id", sale["seller_user_id"]),
        ("consumer_user_id", sale["consumer_user_id"]),
        ("points", sale["points"]),
        ("seller_cost_basis_cents", sale["seller_cost_basis_cents"]),
        ("principal_recovery_cents", sale["principal_recovery_cents"]),
        ("agent_payable_cents", sale["agent_payable_cents"]),
        ("sale_amount_cents", sale["sale_amount_cents"]),
        ("margin_cents", sale["margin_cents"]),
        ("downstream_markup_bps", sale["downstream_markup_bps"]),
        ("pricing_version", sale["pricing_version"]),
        ("price_quote_id", sale["quote_id"]),
    ):
        if str(pricing_snapshot.get(key)) != str(expected):
            raise ResaleError("CONSUMER_CALLBACK_MISMATCH", f"消费者快照字段 {key} 不一致")

    tool_points = int(pricing_snapshot.get("tool_points", base_points) or 0)
    publish_points = int(pricing_snapshot.get("publish_points", 0) or 0)
    snap_bonus = int(pricing_snapshot.get("bonus_points", bonus_points) or 0)
    if tool_points + publish_points != base_points or snap_bonus != bonus_points:
        raise ResaleError("CONSUMER_CREDIT_SPLIT_INVALID", "消费者到账算力分轨不守恒")

    seller_id = int(sale["seller_user_id"])
    cur.execute(
        "SELECT 1 FROM dealer_resale_fulfillment_plans WHERE root_order_id=%s",
        (str(order_id),),
    )
    if cur.fetchone():
        seller_after = _settle_v2_consumer_inventory(
            cur, order_id=str(order_id), sale=sale, paid_at=recharge["paid_at"],
        )
    else:
        seller_wallet = _inventory_wallet(cur, seller_id)
        if int(seller_wallet["frozen_inventory_points"]) < points:
            raise ResaleError("CONSUMER_RESERVED_INVENTORY_MISSING", "服务方冻结库存不足")
        cur.execute(
            """SELECT * FROM dealer_consumer_lot_allocations
               WHERE order_id=%s ORDER BY allocation_seq FOR UPDATE""",
            (str(order_id),),
        )
        allocations = [_row_dict(item) for item in cur.fetchall()]
        if not allocations or sum(int(item["points"]) for item in allocations) != points:
            raise ResaleError("CONSUMER_ALLOCATION_INVALID", "服务方冻结批次不守恒")
        for allocation in allocations:
            if allocation["status"] != "reserved":
                raise ResaleError("CONSUMER_ALLOCATION_STATE_INVALID", "服务方批次预占状态异常")
            cur.execute(
                """UPDATE dealer_inventory_lots
                   SET reserved_points=reserved_points-%s,
                       reserved_cost_cents=reserved_cost_cents-%s,
                       status=CASE WHEN remaining_points=0 AND reserved_points=%s
                                   THEN 'consumed' ELSE 'active' END,
                       updated_at=NOW()
                   WHERE lot_id=%s AND reserved_points >= %s AND reserved_cost_cents >= %s""",
                (
                    int(allocation["points"]), int(allocation["cost_basis_cents"]),
                    int(allocation["points"]), str(allocation["seller_lot_id"]),
                    int(allocation["points"]), int(allocation["cost_basis_cents"]),
                ),
            )
            if cur.rowcount != 1:
                raise ResaleError("CONSUMER_RESERVED_INVENTORY_MISSING", "服务方批次冻结量不足")
        cur.execute(
            """UPDATE dealer_consumer_lot_allocations
               SET status='consumed', consumed_at=NOW()
               WHERE order_id=%s AND status='reserved'""",
            (str(order_id),),
        )
        cur.execute(
            """UPDATE agent_inventory_wallets
               SET frozen_inventory_points=frozen_inventory_points-%s,
                   total_allocated_points=total_allocated_points+%s, updated_at=NOW()
               WHERE agent_user_id=%s AND frozen_inventory_points >= %s
               RETURNING paid_inventory_points,bonus_inventory_points""",
            (points, points, seller_id, points),
        )
        seller_after = _row_dict(cur.fetchone())
        if not seller_after:
            raise ResaleError("CONSUMER_RESERVED_INVENTORY_MISSING", "服务方聚合冻结库存不足")

    # [单账本收敛 2026-07-27] 原来这里做一次"钱包搬运":complete_recharge 刚把充值
    # 记进消费者的 user_wallets,这段又把它扣出来、写一笔
    # v35_migrated_to_customer_credit 流水、再 allocate_credit 塞进信用钱包。
    # 🔴 这里就是两本账的头号入账源 —— user 149 在钱包里看到的「-19,500」正是这段写的
    #    (生产实证:point_transactions 该类型 3 笔 -50,818 全部 source=dealer_consumer_resale)。
    # Owner 2026-07-27 定「不能有两本账」,整段删除:充值本来就该留在 user_wallets,
    # 消费者拿到的就是充值算力 + 赠送算力,不再搬去第二个钱包。
    # ⚠️ 上面的库存拆账(批次冻结释放 / lot allocation / 服务方聚合库存)与本次改动无关,
    #    一行未动 —— 删的只是"把消费者的钱搬进信用钱包"这件事。

    transfer_id = _stable_id("DCT", str(order_id))
    cur.execute(
        """INSERT INTO dealer_consumer_transfer_entries
           (transfer_id,order_id,seller_user_id,consumer_user_id,direction,
            points_delta,cost_basis_cents)
           VALUES (%s,%s,%s,%s,'seller_out',%s,%s),
                  (%s,%s,%s,%s,'consumer_in',%s,%s)""",
        (
            transfer_id, str(order_id), seller_id, int(consumer_user_id), -points,
            int(sale["seller_cost_basis_cents"]), transfer_id, str(order_id), seller_id,
            int(consumer_user_id), points, int(sale["sale_amount_cents"]),
        ),
    )
    _insert_legacy_inventory_tx(
        cur, user_id=seller_id, type_="consumer_sale_out", points=-points,
        paid_after=int(seller_after["paid_inventory_points"]),
        bonus_after=int(seller_after["bonus_inventory_points"]), order_id=str(order_id),
        transfer_id=transfer_id, lot_id=None, counterparty_id=int(consumer_user_id),
        cost_basis_cents=int(sale["seller_cost_basis_cents"]), description="向直属客户销售算力",
    )

    # The withdrawal SSOT receives historical principal recovery plus this
    # seller's margin. JIT incoming cost is already assigned to upstream hops.
    cur.execute(
        """INSERT INTO agent_revenue_ledger
           (agent_user_id,source,recharge_order_id,customer_user_id,
            customer_paid_cents,factory_cents,gateway_fee_bps,gateway_fee_cents,
            settlement_service_fee_bps,settlement_service_fee_cents,
            agent_margin_before_tax_cents,tax_rate_bps,tax_mode,tax_withholding_cents,
            agent_settlement_cents,status,settle_at,manual_review_required,note)
           VALUES (%s,'recharge',%s,%s,%s,0,0,0,0,0,%s,0,'withheld',0,%s,
                    'frozen',%s + INTERVAL '72 hours',FALSE,%s)
           RETURNING id""",
        (
            seller_id, str(order_id), int(consumer_user_id), int(sale["sale_amount_cents"]),
            int(sale["margin_cents"]), int(sale["agent_payable_cents"]),
            recharge["paid_at"], "逐级库存转售 · 历史本金回收 + 本跳利润",
        ),
    )
    revenue_row = _row_dict(cur.fetchone())
    revenue_id = int(revenue_row["id"])
    cur.execute(
        """UPDATE dealer_consumer_sales
           SET state='paid',transfer_id=%s,revenue_ledger_id=%s,paid_at=%s,updated_at=NOW()
           WHERE order_id=%s""",
        (transfer_id, revenue_id, recharge["paid_at"], str(order_id)),
    )
    cur.execute(
        "SELECT 1 FROM dealer_resale_fulfillment_plans WHERE root_order_id=%s",
        (str(order_id),),
    )
    if cur.fetchone():
        verify_persisted_funds_ledger(cur, root_order_id=str(order_id))
    return {
        "consumer_resale_settled": True,
        "transfer_id": transfer_id,
        "revenue_ledger_id": revenue_id,
        "seller_margin_cents": int(sale["margin_cents"]),
    }


def _b2b_bonus_refund_profile(
    order: Dict[str, Any], recharge: Dict[str, Any],
) -> Dict[str, Any]:
    """Classify old merged inventory versus new paid/bonus split orders."""
    base_points = int(recharge.get("base_points") or 0)
    bonus_points = int(recharge.get("bonus_points") or 0)
    resale_points = int(order.get("points") or 0)
    snapshot = _json_object(recharge.get("pricing_snapshot_jsonb"))
    if resale_points == base_points:
        tier_enabled = bool(snapshot.get("channel_tier_enabled"))
        return {
            "mode": "split_v1",
            "base_points": base_points,
            "catalog_bonus_points": bonus_points,
            "channel_tier_enabled": tier_enabled,
            "expected_tier_bonus_points": int(
                snapshot.get("tier_bonus_points", bonus_points if tier_enabled else 0) or 0
            ),
            "untracked_bonus_points": 0 if tier_enabled else bonus_points,
        }
    if bonus_points > 0 and resale_points == base_points + bonus_points:
        return {
            "mode": "legacy_merged_paid",
            "base_points": base_points,
            "catalog_bonus_points": bonus_points,
            "channel_tier_enabled": False,
            "expected_tier_bonus_points": 0,
            "untracked_bonus_points": 0,
        }
    raise ResaleError(
        "B2B_BONUS_SNAPSHOT_MISMATCH",
        "逐级转售订单基础/赠送库存快照不守恒，禁止进入现金退款",
    )


def _reserve_b2b_order_bonus(
    cur, *, order: Dict[str, Any], recharge: Dict[str, Any],
) -> Dict[str, Any]:
    profile = _b2b_bonus_refund_profile(order, recharge)
    bundle: Dict[str, Any] = {"profile": profile, "state": "not_applicable"}
    if profile["mode"] == "legacy_merged_paid":
        return bundle
    from services.bonus_grants import reserve_agent_order_bonus_for_refund

    try:
        reservation = reserve_agent_order_bonus_for_refund(
            cur,
            int(order["buyer_user_id"]),
            str(order["order_id"]),
            untracked_bonus_points=int(profile["untracked_bonus_points"]),
        )
    except ValueError as exc:
        raise ResaleError("B2B_BONUS_REFUND_RESERVE_FAILED", str(exc)) from exc
    expected_tier = int(profile["expected_tier_bonus_points"])
    actual_tier = sum(
        int(item.get("granted_points") or 0)
        for item in list(reservation.get("grants") or [])
        if item.get("grant_type") == "tier_purchase"
    )
    if expected_tier != actual_tier:
        raise ResaleError(
            "B2B_TIER_BONUS_GRANT_MISMATCH",
            "等级赠送快照与持久赠送账本不一致，禁止进入现金退款",
        )
    bundle.update({"state": "reserved", "reservation": reservation})
    return bundle


def _clawback_b2b_order_bonus(
    cur, *, order: Dict[str, Any], recharge: Dict[str, Any], refund: Dict[str, Any],
) -> Dict[str, Any]:
    audit = _json_object(refund.get("audit_jsonb"))
    bundle = _json_object(audit.get("bonus_refund"))
    if not bundle:
        # Compatibility for a refund row created before bonus reservations existed.
        bundle = _reserve_b2b_order_bonus(cur, order=order, recharge=recharge)
    if bundle.get("state") == "revoked":
        prior_result = _json_object(bundle.get("result"))
        return prior_result or {"revoked_points": 0, "idempotent": True}
    if _json_object(bundle.get("profile")).get("mode") == "legacy_merged_paid":
        return {"revoked_points": 0, "legacy_merged": True}
    reservation = _json_object(bundle.get("reservation"))
    if not reservation:
        raise ResaleError("B2B_BONUS_REFUND_SNAPSHOT_MISSING", "赠送库存退款冻结快照缺失")
    from services.bonus_grants import revoke_agent_order_bonus_for_refund

    try:
        result = revoke_agent_order_bonus_for_refund(
            cur,
            int(order["buyer_user_id"]),
            str(order["order_id"]),
            reservation=reservation,
        )
    except ValueError as exc:
        raise ResaleError("B2B_BONUS_REFUND_CLAWBACK_FAILED", str(exc)) from exc
    bundle["state"] = "revoked"
    bundle["result"] = result
    cur.execute(
        """UPDATE dealer_resale_refunds
              SET audit_jsonb=jsonb_set(audit_jsonb,'{bonus_refund}',%s::jsonb,true)
            WHERE refund_id=%s""",
        (_canonical_plan(bundle), str(refund["refund_id"])),
    )
    if cur.rowcount != 1:
        raise ResaleError("B2B_BONUS_REFUND_AUDIT_FAILED", "赠送库存退款终态审计写入失败")
    return result


def _release_b2b_order_bonus(
    cur, *, order: Dict[str, Any], refund: Dict[str, Any],
) -> Dict[str, Any]:
    audit = _json_object(refund.get("audit_jsonb"))
    bundle = _json_object(audit.get("bonus_refund"))
    if not bundle or bundle.get("state") != "reserved":
        return {"released_points": 0, "skipped": True}
    reservation = _json_object(bundle.get("reservation"))
    if not reservation:
        raise ResaleError("B2B_BONUS_REFUND_SNAPSHOT_MISSING", "赠送库存退款冻结快照缺失")
    from services.bonus_grants import release_agent_order_bonus_refund

    try:
        result = release_agent_order_bonus_refund(
            cur,
            int(order["buyer_user_id"]),
            str(order["order_id"]),
            reservation,
        )
    except ValueError as exc:
        raise ResaleError("B2B_BONUS_REFUND_RELEASE_FAILED", str(exc)) from exc
    bundle["state"] = "released"
    bundle["result"] = result
    cur.execute(
        """UPDATE dealer_resale_refunds
              SET audit_jsonb=jsonb_set(audit_jsonb,'{bonus_refund}',%s::jsonb,true)
            WHERE refund_id=%s""",
        (_canonical_plan(bundle), str(refund["refund_id"])),
    )
    if cur.rowcount != 1:
        raise ResaleError("B2B_BONUS_REFUND_AUDIT_FAILED", "赠送库存退款释放审计写入失败")
    return result


def request_refund(
    cur, *, order_id: str, requested_by_user_id: int, reason: str = "",
    is_admin: bool = False, reason_category: str = "voluntary_unused_inventory",
    processing_cost_cents: int = 0,
    processing_cost_evidence: Optional[Dict[str, Any]] = None,
    mandatory_evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    _order_xact_lock(cur, str(order_id))
    cur.execute("SELECT * FROM dealer_resale_orders WHERE order_id=%s FOR UPDATE", (str(order_id),))
    row = cur.fetchone()
    if not row:
        raise ResaleError("RESALE_ORDER_NOT_FOUND", "逐级转售订单不存在")
    order = _row_dict(row)
    if not is_admin and int(order["buyer_user_id"]) != int(requested_by_user_id):
        raise ResaleError("FORBIDDEN", "只能申请自己作为买方的本跳退款")
    cur.execute("SELECT * FROM dealer_resale_refunds WHERE order_id=%s FOR UPDATE", (str(order_id),))
    existing = cur.fetchone()
    if existing:
        return _row_dict(existing)
    if order["state"] != "paid":
        raise ResaleError("REFUND_STATE_INVALID", f"当前订单状态不允许退款: {order['state']}")
    category = str(reason_category or "voluntary_unused_inventory").strip().lower()
    mandatory_b2b = is_admin and category in {
        "duplicate_charge", "not_credited", "system_failure", "seller_breach",
    }
    cost_cents = int(processing_cost_cents or 0)
    cost_evidence = dict(processing_cost_evidence or {})
    mandatory_proof = dict(mandatory_evidence or {})
    if mandatory_b2b and not mandatory_proof:
        raise ResaleError("B2B_MANDATORY_EVIDENCE_REQUIRED", "管理员强制 B2B 退款必须提供可审计证据")
    if mandatory_b2b and cost_cents:
        raise ResaleError("B2B_REFUND_COST_FORBIDDEN", "重复扣款、未到账、系统故障或卖方违约不得扣处理成本")
    if cost_cents > int(order["sale_amount_cents"]) * 5 // 100:
        raise ResaleError("B2B_REFUND_COST_CAP_EXCEEDED", "B2B 自愿退货实际处理成本不得超过实付金额 5%")
    if cost_cents and not cost_evidence:
        raise ResaleError("B2B_REFUND_COST_EVIDENCE_REQUIRED", "扣除处理成本必须提供可审计的实际成本证据")
    cur.execute("SELECT clock_timestamp() AS now")
    now = _row_dict(cur.fetchone()).get("now")
    deadline = order.get("refund_deadline")
    if not mandatory_b2b and (not deadline or (isinstance(now, datetime) and now > deadline)):
        raise ResaleError("REFUND_WINDOW_EXPIRED", "已超过支付后 72 小时 B2B 整批退款窗口")
    cur.execute(
        """SELECT * FROM dealer_inventory_lots
           WHERE source_order_id=%s AND owner_agent_user_id=%s FOR UPDATE""",
        (str(order_id), int(order["buyer_user_id"])),
    )
    lot = _row_dict(cur.fetchone())
    if not lot:
        raise ResaleError("REFUND_LOT_NOT_FOUND", "本单买方成本批次不存在")
    if not (
        lot["status"] == "active"
        and int(lot["remaining_points"]) == int(lot["original_points"])
        and int(lot["reserved_points"]) == 0
        and int(lot["remaining_cost_cents"]) == int(lot["acquisition_cost_cents"])
        and int(lot["reserved_cost_cents"]) == 0
    ):
        raise ResaleError(
            "REFUND_INVENTORY_ALREADY_USED",
            "本批库存已消费、冻结或继续向下转售，不能自动整批退款",
        )
    cur.execute(
        """SELECT base_points,bonus_points,pricing_snapshot_jsonb
             FROM recharge_orders WHERE id=%s FOR UPDATE""",
        (str(order_id),),
    )
    recharge = _row_dict(cur.fetchone())
    if not recharge:
        raise ResaleError("RECHARGE_ORDER_NOT_FOUND", "逐级转售支付订单不存在")
    bonus_refund = _reserve_b2b_order_bonus(cur, order=order, recharge=recharge)
    points = int(order["points"])
    buyer_id = int(order["buyer_user_id"])
    _inventory_wallet(cur, buyer_id)
    cur.execute(
        """UPDATE agent_inventory_wallets
           SET paid_inventory_points=paid_inventory_points-%s,
               frozen_inventory_points=frozen_inventory_points+%s,
               updated_at=NOW()
           WHERE agent_user_id=%s AND paid_inventory_points >= %s""",
        (points, points, buyer_id, points),
    )
    if cur.rowcount != 1:
        raise ResaleError("REFUND_INVENTORY_ALREADY_USED", "买方可退款库存余额不足")
    cur.execute(
        "UPDATE dealer_inventory_lots SET status='refund_pending', updated_at=NOW() WHERE lot_id=%s",
        (str(lot["lot_id"]),),
    )
    refund_id = _stable_id("DRF", str(order_id))
    cur.execute(
        """INSERT INTO dealer_resale_refunds
           (refund_id, order_id, requested_by_user_id, responsible_seller_user_id,
            status, reason, deadline_at,reason_category,processing_cost_cents,
            refund_amount_cents,
             processing_cost_evidence_jsonb,mandatory_evidence_jsonb,audit_jsonb)
            VALUES (%s,%s,%s,%s,'requested',%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)
           RETURNING *""",
        (
            refund_id, str(order_id), int(requested_by_user_id),
            int(order["refund_responsible_user_id"]), str(reason or ""), deadline,
            category, cost_cents, int(order["sale_amount_cents"]) - cost_cents,
            json.dumps(cost_evidence, ensure_ascii=False, separators=(",", ":")),
            json.dumps(mandatory_proof, ensure_ascii=False, separators=(",", ":")),
            _canonical_plan({"bonus_refund": bonus_refund}),
        ),
    )
    refund = _row_dict(cur.fetchone())
    cur.execute(
        "UPDATE dealer_resale_orders SET state='refund_pending', updated_at=NOW() WHERE order_id=%s",
        (str(order_id),),
    )
    cur.execute(
        """UPDATE recharge_orders
           SET refund_status='pending', refund_requested_at=COALESCE(refund_requested_at,NOW())
           WHERE id=%s""",
        (str(order_id),),
    )
    return refund


def create_manual_refund_case(
    cur, *, order_id: str, requested_by_user_id: int, reason: str,
    evidence: Dict[str, Any],
) -> Dict[str, Any]:
    """Admin exception intake only; it never moves inventory or marks cash refunded."""
    if not str(reason).strip() or not isinstance(evidence, dict) or not evidence:
        raise ResaleError(
            "MANUAL_REFUND_EVIDENCE_REQUIRED",
            "人工异常入口必须提供原因和可审计证据",
        )
    cur.execute("SELECT * FROM dealer_resale_orders WHERE order_id=%s FOR UPDATE", (str(order_id),))
    order = _row_dict(cur.fetchone())
    if not order:
        raise ResaleError("RESALE_ORDER_NOT_FOUND", "逐级转售订单不存在")
    if order["state"] not in {"paid", "refund_pending"}:
        raise ResaleError("REFUND_STATE_INVALID", "该订单不能进入人工退款审核")
    cur.execute("SELECT * FROM dealer_resale_refunds WHERE order_id=%s FOR UPDATE", (str(order_id),))
    existing = cur.fetchone()
    if existing:
        refund = _row_dict(existing)
        if refund["status"] != "manual_review":
            raise ResaleError("REFUND_ALREADY_EXISTS", "该订单已进入其他退款状态机")
        return refund
    refund_id = _stable_id("DRF", str(order_id))
    deadline = order.get("refund_deadline")
    if deadline is None:
        cur.execute("SELECT clock_timestamp() AS now")
        deadline = _row_dict(cur.fetchone())["now"]
    cur.execute(
        """INSERT INTO dealer_resale_refunds
           (refund_id, order_id, requested_by_user_id, responsible_seller_user_id,
            status, reason, deadline_at, audit_jsonb)
           VALUES (%s,%s,%s,%s,'manual_review',%s,%s,%s::jsonb)
           RETURNING *""",
        (
            refund_id, str(order_id), int(requested_by_user_id),
            int(order["refund_responsible_user_id"]), str(reason), deadline,
            json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
        ),
    )
    return _row_dict(cur.fetchone())


def record_b2b_external_refund_material(
    cur, *, order_id: str, amount_cents: int, provider: str,
    external_refund_id: str, external_completed_at: str,
    evidence: Dict[str, Any], recorded_by_user_id: int,
) -> Dict[str, Any]:
    """Store unverified operator material without creating a cash terminal.

    Only a signed payment callback or a persisted successful provider query may
    set ``recharge_orders.refund_status='completed'``. Administrator JSON is an
    intake artifact and cannot move inventory or reverse profit.
    """
    _order_xact_lock(cur, str(order_id))
    amount = int(amount_cents)
    provider_name = _required_refund_provider(provider)
    external_id = str(external_refund_id or "").strip()
    external_at = str(external_completed_at or "").strip()
    if amount <= 0 or not provider_name or not external_id or not external_at:
        raise ResaleError(
            "EXTERNAL_REFUND_MATERIAL_REQUIRED",
            "外部退款材料必须提供正金额、支付通道、外部退款号和声称完成时间",
        )
    if not isinstance(evidence, dict) or not evidence:
        raise ResaleError("EXTERNAL_REFUND_MATERIAL_REQUIRED", "外部退款材料必须包含非空审计附件")
    cur.execute(
        """SELECT o.*,r.payment_status,r.refund_status AS recharge_refund_status,
                  r.refunded_amount_cents,dr.refund_id,dr.status AS dealer_refund_status,
                   dr.external_channel,dr.external_refund_id,dr.processing_cost_cents,
                   dr.refund_amount_cents,dr.audit_jsonb
           FROM dealer_resale_orders o
           JOIN recharge_orders r ON r.id=o.order_id
           JOIN dealer_resale_refunds dr ON dr.order_id=o.order_id
           WHERE o.order_id=%s
           FOR UPDATE OF o,r,dr""",
        (str(order_id),),
    )
    state = _row_dict(cur.fetchone())
    if not state:
        raise ResaleError("RESALE_REFUND_NOT_FOUND", "必须先冻结本跳完整批次，再登记待核验材料")
    processing_cost = int(state.get("processing_cost_cents") or 0)
    expected_cash_refund = int(state.get("refund_amount_cents") or 0)
    if expected_cash_refund <= 0 or expected_cash_refund != (
        int(state["sale_amount_cents"]) - processing_cost
    ):
        raise ResaleError("REFUND_AMOUNT_SNAPSHOT_INVALID", "B2B 持久化退款金额与批准快照不一致")
    if amount != expected_cash_refund:
        raise ResaleError("REFUND_AMOUNT_MISMATCH", "现金退款金额必须等于本跳实付减去已证明的实际处理成本")
    existing_material = _json_object(state.get("audit_jsonb")).get(
        "unverified_external_refund_material", {}
    )
    existing_provider = str(existing_material.get("claimed_provider") or "")
    existing_external_id = str(existing_material.get("claimed_external_refund_id") or "")
    if existing_provider or existing_external_id:
        if existing_provider != provider_name or existing_external_id != external_id:
            raise ResaleError(
                "REFUND_IDEMPOTENCY_CONFLICT",
                "该退款已登记另一份待核验材料，禁止覆盖",
            )
        return {
            "order_id": str(order_id), "refund_id": str(state["refund_id"]),
            "status": "pending_verification", "idempotent": True,
        }
    if state.get("payment_status") != "paid":
        raise ResaleError("REFUND_PAYMENT_NOT_PAID", "原订单未支付，不能登记外部退款材料")
    if state.get("state") != "refund_pending" or state.get("dealer_refund_status") not in {
        "requested", "processing",
    }:
        raise ResaleError("REFUND_STATE_INVALID", "本跳库存尚未处于完整退款冻结态")
    if str(state.get("recharge_refund_status") or "") == "completed":
        raise ResaleError("REFUND_CASH_STATE_INVALID", "支付渠道已进入终态，无需登记操作员材料")
    audit = {
        "unverified_external_refund_material": {
            "claimed_provider": provider_name,
            "claimed_external_refund_id": external_id,
            "claimed_completed_at": external_at,
            "claimed_amount_cents": amount,
            "recorded_by_user_id": int(recorded_by_user_id),
            "verification_status": "pending",
            "evidence": evidence,
        }
    }
    cur.execute(
        """UPDATE dealer_resale_refunds
           SET audit_jsonb=audit_jsonb || %s::jsonb,status='processing'
           WHERE refund_id=%s AND status IN ('requested','processing')""",
        (
            json.dumps(audit, ensure_ascii=False, separators=(",", ":")),
            str(state["refund_id"]),
        ),
    )
    if cur.rowcount != 1:
        raise ResaleError("REFUND_STATE_INVALID", "本跳退款状态并发变化")
    return {
        "order_id": str(order_id), "refund_id": str(state["refund_id"]),
        "status": "pending_verification", "idempotent": False,
    }


def _reject_refund(cur, order: Dict[str, Any], refund: Dict[str, Any], reason: str) -> None:
    buyer_id = int(order["buyer_user_id"])
    points = int(order["points"])
    cur.execute(
        "SELECT * FROM dealer_inventory_lots WHERE source_order_id=%s AND owner_agent_user_id=%s FOR UPDATE",
        (str(order["order_id"]), buyer_id),
    )
    lot = _row_dict(cur.fetchone())
    if lot and lot.get("status") == "refund_pending":
        cur.execute(
            "UPDATE dealer_inventory_lots SET status='active', updated_at=NOW() WHERE lot_id=%s",
            (str(lot["lot_id"]),),
        )
        cur.execute(
            """UPDATE agent_inventory_wallets
               SET paid_inventory_points=paid_inventory_points+%s,
                   frozen_inventory_points=frozen_inventory_points-%s, updated_at=NOW()
               WHERE agent_user_id=%s AND frozen_inventory_points >= %s""",
            (points, points, buyer_id, points),
        )
        if cur.rowcount != 1:
            raise ResaleError("REFUND_RELEASE_FAILED", "退款失败后的买方库存解冻失败")
    _release_b2b_order_bonus(cur, order=order, refund=refund)
    cur.execute(
        "UPDATE dealer_resale_orders SET state='paid', updated_at=NOW() WHERE order_id=%s",
        (str(order["order_id"]),),
    )
    cur.execute(
        """UPDATE dealer_resale_refunds
           SET status='rejected', rejected_at=NOW(), rejection_reason=%s
           WHERE refund_id=%s AND status IN ('requested','processing')""",
        (str(reason), str(refund["refund_id"])),
    )


def sync_consumer_refund_from_recharge(cur, order_id: str) -> bool:
    """Restore only the direct service-provider lot after consumer credit is revoked.

    Consumer policy/evidence is owned by the existing service-review refund flow.
    This sink never applies the B2B 72-hour rule and never walks to an ancestor.
    If customer credit was partly consumed, lot conservation cannot be proved, so
    the case is left in manual_review without manufacturing inventory.
    """
    cur.execute("SELECT to_regclass('public.dealer_consumer_sales') AS table_name")
    if not _row_dict(cur.fetchone()).get("table_name"):
        return False
    _order_xact_lock(cur, str(order_id))
    cur.execute("SELECT * FROM dealer_consumer_sales WHERE order_id=%s FOR UPDATE", (str(order_id),))
    row = cur.fetchone()
    if not row:
        return False
    sale = _row_dict(row)
    cur.execute(
        """SELECT refund_status,refund_completed_at,refunded_amount_cents,
                  actual_payment_channel,payment_method,settlement_snapshot_jsonb
           FROM recharge_orders WHERE id=%s FOR UPDATE""",
        (str(order_id),),
    )
    recharge = _row_dict(cur.fetchone())
    refund_status = str(recharge.get("refund_status") or "").lower()
    # ``processed`` is only the platform-internal credit/profit reversal. Cash
    # may still be outstanding, so restoring seller inventory at that point
    # would create a split terminal state. Wait for external/manual evidence.
    if refund_status not in {"completed", "pending_review"}:
        return True
    if refund_status == "pending_review":
        # A workflow label is not cash evidence.  Keep this invariant inside
        # the resale service as well as the caller so direct/recovery callers
        # cannot restore inventory from a hand-edited status.
        from db.refund_work_order_db import assert_external_refund_evidence_cur

        assert_external_refund_evidence_cur(
            cur,
            str(order_id),
            refund_status,
            recharge.get("refund_completed_at"),
        )
    # New direct-service refund cases reverse credit/inventory/revenue before the
    # asynchronous provider call and persist a cash job.  A provider callback
    # only closes the cash leg; it must never restore inventory a second time.
    cur.execute(
        """SELECT case_id,internal_settled_at,cash_status,refund_amount_cents
           FROM consumer_refund_cases WHERE source_order_id=%s FOR UPDATE""",
        (str(order_id),),
    )
    settled_case = _row_dict(cur.fetchone())
    expected_refund_amount = (
        int(settled_case.get("refund_amount_cents") or 0)
        if settled_case.get("internal_settled_at")
        else int(sale["sale_amount_cents"])
    )
    canonical_evidence = _trusted_recharge_refund_proof(
        recharge, expected_amount_cents=expected_refund_amount
    )
    provider_name = str(canonical_evidence["provider"])
    provider_ref = str(canonical_evidence["provider_refund_id"])
    _claim_external_refund_proof(
        cur,
        provider=provider_name,
        external_refund_id=provider_ref,
        refund_scope="consumer",
        local_ref=str(settled_case.get("case_id") or _stable_id("CRF", str(order_id))),
        amount_cents=expected_refund_amount,
        evidence=canonical_evidence,
    )
    # An idempotent terminal must own its proof too.  This repairs a historic
    # terminal row on first touch and prevents a later cross-scope claim.
    if sale["state"] == "refunded":
        return True
    if settled_case.get("internal_settled_at"):
        if settled_case.get("cash_status") != "completed":
            cur.execute(
                """UPDATE service_refund_cash_jobs
                   SET status='completed',completed_at=COALESCE(completed_at,NOW()),
                       provider_refund_id=%s,
                       provider_evidence_jsonb=%s::jsonb,
                       attempt_count=GREATEST(attempt_count,1),last_error=NULL,
                       updated_at=NOW()
                   WHERE case_id=%s AND status<>'completed'""",
                (
                    provider_ref,
                    json.dumps(canonical_evidence, ensure_ascii=False, separators=(",", ":")),
                    str(settled_case["case_id"]),
                ),
            )
            if cur.rowcount != 1:
                raise ResaleError("CONSUMER_REFUND_CASH_STATE_INVALID", "现金退款工单状态已变化")
            cur.execute(
                """UPDATE consumer_refund_cases
                   SET status='completed',cash_status='completed',
                       execution_jsonb=execution_jsonb || %s::jsonb,
                       row_version=row_version+1,updated_at=NOW()
                   WHERE case_id=%s""",
                (
                    json.dumps(
                        {"cash_completed": True, "provider_evidence": canonical_evidence},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    str(settled_case["case_id"]),
                ),
            )
            cur.execute(
                """UPDATE dealer_consumer_sales
                   SET state='refunded',refunded_at=NOW(),updated_at=NOW()
                   WHERE order_id=%s""",
                (str(order_id),),
            )
        return True

    cur.execute(
        """SELECT
             COALESCE(SUM(CASE WHEN type='allocate' THEN points ELSE 0 END),0) AS allocated,
             COALESCE(SUM(CASE WHEN type='revoke' THEN -points ELSE 0 END),0) AS revoked
           FROM customer_credit_transactions
           WHERE customer_user_id=%s AND related_order_id=%s""",
        (int(sale["consumer_user_id"]), str(order_id)),
    )
    credit = _row_dict(cur.fetchone())
    allocated = int(credit.get("allocated") or 0)
    revoked = int(credit.get("revoked") or 0)
    case_id = _stable_id("CRF", str(order_id))
    if allocated != int(sale["points"]) or revoked != int(sale["points"]):
        cur.execute(
            """INSERT INTO consumer_refund_cases
               (case_id,consumer_user_id,responsible_service_user_id,source_order_id,
                policy_code,digital_goods_acknowledged_at,status,reason,execution_jsonb)
               VALUES (%s,%s,%s,%s,%s,%s,'manual_review',%s,%s::jsonb)
               ON CONFLICT(source_order_id) DO UPDATE SET
                 status='manual_review',reason=EXCLUDED.reason,
                 execution_jsonb=EXCLUDED.execution_jsonb,updated_at=NOW()""",
            (
                case_id, int(sale["consumer_user_id"]), int(sale["seller_user_id"]),
                str(order_id), str(sale["digital_goods_policy_code"]),
                sale["digital_goods_acknowledged_at"],
                "客户额度并非整单未消费；禁止自动恢复服务方库存",
                json.dumps(
                    {"allocated_points": allocated, "revoked_points": revoked, "required_points": int(sale["points"])},
                    ensure_ascii=False, separators=(",", ":"),
                ),
            ),
        )
        cur.execute(
            "UPDATE dealer_consumer_sales SET state='manual_review',updated_at=NOW() WHERE order_id=%s",
            (str(order_id),),
        )
        return True

    seller_id = int(sale["seller_user_id"])
    points = int(sale["points"])
    seller_wallet = _inventory_wallet(cur, seller_id)
    cur.execute(
        """SELECT * FROM dealer_consumer_lot_allocations
           WHERE order_id=%s ORDER BY allocation_seq FOR UPDATE""",
        (str(order_id),),
    )
    allocations = [_row_dict(item) for item in cur.fetchall()]
    for allocation in allocations:
        if allocation["status"] == "restored":
            continue
        if allocation["status"] != "consumed":
            raise ResaleError("CONSUMER_REFUND_ALLOCATION_INVALID", "消费者退款原批次状态异常")
        cur.execute(
            """UPDATE dealer_inventory_lots
               SET remaining_points=remaining_points+%s,
                   remaining_cost_cents=remaining_cost_cents+%s,
                   status='active',updated_at=NOW()
               WHERE lot_id=%s""",
            (
                int(allocation["points"]), int(allocation["cost_basis_cents"]),
                str(allocation["seller_lot_id"]),
            ),
        )
    cur.execute(
        """UPDATE dealer_consumer_lot_allocations
           SET status='restored',restored_at=NOW()
           WHERE order_id=%s AND status='consumed'""",
        (str(order_id),),
    )
    cur.execute(
        """UPDATE agent_inventory_wallets
           SET paid_inventory_points=paid_inventory_points+%s,
               total_allocated_points=GREATEST(0,total_allocated_points-%s),updated_at=NOW()
           WHERE agent_user_id=%s
           RETURNING paid_inventory_points,bonus_inventory_points""",
        (points, points, seller_id),
    )
    seller_after = _row_dict(cur.fetchone())
    transfer_id = str(sale["transfer_id"])
    cur.execute(
        """INSERT INTO dealer_consumer_transfer_entries
           (transfer_id,order_id,seller_user_id,consumer_user_id,direction,
            points_delta,cost_basis_cents)
           VALUES (%s,%s,%s,%s,'consumer_refund_out',%s,%s),
                  (%s,%s,%s,%s,'seller_refund_in',%s,%s)
           ON CONFLICT(transfer_id,direction) DO NOTHING""",
        (
            transfer_id, str(order_id), seller_id, int(sale["consumer_user_id"]), -points,
            int(sale["sale_amount_cents"]), transfer_id, str(order_id), seller_id,
            int(sale["consumer_user_id"]), points, int(sale["seller_cost_basis_cents"]),
        ),
    )
    _insert_legacy_inventory_tx(
        cur, user_id=seller_id, type_="consumer_refund_in", points=points,
        paid_after=int(seller_after["paid_inventory_points"]),
        bonus_after=int(seller_after["bonus_inventory_points"]), order_id=str(order_id),
        transfer_id=transfer_id, lot_id=None, counterparty_id=int(sale["consumer_user_id"]),
        cost_basis_cents=int(sale["seller_cost_basis_cents"]), description="消费者退款恢复原履约库存",
    )
    cur.execute(
        "UPDATE dealer_consumer_sales SET state='refunded',refunded_at=NOW(),updated_at=NOW() WHERE order_id=%s",
        (str(order_id),),
    )
    cur.execute(
        """INSERT INTO consumer_refund_cases
           (case_id,consumer_user_id,responsible_service_user_id,source_order_id,
            policy_code,digital_goods_acknowledged_at,status,reason,execution_jsonb)
           VALUES (%s,%s,%s,%s,%s,%s,'completed',%s,%s::jsonb)
           ON CONFLICT(source_order_id) DO UPDATE SET
             status='completed',reason=EXCLUDED.reason,
             execution_jsonb=EXCLUDED.execution_jsonb,updated_at=NOW()""",
        (
            case_id, int(sale["consumer_user_id"]), seller_id, str(order_id),
            str(sale["digital_goods_policy_code"]), sale["digital_goods_acknowledged_at"],
            "消费者退款已完成；未触及其他独立交易",
            json.dumps({"restored_points": points, "ancestor_orders_touched": 0}, ensure_ascii=False),
        ),
    )
    return True


def _trusted_b2b_refund_terminal(
    recharge: Dict[str, Any], order: Dict[str, Any], refund: Dict[str, Any],
) -> Dict[str, Any]:
    """Validate the persisted provider terminal before reversing a B2B hop."""

    snapshot = _json_object(recharge.get("settlement_snapshot_jsonb"))
    provider = str(snapshot.get("refund_provider") or "").strip().lower()
    provider_refund_id = str(snapshot.get("provider_refund_id") or "").strip()
    evidence_kind = str(snapshot.get("refund_evidence_kind") or "").strip()
    trusted_kinds = {
        "signed_wechat_callback",
        "wechat_refund_query",
        "signed_xunhupay_callback",
        "signed_xunhupay_refund_response",
    }
    if (
        not recharge.get("refund_completed_at")
        or not provider
        or not provider_refund_id
        or evidence_kind not in trusted_kinds
        or (
            evidence_kind == "signed_xunhupay_callback"
            and snapshot.get("provider_refund_reference_kind")
            != "signed_xunhupay_refund_response"
        )
    ):
        raise ResaleError(
            "B2B_REFUND_PROVIDER_PROOF_MISSING",
            "原支付渠道尚未留下可信退款终态、外部退款号和验签证据",
        )
    expected_amount = int(refund.get("refund_amount_cents") or 0)
    if expected_amount <= 0 or expected_amount != (
        int(order["sale_amount_cents"]) - int(refund.get("processing_cost_cents") or 0)
    ):
        raise ResaleError("B2B_REFUND_AMOUNT_SNAPSHOT_INVALID", "B2B 持久化退款金额与订单批准快照不一致")
    if int(recharge.get("refunded_amount_cents") or 0) != expected_amount:
        raise ResaleError("B2B_REFUND_AMOUNT_MISMATCH", "支付渠道退款金额与本跳批准金额不一致")
    route = _original_payment_route(recharge)
    if route.startswith("wechat") and provider not in {"wechat", "wechat_pay"}:
        raise ResaleError("B2B_REFUND_PROVIDER_MISMATCH", "退款终态与原微信支付路径不一致")
    if route == "xunhupay" and provider != "xunhupay":
        raise ResaleError("B2B_REFUND_PROVIDER_MISMATCH", "退款终态与原虎皮椒支付路径不一致")
    if not route.startswith("wechat") and route != "xunhupay":
        raise ResaleError("B2B_REFUND_PROVIDER_MISMATCH", "原支付路径没有可自动核验的退款终态")
    return {
        "provider": provider,
        "provider_refund_id": provider_refund_id,
        "refund_evidence_kind": evidence_kind,
        "provider_refund_reference_kind": str(
            snapshot.get("provider_refund_reference_kind") or ""
        ),
        "payment_transaction_id": str(snapshot.get("payment_transaction_id") or ""),
        "refunded_amount_cents": expected_amount,
        "refund_completed_at": recharge["refund_completed_at"].isoformat(),
        "source": "recharge_orders.settlement_snapshot_jsonb",
    }


def _sync_v2_b2b_refund_terminal(
    cur, *, order_id: str, order: Dict[str, Any], refund: Dict[str, Any],
    recharge: Dict[str, Any],
) -> bool:
    cur.execute(
        """SELECT p.plan_id,p.root_catalog_version,h.*
           FROM dealer_resale_fulfillment_plans p
           JOIN dealer_resale_fulfillment_hops h ON h.plan_id=p.plan_id
           WHERE p.root_order_id=%s ORDER BY h.hop_seq DESC LIMIT 1 FOR UPDATE OF p,h""",
        (str(order_id),),
    )
    final = _row_dict(cur.fetchone())
    if not final:
        return False
    buyer_id = int(order["buyer_user_id"])
    seller_id = int(order["seller_user_id"])
    points = int(order["points"])
    if (
        buyer_id != int(final["buyer_user_id"])
        or seller_id != int(final["seller_user_id"])
        or points != int(final["points"])
    ):
        raise ResaleError("REFUND_HOP_SNAPSHOT_MISMATCH", "退款订单与最终 JIT hop 不一致")
    _clawback_b2b_order_bonus(cur, order=order, recharge=recharge, refund=refund)
    cur.execute(
        """SELECT * FROM dealer_inventory_lots
           WHERE lot_id=%s AND owner_agent_user_id=%s FOR UPDATE""",
        (str(final["acquired_lot_id"]), buyer_id),
    )
    buyer_lot = _row_dict(cur.fetchone())
    if not buyer_lot or buyer_lot.get("status") != "refund_pending":
        raise ResaleError("REFUND_LOT_STATE_INVALID", "买方退款冻结批次状态异常")
    buyer_wallet = _inventory_wallet(cur, buyer_id)
    if int(buyer_wallet["frozen_inventory_points"]) < points:
        raise ResaleError("REFUND_FROZEN_INVENTORY_MISSING", "买方退款冻结库存不足")
    cur.execute(
        """UPDATE agent_inventory_wallets
           SET frozen_inventory_points=frozen_inventory_points-%s,
               total_purchased_points=GREATEST(0,total_purchased_points-%s),updated_at=NOW()
           WHERE agent_user_id=%s AND frozen_inventory_points >= %s
           RETURNING paid_inventory_points,bonus_inventory_points""",
        (points, points, buyer_id, points),
    )
    buyer_after = _row_dict(cur.fetchone())
    cur.execute(
        """UPDATE dealer_inventory_lots
           SET remaining_points=0,reserved_points=0,remaining_cost_cents=0,
               reserved_cost_cents=0,status='reversed',updated_at=NOW() WHERE lot_id=%s""",
        (str(buyer_lot["lot_id"]),),
    )
    seller_wallet = _inventory_wallet(cur, seller_id)
    seller_paid_after = int(seller_wallet["paid_inventory_points"]) + points
    cur.execute(
        """UPDATE agent_inventory_wallets
           SET paid_inventory_points=%s,total_allocated_points=GREATEST(0,total_allocated_points-%s),
               updated_at=NOW() WHERE agent_user_id=%s""",
        (seller_paid_after, points, seller_id),
    )
    cur.execute(
        """SELECT * FROM dealer_resale_fulfillment_allocations
           WHERE plan_id=%s AND hop_seq=%s ORDER BY allocation_seq FOR UPDATE""",
        (str(final["plan_id"]), int(final["hop_seq"])),
    )
    final_allocations = [_row_dict(row) for row in cur.fetchall()]
    restored_lot_id: Optional[str] = None
    jit_points = 0
    jit_cost = 0
    for allocation in final_allocations:
        if allocation["status"] == "restored":
            continue
        if allocation["status"] != "consumed":
            raise ResaleError("REFUND_ALLOCATION_STATE_INVALID", "最终 JIT hop 分配状态异常")
        if allocation["allocation_kind"] == "existing_lot":
            cur.execute(
                """UPDATE dealer_inventory_lots
                   SET remaining_points=remaining_points+%s,
                       remaining_cost_cents=remaining_cost_cents+%s,status='active',updated_at=NOW()
                   WHERE lot_id=%s AND owner_agent_user_id=%s""",
                (
                    int(allocation["points"]), int(allocation["cost_basis_cents"]),
                    str(allocation["source_lot_id"]), seller_id,
                ),
            )
            if cur.rowcount != 1:
                raise ResaleError("REFUND_LOT_STATE_INVALID", "卖方原 FIFO 批次无法恢复")
            restored_lot_id = restored_lot_id or str(allocation["source_lot_id"])
        else:
            jit_points += int(allocation["points"])
            jit_cost += int(allocation["cost_basis_cents"])
    if jit_points:
        jit_lot_id = _stable_id("JRL", str(order_id))
        cur.execute(
            """INSERT INTO dealer_inventory_lots
               (lot_id,owner_agent_user_id,original_points,remaining_points,reserved_points,
                acquisition_cost_cents,remaining_cost_cents,reserved_cost_cents,acquired_at,
                status,source_kind,pricing_version,quote_id,evidence_jsonb,standard_reference_cents)
               VALUES (%s,%s,%s,%s,0,%s,%s,0,NOW(),'active',%s,%s,%s,%s::jsonb,%s)""",
            (
                jit_lot_id, seller_id, jit_points, jit_points, jit_cost, jit_cost,
                str(order["source_kind"]), str(order["pricing_version"]), str(order["quote_id"]),
                _canonical_plan({
                    "refund_of_order_id": str(order_id), "scope": "current_hop_only",
                    "original_buyer_lot_id": str(buyer_lot["lot_id"]),
                    "jit_incoming_returned": True, "ancestor_hops_touched": 0,
                }), int(final["standard_reference_cents"]),
            ),
        )
        restored_lot_id = restored_lot_id or jit_lot_id
    if not restored_lot_id:
        raise ResaleError("REFUND_ALLOCATION_STATE_INVALID", "最终 JIT hop 没有可恢复库存")
    cur.execute(
        """UPDATE dealer_resale_fulfillment_allocations
           SET status='restored',restored_at=NOW()
           WHERE plan_id=%s AND hop_seq=%s AND status='consumed'""",
        (str(final["plan_id"]), int(final["hop_seq"])),
    )
    transfer_id = str(order["transfer_id"])
    cur.execute(
        """UPDATE dealer_inventory_transfers SET state='refunded',refunded_at=NOW()
           WHERE transfer_id=%s AND state='settled'""", (transfer_id,),
    )
    cur.execute(
        """INSERT INTO dealer_inventory_transfer_entries
           (transfer_id,owner_agent_user_id,counterparty_user_id,direction,
            points_delta,lot_id,cost_basis_cents,balance_paid_after)
           VALUES (%s,%s,%s,'refund_out',%s,%s,%s,%s),
                  (%s,%s,%s,'refund_in',%s,%s,%s,%s)
           ON CONFLICT (transfer_id,owner_agent_user_id,direction) DO NOTHING""",
        (
            transfer_id, buyer_id, seller_id, -points, str(buyer_lot["lot_id"]),
            int(order["sale_amount_cents"]), int(buyer_after["paid_inventory_points"]),
            transfer_id, seller_id, buyer_id, points, restored_lot_id,
            int(order["seller_cost_basis_cents"]), seller_paid_after,
        ),
    )
    _insert_legacy_inventory_tx(
        cur, user_id=buyer_id, type_="resale_refund_out", points=-points,
        paid_after=int(buyer_after["paid_inventory_points"]),
        bonus_after=int(buyer_after["bonus_inventory_points"]), order_id=str(order_id),
        transfer_id=transfer_id, lot_id=str(buyer_lot["lot_id"]), counterparty_id=seller_id,
        cost_basis_cents=int(order["sale_amount_cents"]), description="JIT 本跳整批退款转出",
    )
    _insert_legacy_inventory_tx(
        cur, user_id=seller_id, type_="resale_refund_in", points=points,
        paid_after=seller_paid_after, bonus_after=int(seller_wallet["bonus_inventory_points"]),
        order_id=str(order_id), transfer_id=transfer_id, lot_id=restored_lot_id,
        counterparty_id=buyer_id, cost_basis_cents=int(order["seller_cost_basis_cents"]),
        description="JIT 本跳退款形成可追溯恢复批次",
    )
    cur.execute(
        """SELECT * FROM dealer_resale_hop_profit_ledger
           WHERE plan_id=%s AND hop_seq=%s FOR UPDATE""",
        (str(final["plan_id"]), int(final["hop_seq"])),
    )
    profit = _row_dict(cur.fetchone())
    if not profit:
        raise ResaleError("REFUND_PROFIT_MISSING", "最终 JIT hop 利润台账缺失")
    if profit.get("revenue_ledger_id") is not None:
        revenue_id = int(profit["revenue_ledger_id"])
        cur.execute("SELECT * FROM agent_revenue_ledger WHERE id=%s FOR UPDATE", (revenue_id,))
        revenue = _row_dict(cur.fetchone())
        if revenue and revenue.get("reversed_at") is None:
            if revenue.get("status") == "frozen":
                cur.execute(
                    """UPDATE agent_revenue_ledger
                       SET status='cancelled',reversed_at=NOW(),note=COALESCE(note,'') || %s
                       WHERE id=%s""", (" | JIT 本跳 B2B 退款冲销", revenue_id),
                )
            elif revenue.get("status") == "settled" and int(profit["agent_payable_cents"]) > 0:
                from services.agent_revenue import insert_revenue_clawback
                clawback_id = insert_revenue_clawback(
                    cur, agent_user_id=seller_id, original_ledger_id=revenue_id,
                    clawback_amount_cents=int(profit["agent_payable_cents"]),
                    note=f"JIT 本跳退款冲销 · order={order_id}",
                )
                cur.execute(
                    """UPDATE agent_revenue_ledger SET reversed_at=NOW(),reversed_by_ledger_id=%s
                       WHERE id=%s""", (clawback_id, revenue_id),
                )
    cur.execute(
        """UPDATE dealer_resale_hop_profit_ledger SET status='reversed',reversed_at=NOW()
           WHERE plan_id=%s AND hop_seq=%s AND status IN ('pending','available')""",
        (str(final["plan_id"]), int(final["hop_seq"])),
    )
    cur.execute(
        "UPDATE dealer_resale_orders SET state='refunded',refunded_at=NOW(),updated_at=NOW() WHERE order_id=%s",
        (str(order_id),),
    )
    cur.execute(
        """UPDATE dealer_resale_refunds SET status='completed',completed_at=NOW()
           WHERE refund_id=%s AND status IN ('requested','processing')""",
        (str(refund["refund_id"]),),
    )
    return True


def sync_refund_from_recharge(cur, order_id: str) -> bool:
    _order_xact_lock(cur, str(order_id))
    if sync_consumer_refund_from_recharge(cur, order_id):
        return True
    cur.execute("SELECT to_regclass('public.dealer_resale_refunds') AS table_name")
    if not _row_dict(cur.fetchone()).get("table_name"):
        return False
    cur.execute("SELECT * FROM dealer_resale_refunds WHERE order_id=%s FOR UPDATE", (str(order_id),))
    refund_row = cur.fetchone()
    if not refund_row:
        return False
    refund = _row_dict(refund_row)
    if refund["status"] == "rejected":
        return True
    cur.execute(
        """SELECT refund_status,refund_completed_at,refunded_amount_cents,
                  base_points,bonus_points,pricing_snapshot_jsonb,
                  actual_payment_channel,payment_method,settlement_snapshot_jsonb
           FROM recharge_orders WHERE id=%s FOR UPDATE""",
        (str(order_id),),
    )
    recharge = _row_dict(cur.fetchone())
    external_status = str(recharge.get("refund_status") or "")
    cur.execute("SELECT * FROM dealer_resale_orders WHERE order_id=%s FOR UPDATE", (str(order_id),))
    order = _row_dict(cur.fetchone())
    if external_status in {"failed", "rejected"}:
        _reject_refund(cur, order, refund, f"external_refund_{external_status}")
        return True
    if external_status not in {"completed", "pending_review"}:
        if external_status in {"pending", "approved", "processing"}:
            cur.execute(
                "UPDATE dealer_resale_refunds SET status='processing' "
                "WHERE refund_id=%s AND status='requested'",
                (str(refund["refund_id"]),),
            )
        return True
    expected_cash_refund = int(order["sale_amount_cents"]) - int(
        refund.get("processing_cost_cents") or 0
    )
    canonical_terminal: Optional[Dict[str, Any]] = None
    if refund.get("external_channel") and refund.get("external_refund_id"):
        audit = _json_object(refund.get("audit_jsonb"))
        persisted_proof = _json_object(audit.get("external_cash_refund")) or _json_object(
            audit.get("verified_cash_terminal")
        )
        provider_name = _required_refund_provider(refund["external_channel"])
        provider_ref = str(refund["external_refund_id"]).strip()
        persisted_kind = str(persisted_proof.get("refund_evidence_kind") or "").strip()
        trusted_persisted_kinds = {
            "signed_wechat_callback",
            "wechat_refund_query",
            "signed_xunhupay_callback",
            "signed_xunhupay_refund_response",
        }
        persisted_amount = int(
            persisted_proof.get("amount_cents")
            or persisted_proof.get("refunded_amount_cents")
            or 0
        )
        if (
            not persisted_proof
            or _canonical_refund_provider(persisted_proof.get("provider")) != provider_name
            or str(
                persisted_proof.get("external_refund_id")
                or persisted_proof.get("provider_refund_id")
                or ""
            ).strip() != provider_ref
            or persisted_amount != expected_cash_refund
            or persisted_kind not in trusted_persisted_kinds
            or (
                persisted_kind == "signed_xunhupay_callback"
                and persisted_proof.get("provider_refund_reference_kind")
                != "signed_xunhupay_refund_response"
            )
            or int(recharge.get("refunded_amount_cents") or 0) != expected_cash_refund
            or recharge.get("refund_completed_at") is None
        ):
            raise ResaleError("EXTERNAL_REFUND_EVIDENCE_REQUIRED", "B2B 外部退款终态证据不完整")
        canonical_evidence = persisted_proof
    else:
        canonical_terminal = _trusted_b2b_refund_terminal(recharge, order, refund)
        canonical_evidence = canonical_terminal
        provider_name = str(canonical_evidence["provider"])
        provider_ref = str(canonical_evidence["provider_refund_id"])
    _claim_external_refund_proof(
        cur,
        provider=provider_name,
        external_refund_id=provider_ref,
        refund_scope="dealer_b2b",
        local_ref=str(order_id),
        amount_cents=expected_cash_refund,
        evidence=canonical_evidence,
    )
    if order["state"] == "refunded":
        cur.execute(
            "UPDATE dealer_resale_refunds SET status='completed', completed_at=COALESCE(completed_at,NOW()) "
            "WHERE refund_id=%s",
            (str(refund["refund_id"]),),
        )
        return True
    if order["state"] != "refund_pending":
        raise ResaleError("REFUND_STATE_INVALID", "外部退款成功但逐级转售订单未处于退款冻结态")
    canonical_terminal = canonical_terminal or _trusted_b2b_refund_terminal(
        recharge, order, refund
    )
    cur.execute(
        """UPDATE dealer_resale_refunds
           SET external_channel=%s,external_refund_id=%s,
               audit_jsonb=audit_jsonb || %s::jsonb
           WHERE refund_id=%s""",
        (
            canonical_terminal["provider"], canonical_terminal["provider_refund_id"],
            json.dumps(
                {"verified_cash_terminal": canonical_terminal},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            str(refund["refund_id"]),
        ),
    )

    if _sync_v2_b2b_refund_terminal(
        cur, order_id=str(order_id), order=order, refund=refund, recharge=recharge,
    ):
        return True

    buyer_id = int(order["buyer_user_id"])
    seller_id = int(order["seller_user_id"])
    points = int(order["points"])
    _clawback_b2b_order_bonus(cur, order=order, recharge=recharge, refund=refund)
    cur.execute(
        "SELECT * FROM dealer_inventory_lots WHERE source_order_id=%s AND owner_agent_user_id=%s FOR UPDATE",
        (str(order_id), buyer_id),
    )
    buyer_lot = _row_dict(cur.fetchone())
    if not buyer_lot or buyer_lot.get("status") != "refund_pending":
        raise ResaleError("REFUND_LOT_STATE_INVALID", "买方退款冻结批次状态异常")
    buyer_wallet = _inventory_wallet(cur, buyer_id)
    if int(buyer_wallet["frozen_inventory_points"]) < points:
        raise ResaleError("REFUND_FROZEN_INVENTORY_MISSING", "买方退款冻结库存不足")
    cur.execute(
        """UPDATE agent_inventory_wallets
           SET frozen_inventory_points=frozen_inventory_points-%s,
               total_purchased_points=GREATEST(0,total_purchased_points-%s), updated_at=NOW()
           WHERE agent_user_id=%s AND frozen_inventory_points >= %s
           RETURNING paid_inventory_points, bonus_inventory_points""",
        (points, points, buyer_id, points),
    )
    buyer_after = _row_dict(cur.fetchone())
    cur.execute(
        """UPDATE dealer_inventory_lots
           SET remaining_points=0, reserved_points=0, remaining_cost_cents=0,
               reserved_cost_cents=0, status='reversed', updated_at=NOW()
           WHERE lot_id=%s""",
        (str(buyer_lot["lot_id"]),),
    )

    seller_paid_after = 0
    seller_bonus_after = 0
    if order["source_kind"] in {"direct_resale", "platform_purchase"}:
        seller_wallet = _inventory_wallet(cur, seller_id)
        cur.execute(
            """SELECT * FROM dealer_inventory_lot_allocations
               WHERE order_id=%s ORDER BY allocation_seq FOR UPDATE""",
            (str(order_id),),
        )
        allocations = [_row_dict(item) for item in cur.fetchall()]
        for allocation in allocations:
            if allocation["status"] == "restored":
                continue
            if allocation["status"] != "consumed":
                raise ResaleError("REFUND_ALLOCATION_STATE_INVALID", "卖方原批次分配状态异常")
            cur.execute(
                """UPDATE dealer_inventory_lots
                   SET remaining_points=remaining_points+%s,
                       remaining_cost_cents=remaining_cost_cents+%s,
                       status='active', updated_at=NOW()
                   WHERE lot_id=%s""",
                (
                    int(allocation["points"]), int(allocation["cost_basis_cents"]),
                    str(allocation["seller_lot_id"]),
                ),
            )
        cur.execute(
            """UPDATE dealer_inventory_lot_allocations
               SET status='restored', restored_at=NOW()
               WHERE order_id=%s AND status='consumed'""",
            (str(order_id),),
        )
        cur.execute(
            """UPDATE agent_inventory_wallets
               SET paid_inventory_points=paid_inventory_points+%s,
                   total_allocated_points=GREATEST(0,total_allocated_points-%s), updated_at=NOW()
               WHERE agent_user_id=%s
               RETURNING paid_inventory_points, bonus_inventory_points""",
            (points, points, seller_id),
        )
        seller_after = _row_dict(cur.fetchone())
        seller_paid_after = int(seller_after["paid_inventory_points"])
        seller_bonus_after = int(seller_after["bonus_inventory_points"])

    transfer_id = str(order["transfer_id"])
    cur.execute(
        "UPDATE dealer_inventory_transfers SET state='refunded', refunded_at=NOW() "
        "WHERE transfer_id=%s AND state='settled'",
        (transfer_id,),
    )
    cur.execute(
        """INSERT INTO dealer_inventory_transfer_entries
           (transfer_id, owner_agent_user_id, counterparty_user_id, direction,
            points_delta, lot_id, cost_basis_cents, balance_paid_after)
           VALUES (%s,%s,%s,'refund_out',%s,%s,%s,%s),
                  (%s,%s,%s,'refund_in',%s,NULL,%s,%s)
           ON CONFLICT (transfer_id, owner_agent_user_id, direction) DO NOTHING""",
        (
            transfer_id, buyer_id, seller_id, -points, str(buyer_lot["lot_id"]),
            int(order["sale_amount_cents"]), int(buyer_after["paid_inventory_points"]),
            transfer_id, seller_id, buyer_id, points,
            int(order["seller_cost_basis_cents"]), seller_paid_after,
        ),
    )
    _insert_legacy_inventory_tx(
        cur, user_id=buyer_id, type_="resale_refund_out", points=-points,
        paid_after=int(buyer_after["paid_inventory_points"]),
        bonus_after=int(buyer_after["bonus_inventory_points"]), order_id=str(order_id),
        transfer_id=transfer_id, lot_id=str(buyer_lot["lot_id"]), counterparty_id=seller_id,
        cost_basis_cents=int(order["sale_amount_cents"]), description="本跳整批退款转出",
    )
    _insert_legacy_inventory_tx(
        cur, user_id=seller_id, type_="resale_refund_in", points=points,
        paid_after=seller_paid_after, bonus_after=seller_bonus_after, order_id=str(order_id),
        transfer_id=transfer_id, lot_id=None, counterparty_id=buyer_id,
        cost_basis_cents=int(order["seller_cost_basis_cents"]), description="本跳退款恢复原库存批次",
    )
    cur.execute(
        """SELECT profit_id,status,margin_cents,revenue_ledger_id
           FROM dealer_resale_profit_ledger WHERE order_id=%s FOR UPDATE""",
        (str(order_id),),
    )
    profit = _row_dict(cur.fetchone())
    if profit and profit.get("revenue_ledger_id") is not None:
        revenue_id = int(profit["revenue_ledger_id"])
        cur.execute("SELECT status,reversed_at FROM agent_revenue_ledger WHERE id=%s FOR UPDATE", (revenue_id,))
        revenue = _row_dict(cur.fetchone())
        if revenue and revenue.get("reversed_at") is None:
            if revenue.get("status") == "frozen":
                cur.execute(
                    """UPDATE agent_revenue_ledger
                       SET status='cancelled',reversed_at=NOW(),note=COALESCE(note,'') || %s
                       WHERE id=%s AND status='frozen'""",
                    (" | 本跳 B2B 退款冲销", revenue_id),
                )
            elif revenue.get("status") == "settled" and int(profit["margin_cents"]) > 0:
                from services.agent_revenue import insert_revenue_clawback
                clawback_id = insert_revenue_clawback(
                    cur, agent_user_id=seller_id, original_ledger_id=revenue_id,
                    clawback_amount_cents=int(profit["margin_cents"]),
                    note=f"逐级转售本跳退款冲销 · order={order_id}",
                )
                cur.execute(
                    "UPDATE agent_revenue_ledger SET reversed_at=NOW(),reversed_by_ledger_id=%s WHERE id=%s",
                    (clawback_id, revenue_id),
                )
    cur.execute(
        """UPDATE dealer_resale_profit_ledger
           SET status='reversed', reversed_at=NOW()
           WHERE order_id=%s AND status IN ('pending','available')""",
        (str(order_id),),
    )
    cur.execute(
        "UPDATE dealer_resale_orders SET state='refunded', refunded_at=NOW(), updated_at=NOW() WHERE order_id=%s",
        (str(order_id),),
    )
    cur.execute(
        """UPDATE dealer_resale_refunds
           SET status='completed', completed_at=NOW()
           WHERE refund_id=%s AND status IN ('requested','processing')""",
        (str(refund["refund_id"]),),
    )
    return True


def mature_consumer_profits(*, limit: int = 200, cur=None) -> Dict[str, Any]:
    """Release direct-service principal recovery plus margin after 72h.

    The same transaction-level advisory lock is used by consumer refund intake
    and refund completion, making maturity/refund ordering deterministic across
    workers.  Redis and scheduler process identity are intentionally irrelevant.
    """
    if cur is None:
        with get_db() as conn:
            db_cur = conn.cursor()
            result = mature_consumer_profits(limit=limit, cur=db_cur)
            conn.commit()
            return result
    cur.execute("SELECT to_regclass('public.dealer_consumer_sales') AS table_name")
    if not _row_dict(cur.fetchone()).get("table_name"):
        return {"matured_count": 0, "order_ids": [], "schema_present": False}
    cur.execute(
        """SELECT s.order_id
           FROM dealer_consumer_sales s
           JOIN agent_revenue_ledger l ON l.id=s.revenue_ledger_id
           WHERE l.status='frozen'
             AND l.settle_at <= clock_timestamp() AND l.reversed_at IS NULL
             AND NOT l.manual_review_required
             AND (
               s.state='paid'
               OR (
                 s.state='refunded'
                 AND EXISTS (
                   SELECT 1 FROM consumer_refund_cases c
                   WHERE c.source_order_id=s.order_id AND c.status='completed'
                      AND c.revenue_reversed_cents < s.agent_payable_cents
                 )
               )
             )
           ORDER BY l.settle_at,s.order_id LIMIT %s""",
        (max(1, min(int(limit), 1000)),),
    )
    matured: List[str] = []
    for candidate in cur.fetchall():
        order_id = str(_row_dict(candidate)["order_id"])
        _order_xact_lock(cur, order_id)
        cur.execute(
            """SELECT s.state,s.revenue_ledger_id,l.status,l.settle_at,l.reversed_at,
                      l.manual_review_required,r.refund_status
               FROM dealer_consumer_sales s
               JOIN agent_revenue_ledger l ON l.id=s.revenue_ledger_id
               JOIN recharge_orders r ON r.id=s.order_id
               WHERE s.order_id=%s
               FOR UPDATE OF s,l,r""",
            (order_id,),
        )
        state = _row_dict(cur.fetchone())
        if not state:
            continue
        refund_status = str(state.get("refund_status") or "").lower()
        residual_refund = state.get("state") == "refunded"
        if (
            state.get("state") not in {"paid", "refunded"}
            or state.get("status") != "frozen"
            or state.get("reversed_at") is not None
            or bool(state.get("manual_review_required"))
            or state.get("settle_at") is None
            or (
                not residual_refund
                and refund_status not in {"", "rejected", "failed"}
            )
            or (
                residual_refund
                and refund_status != "completed"
            )
        ):
            continue
        cur.execute("SELECT clock_timestamp() >= %s AS due", (state["settle_at"],))
        if not bool(_row_dict(cur.fetchone()).get("due")):
            continue
        cur.execute(
            "SELECT status FROM consumer_refund_cases WHERE source_order_id=%s FOR UPDATE",
            (order_id,),
        )
        refund_case = _row_dict(cur.fetchone())
        if refund_case and refund_case.get("status") not in (
            {"completed"} if residual_refund else {"rejected"}
        ):
            continue
        cur.execute(
            """UPDATE agent_revenue_ledger
               SET status='settled',settled_at=clock_timestamp()
               WHERE id=%s AND status='frozen' AND reversed_at IS NULL""",
            (int(state["revenue_ledger_id"]),),
        )
        if cur.rowcount == 1:
            matured.append(order_id)
    return {"matured_count": len(matured), "order_ids": matured, "schema_present": True}


def mature_profits(*, limit: int = 200, cur=None) -> Dict[str, Any]:
    if cur is None:
        with get_db() as conn:
            db_cur = conn.cursor()
            db_cur.execute("SELECT to_regclass('public.dealer_resale_profit_ledger') AS table_name")
            if not _row_dict(db_cur.fetchone()).get("table_name"):
                return {"matured_count": 0, "profit_ids": [], "schema_present": False}
            result = mature_profits(limit=limit, cur=db_cur)
            conn.commit()
            return result
    cur.execute(
        """SELECT p.profit_id,p.order_id
           FROM dealer_resale_profit_ledger p
           WHERE p.status='pending' AND p.available_at <= clock_timestamp()
           ORDER BY p.available_at,p.profit_id LIMIT %s""",
        (max(1, min(int(limit), 1000)),),
    )
    candidates = [_row_dict(row) for row in cur.fetchall()]
    ids: List[int] = []
    for candidate in candidates:
        _order_xact_lock(cur, str(candidate["order_id"]))
        # Re-read after the order lock. A refund request that won the lock is now
        # visible, so profit cannot become withdrawable during its 72h window.
        cur.execute(
            """SELECT p.profit_id,p.order_id,p.seller_user_id,p.buyer_user_id,
                      p.seller_cost_basis_cents,p.sale_amount_cents,p.margin_cents,p.available_at
               FROM dealer_resale_profit_ledger p
               JOIN dealer_resale_orders o ON o.order_id=p.order_id
               WHERE p.profit_id=%s AND p.status='pending'
                 AND p.available_at <= clock_timestamp() AND o.state='paid'
                 AND NOT EXISTS (
                     SELECT 1 FROM dealer_resale_refunds r
                     WHERE r.order_id=p.order_id
                       AND r.status IN ('requested','processing','manual_review')
                 )
               FOR UPDATE OF p""",
            (int(candidate["profit_id"]),),
        )
        profit = _row_dict(cur.fetchone())
        if not profit:
            continue
        revenue_id = None
        margin = int(profit["margin_cents"])
        if margin > 0:
            cur.execute(
                """INSERT INTO agent_revenue_ledger
                   (agent_user_id,source,recharge_order_id,customer_user_id,
                    customer_paid_cents,factory_cents,gateway_fee_bps,gateway_fee_cents,
                    settlement_service_fee_bps,settlement_service_fee_cents,
                    agent_margin_before_tax_cents,tax_rate_bps,tax_mode,tax_withholding_cents,
                    agent_settlement_cents,status,settle_at,settled_at,manual_review_required,note)
                   VALUES (%s,'recharge',%s,%s,%s,%s,0,0,0,0,%s,0,'withheld',0,%s,
                           'settled',%s,clock_timestamp(),FALSE,%s)
                   RETURNING id""",
                (
                    int(profit["seller_user_id"]), str(profit["order_id"]),
                    int(profit["buyer_user_id"]), int(profit["sale_amount_cents"]),
                    int(profit["seller_cost_basis_cents"]), margin, margin,
                    profit["available_at"], "经销商逐级转售 · 仅本跳真实利润",
                ),
            )
            revenue_id = int(_row_dict(cur.fetchone())["id"])
        cur.execute(
            """UPDATE dealer_resale_profit_ledger
               SET status='available', available_since=clock_timestamp(),
                   revenue_ledger_id=%s
               WHERE profit_id=%s AND status='pending'""",
            (revenue_id, int(profit["profit_id"])),
        )
        if cur.rowcount == 1:
            ids.append(int(profit["profit_id"]))
    remaining = max(0, max(1, min(int(limit), 1000)) - len(ids))
    jit_ids: List[int] = []
    if remaining:
        cur.execute(
            """SELECT hp.profit_id,hp.root_order_id
               FROM dealer_resale_hop_profit_ledger hp
               JOIN dealer_resale_fulfillment_plans fp ON fp.plan_id=hp.plan_id
               WHERE hp.status='pending' AND hp.available_at<=clock_timestamp()
                 AND fp.state='settled'
               ORDER BY hp.available_at,hp.profit_id LIMIT %s""", (remaining,),
        )
        jit_candidates = [_row_dict(row) for row in cur.fetchall()]
        for candidate in jit_candidates:
            _order_xact_lock(cur, str(candidate["root_order_id"]))
            cur.execute(
                """SELECT hp.*,fp.order_kind,
                          (hp.hop_seq=(SELECT MAX(h2.hop_seq)
                           FROM dealer_resale_fulfillment_hops h2
                           WHERE h2.plan_id=hp.plan_id)) AS is_final_hop,
                          o.state AS b2b_order_state
                   FROM dealer_resale_hop_profit_ledger hp
                   JOIN dealer_resale_fulfillment_plans fp ON fp.plan_id=hp.plan_id
                   LEFT JOIN dealer_resale_orders o ON o.order_id=hp.root_order_id
                   WHERE hp.profit_id=%s AND hp.status='pending'
                     AND hp.available_at<=clock_timestamp() AND fp.state='settled'
                   FOR UPDATE OF hp""", (int(candidate["profit_id"]),),
            )
            profit = _row_dict(cur.fetchone())
            if not profit:
                continue
            final_b2b = profit["order_kind"] == "B2B" and bool(profit["is_final_hop"])
            if final_b2b:
                cur.execute(
                    """SELECT 1 FROM dealer_resale_refunds
                       WHERE order_id=%s AND status IN ('requested','processing','manual_review')""",
                    (str(profit["root_order_id"]),),
                )
                if profit.get("b2b_order_state") != "paid" or cur.fetchone():
                    continue
            revenue_id = None
            margin = int(profit["margin_cents"])
            payable = int(profit["agent_payable_cents"])
            if payable:
                cur.execute(
                    """INSERT INTO agent_revenue_ledger
                       (agent_user_id,source,recharge_order_id,customer_user_id,
                        customer_paid_cents,factory_cents,gateway_fee_bps,gateway_fee_cents,
                        settlement_service_fee_bps,settlement_service_fee_cents,
                        agent_margin_before_tax_cents,tax_rate_bps,tax_mode,tax_withholding_cents,
                        agent_settlement_cents,status,settle_at,settled_at,manual_review_required,note)
                       VALUES (%s,'recharge',%s,%s,%s,0,0,0,0,0,%s,0,'withheld',0,%s,
                                'settled',%s,clock_timestamp(),FALSE,%s) RETURNING id""",
                    (
                        int(profit["seller_user_id"]), str(profit["root_order_id"]),
                        int(profit["buyer_user_id"]), int(profit["sale_amount_cents"]),
                        margin, payable, profit["available_at"],
                        f"JIT 逐跳历史本金回收 + 利润 · hop={int(profit['hop_seq'])}",
                    ),
                )
                revenue_id = int(_row_dict(cur.fetchone())["id"])
            cur.execute(
                """UPDATE dealer_resale_hop_profit_ledger
                   SET status='available',available_since=clock_timestamp(),revenue_ledger_id=%s
                   WHERE profit_id=%s AND status='pending'""",
                (revenue_id, int(profit["profit_id"])),
            )
            if cur.rowcount == 1:
                jit_ids.append(int(profit["profit_id"]))
    consumer_result = mature_consumer_profits(limit=limit, cur=cur)
    return {
        "matured_count": len(ids) + len(jit_ids),
        "profit_ids": ids + jit_ids,
        "jit_matured_count": len(jit_ids),
        "consumer_matured_count": int(consumer_result["matured_count"]),
        "consumer_order_ids": list(consumer_result["order_ids"]),
    }


def issue_manufacturer_lot(
    cur, *, points: int, acquisition_cost_cents: int, pricing_version: str,
    idempotency_key: str, evidence: Dict[str, Any], issued_by: int,
    promotion_campaign_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Admin-only origin issuance for the configured manufacturer seller.

    This is the sole explicit boundary where inventory enters the resale system.
    It is not automatic replenishment and is never called from a quote, purchase,
    callback, or scheduler. Every downstream order still consumes this real lot.
    """
    assert_schema_ready(cur)
    points = int(points)
    cost = int(acquisition_cost_cents)
    key = str(idempotency_key).strip()
    version = str(pricing_version).strip()
    if points <= 0 or cost <= 0 or not key or not version:
        raise ResaleError(
            "MANUFACTURER_ISSUE_INPUT_INVALID",
            "厂家原始批次必须提供正算力、正真实成本、版本和幂等键",
        )
    if not isinstance(evidence, dict) or not evidence:
        raise ResaleError("MANUFACTURER_ISSUE_EVIDENCE_REQUIRED", "厂家原始批次必须提供审计证据")
    settings = _global_settings(cur, for_update=True)
    seller_id = int(settings.get("platform_seller_user_id") or 0)
    if seller_id <= 0:
        raise ResaleError("PLATFORM_SELLER_NOT_PREPARED", "请先显式配置平台厂家账号")
    issue_id = _stable_id("DMI", key)
    lot_id = _stable_id("DML", key)
    _order_xact_lock(cur, f"manufacturer-issue:{key}")
    cur.execute(
        "SELECT * FROM dealer_manufacturer_lot_issuances WHERE issue_id=%s FOR UPDATE",
        (issue_id,),
    )
    existing = cur.fetchone()
    if existing:
        row = _row_dict(existing)
        cur.execute(
            """SELECT promotion_campaign_id,promotion_product_code,
                      promotion_root_catalog_version
               FROM dealer_inventory_lots WHERE lot_id=%s""",
            (str(row["lot_id"]),),
        )
        lot = _row_dict(cur.fetchone())
        requested_campaign = str(promotion_campaign_id).strip() if promotion_campaign_id else None
        expected_product = None
        expected_root_version = None
        if requested_campaign:
            cur.execute(
                """SELECT product_code,root_catalog_version,funding_scope
                   FROM dealer_resale_promotions WHERE campaign_id=%s FOR SHARE""",
                (requested_campaign,),
            )
            requested_promotion = _row_dict(cur.fetchone())
            if not requested_promotion or requested_promotion.get("funding_scope") != "PLATFORM":
                raise ResaleError("PROMOTION_ORIGIN_INVALID", "厂家促销批次活动不存在或非平台出资")
            expected_product = str(requested_promotion["product_code"])
            expected_root_version = str(requested_promotion["root_catalog_version"])
        if (
            int(row["platform_seller_user_id"]) != seller_id
            or int(row["points"]) != points
            or int(row["acquisition_cost_cents"]) != cost
            or str(row["pricing_version"]) != version
            or str(row["idempotency_key"]) != key
            or _json_object(row.get("evidence_jsonb")) != evidence
            or lot.get("promotion_campaign_id") != requested_campaign
            or lot.get("promotion_product_code") != expected_product
            or lot.get("promotion_root_catalog_version") != expected_root_version
            or (requested_campaign is not None and expected_root_version != version)
        ):
            raise ResaleError(
                "MANUFACTURER_ISSUE_IDEMPOTENCY_CONFLICT",
                "厂家原始批次幂等键已用于不同内容",
            )
        row["promotion_campaign_id"] = lot.get("promotion_campaign_id")
        row["idempotent"] = True
        return row
    promotion: Dict[str, Any] = {}
    if promotion_campaign_id:
        campaign = str(promotion_campaign_id).strip()
        cur.execute(
            "SELECT * FROM dealer_resale_promotions WHERE campaign_id=%s FOR SHARE",
            (campaign,),
        )
        promo_row = _row_dict(cur.fetchone())
        if not promo_row or str(promo_row.get("funding_scope")) != "PLATFORM":
            raise ResaleError("PROMOTION_ORIGIN_INVALID", "厂家促销批次只能绑定已登记的平台活动")
        if str(promo_row.get("status")) not in {"draft", "active"}:
            raise ResaleError("PROMOTION_ORIGIN_INVALID", "已结束活动不能发行新的厂家促销批次")
        if str(promo_row.get("root_catalog_version") or "") != version:
            raise ResaleError(
                "PROMOTION_ORIGIN_CATALOG_MISMATCH",
                "厂家活动批次目录版本必须与活动根目录版本完全一致",
            )
        promotion = _promotion_snapshot(promo_row)
    wallet = _inventory_wallet(cur, seller_id)
    paid_after = int(wallet["paid_inventory_points"]) + points
    bonus_after = int(wallet["bonus_inventory_points"])
    cur.execute(
        """INSERT INTO dealer_inventory_lots
           (lot_id,owner_agent_user_id,original_points,remaining_points,reserved_points,
            acquisition_cost_cents,remaining_cost_cents,reserved_cost_cents,status,
             source_kind,pricing_version,evidence_jsonb,promotion_campaign_id,
             promotion_funding_scope,promotion_snapshot_jsonb,promotion_product_code,
             promotion_root_catalog_version)
           VALUES (%s,%s,%s,%s,0,%s,%s,0,'active','manufacturer_origin',%s,%s::jsonb,
                    %s,%s,%s::jsonb,%s,%s)""",
        (
            lot_id, seller_id, points, points, cost, cost, version,
            json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
            promotion.get("campaign_id"), promotion.get("funding_scope"),
            json.dumps(promotion, ensure_ascii=False, separators=(",", ":")),
            promotion.get("product_code"), promotion.get("root_catalog_version"),
        ),
    )
    cur.execute(
        """INSERT INTO dealer_manufacturer_lot_issuances
           (issue_id,lot_id,platform_seller_user_id,points,acquisition_cost_cents,
            pricing_version,idempotency_key,evidence_jsonb,issued_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
           RETURNING *""",
        (
            issue_id, lot_id, seller_id, points, cost, version, key,
            json.dumps(evidence, ensure_ascii=False, separators=(",", ":")), int(issued_by),
        ),
    )
    issuance = _row_dict(cur.fetchone())
    cur.execute(
        """UPDATE agent_inventory_wallets
           SET paid_inventory_points=paid_inventory_points+%s,
               total_purchased_points=total_purchased_points+%s,
               updated_at=clock_timestamp()
           WHERE agent_user_id=%s""",
        (points, points, seller_id),
    )
    if cur.rowcount != 1:
        raise ResaleError("MANUFACTURER_ISSUE_WALLET_FAILED", "厂家原始批次聚合库存入账失败")
    cur.execute(
        """INSERT INTO agent_inventory_transactions
           (agent_user_id,type,pool,points,balance_paid_after,balance_bonus_after,
            description,lot_id,cost_basis_cents)
           VALUES (%s,'manufacturer_origin_in','paid',%s,%s,%s,%s,%s,%s)""",
        (
            seller_id, points, paid_after, bonus_after,
            f"厂家原始库存发行 · issue={issue_id}", lot_id, cost,
        ),
    )
    issuance["promotion_campaign_id"] = promotion.get("campaign_id")
    issuance["promotion_product_code"] = promotion.get("product_code")
    issuance["promotion_root_catalog_version"] = promotion.get("root_catalog_version")
    return issuance


def opening_lot_dry_run(cur) -> Dict[str, Any]:
    cur.execute(
        """SELECT w.agent_user_id, w.paid_inventory_points, w.frozen_inventory_points,
                  COALESCE(SUM(CASE
                    WHEN l.status IN ('active','refund_pending')
                    THEN l.remaining_points + l.reserved_points ELSE 0 END),0) AS lot_points
           FROM agent_inventory_wallets w
           LEFT JOIN dealer_inventory_lots l ON l.owner_agent_user_id=w.agent_user_id
           GROUP BY w.agent_user_id, w.paid_inventory_points, w.frozen_inventory_points
           HAVING w.paid_inventory_points + w.frozen_inventory_points <>
                  COALESCE(SUM(CASE
                    WHEN l.status IN ('active','refund_pending')
                    THEN l.remaining_points + l.reserved_points ELSE 0 END),0)
           ORDER BY w.agent_user_id"""
    )
    items = []
    for raw in cur.fetchall():
        row = _row_dict(raw)
        wallet_points = int(row["paid_inventory_points"]) + int(row["frozen_inventory_points"])
        lot_points = int(row["lot_points"])
        items.append({
            "owner_agent_user_id": int(row["agent_user_id"]),
            "wallet_points": wallet_points,
            "lot_points": lot_points,
            "missing_points": wallet_points - lot_points,
            "blocked": wallet_points - lot_points < 0 or int(row["frozen_inventory_points"]) > 0,
        })
    return {"items": items, "mismatch_count": len(items)}


def prepare_opening_lot(
    cur, *, owner_agent_user_id: int, points: int, acquisition_cost_cents: int,
    pricing_version: str, idempotency_key: str, evidence: Dict[str, Any],
) -> Dict[str, Any]:
    owner = int(owner_agent_user_id)
    points = int(points)
    cost = int(acquisition_cost_cents)
    key = str(idempotency_key).strip()
    version = str(pricing_version).strip()
    if points <= 0 or cost <= 0 or not key or not version:
        raise ResaleError("OPENING_LOT_INPUT_INVALID", "opening lot 必须显式提供正算力、正真实成本、版本和幂等键")
    if not isinstance(evidence, dict) or not evidence:
        raise ResaleError("OPENING_LOT_EVIDENCE_REQUIRED", "opening lot 必须提供非空审计证据，禁止猜测历史成本")
    lot_id = _stable_id("DLO", f"{owner}:{key}")
    # Serialize every prepare for one owner, even when callers accidentally use
    # different idempotency keys for the same uncovered balance.
    _order_xact_lock(cur, f"opening-lot-owner:{owner}")
    cur.execute("SELECT * FROM dealer_inventory_lots WHERE lot_id=%s FOR UPDATE", (lot_id,))
    existing = cur.fetchone()
    if existing:
        row = _row_dict(existing)
        if (
            int(row["owner_agent_user_id"]) != owner
            or int(row["original_points"]) != points
            or int(row["acquisition_cost_cents"]) != cost
            or str(row["pricing_version"]) != version
            or _json_object(row.get("evidence_jsonb")) != evidence
        ):
            raise ResaleError("OPENING_LOT_IDEMPOTENCY_CONFLICT", "opening lot 幂等键已用于不同内容")
        return row
    cur.execute(
        """SELECT paid_inventory_points, frozen_inventory_points
           FROM agent_inventory_wallets WHERE agent_user_id=%s FOR UPDATE""",
        (owner,),
    )
    wallet = _row_dict(cur.fetchone())
    if not wallet:
        raise ResaleError("OPENING_LOT_WALLET_MISSING", "目标经销商库存钱包不存在")
    if int(wallet["frozen_inventory_points"]) != 0:
        raise ResaleError("OPENING_LOT_FROZEN", "目标经销商存在冻结库存，拒绝自动准备 opening lot")
    cur.execute(
        """SELECT COALESCE(SUM(remaining_points+reserved_points),0) AS c
           FROM dealer_inventory_lots
           WHERE owner_agent_user_id=%s AND status IN ('active','refund_pending')""",
        (owner,),
    )
    covered = int(_row_dict(cur.fetchone()).get("c") or 0)
    missing = int(wallet["paid_inventory_points"]) - covered
    if missing != points:
        raise ResaleError(
            "OPENING_LOT_COVERAGE_MISMATCH",
            f"显式 opening lot 算力必须精确覆盖缺口 {missing}，收到 {points}",
        )
    cur.execute(
        """INSERT INTO dealer_inventory_lots
           (lot_id, owner_agent_user_id, original_points, remaining_points,
            acquisition_cost_cents, remaining_cost_cents, status, source_kind,
            pricing_version, evidence_jsonb)
           VALUES (%s,%s,%s,%s,%s,%s,'active','opening_balance',%s,%s::jsonb)
           RETURNING *""",
        (
            lot_id, owner, points, points, cost, cost, version,
            json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
        ),
    )
    return _row_dict(cur.fetchone())


def readiness(cur=None) -> Dict[str, Any]:
    if cur is None:
        with get_db() as conn:
            return readiness(cur=conn.cursor())
    schema = schema_status(cur)
    blockers = list(schema["blockers"])
    warnings: List[str] = []
    if schema["ready"]:
        settings = _global_settings(cur)
        platform_seller_id = settings.get("platform_seller_user_id")
        if platform_seller_id is None:
            blockers.append("平台厂家账号未配置")
        else:
            cur.execute(
                """SELECT
                     (SELECT COUNT(*) FROM dealer_manufacturer_lot_issuances
                       WHERE platform_seller_user_id=%s) AS issuance_count,
                     (SELECT COALESCE(SUM(remaining_points),0) FROM dealer_inventory_lots
                       WHERE owner_agent_user_id=%s AND source_kind='manufacturer_origin'
                         AND status='active') AS sellable_points,
                     (SELECT COALESCE(paid_inventory_points,0) FROM agent_inventory_wallets
                       WHERE agent_user_id=%s) AS wallet_points""",
                (int(platform_seller_id), int(platform_seller_id), int(platform_seller_id)),
            )
            origin = _row_dict(cur.fetchone())
            if int(origin.get("issuance_count") or 0) <= 0:
                blockers.append("平台厂家尚无经审计的原始库存发行记录")
            if int(origin.get("sellable_points") or 0) <= 0:
                blockers.append("平台厂家尚无可售真实库存批次")
            if int(origin.get("sellable_points") or 0) != int(origin.get("wallet_points") or 0):
                blockers.append(
                    "平台厂家聚合库存与原始批次不一致 "
                    f"wallet={int(origin.get('wallet_points') or 0)} "
                    f"lots={int(origin.get('sellable_points') or 0)}"
                )
        cur.execute(
            """SELECT v.id, COUNT(e.id) AS entry_count
               FROM pricing_catalog_versions v
               LEFT JOIN pricing_catalog_entries e ON e.version_id=v.id
               WHERE v.catalog_type='procurement' AND v.scope_key='PLATFORM_BASE'
                 AND v.status='published'
               GROUP BY v.id ORDER BY v.published_at DESC NULLS LAST LIMIT 1"""
        )
        procurement = _row_dict(cur.fetchone())
        if not procurement or int(procurement.get("entry_count") or 0) <= 0:
            blockers.append("procurement / PLATFORM_BASE 未发布或没有有效明细")
        try:
            from config.pricing_ssot_flags import pricing_flags_snapshot

            pricing_flags = pricing_flags_snapshot(cur=cur)
            if not pricing_flags.get("PRICING_DUAL_SSOT_ENABLED", False):
                blockers.append("PRICING_DUAL_SSOT_ENABLED 未开启，现役入口不会进入持久化报价链")
        except Exception as exc:  # noqa: BLE001
            pricing_flags = None
            blockers.append(f"定价前置 flag 无法权威读取: {exc}")
        from services import channel_pricing

        relationship_check = channel_pricing.relationship_status(cur=cur)
        blockers.extend(
            f"渠道关系: {message}" for message in relationship_check.get("blockers", [])
        )
        cur.execute(
            """SELECT p.seller_user_id
               FROM dealer_resale_policies p
               WHERE p.downstream_markup_bps < 10000
                  OR p.authorized_min_markup_bps < 10000
                  OR p.authorized_max_markup_bps < p.authorized_min_markup_bps
                  OR p.downstream_markup_bps NOT BETWEEN
                     p.authorized_min_markup_bps AND p.authorized_max_markup_bps"""
        )
        invalid_policy_ids = [str(_row_dict(row).get("seller_user_id")) for row in cur.fetchall()]
        if invalid_policy_ids:
            blockers.append("下级售价策略无效: " + ",".join(invalid_policy_ids))
        cur.execute(
            """SELECT lot_id FROM dealer_inventory_lots
               WHERE acquisition_cost_cents <= 0
                  OR (remaining_points > 0 AND remaining_cost_cents <= 0)
               ORDER BY lot_id LIMIT 100"""
        )
        invalid_cost_lots = [str(_row_dict(row).get("lot_id")) for row in cur.fetchall()]
        if invalid_cost_lots:
            blockers.append("库存批次缺少正真实成本: " + ",".join(invalid_cost_lots))
        cur.execute(
            """WITH targets AS (
                 SELECT buyer_dealer_id AS user_id, 'SV'::text AS code_kind
                 FROM channel_pricing_relationships
                 WHERE status='active' AND effective_to IS NULL
                 UNION
                 SELECT upstream_channel_account_id, 'CH'::text
                 FROM channel_pricing_relationships
                 WHERE status='active' AND effective_to IS NULL
               )
               SELECT t.user_id, t.code_kind
               FROM targets t
               LEFT JOIN public_account_codes c ON c.user_id=t.user_id
               WHERE (t.code_kind='SV' AND c.service_account_code IS NULL)
                  OR (t.code_kind='CH' AND c.channel_account_code IS NULL)
               ORDER BY t.code_kind, t.user_id"""
        )
        missing_codes = [
            f"{_row_dict(row).get('code_kind')}:{_row_dict(row).get('user_id')}"
            for row in cur.fetchall()
        ]
        if missing_codes:
            blockers.append("目标经销商/渠道缺公开编号: " + ",".join(missing_codes))
        dry = opening_lot_dry_run(cur)
        for item in dry["items"]:
            blockers.append(
                f"经销商 {item['owner_agent_user_id']} 聚合库存与批次不一致 "
                f"wallet={item['wallet_points']} lots={item['lot_points']}"
            )
        cur.execute(
            """SELECT o.order_id FROM dealer_resale_orders o
               JOIN recharge_orders r ON r.id=o.order_id
               WHERE r.payment_status='pending'
                  AND (
                    o.state<>'reserved'
                    OR BTRIM(o.quote_id)=''
                    OR o.points <= 0
                    OR o.sale_amount_cents <= 0
                    OR o.margin_cents <> o.sale_amount_cents-o.seller_cost_basis_cents
                    OR jsonb_array_length(o.seller_lot_allocations)=0
                  )"""
        )
        invalid_pending = [str(_row_dict(row).get("order_id")) for row in cur.fetchall()]
        if invalid_pending:
            blockers.append("待支付逐级转售订单快照不完整: " + ",".join(invalid_pending))
        cur.execute(
            """SELECT s.order_id FROM dealer_consumer_sales s
               JOIN recharge_orders r ON r.id=s.order_id
               WHERE (r.payment_status='pending' AND s.state<>'reserved')
                  OR BTRIM(s.quote_id)=''
                  OR s.points<=0 OR s.sale_amount_cents<=0
                   OR s.margin_cents<>s.sale_amount_cents-s.seller_cost_basis_cents
                   OR ((s.principal_recovery_cents IS NULL)<>(s.agent_payable_cents IS NULL))
                   OR (EXISTS (
                         SELECT 1 FROM dealer_resale_fulfillment_plans fp
                         WHERE fp.root_order_id=s.order_id
                       ) AND (
                         s.principal_recovery_cents IS NULL
                         OR s.agent_payable_cents<>s.principal_recovery_cents+s.margin_cents
                       ))
                   OR (s.state='paid' AND s.agent_payable_cents IS NOT NULL AND NOT EXISTS (
                         SELECT 1 FROM agent_revenue_ledger ar
                         WHERE ar.id=s.revenue_ledger_id
                           AND ar.agent_user_id=s.seller_user_id
                           AND ar.agent_settlement_cents=s.agent_payable_cents
                           AND ar.agent_margin_before_tax_cents=s.margin_cents
                       ))
                   OR jsonb_array_length(s.seller_lot_allocations)=0
                  OR s.digital_goods_acknowledged_at IS NULL"""
        )
        invalid_consumer = [str(_row_dict(row).get("order_id")) for row in cur.fetchall()]
        if invalid_consumer:
            blockers.append("消费者直属销售订单快照不完整: " + ",".join(invalid_consumer))
        cur.execute(
            """SELECT p.root_order_id
               FROM dealer_resale_fulfillment_plans p
               JOIN recharge_orders r ON r.id=p.root_order_id
               WHERE p.final_sale_amount_cents<>r.amount_cents
                  OR CASE WHEN
                       COALESCE(p.funds_conservation_jsonb->>'final_paid_cents','') ~ '^[0-9]+$'
                       AND COALESCE(p.funds_conservation_jsonb->>'platform_root_revenue_cents','') ~ '^[0-9]+$'
                       AND COALESCE(p.funds_conservation_jsonb->>'dealer_margin_cents','') ~ '^[0-9]+$'
                       AND COALESCE(p.funds_conservation_jsonb->>'final_seller_margin_cents','') ~ '^[0-9]+$'
                       AND COALESCE(p.funds_conservation_jsonb->>'historical_principal_cents','') ~ '^[0-9]+$'
                       AND COALESCE(p.funds_conservation_jsonb->>'explicit_fee_tax_cents','') ~ '^[0-9]+$'
                       AND COALESCE(p.funds_conservation_jsonb->>'assigned_cents','') ~ '^[0-9]+$'
                       AND COALESCE(p.funds_conservation_jsonb->>'residual_cents','') ~ '^-?[0-9]+$'
                     THEN
                       (p.funds_conservation_jsonb->>'final_paid_cents')::bigint<>
                         p.final_sale_amount_cents
                       OR (p.funds_conservation_jsonb->>'assigned_cents')::bigint<>
                          (p.funds_conservation_jsonb->>'platform_root_revenue_cents')::bigint
                          +(p.funds_conservation_jsonb->>'dealer_margin_cents')::bigint
                          +(p.funds_conservation_jsonb->>'final_seller_margin_cents')::bigint
                          +(p.funds_conservation_jsonb->>'historical_principal_cents')::bigint
                          +(p.funds_conservation_jsonb->>'explicit_fee_tax_cents')::bigint
                       OR (p.funds_conservation_jsonb->>'residual_cents')::bigint<>0
                     ELSE TRUE END
                  OR (p.state='reserved' AND r.payment_status<>'pending')
                  OR (p.state='settled' AND r.payment_status<>'paid')
                  OR (p.state='cancelled' AND r.payment_status<>'cancelled')
                   OR p.state IN ('settling','manual_review')
                   OR (p.state='settled' AND p.final_sale_amount_cents<>
                         COALESCE((
                           SELECT SUM(l.platform_root_revenue_cents+l.agent_payable_cents)
                           FROM dealer_resale_hop_profit_ledger l
                           WHERE l.plan_id=p.plan_id
                         ),0)
                         +COALESCE((
                           SELECT s.agent_payable_cents FROM dealer_consumer_sales s
                           WHERE s.order_id=p.root_order_id
                         ),0))
                  OR NOT EXISTS (
                       SELECT 1 FROM dealer_resale_fulfillment_hops h
                       WHERE h.plan_id=p.plan_id
                     )
                  OR EXISTS (
                       SELECT 1 FROM dealer_resale_fulfillment_hops h
                       WHERE h.plan_id=p.plan_id AND (
                         h.points<>(SELECT COALESCE(SUM(a.points),0)
                                   FROM dealer_resale_fulfillment_allocations a
                                   WHERE a.plan_id=h.plan_id AND a.hop_seq=h.hop_seq)
                         OR (p.state='reserved' AND h.state<>'reserved')
                         OR (p.state='settled' AND h.state<>'settled')
                         OR (p.state='cancelled' AND h.state<>'cancelled')
                         OR EXISTS (
                              SELECT 1 FROM dealer_resale_fulfillment_allocations a
                              WHERE a.plan_id=h.plan_id AND a.hop_seq=h.hop_seq
                                AND ((p.state='reserved' AND a.status<>'reserved')
                                  OR (p.state='settled' AND a.status<>CASE WHEN
                                      p.order_kind='B2B' AND h.hop_seq=(
                                        SELECT MAX(last_hop.hop_seq)
                                        FROM dealer_resale_fulfillment_hops last_hop
                                        WHERE last_hop.plan_id=p.plan_id
                                      ) AND EXISTS (
                                        SELECT 1 FROM dealer_resale_orders refunded_order
                                        WHERE refunded_order.order_id=p.root_order_id
                                          AND refunded_order.state='refunded'
                                      ) THEN 'restored' ELSE 'consumed' END)
                                  OR (p.state='cancelled' AND a.status<>'cancelled'))
                            )
                         OR (p.state='settled' AND
                             (SELECT COUNT(*) FROM dealer_resale_hop_transfer_entries t
                              WHERE t.plan_id=h.plan_id AND t.hop_seq=h.hop_seq)<>2)
                         OR (p.state='settled' AND
                             (SELECT COALESCE(SUM(t.points_delta),0)
                              FROM dealer_resale_hop_transfer_entries t
                              WHERE t.plan_id=h.plan_id AND t.hop_seq=h.hop_seq)<>0)
                         OR (p.state='settled' AND
                             (SELECT COALESCE(MAX(t.points_delta),0)
                              FROM dealer_resale_hop_transfer_entries t
                              WHERE t.plan_id=h.plan_id AND t.hop_seq=h.hop_seq
                                AND t.direction='transfer_in')<>h.points)
                         OR (p.state='settled' AND
                             (SELECT COALESCE(MIN(t.points_delta),0)
                              FROM dealer_resale_hop_transfer_entries t
                              WHERE t.plan_id=h.plan_id AND t.hop_seq=h.hop_seq
                                AND t.direction='transfer_out')<>-h.points)
                         OR (p.state='settled' AND h.acquired_lot_id IS NULL)
                         OR (p.state='settled' AND NOT EXISTS (
                              SELECT 1 FROM dealer_resale_hop_profit_ledger l
                              WHERE l.plan_id=h.plan_id AND l.hop_seq=h.hop_seq
                                AND l.seller_user_id=h.seller_user_id
                                AND l.buyer_user_id=h.buyer_user_id
                                 AND l.seller_cost_basis_cents=h.seller_cost_basis_cents
                                 AND l.sale_amount_cents=h.sale_amount_cents
                                 AND l.principal_recovery_cents=h.principal_recovery_cents
                                 AND l.agent_payable_cents=h.agent_payable_cents
                                 AND l.margin_cents=h.margin_cents
                                 AND (
                                   (l.agent_payable_cents=0 AND l.revenue_ledger_id IS NULL)
                                   OR l.status='pending'
                                   OR l.status='reversed'
                                   OR EXISTS (
                                     SELECT 1 FROM agent_revenue_ledger ar
                                     WHERE ar.id=l.revenue_ledger_id
                                       AND ar.agent_user_id=l.seller_user_id
                                       AND ar.agent_settlement_cents=l.agent_payable_cents
                                       AND ar.agent_margin_before_tax_cents=l.margin_cents
                                   )
                                 )
                                AND CASE WHEN
                                      p.order_kind='B2B' AND h.hop_seq=(
                                        SELECT MAX(last_hop.hop_seq)
                                        FROM dealer_resale_fulfillment_hops last_hop
                                        WHERE last_hop.plan_id=p.plan_id
                                      )
                                      AND EXISTS (
                                        SELECT 1 FROM dealer_resale_orders refunded_order
                                        WHERE refunded_order.order_id=p.root_order_id
                                          AND refunded_order.state='refunded'
                                      ) THEN l.status='reversed'
                                    ELSE l.status IN ('pending','available') END
                            ))
                       )
                     )
               ORDER BY p.root_order_id LIMIT 100"""
        )
        invalid_jit_plans = [str(_row_dict(row).get("root_order_id")) for row in cur.fetchall()]
        if invalid_jit_plans:
            blockers.append("JIT 履约计划/逐跳流水/资金守恒不一致: " + ",".join(invalid_jit_plans))
        cur.execute(
            """SELECT r.id
               FROM recharge_orders r
               LEFT JOIN dealer_resale_fulfillment_plans p ON p.root_order_id=r.id
               WHERE r.pricing_snapshot_jsonb->>'resale_snapshot_version'='2'
                 AND p.plan_id IS NULL ORDER BY r.id LIMIT 100"""
        )
        missing_jit_plans = [str(_row_dict(row).get("id")) for row in cur.fetchall()]
        if missing_jit_plans:
            blockers.append("JIT v2 订单缺持久化履约计划: " + ",".join(missing_jit_plans))
        cur.execute(
            """SELECT * FROM dealer_resale_promotions
               WHERE status IN ('draft','active') ORDER BY campaign_id"""
        )
        for raw in cur.fetchall():
            promotion = _row_dict(raw)
            try:
                _normalize_promotion_eligibility(promotion.get("eligibility_jsonb"))
            except ResaleError as exc:
                blockers.append(f"活动 {promotion.get('campaign_id')} 资格规则无效: {exc.message}")
        cur.execute(
            """SELECT p.campaign_id
               FROM dealer_resale_promotions p
               LEFT JOIN pricing_catalog_versions v
                 ON v.catalog_type='procurement' AND v.scope_key='PLATFORM_BASE'
                AND v.version_code=p.root_catalog_version AND v.status='published'
               LEFT JOIN pricing_catalog_entries e
                 ON e.version_id=v.id AND e.product_code=p.product_code
               WHERE p.status='active' AND e.id IS NULL ORDER BY p.campaign_id"""
        )
        invalid_promotion_catalogs = [
            str(_row_dict(row).get("campaign_id")) for row in cur.fetchall()
        ]
        if invalid_promotion_catalogs:
            blockers.append("生效活动未绑定已发布平台进货目录商品: " + ",".join(invalid_promotion_catalogs))
        cur.execute(
            """SELECT l.lot_id
               FROM dealer_inventory_lots l
               JOIN dealer_resale_promotions p ON p.campaign_id=l.promotion_campaign_id
               WHERE l.promotion_product_code IS DISTINCT FROM p.product_code
                  OR l.promotion_root_catalog_version IS DISTINCT FROM p.root_catalog_version
                  OR l.pricing_version IS DISTINCT FROM p.root_catalog_version
                  OR l.promotion_funding_scope IS DISTINCT FROM p.funding_scope
               ORDER BY l.lot_id LIMIT 100"""
        )
        invalid_promotion_lots = [str(_row_dict(row).get("lot_id")) for row in cur.fetchall()]
        if invalid_promotion_lots:
            blockers.append(
                "活动库存产品/根目录版本血缘错误: " + ",".join(invalid_promotion_lots)
            )
        cur.execute(
            """SELECT p.campaign_id
               FROM dealer_resale_promotions p
               WHERE p.status='active' AND p.funding_scope='PLATFORM'
                 AND p.started_at<=clock_timestamp() AND p.expires_at>clock_timestamp()
                 AND NOT EXISTS (
                    SELECT 1 FROM dealer_inventory_lots l
                    WHERE l.promotion_campaign_id=p.campaign_id AND l.status='active'
                      AND l.promotion_product_code=p.product_code
                      AND l.promotion_root_catalog_version=p.root_catalog_version
                      AND l.pricing_version=p.root_catalog_version
                      AND l.remaining_points>0 AND l.source_kind='manufacturer_origin'
                     AND l.owner_agent_user_id=(
                       SELECT platform_seller_user_id
                       FROM dealer_resale_global_settings WHERE singleton_id=1
                     )
                 ) ORDER BY p.campaign_id"""
        )
        unfunded_platform_promotions = [
            str(_row_dict(row).get("campaign_id")) for row in cur.fetchall()
        ]
        if unfunded_platform_promotions:
            blockers.append(
                "生效中的平台活动缺经审计有限厂家促销库存: "
                + ",".join(unfunded_platform_promotions)
            )
        cur.execute(
            """SELECT DISTINCT a.campaign_id
               FROM dealer_resale_promotions a
               JOIN dealer_resale_promotions b ON a.campaign_id<b.campaign_id
                AND a.status='active' AND b.status='active'
                AND a.product_code=b.product_code
                AND a.root_catalog_version=b.root_catalog_version
                AND a.started_at<b.expires_at AND a.expires_at>b.started_at
                AND (a.funding_scope='PLATFORM' OR b.funding_scope='PLATFORM'
                     OR a.seller_user_id=b.seller_user_id)
               ORDER BY a.campaign_id"""
        )
        overlapping_promotions = [str(_row_dict(row).get("campaign_id")) for row in cur.fetchall()]
        if overlapping_promotions:
            blockers.append("存在可能叠加的生效活动: " + ",".join(overlapping_promotions))
        cur.execute(
            """SELECT s.order_id
               FROM dealer_consumer_sales s
               JOIN recharge_orders r ON r.id=s.order_id
               LEFT JOIN purchase_agreement_acceptances a
                 ON a.acceptance_id=r.pricing_snapshot_jsonb->>'terms_acceptance_id'
                AND a.user_id=s.consumer_user_id
               LEFT JOIN purchase_agreement_acceptance_uses u
                 ON u.acceptance_id=a.acceptance_id
                AND u.purchase_kind='customer-recharge'
                AND u.purchase_ref_id=r.id
               WHERE COALESCE(r.actual_payment_channel,'') NOT IN
                     ('wechat_jsapi','wechat_native','xunhupay','manual_bank')
                   OR a.acceptance_id IS NULL
                   OR a.agreement_type<>'user_terms'
                   OR a.surface<>'customer-recharge'
                   OR u.acceptance_id IS NULL
                   OR COALESCE(BTRIM(r.pricing_snapshot_jsonb->>'terms_version'),'')=''
                   OR COALESCE(BTRIM(r.pricing_snapshot_jsonb->>'terms_content_hash'),'')=''
                   OR a.agreement_version IS DISTINCT FROM
                      r.pricing_snapshot_jsonb->>'terms_version'
                   OR a.content_hash IS DISTINCT FROM
                      r.pricing_snapshot_jsonb->>'terms_content_hash'""",
        )
        invalid_consumer_evidence = [str(_row_dict(row).get("order_id")) for row in cur.fetchall()]
        if invalid_consumer_evidence:
            blockers.append("消费者订单缺购买协议快照或原支付路径证据: " + ",".join(invalid_consumer_evidence))
        from services.agent_agreement import CURRENT_CONTENT_HASH, CURRENT_VERSION
        cur.execute(
            """WITH target_services AS (
                 SELECT seller_user_id AS user_id FROM dealer_resale_policies
                 UNION SELECT seller_user_id FROM dealer_consumer_sales
               )
               SELECT t.user_id
               FROM target_services t
               LEFT JOIN agent_factory_agreements a
                 ON a.agent_user_id=t.user_id AND a.version=%s
                AND a.status='signed' AND a.content_hash=%s
               WHERE a.id IS NULL ORDER BY t.user_id""",
            (CURRENT_VERSION, CURRENT_CONTENT_HASH),
        )
        unsigned_services = [str(_row_dict(row).get("user_id")) for row in cur.fetchall()]
        if unsigned_services:
            blockers.append("目标服务商尚未签署正文哈希匹配的 v2.4 协议: " + ",".join(unsigned_services))
        cur.execute(
            """SELECT cash_job_id FROM service_refund_cash_jobs
               WHERE (status='completed' AND (
                        provider_refund_id IS NULL
                        OR provider_evidence_jsonb->>'provider_refund_id'<>provider_refund_id
                        OR provider_evidence_jsonb->>'source'<>'recharge_orders.settlement_snapshot_jsonb'
                        OR provider_evidence_jsonb->>'refund_evidence_kind' NOT IN
                           ('signed_wechat_callback','wechat_refund_query',
                            'signed_xunhupay_callback','signed_xunhupay_refund_response')
                        OR (provider_evidence_jsonb->>'refund_evidence_kind'='signed_xunhupay_callback'
                            AND COALESCE(provider_evidence_jsonb->>'provider_refund_reference_kind','')<>
                                'signed_xunhupay_refund_response')
                        OR COALESCE(provider_evidence_jsonb->>'refunded_amount_cents','') !~ '^[0-9]+$'
                        OR (provider_evidence_jsonb->>'refunded_amount_cents')::bigint<>amount_cents
                      ))
                  OR (status<>'completed' AND original_payment_route NOT IN
                      ('wechat','wechat_jsapi','wechat_native','xunhupay','manual_bank','unknown'))
               ORDER BY cash_job_id LIMIT 100"""
        )
        invalid_cash_jobs = [str(_row_dict(row).get("cash_job_id")) for row in cur.fetchall()]
        if invalid_cash_jobs:
            blockers.append("退款现金工单缺有效支付路径或完成证据: " + ",".join(invalid_cash_jobs))
        cur.execute(
            """SELECT f.refund_id
               FROM dealer_resale_refunds f
               JOIN dealer_resale_orders o ON o.order_id=f.order_id
               WHERE f.refund_amount_cents <= 0
                  OR f.refund_amount_cents <> o.sale_amount_cents-f.processing_cost_cents
               ORDER BY f.refund_id LIMIT 100"""
        )
        invalid_b2b_refund_amounts = [
            str(_row_dict(row).get("refund_id")) for row in cur.fetchall()
        ]
        if invalid_b2b_refund_amounts:
            blockers.append(
                "B2B 退款缺持久金额或与批准处理成本不一致: "
                + ",".join(invalid_b2b_refund_amounts)
            )
        cur.execute(
            """SELECT cash_job_id,status,provider_refund_id FROM service_refund_cash_jobs
               WHERE status='manual_review'
                  OR (status='provider_processing'
                      AND updated_at < clock_timestamp()-INTERVAL '10 minutes')
                  OR (status='queued'
                      AND created_at < clock_timestamp()-INTERVAL '15 minutes')
                  OR status='failed'
               ORDER BY created_at LIMIT 100"""
        )
        cash_attention_rows = [_row_dict(row) for row in cur.fetchall()]
        cash_attention = [
            f"{row.get('cash_job_id')}:{row.get('status')}" for row in cash_attention_rows
        ]
        externally_succeeded_pending = [
            f"{row.get('cash_job_id')}:{row.get('status')}"
            for row in cash_attention_rows
            if row.get("status") == "manual_review" and row.get("provider_refund_id")
        ]
        if externally_succeeded_pending:
            blockers.append(
                "存在外部现金成功但内部终态待补偿的退款工单: "
                + ",".join(externally_succeeded_pending)
            )
        other_cash_attention = [
            item for item in cash_attention if item not in externally_succeeded_pending
        ]
        if other_cash_attention:
            warnings.append("存在需执行、重试或主动查询的退款现金工单: " + ",".join(other_cash_attention))
        cur.execute(
            "SELECT COUNT(*) AS c FROM service_refund_funding_work_orders WHERE status='open'"
        )
        open_funding = int(_row_dict(cur.fetchone()).get("c") or 0)
        if open_funding:
            warnings.append(f"存在 {open_funding} 个服务商退款资金缺口工单；不阻断已成立客户退款")
        cur.execute(
            """SELECT o.order_id
               FROM dealer_resale_orders o
               LEFT JOIN price_quotes q ON q.quote_id=o.quote_id
               WHERE q.quote_id IS NULL
                  OR q.buyer_user_id<>o.buyer_user_id
                  OR q.final_price_cents<>o.sale_amount_cents
                  OR q.catalog_version<>o.pricing_version"""
        )
        quote_mismatch = [str(_row_dict(row).get("order_id")) for row in cur.fetchall()]
        if quote_mismatch:
            blockers.append("逐级转售订单/持久化报价覆盖不一致: " + ",".join(quote_mismatch))
        cur.execute(
            """SELECT s.order_id FROM dealer_consumer_sales s
               LEFT JOIN price_quotes q ON q.quote_id=s.quote_id
               WHERE q.quote_id IS NULL OR q.buyer_user_id<>s.consumer_user_id
                  OR q.final_price_cents<>s.sale_amount_cents
                  OR q.catalog_version<>s.pricing_version"""
        )
        consumer_quote_mismatch = [str(_row_dict(row).get("order_id")) for row in cur.fetchall()]
        if consumer_quote_mismatch:
            blockers.append("消费者销售订单/持久化报价覆盖不一致: " + ",".join(consumer_quote_mismatch))
        cur.execute(
            """SELECT o.order_id
               FROM dealer_resale_orders o
               LEFT JOIN dealer_inventory_transfers t ON t.order_id=o.order_id
               LEFT JOIN dealer_inventory_transfer_entries e ON e.transfer_id=t.transfer_id
               WHERE o.state IN ('paid','refund_pending','refunded')
               GROUP BY o.order_id, o.state, t.transfer_id
               HAVING t.transfer_id IS NULL
                   OR COUNT(e.entry_id) < CASE WHEN o.state='refunded' THEN 4 ELSE 2 END"""
        )
        transfer_mismatch = [str(_row_dict(row).get("order_id")) for row in cur.fetchall()]
        if transfer_mismatch:
            blockers.append("已支付订单双向库存流水不完整: " + ",".join(transfer_mismatch))
        cur.execute(
            """SELECT s.order_id
               FROM dealer_consumer_sales s
               LEFT JOIN dealer_consumer_transfer_entries e ON e.order_id=s.order_id
               WHERE s.state IN ('paid','refund_pending','refunded','manual_review')
               GROUP BY s.order_id,s.state
               HAVING COUNT(e.entry_id) < CASE WHEN s.state='refunded' THEN 4 ELSE 2 END"""
        )
        consumer_transfer_mismatch = [str(_row_dict(row).get("order_id")) for row in cur.fetchall()]
        if consumer_transfer_mismatch:
            blockers.append("消费者销售双向流水不完整: " + ",".join(consumer_transfer_mismatch))
    else:
        relationship_check = {"ready": False, "blockers": ["逐级转售 schema 未就绪"]}
        pricing_flags = None
    try:
        flag = resale_flag_enabled(cur)
    except Exception as exc:  # noqa: BLE001
        flag = None
        blockers.append(f"独立 flag 无法权威读取: {exc}")
    return {
        "ready": not blockers,
        "flag_enabled": flag,
        "schema": schema,
        "channel_relationships": relationship_check,
        "pricing_flags": pricing_flags,
        "cache_contract": "PostgreSQL authoritative; Redis is not used for flag, lots, money or maturity",
        "blockers": blockers,
        "warnings": warnings,
    }


def _serialize_order(
    cur, row: Dict[str, Any], *, actor_user_id: int, is_admin: bool,
) -> Dict[str, Any]:
    data = {
        "order_id": str(row["order_id"]),
        "points": int(row["points"]),
        "sale_amount_cents": int(row["sale_amount_cents"]),
        "pricing_version": str(row["pricing_version"]),
        "quote_id": str(row["quote_id"]),
        "payment_collector": "PLATFORM",
        "state": str(row["state"]),
        "paid_at": row.get("paid_at"),
        "refund_deadline": row.get("refund_deadline"),
    }
    if is_admin:
        data["seller_user_id"] = int(row["seller_user_id"])
        data["buyer_user_id"] = int(row["buyer_user_id"])
        data["refund_responsible_user_id"] = int(row["refund_responsible_user_id"])
    else:
        is_seller = int(actor_user_id) == int(row["seller_user_id"])
        data["direction"] = "sale" if is_seller else "purchase"
        data["refund_handling"] = "PLATFORM_MANAGED"
    if is_admin:
        data.update({
            "seller_cost_basis_cents": int(row["seller_cost_basis_cents"]),
            "margin_cents": int(row["margin_cents"]),
            "downstream_markup_bps": int(row["downstream_markup_bps"]),
        })
        data["seller_lot_allocations"] = _json_array(row.get("seller_lot_allocations"))
        data["relationship_version"] = row.get("relationship_version")
        data["source_kind"] = row.get("source_kind")
    return data


def list_orders(
    cur, *, actor_user_id: int, is_admin: bool = False, limit: int = 100, offset: int = 0,
) -> Dict[str, Any]:
    where = "TRUE" if is_admin else "(seller_user_id=%s OR buyer_user_id=%s)"
    params: List[Any] = [] if is_admin else [int(actor_user_id), int(actor_user_id)]
    cur.execute(f"SELECT COUNT(*) AS c FROM dealer_resale_orders WHERE {where}", tuple(params))
    total = int(_row_dict(cur.fetchone()).get("c") or 0)
    cur.execute(
        f"SELECT * FROM dealer_resale_orders WHERE {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
        tuple(params + [max(1, min(int(limit), 500)), max(0, int(offset))]),
    )
    items = [
        _serialize_order(cur, _row_dict(row), actor_user_id=actor_user_id, is_admin=is_admin)
        for row in cur.fetchall()
    ]
    return {"items": items, "total": total}


def get_order_visible(
    cur, *, order_id: str, actor_user_id: int, is_admin: bool = False,
) -> Dict[str, Any]:
    cur.execute("SELECT * FROM dealer_resale_orders WHERE order_id=%s", (str(order_id),))
    row = cur.fetchone()
    if not row:
        raise ResaleError("RESALE_ORDER_NOT_FOUND", "逐级转售订单不存在")
    order = _row_dict(row)
    if not is_admin and int(actor_user_id) not in {
        int(order["seller_user_id"]), int(order["buyer_user_id"])
    }:
        raise ResaleError("FORBIDDEN", "无权查看该逐级转售订单")
    return _serialize_order(cur, order, actor_user_id=actor_user_id, is_admin=is_admin)


def admin_order_chain(cur, order_id: str) -> Dict[str, Any]:
    jit_plan: Optional[Dict[str, Any]] = None
    jit_hops: List[Dict[str, Any]] = []
    jit_consumer_leg: Dict[str, Any] = {}
    cur.execute(
        "SELECT * FROM dealer_resale_fulfillment_plans WHERE root_order_id=%s",
        (str(order_id),),
    )
    raw_plan = cur.fetchone()
    if raw_plan:
        plan = _row_dict(raw_plan)
        plan["promotion_snapshot_jsonb"] = _json_object(
            plan.get("promotion_snapshot_jsonb")
        )
        plan["immutable_snapshot_jsonb"] = _json_object(
            plan.get("immutable_snapshot_jsonb")
        )
        plan["funds_conservation_jsonb"] = _json_object(
            plan.get("funds_conservation_jsonb")
        )
        cur.execute(
            "SELECT * FROM dealer_resale_fulfillment_hops "
            "WHERE plan_id=%s ORDER BY hop_seq",
            (str(plan["plan_id"]),),
        )
        hops: List[Dict[str, Any]] = []
        for raw_hop in cur.fetchall():
            hop = _row_dict(raw_hop)
            hop["promotion_snapshot_jsonb"] = _json_object(
                hop.get("promotion_snapshot_jsonb")
            )
            cur.execute(
                "SELECT * FROM dealer_resale_fulfillment_allocations "
                "WHERE plan_id=%s AND hop_seq=%s ORDER BY allocation_seq",
                (str(plan["plan_id"]), int(hop["hop_seq"])),
            )
            hop["allocations"] = [_row_dict(row) for row in cur.fetchall()]
            cur.execute(
                "SELECT * FROM dealer_resale_hop_transfer_entries "
                "WHERE plan_id=%s AND hop_seq=%s ORDER BY entry_seq",
                (str(plan["plan_id"]), int(hop["hop_seq"])),
            )
            hop["transfer_entries"] = [_row_dict(row) for row in cur.fetchall()]
            cur.execute(
                "SELECT * FROM dealer_resale_hop_profit_ledger "
                "WHERE plan_id=%s AND hop_seq=%s",
                (str(plan["plan_id"]), int(hop["hop_seq"])),
            )
            hop["profit"] = _row_dict(cur.fetchone())
            hops.append(hop)
        jit_plan = plan
        jit_hops = hops
        jit_consumer_leg = _json_object(
            plan["immutable_snapshot_jsonb"].get("consumer_leg")
        )

    seen = set()
    pending = [str(order_id)]
    rows: List[Dict[str, Any]] = []
    while pending and len(seen) < 100:
        current = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        cur.execute("SELECT * FROM dealer_resale_orders WHERE order_id=%s", (current,))
        row = cur.fetchone()
        if not row:
            continue
        order = _row_dict(row)
        rows.append(_serialize_order(cur, order, actor_user_id=0, is_admin=True))
        allocations = _json_array(order.get("seller_lot_allocations"))
        if allocations:
            cur.execute(
                "SELECT source_order_id FROM dealer_inventory_lots "
                "WHERE lot_id=ANY(%s) AND source_order_id IS NOT NULL",
                ([str(item["lot_id"]) for item in allocations],),
            )
            pending.extend(
                str(_row_dict(item).get("source_order_id")) for item in cur.fetchall()
            )
        cur.execute(
            """SELECT DISTINCT a.order_id
               FROM dealer_inventory_lots l
               JOIN dealer_inventory_lot_allocations a ON a.seller_lot_id=l.lot_id
               WHERE l.source_order_id=%s""",
            (current,),
        )
        pending.extend(str(_row_dict(item).get("order_id")) for item in cur.fetchall())
    rows.sort(key=lambda item: (str(item.get("paid_at") or ""), item["order_id"]))
    if jit_plan is not None:
        return {
            "root_order_id": str(order_id),
            "plan": jit_plan,
            "hops": jit_hops,
            "consumer_leg": jit_consumer_leg,
            "orders": rows,
            "count": max(len(rows), len(jit_hops), 1 if jit_consumer_leg else 0),
        }
    return {"root_order_id": str(order_id), "orders": rows, "count": len(rows)}


def request_consumer_refund_case(
    cur, *, order_id: str, consumer_user_id: int, reason: str,
    reason_category: str = "negotiated_other", evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    _order_xact_lock(cur, str(order_id))
    cur.execute("SELECT * FROM dealer_consumer_sales WHERE order_id=%s FOR UPDATE", (str(order_id),))
    sale = _row_dict(cur.fetchone())
    if not sale:
        raise ResaleError("CONSUMER_SALE_NOT_FOUND", "消费者订单不存在")
    if int(sale["consumer_user_id"]) != int(consumer_user_id):
        raise ResaleError("FORBIDDEN", "只能申请自己的消费者订单退款")
    if sale["state"] not in {"paid", "manual_review", "refund_pending"}:
        raise ResaleError("CONSUMER_REFUND_STATE_INVALID", "该消费者订单当前不能申请退款")
    category = str(reason_category or "negotiated_other").strip().lower()
    allowed_categories = MANDATORY_CONSUMER_REFUND_REASONS | NEGOTIATED_CONSUMER_REFUND_REASONS
    if category not in allowed_categories:
        raise ResaleError("CONSUMER_REFUND_REASON_INVALID", "退款原因类别不合法")
    cur.execute("SELECT * FROM consumer_refund_cases WHERE source_order_id=%s FOR UPDATE", (str(order_id),))
    existing = cur.fetchone()
    if existing:
        return _row_dict(existing)
    cur.execute(
        """SELECT paid_at,base_points,bonus_points,amount_cents,payment_status,
                  NOW() AS db_now
           FROM recharge_orders WHERE id=%s FOR UPDATE""",
        (str(order_id),),
    )
    recharge = _row_dict(cur.fetchone())
    if not recharge or recharge.get("payment_status") != "paid" or not recharge.get("paid_at"):
        raise ResaleError("CONSUMER_REFUND_ORDER_UNPAID", "订单未支付，不能申请退款")
    seven_day_eligible = False
    prior_same_service_orders = 0
    order_unspent = {"tool_unspent": 0, "publish_unspent": 0, "bonus_unspent": 0}
    if category == "statutory_seven_day":
        from services.customer_credit import compute_unspent_from_order

        order_unspent = compute_unspent_from_order(
            cur, int(consumer_user_id), str(order_id)
        )
        fully_unspent = sum(int(value or 0) for value in order_unspent.values()) >= (
            int(recharge.get("base_points") or 0) + int(recharge.get("bonus_points") or 0)
        )
        cur.execute(
            """SELECT COUNT(*) AS c
               FROM dealer_consumer_sales prior
               WHERE prior.consumer_user_id=%s AND prior.seller_user_id=%s
                 AND prior.state IN ('paid','refund_pending','refunded','manual_review')
                 AND prior.paid_at < %s AND prior.order_id<>%s""",
            (
                int(consumer_user_id), int(sale["seller_user_id"]), recharge["paid_at"],
                str(order_id),
            ),
        )
        prior_same_service_orders = int(_row_dict(cur.fetchone()).get("c") or 0)
        cur.execute("SELECT %s + INTERVAL '7 days' >= NOW() AS eligible", (recharge["paid_at"],))
        seven_day_eligible = (
            bool(_row_dict(cur.fetchone()).get("eligible"))
            and prior_same_service_orders == 0
            and fully_unspent
        )
    is_mandatory_claim = category in MANDATORY_CONSUMER_REFUND_REASONS
    is_machine_verified = category == "statutory_seven_day" and seven_day_eligible
    decision_kind = "mandatory" if is_mandatory_claim else "negotiated"
    mandatory_evidence_status = (
        "verified" if is_machine_verified
        else "claimed" if is_mandatory_claim
        else "not_applicable"
    )
    initial_status = "platform_execution" if is_machine_verified else (
        "manual_review" if is_mandatory_claim else "requested"
    )
    eligibility = {
        "reason_category": category,
        "seven_day_eligible": seven_day_eligible,
        "prior_same_service_orders": prior_same_service_orders,
        "order_unspent": order_unspent,
        "b2b_72h_rule_applied": False,
        "fixed_consumer_fee_bps": 0,
        "submitted_evidence": dict(evidence or {}),
    }
    case_id = _stable_id("CRF", str(order_id))
    cur.execute(
        """INSERT INTO consumer_refund_cases
           (case_id,consumer_user_id,responsible_service_user_id,source_order_id,
            policy_code,digital_goods_acknowledged_at,status,reason,reason_category,
            decision_kind,mandatory_evidence_status,eligibility_jsonb,approved_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,
                   CASE WHEN %s='verified' THEN NOW() ELSE NULL END)
           RETURNING *""",
        (
            case_id, int(consumer_user_id), int(sale["seller_user_id"]), str(order_id),
            str(sale["digital_goods_policy_code"]), sale["digital_goods_acknowledged_at"],
            initial_status, str(reason or ""), category, decision_kind,
            mandatory_evidence_status,
            json.dumps(eligibility, ensure_ascii=False, separators=(",", ":")),
            mandatory_evidence_status,
        ),
    )
    result = _row_dict(cur.fetchone())
    cur.execute(
        "UPDATE dealer_consumer_sales SET state='refund_pending',updated_at=NOW() WHERE order_id=%s",
        (str(order_id),),
    )
    return result


def review_consumer_refund_case(
    cur, *, case_id: str, actor_user_id: int, approve: bool, note: str,
    is_admin: bool = False,
) -> Dict[str, Any]:
    cur.execute("SELECT * FROM consumer_refund_cases WHERE case_id=%s FOR UPDATE", (str(case_id),))
    case = _row_dict(cur.fetchone())
    if not case:
        raise ResaleError("CONSUMER_REFUND_NOT_FOUND", "消费者退款申请不存在")
    if not is_admin and int(case["responsible_service_user_id"]) != int(actor_user_id):
        raise ResaleError("FORBIDDEN", "只有本单直属服务方可审核")
    if case.get("decision_kind") == "mandatory":
        evidence_status = str(case.get("mandatory_evidence_status") or "claimed")
        if not is_admin:
            if not approve:
                raise ResaleError(
                    "MANDATORY_REFUND_CANNOT_BE_REJECTED",
                    "法定退款、重复扣款、未到账、未交付或系统故障不得由服务方拒绝",
                )
            raise ResaleError(
                "MANDATORY_REFUND_PLATFORM_REVIEW_REQUIRED",
                "强制退款证据由平台核验，直属服务方无需也无权改变法定判定",
            )
        if case["status"] in {"completed", "rejected"}:
            return case
        target = "platform_execution" if approve else "rejected"
        target_evidence = "verified" if approve else "rejected"
        cur.execute(
            """UPDATE consumer_refund_cases
               SET status=%s,mandatory_evidence_status=%s,
                   reviewed_by_user_id=%s,reviewed_at=NOW(),
                   approved_at=CASE WHEN %s THEN NOW() ELSE approved_at END,
                   execution_jsonb=execution_jsonb || %s::jsonb,
                   row_version=row_version+1,updated_at=NOW()
               WHERE case_id=%s RETURNING *""",
            (
                target, target_evidence, int(actor_user_id), bool(approve),
                json.dumps({
                    "mandatory_evidence_review": target_evidence,
                    "reviewed_by_role": "admin",
                    "previous_evidence_status": evidence_status,
                    "note": str(note or ""),
                    "cash_or_inventory_moved": False,
                }, ensure_ascii=False, separators=(",", ":")),
                str(case_id),
            ),
        )
        result = _row_dict(cur.fetchone())
        if not approve:
            cur.execute(
                """UPDATE dealer_consumer_sales SET state='paid',updated_at=NOW()
                   WHERE order_id=%s AND state='refund_pending'""",
                (str(case["source_order_id"]),),
            )
        return result
    if case["status"] in {"completed", "rejected"}:
        return case
    if case["status"] not in {"requested", "service_review", "manual_review"}:
        raise ResaleError("CONSUMER_REFUND_STATE_INVALID", "消费者退款申请状态已变化")
    target = "platform_execution" if approve else "rejected"
    execution = {
        "reviewed_by_role": "admin" if is_admin else "responsible_service",
        "reviewed_by_user_id": int(actor_user_id),
        "note": str(note or ""), "cash_or_inventory_moved": False,
    }
    cur.execute(
        """UPDATE consumer_refund_cases
           SET status=%s,execution_jsonb=execution_jsonb || %s::jsonb,
               reviewed_by_user_id=%s,reviewed_at=NOW(),
               approved_at=CASE WHEN %s THEN NOW() ELSE approved_at END,
               row_version=row_version+1,updated_at=NOW()
           WHERE case_id=%s RETURNING *""",
        (
            target, json.dumps(execution, ensure_ascii=False, separators=(",", ":")),
            int(actor_user_id), bool(approve), str(case_id),
        ),
    )
    result = _row_dict(cur.fetchone())
    if not approve:
        cur.execute(
            """UPDATE dealer_consumer_sales SET state='paid',updated_at=NOW()
               WHERE order_id=%s AND state='refund_pending'""",
            (str(case["source_order_id"]),),
        )
    return result


def _restore_consumer_inventory_partial(
    cur, *, case: Dict[str, Any], sale: Dict[str, Any], points_to_restore: int,
) -> Dict[str, int]:
    """Restore exactly the credits actually recovered, preserving source-lot cost."""
    if points_to_restore <= 0:
        return {"points": 0, "cost_basis_cents": 0}
    cur.execute(
        """SELECT * FROM dealer_consumer_lot_allocations
           WHERE order_id=%s ORDER BY allocation_seq FOR UPDATE""",
        (str(sale["order_id"]),),
    )
    allocations = [_row_dict(row) for row in cur.fetchall()]
    remaining = int(points_to_restore)
    restored_cost = 0
    for allocation in allocations:
        if remaining <= 0:
            break
        available = int(allocation["points"]) - int(allocation.get("refunded_points") or 0)
        if available <= 0:
            continue
        take = min(remaining, available)
        old_refunded_points = int(allocation.get("refunded_points") or 0)
        old_refunded_cost = int(allocation.get("refunded_cost_cents") or 0)
        new_refunded_points = old_refunded_points + take
        if new_refunded_points == int(allocation["points"]):
            cumulative_cost = int(allocation["cost_basis_cents"])
        else:
            cumulative_cost = (
                int(allocation["cost_basis_cents"]) * new_refunded_points
            ) // int(allocation["points"])
        cost = cumulative_cost - old_refunded_cost
        cur.execute(
            """UPDATE dealer_consumer_lot_allocations
               SET refunded_points=refunded_points+%s,
                   refunded_cost_cents=refunded_cost_cents+%s,
                   status=CASE WHEN refunded_points+%s=points THEN 'restored' ELSE status END,
                   restored_at=CASE WHEN refunded_points+%s=points THEN NOW() ELSE restored_at END
               WHERE allocation_id=%s AND refunded_points+%s<=points""",
            (take, cost, take, take, int(allocation["allocation_id"]), take),
        )
        if cur.rowcount != 1:
            raise ResaleError("CONSUMER_REFUND_ALLOCATION_RACE", "退款批次并发变化，已拒绝重复恢复")
        cur.execute(
            """UPDATE dealer_inventory_lots
               SET remaining_points=remaining_points+%s,
                   remaining_cost_cents=remaining_cost_cents+%s,
                   status='active',updated_at=NOW()
               WHERE lot_id=%s
                 AND remaining_points+reserved_points+%s<=original_points
                 AND remaining_cost_cents+reserved_cost_cents+%s<=acquisition_cost_cents""",
            (take, cost, str(allocation["seller_lot_id"]), take, cost),
        )
        if cur.rowcount != 1:
            raise ResaleError("CONSUMER_REFUND_LOT_CONSERVATION", "原库存批次无法证明守恒")
        cur.execute(
            """INSERT INTO consumer_refund_lot_restorations
               (case_id,source_order_id,seller_lot_id,allocation_id,points,cost_basis_cents)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT(case_id,allocation_id) DO NOTHING""",
            (
                str(case["case_id"]), str(sale["order_id"]), str(allocation["seller_lot_id"]),
                int(allocation["allocation_id"]), take, cost,
            ),
        )
        remaining -= take
        restored_cost += cost
    seller_id = int(sale["seller_user_id"])
    if remaining:
        cur.execute(
            "SELECT plan_id FROM dealer_resale_fulfillment_plans WHERE root_order_id=%s FOR UPDATE",
            (str(sale["order_id"]),),
        )
        if not cur.fetchone():
            raise ResaleError(
                "CONSUMER_REFUND_INVENTORY_UNPROVEN",
                "回收算力无法映射到直属服务方原批次",
            )
        target_total_cost = (
            int(sale["seller_cost_basis_cents"]) * int(points_to_restore)
        ) // int(sale["points"])
        jit_cost = target_total_cost - restored_cost
        if jit_cost <= 0:
            raise ResaleError("CONSUMER_REFUND_INVENTORY_UNPROVEN", "JIT 回收批次成本无法证明")
        lot_id = _stable_id("JCR", str(case["case_id"]))
        cur.execute(
            """INSERT INTO dealer_inventory_lots
               (lot_id,owner_agent_user_id,original_points,remaining_points,reserved_points,
                acquisition_cost_cents,remaining_cost_cents,reserved_cost_cents,acquired_at,
                status,source_kind,pricing_version,quote_id,evidence_jsonb,standard_reference_cents)
               VALUES (%s,%s,%s,%s,0,%s,%s,0,NOW(),'active','direct_resale',%s,%s,%s::jsonb,%s)""",
            (
                lot_id, seller_id, remaining, remaining, jit_cost, jit_cost,
                str(sale["pricing_version"]), str(sale["quote_id"]),
                _canonical_plan({
                    "consumer_refund_case_id": str(case["case_id"]),
                    "source_order_id": str(sale["order_id"]),
                    "scope": "direct_service_only", "ancestor_hops_touched": 0,
                }), int(sale["sale_amount_cents"]),
            ),
        )
        cur.execute(
            """INSERT INTO dealer_consumer_jit_refund_lots
               (case_id,source_order_id,seller_user_id,restored_lot_id,points,cost_basis_cents)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (
                str(case["case_id"]), str(sale["order_id"]), seller_id,
                lot_id, remaining, jit_cost,
            ),
        )
        restored_cost += jit_cost
        remaining = 0
    cur.execute(
        """UPDATE agent_inventory_wallets
           SET paid_inventory_points=paid_inventory_points+%s,
               total_allocated_points=GREATEST(0,total_allocated_points-%s),updated_at=NOW()
           WHERE agent_user_id=%s
           RETURNING paid_inventory_points,bonus_inventory_points""",
        (points_to_restore, points_to_restore, seller_id),
    )
    seller_after = _row_dict(cur.fetchone())
    if not seller_after:
        raise ResaleError("CONSUMER_REFUND_SELLER_WALLET_MISSING", "直属服务方库存钱包不存在")
    transfer_id = str(sale["transfer_id"])
    cur.execute(
        """INSERT INTO dealer_consumer_transfer_entries
           (transfer_id,order_id,seller_user_id,consumer_user_id,direction,points_delta,cost_basis_cents)
           VALUES (%s,%s,%s,%s,'consumer_refund_out',%s,%s),
                  (%s,%s,%s,%s,'seller_refund_in',%s,%s)
           ON CONFLICT(transfer_id,direction) DO NOTHING""",
        (
            transfer_id, str(sale["order_id"]), seller_id, int(sale["consumer_user_id"]),
            -points_to_restore, restored_cost, transfer_id,
            str(sale["order_id"]), seller_id, int(sale["consumer_user_id"]),
            points_to_restore, restored_cost,
        ),
    )
    _insert_legacy_inventory_tx(
        cur, user_id=seller_id, type_="consumer_refund_in", points=points_to_restore,
        paid_after=int(seller_after["paid_inventory_points"]),
        bonus_after=int(seller_after["bonus_inventory_points"]),
        order_id=str(sale["order_id"]), transfer_id=transfer_id, lot_id=None,
        counterparty_id=int(sale["consumer_user_id"]), cost_basis_cents=restored_cost,
        description="消费者退款恢复原履约库存（按实际回收算力）",
    )
    return {"points": points_to_restore, "cost_basis_cents": restored_cost}


def _reverse_direct_service_revenue(
    cur, *, sale: Dict[str, Any], refund_amount_cents: int,
) -> int:
    margin = int(sale["margin_cents"])
    payable = int(sale.get("agent_payable_cents") or margin)
    principal = int(sale.get("principal_recovery_cents") or 0)
    if payable <= 0 or refund_amount_cents <= 0:
        return 0
    reverse_cents = min(
        payable,
        (payable * int(refund_amount_cents) + int(sale["sale_amount_cents"]) - 1)
        // int(sale["sale_amount_cents"]),
    )
    margin_reverse = min(
        margin,
        (margin * int(refund_amount_cents) + int(sale["sale_amount_cents"]) - 1)
        // int(sale["sale_amount_cents"]),
    )
    cur.execute(
        "SELECT * FROM agent_revenue_ledger WHERE id=%s FOR UPDATE",
        (int(sale["revenue_ledger_id"]),),
    )
    revenue = _row_dict(cur.fetchone())
    if not revenue or reverse_cents <= 0:
        return 0
    if revenue.get("status") == "frozen":
        if reverse_cents == payable:
            cur.execute(
                """UPDATE agent_revenue_ledger
                   SET status='cancelled',reversed_at=NOW(),note=COALESCE(note,'') || %s
                   WHERE id=%s AND status='frozen' AND reversed_at IS NULL""",
                (" | 直属消费者退款全额冲销", int(revenue["id"])),
            )
        else:
            residual_payable = payable - reverse_cents
            residual_margin = margin - margin_reverse
            residual_principal = max(0, principal - (reverse_cents - margin_reverse))
            cur.execute(
                """UPDATE agent_revenue_ledger
                   SET agent_margin_before_tax_cents=%s,
                       agent_settlement_cents=%s,
                       note=COALESCE(note,'') || %s
                   WHERE id=%s AND status='frozen' AND reversed_at IS NULL
                      AND agent_settlement_cents=%s""",
                (
                    residual_margin, residual_payable,
                    (
                        f" | 直属消费者部分退款后保留本金 {residual_principal} 分、"
                        f"利润 {residual_margin} 分"
                    ),
                    int(revenue["id"]), payable,
                ),
            )
            if cur.rowcount != 1:
                raise ResaleError("CONSUMER_REFUND_REVENUE_RACE", "服务方待结算利润并发变化，拒绝冲销")
    else:
        from services.agent_revenue import insert_revenue_clawback
        clawback_id = insert_revenue_clawback(
            cur, agent_user_id=int(sale["seller_user_id"]),
            original_ledger_id=int(revenue["id"]), clawback_amount_cents=reverse_cents,
            note=f"直属消费者退款冲销 · order={sale['order_id']}",
        )
        if reverse_cents == payable:
            cur.execute(
                "UPDATE agent_revenue_ledger SET reversed_at=NOW(),reversed_by_ledger_id=%s WHERE id=%s",
                (clawback_id, int(revenue["id"])),
            )
    return reverse_cents


def settle_consumer_refund_case(cur, *, case_id: str) -> Dict[str, Any]:
    """Atomically reverse the approved direct sale and queue original-route cash refund."""
    cur.execute("SELECT * FROM consumer_refund_cases WHERE case_id=%s FOR UPDATE", (str(case_id),))
    case = _row_dict(cur.fetchone())
    if not case:
        raise ResaleError("CONSUMER_REFUND_NOT_FOUND", "消费者退款申请不存在")
    _order_xact_lock(cur, str(case["source_order_id"]))
    if case.get("internal_settled_at"):
        return case
    if case["status"] != "platform_execution":
        raise ResaleError("CONSUMER_REFUND_APPROVAL_REQUIRED", "协商退款须通过本单售后审核后才能改变资金")
    if (
        case.get("decision_kind") == "mandatory"
        and case.get("mandatory_evidence_status") != "verified"
    ):
        raise ResaleError("CONSUMER_REFUND_EVIDENCE_UNVERIFIED", "强制退款证据尚未由平台核验")
    cur.execute(
        "SELECT * FROM dealer_consumer_sales WHERE order_id=%s FOR UPDATE",
        (str(case["source_order_id"]),),
    )
    sale = _row_dict(cur.fetchone())
    cur.execute(
        """SELECT id,amount_cents,base_points,bonus_points,payment_method,actual_payment_channel,payment_status,
                  paid_at,refund_status
           FROM recharge_orders WHERE id=%s FOR UPDATE""",
        (str(case["source_order_id"]),),
    )
    recharge = _row_dict(cur.fetchone())
    if not sale or not recharge or recharge.get("payment_status") != "paid":
        raise ResaleError("CONSUMER_REFUND_ORDER_INVALID", "原订单不可退款")
    # [单账本收敛 2026-07-27] 消费者退款时回收未消费额度。
    # 原来从信用钱包三池 revoke,并用 compute_unspent_from_order 按订单 FIFO 算未消费量。
    # 单账本后额度在客户 user_wallets 里,与客户自充值的钱混在一起,没有等价的按订单 FIFO ——
    # 改为按【当前余额】夹紧回收(revoke_from_customer 内部保证不扣成负数)。
    # ⚠️ 口径变化:原来只回收"这一单还没花的",现在回收上限是客户当前总余额。
    #    退款金额本身由下面的 force_full_cash / base_points 逻辑决定,不受影响;
    #    这里只影响"库存回收多少"。若 Owner 要求严格按单 FIFO,需要另设计订单级台账。
    from services.customer_entitlement import revoke_from_customer as _revoke_customer_wallet
    _order_points = int(recharge.get("base_points") or 0)
    _order_bonus = int(recharge.get("bonus_points") or 0)
    revoked = _revoke_customer_wallet(
        cur, customer_user_id=int(sale["consumer_user_id"]),
        paid_points=_order_points,
        bonus_points=_order_bonus,
        related_order_id=str(sale["order_id"]),
        description="消费者退款库存回收", source="direct_service_refund",
    )
    revoked_paid = int(revoked.get("revoked_paid", 0))
    revoked_bonus = int(revoked.get("revoked_bonus", 0))
    category = str(case.get("reason_category") or "negotiated_other")
    force_full_cash = category in {
        "duplicate_charge", "not_credited", "not_delivered", "system_failure", "legal_required",
    }
    base_points = int(recharge.get("base_points") or 0)
    if force_full_cash:
        refund_amount = int(recharge["amount_cents"])
    elif base_points > 0:
        refund_amount = min(
            int(recharge["amount_cents"]),
            int(recharge["amount_cents"]) * revoked_paid // base_points,
        )
    else:
        refund_amount = 0
    restore_points = revoked_paid + revoked_bonus
    # Cash-remedy scope and inventory recovery are independent.  Mandatory full
    # cash refund never fabricates seller inventory for credits already consumed.
    # Any unrecoverable portion remains the seller's audited refund liability.
    restoration = _restore_consumer_inventory_partial(
        cur, case=case, sale=sale, points_to_restore=restore_points,
    )
    revenue_reversed = _reverse_direct_service_revenue(
        cur, sale=sale, refund_amount_cents=refund_amount,
    )

    liability_id = None
    shortage = 0
    cash_status = "completed" if refund_amount == 0 else "queued"
    if refund_amount > 0:
        cur.execute(
            """INSERT INTO service_refund_reserve_accounts(service_user_id)
               VALUES (%s) ON CONFLICT(service_user_id) DO NOTHING""",
            (int(sale["seller_user_id"]),),
        )
        cur.execute(
            "SELECT * FROM service_refund_reserve_accounts WHERE service_user_id=%s FOR UPDATE",
            (int(sale["seller_user_id"]),),
        )
        reserve = _row_dict(cur.fetchone())
        pending_offset = min(refund_amount, revenue_reversed)
        reserve_offset = min(refund_amount - pending_offset, int(reserve.get("available_cents") or 0))
        shortage = refund_amount - pending_offset - reserve_offset
        if reserve_offset:
            cur.execute(
                """UPDATE service_refund_reserve_accounts
                   SET available_cents=available_cents-%s,row_version=row_version+1,updated_at=NOW()
                   WHERE service_user_id=%s AND available_cents>=%s""",
                (reserve_offset, int(sale["seller_user_id"]), reserve_offset),
            )
        cur.execute(
            """INSERT INTO service_refund_liability_ledger
               (case_id,source_order_id,service_user_id,refund_amount_cents,
                pending_settlement_offset_cents,reserve_offset_cents,negative_settlement_cents,status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING liability_id""",
            (
                str(case["case_id"]), str(sale["order_id"]), int(sale["seller_user_id"]),
                refund_amount, pending_offset, reserve_offset, shortage,
                "negative_settlement" if shortage else "covered",
            ),
        )
        liability_id = int(_row_dict(cur.fetchone())["liability_id"])
        if shortage:
            cur.execute(
                """INSERT INTO service_refund_funding_work_orders
                   (work_order_id,liability_id,service_user_id,shortage_cents,status,resolution_jsonb)
                   VALUES (%s,%s,%s,%s,'open',%s::jsonb)""",
                (
                    _stable_id("SFW", str(case["case_id"])), liability_id,
                    int(sale["seller_user_id"]), shortage,
                    json.dumps({"customer_refund_blocked": False, "negative_settlement": True}),
                ),
            )
        cur.execute(
            """INSERT INTO service_refund_cash_jobs
               (cash_job_id,case_id,source_order_id,consumer_user_id,
                responsible_service_user_id,amount_cents,original_payment_route,
                status,idempotency_key)
               VALUES (%s,%s,%s,%s,%s,%s,%s,'queued',%s)""",
            (
                _stable_id("SCJ", str(case["case_id"])), str(case["case_id"]),
                str(sale["order_id"]), int(sale["consumer_user_id"]),
                int(sale["seller_user_id"]), refund_amount,
                _original_payment_route(recharge),
                f"direct-service-refund:{case['case_id']}",
            ),
        )
    cur.execute(
        """UPDATE recharge_orders
           SET refund_status=CASE WHEN %s=0 THEN 'completed' ELSE 'approved' END,
               refund_requested_at=COALESCE(refund_requested_at,NOW()),
               refund_completed_at=CASE WHEN %s=0 THEN NOW() ELSE refund_completed_at END,
               refunded_amount_cents=%s
           WHERE id=%s""",
        (refund_amount, refund_amount, refund_amount, str(sale["order_id"])),
    )
    cur.execute(
        """UPDATE consumer_refund_cases
           SET status=CASE WHEN %s=0 THEN 'completed' ELSE 'platform_execution' END,
               cash_status=%s,refund_amount_cents=%s,revoked_paid_points=%s,
               revoked_bonus_points=%s,restored_inventory_points=%s,
               revenue_reversed_cents=%s,internal_settled_at=NOW(),
               execution_jsonb=execution_jsonb || %s::jsonb,
               row_version=row_version+1,updated_at=NOW()
           WHERE case_id=%s RETURNING *""",
        (
            refund_amount, cash_status, refund_amount, revoked_paid, revoked_bonus,
            int(restoration["points"]), revenue_reversed,
            json.dumps(
                {
                    "liability_id": liability_id, "negative_settlement_cents": shortage,
                    "ancestor_orders_touched": 0, "fixed_consumer_fee_bps": 0,
                    "cash_completed": refund_amount == 0,
                }, ensure_ascii=False, separators=(",", ":"),
            ), str(case["case_id"]),
        ),
    )
    return _row_dict(cur.fetchone())


def complete_consumer_refund_cash(
    cur, *, case_id: str, amount_cents: int, provider: str,
    external_refund_id: str, evidence: Dict[str, Any],
) -> Dict[str, Any]:
    """Record trusted provider completion once; never infer cash success from an internal state."""
    cur.execute("SELECT * FROM consumer_refund_cases WHERE case_id=%s FOR UPDATE", (str(case_id),))
    case = _row_dict(cur.fetchone())
    if not case:
        raise ResaleError("CONSUMER_REFUND_NOT_FOUND", "消费者退款申请不存在")
    _order_xact_lock(cur, str(case["source_order_id"]))
    if case.get("cash_status") == "completed":
        cur.execute(
            """SELECT amount_cents,provider_refund_id,provider_evidence_jsonb
               FROM service_refund_cash_jobs WHERE case_id=%s FOR UPDATE""",
            (str(case_id),),
        )
        completed_job = _row_dict(cur.fetchone())
        completed_evidence = _json_object(completed_job.get("provider_evidence_jsonb"))
        if (
            int(completed_job.get("amount_cents") or 0) != int(amount_cents)
            or str(completed_job.get("provider_refund_id") or "") != str(external_refund_id or "")
            or _required_refund_provider(completed_evidence.get("provider"))
               != _required_refund_provider(provider)
        ):
            raise ResaleError(
                "CONSUMER_REFUND_IDEMPOTENCY_CONFLICT",
                "现金退款已由另一渠道凭证完成，拒绝冲突重放",
            )
        return case
    if not case.get("internal_settled_at") or int(case.get("refund_amount_cents") or 0) <= 0:
        raise ResaleError("CONSUMER_REFUND_INTERNAL_NOT_SETTLED", "平台内账尚未完成，不能确认现金退款")
    if int(amount_cents) != int(case["refund_amount_cents"]):
        raise ResaleError("CONSUMER_REFUND_AMOUNT_MISMATCH", "现金退款金额与已批准金额不一致")
    if not str(provider or "").strip() or not str(external_refund_id or "").strip() or not evidence:
        raise ResaleError("CONSUMER_REFUND_EVIDENCE_REQUIRED", "缺少原支付渠道退款对账信息")
    cur.execute(
        "SELECT original_payment_route FROM service_refund_cash_jobs WHERE case_id=%s FOR UPDATE",
        (str(case_id),),
    )
    route = str(_row_dict(cur.fetchone()).get("original_payment_route") or "unknown").lower()
    provider_name = _required_refund_provider(provider)
    if route.startswith("wechat") and provider_name != "wechat":
        raise ResaleError("CONSUMER_REFUND_PROVIDER_MISMATCH", "退款凭证与原微信支付路径不一致")
    elif route == "xunhupay" and provider_name != "xunhupay":
        raise ResaleError("CONSUMER_REFUND_PROVIDER_MISMATCH", "退款凭证与原虎皮椒支付路径不一致")
    elif not route.startswith("wechat") and route != "xunhupay":
        raise ResaleError("CONSUMER_REFUND_PROVIDER_MISMATCH", "原支付路径没有可自动核验的退款回调")
    cur.execute(
        """SELECT refund_status,refund_completed_at,refunded_amount_cents,settlement_snapshot_jsonb
           FROM recharge_orders WHERE id=%s FOR UPDATE""",
        (str(case["source_order_id"]),),
    )
    recharge = _row_dict(cur.fetchone())
    snapshot = _json_object(recharge.get("settlement_snapshot_jsonb"))
    if (
        str(recharge.get("refund_status") or "").lower() not in {"completed", "pending_review"}
        or recharge.get("refund_completed_at") is None
        or int(recharge.get("refunded_amount_cents") or 0) != int(amount_cents)
    ):
        raise ResaleError("CONSUMER_REFUND_PROVIDER_PROOF_MISSING", "支付渠道尚未持久化可信退款终态")
    if str(snapshot.get("provider_refund_id") or "") != str(external_refund_id):
        raise ResaleError("CONSUMER_REFUND_PROVIDER_REF_MISMATCH", "外部退款号与验签回调记录不一致")
    if _required_refund_provider(snapshot.get("refund_provider")) != provider_name:
        raise ResaleError("CONSUMER_REFUND_PROVIDER_MISMATCH", "退款渠道与验签回调记录不一致")
    # Enter through the existing canonical channel-revenue sink.  It invokes
    # the direct-sale/B2B inventory sink before reversing the legacy channel
    # ledger, so all terminal legs remain in this transaction without recursive
    # service calls.
    from services.channel_revenue_lifecycle import reverse_channel_revenue_on_refund

    reverse_channel_revenue_on_refund(cur, str(case["source_order_id"]))
    cur.execute("SELECT * FROM consumer_refund_cases WHERE case_id=%s", (str(case_id),))
    result = _row_dict(cur.fetchone())
    if result.get("cash_status") != "completed":
        raise ResaleError("CONSUMER_REFUND_CASH_STATE_INVALID", "可信退款终态尚未完成结算")
    return result


def get_consumer_refund_visible(
    cur, *, case_id: str, actor_user_id: int, is_admin: bool = False,
) -> Dict[str, Any]:
    cur.execute(
        """SELECT c.*,s.sale_amount_cents,s.points,s.state AS sale_state
           FROM consumer_refund_cases c
           JOIN dealer_consumer_sales s ON s.order_id=c.source_order_id
           WHERE c.case_id=%s""",
        (str(case_id),),
    )
    row = _row_dict(cur.fetchone())
    if not row:
        raise ResaleError("CONSUMER_REFUND_NOT_FOUND", "消费者退款申请不存在")
    if not is_admin and int(actor_user_id) not in {
        int(row["consumer_user_id"]), int(row["responsible_service_user_id"]),
    }:
        raise ResaleError("FORBIDDEN", "只能查看本人购买或本人负责处理的退款")
    result = {
        "case_id": str(row["case_id"]), "source_order_id": str(row["source_order_id"]),
        "status": str(row["status"]), "reason": str(row.get("reason") or ""),
        "sale_amount_cents": int(row["sale_amount_cents"]), "points": int(row["points"]),
        "sale_state": str(row["sale_state"]), "policy_code": str(row["policy_code"]),
        "digital_goods_acknowledged_at": row["digital_goods_acknowledged_at"],
        "reason_category": str(row.get("reason_category") or "negotiated_other"),
        "decision_kind": str(row.get("decision_kind") or "negotiated"),
        "mandatory_evidence_status": str(row.get("mandatory_evidence_status") or "not_applicable"),
        "cash_status": str(row.get("cash_status") or "not_started"),
        "refund_amount_cents": int(row.get("refund_amount_cents") or 0),
        "revoked_paid_points": int(row.get("revoked_paid_points") or 0),
        "revoked_bonus_points": int(row.get("revoked_bonus_points") or 0),
        "restored_inventory_points": int(row.get("restored_inventory_points") or 0),
        "refund_handling": "PLATFORM_MANAGED",
    }
    if is_admin:
        result.update({
            "consumer_user_id": int(row["consumer_user_id"]),
            "responsible_service_user_id": int(row["responsible_service_user_id"]),
            "execution": _json_object(row.get("execution_jsonb")),
        })
    return result


def profit_summary(cur, *, seller_user_id: int) -> Dict[str, Any]:
    cur.execute(
        """SELECT status,COALESCE(SUM(margin_cents),0) AS cents,COUNT(*) AS count
           FROM dealer_resale_profit_ledger WHERE seller_user_id=%s GROUP BY status""",
        (int(seller_user_id),),
    )
    buckets = {
        str(row["status"]): {"cents": int(row["cents"]), "count": int(row["count"])}
        for row in cur.fetchall()
    }
    cur.execute(
        """SELECT
             COALESCE(SUM(CASE WHEN l.status='frozen' AND l.reversed_at IS NULL
                               THEN l.agent_settlement_cents ELSE 0 END),0) AS pending,
             COALESCE(SUM(CASE WHEN l.status='settled'
                               THEN l.agent_settlement_cents ELSE 0 END),0) AS available
           FROM agent_revenue_ledger l
           WHERE l.agent_user_id=%s AND l.recharge_order_id IN (
             SELECT order_id FROM dealer_consumer_sales WHERE seller_user_id=%s
           )""",
        (int(seller_user_id), int(seller_user_id)),
    )
    consumer = _row_dict(cur.fetchone())
    return {
        "b2b": buckets,
        "consumer": {
            "pending_cents": int(consumer.get("pending") or 0),
            "available_before_withdrawal_locks_cents": int(consumer.get("available") or 0),
        },
        "withdrawal_ssot": "agent_revenue_ledger",
    }
