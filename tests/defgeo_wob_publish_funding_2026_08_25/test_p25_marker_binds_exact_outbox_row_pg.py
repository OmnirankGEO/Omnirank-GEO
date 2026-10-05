# -*- coding: utf-8 -*-
"""P2-5(Codex 三审)· external-start marker 的 fencing 必须绑到**确切那一行 outbox**。

Codex:「publish marker 未来接第二种 outbox event 前应绑定 exact `outbox_id/event_kind`」。

改之前 `mark_external_start` 的 EXISTS 只按
``o.publish_command_id = c.publish_command_id AND o.status='claimed' AND o.claim_token = ?``
认账。今天每条 command 只有一种 event(``publish_command_created``),
所以它**恰好**就是那一行 —— 接进第二种 event 的那一刻,同一条 command
会有多行 outbox,「任意一行被我领着」就能授权外发:**fencing 认错行**。

修法是在 EXISTS 里补 ``AND o.event_kind = %s``。这与"绑 `outbox_id`"**等价**,
但等价性压在一条前提上:``UNIQUE (publish_command_id, event_kind)``。
🔴 **压在前提上的结论必须把前提也锁住** —— 本仓 2026-08-27 刚吃过这个亏
(MUT-EXTE2-07 的"冗余"压在"可达域只有一个值"上,而那件事没有任何判据锁着)。
所以这一族三条:①行为 ②前提在场 ③集合钉死。
"""

from __future__ import annotations

import pathlib

import pytest

from services.defensive_geo.publish import store as _store

from tests.defgeo_wob_publish_funding_2026_08_25._chain import (   # noqa: F401
    accepted, base, client, confirmed_command, drain, drain_outbox, run,
)
from tests.defgeo_wob_publish_funding_2026_08_25.conftest import connect

REPO = pathlib.Path(__file__).resolve().parents[2]


def _ready(client, name: str) -> str:
    drain()
    drain_outbox()
    return confirmed_command(client, name)["publishCommandId"]


def test_p25_01_a_claimed_row_of_another_event_kind_cannot_authorize_the_marker(
        client) -> None:
    """🔴 正样本 —— 点名规则:**别的 event 的 claimed 行不算授权**。

    真领一次租约(生产形状,不凭空造 token),然后拿**另一种** event_kind
    去写 marker:必须写不进去。改之前 EXISTS 不看 kind ⇒ 会写成功。
    """
    cid = _ready(client, "p25_kind")
    token = "defgeo-p25-" + "a" * 8
    conn = connect()
    try:
        cur = conn.cursor()
        rows = _store.claim_outbox(cur, claim_token=token, limit=50)
        conn.commit()
        mine = [r for r in rows if str(r["publish_command_id"]) == str(cid)]
        assert mine, "没领到这条命令的 outbox 行 —— 场景没搭对"
        real_kind = str(mine[0]["event_kind"])
        assert real_kind == _store.DISPATCH_EVENT_KIND, (
            f"这条行的 event_kind 是 {real_kind!r},与派发路径认的那种不同 —— "
            "先弄清楚是不是又加了一种 event")
        claim_token = str(mine[0]["claim_token"])

        # ① 拿一个**不存在于这条 command 上**的 kind ⇒ 必须授权失败
        ok = _store.mark_external_start(
            cur, publish_command_id=cid, token="tok-wrong-kind",
            claim_token=claim_token, event_kind="publish_command_settled")
        conn.rollback()
        assert ok is False, (
            "另一种 event_kind 的 claimed 行居然授权了外发 —— "
            "fencing 没绑到确切那一行(接第二种 event 之后就会认错行)")

        # ② 阴性对照:同一把租约、**正确的** kind ⇒ 必须授权成功。
        #    没有这一半,上面那条可能只是因为"这条 UPDATE 本来就写不进去"。
        ok2 = _store.mark_external_start(
            cur, publish_command_id=cid, token="tok-right-kind",
            claim_token=claim_token, event_kind=real_kind)
        conn.rollback()
        assert ok2 is True, (
            "正确 kind 也授权不了 —— 那么上面那条红证明不了任何事(判别力两向)")
    finally:
        conn.close()


