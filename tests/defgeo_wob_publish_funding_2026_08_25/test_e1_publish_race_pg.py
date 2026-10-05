# -*- coding: utf-8 -*-
"""E1-1(= Codex 二审 P0-F1)· 退款完成后旧 worker 仍可外发。

═══════════════════════════════════════════════════════════════════════
🔴 这一族钉的那句话:**能不能外发,由写 marker 那一刻的服务端事实说了算**
═══════════════════════════════════════════════════════════════════════
改之前 ``mark_external_start`` 的 WHERE 只有::

    publish_command_id = ? AND external_start_at IS NULL

于是 Codex 用双连接 PG16 复现出这条时序(不是推演,是实跑):

  t0  worker A 读到一份**还没结算**的 command mapping,暂停;
  t1  A 的租约过期,worker B / 收敛器⑦ 完成 release —— 钱退了,
      ``settled_at`` 落库,commandState 摆成 ``needs_action``;
  t2  A 用手里那份**旧 mapping** 恢复。``dispatch_once`` 开头那道
      「已结算不外发」读的正是这份旧 mapping,所以放行;
  t3  A 写 marker **成功**,真调 provider。

  观测:``funding_after=released`` 且 ``provider_calls=1``。
  **钱已经退了,货还是发出去了。**

根因是「判定依据」与「写入时刻」之间隔着一个可被抢跑的窗口:读的是事务开始时的
快照,写的是现在。多加几句 ``if`` 只能把窗口挪短,窗口还在。所以修法是把条件塞进
**同一条 UPDATE 的 WHERE**,由数据库在写这一行的那一刻一次性裁决。

本族的分工
----------
· ``test_e1_00`` 活性对照 —— 一条都不许误伤:正常形状必须真的写 marker、真的外调。
  没有它,一个恒返 False 的实现能让下面每一条都全绿,而 worker 永远不发货。
· ``test_e1_01..04`` **一条件一臂**:每一臂只让 WHERE 里的**一个**条件不成立,
  其余全部成立。删掉哪个条件,就只有对应那一臂变红 —— 这才是「逐条承重」的证明,
  用一个大场景同时踩四个条件是证不出来的(删任一条件它都还红)。
· ``test_e1_10`` **双连接真交错**:复刻报告的四步时序,走完整 ``dispatch_once``,
  断言 provider **零调用**。
· ``test_e1_20`` 枚举锁:可派发/不可派发两个集合必须把库 CHECK 的整个状态域**分完**。
  新加一个状态就必须表态,不许悄悄落进"默认允许"那一侧。
"""

from __future__ import annotations

import re

import pytest

from services.defensive_geo.publish import publish_outbox as _dispatch
from services.defensive_geo.publish import store as _store

from tests.defgeo_wob_publish_funding_2026_08_25._chain import (   # noqa: F401
    base, client, confirmed_command, drain, drain_outbox,
    must_not_be_called, run,
)
from tests.defgeo_wob_publish_funding_2026_08_25.conftest import connect


# ══════════════════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════════════════
def _command(cid: str) -> dict:
    conn = connect()
    try:
        row = _store.get_command_any_tenant(conn.cursor(), publish_command_id=cid)
        assert row is not None
        return dict(row)
    finally:
        conn.close()


def _sql(statement: str, params: tuple = ()) -> None:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(statement, params)
        conn.commit()
    finally:
        conn.close()


def _fresh(client, name: str) -> tuple[str, int, str]:
    """真确认一条命令,并**真领一次租约**。返回 ``(command_id, outbox_id, token)``。"""
    drain()
    drain_outbox()
    ctx = confirmed_command(client, name)
    cid = ctx["publishCommandId"]
    conn = connect()
    try:
        cur = conn.cursor()
        claimed = _store.claim_outbox(cur, claim_token=f"e1-{name}", limit=50)
        conn.commit()
    finally:
        conn.close()
    mine = [r for r in claimed if str(r["publish_command_id"]) == cid]
    assert mine, f"没领到 {cid} 的 outbox 行"
    return cid, int(mine[0]["id"]), str(mine[0]["claim_token"])


def _mark(cid: str, *, claim_token: str) -> bool:
    """在**独立事务**里调一次 marker CAS,提交后返回结果。"""
    conn = connect()
    try:
        cur = conn.cursor()
        ok = _store.mark_external_start(
            cur, publish_command_id=cid, token="e1-marker", claim_token=claim_token)
        conn.commit()
        return bool(ok)
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# ⓪ 活性对照 —— 正常形状必须放行
# ══════════════════════════════════════════════════════════════════════════
def test_e1_00_a_healthy_command_with_a_live_lease_is_marked(client) -> None:
    """🔴 反向对照。恒返 False 的实现能让下面四条全绿,而 worker 永远不发货。"""
    cid, _oid, tok = _fresh(client, "e1_live")
    before = _command(cid)
    assert before["settled_at"] is None
    assert str(before["funding_state"]) in _store.DISPATCHABLE_FUNDING_STATES
    assert str(before["command_state"]) in _store.DISPATCHABLE_COMMAND_STATES

    assert _mark(cid, claim_token=tok) is True, (
        "正常形状 + 有效租约都写不进 marker —— 闸收紧过头,worker 永远发不出货")

    after = _command(cid)
    assert after["external_start_at"] is not None
    assert int(after["provider_call_count"]) == int(before["provider_call_count"]) + 1


