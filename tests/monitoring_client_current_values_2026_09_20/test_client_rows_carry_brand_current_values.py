# -*- coding: utf-8 -*-
"""WO_251 §4 · 监测中心客户条要带**品牌现值**,不能只给报价快照。

Owner 截图:同一个客户,顶部品牌卡显示「揭阳阁揭阳中路雅栖酒店 · 酒店住宿…」,
下面监测中心那一条显示**旧名 + 餐饮食品** —— 两个名字、两个行业。
原因与 WO_251 ① 同源:`/api/monitoring/clients` 按 quote 键取
`q.brand_name` / `q.industry`,而那是**下单那一刻的快照**。

🔴 回落规则放在**后端一处**(SQL 里两级 `NULLIF(TRIM(...), '')` + `COALESCE`),
   不让前端再写一遍 —— 同一条规则写两处,迟早有一处跟不上,
   而两边各自看都"对",只有客户看到的那个名字是旧的。

🔴 「空」= NULL / "" / 仅空白三种。品牌现值为空**不算现值**;两级都空**返 None
   不返空串**(两级都空时怎么显示 → 契约写在 `get_paid_clients` 的 docstring 里,
   那是**显示**默认值,由前端一个共用 helper 实现,不是数据层回落)。

本包连**真 PostgreSQL**:这一整条是 SQL 的行为,假对象复现出来的只是
"我的假对象按我想的那样工作"。
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL")

BRAND_FULL, BRAND_NO_IND = 970101, 970102
Q_FULL, Q_NO_IND, Q_NO_BRAND, Q_ALL_EMPTY = 97001, 97002, 97003, 97004

CUR_NAME = "揭阳阁揭阳中路雅栖酒店"
CUR_IND = "酒店住宿 / 商务出行 / 中端连锁酒店"
SNAP_NAME = "报价快照里的旧名"
SNAP_IND = "餐饮食品"


def _conn():
    c = psycopg2.connect(DSN)
    c.cursor_factory = psycopg2.extras.RealDictCursor
    return c


@pytest.fixture(scope="module", autouse=True)
def _guard_and_seed():
    """🔴 不许 `except: pass`。建不起来就当场炸 ——
    吞掉准备阶段的失败会让"没跑成"伪装成"跑了但判据红了"。
    """
    if not DSN:
        pytest.fail("TEST_DATABASE_URL 未设置 —— 本包必须连真库")
    # 🔴 `db/connection.py` 读的是 **DATABASE_URL**,不是 TEST_DATABASE_URL。
    #    两个不一致就说明被测函数连的根本不是我以为的那个库 ——
    #    那样"验了"验的是别的对象。宁可炸。
    assert os.environ.get("DATABASE_URL") == DSN, (
        "DATABASE_URL 与 TEST_DATABASE_URL 不一致 —— 被测函数连的不是本包这个库")
    assert "_test" in DSN and ("localhost" in DSN or "127.0.0.1" in DSN), (
        "本包只许连本机测试库:%r" % DSN)

    c = _conn()
    cur = c.cursor()
    cur.execute("SELECT to_regclass('brands') AS b, to_regclass('quotes') AS q")
    row = cur.fetchone()
    missing = [k for k in ("b", "q") if row[k] is None]
    assert not missing, "测试库缺表 %s —— 请先灌生产 schema 快照(见交付单)" % (missing,)

    cur.execute("DELETE FROM quotes WHERE id = ANY(%s)",
                ([Q_FULL, Q_NO_IND, Q_NO_BRAND, Q_ALL_EMPTY],))
    cur.execute("DELETE FROM brands WHERE id = ANY(%s)", ([BRAND_FULL, BRAND_NO_IND],))
    cur.execute("INSERT INTO brands (id, name, industry) VALUES (%s,%s,%s)",
                (BRAND_FULL, CUR_NAME, CUR_IND))
    # 行业只有空白 —— 「空」的三种形态里最容易漏的那种
    cur.execute("INSERT INTO brands (id, name, industry) VALUES (%s,%s,%s)",
                (BRAND_NO_IND, "只有名字的品牌", "   "))
    rows = (
        (Q_FULL, BRAND_FULL, SNAP_NAME, SNAP_IND),
        (Q_NO_IND, BRAND_NO_IND, SNAP_NAME, SNAP_IND),
        # brand_id 为空(quotes.brand_id 有 FK,"品牌行不存在"在生产里就长这样)
        (Q_NO_BRAND, None, SNAP_NAME, SNAP_IND),
        # 两级都空:品牌没有 + 快照也是空白
        (Q_ALL_EMPTY, None, "  ", ""),
    )
    for qid, bid, name, ind in rows:
        cur.execute(
            "INSERT INTO quotes (id, brand_id, brand_name, industry, status)"
            " VALUES (%s,%s,%s,%s,'paid')", (qid, bid, name, ind))
    c.commit()
    c.close()
    yield
    c = _conn()
    cur = c.cursor()
    cur.execute("DELETE FROM quotes WHERE id = ANY(%s)",
                ([Q_FULL, Q_NO_IND, Q_NO_BRAND, Q_ALL_EMPTY],))
    cur.execute("DELETE FROM brands WHERE id = ANY(%s)", ([BRAND_FULL, BRAND_NO_IND],))
    c.commit()
    c.close()


def _rows():
    from db.monitoring_db import get_paid_clients
    got = {r["quote_id"]: r for r in get_paid_clients(limit=500)}
    for qid in (Q_FULL, Q_NO_IND, Q_NO_BRAND, Q_ALL_EMPTY):
        assert qid in got, (
            "种下的 quote %s 没出现在返回里 —— 读数不成立,别往下断言" % qid)
    return got


# ══════════════════════════════════════════════════════════════════
# 一、正臂:现值赢
# ══════════════════════════════════════════════════════════════════

def test_the_row_carries_the_current_brand_name_and_industry():
    """🔴 本单的全部意义:客户条上的名字/行业要跟顶部品牌卡是**同一个**。"""
    r = _rows()[Q_FULL]
    assert r["brand_current_name"] == CUR_NAME
    assert r["brand_current_industry"] == CUR_IND


def test_the_snapshot_fields_are_untouched():
    """🔴 快照字段**不许被顶掉** —— 它们是对账用的,和"现在显示什么"是两件事。

    少了这一条,把 SQL 改成直接覆盖 `q.brand_name` 也能让上面那条绿。
    """
    r = _rows()[Q_FULL]
    assert r["brand_name"] == SNAP_NAME
    assert r["industry"] == SNAP_IND


# ══════════════════════════════════════════════════════════════════
# 二、反臂:空不算现值
# ══════════════════════════════════════════════════════════════════

def test_an_empty_brand_industry_falls_back_to_the_snapshot():
    """🔴 反向对照(Review 点名):品牌行业为空 ⇒ **回落快照**。

    空不算"现值" —— 覆盖过去是把"显示了旧的"换成"什么都不显示",那更糟。
    这里品牌行业是三个空格:没有 `TRIM` 的话它会被当成有值,这条就红。
    """
    r = _rows()[Q_NO_IND]
    assert r["brand_current_industry"] == SNAP_IND, "空白行业没有回落到快照"
    assert r["brand_current_name"] == "只有名字的品牌", "名字还在,不该跟着回落"


def test_a_quote_without_a_brand_falls_back_entirely():
    """🔴 `brand_id` 为空(LEFT JOIN 出 NULL)⇒ 两个字段都回落快照。"""
    r = _rows()[Q_NO_BRAND]
    assert r["brand_current_name"] == SNAP_NAME
    assert r["brand_current_industry"] == SNAP_IND


def test_both_levels_empty_yields_none_not_an_empty_string():
    """🔴 两级都空 ⇒ **None**,不是 `""`、也不是 `"  "`。

    空串会让前端多分辨一次"没有值 vs 值是空字符串" —— 那正是
    "同一条规则两处实现"的入口。后端一次说清:没有就是 None。
    """
    r = _rows()[Q_ALL_EMPTY]
    assert r["brand_current_name"] is None, "得到 %r" % (r["brand_current_name"],)
    assert r["brand_current_industry"] is None, "得到 %r" % (r["brand_current_industry"],)


# ══════════════════════════════════════════════════════════════════
# 三、形状与契约
# ══════════════════════════════════════════════════════════════════

def _fn_src():
    import io
    src = io.open(REPO / "db" / "monitoring_db.py", encoding="utf-8").read()
    start = src.index("def get_paid_clients(")
    return src[start: src.index("\ndef ", start + 10)]


def test_every_row_carries_both_keys():
    """🔴 字段对**所有消费方一律附带**,不按调用方分支。

    已知消费方两个(`pages/Monitoring/index.tsx` 与 `services/m3/deliveryApi.ts:136`),
    行形状是共用的 —— 这一格钉住"不存在某些行没有这两个键"的情况。
    """
    for qid, r in _rows().items():
        assert "brand_current_name" in r, "quote %s 少了 brand_current_name" % qid
        assert "brand_current_industry" in r, "quote %s 少了 brand_current_industry" % qid


def test_the_fallback_is_written_once_in_sql():
    """🔴 回落只写一处,且**两级都要 NULLIF(TRIM(...))**。

    只套第一级(品牌)也能让正臂和"空白行业"那条绿,
    却会在两级都空时返回 `"  "` —— 所以这一格分别点名两级四处。
    """
    src = _fn_src()
    for expr in ("NULLIF(TRIM(b.name), '')", "NULLIF(TRIM(q.brand_name), '')",
                 "NULLIF(TRIM(b.industry), '')", "NULLIF(TRIM(q.industry), '')"):
        assert expr in src, "SQL 里缺 %s —— 「空」的三种形态没收全" % expr
    assert src.count("COALESCE(NULLIF(TRIM(") == 2, "回落不是两列各一处"


def test_it_is_one_join_not_a_per_row_lookup():
    """🔴 一次 LEFT JOIN,不许逐行查品牌 —— N+1 在这条列表上会很难看。"""
    src = _fn_src()
    assert "LEFT JOIN brands b" in src, "不是用 JOIN 带出来的"
    assert src.count("cursor.execute") == 1, (
        "这个函数里执行了 %d 次查询 —— 逐行查品牌就是 N+1" % src.count("cursor.execute"))


# ══════════════════════════════════════════════════════════════════
# 四、生产方花名册(轴订正:我原先数的是消费方)
# ══════════════════════════════════════════════════════════════════

#: 除本函数以外,**自己拼同一种客户条**的地方,连同"给不给那两个键"的决定。
#: 🔴 我 09-20 把契约写成「对所有消费方一律附带」—— 枚举的是**消费方**,
#:    而缺口在**另一个生产方**(A 在前端摸出 `_demo_clients`)。
#:    按后果定域重数一遍:后果是"客户条上显示了什么",
#:    域就是**每一个产出这种行的地方**,不是每一个读它的地方。
#: ``emits`` = 这条链**今天给不给**那两个字段。改成 True 的人必须同时在
#: ``why`` 里写明**怎么处理 None** —— 因为这两条链的拼行方式会把 None 键整个丢掉。
KNOWN_PRODUCERS = {
    ("services/demo_access.py", "_demo_clients"): {
        "emits": False,
        "why": "演示态整条路径被接管,按快照自拼行;`if value is not None` 决定键在不在 ⇒ 不给这两个键",
    },
    ("services/admin_cross_tenant_governance.py", "_monitoring_snapshot"): {
        "emits": False,
        "why": "产出落盘快照里的 clients;已落盘的存量快照没有这两个键,结构上补不回来 ⇒ 不给",
    },
}

#: 认"客户条"的形状。比 {quote_id, brand_name} 严 —— 那个松形状全仓 21 处命中,
#: 里面大半是 portal token / 报价回执之类,不是这条列表的行。
ROW_SHAPE = {"quote_id", "brand_name", "industry", "status", "service_start_date"}

#: 本单新加的那两个字段名
FIELDS = ("brand_current_name", "brand_current_industry")


def _dict_keys(node):
    if not isinstance(node, ast.Dict):
        return set()
    return {k.value for k in node.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _scan_producers():
    found = {}
    for p in sorted(REPO.rglob("*.py")):
        rel = p.relative_to(REPO).as_posix()
        if rel.startswith(("tests/", "scripts/")) or "node_modules" in rel:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"), rel)
        except Exception:
            continue          # 仓里有非 utf-8 的历史备份文件,跳过但不吞掉统计
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for n in ast.walk(fn):
                if isinstance(n, ast.Dict) and ROW_SHAPE <= _dict_keys(n):
                    found[(rel, fn.name)] = n.lineno
    return found


def test_no_unregistered_producer_of_this_row():
    """🔴 再出现第四个自拼客户条的地方就红 —— 逼它当场决定给不给这两个键。

    这一格不是"证明现在没问题",是**把轴钉住**:下一个人加生产方时,
    不会像这次一样要等前端摸出来才发现。
    """
    found = _scan_producers()
    assert found, "一个生产方都没扫到 —— 扫描器坏了,不是真的没有"
    unknown = sorted(k for k in found if k not in KNOWN_PRODUCERS)
    assert not unknown, (
        "这些地方自己拼了客户条却没登记(行号 %s):%s" %
        ([found[k] for k in unknown], unknown))


def test_the_roster_has_not_rotted():
    """🔴 花名册会过期:登记的生产方必须还在,理由必须写清楚。

    少了这一格,上面那条会因为"名单越列越长"而永远绿。
    """
    found = _scan_producers()
    dead = sorted(k for k in KNOWN_PRODUCERS if k not in found)
    assert not dead, "这些登记项已经不在代码里了,该删:%s" % (dead,)
    for k, entry in KNOWN_PRODUCERS.items():
        assert isinstance(entry.get("emits"), bool), "登记要写明给不给:%s" % (k,)
        assert len(entry.get("why", "")) > 15, "登记必须写清理由:%s" % (k,)


def _fn_node(rel, name):
    tree = ast.parse((REPO / rel).read_text(encoding="utf-8"), rel)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def _drops_none_keys(comp):
    """这个推导式是不是「值为 None 就把键丢掉」。"""
    for gen in getattr(comp, "generators", []):
        for test in gen.ifs:
            for n in ast.walk(test):
                if isinstance(n, ast.Compare) and any(
                        isinstance(op, ast.IsNot) for op in n.ops):
                    if any(isinstance(c, ast.Constant) and c.value is None
                           for c in n.comparators):
                        return True
    return False


@pytest.mark.parametrize("rel,name", sorted(KNOWN_PRODUCERS))
def test_a_registered_producer_cannot_emit_these_keys_through_a_none_filter(rel, name):
    """🔴 A 09-20 指出的将来那一脚:②③ 都用
    ``{k: v for k, v in {...}.items() if value is not None}`` 拼行 ——
    **值为 None 的键会被整个丢掉**。

    所以将来谁给这两条链补上这两个字段,只要那次算出来是 ``None``,
    键就又消失了:我定义的三态在这条链上**塌回两态**,
    而且不报错、读数与"这条链不提供现值"**完全一样**。

    花名册锁抓的是"第四个自拼点",抓不到这个 —— 这一格专抓它:
    这两个字段名**不许出现在会丢 None 的推导式里**。
    今天两条链都不产这两个键 ⇒ 本格静默;谁去补,谁当场被逼着绕开那个过滤。
    """
    fn = _fn_node(rel, name)
    assert fn is not None, "找不到 %s::%s —— 花名册指向的东西没了" % (rel, name)

    parents = {}
    for node in ast.walk(fn):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    # 🔴 A 09-20 追问"两层推导式会不会停在内层" ⇒ 实测**不会**:往上一直走,
    #    直到遇到带过滤的那一层(嵌套那发毒 CAUGHT,报在正确的行号上)。
    #    但同一轮实测查出**另一个真盲区**:把过滤抽成 helper
    #    (`_compact({...})`)之后,那个推导式是**兄弟不是祖先**,锁看不见(ALIVE)。
    #    所以这里多走一跳:本模块里"会丢 None 键"的函数名也算过滤。
    filters = {f.name for f in ast.walk(
        ast.parse((REPO / rel).read_text(encoding="utf-8"), rel))
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(isinstance(c, (ast.DictComp, ast.ListComp, ast.SetComp,
                               ast.GeneratorExp)) and _drops_none_keys(c)
                for c in ast.walk(f))}

    bad = []
    for n in ast.walk(fn):
        if not (isinstance(n, ast.Constant) and n.value in FIELDS):
            continue
        cur = parents.get(id(n))
        while cur is not None:
            comp = isinstance(cur, (ast.DictComp, ast.ListComp, ast.SetComp,
                                    ast.GeneratorExp)) and _drops_none_keys(cur)
            helper = (isinstance(cur, ast.Call)
                      and getattr(cur.func, "id", None) in filters)
            if comp or helper:
                bad.append("%s:%d %r%s" % (rel, n.lineno, n.value,
                                           "(经 helper)" if helper else ""))
                break
            cur = parents.get(id(cur))
    assert not bad, (
        "这两个字段被放进了「值为 None 就丢键」的推导式:%s\n"
        "⇒ 两级都空时键会消失,三态塌成两态,且读数与「这条链不提供现值」同形。\n"
        "要给这条链补现值,得先绕开那个过滤(显式允许 None),再来改本格。" % (bad,))


@pytest.mark.parametrize("rel,name", sorted(KNOWN_PRODUCERS))
def test_a_producer_that_starts_emitting_must_say_so_in_the_roster(rel, name):
    """🔴 上面那格靠**认形状**(推导式 / 会丢 None 的 helper),形态是开放集 ——
    `**` 展开、`dict(**d)`、`row.pop`、pydantic ``exclude_none`` 都绕得过去。

    所以再配一格**按存在**判:登记说"不给"的链里,这两个字段名**一个都不许出现**。
    谁真要给它补,就得回花名册把 ``emits`` 改 True,并在 ``why`` 里写明
    **怎么处理 None** —— 那是个一行的代价,换来"这件事被想过一次"。

    (这不是路障:改一行登记就过。路障是"无论怎么写都不许补"。)
    """
    entry = KNOWN_PRODUCERS[(rel, name)]
    fn = _fn_node(rel, name)
    assert fn is not None, "找不到 %s::%s" % (rel, name)
    hit = sorted({"%d:%r" % (n.lineno, n.value) for n in ast.walk(fn)
                  if isinstance(n, ast.Constant) and n.value in FIELDS})
    if entry["emits"]:
        # 🔴 不用 skip:skip 掉的判据什么都不保护。登记说给,就得**真给**。
        assert hit, (
            "%s::%s 登记为「给这两个键」,代码里却一个都没有 —— 登记先于实现或已过期" %
            (rel, name))
        return
    assert not hit, (
        "%s::%s 登记为「不给这两个键」,但代码里出现了:%s\n"
        "⇒ 若你确实给它补了,把花名册里的 emits 改 True 并写明怎么处理 None。" %
        (rel, name, hit))


def test_the_contract_defines_the_absent_key_case():
    """🔴 「键不存在」和「值是 None」**必须是两件事**。

    演示态那两个生产方按 `if value is not None` 拼行,而 ③ 的存量快照里
    这两个键根本没有 —— 所以 undefined 在演示态**结构上消不掉**。
    前端若把 undefined 当 None,演示页会把旧名换成「品牌 #19」:
    一个"修好了显示"的改动,在另一条链上变成"把有的显示成没有"。
    """
    from db.monitoring_db import get_paid_clients
    doc = get_paid_clients.__doc__ or ""
    assert "键不存在" in doc and "undefined" in doc, "契约没把「键不存在」单独说清"
    assert "_demo_clients" in doc, "契约没点名演示态那个生产方"
    assert "_monitoring_snapshot" in doc, "契约没点名快照那个生产方"


def test_the_contract_lives_next_to_the_row_it_describes():
    """🔴 跨窗契约要放在**一个 sha 能指到的地方**,不是放在聊天记录里。

    两个消费方 + 两级回落 + 两级都空时的显示默认值 —— 这些前端要照着做的事,
    写在构造这一行的那个函数的 docstring 上:它和代码一起被 review、一起过期。
    (消息是两份副本,会各自演化;这里是一份原件。)
    """
    from db.monitoring_db import get_paid_clients
    doc = get_paid_clients.__doc__ or ""
    for token in ("brand_current_name", "brand_current_industry",
                  "deliveryApi.ts", "Monitoring/index.tsx",
                  "品牌 #", "未填写", "未命名客户"):
        assert token in doc, "契约里没写 %r —— 前端就得自己猜" % token
