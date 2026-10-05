from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import psycopg2
import pytest
from fastapi import HTTPException, Request

from api.admin_user_governance_api import (
    admin_change_business_identity,
    admin_change_commercial_service_binding,
    admin_change_platform_access,
    admin_platform_direct_readiness,
    admin_user_governance_detail,
    admin_user_governance_list,
)
from api.admin_w4_api import DisputePatchRequest, resolve_dispute
from schemas.admin_user_governance import (
    AdminUserDetailResponse,
    AdminUserListResponse,
    ChangeBusinessIdentityRequest,
    ChangeCommercialBindingRequest,
    ChangePlatformAccessRequest,
    GovernanceMutationResponse,
    PlatformDirectReadinessResponse,
)
from services.admin_user_governance import (
    GovernanceValidationError,
    GovernanceVersionConflict,
    change_business_identity,
    change_commercial_binding,
    change_platform_access,
    get_admin_user_detail,
    get_platform_direct_readiness,
    list_admin_users,
)


DB_URL = os.environ["TEST_DATABASE_URL"]


def _query_one(sql: str, params=()):
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()


def _request(user: dict) -> Request:
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    request.state.user = user
    return request


def test_admin_dtos_validate_and_list_has_two_business_identities():
    result = list_admin_users(page=1, page_size=100)
    parsed = AdminUserListResponse.model_validate(result)
    assert {user.business_identity for user in parsed.users} == {"ordinary_user", "service_provider"}
    assert next(user for user in parsed.users if user.user_id == 1).platform_access == "administrator"
    assert next(user for user in parsed.users if user.user_id == 124).needs_attention is True
    # List labels follow the account's operating route: service providers belong
    # to the channel supply chain even without an ordinary-customer binding.
    assert next(user for user in parsed.users if user.user_id == 28).service_mode == "service_provider"


@pytest.mark.parametrize("user_id", [129, 130, 131])
def test_dual_relationships_are_displayed_as_independent_facts_without_repair(user_id):
    before = _query_one(
        "SELECT referrer_id FROM referral_links WHERE referred_id=%s AND level=1", (user_id,)
    )
    detail = AdminUserDetailResponse.model_validate(get_admin_user_detail(user_id))
    assert detail.relationships.registration.inviter.user_id == 102
    assert detail.relationships.commercial.provider.user_id == 28
    assert detail.relationships.dual_relationships_present is True
    assert detail.relationships.dual_relationships_label == "来源与当前服务关系均已记录"
    assert any(n.code == "REGISTRATION_AND_SERVICE_RECORDED" for n in detail.relationships.notices)
    after = _query_one(
        "SELECT referrer_id FROM referral_links WHERE referred_id=%s AND level=1", (user_id,)
    )
    assert before == after == (102,)


def test_admin_manual_binding_30_is_explicitly_marked_incomplete():
    detail = AdminUserDetailResponse.model_validate(get_admin_user_detail(124))
    commercial = detail.relationships.commercial
    assert commercial.binding_id == 30
    assert commercial.provider.user_id == 123
    assert commercial.evidence.status == "incomplete"
    assert commercial.evidence.label == "旧人工归属缺少直接操作凭证"
    assert any(n.code == "ADMIN_MANUAL_EVIDENCE_INCOMPLETE" for n in detail.relationships.notices)


def test_w4_keep_old_starts_a_new_evidenced_version_without_losing_source(monkeypatch):
    """A dispute decision closes the original source fact and evidences the new current version."""
    from db import dispute_escrow_db

    monkeypatch.setattr(dispute_escrow_db, "list_held_for_dispute", lambda cur, dispute_id: [])
    monkeypatch.setattr(dispute_escrow_db, "settle_escrow", lambda *args, **kwargs: True)
    monkeypatch.setattr(dispute_escrow_db, "refund_escrow", lambda *args, **kwargs: True)
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE customer_agent_bindings SET dispute_status='pending',dispute_note='待裁决' "
                "WHERE customer_user_id=129"
            )
            cur.execute(
                """INSERT INTO customer_agent_binding_disputes(
                       id,customer_user_id,old_agent_user_id,new_agent_user_id,status,note)
                   VALUES (901,129,28,200,'pending','测试冲突')"""
            )

    result = asyncio.run(resolve_dispute(
        901,
        DisputePatchRequest(action="keep_old", note="核验原服务协议后保留现有承接方"),
        _request({"user_id": 1, "username": "admin_one", "is_admin": True}),
    ))
    assert result["commercial_binding_version"] == 2
    assert _query_one(
        "SELECT COUNT(*) FROM notification_outbox "
        "WHERE event_type='dispute.resolved' AND recipient_user_id=129"
    ) == (1,)
    assert _query_one(
        "SELECT COUNT(*) FROM notification_outbox "
        "WHERE event_type='dispute.resolved' AND recipient_user_id IN (1,2)"
    ) == (2,)

    projection = _query_one(
        """SELECT id,agent_user_id,binding_source,source_token,dispute_status
           FROM customer_agent_bindings WHERE customer_user_id=129"""
    )
    assert projection[1:] == (28, "admin_manual", None, None)
    closed = _query_one(
        """SELECT provider_user_id,binding_source,source_token,effective_to IS NOT NULL
           FROM customer_agent_binding_history
           WHERE customer_user_id=129 AND effective_to IS NOT NULL"""
    )
    assert closed == (28, "invite_code", "INV-28", True)
    current = _query_one(
        """SELECT provider_user_id,binding_source,source_token,created_reason
           FROM customer_agent_binding_history
           WHERE customer_user_id=129 AND effective_to IS NULL"""
    )
    assert current == (28, "admin_manual", None, "核验原服务协议后保留现有承接方")
    detail = AdminUserDetailResponse.model_validate(get_admin_user_detail(129))
    assert detail.relationships.commercial.evidence.status == "complete"
    assert detail.relationships.commercial.evidence.reason == "核验原服务协议后保留现有承接方"


def test_change_commercial_binding_is_atomic_audited_and_does_not_touch_referral():
    result = change_commercial_binding(
        129, 200, expected_version=1, reason="客户签署新的服务承接确认",
        operator_user_id=1, operator_username="admin_one", request_id="binding-change-129",
        ip_address="127.0.0.1",
    )
    assert result["version"] == 2
    assert result["before"]["commercial_provider_user_id"] == 28
    assert result["after"]["commercial_provider_user_id"] == 200
    assert _query_one("SELECT referrer_id FROM referral_links WHERE referred_id=129") == (102,)
    assert _query_one("SELECT agent_user_id FROM customer_agent_bindings WHERE customer_user_id=129") == (200,)
    audit = _query_one(
        """SELECT reason,version_before,version_after,
                  evidence_jsonb->>'registration_attribution_untouched'
           FROM admin_user_governance_audits WHERE request_id='binding-change-129'"""
    )
    assert audit == ("客户签署新的服务承接确认", 1, 2, "true")
    assert _query_one(
        "SELECT COUNT(*) FROM audit_logs WHERE entity_id=129 AND module='admin_user_governance'"
    ) == (1,)
    detail = AdminUserDetailResponse.model_validate(get_admin_user_detail(129))
    assert detail.relationships.commercial.evidence.status == "complete"
    assert next(user for user in AdminUserListResponse.model_validate(
        list_admin_users(page=1, page_size=100)
    ).users if user.user_id == 129).needs_attention is False


def test_old_binding_audit_is_not_reused_after_an_unaudited_rebind():
    change_commercial_binding(
        129, 200, expected_version=1, reason="先生成一条合法直接证据",
        operator_user_id=1, operator_username="admin_one", request_id="binding-direct-old",
        ip_address="127.0.0.1",
    )
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE customer_agent_bindings
                   SET agent_user_id=123, binding_source='admin_manual',
                       bound_at='2026-07-15 18:00:00', admin_override_user_id=NULL,
                       admin_override_at=NULL
                   WHERE customer_user_id=129"""
            )
    detail = AdminUserDetailResponse.model_validate(get_admin_user_detail(129))
    assert detail.relationships.commercial.provider.user_id == 123
    assert detail.relationships.commercial.evidence.status == "incomplete"
    item = next(user for user in AdminUserListResponse.model_validate(
        list_admin_users(page=1, page_size=100)
    ).users if user.user_id == 129)
    assert item.needs_attention is True


def test_time_correlated_legacy_audit_is_a_lead_not_complete_evidence():
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO audit_logs(
                       user_id,username,action,module,entity_type,entity_id,summary,created_at)
                   VALUES (1,'admin_one','update','legacy','user',124,
                           '人工调整绑定，历史摘要仅作关联线索','2026-07-14 09:03:00')"""
            )
    detail = AdminUserDetailResponse.model_validate(get_admin_user_detail(124))
    assert detail.relationships.commercial.evidence.status == "related"
    assert "非直接外键证据" in detail.relationships.commercial.evidence.label
    item = next(user for user in AdminUserListResponse.model_validate(
        list_admin_users(page=1, page_size=100)
    ).users if user.user_id == 124)
    assert item.needs_attention is True


