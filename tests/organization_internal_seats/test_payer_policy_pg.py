"""True PostgreSQL 16 pytest gates for the owner-consented shared payer policy.

Covers the W2 contract: payer policy SSOT (owner-only CAS + append-only audit +
platform ADMIN emergency disable), the overage split algorithm in
``reserve_charge`` (zero task / zero durable freeze / zero provider traffic on
rejection), frozen dual-leg evidence, refund leg restoration, concurrency,
kill-9 recovery, migration matrix, and catalog decoys. Runs only against a
loopback throwaway PostgreSQL 16 DSN.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import urlsplit

import psycopg2
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from verify_local import (  # noqa: E402
    BASE_SQL,
    CROSS_TENANT_MIGRATION_SQL,
    MIGRATION_SQL,
    ONBOARDING_MIGRATION_SQL,
    PAYER_MIGRATION_SQL,
    connect,
    create_schema,
    execute_sql,
    install_environment,
    isolate_public_qualified_sql,
    seed_runtime,
    sql_value,
)

ROLLBACK_SQL = (
    ROOT / "scripts" / "migration_organization_payer_policies_2026_07_23_rollback.sql"
).read_text(encoding="utf-8")

DSN = os.environ.get("TEST_ORG_WALLET_DSN") or os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres:test_org_wallet_pw@localhost:55499/test_org_wallet",
)
if (urlsplit(DSN).hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
    raise RuntimeError("payer policy pytest only accepts a loopback throwaway PostgreSQL DSN")


def _build_schema(prefix: str, *, rollback: bool = False) -> tuple[str, str]:
    schema, dsn = create_schema(DSN, prefix)
    execute_sql(dsn, BASE_SQL)
    execute_sql(dsn, MIGRATION_SQL)
    execute_sql(dsn, ONBOARDING_MIGRATION_SQL)
    execute_sql(dsn, MIGRATION_SQL)
    execute_sql(dsn, ONBOARDING_MIGRATION_SQL)
    cross_tenant_sql = isolate_public_qualified_sql(CROSS_TENANT_MIGRATION_SQL, schema)
    execute_sql(dsn, cross_tenant_sql)
    execute_sql(dsn, cross_tenant_sql)
    execute_sql(dsn, PAYER_MIGRATION_SQL)
    execute_sql(dsn, PAYER_MIGRATION_SQL)
    if rollback:
        execute_sql(dsn, ROLLBACK_SQL)
        execute_sql(dsn, PAYER_MIGRATION_SQL)
    return schema, dsn


SCHEMA, TEST_DSN = _build_schema("org_payer_pytest")
install_environment(TEST_DSN)
seed_runtime(TEST_DSN)

# Imports occur only after DATABASE_URL points at the isolated schema.
from fastapi import FastAPI  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402

from db.organization_db import readiness, resolve_identity  # noqa: E402
from services.organization_billing import (  # noqa: E402
    billing_consistency,
    claim_live_charge,
    force_release_charge,
    get_charge_reconciliation_state,
    mark_external_side_effect_started,
    release_charge,
    renew_settlement_lease,
    reserve_charge,
    refund_charge,
    settle_charge,
    settle_charge_via_reconciliation,
)
from services.organization_contract import OrganizationError  # noqa: E402
from services.organization_limits import configure_limit  # noqa: E402
from services.organization_payer_policy import get_payer_policy, put_payer_policy  # noqa: E402
from services.organization_service import (  # noqa: E402
    accept_invite,
    assign_brands,
    create_invite,
    create_organization,
    set_member_status,
)


@pytest.fixture(autouse=True)
def _payer_flag_environment(monkeypatch):
    """R3：模块级 install_environment 在 collection 期直写 os.environ 且无
    teardown；feature_flags() 实时读 env，同进程先跑文件（invite 套件）
    泄漏的 flag 会污染本套件的 fail-closed 判定。这里显式钉住本套件依赖
    的 flag——monkeypatch 每个测试后自动还原，本套件用例不依赖外层 env。"""
    monkeypatch.setenv("ORGANIZATION_SEATS_ENABLED", "true")
    monkeypatch.setenv("ORGANIZATION_SHARED_PAYER_ENABLED", "true")
    monkeypatch.setenv("ORGANIZATION_EXTERNAL_ACTIONS_ENABLED", "false")
    yield


def test_invite_suite_flag_env_does_not_leak_into_process():
    """R3 判别性：invite 套件若退回 session 级直写 env（无还原），
    ORGANIZATION_INVITE_ONBOARDING_ENABLED / ORGANIZATION_INVITE_DELIVERY_PROVIDER
    会泄漏进本进程并保持 "true"/"local_capture"，本断言立即转红。"""
    assert os.environ.get("ORGANIZATION_INVITE_ONBOARDING_ENABLED") != "true"
    assert os.environ.get("ORGANIZATION_INVITE_DELIVERY_PROVIDER") != "local_capture"


def _uid(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


def _wallet(user_id: int, paid_points: int = 2_000_000) -> None:
    with connect(TEST_DSN) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO user_wallets(user_id,paid_points,total_recharged,agent_level)
            VALUES (%s,%s,%s,1)
            ON CONFLICT(user_id) DO UPDATE SET paid_points=EXCLUDED.paid_points
            """,
            (int(user_id), int(paid_points), int(paid_points)),
        )


def _brand(brand_id: int, owner_user_id: int, name: str = "pytest 客户") -> None:
    with connect(TEST_DSN) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO brands(id,owner_user_id,name) VALUES (%s,%s,%s) ON CONFLICT(id) DO NOTHING",
            (int(brand_id), int(owner_user_id), name),
        )


def _make_org(owner_user_id: int, *, wallet: int = 2_000_000) -> tuple[int, object]:
    _wallet(owner_user_id, wallet)
    overview = create_organization(
        owner_user_id=owner_user_id,
        name=f"pytest 共享钱包团队 {owner_user_id}",
        request_id=_uid(f"org-create-{owner_user_id}"),
    )
    organization_id = int(overview["id"])
    owner = resolve_identity(owner_user_id, request_id=_uid(f"owner-{owner_user_id}"))
    return organization_id, owner


def _make_member(
    owner,
    organization_id: int,
    member_user_id: int,
    *,
    brand_id: int | None = None,
    role_code: str = "sales",
) -> tuple[int, object]:
    role_id = int(
        sql_value(
            TEST_DSN,
            "SELECT id FROM organization_roles WHERE organization_id=%s AND code=%s",
            (organization_id, role_code),
        )
    )
    invite = create_invite(
        owner,
        target_kind="phone",
        target=f"139{member_user_id:08d}",
        role_id=role_id,
        request_id=_uid("invite"),
        source_ip="192.0.2.5",
        brand_ids=[brand_id] if brand_id else [],
    )
    accepted = accept_invite(
        authenticated_user_id=member_user_id,
        token=invite["delivery_token"],
        request_id=_uid("accept"),
        source_ip="192.0.2.6",
    )
    membership_id = int(accepted["membership_id"])
    member = resolve_identity(member_user_id, request_id=_uid(f"member-{member_user_id}"))
    return membership_id, member


def _enable_policy(
    owner,
    *,
    per_action: int = 100,
    daily: int = 500,
    monthly: int = 2_000,
    overage: bool = True,
    expected_version: int = 0,
    request_id: str | None = None,
    actor_user_id: int | None = None,
) -> dict:
    return put_payer_policy(
        identity=owner,
        actor_user_id=int(actor_user_id if actor_user_id is not None else owner.actor_user_id),
        shared_payer_enabled=True,
        overage_enabled=overage,
        per_action_limit_points=per_action,
        daily_limit_points=daily,
        monthly_limit_points=monthly,
        reason="pytest 开启员工费用代付",
        expected_version=expected_version,
        request_id=request_id or _uid("payer-policy"),
        source_ip="192.0.2.7",
    )


def _member_limits(owner, membership_id: int, *, daily: int, monthly: int) -> None:
    configure_limit(owner, membership_id=membership_id, limit_kind="daily_total", limit_points=daily, reason="pytest 日上限")
    configure_limit(owner, membership_id=membership_id, limit_kind="monthly_total", limit_points=monthly, reason="pytest 月上限")


def _reserve(member, *, execution_id: str, feature_code: str = "geo_diagnosis", brand_id: int | None = 10, extra: int = 0):
    return asyncio.run(
        reserve_charge(
            member,
            execution_id=execution_id,
            feature_code=feature_code,
            work_kind="diagnosis.run",
            payload={"pytest": True},
            brand_id=brand_id,
            dynamic_ceiling_extra_points=extra,
        )
    )


def _expect_code(code: str, callback) -> Exception:
    try:
        callback()
    except Exception as exc:  # noqa: BLE001 - asserts the domain code only
        assert getattr(exc, "code", None) == code, f"expected {code}, got {getattr(exc, 'code', None)}: {exc!r}"
        return exc
    raise AssertionError(f"expected {code}")


def _execute_provider_boundary(member, charge: dict) -> str:
    """Simulate a worker that really reached the provider boundary.

    settle 的耐久证据闸（外部审核裁决 2026-07-23）要求：真实 claim +
    outbox.external_side_effect_started_at + 非空 outcome 快照。本辅助函数走
    真实公开入口（claim_live_charge → mark_external_side_effect_started），
    返回活跃 claim_token 供 settle 绑定证据 (a)。
    """
    claimed = claim_live_charge(member, charge_link_id=int(charge["id"]))
    assert claimed["claim_token"]
    mark_external_side_effect_started(
        charge_link_id=int(charge["id"]),
        claim_token=str(claimed["claim_token"]),
    )
    return str(claimed["claim_token"])


def _count(sql: str, params=()) -> int:
    return int(sql_value(TEST_DSN, sql, params))


# ===========================================================================
# Policy SSOT
# ===========================================================================


def test_legacy_organizations_default_disabled():
    organization_id, owner = _make_org(3)
    view = get_payer_policy(owner)
    assert view["viewer"] == "owner"
    assert view["policy"]["shared_payer_enabled"] is False
    assert view["policy"]["overage_enabled"] is False
    assert view["policy"]["policy_version"] == 0
    membership_id, member = _make_member(owner, organization_id, 4, brand_id=None)
    _member_limits(owner, membership_id, daily=1000, monthly=10000)
    before_charges = _count("SELECT COUNT(*) FROM organization_charge_links")
    before_freezes = _count("SELECT COUNT(*) FROM point_freezes")
    _expect_code("ORG_SHARED_PAYER_CONSENT_MISSING", lambda: _reserve(member, execution_id=_uid("exec"), brand_id=None))
    assert _count("SELECT COUNT(*) FROM organization_charge_links") == before_charges
    assert _count("SELECT COUNT(*) FROM point_freezes") == before_freezes


