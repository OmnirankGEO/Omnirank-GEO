"""True PostgreSQL 16 gates for all-account teams and invite-created operators."""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from verify_local import (  # noqa: E402
    BASE_SQL,
    MIGRATION_SQL,
    ONBOARDING_MIGRATION_SQL,
    PAYER_MIGRATION_SQL,
    Verification,
    connect,
    create_schema,
    execute_sql,
    install_environment,
    seed_runtime,
    sql_value,
)


def expect_code(verify: Verification, name: str, code: str, callback) -> None:
    try:
        callback()
    except Exception as exc:
        verify.check(getattr(exc, "code", None) == code, name, repr(exc))
    else:
        raise AssertionError(f"{name}: expected {code}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    args = parser.parse_args()
    if (urlsplit(args.dsn).hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("verification only accepts a loopback throwaway PostgreSQL DSN")
    verify = Verification()

    _, dsn = create_schema(args.dsn, "org_all_accounts")
    execute_sql(dsn, BASE_SQL)
    execute_sql(dsn, MIGRATION_SQL)
    execute_sql(dsn, ONBOARDING_MIGRATION_SQL)
    execute_sql(dsn, MIGRATION_SQL)
    execute_sql(dsn, ONBOARDING_MIGRATION_SQL)
    execute_sql(dsn, PAYER_MIGRATION_SQL)
    execute_sql(dsn, PAYER_MIGRATION_SQL)
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT p.included_seats,p.extra_seat_price_cents,
                      p.paid_extra_seats_enabled,p.operational,e.final_price_cents
               FROM organization_product_config_publications p
               JOIN pricing_catalog_entries e ON e.id=p.product_catalog_entry_id
               ORDER BY p.publication_version DESC LIMIT 1"""
        )
        governance = dict(cursor.fetchone())
    verify.check(
        governance == {
            "included_seats": 8,
            "extra_seat_price_cents": 0,
            "paid_extra_seats_enabled": False,
            "operational": True,
            "final_price_cents": 0,
        },
        "migration_seed_publishes_versioned_free_basic_team_entitlement",
        governance,
    )

    seed_runtime(dsn)
    install_environment(dsn)
    os.environ["ORGANIZATION_INVITE_ONBOARDING_ENABLED"] = "true"
    os.environ["ORGANIZATION_INVITE_DELIVERY_PROVIDER"] = "local_capture"
    os.environ["APP_ENV"] = "test"

    from db.organization_db import assigned_brand_ids, resolve_identity
    from services.legal_agreements import PRIVACY_VERSION, USER_TERMS_VERSION
    from services.organization_contract import OrganizationError
    from services.organization_onboarding import (
        _local_capture_enabled,
        create_verification_challenge,
        inspect_public_invite,
        onboard_operator,
        publish_product_config,
        verify_challenge,
    )
    from services.organization_service import (
        accept_invite,
        create_invite,
        create_organization,
        resend_invite,
        revoke_invite,
        set_member_status,
    )

    os.environ.pop("APP_ENV", None)
    os.environ.pop("ENVIRONMENT", None)
    verify.check(
        not _local_capture_enabled(),
        "local_capture_needs_explicit_nonproduction_environment",
    )
    os.environ["APP_ENV"] = "test"

    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO user_wallets(user_id,paid_points,total_recharged,agent_level)
               VALUES (3,0,0,1)"""
        )

    ordinary = create_organization(
        owner_user_id=2,
        name="普通账号内部团队",
        request_id="all-account-create-ordinary-0001",
    )
    provider = create_organization(
        owner_user_id=3,
        name="现役服务商内部团队",
        request_id="all-account-create-provider-0001",
    )
    verify.check(
        ordinary["owner_user_id"] == 2
        and sql_value(dsn, "SELECT COUNT(*) FROM user_wallets WHERE user_id=2") == 0,
        "ordinary_active_account_creates_team_without_wallet_or_agent_level",
        ordinary,
    )
    verify.check(
        provider["owner_user_id"] == 3
        and sql_value(dsn, "SELECT agent_level FROM user_wallets WHERE user_id=3") == 1,
        "service_provider_creates_team_without_commercial_mutation",
        provider,
    )
    admin = create_organization(
        owner_user_id=80,
        name="平台管理员内部团队",
        request_id="all-account-create-admin-0001",
    )
    verify.check(
        admin["owner_user_id"] == 80
        and sql_value(
            dsn,
            "SELECT COUNT(*) FROM user_roles ur JOIN roles r ON r.id=ur.role_id WHERE ur.user_id=80 AND r.name='admin'",
        )
        == 1,
        "platform_admin_creates_team_without_losing_platform_role",
        admin,
    )
    seed_owner = resolve_identity(2, request_id="seed-policy-immediate-invite")
    seed_member_role_id = int(
        sql_value(
            dsn,
            "SELECT id FROM organization_roles WHERE organization_id=%s AND code='sales'",
            (ordinary["id"],),
        )
    )
    seed_invite = create_invite(
        seed_owner,
        target_kind="phone",
        target="+8613600000000",
        role_id=seed_member_role_id,
        request_id="seed-policy-immediate-invite-0001",
        source_ip="127.0.0.1",
    )
    verify.check(
        seed_invite["invite"]["status"] == "pending"
        and ordinary["entitlement"]["entitled_seats"] > 0,
        "new_team_can_invite_immediately_from_migration_seed_policy",
        seed_invite["invite"],
    )
    revoke_invite(
        seed_owner,
        invite_id=int(seed_invite["invite"]["id"]),
        reason="完成默认策略即时邀请判别",
    )

    expect_code(
        verify,
        "organization_create_same_key_different_name_conflicts",
        "ORG_IDEMPOTENCY_CONFLICT",
        lambda: create_organization(
            owner_user_id=2,
            name="篡改后的团队名",
            request_id="all-account-create-ordinary-0001",
        ),
    )

    published = publish_product_config(
        actor_user_id=80,
        request_id="all-account-config-publish-0001",
        expected_version=1,
        included_seats=8,
        invite_ttl_hours=72,
        verification_ttl_minutes=10,
        verification_max_attempts=5,
        reason="本地验证零价基础员工席位",
    )
    replayed_publication = publish_product_config(
        actor_user_id=80,
        request_id="all-account-config-publish-0001",
        expected_version=1,
        included_seats=8,
        invite_ttl_hours=72,
        verification_ttl_minutes=10,
        verification_max_attempts=5,
        reason="本地验证零价基础员工席位",
    )
    verify.check(
        published["operational"]
        and published["included_seats"] == 8
        and published["extra_seat_price_cents"] == 0
        and not published["paid_extra_seats_enabled"]
        and replayed_publication["replayed"],
        "admin_publishes_auditable_free_basic_entitlement_with_replay",
        {"published": published, "replay": replayed_publication},
    )
    verify.check(
        sql_value(
            dsn,
            """SELECT COUNT(*) FROM organization_seat_entitlements
               WHERE status='active' AND effective_to IS NULL AND entitled_seats=8""",
        )
        == 3,
        "publication_supersedes_all_live_team_entitlements_atomically",
    )

    owner2 = resolve_identity(2, request_id="owner2-invite-existing")
    owner3 = resolve_identity(3, request_id="owner3-invite-existing")
    member_role2 = int(
        sql_value(
            dsn,
            "SELECT id FROM organization_roles WHERE organization_id=%s AND code='sales'",
            (owner2.organization_id,),
        )
    )
    member_role3 = int(
        sql_value(
            dsn,
            "SELECT id FROM organization_roles WHERE organization_id=%s AND code='sales'",
            (owner3.organization_id,),
        )
    )
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO brands(id,owner_user_id,name) VALUES (20,2,'普通账号团队客户')")
        cursor.execute(
            """SELECT r.code,r.name,array_agg(c.capability ORDER BY c.capability) AS capabilities
               FROM organization_roles r
               LEFT JOIN organization_role_capabilities c ON c.role_id=r.id AND c.effect='allow'
               WHERE r.organization_id=%s AND NOT r.is_owner_role
               GROUP BY r.id ORDER BY r.code""",
            (owner2.organization_id,),
        )
        role_contract = {row["code"]: dict(row) for row in cursor.fetchall()}
    verify.check(
        set(role_contract) == {"sales", "delivery", "readonly"}
        and role_contract["sales"]["name"] == "销售"
        and role_contract["delivery"]["name"] == "交付"
        and role_contract["readonly"]["name"] == "只读协作"
        and "writing.generate" not in role_contract["sales"]["capabilities"]
        and "publish.execute" not in role_contract["sales"]["capabilities"]
        and "diagnosis.run" not in role_contract["delivery"]["capabilities"]
        and "quote.create" not in role_contract["delivery"]["capabilities"]
        and not {
            "diagnosis.run", "quote.create", "writing.generate", "publish.execute", "monitoring.run"
        }.intersection(role_contract["readonly"]["capabilities"]),
        "default_roles_split_sales_delivery_and_readonly_without_full_loop",
        role_contract,
    )
    delivery_role2 = int(
        sql_value(
            dsn,
            "SELECT id FROM organization_roles WHERE organization_id=%s AND code='delivery'",
            (owner2.organization_id,),
        )
    )
    expect_code(
        verify,
        "sales_cross_domain_invite_requires_second_confirmation",
        "ORG_HIGH_RISK_CONFIRMATION_REQUIRED",
        lambda: create_invite(
            owner2,
            target_kind="phone",
            target="13900000074",
            role_id=member_role2,
            request_id="sales-cross-domain-unconfirmed-0001",
            source_ip="192.0.2.12",
            capability_overrides={"writing.generate": "allow"},
        ),
    )
    high_risk_invite = create_invite(
        owner2,
        target_kind="phone",
        target="13900000074",
        role_id=member_role2,
        request_id="sales-cross-domain-confirmed-0001",
        source_ip="192.0.2.12",
        capability_overrides={"writing.generate": "allow"},
        high_risk_confirmed=True,
        high_risk_reason="临时跨域协作，老板已确认并接受审计",
    )
    verify.check(
        high_risk_invite["invite"]["access_policy"]["high_risk_expansions"] == ["writing.generate"]
        and sql_value(
            dsn,
            """SELECT COUNT(*) FROM organization_audit_events
               WHERE action='invite.create'
                 AND after_snapshot @> '{"high_risk_expansions":["writing.generate"]}'::jsonb""",
        )
        == 1,
        "confirmed_cross_domain_invite_is_explicit_and_audited",
        high_risk_invite["invite"],
    )
    revoke_invite(
        owner2,
        invite_id=int(high_risk_invite["invite"]["id"]),
        reason="完成高风险邀请确认判别",
    )
    expect_code(
        verify,
        "invite_preassignment_rejects_cross_tenant_brand",
        "ORG_BRAND_NOT_OWNED",
        lambda: create_invite(
            owner2,
            target_kind="phone",
            target="13900000075",
            role_id=delivery_role2,
            request_id="invite-cross-tenant-brand-0001",
            source_ip="192.0.2.13",
            brand_ids=[72],
        ),
    )
    policy_invite = create_invite(
        owner2,
        target_kind="phone",
        target="13900000073",
        role_id=member_role2,
        request_id="invite-preconfigured-policy-0001",
        source_ip="192.0.2.14",
        brand_ids=[20],
        capability_overrides={"clients.profile_edit": "deny"},
        artifact_scope="own",
        daily_limit_points=100,
        monthly_limit_points=1000,
        feature_limits={"geo_diagnosis": {"daily": 50, "monthly": 500}},
    )
    policy_accept = accept_invite(
        authenticated_user_id=73,
        token=policy_invite["delivery_token"],
        request_id="invite-preconfigured-policy-accept-0001",
        source_ip="192.0.2.15",
    )
    member73 = resolve_identity(73, request_id="member73-preconfigured")
    verify.check(
        assigned_brand_ids(member73) == [20]
        and "clients.profile_edit" not in member73.capabilities
        and "diagnosis.run" in member73.capabilities
        and sql_value(
            dsn,
            "SELECT COUNT(*) FROM organization_spend_limits WHERE membership_id=%s",
            (policy_accept["membership_id"],),
        )
        == 4
        and policy_accept["access_policy_hash"] == policy_invite["invite"]["access_policy_hash"],
        "invite_accept_atomically_applies_clients_permissions_object_scope_and_limits",
        {"invite": policy_invite["invite"], "accept": policy_accept},
    )
    owner2 = resolve_identity(2, request_id="owner2-remove-preconfigured")
    set_member_status(
        owner2,
        membership_id=policy_accept["membership_id"],
        action="remove",
        reason="完成邀请预配置原子落地判别",
    )

    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO user_clients(user_id,brand_id) VALUES (70,71)")
    before_commercial = {
        "brands": sql_value(dsn, "SELECT COUNT(*) FROM brands WHERE owner_user_id=70"),
        "bindings": sql_value(
            dsn,
            "SELECT COUNT(*) FROM customer_agent_bindings WHERE agent_user_id=70",
        ),
        "wallets": sql_value(dsn, "SELECT COUNT(*) FROM user_wallets WHERE user_id=70"),
        "points": sql_value(dsn, "SELECT COUNT(*) FROM point_transactions WHERE user_id=70"),
    }
    existing_invite = create_invite(
        owner2,
        target_kind="phone",
        target="13900000070",
        role_id=member_role2,
        request_id="all-account-existing-user-invite-0001",
        source_ip="192.0.2.20",
    )
    existing_accept = accept_invite(
        authenticated_user_id=70,
        token=existing_invite["delivery_token"],
        request_id="all-account-existing-user-accept-0001",
        source_ip="192.0.2.21",
    )
    member70 = resolve_identity(70, request_id="member70-live")
    verify.check(
        member70.principal_user_id == 2
        and member70.payer_user_id == 2
        and assigned_brand_ids(member70) == []
        and sql_value(dsn, "SELECT COUNT(*) FROM user_clients WHERE user_id=70") == 0,
        "existing_commercial_account_uses_owner_payer_and_zero_assignment",
        existing_accept,
    )
    after_commercial = {
        "brands": sql_value(dsn, "SELECT COUNT(*) FROM brands WHERE owner_user_id=70"),
        "bindings": sql_value(
            dsn,
            "SELECT COUNT(*) FROM customer_agent_bindings WHERE agent_user_id=70",
        ),
        "wallets": sql_value(dsn, "SELECT COUNT(*) FROM user_wallets WHERE user_id=70"),
        "points": sql_value(dsn, "SELECT COUNT(*) FROM point_transactions WHERE user_id=70"),
    }
    verify.check(
        after_commercial == before_commercial,
        "existing_account_acceptance_has_no_commercial_wallet_or_referral_side_effect",
        {"before": before_commercial, "after": after_commercial},
    )
    owner2 = resolve_identity(2, request_id="owner2-remove-existing")
    member70_version = int(
        sql_value(
            dsn,
            "SELECT version FROM organization_memberships WHERE id=%s",
            (existing_accept["membership_id"],),
        )
    )
    removed70 = set_member_status(
        owner2,
        membership_id=existing_accept["membership_id"],
        action="remove",
        reason="验证既有账号原客户范围恢复",
    )
    verify.check(
        removed70["status"] == "removed"
        and sql_value(
            dsn,
            "SELECT COUNT(*) FROM user_clients WHERE user_id=70 AND brand_id=71",
        )
        == 1,
        "existing_account_leave_restores_immutable_legacy_scope",
        removed70,
    )

    from api.auth_api import router as auth_router
    from api.organization_api import public_router

    app = FastAPI()

    @app.exception_handler(OrganizationError)
    async def organization_error_handler(_: Request, exc: OrganizationError):
        return JSONResponse(
            status_code=exc.http_status,
            content={"detail": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(auth_router)
    app.include_router(public_router)
    client = TestClient(app, raise_server_exceptions=True)

    owner2 = resolve_identity(2, request_id="owner2-http-onboarding")
    phone_invite = create_invite(
        owner2,
        target_kind="phone",
        target="+8613700000001",
        role_id=member_role2,
        request_id="all-account-new-phone-invite-0001",
        source_ip="192.0.2.30",
    )
    inspect_response = client.post(
        "/api/public/organization/invites/inspect",
        json={"token": phone_invite["delivery_token"], "request_id": "public-inspect-0001"},
    )
    verify.check(
        inspect_response.status_code == 200
        and inspect_response.json()["account_mode"] == "create_operator"
        and "no-store" in inspect_response.headers.get("cache-control", ""),
        "public_http_inspect_detects_missing_account_without_token_in_url",
        inspect_response.json(),
    )
    challenge_response = client.post(
        "/api/public/organization/invites/verification-challenges",
        json={"token": phone_invite["delivery_token"], "request_id": "public-challenge-0001"},
    )
    challenge_body = challenge_response.json()
    verify_response = client.post(
        f"/api/public/organization/invites/verification-challenges/{challenge_body['challenge_id']}/verify",
        json={
            "token": phone_invite["delivery_token"],
            "request_id": "public-verify-0001",
            "code": challenge_body["test_code"],
        },
    )
    verification_body = verify_response.json()
    onboard_payload = {
        "token": phone_invite["delivery_token"],
        "request_id": "public-onboard-phone-0001",
        "challenge_id": challenge_body["challenge_id"],
        "verification_receipt": verification_body["verification_receipt"],
        "password": "LocalOperator!2026",
        "display_name": "手机号邀请员工",
        "terms_accepted": True,
        "privacy_accepted": True,
        "terms_version": USER_TERMS_VERSION,
        "privacy_version": PRIVACY_VERSION,
    }
    onboard_response = client.post(
        "/api/public/organization/invites/onboard",
        json=onboard_payload,
    )
    onboard_body = onboard_response.json()
    replay_response = client.post(
        "/api/public/organization/invites/onboard",
        json=onboard_payload,
    )
    verify.check(
        challenge_response.status_code == 200
        and verify_response.status_code == 200
        and onboard_response.status_code == 200
        and replay_response.status_code == 200
        and replay_response.json()["user_id"] == onboard_body["user_id"]
        and replay_response.json()["replayed"],
        "phone_http_onboarding_is_verified_atomic_and_response_loss_replayable",
        {"onboard": onboard_body, "replay": replay_response.json()},
    )
    password_conflict_response = client.post(
        "/api/public/organization/invites/onboard",
        json={**onboard_payload, "password": "DifferentOperator!2026"},
    )
    verify.check(
        password_conflict_response.status_code == 409
        and password_conflict_response.json()["detail"]["code"] == "ORG_INVITE_IDEMPOTENCY_CONFLICT",
        "onboarding_same_request_different_password_is_rejected",
        password_conflict_response.json(),
    )
    phone_user_id = int(onboard_body["user_id"])
    login_response = client.post(
        "/api/auth/login",
        json={
            "username": onboard_body["login_username"],
            "password": "LocalOperator!2026",
        },
    )
    login_body = login_response.json()
    verify.check(
        login_response.status_code == 200
        and login_body.get("success") is True
        and bool(login_body.get("token"))
        and int(login_body["user"]["id"]) == phone_user_id
        and login_body["user"].get("roles") == []
        and login_body["user"].get("client_brand_ids") == [],
        "invite_created_operator_can_login_without_legacy_commercial_role_or_scope",
        login_body,
    )
    verify.check(
        sql_value(dsn, "SELECT COUNT(*) FROM user_roles WHERE user_id=%s", (phone_user_id,)) == 0
        and sql_value(dsn, "SELECT COUNT(*) FROM user_wallets WHERE user_id=%s", (phone_user_id,)) == 0
        and sql_value(dsn, "SELECT COUNT(*) FROM brands WHERE owner_user_id=%s", (phone_user_id,)) == 0
        and sql_value(dsn, "SELECT COUNT(*) FROM user_clients WHERE user_id=%s", (phone_user_id,)) == 0
        and sql_value(
            dsn,
            "SELECT COUNT(*) FROM customer_agent_bindings WHERE customer_user_id=%s OR agent_user_id=%s",
            (phone_user_id, phone_user_id),
        )
        == 0
        and sql_value(dsn, "SELECT COUNT(*) FROM agreement_signatures WHERE user_id=%s", (phone_user_id,)) == 2,
        "invite_operator_has_no_legacy_role_brand_wallet_referral_or_client_assets",
        phone_user_id,
    )
    member_phone = resolve_identity(phone_user_id, request_id="phone-operator-live")
    verify.check(
        member_phone.principal_user_id == 2
        and member_phone.payer_user_id == 2
        and member_phone.actor_kind == "member",
        "invite_operator_identity_is_actor_only_owner_is_principal_and_payer",
        member_phone,
    )
    invalid_extra = client.post(
        "/api/public/organization/invites/onboard",
        json={**onboard_payload, "referral_code": "must-not-be-accepted"},
    )
    verify.check(
        invalid_extra.status_code == 422,
        "public_onboarding_rejects_referral_or_commercial_payload_fields",
        invalid_extra.json(),
    )

    owner2 = resolve_identity(2, request_id="owner2-verification-attempts")
    locked_invite = create_invite(
        owner2,
        target_kind="phone",
        target="+8613700000010",
        role_id=member_role2,
        request_id="verification-attempt-limit-invite-0001",
        source_ip="192.0.2.35",
    )
    locked_challenge = create_verification_challenge(
        token=locked_invite["delivery_token"],
        request_id="verification-attempt-limit-challenge-0001",
        source_ip="192.0.2.36",
    )
    wrong_code = "000000" if locked_challenge["test_code"] != "000000" else "999999"
    for attempt in range(1, 6):
        expected = "ORG_INVITE_CODE_LOCKED" if attempt == 5 else "ORG_INVITE_CODE_MISMATCH"
        expect_code(
            verify,
            f"verification_wrong_attempt_{attempt}_persists",
            expected,
            lambda attempt=attempt: verify_challenge(
                challenge_id=locked_challenge["challenge_id"],
                token=locked_invite["delivery_token"],
                code=wrong_code,
                request_id=f"verification-wrong-{attempt:02d}",
                source_ip="192.0.2.37",
            ),
        )
    verify.check(
        sql_value(
            dsn,
            "SELECT attempt_count FROM organization_invite_verification_challenges WHERE id=%s AND status='locked'",
            (locked_challenge["challenge_id"],),
        )
        == 5,
        "verification_attempt_limit_is_durable_after_error_responses",
        locked_challenge,
    )
    expect_code(
        verify,
        "verification_locked_challenge_rejects_later_correct_code",
        "ORG_INVITE_CHALLENGE_EXPIRED",
        lambda: verify_challenge(
            challenge_id=locked_challenge["challenge_id"],
            token=locked_invite["delivery_token"],
            code=locked_challenge["test_code"],
            request_id="verification-after-lock-0001",
            source_ip="192.0.2.37",
        ),
    )
    revoke_invite(
        owner2,
        invite_id=locked_invite["invite"]["id"],
        reason="验证码尝试上限判别完成",
    )

    owner2 = resolve_identity(2, request_id="owner2-email-onboarding")
    email_invite = create_invite(
        owner2,
        target_kind="email",
        target="fresh.operator@example.test",
        role_id=member_role2,
        request_id="all-account-new-email-invite-0001",
        source_ip="192.0.2.40",
    )
    email_challenge = create_verification_challenge(
        token=email_invite["delivery_token"],
        request_id="email-challenge-0001",
        source_ip="192.0.2.41",
    )
    email_verified = verify_challenge(
        challenge_id=email_challenge["challenge_id"],
        token=email_invite["delivery_token"],
        code=email_challenge["test_code"],
        request_id="email-verify-0001",
        source_ip="192.0.2.42",
    )
    email_onboard = onboard_operator(
        token=email_invite["delivery_token"],
        challenge_id=email_challenge["challenge_id"],
        verification_receipt=email_verified["verification_receipt"],
        password="EmailOperator!2026",
        display_name="邮箱邀请员工",
        request_id="email-onboard-0001",
        terms_accepted=True,
        privacy_accepted=True,
        terms_version=USER_TERMS_VERSION,
        privacy_version=PRIVACY_VERSION,
        source_ip="192.0.2.43",
        user_agent="organization-onboarding-test",
    )
    verify.check(
        sql_value(
            dsn,
            """SELECT COUNT(*) FROM users WHERE id=%s AND email=%s
               AND email_verified IS TRUE AND email_verified_for=%s""",
            (
                email_onboard["user_id"],
                "fresh.operator@example.test",
                "fresh.operator@example.test",
            ),
        )
        == 1,
        "email_invite_creates_only_exact_verified_contact_identity",
        email_onboard,
    )

    owner2 = resolve_identity(2, request_id="owner2-revoke-onboarding")
    revoked_invite = create_invite(
        owner2,
        target_kind="phone",
        target="+8613700000002",
        role_id=member_role2,
        request_id="all-account-revoked-invite-0001",
        source_ip="192.0.2.50",
    )
    revoked_challenge = create_verification_challenge(
        token=revoked_invite["delivery_token"],
        request_id="revoked-challenge-0001",
        source_ip="192.0.2.51",
    )
    revoke_invite(
        owner2,
        invite_id=revoked_invite["invite"]["id"],
        reason="验证撤销即时生效",
    )
    expect_code(
        verify,
        "revoked_invite_cannot_verify_or_create_account",
        "ORG_INVITE_REVOKED",
        lambda: verify_challenge(
            challenge_id=revoked_challenge["challenge_id"],
            token=revoked_invite["delivery_token"],
            code=revoked_challenge["test_code"],
            request_id="revoked-verify-0001",
            source_ip="192.0.2.52",
        ),
    )

    original_hmac = os.environ["ORGANIZATION_INVITE_HMAC_KEYS"]
    key2 = base64.urlsafe_b64encode(b"organization-test-key-material-2!").decode("ascii").rstrip("=")
    owner2 = resolve_identity(2, request_id="owner2-rotated-target")
    rotated_target_invite = create_invite(
        owner2,
        target_kind="email",
        target="rotation.target@example.test",
        role_id=member_role2,
        request_id="rotation-target-v1-0001",
        source_ip="192.0.2.60",
    )
    os.environ["ORGANIZATION_INVITE_HMAC_KEYS"] = f"{original_hmac},v2:{key2}"
    os.environ["ORGANIZATION_INVITE_HMAC_KEYS_ACTIVE_VERSION"] = "v2"
    owner3 = resolve_identity(3, request_id="owner3-rotated-target")
    expect_code(
        verify,
        "same_target_cross_org_and_hmac_rotation_remains_unique",
        "ORG_INVITE_ALREADY_PENDING",
        lambda: create_invite(
            owner3,
            target_kind="email",
            target="rotation.target@example.test",
            role_id=member_role3,
            request_id="rotation-target-v2-0001",
            source_ip="192.0.2.61",
        ),
    )
    os.environ["ORGANIZATION_INVITE_HMAC_KEYS"] = original_hmac
    os.environ["ORGANIZATION_INVITE_HMAC_KEYS_ACTIVE_VERSION"] = "v1"
    revoke_invite(
        resolve_identity(2, request_id="owner2-revoke-rotation"),
        invite_id=rotated_target_invite["invite"]["id"],
        reason="结束轮换唯一性验证",
    )

    owner2 = resolve_identity(2, request_id="owner2-concurrent-onboarding")
    concurrent_invite = create_invite(
        owner2,
        target_kind="phone",
        target="+8613700000003",
        role_id=member_role2,
        request_id="concurrent-operator-invite-0001",
        source_ip="192.0.2.70",
    )
    concurrent_challenge = create_verification_challenge(
        token=concurrent_invite["delivery_token"],
        request_id="concurrent-challenge-0001",
        source_ip="192.0.2.71",
    )
    concurrent_verified = verify_challenge(
        challenge_id=concurrent_challenge["challenge_id"],
        token=concurrent_invite["delivery_token"],
        code=concurrent_challenge["test_code"],
        request_id="concurrent-verify-0001",
        source_ip="192.0.2.72",
    )

    def concurrent_onboard(index: int):
        try:
            return onboard_operator(
                token=concurrent_invite["delivery_token"],
                challenge_id=concurrent_challenge["challenge_id"],
                verification_receipt=concurrent_verified["verification_receipt"],
                password="ConcurrentOperator!2026",
                display_name="并发邀请员工",
                request_id="concurrent-onboard-stable-0001",
                terms_accepted=True,
                privacy_accepted=True,
                terms_version=USER_TERMS_VERSION,
                privacy_version=PRIVACY_VERSION,
                source_ip=f"198.51.100.{index + 1}",
                user_agent="organization-onboarding-concurrency",
            )
        except OrganizationError as exc:
            return {"error": exc.code}

    with ThreadPoolExecutor(max_workers=20) as pool:
        concurrent_results = list(pool.map(concurrent_onboard, range(20)))
    concurrent_user_ids = {
        int(result["user_id"]) for result in concurrent_results if result.get("user_id")
    }
    verify.check(
        len(concurrent_user_ids) == 1
        and not [result for result in concurrent_results if result.get("error")]
        and sql_value(
            dsn,
            "SELECT COUNT(*) FROM organization_operator_accounts WHERE invite_id=%s",
            (concurrent_invite["invite"]["id"],),
        )
        == 1
        and sql_value(
            dsn,
            "SELECT COUNT(*) FROM organization_invite_accept_receipts WHERE invite_id=%s",
            (concurrent_invite["invite"]["id"],),
        )
        == 1,
        "onboarding_20_way_same_request_creates_one_operator_membership_and_receipt",
        concurrent_results,
    )

    operator_user_id = next(iter(concurrent_user_ids))
    operator_membership_id = int(
        sql_value(
            dsn,
            "SELECT membership_id FROM organization_operator_accounts WHERE user_id=%s",
            (operator_user_id,),
        )
    )
    operator_version = int(
        sql_value(
            dsn,
            "SELECT version FROM organization_memberships WHERE id=%s",
            (operator_membership_id,),
        )
    )
    retired = set_member_status(
        resolve_identity(2, request_id="owner2-retire-operator"),
        membership_id=operator_membership_id,
        action="remove",
        reason="验证员工专用账号退出即停用",
    )
    verify.check(
        retired["status"] == "removed"
        and sql_value(dsn, "SELECT is_active FROM users WHERE id=%s", (operator_user_id,)) == 0
        and sql_value(
            dsn,
            "SELECT COUNT(*) FROM organization_operator_accounts WHERE user_id=%s AND status='retired'",
            (operator_user_id,),
        )
        == 1,
        "dedicated_operator_remove_rotates_permission_and_retires_login",
        retired,
    )

    summary = {
        "status": "passed",
        "passed": len(verify.passed),
        "failed": 0,
        "skipped": 0,
        "schema": urlsplit(dsn).query,
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "failed": 1,
                    "skipped": 0,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        raise
