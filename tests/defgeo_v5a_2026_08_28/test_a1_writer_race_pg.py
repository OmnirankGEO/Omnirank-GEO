"""【A-1 = Codex fix-of-fix2 P1-3】谓词写者 × 门户轮换 —— 真 PG16 行为反例。

判据形态(为什么是这个形状)
--------------------------
Codex 的反例是:**轮换过了 canonical guard 之后阻塞 → 并发 archive 提交 →
轮换仍给 quote 6 签发新 token**。要在真库里复现"轮换在那个点上阻塞",
需要一个**结构性屏障**,不能靠 sleep 赌时序。

用的屏障是 ``LOCK TABLE client_access_tokens IN EXCLUSIVE MODE``:
``generate_client_token`` 的顺序是 ``in_tx_guard(cursor)`` → ``UPDATE
client_access_tokens SET is_active=0``。所以持这把表锁,轮换就**恰好**停在
「已经过了 canonical guard、还没动任何 token」那一格 —— Codex 描述的那一格,
而且此刻它**手里握着品牌级序列化点**。

然后让 archive 在旁边跑,结论取**终态**,不取耗时:

  · 修好之后:archive 也要拿同一把品牌锁 ⇒ 拿不到 ⇒ ``55P03 LockNotAvailable``
    (给 archive 那条连接单独设 ``lock_timeout``,不是给全库设 ——
     给全库设会把**正在等表锁的轮换**也打断,屏障就没了);
  · 修好之前:archive 不拿那把锁 ⇒ 直接提交 ⇒ 放开屏障后轮换给 quote 6 签发,
    而 quote 6 此刻已经 ``deleted_at IS NOT NULL`` ⇒
    **一个 active token 指向一份已归档的报价**。

两条断言各自独立:①archive 必须被挡住(错误签名 55P03);
②终局不许存在「active token → 已归档 quote」。①红说明锁没接上,
②红说明后果真的发生了 —— 分开写是因为它们坏的方式不同。

🔴 无 sleep。等待用的是 ``pg_locks`` 上的**状态谓词**(有人在等
   client_access_tokens 的锁),不是"睡够久应该就好了"。
"""
from __future__ import annotations

import threading
import uuid

import psycopg2
import psycopg2.errors
import psycopg2.extras
import pytest

pytestmark = pytest.mark.integration

LOCK_TIMEOUT_DSN_SUFFIX = "?options=-c%20lock_timeout%3D800ms"


# ══════════════════════════════════════════════════════════════════════════
# 底座
# ══════════════════════════════════════════════════════════════════════════
def _conn(dsn):
    c = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    c.autocommit = True
    c.cursor().execute("SET search_path = public")
    return c


def _bind_db(dsn):
    """把生产代码的连接池指到这把一次性库上。

    只把 ``_pool`` 置 None(**不** closeall)—— closeall 会连带关掉别的线程
    正握在手里的那条连接,屏障就没了。
    """
    import db.connection as dbconn

    dbconn.DATABASE_URL = dsn
    dbconn._pool = None


def _seed(cur, *, owner=9510):
    cur.execute(
        "INSERT INTO users (id, username, display_name, password_hash, email, is_active) "
        "VALUES (%s,%s,%s,'x',%s,1) ON CONFLICT (id) DO NOTHING",
        (owner, "v5a_%d" % owner, "v5a", "v5a_%d@example.com" % owner))
    cur.execute("INSERT INTO brands (name, owner_user_id) VALUES (%s,%s) RETURNING id",
                ("V5A_" + uuid.uuid4().hex[:6], owner))
    return int(cur.fetchone()["id"]), owner


def _quote(cur, brand_id, created_at, *, status="draft", deleted=False):
    cur.execute(
        "INSERT INTO quotes (brand_id, created_at, status, deleted_at) "
        "VALUES (%s,%s,%s,%s) RETURNING id",
        (brand_id, created_at, status, "2026-01-01" if deleted else None))
    return int(cur.fetchone()["id"])