def test_member_and_outsider_cannot_enable_or_raise():
    organization_id, owner = _make_org(5)
    _, member = _make_member(owner, organization_id, 6)
    _expect_code(
        "ORG_OWNER_REQUIRED",
        lambda: put_payer_policy(
            identity=member,
            actor_user_id=6,
            shared_payer_enabled=True,
            overage_enabled=True,
            per_action_limit_points=10,
            daily_limit_points=10,
            monthly_limit_points=10,
            reason="员工试图越权开启",
            expected_version=0,
            request_id=_uid("payer-policy"),
            source_ip="192.0.2.8",
        ),
    )
    _expect_code(
        "ORG_MEMBERSHIP_REQUIRED",
        lambda: put_payer_policy(
            identity=None,
            actor_user_id=7,
            is_platform_admin=False,
            organization_id=organization_id,
            shared_payer_enabled=True,
            overage_enabled=True,
            per_action_limit_points=10,
            daily_limit_points=10,
            monthly_limit_points=10,
            reason="外部账号试图开启",
            expected_version=0,
            request_id=_uid("payer-policy"),
        ),
    )


def test_owner_enable_requires_positive_limits():
    _, owner = _make_org(8)
    for caps in (
        {"per_action_limit_points": None, "daily_limit_points": 10, "monthly_limit_points": 10},
        {"per_action_limit_points": 0, "daily_limit_points": 10, "monthly_limit_points": 10},
        {"per_action_limit_points": 10, "daily_limit_points": 10, "monthly_limit_points": None},
    ):
        _expect_code(
            "ORG_PAYER_POLICY_LIMITS_REQUIRED",
            lambda caps=caps: put_payer_policy(
                identity=owner,
                actor_user_id=8,
                shared_payer_enabled=True,
                overage_enabled=True,
                reason="缺硬顶",
                expected_version=0,
                request_id=_uid("payer-policy"),
                **caps,
            ),
        )


def test_overage_requires_shared_payer_enabled():
    """R3：shared=false + overage=true 惰性组合 → 422 显式拒绝。

    member_payer_consent 先闸 shared_payer_enabled，该组合下 overage 永不
    可达，配置面只会误导老板。判别性：删掉 _desired_state 的组合校验 →
    惰性组合静默落库，本测试 422 断言转红。
    """
    _, owner = _make_org(45)
    error = _expect_code(
        "ORG_PAYER_POLICY_OVERAGE_REQUIRES_SHARED",
        lambda: put_payer_policy(
            identity=owner,
            actor_user_id=45,
            shared_payer_enabled=False,
            overage_enabled=True,
            per_action_limit_points=100,
            daily_limit_points=500,
            monthly_limit_points=2000,
            reason="惰性组合",
            expected_version=0,
            request_id=_uid("payer-policy"),
        ),
    )
    assert getattr(error, "http_status", None) == 422
    assert "请先开启共享钱包" in getattr(error, "message", "")
    # 组合被拒后策略保持默认关闭，零版本推进
    view = get_payer_policy(owner)
    assert view["policy"]["shared_payer_enabled"] is False
    assert view["policy"]["overage_enabled"] is False
    assert view["policy"]["policy_version"] == 0


def test_negative_caps_rejected_422_not_500():
    """R3：负值上限前置 422（disabled 态负值不再撞 DB CHECK 变 500）。

    判别性：删掉 _desired_state 的负值校验 → UPSERT 触发
    organization_payer_policies 的 CHECK 违规，code 不再是
    ORG_PAYER_POLICY_LIMIT_NEGATIVE 且 http_status 不再是 422，断言转红。
    """
    _, owner = _make_org(47)
    for caps in (
        {"per_action_limit_points": -1, "daily_limit_points": 10, "monthly_limit_points": 10},
        {"per_action_limit_points": 10, "daily_limit_points": -5, "monthly_limit_points": 10},
        {"per_action_limit_points": 10, "daily_limit_points": 10, "monthly_limit_points": -100},
    ):
        error = _expect_code(
            "ORG_PAYER_POLICY_LIMIT_NEGATIVE",
            lambda caps=caps: put_payer_policy(
                identity=owner,
                actor_user_id=47,
                shared_payer_enabled=False,
                overage_enabled=False,
                reason="负值上限",
                expected_version=0,
                request_id=_uid("payer-policy"),
                **caps,
            ),
        )
        assert getattr(error, "http_status", None) == 422


def test_owner_enable_cas_replay_audit_without_authority_bump():
    organization_id, owner = _make_org(9)
    authority_before = _count("SELECT authority_version FROM organizations WHERE id=%s", (organization_id,))
    request_id = _uid("payer-policy")
    result = _enable_policy(owner, per_action=100, daily=500, monthly=2000, request_id=request_id)
    assert result["replayed"] is False and result["policy_version"] == 1
    # 开启是语义放宽：不得 bump authority_version（否则会误杀刚授权的在途任务）
    assert _count("SELECT authority_version FROM organizations WHERE id=%s", (organization_id,)) == authority_before
    replayed = put_payer_policy(
        identity=owner,
        actor_user_id=9,
        shared_payer_enabled=True,
        overage_enabled=True,
        per_action_limit_points=100,
        daily_limit_points=500,
        monthly_limit_points=2000,
        reason="pytest 开启员工费用代付",
        expected_version=0,
        request_id=request_id,
        source_ip="192.0.2.7",
    )
    assert replayed["replayed"] is True
    assert _count("SELECT COUNT(*) FROM organization_payer_policy_events WHERE organization_id=%s", (organization_id,)) == 1
    _expect_code(
        "ORG_IDEMPOTENCY_CONFLICT",
        lambda: put_payer_policy(
            identity=owner,
            actor_user_id=9,
            shared_payer_enabled=True,
            overage_enabled=False,
            per_action_limit_points=100,
            daily_limit_points=500,
            monthly_limit_points=2000,
            reason="同键不同内容",
            expected_version=0,
            request_id=request_id,
        ),
    )
    _expect_code(
        "ORG_PAYER_POLICY_VERSION_CONFLICT",
        lambda: _enable_policy(owner, expected_version=3),
    )
    event = sql_value(
        TEST_DSN,
        "SELECT row_to_json(e)::text FROM organization_payer_policy_events e WHERE organization_id=%s",
        (organization_id,),
    )
    import json

    event_row = json.loads(event)
    assert event_row["actor_kind"] == "owner"
    assert event_row["actor_user_id"] == 9
    assert event_row["source_ip"] == "192.0.2.7"
    assert event_row["old_snapshot"] is None
    assert event_row["new_snapshot"]["shared_payer_enabled"] is True
    assert event_row["new_snapshot"]["policy_version"] == 1
    assert event_row["reason"] == "pytest 开启员工费用代付"


def test_admin_emergency_disable_only():
    organization_id, owner = _make_org(10)
    _enable_policy(owner)
    # ADMIN must not enable, raise, or act without an explicit target.
    _expect_code(
        "ORG_PAYER_POLICY_ADMIN_DISABLE_ONLY",
        lambda: put_payer_policy(
            identity=None,
            actor_user_id=80,
            is_platform_admin=True,
            organization_id=organization_id,
            shared_payer_enabled=True,
            overage_enabled=True,
            per_action_limit_points=999,
            daily_limit_points=999,
            monthly_limit_points=999,
            reason="管理员试图提额",
            expected_version=1,
            request_id=_uid("payer-policy"),
        ),
    )
    _expect_code(
        "ORG_PAYER_POLICY_TARGET_REQUIRED",
        lambda: put_payer_policy(
            identity=None,
            actor_user_id=80,
            is_platform_admin=True,
            organization_id=None,
            shared_payer_enabled=False,
            overage_enabled=False,
            per_action_limit_points=100,
            daily_limit_points=500,
            monthly_limit_points=2000,
            reason="缺目标组织",
            expected_version=1,
            request_id=_uid("payer-policy"),
        ),
    )
    result = put_payer_policy(
        identity=None,
        actor_user_id=80,
        is_platform_admin=True,
        organization_id=organization_id,
        shared_payer_enabled=False,
        overage_enabled=False,
        per_action_limit_points=100,
        daily_limit_points=500,
        monthly_limit_points=2000,
        reason="风险处置：平台管理员紧急关闭代付",
        expected_version=1,
        request_id=_uid("payer-policy"),
        source_ip="198.51.100.9",
    )
    assert result["shared_payer_enabled"] is False and result["policy_version"] == 2
    import json

    event_row = json.loads(
        sql_value(
            TEST_DSN,
            "SELECT row_to_json(e)::text FROM organization_payer_policy_events e WHERE organization_id=%s ORDER BY id DESC LIMIT 1",
            (organization_id,),
        )
    )
    assert event_row["actor_kind"] == "platform_admin"
    assert event_row["actor_user_id"] == 80
    assert event_row["source_ip"] == "198.51.100.9"
    assert event_row["old_snapshot"]["shared_payer_enabled"] is True
    assert event_row["new_snapshot"]["shared_payer_enabled"] is False
    row = sql_value(
        TEST_DSN,
        "SELECT row_to_json(p)::text FROM organization_payer_policies p WHERE organization_id=%s",
        (organization_id,),
    )
    assert json.loads(row)["disabled_at"] is not None


def test_policy_events_are_append_only():
    _, owner = _make_org(11)
    _enable_policy(owner)
    with pytest.raises(psycopg2.errors.RaiseException):
        with connect(TEST_DSN) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE organization_payer_policy_events SET reason='tamper'")
    with pytest.raises(psycopg2.errors.RaiseException):
        with connect(TEST_DSN) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM organization_payer_policy_events")


# ===========================================================================
# Overage split algorithm
# ===========================================================================


def test_member_within_limit_uses_allowance_only():
    organization_id, owner = _make_org(12)
    _brand(901, 12)
    membership_id, member = _make_member(owner, organization_id, 13, brand_id=901)
    _member_limits(owner, membership_id, daily=1000, monthly=10000)
    _enable_policy(owner)
    charge = _reserve(member, execution_id=_uid("exec"), brand_id=901)
    assert charge["within_limit_points"] == 50 and charge["overage_points"] == 0
    assert _count("SELECT COUNT(*) FROM point_freezes WHERE user_id=12 AND amount_total=50") == 1
    assert _count(
        "SELECT COUNT(*) FROM organization_charge_limit_links WHERE charge_link_id=%s AND reserved_points=50",
        (charge["id"],),
    ) >= 1
    claim_token = _execute_provider_boundary(member, charge)
    settled = asyncio.run(settle_charge(charge_link_id=charge["id"], actual_points=50, result_payload={"ok": True}, claim_token=claim_token))
    assert settled["status"] == "committed"
    assert _count("SELECT paid_points FROM user_wallets WHERE user_id=12") == 2_000_000 - 50
    # Every bucket leg counts the within-limit portion once (daily + monthly).
    assert _count(
        "SELECT consumed_points FROM organization_spend_limits WHERE membership_id=%s AND limit_kind='daily_total'",
        (membership_id,),
    ) == 50
    assert _count(
        "SELECT consumed_points FROM organization_spend_limits WHERE membership_id=%s AND limit_kind='monthly_total'",
        (membership_id,),
    ) == 50
    assert _count("SELECT overage_points FROM organization_charge_links WHERE id=%s", (charge["id"],)) == 0
    assert billing_consistency()["ready"]