# ══════════════════════════════════════════════════════════════════════════
# ①–④ 一条件一臂:每一臂只让 WHERE 里的**一个**条件不成立
# ══════════════════════════════════════════════════════════════════════════
def test_e1_01_a_stale_lease_cannot_mark(client) -> None:
    """条件① fencing:租约已被别人重领,我就不是有权外发的那个人。

    其余三个条件**全部成立**(未结算 / 资金 frozen / 命令 queued)——
    所以删掉 WHERE 里的 EXISTS 子句,只有这一臂会红。
    """
    cid, oid, tok_a = _fresh(client, "e1_stale_lease")
    # 租约过期 → B 重领
    _sql(f"UPDATE {_store.OUTBOX_TABLE} SET available_at = NOW() - INTERVAL '1 hour' "
         "WHERE id = %s", (oid,))
    conn = connect()
    try:
        cur = conn.cursor()
        again = _store.claim_outbox(cur, claim_token="e1-worker-B", limit=50)
        conn.commit()
    finally:
        conn.close()
    tok_b = [str(r["claim_token"]) for r in again if int(r["id"]) == oid]
    assert tok_b and tok_b[0] != tok_a, "场景没搭对:B 没能重领"

    row = _command(cid)
    assert row["settled_at"] is None, "场景不纯:这一臂只该让**租约**不成立"
    assert str(row["funding_state"]) in _store.DISPATCHABLE_FUNDING_STATES
    assert str(row["command_state"]) in _store.DISPATCHABLE_COMMAND_STATES

    assert _mark(cid, claim_token=tok_a) is False, (
        "过期租约写进了 external-start marker —— 旧 worker 仍可对外发货")
    assert _command(cid)["external_start_at"] is None


def test_e1_02_a_settled_command_cannot_mark(client) -> None:
    """条件② ``settled_at IS NULL``:钱已按某个方向收过尾,货不能再发。

    只动 ``settled_at``,资金态/命令态**留在允许集里** —— 这一臂专证那一个谓词承重。
    """
    cid, _oid, tok = _fresh(client, "e1_settled")
    _sql("UPDATE defgeo_publish_commands SET settled_at = NOW() "
         "WHERE publish_command_id = %s", (cid,))

    row = _command(cid)
    assert row["settled_at"] is not None
    assert str(row["funding_state"]) in _store.DISPATCHABLE_FUNDING_STATES, (
        "场景不纯:这一臂只该让 settled_at 不成立")
    assert str(row["command_state"]) in _store.DISPATCHABLE_COMMAND_STATES

    assert _mark(cid, claim_token=tok) is False, (
        "已收尾的命令写进了 marker —— 先退款后外发")
    assert _command(cid)["external_start_at"] is None


def test_e1_03_a_non_dispatchable_funding_state_cannot_mark(client) -> None:
    """条件③ 资金态:``released`` 等于钱已经回去了,不许再发。

    ``settled_at`` 留 NULL、命令态留 queued —— 只让资金态不成立。
    """
    cid, _oid, tok = _fresh(client, "e1_fund_state")
    _sql("UPDATE defgeo_publish_commands SET funding_state = 'released' "
         "WHERE publish_command_id = %s", (cid,))

    row = _command(cid)
    assert row["settled_at"] is None, "场景不纯:这一臂只该让资金态不成立"
    assert str(row["funding_state"]) in _store.NON_DISPATCHABLE_FUNDING_STATES
    assert str(row["command_state"]) in _store.DISPATCHABLE_COMMAND_STATES

    assert _mark(cid, claim_token=tok) is False, (
        f"资金态 {row['funding_state']} 仍然写进了 marker")
    assert _command(cid)["external_start_at"] is None