def test_concurrent_admin_binding_edits_only_one_succeeds():
    def write(provider: int):
        try:
            return change_commercial_binding(
                130, provider, expected_version=1, reason=f"并发核验切换到 {provider}",
                operator_user_id=1 if provider == 200 else 2,
                operator_username="admin_one" if provider == 200 else "admin_two",
                request_id=f"concurrent-binding-{provider}", ip_address="127.0.0.1",
            )
        except GovernanceVersionConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [200, 123]))
    assert sum(isinstance(result, GovernanceVersionConflict) for result in results) == 1
    assert sum(isinstance(result, dict) for result in results) == 1
    assert _query_one(
        "SELECT COUNT(*) FROM admin_user_governance_audits WHERE subject_user_id=130"
    ) == (1,)
    assert _query_one(
        "SELECT version FROM admin_user_governance_versions WHERE subject_user_id=130 AND scope='commercial_binding'"
    ) == (2,)


def test_invalid_provider_rolls_back_version_binding_and_audit():
    with pytest.raises(GovernanceValidationError) as exc:
        change_commercial_binding(
            131, 999, expected_version=1, reason="无效服务商应整体回滚",
            operator_user_id=1, operator_username="admin_one",
            request_id="invalid-provider", ip_address="127.0.0.1",
        )
    assert exc.value.code == "PROVIDER_UNAVAILABLE"
    assert _query_one("SELECT agent_user_id FROM customer_agent_bindings WHERE customer_user_id=131") == (28,)
    assert _query_one("SELECT COUNT(*) FROM admin_user_governance_versions WHERE subject_user_id=131") == (0,)
    assert _query_one("SELECT COUNT(*) FROM admin_user_governance_audits WHERE subject_user_id=131") == (0,)


@pytest.mark.parametrize("provider_kind", ["administrator", "missing_service_scope"])
def test_commercial_assignment_uses_the_canonical_provider_contract(provider_kind):
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            if provider_kind == "administrator":
                cur.execute("UPDATE user_wallets SET agent_level=1 WHERE user_id=1")
                cur.execute(
                    "INSERT INTO public_account_codes(user_id,service_account_code) "
                    "VALUES (1,'SV-ADMIN001')"
                )
                provider_id = 1
            else:
                cur.execute("DELETE FROM public_account_codes WHERE user_id=102")
                provider_id = 102
    with pytest.raises(GovernanceValidationError) as exc:
        change_commercial_binding(
            129,
            provider_id,
            expected_version=1,
            reason="不合格承接方必须在写关系前被拒绝",
            operator_user_id=1,
            operator_username="admin_one",
            request_id=f"invalid-canonical-provider-{provider_kind}",
            ip_address="127.0.0.1",
        )
    assert exc.value.code == "PROVIDER_UNAVAILABLE"
    assert _query_one(
        "SELECT agent_user_id FROM customer_agent_bindings WHERE customer_user_id=129"
    ) == (28,)
    assert _query_one(
        "SELECT COUNT(*) FROM admin_user_governance_audits WHERE request_id=%s",
        (f"invalid-canonical-provider-{provider_kind}",),
    ) == (0,)