def test_overage_disabled_rejects_with_zero_side_effects():
    organization_id, owner = _make_org(14)
    _brand(902, 14)
    membership_id, member = _make_member(owner, organization_id, 15, brand_id=902)
    _member_limits(owner, membership_id, daily=30, monthly=30)
    _enable_policy(owner, overage=False)
    before_charges = _count("SELECT COUNT(*) FROM organization_charge_links")
    before_freezes = _count("SELECT COUNT(*) FROM point_freezes")
    before_outbox = _count("SELECT COUNT(*) FROM organization_work_outbox")
    _expect_code("ORG_OVERAGE_NOT_CONSENTED", lambda: _reserve(member, execution_id=_uid("exec"), brand_id=902))
    assert _count("SELECT COUNT(*) FROM organization_charge_links") == before_charges
    assert _count("SELECT COUNT(*) FROM point_freezes") == before_freezes
    assert _count("SELECT COUNT(*) FROM organization_work_outbox") == before_outbox
    assert billing_consistency()["ready"]


def test_overage_within_caps_single_freeze_and_dual_legs():
    organization_id, owner = _make_org(16)
    _brand(903, 16)
    membership_id, member = _make_member(owner, organization_id, 17, brand_id=903)
    _member_limits(owner, membership_id, daily=30, monthly=30)
    _enable_policy(owner, per_action=100, daily=500, monthly=1000)
    charge = _reserve(member, execution_id=_uid("exec"), brand_id=903)
    assert charge["within_limit_points"] == 30 and charge["overage_points"] == 20
    # Exactly one physical owner-wallet freeze for the whole ceiling.
    assert _count("SELECT COUNT(*) FROM point_freezes WHERE user_id=16 AND amount_total=50 AND status='frozen'") == 1
    row = sql_value(
        TEST_DSN,
        "SELECT payer_policy_version,owner_consent_snapshot IS NOT NULL,employee_limit_snapshot IS NOT NULL FROM organization_charge_links WHERE id=%s",
        (charge["id"],),
    )
    claim_token = _execute_provider_boundary(member, charge)
    settled = asyncio.run(settle_charge(charge_link_id=charge["id"], actual_points=50, result_payload={"ok": True}, claim_token=claim_token))
    assert settled["status"] == "committed"
    assert _count("SELECT paid_points FROM user_wallets WHERE user_id=16") == 2_000_000 - 50
    assert _count(
        "SELECT consumed_points FROM organization_spend_limits WHERE membership_id=%s AND limit_kind='daily_total'",
        (membership_id,),
    ) == 30
    final = sql_value(
        TEST_DSN,
        "SELECT within_limit_points || ':' || overage_points FROM organization_charge_links WHERE id=%s",
        (charge["id"],),
    )
    assert final == "30:20"
    view = get_payer_policy(owner)
    assert view["usage"]["daily_overage_used_points"] == 20
    assert view["usage"]["monthly_overage_used_points"] == 20
    assert view["usage"]["daily_overage_remaining_points"] == 480
    assert billing_consistency()["ready"]


def test_overage_caps_exceeded_with_zero_external_effects():
    organization_id, owner = _make_org(18)
    _brand(904, 18)
    membership_id, member = _make_member(owner, organization_id, 19, brand_id=904)
    _member_limits(owner, membership_id, daily=30, monthly=30)
    _enable_policy(owner, per_action=10, daily=500, monthly=1000)
    before = (_count("SELECT COUNT(*) FROM organization_charge_links"), _count("SELECT COUNT(*) FROM point_freezes"))
    _expect_code("ORG_OVERAGE_PER_ACTION_CAP_EXCEEDED", lambda: _reserve(member, execution_id=_uid("exec"), brand_id=904))
    put_payer_policy(
        identity=owner, actor_user_id=18, shared_payer_enabled=True, overage_enabled=True,
        per_action_limit_points=100, daily_limit_points=15, monthly_limit_points=1000,
        reason="收紧每日上限", expected_version=1, request_id=_uid("payer-policy"),
    )
    _expect_code("ORG_OVERAGE_DAILY_CAP_EXCEEDED", lambda: _reserve(member, execution_id=_uid("exec"), brand_id=904))
    put_payer_policy(
        identity=owner, actor_user_id=18, shared_payer_enabled=True, overage_enabled=True,
        per_action_limit_points=100, daily_limit_points=500, monthly_limit_points=19,
        reason="收紧每月上限", expected_version=2, request_id=_uid("payer-policy"),
    )
    _expect_code("ORG_OVERAGE_MONTHLY_CAP_EXCEEDED", lambda: _reserve(member, execution_id=_uid("exec"), brand_id=904))
    assert (_count("SELECT COUNT(*) FROM organization_charge_links"), _count("SELECT COUNT(*) FROM point_freezes")) == before
    assert billing_consistency()["ready"]


def test_unknown_quarantine_always_counts_toward_caps_until_force_released():
    """外部审核裁决（覆盖 R3 的 24h 豁免）：unknown 始终占额，直到人工解决。

    判别性：若 _overage_usage 恢复任何时间豁免分支（如 24h/30 天过滤）→
    过期 25h/30 天的 unknown charge 不再占额，"新 reservation 被拒"断言转红；
    若 force_release_charge 不释放冻结腿 → 释放后额度恢复断言转红。
    """
    organization_id, owner = _make_org(49)
    _brand(914, 49)
    membership_id, member = _make_member(owner, organization_id, 50, brand_id=914)
    _member_limits(owner, membership_id, daily=30, monthly=30)
    _enable_policy(owner, per_action=100, daily=60, monthly=60)
    stale_execution = _uid("exec")
    charge = _reserve(member, execution_id=stale_execution, brand_id=914)
    assert charge["within_limit_points"] == 30 and charge["overage_points"] == 20
    # 隔离态 #1：unknown + lease 刚过期 1h → 全额占额，新 overage reservation 拒
    with connect(TEST_DSN) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_charge_links SET status='unknown',lease_until=NOW()-INTERVAL '1 hour' WHERE id=%s",
            (charge["id"],),
        )
    _expect_code("ORG_OVERAGE_DAILY_CAP_EXCEEDED", lambda: _reserve(member, execution_id=_uid("exec"), brand_id=914))
    # 隔离态 #2：lease 过期满 25h → 仍全额占额（无 24h 豁免）
    with connect(TEST_DSN) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_charge_links SET lease_until=NOW()-INTERVAL '25 hours' WHERE id=%s",
            (charge["id"],),
        )
    _expect_code("ORG_OVERAGE_DAILY_CAP_EXCEEDED", lambda: _reserve(member, execution_id=_uid("exec"), brand_id=914))
    # 隔离态 #3：lease 过期满 30 天 → 仍全额占额（无长期豁免）
    with connect(TEST_DSN) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_charge_links SET lease_until=NOW()-INTERVAL '30 days' WHERE id=%s",
            (charge["id"],),
        )
    _expect_code("ORG_OVERAGE_DAILY_CAP_EXCEEDED", lambda: _reserve(member, execution_id=_uid("exec"), brand_id=914))
    # 冻结点保持冻结：隔离 charge 维持 unknown，物理冻结不释放
    assert _count(
        "SELECT COUNT(*) FROM organization_charge_links WHERE id=%s AND status='unknown'",
        (charge["id"],),
    ) == 1
    assert _count("SELECT COUNT(*) FROM point_freezes WHERE user_id=49 AND status='frozen'") == 1
    # 人工解决：owner force-release → 额度恢复、资金腿正确回冲、审计完整
    with connect(TEST_DSN) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_work_outbox SET status='unknown',lease_until=NOW()-INTERVAL '30 days' WHERE charge_link_id=%s",
            (charge["id"],),
        )
    force_request_id = _uid("force-release")
    released = asyncio.run(
        force_release_charge(
            identity=owner,
            actor_user_id=49,
            charge_link_id=charge["id"],
            reason="人工核对：provider 未执行，退回冻结",
            request_id=force_request_id,
            source_ip="192.0.2.30",
        )
    )
    assert released["status"] == "released" and released["replayed"] is False
    assert _count("SELECT COUNT(*) FROM point_freezes WHERE user_id=49 AND status='frozen'") == 0
    assert _count("SELECT paid_points FROM user_wallets WHERE user_id=49") == 2_000_000
    assert _count(
        "SELECT reserved_points FROM organization_spend_limits WHERE membership_id=%s AND limit_kind='daily_total'",
        (membership_id,),
    ) == 0
    # 审计完整：actor/IP/reason/before/after 齐全
    import json

    audit_row = json.loads(
        sql_value(
            TEST_DSN,
            "SELECT row_to_json(a)::text FROM organization_audit_events a WHERE action='billing.force_release' AND entity_id=%s",
            (str(charge["id"]),),
        )
    )
    assert audit_row["organization_id"] == organization_id
    assert audit_row["actor_kind"] == "owner"
    assert audit_row["actor_user_id"] == 49
    assert audit_row["reason"] == "人工核对：provider 未执行，退回冻结"
    assert audit_row["before_snapshot"]["status"] == "unknown"
    assert audit_row["before_snapshot"]["overage_points"] == 20
    assert audit_row["after_snapshot"]["status"] == "released"
    assert audit_row["after_snapshot"]["manual_actor_kind"] == "owner"
    assert audit_row["after_snapshot"]["source_ip"] == "192.0.2.30"
    # 额度恢复：新 reservation 放行——员工日额度腿已退回（within 30），
    # overage 硬顶已释放（20 ≤ 日顶 60）
    allowed = _reserve(member, execution_id=_uid("exec"), brand_id=914)
    assert not allowed.get("replayed")
    assert allowed["within_limit_points"] == 30 and allowed["overage_points"] == 20
    # 幂等 replay：同 request_id 重放不重复释放、不重复审计
    replayed = asyncio.run(
        force_release_charge(
            identity=owner,
            actor_user_id=49,
            charge_link_id=charge["id"],
            reason="人工核对：provider 未执行，退回冻结",
            request_id=force_request_id,
            source_ip="192.0.2.30",
        )
    )
    assert replayed["replayed"] is True
    assert _count(
        "SELECT COUNT(*) FROM organization_audit_events WHERE action='billing.force_release' AND entity_id=%s",
        (str(charge["id"]),),
    ) == 1
    asyncio.run(release_charge(charge_link_id=allowed["id"], reason="pytest 收尾释放"))
    assert billing_consistency()["ready"]


