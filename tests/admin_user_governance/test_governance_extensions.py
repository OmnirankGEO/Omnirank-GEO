from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
import os

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor
from fastapi import HTTPException, Request

from api.admin_user_governance_api import (
    admin_adjust_user_wallet,
    admin_change_channel_relationship,
    admin_reset_user_password,
)
from db.auth_db import verify_password
from scripts.backfill_safe_referral_commercial_bindings import (
    apply_safe_candidates,
    audit_candidates,
)
from schemas.admin_user_governance import (
    AdjustUserWalletRequest,
    ChangeChannelRelationshipRequest,
    ResetUserPasswordRequest,
)
from services.admin_user_governance import (
    GovernanceValidationError,
    GovernanceVersionConflict,
    adjust_user_wallet,
    change_channel_relationship,
    reset_user_password,
)
from services.commercial_service_routing import solidify_service_provider_invitation


DB_URL = os.environ["TEST_DATABASE_URL"]


def _one(sql: str, params=()):
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()


def _insert_user(user_id: int, *, agent_level: int, active: int = 1) -> None:
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users(id,username,display_name,is_active) VALUES (%s,%s,%s,%s)",
                (user_id, f"test-{user_id}", f"测试账号 {user_id}", active),
            )
            cur.execute(
                "INSERT INTO user_wallets(user_id,agent_level) VALUES (%s,%s)",
                (user_id, agent_level),
            )


def _request(user: dict) -> Request:
    request = Request({"type": "http", "method": "PUT", "path": "/", "headers": []})
    request.state.user = user
    return request


def test_service_provider_invitation_creates_only_an_ordinary_customer_binding():
    _insert_user(401, agent_level=0)
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            result = solidify_service_provider_invitation(cur, 401, 28, "RAW-INVITE-401")
    assert result["action"] == "inserted"
    agent_id, source, source_token = _one(
        "SELECT agent_user_id,binding_source,source_token FROM customer_agent_bindings "
        "WHERE customer_user_id=401"
    )
    assert (agent_id, source) == (28, "invite_code")
    assert source_token.startswith("sha256:")
    assert "RAW-INVITE-401" not in source_token

    _insert_user(402, agent_level=1)
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            result = solidify_service_provider_invitation(cur, 402, 28, "RAW-INVITE-402")
    assert result["action"] == "service_provider_requires_channel_relationship"
    assert _one(
        "SELECT COUNT(*) FROM customer_agent_bindings WHERE customer_user_id=402"
    ) == (0,)


def test_invitation_never_overwrites_an_existing_commercial_decision():
    _insert_user(403, agent_level=0)
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO customer_agent_bindings(customer_user_id,agent_user_id,binding_source) "
                "VALUES (403,123,'admin_manual')"
            )
            result = solidify_service_provider_invitation(cur, 403, 28, "RAW-INVITE-403")
    assert result["action"] == "existing_binding_preserved"
    assert _one(
        "SELECT agent_user_id,binding_source,dispute_status FROM customer_agent_bindings "
        "WHERE customer_user_id=403"
    ) == (123, "admin_manual", None)


def test_inactive_inviter_cannot_become_commercial_provider():
    _insert_user(404, agent_level=0)
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("UPDATE users SET is_active=0 WHERE id=28")
            result = solidify_service_provider_invitation(cur, 404, 28, "RAW-INVITE-404")
    assert result["action"] == "inviter_not_eligible"
    assert _one(
        "SELECT COUNT(*) FROM customer_agent_bindings WHERE customer_user_id=404"
    ) == (0,)


def test_invitation_does_not_change_service_principal_with_pending_order():
    _insert_user(412, agent_level=0)
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """INSERT INTO recharge_orders(
                       id,user_id,amount_cents,order_type,payment_status)
                   VALUES ('PENDING-INVITE-412',412,100,'customer_recharge','pending')"""
            )
            result = solidify_service_provider_invitation(cur, 412, 28, "INV-28")
            assert result == {"action": "pending_order_preserved"}

    assert _one(
        "SELECT COUNT(*) FROM customer_agent_bindings WHERE customer_user_id=412"
    ) == (0,)