def test_e1_04_a_non_dispatchable_command_state_cannot_mark(client) -> None:
    """条件④ 命令态:``needs_action`` 正是收敛器⑦ release 之后摆的那个。

    它**不是终局命令态** —— 正因如此旧 worker 才能一路走到外调,
    这是 P0-F1 的载体状态。``settled_at`` 留 NULL、资金态留 frozen。
    """
    cid, _oid, tok = _fresh(client, "e1_cmd_state")
    _sql("UPDATE defgeo_publish_commands SET command_state = 'needs_action' "
         "WHERE publish_command_id = %s", (cid,))

    row = _command(cid)
    assert row["settled_at"] is None, "场景不纯:这一臂只该让命令态不成立"
    assert str(row["funding_state"]) in _store.DISPATCHABLE_FUNDING_STATES
    assert str(row["command_state"]) in _store.NON_DISPATCHABLE_COMMAND_STATES

    assert _mark(cid, claim_token=tok) is False, (
        "needs_action 的命令写进了 marker —— 退款之后那个窗口还开着")
    assert _command(cid)["external_start_at"] is None


# ══════════════════════════════════════════════════════════════════════════
# ⑩ 双连接真交错 —— 复刻报告的四步时序
# ══════════════════════════════════════════════════════════════════════════
def test_e1_10_the_reported_four_step_interleaving_dispatches_nothing(client) -> None:
    """🔴 报告那条时序的原样复刻,走**完整 dispatch_once**,provider 必须零调用。

    两条连接同时活着:

      connA(worker A)  BEGIN → 读到未结算 mapping → **事务保持打开**
      connB(收敛器)                                → 完成 release 并提交
      connA                                        → 用旧 mapping 继续走 dispatch_once

    关键在于 A 手里那份 mapping 是**旧的**:``dispatch_once`` 开头那道
    「已结算不外发」读的就是它,所以那道门会**放行** —— 这一条能不能过,
    全看 marker 那条 CAS 是不是用**服务端当下的事实**裁决。
    """
    cid, oid, tok_a = _fresh(client, "e1_race")

    # ── t0:worker A 开事务,读到「还没结算」的 mapping,然后停在这儿 ──────
    conn_a = connect()
    try:
        cur_a = conn_a.cursor()
        stale = dict(_store.get_command_any_tenant(cur_a, publish_command_id=cid))
        assert stale["settled_at"] is None, "场景没搭对:A 读到的就已经是结算过的"

        # ── t1:另一条连接上完成 release(钱退了)──────────────────────
        #    走真链:标成权威零接单 → 收敛器 release。A 的事务全程开着。
        conn_b = connect()
        try:
            cur_b = conn_b.cursor()
            _store.bump_status(cur_b, publish_command_id=cid,
                               canonical_publication_state="rejected_no_effect")
            conn_b.commit()
        finally:
            conn_b.close()
        drain()

        settled_now = _command(cid)
        assert settled_now["settled_at"] is not None, "场景没搭对:release 没真发生"
        assert str(settled_now["funding_state"]) == "released", settled_now["funding_state"]
        # A 手里那份仍然是旧的 —— 这正是 ⓪ 门挡不住的原因。
        assert stale["settled_at"] is None

        # ── t2/t3:A 用旧 mapping 恢复,走完整 dispatch_once ──────────────
        outcome = run(_dispatch.dispatch_once(
            cur_a, command=stale,
            frozen_body="E1 判据正文 —— 退款之后不该再发",
            provider_call=must_not_be_called(), claim_token=tok_a))
        conn_a.commit()
    finally:
        conn_a.close()

    after = _command(cid)
    assert after["external_start_at"] is None, (
        "退款完成之后,旧 worker 仍然写上了 external-start marker —— P0-F1 未闭合")
    assert int(after["provider_call_count"]) == int(settled_now["provider_call_count"]), (
        f"provider_call_count 从 {settled_now['provider_call_count']} 涨到 "
        f"{after['provider_call_count']} —— 钱退了货还是发了")
    assert outcome.provider_calls == int(settled_now["provider_call_count"])
    assert str(after["funding_state"]) == "released", after["funding_state"]


# ══════════════════════════════════════════════════════════════════════════
# ⑳ 枚举锁:两个集合必须把库 CHECK 的整个域分完
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "conname,dispatchable,non_dispatchable",
    [
        ("chk_defgeo_pcmd_funding_state",
         _store.DISPATCHABLE_FUNDING_STATES, _store.NON_DISPATCHABLE_FUNDING_STATES),
        ("chk_defgeo_pcmd_command_state",
         _store.DISPATCHABLE_COMMAND_STATES, _store.NON_DISPATCHABLE_COMMAND_STATES),
    ],
)
def test_e1_20_every_state_in_the_db_domain_is_classified(
    client, conname: str, dispatchable: tuple, non_dispatchable: tuple,
) -> None:
    """🔴 域里每一个值都必须被**显式分类**成"可派发"或"不可派发"。

    手写白名单最典型的坏法:域里新加一个状态,它既不在允许集也不在禁止集 ——
    于是没有任何判据变红,而那个新状态**默认落在被 CAS 挡住的一侧**
    (或者更糟,有人为了让它跑通就顺手加进允许集)。
    这一条把分类做成必须表态的事。
    """
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
                    "WHERE conname = %s", (conname,))
        row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None, f"{conname} 不存在 —— 域没人守,分类也就没有边界"
    domain = set(re.findall(r"'([a-z_]+)'::character varying", str(row["d"])))
    assert domain, f"没从 {conname} 里解析出任何状态值:{row['d']}"

    a, b = set(dispatchable), set(non_dispatchable)
    assert not (a & b), f"同一个状态既可派发又不可派发:{sorted(a & b)}"
    assert a | b == domain, (
        f"分类没覆盖整个域。库里有而没分类的:{sorted(domain - (a | b))};"
        f"分类了但库里没有的:{sorted((a | b) - domain)}")


