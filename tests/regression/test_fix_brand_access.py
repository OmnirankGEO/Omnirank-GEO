"""判别性回归锁 · auth/brand_access.py NULL-brand fail-open IDOR 修复

覆盖:
- GEO-R1-CAN-139: require_diagnosis_access 默认 allow_null 从 True → False(fail-closed)
- GEO-R1-CAN-141: require_report_access 从硬编码 allow_null=True → 新增 allow_null 参数(默认 False)

主形态 = source-inspection 判别锁 + 纯行为单测(mock Request,不依赖 DB / 不 import server.py)。
回退修复 → 断言失败。
"""
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import auth.brand_access as ba  # noqa: E402


SRC = (ROOT / "auth" / "brand_access.py").read_text(encoding="utf-8")


# ---------------- source-inspection 判别锁 ----------------

def test_diagnosis_default_is_fail_closed_signature():
    """CAN-139: require_diagnosis_access 的 allow_null 默认必须为 False。"""
    sig = inspect.signature(ba.require_diagnosis_access)
    assert sig.parameters["allow_null"].default is False, (
        "require_diagnosis_access 默认应 fail-closed(allow_null=False)"
    )


def test_report_access_has_allow_null_param_default_false():
    """CAN-141: require_report_access 必须新增 allow_null 参数且默认 False。"""
    sig = inspect.signature(ba.require_report_access)
    assert "allow_null" in sig.parameters, "require_report_access 应新增 allow_null 参数"
    assert sig.parameters["allow_null"].default is False, (
        "require_report_access 默认应 fail-closed(allow_null=False)"
    )


def test_report_access_no_longer_hardcodes_allow_null_true():
    """CAN-141: 不得再硬编码 require_brand_access(..., allow_null=True)。"""
    # require_report_access 函数体内应传参 allow_null=allow_null,而非常量 True
    src_report = inspect.getsource(ba.require_report_access)
    assert "allow_null=allow_null" in src_report, (
        "require_report_access 应把 allow_null 透传给 require_brand_access"
    )
    # 精确匹配旧的硬编码调用表达式(避开 docstring 里的 allow_null=True 文字说明)
    assert 'report.get("brand_id"), allow_null=True' not in src_report, (
        "require_report_access 不得再硬编码 require_brand_access(..., allow_null=True)"
    )


def test_fix_markers_present():
    """修复标记注释存在,防止悄悄回退。"""
    assert "[GEO-R1-CAN-139]" in SRC
    assert "[GEO-R1-CAN-141]" in SRC


# ---------------- 纯行为单测(mock Request,fail-closed 验证) ----------------

class _FakeState:
    def __init__(self, user):
        self.user = user


class _FakeRequest:
    def __init__(self, user):
        self.state = _FakeState(user)


def _run_null_brand(func_name, user):
    """直接调 require_brand_access(brand_id=None) 模拟 NULL-brand 归属校验。

    require_diagnosis_access / require_report_access 在 brand_id 为 None 时
    最终调用 require_brand_access(request, None, allow_null=<default>)。
    这里用默认参数直接验证核心分支行为(不触 DB)。
    """
    from fastapi import HTTPException
    req = _FakeRequest(user)
    # 默认 allow_null=False → 非 admin 必须 403
    try:
        ba.require_brand_access(req, None)  # allow_null 默认 False
    except HTTPException as e:
        return e.status_code
    return 200


def test_null_brand_denied_for_non_admin_behavior():
    """非管理员访问 NULL-brand(allow_null 默认 False)→ 403,而非 fail-open 200。"""
    code = _run_null_brand("require_brand_access", {"user_id": 999, "username": "attacker_B", "is_admin": False})
    assert code == 403, "NULL-brand 对非管理员必须 fail-closed 403"


def test_null_brand_allowed_for_admin_behavior():
    """管理员访问 NULL-brand → 放行(不抛)。"""
    from fastapi import HTTPException
    req = _FakeRequest({"user_id": 1, "username": "admin", "is_admin": True})
    try:
        ba.require_brand_access(req, None)
    except HTTPException:
        raise AssertionError("admin 访问 NULL-brand 不应被拒绝")


def test_explicit_allow_null_true_still_opt_in():
    """显式 allow_null=True 仍可放行(向后兼容 · 需显式 opt-in)。"""
    from fastapi import HTTPException
    req = _FakeRequest({"user_id": 999, "username": "u", "is_admin": False})
    try:
        ba.require_brand_access(req, None, allow_null=True)
    except HTTPException:
        raise AssertionError("显式 allow_null=True 应放行 NULL-brand")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
