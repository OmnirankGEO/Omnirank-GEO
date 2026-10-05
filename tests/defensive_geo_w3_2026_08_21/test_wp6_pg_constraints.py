"""WP6 · 迁移 044 的**真 PG16 约束**判据(不打 Python,打库)。

覆盖:MIG-01(2× 重放幂等)/ §12.2 三条 partial unique / 冻结面不可变 trigger /
      12 条 CHECK 逐条正反 / one_live_child / outbox 唯一 / store 层现算与 CAS /
      列清单与 information_schema 逐列对账。

═══════════════════════════════════════════════════════════════════════════
🔴 本文件的三条纪律(每条都是付过学费买来的)
═══════════════════════════════════════════════════════════════════════════
1. **每条"必须命中"配一条"必须不命中"**。只证明"约束会拒"证明不了它拒得对 ——
   一条 ``CHECK (false)`` 能让所有正样本全红、所有反样本全绿。所以每把锁都要
   同时给一发**该放行**的正样本。

2. **反样本必须断言 constraint_name 逐字相等**,不能只断言"抛了异常"。
   2026-08-21 记过:多规则扫描器的正样本只断言「有命中」= 被别的规则顺手判红,
   那条规则整个删掉判据照样绿。这里同理 —— 同一条 INSERT 身上叠着 3~5 把锁,
   随便哪把拒了都会抛 IntegrityError。只有比对 ``exc.diag.constraint_name``
   才能确定**是被测的那一把**在干活。

3. **反样本必须与其它锁隔离**。例:测 ``defgeo_pcmd_one_live_child`` 时两个 child
   必须放在**不同 slot** 上,否则先撞 ``defgeo_pcmd_one_live_per_slot``,
   目标锁删掉判据照样绿(「第二把锁遮住第一把,变异存活」)。

🔴 撕锁自证:本文件每一条"证明约束存在"的判据都在一次性库上被**亲手 DROP 过**
   并确认转红、重建后转绿。过程见交付报告的 commandsRun。
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

from services.defensive_geo.publish import store

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db" / "migration_044_defgeo_publish_decision_2026_08_21.sql"

TENANT = 9301
OTHER_TENANT = 9302
BRAND = 9401

#: 044 建的六张表 —— 重放前后逐表数行,任何一张多出行 = 迁移体内有 DML。
PKG_TABLES = (
    "defgeo_publish_slots",
    "defgeo_publish_decision_snapshots",
    "defgeo_publish_commands",
    "defgeo_publish_outbox",
    "defgeo_settlement_review_entries",
    "defgeo_provider_execution_budgets",
)


def _dsn() -> str:
    """🔴 安全栓复用窗A/窗B 口径:库名必须同时含 ``defgeo`` 与 ``test``。

    2026-08-15 实测过:库名不含 ``test`` 会让安全栓整个失效。本文件会开
    **独立连接**跑迁移重放与并发,不走 conftest 的 ``db`` fixture,
    所以安全栓必须在这里再钉一遍 —— 不然它就只守住了 fixture 那一条路径。
    """
    raw = os.environ.get("TEST_DATABASE_URL", "").split("?", 1)[0]
    dbname = raw.rsplit("/", 1)[-1].lower()
    missing = [t for t in ("defgeo", "test") if t not in dbname]
    assert raw and not missing, (
        f"判据锁死在一次性库上:库名必须同时含 defgeo 与 test,实得 {raw!r}(缺 {missing})"
    )
    return raw


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:18]}"


def _h64(seed: str) -> str:
    """CHARACTER(64) 列要的就是 64 位十六进制 —— 用真 sha256,不用 'f'*64 凑数。"""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════════════════
# 反样本工具:必须命中**指定的那一把**锁
# ══════════════════════════════════════════════════════════════════════════
def assert_rejected(db, fn, *, constraint: str, label: str):
    """执行 ``fn``,要求它被名为 ``constraint`` 的约束拒绝。

    用 SAVEPOINT 包住 —— 失败语句会把整个事务打成 aborted,
    不回滚到 savepoint 的话**后面每一条判据都会变成 InFailedSqlTransaction**,
    那是「一条红把后面全部染红」的老形态,红因与被测代码无关。
    """
    with db.cursor() as cur:
        cur.execute("SAVEPOINT sp_neg")
    try:
        fn()
    except psycopg2.Error as exc:
        got = getattr(exc.diag, "constraint_name", None)
        with db.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT sp_neg")
        assert got == constraint, (
            f"{label}:确实被拒了,但拒它的是 {got!r},不是被测的 {constraint!r}。"
            "只断言「抛了异常」会被同一行上的别的锁顺手兜住 —— 目标锁整个删掉判据照样绿。"
        )
        return exc
    with db.cursor() as cur:
        cur.execute("ROLLBACK TO SAVEPOINT sp_neg")
    raise AssertionError(
        f"{label}:约束 {constraint} **没有**拦住它。这把锁不存在,或者它的谓词根本盖不到这个形态。"
    )


def assert_accepted(db, fn, *, label: str):
    """正样本:必须**放行**。没有它,一条 ``CHECK (false)`` 能让全部反样本假绿。"""
    with db.cursor() as cur:
        cur.execute("SAVEPOINT sp_pos")
    try:
        out = fn()
    except psycopg2.Error as exc:
        with db.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT sp_pos")
        raise AssertionError(
            f"{label}:这是**合法**形态却被拒了(constraint={getattr(exc.diag, 'constraint_name', None)!r}"
            f" / {type(exc).__name__}: {exc})—— 约束误挡"
        ) from exc
    with db.cursor() as cur:
        cur.execute("RELEASE SAVEPOINT sp_pos")
    return out


def assert_trigger_raises(db, fn, *, must_contain: str, label: str):
    """trigger 的 RAISE 没有 constraint_name,只能比 pgcode + 文案。"""
    with db.cursor() as cur:
        cur.execute("SAVEPOINT sp_trg")
    try:
        fn()
    except psycopg2.Error as exc:
        code = exc.pgcode
        msg = str(exc)
        with db.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT sp_trg")
        assert code == "P0001", (
            f"{label}:抛了但不是 plpgsql RAISE(pgcode={code!r})—— "
            f"可能是别的约束顺手拒的,不是冻结守卫:{msg}"
        )
        assert must_contain in msg, f"{label}:RAISE 文案里找不到 {must_contain!r};实得 {msg}"
        return exc
    with db.cursor() as cur:
        cur.execute("ROLLBACK TO SAVEPOINT sp_trg")
    raise AssertionError(f"{label}:冻结守卫 trigger **没有**拦住它 —— 冻结面其实可改")


# ══════════════════════════════════════════════════════════════════════════
# 造数
# ══════════════════════════════════════════════════════════════════════════
def mk_slot(cur, *, tenant: int = TENANT, brand: int = BRAND) -> str:
    slot_id = _uid("slot")
    cur.execute(
        "INSERT INTO defgeo_publish_slots "
        "(publish_slot_id, tenant_owner_id, service_projection_id, accepted_snapshot_id, "
        " plan_item_key, brand_id, publish_item_request_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (slot_id, tenant, f"svc-w3c-{tenant}", 9001, _uid("plan"), brand, _uid("pir")),
    )
    return slot_id


def mk_snapshot(
    cur,
    slot_id: str,
    *,
    tenant: int = TENANT,
    version: int = 1,
    lifecycle: str = "open",
    successor_id: str | None = None,
    successor_hash: str | None = None,
    supersession_kind: str | None = None,
    consumed_command_id: str | None = None,
    payload: dict | None = None,
    expires_sql: str = "NOW() + INTERVAL '30 minutes'",
    snapshot_id: str | None = None,
) -> str:
    """🔴 ``expires_at`` 走 **服务端 NOW() + INTERVAL**,不传 Python 时钟值。

    「判据里不许有今天」—— 过期与否是 DB 的 NOW() 说了算,
    夹具塞一个本机算出来的绝对时间戳,跨时区/跨日界就会变成定时炸弹。
    """
    sid = snapshot_id or _uid("snap")
    cur.execute(
        "INSERT INTO defgeo_publish_decision_snapshots "
        "(decision_snapshot_id, publish_slot_id, snapshot_version, canonical_hash, "
        " frozen_payload, lifecycle, expires_at, superseded_by_snapshot_id, "
        " superseded_by_snapshot_hash, supersession_kind, consumed_command_id, "
        " idempotency_key, request_canonical_hash, tenant_owner_id) "
        f"VALUES (%s,%s,%s,%s,%s::jsonb,%s,{expires_sql},%s,%s,%s,%s,%s,%s,%s)",
        (sid, slot_id, version, _h64(sid),
         json.dumps(payload if payload is not None else {"slot": slot_id}, ensure_ascii=False),
         lifecycle, successor_id, successor_hash, supersession_kind, consumed_command_id,
         _uid("idem"), _h64("req-" + sid), tenant),
    )
    return sid


def command_values(slot_id: str, snapshot_id: str, *, tenant: int = TENANT,
                   brand: int = BRAND, **over) -> dict:
    cid = over.pop("publish_command_id", None) or _uid("cmd")
    vals = {
        "publish_command_id": cid,
        "publish_slot_id": slot_id,
        "decision_snapshot_id": snapshot_id,
        "decision_snapshot_hash": _h64(snapshot_id),
        "command_canonical_hash": _h64("cc-" + cid),
        "parent_command_id": None,
        "command_generation": 1,
        "lineage_kind": "root",
        "tenant_owner_id": tenant,
        "actor_user_id": tenant,
        "brand_id": brand,
        "publish_item_request_id": _uid("pir"),
        "article_revision_id": "artrev-1",
        "article_hash": "arthash-1",
        "public_media_key": _h64("media-990001"),
        "canonical_root_domain_key": _h64("example-daily.com.cn"),
        "funding_policy": "personal_wallet",
        "principal_kind": "service_provider",
        "payer_user_id": tenant,
        "exact_settlement_points": 100,
        "freeze_id": 4001,
        "freeze_task_ref": _uid("ftr"),
        "freeze_backend": "wallet",
        "funding_state": "frozen",
        "command_state": "queued",
        "canonical_publication_state": "not_started",
        "provider_call_count": 0,
        "idempotency_key": _uid("idem"),
        "request_canonical_hash": _h64("rq-" + cid),
    }
    vals.update(over)
    return vals


def mk_claimed_outbox(cur, cmd: str, slot_id: str, token: str) -> str:
    """建一条 outbox 并**真领一次租约**,返回行上真正的 ``claim_token``。

    🔴 [E1-1 = Codex 二审 P0-F1] ``mark_external_start`` 的 WHERE 现在还绑
       「outbox 仍被我这次租约持有」。原来这几条判据直接对一条**没有 outbox**
       的 command 调它 —— 那是生产里不存在的形状。改成真走一遍 enqueue+claim。
    """
    store.enqueue_outbox(cur, publish_command_id=cmd, publish_slot_id=slot_id)
    # 🔴 本库不是每条判据都清空的,历史 outbox 有几百行;``claim_outbox`` 按
    #    ``ORDER BY available_at`` 取 limit 条,新入队的行排在最后**根本领不到**。
    #    所以把**我这一行**的可用时间拨到十年前让它排第一,再走**真的**
    #    ``claim_outbox`` —— 不绕过被测函数,也不手搓 claimed 状态。
    cur.execute(
        "UPDATE defgeo_publish_outbox SET available_at = NOW() - INTERVAL '10 years' "
        "WHERE publish_command_id = %s", (cmd,))
    rows = store.claim_outbox(cur, claim_token=token, limit=1)
    mine = [r for r in rows if str(r["publish_command_id"]) == str(cmd)]
    assert mine, f"没领到 {cmd} 的 outbox 行(领到的是 {[r['id'] for r in rows]})"
    return str(mine[0]["claim_token"])


def mk_command(cur, slot_id: str, snapshot_id: str, **over) -> str:
    vals = command_values(slot_id, snapshot_id, **over)
    cols = list(vals)
    cur.execute(
        f"INSERT INTO defgeo_publish_commands ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))})",
        tuple(vals[c] for c in cols),
    )
    return vals["publish_command_id"]


# ══════════════════════════════════════════════════════════════════════════
# MIG-01 · 044 在本库 2× 重放幂等
# ══════════════════════════════════════════════════════════════════════════
def _autocommit_conn():
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    with conn.cursor() as cur:
        cur.execute("SET search_path TO public")
    return conn


SENTINEL = "mig01sent"


def _seed_committed_sentinels(conn) -> None:
    """在六张表各**提交**一行哨兵。

    🔴 这不是装饰,是**分母**。迁移重放跑在自己的 autocommit 连接上,只看得见
       已提交的行;而本包其余判据全在 ``db`` fixture 的事务里跑完就回滚 ——
       所以不铺哨兵的话,"行数守恒"是 0 == 0,一条会**删行**的 DML 也照样全绿。
       「零分母判据只能记『没验』」。
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO defgeo_publish_slots (publish_slot_id, tenant_owner_id, "
            "service_projection_id, accepted_snapshot_id, plan_item_key, brand_id, "
            "publish_item_request_id) VALUES (%s,%s,'svc-mig01',9001,%s,%s,%s)",
            (f"{SENTINEL}-slot", TENANT, f"{SENTINEL}-plan", BRAND, f"{SENTINEL}-pir"))
        cur.execute(
            "INSERT INTO defgeo_publish_decision_snapshots (decision_snapshot_id, "
            "publish_slot_id, snapshot_version, canonical_hash, frozen_payload, lifecycle, "
            "expires_at, idempotency_key, request_canonical_hash, tenant_owner_id) "
            "VALUES (%s,%s,1,%s,'{}'::jsonb,'open',NOW()+INTERVAL '30 minutes',%s,%s,%s)",
            (f"{SENTINEL}-snap", f"{SENTINEL}-slot", _h64(SENTINEL),
             f"{SENTINEL}-idem", _h64(SENTINEL + "-req"), TENANT))
        cur.execute(
            "INSERT INTO defgeo_publish_commands (publish_command_id, publish_slot_id, "
            "decision_snapshot_id, decision_snapshot_hash, command_canonical_hash, "
            "tenant_owner_id, actor_user_id, brand_id, publish_item_request_id, "
            "article_revision_id, article_hash, public_media_key, canonical_root_domain_key, "
            "funding_policy, principal_kind, payer_user_id, exact_settlement_points, "
            "freeze_id, freeze_task_ref, freeze_backend, idempotency_key, "
            "request_canonical_hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'rev1','h1',%s,%s,"
            "'personal_wallet','service_provider',%s,100,4001,%s,'wallet',%s,%s)",
            (f"{SENTINEL}-cmd", f"{SENTINEL}-slot", f"{SENTINEL}-snap", _h64(SENTINEL),
             _h64(SENTINEL + "-cc"), TENANT, TENANT, BRAND, f"{SENTINEL}-pir",
             _h64("media"), _h64("domain"), TENANT, f"{SENTINEL}-ftr",
             f"{SENTINEL}-idem", _h64(SENTINEL + "-rq")))
        cur.execute(
            "INSERT INTO defgeo_publish_outbox (publish_command_id, publish_slot_id, "
            "event_kind) VALUES (%s,%s,'publish_command_created')",
            (f"{SENTINEL}-cmd", f"{SENTINEL}-slot"))
        cur.execute(
            "INSERT INTO defgeo_settlement_review_entries (publish_command_id, entry_kind, "
            "actor_user_id, actor_role, funding_state_before) "
            "VALUES (%s,'provider_evidence',%s,'service_provider','frozen')",
            (f"{SENTINEL}-cmd", TENANT))
        cur.execute(
            "INSERT INTO defgeo_provider_execution_budgets (execution_budget_snapshot_id, "
            "budget_version, tenant_owner_id, accepted_snapshot_id, service_projection_id, "
            "global_cap_points, scope_cap_points, funding_policy, budget_hash) "
            "VALUES (%s,1,%s,9001,'svc-mig01',1000,1000,'personal_wallet',%s)",
            (f"{SENTINEL}-budget", TENANT, _h64(SENTINEL + "-b")))


