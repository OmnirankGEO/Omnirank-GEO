"""WP1 discriminating tests — SSOT business-governance-master §4.1 / V4.

Promotion of an ordinary account that is currently a customer of an upstream
provider must be:
  * atomic (agent_level flips, no "please end the relationship first" wall),
  * relationship-preserving (the inbound customer binding becomes an upstream
    channel/procurement relationship — A stays B's upstream),
  * cost-only (the channel edge inherits the procurement cost at passthrough,
    never the upstream's end-customer retail markup — §5.2),
  * history-preserving and funds-neutral (§4.1.6),
  * without weakening the downgrade guard or the CAS/version invariants.
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

import psycopg2
import pytest

from services import channel_pricing
from services.admin_user_governance import (
    GovernanceValidationError,
    GovernanceVersionConflict,
    change_business_identity,
)


DB_URL = os.environ["TEST_DATABASE_URL"]


def _query_one(sql, params=()):
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()


def _promote(user_id, request_id, *, expected_version=1):
    return change_business_identity(
        user_id, "service_provider", expected_version=expected_version,
        reason="签约升级为下级服务商", operator_user_id=1,
        operator_username="admin_one", request_id=request_id, ip_address="127.0.0.1",
    )


def test_unbound_ordinary_user_promotion_creates_no_channel_relationship():
    # Guard against over-conversion: user 300 has no inbound binding, so promotion
    # must NOT fabricate a channel relationship.
    result = _promote(300, "wp1-unbound-300")
    assert result["success"] is True
    assert _query_one("SELECT agent_level FROM user_wallets WHERE user_id=300") == (1,)
    assert _query_one(
        "SELECT COUNT(*) FROM channel_pricing_relationships WHERE buyer_dealer_id=300"
    ) == (0,)


def test_promotion_audit_evidence_records_binding_conversion():
    _promote(124, "wp1-evidence-124")
    row = _query_one(
        "SELECT evidence_jsonb FROM admin_user_governance_audits "
        "WHERE request_id='wp1-evidence-124'"
    )
    assert row is not None
    evidence = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    conv = evidence["inbound_binding_conversion"]
    assert conv["upstream_provider_user_id"] == 123
    assert conv["channel_cost_multiplier_bps"] == 10000
    assert conv["historical_orders_untouched"] is True
    assert conv["pricing_snapshots_untouched"] is True


def test_promotion_does_not_inherit_upstream_retail_markup():
    # §5.2: the converted channel edge inherits the COST relationship at
    # cost-passthrough (10000 bps), never the upstream's end-customer markup.
    _promote(124, "wp1-markup-124")
    assert _query_one(
        "SELECT cost_multiplier_bps FROM channel_pricing_relationships "
        "WHERE buyer_dealer_id=124 AND effective_to IS NULL"
    ) == (10000,)


def test_promoted_user_resolves_channel_cost_basis_under_upstream():
    # The converted relationship is a real, usable channel edge: the new provider's
    # effective procurement cost resolves via the chain rooted at its upstream.
    _promote(124, "wp1-costbasis-124")
    basis = channel_pricing.resolve_effective_cost_basis(124, 10000)
    assert basis["effective_cost_cents"] == 10000
    assert basis["beneficiary_user_id"] == 123
    assert basis["depth"] == 1


def test_promotion_is_cas_guarded_against_stale_version():
    with pytest.raises(GovernanceVersionConflict):
        _promote(124, "wp1-stale-124", expected_version=999)
    # nothing changed on a rejected CAS
    assert _query_one("SELECT agent_level FROM user_wallets WHERE user_id=124") == (0,)
    assert _query_one(
        "SELECT COUNT(*) FROM channel_pricing_relationships WHERE buyer_dealer_id=124"
    ) == (0,)
    assert _query_one(
        "SELECT COUNT(*) FROM customer_agent_bindings WHERE customer_user_id=124"
    ) == (1,)


def test_downgrade_guard_still_blocks_provider_with_bound_customers():
    # Reverse discriminator: removing the *upgrade* block must NOT weaken the
    # *downgrade* guard. Provider 28 has bound customers 129/130/131.
    with pytest.raises(GovernanceValidationError) as exc:
        change_business_identity(
            28, "ordinary_user", expected_version=1,
            reason="尝试降级仍有依赖的服务商", operator_user_id=1,
            operator_username="admin_one", request_id="wp1-downgrade-28",
            ip_address="127.0.0.1",
        )
    assert exc.value.code == "ACTIVE_PROVIDER_DEPENDENCIES"
    assert _query_one("SELECT agent_level FROM user_wallets WHERE user_id=28") == (1,)


def test_upstream_cannot_be_downgraded_after_conversion_creates_channel_dependency():
    # After 124 becomes 123's downstream dealer, 123 now has an active channel
    # relation and cannot be silently downgraded to an ordinary user.
    _promote(124, "wp1-chain-124")
    with pytest.raises(GovernanceValidationError) as exc:
        change_business_identity(
            123, "ordinary_user", expected_version=1,
            reason="尝试降级已有下级渠道的上游", operator_user_id=1,
            operator_username="admin_one", request_id="wp1-downgrade-123",
            ip_address="127.0.0.1",
        )
    assert exc.value.code == "ACTIVE_PROVIDER_DEPENDENCIES"
    assert _query_one("SELECT agent_level FROM user_wallets WHERE user_id=123") == (1,)


def test_concurrent_double_promotion_converts_exactly_once():
    # Master §4.1.7: concurrent double-submit / lost-response replay must apply the
    # promotion + binding->channel conversion EXACTLY once (one channel edge, one
    # audit, no duplicate); losers fail-closed on the CAS version.
    def promote(i):
        try:
            return _promote(124, f"wp1-race-{i}", expected_version=1)
        except (GovernanceVersionConflict, GovernanceValidationError) as exc:
            return exc

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(promote, range(8)))

    winners = [r for r in results if isinstance(r, dict)]
    assert len(winners) == 1, results
    assert _query_one("SELECT agent_level FROM user_wallets WHERE user_id=124") == (1,)
    assert _query_one(
        "SELECT COUNT(*) FROM channel_pricing_relationships "
        "WHERE buyer_dealer_id=124 AND effective_to IS NULL"
    ) == (1,)
    assert _query_one(
        "SELECT COUNT(*) FROM customer_agent_bindings WHERE customer_user_id=124"
    ) == (0,)
    assert _query_one(
        "SELECT COUNT(*) FROM admin_user_governance_audits "
        "WHERE subject_user_id=124 AND scope='business_identity'"
    ) == (1,)


def test_stale_request_replay_after_success_is_rejected_without_double_apply():
    # First promotion succeeds; replaying the same request with the now-stale
    # expected_version must be rejected (CAS) and must NOT double-convert.
    _promote(124, "wp1-replay-124", expected_version=1)
    with pytest.raises(GovernanceVersionConflict):
        _promote(124, "wp1-replay-124", expected_version=1)
    assert _query_one(
        "SELECT COUNT(*) FROM channel_pricing_relationships WHERE buyer_dealer_id=124"
    ) == (1,)
    assert _query_one(
        "SELECT COUNT(*) FROM admin_user_governance_audits "
        "WHERE subject_user_id=124 AND scope='business_identity'"
    ) == (1,)


def test_promotion_preserves_registration_ownership_and_history_invariants():
    # Master §19.1 / §4.1.6: promotion must not rewrite registration source, owned
    # brands, client assignments, historical orders/points, or wallet balances.
    def snapshot():
        return (
            _query_one("SELECT COUNT(*) FROM brands WHERE owner_user_id=124"),
            _query_one("SELECT owner_user_id FROM brands WHERE id=601"),
            _query_one("SELECT COUNT(*) FROM user_clients WHERE user_id=124"),
            _query_one("SELECT payment_status FROM recharge_orders WHERE id='ORDER-124'"),
            _query_one(
                "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM point_transactions WHERE user_id=124"
            ),
            _query_one("SELECT referred_by_agent_id FROM users WHERE id=124"),
            _query_one(
                "SELECT paid_points, bonus_points, total_recharged FROM user_wallets WHERE user_id=124"
            ),
        )

    before = snapshot()
    _promote(124, "wp1-invariants-124")
    after = snapshot()
    assert before == after
    # only agent_level flips; wallet balances untouched
    assert _query_one("SELECT agent_level FROM user_wallets WHERE user_id=124") == (1,)


def test_platform_direct_user_has_no_binding_and_promotes_without_channel_edge():
    # Owner-named data premise: a platform-direct-served customer is represented by
    # the ABSENCE of a customer_agent_bindings row (commercial mode is derived from
    # "not in bound_ids" — services/admin_user_governance.py:484/673, binding_id=None),
    # NOT by a binding pointing at the platform-direct account. Promotion must
    # therefore create NO channel edge and fabricate NO service_provider
    # binding-history conversion (there is nothing to convert).
    # user 300 is unbound => platform-direct by definition.
    assert _query_one(
        "SELECT COUNT(*) FROM customer_agent_bindings WHERE customer_user_id=300"
    ) == (0,)  # premise locked: platform-direct == no binding row

    result = _promote(300, "wp1-platform-direct-300")
    assert result["success"] is True
    assert _query_one("SELECT agent_level FROM user_wallets WHERE user_id=300") == (1,)
    assert _query_one(
        "SELECT COUNT(*) FROM channel_pricing_relationships WHERE buyer_dealer_id=300"
    ) == (0,)
    # no service_provider conversion record fabricated for a never-bound user
    assert _query_one(
        "SELECT COUNT(*) FROM customer_agent_binding_history "
        "WHERE customer_user_id=300 AND relationship_state='service_provider'"
    ) == (0,)
    # audit evidence carries no inbound_binding_conversion (nothing was converted)
    row = _query_one(
        "SELECT evidence_jsonb FROM admin_user_governance_audits "
        "WHERE request_id='wp1-platform-direct-300'"
    )
    evidence = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    assert "inbound_binding_conversion" not in evidence