def _token(cur, quote_id, token, *, is_active=1):
    cur.execute(
        "INSERT INTO client_access_tokens (quote_id, token, is_active, expires_at) "
        "VALUES (%s,%s,%s,'2099-01-01')", (quote_id, token, is_active))


def _wait_for_lock_waiter(admin_cur, relname: str, *, timeout_s: float = 15.0) -> bool:
    """等到**真的有人在等**这张表的锁 —— 状态谓词,不是 sleep。"""
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        admin_cur.execute(
            "SELECT count(*) AS n FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
            " WHERE c.relname = %s AND NOT l.granted", (relname,))
        if int(admin_cur.fetchone()["n"]) >= 1:
            return True
        time.sleep(0.02)          # 轮询间隔,不是"等它好"——上面那句才是判据
    return False


def _advisory_held(admin_cur, brand_id: int) -> bool:
    from db.diagnosis_db import BRAND_QUOTE_LOCK_NS

    admin_cur.execute(
        "SELECT count(*) AS n FROM pg_locks "
        " WHERE locktype='advisory' AND classid=%s AND objid=%s AND granted",
        (BRAND_QUOTE_LOCK_NS, brand_id))
    return int(admin_cur.fetchone()["n"]) >= 1


def _pgcode(exc) -> str:
    return getattr(exc, "pgcode", None) or getattr(getattr(exc, "__cause__", None), "pgcode", "") or ""


# ══════════════════════════════════════════════════════════════════════════
# A · Codex 反例逐字复现:轮换过 guard 后阻塞 → archive 想插进来
# ══════════════════════════════════════════════════════════════════════════
def test_a1_archive_cannot_slip_in_while_rotation_holds_the_point(chain_db) -> None:
    dsn = chain_db("archrace")
    _bind_db(dsn)
    admin = _conn(dsn)
    acur = admin.cursor()

    brand_id, owner = _seed(acur)
    q5 = _quote(acur, brand_id, "2026-01-01 10:00:00")
    q6 = _quote(acur, brand_id, "2026-01-02 10:00:00")     # canonical(更新)
    _token(acur, q6, "OLDTOKEN6")

    from services.defensive_geo.customer_links import canonical_quote_id

    with admin.cursor() as c2:
        assert canonical_quote_id(c2, brand_id) == q6, "夹具没摆对:canonical 应当是 q6"

    # ── 屏障:占住 client_access_tokens 的写锁 ──────────────────────────
    barrier = psycopg2.connect(dsn)
    barrier.autocommit = False
    bcur = barrier.cursor()
    bcur.execute("LOCK TABLE client_access_tokens IN EXCLUSIVE MODE")

    rot: dict = {}

    def _rotate():
        from services.defensive_geo.customer_links import reissue_link
        try:
            rot["link"] = reissue_link(
                kind="customer_portal", brand_id=brand_id, brand_name="V5A",
                expected_object_ref={"kind": "quote", "id": q6})
        except Exception as exc:                              # noqa: BLE001
            rot["exc"] = exc

    t = threading.Thread(target=_rotate, daemon=True)
    t.start()

    try:
        parked = _wait_for_lock_waiter(acur, "client_access_tokens")
        assert parked, "轮换没有停在预期的那一格 —— 屏障失效,后面全是假绿"
        assert _advisory_held(acur, brand_id), \
            "轮换停住时**没有**握着品牌级序列化点 —— 那这条判据打的不是那把锁"

        # ── archive 在旁边跑。给它**单独**一条带 lock_timeout 的连接。 ──
        _bind_db(dsn + LOCK_TIMEOUT_DSN_SUFFIX)
        from services.artifact_archive import archive_quote

        archive_exc = None
        try:
            archive_quote(q6, actor_user_id=owner, reason="v5a race")
        except Exception as exc:                              # noqa: BLE001
            archive_exc = exc
        _bind_db(dsn)

        assert archive_exc is not None, (
            "轮换正握着品牌级序列化点,archive 却照样跑完了 —— "
            "archive 没拿那把锁,Codex 的那个窗口原样敞着")
        assert _pgcode(archive_exc) == "55P03", (
            "archive 是被挡住了,但错误签名不对:%s / %r —— "
            "期望 55P03 LockNotAvailable(等品牌锁超时)"
            % (_pgcode(archive_exc), archive_exc))

        acur.execute("SELECT deleted_at FROM quotes WHERE id=%s", (q6,))
        assert acur.fetchone()["deleted_at"] is None, "archive 被挡住了却还是把行改了"
    finally:
        barrier.rollback()
        barrier.close()
        t.join(timeout=30)

    # ── 屏障放开后轮换正常收尾:签的是 q6,而 q6 仍是 canonical ─────────
    assert "exc" not in rot, "轮换自己炸了:%r" % rot.get("exc")
    assert rot.get("link"), "轮换没有产出链接"
    assert rot["link"]["objectRef"]["id"] == q6

    acur.execute(
        "SELECT count(*) AS n FROM client_access_tokens t JOIN quotes q ON q.id=t.quote_id "
        " WHERE t.is_active=1 AND q.deleted_at IS NOT NULL")
    assert int(acur.fetchone()["n"]) == 0, (
        "存在 active token 指向一份**已归档**的报价 —— 这正是 Codex 那条反例的后果:"
        "旧 token 被撤销、新 token 指向已归档对象,客户两头都打不开")
    admin.close()