def test_historical_referral_audit_only_selects_safe_ordinary_customers():
    _insert_user(405, agent_level=0)
    _insert_user(406, agent_level=0, active=0)
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO users(id,username,display_name,is_active) "
                "VALUES (407,'test-407','缺失钱包身份客户',1)"
            )
            cur.executemany(
                "INSERT INTO referral_links(referrer_id,referred_id,level) "
                "VALUES (28,%s,1)",
                [(405,), (406,), (407,)],
            )
            report = audit_candidates(cur)

    classifications = {
        item["customer_user_id"]: item["classification"] for item in report
    }
    assert classifications[405] == "safe_ordinary_unbound"
    assert classifications[406] == "customer_inactive"
    assert classifications[407] == "customer_identity_unavailable"


def test_historical_referral_apply_rechecks_stale_audit_without_creating_dispute():
    _insert_user(408, agent_level=0)
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO referral_links(referrer_id,referred_id,level) "
                "VALUES (28,408,1)"
            )
            report = audit_candidates(cur)
            candidate = next(
                item for item in report if item["customer_user_id"] == 408
            )
            assert candidate["classification"] == "safe_ordinary_unbound"

            # Simulate a canonical relationship writer winning after the
            # read-only report was produced but before repair execution.
            cur.execute(
                "INSERT INTO customer_agent_bindings("
                "customer_user_id,agent_user_id,binding_source) "
                "VALUES (408,123,'admin_manual')"
            )
            applied = apply_safe_candidates(
                cur,
                report,
                operator_user_id=1,
                reason="只修复无争议的历史普通客户归属",
                request_prefix="stale-audit-race",
            )

    assert applied == []
    assert candidate["classification"] == "binding_conflict"
    assert _one(
        "SELECT agent_user_id,binding_source,dispute_status,dispute_note "
        "FROM customer_agent_bindings WHERE customer_user_id=408"
    ) == (123, "admin_manual", None, None)
    assert _one(
        "SELECT COUNT(*) FROM admin_user_governance_audits "
        "WHERE subject_user_id=408"
    ) == (0,)


def test_historical_referral_apply_rechecks_identity_and_pending_orders():
    _insert_user(409, agent_level=0)
    _insert_user(410, agent_level=0)
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.executemany(
                "INSERT INTO referral_links(referrer_id,referred_id,level) "
                "VALUES (28,%s,1)",
                [(409,), (410,)],
            )
            report = audit_candidates(cur)
            candidates = {
                item["customer_user_id"]: item
                for item in report
                if item["customer_user_id"] in {409, 410}
            }
            assert {item["classification"] for item in candidates.values()} == {
                "safe_ordinary_unbound"
            }

            cur.execute("UPDATE user_wallets SET agent_level=1 WHERE user_id=409")
            cur.execute(
                "INSERT INTO recharge_orders("
                "id,user_id,amount_cents,order_type,payment_status) "
                "VALUES ('PENDING-410',410,100,'customer_recharge','pending')"
            )
            applied = apply_safe_candidates(
                cur,
                report,
                operator_user_id=1,
                reason="执行前再次核验身份与待支付订单",
                request_prefix="stale-subject-race",
            )

    assert applied == []
    assert candidates[409]["classification"] == (
        "service_provider_requires_explicit_channel_mapping"
    )
    assert candidates[410]["classification"] == "pending_order_blocked"
    assert _one(
        "SELECT COUNT(*) FROM customer_agent_bindings "
        "WHERE customer_user_id IN (409,410)"
    ) == (0,)
    assert _one(
        "SELECT COUNT(*) FROM admin_user_governance_audits "
        "WHERE subject_user_id IN (409,410)"
    ) == (0,)


def test_historical_referral_apply_requires_active_admin_operator():
    _insert_user(411, agent_level=0)
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO referral_links(referrer_id,referred_id,level) "
                "VALUES (28,411,1)"
            )
            report = audit_candidates(cur)
            with pytest.raises(PermissionError):
                apply_safe_candidates(
                    cur,
                    report,
                    operator_user_id=300,
                    reason="普通账号不能执行历史商业关系修复",
                    request_prefix="unauthorized-backfill",
                )

    assert _one(
        "SELECT COUNT(*) FROM customer_agent_bindings WHERE customer_user_id=411"
    ) == (0,)
    assert _one(
        "SELECT COUNT(*) FROM admin_user_governance_audits "
        "WHERE subject_user_id=411"
    ) == (0,)


