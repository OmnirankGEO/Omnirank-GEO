"""Focused regression proofs for the two concentrated adversarial reviews."""

import copy
import os

import psycopg2
import psycopg2.errors
import pytest
from fastapi import HTTPException

from config import pricing_ssot_flags
from services import account_codes, channel_pricing, pricing_catalog, pricing_publication
from services.channel_pricing import ChannelError


def _publish_from_review(review: dict, *, actor_id: int = 900) -> dict:
    contract = review["review_contract"]
    return pricing_publication.publish_all(
        actor_id=actor_id,
        reason="adversarial remediation regression",
        expected_epoch=contract["config_epoch"],
        reviewed_target_mode=contract["target_mode"],
        reviewed_service_user_ids=contract["service_user_ids"],
        reviewed_scopes=contract["scopes"],
    )


def test_pricing_policy_read_failure_is_503_not_legacy_false(monkeypatch):
    class BrokenConnection:
        def __enter__(self):
            raise RuntimeError("policy database unavailable")

        def __exit__(self, *_args):
            return False

    # Prime an authoritative all-disabled snapshot first. A later read failure must
    # still stop; it may not reuse that compatibility state as if it were fresh.
    assert pricing_ssot_flags.pricing_flags_snapshot() == {
        "PRICING_DUAL_SSOT_ENABLED": False,
        "CHANNEL_PRICING_ENABLED": False,
        "PRICING_QUOTE_REQUIRED": False,
    }
    monkeypatch.setattr(pricing_ssot_flags, "get_db", lambda: BrokenConnection())

    with pytest.raises(pricing_ssot_flags.PricingFlagsUnavailable):
        pricing_ssot_flags.pricing_flags_snapshot()

    from api import agent_workbench_api, wallet_api

    for reader in (wallet_api._pricing_quote_flags, agent_workbench_api._pricing_quote_flags):
        pricing_ssot_flags.invalidate()
        with pytest.raises(HTTPException) as caught:
            reader()
        assert caught.value.status_code == 503
        assert caught.value.detail["code"] == "PRICING_POLICY_UNAVAILABLE"


def test_publication_binds_review_to_retail_source_fingerprint(raw_conn):
    account_codes.prepare_account_codes(service_user_ids=[100], channel_user_ids=[])
    with raw_conn.cursor() as cur:
        cur.execute(
            """INSERT INTO agent_sku_overrides
               (agent_user_id, sku_template_id, source_template_id, retail_sku_id,
                points_granted, retail_cents, custom_name, is_active, version)
               VALUES (100, 1, 1, 'RSKU-REVIEWED-100',
                       195000, 180000, 'reviewed package', TRUE, 1)"""
        )
    raw_conn.commit()

    reviewed = pricing_publication.dry_run_publication([100])
    assert reviewed["publishable"] is True
    reviewed_retail = next(
        scope for scope in reviewed["review_contract"]["scopes"]
        if scope["catalog_type"] == "retail"
    )

    forged = copy.deepcopy(reviewed)
    forged["review_contract"]["scopes"][0]["source_fingerprint"] = "0" * 64
    with pytest.raises(pricing_publication.PublicationReviewConflict):
        _publish_from_review(forged)

    # This management-source mutation deliberately does not bump config_epoch.
    with raw_conn.cursor() as cur:
        cur.execute(
            """UPDATE agent_sku_overrides
                  SET retail_cents=220000, updated_at=NOW()
                WHERE agent_user_id=100"""
        )
    raw_conn.commit()

    with pytest.raises(pricing_publication.PublicationReviewConflict):
        _publish_from_review(reviewed)
    with raw_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM pricing_catalog_versions")
        assert int(cur.fetchone()["c"]) == 0

    fresh = pricing_publication.dry_run_publication([100])
    fresh_retail = next(
        scope for scope in fresh["review_contract"]["scopes"]
        if scope["catalog_type"] == "retail"
    )
    assert fresh_retail["source_fingerprint"] != reviewed_retail["source_fingerprint"]
    result = _publish_from_review(fresh)
    assert result["published_count"] == 2


def test_publication_all_active_review_rejects_target_set_drift(raw_conn):
    account_codes.prepare_account_codes(
        service_user_ids=[100, 200, 300, 400], channel_user_ids=[]
    )
    reviewed = pricing_publication.dry_run_publication()
    assert reviewed["publishable"] is True
    assert reviewed["review_contract"]["target_mode"] == "all_active"
    assert reviewed["review_contract"]["service_user_ids"] == [100, 200, 300]

    with raw_conn.cursor() as cur:
        cur.execute("UPDATE user_wallets SET agent_level=1 WHERE user_id=400")
    raw_conn.commit()

    with pytest.raises(pricing_publication.PublicationReviewConflict):
        _publish_from_review(reviewed)


def test_explicit_empty_target_publishes_procurement_without_retail_scopes(raw_conn):
    """Admin may activate procurement independently from unrelated retail catalogs."""
    reviewed = pricing_publication.dry_run_publication([])

    assert reviewed["publishable"] is True
    assert reviewed["review_contract"]["target_mode"] == "explicit"
    assert reviewed["review_contract"]["service_user_ids"] == []
    assert [scope["catalog_type"] for scope in reviewed["review_contract"]["scopes"]] == [
        "procurement"
    ]

    result = _publish_from_review(reviewed)
    assert result["published_count"] == 1
    with raw_conn.cursor() as cur:
        cur.execute(
            "SELECT catalog_type, scope_key FROM pricing_catalog_versions WHERE status='published'"
        )
        assert [
            (row["catalog_type"], row["scope_key"]) for row in cur.fetchall()
        ] == [("procurement", "PLATFORM_BASE")]