def _drop_sentinels(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM defgeo_settlement_review_entries WHERE publish_command_id=%s",
                    (f"{SENTINEL}-cmd",))
        cur.execute("DELETE FROM defgeo_publish_outbox WHERE publish_command_id=%s",
                    (f"{SENTINEL}-cmd",))
        cur.execute("DELETE FROM defgeo_publish_commands WHERE publish_command_id=%s",
                    (f"{SENTINEL}-cmd",))
        cur.execute("DELETE FROM defgeo_publish_decision_snapshots WHERE decision_snapshot_id=%s",
                    (f"{SENTINEL}-snap",))
        cur.execute("DELETE FROM defgeo_publish_slots WHERE publish_slot_id=%s",
                    (f"{SENTINEL}-slot",))
        cur.execute("DELETE FROM defgeo_provider_execution_budgets "
                    "WHERE execution_budget_snapshot_id=%s", (f"{SENTINEL}-budget",))


def test_mig01_migration_replays_twice_without_error_and_without_dml():
    """044 再跑两遍必须全 no-op:不抛错、六张表**一行不多也一行不少**。

    🔴 「不抛错」单独是不够的:``prestart`` 每次部署无条件重放全部迁移(无追踪表),
       所以迁移体内任何一条 DML 都会**每次部署再执行一遍**。行数守恒才是那条锁。
    🔴 六张表先各铺一行**已提交**哨兵,否则守恒是 0 == 0 的空断言(见下面的
       ``assert min(...) >= 1``,那一行就是分母自证)。
    🔴 配对的「必须不命中」在下一条判据:同一条执行路径喂一段必炸的 SQL 必须真炸。
    """
    sql = MIGRATION.read_text(encoding="utf-8", errors="replace")
    assert "CREATE TABLE IF NOT EXISTS public.defgeo_publish_slots" in sql, (
        "迁移文件形态变了 —— 判据可能打在空气上")

    conn = _autocommit_conn()

    def _counts() -> dict[str, int]:
        out = {}
        with conn.cursor() as cur:
            for t in PKG_TABLES:
                cur.execute(f"SELECT count(*) AS c FROM {t}")
                out[t] = cur.fetchone()["c"]
        return out

    try:
        _drop_sentinels(conn)                 # 上一次跑崩留下的残留先清,分母才干净
        _seed_committed_sentinels(conn)
        before = _counts()
        assert min(before.values()) >= 1, (
            f"哨兵没铺上,行数守恒会退化成 0 == 0 的空断言:{before}")

        for round_no in (1, 2):
            replay = _autocommit_conn()
            try:
                with replay.cursor() as cur:
                    cur.execute(sql)
            except psycopg2.Error as exc:              # pragma: no cover - 红了就是真红
                raise AssertionError(
                    f"044 第 {round_no} 次重放抛错:{type(exc).__name__}: {exc}。"
                    "prestart 每次部署都会重放,不幂等 = 每次部署炸一次"
                ) from exc
            finally:
                replay.close()

        after = _counts()
        assert after == before, (
            f"044 重放后行数变了:{before} → {after}。迁移体内有 DML,"
            "而 prestart 无条件重放 = 每次部署再执行一遍")

        # 行数相等还挡不住「删一行又插一行」,所以哨兵的身份也要逐条回读。
        with conn.cursor() as cur:
            cur.execute("SELECT canonical_hash, lifecycle FROM "
                        "defgeo_publish_decision_snapshots WHERE decision_snapshot_id=%s",
                        (f"{SENTINEL}-snap",))
            snap = cur.fetchone()
            cur.execute("SELECT funding_state, command_state, status_version FROM "
                        "defgeo_publish_commands WHERE publish_command_id=%s",
                        (f"{SENTINEL}-cmd",))
            cmd = cur.fetchone()
        assert snap is not None and cmd is not None, "重放把哨兵行删掉了"
        assert snap["canonical_hash"] == _h64(SENTINEL) and snap["lifecycle"] == "open"
        assert (cmd["funding_state"], cmd["command_state"], cmd["status_version"]) == \
            ("frozen", "queued", 1), f"重放改动了既有 command 行:{cmd}"
    finally:
        _drop_sentinels(conn)
        conn.close()


