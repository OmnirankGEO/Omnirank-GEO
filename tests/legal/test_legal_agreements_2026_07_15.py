import hashlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.auth_api import RegisterRequest, validate_registration_agreements
from api.auth_api import _get_client_ip
from services import agent_agreement
from services.legal_agreements import PRIVACY_VERSION, USER_TERMS_VERSION


ROOT = Path(__file__).resolve().parents[2]


def _register_request(**overrides):
    values = {
        "phone": "15500000001",
        "password": "secure-pass",
        "terms_accepted": True,
        "privacy_accepted": True,
        "terms_version": USER_TERMS_VERSION,
        "privacy_version": PRIVACY_VERSION,
    }
    values.update(overrides)
    return RegisterRequest(**values)


def test_registration_requires_each_current_agreement_version():
    validate_registration_agreements(_register_request())
    with pytest.raises(HTTPException) as stale_terms:
        validate_registration_agreements(_register_request(terms_version="user-v1.0"))
    assert stale_terms.value.status_code == 400
    with pytest.raises(HTTPException) as missing_privacy:
        validate_registration_agreements(_register_request(privacy_accepted=False))
    assert missing_privacy.value.status_code == 400


def test_service_provider_agreement_body_hash_is_exact():
    source = (ROOT / "frontend/src/pages/Agent/AgreementPage.tsx").read_text(encoding="utf-8")
    match = re.search(r"const AGREEMENT_TEXT = `(.*?)`;", source, re.DOTALL)
    assert match is not None
    digest = hashlib.sha256(match.group(1).replace("\r\n", "\n").encode("utf-8")).hexdigest()
    assert agent_agreement.CURRENT_VERSION == "v2.4"
    assert digest == agent_agreement.CURRENT_CONTENT_HASH
    assert "AGENT_OPERATING_AGREEMENT_CONTENT_HASH" in source


def test_sms_login_never_auto_registers_without_registration_evidence():
    source = (ROOT / "api/auth_api.py").read_text(encoding="utf-8")
    block = source[source.index('@router.post("/login-sms")'):]
    assert "自动注册新用户" not in block
    assert "请先阅读协议并完成注册" in block
    assert "_require_registration_agreements" in block


def test_agreement_ip_evidence_ignores_untrusted_forwarded_for():
    spoofed = SimpleNamespace(
        headers={"X-Forwarded-For": "198.51.100.66"},
        client=SimpleNamespace(host="10.0.0.8"),
    )
    assert _get_client_ip(spoofed) == "10.0.0.8"
    proxy_stamped = SimpleNamespace(
        headers={"X-Real-IP": "203.0.113.88", "X-Forwarded-For": "198.51.100.66"},
        client=SimpleNamespace(host="10.0.0.8"),
    )
    assert _get_client_ip(proxy_stamped) == "203.0.113.88"


def test_consumer_and_business_refund_language_are_separate():
    terms = (ROOT / "frontend/src/pages/Legal/TermsPage.tsx").read_text(encoding="utf-8")
    agent = (ROOT / "frontend/src/pages/Agent/AgreementPage.tsx").read_text(encoding="utf-8")
    assert "不预扣固定百分之五费用" in terms
    assert "服务方结算余额不足不影响依法成立的客户退款" in terms
    assert "不自动撤销任何上游或其他独立订单" in terms
    assert "不超过该笔实付金额的百分之五" in agent
    assert "可证明且实际发生" in agent
    assert "不得由服务商任意拒绝" in agent


def test_checkout_records_acceptance_before_quote_or_order():
    source = (ROOT / "frontend/src/pages/Customer/BuyCredit.tsx").read_text(encoding="utf-8")
    accept_at = source.index("await customerApi.acceptLegalAgreements")
    order_at = source.index("const r = sku.pricing_mode", accept_at)
    assert accept_at < order_at
    assert "terms_acceptance_id: termsAcceptanceId" in source
    assert "terms_version: USER_TERMS_VERSION" in source


def test_subscription_signing_uses_canonical_server_evidence_not_placeholder_storage():
    source = (ROOT / "frontend/src/pages/Subscription/SubscriptionSigningPage.tsx").read_text(
        encoding="utf-8"
    )
    assert "/api/auth/legal-agreements/accept" in source
    assert "USER_TERMS_VERSION" in source
    assert "surface: 'subscription-autorenew'" in source
    assert "localStorage" not in source
    assert "占位文本" not in source
