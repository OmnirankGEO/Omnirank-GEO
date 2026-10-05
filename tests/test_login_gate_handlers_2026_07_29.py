"""工单 2026-07-29 · 协议门禁后端 handler 行为锁

**真调 handler**(`asyncio.run` 跑真 async 端点函数),断言真实响应对象与真实抛出的
HTTPException。不做源码字符串断言 —— 本单明确不接受那种形态。

覆盖:
  - 428 detail 的六项契约(前端解析靠它;少任何一项都会让补签页到不了)
  - 新增的补签凭证自助重取端点:凭证校验、限流、**绝不签发 JWT**、已齐全时不发凭证
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _request():
    return SimpleNamespace(
        headers={"User-Agent": "pytest"},
        client=SimpleNamespace(host="127.0.0.1"),
        cookies={},
    )


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return SimpleNamespace()


@pytest.fixture
def gate(monkeypatch):
    """把门禁的两个外部依赖换成可控桩:DB 连接 + 协议齐全判定。"""
    import api.auth_api as auth_api
    import db.connection as connection
    import services.legal_agreements as legal
    import services.registration_agreement_gate_metrics as metrics

    state = {"complete": False, "recorded": []}
    monkeypatch.setattr(connection, "get_db", lambda: _Conn())
    monkeypatch.setattr(
        legal, "has_current_registration_agreements",
        lambda cur, *, user_id: state["complete"],
    )
    monkeypatch.setattr(
        metrics, "record_gate_trigger",
        lambda **kw: state["recorded"].append(kw) or True,
    )
    return SimpleNamespace(module=auth_api, state=state)


# ============================================================================
# 锁 · 428 detail 六项契约
# ============================================================================

def test_gate_raises_428_carrying_all_six_contract_fields(gate):
    """🔴 前端六项校验读的就是这六项。少一项 = 补签页到不了 = 账号锁死。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        gate.module._require_registration_agreements(user_id=901, auth_method="password")

    assert exc.value.status_code == 428
    detail = exc.value.detail
    assert isinstance(detail, dict), "detail 必须是结构化对象,前端按 code 分支"
    assert detail["code"] == "REGISTRATION_AGREEMENTS_REQUIRED"
    assert detail["requires_agreement"] is True
    assert isinstance(detail["agreement_session_token"], str) and detail["agreement_session_token"]
    assert isinstance(detail["expires_at"], int)
    assert isinstance(detail["agreements"], list) and len(detail["agreements"]) == 2
    assert isinstance(detail["message"], str) and detail["message"]


def test_gate_records_a_trigger_before_blocking(gate):
    """留痕在拦截之前 —— 否则被挡的人永远不会出现在任何计数里。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        gate.module._require_registration_agreements(user_id=902, auth_method="sms")
    assert gate.state["recorded"] == [{"user_id": 902, "auth_method": "sms"}]


def test_gate_passes_through_when_agreements_complete(gate):
    """反向锁:协议齐全就必须放行(别把门禁改成永远拦)。"""
    gate.state["complete"] = True
    assert gate.module._require_registration_agreements(user_id=903, auth_method="password") is None
    assert gate.state["recorded"] == [], "放行路径不该产生门禁计数"


# ============================================================================
# 锁 · 补签凭证自助重取端点(fail-safe 的后端半边)
# ============================================================================

def _call_session_endpoint(module, username="13800000001", password="pw"):
    req = module.AgreementSupplementSessionRequest(username=username, password=password)
    return asyncio.run(module.reissue_registration_agreement_session(req, _request()))


@pytest.fixture
def session_endpoint(gate, monkeypatch):
    module = gate.module
    monkeypatch.setattr(module, "check_rate_limit", lambda u: (True, ""))
    monkeypatch.setattr(module, "record_failed_attempt", lambda u: None)
    monkeypatch.setattr(module, "clear_attempts", lambda u: None)
    monkeypatch.setattr(
        module, "get_user_by_username",
        lambda u: {"id": 951, "is_active": True, "password_hash": "hash"},
    )
    monkeypatch.setattr(module, "verify_password", lambda raw, hashed: raw == "pw")
    return gate


def test_session_endpoint_issues_a_session_but_never_a_token(session_endpoint):
    """🔴 最关键的一条:自助重取拿到的是**协议凭证**,不是登录态。"""
    result = _call_session_endpoint(session_endpoint.module)

    assert result["success"] is True
    assert isinstance(result["agreement_session_token"], str)
    assert isinstance(result["expires_at"], int)
    assert len(result["agreements"]) == 2
    assert "token" not in result, "这个端点绝不允许签发业务 JWT"
    assert "user" not in result


def test_session_endpoint_rejects_wrong_password(session_endpoint):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _call_session_endpoint(session_endpoint.module, password="wrong")
    assert exc.value.status_code == 401


def test_session_endpoint_rejects_unknown_or_disabled_account(session_endpoint, monkeypatch):
    from fastapi import HTTPException

    module = session_endpoint.module
    monkeypatch.setattr(module, "get_user_by_username", lambda u: None)
    with pytest.raises(HTTPException) as exc:
        _call_session_endpoint(module)
    assert exc.value.status_code == 401

    monkeypatch.setattr(
        module, "get_user_by_username",
        lambda u: {"id": 951, "is_active": False, "password_hash": "hash"},
    )
    with pytest.raises(HTTPException) as exc:
        _call_session_endpoint(module)
    assert exc.value.status_code == 401


def test_session_endpoint_honours_the_same_rate_limiter_as_login(session_endpoint, monkeypatch):
    """门禁没有被放宽:限流与 /login 同一套。"""
    from fastapi import HTTPException

    module = session_endpoint.module
    monkeypatch.setattr(module, "check_rate_limit", lambda u: (False, "尝试过于频繁"))
    with pytest.raises(HTTPException) as exc:
        _call_session_endpoint(module)
    assert exc.value.status_code == 429


def test_session_endpoint_does_not_issue_a_session_when_agreements_are_current(session_endpoint):
    """协议已齐全 → 不发凭证,给明确下一步(而不是发一张没用的凭证)。"""
    session_endpoint.state["complete"] = True
    result = _call_session_endpoint(session_endpoint.module)

    assert result["success"] is False
    assert result["code"] == "AGREEMENTS_ALREADY_CURRENT"
    assert "agreement_session_token" not in result
    assert "token" not in result
