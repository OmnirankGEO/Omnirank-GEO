"""B-3(= Codex P1-2)· 通道能力在冻钱之前问一次;通道不通不许无限延期挂钱。

═══════════════════════════════════════════════════════════════════════
🔴 两半各钉一句
═══════════════════════════════════════════════════════════════════════
上半:``api/defensive_publish_api._do_confirm`` 原来写死 ``capability_available=True``。
     写死 ⇒ ``work_admission`` 的 ``capability_unavailable`` 那一格从上线起
     **一次都没执行过**(守卫的候选集里永远不含它)。
     现在由真实登记表 ``provider_transport.readiness()`` 回答。

     🟡 口径说明(与代码里那段被订正的注释同源):这个探针只问
     「``phpsessid`` 这一行**在不在**」,不问它有没有过期 —— 过期的 cookie
     仍是非空字符串,``ready`` 仍是 True。所以它挡的是「从来没配过」,
     不是「这次不通」。后者仍走收敛路径(⑦ release 一次 + needs_action)。

下半:worker 捕到 ``TransportNotConfigured`` 后原来**无条件** defer,
     每 20 秒延后 120 秒、永远延下去 —— 客户的算力无限期冻着,
     而 outbox 永远 pending ⇒ 收敛器⑦ 的 ``o.status='needs_review'``
     永不成立 ⇒ 那条"零外调就退款"的活路也永远走不到。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from services.defensive_geo.publish import provider_transport as _transport
from services.defensive_geo.publish import publish_worker as _worker
from services.defensive_geo.publish import store as _store

from tests.defgeo_wob_publish_funding_2026_08_25 import _seed
from tests.defgeo_wob_publish_funding_2026_08_25._chain import (   # noqa: F401
    base, client, confirm, drain, drain_outbox, preview, ready_context, run,
)
from tests.defgeo_wob_publish_funding_2026_08_25.conftest import connect

ROOT = Path(__file__).resolve().parents[2]


def _counts() -> dict[str, int]:
    """本链的全局计数 —— 「零副作用」必须是**真的数了**,不是"没查"。"""
    conn = connect()
    try:
        cur = conn.cursor()
        out: dict[str, int] = {}
        for key, sql in (
            ("commands", f"SELECT COUNT(*) AS c FROM {_store.COMMAND_TABLE}"),
            ("outbox", f"SELECT COUNT(*) AS c FROM {_store.OUTBOX_TABLE}"),
            ("freezes", "SELECT COUNT(*) AS c FROM point_freezes "
                        "WHERE task_ref LIKE 'defgeo_publish_%%'"),
        ):
            cur.execute(sql)
            out[key] = int(cur.fetchone()["c"])
        return out
    finally:
        conn.close()


def _drop_channel_config() -> str | None:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM mhz_config WHERE key='phpsessid'")
        row = cur.fetchone()
        before = row["value"] if row else None
        cur.execute("DELETE FROM mhz_config WHERE key='phpsessid'")
        conn.commit()
        return before
    finally:
        conn.close()


def _restore_channel_config(value: str | None) -> None:
    conn = connect()
    try:
        cur = conn.cursor()
        if value is None:
            cur.execute("DELETE FROM mhz_config WHERE key='phpsessid'")
        else:
            cur.execute(
                "INSERT INTO mhz_config (key, value) VALUES ('phpsessid', %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (value,))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def unconfigured_channel():
    """把外发通道**真的**拆掉(删配置行),用完还原。不 patch 探针。

    🔴 patch 掉 ``readiness`` 等于夹具替被测代码回答 —— 那条闸会恒绿。
       这里删的是它真读的那一行,链路一整条都是真的。
    """
    before = _drop_channel_config()
    try:
        yield
    finally:
        _restore_channel_config(before)


# ══════════════════════════════════════════════════════════════════════════
# ① confirm 侧:通道没配过 ⇒ typed 503 + 零副作用
# ══════════════════════════════════════════════════════════════════════════
def test_b3_01_readiness_reflects_the_real_config(unconfigured_channel) -> None:
    """先自证探针是活的:配置行删掉 ⇒ ``readiness().ready`` 必须是 False。"""
    state = _transport.readiness()
    assert state.ready is False, (
        "配置行都删了 readiness 还说 ready —— 探针写废了,下面的判据全是空气")
    assert state.reason, "不 ready 却没有给人看的理由"
    assert "phpsessid" not in state.reason and "mhz" not in state.reason.lower(), (
        f"理由里泄了内部参数/供应商:{state.reason}(POR-14)")


def test_b3_02_confirm_is_typed_503_with_zero_side_effects(
        client, unconfigured_channel) -> None:
    """通道从来没配过 ⇒ confirm **零冻结 / 零 command / 零 outbox**。

    🔴 「删掉修复即红」:把 ``capability_available`` 改回写死 ``True``,
       这条当场变成 200 + 一笔真冻结。
    """
    drain()
    drain_outbox()
    ctx = ready_context("b3_unavailable")
    snap = preview(client, ctx)["snapshotResponse"]
    before = _counts()
    wallet_before = _seed.wallet(_seed.TENANT_A)

    resp = confirm(client, snap)
    assert resp.status_code == 503, f"期望 503,实得 {resp.status_code}: {resp.text[:400]}"
    body = resp.json()["detail"]
    assert body["code"] == "ADMISSION_UNAVAILABLE", body
    assert body["retryable"] is True, body

    after = _counts()
    assert after == before, f"通道不可用却留下了副作用:{before} → {after}"
    assert _seed.wallet(_seed.TENANT_A) == wallet_before, "通道不可用却冻了钱"


def test_b3_03_confirm_succeeds_once_the_channel_is_configured(client) -> None:
    """反向对照:通道配好了就照常放行 —— 那道闸不许拦住正常销售动作。"""
    drain()
    drain_outbox()
    ctx = ready_context("b3_available")
    snap = preview(client, ctx)["snapshotResponse"]
    resp = confirm(client, snap)
    assert resp.status_code == 200, f"通道正常却被拦:{resp.text[:400]}"


def test_b3_04_capability_flag_is_not_hardcoded() -> None:
    """AST:``capability_available=`` 的实参**不是常量**。

    行为判据钉的是后果;这一条钉的是"改回写死"这个动作本身
    (本仓记过:门禁接了但接线是坏的 —— 写死的守卫参数 = 那一格永不执行)。
    """
    path = ROOT / "api" / "defensive_publish_api.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "capability_available":
                continue
            found += 1
            assert not isinstance(kw.value, ast.Constant), (
                f"defensive_publish_api.py:{node.lineno} 把 capability_available "
                f"写死成 {getattr(kw.value, 'value', '?')!r} —— "
                "那一格守卫的候选集会永远为空")
    assert found == 1, f"扫到 {found} 处 capability_available —— 探针写废了"


# ══════════════════════════════════════════════════════════════════════════
# ② worker 侧:通道不通有 attempt 上限,不无限延期
# ══════════════════════════════════════════════════════════════════════════
def _transport_down(monkeypatch) -> None:
    def _boom(*_a, **_kw):
        raise _transport.TransportNotConfigured("注入:外发通道未配置")
    monkeypatch.setattr(_transport, "resolve", _boom)


def test_b3_10_transport_down_defers_below_the_ceiling(client, monkeypatch) -> None:
    """没到上限:放回队列、**资金态一格不动**(与"这条坏了"分开)。"""
    drain()
    drain_outbox()
    ctx = confirmed_command_local(client, "b3_defer")
    cid = ctx["publishCommandId"]
    _transport_down(monkeypatch)

    out = run(_worker.dispatch_pending(limit=50))
    assert out["deferred"] >= 1, out
    assert out.get("quarantined", 0) == 0, out

    conn = connect()
    try:
        rows = _store.outbox_rows(conn.cursor(), publish_command_id=cid)
    finally:
        conn.close()
    assert str(rows[0]["status"]) == "pending", rows[0]["status"]
    row = _command_row(cid)
    assert str(row["funding_state"]) == "frozen", row["funding_state"]
    assert row["external_start_at"] is None, "通道没通却写了 external-start marker"


def test_b3_11_transport_down_stops_deferring_at_the_ceiling(client, monkeypatch) -> None:
    """达 ``MAX_ATTEMPTS`` ⇒ 收成 ``needs_review``,不再无限延期。

    🔴 「删掉修复即红」:把 worker 里那个 ``attempt_count >= MAX_ATTEMPTS``
       分支拿掉,这条会永远停在 pending。
    """
    drain()
    drain_outbox()
    ctx = confirmed_command_local(client, "b3_ceiling")
    cid = ctx["publishCommandId"]
    _transport_down(monkeypatch)

    conn = connect()
    try:
        rows = _store.outbox_rows(conn.cursor(), publish_command_id=cid)
        oid = int(rows[0]["id"])
    finally:
        conn.close()

    seen_quarantine = False
    for _ in range(_worker.MAX_ATTEMPTS + 3):
        _force_available(oid)
        out = run(_worker.dispatch_pending(limit=50))
        if out.get("quarantined", 0):
            seen_quarantine = True
            break
    assert seen_quarantine, (
        f"通道连续不可用 {_worker.MAX_ATTEMPTS}+ 次仍在无限延期 —— 钱会一直冻着")

    conn = connect()
    try:
        rows = _store.outbox_rows(conn.cursor(), publish_command_id=cid)
    finally:
        conn.close()
    assert str(rows[0]["status"]) == "needs_review", rows[0]["status"]
    assert "通道持续不可用" in str(rows[0]["last_error"] or ""), rows[0]["last_error"]

    row = _command_row(cid)
    assert str(row["funding_state"]) == "frozen", (
        "队列转人工时顺手动了资金态 —— 队列失败与钱向是两件事")
    assert row["settled_at"] is None


def test_b3_12_the_reconciler_can_now_reach_it_and_refund(client, monkeypatch) -> None:
    """转人工之后收敛器⑦ **够得着**它,并做真正的退款裁决。

    这条把上一条的"停止无限延期"接到"钱真的回来了"上 ——
    否则「不再无限延期」只是换了个地方挂钱。
    """
    drain()
    drain_outbox()
    ctx = confirmed_command_local(client, "b3_refund")
    cid = ctx["publishCommandId"]
    _transport_down(monkeypatch)

    conn = connect()
    try:
        rows = _store.outbox_rows(conn.cursor(), publish_command_id=cid)
        oid = int(rows[0]["id"])
    finally:
        conn.close()
    for _ in range(_worker.MAX_ATTEMPTS + 3):
        _force_available(oid)
        if run(_worker.dispatch_pending(limit=50)).get("quarantined", 0):
            break

    monkeypatch.undo()
    before = _seed.wallet(_seed.TENANT_A)
    run(_worker.reconcile_tick(limit=200))

    row = _command_row(cid)
    assert str(row["funding_state"]) == "released", (
        f"通道彻底不通、零外调,钱却没退回来:{row['funding_state']}")
    assert row["settled_at"] is not None
    after = _seed.wallet(_seed.TENANT_A)
    assert after["paid_points"] > before["paid_points"], "只改了状态没动钱"


def test_b3_13_ceiling_branch_exists_structurally() -> None:
    """AST:``TransportNotConfigured`` 那条 except 里**有**上限分支。"""
    src = inspect.getsource(_worker.dispatch_pending)
    assert "MAX_ATTEMPTS" in src, (
        "TransportNotConfigured 的处置里没有任何上限 —— 那是无限延期挂钱")
    assert "_quarantine_row" in src, "达限之后没有转人工的出口"


# ── 本文件自用的小工具(不放 _chain:只有这一族要用)────────────────────
def confirmed_command_local(client, name: str) -> dict:
    from tests.defgeo_wob_publish_funding_2026_08_25._chain import confirmed_command
    return confirmed_command(client, name)


def _command_row(publish_command_id: str) -> dict:
    conn = connect()
    try:
        row = _store.get_command_any_tenant(
            conn.cursor(), publish_command_id=publish_command_id)
        assert row is not None
        return dict(row)
    finally:
        conn.close()


def _force_available(outbox_id: int) -> None:
    """把租约到期时间拨到当下 —— 判据不等 300 秒(等待不是被测对象)。"""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE {_store.OUTBOX_TABLE} SET available_at = NOW() - INTERVAL '1 second' "
            f"WHERE id = %s", (outbox_id,))
        conn.commit()
    finally:
        conn.close()