def test_force_release_gates_status_lease_and_actor():
    """force-release 受控终态：非 unknown/未过期 409，非 owner/admin 403。

    判别性：去掉 status='unknown' 闸 → reserved charge 被人工释放，两条 409
    断言转红；去掉 lease 闸 → 活跃租约被释放，转红；去掉 actor 闸 → 员工/
    外部账号释放成功，403 断言转红。
    """
    organization_id, owner = _make_org(55)
    _brand(916, 55)
    membership_id, member = _make_member(owner, organization_id, 56, brand_id=916)
    _member_limits(owner, membership_id, daily=30, monthly=30)
    _enable_policy(owner)
    charge = _reserve(member, execution_id=_uid("exec"), brand_id=916)
    # 非 unknown（reserved）→ 409
    _expect_code(
        "ORG_CHARGE_NOT_FORCE_RELEASABLE",
        lambda: asyncio.run(
            force_release_charge(
                identity=owner, actor_user_id=55, charge_link_id=charge["id"],
                reason="reserved 试图人工释放", request_id=_uid("force-release"),
            )
        ),
    )
    # unknown 但 lease 未过期 → 409
    with connect(TEST_DSN) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_charge_links SET status='unknown',lease_until=NOW()+INTERVAL '1 hour' WHERE id=%s",
            (charge["id"],),
        )
    _expect_code(
        "ORG_CHARGE_LEASE_ACTIVE",
        lambda: asyncio.run(
            force_release_charge(
                identity=owner, actor_user_id=55, charge_link_id=charge["id"],
                reason="租约未过期试图人工释放", request_id=_uid("force-release"),
            )
        ),
    )
    with connect(TEST_DSN) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_charge_links SET lease_until=NOW()-INTERVAL '1 hour' WHERE id=%s",
            (charge["id"],),
        )
    # 员工（非 owner 非 admin）→ 403
    _expect_code(
        "ORG_OWNER_REQUIRED",
        lambda: asyncio.run(
            force_release_charge(
                identity=member, actor_user_id=56, charge_link_id=charge["id"],
                reason="员工试图人工释放", request_id=_uid("force-release"),
            )
        ),
    )
    # 外部账号（无身份且非 admin）→ 404
    _expect_code(
        "ORG_MEMBERSHIP_REQUIRED",
        lambda: asyncio.run(
            force_release_charge(
                identity=None, actor_user_id=57, is_platform_admin=False, charge_link_id=charge["id"],
                reason="外部账号试图人工释放", request_id=_uid("force-release"),
            )
        ),
    )
    # 伪 admin 旗标但 users 表无 admin 角色 → 403
    _expect_code(
        "PLATFORM_ADMIN_REQUIRED",
        lambda: asyncio.run(
            force_release_charge(
                identity=None, actor_user_id=57, is_platform_admin=True, charge_link_id=charge["id"],
                reason="伪管理员试图人工释放", request_id=_uid("force-release"),
            )
        ),
    )
    # 他组织老板 → 404（不泄露存在性）
    _, other_owner = _make_org(58)
    _expect_code(
        "ORG_CHARGE_NOT_FOUND",
        lambda: asyncio.run(
            force_release_charge(
                identity=other_owner, actor_user_id=58, charge_link_id=charge["id"],
                reason="跨组织老板试图人工释放", request_id=_uid("force-release"),
            )
        ),
    )
    # 收尾：unknown 隔离单走 force-release 受控通道退回冻结
    asyncio.run(
        force_release_charge(
            identity=owner, actor_user_id=55, charge_link_id=charge["id"],
            reason="pytest 收尾人工释放", request_id=_uid("force-release"),
        )
    )
    assert billing_consistency()["ready"]


def test_unexecuted_reservation_cannot_settle_only_release():
    """settle 耐久证据闸：未越过 provider 边界的 reservation 只能 release。

    判别性：删掉 outbox.external_side_effect_started_at 闸 → 未执行 reservation
    直接 settle 成功（冻结点被确认成消费），ORG_CHARGE_NOT_EXECUTED 断言转红；
    删掉 outcome 快照闸 → 空 result_payload settle 成功，转红；claim_token
    退回 Optional 不传即放行 → TypeError 断言转红。
    """
    organization_id, owner = _make_org(60)
    _brand(917, 60)
    membership_id, member = _make_member(owner, organization_id, 61, brand_id=917)
    _member_limits(owner, membership_id, daily=30, monthly=30)
    _enable_policy(owner)
    # 路径 1：活跃入口不传 claim_token → 立即 TypeError（必填关键字参数，
    # 外部独立审核裁决 2026-07-23：活跃路径禁止隐式恢复模式）
    charge = _reserve(member, execution_id=_uid("exec"), brand_id=917)
    try:
        asyncio.run(settle_charge(charge_link_id=charge["id"], actual_points=50, result_payload={"ok": True}))
    except TypeError:
        pass
    else:
        raise AssertionError("settle_charge without claim_token must raise TypeError")
    # reconciliation 专用入口同样被证据闸拒绝（未到达 provider 边界）
    _expect_code(
        "ORG_CHARGE_NOT_EXECUTED",
        lambda: asyncio.run(
            settle_charge_via_reconciliation(
                charge_link_id=charge["id"], actual_points=50,
                result_payload={"ok": True}, recovery_identity="pytest:unexecuted-reservation",
            )
        ),
    )
    # 路径 2：有 claim 但未到达 provider 边界 → 仍拒（claim 本身不是执行证据）
    claimed = claim_live_charge(member, charge_link_id=charge["id"])
    _expect_code(
        "ORG_CHARGE_NOT_EXECUTED",
        lambda: asyncio.run(
            settle_charge(
                charge_link_id=charge["id"], actual_points=50,
                result_payload={"ok": True}, claim_token=claimed["claim_token"],
            )
        ),
    )
    # 未执行 reservation 只能 release（冻结退回）
    released = asyncio.run(release_charge(charge_link_id=charge["id"], reason="未执行，退回冻结"))
    assert released["status"] == "released"
    assert _count("SELECT paid_points FROM user_wallets WHERE user_id=60") == 2_000_000
    # 路径 3：越过 provider 边界但 outcome 快照为空 → 拒
    charge2 = _reserve(member, execution_id=_uid("exec"), brand_id=917)
    claim_token2 = _execute_provider_boundary(member, charge2)
    _expect_code(
        "ORG_CHARGE_OUTCOME_MISSING",
        lambda: asyncio.run(
            settle_charge(charge_link_id=charge2["id"], actual_points=50, result_payload={}, claim_token=claim_token2)
        ),
    )
    # 路径 4：伪造 claim_token → 拒（证据 (a) 必须匹配耐久记录）
    _expect_code(
        "ORG_WORK_LEASE_LOST",
        lambda: asyncio.run(
            settle_charge(
                charge_link_id=charge2["id"], actual_points=50,
                result_payload={"ok": True}, claim_token="forged-claim-token",
            )
        ),
    )
    # 正常执行 settle 不变：真实 claim + provider 边界 + outcome 快照 → committed
    settled = asyncio.run(
        settle_charge(charge_link_id=charge2["id"], actual_points=50, result_payload={"ok": True}, claim_token=claim_token2)
    )
    assert settled["status"] == "committed"
    assert _count("SELECT paid_points FROM user_wallets WHERE user_id=60") == 2_000_000 - 50
    assert billing_consistency()["ready"]


def test_owner_wallet_insufficient_blocks_with_zero_external_effects():
    organization_id, owner = _make_org(20, wallet=40)
    _brand(905, 20)
    membership_id, member = _make_member(owner, organization_id, 21, brand_id=905)
    _member_limits(owner, membership_id, daily=30, monthly=30)
    _enable_policy(owner)
    before = (_count("SELECT COUNT(*) FROM organization_charge_links"), _count("SELECT COUNT(*) FROM point_freezes"))
    try:
        _reserve(member, execution_id=_uid("exec"), brand_id=905)
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "http_status", None) in {402, 409, 503}
    else:
        raise AssertionError("expected wallet insufficiency rejection")
    assert (_count("SELECT COUNT(*) FROM organization_charge_links"), _count("SELECT COUNT(*) FROM point_freezes")) == before
    assert billing_consistency()["ready"]


def test_concurrent_duplicate_reservation_freezes_and_settles_once():
    from concurrent.futures import ThreadPoolExecutor

    organization_id, owner = _make_org(22)
    _brand(906, 22)
    membership_id, member = _make_member(owner, organization_id, 23, brand_id=906)
    _member_limits(owner, membership_id, daily=30, monthly=30)
    _enable_policy(owner)
    execution_id = _uid("exec")

    def attempt(_: int):
        try:
            return ("ok", _reserve(member, execution_id=execution_id, brand_id=906))
        except Exception as exc:  # noqa: BLE001
            return (getattr(exc, "code", type(exc).__name__), None)

    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(attempt, range(20)))
    oks = [item for item in results if item[0] == "ok"]
    assert len(oks) == 20, results
    charge_ids = {int(item[1]["id"]) for item in oks}
    assert len(charge_ids) == 1
    replays = [item for item in oks if item[1].get("replayed")]
    assert len(replays) == 19
    charge_id = charge_ids.pop()
    assert _count("SELECT COUNT(*) FROM organization_charge_links WHERE request_id=%s", (execution_id,)) == 1
    assert _count("SELECT COUNT(*) FROM organization_work_outbox WHERE execution_id=%s", (execution_id,)) == 1
    assert _count(
        "SELECT COUNT(*) FROM point_freezes WHERE user_id=22 AND task_ref=%s",
        (execution_id,),
    ) == 1
    assert billing_consistency()["ready"]