def test_w4_reassign_rejects_service_provider_subject_with_zero_writes():
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO customer_agent_bindings(
                       customer_user_id,agent_user_id,binding_source,bound_at,dispute_status)
                   VALUES (102,123,'admin_manual',NOW(),'pending')"""
            )
            cur.execute(
                """INSERT INTO customer_agent_binding_disputes(
                       id,customer_user_id,old_agent_user_id,new_agent_user_id,status)
                   VALUES (9102,102,123,200,'pending')"""
            )
    request = _request({"user_id": 1, "username": "admin_one", "is_admin": True})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(resolve_dispute(
            9102,
            DisputePatchRequest(action="reassign", note="服务商主体不得写客户商业绑定"),
            request,
        ))
    assert exc.value.status_code == 422
    assert _query_one(
        "SELECT agent_user_id,dispute_status FROM customer_agent_bindings "
        "WHERE customer_user_id=102"
    ) == (123, "pending")
    assert _query_one(
        "SELECT status FROM customer_agent_binding_disputes WHERE id=9102"
    ) == ("pending",)
    assert _query_one(
        "SELECT COUNT(*) FROM admin_user_governance_audits WHERE subject_user_id=102"
    ) == (0,)


def test_w4_reassign_rejects_pending_order_with_zero_writes():
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE customer_agent_bindings SET dispute_status='pending' "
                "WHERE customer_user_id=129"
            )
            cur.execute(
                """INSERT INTO customer_agent_binding_disputes(
                       id,customer_user_id,old_agent_user_id,new_agent_user_id,status)
                   VALUES (9129,129,28,200,'pending')"""
            )
            cur.execute(
                """INSERT INTO recharge_orders(
                       id,user_id,agent_user_id,amount_cents,order_type,payment_status)
                   VALUES ('W4-PENDING-129',129,28,10000,'customer_recharge','pending')"""
            )
    request = _request({"user_id": 1, "username": "admin_one", "is_admin": True})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(resolve_dispute(
            9129,
            DisputePatchRequest(action="reassign", note="待支付订单存在时不得改派"),
            request,
        ))
    assert exc.value.status_code == 409
    assert _query_one(
        "SELECT agent_user_id,dispute_status FROM customer_agent_bindings "
        "WHERE customer_user_id=129"
    ) == (28, "pending")
    assert _query_one(
        "SELECT status FROM customer_agent_binding_disputes WHERE id=9129"
    ) == ("pending",)
    assert _query_one(
        "SELECT COUNT(*) FROM admin_user_governance_audits WHERE subject_user_id=129"
    ) == (0,)


def test_w4_reassign_reuses_canonical_provider_readiness_with_zero_writes():
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM public_account_codes WHERE user_id=102")
            cur.execute(
                "UPDATE customer_agent_bindings SET dispute_status='pending' "
                "WHERE customer_user_id=129"
            )
            cur.execute(
                """INSERT INTO customer_agent_binding_disputes(
                       id,customer_user_id,old_agent_user_id,new_agent_user_id,status)
                   VALUES (9229,129,28,102,'pending')"""
            )
    request = _request({"user_id": 1, "username": "admin_one", "is_admin": True})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(resolve_dispute(
            9229,
            DisputePatchRequest(action="reassign", note="无价目范围的服务商不得承接"),
            request,
        ))
    assert exc.value.status_code == 409
    assert _query_one(
        "SELECT agent_user_id,dispute_status FROM customer_agent_bindings "
        "WHERE customer_user_id=129"
    ) == (28, "pending")
    assert _query_one(
        "SELECT status FROM customer_agent_binding_disputes WHERE id=9229"
    ) == ("pending",)
    assert _query_one(
        "SELECT COUNT(*) FROM admin_user_governance_audits WHERE subject_user_id=129"
    ) == (0,)


def test_duplicate_request_id_rolls_back_binding_version_and_audit():
    change_commercial_binding(
        129, 200, expected_version=1, reason="先生成一条占用请求编号的合法审计",
        operator_user_id=1, operator_username="admin_one", request_id="duplicate-request-id",
        ip_address="127.0.0.1",
    )
    with pytest.raises(psycopg2.errors.UniqueViolation):
        change_commercial_binding(
            130, 200, expected_version=1, reason="重复请求编号必须整体回滚",
            operator_user_id=1, operator_username="admin_one", request_id="duplicate-request-id",
            ip_address="127.0.0.1",
        )
    assert _query_one(
        "SELECT agent_user_id FROM customer_agent_bindings WHERE customer_user_id=130"
    ) == (28,)
    assert _query_one(
        "SELECT COUNT(*) FROM admin_user_governance_versions "
        "WHERE subject_user_id=130 AND scope='commercial_binding'"
    ) == (0,)
    assert _query_one(
        "SELECT COUNT(*) FROM admin_user_governance_audits "
        "WHERE request_id='duplicate-request-id'"
    ) == (1,)


def test_business_identity_and_platform_access_are_independent():
    identity = change_business_identity(
        300, "service_provider", expected_version=1, reason="签约成为服务商",
        operator_user_id=1, operator_username="admin_one", request_id="identity-300",
        ip_address="127.0.0.1",
    )
    access = change_platform_access(
        300, True, expected_version=1, reason="承担内部运营管理职责",
        operator_user_id=1, operator_username="admin_one", request_id="access-300",
        ip_address="127.0.0.1",
    )
    assert identity["version"] == 2 and access["version"] == 2
    detail = get_admin_user_detail(300)
    assert detail["overview"]["business_identity"] == "service_provider"
    assert detail["overview"]["platform_access"] == "administrator"
    # Legacy roles are preserved instead of replaced.
    role_ids = _query_one("SELECT COUNT(*) FROM user_roles WHERE user_id=300")
    assert role_ids == (2,)


def test_last_platform_admin_role_census_is_serialized():
    start = Event()

    def remove_admin(target: int, operator: int):
        start.wait(timeout=5)
        try:
            return change_platform_access(
                target, False, expected_version=1,
                reason="并发移除平台管理员权限",
                operator_user_id=operator,
                operator_username=f"admin_{operator}",
                request_id=f"concurrent-remove-admin-{target}",
                ip_address="127.0.0.1",
            )
        except GovernanceValidationError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(remove_admin, 1, 2),
            pool.submit(remove_admin, 2, 1),
        ]
        start.set()
        results = [future.result(timeout=10) for future in futures]

    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("LAST_ADMIN") == 1
    assert _query_one(
        """SELECT COUNT(*) FROM users u JOIN user_roles ur ON ur.user_id=u.id
           JOIN roles r ON r.id=ur.role_id
           WHERE r.name='admin' AND COALESCE(u.is_active,1)=1"""
    ) == (1,)


def test_admin_grant_cannot_invalidate_an_active_commercial_provider():
    change_commercial_binding(
        300, 200, expected_version=1, reason="先建立显式商业服务关系",
        operator_user_id=1, operator_username="admin_one", request_id="bind-300-200-before-admin",
        ip_address="127.0.0.1",
    )
    with pytest.raises(GovernanceValidationError) as exc:
        change_platform_access(
            200, True, expected_version=1, reason="承接客户期间不得改为管理员",
            operator_user_id=1, operator_username="admin_one", request_id="admin-provider-200",
            ip_address="127.0.0.1",
        )
    assert exc.value.code == "ACTIVE_COMMERCIAL_PROVIDER"
    assert _query_one(
        "SELECT COUNT(*) FROM user_roles WHERE user_id=200 AND role_id=1"
    ) == (0,)
    assert _query_one(
        "SELECT agent_user_id FROM customer_agent_bindings WHERE customer_user_id=300"
    ) == (200,)
    assert _query_one(
        "SELECT COUNT(*) FROM admin_user_governance_audits WHERE request_id='admin-provider-200'"
    ) == (0,)


def test_admin_grant_cannot_disable_platform_direct_service(monkeypatch):
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", "200")
    with pytest.raises(GovernanceValidationError) as exc:
        change_platform_access(
            200, True, expected_version=1, reason="直营承接账号必须保持非管理员",
            operator_user_id=1, operator_username="admin_one", request_id="admin-direct-200",
            ip_address="127.0.0.1",
        )
    assert exc.value.code == "ACTIVE_COMMERCIAL_PROVIDER"
    assert _query_one(
        "SELECT COUNT(*) FROM user_roles WHERE user_id=200 AND role_id=1"
    ) == (0,)
    assert _query_one(
        "SELECT COUNT(*) FROM admin_user_governance_versions "
        "WHERE subject_user_id=200 AND scope='platform_access'"
    ) == (0,)


def test_admin_grant_and_new_binding_serialize_to_one_valid_outcome():
    start = Event()

    def grant_admin():
        start.wait()
        try:
            return change_platform_access(
                200, True, expected_version=1, reason="并发授予平台管理员",
                operator_user_id=1, operator_username="admin_one", request_id="race-admin-200",
                ip_address="127.0.0.1",
            )
        except GovernanceValidationError as exc:
            return exc

    def bind_customer():
        start.wait()
        try:
            return change_commercial_binding(
                300, 200, expected_version=1, reason="并发建立商业服务关系",
                operator_user_id=2, operator_username="admin_two", request_id="race-bind-300-200",
                ip_address="127.0.0.1",
            )
        except GovernanceValidationError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(grant_admin), pool.submit(bind_customer)]
        start.set()
        results = [future.result(timeout=10) for future in futures]
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, GovernanceValidationError) for result in results) == 1
    is_admin = _query_one(
        "SELECT EXISTS(SELECT 1 FROM user_roles WHERE user_id=200 AND role_id=1)"
    )[0]
    binding = _query_one(
        "SELECT agent_user_id FROM customer_agent_bindings WHERE customer_user_id=300"
    )
    assert not (is_admin and binding == (200,))


def test_provider_lifecycle_binding_user_order_serializes_downgrade(monkeypatch):
    """Admin/W4 holds lifecycle+binding; downgrade must wait before user lock."""
    from services import commercial_service_routing as routing

    reached_lifecycle = Event()
    original_lock = routing.lock_commercial_provider_lifecycle

    def observed_lock(cur, provider_user_id):
        reached_lifecycle.set()
        return original_lock(cur, provider_user_id)

    monkeypatch.setattr(routing, "lock_commercial_provider_lifecycle", observed_lock)

    def downgrade():
        try:
            return change_business_identity(
                28, "ordinary_user", expected_version=1,
                reason="并发降级必须遵守承接方锁序",
                operator_user_id=2, operator_username="admin_two",
                request_id="race-demote-provider-28", ip_address="127.0.0.1",
            )
        except GovernanceValidationError as exc:
            return exc

    with psycopg2.connect(DB_URL) as admin_conn:
        with admin_conn.cursor() as admin_cur:
            original_lock(admin_cur, 28)
            admin_cur.execute(
                "SELECT id FROM customer_agent_bindings "
                "WHERE agent_user_id=28 ORDER BY id FOR UPDATE"
            )
            admin_cur.fetchall()
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(downgrade)
                assert reached_lifecycle.wait(timeout=5)
                # With the unified order the downgrade is waiting on lifecycle,
                # so this provider user lock cannot participate in a cycle.
                admin_cur.execute("SELECT id FROM users WHERE id=28 FOR UPDATE")
                assert admin_cur.fetchone()[0] == 28
                admin_conn.commit()
                result = future.result(timeout=10)
    assert isinstance(result, GovernanceValidationError)
    assert result.code == "ACTIVE_PROVIDER_DEPENDENCIES"
    assert _query_one("SELECT agent_level FROM user_wallets WHERE user_id=28") == (1,)
    assert _query_one(
        "SELECT COUNT(*) FROM admin_user_governance_audits "
        "WHERE request_id='race-demote-provider-28'"
    ) == (0,)


def test_bound_ordinary_user_is_promoted_and_binding_becomes_channel_relationship():
    # SSOT business-governance-master §4.1 / V4: an ordinary user that is a
    # customer of provider A MUST be promotable to service_provider atomically,
    # with the inbound customer binding re-expressed as an upstream channel
    # relationship (A stays B's upstream). The former ACTIVE_CUSTOMER_COMMERCIAL_BINDING
    # hard block is removed and the promotion is fully audited.
    result = change_business_identity(
        124, "service_provider", expected_version=1,
        reason="签约升级为下级服务商",
        operator_user_id=1, operator_username="admin_one",
        request_id="identity-bound-124", ip_address="127.0.0.1",
    )
    assert result["success"] is True
    assert result["after"] == {"business_identity": "service_provider"}
    # promoted
    assert _query_one("SELECT agent_level FROM user_wallets WHERE user_id=124") == (1,)
    # inbound customer projection is gone (124 is no longer a customer)
    assert _query_one(
        "SELECT COUNT(*) FROM customer_agent_bindings WHERE customer_user_id=124"
    ) == (0,)
    # A (123) is now B's (124) upstream channel account, cost-passthrough default
    assert _query_one(
        "SELECT upstream_channel_account_id, cost_multiplier_bps, status "
        "FROM channel_pricing_relationships "
        "WHERE buyer_dealer_id=124 AND effective_to IS NULL"
    ) == (123, 10000, "active")
    # source fact preserved as an ended history version (funds/orders untouched)
    assert _query_one(
        "SELECT provider_user_id FROM customer_agent_binding_history "
        "WHERE customer_user_id=124 AND effective_to IS NOT NULL "
        "ORDER BY id DESC LIMIT 1"
    ) == (123,)
    # atomic audit written (previously this request produced 0 audits)
    assert _query_one(
        "SELECT COUNT(*) FROM admin_user_governance_audits WHERE request_id='identity-bound-124'"
    ) == (1,)


def test_admin_identity_api_publishes_committed_permission_version(monkeypatch):
    from auth import perm_cache

    monkeypatch.setattr(perm_cache, "redis_set", lambda *args, **kwargs: True)
    perm_cache._mem_cache.clear()
    request = _request({"user_id": 1, "username": "admin_one", "is_admin": True})
    result = asyncio.run(admin_change_business_identity(
        300,
        ChangeBusinessIdentityRequest(
            expected_version=1,
            reason="通过治理接口签约成为服务商",
            business_identity="service_provider",
        ),
        request,
    ))
    parsed = GovernanceMutationResponse.model_validate(result)
    assert parsed.version == 2
    assert perm_cache._mem_cache[300][0] == _query_one(
        "SELECT permission_version FROM users WHERE id=300"
    )[0]


def test_permission_cache_ignores_stale_process_memory_on_redis_miss(monkeypatch):
    from auth import perm_cache
    from db import auth_db

    perm_cache._mem_cache[124] = (1, 9999999999.0)
    monkeypatch.setattr(perm_cache, "redis_get", lambda *args, **kwargs: None)
    monkeypatch.setattr(perm_cache, "redis_set", lambda *args, **kwargs: True)
    monkeypatch.setattr(auth_db, "get_user_permission_version", lambda user_id: 9)
    assert perm_cache.get_cached_permission_version(124) == 9
    assert perm_cache._mem_cache[124][0] == 9


def test_permission_cache_ignores_stale_redis_hit_after_admin_revoke(monkeypatch):
    """A successful stale Redis read must never extend removed admin access."""
    from auth import perm_cache
    from db import auth_db

    monkeypatch.setattr(perm_cache, "redis_get", lambda *args, **kwargs: "1")
    monkeypatch.setattr(perm_cache, "redis_set", lambda *args, **kwargs: False)
    monkeypatch.setattr(auth_db, "get_user_permission_version", lambda user_id: 12)
    assert perm_cache.get_cached_permission_version(124) == 12


def test_commercial_rebind_preserves_immutable_source_evidence_and_unbind_history(monkeypatch):
    """Changing the current projection must close history, never rewrite original evidence."""
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", "200")
    change_commercial_binding(
        129, 200, expected_version=1, reason="客户确认由新服务商承接",
        operator_user_id=1, operator_username="admin_one", request_id="history-rebind-129",
        ip_address="127.0.0.1",
    )
    original = _query_one(
        """SELECT provider_user_id,binding_source,source_token,effective_from,effective_to,
                  dispute_status,created_request_id,ended_request_id
           FROM customer_agent_binding_history
           WHERE customer_user_id=129 ORDER BY id LIMIT 1"""
    )
    assert original[0:3] == (28, "invite_code", "INV-28")
    assert original[3] is not None and original[4] is not None
    assert original[5] is None
    assert original[6] is None
    assert original[7] == "history-rebind-129"

    current = _query_one(
        """SELECT provider_user_id,relationship_state,binding_source,created_request_id,effective_to
           FROM customer_agent_binding_history
           WHERE customer_user_id=129 ORDER BY id DESC LIMIT 1"""
    )
    assert current == (200, "service_provider", "admin_manual", "history-rebind-129", None)

    change_commercial_binding(
        129, None, expected_version=2, reason="客户确认转由平台直营服务",
        operator_user_id=2, operator_username="admin_two", request_id="history-unbind-129",
        ip_address="127.0.0.1",
    )
    assert _query_one(
        "SELECT COUNT(*) FROM customer_agent_binding_history WHERE customer_user_id=129"
    ) == (3,)
    platform_direct = _query_one(
        """SELECT provider_user_id,relationship_state,binding_source,created_request_id,effective_to
           FROM customer_agent_binding_history
           WHERE customer_user_id=129 ORDER BY id DESC LIMIT 1"""
    )
    assert platform_direct == (None, "platform_direct", "admin_manual", "history-unbind-129", None)


def test_commercial_rebind_updates_credit_wallet_projection_in_same_transaction():
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO customer_agent_credit_wallets(
                       customer_user_id,agent_user_id,tool_credit_points)
                   VALUES (129,28,500)"""
            )
    change_commercial_binding(
        129, 200, expected_version=1, reason="客户确认由新服务商承接",
        operator_user_id=1, operator_username="admin_one", request_id="wallet-projection-129",
        ip_address="127.0.0.1",
    )
    assert _query_one(
        "SELECT agent_user_id,tool_credit_points FROM customer_agent_credit_wallets WHERE customer_user_id=129"
    ) == (200, 500)