def test_a1_restore_cannot_slip_in_while_rotation_holds_the_point(chain_db) -> None:
    """对称臂:恢复也翻 ``deleted_at``,方向相反,同样必须被挡住。

    不做这一臂的话,只给 archive 补锁也能让上面那条绿 —— 而 restore 那半边
    的窗口原样敞着,且不会有任何判据变红。
    """
    dsn = chain_db("restrace")
    _bind_db(dsn)
    admin = _conn(dsn)
    acur = admin.cursor()

    brand_id, owner = _seed(acur, owner=9511)
    q5 = _quote(acur, brand_id, "2026-01-01 10:00:00")
    q6 = _quote(acur, brand_id, "2026-01-02 10:00:00", status="archived", deleted=True)
    _token(acur, q5, "OLDTOKEN5")

    barrier = psycopg2.connect(dsn)
    barrier.autocommit = False
    barrier.cursor().execute("LOCK TABLE client_access_tokens IN EXCLUSIVE MODE")

    rot: dict = {}

    def _rotate():
        from services.defensive_geo.customer_links import reissue_link
        try:
            rot["link"] = reissue_link(
                kind="customer_portal", brand_id=brand_id, brand_name="V5A",
                expected_object_ref={"kind": "quote", "id": q5})
        except Exception as exc:                              # noqa: BLE001
            rot["exc"] = exc

    t = threading.Thread(target=_rotate, daemon=True)
    t.start()
    try:
        assert _wait_for_lock_waiter(acur, "client_access_tokens"), "屏障失效"
        assert _advisory_held(acur, brand_id), "轮换没握着序列化点"

        _bind_db(dsn + LOCK_TIMEOUT_DSN_SUFFIX)
        from services.artifact_archive import restore_quote

        exc = None
        try:
            restore_quote(q6, actor_user_id=owner)
        except Exception as e:                                # noqa: BLE001
            exc = e
        _bind_db(dsn)

        assert exc is not None and _pgcode(exc) == "55P03", (
            "restore 没有被品牌锁挡住(签名 %s / %r)—— 它把一份报价放回 canonical 候选池,"
            "而轮换此刻正在给另一份签 token" % (_pgcode(exc), exc))
        acur.execute("SELECT deleted_at FROM quotes WHERE id=%s", (q6,))
        assert acur.fetchone()["deleted_at"] is not None, "restore 被挡住了却还是把行改了"
    finally:
        barrier.rollback()
        barrier.close()
        t.join(timeout=30)

    assert "exc" not in rot, "轮换自己炸了:%r" % rot.get("exc")
    admin.close()