def test_kill9_settlement_recovery_and_lease_fencing():
    organization_id, owner = _make_org(24)
    _brand(907, 24)
    membership_id, member = _make_member(owner, organization_id, 25, brand_id=907)
    _member_limits(owner, membership_id, daily=30, monthly=30)
    _enable_policy(owner)
    charge = _reserve(member, execution_id=_uid("exec"), brand_id=907)
    claimed = claim_live_charge(member, charge_link_id=charge["id"])
    assert claimed["claim_token"]
    # A second claim while the first lease is live reports in-progress instead
    # of double-granting provider authority.
    second = claim_live_charge(member, charge_link_id=charge["id"])
    assert second.get("in_progress") is True and "claim_token" not in second
    # The worker really reaches the provider boundary, then dies (kill-9): the
    # claim token is lost but external_side_effect_started_at is durable.
    mark_external_side_effect_started(
        charge_link_id=charge["id"],
        claim_token=str(claimed["claim_token"]),
    )
    _expect_code(
        "ORG_WORK_LEASE_LOST",
        lambda: renew_settlement_lease(charge_link_id=charge["id"], claim_token="dead-worker-token"),
    )
    # After the provider boundary no new claim is ever granted: the execution
    # is durably ambiguous and must reconcile, never double-execute.
    _expect_code(
        "ORG_WORK_CLAIM_CONFLICT",
        lambda: claim_live_charge(member, charge_link_id=charge["id"]),
    )
    # The recovery path reconciles the durable reservation without provider
    # authority. settle 的证据 (a) 等价物：external_side_effect_started_at 只能
    # 由持有有效 claim 的 worker 耐久写入，证明真实执行曾到达 provider 边界。
    # 外部独立审核裁决（2026-07-23）：kill-9 恢复必须走显式 reconciliation
    # 专用入口并记录 recovery_identity，不再有"省略 token 即恢复"的隐式模式。
    # 2026-07-23 统一 R3 §六收紧：活跃 lease 未过期时对账入口拒收（防与活跃
    # worker 双结算）——kill-9 恢复必须等租约自然死亡后再进 reconciliation。
    with connect(TEST_DSN) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_work_outbox SET lease_until=NOW()-INTERVAL '1 hour' WHERE charge_link_id=%s",
            (charge["id"],),
        )
        cursor.execute(
            "UPDATE organization_charge_links SET lease_until=NOW()-INTERVAL '1 hour' WHERE id=%s",
            (charge["id"],),
        )
    state = get_charge_reconciliation_state(member, charge_link_id=charge["id"])
    assert state["status"] == "reserved"
    settled = asyncio.run(
        settle_charge_via_reconciliation(
            charge_link_id=charge["id"], actual_points=45,
            result_payload={"recovered": True},
            recovery_identity="pytest:kill9-settlement-recovery",
        )
    )
    assert settled["status"] == "committed" and settled["actual_points"] == 45
    # 审计必须能回答"谁/什么流程在恢复"
    import json as _json

    settle_audit = _json.loads(
        sql_value(
            TEST_DSN,
            "SELECT row_to_json(a)::text FROM organization_audit_events a "
            "WHERE action='billing.settle' AND entity_id=%s",
            (str(charge["id"]),),
        )
    )
    assert settle_audit["after_snapshot"]["recovery"] == {
        "mode": "reconciliation",
        "recovery_identity": "pytest:kill9-settlement-recovery",
    }
    replayed = asyncio.run(
        settle_charge_via_reconciliation(
            charge_link_id=charge["id"], actual_points=45,
            result_payload={"recovered": True},
            recovery_identity="pytest:kill9-settlement-recovery",
        )
    )
    assert replayed["replayed"] is True
    _expect_code(
        "ORG_SETTLE_CONFLICT",
        lambda: asyncio.run(
            settle_charge_via_reconciliation(
                charge_link_id=charge["id"], actual_points=46,
                result_payload={}, recovery_identity="pytest:kill9-settlement-recovery",
            )
        ),
    )
    assert _count("SELECT paid_points FROM user_wallets WHERE user_id=24") == 2_000_000 - 45
    final = sql_value(
        TEST_DSN,
        "SELECT within_limit_points || ':' || overage_points FROM organization_charge_links WHERE id=%s",
        (charge["id"],),
    )
    assert final == "30:15"
    assert billing_consistency()["ready"]


def test_settle_requires_live_claim_token_and_reconciliation_is_explicit():
    """外部独立审核裁决（2026-07-23）：settle 租约校验强制化。

    判别性：claim_token 退回 Optional → 不传不报 TypeError、空串不拒，前
    两条断言转红；删掉租约比对 → 过期 token/被抢 token settle 成功，中间
    两条转红；reconciliation 入口去掉 recovery_identity 强制 → 空身份
    恢复成功，末两条转红。
    """
    organization_id, owner = _make_org(62)
    _brand(918, 62)
    membership_id, member = _make_member(owner, organization_id, 63, brand_id=918)
    _member_limits(owner, membership_id, daily=30, monthly=30)
    _enable_policy(owner)
    charge = _reserve(member, execution_id=_uid("exec"), brand_id=918)
    # 不传 claim_token → TypeError（必填关键字参数，无隐式恢复模式）
    try:
        asyncio.run(settle_charge(charge_link_id=charge["id"], actual_points=50, result_payload={"ok": True}))
    except TypeError:
        pass
    else:
        raise AssertionError("settle_charge without claim_token must raise TypeError")
    # 空串/空白 token → 明确 ORG_CLAIM_TOKEN_REQUIRED（不是含糊的租约失配）
    claim_a = claim_live_charge(member, charge_link_id=charge["id"])
    _expect_code(
        "ORG_CLAIM_TOKEN_REQUIRED",
        lambda: asyncio.run(
            settle_charge(
                charge_link_id=charge["id"], actual_points=50,
                result_payload={"ok": True}, claim_token="   ",
            )
        ),
    )
    # 真实但已过期的租约 → ORG_WORK_LEASE_LOST
    claim_token_a = str(claim_a["claim_token"])
    mark_external_side_effect_started(charge_link_id=charge["id"], claim_token=claim_token_a)
    with connect(TEST_DSN) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_work_outbox SET lease_until=NOW()-INTERVAL '1 hour' WHERE charge_link_id=%s",
            (charge["id"],),
        )
    _expect_code(
        "ORG_WORK_LEASE_LOST",
        lambda: asyncio.run(
            settle_charge(
                charge_link_id=charge["id"], actual_points=50,
                result_payload={"ok": True}, claim_token=claim_token_a,
            )
        ),
    )
    # 过期租约的 charge 由 reconciliation 专用入口收尾（kill-9 等价现场：
    # 边界已越过、租约已死、outcome 快照齐备）→ committed，不留隔离残留
    recovered_a = asyncio.run(
        settle_charge_via_reconciliation(
            charge_link_id=charge["id"], actual_points=50,
            result_payload={"ok": True}, recovery_identity="pytest:expired-lease-recovery",
        )
    )
    assert recovered_a["status"] == "committed"
    # stale worker：A 租约过期被 B 重新 claim（provider 边界前），B 越过边界
    # 后 A 再持旧 token settle → ORG_WORK_LEASE_LOST；B 持新 token 正常结算
    charge2 = _reserve(member, execution_id=_uid("exec"), brand_id=918)
    claim_stale = claim_live_charge(member, charge_link_id=charge2["id"])
    with connect(TEST_DSN) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_work_outbox SET lease_until=NOW()-INTERVAL '1 hour' WHERE charge_link_id=%s",
            (charge2["id"],),
        )
    claim_fresh = claim_live_charge(member, charge_link_id=charge2["id"])
    assert claim_fresh["claim_token"] and claim_fresh["claim_token"] != claim_stale["claim_token"]
    mark_external_side_effect_started(charge_link_id=charge2["id"], claim_token=str(claim_fresh["claim_token"]))
    _expect_code(
        "ORG_WORK_LEASE_LOST",
        lambda: asyncio.run(
            settle_charge(
                charge_link_id=charge2["id"], actual_points=50,
                result_payload={"ok": True}, claim_token=str(claim_stale["claim_token"]),
            )
        ),
    )
    settled = asyncio.run(
        settle_charge(
            charge_link_id=charge2["id"], actual_points=50,
            result_payload={"ok": True}, claim_token=str(claim_fresh["claim_token"]),
        )
    )
    assert settled["status"] == "committed"
    # reconciliation 入口：recovery_identity 必填（缺省 TypeError、空白 422）
    charge3 = _reserve(member, execution_id=_uid("exec"), brand_id=918)
    claim3 = claim_live_charge(member, charge_link_id=charge3["id"])
    mark_external_side_effect_started(charge_link_id=charge3["id"], claim_token=str(claim3["claim_token"]))
    try:
        asyncio.run(
            settle_charge_via_reconciliation(
                charge_link_id=charge3["id"], actual_points=50, result_payload={"ok": True},
            )
        )
    except TypeError:
        pass
    else:
        raise AssertionError("settle_charge_via_reconciliation without recovery_identity must raise TypeError")
    _expect_code(
        "ORG_RECOVERY_IDENTITY_REQUIRED",
        lambda: asyncio.run(
            settle_charge_via_reconciliation(
                charge_link_id=charge3["id"], actual_points=50,
                result_payload={"ok": True}, recovery_identity="  ",
            )
        ),
    )
    # 无 token 的 reconciliation 可走通（证据闸一致：边界 + outcome 快照）。
    # 2026-07-23 统一 R3 §六收紧：活跃 lease 未过期时对账入口拒收——先让
    # 租约死亡（kill-9 等价现场：边界已越过、租约已死、outcome 快照齐备）。
    with connect(TEST_DSN) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_work_outbox SET lease_until=NOW()-INTERVAL '1 hour' WHERE charge_link_id=%s",
            (charge3["id"],),
        )
        cursor.execute(
            "UPDATE organization_charge_links SET lease_until=NOW()-INTERVAL '1 hour' WHERE id=%s",
            (charge3["id"],),
        )
    recovered = asyncio.run(
        settle_charge_via_reconciliation(
            charge_link_id=charge3["id"], actual_points=50,
            result_payload={"ok": True}, recovery_identity="pytest:explicit-reconciliation",
        )
    )
    assert recovered["status"] == "committed"
    assert billing_consistency()["ready"]