def test_commercial_rebind_is_blocked_while_old_provider_order_is_pending():
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO recharge_orders(
                       id,user_id,agent_user_id,amount_cents,payment_status,order_type)
                   VALUES ('PENDING-REBIND-129',129,28,10000,'pending','customer_recharge')"""
            )
    with pytest.raises(GovernanceValidationError) as exc:
        change_commercial_binding(
            129, 200, expected_version=1, reason="不应重定向待支付订单",
            operator_user_id=1, operator_username="admin_one", request_id="pending-rebind-129",
            ip_address="127.0.0.1",
        )
    assert exc.value.code == "BINDING_PENDING_ORDERS"
    assert _query_one(
        "SELECT agent_user_id FROM customer_agent_bindings WHERE customer_user_id=129"
    ) == (28,)


def test_platform_direct_pending_order_blocks_first_explicit_binding(monkeypatch):
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", "200")
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO recharge_orders(
                       id,user_id,agent_user_id,amount_cents,payment_status,order_type)
                   VALUES ('PENDING-DIRECT-300',300,200,10000,'pending','customer_recharge')"""
            )
    with pytest.raises(GovernanceValidationError) as exc:
        change_commercial_binding(
            300, 28, expected_version=1, reason="待支付直营订单不得改换服务方",
            operator_user_id=1, operator_username="admin_one",
            request_id="pending-direct-bind-300", ip_address="127.0.0.1",
        )
    assert exc.value.code == "BINDING_PENDING_ORDERS"
    assert _query_one(
        "SELECT COUNT(*) FROM customer_agent_bindings WHERE customer_user_id=300"
    ) == (0,)
    assert _query_one(
        "SELECT COUNT(*) FROM customer_agent_binding_history WHERE customer_user_id=300"
    ) == (0,)
    assert _query_one(
        "SELECT COUNT(*) FROM admin_user_governance_audits WHERE request_id='pending-direct-bind-300'"
    ) == (0,)