# ══════════════════════════════════════════════════════════════════════════
# B · 四个谓词写者逐档:握着品牌锁时,每一个都必须被挡住
# ══════════════════════════════════════════════════════════════════════════
def _hold_brand_lock(dsn, brand_id):
    """另一条连接握住品牌级序列化点(不提交)。返回那条连接。"""
    from db.diagnosis_db import BRAND_QUOTE_LOCK_NS

    holder = psycopg2.connect(dsn)
    holder.autocommit = False
    holder.cursor().execute("SELECT pg_advisory_xact_lock(%s, %s)",
                            (BRAND_QUOTE_LOCK_NS, int(brand_id)))
    return holder


def _call_archive(brand_id, quote_id, owner):
    from services.artifact_archive import archive_quote
    archive_quote(quote_id, actor_user_id=owner, reason="v5a")


def _call_restore(brand_id, quote_id, owner):
    from services.artifact_archive import restore_quote
    restore_quote(quote_id, actor_user_id=owner)


def _call_delete_client(brand_id, quote_id, owner):
    import asyncio

    from api.brand_api import delete_client

    class _Req:
        class state:                                          # noqa: D106
            user = {"user_id": owner, "is_admin": True}
        headers: dict = {}

    asyncio.run(delete_client(brand_id, _Req(), reason="v5a"))


def _call_restore_client(brand_id, quote_id, owner):
    import asyncio

    from api.brand_api import restore_deleted_client

    class _Req:
        class state:                                          # noqa: D106
            user = {"user_id": owner, "is_admin": True}
        headers: dict = {}

    asyncio.run(restore_deleted_client(brand_id, _Req()))


#: 四个谓词写者 —— 与 ``test_a1_lock_census.FROZEN_PREDICATE_WRITERS`` 同一份分母。
#:
#: 两个夹具开关分开写,**不合成一个**:
#:   · ``archived_quote`` 这条路径要先有一份已归档的报价才走得到;
#:   · ``archived_brand`` 只有品牌恢复那条要 —— 给 ``restore_quote`` 也设的话,
#:     它会先撞上「客户仍在回收站中,请先恢复客户」那个业务守卫。
#:     修好的树上看不出来(品牌锁排在更前面,先抛 55P03),但**底**那一臂会
#:     因为一个与本条无关的原因而红 —— 红得对不上号,等于没验到这一档。
#:     (这就是本仓「比错误签名、别只比红不红」那条:签名一比就露馅了。)
PREDICATE_WRITER_CALLS = [
    ("artifact_archive.archive_quote", _call_archive, False, False),
    ("artifact_archive.restore_quote", _call_restore, True, False),
    ("brand_api.delete_client", _call_delete_client, False, False),
    ("brand_api.restore_deleted_client", _call_restore_client, True, True),
]


@pytest.mark.parametrize("name,call,needs_archived,needs_archived_brand",
                         PREDICATE_WRITER_CALLS,
                         ids=[c[0] for c in PREDICATE_WRITER_CALLS])