def test_mig01_replay_path_is_really_executed(db):
    """MIG-01 的「必须不命中」臂:同一条执行路径喂必炸 SQL 必须真炸。

    没有这一条,上一条判据的绿可能来自「SQL 压根没提交给服务端」——
    退出码 0 可能是工具根本没跑,同样的假绿在 SQL 这一侧长这个样子。
    """
    sql = MIGRATION.read_text(encoding="utf-8", errors="replace") + "\nSELECT 1/0;\n"
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    try:
        with pytest.raises(psycopg2.errors.DivisionByZero):
            with conn.cursor() as cur:
                cur.execute("SET search_path TO public")
                cur.execute(sql)
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# §12.2 ① defgeo_pds_one_open_per_slot
# ══════════════════════════════════════════════════════════════════════════
def test_pds_one_open_per_slot_blocks_second_open(db):
    with db.cursor() as cur:
        slot = mk_slot(cur)
        mk_snapshot(cur, slot, version=1)
        assert_rejected(
            db, lambda: mk_snapshot(cur, slot, version=2),
            constraint="defgeo_pds_one_open_per_slot",
            label="同 slot 第二个 open snapshot",
        )


def test_pds_one_open_per_slot_releases_after_supersede(db):
    """「必须不命中」臂之一:前一个改成 superseded 之后,第二个 open 必须插得进。

    这条打的是**谓词的窄度**。如果有人把 partial unique 写成全表 unique
    (掉了 ``WHERE lifecycle='open'``),上一条判据照样绿,只有这一条会红。
    """
    with db.cursor() as cur:
        slot = mk_slot(cur)
        first = mk_snapshot(cur, slot, version=1)
        successor = _uid("snap")
        cur.execute(
            "UPDATE defgeo_publish_decision_snapshots "
            "SET lifecycle='superseded', superseded_by_snapshot_id=%s, "
            "    superseded_by_snapshot_hash=%s, supersession_kind='new_preview' "
            "WHERE decision_snapshot_id=%s",
            (successor, _h64(successor), first),
        )
        assert_accepted(
            db, lambda: mk_snapshot(cur, slot, version=2, snapshot_id=successor),
            label="前任已 superseded 后插新的 open",
        )
        cur.execute(
            "SELECT count(*) AS c FROM defgeo_publish_decision_snapshots "
            "WHERE publish_slot_id=%s AND lifecycle='open'", (slot,))
        assert cur.fetchone()["c"] == 1


