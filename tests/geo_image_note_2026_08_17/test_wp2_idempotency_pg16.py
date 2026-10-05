"""WP2 · 原子幂等的 PG16 **真并发**判据(03 §5 头条)。

判据原文:「同 key 同 payload 20 次真并发:一个 command/batch,每项一次 freeze,
发布渠道 external-start 至多一次」;「同 key 异 payload:409,零新增行、零资金、零外调」。

🔴 为什么必须真并发而不是顺序调用 20 次:
   check-then-create 在**顺序**下表现完全正常(第 2 次查到已存在就返回),
   只有在真并发下才双开。用顺序循环测幂等 = 用一把量不到毒的尺子。
   这里用 20 条真线程 + 20 条独立连接,并用 barrier 让它们尽量同刻发车。
"""
from __future__ import annotations

import os
import pathlib
import threading
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import RealDictCursor  # noqa: E402

from services.geo_douyin.contract_idempotency import (  # noqa: E402
    COORDINATION_CLAIMED,
    RECORD_KIND_ROOT,
    IdempotencyConflict,
    claim_request,
    request_hash,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
MIGRATION = REPO / "db" / "migration_034_geo_image_note_contract_2026_08_17.sql"
PROD_SCHEMA = pathlib.Path(
    os.getenv("GEOIMG_PROD_SCHEMA_SQL", r"C:/AI-Test/.deploy_toolkit/_geoimg_prodschema_20260817.sql")
)
DSN = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DSN or not PROD_SCHEMA.is_file(),
    reason="需要 TEST_DATABASE_URL 与生产 schema 夹具",
)

IDENTITY = {
    "tenant_owner_user_id": 42, "principal_user_id": 42, "payer_user_id": 42,
    "actor_user_id": 42, "actor_kind": "owner", "organization_id": None,
    "membership_id": None, "membership_version": None,
    "payer_policy_snapshot_json": None,
}
ENDPOINT = "/api/meijiehezi/image-notes/publish-batch"


def _admin_dsn() -> str:
    return DSN.rsplit("/", 1)[0] + "/postgres"


def _db_dsn(name: str) -> str:
    return DSN.rsplit("/", 1)[0] + "/" + name


@pytest.fixture(scope="module")
def db_name():
    name = f"geoimg_test_idem_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(_admin_dsn())
    admin.autocommit = True
    admin.cursor().execute(f'CREATE DATABASE "{name}"')
    admin.close()

    conn = psycopg2.connect(_db_dsn(name), cursor_factory=RealDictCursor)
    conn.autocommit = True
    cur = conn.cursor()
    sql = "\n".join(
        line for line in PROD_SCHEMA.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not line.startswith("\\restrict") and not line.startswith("\\unrestrict")
    )
    cur.execute(sql)
    cur.execute("SET search_path = public")
    cur.execute(MIGRATION.read_text(encoding="utf-8"))
    conn.close()
    try:
        yield name
    finally:
        admin = psycopg2.connect(_admin_dsn())
        admin.autocommit = True
        admin.cursor().execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


@pytest.fixture()
def clean(db_name):
    conn = psycopg2.connect(_db_dsn(db_name), cursor_factory=RealDictCursor)
    conn.autocommit = True
    conn.cursor().execute("DELETE FROM publish_idempotency_keys")
    conn.close()
    return db_name


def _count(db_name: str, request_id: str) -> int:
    conn = psycopg2.connect(_db_dsn(db_name), cursor_factory=RealDictCursor)
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*) AS n FROM publish_idempotency_keys WHERE request_id=%s",
                    (request_id,))
        return int(cur.fetchone()["n"])
    finally:
        conn.close()


def _race(db_name: str, request_id: str, payloads: list[dict], n: int):
    """n 条真线程、n 条独立连接,barrier 同刻发车。返回 (新建次数, 回放次数, 冲突次数)。"""
    barrier = threading.Barrier(n)
    results: list[tuple[str, object]] = []
    lock = threading.Lock()

    def worker(index: int):
        payload = payloads[index % len(payloads)]
        expected = request_hash(owner_user_id=42, endpoint=ENDPOINT, payload=payload)
        conn = psycopg2.connect(_db_dsn(db_name), cursor_factory=RealDictCursor)
        try:
            cur = conn.cursor()
            barrier.wait(timeout=30)
            try:
                is_new, _row = claim_request(
                    cur, request_id=request_id, endpoint=ENDPOINT, owner_user_id=42,
                    expected_hash=expected, identity=IDENTITY,
                    record_kind=RECORD_KIND_ROOT,
                    command_id=f"pubcmd_{request_id}",
                    coordination_state=COORDINATION_CLAIMED)
                conn.commit()
                outcome = ("new" if is_new else "replay", None)
            except IdempotencyConflict as exc:
                conn.rollback()
                outcome = ("conflict", str(exc))
            except psycopg2.Error as exc:
                conn.rollback()
                outcome = ("db_error", str(exc)[:120])
        finally:
            conn.close()
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    new = sum(1 for kind, _ in results if kind == "new")
    replay = sum(1 for kind, _ in results if kind == "replay")
    conflict = sum(1 for kind, _ in results if kind == "conflict")
    errors = [msg for kind, msg in results if kind == "db_error"]
    assert not errors, f"并发中出现 DB 错误(说明 claim 不是原子的):{errors[:3]}"
    assert len(results) == n, f"只有 {len(results)}/{n} 条线程返回 —— 并发夹具没跑满"
    return new, replay, conflict