def test_channel_relationship_change_is_atomic_versioned_and_audited():
    result = change_channel_relationship(
        200,
        123,
        12000,
        expected_version=1,
        reason="书面审批调整直属渠道和进货系数",
        operator_user_id=1,
        operator_username="admin_one",
        request_id="channel-change-200",
        ip_address="203.0.113.20",
    )
    assert result["version"] == 2
    assert result["after"] == {
        "channel_upstream_user_id": 123,
        "channel_mode": "upstream_channel",
        "cost_multiplier_bps": 12000,
    }
    assert _one(
        "SELECT upstream_channel_account_id,cost_multiplier_bps,status "
        "FROM channel_pricing_relationships WHERE buyer_dealer_id=200 "
        "AND status='active' AND effective_to IS NULL"
    ) == (123, 12000, "active")
    reason, request_id, ip_address, before_json, after_json = _one(
        "SELECT reason,request_id,ip_address,before_snapshot::text,after_snapshot::text "
        "FROM admin_user_governance_audits WHERE subject_user_id=200 "
        "AND scope='channel_relationship'"
    )
    assert reason == "书面审批调整直属渠道和进货系数"
    assert request_id == "channel-change-200"
    assert ip_address == "203.0.113.20"
    assert json.loads(before_json)["channel_upstream_user_id"] == 28
    assert json.loads(after_json)["cost_multiplier_bps"] == 12000


def test_channel_relationship_stale_version_cycle_and_ordinary_subject_write_nothing():
    with pytest.raises(GovernanceValidationError) as cycle:
        change_channel_relationship(
            28,
            200,
            11000,
            expected_version=1,
            reason="该操作会形成渠道环",
            operator_user_id=1,
            operator_username="admin_one",
            request_id="channel-cycle-28",
            ip_address="127.0.0.1",
        )
    assert cycle.value.code == "CHANNEL_RELATIONSHIP_INVALID"

    with pytest.raises(GovernanceValidationError) as ordinary:
        change_channel_relationship(
            300,
            28,
            11000,
            expected_version=1,
            reason="普通用户不得写渠道关系",
            operator_user_id=1,
            operator_username="admin_one",
            request_id="channel-ordinary-300",
            ip_address="127.0.0.1",
        )
    assert ordinary.value.code == "CHANNEL_SUBJECT_MUST_BE_SERVICE_PROVIDER"

    change_channel_relationship(
        200,
        123,
        12000,
        expected_version=1,
        reason="先提交一条新关系",
        operator_user_id=1,
        operator_username="admin_one",
        request_id="channel-first-200",
        ip_address="127.0.0.1",
    )
    with pytest.raises(GovernanceVersionConflict):
        change_channel_relationship(
            200,
            102,
            13000,
            expected_version=1,
            reason="旧页面不能覆盖新关系",
            operator_user_id=2,
            operator_username="admin_two",
            request_id="channel-stale-200",
            ip_address="127.0.0.2",
        )
    assert _one(
        "SELECT upstream_channel_account_id,cost_multiplier_bps FROM "
        "channel_pricing_relationships WHERE buyer_dealer_id=200 "
        "AND status='active' AND effective_to IS NULL"
    ) == (123, 12000)
    assert _one(
        "SELECT COUNT(*) FROM admin_user_governance_audits "
        "WHERE request_id IN ('channel-cycle-28','channel-ordinary-300','channel-stale-200')"
    ) == (0,)