def test_pds_one_open_per_slot_does_not_block_other_slots(db):
    """「必须不命中」臂之二:**不同 slot** 各一个 open 必须都成功。

    如果谓词的键掉了 ``publish_slot_id``(只剩 tenant),这条会红。
    """
    with db.cursor() as cur:
        slot_a, slot_b = mk_slot(cur), mk_slot(cur)
        mk_snapshot(cur, slot_a)
        assert_accepted(db, lambda: mk_snapshot(cur, slot_b), label="另一个 slot 的 open")
        cur.execute(
            "SELECT count(*) AS c FROM defgeo_publish_decision_snapshots "
            "WHERE publish_slot_id = ANY(%s) AND lifecycle='open'", ([slot_a, slot_b],))
        assert cur.fetchone()["c"] == 2


# ══════════════════════════════════════════════════════════════════════════
# §12.2 ② defgeo_pcmd_one_live_per_slot
# ══════════════════════════════════════════════════════════════════════════
def test_pcmd_one_live_per_slot_blocks_second_live(db):
    """同 slot 第二个**非终态** command 必冲突。

    两条都用 ``parent_command_id=NULL``(root)—— 不然会先撞
    ``defgeo_pcmd_one_live_child``,那把锁会遮住被测的这一把。
    """
    with db.cursor() as cur:
        slot = mk_slot(cur)
        snap = mk_snapshot(cur, slot)
        mk_command(cur, slot, snap, command_state="running")
        assert_rejected(
            db, lambda: mk_command(cur, slot, snap, command_state="queued"),
            constraint="defgeo_pcmd_one_live_per_slot",
            label="同 slot 第二个非终态 command",
        )


def test_pcmd_one_live_per_slot_releases_after_cancelled(db):
    """前一个进终态(cancelled)后,同 slot 必须能再插一条。

    ⚠️ 这里用 ``cancelled`` 收尾而不是 ``completed`` + committed/verified ——
       后者会同时落进 ``defgeo_pcmd_one_committed_fulfillment`` 的谓词,
       第二把锁会把这条判据的绿/红整个顶掉(两把锁叠在同一条路径上时,
       "相关判据全绿"证明不了被测的那一把被验过)。
    """
    with db.cursor() as cur:
        slot = mk_slot(cur)
        snap = mk_snapshot(cur, slot)
        first = mk_command(cur, slot, snap, command_state="running")
        cur.execute("UPDATE defgeo_publish_commands SET command_state='cancelled' "
                    "WHERE publish_command_id=%s", (first,))
        assert_accepted(db, lambda: mk_command(cur, slot, snap, command_state="queued"),
                        label="前任 cancelled 后的第二条 command")
        cur.execute(
            "SELECT count(*) AS c FROM defgeo_publish_commands "
            "WHERE publish_slot_id=%s AND command_state <> ALL(%s)",
            (slot, list(store.TERMINAL_COMMAND_STATES)))
        assert cur.fetchone()["c"] == 1


def test_pcmd_one_live_per_slot_does_not_block_other_slots(db):
    with db.cursor() as cur:
        slot_a, slot_b = mk_slot(cur), mk_slot(cur)
        snap_a, snap_b = mk_snapshot(cur, slot_a), mk_snapshot(cur, slot_b)
        mk_command(cur, slot_a, snap_a, command_state="running")
        assert_accepted(db, lambda: mk_command(cur, slot_b, snap_b, command_state="running"),
                        label="另一个 slot 的 live command")


def test_live_command_states_match_the_index_predicate(db):
    """``store.LIVE_COMMAND_STATES`` 必须与 044 的 partial unique 谓词**同源**。

    两处写不一样 = 「DB 拦住了但应用层以为没拦」,这是最难查的一类状态。
    这里直接从 ``pg_indexes`` 取索引定义,逐个状态做**成员判定**。
    """
    with db.cursor() as cur:
        cur.execute("SELECT indexdef FROM pg_indexes WHERE indexname=%s",
                    ("defgeo_pcmd_one_live_per_slot",))
        row = cur.fetchone()
    assert row is not None, "defgeo_pcmd_one_live_per_slot 索引不存在"
    indexdef = row["indexdef"]
    for terminal in store.TERMINAL_COMMAND_STATES:
        assert f"'{terminal}'" in indexdef, (
            f"终态 {terminal!r} 不在索引谓词里:{indexdef}")
    for live in store.LIVE_COMMAND_STATES:
        assert f"'{live}'" not in indexdef, (
            f"store 认为 {live!r} 是非终态,但索引谓词把它当终态排除了:{indexdef}")


# ══════════════════════════════════════════════════════════════════════════
# §12.2 ③ defgeo_pcmd_one_committed_fulfillment
# ══════════════════════════════════════════════════════════════════════════
def _committed_fulfilled(cur, slot, snap, **over):
    """committed + verified_published,且 command_state 走终态 ——
    终态是为了让 ``one_live_per_slot`` **不参与**,把被测面隔离出来。"""
    return mk_command(cur, slot, snap, funding_state="committed",
                      canonical_publication_state="verified_published",
                      command_state="completed", **over)


def test_pcmd_one_committed_fulfillment_blocks_second(db):
    with db.cursor() as cur:
        slot = mk_slot(cur)
        snap = mk_snapshot(cur, slot)
        _committed_fulfilled(cur, slot, snap)
        assert_rejected(
            db, lambda: _committed_fulfilled(cur, slot, snap),
            constraint="defgeo_pcmd_one_committed_fulfillment",
            label="同 slot 第二个 committed+verified 履约",
        )


def test_pcmd_one_committed_fulfillment_covers_retracted_too(db):
    """索引谓词写的是 ``IN ('verified_published','retracted')`` —— 两个值都在锁内。

    只测 verified_published 会让谓词被改成单值时判据照样绿。
    """
    with db.cursor() as cur:
        slot = mk_slot(cur)
        snap = mk_snapshot(cur, slot)
        _committed_fulfilled(cur, slot, snap)
        assert_rejected(
            db,
            lambda: mk_command(cur, slot, snap, funding_state="committed",
                               canonical_publication_state="retracted",
                               command_state="completed"),
            constraint="defgeo_pcmd_one_committed_fulfillment",
            label="同 slot committed+retracted 第二条",
        )


def test_pcmd_one_committed_fulfillment_predicate_is_narrow(db):
    """「必须不命中」臂:committed 但**未核实发布**的第二条必须插得进。

    这条锁的是谓词的窄度 —— 掉了 ``canonical_publication_state IN (...)``
    这一半的话,上面两条照样绿,只有这一条会红。
    """
    with db.cursor() as cur:
        slot = mk_slot(cur)
        snap = mk_snapshot(cur, slot)
        _committed_fulfilled(cur, slot, snap)
        assert_accepted(
            db,
            lambda: mk_command(cur, slot, snap, funding_state="committed",
                               canonical_publication_state="reported_success_unverified",
                               command_state="completed"),
            label="committed 但未核实发布的第二条",
        )
        assert_accepted(
            db,
            lambda: mk_command(cur, slot, snap, funding_state="frozen",
                               canonical_publication_state="verified_published",
                               command_state="completed"),
            label="verified 但资金未 commit 的第二条",
        )