# ============================================================
# ① 同 key 同 payload · 20 次真并发 → 恰好一个
# ============================================================

def test_same_key_same_payload_20_concurrent_creates_exactly_one(clean):
    request_id = str(uuid.uuid4())
    payload = {"items": [{"media_id": 9001}], "expected_total_price_points": 9360}

    new, replay, conflict = _race(clean, request_id, [payload], n=20)

    assert new == 1, f"20 次并发产生了 {new} 个 claim —— 幂等不是原子的"
    assert replay == 19, f"回放次数 {replay},期望 19"
    assert conflict == 0
    assert _count(clean, request_id) == 1, "库里出现了多行 —— 唯一约束或 claim 逻辑失效"


def test_concurrency_fixture_really_runs_concurrently(clean):
    """反向对照:并发夹具本身必须是活的。

    分母先立住 —— 若 20 条线程实际只跑起来 1 条,上面那条"恰好一个"是恒真。
    这里用**不同** request_id 让 20 条线程各建各的:必须真出现 20 行。
    """
    ids = [str(uuid.uuid4()) for _ in range(20)]
    barrier = threading.Barrier(20)
    ok = []
    lock = threading.Lock()

    def worker(rid: str):
        payload = {"rid": rid}
        expected = request_hash(owner_user_id=42, endpoint=ENDPOINT, payload=payload)
        conn = psycopg2.connect(_db_dsn(clean), cursor_factory=RealDictCursor)
        try:
            cur = conn.cursor()
            barrier.wait(timeout=30)
            is_new, _ = claim_request(cur, request_id=rid, endpoint=ENDPOINT, owner_user_id=42,
                                      expected_hash=expected, identity=IDENTITY,
                                      record_kind=RECORD_KIND_ROOT, command_id=f"c_{rid}")
            conn.commit()
        finally:
            conn.close()
        with lock:
            ok.append(is_new)

    threads = [threading.Thread(target=worker, args=(r,)) for r in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(ok) == 20 and all(ok), f"并发夹具没跑满:{len(ok)}/20"


# ============================================================
# ② 同 key 异 payload → 409,零新增行
# ============================================================

def test_same_key_different_payload_conflicts_with_zero_new_rows(clean):
    request_id = str(uuid.uuid4())
    payload_a = {"items": [{"media_id": 9001}], "expected_total_price_points": 9360}
    payload_b = {"items": [{"media_id": 9002}], "expected_total_price_points": 9360}

    conn = psycopg2.connect(_db_dsn(clean), cursor_factory=RealDictCursor)
    cur = conn.cursor()
    is_new, _ = claim_request(
        cur, request_id=request_id, endpoint=ENDPOINT, owner_user_id=42,
        expected_hash=request_hash(owner_user_id=42, endpoint=ENDPOINT, payload=payload_a),
        identity=IDENTITY, record_kind=RECORD_KIND_ROOT, command_id=f"pubcmd_{request_id}")
    conn.commit()
    assert is_new
    before = _count(clean, request_id)

    with pytest.raises(IdempotencyConflict, match="不同内容"):
        claim_request(
            cur, request_id=request_id, endpoint=ENDPOINT, owner_user_id=42,
            expected_hash=request_hash(owner_user_id=42, endpoint=ENDPOINT, payload=payload_b),
            identity=IDENTITY, record_kind=RECORD_KIND_ROOT, command_id=f"pubcmd_{request_id}")
    conn.rollback()
    conn.close()
    assert _count(clean, request_id) == before, "冲突路径产生了新增行"


def test_same_key_different_owner_conflicts(clean):
    """跨租户拿同一个 request id → 拒绝(H0 授权,不是普通冲突)。"""
    request_id = str(uuid.uuid4())
    payload = {"x": 1}
    conn = psycopg2.connect(_db_dsn(clean), cursor_factory=RealDictCursor)
    cur = conn.cursor()
    claim_request(cur, request_id=request_id, endpoint=ENDPOINT, owner_user_id=42,
                  expected_hash=request_hash(owner_user_id=42, endpoint=ENDPOINT, payload=payload),
                  identity=IDENTITY, record_kind=RECORD_KIND_ROOT, command_id=f"c_{request_id}")
    conn.commit()
    with pytest.raises(IdempotencyConflict, match="另一个账户"):
        claim_request(cur, request_id=request_id, endpoint=ENDPOINT, owner_user_id=99,
                      expected_hash=request_hash(owner_user_id=99, endpoint=ENDPOINT, payload=payload),
                      identity={**IDENTITY, "tenant_owner_user_id": 99},
                      record_kind=RECORD_KIND_ROOT, command_id=f"c_{request_id}")
    conn.rollback()
    conn.close()


def test_same_key_different_endpoint_conflicts(clean):
    request_id = str(uuid.uuid4())
    payload = {"x": 1}
    conn = psycopg2.connect(_db_dsn(clean), cursor_factory=RealDictCursor)
    cur = conn.cursor()
    claim_request(cur, request_id=request_id, endpoint=ENDPOINT, owner_user_id=42,
                  expected_hash=request_hash(owner_user_id=42, endpoint=ENDPOINT, payload=payload),
                  identity=IDENTITY, record_kind=RECORD_KIND_ROOT, command_id=f"c_{request_id}")
    conn.commit()
    other = "/api/geo-douyin/batches"
    with pytest.raises(IdempotencyConflict, match="另一个操作"):
        claim_request(cur, request_id=request_id, endpoint=other, owner_user_id=42,
                      expected_hash=request_hash(owner_user_id=42, endpoint=other, payload=payload),
                      identity=IDENTITY, record_kind=RECORD_KIND_ROOT, command_id=f"c_{request_id}")
    conn.rollback()
    conn.close()


# ============================================================
# ②b 定点回归:ON CONFLICT 必须覆盖**全部**唯一键,不只 request_id
# ============================================================

def test_deterministic_command_id_still_replays_not_errors(clean):
    """🔴 本包自己栽过的那个坑的定点回归(2026-08-17)。

    病史:`claim_request` 原文写 `ON CONFLICT (request_id) DO NOTHING`,
    只覆盖 request_id 一把锁。但 034 还建了 `uq_publish_idem_command_id`。
    调用方按 request_id **确定性派生** command_id(非常自然的写法)时,
    并发落败者撞的是 command_id 那把锁 —— 不在冲突目标里 ⇒ PG 抛 duplicate key,
    而不是返回零行走回放。20 并发里 19 条炸错误。

    这条用**两次顺序调用 + 同一个确定性 command_id** 精确复现该场景:
    第二次必须是干净的 replay,不能抛 psycopg2.Error。
    """
    request_id = str(uuid.uuid4())
    command_id = f"pubcmd_{request_id}"       # ← 确定性派生,正是触发条件
    payload = {"x": 1}
    expected = request_hash(owner_user_id=42, endpoint=ENDPOINT, payload=payload)

    conn = psycopg2.connect(_db_dsn(clean), cursor_factory=RealDictCursor)
    try:
        cur = conn.cursor()
        is_new, _ = claim_request(cur, request_id=request_id, endpoint=ENDPOINT,
                                  owner_user_id=42, expected_hash=expected,
                                  identity=IDENTITY, record_kind=RECORD_KIND_ROOT,
                                  command_id=command_id)
        conn.commit()
        assert is_new is True

        # 第二次:必须 replay。修复前这里是 psycopg2.errors.UniqueViolation。
        is_new2, row = claim_request(cur, request_id=request_id, endpoint=ENDPOINT,
                                     owner_user_id=42, expected_hash=expected,
                                     identity=IDENTITY, record_kind=RECORD_KIND_ROOT,
                                     command_id=command_id)
        conn.commit()
        assert is_new2 is False, "第二次没走回放"
        assert row["command_id"] == command_id
    finally:
        conn.close()
    assert _count(clean, request_id) == 1


def test_command_id_stolen_by_another_request_is_a_clear_conflict(clean):
    """command_id 被**另一个** request 占用时,必须报明确冲突。

    不许静默当新建(会双开),也不许含糊成"记录消失,请重试"(把调用方 bug
    伪装成偶发故障)。
    """
    shared_command = f"pubcmd_{uuid.uuid4().hex}"
    conn = psycopg2.connect(_db_dsn(clean), cursor_factory=RealDictCursor)
    try:
        cur = conn.cursor()
        first = str(uuid.uuid4())
        claim_request(cur, request_id=first, endpoint=ENDPOINT, owner_user_id=42,
                      expected_hash=request_hash(owner_user_id=42, endpoint=ENDPOINT, payload={"a": 1}),
                      identity=IDENTITY, record_kind=RECORD_KIND_ROOT, command_id=shared_command)
        conn.commit()

        second = str(uuid.uuid4())
        with pytest.raises(IdempotencyConflict, match="已属于另一个请求"):
            claim_request(cur, request_id=second, endpoint=ENDPOINT, owner_user_id=42,
                          expected_hash=request_hash(owner_user_id=42, endpoint=ENDPOINT, payload={"a": 2}),
                          identity=IDENTITY, record_kind=RECORD_KIND_ROOT,
                          command_id=shared_command)
        conn.rollback()
        cur.execute("SELECT count(*) AS n FROM publish_idempotency_keys WHERE request_id=%s",
                    (second,))
        assert int(cur.fetchone()["n"]) == 0, "冲突路径留下了行"
    finally:
        conn.close()


def _strip_sql_comments(sql: str) -> str:
    """剥掉 `--` 注释再判。

    🔴 第一版这条锁写的是 `assert "ON CONFLICT (" not in sql`,当场被自己顶红 ——
       因为 SQL 里那段解释缺陷病史的注释**逐字**写着 `ON CONFLICT (request_id)`。
       本仓记过这个形态:判据要打**代码形态**,不能打在会命中自己注释的字符串上。
    """
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def test_claim_sql_uses_bare_on_conflict():
    """接线锁:SQL 必须是**裸** ON CONFLICT(覆盖全部唯一键)。

    没有这条,谁把冲突目标加回 `(request_id)` 就会悄悄退回原缺陷 ——
    而上面两条行为判据在**顺序**调用下仍可能碰巧不炸。
    """
    from services.geo_douyin import contract_idempotency

    statement = _strip_sql_comments(contract_idempotency._CLAIM_SQL)
    assert "ON CONFLICT DO NOTHING" in statement, "冲突目标被加回来了 —— 只覆盖一把锁"
    assert "ON CONFLICT (" not in statement, "语句里出现了带目标的 ON CONFLICT"
    # 反向对照:剥注释这一步本身要有判别力,不能把真语句也剥没了
    assert "INSERT INTO publish_idempotency_keys" in statement
    assert _strip_sql_comments("-- ON CONFLICT (request_id)\nSELECT 1") .strip() == "SELECT 1"


# ============================================================
# ③ 变异:改回 check-then-create → 上面那条必须转红
# ============================================================

def test_check_then_create_would_double_open(clean):
    """🔴 反向变异(03 §12「atomic idempotency」行:「改回 check-then-create」)。

    这条**故意**用先查后写的写法跑同一场 20 并发。它必须产生 >1 个 claim ——
    否则说明本机的并发根本没形成竞争,那么 test_same_key_same_payload_20_concurrent
    的"恰好一个"就不是原子性挣来的,而是运气。
    """
    request_id = str(uuid.uuid4())
    barrier = threading.Barrier(20)
    created = []
    lock = threading.Lock()

    def worker():
        conn = psycopg2.connect(_db_dsn(clean), cursor_factory=RealDictCursor)
        try:
            cur = conn.cursor()
            barrier.wait(timeout=30)
            # ── 缺陷写法:先查后写 ──
            cur.execute("SELECT 1 FROM publish_idempotency_keys WHERE request_id=%s", (request_id,))
            if cur.fetchone() is None:
                try:
                    cur.execute(
                        "INSERT INTO publish_idempotency_keys "
                        "(request_id,user_id,endpoint,response_json,record_kind,command_id,"
                        " coordination_state,root_version,request_hash) "
                        "VALUES (%s,42,%s,'{}'::jsonb,'root',%s,'claimed',1,'h')",
                        (request_id, ENDPOINT, f"c_{uuid.uuid4().hex}"))
                    conn.commit()
                    with lock:
                        created.append(1)
                except psycopg2.Error:
                    # 主键把第二个 INSERT 拦下了 —— 这正是"先查后写会双开"的物证:
                    # 应用层判断说"可以建",只有 DB 约束在兜底。
                    conn.rollback()
                    with lock:
                        created.append(0)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(created) > 1, (
        "先查后写的写法下只有 1 条线程认为可以建 —— 本机并发没形成竞争,"
        "那么原子 claim 那条用例的『恰好一个』是运气不是判据"
    )
    assert _count(clean, request_id) == 1, "主键兜底也失效了"
