"""第四班 ④:activation outbox 接进 `/s/{token}/confirm-quote` 的 TX-A。

先说清楚被测对象的形状(路线普查结论,不是猜的)
------------------------------------------------
`confirm_quote`(`api/selection_api.py`)**自己不持有任何事务** ——
它是纯编排器,全部库动作都 `asyncio.to_thread(helper)` 委派出去,每个 helper
在自己的线程里各自 `get_connection()` / 各自 commit。一次请求借 8 次连接。
所以「接进这个端点的事务」字面上不可满足,必须点名接进**哪一段**。

真正存在的、且只有一条语句的那段事务 = `_commit_frozen_quote_confirmation`
(TX-A):它把 session 从 `quoted` CAS 成 `confirmed`。激活登记接的就是它。

判据的两条主线
--------------
1. **旧入参 → 旧行为**(工单点名先保这条):不传 `accepted_snapshot_hash` 时,
   SET 子句不追加任何一列、accepted 三件组全 NULL、零 activation 行、
   响应不多一个键。存量报价 100% 走这条。
2. **enrolled v2**:accepted 三件组 + 恰一条 activation root,**同一事务**;
   任一步失败则整笔回滚,不留「客户看到确认成功、激活却没入列」的半状态。

🔴 一个普查抓出来的坑,判据必须钉住:
   `orphaned_activation_roots` 的对账是拿 outbox 反 join
   `keyword_selection_sessions.customer_confirmed_snapshot_id`。
   合流时那一列**全仓零 writer** —— 只写 outbox 不写指针,对账器永远返空、
   判据恒绿。同一个谓词写成两半,必有一半没人验。
"""

from __future__ import annotations

import json

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

pytestmark = pytest.mark.integration

_ENROLLED_SNAPSHOT = {"delivery_plan": {"schema_version": "geo-delivery-plan-v1"}}
#: 只有 mode、没有服务端 delivery_plan —— 客户端自报不算数。
_CLIENT_CLAIMED_ONLY = {"mode": "defensive", "campaign_mode": "defensive"}


def _fresh_snapshot(schema_loaded, quote_id, brand_id, *, pricing: dict,
                   clusters: dict | None = None) -> int:
    """新建一行 quote_pricing_snapshots 并返回 id。

    🔴 为什么是**新建**而不是改现有那行:生产装了
       ``reject_quote_pricing_snapshot_mutation``(BEFORE UPDATE OR DELETE)——
       报价快照不可变是真不变式,判据不该为了摆夹具去跟它打架。

    🔴 为什么每次取 ``MAX(id)+1`` 而不是写死一个 id + ON CONFLICT DO NOTHING:
       写死 id 时,第二次跑这套判据会因为行已存在而**静默沿用上一轮的
       pricing_snapshot** —— legacy 判据会拿到 enrolled 的快照(或反过来),
       红得莫名其妙,或者更糟:假绿。取新 id 让每一跑都拿到自己要的那份。
    """
    c = psycopg2.connect(schema_loaded, cursor_factory=RealDictCursor)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(id),9200)+1 AS nid FROM quote_pricing_snapshots")
            snap_id = int(cur.fetchone()["nid"])
            # selection_session_id 是 NOT NULL —— 不猜,现查
            cur.execute("SELECT id FROM keyword_selection_sessions WHERE quote_id=%s", (quote_id,))
            session_id = int(cur.fetchone()["id"])
            # (quote_id, version) 唯一 —— version 也得现取,写死 1 第二跑就撞
            cur.execute("SELECT COALESCE(MAX(version),0)+1 AS nv FROM quote_pricing_snapshots"
                        " WHERE quote_id=%s", (quote_id,))
            version = int(cur.fetchone()["nv"])
            cur.execute(
                "INSERT INTO quote_pricing_snapshots"
                " (id,quote_id,brand_id,selection_session_id,version,reason,"
                "  calculation_version,pricing_snapshot,clusters_snapshot,snapshot_hash)"
                " VALUES (%s,%s,%s,%s,%s,'wp4-probe','v1',%s::jsonb,%s::jsonb,"
                "         repeat('9',64))",
                (snap_id, quote_id, brand_id, session_id, version,
                 json.dumps(pricing),
                 json.dumps(clusters) if clusters is not None else None),
            )
    finally:
        c.close()
    return snap_id


def _point_session(schema_loaded, quote_id, snap_id):
    """把 session 摆成 TX-A 的 WHERE 能命中的样子(status='quoted' + 指向该快照)。

    **已提交** —— TX-A 自开连接,看不见未提交的夹具行。
    accepted 三件组一起清空:迁移 042 的 CHECK 要求它们要么全空要么全齐。

    🔴 [工单 E3-4 · 2026-08-26] 受理审计五列也一起清空。
       本包的 probe 行是**跨判据复用**的:不清的话,上一条判据落下的证据
       会留到下一条身上,而下一条断言的正是"legacy 路径什么都没写"——
       于是判据的结果取决于**执行顺序**。顺序依赖的红与真红分不开。
    """
    c = psycopg2.connect(schema_loaded, cursor_factory=RealDictCursor)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            # 🔴 [工单 V3-C · C-2] quotes 也要指过去。
            #    `services.quote_pricing_snapshot.get_frozen_snapshot` 是
            #    `quotes q JOIN quote_pricing_snapshots ps ON ps.id =
            #    q.active_pricing_snapshot_id` —— 只摆 session 那一列时,走真
            #    HTTP 的判据会拿到 409 QUOTE_SNAPSHOT_REQUIRED,红得像"端点坏了"。
            #    生产上这两列本来就一起写,夹具照生产摆。
            cur.execute(
                "UPDATE quotes SET active_pricing_snapshot_id=%s WHERE id=%s",
                (snap_id, quote_id))
            cur.execute(
                "UPDATE keyword_selection_sessions SET status='quoted',"
                " active_pricing_snapshot_id=%s, customer_confirmed_snapshot_id=NULL,"
                " customer_confirmed_snapshot_hash=NULL, customer_confirmed_at=NULL,"
                " customer_confirmed_token_purpose=NULL,"
                " customer_confirmed_token_subject=NULL,"
                " customer_confirmed_actor=NULL,"
                " customer_confirmed_request_hash=NULL,"
                " customer_confirmed_request=NULL"
                " WHERE quote_id=%s",
                (snap_id, quote_id),
            )
    finally:
        c.close()