def test_admin_owner_of_org_a_can_govern_org_b():
    """平台 ADMIN 拥有自己团队时仍可治理其他组织（外部独立审核裁决 2026-07-23）。

    判别性：force_release/put policy 回退"优先当 owner"旧逻辑 → ADMIN（组织
    A owner）对组织 B 的 force-release 返 404、紧急关闭返 403，本测试前
    两段转红；非 admin 的组织 A' owner 碰 B 仍 404/403（负向不泄漏）。
    """
    # 用户 66：平台 ADMIN 且是组织 A 的 owner（生产常见状态）
    organization_a, owner_a = _make_org(66)
    with connect(TEST_DSN) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO user_roles(user_id,role_id) SELECT 66,id FROM roles WHERE name='admin'"
        )
    # 组织 B：另一个老板 + 员工 + 已开启代付
    organization_b, owner_b = _make_org(68)
    _brand(919, 68)
    membership_b, member_b = _make_member(owner_b, organization_b, 69, brand_id=919)
    _member_limits(owner_b, membership_b, daily=30, monthly=30)
    _enable_policy(owner_b)
    # 两笔 unknown+过期隔离单都提前备好：紧急关闭后新 reservation 会被
    # consent 闸拒绝，负向用例的 charge 必须在关闭前落库
    charge_b = _reserve(member_b, execution_id=_uid("exec"), brand_id=919)
    charge_b2 = _reserve(member_b, execution_id=_uid("exec"), brand_id=919)
    with connect(TEST_DSN) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_charge_links SET status='unknown',lease_until=NOW()-INTERVAL '2 hours' WHERE id=ANY(%s)",
            ([charge_b["id"], charge_b2["id"]],),
        )
    # ADMIN（org A owner 身份）对 org B 的 unknown 隔离单 force-release → 成功
    released = asyncio.run(
        force_release_charge(
            identity=owner_a, actor_user_id=66, is_platform_admin=True,
            charge_link_id=charge_b["id"],
            reason="风险处置：平台管理员人工释放隔离单",
            request_id=_uid("force-release"), source_ip="203.0.113.7",
        )
    )
    assert released["status"] == "released"
    import json as _json

    force_audit = _json.loads(
        sql_value(
            TEST_DSN,
            "SELECT row_to_json(a)::text FROM organization_audit_events a "
            "WHERE action='billing.force_release' AND entity_id=%s",
            (str(charge_b["id"]),),
        )
    )
    # 审计 actor 语义与纯 admin 路径一致：枚举列落 system，真实身份在快照
    assert force_audit["organization_id"] == organization_b
    assert force_audit["actor_kind"] == "system"
    assert force_audit["after_snapshot"]["manual_actor_kind"] == "platform_admin"
    assert force_audit["after_snapshot"]["manual_actor_user_id"] == 66
    # ADMIN（org A owner 身份）对 org B 紧急关闭代付 → 成功且 events 语义正确
    result = put_payer_policy(
        identity=owner_a,
        actor_user_id=66,
        is_platform_admin=True,
        organization_id=organization_b,
        shared_payer_enabled=False,
        overage_enabled=False,
        per_action_limit_points=100,
        daily_limit_points=500,
        monthly_limit_points=2000,
        reason="风险处置：平台管理员紧急关闭代付",
        expected_version=1,
        request_id=_uid("payer-policy"),
        source_ip="203.0.113.7",
    )
    assert result["shared_payer_enabled"] is False and result["policy_version"] == 2
    event_row = _json.loads(
        sql_value(
            TEST_DSN,
            "SELECT row_to_json(e)::text FROM organization_payer_policy_events e "
            "WHERE organization_id=%s ORDER BY id DESC LIMIT 1",
            (organization_b,),
        )
    )
    assert event_row["actor_kind"] == "platform_admin"
    assert event_row["actor_user_id"] == 66
    assert event_row["source_ip"] == "203.0.113.7"
    # 次级审计不污染管理员自己组织 A 的审计流
    assert _count(
        "SELECT COUNT(*) FROM organization_audit_events WHERE organization_id=%s AND action='payer_policy.update'",
        (organization_a,),
    ) == 0
    # 负向：非 admin 的组织 A' owner 碰 B → force-release 404（不泄露存在性）、
    # 紧急关闭 403（scope mismatch），ADMIN 修复不得放大普通老板权限
    _, owner_plain = _make_org(70)
    _expect_code(
        "ORG_CHARGE_NOT_FOUND",
        lambda: asyncio.run(
            force_release_charge(
                identity=owner_plain, actor_user_id=70,
                charge_link_id=charge_b2["id"],
                reason="跨组织老板试图人工释放", request_id=_uid("force-release"),
            )
        ),
    )
    _expect_code(
        "ORG_PAYER_POLICY_SCOPE_MISMATCH",
        lambda: put_payer_policy(
            identity=owner_plain,
            actor_user_id=70,
            organization_id=organization_b,
            shared_payer_enabled=False,
            overage_enabled=False,
            per_action_limit_points=100,
            daily_limit_points=500,
            monthly_limit_points=2000,
            reason="跨组织老板试图紧急关闭",
            expected_version=2,
            request_id=_uid("payer-policy"),
        ),
    )
    # 收尾：B 的老板走 owner 路径人工释放第二笔隔离单（不留 unknown 残留）
    asyncio.run(
        force_release_charge(
            identity=owner_b, actor_user_id=68, charge_link_id=charge_b2["id"],
            reason="pytest 收尾人工释放", request_id=_uid("force-release"),
        )
    )
    assert billing_consistency()["ready"]


def test_refund_restores_owner_money_leg_and_member_allowance():
    organization_id, owner = _make_org(26)
    _brand(908, 26)
    membership_id, member = _make_member(owner, organization_id, 27, brand_id=908)
    _member_limits(owner, membership_id, daily=30, monthly=30)
    _enable_policy(owner)
    charge = _reserve(member, execution_id=_uid("exec"), brand_id=908)
    claim_token = _execute_provider_boundary(member, charge)
    asyncio.run(settle_charge(charge_link_id=charge["id"], actual_points=50, result_payload={"ok": True}, claim_token=claim_token))
    partial = asyncio.run(
        refund_charge(
            charge_link_id=charge["id"],
            cumulative_refund_target=10,
            refund_request_id=_uid("refund"),
            reason="部分退款",
        )
    )
    assert partial["refunded_points"] == 10
    # The first 30 refund points restore the employee allowance leg.
    assert _count(
        "SELECT refunded_points FROM organization_spend_limits WHERE membership_id=%s AND limit_kind='daily_total'",
        (membership_id,),
    ) == 10
    assert _count("SELECT overage_points FROM organization_charge_links WHERE id=%s", (charge["id"],)) == 20
    full = asyncio.run(
        refund_charge(
            charge_link_id=charge["id"],
            cumulative_refund_target=50,
            refund_request_id=_uid("refund"),
            reason="全额退款",
        )
    )
    assert full["status"] == "refunded"
    assert _count("SELECT paid_points FROM user_wallets WHERE user_id=26") == 2_000_000
    assert _count(
        "SELECT refunded_points FROM organization_spend_limits WHERE membership_id=%s AND limit_kind='daily_total'",
        (membership_id,),
    ) == 30
    assert _count("SELECT overage_points FROM organization_charge_links WHERE id=%s", (charge["id"],)) == 0
    view = get_payer_policy(owner)
    assert view["usage"]["daily_overage_used_points"] == 0
    assert billing_consistency()["ready"]


def test_policy_disable_blocks_new_reservations_only_inflight_completes():
    """外部审核裁决（覆盖 R1）：关闭代付不 bump 代际、不中断在途，只闸新 reservation。

    判别性：若 put_payer_policy 恢复任何 authority_version bump → 关闭后
    authority 不变断言与 claim 成功断言转红（代际围栏误杀在途）。
    """
    organization_id, owner = _make_org(28)
    _brand(909, 28)
    membership_id, member = _make_member(owner, organization_id, 29, brand_id=909)
    _member_limits(owner, membership_id, daily=1000, monthly=10000)
    _enable_policy(owner)
    charge = _reserve(member, execution_id=_uid("exec"), brand_id=909)
    authority_before = _count("SELECT authority_version FROM organizations WHERE id=%s", (organization_id,))
    put_payer_policy(
        identity=owner, actor_user_id=28, shared_payer_enabled=False, overage_enabled=False,
        per_action_limit_points=100, daily_limit_points=500, monthly_limit_points=2000,
        reason="老板立即关闭代付", expected_version=1, request_id=_uid("payer-policy"),
    )
    # 关闭是策略变化：只影响新 reservation 的 consent 闸，永不 bump 代际
    assert _count("SELECT authority_version FROM organizations WHERE id=%s", (organization_id,)) == authority_before
    # 在途 charge 不被中断：可 claim、越过 provider 边界、按冻结快照完成
    claim_token = _execute_provider_boundary(member, charge)
    settled = asyncio.run(settle_charge(charge_link_id=charge["id"], actual_points=50, result_payload={"ok": True}, claim_token=claim_token))
    assert settled["status"] == "committed"
    # 新 reservation 立即被 live policy 的 consent 闸拒绝，零任务零冻结
    before = (_count("SELECT COUNT(*) FROM organization_charge_links"), _count("SELECT COUNT(*) FROM point_freezes"))
    _expect_code("ORG_SHARED_PAYER_CONSENT_MISSING", lambda: _reserve(member, execution_id=_uid("exec"), brand_id=909))
    assert (_count("SELECT COUNT(*) FROM organization_charge_links"), _count("SELECT COUNT(*) FROM point_freezes")) == before
    assert billing_consistency()["ready"]


def test_loosening_policy_update_does_not_fence_inflight_charge():
    """提额/仅改 reason 不 bump authority_version：在途 charge 按快照完成。

    判别性：若 put_payer_policy 恢复任何 authority_version bump → authority
    不变断言与 claim 成功断言同时转红（代际围栏误杀）。
    """
    organization_id, owner = _make_org(36)
    _brand(911, 36)
    membership_id, member = _make_member(owner, organization_id, 37, brand_id=911)
    _member_limits(owner, membership_id, daily=30, monthly=30)
    _enable_policy(owner, per_action=100, daily=500, monthly=1000)
    charge = _reserve(member, execution_id=_uid("exec"), brand_id=911)
    assert charge["within_limit_points"] == 30 and charge["overage_points"] == 20
    authority_before = _count("SELECT authority_version FROM organizations WHERE id=%s", (organization_id,))
    # 语义放宽 #1：提高月度硬顶 1000 → 5000
    raised = put_payer_policy(
        identity=owner, actor_user_id=36, shared_payer_enabled=True, overage_enabled=True,
        per_action_limit_points=100, daily_limit_points=500, monthly_limit_points=5000,
        reason="老板提高月度上限", expected_version=1, request_id=_uid("payer-policy"),
    )
    assert raised["policy_version"] == 2
    # 语义不变：仅改 reason
    remarked = put_payer_policy(
        identity=owner, actor_user_id=36, shared_payer_enabled=True, overage_enabled=True,
        per_action_limit_points=100, daily_limit_points=500, monthly_limit_points=5000,
        reason="仅更新备注", expected_version=2, request_id=_uid("payer-policy"),
    )
    assert remarked["policy_version"] == 3
    # 两次放宽/备注更新都不得触碰组织权限代际
    assert _count("SELECT authority_version FROM organizations WHERE id=%s", (organization_id,)) == authority_before
    # 在途 charge 不被代际围栏误杀：可 claim、越过 provider 边界、按冻结快照结算出 30:20 双腿
    claim_token = _execute_provider_boundary(member, charge)
    settled = asyncio.run(settle_charge(charge_link_id=charge["id"], actual_points=50, result_payload={"ok": True}, claim_token=claim_token))
    assert settled["status"] == "committed"
    assert sql_value(
        TEST_DSN,
        "SELECT within_limit_points || ':' || overage_points FROM organization_charge_links WHERE id=%s",
        (charge["id"],),
    ) == "30:20"
    assert billing_consistency()["ready"]


