"""真 PG16：邀请短信送达 + 员工免注册入职闭环（W1）。

覆盖合同 §2.W1 测试矩阵：
  - 已注册接受邀请回归
  - 未注册手机号 OTP 开户（local_capture + aliyun_sms mock）
  - 错码 / 过期 / 撤销 / 重放 / 跨团队
  - 20 路并发接受只建一个用户/成员
  - provider worker 20 并发同一 outbox 只真实发送一次（mock 计数）
  - 429/限流/超时退避重试、确定性错误直接终态、max_attempts dead-letter
  - disabled provider 不得假成功；邮箱邀请显式不可用
  - 新员工无任何推荐/佣金/钱包/角色/品牌资产
  - 未 sent 不得 verify（送达证据闸）
  - 登录短信回归：send_sms_code 仍自生成码并写 sms_codes；
    send_verification_code 绝不写 sms_codes、绝不自生成码

跑法：
  TEST_DATABASE_URL="postgresql://postgres:test_org_wallet_pw@localhost:55499/test_org_wallet" \
    "$DAIMON_USER_PYTHON" -m pytest tests/organization_internal_seats/ -q
"""

from __future__ import annotations

import os
import sys
import threading
import types
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

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
    connect,
    create_schema,
    execute_sql,
    install_environment,
    seed_runtime,
    sql_value,
)

PAYER_MIGRATION_SQL = (
    ROOT / "scripts" / "migration_organization_payer_policies_2026_07_23.sql"
).read_text(encoding="utf-8")

# [WP6] 用户名式邀请:放宽 organization_invites / operator_accounts 的 target_kind CHECK 含 'username'。
USERNAME_MIGRATION_SQL = (
    ROOT / "scripts" / "migration_organization_username_invite_2026_07_25.sql"
).read_text(encoding="utf-8")

TEST_DSN = os.environ["DATABASE_URL"]  # tests/conftest.py 已校验库名含 test

PASSWORD = "W1Operator!2026"


# ========== fake 阿里云 SDK ==========


class _FakeSendSmsRequest:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeBody:
    def __init__(self, code="OK", message="OK", biz_id="BIZ-fake-1"):
        self.code = code
        self.message = message
        self.biz_id = biz_id


class _FakeResponse:
    def __init__(self, body):
        self.body = body


class FakeSmsClient:
    """线程安全的假阿里云 client；behaviors 出队决定每次发送结果。"""

    def __init__(self, behaviors=()):
        self.calls: list[dict] = []
        self._behaviors = list(behaviors)
        self._lock = threading.Lock()

    def send_sms(self, request):
        with self._lock:
            self.calls.append(dict(request.kwargs))
            behavior = self._behaviors.pop(0) if self._behaviors else "ok"
        if isinstance(behavior, Exception):
            raise behavior
        if behavior == "ok":
            return _FakeResponse(_FakeBody())
        code, message = behavior
        return _FakeResponse(_FakeBody(code=code, message=message, biz_id=None))


@pytest.fixture
def fake_sms(monkeypatch):
    """stub 阿里云 SDK import + get_sms_client；绝不真实外发。"""
    client = FakeSmsClient()
    models_mod = types.ModuleType("alibabacloud_dysmsapi20170525.models")
    models_mod.SendSmsRequest = _FakeSendSmsRequest
    pkg = types.ModuleType("alibabacloud_dysmsapi20170525")
    pkg.models = models_mod
    monkeypatch.setitem(sys.modules, "alibabacloud_dysmsapi20170525", pkg)
    monkeypatch.setitem(sys.modules, "alibabacloud_dysmsapi20170525.models", models_mod)
    return client


@pytest.fixture
def patched_sms_client(fake_sms, monkeypatch):
    import auth.sms_service as sms

    monkeypatch.setattr(sms, "get_sms_client", lambda: fake_sms)
    return fake_sms


# ========== schema / 服务 fixture ==========


@pytest.fixture(scope="session")
def pg_schema():
    _, dsn = create_schema(TEST_DSN, "org_invite_sms")
    for sql in (
        BASE_SQL,
        MIGRATION_SQL,
        ONBOARDING_MIGRATION_SQL,
        PAYER_MIGRATION_SQL,
        USERNAME_MIGRATION_SQL,
        MIGRATION_SQL,
        ONBOARDING_MIGRATION_SQL,
        PAYER_MIGRATION_SQL,
        USERNAME_MIGRATION_SQL,
    ):
        execute_sql(dsn, sql)
    seed_runtime(dsn)
    install_environment(dsn)
    return dsn


@pytest.fixture(autouse=True)
def _invite_flag_environment(monkeypatch):
    """R3：flag env 改函数级 monkeypatch。feature_flags() 与
    _local_capture_enabled() 都是实时读 env——session 级直写无 teardown
    会泄漏给同进程后跑文件（payer 套件），让其 flag 相关用例静默失真。
    monkeypatch 在每个测试后自动还原原值。"""
    monkeypatch.setenv("ORGANIZATION_INVITE_ONBOARDING_ENABLED", "true")
    monkeypatch.setenv("ORGANIZATION_INVITE_DELIVERY_PROVIDER", "local_capture")
    monkeypatch.setenv("APP_ENV", "test")
    yield


@pytest.fixture(autouse=True)
def _rebind_pool(pg_schema):
    """db.connection 在模块级缓存 DATABASE_URL 且 _pool 全局唯一：同进程跑多个
    真 PG 测试文件（如 W2 payer 套件，模块级绑定自己的 schema）时，每个测试
    函数开始时把池运行时重绑到本 schema、结束还原（测试运行时重指向，不改
    db/connection.py 任何一行）；文件间因此互不污染。"""
    import db.connection as db_connection

    previous = (db_connection.DATABASE_URL, db_connection._pool)
    db_connection.DATABASE_URL = pg_schema
    db_connection._pool = None
    try:
        yield pg_schema
    finally:
        db_connection.DATABASE_URL, db_connection._pool = previous


@pytest.fixture(scope="session")
def svc(pg_schema):
    """在 env 就位后才 import 业务模块（db.connection 绑定 schema DSN）。"""
    from db.organization_db import resolve_identity
    from services.legal_agreements import PRIVACY_VERSION, USER_TERMS_VERSION
    from services.organization_contract import OrganizationError
    from services.organization_onboarding import (
        _derived_code,
        create_verification_challenge,
        get_challenge_delivery_status,
        list_invite_delivery_states,
        onboard_operator,
        onboard_operator_via_credential,
        organization_invite_delivery_tick,
        verify_challenge,
    )
    from services.organization_service import (
        accept_invite,
        create_invite,
        create_organization,
        revoke_invite,
    )

    return types.SimpleNamespace(
        dsn=pg_schema,
        resolve_identity=resolve_identity,
        OrganizationError=OrganizationError,
        create_organization=create_organization,
        create_invite=create_invite,
        accept_invite=accept_invite,
        revoke_invite=revoke_invite,
        create_verification_challenge=create_verification_challenge,
        verify_challenge=verify_challenge,
        onboard_operator=onboard_operator,
        onboard_operator_via_credential=onboard_operator_via_credential,
        organization_invite_delivery_tick=organization_invite_delivery_tick,
        get_challenge_delivery_status=get_challenge_delivery_status,
        list_invite_delivery_states=list_invite_delivery_states,
        derived_code=_derived_code,
        USER_TERMS_VERSION=USER_TERMS_VERSION,
        PRIVACY_VERSION=PRIVACY_VERSION,
    )


