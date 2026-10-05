"""[F-3 · Owner 裁决]操作员开户密码最短 6 位(原 12 位属过度设防 · §1.6 可用性)。

锁定:
- 唯一权威常量 = services.organization_onboarding.MIN_OPERATOR_PASSWORD_LENGTH = 6;
- 两个开户请求模型都用该常量(6 位过、5 位被 Pydantic 拒);
- 服务层校验与之同源(不会出现"模型放行、服务层拒");
- **前端阈值与后端一致**——防"前端放行后端 422"的死胡同(Owner 点名的判别)。
"""
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from api.organization_api import (
    PublicInviteCredentialOnboardRequest,
    PublicInviteOnboardRequest,
)
from services.organization_onboarding import MIN_OPERATOR_PASSWORD_LENGTH

_ROOT = Path(__file__).parent.parent


def _base(**over):
    payload = {
        "token": "t" * 40,
        "request_id": "req-onboard-0001",
        "display_name": "员工甲",
        "terms_accepted": True,
        "privacy_accepted": True,
        "terms_version": "v2",
        "privacy_version": "v2",
    }
    payload.update(over)
    return payload


def test_authoritative_minimum_is_six():
    assert MIN_OPERATOR_PASSWORD_LENGTH == 6


def test_credential_onboard_accepts_six_and_rejects_five():
    ok = PublicInviteCredentialOnboardRequest(**_base(password="a" * 6))
    assert len(ok.password) == 6
    with pytest.raises(ValidationError):
        PublicInviteCredentialOnboardRequest(**_base(password="a" * 5))


def test_challenge_onboard_accepts_six_and_rejects_five():
    extra = {"challenge_id": 1, "verification_receipt": "r" * 24}
    ok = PublicInviteOnboardRequest(**_base(password="a" * 6, **extra))
    assert len(ok.password) == 6
    with pytest.raises(ValidationError):
        PublicInviteOnboardRequest(**_base(password="a" * 5, **extra))


def test_upper_bound_128_is_unchanged():
    PublicInviteCredentialOnboardRequest(**_base(password="a" * 128))
    with pytest.raises(ValidationError):
        PublicInviteCredentialOnboardRequest(**_base(password="a" * 129))


def test_service_layer_uses_the_same_constant_not_a_hardcoded_number():
    src = (_ROOT / "services" / "organization_onboarding.py").read_text(encoding="utf-8")
    assert "len(str(password or \"\")) < MIN_OPERATOR_PASSWORD_LENGTH" in src
    assert "< 12 or len(str(password)) > 128" not in src   # 旧硬编码不得残留


def test_frontend_threshold_matches_backend_no_dead_end():
    """前端放行、后端 422 = 死胡同。两端必须同值(Owner 点名判别)。"""
    tsx = (_ROOT / "frontend" / "src" / "pages" / "Organization"
           / "OrganizationInviteAccept.tsx").read_text(encoding="utf-8")
    m = re.search(r"const MIN_OPERATOR_PASSWORD_LENGTH\s*=\s*(\d+)", tsx)
    assert m, "前端未定义阈值常量"
    assert int(m.group(1)) == MIN_OPERATOR_PASSWORD_LENGTH
    # 四处硬编码 12 必须全部消失(minLength / disabled 判断 / 文案)
    assert "minLength={12}" not in tsx
    assert "newPassword.length < 12" not in tsx
    assert "至少 12 位" not in tsx