def test_p25_04_the_second_event_kind_scenario_two_rules_side_by_side(client) -> None:
    """🔴 把 Codex 说的**未来那一幕**真造出来,并把两套规则并排跑。

    场景:同一条 command 上出现第二种 event(这正是"接第二种 outbox event"),
    而 worker 领走的是**那一行**(不是派发行)。此时:

      · 旧规则(EXISTS 不看 kind)⇒ "有一行被我领着" ⇒ **授权外发**(认错行);
      · 新规则(EXISTS 绑 kind) ⇒ 领的不是派发那一行 ⇒ **拒绝**。

    只断言新规则拒绝是不够的 —— 那条红也可能来自别的原因。所以把旧规则的
    SQL 原样在同一份数据上跑一遍,证明它**确实会放行**:差异是真的。
    """
    cid = _ready(client, "p25_two_kinds")
    other = "publish_command_settled"
    token = "defgeo-p25b-" + "b" * 8
    conn = connect()
    try:
        cur = conn.cursor()
        # 造第二种 event 的行(UNIQUE 是 (command, kind),所以插得进去)
        cur.execute(
            f"INSERT INTO {_store.OUTBOX_TABLE} "
            "(publish_command_id, publish_slot_id, event_kind) "
            f"SELECT publish_command_id, publish_slot_id, %s FROM {_store.OUTBOX_TABLE} "
            "WHERE publish_command_id = %s LIMIT 1",
            (other, cid))
        # 只把**第二种**那一行摆成 claimed(派发那一行保持原状)
        cur.execute(
            f"UPDATE {_store.OUTBOX_TABLE} SET status='claimed', claim_token=%s "
            "WHERE publish_command_id = %s AND event_kind = %s",
            (token, cid, other))
        assert cur.rowcount == 1, "第二种 event 的行没造出来 —— 场景没搭对"

        # ① 旧规则:原样跑一遍它的 EXISTS(不看 kind)
        cur.execute(
            f"SELECT EXISTS (SELECT 1 FROM {_store.OUTBOX_TABLE} o "
            " WHERE o.publish_command_id = %s AND o.status='claimed' "
            "   AND o.claim_token = %s) AS ok", (cid, token))
        # 本仓游标是 RealDictCursor —— 取名列,不按位置取。
        old_would_authorize = bool(cur.fetchone()["ok"])

        # ② 新规则:实跑那条 CAS,带派发那一种 kind
        new_authorized = _store.mark_external_start(
            cur, publish_command_id=cid, token="tok-two-kinds",
            claim_token=token, event_kind=_store.DISPATCH_EVENT_KIND)
        conn.rollback()
    finally:
        conn.close()

    assert old_would_authorize is True, (
        "旧规则在这一幕下没有放行 —— 那这条判据打的不是真差异,场景要重搭")
    assert new_authorized is False, (
        "新规则也放行了 —— fencing 仍然认错行,P2-5 没修好")


def test_p25_02_the_uniqueness_premise_is_actually_in_the_migration() -> None:
    """前提锁:``UNIQUE (publish_command_id, event_kind)`` 必须在迁移里。

    「绑 kind 等价于绑 outbox_id」这句话**只在这条约束成立时成立**。
    约束哪天被人改宽,等价性当场失效,而代码不会有任何反应 —— 所以锁它。
    """
    sql = (REPO / "db" / "migration_044_defgeo_publish_decision_2026_08_21.sql"
           ).read_text(encoding="utf-8")
    assert "UNIQUE (publish_command_id, event_kind)" in sql, (
        "outbox 的 (command, kind) 唯一约束不在了 —— "
        "「绑 kind ≡ 绑 outbox_id」这个等价性没了,marker 必须改成显式绑 outbox_id")


def test_p25_03_the_known_event_kind_set_is_pinned() -> None:
    """集合钉死:今天只有一种 event kind。**加第二种的人会先看到这条红。**

    这正是 Codex 说的"接第二种之前"的那个时刻 —— 到那时,
    `dispatch_once` / worker 必须把 kind 显式穿进 `mark_external_start`,
    而不是继续吃默认值。
    """
    assert _store.KNOWN_OUTBOX_EVENT_KINDS == {"publish_command_created"}, (
        f"outbox event kind 集合变成了 {sorted(_store.KNOWN_OUTBOX_EVENT_KINDS)} —— "
        "派发路径必须显式传 event_kind,默认值不再安全")
    assert _store.DISPATCH_EVENT_KIND in _store.KNOWN_OUTBOX_EVENT_KINDS
