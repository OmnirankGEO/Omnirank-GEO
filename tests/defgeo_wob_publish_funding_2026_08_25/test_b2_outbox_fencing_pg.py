"""B-2(= Codex P1-1)· outbox 租约 fencing:旧 worker 不许覆盖新租约。

═══════════════════════════════════════════════════════════════════════
🔴 这一族钉的那句话:**收尾的谓词是「还是我领的那一次吗」,不是「还是 claimed 吗」**
═══════════════════════════════════════════════════════════════════════
改之前 ``claim_outbox`` 不返 token,``settle_outbox`` / ``defer_outbox``
只按 ``status = 'claimed'`` 收尾。可达时序:

  t0  worker A 领走 #7(租约 300s),外调途中卡住;
  t1  租约过期,worker B 重新领走 #7;
  t2  A 醒来 settle(#7) —— 状态仍是 claimed ⇒ **A 把 B 的租约盖掉**。

与收敛器组合就是「先退款、后外发」:⑦ 见到 ``o.status='needs_review'`` 会退款,
而一个还活着的旧 worker 仍握着这条命令往外发。

配套的第二道(``dispatch_once`` 的"已结算不外发")单独有臂 —— fencing 挡的是
"迟到的收尾",那一道挡的是"迟到的外发"本身。
"""

from __future__ import annotations

import ast
import inspect

import pytest

from services.defensive_geo.publish import publish_outbox as _dispatch
from services.defensive_geo.publish import store as _store

from tests.defgeo_wob_publish_funding_2026_08_25._chain import (   # noqa: F401
    accepted, base, client, confirmed_command, drain, drain_outbox,
    must_not_be_called, run,
)
from tests.defgeo_wob_publish_funding_2026_08_25.conftest import connect


def _outbox(publish_command_id: str) -> dict:
    conn = connect()
    try:
        rows = _store.outbox_rows(conn.cursor(), publish_command_id=publish_command_id)
        assert rows, f"{publish_command_id} 没有 outbox 行 —— 场景没搭对"
        return dict(rows[0])
    finally:
        conn.close()


def _command(publish_command_id: str) -> dict:
    conn = connect()
    try:
        row = _store.get_command_any_tenant(
            conn.cursor(), publish_command_id=publish_command_id)
        assert row is not None
        return dict(row)
    finally:
        conn.close()


def _ready_outbox(client, name: str) -> tuple[str, int]:
    drain()
    drain_outbox()
    ctx = confirmed_command(client, name)
    return ctx["publishCommandId"], int(_outbox(ctx["publishCommandId"])["id"])


def _claim(publish_command_id: str, token: str) -> str:
    """真领一次租约,返回**这条行上真正的** claim_token。

    🔴 [E1-1] ``dispatch_once`` 现在必须带租约凭据 —— marker 那条 CAS 会验
       「outbox 仍被我这次租约持有」。所以判据也不能再凭空造 token:
       凭空造的过不了 CAS,那是**假红**;而真领一次才是生产的形状。
    """
    conn = connect()
    try:
        cur = conn.cursor()
        rows = _store.claim_outbox(cur, claim_token=token, limit=50)
        conn.commit()
    finally:
        conn.close()
    mine = [r for r in rows if str(r["publish_command_id"]) == str(publish_command_id)]
    assert mine, f"没领到 {publish_command_id} 的那一条:{[r['id'] for r in rows]}"
    return str(mine[0]["claim_token"])


# ══════════════════════════════════════════════════════════════════════════
# ① claim 返回 token
# ══════════════════════════════════════════════════════════════════════════
def test_b2_01_claim_returns_the_fencing_token(client) -> None:
    """``claim_outbox`` 的返回里必须有 ``claim_token`` —— 没有它就没法 fencing。"""
    cid, oid = _ready_outbox(client, "b2_token")
    conn = connect()
    try:
        cur = conn.cursor()
        rows = _store.claim_outbox(cur, claim_token="wob-tok-A", limit=50)
        conn.commit()
    finally:
        conn.close()
    mine = [r for r in rows if int(r["id"]) == oid]
    assert mine, f"我的 outbox #{oid} 没进这一批:{[r['id'] for r in rows]}"
    assert "claim_token" in mine[0], f"claim 没返回 fencing token:{sorted(mine[0])}"
    assert str(mine[0]["claim_token"]) == "wob-tok-A", mine[0]["claim_token"]