def test_twenty_concurrent_channel_edits_have_one_winner():
    def write(index: int):
        try:
            return change_channel_relationship(
                200,
                123 if index % 2 else 102,
                11001 + index,
                expected_version=1,
                reason=f"并发渠道治理请求 {index}",
                operator_user_id=1,
                operator_username="admin_one",
                request_id=f"channel-race-{index}",
                ip_address="127.0.0.1",
            )
        except GovernanceVersionConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(write, range(20)))
    assert sum(isinstance(item, dict) for item in results) == 1
    assert sum(isinstance(item, GovernanceVersionConflict) for item in results) == 19
    assert _one(
        "SELECT COUNT(*) FROM admin_user_governance_audits "
        "WHERE subject_user_id=200 AND scope='channel_relationship'"
    ) == (1,)
    assert _one(
        "SELECT version FROM admin_user_governance_versions "
        "WHERE subject_user_id=200 AND scope='channel_relationship'"
    ) == (2,)


def test_password_reset_hashes_secret_invalidates_sessions_and_never_audits_secret():
    secret = "New-Admin-Only-Password-2026"
    result = reset_user_password(
        124,
        secret,
        expected_version=1,
        reason="用户完成身份核验后由最高管理员重置密码",
        operator_user_id=1,
        operator_username="admin_one",
        request_id="password-reset-124",
        ip_address="198.51.100.24",
    )
    assert result["version"] == 2
    assert secret not in json.dumps(result, ensure_ascii=False)
    password_hash, must_change, permission_version = _one(
        "SELECT password_hash,must_change_password,permission_version FROM users WHERE id=124"
    )
    assert password_hash != secret
    assert verify_password(secret, password_hash)
    assert (must_change, permission_version) == (1, 2)

    audit_text, evidence_text = _one(
        "SELECT (before_snapshot::text || after_snapshot::text || reason),evidence_jsonb::text "
        "FROM admin_user_governance_audits WHERE request_id='password-reset-124'"
    )
    outbox_text = _one(
        "SELECT payload::text FROM notification_outbox "
        "WHERE business_id='user:124:password_security:2'"
    )[0]
    for material in (secret, password_hash):
        assert material not in audit_text
        assert material not in evidence_text
        assert material not in outbox_text


def test_password_reset_stale_version_rolls_back_without_secret_leakage():
    first_secret = "First-Password-Reset-2026"
    stale_secret = "Stale-Password-Must-Not-Win-2026"
    reset_user_password(
        124,
        first_secret,
        expected_version=1,
        reason="首次合法重置",
        operator_user_id=1,
        operator_username="admin_one",
        request_id="password-first-124",
        ip_address="127.0.0.1",
    )
    with pytest.raises(GovernanceVersionConflict) as exc:
        reset_user_password(
            124,
            stale_secret,
            expected_version=1,
            reason="旧页面不得覆盖新密码",
            operator_user_id=2,
            operator_username="admin_two",
            request_id="password-stale-124",
            ip_address="127.0.0.2",
        )
    assert stale_secret not in str(exc.value)
    password_hash = _one("SELECT password_hash FROM users WHERE id=124")[0]
    assert verify_password(first_secret, password_hash)
    assert not verify_password(stale_secret, password_hash)
    assert _one(
        "SELECT COUNT(*) FROM admin_user_governance_audits "
        "WHERE request_id='password-stale-124'"
    ) == (0,)


def test_password_reset_rejects_overlong_utf8_without_hash_or_audit_write():
    before_hash = _one("SELECT password_hash FROM users WHERE id=124")[0]
    with pytest.raises(GovernanceValidationError) as exc:
        reset_user_password(
            124,
            "密" * 25,
            expected_version=1,
            reason="拒绝超过密码哈希后端字节上限的输入",
            operator_user_id=1,
            operator_username="admin_one",
            request_id="password-overlong-124",
            ip_address="127.0.0.1",
        )
    assert exc.value.code == "PASSWORD_POLICY_INVALID"
    assert _one("SELECT password_hash FROM users WHERE id=124")[0] == before_hash
    assert _one(
        "SELECT COUNT(*) FROM admin_user_governance_audits "
        "WHERE request_id='password-overlong-124'"
    ) == (0,)