# ══════════════════════════════════════════════════════════════════════════
# defgeo_pcmd_one_live_child
# ══════════════════════════════════════════════════════════════════════════
def test_pcmd_one_live_child_blocks_second_child(db):
    """同 parent 第二个 live child 必冲突。

    🔴 两个 child 放在**不同 slot** 上 —— 同 slot 会先撞 one_live_per_slot,
       那把锁会把这条判据的红整个顶掉(目标锁删了也照样红 = 零区分力)。
    """
    with db.cursor() as cur:
        slot_p, slot_a, slot_b = mk_slot(cur), mk_slot(cur), mk_slot(cur)
        snap_p = mk_snapshot(cur, slot_p)
        parent = mk_command(cur, slot_p, snap_p, command_state="failed")
        snap_a, snap_b = mk_snapshot(cur, slot_a), mk_snapshot(cur, slot_b)
        mk_command(cur, slot_a, snap_a, parent_command_id=parent,
                   command_generation=2, lineage_kind="retry_child", command_state="running")
        assert_rejected(
            db,
            lambda: mk_command(cur, slot_b, snap_b, parent_command_id=parent,
                               command_generation=2, lineage_kind="retry_child",
                               command_state="queued"),
            constraint="defgeo_pcmd_one_live_child",
            label="同 parent 第二个 live child",
        )


def test_pcmd_one_live_child_does_not_block_other_parents_or_terminal(db):
    """两条「必须不命中」:不同 parent 各一个 live child / 前一个 child 进终态后可再生。"""
    with db.cursor() as cur:
        slots = [mk_slot(cur) for _ in range(6)]
        snaps = [mk_snapshot(cur, s) for s in slots]
        parent_a = mk_command(cur, slots[0], snaps[0], command_state="failed")
        parent_b = mk_command(cur, slots[1], snaps[1], command_state="failed")
        child_a = mk_command(cur, slots[2], snaps[2], parent_command_id=parent_a,
                             command_generation=2, lineage_kind="retry_child",
                             command_state="running")
        assert_accepted(
            db,
            lambda: mk_command(cur, slots[3], snaps[3], parent_command_id=parent_b,
                               command_generation=2, lineage_kind="retry_child",
                               command_state="running"),
            label="另一个 parent 的 live child",
        )
        cur.execute("UPDATE defgeo_publish_commands SET command_state='failed' "
                    "WHERE publish_command_id=%s", (child_a,))
        assert_accepted(
            db,
            lambda: mk_command(cur, slots[4], snaps[4], parent_command_id=parent_a,
                               command_generation=3, lineage_kind="retry_child",
                               command_state="queued"),
            label="前一个 child 已终态后的再 retry",
        )


# ══════════════════════════════════════════════════════════════════════════
# 冻结面不可变 trigger(§15.7 / MED-14)
# ══════════════════════════════════════════════════════════════════════════
FREEZE_MSG = "冻结面不可变"


@pytest.mark.parametrize("column, value_sql, param_factory", [
    ("frozen_payload", "%s::jsonb", lambda: json.dumps({"tampered": True})),
    ("canonical_hash", "%s", lambda: _h64("tampered")),
    ("snapshot_version", "%s", lambda: 99),
    ("expires_at", "NOW() + INTERVAL '999 minutes'", None),
])
def test_freeze_guard_blocks_frozen_surface_update(db, column, value_sql, param_factory):
    with db.cursor() as cur:
        slot = mk_slot(cur)
        snap = mk_snapshot(cur, slot)
        params = [] if param_factory is None else [param_factory()]
        params.append(snap)

        def _tamper():
            cur.execute(
                f"UPDATE defgeo_publish_decision_snapshots SET {column} = {value_sql} "
                f"WHERE decision_snapshot_id = %s", tuple(params))

        assert_trigger_raises(db, _tamper, must_contain=FREEZE_MSG,
                              label=f"改冻结面列 {column}")


def test_freeze_guard_blocks_slot_rehoming(db):
    """``publish_slot_id`` 改嫁 = 把一份已冻结的方案挪到别人的格上。"""
    with db.cursor() as cur:
        slot_a, slot_b = mk_slot(cur), mk_slot(cur)
        snap = mk_snapshot(cur, slot_a)
        assert_trigger_raises(
            db,
            lambda: cur.execute(
                "UPDATE defgeo_publish_decision_snapshots SET publish_slot_id=%s "
                "WHERE decision_snapshot_id=%s", (slot_b, snap)),
            must_contain=FREEZE_MSG, label="改 publish_slot_id")


def test_freeze_guard_allows_live_surface_update(db):
    """🔴 「必须不命中」臂:改 ``lifecycle`` 必须成功。

    没有这一条,一个 ``RAISE EXCEPTION`` 无条件抛的 trigger 能让上面 5 条全绿 ——
    而那样的 trigger 会把整条 supersede/consume 链锁死。
    """
    with db.cursor() as cur:
        slot = mk_slot(cur)
        snap = mk_snapshot(cur, slot)
        assert_accepted(
            db,
            lambda: cur.execute(
                "UPDATE defgeo_publish_decision_snapshots "
                "SET lifecycle='cancelled', lifecycle_changed_at=NOW() "
                "WHERE decision_snapshot_id=%s", (snap,)),
            label="改 lifecycle(live 面)")
        cur.execute("SELECT lifecycle FROM defgeo_publish_decision_snapshots "
                    "WHERE decision_snapshot_id=%s", (snap,))
        assert cur.fetchone()["lifecycle"] == "cancelled"


# ══════════════════════════════════════════════════════════════════════════
# CHECK 逐条(每条 ≥1 正样本 + ≥1 反样本)
# ══════════════════════════════════════════════════════════════════════════
def test_chk_pds_lifecycle(db):
    with db.cursor() as cur:
        slot_ok, slot_bad = mk_slot(cur), mk_slot(cur)
        assert_accepted(db, lambda: mk_snapshot(cur, slot_ok, lifecycle="expired"),
                        label="lifecycle=expired(五值之一)")
        assert_rejected(db, lambda: mk_snapshot(cur, slot_bad, lifecycle="zombie"),
                        constraint="chk_defgeo_pds_lifecycle",
                        label="lifecycle=zombie(第六个值)")


def test_chk_pds_successor_superseded_needs_all_three(db):
    with db.cursor() as cur:
        slot = mk_slot(cur)
        succ = _uid("snap")
        assert_accepted(
            db,
            lambda: mk_snapshot(cur, slot, lifecycle="superseded", successor_id=succ,
                                successor_hash=_h64(succ), supersession_kind="override"),
            label="superseded 三项齐全")
        assert_rejected(
            db,
            lambda: mk_snapshot(cur, slot, lifecycle="superseded", successor_id=succ,
                                supersession_kind="override"),
            constraint="chk_defgeo_pds_successor",
            label="superseded 有 id 无 hash(半套 successor = 死 CTA)")
        assert_rejected(
            db,
            lambda: mk_snapshot(cur, slot, lifecycle="superseded", successor_id=succ,
                                successor_hash=_h64(succ), supersession_kind="whatever"),
            constraint="chk_defgeo_pds_successor",
            label="superseded 但 supersession_kind 不在三值内")


def test_chk_pds_successor_non_superseded_must_be_empty(db):
    with db.cursor() as cur:
        slot_a, slot_b = mk_slot(cur), mk_slot(cur)
        assert_accepted(db, lambda: mk_snapshot(cur, slot_a, lifecycle="open"),
                        label="open 且 successor 三项全空")
        succ = _uid("snap")
        assert_rejected(
            db,
            lambda: mk_snapshot(cur, slot_b, lifecycle="open", successor_id=succ,
                                successor_hash=_h64(succ), supersession_kind="override"),
            constraint="chk_defgeo_pds_successor",
            label="open 却带着 successor")