# ══════════════════════════════════════════════════════════════════════════
# ② 过期租约的收尾 0 行(settle / defer 两条)
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("op", ["settle", "defer"])
def test_b2_02_stale_lease_cannot_finish_the_row(client, op: str) -> None:
    """A 领 → 租约过期 → B 重领 → **A 的收尾 0 行**,B 的租约原样活着。"""
    cid, oid = _ready_outbox(client, f"b2_stale_{op}")
    conn = connect()
    try:
        cur = conn.cursor()
        first = {int(r["id"]): r for r in _store.claim_outbox(
            cur, claim_token="wob-tok-A", limit=50)}
        assert oid in first, "第一次领取没拿到我的行"
        token_a = str(first[oid]["claim_token"])
        # 租约过期 → B 重领(available_at 拨回当下)
        cur.execute(
            f"UPDATE {_store.OUTBOX_TABLE} SET available_at = NOW() - INTERVAL '1 second' "
            f"WHERE id = %s", (oid,))
        second = {int(r["id"]): r for r in _store.claim_outbox(
            cur, claim_token="wob-tok-B", limit=50)}
        assert oid in second, "B 没能重新领走"
        token_b = str(second[oid]["claim_token"])
        assert token_a != token_b

        if op == "settle":
            changed = _store.settle_outbox(
                cur, outbox_id=oid, claim_token=token_a, status="dispatched")
        else:
            changed = _store.defer_outbox(
                cur, outbox_id=oid, claim_token=token_a, seconds=60,
                last_error="迟到的延后")
        conn.commit()
    finally:
        conn.close()

    assert changed is False, (
        f"过期租约的 {op} 改到了行 —— 旧 worker 把新持有者的进度盖掉了")
    row = _outbox(cid)
    assert str(row["status"]) == "claimed", row["status"]
    assert str(row["claim_token"]) == token_b, (
        f"新持有者的 token 被抹了:{row['claim_token']}")


@pytest.mark.parametrize("op", ["settle", "defer"])
def test_b2_03_current_lease_can_finish_the_row(client, op: str) -> None:
    """反向对照:**当前**租约的收尾必须成功 —— 不许把正路也堵死。"""
    cid, oid = _ready_outbox(client, f"b2_ok_{op}")
    conn = connect()
    try:
        cur = conn.cursor()
        rows = {int(r["id"]): r for r in _store.claim_outbox(
            cur, claim_token="wob-tok-OK", limit=50)}
        token = str(rows[oid]["claim_token"])
        if op == "settle":
            changed = _store.settle_outbox(
                cur, outbox_id=oid, claim_token=token, status="dispatched")
        else:
            changed = _store.defer_outbox(
                cur, outbox_id=oid, claim_token=token, seconds=60, last_error="正常延后")
        conn.commit()
    finally:
        conn.close()
    assert changed is True, f"当前租约的 {op} 也被挡了 —— 收紧过头"


def test_b2_04_token_is_required_not_optional() -> None:
    """``claim_token`` 是**必填**的(没有默认值)—— 结构上不可能忘。"""
    for fn in (_store.settle_outbox, _store.defer_outbox):
        sig = inspect.signature(fn)
        param = sig.parameters.get("claim_token")
        assert param is not None, f"{fn.__name__} 没有 claim_token 形参"
        assert param.default is inspect.Parameter.empty, (
            f"{fn.__name__} 的 claim_token 有默认值 —— 忘了传就静默退化成无 fencing")
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, param.kind
    with pytest.raises(_store.StoreError):
        _store.settle_outbox(None, outbox_id=1, claim_token="", status="dispatched")
    with pytest.raises(_store.StoreError):
        _store.defer_outbox(None, outbox_id=1, claim_token="", seconds=1, last_error="x")


