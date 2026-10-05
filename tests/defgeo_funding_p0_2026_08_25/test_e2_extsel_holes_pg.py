"""【外选 E2 · 判据洞补齐】Review 终单 V2 里 E2 那几发存活的收口(funding_p0 侧)。

这个文件里没有一条是我自己出的题。每一条对应外选草单
(`C:/AI-Test/EXTSEL_E2_DRAFT_2026-08-27.md`)里一发**在全分母下活下来**的变异,
门9 报告(`WOB_GATE9_MERGE_TIP_2026-08-27.md` §7.5)已逐发定性为「判据洞」:

  · MUT-EXTE2-08 —— 版本比对挪到锁之前。锁还在、还真锁,只是**版本与锁读脱钩**。
    靶心那条并发判据的窗口是 monkeypatch ``_freeze_exact`` 打开的,那一刻锁**已经持有**,
    所以它照样绿;canonical 结构锁只数"实现恰一处",不核「版本取自锁住的那一行」这条数据流。
  · MUT-EXTE2-12 —— 050 自证的 DO 块摘掉 ``table_schema = 'public'``。
    两条 050 判据都在**新建单 schema 库**里跑,public 里只有一张表,谓词在不在观测等价;
    而机械枚举锁 ``test_e2_every_schema_query_in_the_guard_is_schema_qualified`` 的分母
    = 守卫的 AST,**够不到 .sql**。轴的作用域是「守卫 + 迁移两处」,枚举锁只圈了一处。

  · MUT-EXTE2-07 —— 锁/读价目那一行时,SQL 实参被写死成 ``"geo_diagnosis"``。
    见文件末尾那一段(2026-08-27 补;**上一版我误报成「Review 已裁冗余」,查无此裁**)。

🔴 不在本文件里的一发,以及为什么:
  · MUT-EXTE2-04(多池 order 反转)门9 定性为**分母洞**不是判据洞 —— 能杀它的判据仓里
    已经有(``tests/p03_settlement_2026_08_24`` 那条逐池断言),再写一条等于写重。
"""
from __future__ import annotations

import ast
import inspect
import re
import textwrap
import uuid
from pathlib import Path

import psycopg2
import pytest

from tests.defgeo_funding_p0_2026_08_25 import _world as W
from tests.defgeo_funding_p0_2026_08_25 import conftest as CT

pytestmark = pytest.mark.integration

COST = 650
REPO = Path(__file__).resolve().parents[2]


# ══════════════════════════════════════════════════════════════════════════
# MUT-EXTE2-08 —— 「confirm 比对的版本必须取自**它自己锁住的那一行**」
# ══════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def locked_world(db, migrated_dsn, monkeypatch):
    tenant, _a, _b = W.fresh_uids(3)
    with db.cursor() as cur:
        W.ensure_user(cur, tenant, "e2xs_%d" % tenant)
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
    from fastapi.testclient import TestClient
    return TestClient(W.make_app(), raise_server_exceptions=False)


