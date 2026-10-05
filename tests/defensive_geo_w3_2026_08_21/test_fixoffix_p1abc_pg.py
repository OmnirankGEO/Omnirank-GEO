"""fix-of-fix P1-A / P1-B / P1-C 判据 —— 真 HTTP + 真 PG16。

P1-A 关闸前的只读幂等重放
    P0-1 那一版把关闸放在第一行,于是**已经确认过、钱已经冻住**的命令也一并
    被 403 挡住。对她来说那是"我明明已经确认过,现在连查都查不到" ——
    我们把一次已经发生的资金事件从她眼前抹掉了。
    本文件用**过渡测试**证明:临时开闸→真确认(真冻钱)→关闸→同键重放,
    拿到同一个 command,且资金六面**逐字节不变**。

P1-B 监测进度入口关闭 + 重开哨兵
P1-C 进度只数 planned 的格(端点关着也要修对 —— 重开那天不该再有人回来补)

🔴 本文件**不用**窗C 那个 ``client`` fixture:它是 ``_EntryClosed`` 子类,
   见到 ``PUBLISH_ENTRY_CLOSED`` 会 skip —— 而我这里的反向对照**要断言 403**,
   用那个 client 会把反向对照变成 skip(= 恒不失败 = 没有判别力)。
"""

from __future__ import annotations

import ast
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Iterator, Mapping

import pytest
from fastapi.testclient import TestClient

from services.defensive_geo.publish import store as _store
from tests.defensive_geo_w3_2026_08_21.conftest import connect
from tests.defensive_geo_w3_2026_08_21.test_p0_close_publish_entry_pg import _mk_client
from tests.defensive_geo_w3_2026_08_21.test_wp5_publish_http_pg import (
    BRAND_A, _ok, _ready_snapshot, _scenario,
)

ROOT = Path(__file__).resolve().parents[2]
PUBLISH_API = ROOT / "api" / "defensive_publish_api.py"


@pytest.fixture(scope="module")
def pub() -> Iterator[TestClient]:
    """裸 client —— 不吞 403,反向对照才有判别力。"""
    yield from _mk_client("api.defensive_publish_api")


@pytest.fixture(scope="module")
def mon() -> Iterator[TestClient]:
    yield from _mk_client("api.defensive_monitoring_api")


def _confirm(client: TestClient, snapshot_id: str, snap: Mapping[str, Any], *,
             key: str, expected_hash: str | None = None):
    return client.post(
        f"/api/defensive-geo/publish/decision-snapshots/{snapshot_id}/confirm",
        json={"expectedHash": expected_hash or snap["canonicalHash"],
              "expectedVersion": snap["snapshotVersion"]},
        headers={"Idempotency-Key": key, "X-Test-Identity": "a"},
    )


# ══════════════════════════════════════════════════════════════════════════
# 资金六面 —— 逐字节指纹(不是计数)
# ══════════════════════════════════════════════════════════════════════════
#: 钱与命令的全部落点。计数会漏掉"行数没变但**值**变了"(比如余额被改、
#: 冻结被 commit 成已扣)—— 那正是重放最危险的失败形态。
_SIX_SURFACES = (
    "user_wallets",
    "point_freezes",
    "point_transactions",
    _store.COMMAND_TABLE,
    _store.OUTBOX_TABLE,
    _store.SNAPSHOT_TABLE,
)


def _fingerprint() -> dict[str, str]:
    """六张表各取全量行 → 规范化 → sha256。行序无关(排序后再 hash)。"""
    conn = connect()
    try:
        cur = conn.cursor()
        out: dict[str, str] = {}
        for table in _SIX_SURFACES:
            cur.execute(f"SELECT * FROM {table}")          # noqa: S608 - 表名是本文件常量
            rows = sorted(
                json.dumps(dict(r), sort_keys=True, default=str)
                for r in (cur.fetchall() or [])
            )
            out[table] = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()
        conn.rollback()
    finally:
        conn.close()
    return out


def _diff(a: Mapping[str, str], b: Mapping[str, str]) -> list[str]:
    return sorted(t for t in a if a[t] != b[t])