# ══════════════════════════════════════════════════════════════════════════
# E1-2(= Codex 二审 §9 P2)· 计数与写入必须同命运
# ══════════════════════════════════════════════════════════════════════════
# 🔴 原来是**先 ``+= 1`` 再写**:
#
#     if attempt_count >= MAX_ATTEMPTS:
#         quarantined += 1
#         _quarantine_row(...)          # ← 租约已易主时命中 0 行
#     else:
#         deferred += 1
#         _defer(...)                   # ← 同上
#
# 租约过期被别人重领时,两个写入的 fencing 谓词都命中 0 行 —— **什么都没发生**,
# 而运维统计里多了一笔"延后 / 转人工"。看板上那条曲线说的是一件没做过的事。
#
# 这两条钉的是**调用点**:计数只能由"写入真的落了"驱动。
# 把修复原样删回去(无条件 += 1),它们必须红。
from services.defensive_geo.publish import publish_worker as _worker   # noqa: E402
from services.defensive_geo.publish import provider_transport as _transport  # noqa: E402


def _transport_down(monkeypatch) -> None:
    """与 B-3 / B-7 同一个注入点 —— 换个地方 patch 就可能绕开真正那条 except。"""
    def _boom(*_a, **_kw):
        raise _transport.TransportNotConfigured("E1:注入 —— 外发通道未配置")
    monkeypatch.setattr(_transport, "resolve", _boom)


def test_e1_30_a_defer_that_wrote_nothing_is_not_counted(client, monkeypatch) -> None:
    """写入 0 行 ⇒ ``deferred`` **不许 +1**。"""
    drain()
    drain_outbox()
    confirmed_command(client, "e1_defer_lost")
    _transport_down(monkeypatch)
    # 租约易主的效果:fencing 谓词命中 0 行。直接让被调方如实返回 False。
    monkeypatch.setattr(_worker._store, "defer_outbox", lambda *a, **kw: False)

    out = run(_worker.dispatch_pending(limit=50))
    assert out["deferred"] == 0, (
        f"写入 0 行却记了 {out['deferred']} 笔延后 —— 运维统计里多了一件没发生的事")


def test_e1_31_a_defer_that_landed_is_counted(client, monkeypatch) -> None:
    """反向对照:真写进去了就必须计数 —— 不许为了让上一条绿而把计数删了。"""
    drain()
    drain_outbox()
    confirmed_command(client, "e1_defer_ok")
    _transport_down(monkeypatch)

    out = run(_worker.dispatch_pending(limit=50))
    assert out["deferred"] >= 1, f"真延后了却一笔都没记:{out}"


def test_e1_32_a_quarantine_that_wrote_nothing_is_not_counted(client, monkeypatch) -> None:
    """达上限那一支同理:``settle_outbox`` 0 行 ⇒ ``quarantined`` 不许 +1。"""
    drain()
    drain_outbox()
    cid = confirmed_command(client, "e1_quar_lost")["publishCommandId"]
    _sql(f"UPDATE {_store.OUTBOX_TABLE} SET attempt_count = %s "
         "WHERE publish_command_id = %s", (_worker.MAX_ATTEMPTS, cid))
    _transport_down(monkeypatch)
    monkeypatch.setattr(_worker._store, "settle_outbox", lambda *a, **kw: False)

    out = run(_worker.dispatch_pending(limit=50))
    assert out["quarantined"] == 0, (
        f"转人工写了 0 行却记了 {out['quarantined']} 笔 —— 运维以为有人去看了,其实没有")


def test_e1_33_a_quarantine_that_landed_is_counted(client, monkeypatch) -> None:
    """反向对照:真转人工了必须计数。"""
    drain()
    drain_outbox()
    cid = confirmed_command(client, "e1_quar_ok")["publishCommandId"]
    _sql(f"UPDATE {_store.OUTBOX_TABLE} SET attempt_count = %s "
         "WHERE publish_command_id = %s", (_worker.MAX_ATTEMPTS, cid))
    _transport_down(monkeypatch)

    out = run(_worker.dispatch_pending(limit=50))
    assert out["quarantined"] >= 1, f"真转人工了却一笔都没记:{out}"
