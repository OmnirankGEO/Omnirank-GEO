"""WP5 · slot 并发收敛(MED-19)—— 真 PG16 × 20 条**独立连接**。

规格 §12.2 逐字:「任何时刻同 slot 不能有两个可 confirm 的 snapshot」,
并发两个不同 Idempotency-Key 的 preview 由 **partial unique 收敛**,
不靠应用层 SELECT-then-INSERT(那在并发下必然有窗口)。

═══════════════════════════════════════════════════════════════════════════
🔴 为什么必须是 20 条独立连接,而不是一条连接跑 20 次
═══════════════════════════════════════════════════════════════════════════
同一条连接上的 20 次 INSERT 是**串行**的,第 2 次一定看得见第 1 次 ——
那样测出来的是"约束在单事务里生效",不是"并发下恰一 winner"。
应用层 SELECT-then-INSERT 在这种串行夹具下**也会全绿**,
于是判据就从来没有碰过它要守的那个窗口。

🔴 每条判据的「必须不命中」臂
   同 slot 20 并发 → 恰 1 赢(必须命中);
   20 个**不同** slot 各 1 并发 → 20 全赢(必须不命中,证明锁不误挡)。
   只有前者的话,一条 ``UNIQUE (tenant_owner_id)`` 的错误索引照样全绿。

🔴 幂等键必须**逐线程互不相同**
   如果 20 条都用同一把 idempotency_key + 同一个 request hash,
   它们会先撞 ``defgeo_pds_idem_root``(全表 unique),
   目标锁 ``defgeo_pds_one_open_per_slot`` 整个删掉判据照样绿。
   所以下面每条都断言 **失败者撞的是哪一把锁**,逐字比 constraint_name。
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import uuid

import psycopg2
import psycopg2.extras
import pytest

pytestmark = pytest.mark.integration

TENANT = 9301
BRAND = 9401
FANOUT = 20


def _dsn() -> str:
    """安全栓:库名必须同时含 ``defgeo`` 与 ``test``(独立连接不走 conftest 的 fixture)。"""
    raw = os.environ.get("TEST_DATABASE_URL", "").split("?", 1)[0]
    dbname = raw.rsplit("/", 1)[-1].lower()
    missing = [t for t in ("defgeo", "test") if t not in dbname]
    assert raw and not missing, (
        f"并发判据会 **COMMIT**,只允许打一次性库:库名须含 defgeo 与 test,实得 {raw!r}(缺 {missing})")
    return raw


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:18]}"


def _h64(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _conn(autocommit: bool = False):
    c = psycopg2.connect(_dsn())
    # 🔴 autocommit 必须在**第一条语句之前**设 —— 先跑了 SET 就已经开了事务,
    #    psycopg2 会抛 "set_session cannot be used inside a transaction"。
    c.autocommit = autocommit
    c.cursor_factory = psycopg2.extras.RealDictCursor
    with c.cursor() as cur:
        cur.execute("SET search_path TO public")
    return c


def _commit_slots(n: int) -> list[str]:
    """并发前把 slot **提交**掉 —— 未提交的 slot 对其它连接不可见,FK 会先炸。"""
    ids = [_uid("cslot") for _ in range(n)]
    conn = _conn(autocommit=True)
    try:
        with conn.cursor() as cur:
            for sid in ids:
                cur.execute(
                    "INSERT INTO defgeo_publish_slots "
                    "(publish_slot_id, tenant_owner_id, service_projection_id, "
                    " accepted_snapshot_id, plan_item_key, brand_id, publish_item_request_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (sid, TENANT, "svc-w3c-conc", 9001, _uid("plan"), BRAND, _uid("pir")))
    finally:
        conn.close()
    return ids


def _drop_slots(slot_ids: list[str]) -> None:
    """并发判据是 **COMMIT** 的,不清就会留给后面的判据当分母噪音。"""
    conn = _conn(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM defgeo_publish_outbox WHERE publish_slot_id = ANY(%s)",
                        (slot_ids,))
            cur.execute("DELETE FROM defgeo_publish_commands WHERE publish_slot_id = ANY(%s)",
                        (slot_ids,))
            cur.execute("DELETE FROM defgeo_publish_decision_snapshots "
                        "WHERE publish_slot_id = ANY(%s)", (slot_ids,))
            cur.execute("DELETE FROM defgeo_publish_slots WHERE publish_slot_id = ANY(%s)",
                        (slot_ids,))
    finally:
        conn.close()


def _insert_open_snapshot(slot_id: str, worker: int) -> tuple[bool, str | None, str | None]:
    """一条**自己的**连接 + 自己的事务。返回 (成功?, 撞了哪把锁, 异常类型)。"""
    conn = _conn()
    try:
        sid = _uid(f"snap{worker}")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO defgeo_publish_decision_snapshots "
                "(decision_snapshot_id, publish_slot_id, snapshot_version, canonical_hash, "
                " frozen_payload, lifecycle, expires_at, idempotency_key, "
                " request_canonical_hash, tenant_owner_id) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,'open',NOW() + INTERVAL '30 minutes',%s,%s,%s)",
                (sid, slot_id, 1, _h64(sid),
                 json.dumps({"worker": worker}),
                 # 🔴 逐线程不同的幂等键 —— 同一把会先撞 defgeo_pds_idem_root,
                 #    把被测的那把 partial unique 整个遮住。
                 _uid(f"idem{worker}"), _h64(f"req-{sid}"), TENANT))
        conn.commit()
        return True, None, None
    except psycopg2.Error as exc:
        conn.rollback()
        return False, getattr(exc.diag, "constraint_name", None), type(exc).__name__
    finally:
        conn.close()


def _fan_out(jobs) -> list[tuple[bool, str | None, str | None]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=FANOUT) as ex:
        futures = [ex.submit(fn, *args) for fn, args in jobs]
        return [f.result() for f in futures]


# ══════════════════════════════════════════════════════════════════════════
# MED-19:同 slot 20 并发,恰一 winner
# ══════════════════════════════════════════════════════════════════════════
def test_med19_twenty_concurrent_open_snapshots_exactly_one_winner():
    slot_ids = _commit_slots(1)
    slot = slot_ids[0]
    try:
        results = _fan_out([(_insert_open_snapshot, (slot, i)) for i in range(FANOUT)])

        winners = [r for r in results if r[0]]
        losers = [r for r in results if not r[0]]
        assert len(winners) == 1, (
            f"{FANOUT} 并发产生了 {len(winners)} 个 winner。"
            "同 slot 出现两个可 confirm 的 snapshot = 服务商会对着一份、"
            "系统会拿另一份去扣钱(§12.2)"
        )
        assert len(losers) == FANOUT - 1

        # 🔴 逐字比 constraint_name:失败必须来自**被测的那一把**锁。
        #    只数「19 个失败」的话,连接数打满、死锁、FK 缺失都能凑出同一个数字。
        constraints = {c for _, c, _ in losers}
        assert constraints == {"defgeo_pds_one_open_per_slot"}, (
            f"失败者撞的锁不是被测的那一把:{constraints}(异常类型 "
            f"{ {t for _, _, t in losers} })"
        )

        conn = _conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) AS c FROM defgeo_publish_decision_snapshots "
                            "WHERE publish_slot_id=%s AND lifecycle='open'", (slot,))
                open_rows = cur.fetchone()["c"]
                cur.execute("SELECT count(*) AS c FROM defgeo_publish_decision_snapshots "
                            "WHERE publish_slot_id=%s", (slot,))
                all_rows = cur.fetchone()["c"]
        finally:
            conn.close()
        assert open_rows == 1, f"库里留下了 {open_rows} 个 open snapshot"
        assert all_rows == 1, (
            f"库里留下了 {all_rows} 行(open 只有 1 行)—— 失败者没有把自己的事务回滚干净")
    finally:
        _drop_slots(slot_ids)


def test_med19_concurrency_does_not_block_distinct_slots():
    """「必须不命中」臂:20 个**不同** slot 各插 1 个 open,必须 20 全赢。

    如果索引键掉了 ``publish_slot_id``(退化成 tenant 级唯一),
    上一条判据照样绿,只有这一条会红成 19 个失败。
    """
    slot_ids = _commit_slots(FANOUT)
    try:
        results = _fan_out([(_insert_open_snapshot, (slot_ids[i], i)) for i in range(FANOUT)])
        winners = [r for r in results if r[0]]
        assert len(winners) == FANOUT, (
            f"{FANOUT} 个不同 slot 只成功了 {len(winners)} 个;"
            f"失败者撞的锁 = { {c for ok, c, _ in results if not ok} } —— 约束在误挡"
        )
        conn = _conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) AS c FROM defgeo_publish_decision_snapshots "
                            "WHERE publish_slot_id = ANY(%s) AND lifecycle='open'", (slot_ids,))
                assert cur.fetchone()["c"] == FANOUT
        finally:
            conn.close()
    finally:
        _drop_slots(slot_ids)


# ══════════════════════════════════════════════════════════════════════════
# MED-20:同 slot 20 并发 command,恰一 winner(顺序二发二扣的并发形态)
# ══════════════════════════════════════════════════════════════════════════
def _seed_snapshot(slot_id: str) -> str:
    sid = _uid("cseed")
    conn = _conn(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO defgeo_publish_decision_snapshots "
                "(decision_snapshot_id, publish_slot_id, snapshot_version, canonical_hash, "
                " frozen_payload, lifecycle, expires_at, idempotency_key, "
                " request_canonical_hash, tenant_owner_id) "
                "VALUES (%s,%s,1,%s,%s::jsonb,'open',NOW() + INTERVAL '30 minutes',%s,%s,%s)",
                (sid, slot_id, _h64(sid), json.dumps({"seed": True}),
                 _uid("idem"), _h64("req-" + sid), TENANT))
    finally:
        conn.close()
    return sid


def _insert_live_command(slot_id: str, snapshot_id: str, worker: int):
    conn = _conn()
    try:
        cid = _uid(f"cmd{worker}")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO defgeo_publish_commands "
                "(publish_command_id, publish_slot_id, decision_snapshot_id, "
                " decision_snapshot_hash, command_canonical_hash, tenant_owner_id, "
                " actor_user_id, brand_id, publish_item_request_id, article_revision_id, "
                " article_hash, public_media_key, canonical_root_domain_key, funding_policy, "
                " principal_kind, payer_user_id, exact_settlement_points, freeze_id, "
                " freeze_task_ref, freeze_backend, idempotency_key, request_canonical_hash) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'artrev-1','arthash-1',%s,%s,"
                "'personal_wallet','service_provider',%s,100,4001,%s,'wallet',%s,%s)",
                (cid, slot_id, snapshot_id, _h64(snapshot_id), _h64("cc-" + cid), TENANT,
                 TENANT, BRAND, _uid("pir"), _h64("media"), _h64("domain"), TENANT,
                 _uid("ftr"), _uid(f"idem{worker}"), _h64("rq-" + cid)))
        conn.commit()
        return True, None, None
    except psycopg2.Error as exc:
        conn.rollback()
        return False, getattr(exc.diag, "constraint_name", None), type(exc).__name__
    finally:
        conn.close()


def test_med20_twenty_concurrent_commands_on_one_slot_exactly_one_winner():
    """换 HTTP key 都不能顺序二发二扣 —— 这里打的是它的**并发**形态。"""
    slot_ids = _commit_slots(1)
    slot = slot_ids[0]
    try:
        snap = _seed_snapshot(slot)
        results = _fan_out([(_insert_live_command, (slot, snap, i)) for i in range(FANOUT)])
        winners = [r for r in results if r[0]]
        assert len(winners) == 1, (
            f"{FANOUT} 并发产生了 {len(winners)} 条非终态 command —— 一格发两次、扣两次")
        constraints = {c for ok, c, _ in results if not ok}
        assert constraints == {"defgeo_pcmd_one_live_per_slot"}, (
            f"失败者撞的锁不是被测的那一把:{constraints}")

        conn = _conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) AS c FROM defgeo_publish_commands "
                            "WHERE publish_slot_id=%s", (slot,))
                assert cur.fetchone()["c"] == 1
        finally:
            conn.close()
    finally:
        _drop_slots(slot_ids)


def test_med20_concurrency_does_not_block_distinct_slots():
    """「必须不命中」臂:20 个不同 slot 各 1 条 live command,必须 20 全赢。"""
    slot_ids = _commit_slots(FANOUT)
    try:
        snaps = [_seed_snapshot(s) for s in slot_ids]
        results = _fan_out([(_insert_live_command, (slot_ids[i], snaps[i], i))
                            for i in range(FANOUT)])
        winners = [r for r in results if r[0]]
        assert len(winners) == FANOUT, (
            f"{FANOUT} 个不同 slot 只成功了 {len(winners)};"
            f"失败者撞的锁 = { {c for ok, c, _ in results if not ok} } —— 约束在误挡")
    finally:
        _drop_slots(slot_ids)