def test_twenty_concurrent_password_resets_have_one_winner_and_one_hash():
    secrets = [f"Concurrent-Password-{index:02d}-2026" for index in range(20)]

    def write(index: int):
        try:
            return reset_user_password(
                124,
                secrets[index],
                expected_version=1,
                reason=f"并发密码重置核验 {index}",
                operator_user_id=1,
                operator_username="admin_one",
                request_id=f"password-race-{index}",
                ip_address="127.0.0.1",
            )
        except GovernanceVersionConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(write, range(20)))
    winners = [index for index, item in enumerate(results) if isinstance(item, dict)]
    assert len(winners) == 1
    assert sum(isinstance(item, GovernanceVersionConflict) for item in results) == 19
    password_hash, permission_version = _one(
        "SELECT password_hash,permission_version FROM users WHERE id=124"
    )
    assert verify_password(secrets[winners[0]], password_hash)
    assert sum(verify_password(secret, password_hash) for secret in secrets) == 1
    assert permission_version == 2
    assert _one(
        "SELECT COUNT(*) FROM admin_user_governance_audits "
        "WHERE subject_user_id=124 AND scope='password_security'"
    ) == (1,)


@pytest.mark.parametrize("operation", ["channel", "password", "wallet"])
def test_new_governance_mutations_are_admin_only(operation: str):
    request = _request({"id": 300, "username": "ordinary", "is_admin": False})
    with pytest.raises(HTTPException) as exc:
        if operation == "channel":
            asyncio.run(admin_change_channel_relationship(
                200,
                ChangeChannelRelationshipRequest(
                    expected_version=1,
                    reason="非管理员不得调整渠道关系",
                    upstream_user_id=123,
                    cost_multiplier_bps=12000,
                ),
                request,
            ))
        elif operation == "password":
            asyncio.run(admin_reset_user_password(
                124,
                ResetUserPasswordRequest(
                    expected_version=1,
                    reason="非管理员不得重置密码",
                    new_password="Must-Not-Be-Applied-2026",
                ),
                request,
            ))
        else:
            asyncio.run(admin_adjust_user_wallet(
                124,
                AdjustUserWalletRequest(
                    expected_version=1,
                    reason="非管理员不得校正用户算力",
                    point_type="paid",
                    operation="add",
                    amount=100,
                ),
                request,
            ))
    assert exc.value.status_code == 403
    assert _one("SELECT password_hash FROM users WHERE id=124") == ("test",)
    assert _one(
        "SELECT COUNT(*) FROM admin_user_governance_audits "
        "WHERE scope IN ('channel_relationship','password_security','wallet_adjustment')"
    ) == (0,)


@pytest.mark.parametrize(
    ("point_type", "operation", "amount", "expected_paid", "expected_bonus", "delta"),
    [
        ("paid", "add", 300, 1424, 50, 300),
        ("paid", "deduct", 300, 824, 50, -300),
        ("paid", "set", 13000, 13000, 50, 11876),
        ("bonus", "add", 300, 1124, 350, 300),
        ("bonus", "deduct", 30, 1124, 20, -30),
        ("bonus", "set", 1300, 1124, 1300, 1250),
    ],
)
def test_wallet_adjustment_is_one_non_revenue_transaction_with_complete_evidence(
    point_type, operation, amount, expected_paid, expected_bonus, delta,
):
    before_recharged = _one(
        "SELECT total_recharged FROM user_wallets WHERE user_id=124"
    )[0]
    result = adjust_user_wallet(
        124,
        point_type=point_type,
        operation=operation,
        amount=amount,
        expected_version=1,
        reason=f"核验支付与账本后执行 {operation} 校正",
        operator_user_id=1,
        operator_username="admin_one",
        request_id=f"wallet-{point_type}-{operation}",
        ip_address="203.0.113.19",
    )
    assert result["scope"] == "wallet_adjustment"
    assert result["version"] == 2
    assert _one(
        "SELECT paid_points,bonus_points,total_recharged FROM user_wallets WHERE user_id=124"
    ) == (expected_paid, expected_bonus, before_recharged)
    assert _one(
        "SELECT type,point_type,amount,balance_after,order_id,source "
        "FROM point_transactions WHERE user_id=124 AND type='admin_adjust'"
    ) == (
        "admin_adjust",
        point_type,
        delta,
        expected_paid if point_type == "paid" else expected_bonus,
        f"admin-governance:wallet-{point_type}-{operation}",
        "admin_governance",
    )
    scope, operator_id, request_id, ip_address, before, after, evidence = _one(
        "SELECT scope,operator_user_id,request_id,ip_address,before_snapshot,"
        "after_snapshot,evidence_jsonb FROM admin_user_governance_audits "
        "WHERE subject_user_id=124 AND scope='wallet_adjustment'"
    )
    assert (scope, operator_id, request_id, ip_address) == (
        "wallet_adjustment", 1, f"wallet-{point_type}-{operation}", "203.0.113.19"
    )
    assert before == {"paid_points": 1124, "bonus_points": 50}
    assert after == {"paid_points": expected_paid, "bonus_points": expected_bonus}
    assert evidence["accounting_class"] == "non_revenue_admin_correction"
    assert evidence["total_recharged_unchanged"] is True
    assert evidence["delta_points"] == delta
    assert _one(
        "SELECT COUNT(*) FROM audit_logs WHERE user_id=1 "
        "AND module='admin_user_governance' AND entity_id=124"
    ) == (1,)


