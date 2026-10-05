# -*- coding: utf-8 -*-
"""边界臂:经营后台**仍然**只对服务商开(WO_222 §9 的另一半)。

🔴 这一包其余判据全在证明「两臂相同」。只有这些的话,
   我把经营后台也一起放开会**照样全绿** —— "全放开"那句话就没有边界了。
   本仓 criteria-all-lived-on-the-fixed-side-of-the-seam:判据全长在一侧。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from tests.normal_account_parity_2026_09_16.conftest import fake_request


def test_normal_account_is_refused_by_the_provider_workbench(mods, two_accounts):
    """普通账号进 `agent_workbench`(进货/定价/结算)⇒ 403。"""
    with pytest.raises(HTTPException) as exc:
        mods.workbench._require_agent(fake_request(two_accounts.l0))
    assert exc.value.status_code == 403, exc.value.status_code


def test_the_provider_is_let_through(mods, two_accounts):
    """反臂:服务商能过 —— 否则上一条的 403 可能只是"谁都进不去"。"""
    got = mods.workbench._require_agent(fake_request(two_accounts.provider))
    assert got.get("user_id") == two_accounts.provider, got


def test_the_gate_reads_the_live_wallet_not_the_request(mods, two_accounts):
    """闸取的是**库里的** agent_level,不是请求里带的字段。

    🔴 假 Request 里我一个 `agent_level` 都没放。如果闸改成读 `request.state.user`
       里的值,普通账号那条会因为"取不到 ⇒ 当 0"而**照样 403**,看起来还是绿的,
       但生产上浏览器就能自己声明身份。这条用服务商臂把它钉住:
       服务商的 Request 里同样没有 agent_level,他能过 ⇒ 闸确实回库查了。
    """
    assert "agent_level" not in fake_request(two_accounts.provider).state.user
    assert mods.workbench._require_agent(fake_request(two_accounts.provider))
