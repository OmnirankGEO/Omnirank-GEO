"""【V4-A = Codex fix-of-fix P1-2a / P1-2b / P2-1】三条 finding 的并发与形态判据。

底 `48979fb5c` 上我先独立复现过三条(交付文 §1 贴了实输出):

    A-1  returned={'kind':'quote','id':1} · canonical=2 · 无 409     ⇒ 复现
    A-2  read=expired · concurrent=confirmed · final=**selecting**   ⇒ 复现(已确认报价被重开)
    A-3  版本 hash active/inactive 完全相同 · 锁定查询能取到 inactive 行 ⇒ 复现

🔴 关于 A-1 的语义边界(必须写在最前面,不许含糊)
------------------------------------------------
序列化点是 `pg_advisory_xact_lock`,它只对**拿这把锁的写者**成立。
生产上建报价的三处都拿了(见 `db.diagnosis_db.lock_brand_quote_serialization_point`
的调用点 census 判据),所以「建报价 ↔ 轮换」这一对是真的互斥了。
但一条**绕开生产路径的裸 INSERT**(比如判据自己随手插一行)不拿锁,
它仍然可以在复核之后、提交之前挤进来。

这不是没修干净,是这个机制的**能力边界**:新报价是 INSERT,没有旧行可以 `FOR UPDATE`;
SERIALIZABLE 也挡不住 —— T1(读 quotes 谓词 + 写 tokens)与 T2(插 quotes)之间
只有一条 rw-反依赖,不构成危险结构,PG 会认为「T1 先」是合法串行序而放行。

而即便真发生那一幕,**写出去的东西仍然是对的**:轮换的是她看到的那个 exact object
(objectRef 必传,不再进函数后自己再求一次 canonical),没有任何一个别的对象被动过。
丢的只是那一句「顺便告诉你 canonical 变了」。所以下面的判据分两组:

  · 走**生产路径**建报价 ⇒ 必 409、零轮换(工单点名那条,确定性);
  · 裸 INSERT 的那一幕 ⇒ 判据如实断言「轮换的仍是 exact object、别的对象一根汗毛没动」,
    **不假装它会 409**。
"""
from __future__ import annotations

import threading
import uuid

import psycopg2
import psycopg2.extras
import pytest

pytestmark = pytest.mark.integration


def _conn(dsn):
    c = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    c.autocommit = True
    c.cursor().execute("SET search_path = public")
    return c


def _tx_conn(dsn):
    c = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    c.autocommit = False
    c.cursor().execute("SET search_path = public")
    return c


def _bind(dsn):
    import db.connection as dbconn

    dbconn.DATABASE_URL = dsn
    dbconn._pool = None


def _seed(cur):
    cur.execute("INSERT INTO users (id, username, display_name, password_hash, email, is_active)"
                " VALUES (9410,'v4a','v4a','x','v4a@example.com',1) ON CONFLICT (id) DO NOTHING")
    cur.execute("INSERT INTO brands (name, owner_user_id) VALUES (%s,9410) RETURNING id",
                ("V4A_" + uuid.uuid4().hex[:6],))
    brand = int(cur.fetchone()["id"])
    cur.execute("INSERT INTO quotes (brand_id, created_at) VALUES (%s,'2026-08-01 10:00:00')"
                " RETURNING id", (brand,))
    qa = int(cur.fetchone()["id"])
    cur.execute("INSERT INTO client_access_tokens (quote_id, token, is_active, expires_at)"
                " VALUES (%s,%s,1,'2099-01-01')",
                (qa, "V4ATOKA" + uuid.uuid4().hex[:8].upper()))
    return brand, qa


@pytest.fixture()
def world(chain_db):
    dsn = chain_db("v4a")
    _bind(dsn)
    c = _conn(dsn)
    try:
        brand, qa = _seed(c.cursor())
    finally:
        c.close()
    return {"dsn": dsn, "brand": brand, "qa": qa}