def test_chk_pds_consumed(db):
    with db.cursor() as cur:
        slot_a, slot_b, slot_c = mk_slot(cur), mk_slot(cur), mk_slot(cur)
        assert_accepted(
            db,
            lambda: mk_snapshot(cur, slot_a, lifecycle="consumed",
                                consumed_command_id=_uid("cmd")),
            label="consumed 带 command")
        assert_rejected(
            db, lambda: mk_snapshot(cur, slot_b, lifecycle="consumed"),
            constraint="chk_defgeo_pds_consumed", label="consumed 却没有 command")
        assert_rejected(
            db,
            lambda: mk_snapshot(cur, slot_c, lifecycle="open",
                                consumed_command_id=_uid("cmd")),
            constraint="chk_defgeo_pds_consumed", label="open 却带着 consumed command")


def test_chk_pcmd_funding_state(db):
    with db.cursor() as cur:
        slot = mk_slot(cur)
        snap = mk_snapshot(cur, slot)
        assert_accepted(
            db, lambda: mk_command(cur, slot, snap, funding_state="pending_reconciliation"),
            label="funding_state=pending_reconciliation(六值之一)")
        assert_rejected(
            db, lambda: mk_command(cur, slot, snap, funding_state="settled",
                                   command_state="completed"),
            constraint="chk_defgeo_pcmd_funding_state", label="funding_state=settled(表外值)")


def test_chk_pcmd_command_state(db):
    with db.cursor() as cur:
        slot = mk_slot(cur)
        snap = mk_snapshot(cur, slot)
        assert_accepted(db, lambda: mk_command(cur, slot, snap, command_state="needs_action"),
                        label="command_state=needs_action(九值之一)")
        assert_rejected(db, lambda: mk_command(cur, slot, snap, command_state="done"),
                        constraint="chk_defgeo_pcmd_command_state",
                        label="command_state=done(表外值)")


def test_chk_pcmd_pub_state(db):
    with db.cursor() as cur:
        slot = mk_slot(cur)
        snap = mk_snapshot(cur, slot)
        assert_accepted(
            db,
            lambda: mk_command(cur, slot, snap,
                               canonical_publication_state="reported_success_unverified"),
            label="pub_state=reported_success_unverified(十二值之一)")
        assert_rejected(
            db,
            lambda: mk_command(cur, slot, snap, canonical_publication_state="published",
                               command_state="completed"),
            constraint="chk_defgeo_pcmd_pub_state",
            label="pub_state=published(表外值 · 与 verified_published 差在「谁核实的」)")


def test_chk_pcmd_lineage_root(db):
    with db.cursor() as cur:
        slot_a, slot_b, slot_c = mk_slot(cur), mk_slot(cur), mk_slot(cur)
        snap_a, snap_b, snap_c = (mk_snapshot(cur, slot_a), mk_snapshot(cur, slot_b),
                                  mk_snapshot(cur, slot_c))
        assert_accepted(db, lambda: mk_command(cur, slot_a, snap_a), label="root 正样本")
        assert_rejected(
            db, lambda: mk_command(cur, slot_b, snap_b, parent_command_id=_uid("cmd")),
            constraint="chk_defgeo_pcmd_lineage", label="root 却带 parent")
        assert_rejected(
            db, lambda: mk_command(cur, slot_c, snap_c, command_generation=2),
            constraint="chk_defgeo_pcmd_lineage", label="root 却 generation=2")


def test_chk_pcmd_lineage_retry_child(db):
    with db.cursor() as cur:
        slots = [mk_slot(cur) for _ in range(4)]
        snaps = [mk_snapshot(cur, s) for s in slots]
        parent = mk_command(cur, slots[0], snaps[0], command_state="failed")
        assert_accepted(
            db,
            lambda: mk_command(cur, slots[1], snaps[1], lineage_kind="retry_child",
                               parent_command_id=parent, command_generation=2),
            label="retry_child 正样本(parent 非空 + generation=2)")
        assert_rejected(
            db,
            lambda: mk_command(cur, slots[2], snaps[2], lineage_kind="retry_child",
                               command_generation=2),
            constraint="chk_defgeo_pcmd_lineage", label="retry_child 却无 parent")
        assert_rejected(
            db,
            lambda: mk_command(cur, slots[3], snaps[3], lineage_kind="replacement",
                               parent_command_id=parent, command_generation=1),
            constraint="chk_defgeo_pcmd_lineage", label="replacement 却 generation=1")


def test_chk_pcmd_platform_state(db):
    """平台成本两格的 fundingState 恒为 ``exempt_recorded``(§15.7 表格末列)。"""
    with db.cursor() as cur:
        slot_a, slot_b = mk_slot(cur), mk_slot(cur)
        snap_a, snap_b = mk_snapshot(cur, slot_a), mk_snapshot(cur, slot_b)
        assert_accepted(
            db,
            lambda: mk_command(cur, slot_a, snap_a, principal_kind="platform_cost_center",
                               funding_policy="admin_platform_ledger",
                               funding_state="exempt_recorded", freeze_id=None,
                               freeze_backend=None, payer_user_id=None),
            label="platform_cost_center + exempt_recorded")
        assert_rejected(
            db,
            lambda: mk_command(cur, slot_b, snap_b, principal_kind="platform_cost_center",
                               funding_policy="admin_platform_ledger",
                               funding_state="frozen", freeze_id=None,
                               freeze_backend=None, payer_user_id=None),
            constraint="chk_defgeo_pcmd_platform_state",
            label="platform_cost_center 却 frozen(平台成本走进了客户钱包语义)")


def test_chk_pcmd_platform_state_does_not_touch_tenant_principals(db):
    """「必须不命中」臂:非 platform 主体的 frozen 必须放行 —— 否则整条付费路径被误挡。"""
    with db.cursor() as cur:
        slot = mk_slot(cur)
        snap = mk_snapshot(cur, slot)
        assert_accepted(
            db,
            lambda: mk_command(cur, slot, snap, principal_kind="service_provider",
                               funding_state="frozen"),
            label="service_provider + frozen")


def test_chk_pcmd_external_start(db):
    """有 token 无时间戳(或反过来)必拒;两者同在或同空必过。"""
    with db.cursor() as cur:
        slots = [mk_slot(cur) for _ in range(4)]
        snaps = [mk_snapshot(cur, s) for s in slots]
        assert_rejected(
            db,
            lambda: mk_command(cur, slots[0], snaps[0], external_start_token=_uid("tok")),
            constraint="chk_defgeo_pcmd_external_start", label="有 token 无时间戳")

        def _ts_only():
            vals = command_values(slots[1], snaps[1])
            cols = list(vals)
            cur.execute(
                f"INSERT INTO defgeo_publish_commands ({', '.join(cols)}, external_start_at) "
                f"VALUES ({', '.join(['%s'] * len(cols))}, NOW())",
                tuple(vals[c] for c in cols))

        assert_rejected(db, _ts_only, constraint="chk_defgeo_pcmd_external_start",
                        label="有时间戳无 token")

        def _both():
            vals = command_values(slots[2], snaps[2], external_start_token=_uid("tok"))
            cols = list(vals)
            cur.execute(
                f"INSERT INTO defgeo_publish_commands ({', '.join(cols)}, external_start_at) "
                f"VALUES ({', '.join(['%s'] * len(cols))}, NOW())",
                tuple(vals[c] for c in cols))

        assert_accepted(db, _both, label="token 与时间戳同在")
        assert_accepted(db, lambda: mk_command(cur, slots[3], snaps[3]),
                        label="token 与时间戳同空(尚未外调)")