def test_tightening_cap_decrease_never_bumps_inflight_settles_per_snapshot():
    """外部审核裁决（覆盖 R1）：降额不 bump 代际、在途按快照完成，新 reservation 按 live 闸。

    判别性：若 put_payer_policy 恢复"收紧 bump" → authority +0 断言转红；
    若 settle 重新读 live policy → 30:20 快照腿断言转红；若 reserve 读快照
    而非 live policy → 新 reservation 的 ORG_OVERAGE_DAILY_CAP_EXCEEDED 转红。
    """
    organization_id, owner = _make_org(38)
    _brand(912, 38)
    membership_id, member = _make_member(owner, organization_id, 39, brand_id=912)
    _member_limits(owner, membership_id, daily=30, monthly=30)
    _enable_policy(owner, per_action=100, daily=500, monthly=1000)
    charge = _reserve(member, execution_id=_uid("exec"), brand_id=912)
    assert charge["overage_points"] == 20
    authority_before = _count("SELECT authority_version FROM organizations WHERE id=%s", (organization_id,))
    # 语义收紧：每日硬顶 500 → 10（策略编辑永不 bump）
    tightened = put_payer_policy(
        identity=owner, actor_user_id=38, shared_payer_enabled=True, overage_enabled=True,
        per_action_limit_points=100, daily_limit_points=10, monthly_limit_points=1000,
        reason="收紧每日上限", expected_version=1, request_id=_uid("payer-policy"),
    )
    assert tightened["policy_version"] == 2
    assert _count("SELECT authority_version FROM organizations WHERE id=%s", (organization_id,)) == authority_before
    # 在途 charge 不被中断：可 claim、越过 provider 边界
    claim_token = _execute_provider_boundary(member, charge)
    # 已冻结的订单按不可变快照完成结算（快照含 owner_consent）
    settled = asyncio.run(settle_charge(charge_link_id=charge["id"], actual_points=50, result_payload={"ok": True}, claim_token=claim_token))
    assert settled["status"] == "committed" and settled["overage_points"] == 20
    assert sql_value(
        TEST_DSN,
        "SELECT within_limit_points || ':' || overage_points FROM organization_charge_links WHERE id=%s",
        (charge["id"],),
    ) == "30:20"
    # 新 reservation 立即按新硬顶判定：已结算 20 + 新 50 远超每日 10
    _expect_code("ORG_OVERAGE_DAILY_CAP_EXCEEDED", lambda: _reserve(member, execution_id=_uid("exec"), brand_id=912))
    assert billing_consistency()["ready"]


def test_idempotent_replay_survives_payer_consent_disable():
    """关代付后同 request_id 同参重试返原 charge（replay 在 consent 闸之前）。

    判别性：把 replay 查找挪回 consent 闸之后 → 重试拿到 403
    ORG_SHARED_PAYER_CONSENT_MISSING 而非原 charge → 前两条断言转红。
    """
    organization_id, owner = _make_org(40)
    _brand(913, 40)
    membership_id, member = _make_member(owner, organization_id, 41, brand_id=913)
    _member_limits(owner, membership_id, daily=30, monthly=30)
    _enable_policy(owner)
    execution_id = _uid("exec")
    charge = _reserve(member, execution_id=execution_id, brand_id=913)
    assert charge["replayed"] is False
    # 老板关闭代付（外部审核裁决：策略编辑永不 bump 代际，只闸新 reservation）
    authority_before = _count("SELECT authority_version FROM organizations WHERE id=%s", (organization_id,))
    put_payer_policy(
        identity=owner, actor_user_id=40, shared_payer_enabled=False, overage_enabled=False,
        per_action_limit_points=100, daily_limit_points=500, monthly_limit_points=2000,
        reason="老板立即关闭代付", expected_version=1, request_id=_uid("payer-policy"),
    )
    assert _count("SELECT authority_version FROM organizations WHERE id=%s", (organization_id,)) == authority_before
    # 客户端重试原始请求（同 execution_id + 同 payload）：返原 charge，不 403 丢句柄
    replayed = _reserve(member, execution_id=execution_id, brand_id=913)
    assert int(replayed["id"]) == int(charge["id"])
    assert replayed["replayed"] is True
    # 同键异参仍 409 fail closed
    _expect_code(
        "ORG_IDEMPOTENCY_CONFLICT",
        lambda: asyncio.run(
            reserve_charge(
                member,
                execution_id=execution_id,
                feature_code="geo_diagnosis",
                work_kind="diagnosis.run",
                payload={"pytest": "tampered"},
                brand_id=913,
            )
        ),
    )
    # 全新 execution_id 的新 reservation 仍被 consent 闸拒绝
    _expect_code("ORG_SHARED_PAYER_CONSENT_MISSING", lambda: _reserve(member, execution_id=_uid("exec"), brand_id=913))
    # 在途 charge 按快照退款，资金/额度现场干净
    released = asyncio.run(release_charge(charge_link_id=charge["id"], reason="代付已关闭，释放预留"))
    assert released["status"] == "released"
    assert billing_consistency()["ready"]


def test_admin_emergency_disable_keeps_target_org_out_of_actor_audit_stream():
    """ADMIN 紧急关闭：组织 A 审计流无 B 实体，events 主审计完整。

    判别性：若 admin_override 路径重新用 locked_identity（组织 A）写 _audit
    → 组织 A 的 payer_policy.update 审计行带上 B 的 entity_id → 两条 0 计数
    断言转红。
    """
    organization_a, owner_a = _make_org(42)
    # 平台管理员同时持有组织 A 的普通成员身份（遗留场景：先入组织后授 admin）。
    # 邀请评估禁止 admin 转换，因此 role 授予在 accept 之后由 fixture 直接落库。
    _, admin_member = _make_member(owner_a, organization_a, 44)
    with connect(TEST_DSN) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO user_roles(user_id,role_id) SELECT 44,id FROM roles WHERE name='admin'"
        )
    organization_b, owner_b = _make_org(43)
    _enable_policy(owner_b)
    result = put_payer_policy(
        identity=admin_member,
        actor_user_id=44,
        is_platform_admin=True,
        organization_id=organization_b,
        shared_payer_enabled=False,
        overage_enabled=False,
        per_action_limit_points=100,
        daily_limit_points=500,
        monthly_limit_points=2000,
        reason="风险处置：平台管理员紧急关闭代付",
        expected_version=1,
        request_id=_uid("payer-policy"),
        source_ip="198.51.100.10",
    )
    assert result["shared_payer_enabled"] is False and result["policy_version"] == 2
    # 次级组织审计：管理员自己组织 A 的审计流里绝不允许出现 payer 写操作
    assert _count(
        "SELECT COUNT(*) FROM organization_audit_events WHERE organization_id=%s AND action='payer_policy.update'",
        (organization_a,),
    ) == 0
    # B 的 payer 实体只能出现在 B 自己的审计流（owner enable 合法记录）；
    # 任何"非本组织"流里出现 B 实体都是错记
    assert _count(
        "SELECT COUNT(*) FROM organization_audit_events WHERE entity_type='organization_payer_policy' AND entity_id=%s AND organization_id<>%s",
        (str(organization_b), organization_b),
    ) == 0
    # 主审计（append-only events）完整：actor 身份、来源 IP、新旧快照齐全
    import json

    event_row = json.loads(
        sql_value(
            TEST_DSN,
            "SELECT row_to_json(e)::text FROM organization_payer_policy_events e WHERE organization_id=%s ORDER BY id DESC LIMIT 1",
            (organization_b,),
        )
    )
    assert event_row["actor_kind"] == "platform_admin"
    assert event_row["actor_user_id"] == 44
    assert event_row["source_ip"] == "198.51.100.10"
    assert event_row["old_snapshot"]["shared_payer_enabled"] is True
    assert event_row["new_snapshot"]["shared_payer_enabled"] is False
    assert event_row["reason"] == "风险处置：平台管理员紧急关闭代付"


def test_member_suspend_and_brand_unassign_block_new_external_calls():
    organization_id, owner = _make_org(30)
    _brand(910, 30)
    membership_id, member = _make_member(owner, organization_id, 31, brand_id=910)
    _member_limits(owner, membership_id, daily=1000, monthly=10000)
    _enable_policy(owner)
    charge = _reserve(member, execution_id=_uid("exec"), brand_id=910)
    set_member_status(owner, membership_id=membership_id, action="suspend", reason="pytest 停用员工")
    _expect_code(
        "ORG_MEMBERSHIP_INACTIVE",
        lambda: claim_live_charge(member, charge_link_id=charge["id"]),
    )
    asyncio.run(release_charge(charge_link_id=charge["id"], reason="员工已停用"))
    set_member_status(owner, membership_id=membership_id, action="resume", reason="pytest 恢复员工")
    assign_brands(owner, membership_id=membership_id, brand_ids=[], request_id=_uid("assign"), reason="pytest 撤销客户分配")
    live_member = resolve_identity(31, request_id=_uid("member-31"))
    _expect_code("ORG_BRAND_NOT_ASSIGNED", lambda: _reserve(live_member, execution_id=_uid("exec"), brand_id=910))
    assert billing_consistency()["ready"]


def test_member_view_hides_owner_wallet_and_policy_caps():
    organization_id, owner = _make_org(32)
    membership_id, member = _make_member(owner, organization_id, 33)
    _member_limits(owner, membership_id, daily=1000, monthly=10000)
    _enable_policy(owner)
    view = get_payer_policy(member)
    assert view["viewer"] == "member"
    assert view["shared_payer_enabled"] is True and view["overage_enabled"] is True
    leaked = str(view)
    for forbidden in ("paid_points", "balance", "per_action_limit_points", "daily_limit_points", "monthly_limit_points", "cost_floor"):
        assert forbidden not in leaked


# ===========================================================================
# HTTP API
# ===========================================================================


class _ThrowawayAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        raw_user_id = request.headers.get("x-throwaway-user-id")
        if raw_user_id:
            request.state.user = {
                "user_id": int(raw_user_id),
                "id": int(raw_user_id),
                "username": f"user{int(raw_user_id)}",
                "is_admin": int(raw_user_id) == 80,
            }
        return await call_next(request)


def _build_app() -> FastAPI:
    from api.organization_api import router as organization_router
    from api.organization_payer_policy_api import router as payer_policy_router
    from middleware.organization_guard import setup_organization_guard

    app = FastAPI()

    @app.exception_handler(OrganizationError)
    async def _organization_error_handler(request, exc):  # noqa: ANN001
        from uuid import uuid4

        request_id = getattr(request.state, "organization_request_id", None) or str(uuid4())
        return JSONResponse(
            status_code=exc.http_status,
            content={"detail": exc.as_detail(request_id)},
            headers={"X-Request-ID": request_id, "Cache-Control": "no-store"},
        )

    app.include_router(organization_router)
    app.include_router(payer_policy_router)
    setup_organization_guard(app)
    app.add_middleware(_ThrowawayAuthMiddleware)
    return app