# ══════════════════════════════════════════════════════════════════════════
# P1-A · 过渡测试:开闸确认 → 关闸重放
# ══════════════════════════════════════════════════════════════════════════
def test_p1a_consumed_replay_survives_the_closed_gate(pub: TestClient, monkeypatch) -> None:
    from api import defensive_publish_api as mod

    # ── ① 临时开闸,跑一次**真**确认(真冻钱)────────────────────────
    monkeypatch.setattr(mod, "_CUSTOMER_PUBLISH_ENTRY_OPEN", True)
    body = _scenario("p1a_replay")
    snap = _ready_snapshot(pub, body)["snapshot"]
    snapshot_id = snap["decisionSnapshotId"]

    before_confirm = _fingerprint()
    key = "p1a-" + uuid.uuid4().hex
    first = _ok(_confirm(pub, snapshot_id, snap, key=key))
    command_id = first["publishCommandId"]
    assert first["idempotentReplay"] is False, first
    after_confirm = _fingerprint()

    # 🔴 判据活性:这把指纹**必须会动**。不做这一步,下面"逐字节不变"
    #    与"我根本没在量"就分不开。
    moved = _diff(before_confirm, after_confirm)
    assert "point_freezes" in moved, (
        f"真确认之后 point_freezes 指纹没变 —— 这次确认根本没冻钱,"
        f"后面那条『重放零变』就是空的。变了的表:{moved}")
    assert _store.COMMAND_TABLE in moved, f"真确认没建 command:{moved}"

    # ── ② 关闸 ────────────────────────────────────────────────────────
    monkeypatch.setattr(mod, "_CUSTOMER_PUBLISH_ENTRY_OPEN", False)

    # ── ③ 同键 + 同 hash 重放:必须拿回同一个 command,且六面零变 ────
    baseline = _fingerprint()
    replay = _confirm(pub, snapshot_id, snap, key=key)
    assert replay.status_code == 200, (
        f"关闸后同键重放被挡住了({replay.status_code}) —— "
        f"已经确认过、钱已经冻住的那一次从她眼前消失了:{replay.text[:400]}")
    payload = replay.json()
    assert payload["idempotentReplay"] is True, payload
    assert payload["publishCommandId"] == command_id, (
        f"重放返回了**另一个** command:{payload['publishCommandId']} != {command_id}")

    changed = _diff(baseline, _fingerprint())
    assert changed == [], f"重放动了这些面:{changed}(必须逐字节不变)"


def test_p1a_reverse_different_key_is_still_closed(pub: TestClient, monkeypatch) -> None:
    """反向 ①:换 Idempotency-Key = **新命令**,而新命令正是关闸要挡的。"""
    from api import defensive_publish_api as mod

    monkeypatch.setattr(mod, "_CUSTOMER_PUBLISH_ENTRY_OPEN", True)
    body = _scenario("p1a_revkey")
    snap = _ready_snapshot(pub, body)["snapshot"]
    snapshot_id = snap["decisionSnapshotId"]
    _ok(_confirm(pub, snapshot_id, snap, key="p1a-orig-" + uuid.uuid4().hex))

    monkeypatch.setattr(mod, "_CUSTOMER_PUBLISH_ENTRY_OPEN", False)
    baseline = _fingerprint()
    resp = _confirm(pub, snapshot_id, snap, key="p1a-other-" + uuid.uuid4().hex)
    assert resp.status_code == 403, f"换了 key 仍被放行:{resp.text[:300]}"
    assert resp.json()["detail"]["code"] == "PUBLISH_ENTRY_CLOSED", resp.text
    assert _diff(baseline, _fingerprint()) == [], "被拒的请求动了资金面"


def test_p1a_reverse_different_hash_is_still_closed(pub: TestClient, monkeypatch) -> None:
    """反向 ②:换 expectedHash = 不是同一次确认。"""
    from api import defensive_publish_api as mod

    monkeypatch.setattr(mod, "_CUSTOMER_PUBLISH_ENTRY_OPEN", True)
    body = _scenario("p1a_revhash")
    snap = _ready_snapshot(pub, body)["snapshot"]
    snapshot_id = snap["decisionSnapshotId"]
    key = "p1a-h-" + uuid.uuid4().hex
    _ok(_confirm(pub, snapshot_id, snap, key=key))

    monkeypatch.setattr(mod, "_CUSTOMER_PUBLISH_ENTRY_OPEN", False)
    baseline = _fingerprint()
    resp = _confirm(pub, snapshot_id, snap, key=key, expected_hash="f" * 64)
    assert resp.status_code == 403, f"换了 hash 仍被放行:{resp.text[:300]}"
    assert resp.json()["detail"]["code"] == "PUBLISH_ENTRY_CLOSED", resp.text
    assert _diff(baseline, _fingerprint()) == [], "被拒的请求动了资金面"