def _arm(schema_loaded, quote_id, brand_id, *, pricing: dict,
         clusters: dict | None = None) -> int:
    snap_id = _fresh_snapshot(schema_loaded, quote_id, brand_id, pricing=pricing,
                              clusters=clusters)
    _point_session(schema_loaded, quote_id, snap_id)
    return snap_id


def _read(schema_loaded, quote_id):
    c = psycopg2.connect(schema_loaded, cursor_factory=RealDictCursor)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT token,status,customer_confirmed_snapshot_id,"
                " customer_confirmed_snapshot_hash,customer_confirmed_at"
                " FROM keyword_selection_sessions WHERE quote_id=%s", (quote_id,))
            sess = cur.fetchone()
            cur.execute(
                "SELECT id,accepted_snapshot_id,quote_id,brand_id,accepted_snapshot_hash,status"
                " FROM defgeo_activation_outbox WHERE quote_id=%s", (quote_id,))
            outbox = cur.fetchall()
    finally:
        c.close()
    return sess, outbox


def _confirm(token, snap_id, *, accepted_hash=None, accepted_request=None):
    from api.selection_api import _commit_frozen_quote_confirmation

    _commit_frozen_quote_confirmation(
        token=token,
        expected_snapshot_id=int(snap_id),
        selected_tier="basic",
        final_keyword_ids="[1]",
        confirmed_at="2026-08-21 00:00:00",
        confirmed_total_price=100.0,
        accepted_snapshot_hash=accepted_hash,
        accepted_request=accepted_request,
    )


# ══════════════════════════════════════════════════════════════════════════
# 主线 1:旧入参 → 旧行为(工单点名先保这条)
# ══════════════════════════════════════════════════════════════════════════
def test_legacy_confirm_writes_no_pointer_and_no_activation(probe_rows_committed, schema_loaded):
    """不传 hash = 存量报价 = 零漂移。

    三面同时验:session 确实 confirmed(证明这条判据真的跑到了业务)、
    accepted 三件组全 NULL、activation 零行。
    只验其中一面会漏掉「确认没成但也没写激活」这种"看着也对"的假绿。
    """
    _, quote_id, brand_id = probe_rows_committed
    snap_id = _arm(schema_loaded, quote_id, brand_id, pricing=_ENROLLED_SNAPSHOT)
    sess, _ = _read(schema_loaded, quote_id)

    _confirm(sess["token"], snap_id, accepted_hash=None)

    sess, outbox = _read(schema_loaded, quote_id)
    assert sess["status"] == "confirmed", "legacy 确认本身没成功 —— 判据够不到被测行"
    assert sess["customer_confirmed_snapshot_id"] is None
    assert sess["customer_confirmed_snapshot_hash"] is None
    assert sess["customer_confirmed_at"] is None
    assert outbox == [], f"legacy 路径产生了 activation 行:{outbox}"


def test_legacy_sql_appends_nothing_to_the_set_clause():
    """结构:legacy 分支必须是「追加空片段」,不是「改写原谓词成三元式」。

    改写原谓词的问题是没法再证明"旧行为没变" —— 每个列都得逐个论证。
    追加空片段则可以逐字比对。
    """
    import inspect

    from api.selection_api import _commit_frozen_quote_confirmation

    src = inspect.getsource(_commit_frozen_quote_confirmation)
    assert '_accepted_sets = ""' in src, "legacy 分支不是空片段"
    assert "if accepted_snapshot_hash is not None:" in src, "分流不是按 hash 是否给出"
    # 原有 SET 列一个都不许少
    for col in ("status='confirmed'", "selected_tier=%s", "final_keyword_ids=%s",
                "confirmed_at=%s", "confirmed_total_price=%s",
                "clusters_data=COALESCE(%s, clusters_data)", "updated_at=NOW()"):
        assert col in src, f"原 SET 子句丢了 {col!r}"
    # 原有 WHERE 三条件一个都不许少(CAS 语义)
    assert "WHERE token=%s AND status='quoted' AND active_pricing_snapshot_id=%s" in src


def test_enrollment_key_ignores_client_claimed_mode(probe_rows_committed, schema_loaded):
    """分流键只认服务端 snapshot schema。客户端自报 mode 一个字都不读。"""
    from services.defensive_geo.commercial_milestones import is_v2_enrolled

    assert is_v2_enrolled(_CLIENT_CLAIMED_ONLY) is False
    assert is_v2_enrolled(_ENROLLED_SNAPSHOT) is True
    assert is_v2_enrolled(None) is False
    assert is_v2_enrolled({}) is False


# ══════════════════════════════════════════════════════════════════════════
# 主线 2:enrolled v2 —— 指针与 outbox 同事务,且两半必须都写
# ══════════════════════════════════════════════════════════════════════════
def test_v2_confirm_writes_pointer_and_exactly_one_root(probe_rows_committed, schema_loaded):
    _, quote_id, brand_id = probe_rows_committed
    snap_id = _arm(schema_loaded, quote_id, brand_id, pricing=_ENROLLED_SNAPSHOT)
    sess, _ = _read(schema_loaded, quote_id)
    h = "c" * 64

    _confirm(sess["token"], snap_id, accepted_hash=h)

    sess, outbox = _read(schema_loaded, quote_id)
    assert sess["status"] == "confirmed"
    assert sess["customer_confirmed_snapshot_id"] == snap_id
    assert sess["customer_confirmed_snapshot_hash"] == h
    assert sess["customer_confirmed_at"] is not None
    assert len(outbox) == 1, f"要求恰一条 activation root,实得 {len(outbox)}"
    root = outbox[0]
    assert root["accepted_snapshot_id"] == snap_id
    assert root["quote_id"] == quote_id
    assert root["brand_id"] == brand_id, "brand_id 不是从被更新的那一行取的"
    assert root["accepted_snapshot_hash"] == h
    assert root["status"] == "pending"