def test_quoteless_order_fence_serializes_platform_direct_with_first_binding(monkeypatch):
    from services.commercial_service_routing import execute_under_commercial_relationship_fence

    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", "200")
    writer_entered = Event()
    release_writer = Event()

    def write_pending_order():
        writer_entered.set()
        assert release_writer.wait(timeout=5)
        with psycopg2.connect(DB_URL) as order_conn:
            with order_conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO recharge_orders(
                           id,user_id,agent_user_id,amount_cents,payment_status,order_type)
                       VALUES ('QUOTLESS-FENCED-300',300,200,10000,'pending','customer_recharge')"""
                )
        return "QUOTLESS-FENCED-300"

    def create_order():
        return execute_under_commercial_relationship_fence(
            customer_user_id=300,
            expected_service_user_id=200,
            expected_source="platform_direct",
            writer=write_pending_order,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        order_future = pool.submit(create_order)
        assert writer_entered.wait(timeout=5)
        binding_future = pool.submit(
            change_commercial_binding,
            300,
            28,
            expected_version=1,
            reason="并发首次绑定不得改写已锁定的直营订单",
            operator_user_id=1,
            operator_username="admin_one",
            request_id="quoteless-race-bind-300",
            ip_address="127.0.0.1",
        )
        time.sleep(0.2)
        assert binding_future.done() is False
        release_writer.set()
        assert order_future.result(timeout=5) == "QUOTLESS-FENCED-300"
        with pytest.raises(GovernanceValidationError) as exc:
            binding_future.result(timeout=5)
    assert exc.value.code == "BINDING_PENDING_ORDERS"
    assert _query_one(
        "SELECT agent_user_id FROM recharge_orders WHERE id='QUOTLESS-FENCED-300'"
    ) == (200,)
    assert _query_one(
        "SELECT COUNT(*) FROM customer_agent_bindings WHERE customer_user_id=300"
    ) == (0,)


def test_inflight_purchase_serializes_before_provider_downgrade():
    from api.agent_workbench_api import _lock_agent_identity_for_purchase

    purchase_conn = psycopg2.connect(DB_URL)
    try:
        cur = purchase_conn.cursor()
        cur.execute("SELECT pg_advisory_xact_lock(920714, %s)", (102,))
        _lock_agent_identity_for_purchase(cur, 102)
        cur.execute(
            """INSERT INTO recharge_orders(id,user_id,amount_cents,order_type,payment_status)
               VALUES ('INFLIGHT-PURCHASE-102',102,10000,'agent_inventory_purchase','pending')"""
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            demote = pool.submit(
                change_business_identity,
                102,
                "ordinary_user",
                expected_version=1,
                reason="并发降级必须等待进货写事务",
                operator_user_id=1,
                operator_username="admin_one",
                request_id="concurrent-demote-102",
                ip_address="127.0.0.1",
            )
            time.sleep(0.2)
            assert demote.done() is False
            purchase_conn.commit()
            with pytest.raises(GovernanceValidationError) as exc:
                demote.result(timeout=5)
        assert exc.value.code == "ACTIVE_PROVIDER_DEPENDENCIES"
    finally:
        purchase_conn.close()
    assert _query_one("SELECT agent_level FROM user_wallets WHERE user_id=102") == (1,)
    assert _query_one(
        "SELECT COUNT(*) FROM recharge_orders WHERE id='INFLIGHT-PURCHASE-102'"
    ) == (1,)
    assert _query_one(
        "SELECT COUNT(*) FROM admin_user_governance_audits WHERE request_id='concurrent-demote-102'"
    ) == (0,)


def test_payment_callback_order_lock_never_deadlocks_provider_downgrade():
    with psycopg2.connect(DB_URL) as setup_conn:
        with setup_conn.cursor() as cur:
            cur.execute(
                """INSERT INTO recharge_orders(id,user_id,amount_cents,order_type,payment_status)
                   VALUES ('CALLBACK-LOCK-102',102,10000,'agent_inventory_purchase','pending')"""
            )

    callback_conn = psycopg2.connect(DB_URL)
    try:
        callback_cur = callback_conn.cursor()
        callback_cur.execute(
            "SELECT id FROM recharge_orders WHERE id='CALLBACK-LOCK-102' FOR UPDATE"
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            demote = pool.submit(
                change_business_identity,
                102,
                "ordinary_user",
                expected_version=1,
                reason="支付回调持单锁时降级必须遵循同一锁序",
                operator_user_id=1,
                operator_username="admin_one",
                request_id="callback-order-demote-102",
                ip_address="127.0.0.1",
            )
            time.sleep(0.2)
            assert demote.done() is False
            # A former advisory->order downgrade would now own this advisory and
            # create a deterministic deadlock.  The fixed order->advisory sequence
            # leaves it immediately available to the cash-success callback.
            callback_cur.execute("SELECT pg_advisory_xact_lock(920714,%s)", (102,))
            callback_conn.commit()
            with pytest.raises(GovernanceValidationError) as exc:
                demote.result(timeout=5)
        assert exc.value.code == "ACTIVE_PROVIDER_DEPENDENCIES"
    finally:
        callback_conn.close()
    assert _query_one("SELECT agent_level FROM user_wallets WHERE user_id=102") == (1,)
    assert _query_one(
        "SELECT COUNT(*) FROM admin_user_governance_audits "
        "WHERE request_id='callback-order-demote-102'"
    ) == (0,)


def test_payment_callback_order_lock_never_deadlocks_commercial_rebind():
    with psycopg2.connect(DB_URL) as setup_conn:
        with setup_conn.cursor() as cur:
            cur.execute(
                """INSERT INTO recharge_orders(
                       id,user_id,agent_user_id,amount_cents,order_type,payment_status)
                   VALUES ('CALLBACK-BINDING-124',124,123,10000,'customer_recharge','pending')"""
            )

    callback_conn = psycopg2.connect(DB_URL)
    try:
        callback_cur = callback_conn.cursor()
        callback_cur.execute(
            "SELECT id FROM recharge_orders WHERE id='CALLBACK-BINDING-124' FOR UPDATE"
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            rebind = pool.submit(
                change_commercial_binding,
                124,
                200,
                expected_version=1,
                reason="支付回调持单锁时换绑必须遵循同一锁序",
                operator_user_id=1,
                operator_username="admin_one",
                request_id="callback-order-rebind-124",
                ip_address="127.0.0.1",
            )
            time.sleep(0.2)
            assert rebind.done() is False
            # Settlement resolves the subject only after locking the order.  A
            # subject->order admin path would own this advisory and deadlock.
            from services.commercial_service_routing import lock_commercial_binding_subject

            lock_commercial_binding_subject(callback_cur, 124)
            callback_conn.commit()
            with pytest.raises(GovernanceValidationError) as exc:
                rebind.result(timeout=5)
        assert exc.value.code == "BINDING_PENDING_ORDERS"
    finally:
        callback_conn.close()
    assert _query_one(
        "SELECT agent_user_id FROM customer_agent_bindings WHERE customer_user_id=124"
    ) == (123,)
    assert _query_one(
        "SELECT COUNT(*) FROM admin_user_governance_audits "
        "WHERE request_id='callback-order-rebind-124'"
    ) == (0,)


def test_purchase_recheck_fails_after_downgrade_commits():
    from api.agent_workbench_api import _lock_agent_identity_for_purchase

    change_business_identity(
        102, "ordinary_user", expected_version=1, reason="无依赖服务商正常降级",
        operator_user_id=1, operator_username="admin_one",
        request_id="demote-before-purchase-102", ip_address="127.0.0.1",
    )
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(920714, %s)", (102,))
            with pytest.raises(HTTPException) as exc:
                _lock_agent_identity_for_purchase(cur, 102)
            assert exc.value.status_code == 403
            assert exc.value.detail["code"] == "AGENT_IDENTITY_REQUIRED"
    assert _query_one(
        "SELECT COUNT(*) FROM recharge_orders WHERE user_id=102"
    ) == (0,)


@pytest.mark.parametrize(
    ("insert_sql", "params"),
    [
        ("UPDATE dealer_resale_global_settings SET platform_seller_user_id=%s WHERE singleton_id=1", (102,)),
        ("INSERT INTO dealer_inventory_lots(lot_id,owner_agent_user_id,remaining_points,reserved_points) VALUES ('L-OPEN',%s,1,0)", (102,)),
        ("INSERT INTO dealer_resale_orders(order_id,seller_user_id,refund_responsible_user_id,state) VALUES ('R-OPEN',%s,%s,'paid')", (102, 102)),
        ("INSERT INTO dealer_consumer_sales(order_id,seller_user_id,refund_responsible_user_id,state) VALUES ('C-OPEN',%s,%s,'manual_review')", (102, 102)),
        ("INSERT INTO dealer_resale_profit_ledger(seller_user_id,status) VALUES (%s,'pending')", (102,)),
        ("INSERT INTO consumer_refund_cases(case_id,responsible_service_user_id,status) VALUES ('RF-OPEN',%s,'platform_execution')", (102,)),
        ("INSERT INTO service_refund_liability_ledger(service_user_id,status) VALUES (%s,'negative_settlement')", (102,)),
        ("INSERT INTO service_refund_funding_work_orders(work_order_id,service_user_id,status) VALUES ('WO-OPEN',%s,'open')", (102,)),
        ("INSERT INTO service_refund_cash_jobs(cash_job_id,responsible_service_user_id,status) VALUES ('CJ-OPEN',%s,'queued')", (102,)),
        ("INSERT INTO service_refund_cash_jobs(cash_job_id,responsible_service_user_id,status) VALUES ('CJ-FAILED',%s,'failed')", (102,)),
        ("INSERT INTO service_refund_reserve_accounts(service_user_id,available_cents) VALUES (%s,1)", (102,)),
    ],
)
def test_provider_downgrade_blocks_every_resale_and_refund_obligation(insert_sql, params):
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(insert_sql, params)
    with pytest.raises(GovernanceValidationError) as exc:
        change_business_identity(
            102, "ordinary_user", expected_version=1, reason="仍有转售资金责任时不得降级",
            operator_user_id=1, operator_username="admin_one", request_id="resale-blocker-102",
            ip_address="127.0.0.1",
        )
    assert exc.value.code == "ACTIVE_PROVIDER_DEPENDENCIES"
    assert _query_one("SELECT agent_level FROM user_wallets WHERE user_id=102") == (1,)


def test_external_relationship_writer_advances_shared_cas_and_audit():
    from services.admin_user_governance import record_external_commercial_binding_change
    from services.commercial_service_routing import lock_commercial_binding_subject

    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            lock_commercial_binding_subject(cur, 129)
            version = record_external_commercial_binding_change(
                cur,
                subject_user_id=129,
                operator_user_id=1,
                operator_username="admin_one",
                request_id="external-dispute-resolution-129",
                reason="争议裁决入口确认继续由原服务商承接",
                before={
                    "commercial_provider_user_id": 28,
                    "commercial_mode": "service_provider",
                    "dispute_status": "pending",
                },
                after={
                    "commercial_provider_user_id": 28,
                    "commercial_mode": "service_provider",
                    "dispute_status": None,
                },
                ip_address="127.0.0.1",
                evidence={"dispute_id": 88, "resolution_action": "keep_old"},
            )
    assert version == 2
    assert _query_one(
        """SELECT version FROM admin_user_governance_versions
           WHERE subject_user_id=129 AND scope='commercial_binding'"""
    ) == (2,)
    assert _query_one(
        """SELECT before_snapshot->>'dispute_status',after_snapshot->>'dispute_status'
           FROM admin_user_governance_audits
           WHERE request_id='external-dispute-resolution-129'"""
    ) == ("pending", None)
    with pytest.raises(GovernanceVersionConflict):
        change_commercial_binding(
            129, 200, expected_version=1, reason="旧页面不得覆盖争议裁决结果",
            operator_user_id=2, operator_username="admin_two", request_id="stale-after-dispute",
            ip_address="127.0.0.1",
        )


def test_provider_with_active_customers_cannot_be_silently_demoted():
    with pytest.raises(GovernanceValidationError) as exc:
        change_business_identity(
            28, "ordinary_user", expected_version=1, reason="不应绕过现役客户关系",
            operator_user_id=1, operator_username="admin_one", request_id="demote-28",
            ip_address="127.0.0.1",
        )
    assert exc.value.code == "ACTIVE_PROVIDER_DEPENDENCIES"
    assert _query_one("SELECT agent_level FROM user_wallets WHERE user_id=28") == (1,)


def test_provider_with_inventory_cannot_be_demoted_and_nothing_is_written():
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO agent_inventory_wallets(
                       agent_user_id,paid_inventory_points,bonus_inventory_points,frozen_inventory_points)
                   VALUES (102,80,20,5)"""
            )
    with pytest.raises(GovernanceValidationError) as exc:
        change_business_identity(
            102, "ordinary_user", expected_version=1, reason="库存未清不得降级",
            operator_user_id=1, operator_username="admin_one", request_id="demote-inventory-102",
            ip_address="127.0.0.1",
        )
    assert exc.value.code == "ACTIVE_PROVIDER_DEPENDENCIES"
    assert _query_one("SELECT agent_level FROM user_wallets WHERE user_id=102") == (1,)
    assert _query_one("SELECT COUNT(*) FROM admin_user_governance_audits WHERE subject_user_id=102") == (0,)