def test_chk_sre_hold_reason(db):
    """admin_hold 无理由必拒;有理由必过。「先放着」没有理由 = 把死路写进账本。"""
    with db.cursor() as cur:
        slot = mk_slot(cur)
        snap = mk_snapshot(cur, slot)
        cmd = mk_command(cur, slot, snap, funding_state="quarantined")

        def _entry(**over):
            vals = {"publish_command_id": cmd, "entry_kind": "admin_hold",
                    "actor_user_id": TENANT, "actor_role": "platform_admin",
                    "reason": None, "funding_state_before": "quarantined"}
            vals.update(over)
            cols = list(vals)
            cur.execute(
                f"INSERT INTO defgeo_settlement_review_entries ({', '.join(cols)}) "
                f"VALUES ({', '.join(['%s'] * len(cols))})",
                tuple(vals[c] for c in cols))

        assert_rejected(db, lambda: _entry(), constraint="chk_defgeo_sre_hold_reason",
                        label="admin_hold 无理由")
        assert_rejected(db, lambda: _entry(reason="  ab "),
                        constraint="chk_defgeo_sre_hold_reason",
                        label="admin_hold 理由 btrim 后不足 4 字(空格凑数)")
        assert_accepted(db, lambda: _entry(reason="等服务商补发布凭证"),
                        label="admin_hold 有理由")
        assert_accepted(db, lambda: _entry(entry_kind="admin_release",
                                           funding_state_after="released"),
                        label="非 admin_hold 无理由(该放行)")


def test_chk_sre_provider_readonly(db):
    """服务商只能提交凭证,动不了钱 —— 收口权在平台。"""
    with db.cursor() as cur:
        slot = mk_slot(cur)
        snap = mk_snapshot(cur, slot)
        cmd = mk_command(cur, slot, snap, funding_state="pending_reconciliation")

        def _entry(role, kind, **over):
            vals = {"publish_command_id": cmd, "entry_kind": kind,
                    "actor_user_id": TENANT, "actor_role": role,
                    "funding_state_before": "pending_reconciliation"}
            vals.update(over)
            cols = list(vals)
            cur.execute(
                f"INSERT INTO defgeo_settlement_review_entries ({', '.join(cols)}) "
                f"VALUES ({', '.join(['%s'] * len(cols))})",
                tuple(vals[c] for c in cols))

        assert_rejected(db, lambda: _entry("service_provider", "admin_commit"),
                        constraint="chk_defgeo_sre_provider_readonly",
                        label="服务商自己 commit 资金")
        assert_rejected(db, lambda: _entry("service_provider", "admin_release"),
                        constraint="chk_defgeo_sre_provider_readonly",
                        label="服务商自己 release 资金")
        assert_accepted(db, lambda: _entry("service_provider", "provider_evidence"),
                        label="服务商提交凭证")
        assert_accepted(db, lambda: _entry("platform_admin", "admin_commit",
                                           funding_state_after="committed"),
                        label="平台 admin commit")


# ══════════════════════════════════════════════════════════════════════════
# outbox 唯一(kill window ②)
# ══════════════════════════════════════════════════════════════════════════
def test_outbox_command_unique_second_enqueue_is_noop(db):
    """同 command 同 event_kind 第二次 ``enqueue_outbox`` 返 False,表里仍 1 行。

    🔴 分母就是这条 command 自己(全新的),所以「仍 1 行」不会被别的行喂成绿。
    """
    with db.cursor() as cur:
        slot = mk_slot(cur)
        snap = mk_snapshot(cur, slot)
        cmd = mk_command(cur, slot, snap)

        first = store.enqueue_outbox(cur, publish_command_id=cmd, publish_slot_id=slot)
        assert first is True, "第一次入队应当真插"
        second = store.enqueue_outbox(cur, publish_command_id=cmd, publish_slot_id=slot)
        assert second is False, "第二次入队返回了 True —— 重放会产生第二条派发事实"

        cur.execute("SELECT count(*) AS c FROM defgeo_publish_outbox "
                    "WHERE publish_command_id=%s AND event_kind='publish_command_created'",
                    (cmd,))
        assert cur.fetchone()["c"] == 1


def test_outbox_unique_is_scoped_by_event_kind(db):
    """「必须不命中」臂:换 ``event_kind`` 必须能再入一条 —— 唯一键是 (command, kind) 二元组。"""
    with db.cursor() as cur:
        slot = mk_slot(cur)
        snap = mk_snapshot(cur, slot)
        cmd = mk_command(cur, slot, snap)
        assert store.enqueue_outbox(cur, publish_command_id=cmd, publish_slot_id=slot) is True
        assert store.enqueue_outbox(cur, publish_command_id=cmd, publish_slot_id=slot,
                                    event_kind="publish_command_settled") is True
        cur.execute("SELECT count(*) AS c FROM defgeo_publish_outbox WHERE publish_command_id=%s",
                    (cmd,))
        assert cur.fetchone()["c"] == 2


def test_outbox_unique_constraint_is_the_one_that_blocks(db):
    """裸 INSERT 撞的必须是 ``defgeo_pout_command_unique`` 本人。

    ``enqueue_outbox`` 走的是 ``ON CONFLICT DO NOTHING`` —— 它的 False
    只证明「有冲突」,证明不了冲突来自这把锁。这条补上那半截。
    """
    with db.cursor() as cur:
        slot = mk_slot(cur)
        snap = mk_snapshot(cur, slot)
        cmd = mk_command(cur, slot, snap)
        cur.execute("INSERT INTO defgeo_publish_outbox "
                    "(publish_command_id, publish_slot_id, event_kind) VALUES (%s,%s,%s)",
                    (cmd, slot, "publish_command_created"))
        assert_rejected(
            db,
            lambda: cur.execute(
                "INSERT INTO defgeo_publish_outbox "
                "(publish_command_id, publish_slot_id, event_kind) VALUES (%s,%s,%s)",
                (cmd, slot, "publish_command_created")),
            constraint="defgeo_pout_command_unique", label="裸 INSERT 第二条同 kind")


# ══════════════════════════════════════════════════════════════════════════
# store 层:现算 / external-start ≤1 / statusVersion CAS
# ══════════════════════════════════════════════════════════════════════════
def _budget_scenario(cur, budget_id: str):
    """五条 command,各一个 funding_state。reserved 面 3 条 + committed 1 条 + released 1 条。

    每条各占一个 slot —— 同 slot 会撞 one_live_per_slot,那样构造不出这个场景。
    """
    plan = [("frozen", 100), ("pending_reconciliation", 30), ("quarantined", 7),
            ("committed", 250), ("released", 70)]
    for state, points in plan:
        slot = mk_slot(cur)
        snap = mk_snapshot(cur, slot,
                           payload={"executionBudgetSnapshotId": budget_id, "slot": slot})
        mk_command(cur, slot, snap, funding_state=state, exact_settlement_points=points)
    return {"reserved": 100 + 30 + 7, "committed": 250}