def _active_tokens(dsn, brand):
    c = _conn(dsn)
    try:
        cur = c.cursor()
        cur.execute("SELECT t.quote_id, t.token FROM client_access_tokens t"
                    "  JOIN quotes q ON q.id = t.quote_id"
                    " WHERE q.brand_id = %s AND t.is_active = 1"
                    " ORDER BY t.quote_id, t.token", (brand,))
        return [(int(r["quote_id"]), r["token"]) for r in cur.fetchall()]
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════════
# A-1(P1-2a)门户轮换并发窗口
# ══════════════════════════════════════════════════════════════════════════
def test_v4a_a1_a_quote_created_through_the_production_path_forces_409(world):
    """🔴 工单点名那条:**走生产路径**建新报价 ⇒ 轮换必 409,且 token 零轮换。

    怎么把「guard 之后」这一刻做成确定性的
    -------------------------------------
    不靠 sleep 猜时机:判据先**替新报价那一方拿住品牌级序列化点**(它拿的就是
    生产建报价站点会拿的那一把),再让轮换在另一条线程上跑 —— 轮换进事务后
    第一件事就是拿同一把锁,于是它**阻塞**。这时插入新报价并提交、释放锁,
    轮换才拿到锁、复核 canonical、看见新报价 ⇒ 409。

    两个终态,没有阈值:要么 `CustomerLinkObjectDrifted`,要么成交。
    """
    from db.diagnosis_db import BRAND_QUOTE_LOCK_NS
    from services.defensive_geo import customer_links as cl

    before = _active_tokens(world["dsn"], world["brand"])
    assert len(before) == 1, before

    creator = _tx_conn(world["dsn"])          # 扮演「生产建报价那一方」
    ccur = creator.cursor()
    ccur.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                 (BRAND_QUOTE_LOCK_NS, world["brand"]))

    outcome = {}
    entered = threading.Event()

    def _rotate():
        entered.set()
        try:
            cl.reissue_link(kind="customer_portal", brand_id=world["brand"],
                            brand_name="V4A",
                            expected_object_ref={"kind": "quote", "id": world["qa"]})
            outcome["verdict"] = "rotated"
        except cl.CustomerLinkObjectDrifted:
            outcome["verdict"] = "drift"
        except Exception as exc:                     # noqa: BLE001
            outcome["verdict"] = "error"
            outcome["error"] = repr(exc)

    t = threading.Thread(target=_rotate, daemon=True)
    t.start()
    assert entered.wait(30), "轮换线程没起来 —— 这一次什么都没验到"

    # 轮换此刻正卡在序列化点上。新报价在这里落库并提交。
    ccur.execute("INSERT INTO quotes (brand_id, created_at)"
                 " VALUES (%s,'2026-08-27 10:00:00') RETURNING id", (world["brand"],))
    new_quote = int(ccur.fetchone()["id"])
    creator.commit()                                  # 提交 = 释放 xact 锁
    creator.close()

    t.join(timeout=60)
    assert not t.is_alive(), "轮换过了 60s 还卡着 —— 序列化点没释放?读数不可信"

    assert outcome.get("verdict") == "drift", (
        "canonical 已经变成 %s,轮换却没有给出 typed drift(实得 %r)—— "
        "「事务外 guard + 事务内盲写」的窗口还开着" % (new_quote, outcome))
    assert _active_tokens(world["dsn"], world["brand"]) == before, (
        "drift 了却动了 token —— 「一行不写」没做到:%r → %r"
        % (before, _active_tokens(world["dsn"], world["brand"])))


def test_v4a_a1_an_uncontended_rotation_still_succeeds(world):
    """配对的必须不命中:没有并发建报价时,轮换必须照常成交。

    少了它,一个「永远 drift」或「永远卡在锁上」的实现也能让上面那条绿。
    """
    from services.defensive_geo import customer_links as cl

    out = cl.reissue_link(kind="customer_portal", brand_id=world["brand"],
                          brand_name="V4A",
                          expected_object_ref={"kind": "quote", "id": world["qa"]})
    assert out is not None and out["objectRef"] == {"kind": "quote", "id": world["qa"]}, out
    active = _active_tokens(world["dsn"], world["brand"])
    assert len(active) == 1 and active[0][0] == world["qa"], active


