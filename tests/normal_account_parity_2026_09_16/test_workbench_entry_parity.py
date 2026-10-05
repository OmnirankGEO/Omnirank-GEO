# -*- coding: utf-8 -*-
"""普通账号与服务商:工作台入口完全一致(WO_222 §9)。

被治的缺陷:`api/c_end_api.py` 的 `switch_user_mode` 对 `agent_level == 0`
直接 403「开通服务商权限后可进入服务方工作台(消费满 ¥500 或邀请 5 个付费朋友)」,
`get_user_mode` 又把普通账号的 `recommended_route` 指到 `/c/chat`
—— 而 `/c/chat` 从 2026-06-03 起就是**死路由**(App.tsx 把 /c 与 /c/* 一律 Navigate 到 "/")。
"""
from __future__ import annotations

import asyncio


from tests.normal_account_parity_2026_09_16.conftest import fake_request


def _run(coro):
    return asyncio.run(coro)


def _read(mods, user_id):
    return _run(mods.c_end.get_user_mode(fake_request(user_id)))


def test_can_switch_is_no_longer_identity_scoped(mods, two_accounts):
    """`can_switch` 两臂都为真 —— 它原来是 `is_admin or agent_level >= 1`。"""
    l0 = _read(mods, two_accounts.l0)
    provider = _read(mods, two_accounts.provider)
    assert l0["can_switch"] is True and provider["can_switch"] is True, (l0, provider)


def test_the_two_arms_really_differ_in_agent_level(mods, two_accounts):
    """反向对照:两臂在 `agent_level` 上**确实**不同。

    🔴 没有这一条,上面每一条「两臂相同」都可能是因为两臂本来就是同一个人 ——
       本仓 a-zero-diff-between-two-arms-means-nothing-without-a-positive-control。
    """
    l0 = _read(mods, two_accounts.l0)
    provider = _read(mods, two_accounts.provider)
    assert l0["agent_level"] == 0, l0
    assert provider["agent_level"] >= 1, provider