def test_e2_confirm_derives_the_version_from_the_row_it_locked(
        client, locked_world, db, live_server, monkeypatch):
    """🔴 数据流判据:版本必须由**锁住的那一行**算出来,不许另起一条连接自己读。

    怎么把这条数据流变成可观测的
    ----------------------------
    把 ``_lock_pricing_row_for_confirm`` 换成一个**如实上锁、但把读回来的行下毒**的
    替身(只改 ``cost_points`` 一个字段,别的一个字不动)。于是:

      · 版本真的取自锁住那一行 ⇒ 算出来的版本与 preview 里那个对不上 ⇒
        409 ``SNAPSHOT_CHANGED``,零 run 零冻结;
      · 版本另起一条连接自己读(``_live_pricing_catalog_version``,锁外读)⇒
        毒行**影响不到它** ⇒ 版本一致 ⇒ 200 成交。

    两个世界的可观测差是 409 vs 200 —— 这正是外选 MUT-EXTE2-08 钻过去的那条缝:
    它把版本挪回锁**之前**从锁外读,锁还在、还真锁,所以靠"并发写被挡住"取签名的
    那条判据一点都看不出来(那时锁已经持有了)。

    🔴 为什么不能用"改价 → 409"来代替:那测的是版本闸本身,两个世界都会 409。
       必须让**锁读**与**锁外读**给出不同的行,差别才落在这条数据流上。
    """
    import api.defensive_geo_api as mod

    real_lock = mod._lock_pricing_row_for_confirm
    called = []

    def _poisoned_lock(cur, feature_code):
        row = real_lock(cur, feature_code)          # 真上锁,不代劳
        called.append(dict(row))
        poisoned = dict(row)
        poisoned["cost_points"] = int(row["cost_points"]) + 7
        return poisoned

    prev = W.make_preview(client, locked_world["tenant"], locked_world["brand_id"])
    assert prev["exactTotalPoints"] == COST, prev

    monkeypatch.setattr(mod, "_lock_pricing_row_for_confirm", _poisoned_lock)
    before = W.Counts(db)
    r = W.confirm(client, locked_world["tenant"], prev)
    after = W.Counts(db)

    assert called, (
        "替身一次都没被调用 —— confirm 根本没走「在自己事务里锁那一行」这一步,"
        "这条判据什么都没验到")
    assert r.status_code == 409, (
        "锁到的那一行被下了毒(cost_points 改了),confirm 却照常成交(%s)—— "
        "说明版本不是从锁住的那一行算的,而是另起一条连接自己读的。"
        "那条读与 freeze 之间又是敞开的窗口,P1-F2 原样复活:%s"
        % (r.status_code, r.text[:200]))
    assert r.json()["detail"]["code"] == "SNAPSHOT_CHANGED", r.text
    assert before.delta(after) == (0, 0, 0), (
        "被 409 拦下却留了副作用:%r" % (before.delta(after),))


def test_e2_a_faithful_lock_replacement_still_confirms(
        client, locked_world, db, live_server, monkeypatch):
    """配对的必须不命中:替身**如实**返回锁到的那一行时,confirm 必须照常 200。

    少了它,一个「凡是被 monkeypatch 过就 409」甚至「永远 409」的实现也能让上面那条绿。
    这一条把 409 的来源钉死在**毒**上,而不是钉在"我动了那个函数"上。
    """
    import api.defensive_geo_api as mod

    real_lock = mod._lock_pricing_row_for_confirm
    called = []

    def _faithful_lock(cur, feature_code):
        row = real_lock(cur, feature_code)
        called.append(dict(row))
        return row

    prev = W.make_preview(client, locked_world["tenant"], locked_world["brand_id"])
    monkeypatch.setattr(mod, "_lock_pricing_row_for_confirm", _faithful_lock)
    r = W.confirm(client, locked_world["tenant"], prev)

    assert called, "替身没被调用 —— 这条配对判据也什么都没验到"
    assert r.status_code == 200, (
        "锁到的行原样返回,confirm 却不成交(%s)—— 409 不是毒造成的:%s"
        % (r.status_code, r.text[:200]))
    run = W.run_row(db, r.json()["diagnosisCommandId"])
    assert int(W.freeze_row(db, run["freeze_id"])["amount_total"]) == COST


