"""【E2-2 = Codex 二审 P1-F2】同价切池:换池不换钱,两把保险丝都拦不住。

Codex 真 HTTP × PG16 复现的那一手:
  preview 时 `requires_paid_points=false` → **版本读取之后、freeze 之前**切成 true
  ⇒ confirm 200,总额同样 650,但物理拆分从 bonus 出钱变成 paid 出钱。

为什么既有的两把锁都没拦住:
  · **版本闸**读的是**另一条连接**(`_live_pricing_catalog_version` 自己
    `get_connection()`,读完就关)。读完到 freeze 之间是敞开的窗口 ——
    改在窗口里发生,版本闸已经走过去了;
  · **金额闸**只比**总额**,而这一手总额压根没变。

修法(见 `_lock_pricing_row_for_confirm`):在 confirm 自己的事务里对那一行
`SELECT … FOR SHARE`,并发的 `UPDATE feature_pricing` 就必须等 confirm 结束。
`freeze_points` 内部是 `get_feature_pricing(feature_code, cursor=_cursor)` ——
走的正是同一个 cursor,所以它那次读必然读到与算版本时同一行。

🔴 判据形态:**两条真连接交错**,窗口精确打在「版本读取之后、freeze 之前」。
   打不到那个窗口的判据证明不了任何事 —— 改在版本读取**之前**发生的话,
   版本闸自己就会 409,那测的是版本闸不是本次修复。
"""
from __future__ import annotations

import threading
import time

import psycopg2
import pytest
from fastapi.testclient import TestClient

from tests.defgeo_funding_p0_2026_08_25 import _world as W

pytestmark = pytest.mark.integration

COST = 650


@pytest.fixture()
def pool_world(db, migrated_dsn, monkeypatch):
    """钱包**两个池都有钱** —— 只有这样「切池」才看得见。

    单池世界里 requires_paid_points 翻不翻转,冻结拆分都一样,
    这条判据会因为世界造得不对而恒绿。
    """
    tenant, _a, _b = W.fresh_uids(3)
    with db.cursor() as cur:
        W.ensure_user(cur, tenant, "e2lock_%d" % tenant)
        W.ensure_wallet(cur, tenant, paid=1_000_000, bonus=100_000)
        W.set_pricing(cur, COST)
        cur.execute("UPDATE feature_pricing SET requires_paid_points=false "
                    "WHERE feature_code=%s", (W.FEATURE,))
        brand_id = W.new_brand(cur, tenant)
    monkeypatch.setattr("auth.brand_access.require_brand_access", lambda *a, **k: None)
    yield {"tenant": tenant, "brand_id": brand_id, "dsn": migrated_dsn}
    with db.cursor() as cur:
        W.set_pricing(cur, COST)
        cur.execute("UPDATE feature_pricing SET requires_paid_points=false "
                    "WHERE feature_code=%s", (W.FEATURE,))


@pytest.fixture()
def client():
    return TestClient(W.make_app(), raise_server_exceptions=False)


#: 第二条连接的 `lock_timeout`。这条判据的结局由**它**给出,不由主线程睡多久给出。
_WRITER_LOCK_TIMEOUT_MS = 750


@pytest.fixture()
def writer_conn(pool_world):
    """第二条**真连接**,在窗口打开**之前**就建好并验活。

    🔴 为什么非要提前建(门9 §8.3 实测:这条判据在全分母语境下约 3/7「没验到」)
    ------------------------------------------------------------------
    上一版把 `psycopg2.connect` 放在窗口**里**,而窗口是固定的 `sleep(1.0)`。
    库一忙,连接还没起来窗口就过去了 —— 判据以 `done` 空集的形式红,
    那个红的含义是「**没验到**」而不是「代码坏了」,却和真发现长得一模一样
    (门9 作者自陈差点被它带偏)。

    把连接建在窗口**外**,这个失败模式就从窗口里被整个摘出去了:
    连不上就在 fixture 上当场炸,炸在这里不会冒充判据红。
    窗口里剩下的只有一件事 —— 发那条 UPDATE。
    """
    conn = psycopg2.connect(pool_world["dsn"])
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SET lock_timeout = '%dms'" % _WRITER_LOCK_TIMEOUT_MS)
    cur.execute("SELECT 1")
    assert cur.fetchone()[0] == 1, "第二条连接建起来了却不能用 —— 这是环境问题,不是判据问题"
    yield conn
    conn.close()