def test_payer_policy_http_endpoints():
    organization_id, owner = _make_org(34)
    membership_id, member = _make_member(owner, organization_id, 35)
    _member_limits(owner, membership_id, daily=1000, monthly=10000)
    app = _build_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        owner_headers = {"x-throwaway-user-id": "34"}
        member_headers = {"x-throwaway-user-id": "35"}
        admin_headers = {"x-throwaway-user-id": "80"}
        member_put = client.put(
            "/api/organization/payer-policy",
            headers=member_headers,
            json={
                "shared_payer_enabled": True,
                "overage_enabled": True,
                "per_action_limit_points": 100,
                "daily_limit_points": 500,
                "monthly_limit_points": 2000,
                "reason": "员工越权",
                "expected_version": 0,
                "request_id": _uid("http-policy"),
            },
        )
        assert member_put.status_code == 403
        assert member_put.json()["detail"]["code"] == "ORG_OWNER_REQUIRED"
        owner_put = client.put(
            "/api/organization/payer-policy",
            headers=owner_headers,
            json={
                "shared_payer_enabled": True,
                "overage_enabled": True,
                "per_action_limit_points": 100,
                "daily_limit_points": 500,
                "monthly_limit_points": 2000,
                "reason": "老板开启",
                "expected_version": 0,
                "request_id": _uid("http-policy"),
            },
        )
        assert owner_put.status_code == 200, owner_put.json()
        assert owner_put.json()["policy_version"] == 1
        owner_get = client.get("/api/organization/payer-policy", headers=owner_headers)
        assert owner_get.status_code == 200
        assert owner_get.json()["viewer"] == "owner"
        assert owner_get.json()["policy"]["daily_limit_points"] == 500
        member_get = client.get("/api/organization/payer-policy", headers=member_headers)
        assert member_get.status_code == 200
        member_view = member_get.json()
        assert member_view["viewer"] == "member"
        assert "policy" not in member_view and "paid_points" not in str(member_view)
        # force-release 端点：unknown 隔离 charge 的受控人工解决通道
        charge = _reserve(member, execution_id=_uid("exec"), brand_id=None)
        member_force = client.post(
            f"/api/organization/charges/{charge['id']}/force-release",
            headers=member_headers,
            json={"reason": "员工试图人工释放", "request_id": _uid("http-force")},
        )
        assert member_force.status_code == 403
        assert member_force.json()["detail"]["code"] == "ORG_OWNER_REQUIRED"
        owner_premature = client.post(
            f"/api/organization/charges/{charge['id']}/force-release",
            headers=owner_headers,
            json={"reason": "reserved 态试图人工释放", "request_id": _uid("http-force")},
        )
        assert owner_premature.status_code == 409
        assert owner_premature.json()["detail"]["code"] == "ORG_CHARGE_NOT_FORCE_RELEASABLE"
        with connect(TEST_DSN) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE organization_charge_links SET status='unknown',lease_until=NOW()-INTERVAL '2 hours' WHERE id=%s",
                (charge["id"],),
            )
        force_request_id = _uid("http-force")
        owner_force = client.post(
            f"/api/organization/charges/{charge['id']}/force-release",
            headers=owner_headers,
            json={"reason": "人工核对：provider 未执行，退回冻结", "request_id": force_request_id},
        )
        assert owner_force.status_code == 200, owner_force.json()
        assert owner_force.json()["status"] == "released" and owner_force.json()["replayed"] is False
        owner_force_replay = client.post(
            f"/api/organization/charges/{charge['id']}/force-release",
            headers=owner_headers,
            json={"reason": "人工核对：provider 未执行，退回冻结", "request_id": force_request_id},
        )
        assert owner_force_replay.status_code == 200
        assert owner_force_replay.json()["replayed"] is True
        charge2 = _reserve(member, execution_id=_uid("exec"), brand_id=None)
        with connect(TEST_DSN) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE organization_charge_links SET status='unknown',lease_until=NOW()-INTERVAL '2 hours' WHERE id=%s",
                (charge2["id"],),
            )
        admin_force = client.post(
            f"/api/organization/charges/{charge2['id']}/force-release",
            headers=admin_headers,
            json={"reason": "风险处置：平台管理员人工释放隔离单", "request_id": _uid("http-force")},
        )
        assert admin_force.status_code == 200, admin_force.json()
        assert admin_force.json()["status"] == "released"
        import json as _json

        admin_audit = _json.loads(
            sql_value(
                TEST_DSN,
                "SELECT row_to_json(a)::text FROM organization_audit_events a WHERE action='billing.force_release' AND entity_id=%s",
                (str(charge2["id"]),),
            )
        )
        assert admin_audit["actor_kind"] == "system"
        assert admin_audit["after_snapshot"]["manual_actor_kind"] == "platform_admin"
        assert admin_audit["after_snapshot"]["manual_actor_user_id"] == 80
        admin_enable = client.put(
            "/api/organization/payer-policy",
            headers=admin_headers,
            json={
                "shared_payer_enabled": True,
                "overage_enabled": True,
                "per_action_limit_points": 100,
                "daily_limit_points": 500,
                "monthly_limit_points": 2000,
                "reason": "管理员试图开启",
                "expected_version": 1,
                "request_id": _uid("http-policy"),
                "organization_id": organization_id,
            },
        )
        assert admin_enable.status_code == 403
        assert admin_enable.json()["detail"]["code"] == "ORG_PAYER_POLICY_ADMIN_DISABLE_ONLY"
        admin_disable = client.put(
            "/api/organization/payer-policy",
            headers=admin_headers,
            json={
                "shared_payer_enabled": False,
                "overage_enabled": False,
                "per_action_limit_points": 100,
                "daily_limit_points": 500,
                "monthly_limit_points": 2000,
                "reason": "管理员紧急关闭",
                "expected_version": 1,
                "request_id": _uid("http-policy"),
                "organization_id": organization_id,
            },
        )
        assert admin_disable.status_code == 200, admin_disable.json()
        assert admin_disable.json()["shared_payer_enabled"] is False


# ===========================================================================
# Migration matrix and catalog decoys
# ===========================================================================


def test_migration_fresh_chain_is_ready_and_signed():
    result = readiness()
    assert result["ready"], result
    assert result["schema_contract"]["matched_variant"] == "fresh_pg16_payer_policies_v1"
    assert result["payer_policy"]["ready"]
    assert result["payer_policy"]["global_shared_payer_flag"] is True


def test_migration_rollback_then_forward_in_own_schema():
    from db.organization_db import readiness as readiness_fn

    _, rollback_dsn = _build_schema("org_payer_rollback", rollback=True)
    with connect(rollback_dsn) as conn:
        result = readiness_fn(cursor=conn.cursor())
    assert result["ready"], result
    assert result["schema_contract"]["matched_variant"] == "fresh_pg16_payer_policies_v1"
    # Rollback alone removes the payer surface and turns readiness red.
    halfway_schema, halfway_dsn = create_schema(DSN, "org_payer_half")
    execute_sql(halfway_dsn, BASE_SQL)
    execute_sql(halfway_dsn, MIGRATION_SQL)
    execute_sql(halfway_dsn, ONBOARDING_MIGRATION_SQL)
    cross = isolate_public_qualified_sql(CROSS_TENANT_MIGRATION_SQL, halfway_schema)
    execute_sql(halfway_dsn, cross)
    execute_sql(halfway_dsn, PAYER_MIGRATION_SQL)
    execute_sql(halfway_dsn, ROLLBACK_SQL)
    with connect(halfway_dsn) as conn:
        dropped = readiness_fn(cursor=conn.cursor())
    assert not dropped["ready"]
    assert "organization_payer_policies" in dropped["missing_tables"]
    assert "organization_payer_policy_events" in dropped["missing_tables"]
    execute_sql(halfway_dsn, PAYER_MIGRATION_SQL)
    with connect(halfway_dsn) as conn:
        restored = readiness_fn(cursor=conn.cursor())
    assert restored["ready"], restored


def test_migration_static_guards():
    sql = PAYER_MIGRATION_SQL
    assert "to_regclass(format('%I.%I', current_schema(), 'organizations'))" in sql
    assert "BEFORE UPDATE OR DELETE ON organization_payer_policy_events" in sql
    assert "append-only" in sql
    # No broad UPDATE ever guesses historical consent.
    assert "UPDATE organization_payer_policies" not in sql
    assert "UPDATE organization_payer_policy_events" not in sql
    # Every NOT VALID constraint is validated in the same migration.
    assert sql.count("NOT VALID") == sql.count("VALIDATE CONSTRAINT")
    # The three owner hard caps are non-negative by CHECK and nullable.
    for column in ("per_action_limit_points", "daily_limit_points", "monthly_limit_points"):
        assert f"{column} BIGINT CHECK ({column} IS NULL OR {column} >= 0)" in sql
    # Additive frozen evidence columns.
    for column in (
        "payer_policy_version",
        "owner_consent_snapshot",
        "employee_limit_snapshot",
        "within_limit_points",
        "overage_points",
    ):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in sql


def test_readiness_rejects_payer_catalog_decoys():
    _, decoy_dsn = _build_schema("org_payer_decoy")
    from db.organization_db import readiness as readiness_fn

    execute_sql(decoy_dsn, "ALTER TABLE organization_payer_policies DROP COLUMN reason")
    with connect(decoy_dsn) as conn:
        result = readiness_fn(cursor=conn.cursor())
    assert not result["ready"] and not result["schema_contract"]["matches"]

    _, weak_dsn = _build_schema("org_payer_weakcheck")
    execute_sql(
        weak_dsn,
        """
        ALTER TABLE organization_payer_policies
            DROP CONSTRAINT organization_payer_policy_enabled_limits_required;
        ALTER TABLE organization_payer_policies
            ADD CONSTRAINT organization_payer_policy_enabled_limits_required CHECK (TRUE);
        """,
    )
    with connect(weak_dsn) as conn:
        weak = readiness_fn(cursor=conn.cursor())
    assert not weak["ready"] and not weak["schema_contract"]["matches"]


def test_enabled_policy_without_caps_turns_readiness_red():
    _, drift_dsn = _build_schema("org_payer_drift")
    from db.organization_db import readiness as readiness_fn

    # Bypass the CHECK the way a hostile/manual writer would try: drop it,
    # insert an enabled policy without caps, and require the readiness layer
    # (not just the constraint) to fail closed.
    execute_sql(
        drift_dsn,
        """
        INSERT INTO users(id,username,display_name) VALUES (1,'drift','漂移') ;
        INSERT INTO organizations(owner_user_id,creation_request_id,name)
        VALUES (1,'drift-org','漂移团队');
        ALTER TABLE organization_payer_policies
            DROP CONSTRAINT organization_payer_policy_enabled_limits_required;
        INSERT INTO organization_payer_policies(
          organization_id,shared_payer_enabled,overage_enabled,policy_version
        ) SELECT id,TRUE,TRUE,1 FROM organizations;
        """,
    )
    with connect(drift_dsn) as conn:
        result = readiness_fn(cursor=conn.cursor())
    assert not result["ready"]
    assert result["payer_policy"]["invalid_enabled_policies"] == 1


def test_final_billing_consistency_and_readiness():
    assert billing_consistency()["ready"]
    result = readiness()
    assert result["ready"], result