def test_both_halves_of_the_predicate_are_written(probe_rows_committed, schema_loaded):
    """🔴 对账器的分母必须真的够得着。

    `orphaned_activation_roots` 拿 outbox 反 join
    `keyword_selection_sessions.customer_confirmed_snapshot_id`。
    只写 outbox 不写指针,那个 join 永远匹配不上 —— 对账器返空、判据恒绿,
    而线上真的有孤儿也照样看不见。

    这条判据直接打那个 join 键:两半必须指向同一个 accepted snapshot。
    """
    _, quote_id, brand_id = probe_rows_committed
    snap_id = _arm(schema_loaded, quote_id, brand_id, pricing=_ENROLLED_SNAPSHOT)
    sess, _ = _read(schema_loaded, quote_id)
    _confirm(sess["token"], snap_id, accepted_hash="d" * 64)

    c = psycopg2.connect(schema_loaded, cursor_factory=RealDictCursor)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT o.id FROM defgeo_activation_outbox o"
                " JOIN keyword_selection_sessions s"
                "   ON s.customer_confirmed_snapshot_id = o.accepted_snapshot_id"
                "  AND s.quote_id = o.quote_id"
                " WHERE o.quote_id=%s", (quote_id,))
            joined = cur.fetchall()
    finally:
        c.close()
    assert len(joined) == 1, (
        "outbox 行 join 不上 session 指针 —— 同一个谓词写成了两半,"
        "对账器的分母是零"
    )


def test_replay_same_snapshot_yields_the_same_root(probe_rows_committed, schema_loaded):
    """重放返回原对象,不建第二个(承重的是唯一约束,不是应用层 SELECT-then-INSERT)。"""
    _, quote_id, brand_id = probe_rows_committed
    snap_id = _arm(schema_loaded, quote_id, brand_id, pricing=_ENROLLED_SNAPSHOT)
    sess, _ = _read(schema_loaded, quote_id)
    h = "e" * 64
    _confirm(sess["token"], snap_id, accepted_hash=h)
    _, first = _read(schema_loaded, quote_id)

    # 第二次:摆回 quoted 但**指向同一个 snapshot**(重放到达同一 accepted snapshot)
    _point_session(schema_loaded, quote_id, snap_id)
    sess, _ = _read(schema_loaded, quote_id)
    _confirm(sess["token"], snap_id, accepted_hash=h)
    _, second = _read(schema_loaded, quote_id)

    assert len(second) == 1, f"重放建出了第二条 root:{second}"
    assert second[0]["id"] == first[0]["id"], "重放没有返回原 root"


def test_activation_failure_rolls_back_the_whole_confirmation(
    probe_rows_committed, schema_loaded, monkeypatch
):
    """🔴 同事务的意义就在这一条:激活登记失败 ⇒ 确认整笔回滚。

    否则会留下最坏的一种半状态:**客户看到"确认成功",而激活从来没有入列** ——
    没有任何东西会去补它,因为业务侧看起来一切正常。
    """
    _, quote_id, brand_id = probe_rows_committed
    snap_id = _arm(schema_loaded, quote_id, brand_id, pricing=_ENROLLED_SNAPSHOT)
    sess, _ = _read(schema_loaded, quote_id)

    import services.defensive_geo.activation_outbox as ao

    def _boom(*a, **k):
        raise ao.ActivationEnqueueError("注入:激活登记失败")

    monkeypatch.setattr(ao, "enqueue_activation", _boom)

    with pytest.raises(ao.ActivationEnqueueError):
        _confirm(sess["token"], snap_id, accepted_hash="f" * 64)

    sess, outbox = _read(schema_loaded, quote_id)
    assert sess["status"] == "quoted", (
        f"激活登记失败了,session 却已经是 {sess['status']!r} —— "
        "客户会看到确认成功,而激活从没入列"
    )
    assert sess["customer_confirmed_snapshot_id"] is None
    assert outbox == []


# ══════════════════════════════════════════════════════════════════════════
# ④ work_admission:只留出口,消费面不接(窗C 地界)
# ══════════════════════════════════════════════════════════════════════════
#: 🔴 窗C(WP5+WP6)接线后,``work_admission`` 的生产消费方**恰等于**这一个集合。
#:
#: 这条锁的语义在本窗**翻转**了:窗B 时它断言「零生产调用者」(出口未接);
#: 窗C 把付费发布命令接上之后,它改成正向 census —— 分母仍然机械枚举,
#: 但判据从「一个都不许有」变成「只许有这些」。
#:
#: 🔴 顺带修了原锁的一个**探测洞**(窗C 2026-08-21 实测):
#:    原实现只认 ``ImportFrom(module.endswith("work_admission"))``,
#:    也就是只认 ``from ....work_admission import admit`` 这一种写法。
#:    而窗C 真实接线用的是 ``from services.defensive_geo import work_admission``
#:    —— 那是 ``module="services.defensive_geo"``,**原锁扫不到**。
#:    也就是说:如果不改这条锁,窗C 把闸接上之后它照样是绿的。
#:    「全绿骗不过变异」的前提是锁真的覆盖了被测形态,所以这里把三种写法
#:    (from X.work_admission import / from X import work_admission /
#:     import X.work_admission)全部纳入探测面。
SANCTIONED_WORK_ADMISSION_CALLERS = {
    "api/defensive_publish_api.py",
}


def _work_admission_importers(root) -> dict[str, list[str]]:
    """机械枚举生产代码里 import 了 work_admission 的文件。三种写法全覆盖。"""
    import ast

    skip = {"tests", "node_modules", "frontend", ".git", "docs", "scripts"}
    callers: dict[str, list[str]] = {}
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if any(rel.startswith(d + "/") for d in skip):
            continue
        if rel == "services/defensive_geo/work_admission.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            # ① from a.b.work_admission import admit
            if isinstance(node, ast.ImportFrom) and node.module \
                    and node.module.endswith("work_admission"):
                callers.setdefault(rel, []).append("from_module")
            # ② from a.b import work_admission
            elif isinstance(node, ast.ImportFrom) and node.module:
                if any(a.name == "work_admission" for a in node.names):
                    callers.setdefault(rel, []).append("from_package")
            # ③ import a.b.work_admission
            elif isinstance(node, ast.Import):
                if any(a.name.endswith("work_admission") for a in node.names):
                    callers.setdefault(rel, []).append("import")
    return callers


