# -*- coding: utf-8 -*-
"""[D10 · Owner 2026-07-26 授权保护文件单点例外 · SSOT v2.2] auth/middleware.py 演示只读放行守卫。

staging 门② 实测:品牌隔离安全网在 demo 中间件之前 403,把 D10 演示实时投影全局拦死。
本守卫在安全网 403 前放行「已授权演示品牌 + 只读」。判别锁死五条,并证明「删掉守卫会转红」
（每条都直接调 _demo_readonly_grant_failclosed 值级验证，不依赖起 HTTP 栈）。
"""
from types import SimpleNamespace
from unittest.mock import patch

import auth.middleware as mw


def _ctx(brand_id):
    return SimpleNamespace(brand_id=brand_id, access_mode="demo")


def test_authorized_brand_get_is_granted():
    with patch.object(mw, "resolve_demo_case_access", return_value=_ctx(601)) if hasattr(mw, "resolve_demo_case_access") \
            else patch("services.demo_access.resolve_demo_case_access", return_value=_ctx(601)):
        out = mw._demo_readonly_grant_failclosed(46, "GET", "case-x", 601)
    assert out is not None and int(out.brand_id) == 601


def test_cross_brand_is_denied_even_with_valid_grant():
    # 授权的是 601，请求 602 → 必须 None（跨租户不得放宽）
    with patch("services.demo_access.resolve_demo_case_access", return_value=_ctx(601)):
        assert mw._demo_readonly_grant_failclosed(46, "GET", "case-x", 602) is None


def test_write_methods_never_granted_here():
    with patch("services.demo_access.resolve_demo_case_access", return_value=_ctx(601)):
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            assert mw._demo_readonly_grant_failclosed(46, m, "case-x", 601) is None, m


def test_expired_or_revoked_grant_is_denied():
    # resolver 对失效 grant 返回 None → 守卫必须 None（保持 403）
    with patch("services.demo_access.resolve_demo_case_access", return_value=None):
        assert mw._demo_readonly_grant_failclosed(46, "GET", "case-x", 601) is None


def test_resolver_exception_fails_closed_not_open():
    # 关键：这里是跨租户读授权，异常必须拒（与同段 owner 判定的 fail-open 相反）
    with patch("services.demo_access.resolve_demo_case_access", side_effect=RuntimeError("db down")):
        assert mw._demo_readonly_grant_failclosed(46, "GET", "case-x", 601) is None


def test_missing_header_or_ids_denied():
    assert mw._demo_readonly_grant_failclosed(None, "GET", "case-x", 601) is None
    assert mw._demo_readonly_grant_failclosed(46, "GET", "", 601) is None
    assert mw._demo_readonly_grant_failclosed(46, "GET", "case-x", None) is None


def test_guard_removal_would_regress():
    # 反证：若守卫恒放行（无跨品牌/只读/失效校验），下面三条应能被放行——证明校验非空转
    with patch("services.demo_access.resolve_demo_case_access", return_value=_ctx(601)):
        granted = mw._demo_readonly_grant_failclosed(46, "GET", "case-x", 601) is not None
        cross = mw._demo_readonly_grant_failclosed(46, "GET", "case-x", 999) is not None
        write = mw._demo_readonly_grant_failclosed(46, "POST", "case-x", 601) is not None
    assert granted and not cross and not write