def test_v4a_a1_the_serialization_point_is_taken_by_every_production_quote_writer():
    """机械 census:**每一处**生产建报价都要拿同一把序列化点。

    🔴 少拿一处,窗口就从那一处漏回来,而漏掉的那一处不会让任何判据变红 ——
       所以分母是 `grep INSERT INTO quotes` 机械扫出来的生产站点,不是手抄的清单。
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    prod = []
    for path in list(root.glob("db/*.py")) + [root / "server.py"]:
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"INSERT\s+INTO\s+quotes\b", text, re.I):
            prod.append((path.name, m.start(), text))
    assert len(prod) >= 3, "只扫到 %d 处生产建报价 —— 分母塌了" % len(prod)

    missing = []
    for name, pos, text in prod:
        window = text[max(0, pos - 1200):pos]
        if "lock_brand_quote_serialization_point" not in window:
            missing.append((name, text[:pos].count("\n") + 1))
    assert not missing, (
        "这些生产建报价站点没有拿品牌级序列化点 %r —— "
        "门户轮换的 canonical 复核会从这些站点漏回去" % (missing,))


def test_v4a_a1_rotation_targets_the_exact_object_not_a_refetched_canonical():
    """结构锁:轮换的目标必须来自 `expected_object_ref`,不许再求一次 canonical。

    这是 Review 裁定语义的前半句(「轮换用户看到的 exact object」)。
    行为面由上面两条守;这一条防的是下一版有人"顺手"把它改回去 ——
    改回去之后,序列化点还在、判据还绿,而轮换的对象又变成了另一个时刻的答案。
    """
    import ast
    import inspect
    import textwrap

    from services.defensive_geo import customer_links as cl

    src = textwrap.dedent(inspect.getsource(cl.reissue_link))
    tree = ast.parse(src)
    assigns = [n for n in ast.walk(tree)
               if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "quote_id" for t in n.targets)]
    assert len(assigns) == 1, "`quote_id` 的赋值点不是恰好一处(%d)" % len(assigns)
    dumped = ast.dump(assigns[0].value)
    assert "expected_object_ref" in dumped, (
        "轮换目标不是从 expected_object_ref 取的:%s —— "
        "再求一次 canonical 就是「另一个时刻的答案」" % dumped[:200])
    assert "canonical_quote_id" not in dumped, dumped[:200]


# ══════════════════════════════════════════════════════════════════════════
# A-2(P1-2b)延期把 confirmed 回退成 selecting
# ══════════════════════════════════════════════════════════════════════════
def _new_session(cur, brand, quote, token, status="expired",
                 expires_at="2020-01-01 00:00:00"):
    cur.execute("INSERT INTO keyword_selection_sessions"
                " (token, quote_id, brand_id, keywords_snapshot, expires_at, status)"
                " VALUES (%s,%s,%s,'[]',%s,%s)", (token, quote, brand, expires_at, status))


def test_v4a_a2_a_concurrent_confirm_is_never_rolled_back_by_an_extend(world):
    """🔴 靶心:延期读到 expired 之后并发推进成 confirmed ⇒ 延期必须 typed 拒绝,
    终态仍是 **confirmed**。

    确定性做法:另一条连接先 `SELECT … FOR UPDATE` 锁住那一行并改成 confirmed
    (**不提交**),延期在另一线程上跑 —— 它第一件事就是 `FOR UPDATE`,于是阻塞;
    这时对方提交,延期才拿到行、看到 confirmed、拒绝。
    没有 sleep:两个终态(拒绝 / 延成功)二选一。
    """
    from db.diagnosis_db import extend_selection_session_atomically

    tok = "v4a-" + uuid.uuid4().hex[:8]
    c = _conn(world["dsn"])
    try:
        _new_session(c.cursor(), world["brand"], world["qa"], tok)
    finally:
        c.close()

    blocker = _tx_conn(world["dsn"])
    bcur = blocker.cursor()
    bcur.execute("SELECT status FROM keyword_selection_sessions WHERE token=%s FOR UPDATE",
                 (tok,))
    assert bcur.fetchone()["status"] == "expired", "世界没造对"
    bcur.execute("UPDATE keyword_selection_sessions SET status='confirmed' WHERE token=%s",
                 (tok,))

    out = {}
    entered = threading.Event()

    def _extend():
        entered.set()
        out["result"] = extend_selection_session_atomically(tok, days=7)

    t = threading.Thread(target=_extend, daemon=True)
    t.start()
    assert entered.wait(30), "延期线程没起来"
    blocker.commit()
    blocker.close()
    t.join(timeout=60)
    assert not t.is_alive(), "延期过了 60s 还卡着"

    assert out["result"]["ok"] is False, (
        "并发推进成 confirmed 之后,延期竟然成功了:%r —— 已确认的报价被重新打开"
        % (out["result"],))
    assert out["result"]["reason"] == "status_not_extendable", out["result"]

    c = _conn(world["dsn"])
    try:
        cur = c.cursor()
        cur.execute("SELECT status FROM keyword_selection_sessions WHERE token=%s", (tok,))
        assert cur.fetchone()["status"] == "confirmed", (
            "终态不是 confirmed —— 状态被延期回退了")
    finally:
        c.close()


def test_v4a_a2_an_expired_session_still_extends_when_nobody_competes(world):
    """配对的必须不命中:没人竞争时,expired 必须照常延期并恢复 selecting。

    少了它,一个「永远拒绝」的实现也能让上面那条绿 —— 而那样按钮就永远点不动。
    """
    from db.diagnosis_db import extend_selection_session_atomically

    tok = "v4a-" + uuid.uuid4().hex[:8]
    c = _conn(world["dsn"])
    try:
        _new_session(c.cursor(), world["brand"], world["qa"], tok)
    finally:
        c.close()

    r = extend_selection_session_atomically(tok, days=7)
    assert r["ok"] is True and r["status"] == "selecting" and r["restored"] is True, r

    c = _conn(world["dsn"])
    try:
        cur = c.cursor()
        cur.execute("SELECT status, expires_at FROM keyword_selection_sessions"
                    " WHERE token=%s", (tok,))
        row = dict(cur.fetchone())
        assert row["status"] == "selecting" and str(row["expires_at"]) > "2026-", row
    finally:
        c.close()


@pytest.mark.parametrize("status", ["confirmed", "pending_payment", "active",
                                    "payment_overdue", "pricing_pending_review",
                                    "business_lines_submitted"])
def test_v4a_a2_no_advanced_status_can_be_walked_back(world, status):
    """状态机**方向锁**:已进入商业推进的每一档都不许被延期改回去。

    逐档跑而不是只跑 confirmed:掉的那一档就是可以被回退的那一档,
    而它不会让任何判据变红。
    """
    from db.diagnosis_db import extend_selection_session_atomically

    tok = "v4a-" + uuid.uuid4().hex[:8]
    c = _conn(world["dsn"])
    try:
        _new_session(c.cursor(), world["brand"], world["qa"], tok, status=status)
    finally:
        c.close()

    r = extend_selection_session_atomically(tok, days=7)
    assert r["ok"] is False and r["reason"] == "status_not_extendable", (status, r)

    c = _conn(world["dsn"])
    try:
        cur = c.cursor()
        cur.execute("SELECT status FROM keyword_selection_sessions WHERE token=%s", (tok,))
        assert cur.fetchone()["status"] == status, "状态被动了"
    finally:
        c.close()


def test_v4a_a2_the_direction_lock_set_matches_the_live_expiry_exemption_set():
    """🔴 方向锁那份名单是**第二份字面量** —— 从现役那一侧的 AST 机械抽出来逐字比。

    手抄的清单掉一档不会让任何判据变红,而掉的那一档正是可以被回退的那一档。
    分母来源:`api/selection_api._check_expired_locked` 里那个 status 元组
    (去掉 `expired` 本身 —— 它是**被**恢复的那一个,不是不许回退的那一个)。
    """
    import ast
    import inspect
    import textwrap

    import api.selection_api as sel
    from db.diagnosis_db import NON_REVERSIBLE_SELECTION_STATUSES

    tree = ast.parse(textwrap.dedent(inspect.getsource(sel._check_expired_locked)))
    live = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.In):
            for elt in getattr(node.comparators[0], "elts", []):
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    live.add(elt.value)
    assert live, "从现役那一侧抽不到 status 集合 —— 这条锁在守空气"
    assert set(NON_REVERSIBLE_SELECTION_STATUSES) == live - {"expired"}, (
        "方向锁名单与现役豁免集合对不上:我这份 %r · 现役 %r"
        % (sorted(NON_REVERSIBLE_SELECTION_STATUSES), sorted(live - {"expired"})))


def test_v4a_a2_the_live_endpoint_uses_the_same_atomic_primitive():
    """现役 `extend_session` 端点必须走**同一个**原子原语。

    只修 defgeo 这一侧的话,同一个业务动作会有两份实现,
    而没被修的那一份仍然能把 confirmed 写回 selecting —— 且没有判据在守它。
    """
    import inspect

    import api.selection_api as sel

    src = inspect.getsource(sel.extend_session)
    assert "extend_selection_session_atomically" in src, (
        "现役 extend_session 没走原子原语 —— 同一谓词又变成两份实现")
    assert "update_session(" not in src, (
        "现役 extend_session 里还留着盲写 update_session —— 那条路径没被修")


# ══════════════════════════════════════════════════════════════════════════
# A-3(P2-1)价目 preview 后停用 ⇒ typed,不是裸 500
# ══════════════════════════════════════════════════════════════════════════
def test_v4a_a3_the_lock_query_refuses_an_inactive_pricing_row(world):
    """🔴 锁定查询必须只认**在售**的价目行。

    这是 P2-1 的病根:锁定查询取得到 inactive 行、版本 hash 又不含 is_active,
    于是版本闸完全对得上,一路走到 active-only 的钱包查询处抛 ValueError,
    被兜底翻成裸 500。
    """
    import api.defensive_geo_api as mod

    c = _conn(world["dsn"])
    try:
        cur = c.cursor()
        cur.execute("INSERT INTO feature_pricing (feature_code, feature_name, cost_points)"
                    " VALUES ('geo_diagnosis','GEO 诊断',650)"
                    " ON CONFLICT (feature_code) DO UPDATE SET cost_points=650")
        cur.execute("UPDATE feature_pricing SET is_active=true WHERE feature_code='geo_diagnosis'")
        assert mod._lock_pricing_row_for_confirm(c.cursor(), "geo_diagnosis") is not None, (
            "在售的行都锁不到 —— 那是把正确的库也拒了,比原来的洞更贵")

        cur.execute("UPDATE feature_pricing SET is_active=false WHERE feature_code='geo_diagnosis'")
        with pytest.raises(mod.PricingCatalogUnreadable):
            mod._lock_pricing_row_for_confirm(c.cursor(), "geo_diagnosis")
    finally:
        c.close()


def test_v4a_a3_confirm_maps_an_unreadable_catalog_to_a_typed_error_with_a_way_out():
    """`PricingCatalogUnreadable` 在 confirm 里必须翻成 **typed** 错误,
    且 `nextAction` 是**重新预览**而不是「联系客服」。

    P2-1 里资金侧是安全的(Codex 实证三表皆 0),坏的是**错误形态**:
    她拿到的是"稍后重试",而再试一百次也不会成功。
    谓词走 AST 只认代码不认注释。
    """
    import ast
    import inspect
    import textwrap

    import api.defensive_geo_api as mod

    src = textwrap.dedent(inspect.getsource(mod))
    tree = ast.parse(src)
    handlers = [n for n in ast.walk(tree)
                if isinstance(n, ast.ExceptHandler)
                and getattr(n.type, "id", None) == "PricingCatalogUnreadable"]
    assert len(handlers) >= 2, (
        "PricingCatalogUnreadable 的处理点少于两处(preview + confirm)——分母塌了")
    for h in handlers:
        dumped = ast.dump(h)
        assert "POLICY_UNAVAILABLE" in dumped, (
            "有一处没翻成 typed POLICY_UNAVAILABLE:%s" % dumped[:160])
        assert "INTERNAL_ERROR" not in dumped, dumped[:160]
    # confirm 那一处(行号更大的那个)必须给「重新预览」这条出路
    confirm_handler = max(handlers, key=lambda n: n.lineno)
    assert "new_preview" in ast.dump(confirm_handler), (
        "confirm 里价目读不出来时给的不是「重新预览」—— "
        "而这一支现在的主因是「商品在她预览之后被停用了」,那是她自己能处理的")