def _flip_requires_paid(conn, entered, outcome):
    """在**已经建好**的连接上发那一条 UPDATE,让 `lock_timeout` 自己给出结局。

    🔴 **无 sleep**:结论来自两个**终态**之一,不是「等了多久」——
      · `"blocked"`   —— 55P03 LockNotAvailable:`FOR SHARE` 真的把写者挡在外面;
      · `"committed"` —— 立刻改成了:那一行当时根本没被锁。
    两种都是终态 ⇒ 主线程 `join` 一下就能往下走,不需要猜一个等待时长。
    上一版拿 `blocked[0] >= 0.5` 当签名 —— 那是**时长阈值**,库一慢就误判,
    库一快(锁没上)也可能碰巧过阈值。终态没有这个毛病。
    """
    cur = conn.cursor()
    entered.set()
    started = time.monotonic()
    try:
        cur.execute("UPDATE feature_pricing SET requires_paid_points=true "
                    "WHERE feature_code=%s", (W.FEATURE,))
        outcome["verdict"] = "committed"
    except psycopg2.errors.LockNotAvailable:
        outcome["verdict"] = "blocked"
    except Exception as exc:                       # noqa: BLE001
        outcome["verdict"] = "error"
        outcome["error"] = exc
    outcome["waited"] = time.monotonic() - started


def test_e2_flipping_the_pool_policy_mid_confirm_cannot_change_which_pool_pays(
        client, pool_world, db, live_server, monkeypatch, writer_conn):
    """🔴 本单的靶心:在**版本读取之后、freeze 之前**切 `requires_paid_points`,
    实际冻结的池必须仍然是**确认那一刻**的口径。

    窗口是这样打进去的:monkeypatch `_freeze_exact`,在它真正开冻之前先放行
    另一条**已经建好的**连接去 UPDATE。修复在的时候,那条 UPDATE 拿不到行锁,
    `lock_timeout` 到点后以 55P03 收场 —— freeze 读到的仍是旧口径。
    修复不在的时候,UPDATE 立刻提交,freeze 读到新口径,钱从另一个池出。

    🔴 [门9 §8.3 收口] 这一版把两处不确定性都摘掉了,一处 sleep 都不留:
      · **连接**在窗口外建(见 `writer_conn`)—— 「第二条连接没起来」不再冒充判据红;
      · **等待**由 `lock_timeout` 终结,主线程 `join` 到终态再往下走 ——
        不再靠 `sleep(1.0)` 猜时机,也不再拿「等了 ≥0.5s」当锁生效的签名。
      屏障(`entered`)只用来隔离「线程压根没起来」这一种失败,不参与判定。
    """
    _ = live_server
    import api.defensive_geo_api as mod

    entered, outcome = threading.Event(), {}
    real_freeze_exact = mod._freeze_exact

    async def _windowed(cur, **kw):
        # 此刻:版本已经算过(且行已被 FOR SHARE 锁住),freeze 还没发生 —— 正是那个窗口。
        t = threading.Thread(target=_flip_requires_paid,
                             args=(writer_conn, entered, outcome), daemon=True)
        t.start()
        assert entered.wait(30), (
            "第二条连接的线程没走到 UPDATE —— 窗口没打开,这一次什么都没验到")
        t.join(timeout=30)
        assert not t.is_alive(), (
            "UPDATE 过了 %dms 的 lock_timeout 还挂着 —— 既没被挡住也没提交,读数不可信"
            % _WRITER_LOCK_TIMEOUT_MS)
        return await real_freeze_exact(cur, **kw)

    monkeypatch.setattr(mod, "_freeze_exact", _windowed)

    prev = W.make_preview(client, pool_world["tenant"], pool_world["brand_id"])
    assert prev["exactTotalPoints"] == COST, prev
    r = W.confirm(client, pool_world["tenant"], prev)
    assert r.status_code == 200, (
        "confirm 没成功(%s)—— 这条判据要验的是「成交了但池对不对」,"
        "先得让它成交:%s" % (r.status_code, r.text[:200]))

    run = W.run_row(db, r.json()["diagnosisCommandId"])
    fz = W.freeze_row(db, run["freeze_id"])

    # 自证:那条 UPDATE 确实发出去过,并且给出了两个终态之一(不是 error、不是没跑)。
    assert outcome.get("verdict") in ("blocked", "committed"), (
        "并发 UPDATE 没有给出终态:%r —— 这一次什么都没验到,不要把它当发现"
        % (outcome or None,))

    # 🔴 确认那一刻 requires_paid_points=false ⇒ 冻结应当**用到 bonus**。
    #    切池成功的签名是:bonus 一分没出,全从 paid 走。
    assert int(fz["amount_total"]) == COST, fz
    assert int(fz["amount_bonus"] or 0) > 0, (
        "确认时的口径是「不强制现金池」(requires_paid_points=false),"
        "冻结却一分 bonus 都没用(%r)—— 池被中途换掉了:她确认的是从赠送池出,"
        "系统却动了她的现金池。总额一样,所以金额闸看不见" % dict(fz))
    # 锁生效的签名 = **终态**,不是时长:写者必须被挡到 lock_timeout 为止。
    assert outcome["verdict"] == "blocked", (
        "并发 UPDATE 没有被挡住(结局=%r,%.3fs 就走完了)—— FOR SHARE 没锁住那一行,"
        "「版本读取之后、freeze 之前」的窗口还是敞开的"
        % (outcome["verdict"], outcome.get("waited", -1)))