def _tag() -> str:
    return uuid.uuid4().hex[:10]


def _phone() -> str:
    return "+86137" + "".join(str(int(b) % 10) for b in uuid.uuid4().bytes[:8])


import itertools

# 一个账号只能属于一个组织：每个测试团队用不同的 owner（seed 用户 1-79，
# 80 是平台管理员；70/71 有商业绑定，留给被邀请场景，不做 owner）。
_owners = itertools.chain(range(1, 70), range(72, 79))


def make_org(svc) -> int:
    owner_id = next(_owners)
    svc.create_organization(
        owner_user_id=owner_id,
        name=f"W1送达测试团队{_tag()}",
        request_id=f"w1-org-{_tag()}",
    )
    return owner_id


def make_invite(svc, owner_id: int, *, target_kind: str = "phone", target: str | None = None) -> dict:
    identity = svc.resolve_identity(owner_id, request_id=f"w1-resolve-{_tag()}")
    role_id = int(
        sql_value(
            svc.dsn,
            "SELECT id FROM organization_roles WHERE organization_id=%s AND code='sales'",
            (identity.organization_id,),
        )
    )
    return svc.create_invite(
        identity,
        target_kind=target_kind,
        target=target or _phone(),
        role_id=role_id,
        request_id=f"w1-invite-{_tag()}",
        source_ip="192.0.2.1",
    )


def make_challenge(svc, invite: dict) -> dict:
    return svc.create_verification_challenge(
        token=invite["delivery_token"],
        request_id=f"w1-challenge-{_tag()}",
        source_ip="192.0.2.2",
    )


def derive_code(svc, challenge_id: int) -> str:
    with connect(svc.dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT receipt_derivation_id,code_key_version
               FROM organization_invite_verification_challenges WHERE id=%s""",
            (int(challenge_id),),
        )
        row = cursor.fetchone()
    code, _, _ = svc.derived_code(
        str(row["receipt_derivation_id"]), key_version=str(row["code_key_version"])
    )
    return code


def do_verify(svc, invite: dict, challenge: dict, code: str) -> dict:
    return svc.verify_challenge(
        challenge_id=int(challenge["challenge_id"]),
        token=invite["delivery_token"],
        code=code,
        request_id=f"w1-verify-{_tag()}",
        source_ip="192.0.2.3",
    )


def do_onboard(svc, invite: dict, challenge: dict, verified: dict, *, request_id: str | None = None) -> dict:
    return svc.onboard_operator(
        token=invite["delivery_token"],
        challenge_id=int(challenge["challenge_id"]),
        verification_receipt=verified["verification_receipt"],
        password=PASSWORD,
        display_name="W1测试员工",
        request_id=request_id or f"w1-onboard-{_tag()}",
        terms_accepted=True,
        privacy_accepted=True,
        terms_version=svc.USER_TERMS_VERSION,
        privacy_version=svc.PRIVACY_VERSION,
        source_ip="192.0.2.4",
        user_agent="w1-pytest",
    )


def outbox_rows(svc, challenge_id: int) -> list[dict]:
    with connect(svc.dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM organization_invite_delivery_outbox WHERE challenge_id=%s ORDER BY id",
            (int(challenge_id),),
        )
        return [dict(row) for row in cursor.fetchall()]


def force_due(svc, outbox_id: int) -> None:
    with connect(svc.dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_invite_delivery_outbox SET next_attempt_at=NOW()-INTERVAL '1 second' WHERE id=%s",
            (int(outbox_id),),
        )


def sweep_outbox(svc) -> None:
    """session 共享 schema：把其他测试遗留的可认领行全部终态化，隔离本测试的 tick。"""
    with connect(svc.dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE organization_invite_delivery_outbox
               SET status='cancelled',claim_token=NULL,lease_expires_at=NULL,
                   last_error_code='TEST_SWEEP',updated_at=NOW()
               WHERE status IN ('pending','retry','sending','unknown')"""
        )


def expect_error(svc, code: str, callback) -> Exception:
    with pytest.raises(svc.OrganizationError) as captured:
        callback()
    assert captured.value.code == code, f"expected {code}, got {captured.value.code}: {captured.value}"
    return captured.value


@pytest.fixture
def aliyun_provider(monkeypatch, patched_sms_client):
    monkeypatch.setenv("ORGANIZATION_INVITE_DELIVERY_PROVIDER", "aliyun_sms")
    monkeypatch.setenv("SMS_TEMPLATE_INVITE", "SMS_INVITE_TEST")
    return patched_sms_client


# ========== 1. 已注册接受邀请回归 ==========


def test_registered_user_accept_invite_regression(svc):
    sweep_outbox(svc)
    owner = make_org(svc)
    invite = make_invite(svc, owner, target="13900000070")
    accept_request_id = f"w1-accept-{_tag()}"
    accepted = svc.accept_invite(
        authenticated_user_id=70,
        token=invite["delivery_token"],
        request_id=accept_request_id,
        source_ip="192.0.2.10",
    )
    assert accepted["status"] == "active"
    # 同一 request_id 重放 → 返回原结果
    replayed = svc.accept_invite(
        authenticated_user_id=70,
        token=invite["delivery_token"],
        request_id=accept_request_id,
        source_ip="192.0.2.10",
    )
    assert replayed["membership_id"] == accepted["membership_id"]
    assert replayed["replayed"] is True
    # 不同 request_id → 幂等冲突
    expect_error(
        svc,
        "ORG_INVITE_IDEMPOTENCY_CONFLICT",
        lambda: svc.accept_invite(
            authenticated_user_id=70,
            token=invite["delivery_token"],
            request_id=f"w1-accept-{_tag()}",
            source_ip="192.0.2.10",
        ),
    )


# ========== 2. 未注册手机号 OTP 开户（local_capture） ==========


def test_unregistered_phone_otp_onboard_local_capture(svc):
    sweep_outbox(svc)
    owner = make_org(svc)
    invite = make_invite(svc, owner)
    challenge = make_challenge(svc, invite)
    assert challenge["test_code"] and challenge["delivery_queued"]
    verified = do_verify(svc, invite, challenge, challenge["test_code"])
    onboard_request_id = f"w1-onboard-{_tag()}"
    onboarded = do_onboard(svc, invite, challenge, verified, request_id=onboard_request_id)
    # JWT 直发：与登录一致的响应结构
    assert onboarded["success"] is True
    assert onboarded["auto_login"] is True
    assert onboarded["token"]
    assert int(onboarded["user"]["id"]) == int(onboarded["user_id"])
    assert onboarded["user"]["roles"] == []
    # 幂等重放也带回会话
    replayed = do_onboard(svc, invite, challenge, verified, request_id=onboard_request_id)
    assert replayed["replayed"] is True
    assert replayed["token"] and int(replayed["user"]["id"]) == int(onboarded["user_id"])
    assert int(sql_value(svc.dsn, "SELECT COUNT(*) FROM users WHERE id=%s", (onboarded["user_id"],))) == 1