def test_provider_with_pending_purchase_cannot_be_demoted():
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO recharge_orders(id,user_id,amount_cents,order_type,payment_status)
                   VALUES ('PENDING-AGENT-102',102,10000,'agent_inventory_purchase','pending')"""
            )
    with pytest.raises(GovernanceValidationError) as exc:
        change_business_identity(
            102, "ordinary_user", expected_version=1, reason="待支付订单存在不得降级",
            operator_user_id=1, operator_username="admin_one", request_id="demote-pending-102",
            ip_address="127.0.0.1",
        )
    assert exc.value.code == "ACTIVE_PROVIDER_DEPENDENCIES"
    assert _query_one("SELECT agent_level FROM user_wallets WHERE user_id=102") == (1,)


def _assert_demote_rejected_without_writes(user_id: int, request_id: str) -> None:
    with pytest.raises(GovernanceValidationError) as exc:
        change_business_identity(
            user_id, "ordinary_user", expected_version=1, reason="存在未结依赖不得降级",
            operator_user_id=1, operator_username="admin_one", request_id=request_id,
            ip_address="127.0.0.1",
        )
    assert exc.value.code == "ACTIVE_PROVIDER_DEPENDENCIES"
    assert _query_one("SELECT agent_level FROM user_wallets WHERE user_id=%s", (user_id,)) == (1,)
    assert _query_one(
        "SELECT COUNT(*) FROM admin_user_governance_audits WHERE request_id=%s",
        (request_id,),
    ) == (0,)
    assert _query_one(
        "SELECT COUNT(*) FROM admin_user_governance_versions "
        "WHERE subject_user_id=%s AND scope='business_identity'",
        (user_id,),
    ) == (0,)


def test_service_provider_cannot_be_commercial_binding_subject_and_nothing_is_written():
    with pytest.raises(GovernanceValidationError) as exc:
        change_commercial_binding(
            102, 28, expected_version=1, reason="服务商不得写入客户商业绑定",
            operator_user_id=1, operator_username="admin_one", request_id="bind-provider-102",
            ip_address="127.0.0.1",
        )
    assert exc.value.code == "COMMERCIAL_BINDING_SUBJECT_MUST_BE_ORDINARY"
    assert _query_one(
        "SELECT COUNT(*) FROM customer_agent_bindings WHERE customer_user_id=102"
    ) == (0,)
    assert _query_one(
        "SELECT COUNT(*) FROM customer_agent_binding_history WHERE customer_user_id=102"
    ) == (0,)
    assert _query_one(
        "SELECT COUNT(*) FROM admin_user_governance_audits WHERE request_id='bind-provider-102'"
    ) == (0,)


def test_shared_commercial_binding_writer_rejects_provider_subject_and_rolls_back():
    from services.customer_binding import (
        CommercialBindingSubjectError,
        upsert_customer_agent_binding,
    )

    with pytest.raises(CommercialBindingSubjectError):
        with psycopg2.connect(DB_URL) as conn:
            with conn.cursor() as cur:
                upsert_customer_agent_binding(
                    cur,
                    customer_user_id=102,
                    agent_user_id=28,
                    binding_source="admin_manual",
                    source_token="offline_allocation",
                )

    assert _query_one(
        "SELECT COUNT(*) FROM customer_agent_bindings WHERE customer_user_id=102"
    ) == (0,)


def test_service_provider_commercial_binding_api_returns_422_and_zero_writes():
    request = _request({"user_id": 1, "username": "admin_one", "is_admin": True})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin_change_commercial_service_binding(
            102,
            ChangeCommercialBindingRequest(
                provider_user_id=28,
                expected_version=1,
                reason="服务商主体必须由后端拒绝",
            ),
            request,
        ))
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "COMMERCIAL_BINDING_SUBJECT_MUST_BE_ORDINARY"
    assert _query_one(
        "SELECT COUNT(*) FROM admin_user_governance_audits WHERE subject_user_id=102"
    ) == (0,)


def test_a_wallet_less_subject_is_backfilled_then_bound():
    """🔴 [#139 返修三 · 2026-09-07] 本条**改写**自
    `..._blocks_commercial_binding_without_writes`,不是删除。

    它原来断言:主体没有钱包行 ⇒ 拒绝 + 零写入,理由「身份真相缺失时不得猜测」。
    裁定(2026-09-07)把这一格改了:**被治理主体**先补钱包行再继续,
    **关系对手方**保持严格。

    🔴 为什么这不违背它原来的意思:
       「不得猜测」守的是**读** —— 把缺行显示成「普通用户」是猜。
       写路径补出来的 `agent_level=0` 不是猜,是把默认值**落成事实**:
       新账号本来就是普通用户,直到被提升。
       而且原来的拒绝没保护任何东西 —— 它要求存在的那一行,补一下就有了;
       代价是管理员**修不了**那些邀请进来还没初始化钱包的账号。

    同一份口径 `change_business_identity` 早就是这么做的(补行 + FOR UPDATE 读),
    本次只是把两条治理写路径统一到一个 helper。
    """
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_wallets WHERE user_id=124")
    change_commercial_binding(
        124, 28, expected_version=1, reason="主体缺钱包行时补齐后继续",
        operator_user_id=1, operator_username="admin_one", request_id="bind-no-wallet-124",
        ip_address="127.0.0.1",
    )
    # 行被补出来,且默认是普通用户(不是猜,是默认值落成事实)
    assert _query_one("SELECT agent_level FROM user_wallets WHERE user_id=124") == (0,)
    # 绑定真的改到了 28
    assert _query_one(
        "SELECT agent_user_id FROM customer_agent_bindings WHERE customer_user_id=124"
    ) == (28,)
    # 🔴 这一条是原判据的尾巴,我改写时**没覆盖到它**(替换区间少了最后三行),
    #    于是它带着旧语义活了下来:「被拒了所以没写审计」。
    #    现在绑定成功,审计**必须**写 —— 治理写动作不留痕才是真问题。
    #    教训:改写判据时,替换区间要覆盖**整个逻辑单元**,不是覆盖到我眼睛停下的地方。
    assert _query_one(
        "SELECT COUNT(*) FROM admin_user_governance_audits WHERE request_id='bind-no-wallet-124'"
    ) == (1,)


def test_missing_business_identity_ssot_degrades_the_row_without_guessing_ordinary():
    """🔴 [#139 · 2026-09-07] 本条**改写**自
    `..._blocks_admin_reads_instead_of_guessing_ordinary`,不是删除。

    它原来断言两件事:
      ① 详情对缺钱包用户 raise —— **返修二已改**:详情同样按行降级,
         理由见下方注释(拒读把管理员挡在了修复入口之外);
      ② **列表也 raise** —— 那正是 Owner 2026-09-07 报的缺陷:
         一个缺钱包的用户把**整页**用户列表拒读了。

    🔴 判据把 bug 钉成了预期,所以修复会让它变红 ——
       红了不代表修错了。它名字里真正要守的是「**不许猜成普通用户**」,
       那一条修复完全满足(缺行标 `unverified`,不是 `COALESCE(...,0)` 读出的
       普通用户)。改写后守的还是同一件事,只是把「整页拒读」换成「按行降级」。
    """
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_wallets WHERE user_id=124")
    # 🔴 ① 详情**也降级**(2026-09-07 返修二改的)。
    #    第一版返修我写的是「详情仍 fail-closed」,那时确实如此;
    #    随后 Review 指出:管理员打不开详情就到不了那个能补钱包的写动作
    #    (`change_business_identity` 本来就有 `INSERT INTO user_wallets`),
    #    守卫把人挡在了修复入口之外。于是详情也改成按行降级 ——
    #    而我**当时没回头改这条**,它单跑绿、整包红,是我自己的疏漏不是环境。
    detail = get_admin_user_detail(124)
    assert detail["overview"]["business_identity"] == "unverified", detail["overview"]
    assert "尚未初始化钱包" in (detail["overview"]["business_identity_label"] or "")
    # ② 列表必须能渲染,且那一行**不许**被猜成普通用户
    out = list_admin_users(page=1, page_size=100)
    rows = {int(u["user_id"]): u for u in out["users"]}
    assert 124 in rows, "缺钱包的行被整页吃掉了 —— 缺陷复发"
    assert len(rows) > 1, "列表只剩一行 —— 别的行被连累了"
    assert rows[124]["business_identity"] == "unverified", (
        "缺钱包被猜成了普通用户:%r" % rows[124]["business_identity"])
    assert rows[124]["needs_attention"] is True


def test_platform_direct_service_account_cannot_be_demoted(monkeypatch):
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", "102")
    _assert_demote_rejected_without_writes(102, "demote-platform-direct-102")


def test_provider_pinned_by_customer_pending_order_cannot_be_demoted():
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO recharge_orders(
                       id,user_id,amount_cents,order_type,agent_user_id,payment_status)
                   VALUES ('PENDING-CUSTOMER-124',124,12800,'customer_recharge',102,'pending')"""
            )
    _assert_demote_rejected_without_writes(102, "demote-customer-order-102")


def test_provider_pinned_by_legacy_pending_customer_order_cannot_be_demoted():
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO recharge_orders(
                       id,user_id,amount_cents,order_type,agent_user_id,payment_status)
                   VALUES ('PENDING-LEGACY-124',124,12800,NULL,102,'pending')"""
            )
    _assert_demote_rejected_without_writes(102, "demote-legacy-customer-order-102")


def test_provider_with_pending_dispute_or_held_escrow_cannot_be_demoted():
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO customer_agent_binding_disputes(
                       customer_user_id,old_agent_user_id,new_agent_user_id,status)
                   VALUES (124,102,28,'pending')"""
            )
            cur.execute(
                """INSERT INTO dispute_escrow(
                       order_id,dispute_id,customer_user_id,order_agent_user_id,
                       bound_agent_user_id,status)
                   VALUES ('ESCROW-102',1,124,102,28,'held')"""
            )
    _assert_demote_rejected_without_writes(102, "demote-dispute-102")