def test_work_admission_consumption_side_is_exactly_the_sanctioned_set():
    """付费动作闸的消费面 = 恰好那一处。多一处少一处都要红。

    多一处 = 有人在别处又开了一个付费入口(钱向可能不同);
    少一处 = 窗C 的接线被删了(付费发布不再过里程碑/审批/预算闸)。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    import services.defensive_geo.work_admission as wa

    exported = [n for n in vars(wa) if not n.startswith("_") and callable(getattr(wa, n, None))]
    assert exported, "work_admission 一个公开符号都没有 —— 这条锁的分母是零"

    callers = set(_work_admission_importers(root))
    assert callers == SANCTIONED_WORK_ADMISSION_CALLERS, (
        f"work_admission 生产消费方实得 {sorted(callers)},"
        f"期望恰好 {sorted(SANCTIONED_WORK_ADMISSION_CALLERS)}。"
        "多一处 = 又开了一个付费入口;少一处 = 付费发布绕过了准入闸。"
    )


def test_work_admission_detection_covers_all_three_import_forms():
    """判别力自证:三种 import 写法**都**能被探测到。

    这条存在的理由是一次真实教训 —— 原锁只覆盖第①种写法,
    而窗C 的真实接线用的是第②种,于是「接上了」和「没接」在原锁眼里一样绿。
    """
    import ast
    import pathlib
    import tempfile

    forms = [
        "from services.defensive_geo.work_admission import admit\n",
        "from services.defensive_geo import work_admission\n",
        "import services.defensive_geo.work_admission\n",
    ]
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        for i, src in enumerate(forms):
            (root / f"probe_{i}.py").write_text(src, encoding="utf-8")
        ast.parse(forms[0])                       # 三段都必须是合法 Python
        found = _work_admission_importers(root)
    assert len(found) == len(forms), (
        f"三种 import 写法只探测到 {sorted(found)} —— 探测面有洞,"
        "锁会在真接线时保持绿色(这正是原实现的缺陷)"
    )


# ══════════════════════════════════════════════════════════════════════════
# 游标形态矩阵 —— 锁住一个被真接线照出来的生产缺陷
# ══════════════════════════════════════════════════════════════════════════
def test_activation_outbox_works_under_both_cursor_shapes(probe_rows_committed, schema_loaded):
    """🔴 生产用 RealDictCursor,窗B 判据底座用 tuple 游标 —— 两种都必须过。

    这条是被真接线照出来的:`enqueue_activation` 原来按**位置**取列
    (`row[0]`),接进 `/s/{token}/confirm-quote` 的真事务时当场
    `KeyError: 0`。窗B 自己的判据全绿,因为**夹具用的游标类型是生产
    从来不会发的那一种**。

    reconciler 那两处更阴:`dict(zip(cols, r))` 在 RealDictCursor 下不抛,
    `zip` 会去遍历 dict 的**键**,安静地返回一堆值全错的行 ——
    对账器不报错,只是给出错误答案。

    所以判据打**矩阵**(两种游标 × 三个入口),不是打一个例子。
    """
    from services.defensive_geo.activation_outbox import (
        enqueue_activation, orphaned_activation_roots, unmaterialized_roots,
    )

    _, quote_id, brand_id = probe_rows_committed
    snap_id = _fresh_snapshot(schema_loaded, quote_id, brand_id, pricing=_ENROLLED_SNAPSHOT)

    seen = {}
    for label, factory in (("tuple", None), ("realdict", RealDictCursor)):
        c = psycopg2.connect(schema_loaded) if factory is None else \
            psycopg2.connect(schema_loaded, cursor_factory=factory)
        try:
            cur = c.cursor()
            root = enqueue_activation(
                cur, accepted_snapshot_id=snap_id, quote_id=quote_id,
                brand_id=brand_id, accepted_snapshot_hash="a" * 64)
            seen[label] = root.outbox_id
            # 两个 reconciler 入口也各跑一遍。
            #
            # 🔴 断言必须打**值**,不能只打键。第一版写的是「键都是字符串 + 有 id 列」,
            #    结果变异 W6(回退成 `dict(zip(cols, r))`)**存活**了 ——
            #    因为在 RealDictCursor 下 zip 遍历的是 dict 的键,产出的正是
            #    `{列名: 列名}`:键完全正常、`"id" in r` 也成立,**只有值是错的**。
            #    这就是那个缺陷最危险的地方:它不抛异常,只是安静地给出错误答案。
            for fn in (orphaned_activation_roots, unmaterialized_roots):
                rows = fn(cur, limit=5)
                assert isinstance(rows, list)
                for r in rows:
                    assert isinstance(r, dict)
                    # 腐坏的签名就是「每个值都等于自己的键」(`{列名: 列名}`)。
                    # 打这个签名而不是打某个具体列名 —— 两个 reconciler 的列集合不同,
                    # 写死列名会在其中一个上永远取不到、断言变成空转。
                    assert not all(k == v for k, v in r.items()), (
                        f"{fn.__name__} 在 {label} 游标下每个值都等于自己的键:{r} —— "
                        "zip 遍历了 dict 的键,行里装的是列名不是数据"
                    )
            # 反向对照:刚入队的这条必然 materialized_at IS NULL,
            # 所以 unmaterialized_roots **必须**非空 —— 否则上面那个 for 一次都不执行,
            # 整段断言是零分母的空转。
            fresh = unmaterialized_roots(cur, limit=50)
            assert fresh, f"{label} 游标下 unmaterialized_roots 返空 —— 上面的断言零分母"
            assert any(int(r["accepted_snapshot_id"]) == snap_id for r in fresh), (
                f"{label} 游标下找不到刚入队的那条(snap={snap_id}):"
                f"{[r.get('accepted_snapshot_id') for r in fresh][:5]}"
            )
            c.commit()
        finally:
            c.close()

    assert seen["tuple"] == seen["realdict"], (
        f"两种游标拿到不同的 root:{seen} —— 幂等应当返回同一个"
    )


def test_confirm_quote_gate_reads_server_schema_not_client_mode():
    """结构锚:端点的激活分流必须走 `is_v2_enrolled(pricing_data)`。

    行为判据够不到这一层(它们直接调 TX-A,绕过了端点的分流),所以这里用结构锚
    补上 —— 并且如实标注它是结构锚:它证明的是"读的是服务端快照",
    不是"客户端自报永远不生效"。
    """
    import ast
    import inspect
    import textwrap

    import api.selection_api as mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(mod.confirm_quote)))

    gate_args = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name == "is_v2_enrolled":
                gate_args.append(node.args[0].id if node.args and isinstance(node.args[0], ast.Name)
                                 else "<非裸变量>")
    assert gate_args == ["pricing_data"], (
        f"激活分流没有读服务端快照 pricing_data:{gate_args}"
    )

    # req.mode / req.campaign_mode 不许参与分流
    src = inspect.getsource(mod.confirm_quote)
    for banned in ("req.mode", "req.campaign_mode"):
        assert banned not in src, f"端点读了客户端自报的 {banned} —— 分流键必须来自服务端 schema"


def test_reconciler_actually_detects_a_real_orphan(probe_rows_committed, schema_loaded):
    """🔴 造一个真孤儿,证明对账器**查得出来**(而不是永远返空)。

    上一条判据里 `orphaned_activation_roots` 的分母是**零** —— 确认成功后
    每个指针都有对应的 outbox 行,所以那个循环一次都不执行。变异 W6
    (回退成 `dict(zip(cols, r))`)因此在它身上**存活**:判据够不到那一行。
    零分母的"全绿"不是证据。

    这里把 outbox 行删掉、指针留着,人为制造 §12.3 要对账的那种孤儿,
    再断言 reconciler 查得出它、且行里装的是**数据**不是列名。
    """
    from services.defensive_geo.activation_outbox import orphaned_activation_roots

    _, quote_id, brand_id = probe_rows_committed
    snap_id = _arm(schema_loaded, quote_id, brand_id, pricing=_ENROLLED_SNAPSHOT)
    sess, _ = _read(schema_loaded, quote_id)
    _confirm(sess["token"], snap_id, accepted_hash="b" * 64)

    # 制造孤儿:指针留着,outbox 行删掉(= 「已收款、activation 却丢了」)
    c = psycopg2.connect(schema_loaded, cursor_factory=RealDictCursor)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute("DELETE FROM defgeo_activation_outbox WHERE quote_id=%s", (quote_id,))
    finally:
        c.close()

    for label, factory in (("tuple", None), ("realdict", RealDictCursor)):
        c = psycopg2.connect(schema_loaded) if factory is None else \
            psycopg2.connect(schema_loaded, cursor_factory=factory)
        try:
            rows = orphaned_activation_roots(c.cursor(), limit=50)
            assert rows, f"{label} 游标下对账器查不出这个孤儿 —— 它的分母是零"
            mine = [r for r in rows
                    if str(r.get("customer_confirmed_snapshot_id")) == str(snap_id)]
            assert mine, (
                f"{label} 游标下孤儿行里没有我造的那条。行内容:{rows[:2]} —— "
                "如果值看起来像列名,那就是 zip 遍历了 dict 的键"
            )
            # 该投影选的是 `s.id AS session_id`(没有裸 `id` 列)——
            # 列名不猜,按它真正返回的那一列断言值的类型。
            assert isinstance(mine[0].get("session_id"), int), (
                f"{label} 游标下 session_id 的值是 {mine[0].get('session_id')!r},应为 int"
            )
            assert not all(k == v for k, v in mine[0].items()), (
                f"{label} 游标下孤儿行每个值都等于自己的键:{mine[0]}"
            )
        finally:
            c.close()

# ══════════════════════════════════════════════════════════════════════════
# [工单 E3-4 · P1-8 · 2026-08-26 · Codex 二审「accepted 审计不接受原样延期」]
# ══════════════════════════════════════════════════════════════════════════

def _read_acceptance(schema_loaded, quote_id) -> dict:
    """把受理证据**全部**列读回来。

    🔴 投影列表从 ``acceptance_evidence.REQUIRED_EVIDENCE_COLUMNS`` 机械生成,
       不手写 —— 上面那个 ``_read`` 只 SELECT 五列(建它时还没有审计列),
       拿它去判"证据齐不齐"会**恒判缺**:投影里根本没有那几列。
       本条第一版就是这么假红的,而假红与真红一样浪费下一个人的时间。
       让投影与判定共用同一个分母,这种错就结构性地不可能再犯。
    """
    from services.defensive_geo import acceptance_evidence as ae

    cols = ", ".join(ae.REQUIRED_EVIDENCE_COLUMNS)
    c = psycopg2.connect(schema_loaded, cursor_factory=RealDictCursor)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute(
                f"SELECT status, {cols} FROM keyword_selection_sessions "
                " WHERE quote_id=%s", (quote_id,))
            return dict(cur.fetchone())
    finally:
        c.close()


def test_e4_confirm_lands_every_irreproducible_acceptance_fact(
        probe_rows_committed, schema_loaded):
    """确认落库必须**同事务**写下五项不可重建事实。

    🔴 为什么必须真库打一发:上面 ``test_v2_confirm_writes_pointer_and_exactly_one_root``
       只读回 id/hash/at 三列 —— 新增的审计列**全是 NULL 它也照样绿**。
       换句话说,只有这一条能证明那几列真的被写了。
    🔴 分母取 ``acceptance_evidence.REQUIRED_EVIDENCE_COLUMNS``,不手抄。
    🔴 [E3 补洞 2026-08-27 · 外选 MUT-EXTE3-11] 信封由**生产的组装函数**
       ``api.selection_api._acceptance_evidence`` 现场组出来,判据不再自己搭。
       第一版是判据自己按同样的键手搭一份递进去 —— 于是被测的那一半
       (组装)根本没被驱动:把 ``_acceptance_evidence`` 里的
       ``token_subject`` 直接改成 ``None``,每一行新确认都被静默降级成
       ``legacy_unproven``,而本条照绿。**判据自己构造被测的中间值,
       就等于把被测对象换成了自己**(本仓记过)。
    """
    from api.selection_api import _acceptance_evidence
    from services.defensive_geo import acceptance_evidence as ae

    _, quote_id, brand_id = probe_rows_committed
    snap_id = _arm(schema_loaded, quote_id, brand_id, pricing=_ENROLLED_SNAPSHOT)
    sess, _ = _read(schema_loaded, quote_id)
    token = sess["token"]

    # 组装:生产的那一份。判据只提供入参,不提供答案。
    envelope = _acceptance_evidence(
        token=token, tier="basic", selected_keyword_ids=[1],
        quote_id=quote_id, brand_id=brand_id)
    _confirm(token, snap_id, accepted_hash="e" * 64, accepted_request=envelope)

    # 期望值:独立按 SSOT 纯函数算一遍,不复用上面那个信封里的值
    #(复用 = 拿被测对象的输出当自己的期望,恒等式而已)。
    payload = ae.canonical_request(token=token, tier="basic",
                                   selected_keyword_ids=[1])
    row = _read_acceptance(schema_loaded, quote_id)
    assert ae.classify_acceptance(row) == ae.EVIDENCE_PROVEN, (
        f"确认之后仍然缺证据:{ae.missing_evidence_columns(row)}")
    assert row["customer_confirmed_token_purpose"] == ae.TOKEN_PURPOSE_CONFIRM
    assert row["customer_confirmed_token_subject"] == ae.token_subject_of(
        quote_id=quote_id, brand_id=brand_id), (
        f"授权主体没落库(实得 {row['customer_confirmed_token_subject']!r})—— "
        "缺它这一行会被 classify_acceptance 判成 legacy_unproven:"
        "**新行为被静默降级成存量欠账**,activation 拿不到授权主体")
    assert row["customer_confirmed_actor"].startswith(
        ae.ACTOR_KIND_CUSTOMER_TOKEN + ":")
    assert token not in str(row["customer_confirmed_actor"]), "令牌原文落库了"
    assert row["customer_confirmed_request_hash"] == ae.canonical_request_hash(payload)
    assert dict(row["customer_confirmed_request"]) == payload


def test_e4_the_acceptance_criterion_drives_production_instead_of_hand_building():
    """🔴 上一条的判别力自证 —— 钉住"信封是谁组的"这件事本身。

    这一条对"``token_subject`` 恒 NULL"那个变异是**绿**的(它不碰库、
    也不碰组装函数),所以它不是第二把重复的锁;它守的是
    **上一条不许退回自己搭信封**:一旦有人把 ``_acceptance_evidence(...)``
    换回一个字面量 dict,上一条会立刻失去全部判别力而仍然全绿 ——
    那种退化没有任何别的判据看得见。

    两件事一起判:
      ① 上一条真的在调生产的组装函数;
      ② 它没有在调用点手搭 ``token_subject`` 这一键。
    """
    import ast
    from pathlib import Path

    src = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    target = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef)
         and n.name == "test_e4_confirm_lands_every_irreproducible_acceptance_fact"),
        None)
    assert target is not None, "找不到那条判据 —— 探针失效(改名了?)"

    # ① 组装走生产
    calls = [n for n in ast.walk(target) if isinstance(n, ast.Call)]
    assert any(getattr(c.func, "id", "") == "_acceptance_evidence" for c in calls), (
        "那条判据不再调用生产的受理证据组装函数 —— 组装那一半又没人驱动了")

    # ② 递给 _confirm 的信封不许是**字面量 dict**(那就是手搭)。
    #    判的是 AST 形状,不是文本里出现过 token_subject ——
    #    断言消息与 docstring 里当然会提到这个词,裸串扫描会把解释判成违规
    #    (本仓记过:引用裁决原文会让裸串结构锁判红)。
    confirms = [c for c in calls if getattr(c.func, "id", "") == "_confirm"]
    assert confirms, "那条判据不再走 _confirm —— 探针失效"
    for c in confirms:
        for kw in c.keywords:
            if kw.arg == "accepted_request":
                assert not isinstance(kw.value, ast.Dict), (
                    "那条判据在调用点手搭了受理信封 —— "
                    "判据自己构造被测的中间值,组装函数怎么改它都不会红")


def test_e4_a_legacy_confirm_stays_evidence_free_and_unproven(
        probe_rows_committed, schema_loaded):
    """判别力自证 + 存量口径:legacy 路径(不传 hash)一列审计都不写。

    🔴 两件事一起验:
       · 旧入参旧行为不变(SET 子句不该多出任何一列);
       · 这种行**必须**被判成 ``legacy_unproven`` 之外的那一档 ——
         它连指针都没有,是 ``absent``(还没确认过 v2 受理),
         把它判成 unproven 会让"正常的老报价"看起来像"欠账"。
    """
    from services.defensive_geo import acceptance_evidence as ae

    _, quote_id, brand_id = probe_rows_committed
    snap_id = _arm(schema_loaded, quote_id, brand_id, pricing=_CLIENT_CLAIMED_ONLY)
    # 🔴 前后对比,不看绝对值:本包的 probe 行是**复用**的,上一条判据可能
    #    已经在同一行上写过证据。断言"八列全空"会因为**别的判据的残留**而红,
    #    那种红与真红分不开(本仓记过:计数式/绝对值判据会被正常业务写过期)。
    before = _read_acceptance(schema_loaded, quote_id)
    sess, _ = _read(schema_loaded, quote_id)
    _confirm(sess["token"], snap_id, accepted_hash=None)
    after = _read_acceptance(schema_loaded, quote_id)

    assert after["status"] == "confirmed", "这条判据没跑到业务上"
    changed = [c for c in ae.REQUIRED_EVIDENCE_COLUMNS
               if before.get(c) != after.get(c)]
    assert not changed, (
        f"legacy 路径动了受理证据列 {changed} —— 旧入参必须旧行为")
    # 指针那三列在 legacy 路径上必须仍是空的:没有 v2 受理这件事发生过。
    assert ae.classify_acceptance(after) == ae.EVIDENCE_ABSENT, dict(after)


# ══════════════════════════════════════════════════════════════════════════
# [工单 V3-C · C-2 · Codex 三审 P1-4] v2 主题包确认:**真 HTTP**
# ══════════════════════════════════════════════════════════════════════════
# 🔴 为什么必须是真 HTTP,不能再用 `_confirm`(直调 TX-A):
#    本文件既有的全部判据都从 `_commit_frozen_quote_confirmation` 进,
#    于是 `confirm_quote` 端点里**组装受理信封那一段**从来没被任何判据执行过。
#    `acceptance_evidence.canonical_request` 对每条 cluster 做
#    `getattr(c, "cluster_id", None) or (c or {}).get("cluster_id")` ——
#    而请求体里的 `ClusterSelectionItem` 是 Pydantic 模型:没有 `cluster_id`
#    字段(getattr 落空),也没有 `.get` 方法 ⇒ **AttributeError ⇒ 500**,
#    而且发生在证据落库**之前**。
#    直调 TX-A 的判据 100% 绿,线上主题包客户 100% 点不了确认。
#    分界线就是"判据从哪一层进":这一条从端点进。
_V2_CLUSTERS = {
    "clusters": [
        {
            "cluster_name": "乙方合规",
            "business_tag": "合规词",
            "core_keywords": [
                {"id": 5102, "keyword": "合规审查报价", "standard": {"price": 200.0}},
                {"id": 5101, "keyword": "合规审查", "standard": {"price": 300.0}},
            ],
            "covered_keywords": [
                {"id": 6101, "keyword": "合规流程", "standard": {"price": 50.0}},
                {"id": 6102, "keyword": "合规清单", "standard": {"price": 50.0}},
            ],
        },
        {
            "cluster_name": "甲方尽调",
            "business_tag": "尽调词",
            "core_keywords": [
                {"id": 5201, "keyword": "尽调服务", "standard": {"price": 400.0}},
            ],
            "covered_keywords": [
                {"id": 6201, "keyword": "尽调流程", "standard": {"price": 60.0}},
            ],
        },
    ]
}

#: 客户真发的那份 body。**故意把包按 甲→乙 的顺序发**:规范形要求按名排序,
#: 不排序的话同一笔确认会因为前端顺序不同算出两个 hash。
_V2_BODY = {
    "tier": "standard",
    "selected_keyword_ids": [],
    "clusters_selection": [
        {"cluster_name": "甲方尽调", "selected_core_ids": [5201], "covered_count": 1},
        {"cluster_name": "乙方合规", "selected_core_ids": [5102, 5101], "covered_count": 2},
    ],
}


@pytest.fixture()
def confirm_client():
    """只挂 `selection_api.router` 的一次性 app。

    🔴 `raise_server_exceptions=False`:让端点内的异常变成 **500 响应**
       而不是把异常抛进判据 —— 否则"端点炸了"会以 `AttributeError` 的形态
       出现在 traceback 里,和"判据自己写错了"长得一样。
    🔴 不挂任何鉴权中间件:`/s/{token}/confirm-quote` 是 token-only 端点
       (C 端客户不登录)。造一个 `request.state.user` 会是生产不会发的形状。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import api.selection_api as mod

    app = FastAPI()
    app.include_router(mod.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_e5_v2_cluster_confirm_over_real_http_lands_evidence(
        probe_rows_committed, schema_loaded, confirm_client):
    """真 HTTP + 真 PG:主题包客户点确认 → 200 + 证据落库 + hash 可复算。

    修前:500(AttributeError,证据一列都没落)。
    """
    from services.defensive_geo import acceptance_evidence as ae

    _, quote_id, brand_id = probe_rows_committed
    _arm(schema_loaded, quote_id, brand_id,
         pricing=_ENROLLED_SNAPSHOT, clusters=_V2_CLUSTERS)
    sess, _ = _read(schema_loaded, quote_id)

    resp = confirm_client.post(
        f"/api/s/{sess['token']}/confirm-quote", json=_V2_BODY)

    assert resp.status_code == 200, (
        f"v2 主题包确认没走通:{resp.status_code} {resp.text[:400]}")

    after = _read_acceptance(schema_loaded, quote_id)
    assert after["status"] == "confirmed", "端点返 200 但会话没确认 —— 判据够不到业务"
    assert ae.classify_acceptance(after) == ae.EVIDENCE_PROVEN, (
        f"确认之后仍然缺证据:{ae.missing_evidence_columns(after)}")

    stored = after["customer_confirmed_request"]
    if isinstance(stored, str):
        stored = json.loads(stored)

    # ① 前提自证:这一笔请求**真的**带了主题包选择。
    #    不带的话下面三条恒真(空段落什么都断言不了),这条判据就成了摆设。
    assert stored.get("clusters_selection"), (
        "落库的 canonical request 里没有主题包段 —— 下面三条断言全部失去意义")

    # ② 规范形用的是**真实请求字段**,不是想象中的 cluster_id。
    #    分母取自 acceptance_evidence 的 SSOT,不手抄。
    got_keys = {k for c in stored["clusters_selection"] for k in c}
    assert got_keys == set(ae.CLUSTER_CANONICAL_KEYS), (
        f"规范形字段与真实请求对不上:{sorted(got_keys)}")

    # ③ 内容 + 稳定排序:发的是 甲→乙,落的必须是按名排好的 乙→甲,
    #    包内 id 也排序。前端换个顺序发不能算出第二个 hash。
    assert stored["clusters_selection"] == [
        {"cluster_name": "乙方合规", "selected_core_ids": [5101, 5102],
         "covered_count": 2},
        {"cluster_name": "甲方尽调", "selected_core_ids": [5201],
         "covered_count": 1},
    ], stored["clusters_selection"]

    # ④ hash 可复算 —— 落的那个 hash 就是这份正文算出来的,
    #    不是"我们当时算过一次"的一个不可复核的字符串。
    assert ae.canonical_request_hash(stored) == \
        after["customer_confirmed_request_hash"], "落库 hash 与正文对不上"


def test_e5_the_http_criterion_really_takes_the_cluster_leg(
        probe_rows_committed, schema_loaded, confirm_client):
    """判别力自证:上一条走的确实是**主题包腿**,不是平铺腿。

    🔴 平铺腿也会落受理证据(它一样调 `_acceptance_evidence`),只是不带
       clusters。如果 `_V2_CLUSTERS` 哪天摆歪了、端点掉进平铺分支,上一条的
       ①会红——但红的原因会被读成"canonical 没写主题包",而真因是"根本没走
       主题包"。这里把两者分开:只有主题包腿会写 `confirmed_covered_count`。
    """
    _, quote_id, brand_id = probe_rows_committed
    _arm(schema_loaded, quote_id, brand_id,
         pricing=_ENROLLED_SNAPSHOT, clusters=_V2_CLUSTERS)
    sess, _ = _read(schema_loaded, quote_id)
    assert confirm_client.post(
        f"/api/s/{sess['token']}/confirm-quote", json=_V2_BODY).status_code == 200

    c = psycopg2.connect(schema_loaded, cursor_factory=RealDictCursor)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute("SELECT clusters_data FROM keyword_selection_sessions"
                        " WHERE quote_id=%s", (quote_id,))
            raw = cur.fetchone()["clusters_data"]
    finally:
        c.close()
    data = json.loads(raw) if isinstance(raw, str) else raw
    covered = [cl.get("confirmed_covered_count") for cl in data["clusters"]]
    assert covered == [2, 1], (
        f"没走主题包腿(confirmed_covered_count={covered})—— "
        "上一条判据的主题包断言等于没打")


# ══════════════════════════════════════════════════════════════════════════
# [工单 V3-C · C-2 后半] classify_acceptance 的**真实消费者**
# ══════════════════════════════════════════════════════════════════════════
# 🔴 Codex 三审:`classify_acceptance` / 三档状态**全仓零生产调用者** ——
#    一个只被判据调用的分类器,证明的是"我们能算出这个档位",不是
#    "系统里有人按这个档位行事"。三档全是死码。
#
#    消费者选在 `orphaned_activation_roots`,理由是它是**唯一**会拿存量确认行
#    去重建 activation 事实的地方:重建之前必须先说清楚这一行的受理证据够不够。
#    `legacy_unproven` 的行照着重建 = 拿一份不可复核的受理去激活服务。
#    这里不替运维做决定(既不静默跳过、也不静默激活),只把档位与缺哪几列
#    一起交出去 —— 处置是人的裁定,可机读是我们的义务。
def _orphan_row(schema_loaded, quote_id):
    """把 outbox 行删掉,让这一笔确认变成"孤儿",再读对账器。"""
    from db.connection import get_connection
    from services.defensive_geo import activation_outbox as ao

    c = psycopg2.connect(schema_loaded, cursor_factory=RealDictCursor)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute("DELETE FROM defgeo_activation_outbox WHERE quote_id=%s",
                        (quote_id,))
            rows = ao.orphaned_activation_roots(cur)
    finally:
        c.close()
    return [r for r in rows if int(r["quote_id"]) == int(quote_id)]


def test_e5_reconciler_labels_the_acceptance_evidence_of_every_orphan(
        probe_rows_committed, schema_loaded, confirm_client):
    """两臂:证据齐的孤儿 = proven;审计列被抹掉的存量孤儿 = legacy_unproven。

    🔴 两臂缺一不可。只验 proven 那一臂时,把 `classify_acceptance` 换成
       `lambda row: "proven"` 照样全绿 —— 那正是"零判别力判据"。
    """
    from services.defensive_geo import acceptance_evidence as ae

    _, quote_id, brand_id = probe_rows_committed
    _arm(schema_loaded, quote_id, brand_id,
         pricing=_ENROLLED_SNAPSHOT, clusters=_V2_CLUSTERS)
    sess, _ = _read(schema_loaded, quote_id)
    assert confirm_client.post(
        f"/api/s/{sess['token']}/confirm-quote", json=_V2_BODY).status_code == 200

    # ── 臂 A:走完整 v2 受理的行 —— 证据齐 ──────────────────────────────
    got = _orphan_row(schema_loaded, quote_id)
    assert len(got) == 1, f"对账器没把这一笔看成孤儿:{got}"
    assert got[0]["acceptance_evidence"] == ae.EVIDENCE_PROVEN, got[0]
    assert got[0]["missing_evidence_columns"] == []

    # ── 臂 B:存量行 —— 指针三件组在、五列审计为空 ────────────────────
    #    这是 042 之前所有已确认会话的**真实形状**(CHECK 只管三件组)。
    _audit_only = [c for c in ae.REQUIRED_EVIDENCE_COLUMNS
                   if c not in ae.POINTER_COLUMNS]
    c = psycopg2.connect(schema_loaded, cursor_factory=RealDictCursor)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE keyword_selection_sessions SET "
                + ", ".join(f"{col}=NULL" for col in _audit_only)
                + " WHERE quote_id=%s", (quote_id,))
    finally:
        c.close()

    got = _orphan_row(schema_loaded, quote_id)
    assert len(got) == 1, "抹掉审计列之后对账器反而看不见它了 —— join 键选错了"
    assert got[0]["acceptance_evidence"] == ae.EVIDENCE_LEGACY_UNPROVEN, got[0]
    assert set(got[0]["missing_evidence_columns"]) == set(_audit_only), (
        f"缺列清单不对:{got[0]['missing_evidence_columns']}")