def test_a1_every_predicate_writer_blocks_on_the_brand_lock(chain_db, name, call,
                                                            needs_archived,
                                                            needs_archived_brand) -> None:
    """逐档:握着品牌级序列化点时,**每一个**谓词写者都拿不到锁。

    结论取的是**错误签名**(55P03),不是"抛没抛":
    - 没拿锁的实现会跑过去(或因别的原因抛别的码);
    - 拿了锁的实现只会是 55P03。
    两种红长得不一样,失败信息里直接印出实得的签名 ——
    「两边都红」不等于验过,得比签名。

    🔴 ``brand_api`` 那两条:品牌锁是它们事务里的**第一条语句**,
       所以就算后面因为环境原因走不完,这一格也已经被观测到了。
    """
    dsn = chain_db("writer")
    _bind_db(dsn)
    admin = _conn(dsn)
    acur = admin.cursor()

    brand_id, owner = _seed(acur, owner=9520)
    quote_id = _quote(acur, brand_id, "2026-01-02 10:00:00",
                      status="archived" if needs_archived else "draft",
                      deleted=needs_archived)
    if needs_archived_brand:
        # 只有「品牌恢复」那一条要求品牌本身处在已删态
        acur.execute("UPDATE brands SET is_deleted=TRUE, deleted_at=NOW() WHERE id=%s",
                     (brand_id,))
        acur.execute("UPDATE quotes SET archived_with_brand_at=NOW() WHERE id=%s", (quote_id,))

    holder = _hold_brand_lock(dsn, brand_id)
    try:
        assert _advisory_held(acur, brand_id), "夹具没握住那把锁 —— 这一发没跑"

        _bind_db(dsn + LOCK_TIMEOUT_DSN_SUFFIX)
        exc = None
        try:
            call(brand_id, quote_id, owner)
        except Exception as e:                                # noqa: BLE001
            exc = e
        _bind_db(dsn)

        assert exc is not None, (
            "%s 在别人握着品牌级序列化点时照样跑完了 —— 它没拿那把锁" % name)
        assert _pgcode(exc) == "55P03", (
            "%s 抛了,但签名是 %s / %r —— 期望 55P03(等品牌锁超时)。"
            "别的签名说明它是因为**别的原因**失败的,证明不了它在等那把锁。"
            % (name, _pgcode(exc) or "(无 pgcode)", exc))
    finally:
        holder.rollback()
        holder.close()
    admin.close()


# ══════════════════════════════════════════════════════════════════════════
# C · 配对的必须不命中:没有争用时,每一个写者都照常跑完
# ══════════════════════════════════════════════════════════════════════════
def test_a1_archive_still_works_without_contention(chain_db) -> None:
    """补锁不许把正常路径堵死。没有这一条,把锁写成"永远拿不到"也会让 B 组全绿。"""
    dsn = chain_db("nocontend")
    _bind_db(dsn)
    admin = _conn(dsn)
    acur = admin.cursor()

    brand_id, owner = _seed(acur, owner=9530)
    q = _quote(acur, brand_id, "2026-01-02 10:00:00")

    from services.artifact_archive import archive_quote, restore_quote

    out = archive_quote(q, actor_user_id=owner, reason="v5a ok")
    assert out.get("archived") is True, "无争用时归档失败了:%r" % out
    acur.execute("SELECT deleted_at FROM quotes WHERE id=%s", (q,))
    assert acur.fetchone()["deleted_at"] is not None

    back = restore_quote(q, actor_user_id=owner)
    assert int(back["id"]) == q
    acur.execute("SELECT deleted_at FROM quotes WHERE id=%s", (q,))
    assert acur.fetchone()["deleted_at"] is None, "无争用时恢复没生效"
    admin.close()


def test_a1_two_writers_on_the_same_brand_serialize_without_deadlock(chain_db) -> None:
    """同品牌两个写者串行通过,不成环。

    锁序统一(品牌锁永远第一把 + 每条路径最多一个品牌)的**行为**证明:
    两条都跑完,谁也没拿到 40P01。
    """
    dsn = chain_db("serialize")
    _bind_db(dsn)
    admin = _conn(dsn)
    acur = admin.cursor()

    brand_id, owner = _seed(acur, owner=9540)
    qa = _quote(acur, brand_id, "2026-01-02 10:00:00")
    qb = _quote(acur, brand_id, "2026-01-03 10:00:00")

    errs: list = []

    def _arch(qid):
        from services.artifact_archive import archive_quote
        try:
            archive_quote(qid, actor_user_id=owner, reason="v5a par")
        except Exception as e:                                # noqa: BLE001
            errs.append((qid, e))

    ts = [threading.Thread(target=_arch, args=(q,)) for q in (qa, qb)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=60)

    assert not errs, "同品牌两个归档互相打死了:%r" % errs
    acur.execute("SELECT count(*) AS n FROM quotes WHERE brand_id=%s AND deleted_at IS NOT NULL",
                 (brand_id,))
    assert int(acur.fetchone()["n"]) == 2, "两个都该归档成功"
    admin.close()