def test_e2_the_locked_row_is_structurally_what_feeds_the_version():
    """结构锁(与上面那条行为判据互补):confirm 里那个 ``_live_version``
    必须由 ``_lock_pricing_row_for_confirm`` 的返回值喂出来,且**上锁在前**。

    行为判据管"这一版对不对",结构锁管"下一版别悄悄挪回去":
    MUT-EXTE2-08 的形态就是**把两行对调** —— 版本先从锁外读,锁在其后才上。
    行为判据能抓住它;这条把「先后」这件事本身写成可读的规格,
    让下一个改这段的人在 diff 里就看见代价。

    谓词走 AST 不走 grep:注释与 docstring 里怎么写都不影响,
    而这几行 SQL/调用是跨行的,grep 只能看到半截。
    """
    import api.defensive_geo_api as mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(mod)))

    version_assigns = [n for n in ast.walk(tree)
                       if isinstance(n, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == "_live_version"
                               for t in n.targets)]
    assert len(version_assigns) == 1, (
        "`_live_version` 的赋值点不是恰好一处(%d)—— 分母塌了,这条锁在守空气"
        % len(version_assigns))
    va = version_assigns[0]

    fn = _enclosing_function(tree, va)
    assert fn is not None, "找不到 `_live_version` 所在的函数"

    call = va.value
    assert isinstance(call, ast.Call), ast.dump(va.value)[:200]
    fname = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
    assert fname == "_pricing_catalog_version_from_row", (
        "版本不是从「一行已经读出来的价目」派生的,而是 %r —— "
        "外选 MUT-EXTE2-08 走的就是这条缝:改成 `_live_pricing_catalog_version()`,"
        "版本又来自锁外的另一条连接" % (fname,))
    assert call.args, "`_pricing_catalog_version_from_row` 被调用时没传行"
    row_arg = call.args[0]
    assert isinstance(row_arg, ast.Name), (
        "喂给版本函数的第一个实参不是一个具名变量:%s" % ast.dump(row_arg)[:200])

    lock_assigns = [n for n in ast.walk(fn)
                    if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                    and (getattr(n.value.func, "id", None)
                         or getattr(n.value.func, "attr", None)
                         ) == "_lock_pricing_row_for_confirm"]
    assert len(lock_assigns) == 1, (
        "confirm 里给价目行上锁的地方不是恰好一处(%d)" % len(lock_assigns))
    la = lock_assigns[0]
    assert any(isinstance(t, ast.Name) and t.id == row_arg.id for t in la.targets), (
        "喂给版本函数的那一行(%s)不是上锁那一步的返回值 —— 版本与锁读脱钩了"
        % row_arg.id)
    assert la.lineno < va.lineno, (
        "上锁(第 %d 行)排在算版本(第 %d 行)**后面** —— "
        "「版本读取之后、上锁之前」那条窗口又开了" % (la.lineno, va.lineno))

    # 且同一个函数里不许再出现"锁外现读"那条路 —— 那是被这次修复摘掉的。
    fresh_reads = [n for n in ast.walk(fn)
                   if isinstance(n, ast.Call)
                   and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)
                        ) == "_live_pricing_catalog_version"]
    assert not fresh_reads, (
        "confirm 里又出现了 `_live_pricing_catalog_version`(锁外另起一条连接读)—— "
        "它读完就关连接,读完到 freeze 之间是敞开的窗口")


def _enclosing_function(tree, node):
    """找 node 所在的**最内层**函数(AST 没有 parent 指针,只能自己走一遍)。"""
    best = None
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(fn, "end_lineno", None) or fn.lineno
        if fn.lineno <= node.lineno <= end and (best is None or fn.lineno > best.lineno):
            best = fn
    return best


# ══════════════════════════════════════════════════════════════════════════
# MUT-EXTE2-12 —— 050 自证的 `table_schema = 'public'` 谓词
# ══════════════════════════════════════════════════════════════════════════
MIG_050 = "db/migration_050_diagnosis_payer_identity_2026_08_25.sql"


def _fresh_db(tag):
    name = "defgeo_p0fix_%s_%s_test" % (tag, uuid.uuid4().hex[:6])
    assert "defgeo" in name and "test" in name, "unsafe test db name"
    admin = psycopg2.connect(CT._admin_dsn())
    admin.autocommit = True
    admin.cursor().execute('CREATE DATABASE "%s"' % name)
    admin.close()
    return name, CT.ADMIN_DSN.rsplit("/", 1)[0] + "/" + name


