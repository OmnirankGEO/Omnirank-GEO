# -*- coding: utf-8 -*-
"""P3 策略批(audit · 2026-06-10, 2026-07-16 契约更新):
#511 login-sms 不得自动注册;注册端独立执行默认开启的纯邀请制门禁。
(#500 自助注册默认权限:老板拍不动 —— 保"普通用户全量工具",越权已被端点 RBAC 纵深覆盖。)
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTH = (ROOT / "api" / "auth_api.py").read_text(encoding="utf-8")


def _login_sms_block() -> str:
    i = AUTH.find("async def login_sms(")
    assert i >= 0, "未找到 login_sms"
    cands = [AUTH.find("\n@router.", i + 10), AUTH.find("\nasync def ", i + 10)]
    ends = [x for x in cands if x > 0]
    return AUTH[i:(min(ends) if ends else len(AUTH))]


def _register_block() -> str:
    i = AUTH.find("async def register(")
    assert i >= 0, "未找到 register"
    cands = [AUTH.find("\n@router.", i + 10), AUTH.find("\nasync def ", i + 10)]
    ends = [x for x in cands if x > 0]
    return AUTH[i:(min(ends) if ends else len(AUTH))]


def test_511_sms_login_never_autocreates():
    """短信登录只允许既有账户,未注册手机号必须回到注册流程。"""
    blk = _login_sms_block()
    i_notuser = blk.find("if not user:")
    assert i_notuser > 0, "缺未注册账户拒绝分支"
    assert "create_user(" not in blk, "短信登录不得创建账户或绕过注册协议/邀请码门禁"
    assert "该手机号未注册" in blk, "未注册手机号须引导走注册"


def test_511_registration_invite_gate_precedes_user_creation():
    """注册默认要求邀请码,且门禁必须先于建号和注册赠送。"""
    blk = _register_block()
    i_gate = blk.find('getenv("SIGNUP_REQUIRE_REFERRAL", "true")')
    i_validate = blk.find("if _require_referral:", i_gate)
    i_create = blk.find("create_user(", i_validate)
    assert i_gate > 0, "注册须读取默认开启的 SIGNUP_REQUIRE_REFERRAL"
    assert 0 < i_gate < i_validate < i_create, "邀请码校验必须先于 create_user"
    assert "注册需要有效的推荐码" in blk
    assert "推荐码无效或已失效" in blk