# ========== 3. 错码 / 过期 / 撤销 / 重放 / 跨团队 ==========


def test_wrong_code_expired_and_revoked(svc):
    sweep_outbox(svc)
    owner = make_org(svc)
    invite = make_invite(svc, owner)
    challenge = make_challenge(svc, invite)
    wrong = "000000" if challenge["test_code"] != "000000" else "999999"
    expect_error(svc, "ORG_INVITE_CODE_MISMATCH", lambda: do_verify(svc, invite, challenge, wrong))

    # 过期：challenge 过期后正确码也拒（created_at 一并后移满足 expiry CHECK）
    with connect(svc.dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE organization_invite_verification_challenges
               SET created_at=NOW()-INTERVAL '20 minutes',expires_at=NOW()-INTERVAL '1 minute'
               WHERE id=%s""",
            (int(challenge["challenge_id"]),),
        )
    expect_error(
        svc,
        "ORG_INVITE_CHALLENGE_EXPIRED",
        lambda: do_verify(svc, invite, challenge, challenge["test_code"]),
    )

    # 撤销：撤销邀请后核验直接拒
    invite2 = make_invite(svc, owner)
    challenge2 = make_challenge(svc, invite2)
    svc.revoke_invite(
        svc.resolve_identity(owner, request_id=f"w1-resolve-{_tag()}"),
        invite_id=int(invite2["invite"]["id"]),
        reason="W1 撤销测试",
    )
    expect_error(
        svc,
        "ORG_INVITE_REVOKED",
        lambda: do_verify(svc, invite2, challenge2, challenge2["test_code"]),
    )


def test_cross_team_membership_rejected(svc):
    sweep_outbox(svc)
    owner = make_org(svc)
    invite_a = make_invite(svc, owner)
    challenge_a = make_challenge(svc, invite_a)
    verified_a = do_verify(svc, invite_a, challenge_a, challenge_a["test_code"])
    onboarded = do_onboard(svc, invite_a, challenge_a, verified_a)
    user_id = int(onboarded["user_id"])

    # 第二个团队向同一手机号发邀请：challenge 阶段即发现已有账号
    sweep_outbox(svc)
    owner_b = make_org(svc)
    phone = sql_value(svc.dsn, "SELECT phone FROM users WHERE id=%s", (user_id,))
    invite_b = make_invite(svc, owner_b, target=str(phone))
    expect_error(svc, "ORG_INVITEE_ACCOUNT_EXISTS", lambda: make_challenge(svc, invite_b))


# ========== 4. 20 路并发接受只建一个用户/成员 ==========


def test_concurrent_onboard_20_ways_creates_one_operator(svc):
    sweep_outbox(svc)
    owner = make_org(svc)
    invite = make_invite(svc, owner)
    challenge = make_challenge(svc, invite)
    verified = do_verify(svc, invite, challenge, challenge["test_code"])
    onboard_request_id = f"w1-onboard-{_tag()}"

    def attempt(index: int):
        try:
            return svc.onboard_operator(
                token=invite["delivery_token"],
                challenge_id=int(challenge["challenge_id"]),
                verification_receipt=verified["verification_receipt"],
                password=PASSWORD,
                display_name="W1并发员工",
                request_id=onboard_request_id,
                terms_accepted=True,
                privacy_accepted=True,
                terms_version=svc.USER_TERMS_VERSION,
                privacy_version=svc.PRIVACY_VERSION,
                source_ip=f"198.51.100.{index + 1}",
                user_agent="w1-pytest-concurrency",
            )
        except svc.OrganizationError as exc:
            return {"error": exc.code}

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(attempt, range(20)))
    user_ids = {int(r["user_id"]) for r in results if r.get("user_id")}
    errors = [r for r in results if r.get("error")]
    # 核心不变量：20 路并发只建一个用户/成员/操作员账号/回执。
    # target 维度限流(5/h)在获胜事务 commit 前可能拦住少量并发请求，
    # 这属于既有设计行为（verify_all_accounts_onboarding 同型断言）。
    assert len(user_ids) == 1, results
    assert all(r["error"] == "ORG_RATE_LIMITED" for r in errors), errors
    assert all(r.get("token") for r in results if r.get("user_id"))
    user_id = next(iter(user_ids))
    assert int(sql_value(svc.dsn, "SELECT COUNT(*) FROM organization_memberships WHERE user_id=%s", (user_id,))) == 1
    assert int(sql_value(svc.dsn, "SELECT COUNT(*) FROM organization_operator_accounts WHERE user_id=%s", (user_id,))) == 1
    assert int(
        sql_value(
            svc.dsn,
            "SELECT COUNT(*) FROM organization_invite_accept_receipts WHERE invite_id=%s",
            (invite["invite"]["id"],),
        )
    ) == 1


# ========== 5. provider worker 20 并发只发送一次 ==========


def test_provider_worker_20_concurrent_sends_once(svc, aliyun_provider):
    sweep_outbox(svc)
    owner = make_org(svc)
    invite = make_invite(svc, owner)
    challenge = make_challenge(svc, invite)
    rows = outbox_rows(svc, challenge["challenge_id"])
    assert len(rows) == 1 and rows[0]["status"] == "pending"

    with ThreadPoolExecutor(max_workers=20) as pool:
        tick_results = list(pool.map(lambda _: svc.organization_invite_delivery_tick(limit=10), range(20)))

    code_calls = [c for c in aliyun_provider.calls if c["template_code"] == "SMS_INVITE_TEST"]
    assert len(code_calls) == 1, aliyun_provider.calls
    assert sum(int(r["real_messages_sent"]) for r in tick_results) == 1
    row = outbox_rows(svc, challenge["challenge_id"])[0]
    assert row["status"] == "sent"
    assert row["sent_at"] is not None
    assert str(row["provider_message_reference"]).startswith("aliyun_sms:")
    assert row["claim_token"] is None and row["lease_expires_at"] is None
    # 送达状态派生：sent
    status = svc.get_challenge_delivery_status(
        challenge_id=int(challenge["challenge_id"]),
        token=invite["delivery_token"],
        request_id=f"w1-status-{_tag()}",
        source_ip="192.0.2.5",
    )
    assert status["delivery_state"] == "sent"


# ========== 6. 退避重试 / 确定性终态 / dead-letter / disabled ==========


def test_retryable_throttling_then_success_with_backoff(svc, aliyun_provider):
    aliyun_provider._behaviors.extend([("Throttling", "触发流控"), "ok"])
    sweep_outbox(svc)
    owner = make_org(svc)
    invite = make_invite(svc, owner)
    challenge = make_challenge(svc, invite)
    outbox_id = int(outbox_rows(svc, challenge["challenge_id"])[0]["id"])

    first = svc.organization_invite_delivery_tick(limit=10)
    assert first["real_messages_sent"] == 0
    row = outbox_rows(svc, challenge["challenge_id"])[0]
    assert row["status"] == "retry"
    assert row["last_error_code"] == "Throttling"
    assert int(row["attempt_count"]) == 1
    # 指数退避第一档 60s
    delta = (row["next_attempt_at"] - row["updated_at"]).total_seconds()
    assert 50 <= delta <= 70, delta

    # 未到退避时间不再发送
    svc.organization_invite_delivery_tick(limit=10)
    assert len(aliyun_provider.calls) == 1

    force_due(svc, outbox_id)
    second = svc.organization_invite_delivery_tick(limit=10)
    assert second["real_messages_sent"] == 1
    row = outbox_rows(svc, challenge["challenge_id"])[0]
    assert row["status"] == "sent" and int(row["attempt_count"]) == 2


def test_terminal_provider_error_goes_dead_letter_without_retry(svc, aliyun_provider):
    aliyun_provider._behaviors.append(("isv.MOBILE_NUMBER_ILLEGAL", "手机号非法"))
    sweep_outbox(svc)
    owner = make_org(svc)
    invite = make_invite(svc, owner)
    challenge = make_challenge(svc, invite)

    svc.organization_invite_delivery_tick(limit=10)
    row = outbox_rows(svc, challenge["challenge_id"])[0]
    assert row["status"] == "cancelled"
    assert row["last_error_code"] == "FAILED:isv.MOBILE_NUMBER_ILLEGAL"
    assert int(row["attempt_count"]) == 1
    assert len(aliyun_provider.calls) == 1

    # 终态后不再被认领重发
    svc.organization_invite_delivery_tick(limit=10)
    assert len(aliyun_provider.calls) == 1

    status = svc.get_challenge_delivery_status(
        challenge_id=int(challenge["challenge_id"]),
        token=invite["delivery_token"],
        request_id=f"w1-status-{_tag()}",
        source_ip="192.0.2.5",
    )
    assert status["delivery_state"] == "failed"
    # 公开面只返归类码：isv.* 原文留在 outbox.last_error_code（上面已断言）
    assert status["failure_code"] == "PARAM_ERROR"


def test_timeout_exception_is_retryable(svc, aliyun_provider):
    aliyun_provider._behaviors.extend([TimeoutError("read timeout"), "ok"])
    sweep_outbox(svc)
    owner = make_org(svc)
    invite = make_invite(svc, owner)
    challenge = make_challenge(svc, invite)

    svc.organization_invite_delivery_tick(limit=10)
    row = outbox_rows(svc, challenge["challenge_id"])[0]
    assert row["status"] == "retry"
    assert row["last_error_code"] == "SMS_SEND_EXCEPTION"

    force_due(svc, int(row["id"]))
    svc.organization_invite_delivery_tick(limit=10)
    row = outbox_rows(svc, challenge["challenge_id"])[0]
    assert row["status"] == "sent"


def test_max_attempts_dead_letter(svc, aliyun_provider):
    aliyun_provider._behaviors.append(("Throttling", "触发流控"))
    sweep_outbox(svc)
    owner = make_org(svc)
    invite = make_invite(svc, owner)
    challenge = make_challenge(svc, invite)
    row0 = outbox_rows(svc, challenge["challenge_id"])[0]
    # 已是第 4 次失败后的 retry：下一次失败即达 max(5)
    with connect(svc.dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_invite_delivery_outbox SET attempt_count=4 WHERE id=%s",
            (int(row0["id"]),),
        )
    svc.organization_invite_delivery_tick(limit=10)
    row = outbox_rows(svc, challenge["challenge_id"])[0]
    assert row["status"] == "cancelled"
    assert row["last_error_code"] == "FAILED:MAX_ATTEMPTS:Throttling"
    assert int(row["attempt_count"]) == 5


def _simulate_crashed_worker(svc, outbox_id: int) -> None:
    """模拟 worker 崩溃：留下 lease 已过期的 sending 行（发送结果未知）。"""
    with connect(svc.dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE organization_invite_delivery_outbox
               SET status='sending',claim_token=gen_random_uuid(),
                   lease_expires_at=NOW()-INTERVAL '1 minute'
               WHERE id=%s""",
            (int(outbox_id),),
        )