def test_e5_the_reconciler_projection_covers_the_whole_evidence_denominator():
    """投影分母 = `REQUIRED_EVIDENCE_COLUMNS`,机械展开而非手抄。

    🔴 手抄的那份漏掉哪一列,`classify_acceptance` 就会**恒判缺** ——
       对账器把每一行都标成 legacy_unproven,而没有任何判据会红
       (上一条两臂里的臂 B 照样绿:它本来就期待 unproven)。
       所以这里直接钉住"SQL 投影是从 SSOT 生成的"这件事。
    """
    import ast
    import inspect
    import textwrap

    from services.defensive_geo import acceptance_evidence as ae
    from services.defensive_geo import activation_outbox as ao

    src = inspect.getsource(ao.orphaned_activation_roots)
    tree = ast.parse(textwrap.dedent(src))
    # 必须出现一次"对 REQUIRED_EVIDENCE_COLUMNS 做推导"的形状
    comps = [n for n in ast.walk(tree)
             if isinstance(n, (ast.GeneratorExp, ast.ListComp))]
    assert any(
        any(isinstance(g.iter, ast.Attribute)
            and g.iter.attr == "REQUIRED_EVIDENCE_COLUMNS"
            for g in comp.generators)
        for comp in comps), "投影列不是从 REQUIRED_EVIDENCE_COLUMNS 机械展开的"
    # 反向自证:分母非空,否则上面那条恒真
    assert len(ae.REQUIRED_EVIDENCE_COLUMNS) >= 8