def test_provider_with_unsettled_revenue_or_payout_request_cannot_be_demoted():
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO agent_revenue_ledger(
                       agent_user_id,agent_settlement_cents,status)
                   VALUES (102,8800,'frozen')"""
            )
            cur.execute(
                """INSERT INTO agent_settlement_requests(
                       agent_user_id,request_amount_cents,status)
                   VALUES (102,5000,'pending')"""
            )
            cur.execute(
                "INSERT INTO withdrawal_requests(user_id,status) VALUES (102,'pending')"
            )
    _assert_demote_rejected_without_writes(102, "demote-revenue-102")


def test_provider_with_unsettled_channel_revenue_cannot_be_demoted():
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO channel_revenue_ledger(
                       channel_beneficiary_user_id,buyer_dealer_id,recharge_order_id,
                       channel_revenue_cents,status)
                   VALUES (102,200,'CHANNEL-REVENUE-102',3600,'recorded')"""
            )
    _assert_demote_rejected_without_writes(102, "demote-channel-revenue-102")


def test_fully_paid_and_redeemed_revenue_does_not_permanently_block_demote():
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO agent_revenue_ledger(
                       agent_user_id,agent_settlement_cents,status)
                   VALUES (102,10000,'settled') RETURNING id"""
            )
            ledger_id = int(cur.fetchone()[0])
            cur.execute(
                """INSERT INTO agent_settlement_requests(
                       agent_user_id,request_amount_cents,status)
                   VALUES (102,4000,'paid') RETURNING id"""
            )
            settlement_id = int(cur.fetchone()[0])
            cur.execute(
                """INSERT INTO agent_settlement_request_items(
                       settlement_request_id,ledger_id,locked_amount_cents)
                   VALUES (%s,%s,4000)""",
                (settlement_id, ledger_id),
            )
            cur.execute(
                """INSERT INTO agent_commission_redemption_requests(agent_user_id,status)
                   VALUES (102,'redeemed') RETURNING id"""
            )
            redemption_id = int(cur.fetchone()[0])
            cur.execute(
                """INSERT INTO agent_commission_redemption_items(
                       redemption_request_id,ledger_id,locked_amount_cents)
                   VALUES (%s,%s,6000)""",
                (redemption_id, ledger_id),
            )

    result = change_business_identity(
        102, "ordinary_user", expected_version=1,
        reason="收益已全部提现或换算力并结清",
        operator_user_id=1, operator_username="admin_one",
        request_id="demote-settled-revenue-102", ip_address="127.0.0.1",
    )
    assert result["after"]["business_identity"] == "ordinary_user"
    assert _query_one("SELECT agent_level FROM user_wallets WHERE user_id=102") == (0,)


def test_legacy_role_user_remains_readable_and_permissions_still_resolve():
    from db.auth_db import get_user

    legacy_user = get_user(124)
    assert legacy_user is not None
    assert {role["name"] for role in legacy_user["roles"]} == {"social_ops", "geo_writer"}
    assert "dashboard:read" in legacy_user["permissions"]
    assert "writing:write" in legacy_user["permissions"]


def test_platform_direct_readiness_rejects_admin_personal_account(monkeypatch):
    monkeypatch.delenv("PLATFORM_DIRECT_SERVICE_USER_ID", raising=False)
    assert get_platform_direct_readiness()["status"] == "missing"
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", "200")
    ready = get_platform_direct_readiness()
    PlatformDirectReadinessResponse.model_validate({"success": True, "readiness": ready})
    assert ready["ready"] is True
    assert ready["service_user"]["user_id"] == 200
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", "1")
    invalid = get_platform_direct_readiness()
    assert invalid["ready"] is False
    assert any("管理员权限" in check for check in invalid["checks"])


def test_platform_direct_readiness_rejects_missing_or_unquoteable_retail_catalog(monkeypatch):
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", "200")
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """DELETE FROM pricing_catalog_entries WHERE version_id IN (
                       SELECT id FROM pricing_catalog_versions WHERE scope_key='SV-AAA200'
                   )"""
            )
            cur.execute("DELETE FROM pricing_catalog_versions WHERE scope_key='SV-AAA200'")
    missing = get_platform_direct_readiness()
    assert missing["ready"] is False
    assert any("尚无已发布零售价目版本" in check for check in missing["checks"])

    # The fixture is reseeded for each test; corrupting the entry in this same
    # transaction proves a named published version is still insufficient.
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO pricing_catalog_versions(
                       catalog_type,scope_key,version_code,status,effective_to)
                   VALUES ('retail','SV-AAA200','retail-invalid-v1','published',NULL)
                   RETURNING id"""
            )
            version_id = int(cur.fetchone()[0])
            cur.execute(
                """INSERT INTO pricing_catalog_entries(
                       version_id,product_code,base_price_cents,multiplier_bps,
                       final_price_cents,paid_points,bonus_points,cost_floor_cents)
                   VALUES (%s,'broken-sku',10000,10000,10000,0,0,8000)""",
                (version_id,),
            )
    invalid = get_platform_direct_readiness()
    assert invalid["ready"] is False
    assert any("无法完整生成报价" in check for check in invalid["checks"])