def test_budget_usage_computes_reserved_and_committed_live(db):
    with db.cursor() as cur:
        budget_id = _uid("budget")
        want = _budget_scenario(cur, budget_id)
        got = store.budget_usage(cur, execution_budget_snapshot_id=budget_id)
        assert got == {"reservedPoints": want["reserved"], "committedPoints": want["committed"]}, (
            f"现算的 reserved/committed 不对:{got},期望 {want}。"
            "released 必须两边都不算(§3.4 明确 release 后才可由 child retry 重新占用)"
        )


def test_budget_usage_is_scoped_to_its_own_budget(db):
    """「必须不命中」臂:另一个 budget id 必须读到 0/0。

    没有这一条,一个把 WHERE 整个丢掉的实现(SUM 全表)也能让上一条判据绿 ——
    只要库里恰好只有这一批数据。分母干净反而会掩盖「查询没在过滤」。
    """
    with db.cursor() as cur:
        budget_id = _uid("budget")
        _budget_scenario(cur, budget_id)
        other = store.budget_usage(cur, execution_budget_snapshot_id=_uid("budget"))
        assert other == {"reservedPoints": 0, "committedPoints": 0}, (
            f"另一个 budget 读到了 {other} —— 现算没有按 executionBudgetSnapshotId 过滤,"
            "cap 守恒会在错误的一侧全绿")


def test_mark_external_start_is_at_most_once(db):
    """§12.3:worker 外调前写 marker;恢复时任一 marker 存在均不得盲目二次外调。"""
    with db.cursor() as cur:
        slot = mk_slot(cur)
        snap = mk_snapshot(cur, slot)
        cmd = mk_command(cur, slot, snap)
        tok = mk_claimed_outbox(cur, cmd, slot, "w3-tok-atmostonce")

        assert store.mark_external_start(
            cur, publish_command_id=cmd, token="tok-1", claim_token=tok) is True
        cur.execute("SELECT provider_call_count, external_start_token, external_start_at "
                    "FROM defgeo_publish_commands WHERE publish_command_id=%s", (cmd,))
        row1 = cur.fetchone()
        assert row1["provider_call_count"] == 1
        assert row1["external_start_token"] == "tok-1"
        assert row1["external_start_at"] is not None

        assert store.mark_external_start(
            cur, publish_command_id=cmd, token="tok-2", claim_token=tok) is False, (
            "第二次 mark 返回了 True —— 恢复路径会据此再外调一次,那是**真发一次媒体单**")
        cur.execute("SELECT provider_call_count, external_start_token "
                    "FROM defgeo_publish_commands WHERE publish_command_id=%s", (cmd,))
        row2 = cur.fetchone()
        assert row2["provider_call_count"] == 1, (
            f"provider_call_count 被第二次调用推到了 {row2['provider_call_count']}")
        assert row2["external_start_token"] == "tok-1", "marker 被后到者覆盖了"


def test_mark_external_start_is_not_always_false(db):
    """「必须不命中」臂:另一条全新 command 上必须返回 True。

    一个恒返 False 的实现能让上一条判据的后半截全绿 —— 而那样 worker 永远不外调。
    """
    with db.cursor() as cur:
        slot_a, slot_b = mk_slot(cur), mk_slot(cur)
        snap_a, snap_b = mk_snapshot(cur, slot_a), mk_snapshot(cur, slot_b)
        cmd_a, cmd_b = mk_command(cur, slot_a, snap_a), mk_command(cur, slot_b, snap_b)
        tok_a = mk_claimed_outbox(cur, cmd_a, slot_a, "w3-tok-a")
        tok_b = mk_claimed_outbox(cur, cmd_b, slot_b, "w3-tok-b")
        assert store.mark_external_start(
            cur, publish_command_id=cmd_a, token="t", claim_token=tok_a) is True
        assert store.mark_external_start(
            cur, publish_command_id=cmd_b, token="t", claim_token=tok_b) is True, (
            "另一条 command 也被拦了 —— marker 的 WHERE 没有按 command 收敛")
        cur.execute("SELECT provider_call_count FROM defgeo_publish_commands "
                    "WHERE publish_command_id=%s", (cmd_b,))
        assert cur.fetchone()["provider_call_count"] == 1


def test_bump_status_version_is_monotonic(db):
    with db.cursor() as cur:
        slot = mk_slot(cur)
        snap = mk_snapshot(cur, slot)
        cmd = mk_command(cur, slot, snap)

        seen = []
        for state in ("running", "settlement_pending", "completed"):
            row = store.bump_status(cur, publish_command_id=cmd, command_state=state)
            assert row is not None
            seen.append(row["status_version"])
        assert seen == sorted(set(seen)) and len(seen) == 3, (
            f"statusVersion 不是严格递增:{seen} —— 前端靠它判「这是不是更新的状态」")
        assert seen[0] > 1, "第一次 bump 没有把 statusVersion 从初始值推上去"


def test_bump_status_cas_rejects_stale_version(db):
    """``expect_status_version`` 不匹配 ⇒ 返回 None,且**版本不许动**。

    只断言返回 None 是不够的:一个「CAS 失败但还是写了」的实现照样返回 None
    (RETURNING 空 ≠ 没写)。所以要回读 status_version 证明它没被推。
    """
    with db.cursor() as cur:
        slot = mk_slot(cur)
        snap = mk_snapshot(cur, slot)
        cmd = mk_command(cur, slot, snap)
        cur.execute("SELECT status_version FROM defgeo_publish_commands "
                    "WHERE publish_command_id=%s", (cmd,))
        v0 = cur.fetchone()["status_version"]

        stale = store.bump_status(cur, publish_command_id=cmd,
                                  expect_status_version=v0 + 99, command_state="running")
        assert stale is None, "陈旧版本的 CAS 赢了 —— reconciler 与 worker 会互相覆盖"
        cur.execute("SELECT status_version, command_state FROM defgeo_publish_commands "
                    "WHERE publish_command_id=%s", (cmd,))
        after = cur.fetchone()
        assert after["status_version"] == v0, (
            f"CAS 失败却把 statusVersion 从 {v0} 推到了 {after['status_version']}")
        assert after["command_state"] == "queued", "CAS 失败却把状态改了"

        won = store.bump_status(cur, publish_command_id=cmd,
                                expect_status_version=v0, command_state="running")
        assert won is not None, "版本对得上的 CAS 反而输了 —— CAS 恒失败等于状态机卡死"
        assert won["status_version"] == v0 + 1


# ══════════════════════════════════════════════════════════════════════════
# 列清单不手抄:与 information_schema 逐列对账
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("declared, table", [
    (store.SNAPSHOT_COLUMNS, store.SNAPSHOT_TABLE),
    (store.COMMAND_COLUMNS, store.COMMAND_TABLE),
    (store.BUDGET_COLUMNS, store.BUDGET_TABLE),
])
def test_declared_columns_match_information_schema(db, declared, table):
    """少一列或多一列都红。

    🔴 分母自证在下面两行:两侧都必须非空。一个查错表名的实现会让
       ``db_cols`` 空集,而如果 ``declared`` 也空,两个空集相等 = 假绿。
    """
    with db.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s", (table,))
        db_cols = {r["column_name"] for r in cur.fetchall()}

    assert db_cols, f"information_schema 里查不到 {table} 的任何列 —— 对账会退化成空集比空集"
    assert declared, f"store 里 {table} 的列清单是空的 —— 对账没有被测对象"

    missing = sorted(db_cols - set(declared))
    extra = sorted(set(declared) - db_cols)
    assert not missing and not extra, (
        f"{table} 列清单与真库对不上:store 漏了 {missing};store 多写了 {extra}。"
        "漏掉的那一列恰恰是读面永远读不到的那一列(而 SELECT * 会掩盖它)"
    )
