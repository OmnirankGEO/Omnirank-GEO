"""判别锁 · 用户名邀请重发崩溃(生产 live bug)+ 组织守卫吞异常。

现象:老板在「团队与席位」点「重发」→ 弹「组织权限校验暂不可用」。
真因(两层):
  ① `resend_invite` 无条件把邀请排进 `organization_invite_delivery_outbox`,
     而该表有 `CHECK (target_kind = ANY (ARRAY['phone','email']))`;
     用户名邀请 `target_kind='username'` → CheckViolation。
     `create_invite` 本来就有 `if target_kind != "username"` 守卫,重发路径漏了。
  ② 那是 psycopg2 异常不是 OrganizationError,一路冒到
     `OrganizationGuardMiddleware` 把 `call_next` 也包进去的兜底 `except Exception`,
     被改写成「组织权限校验暂不可用」(503 retryable)—— 方向完全带偏,
     而且 retryable 让人反复重试一个确定性失败。

生产实证(2026-07-28,只读通道):
  · invite #10 = username / pending,`resend_count` 恒 0、`version` 恒 1(主事务全回滚)
  · `organization_security_rate_events` 里 `invite.resend` 有 8 条(限流是独立事务,先落库)
  → 正好对上"点了很多次,一次没成功"。
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# ① 投递队列:不可投递的渠道必须跳过
# ---------------------------------------------------------------------------
class _StubCursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))


def _invite(target_kind: str) -> dict:
    return {
        "id": 10,
        "target_kind": target_kind,
        "target_hmac": "a" * 64,
        "delivery_ciphertext_or_reference": "cipher",
    }


def test_username_invite_is_never_queued_for_delivery():
    """🔒 用户名邀请**绝不**进投递队列 —— 进了就撞 DB 的 CHECK 约束。"""
    from services import organization_service as svc

    cursor = _StubCursor()
    queued = svc._queue_invite_link_delivery(
        cursor, invite=_invite("username"), token="t", request_id="r",
    )
    assert queued is False
    assert cursor.calls == [], (
        "用户名邀请仍然发起了 INSERT —— 会撞 "
        "organization_invite_delivery_outbox_target_kind_check"
    )


@pytest.mark.parametrize("kind", ["phone", "email"])
def test_deliverable_kinds_still_queue(kind, monkeypatch):
    """🔒 反向:手机号/邮箱**照常**入队(别把守卫写成"全都不发")。"""
    from services import organization_service as svc

    monkeypatch.setattr(svc, "encrypt_delivery_target", lambda *_a, **_k: ("ct", "v1"))
    cursor = _StubCursor()
    queued = svc._queue_invite_link_delivery(
        cursor, invite=_invite(kind), token="t", request_id="r",
    )
    assert queued is True
    assert len(cursor.calls) == 1
    sql, params = cursor.calls[0]
    assert "organization_invite_delivery_outbox" in sql
    assert kind in params, f"入队参数里没有 target_kind={kind}"


def test_deliverable_set_matches_db_check_constraint():
    """🔒 可投递渠道集合必须与 DB 的 CHECK 一一对应。

    生产 `organization_invite_delivery_outbox_target_kind_check`:
        CHECK (target_kind = ANY (ARRAY['phone','email']))
    两边任何一边加渠道,另一边必须同步 —— 否则又是一次 CheckViolation。
    """
    from services.organization_service import _DELIVERABLE_TARGET_KINDS

    assert _DELIVERABLE_TARGET_KINDS == frozenset({"phone", "email"})


def test_resend_call_site_also_guards():
    """🔒 重发调用点保留显式守卫(双保险 + 自解释)。

    用 **AST** 判而不是源码 grep:第一版这条写成 `'!= "username"' in src`,
    结果我自己在同一个函数里写的说明注释就含这段字面量 —— 把守卫整条删掉,
    测试照样绿。注释骗得过 grep,骗不过语法树。
    """
    import ast
    import inspect
    import textwrap

    from services.organization_service import resend_invite

    tree = ast.parse(textwrap.dedent(inspect.getsource(resend_invite)))

    def _guards_username(node: ast.If) -> bool:
        # 条件里出现字符串 'username'(注释与文档串不会进 AST)
        return any(
            isinstance(sub, ast.Constant) and sub.value == "username"
            for sub in ast.walk(node.test)
        )

    def _queues_delivery(node: ast.AST) -> bool:
        return any(
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == "_queue_invite_link_delivery"
            for sub in ast.walk(node)
        )

    guarded = any(
        isinstance(node, ast.If) and _guards_username(node) and _queues_delivery(node)
        for node in ast.walk(tree)
    )
    assert guarded, (
        "重发路径没有把 _queue_invite_link_delivery 放在 username 判断分支里 —— "
        "函数内守卫虽然还能兜住,但调用点的自解释性没了"
    )


# ---------------------------------------------------------------------------
# ② 组织守卫:端点异常不许被改写成权限错
# ---------------------------------------------------------------------------
def test_guard_does_not_mask_downstream_endpoint_errors():
    """🔒 端点抛的异常必须原样上抛,不能变成「组织权限校验暂不可用」。

    这条锁住的是**可诊断性**:原实现把 `call_next` 包进兜底 except,
    任何业务 bug 都显示成权限问题,且带 `retryable: True` 诱导无限重试。
    """
    import asyncio

    from middleware.organization_guard import OrganizationGuardMiddleware

    class _Boom(RuntimeError):
        pass

    async def _call_next(_request):
        raise _Boom("endpoint blew up")

    class _State:
        user = None

    class _Request:
        headers = {}
        state = _State()

    middleware = OrganizationGuardMiddleware.__new__(OrganizationGuardMiddleware)

    with pytest.raises(_Boom):
        asyncio.run(middleware.dispatch(_Request(), _call_next))
