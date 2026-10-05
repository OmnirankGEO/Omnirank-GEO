# -*- coding: utf-8 -*-
"""监测自动化偏好:普通账号与服务商一致(WO_222 §9 · 监测在放开清单里)。

被治的缺陷:`PATCH /api/wallet/auto-monitor-preference` 原来卡
`agent_level >= 1` ⇒ 403「仅服务方可切换自动监测偏好」。
扣的是本账号自己钱包里的算力、按同一份功能目录计价,不涉及身份语义。
"""
from __future__ import annotations

import asyncio

from tests.normal_account_parity_2026_09_16.conftest import fake_request


def _set(mods, user_id, enabled):
    req = mods.wallet.AutoMonitorPreferenceRequest(auto_monitor_after_publish=enabled)
    return asyncio.run(mods.wallet.update_auto_monitor_preference(req, fake_request(user_id)))


def test_normal_account_can_turn_auto_monitor_on(mods, two_accounts):
    out = _set(mods, two_accounts.l0, True)
    assert out.get("success") is True, out


def test_both_arms_get_the_same_result(mods, two_accounts):
    """两臂同形:同一个动作,普通账号与服务商拿到同一个回包。"""
    l0 = _set(mods, two_accounts.l0, True)
    provider = _set(mods, two_accounts.provider, True)
    assert l0 == provider, "L0=%r 服务商=%r" % (l0, provider)


def test_the_switch_actually_lands_in_the_wallet_row(mods, two_accounts, db):
    """🔴 问被服务方,不问做事方。

    `success: True` 是**端点自己**说的。真判据是那一列真的被写了 ——
    本仓 the-signal-is-emitted-by-the-wrong-party(同形第十一件)。
    """
    _set(mods, two_accounts.l0, True)
    with db.cursor() as cur:
        cur.execute("SELECT auto_monitor_after_publish AS v FROM user_wallets WHERE user_id=%s",
                    (two_accounts.l0,))
        assert cur.fetchone()["v"] is True

    _set(mods, two_accounts.l0, False)
    with db.cursor() as cur:
        cur.execute("SELECT auto_monitor_after_publish AS v FROM user_wallets WHERE user_id=%s",
                    (two_accounts.l0,))
        assert cur.fetchone()["v"] is False, "关不回去 —— 写入方向没钉住"


def test_deduction_preference_is_still_provider_only(mods, two_accounts):
    """🔴 保留项:扣费偏好**没有**放开,而且这是有意的。

    `deduction_preference` 排的是 bonus / commission / paid 三个池子的消耗顺序,
    属于**资金语义**;WO_222 §9 白纸黑字「资金不变」。
    本条钉住"本单没有顺手把它一起开了" —— 没有它,我在 wallet_api 里多删一道闸
    不会有任何判据变红。
    """
    import pytest
    from fastapi import HTTPException

    req = mods.wallet.PreferenceUpdateRequest(deduction_preference="agent_friendly")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(mods.wallet.update_deduction_preference(req, fake_request(two_accounts.l0)))
    assert exc.value.status_code == 403, exc.value.status_code