def test_unknown_after_lease_expiry_is_recovered(svc, aliyun_provider):
    sweep_outbox(svc)
    owner = make_org(svc)
    invite = make_invite(svc, owner)
    challenge = make_challenge(svc, invite)
    row0 = outbox_rows(svc, challenge["challenge_id"])[0]
    _simulate_crashed_worker(svc, int(row0["id"]))
    # R3：回收即计 attempt + 按退避档排程，同一 tick 不再立即重领补发
    first = svc.organization_invite_delivery_tick(limit=10)
    row = outbox_rows(svc, challenge["challenge_id"])[0]
    assert row["status"] == "unknown"
    assert int(row["attempt_count"]) == 1
    assert row["last_error_code"] == "UNKNOWN_AFTER_LEASE_EXPIRY"
    assert first["real_messages_sent"] == 0
    assert len(aliyun_provider.calls) == 0
    # 指数退避第一档 60s
    delta = (row["next_attempt_at"] - row["updated_at"]).total_seconds()
    assert 50 <= delta <= 70, delta
    # 退避到期后重新认领 → 确定性派生同码补发 → sent
    force_due(svc, int(row["id"]))
    second = svc.organization_invite_delivery_tick(limit=10)
    row = outbox_rows(svc, challenge["challenge_id"])[0]
    assert row["status"] == "sent"
    assert int(row["attempt_count"]) == 2
    assert second["real_messages_sent"] == 1
    assert len(aliyun_provider.calls) == 1


def test_unknown_recovery_dead_letters_at_max_attempts(svc, aliyun_provider):
    """连续 5 次 worker 崩溃（lease 过期回收）：第 5 次 dead-letter，不再被认领。

    判别性：若回收不计 attempt / 不退避 → 每次回收后下一 tick 立即重领，
    mock 计数随 tick 无限增长且永不 dead-letter，全部断言转红。
    """
    sweep_outbox(svc)
    owner = make_org(svc)
    invite = make_invite(svc, owner)
    challenge = make_challenge(svc, invite)
    outbox_id = int(outbox_rows(svc, challenge["challenge_id"])[0]["id"])
    backoffs = (60, 300, 900, 3600)
    for cycle in range(1, 5):
        _simulate_crashed_worker(svc, outbox_id)
        svc.organization_invite_delivery_tick(limit=10)
        row = outbox_rows(svc, challenge["challenge_id"])[0]
        assert row["status"] == "unknown", (cycle, row["status"])
        assert int(row["attempt_count"]) == cycle
        assert row["last_error_code"] == "UNKNOWN_AFTER_LEASE_EXPIRY"
        delta = (row["next_attempt_at"] - row["updated_at"]).total_seconds()
        assert backoffs[cycle - 1] - 10 <= delta <= backoffs[cycle - 1] + 10, (cycle, delta)
        # 退避未到期：同一 tick 与后续 tick 都不会重领外发
        svc.organization_invite_delivery_tick(limit=10)
        assert len(aliyun_provider.calls) == 0
    # 第 5 次回收：attempt_count 4→5 达 max，直接 dead-letter 终态
    _simulate_crashed_worker(svc, outbox_id)
    svc.organization_invite_delivery_tick(limit=10)
    row = outbox_rows(svc, challenge["challenge_id"])[0]
    assert row["status"] == "cancelled"
    assert int(row["attempt_count"]) == 5
    assert row["last_error_code"] == "FAILED:MAX_ATTEMPTS:UNKNOWN_AFTER_LEASE_EXPIRY"
    # dead-letter 后永不再被认领：mock 计数恒为 0（非无限补发）
    svc.organization_invite_delivery_tick(limit=10)
    svc.organization_invite_delivery_tick(limit=10)
    assert len(aliyun_provider.calls) == 0
    status = svc.get_challenge_delivery_status(
        challenge_id=int(challenge["challenge_id"]),
        token=invite["delivery_token"],
        request_id=f"w1-status-{_tag()}",
        source_ip="192.0.2.62",
    )
    assert status["delivery_state"] == "failed"
    assert status["failure_code"] == "DELIVERY_FAILED"


