import json
import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import psycopg2
import pytest

from services.legal_agreements import record_purchase_acceptance, validate_purchase_acceptance


def _user(cur, user_id: int) -> None:
    cur.execute(
        "INSERT INTO users(id,username) VALUES (%s,%s)",
        (user_id, f"agreement-{user_id}"),
    )


def test_purchase_acceptance_is_owned_versioned_and_expires(db_conn):
    cur = db_conn.cursor()
    _user(cur, 901)
    _user(cur, 902)
    acceptance = record_purchase_acceptance(
        cur,
        user_id=901,
        ip_address="203.0.113.9",
        user_agent="pytest-agent",
        surface="customer-recharge",
    )
    db_conn.commit()

    validated = validate_purchase_acceptance(
        cur, acceptance_id=acceptance["acceptance_id"], user_id=901,
        expected_surface="customer-recharge",
    )
    assert validated["content_hash"]
    assert validated["surface"] == "customer-recharge"
    with pytest.raises(ValueError, match="不属于当前用户"):
        validate_purchase_acceptance(
            cur, acceptance_id=acceptance["acceptance_id"], user_id=902,
            expected_surface="customer-recharge",
        )
    with pytest.raises(ValueError, match="购买场景不一致"):
        validate_purchase_acceptance(
            cur, acceptance_id=acceptance["acceptance_id"], user_id=901,
            expected_surface="subscription-checkout",
        )

    cur.execute(
        "UPDATE purchase_agreement_acceptances SET accepted_at=NOW()-INTERVAL '31 minutes' WHERE acceptance_id=%s",
        (acceptance["acceptance_id"],),
    )
    db_conn.commit()
    with pytest.raises(ValueError, match="已过期"):
        validate_purchase_acceptance(
            cur, acceptance_id=acceptance["acceptance_id"], user_id=901,
            expected_surface="customer-recharge",
        )


def test_one_acceptance_cannot_create_two_recharge_orders(db_conn):
    cur = db_conn.cursor()
    _user(cur, 903)
    acceptance = record_purchase_acceptance(
        cur,
        user_id=903,
        ip_address="198.51.100.7",
        user_agent="pytest-agent",
        surface="customer-recharge",
    )
    snapshot = json.dumps({"terms_acceptance_id": acceptance["acceptance_id"]})
    cur.execute(
        """INSERT INTO recharge_orders(id,user_id,amount_cents,base_points,pricing_snapshot_jsonb)
           VALUES ('terms-order-1',903,100,10,%s::jsonb)""",
        (snapshot,),
    )
    db_conn.commit()
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute(
            """INSERT INTO recharge_orders(id,user_id,amount_cents,base_points,pricing_snapshot_jsonb)
               VALUES ('terms-order-2',903,100,10,%s::jsonb)""",
            (snapshot,),
        )
    db_conn.rollback()


def test_database_rejects_cross_user_purchase_acceptance(db_conn):
    cur = db_conn.cursor()
    _user(cur, 910)
    _user(cur, 911)
    acceptance = record_purchase_acceptance(
        cur,
        user_id=910,
        ip_address="198.51.100.10",
        user_agent="pytest-agent",
        surface="customer-recharge",
    )
    db_conn.commit()
    with pytest.raises(psycopg2.errors.CheckViolation, match="owner mismatch"):
        cur.execute(
            """INSERT INTO recharge_orders
                 (id,user_id,amount_cents,base_points,pricing_snapshot_jsonb)
               VALUES ('terms-cross-user',911,100,10,%s::jsonb)""",
            (json.dumps({"terms_acceptance_id": acceptance["acceptance_id"]}),),
        )
    db_conn.rollback()


