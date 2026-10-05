"""B-7 · **Review 裁定要在本轮闭掉的五条审计缺口**(§7.4-4)。

═══════════════════════════════════════════════════════════════════════
这一族与 B-6 的区别:B-6 是审计方**挑变异**逼出来的,B-7 是我自己在交付文
里如实列出「知道有洞、没闭」的那五条 —— Review 2026-08-26 裁定全部本轮闭。
═══════════════════════════════════════════════════════════════════════

五条各自的形态不同,别混:

  ① 收敛器⑦「权威零接单」是**双保险**(``external_start_at IS NULL`` +
     ``provider_call_count = 0``)。E 包只覆盖了前一半 —— 后一半守的是
     「计数加了但 marker 没落」那个崩溃窗口,把它删掉不会有任何判据变红。
     → 行为判据 + 活性对照(同形状零调用必须真退)。

  ② 平台腿那两道 ``quarantined`` 收口闸 —— **不在本文件**。
     它们的真链臂长在 E 包(本包的平台腿行为分母 = 0),按 Review 裁定
     写进 ``tests/defensive_geo_pkge_2026_08_24/test_r2_platform_leg_pg.py``。

  ③ 迁移 051 的唯一索引在**已装载的库**上不可证伪:index-guard 第一分支
     命中 ⇒ ``CREATE UNIQUE INDEX`` 那一行根本不执行。要真验只能开**冷库**:
     先在别的表上占掉同名索引 → 跑 051 → 断言 RAISE 命中。
     (本仓记过:``CREATE INDEX IF NOT EXISTS`` 按关系名判存、不绑表、
      不限 relkind —— 名字被占着时唯一性从上线起就不存在。)

  ④ ``publish_worker._settle_row`` 整条路径零判据。它有三条出口
     (达限收 ``needs_review`` / 未达限 ``defer`` / DB 异常吞掉),
     还有一条**边界**(第 N 次就是终局,不是第 N+1 次)和一条 fencing。

  ⑤ ``publish_wemedia`` 那条入口没有 strict 反查 —— **今天不是活洞**
     (defgeo 链不走它),所以按 Review 裁定**只补枚举锁、不改行为体**:
     冻结豁免集 + 钉死大小,第三条入口出现时当场红。
     (本仓记过:同一目标的语法形态要一次枚举全,别等第三个补丁。)
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
from pathlib import Path

import psycopg2
import pytest

from services.defensive_geo.publish import provider_transport as _transport
from services.defensive_geo.publish import publish_worker as _worker
from services.defensive_geo.publish import reconciler as _recon
from services.defensive_geo.publish import store as _store
from services.meijiehezi import client as _mhz

from tests.defgeo_wob_publish_funding_2026_08_25 import _seed
from tests.defgeo_wob_publish_funding_2026_08_25._chain import (   # noqa: F401
    base, client, confirmed_command, drain, drain_outbox, run,
)
from tests.defgeo_wob_publish_funding_2026_08_25.conftest import (
    EXACT_THROWAWAY_URL, connect,
)

ROOT = Path(__file__).resolve().parents[2]

MIGRATION_051 = ROOT / "db" / "migration_051_defgeo_publish_settlement_guards_2026_08_25.sql"
INDEX_NAME = "uq_defgeo_pcmd_provider_order_ref"


# ══════════════════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════════════════
def _command_row(publish_command_id: str) -> dict:
    conn = connect()
    try:
        row = _store.get_command_any_tenant(
            conn.cursor(), publish_command_id=publish_command_id)
        assert row is not None
        return dict(row)
    finally:
        conn.close()


def _sql(conn_or_none, statement: str, params: tuple = ()) -> None:
    conn = conn_or_none or connect()
    try:
        cur = conn.cursor()
        cur.execute(statement, params)
        conn.commit()
    finally:
        if conn_or_none is None:
            conn.close()


def _strip_sql_comments(src: str) -> str:
    """判**代码**不判散文 —— 注释里写着谓词不等于查询里有谓词。

    本仓记过:引用裁决原文 / 写病历会让裸串结构锚判红;反过来同样成立,
    注释里出现谓词会让"我有守"变成假绿。
    """
    return "\n".join(
        ln for ln in src.splitlines() if not ln.lstrip().startswith("--")
    )


def _outbox_to_needs_review(client, name: str, monkeypatch) -> str:
    """把一条真确认出来的命令,经**真通道不可用**推到 ⑦ 的候选集里。

    走的是 B-3 那条真路径(达 ``MAX_ATTEMPTS`` ⇒ ``needs_review``),
    不是直接 UPDATE 队列 —— 夹具替被测代码把行摆好,判据就恒绿了。
    """
    drain()
    drain_outbox()
    ctx = confirmed_command(client, name)
    cid = ctx["publishCommandId"]

    # 🔴 与 B-3 同一个注入点(``_transport.resolve``)—— 换个地方 patch
    #    就可能绕开真正的那条 except 分支,判据也就不在验它了。
    def _boom(*_a, **_kw):
        raise _transport.TransportNotConfigured("b7:注入 —— 外发通道未配置")

    monkeypatch.setattr(_transport, "resolve", _boom)

    conn = connect()
    try:
        rows = _store.outbox_rows(conn.cursor(), publish_command_id=cid)
        oid = int(rows[0]["id"])
    finally:
        conn.close()

    for _ in range(_worker.MAX_ATTEMPTS + 3):
        _sql(None, f"UPDATE {_store.OUTBOX_TABLE} "
                   f"SET available_at = NOW() - INTERVAL '1 second' WHERE id = %s",
             (oid,))
        if run(_worker.dispatch_pending(limit=50)).get("quarantined", 0):
            break

    conn = connect()
    try:
        rows = _store.outbox_rows(conn.cursor(), publish_command_id=cid)
    finally:
        conn.close()
    assert str(rows[0]["status"]) == "needs_review", (
        f"前提没造出来:outbox 还是 {rows[0]['status']!r} —— 这条判据没在验任何东西")
    return cid


# ══════════════════════════════════════════════════════════════════════════
# ① 收敛器⑦「权威零接单」双保险的**后一半**
# ══════════════════════════════════════════════════════════════════════════
def test_b7_01_a_command_with_provider_calls_is_never_auto_refunded(
        client, monkeypatch) -> None:
    """🔴 ``provider_call_count > 0`` ⇒ ⑦ **够不着它**,一分钱不退。

    守的是这个崩溃窗口:``dispatch_once`` 已经把调用计数加上去了,
    ``external_start_at`` 那一句还没提交就崩了。此时「调过没有」在两列上
    **不一致** —— 权威零接单的证据不成立,自动退款就是在赌。

    「删修复即红」:把 ⑦ 查询里的 ``AND c.provider_call_count = 0`` 拿掉,
    这条命令会被自动退款,断言当场红。
    活性对照 = ``test_b7_02``(同一形状、计数为 0 ⇒ 必须真退)。
    """
    cid = _outbox_to_needs_review(client, "b7_pcc", monkeypatch)
    monkeypatch.undo()

    # 只动**这一列**:external_start_at 仍是 NULL(两列不一致才是被守的形状)。
    _sql(None, f"UPDATE {_store.COMMAND_TABLE} SET provider_call_count = 1 "
               f"WHERE publish_command_id = %s", (cid,))
    row_before = _command_row(cid)
    assert row_before["external_start_at"] is None, "夹具把 marker 也写了 —— 造错了形状"
    assert int(row_before["provider_call_count"]) == 1
    assert str(row_before["funding_state"]) == "frozen"
    wallet_before = _seed.wallet(_seed.TENANT_A)

    actions = run(_worker.reconcile_tick(limit=200))

    mine = [a for a in actions["items"] if a["commandId"] == cid]
    assert not [a for a in mine if a["kind"] == "release_never_dispatched"], (
        f"调用计数不为 0 却被当成「权威零接单」退款了:{mine} —— "
        "计数与 marker 不一致时不猜,留给 Z-1 人工队列")
    row = _command_row(cid)
    assert str(row["funding_state"]) == "frozen", (
        f"钱向被动了:{row['funding_state']} —— 双保险的后一半没有在守")
    assert row["settled_at"] is None
    assert _seed.wallet(_seed.TENANT_A) == wallet_before, "动了客户钱包"


def test_b7_02_the_same_shape_with_zero_calls_is_really_refunded(
        client, monkeypatch) -> None:
    """🟢 上一条的**活性对照**:唯一差别是 ``provider_call_count = 0``。

    对照不绿 ⇒ 上一条的"没退款"可能只是因为夹具根本没进候选集
    (那样它一条东西都没在验)。两条必须成对读。
    """
    cid = _outbox_to_needs_review(client, "b7_pcc0", monkeypatch)
    monkeypatch.undo()

    row_before = _command_row(cid)
    assert int(row_before["provider_call_count"] or 0) == 0, row_before
    before = _seed.wallet(_seed.TENANT_A)

    actions = run(_worker.reconcile_tick(limit=200))

    mine = [a for a in actions["items"] if a["commandId"] == cid]
    assert any(a["kind"] == "release_never_dispatched" for a in mine), (
        f"零调用、零 marker 的命令没被退款:{mine} —— 对照臂不成立,"
        "上一条的「没退款」也就证明不了任何事")
    row = _command_row(cid)
    assert str(row["funding_state"]) == "released", row["funding_state"]
    assert row["settled_at"] is not None
    assert _seed.wallet(_seed.TENANT_A)["paid_points"] > before["paid_points"], (
        "只改了状态没动钱")


def test_b7_03_both_halves_of_the_zero_dispatch_predicate_are_in_the_query() -> None:
    """结构地板:两个谓词**都**在 ⑦ 的查询体里(判代码不判注释)。

    行为判据只能证明"删掉某一半会红";这一条钉的是**枚举** ——
    以后再加第三个"零接单证据"列时,这里会提醒它也要被列进来。
    """
    src = _strip_sql_comments(
        textwrap.dedent(inspect.getsource(_recon._release_never_dispatched)))
    for predicate in ("external_start_at IS NULL", "provider_call_count = 0"):
        assert predicate in src, (
            f"⑦ 的候选集里没有 {predicate!r} —— 「权威零接单」少了一半证据")


# ══════════════════════════════════════════════════════════════════════════
# ③ 迁移 051 唯一索引:**冷库**才证得了的那一格
# ══════════════════════════════════════════════════════════════════════════
def _cold_db(name: str):
    """开一把真·冷库(建完即用,用完必删)。库名沿用本包安全栓三词。"""
    assert "defgeo" in name and "wob" in name and "test" in name, name
    admin_url = EXACT_THROWAWAY_URL.rsplit("/", 1)[0] + "/postgres"
    admin = psycopg2.connect(admin_url)
    admin.autocommit = True                    # 🔴 CREATE DATABASE 不能在事务里
    try:
        cur = admin.cursor()
        cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        admin.close()
    return EXACT_THROWAWAY_URL.rsplit("/", 1)[0] + "/" + name


def _drop_cold_db(name: str) -> None:
    admin_url = EXACT_THROWAWAY_URL.rsplit("/", 1)[0] + "/postgres"
    admin = psycopg2.connect(admin_url)
    admin.autocommit = True
    try:
        cur = admin.cursor()
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
        cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        admin.close()


_MINIMAL_HOST = """
CREATE TABLE public.defgeo_publish_commands (
    publish_command_id TEXT PRIMARY KEY,
    provider_order_ref TEXT
);
"""


@pytest.fixture()
def cold_db():
    made: list[str] = []

    def _make(name: str, *, decoy: bool):
        url = _cold_db(name)
        made.append(name)
        conn = psycopg2.connect(url)
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute(_MINIMAL_HOST)
            if decoy:
                # 🔴 同名索引长在**别的表**上 —— 这正是 `IF NOT EXISTS` 判存
                #    会静默跳过、而唯一性从上线起不存在的那一格。
                cur.execute("CREATE TABLE public.b7_decoy_host (id INTEGER)")
                cur.execute(
                    f"CREATE UNIQUE INDEX {INDEX_NAME} ON public.b7_decoy_host (id)")
        finally:
            conn.close()
        return url

    try:
        yield _make
    finally:
        for name in made:
            _drop_cold_db(name)


def _run_051(url: str) -> None:
    conn = psycopg2.connect(url)
    conn.autocommit = True
    try:
        conn.cursor().execute(MIGRATION_051.read_text(encoding="utf-8"))
    finally:
        conn.close()


def test_b7_10_index_guard_raises_when_the_name_is_taken_by_another_table(
        cold_db) -> None:
    """🔴 同名索引占在别的表上 ⇒ 051 必须 **RAISE**,不许静默跳过。

    这是 051 里 index-guard 中间那条分支的**唯一**证明方式:
    在已装载的库上第一分支就命中了,那条 RAISE 一行都执行不到。

    「删修复即红」:把 ``ELSIF ... RAISE EXCEPTION`` 那一段换回裸
    ``CREATE UNIQUE INDEX IF NOT EXISTS``,051 会安静跑过 ——
    然后 ``provider_order_ref`` 的唯一性从上线起就不存在,一个上游单号
    能结算两笔冻结,而没有任何东西会告诉你。
    """
    url = cold_db("geo_defgeo_wob_test_b7cold_decoy", decoy=True)

    with pytest.raises(psycopg2.errors.DuplicateObject) as exc:
        _run_051(url)

    msg = str(exc.value)
    assert "index-guard" in msg, msg
    assert INDEX_NAME in msg, msg
    assert "b7_decoy_host" in msg, (
        f"RAISE 没有把**实际宿主**说出来:{msg} —— 排障时得知道名字被谁占了")


def test_b7_11_a_cold_database_gets_a_genuine_unique_partial_index(
        cold_db) -> None:
    """🟢 上一条的活性对照:没有占名者时,051 真的建出**唯一 + NULL 豁免**的索引。

    没有这一条,上一条的 RAISE 可能只是"051 在冷库上根本跑不起来"。
    顺带把索引的**定义**量出来 —— 名字对、定义不对等于这条守卫从没守过。
    """
    url = cold_db("geo_defgeo_wob_test_b7cold_clean", decoy=False)

    _run_051(url)

    conn = psycopg2.connect(url)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT pg_get_indexdef(c.oid) FROM pg_class c "
            "  JOIN pg_index i ON i.indexrelid = c.oid "
            " WHERE c.relname = %s "
            "   AND i.indrelid = to_regclass('public.defgeo_publish_commands')",
            (INDEX_NAME,))
        row = cur.fetchone()
    finally:
        conn.close()

    assert row is not None, "冷库上 051 没有建出唯一索引 —— 对照臂不成立"
    idxdef = row[0] if not isinstance(row, dict) else list(row.values())[0]
    assert "UNIQUE INDEX" in idxdef, idxdef
    assert "provider_order_ref IS NOT NULL" in idxdef, (
        f"缺 NULL 豁免谓词:{idxdef} —— 「还没外调过」的命令会互相撞唯一")


# ══════════════════════════════════════════════════════════════════════════
# ④ publish_worker._settle_row 三条出口 + 边界 + fencing
# ══════════════════════════════════════════════════════════════════════════
def _fresh_outbox(client, name: str) -> tuple[str, int, str]:
    """真确认一条命令,并把它的 outbox 领出来(拿到真 claim_token)。"""
    drain()
    drain_outbox()
    ctx = confirmed_command(client, name)
    cid = ctx["publishCommandId"]
    conn = connect()
    try:
        cur = conn.cursor()
        claimed = _store.claim_outbox(cur, claim_token=f"b7-{name}", limit=50)
        conn.commit()
    finally:
        conn.close()
    mine = [r for r in claimed if str(r["publish_command_id"]) == cid]
    assert mine, f"没领到自己的那一条:{claimed}"
    return cid, int(mine[0]["id"]), str(mine[0]["claim_token"])


def _outbox_row(outbox_id: int) -> dict:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM {_store.OUTBOX_TABLE} WHERE id = %s", (outbox_id,))
        return dict(cur.fetchone())
    finally:
        conn.close()


def test_b7_20_settle_row_defers_below_the_ceiling(client) -> None:
    """未达限 ⇒ 放回队列重试(``pending``),不是终局。"""
    _cid, oid, token = _fresh_outbox(client, "b7_defer")

    _worker._settle_row(oid, token, _worker.MAX_ATTEMPTS - 1, "b7:未达限")

    row = _outbox_row(oid)
    assert str(row["status"]) == "pending", (
        f"次数没耗尽就被收成 {row['status']!r} —— 通道恢复后这条永远发不出去")
    assert "b7:未达限" in str(row["last_error"] or ""), row["last_error"]


def test_b7_21_the_ceiling_is_inclusive_not_off_by_one(client) -> None:
    """🔴 **边界**:第 ``MAX_ATTEMPTS`` 次就是终局,不是第 N+1 次。

    「删修复即红」:把 ``attempt_count >= MAX_ATTEMPTS`` 改成 ``>``,
    这条会停在 ``pending`` —— 也就是多挂一轮钱。
    """
    _cid, oid, token = _fresh_outbox(client, "b7_ceiling")

    _worker._settle_row(oid, token, _worker.MAX_ATTEMPTS, "b7:恰好达限")

    row = _outbox_row(oid)
    assert str(row["status"]) == "needs_review", (
        f"恰好达限却没转人工:{row['status']!r} —— 上限差一位就是无限延期")


# ══════════════════════════════════════════════════════════════════════════
# [Review 三裁 ② 2026-08-26] b7_21 拆轴 —— 终止性 / 去向 各一条第二落点
# ══════════════════════════════════════════════════════════════════════════
# 🔴 为什么拆:外选 EXTB7-04(``terminal = False``)与 EXTB7-05(耗尽单标成
#    成功态)打的是**两件不同的事**,却都只红 ``test_b7_21`` 一条 ——
#    那一条 ``status == "needs_review"`` 把「不再重试」和「去向正确」压成了
#    一次字符串比较。单点判据被删或被重构打歪,两发同时失守。
#
#    拆完之后两条各走**不同观测面**,不是把同一个字符串再断言一遍:
#      · 21b 走**派发循环真正用的那条领取查询**;
#      · 21c 走**状态域**(库 CHECK ↔ 代码常量对账 + 钉大小)。


def test_b7_21b_the_ceiling_actually_ends_the_retry_loop(client) -> None:
    """🔴 终止性(第二落点):达限之后,**派发循环不许再看见这一条**。

    ``b7_21`` 比的是状态字符串。字符串对不对是一回事,**领取谓词还捞不捞得到它**
    是另一回事 —— 后者才是「多挂一轮钱」真正的成因。所以这条不比字符串,
    直接拿生产的 ``_store.claim_outbox``(派发循环用的就是它)再领一次。

    ``available_at`` 先强行拨回一小时:否则「暂时还没到可用时间」会**冒充**
    「已经终局」,让这条判据假绿。
    """
    _cid, oid, token = _fresh_outbox(client, "b7_ceiling_loop")

    _worker._settle_row(oid, token, _worker.MAX_ATTEMPTS, "b7:恰好达限")

    # 把可用时间拨到过去 —— 排除「只是还没到点」这个假终局。
    _sql(None, f"UPDATE {_store.OUTBOX_TABLE} "
               "SET available_at = NOW() - INTERVAL '1 hour' WHERE id = %s", (oid,))

    conn = connect()
    try:
        cur = conn.cursor()
        again = _store.claim_outbox(cur, claim_token="b7-21b-probe", limit=50)
        conn.commit()
    finally:
        conn.close()

    reclaimed = [int(r["id"]) for r in again]
    assert oid not in reclaimed, (
        f"达限之后派发循环又把 outbox={oid} 领走了(现状 "
        f"{_outbox_row(oid)['status']!r})—— 这就是无限重试:"
        "每多领一轮就多冻一轮钱,而没有任何人在看它")


def test_b7_21c_an_exhausted_row_lands_in_the_one_terminal_a_human_reads(client) -> None:
    """🔴 去向(第二落点):终局有三个,**只有一个是有人会看的**。

    ``dispatched`` / ``failed`` 同样是终局、同样不会被再领取 —— 所以
    ``21b`` 对它们是绿的。但耗尽的单落在那两个里面意味着:没人复核、
    冻着的钱也没人退。去向这一维必须单独有判据。

    顺带把**状态域**钉住:从库的 CHECK 约束里机械读出允许值,与代码常量
    对账并钉大小。往域里加一个"看起来像成功"的新状态(EXTB7-05 想干的正是
    这件事)会当场把这条打红。
    """
    _cid, oid, token = _fresh_outbox(client, "b7_ceiling_dest")

    # ① 状态域:库 CHECK ↔ 代码常量,逐个对账 + 钉大小。
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
            "WHERE conname = 'chk_defgeo_pout_status'")
        crow = cur.fetchone()
    finally:
        conn.close()
    assert crow is not None, "状态域的 CHECK 约束不见了 —— 域没人守,下面的断言也就没有边界"
    domain = set(re.findall(r"'([a-z_]+)'::character varying", str(crow["d"])))
    assert domain == set(_store.OUTBOX_STATUSES), (
        f"库的状态域与代码常量对不上:库={sorted(domain)} "
        f"代码={sorted(_store.OUTBOX_STATUSES)} —— 两边各讲各的故事")
    assert len(domain) == 5, f"状态域大小变了({sorted(domain)})—— 新增状态必须先过这条判据"

    # ② 耗尽单的去向:三个终局里只有 needs_review 是有人会看的那一个。
    _worker._settle_row(oid, token, _worker.MAX_ATTEMPTS, "b7:恰好达限")

    row = _outbox_row(oid)
    status = str(row["status"])
    unread_terminals = set(_store.OUTBOX_TERMINAL_STATUSES) - {"needs_review"}
    assert status not in unread_terminals, (
        f"耗尽单落进了**没人看的终局** {status!r}(另两个终局是 "
        f"{sorted(unread_terminals)})—— 不会被复核,冻着的钱也不会被退")
    assert status == "needs_review", (
        f"耗尽单的去向是 {status!r},不是人工复核队列 —— "
        "钱冻在那里,而没有任何人被通知去看它")
    assert (row["last_error"] or "").strip(), (
        "转了人工却没留失败原因 —— 人到了运维面也不知道该看什么")


def test_b7_22_settle_row_never_touches_the_command_funding_state(client) -> None:
    """队列失败与钱向是**两件事** —— 收队列不许顺手动 command 的资金态。

    在这里改钱会把「没调过」和「调过但没记上」压成一件事,
    而只有 ``dispatch_once`` 知道到底是哪一件。
    """
    cid, oid, token = _fresh_outbox(client, "b7_notouch")
    before = _command_row(cid)
    wallet_before = _seed.wallet(_seed.TENANT_A)

    _worker._settle_row(oid, token, _worker.MAX_ATTEMPTS, "b7:收尾")

    after = _command_row(cid)
    assert str(after["funding_state"]) == str(before["funding_state"]), (
        f"收队列动了钱向:{before['funding_state']} → {after['funding_state']}")
    assert after["settled_at"] == before["settled_at"]
    assert after["external_start_at"] == before["external_start_at"]
    assert _seed.wallet(_seed.TENANT_A) == wallet_before


def test_b7_23_settle_row_carries_the_fencing_token(client) -> None:
    """🔴 fencing:拿**别人**的 token 来收尾,一行都改不动。

    这是 B-2 那道 fencing 在 ``_settle_row`` 这条出口上的落实 ——
    ``dispatch_once`` 的出口有判据,异常收尾这条没有。
    """
    _cid, oid, _token = _fresh_outbox(client, "b7_fence")
    before = _outbox_row(oid)

    _worker._settle_row(oid, "b7-not-my-lease", _worker.MAX_ATTEMPTS, "b7:过期租约")

    after = _outbox_row(oid)
    assert str(after["status"]) == str(before["status"]), (
        f"过期租约把状态盖掉了:{before['status']} → {after['status']}")
    assert after["claim_token"] == before["claim_token"], "租约凭据被别人改了"


def test_b7_24_settle_row_swallows_db_errors_instead_of_raising(
        client, monkeypatch) -> None:
    """第三条出口:收尾本身失败时**吞掉并告警**,不把异常抛回派发循环。

    抛出去的话,一条收不了尾的队列行会让整轮 ``dispatch_pending`` 中断 ——
    后面排队的命令一条都发不出去(单条故障升级成全链停摆)。
    """
    _cid, oid, token = _fresh_outbox(client, "b7_swallow")

    def _boom(*_a, **_kw):
        raise RuntimeError("b7:收尾时数据库炸了")

    monkeypatch.setattr(_store, "settle_outbox", _boom)
    monkeypatch.setattr(_store, "defer_outbox", _boom)

    _worker._settle_row(oid, token, _worker.MAX_ATTEMPTS, "b7:异常出口")  # 不许抛

    monkeypatch.undo()
    assert str(_outbox_row(oid)["status"]) == "claimed", (
        "收尾失败却把状态改了 —— 那说明异常不是在收尾这一步抛的,这条判据没打中")


# ══════════════════════════════════════════════════════════════════════════
# ⑤ publish_wemedia:只补枚举锁,**不改行为体**
# ══════════════════════════════════════════════════════════════════════════
#: 🔴 **冻结豁免集**:今天允许不带 strict 反查的入口,连同它的调用点条数。
#:    ``publish_wemedia`` 走的是自媒体单据线,defgeo 发布链**不调它**
#:    (调用点见 ``services/defensive_geo/publish/provider_transport.py``),
#:    所以它不是活洞;但它与 ``publish`` 是同一目标的两种语法形态。
#:    本仓记过:同一目标的语法形态要**一次枚举全**,别等第三个补丁。
#:    → 冻结在这里,大小钉死;真要给它接 strict 是**行为改动**,归 Owner 拍板。
_STRICT_EXEMPT_ENTRYPOINTS: dict[str, int] = {
    "publish_wemedia": 2,
}

#: 反查辅助函数的**全集**。多出第三个 ⇒ 轴变宽了,这把锁的分母当场失效。
_REVERSE_LOOKUP_HELPERS = frozenset({
    "_lookup_order_sn_by_signature",
    "_lookup_order_sns_for_batch",
})


def _reverse_lookup_call_sites() -> list[tuple[str, str, int, bool]]:
    """机械枚举:``(入口函数, 辅助函数, 行号, 是否传了 strict_unique)``。

    走 AST 不走字符串 —— 注释/docstring 里出现函数名不算调用点
    (本仓记过:census 用裸符号名会把 import 当调用)。
    """
    src = Path(_mhz.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    seen: dict[int, tuple[str, str, int, bool]] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in _REVERSE_LOOKUP_HELPERS:
                continue
            strict = any(kw.arg == "strict_unique" for kw in node.keywords)
            # 嵌套函数会被外层 walk 重复访问 —— 按行号去重,并保留最内层的入口名。
            seen[node.lineno] = (fn.name, node.func.attr, node.lineno, strict)
    return sorted(seen.values(), key=lambda r: r[2])


def test_b7_30_reverse_lookup_helpers_are_exactly_two() -> None:
    """轴的分母:反查辅助函数恰好两个。多一个 ⇒ 下面那把枚举锁就漏了。"""
    defined = {
        node.name for node in ast.walk(ast.parse(
            Path(_mhz.__file__).read_text(encoding="utf-8")))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("_lookup_order_sn")
    }
    assert defined == set(_REVERSE_LOOKUP_HELPERS), (
        f"反查辅助函数的全集变了:{sorted(defined)} —— "
        f"枚举锁的分母跟着变,先把新的那个归类再改这里")


def test_b7_31_every_reverse_lookup_call_site_is_classified() -> None:
    """🔴 **枚举锁**:每个调用点要么传 strict,要么在冻结豁免集里。

    「删修复即红」有两个方向:
      · 给 ``publish`` 的调用点去掉 ``strict_unique`` ⇒ 它掉进"未分类"当场红;
      · 新增第三条入口(比如 ``publish_short_video`` 接上反查)⇒ 同样当场红。
    这正是本仓「同一目标的语法形态一次枚举全」要的形态:
    **不改行为体**,只让下一个补丁写的时候被迫看见这里。
    """
    sites = _reverse_lookup_call_sites()
    assert sites, "一个调用点都没枚举到 —— census 坏了,不是代码干净了"

    exempt_counts: dict[str, int] = {}
    unclassified: list[tuple[str, str, int, bool]] = []
    for entry, helper, lineno, strict in sites:
        if strict:
            continue
        if entry in _STRICT_EXEMPT_ENTRYPOINTS:
            exempt_counts[entry] = exempt_counts.get(entry, 0) + 1
        else:
            unclassified.append((entry, helper, lineno, strict))

    assert not unclassified, (
        f"有调用点既不传 strict_unique、也不在冻结豁免集里:{unclassified} —— "
        "要么接上 strict(行为改动,归 Owner),要么显式写进豁免集并说明理由")
    assert exempt_counts == _STRICT_EXEMPT_ENTRYPOINTS, (
        f"豁免集的**大小**变了:实得 {exempt_counts},冻结值 {_STRICT_EXEMPT_ENTRYPOINTS} —— "
        "冻结例外集必须钉死大小,否则新洞会顺着老豁免溜进来")


def test_b7_32_the_defgeo_entrypoint_really_asks_for_strict_binding() -> None:
    """反向对照:defgeo 链那条入口**确实**要求 strict —— 豁免集不是全集。

    没有这一条,上面那把枚举锁可以在"全都豁免"的状态下也全绿。
    """
    sites = _reverse_lookup_call_sites()
    strict_entries = {entry for entry, _h, _l, strict in sites if strict}
    assert strict_entries, "没有任何一条入口传 strict_unique —— 那 B-5 根本没接上"
    assert "publish" in strict_entries, (
        f"defgeo 链走的 publish 入口没要求 strict 反查:{sorted(strict_entries)}")

    transport = (ROOT / "services" / "defensive_geo" / "publish"
                 / "provider_transport.py").read_text(encoding="utf-8")
    assert "strict_order_ref_binding=True" in transport, (
        "provider_transport 没有把 strict 传下去 —— 上面那条只证明了形参存在")