@pytest.mark.parametrize(
    ("point_type", "operation", "amount", "code"),
    [
        ("paid", "deduct", 999999, "WALLET_BALANCE_INSUFFICIENT"),
        ("paid", "set", 1124, "NO_CHANGE"),
        ("paid", "add", 0, "WALLET_AMOUNT_INVALID"),
        ("invalid", "add", 1, "WALLET_POINT_TYPE_INVALID"),
        ("paid", "invalid", 1, "WALLET_OPERATION_INVALID"),
    ],
)
def test_rejected_wallet_adjustment_is_zero_write(point_type, operation, amount, code):
    before = _one(
        "SELECT paid_points,bonus_points,total_recharged FROM user_wallets WHERE user_id=124"
    )
    with pytest.raises(GovernanceValidationError) as exc:
        adjust_user_wallet(
            124,
            point_type=point_type,
            operation=operation,
            amount=amount,
            expected_version=1,
            reason="拒绝无效算力校正请求",
            operator_user_id=1,
            operator_username="admin_one",
            request_id=f"wallet-reject-{code}",
            ip_address="203.0.113.20",
        )
    assert exc.value.code == code
    assert _one(
        "SELECT paid_points,bonus_points,total_recharged FROM user_wallets WHERE user_id=124"
    ) == before
    assert _one("SELECT COUNT(*) FROM point_transactions WHERE type='admin_adjust'") == (0,)
    assert _one("SELECT COUNT(*) FROM admin_user_governance_audits WHERE scope='wallet_adjustment'") == (0,)
    assert _one("SELECT COUNT(*) FROM admin_user_governance_versions WHERE scope='wallet_adjustment'") == (0,)


def test_twenty_concurrent_wallet_adjustments_have_one_winner():
    def write(index: int):
        try:
            return adjust_user_wallet(
                124,
                point_type="paid",
                operation="add",
                amount=100 + index,
                expected_version=1,
                reason=f"并发管理员算力校正核验 {index}",
                operator_user_id=1,
                operator_username="admin_one",
                request_id=f"wallet-race-{index}",
                ip_address="203.0.113.21",
            )
        except GovernanceVersionConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(write, range(20)))
    winners = [item for item in results if isinstance(item, dict)]
    assert len(winners) == 1
    assert sum(isinstance(item, GovernanceVersionConflict) for item in results) == 19
    delta = winners[0]["after"]["paid_points"] - winners[0]["before"]["paid_points"]
    assert 100 <= delta <= 119
    assert _one("SELECT paid_points FROM user_wallets WHERE user_id=124") == (1124 + delta,)
    assert _one("SELECT COUNT(*) FROM point_transactions WHERE type='admin_adjust'") == (1,)
    assert _one("SELECT COUNT(*) FROM admin_user_governance_audits WHERE scope='wallet_adjustment'") == (1,)
    assert _one(
        "SELECT version FROM admin_user_governance_versions "
        "WHERE subject_user_id=124 AND scope='wallet_adjustment'"
    ) == (2,)