def test_explicit_reaccept_repairs_stale_same_version_signature(db_conn, monkeypatch):
    from api import auth_api
    from services.legal_agreements import USER_TERMS_CONTENT_HASH, USER_TERMS_VERSION
    import db.connection

    cur = db_conn.cursor()
    _user(cur, 912)
    cur.execute(
        """INSERT INTO agreement_signatures
             (user_id,agreement_type,agreement_version,content_hash,ip_address,user_agent)
           VALUES (912,'user_terms',%s,'stale-body-hash','198.51.100.1','stale-agent')""",
        (USER_TERMS_VERSION,),
    )
    db_conn.commit()
    monkeypatch.setattr(db.connection, "get_db", lambda: nullcontext(db_conn))

    result = asyncio.run(auth_api.accept_legal_agreements(
        auth_api.UserTermsAcceptanceRequest(
            terms_accepted=True,
            terms_version=USER_TERMS_VERSION,
            surface="customer-recharge",
        ),
        SimpleNamespace(
            state=SimpleNamespace(user={"user_id": 912}),
            headers={"X-Real-IP": "203.0.113.12", "User-Agent": "pytest-current-agent"},
            client=SimpleNamespace(host="127.0.0.1"),
        ),
    ))
    assert result["success"] is True
    cur.execute(
        """SELECT content_hash,ip_address,user_agent,evidence_jsonb
           FROM agreement_signatures
           WHERE user_id=912 AND agreement_type='user_terms' AND agreement_version=%s""",
        (USER_TERMS_VERSION,),
    )
    repaired = cur.fetchone()
    assert repaired["content_hash"] == USER_TERMS_CONTENT_HASH
    assert repaired["ip_address"] == "203.0.113.12"
    assert repaired["user_agent"] == "pytest-current-agent"
    assert repaired["evidence_jsonb"]["explicit_acceptance"] is True


def test_one_acceptance_cannot_create_two_subscription_orders(db_conn):
    cur = db_conn.cursor()
    _user(cur, 904)
    acceptance = record_purchase_acceptance(
        cur,
        user_id=904,
        ip_address="192.0.2.5",
        user_agent="pytest-agent",
        surface="subscription-checkout",
    )
    cur.execute(
        """INSERT INTO user_social_subscriptions(user_id,order_id,purchase_terms_acceptance_id)
           VALUES (904,'sub-terms-1',%s)""",
        (acceptance["acceptance_id"],),
    )
    db_conn.commit()
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute(
            """INSERT INTO user_social_subscriptions(user_id,order_id,purchase_terms_acceptance_id)
               VALUES (904,'sub-terms-2',%s)""",
            (acceptance["acceptance_id"],),
        )
    db_conn.rollback()


def test_purchase_acceptance_surface_is_bound_at_database_boundary(db_conn):
    cur = db_conn.cursor()
    _user(cur, 907)
    acceptance = record_purchase_acceptance(
        cur,
        user_id=907,
        ip_address="192.0.2.7",
        user_agent="pytest-agent",
        surface="customer-recharge",
    )
    db_conn.commit()
    with pytest.raises(psycopg2.errors.CheckViolation, match="surface mismatch"):
        cur.execute(
            """INSERT INTO user_social_subscriptions
                 (user_id,order_id,purchase_terms_acceptance_id)
               VALUES (907,'sub-cross-surface',%s)""",
            (acceptance["acceptance_id"],),
        )
    db_conn.rollback()


def test_service_agreement_gate_requires_current_version_and_exact_body_hash(db_conn):
    from services import agent_agreement

    cur = db_conn.cursor()
    _user(cur, 908)
    with pytest.raises(ValueError, match="正文版本已更新"):
        agent_agreement.sign_agreement(
            cur, 908, version=agent_agreement.CURRENT_VERSION,
            content_hash="wrong-body-hash",
        )
    with pytest.raises(ValueError, match="版本无效"):
        agent_agreement.sign_agreement(
            cur, 908, version="v2.3", content_hash="legacy-hash",
        )
    agent_agreement.sign_agreement(
        cur, 908, version=agent_agreement.CURRENT_VERSION,
        content_hash=agent_agreement.CURRENT_CONTENT_HASH,
        signed_ip="203.0.113.8", signed_ua="pytest-agent",
    )
    assert agent_agreement.is_agent_signed(
        cur, 908, agent_agreement.CURRENT_VERSION,
    ) is True