def _drop_db(name):
    admin = psycopg2.connect(CT._admin_dsn())
    admin.autocommit = True
    admin.cursor().execute('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % name)
    admin.close()


def _mig_050_sql():
    return (REPO / MIG_050).read_text(encoding="utf-8")


def test_e2_migration_050_self_check_is_bound_to_public_not_to_whatever_schema(migrated_dsn):
    """🔴 050 的自证只许给 **public.diagnosis_runs** 发通行证。

    造法(0 行 vs 1 行,不吃排序运气)
    --------------------------------
    整个库里唯一一张 ``diagnosis_runs`` 长在 ``decoy`` schema 里,列建得**完全合格**
    (integer / 可空);``public`` 里一张都没有。``search_path = decoy, public`` 让
    ``ALTER TABLE diagnosis_runs`` 落到 decoy 那张上(现实里的对应物:多租户 schema、
    回滚副本、测试残留 —— 都是 search_path 指着别处的形态)。于是自证那条查询:

      · 带 ``table_schema='public'`` ⇒ 命中 **0 行** ⇒ ``v_type IS NULL`` ⇒ RAISE,
        拒绝给一张不在 public 的表发通行证;
      · 不带 ⇒ 恰好命中 **1 行**(decoy 那张,完全合格)⇒ **静默通过**,
        别的 schema 里的表替真表领走了通行证。

    plpgsql 的 ``SELECT … INTO`` 多行时静默取第一行 —— 所以判据故意把两个世界做成
    0 行 / 1 行,而不是 1 行 / 2 行:后者的结论会变成"哪一行排在前面"的函数。
    """
    name, dsn = _fresh_db("mig050schema")
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("CREATE SCHEMA decoy")
        cur.execute("CREATE TABLE decoy.diagnosis_runs "
                    "(run_token text, payer_user_id integer)")
        cur.execute("SET search_path = decoy, public")
        # 自证:此刻 public 里确实一张都没有,decoy 里恰好一张(否则下面的 0/1 不成立)
        cur.execute("SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name='diagnosis_runs' AND column_name='payer_user_id'")
        assert cur.fetchone()[0] == 1, "世界没造对:不带 schema 谓词应当恰好命中 1 行"
        cur.execute("SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='diagnosis_runs' "
                    "AND column_name='payer_user_id'")
        assert cur.fetchone()[0] == 0, "世界没造对:public 里不该有 diagnosis_runs"

        with pytest.raises(psycopg2.Error) as err:
            cur.execute(_mig_050_sql())
        assert "payer_user_id" in str(err.value), (
            "050 抛了,但不是自证那条:%s" % err.value)
        conn.close()
    finally:
        _drop_db(name)


def test_e2_migration_050_still_passes_when_another_schema_merely_has_a_namesake(
        migrated_dsn):
    """配对的必须不命中:别的 schema 里有一张同名表,只要 public 那张是对的,
    050 必须照常通过。

    少了它,一个「见到第二张同名表就拒」的实现也能让上面那条绿 ——
    而那会让任何多 schema 的库(生产就有 pg_catalog 之外的 schema)装不上这条迁移,
    prestart 每次部署卡死。写窄比写漏更贵,这一条守的是那一侧。
    """
    name, dsn = _fresh_db("mig050decoyok")
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("CREATE TABLE public.diagnosis_runs "
                    "(run_token text, payer_user_id integer)")
        cur.execute("CREATE SCHEMA decoy")
        cur.execute("CREATE TABLE decoy.diagnosis_runs "
                    "(run_token text, payer_user_id integer)")
        cur.execute("SET search_path = public")
        cur.execute(_mig_050_sql())          # 不抛 = 通过
        conn.close()
    finally:
        _drop_db(name)


#: 🔴 冻结的历史欠账。**不是白名单,是账单**。
#:
#: **2026-08-27:账单已结清,余额归零。** 上面那一条(041 的
#: ``SELECT string_agg(column_name…) FROM information_schema.columns
#: WHERE table_name = 'defgeo_diagnosis_run_previews'`` 没带 schema 谓词)
#: 已由 041 属主同笔补上 ``table_schema = 'public'`` —— 记账那一刻就说好了
#: "修好了这条锁也会红,提醒把它从账上划掉",现在划掉。
#:
#: 🔴 保持**空集**。下面 ``test_e2_the_frozen_bill_stays_settled`` 钉住这一点:
#:    再往里加条目必须是一次**显式**编辑(连同那条断言一起改),
#:    不许顺手把一条新欠账塞进来 —— 账单一旦能静默增长,它就变成白名单了。
_FROZEN_UNQUALIFIED: set[str] = set()


def _information_schema_statements(sql_text):
    """把一份 .sql 里每一条**碰 information_schema 的语句**机械切出来。

    先剥行注释:文档里大量 ``-- WHERE table_name=…`` 是**注释掉的示例**,
    把它们算进分母会让这条锁一上来就红几十条,红多了就没人看了。
    """
    lines = []
    for ln in sql_text.splitlines():
        i = ln.find("--")
        lines.append(ln if i < 0 else ln[:i])
    body = "\n".join(lines)
    out = []
    for m in re.finditer(r"information_schema", body):
        starts = [x.start() for x in re.finditer(r"(?i)\bselect\b", body[:m.start()])]
        s = starts[-1] if starts else max(0, m.start() - 200)
        e = body.find(";", m.start())
        out.append(" ".join(body[s:e if e != -1 else len(body)].split()))
    return out


def test_e2_the_frozen_bill_stays_settled():
    """账单余额必须是 **0**。

    这条与下面那条的分工:下面那条判「有没有新欠账」,这条判「账单本身有没有
    在悄悄变长」。少了它,任何人都可以把一条新的不合规查询写进
    ``_FROZEN_UNQUALIFIED`` 让下面那条继续绿 —— 账单就退化成白名单了
    (本仓记过:冻结例外集必须钉大小)。
    """
    assert _FROZEN_UNQUALIFIED == set(), (
        "冻结账单又有余额了:%r —— 记一笔新欠账必须是显式决定,"
        "连同本条断言一起改,并在 commit 里说明为什么这一条现在修不了"
        % sorted(_FROZEN_UNQUALIFIED))


def test_e2_every_information_schema_query_in_the_defgeo_migrations_is_schema_qualified():
    """机械枚举锁(轴的**第二处**):``table_schema`` 这根轴的作用域是
    「启动守卫 + 迁移产物」两处,而原来的枚举锁只圈了守卫那一处 ——
    外选 MUT-EXTE2-12 钻的就是这条缝(它打在 .sql 上,守卫的 AST 够不到)。

    分母是 **glob 出来的**,不是手抄的清单:``db/migration_04x/05x_*.sql``
    整个防御 GEO 班列,每一条碰 ``information_schema`` 的语句都要带
    ``table_schema``。将来谁在这个班列里新加一条忘了带,这里当场红。

    (同族教训:``DROP INDEX`` 语法上不绑表,撞名会静默删掉无辜表的索引;
     ``CREATE INDEX IF NOT EXISTS`` 按名判存不绑表。information_schema 跨 schema
     是同一件事的第三种长法。)
    """
    files = sorted((REPO / "db").glob("migration_0[45][0-9]_*.sql"))
    assert len(files) >= 12, (
        "只 glob 到 %d 个迁移文件 —— 分母塌了,这条锁在守空气" % len(files))

    total, offenders = 0, {}
    for f in files:
        for stmt in _information_schema_statements(f.read_text(encoding="utf-8",
                                                               errors="replace")):
            total += 1
            if "table_schema" not in stmt:
                offenders.setdefault(f.name, []).append(stmt[:120])
    assert total >= 11, (
        "整个班列只枚举到 %d 条 information_schema 查询 —— 分母塌了" % total)

    unexpected = {k: v for k, v in offenders.items() if k not in _FROZEN_UNQUALIFIED}
    assert not unexpected, (
        "这些迁移里的 information_schema 查询没带 table_schema —— "
        "information_schema 是跨 schema 的,不带它等于问「**任何** schema 里有没有」,"
        "别的 schema 里撞名的表会替真表领走通行证:%r" % unexpected)
    healed = _FROZEN_UNQUALIFIED - set(offenders)
    assert not healed, (
        "冻结账单上的 %r 已经修好了 —— 请把它从 `_FROZEN_UNQUALIFIED` 里划掉,"
        "否则这个集合会慢慢变成一张没人看的白名单" % sorted(healed))


# ══════════════════════════════════════════════════════════════════════════
# MUT-EXTE2-07 —— 「锁/读的那一行 = 本单 feature 的那一行」
#
# 🔴 更正:上一版交付文里我写「Review 已裁冗余」—— **查无此裁**。
#    门9 报告 §7.5 是窗口B 把它**提请** Review 定「洞 / 冗余」,我把提请读成了裁定。
#
# Review 令给了二选一:①机械枚举全部到达路径恒为 geo_diagnosis + 单一假设锁,
# 或 ②直接补判据让 07 必死。**选 ②**,理由是证据而不是偏好:
#
#   「第二个 feature」在这个仓里**不是假设,是已经存在的事实** ——
#   `services/diagnosis_question_pricing.py` 的 DIAGNOSIS_FEATURE_BY_SCOPE 就是
#   (原文写的 `server.py:3698` 的 fee_map —— 那份已于订正二十二合进上面这个
#    唯一映射:预览/守卫/扣费原先各写一份,漂了不会有任何东西报错。
#    只改指针,不动本包任何锁。)
#   ``{"geo": "geo_diagnosis", "social": "social_diagnosis", "full": "full_diagnosis"}``。
#   也就是说「恒为 geo_diagnosis」是**当下这一个端点**的事实
#   (`api/defensive_geo_api.py:665` 那句无条件字面量),不是结构不变量:
#   defgeo 这条链哪天接上 fee_map,枚举锁的分母当场作废。
#   一条会死的判据比「枚举 + 单一假设锁」便宜得多 —— 后者要有人一直看着它还准不准。
# ══════════════════════════════════════════════════════════════════════════
#: 兄弟 feature。**不是我编的产品**:见上面 fee_map。
SIBLING = "social_diagnosis"
#: 两行的 cost_points 与 requires_paid_points 都必须不同 ——
#: 一样的话「锁错行」在观测上就等价于「没锁错」(零判别力)。
SIBLING_COST = COST + 133


@pytest.fixture()
def two_priced_features(db):
    """价目表里**两个真行**:本单的 `geo_diagnosis` + 兄弟 `social_diagnosis`。

    跑完把兄弟那一行还原(本来就有就改回原值,本来没有就删掉)——
    本包是**会话共享库**,留脏会让别的判据变成「谁先跑」的函数。
    """
    with db.cursor() as cur:
        cur.execute("SELECT cost_points, requires_paid_points FROM feature_pricing "
                    "WHERE feature_code=%s", (SIBLING,))
        row = cur.fetchone()
        before = dict(row) if row else None
        W.set_pricing(cur, COST)
        cur.execute("UPDATE feature_pricing SET requires_paid_points=false "
                    "WHERE feature_code=%s", (W.FEATURE,))
        cur.execute(
            "INSERT INTO feature_pricing "
            "(feature_code, feature_name, cost_points, requires_paid_points) "
            "VALUES (%s,%s,%s,true) ON CONFLICT (feature_code) DO UPDATE SET "
            "cost_points=EXCLUDED.cost_points, "
            "requires_paid_points=EXCLUDED.requires_paid_points",
            (SIBLING, "社媒诊断", SIBLING_COST))
        # 自证世界造对了:两行**真的**在两个维度上都不同,否则下面全是零判别。
        cur.execute("SELECT feature_code, cost_points, requires_paid_points "
                    "FROM feature_pricing WHERE feature_code IN (%s,%s) "
                    "ORDER BY feature_code", (W.FEATURE, SIBLING))
        rows = {r["feature_code"]: r for r in cur.fetchall()}
    assert set(rows) == {W.FEATURE, SIBLING}, "两行没都造出来:%r" % (list(rows),)
    assert int(rows[W.FEATURE]["cost_points"]) != int(rows[SIBLING]["cost_points"]), rows
    assert bool(rows[W.FEATURE]["requires_paid_points"]) != \
        bool(rows[SIBLING]["requires_paid_points"]), rows
    yield rows
    with db.cursor() as cur:
        if before is None:
            cur.execute("DELETE FROM feature_pricing WHERE feature_code=%s", (SIBLING,))
        else:
            cur.execute("UPDATE feature_pricing SET cost_points=%s, requires_paid_points=%s "
                        "WHERE feature_code=%s",
                        (before["cost_points"], before["requires_paid_points"], SIBLING))
        W.set_pricing(cur, COST)
        cur.execute("UPDATE feature_pricing SET requires_paid_points=false "
                    "WHERE feature_code=%s", (W.FEATURE,))


def _tx_connection(dsn):
    """一条**不自动提交**的真连接:要在事务里持有 FOR SHARE,所以不能 autocommit。"""
    from psycopg2.extras import RealDictCursor
    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    conn.autocommit = False
    conn.cursor().execute("SET search_path = public")
    return conn


def _probe_update(dsn, feature_code, timeout_ms=750):
    """另一条连接上打一条 UPDATE,让 `lock_timeout` 给**终态**。

    无 sleep:`"blocked"`(55P03 拿不到行锁)/ `"committed"`(立刻改成了)二选一。
    `SET cost_points = cost_points` 不改值,但**照样要行锁** —— 要的就是锁不锁得住。
    """
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("SET lock_timeout = '%dms'" % timeout_ms)
        try:
            cur.execute("UPDATE feature_pricing SET cost_points = cost_points "
                        "WHERE feature_code=%s", (feature_code,))
            return "committed"
        except psycopg2.errors.LockNotAvailable:
            return "blocked"
    finally:
        conn.close()


@pytest.mark.parametrize("want", [SIBLING, W.FEATURE])
def test_e2_the_pricing_lock_reads_the_row_of_the_feature_it_was_asked_for(
        two_priced_features, migrated_dsn, live_server, want):
    """🔴 `_lock_pricing_row_for_confirm(cur, X)` 必须读 **X 那一行**。

    外选 MUT-EXTE2-07 把 SQL 实参写死成 ``"geo_diagnosis"``:函数签名还老老实实
    收着 ``feature_code``,读回来的却永远是同一个商品的价目。后果不是「读错一个数」:

      · 版本闸拿**别的商品**的价目算版本 ⇒ 本商品改一次价版本纹丝不动 ⇒
        A-2 那个 P0(「她确认 7,800、实际冻 8,200」)对新 feature 原样复活;
      · ``FOR SHARE`` 守着不相干的行 ⇒ 「同价切池」那条窗口(P1-F2)对新 feature 重新敞开。

    两个参数臂**同时**在:只验兄弟臂的话,一个「永远返回第二行」的实现也能绿;
    只验自身臂的话,正是被变异后的现状。
    """
    import api.defensive_geo_api as mod

    expect = two_priced_features[want]
    conn = _tx_connection(migrated_dsn)
    try:
        cur = conn.cursor()
        row = mod._lock_pricing_row_for_confirm(cur, want)
        assert str(row["feature_code"]) == str(want), (
            "问的是 %r,读回来的却是 %r —— SQL 实参没跟着参数走,"
            "版本闸与行锁都会落到别的商品上" % (want, row["feature_code"]))
        assert int(row["cost_points"]) == int(expect["cost_points"]), (
            "读回来的价不是 %s 那一行的(%r ≠ %r)"
            % (want, row["cost_points"], expect["cost_points"]))
        assert bool(row["requires_paid_points"]) == bool(expect["requires_paid_points"]), (
            "读回来的池策略不是 %s 那一行的(%r ≠ %r)"
            % (want, row["requires_paid_points"], expect["requires_paid_points"]))
    finally:
        conn.rollback()
        conn.close()


def test_e2_the_pricing_lock_locks_the_row_of_the_feature_it_was_asked_for(
        two_priced_features, migrated_dsn, live_server):
    """🔴 **锁**也必须落在被要的那一行上 —— 不是「读对了行、锁在别处」。

    上一条只看返回值;一个「读 X 的行、却对 geo_diagnosis 上锁」的实现能让它绿,
    而那样 P1-F2 的窗口照旧敞着。这一条直接看锁:在事务里持有 ``FOR SHARE`` 之后,
    从**另一条真连接**打两发 UPDATE,由 ``lock_timeout`` 给终态 ——

      · 打**兄弟**那一行 ⇒ 必须 ``blocked``(锁确实落在它身上);
      · 打 ``geo_diagnosis`` 那一行 ⇒ 必须 ``committed``(锁没有殃及无辜。
        少了这一条,一个「把整张价目表锁住」的实现也能过第一问,
        而那会把所有商品的 confirm 串行化)。

    无 sleep:两个断言都是**终态**,不是时长阈值。
    """
    import api.defensive_geo_api as mod

    conn = _tx_connection(migrated_dsn)
    try:
        cur = conn.cursor()
        row = mod._lock_pricing_row_for_confirm(cur, SIBLING)
        assert str(row["feature_code"]) == SIBLING, row      # 前置:先得读对
        assert _probe_update(migrated_dsn, SIBLING) == "blocked", (
            "锁没落在 %s 那一行上:并发 UPDATE 立刻就改成了。"
            "实参一旦被写死,FOR SHARE 就守着别的商品,"
            "本商品「版本读取之后、freeze 之前」的窗口整个敞开" % SIBLING)
        assert _probe_update(migrated_dsn, W.FEATURE) == "committed", (
            "锁殃及了 %s 那一行 —— 锁的粒度不对(整表锁?),"
            "那会把所有商品的 confirm 互相排队" % W.FEATURE)
    finally:
        conn.rollback()
        conn.close()