def test_new_quote_rejects_demoted_channel_actor_and_graph_save_rechecks_all(raw_conn):
    channel_pricing.save_relationships(
        [{
            "buyer_dealer_id": 100,
            "upstream_channel_account_id": 200,
            "expected_relationship_version": None,
            "new_relationship_version": "rel-v1",
            "cost_multiplier_bps": 12000,
            "reason": "explicit admin list",
        }],
        approved_by=900,
        created_by=900,
    )
    with raw_conn.cursor() as cur:
        cur.execute("UPDATE user_wallets SET agent_level=0 WHERE user_id=200")
    raw_conn.commit()

    with pytest.raises(ChannelError, match="200"):
        channel_pricing.resolve_effective_cost_basis(100, 100000)

    with pytest.raises(ChannelError, match="200"):
        channel_pricing.save_relationships(
            [{
                "buyer_dealer_id": 300,
                "upstream_channel_account_id": 100,
                "expected_relationship_version": None,
                "new_relationship_version": "rel-v2",
                "cost_multiplier_bps": 10000,
                "reason": "unrelated graph extension",
            }],
            approved_by=900,
            created_by=900,
        )


def test_new_quote_revalidates_buyer_and_every_chain_actor(raw_conn):
    channel_pricing.save_relationships(
        [
            {
                "buyer_dealer_id": 100,
                "upstream_channel_account_id": 200,
                "expected_relationship_version": None,
                "new_relationship_version": "near-v1",
                "cost_multiplier_bps": 12000,
                "reason": "explicit admin list",
            },
            {
                "buyer_dealer_id": 200,
                "upstream_channel_account_id": 300,
                "expected_relationship_version": None,
                "new_relationship_version": "far-v1",
                "cost_multiplier_bps": 11000,
                "reason": "explicit admin list",
            },
        ],
        approved_by=900,
        created_by=900,
    )

    with raw_conn.cursor() as cur:
        cur.execute("UPDATE user_wallets SET agent_level=0 WHERE user_id=300")
    raw_conn.commit()
    with pytest.raises(ChannelError, match="300"):
        channel_pricing.resolve_effective_cost_basis(100, 100000)

    with raw_conn.cursor() as cur:
        cur.execute("UPDATE user_wallets SET agent_level=1 WHERE user_id=300")
        cur.execute("UPDATE user_wallets SET agent_level=0 WHERE user_id=100")
    raw_conn.commit()
    with pytest.raises(ChannelError, match="100"):
        channel_pricing.resolve_effective_cost_basis(100, 100000)


@pytest.mark.parametrize("entry_operation", ["insert", "update", "delete"])
def test_catalog_entry_mutation_locks_parent_before_concurrent_publish(entry_operation):
    version_id = pricing_catalog.create_draft_version(
        catalog_type="procurement",
        scope_key="PLATFORM_BASE",
        version_code=f"race-{entry_operation}",
        entries=[{
            "product_code": "race-base",
            "base_price_cents": 10000,
            "multiplier_bps": 10000,
            "paid_points": 10000,
            "bonus_points": 0,
        }],
        calc_meta={"source_fingerprint": (entry_operation[0] * 64)},
    )

    writer = psycopg2.connect(os.environ["DATABASE_URL"])
    publisher = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with writer.cursor() as cur:
            cur.execute(
                "SELECT id FROM pricing_catalog_entries WHERE version_id=%s",
                (version_id,),
            )
            entry_id = int(cur.fetchone()[0])
            if entry_operation == "insert":
                cur.execute(
                    """INSERT INTO pricing_catalog_entries
                       (version_id, product_code, base_price_cents, multiplier_bps,
                        final_price_cents, paid_points, bonus_points)
                       VALUES (%s, 'race-inserted', 11000, 10000, 11000, 11000, 0)""",
                    (version_id,),
                )
            elif entry_operation == "update":
                cur.execute(
                    "UPDATE pricing_catalog_entries SET base_price_cents=12000 WHERE id=%s",
                    (entry_id,),
                )
            else:
                cur.execute("DELETE FROM pricing_catalog_entries WHERE id=%s", (entry_id,))

        with publisher.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout='200ms'")
            with pytest.raises(psycopg2.errors.LockNotAvailable):
                cur.execute(
                    """UPDATE pricing_catalog_versions
                          SET status='published', effective_from=NOW(), published_at=NOW(),
                              approved_at=NOW(), approved_by=900, updated_at=NOW()
                        WHERE id=%s""",
                    (version_id,),
                )
        publisher.rollback()

        writer.commit()
        with publisher.cursor() as cur:
            cur.execute(
                """UPDATE pricing_catalog_versions
                      SET status='published', effective_from=NOW(), published_at=NOW(),
                          approved_at=NOW(), approved_by=900, updated_at=NOW()
                    WHERE id=%s""",
                (version_id,),
            )
        publisher.commit()

        with publisher.cursor() as cur:
            with pytest.raises(psycopg2.errors.ObjectNotInPrerequisiteState):
                cur.execute(
                    """INSERT INTO pricing_catalog_entries
                       (version_id, product_code, base_price_cents, multiplier_bps,
                        final_price_cents, paid_points, bonus_points)
                       VALUES (%s, 'post-publish', 1, 10000, 1, 1, 0)""",
                    (version_id,),
                )
        publisher.rollback()
    finally:
        writer.close()
        publisher.close()