def test_p1a_replay_helper_is_structurally_read_only() -> None:
    """结构锚:重放 helper 体内不许有写语句 / 资金调用。

    行为判据(六面逐字节不变)只覆盖它跑到的那条路径;这条覆盖**全部**分支,
    包括将来有人往里加的那一行。
    """
    tree = ast.parse(PUBLISH_API.read_text(encoding="utf-8"))
    fn = next((n for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_consumed_replay_or_none"), None)
    assert fn is not None, "找不到 _consumed_replay_or_none —— 结构锚打在空气上"

    sql_literals = [
        node.value for node in ast.walk(fn)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    body_sql = " ".join(sql_literals).upper()
    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        assert verb not in body_sql, f"重放 helper 里出现写语句 {verb!r}"

    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            assert not (isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "_funding"), "重放 helper 碰了资金"
            assert node.func.attr != "commit", "重放 helper 里有 commit()"
    # 反向对照:同一套检测跑在**真的会写**的 _do_confirm 上必须命中,
    # 否则上面那三条只是"检测器什么都认不出来"。
    doer = next((n for n in tree.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == "_do_confirm"), None)
    assert doer is not None
    doer_funding = [n for n in ast.walk(doer)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and isinstance(n.func.value, ast.Name) and n.func.value.id == "_funding"]
    assert doer_funding, "检测器在 _do_confirm 上都认不出资金调用 —— 它是瞎的"


def test_p1a_guard_still_precedes_every_funding_call() -> None:
    """P1-A 把关闸从"第一条语句"往后挪了一位(重放先跑),

    所以原来那条"必须是第一条语句"的锁要换成**更准确**的说法:
    关闸必须出现在**任何**资金调用之前。位置变了,承重的东西没变。
    """
    tree = ast.parse(PUBLISH_API.read_text(encoding="utf-8"))
    handler = next(n for n in tree.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == "confirm_decision_snapshot")
    guard_lines = [n.lineno for n in ast.walk(handler)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "_assert_customer_publish_entry_open"]
    money_lines = [n.lineno for n in ast.walk(handler)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "_do_confirm"]
    assert guard_lines, "confirm 里没有关闸了"
    assert money_lines, "confirm 里找不到资金腿调用 —— 分母塌了"
    assert max(guard_lines) < min(money_lines), (
        f"关闸({guard_lines})没有排在资金腿({money_lines})之前")


# ══════════════════════════════════════════════════════════════════════════
# P1-B · 监测进度入口关闭
# ══════════════════════════════════════════════════════════════════════════
def test_p1b_progress_is_open_and_typed(mon: TestClient) -> None:
    """[包F ② 2026-08-23] 进度入口**已重开**。

    一期这条判据的名字是 ``test_p1b_progress_is_typed_closed``,断言 403 +
    ``MONITORING_PROGRESS_CLOSED``。包F ① 把账本接上(生产写入方 0 → 5)后
    一期自己给的重开前置已满足,② 撤闸 —— 所以这条判据**换向**:
    现在必须**不是** 403-closed。

    🔴 为什么不是简单删掉:删掉等于这一格从"有判据"变成"零判据",
       而且不会有任何东西变红。换向之后,谁再悄悄加一道静默闸,这里立刻红。

    task 1 在本夹具里不存在 ⇒ 期望的是**存在性闸**给的 404,
    而不是关闸给的 403。两者都不是 200 —— 但含义完全不同,
    所以这里逐值判 code,不只判"非 200"。
    """
    resp = mon.get("/api/defensive-geo/monitoring/runs/1/progress",
                   params={"brandId": BRAND_A})
    assert resp.status_code != 500, f"当成故障了:{resp.text[:300]}"
    assert resp.status_code != 501, "501 是被明确禁止的解法"
    assert resp.status_code != 403, (
        f"进度入口又被关上了 —— 包F ② 已撤闸,账本有生产写入方:{resp.text[:300]}")
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "NOT_FOUND", detail
    assert detail["nextAction"]["label"], "404 也必须自带下一步"


def test_p1b_closed_sentinel_is_fully_retired() -> None:
    """反向锁:关闸的**残骸**一处都不许留。

    一期的重开哨兵 ``test_p1b_flag_is_closed`` 钉的是
    ``_MONITORING_PROGRESS_OPEN is False``;包F ② 之后那个常量不该存在了。
    留着一个恒 True 的开关 = 留一个"看起来还有闸"的死机关。

    🔴 三处一起判(缺一处就是"撤了一半"):
       ① 模块里没有那个常量、也没有那个断言函数;
       ② 错误码表里没有那个 code —— 再也不会被 raise 的 code 是死枚举;
       ③ 两个 api 模块里没有那个**字符串字面量**。

    ③ 用 AST 只看 ``ast.Constant`` 的字符串值,**不裸 grep**:
    本仓记过「引用裁决原文/写病历会让裸串结构锚判红」——
    本模块与 ``defensive_monitoring_api`` 的注释里都写着这个词
    (那是病历,不是闸),裸 grep 会把它们判红。
    """
    import ast as _ast
    import io as _io
    from pathlib import Path as _Path

    from api import defensive_geo_api as api_mod
    from api import defensive_monitoring_api as mon_mod

    assert not hasattr(mon_mod, "_MONITORING_PROGRESS_OPEN"), (
        "关闸常量还在 —— 撤闸没撤干净")
    assert not hasattr(mon_mod, "_assert_monitoring_progress_open"), (
        "关闸断言函数还在 —— 撤闸没撤干净")
    assert "MONITORING_PROGRESS_CLOSED" not in api_mod._ERROR_TABLE, (
        "错误码表里还留着一个再也不会被 raise 的 code")
    assert "MONITORING_PROGRESS_CLOSED" not in api_mod._ERROR_DEFAULTS, (
        "默认文案表里还留着那个 code")

    root = _Path(api_mod.__file__).resolve().parents[1]
    offenders = []
    for rel in ("api/defensive_geo_api.py", "api/defensive_monitoring_api.py"):
        tree = _ast.parse(_io.open(root / rel, encoding="utf-8", newline="").read())
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Constant) and                     node.value == "MONITORING_PROGRESS_CLOSED":
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        f"还有活的字符串字面量(不是注释):{offenders} —— "
        "有人在悄悄加回一道静默闸")


def test_p1b_existence_gate_is_permanent(mon: TestClient) -> None:
    """🔴 一期 MUT-18 补的那条**临时开闸**判据,转常驻。

    一期原文:「P1-B 把进度端点关了 ⇒ test_10/11 走重开哨兵 skip。
    撕锁时发现:此时把存在性闸整段摘掉,**一条判据都不会红** ——
    那等于 P1-3 在这次关闭里被悄悄退役了」。当时的解法是
    ``monkeypatch.setattr(mod, "_MONITORING_PROGRESS_OPEN", True)`` 临时开闸。

    包F ② 真的开了闸 ⇒ 不再需要 monkeypatch。去掉它是**加强**不是放松:
    临时开闸打的是一个被 patch 出来的状态,现在打的是真实生产状态。
    """
    resp = mon.get("/api/defensive-geo/monitoring/runs/-1/progress",
                   params={"brandId": BRAND_A})
    assert resp.status_code != 200, f"不存在的 task 又开始编造零进度了:{resp.text[:300]}"
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["code"] == "NOT_FOUND", resp.text


def test_p1b_sibling_endpoints_are_untouched(mon: TestClient) -> None:
    """反向对照:只关 progress 一条,admission / comparability 不动。

    没有这条,"关闭"和"把整个 router 关了"分不开。
    """
    resp = mon.get("/api/defensive-geo/monitoring/reports/nope/comparability",
                   params={"brandId": BRAND_A})
    assert resp.status_code != 403 or \
        resp.json().get("detail", {}).get("code") != "MONITORING_PROGRESS_CLOSED", (
            f"comparability 被关闭闸误伤:{resp.text[:300]}")


# ══════════════════════════════════════════════════════════════════════════
# P1-C · 进度只数 planned 的格
# ══════════════════════════════════════════════════════════════════════════
def _seed_progress(task_id: int, *, planned: int, unplanned: int,
                   terminal_on: int) -> None:
    """造 task + 格 + attempt 账本。``terminal_on`` = 前 N 个 planned 格标记完成。"""
    conn = connect()
    try:
        cur = conn.cursor()
        # 🔴 先清本 task 的残留:撕锁 runner 会把整套判据跑十几遍,
        #    不清的话第 2 遍就撞 uq_monitoring_run_cells_plan,
        #    而那种红与被测代码无关(会把"变异没杀死"混成一团)。
        cur.execute(
            "DELETE FROM public.defgeo_monitoring_attempts WHERE plan_cell_id IN "
            "(SELECT plan_hash FROM public.monitoring_run_cells WHERE task_id=%s)",
            (task_id,))
        cur.execute("DELETE FROM public.monitoring_run_cells WHERE task_id=%s", (task_id,))
        cur.execute("INSERT INTO brands (id, name, owner_user_id) VALUES (%s,%s,%s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (BRAND_A, "p1c 品牌", 9301))
        cur.execute("INSERT INTO public.monitoring_tasks (id, client_id, brand_id) "
                    "VALUES (%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                    (task_id, f"p1c-{task_id}", BRAND_A))
        idx = 0
        for is_planned, count in ((True, planned), (False, unplanned)):
            for _ in range(count):
                idx += 1
                plan_hash = f"{task_id:06d}".ljust(8, "0") + f"{idx:056d}"
                # 🔴 真 schema 的 chk_monitoring_run_cells_plan_shape:
                #    ``is_planned OR (NOT is_planned AND state='unavailable')``。
                #    也就是说未计划的格**合法存在**、而且 plan_hash 是 NOT NULL 列
                #    ⇒ 它一定带着一个 plan_hash 进 DISTINCT。P1-C 说的稀释是真的,
                #    不是理论值。(手写 schema 多半没有这条 CHECK,于是夹具
                #    "造得出"的数据生产根本插不进去 —— 用生产 dump 的价值就在这。)
                cur.execute(
                    "INSERT INTO public.monitoring_run_cells ("
                    " task_id, brand_id, keyword_id, keyword_source, keyword_snapshot,"
                    " question_snapshot, target_brand_snapshot, platform, is_planned,"
                    " state, entitlement_snapshot, order_snapshot,"
                    " fulfillment_credential, plan_hash"
                    ") VALUES (%s,%s,%s,'contract','k','q','b','kimi',%s,%s,"
                    " '{}'::jsonb,'{}'::jsonb, gen_random_uuid(), %s)",
                    (task_id, BRAND_A, idx, is_planned,
                     "queued" if is_planned else "unavailable", plan_hash))
                if is_planned and idx <= terminal_on:
                    cur.execute(
                        "INSERT INTO public.defgeo_monitoring_attempts ("
                        " attempt_id, plan_cell_id, attempt_ordinal, run_authority_id,"
                        " tenant_owner_user_id, brand_id, actual_provider, actual_model,"
                        " actual_surface, actual_search_mode, request_hash,"
                        " terminal_state, terminal_at, ledger_version"
                        ") VALUES (%s,%s,1,'ra',9301,%s,'kimi','m','s','sm','rh',"
                        " 'answered', NOW(), 'v1')",
                        (uuid.uuid4().hex.ljust(64, "0")[:64], plan_hash, BRAND_A))
        conn.commit()
    finally:
        conn.close()


def test_p1c_unplanned_cells_do_not_dilute_progress(mon: TestClient) -> None:
    """1 格 planned(已完成)+ 3 格 unplanned ⇒ **100%**,不是 25%。

    端点关着也要修对:重开那天不该再有人回来补这一刀。
    """
    _seed_progress(770001, planned=1, unplanned=3, terminal_on=1)

    resp = mon.get("/api/defensive-geo/monitoring/runs/770001/progress",
                   params={"brandId": BRAND_A})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["plannedCells"] == 1, (
        f"分母里混进了未计划的格:plannedCells={body['plannedCells']}(应为 1)")
    assert body["terminalCells"] == 1, body
    assert body["progressPct"] == 100.0, (
        f"进度被未计划的格稀释成 {body['progressPct']}% —— "
        "她的计划其实已经跑完了,而我们告诉她还差一大截")


def test_p1c_reverse_all_planned_is_unchanged(mon: TestClient) -> None:
    """反向:全是 planned 时,加不加过滤结果一样(证明这刀没伤到正常场景)。

    [包F ② 2026-08-23] 原来这里要 ``monkeypatch`` 临时开闸;闸已撤除
    (见 ``api/defensive_monitoring_api`` 顶部说明),所以直接打真实状态。
    去掉 monkeypatch 是**加强**:临时开闸打的是一个 patch 出来的状态。
    """
    _seed_progress(770002, planned=4, unplanned=0, terminal_on=1)

    resp = mon.get("/api/defensive-geo/monitoring/runs/770002/progress",
                   params={"brandId": BRAND_A})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["plannedCells"] == 4, body
    assert body["terminalCells"] == 1, body
    assert body["progressPct"] == 25.0, body