def test_b2_05_every_production_caller_passes_a_token() -> None:
    """AST:生产侧每一处 settle/defer 调用都真的传了 ``claim_token``。

    形参必填只保证"不传会 TypeError";这一条保证**没有人传了个空壳**
    (例如 ``claim_token=None``)。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    pkg = root / "services" / "defensive_geo"
    seen = 0
    for path in sorted(pkg.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in ("settle_outbox", "defer_outbox"):
                continue
            kw = {k.arg: k.value for k in node.keywords}
            assert "claim_token" in kw, (
                f"{path.relative_to(root).as_posix()}:{node.lineno} "
                f"调 {node.func.attr} 没传 claim_token")
            val = kw["claim_token"]
            assert not (isinstance(val, ast.Constant) and val.value in (None, "")), (
                f"{path.relative_to(root).as_posix()}:{node.lineno} 传的是空 token")
            seen += 1
    assert seen >= 4, f"只扫到 {seen} 个收尾调用点 —— 探针可能写废了"


# ══════════════════════════════════════════════════════════════════════════
# ③ 已退款的 command 不可能再被外发(新链调旧执行器:验被调方的领取谓词)
# ══════════════════════════════════════════════════════════════════════════
def test_b2_10_a_settled_command_is_never_dispatched(client) -> None:
    """钱已收尾的命令,``dispatch_once`` **一次外调都不发起**。

    🔴 这一条不是"看调用方有没有记得判",是**验被调方的领取谓词**:
       直接把一条已 settled 的 command 交给 ``dispatch_once``,
       provider 那一跳被换成"被调就炸"。
    """
    drain()
    drain_outbox()
    ctx = confirmed_command(client, "b2_settled_nodispatch")
    cid = ctx["publishCommandId"]
    # 走真链把它收敛到 released(零外调 ⇒ 权威零接单)
    conn = connect()
    try:
        cur = conn.cursor()
        _store.bump_status(cur, publish_command_id=cid,
                           canonical_publication_state="rejected_no_effect")
        conn.commit()
    finally:
        conn.close()
    drain()
    settled = _command(cid)
    assert settled["settled_at"] is not None, "场景没搭对:还没结算"
    # 🔴 [E1-1] 这一条钉的是 ⓪ 门(读 mapping 就该拒),它在 marker CAS **之前**
    #    返回,所以租约凭据取什么都不改变结论。给一个**明显不可能有效**的 token:
    #    万一 ⓪ 门被摘掉,后面那道 CAS 也会因租约不匹配而 0 行 —— 两道都失守才
    #    可能外发,而 note 断言会先把 ⓪ 门的失守暴露出来。
    tok = "b2-10-not-a-live-lease"
    assert str(settled["funding_state"]) == "released", settled["funding_state"]

    conn = connect()
    try:
        cur = conn.cursor()
        outcome = run(_dispatch.dispatch_once(
            cur, command=settled, frozen_body="工单B 判据正文 —— 已结算不该再发",
            provider_call=must_not_be_called(), claim_token=tok))
        conn.commit()
    finally:
        conn.close()
    assert outcome.provider_calls == int(settled["provider_call_count"]), (
        "已结算的命令仍然发起了外调 —— 先退款后外发")
    assert "已结算" in outcome.note or "结算之后" in outcome.note, outcome.note
    after = _command(cid)
    assert after["external_start_at"] is None, "已退款的命令被写上了 external-start marker"
    assert str(after["funding_state"]) == "released", after["funding_state"]


def test_b2_11_an_unsettled_command_still_dispatches(client) -> None:
    """反向对照:没结算的命令照常外发 —— 那道闸不许误伤活路径。"""
    drain()
    drain_outbox()
    ctx = confirmed_command(client, "b2_live_dispatch")
    cid = ctx["publishCommandId"]
    live = _command(cid)
    assert live["settled_at"] is None
    # 🔴 [E1-1] 活路径必须**真领一次租约** —— marker 的 CAS 现在要验
    #    「outbox 仍被我这次租约持有」。凭空造 token 会 0 行,那是假红。
    tok = _claim(cid, "wob-tok-LIVE")

    calls = {"n": 0}

    def _call(command):                                   # noqa: ANN001
        calls["n"] += 1
        return _dispatch.ProviderResult(
            kind="accepted", raw_table="mhz_publish_order_items",
            raw_column="status", raw_value="submitted",
            detail=f"WOB-LIVE-{str(cid)[-8:]}")

    conn = connect()
    try:
        cur = conn.cursor()
        run(_dispatch.dispatch_once(
            cur, command=live, frozen_body="工单B 判据正文", provider_call=_call,
            claim_token=tok))
        conn.commit()
    finally:
        conn.close()
    assert calls["n"] == 1, f"活命令没被外发(调了 {calls['n']} 次)—— 闸收紧过头"