def test_platform_direct_readiness_reuses_live_resale_inventory_gate(monkeypatch):
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", "200")
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE system_settings SET value='true'
                   WHERE key='DEALER_INVENTORY_RESALE_ENABLED'"""
            )

    readiness = get_platform_direct_readiness()

    assert readiness["ready"] is False
    assert any("schema" in check.lower() or "缺表" in check for check in readiness["checks"])


def test_governance_migration_tables_exist_after_double_apply():
    assert _query_one("SELECT to_regclass('admin_user_governance_versions')") == (
        "admin_user_governance_versions",
    )
    assert _query_one("SELECT to_regclass('admin_user_governance_audits')") == (
        "admin_user_governance_audits",
    )
    assert _query_one("SELECT to_regclass('customer_agent_binding_history')") == (
        "customer_agent_binding_history",
    )


def test_governance_fresh_migration_has_no_duplicate_checks_or_request_index():
    checks = _query_one(
        """SELECT COUNT(*),COUNT(DISTINCT t.relname || ':' || pg_get_constraintdef(c.oid))
           FROM pg_constraint c
           JOIN pg_class t ON t.oid=c.conrelid
           JOIN pg_namespace n ON n.oid=t.relnamespace
           WHERE n.nspname=current_schema() AND c.contype='c' AND t.relname IN (
             'admin_user_governance_versions','admin_user_governance_audits',
             'customer_agent_binding_history'
           )"""
    )
    assert checks == (9, 9)
    request_indexes = _query_one(
        """SELECT COUNT(*) FROM pg_indexes
           WHERE schemaname='public' AND tablename='admin_user_governance_audits'
             AND indexdef LIKE '%%(request_id)%%'"""
    )
    assert request_indexes == (1,)


def test_governance_schema_verifier_rejects_malformed_constraint():
    from services.admin_user_governance_schema import verify_admin_user_governance_schema

    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE admin_user_governance_versions "
                "DROP CONSTRAINT admin_user_governance_versions_version_check"
            )
            with pytest.raises(RuntimeError, match="version_check"):
                verify_admin_user_governance_schema(cur)
            conn.rollback()


def test_governance_schema_verifier_rejects_not_valid_constraint():
    from services.admin_user_governance_schema import verify_admin_user_governance_schema

    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE admin_user_governance_versions "
                "DROP CONSTRAINT admin_user_governance_versions_version_check"
            )
            cur.execute(
                "ALTER TABLE admin_user_governance_versions ADD CONSTRAINT "
                "admin_user_governance_versions_version_check CHECK (version >= 1) NOT VALID"
            )
            with pytest.raises(RuntimeError, match="not validated"):
                verify_admin_user_governance_schema(cur)
            conn.rollback()


def test_governance_migration_rejects_named_not_valid_constraint():
    migration_sql = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "migration_admin_user_governance_2026_07_15.sql"
    ).read_text(encoding="utf-8")
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE admin_user_governance_versions "
                "DROP CONSTRAINT admin_user_governance_versions_version_check"
            )
            cur.execute(
                "ALTER TABLE admin_user_governance_versions ADD CONSTRAINT "
                "admin_user_governance_versions_version_check CHECK (version >= 1) NOT VALID"
            )
            with pytest.raises(psycopg2.Error, match="not validated"):
                cur.execute(migration_sql)
            conn.rollback()


def test_governance_migration_repairs_partial_history_table():
    """Prestart migration repairs additive drift before startup accepts the schema."""
    from services.admin_user_governance_schema import verify_admin_user_governance_schema

    migration_sql = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "migration_admin_user_governance_2026_07_15.sql"
    ).read_text(encoding="utf-8")
    extension_sql = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "migration_admin_user_governance_extensions_2026_07_19.sql"
    ).read_text(encoding="utf-8")
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP INDEX idx_customer_agent_binding_history_timeline")
            cur.execute("ALTER TABLE customer_agent_binding_history DROP COLUMN ended_reason")
            cur.execute(migration_sql)
            cur.execute(extension_sql)
            verify_admin_user_governance_schema(cur)
            cur.execute(
                """SELECT data_type,is_nullable
                   FROM information_schema.columns
                   WHERE table_schema='public'
                     AND table_name='customer_agent_binding_history'
                     AND column_name='ended_reason'"""
            )
            assert cur.fetchone() == ("text", "YES")
            cur.execute("SELECT to_regclass('idx_customer_agent_binding_history_timeline')")
            assert cur.fetchone() == ("idx_customer_agent_binding_history_timeline",)


def test_non_admin_user_and_service_provider_get_403_for_admin_routes():
    ordinary = _request({"user_id": 124, "username": "customer_124", "is_admin": False})
    provider = _request({"user_id": 28, "username": "provider_28", "is_admin": False})
    for request in (ordinary, provider):
        with pytest.raises(HTTPException) as list_exc:
            asyncio.run(admin_user_governance_list(request))
        assert list_exc.value.status_code == 403
        with pytest.raises(HTTPException) as detail_exc:
            asyncio.run(admin_user_governance_detail(124, request))
        assert detail_exc.value.status_code == 403
        with pytest.raises(HTTPException) as write_exc:
            asyncio.run(admin_change_business_identity(
                124,
                ChangeBusinessIdentityRequest(
                    expected_version=1,
                    reason="无权限请求",
                    business_identity="service_provider",
                ),
                request,
            ))
        assert write_exc.value.status_code == 403
        with pytest.raises(HTTPException) as binding_exc:
            asyncio.run(admin_change_commercial_service_binding(
                124,
                ChangeCommercialBindingRequest(
                    expected_version=1,
                    reason="无权限请求",
                    provider_user_id=28,
                ),
                request,
            ))
        assert binding_exc.value.status_code == 403
        with pytest.raises(HTTPException) as access_exc:
            asyncio.run(admin_change_platform_access(
                124,
                ChangePlatformAccessRequest(
                    expected_version=1,
                    reason="无权限请求",
                    administrator=True,
                ),
                request,
            ))
        assert access_exc.value.status_code == 403
        with pytest.raises(HTTPException) as readiness_exc:
            asyncio.run(admin_platform_direct_readiness(request))
        assert readiness_exc.value.status_code == 403