def test_expired_challenge_row_goes_terminal_without_sending(svc, aliyun_provider):
    """challenge TTL 过期：deliver 前拦截，零外发 + dead-letter + cancel challenge。

    判别性：删掉 _deliver_claimed_row 的 challenge TTL 闸 → tick 会真实外发
    （mock 计数 >0）且 outbox 变 sent，本测试全部断言转红。
    """
    sweep_outbox(svc)
    owner = make_org(svc)
    invite = make_invite(svc, owner)
    challenge = make_challenge(svc, invite)
    challenge_id = int(challenge["challenge_id"])
    # challenge 过期（created_at 一并后移满足 expiry CHECK）
    with connect(svc.dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE organization_invite_verification_challenges
               SET created_at=NOW()-INTERVAL '20 minutes',expires_at=NOW()-INTERVAL '1 minute'
               WHERE id=%s""",
            (challenge_id,),
        )
    svc.organization_invite_delivery_tick(limit=10)
    # 零外发：用户不会收到一个必定 410 的码
    assert len(aliyun_provider.calls) == 0
    row = outbox_rows(svc, challenge_id)[0]
    assert row["status"] == "cancelled"
    assert row["last_error_code"] == "FAILED:CHALLENGE_EXPIRED"
    # challenge 同步关闭（复用既有 cancel 语义）
    with connect(svc.dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status FROM organization_invite_verification_challenges WHERE id=%s",
            (challenge_id,),
        )
        assert cursor.fetchone()["status"] == "cancelled"
    # 终态后不再被认领重发
    svc.organization_invite_delivery_tick(limit=10)
    assert len(aliyun_provider.calls) == 0


def test_disabled_provider_never_fakes_success(svc, monkeypatch, patched_sms_client):
    monkeypatch.setenv("ORGANIZATION_INVITE_DELIVERY_PROVIDER", "disabled")
    sweep_outbox(svc)
    owner = make_org(svc)
    invite = make_invite(svc, owner)
    # challenge 创建闸直接 503
    error = expect_error(svc, "ORG_INVITE_DELIVERY_NOT_CONFIGURED", lambda: make_challenge(svc, invite))
    assert error.http_status == 503

    # 存量 pending 行：disabled tick 是 no-op，绝不假 sent
    with connect(svc.dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM organization_invites WHERE id=%s",
            (invite["invite"]["id"],),
        )
        invite_row = dict(cursor.fetchone())
        cursor.execute(
            """INSERT INTO organization_invite_delivery_outbox(
                 invite_id,challenge_id,event_kind,target_kind,target_hmac,
                 delivery_ciphertext,payload_ciphertext,encryption_key_version,request_id
               ) VALUES (%s,NULL,'invite_link','phone',%s,%s,%s,%s,%s) RETURNING id""",
            (
                invite_row["id"], invite_row["target_hmac"],
                invite_row["delivery_ciphertext_or_reference"],
                invite_row["delivery_ciphertext_or_reference"],
                "v1", f"w1-disabled-row-{_tag()}",
            ),
        )
        outbox_id = int(cursor.fetchone()["id"])
    result = svc.organization_invite_delivery_tick(limit=10)
    assert result["real_messages_sent"] == 0
    assert patched_sms_client.calls == []
    with connect(svc.dsn) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM organization_invite_delivery_outbox WHERE id=%s", (outbox_id,))
        assert cursor.fetchone()["status"] == "pending"

    # 未知 provider 值同样拒绝
    monkeypatch.setenv("ORGANIZATION_INVITE_DELIVERY_PROVIDER", "some_random_provider")
    expect_error(svc, "ORG_INVITE_DELIVERY_NOT_CONFIGURED", lambda: make_challenge(svc, invite))


# ========== 7. 邮箱邀请显式不可用 ==========


def test_email_invite_explicitly_unavailable_with_real_provider(svc, aliyun_provider):
    sweep_outbox(svc)
    owner = make_org(svc)
    invite = make_invite(svc, owner, target_kind="email", target=f"w1-{_tag()}@example.test")
    error = expect_error(svc, "ORG_INVITE_EMAIL_UNAVAILABLE", lambda: make_challenge(svc, invite))
    assert error.http_status == 503
    assert "邮箱邀请暂不可用" in error.message
    # 绝不产生 outbox 行、绝不发送
    with connect(svc.dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) AS c FROM organization_invite_delivery_outbox WHERE invite_id=%s AND event_kind='verification_code'",
            (invite["invite"]["id"],),
        )
        assert int(cursor.fetchone()["c"]) == 0
    assert aliyun_provider.calls == []


def test_email_challenge_outbox_row_goes_terminal_not_sent(svc, aliyun_provider, monkeypatch):
    """存量 email outbox 行（如历史 local_capture 产生）：终态 EMAIL_UNAVAILABLE。"""
    sweep_outbox(svc)
    owner = make_org(svc)
    # local_capture 下允许 email challenge（既有测试通道），先切回 local 建行
    monkeypatch.setenv("ORGANIZATION_INVITE_DELIVERY_PROVIDER", "local_capture")
    invite = make_invite(svc, owner, target_kind="email", target=f"w1-{_tag()}@example.test")
    challenge = make_challenge(svc, invite)
    monkeypatch.setenv("ORGANIZATION_INVITE_DELIVERY_PROVIDER", "aliyun_sms")
    svc.organization_invite_delivery_tick(limit=10)
    row = outbox_rows(svc, challenge["challenge_id"])[0]
    assert row["status"] == "cancelled"
    assert row["last_error_code"] == "FAILED:EMAIL_UNAVAILABLE"
    assert aliyun_provider.calls == []


# ========== 8. 未 sent 不可 verify（送达证据闸） ==========


def test_verify_requires_sent_evidence(svc, aliyun_provider):
    sweep_outbox(svc)
    owner = make_org(svc)
    invite = make_invite(svc, owner)
    challenge = make_challenge(svc, invite)
    code = derive_code(svc, challenge["challenge_id"])

    # outbox 仍 pending：即使码正确也拒绝核验
    error = expect_error(svc, "ORG_INVITE_CODE_NOT_DELIVERED", lambda: do_verify(svc, invite, challenge, code))
    assert error.retryable is True

    # worker 真实送达后放行
    svc.organization_invite_delivery_tick(limit=10)
    verified = do_verify(svc, invite, challenge, code)
    assert verified["status"] == "verified" and verified["verification_receipt"]

    # 全链：开户 + JWT
    onboarded = do_onboard(svc, invite, challenge, verified)
    assert onboarded["auto_login"] is True and onboarded["token"]


def test_resend_supersedes_previous_challenge(svc, aliyun_provider):
    sweep_outbox(svc)
    owner = make_org(svc)
    invite = make_invite(svc, owner)
    first = make_challenge(svc, invite)
    second = make_challenge(svc, invite)
    assert int(second["challenge_id"]) != int(first["challenge_id"])
    with connect(svc.dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status FROM organization_invite_verification_challenges WHERE id=%s",
            (int(first["challenge_id"]),),
        )
        assert cursor.fetchone()["status"] == "cancelled"
    first_outbox = outbox_rows(svc, first["challenge_id"])[0]
    assert first_outbox["status"] == "cancelled"
    assert first_outbox["last_error_code"] == "CHALLENGE_SUPERSEDED"
    # 旧码不再可用；只有最新 challenge 可送达可核验
    svc.organization_invite_delivery_tick(limit=10)
    old_code = derive_code(svc, first["challenge_id"])
    expect_error(svc, "ORG_INVITE_CHALLENGE_EXPIRED", lambda: do_verify(svc, invite, first, old_code))
    new_code = derive_code(svc, second["challenge_id"])
    verified = do_verify(svc, invite, second, new_code)
    assert verified["status"] == "verified"


# ========== 8.5 送达轮询独立限流桶 + 公开面失败码归类 ==========


def test_delivery_status_polling_has_own_rate_bucket(svc):
    """连续 12 次送达轮询不 429；轮询不消耗 verify 桶（种子：verify target=20/h）。

    判别性：若 delivery-status 仍与 verify 共桶 → 12 次轮询 + 1 次建 challenge
    共占 13 格，第 8 次 verify 即 429 → 19 次业务失败循环断言转红。
    若新桶未在种子/发布配置注册 → 首次轮询即 ORG_RATE_POLICY_MISSING 转红。
    """
    sweep_outbox(svc)
    owner = make_org(svc)
    invite = make_invite(svc, owner)
    challenge = make_challenge(svc, invite)  # verify 桶计 1（配额 20）
    # 连续 12 次轮询：独立放量桶（60/h），不 429、不消耗 verify 桶
    for _ in range(12):
        status = svc.get_challenge_delivery_status(
            challenge_id=int(challenge["challenge_id"]),
            token=invite["delivery_token"],
            request_id=f"w1-status-{_tag()}",
            source_ip="192.0.2.60",
        )
        assert status["delivery_state"] in {"queued", "sending", "sent"}
        assert status["failure_code"] is None
    wrong = "000000" if challenge["test_code"] != "000000" else "999999"
    # verify 桶已计 1，还可再过 19 次（业务层失败但非限流）
    business_codes = set()
    for _ in range(19):
        with pytest.raises(svc.OrganizationError) as captured:
            do_verify(svc, invite, challenge, wrong)
        assert captured.value.code != "ORG_RATE_LIMITED", "verify 桶被轮询提前耗尽"
        business_codes.add(captured.value.code)
    assert business_codes <= {"ORG_INVITE_CODE_MISMATCH", "ORG_INVITE_CODE_LOCKED", "ORG_INVITE_CHALLENGE_EXPIRED"}
    assert "ORG_INVITE_CODE_MISMATCH" in business_codes  # 确实走到了业务层而非提前 429
    # 第 21 次 invite.verify 动作（1 建 challenge + 19 verify 之后）按原桶 429
    expect_error(svc, "ORG_RATE_LIMITED", lambda: do_verify(svc, invite, challenge, wrong))


def test_public_delivery_status_never_leaks_provider_error_code(svc, aliyun_provider):
    """公开轮询面只返归类码：平台短信余额/限流主体等 isv.* 原文绝不外泄。

    判别性：若 get_challenge_delivery_status 回传 _derive_outbox_state 原文
    → failure_code 带 isv. 前缀/AMOUNT_NOT_ENOUGH → 本测试全部归类断言转红。
    """
    import json as _json

    aliyun_provider._behaviors.extend([
        ("isv.AMOUNT_NOT_ENOUGH", "余额不足"),
        ("Throttling", "触发流控"),
    ])
    sweep_outbox(svc)
    owner = make_org(svc)
    # 余额不足：确定性错误首次即 dead-letter
    invite = make_invite(svc, owner)
    challenge = make_challenge(svc, invite)
    svc.organization_invite_delivery_tick(limit=10)
    row = outbox_rows(svc, challenge["challenge_id"])[0]
    assert row["status"] == "cancelled"
    # 内部证据保留原文（脱敏日志/DB 排查用）
    assert row["last_error_code"] == "FAILED:isv.AMOUNT_NOT_ENOUGH"
    status = svc.get_challenge_delivery_status(
        challenge_id=int(challenge["challenge_id"]),
        token=invite["delivery_token"],
        request_id=f"w1-status-{_tag()}",
        source_ip="192.0.2.61",
    )
    assert status["delivery_state"] == "failed"
    assert status["failure_code"] == "DELIVERY_FAILED"
    assert "isv." not in _json.dumps(status, default=str)
    assert "AMOUNT_NOT_ENOUGH" not in _json.dumps(status, default=str)
    # 限流耗尽（MAX_ATTEMPTS dead-letter）→ 公开面 RATE_LIMITED，同样无原文
    invite2 = make_invite(svc, owner)
    challenge2 = make_challenge(svc, invite2)
    row2 = outbox_rows(svc, challenge2["challenge_id"])[0]
    with connect(svc.dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_invite_delivery_outbox SET attempt_count=4 WHERE id=%s",
            (int(row2["id"]),),
        )
    svc.organization_invite_delivery_tick(limit=10)
    row2 = outbox_rows(svc, challenge2["challenge_id"])[0]
    assert row2["last_error_code"] == "FAILED:MAX_ATTEMPTS:Throttling"
    status2 = svc.get_challenge_delivery_status(
        challenge_id=int(challenge2["challenge_id"]),
        token=invite2["delivery_token"],
        request_id=f"w1-status-{_tag()}",
        source_ip="192.0.2.61",
    )
    assert status2["delivery_state"] == "failed"
    assert status2["failure_code"] == "RATE_LIMITED"
    assert "isv." not in _json.dumps(status2, default=str)


# ========== 9. 新员工无任何商业资产 ==========


def test_new_operator_has_no_commercial_assets(svc):
    sweep_outbox(svc)
    owner = make_org(svc)
    invite = make_invite(svc, owner)
    challenge = make_challenge(svc, invite)
    verified = do_verify(svc, invite, challenge, challenge["test_code"])
    onboarded = do_onboard(svc, invite, challenge, verified)
    user_id = int(onboarded["user_id"])
    checks = {
        "user_roles": "SELECT COUNT(*) FROM user_roles WHERE user_id=%s",
        "user_wallets": "SELECT COUNT(*) FROM user_wallets WHERE user_id=%s",
        "point_transactions": "SELECT COUNT(*) FROM point_transactions WHERE user_id=%s",
        "brands": "SELECT COUNT(*) FROM brands WHERE owner_user_id=%s",
        "user_clients": "SELECT COUNT(*) FROM user_clients WHERE user_id=%s",
        "customer_agent_bindings": "SELECT COUNT(*) FROM customer_agent_bindings WHERE customer_user_id=%s OR agent_user_id=%s",
        "referral_links": "SELECT COUNT(*) FROM referral_links WHERE referrer_id=%s OR referred_id=%s",
    }
    executed: list[str] = []
    skipped: list[str] = []
    for name, query in checks.items():
        params = (user_id, user_id) if query.count("%s") == 2 else (user_id,)
        if name == "referral_links":
            # 显式豁免：referral_links 表可能不存在于本 fixture schema。
            # 存在时必须查证为 0；不存在时记录跳过标记。其余 6 张表由
            # fixture 保障存在，任何查询异常（表缺失/列漂移）一律让测试
            # fail，绝不静默 continue。
            table_exists = int(
                sql_value(
                    svc.dsn,
                    "SELECT COUNT(*) FROM information_schema.tables"
                    " WHERE table_schema=current_schema() AND table_name='referral_links'",
                )
            )
            if not table_exists:
                skipped.append(name)
                print("[commercial-assets] SKIP referral_links: table absent in fixture schema")
                continue
        count = int(sql_value(svc.dsn, query, params))
        executed.append(name)
        print(f"[commercial-assets] CHECK {name}: count={count}")
        assert count == 0, f"{name} 不应有记录"
    # 实际执行标记：除显式豁免项外，每一项都必须真实执行过查询
    assert set(executed) >= set(checks) - {"referral_links"}, f"未执行的检查: {set(checks) - set(executed) - set(skipped)}"
    assert set(skipped) <= {"referral_links"}
    assert int(sql_value(svc.dsn, "SELECT COUNT(*) FROM agreement_signatures WHERE user_id=%s", (user_id,))) == 2


# ========== 10. 登录短信回归 + 原语边界 ==========


def test_login_sms_code_still_generates_and_stores(svc, patched_sms_client, monkeypatch):
    """原语重构后登录路径零行为变化：send_sms_code 自生成码 + 写 sms_codes。"""
    monkeypatch.setenv("SMS_TEMPLATE_CODE", "SMS_LOGIN_MAIN")
    import auth.sms_service as sms

    phone = _phone()
    result = sms.send_sms_code(phone, purpose="login")
    assert result == {"success": True}
    assert len(patched_sms_client.calls) == 1
    call = patched_sms_client.calls[0]
    assert call["template_code"] == "SMS_LOGIN_MAIN"
    assert call["phone_numbers"] == phone

    # 写 sms_codes（明文 SSOT 登录用），且可用 verify_sms_code 核销
    with connect(svc.dsn) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT code,purpose,attempts FROM sms_codes WHERE phone=%s", (phone,))
        row = cursor.fetchone()
    assert row is not None and row["purpose"] == "login"
    import json as _json
    sent_code = _json.loads(call["template_param"])["code"]
    assert row["code"] == sent_code and len(sent_code) == 6
    assert sms.verify_sms_code(phone, sent_code, "login") == {"success": True}


def test_login_sms_fallback_template_still_stores_once(svc, patched_sms_client, monkeypatch):
    """主模板失败走备用模板：成功后仍写 sms_codes（行为锁定）。"""
    monkeypatch.setenv("SMS_TEMPLATE_CODE", "SMS_LOGIN_MAIN")
    monkeypatch.setenv("SMS_TEMPLATE_FALLBACK", "SMS_LOGIN_FALLBACK")
    patched_sms_client._behaviors.append(("isv.TEMPLATE_NOT_PASS", "模板未过审"))
    import auth.sms_service as sms

    phone = _phone()
    assert sms.send_sms_code(phone, purpose="login") == {"success": True}
    assert [c["template_code"] for c in patched_sms_client.calls] == ["SMS_LOGIN_MAIN", "SMS_LOGIN_FALLBACK"]
    with connect(svc.dsn) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS c FROM sms_codes WHERE phone=%s", (phone,))
        assert int(cursor.fetchone()["c"]) == 1


def test_primitive_never_generates_never_stores(svc, patched_sms_client, monkeypatch):
    """send_verification_code：调用方供码、绝不写 sms_codes。"""
    monkeypatch.setenv("SMS_TEMPLATE_INVITE", "SMS_INVITE_TEST")
    import auth.sms_service as sms

    phone = _phone()
    result = sms.send_verification_code(
        phone=phone,
        caller_supplied_code="642013",
        purpose="organization_invite",
        request_id="w1-primitive-boundary",
    )
    assert result["success"] is True
    assert result["receipt"]
    import json as _json
    sent = _json.loads(patched_sms_client.calls[0]["template_param"])
    assert sent["code"] == "642013"  # 完全是调用方供码
    with connect(svc.dsn) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS c FROM sms_codes WHERE phone=%s", (phone,))
        assert int(cursor.fetchone()["c"]) == 0

    # 模板未配置 → 确定性失败，不发送
    monkeypatch.delenv("SMS_TEMPLATE_INVITE")
    failed = sms.send_verification_code(
        phone=phone,
        caller_supplied_code="642013",
        purpose="organization_invite",
        request_id="w1-primitive-no-template",
    )
    assert failed["success"] is False
    assert failed["provider_code"] == "SMS_TEMPLATE_NOT_CONFIGURED"
    assert failed["retryable"] is False


def test_primitive_purpose_whitelist_fail_closed(svc, patched_sms_client, monkeypatch):
    """purpose 白名单：未登记 purpose / 越界模板 / 越界参数一律拒绝且不发送。

    判别性：删掉 _enforce_approved_purpose 调用 → 前三个 raises 断言转红
    （未登记 purpose 会真实外发，calls 非空）；登录路径不走白名单（由
    test_login_sms_code_still_generates_and_stores 锁定零变化）。
    """
    import json as _json

    monkeypatch.setenv("SMS_TEMPLATE_INVITE", "SMS_INVITE_TEST")
    import auth.sms_service as sms

    phone = _phone()
    # 未登记 purpose → 拒绝（ValueError 语义：调用方契约错误，非 provider 失败）
    with pytest.raises(sms.SmsPurposeNotApproved):
        sms.send_verification_code(
            phone=phone,
            caller_supplied_code="642013",
            purpose="marketing_blast",
            request_id="w1-purpose-unapproved",
        )
    assert isinstance(sms.SmsPurposeNotApproved("x"), ValueError)
    # 登记 purpose 但不允许调用方覆盖模板 → 拒绝
    with pytest.raises(sms.SmsPurposeNotApproved):
        sms.send_verification_code(
            phone=phone,
            caller_supplied_code="642013",
            purpose="organization_invite",
            request_id="w1-purpose-template-override",
            template_code="SMS_ARBITRARY",
        )
    # 模板参数越界（白名单外 key）→ 拒绝
    with pytest.raises(sms.SmsPurposeNotApproved):
        sms.send_verification_code(
            phone=phone,
            caller_supplied_code="642013",
            purpose="organization_invite",
            request_id="w1-purpose-param-injection",
            template_param=_json.dumps({"code": "642013", "promo": "x"}, ensure_ascii=False),
        )
    # invite_link 必须显式给模板 → 缺失即拒绝
    with pytest.raises(sms.SmsPurposeNotApproved):
        sms.send_verification_code(
            phone=phone,
            caller_supplied_code="https://app.example.test/i#token=x",
            purpose="organization_invite_link",
            request_id="w1-purpose-link-no-template",
        )
    # 全部被拒于发送之前：零 provider 流量
    assert patched_sms_client.calls == []
    # 登记 purpose 正常送达（组织邀请两个调用点语义锁定）
    ok = sms.send_verification_code(
        phone=phone,
        caller_supplied_code="642013",
        purpose="organization_invite",
        request_id="w1-purpose-ok",
    )
    assert ok["success"] is True
    ok_link = sms.send_verification_code(
        phone=phone,
        caller_supplied_code="https://app.example.test/i#token=x",
        purpose="organization_invite_link",
        request_id="w1-purpose-link-ok",
        template_code="SMS_LINK_TEST",
        template_param=_json.dumps({"url": "https://app.example.test/i#token=x"}, ensure_ascii=False),
    )
    assert ok_link["success"] is True
    assert [c["template_code"] for c in patched_sms_client.calls] == ["SMS_INVITE_TEST", "SMS_LINK_TEST"]


def test_provider_error_classification():
    from auth.sms_service import classify_sms_provider_error

    for code in (
        "isv.MOBILE_NUMBER_ILLEGAL",
        "isv.INVALID_TEMPLATE_CODE",
        "isv.TEMPLATE_MISSING_PARAMETERS",
        "isv.SIGN_NOT_PASS",
        "isv.AMOUNT_NOT_ENOUGH",
        "Forbidden.RAM",
    ):
        assert classify_sms_provider_error(code) == "terminal", code
    for code in ("Throttling", "Throttling.User", "isv.BUSINESS_LIMIT_CONTROL", "ServiceUnavailable", "SMS_SEND_EXCEPTION"):
        assert classify_sms_provider_error(code) == "retryable", code
    # 未知错误码默认可重试（max_attempts + dead-letter 兜底）
    assert classify_sms_provider_error("isv.SOMETHING_NEW") == "retryable"


# ========== 11. 老板侧送达状态数据源（W2 挂载用） ==========


def test_owner_delivery_states_feed(svc, aliyun_provider):
    sweep_outbox(svc)
    owner = make_org(svc)
    invite = make_invite(svc, owner)
    challenge = make_challenge(svc, invite)
    svc.organization_invite_delivery_tick(limit=10)
    identity = svc.resolve_identity(owner, request_id=f"w1-resolve-{_tag()}")
    items = svc.list_invite_delivery_states(identity, invite_ids=[int(invite["invite"]["id"])])
    assert len(items) == 1
    deliveries = items[0]["deliveries"]
    assert deliveries["verification_code"]["state"] == "sent"
    # invite_link 未配置模板/前缀 → dead-letter failed，人话 code 可查
    assert deliveries["invite_link"]["state"] == "failed"
    assert deliveries["invite_link"]["failure_code"] == "INVITE_LINK_CONFIG_MISSING"


def test_invite_link_sms_with_full_config(svc, aliyun_provider, monkeypatch):
    monkeypatch.setenv("SMS_TEMPLATE_INVITE_LINK", "SMS_LINK_TEST")
    monkeypatch.setenv("ORGANIZATION_INVITE_LINK_BASE_URL", "https://app.example.test")
    sweep_outbox(svc)
    owner = make_org(svc)
    invite = make_invite(svc, owner)
    svc.organization_invite_delivery_tick(limit=10)
    link_calls = [c for c in aliyun_provider.calls if c["template_code"] == "SMS_LINK_TEST"]
    assert len(link_calls) == 1
    import json as _json
    param = _json.loads(link_calls[0]["template_param"])
    assert param["url"].startswith("https://app.example.test/organization/invite#token=")


# ========== [WP6] 用户名式邀请:owner 设登录名 + 被邀请人凭 token 自设密码开户 ==========


def do_credential_onboard(svc, invite: dict, *, request_id: str | None = None) -> dict:
    return svc.onboard_operator_via_credential(
        token=invite["delivery_token"],
        password=PASSWORD,
        display_name="用户名式员工",
        request_id=request_id or f"w6-cred-{_tag()}",
        terms_accepted=True,
        privacy_accepted=True,
        terms_version=svc.USER_TERMS_VERSION,
        privacy_version=svc.PRIVACY_VERSION,
        source_ip="192.0.2.6",
        user_agent="w6-pytest",
    )


def test_username_invite_credential_onboard_succeeds_without_challenge(svc):
    # ALLOWED:owner 以用户名方式邀请 → 被邀请人凭 token 直接自设密码开户,无需短信验证码。
    sweep_outbox(svc)
    owner = make_org(svc)
    uname = f"op{_tag()}"
    invite = make_invite(svc, owner, target_kind="username", target=uname)
    onboarded = do_credential_onboard(svc, invite)
    assert onboarded["success"] is True and onboarded["auto_login"] is True and onboarded["token"]
    assert onboarded["login_username"] == uname.casefold()  # 登录名 = owner 设定用户名(归一小写)
    assert onboarded["status"] == "active"
    assert onboarded["account_origin"] == "organization_invite"
    assert int(onboarded["organization_id"]) > 0  # 绑定到 owner 组织(被邀请人不能改)


def test_username_credential_onboard_is_one_time(svc):
    # token 一次性:换 request_id 再来 → 邀请已消费 → 幂等冲突(不产生第二个账号)。
    sweep_outbox(svc)
    owner = make_org(svc)
    invite = make_invite(svc, owner, target_kind="username", target=f"op{_tag()}")
    do_credential_onboard(svc, invite, request_id=f"w6-a-{_tag()}")
    expect_error(svc, "ORG_INVITE_IDEMPOTENCY_CONFLICT",
                 lambda: do_credential_onboard(svc, invite, request_id=f"w6-b-{_tag()}"))


def test_username_credential_same_request_id_replays(svc):
    # 同 request_id 幂等重放 → 返回同一账号 + 会话,不重复开户。
    sweep_outbox(svc)
    owner = make_org(svc)
    invite = make_invite(svc, owner, target_kind="username", target=f"op{_tag()}")
    rid = f"w6-r-{_tag()}"
    first = do_credential_onboard(svc, invite, request_id=rid)
    replay = do_credential_onboard(svc, invite, request_id=rid)
    assert replay["user_id"] == first["user_id"] and replay["replayed"] is True


def test_phone_invite_rejects_credential_bypass(svc):
    # DENIED 反向判别:短信/邮箱邀请绝不能用 credential 路径绕过验证码(防降级二次验证)。
    sweep_outbox(svc)
    owner = make_org(svc)
    invite = make_invite(svc, owner, target_kind="phone")
    expect_error(svc, "ORG_INVITE_VERIFICATION_REQUIRED",
                 lambda: do_credential_onboard(svc, invite))


def test_username_invite_rejects_sms_challenge(svc):
    # DENIED 反向判别:用户名式邀请没有联系方式,不能创建短信 challenge(fail-closed)。
    sweep_outbox(svc)
    owner = make_org(svc)
    invite = make_invite(svc, owner, target_kind="username", target=f"op{_tag()}")
    expect_error(svc, "ORG_INVITE_CREDENTIAL_MODE",
                 lambda: make_challenge(svc, invite))