def test_e2_a_flip_before_the_version_read_is_still_rejected_by_the_version_gate(
        client, pool_world, db, live_server):
    """配对的必须命中(另一条臂):改发生在**版本读取之前** ⇒ 版本闸 409。

    这条钉住:我加的行锁没有把版本闸架空。少了它,一个「锁住行但不再比版本」的
    实现也能让上面那条绿 —— 而那样改价就再没人拦了。
    """
    _ = live_server
    prev = W.make_preview(client, pool_world["tenant"], pool_world["brand_id"])
    with db.cursor() as cur:            # 改在 confirm 之前 ⇒ 版本必然对不上
        cur.execute("UPDATE feature_pricing SET requires_paid_points=true "
                    "WHERE feature_code=%s", (W.FEATURE,))
    before = W.Counts(db)
    r = W.confirm(client, pool_world["tenant"], prev)
    after = W.Counts(db)
    assert r.status_code == 409, (
        "改在版本读取之前却没被 409:%s %s" % (r.status_code, r.text[:200]))
    assert r.json()["detail"]["code"] == "SNAPSHOT_CHANGED", r.text
    assert before.delta(after) == (0, 0, 0), before.delta(after)


def test_e2_an_uncontended_confirm_is_not_slowed_or_blocked(
        client, pool_world, db, live_server):
    """配对的必须不命中:没有并发写的时候,confirm 必须照常成交。

    少了它,一个「拿 FOR UPDATE 把所有 confirm 串行化」甚至「拿不到锁就拒」的
    实现也能让上面两条绿 —— 而那会把并发下单全堵死。
    这也是选 FOR SHARE 而不是 FOR UPDATE 的理由:confirm 只是读者,
    读者之间不该互相排队,它要挡的只有写者。
    """
    _ = live_server
    prev = W.make_preview(client, pool_world["tenant"], pool_world["brand_id"])
    r = W.confirm(client, pool_world["tenant"], prev)
    assert r.status_code == 200, r.text
    run = W.run_row(db, r.json()["diagnosisCommandId"])
    assert int(W.freeze_row(db, run["freeze_id"])["amount_total"]) == COST


def test_e2_the_canonical_string_has_exactly_one_implementation():
    """结构锁:版本串的 canonical 只许有**一处**实现。

    E2-2 让 confirm 用「自己事务里锁住的那一行」算版本,而 preview 仍走
    `_live_pricing_catalog_version` 自己读一次 —— 两个调用方。
    canonical 要是写成两份,迟早有一份漏掉一列(比如又忘了
    requires_paid_points),而漏掉的那一份不会让任何判据变红。

    谓词走 AST 只认代码不认字符串:注释与 docstring 里怎么描述都不影响。
    """
    import ast
    import inspect
    import textwrap

    import api.defensive_geo_api as mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(mod)))
    builders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = ast.get_source_segment(textwrap.dedent(inspect.getsource(mod)), node) or ""
        code_only = "\n".join(
            ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
        doc = ast.get_docstring(node) or ""
        for line in doc.splitlines():
            code_only = code_only.replace(line, "")
        if "requires_paid_points" in code_only and "sha256" in code_only:
            builders.append(node.name)
    assert builders == ["_pricing_catalog_version_from_row"], (
        "canonical+hash 出现在这些函数里:%r —— 只许有一处实现" % builders)
