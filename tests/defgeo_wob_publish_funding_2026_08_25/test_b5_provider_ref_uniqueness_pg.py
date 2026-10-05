"""B-5(= Codex P1-5)· 一个上游单号只能结算一笔冻结。

═══════════════════════════════════════════════════════════════════════
🔴 三层各钉一句
═══════════════════════════════════════════════════════════════════════
① **DB**:051 的部分唯一索引(NULL 豁免)。应用层的预检会被并发绕过(TOCTOU),
   唯一索引不会 —— 承重的是它。
② **派发侧**:撞号时**拒绝绑定 + 转核验**,而不是让 UniqueViolation
   把整个派发事务炸掉(那会连 canonical outcome 一起回滚,
   「已经外调过」这件事就丢了 ⇒ 下一轮盲重传)。
③ **上游反查**:``services/meijiehezi/client`` 的「标题 + 媒体」反查命中多条时
   **不挑第一条**。那不是身份键 —— 同标题同媒体重发会命中多条,
   挑中旧单 ⇒ 新命令绑到旧订单号上,同一个上游终态去结算两笔冻结。
   ``strict_unique`` 默认 False ⇒ 媒介盒子老链逐字不变(追加谓词,不改写默认路径)。
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any

import psycopg2
import pytest

from services.defensive_geo.publish import provider_transport as _transport
from services.defensive_geo.publish import publish_worker as _worker
from services.defensive_geo.publish import store as _store
from services.meijiehezi import client as _mhz

from tests.defgeo_wob_publish_funding_2026_08_25._chain import (   # noqa: F401
    base, client, confirmed_command, drain, drain_outbox, fixed_ref, run,
)
from tests.defgeo_wob_publish_funding_2026_08_25.conftest import connect

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_051 = ROOT / "db" / "migration_051_defgeo_publish_settlement_guards_2026_08_25.sql"


def _row(publish_command_id: str) -> dict:
    conn = connect()
    try:
        row = _store.get_command_any_tenant(
            conn.cursor(), publish_command_id=publish_command_id)
        assert row is not None
        return dict(row)
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# ① DB 层:部分唯一索引真的在、真的拦得住、NULL 真的豁免
# ══════════════════════════════════════════════════════════════════════════
def test_b5_01_unique_index_is_live_and_partial() -> None:
    """从**真库**读索引定义:UNIQUE + ``WHERE provider_order_ref IS NOT NULL``。

    只查名字存在是不够的 —— 名字对、定义不对 = 这条守卫从没守过。
    """
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT pg_get_indexdef(c.oid) AS d FROM pg_class c "
            "JOIN pg_index i ON i.indexrelid = c.oid "
            "WHERE c.relname = 'uq_defgeo_pcmd_provider_order_ref' "
            "  AND i.indrelid = to_regclass('public.defgeo_publish_commands')")
        row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None, "051 的唯一索引不在库里 —— 下面的判据全是空气"
    assert "UNIQUE INDEX" in row["d"], row["d"]
    assert "provider_order_ref IS NOT NULL" in row["d"], (
        f"缺 NULL 豁免谓词:{row['d']} —— 「还没外调过」会互相撞唯一")


def test_b5_02_database_refuses_a_duplicate_order_ref(client) -> None:
    """两条 command 直写同一个 ``provider_order_ref`` ⇒ 库层 UniqueViolation。

    🔴 这是**承重那一半**:绕过应用层预检(直接 UPDATE)也拦得住。
    """
    drain()
    drain_outbox()
    a = confirmed_command(client, "b5_dup_a")["publishCommandId"]
    b = confirmed_command(client, "b5_dup_b")["publishCommandId"]
    ref = f"WOB-DUP-{a[-8:]}"
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE {_store.COMMAND_TABLE} SET provider_order_ref = %s "
            f"WHERE publish_command_id = %s", (ref, a))
        conn.commit()
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute(
                f"UPDATE {_store.COMMAND_TABLE} SET provider_order_ref = %s "
                f"WHERE publish_command_id = %s", (ref, b))
        conn.rollback()
    finally:
        conn.close()


def test_b5_03_null_order_refs_do_not_collide(client) -> None:
    """反向对照:多条"还没外调过"(NULL)必须能共存 —— 否则整条链只剩一条命令。"""
    drain()
    drain_outbox()
    ids = [confirmed_command(client, f"b5_null_{i}")["publishCommandId"] for i in range(3)]
    for cid in ids:
        assert _row(cid)["provider_order_ref"] is None, cid   # 三条 NULL 并存,没炸


# ══════════════════════════════════════════════════════════════════════════
# ② 派发侧:撞号拒绝绑定转核验(不是炸事务、也不是覆盖)
# ══════════════════════════════════════════════════════════════════════════
def test_b5_10_duplicate_ref_refuses_binding_and_quarantines(client) -> None:
    """上游把同一个单号交给两条命令 ⇒ 第二条**不绑**、转核验、钱保持冻结。

    🔴 「删掉修复即红」:把 ``publish_outbox`` 里那段
       ``command_by_provider_order_ref`` 预检拿掉,这条会变成
       UniqueViolation 炸掉整个派发事务(external-start marker 一并回滚)。
    """
    drain()
    drain_outbox()
    a = confirmed_command(client, "b5_bind_a")["publishCommandId"]
    b = confirmed_command(client, "b5_bind_b")["publishCommandId"]
    shared = f"WOB-SHARED-{a[-8:]}"

    out = run(_worker.dispatch_pending(limit=50, provider_call=fixed_ref(shared)))
    assert out["dispatched"] >= 2, out

    rows = {cid: _row(cid) for cid in (a, b)}
    bound = [cid for cid, r in rows.items() if str(r["provider_order_ref"] or "") == shared]
    assert len(bound) == 1, (
        f"同一个上游单号绑到了 {len(bound)} 条命令上 —— "
        "同一个上游终态会去结算多笔冻结")
    other = [cid for cid in (a, b) if cid not in bound][0]
    orow = rows[other]
    assert orow["provider_order_ref"] is None, orow["provider_order_ref"]
    assert str(orow["canonical_publication_state"]) == "unknown", (
        "撞号被当成了确定结果 —— 未知就是未知")
    assert str(orow["funding_state"]) == "pending_reconciliation", orow["funding_state"]
    assert orow["settled_at"] is None, "撞号却把账结了"
    # 双方的 external-start marker 都在:事务没被炸回去
    for cid in (a, b):
        assert rows[cid]["external_start_at"] is not None, (
            f"{cid} 的 external-start marker 丢了 —— 下一轮会盲重传")


def test_b5_11_distinct_refs_both_bind(client) -> None:
    """反向对照:单号各不相同时两条都正常绑 —— 那道预检不许误伤活路径。"""
    from tests.defgeo_wob_publish_funding_2026_08_25._chain import accepted

    drain()
    drain_outbox()
    a = confirmed_command(client, "b5_ok_a")["publishCommandId"]
    b = confirmed_command(client, "b5_ok_b")["publishCommandId"]
    run(_worker.dispatch_pending(limit=50, provider_call=accepted("WOB-OK")))
    refs = {str(_row(cid)["provider_order_ref"] or "") for cid in (a, b)}
    assert len(refs) == 2 and "" not in refs, f"正常单号没绑上:{refs}"


def test_b5_12_lookup_is_by_order_ref_not_by_title(client) -> None:
    """回执核对按 ``provider_order_ref`` **精确匹配**,不按标题/媒体。"""
    src = inspect.getsource(_worker._synced_order_outcome)        # noqa: SLF001
    assert "WHERE order_sn = %s" in src, (
        f"回执反查不是按 order_sn 精确匹配:{src}")
    assert "title" not in src and "media" not in src, (
        "回执反查里出现了标题/媒体 —— 那不是身份键")
    poll = inspect.getsource(_worker.poll_pending_outcomes)
    assert 'command.get("provider_order_ref")' in poll and "if not ref:" in poll, (
        "没有单号时必须什么都不做(不许用标题去猜一条)")


# ══════════════════════════════════════════════════════════════════════════
# ③ 上游反查:命中多条不挑第一条(strict);默认路径逐字不变
# ══════════════════════════════════════════════════════════════════════════
class _FakeResp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.status_code = 200
        self._p = payload

    def json(self) -> dict[str, Any]:
        return self._p


class _FakeHttp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._p = payload
        self.gets = 0

    async def get(self, _url, params=None):               # noqa: ANN001
        self.gets += 1
        return _FakeResp(self._p)


def _client_with(items: list[dict[str, Any]]) -> Any:
    c = _mhz.MeiJieHeZiClient.__new__(_mhz.MeiJieHeZiClient)
    c.base_url = "http://fake"
    c._http = _FakeHttp({"data": items})                  # noqa: SLF001
    return c


_TITLE = "同标题同媒体的两次投放"
_TWO_HITS = [
    {"title": _TITLE, "resource_id": 991001, "ordernum": "11NEW"},
    {"title": _TITLE, "resource_id": 991001, "ordernum": "11OLD"},
]
_ONE_HIT = [{"title": _TITLE, "resource_id": 991001, "ordernum": "11ONLY"}]


def _lookup(items, *, strict: bool) -> str:
    c = _client_with(items)
    return asyncio.run(c._lookup_order_sn_by_signature(       # noqa: SLF001
        list_endpoint="/x", title=_TITLE, target_resource_id=991001,
        retries=1, delay_seconds=0, strict_unique=strict))


def test_b5_20_strict_lookup_refuses_multiple_matches() -> None:
    """strict:同标题同媒体命中两条 ⇒ **不绑**(返空),不挑第一条。"""
    assert _lookup(_TWO_HITS, strict=True) == "", (
        "命中多条却挑了一条当身份 —— 旧订单会被绑到新命令上")


def test_b5_20b_refusal_is_distinguishable_from_no_match(caplog) -> None:  # noqa: ANN001
    """🔴 「返空」有两种成因,判据必须**分得出来**。

    ═══════════════════════════════════════════════════════════════════════
    这条是撕锁 MUT-B5-02 逼出来的:上一条判据的区分力是 0
    ═══════════════════════════════════════════════════════════════════════
    把 ``if strict_unique and len(_matched) > 1:`` 那道拒绝摘掉之后,
    strict 分支仍然只往 ``_matched`` 里收、从不赋 ``_hit_sn``,
    于是 ``len(_matched) == 1`` 那一格不成立 ⇒ 照样返回 ``""``。
    **变异活着,而上一条判据全绿** —— 它只看返回值,而"拒绝绑定"与
    "这一轮没匹配到"在返回值上长得一模一样。

    所以这一条钉两个**可观测的**差别:

      ① 它是一次**裁定**,不是"还没等到" ⇒ 立刻 break,**只发一次**列表请求
         (没有那道拒绝时会把 retries 全烧完 —— 上游明明已经回答了);
      ② 落一条明确的拒绝日志(运营侧要能知道为什么这一单没绑上)。
    """
    import logging

    c = _client_with(_TWO_HITS)
    with caplog.at_level(logging.ERROR, logger="GEO-Meijiehezi"):
        got = asyncio.run(c._lookup_order_sn_by_signature(      # noqa: SLF001
            list_endpoint="/x", title=_TITLE, target_resource_id=991001,
            retries=3, delay_seconds=0, strict_unique=True))
    assert got == ""
    assert c._http.gets == 1, (                                 # noqa: SLF001
        f"命中多条是**裁定**,却又重试了 {c._http.gets} 次 —— "   # noqa: SLF001
        "把「上游已经回答了、只是答案不唯一」当成了「还没等到」")
    assert any("拒绝绑定" in r.message for r in caplog.records), (
        "拒绝绑定没有留下可运营的信号 —— 返回值上它与「没匹配到」不可区分")


def test_b5_20c_no_match_still_burns_the_retries(caplog) -> None:  # noqa: ANN001
    """反向对照:**真的没匹配到**时仍然重试(不许把上一条收紧成"永不重试")。"""
    c = _client_with([{"title": "别的标题", "resource_id": 991001, "ordernum": "11X"}])
    got = asyncio.run(c._lookup_order_sn_by_signature(          # noqa: SLF001
        list_endpoint="/x", title=_TITLE, target_resource_id=991001,
        retries=3, delay_seconds=0, strict_unique=True))
    assert got == ""
    assert c._http.gets == 3, (                                 # noqa: SLF001
        f"没匹配到只查了 {c._http.gets} 次 —— 上游列表有秒级缓存延迟,"  # noqa: SLF001
        "不重试会把「还没同步过来」误判成「没有这一单」")


def test_b5_21_strict_lookup_still_binds_a_unique_match() -> None:
    """反向对照:唯一命中时 strict 照常返回 —— 不许收紧过头。"""
    assert _lookup(_ONE_HIT, strict=True) == "11ONLY"


def test_b5_22_legacy_default_behaviour_is_byte_for_byte_unchanged() -> None:
    """默认路径(``strict_unique=False``)仍取第一条 —— 老链逐字不变。

    这是"追加谓词,不改写原谓词"的反向对照:媒介盒子自己的发文/重发链
    不在本单授权范围内,它的行为一格都不许动。
    """
    assert _lookup(_TWO_HITS, strict=False) == "11NEW"
    assert _lookup(_ONE_HIT, strict=False) == "11ONLY"
    for fn in (_mhz.MeiJieHeZiClient._lookup_order_sn_by_signature,     # noqa: SLF001
               _mhz.MeiJieHeZiClient._lookup_order_sns_for_batch,       # noqa: SLF001
               _mhz.MeiJieHeZiClient.publish):
        params = inspect.signature(fn).parameters
        name = "strict_unique" if "strict_unique" in params else "strict_order_ref_binding"
        assert params[name].default is False, (
            f"{fn.__name__} 的 {name} 默认值不是 False —— 老链行为被改写了")


def test_b5_23_defgeo_transport_opts_into_strict_binding() -> None:
    """防御型 GEO 这条链**必须**开 strict —— 它把上游单号当结算身份用。"""
    src = inspect.getsource(_transport._mhz_publish)      # noqa: SLF001
    assert "strict_order_ref_binding=True" in src, (
        "发布链没开 strict —— 上游反查仍会挑第一条,051 的唯一索引会把"
        "整个派发事务炸掉(或绑到旧单上)")
    # 签名对得上(照抄调用必过 bind —— AST 结构锁证不了 kwarg 对不对)
    inspect.signature(_mhz.MeiJieHeZiClient.publish).bind(
        None, title="t", content_md="c", media_ids=[1], strict_order_ref_binding=True)


def test_b5_24_batch_lookup_refuses_multiple_matches_too() -> None:
    """同一道谓词也覆盖批量反查(同一目标的语法形态要一次枚举全)。"""
    items = [
        {"title": _TITLE, "resource_id": 991001, "ordernum": "11A"},
        {"title": _TITLE, "resource_id": 991001, "ordernum": "11B"},
        {"title": _TITLE, "resource_id": 991002, "ordernum": "12ONLY"},
    ]
    c = _client_with(items)
    got = asyncio.run(c._lookup_order_sns_for_batch(          # noqa: SLF001
        list_endpoint="/x", title=_TITLE, target_resource_ids=[991001, 991002],
        retries=1, delay_seconds=0, strict_unique=True))
    assert 991001 not in got, f"撞号的那个媒体仍然被绑了:{got}"
    assert got.get(991002) == "12ONLY", got


# ══════════════════════════════════════════════════════════════════════════
# ④ 迁移体本身
# ══════════════════════════════════════════════════════════════════════════
def test_b5_30_migration_051_is_registered_and_additive() -> None:
    """051 在 manifest 里、体内零 DML、走 index-guard 形态。"""
    from db.migration_manifest import MIGRATIONS

    rel = "db/migration_051_defgeo_publish_settlement_guards_2026_08_25.sql"
    assert rel in MIGRATIONS, "051 没进 manifest —— prestart 只按 manifest 跑,漏登记 = 永远不会跑"
    i47 = MIGRATIONS.index("db/migration_047_defgeo_publish_provider_ref_2026_08_24.sql")
    assert MIGRATIONS.index(rel) > i47, "051 依赖 047 的列,必须排在它后面"

    sql = MIGRATION_051.read_text(encoding="utf-8")
    # 🔴 先剥注释:本迁移的头部注释**正是在解释**为什么不用
    #    ``CREATE UNIQUE INDEX IF NOT EXISTS``。散文里提一句不是写点 ——
    #    本仓记过:引用裁定原文会让裸串结构锚判红,锚要打在代码上不打在字面上。
    body = "\n".join(ln for ln in sql.splitlines() if not ln.strip().startswith("--"))
    for banned in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        assert banned not in body.upper(), (
            f"051 体内出现 DML({banned})—— prestart 每次部署无条件重放")
    assert "@index-guard" in sql, "唯一索引没走本仓 index-guard 形态"
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" not in body, (
        "用了 IF NOT EXISTS:它按 schema 关系名判存,不绑表也不限 relkind —— "
        "名字被别的宿主占着时会静默跳过,这条唯一性从上线起就不存在")
    assert "CREATE UNIQUE INDEX uq_defgeo_pcmd_provider_order_ref" in body, (
        "探针写废了:迁移体里根本没有那条 CREATE UNIQUE INDEX")


def test_b5_31_migration_051_replays_idempotently() -> None:
    """二跑幂等:同一条 SQL 再跑一遍不炸(prestart 每次部署都重放全部迁移)。"""
    from tests.defgeo_wob_publish_funding_2026_08_25.conftest import EXACT_THROWAWAY_URL

    sql = MIGRATION_051.read_text(encoding="utf-8")
    # 🔴 新连接:``autocommit`` 只能在事务外设,而 ``connect()`` 已经开过一个。
    conn = psycopg2.connect(EXACT_THROWAWAY_URL)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute(sql)
            cur.execute(sql)          # 第二遍 —— 幂等
    finally:
        conn.close()
